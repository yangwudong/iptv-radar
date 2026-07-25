#!/usr/bin/env python3
"""
iptv-radar 采集层: 通用流探测模块 (probe.py)
被 scan_multicast.py / scan_rtsp.py 复用。

设计原则(见 REFACTOR_DESIGN.md 五.六 扫描优化):
  - 优化参数: analyzeduration/probesize 给足(默认8M),救回高码率4K
  - 去掉 +nobuffer (组播要缓冲等关键帧)
  - 进程级超时强杀 (借鉴iptv-checker,比subprocess.timeout更可靠)
  - 只输出事实,不做判断/清理
"""
import subprocess
import json
import re
import os
import select
import signal
import time

# HDR判定: color_transfer/primaries/pix_fmt 组合
def detect_hdr(color_transfer, color_primaries, pix_fmt):
    ct = (color_transfer or '').lower()
    if 'arib-std-b67' in ct or 'hlg' in ct:
        return 'HLG'
    if 'smpte2084' in ct or 'pq' in ct:
        return 'HDR10'
    if 'bt2020' in (color_primaries or '').lower() and '10' in (pix_fmt or ''):
        return 'HDR'  # bt2020 + 10bit 但transfer未明确
    return 'SDR'


def res_label(w):
    return {3840: '4K', 1920: '1080P', 1280: '720P', 720: 'SD'}.get(w, f'{w}p' if w else '')


def parse_fps(s):
    try:
        if '/' in s:
            n, d = s.split('/')
            return round(int(n) / int(d), 1) if int(d) else 0
        return round(float(s), 1)
    except Exception:
        return 0


def run_with_timeout(cmd, timeout_sec):
    """启动子进程,超时强杀(进程组),返回(stdout, stderr, ok)"""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 start_new_session=True)
    except Exception:
        return b'', b'', False
    try:
        # communicate 自带超时,且会持续读取管道(避免PIPE缓冲满导致的死锁)
        out, err = proc.communicate(timeout=timeout_sec)
        return out, err, (proc.returncode == 0)
    except subprocess.TimeoutExpired:
        # 超时: 强杀整个进程组(ffprobe可能卡在读组播流)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        # 必须回收,避免僵尸进程 + 确保管道关闭(否则父进程可能卡在等待)
        try:
            proc.communicate(timeout=3)
        except Exception:
            try:
                proc.wait(timeout=3)
            except Exception:
                pass
        return b'', b'', False
    except Exception:
        try:
            proc.kill(); proc.wait(timeout=3)
        except Exception:
            pass
        return b'', b'', False


def probe_stream(url, timeout=12, analyzeduration=8_000_000, probesize=8_000_000,
                 rtsp_transport=None):
    """
    探测一个流,返回 dict:
      {available, resolution, res_label, video_codec, fps, hdr,
       audio_codec, audio_channels, status}
    status: OK(有视频) / AUDIO_ONLY(仅音频,如广播) / DEAD(源不存在5XX/404)
            / NO_VIDEO(有响应但没解析出流,可能偶发) / TIMEOUT / ERROR
    """
    # -rw_timeout: ffprobe自身读写超时(微秒),让它卡在读组播流时能自己退出,
    #   不依赖外部强杀(实测外部kill对卡读的ffprobe不可靠)。设为外层timeout的80%,让它先自退。
    rw_timeout_us = int(timeout * 0.8 * 1_000_000)
    cmd = ['ffprobe', '-v', 'error', '-print_format', 'json',
           '-rw_timeout', str(rw_timeout_us),
           '-show_streams',
           '-analyzeduration', str(analyzeduration),
           '-probesize', str(probesize)]
    if rtsp_transport:
        cmd += ['-rtsp_transport', rtsp_transport]
    cmd.append(url)

    out, err, ok = run_with_timeout(cmd, timeout)
    errtext = (err or b'').decode('utf-8', 'replace')
    # 源明确不存在(404/连接被拒/无路由) → DEAD,重试无意义
    if any(k in errtext for k in ('404 Not Found', 'Connection refused', 'No route to host')):
        return {'available': 0, 'status': 'DEAD'}
    # 服务器临时忙/过载(5XX/503/500) → BUSY,可重试(msd_lite并发压力下的临时拒绝,非源失效)
    if any(k in errtext for k in ('5XX', '500', '502', '503', 'Server returned 5')):
        return {'available': 0, 'status': 'BUSY'}
    if not out:
        return {'available': 0, 'status': 'TIMEOUT'}
    try:
        data = json.loads(out.decode('utf-8', 'replace'))
    except Exception:
        return {'available': 0, 'status': 'ERROR'}

    video, audio = {}, {}
    for s in data.get('streams', []):
        if s.get('codec_type') == 'video' and not video:
            video = s
        elif s.get('codec_type') == 'audio' and not audio:
            audio = s

    w = video.get('width', 0)
    if not w:
        # 有音频无视频 = 广播类(有效); 完全无流 = 偶发失败(可重试)
        if audio:
            return {'available': 1, 'status': 'AUDIO_ONLY',
                    'resolution': '', 'res_label': '', 'video_codec': '', 'fps': 0, 'hdr': '',
                    'audio_codec': audio.get('codec_name', ''),
                    'audio_channels': audio.get('channels', 0)}
        return {'available': 0, 'status': 'NO_VIDEO'}

    return {
        'available': 1,
        'status': 'OK',
        'resolution': f"{w}x{video.get('height', 0)}",
        'res_label': res_label(w),
        'video_codec': video.get('codec_name', ''),
        'fps': parse_fps(video.get('r_frame_rate', '0/1')),
        'hdr': detect_hdr(video.get('color_transfer'), video.get('color_primaries'),
                          video.get('pix_fmt')),
        'audio_codec': audio.get('codec_name', ''),
        'audio_channels': audio.get('channels', 0),
    }


def measure_bitrate(url, duration=4, timeout=15, rtsp_transport=None):
    """实测视频码率(bps): 抓duration秒流,算 bytes*8/duration

    注意: 必须用 select 非阻塞轮询读,不能直接 proc.stdout.read()。
    ffmpeg 卡在读流时(信号衰减/IGMP join延迟/僵尸源)stdout 既无数据也不关闭,
    阻塞 read() 会永久挂住 —— 外层 while 只在两次读之间检查时间,救不回来。
    该函数在 ThreadPoolExecutor 里被调用,一个线程挂死会让整轮扫描的
    shutdown(wait=True) 永远等不到结束,整条 pipeline 卡住。
    (同文件 trace_rtsp_redirects 用的就是下面这个 select 轮询写法。)
    """
    cmd = ['ffmpeg', '-v', 'quiet']
    if rtsp_transport:
        cmd += ['-rtsp_transport', rtsp_transport]
    cmd += ['-i', url, '-t', str(duration), '-c', 'copy', '-f', 'mpegts', '-']
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
    except Exception:
        return 0
    start = time.time()
    total = 0
    try:
        while time.time() - start < timeout:
            try:
                r, _, _ = select.select([proc.stdout], [], [], 0.2)
            except Exception:
                break
            if r:
                try:
                    chunk = os.read(proc.stdout.fileno(), 65536)
                except OSError:
                    break
                if not chunk:      # EOF: ffmpeg 正常结束
                    break
                total += len(chunk)
            elif proc.poll() is not None:
                break              # 进程已退出且无残留数据
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass
    if total > 0:
        return int(total * 8 / duration)
    return 0


def capture_screenshots(url, out_dir, prefix, count=3, offsets=(3, 8, 15),
                        rtsp_transport=None, timeout=20):
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for i in range(count):
        off = offsets[i] if i < len(offsets) else (offsets[-1] + (i - len(offsets) + 1) * 5)
        outp = os.path.join(out_dir, f"{prefix}_{i+1}.jpg")
        cmd = ['ffmpeg', '-y', '-v', 'quiet']
        if rtsp_transport:
            cmd += ['-rtsp_transport', rtsp_transport]
        cmd += ['-analyzeduration', '5000000', '-probesize', '5000000',
                '-i', url, '-ss', str(off), '-frames:v', '1', '-q:v', '2', outp]
        _, _, ok = run_with_timeout(cmd, timeout)
        if os.path.exists(outp) and os.path.getsize(outp) > 1000:
            paths.append(outp)
    return paths


def trace_rtsp_redirects(url, timeout=15):
    """追踪RTSP重定向链(用ffmpeg -v verbose解析302)。
    返回 {chain:[ip1,ip2,...], hops:N, loop:bool, final_ok:bool, status}"""
    cmd = ['ffmpeg', '-v', 'verbose', '-rtsp_transport', 'tcp',
           '-i', url, '-t', '1', '-f', 'null', '-']
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                 start_new_session=True)
    except Exception:
        return {'chain': [], 'hops': 0, 'loop': False, 'final_ok': False, 'status': 'ERROR'}
    start = time.time()
    stderr_data = b''
    while time.time() - start < timeout:
        ret = proc.poll()
        if ret is not None:
            try:
                stderr_data += proc.stderr.read() or b''
            except Exception:
                pass
            break
        try:
            r, _, _ = select.select([proc.stderr], [], [], 0.2)
            if r:
                stderr_data += os.read(proc.stderr.fileno(), 65536)
        except Exception:
            time.sleep(0.1)
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        pass

    text = stderr_data.decode('utf-8', 'replace')
    # 入口IP
    entry = re.search(r'rtsp://(\d+\.\d+\.\d+\.\d+)', url)
    chain = [entry.group(1)] if entry else []
    for m in re.finditer(r'Redirecting to rtsp://(\d+\.\d+\.\d+\.\d+)', text):
        chain.append(m.group(1))
    # 死循环检测(同IP出现2次)
    loop = len(chain) != len(set(chain))
    # 成功判定: 有Input #0 from 且拿到流
    final_ok = 'Input #0' in text and ('Video:' in text or 'Stream #0' in text)
    status = 'LOOP' if loop else ('OK' if final_ok else 'FAIL')
    return {'chain': chain, 'hops': len(chain) - 1, 'loop': loop,
            'final_ok': final_ok, 'status': status}
