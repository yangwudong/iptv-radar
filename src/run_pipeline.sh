#!/bin/bash
# iptv-radar 一键流水线: 采集 → 数据清洗 → ETL → 生成 → 发布
# 三层解耦,任一步失败可单独重跑。
#
# 用法: ./run_pipeline.sh [--full] [--bitrate] [--publish]
#   (默认)    known增量扫描(只扫库里已知源,快~11分钟) — 适合每周cron
#   --full    full全量扫描(全768段,发现新频道,慢~17分钟) — 适合每月/初始化
#   --bitrate 组播实测码率(慢)
#   --publish 发布m3u到nginx目录(需配置 NGINX_M3U_DIR)
#
# cron示例:
#   每周一 3:00 增量:  0 3 * * 1  cd /path/iptv-radar/src && ./run_pipeline.sh --publish
#   每月1号 4:00 全量: 0 4 1 * *  cd /path/iptv-radar/src && ./run_pipeline.sh --full --publish
set -e
cd "$(dirname "$0")"

# 读取 .env(真实配置,含msd_lite地址/nginx路径等)
if [ -f "../.env" ]; then
    set -a; . ../.env; set +a
fi

# msd_lite/udpxy 组播转码地址(.env 里 UDPXY= 或 MSD=,兼容)
MSD="${MSD:-${UDPXY:-127.0.0.1:4088}}"
NGINX_M3U_DIR="${NGINX_M3U_DIR:-/volume1/docker/nginx/m3u}"
EPG_JSON="../reference/channels.sample.json"
STAMP=$(date +%Y%m%d_%H%M%S)

# 扫描模式: 默认 known(增量), --full 则 full(全量)
SCAN_MODE="known"
[[ "$*" == *"--full"* ]] && SCAN_MODE="full"
MC_ARGS="--mode $SCAN_MODE --msd $MSD"
[[ "$*" == *"--bitrate"* ]] && MC_ARGS="$MC_ARGS --bitrate"

echo "############################################"
echo "# iptv-radar pipeline  $STAMP"
echo "# 扫描模式: $SCAN_MODE"
echo "############################################"

# 0. 消费上次App的孤儿源识别结果(若有) → 写库归并
echo ""; echo ">>> [0/7] 消费孤儿源识别结果(data/orphan_inbox/)"
python3 orphan_import.py || echo "  无识别结果或消费出错(继续)"

# 1. 采集: 组播扫描(三轮递进,零误报)
echo ""; echo ">>> [1/7] 组播扫描 ($SCAN_MODE)"
python3 scan_multicast.py $MC_ARGS || echo "  组播扫描出错(继续)"

# 2. 采集: RTSP单播扫描(追踪重定向链)
echo ""; echo ">>> [2/7] RTSP扫描"
python3 scan_rtsp.py --epg "$EPG_JSON" --trace || echo "  RTSP扫描出错(继续)"

# 3. 数据清洗: 源归并到频道(自动: 官方channels.json + source_links.json快照)
echo ""; echo ">>> [3/7] 数据清洗(归并)"
python3 link_sources.py --epg "$EPG_JSON"

# 4. ETL: 源优选(全失效频道不选主源) + 变更检测(下线)
echo ""; echo ">>> [4/7] ETL处理(优选+变更检测)"
python3 etl_process.py

# 5. 产出待识别包: 剩余孤儿源导出给App人工识别
echo ""; echo ">>> [5/7] 产出孤儿源待识别包(output/orphan_review/)"
python3 orphan_export.py --msd "$MSD" || echo "  无孤儿源或导出出错(继续)"

# 6. 生成 m3u
echo ""; echo ">>> [6/7] 生成m3u"
python3 gen_m3u.py --msd "$MSD"

# 7. 生成 Dashboard + EPG
echo ""; echo ">>> [7/7] 抓EPG + 生成Dashboard"
python3 fetch_epg.py || echo "  EPG抓取失败(继续,Dashboard将无节目单)"
python3 gen_dashboard.py
python3 gen_channels_page.py

# 发布(可选)
if [[ "$*" == *"--publish"* ]]; then
    echo ""; echo ">>> 发布 m3u → $NGINX_M3U_DIR"
    if [ -d "$NGINX_M3U_DIR" ]; then
        cp ../output/iptv.m3u "$NGINX_M3U_DIR/iptv.m3u"
        echo "  已发布 iptv.m3u"
    else
        echo "  ⚠️ Nginx目录不存在,跳过: $NGINX_M3U_DIR"
    fi
fi

echo ""; echo "############################################"
echo "# 完成 $STAMP  (模式:$SCAN_MODE)"
echo "# m3u:       ../output/iptv.m3u"
echo "# dashboard: ../output/dashboard/index.html"
echo "############################################"
