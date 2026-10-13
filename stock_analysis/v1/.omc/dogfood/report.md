# Dogfood QA Report — v1 股票分析应用 (2026-08-05)

**Target**: http://127.0.0.1:5179 (Frontend) + :8100 (Backend)
**Method**: agent-browser 完整端到端, 模拟人类用户操作 (点击/输入/切换)
**Screenshots**: `.omc/dogfood/screenshots/` (18 张)

---

## 摘要

| 严重度 | 数量 | 状态 |
|---|---|---|
| **HIGH** | 0 | — |
| **MEDIUM** | 1 | 已修复 |
| **LOW** | 1 | 已记录 (非应用 bug) |
| 上游/配置问题 | 2 | 已记录 (非应用 bug) |

总验证功能: 16 项, 14 项通过, 1 项修复, 1 项环境问题。

---

## 发现的 Bug

### ISSUE-001 (MEDIUM, 已修复) — 股票切换时副标题显示上一只股票的旧统计

**现象**: 从 AAPL (365 根 K线, 来源 akshare) 切换到贵州茅台时, 新数据加载期间 (~10s),
副标题显示 **"365 根 K线 · 来源 akshare"** (旧 AAPL 数据) 但市场标签已是 **"A股"** (新 Maotai)。
数据加载完成后才更新为正确的 "来源 mock_fallback"。

**根因**: `App.tsx` 的副标题 `{history?.data.length} 根 K线 · 来源 {history?.source}`
在 `loadingHistory=true` 时仍读取**旧的 `history` state** (属于上一只股票)。切换股票时
`refresh()` 不清理 `history`, 副标题在加载期间显示陈旧统计。

**复现**: 加载 AAPL → 搜索"茅台"并点击 → 加载期间立即读取副标题 → 显示旧 AAPL 统计。
见截图 `02-aapl-daily.png` (加载前) + `11-maotai-final.png` (加载后)。

**修复 (iter 161)**: `App.tsx` 副标题改为 `loadingHistory` 时显示 **"加载中..."**,
不显示旧 bar 数/来源。回归测试: `App.integration.test.tsx` "subtitle stale stats during stock switch"。

---

### ISSUE-002 (LOW, 非应用 bug) — 浏览器自动化会话下载中断

**现象**: 导出的 CSV/JSON 正常下载到 Downloads, 但自选股导出 (watchlist JSON) 在自动化会话中
没有生成文件。

**结论**: 非应用 bug。通过 JS 拦截验证 `URL.createObjectURL` 被调用 (blob 大小 166 bytes,
内容正确); Chrome 下载历史显示下载**已触发**但 state=4 (interrupted), 被 agent-browser 的
下载拦截保存到 UUID 临时路径。CSV/JSON 导出 state=1 (complete)。是自动化工具 (agent-browser
`download` 命令启用的下载拦截) 的 artifact, 真实浏览器中正常。

---

## 已验证功能 (16 项)

| # | 功能 | 结果 | 证据 |
|---|---|---|---|
| 1 | 首页加载, 无 console 错误 | ✅ | 截图 01 |
| 2 | 搜索 (代码/中文名, 防抖, 空状态"未找到匹配") | ✅ | 截图 06 |
| 3 | 股票加载: AAPL (美股真实数据) | ✅ 来源 akshare | 截图 02 |
| 4 | 股票加载: 600519 (A股真实数据) | ✅ 来源 akshare | 截图 18 |
| 5 | 周期切换: 日/周/月 K | ✅ | 截图 03 |
| 6 | 天数选择: 3月/6月/1年/2年/5年 | ✅ 90/365/2000 根 | 截图 16 |
| 7 | 指标: BOLL/MACD/KDJ 切换 + 渲染 | ✅ | 截图 05 |
| 8 | 主题切换: 暗/亮 + localStorage 持久化 | ✅ | 截图 09 |
| 9 | CSV 导出 (含 Source 标注) | ✅ | 贵州茅台 csv 已验证 |
| 10 | JSON 导出 (含 source + 完整分析) | ✅ | AAPL json 已验证 |
| 11 | 自选股添加/显示 | ✅ | "我的自选 (1)" |
| 12 | Mock_fallback banner + 重试按钮 | ✅ | 截图 11 |
| 13 | AI 解读错误处理 (LLM 认证失败 → 提示 + 重试) | ✅ | 截图 12 |
| 14 | 刷新按钮 | ✅ | — |
| 15 | 波峰波谷分析 (真实数据) | ✅ 回撤/回弹正确 | 截图 18 |
| 16 | 2000 根 K线 (5年) 渲染 | ✅ | 截图 16 |

---

## 上游/配置问题 (非应用 bug, 已记录)

### UPSTREAM-001 — akshare 东财接口间歇性不可达
A股/东财 `stock_zh_a_hist` 在本网络间歇性 `RemoteDisconnected`, 导致 A股数据有时
fallback 到 mock_fallback。应用正确显示玫红色 banner + 重试按钮 (iter 157 修复生效)。
日线恢复后返回真实数据 (264 根, 来源 akshare)。

### CONFIG-001 — CC switch LLM provider 认证失败
AI 解读返回 503 (`LLM_AUTH:AuthenticationError`), provider 为 "OpenCode Go (cc_switch)"。
前端正确显示 "⚠️ 服务暂不可用, 请稍后重试" + 重试按钮。需用户在 CC switch 中更新
provider 的 API key。

---

## 验证环境说明

- agent-browser 会话在 `agent-browser download` 命令后 click 命令退化 (ref 点击不触发),
  改用 JS 派发 `.click()` 验证确认**应用本身正常** (React onClick 工作正常)。
- 后端: 465 tests pass; 前端: 272 tests pass (含 BUG-1 回归测试); build 成功。
