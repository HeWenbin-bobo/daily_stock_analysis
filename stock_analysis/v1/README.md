# 个股 K线 + 波峰波谷分析

一个轻量级前端应用, 用于查看 A股/港股/美股的日/周/月 K线数据, 并从当前年度最高点之后开始分析波峰波谷的最大回撤和回弹比例。集成本地 CC switch 切换的 LLM, 提供一键 AI 解读。

## 技术栈

- **前端**: Vite + React 18 + TypeScript + Tailwind CSS + lightweight-charts + Zustand + Axios
- **后端**: FastAPI + akshare + yfinance + LiteLLM
- **LLM**: 读取本地 CC switch (`C:\Users\jww\.cc-switch\`) 当前激活 provider

## 启动

### 方式一: 本地开发 (推荐)

```cmd
start.bat
```

或分别启动:
```cmd
cd backend && run.bat
cd frontend && run.bat
```

### 方式二: Docker 一键部署

```bash
docker compose up --build
```

启动后访问 http://127.0.0.1:5179 (前端) / http://127.0.0.1:8100/docs (Swagger)

> **端口说明**: 后端 `8100` / 前端 `5179` 是刻意挑选的, 与参考项目 `daily_stock_analysis_v2` 的 `WEBUI_PORT=8000` / Vite 默认 `5173` 不冲突, 两个系统可以同时运行。

## 功能特性

| 功能 | 状态 |
|---|---|
| A股/港股/美股 日/周/月 K线 | ✅ |
| 股票搜索 (代码/中文名/分类) | ✅ 100+ 内置 hint |
| K线图 + MA5/MA10/MA20 + 成交量 | ✅ |
| 波峰/波谷自动识别 (从年度高点) | ✅ |
| BOLL 布林带叠加 | ✅ |
| MACD 副图 (DIF/DEA/HIST) | ✅ |
| KDJ 副图 (K/D/J) | ✅ |
| 指标切换器 (可独立开关) | ✅ |
| 年度高点水平线 + 波峰波谷 markers | ✅ |
| 回撤/回弹 + 最大回撤/最大回弹 | ✅ |
| **当前价** vs **年度高点** vs **年初价** (3 个独立指标) | ✅ |
| AI 一键解读 (Markdown 渲染 + 重试) | ✅ |
| 数据导出 (CSV / JSON) | ✅ |
| 暗/亮主题切换 + 跟随系统 | ✅ |
| 移动端响应式 | ✅ |
| 自选股 (⭐ + localStorage + JSON 导入/导出) | ✅ |
| 错误边界 + 加载骨架屏 | ✅ |
| Mock 数据 banner (网络不可用时) | ✅ |
| 进程内 TTL+LRU 缓存 (K线 5min, 分析 5min) | ✅ |
| 端点性能监控 + /admin/stats 调试面板 | ✅ |
| LLM 速率限制 5/min/IP (防滥用) | ✅ |
| Docker 一键部署 (backend + nginx) | ✅ |
| **自动化测试 (682 用例, 96% 覆盖率)** | ✅ |
| GitHub Actions CI (3 Python + Node 20 + Docker) | ✅ |

## API 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/stocks/search?q=` | 股票搜索 |
| GET | `/api/v1/stocks/{code}/quote` | 实时行情 |
| GET | `/api/v1/stocks/{code}/history?period=daily\|weekly\|monthly&days=N` | K线历史 |
| GET | `/api/v1/stocks/{code}/indicators/{MACD\|BOLL\|KDJ}?period=...&days=...` | 技术指标 |
| POST | `/api/v1/analysis/peak-trough` | 波峰波谷分析 |
| POST | `/api/v1/analysis/llm-insight` | LLM 解读 (限流 5/min/IP) |
| GET | `/api/v1/analysis/llm-status` | 当前 LLM provider 状态 |
| GET | `/health` | 健康检查 |
| GET | `/admin/stats` | 端点性能 + 缓存统计 |
| POST | `/admin/stats/reset` | 清零统计 |

## 测试

| 维度 | 数量 |
|---|---|
| 后端 pytest | 422 (1 skipped) |
| 前端 vitest | 260 |
| **总测试数** | **682** |
| 后端覆盖率 | **96%** |
| CI 门槛 | 70% (fail-under) |

运行测试:
```bash
# 后端
cd backend && python -m pytest --cov

# 前端
cd frontend && npm test            # 等价于 npx vitest run
cd frontend && npm run test:coverage
```

## 波峰波谷算法

1. 取本年度(最后一条数据的年份)的所有数据
2. 找到年度最高点
3. 从最高点之后开始, 用滑动窗口 (默认 k=3) 识别局部极大/极小
4. 相邻极值计算回撤 (`peak → trough`) 或回弹 (`trough → peak`) 比例
5. 末尾补足"到最后交易日"的段落, 给出 summary

> **iter 164**: 滑动窗口覆盖到数据末尾 — 之前末尾 3 根 bar 不参与极值检测, 真正的低点/高点
> 若落在末尾窗口内会被漏掉 (e.g. 昊华科技 600378 2026-08-03 low=37.5 被漏, 波谷误判为
> 更早的 38.56, 而 08-04/08-05 已反弹到 44)。现用 `min_periods=1` 让末尾 bar 也能检测,
> 并修复末根已是极值时重复添加零长度 segment 的问题。

## 配置

### 后端 (.env)

```bash
# 兜底 LLM key, CC switch 不可用时使用
# 仅以下两个变量会被代码读取
ANTHROPIC_API_KEY=
OPENAI_API_KEY=

BACKEND_PORT=8100
BACKEND_HOST=127.0.0.1

# 管理端点鉴权 (iter 65 文档同步):
#   - 配置后强制要求 ``Authorization: Bearer <key>``
#   - 受保护: ``/api/v1/analysis/llm-insight``, ``/api/v1/analysis/llm-status``,
#            ``/admin/stats``, ``/admin/stats/reset``
#   - 留空时本地回环地址放行 (开发友好); 部署到非 loopback 必须配置
ADMIN_API_KEY=

# 可选: 日志级别 (默认 INFO), LLM 配置文件位置 (默认 C:\Users\jww\.cc-switch)
LOG_LEVEL=INFO
CC_SWITCH_DIR=
```

### CC switch 集成

应用启动时会自动读取:
- `C:\Users\jww\.cc-switch\settings.json` 中 `currentProviderClaude` 字段
- `C:\Users\jww\.cc-switch\cc-switch.db` 中对应 provider 的 `settings_config`

支持 `env` 字段包含的 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL`。

## 参考

参考了 `参考文件/daily_stock_analysis_v2` 的:
- API schema 契约 (StockQuote / KLineData / StockHistoryResponse)
- LiteLLM 多 provider 调用模式
- DataFetcherManager market routing 思路

但本应用为独立实现, 简化了 provider 优先级 (akshare → yfinance), 补全了 weekly/monthly 周期支持, 并新增波峰波谷分析能力。