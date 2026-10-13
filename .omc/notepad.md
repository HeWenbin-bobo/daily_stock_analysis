# update_version 项目进度（2026-07-28 收尾）

## 当前状态

**应用功能**：✅ 完整可用，已通过代码审计与端到端 E2E 验证。

**dev 环境**：✅ 已关闭，无残留进程
- 后端 FastAPI（:8000）已停
- 前端 Vite dev（:5173）已停
- Agent-browser chrome 残留进程已清理（rename `~/.agent-browser/browsers` 阻断重生 + .NET `Process.Kill()` 终结）
- 端口 8000/5173/9222 均空闲

---

## 本轮变更清单（按时间序，集中在 update_version 应用）

### 1. 取消分析后保留已展示数据
- **后端** `api/v1/endpoints/hot_analysis.py`
  - `_run_analysis_background`：循环每只股票前检查 `task["status"]=='cancelled'`，是则 break
  - 抓榜完成后增加一次 cancel 检查点（避免无谓启动 LLM）
  - `cancel_analysis_task`：仅置 `status='cancelled'`，**不再 push done**（避免抢在 partial 之前关闭 SSE 监听）
  - status 扩 `cancelled/timeout/ok/failed`
  - 历史落库：cancelled/partial 也会保存到 SQLite
- **前端** `update_version/frontend/src/composables/useHotAnalysis.ts`
  - 新增 `applyAnalysisResult(data, isPartial)` 统一入口
  - `hasFinalized` 状态机防止 partial/complete/catch 重复覆盖
  - `partialResult` / `cancelledWithPartial` 状态 + 5s 兜底 `cancelFallbackTimer`（不重置已 finalize）
- **前端** `update_version/frontend/src/api/hotAnalysis.ts`
  - `createProgressStream` 区分 `partial` 与 `complete` 事件，`onComplete(_, isPartial)`
  - `HotAnalysisRunResponse.status` 加上 `'cancelled'` 字面量
- **前端** `update_version/frontend/src/App.vue`
  - 警告条 "分析已取消" 渲染条件 `cancelledWithPartial`
  - 解构新增 `partialResult` / `cancelledWithPartial`
- **回归测试** `update_version/tests/test_hot_analysis.py`
  - 新增 `TestCancelKeepsPartialResult` 两个用例：取消推 partial + 落库 cancelled；取消在第一只前不崩溃

### 2. 单股分析改并发（取消后保留数据 → 真正并发查询）
- **前端** `update_version/frontend/src/composables/useHotAnalysis.ts`
  - 新增 `SingleTask` 接口；`singleTasks` 列表；`singleConcurrency = ref<number>(3)`（可调）
  - 模块级 `singleQueue` + `singleWorkersStarted` 幂等启动 worker 池
  - `submitSingle(codes)`：去重 + push 队列 + ensureSingleWorkers
  - `cancelSingleTask(id)` / `cancelAllSingle()` / `removeSingleTask(id)` / `clearFinishedSingle()`
  - worker 用 id 重新查 live task（防止 cancelAll 后改已删 task）
  - 单股 `rounds` 用 `rounds.value`（可调）
- **前端** `update_version/frontend/src/components/SingleStockInput.vue`：完全重写
  - textarea 支持逗号/空格/换行分隔多只股票
  - 概览计数（排队/分析中/完成/失败/已取消）
  - 每只股票独立卡片：状态 tag + 取消/移除按钮 + 数据表
- **前端** `update_version/frontend/src/App.vue`：替换 prop/事件

### 3. 中文名解析 Bug
- **后端** `src/data/stock_mapping.py`
  - 加入 6 只常见工程机械/有色/铁路/化工/科技股 + 比亚迪（本地优先命中，避免依赖 AkShare 冷启动）
    - `000528 柳工 / 000338 潍柴动力 / 601899 紫金矿业 / 601006 大秦铁路 / 600961 株冶集团 / 002428 云南锗业 / 002594 比亚迪`
  - 删重复条目（紫金矿业/比亚迪/药明康德/豪威集团/洛阳钼业）

### 4. 配置持久化 + 暴露用户可调选项
- **前端** `update_version/frontend/src/utils/config.ts`
  - `AppConfig` 加 `rounds / interRoundSleep / singleConcurrency`
  - 默认值 3 / 0.6 / 3
- **前端** `update_version/frontend/src/composables/useHotAnalysis.ts`
  - `rounds = ref(3)` / `interRoundSleep = ref(0.6)` / `singleConcurrency = ref(3)`
  - `onMounted` 从 localStorage 加载
  - 硬编码 `rounds: 3, inter_round_sleep: 0.6` 全部替换为 `rounds.value / interRoundSleep.value`
- **前端** `update_version/frontend/src/components/ConfigPanel.vue`
  - 新增 "轮数 / 间隔" 选项（1-5 轮 + 0/0.3/0.6/1/2/5 秒）
  - 新增 "单股并发" 选项（1/2/3/5/8 路）
  - 新增 1800 分钟 / 300 只股票选项
  - 持久化 watch 同步

### 5. 防御性 Bug 修复
- 重复连点"确认开始分析"防抖：`onConfirmAnalysis` 加 `phase === 'analyzing'` 拦截
- partial 路径不再残留旧 "正在分析 XXX" 文案
- 切视图清掉 cancelledWithPartial 警告标记（避免离开分析页后警告条仍渲染）
- `setTimeout(5000)` 兜底 timer 改用 `cancelFallbackTimer` 句柄，避免多次取消叠加

---

## 已验证（每次变更都跑）

- ✅ `python -m py_compile api/v1/endpoints/hot_analysis.py`：0 错
- ✅ `pytest update_version/tests/`：**42 passed**
- ✅ `vue-tsc --noEmit`：0 错
- ✅ `npm run build`：成功
- ✅ 后端 schema 验证 `max_intersection=300`、`rounds=5`、`inter_round_sleep=0.3` 都 200 OK

## E2E 端到端（agent-browser 实测覆盖）

| 场景 | 结果 |
|---|---|
| 抓榜 → 确认 → 取消 → partial 保留 | ✅ |
| 6 中文名 + 限流到 5 路 → 排队 1 | ✅ |
| 重复连点"确认开始分析" → 后端只启 1 个 run-async | ✅ |
| 取消后文案无"正在分析 XXX"残留 | ✅ |
| localStorage 持久化（5轮/5路刷新保留） | ✅ |
| 历史标签多条 cancelled 记录 | ✅ |
| 7 个标签（分析/单股/历史/对比/自选/模板/定时）切换 | ✅ |

---

## 给下个会话的提示

### 重启 dev
```bash
# 一键
powershell -NoProfile -Command "Start-Process -FilePath 'cmd.exe' -ArgumentList '/c','\"F:\daily_stock_analysis\update_version\start_hot_analysis.bat\"' -WorkingDirectory 'F:\daily_stock_analysis\update_version' -WindowStyle Hidden"

# 或手动
cd F:/daily_stock_analysis && conda run -n daily_stock_analysis --no-capture-output python main.py --serve-only --port 8000 > logs/hot_analysis_e2e_backend.log 2>&1
cd F:/daily_stock_analysis/update_version/frontend && npm run dev -- --host 127.0.0.1 --port 5173 > F:/daily_stock_analysis/logs/hot_analysis_e2e_frontend.log 2>&1
```

### 关闭 dev
- 后端 / Vite：powershell `Stop-Process -Id <pid> -Force`
- agent-browser chrome：如遇 respawn，rename `~/.agent-browser/browsers` → `browsers_disabled_<ts>`，然后 .NET `Process.Kill()` 终结残留 chrome

### 待办 / 已知局限
1. LLM mock 实测（`anthropic/MiniMax-M3` 走 `127.0.0.1:15721`，网络抖动 → RemoteDisconnected 常见）— 真实生产环境接外部 LLM API 即可解决
2. `update_version/.run_state/backend.pid` marker 仅在用 `start_hot_analysis.bat` 启后端时写入；手动 conda run 不会写 marker，所以 `stop_hot_analysis.bat` 不会杀它
3. Akshare 同花顺接口 `stock_hot_ths_*d` 在当前版本被移除，已自动 fallback 到 `stock_hot_deal_xq`（雪球 deals）
4. 用户希望"1800 分钟"已有；"300 只"已有；可继续扩展"轮数/间隔/单股并发"之外的自定义（例如 LLM 温度、tool-call budget）
5. `singleConcurrency` 改变后，**已运行的 worker 池不会立即缩编/扩编**（避免在跑中重启）；新提交按新值生效（已在文档级明确）

### 关键文件锚点
- `api/v1/endpoints/hot_analysis.py` — 后端核心（1500+ 行）
- `api/v1/schemas/hot_analysis.py` — Pydantic schema
- `update_version/frontend/src/composables/useHotAnalysis.ts` — 前端核心状态机
- `update_version/frontend/src/components/ConfigPanel.vue` — 配置面板
- `update_version/frontend/src/components/SingleStockInput.vue` — 并发单股 UI
- `update_version/frontend/src/App.vue` — 主页 + 警告条
- `update_version/db/history.py` — 历史 SQLite 持久化
- `src/data/stock_mapping.py` — 股票名映射（常见名本地优先）
- `src/services/name_to_code_resolver.py` — 名称→代码解析（local → pinyin → AkShare → fuzzy）
- `update_version/BACKEND_PATCH.md` — 后端改动备忘
- `update_version/AGENTS.md` — 子项目入口
- `update_version/frontend/src/api/hotAnalysis.ts` — 前端 API client
