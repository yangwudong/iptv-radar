#!/usr/bin/env python3
"""数据库连接工具: 统一 SQLite 连接参数。

为什么要有这个模块(别再各处 sqlite3.connect 直写):
  1. timeout: 默认只有 5 秒。扫描一轮可能持续几十秒并持有写事务,
     此时其他脚本(生成/Dashboard)5秒就抛 "database is locked" 中断整条流水线。
     统一 30 秒。曾经14处连接里有7处漏了 timeout,靠人工发现。
  2. foreign_keys: SQLite 默认**不启用**外键约束(每连接独立)。
     schema 里写的 FOREIGN KEY 声明在不开这个 PRAGMA 时纯属装饰,
     写入不存在的 channel_id 不会报错,静默产生悬空引用。
"""
import sqlite3


def connect(db_path, row_factory=True, foreign_keys=True, timeout=30):
    conn = sqlite3.connect(db_path, timeout=timeout)
    if row_factory:
        conn.row_factory = sqlite3.Row
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys=ON")
    return conn


def check_integrity(conn):
    """检查悬空外键引用,返回问题列表 [(表, rowid, 被引用表, 外键序号), ...]。

    为什么需要它(光靠 foreign_keys=ON 不够): 人工纠错时用 sqlite3 命令行
    DELETE 重复频道行,CLI 会话默认不开 FK,照样能删成功并留下悬空的
    channel_groups / channel_preferred_sources / sources.channel_id。
    这个检查能在下次 pipeline 里把这类损坏显式报出来。
    """
    return list(conn.execute("PRAGMA foreign_key_check"))


def load_env(radar_root=None):
    """解析项目根 .env → dict。

    为什么不能用 `line.split('=',1)` 了事(踩过):
      `.env` 里写 `FCC_SERVER=1.2.3.4:8027  # 说明` 时,朴素解析会把注释一起当成值,
      于是 FCC 服务器地址变成 "1.2.3.4:8027  # 说明" —— 认证/URL 静默用错值且不报错。
      shell 的 `source` 能正确忽略行内注释,Python 手写解析不会,两边行为不一致更坑。
    处理: 去引号;仅当 '#' 前有空白且不在引号内时,视为行内注释截断。
    """
    import os as _os
    if radar_root is None:
        radar_root = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), '..')
    env = {}
    path = _os.path.join(radar_root, '.env')
    if not _os.path.exists(path):
        return env
    for raw in open(path, encoding='utf-8'):
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in '"\'':
            v = v[1:-1]                      # 带引号: 引号内原样(允许含 #)
        else:
            cut = len(v)                     # 无引号: 空白+# 之后算注释
            for i in range(1, len(v)):
                if v[i] == '#' and v[i - 1] in ' \t':
                    cut = i
                    break
            v = v[:cut].strip()
        env[k.strip()] = v
    return env


def ensure_schema(conn, verbose=True):
    """自愈式 schema 迁移(旧库升级到当前 schema)。幂等,可反复调用。

    为什么放在这里而不是各脚本自己 ALTER: 原来 link_sources / probe_timeshift / etl_process
    各自散落着"缺列就 ALTER"的自愈代码,schema 兼容逻辑跑到了清洗层和生成层里。
    """
    changed = []
    cols_of = lambda t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
    has_table = lambda t: bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone())

    # 库还没建表(首次部署 / CI 里没有 data/iptv.db) → 无可迁移,直接返回。
    # 建表是 db_schema.py 的职责,这里只负责把**已存在**的旧库升级到当前 schema。
    if not has_table('channels'):
        return changed

    scols = cols_of('sources')
    for col, decl in (('timeshift_query', 'TEXT'), ('playback_days', 'INTEGER')):
        if scols and col not in scols:
            conn.execute(f"ALTER TABLE sources ADD COLUMN {col} {decl}")
            changed.append(f'+sources.{col}')
            scols = cols_of('sources')

    # 分组双真相清理: channels.group_primary/group_extra 与 channel_groups 表重复,
    # 而 m3u 读表、Dashboard 曾读列 → 只改一处就两边不一致。表是唯一真相,删列。
    # 必须先删索引: idx_channels_group 建在 group_primary 上,
    # 不先删的话 ALTER TABLE ... DROP COLUMN 会报 "error in index ... no such column"
    conn.execute("DROP INDEX IF EXISTS idx_channels_group")

    ccols = cols_of('channels')
    for col in ('group_primary', 'group_extra'):
        if ccols and col in ccols:
            # 删列前先确保信息不丢: 凡是列里写了分组、但 channel_groups 里没有对应行的,
            # 删列就会丢分组。占位频道(__UNKNOWN__/__JUNK__)本就无分组也不进m3u,不算。
            missing = conn.execute(f"""SELECT COUNT(*) FROM channels ch
                                      WHERE COALESCE(ch.{col},'') != ''
                                        AND NOT EXISTS(SELECT 1 FROM channel_groups g
                                                       WHERE g.channel_id=ch.channel_id)""").fetchone()[0]
            if missing:
                raise RuntimeError(
                    f"有 {missing} 个频道 channels.{col} 有值但 channel_groups 里没有分组行,"
                    f"删列会丢失分组信息。请先补齐 channel_groups 再升级。")
            conn.execute(f"ALTER TABLE channels DROP COLUMN {col}")
            changed.append(f'-channels.{col}')
            ccols = cols_of('channels')

    if has_table('channel_preferred_sources'):
        conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_pref_rank1_source
                        ON channel_preferred_sources(source_id) WHERE rank = 1""")
    if changed:
        conn.commit()
        if verbose:
            print(f"  schema自愈: {', '.join(changed)}")
    return changed
