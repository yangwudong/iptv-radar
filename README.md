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

# 流水线模式(均可加 --publish 发布 m3u):
./src/run_pipeline.sh                  # 默认: known 增量扫描(只扫已知源,~11分钟,适合每周)
./src/run_pipeline.sh --full           # 全量扫描(全网段+回看探测,~20分钟,适合每月/初始化)
./src/run_pipeline.sh --timeshift-only # 只补回看天数数据(不重扫,~5分钟)
./src/run_pipeline.sh --gen-only       # 只从现有库重新生成 m3u+Dashboard+页面(改模板/样式后用,几秒)

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

## 进阶: OpenWRT 让 IPTV 组播在 LAN 内直接播放

默认组播需经 `udpxy`/`msd_lite` 转 HTTP 单播,软路由要为每个观看者做一路转码,
是 CPU 瓶颈。若软路由是 OpenWRT,可让 LAN 设备(IINA/APTV 等)**直接收组播 RTP**
(`rtp://@<组播地址>`),绕过转码中转。原理是用 IGMP proxy 把 IPTV 上游接口的组播
按需转发到 LAN 网桥。

> 以下为通用步骤,`<IPTV接口>`/`<LAN接口>`/`<组播源网段>` 请替换为你自己的实际值。
> 组播只在**同一局域网**内可达;跨 VPN(如 WireGuard/Tailscale)不转发组播,远程仍需 HTTP 转码那套。

**1. 装 igmpproxy(比自带的 omcproxy 更适合 IPv4 IPTV)**
```sh
opkg update && opkg install igmpproxy
```

**2. 配 igmpproxy: 上游=IPTV接口, 下游=LAN**  (`/etc/config/igmpproxy`)
```
config igmpproxy
    option quickleave 1

config phyint
    option network   <IPTV接口>      # 组播上游(如 IPTV 的 VLAN 接口)
    option direction upstream
    list   altnet    <组播源网段>    # 允许接收组播的源网段,如 x.x.0.0/16
config phyint
    option network   lan             # LAN 下游
    option direction downstream
```
注意上游用**真实接口/VLAN 子接口**,不要用网桥(`br-lan`)。

**3. ⚠️ 关键: 放行防火墙的组播转发(最容易漏、最难查的坑)**

IPTV 所在防火墙 zone 的 `forward` 若是 `REJECT/DROP`(常见默认),转发的组播 UDP
会被静默丢弃 —— 现象是 `ip_mr_vif` 里下游计数在涨(内核以为转发了),但设备/物理口
上抓不到数据包。**加一条允许 IPTV→LAN 组播 UDP 的规则**:
```
config rule
    option name   'Allow-IPTV-Multicast-to-LAN'
    option src    '<IPTV_zone>'
    option dest   'lan'
    option proto  'udp'
    option dest_ip '224.0.0.0/4'
    option target 'ACCEPT'
```

**4. LAN 网桥关闭 IGMP snooping(泛洪,最省心)**
```sh
uci set network.@device[0].igmp_snooping='0'   # 对应 br-lan 的 device
uci commit network
```
(千兆 LAN 泛洪压力可忽略;按需 join 时只有被观看的组会真正拉流。)

**5. 重载并验证**
```sh
/etc/init.d/igmpproxy restart; /etc/init.d/firewall reload; /etc/init.d/network reload
ip mroute show          # 观看时应出现 (源, 组) Iif:<IPTV接口> Oifs:lan State:resolved
```
LAN 设备用播放器打开 `rtp://@<组播地址>` 即可直接观看。

**排查提示**(按此顺序,逐步用证据定位):
- 设备发不出 IGMP join → 检查客户端(macOS 上 OrbStack/Docker Desktop 会创建虚拟网桥,
  干扰主机组播 join 的接口选择;OrbStack 里关掉 "Allow access to container domains & IPs"
  即可恢复,无需退出 Docker);
- `ip mroute show` 无 resolved 条目 → igmpproxy 上游/altnet/scope 配置(某些运营商组播是 organization-local,igmpproxy 默认只代理 global);
- 有 resolved 但设备收不到、物理口抓不到包 → **十有八九是防火墙 forward 拦了(见第3步)**。

本仓库的 `gen_m3u.py --multicast-mode direct` 会生成组播直通版 m3u(`rtp://@...`),
`--multicast-mode msd`(默认)生成经转码的兼容版,两套并存分别用于 LAN 直连 / 远程。

## License

个人自用项目,仅供学习交流。
