# 孤儿源识别流程设计 (ORPHAN_REVIEW)

> 创建: 2026-07-24
> 目标: 设计"待识别孤儿源"的人工处理流程。核心原则: 全异步文件交换,无常驻服务。
> 状态: Python侧待实现; Electron App 独立项目后做(本文档为其提供完整上下文)。

---

## 一、背景与约束

### 什么是孤儿源
扫描发现的组播/单播源,`sources.channel_id IS NULL` = 未归并到任何频道。分两类:
- **可自动归并的**: 官方 channels.json 有 或 source_links.json 快照有 → link_sources 自动处理,不算孤儿。
- **真未知孤儿**(本流程处理对象): 官方和快照都没有的源(如百视通BesTV那种"能播但不对应真频道"、
  购物/测试流、改了地址的频道)。需人工看画面识别。

### 硬约束(决定了架构)
1. **pipeline 是瞬时任务**: NAS Docker + 群晖cron 触发,执行完即退出,**不常驻、不占资源**。
2. **人工交互要友好**: 不能让用户查 SQLite;要能看画面(截图/播放)再决定。
3. **要能脱离 AI 独立运行**: 交互工具是给人用的,不依赖 AI 辅助。
4. **cron 环境无交互终端**: 排除"命令行问答"式工具(容器里 cron 跑没有 tty)。

### 结论: 异步文件交换(三方解耦)
pipeline(产出) → Electron App(人工识别) → pipeline(消费),通过 **json 文件**交换,
三方全解耦、全异步、无任何常驻服务/后端API。

---

## 二、整体流程

```
① NAS pipeline (cron瞬时任务,发现孤儿源时):
     生成"待识别包" → output/orphan_review/
       ├── orphans.json      待识别孤儿源清单(地址/技术属性/播放URL)
       └── shots/            每个源3张截图 (<ip>_1.jpg ...)
                    ↓ 文件夹同步到 Mac/Win (手动拷贝 / 共享目录 / 未来自动)
② Mac/Win Electron App (人工,想弄才开,平时零占用):
     加载 orphans.json → 逐个看截图 + IINA/内置播放器播源 → 选匹配的频道(或新建/拉黑/跳过)
     → 导出"识别结果" → resolved.json (地址 → 决定)
                    ↓ 结果json放回 NAS 待处理目录 data/orphan_inbox/
③ 下次 NAS pipeline 执行 (启动时先检查):
     发现 data/orphan_inbox/*.json → 读取 → 写库(建立源↔频道关系) + 合并进 source_links.json
     → 处理完的结果json归档到 data/orphan_inbox/done/
     → 之后正常跑 link_sources,孤儿源已归并
```

**关键**: pipeline 只多了两个文件操作(产出待识别包、消费识别结果),仍是瞬时任务。
Electron App 是独立客户端,不进 cron/Docker,你想弄才开。

---

## 三、JSON 契约(核心,三方交换的根基,先定死)

### 3.1 产出: orphan_review/orphans.json (pipeline → App)
```json
{
  "generated_at": "2026-07-24T10:30:00",
  "msd_prefix": "http://<msd_lite地址>/rtp/",   // 播放URL前缀(脱敏:实际值运行时填,或App侧配置)
  "channels": [                                  // 可归属的频道清单(供App做下拉/tag匹配)
    {"channel_key": "CCTV1综合", "name": "CCTV1综合", "group": "央视"},
    {"channel_key": "浙江钱江都市", "name": "浙江钱江都市", "group": "浙江"}
    // ... 全部143个真实频道
  ],
  "placeholders": [                              // 拉黑用的占位频道
    {"channel_key": "__UNKNOWN__", "name": "未知待查"},
    {"channel_key": "__JUNK__", "name": "垃圾/测试流"}
  ],
  "orphans": [                                   // 待识别的孤儿源
    {
      "address": "233.50.200.233:5140",
      "source_type": "multicast",
      "res_label": "1080P", "video_codec": "h264", "fps": 25.0,
      "hdr": "SDR", "audio_codec": "mp2",
      "play_url": "http://<msd>/rtp/233.50.200.233:5140",  // 组播: msd前缀拼; 单播: rtsp原样
      "iina_url": "iina://weblink?url=...",                // 一键IINA播放
      "shots": ["shots/233_50_200_233_1.jpg", "shots/..._2.jpg", "shots/..._3.jpg"]
    }
    // ...
  ]
}
```

### 3.2 消费: orphan_inbox/resolved_<时间>.json (App → pipeline)
```json
{
  "resolved_at": "2026-07-24T14:00:00",
  "decisions": [
    {"address": "233.50.200.233:5140", "action": "assign", "channel_key": "某某频道"},
    {"address": "233.50.200.234:5140", "action": "new",    "channel_key": "新频道名", "group": "其他"},
    {"address": "233.50.200.235:5140", "action": "junk"},      // 拉黑→挂__JUNK__
    {"address": "233.50.200.236:5140", "action": "unknown"},   // 未知→挂__UNKNOWN__
    {"address": "233.50.200.237:5140", "action": "skip"}       // 跳过,下次再看(不写库)
  ]
}
```

**action 类型**:
- `assign`: 归并到已有频道(channel_key必填)
- `new`: 是官方没有的新频道 → 建新频道(channel_key+group) + 归并。新频道归到所属分组末尾(order=max+1)
- `junk`: 垃圾/测试流 → 挂 __JUNK__ 占位
- `unknown`: 拿不准但确定不是正常频道 → 挂 __UNKNOWN__ 占位
- `skip`: 本次不处理,保持孤儿,下次待识别包还会出现

### 3.3 落库规则(pipeline消费时)
- `assign`/`new`: 写 source_links.json 快照(address→channel_key) + sources表回填channel_id
- `new`: 额外在 channels 表建新频道(status=active,enabled=1,归所属分组末尾) + channels_seed.json 更新
- `junk`/`unknown`: sources.channel_id 指向占位频道,不进m3u
- `skip`: 不动
- 处理完的 resolved json 移到 data/orphan_inbox/done/ 归档(留痕)

---

## 四、Electron App 设计要求(独立项目,后做)

### 4.1 定位
纯客户端桌面App(Mac/Win),**只读写上述两个json文件**,不连数据库、不连pipeline。
平时不运行,用户想识别孤儿源时打开,处理完关闭。

### 4.2 核心功能
1. **加载**: 选择/拖入 orphans.json(及同目录shots/) → 展示孤儿源列表
2. **看画面**: 每个孤儿源显示3张截图缩略图(点击放大);提供两种播放:
   - 内置播放器(如 video.js/mpegts.js 播组播http流,注: 组播需msd前缀,H.265可能不支持→提示用IINA)
   - 一键 IINA(iina_url,调起本地IINA)
3. **匹配决定**: 每个源选一个 action:
   - 归到已有频道: 下拉/搜索选 channel_key(从json的channels列表,支持模糊搜索/tag匹配)
   - 新建频道: 填频道名+选分组
   - 拉黑: junk / unknown
   - 跳过: skip
4. **导出**: 生成 resolved_<时间>.json,提示用户放回 NAS 的 data/orphan_inbox/

### 4.3 技术建议
- Electron + 简单前端(React/Vue或纯HTML均可,App简单)
- 内置播放: mpegts.js(组播TS流) — 但注意 CORS(App是本地file://或localhost,可能需App侧代理或直接IINA)
- 调 IINA: `iina://weblink?url=<encoded>` (Mac);Win 可考虑 potplayer/mpv 协议
- 打包: electron-builder,出 dmg/exe

### 4.4 交互体验目标
- 一屏一个源(或列表+详情): 截图大图 + 技术信息 + 播放按钮 + 频道选择器
- 键盘友好: 上下切换源,快捷键选常用action
- 进度可见: X/Y 已处理,可中途保存草稿继续

### 4.5 与主项目的边界
- App 不进主 repo 的 pipeline,可以是 repo 内的独立子目录(如 `orphan-review-app/`)或独立repo
- 唯一耦合点: 上述 §3 的 json 契约。契约不变,App 和 pipeline 可各自独立演进。

---

## 五、实施顺序

1. **[本次] Python侧产出**: 改 pipeline/scan,发现孤儿源→生成 orphan_review/(orphans.json+截图)
2. **[本次] Python侧消费**: pipeline启动时检查 orphan_inbox/*.json→读取→写库归并→归档
3. **[本次] 手工验证契约**: 手写一个 resolved.json,验证消费端能正确落库(assign/new/junk/skip)
4. **[后续独立项目] Electron App**: 按 §4 实现,基于已定死的 json 契约

先做1-3(Python侧闭环+契约验证),App留待独立session。契约(§3)是根基,一旦定死,App可独立开发。
