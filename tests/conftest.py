"""pytest 公共 fixture: 造一个最小可用的临时库(不碰生产 data/iptv.db)。

原则: 每个测试拿到全新临时库,互不干扰;schema 从 db_schema.py 建(保证测试跟着权威schema走)。
"""
import os
import sys
import sqlite3
import subprocess

import pytest

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src')
sys.path.insert(0, SRC)


@pytest.fixture
def db(tmp_path):
    """空库(仅schema)。返回 db 文件路径。"""
    p = str(tmp_path / 'test.db')
    subprocess.run([sys.executable, os.path.join(SRC, 'db_schema.py'), '--db', p],
                   check=True, capture_output=True)
    return p


@pytest.fixture
def conn(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def add_channel(conn, channel_key, name=None, group='测试组', order=1,
                enabled=1, status='active', sort_hint=None):
    """插一个频道 + 它的分组行。返回 channel_id。"""
    cur = conn.execute(
        """INSERT INTO channels(channel_key,name,tvg_id,tvg_logo,enabled,status,sort_hint)
           VALUES(?,?,?,?,?,?,?)""",
        (channel_key, name or channel_key, channel_key, '', enabled, status, sort_hint))
    cid = cur.lastrowid
    conn.execute(
        """INSERT INTO channel_groups(channel_id,group_name,is_primary,order_in_group)
           VALUES(?,?,1,?)""", (cid, group, order))
    conn.commit()
    return cid


def add_source(conn, address, channel_id=None, channel_key=None, source_type='multicast',
               available=1, fail_count=0, res_label='1080P', vbitrate=8000,
               playback_days=0, timeshift_query=None):
    cur = conn.execute(
        """INSERT INTO sources(address,source_type,channel_id,channel_key,available,
                               fail_count,res_label,vbitrate,playback_days,timeshift_query)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (address, source_type, channel_id, channel_key, available, fail_count,
         res_label, vbitrate, playback_days, timeshift_query))
    conn.commit()
    return cur.lastrowid


def set_preferred(conn, channel_id, source_id, rank=1):
    conn.execute("""INSERT INTO channel_preferred_sources(channel_id,source_id,rank)
                    VALUES(?,?,?)""", (channel_id, source_id, rank))
    conn.commit()


def run_script(name, *args, cwd=SRC, expect_ok=True):
    """跑 src/ 下的脚本,返回 CompletedProcess。"""
    r = subprocess.run([sys.executable, os.path.join(SRC, name), *args],
                       capture_output=True, text=True, cwd=cwd)
    if expect_ok and r.returncode != 0:
        raise AssertionError(f"{name} 失败(rc={r.returncode}):\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}")
    return r


def preferred_rows(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT channel_id,source_id,rank FROM channel_preferred_sources ORDER BY channel_id,rank")]
