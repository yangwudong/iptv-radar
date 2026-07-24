#!/usr/bin/env python3
"""
iptv-radar 阶段2 生成层: gen_m3u.py
纯从 SQLite(iptv.db) 读数据,生成 m3u。不读任何其他源,不做数据决策。

规则(见 M3U_ACCEPTANCE_CRITERIA.md 生成层 G1-G8):
  - 只输出 enabled=1 的频道
  - 频道按 sort_hint(迁移自m3u顺序,即分组优先级+组内排序)输出
  - 附加分组(group_extra): 一个频道额外输出到每个附加组(复制法)
  - 每频道用主源(channel_preferred_sources rank=1)的地址
  - EXTINF格式: #EXTINF:-1 tvg-id="" tvg-logo="" group-title="",name

运行: python3 gen_m3u.py [--db PATH] [--out PATH] [--msd HOST:PORT]
  --msd  msd_lite/udpxy 组播转HTTP地址(m3u里的占位符,发布时替换成真实地址)
"""
import sqlite3
import os
import argparse

RADAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
DEFAULT_DB = os.path.join(RADAR, 'data', 'iptv.db')
DEFAULT_OUT = os.path.join(RADAR, 'output', 'iptv.m3u')
# 组播源用的 msd_lite/udpxy 地址(m3u里的占位符,发布时替换成真实地址)
DEFAULT_MSD = '127.0.0.1:4088'

# 分组顺序(策略配置,G1) — 分组间的固定优先级
GROUP_ORDER = ['央视', '4K超高清', '卫视', '浙江', '北京', '上海', '湖南',
               '港澳台', '央视教育', '央视国际', '少儿', 'BesTV', '睛彩',
               '其他', '广播', '未识别']


def addr_to_url(source_type, address, msd):
    """源地址 → 播放URL"""
    if source_type == 'multicast':
        # address = "233.50.201.118:5140"
        return f"http://{msd}/rtp/{address}"
    elif source_type == 'rtsp':
        return address  # rtsp url 原样
    return address


def generate(db_path, out_path, msd):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # 读所有启用的频道 + 主源(经 channel_preferred_sources rank=1)
    rows = c.execute("""
        SELECT ch.channel_id, ch.name, ch.tvg_id, ch.tvg_logo,
               s.source_type, s.address, s.playback_days
        FROM channels ch
        LEFT JOIN channel_preferred_sources p ON ch.channel_id = p.channel_id AND p.rank = 1
        LEFT JOIN sources s ON p.source_id = s.source_id
        WHERE ch.enabled = 1
    """).fetchall()
    ch_by_id = {r['channel_id']: r for r in rows}

    # 读频道-分组关联(含组内位置)
    from collections import defaultdict
    group_members = defaultdict(list)
    all_groups = set()
    for r in c.execute("SELECT channel_id, group_name, order_in_group FROM channel_groups"):
        if r['channel_id'] in ch_by_id:  # 只要enabled的
            group_members[r['group_name']].append((r['order_in_group'], r['channel_id']))
            all_groups.add(r['group_name'])

    # 分组顺序: 按固定 GROUP_ORDER 策略(G1),不在列表的组排最后
    all_groups.discard('')
    group_order = [g for g in GROUP_ORDER if g in all_groups]
    group_order += sorted(g for g in all_groups if g not in GROUP_ORDER)

    # 每组内按 order_in_group 排序输出
    lines = ['#EXTM3U']
    count = 0
    for g in group_order:
        members = sorted(group_members[g], key=lambda x: x[0])
        for _, cid in members:
            r = ch_by_id[cid]
            url = addr_to_url(r['source_type'], r['address'], msd) if r['address'] else ''
            if not url:
                continue
            logo_attr = f' tvg-logo="{r["tvg_logo"]}"' if r['tvg_logo'] else ''
            id_attr = f' tvg-id="{r["tvg_id"]}"' if r['tvg_id'] else ''
            # 回看: 单播源(含PLTV) + playback_days>0 → 加catchup标签(APTV等可回看)
            # catchup-source用&playseek(直播url已有?参数,用&接续);本地时间(实测电信playseek用北京时间)
            catchup_attr = ''
            if r['source_type'] == 'rtsp' and (r['playback_days'] or 0) > 0:
                catchup_attr = ' catchup="append" catchup-source="&playseek=${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}"'
            lines.append(f'#EXTINF:-1{id_attr}{logo_attr}{catchup_attr} group-title="{g}",{r["name"]}')
            lines.append(url)
            count += 1

    conn.close()
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return count, len(group_order)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--out', default=DEFAULT_OUT)
    ap.add_argument('--msd', '--udpxy', dest='msd', default=DEFAULT_MSD,
                    help='msd_lite/udpxy 组播转HTTP地址')
    args = ap.parse_args()
    print("=" * 50)
    print("  iptv-radar 生成m3u (gen_m3u.py)")
    print("=" * 50)
    n, ng = generate(args.db, args.out, args.msd)
    print(f"  输出: {args.out}")
    print(f"  频道条目: {n}, 分组: {ng}")
    print("完成")
