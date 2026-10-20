# update_version — 可上线的产品级股票分析平台

把原项目从「手动触发的工具脚本」升级为「产品化的 Web 应用」。

## 与 update_version 的差异

| 维度 | update_version (v1) | update_version (v2) |
|---|---|---|
| **后端** | 复用主项目 `api/v1/endpoints/hot_analysis.py` | **独立的 FastAPI** 应用（端口 8002） |
| **数据库** | SQLite `history.db` | 新增 16 张表（异动/推送/回测/模拟盘/社区/组合/审计/llm_usage） |
| **定时任务** | 仅手动触发 | **APScheduler 持久化**（4 个内置任务 + 用户自定义） |
| **通知** | API 字段有 webhook_url 但未实现 | **4 通道推送**（企业微信/钉钉/邮件/Server酱）+ 模板 + 重试 |
| **异动监控** | 无 | **8 种规则引擎**（开盘涨幅/尾盘/连续放量/MA 突破/量比/北向资金/涨跌停） |
| **策略回测** | 无 | **LLM 翻译 + MA/MACD/RSI/量比 + 止盈止损 + 7 大指标** |
| **模拟盘** | 无 | **虚拟账户 + T+1 结算 + 排行榜 + 账户间转账** |
| **社区** | 无 | **公开榜单 + 短链分享 + 关注 + profile** |
| **数据可视化** | 表格 | **K 线 / 热力图 / 资金流向**（ECharts 按需懒加载） |
| **数据源** | akshare | **akshare 主 + baostock fallback + 健康度 + 60s 缓存** |
| **LLM** | 单源直调 | **多模型路由**（DeepSeek/Qwen/OpenAI）+ mock fallback + 用量统计 |
| **组合管理** | 无 | **持仓 + 盈亏 + 行业饼图 + 3 重风险预警** |
| **合规安全** | 无 | **Fernet 加密 + 审计日志 + 限流 + 安全头 + 强制风险提示** |
| **前端 tab** | 8 个 | 9 个（新增 📈 可视化） |

## 目录结构

```
update_version/
├── backend/
│   ├── app.py                  # FastAPI 入口（端口 8002）
│   ├── core/
│   │   ├── crypto.py           # Fernet 加密 + 脱敏
│   │   ├── audit.py            # 审计日志
│   │   ├── ratelimit.py        # 滑动窗口限流
│   │   ├── security_headers.py # 安全头 + 风险提示
│   │   ├── cron.py             # APScheduler + 持久化
│   │   ├── data_source.py      # akshare / baostock fallback
│   │   ├── llm_router.py       # deepseek/qwen/openai 路由
│   │   └── notifier.py         # 4 通道推送 + 模板 + 重试
│   ├── modules/
│   │   ├── alerts_engine.py    # 8 种异动规则
│   │   ├── backtest_engine.py  # LLM 翻译 + 指标 + 回测 + 策略对比
│   │   ├── paper_trading.py    # 模拟盘 + 排行榜 + 转账
│   │   ├── community.py        # 榜单 + 分享 + 关注
│   │   └── portfolio.py        # 持仓 + 风险预警
│   ├── api/
│   │   ├── endpoints_v2.py     # 50+ 端点（/api/v2/*）
│   │   └── endpoints_hot.py    # 30+ 端点（/api/v2/hot-analysis/* + /api/v2/market-review/*）
│   ├── db/
│   │   ├── schema.sql          # 16 张新表
│   │   └── migrate.py
│   ├── data/
│   │   └── v2_dev.db           # SQLite
│   └── tests/
│       └── test_phase*.py      # 12 个测试文件（161 个测试）
├── frontend/
│   └── src/components/
│       ├── AdvancedViz.vue     # K 线 + 热力图 + 资金流向（ECharts）
│       ├── MarketReviewView.vue
│       ├── SingleStockInput.vue
│       ├── HistoryView.vue
│       ├── CompareView.vue
│       ├── WatchlistView.vue
│       ├── TemplatesView.vue
│       ├── ScheduledTasksView.vue
│       └── ... (从 update_version 复制的所有组件)
├── services/
│   └── market_review_service.py  # 大盘复盘业务逻辑（v1 沿用）
├── db/
│   └── history.py              # SQLite CRUD（v1 沿用）
└── logs/                       # 日志目录
```

## 启动方式

### Docker（推荐，前后端同一进程）

```bash
docker compose -f ./update_version/docker-compose.yml up -d --build
```

访问 http://localhost:8002 —— 前端由后端进程直接托管，不需要另起 Vite。

- 镜像内同时包含主项目的 `src/`、`api/`、`data_provider/`、`bot/`、`strategies/`：
  `backend/api/endpoints_hot.py` 靠 `Path(__file__).parent.parent.parent.parent`
  反推仓库根来复用主项目模块，所以目录层级必须与仓库一致，不能只 COPY
  `update_version/`。其中 `bot/` 是硬依赖（`src/notification.py` 顶层 import
  `bot.models`），`strategies/` 是 agent 技能目录。
- 需要持久化的是三个部分：
  - `backend/data/`（`v2_dev.db`、`.encryption_key`、`.llm_usage_hmac_secret`、
    `llm_settings.json`）
  - `data/`（`history.db`）
  - `data/main/`、`reports/`、`logs/`：主项目模块按**仓库根**写路径，不看
    update_version 的目录约定（`DATABASE_PATH` → `/app/data`，
    `NotificationService.save_report_to_file` → `/app/reports`）。不挂载时
    组合账户/交易/用量统计/ETF 报告在容器重建后全部丢失。
  全部已挂载并被 `.dockerignore` 排除，不会进镜像层。
- 镜像额外装了 `optuna`（`endpoints_v2` 模块级 import `hyperopt_engine`）与
  `reportlab` + `fonts-wqy-zenhei`（主项目 PDF 导出的硬编码字体路径），
  三者主项目本身都不需要，因此不写进 `requirements.txt`。
- 镜像是**自检**的：`docker build` 最后会跑两段导入冒烟（主项目链 / backend
  链各一个进程，因为两边都有 `api` 包会互相遮蔽）。缺 `bot/`、缺主项目模块
  这类问题会在构建阶段失败，不用等上线后端点 500。
- 与主应用一致的两处挂载：`../strategies`（只读，改技能 YAML 不用重建镜像）
  与 `../longbridge_tokens`（OAuth token 缓存；该目录已在 `.dockerignore`
  中排除，凭据不进构建上下文）。
- 不需要 Playwright/Chromium：同花顺热度榜直接解析服务端渲染的静态 HTML
  （`data_provider.hot_lists.parse_ths_rank_html`）。若页面将来改成前端渲染，
  `fetch_ths_hot` 会自动尝试 Playwright（需自行在镜像里装 chromium），
  再回退到 akshare 历史接口。

### 后端（本地开发）

```bash
cd update_version/backend
python -m db.migrate              # 建表
python app.py                     # 端口 8002（V4_PORT 可覆盖）
```

### 前端

```bash
cd update_version/frontend
npm install                      # 首次
npm run dev                      # 端口 5174（被占用时自动 +1）
```

前端访问：http://localhost:5174

> 本地开发时后端**没有** `frontend/dist` 就会跳过静态托管，只提供 API；
> 这是预期行为，前端仍走 Vite 的 `/api` 代理。

## API 端点（共 80+）

### v2 新功能端点（`/api/v2/*`）

**通知推送**
- `GET/POST/DELETE /notifications/channels`
- `POST /notifications/test`（限流 5/min）
- `GET /notifications/logs`

**异动监控**
- `GET /alerts/rule-types`
- `GET/POST/PUT/DELETE /alerts/rules`
- `POST /alerts/scan` / `GET /alerts/triggers`
- `POST /alerts/rules/bulk-toggle` ★

**策略回测**
- `GET/POST/DELETE /backtest/strategies`
- `POST /backtest/strategies/{id}/compile`
- `POST /backtest/run`（限流 5/min）
- `GET /backtest/results`
- `POST /backtest/compare-results` ★

**模拟盘**
- `POST /paper/accounts` / `GET /paper/accounts` / `DELETE`
- `POST /paper/accounts/{id}/orders`
- `GET /paper/accounts/{id}/trades`
- `POST /paper/accounts/{id}/snapshot`
- `GET /paper/leaderboard`
- `POST /paper/transfer` ★

**社区**
- `GET /community/hot-list` / `POST /community/hot-list/refresh`
- `POST /community/shares` / `GET /community/shares/{token}`
- `POST /community/follow` / `unfollow`
- `GET /community/profile/{user_id}`

**组合管理**
- `POST/GET/DELETE /portfolio/holdings`
- `GET /portfolio/overview`
- `GET /portfolio/report`

**基础设施**
- `GET /data/health` / `GET /data/quote` / `GET /data/kline`
- `GET /llm/usage` / `POST /llm/chat`
- `GET /cron/jobs` / `POST /cron/trigger/{job_id}`
- `GET /audit/recent`

### v1 兼容端点（`/api/v1/*`，挂同一 router）

- `/api/v1/hot-analysis/*`（单股/四榜/历史/自选/模板/定时）
- `/api/v1/market-review/*`（生成/取/列表/导出/对比/批量/latest）

## 测试

```bash
cd update_version/backend
for t in tests/test_phase*.py tests/test_audit_round1.py; do
  python -X utf8 "$t"
done
```

| 测试套件 | 数量 | 状态 |
|---|---|---|
| Phase 1-12 smoke + e2e | 148 | ✅ |
| Audit round 1 新功能 | 13 | ✅ |
| **总计** | **161** | **161/161 ✅** |

## 关键技术决策

### 1. APScheduler 模块级函数引用 + 全局 listener
APScheduler 持久化要求函数是 module-level named function（不能 lambda/closure）。listener 全局挂一个捕获所有 job 状态写 cron_run_log。

### 2. Fernet 加密 webhook / 持仓成本
`encrypt_str` 加密存储到 DB，list API 返脱敏（`mask_sensitive`）。

### 3. LLM 路由自动选模型 + mock fallback
按 task_complexity 选 provider：无 API key 自动降级到 mock，保证测试不依赖外部。

### 4. 回测 DSL（LLM 编译目标）
支持指标 ma/ema/macd/rsi/volume_ratio/close + 6 个比较运算符 + 止盈止损 + 持有天数。

### 5. 风险预警三重门
单股 > 30% / 行业 > 60% / 总亏损 > -20% → 触发。

### 6. 模拟盘 T+1 结算
每日 15:30 cron 触发 `settle_daily()`，排行榜按最新快照 total_equity 降序。

### 7. v2 backend 复用 v1 endpoint 函数
通过 `sys.path.insert + 清 sys.modules['api.*'] + 普通 import` 让 v2 backend 能复用主项目的 `api.v1.endpoints.hot_analysis.analyze_single_stock` 等函数。

### 8. v1 兼容 alias
前端用 `/api/v1/hot-analysis/*`，v2 后端**同时挂 v1 + v2 两个前缀**（同一个 router），避免改前端调用路径。

## 跳过的项

> 这一节区分「不需要做」「做不了」「以后要做时怎么做」。凡是 Docker 部署相关
> 且确实需要的，都已在镜像/compose 里落实（见上一节），这里只留真正的边界。

- **用户系统 + 鉴权**：复用主项目 `api/v1/endpoints/auth.py`。当前 v2 用
  `X-User-Id` header + `_require_user_match()` 做「身份自证」，不含登录态。
  *补齐方式*：把主项目 `api.v1.endpoints.auth` 的 router 挂到 v2 app，
  `APP_ENV=production` 时强制开启；同时把 `_require_user_match` 的匿名分支
  从「允许」改为 401。
- **Playwright / Chromium**：**已不再需要**。同花顺热度榜改为直接解析服务端
  渲染的静态 HTML（三个 rank 页实测每页 50 行、GBK），`parse_ths_rank_html`
  覆盖解析，容器内实测抓到 131 只。镜像因此不需要 Chromium（省 ~400MB）。
  *若页面将来改成前端渲染*：`fetch_ths_hot` 会自动降级到 Playwright，
  届时在 Dockerfile 里加 `pip install playwright && playwright install --with-deps chromium`
  并接受镜像体积增长即可，无需改业务代码。
- **PostgreSQL 迁移**：开发/单机用 SQLite（`v2_dev.db` + `history.db`）。
  *补齐方式*：`db/migrate.py` 的 `get_conn()` 是唯一连接入口，改为返回
  SQLAlchemy engine 的 Connection 即可；注意 APScheduler 的
  `SQLAlchemyJobStore(url=...)` 需同步改 `postgresql://`。并发写场景
  （多副本）在此之前不要横向扩容。
- **Tushare Pro / eFinance 直连**：骨架已在，实际只跑 akshare 主源 +
  baostock fallback。`efinance` 已随 requirements 安装且缓存目录已修权限
  （见 Dockerfile），配 key 后可直接启用；*Tushare* 仅需在 `.env` 填
  `TUSHARE_TOKEN`，主项目 fallback 链会自动接管。
- **Webhook 实际推送**：v2 自带 4 通道推送（企业微信/钉钉/邮件/Server酱），
  模板/重试/日志已就绪，缺的只是可用的 webhook URL/key。
  *补齐方式*：`POST /api/v2/notifications/channels` 写入即可，无需改镜像。
  主项目 `src/notification.py` 的飞书/Telegram 通道未接入 v2（v2 有自己的
  `core/notifier.py`），需要时按同一模板加一个 channel 实现。
- **LLM 调用真限流**：`MAX_ANALYSIS_ATTEMPTS=6` + 5min 节流已能抗滥用；
  多用户并发时*建议*接入主项目 `src/llm` 的配额实现，而非再写一套。
- **wkhtmltopdf / 分享图**：**确认不需要**。v2 只调
  `StockAnalysisPipeline.analyze_stock()`（实测该函数 0 处通知/图片调用），
  不走 `process_single_stock()` 的推送链路；PDF 走 reportlab（已装）。
  *若将来要在 v2 里发分享图*：镜像需补 `wkhtmltopdf` + `fonts-noto-cjk`，
  并在 compose 里挂载 `reports/`（已挂）。
- **Longbridge OAuth**：镜像与 compose 已按主应用配置好
  `../longbridge_tokens:/home/dsa/.longbridge` 与 entrypoint 权限修复。
  未启用只是因为根 `.env` 里没填 `LONGBRIDGE_*`。填写后无需重建镜像。
