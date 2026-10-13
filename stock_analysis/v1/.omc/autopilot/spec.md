# v1 Autopilot Spec — AAPL Data Source Bug & Audit-driven Upgrades

**Generated**: 2026-08-04
**Iter basis**: 155 prior iterations (682 tests, 96% backend coverage)
**Mode**: autopilot (Phase 0 — Expansion)
**Investigation surface**: `F:\stock_analysis\v1\` (backend FastAPI + frontend Vite/React)

---

## 1. User-stated problem

> "检查v1应用的数据源，我在查询苹果AAPL的日线数据就发现跟实际数据对不上的。此外，检查应用是否有bug或者需要进行功能升级的内容，请你自主执行"

Translation: Inspect the v1 app's data source — querying AAPL daily K-line data doesn't match reality. Additionally, audit the app for bugs / upgrade-worthy items and execute autonomously.

## 2. Live investigation evidence (2026-08-04)

### 2.1 AAPL data path

`routers/stocks.py:326` → `services/data_provider.py:fetch_history()` → `detect_market("AAPL")` → `"us"` → `_fetch_us()` → `yf.download("AAPL", period=..., interval=...)`.

### 2.2 Verified failures

| Probe | Result |
|---|---|
| `yf.download("AAPL", period="5d")` direct call | `YFRateLimitError('Too Many Requests. Rate limited.')` — empty DataFrame |
| `yf.Ticker("AAPL").history(period="5d")` direct call | Same `YFRateLimitError` on `_fetch_ticker_tz` (shares rate-limit pool) |
| `https://query2.finance.yahoo.com/v8/finance/chart/AAPL` (curl) | `HTTP 403 Forbidden` |
| `https://query1.finance.yahoo.com/v8/finance/chart/AAPL` (curl) | `HTTP 403 Forbidden` |
| `https://stooq.com/q/d/l/?s=aapl.us&i=d` (curl) | Cloudflare JS-challenge page (no CSV) |
| `akshare` A-share `600519` (Maotai) | ✅ real data (1373 high, 1362 low on 2025-08-04) |
| `akshare` US `105.AAPL` | `ConnectionError` (akshare US endpoint down on this network) |

### 2.3 Code path: what user actually sees

```
real network OK:  AAPL → yf.download → 200 rows → normalized → cached 5min → "yfinance"   (correct)
real network OK:  AAPL → yf.download → 429    → except → mock   → cached 5min → "yfinance" (BUG: source label lies)
FORCE_MOCK=1:     AAPL → mock generator → cached 5min → "mock"   (intended fallback, source label correct)
```

The `source` field in `StockHistoryResponse` correctly distinguishes `yfinance` vs `mock` per call, but **the frontend never displays `source`** to the user (verified — no consumer of `data.source` in frontend code). So a 429-induced mock fallback is indistinguishable from real data. Cache TTL of 5 min means a transient 429 poisons the user experience for 5 minutes.

### 2.4 Why the rate-limit is structural

Yahoo's policy: `query1.finance.yahoo.com/v7/finance/download` (the endpoint `yf.download` hits) is throttled more aggressively than `query1.finance.yahoo.com/v8/finance/chart` (which `Ticker.history` uses internally for the price portion). The current code uses the high-throttle endpoint by default and has no failover, no retry, no User-Agent override, no crumb/cookie handling, and no diagnostic surfaced to the client.

### 2.5 Second-order bug: FORCE_MOCK default is `"1"` in `is_force_mock()`

`config.py:14` defaults `FORCE_MOCK=1` when the env var is missing. Combined with the absence of a `.env` file in this checkout, every fresh install is mock-only out of the box. This is intentional for offline dev, but there is **no UI indicator of mock mode** (the README claims "Mock 数据 banner" but no banner is actually rendered — verified).

### 2.6 Frontend source display — exists or not?

Confirmed by reading `frontend/src/components/KLineChart.tsx`, `App.tsx`, `api/client.ts`, `utils/exportData.ts` — `response.data.source` is propagated to the frontend but never displayed. Even the `_e2e_screenshots/qa_aapl_*.png` series from iter 3 shows the same price in every screenshot regardless of period, suggesting mock data was already the test baseline.

## 3. Other findings (audit pass)

| # | Severity | Area | Finding | Status |
|---|---|---|---|---|
| F1 | **HIGH** | data source | Silent mock fallback on yfinance 429/5xx (this task) | Open |
| F2 | **HIGH** | data source | `yf.download` is the wrong endpoint; Yahoo rate-limits it hardest | Open |
| F3 | **MEDIUM** | UX | `data.source` propagated but not rendered → user can't tell real from mock | Open |
| F4 | **MEDIUM** | DX | No visible "Mock 数据" banner — README claim is stale | Open |
| F5 | **MEDIUM** | reliability | 5-min cache on `yfinance→mock` 429 path keeps poisoning cache for 5 min | Open |
| F6 | **LOW** | reliability | No retry/backoff on transient 429/5xx — first failure always loses | Open |
| F7 | **LOW** | observability | yfinance exception is caught generically; not logged at WARNING with response code | Open (already WARNING — need to log cause) |
| F8 | **LOW** | config | `FORCE_MOCK=1` default hides network failures from the user; should be `0` when `.env` is missing | Open |
| F9 | — | coverage | 96% coverage already; 5-min cache miss/hit ratio is exposed at `/admin/stats` | OK |
| F10 | — | tests | `test_data_provider_advanced.py:test_fetch_a_share_fallback_to_yfinance` is `pytest.skip`'d with note "akshare 模块缓存导致不稳定" — coverage gap on the very fallback we're now improving | Open (untangle sys.modules) |

## 4. Out-of-scope for this iteration

- General frontend refactors (already at desired end-state per iter 144).
- yfinance library upgrade / replacement (long-term architectural decision; defer).
- akshare endpoint diversification (akshare US is broken on this network but is the user's local environment, not a code bug).
- Switching primary data provider (would require a separate spec).

## 5. Goals for the fix

1. **Correctness**: when yfinance 429s, the user MUST see that data is fake, never silently.
2. **Reliability**: transient yfinance failures should retry briefly before falling back.
3. **Observability**: real source (`yfinance` / `akshare` / `mock` / `mock_fallback`) is shown in the UI for every K-line response.
4. **Testability**: untangle the sys.modules mock so we can cover the fallback paths.
5. **No regressions**: 682/682 tests still pass; coverage stays ≥ 95%.

## 6. Design constraints (architectural)

- **No new heavy deps.** yfinance is the chosen library; switch internal call pattern only.
- **No new endpoint shapes** for the live fix — `data.source` field already exists; just expose it.
- **Cache semantics preserved** — TTL 5 min for kline_cache stays. Failure cache must NOT poison (see F5).
- **No mocks-on-by-default flip** — the env-var default `1` is intentional for offline dev; just log it at startup (already does) and add a UI banner so it's discoverable.

## 7. Proposed solution (for Phase 1 / Planning)

### 7.1 Backend (data_provider.py + stocks.py)

| Component | Change |
|---|---|
| `_fetch_us` | Switch from `yf.download` to `yf.Ticker(t).history(...)`; add 1-retry with 2 s backoff on `YFRateLimitError`; distinguish "fallback" source from "intentional mock" by returning a new `source` value: `mock_fallback`. |
| `_fetch_a_share` / `_fetch_hk` | Same retry + `mock_fallback` source on akshare failure. |
| `fetch_history` | New `source` enum semantics: `akshare` / `yfinance` / `mock` / `mock_fallback`. No signature change. |
| Cache poisoning guard | If fallback occurs, store under a `__fallback` key suffix so a real yfinance success later is not blocked by a stale 5-min mock. (`kline_cache.delete(real_key)` on every fresh real fetch — already happens because `set` overwrites. Real risk: 5-min-old mock from previous call is still returned for 5 min. → **fix: shorten fallback TTL to 30 s, OR use separate key namespace.**) |

### 7.2 Frontend (App.tsx, KLineChart, types)

| Component | Change |
|---|---|
| `App.tsx` | Read `response.data.source`; if `mock` or `mock_fallback`, show a `<MockBanner />` inline at the top with the source label and a "重新拉取" button. |
| `types.ts` | Confirm `data.source: 'akshare' \| 'yfinance' \| 'mock' \| 'mock_fallback'`. |
| `utils/exportData.ts` | Include `source` in exported CSV/JSON header. |
| New `MockBanner.tsx` | Simple accessible banner with role=alert + aria-live=polite, dismissible. |

### 7.3 Tests

- Untangle `test_data_provider_advanced.py:test_fetch_a_share_fallback_to_yfinance` (`pytest.skip` removal) so the yfinance fallback path is actually covered.
- New tests:
  - yfinance raises `YFRateLimitError` → fetch_history returns `source='mock_fallback'`
  - yfinance raises generic exception → same
  - retry succeeds on 2nd attempt → `source='yfinance'`
  - cache poisoning: after a `mock_fallback`, a later real call writes to a different key and is visible
- Frontend: `MockBanner` renders on `mock_fallback`, hides on `yfinance`.

## 8. Acceptance criteria

1. `pytest` (with `-m "not slow"`) returns 422+ tests, 0 failures, coverage ≥ 95%.
2. `npx vitest run` returns 260+ tests, 0 failures.
3. New tests cover the `mock_fallback` path on both ends.
4. Live probe (with `FORCE_MOCK=0`): the yfinance 429 path returns data with `source='mock_fallback'` AND the UI shows the banner.
5. Frontend build (`npm run build`) succeeds; bundle size delta < 5 kB.
6. ARCHITECTURE.md updated to document `mock_fallback` semantics.
7. No `print()`, no hardcoded secrets, no `any`-typed API surface, no test coverage regression.

## 9. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| yfinance `Ticker.history` is also rate-limited | High (verified) | Fallback to mock is by design; the fix is to make the fallback visible, not to make yfinance reliable |
| Cache poisoning: 5-min mock hides a later real fetch | Medium | Shorten fallback TTL to 30 s or namespace keys |
| akshare US endpoint stays down | High on this network | Out of scope (per user, focus on AAPL) |
| Frontend banner scope creep | Low | Keep banner to a single component, no theming changes |
| `test_fetch_a_share_fallback_to_yfinance` untangling is fragile | Medium | Cover with a new test in `test_data_provider.py` that uses `monkeypatch.setattr` on a module-level symbol instead of `sys.modules` |

## 10. Sequenced plan (input to Phase 1)

```
1. Add `mock_fallback` source semantics to data_provider + tests
2. Switch yfinance call to Ticker.history with 1-retry
3. Untangle the skipped test, add 4 new tests for fallback path
4. Frontend MockBanner component + App.tsx wiring
5. exportData includes source
6. Update ARCHITECTURE.md
7. Full test run + frontend build
8. Live verification: hit AAPL endpoint, confirm banner appears
```
