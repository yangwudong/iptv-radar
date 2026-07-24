# 浙江电信IPTV工具集 — 技术规格文档

> 最后更新: 2026-06-22
> 环境: 杭州电信宽带+IPTV / OpenWRT 25.12 / msd_lite / HS8125C光猫(桥接模式)

---

## 一、背景

### 问题描述
浙江电信IPTV的组播频道分三个网段:
- `233.50.200.x` — 部分频道可用(用户扫描发现)
- `233.50.201.x` — 大部分频道可用(EPG官方地址)
- `233.50.202.x` — 本区域组播不通(4K卫视、凤凰、部分地方台)

`202.x`频道不通的原因不是IGMP版本问题,而是**BRAS/OLT未推送这批组播流到本区域**。但通过EPG认证后可以获取RTSP单播地址,绕过组播限制观看这些频道。

### 解决方案
```
200.x/201.x频道 → msd_lite组播转HTTP (稳定,低延迟)
202.x频道      → EPG认证获取RTSP单播地址 (通过115.233.40.x服务器)
                → 开放防火墙+路由让LAN设备可达
                → 生成两套m3u(组播+单播)配合APTV聚合使用
```

---

## 二、网络配置

> **⚠️ 接线方式变更(2026-07)**: IPTV接入已从"USB网卡独立口(eth2)"迁移到
> "板载千兆口eth1的VLAN 43子接口(eth1.43),与WAN共享物理线路"。
> 原因: USB网卡(AX88179)协商100M(实测上限89Mbps),换到千兆eth1彻底解决带宽瓶颈。
> 光猫接eth1的口透传 WAN(PPPoE untag) + IPTV(VLAN43 tag)。详见 IPTV_KnowledgeBase.md。

### 2.1 OpenWRT接口配置(当前: VLAN方式)
```
# VLAN 43 子接口设备
config device
    option type '8021q'
    option ifname 'eth1'
    option vid '43'
    option name 'eth1.43'

# IPTV接口(走eth1.43)
config interface 'zIPTV'
    option proto 'dhcp'
    option device 'eth1.43'       # 板载千兆口eth1的VLAN43子接口
    option macaddr '<stb_mac>'    # 克隆机顶盒MAC
    option hostname '<STBID>'
    option sendopts '0x3c:<hex>'  # DHCP Option 60 IPoE认证
    option delegate '0'
    option multipath 'off'
    option metric '200'
    option defaultroute '0'
    option peerdns '0'
```

**注意**: 
- `option defaultroute '0'` 禁止DHCP默认路由(避免和PPPoE冲突)
- `option peerdns '0'` 禁止用IPTV的DNS
- `option multipath 'off'` 避免干扰手动路由
- 认证参数(macaddr/hostname/sendopts)迁移时原样保留,只改device

**历史方案 — USB网卡(eth2, 已弃用)**:
之前用USB千兆网卡(AX88179)接光猫LAN2口做独立IPTV接口(device='eth2')。
瓶颈: 该软路由(Atom J1900)USB控制器只有USB2.0(EHCI,无USB3.0),
网卡协商100M,单条RTSP流限速~42Mbps,多路并发实测上限~89Mbps(100M网口满速)。
迁移到eth1.43后可拔掉此USB网卡。

**备选方案 — 静态IP配置**:
```ini
config interface 'zIPTV'
    option proto 'static'
    option device 'eth1.43'
    option ipaddr '10.235.140.44'      # 用DHCP分配过的IP
    option netmask '255.255.224.0'      # /19,对应DHCP分配的掩码
    option macaddr '<stb_mac>'
    option hostname '<STBID>'
    option sendopts '0x3c:00001f39...'   # Option 60
    option delegate '0'
    option metric '200'
```
静态IP的优点: 网关固定,路由不会因IP变化而失效。缺点: 如果该IP被BRAS回收或冲突,需要换IP。

### 2.2 防火墙配置

#### IPTV Zone
```ini
config zone
    option name 'IPTV'
    option network 'zIPTV'
    option input 'ACCEPT'
    option output 'ACCEPT'
    option forward 'REJECT'
    option masq '1'        # ← 必须开启!NAT伪装让LAN设备能访问IPTV网络
    option mtu_fix '1'
```

**`masq=1`的作用**: LAN设备(如Mac)访问EPG服务器(115.233.40.140)时,OpenWRT将源IP从LAN IP替换为IPTV IP。没有NAT伪装,EPG服务器看到的源IP是LAN内网IP,认证会失败。

#### LAN→IPTV转发规则
```ini
config forwarding
    option src 'lan'
    option dest 'IPTV'
```

**方向必须是 lan→IPTV**(不是IPTV→lan)。这条规则允许LAN设备主动发起连接到IPTV网络。IPTV网络不能主动连LAN(安全)。

**完整配置命令**:
```bash
# IPTV zone开启masquerading
uci set firewall.cfg05dc81.masq='1'
uci set firewall.cfg05dc81.mtu_fix='1'

# 添加lan→IPTV转发
uci add firewall forwarding
uci set firewall.@forwarding[-1].src='lan'
uci set firewall.@forwarding[-1].dest='IPTV'

uci commit firewall
service firewall restart
```

### 2.3 路由配置

#### 问题背景
EPG服务器(115.233.40.140:33200)和RTSP服务器(115.233.40.137)在IPTV专网上,不在公网。LAN设备默认走PPPoE出口,到不了这些服务器。需要添加路由让这些IP走IPTV接口(eth2)。

#### 需要路由的IP段
| 目标网络 | 掩码 | 用途 |
|----------|------|------|
| 115.233.40.0 | 255.255.248.0 (/21) | EPG服务器(40.140) + RTSP入口服务器(40.137/40.206) + 流媒体服务器(45.x) |
| 220.191.136.0 | 255.255.255.0 (/24) | EDS认证(136.23) + RTSP重定向服务器(136.24) |
| 122.229.6.0 | 255.255.255.0 (/24) | 4K/高清频道流媒体服务器(6.193/6.195),重定向链终点 |

**注意**: `122.229.6.0/24`是后来发现的 — 4K频道的RTSP重定向链最终会到这里。如果不加这个路由,4K频道会超时(流量走了PPPoE公网而不是IPTV专网)。

#### RTSP重定向链分析

不同频道的RTSP重定向深度不同:

**普通频道(1跳,稳定):**
```
115.233.40.137 → 115.233.45.135 → 200 OK → 播放
```

**4K频道(4跳,容易出问题):**
```
115.233.40.137 → 220.191.136.24 → 115.233.41.137 → 220.191.136.24 → 122.229.6.193 → 200 OK → 播放
```

**故障模式 — 重定向死循环:**
```
115.233.40.137 → 220.191.136.24 → 115.233.40.137 → 220.191.136.24 → ... (无限循环)
```
原因: 220.191.136.24(重定向服务器)没有正确分配流媒体服务器,而是把请求踢回给入口服务器。

**另一个入口: 115.233.41.137**
myepg.org上的RTSP地址用的是115.233.41.137(不是40.137)。41.137也在路由范围内(/21覆盖40.0-47.255),但重定向链可能不同。

#### 方案A: hotplug脚本(推荐,适配动态IP+动态设备名)
脚本路径: `/etc/hotplug.d/iface/99-iptv-routes`

```sh
#!/bin/sh
[ "$ACTION" = "ifup" ] || exit 0
[ "$INTERFACE" = "zIPTV" ] || exit 0
sleep 3

# 动态获取zIPTV实际设备名(eth2/eth1.43都自适应)
DEV=$(ubus call network.interface.zIPTV status 2>/dev/null | jsonfilter -e '@.l3_device')
[ -z "$DEV" ] && DEV="eth1.43"

GATEWAY=$(ubus call network.interface.zIPTV status 2>/dev/null | jsonfilter -e '@.inactive.route[0].nexthop')
[ -z "$GATEWAY" ] && GATEWAY=$(ubus call network.interface.zIPTV status 2>/dev/null | jsonfilter -e '@.data.dhcpserver')

if [ -n "$GATEWAY" ]; then
    for net in 115.233.40.0/21 220.191.136.0/24 122.229.6.0/24; do
        ip route del $net 2>/dev/null
        ip route replace $net via "$GATEWAY" dev "$DEV"
    done
    logger "iptv-routes: routes added via $GATEWAY dev $DEV"
else
    logger "iptv-routes: no gateway found"
fi
```

**关键改动(演进历史)**:
- **v3(2026-07,VLAN迁移后)**: `dev` 不再写死,改为动态获取 `l3_device`(自适应eth2→eth1.43)。切VLAN后设备名从eth2变成eth1.43,写死会导致单播RTSP路由失效(组播正常但4K单播播不了)
- v2: `ip route del` 不带 `dev`(才能删掉pppoe-wan的竞争路由);`ip route replace`(不是add,强制覆盖);新增 `122.229.6.0/24`(4K流媒体服务器)

#### 方案B: OpenWRT Web UI静态路由(静态IP场景)
LuCI → 网络 → 接口 → zIPTV → 路由 → 添加:

| 目标 | 掩码 | 网关 | 类型 |
|------|------|------|------|
| 115.233.40.0 | 255.255.248.0 | <IPTV网关> | unicast |
| 220.191.136.0 | 255.255.255.0 | <IPTV网关> | unicast |

**注意**: 路由类型选 **unicast**。网关填写IPTV网络的网关(当前 10.235.128.1,取决于DHCP分配的IP所在子网)。

#### 方案C: uci命令行配置
```bash
uci set network.iptv_rtsp1=route
uci set network.iptv_rtsp1.interface='zIPTV'
uci set network.iptv_rtsp1.target='115.233.40.0'
uci set network.iptv_rtsp1.netmask='255.255.248.0'
uci set network.iptv_rtsp1.gateway='<网关IP>'

uci set network.iptv_eds=route
uci set network.iptv_eds.interface='zIPTV'
uci set network.iptv_eds.target='220.191.136.0'
uci set network.iptv_eds.netmask='255.255.255.0'
uci set network.iptv_eds.gateway='<网关IP>'

uci commit network
service network restart
```

**注意**: 静态路由(方案B/C)的网关是写死的。如果DHCP IP换了子网,网关就错了,路由失效。所以推荐方案A(hotplug动态适配)。

### 2.4 IPoE认证参数
```
DHCP Option 12 (Hostname): <STBID>
  格式: 固定前缀 + IPTV账号 + MAC地址

DHCP Option 60 (Vendor Class ID): 00001F39...
  结构: 
    00 00 1F          // 固定3字节
    37                // 内容长度
    64                // 固定0x64
    [8字节 random]    // 随机数
    [8字节 timestamp] // 时间戳
    [16字节 HashKey]  // MD5(random + 6字节密码 + timestamp)
    [22字节 USERID]   // tvxxxx@itv格式
```

---

## 三、脚本说明

### 3.1 epg_client.py — EPG认证与频道获取
**功能**: 模拟机顶盒EPG认证流程,获取官方频道列表(含组播地址和RTSP单播地址)

**认证流程**:
```
1. GET  115.233.40.140:33200/EPG/jsp/AuthenticationURL → 获取EncryptToken
2. POST /EPG/jsp/authLoginHWCTC.jsp → 登录
3. POST /EPG/jsp/ValidAuthenticationHWCTC.jsp → 带Authenticator验证
   Authenticator = DES-ECB加密("99999$TOKEN$USERID$STBID$IP$MAC$$CTC", key="00000000")
4. POST /EPG/jsp/getchannellistHWCTC.jsp → 获取频道列表
```

**内置纯Python DES实现**: 不依赖pycryptodome,可在OpenWRT裸奔

**关键配置**(写在脚本顶部):
```
USERID = "<USERID>"
STBID = "<STBID>"
MAC = "<STB_MAC>"
DES_KEY = "00000000"
```

**命令行**: `python3 epg_client.py --ip <IPTV_IP>` (从Mac运行时需指定OpenWRT的IPTV IP)

### 3.2 find_key.py — DES密钥爆破
爆破3DES Authenticator的8位密钥。本例密钥为 `00000000`。
仅用于首次获取密钥,后续不需要重复运行。

### 3.3 scan_channels.py — EPG频道扫描
扫描EPG频道列表中每个频道的媒体信息(分辨率/编码/帧率/音频)。
- 并发: 8线程
- 超时: 6秒/频道
- 失败重试: 1次
- 通过msd_lite HTTP接口扫描(不直接收组播)

### 3.4 scan_full.py — 全量IP扫描
扫描 `233.50.200.0/24` + `233.50.201.0/24` 共512个IP。
发现EPG和用户m3u中都没有的未知频道。
- 并发: 10线程
- 超时: 5秒/IP

### 3.5 merge_m3u.py — M3U合并工具(核心)
合并多个数据源生成最终播放列表。

**输入数据源**:
1. `channels.json` — EPG认证获取(官方频道名+组播IP+RTSP地址)
2. `m3u/china_telecom_tv.m3u` — 用户整理(分组+台标+额外频道)
3. `m3u/Zhejiang_Telecom_IPTV.m3u` — myepg.org参考(台标+命名)
4. `scan_full.json` / `scan_full_2.json` — 全量扫描(分辨率+未知频道)
5. `scan_results.json` — EPG频道扫描

**输出**:
- `merged_multicast.m3u` — 组播版(普通频道用组播,4K频道用RTSP)
- `merged_unicast.m3u` — 单播版(全部RTSP)

### 3.6 get_iptv.sh — 一键获取
Shell包装脚本: 自动获取IPTV IP → 运行epg_client.py → 生成播放列表。

---

## 四、M3U合并规则

### 4.1 频道命名规则

#### CCTV频道(无空格,匹配EPG节目单)
| EPG原名 | 输出名 | tvg-id |
|---------|--------|--------|
| 中央一套高清 / 中央一套 | CCTV1综合 | CCTV1 |
| 中央二套高清 / 中央二套 | CCTV2财经 | CCTV2 |
| ... | ... | ... |
| 中央十七套 | CCTV17农业农村 | CCTV17 |
| 中央奥运4K | CCTV16奥林匹克4K | CCTV16 |
| 中央奥运测试 | CCTV16奥林匹克 | CCTV16 |

**规则**: CCTV频道名不能有空格(APTV节目单匹配要求),tvg-id为CCTV+数字。

#### 卫视频道(去掉"高清"后缀,合并HD/SD)
| EPG原名 | 输出名 |
|---------|--------|
| 浙江卫视高清 + 浙江卫视 | 浙江卫视 |
| 北京卫视高清 + 北京卫视 | 北京卫视 |

#### 4K频道(独立条目)
4K版本和普通版是独立频道:
- 浙江卫视 (1080P组播 + RTSP) → 卫视组
- 浙江卫视4K (RTSP单播) → 4K超高清组

**特例**: 多彩文体4K使用4K组播源(233.50.200.52),不用RTSP。

#### 频道改名映射
通过 `NAME_OVERRIDES` 字典配置。改名的频道,`tvg-id`从新名推导。

### 4.2 分组规则
分组优先级(从上到下):
```
1. 央视          — CCTV系列
2. 4K超高清      — 名称含"4K"的频道
3. 卫视          — 各省卫视
4. 浙江          — 浙江本地频道
5. 上海          — SiTV/BesTV/上海频道
6. 湖南          — 快乐垂钓/茶频道
7. 少儿          — 少儿/卡通频道
8. 睛彩          — 睛彩系列
9. 其他          — 杂项
10. 广播         — 纯音频频道
11. 教育         — 教育频道
12. 央视国际     — CGTN系列
13. 未识别       — 扫描发现但未知名称的频道
```

**4K强制规则**: 任何名称含"4K"的频道,无论原分组是什么,强制进入"4K超高清"组。

### 4.3 排序规则
- 分组间: 按4.2的优先级排序
- 分组内: 自然数字排序(CCTV1 < CCTV2 < ... < CCTV13,不是CCTV1 < CCTV11 < CCTV2)
- 排序key: `(group_index, first_number_in_name, display_name)`

### 4.4 源选择规则

#### 组播m3u (merged_multicast.m3u)
```
4K频道:
  1. 优先4K组播源(扫描确认3840x2160的) — 如233.50.200.52
  2. 没有4K组播源 → 用RTSP(202.x组播不通)
非4K频道:
  1. EPG官方组播(201.x) — 最稳定
  2. 用户组播(200.x) — 备选
```

#### 单播m3u (merged_unicast.m3u)
```
所有频道: RTSP单播地址
```

### 4.5 黑名单
```python
BLACKLIST_NAMES = {'4K测试1', '信号测试2', '信号测试3', '好享购', '好易购1高清', '好易购1',
                   '中国教育一套', '中国教育四套', 'BesTV 未知'}
BLACKLIST_IPS = {'233.50.201.225', '233.50.201.224', '233.50.201.226', '233.50.201.227'}
```
- 黑名单频道完全不输出到m3u,黑名单IP的源被过滤。**数据库(SQLite)不受影响,仍保留记录**。
- **子串匹配**: `any(b in display for b in BLACKLIST_NAMES)`,所以可用前缀屏蔽一批。
  例: `'BesTV 未知'` 一条即屏蔽全部 `BesTV 未知1~15`(未识别的BesTV点播源)。

### 4.6 台标规则
1. `LOGO_OVERRIDES` 字典 — 手动覆盖(最高优先级,如多彩文体4K→欢笑剧场4K)
2. `resolve_logo()` — GitHub图床自动匹配(主方案),多级fallback + 名字规范化
3. 保底 — 原myepg logo(基本已失效)

**台标图床迁移(2026-07)**:
- **myepg.org 台标路径变更(不是挂了)** — 台标目录从旧的 `/Zhejiang_Telecom_IPTV/Logo/` 迁到了 `/Logo/`。旧路径返回HTML(SPA兜底页,52417字节)导致全部死链;`/Logo/` 新路径正常返回图片。之前m3u用的是旧路径,所以CETV1/CETV4等靠tvg-logo拉图的频道台标不显示。myepg有Cloudflare但不拦正常请求。
- **新方案**: myepg.org官方源为主 + CCSH(jsdelivr)兜底。优先级:
  | 库 | 数量 | URL前缀 | 说明 |
  |----|------|---------|------|
  | myepg.org (主) | 132 | `https://myepg.org/Logo/` | 浙江电信本地区专属,命名最贴合。判断清单用 LionixQ/Zhejiang_Telecom_IPTV 仓库(=myepg同批图) |
  | CCSH/IPTV (补) | 15540 | `cdn.jsdelivr.net/gh/CCSH/IPTV@main/logo/` | myepg没有的兜底 |
  | fanmingming/live | 929 | `cdn.jsdelivr.net/gh/fanmingming/live@main/tv/` | 第三备选 |
- **名字规范化**(`_logo_name_variants`): 去`_上海`/`_湖南`后缀、去空格、4K频道去后缀复用普通台标、CCTVn取主体、`中国教育N台→CETVN`、地方台去"新闻综合"、SiTV系加前缀。命中率约76%(122/160),核心频道100%。生成结果:112个用myepg官方源,17个用CCSH补全。
- **图床清单缓存**: `logo_index.json`(脚本运行时查表,不联网)。含CCSH/fanmingming/LionixQ三库文件名。若要更新,重新拉GitHub API即可。
- **缺失台标**(约40个): BesTV点播系、FM广播、少数小地方台(中国蓝直播/衢州等),图床本身没有,留空让APTV用内置或不显示。

### 4.7 APTV使用方式
1. 两个m3u都添加为APTV配置
2. 开启**聚合配置** — APTV按频道名自动合并多源
3. 播放失败时自动切换备选源

---

## 五、RTSP单播说明

### 5.1 RTSP服务器
- 入口: `115.233.40.137:554`
- 实际流媒体: 302重定向到 `115.233.45.x:554`

### 5.2 RTSP URL格式
```
完整版(带认证): rtsp://115.233.40.137/PLTV/88888913/224/xxx/yyy.smil?accountinfo=...&it=...
简化版(可用):  rtsp://115.233.40.137/PLTV/88888913/224/xxx/yyy.smil
```

简化版去掉了.smil后面的所有参数。直播频道IP白名单认证,不需要token。m3u中使用简化版(静态地址,不需要刷新)。

### 5.3 时移/回看(TODO)
- 97个频道支持时移,窗口2-4小时
- RTSP服务器返回 `Timeshift-Status: 1` 确认支持
- 直接Range向过去seek不生效(服务器忽略)
- 需要抓取机顶盒暂停/回看时的RTSP交互来研究具体机制
- 详见 `TODO_时移回看.md`

### 5.4 APTV播放RTSP单播卡顿排查(2026-07)

**现象**: APTV 1.5.8(83) 播放4K RTSP单播频道(如北京卫视4K,rtsp://.../128124469.smil)卡顿,换IINA不卡。

**排查过程(路由器侧全部正常)**:
- 到RTSP服务器 115.233.40.137 延迟3ms、0%丢包,路由正确走eth2
- 该频道实测码率 **~32Mbps**(4K@50fps HEVC Main10 HLG HDR,超高码率)
- eth2(USB网卡 AX88179)瓶颈: 协商100M + 只能跑在**USB2.0总线**(这台Atom Z3xxx软路由只有EHCI,无USB3.0控制器),实测拉流上限**~42Mbps**。带宽够但紧。
- link抖动是29天前历史,已排除;无QoS/限速规则

**根因(已确认)**: **APTV的"实验性-组播FCC优化"功能干扰了RTSP单播流**。
- FCC(Fast Channel Change快速换台)是给**组播**用的,对`rtsp://`单播流无意义
- 这个**实验性**功能对单播流有bug,导致卡顿
- **关闭"组播FCC优化"后恢复流畅** ✅

**结论/经验**:
- 播RTSP单播时,APTV的"组播FCC优化"必须**关闭**
- RTSP over TCP建议开启(UDP丢包不重传,带宽紧时易花屏;IINA默认用TCP)
- APTV 1.5.8改动了缓冲逻辑和HEVC硬解(见更新日志"修复缓冲不均衡""修复HEVC硬解格式描述异常"),这也是"更新后才卡"的背景
- USB网卡在这台软路由锁死USB2.0(42Mbps),要根治带宽需换有USB3.0/多千兆口的硬件,把IPTV接板载千兆口(eth0/eth1)。但既然关FCC后流畅,暂不需要

---

## 六、已知问题与限制

1. **组播不稳定**: 三次扫描结果149/170/173,仅约40%频道稳定可用
2. **DHCP动态IP**: IPTV IP每次可能变化,导致网关变化,hotplug脚本处理
3. **RTSP token时效**: 完整版RTSP URL有时效,m3u中使用简化版规避
4. **EPG Option 60**: DHCP认证参数可能过期(目前稳定)
5. **61个未识别频道**: 需要截图+AI识别台标
6. **分组清理**: "BesTV"、"央视教育"等小分组待合并到标准分组

---

## 七、文件清单

```
/Users/<user>/Downloads/IPTV/
├── epg_client.py          # EPG认证+频道获取
├── find_key.py            # DES密钥爆破
├── scan_channels.py       # EPG频道扫描
├── scan_full.py           # 全量IP扫描(200.x+201.x)
├── merge_m3u.py           # M3U合并工具
├── get_iptv.sh            # 一键获取脚本
├── test_timeshift.py      # 时移测试(TODO)
├── IPTV_KnowledgeBase.md  # 知识库: 家庭网络架构/拓扑/光猫软路由关系/组播单播原理
├── IPTV_202x_Analysis.md  # 根因分析报告
├── TODO_时移回看.md        # 待办: 时移/回看+LAN组播
├── SPEC.md                # 本文档
├── logo_index.json        # 台标图床文件名清单缓存
├── channels.json          # EPG频道列表
├── epg_raw_response.txt   # EPG原始响应
├── scan_results.json      # EPG扫描结果
├── scan_full.json         # 全量扫描结果(最新)
├── scan_full_2.json       # 全量扫描结果(第二次)
├── merged_multicast.m3u   # 合并输出: 组播版
├── merged_unicast.m3u     # 合并输出: 单播版
└── m3u/
    ├── china_telecom_tv.m3u       # 用户整理的m3u
    └── Zhejiang_Telecom_IPTV.m3u  # myepg.org的m3u
```

### OpenWRT上的文件
```
/root/epg_client.py               # EPG认证脚本副本
/root/scan_channels.py            # 扫描脚本副本
/etc/hotplug.d/iface/99-iptv-routes     # IPTV路由自动添加
```

---

## 八、修改指南

### 添加频道改名
编辑 `merge_m3u.py` 的 `NAME_OVERRIDES` 字典:
```python
'旧频道名': '新频道名',
```

### 添加台标覆盖
编辑 `LOGO_OVERRIDES` 字典:
```python
'频道名': 'https://台标URL.png',
```

### 添加黑名单
编辑 `BLACKLIST_NAMES`(按名)或 `BLACKLIST_IPS`(按IP)。

### 修改分组顺序
编辑 `group_order` 列表。

### 修改源优先级
编辑 `source_priority()` 函数或4K排序逻辑。
