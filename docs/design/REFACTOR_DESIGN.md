# IPTV 系统重构设计文档 (Architecture v2)

> ⚠️ **本文档是重构前的设计规划(2026-07-22),记录了架构演进思路。**
> **当前实现的权威描述见 `ARCHITECTURE.md`。** 本文档部分细节已随实现演进(如:
> primary_source_id已拆为channel_preferred_sources表; 两阶段扫描已升级为三轮递进/4并发;
> 新增orphan_export/import孤儿源流程、seed种子、双模式扫描)。历史保留,不逐条更新。
> 5.6.5/5.6.6 的扫描调优实测结论仍然有效准确。

> 创建: 2026-07-22
> 项目代号: **iptv-radar** (Docker Compose项目名, 部署在 NAS1 /volume1/docker/iptv-radar/)
> 目标: 把 SQLite 确立为唯一主数据源(single source of truth),
>       扫描/ETL/生成三层解耦,消除当前 merge_m3u.py 硬编码字典 + 多源现算的问题。
> 状态: **设计阶段** → 已实施,当前实现见 ARCHITECTURE.md
>
> 配套文档: ARCHITECTURE.md(当前实现总览) / M3U_ACCEPTANCE_CRITERIA.md(验收标准) /
>          CHANNEL_KEY_DESIGN.md(数据模型) / ORPHAN_REVIEW.md(孤儿源流程)

---

## 〇、借鉴调研: iptv-checker (zhimin-dev)

NAS1 上曾部署 zhimin-dev/iptv-checker(Rust, 现已停),调研其实现供借鉴:
- **方法**: HTTP快筛 + ffprobe精验 + 并发 (架构思路与我们设计一致)
- **ffprobe参数**: `ffprobe -v quiet -print_format json -show_format -show_streams -timeout <秒> <url>`
  → **没有 analyzeduration/probesize 调优**,这正是它"会卡顿"的已知问题(与我们发现的糙参数同源)
- **值得借鉴**: **进程级超时强杀** — spawn ffprobe + 独立线程读输出 + 超时了 kill 进程。
  比 Python subprocess.timeout 更可靠(卡死的组播流,subprocess超时有时杀不净子进程)
- **结论**: 架构思路印证一致,但我们的扫描器更优(8M probesize救回4K + 分级 + 重试),
  自己写更贴合,不用它;借鉴"进程级超时强杀"技巧

---

## 一、当前架构的问题(为什么要重构)

### 1.1 现状数据流(乱)
```
channels.json ─┐
用户m3u       ─┤
myepg m3u     ─┼─→ merge_m3u.py (现场合并+硬编码NAME_OVERRIDES/LOGO_OVERRIDES/分组规则)
scan_*.json   ─┤                └─→ merged_multicast.m3u / merged_unicast.m3u
logo_index    ─┘
iptv_channels.db (SQLite) ←── channel_db.py 单独维护, merge_m3u.py 根本不读它!
```

### 1.2 核心问题
1. **SQLite 是旁路记录库,不是主数据源** — merge_m3u.py 绕过它,直接读 5 个源头现算
2. **数据重复维护** — 改个频道名要同时改 merge_m3u.py 的字典 + DB(如BesTV/广播改名)
3. **同频道多源无可靠关联键** — CCTV10 的1080P和SD散成两行,名字还不一致(`CCTV10 科教` vs `CCTV10科教`)
4. **扫描数据不全** — vbitrate 只有155/459有值
5. **规则和数据混在代码里** — 分组/命名/台标/黑名单全是 merge_m3u.py 里的硬编码字典

---

## 二、目标架构(三层解耦)

```
┌─────────────────────────────────────────────────────────────┐
│  第1层: 采集 (Collect) — 只写事实,不做清理                      │
│  ├── epg_client.py    EPG认证→拿官方频道表(名/组播IP/RTSP)      │
│  ├── scan_multicast   扫组播IP → 可用性/分辨率/码率/HDR/编码     │
│  └── scan_rtsp        扫RTSP → 同上 + 重定向链                  │
│                          ↓ 写入                                │
├─────────────────────────────────────────────────────────────┤
│              SQLite (唯一主数据源 single source of truth)       │
│  ┌──────────────┐      ┌──────────────────┐                   │
│  │ channels表    │1────*│ sources表         │                  │
│  │ 频道级信息     │      │ 源级信息(每IP一行) │                  │
│  │ 名/logo/      │      │ ip/type/分辨率/    │                  │
│  │ tvg-id/分组/  │      │ 码率/HDR/可用性/   │                  │
│  │ 主源指针      │      │ 优选评分          │                   │
│  └──────────────┘      └──────────────────┘                   │
├─────────────────────────────────────────────────────────────┤
│  第2层: 加工 (ETL) — 分析+决策,写回DB                          │
│  └── etl_process.py                                            │
│      ├── 关联: 把散落的源按频道归并(填 sources.channel_id)      │
│      ├── 源优选: 同频道下按 分辨率>码率>稳定性 选主源           │
│      ├── 富化: 匹配 tvg-id / logo / 分组                       │
│      └── 写回: channels.primary_source_id, 各富化字段           │
├─────────────────────────────────────────────────────────────┤
│  第3层: 生成 (Generate) — 纯读DB,套规则出m3u                    │
│  └── gen_m3u.py                                                │
│      ├── 读 channels + 主源                                    │
│      ├── 套分组顺序/排序规则(规则可留代码或入DB,见4.3)          │
│      └── 输出 merged_multicast.m3u (组播优先,4K/202走RTSP)     │
└─────────────────────────────────────────────────────────────┘
```

**关键原则**:
- 每层单向依赖下层,互不反向耦合
- 采集层"只写不判断",ETL层"只判断不采集",生成层"只读不改数据"
- 每层可独立运行、独立测试 → 适合 sub-agents 分工实现

---

## 三、数据库设计 (v2)

### 3.1 为什么源不拆两张表(组播/单播)
组播源和单播源的**属性完全同构**(都是 分辨率/码率/HDR/编码/可用性),
差异只在 url 格式和优选权重。拆两张表会导致 ETL 和查询反复 UNION,得不偿失。
→ **一张 sources 表 + source_type 字段** 区分。

### 3.2 channels 表(频道级 — 一个逻辑频道一行)
```sql
CREATE TABLE channels (
    channel_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,      -- 规范显示名(如 CCTV1综合)
    tvg_id          TEXT,               -- EPG匹配id(如 CCTV1)
    tvg_logo        TEXT,               -- 台标URL
    group_primary   TEXT,               -- 主分组(如 央视)
    group_extra     TEXT,               -- 附加分组,分号分隔(如 北京;少儿) 备将来多组用
    primary_source_id INTEGER,          -- 主源(ETL优选结果,外键→sources)
    enabled         INTEGER DEFAULT 1,  -- 是否输出到m3u(0=黑名单/禁用,如BesTV未知)
    timeshift       INTEGER DEFAULT 0,  -- 是否支持时移
    sort_hint       INTEGER,            -- 组内排序提示(可选)
    notes           TEXT,
    UNIQUE(name)
);
```

### 3.3 sources 表(源级 — 每个可播地址一行)
```sql
CREATE TABLE sources (
    source_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id    INTEGER,             -- 所属频道(外键→channels; NULL=未归并的孤儿源)
    source_type   TEXT,                -- multicast / rtsp
    address       TEXT NOT NULL,       -- 组播:233.50.201.118:5140  单播:rtsp://.../xxx.smil
    -- 扫描采集的技术属性(采集层写)
    available     INTEGER DEFAULT 0,   -- 最近一次扫描是否可用
    resolution    TEXT,                -- 1920x1080
    res_label     TEXT,                -- 4K/1080P/720P/SD
    video_codec   TEXT,                -- h264/hevc
    fps           REAL,
    vbitrate      INTEGER DEFAULT 0,   -- 实测视频码率(bps)
    hdr           TEXT,
    audio_codec   TEXT,
    audio_channels INTEGER,
    -- ETL算的
    quality_score REAL DEFAULT 0,      -- 优选评分(ETL算)
    -- 元数据
    first_seen    TEXT,
    last_seen     TEXT,
    last_scan     TEXT,
    UNIQUE(address)
);
```

### 3.4 scan_history 表(保留,趋势分析用)
```sql
-- 沿用现有设计,每次扫描追加,记录 source_id + 时间 + 结果状态
CREATE TABLE scan_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_date TEXT, source_id INTEGER, address TEXT,
    available INTEGER, resolution TEXT, vbitrate INTEGER, status TEXT
);
```

### 3.5 源优选评分逻辑(ETL)
同一 channel_id 下的多个 source,按下面打分,最高分设为 primary_source_id:
```
quality_score = 分辨率权重 + 码率归一 + 稳定性 + 类型偏好
  分辨率: 4K=1000, 1080P=500, 720P=200, SD=50
  码率:   vbitrate/1000 (kbps,越高越好,同分辨率下区分)
  稳定性: 最近N次扫描可用率 * 100
  类型:   组播优先(+30, 因低延迟稳定) / RTSP(+0)
          注:4K和202段频道组播不通,只有RTSP源,自然选RTSP
  黑名单/不可用: score=-1 (不选)
```

---

## 四、规则该放哪(DB vs 代码)

这是架构关键决策。原则: **数据放DB,策略放代码(或配置文件)**。

### 4.1 放进 DB 的(数据性,每频道不同)
- 频道名/tvg-id/logo/主分组/附加分组/enabled/timeshift → channels表
- 源的技术属性 → sources表
→ 这些以前散在 NAME_OVERRIDES/LOGO_OVERRIDES/BLACKLIST,现在都入库

### 4.2 留在代码/配置的(策略性,全局规则)
- **分组顺序**(央视→4K→卫视→...) — 全局排序策略,放 gen_m3u.py 或 config.yaml
- **组内排序规则**(CCTV按数字/卫视按影响力) — 算法,放代码
- **源优选算法** — 放 etl_process.py
- **命名规范化规则**(如"中央N套"→"CCTVN"的转换算法) — 放采集/ETL,作为入库前的清洗
→ 这些是"怎么算",不是"算什么",留代码更合适

### 4.3 折中: 命名/台标映射
像 BesTV→BesTV4K台标、广播改名 这种**一次性人工映射**,
建议直接写进 channels 表(name/tvg_logo已是最终值),不再需要 OVERRIDES 字典。
ETL 富化时,已在 channels 表有值的不覆盖(人工优先)。

---

## 五、脚本职责划分(解耦,适合sub-agents)

| 脚本 | 层 | 输入 | 输出 | 职责 |
|------|----|----|----|------|
| epg_client.py | 采集 | EPG认证 | sources表(新增地址) | 拿官方频道表,只填address/name雏形 |
| scan_multicast.py | 采集 | 组播IP段 | sources表(技术属性) | 扫组播,填可用性/分辨率/码率/HDR |
| scan_rtsp.py | 采集 | RTSP地址 | sources表(技术属性) | 扫单播,同上+重定向链 |
| etl_process.py | 加工 | sources表 | channels表+主源 | 归并/优选/富化/写回 |
| gen_m3u.py | 生成 | channels+sources | m3u文件 | 纯读DB,套规则生成 |
| (config.yaml) | 配置 | — | — | 分组顺序/命名规则/黑名单等策略 |

每个脚本**只碰自己的输入输出**,可独立跑、独立测,sub-agent 各领一个实现。

---

## 五.五、技术选型 & 部署环境

### 5.5.1 部署环境
| 设备 | CPU | 内存 | 角色 | 网络 |
|------|-----|------|------|------|
| NAS1 (nas.<DOMAIN>:<SSH_PORT>) | Intel N100 | 8G(可用~3G,紧) | 备选 | LAN <NAS1_LAN_IP> |
| **NAS2 (nas2.<DOMAIN>)** | J3455 4核@1.5G | **16G(可用12G)** | **✅选定宿主** | LAN <NAS2_LAN_IP> |
| 软路由 (OpenWRT) | J1900 | 4G | IPTV网关,不宜再加负载 | <ROUTER_IP> |

**运行宿主决定: NAS1(N100) docker** (最终)
- 发布用 NAS1(Synology) 现有 Nginx(已在跑,不迁移),整套跑NAS1减少跨机同步
- N100 比 J3455 强,且扫描是瞬时任务(每周1次),不常驻内存
- NAS1(Synology)/NAS2(飞牛Debian)可做目录映射
- 网络可达性: NAS2已实测组播/RTSP全通;NAS1同在LAN应同样可达,**部署时NAS1只读验证一次**
- ⚠️注意 NAS1 内存可用仅~3G(N100),扫描并发别开太大;数据库/大文件注意占用

### 5.5.2 网络可达性(✅ 已实测验证,不是纸面推断)
从 NAS2 实测:
- **扫组播**: `curl http://<ROUTER_IP>:4088/` → HTTP 404(服务在); 拉CCTV1组播 → 有h264流 ✅
- **扫RTSP单播**: `ip route get 115.233.40.137` → `via <ROUTER_IP> dev enp4s0`(路由已通软路由);
  拉北京卫视4K RTSP → `hevc,3840,2160`+eac3 ✅; 554端口TCP可达 ✅
- **结论**: NAS2 经软路由(lan→IPTV转发+masq已配)可完整访问IPTV组播和RTSP专网,**架构最大风险已排除**

### 5.5.3 扫描性能(✅ NAS2实测)
- 单个频道(优化参数8M): ~7秒/个(串行)
- 并发10: 20个IP仅14秒(vs串行140秒,快10倍)
- 512个IP用合理并发(如20-40),预计几分钟扫完
- 优化参数下,之前被误判的4K(.63/.52)全部正确识别为3840 ✅

### 5.5.3 语言选型: Python (实测结论,非拍脑袋)
**实测扫描瓶颈 = 纯IO等待,CPU占用趋近0:**
```
单次组播probe: real 1.5~3.4s, 其中 user+sys(CPU) 仅 0.3s → 90%时间在等组播流
```
- 扫描是 **IO密集 (等IGMP生效/等关键帧/等probe缓冲)**,不是CPU密集
- 总耗时 = IP数 / 并发数 × 单次等待,**与语言无关,只跟并发数+超时有关**
- Go 的性能优势(CPU/高并发计算)在此用不上;Python asyncio 处理大量IO等待绰绰有余
- Go 唯一实际优势是部署(单二进制),但可用 Python Docker 化解决
- **结论: 扫描器用 Python + asyncio(或线程池)。ETL/生成/DB管理也Python(逻辑复杂不吃性能)**

---

## 五.六、扫描质量优化(重点,当前脚本"糙"的根因)

### 5.6.1 实测证据: 高码率流被误判
```
233.50.201.63 (CCTV16奥林匹克4K):
  糙参数(analyzeduration=1.5M, probesize=1.5M, +nobuffer): width=0  → 误判"不可用" ❌
  优化参数(analyzeduration=8M, probesize=8M, 正常缓冲):     width=3840 → 正确识别4K ✅
```
"扫不出来的实际是在的" = 高码率流(4K ~32Mbps)在 1.5M probesize 内采不到完整GOP/关键帧。

### 5.6.2 当前脚本的问题
| 问题 | 现状 | 后果 |
|------|------|------|
| 超时太短 | TIMEOUT=5~6s | 高码率/网络抖动误判超时 |
| probe参数小 | analyzeduration/probesize=1.5M | 高码率流采不到关键帧→width=0 |
| +nobuffer | 加了 | 对需缓冲等关键帧的组播有害 |
| 并发过高 | 10并发 | 多路组播抢IGMP/带宽互相干扰 |
| 单次探测 | 无重试或仅1次 | 网络抖动一次失败即判死 |

### 5.6.3 优化策略(新扫描器采用)
1. **动态 probesize/analyzeduration**: 默认8M/8s,给足高码率流采样空间
2. **去掉 +nobuffer**: 组播要缓冲等关键帧
3. **合理超时**: 单次≥10s(等IGMP+关键帧),分级:先快扫(判在线),再精扫(采分辨率/码率)
4. **控制并发**: 组播join有IGMP开销+带宽,并发不宜过高(如4~6),避免互相干扰
5. **多次确认+重试**: 失败重试2~3次,取最好结果;"不可用"要多次确认才判死
6. **两阶段扫描**:
   - 阶段A(快): 只判在线/分辨率 (probe)
   - 阶段B(慢): 只对在线的测实测码率 (抓N秒算bytes×8/duration,现channel_db.py的做法)
7. **码率实测**: ffprobe对组播bit_rate返回N/A,需抓流实测(已有逻辑,并入新扫描器)

### 5.6.4 扫描器职责边界(呼应三层解耦)
- 扫描器**只输出事实**: available/resolution/codec/fps/vbitrate/hdr/audio → 写 sources 表
- **不做**: 频道名匹配、分组、优选、命名规范化 (那些是ETL的事)

### 5.6.5 实测调优结论(2026-07-23,重构后真实压测)

**关键发现: 误报根因是 J1900 CPU 转发瓶颈,不是带宽/配置**
- msd_lite 配置其实正常: threadsCountMax=0(auto,4线程) + 大缓冲(precache=16384/ringBuf=24576),非单线程瓶颈
- 软路由 J1900(4核1.99GHz低功耗)在高并发组播转发(尤其多路4K HEVC)时 CPU 到瓶颈 → 临时拒绝(HTTP 5XX)
- 观察"eth1.43带宽用不满却有失效"的真相: 瓶颈在CPU转发+瞬时并发连接,不是带宽

**并发压测数据(30个已知可用源,各2轮):**
| 并发 | 结果 | 结论 |
|------|------|------|
| 6 | 22-25/30 (5-8个BUSY误报) | 降并发治标不治本 |
| 7 | 20-21/30 | 误报随并发升高增多 |
| 8 | 16-20/30 | 更差 |
- **教训**: 不存在"零误报的魔法并发数"。单纯降并发无法根治(6并发30源持续压仍误报)。
- 误报全是 **BUSY(临时忙)**,非 DEAD(源不存在)。

**正解: 两阶段扫描(已实装 scan_multicast.py)**
1. **probe.py 修正**: 5XX/服务器忙 → 判 `BUSY`(可重试),不再误判为永久 `DEAD`。
   只有 404 Not Found / Connection refused / No route 才是真 DEAD(不重试)。
2. **第一阶段**: 6并发快扫全部(BUSY无所谓,二阶段兜底)
3. **第二阶段**: 收集所有"非DEAD失败"源 → 2并发+重试 重扫(避开并发压力救回BUSY)
4. **验证**: BUSY源低并发重扫 **5/5 全救回**; 实扫201段(256IP) **BUSY归0、零误报残留**、可用源稳定163+
- 参数: `--workers 6`(一阶) `--workers2 2`(二阶) `--retry 2`。命名 udpxy→msd 正名(--msd主名,--udpxy兼容)。

**待调研(low)**: 组播引入LAN直接收RTP(ffprobe探 `rtp://@233.50.x.x:5140`),绕过msd_lite这个CPU瓶颈。
需软路由把组播路由/桥接到LAN(改网络配置,待同意)。届时并发可大幅提高。

### 5.6.6 端到端测试再深挖(2026-07-24,Round2揪出2个严重bug)

Round2(增量/全量测试)暴露了 5.6.5 方案的两个深层bug,修复后才真正稳定:

**Bug1(稳定性): ffprobe 卡死永不退出**
- 现象: full全量768段扫描卡死1小时41分未完,进程CPU 0%但ffprobe(PID)运行51分钟不退。
- 根因: ffprobe **缺 `-rw_timeout` 参数**,卡在读组播流(msd_lite接受连接但无数据)时永不超时退出;
  而外部 `os.killpg`/`communicate(timeout)` 对这种卡读的子进程**不可靠**(杀不掉/回收阻塞)。
- 修复(probe.py): 加 `-rw_timeout`(=外层timeout×0.8,微秒),让ffprobe**自己超时退出**,不依赖外部强杀。
  实测: 之前卡死的空地址,现在2-3秒返回。

**Bug2(误报): 超时太紧是主因,不只是并发**
- 现象: known增量扫305源,三轮后仍38个误报(标失效);单独探测这38个全部available=1。
- 根因: 这些是**慢源(单独探测需8s+)**,而重试轮超时一开始设成递减(8s)→ 越重试超时越紧 → 必然失败。
- 修复: 三轮**降并发+超时递增** `[(4,12),(2,15),(1,18)]`。

**双模式(用户设计,解决空地址拖慢)**
- `--mode full`: 扫全部768段(初始化/每月,发现新频道)。含463个未知空地址。
- `--mode known`: 只扫库里已知源(每周cron,快)。默认。
- 空地址(msd_lite对不存在组播也返5XX,与"忙"无法区分)只在full第1轮扫1次,**不进重试轮**(否则几百空地址反复重扫=1h41m元凶)。重试轮只重扫"已知源中失败的"。

**第1轮并发调优(关键发现: J1900甜点是4不是6)**
| 第1轮并发 | 80源第1轮失败 | 305源总耗时 | 误报 |
|-----------|--------------|------------|------|
| 6 | 10(→靠2轮救) | 830s | 0(但靠救) |
| **4** | **0** | **669s** | **0** |
- **反直觉结论**: 降并发反而更快。4并发J1900不过载→第1轮就扫准(305源仅7个失败)→省掉大量低并发救援时间。
- 过载(BUSY)的代价 > 降并发。**4并发才是低功耗CPU组播转发的真甜点**。

**Round2 最终成绩(4并发/三轮递增超时/双模式):**
- known增量305源: **305/305零误报, 669秒(~11分钟)**
- full全量768段: **不卡死, 1019秒(~17分钟), 发现1个新源**
- cron策略: 每周known增量, 每月full全量; 连续N次扫描都失败才标offline(避免误报下线)

---

## 五.七、运维层(周期运行 + 发布 + Dashboard)

### 5.7.1 周期运行(每周/每两周)
- NAS1 docker + cron/定时,完整流水线: **采集→ETL→生成→发布**
- 瞬时任务(跑完退出,不常驻),减少N100内存压力

### 5.7.2 变更检测(新增/下线)
- **新增**: 本次扫到、DB没有的 → 标记 status='new'(待人工识别,已抓3张截图)
- **下线**: DB有、连续**4次(4周)**未扫到可用 → 标记 status='offline'
- **产出**: 每次运行生成变更报告(新增X/下线Y/分辨率变化Z),存DB + Dashboard展示
- 实现: ETL层对比 scan_history

### 5.7.3 发布(复用NAS1现有Nginx,零改动发m3u)
```
现有: /volume1/docker/nginx/m3u/ → https://<PUBLISH_HOST>:<PUBLISH_PORT>/<name>.m3u
(现有旧文件: Zhejiang_Telecom_IPTV.m3u / china_telecom_tv.m3u / jinan_*.m3u)
iptv-radar生成的 iptv.m3u 写入/软链到该目录 → https://<PUBLISH_HOST>:<PUBLISH_PORT>/iptv.m3u
Nginx配置不动 ✅ (旧机制可替换,<PUBLISH_HOST>:<PUBLISH_PORT> 保留)
```

### 5.7.5 iptv-radar 项目目录结构(NAS1)
```
/volume1/docker/iptv-radar/
├── compose.yaml              # Docker Compose定义
├── data/
│   └── iptv.db               # SQLite主数据源(v2)
├── scripts/                  # Python脚本(采集/ETL/生成)
│   ├── scan_multicast.py
│   ├── scan_rtsp.py
│   ├── etl_process.py
│   ├── gen_m3u.py
│   └── run_pipeline.sh       # 一键流水线(cron调用)
├── output/
│   ├── iptv.m3u              # 生成的m3u(→软链到nginx/m3u/)
│   └── dashboard/            # 静态HTML Dashboard
│       ├── index.html
│       └── screenshots/      # 频道截图(带清理机制)
└── config.yaml               # 策略配置(分组顺序/命名规则等)
```
- NAS1/NAS2 目录映射: 若扫描想用NAS2,可映射;当前定在NAS1

### 5.7.4 Dashboard(静态HTML,参考myepg report风格)
- **形式**: 静态HTML(生成层每次导出),放独立docker项目目录
- **内容**:
  - 频道表格: 名/分辨率/编码/帧率/码率/HDR/音频/在线状态/**截图缩略图(点开放大)**
  - 统计: 总数/在线数/分辨率分布/4K数/码率排行
  - 变更时间线: 新增/下线历史
  - 在线率趋势(scan_history)
- **截图管理**: 存docker项目下专门目录; **清理机制**: 下线频道的截图/超过N期的旧截图定期删
- ⚠️ **Nginx发HTML需加location**(现有只放行.m3u),部署时改nginx.conf先经同意;
  或Dashboard docker自带轻量web端口

---

## 六、分阶段实施计划

### 阶段0: 设计定稿(本文档) ✅进行中
- 确认表结构、职责划分、规则归属

### 阶段1: 建新库 + 数据迁移(ETL基础)
- 建 channels/sources 新表结构(可新建 iptv_v2.db,不动老库)
- 写迁移脚本: 把老库459行 + channels.json + 现有m3u的人工映射(名/logo/分组/黑名单) 灌进新表
- **产出**: 一个数据完整的新库(频道-源关联好,主源初步选好)
- **验证**: 用新库能查出每个频道的所有源、主源

### 阶段2: 生成层(gen_m3u.py)
- 纯从新库读,复刻当前 merged_multicast.m3u 的分组/排序/源选择规则
- **验证**: 新生成的 m3u 和当前 merged_multicast.m3u diff,内容一致(证明迁移无损)

### 阶段3: ETL层(etl_process.py)
- 把阶段1迁移脚本里的"归并/优选/富化"逻辑独立成可重复运行的ETL
- **验证**: 重跑ETL,主源选择结果稳定合理

### 阶段4: 采集层改造(scan_*.py)
- 改现有扫描脚本,输出直接写 sources 表(而非json)
- 扫描只写事实,不做清理
- **验证**: 扫一遍,sources表技术属性更新

### 阶段5: 收尾
- 老 merge_m3u.py / scan_*.json 归档
- 更新文档/KB
- 一键流程: 采集→ETL→生成

### sub-agents 分工设想
阶段1定好库结构和接口后,阶段2/3/4的脚本互相解耦,
可以并行派 sub-agent: 一个写gen_m3u、一个写etl、一个改scan,
各自按 sources/channels 表的契约(schema)开发,最后集成。

---

## 七、待确认/风险

1. **迁移无损验证**: 阶段2用 diff 保证新旧m3u一致,是重构安全的关键
2. **老库并存**: 建 iptv_v2.db,老库保留,回滚零成本
3. **规则配置化**: 分组顺序等要不要抽成 config.yaml(vs 硬编码),阶段2再定
4. **单频道多源**: 暂不做(客户端支持差),但schema预留(sources表已支持一对多),将来开箱即用
