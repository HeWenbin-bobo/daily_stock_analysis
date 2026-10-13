# Dogfood QA Report Round 2 — v1 股票分析应用 (2026-08-06)

**Target**: http://127.0.0.1:5179 (Frontend) + :8100 (Backend)
**Method**: agent-browser 端到端, 模拟人类操作 (点击/搜索/切换)
**Screenshots**: `.omc/dogfood/screenshots/2-*.png` (5 张)

---

## 摘要

| 严重度 | 数量 | 状态 |
|---|---|---|
| **MEDIUM** | 1 | 已修复 |
| 回归验证 | 10 项 | 全部通过 |

---

## 发现的 Bug

### ISSUE-003 (MEDIUM, 已修复) — 美股周/月线时间范围错误

**现象**: 选"1年"周K显示 **365 根**(7年数据, 首根 2019-08), 应 ~52 根(1年)。
日线正确(365 根 ≈ 1年), 周/月线错误。

**根因**: `_fetch_us_akshare` 用 akshare 新浪 `stock_us_daily` 返回 **40 年全量日线**,
resample 成周/月线后执行 `data[-days:]` — **把 days(日历天数)当 bar 数截取**。
- weekly: days=365 → 365 根周K = 7年(应 ~52 根)
- monthly: days=365 → 365 根月K = 30年(应 ~12 根)

同样影响 `_fetch_us` yfinance 路径 和 `_fetch_tencent`。A股 akshare 主路径按日期范围返回,
影响小。

**复现**: 浏览器选 AAPL + 周K + 1年 → 副标题 "365 根 K线", 图表显示 7 年历史。
后端验证: `fetch_history('AAPL','weekly',365)` → 365 bars。

**修复 (iter 165)**: 新增 `_bars_for_days(period, days)`:
- daily → days; weekly → max(1, days//7); monthly → max(1, days//30)
- 统一替换 data_provider 7 处 `data[-days:]` / `result[-days:]`

**验证**:
- `fetch_history('AAPL','weekly',365)` → **52 bars** (首 2025-08-15 末 2026-08-05)
- `fetch_history('AAPL','monthly',365)` → **12 bars**
- 浏览器: AAPL 周K → "52 根 K线 · 来源 akshare" ✓

---

## 回归验证 (10 项, 全通过)

| # | 功能 | 结果 | 说明 |
|---|---|---|---|
| 1 | 全量 A股搜索 | ✅ | 昊华科技→600378 (iter 163) |
| 2 | 股票加载 | ✅ | 昊华科技 akshare, AAPL akshare |
| 3 | 波峰波谷末尾低点 | ✅ | 回撤 88.1→37.5 (iter 164) |
| 4 | AI 解读 | ✅ | OpenCode Go 真实分析 (iter 162) |
| 5 | 周K切换加载中 | ✅ | "加载中..." (iter 161) |
| 6 | 腾讯 fallback | ✅ | 周K source=tencent (iter 162) |
| 7 | CSV 导出 | ✅ | # Source: tencent, 65 行 |
| 8 | JSON 导出 | ✅ | source+2000 bars |
| 9 | 5年选择器 | ✅ | 2000 根 |
| 10 | 主题切换/console | ✅ | 无错误 |

---

## 测试

- Backend: **490 passed** (新增 TestBarsForDays +4, 修 1)
- 无前端改动 (272 不变)
