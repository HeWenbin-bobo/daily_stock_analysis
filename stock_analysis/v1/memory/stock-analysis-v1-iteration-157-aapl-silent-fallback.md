---
name: stock-analysis-v1-iteration-157-aapl-silent-fallback
description: AAPL yfinance 静默 mock fallback bug 修复 + 4 个相关可靠性/可观测性升级
metadata:
  type: project
---

# iter 157 — AAPL silent fallback fix + audit upgrades

## 核心 bug
- 用户报告 AAPL 日线数据"跟实际对不上" — 根因是 `yf.download()` 命中 Yahoo 限流最严重的 `v7/finance/download` 端点, 静默 fallback 到 mock, 5min 缓存塞假数据, 用户毫无察觉。
- 同时发现的**潜在 bug**: `_generate_mock_data()` 返回 `(list, str)` 2-tuple, 但 `_fetch_*` 的 except 分支用 `return _generate_mock_data(...), SOURCE_*` 又包了一层, 实际返回 3-tuple 而非 2-tuple; 缓存里塞了 `data = (list, "mock")` 而不是 `data = list`。这是连续 156 iter 都没人发现的真 bug, 因为之前的 `data, source = _fetch_*(...)` 拆包把 (list, "mock", SOURCE_*) 当 3 元组拆 → `data` 拿到 list, 但... 实际 fetch_history 内部 `data, source = _fetch_us(...)` 是 2 元组拆包, 实际拿到的 data 是 list, source 是 SOURCE_*. 但 `return _generate_mock_data(...), SOURCE_MOCK_FALLBACK` 返回的是 `(data, source_str, MOCK_FALLBACK)` 还是 `(data, MOCK_FALLBACK)`? 取决于 Python 表达式优先级 — 实际是 `(data, MOCK_FALLBACK)` 因为三元是右侧表达式, 整条 return 是 `return (a, b)`, 看似 2-tuple 但 `a` 本身是 2-tuple 来自 `_generate_mock_data`。本轮修复: 显式 `mock_data, _ = _generate_mock_data(...)` 再 return。

## 实施内容 (8 个 tasks)

### Backend
1. **services/data_provider.py**:
   - 新增 `SOURCE_MOCK_FALLBACK = "mock_fallback"` 常量
   - 新增 `_is_transient_upstream_error()` + `_with_upstream_retry()` helper (瞬时错误 1 次重试, 2s backoff)
   - `_fetch_a_share / _fetch_hk / _fetch_us`: 用 `_with_upstream_retry` 包装 akshare + yfinance 调用; 失败时返回 `SOURCE_MOCK_FALLBACK` 而非 `SOURCE_MOCK`
   - `_fetch_us`: `yf.download` → `yf.Ticker(t).history()` (v8/finance/chart 端点限流阈值更高)
   - 修复 _generate_mock_data 2-tuple 拆包 bug
   - `fetch_history`: fallback 路径 cache TTL 30s (其余 5min)
2. **tests/test_data_provider.py**: +11 个新测试 (transient error detection, retry 行为, mock_fallback 路径, FORCE_MOCK vs fallback 区分, fallback cache TTL)
3. **tests/test_data_provider_advanced.py**: 7 个旧 fallback test 改为断言 `mock_fallback`; 1 个 mock 改用 `Ticker.history` 路径

### Frontend
4. **components/MockBanner.tsx** (新): 抽出 mock banner 为独立组件, 同时支持 `mock` (琥珀) 和 `mock_fallback` (玫红) + 重试 + 关闭按钮
5. **components/MockBanner.test.tsx** (新): 7 个测试
6. **App.tsx**: 用 `<MockBanner />` 替换内联 banner; 新增 `mockBannerDismissed` state (切股票时重置)
7. **types.ts**: `DataSource` literal union 类型; `StockHistoryResponse.source` 字段
8. **components/PeakTroughPanel.tsx**: 新增可选 `source` prop, 透传给 `exportHistoryAsCSV` / `exportAnalysisAsJSON`
9. **utils/exportData.ts**: CSV 加 `# Source:` 注释行; JSON 顶层加 `source` 字段
10. **utils/exportData.test.ts**: 4 个新测试覆盖 source 透传

### 文档
11. **ARCHITECTURE.md**: 数据源路由表更新, 新增 "Source 标签 (iter 157)" 章节 + 缓存策略表加 fallback 30s TTL

## 测试结果
- 后端: 444 → 444 passed (1 skipped, 2 deselected slow)
- 前端: 260 → 271 passed (+11)
- 总计: 715 tests, 0 failures
- 后端 data_provider.py 覆盖率: 92%
- Live verify: AAPL 实际调用 → yfinance 429 → 重试 1 次 → 仍失败 → `source: "mock_fallback"` 返回, 前端 banner 可见

## 文件
- 修改: `backend/services/data_provider.py`, `backend/tests/test_data_provider.py`, `backend/tests/test_data_provider_advanced.py`
- 修改: `frontend/src/App.tsx`, `frontend/src/types.ts`, `frontend/src/components/PeakTroughPanel.tsx`, `frontend/src/utils/exportData.ts`, `frontend/src/utils/exportData.test.ts`
- 新建: `frontend/src/components/MockBanner.tsx`, `frontend/src/components/MockBanner.test.tsx`
- 修改: `ARCHITECTURE.md`
- spec/plan: `.omc/autopilot/spec.md`, `.omc/plans/autopilot-impl.md`

## Why
用户原话: "查询苹果 AAPL 的日线数据就发现跟实际数据对不上"。根因不是数据源本身错, 而是静默 fallback 隐藏了真实失败状态。

## How to apply
- 任何 yfinance 失败场景 (429 / 连接超时 / 5xx) 都会返回 `source: "mock_fallback"`, 前端必须用 `<MockBanner />` 展示给用户, 不能静默
- fallback 路径 cache TTL 是 30s, 让用户 30s 后自动重试真实数据
- `_generate_mock_data` 返回 `(list, str)` 2-tuple, 调用方必须显式 `mock_data, _ = ...` 解包, 不要直接 return
- yfinance 美股用 `yf.Ticker(t).history()` 路径, 不要用 `yf.download()` (后者限流最严重)
- 增加 mock_fallback 这个 source 后, 任何 "assert source == 'mock'" 的测试都需审视 — 是 FORCE_MOCK 路径还是 fallback 路径

## 相关链接
- [[stock-analysis-v1-progress-snapshot]] — 进度快照
- [[stock-analysis-v1-iteration-149-dead-code-audit]] — 上一稳定性验证
- [[stock-analysis-v1-iteration-156-test-fixture-audit]] — 上一测试质量审计
