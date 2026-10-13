---
name: stock-analysis-v1-iteration-160-akshare-us-stocks
description: 发现 akshare 新浪/东财美股接口在 Yahoo 限流下可用, 设为美股首选数据源
metadata:
  type: project
---

# iter 160 — akshare 美股接口, 美股首选数据源

## 关键发现 (用户推动)
用户多次指出"参考文件里的应用都能实时拉美股, 你是不是看错了"。我此前只测了
`ak.stock_us_hist` (东财) 一次碰壁 (ConnectionError) 就误判 akshare 不支持美股,
一直依赖 yfinance + Finnhub/AlphaVantage。

用户坚持后重新实测, **发现 `ak.stock_us_daily` (新浪) 在 Yahoo 限流网络下完全可用**:
- `ak.stock_us_daily(symbol='AAPL', adjust='qfq')` → 10003 行真实数据 (30+ 年), 最新 2026-08-04 close 309.38
- `ak.stock_us_hist(symbol='105.AAPL', period='weekly')` → 东财也可用, 但需要交易所前缀 (105=NASDAQ)
- 走新浪/东财国内服务器, **不受 Yahoo 429 影响**

## 为什么之前没发现
1. 只测了 `stock_us_hist` 一次 (ConnectionError) 就放弃, 没试 `stock_us_daily`
2. v2 参考项目的 `akshare_fetcher.py` 故意不接美股 (Issue #311, 因复权精度), 文档注释
   写"美股用 YfinanceFetcher", 让我误以为 akshare 美股全不可用
3. 实际上**裸 akshare 的美股接口是好的**, 只是 v2 的 akshare fetcher 做了封装屏蔽

## 实施
### services/data_provider.py
- 新增 `_fetch_us_akshare(code, period, days)`: 用 `ak.stock_us_daily` 拉美股,
  周/月用 `_resample_us_ohlcv` 本地重采样 (W-FRI / ME, 最后 partial period 日期 clamp 防未来)
- 新增 `_resample_us_ohlcv(df, period)`: open=first, high=max, low=min, close=last, volume=sum
- `_fetch_us` 重构: 顺序改为 **akshare → yfinance → Finnhub → AlphaVantage → mock_fallback**
  - akshare 是首选 (iter 160)
  - 个别 akshare 覆盖不到的美股 ticker 自动 fall through 到 yfinance

### services/config.py
- `FORCE_MOCK` 默认值 "1" → **"0"** (iter 159): 启动默认尝试真实数据源, 与 v2 一致

### tests
- `TestFetchUsAkshare` (+6): akshare-US 成功路径 / days 截取 / weekly resample OHLCV /
  akshare 失败返回 None / fetch_history 走 akshare 且 yfinance 不被调用 / weekly 端到端
- 既有 10 个美股 fallback 测试全部加 `monkeypatch.setattr("services.data_provider._fetch_us_akshare", lambda *a,**k: None)` —
  akshare 现在是首选, 测 fallback 链必须跳过它

## 测试结果
- Backend: 459 → **465 passed** (+6)
- Frontend: 271 passed (无变化)
- 总计: 736 tests, 0 failures
- Live verify: AAPL daily/weekly/monthly 全部 `source: akshare` 真实数据

## 数据源优先级 (美股)
1. **akshare `stock_us_daily`** (新浪, 国内服务器, 首选)
2. yfinance `Ticker.history` (3 次指数退避重试)
3. Finnhub (仅日 K, 需 FINNHUB_API_KEY)
4. AlphaVantage (仅日 K, 需 ALPHAVANTAGE_API_KEY)
5. mock_fallback

## 文件
- 修改: `backend/services/data_provider.py`, `backend/services/config.py`
- 修改: `backend/tests/test_data_provider.py` (+6 测试 + 6 fallback 测试打补丁)
- 修改: `backend/tests/test_data_provider_advanced.py` (4 个美股测试打补丁)
- 修改: `ARCHITECTURE.md`

## How to apply
- 美股首选 akshare 新浪接口 — 这是中国用户网络下最可靠的美股数据源, 不受 Yahoo 限流
- 测 akshare 美股时用 `stock_us_daily` (新浪, 无需交易所前缀), 别只用 `stock_us_hist` (东财, 需 105./106./107. 前缀)
- 周/月从日线本地 resample (W-FRI/ME), 不需要东财的 period 参数
- 新增美股 fallback 测试时, 必须先禁用 `_fetch_us_akshare` (akshare 是首选, 否则走真网络)

## 相关链接
- [[stock-analysis-v1-iteration-158-v2-pattern-adoption]] — 上 iter, Finnhub/AlphaVantage 兜底
- [[stock-analysis-v1-iteration-157-aapl-silent-fallback]] — mock_fallback 标签引入
