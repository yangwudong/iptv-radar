#!/usr/bin/env python3
"""
iptv-radar 采集层: scan_rtsp.py
扫描RTSP单播地址(来自EPG channels.json),探测流信息+追踪重定向链。
结果写入 sources 表(source_type='rtsp')。

RTSP地址特点:
  - 入口 115.233.40.137:554,302重定向到实际流媒体(4K链最长4跳)
  - 用TCP传输更稳(带宽紧时抗丢包)
  - 重定向链追踪 = 实现TODO任务C的核心(检测死循环)

运行: python3 scan_rtsp.py [--db] [--epg channels.json] [--workers 4] [--trace]
注: RTSP扫描比组播重,并发别太高(默认4)
"""
import sqlite3
import db_util
import os
import sys
import re
import json
import argparse
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe

RADAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
DEFAULT_DB = os.path.join(RADAR, 'data', 'iptv.db')
# 默认EPG指向仓库内的 reference/(曾误写成 RADAR/../channels.json,解析到项目目录之外,
# 不带 --epg 跑必然 FileNotFoundError)。运行时优先用 fetch_channels 刷出的 channels.json。
# 优先用 fetch_channels 刷出的(含新token)channels.json,否则退到仓库自带的脱敏样例
_CANDS = [os.path.join(RADAR, 'data', 'channels.json'),
          os.path.join(RADAR, 'reference', 'channels.json')]      # 后者=旧部署位置
DEFAULT_EPG = next((c for c in _CANDS if os.path.isfile(c)),
                   os.path.join(RADAR, 'reference', 'channels.sample.json'))
NOW = lambda: datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def simplify_rtsp(url):
    """RTSP简化版(去掉.smil后参数,静态可用)"""
    m = re.match(r'(rtsp://[^?]+\.smil)', url)
    return m.group(1) if m else url.split('?')[0]


def load_rtsp_addrs(epg_path):
    """从EPG提取 (name, rtsp简化地址)"""
    addrs = []
    d = json.load(open(epg_path, encoding='utf-8'))
    for ch in d:
        m = re.search(r'(rtsp://[^|"\s]+)', ch.get('url', ''))
        if m:
            addrs.append((ch.get('name', ''), simplify_rtsp(m.group(1))))
    return addrs


def scan_one(name, addr, retry, do_trace):
    r = None
    for _ in range(retry + 1):
        r = probe.probe_stream(addr, timeout=15, rtsp_transport='tcp')
        if r.get('available'):
            break
    if r.get('available') and do_trace:
        tr = probe.trace_rtsp_redirects(addr)
        r['redirect_chain'] = ' → '.join(tr['chain'])
        r['redirect_hops'] = tr['hops']
        r['redirect_loop'] = tr['loop']
    return name, addr, r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--epg', default=DEFAULT_EPG)
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--retry', type=int, default=1)
    ap.add_argument('--trace', action='store_true', help='追踪重定向链(慢)')
    ap.add_argument('--limit', type=int, default=0, help='只扫前N个(测试用)')
    args = ap.parse_args()

    addrs = load_rtsp_addrs(args.epg)
    if args.limit:
        addrs = addrs[:args.limit]
    run_id = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')

    print("=" * 55)
    print(f"  iptv-radar RTSP扫描  run={run_id}")
    print(f"  地址数:{len(addrs)} 并发:{args.workers} 追踪链:{args.trace}")
    print("=" * 55)

    conn = db_util.connect(args.db)
    c = conn.cursor()
    known = {r[0] for r in c.execute("SELECT address FROM sources WHERE source_type='rtsp'")}

    t0 = datetime.datetime.now()
    found = 0
    done = 0
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(scan_one, n, a, args.retry, args.trace): a for n, a in addrs}
        for fut in as_completed(futs):
            done += 1
            name, addr, r = fut.result()
            results.append((name, addr, r))
            if r.get('available'):
                found += 1
                extra = ''
                if args.trace and 'redirect_chain' in r:
                    loop = ' ⚠️死循环' if r.get('redirect_loop') else ''
                    extra = f" [{r['redirect_hops']}跳{loop}]"
                print(f"  [{done}/{len(addrs)}] ✅ {r.get('resolution',''):<10} "
                      f"{r.get('video_codec',''):<5} {r.get('hdr',''):<5} {name}{extra}", flush=True)
            elif done % 30 == 0:
                print(f"  [{done}/{len(addrs)}] ...", flush=True)

    # 写库
    new_cnt = 0
    for name, addr, r in results:
        avail = r.get('available', 0)
        c.execute("""INSERT INTO scan_history(scan_date,scan_run,address,available,resolution,vbitrate,status)
                     VALUES (?,?,?,?,?,?,?)""",
                  (NOW(), run_id, addr, avail, r.get('resolution', ''), 0, r.get('status', '')))
        note = ''
        if 'redirect_chain' in r:
            note = f"链:{r['redirect_chain']}" + (" [死循环]" if r.get('redirect_loop') else "")
        if addr in known:
            if avail:
                c.execute("""UPDATE sources SET available=1,resolution=?,res_label=?,video_codec=?,
                    fps=?,hdr=?,audio_codec=?,audio_channels=?,fail_count=0,last_seen=?,last_scan=?
                    WHERE address=?""",
                    (r.get('resolution', ''), r.get('res_label', ''), r.get('video_codec', ''),
                     r.get('fps', 0), r.get('hdr', ''), r.get('audio_codec', ''),
                     r.get('audio_channels', 0), NOW(), NOW(), addr))
            else:
                c.execute("UPDATE sources SET available=0,fail_count=fail_count+1,last_scan=? WHERE address=?",
                          (NOW(), addr))
        elif avail:
            c.execute("""INSERT OR IGNORE INTO sources
                (source_type,address,available,resolution,res_label,video_codec,fps,hdr,
                 audio_codec,audio_channels,fail_count,first_seen,last_seen,last_scan,notes)
                VALUES ('rtsp',?,1,?,?,?,?,?,?,?,0,?,?,?,?)""",
                (addr, r.get('resolution', ''), r.get('res_label', ''), r.get('video_codec', ''),
                 r.get('fps', 0), r.get('hdr', ''), r.get('audio_codec', ''),
                 r.get('audio_channels', 0), NOW(), NOW(), NOW(), note))
            new_cnt += 1
    conn.commit()

    elapsed = (datetime.datetime.now() - t0).total_seconds()
    print("\n" + "=" * 55)
    print(f"  完成! 耗时{elapsed:.0f}秒  可用:{found}/{len(addrs)}  新源:{new_cnt}")
    conn.close()


if __name__ == '__main__':
    main()
