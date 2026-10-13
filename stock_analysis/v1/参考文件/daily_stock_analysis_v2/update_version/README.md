# 四榜交集分析（update_version）

一个独立的子项目，挂在现有 `daily_stock_analysis` 主项目下，复用其后端 `StockAnalysisPipeline.analyze_stock()` 的能力，做"四榜实时筛选 + 三轮 LLM 分析 + 严格过滤"的端到端分析。

## 功能概览

1. **抓四榜**：东方财富人气榜 + 雪球关注榜 + 同花顺热度榜 + 全市场活跃
2. **求交集**：四榜标准化代码后取交集，剔除科创板（688*）
3. **三轮分析**：对每只股票调用 `analyze_stock()` 跑 3 轮（带轮间间隔制造扰动）
4. **过滤**：
   - 3 轮 score 均 ≥ 60 **或** 3 轮 advice 均为 买入/加仓/持有
   - 任意一轮出现"卖出/减仓" → 直接淘汰
   - 支持自定义过滤规则（最低评分、允许建议、排除关键词）
5. **呈现**：单页 Vue 3 应用，单按钮触发，可排序表格，复制 / 导出 .txt / CSV / PDF

## 功能模块

| 模块 | 说明 |
|------|------|
| 📊 分析 | 四榜交集 + 三轮 LLM 分析，支持预览 → 确认 → 分析流程 |
| 🔍 单股 | 直接输入股票代码/名称进行分析（不走四榜交集） |
| 📚 历史 | 查看历史分析记录、删除、查看详情 |
| 🔄 对比 | 选择两次分析结果进行对比（新增/退出/评分变化） |
| ⭐ 自选 | 自选股跟踪管理 |
| 📋 模板 | 保存/加载过滤规则预设 |
| ⏰ 定时 | 定时任务管理（手动触发模式） |

## 目录结构

```
update_version/
├── frontend/                 # 独立 Vue 3 + Vite + TypeScript SPA
│   ├── package.json
│   ├── vite.config.ts        # /api 代理到 127.0.0.1:8000
│   ├── index.html
│   └── src/
│       ├── main.ts
│       ├── App.vue           # 主页面（多标签视图）
│       ├── api/hotAnalysis.ts
│       ├── composables/      # Vue composables
│       ├── components/       # Vue 组件
│       ├── utils/            # 工具函数
│       └── styles/global.css
├── db/
│   └── history.py            # SQLite 持久化（历史记录、自选股、模板、定时任务）
├── tests/
│   └── test_hot_analysis.py  # 单元测试
├── README.md
├── BACKEND_PATCH.md          # 主项目改动备忘
├── .gitignore
├── start_hot_analysis.bat    # Windows 一键启动
└── stop_hot_analysis.bat     # Windows 一键停止
```

## 启动方法

### 1. 启动后端（主项目 FastAPI）

```bash
# 回到主项目根目录
cd F:\daily_stock_analysis

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

双击以下脚本：

| 脚本 | 行为 |
|------|------|
| `start_hot_analysis.bat` | 启动后端（如 8000 已被占用则复用主项目后端）+ Vite dev (5173) + 打开浏览器 |
| `stop_hot_analysis.bat`  | **总是**杀掉 5173 前端；**仅当**本脚本启动过后端时杀掉 8000 |

启动后访问 `http://localhost:5173`，关闭后只需双击 `stop_hot_analysis.bat`。

> **共享 8000 设计**：与主项目 `daily_stock.bat` 共享 8000 端口的后端进程。

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/v1/hot-analysis/run` | 触发完整流程 |
| `POST` | `/api/v1/hot-analysis/preview` | 预览（仅抓榜） |
| `POST` | `/api/v1/hot-analysis/run-async` | 异步任务（返回 task_id） |
| `GET` | `/api/v1/hot-analysis/run/{task_id}/stream` | SSE 进度推送 |
| `POST` | `/api/v1/hot-analysis/analyze-single` | 单股分析 |
| `GET` | `/api/v1/hot-analysis/health` | 健康检查 |
| `GET` | `/api/v1/hot-analysis/history` | 历史记录（分页） |
| `GET` | `/api/v1/hot-analysis/history/{run_id}` | 历史详情 |
| `DELETE` | `/api/v1/hot-analysis/history/{run_id}` | 删除历史记录 |
| `POST` | `/api/v1/hot-analysis/compare` | 对比分析 |
| `GET` | `/api/v1/hot-analysis/watchlist` | 自选股列表 |
| `POST` | `/api/v1/hot-analysis/watchlist` | 添加自选股 |
| `DELETE` | `/api/v1/hot-analysis/watchlist/{code}` | 移除自选股 |
| `GET` | `/api/v1/hot-analysis/templates` | 模板列表 |
| `POST` | `/api/v1/hot-analysis/templates` | 保存模板 |
| `DELETE` | `/api/v1/hot-analysis/templates/{id}` | 删除模板 |
| `GET` | `/api/v1/hot-analysis/scheduled-tasks` | 定时任务列表 |
| `POST` | `/api/v1/hot-analysis/scheduled-tasks` | 保存定时任务 |
| `DELETE` | `/api/v1/hot-analysis/scheduled-tasks/{id}` | 删除定时任务 |
| `POST` | `/api/v1/hot-analysis/scheduled-tasks/{id}/run` | 手动触发任务 |
| `GET` | `/api/v1/hot-analysis/market-data` | 行情数据 |
| `POST` | `/api/v1/hot-analysis/pdf-report` | PDF 报告 |

### 请求体示例

```json
{
  "hot_list_n": 200,
  "max_intersection": 10,
  "exclude_kechuang": true,
  "rounds": 3,
  "max_runtime_seconds": 600,
  "inter_round_sleep": 0.6,
  "simple_report": true,
  "llm_timeout_seconds": 180,
  "min_score": 60,
  "allowed_advice": ["买入", "加仓", "持有"],
  "exclude_keywords": ["卖出", "减仓"],
  "require_all_rounds": true
}
```

## 关键设计决策

| 决策 | 理由 |
|------|------|
| **后端 API 复用现有 FastAPI** | 任务要求"优先复用项目现有架构"；CORS、auth、logging、Config 全部现成 |
| **不重新实现 LLM 调用** | 直接调用 `StockAnalysisPipeline.analyze_stock()`（项目核心入口） |
| **四榜抓取独立于现有 fetcher** | 现有 `AkshareFetcher.get_hot_stocks()` 只返回单源（fallback 链），本任务需要 4 个**独立**列表 |
| **同花顺用 `stock_hot_ths_*d` 系列** | akshare 的同花顺接口按时间维度划分，依次降级尝试（20d→10d→5d→30d） |
| **max_intersection 默认 10** | 避免 LLM 调用爆炸（10 × 3 轮 = 30 次 LLM 调用 ≈ 1-5 分钟） |
| **温度扰动 = 轮间 sleep + LLM 自身非确定性** | 不侵入修改 analyzer 内部 LLM 调用，保持架构纯净 |
| **报告类型默认 SIMPLE** | 节省时间与 token；可由前端参数切换为 FULL |
| **Vue 3 + Vite + Naive UI** | 任务推荐；轻量、组件丰富、TypeScript 友好 |
| **前端 5173 端口 + 代理** | 与主项目 CORS 白名单一致；开发期无需配 CORS |
| **SQLite 持久化** | 历史记录、自选股、模板、定时任务均使用本地 SQLite 存储 |
| **SSE 进度推送** | 异步任务模式 + EventSource 实时推送分析进度 |
| **暗色模式** | 支持亮色/暗色主题切换，使用 CSS 变量实现 |

## 技术栈

### 前端
- Vue 3 + TypeScript + Vite
- Naive UI 组件库
- Axios HTTP 客户端
- CSS 自定义属性（暗色模式）

### 后端
- FastAPI + Pydantic v2
- SQLite（历史记录持久化）
- akshare（榜单数据抓取）
- SSE（Server-Sent Events 进度推送）

## 已知限制

1. **榜单"前 1000 名"工程上限**：akshare 的 `stock_hot_rank_em` / `stock_hot_follow_xq` 实际最多返回约 100 条；任务要求"前 1000"在工程上等同"全榜"。
2. **同花顺接口可能版本变化**：akshare 各版本提供的 `stock_hot_ths_*d` / `stock_hot_rank_ths` 接口名可能不同；本模块按当前主流版本写死，并做了 candidate 降级。
3. **LLM 长任务**：每只股票 3 轮分析 + LLM 响应，单次完整流程典型耗时 1-5 分钟。
4. **定时任务仅为手动触发**：cron 调度逻辑未实现，定时任务只能手动点击"执行"。
5. **Webhook 通知未实现**：API 支持 `webhook_url` 参数但实际发送逻辑未实现。

## 验证

- 后端：`python -m py_compile data_provider/hot_lists.py api/v1/schemas/hot_analysis.py api/v1/endpoints/hot_analysis.py`
- 路由注册：导入 `api.v1.router` 后 `/api/v1/hot-analysis/*` 出现在 routes 列表
- 过滤逻辑：4 种典型场景（score+advice / advice-only / 出现卖出 / 评分低+观望）
- 前端：`npm run build` 通过
- 测试：`python -m pytest update_version/tests/ -v`
