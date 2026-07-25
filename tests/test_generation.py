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

def test_epg_json_不得逃出script标签(tmp_path):
    """第三方EPG(112114.xyz)的节目名若含 </script>, 会闭合script块执行任意JS。
    json.dumps 不转义 </script>, 必须额外处理。"""
    sys.path.insert(0, SRC)
    import gen_dashboard

    payload = {'CCTV1': [{'title': '</script><script>alert(1)</script>', 'start': '20:00'}]}
    rendered = gen_dashboard.json_for_script(payload)
    assert '</script>' not in rendered, f"EPG数据可逃出script标签: {rendered}"
    # 仍必须是合法JSON(前端 JSON.parse / var= 都要能用)
    assert json.loads(rendered.replace('\\u003c', '<').replace('\\u003e', '>'))


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


def test_m3u生成是原子的(db, conn, tmp_path):
    """写入过程中不得留下 .tmp 残留;最终文件应完整。"""
    cid = add_channel(conn, '频道A')
    sid = add_source(conn, '233.6.6.6:5140', channel_id=cid, channel_key='频道A')
    set_preferred(conn, cid, sid)
    out = str(tmp_path / 'a.m3u')
    run_script('gen_m3u.py', '--db', db, '--out', out, '--msd', 'H:4088')
    assert os.path.exists(out)
    assert not os.path.exists(out + '.tmp'), "遗留 .tmp 文件"
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
