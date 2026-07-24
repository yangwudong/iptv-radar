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
import argparse
import datetime
import html
import json

RADAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
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


def load_data(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    rows = c.execute("""
        SELECT ch.name, ch.group_primary, ch.group_extra, ch.tvg_logo, ch.status,
               ch.epg_channel_id, ch.tvg_id, ch.timeshift, ch.sort_hint,
               s.source_type, s.address, s.resolution, s.res_label, s.video_codec,
               s.fps, s.vbitrate, s.hdr, s.audio_codec, s.audio_channels, s.available,
               s.redirect_hops, s.redirect_loop, s.screenshots
        FROM channels ch
        LEFT JOIN channel_preferred_sources p ON ch.channel_id = p.channel_id AND p.rank = 1
        LEFT JOIN sources s ON p.source_id = s.source_id
        WHERE ch.enabled = 1
    """).fetchall()
    conn.close()
    data = []
    for r in rows:
        cat = res_category(r['res_label'], r['group_primary'])
        data.append({
            'name': r['name'], 'group': r['group_primary'], 'group_extra': r['group_extra'] or '',
            'logo': r['tvg_logo'] or '', 'status': r['status'] or 'active',
            'epg_id': r['epg_channel_id'] or '', 'tvg_id': r['tvg_id'] or '', 'timeshift': r['timeshift'],
            'sort_hint': r['sort_hint'] if r['sort_hint'] is not None else 9999,
            'stype': r['source_type'] or '', 'address': r['address'] or '',
            'resolution': r['resolution'] or '', 'res_label': r['res_label'] or '',
            'vcodec': (r['video_codec'] or '').upper(), 'fps': r['fps'] or 0,
            'vbitrate': r['vbitrate'] or 0, 'hdr': r['hdr'] or '',
            'acodec': (r['audio_codec'] or '').upper(), 'achannels': r['audio_channels'] or 0,
            'available': r['available'], 'redirect_hops': r['redirect_hops'] or 0,
            'redirect_loop': r['redirect_loop'], 'screenshots': r['screenshots'] or '',
            'category': cat,
        })
    # 排序: 保持 merged_multicast.m3u 原始顺序(sort_hint,即分组优先级+组内顺序)
    data.sort(key=lambda x: x['sort_hint'])
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
            simple = re.match(r'(rtsp://[^?]+\.smil)', addr)
            simple = simple.group(1) if simple else addr.split('?')[0]
            addr_html = f'<code class="addr-uc" ondblclick="copyText(this)" title="双击复制">{esc(simple)}</code>'
            # 播放: 单播直接拼 iina://(RTSP,不需前缀)
            import urllib.parse
            iina_url = 'iina://weblink?url=' + urllib.parse.quote(simple, safe='')
            play_html = f'<a class="play-btn play-iina" href="{esc(iina_url)}" title="用IINA播放">▶ IINA</a>'
        else:
            addr_html = '<span class="muted">-</span>'
        # 截图
        shots = ''
        if d['screenshots']:
            for sp in d['screenshots'].split(';')[:3]:
                fn = os.path.basename(sp)
                shots += f'<img class="thumb" src="screenshots/{esc(fn)}" onclick="zoom(this.src)" loading="lazy">'
        grp = esc(d['group']) + (f'<small>+{esc(d["group_extra"])}</small>' if d['group_extra'] else '')
        ts = '<span class="tag t-ts">时移</span>' if d['timeshift'] else ''

        trs.append(f'''<tr data-cat="{d['category']}" data-name="{esc(d['name'])}">
          <td class="c-idx">{i}</td>
          <td class="c-name">{logo}<span>{esc(d['name'])}{ts}</span></td>
          <td class="c-grp">{grp}</td>
          <td class="c-video">{vtags}</td>
          <td class="c-audio">{atag}</td>
          <td class="c-src">{stype}{redir}</td>
          <td class="c-addr">{addr_html}</td>
          <td class="c-play">{play_html}</td>
          <td class="c-status">{badge}</td>
          <td class="c-id">{epg_link}</td>
          <td class="c-shots">{shots}</td>
        </tr>''')

    # 只嵌入实际用到的频道节目单(减小体积)
    epg_subset = {k: epg[k] for k in used_epg if k in epg}
    epg_json = json.dumps(epg_subset, ensure_ascii=False, separators=(',', ':'))

    return HTML_TEMPLATE.format(
        gen_time=gen_time, total=total, online=online, offline=total - online,
        n4k=cats.get('4K', 0), filter_btns=filter_btns, rows='\n'.join(trs),
        epg_server=EPG_SERVER, epg_json=epg_json)


HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>浙江IPTV优选源列表</title>
<style>
:root {{
  --accent:#0066cc; --accent2:#2997ff;
  --bg:#f5f5f7; --card:#ffffff; --ink:#1d1d1f; --muted:#86868b;
  --border:#e0e0e0; --hover:#f5f5f7; --head:#fafafc;
  --tag-ink:#fff; --shadow:0 4px 20px rgba(0,0,0,.06);
}}
html[data-theme="dark"] {{
  --bg:#000000; --card:#1c1c1e; --ink:#f5f5f7; --muted:#98989d;
  --border:#38383a; --hover:#2c2c2e; --head:#2c2c2e;
  --shadow:0 4px 20px rgba(0,0,0,.4);
}}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{
  font-family:"SF Pro Text","SF Pro Display",-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
  background:var(--bg); color:var(--ink); transition:background .3s,color .3s;
  -webkit-font-smoothing:antialiased;
}}
.wrap {{ max-width:1760px; margin:0 auto; padding:0 20px 60px; }}
header {{ text-align:center; padding:36px 0 20px; }}
header h1 {{ font-size:30px; font-weight:600; letter-spacing:-.5px; }}
header .sub {{ color:var(--muted); font-size:14px; margin-top:8px; }}
header .nav {{ margin-top:12px; }}
header .nav a {{ color:var(--accent); text-decoration:none; font-size:14px; padding:7px 16px;
  border:1px solid var(--border); border-radius:18px; background:var(--card); transition:all .2s; }}
header .nav a:hover {{ border-color:var(--accent); background:var(--accent); color:#fff; }}
.nav-logo {{ height:16px; width:auto; vertical-align:middle; margin-right:6px; }}
.theme-btn {{
  position:fixed; top:20px; right:24px; z-index:50; cursor:pointer;
  background:var(--card); border:1px solid var(--border); border-radius:20px;
  padding:8px 14px; font-size:14px; color:var(--ink); box-shadow:var(--shadow);
  transition:all .2s;
}}
.theme-btn:hover {{ transform:scale(1.05); }}
/* 统计卡片 */
.stats {{ display:flex; gap:14px; justify-content:center; flex-wrap:wrap; margin:20px 0; }}
.stat {{ background:var(--card); border-radius:14px; padding:16px 28px; text-align:center; box-shadow:var(--shadow); min-width:96px; }}
.stat .n {{ font-size:30px; font-weight:600; letter-spacing:-.5px; }}
.stat .l {{ font-size:12px; color:var(--muted); margin-top:2px; }}
/* 过滤按钮 */
.filters {{ display:flex; gap:10px; justify-content:center; flex-wrap:wrap; margin:24px 0 18px; }}
.fbtn {{
  background:var(--card); border:1px solid var(--border); border-radius:14px;
  padding:10px 18px; font-size:14px; color:var(--ink); cursor:pointer; transition:all .2s;
  display:inline-flex; flex-direction:column; align-items:center; gap:4px; min-width:78px;
}}
.fbtn b {{ color:var(--muted); font-weight:600; margin-left:3px; }}
.fbtn:hover {{ border-color:var(--accent); }}
.fbtn.active {{ background:var(--accent); color:#fff; border-color:var(--accent); }}
.fbtn.active b {{ color:rgba(255,255,255,.7); }}
/* 搜索 */
.search {{ display:block; margin:0 auto 20px; max-width:400px; width:100%;
  padding:11px 18px; border:1px solid var(--border); border-radius:22px;
  background:var(--card); color:var(--ink); font-size:15px; outline:none; }}
.search:focus {{ border-color:var(--accent); }}
/* 表格 */
.tbl-wrap {{ background:var(--card); border-radius:16px; box-shadow:var(--shadow); overflow:hidden; }}
table {{ width:100%; border-collapse:collapse; }}
th,td {{ padding:11px 14px; text-align:center; border-bottom:1px solid var(--border); font-size:13px; }}
th:nth-child(2),td.c-name {{ text-align:left; }}
th {{ background:var(--head); color:var(--muted); font-weight:600; font-size:12px;
  text-transform:uppercase; letter-spacing:.4px; position:sticky; top:0; z-index:5; }}
tr:last-child td {{ border-bottom:0; }}
tbody tr:hover td {{ background:var(--hover); }}
.c-idx {{ color:var(--muted); width:40px; text-align:center; }}
.c-name {{ display:flex; align-items:center; justify-content:flex-start; gap:10px; font-weight:500; min-width:180px; text-align:left; }}
.c-name span {{ display:inline-flex; align-items:center; gap:6px; }}
/* 台标: 再放大 + APTV双色斜块背景(浅色台标可见) */
.logo-box {{ width:96px; height:60px; border-radius:9px; flex-shrink:0;
  background:repeating-linear-gradient(135deg,#323132 0 16px,#373536 16px 32px);
  display:flex; align-items:center; justify-content:center;
  overflow:hidden; padding:6px; }}
.logo-box img {{ max-width:100%; max-height:100%; object-fit:contain; }}
.logo-box.noimg::after {{ content:"📺"; font-size:24px; opacity:.5; }}
.logo-box.noimg img {{ display:none; }}
/* 过滤按钮的清晰度图标(放大清晰) */
.fbtn-icon {{ height:60px; width:auto; object-fit:contain; }}
.fbtn-emoji {{ height:60px; display:flex; align-items:center; justify-content:center; font-size:36px; }}
.fbtn-txt {{ white-space:nowrap; }}
.fbtn b {{ margin-left:0; }}
/* tag块状 */
.tag {{ display:inline-block; padding:2px 9px; border-radius:5px; color:var(--tag-ink);
  font-size:11px; font-weight:600; margin:2px 3px 2px 0; letter-spacing:.2px; }}
.t-4k {{ background:#875bf7; }}       /* 4K紫 */
.t-hd {{ background:#17b26a; }}       /* 1080P绿 */
.t-sd {{ background:#f79009; }}       /* 标清橙 */
.t-codec {{ background:#667085; }}
.t-fps {{ background:#0ea5e9; }}
.t-hdr {{ background:#e67e22; }}
.t-br {{ background:#475467; }}
.t-au-mp2 {{ background:#0e9488; }}     /* MP2 青绿 */
.t-au-aac {{ background:#7c3aed; }}     /* AAC 紫 */
.t-au-ac3 {{ background:#c026d3; }}     /* AC3 品红 */
.t-au-eac3 {{ background:#db2777; }}    /* EAC3 玫红(杜比) */
.t-au-other {{ background:#0d9488; }}
.t-mc {{ background:#3b5bdb; }}         /* 组播 靛蓝 */
.t-uc {{ background:#ea580c; }}         /* 单播 橙 */
.hop {{ color:var(--muted); margin-left:4px; font-size:11px; }}
.t-radio {{ background:#8b5cf6; }}
.t-unknown {{ background:#98a2b3; }}
.t-ts {{ background:#ec4899; font-size:10px; padding:1px 6px; }}
.chid {{ color:var(--muted); font-size:12px; display:block; }}
.epg-btn {{ background:var(--accent); color:#fff; border:0; border-radius:6px;
  padding:3px 10px; font-size:12px; cursor:pointer; margin-top:3px; transition:opacity .2s; }}
.epg-btn:hover {{ opacity:.85; }}
.c-addr code {{ font-family:"SF Mono",Menlo,monospace; font-size:10px; padding:2px 7px;
  border-radius:5px; background:var(--head); cursor:pointer; }}
.c-addr code:hover {{ outline:1px solid var(--accent); }}
.addr-mc {{ color:#2563eb; white-space:nowrap; }}
/* 单播: 多行完整显示,不截断 */
.addr-uc {{ color:#ea580c; display:inline-block; max-width:260px; word-break:break-all;
  white-space:normal; line-height:1.4; text-align:left; }}
/* 播放按钮 */
.c-play {{ white-space:nowrap; }}
.play-btn {{ display:inline-block; border:0; border-radius:6px; padding:4px 10px; margin:2px;
  font-size:12px; cursor:pointer; text-decoration:none; font-weight:600; transition:opacity .2s; }}
.play-btn:hover {{ opacity:.85; }}
.play-iina {{ background:var(--accent); color:#fff; }}
/* msd_lite 前缀输入 */
.prefix-bar {{ display:flex; gap:10px; justify-content:center; align-items:center; flex-wrap:wrap;
  margin:0 auto 18px; max-width:640px; background:var(--card); border:1px solid var(--border);
  border-radius:14px; padding:12px 18px; box-shadow:var(--shadow); }}
.prefix-bar label {{ font-size:13px; color:var(--muted); white-space:nowrap; }}
.prefix-bar input {{ flex:1; min-width:220px; padding:8px 12px; border:1px solid var(--border);
  border-radius:8px; background:var(--bg); color:var(--ink); font-size:13px;
  font-family:"SF Mono",Menlo,monospace; outline:none; }}
.prefix-bar input:focus {{ border-color:var(--accent); }}
.prefix-bar .pfx-save {{ background:var(--accent); color:#fff; border:0; border-radius:8px;
  padding:8px 16px; font-size:13px; cursor:pointer; white-space:nowrap; }}
.prefix-bar .pfx-hint {{ font-size:11px; color:var(--muted); width:100%; text-align:center; }}
/* 节目单弹窗 */
#epgModal {{ display:none; position:fixed; inset:0; background:rgba(0,0,0,.5); z-index:100;
  justify-content:center; align-items:center; }}
.epg-panel {{ background:var(--card); border-radius:16px; max-width:480px; width:90%;
  max-height:75vh; overflow:hidden; box-shadow:var(--shadow); display:flex; flex-direction:column; }}
.epg-head {{ padding:16px 20px; border-bottom:1px solid var(--border); font-weight:600;
  display:flex; justify-content:space-between; align-items:center; }}
.epg-head .close {{ cursor:pointer; color:var(--muted); font-size:22px; line-height:1; }}
.epg-list {{ overflow-y:auto; padding:8px 0; }}
.epg-item {{ display:flex; gap:14px; padding:8px 20px; font-size:14px; }}
.epg-item:hover {{ background:var(--hover); }}
.epg-item .et {{ color:var(--accent); font-weight:600; min-width:44px; }}
.epg-item.now {{ background:var(--accent); color:#fff; }}
.epg-item.now .et {{ color:#fff; }}
.epg-day {{ padding:6px 20px; font-size:12px; color:var(--muted); background:var(--head); font-weight:600; }}
.muted {{ color:var(--muted); }}
.c-name small,.c-grp small {{ color:var(--muted); font-weight:400; font-size:11px; margin-left:4px; }}
.dot {{ display:inline-block; width:9px; height:9px; border-radius:50%; }}
.dot.ok {{ background:#17b26a; }} .dot.off {{ background:#f04438; }} .dot.new {{ background:#f79009; }}
.thumb {{ height:34px; border-radius:5px; margin-right:4px; cursor:pointer; border:1px solid var(--border); vertical-align:middle; }}
/* lightbox */
#lb {{ display:none; position:fixed; inset:0; background:rgba(0,0,0,.9); z-index:99; justify-content:center; align-items:center; }}
#lb img {{ max-width:92%; max-height:92%; border-radius:10px; }}
footer {{ text-align:center; color:var(--muted); font-size:13px; padding:30px 0; }}
footer a {{ color:var(--accent); text-decoration:none; }}
@media(max-width:768px) {{ .c-grp,.c-shots {{ display:none; }} header h1 {{ font-size:24px; }} }}
</style></head>
<body>
<button class="theme-btn" onclick="toggleTheme()"><span id="themeIcon">🌙</span> <span id="themeTxt">深色</span></button>
<div class="wrap">
  <header>
    <h1>📡 浙江IPTV优选源列表</h1>
    <div class="sub">生成于 {gen_time} · 共 {total} 个频道 · 在线 {online}</div>
    <div class="nav"><a href="channels.html"><img class="nav-logo" src="icons/China-Telecom.png" alt="电信">电信官方频道列表 (ID/组播/单播/时移)</a></div>
  </header>
  <div class="stats">
    <div class="stat"><div class="n">{total}</div><div class="l">总频道</div></div>
    <div class="stat"><div class="n" style="color:#17b26a">{online}</div><div class="l">在线</div></div>
    <div class="stat"><div class="n" style="color:#875bf7">{n4k}</div><div class="l">4K超高清</div></div>
    <div class="stat"><div class="n" style="color:#f04438">{offline}</div><div class="l">离线</div></div>
  </div>
  <div class="filters">{filter_btns}</div>
  <div class="prefix-bar">
    <label>组播前缀</label>
    <input id="mcPrefix" placeholder="http://你的msd_lite地址:4088/rtp/" spellcheck="false">
    <button class="pfx-save" onclick="savePrefix()">保存</button>
    <div class="pfx-hint">填入 msd_lite/udpxy 的 http 前缀(组播转码地址),用于组播源的 IINA 播放。仅存本地浏览器,不上传。</div>
  </div>
  <input class="search" id="search" placeholder="🔍 搜索频道名..." oninput="doFilter()">
  <div class="tbl-wrap">
    <table>
      <thead><tr>
        <th>#</th><th>频道</th><th>分组</th>
        <th>视频信息</th><th>音频</th><th>源</th><th>地址</th><th>播放</th><th>状态</th><th>节目单</th><th>截图</th>
      </tr></thead>
      <tbody id="tbody">{rows}</tbody>
    </table>
  </div>
  <footer>
    浙江电信IPTV源检测 · <a href="iptv.m3u">下载 m3u</a> ·
    数据源 EPG {epg_server}
  </footer>
</div>
<div id="lb" onclick="this.style.display='none'"><img id="lbimg"></div>
<div id="epgModal" onclick="if(event.target===this)this.style.display='none'">
  <div class="epg-panel">
    <div class="epg-head"><span id="epgTitle"></span><span class="close" onclick="document.getElementById('epgModal').style.display='none'">×</span></div>
    <div class="epg-list" id="epgList"></div>
  </div>
</div>
<script>
var EPG = {epg_json};
// 主题: 跟随系统 + localStorage记忆
function applyTheme(t) {{
  document.documentElement.setAttribute('data-theme', t);
  document.getElementById('themeIcon').textContent = t==='dark'?'☀️':'🌙';
  document.getElementById('themeTxt').textContent = t==='dark'?'浅色':'深色';
}}
function toggleTheme() {{
  var cur = document.documentElement.getAttribute('data-theme');
  var next = cur==='dark'?'light':'dark';
  localStorage.setItem('theme', next); applyTheme(next);
}}
(function() {{
  var saved = localStorage.getItem('theme');
  if (saved) applyTheme(saved);
  else applyTheme(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');
}})();
// 过滤(分类tag + 搜索)
var curCat = '全部';
document.querySelectorAll('.fbtn').forEach(function(b) {{
  b.onclick = function() {{
    document.querySelectorAll('.fbtn').forEach(x=>x.classList.remove('active'));
    b.classList.add('active'); curCat = b.dataset.cat; doFilter();
  }};
}});
function doFilter() {{
  var kw = document.getElementById('search').value.toLowerCase();
  var idx = 0;
  document.querySelectorAll('#tbody tr').forEach(function(tr) {{
    var okCat = curCat==='全部' || tr.dataset.cat===curCat;
    var okKw = !kw || tr.dataset.name.toLowerCase().indexOf(kw)>=0;
    if (okCat && okKw) {{ tr.style.display=''; idx++; tr.querySelector('.c-idx').textContent=idx; }}
    else tr.style.display='none';
  }});
}}
function zoom(src) {{ document.getElementById('lbimg').src=src; document.getElementById('lb').style.display='flex'; }}
// 节目单弹窗
function showEpg(tvgId, name) {{
  var progs = EPG[tvgId] || [];
  document.getElementById('epgTitle').textContent = name + ' · 节目单';
  var now = new Date();
  var today = now.getFullYear()+String(now.getMonth()+1).padStart(2,'0')+String(now.getDate()).padStart(2,'0');
  var nowHM = String(now.getHours()).padStart(2,'0')+':'+String(now.getMinutes()).padStart(2,'0');
  var html='', lastDay='', nowMarked=false;
  for (var i=0;i<progs.length;i++) {{
    var p=progs[i];
    if (p.d!==lastDay) {{
      var dstr = p.d.slice(0,4)+'-'+p.d.slice(4,6)+'-'+p.d.slice(6,8);
      html += '<div class="epg-day">'+dstr+(p.d===today?' (今天)':'')+'</div>';
      lastDay=p.d;
    }}
    // 标记当前正在播的(今天 且 该节目时间<=现在<下一节目)
    var isNow=false;
    if (p.d===today && !nowMarked) {{
      var next=progs[i+1];
      if (p.t<=nowHM && (!next || next.d!==today || next.t>nowHM)) {{ isNow=true; nowMarked=true; }}
    }}
    html += '<div class="epg-item'+(isNow?' now':'')+'"><span class="et">'+p.t+'</span><span>'+
            p.n.replace(/</g,'&lt;')+(isNow?' ▶':'')+'</span></div>';
  }}
  if (!html) html='<div class="epg-item"><span>暂无节目单数据</span></div>';
  document.getElementById('epgList').innerHTML=html;
  document.getElementById('epgModal').style.display='flex';
}}
// ===== 组播前缀(msd_lite) localStorage 存取 =====
function getPrefix() {{ return localStorage.getItem('mcPrefix') || ''; }}
function savePrefix() {{
  var v = document.getElementById('mcPrefix').value.trim();
  if (v && !v.endsWith('/')) v += '/';   // 容错: 自动补尾斜杠
  localStorage.setItem('mcPrefix', v);
  document.getElementById('mcPrefix').value = v;
  alert(v ? '已保存组播前缀:\\n'+v : '已清空组播前缀');
}}
(function() {{ document.getElementById('mcPrefix').value = getPrefix(); }})();
// 组播地址(233.x:port) + 前缀 → 完整播放URL
function mcUrl(mc) {{
  var p = getPrefix();
  if (!p) {{ alert('请先在顶部填写"组播前缀"(msd_lite地址)并保存'); return null; }}
  return p + mc;   // 如 http://host:4088/rtp/ + 233.50.201.1:5140
}}
// 双击复制地址
function copyText(el) {{
  var t = el.textContent;
  navigator.clipboard.writeText(t).then(function() {{
    var old = el.style.outline; el.style.outline = '2px solid #17b26a';
    setTimeout(function() {{ el.style.outline = old; }}, 600);
  }});
}}
// IINA播放(组播): 用前缀拼http后交给IINA
function playIINA(btn) {{
  var url = mcUrl(btn.dataset.mc);
  if (!url) return;
  window.location.href = 'iina://weblink?url=' + encodeURIComponent(url);
}}
</script>
</body></html>'''


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
