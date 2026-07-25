# 文档索引

iptv-radar 的全部文档,按用途分四类。

## 📡 mechanism/ — 工作原理(想理解电信IPTV怎么回事,看这里)
| 文档 | 内容 |
|------|------|
| [01_浙江IPTV工作原理](mechanism/01_浙江IPTV工作原理.md) | 分发方式(组播/单播/EPG)、各自要求、**杭州市区可看/萧山不可看的深度分析案例** |
| [02_光猫与软路由配置](mechanism/02_光猫与软路由配置.md) | 光猫桥接、**网口绑定 vs VLAN透传优缺点对比**、OpenWRT配置/注意事项/原理 |
| [03_EPG认证与频道获取](mechanism/03_EPG认证与频道获取.md) | 华为HWCTC认证算法、DES密钥爆破、频道表获取 |
| [04_msd_lite优化](mechanism/04_msd_lite优化.md) | 组播转HTTP、msd_lite配置优化、**vs udpxy对比**(网关三者对比含 rtp2httpd 见 README) |

## 📚 knowledge-base/ — 项目知识库(积累的事实与分析)
| 文档 | 内容 |
|------|------|
| [IPTV_KnowledgeBase](knowledge-base/IPTV_KnowledgeBase.md) | 家庭网络架构/拓扑/服务器清单/故障速查 |
| [SPEC](knowledge-base/SPEC.md) | 完整技术规格(网络/脚本/m3u规则) |
| [STB_Behavior_Analysis](knowledge-base/STB_Behavior_Analysis.md) | 机顶盒开机完整行为分析(抓包还原) |
| [IPTV_202x_Analysis](knowledge-base/IPTV_202x_Analysis.md) | 202.x频道不通根因分析(已整合进mechanism/01) |

## 🏗️ design/ — 设计文档(系统怎么设计的)
| 文档 | 内容 |
|------|------|
| [ARCHITECTURE](design/ARCHITECTURE.md) | **当前实现权威总览**:架构图/ER图/数据库约束/模块清单/关键机制/从0重建 |
| [CHANNEL_KEY_DESIGN](design/CHANNEL_KEY_DESIGN.md) | 数据模型:channel_id主键/channel_key/三表解耦(V2) |
| [ORPHAN_REVIEW](design/ORPHAN_REVIEW.md) | 孤儿源识别流程(异步文件交换+json契约+Electron App设计) |
| [REFACTOR_DESIGN](design/REFACTOR_DESIGN.md) | 重构前设计规划(含扫描调优实测5.6.5/5.6.6);部分过时,当前看ARCHITECTURE |
| [M3U_ACCEPTANCE_CRITERIA](design/M3U_ACCEPTANCE_CRITERIA.md) | m3u验收标准(分采集/ETL/生成三层) |

## 📋 其他
- [DEPLOY](DEPLOY.md) — NAS部署指南(Docker Compose + 群晖cron + GitHub Actions)
- [TODO](TODO.md) — 待办事项
