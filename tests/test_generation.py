"""生成层(m3u / Dashboard)的注入与健壮性测试。"""
import json
import os
import subprocess
import sys

import pytest

from conftest import SRC, add_channel, add_source, set_preferred, run_script


# ============================================================
# XSS: 第三方EPG节目名未转义直接塞进 <script> 块
# ============================================================

def test_epg注入不得逃出script块_端到端(db, conn):
    """真正的注入点是 build_html→模板的 `var EPG = {{ epg_json | safe }}`。
    只测 helper 的话,把 build_html 里改回裸 json.dumps 也照样通过 —— 那是假通过。
    """
    sys.path.insert(0, SRC)
    import json as _json
    import re as _re
    import gen_dashboard
    cid = add_channel(conn, '测试台')
    conn.execute("UPDATE channels SET tvg_id='T1' WHERE channel_id=?", (cid,))
    conn.commit()
    set_preferred(conn, cid, add_source(conn, '233.1.1.1:5140',
                                        channel_id=cid, channel_key='测试台'))
    payload = {'T1': [{'title': '</script><script>alert(1)</script>', 'start': '20:00'}]}
    doc = gen_dashboard.build_html(gen_dashboard.load_data(db), payload)

    m = _re.search(r'var EPG = (.*?);\n', doc, _re.S)
    assert m, "模板里找不到 var EPG 赋值,测试需要跟进模板改动"
    embedded = m.group(1)
    assert '</script>' not in embedded, f"EPG数据可逃出script块: {embedded[:120]}"
    assert _json.loads(embedded) == payload, "转义破坏了数据(前端 JSON.parse 会拿到错内容)"


def test_epg_json_不得逃出script标签(tmp_path):
    """第三方EPG(112114.xyz)的节目名若含 </script>, 会闭合script块执行任意JS。
    json.dumps 不转义 </script>, 必须额外处理。"""
    sys.path.insert(0, SRC)
    import gen_dashboard

    payload = {'CCTV1': [{'title': '</script><script>alert(1)</script>', 'start': '20:00'}]}
    rendered = gen_dashboard.json_for_script(payload)
    assert '</script>' not in rendered, f"EPG数据可逃出script标签: {rendered}"
    # 仍必须是合法JSON且内容无损(原来只断言"非空dict",丢字段也能过)
    assert json.loads(rendered) == payload


def test_m3u_属性含双引号不得破坏格式(db, conn, tmp_path):
    """频道名/台标URL含双引号时,不能生成畸形 EXTINF(属性提前闭合,播放器解析错乱)。

    坏例(修复前实际输出): #EXTINF:-1 tvg-id="ID"X" tvg-logo="http://x/a".png" ...
    """
    import re
    cid = add_channel(conn, '恶意"频道')
    conn.execute("UPDATE channels SET tvg_logo=?, tvg_id=? WHERE channel_id=?",
                 ('http://x/a".png', 'ID"X', cid))
    conn.commit()
    sid = add_source(conn, '233.7.7.7:5140', channel_id=cid, channel_key='恶意"频道')
    set_preferred(conn, cid, sid)

    out = str(tmp_path / 'q.m3u')
    run_script('gen_m3u.py', '--db', db, '--out', out, '--msd', 'H:4088')
    extinf = [l for l in open(out, encoding='utf-8') if l.startswith('#EXTINF')][0]

    # 属性区(逗号前) 必须能被 key="value" 完整消费,不留残渣 —— 这才是格式合法的定义
    attr_region = extinf[len('#EXTINF:-1'):].rsplit(',', 1)[0]
    leftover = re.sub(r'[\w-]+="[^"]*"', '', attr_region).strip()
    assert leftover == '', (
        f"EXTINF属性区畸形(残渣={leftover!r}):\n  {extinf.strip()}")


def test_无可用主源的频道应有日志(db, conn, tmp_path, capfd):
    """enabled频道没有可解析主源时被静默跳过,运维无从发现频道消失。"""
    add_channel(conn, '没源的频道')          # 无源 → 不会出现在m3u
    ok = add_channel(conn, '有源的频道', order=2)
    sid = add_source(conn, '233.8.8.8:5140', channel_id=ok, channel_key='有源的频道')
    set_preferred(conn, ok, sid)

    out = str(tmp_path / 'w.m3u')
    r = run_script('gen_m3u.py', '--db', db, '--out', out, '--msd', 'H:4088')
    assert '没源的频道' in r.stdout, f"跳过的频道未记录日志:\n{r.stdout}"


def test_m3u生成失败时不得破坏已发布的播放列表(db, conn, tmp_path, monkeypatch):
    """原子写的真正含义: 写入中途失败时,上一次正常发布的文件必须完好无损。

    (原来的写法只断言"没有 .tmp 残留" —— 把代码改回朴素的 open().write() 也照样通过,
     因为朴素写法根本不产生 .tmp。那是个假通过的测试。)
    """
    sys.path.insert(0, SRC)
    import gen_m3u
    cid = add_channel(conn, '频道A')
    set_preferred(conn, cid, add_source(conn, '233.6.6.6:5140',
                                        channel_id=cid, channel_key='频道A'))
    out = tmp_path / 'a.m3u'
    out.write_text('#EXTM3U\n#EXTINF:-1,上一次正常发布的内容\nhttp://old\n', encoding='utf-8')

    def boom(*a, **k):
        raise OSError('disk full')
    monkeypatch.setattr(gen_m3u.os, 'replace', boom)
    with pytest.raises(OSError):
        gen_m3u.generate(db, str(out), 'H:4088')
    assert '上一次正常发布的内容' in out.read_text(encoding='utf-8'), \
        "非原子写: 生成失败后旧播放列表已被截断/覆盖"


def test_m3u生成成功后不留临时文件(db, conn, tmp_path):
    cid = add_channel(conn, '频道A')
    set_preferred(conn, cid, add_source(conn, '233.6.6.6:5140',
                                        channel_id=cid, channel_key='频道A'))
    out = str(tmp_path / 'a.m3u')
    run_script('gen_m3u.py', '--db', db, '--out', out, '--msd', 'H:4088')
    assert os.path.exists(out) and not os.path.exists(out + '.tmp')
    assert open(out, encoding='utf-8').read().startswith('#EXTM3U')


# ============================================================
# 排序一致性: m3u 与 Dashboard 必须同序
# ============================================================

def test_dashboard与m3u排序一致(db, conn, tmp_path):
    """新频道(orphan_import 建的)只有 order_in_group 没有 sort_hint,
    Dashboard 若按 sort_hint 排会把它甩到最后,与 m3u(按 order_in_group)顺序不一致。

    构造让两种排序必然不同: 组内位置 a=1, 新频道=2, b=3;
    而 sort_hint 是 a=1, b=2, 新频道=NULL(→9999 排最后)。
      按 order_in_group: a, 新频道, b
      按 sort_hint:      a, b, 新频道   ← 不一致
    """
    sys.path.insert(0, SRC)
    a = add_channel(conn, 'AA', group='央视', order=1, sort_hint=1)
    b = add_channel(conn, 'BB', group='央视', order=3, sort_hint=2)
    cnew = add_channel(conn, 'CC新频道', group='央视', order=2, sort_hint=None)
    for i, cid in enumerate((a, b, cnew)):
        sid = add_source(conn, f'233.5.5.{i}:5140', channel_id=cid)
        conn.execute("""UPDATE sources SET channel_key=
                        (SELECT channel_key FROM channels WHERE channel_id=?)
                        WHERE source_id=?""", (cid, sid))
        set_preferred(conn, cid, sid)
    conn.commit()

    out = str(tmp_path / 'o.m3u')
    run_script('gen_m3u.py', '--db', db, '--out', out, '--msd', 'H:4088')
    m3u_order = [l.rsplit(',', 1)[1].strip() for l in open(out, encoding='utf-8')
                 if l.startswith('#EXTINF')]
    assert m3u_order == ['AA', 'CC新频道', 'BB'], f"m3u顺序前提不成立: {m3u_order}"

    import importlib
    import gen_dashboard
    importlib.reload(gen_dashboard)
    dash_order = [d['name'] for d in gen_dashboard.load_data(db)]

    assert m3u_order == dash_order, (
        f"顺序不一致:\n  m3u  ={m3u_order}\n  dash ={dash_order}")


# ============================================================
# 项目5: 分组只有一份真相(channel_groups 表)
#   原来 channels.group_primary/group_extra 又存了一遍,而 m3u 读表、Dashboard 显示读列
#   → 只改一处就会两边不一致(已实证: 往表里加"少儿"后 m3u 出现在少儿组,
#     Dashboard 标签却仍只显示"卫视+浙江")
# ============================================================

def test_p5_加分组后m3u与dashboard必须同时生效(db, conn, tmp_path):
    """只往 channel_groups 加一行(不碰 channels 的列),两边都要体现。"""
    sys.path.insert(0, SRC)
    import importlib
    import gen_dashboard
    importlib.reload(gen_dashboard)

    cid = add_channel(conn, '浙江卫视', group='卫视', order=1)
    set_preferred(conn, cid, add_source(conn, '233.4.1.1:5140',
                                        channel_id=cid, channel_key='浙江卫视'))
    # 追加一个附加组(AGENTS.md 说的"有意的重复分组")
    conn.execute("""INSERT INTO channel_groups(channel_id,group_name,is_primary,order_in_group)
                    VALUES(?,'少儿',0,9)""", (cid,))
    conn.commit()

    out = str(tmp_path / 'g.m3u')
    run_script('gen_m3u.py', '--db', db, '--out', out, '--msd', 'H:4088')
    text = open(out, encoding='utf-8').read()
    assert 'group-title="卫视",浙江卫视' in text
    assert 'group-title="少儿",浙江卫视' in text, "m3u 未按 channel_groups 输出到少儿组"

    d = [x for x in gen_dashboard.load_data(db) if x['name'] == '浙江卫视'][0]
    groups = {d['group']} | {g for g in d['group_extra'].split(';') if g}
    assert groups == {'卫视', '少儿'}, (
        f"Dashboard 分组与 channel_groups 不一致(m3u有少儿,页面没有): {groups}")


def test_p5_主组由is_primary决定(db, conn):
    sys.path.insert(0, SRC)
    import importlib
    import gen_dashboard
    importlib.reload(gen_dashboard)
    cid = add_channel(conn, 'BesTV少儿4K', group='4K超高清', order=0)   # is_primary=1
    set_preferred(conn, cid, add_source(conn, '233.4.2.2:5140',
                                        channel_id=cid, channel_key='BesTV少儿4K'))
    for g, o in (('少儿', 0), ('BesTV', 2)):
        conn.execute("""INSERT INTO channel_groups(channel_id,group_name,is_primary,order_in_group)
                        VALUES(?,?,0,?)""", (cid, g, o))
    conn.commit()
    d = [x for x in gen_dashboard.load_data(db) if x['name'] == 'BesTV少儿4K'][0]
    assert d['group'] == '4K超高清', f"主组应取 is_primary=1 的那行,实际 {d['group']}"
    assert [x for x in d['group_extra'].split(';') if x] == ['少儿', 'BesTV'], \
        f"附加组应按 order_in_group 排序,实际 {d['group_extra']}"


def test_p5_channels表不该再有分组列(db, conn):
    """删列后 schema 里不能残留(否则又会有人去写它,双真相回归)。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(channels)")}
    assert 'group_primary' not in cols, "group_primary 仍在,分组仍是双真相"
    assert 'group_extra' not in cols, "group_extra 仍在,分组仍是双真相"


# ============================================================
# 购物分组: 排在 其他 之后、广播 之前
#   缘由: 购物台不想混在"其他"里,但也不该排到广播后面(广播固定收尾)。
#   V2 没有旧 legacy 的 BLACKLIST_NAMES 名字黑名单(那只在 SPEC.md 里描述 legacy 行为),
#   购物台照常输出,只是单独成组便于播放器里整组跳过。
# ============================================================

def test_购物分组排在其他之后广播之前():
    sys.path.insert(0, SRC)
    from gen_m3u import GROUP_ORDER
    for g in ('其他', '购物', '广播'):
        assert g in GROUP_ORDER, f"GROUP_ORDER 里没有 {g}"
    assert GROUP_ORDER.index('其他') < GROUP_ORDER.index('购物') < GROUP_ORDER.index('广播'), \
        f"购物组位置不对: {GROUP_ORDER}"


def test_购物分组在m3u里的实际位置(db, conn, tmp_path):
    """光改常量不够 —— 要在真实产出的 m3u 里验证组的先后。"""
    for i, (key, grp) in enumerate((('某其他台', '其他'), ('某购物台', '购物'), ('某广播', '广播'))):
        cid = add_channel(conn, key, group=grp, order=1)
        sid = add_source(conn, f'233.8.8.{i + 1}:5140', channel_id=cid, channel_key=key)
        set_preferred(conn, cid, sid)
    out = str(tmp_path / 'g.m3u')
    run_script('gen_m3u.py', '--db', db, '--out', out, '--msd', 'H:4088')
    text = open(out, encoding='utf-8').read()
    pos = {g: text.index(f'group-title="{g}"') for g in ('其他', '购物', '广播')}
    assert pos['其他'] < pos['购物'] < pos['广播'], f"m3u 里分组顺序不对: {pos}"
