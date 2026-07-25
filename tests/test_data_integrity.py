"""核心数据正确性回归测试。

每个测试对应一条 2026-07-25 review 中【已实证复现】的 bug。
测试先写(红) → 再改代码(绿),确保修复真的修到了点上而不是改了个别的地方。
"""
import json
import os
import sqlite3
import sys

import pytest

from conftest import (SRC, add_channel, add_source, set_preferred, run_script,
                      preferred_rows)


# ============================================================
# C1 串台: 源改判到别的频道后,原频道的旧优选记录没被清理
#    → gen_m3u 里两个频道指向同一地址(点A播出B的内容)
# ============================================================

def test_c1_源改判后原频道旧优选必须被清理(db, conn):
    """频道A的唯一源被改判给频道B后, A 不应再残留 rank=1 优选行。"""
    a = add_channel(conn, 'A频道', order=1)
    b = add_channel(conn, 'B频道', order=2)
    sid = add_source(conn, '233.1.1.1:5140', channel_id=a, channel_key='A频道')
    set_preferred(conn, a, sid)

    # 人工纠错: 把源改判给 B (link_sources 的行为: 只改 sources,不动优选表)
    conn.execute("UPDATE sources SET channel_id=?, channel_key='B频道' WHERE source_id=?", (b, sid))
    conn.commit()

    run_script('etl_process.py', '--db', db)

    rows = preferred_rows(conn)
    a_rows = [r for r in rows if r[0] == a]
    assert a_rows == [], f"频道A零源后仍残留优选行 {a_rows} → 会串台"


def test_c1_串台数据不得进入m3u(db, conn, tmp_path):
    """防御层: 即使库里存在脏的跨频道优选行(如旧库没有唯一索引时留下的),
    gen_m3u 也不能把B的地址输出给A。"""
    a = add_channel(conn, 'A频道', order=1)
    b = add_channel(conn, 'B频道', order=2)
    sid = add_source(conn, '233.1.1.1:5140', channel_id=b, channel_key='B频道')
    # 模拟旧库(无防串台索引)遗留的脏数据
    conn.execute("DROP INDEX IF EXISTS idx_pref_rank1_source")
    set_preferred(conn, a, sid)   # 脏行: A 指向属于 B 的源
    set_preferred(conn, b, sid)

    out = str(tmp_path / 'x.m3u')
    run_script('gen_m3u.py', '--db', db, '--out', out, '--msd', 'H:4088')
    text = open(out, encoding='utf-8').read()

    # A 不该出现(它已无合法自有源),B 应正常出现
    assert ',A频道' not in text, "串台: A频道用了B频道的源被写进m3u"
    assert ',B频道' in text


def test_c1_唯一索引阻止造出串台(db, conn):
    """数据库层约束: rank=1 的 source_id 唯一(任何代码路径都造不出串台)。"""
    a = add_channel(conn, 'A频道', order=1)
    b = add_channel(conn, 'B频道', order=2)
    sid = add_source(conn, '233.1.1.1:5140', channel_id=a, channel_key='A频道')
    set_preferred(conn, a, sid)

    with pytest.raises(sqlite3.IntegrityError):
        set_preferred(conn, b, sid)


def test_c1_etl自愈补索引(db, conn):
    """旧库(无索引)跑一次 etl 后应自动补上防串台索引。"""
    conn.execute("DROP INDEX IF EXISTS idx_pref_rank1_source")
    conn.commit()
    run_script('etl_process.py', '--db', db)
    idx = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_pref_rank1_source'"
    ).fetchone()
    assert idx is not None, "etl 未自愈补上索引"


def test_c1_旧库脏行应被etl清理且不阻塞建索引(db, conn):
    """旧库里已有脏行时, etl 必须先清脏再建索引(顺序错就会建索引失败)。"""
    a = add_channel(conn, 'A频道', order=1)
    b = add_channel(conn, 'B频道', order=2)
    sid = add_source(conn, '233.1.1.1:5140', channel_id=b, channel_key='B频道')
    conn.execute("DROP INDEX IF EXISTS idx_pref_rank1_source")
    set_preferred(conn, a, sid)
    set_preferred(conn, b, sid)
    conn.commit()

    run_script('etl_process.py', '--db', db)   # 不得抛错

    rows = preferred_rows(conn)
    assert [r for r in rows if r[0] == a] == [], "A 的脏行未被清理"
    assert [r for r in rows if r[0] == b] != [], "B 的合法优选被误删"


# ============================================================
# H5 零源频道永不标 offline (生产库12个频道正在错标 active)
# ============================================================

def test_h5_零源频道应标offline(db, conn):
    cid = add_channel(conn, '无源频道', status='active')
    run_script('etl_process.py', '--db', db)
    st = conn.execute("SELECT status FROM channels WHERE channel_id=?", (cid,)).fetchone()['status']
    assert st == 'offline', f"零源频道 status={st}, 期望 offline"


def test_h5_有可用源的频道保持active(db, conn):
    """反向保护: 不能把正常频道误标 offline。"""
    cid = add_channel(conn, '正常频道')
    add_source(conn, '233.1.1.2:5140', channel_id=cid, channel_key='正常频道', available=1)
    run_script('etl_process.py', '--db', db)
    st = conn.execute("SELECT status FROM channels WHERE channel_id=?", (cid,)).fetchone()['status']
    assert st == 'active'


def test_h5_源失效未达阈值不应标offline(db, conn):
    """容错: fail_count < 阈值 视为可能误报,不下线。"""
    cid = add_channel(conn, '临时失效频道')
    add_source(conn, '233.1.1.3:5140', channel_id=cid, channel_key='临时失效频道',
               available=0, fail_count=2)
    run_script('etl_process.py', '--db', db, '--offline-threshold', '4')
    st = conn.execute("SELECT status FROM channels WHERE channel_id=?", (cid,)).fetchone()['status']
    assert st == 'active', "fail_count=2 < 阈值4, 不该下线"


def test_h5_多个零源频道必须全部被标记(db, conn):
    """回归: 下线检测曾经边遍历cursor边UPDATE同一cursor,
    第一次UPDATE会重置结果集导致循环静默中断 —— 只有第一个频道被标记,后面全漏。
    (AGENTS.md 规则3: SQLite遍历时别边读边写同一cursor)"""
    ids = [add_channel(conn, f'无源{i}', status='active') for i in range(5)]
    run_script('etl_process.py', '--db', db)
    got = {r['channel_key']: r['status'] for r in
           conn.execute("SELECT channel_key,status FROM channels")}
    missed = [k for k, v in got.items() if v != 'offline']
    assert not missed, f"这些零源频道没被标记(循环提前中断): {missed}"


def test_h5_下线与恢复混合场景全部生效(db, conn):
    """同一次运行里既有要下线的、也有要恢复的,两类都不能因循环中断被漏掉。"""
    dead = [add_channel(conn, f'该下线{i}', status='active') for i in range(3)]
    alive = [add_channel(conn, f'该恢复{i}', status='offline') for i in range(3)]
    for i, cid in enumerate(alive):
        add_source(conn, f'233.9.9.{i}:5140', channel_id=cid,
                   channel_key=f'该恢复{i}', available=1)
    run_script('etl_process.py', '--db', db)
    rows = {r['channel_key']: r['status'] for r in
            conn.execute("SELECT channel_key,status FROM channels")}
    assert all(rows[f'该下线{i}'] == 'offline' for i in range(3)), rows
    assert all(rows[f'该恢复{i}'] == 'active' for i in range(3)), rows


def test_h5_零源频道恢复源后应回active(db, conn):
    cid = add_channel(conn, '恢复频道', status='offline')
    add_source(conn, '233.1.1.4:5140', channel_id=cid, channel_key='恢复频道', available=1)
    run_script('etl_process.py', '--db', db)
    st = conn.execute("SELECT status FROM channels WHERE channel_id=?", (cid,)).fetchone()['status']
    assert st == 'active'


# ============================================================
# H4 禁用频道会永久销毁人工归并快照 (source_links.json 被全量覆盖重建)
# ============================================================

def _write_snapshot(radar_dir, mapping):
    d = os.path.join(radar_dir, 'data')
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, 'source_links.json')
    json.dump(mapping, open(p, 'w', encoding='utf-8'), ensure_ascii=False)
    return p


def test_h4_禁用频道不得销毁人工归并快照(db, conn, tmp_path):
    """禁用一个频道后跑 link_sources, 该频道的人工归并记录必须仍在 source_links.json 里。"""
    cid = add_channel(conn, '被禁频道', enabled=0)       # 已禁用
    add_source(conn, 'rtsp://x/1.smil', channel_id=cid, channel_key='被禁频道',
               source_type='rtsp')

    # link_sources 用 RADAR/data/source_links.json, 需要构造一个隔离的 RADAR 目录
    radar = tmp_path / 'radar'
    (radar / 'src').mkdir(parents=True)
    (radar / 'data').mkdir(exist_ok=True)
    # 复制脚本进隔离目录,让它的 RADAR 指向 tmp
    import shutil
    for f in ('link_sources.py',):
        shutil.copy(os.path.join(SRC, f), radar / 'src' / f)
    shutil.copy(db, radar / 'data' / 'iptv.db')
    snap = _write_snapshot(str(radar), {'rtsp://x/1.smil': '被禁频道'})
    epg = radar / 'data' / 'epg.json'
    json.dump([], open(epg, 'w', encoding='utf-8'))

    import subprocess
    r = subprocess.run([sys.executable, str(radar / 'src' / 'link_sources.py'),
                        '--db', str(radar / 'data' / 'iptv.db'), '--epg', str(epg)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

    after = json.load(open(snap, encoding='utf-8'))
    assert after.get('rtsp://x/1.smil') == '被禁频道', (
        f"禁用频道后人工归并快照被销毁(不可逆): {after}")


# ============================================================
# C2 measure_bitrate 阻塞读: 上游进程不关闭 stdout 时会永久挂死
# ============================================================

def test_c2_measure_bitrate必须在超时内返回(monkeypatch):
    """用一个永不关闭 stdout、也永不输出数据的假子进程模拟卡死的 ffmpeg。"""
    import time
    import probe

    class _HangingStdout:
        def read(self, n):
            time.sleep(30)      # 模拟阻塞读: 远超 timeout
            return b''
        def fileno(self):
            return os.open(os.devnull, os.O_RDONLY)

    class _FakeProc:
        pid = os.getpid()       # killpg 会被 monkeypatch 掉,不会真杀自己
        stdout = _HangingStdout()
        def poll(self):
            return None

    monkeypatch.setattr(probe.subprocess, 'Popen', lambda *a, **k: _FakeProc())
    monkeypatch.setattr(probe.os, 'killpg', lambda *a, **k: None)
    monkeypatch.setattr(probe.os, 'getpgid', lambda pid: pid)

    t0 = time.time()
    probe.measure_bitrate('rtp://fake', duration=1, timeout=2)
    elapsed = time.time() - t0
    assert elapsed < 8, f"measure_bitrate 卡了 {elapsed:.1f}s (timeout=2), 阻塞读未受控"
