# 后端集成备忘录（BACKEND_PATCH）

> **目的**：本文件记录为了支撑 `update_version/frontend` 运行，在主项目（`daily_stock_analysis/`）中所做的**全部改动**。
>
> **为什么需要它**：`update_version/frontend` 是一个独立的 Vue 3 SPA，但需要主项目的 FastAPI 提供后端 API。如果主项目将来更新或重构，本文件可作为修复指南，让 `update_version/frontend` 继续工作。

---

## 0. 总览：本次对主项目的改动

| 文件 | 类型 | 行数变化 | 说明 |
|------|------|----------|------|
| `data_provider/hot_lists.py` | **新建** | +310 行 | 三榜抓取 + 交集计算工具 |
| `api/v1/schemas/hot_analysis.py` | **新建** | +95 行 | Pydantic Schema |
| `api/v1/endpoints/hot_analysis.py` | **新建** | +260 行 | FastAPI 端点 + 过滤逻辑 |
| `api/v1/router.py` | **修改** | +7 行 | 注册新路由 + import |
| `api/v1/endpoints/__init__.py` | **修改** | +2 行 | 导出新模块 |

> **重要**：本前端只依赖**外部行为**（API 路径、请求/响应字段、所需配置），**不**依赖主项目内部实现细节。即使主项目内部重构，只要保留外部行为兼容，前端无需改动。

> **运行时状态文件**：本子项目在 `update_version/.run_state/backend.pid` 写入 1 字节标记（仅用于 `stop_hot_analysis.bat` 识别 8000 端口是否由本子项目启动）。该目录已被 `.gitignore` 忽略，无需纳入版本控制；如主项目备份时需要"完整还原本子项目状态"，可跳过此目录。

> **一键脚本的编码要求**：`start_hot_analysis.bat` / `stop_hot_analysis.bat` **必须**是 **GBK 编码 + CRLF 行尾**，否则在 Windows cmd.exe 中中文会乱码、可能误执行错误的命令。检查命令：`file update_version/start_hot_analysis.bat` 应显示 `ISO-8859 text, with CRLF line terminators`；如需重写，请用 `iconv -f UTF-8 -t GBK` 转换编码，再用 `unix2dos` 转换行尾。

---

## 1. 新增文件全文

### 1.1 `data_provider/hot_lists.py`（新建）

> 完整源码在 `F:\daily_stock_analysis\data_provider\hot_lists.py`，**300+ 行**。本节列出**关键公共 API**，用于"主项目代码漂移"时对齐。

#### 关键公共 API

```python
# === 标准化与过滤 ===
def normalize_code(code: Any) -> str
    """标准化到 6 位字符串；去 SH/SZ/BJ 前缀；补 0；非数字返回 ''。"""

def is_a_share_main(code: str) -> bool
    """True iff code 是 60* / 00* / 30* 开头；False iff 688* 或其他。"""

# === 三个榜单独立抓取 ===
def fetch_eastmoney_hot(n: int = 200) -> List[Dict[str, Any]]
    """调用 akshare.stock_hot_rank_em()，返回 [{code, name, rank, source}, ...]"""

def fetch_xueqiu_hot(n: int = 200) -> List[Dict[str, Any]]
    """调用 akshare.stock_hot_follow_xq(symbol='最热门')"""

def fetch_ths_hot(n: int = 200) -> List[Dict[str, Any]]
    """按时间维度降级尝试 akshare.stock_hot_ths_20d → 10d → 5d → 30d → rank_ths/follow_ths/ths_index"""

# === 抓取全部 + 交集计算 ===
def fetch_all_three(n: int = 200) -> Dict[str, List[Dict[str, Any]]]
    """返回 {"东方财富": [...], "雪球": [...], "同花顺": [...]}"""

def compute_intersection(lists: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]
    """求多榜交集，标准化 code；返回 [{code, name, in_sources, ranks}, ...]"""

def filter_a_share(stocks, *, exclude_kechuang: bool = True) -> List[Dict[str, Any]]
    """剔除科创板 688*；仅保留 60* / 00* / 30*"""
```

#### 模块依赖
- `akshare`（运行时 import，避免硬依赖）
- 标准库：`logging / threading / time / typing`

#### 缓存策略
- 进程内内存缓存 `_CACHE`（dict），TTL 5 分钟
- Key：`"eastmoney"` / `"xueqiu"` / `"ths"`

#### 如果主项目 `akshare` 版本变化
1. 检查 `fetch_ths_hot` 中的 candidates 列表是否仍然有效：
   ```python
   ("stock_hot_ths_20d", lambda: ak.stock_hot_ths_20d()),
   ("stock_hot_ths_10d", lambda: ak.stock_hot_ths_10d()),
   ("stock_hot_ths_5d", lambda: ak.stock_hot_ths_5d()),
   ("stock_hot_ths_30d", lambda: ak.stock_hot_ths_30d()),
   ```
2. 如果某个接口在新版被移除/改名，注释掉对应行即可
3. 如果想加新候选：append `("新接口名", lambda: ak.新接口(...))` 即可

---

### 1.2 `api/v1/schemas/hot_analysis.py`（新建）

> 完整源码在 `F:\daily_stock_analysis\api\v1\schemas\hot_analysis.py`，**95 行**。定义了 3 个 Pydantic 模型。

```python
class HotAnalysisRunRequest(BaseModel):
    hot_list_n: int = 200           # 20-1000
    max_intersection: int = 10      # 1-50
    exclude_kechuang: bool = True
    rounds: int = 3                 # 1-5
    max_runtime_seconds: int = 600  # 30-3600
    inter_round_sleep: float = 0.6 # 0-10
    simple_report: bool = True

class AnalysisRunItem(BaseModel):
    round: int
    sentiment_score: Optional[int] = None
    operation_advice: Optional[str] = None
    decision_type: Optional[str] = None
    trend_prediction: Optional[str] = None
    model_used: Optional[str] = None
    timestamp: Optional[str] = None
    error: Optional[str] = None

class HotAnalysisStockResult(BaseModel):
    code: str
    name: str
    in_sources: List[str]
    analyses: List[AnalysisRunItem]
    pass_reason: str                # "score" / "advice" / "score+advice" / "incomplete" / "rejected_sell" / "rejected_filter"
    avg_score: float
    final_advice: str

class HotAnalysisRunResponse(BaseModel):
    status: str                     # "ok" / "failed" / "timeout"
    started_at: str                 # ISO8601
    finished_at: str                # ISO8601
    duration_seconds: float
    hot_lists_summary: Dict[str, int]   # {"东方财富": int, "雪球": int, "同花顺": int}
    raw_intersection_count: int
    filtered_intersection_count: int
    analyzed_stock_count: int
    passed_stock_count: int
    results: List[HotAnalysisStockResult]
    error: Optional[str] = None
```

> **前端契约**：如果这些字段名/类型变了，必须同步更新 `update_version/frontend/src/api/hotAnalysis.ts` 中的 `HotAnalysisRunResponse / HotAnalysisStockResult / AnalysisRunItem` 三个 interface。

---

### 1.3 `api/v1/endpoints/hot_analysis.py`（新建）

> 完整源码在 `F:\daily_stock_analysis\api\v1\endpoints\hot_analysis.py`，**260 行**。

#### 路由清单

| 方法 | 路径 | 用途 | 请求体 | 响应体 |
|------|------|------|--------|--------|
| `POST` | `/api/v1/hot-analysis/run` | 触发全流程 | `HotAnalysisRunRequest` | `HotAnalysisRunResponse` |
| `GET` | `/api/v1/hot-analysis/health` | 健康检查 | - | `{status, akshare_available, ...}` |

#### 关键依赖（从主项目 import）

```python
from api.v1.schemas.hot_analysis import (...)           # 上面 §1.2
from data_provider.hot_lists import (
    compute_intersection, fetch_all_three, filter_a_share,
)                                                      # 上面 §1.1

# 懒加载（避免 endpoint 模块 import 时就初始化 LLM）
from src.config import get_config                      # 主项目 Config
from src.core.pipeline import StockAnalysisPipeline    # 主项目核心 LLM 编排
from src.enums import ReportType                       # 主项目枚举
```

#### 调用链
```
POST /api/v1/hot-analysis/run
    ↓
1. fetch_all_three(req.hot_list_n)             # 三榜抓取
2. compute_intersection(lists)                 # 求交
3. filter_a_share(intersection, kechuang)      # 剔 688
4. StockAnalysisPipeline(config, max_workers=1)  # 初始化（只构造一次）
5. for stock in target[:max_intersection]:     # 串行
     for round in 1..rounds:
       pipeline.analyze_stock(stock, ReportType.SIMPLE, query_id, current_time)
     _check_pass([3 rounds])
6. 返回 HotAnalysisRunResponse
```

#### 关键函数（行为契约）

```python
def _check_pass(analyses: List[Dict]) -> Tuple[str, bool]:
    """
    规则：
    - 任何一轮出现 "卖出" / "减仓" / "SELL" / "REDUCE" → rejected_sell (False)
    - 否则：所有有效轮 score >= 60 → "score" (True)
    - 否则：所有有效轮 advice ∈ {买入/加仓/持有/BUY/HOLD/ADD} → "advice" (True)
    - 否则：rejected_filter (False)
    """
```

#### 如果主项目 `StockAnalysisPipeline` 变化
- **关注点 1**：`pipeline.analyze_stock()` 的签名
  - 当前签名：`analyze_stock(code: str, report_type: ReportType, query_id: str, current_time: Optional[datetime] = None) -> Optional[AnalysisResult]`
  - 如果将来要求必传新参数，需要修改 `api/v1/endpoints/hot_analysis.py:_run_one_analysis_sync`

- **关注点 2**：`AnalysisResult` 的字段
  - 关键字段：`sentiment_score (int 0-100)`, `operation_advice (str ∈ {买入, 加仓, 持有, 减仓, 卖出, 观望})`
  - 字段重命名：前端会拿到 None，前端代码 `_check_pass` 失效

- **关注点 3**：`ReportType` 枚举值
  - 当前使用：`ReportType.SIMPLE` / `ReportType.FULL`
  - 在 `src/enums.py` 定义

---

## 2. 修改文件 diff（before / after）

### 2.1 `api/v1/router.py`（2 处改动）

#### Diff 1：import 块（顶部 ~第 14-29 行）

**Before**：
```python
from api.v1.endpoints import (
    agent,
    alerts,
    alphasift,
    analysis,
    auth,
    backtest,
    decision_signals,
    health,
    history,
    intelligence,
    portfolio,
    stocks,
    system_config,
    usage,
)
```

**After**：
```python
from api.v1.endpoints import (
    agent,
    alerts,
    alphasift,
    analysis,
    auth,
    backtest,
    decision_signals,
    health,
    history,
    hot_analysis,                    # ← 新增
    intelligence,
    portfolio,
    stocks,
    system_config,
    usage,
)
```

#### Diff 2：include_router 块（底部 ~第 113-116 行后）

**Before**（在 `intelligence` include_router 之后）：
```python
router.include_router(
    intelligence.router,
    prefix="/intelligence",
    tags=["Intelligence"]
)
```

**After**（在 `intelligence` include_router 之后追加）：
```python
router.include_router(
    intelligence.router,
    prefix="/intelligence",
    tags=["Intelligence"]
)

router.include_router(
    hot_analysis.router,             # ← 新增
    prefix="/hot-analysis",          # ← 新增
    tags=["HotAnalysis"]             # ← 新增
)
```

---

### 2.2 `api/v1/endpoints/__init__.py`（2 处改动）

**Before**（第 11-40 行）：
```python
from api.v1.endpoints import (
    health,
    analysis,
    history,
    stocks,
    backtest,
    system_config,
    auth,
    agent,
    usage,
    portfolio,
    alerts,
    decision_signals,
    alphasift,
)
__all__ = [
    "health",
    "analysis",
    "history",
    "stocks",
    "backtest",
    "system_config",
    "auth",
    "agent",
    "usage",
    "portfolio",
    "alerts",
    "decision_signals",
    "alphasift",
]
```

**After**：
```python
from api.v1.endpoints import (
    health,
    analysis,
    history,
    stocks,
    backtest,
    system_config,
    auth,
    agent,
    usage,
    portfolio,
    alerts,
    decision_signals,
    alphasift,
    hot_analysis,                    # ← 新增
)
__all__ = [
    "health",
    "analysis",
    "history",
    "stocks",
    "backtest",
    "system_config",
    "auth",
    "agent",
    "usage",
    "portfolio",
    "alerts",
    "decision_signals",
    "alphasift",
    "hot_analysis",                  # ← 新增
]
```

---

## 3. 主项目外部环境依赖

后端运行还隐式依赖以下主项目已有的配置：

| 配置 | 位置 | 必需 |
|------|------|------|
| 至少 1 个 LLM API Key | `.env`（`GEMINI_API_KEY` / `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` 等） | ✅ |
| `STOCK_LIST` | `.env`（**不强制**，本端点不读） | ❌ |
| CORS 允许 `http://localhost:5173` | `api/app.py:334-339`（已配置） | ✅ |
| 端口 8000 不被占用 | 操作系统 | ✅ |
| `akshare` 库 | `requirements.txt` | ✅ |

**注意**：`hot_lists.py` 中 `import akshare` 是运行时 import，不会因 akshare 缺失导致模块加载失败（端点会返回 500 错误而不是启动失败）。

---

## 4. 重新应用补丁（如果主项目被重置）

按以下顺序恢复：

```bash
# 1. 把下列 3 个文件恢复到主项目原位（内容见主项目原文件或本备忘 §1）
cp <backup>/data_provider/hot_lists.py       F:/daily_stock_analysis/data_provider/hot_lists.py
cp <backup>/api/v1/schemas/hot_analysis.py  F:/daily_stock_analysis/api/v1/schemas/hot_analysis.py
cp <backup>/api/v1/endpoints/hot_analysis.py F:/daily_stock_analysis/api/v1/endpoints/hot_analysis.py

# 2. 修改 api/v1/router.py（2 处，见 §2.1）：
#    - import 块加 hot_analysis
#    - 末尾追加 hot_analysis.router include

# 3. 修改 api/v1/endpoints/__init__.py（2 处，见 §2.2）：
#    - import 块加 hot_analysis
#    - __all__ 加 "hot_analysis"

# 4. 验证：
cd F:/daily_stock_analysis
python -m py_compile data_provider/hot_lists.py api/v1/schemas/hot_analysis.py api/v1/endpoints/hot_analysis.py
python -c "from api.v1.router import router; print([r.path for r in router.routes if '/hot-analysis' in r.path])"
# 期望输出: ['/api/v1/hot-analysis/run', '/api/v1/hot-analysis/health']
```

---

## 5. 回滚主项目（如果需要撤销本子项目）

```bash
# 1. 删除 3 个新文件
rm F:/daily_stock_analysis/data_provider/hot_lists.py
rm F:/daily_stock_analysis/api/v1/schemas/hot_analysis.py
rm F:/daily_stock_analysis/api/v1/endpoints/hot_analysis.py

# 2. 还原 2 个修改文件（用 git checkout 或手动移除 §2 中标注的"新增"行）
cd F:/daily_stock_analysis
git checkout api/v1/router.py api/v1/endpoints/__init__.py
# 或手动编辑移除 "hot_analysis" 行（见 §2）

# 3. 验证后端可正常启动
python -c "from api.v1.router import router; print('OK')"
```

> ⚠️ **回滚后** `update_version/frontend` 仍可启动，但其 API 调用（`/api/v1/hot-analysis/run`）将得到 404。

---

## 6. 兼容性检查清单（主项目更新后）

| 主项目变更 | 风险等级 | 前端是否需要改 | 备忘位置 |
|------------|----------|----------------|----------|
| `akshare` 大版本升级 | 中 | ❌ | §1.1 — 检查 `stock_hot_ths_*d` 是否改名 |
| `StockAnalysisPipeline.analyze_stock()` 签名变化 | **高** | ❌ | §1.3 — 关注 1 |
| `AnalysisResult.sentiment_score` 字段重命名 | **高** | ✅ | §1.3 — 关注 2 |
| `AnalysisResult.operation_advice` 字段重命名 | **高** | ✅ | §1.3 — 关注 2 |
| `ReportType` 枚举值变化 | 低 | ❌ | §1.3 — 关注 3 |
| CORS 白名单移除 5173 | 中 | ❌ | §3 |
| 端口从 8000 改为其它 | 中 | ✅ | `update_version/frontend/vite.config.ts` |
| FastAPI 框架大改（如迁移到其它） | **高** | ✅ | 整个 `api/hotAnalysis.ts` |
| 主项目移除 `data_provider/hot_lists.py` | **高** | ❌ | §1.1 |
| 新增其它端点 | 无影响 | ❌ | — |

---

## 7. 快速诊断命令

```bash
# 1. 后端路由是否注册？
cd F:/daily_stock_analysis
python -c "from api.v1.router import router; print([r.path for r in router.routes if '/hot-analysis' in r.path])"

# 2. 抓榜是否可用？
python -c "from data_provider.hot_lists import fetch_all_three; print(fetch_all_three(10))"

# 3. LLM Pipeline 是否能实例化？
python -c "from src.core.pipeline import StockAnalysisPipeline; p = StockAnalysisPipeline(); print('OK')"

# 4. 端到端冒烟（会消耗 LLM 配额！仅用于一次性验证）：
curl -X POST http://localhost:8000/api/v1/hot-analysis/run \
  -H "Content-Type: application/json" \
  -d '{"max_intersection": 1, "rounds": 1, "max_runtime_seconds": 60}'

# 5. 验证 .bat 脚本编码
file update_version/start_hot_analysis.bat update_version/stop_hot_analysis.bat
# 期望输出: "DOS batch file, ISO-8859 text, with CRLF line terminators"
# 如显示 "UTF-8" 或 "LF" 需重新转换:
iconv -f UTF-8 -t GBK update_version/stop_hot_analysis.bat | unix2dos > /tmp/x && mv /tmp/x update_version/stop_hot_analysis.bat
```

## 7.1 一键脚本功能验证（实测通过 2026-07-03）

| 场景 | 期望 | 实测 |
|------|------|------|
| `start_hot_analysis.bat` 双击 / `cmd.exe //c` 调用 | 启动后端（conda run python main.py --serve-only）+ Vite（npm run dev）+ 打开浏览器 | ✅ 6 步全部执行，端口 8000 + 5173 起来 |
| 后端启动延迟 | conda run 启动 + main.py 初始化需要 **30-45 秒** | ✅ 实测 35-45s |
| `stop_hot_analysis.bat` 前端总是杀 | taskkill PID 13552（Vite node.exe） | ✅ "成功: 已终止 PID 13552" |
| `stop_hot_analysis.bat` 后端按 ownership 杀 | taskkill PID 11636（FastAPI python.exe） | ✅ "成功: 已终止 PID 11636" |
| `.run_state/backend.pid` 自动清理 | stop 成功后 marker 文件被 `del` 删除 | ✅ `ls .run_state/` 显示空目录 |
| 不杀主项目后端 | stop 时若无 marker，跳过 8000 杀进程 | ✅ "No marker - we don't own the backend. Skipping." |

---

## 8. 文件位置总图

```
F:\daily_stock_analysis\                      # 主项目（被修改了 5 个文件）
├── data_provider\
│   └── hot_lists.py                          # ← 新增
├── api\
│   └── v1\
│       ├── router.py                         # ← 修改（+7 行）
│       ├── endpoints\
│       │   ├── __init__.py                   # ← 修改（+2 行）
│       │   └── hot_analysis.py               # ← 新增
│       └── schemas\
│           └── hot_analysis.py               # ← 新增
└── update_version\                           # 子项目（本前端所在）
    ├── BACKEND_PATCH.md                      # ← 本备忘
    ├── README.md
    ├── start.bat / start.sh  (历史遗留，可忽略)
    └── frontend\                             # Vue 3 + Vite SPA
        ├── package.json
        ├── vite.config.ts                    # 代理 /api → :8000
        ├── tsconfig.json
        ├── index.html
        └── src\
            ├── main.ts
            ├── App.vue
            ├── api\hotAnalysis.ts            # 调用 /api/v1/hot-analysis/*
            └── styles\global.css
```

---

**最后更新**：本次实现时同步创建。
**维护建议**：每次主项目有破坏性变更（特别是 LLM 字段、Pipeline 签名、akshare 大版本），回来更新 §6 兼容性表。
