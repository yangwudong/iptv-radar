#!/usr/bin/env python3
"""
iptv-radar: probe_timeshift.py — 单播源回看天数探测(仅full模式,每月一次)

回看是单播源(rtsp)专有能力: 用 timeshift_url 加 &playseek=<N天前UTC>-<结束> 拉流,
能拉到=能回看N天。二分查找每个单播源的最大可回看天数,写回 sources.playback_days。
(组播源无此属性;版权频道如CCTV5/6/8常不支持回看→0天,与myepg报告的"-"一致)

设计见 ARCHITECTURE.md。天数变化慢,只在 full 全量模式跑。

用法: python3 probe_timeshift.py [--db] [--epg channels.json] [--workers 8] [--max-days 8]
"""
import json
import datetime
import subprocess
import os
import sys
import signal
import argparse
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

RADAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
DEFAULT_DB = os.path.join(RADAR, 'data', 'iptv.db')
# 优先用 fetch_channels 刷出的(含新token)channels.json,否则退到仓库自带的脱敏样例
_CANDS = [os.path.join(RADAR, 'data', 'channels.json'),
          os.path.join(RADAR, 'reference', 'channels.json')]      # 后者=旧部署位置
DEFAULT_EPG = next((c for c in _CANDS if os.path.isfile(c)),
                   os.path.join(RADAR, 'reference', 'channels.sample.json'))


def smil_path(url):
    """从 rtsp url 提取 .smil 路径部分(去token/参数),用于和库里address匹配"""
    m = re.match(r'(rtsp://[^?]+\.smil)', url)
    return m.group(1) if m else url.split('?')[0]


def probe_playback(ts_url, days, timeout=15):
    """探测 days 天前能否回看。ts_url=完整timeshift_url(带token)。
    注: 电信 playseek 用北京本地时间(UTC+8),不是UTC(实测IINA验证:传UTC会偏8小时)。"""
    bj = datetime.timezone(datetime.timedelta(hours=8))
    now = datetime.datetime.now(bj)
    s = (now - datetime.timedelta(days=days)).strftime('%Y%m%d%H%M%S')
    e = (now - datetime.timedelta(days=days) + datetime.timedelta(minutes=2)).strftime('%Y%m%d%H%M%S')
    url = ts_url + f'&playseek={s}-{e}'
    cmd = ['ffprobe', '-v', 'error', '-rtsp_transport', 'tcp', '-rw_timeout', '10000000',
           '-show_streams', url]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        o, _ = p.communicate(timeout=timeout)
        return b'"video"' in o or b'codec_type=video' in o
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL); p.wait(3)
        except Exception:
            pass
        return False
    except Exception:
        return False


def find_days(smil, ts_url, max_days):
    """二分查找最大可回看天数。返回(smil路径, days)。0=不支持,N=N天"""
    if not probe_playback(ts_url, 1):
        return smil, 0
    lo, hi, best = 1, max_days, 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if probe_playback(ts_url, mid):
            best = mid; lo = mid + 1
        else:
            hi = mid - 1
    return smil, best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--epg', default=DEFAULT_EPG)
    ap.add_argument('--workers', type=int, default=8, help='并发(实测8并发CDN扛得住)')
    ap.add_argument('--max-days', type=int, default=8, help='探测上限(电信一般≤7天)')
    args = ap.parse_args()

    print("=" * 55)
    print("  iptv-radar 单播回看天数探测")
    print("=" * 55)

    # 从 channels.json 收集 timeshift_url(完整,带token)
    epg = json.load(open(args.epg, encoding='utf-8'))
    targets = []   # (smil路径, 完整timeshift_url)
    for ch in epg:
        ts = ch.get('timeshift_url', '')
        if ts:
            targets.append((smil_path(ts), ts))
    print(f"  待探测单播源(有timeshift_url): {len(targets)}")

    import time
    t0 = time.time()
    results = {}   # smil → days
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(find_days, smil, ts, args.max_days): smil for smil, ts in targets}
        done = 0
        for f in as_completed(futs):
            smil, days = f.result()
            results[smil] = days
            done += 1
            if done % 20 == 0:
                print(f"  [{done}/{len(targets)}] ...", flush=True)

    # 写回 sources.playback_days(按address匹配smil路径)
    import sqlite3
    conn = db_util.connect(args.db)
    c = conn.cursor()
    # 自愈: 确保 playback_days 字段存在(已有旧库可能没有,新库db_schema已含)
    cols = [r[1] for r in c.execute("PRAGMA table_info(sources)").fetchall()]
    if 'playback_days' not in cols:
        c.execute("ALTER TABLE sources ADD COLUMN playback_days INTEGER")
        print("  + sources.playback_days 字段(自愈)")
    updated = 0
    for smil, days in results.items():
        c.execute("UPDATE sources SET playback_days=? WHERE address=? AND source_type='rtsp'",
                  (days, smil))
        updated += c.rowcount
    conn.commit()

    # 统计
    from collections import Counter
    dist = Counter(results.values())
    conn.close()
    print(f"\n  探测完成: {len(results)}个源, 耗时{time.time()-t0:.0f}秒")
    print(f"  写回库(匹配sources): {updated}个")
    print(f"  天数分布: {dict(sorted(dist.items()))}")
    print("完成")


if __name__ == '__main__':
    main()
