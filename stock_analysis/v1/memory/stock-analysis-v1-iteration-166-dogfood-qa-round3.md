---
name: stock-analysis-v1-iteration-166-dogfood-qa-round3
description: agent-browser 三轮 QA, 修复抢 akshare (singleflight) + LLM 超时 + 标题混合
metadata:
  type: project
---

# iter 166 — Dogfood QA round 3: 抢 akshare / LLM 超时 / 标题混合

## 用户关注的三个问题
用户要求 agent-browser 端到端验证, 特别问"是否会出现抢 akshare 数据源、LLM超时等等问题"。

## 发现并修复 3 个 bug

### BUG-1 (HIGH): 抢 akshare 数据源 — 并发 cache miss 重复拉取
**现象**: 8 个并发请求同一股票 (cache miss) → akshare `stock_zh_a_hist` **被调 8 次**。
**根因**: `fetch_history` 检查 kline_cache miss 后直接拉取, 无 singleflight 去重。多个线程
同时 miss → 同时拉同一股票 → 浪费 + 触发东财限流 + 相互竞争。
**修复**: `_singleflight_fetch(cache_key, fetcher)` (data_provider.py):
- `threading.Event` + 锁: 首个线程拉取, 其余 event.wait 等待复用
- 等待者 `get` (不 pop) 读结果 — 关键: pop 会让后续等待者拿 None 误以为自己要拉取
- 新请求 `pop` 读即清; fetcher 异常时事件触发, 等待者重试
- fetch_history 的拉取部分包 singleflight
**验证**: 8 并发同一股票 → akshare 调用 **1 次** (修复前 8); 6 并发 HTTP → 1 次 fetch + 5 cache hit;
不同股票各自拉取不受影响。

### BUG-2 (MEDIUM): LLM 超时 — litellm.completion 无 timeout
**现象**: `litellm_backend.chat()` kwargs 没有 timeout, litellm 默认无限等待。上游 API 卡住时
后端 to_thread 线程一直占用 (前端 axios 30s 超时后, 后端仍在等), 线程池可能耗尽。
**修复**: `LLM_TIMEOUT_SECONDS = 60` + `kwargs["timeout"]`。
**验证**: AI 解读正常返回 (iter 162 修复), timeout 参数传递有测试断言。

### BUG-3 (LOW): 股票切换时标题混合旧名+新代码
**现象**: 切到 600519 时加载中标题显示 "Apple Inc. 600519" (旧 history.stock_name + 新代码)。
**根因**: iter 161 只修了副标题 (加载中显示 "加载中..."), 标题 `{history?.stock_name ?? selectedStock.name}`
加载中仍读旧 history。
**修复**: 标题加载中用 `selectedStock.name`, 加载完成用 history.stock_name。
**验证**: 切 TSLA 加载中标题正确显示 "Tesla TSLA"。

## 测试
- 新增 `TestSingleflight` (+3): 8 并发同 key 只调 1 次 / 不同 key 并行 / fetcher 异常等待者重试
- conftest 加 autouse fixture 清理 `_FLIGHT`/`_FLIGHT_RESULT` (防测试间泄漏)
- 新增 `test_chat_passes_timeout` (+1)
- Backend: 491 → **494 passed**; Frontend: 272 (标题修复无破坏)

## 文件
- 修改: `backend/services/data_provider.py` (+`_singleflight_fetch`, fetch_history 重构)
- 修改: `backend/llm/litellm_backend.py` (+`LLM_TIMEOUT_SECONDS`, kwargs timeout)
- 修改: `frontend/src/App.tsx` (标题加载中)
- 修改: `backend/tests/conftest.py`, `test_data_provider.py` (+3), `test_litellm_backend.py` (+1)
- 修改: `ARCHITECTURE.md` (singleflight)

## 关键点
- singleflight 等待者必须 `get` 不 `pop` — pop 会导致多等待者中后续拿 None 而重复拉取
- singleflight 模块级状态需测试 teardown 清理, 否则测试间泄漏 (单独跑通过, 全套失败)
- litellm timeout 参数是防御上游挂起的关键; 前端 axios 30s 是兜底, 后端 60s 防线程耗尽

## 相关链接
- [[stock-analysis-v1-iteration-165-dogfood-qa-round2]] — 上轮 QA
- [[stock-analysis-v1-iteration-162-llm-protocol-and-tencent-fallback]] — LLM 协议修复
