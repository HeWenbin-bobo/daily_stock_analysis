# 后端 Dockerfile
FROM python:3.13-slim

WORKDIR /app

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 源码
COPY . .

# 端口
EXPOSE 8100

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8100/health || exit 1

# 启动
# iter 71: 以非 root 用户运行 — 容器被攻破时无法直接获得 host root
RUN groupadd --gid 1000 appuser && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash appuser
USER appuser

CMD ["python", "app.py"]
