# iptv-radar Dockerfile
# 轻量 Python + ffmpeg,用于扫描/ETL/生成。瞬时任务(cron触发),非常驻。
FROM python:3.12-slim

# ffmpeg(含ffprobe) 用于流探测/截图/重定向追踪
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg tzdata && \
    rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Shanghai
WORKDIR /app

# Python 依赖(jinja2 模板引擎,生成dashboard/m3u页面用)
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# 代码(仅jinja2一个第三方依赖,见requirements.txt)
COPY src/ /app/src/
COPY reference/ /app/reference/
# data/ output/ 运行时挂载(持久化),不进镜像

# 非root运行: 容器用 network_mode:host 且挂载了宿主的 nginx 发布目录,
# 以 root 跑意味着任何代码执行缺陷都是 root+host网络+可写发布目录的影响面。
# UID/GID 1000 对应群晖/飞牛常见的首个普通用户;挂载目录需该用户可写。
RUN groupadd -g 1000 radar && useradd -u 1000 -g 1000 -m radar \
    && mkdir -p /app/data /app/output && chown -R radar:radar /app
USER radar

WORKDIR /app/src
# 默认跑一次完整流水线后退出(cron在宿主调度 docker compose run --rm)
ENTRYPOINT ["bash", "run_pipeline.sh"]
