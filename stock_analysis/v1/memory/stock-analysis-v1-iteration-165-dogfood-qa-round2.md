---
name: stock-analysis-v1-iteration-165-dogfood-qa-round2
description: agent-browser 二次端到端 QA, 修复美股周/月线时间范围截取 bug
metadata:
  type: project
---

# iter 165 — Dogfood QA round 2 + 周/月线时间范围修复

## 做了什么
用户再次要求 agent-browser 完整端到端验证 (模拟人类操作)。手动启动 Chrome CDP
(避开 auto-launch 失败), 连 agent-browser, 系统遍历功能。

## 验证通过 (重点回归之前 iter 162-164 修复)
- ✅ 全量 A股搜索: 昊华科技 600378 (iter 163)
- ✅ 波峰波谷末尾低点: 昊华科技 回撤到 37.5 (-57.43%) (iter 164)
- ✅ AI 解读: OpenCode Go 返回真实分析 (iter 162) — "年度高点88.1元后最深下探37.5元, 回撤57.43%"
- ✅ 周K切换: 显示"加载中..." (iter 161)
- ✅ 腾讯 fallback: 周K source=tencent (iter 162)
- ✅ CSV/JSON 导出含 source
- ✅ 5年/主题/无 console 错误

## 发现并修复 BUG: 美股周/月线时间范围错误 (iter 165)
**现象**: 选"1年"周线显示 365 根 (7年数据, 首 2019-08), 应 ~52 根 (1年)。
**根因**: `_fetch_us_akshare` 用新浪 `stock_us_daily` 返回 40 年全量日线, resample 周线后
`data[-days:]` 截取 — 把 days (日历天数) 当 bar 数。days=365 → 365 根周K (7年)。
同样影响: yfinance 路径、`_fetch_tencent`。
**修复**: 新增 `_bars_for_days(period, days)` (weekly=days//7, monthly=days//30, daily=days),
统一替换 7 处 `data[-days:]`/`result[-days:]`。
**验证**: AAPL weekly days=365 → **52 根** (1年), monthly → 12 根。

## 测试
- 新增 `TestBarsForDays` (+4): daily/weekly/monthly 换算 + 小 days 不为 0
- 修 1 个既有测试 (weekly resample 测试 days 10→20)
- Backend: 485 → **490 passed**
- 浏览器 HTTP 验证: weekly days=365 → 52 bars

## 文件
- 修改: `backend/services/data_provider.py` (+`_bars_for_days`, 7 处截取替换)
- 修改: `backend/tests/test_data_provider.py` (+4, 1 修)
- 修改: `ARCHITECTURE.md`
- QA: `.omc/dogfood/screenshots/2-*.png` (5 张)

## 关键点
- 新浪 `stock_us_daily` 返回 40 年全量 → 周/月 resample 后必须按日历天数换算 bar 数截取
- A股 akshare / 腾讯按日期范围返回, 超量截取影响小, 但统一修复更安全
- agent-browser ref click 会话后期退化 → 用 JS eval 派发 .click() 兜底 (已知问题)

## 相关链接
- [[stock-analysis-v1-iteration-164-peak-trough-tail-window]] — 波峰波谷修复
- [[stock-analysis-v1-iteration-161-dogfood-qa]] — 首轮 QA
