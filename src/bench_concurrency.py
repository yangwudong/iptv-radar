#!/usr/bin/env python3
"""并发压测: 测不同并发下组播探测的成功率/耗时。一次性实验脚本。
从库动态取N个已知可用组播源,更真实反映全扫压力。"""
import sys, os, time, argparse, sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe

RADAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
DB = os.path.join(RADAR, 'data', 'iptv.db')

def load_sources(n):
    c = sqlite3.connect(DB, timeout=30)
    rows = c.execute("""SELECT s.address, COALESCE(s.res_label,'?'), ch.name
        FROM sources s JOIN channels ch ON s.channel_id=ch.channel_id
        WHERE s.source_type='multicast' AND s.available=1
        ORDER BY RANDOM() LIMIT ?""", (n,)).fetchall()
    c.close()
    return rows

def probe_one(msd, addr, label, name, timeout):
    url = f"http://{msd}/rtp/{addr}"
    t0 = time.time()
    r = probe.probe_stream(url, timeout=timeout)
    return (addr, label, name, r.get('available', 0), time.time()-t0, r.get('status',''))

def run(msd, sources, workers, timeout):
    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(probe_one, msd, a, l, n, timeout) for a,l,n in sources]
        for f in as_completed(futs):
            results.append(f.result())
    total = time.time() - t0
    ok = sum(1 for r in results if r[3])
    fails = [(r[2], r[5]) for r in results if not r[3]]
    return ok, len(results), total, fails

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--msd', required=True, help='msd_lite 地址 HOST:PORT')
    ap.add_argument('--sources', type=int, default=30)
    ap.add_argument('--workers', type=int, default=0, help='0=扫一组6,7,8')
    ap.add_argument('--timeout', type=int, default=12)
    ap.add_argument('--rounds', type=int, default=2)
    args = ap.parse_args()
    srcs = load_sources(args.sources)
    print(f"取 {len(srcs)} 个已知可用组播源做压测\n")
    levels = [args.workers] if args.workers else [6, 7, 8]
    for w in levels:
        for rd in range(1, args.rounds+1):
            ok, tot, dt, fails = run(args.msd, srcs, w, args.timeout)
            fstr = '' if not fails else '  误报:' + ','.join(f'{n}({s})' for n,s in fails[:4])
            print(f"  {w}并发 第{rd}轮: {ok}/{tot} 成功, {dt:.1f}s{fstr}")
            time.sleep(3)
