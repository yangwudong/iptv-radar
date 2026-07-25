#!/usr/bin/env python3
"""
iptv-radar: orphan_import.py — 消费"识别结果"(孤儿源识别流程第③步)

读取 Electron App 导出的 resolved.json,把孤儿源写库归并。
设计见 docs/design/ORPHAN_REVIEW.md §3.2/§3.3。

消费: data/orphan_inbox/*.json (App放入的识别结果)
  action: assign(归已有频道) / new(建新频道+归并) / junk / unknown / skip
  → 写 sources.channel_id + channel_key 冗余 + 更新 source_links.json 快照
  → new: 额外建 channels 记录(归所属分组末尾) + channel_groups
  → 处理完的json归档到 data/orphan_inbox/done/

用法: python3 orphan_import.py [--db] [--inbox DIR] [--dry-run]
  由 run_pipeline.sh 在扫描前自动调用(消费上次App的识别结果)。
"""
import sqlite3
import db_util
import os
import sys
import json
import glob
import shutil
import argparse
import datetime

RADAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
DEFAULT_DB = os.path.join(RADAR, 'data', 'iptv.db')
INBOX = os.path.join(RADAR, 'data', 'orphan_inbox')
NOW = lambda: datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def snapshot_path_for(db_path):
    """快照与db同目录(测试用临时db时不污染正式快照)"""
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), 'source_links.json')


def apply_decision(c, d, snapshot):
    """处理一条决定,返回(动作描述, 是否改动snapshot)。c=cursor"""
    addr = d.get('address')
    action = d.get('action')
    if not addr or not action:
        return f"跳过(缺address/action): {d}", False

    src = c.execute("SELECT source_id, channel_id FROM sources WHERE address=?", (addr,)).fetchone()
    if not src:
        return f"⚠️ 源不存在库中,跳过: {addr}", False

    if action == 'skip':
        return f"skip: {addr}", False

    if action in ('junk', 'unknown'):
        key = '__JUNK__' if action == 'junk' else '__UNKNOWN__'
        ch = c.execute("SELECT channel_id FROM channels WHERE channel_key=?", (key,)).fetchone()
        if not ch:
            return f"⚠️ 占位频道{key}不存在,跳过: {addr}", False
        c.execute("UPDATE sources SET channel_id=?, channel_key=? WHERE address=?",
                  (ch['channel_id'], key, addr))
        # 占位归并也写快照: 这同样是人工识别成果(17个垃圾流认一次就该永久生效)。
        # 曾经这里返回 False 不写,靠 link_sources 事后从库全量重建快照才补上 ——
        # 即"能不能持久化"取决于另一个脚本的副作用和执行顺序,而不是本脚本自己的保证。
        snapshot[addr] = {'channel_id': ch['channel_id'], 'channel_key': key}
        return f"{action}: {addr} → {key}", True

    if action == 'assign':
        key = d.get('channel_key')
        ch = c.execute("SELECT channel_id FROM channels WHERE channel_key=?", (key,)).fetchone()
        if not ch:
            return f"⚠️ 频道{key}不存在,跳过: {addr}", False
        c.execute("UPDATE sources SET channel_id=?, channel_key=? WHERE address=?",
                  (ch['channel_id'], key, addr))
        # 快照按 channel_id 存(频道改名不断关联),channel_key 只是可读注释
        snapshot[addr] = {'channel_id': ch['channel_id'], 'channel_key': key}
        return f"assign: {addr} → {key}", True

    if action == 'new':
        key = d.get('channel_key')
        group = d.get('group', '其他')
        if not key:
            return f"⚠️ new缺channel_key,跳过: {addr}", False
        # 已存在同名频道? 直接assign(不建重复频道)。
        # 一台多源时用户会对每条都填同名,只有第一条真建 —— 文案必须说清楚是哪种,
        # 否则看日志像是建了N个重名频道(实际用户被误导过)。
        ch = c.execute("SELECT channel_id FROM channels WHERE channel_key=?", (key,)).fetchone()
        reused = bool(ch)
        if ch:
            cid = ch['channel_id']
        else:
            # 建新频道: 归所属分组末尾(order=组内max+1)
            # 分组只写 channel_groups(下面几行),channels 表不再有分组列
            c.execute("""INSERT INTO channels(channel_key,name,enabled,status,first_seen,last_seen)
                         VALUES(?,?,1,'active',?,?)""", (key, key, NOW(), NOW()))
            cid = c.lastrowid
            maxord = c.execute("SELECT COALESCE(MAX(order_in_group),0) FROM channel_groups WHERE group_name=?",
                               (group,)).fetchone()[0]
            c.execute("""INSERT INTO channel_groups(channel_id,group_name,is_primary,order_in_group)
                         VALUES(?,?,1,?)""", (cid, group, maxord + 1))
        c.execute("UPDATE sources SET channel_id=?, channel_key=? WHERE address=?", (cid, key, addr))
        snapshot[addr] = {'channel_id': cid, 'channel_key': key}
        if reused:
            return f"new→归并: {addr} → 已有频道[{key}](id={cid},未建重复频道)", True
        return f"new: {addr} → 新频道[{key}]({group}) id={cid}", True

    return f"⚠️ 未知action '{action}': {addr}", False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--inbox', default=INBOX)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.inbox, '*.json')))
    if not files:
        print(f"  无待消费的识别结果 ({args.inbox}/*.json),跳过。")
        return

    print("=" * 55)
    print(f"  iptv-radar 孤儿源识别结果消费  ({len(files)}个文件)")
    print("=" * 55)

    conn = db_util.connect(args.db)
    c = conn.cursor()
    snap_path = snapshot_path_for(args.db)
    snapshot = json.load(open(snap_path, encoding='utf-8')) if os.path.exists(snap_path) else {}
    snap_changed = False
    stats = {'assign': 0, 'new': 0, 'junk': 0, 'unknown': 0, 'skip': 0, 'error': 0}

    for fp in files:
        try:
            data = json.load(open(fp, encoding='utf-8'))
        except Exception as e:
            print(f"  ⚠️ 解析失败 {os.path.basename(fp)}: {e}")
            continue
        print(f"\n  处理 {os.path.basename(fp)} ({len(data.get('decisions',[]))}条决定):")
        for d in data.get('decisions', []):
            desc, changed = apply_decision(c, d, snapshot)
            print(f"    {desc}")
            act = d.get('action', 'error')
            stats[act if act in stats else 'error'] += 1
            snap_changed = snap_changed or changed

    if args.dry_run:
        print("\n[dry-run] 回滚,不写库/快照。")
        conn.rollback()
    else:
        conn.commit()
        # 更新快照(持久化归并)
        if snap_changed:
            json.dump(snapshot, open(snap_path, 'w', encoding='utf-8'),
                      ensure_ascii=False, indent=1, sort_keys=True)
            print(f"\n  已更新快照 {os.path.basename(snap_path)}")
        # 归档处理完的json
        done_dir = os.path.join(args.inbox, 'done')
        os.makedirs(done_dir, exist_ok=True)
        for fp in files:
            shutil.move(fp, os.path.join(done_dir, os.path.basename(fp)))
        print(f"  已归档 {len(files)} 个结果文件 → {done_dir}")
    conn.close()

    print(f"\n  统计: {stats}")
    print("完成" + (" (dry-run)" if args.dry_run else ""))


if __name__ == '__main__':
    main()
