# IPTV 待办事项

> 最后更新: 2026-07-25
> 状态总览: 核心功能全部完成并上线(NAS部署+cron自动化+GitHub+Docker Hub)。

## ✅ 已完成

### 核心系统
- 三层架构重构(采集/清洗/ETL/生成),SQLite 唯一主数据源
- 数据模型 V2: channel_id 稳定主键 + channel_key 可读, channels/sources/channel_preferred_sources 三表解耦
- 扫描优化: 三轮递进(4→2→1并发,超时递增) + probe rw_timeout 防卡死 + 双模式(known增量/full全量), 零误报
- 失效容错: fail_count 阈值, 全失效频道不选主源, 僵尸频道 bug 已修
- 回看天数探测(probe_timeshift.py): 单播源 timeshift_url + playseek 二分查找, 仅 full 跑, --timeshift-only 可补数据
- 孤儿源识别流程 Python 侧: orphan_export 产出待识别包 + orphan_import 消费(5种action), 异步文件交换

### 组播网关与播放适配(2026-07-25)
- 组播 LAN 直通: 软路由 igmpproxy + 防火墙放行组播转发(真凶是 IPTV zone forward=REJECT 静默丢包), LAN 设备可直接 `rtp://@` 收组播
- rtp2httpd 替代 msd_lite: FCC 快速换台(换台快1-2秒+更稳, 抓包定位 FCC 服务器 RTCP PT=205/FMT=5) + CORS + RTSP转HTTP + zerocopy/2MB缓冲优化
- 三套 m3u: 标准版(组播+FCC/单播回看catchup) + 直通版(rtp直收, LAN低延迟) + 兼容版(全组播HTTP, 适配不支持rtsp的网页播放器如飞牛影音)
- 单播回看可持续: sources.timeshift_query 存含 token 的 query, pipeline 每次 fetch_channels 刷 token, etl 回看加成让可回看单播成主源
- pipeline 加 `--gen-only`(改模板/样式后几秒重出静态页, 不重扫)
- 网页播放器适配: Mixed Content(用 http 访问) + CORS(网关加 Access-Control-Allow-Origin) + 不支持rtsp(兼容版组播优先)

### 界面/输出
- 双 Dashboard: 优选源列表(index) + 官方频道列表(channels), Jinja2 模板重构(数据/界面分离)
- m3u 生成(优选主源, 失效容错) + EPG 节目单(第三方112114)
- 台标匹配(address→channel_id→logo), 回看天数显示

### 工程/部署
- 上 GitHub(yangwudong/iptv-radar), 脱敏完成(.env/AGENTS.md本地保留)
- GitHub Actions: push 自动 build 多架构镜像推 Docker Hub
- NAS1 部署: Docker Compose(瞬时任务) + 数据持久化 + host网络
- Nginx 发布: <PUBLISH_HOST>:<PUBLISH_PORT>/iptv.m3u(旧m3u保留) + /dashboard/
- 群晖任务计划 cron: 每周四04:00 known增量 / 每月第2周二03:00 full全量

## 📋 真正的待办

- [ ] 9个 RTSP 单播孤儿源(名字没对上)人工识别 → 走 orphan 流程
- [ ] Electron App(独立项目): 孤儿源识别客户端, 契约见 design/ORPHAN_REVIEW.md §3
- [ ] 内网IP暴露公网DNS(claw/tv子域名直解析到内网IP) — 另开 session, 非本项目范畴

## 🔮 未来可选(不紧急)

- Dashboard 公开分享: 学 myepg 静态推 Cloudflare Pages(别暴露家里服务; Dashboard 已是自包含静态页)
- 界面美化: 参考 base-ui.com 等风格(现 Jinja2 模板便于改样式, 纯 CSS 即可, 不用引 React)

## 任务D: M3U多组标签(group-title分号语法) (优先级: 低)

### 背景
一个频道要出现在多个分类(如北京卫视既在"卫视"又在"北京"), 现用**复制多份+不同group-title**的做法。
M3U标准 group-title 单值, 复制是唯一原生方案, 缺点是冗余。

### 想法: 分号多组语法
部分播放器(APTV)支持 `group-title="卫视;北京"`, 一条 EXTINF 进多个组, 免复制。

### 需要做的事
1. **验证 APTV 是否真支持** — 手写 `group-title="卫视;北京"` 测试
2. 若支持, 改造 gen_m3u.py: 频道支持"主组+附加组"列表, 输出拼分号; 保留复制法作 fallback
3. **兼容性风险**: VLC/IPTV Smarters 可能不认分号, 会当成一个组名

### 现状(复制法, 有意设计非bug)
- 北京/上海/湖南/少儿等"受众入口组"的频道复制自其他组, 是刻意的软分组
- 当前 APTV 用复制法工作正常, 不急。等有空验证分号语法再改。
