# iptv-radar

[English](README.md) | [中文](README.zh-CN.md)

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
  db_util.py           # 统一 SQLite 连接(超时 + 启用外键约束)
  seed.py              # 从种子文件重建 channels/分组
  scan_multicast.py    # 组播扫描
  scan_rtsp.py         # RTSP 单播扫描 + 重定向链追踪
  probe.py             # ffprobe 流探测
  link_sources.py      # 数据清洗:源归并到频道
  etl_process.py       # 源优选 + 变更检测 + 引用完整性体检
  orphan_export.py     # 导出待人工识别的孤儿源
  orphan_import.py     # 消费人工识别结果写回库
  gen_m3u.py           # 生成 m3u(三套)
  gen_dashboard.py     # 生成检测 Dashboard(优选源列表)
  gen_channels_page.py # 生成官方频道列表页
  template_util.py     # Jinja2 渲染封装
  templates/           # Dashboard/频道页模板
  fetch_channels.py    # EPG 认证,刷新单播/回看 token
  probe_timeshift.py   # 回看(时移)天数探测
  fetch_epg.py         # 抓取 EPG 节目单
  run_pipeline.sh      # 一键流水线
tests/                 # 回归测试(数据正确性 + 生成层)
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
python3 src/gen_dashboard.py      # 生成 Dashboard
```

## 测试

回归测试锁住那些曾经静默出错的数据正确性约束(跨频道串台、下线判定、
人工归并快照不丢、m3u 与 Dashboard 同序、探测超时)。CI 会在推镜像前先跑一遍。

```bash
pip install -r requirements.txt pytest
python3 -m pytest tests/ -v
```

## 配置

真实的运营商地址、认证凭证、部署路径等通过项目根目录的 `.env` 提供
(不随仓库分发,参考 `.env.example`)。

## 组播网关选择: udpxy vs msd_lite vs rtp2httpd

IPTV 组播多数播放器/网络无法直接消费,需要网关把组播 RTP/UDP 转成 HTTP 单播。
三个常见选择 —— 本项目都支持(m3u 只需要 `http://<主机>:<端口>/rtp/<组播地址>`
这种三者通用的 URL 形式):

| | **udpxy** | **msd_lite** | **rtp2httpd** |
|---|---|---|---|
| 组播 → HTTP | ✅ | ✅ | ✅ |
| 成熟度 | 最老牌,到处都有 | 成熟,各源都打包 | 较新,活跃开发中 |
| 性能 | 单线程,简单 | 多线程,高效 | epoll + 多 worker + 零拷贝 |
| **FCC 快速换台** | ❌ | ❌ | ✅ 电信/中兴/烽火 + 华为 |
| **RTSP → HTTP(回看/点播)** | ❌ | ❌ | ✅ |
| FEC 纠错 / RTP 重排序 | ❌ | ❌ | ✅ (Reed-Solomon FEC + 滑窗重排) |
| 自定义 HTTP 头(CORS) | ❌ | ✅ 通过 `headersList` | ✅ 通过 `--cors-allow-origin` |
| 状态页 / 网页播放器 | ❌ | 基础状态 | ✅ `/status` + `/player` + 截图 |
| 依赖 | 极小 C | 极小 C | 极小 C(零依赖) |
| OpenWRT 包 | ✅ | ✅ | ✅(可能需手动 ipk) |

**选择建议** —— 纯组播转 HTTP 用哪个都行。如果想要**更快的换台(FCC)**、**HTTP 回看**、
或**弱网抗丢包**,选 **rtp2httpd**;它的 URL 格式是 udpxy/msd_lite 的 drop-in 替代,
现有播放列表不用改就能直接用。**msd_lite** 依然是稳妥、久经考验的选择(也支持自定义
HTTP 头,如网页播放器需要的 CORS)。

**CORS 什么时候重要**:在**浏览器网页播放器**里播放时,网关响应需要带
`Access-Control-Allow-Origin`,否则跨域 fetch 会被浏览器拦截。
- msd_lite: 在配置的 `headersList` 里加 `<header>Access-Control-Allow-Origin: *</header>`
- rtp2httpd: `option cors_allow_origin '*'`(UCI)或 `--cors-allow-origin '*'`

### rtp2httpd 的 FCC 快速换台

FCC 会先向运营商 FCC 服务器请求一段单播补帧(IDR 帧 + 初始数据)让画面立刻出来,
然后无缝切到组播。URL 后面加 FCC 服务器即可:

```
http://<网关>:<端口>/rtp/<组播地址>:<端口>?fcc=<FCC服务器IP>:<FCC端口>
```

**怎么找自己的 FCC 服务器**:抓机顶盒的包,找 **RTCP payload type 205
(Generic RTP Feedback)、FMT=5 (RTCP-SR-REQ)** 的包 —— 目标地址就是 FCC 服务器。
不认识 `?fcc=` 的网关会直接忽略该参数,所以同一份播放列表仍然通用。本项目里 FCC 服务器
配在 `.env` 的 `FCC_SERVER`,由 `gen_m3u.py --fcc` 写入 m3u。

## 进阶: OpenWRT 让 IPTV 组播在 LAN 内直接播放

默认组播要经网关转成 HTTP 单播,软路由要为每个观看者做一路转发 —— 是 CPU 瓶颈。
若软路由是 OpenWRT,可让 LAN 设备(IINA/APTV 等)**直接收组播 RTP**
(`rtp://@<组播地址>`),绕过网关中转。原理是用 IGMP proxy 把 IPTV 上游接口的组播
按需转发到 LAN 网桥。

> `<IPTV接口>`/`<组播源网段>` 请替换为你自己的实际值。
> 组播只在**同一局域网**内可达;跨 VPN(如 WireGuard/Tailscale)不转发组播,远程仍需 HTTP 网关那套。

**1. 装 igmpproxy**(比自带的 omcproxy 更适合 IPv4 IPTV)
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

**4. LAN 网桥的 IGMP snooping**
```sh
uci set network.@device[0].igmp_snooping='0'   # 对应 br-lan 的 device;0=泛洪(本项目实测可用),1=精准投递但需 querier
uci commit network
```
(`1` 只投递给 join 过的端口,对 Wi-Fi 设备更友好;`0` 泛洪在千兆 LAN 也没问题。
两种方式下都只有真正被观看的组才会从上游拉流。)

**5. 重载并验证**
```sh
/etc/init.d/igmpproxy restart; /etc/init.d/firewall reload; /etc/init.d/network reload
ip mroute show   # 观看时应出现 (源, 组) Iif:<IPTV接口> Oifs:lan State:resolved
```
LAN 设备用播放器打开 `rtp://@<组播地址>` 即可直接观看。

**排查提示**(按此顺序,逐步用证据定位):
- 设备发不出 IGMP join → 检查客户端。macOS 上 OrbStack / Docker Desktop 会创建虚拟网桥,
  干扰主机组播 join 的接口选择;OrbStack 里关掉 *"Allow access to container domains & IPs"*
  即可恢复(无需退出 Docker)。
- `ip mroute show` 无 resolved 条目 → igmpproxy 上游 / altnet / scope 配置。某些运营商用
  organization-local 组播(`233.x`),而 igmpproxy 默认只代理 global scope。
- 有 resolved 但设备收不到、物理口也抓不到包 → **十有八九是防火墙 forward 拦了(见第3步)**。
- 高并发探测大量失败: 单台设备同时接收多路组播有上限(运营商 CPAR 限速、snooping 表压力),
  实测 **4 路并发是甜点**。

## 生成的三套播放列表

流水线会生成三套 m3u,分别适配不同播放场景:

| m3u | 组播源 | 单播/回看 | 适用 |
|-----|--------|-----------|------|
| `iptv.m3u` 标准版 | `http://<网关>/rtp/…`(配了 FCC 则带 `?fcc=`) | 可回看频道用单播主源 + catchup 标签 | 远程 / Tailscale / 支持回看的原生播放器(如 APTV) |
| `iptv_direct.m3u` 直通版 | `rtp://@…`(组播直收) | 同标准版 | 仅 LAN 内、低延迟无中转(如 IINA) |
| `iptv_compat.m3u` 兼容版 | `http://<网关>/rtp/…`(带 `?fcc=`) | 有组播源的一律回退组播(无回看),纯单播频道保留 rtsp | 只支持组播转 HTTP、不支持 rtsp 的播放器(如网页播放器) |

对应 `gen_m3u.py` 参数: `--multicast-mode msd|direct` 控制组播源形式,
`--prefer-multicast` 生成兼容版,`--fcc <ip:port>` 加 FCC。

## License

个人自用项目,仅供学习交流。
