# iptv-radar

浙江电信 IPTV 频道 **扫描 · 管理 · 发布** 系统。

自动扫描组播/单播频道质量,以 **SQLite 为唯一主数据源**,生成带台标 /EPG 的
m3u 播放列表 + 频道监控 Dashboard。

> ⚠️ 本项目为个人自用的逆向研究与自动化工具,仅供学习交流。所涉及的组播/单播地址
> 为运营商公开分发信息,不含任何个人账号信息(认证凭证等敏感配置通过 `.env` 隔离,
> 不随仓库分发)。

## 架构(三层解耦)

```
采集(scan) → SQLite主库 → 加工(ETL:归并/优选) → 生成(m3u+Dashboard) → 发布
```

- **采集** 扫描组播/单播源,只写事实(技术属性),不做命名/优选
- **加工** 归并源到频道(link_sources) + 源优选(etl_process)
- **生成** m3u 播放列表 + 检测 Dashboard + 官方频道列表页 + EPG 节目单

## 核心数据模型

以 SQLite 为唯一主数据源,三张核心表职责严格解耦:

| 表 | 职责 |
|----|------|
| `channels` | 频道元数据台账(名/台标/EPG-id/分组),只增不删 |
| `sources` | 采集到的播放源清单(组播/单播),进表≠已识别 |
| `channel_preferred_sources` | 优选关系(ETL 产出,带 rank,支持一频道多源按画质排序) |

设计要点:

- **主键用稳定代理键 `channel_id`(自增整数)**,频道改名不影响关联
- `channel_key`(规范频道名)作 UNIQUE 列,人可读、可沟通、作台标匹配入口
- 源失效不删,标记 `available=0`/`fail_count`;频道无源可用仍保留(标 offline)
- 任意源地址 → channel_id → 频道全部信息(名/台标/EPG),台标匹配走这条路

## 目录结构

```
src/
  db_schema.py         # 权威建库 schema
  scan_multicast.py    # 组播扫描
  scan_rtsp.py         # RTSP 单播扫描 + 重定向链追踪
  probe.py             # ffprobe 流探测
  link_sources.py      # 数据清洗:源归并到频道
  etl_process.py       # 源优选 + 变更检测
  gen_m3u.py           # 生成 m3u
  gen_dashboard.py     # 生成检测 Dashboard(优选源列表)
  gen_channels_page.py # 生成官方频道列表页
  fetch_epg.py         # 抓取 EPG 节目单
  run_pipeline.sh      # 一键流水线
docs/                  # 设计文档与工作原理
reference/             # 官方频道样例、台标索引
data/                  # SQLite 主库(不随仓库分发)
output/                # 生成产物(m3u/dashboard)
```

## 使用

```bash
# 建库(首次)
python3 src/db_schema.py

# 一键流水线: 采集 → 清洗 → 优选 → 生成
./src/run_pipeline.sh

# 或分步执行
python3 src/scan_multicast.py     # 组播扫描
python3 src/scan_rtsp.py --trace  # 单播扫描
python3 src/link_sources.py       # 归并
python3 src/etl_process.py        # 优选
python3 src/gen_m3u.py            # 生成 m3u
python3 src/gen_dashboard.py     # 生成 Dashboard
```

组播源需通过 `udpxy`/`msd_lite` 转 HTTP 播放。Dashboard 支持在页面顶部
填入你的转码前缀(存 localStorage),用于 IINA 播放。

## 配置

真实的运营商地址、认证凭证、部署路径等通过项目根目录的 `.env` 提供
(不随仓库分发)。参考各脚本的默认参数与命令行选项。

## License

个人自用项目,仅供学习交流。
