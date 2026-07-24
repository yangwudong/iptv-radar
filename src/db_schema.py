#!/usr/bin/env python3
"""
iptv-radar 数据库 schema (v2)
建立 SQLite 主数据源: channels(频道级) + sources(源级) + scan_history(扫描历史)

设计见 REFACTOR_DESIGN.md 三、数据库设计
运行: python3 db_schema.py [--db PATH] [--reset]
"""
import sqlite3
import os
import sys
import argparse

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'iptv.db')

SCHEMA = """
-- ============ channels 频道级(频道元数据台账,只增不删,不含优选源) ============
-- 主键用稳定代理键 channel_id;channel_key(规范名)加UNIQUE作人读入口。设计见 CHANNEL_KEY_DESIGN.md V2
CREATE TABLE IF NOT EXISTS channels (
    channel_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_key       TEXT UNIQUE,         -- 规范名(人读/沟通/台标匹配入口),紧挨主键
    name              TEXT NOT NULL,       -- 展示名(如 CCTV1综合)
    tvg_id            TEXT,                -- EPG匹配id(如 CCTV1)
    tvg_logo          TEXT,                -- 台标URL
    group_primary     TEXT,               -- 主分组(如 央视)
    group_extra       TEXT,               -- 附加分组,分号分隔(如 北京;少儿)
    enabled           INTEGER DEFAULT 1,   -- 是否输出到m3u(0=黑名单/禁用/占位)
    timeshift         INTEGER DEFAULT 0,   -- 是否支持时移
    sort_hint         INTEGER,             -- 组内排序提示(可选)
    status            TEXT DEFAULT 'active', -- active/new(待识别)/offline(疑似下线)/placeholder(占位)
    notes             TEXT,
    first_seen        TEXT,
    last_seen         TEXT,
    epg_channel_id    TEXT
);

-- ============ sources 源级(每个可播地址一行,进表≠已识别) ============
CREATE TABLE IF NOT EXISTS sources (
    source_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id     INTEGER,              -- 关联键(→channels; NULL=未归并孤儿源),稳定
    channel_key    TEXT,                 -- 可读冗余快照(link_sources回写;=所属频道规范名),紧挨主键
    source_type    TEXT,                 -- multicast / rtsp
    address        TEXT NOT NULL,        -- 组播:233.50.201.118:5140  单播:rtsp://.../xxx.smil
    -- 采集层写入的技术属性
    available      INTEGER DEFAULT 0,    -- 最近一次扫描是否可用
    resolution     TEXT,                 -- 1920x1080
    res_label      TEXT,                 -- 4K/1080P/720P/SD
    video_codec    TEXT,                 -- h264/hevc
    fps            REAL,
    vbitrate       INTEGER DEFAULT 0,    -- 实测视频码率(bps)
    hdr            TEXT,                 -- SDR/HLG/HDR10
    audio_codec    TEXT,                 -- mp2/aac/eac3
    audio_channels INTEGER,
    -- ETL写入
    quality_score  REAL DEFAULT 0,       -- 优选评分
    -- 单播回看(仅rtsp源;NULL=未探测/非单播, 0=不支持回看, N=可回看N天)
    playback_days  INTEGER,
    -- 元数据
    screenshots    TEXT,                 -- 截图路径(分号分隔,最多3张)
    fail_count     INTEGER DEFAULT 0,    -- 连续失败次数(用于下线判定)
    redirect_chain TEXT,                 -- RTSP重定向链(如 40.137→136.24→6.192)
    redirect_hops  INTEGER DEFAULT 0,    -- 重定向跳数
    redirect_loop  INTEGER DEFAULT 0,    -- 是否死循环
    notes          TEXT,                 -- 备注
    first_seen     TEXT,
    last_seen      TEXT,                 -- 最近一次"可用"时间
    last_scan      TEXT,                 -- 最近一次扫描时间
    FOREIGN KEY(channel_id) REFERENCES channels(channel_id),
    UNIQUE(address)
);

-- ============ channel_preferred_sources 优选关系(ETL产出,与channels解耦) ============
-- 一频道可多行(rank=1最佳,2/3备选),现阶段每频道一行。source优选是易变加工结果,故独立成表
CREATE TABLE IF NOT EXISTS channel_preferred_sources (
    channel_id  INTEGER NOT NULL,
    source_id   INTEGER NOT NULL,
    rank        INTEGER NOT NULL DEFAULT 1,   -- 1=最佳,2=备选...(未来多源按画质)
    PRIMARY KEY(channel_id, rank),
    FOREIGN KEY(channel_id) REFERENCES channels(channel_id),
    FOREIGN KEY(source_id)  REFERENCES sources(source_id)
);

-- ============ channel_groups 频道-分组关联(多对多,保序) ============
-- 一个频道可属于多个组(主组+附加组),每个(频道,组)对记录组内位置
CREATE TABLE IF NOT EXISTS channel_groups (
    channel_id  INTEGER,
    group_name  TEXT,
    is_primary  INTEGER DEFAULT 0,   -- 1=主组,0=附加组
    order_in_group INTEGER,          -- 组内排序位置(越小越前)
    PRIMARY KEY(channel_id, group_name),
    FOREIGN KEY(channel_id) REFERENCES channels(channel_id)
);

-- ============ scan_history 扫描历史(趋势/变更分析) ============
CREATE TABLE IF NOT EXISTS scan_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_date   TEXT,
    scan_run    TEXT,                    -- 本次运行批次id(同一次pipeline共用)
    source_id   INTEGER,
    address     TEXT,
    available   INTEGER,
    resolution  TEXT,
    vbitrate    INTEGER,
    status      TEXT                     -- OK/TIMEOUT/NO_VIDEO/ERROR
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_sources_channel ON sources(channel_id);
CREATE INDEX IF NOT EXISTS idx_sources_type ON sources(source_type);
CREATE INDEX IF NOT EXISTS idx_sources_avail ON sources(available);
CREATE INDEX IF NOT EXISTS idx_history_run ON scan_history(scan_run);
CREATE INDEX IF NOT EXISTS idx_channels_group ON channels(group_primary);
CREATE INDEX IF NOT EXISTS idx_channels_key ON channels(channel_key);
"""


def init_db(db_path, reset=False):
    db_path = os.path.abspath(db_path)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    if reset and os.path.exists(db_path):
        os.remove(db_path)
        print(f"  已删除旧库: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    # 验证表
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    conn.close()
    print(f"  数据库: {db_path}")
    print(f"  表: {', '.join(tables)}")
    return db_path


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB, help='数据库路径')
    ap.add_argument('--reset', action='store_true', help='删除重建')
    args = ap.parse_args()
    print("=" * 50)
    print("  iptv-radar 建库 (schema v2)")
    print("=" * 50)
    init_db(args.db, args.reset)
    print("完成")
