# iptv-radar Dockerfile
# 轻量 Python + ffmpeg,用于扫描/ETL/生成。瞬时任务(cron触发),非常驻。
FROM python:3.12-slim

# ffmpeg(含ffprobe) 用于流探测/截图/重定向追踪
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg tzdata && \
    rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Shanghai
WORKDIR /app

# 无第三方 Python 依赖(仅标准库),直接拷代码
COPY src/ /app/src/
COPY reference/ /app/reference/
# data/ output/ 运行时挂载(持久化),不进镜像

WORKDIR /app/src
# 默认跑一次完整流水线后退出(cron在宿主调度 docker compose run --rm)
ENTRYPOINT ["bash", "run_pipeline.sh"]
