#!/usr/bin/env python3
"""
iptv-radar 采集层: scan_multicast.py
扫描组播IP段(233.50.200.0/24 + 201.0/24 + 202.0/24),通过 msd_lite HTTP 接口探测。
结果写入 sources 表(技术属性)。只写事实,不做命名/分组/优选。

优化(见 REFACTOR_DESIGN.md 五.六):
  - 优化probe参数(8M),救回高码率4K
  - 并发适中(默认8),避免多路组播抢IGMP/带宽
  - 失败重试(默认2次)
  - 新源(DB没见过的)记录待识别(截图由orphan_export产出)
  - 更新 available/fail_count/last_scan;写 scan_history

运行: python3 scan_multicast.py [--db] [--msd HOST:PORT] [--segments 200,201,202]
                                 [--workers 8] [--retry 2] [--bitrate]
"""
import sqlite3
import db_util
import os
import sys
import argparse
import datetime
import uuid
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe

RADAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
DEFAULT_DB = os.path.join(RADAR, 'data', 'iptv.db')
NOW = lambda: datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def scan_one_once(msd, ip, timeout, want_bitrate):
    """扫单个组播IP,单次探测(不内部重试——靠外层三轮递进重试)。返回(address, result)"""
    address = f"{ip}:5140"
    url = f"http://{msd}/rtp/{address}"
    r = probe.probe_stream(url, timeout=timeout)
    if r.get('available') and want_bitrate:
        r['vbitrate'] = probe.measure_bitrate(url, duration=4)
    return address, r


def address_known(address, known):
    """地址是否是库里已知源(曾扫到过)。已知源的失败值得重试,未知空地址不重试。"""
    return address in known


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--msd', '--udpxy', dest='msd', default='127.0.0.1:4088',
                    help='msd_lite/udpxy 地址 HOST:PORT (组播转HTTP)')
    ap.add_argument('--mode', choices=['full', 'known'], default='known',
                    help="full=扫全部段(初始化/每月,发现新频道,慢); known=只扫库里已知源(每周cron,快)")
    ap.add_argument('--segments', default='200,201,202', help='full模式要扫的第三段,逗号分隔')
    ap.add_argument('--range', default='0-255', help='full模式第四段范围')
    ap.add_argument('--bitrate', action='store_true', help='对可用源实测码率(慢)')
    args = ap.parse_args()

    # 三轮递进配置: (线程数, ffprobe超时秒)。逐轮降并发+给足超时,治并发误报。
    # 实测: 第1轮4并发J1900不过载,80源零失败(6并发失败10个反而要靠第2轮慢慢救)。
    # 慢源单独探测需8s+,故重试轮超时要够长(并发低了可容忍单个等更久),不能缩短。
    ROUNDS = [(4, 12), (2, 15), (1, 18)]

    run_id = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')

    conn = db_util.connect(args.db)
    c = conn.cursor()
    # 已知源(判断新源 + known模式的扫描范围) address→source_id
    known = {r[0]: r[1] for r in c.execute(
        "SELECT address, source_id FROM sources WHERE source_type='multicast'")}

    # 扫描范围: full=全部768段; known=仅库里已知源
    if args.mode == 'full':
        lo, hi = map(int, args.range.split('-'))
        segs = [int(x) for x in args.segments.split(',')]
        ips = [f"233.50.{s}.{i}" for s in segs for i in range(lo, hi + 1)]
        scope = f"full 全量({segs}段 {lo}-{hi})"
    else:
        ips = [addr.split(':')[0] for addr in known.keys()]
        scope = f"known 增量(库里{len(ips)}个已知源)"

    print("=" * 55)
    print(f"  iptv-radar 组播扫描  run={run_id}")
    print(f"  模式: {scope}  共{len(ips)}个IP")
    print(f"  三轮递进: {ROUNDS}  码率:{args.bitrate}")
    print("=" * 55)

    t0 = datetime.datetime.now()
    results = {}

    def run_round(ip_list, workers, timeout, label):
        """跑一轮扫描(每个源单次探测,不内部重试——靠外层多轮递进)。
        用 future 硬超时保护,避免单个卡死拖死 as_completed。返回 {address: result}。"""
        out = {}
        done = 0
        per_src_timeout = timeout + 15  # future硬上限,略大于ffprobe超时
        with ThreadPoolExecutor(max_workers=workers) as pool:
            fut2ip = {pool.submit(scan_one_once, args.msd, ip, timeout, args.bitrate): ip
                      for ip in ip_list}
            for fut in as_completed(fut2ip):
                done += 1
                ip = fut2ip[fut]
                try:
                    address, r = fut.result(timeout=per_src_timeout)
                except Exception:
                    address, r = f"{ip}:5140", {'available': 0, 'status': 'TIMEOUT'}
                out[address] = r
                if r.get('available'):
                    print(f"  [{label} {done}/{len(ip_list)}] ✅ {address:<20} "
                          f"{r.get('resolution',''):<10} {r.get('video_codec',''):<5} {r.get('hdr','')}", flush=True)
                elif done % 80 == 0:
                    print(f"  [{label} {done}/{len(ip_list)}] ...", flush=True)
        return out

    # ===== 第1轮: 全范围粗筛 =====
    w, to = ROUNDS[0]
    print(f"\n>>> 第1轮: {w}并发/{to}s 扫 {len(ips)} 个IP")
    results = run_round(ips, w, to, "R1")

    # ===== 第2/3轮: 只重扫"已知源中失败的"(未知空地址不反复重扫,省时间) =====
    # 关键: known.keys()是曾扫到过的真实源,值得重试;未知地址5XX大概率是空,不重扫
    for rnd in (1, 2):
        w, to = ROUNDS[rnd]
        retry_ips = [addr.split(':')[0] for addr, r in results.items()
                     if not r.get('available')
                     and r.get('status') != 'DEAD'
                     and address_known(addr, known)]
        if not retry_ips:
            print(f"\n>>> 第{rnd+1}轮: 无需重扫(已知源全部可用)")
            break
        print(f"\n>>> 第{rnd+1}轮: {w}并发/{to}s 重扫 {len(retry_ips)} 个已知源(救误报)")
        rescued = 0
        rr = run_round(retry_ips, w, to, f"R{rnd+1}")
        for address, r in rr.items():
            if r.get('available'):
                results[address] = r
                rescued += 1
        print(f"  第{rnd+1}轮救回 {rescued}/{len(retry_ips)}")

    found = sum(1 for r in results.values() if r.get('available'))

    # 写库
    new_sources = []
    for address, r in results.items():
        avail = r.get('available', 0)
        # scan_history
        c.execute("""INSERT INTO scan_history(scan_date,scan_run,address,available,resolution,vbitrate,status)
                     VALUES (?,?,?,?,?,?,?)""",
                  (NOW(), run_id, address, avail, r.get('resolution', ''),
                   r.get('vbitrate', 0), r.get('status', '')))
        if address in known:
            # 更新已有源
            if avail:
                c.execute("""UPDATE sources SET available=1,resolution=?,res_label=?,video_codec=?,
                    fps=?,vbitrate=CASE WHEN ?>0 THEN ? ELSE vbitrate END,hdr=?,audio_codec=?,
                    audio_channels=?,fail_count=0,last_seen=?,last_scan=? WHERE address=?""",
                    (r.get('resolution', ''), r.get('res_label', ''), r.get('video_codec', ''),
                     r.get('fps', 0), r.get('vbitrate', 0), r.get('vbitrate', 0),
                     r.get('hdr', ''), r.get('audio_codec', ''), r.get('audio_channels', 0),
                     NOW(), NOW(), address))
            else:
                c.execute("""UPDATE sources SET available=0,fail_count=fail_count+1,last_scan=?
                             WHERE address=?""", (NOW(), address))
        elif avail:
            # 新源(只记录可用的新源)
            c.execute("""INSERT OR IGNORE INTO sources
                (source_type,address,available,resolution,res_label,video_codec,fps,vbitrate,
                 hdr,audio_codec,audio_channels,fail_count,first_seen,last_seen,last_scan)
                VALUES ('multicast',?,1,?,?,?,?,?,?,?,?,0,?,?,?)""",
                (address, r.get('resolution', ''), r.get('res_label', ''), r.get('video_codec', ''),
                 r.get('fps', 0), r.get('vbitrate', 0), r.get('hdr', ''), r.get('audio_codec', ''),
                 r.get('audio_channels', 0), NOW(), NOW(), NOW()))
            new_sources.append(address)

    conn.commit()
    conn.close()

    elapsed = (datetime.datetime.now() - t0).total_seconds()
    print("\n" + "=" * 55)
    print(f"  完成! 耗时{elapsed:.0f}秒")
    print(f"  可用: {found}/{len(ips)}   新源: {len(new_sources)}")


if __name__ == '__main__':
    main()
