#!/usr/bin/env python3
"""
iptv-radar: link_sources.py (归并脚本, ETL的E1核心)
把所有 sources 归并到频道业务主键 channel_key(=规范频道名)。
解决孤儿源问题→台标/信息按 address→channel_key→频道 全部能反查。

归并权威顺序:
  1. 已关联的源: 用其 channel 的规范名做 channel_key(种子)
  2. 官方 channels.json: 同一官方频道的多个地址→同一 channel_key
  3. tag 别名匹配: 频道名/EPG名 含别名tag
  4. 都不行: 孤儿源,channel_key=NULL,待识别

设计见 docs/design/CHANNEL_KEY_DESIGN.md
运行: python3 link_sources.py [--db] [--epg channels.json] [--dry-run]
"""
import sqlite3
import db_util
import json
import os
import re
import shutil
import argparse

RADAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
DEFAULT_DB = os.path.join(RADAR, 'data', 'iptv.db')
DEFAULT_EPG = os.path.join(RADAR, 'reference', 'channels.sample.json')


# 脱敏样例(reference/channels.sample.json)里的假token特征。
# 该文件是提交进仓库的示例数据,token 与账号都被占位化过。
_PLACEHOLDER_MARKS = ('SAMPLE_TOKEN_REDACTED', 'accountinfo=%2C00000000%2C')


def is_placeholder_query(q):
    """这个 query 是否来自脱敏样例(=假token,写进库会让频道播不了)。"""
    return bool(q) and any(m in q for m in _PLACEHOLDER_MARKS)


def norm(s):
    """归一化用于匹配: 去画质后缀/空格"""
    return re.sub(r'(高清|HD|标清|SD|4K| )', '', s or '').strip()


def load_name_overrides():
    """加载官方名→规范名映射(别名归并用,157条)。数据在 reference/name_overrides.json。"""
    path = os.path.join(RADAR, 'reference', 'name_overrides.json')
    if not os.path.exists(path):
        return {}
    return json.load(open(path, encoding='utf-8')).get('overrides', {})


def get_addrs(url):
    """从channels.json的url提取 组播地址 + 单播简化地址"""
    addrs = []
    for part in (url or '').split('|'):
        part = part.strip()
        m = re.search(r'igmp://(\d+\.\d+\.\d+\.\d+:\d+)', part)
        if m:
            addrs.append(m.group(1))
        elif part.startswith('rtsp://'):
            s = re.match(r'(rtsp://[^?]+\.smil)', part)
            addrs.append(s.group(1) if s else part.split('?')[0])
    return addrs


def get_addr_queries(ch):
    """从channels.json一个频道提取单播 {简化地址: 完整query(含token)}。
    用 timeshift_url(回看地址,带token);完整地址 = 简化地址 + '?' + query。
    catchup='append'模式下直播也用此地址(后接&playseek即回看),故直播回看同源。"""
    out = {}
    ts = ch.get('timeshift_url') or ''
    if ts.startswith('rtsp://') and '?' in ts:
        s = re.match(r'(rtsp://[^?]+\.smil)', ts)
        if s:
            out[s.group(1)] = ts.split('?', 1)[1]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--epg', default=DEFAULT_EPG)
    ap.add_argument('--dry-run', action='store_true', help='只报告不写库')
    args = ap.parse_args()

    print("=" * 55)
    print("  link_sources: 源归并到 channel_key")
    print("=" * 55)

    conn = db_util.connect(args.db)
    c = conn.cursor()

    # === 1. channel_key 主数据: 频道的规范名(channels.channel_key 已由迁移建好) ===
    # 两张表分工(别合并!):
    #   key2id_all  = 全部频道(含 enabled=0 / placeholder) → 用于"保留已有归并"和最终回填 channel_id。
    #                 禁用是"是否发布"的决定,不是"解除归并"。占位频道(__JUNK__/__UNKNOWN__)挂靠的源
    #                 也必须保住,否则孤儿识别结果每次 pipeline 都被打回,同一批垃圾流反复出现。
    #   key2id      = 仅 enabled 频道 → 用于按官方名自动匹配新地址(占位/黑名单频道不当自动归并目标)。
    # 曾经的bug: 两者混用一张 enabled-only 表 → 禁用一个频道就把它的人工归并结果清空,
    #            且快照被全量重建覆盖(见下方 §5),不可逆。
    key2id_all = {}  # channel_key → channel_id (全部频道)
    key2id = {}      # channel_key → channel_id (仅enabled,自动匹配用)
    norm2key = {}    # 归一化名 → channel_key (仅enabled)
    for r in c.execute("SELECT channel_id, channel_key, enabled FROM channels WHERE channel_key IS NOT NULL").fetchall():
        key = r['channel_key']
        key2id_all[key] = r['channel_id']
        if r['enabled']:
            key2id[key] = r['channel_id']
            norm2key[norm(key)] = key
    id2key = {v: k for k, v in key2id_all.items()}   # channel_id → channel_key(反查)
    canon = key2id  # 兼容下方报告(自动匹配目标数)

    # === 2. 种子: 已关联的源 → 记其 channel_id ===
    # 归并关系一律以 channel_id(不变的代理键)承载,不用会变的 channel_key,
    # 否则频道改名/合并时归并关系会跟着断(AGENTS.md 铁律)。
    # 不按 enabled 过滤: 已建立的归并关系是事实,禁用/占位频道的关联同样要保留。
    addr2cid = {}    # address → channel_id
    for r in c.execute("""SELECT address, channel_id FROM sources
                          WHERE channel_id IS NOT NULL""").fetchall():
        if r['channel_id'] in id2key:
            addr2cid[r['address']] = r['channel_id']

    # === 2.5 加载持久化归并快照(人工确认的归并结果,最高优先) ===
    # data/source_links.json: {address: {"channel_id": N, "channel_key": "可读名"}}
    #   以 channel_id 为准。曾经用 channel_key 做键(旧格式),频道一改名/合并,
    #   快照条目就被当"频道已不存在"静默丢弃,而缩水告警(>10%)抓不住
    #   (447条丢5条=1.1%)—— 快照本来是"库丢了也能恢复归并"的最后保险,那样就失效了。
    # 兼容: 值是字符串 = 旧格式,按 channel_key 查(升级无缝,不丢现有447条)。
    snapshot_path = os.path.join(RADAR, 'data', 'source_links.json')
    snap_loaded, snap_dropped, snap_prev_count, snap_legacy = 0, 0, 0, 0
    if os.path.exists(snapshot_path):
        snap = json.load(open(snapshot_path, encoding='utf-8'))
        snap_prev_count = len(snap)
        for a, v in snap.items():
            cid = None
            if isinstance(v, dict):
                if v.get('channel_id') in id2key:
                    cid = v['channel_id']                      # 按不变的 id 解析(首选)
                elif v.get('channel_key') in key2id_all:
                    cid = key2id_all[v['channel_key']]         # id 没了但名字还在
            else:
                snap_legacy += 1
                cid = key2id_all.get(v)                        # 旧格式: 值就是 channel_key
            if cid:
                addr2cid[a] = cid
                snap_loaded += 1
            else:
                snap_dropped += 1        # 频道确实已被删除,才丢弃(会打印警告)

    # === 3. 官方channels.json: 同一官方频道的多地址 → 同一channel_key ===
    name_ov = load_name_overrides()   # 官方名→规范名(159条)
    valid_keys = set(key2id.keys())
    epg = json.load(open(args.epg, encoding='utf-8'))
    official_added = 0
    addr2query = {}   # 单播简化地址 → 完整query(含token),用于回写 sources.timeshift_query
    for ch in epg:
        addrs = get_addrs(ch['url'])
        addr2query.update(get_addr_queries(ch))   # 收集单播回看query(带token)
        key = None
        # a) 地址已知
        for a in addrs:
            if a in addr2cid:
                key = id2key.get(addr2cid[a])
                break
        # b) NAME_OVERRIDES映射官方名→规范名
        if not key:
            mapped = name_ov.get(ch['name'])
            if mapped and mapped in valid_keys:
                key = mapped
        # c) 归一化名匹配
        if not key:
            key = norm2key.get(norm(ch['name']))
        if key and key in key2id_all:
            for a in addrs:
                if a not in addr2cid:
                    addr2cid[a] = key2id_all[key]
                    official_added += 1

    # === 4. 回填 sources: channel_id(关联键,为主) + channel_key(可读冗余) ===
    # 自愈: 确保 timeshift_query 字段存在(旧库可能没有)
    cols = [r[1] for r in c.execute("PRAGMA table_info(sources)").fetchall()]
    if 'timeshift_query' not in cols:
        c.execute("ALTER TABLE sources ADD COLUMN timeshift_query TEXT")
        print("  + sources.timeshift_query 字段(自愈)")
    # 先取完再更新,避免cursor冲突
    src_rows = c.execute("SELECT source_id, address, source_type FROM sources").fetchall()
    linked, orphan = 0, 0
    ts_written = 0
    ts_skipped = 0   # 因是脱敏假token而拒写的数量
    updates = []   # (channel_id, channel_key, source_id)
    ts_updates = []  # (timeshift_query, source_id) 仅单播
    for r in src_rows:
        cid = addr2cid.get(r['address'])
        key = id2key.get(cid) if cid else None   # channel_key 是可读冗余,由 id 推出
        if cid:
            updates.append((cid, key, r['source_id']))
            linked += 1
        else:
            # 归不上: 清空两列(可能之前归错的也一并清,保持一致)
            updates.append((None, None, r['source_id']))
            orphan += 1
        # 单播源: 回写完整回看query(含token)。每周pipeline刷token→此列随之更新。
        # 但绝不能用**脱敏样例**里的假token覆盖库里的真token(已实证的严重故障):
        #   认证失败 → pipeline 回退 reference/channels.sample.json → 这里把167个源的
        #   timeshift_query 全写成 it=SAMPLE_TOKEN_REDACTED_NOT_REAL → 其中53个是m3u主源
        #   → 播放列表38%的频道直接播不了,而且退出码0、静默发布。
        # 真token不可恢复(要等下次认证成功),所以宁可保留旧token(可能过期)也不能覆盖成假的。
        if r['source_type'] == 'rtsp':
            q = addr2query.get(r['address'])
            if q and is_placeholder_query(q):
                ts_skipped += 1
            elif q:
                ts_updates.append((q, r['source_id']))
                ts_written += 1
    if not args.dry_run:
        c.executemany("UPDATE sources SET channel_id=?, channel_key=? WHERE source_id=?", updates)
        if ts_updates:
            c.executemany("UPDATE sources SET timeshift_query=? WHERE source_id=?", ts_updates)
        conn.commit()
        # 回写归并快照(持久化: 新归并的下次也不丢)。
        # 格式: {address: {"channel_id": N, "channel_key": "可读名"}}
        #   channel_id 是解析依据(频道改名也不断);channel_key 只是给人看的注释。
        # 从 channels 表 JOIN 取名,而不是读 sources.channel_key 那个冗余列 ——
        # 否则这份"最后保险"会依赖冗余列此刻是否新鲜。
        links = {r['address']: {'channel_id': r['channel_id'], 'channel_key': r['ck']}
                 for r in c.execute("""SELECT s.address, s.channel_id, ch.channel_key AS ck
                                       FROM sources s JOIN channels ch
                                         ON ch.channel_id = s.channel_id
                                       WHERE s.channel_id IS NOT NULL""").fetchall()}
        # 缩水告警: 快照是全量重建覆盖写,一旦归并逻辑出错(如误把频道排除在匹配目标外),
        # 人工归并成果会被静默销毁且不可逆。降幅>10% 先备份旧快照再写,并打印警告。
        if snap_prev_count and len(links) < snap_prev_count * 0.9:
            shutil.copy(snapshot_path, snapshot_path + '.bak')
            print(f"\n  ⚠️  归并快照从 {snap_prev_count} 条降到 {len(links)} 条(降幅>10%)!")
            print(f"      可能有频道被禁用/删除导致归并丢失。旧快照已备份: {snapshot_path}.bak")
        # 原子写: 先写临时文件再 rename,避免中途被杀留下截断的快照
        tmp = snapshot_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(links, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, snapshot_path)

    # === 报告 ===
    print(f"\n  自动匹配目标(enabled频道): {len(canon)}")
    print(f"  持久化快照加载: {snap_loaded} 条(人工归并结果)")
    if snap_legacy:
        print(f"  快照旧格式条目: {snap_legacy} 条(按channel_key解析,本次写出已升级为channel_id)")
    if snap_dropped:
        print(f"  快照丢弃: {snap_dropped} 条(所指频道已不存在)")
    print(f"  官方台账新增地址映射: {official_added}")
    print(f"  源归并结果: 已归并 {linked}, 孤儿 {orphan}")
    print(f"  单播回看query回写(含token): {ts_written} 个单播源")
    if ts_skipped:
        print(f"\n  ⚠️  拒绝写入 {ts_skipped} 个**脱敏样例**的假token(已保留库里原有token)!")
        print(f"      说明本次用的EPG是 reference/channels.sample.json,即 token 刷新失败。")
        print(f"      单播回看会随旧token过期而失效,请检查 fetch_channels 的认证。")
    # 抽样打印几个多源频道,便于人眼核对归并结果(原来硬编码"钱江"频道名,
    # 且 resolution 为 NULL 时会 TypeError 崩掉整个脚本 —— 一段调试代码不该有这种杀伤力)
    sample = c.execute("""SELECT channel_key FROM sources WHERE channel_key IS NOT NULL
                          GROUP BY channel_key HAVING COUNT(*) > 1
                          ORDER BY COUNT(*) DESC LIMIT 1""").fetchone()
    if sample:
        print(f"\n  抽样核对: {sample['channel_key']} 的所有源")
        for r in c.execute("""SELECT address, resolution, channel_key, source_type
                              FROM sources WHERE channel_key=?""", (sample['channel_key'],)):
            print(f"    {r['address']:<26} {(r['resolution'] or '-'):<10} → {r['channel_key']}")
    conn.close()
    print("\n完成" + (" (dry-run,未写库)" if args.dry_run else ""))


if __name__ == '__main__':
    main()
