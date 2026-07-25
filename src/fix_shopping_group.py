#!/usr/bin/env python3
"""一次性数据更正: 央广购物(132) 从 其他 组挪入 购物 组。

为什么需要手动跑: run_pipeline.sh 不 load 种子,本地库改动 / channels_seed.json
只对"从零重建"有效,传不到 NAS 活库。
幂等: 已在购物组则什么都不做,可重复执行。
已于 2026-07-25 在本地库 + NAS 活库执行完毕。
"""
import sqlite3
import os
import sys

RADAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
DEFAULT_DB = os.path.join(RADAR, 'data', 'iptv.db')
CID = 132          # 央广购物


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    rows = db.execute("SELECT group_name, order_in_group FROM channel_groups "
                      "WHERE channel_id=?", (CID,)).fetchall()
    print(f"  改前: 央广购物 分组行={[dict(r) for r in rows]}")

    if len(rows) != 1:
        print(f"  ❌ 分组行数为 {len(rows)}(应为1),不动手,请人工检查")
        return 1

    if rows[0]['group_name'] == '购物':
        print("  已在购物组,无需改动(幂等)")
    else:
        db.execute("UPDATE channel_groups SET group_name='购物', order_in_group=1 "
                   "WHERE channel_id=?", (CID,))
        db.commit()

    after = db.execute("SELECT group_name, order_in_group FROM channel_groups "
                       "WHERE channel_id=?", (CID,)).fetchall()
    print(f"  改后: 央广购物 分组行={[dict(r) for r in after]}")
    if not (len(after) == 1 and after[0]['group_name'] == '购物'):
        print("  ❌ 更正未生效")
        return 1
    print("  ✅ 完成")
    return 0


if __name__ == '__main__':
    sys.exit(main())
