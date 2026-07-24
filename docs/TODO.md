# IPTV 待办事项

> 最后更新: 2026-07-23 (项目搬迁到 workspaces/self/iptv-radar)

## ✅ 已完成(重构)
- iptv-radar 三层架构重构(采集/ETL/生成),SQLite主数据源
- 扫描器优化(救回误判4K),RTSP重定向链追踪,源优选
- 项目搬迁+脱敏+文档体系(mechanism/knowledge-base/design)

## 📋 待办

### 部署上线
- [ ] iptv-radar 部署到 NAS Docker(需最终确认后执行)
- [ ] cron 周期运行 + Nginx 发布 Dashboard(需加location)
- [ ] 考虑上 GitHub/Gitea + GitHub Actions 自动build镜像→Docker Hub

### 功能(原TODO)

## 任务D: M3U多组标签(group-title分号语法) (优先级: 低)

### 背景
目前为了让一个频道出现在多个分类里(如北京卫视既在"卫视"组又在"北京"组、
少儿类频道单独进"少儿"组方便孩子选),采用**复制多份+不同group-title**的做法。
这是M3U标准的唯一原生方案(group-title单值,一条EXTINF只能属于一个组),
缺点是同一频道要维护多份、有冗余。

### 想法: 用分号多组语法
部分播放器(如APTV)支持 `group-title="卫视;北京"` 分号分隔多个组,
一条EXTINF就能同时进多个组,免复制、免冗余。

### 需要做的事
1. **验证APTV是否真支持** — 手动写一条 `group-title="卫视;北京"` 测试,
   看APTV是否把该频道同时显示在两个分组下
2. 若支持,改造 merge_m3u.py:
   - 频道支持"主组 + 附加组"列表(如 北京卫视: 主组=卫视, 附加=[北京])
   - 输出时拼成 `group-title="卫视;北京"`,不再复制条目
   - 少儿/北京/湖南这些"受众入口组"改为附加组
3. **兼容性风险**: VLC/IPTV Smarters等其他客户端可能不认分号语法,
   会把整个"卫视;北京"当成一个组名。需要保留"复制法"作为fallback开关
   (如 merge_m3u.py 加 --multi-group / --duplicate 两种模式)

### 现状(复制法,已确认是有意设计,不是bug)
- 北京组: 北京卫视(重复自卫视) + 北京纪实科教 + 北京卡酷少儿
- 湖南组: 快乐垂钓 + 湖南茶频道 + 金鹰卡通
- 少儿组: BesTV少儿 + 北京卡酷少儿 + 哈哈炫动 + 金鹰卡通(均复制自其他组)
- 这些是按"地域/受众"划分的软分组,重复是刻意的

### 优先级说明
不紧急。当前APTV用复制法工作正常。等有空验证分号语法再改造。

---

## 任务C: IPTV状态Dashboard (优先级: 中)

### 背景
RTSP单播服务器有时会故障(如重定向死循环:115.233.40.137 ↔ 220.191.136.24)。
组播频道也有约60%不稳定率。需要一个监控面板实时掌握各组件状态。

### 功能需求
1. **RTSP重定向链追踪(核心)**
   - 从115.233.40.137和115.233.41.137两个入口分别发DESCRIBE
   - 跟踪完整重定向链,显示每一跳: IP1 → IP2 → IP3 → ... → 最终结果
   - 检测死循环(同一IP出现两次)并标记为❌
   - 检测最终结果: 200 OK(可播) / 超时 / 403 / 死循环
   - 分频道测试: 抽样4K频道+普通频道各几个
   - 历史记录: 记录每次检测的时间和结果,展示恢复时间线

2. **服务器健康检查**
   - TCP连通性: 115.233.40.137/41.137:554, 220.191.136.24:554
   - 122.229.6.x:554 (4K流媒体终端)
   - 115.233.45.x:554 (普通流媒体)
   - 响应延迟趋势

2. **组播频道可用性**
   - 定期抽样测试10-20个关键频道(CCTV1/CCTV5/浙江卫视等)
   - 显示在线/离线比例
   - 历史趋势图(用SQLite scan_history数据)

3. **网络状态**
   - zIPTV接口状态(IP/网关/uptime)
   - 路由状态(115.233.40.0/21, 220.191.136.0/24)
   - 光猫IGMP Proxy状态(通过ping网关间接判断)

4. **EPG认证状态**
   - 上次成功认证时间
   - UserID/token是否过期

5. **历史数据可视化**
   - 频道数量变化趋势
   - 码率对比(组播vs单播)
   - 各频道稳定性(在线率)

### 技术方案
- **后端**: Python(Flask/FastAPI)或Shell脚本定时采集
- **存储**: 已有SQLite(iptv_channels.db + scan_history)
- **前端**: 简单HTML+JS(类似Grafana轻量版)
- **部署**: OpenWRT上运行(用uci定时任务触发采集)

### 参考实现
```python
# 健康检查核心逻辑
def check_rtsp_health():
    # 1. TCP连通性
    # 2. DESCRIBE请求看是否302
    # 3. 跟踪重定向链是否死循环
    # 4. 最终是否拿到SDP(200 OK)
    
def check_multicast_sample():
    # 抽样10个频道,ffprobe快速测试
    
def check_network():
    # ip addr/route/ping
```

---

## 任务A: 组播引到LAN (优先级: 中)

### 背景
目前组播只在zIPTV(eth2)上,局域网设备无法直接收组播,只能通过msd_lite/udpxy转HTTP。
如果将组播引到LAN,任何设备(IPTV Smarters/VLC/手机)都可以直接播放 `rtp://233.50.x.x:5140`。

### 需要做的事
1. OpenWRT安装 `igmpproxy` (`opkg install igmpproxy`)
2. 配置igmpproxy:
   - 上游(upstream): zIPTV接口(eth2)
   - 下游(downstream): LAN接口(br-lan)
   - 让LAN设备可以发IGMP join到光猫的IGMP Proxy
3. OpenWRT的LAN bridge开启IGMP Snooping
   - 防止组播flood到所有LAN端口
   - LuCI → 网络 → 接口 → LAN → 物理设置 → 高级 → 勾选IGMP Snooping
4. 验证不与光猫的IGMP Proxy冲突
   - 光猫(HS8125C)已有IGMP Proxy(桥接WAN模式)
   - OpenWRT的igmpproxy作为第二级proxy
   - 需要测试是否正常工作

### 注意事项
- 光猫的IGMP Proxy是组播的唯一通道(关掉后所有频道不通)
- OpenWRT的igmpproxy需要正确配置,避免IGMP查询冲突
- 如果交换机不支持IGMP Snooping,组播会flood所有端口(网络风暴风险)
- 建议先在测试环境验证,再全量部署

### 参考配置
```
# /etc/config/igmpproxy
config igmpproxy
    option quickleave 1

config phyint
    option network zIPTV
    option direction upstream

config phyint
    option network lan
    option direction downstream
```

---

## 任务B: 时移/回放研究 (优先级: 中)

## 已验证的事实

### EPG时移字段
- 97个频道支持时移 (`TimeShift=1`)
- 72个频道不支持 (`TimeShift=0`)
- 时移窗口: 2小时(57频道) / 4小时(36频道) / 1小时(CGTN)

### RTSP Range测试结果
- ✅ 格式: `Range: clock=YYYYMMDDTHHMMSS[.frac]Z-` (必须带Z后缀)
- ✅ 服务器返回 `Timeshift-Status: 1` 确认支持时移
- ✅ 未来时间: 服务器接受并echo
- ❌ 过去时间: 服务器忽略,始终返回直播当前位置
- ❌ `npt=` 格式: 接受但不生效
- ❌ 无Z后缀: `457 Invalid Range`

### 直播URL vs 回看URL区别
```
直播 (ChannelURL):  accountinfo flags = 2,2
回看 (TimeShiftURL): accountinfo flags = 7,4
```
两个URL的.smil路径完全相同,仅accountinfo参数不同。

### 服务器时间体系
- 服务器Range响应时间与实际UTC有约4小时偏差
- 服务器实际UTC正确(Date头),但Range时钟可能使用内容时间(content time)

## 未解之谜

1. **如何向过去seek?** 直接Range不生效,可能需要PLAY→PAUSE→PLAY序列
2. **STB用了什么特殊命令?** 可能是华为自定义RTSP扩展头
3. **回看(过去几天)走什么协议?** 可能不是RTSP,而是EPG/VOD系统

## 下一步计划

### 任务1: 抓取STB时移操作(优先级:中)
- 用MacBook Pro桥接,在机顶盒上操作:
  - 正常播放CCTV1 → 按暂停 → 等30秒 → 按播放
  - 或进入回看菜单 → 选择过去节目
- Wireshark抓取完整RTSP交互
- 重点看: PAUSE请求、PLAY请求的Range头、自定义头(x-*开头)

### 任务2: 研究EPG回看API(优先级:低)
- EPG服务器(115.233.40.140:33200)可能有回看API
- 查看EPG响应中是否有回看相关的URL
- 测试 `/EPG/jsp/` 下的回看接口

### 任务3: 集成时移到播放脚本(优先级:低)
- 确认时移机制后,在epg_client.py中添加时移URL生成
- 生成带时移支持的m3u播放列表

## 测试脚本
- `/Users/<user>/Downloads/IPTV/test_timeshift.py` — RTSP Range测试脚本
- `/tmp/cctv1_url_2.txt` — CCTV1回看URL(需刷新token)
