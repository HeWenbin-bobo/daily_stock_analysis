# 项目协作指南

治理规则以 `AGENTS.md` 为准（single source of truth），本文件记录本会话实践中发现的环境要点与工作习惯。

## 环境要点（本机实测）

- **Windows 11 + bash shell**：路径用正斜杠或加引号；`F:\` 盘符在 bash 里写作 `/f/` 或 `F:/`。
- **当前目录不是 git 仓库**：`.github/instructions/` 治理文档中的 git 流程（fetch/pull/PR）暂不适用，不要盲目执行 git 命令。
- **PreToolUse 钩子会注入治理规则**：访问 `CLAUDE.md` 时自动加载 `.github/instructions/governance.instructions.md`，修改前先读其内容。
- **长操作后台化**：`npm ci`、`npm run build`、测试等用 `run_in_background`；独立的多个 Bash 调用放在同一消息并行执行。
- **`.remember/` 目录**：会话记忆（now.md / today-*.md / recent.md 等），按需检索，不在此展开。

## 目录边界

- 后端：`src/`、`data_provider/`、`api/`、`bot/`
- Web：`apps/dsa-web/`
- 桌面：`apps/dsa-desktop/`
- 部署/工作流：`scripts/`、`.github/workflows/`、`docker/`

## 验证命令

- 后端改动：至少对改动的 Python 文件跑 `python -m py_compile`。
- Web 改动：`cd apps/dsa-web && npm ci && npm run lint && npm run build`。
- 桌面改动：先构建 web，再构建 desktop。

## 可视化约定（A股习惯）

- **涨 = 红色，跌 = 绿色**（与美欧相反），除非用户明确要求。
- 货币符号默认 `¥`（人民币）。
- 根目录的 `iter*_*.png`、`round5_*.png` 是迭代验证的可视化输出，属正常产物，勿当垃圾清理。

## 其他规则

- 不硬编码密钥、账户、端口、模型名、绝对路径。
- 复用现有模块/脚本/测试，不新增平行实现。
- 用户可见行为、CLI/API、部署、通知、报告结构变更 → 更新 `docs/CHANGELOG.md`（`[Unreleased]` 平铺格式：`- [type] 描述`）。
- 详细行为文档放 `docs/*.md`，`README.md` 只放定位、快速开始、入口。
- 不经用户明确确认不执行 `git commit` / `git tag` / `git push`（若未来启用 git）。

## update_version_v4 专项（2026-09 实测）

- **启动**：`cd update_version_v4/backend && python app.py`（端口 **8002**，README 写 8001 已过时）；前端 `cd update_version_v4/frontend && npm run dev`（端口 5174，vite 代理 `/api` → 8002）。
- **api 包遮蔽机制**：v4 的 `backend/api` 与主项目根 `api/` 同名。主项目模块加载统一走 `endpoints_hot._import_main_hot_analysis()`（快照/恢复 v4 的 `api.*`、主项目根强制提到 `sys.path[0]`），**不要**在别处手工做 sys.modules 手术——加载后必须恢复快照，否则主项目 api 包永久占坑，后续 `import api.endpoints_v2` 必炸。
- **测试约束（backend/tests/conftest.py v4 语义）**：绝不动 `sys.modules["api"]`——app 绑定的模块对象必须与测试共享单例，清了会导致 `_MAIN_HOT_MOD`/`_tasks` 状态分裂（症状：SSE 测试 404）。只重排 sys.path + 恢复 `cron.APS_AVAILABLE`。
- **测试 mock 泄漏教训**：脚本式测试（如 test_hyperopt_min.py）里 `ds.fetch_kline_with_fallback = lambda...` 不恢复，会污染字母序在其后的所有测试文件的 kline 断言（症状：期望 30/120 根却拿到 300 根合成 K 线、`is_mock` 属性丢失）。模块级状态赋值必须配恢复。
- **CC_SWITCH LLM 通道**：走本机代理 `http://127.0.0.1:15721`（glm-5.3-flash，**思考模型，无法禁用 thinking**）。思考块会耗尽 `max_tokens` 导致 text 块缺失（content 为空）→ llm_router 已做加倍重试；deep_research 用 16k 预算 + `LLM_TIMEOUT`（默认 300s，思考模型 ~190s）。超时/空响应自动降级 mock，日志看 `deep_research: LLM 输出未解析出 JSON`。
- **数据源**：akshare 东方财富接口夜间常 RemoteDisconnected，属已知外部问题；fallback 链（tencent/baostock）自动接管，不是 bug。
- **K线 period 参数**：`/api/v2/data/kline` 只接受 `daily|weekly|monthly|quarterly|yearly`（不是 `d`/`w`）。
- **agent-browser**：naive-ui 组件在 a11y 树里常不可见，用 `eval` JS 直点（`document.querySelectorAll('button')` 找文本）；Chrome 启动失败加 `--no-sandbox`。
- **pytest 全量**：`cd backend && python -m pytest tests/ -q --rootdir=.`，当前基线 **340 passed / 0 failed**；跑的时候别并行开第二个 pytest（SQLite WAL 文件锁会互相炸出 PermissionError）。
- **版本复制的修复继承审计（第二轮 /loop 经验）**：v3/v4 复制自 v2 时不是所有修复都跟随复制——已发现唯一丢失项：v2 Round 53 的 `alerts_engine.validate_rule_params`（非法数值 params 入库校验），v3/v4 均缺（v4 已在 2026-09-04 补回，v3 已退役但若复活需补）。**建新版本（v5）或复活旧版本时，先做一次旧版 vs 新版 `★ 修复` 标记的全文件 diff**：`diff <(grep -o '★ [^：:]*' 旧版/*.py | sort -u) <(...新版...)`，再对差异项逐一 API 实测确认。注意修复标记命名会跨版本漂移（同一修复 v2 标"（第五轮）修复"、v4 标"迭代4 修复"），文本 diff 会有误报，最终以实测行为为准。
