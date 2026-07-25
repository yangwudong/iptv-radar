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
    # 复制 src/ 下所有 .py: 每次新增共享模块都手动往列表里加太容易漏
    # (漏了就是运行期 ModuleNotFoundError,已踩过一次: 新增 address_util 时)
    for f in os.listdir(SRC):
        if f.endswith('.py'):
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
    entry = after.get('rtsp://x/1.smil')
    # 快照值现在是 {"channel_id":N,"channel_key":"名"};旧格式是纯字符串,两种都接受
    got_key = entry.get('channel_key') if isinstance(entry, dict) else entry
    assert got_key == '被禁频道', f"禁用频道后人工归并快照被销毁(不可逆): {after}" 


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
    # 复制 src/ 下所有 .py: 每次新增共享模块都手动往列表里加太容易漏
    # (漏了就是运行期 ModuleNotFoundError,已踩过一次: 新增 address_util 时)
    for f in os.listdir(SRC):
        if f.endswith('.py'):
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


# ============================================================
# 项目3: 归并快照必须用不变的 channel_id 做键
#   原来用 channel_key(频道名)做键。改名/合并频道 → 快照条目被当"频道已不存在"
#   静默丢弃,而缩水告警(>10%)抓不住(447条丢5条=1.1%)。
#   而快照存在的意义就是"库丢了也能恢复人工归并" —— 改名后这层保险就失效了。
# ============================================================

def test_p3_频道改名后快照仍能恢复归并(db, conn, tmp_path):
    cid = add_channel(conn, '浙江钱江都市')
    add_source(conn, '233.5.1.1:5140', channel_id=None, channel_key=None)   # 未关联,只能靠快照
    radar = _isolated_radar(tmp_path, db)

    # 快照按新格式记录(带 channel_id)
    snap = radar / 'data' / 'source_links.json'
    json.dump({'233.5.1.1:5140': {'channel_id': cid, 'channel_key': '浙江钱江都市'}},
              open(snap, 'w', encoding='utf-8'), ensure_ascii=False)

    # 人工改名(AGENTS.md 记录过的真实操作: 合并更正/改规范名)
    raw = sqlite3.connect(radar / 'data' / 'iptv.db')
    raw.execute("UPDATE channels SET channel_key='钱江都市', name='钱江都市' WHERE channel_id=?", (cid,))
    raw.commit(); raw.close()

    epg = radar / 'data' / 'epg.json'
    json.dump([], open(epg, 'w', encoding='utf-8'))
    r = _run_link_sources(radar, epg)
    assert r.returncode == 0, r.stderr

    row = sqlite3.connect(radar / 'data' / 'iptv.db').execute(
        "SELECT channel_id, channel_key FROM sources WHERE address='233.5.1.1:5140'").fetchone()
    assert row[0] == cid, f"改名后快照失效,人工归并丢了(改名前 channel_id={cid}, 实际 {row[0]})"
    assert row[1] == '钱江都市', f"channel_key 冗余列未跟着更新: {row[1]}"


def test_p3_旧格式快照必须仍能读(db, conn, tmp_path):
    """向后兼容: 现有 447 条是 {address: "频道名"} 旧格式,升级不能让它们失效。"""
    cid = add_channel(conn, 'CCTV1综合')
    add_source(conn, '233.5.2.2:5140', channel_id=None, channel_key=None)
    radar = _isolated_radar(tmp_path, db)
    json.dump({'233.5.2.2:5140': 'CCTV1综合'},          # 旧格式: 值是字符串
              open(radar / 'data' / 'source_links.json', 'w', encoding='utf-8'),
              ensure_ascii=False)
    epg = radar / 'data' / 'epg.json'
    json.dump([], open(epg, 'w', encoding='utf-8'))
    r = _run_link_sources(radar, epg)
    assert r.returncode == 0, r.stderr
    row = sqlite3.connect(radar / 'data' / 'iptv.db').execute(
        "SELECT channel_id FROM sources WHERE address='233.5.2.2:5140'").fetchone()
    assert row[0] == cid, "旧格式快照读不了了(447条人工归并会全丢)"


def test_p3_快照写出的是新格式(db, conn, tmp_path):
    cid = add_channel(conn, 'CCTV1综合')
    add_source(conn, '233.5.3.3:5140', channel_id=cid, channel_key='CCTV1综合')
    radar = _isolated_radar(tmp_path, db)
    json.dump({}, open(radar / 'data' / 'source_links.json', 'w', encoding='utf-8'))
    epg = radar / 'data' / 'epg.json'
    json.dump([], open(epg, 'w', encoding='utf-8'))
    r = _run_link_sources(radar, epg)
    assert r.returncode == 0, r.stderr
    snap = json.load(open(radar / 'data' / 'source_links.json', encoding='utf-8'))
    v = snap.get('233.5.3.3:5140')
    assert isinstance(v, dict), f"快照仍是旧格式(改名后会丢): {v!r}"
    assert v['channel_id'] == cid and v['channel_key'] == 'CCTV1综合', v


# ============================================================
# orphan_import: 5种 action 的落库正确性
#   这是"孤儿源识别"的落库端,从未在真实数据上跑过。而删除 channels.group_primary 列时
#   改漏了 new 分支的 INSERT(7个值塞6个列 → 直接 OperationalError)。
# ============================================================

def _write_inbox(tmp_path, decisions):
    inbox = tmp_path / 'inbox'
    inbox.mkdir(exist_ok=True)
    p = inbox / 'resolved_test.json'
    json.dump({'resolved_at': '2026-07-25T00:00:00', 'decisions': decisions},
              open(p, 'w', encoding='utf-8'), ensure_ascii=False)
    return str(inbox)


def _run_import(db, inbox, radar, extra=()):
    return subprocess.run(
        [sys.executable, str(radar / 'src' / 'orphan_import.py'),
         '--db', db, '--inbox', inbox, *extra],
        capture_output=True, text=True)


def test_orphan_new_建新频道并归到分组末尾(db, conn, tmp_path):
    """action=new: 建频道 + 写 channel_groups(组内末尾) + 归并源。
    回归: 删 group_primary 列时改漏了这条 INSERT,new 直接报错。"""
    add_channel(conn, '已有频道', group='其他', order=7)
    add_source(conn, '233.7.0.1:5140', channel_id=None)
    radar = _isolated_radar(tmp_path, db)
    inbox = _write_inbox(tmp_path, [
        {'address': '233.7.0.1:5140', 'action': 'new',
         'channel_key': '浙江政务', 'group': '其他'}])
    r = _run_import(db, inbox, radar)
    assert r.returncode == 0, f"new 落库失败:\n{r.stdout}{r.stderr}"

    row = conn.execute("""SELECT ch.channel_id, ch.enabled, ch.status
                          FROM channels ch WHERE ch.channel_key='浙江政务'""").fetchone()
    assert row is not None, f"新频道没建出来:\n{r.stdout}"
    assert row['enabled'] == 1, f"enabled 应为1,实际 {row['enabled']!r}(列错位?)"
    assert row['status'] == 'active', f"status 应为 active,实际 {row['status']!r}"

    g = conn.execute("""SELECT group_name, is_primary, order_in_group FROM channel_groups
                        WHERE channel_id=?""", (row['channel_id'],)).fetchone()
    assert g['group_name'] == '其他' and g['is_primary'] == 1
    assert g['order_in_group'] == 8, f"应归到组内末尾(7+1=8),实际 {g['order_in_group']}"

    src = conn.execute("SELECT channel_id, channel_key FROM sources WHERE address='233.7.0.1:5140'").fetchone()
    assert src['channel_id'] == row['channel_id'] and src['channel_key'] == '浙江政务'


def test_orphan_new_可以创建全新分组(db, conn, tmp_path):
    """用户要能新建分组(填一个库里还没有的组名)。"""
    add_source(conn, '233.7.0.2:5140', channel_id=None)
    radar = _isolated_radar(tmp_path, db)
    inbox = _write_inbox(tmp_path, [
        {'address': '233.7.0.2:5140', 'action': 'new',
         'channel_key': '某新频道', 'group': '全新分组'}])
    r = _run_import(db, inbox, radar)
    assert r.returncode == 0, r.stdout + r.stderr
    g = conn.execute("""SELECT g.group_name, g.order_in_group FROM channel_groups g
                        JOIN channels ch USING(channel_id)
                        WHERE ch.channel_key='某新频道'""").fetchone()
    assert g['group_name'] == '全新分组'
    assert g['order_in_group'] == 1, f"新分组第一个成员应为1,实际 {g['order_in_group']}"


def test_orphan_assign_归并到已有频道(db, conn, tmp_path):
    cid = add_channel(conn, 'CCTV1综合')
    add_source(conn, '233.7.0.3:5140', channel_id=None)
    radar = _isolated_radar(tmp_path, db)
    inbox = _write_inbox(tmp_path, [
        {'address': '233.7.0.3:5140', 'action': 'assign', 'channel_key': 'CCTV1综合'}])
    r = _run_import(db, inbox, radar)
    assert r.returncode == 0, r.stdout + r.stderr
    src = conn.execute("SELECT channel_id FROM sources WHERE address='233.7.0.3:5140'").fetchone()
    assert src['channel_id'] == cid


def test_orphan_junk与unknown挂占位频道且不进m3u(db, conn, tmp_path):
    for k in ('__JUNK__', '__UNKNOWN__'):
        conn.execute("""INSERT INTO channels(channel_key,name,enabled,status)
                        VALUES(?,?,0,'placeholder')""", (k, k))
    conn.commit()
    add_source(conn, '233.7.0.4:5140', channel_id=None)
    add_source(conn, '233.7.0.5:5140', channel_id=None)
    radar = _isolated_radar(tmp_path, db)
    inbox = _write_inbox(tmp_path, [
        {'address': '233.7.0.4:5140', 'action': 'junk'},
        {'address': '233.7.0.5:5140', 'action': 'unknown'}])
    r = _run_import(db, inbox, radar)
    assert r.returncode == 0, r.stdout + r.stderr
    for addr, key in (('233.7.0.4:5140', '__JUNK__'), ('233.7.0.5:5140', '__UNKNOWN__')):
        got = conn.execute("""SELECT ch.channel_key FROM sources s
                              JOIN channels ch ON ch.channel_id=s.channel_id
                              WHERE s.address=?""", (addr,)).fetchone()
        assert got and got['channel_key'] == key, f"{addr} 未挂到 {key}"
    out = str(tmp_path / 'j.m3u')
    run_script('gen_m3u.py', '--db', db, '--out', out, '--msd', 'H:4088')
    text = open(out, encoding='utf-8').read()
    assert '233.7.0.4' not in text and '233.7.0.5' not in text, "垃圾流进了m3u"


def test_orphan_skip不动库(db, conn, tmp_path):
    add_source(conn, '233.7.0.6:5140', channel_id=None)
    radar = _isolated_radar(tmp_path, db)
    inbox = _write_inbox(tmp_path, [{'address': '233.7.0.6:5140', 'action': 'skip'}])
    r = _run_import(db, inbox, radar)
    assert r.returncode == 0, r.stdout + r.stderr
    src = conn.execute("SELECT channel_id FROM sources WHERE address='233.7.0.6:5140'").fetchone()
    assert src['channel_id'] is None, "skip 却写了库"


def test_orphan_快照写新格式(db, conn, tmp_path):
    """快照必须是 {address:{channel_id,channel_key}} —— 否则改名后归并会丢(项目3)。

    注: orphan_import 的快照路径跟着 --db 走(snapshot_path_for),不是 RADAR/data/。
    """
    cid = add_channel(conn, 'CCTV1综合')
    add_source(conn, '233.7.0.7:5140', channel_id=None)
    radar = _isolated_radar(tmp_path, db)
    snap_file = os.path.join(os.path.dirname(os.path.abspath(db)), 'source_links.json')
    json.dump({}, open(snap_file, 'w', encoding='utf-8'))
    inbox = _write_inbox(tmp_path, [
        {'address': '233.7.0.7:5140', 'action': 'assign', 'channel_key': 'CCTV1综合'}])
    r = _run_import(db, inbox, radar)
    assert r.returncode == 0, r.stdout + r.stderr
    snap = json.load(open(snap_file, encoding='utf-8'))
    v = snap.get('233.7.0.7:5140')
    assert isinstance(v, dict), f"快照写成旧格式了: {v!r}"
    assert v['channel_id'] == cid and v['channel_key'] == 'CCTV1综合'


# ============================================================
# junk/unknown 的识别成果必须持久化(否则每周重新识别同一批垃圾流)
#   arrange: 17 个组播孤儿是黑名单/无效流,人工标 junk 一次就该永久生效。
#   若只写库不写快照,库一重建(或源行被重新插入)就全变回孤儿 —— 白干。
# ============================================================

def _junk_cid(conn):
    for k in ('__JUNK__', '__UNKNOWN__'):
        conn.execute("""INSERT INTO channels(channel_key,name,enabled,status)
                        VALUES(?,?,0,'placeholder')""", (k, k))
    conn.commit()
    return conn.execute("SELECT channel_id FROM channels WHERE channel_key='__JUNK__'").fetchone()[0]


def test_orphan_junk决定必须写入归并快照(db, conn, tmp_path):
    """junk 是人工识别成果,和 assign 一样要进快照。"""
    _junk_cid(conn)
    add_source(conn, '233.7.9.1:5140', channel_id=None)
    radar = _isolated_radar(tmp_path, db)
    snap_file = os.path.join(os.path.dirname(os.path.abspath(db)), 'source_links.json')
    json.dump({}, open(snap_file, 'w', encoding='utf-8'))
    inbox = _write_inbox(tmp_path, [{'address': '233.7.9.1:5140', 'action': 'junk'}])
    r = _run_import(db, inbox, radar)
    assert r.returncode == 0, r.stdout + r.stderr
    snap = json.load(open(snap_file, encoding='utf-8'))
    v = snap.get('233.7.9.1:5140')
    assert v is not None, "junk 决定没进快照 → 库一重建就要重新识别一遍"
    assert v['channel_key'] == '__JUNK__', f"快照里的 channel_key 不对: {v!r}"


def test_orphan_junk在库重建后仍不是孤儿(db, conn, tmp_path):
    """端到端: 标 junk → 库里关联被清空(模拟重建) → link_sources 必须从快照恢复。
    这条才是'持久化'的真实意义,只断言快照有条目不够。"""
    _junk_cid(conn)
    add_source(conn, '233.7.9.2:5140', channel_id=None)
    radar = _isolated_radar(tmp_path, db)
    rdb = str(radar / 'data' / 'iptv.db')
    # 用 radar 里的库跑 import,让快照正好落在 link_sources 会读的位置
    inbox = _write_inbox(tmp_path, [{'address': '233.7.9.2:5140', 'action': 'junk'}])
    r = _run_import(rdb, inbox, radar)
    assert r.returncode == 0, r.stdout + r.stderr

    rconn = sqlite3.connect(rdb)
    jcid = rconn.execute("SELECT channel_id FROM channels WHERE channel_key='__JUNK__'").fetchone()[0]
    assert rconn.execute("SELECT channel_id FROM sources WHERE address='233.7.9.2:5140'").fetchone()[0] == jcid
    # 模拟库重建/源行重插: 关联清空
    rconn.execute("UPDATE sources SET channel_id=NULL, channel_key=NULL")
    rconn.commit()
    rconn.close()

    epg = radar / 'reference' / 'e.json'
    json.dump([], open(epg, 'w', encoding='utf-8'))
    r2 = _run_link_sources(radar, epg)
    assert r2.returncode == 0, r2.stderr

    got = sqlite3.connect(rdb).execute(
        "SELECT channel_id FROM sources WHERE address='233.7.9.2:5140'").fetchone()[0]
    assert got == jcid, f"junk 源变回孤儿了(channel_id={got!r}) → 人工识别成果丢失"


def test_orphan_junk会连带同一官方频道的配对地址(db, conn, tmp_path):
    """表征测试(锁既有行为,非新功能): 官方 channels.json 里一个频道同时有组播+单播地址。
    只把组播标 junk,link_sources 按官方列表会把配对的单播也归到 __JUNK__。

    这是对的(本来就是同一频道,一个决定覆盖两个源),但很隐蔽 ——
    识别页面必须展示配对关系,否则用户不知道自己一次动了两个源。
    实测: 好易购1高清 = 233.50.201.248:5140 + .../53485722.smil
    """
    _junk_cid(conn)
    mc, rt = '233.50.201.248:5140', 'rtsp://115.233.40.137/PLTV/x/1.smil'
    add_source(conn, mc, channel_id=None)
    add_source(conn, rt, channel_id=None, source_type='rtsp')
    radar = _isolated_radar(tmp_path, db)
    rdb = str(radar / 'data' / 'iptv.db')
    inbox = _write_inbox(tmp_path, [{'address': mc, 'action': 'junk'}])   # 只标组播
    assert _run_import(rdb, inbox, radar).returncode == 0

    epg = radar / 'reference' / 'e.json'
    json.dump([{'id': '1', 'name': '好易购1高清', 'url': f'igmp://{mc}|{rt}'}],
              open(epg, 'w', encoding='utf-8'), ensure_ascii=False)
    r = _run_link_sources(radar, epg)
    assert r.returncode == 0, r.stderr

    got = dict(sqlite3.connect(rdb).execute(
        "SELECT address, channel_key FROM sources").fetchall())
    assert got[mc] == '__JUNK__', f"被标的组播没归 junk: {got[mc]!r}"
    assert got[rt] == '__JUNK__', (
        f"配对单播未连带(得 {got[rt]!r}) —— 若此行为变了,识别页面的配对提示要同步改")


# ============================================================
# 单播孤儿也要截图(官方名只是线索,画面才是证据)
#   坑: 截图前缀原来是 addr.split(':')[0].replace('.','_'),
#   对 'rtsp://...' 得到的是字面 'rtsp' → 10个单播源全部互相覆盖,
#   最后只剩一套图,而且每个源都指向它 —— 静默错到"看图识别"直接失效。
# ============================================================

def _fake_capture_factory(calls):
    def fake_capture(url, out_dir, prefix, count=3, **kw):
        calls.append((prefix, url))
        os.makedirs(out_dir, exist_ok=True)
        paths = []
        for i in range(1, count + 1):
            fp = os.path.join(out_dir, f'{prefix}_{i}.jpg')
            open(fp, 'wb').write(b'\xff\xd8\xff\xd9')
            paths.append(fp)
        return paths
    return fake_capture


def _run_export(db, tmp_path, monkeypatch, calls, extra=()):
    sys.path.insert(0, SRC)
    import orphan_export
    shots = tmp_path / 'shots'
    monkeypatch.setattr(orphan_export, 'REVIEW_DIR', str(tmp_path))
    monkeypatch.setattr(orphan_export, 'SHOTS_DIR', str(shots))
    monkeypatch.setattr(orphan_export.probe, 'capture_screenshots', _fake_capture_factory(calls))
    monkeypatch.setattr(sys, 'argv', ['x', '--db', db, '--msd', 'H:4088', *extra])
    orphan_export.main()
    return shots


RTSP_A = 'rtsp://115.233.40.137/PLTV/88888913/224/3221229213/53485722.smil'
RTSP_B = 'rtsp://115.233.40.137/PLTV/88888913/224/3221229475/148775850.smil'


def test_orphan_export_单播孤儿也要截图(db, conn, tmp_path, monkeypatch):
    """单播源画面同样要能看(直播裸地址不需要token,已实测)。"""
    add_source(conn, RTSP_A, channel_id=None, source_type='rtsp', available=1)
    calls = []
    _run_export(db, tmp_path, monkeypatch, calls)
    assert len(calls) == 1, f"单播孤儿没被截图(calls={calls})"
    assert calls[0][1] == RTSP_A, f"截图用的URL不对: {calls[0][1]!r}"
    pkg = json.load(open(tmp_path / 'orphans.json', encoding='utf-8'))
    o = pkg['orphans'][0]
    assert len(o['shots']) == 3, f"待识别包里没带上截图: {o['shots']}"


def test_orphan_export_不同单播源截图不得互相覆盖(db, conn, tmp_path, monkeypatch):
    """回归: 前缀取 addr.split(':')[0] 对 rtsp 恒为 'rtsp',10个源共用一套图。"""
    add_source(conn, RTSP_A, channel_id=None, source_type='rtsp', available=1)
    add_source(conn, RTSP_B, channel_id=None, source_type='rtsp', available=1)
    calls = []
    _run_export(db, tmp_path, monkeypatch, calls)
    prefixes = [p for p, _ in calls]
    assert len(set(prefixes)) == 2, f"两个单播源用了相同截图前缀 {prefixes} → 图会互相覆盖"

    pkg = json.load(open(tmp_path / 'orphans.json', encoding='utf-8'))
    by_addr = {o['address']: o['shots'] for o in pkg['orphans']}
    assert by_addr[RTSP_A] and by_addr[RTSP_B]
    assert set(by_addr[RTSP_A]).isdisjoint(by_addr[RTSP_B]), (
        f"两个源指向同一批截图文件: {by_addr[RTSP_A]} vs {by_addr[RTSP_B]}")

    rows = dict(conn.execute("SELECT address, screenshots FROM sources").fetchall())
    assert rows[RTSP_A] != rows[RTSP_B], "库里两个源的 screenshots 相同"


# ============================================================
# 待识别包必须产出到 nginx 已服务的目录(否则页面根本打不开)
#   实测 NAS 上的真实配置:
#     compose: /volume1/docker/iptv-radar/output/dashboard:/usr/share/nginx/html/dashboard:ro
#     nginx:   location /dashboard/ { alias /usr/share/nginx/html/dashboard/; }
#   → 放进 output/dashboard/ 的任何文件都会被 /dashboard/<file> 服务,零配置改动。
#   原来产出在 output/orphan_review/,nginx 访问不到。
# ============================================================

def test_orphan_export_产出目录必须是nginx服务的dashboard目录():
    sys.path.insert(0, SRC)
    import importlib, orphan_export
    importlib.reload(orphan_export)
    rd = orphan_export.REVIEW_DIR.replace(os.sep, '/')
    sd = orphan_export.SHOTS_DIR.replace(os.sep, '/')
    assert rd.endswith('output/dashboard'), (
        f"待识别包产出目录不在 nginx 服务范围内: {rd}")
    assert sd.endswith('output/dashboard/orphan-shots'), (
        f"截图目录不对(会404): {sd}")


def test_orphan_export_截图相对路径要能被页面直接引用(db, conn, tmp_path, monkeypatch):
    """页面和图同在 dashboard/ 下,json 里的 shots 必须是 'orphan-shots/x.jpg',
    否则 <img src> 404(旧值 'shots/x.jpg' 指向已废弃目录)。"""
    add_source(conn, '233.9.5.5:5140', channel_id=None, source_type='multicast', available=1)
    calls = []
    _run_export(db, tmp_path, monkeypatch, calls)
    pkg = json.load(open(tmp_path / 'orphans.json', encoding='utf-8'))
    shots = pkg['orphans'][0]['shots']
    assert shots, "没截图"
    for s in shots:
        assert s.startswith('orphan-shots/'), f"截图相对路径不对(页面会404): {s!r}"


# ============================================================
# 待识别包要带官方名 + 配对关系
#   实测: 26个孤儿里13个官方 channels.json 已给出名字(直播室1-7/好易购1高清/好享购)。
#   它们成为孤儿只是因为库里没这些频道、NAME_OVERRIDES 也没映射 —— 不是认不出来。
#   且同一官方频道的组播+单播成对: 标一个 junk 会连带另一个(见配对连带那条测试),
#   页面必须展示,否则用户不知道一次动了两个源。
# ============================================================

MC_OFFICIAL = '233.50.201.204:5140'
RTSP_OFFICIAL = 'rtsp://115.233.40.137/PLTV/88888913/224/3221229007/1000010000000006_0.smil'


def _write_epg(tmp_path, entries):
    p = tmp_path / 'epg.json'
    json.dump(entries, open(p, 'w', encoding='utf-8'), ensure_ascii=False)
    return str(p)


def test_orphan_export_带上官方频道名(db, conn, tmp_path, monkeypatch):
    """官方列表已给出名字的孤儿,包里必须带 official_name(识别时最有价值的线索)。"""
    add_source(conn, MC_OFFICIAL, channel_id=None, source_type='multicast', available=1)
    epg = _write_epg(tmp_path, [
        {'id': '1', 'name': '直播室4', 'url': f'igmp://{MC_OFFICIAL}|{RTSP_OFFICIAL}'}])
    _run_export(db, tmp_path, monkeypatch, [], extra=('--epg', epg, '--no-shots'))
    pkg = json.load(open(tmp_path / 'orphans.json', encoding='utf-8'))
    o = pkg['orphans'][0]
    assert o.get('official_name') == '直播室4', (
        f"官方名没带上(用户只能靠猜): {o.get('official_name')!r}")


def test_orphan_export_标出成对的孤儿地址(db, conn, tmp_path, monkeypatch):
    """同一官方频道的组播+单播都还是孤儿 → 互相标出,提示一个决定覆盖两个源。"""
    add_source(conn, MC_OFFICIAL, channel_id=None, source_type='multicast', available=1)
    add_source(conn, RTSP_OFFICIAL, channel_id=None, source_type='rtsp', available=1)
    epg = _write_epg(tmp_path, [
        {'id': '1', 'name': '直播室4', 'url': f'igmp://{MC_OFFICIAL}|{RTSP_OFFICIAL}'}])
    _run_export(db, tmp_path, monkeypatch, [], extra=('--epg', epg, '--no-shots'))
    pkg = json.load(open(tmp_path / 'orphans.json', encoding='utf-8'))
    by = {o['address']: o for o in pkg['orphans']}
    assert by[MC_OFFICIAL].get('paired_with') == [RTSP_OFFICIAL], (
        f"组播没标出配对单播: {by[MC_OFFICIAL].get('paired_with')!r}")
    assert by[RTSP_OFFICIAL].get('paired_with') == [MC_OFFICIAL], (
        f"单播没标出配对组播: {by[RTSP_OFFICIAL].get('paired_with')!r}")


def test_orphan_export_官方列表里没有的不得编造名字(db, conn, tmp_path, monkeypatch):
    """13个组播孤儿官方列表里根本没有 —— 必须留空,不能瞎猜(猜错比空白更糟)。"""
    add_source(conn, '233.9.7.7:5140', channel_id=None, source_type='multicast', available=1)
    epg = _write_epg(tmp_path, [
        {'id': '1', 'name': '别的频道', 'url': f'igmp://{MC_OFFICIAL}'}])
    _run_export(db, tmp_path, monkeypatch, [], extra=('--epg', epg, '--no-shots'))
    pkg = json.load(open(tmp_path / 'orphans.json', encoding='utf-8'))
    o = pkg['orphans'][0]
    assert not o.get('official_name'), f"给查不到的源编了名字: {o.get('official_name')!r}"
    assert not o.get('paired_with'), f"编了配对关系: {o.get('paired_with')!r}"


# ============================================================
# orphan-review.html: 识别页面(纯静态,浏览器直接打开)
#   必须自包含 —— 数据内联,不 fetch orphans.json:
#   页面既可能从 nginx 打开,也可能被下载到本地用 file:// 打开,
#   后者 fetch 会被 CORS 拦死(且报错只在控制台,表现为"页面空白"很难查)。
# ============================================================

def _export_with_page(db, tmp_path, monkeypatch, extra=()):
    calls = []
    _run_export(db, tmp_path, monkeypatch, calls, extra=('--no-shots', *extra))
    return (tmp_path / 'orphan-review.html').read_text(encoding='utf-8')


def test_orphan_review页面必须产出到dashboard目录(db, conn, tmp_path, monkeypatch):
    add_source(conn, '233.9.8.1:5140', channel_id=None, source_type='multicast', available=1)
    html_txt = _export_with_page(db, tmp_path, monkeypatch)
    assert html_txt.lstrip().lower().startswith('<!doctype html'), "产出的不是HTML"
    assert '233.9.8.1:5140' in html_txt, "孤儿地址没进页面"


def test_orphan_review页面必须自包含不fetch(db, conn, tmp_path, monkeypatch):
    """file:// 打开时 fetch 会被 CORS 拦死,且只在控制台报错 → 表现为页面空白。"""
    add_source(conn, '233.9.8.2:5140', channel_id=None, source_type='multicast', available=1)
    html_txt = _export_with_page(db, tmp_path, monkeypatch)
    assert 'orphans.json' not in html_txt or 'fetch(' not in html_txt, \
        "页面在运行时抓取 orphans.json → file:// 下会空白"
    assert 'XMLHttpRequest' not in html_txt


def test_orphan_review页面要带可归属频道清单(db, conn, tmp_path, monkeypatch):
    """没有频道清单就没法做 assign(用户只能 junk/skip),等于工具废掉一半。"""
    add_channel(conn, 'CCTV1综合', group='央视')
    add_channel(conn, '浙江钱江都市', group='浙江')
    add_source(conn, '233.9.8.3:5140', channel_id=None, source_type='multicast', available=1)
    html_txt = _export_with_page(db, tmp_path, monkeypatch)
    for k in ('CCTV1综合', '浙江钱江都市'):
        assert k in html_txt, f"频道 {k} 没进页面(无法 assign)"
    assert '央视' in html_txt and '浙江' in html_txt, "分组没带上(新建频道时要选组)"


def test_orphan_review页面展示官方名与配对提示(db, conn, tmp_path, monkeypatch):
    add_source(conn, MC_OFFICIAL, channel_id=None, source_type='multicast', available=1)
    add_source(conn, RTSP_OFFICIAL, channel_id=None, source_type='rtsp', available=1)
    epg = _write_epg(tmp_path, [
        {'id': '1', 'name': '直播室4', 'url': f'igmp://{MC_OFFICIAL}|{RTSP_OFFICIAL}'}])
    html_txt = _export_with_page(db, tmp_path, monkeypatch, extra=('--epg', epg))
    assert '直播室4' in html_txt, "官方名没显示 —— 这是最有价值的识别线索"
    assert RTSP_OFFICIAL in html_txt and MC_OFFICIAL in html_txt, "配对地址没进页面"


def test_orphan_review页面内联数据要转义(db, conn, tmp_path, monkeypatch):
    """频道名里若出现 '</script>' 会直接截断内联脚本、整页失效(静默白屏)。"""
    add_channel(conn, '恶意</script><b>x', group='测试组')
    add_source(conn, '233.9.8.4:5140', channel_id=None, source_type='multicast', available=1)
    html_txt = _export_with_page(db, tmp_path, monkeypatch)
    head = html_txt.split('__ORPHAN_DATA__', 1)[-1]
    assert '</script><b>x' not in head, "内联数据未转义 </script> → 页面会被截断白屏"


# ============================================================
# 组播播放前缀必须能在页面上改(存 localStorage),不能烧死在生成时
#   坑: iina_url 是导出时用 --msd 拼好的。NAS 上 pipeline 传的是容器/内网视角的
#   地址(如 127.0.0.1:4088),而看页面的是 Mac —— 拼出来的链接根本播不了,
#   且页面上没有任何地方能改。dashboard.html 早就有"组播前缀"输入框,键 mcPrefix。
# ============================================================

def test_orphan_review页面可改组播前缀且与dashboard共用键(db, conn, tmp_path, monkeypatch):
    add_source(conn, '233.9.9.5:5140', channel_id=None, source_type='multicast', available=1)
    html_txt = _export_with_page(db, tmp_path, monkeypatch, extra=('--msd', '127.0.0.1:4088'))
    assert 'id="mcPrefix"' in html_txt, "页面没有组播前缀输入框 → 组播链接改不了"
    assert "'mcPrefix'" in html_txt or '"mcPrefix"' in html_txt, \
        "没用 localStorage 键 mcPrefix(与 dashboard.html 共用,用户只需设一次)"


def test_orphan_review页面不得烧死组播播放地址(db, conn, tmp_path, monkeypatch):
    """内联数据里若已含拼好的 iina://...127.0.0.1... 链接,改前缀就没用了。"""
    add_source(conn, '233.9.9.6:5140', channel_id=None, source_type='multicast', available=1)
    html_txt = _export_with_page(db, tmp_path, monkeypatch, extra=('--msd', '127.0.0.1:4088'))
    data = json.loads(html_txt.split('__ORPHAN_DATA__', 1)[1].split('</script>', 1)[0])
    o = data['orphans'][0]
    # 每条源都不能带按导出时 msd 拼好的链接 —— 那样改前缀就没用了
    assert 'iina_url' not in o and 'play_url' not in o, \
        f"每条源里仍带着拼死的播放链接(改前缀无效): {sorted(o)}"
    # msd_prefix 只作输入框默认值保留(pipeline 传的是 .env 里的真实网关,对局域网有效)
    assert data.get('msd_prefix'), "没给出默认前缀(用户第一次打开无从下手)"


def test_orphan_export_json仍保留play_url与iina_url(db, conn, tmp_path, monkeypatch):
    """契约(ORPHAN_REVIEW §3.1)里有这两个字段,别为了页面把 json 契约破了。"""
    add_source(conn, '233.9.9.7:5140', channel_id=None, source_type='multicast', available=1)
    _export_with_page(db, tmp_path, monkeypatch, extra=('--msd', 'H:4088'))
    o = json.load(open(tmp_path / 'orphans.json', encoding='utf-8'))['orphans'][0]
    assert o['play_url'] == 'http://H:4088/rtp/233.9.9.7:5140'
    assert o['iina_url'].startswith('iina://weblink?url=')


def test_shot_prefix_单播前缀要能肉眼区分():
    """实拍后发现: 3 个直播室的文件名前 24 字符完全相同(只有 md5 尾不同),
    翻目录时根本分不出谁是谁。频道标识段(倒数第二段,如 3221229007)必须进文件名。"""
    sys.path.insert(0, SRC)
    from orphan_export import shot_prefix
    a = 'rtsp://h/PLTV/88888913/224/3221229007/10000100000000060000000004308260_0.smil'
    b = 'rtsp://h/PLTV/88888913/224/3221229011/10000100000000060000000004308262_0.smil'
    pa, pb = shot_prefix('rtsp', a), shot_prefix('rtsp', b)
    assert '3221229007' in pa, f"频道标识段没进前缀: {pa}"
    assert '3221229011' in pb, f"频道标识段没进前缀: {pb}"
    # 去掉各自的 md5 尾后仍必须不同(即可读部分本身就能区分)
    assert pa.rsplit('_', 1)[0] != pb.rsplit('_', 1)[0], \
        f"可读部分相同,只靠哈希区分: {pa} vs {pb}"
