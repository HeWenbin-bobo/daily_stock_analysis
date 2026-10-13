# update_version_v2 — 可上线的产品级股票分析平台

`update_version` 的升级版本，把原项目从「手动触发的工具脚本」升级为「产品化的 Web 应用」。

## 与 update_version 的差异

| 维度 | update_version (v1) | update_version_v2 (v2) |
|---|---|---|
| **后端** | 复用主项目 `api/v1/endpoints/hot_analysis.py` | **独立的 FastAPI** 应用（端口 8001） |
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
update_version_v2/
├── backend/
│   ├── app.py                  # FastAPI 入口（端口 8001）
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

### 后端

```bash
cd update_version_v2/backend
python -m db.migrate              # 建表
python app.py                     # 端口 8001
```

### 前端

```bash
cd update_version_v2/frontend
npm install                      # 首次
npm run dev                      # 端口 5173（被占用时自动 5174）
```

前端访问：http://localhost:5173

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
cd update_version_v2/backend
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

- 用户系统 + 鉴权：复用主项目 `api/v1/endpoints/auth.py`
- PostgreSQL 迁移：开发用 SQLite，生产部署时迁移
- Tushare Pro / eFinance：骨架已写，akshare 主源 + baostock fallback 已能跑
- Webhook 实际推送：模板/重试/日志已就绪，需 webhook URL 才推送
- LLM 调用真限流：agent round 5 已提及，MAX_ANALYSIS_ATTEMPTS=6 + 5min 节流已能抗滥用