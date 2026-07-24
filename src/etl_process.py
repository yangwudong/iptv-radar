#!/usr/bin/env python3
"""
iptv-radar 加工层: etl_process.py
分析 sources 表,做决策,写回 channels 表。不采集,不生成m3u。

职责(见 M3U_ACCEPTANCE_CRITERIA.md ETL层 + REFACTOR_DESIGN.md 5.7.2):
  1. 源优选(E2): 同频道多源按 分辨率>码率>稳定性>类型 打分,选primary_source_id
  2. 变更检测:
     - 新增: sources有channel_id=NULL的可用源 → 待人工归并/识别(status=new)
     - 下线: 频道所有源连续fail_count>=阈值 → status=offline
  3. 富化(可选,人工优先): 已有name/logo/group的不覆盖

注: 归并(E1,把孤儿源关联到频道)需要频道名匹配,当前迁移已建好关联,
    新扫描发现的孤儿源留待人工处理(Dashboard展示"待识别")。

运行: python3 etl_process.py [--db] [--offline-threshold 4]
"""
import sqlite3
import os
import sys
import argparse
import datetime

RADAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
DEFAULT_DB = os.path.join(RADAR, 'data', 'iptv.db')
NOW = lambda: datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

# 源优选评分(E2): 分辨率权重 + 码率 + 稳定性 + 类型偏好
RES_SCORE = {'4K': 1000, '1080P': 500, '720P': 200, 'SD': 50}


def quality_score(src, fail_threshold):
    """给一个源打分(越高越优选)。
    可用源正常评分;临时失效(fail_count<阈值,可能误报)给很低分但仍可当候选;
    真失效(fail_count>=阈值)返回None表示不可作主源。"""
    fc = src['fail_count'] or 0
    if not src['available']:
        if fc < fail_threshold:
            return -50 + fc * -1   # 临时失效:极低分(优先让位给可用源),但连续失败越多越靠后
        return None                # 真失效:不可作主源
    score = RES_SCORE.get(src['res_label'], 0)
    score += (src['vbitrate'] or 0) / 1000        # kbps,同分辨率下区分
    score += 100 if fc == 0 else 0  # 稳定性
    score += 30 if src['source_type'] == 'multicast' else 0  # 组播偏好(低延迟稳)
    # 回看加成: 支持回看的单播源(playback_days>0)加分。适度(+60):
    #   同分辨率时压过组播偏好(+30)让回看源胜出(APTV能回看);
    #   但压不过高一档分辨率(差500),不为回看牺牲明显更高画质。
    #   组播+单播混合回看APTV不支持,故要回看必须让单播作主源(直播也走单播)。
    if src['source_type'] == 'rtsp' and (src['playback_days'] or 0) > 0:
        score += 60
    return round(score, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--offline-threshold', type=int, default=4,
                    help='连续N次不可用判下线(每周1次=4周)')
    args = ap.parse_args()
    FT = args.offline_threshold   # fail_count阈值(容错:临时失效保留,达阈值才真失效)

    print("=" * 55)
    print("  iptv-radar ETL处理")
    print("=" * 55)

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # === 1. 源优选 ===
    # 给所有源打分,每频道选最高分为主源,写入 channel_preferred_sources(rank=1)
    changed_primary = 0
    no_source = 0    # 全失效(无有效主源)的频道数
    # 只对真实频道优选(排除占位频道 __UNKNOWN__/__JUNK__ 等 placeholder)
    channels = c.execute("SELECT channel_id FROM channels WHERE status != 'placeholder'").fetchall()
    for ch in channels:
        cid = ch['channel_id']
        srcs = c.execute("""SELECT source_id,source_type,available,res_label,vbitrate,fail_count,playback_days
                            FROM sources WHERE channel_id=?""", (cid,)).fetchall()
        if not srcs:
            continue
        best = None
        best_score = -9999
        for s in srcs:
            sc = quality_score(s, FT)
            # 记录评分(真失效记-1标识)
            c.execute("UPDATE sources SET quality_score=? WHERE source_id=?",
                      (sc if sc is not None else -1, s['source_id']))
            if sc is not None and sc > best_score:
                best_score, best = sc, s['source_id']
        cur = c.execute("SELECT source_id FROM channel_preferred_sources WHERE channel_id=? AND rank=1",
                        (cid,)).fetchone()
        if best is not None:
            # 有有效主源(可用或临时失效),更新优选
            if cur is None or cur['source_id'] != best:
                c.execute("""INSERT INTO channel_preferred_sources(channel_id,source_id,rank)
                             VALUES(?,?,1)
                             ON CONFLICT(channel_id,rank) DO UPDATE SET source_id=excluded.source_id""",
                          (cid, best))
                changed_primary += 1
        else:
            # 全部源真失效(fail_count>=阈值): 清掉旧优选,不选主源(避免僵尸频道进m3u)
            if cur is not None:
                c.execute("DELETE FROM channel_preferred_sources WHERE channel_id=? AND rank=1", (cid,))
            no_source += 1

    # === 2. 变更检测:下线 ===
    # 频道的主源(或所有源)连续fail_count>=阈值 → offline
    offline = []
    for ch in c.execute("""SELECT ch.channel_id,ch.name,ch.status,
                           MAX(CASE WHEN s.available=1 THEN 1 ELSE 0 END) any_avail,
                           MIN(s.fail_count) min_fail
                           FROM channels ch LEFT JOIN sources s ON ch.channel_id=s.channel_id
                           WHERE ch.enabled=1 GROUP BY ch.channel_id"""):
        # 所有源都不可用 且 最少失败次数>=阈值
        if ch['any_avail'] == 0 and (ch['min_fail'] or 0) >= args.offline_threshold:
            if ch['status'] != 'offline':
                c.execute("UPDATE channels SET status='offline' WHERE channel_id=?", (ch['channel_id'],))
                offline.append(ch['name'])
        elif ch['any_avail'] == 1 and ch['status'] == 'offline':
            # 恢复
            c.execute("UPDATE channels SET status='active' WHERE channel_id=?", (ch['channel_id'],))

    # === 3. 变更检测:新增(孤儿源) ===
    orphans = c.execute("""SELECT COUNT(*) n FROM sources
                           WHERE channel_id IS NULL AND available=1""").fetchone()['n']

    conn.commit()

    # 统计
    print(f"  源优选: {len(channels)}频道已评分, 主源变更 {changed_primary}, 全失效无主源 {no_source}")
    print(f"  下线检测(阈值{args.offline_threshold}): 新标记下线 {len(offline)}")
    if offline:
        for n in offline[:10]:
            print(f"    - {n}")
    print(f"  待识别孤儿源(新扫描发现,未归并): {orphans}")
    print("\n  主源类型分布:")
    for r in c.execute("""SELECT s.source_type, COUNT(*) FROM channel_preferred_sources p
                          JOIN sources s ON p.source_id=s.source_id
                          WHERE p.rank=1
                          GROUP BY s.source_type"""):
        print(f"    {r[0]}: {r[1]}")
    conn.close()
    print("\n完成")


if __name__ == '__main__':
    main()
