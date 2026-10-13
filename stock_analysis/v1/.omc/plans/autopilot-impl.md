# v1 Implementation Plan — AAPL Silent Fallback Fix + Audit Upgrades

**Generated**: 2026-08-04
**Inputs**: `.omc/autopilot/spec.md` (Phase 0 expansion)
**Mode**: autopilot (Phase 1 — Planning)
**Iteration baseline**: 155 (682 tests stable, 96% backend coverage)

---

## Goal

Make the AAPL data-discrepancy bug visible to the user (silent mock fallback on yfinance 429) and add the related reliability/observability improvements. No new heavy dependencies. Tests stay green; coverage stays ≥ 95%.

## Sequenced tasks

### Task 1 — Backend: `mock_fallback` source semantics (data_provider.py)

**Files**: `backend/services/data_provider.py`
**Scope**: Distinguish "user requested mock" from "yfinance failed → mock was the fallback". Add the string `"mock_fallback"` as a new source value; keep `"yfinance"`, `"akshare"`, `"mock"` for the existing paths.

**Concrete changes**:
- `_fetch_a_share` (line 270): the existing `except → mock` block returns `"mock"` today. Change to return `"mock_fallback"` and add a 1-attempt retry on `YFRateLimitError` / transient HTTPError before the fallback. Log at WARNING with the yfinance exception class name (currently logs only message via `inner_exc`).
- `_fetch_hk` (line 334): same change.
- `_fetch_us` (line 393): same change. Use `yf.Ticker(t).history(...)` instead of `yf.download(...)` because (a) the chart endpoint rate-limits less aggressively, (b) the `download` path is currently failing on the user's network (verified live: `YFRateLimitError`).
- Retry helper: a private `_with_yfinance_retry(fn, max_attempts=2, base_delay=2.0)` that catches `YFRateLimitError` / `requests.exceptions.HTTPError` and sleeps `base_delay * attempt`.

**Why**: a single 5xx/429 today poisons 5 minutes of cache with deterministic fake data. After this change, the user sees `source="mock_fallback"` and the UI shows a banner.

**Test coverage**: see Task 4.

### Task 2 — Backend: cache key separation (cache.py + data_provider.py)

**Files**: `backend/services/cache.py`, `backend/services/data_provider.py`
**Scope**: when the response is `mock_fallback`, store under a separate key so a real fetch 30 s later is not blocked by the stale fallback. Two options:
- (a) Short TTL for `mock_fallback` (e.g. 30 s).
- (b) Two cache namespaces (`kline_cache_real`, `kline_cache_fallback`) — heavier refactor.

**Choice**: (a) — minimal change. Add a 2-arg overload `kline_cache.set(key, value, ttl=30.0)` for the fallback path.

### Task 3 — Backend: surface `source` to logs

**Files**: `backend/services/data_provider.py`
**Scope**: change existing `logger.info("fetch_history %s/%s/%s -> %d bars from %s", ...)` to also include the cache hit/miss path so `/admin/stats` and tail-grep show fallback frequency.

### Task 4 — Backend: untangle skipped test + add new tests

**Files**: `backend/tests/test_data_provider_advanced.py`, `backend/tests/test_data_provider.py`

**Unskip**: `test_fetch_a_share_fallback_to_yfinance` (line 230). The current skip reason "akshare 模块缓存导致不稳定" is solvable by `monkeypatch.setattr(sys.modules, ...)` instead of `patch.dict("sys.modules", ...)`. Replace the test's body with a cleaner mock pattern.

**New tests** in `test_data_provider.py`:
- `test_us_yfinance_429_returns_mock_fallback` — monkeypatch yfinance `download` to raise `YFRateLimitError`; assert `source == "mock_fallback"`.
- `test_us_yfinance_generic_exception_returns_mock_fallback` — same with generic `Exception`.
- `test_us_yfinance_retry_succeeds_on_second_attempt` — first call raises, second returns valid DataFrame; assert `source == "yfinance"`.
- `test_cache_separates_fallback_from_real` — store a `mock_fallback` then a real; assert second wins.
- `test_fetch_history_returns_mock_source_when_force_mock` — assert `source == "mock"` (not `mock_fallback`) for explicit user choice.

### Task 5 — Frontend: MockBanner component

**Files**: `frontend/src/components/MockBanner.tsx` (new), `frontend/src/components/MockBanner.test.tsx` (new), `frontend/src/App.tsx`, `frontend/src/components/index.ts` or wherever components are exported.

**MockBanner**:
- Props: `source: 'akshare' | 'yfinance' | 'mock' | 'mock_fallback'`, `onRetry?: () => void`.
- Renders a fixed banner above the K-line when `source === 'mock'` or `source === 'mock_fallback'`.
- Accessible: `role="status"`, `aria-live="polite"`, dismissable via close button.
- Chinese label per project convention:
  - `mock` → "演示数据 (Mock) — 真实数据源不可用"
  - `mock_fallback` → "数据源暂时不可用,已回退到模拟数据。请稍后点击「重试」。"
- Re-uses Tailwind tokens already in the project (no new colors).

### Task 6 — Frontend: App.tsx wiring

**Files**: `frontend/src/App.tsx`, `frontend/src/types.ts` (if needed)

**Scope**: After every `fetchHistory` call, the response `source` field is already in the Pydantic schema. App.tsx already receives the typed response. Read `response.source` and pass it to `<MockBanner />`. The existing `Mock 数据 banner` line in the README was aspirational — this implements it.

### Task 7 — Frontend: exportData includes source

**Files**: `frontend/src/utils/exportData.ts`

**Scope**: Add `source` as a top-level field in exported JSON; add a `# Source: <source>` comment line in CSV header. Tests in `exportData.test.ts` cover both.

### Task 8 — Documentation: ARCHITECTURE.md

**Files**: `backend/ARCHITECTURE.md` (in repo root, or wait — actually the file is at `F:\stock_analysis\v1\ARCHITECTURE.md` at repo root).

**Scope**:
- Update the data-flow diagram to mention the fallback path.
- Update the data-source routing table to include `mock_fallback`.
- Update the cache strategy table with the 30 s fallback TTL.
- Update the known limitations list to clarify mock fallback is visible to the user.

### Task 9 — Frontend build + test

- `cd frontend && npx tsc --noEmit` — 0 errors.
- `cd frontend && npm run build` — bundle delta < 5 kB.
- `cd frontend && npx vitest run` — 260+ tests, 0 failures.

### Task 10 — Live verification

- Start backend with `FORCE_MOCK=0` to actually trigger the yfinance path.
- `curl http://127.0.0.1:8100/api/v1/stocks/AAPL/history?period=daily&days=10` — observe `source` value.
- `curl http://127.0.0.1:8100/admin/stats` — observe cache stats.
- Note: the live network has Yahoo blocked, so the user will see `source=mock_fallback`. That's the intended behavior; the banner is the user-facing fix.

## Files touched (summary)

| File | Change |
|---|---|
| `backend/services/data_provider.py` | Tasks 1, 2, 3 |
| `backend/services/cache.py` | Task 2 (TTL overload) |
| `backend/tests/test_data_provider_advanced.py` | Task 4 (unskip) |
| `backend/tests/test_data_provider.py` | Task 4 (new tests) |
| `frontend/src/components/MockBanner.tsx` | Task 5 (new) |
| `frontend/src/components/MockBanner.test.tsx` | Task 5 (new) |
| `frontend/src/App.tsx` | Task 6 |
| `frontend/src/utils/exportData.ts` | Task 7 |
| `frontend/src/utils/exportData.test.ts` | Task 7 (extend) |
| `ARCHITECTURE.md` | Task 8 |

## Sequencing rationale

Tasks 1–4 are backend-only and can be implemented as a single unit (executor). Tasks 5–7 are frontend-only and can be a second parallel unit. Tasks 8–10 are sequential. Total: 3 phases, low cross-dependency.

## Out-of-scope (deliberate)

- Replacing yfinance with another library (long-term decision).
- akshare US endpoint re-routing (separate concern; user's network issue).
- Frontend theming / dark-mode reshuffles (already stable per iter 152).
- WebSocket realtime quotes (existing roadmap item).
- Switching default `FORCE_MOCK` to `0` (intentional dev-friendly default; user can change).
