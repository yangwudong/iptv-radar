# M3U 生成 Acceptance Criteria & 分层归属

> 创建: 2026-07-22
> 目的: 汇总历次对话中对 m3u 的全部要求(验收标准),并按三层架构归类:
>       哪些在【采集层】、哪些在【ETL清洗层】、哪些在【生成层】实现。
> 这是重构的验收契约,也是 sub-agents 开发的接口约定。
> 配套: REFACTOR_DESIGN.md(架构) / SPEC.md(现有规则细节)

---

## 一、三层职责回顾

| 层 | 脚本 | 输入→输出 | 一句话职责 |
|----|------|----------|-----------|
| 采集层 | scan_*.py / epg_client.py | IPTV→sources表 | 只写事实(技术属性),不判断 |
| ETL清洗层 | etl_process.py | sources表→channels表 | 归并/优选/富化/命名/分组决策 |
| 生成层 | gen_m3u.py | DB→m3u文件 | 纯读,套全局排序策略输出 |

---

## 二、Acceptance Criteria 全集(按层归类)

### 【采集层】AC — 技术事实的获取

| # | Acceptance Criteria | 实现要点 | 验证 |
|---|---------------------|---------|------|
| C1 | 抓到每个源的分辨率(width×height) | ffprobe width/height | ✅已测 |
| C2 | 抓到编码(h264/hevc) | codec_name | ✅已测 |
| C3 | 抓到帧率(如50fps) | r_frame_rate | ✅已测 |
| C4 | 判定HDR类型(SDR/HLG/HDR10) | color_transfer(arib-std-b67=HLG,smpte2084=HDR10)+color_primaries(bt2020)+pix_fmt(10le) | ✅已测 |
| C5 | 抓到音频编码+声道(mp2/aac/eac3, 2.0/5.1) | codec_name/channels | ✅可测 |
| C6 | 实测视频码率(kbps) | ffprobe对组播返回N/A,需抓N秒流算 bytes×8/duration | ✅已有逻辑 |
| C7 | 判定可用性(在线/超时/无视频) | 优化参数(8M/去nobuffer)+重试,避免误判 | ✅已测(4K救回) |
| C8 | **新频道抓3张截图**(用于后期识别) | 间隔抓(3s/8s/15s),避开广告黑屏,存路径入DB | ✅可测 |
| C9 | 不做数据清理/命名/分组 | 采集层只输出原始事实 | 设计约束 |

**采集层输出到 sources 表**: address/source_type/available/resolution/res_label/
video_codec/fps/vbitrate/hdr/audio_codec/audio_channels/screenshot_paths/last_scan

---

### 【ETL清洗层】AC — 频道级决策与规范化

| # | Acceptance Criteria | 实现要点 | 现状 |
|---|---------------------|---------|------|
| E1 | 同频道多源归并 | 按频道名/EPG映射把散落的源关联到同一channel_id | 现散成多行,名字还不一致 |
| E2 | **源优选**(每频道选1主源) | 分辨率>码率>稳定性>类型偏好,写primary_source_id | 新增 |
| E3 | CCTV命名规范化(无空格,如CCTV1综合) | "中央一套高清"→"CCTV1综合",tvg-id=CCTV1 | 现在NAME_OVERRIDES |
| E4 | 卫视去"高清"后缀,合并HD/SD | "浙江卫视高清"→"浙江卫视" | 现在NAME_OVERRIDES |
| E5 | 4K频道独立成条(如浙江卫视4K) | 4K版和普通版是不同channel | 现有逻辑 |
| E6 | 特殊命名映射 | BesTV→BesTV少儿4K/纪实4K/视界4K;广播统一"浙江X广播/之声";旅游之声_女主播 | 本次会话已定 |
| E7 | tvg-id 生成(EPG匹配用) | CCTVn取主体;带"浙江"前缀防串台(旅游之声) | 现有逻辑+本次广播 |
| E8 | tvg-logo 解析(台标URL) | myepg/Logo/主 + CCSH(jsdelivr)兜底 + 名字规范化匹配 + 手动override | 本次会话已定 |
| E9 | 分组归属(主分组) | 按名判定: 央视/4K超高清/卫视/浙江/上海/北京/湖南/少儿/BesTV/睛彩/港澳台/央视国际/广播/其他 | 现在get_group |
| E10 | 4K强制归组 | 名含"4K"强制进"4K超高清"组 | 现有 |
| E11 | 附加分组(受众入口,重复条目) | 北京卫视→卫视+北京;少儿类→少儿;湖南台→湖南(复制法,详见任务D) | 现有复制逻辑 |
| E12 | 黑名单标记(enabled=0) | 测试台/购物台/BesTV未知X → 不输出但DB保留 | 本次会话已定 |
| E13 | 时移标记 | EPG的TimeShift字段→channels.timeshift | 现有 |
| E14 | 人工映射优先 | DB里已人工确认的name/logo/group,ETL不覆盖 | 新增原则 |

**ETL层输出到 channels 表**: name/tvg_id/tvg_logo/group_primary/group_extra/
primary_source_id/enabled/timeshift + sources.channel_id关联 + quality_score

---

### 【生成层】AC — 全局排序与输出格式

| # | Acceptance Criteria | 实现要点 | 现状 |
|---|---------------------|---------|------|
| G1 | 分组间排序(固定优先级) | 央视→4K超高清→卫视→浙江→上海→北京→湖南→港澳台→央视国际→少儿→BesTV→睛彩→其他→广播 | 现在group_order |
| G2 | 组内自然数字排序 | CCTV1<CCTV2<...<CCTV13(不是字典序CCTV1<CCTV11<CCTV2) | 现有 |
| G3 | 组内特殊排序 | 卫视按影响力;浙江按STB频道号;上海/BesTV自定义顺序 | 现有各order字典 |
| G4 | 源选择(组播m3u) | 非4K用组播(201.x优先>200.x);4K/202段用RTSP(组播不通) | 现有 |
| G5 | 只输出enabled=1的频道 | 跳过黑名单/禁用 | 生成时过滤 |
| G6 | EXTINF格式正确 | `#EXTINF:-1 tvg-id="" tvg-logo="" group-title="",显示名` | 现有 |
| G7 | 单个m3u含组播+单播 | (你的新要求)不再分两个文件,一个m3u里频道用优选主源 | **变更:原来分multicast/unicast两文件** |
| G8 | 附加分组用复制法输出 | 一频道多分组=多条EXTINF(APTV复制法);预留分号多组(任务D) | 现有 |

**生成层输出**: 单个 m3u 文件(如 iptv.m3u),不再分组播/单播两个

---

## 三、关键变更点(相比现状)

1. **G7澄清: 一个m3u(不是大变更,是现状的延续)** — 澄清:现有 merged_multicast.m3u
   虽名字带"multicast",**实际已是混合源**(实测135组播+19单播,4K/202段用RTSP)。
   新方案就是它的进化版:**一个m3u,每频道选最清晰的单一源**(ETL优选)。
   不再输出单独的 merged_unicast.m3u。命名可改为中性的 iptv.m3u。
2. **规则从代码字典迁到DB** — E3~E13 的一次性人工映射(NAME_OVERRIDES/LOGO_OVERRIDES/黑名单)入channels表;
   E9/G1~G3的算法性规则留代码/配置。
3. **源优选(E2)是新增核心** — 用ETL算好的主源替代原来"生成时现算源优先级"。

---

## 四、周期运行 & 运维需求(新增)

### 4.1 定时任务
- **频率**: 每周或每两周跑一次(NAS2 cron / Docker定时)
- **完整流程**: 采集(扫描)→ETL(优选/富化)→生成(m3u)→发布(Nginx)

### 4.2 变更检测(新增/下线)
- **新增频道**: 本次扫描发现、DB里没有的源/频道 → 标记"待人工识别"(抓了3张截图)
- **下线频道**: DB里有、连续N次扫描不可用 → 标记"疑似下线"
- **产出**: 每次运行生成 diff 报告(新增X个/下线Y个/分辨率变化Z个)
- **实现层**: ETL层对比 scan_history,生成变更列表

### 4.3 发布(Nginx)
- **m3u 暴露**: 通过 NAS1(Synology) 的 Nginx 对局域网/其他电脑提供 m3u URL
  (注:NAS1是Synology,NAS2是飞牛NAS。扫描/ETL/生成跑NAS2,m3u产物同步到NAS1 Nginx发布,或直接NAS2发布——见待确认Q2)
- **Dashboard**: 扫描结果可视化,也通过Nginx暴露

### 4.4 Dashboard(新增,风格参考myepg report)
参考 https://myepg.org/Zhejiang_Multicast/report 和 /Zhejiang_Unicast/report
- **频道列表表格**: 频道名/分辨率/编码/码率/HDR/音频/在线状态/截图缩略图
- **统计**: 总频道/在线数/各分辨率分布/4K数量/码率排行
- **变更时间线**: 新增/下线历史(用scan_history)
- **在线率趋势**: 各频道稳定性
- **技术**: 静态HTML(ETL/生成时产出)或轻量后端读DB;Nginx托管

---

## 五、决策已定案(原待确认问题)

| # | 问题 | **决定** |
|---|------|---------|
| Q1 | 一个m3u还是保留单播备份 | **只要一个m3u**,含所有能播频道,每频道选最清晰的单一源(组播/单播都行,不做单频道多源因兼容差) |
| Q2 | 发布在哪 | **发布在NAS1(Synology)现有Nginx**(已在跑,不迁移)。扫描/ETL/生成可跑NAS1 docker(N100比J3455强且瞬时任务不常驻);NAS1/NAS2可做目录映射 |
| Q3 | Dashboard形式 | **静态HTML**(每次生成时导出) |
| Q4 | 截图存放+展示 | 存在Dashboard的docker项目目录下专门文件夹;Dashboard默认显示缩略图,点开放大;**需考虑清理机制**(旧截图定期删) |
| Q5 | 下线阈值 | 每周扫1次的话,**连续4周(4次)未发现→标记下线** |

### 5.1 现有Nginx发布机制(已实地确认,复用零改动)
```
NAS1: /volume1/docker/nginx/
  ├── compose.yaml   → 挂载 ./m3u → /usr/share/nginx/html/m3u (只读)
  ├── nginx.conf     → server <PUBLISH_HOST>:<PUBLISH_PORT>, location ~ \.m3u$
  └── m3u/           → 放m3u文件的目录(已有几个)
访问: https://<PUBLISH_HOST>:<PUBLISH_PORT>/<文件名>.m3u
```
- **m3u发布**: 生成的 iptv.m3u 放到 `/volume1/docker/nginx/m3u/` → 直接可访问,**Nginx零改动** ✅
- **Dashboard(HTML)**: ⚠️现有Nginx只放行`.m3u`(正则),HTML会404。
  需求: 给Dashboard加一个location(要改nginx.conf,**部署时先经你同意**),
  或Dashboard的docker项目自带一个轻量web serve端口。→ 部署阶段定

### 5.2 运行宿主决定
- **扫描/ETL/生成**: 跑 **NAS1(N100) docker**(比J3455强,瞬时任务不常驻内存)
  注:NAS1是Synology,docker在/volume1/docker/。之前测NAS2(飞牛/Debian)网络可达性OK,
  NAS1同在LAN应同样可达,**部署时需在NAS1只读验证一次组播/RTSP可达性**
- **数据/产物**: DB、m3u、Dashboard、截图 都在 NAS1 的某个 docker项目目录
- **发布**: m3u产物直接写入/软链到 nginx/m3u/
