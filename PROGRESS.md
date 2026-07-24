
## 2026-07-23 扫描器改进(基于实测诊断)
诊断发现的问题+修复:
- **并发过高偶发失败**: 全段10并发时,正常频道(如CCTV5体育197)偶尔NO_VIDEO
  → 默认并发 10→5,降低IGMP/带宽争抢
- **NO_VIDEO不区分**: 源不存在 vs 偶发拿不到帧 混为一谈
  → probe区分: DEAD(5XX/404源不存在,不重试) / NO_VIDEO(偶发,降速重试) / AUDIO_ONLY(仅音频=广播类,有效)
- **重试无效**: 之前连续快速重试(并发压力还在)
  → 失败后 sleep 1.5s 降速重试,错开并发
- **广播误判**: 有音频无视频的被判失败 → AUDIO_ONLY标记为available

Dashboard分类修正:
- 广播只看分组(之前用"无视频"判,导致衢州/台州扫描失败也变广播)
- "未知"→"未知/不稳定"(扫描失败/DEAD/新频道)
- 编码 H264→H.264, HEVC→H.265
- 音频按编码分色(mp2/aac/ac3/eac3)
- 组播(靛蓝)/单播(橙)tag
- 过滤按钮统一等高(图标60px, 全部/未知用emoji占位)
- 表头全部居中

## 2026-07-23 孤儿源识别(channel_key归并后)
- 161孤儿源 → 批量截图(Mac直连msd_lite) + 4个subagent并行zai识别 + 人工复核
- 归并结果: 444源已归到频道, BesTV未知13(不入m3u), 剩18孤儿(购物/测试/花屏/14个RTSP待处理)
- 新建频道: 浙江政务(归"其他"组,首个走完整流程新建的频道)
- 别名映射: 都市剧场→都市剧场_上海/钱江→浙江钱江都市/HTV编号→杭州台/NBTV→宁波台等
- 截图归档 reference/orphan_shots/ (不上git), 待办清单 reference/orphan_todo.md
- 遗留: 14个RTSP单播孤儿源(扫描发现,名字没对上,后续处理)

## 2026-07-23 架构核查 + 持久化根治
核查结论: 三层解耦✅ / channel_key归并✅ / 源优选✅ / 单m3u✅ / 台标治本✅ / 源失效标记✅
修复的问题:
- CCTV5+/CCTV5顺序回退 → 改回(CCTV5体育在前)
- BesTV未知悬空channel_key(13源指向不存在频道) → 清理为孤儿
- **持久化隐患根治**: 人工归并结果存 data/source_links.json 快照,
  link_sources重跑优先加载+自动回写,保证重跑不丢人工归并(验证449归并稳定不回退)
最终: 144频道/449源归并/0悬空key/0无主源频道/m3u 155条,端到端一致

## 2026-07-23 数据库 schema 重构 V2 (根治双主键)
问题(用户复盘): channel_key(文本频道名)当外键不合理——名字会变,关联会断;
且实现成 channel_id/channel_key 双主键并存、脚本混用、数据分叉(308源只有channel_key无channel_id)。
方案(方案A, migrate_v2.py 一次性迁移,已跑完):
- channels: channel_key 挪到 channel_id 右边+加UNIQUE, 删 primary_source_id (频道不存优选源)
- sources:  channel_id(整数)为关联键, channel_key 降为可读冗余(SELECT*一眼看懂), 回填对齐308分叉源(141→449)
- 新表 channel_preferred_sources(channel_id+source_id+rank): 优选从channels拆出,未来一频道多源按画质排
- 占位频道 __UNKNOWN__/__JUNK__ (enabled=0) 集中挂靠未知/垃圾流
- 6脚本统一: link_sources回填channel_id+回写channel_key冗余; etl/gen_m3u/gen_dashboard 改join优选表; gen_channels_page统一channel_id join; db_schema.py权威schema同步
验证: 146频道(144+2占位)/449源归并/0悬空/0两列不一致/优选144条/m3u155条(与重构前一致)/CCTV5顺序对

## 2026-07-23 台标核查 + 频道更名合并
manual test发现: channels.html 中国教育一套用了CCTV1台标。
全量核查169官方频道"台标vs频道名",发现:
- 中国教育一套(233.50.201.75+smil)错归到CCTV1 → 改快照改归中国教育1台(治本)
- 多彩文体4K: 欢笑剧场2026-3更名多彩文体(SiTV上海首个4K频道),台标欢笑剧场4K→多彩文体4K.png(myepg)
- 欢笑剧场_上海(108)实为多彩文体FHD版=25重复记录 → 源并入25+DELETE 108(纠错合并,非只增不删范畴)
  25现4源(4K主源+3×1080P),符合一频道多源
其余15处"官方名≠快照名"确认是有意人工归并(睛彩/浙江政务/中国蓝,官方名反而不准),不动。
结果: 143频道/449源归并/m3u154条。补充"只增不删"边界:数据纠错(重复合并)不在其内。

## 2026-07-23 扫描优化(源头降误报) + 种子重建能力
背景: 用户观察"eth1.43带宽用不满却有失效",质疑扫描误报。先优化源头再谈失效判定。
调优过程(数据驱动):
- SSH只读查msd_lite配置: threadsCountMax=0(auto/4线程)+大缓冲,配置正常。瓶颈是J1900低功耗CPU转发。
- 30源并发压测: 6并发5-8个BUSY误报,7/8更差 → 降并发治标不治本。误报全是BUSY(临时忙)非DEAD。
- 验证两阶段: BUSY源低并发(2)重扫 5/5救回。
实装:
- probe.py: 5XX→可重试BUSY(不再误判永久DEAD),只有404/Connection refused/No route才DEAD。
- scan_multicast.py: 两阶段(一阶6并发快扫+二阶2并发重扫非DEAD失败源) + udpxy→msd正名(--msd/--udpxy兼容)。
- 验证: 实扫201段(256IP) BUSY归0、零误报残留、可用源稳定163+。
种子重建能力(补缺口):
- 发现: 无脚本能从0重建channels表(名/分组/台标只在db里)。写 seed.py export/load。
- data/channels_seed.json(145频道+154分组)。空库载入种子 vs 基准库: 0差异,完全一致。
- 从0重建闭环: db_schema建表→seed load→scan→link_sources→etl→gen。
文档: REFACTOR_DESIGN 5.6.5实测调优结论; AGENTS.md扫描经验+Round2待办(统计增量耗时)。
待办: Round2幂等+增量耗时统计; udpxy→msd全项目正名; 失效判定/ETL/gen_m3u修复。

## 2026-07-24 孤儿源识别流程(Python侧闭环) + pipeline双模式
设计: docs/design/ORPHAN_REVIEW.md — 异步文件交换,无常驻服务(契合瞬时任务原则)。
  pipeline产出待识别包 → Electron App人工识别(独立项目,后做) → 导出resolved.json → pipeline消费写库。
实现(Python侧):
- orphan_export.py: 孤儿源→output/orphan_review/(orphans.json契约§3.1 + 截图)。含可归属频道清单+占位频道+播放URL+IINA链接。
- orphan_import.py: 读data/orphan_inbox/*.json→写库归并→归档。5种action全验证(assign/new/junk/unknown/skip)。
  new自动建频道归所属分组末尾;快照随db同目录(测试不污染正式)。
- run_pipeline.sh: 集成为7步([0]消费识别结果→扫描→清洗→ETL→[5]产出待识别包→生成)。双模式--mode known/full。
验证: 5种action造resolved.json测试全通过;空inbox优雅跳过;截图真实生成。
gitignore: output/orphan_review/ + data/orphan_inbox/ (运行时数据不上git)。
契约§3已定死,Electron App可独立开发。

## 2026-07-24 NAS1 实际部署完成 + Dashboard发布
- 上GitHub: yangwudong/iptv-radar, Actions build多架构镜像推Docker Hub(yangwudong/iptv-radar:latest),验证成功。
- NAS1(Synology,ContainerManager)部署:
  - compose用Docker Hub镜像,network_mode host(访问组播/RTSP专网),data+output挂载持久化。
  - 传现成iptv.db+种子;.env的NGINX_M3U_DIR改容器内/nginx_m3u。
  - docker需sudo /usr/local/bin/docker;scp需-O(群晖无sftp subsystem)。
  - 首次pipeline全流程跑通: 306/306零误报(644秒,与本地一致)+167单播+447归并+143优选,m3u发布。
  - 坑: output挂载空目录需先mkdir;bind mount源目录不存在会失败。
- Nginx发布(复用<PUBLISH_HOST>:<PUBLISH_PORT>):
  - m3u零改动(现有location支持任意.m3u): <PUBLISH_HOST>:<PUBLISH_PORT>/iptv.m3u。旧m3u保留不删。
  - Dashboard加发布: nginx compose加挂载iptv-radar/output/dashboard,nginx.conf tv块加/dashboard/ location,重启nginx。
  - 坑修复(治本): icons静态资源没进镜像(Dockerfile只COPY src/reference)→移到reference/icons/,gen_dashboard运行时复制到output。
- 验收全200: iptv.m3u / 旧m3u / dashboard主页 / channels.html / icons。
待办: 群晖任务计划配cron(每周known/每月full);等新镜像build完下次pipeline自带icons。

## 2026-07-24 群晖任务计划(cron)配置完成 + icons治本验证
- 两个任务计划(root身份):
  - iptv-radar weekly incremental: 每周四04:00, `docker compose run --rm pipeline --publish`(known增量)
  - iptv-radar monthly full scan: 每月第2周二03:00, 加--full(全量)
- 验证: 以root完整跑任务计划命令,端到端通(306/306零误报653s+发布), 容器--rm自动退出(瞬时任务)。
  注: synoschedtask --run 手动触发不生效(命令行方式限制),但不影响DSM调度器到点自动执行。
- icons治本生效: 新镜像(reference/icons进镜像)+gen_dashboard运行时复制→output/dashboard/icons/8个,不再手动传。
- <PUBLISH_HOST>:<PUBLISH_PORT> 全200: iptv.m3u / dashboard/ / icons。
- 部署+自动化全部完成。

## 2026-07-24 单播回看天数功能 + --timeshift-only补数据
- 回看是单播源专有能力(组播不能回看)→ 存sources.playback_days(源级,非频道级)。
- probe_timeshift.py: timeshift_url加playseek时间参数拉流,二分查找最大可回看天数,8并发。
  实测:本地169源320秒/NAS 52秒(NAS到CDN延迟低)。分布~6天33/2天57/0不支持77(版权频道)。
- pipeline: --full含回看探测(每月);新增--timeshift-only(只探测+生成页面,补数据不重扫)。
- gen_channels_page: 时移列→回看列,显示具体天数(6天回看/2天回看/✕不支持)。
- 坑: 旧库(部署前建的)无playback_days字段→probe_timeshift写库前ALTER自愈+gen_channels_page字段检测降级。
- NAS用--timeshift-only补数据成功: <PUBLISH_HOST>:<PUBLISH_PORT>/dashboard/channels.html 显示回看天数。

## 2026-07-24 Dashboard重构为Jinja2模板(数据/界面分离)
背景: gen_dashboard/gen_channels_page 原本HTML/CSS/JS全嵌Python f-string(难维护,CSS花括号要{{转义)。
重构:
- 数据/界面分离: Python(gen_*.py)只查库+备数据dict → template_util.render_template渲染。
- HTML/CSS/JS拆到 src/templates/dashboard.html + channels.html(独立模板文件,正常编辑)。
- gen_dashboard 574→281行, gen_channels 348→226行。新增template_util.py(共享Jinja2渲染)。
- 引入jinja2依赖: requirements.txt + Dockerfile pip install(打破纯标准库,但模板引擎值得)。
验证: 两页重构后内容完全一致(dashboard 11项/channels 9项指标全对),视觉不变。
NAS用新镜像生成+发布,<PUBLISH_HOST>全200。为将来改样式/CF公开版铺路。
待办(未来): CF Pages公开(学myepg,静态托管,别暴露家里服务);内网IP暴露公网DNS问题(另开session)。
