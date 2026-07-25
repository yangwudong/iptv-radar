#!/usr/bin/env python3
"""
iptv-radar 生成层: gen_dashboard.py (v2 精致版)
从 SQLite 读数据,生成静态HTML Dashboard。

设计:
  - 融合 myepg 块状tag布局 + Apple克制风格(SF Pro/Action Blue)
  - 浅色/深色模式(跟随系统 + 手动切换,localStorage记忆)
  - 清晰度分级排序(4K>1080P>720P>SD>广播>未知)
  - 可点击TAG过滤(全部/4K/1080P/标清/广播 + 音频/HDR)
  - 台标放大+背景(央视等浅色台标可见)
  - 频道ID列 + 官方EPG节目单链接
  - 截图缩略图点击放大

运行: python3 gen_dashboard.py [--db] [--out] [--epg-server HOST:PORT]
"""
import sqlite3
import os
import re
import address_util
import argparse
import datetime
import html
import json
import sys

RADAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from template_util import render_template
# 分组顺序从 gen_m3u 复用同一份定义: 两处各写一份必然漂移(m3u 与 Dashboard 顺序会不一致)
from gen_m3u import GROUP_ORDER
DEFAULT_DB = os.path.join(RADAR, 'data', 'iptv.db')
DEFAULT_OUT = os.path.join(RADAR, 'output', 'dashboard', 'index.html')
EPG_SERVER = os.environ.get('EPG_SERVER', '115.233.40.140:33200')

# 清晰度排序权重(4K最前)
RES_RANK = {'4K': 0, '1080P': 1, '720P': 2, 'SD': 3, '广播': 4, '': 5}
# 频道分类: 广播只看分组;有视频按分辨率;无分辨率且非广播组=未知(可能扫描失败)
def res_category(res_label, group):
    if group == '广播':
        return '广播'
    if res_label == '4K':
        return '4K'
    if res_label == '1080P':
        return '1080P'
    if res_label in ('720P', 'SD'):
        return '标清'
    return '未知'   # 无分辨率但不是广播组 → 待确认(扫描失败/新频道)


def esc(s):
    return html.escape(str(s or ''))


def json_for_script(obj):
    r"""把数据序列化成可安全嵌进 <script> 块的 JSON。

    json.dumps 不转义 "</script>" —— EPG 节目名来自第三方源(epg.112114.xyz)且未做清洗,
    一旦某个节目名含 "</script><script>...", 生成的静态页会提前闭合script块并执行注入的JS
    (Dashboard 若公开分享即为真实攻击面)。把 < > & 转成 \uXXXX: 既杜绝闭合,又仍是合法JSON。
    """
    raw = json.dumps(obj, ensure_ascii=False, separators=(',', ':'))
    return (raw.replace('<', r'\u003c')
               .replace('>', r'\u003e')
               .replace('&', r'\u0026'))


def load_data(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    rows = c.execute("""
        SELECT ch.channel_id, ch.name, ch.group_primary, ch.group_extra, ch.tvg_logo, ch.status,
               ch.epg_channel_id, ch.tvg_id, ch.timeshift, ch.sort_hint,
               s.source_type, s.address, s.resolution, s.res_label, s.video_codec,
               s.fps, s.vbitrate, s.hdr, s.audio_codec, s.audio_channels, s.available,
               s.redirect_hops, s.redirect_loop
        FROM channels ch
        LEFT JOIN channel_preferred_sources p ON ch.channel_id = p.channel_id AND p.rank = 1
        LEFT JOIN sources s ON p.source_id = s.source_id AND s.channel_id = ch.channel_id
        WHERE ch.enabled = 1
    """).fetchall()
    # 排序键: 与 gen_m3u 用同一套规则(GROUP_ORDER 分组优先级 + 组内 order_in_group)。
    # 不能用 channels.sort_hint: 那是迁移期遗留字段,orphan_import 新建频道只写 order_in_group、
    # 不写 sort_hint(默认兜成9999),会被甩到列表最末 → Dashboard 与 m3u 顺序不一致
    # (已实证: 浙江政务 在m3u里排"其他"组末尾,在Dashboard里却排到广播组之后)。
    order_key = {}
    for r in c.execute("""SELECT channel_id, group_name, order_in_group, is_primary
                          FROM channel_groups"""):
        gi = GROUP_ORDER.index(r['group_name']) if r['group_name'] in GROUP_ORDER else len(GROUP_ORDER)
        key = (gi, r['order_in_group'] if r['order_in_group'] is not None else 9999)
        cid = r['channel_id']
        # 一频道可属多组(主组+附加组),Dashboard每频道只一行 → 取最靠前的位置
        if cid not in order_key or key < order_key[cid]:
            order_key[cid] = key
    conn.close()
    data = []
    for r in rows:
        cat = res_category(r['res_label'], r['group_primary'])
        data.append({
            'name': r['name'], 'group': r['group_primary'], 'group_extra': r['group_extra'] or '',
            'logo': r['tvg_logo'] or '', 'status': r['status'] or 'active',
            'epg_id': r['epg_channel_id'] or '', 'tvg_id': r['tvg_id'] or '', 'timeshift': r['timeshift'],
            'sort_hint': r['sort_hint'] if r['sort_hint'] is not None else 9999,
            '_order': order_key.get(r['channel_id'], (len(GROUP_ORDER), 9999)),
            'stype': r['source_type'] or '', 'address': r['address'] or '',
            'resolution': r['resolution'] or '', 'res_label': r['res_label'] or '',
            'vcodec': (r['video_codec'] or '').upper(), 'fps': r['fps'] or 0,
            'vbitrate': r['vbitrate'] or 0, 'hdr': r['hdr'] or '',
            'acodec': (r['audio_codec'] or '').upper(), 'achannels': r['audio_channels'] or 0,
            'available': r['available'], 'redirect_hops': r['redirect_hops'] or 0,
            'redirect_loop': r['redirect_loop'],
            'category': cat,
        })
    # 排序: 与 m3u 同序(见上方 order_key 说明)
    data.sort(key=lambda x: x['_order'])
    return data


# 音频声道友好名
def audio_label(codec, ch):
    if not codec:
        return ''
    ch_name = {1: '单声道', 2: '立体声', 6: '5.1', 8: '7.1'}.get(ch, f'{ch}ch' if ch else '')
    cn = codec_label(codec)
    return f'{cn} {ch_name}' if ch_name else cn


# 视频编码规范化: H264→H.264, HEVC→H.265
def codec_label(codec):
    c = (codec or '').upper()
    return {'H264': 'H.264', 'AVC': 'H.264', 'H265': 'H.265', 'HEVC': 'H.265'}.get(c, c)


# 音频编码规范化 + 按编码分配tag样式(区分MP2/AAC/EAC3等)
def audio_class(codec):
    c = (codec or '').upper()
    return {'MP2': 't-au-mp2', 'AAC': 't-au-aac', 'AC3': 't-au-ac3',
            'EAC3': 't-au-eac3', 'MP3': 't-au-mp2'}.get(c, 't-au-other')


def res_tag_class(res_label):
    return {'4K': 't-4k', '1080P': 't-hd', '720P': 't-sd', 'SD': 't-sd'}.get(res_label, 't-unknown')


# 清晰度 → 图标文件(用户提供的图标, 放 dashboard/icons/)
RES_ICON = {
    '8K': '8K_UHD.png', '4K': '4K_UHD.png', '2K': '2K_QHD.png',
    '1080P': '1080P_FHD.png', '720P': '720P_HD.png', 'SD': '480P_SD.png',
}

def res_icon_html(res_label, resolution):
    icon = RES_ICON.get(res_label)
    if icon:
        return f'<img class="res-icon" src="icons/{icon}" alt="{esc(res_label)}" title="{esc(resolution)}">'
    return ''


def build_html(data, epg=None):
    epg = epg or {}
    gen_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    total = len(data)
    online = sum(1 for d in data if d['available'])
    # 分类统计
    cats = {}
    for d in data:
        cats[d['category']] = cats.get(d['category'], 0) + 1
    cat_order = ['4K', '1080P', '标清', '广播', '未知']
    cat_emoji = {'4K': '📺', '1080P': '🎬', '标清': '📻', '广播': '🔊', '未知': '⚠️', '全部': '🌐'}
    cat_label = {'未知': '未知/不稳定'}  # 显示名(分类内部名仍是'未知')

    # 顶部过滤按钮(用清晰度图标)
    cat_icon = {'4K': 'icons/4K_UHD.png', '1080P': 'icons/1080P_FHD.png',
                '标清': 'icons/480P_SD.png', '广播': 'icons/radio.png'}
    def btn_icon(cat):
        ic = cat_icon.get(cat)
        if ic:
            return f'<img class="fbtn-icon" src="{ic}">'
        return f'<span class="fbtn-emoji">{cat_emoji.get(cat, "")}</span>'  # 无图标用大emoji占位,等高
    def btn(cat, count, active=False):
        label = cat_label.get(cat, cat)
        return (f'<button class="fbtn{" active" if active else ""}" data-cat="{cat}">'
                f'{btn_icon(cat)}<span class="fbtn-txt">{label} <b>{count}</b></span></button>')
    filter_btns = btn('全部', total, active=True)
    for cat in cat_order:
        if cats.get(cat):
            filter_btns += btn(cat, cats[cat])

    # 表格行
    trs = []
    used_epg = set()   # 收集实际用到节目单的频道
    for i, d in enumerate(data, 1):
        # 台标(APTV双色斜块背景)
        logo = f'<div class="logo-box"><img src="{esc(d["logo"])}" onerror="this.parentNode.classList.add(\'noimg\')" loading="lazy"></div>' if d['logo'] else '<div class="logo-box noimg"></div>'
        # 视频信息: 分辨率+编码+帧率+HDR+码率 都用彩色tag(不用图标)
        vtags = ''
        if d['res_label']:
            vtags += f'<span class="tag {res_tag_class(d["res_label"])}">{esc(d["resolution"])}</span>'
        if d['vcodec']:
            vtags += f'<span class="tag t-codec">{esc(codec_label(d["vcodec"]))}</span>'
        if d['fps']:
            vtags += f'<span class="tag t-fps">{int(d["fps"])}fps</span>'
        if d['hdr'] and d['hdr'] != 'SDR':
            vtags += f'<span class="tag t-hdr">{esc(d["hdr"])}</span>'
        if d['vbitrate'] > 0:
            vtags += f'<span class="tag t-br">{d["vbitrate"]//1000}M</span>'
        if not d['res_label']:
            if d['category'] == '广播':
                vtags = '<span class="tag t-radio">广播</span>'
            else:
                vtags = '<span class="tag t-unknown">未知/不稳定</span>'   # 扫描失败/DEAD/新频道
        # 音频: 按编码区分颜色
        atag = ''
        if d['acodec']:
            acls = audio_class(d['acodec'])
            atag = f'<span class="tag {acls}">{esc(audio_label(d["acodec"], d["achannels"]))}</span>'
        # 源类型: 彩色tag区分组播/单播
        if d['stype'] == 'multicast':
            stype = '<span class="tag t-mc">组播</span>'; redir = ''
        elif d['stype'] == 'rtsp':
            hop = f'<small class="hop">{d["redirect_hops"]}跳{"⚠" if d["redirect_loop"] else ""}</small>' if d['redirect_hops'] else ''
            stype = f'<span class="tag t-uc">单播</span>{hop}'; redir = ''
        else:
            stype = '<span class="muted">-</span>'; redir = ''
        # 状态
        badge = {'active': '<span class="dot ok"></span>',
                 'offline': '<span class="dot off"></span>',
                 'new': '<span class="dot new"></span>'}.get(d['status'], '<span class="dot ok"></span>' if d['available'] else '<span class="dot off"></span>')
        # 节目单列(只留节目单按钮,不再显示频道ID数字)
        tvg = d['tvg_id']
        has_epg = tvg and tvg in epg
        if has_epg:
            used_epg.add(tvg)
            epg_link = f'<button class="epg-btn" onclick="showEpg(\'{esc(tvg)}\',\'{esc(d["name"])}\')">📅 节目单</button>'
        else:
            epg_link = '<span class="muted">-</span>'
        # 地址(组播ip:port 或 单播rtsp完整多行)
        addr = d['address']
        play_html = '<span class="muted">-</span>'
        if d['stype'] == 'multicast':
            addr_html = f'<code class="addr-mc" ondblclick="copyText(this)" title="双击复制">{esc(addr)}</code>'
            # 播放: 组播用IINA(需前缀,JS运行时用localStorage前缀拼http)
            play_html = f'<button class="play-btn play-iina" data-mc="{esc(addr)}" onclick="playIINA(this)">▶ IINA</button>'
        elif d['stype'] == 'rtsp':
            # 单播: 完整地址(到.smil去token), 多行不截断, 双击复制
            simple = address_util.canonical_rtsp(addr)
            addr_html = f'<code class="addr-uc" ondblclick="copyText(this)" title="双击复制">{esc(simple)}</code>'
            # 播放: 单播直接拼 iina://(RTSP,不需前缀)
            import urllib.parse
            iina_url = 'iina://weblink?url=' + urllib.parse.quote(simple, safe='')
            play_html = f'<a class="play-btn play-iina" href="{esc(iina_url)}" title="用IINA播放">▶ IINA</a>'
        else:
            addr_html = '<span class="muted">-</span>'
        grp = esc(d['group']) + (f'<small>+{esc(d["group_extra"])}</small>' if d['group_extra'] else '')
        ts = '<span class="tag t-ts">时移</span>' if d['timeshift'] else ''

        trs.append({
            'category': d['category'], 'name': esc(d['name']),
            'logo': logo, 'ts': ts, 'grp': grp, 'vtags': vtags, 'atag': atag,
            'stype': stype, 'redir': redir, 'addr_html': addr_html,
            'play_html': play_html, 'badge': badge, 'epg_link': epg_link,
        })

    # 只嵌入实际用到的频道节目单(减小体积)
    epg_subset = {k: epg[k] for k in used_epg if k in epg}
    epg_json = json_for_script(epg_subset)

    return render_template(
        'dashboard.html',
        gen_time=gen_time, total=total, online=online, offline=total - online,
        n4k=cats.get('4K', 0), filter_btns=filter_btns, rows=trs,
        epg_server=EPG_SERVER, epg_json=epg_json)




if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--out', default=DEFAULT_OUT)
    ap.add_argument('--epg', default=os.path.join(RADAR, 'output', 'epg_today.json'),
                    help='节目单JSON(fetch_epg.py生成)')
    args = ap.parse_args()
    print("=" * 50)
    print("  iptv-radar 生成Dashboard v2")
    print("=" * 50)
    epg = {}
    if os.path.exists(args.epg):
        with open(args.epg, encoding='utf-8') as f:
            epg = json.load(f)
        print(f"  节目单: {len(epg)} 频道")
    else:
        print(f"  (无节目单json,跳过。先运行 fetch_epg.py)")
    data = load_data(args.db)
    doc = build_html(data, epg)
    out_dir = os.path.dirname(args.out)
    os.makedirs(out_dir, exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as f:
        f.write(doc)
    # 复制静态图标(reference/icons/ → dashboard/icons/),确保发布时图标就位
    # (icons是静态资源,放reference进镜像;output是挂载空目录,每次生成时复制)
    import shutil
    src_icons = os.path.join(RADAR, 'reference', 'icons')
    dst_icons = os.path.join(out_dir, 'icons')
    if os.path.isdir(src_icons):
        os.makedirs(dst_icons, exist_ok=True)
        for fn in os.listdir(src_icons):
            shutil.copy2(os.path.join(src_icons, fn), os.path.join(dst_icons, fn))
    print(f"  输出: {args.out} ({len(doc)} 字节, {len(data)} 频道), icons已复制")
    print("完成")
