# syntax=docker/dockerfile:1.7

# ===================================
# update_version (v2) - Docker 镜像
# ===================================
# 多阶段构建：Vue 前端打包 + FastAPI 后端运行
#
# 与主镜像（docker/Dockerfile）的关键差异：
# - 后端不是 main.py，而是 update_version/backend/app.py
# - 必须同时带主项目的 src/ api/ data_provider/：endpoints_hot.py 通过
#   _MAIN_ROOT = Path(__file__).parent.parent.parent.parent 反推仓库根，
#   再 import api.v1.* / src.* / data_provider.*，缺一不可
# - 目录层级必须保持与仓库一致（/app/update_version/backend/...），
#   否则上面的路径反推会指向错误位置

FROM node:20-slim AS web-builder

WORKDIR /app/update_version/frontend

COPY update_version/frontend/package.json update_version/frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci

COPY update_version/frontend/ ./
RUN npm run build

FROM python:3.11-slim-bookworm

WORKDIR /app

ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# gosu: entrypoint 降权；curl: 健康检查兜底调试。
# 不装 wkhtmltopdf / fonts-noto-cjk —— update_version 不生成分享图。
# fonts-wqy-zenhei：主项目 PDF 生成按硬编码路径探测
# /usr/share/fonts/truetype/wqy/wqy-zenhei.ttc；缺字体时报告里的中文会渲染成
# 空白方块。该 deb 只有 748KB，比 fonts-noto-cjk 轻得多。
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gosu \
    fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 1000 dsa && \
    useradd -u 1000 -g dsa -m dsa

COPY requirements.txt .

# optuna 只被 update_version 的 modules/hyperopt_engine.py 使用，主项目不需要它，
# 因此不写进 requirements.txt（会把参数调优的重依赖强加给主镜像）。
# 但它由 endpoints_v2 在模块级 import 拉起，缺了应用直接起不来。
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt && \
    pip install --no-cache-dir "optuna>=3.0,<5" "reportlab>=4,<6"

# ---- 主项目模块（update_version 复用）----
# bot/ 是硬依赖而非可选：src/notification.py 顶层 `from bot.models import
# BotMessage`，而 src.core.market_review → src.notification 是分析/复盘链路的
# 必经 import。缺 bot/ 的表现不是"机器人功能不可用"，而是 /hot-analysis/preview、
# /run-async、/pdf-report 直接 500（No module named 'bot'），ETF 轮动报告也
# 保存失败。实测过：仅 COPY src/api/data_provider 时这些端点全挂。
COPY src/ ./src/
COPY api/ ./api/
COPY data_provider/ ./data_provider/
COPY bot/ ./bot/
# 交易技能 YAML：src/agent/skills 默认从 <仓库根>/strategies 加载，
# 缺失时只打一条 warning 并静默跳过全部技能分析（分析质量下降且无报错）。
COPY strategies/ ./strategies/

# ---- update_version 本体 ----
# 层级必须与仓库一致：backend/api/endpoints_hot.py 靠 4 层 parent 反推仓库根。
COPY update_version/backend/ ./update_version/backend/
COPY update_version/db/ ./update_version/db/
COPY update_version/services/ ./update_version/services/
COPY update_version/__init__.py ./update_version/
COPY --from=web-builder /app/update_version/frontend/dist ./update_version/frontend/dist

# efinance 把搜索缓存写进自身安装包的 data 目录（默认 root 属主），容器以 dsa
# 运行时每次都 PermissionError，而它是主项目 fallback 链里的优先数据源。
# 主镜像 docker/Dockerfile 有同样的修复，这里漏掉会让 v2 每条行情都先失败一次。
RUN EF_DATA_DIR="$(python -c 'import efinance, pathlib; print(pathlib.Path(efinance.__file__).parent / "data")')" && \
    mkdir -p "$EF_DATA_DIR" && \
    chown -R dsa:dsa "$EF_DATA_DIR"

COPY docker/entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY docker/healthcheck.py /usr/local/bin/docker-healthcheck.py

# 运行期可写目录：SQLite 库、加密密钥、LLM 设置、history.db 都在 data/ 下。
# DSA_WRITABLE_DIRS 让共享 entrypoint 知道本镜像要修哪些路径。
# /app/{data,logs,reports} 是主项目模块（DatabaseManager / PortfolioService /
# llm usage / NotificationService）按仓库根写的路径——它们不看 update_version
# 的目录约定，必须在镜像里预建并授权，否则非 root 用户运行时报只写失败。
RUN mkdir -p /app/update_version/backend/data /app/update_version/data \
             /app/data /app/logs /app/reports && \
    chown -R dsa:dsa /app && \
    chmod +x /usr/local/bin/docker-entrypoint.sh

# 构建期导入自检（对齐主镜像 CI 的 "Docker smoke imports"）：
# 缺 bot/ 时 endpoints_hot 的整条主项目复用链会 ImportError，症状是运行时
# /preview、/run-async、PDF 导出 500 —— 在这里失败比上线后才发现便宜得多。
#
# 必须分成两个进程：主项目 `api` 包与 backend `api` 包同名，同一进程里
# 先 import 谁就会遮住另一个（这也是 endpoints_hot 运行时要整套
# sys.path/sys.modules 手术的原因）。脚本写进临时文件而不是 `python -c`，
# 避免 Docker 合并续行破坏缩进。
#
# ★ 探索期修复(镜像被构建期副作用污染)：`api/endpoints_hot.py` 在**模块级**
#   就 `_import_from_path(... db/history.py)`，而 history.py 导入时会建库建表。
#   自检以 root 跑，于是镜像层里被写进一个 root 属主的
#   `/app/update_version/data/history.db`（实测 73KB、时间戳=构建时刻）。
#   该目录本该是 bind mount 的挂载点、镜像内应为空；固化一个空库进去既让镜像
#   不可复现，也让首次启动的"建库"语义变得不明确。把 V4_HISTORY_PATH 指到
#   /tmp 让自检的副作用落在临时层，然后删掉。
RUN printf '%s\n' \
    'from api.v1.endpoints.hot_analysis import preview_hot_analysis' \
    'from src.notification import NotificationService' \
    'from src.services.portfolio_service import PortfolioService' \
    'from src.services.etf_rotation_service import run_etf_rotation' \
    'from bot.models import BotMessage' \
    'from data_provider.hot_lists import fetch_all_four, fetch_ths_hot_static' \
    > /tmp/smoke_main.py && \
    printf '%s\n' \
    'from api.endpoints_v2 import router' \
    'from api.endpoints_hot import _load_main_hot_analysis' \
    'import app' \
    > /tmp/smoke_backend.py && \
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app \
      python -B /tmp/smoke_main.py && \
    cd /app/update_version/backend && \
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/update_version/backend \
      V4_HISTORY_PATH=/tmp/smoke_history.db \
      V4_DB_PATH=/tmp/smoke_v2.db \
      python -B /tmp/smoke_backend.py && \
    rm -f /tmp/smoke_main.py /tmp/smoke_backend.py \
          /tmp/smoke_history.db /tmp/smoke_v2.db && \
    rm -rf /app/update_version/data/*.db /app/update_version/backend/data/*.db

ENV PYTHONUNBUFFERED=1
# 主项目模块的 DATABASE_PATH 由 compose 指到持久卷（见 docker-compose.yml）；
# 这里列出的可写目录用于 bind mount 权限修复。
# /home/dsa/.longbridge 必须列出来：设置 DSA_WRITABLE_DIRS 会**覆盖** entrypoint
# 的内置默认值（默认值里含该路径），漏掉时宿主挂进来的 token 缓存属主仍是
# Windows/root，dsa 用户读不了 → Longbridge OAuth 静默失效。
ENV DSA_WRITABLE_DIRS="/app/update_version/backend/data /app/update_version/data /app/data /app/logs /app/reports /home/dsa/.longbridge"
# 健康检查走 /health（本应用没有 /api/health）
ENV DSA_HEALTH_PATH=/health
ENV V4_HOST=0.0.0.0
ENV V4_PORT=8002
# ★ 探索期修复(健康检查误判)：健康检查端口必须跟随应用实际监听的 V4_PORT。
#   此前这里写死 `ENV DSA_HEALTH_PORT=8002`，而 app.py 读的是 `V4_PORT` ——
#   用户用 `-e V4_PORT=9000` 换端口时，服务在 9000 上正常响应（实测 /health 200），
#   探针却一直打 8002，重试 3 次后容器被判 unhealthy。在有重启策略/编排器的环境里，
#   一个完全健康的容器会被反复杀掉重建。
#   用 `${V4_PORT}` 让两者只有一个真相来源；healthcheck.py 的 candidate_ports()
#   已经在读 DSA_HEALTH_PORT，所以这里只需保证它不再是独立常量。
ENV DSA_HEALTH_PORT=${V4_PORT}
# 注意：APP_ENV / APP_CORS_ORIGINS 故意不写死。前端由本进程同源托管时不需要
# CORS；暴露给其它来源时由 compose 或 .env 显式设置（APP_ENV=production 会强制
# 要求白名单，缺省则拒绝启动，这是应用自身的护栏）。

# EXPOSE 只是文档性声明，不支持变量插值；换端口时以 V4_PORT 为准（健康检查已跟随）。
EXPOSE 8002

VOLUME ["/app/update_version/backend/data", "/app/update_version/data"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "/usr/local/bin/docker-healthcheck.py"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

CMD ["python", "update_version/backend/app.py"]
