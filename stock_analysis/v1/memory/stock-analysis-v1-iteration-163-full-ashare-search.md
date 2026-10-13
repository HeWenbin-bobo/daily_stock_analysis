---
name: stock-analysis-v1-iteration-163-full-ashare-search
description: 修复大量 A股搜不到 — akshare 全量股票列表集成 + 启动预加载 + to_thread
metadata:
  type: project
---

# iter 163 — A股全量搜索 (昊华科技/天齐锂业等)

## 用户报告的问题
"有大量的股票搜索不到, 比如昊华科技、天齐锂业等等"

## 根因
`search_stocks()` 用静态内置 `CN_NAME_HINTS` 字典 (只有 ~100 条 A股), 而 A股有 5000+ 只。
昊华科技 (600378)、天齐锂业 (002466) 不在内置 → 搜不到, `guess_name` 也显示 "(未匹配)"。

## 修复
### services/data_provider.py
- `_get_cn_name_map()`: 全量 A股 {code: name} 映射, 内置 + akshare `stock_info_a_code_name` (~5500 只) 合并
  - 进程内缓存 24h (TTL + threading.Lock 防并发重复加载)
  - **尊重 FORCE_MOCK**: FORCE_MOCK=1 时只用内置 (测试/离线不拉网络)
  - 屏蔽 akshare 内部 tqdm 进度条 (污染日志)
  - 失败降级内置 (搜索仍可用)
- `search_stocks()`: A股循环用 `_get_cn_name_map()` (之前用 CN_NAME_HINTS)
- `guess_name()`: cn 分支用 `_get_cn_name_map()` (之前 "(未匹配)" 的股票现在有真名)

### routers/stocks.py
- `/search` 端点: `search_stocks` 改用 `asyncio.to_thread` — 首次搜索会触发全量列表懒加载 (3-5s), 直接在 async handler 调用会阻塞事件循环

### app.py
- 用 lifespan (替代已弃用的 `@app.on_event`) — 启动时后台 daemon 线程预加载全量列表, 首次搜索立即完成

## 测试
- `TestCnNameMap` (+5): FORCE_MOCK=1 只用内置 / akshare 全量合并 / akshare 失败降级 / 搜索全量股票 / guess_name 全量名
- 5 个 TestDoubleFailureFallback A股测试补丁 `_get_cn_name_map` (避免真实网络拉取)
- Backend: 465 → **485 passed**
- Frontend: 272 (未改)

## Live verify
- `GET /search?q=昊华科技` → `[600378 昊华科技]`
- `GET /search?q=002466` → `[002466 天齐锂业]`
- `GET /stocks/600378/history` → source akshare, name 昊华科技, close 46.34
- `GET /stocks/002466/history` → source akshare, name 天齐锂业, close 47.52

## 关键点
- akshare `stock_info_a_code_name` 自带 `@lru_cache`, 但 tqdm 进度条需屏蔽 (monkeypatch tqdm.__init__ disable=True)
- FORCE_MOCK=1 时全量拉取跳过 — 避免测试/离线场景拉网络
- 启动预加载用 daemon 线程 fire-and-forget, lifespan 替代 on_event (0.110+ deprecation)

## 文件
- 修改: `backend/services/data_provider.py`, `backend/routers/stocks.py`, `backend/app.py`
- 修改: `backend/tests/test_data_provider.py` (+5), `test_data_provider_advanced.py` (5 补丁)
- 修改: `ARCHITECTURE.md`

## 相关链接
- [[stock-analysis-v1-iteration-162-llm-protocol-and-tencent-fallback]] — 上 iter, 数据源 fallback
