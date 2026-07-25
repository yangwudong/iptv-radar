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
