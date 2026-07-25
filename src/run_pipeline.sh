#!/bin/bash
# iptv-radar 一键流水线: 采集 → 数据清洗 → ETL → 生成 → 发布
# 三层解耦,任一步失败可单独重跑。
#
# 用法: ./run_pipeline.sh [--full] [--bitrate] [--publish] [--timeshift-only] [--gen-only]
#   (默认)          known增量扫描(只扫库里已知源,快~11分钟) — 适合每周cron
#   --full          full全量扫描(全768段+回看探测,慢~20分钟) — 适合每月/初始化
#   --bitrate       组播实测码率(慢)
#   --publish       发布m3u到nginx目录(需配置 NGINX_M3U_DIR)
#   --timeshift-only 只跑回看天数探测+重新生成页面(补回看数据,不重扫,~5分钟)
#   --gen-only      只从现有库重新生成 m3u+Dashboard+页面(改模板/样式后用,几秒,不扫描/不刷token)
#
# cron示例:
#   每周一 3:00 增量:  0 3 * * 1  cd /path/iptv-radar/src && ./run_pipeline.sh --publish
#   每月1号 4:00 全量: 0 4 1 * *  cd /path/iptv-radar/src && ./run_pipeline.sh --full --publish
set -e
cd "$(dirname "$0")"

# ===== 参数解析 =====
# 用正规解析而不是 [[ "$*" == *"--full"* ]] 子串匹配: 子串匹配下拼错的参数(如 --gen-onl)
# 会被静默忽略 → 本想几秒重生成,结果跑了20分钟的全量扫描并刷了token。未知参数必须报错。
OPT_FULL=0; OPT_BITRATE=0; OPT_PUBLISH=0; OPT_TIMESHIFT_ONLY=0; OPT_GEN_ONLY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --full)           OPT_FULL=1 ;;
        --bitrate)        OPT_BITRATE=1 ;;
        --publish)        OPT_PUBLISH=1 ;;
        --timeshift-only) OPT_TIMESHIFT_ONLY=1 ;;
        --gen-only)       OPT_GEN_ONLY=1 ;;
        -h|--help)
            sed -n '2,16p' "$0"; exit 0 ;;
        *)
            echo "❌ 未知参数: $1" >&2
            echo "   可用: --full --bitrate --publish --timeshift-only --gen-only" >&2
            exit 2 ;;
    esac
    shift
done

# ===== 并发保护 =====
# 无锁时: 手动跑一次与cron自动跑撞上 → 两个进程同时写同一个 SQLite,
# 轻则 "database is locked" 中断,重则写出互相覆盖的库/半成品m3u。
LOCK_FILE="/tmp/iptv-radar-pipeline.lock"
exec 9>"$LOCK_FILE"
if command -v flock >/dev/null 2>&1; then
    if ! flock -n 9; then
        echo "❌ 另一个 pipeline 正在运行(锁: $LOCK_FILE),本次退出以免并发写库。" >&2
        exit 3
    fi
fi

# 读取 .env(真实配置,含msd_lite地址/nginx路径等)
if [ -f "../.env" ]; then
    set -a; . ../.env; set +a
fi

# msd_lite/udpxy 组播转码地址(.env 里 UDPXY= 或 MSD=,兼容)
MSD="${MSD:-${UDPXY:-127.0.0.1:4088}}"
# rtp2httpd FCC快速换台服务器(.env 里 FCC_SERVER=IP:端口);空则不加FCC。加--fcc参数给msd组播URL。
# 用数组而非字符串: 字符串未加引号展开会被 word-split/glob(FCC_SERVER 含空格时静默传错参数)。
FCC_ARG=()
[ -n "$FCC_SERVER" ] && FCC_ARG=(--fcc "$FCC_SERVER")
NGINX_M3U_DIR="${NGINX_M3U_DIR:-/volume1/docker/nginx/m3u}"
# EPG源: 优先用 fetch_channels 刷新的 channels.json(含新token,单播回看可持续),
#   没有(未刷成功)则回退历史快照 channels.sample.json(token可能过期,回看不保证)。
# 刷新出的(含新token)channels.json: 现放 data/(可写挂载目录);兼容旧部署的 reference/ 位置
EPG_FRESH="../data/channels.json"
[ ! -s "$EPG_FRESH" ] && [ -s "../reference/channels.json" ] && EPG_FRESH="../reference/channels.json"
EPG_SAMPLE="../reference/channels.sample.json"
EPG_JSON="$EPG_SAMPLE"
STAMP=$(date +%Y%m%d_%H%M%S)

# 扫描模式: 默认 known(增量), --full 则 full(全量)
SCAN_MODE="known"
[ "$OPT_FULL" = 1 ] && SCAN_MODE="full"
MC_ARGS="--mode $SCAN_MODE --msd $MSD"
[ "$OPT_BITRATE" = 1 ] && MC_ARGS="$MC_ARGS --bitrate"

# 发布: 原子替换(先cp到临时名再mv),避免播放器正好在读时拿到半个文件
publish_m3u() {
    local src="$1" dst="$2"
    cp "$src" "$dst.tmp" && mv -f "$dst.tmp" "$dst"
}

echo "############################################"
echo "# iptv-radar pipeline  $STAMP"
echo "############################################"

# ===== 特殊模式: --gen-only 只从现有库重新生成 m3u+Dashboard+页面(不扫描/不探测/不刷token) =====
# 用途: 改了模板/样式/gen脚本后,几秒内重出静态页,无需重跑扫描。
if [ "$OPT_GEN_ONLY" = 1 ]; then
    echo "# 模式: 仅重新生成(--gen-only, 不扫描/不刷token, 用现有库数据)"
    echo "############################################"
    echo ""; echo ">>> 生成 m3u(msd版 + 直通版)"
    python3 gen_m3u.py --msd "$MSD" "${FCC_ARG[@]}" --multicast-mode msd --out ../output/iptv.m3u
    python3 gen_m3u.py --msd "$MSD" --multicast-mode direct --out ../output/iptv_direct.m3u
    python3 gen_m3u.py --msd "$MSD" "${FCC_ARG[@]}" --multicast-mode msd --prefer-multicast --out ../output/iptv_compat.m3u
    echo ""; echo ">>> 抓EPG + 生成Dashboard + 频道页"
    python3 fetch_epg.py || echo "  EPG抓取失败(继续,Dashboard将无节目单)"
    python3 gen_dashboard.py
    python3 gen_channels_page.py
    if [ "$OPT_PUBLISH" = 1 ]; then
        echo ""; echo ">>> 发布 m3u → $NGINX_M3U_DIR"
        if [ -d "$NGINX_M3U_DIR" ]; then
            publish_m3u ../output/iptv.m3u "$NGINX_M3U_DIR/iptv.m3u"
            publish_m3u ../output/iptv_direct.m3u "$NGINX_M3U_DIR/iptv_direct.m3u"
            publish_m3u ../output/iptv_compat.m3u "$NGINX_M3U_DIR/iptv_compat.m3u"
            echo "  已发布 iptv.m3u + iptv_direct.m3u + iptv_compat.m3u"
        else
            echo "  ⚠️ Nginx目录不存在,跳过: $NGINX_M3U_DIR"
        fi
    fi
    echo ""; echo "# 完成(仅重新生成) $STAMP"
    exit 0
fi

# ===== 特殊模式: --timeshift-only 只跑回看探测+重新生成页面(补数据用,不重扫) =====
# 用途: 补回看天数时,不必重跑整个full。手工触发一次即可。
if [ "$OPT_TIMESHIFT_ONLY" = 1 ]; then
    echo "# 模式: 仅回看探测(--timeshift-only)"
    echo "############################################"
    echo ""; echo ">>> 单播回看天数探测"
    python3 probe_timeshift.py --epg "$EPG_JSON" || echo "  探测出错"
    echo ""; echo ">>> 重新生成页面(体现回看天数)"
    python3 gen_channels_page.py
    python3 gen_dashboard.py
    echo ""; echo "# 完成(仅回看探测) $STAMP"
    exit 0
fi

echo "# 扫描模式: $SCAN_MODE"
echo "############################################"

# -1. 刷新token: EPG认证拉最新channels.json(含新鲜token,单播/回看地址每次都新鲜)。
#     token跟IP无关(CDN不校验绑定IP),但会按签发时间过期→每次pipeline刷一次最稳。
#     成功→后续都用新channels.json;失败→回退历史sample快照(组播不受影响,回看用旧token可能失效)。
echo ""; echo ">>> [刷新token] EPG认证 → channels.json"
if python3 fetch_channels.py; then
    # 只判 -s(非空) 不够: 写到一半被杀的 JSON 也非空,会被下游当好数据用。必须验证能解析。
    if [ -s "$EPG_FRESH" ] && python3 -c "import json,sys;json.load(open(sys.argv[1]))" "$EPG_FRESH" 2>/dev/null; then
        EPG_JSON="$EPG_FRESH"
        echo "  ✅ token已刷新,后续用 channels.json"
    else
        echo "  ⚠️ channels.json 为空或不是合法JSON,回退 sample 快照"
    fi
else
    echo "  ⚠️ EPG认证失败(网络/路由/凭证?),回退 sample 快照(组播不受影响)"
fi

# 0. 消费上次App的孤儿源识别结果(若有) → 写库归并
echo ""; echo ">>> [0/7] 消费孤儿源识别结果(data/orphan_inbox/)"
python3 orphan_import.py || echo "  无识别结果或消费出错(继续)"

# 1. 采集: 组播扫描(三轮递进,零误报)
echo ""; echo ">>> [1/7] 组播扫描 ($SCAN_MODE)"
python3 scan_multicast.py $MC_ARGS || echo "  组播扫描出错(继续)"

# 2. 采集: RTSP单播扫描(追踪重定向链)
echo ""; echo ">>> [2/7] RTSP扫描"
python3 scan_rtsp.py --epg "$EPG_JSON" --trace || echo "  RTSP扫描出错(继续)"

# 2b. 单播回看天数探测(仅full模式,每月一次;天数变化慢,增量不做)
if [ "$SCAN_MODE" = "full" ]; then
    echo ""; echo ">>> [2b] 单播回看天数探测(full模式)"
    python3 probe_timeshift.py --epg "$EPG_JSON" || echo "  回看探测出错(继续)"
fi

# 3. 数据清洗: 源归并到频道(自动: 官方channels.json + source_links.json快照)
# 这两步(归并/ETL)是数据正确性的关键,失败不能"继续"当没事:
# 归并失败会让 sources 关联错乱,ETL失败会让优选/状态是旧的,
# 若继续往下生成,会发布一份基于错数据的m3u。所以显式 FATAL 并退出,让cron能看到失败。
echo ""; echo ">>> [3/7] 数据清洗(归并)"
if ! python3 link_sources.py --epg "$EPG_JSON"; then
    echo "❌ FATAL: 归并失败,已中止(不生成/不发布,保留上一次的m3u)" >&2
    exit 4
fi

# 4. ETL: 源优选(全失效频道不选主源) + 变更检测(下线)
echo ""; echo ">>> [4/7] ETL处理(优选+变更检测)"
if ! python3 etl_process.py; then
    echo "❌ FATAL: ETL失败,已中止(不生成/不发布,保留上一次的m3u)" >&2
    exit 5
fi

# 5. 产出待识别包: 剩余孤儿源导出给App人工识别
echo ""; echo ">>> [5/7] 产出孤儿源待识别包(output/orphan_review/)"
python3 orphan_export.py --msd "$MSD" || echo "  无孤儿源或导出出错(继续)"

# 6. 生成 m3u(两套): msd版(组播转HTTP,远程/Tailscale可用) + direct版(组播直通rtp,LAN内省中转)
echo ""; echo ">>> [6/7] 生成m3u(msd版 + 组播直通版)"
python3 gen_m3u.py --msd "$MSD" "${FCC_ARG[@]}" --multicast-mode msd --out ../output/iptv.m3u
python3 gen_m3u.py --msd "$MSD" --multicast-mode direct --out ../output/iptv_direct.m3u
python3 gen_m3u.py --msd "$MSD" "${FCC_ARG[@]}" --multicast-mode msd --prefer-multicast --out ../output/iptv_compat.m3u

# 7. 生成 Dashboard + EPG
echo ""; echo ">>> [7/7] 抓EPG + 生成Dashboard"
python3 fetch_epg.py || echo "  EPG抓取失败(继续,Dashboard将无节目单)"
python3 gen_dashboard.py
python3 gen_channels_page.py

# 发布(可选)
if [ "$OPT_PUBLISH" = 1 ]; then
    echo ""; echo ">>> 发布 m3u → $NGINX_M3U_DIR"
    if [ -d "$NGINX_M3U_DIR" ]; then
        publish_m3u ../output/iptv.m3u "$NGINX_M3U_DIR/iptv.m3u"
        publish_m3u ../output/iptv_direct.m3u "$NGINX_M3U_DIR/iptv_direct.m3u"
        publish_m3u ../output/iptv_compat.m3u "$NGINX_M3U_DIR/iptv_compat.m3u"
        echo "  已发布 iptv.m3u(msd版) + iptv_direct.m3u(组播直通版) + iptv_compat.m3u(兼容版)"
    else
        echo "  ⚠️ Nginx目录不存在,跳过: $NGINX_M3U_DIR"
    fi
fi

echo ""; echo "############################################"
echo "# 完成 $STAMP  (模式:$SCAN_MODE)"
echo "# m3u:       ../output/iptv.m3u"
echo "# dashboard: ../output/dashboard/index.html"
echo "############################################"
