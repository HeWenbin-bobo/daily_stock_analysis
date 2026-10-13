---
name: stock-analysis-v1-iteration-162-llm-protocol-and-tencent-fallback
description: 修复 CC switch OpenAI 兼容 provider 协议误判 (LLM 503) + A股/港股腾讯直连 fallback
metadata:
  type: project
---

# iter 162 — LLM 协议修复 + A股/港股腾讯 fallback

## 用户报告的两个问题
1. mock_fallback banner 持续出现 (akshare/yfinance 上游失败)
2. CC switch LLM "OpenCode Go" AI 解读 503, 但用户说 CC switch 现在能成功使用

## 问题 1: LLM 认证失败根因
Provider "OpenCode Go" 配置:
- base_url=`https://opencode.ai/zen/go`, **apiFormat=`openai_chat`** (OpenAI 兼容, 在 DB `meta` 列)
- env 变量名是 ANTHROPIC_* (CC switch 对 claude app 的惯例)
- model=`deepseek-v4-flash[1M]` ([1M] 是 Claude Code 上下文标记)

应用的 `_normalize_provider` **没读 meta 列**, 从 env 变量名 ANTHROPIC_* 猜协议 → anthropic。
用 Anthropic 协议调用 OpenAI 兼容端点 → 401 AuthenticationError → 503。

实测验证:
- OpenAI 协议 + 浏览器 UA → 认证通过, 但 `deepseek-v4-flash[1M]` 报 "Model not supported"
- `deepseek-v4-flash` (无 [1M]) + OpenAI 协议 + api_base 补 `/v1` → **200 成功**
- LiteLLM `api_base="https://opencode.ai/zen/go"` → 404 (拼到 /chat/completions)
- LiteLLM `api_base="https://opencode.ai/zen/go/v1"` → OK

## 修复 (3 处)
1. **`llm/cc_switch.py`**:
   - `_fetch_provider_by_id_inner` / `_query_current_provider_inner`: SELECT 加 `meta` 列, 解析 `meta_parsed`
   - `_normalize_provider`: 新增 `_api_format_prefix(provider)` 读 `meta.apiFormat` → litellm_prefix
     (优先级: explicit > **meta.apiFormat** > name > env 名 > base_url > default)
2. **`llm/litellm_backend.py`**:
   - `chat()`: `_MODEL_CONTEXT_SUFFIX_RE` 剔除模型名 `[1M]` 后缀
   - `_normalize_api_base(base_url, prefix)`: openai 前缀补 `/v1` (不重复), anthropic 不动
3. **前端 `types.ts`**: DataSource 加 `tencent`; MockBanner 测试覆盖 (tencent 是真实源不显示 banner)

## 问题 2: mock_fallback banner 根因 + 修复
用户网络下 akshare 东财接口 (EastMoney) 间歇性 `RemoteDisconnected` → A股直接 fallback mock。
参考 v2 daily_stock_analysis_v2 的 tencent_fetcher (多源 fallback 设计), 实测腾讯直连可用:
- `https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},{freq},{start},{end},{count},qfq`
- A股 sh600519/sz000001, 港股 hk00700; freq=day/week/month
- 响应 `data[symbol]["qfq{freq}"]` = `[[date, open, close, high, low, volume], ...]`
- **列序注意: open, close, high, low (high/low 在后!)**

接入: `_fetch_tencent(code, period, days, market)` helper + `SOURCE_TENCENT` 标签。
链: A股/港股 `akshare → tencent → yfinance → mock_fallback` (美股不变, tencent 不支持美股)。

## 测试
- Backend: 465 → **480 passed** (+15: 6 tencent + 3 cc_switch meta + 5 litellm api/model)
- Frontend: 272 passed
- Build: 成功

## Live verify
- `/analysis/llm-insight` 600519 → **真实 DeepSeek 分析** ("贵州茅台距年度高点 1363.35 回落 4.17%...")
- `/stocks/600519/history` → `source: tencent` (东财不可达时腾讯兜底真实数据)
- `/stocks/AAPL/history` → `source: akshare`

## 文件
- 修改: `backend/llm/cc_switch.py`, `backend/llm/litellm_backend.py`
- 修改: `backend/services/data_provider.py` (+`_fetch_tencent`, +SOURCE_TENCENT, A股/港股链)
- 修改: `backend/tests/test_cc_switch.py` (fixture 加 meta 列 + 2 测试), `test_litellm_backend.py` (+5), `test_data_provider.py` (+6), `test_data_provider_advanced.py` (5 个补丁)
- 修改: `frontend/src/types.ts`, `frontend/src/components/MockBanner.test.tsx`
- 修改: `ARCHITECTURE.md`

## How to apply
- CC switch provider 协议判断: 优先读 `meta.apiFormat`, 不要从 env 变量名猜
- OpenAI 兼容 provider: model 剔除 `[N]` 后缀, api_base 补 `/v1`
- A股/港股: akshare (东财) 不可达时用腾讯直连兜底, 实测稳定
- 腾讯响应列序是 [date, open, close, high, low, volume] — 别按 OHLC 顺序读

## 相关链接
- [[stock-analysis-v1-iteration-160-akshare-us-stocks]] — 美股 akshare 首选
- [[stock-analysis-v1-iteration-157-aapl-silent-fallback]] — mock_fallback banner
- [[stock-analysis-v1-iteration-161-dogfood-qa]] — 发现问题的 QA
