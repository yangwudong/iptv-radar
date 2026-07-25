"""核心数据正确性回归测试。

每个测试对应一条 2026-07-25 review 中【已实证复现】的 bug。
测试先写(红) → 再改代码(绿),确保修复真的修到了点上而不是改了个别的地方。
"""
import json
import os
import sqlite3
import subprocess
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
    """反向保护: 不能把正常频道误标 offline。

    带 canary: 光断言 active 是无效的(频道本来就是active),必须同时证明
    下线逻辑确实执行了 —— 否则把整段下线检测删掉,这个测试照样通过。
    """
    cid = add_channel(conn, '正常频道')
    add_source(conn, '233.1.1.2:5140', channel_id=cid, channel_key='正常频道', available=1)
    canary = add_channel(conn, '零源必须下线')      # 必须被翻成 offline
    run_script('etl_process.py', '--db', db)
    got = {r['channel_key']: r['status'] for r in
           conn.execute("SELECT channel_key,status FROM channels")}
    assert got['零源必须下线'] == 'offline', "下线逻辑没跑,本测试的 active 断言无意义"
    assert got['正常频道'] == 'active'


@pytest.mark.parametrize('fail_count,expect', [(3, 'active'), (4, 'offline'), (5, 'offline')])
def test_h5_下线阈值边界(db, conn, fail_count, expect):
    """精确锁住 >= 还是 > : 阈值4时,fail_count=3不下线、=4下线。

    带 canary 确保 ETL 真的执行了(否则 active 那半边是空断言)。
    """
    cid = add_channel(conn, '边界频道', status='active')
    add_source(conn, '233.1.1.3:5140', channel_id=cid, channel_key='边界频道',
               available=0, fail_count=fail_count)
    add_channel(conn, '零源必须下线')
    run_script('etl_process.py', '--db', db, '--offline-threshold', '4')
    got = {r['channel_key']: r['status'] for r in
           conn.execute("SELECT channel_key,status FROM channels")}
    assert got['零源必须下线'] == 'offline', "下线逻辑没跑,断言无意义"
    assert got['边界频道'] == expect, f"fail_count={fail_count} 期望 {expect}, 实际 {got['边界频道']}"


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
    for f in ('link_sources.py', 'db_util.py'):
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


# ============================================================
# H3 外键约束: schema 声明了 FOREIGN KEY 但 SQLite 默认不启用
# ============================================================

def test_h3_写入不存在的频道id应被拒绝(db):
    """启用 PRAGMA foreign_keys 后,插入悬空 channel_id 必须报错而不是静默写入。"""
    sys.path.insert(0, SRC)
    import db_util
    conn = db_util.connect(db)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("""INSERT INTO sources(address,source_type,channel_id)
                        VALUES('233.1.2.3:5140','multicast',999999)""")
    conn.close()


def test_h3_体检能发现人工删频道留下的悬空引用(db, conn):
    """模拟人工用 sqlite3 CLI 删频道行(不开FK)→ 留下悬空子行,体检必须报出来。"""
    sys.path.insert(0, SRC)
    import db_util
    cid = add_channel(conn, '将被删除的频道')
    add_source(conn, '233.4.5.6:5140', channel_id=cid, channel_key='将被删除的频道')
    # 不开FK的连接(模拟CLI)强删父行
    raw = sqlite3.connect(db)
    raw.execute("DELETE FROM channels WHERE channel_id=?", (cid,))
    raw.commit()
    raw.close()

    check_conn = db_util.connect(db)
    broken = db_util.check_integrity(check_conn)
    check_conn.close()
    assert broken, "悬空引用未被体检发现"

    r = run_script('etl_process.py', '--db', db)
    assert '悬空外键引用' in r.stdout, f"ETL 未报告悬空引用:\n{r.stdout}"


# ============================================================
# F2 脱敏样例的假token不得覆盖库里的真token(已实证: 会毁掉38%播放列表)
# ============================================================

def _isolated_radar(tmp_path, db):
    """搭一个隔离的 RADAR 目录(link_sources 用 RADAR/data/source_links.json)。"""
    import shutil
    radar = tmp_path / 'radar'
    (radar / 'src').mkdir(parents=True)
    (radar / 'data').mkdir(exist_ok=True)
    (radar / 'reference').mkdir(exist_ok=True)
    for f in ('link_sources.py', 'db_util.py'):
        shutil.copy(os.path.join(SRC, f), radar / 'src' / f)
    shutil.copy(db, radar / 'data' / 'iptv.db')
    return radar


def _run_link_sources(radar, epg):
    return subprocess.run(
        [sys.executable, str(radar / 'src' / 'link_sources.py'),
         '--db', str(radar / 'data' / 'iptv.db'), '--epg', str(epg)],
        capture_output=True, text=True)


def test_f2_脱敏假token不得覆盖真token(db, conn, tmp_path):
    """认证失败时 pipeline 会回退到脱敏样例。此时绝不能把假token写进库 ——
    真token不可恢复(要等下次认证成功),覆盖=让这些频道直接播不了。"""
    cid = add_channel(conn, 'CCTV1综合')
    real = 'zoneoffset=480&accountinfo=%2C12345678%2C&it=REAL_TOKEN_ABC'
    sid = add_source(conn, 'rtsp://h/1.smil', channel_id=cid, channel_key='CCTV1综合',
                     source_type='rtsp', timeshift_query=real)
    set_preferred(conn, cid, sid)

    radar = _isolated_radar(tmp_path, db)
    # 造一个脱敏样例(和 reference/channels.sample.json 同特征)
    fake_q = 'zoneoffset=480&accountinfo=%2C00000000%2C&it=SAMPLE_TOKEN_REDACTED_NOT_REAL'
    epg = radar / 'reference' / 'sample.json'
    json.dump([{'id': '1', 'name': 'CCTV1综合',
                'url': f'rtsp://h/1.smil?{fake_q}',
                'timeshift_url': f'rtsp://h/1.smil?{fake_q}'}],
              open(epg, 'w', encoding='utf-8'), ensure_ascii=False)

    r = _run_link_sources(radar, epg)
    assert r.returncode == 0, r.stderr

    got = sqlite3.connect(radar / 'data' / 'iptv.db').execute(
        "SELECT timeshift_query FROM sources WHERE address='rtsp://h/1.smil'").fetchone()[0]
    assert 'SAMPLE_TOKEN_REDACTED' not in got, "假token覆盖了真token → 该频道会播不了"
    assert got == real, f"真token被改动: {got}"
    assert '拒绝写入' in r.stdout, f"拒写假token时必须告警,否则运维不知道token没刷新:\n{r.stdout}"


def test_f2_真token正常情况下必须能更新(db, conn, tmp_path):
    """反向保护: 别把防护做成'永不更新token'。"""
    cid = add_channel(conn, 'CCTV1综合')
    sid = add_source(conn, 'rtsp://h/1.smil', channel_id=cid, channel_key='CCTV1综合',
                     source_type='rtsp', timeshift_query='it=OLD_TOKEN')
    set_preferred(conn, cid, sid)
    radar = _isolated_radar(tmp_path, db)
    new_q = 'zoneoffset=480&accountinfo=%2C12345678%2C&it=NEW_REAL_TOKEN'
    epg = radar / 'reference' / 'fresh.json'
    json.dump([{'id': '1', 'name': 'CCTV1综合',
                'url': f'rtsp://h/1.smil?{new_q}',
                'timeshift_url': f'rtsp://h/1.smil?{new_q}'}],
              open(epg, 'w', encoding='utf-8'), ensure_ascii=False)
    r = _run_link_sources(radar, epg)
    assert r.returncode == 0, r.stderr
    got = sqlite3.connect(radar / 'data' / 'iptv.db').execute(
        "SELECT timeshift_query FROM sources WHERE address='rtsp://h/1.smil'").fetchone()[0]
    assert got == new_q, f"新的真token未写入(防护做过头了): {got}"


# ============================================================
# orphan_export 截图复用: 改前每次运行都重拍全部组播孤儿源(17个×3张,单张超时20s),
# 且拍完从不回写库 → sources.screenshots 长期为空 → "复用"分支是死代码
# ============================================================

def test_orphan_export_已有截图必须复用而不重拍(db, conn, tmp_path, monkeypatch):
    add_source(conn, '233.9.1.1:5140', channel_id=None, source_type='multicast', available=1)
    # 先跑一次(桩掉 ffmpeg 抓帧),应"新拍1个"并把路径回写库
    shots_dir = tmp_path / 'shots'
    shots_dir.mkdir()
    calls = []

    sys.path.insert(0, SRC)
    import orphan_export
    import probe

    def fake_capture(url, out_dir, prefix, count=3, **kw):
        calls.append(prefix)
        paths = []
        for i in range(1, count + 1):
            p = os.path.join(out_dir, f'{prefix}_{i}.jpg')
            os.makedirs(out_dir, exist_ok=True)
            open(p, 'wb').write(b'\xff\xd8\xff\xd9')
            paths.append(p)
        return paths

    monkeypatch.setattr(orphan_export, 'REVIEW_DIR', str(tmp_path))
    monkeypatch.setattr(orphan_export, 'SHOTS_DIR', str(shots_dir))
    monkeypatch.setattr(orphan_export.probe, 'capture_screenshots', fake_capture)
    monkeypatch.setattr(sys, 'argv', ['x', '--db', db, '--msd', 'H:4088'])
    orphan_export.main()
    assert len(calls) == 1, f"第一次应该拍1个源,实际 {len(calls)}"
    got = conn.execute("SELECT screenshots FROM sources WHERE address='233.9.1.1:5140'").fetchone()[0]
    assert got and got.count(';') == 2, f"截图路径未回写库(下次就会重拍): {got!r}"

    # 第二次: 截图已在库、文件也在磁盘 → 必须跳过
    calls.clear()
    monkeypatch.setattr(sys, 'argv', ['x', '--db', db, '--msd', 'H:4088'])
    orphan_export.main()
    assert calls == [], f"已有截图仍被重拍(每周白烧十几分钟): {calls}"

    # --reshoot 要能强制重拍
    calls.clear()
    monkeypatch.setattr(sys, 'argv', ['x', '--db', db, '--msd', 'H:4088', '--reshoot'])
    orphan_export.main()
    assert len(calls) == 1, "--reshoot 未强制重拍"


def test_orphan_export_截图文件丢了要重拍(db, conn, tmp_path, monkeypatch):
    """库里有记录但磁盘文件被删(清理output时常见) → 必须重拍,不能给出空截图的待识别包。"""
    add_source(conn, '233.9.2.2:5140', channel_id=None, source_type='multicast', available=1)
    conn.execute("UPDATE sources SET screenshots=? WHERE address='233.9.2.2:5140'",
                 ('/gone/a_1.jpg;/gone/a_2.jpg;/gone/a_3.jpg',))
    conn.commit()
    sys.path.insert(0, SRC)
    import orphan_export
    calls = []
    monkeypatch.setattr(orphan_export, 'REVIEW_DIR', str(tmp_path))
    monkeypatch.setattr(orphan_export, 'SHOTS_DIR', str(tmp_path / 'shots'))
    monkeypatch.setattr(orphan_export.probe, 'capture_screenshots',
                        lambda u, d, p, count=3, **k: calls.append(p) or [])
    monkeypatch.setattr(sys, 'argv', ['x', '--db', db, '--msd', 'H:4088'])
    orphan_export.main()
    assert len(calls) == 1, "磁盘截图丢失却没重拍"
