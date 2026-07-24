#!/usr/bin/env python3
"""
migrate_v2.py — 数据库 schema 重构 V2 (一次性迁移,跑完即弃)

目标(详见 docs/design/CHANNEL_KEY_DESIGN.md V2):
  1. channels: channel_key 挪到 channel_id 右边 + 加 UNIQUE; 删 primary_source_id
  2. sources:  channel_key 挪到 source_id 右边(降为可读冗余); channel_id 为关联主键
  3. 回填 sources.channel_id: 308个"只有channel_key无channel_id"的源, 按 channel_key→channel_id 补齐
  4. 新建 channel_preferred_sources 表, 从旧 channels.primary_source_id 迁优选(rank=1)
  5. 建占位频道 __UNKNOWN__ / __JUNK__ (status=placeholder)

安全:
  - 跑前自动备份 data/iptv.db → data/iptv.db.bak.migrate_v2
  - 全程一个事务, 出错 rollback
  - channel_id 值显式保留(不重新分配), 保证 channel_groups/sources 关联不断
  - 幂等: 检测到已迁移(sources无channel_key列/已有preferred表)则跳过
"""
import sqlite3, shutil, sys, os

DB = os.path.join(os.path.dirname(__file__), '..', 'data', 'iptv.db')


def already_migrated(c):
    """检测是否已迁移过(幂等)"""
    src_cols = [r[1] for r in c.execute("PRAGMA table_info(sources)").fetchall()]
    ch_cols = [r[1] for r in c.execute("PRAGMA table_info(channels)").fetchall()]
    has_pref = c.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='channel_preferred_sources'"
    ).fetchone()[0] > 0
    # 迁移完成标志: channels 无 primary_source_id + 有 preferred 表
    return ('primary_source_id' not in ch_cols) and has_pref


def main(dry_run=False):
    if not os.path.exists(DB):
        print(f"数据库不存在: {DB}"); sys.exit(1)

    # === 备份 ===
    bak = DB + '.bak.migrate_v2'
    shutil.copy(DB, bak)
    print(f"已备份 → {bak}")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("PRAGMA foreign_keys=OFF")  # 重建表期间关外键检查

    if already_migrated(c):
        print("检测到已迁移(channels无primary_source_id且有preferred表),跳过。")
        conn.close(); return

    try:
        # ============ 0. 迁移前快照(用于事后核对) ============
        before = {
            'channels': c.execute("SELECT COUNT(*) FROM channels").fetchone()[0],
            'sources': c.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
            'src_linked_key': c.execute("SELECT COUNT(*) FROM sources WHERE channel_key IS NOT NULL").fetchone()[0],
            'src_linked_id': c.execute("SELECT COUNT(*) FROM sources WHERE channel_id IS NOT NULL").fetchone()[0],
            'preferred': c.execute("SELECT COUNT(*) FROM channels WHERE primary_source_id IS NOT NULL").fetchone()[0],
        }
        print(f"\n迁移前: 频道{before['channels']} 源{before['sources']} "
              f"归并(key){before['src_linked_key']} 归并(id){before['src_linked_id']} "
              f"优选{before['preferred']}")

        # ============ 1. 回填 sources.channel_id (对齐分叉数据) ============
        # 308个源: 有channel_key但channel_id=NULL → 按 channel_key 查 channels.channel_id 补上
        key2id = {r['channel_key']: r['channel_id']
                  for r in c.execute("SELECT channel_id, channel_key FROM channels WHERE channel_key IS NOT NULL")}
        backfill = []
        for r in c.execute("SELECT source_id, channel_key FROM sources WHERE channel_key IS NOT NULL AND channel_id IS NULL"):
            cid = key2id.get(r['channel_key'])
            if cid:
                backfill.append((cid, r['source_id']))
        c.executemany("UPDATE sources SET channel_id=? WHERE source_id=?", backfill)
        print(f"  [1] 回填 sources.channel_id: {len(backfill)} 个源")
        # 回填后仍有channel_key但查不到channel_id的(悬空key)? 报警
        orphan_key = c.execute(
            "SELECT COUNT(*) FROM sources WHERE channel_key IS NOT NULL AND channel_id IS NULL").fetchone()[0]
        if orphan_key:
            print(f"      ⚠ 仍有 {orphan_key} 个源 channel_key 查不到对应频道(悬空key),将清空其channel_key")
            c.execute("UPDATE sources SET channel_key=NULL WHERE channel_key IS NOT NULL AND channel_id IS NULL")

        # ============ 2. 新建 channel_preferred_sources + 迁优选 ============
        c.execute("""CREATE TABLE IF NOT EXISTS channel_preferred_sources (
            channel_id INTEGER NOT NULL,
            source_id  INTEGER NOT NULL,
            rank       INTEGER NOT NULL DEFAULT 1,   -- 1=最佳,2=备选...(未来多源按画质)
            PRIMARY KEY(channel_id, rank),
            FOREIGN KEY(channel_id) REFERENCES channels(channel_id),
            FOREIGN KEY(source_id)  REFERENCES sources(source_id)
        )""")
        pref = [(r['channel_id'], r['primary_source_id'])
                for r in c.execute("SELECT channel_id, primary_source_id FROM channels WHERE primary_source_id IS NOT NULL")]
        c.executemany("INSERT OR REPLACE INTO channel_preferred_sources(channel_id,source_id,rank) VALUES(?,?,1)", pref)
        print(f"  [2] channel_preferred_sources 建表 + 迁入优选 {len(pref)} 条(rank=1)")

        # ============ 3. 重建 channels (挪列+UNIQUE+删primary_source_id) ============
        # 保留原 channel_id 值! 新表列顺序: channel_id, channel_key, name, ...
        c.execute("""CREATE TABLE channels_new (
            channel_id    INTEGER PRIMARY KEY,          -- 保留原值,不AUTOINCREMENT重排
            channel_key   TEXT UNIQUE,                  -- 规范名(人读入口),紧挨channel_id右边
            name          TEXT NOT NULL,
            tvg_id        TEXT,
            tvg_logo      TEXT,
            group_primary TEXT,
            group_extra   TEXT,
            enabled       INTEGER DEFAULT 1,
            timeshift     INTEGER DEFAULT 0,
            sort_hint     INTEGER,
            status        TEXT DEFAULT 'active',        -- active/offline/placeholder/new
            notes         TEXT,
            first_seen    TEXT,
            last_seen     TEXT,
            epg_channel_id TEXT
        )""")
        c.execute("""INSERT INTO channels_new
            (channel_id, channel_key, name, tvg_id, tvg_logo, group_primary, group_extra,
             enabled, timeshift, sort_hint, status, notes, first_seen, last_seen, epg_channel_id)
            SELECT channel_id, channel_key, name, tvg_id, tvg_logo, group_primary, group_extra,
             enabled, timeshift, sort_hint, status, notes, first_seen, last_seen, epg_channel_id
            FROM channels""")
        c.execute("DROP TABLE channels")
        c.execute("ALTER TABLE channels_new RENAME TO channels")
        c.execute("CREATE INDEX idx_channels_group ON channels(group_primary)")
        c.execute("CREATE INDEX idx_channels_key ON channels(channel_key)")
        print(f"  [3] channels 重建: channel_key挪到channel_id右边+UNIQUE, 删除primary_source_id")

        # ============ 4. 重建 sources (channel_key挪到source_id右边,降为可读冗余) ============
        c.execute("""CREATE TABLE sources_new (
            source_id      INTEGER PRIMARY KEY,          -- 保留原值
            channel_id     INTEGER,                      -- 关联键(稳定),NULL=待识别孤儿
            channel_key    TEXT,                         -- 可读冗余快照(link_sources回写),紧挨右边
            source_type    TEXT,
            address        TEXT NOT NULL UNIQUE,
            available      INTEGER DEFAULT 0,
            resolution     TEXT,
            res_label      TEXT,
            video_codec    TEXT,
            fps            REAL,
            vbitrate       INTEGER DEFAULT 0,
            hdr            TEXT,
            audio_codec    TEXT,
            audio_channels INTEGER,
            quality_score  REAL DEFAULT 0,
            screenshots    TEXT,
            fail_count     INTEGER DEFAULT 0,
            redirect_chain TEXT,
            redirect_hops  INTEGER DEFAULT 0,
            redirect_loop  INTEGER DEFAULT 0,
            notes          TEXT,
            first_seen     TEXT,
            last_seen      TEXT,
            last_scan      TEXT,
            FOREIGN KEY(channel_id) REFERENCES channels(channel_id)
        )""")
        c.execute("""INSERT INTO sources_new
            (source_id, channel_id, channel_key, source_type, address, available, resolution,
             res_label, video_codec, fps, vbitrate, hdr, audio_codec, audio_channels, quality_score,
             screenshots, fail_count, redirect_chain, redirect_hops, redirect_loop, notes,
             first_seen, last_seen, last_scan)
            SELECT source_id, channel_id, channel_key, source_type, address, available, resolution,
             res_label, video_codec, fps, vbitrate, hdr, audio_codec, audio_channels, quality_score,
             screenshots, fail_count, redirect_chain, redirect_hops, redirect_loop, notes,
             first_seen, last_seen, last_scan
            FROM sources""")
        c.execute("DROP TABLE sources")
        c.execute("ALTER TABLE sources_new RENAME TO sources")
        c.execute("CREATE INDEX idx_sources_channel ON sources(channel_id)")
        c.execute("CREATE INDEX idx_sources_type ON sources(source_type)")
        c.execute("CREATE INDEX idx_sources_avail ON sources(available)")
        print(f"  [4] sources 重建: channel_key挪到source_id右边(降为可读冗余), channel_id为关联键")

        # ============ 5. 建占位频道 __UNKNOWN__ / __JUNK__ ============
        # 取现有最大 channel_id 之后的值, 避免和真实频道冲突
        maxid = c.execute("SELECT MAX(channel_id) FROM channels").fetchone()[0] or 0
        placeholders = [
            (maxid + 1, '__UNKNOWN__', '未知待查', 'placeholder'),
            (maxid + 2, '__JUNK__',    '垃圾/测试流', 'placeholder'),
        ]
        for cid, key, name, st in placeholders:
            c.execute("""INSERT OR IGNORE INTO channels(channel_id,channel_key,name,enabled,status)
                         VALUES(?,?,?,0,?)""", (cid, key, name, st))
        print(f"  [5] 占位频道: __UNKNOWN__(id={maxid+1}) __JUNK__(id={maxid+2}), enabled=0")

        # ============ 核对 ============
        after = {
            'channels': c.execute("SELECT COUNT(*) FROM channels").fetchone()[0],
            'sources': c.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
            'src_linked_id': c.execute("SELECT COUNT(*) FROM sources WHERE channel_id IS NOT NULL").fetchone()[0],
            'preferred': c.execute("SELECT COUNT(*) FROM channel_preferred_sources").fetchone()[0],
            'dangling': c.execute("""SELECT COUNT(*) FROM sources s LEFT JOIN channels ch
                        ON s.channel_id=ch.channel_id WHERE s.channel_id IS NOT NULL AND ch.channel_id IS NULL""").fetchone()[0],
            'key_id_mismatch': c.execute("""SELECT COUNT(*) FROM sources s JOIN channels ch
                        ON s.channel_id=ch.channel_id WHERE s.channel_key IS NOT NULL
                        AND s.channel_key != ch.channel_key""").fetchone()[0],
        }
        print(f"\n迁移后核对:")
        print(f"  频道: {before['channels']} → {after['channels']} (+2占位)")
        print(f"  源: {after['sources']} (不变应={before['sources']})")
        print(f"  源已归并(channel_id): {before['src_linked_id']} → {after['src_linked_id']} (应≈449,对齐了308分叉)")
        print(f"  优选表: {after['preferred']} 条 (应={before['preferred']})")
        print(f"  悬空channel_id: {after['dangling']} (应0)")
        print(f"  channel_key与channel_id指向不一致: {after['key_id_mismatch']} (应0)")

        # 断言防线
        assert after['sources'] == before['sources'], "源数量变了!"
        assert after['dangling'] == 0, "有悬空channel_id!"
        assert after['key_id_mismatch'] == 0, "channel_key/channel_id指向不一致!"
        assert after['preferred'] == before['preferred'], "优选数量对不上!"

        if dry_run:
            print("\n[dry-run] 回滚,不写入。")
            conn.rollback()
        else:
            conn.commit()
            print("\n✅ 迁移提交成功。")
    except Exception as e:
        conn.rollback()
        print(f"\n❌ 出错已回滚: {e}")
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    main(dry_run='--dry-run' in sys.argv)
