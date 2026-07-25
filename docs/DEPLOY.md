# 部署文档 (DEPLOY)

> 创建: 2026-07-24
> 目标: 把 iptv-radar 部署到 NAS,群晖任务计划定时触发,瞬时执行(不常驻)。
> 状态: **待实施**。本文档为部署指南 + 所需配置文件说明。实际部署时按此照做并实测验证。

---

## 一、部署原则

- **pipeline 是瞬时任务**: 群晖任务计划触发 `docker compose run --rm pipeline` → 容器跑完即退 → 零常驻占用。
- **发布复用现有 Nginx**: 不新建 web 服务,生成的 m3u/dashboard 写入现有 Nginx 的静态目录。
- **数据持久化**: iptv.db / 种子 / output 挂载到宿主,容器重建不丢数据。
- **配置外置**: 真实凭证/地址在 `.env`(不进镜像,运行时挂载)。

---

## 二、部署环境

| 项 | 值 |
|----|----|
| 宿主 | NAS1 (Intel N100, Synology, 内存可用~3G) |
| 部署目录 | `/volume1/docker/iptv-radar/` |
| 发布 | 复用现有 Nginx: `/volume1/docker/nginx/m3u/` → `https://<发布域名>:<PUBLISH_PORT>/{iptv,iptv_direct,iptv_compat}.m3u`(三套) |
| 网络 | 同 LAN,经软路由可达组播网关(rtp2httpd/msd_lite)和电信CDN(RTSP单播/回看) |
| 触发 | 群晖 控制面板→任务计划→计划的任务(定时) |

**⚠️ 部署前必做的只读验证(在 NAS1 上)**:
```bash
# 1. 能否访问 msd_lite 组播(应返回数据)
curl -s --max-time 8 -o /dev/null -w "%{http_code} %{size_download}bytes\n" \
  "http://<ROUTER_IP>:4088/rtp/233.50.201.118:5140"
# 2. 能否访问电信CDN RTSP(路由应经软路由)
ip route get 115.233.40.137
# 3. Nginx m3u目录存在且可写
ls -ld /volume1/docker/nginx/m3u/
```

---

## 三、镜像与依赖

**基础镜像**: `python:3.12-slim`(纯Python标准库,无pip第三方依赖)
**系统依赖**: `ffmpeg`(含ffprobe,用于流探测/截图/重定向追踪)

### Dockerfile (待创建于项目根)
```dockerfile
FROM python:3.12-slim

# 流探测依赖: ffmpeg/ffprobe
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY src/ /app/src/
COPY reference/ /app/reference/
# data/ output/ 运行时挂载,不进镜像

# 时区(日志/scan_run时间戳用本地时间)
ENV TZ=Asia/Shanghai

WORKDIR /app/src
ENTRYPOINT ["bash", "run_pipeline.sh"]
```

> 注: 纯标准库,无 requirements.txt。若未来引入第三方库,加 `COPY requirements.txt` + `pip install`。

---

## 四、Docker Compose

### compose.yaml (待创建于项目根)
```yaml
services:
  pipeline:
    build: .
    image: iptv-radar:latest
    # network_mode host: 让容器直接用宿主网络,确保能路由到软路由的组播/RTSP专网
    # (桥接网络可能无法访问IPTV专网路由,部署时实测确认)
    network_mode: host
    env_file:
      - .env
    volumes:
      # 数据持久化(容器重建不丢)
      - ./data:/app/data
      - ./output:/app/output
      # 发布: 挂载现有nginx的m3u目录,pipeline --publish 时cp进去
      - /volume1/docker/nginx/m3u:/nginx_m3u
    environment:
      - TZ=Asia/Shanghai
    # 瞬时任务: 不设restart,用 `run --rm` 触发,跑完即退
```

> **发布路径说明**: compose 把 nginx 目录挂载为 `/nginx_m3u`,需在 `.env` 里设
> `NGINX_M3U_DIR=/nginx_m3u`(容器内路径),run_pipeline.sh `--publish` 会 cp 到此。

---

## 五、群晖任务计划(cron触发,瞬时)

群晖: **控制面板 → 任务计划 → 新增 → 计划的任务 → 用户定义的脚本**

### 任务1: 每周增量扫描(周一 03:00)
```bash
cd /volume1/docker/iptv-radar && \
  docker compose run --rm pipeline --publish
# (默认 known 增量模式,~11分钟)
```

### 任务2: 每月全量扫描(1号 04:00,发现新频道)
```bash
cd /volume1/docker/iptv-radar && \
  docker compose run --rm pipeline --full --publish
# (full 全量768段,~17分钟)
```

- `--rm`: 容器跑完自动删除,不留残余,零常驻 ✅
- 群晖任务计划可配"发送运行详情到邮箱",失败时通知

---

## 六、首次部署步骤

```bash
# 1. 上传项目到 NAS1
#    /volume1/docker/iptv-radar/ (src/ reference/ docs/ Dockerfile compose.yaml)

# 2. 准备 .env(从 .env.example 复制,填真实值)
cp .env.example .env && vi .env
#    MSD=<ROUTER_IP>:4088
#    NGINX_M3U_DIR=/nginx_m3u   # 容器内挂载路径
#    EPG_USERID/STB_MAC/... 等真实凭证

# 3. 准备种子数据(从0重建)或直接放已有iptv.db
#    方式A(从0重建): 需 data/channels_seed.json + data/source_links.json
mkdir -p data output
docker compose run --rm --entrypoint bash pipeline -c \
  "cd /app/src && python3 db_schema.py && python3 seed.py load"
#    方式B(用现成库): 直接把 data/iptv.db 放进去

# 4. 构建镜像
docker compose build

# 5. 网络可达性验证(见 §二)

# 6. 首次手动全量跑一遍
docker compose run --rm pipeline --full --publish

# 7. 验证产出
ls -la output/iptv.m3u output/dashboard/index.html
curl -s https://<发布域名>:<PUBLISH_PORT>/iptv.m3u | head

# 8. 配置群晖任务计划(见 §五)
```

---

## 七、目录结构(NAS1 部署后)

```
/volume1/docker/iptv-radar/
├── Dockerfile
├── compose.yaml
├── .env                      # 真实配置(不进git/镜像)
├── src/                      # 全部脚本(镜像内也有一份)
├── reference/                # channels.sample.json 等
├── data/                     # 挂载,持久化
│   ├── iptv.db               # SQLite主库
│   ├── source_links.json     # 归并快照
│   ├── channels_seed.json    # 频道种子
│   └── orphan_inbox/         # 待消费的App识别结果
└── output/                   # 挂载,产出
    ├── iptv.m3u              # → 发布时cp到nginx(另有 iptv_direct/iptv_compat 共三套)
    ├── dashboard/            # 静态HTML(index/channels)
    └── orphan_review/        # 待识别包(给Electron App)
```

---

## 八、Dashboard 发布(已实施, <PUBLISH_HOST>:<PUBLISH_PORT>/dashboard/)

m3u 通过 nginx m3u 目录发布(零改动)。Dashboard 发布步骤(已在NAS1实施):

**1. nginx compose.yaml 加挂载**(iptv-radar的dashboard目录):
```yaml
    volumes:
       # ... 原有 ...
       - /volume1/docker/iptv-radar/output/dashboard:/usr/share/nginx/html/dashboard:ro
```

**2. nginx.conf 的 tv server 块加 location**:
```nginx
    # /dashboard 无斜杠 → 重定向到 /dashboard/
    location = /dashboard {
        absolute_redirect off;    # ⚠️关键:用相对路径,否则301会拼成容器内部端口(8000)导致跳转失败
        return 301 /dashboard/;
    }
    location /dashboard/ {
        alias /usr/share/nginx/html/dashboard/;
        index index.html;
        add_header 'Access-Control-Allow-Origin' '*' always;
    }
```

**3. 重启/reload nginx**: `docker exec nginx nginx -s reload`(改conf内容用reload;改compose挂载才需 compose up -d)

**坑记录**:
- **icons静态资源没进镜像**: Dockerfile只COPY src/reference,而output是挂载空目录→图标404。
  已治本: icons移到 reference/icons/(进镜像),gen_dashboard运行时复制到 output/dashboard/icons/。
- **端口映射下301重定向拼错端口**: 容器内listen 8000,外部<PUBLISH_PORT>。`return 301`默认拼绝对URL用内部端口8000。
  修复: 加 `absolute_redirect off` 让重定向用相对路径。
- dashboard目录需先存在(pipeline首次跑生成)才能被nginx挂载。

访问: `https://<PUBLISH_HOST>:<PUBLISH_PORT>/dashboard/`(带不带斜杠都可)。旧m3u保留不删。

---

## 九、运维

- **日志**: `docker compose run` 的输出;群晖任务计划可存运行日志
- **数据备份**: 定期备份 data/iptv.db + source_links.json + channels_seed.json(核心资产)
- **回滚**: 镜像用 tag;数据有备份;pipeline 各步解耦,单步可重跑
- **孤儿源识别**: pipeline 产出 output/orphan_review/ → 同步到 Mac/Win 用 Electron App 识别
  → resolved.json 放回 data/orphan_inbox/ → 下次 pipeline 自动消费(见 ORPHAN_REVIEW.md)

---

## 十、待办 / 未决

- [ ] Dockerfile + compose.yaml 实际创建(本文档已给出模板)
- [ ] network_mode host 是否够(容器能否访问IPTV专网)——部署时实测
- [ ] 群晖任务计划实际配置
- [ ] Dashboard 是否发布 + nginx location(需确认)
- [ ] 上 GitHub 后可加 Actions 自动 build 镜像推 Docker Hub

---

## 十一、GitHub Actions 自动构建镜像

`.github/workflows/docker-build.yml` 已配置: push到main/打tag时,自动构建多架构(amd64/arm64)
镜像并推送到 Docker Hub。**Actions 只构建镜像,不跑扫描**(云端访问不了家庭组播专网)。

### 需在 GitHub repo 配置 2 个 Secret
仓库 → Settings → Secrets and variables → Actions → New repository secret:
- `DOCKERHUB_USERNAME`: 你的 Docker Hub 用户名
- `DOCKERHUB_TOKEN`: Docker Hub → Account Settings → Security → New Access Token(读写权限)

### NAS 用镜像的两种方式
**方式A(推荐,用Actions构建的镜像)**: compose.yaml 改为 pull 镜像,不本地build:
```yaml
services:
  pipeline:
    image: <dockerhub用户名>/iptv-radar:latest   # 替换 build: .
    # ... 其余不变
```
NAS 上 `docker compose pull` 拉最新镜像即可。

**方式B(NAS本地build)**: compose.yaml 保留 `build: .`,在 NAS 上 `docker compose build`。
适合 NAS 能访问代码、不想用 Docker Hub 的情况。
