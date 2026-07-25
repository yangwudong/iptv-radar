# 孤儿源识别工具 — 实施计划书

> 创建: 2026-07-25
> 需求(用户原话):
> 「给我个链接，我可以通过 IINA 打开他们，然后人工识别之后，我要能选择或者填出来 channel 的名称
> （最好能 auto complete）之前的，当然也可以自己新加的。估计还要选择分组对吧？或者创建新的分组。」
> 「生成个 JSON 让我下载，我 smb 复制到 docker 下面的哪里好了。甚至于说，JavaScript 都可以生成一个
> scp 命令，我直接一键搞定。」
> 「更新的话，一周一次就行了。」
>
> 契约: [ORPHAN_REVIEW.md §3](./ORPHAN_REVIEW.md) 的两个 json（已定死，不改）

---

## 一、最终方案：纯静态页面，零基础设施改动

```
① NAS pipeline 产出(每周 cron 自然产生)
     output/dashboard/orphan-review.html      ← nginx 已挂载该目录,直接可访问
     output/dashboard/orphan-shots/*.jpg

② 浏览器打开(任何设备,不用挂 SMB)
     https://<PUBLISH_HOST>:<PUBLISH_PORT>/dashboard/orphan-review.html
     · 看截图 → 点「▶ IINA」用本机 IINA 播(Mac 能直连专网,已实测)
     · 频道名 autocomplete(143个已有频道,显示所属分组)
     · 或新建频道: 填名字 + 选分组(15个现有组)或输入新组名
     · junk / unknown / skip
     · 进度存 localStorage,关了再开不丢

③ 点「导出」→ 浏览器下载 resolved_<时间>.json
   页面同时给出两条一键复制的命令,二选一:
     ┌ scp -O -P <port> ~/Downloads/resolved_xxx.json \
     │   <user>@<nas>:/volume1/docker/iptv-radar/data/orphan_inbox/
     └ 或: cp ~/Downloads/resolved_xxx.json \
           /Volumes/docker/iptv-radar/data/orphan_inbox/     (若已挂 SMB)

④ 下次每周 cron(周四 04:00)自动消费
     pipeline 的 [0/7] 步 orphan_import 读 inbox → 落库 → 归档到 done/
     → 之后正常 link_sources/etl/生成/发布,识别结果自然生效
```

**这个方案的取舍**：接受"最长等一周生效"，换来**零新增服务、零 nginx 配置改动、零挂载依赖**。用户判断：播放列表不常变，不值得为即时生效加基础设施。

### 为什么不是其他方案（都已评估并放弃）

| 备选 | 放弃原因 |
|---|---|
| Electron App | 播放由 IINA 完成、写结果只是下载文件 —— 它唯一优势(直接读写本地文件)用不上，却要背打包/签名/更新/CVE 维护。且是一次性任务 |
| 依赖 SMB 挂载打开页面 | 用户明确不喜欢"干活前先挂远程盘"。走 nginx URL 更直接（SMB 仅作为**放回结果**的可选途径） |
| nginx WebDAV 收提交 + 每2分钟 cron 消费 | 技术可行（nginx 已编译 `--with-http_dav_module`），但要改 nginx.conf + 加高频 cron。用户判断"一周一次够了"，不值 |
| 常驻小服务 + 反代 | 最即时(几秒)，但多一个常驻容器要维护/打补丁，且破坏 ORPHAN_REVIEW「无常驻服务」原则 |

---

## 二、已核实的基础设施事实

| 事实 | 实测结果 |
|---|---|
| nginx 挂载 | `output/dashboard → /usr/share/nginx/html/dashboard`，页面放这里直接可访问 |
| Mac 直连播放 | ✅ 单播 rtsp（h264 1080p）、组播经网关 HTTP 都能播 → **IINA 点开就能看** |
| `data/orphan_inbox/` | ✅ 已存在，`drwxrwxrwx`，容器（uid 1026）能读能归档（实测） |
| SMB（可选路径） | `//jack.yang@…/docker → /Volumes/docker` 已挂；Mac 写入后 NAS 侧属主自动 `1026:100` |
| 待识别孤儿 | **27 个**（17 组播 + 10 单播，全 1080P h264） |
| 10 个单播孤儿的 token | **全部有** `timeshift_query`（529–555 字符）→ 拼上就能播 |
| 每周 cron | 周四 04:00 known 增量（`--publish`），会自动跑 `orphan_import` |

---

## 三、动手前要处理的几件事

### 3.1 ~~单播 IINA 链接是坏的~~ → 实测不成立，无需修

我一度判断"单播的 `play_url` 是裸 rtsp 地址、缺 token、IINA 打不开"。**用户指出后实测证伪**：

```
裸地址(库里的 address)        : ✅ 能播 h264,1920,1080
带 token(address + ?query)   : ✅ 能播 h264,1920,1080
```

**直播不需要 token，token 只在回看（`playseek`）时才需要。** 现有 `play_url` / `iina_url` 本来就是好的，
组播和单播 IINA 都能直接打开（用户此前已实际用过）。此项撤销。

> 教训: 已被用户实测过的结论不要凭代码推断反复质疑；要验证就先跑一次，别先写进计划。

### 3.2 单播源没有截图

`need_shot` 写死 `stype == 'multicast'`，10 个单播源一张截图都没有。主要靠 IINA 看，所以**不是阻断项**，但有截图能一眼跳过明显的垃圾流。建议一并打开（已有复用机制，只拍新孤儿）。

### 3.3 页面与截图要输出到 nginx 已挂载的目录

现在待识别包在 `output/orphan_review/`，**不在** nginx 挂载范围内。需改成输出到 `output/dashboard/`（已挂载）：
- `output/dashboard/orphan-review.html`
- `output/dashboard/orphan-shots/*.jpg`（避免与 Dashboard 自身的 `screenshots/` 混淆）

`orphans.json` 仍留在 `output/orphan_review/` 作为契约产物（供将来别的消费端用），页面则把数据**内联**进 HTML（省一次 fetch，也避免把含 token 的 json 暴露成可直接下载的 URL）。

### 3.4 ~~待识别包会累积~~ → 实测不成立，已经就是"只留最近一份"

我说过"从不清理，只累积"，**用户指出后实测证伪**：

| 产物 | 实际行为 |
|---|---|
| `orphans.json` | `open(...,'w')` **覆盖写**，永远只有一份 |
| 截图 jpg | 文件名按地址派生、固定为 `<ip>_1.jpg`（不带时间戳）→ 同一个源重拍**原地覆盖**，不会增多 |

实测当前状态：磁盘截图对应 **17** 个源 = 当前组播孤儿 **17** 个 → **零残留**。

唯一理论上的残留场景：某个源被归并/标 junk 后不再是孤儿，它那 3 张 jpg 不会被主动删。
但这属于"极少量、且下次同名地址重现时会被覆盖"，实测目前为 0，**不值得为它写清理逻辑**。
若哪天真堆多了，一条 `find` 就能清，不必进代码。

### 3.5 `junk`/`unknown` 标记不进归并快照（已确认要修）

> 这里的"快照"是 `data/source_links.json`（**归并快照**：记录"哪个地址属于哪个频道"，447 条，进 git），**不是截图**。

`orphan_import` 对 `junk`/`unknown` 刻意不写快照。这些标记只存在数据库 `sources.channel_id` 里。已验证它能在 `link_sources` 重跑后存活（走 DB 种子路径），但**万一库丢了从快照重建，标好的 17 个垃圾流会全部变回孤儿，得重标一遍**。

建议一并写入快照（多 17 条指向占位频道的记录，无害）。

---

## 四、落库端已修好（本次核实时发现是坏的）

| 问题 | 影响 | 状态 |
|---|---|---|
| `action:new` 的 INSERT 列数不匹配（7 值塞 6 列） | **建新频道直接 OperationalError** —— 最需要的功能完全不能用 | ✅ 已修 |
| 快照仍写旧格式（字符串） | 识别结果以旧格式落盘，频道改名就丢 | ✅ 已修 |
| 该文件完全没有测试 | 所以第一个 bug 从"删 group 列"那次提交起一直坏着没人发现 | ✅ 补 8 条测试 |

现被测试锁住的行为：`new`（建频道+`enabled=1`+归组内末尾）、`new` **可创建全新分组**、`assign`、`junk`/`unknown`（挂占位且**不进 m3u**）、`skip`（不动库）、快照写**新格式**。

---

## 五、页面长什么样

```
┌──────────────────────────────────────────────────┐
│ 孤儿源 3/27   ●已决定12 ○待定15    [导出结果]     │
├──────────────────────────────────────────────────┤
│ rtsp://115.233.40.137/PLTV/.../53485722.smil     │
│ 1080P · h264 · 25fps · mp2 · 单播                │
│   [ ▶ 用 IINA 打开 ]   [ 复制地址 ]              │
│   (3 张截图缩略图,点击放大)                       │
├──────────────────────────────────────────────────┤
│ 这是哪个频道?                                     │
│  ● 归到已有频道                                   │
│    [ 钱江              🔍 ]  ← autocomplete       │
│      浙江钱江都市 (浙江)                          │
│  ○ 新建频道  名称[____]  分组[其他▾]或新建[____]  │
│  ○ 垃圾/测试流  ○ 拿不准  ○ 跳过                  │
├──────────────────────────────────────────────────┤
│         [← 上一个]         [下一个 →]            │
└──────────────────────────────────────────────────┘
导出后弹出:
  ✅ resolved_20260726T1030.json 已下载
  放到 NAS 即可(下次周四 cron 自动生效):
  [ 复制 scp 命令 ]  [ 复制 cp 命令(SMB) ]
```

**额外要有**：
- **「一键全部标 junk」**（已确认要做）：17 个组播孤儿大概率都是 BesTV 垃圾流，逐个点太累。
  做成"筛选后批量应用"：先按类型/分辨率筛，勾中的一次性标 junk，避免误伤
- **键盘操作**：`↑/↓` 切源、`1`~`5` 选 action、`/` 聚焦搜索、`Enter` 确认并下一个
- **草稿**：`localStorage` 存决定，27 个源可以分几次认

**明确不做**：内置播放器（IINA 干）、直接写库/上传（需后端或改 nginx）、登录/权限。

---

## 六、实施步骤

| 步 | 内容 | 工作量 | 必做 |
|---|---|---|---|
| 1 | 单播也截图（`need_shot` 去掉 multicast 限制） | 小（~5 行 + 测试） | 建议 |
| 2 | 产出目录改到 `output/dashboard/`（nginx 已挂载） | 小 | **是** |
| 3 | `junk`/`unknown` 写入归并快照 | 小（~5 行 + 测试） | **是**（已确认） |
| 4 | 生成 `orphan-review.html`（Jinja2 模板，风格同 Dashboard，数据内联） | 中（~350 行） | **是** |
| 5 | 页面导出时生成 scp / cp 命令（一键复制） | 小 | **是** |
| 6 | NAS 生成真实待识别包（27 个源），你实际用一遍 | 小 | **是** |
| 7 | 落库验证（见下） | 中 | **是** |

### 第 7 步验证清单（不能省）

`orphan_import` 现在有测试了，但**从未在真实数据上跑过**：

1. 备份 NAS 的库 + `source_links.json`
2. 存下当前三套 m3u 作黄金基线
3. `--dry-run` 先看输出，不写库
4. 只放 2–3 个决定（1 个 `assign` + 1 个 `junk`）真跑，检查：
   `sources.channel_id`/`channel_key` 正确、快照是**新格式**、`junk` 的源**没进 m3u**
5. 同一个 json 再消费一次 → 不应重复建频道（幂等）
6. 跑一次完整 `link_sources` → 确认 `junk`/`unknown` **没被打回孤儿**
   （正是本轮修的 bug：早期 `enabled=0` 占位频道的源会被打回，标记全白做）
7. 对比黄金基线：只有被 `assign` 的频道该变
8. 通过后再处理剩余源

> 注意：因为不加 `--import-only`，第 3–7 步的验证我会**直接调 `orphan_import.py`** 完成（不必跑整条 pipeline）。
> 你日常使用则是等每周 cron 自动消费。

---

## 七、已确认的决定

| 项 | 决定 |
|---|---|
| 整体方案 | **纯静态页面 + 下载 json + 手动放回 + 每周 cron 生效**（零新增服务/零 nginx 改动/零挂载依赖） |
| `junk`/`unknown` 写归并快照 | ✅ 要写（防库丢失后重标 17 个垃圾流） |
| AI 预识别 | ❌ 不做（接入成本 + 使用频率低，不值） |
| 一键全部标 junk | ✅ 做（17 个组播孤儿大概率都是 BesTV 垃圾流，逐个点太累） |
| 待识别包保留策略 | 无需改 —— 实测本来就是"只留最近一份"(json 覆盖写、截图按地址固定命名原地覆盖，当前零残留) |
| 单播 IINA 链接 | 实测本来就好的（直播不需要 token），**无需修** |

---

## 八、开工前的交接清单（compact 后接手直接看这里）

### 起点状态（2026-07-25 定稿时）
- HEAD `3e32696`，已推送，工作区干净，**102 条测试全绿**
- 跑测试: `.venv-test/bin/python -m pytest`（本机虚拟环境，gitignore）
- NAS 已部署最新镜像（`latest` = `e15dd18` 之后的构建），库 schema 已迁移完成
- **NAS 才是权威数据源**（474 个源），本地 `data/iptv.db` 是旧拷贝（473 个）—— 验证时注意

### 按此顺序实施（先数据侧、后界面，每步先写测试）

**步骤 3 —— `junk`/`unknown` 写入归并快照**
- 文件: `src/orphan_import.py`，`apply_decision()` 里 junk/unknown 分支目前 `return ..., False`
  （第二个返回值 = 是否改动 snapshot），且不写 `snapshot[addr]`
- 改成写 `snapshot[addr] = {'channel_id': <占位频道id>, 'channel_key': '__JUNK__'}` 并返回 True
- ⚠️ 必须同时确认 `link_sources` 加载这类快照条目时**不会**把源当成正常归并
  （`key2id_all` 含禁用频道，所以会正确指回占位频道 —— 但要写测试锁住）
- 测试: 库丢失后从快照重建 → 17 个垃圾流不该变回孤儿

**步骤 1 —— 单播也截图**
- 文件: `src/orphan_export.py`，`need_shot = (stype == 'multicast' and ...)` 去掉类型限制
- 单播截图用 `play_url`（裸 rtsp 地址即可，**直播不需要 token**，已实测）
- 代价: 10 个单播源首次导出多花几分钟；已有复用机制，只拍新孤儿

**步骤 2 —— 产出目录挪到 nginx 已挂载处**
- 现在: `output/orphan_review/`（**nginx 访问不到**）
- 改成: `output/dashboard/orphan-review.html` + `output/dashboard/orphan-shots/*.jpg`
- nginx 已挂 `output/dashboard → /usr/share/nginx/html/dashboard`
- `orphans.json` 仍留在 `output/orphan_review/`（契约产物），**不放进 dashboard**
  （那 10 个单播地址含 token，少一个可直接下载的 URL 更好）

**步骤 4 —— 生成 `orphan-review.html`**
- 用 Jinja2（项目已有 `src/template_util.py` + `src/templates/`，风格照 `dashboard.html`）
- 数据**内联**进 HTML（不 fetch，`file://` 下也能用）
- 截图用 `<img src="orphan-shots/x.jpg">`（img 不受 CORS 限制）
- 必备: IINA 按钮（`iina_url` 现成可用）/ 频道名 autocomplete（143 频道，显示所属分组）/
  新建频道（名字 + 选现有分组或输入新组名）/ junk / unknown / skip /
  **批量标 junk**（先筛选再勾选应用，避免误伤）/ localStorage 草稿 / 键盘操作

**步骤 5 —— 导出时给出一键复制命令**
```
scp -O -P <port> ~/Downloads/resolved_xxx.json <user>@<nas>:/volume1/docker/iptv-radar/data/orphan_inbox/
cp ~/Downloads/resolved_xxx.json /Volumes/docker/iptv-radar/data/orphan_inbox/   # 已挂 SMB 时
```
- 真实主机/端口从 `.env` 读，**不要写死在代码或文档里**（AGENTS.md 规则2）

**⚠️ 步骤 6/7 时必须一并做的 NAS 库更正（用户已同意，别忘）**
- `央广购物`（channel_id=132）在 NAS 活库里仍在 `其他` 组，要挪进 `购物`：
  `UPDATE channel_groups SET group_name='购物', order_in_group=1 WHERE channel_id=132;`
- 为什么必须手动：**run_pipeline.sh 不 load 种子**，本地库改动 / `channels_seed.json`
  只对"从零重建"有效，传不到 NAS 活库。NAS（474源）才是权威，本地是旧拷贝（473）。
- 做之前先备份 NAS 库；改完 `--gen-only` 重发，并给用户看 m3u diff
  （预期：三套各只有央广购物一处 其他→购物）。

**步骤 6/7 —— 真实数据验证（不能省）**
见本文 §六「第 7 步验证清单」8 条。重点:
`--dry-run` → 小批量 2-3 个 → 查快照是**新格式** → 验幂等 → **验 junk 不被 link_sources 打回**

### 两个已撤销的错误判断（别再犯）
1. ~~"单播 IINA 链接缺 token 打不开"~~ → 实测裸地址直接能播，**直播不需要 token**
2. ~~"待识别包只累积不清理"~~ → 实测 json 覆盖写、截图按地址固定命名原地覆盖，**当前零残留**

两次都是"看到代码里没有某个处理就断定行为有问题，而没去看实际产物"。
**动手前先跑一次看真实结果，别把推断写进计划。**

### 不做的事（已决策，别重开）
- ❌ Electron App（播放由 IINA 完成、写结果只是下载文件，唯一优势用不上）
- ❌ nginx WebDAV 收提交 / 常驻小服务（用户判断"一周一次够了"，不值加基础设施）
- ❌ AI 预识别（接入成本 + 使用频率低）
- ❌ 依赖 SMB 挂载**打开页面**（用户不喜欢；SMB 仅作为放回结果的可选途径）
