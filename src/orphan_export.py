#!/usr/bin/env python3
"""
iptv-radar: orphan_export.py — 产出"待识别包"(孤儿源识别流程第①步)

发现的孤儿源(channel_id=NULL)导出为待识别包,供 Electron App 人工识别。
设计见 docs/design/ORPHAN_REVIEW.md §3.1。

产出: output/orphan_review/
  ├── orphans.json   孤儿源清单 + 可归属频道清单 + 占位频道(契约§3.1)
  └── shots/         每个孤儿源截图(组播才截,单播复用已有截图)

用法: python3 orphan_export.py [--db] [--msd HOST:PORT] [--shots/--no-shots] [--limit N]
  --no-shots  不重新截图(快,用库里已有的screenshots)
"""
import sqlite3
import os
import sys
import json
import argparse
import datetime
import urllib.parse
import re
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe
import address_util
from template_util import render_template

RADAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
DEFAULT_DB = os.path.join(RADAR, 'data', 'iptv.db')
# 产出到 output/dashboard/: 该目录已被宿主 nginx 挂载并服务(/dashboard/),
# 放进来的文件直接可用浏览器打开,零 nginx 配置改动、零新增服务。
# (实测 NAS 真实配置: compose 挂 output/dashboard → /usr/share/nginx/html/dashboard:ro,
#  nginx `location /dashboard/ { alias ...; }` → 目录内任何文件都被服务)
# 原来产出在 output/orphan_review/,nginx 访问不到,页面打不开。
REVIEW_DIR = os.path.join(RADAR, 'output', 'dashboard')
SHOTS_DIR = os.path.join(REVIEW_DIR, 'orphan-shots')
SHOTS_REL = 'orphan-shots'      # json/HTML 里的相对路径(页面与图同目录)


def play_url(source_type, address, msd):
    """孤儿源播放URL: 组播用msd前缀拼http, 单播rtsp原样"""
    if source_type == 'multicast':
        return f"http://{msd}/rtp/{address}"
    return address  # rtsp


def shot_prefix(source_type, address):
    """截图文件名前缀: 必须每个地址唯一,否则多个源共用一套图(看图识别直接失效)。

    组播沿用 IP 形式(233_50_201_204),不改 —— 变了会让已有截图全部失效重拍。
    单播不能用 addr.split(':')[0]: 'rtsp://...' 恒得字面 'rtsp',所有单播源互相覆盖。
    单播用 频道标识段(倒数第二段) + 尾段截断 + 地址md5前6位:
      .../3221229007/10000100000000060000000004308260_0.smil → u3221229007_100001000000_xxxxxx
    只截尾段的话,几个直播室频道前24字符完全相同、只有哈希不同,翻目录分不出谁是谁
    (实拍后发现)。md5 保证唯一,不依赖路径规律。
    """
    if source_type == 'multicast':
        return address.split(':')[0].replace('.', '_')
    parts = [re.sub(r'[^0-9A-Za-z]+', '_', x).strip('_')
             for x in address.rsplit('/', 3)[1:]]          # 取末尾若干段
    ident = parts[-2][:14] if len(parts) >= 2 else ''      # 频道标识段(如 3221229007)
    tail = parts[-1][:12] if parts else ''                 # 尾段(截断即可,唯一性靠md5)
    human = '_'.join(x for x in (ident, tail) if x)
    return f"u{human}_{hashlib.md5(address.encode()).hexdigest()[:6]}" if human \
        else 'u' + hashlib.md5(address.encode()).hexdigest()[:10]


def default_epg():
    """官方台账优先用 fetch_channels 刷新的(含新token),回退旧位置,最后回退历史样例。
    路径顺序与 run_pipeline.sh 的 EPG_JSON 保持一致。"""
    for p in (os.path.join(RADAR, 'data', 'channels.json'),
              os.path.join(RADAR, 'reference', 'channels.json'),
              os.path.join(RADAR, 'reference', 'channels.sample.json')):
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
    return ''


def load_official(epg_path):
    """官方 channels.json → (地址→官方名, 地址→同一官方频道的其他地址)。

    实测价值: 26个孤儿里13个官方列表已给出名字(直播室1-7/好易购1高清/好享购)。
    它们成为孤儿只因库里没这些频道、NAME_OVERRIDES 也没映射 —— 不是认不出来。
    查不到的一律留空: 猜错的名字比空白更糟(会误导人工识别)。
    """
    if not epg_path or not os.path.exists(epg_path):
        return {}, {}
    try:
        epg = json.load(open(epg_path, encoding='utf-8'))
    except Exception as e:
        print(f"  ⚠️ 官方台账读取失败({e}),本次不带官方名")
        return {}, {}
    name, sib = {}, {}
    for ch in epg:
        addrs = address_util.parse_official_url(ch.get('url'))
        for a in addrs:
            name[a] = ch.get('name') or ''
            sib[a] = [x for x in addrs if x != a]
    return name, sib


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--msd', '--udpxy', dest='msd', default='127.0.0.1:4088',
                    help='msd_lite地址(组播播放URL前缀用)')
    ap.add_argument('--epg', default=None,
                    help='官方 channels.json(给孤儿带上官方名+配对关系)。默认自动探测')
    ap.add_argument('--no-shots', action='store_true', help='完全不截图(最快)')
    ap.add_argument('--reshoot', action='store_true',
                    help='强制重拍已有截图的源(默认: 已有截图的跳过,只拍新出现的孤儿源)')
    ap.add_argument('--limit', type=int, default=0, help='只导出前N个(测试用)')
    # 识别完把 resolved.json 放回 orphan_inbox 的提示命令(页面上一键复制)。
    # 真实主机/端口/路径不写死在代码里(AGENTS.md 规则2),从环境变量读 —— 由 .env 提供,
    # pipeline 通过 env_file 传进容器。没配就退化成占位符(能看出该填什么)。
    #   INBOX_SCP_TARGET  如 user@host:/volume1/docker/iptv-radar/data/orphan_inbox/
    #   INBOX_SSH_PORT    如 1222(非22时才需要)
    #   INBOX_LOCAL_PATH  已挂载(SMB/NFS)时的本地路径,如 /Volumes/docker/iptv-radar/data/orphan_inbox/
    scp_target = os.environ.get('INBOX_SCP_TARGET', '<user>@<nas>:/volume1/docker/iptv-radar/data/orphan_inbox/')
    ssh_port = os.environ.get('INBOX_SSH_PORT', '').strip()
    port_arg = f'-P {ssh_port} ' if ssh_port else ''
    local_path = os.environ.get('INBOX_LOCAL_PATH', '/Volumes/docker/iptv-radar/data/orphan_inbox/')
    ap.add_argument('--cp-hint', default=f'cp ~/Downloads/{{f}} {local_path}',
                    help='页面提示: 已挂载时的拷贝命令({f}=文件名占位)')
    ap.add_argument('--scp-hint', default=f'scp -O {port_arg}~/Downloads/{{f}} {scp_target}',
                    help='页面提示: scp 命令({f}=文件名占位)')

    args = ap.parse_args()

    print("=" * 55)
    print("  iptv-radar 孤儿源导出(待识别包)")
    print("=" * 55)

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # 孤儿源: channel_id=NULL 且可用(不导失效的,避免噪音)
    q = """SELECT address, source_type, res_label, video_codec, fps, hdr,
                  audio_codec, screenshots
           FROM sources WHERE channel_id IS NULL AND available=1
           ORDER BY source_type, address"""
    orphan_rows = c.execute(q).fetchall()
    if args.limit:
        orphan_rows = orphan_rows[:args.limit]

    if not orphan_rows:
        print("  无待识别孤儿源,不生成包。")
        conn.close()
        return

    # 官方名 + 配对关系(识别时最有价值的线索,见 load_official)
    epg_path = args.epg or default_epg()
    off_name, off_sib = load_official(epg_path)
    orphan_addrs = {r['address'] for r in orphan_rows}

    # 可归属频道清单(供App下拉/tag匹配)
    channels = [{'channel_key': r['channel_key'], 'name': r['name'], 'group': r['grp'] or ''}
                for r in c.execute("""SELECT channel_key, name,
                        (SELECT group_name FROM channel_groups g
                         WHERE g.channel_id=channels.channel_id AND g.is_primary=1) AS grp
                    FROM channels
                                      WHERE status!='placeholder' AND channel_key IS NOT NULL
                                      ORDER BY sort_hint""")]
    placeholders = [{'channel_key': r['channel_key'], 'name': r['name']}
                    for r in c.execute("SELECT channel_key, name FROM channels WHERE status='placeholder'")]
    # 注意: 连接要留到下面回写截图路径之后再关

    os.makedirs(SHOTS_DIR, exist_ok=True)

    orphans = []
    shot_writes = []   # (screenshots, address) 截图路径回写
    reused = 0         # 复用已有截图的源数
    for r in orphan_rows:
        addr = r['address']
        stype = r['source_type']
        purl = play_url(stype, addr, args.msd)
        iina = 'iina://weblink?url=' + urllib.parse.quote(purl, safe='')

        # 截图策略(改前每次运行都把全部组播孤儿源重拍一遍: 17个源×3张,单张超时上限20s,
        # 最坏每周白烧17分钟,而这些源是已知黑名单/无效源、短期不会变。
        # 更关键的是拍完**从不回写库**,导致 sources.screenshots 长期为空、
        # "复用已有截图"那条分支形同死代码 —— 现在补上回写):
        #   已有截图且文件还在磁盘 → 跳过; 只拍新出现的孤儿源; --reshoot 可强制重拍
        shots = []
        if r['screenshots']:
            shots = [os.path.basename(x) for x in r['screenshots'].split(';') if x]
        have = bool(shots) and all(os.path.exists(os.path.join(SHOTS_DIR, x)) for x in shots)
        # 单播也截图: 官方名只是线索,画面才是证据。直播裸地址不需要token(已实测)。
        need_shot = not args.no_shots and (args.reshoot or not have)
        if have and not args.reshoot:
            reused += 1
        if need_shot:
            prefix = shot_prefix(stype, addr)
            paths = probe.capture_screenshots(purl, SHOTS_DIR, prefix, count=3)
            if paths:
                shots = [os.path.basename(x) for x in paths]
                shot_writes.append((';'.join(paths), addr))   # 回写库,下次可复用
                print(f"    {addr}: {len(shots)}张截图")

        orphans.append({
            'address': addr, 'source_type': stype,
            'res_label': r['res_label'] or '', 'video_codec': r['video_codec'] or '',
            'fps': r['fps'] or 0, 'hdr': r['hdr'] or '', 'audio_codec': r['audio_codec'] or '',
            'play_url': purl, 'iina_url': iina,
            'shots': [f"{SHOTS_REL}/{s}" for s in shots],
            # 官方台账给出的名字(查不到则空,不编造)
            'official_name': off_name.get(addr, ''),
            # 同一官方频道、且同样还是孤儿的其他地址: 一个决定会连带它们
            # (link_sources 按官方列表归并,已由测试锁住该行为)
            'paired_with': [x for x in off_sib.get(addr, []) if x in orphan_addrs],
        })

    # 回写截图路径(让下次能复用,避免每周重拍同一批已知垃圾流)
    if shot_writes:
        c.executemany("UPDATE sources SET screenshots=? WHERE address=?", shot_writes)
        conn.commit()
    conn.close()

    pkg = {
        'generated_at': datetime.datetime.now().isoformat(timespec='seconds'),
        'msd_prefix': f"http://{args.msd}/rtp/",
        'channels': channels,
        'placeholders': placeholders,
        'orphans': orphans,
    }
    out_json = os.path.join(REVIEW_DIR, 'orphans.json')
    json.dump(pkg, open(out_json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    # 识别页面: 纯静态、数据内联(不 fetch —— 页面可能被下载到本地用 file:// 打开,
    # 那时 fetch 会被 CORS 拦死且只在控制台报错,表现为"页面空白"极难查)。
    out_html = os.path.join(REVIEW_DIR, 'orphan-review.html')
    groups = sorted({c['group'] for c in channels if c['group']})
    # 页面数据不带 play_url/iina_url: 那是导出时按 --msd 拼的,而 pipeline 在 NAS 上
    # 传的是容器/内网视角地址(如 127.0.0.1:4088),看页面的却是 Mac —— 烧死就播不了。
    # 页面按用户自填的"组播前缀"(localStorage 键 mcPrefix,与 dashboard.html 共用)现算。
    page_orphans = [{k: v for k, v in o.items() if k not in ('play_url', 'iina_url')}
                    for o in orphans]
    page_data = {
        'orphans': page_orphans, 'channels': channels,
        'msd_prefix': pkg['msd_prefix'],     # 仅作输入框的默认值/占位提示
        'paths': {'cp_hint': args.cp_hint, 'scp_hint': args.scp_hint},
    }
    # 内联进 <script type="application/json">: 只需断开 '</' 即可,
    # 否则数据里出现 '</script>' 会截断脚本、整页白屏。
    data_json = json.dumps(page_data, ensure_ascii=False).replace('</', '<\\/')
    html_txt = render_template(
        'orphan_review.html',
        gen_time=pkg['generated_at'].replace('T', ' '),
        total=len(orphans),
        n_mc=sum(1 for o in orphans if o['source_type'] == 'multicast'),
        n_uc=sum(1 for o in orphans if o['source_type'] == 'rtsp'),
        n_named=sum(1 for o in orphans if o['official_name']),
        channels=channels, groups=groups, data_json=data_json)
    open(out_html, 'w', encoding='utf-8').write(html_txt)

    print(f"\n  待识别孤儿源: {len(orphans)} 个 (组播{sum(1 for o in orphans if o['source_type']=='multicast')} "
          f"/ 单播{sum(1 for o in orphans if o['source_type']=='rtsp')})")
    print(f"  可归属频道: {len(channels)}  占位: {len(placeholders)}")
    print(f"  截图: 新拍 {len(shot_writes)} 个源, 复用已有 {reused} 个"
          + ("  (--no-shots: 本次未截图)" if args.no_shots else ""))
    print(f"  产出: {out_json}")
    print(f"        {out_html}")
    print("完成")


if __name__ == '__main__':
    main()
