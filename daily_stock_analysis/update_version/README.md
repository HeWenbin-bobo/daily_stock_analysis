# 三榜交集分析（update_version）

一个独立的子项目，挂在现有 `daily_stock_analysis` 主项目下，复用其后端 `StockAnalysisPipeline.analyze_stock()` 的能力，做"三榜实时筛选 + 三轮 LLM 分析 + 严格过滤"的端到端分析。

## 功能概览

1. **抓三榜**：东方财富人气榜 + 雪球关注榜 + 同花顺热度榜
2. **求交集**：三榜标准化代码后取交集，剔除科创板（688*）
3. **三轮分析**：对每只股票调用 `analyze_stock()` 跑 3 轮（带轮间间隔制造扰动）
4. **过滤**：
   - 3 轮 score 均 ≥ 60 **或** 3 轮 advice 均为 买入/加仓/持有
   - 任意一轮出现"卖出/减仓" → 直接淘汰
5. **呈现**：单页 Vue 3 应用，单按钮触发，可排序表格，复制 / 导出 .txt

## 目录结构

```
update_version/
├── backend/                  # （说明：本项目后端代码已合并到主项目 FastAPI 中）
│                             # 新增文件：
│                             #   data_provider/hot_lists.py
│                             #   api/v1/schemas/hot_analysis.py
│                             #   api/v1/endpoints/hot_analysis.py
│                             # 已注册到 api/v1/router.py
├── frontend/                 # 独立 Vue 3 + Vite + TypeScript SPA
│   ├── package.json
│   ├── vite.config.ts        # /api 代理到 127.0.0.1:8000
│   ├── index.html
│   └── src/
│       ├── main.ts
│       ├── App.vue           # 单页面（按钮 + 表格 + 空态）
│       ├── api/hotAnalysis.ts
│       └── styles/global.css
├── README.md
├── BACKEND_PATCH.md          # 主项目改动备忘
├── start_hot_analysis.bat    # Windows 一键启动（仿主项目风格）
├── stop_hot_analysis.bat     # Windows 一键停止
├── .run_state/               # 运行时状态（gitignored）
│   └── backend.pid           # 标记：start 是否启动了 8000
```

## 启动方法

### 1. 启动后端（主项目 FastAPI）

```bash
# 回到主项目根目录
cd F:\daily_stock_analysis

# 激活 conda 环境（如果使用 daily_stock_analysis 环境）
# 注意：bat 文件中应使用 conda run -n daily_stock_analysis ...（避开 conda activate 在批处理中的副作用）

# 启动 API 服务（端口 8000）
python main.py --serve-only
# 或：
uvicorn server:app --host 0.0.0.0 --port 8000
```

### 2. 启动前端

```bash
cd F:\daily_stock_analysis\update_version\frontend

# 首次启动安装依赖
npm install

# 启动 dev server（端口 5173）
npm run dev
```

打开浏览器访问 `http://localhost:5173`。

### 3. 一键启动 / 停止（Windows，推荐）

双击以下脚本（**仿主项目 `daily_stock.bat` / `stop_daily_stock.bat` 风格**）：

| 脚本 | 行为 |
|------|------|
| `start_hot_analysis.bat` | 启动后端（如 8000 已被占用则复用主项目后端）+ Vite dev (5173) + 打开浏览器 |
| `stop_hot_analysis.bat`  | **总是**杀掉 5173 前端；**仅当**本脚本启动过后端时杀掉 8000 |

启动后访问 `http://localhost:5173`，关闭后只需双击 `stop_hot_analysis.bat`。

> **共享 8000 设计**：与主项目 `daily_stock.bat` 共享 8000 端口的后端进程。后端用 `.run_state/backend.pid` 标记"是否本子项目启动的"，避免 stop 时误杀主项目正在运行的后端。

## API 端点

- `POST /api/v1/hot-analysis/run` —— 触发完整流程
- `GET /api/v1/hot-analysis/health` —— 健康检查

### 请求体示例

```json
{
  "hot_list_n": 200,
  "max_intersection": 10,
  "exclude_kechuang": true,
  "rounds": 3,
  "max_runtime_seconds": 600,
  "inter_round_sleep": 0.6,
  "simple_report": true
}
```

### 响应体示例（节选）

```json
{
  "status": "ok",
  "duration_seconds": 187.4,
  "hot_lists_summary": {"东方财富": 100, "雪球": 100, "同花顺": 100},
  "raw_intersection_count": 12,
  "filtered_intersection_count": 9,
  "analyzed_stock_count": 27,
  "passed_stock_count": 2,
  "results": [
    {
      "code": "600519",
      "name": "贵州茅台",
      "in_sources": ["东方财富", "雪球", "同花顺"],
      "pass_reason": "score+advice",
      "avg_score": 78.3,
      "final_advice": "持有",
      "analyses": [
        {"round": 1, "sentiment_score": 75, "operation_advice": "持有", "timestamp": "2026-07-03T10:00:01"},
        {"round": 2, "sentiment_score": 80, "operation_advice": "买入", "timestamp": "2026-07-03T10:00:08"},
        {"round": 3, "sentiment_score": 80, "operation_advice": "持有", "timestamp": "2026-07-03T10:00:15"}
      ]
    }
  ]
}
```

## 关键设计决策

| 决策 | 理由 |
|------|------|
| **后端 API 复用现有 FastAPI** | 任务要求"优先复用项目现有架构"；CORS、auth、logging、Config 全部现成 |
| **不重新实现 LLM 调用** | 直接调用 `StockAnalysisPipeline.analyze_stock()`（项目核心入口） |
| **三榜抓取独立于现有 fetcher** | 现有 `AkshareFetcher.get_hot_stocks()` 只返回单源（fallback 链），本任务需要 3 个**独立**列表 |
| **同花顺用 `stock_hot_ths_*d` 系列** | akshare 的同花顺接口按时间维度划分，依次降级尝试（20d→10d→5d→30d） |
| **max_intersection 默认 10** | 避免 LLM 调用爆炸（10 × 3 轮 = 30 次 LLM 调用 ≈ 1-5 分钟） |
| **温度扰动 = 轮间 sleep + LLM 自身非确定性** | 不侵入修改 analyzer 内部 LLM 调用，保持架构纯净 |
| **报告类型默认 SIMPLE** | 节省时间与 token；可由前端参数切换为 FULL |
| **Vue 3 + Vite + Naive UI** | 任务推荐；轻量、组件丰富、TypeScript 友好 |
| **前端 5173 端口 + 代理** | 与主项目 CORS 白名单一致；开发期无需配 CORS |

## 已知限制

1. **榜单"前 1000 名"工程上限**：akshare 的 `stock_hot_rank_em` / `stock_hot_follow_xq` 实际最多返回约 100 条；任务要求"前 1000"在工程上等同"全榜"，已在代码注释中说明。
2. **同花顺接口可能版本变化**：akshare 各版本提供的 `stock_hot_ths_*d` / `stock_hot_rank_ths` 接口名可能不同；本模块按当前主流版本写死，并做了 candidate 降级。
3. **LLM 长任务**：每只股票 3 轮分析 + LLM 响应，单次完整流程典型耗时 1-5 分钟（视 max_intersection 与 LLM 速度）。前端已用 `loading` 状态锁定按钮防重入。
4. **同步阻塞请求**：当前实现是同步 HTTP，**未做轮询 / SSE**。如需更友好的进度反馈，可改造为后台任务 + `task_id` 轮询。

## 验证

- 后端：`python -m py_compile data_provider/hot_lists.py api/v1/schemas/hot_analysis.py api/v1/endpoints/hot_analysis.py` —— ✅
- 路由注册：导入 `api.v1.router` 后 `/api/v1/hot-analysis/*` 出现在 routes 列表 —— ✅
- 过滤逻辑：4 种典型场景（score+advice / advice-only / 出现卖出 / 评分低+观望） —— ✅
- 前端：`npm run build` 通过（见 `update_version/frontend/dist/`）
