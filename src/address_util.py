#!/usr/bin/env python3
"""地址形态工具: 官方 channels.json 的 URL ↔ 库里 sources.address 的身份规则。

为什么要单独成一个模块(而不是各脚本自己写正则):
  `rtsp://...\\.smil` 这条正则原本在 5 个文件里抄了 8 遍,横跨采集/清洗/生成三层。
  它**就是**"官方台账里的一条 URL 对应库里哪一行 sources"的判定依据,
  进而决定 token(timeshift_query)、回看天数、优选源能否对上号。
  运营商哪天改了 URL 形态(换后缀、加路径层级),只改了一部分调用处的话,
  后果是最糟的一类: 一部分脚本按新形态规范化、一部分按旧的 →
  同一个源分裂成重复/孤儿行,timeshift_query 写不到目标上 ——
  全程零报错,只有播放列表慢慢烂掉。所以规则必须只有一处。
"""
import re

# 单播地址的规范形态: 到 .smil 为止(token/参数都在 ? 后面,不进 address)
_RTSP_CANON = re.compile(r'(rtsp://[^?]+\.smil)')
# 组播地址: 官方台账写成 igmp://a.b.c.d:port,库里存裸 a.b.c.d:port
_MCAST = re.compile(r'^(?:igmp|rtp)://(\d{1,3}(?:\.\d{1,3}){3}:\d+)$')
_MCAST_BARE = re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}:\d+$')


def canonical_rtsp(url):
    """单播 URL → 规范地址(到 .smil,去掉 token/参数)。

    匹配不上时退化成"去掉 ? 后面"(与改造前 6 处调用点的行为一致):
    运营商换了后缀也不至于直接崩,仍是个可用的近似。
    需要"匹配不上就别猜"的场合用 canonical_rtsp_strict。
    """
    if not url:
        return ''
    m = _RTSP_CANON.match(url)
    return m.group(1) if m else url.split('?')[0]


def canonical_rtsp_strict(url):
    """同上,但匹配不上返回 None(不猜)。

    用于"这个地址要当键去写库/查表"的场合 —— 拿一个猜出来的地址当键,
    会把 token 写到错误的源上,比写不进去更糟。
    """
    if not url:
        return None
    m = _RTSP_CANON.match(url)
    return m.group(1) if m else None


def split_query(url):
    """URL → (规范地址, query)。query 不含 '?';无参数时为 ''。"""
    if not url:
        return '', ''
    addr, sep, q = url.partition('?')
    m = _RTSP_CANON.match(url)
    return (m.group(1) if m else addr), q


def multicast_addr(url):
    """组播 URL → 库里的裸地址 'a.b.c.d:port';不是组播则 None。"""
    if not url:
        return None
    m = _MCAST.match(url)
    if m:
        return m.group(1)
    return url if _MCAST_BARE.match(url) else None


def parse_official_url(url):
    """官方台账的 url 字段 → 该频道涉及的全部库内地址(保序)。

    官方形态是竖线分隔的多个地址,例如:
        igmp://233.50.201.118:5140|rtsp://.../xxx.smil?<token>
    """
    if not url:
        return []
    out = []
    for part in url.split('|'):
        part = part.strip()
        if not part:
            continue
        mc = multicast_addr(part)
        if mc:
            out.append(mc)
        elif part.startswith('rtsp://'):
            out.append(canonical_rtsp(part))
    return out
