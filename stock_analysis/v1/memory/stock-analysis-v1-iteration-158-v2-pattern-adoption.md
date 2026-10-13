---
name: stock-analysis-v1-iteration-158-v2-pattern-adoption
description: Adopt v2 daily_stock_analysis_v2 pattern: 3-attempt exponential retry + Finnhub/AlphaVantage US fallbacks
metadata:
  type: project
---

# iter 158 — Adopt v2 pattern: 3-retry + Finnhub/AlphaVantage

## 上下文
用户反馈 v1 数据源"按道理是能够使用的"(参参考 v2 应该能跑)。调查发现 v2 daily_stock_analysis_v2 用 yfinance 拉美股 + tenacity 指数退避 (stop=3, exponential 2-30s), 并提供 Finnhub/AlphaVantage/Longbridge 三个 Yahoo 备选。

## 实施

### Backend
1. **services/data_provider.py**:
   - `UPSTREAM_RETRY_MAX_ATTEMPTS` 2 → **3** (v2 tenacity 模式)
   - 重试退避 1 次 2s → 指数退避 2s/4s/8s 封顶 10s
   - 新增 `SOURCE_FINNHUB = "finnhub"` + `SOURCE_ALPHAVANTAGE = "alphavantage"` source 标签
   - `_fetch_us` 重构: yfinance (3 retries) → Finnhub (仅日 K) → AlphaVantage (仅日 K) → mock_fallback
2. **services/us_alternate_fetchers.py** (新): 借鉴 v2 finnhub_fetcher + alphavantage_fetcher 实现, 用 requests 直调 REST API (不引入第三方 SDK)
3. **.env.example**: 新增 `FINNHUB_API_KEY` + `ALPHAVANTAGE_API_KEY` 配置示例

### Frontend
4. **types.ts**: `DataSource` union 扩展 4 → 6 个 source

### Tests
5. **tests/test_data_provider.py**: +2 个测试 (3 次重试计数 + Finnhub 兜底成功)
6. **tests/test_us_alternate_fetchers.py** (新): 13 个测试覆盖 Finnhub/AlphaVantage 全部路径 (缺失 key / 非日 K / 成功 / HTTP 错误 / 限流 / 限流 Note / 字段缺失)
7. 修复: 旧 `patch.dict("sys.modules", ...)` 模式被新 `patch("requests.get", ...)` 替代 (us_alternate_fetchers 内部 import requests)

## 测试结果
- Backend: 444 → **459 passed** (+15)
- Frontend: 271 passed (无变化, 仅类型扩展)
- 总计: **730 tests, 0 failures**
- TypeScript 类型检查: 0 errors
- Live verify: yfinance 3 retries → Finnhub 401 (fake key) → AlphaVantage timeout → `source: "mock_fallback"` 40.6s

## 文件
- 修改: `backend/services/data_provider.py`, `backend/.env.example`
- 修改: `frontend/src/types.ts`
- 新建: `backend/services/us_alternate_fetchers.py`, `backend/tests/test_us_alternate_fetchers.py`
- 修改: `backend/tests/test_data_provider.py` (新增 2 测试)
- 修改: `ARCHITECTURE.md` (数据源路由 + Source 标签表扩展)

## 与 v2 差异
- v2 用 tenacity 库, v1 自实现指数退避 helper (避免新依赖)
- v2 把 fetch chain 抽象成 BaseFetcher + DataFetcherManager (策略模式), v1 在 _fetch_us 内顺序写死 (简单直接, 符合 v1 极简风格)
- v2 fetch_us 链还包括 Longbridge (OAuth 复杂), v1 只接 Finnhub + AlphaVantage (纯 API key)
- v1 的 Finnhub/AlphaVantage 仅日 K 周期 (v2 通过 resolution 参数支持 W/M), 因为 v1 主力是日 K

## Why
用户原话: "数据源不是让你参考v1中的参考文件了吗, 按道理是能够使用的"。v1 之前只用 yfinance 拉美股, 限流时直接 mock_fallback。借鉴 v2 模式能: (1) 提升瞬时失败恢复率 (3 retries vs 1); (2) 提供 Yahoo 之外的独立兜底链 (Finnhub/AlphaVantage)。

## How to apply
- 任何 yfinance 拉美股的失败场景: 重试 3 次, 2/4/8s 退避, 仍失败尝试 Finnhub (日 K 周期) → AlphaVantage (日 K 周期) → mock_fallback
- API key 通过 env var 读: `FINNHUB_API_KEY`, `ALPHAVANTAGE_API_KEY`
- 缺失 API key 时 fetcher 返回 None, 链路自然 fall through 到下一节点, 不破坏 yfinance 主路径
- Frontend 6 个 source 标签需在 MockBanner / 数据展示逻辑中正确处理 (现在 mock_fallback 显示 banner, finnhub/alphavantage 不显示)

## 相关链接
- [[stock-analysis-v1-iteration-157-aapl-silent-fallback]] — 上一 iter, 引入 mock_fallback 标签
- [[stock-analysis-v1-progress-snapshot]] — 进度快照
