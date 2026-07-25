#!/usr/bin/env python3
"""
iptv-radar: seed.py — 频道种子数据 导出/载入

channels(频道台账:规范名/分组/台标/tvg-id/顺序) 是人工养出来的知识资产,
只存在于 db 里、扫描重建不了。本脚本把它导出为可分享/可复现的种子文件,
并支持从种子重建库。配合 source_links.json(归并快照) 实现"从0重建整库"。

种子文件: data/channels_seed.json
  - channels: 频道台账(含占位频道,保留channel_id保证关联一致)
  - channel_groups: 分组保序

用法:
  python3 seed.py export              # 从当前库导出种子
  python3 seed.py load [--db PATH]    # 从种子载入(建channels+channel_groups)
    注: load 只建频道台账,不含sources(靠scan填)/优选(靠etl产);
        完整重建: db_schema.py建表 → seed.py load → scan → link_sources → etl → gen

设计见 docs/design/CHANNEL_KEY_DESIGN.md
"""
import sqlite3
import os
import sys
import json
import argparse

RADAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
DEFAULT_DB = os.path.join(RADAR, 'data', 'iptv.db')
SEED_PATH = os.path.join(RADAR, 'data', 'channels_seed.json')

# 导出的频道字段(知识资产;跳过 first_seen/last_seen 等运行时时间戳)
CH_FIELDS = ['channel_id', 'channel_key', 'name', 'tvg_id', 'tvg_logo',
             'enabled', 'timeshift',
             'sort_hint', 'status', 'epg_channel_id']
GRP_FIELDS = ['channel_id', 'group_name', 'is_primary', 'order_in_group']


def export_seed(db_path, out_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    channels = [{k: r[k] for k in CH_FIELDS}
                for r in c.execute(f"SELECT {','.join(CH_FIELDS)} FROM channels ORDER BY channel_id")]
    groups = [{k: r[k] for k in GRP_FIELDS}
              for r in c.execute(f"SELECT {','.join(GRP_FIELDS)} FROM channel_groups ORDER BY channel_id, group_name")]
    conn.close()
    seed = {
        '_comment': 'iptv-radar 频道种子数据(频道台账+分组保序)。配合 source_links.json 可从0重建库。',
        'channels': channels,
        'channel_groups': groups,
    }
    json.dump(seed, open(out_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f"  导出种子 → {out_path}")
    print(f"  频道: {len(channels)} (含占位)  分组关系: {len(groups)}")


def load_seed(db_path, seed_path):
    if not os.path.exists(seed_path):
        print(f"  种子文件不存在: {seed_path}"); sys.exit(1)
    seed = json.load(open(seed_path, encoding='utf-8'))
    conn = sqlite3.connect(db_path, timeout=30)
    c = conn.cursor()
    # 建表(若未建)——复用 db_schema
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import db_schema
    conn.executescript(db_schema.SCHEMA)
    conn.commit()

    ch_cnt = 0
    for ch in seed['channels']:
        cols = list(ch.keys())
        ph = ','.join('?' * len(cols))
        c.execute(f"INSERT OR REPLACE INTO channels({','.join(cols)}) VALUES({ph})",
                  [ch[k] for k in cols])
        ch_cnt += 1
    grp_cnt = 0
    for g in seed['channel_groups']:
        cols = list(g.keys())
        ph = ','.join('?' * len(cols))
        c.execute(f"INSERT OR REPLACE INTO channel_groups({','.join(cols)}) VALUES({ph})",
                  [g[k] for k in cols])
        grp_cnt += 1
    conn.commit()
    conn.close()
    print(f"  载入种子 ← {seed_path}")
    print(f"  频道: {ch_cnt} (含占位)  分组关系: {grp_cnt}")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=['export', 'load'])
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--seed', default=SEED_PATH)
    args = ap.parse_args()
    print("=" * 50)
    print(f"  iptv-radar seed {args.action}")
    print("=" * 50)
    if args.action == 'export':
        export_seed(args.db, args.seed)
    else:
        load_seed(args.db, args.seed)
    print("完成")
