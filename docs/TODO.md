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

## ✅ 2026-07-25 代码审查修复(已完成)

- 修6条数据正确性bug(串台/循环静默中断/零源不下线/禁用销毁归并/探测挂死/排序不一致),详见 PROGRESS.md
- 建立首套自动化测试 `tests/`(20条,pytest) + CI 测试门禁(不过不推镜像)
- 安全: 清除公开仓库历史里的真实账号数据 + 仓库转 private + `.dockerignore` + 容器非root + `.env` 600
- 工程加固: 原子写/参数正规解析/flock并发锁/关键步骤FATAL/启用外键约束+悬空体检
- 文档对齐: ARCHITECTURE/DEPLOY/ORPHAN_REVIEW/README/SPEC 的过时与自相矛盾处

## 📋 真正的待办

- [ ] **升级到新镜像时确认挂载目录权限**: 镜像已改非 root(uid 1000) 运行,
      宿主的 `data/`、`output/`、nginx m3u 目录需 uid 1000 可写,否则 pipeline 写不进去。
      首次部署新镜像前在 NAS 上执行一次 `chown -R 1000:1000` 或确认现有权限。
- [ ] **仓库是否转回 public**: 目前为 private(凭证泄露止血)。转回前确认
      `reference/channels.sample.json` 脱敏版无误、且历史已清干净。

- [ ] **9个 RTSP 单播孤儿源人工识别** → 走 orphan 流程
  - 现状: 孤儿源共26个 = 组播17(黑名单/无效, 不用管) + RTSP 9个(1080P, available=1, 名字对不上已知频道)
  - 流程已就绪: `orphan_export.py` 产出待识别包(orphans.json + 截图) → 人工识别 → `resolved.json` → `orphan_import.py` 消费写库
  - 5种 action: assign / new / junk / unknown / skip; **json 契约见 design/ORPHAN_REVIEW.md §3**
  - 可手动做: 播放这9个地址看画面认频道 → 手写 resolved.json → 跑 orphan_import
  - 注意: 单播地址需带 token 才能播(完整地址 = `sources.address` + "?" + `sources.timeshift_query`), 且需在能到 IPTV 专网的环境(NAS/软路由)
- [ ] Electron App(独立项目): 孤儿源识别客户端(看截图 + 播放 + 选 tag), 契约见 design/ORPHAN_REVIEW.md §3
- [x] ~~**RTSP 回看转 HTTP 验证**~~ → **已验证:技术可行,但对飞牛影音不可行**(2026-07-25 实测结论)
  - **rtp2httpd 的 RTSP→HTTP 能力完全可用**(已实测,可用于未来其他播放器):
    - URL: `http://<网关>:4088/rtsp/<RTSP服务器>:554/<path>?<原query>&playseek=<起>-<止>`
    - token(`accountinfo`/`it=`)原样透传,无需处理;`:554` 带与不带都行
    - 回看返回**真历史内容**(4个不同时刻画面 md5 全异)
    - **时区无需 offset**: 请求 19:00 → 画面茅台报时钟显示 18:59:57(早约3秒是GOP对齐),
      与电信 playseek 用北京时间的语义一致,不需要 `r2h-seek-offset`
  - **但飞牛影音用不了,两个硬阻碍**:
    - ① 飞牛网页播放器**不支持 m3u 的 catchup 标签** —— 界面里没有回看/时移入口,
      给它再完美的回看地址也没 UI 让用户选时间点
    - ② 纯单播 4K 频道是 **HEVC(h265) Main 10 + 3840x2160** —— 浏览器/mpegts.js
      根本解不了 H.265,除非服务端转码(另一个量级的工程)
  - **结论**: 不再投入。兼容版保持"组播优先 + 无回看"现状即可。
    需要回看就用标准版 `iptv.m3u` + APTV(原生 rtsp 回看已工作)。
  - 遗留可选小改进(价值低,未做): 兼容版里那9个纯单播4K频道对飞牛是永远播不了的噪音,
    可考虑加开关剔除。

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
