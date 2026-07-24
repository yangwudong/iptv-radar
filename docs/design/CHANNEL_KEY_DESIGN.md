# 频道归并与业务主键设计 (channel_key)

> 创建: 2026-07-23
> 目标: 解决"源没正确归并到频道→台标/信息匹配不全"的根本问题
> 呼应用户架构思路: 频道唯一标识 + 一频道多源 + 源失效不删只标记

---

## ★ 设计修订 V2 (2026-07-23 定案 — 以此为准,下方 V1 保留作历史)

V1 把 channel_key 定为 PRIMARY KEY,实现时演变成 channel_id/channel_key 双主键并存、
脚本混用、数据分叉。经与用户复盘,纠正为下述最终设计。

### 主键选型: 代理键 channel_id,而非自然键 channel_key
- **channel_id** INTEGER AUTOINCREMENT — 真正的主键/外键目标。无业务含义、稳定,
  频道改名不影响关联(本项目改名是常态: 别名归并/规范化)。数据库铁律: 主键用不变的代理键。
- **channel_key** TEXT UNIQUE — 规范名,给人读/沟通/台标匹配入口。加 UNIQUE 防重,但不做主键。
- 一句话: **channel_key 给人看, channel_id 给机器关联**。

### 三表职责边界(严格解耦)
| 表 | 职责 | 特点 |
|----|------|------|
| **channels** | 频道元数据台账 | 存所有探测/识别过的频道,**只增不删**(metadata风格,无源可用也保留);只放频道级基础信息(名/EPG名/台标/分组);**不含优选源** |
| **sources** | 采集到的播放源清单 | 扫描到就写(组播/单播);**进表≠被识别**,channel_id=NULL 即待识别孤儿;识别/匹配后才获得 channel_id |
| **channel_preferred_sources** | 优选关系(ETL产出) | 从 channels 拆出;带 rank(1=最佳),现阶段一频道一行,未来一频道多源按画质排序 |

### 关联列: sources 两列都留
- `channel_id` INTEGER FK→channels(channel_id) — 关联键(稳定,机器用),NULL=孤儿
- `channel_key` TEXT — **冗余可读快照**(`SELECT * FROM sources` 一眼看懂是哪个台),
  由 link_sources 每次重跑按 channel_id→channels.channel_key 统一回写,人不手改。
- 既符合范式(整数外键关联稳)又直观(查库看得懂),代价是 link_sources 顺手同步冗余。

### 最终 schema
```sql
-- 频道元数据(只增不删,不含优选源)
channels(
  channel_id   INTEGER PK AUTOINCREMENT,   -- 稳定代理主键
  channel_key  TEXT UNIQUE NOT NULL,       -- 规范名(人读入口),紧挨 channel_id 右边
  name         TEXT,                       -- 展示名
  tvg_id       TEXT,                        -- EPG匹配名
  tvg_logo     TEXT,                        -- 台标位置
  group_primary/group_extra/enabled/timeshift/sort_hint,
  status       TEXT   -- active / offline(源全失效仍保留) / placeholder(占位)
  -- 注: 删除 primary_source_id, 优选移到独立表
)

-- 采集清单(进表≠识别)
sources(
  source_id    INTEGER PK AUTOINCREMENT,
  channel_id   INTEGER FK→channels,        -- 关联键;NULL=待识别孤儿,紧挨 source_id 右边
  channel_key  TEXT,                        -- 冗余可读快照(link_sources回写)
  address      TEXT UNIQUE,
  ... 技术属性(采集层写) + quality_score(ETL写) ...
)

-- 优选(ETL产出,新表)
channel_preferred_sources(
  channel_id   INTEGER FK→channels,
  source_id    INTEGER FK→sources,
  rank         INTEGER,   -- 1=最佳,2=备选...(未来多源按画质)
  PRIMARY KEY(channel_id, rank)
)
```

### 未知/垃圾流处置(百视通BesTV那种"源能播但不对应真频道")
建**占位频道**集中挂靠,不进 m3u:
- `channel_key='__UNKNOWN__'` status=placeholder — 未知待查(可能是真频道没认出)
- `channel_key='__JUNK__'`    status=placeholder — 垃圾/测试/购物流(确认不要)

### 层与脚本对应
- 采集(scan_*/probe) → 只写 sources(新地址 channel_id=NULL)
- **数据清洗 = link_sources.py** → 给 sources 建立 channel_id 关联 + 回写 channel_key 冗余(不算优选)
- ETL = etl_process.py → 算画质评分,产出 channel_preferred_sources(带rank)
- 生成 gen_* → 统一按 channel_id join(不再混用)

---

## 一、问题回顾

当前 sources 表 334/475 是孤儿源(channel_id=NULL),导致:
- 同一频道的高清源和标清源没归并(如钱江频道高清关联了、标清是孤儿)
- 按地址反查频道→台标时,孤儿源查不到→台标缺失(用户发现的75/77/79号问题)

根因: 迁移时只关联了 m3u 用到的主源,ETL 的归并(E1)没做完。

---

## 二、核心设计: channel_key 业务主键

### 2.1 为什么不用自增ID/EPG id
- 自增ID(143): 无业务含义,不能沟通
- EPG id: 是**源级/画质级**的,同一频道多个号(钱江高清=5375944, 标清=2802),不适合做频道级主键

### 2.2 channel_key = 规范频道名
用 `浙江IPTV优选源列表` 里审查过的规范频道名做业务主键:
- 值如: `CCTV1综合`、`钱江频道`、`浙江卫视`
- 特点: 官媒官方名 或 能匹配EPG的名,人可读、可沟通
- 钱江高清+钱江标清 → 都归到 channel_key=`钱江频道`(去画质后缀)

### 2.3 频道别名 tag (提高匹配)
一个频道除了 channel_key,还有多个**匹配 tag**(别名/关键词):
```
channel_key="钱江频道"
  tags: ["钱江频道","钱江都市","钱江","浙江6","钱江频道高清","钱江频道标清"]
```
新资源来了,两级匹配:
1. channel_key 精确匹配
2. tag 模糊匹配(资源名/EPG名 含某个tag)→ 归到该频道
→ 大幅提高"各种叫法"的归并命中率

---

## 三、数据模型 (在现有表上扩展)

### channels 表(频道级,一频道一行,失效不删)
```
channel_key   TEXT PRIMARY KEY   -- 业务主键(规范频道名,如"钱江频道")
name          TEXT               -- 显示名(可=channel_key,或带画质)
tags          TEXT               -- 匹配别名,JSON数组或分号分隔
tvg_logo      TEXT               -- 台标(频道级,所有源共享)
tvg_id        TEXT               -- EPG匹配id(节目单用)
epg_ids       TEXT               -- 官方EPG的channelID(可多个,画质各异)
group_primary/group_extra/enabled/timeshift/status/sort_hint...
```
(注: 现有表用自增channel_id,重构时加channel_key列并以它为逻辑主键)

### sources 表(源级,多个,失效标记不删)
```
source_id     INTEGER PK
channel_key   TEXT               -- 所属频道(→channels.channel_key; NULL=孤儿待识别)
address       TEXT UNIQUE        -- 组播ip:port 或 rtsp url
source_type/available/resolution/.../fail_count/last_seen
```
关键: **任意 address → channel_key → 频道全部信息(名/台标/EPG)**

---

## 四、归并逻辑 (link_sources.py)

### 权威来源顺序(解决"官方列表不最全")
1. **官方 channels.json**: 地址→官方频道名(最准,169频道每个有组播+单播地址)
2. **我们的识别结果**: 扫描+截图识别的频道(官方没列的,如200.x备选源)
3. **tag 模糊匹配**: 上面没命中,用别名tag匹配
4. **都不行**: 孤儿源,channel_key=NULL,标记 status=待识别(不丢)

### 归并步骤
```
1. 建 channel_key 主数据: 用优选列表(enabled频道)的规范名 + 建tags(含各种别名)
2. 官方台账映射: 解析channels.json,建 address→规范名 映射
   (官方名"中央一套高清" → 规范名"CCTV1综合" 用NAME_OVERRIDES同款规则)
3. 遍历所有source: 按 address查官方映射 → 得channel_key → 回填sources.channel_key
4. 台标/EPG-id: 绑定到channel_key(频道级)
5. 孤儿源: 标记待识别
```

### 失效处理(呼应用户)
- 源失效: available=0, fail_count累加, **不删**
- 频道所有源失效: status=offline, **频道行保留**(名/台标/tag都在)
- 以后源恢复/新增,还能挂回这个channel_key

---

## 五、验证目标
- 归并后 sources.channel_key 覆盖率 (目标: 官方169频道的源全部归并)
- 台标: 官方页面按 address→channel_key→台标,覆盖率大幅提升(解决75/77/79)
- 孤儿源数量(剩下的都是官方没有、tag也没匹配上的真未知源)

---

## 六、分步实施(先验证)
1. **本步**: 写 link_sources.py, 加channel_key, 归并, 输出归并报告验证
2. 下步: 台标匹配改成 address→channel_key→台标
3. 再下步: 整合进ETL, 两个页面都基于channel_key
