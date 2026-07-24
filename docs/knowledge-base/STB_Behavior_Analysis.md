# 机顶盒开机完整行为分析

> 数据来源: iptv6.pcap (MacBook Pro桥接抓包)
> 机顶盒: 华为 EC6108V9U_pub_zjzdx, MAC: <stb_mac>
> 机顶盒IP: 10.235.154.64

---

## 时间线总览

```
0s      ┃ 桥接开始抓包
        ┃
20.6s   ┃ ① 机顶盒DHCP请求(DHCP Discover)
22.3s   ┃ ② DHCP请求重发(3次,获取IP)
        ┃ ── 获取IP: 10.235.154.64 ──
        ┃
27.5s   ┃ ③ 广告预加载 + EDS认证入口
27.6s   ┃ ④ EPG认证(AuthenticationURL → authLogin → ValidAuthentication)
28.1s   ┃ ⑤ 获取频道列表(getchannellist)
28.6s   ┃ ⑥ 加载EPG首页(ServicesEntry → index → Category)
28.9s   ┃ ⑦ TVMS上报(stb.zj.vnet.cn:37021/acs)
29.0s   ┃ ⑧ VSP订阅查询(QuerySubscriber)
        ┃
~100s   ┃ ⑨ NTP时间同步(sgp.ntp.org.cn)
~110s   ┃ ⑩ Yahoo分析上报
        ┃
        ┃ ── 用户操作播放频道 ──
        ┃
        ┃ ⑪ 单播UDP视频流开始(115.233.45.x → 10.235.154.64)
        ┃    总计 80万+包, 多路并发
```

---

## ① DHCP IPoE认证

### 请求参数
```
时间: 20.6s
源MAC: <stb_mac>
Hostname (Option 12): <STBID>
请求的Options: 53(Message Type), 61(Client ID), 50(Requested IP), 
              57(Max Message Size), 60(Vendor Class ID), 12(Hostname), 55(Parameter Request List)
```

### Option 60 (Vendor Class ID)
Hex: `00001f3901<USERID_HEX>...<HASHKEY>...<RANDOM>...<TIMESTAMP>`（真实抓包数据已脱敏）

结构解析:
```
00 00 1F          固定3字节头
39                内容长度(57字节)
01                版本/类型
<USERID_HEX>      → ASCII解出为 UserID/账号(15位数字,已脱敏)
<HASHKEY>         16字节 HashKey (MD5)
<...>             4字节
<...>             8字节
<RANDOM>          8字节 random
<TIMESTAMP>       4字节 timestamp相关
```

### 认证结果
- 分配IP: 10.235.154.64
- 子网掩码: 255.255.224.0 (/19)
- DNS: 202.101.172.35, 202.101.172.46
- DHCP服务器: 10.235.128.1

### 凭证信息汇总
| 参数 | 值 | 用途 |
|------|------|------|
| UserID | <USERID> | EPG认证用户ID |
| STBID | <STBID> | 设备标识(前缀+USERID+MAC) |
| MAC | <STB_MAC> | 设备MAC |
| Hostname | <STBID> | = STBID |
| DES Key | 00000000 | Authenticator加密密钥 |
| 设备型号 | EC6108V9U_pub_zjzdx | 华为机顶盒(浙江电信定制) |
| 软件版本 | V100R003C82LZJD11SPC002B012 | 固件版本 |
| 区域ID | 57104 | 杭州 |

---

## ② 开机上报数据

### 2.1 广告预加载 (27.5s)
```
GET 10.255.247.130:9091/adsserver/web/adreq?slotid=AuthenPIC2&userid=<USERID>&terminaltype=EC6108V9U_pub_zjzdx&definition=1
GET 10.255.247.130:9091/adsserver/web/adreq?slotid=StartPIC2&userid=<USERID>&terminaltype=EC6108V9U_pub_zjzdx&definition=1
GET 10.255.247.130:9091/adsserver/web/adreq?slotid=AppLaunchPIC2&userid=<USERID>&terminaltype=EC6108V9U_pub_zjzdx&definition=1
GET 10.255.247.130:9000/active/admin/zw/1776825068501.jpg   (开机图片)
```
广告服务器: `10.255.247.130`, 端口9000/9091

### 2.2 EDS认证入口 (27.5s)
```
GET 220.191.136.23:8082/EDS/jsp/AuthenticationURL?UserID=<USERID>&Action=Login
→ 302重定向到 115.233.40.140:33200
```
EDS服务器: `220.191.136.23:8082` (itv.zj.vnet.cn)

### 2.3 EPG认证 (27.6s)
```
GET  115.233.40.140:33200/EPG/jsp/AuthenticationURL?UserID=<USERID>&Action=Login
POST 115.233.40.140:33200/EPG/jsp/authLoginHWCTC.jsp?UserID=<USERID>&SampleId=
POST 115.233.40.140:33200/EPG/jsp/ValidAuthenticationHWCTC.jsp
  → 提交Authenticator(DES加密) + 设备信息
  → 返回JSESSIONID (认证成功)
```

ValidAuthentication提交的字段:
```
UserID:          <USERID>
Authenticator:   009134661F3B8BE5...(DES加密的认证串)
STBType:         EC6108V9U_pub_zjzdx
STBVersion:      V100R003C82LZJD11SPC002B012
STBID:           <STBID>
mac:             <STB_MAC>
templateName:    epg30
areaId:          57104
userToken:       AADC2D693084748B386F8CC62DB8A746
userGroupId:     12
```

### 2.4 频道列表获取 (28.1s)
```
POST 115.233.40.140:33200/EPG/jsp/getchannellistHWCTC.jsp
→ 返回169个频道的完整信息(420KB)
```

### 2.5 EPG首页加载 (28.6s)
```
POST /EPG/jsp/ServicesEntryHWCTC.jsp
GET  /EPG/jsp/indexHWCTC.jsp?lang=1&UserID=<USERID>
GET  /EPG/jsp/PreDealHWCTC.jsp
GET  /EPG/jsp/epg30/en/Category.jsp
+ 加载CSS/JS: commonFHD.min.css, tvms_relative.js, socket.io.js, epg_tvms.js 等
```

### 2.6 TVMS上报 (28.9s)
```
POST 122.229.17.2:37021/acs  (stb.zj.vnet.cn)
→ 盒子状态上报(在线状态、当前频道等), 连续发送6次
```
TVMS服务器: `122.229.17.2:37021`

### 2.7 VSP订阅查询 (29.0s)
```
POST 115.233.40.140:33200/VSP/V3/QuerySubscriber
POST 115.233.40.140:33200/VSP/V3/UpdateSubscriber
→ 查询用户订购的增值服务
```

### 2.8 服务服务器连接 (29-31s)
盒子还连接了以下服务器:
```
115.233.200.50:8081     服务服务器
115.233.200.59:58003    服务服务器
115.233.200.61:58001    服务服务器
115.233.200.94:58002    服务服务器
115.233.200.101:58004   服务服务器
115.233.200.146:9020    服务服务器
115.233.200.154:9010    服务服务器
115.233.200.162:8082    服务服务器
122.229.17.66:3000      浙江电信服务
```

### 2.9 NTP时间同步 (101s)
```
DNS查询: sgp.ntp.org.cn, us.ntp.org.cn
→ 时间同步
```

### 2.10 第三方上报 (110s+)
```
analytics.query.yahoo.com    Yahoo分析
astat.bugly.qq.com           腾讯Bugly崩溃上报
```

---

## ③ 视频流分析

### 关键发现: 机顶盒使用单播UDP,不使用组播

**组播流量: 零!**
```
233.50.202.x 的IGMP join: 0个
233.50.202.x 的UDP视频流: 0包
233.50.201.x 的组播视频流: 0包(本次抓包中盒子没有看任何组播频道)
```

**单播UDP视频流: 80万+包**

所有视频流都来自 `115.233.45.x` 服务器,发往盒子 `10.235.154.64`:

| 流媒体服务器 | 源端口范围 | 目的端口范围 | 数据包数 | 说明 |
|-------------|-----------|-------------|---------|------|
| 115.233.45.153 | 32156-32240 | 2026-2135 | ~30万 | 主要流媒体服务器 |
| 115.233.45.154 | 32164-32238 | 2016-2133 | ~25万 | 第二流媒体服务器 |
| 115.233.45.132 | 32228-32238 | 2012-2015 | ~4万 | 第三流媒体服务器 |
| 115.233.40.201 | - | - | 少量 | 备用流媒体 |

### 流媒体服务器端口规律
```
源端口(服务器侧): 32156-32240 范围,动态分配
目的端口(盒子侧): 2012-2135 范围,对应不同的频道/节目流
```
每个频道的单播流使用独立的目的端口(如2026, 2046, 2062等),盒子通过EPG获取的RTSP URL发起请求,服务器302重定向到115.233.45.x的实际流媒体服务器。

### 组播 vs 单播的频道对比

| 频道 | 组播地址 | 是否用组播 | 单播地址 | 说明 |
|------|---------|-----------|---------|------|
| CCTV1综合 | 233.50.201.118 | ❌本次未用 | rtsp://115.233.40.137/.../10000100000000060000000002460690_0.smil | 盒子用单播 |
| 北京卫视4K | 233.50.202.100 | ❌组播不通 | rtsp://115.233.40.137/.../128124469.smil | 只能单播 |
| 浙江卫视4K | 233.50.202.104 | ❌组播不通 | rtsp://115.233.40.137/.../136086103.smil | 只能单播 |

**结论**: 本次抓包期间,盒子所有频道都走单播UDP(通过RTSP建立连接),没有使用任何组播。

---

## ④ 完整服务器清单

### 认证/EPG
| 服务器 | 端口 | 用途 |
|--------|------|------|
| 220.191.136.23 (itv.zj.vnet.cn) | 8082 | EDS认证入口 |
| 115.233.40.140 | 33200 | EPG服务器(认证/频道列表/首页) |

### 广告
| 服务器 | 端口 | 用途 |
|--------|------|------|
| 10.255.247.130 | 9000, 9091 | 广告图片/开机画面 |

### 服务

| 服务器 | 端口 | 用途 |
|--------|------|------|
| 122.229.17.2 (stb.zj.vnet.cn) | 37021 | TVMS盒子状态上报 |
| 122.229.17.66 | 3000 | 浙江电信服务 |
| 115.233.200.50 | 8081 | 服务服务器 |
| 115.233.200.59 | 58003 | 服务服务器 |
| 115.233.200.61 | 58001 | 服务服务器 |
| 115.233.200.94 | 58002 | 服务服务器 |
| 115.233.200.101 | 58004 | 服务服务器 |
| 115.233.200.146 | 9020 | 服务服务器 |
| 115.233.200.154 | 9010 | 服务服务器 |
| 115.233.200.162 | 8082 | 服务服务器 |

### 流媒体(RTSP入口 → 单播UDP)
| 服务器 | 端口 | 用途 |
|--------|------|------|
| 115.233.40.137 | 554 | RTSP入口(302重定向) |
| 115.233.45.132 | 动态 | 流媒体服务器 |
| 115.233.45.153 | 动态 | 流媒体服务器(主) |
| 115.233.45.154 | 动态 | 流媒体服务器 |
| 115.233.40.201 | 动态 | 流媒体服务器(备用) |

### 第三方
| 服务器 | 端口 | 用途 |
|--------|------|------|
| analytics.query.yahoo.com | 443 | Yahoo分析 |
| astat.bugly.qq.com | 443 | 腾讯Bugly(崩溃上报) |
| sgp.ntp.org.cn | 123 | NTP时间同步 |
| 111.30.131.23 | 443, 14000 | 未知(可能是微信/QQ SDK) |
| 183.3.235.158 | 8011 | 未知(可能是腾讯服务) |
