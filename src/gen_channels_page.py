#!/usr/bin/env python3
"""
iptv-radar: gen_channels_page.py
生成"电信官方频道列表"子页面(channels.html)。
数据来自官方 getchannellist 的解析结果(channels.json),展示原始台账:
  频道ID / 频道名 / 组播地址 / 单播(RTSP)地址 / 时移地址

与主Dashboard(index.html,扫描检测信息)区分: 这个是官方原始频道台账。
运行: python3 gen_channels_page.py [--json channels.json] [--out output/dashboard/channels.html]
"""
import json
import os
import re
import argparse
import datetime
import html
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from template_util import render_template

RADAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
# channels.json 优先用项目reference,回退老目录
DEFAULT_JSON = os.path.join(RADAR, 'reference', 'channels.sample.json')
DEFAULT_OUT = os.path.join(RADAR, 'output', 'dashboard', 'channels.html')


def esc(s):
    return html.escape(str(s or ''))


def split_url(url):
    """url字段: igmp://...|rtsp://...  拆成组播/单播"""
    mc, uc = '', ''
    for part in (url or '').split('|'):
        part = part.strip()
        if part.startswith('igmp://') or part.startswith('rtp://'):
            mc = part.replace('igmp://', '').replace('rtp://', '')
        elif part.startswith('rtsp://'):
            uc = part
    return mc, uc


def load_scan_info(db_path):
    """从数据库读所有源的扫描信息, 按地址索引。返回 {address: {...}}"""
    info = {}
    if not db_path or not os.path.exists(db_path):
        return info
    import sqlite3
    c = sqlite3.connect(db_path, timeout=30)
    # 兼容: 旧库可能无 playback_days 字段
    cols = [r[1] for r in c.execute("PRAGMA table_info(sources)").fetchall()]
    has_pb = 'playback_days' in cols
    pb_sel = ', playback_days' if has_pb else ''
    for r in c.execute(f"""SELECT address, resolution, res_label, video_codec, fps, hdr,
                          audio_codec, audio_channels, available{pb_sel} FROM sources"""):
        info[r[0]] = {'res': r[1], 'res_label': r[2], 'codec': r[3], 'fps': r[4],
                      'hdr': r[5], 'acodec': r[6], 'ach': r[7], 'avail': r[8],
                      'playback_days': r[9] if has_pb else None}
    c.close()
    return info


_CN = {'一': '1', '二': '2', '三': '3', '四': '4', '五': '5', '六': '6', '七': '7',
       '八': '8', '九': '9', '十': '10', '十一': '11', '十二': '12', '十三': '13',
       '十四': '14', '十五': '15', '十六': '16', '十七': '17'}

def _canon(name):
    """官方名→规范名(中央N套→CCTVN)"""
    m = re.match(r'中央(.+?)套', name)
    if m and m.group(1) in _CN:
        return 'CCTV' + _CN[m.group(1)]
    return re.sub(r'(高清|HD|标清|SD| )', '', name)


def load_logos(db_path):
    """返回(按名字索引, 按地址索引)。
    按地址索引走 channel_id: sources.channel_id → channels.tvg_logo (治本,任意源地址可查)"""
    by_name, by_addr = {}, {}
    if not db_path or not os.path.exists(db_path):
        return by_name, by_addr
    import sqlite3
    c = sqlite3.connect(db_path, timeout=30)
    for name, logo in c.execute("SELECT name,tvg_logo FROM channels WHERE tvg_logo!='' AND tvg_logo IS NOT NULL"):
        by_name[name] = logo
        by_name[re.sub(r'(高清|HD|标清|SD| )', '', name)] = logo
        m = re.match(r'(CCTV\d+\+?)', name)
        if m:
            by_name[m.group(1)] = logo
    # 地址→channel_id→台标(整数外键关联,稳定;任意已归并源可查)
    for addr, logo in c.execute("""SELECT s.address, ch.tvg_logo FROM sources s
            JOIN channels ch ON s.channel_id=ch.channel_id
            WHERE ch.tvg_logo!='' AND ch.tvg_logo IS NOT NULL"""):
        by_addr[addr] = logo
    c.close()
    return by_name, by_addr


def find_logo(name, mc, by_name, by_addr):
    """先按名字匹配,不行再按组播地址精确匹配(补漏)"""
    logo = (by_name.get(name) or by_name.get(_canon(name))
            or by_name.get(re.sub(r'(高清|HD|标清|SD| )', '', name)))
    if not logo and mc:
        logo = by_addr.get(mc)   # 组播地址精确匹配兜底
    return logo or ''


def codec_label(c):
    return {'h264': 'H.264', 'hevc': 'H.265', 'avc': 'H.264'}.get((c or '').lower(), (c or '').upper())


def audio_label(codec, ch):
    if not codec:
        return ''
    chn = {1: '单声道', 2: '立体声', 6: '5.1', 8: '7.1'}.get(ch, f'{ch}ch' if ch else '')
    return f'{codec.upper()} {chn}' if chn else codec.upper()


def video_tags(sc):
    """扫描信息 → 视频tag HTML"""
    if not sc:
        return '<span class="muted">未扫描</span>'
    if not sc.get('res'):
        return '<span class="muted">-</span>'
    h = f'<span class="tag {res_cls(sc["res_label"])}">{esc(sc["res"])}</span>'
    if sc.get('codec'):
        h += f'<span class="tag t-codec">{esc(codec_label(sc["codec"]))}</span>'
    if sc.get('fps'):
        h += f'<span class="tag t-fps">{int(sc["fps"])}fps</span>'
    if sc.get('hdr') and sc['hdr'] != 'SDR':
        h += f'<span class="tag t-hdr">{esc(sc["hdr"])}</span>'
    return h


def audio_tag(sc):
    if not sc or not sc.get('acodec'):
        return '<span class="muted">-</span>'
    return f'<span class="tag t-audio">{esc(audio_label(sc["acodec"], sc["ach"]))}</span>'


def res_cls(label):
    return {'4K': 't-4k', '1080P': 't-hd', '720P': 't-sd', 'SD': 't-sd'}.get(label, 't-unknown')


def build(json_path, db_path=None):
    data = json.load(open(json_path, encoding='utf-8'))
    scan = load_scan_info(db_path)
    logo_by_name, logo_by_addr = load_logos(db_path)
    gen_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    import urllib.parse
    rows = []
    for ch in data:
        name = ch.get('name', '')
        cid = ch.get('id', '')
        mc, uc = split_url(ch.get('url', ''))
        ts = ch.get('timeshift_url', '')
        # 回看天数(源级,查单播地址的playback_days): N天/不支持/未探测
        pb_days = None
        if uc:
            _m = re.match(r'(rtsp://[^?]+\.smil)', uc)
            _smil = _m.group(1) if _m else uc.split('?')[0]
            _sc = scan.get(_smil)
            if _sc:
                pb_days = _sc.get('playback_days')
        if pb_days and pb_days > 0:
            ts_badge = f'<span class="tag t-ts">{pb_days}天回看</span>'
        elif pb_days == 0:
            ts_badge = '<span class="muted" title="不支持回看(版权等)">✕</span>'
        elif ts:
            ts_badge = '<span class="tag t-ts">支持</span>'
        else:
            ts_badge = '<span class="muted">-</span>'
        mc_html = f'<code class="mc">{esc(mc)}</code>' if mc else '<span class="muted">-</span>'
        # 单播地址: 完整简化版(到.smil,去token),双击复制;单独用"播放"列调IINA
        if uc:
            simple = re.match(r'(rtsp://[^?]+\.smil)', uc)
            simple = simple.group(1) if simple else uc.split('?')[0]
            iina_url = 'iina://weblink?url=' + urllib.parse.quote(simple, safe='')
            uc_html = f'<code class="uc" title="双击复制" data-full="{esc(simple)}" ondblclick="copyUrl(this)">{esc(simple)}</code>'
            play_html = f'<a class="play-btn" href="{esc(iina_url)}" title="用IINA播放">▶ IINA</a>'
        else:
            uc_html = '<span class="muted">-</span>'
            play_html = '<span class="muted">-</span>'
        # 台标
        logo = find_logo(name, mc, logo_by_name, logo_by_addr)
        logo_html = (f'<div class="logo-box"><img src="{esc(logo)}" onerror="this.parentNode.classList.add(\'noimg\')" loading="lazy"></div>'
                     if logo else '<div class="logo-box noimg"></div>')
        # 扫描信息(视频/音频tag)
        sc = scan.get(mc)
        if not sc and uc:
            m = re.match(r'(rtsp://[^?]+\.smil)', uc)
            if m:
                sc = scan.get(m.group(1))
        rows.append({
            'name': esc(name), 'id': esc(cid),
            'logo_html': logo_html, 'video_tags': video_tags(sc), 'audio_tag': audio_tag(sc),
            'mc_html': mc_html, 'uc_html': uc_html, 'play_html': play_html, 'ts_badge': ts_badge,
        })
    n_mc = sum(1 for ch in data if 'igmp://' in ch.get('url', '') or 'rtp://' in ch.get('url', ''))
    n_uc = sum(1 for ch in data if 'rtsp://' in ch.get('url', ''))
    n_ts = sum(1 for ch in data if ch.get('timeshift_url'))
    return render_template('channels.html', gen_time=gen_time, total=len(data),
                           n_mc=n_mc, n_uc=n_uc, n_ts=n_ts, rows=rows)




if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', default=DEFAULT_JSON)
    ap.add_argument('--out', default=DEFAULT_OUT)
    ap.add_argument('--db', default=os.path.join(RADAR, 'data', 'iptv.db'),
                    help='扫描数据库(取视频/音频信息)')
    args = ap.parse_args()
    print("=" * 50)
    print("  iptv-radar 生成官方频道列表页")
    print("=" * 50)
    if not os.path.exists(args.json):
        print(f"  错误: 找不到 {args.json}")
        raise SystemExit(1)
    doc = build(args.json, args.db)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as f:
        f.write(doc)
    print(f"  输出: {args.out} ({len(doc)} 字节)")
    print("完成")
