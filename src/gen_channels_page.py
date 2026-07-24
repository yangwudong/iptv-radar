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
    c = sqlite3.connect(db_path)
    for r in c.execute("""SELECT address, resolution, res_label, video_codec, fps, hdr,
                          audio_codec, audio_channels, available FROM sources"""):
        info[r[0]] = {'res': r[1], 'res_label': r[2], 'codec': r[3], 'fps': r[4],
                      'hdr': r[5], 'acodec': r[6], 'ach': r[7], 'avail': r[8]}
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
    c = sqlite3.connect(db_path)
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
    trs = []
    for i, ch in enumerate(data, 1):
        name = ch.get('name', '')
        mc, uc = split_url(ch.get('url', ''))
        ts = ch.get('timeshift_url', '')
        ts_badge = '<span class="tag t-ts">支持</span>' if ts else '<span class="muted">-</span>'
        mc_html = f'<code class="mc">{esc(mc)}</code>' if mc else '<span class="muted">-</span>'
        # 单播地址: 完整简化版(到.smil,去token),字体小可换行,双击复制。单独用"播放"列调IINA
        if uc:
            simple = re.match(r'(rtsp://[^?]+\.smil)', uc)
            simple = simple.group(1) if simple else uc.split('?')[0]
            import urllib.parse
            iina_url = 'iina://weblink?url=' + urllib.parse.quote(simple, safe='')
            uc_html = f'<code class="uc" title="双击复制" data-full="{esc(simple)}" ondblclick="copyUrl(this)">{esc(simple)}</code>'
            play_html = f'<a class="play-btn" href="{esc(iina_url)}" title="用IINA播放">▶ IINA</a>'
        else:
            uc_html = '<span class="muted">-</span>'
            play_html = '<span class="muted">-</span>'
        # 台标(APTV双色斜块背景)
        logo = find_logo(name, mc, logo_by_name, logo_by_addr)
        logo_html = (f'<div class="logo-box"><img src="{esc(logo)}" onerror="this.parentNode.classList.add(\'noimg\')" loading="lazy"></div>'
                     if logo else '<div class="logo-box noimg"></div>')
        # 扫描信息
        sc = scan.get(mc)
        if not sc and uc:
            m = re.match(r'(rtsp://[^?]+\.smil)', uc)
            if m:
                sc = scan.get(m.group(1))
        trs.append(f'''<tr data-name="{esc(name)}" data-id="{esc(ch.get('id',''))}">
          <td class="c-idx">{i}</td>
          <td class="c-name">{logo_html}<span>{esc(name)}</span></td>
          <td class="c-id">{esc(ch.get('id',''))}</td>
          <td class="c-video">{video_tags(sc)}</td>
          <td class="c-audio">{audio_tag(sc)}</td>
          <td class="c-mc">{mc_html}</td>
          <td class="c-uc">{uc_html}</td>
          <td class="c-play">{play_html}</td>
          <td class="c-ts">{ts_badge}</td>
        </tr>''')
    n_mc = sum(1 for ch in data if 'igmp://' in ch.get('url', '') or 'rtp://' in ch.get('url', ''))
    n_uc = sum(1 for ch in data if 'rtsp://' in ch.get('url', ''))
    n_ts = sum(1 for ch in data if ch.get('timeshift_url'))
    return TEMPLATE.format(gen_time=gen_time, total=len(data), n_mc=n_mc, n_uc=n_uc,
                           n_ts=n_ts, rows='\n'.join(trs))


TEMPLATE = '''<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>电信官方频道列表</title>
<style>
:root {{ --accent:#0066cc; --bg:#f5f5f7; --card:#fff; --ink:#1d1d1f; --muted:#86868b;
  --border:#e0e0e0; --hover:#f5f5f7; --head:#fafafc; --shadow:0 4px 20px rgba(0,0,0,.06); }}
html[data-theme="dark"] {{ --bg:#000; --card:#1c1c1e; --ink:#f5f5f7; --muted:#98989d;
  --border:#38383a; --hover:#2c2c2e; --head:#2c2c2e; --shadow:0 4px 20px rgba(0,0,0,.4); }}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font-family:"SF Pro Text",-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
  background:var(--bg); color:var(--ink); -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:1600px; margin:0 auto; padding:0 20px 60px; }}
header {{ text-align:center; padding:36px 0 16px; }}
header h1 {{ font-size:28px; font-weight:600; letter-spacing:-.5px; display:flex; align-items:center; justify-content:center; gap:12px; }}
.title-logo {{ height:56px; width:auto; vertical-align:middle; }}
header .sub {{ color:var(--muted); font-size:14px; margin-top:8px; }}
.nav {{ text-align:center; margin:10px 0 20px; }}
.nav a {{ color:var(--accent); text-decoration:none; font-size:14px; padding:6px 14px;
  border:1px solid var(--border); border-radius:18px; background:var(--card); }}
.nav a:hover {{ border-color:var(--accent); }}
.theme-btn {{ position:fixed; top:20px; right:24px; z-index:50; cursor:pointer; background:var(--card);
  border:1px solid var(--border); border-radius:20px; padding:8px 14px; font-size:14px; color:var(--ink); box-shadow:var(--shadow); }}
.stats {{ display:flex; gap:14px; justify-content:center; flex-wrap:wrap; margin:16px 0; }}
.stat {{ background:var(--card); border-radius:14px; padding:14px 26px; text-align:center; box-shadow:var(--shadow); min-width:90px; }}
.stat .n {{ font-size:28px; font-weight:600; }}
.stat .l {{ font-size:12px; color:var(--muted); margin-top:2px; }}
.search {{ display:block; margin:0 auto 20px; max-width:400px; width:100%; padding:11px 18px;
  border:1px solid var(--border); border-radius:22px; background:var(--card); color:var(--ink); font-size:15px; outline:none; }}
.search:focus {{ border-color:var(--accent); }}
.tbl-wrap {{ background:var(--card); border-radius:16px; box-shadow:var(--shadow); overflow:hidden; }}
table {{ width:100%; border-collapse:collapse; }}
th,td {{ padding:11px 14px; text-align:center; border-bottom:1px solid var(--border); font-size:13px; }}
th {{ background:var(--head); color:var(--muted); font-weight:600; font-size:12px; text-transform:uppercase;
  letter-spacing:.4px; position:sticky; top:0; z-index:5; }}
tr:last-child td {{ border-bottom:0; }}
tbody tr:hover td {{ background:var(--hover); }}
.c-idx {{ color:var(--muted); width:44px; }}
.c-name {{ font-weight:500; text-align:left; min-width:180px; }}
.c-name {{ display:flex; align-items:center; gap:10px; }}
.logo-box {{ width:72px; height:46px; border-radius:8px; flex-shrink:0;
  background:repeating-linear-gradient(135deg,#323132 0 14px,#373536 14px 28px);
  display:flex; align-items:center; justify-content:center; overflow:hidden; padding:5px; }}
.logo-box img {{ max-width:100%; max-height:100%; object-fit:contain; }}
.logo-box.noimg::after {{ content:"📺"; font-size:18px; opacity:.5; }}
.logo-box.noimg img {{ display:none; }}
.c-id {{ color:var(--accent); font-weight:600; font-family:monospace; }}
code {{ font-family:"SF Mono",Menlo,monospace; font-size:12px; background:var(--head);
  padding:2px 8px; border-radius:5px; color:var(--ink); }}
code.mc {{ color:#2563eb; }}
code.uc,a.uc {{ color:#ea580c; cursor:pointer; user-select:all; font-size:10px; text-decoration:none;
  max-width:320px; display:inline-block; word-break:break-all; white-space:normal;
  line-height:1.4; text-align:left; padding:3px 8px; }}
code.uc:hover,a.uc:hover {{ background:var(--accent); color:#fff; }}
code.uc.copied,a.uc.copied {{ background:#17b26a; color:#fff; }}
th small {{ font-weight:400; text-transform:none; opacity:.7; }}
.tag {{ display:inline-block; padding:2px 9px; border-radius:5px; color:#fff; font-size:11px; font-weight:600; margin:2px 3px 2px 0; }}
.t-ts {{ background:#ec4899; }}
.t-4k {{ background:#875bf7; }}
.t-hd {{ background:#17b26a; }}
.t-sd {{ background:#f79009; }}
.t-codec {{ background:#667085; }}
.t-fps {{ background:#0ea5e9; }}
.t-hdr {{ background:#e67e22; }}
.t-audio {{ background:#0e9488; }}
.play-btn {{ display:inline-block; background:#7c3aed; color:#fff; text-decoration:none;
  padding:5px 12px; border-radius:7px; font-size:12px; font-weight:600; white-space:nowrap;
  transition:all .2s; }}
.play-btn:hover {{ background:#6d28d9; transform:scale(1.05); }}
.c-play {{ white-space:nowrap; }}
.t-unknown {{ background:#98a2b3; }}
.c-video,.c-audio {{ min-width:90px; }}
.muted {{ color:var(--muted); }}
footer {{ text-align:center; color:var(--muted); font-size:13px; padding:30px 0; }}
footer a {{ color:var(--accent); text-decoration:none; }}
</style></head><body>
<button class="theme-btn" onclick="toggleTheme()"><span id="ti">🌙</span> <span id="tt">深色</span></button>
<div class="wrap">
  <header>
    <h1><img class="title-logo" src="icons/China-Telecom.png" alt="中国电信">电信官方频道列表</h1>
    <div class="sub">来自电信EPG官方 getchannellist · 生成于 {gen_time}</div>
    <div class="nav"><a href="index.html">← 返回频道检测 Dashboard</a></div>
  </header>
  <div class="stats">
    <div class="stat"><div class="n">{total}</div><div class="l">官方频道</div></div>
    <div class="stat"><div class="n" style="color:#2563eb">{n_mc}</div><div class="l">有组播</div></div>
    <div class="stat"><div class="n" style="color:#ea580c">{n_uc}</div><div class="l">有单播</div></div>
    <div class="stat"><div class="n" style="color:#ec4899">{n_ts}</div><div class="l">支持时移</div></div>
  </div>
  <input class="search" id="search" placeholder="🔍 搜索频道名/ID..." oninput="doSearch()">
  <div class="tbl-wrap">
    <table>
      <thead><tr><th>#</th><th>频道名</th><th>频道ID</th><th>视频信息</th><th>音频</th><th>组播地址</th><th>单播地址 <small>(双击复制)</small></th><th>播放</th><th>时移</th></tr></thead>
      <tbody id="tbody">{rows}</tbody>
    </table>
  </div>
  <footer>电信官方频道台账 · <a href="index.html">频道检测 Dashboard</a></footer>
</div>
<script>
function applyTheme(t){{document.documentElement.setAttribute('data-theme',t);
  document.getElementById('ti').textContent=t==='dark'?'☀️':'🌙';
  document.getElementById('tt').textContent=t==='dark'?'浅色':'深色';}}
function toggleTheme(){{var c=document.documentElement.getAttribute('data-theme');
  var n=c==='dark'?'light':'dark';localStorage.setItem('theme',n);applyTheme(n);}}
(function(){{var s=localStorage.getItem('theme');
  applyTheme(s||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light'));}})();
function doSearch(){{var kw=document.getElementById('search').value.toLowerCase();var idx=0;
  document.querySelectorAll('#tbody tr').forEach(function(tr){{
    var ok=!kw||tr.dataset.name.toLowerCase().indexOf(kw)>=0||tr.dataset.id.indexOf(kw)>=0;
    if(ok){{tr.style.display='';idx++;tr.querySelector('.c-idx').textContent=idx;}}else tr.style.display='none';
  }});}}
function copyUrl(el){{
  var full=el.dataset.full;
  navigator.clipboard.writeText(full).then(function(){{
    var old=el.textContent; el.classList.add('copied'); el.textContent='✓ 已复制';
    setTimeout(function(){{el.classList.remove('copied'); el.textContent=old;}},1200);
  }});
}}
</script>
</body></html>'''


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
