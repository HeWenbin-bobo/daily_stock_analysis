---
name: stock-analysis-v1-iteration-161-dogfood-qa
description: agent-browser 端到端 QA, 修复股票切换时副标题显示旧数据 bug
metadata:
  type: project
---

# iter 161 — Dogfood QA (agent-browser 端到端) + 副标题 stale bug 修复

## 做了什么
用户要求用 agent-browser 模拟人类操作做完整端到端验证。加载 dogfood skill,
连到手动启动的 Chrome (CDP port 9222, --user-data-dir 临时目录), 遍历全部功能。

## 验证结果 (16 项功能)
- ✅ 搜索 (代码/中文名/空状态"未找到匹配")
- ✅ AAPL 真实数据 (来源 akshare), 600519 真实数据
- ✅ 日/周/月 K, 天数选择器 (90/365/2000 根)
- ✅ BOLL/MACD/KDJ 指标切换渲染
- ✅ 主题暗/亮 + localStorage
- ✅ CSV/JSON 导出 (含 source 字段)
- ✅ 自选股添加/导出
- ✅ mock_fallback banner + 重试
- ✅ AI 解读错误处理 (LLM 503 → 提示 + 重试)
- ✅ 波峰波谷分析 (真实数据)
- ✅ 2000 根 K线渲染

## BUG-1 (MEDIUM, 已修复): 副标题 stale stats
**现象**: AAPL(365根 akshare) → 切换茅台(慢加载 ~10s)时, 副标题显示旧 AAPL 的
"365 根 K线 · 来源 akshare" 但市场标签已是新茅台的 "A股"。加载完成才更新。

**根因**: `App.tsx` 副标题 `{history?.data.length} 根 K线 · 来源 {history?.source}`
在 `loadingHistory=true` 时仍读旧 `history` state (属于上一只股票)。切换股票时
`refresh()` 不清理 `history`。

**修复**: 副标题在 `loadingHistory` 时显示 "加载中...", 不显示旧 bar 数/来源。
回归测试: `App.integration.test.tsx` "subtitle stale stats during stock switch" (iter 161)。

## 非 bug 发现 (记录)
- akshare 东财接口间歇性 RemoteDisconnected → A股有时 mock_fallback (banner 正确显示)
- CC switch LLM provider "OpenCode Go" 认证失败 → AI 503 (前端正确处理)
- agent-browser 会话 download 命令后 click 命令退化 → 用 JS eval 派发 click 确认应用正常
- watchlist 导出下载中断 = agent-browser 下载拦截 artifact, 非应用 bug (blob 166 bytes 正确)

## 测试
- Backend: 465 passed
- Frontend: 271 → **272 passed** (+1 回归测试)
- Build: 成功

## 文件
- 修改: `frontend/src/App.tsx` (副标题加载中状态)
- 修改: `frontend/src/App.integration.test.tsx` (+1 回归测试)
- 新建: `.omc/dogfood/report.md` (QA 报告), `.omc/dogfood/screenshots/` (18 张)

## Why
用户原话: "使用agent browser进行一次完整的端到端验证, 所有功能必须模拟人类用户操作"。
发现并修复了股票切换时副标题显示旧数据的一致性问题。

## How to apply
- 股票切换加载中, 副标题显示 "加载中..." 而非旧股票的 bar 数/来源
- agent-browser 会话长时间运行后 click 命令可能退化, 用 `eval` 派发 `.click()` 兜底
- akshare 东财间歇性不可用是上游问题, 应用 fallback 链已正确兜底

## 相关链接
- [[stock-analysis-v1-iteration-160-akshare-us-stocks]] — akshare 美股首选
- [[stock-analysis-v1-iteration-157-aapl-silent-fallback]] — mock_fallback banner
