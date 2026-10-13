# 个股 K线 + 波峰波谷分析 - 架构文档

## 系统概述

一个轻量级全栈应用, 用于查看 A股/港股/美股的日/周/月 K线数据, 自动分析从年度高点之后的波峰波谷, 并集成 CC switch 当前激活的 LLM 提供一键 AI 解读。

**参考**: `../参考文件/daily_stock_analysis_v2/` (本项目独立实现, 借鉴 schema 契约)

## 架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                     Frontend (Vite + React 18)                   │
│                                                                  │
│  ┌────────────┐  ┌─────────────┐  ┌────────────┐  ┌─────────┐  │
│  │ SearchBar  │  │ KLineChart  │  │ Indicator  │  │PeakTrough│  │
│  │ (debounce) │  │   + BOLL    │  │  Charts    │  │  Panel   │  │
│  └─────┬──────┘  └──────┬──────┘  │  MACD/KDJ  │  └────┬────┘  │
│        │                │         └─────┬──────┘       │       │
│        └────────────────┴───────────────┴───────────────┘       │
│                            │                                     │
│  ┌─────────────────────────┴────────────────────────────────┐   │
│  │  Hooks: useAppStore (zustand) · useWatchlist · useTheme   │   │
│  │  Utils: markdown · exportData · watchlistIO              │   │
│  │  ErrorBoundary · Skeletons                               │   │
│  └──────────────────────────────────────────────────────────┘   │
└───────────────────────────┬──────────────────────────────────────┘
                            │  /api/v1/* (Vite proxy → :8100)
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                Backend (FastAPI + Uvicorn :8100)                │
│                                                                  │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐              │
│  │  Routers    │  │ Middlewares │  │  Schemas    │              │
│  │ stocks/     │  │   perf      │  │  Pydantic   │              │
│  │ analysis/   │  │ rate_limit  │  │   types     │              │
│  └──────┬──────┘  └─────────────┘  └─────────────┘              │
│         │                                                        │
│  ┌──────┴──────────────────────────────────────────────────┐   │
│  │              Services                                  │   │
│  │  data_provider: akshare → yfinance → mock (fallback)   │   │
│  │  peak_trough:   算法: 找年度高 → 滑动窗口 → segments   │   │
│  │  indicators:    MACD / BOLL / KDJ 计算               │   │
│  │  cache:         TTL+LRU (K线 5min, 分析 5min)         │   │
│  └────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌─────────────── LLM Layer ──────────────┐                    │
│  │  cc_switch: 读 SQLite + settings.json  │                    │
│  │  litellm:   chat(messages) → text       │                    │
│  │  rate_limit: 5/min/IP 保护上游         │                    │
│  └─────────────────────────────────────────┘                    │
└──────────────────────┬───────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                 External Services                                │
│  akshare · yfinance · CC switch SQLite · LLM upstream           │
└─────────────────────────────────────────────────────────────────┘
```

## 技术栈

### Frontend
- **构建工具**: Vite 6
- **UI 框架**: React 18 + TypeScript 5
- **样式**: Tailwind CSS 3 (深/浅主题)
- **状态管理**: zustand
- **图表**: lightweight-charts v4 (TradingView 开源)
- **HTTP**: axios

### Backend
- **框架**: FastAPI + Uvicorn
- **数据源**: akshare (A股/港股), yfinance (美股), mock 兜底
- **LLM**: LiteLLM (Anthropic/OpenAI/DeepSeek/Gemini/MiniMax 等多 provider)
- **测试**: pytest + pytest-cov
- **日志**: Python logging (INFO/WARNING 级别)

### 部署
- Docker + Docker Compose (nginx 前端 + FastAPI 后端)
- 健康检查 + 自动重启

## 数据流

### 1. K线查询流程

```
用户输入股票代码/名称
    ↓
SearchBar (300ms 防抖)
    ↓
GET /api/v1/stocks/search?q=...
    ↓
search_stocks() 搜索静态 hint 字典 (100+ 条)
    ↓
返回候选列表 → 用户选择
    ↓
GET /api/v1/stocks/{code}/history?period=daily&days=365
    ↓
fetch_history() 检查 kline_cache
    ↓ (miss)
detect_market() → _fetch_a_share/hk/us
    ↓ (akshare → yfinance → mock)
_normalize_df() → KLineData 列表
    ↓
缓存到 kline_cache (TTL 5min)
    ↓
返回前端 → KLineChart + PeakTroughPanel + IndicatorCharts
```

### 2. 波峰波谷分析

```
POST /api/v1/analysis/peak-trough
body: {stock_code, period, days, window}
    ↓
检查 analysis_cache
    ↓ (miss)
fetch_history() (复用 K线缓存)
    ↓
analyze_peaks_troughs():
  1. 取本年度数据 (按最后一日的年份)
  2. 找 year_high (max high)
  3. 从 year_high 之后开始:
     - 滑动窗口 k=3 找局部极大/极小
     - 合并 peaks + troughs
     - 过滤无效 swings (从 year_high 出发方向不一致)
     - 交替构建 segments (decline/rebound)
  4. 收尾段: 从最后极值到当前价
  5. 计算 summary (max_decline/rebound, current_vs_high, current_vs_year_start)
    ↓
缓存到 analysis_cache (TTL 5min, 与 kline_cache 共享同一 default_ttl)
    ↓
返回 PeakTroughResponse
```

> 注 (iter 138): window > 3 时会被量化到偶数 (`// 2 * 2`), 以提升缓存命中率。用户请求 `window=8` 实际使用 `window=6` 计算, 响应中通过 `effective_window` 字段告知实际值 (iter 110/121)。

### 3. LLM 解读 (限流保护)

```
POST /api/v1/analysis/llm-insight
    ↓
rate_limit.is_allowed("llm:{ip}", 5, 60s)
    ↓ (拒绝: HTTP 429 + Retry-After)
select_provider():
  - 优先 CC switch 当前激活 (MiniMax/Claude Official/Kimi 等)
  - 兜底: env ANTHROPIC_API_KEY / OPENAI_API_KEY
    ↓
fetch_history() + analyze_peaks_troughs() (复用)
    ↓
构造 system prompt + 用户消息 (segments + summary)
    ↓
litellm.completion(model="anthropic/MiniMax-M3", ...)
    ↓
渲染 LLMInsightResponse (Markdown)
```

## 搜索

`GET /api/v1/stocks/search?q=` 支持代码前缀/包含 + 中文名匹配 + 大小写不敏感。

**A股全量搜索 (iter 163)**: 内置 `CN_NAME_HINTS` 只有 ~100 条, 大量股票 (昊华科技 600378 /
天齐锂业 002466 等) 之前搜不到。现在:
- 启动时后台预加载 akshare `stock_info_a_code_name` 全量 A股列表 (~5500 只), 进程内缓存 24h
- 与内置字典合并 (内置优先, 名称更友好); akshare 不可达时降级内置
- `guess_name()` 同样用全量表, 之前显示 "(未匹配)" 的股票现在有真名
- 首次搜索走 `asyncio.to_thread`, 不阻塞事件循环

## 数据源路由

| 代码格式 | 市场 | 主数据源 | 兜底 |
|---|---|---|---|
| 6 位数字 | A股 | akshare `stock_zh_a_hist` | **tencent (腾讯直连) → yfinance → mock_fallback** |
| 5 位数字 / `hk` 前缀 | 港股 | akshare `stock_hk_hist` | **tencent (腾讯直连) → yfinance → mock_fallback** |
| `HK{N}` 或 `XXX.HK` | 港股 | 同上 | 同上 |
| 字母 | 美股 | **akshare `stock_us_daily` (新浪)** | yfinance → Finnhub → AlphaVantage → **mock_fallback** |
| `XXX.SS` / `XXX.SZ` | A股 | yfinance | **mock_fallback** |

**默认行为**: `FORCE_MOCK` 默认 `"0"` — 启动时尝试真实数据源 (akshare/yfinance), 失败按 fallback 链兜底。离线开发时设 `FORCE_MOCK=1` 直接走 mock。

**A股/港股腾讯直连 fallback (iter 162)**: 用户实测 akshare 东财接口 (EastMoney) 在部分网络
间歇性 `RemoteDisconnected`, 直接 fallback mock 让用户看到假数据。腾讯直连
(`https://web.ifzq.gtimg.cn/appstock/app/fqkline/get`) 是独立服务器, 与东财不共享
故障域。接入链: `akshare → tencent → yfinance → mock_fallback`。
- 覆盖日/周/月 (freq=day/week/month), A股 (sh/sz 前缀) + 港股 (hk 前缀)
- 响应列序注意: `[date, open, close, high, low, volume]` (open/close 在 high/low 之前)

**周期时间范围截取 (iter 165)**: `_bars_for_days(period, days)` 把日历天数换算成周期 bar 数
再截取。之前直接用 `data[-days:]` 把 days 当 bar 数 — 选 "1年" 周线会返回 365 根 (7年) /
月线 365 根 (30年), 而非 ~52 / ~12 根。美股 akshare 新浪接口返回 40 年全量历史, 最受影响;
腾讯/A股 akshare 按日期范围返回, 影响小但已统一修复。

**美股首选 akshare (iter 160)**: 用户指出"参考项目能实时拉美股" — 实测 akshare 的新浪 `stock_us_daily` 接口在 Yahoo 限流网络下依然返回真实数据 (AAPL/TSLA 等)。之前只测了 `stock_us_hist` (东财) 一次碰壁就误判 akshare 不支持美股。现在 `_fetch_us` 顺序:
1. **akshare `stock_us_daily`** (新浪, 国内服务器不受 Yahoo 限流) — 首选; 周/月用本地 `_resample_us_ohlcv` (W-FRI / ME) 重采样
2. yfinance `Ticker.history` (3 次指数退避重试)
3. Finnhub (仅日 K, 需 `FINNHUB_API_KEY`)
4. AlphaVantage (仅日 K, 需 `ALPHAVANTAGE_API_KEY`)
5. mock_fallback

### Source 标签 (iter 157-162)

`/api/v1/stocks/{code}/history` 的 `source` 字段区分七种来源, 让前端能精准提示用户:

| Source | 含义 | UI 表现 |
|---|---|---|
| `akshare` | A股/港股/美股真实数据 (东财/新浪) | 不显示 banner |
| `yfinance` | 美股真实数据 (主) | 不显示 banner |
| `tencent` | A股/港股真实数据 (腾讯直连兜底) | 不显示 banner |
| `finnhub` | 美股真实数据 (Finnhub 兜底, 需 `FINNHUB_API_KEY`) | 不显示 banner |
| `alphavantage` | 美股真实数据 (AlphaVantage 兜底, 需 `ALPHAVANTAGE_API_KEY`) | 不显示 banner |
| `mock` | 用户主动 `FORCE_MOCK=1` | 琥珀色 banner "演示数据(非实时)" |
| `mock_fallback` | 所有上游失败 (akshare + tencent + yfinance + Finnhub/AlphaVantage 均不可用) | 玫红色 banner "数据源暂时不可用, 已回退到模拟数据" + 重试按钮 |

**iter 157 bug fix**: 静默 fallback 路径返回的 `source` 标签也是 `"mock"`, 用户无法分辨"主动 mock"与"上游失败 → mock"两种场景; 而且 `_generate_mock_data` 的 2-tuple 返回值在 except 分支被错误地包成 3-tuple, 缓存里塞了不可迭代的数据。

**iter 158 升级 (借鉴 v2 daily_stock_analysis_v2)**:
1. yfinance 重试次数 1 → 3 (v2 tenacity `stop_after_attempt(3)`), 指数退避 2s/4s/8s 封顶 10s
2. 新增 Finnhub (60/min 免费) + AlphaVantage (25/day 免费) 作为独立于 Yahoo 的 fallback 链
3. 美股 fetch 链: `yfinance (3 retries) → finnhub (仅日 K) → alphavantage (仅日 K) → mock_fallback`
4. API key 缺失时 fetcher 自动跳过, 链路自然 fall through, 不破坏现有 yfinance 主路径

## 缓存策略

| 缓存 | Key 格式 | TTL | Max Size | 用途 |
|---|---|---|---|---|
| kline_cache (real) | `{code}\|{period}\|days={N}` | 5min | 256 | 同一股票同一周期内不重复拉取 |
| kline_cache (fallback) | `{code}\|{period}\|days={N}` | **30s** (iter 157) | 256 | 兜底数据短 TTL, 让用户 30s 后自动重试真实数据 |

> iter 157 之前: fallback 与 real 共享 5min TTL, 一次 429 会让用户被假数据阻塞 5 分钟。改为 30s 后下次刷新会再次尝试真实数据源, 配合 1 次瞬时错误重试, 大多数 429 会在重试后解决。
> iter 158: 重试次数升到 3 (v2 tenacity 模式), 指数退避最长 10s — 进一步提升瞬时失败下的恢复率。

**singleflight 并发去重 (iter 165)**: `_singleflight_fetch(cache_key, fetcher)` 让同一
cache_key 并发 cache miss 只执行一次 fetcher — 之前 8 个并发请求同一股票会同时调 8 次
akshare (抢数据源)。首个线程拉取, 其余等待复用 (`threading.Event`), 缓存命中后不再进
singleflight。不同股票各自拉取不受影响。

**缓存监控**: `/health` 和 `/admin/stats` 暴露 hits/misses/hit_rate

## 速率限制

| 端点 | 限制 | 触发条件 |
|---|---|---|
| `/api/v1/stocks/search` | 60 req / 60s / IP | 滑动窗口算法 (iter 135) |
| `/api/v1/stocks/{code}/indicators` | 30 req / 60s / IP | 滑动窗口算法 (iter 111) |
| `/api/v1/analysis/llm-insight` | 5 req / 60s / IP | 滑动窗口算法 |

超限返回 `HTTP 429` + `Retry-After` 头部。

## 性能监控

每个请求经过 `PerfMiddleware`:
- 记录: 方法、路径、状态码、耗时 (ms)
- 跳过: `/health`、`/docs`、`/openapi.json` (高频)
- 慢请求 (>1000ms) → WARNING 级别日志
- 响应头 `X-Response-Time-ms`
- `/admin/stats` 暴露端点统计 + 缓存统计

## 安全考量

- **CORS**: 显式 origin 白名单 (不允许 `*` + credentials)
- **SQLite**: cc_switch DB 用参数化查询 (防注入)
- **HTML 转义**: markdown 渲染器转义所有用户输入
- **LLM API Key**: 仅从环境变量 + CC switch 读取 (不入源码)
- **限流**: LLM 端点防滥用 + 计费爆仓
- **错误边界**: React 捕获 render 错误, 防止白屏

## 测试覆盖

### Backend (422 tests, ~17s, **95% 覆盖率**)

```
llm/cc_switch.py                     71%  (112 statements)
llm/litellm_backend.py               94%  (36)
middlewares/perf.py                  91%  (66)
middlewares/rate_limit.py            98%  (56)
routers/analysis.py                  74%  (80)
routers/stocks.py                    83%  (124)
security.py                          94%  (88)
services/cache.py                    100% (50)
services/data_provider.py            83%  (247)
services/indicators.py               100% (59)
services/peak_trough.py              98%  (110)

tests/test_app_startup.py            (11)
tests/test_cache.py                  (17)
tests/test_cc_switch.py              (16)
tests/test_data_provider.py          (39)
tests/test_data_provider_advanced.py (41)
tests/test_indicators.py             (17)
tests/test_litellm_backend.py        (11)
tests/test_markdown_logic.py         (helper, 0 test functions)
tests/test_peak_trough.py            (23)
tests/test_perf_middleware.py        (24)
tests/test_quote.py                  (20)
tests/test_rate_limit.py             (14)
tests/test_routers_integration.py    (35)
tests/test_schemas.py                (35)
tests/test_security.py               (62)

TOTAL                               96% (~1037 statements covered)
```

CI 门槛: `fail_under = 70` ✅

### Frontend (260 tests, ~6.5s, 22 test files)

- `src/api/client.test.ts` (13): axios mock + AbortSignal + 所有端点
- `src/App.test.tsx` (12): 顶层集成测试
- `src/components/DaysSelector.test.tsx` (7): 回看周期 + title
- `src/components/ErrorBoundary.test.tsx` (11): 错误捕获 + fallback + 重试 + 详细错误
- `src/components/IndicatorChart.test.tsx` (16): 副图 line + histogram
- `src/components/KLineChart.test.tsx` (10): 主图基础渲染 + unmount 清理
- `src/components/PeakTroughPanel.test.tsx` (21): 分析面板 + AI 解读
- `src/components/PeriodSelector.test.tsx` (7): 周期按钮 + aria-pressed
- `src/components/SearchBar.test.tsx` (10): 防抖 + 搜索结果显示 + 选中跳过重搜
- `src/components/Skeletons.test.tsx` (6): 加载骨架
- `src/components/Watchlist.test.tsx` (11): 自选股 CRUD + 导入状态反馈 + a11y
- `src/hooks/useAppStore.test.ts` (17): zustand store
- `src/hooks/useTheme.test.ts` (8): 主题切换
- `src/hooks/useWatchlist.test.ts` (10): localStorage 持久化
- `src/utils/errors.test.ts` (8): 错误消息映射
- `src/utils/exportData.test.ts` (8): CSV/JSON Blob 生成
- `src/utils/markdown.test.ts` (11): 各种 markdown 语法
- `src/utils/watchlistIO.test.ts` (13): 导入/导出 + 错误处理

CI 步骤: `npx vitest run --reporter=verbose` + type check + build

### 集成测试

- **后端**: 35 个 routers 集成测试 (TestClient + 完整 HTTP 流程)
- **限流**: 验证 llm-insight (5/min/IP, iter 98)、indicators (30/min/IP, iter 111)、search (60/min/IP, iter 135) 限制 + Retry-After 头
- **前端**: 12 个 App.tsx 集成测试 (mock 所有 API + 真实 React 渲染)

## 部署架构

```
┌──────────────────────────────────────────────────────────────┐
│                    Docker Compose                            │
│                                                              │
│  ┌────────────────┐         ┌─────────────────────────┐    │
│  │   frontend     │  proxy  │       backend            │    │
│  │   nginx        │ ──────► │   FastAPI + uvicorn     │    │
│  │   :5179        │         │   :8100                  │    │
│  │   (静态文件)   │         │                         │    │
│  └────────────────┘         └─────────────────────────┘    │
│         │                            │                       │
│         └─────────────┬────────────┘                       │
│                       │                                    │
│                  stock_net (bridge)                         │
└──────────────────────────────────────────────────────────────┘
                       │
                       ▼
       Host: ~/.cc-switch (read-only mount)
```

启动: `docker compose up --build`
停止: `docker compose down`

## CI/CD

`.github/workflows/ci.yml`:
- **backend-tests**: Python 3.11/3.12/3.13 matrix, pytest + coverage
- **frontend-build**: Node 20, type check + build
- **docker-build**: 验证镜像构建成功

## 已知限制

1. **mock 数据**: 本地无外网时返回确定性生成的 mock (基于代码 hash 种子)
2. **进程内缓存**: 多实例部署时缓存不共享 (如需可换 Redis)
3. **限流**: 单进程内存限流 (多实例时需集中式如 Redis)
4. **实时报价**: 当前用最近 K线模拟 (无 ws 推送)
5. **真实数据源依赖**: akshare/yfinance 偶尔会被限流或网络中断

## 演进方向

- [ ] WebSocket 实时行情推送
- [ ] Redis 分布式缓存
- [ ] 前端单元测试 (Vitest)
- [ ] K线图截图分享
- [ ] 价格预警 (邮件/Webhook)
- [ ] 多股票对比模式
- [ ] 自定义策略回测

## 参考

- 数据源策略借鉴自 `daily_stock_analysis_v2/data_provider/`
- API schema 对齐 `daily_stock_analysis_v2/api/v1/schemas/stocks.py`
- LiteLLM 调用模式参考 `daily_stock_analysis_v2/src/llm/litellm_backend.py`
- K线图表: [TradingView Lightweight Charts](https://tradingview.github.io/lightweight-charts/)
- CC switch: 本地 LLM provider 切换工具 (用户自定义)

## 项目指标 (138 轮迭代后)

| 指标 | 值 |
|---|---|
| 后端 Python 模块 | 13 |
| 前端组件/hooks/utils | 30+ |
| 后端测试数 | 422 (1 skipped) |
| 前端测试数 | 260 |
| **总测试数** | **682** |
| 后端覆盖率 | 95% (~1037 statements) |
| 后端响应时间 | < 10ms (mock) |
| CI 覆盖 | Python 3.11/3.12/3.13 + Node 20 + Docker |
| Docker 部署 | ✅ 一键 compose up |
| 文档 | README + ARCHITECTURE.md |

## 35 轮迭代里程碑

| 迭代 | 主题 | 测试增量 |
|---|---|---|
| 1 | CRITICAL 修复 | 0 → 8 |
| 2-5 | 搜索 + 算法 + 数据导出 | 8 → 37 |
| 6-10 | 渲染 + 缓存 + 监控 + 副图 | 37 → 66 |
| 11-15 | 限流 + Sub图表 + 前端测试 | 66 → 93 |
| 16-20 | App 测试 + 集成 + schema | 93 → 176 |
| 21-25 | 边缘案例 + PeakTrough 100% | 176 → 185 |
| 26-30 | 组件测试 + 文档 | 185 → 186 (后端) / 75 → 126 (前端) |
| 31-35 | data_provider + 组件集成 | 186 → 224 (后端) / 126 → 159 (前端) |
| 36-38 | 资源泄漏 + 鉴权 + quote + e2e bug 修复 | 224 → 345 (后端) / 159 → 196 (前端) |
| 39-47 | 文档同步 + analysis/perf/schema/indicators/peak_trough 深度覆盖 + 前端 a11y | 345 → 370 (后端) / 196 → 200 (前端) |

**累计代码量**: ~5500 行 (含 30% 测试代码)
