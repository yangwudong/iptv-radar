"""address_util 的契约测试。

这个模块承载的是"官方 channels.json 的 URL ↔ 库里 sources.address"的**身份规则**。
它决定了 token(timeshift_query)、回看天数、优选源能否对上号。
如果运营商改了 URL 形态而只改了一部分调用处,后果是最糟的一类:
地址分裂成重复/孤儿行、timeshift_query 写不到目标上,全程无报错,只有播放列表慢慢烂掉。
所以这里把规则钉死。
"""
import sys

import pytest

from conftest import SRC

sys.path.insert(0, SRC)
import address_util as au


REAL = ('rtsp://115.233.40.137/PLTV/88888913/224/3221228078/'
        '10000100000000060000000002460690_0.smil')
REAL_Q = 'rrsip=220.191.136.24&zoneoffset=480&accountinfo=%2C123%2C&it=TOKEN'


class Test规范化:
    def test_去掉query只留smil路径(self):
        assert au.canonical_rtsp(f'{REAL}?{REAL_Q}') == REAL

    def test_已经是干净地址时原样返回(self):
        assert au.canonical_rtsp(REAL) == REAL

    def test_带playseek的回看地址也能规范化(self):
        u = f'{REAL}?{REAL_Q}&playseek=20260724190000-20260724193000'
        assert au.canonical_rtsp(u) == REAL

    def test_非smil地址回退到去query(self):
        """运营商若换了后缀,不能直接崩 —— 退化成"去掉?后面"仍是可用的近似。
        6处调用点原本就是这个 fallback,抽函数必须保持。"""
        assert au.canonical_rtsp('rtsp://h/a/b.m3u8?x=1') == 'rtsp://h/a/b.m3u8'
        assert au.canonical_rtsp('rtsp://h/plain') == 'rtsp://h/plain'

    def test_空值不炸(self):
        assert au.canonical_rtsp('') == ''
        assert au.canonical_rtsp(None) == ''


class Test严格模式:
    """link_sources 写 timeshift_query、gen_channels_page 查扫描结果时用严格版:
    匹配不上就跳过,不能拿一个"猜的"地址去当键 —— 那会把 token 写到错的源上。"""

    def test_匹配成功返回路径(self):
        assert au.canonical_rtsp_strict(f'{REAL}?{REAL_Q}') == REAL

    def test_匹配失败返回None而不是猜(self):
        assert au.canonical_rtsp_strict('rtsp://h/a/b.m3u8?x=1') is None
        assert au.canonical_rtsp_strict('') is None


class Test拆分query:
    def test_取出query部分(self):
        assert au.split_query(f'{REAL}?{REAL_Q}') == (REAL, REAL_Q)

    def test_无query时query为空(self):
        assert au.split_query(REAL) == (REAL, '')

    def test_query里含问号也只按第一个切(self):
        u = f'{REAL}?a=1&b=x?y'
        assert au.split_query(u) == (REAL, 'a=1&b=x?y')


class Test组播地址:
    def test_从igmp形式提取(self):
        assert au.multicast_addr('igmp://233.50.201.118:5140') == '233.50.201.118:5140'

    def test_从rtp形式提取(self):
        assert au.multicast_addr('rtp://233.50.201.118:5140') == '233.50.201.118:5140'

    def test_裸地址原样(self):
        assert au.multicast_addr('233.50.201.118:5140') == '233.50.201.118:5140'

    def test_非组播返回None(self):
        assert au.multicast_addr('rtsp://h/a.smil') is None
        assert au.multicast_addr('') is None


class Test官方url拆多地址:
    def test_竖线分隔的组播加单播(self):
        u = f'igmp://233.50.201.118:5140|{REAL}?{REAL_Q}'
        assert au.parse_official_url(u) == ['233.50.201.118:5140', REAL]

    def test_只有组播(self):
        assert au.parse_official_url('igmp://233.50.200.99:5140') == ['233.50.200.99:5140']

    def test_只有单播(self):
        assert au.parse_official_url(f'{REAL}?{REAL_Q}') == [REAL]

    def test_空值返回空列表(self):
        assert au.parse_official_url('') == []
        assert au.parse_official_url(None) == []


def test_与现有库数据完全吻合():
    """回归护栏: 用生产库里的真实地址跑一遍,规范化必须是幂等的
    (库里存的就是规范形式,再规范一次不能变) —— 否则地址会分裂成重复行。"""
    import os
    import sqlite3
    db = os.path.join(SRC, '..', 'data', 'iptv.db')
    if not os.path.exists(db):
        pytest.skip('无生产库,跳过')
    conn = sqlite3.connect(db)
    rows = [r[0] for r in conn.execute(
        "SELECT address FROM sources WHERE source_type='rtsp'")]
    conn.close()
    assert rows, '库里没有rtsp源,护栏无意义'
    bad = [a for a in rows if au.canonical_rtsp(a) != a]
    assert not bad, f"{len(bad)} 个库内地址规范化后会变(会造成地址分裂): {bad[:3]}"
