#!/usr/bin/env python3
"""
iptv-radar: orphan_export.py — 产出"待识别包"(孤儿源识别流程第①步)

发现的孤儿源(channel_id=NULL)导出为待识别包,供 Electron App 人工识别。
设计见 docs/design/ORPHAN_REVIEW.md §3.1。

产出: output/orphan_review/
  ├── orphans.json   孤儿源清单 + 可归属频道清单 + 占位频道(契约§3.1)
  └── shots/         每个孤儿源截图(组播才截,单播复用已有截图)

用法: python3 orphan_export.py [--db] [--msd HOST:PORT] [--shots/--no-shots] [--limit N]
  --no-shots  不重新截图(快,用库里已有的screenshots)
"""
import sqlite3
import os
import sys
import json
import argparse
import datetime
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe

RADAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
DEFAULT_DB = os.path.join(RADAR, 'data', 'iptv.db')
REVIEW_DIR = os.path.join(RADAR, 'output', 'orphan_review')
SHOTS_DIR = os.path.join(REVIEW_DIR, 'shots')


def play_url(source_type, address, msd):
    """孤儿源播放URL: 组播用msd前缀拼http, 单播rtsp原样"""
    if source_type == 'multicast':
        return f"http://{msd}/rtp/{address}"
    return address  # rtsp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--msd', '--udpxy', dest='msd', default='127.0.0.1:4088',
                    help='msd_lite地址(组播播放URL前缀用)')
    ap.add_argument('--no-shots', action='store_true', help='不重新截图(用库里已有的)')
    ap.add_argument('--limit', type=int, default=0, help='只导出前N个(测试用)')
    args = ap.parse_args()

    print("=" * 55)
    print("  iptv-radar 孤儿源导出(待识别包)")
    print("=" * 55)

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # 孤儿源: channel_id=NULL 且可用(不导失效的,避免噪音)
    q = """SELECT address, source_type, res_label, video_codec, fps, hdr,
                  audio_codec, screenshots
           FROM sources WHERE channel_id IS NULL AND available=1
           ORDER BY source_type, address"""
    orphan_rows = c.execute(q).fetchall()
    if args.limit:
        orphan_rows = orphan_rows[:args.limit]

    if not orphan_rows:
        print("  无待识别孤儿源,不生成包。")
        conn.close()
        return

    # 可归属频道清单(供App下拉/tag匹配)
    channels = [{'channel_key': r['channel_key'], 'name': r['name'], 'group': r['group_primary']}
                for r in c.execute("""SELECT channel_key, name, group_primary FROM channels
                                      WHERE status!='placeholder' AND channel_key IS NOT NULL
                                      ORDER BY sort_hint""")]
    placeholders = [{'channel_key': r['channel_key'], 'name': r['name']}
                    for r in c.execute("SELECT channel_key, name FROM channels WHERE status='placeholder'")]
    conn.close()

    os.makedirs(SHOTS_DIR, exist_ok=True)

    orphans = []
    for r in orphan_rows:
        addr = r['address']
        stype = r['source_type']
        purl = play_url(stype, addr, args.msd)
        iina = 'iina://weblink?url=' + urllib.parse.quote(purl, safe='')

        # 截图: 组播重新截(除非--no-shots); 单播/已有的复用库里screenshots
        shots = []
        if r['screenshots']:
            shots = [os.path.basename(s) for s in r['screenshots'].split(';')]
        if stype == 'multicast' and not args.no_shots:
            prefix = addr.split(':')[0].replace('.', '_')
            paths = probe.capture_screenshots(purl, SHOTS_DIR, prefix, count=3)
            if paths:
                shots = [os.path.basename(p) for p in paths]
                print(f"    {addr}: {len(shots)}张截图")

        orphans.append({
            'address': addr, 'source_type': stype,
            'res_label': r['res_label'] or '', 'video_codec': r['video_codec'] or '',
            'fps': r['fps'] or 0, 'hdr': r['hdr'] or '', 'audio_codec': r['audio_codec'] or '',
            'play_url': purl, 'iina_url': iina,
            'shots': [f"shots/{s}" for s in shots],
        })

    pkg = {
        'generated_at': datetime.datetime.now().isoformat(timespec='seconds'),
        'msd_prefix': f"http://{args.msd}/rtp/",
        'channels': channels,
        'placeholders': placeholders,
        'orphans': orphans,
    }
    out_json = os.path.join(REVIEW_DIR, 'orphans.json')
    json.dump(pkg, open(out_json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    print(f"\n  待识别孤儿源: {len(orphans)} 个 (组播{sum(1 for o in orphans if o['source_type']=='multicast')} "
          f"/ 单播{sum(1 for o in orphans if o['source_type']=='rtsp')})")
    print(f"  可归属频道: {len(channels)}  占位: {len(placeholders)}")
    print(f"  产出: {out_json}")
    print("完成")


if __name__ == '__main__':
    main()
