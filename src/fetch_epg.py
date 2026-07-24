#!/usr/bin/env python3
"""
iptv-radar: fetch_epg.py
下载第三方EPG源(与APTV同源),解析当天节目单,存成精简JSON供Dashboard用。
不依赖电信官方EPG(那个需模拟STB浏览器,拿不到)。

EPG源: epg.112114.xyz(默认,含555频道节目单) 等
运行: python3 fetch_epg.py [--url URL] [--out output/epg_today.json] [--days 2]
"""
import urllib.request
import re
import json
import os
import argparse
import datetime
import gzip
import io

RADAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
DEFAULT_URL = 'https://epg.112114.xyz/pp.xml'
DEFAULT_OUT = os.path.join(RADAR, 'output', 'epg_today.json')


def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=40) as r:
        data = r.read()
    if data[:2] == b'\x1f\x8b':  # gzip
        data = gzip.GzipFile(fileobj=io.BytesIO(data)).read()
    return data.decode('utf-8', 'replace')


def parse_epg(xml, days=2):
    """解析出 {channel_id: [{time, title}, ...]},只保留今天起days天内"""
    today = datetime.datetime.now()
    valid_dates = {(today + datetime.timedelta(d)).strftime('%Y%m%d') for d in range(days)}
    epg = {}
    # <programme channel="X" start="20260723000700 +0800" stop="...">...<title>T</title>
    pat = re.compile(
        r'<programme channel="([^"]+)" start="(\d{14})[^"]*"[^>]*>\s*<title[^>]*>([^<]+)</title>',
        re.S)
    for m in pat.finditer(xml):
        cid, start, title = m.group(1), m.group(2), m.group(3).strip()
        if start[:8] not in valid_dates:
            continue
        t = f"{start[8:10]}:{start[10:12]}"  # HH:MM
        epg.setdefault(cid, []).append({'d': start[:8], 't': t, 'n': title})
    return epg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', default=DEFAULT_URL)
    ap.add_argument('--out', default=DEFAULT_OUT)
    ap.add_argument('--days', type=int, default=2)
    args = ap.parse_args()
    print("=" * 50)
    print("  iptv-radar 抓取EPG节目单")
    print("=" * 50)
    print(f"  源: {args.url}")
    xml = fetch(args.url)
    print(f"  下载: {len(xml)} 字符")
    epg = parse_epg(xml, args.days)
    total = sum(len(v) for v in epg.values())
    print(f"  解析: {len(epg)} 频道, {total} 条节目({args.days}天内)")
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(epg, f, ensure_ascii=False, separators=(',', ':'))
    print(f"  输出: {args.out} ({os.path.getsize(args.out)} 字节)")
    print("完成")


if __name__ == '__main__':
    main()
