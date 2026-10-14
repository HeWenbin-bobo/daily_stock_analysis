# Loop 状态：5 轮串行迭代全部完成 ✅（2026-09-03 21:03 ~ 09-04 03:40）

## 最终战果

### 修复的 Bug（10 项）
| # | Bug | 修复 |
|---|-----|------|
| 1 | backend/db 缺 `__init__.py` → namespace package 被根目录同名包遮蔽 | 补 `__init__.py` |
| 2 | backend/services 同上 | 补 `__init__.py` |
| 3 | deep_research 60s 硬编码超时 | `LLM_TIMEOUT` 环境变量化（默认 300s） |
| 4 | 大盘复盘 PDF 静默降级 markdown（api 包遮蔽主项目命名空间） | 统一走 `_load_main_hot_analysis()` + fallback 缩窄 + error 日志 |
| 5 | deep_research 空报告三层根因：思考模型耗尽 max_tokens(4000) → text 块缺失 | 加倍重试 + max_tokens 16k + 300s 超时；验证完整报告（6 评分/观望/high/估值区间） |
| 6 | deep_research 静默空解析无日志 | 补 warning（content_len/head/tail），诊断链闭环 |
| 7 | pytest 顺序污染（21 failed） | conftest.py v4 语义（不动 sys.modules 保单例 + sys.path 重排 + APS 恢复） |
| 8 | test_llm_rate_limit flaky（真实 LLM 8s×10 > 60s 滑窗） | 测试内 mock llm_router.chat（87s → 2s） |
| 9 | 生产 loader 4 处健壮性缺陷（sys.path 已含主项目根但不在 [0] 时跳过重排 + 加载后不恢复 v4 api.* 快照占坑） | 重构统一 `_import_main_hot_analysis()`（快照→强制 [0]→清理→import→finally 恢复） |
| 10 | test_hyperopt_min mock 泄漏（`ds.fetch_kline_with_fallback = lambda` 不恢复）污染后续 kline 测试 | 保存 orig + 退出前恢复 |
| 11 | test_phase12 模块级 `APS_AVAILABLE=False` 收集期污染 phase1 | 导入完成后立即恢复 True |

### 测试战绩
- 完整套件：**337 passed / 0 failed**（起点 21 failed → 12 → 7 → 3 → 0）
- 前端 build：✅ vue-tsc + vite 24s

### E2E（agent-browser 全功能覆盖）
- 📊 四榜交集：✅ 完整 LLM 轮（64 步 agent，管线无恙）
- 🔍 单股：✅ 600519/000858/601318 三只全链路 + 历史落库
- 🗞 大盘复盘：✅ 生成/详情/历史/批量/对比 + 09-04 冒烟 ok
- 📈 可视化：✅ K线/热力图/资金流向 canvas 全渲染
- 📐 技术 / 📚 历史 / 🔄 对比 / ⭐ 自选增删 / 📋 模板新建 / ⏰ 定时新建+执行：✅
- 🧠 深度研究：✅ 完整四大师报告渲染
- ⚙️ 参数优化：✅ 建策略→LLM 编译 DSL→hyperopt→结果
- 💰 模拟盘：✅ 建户→下单→成交→持仓

### 经验沉淀
- 关键机制写入根目录 `CLAUDE.md`（api 包遮蔽、conftest v4 约束、CC_SWITCH 思考模型 token/超时预算、数据源夜间降级、kline period 枚举、pytest 全量命令）
- 详细过程存本文件（LOOP_STATE.md），后续迭代可直接续用

---

# 第二轮 /loop 状态（续：2026-09-04 10:00 ~ 11:00）

## 迭代1：未测 API 模块覆盖扫描（clean，无新 bug 修复）

**触发背景**：第一轮（11 项修复 + 337 全绿）完成后，会话中断于第二轮·迭代1 中途（60W318 调查 + API 模块覆盖刚开始）。本会话续跑完成该迭代。

### 边界探测（26 项，全部无 500）
- 12 项空 body POST（backtest/strategies、backtest/run、hyperopt、paper/accounts、paper/transfer、portfolio/holdings、portfolio/rebalance、alerts/rules、notifications/channels、community/shares、webhooks/sign、cron/user-jobs）→ 全部规范 400（missing_fields 清单明确）；webhooks/sign 在 APP_ADMIN_TOKEN 未配置时返回 503 且带清晰提示（预期行为）。
- 14 项深边界（不存在 ID/非法枚举/伪造 token）：strategies/999999 删除→401（X-User-Id 缺失）、hyperopt/runs/999999→404、paper/accounts/999999→404、community/shares/BADTOKEN→404、alerts/rules/999999→404、cron/trigger/999999→404、kline period=bad→422（pattern 校验）等，全部干净。

### 正向链路（12 项全通）
- 建模拟盘账户 4194 → 加组合持仓 600519×100（trade 4053）→ 建 volume_surge 规则（rule 1677）→ 建 serverchan 渠道（channel 1544，send_key 格式校验生效）→ 审计逐条记录（create_notification_channel success/error、add_holding、create_alert_rule 等）。
- 组合持仓会**惰性自动建「我的持仓」账户**（modules/portfolio.py 加仓时若无则 create_account）——列表 2 个账户但审计 1 次是设计行为，非缺陷。
- 社区：分享不存在 resource → 404 归属校验生效；webhooks/verify 伪造签名 → valid:false；alerts/scan 手动触发 → scanned 4 rules / 6 triggers / 0 errors。

### 资金/安全路径审查 + 实测（money 路径无回归）
- 代码审查：`transfer_between_accounts`（Round 22 TOCTOU 条件更新 + rowcount）、`place_order`（Round 5 price fail-soft + mock 排除、Round 20 T+1、Round 13 BEGIN/COMMIT、Round 22 BEGIN IMMEDIATE + 锁内重核）、`settle_daily`、`leaderboard`（除零兜底）、`community.create_share`（token 碰撞重试 + 私有分享 resolve 校验）——全部修复在位。
- 实测：转账 4194→4195 ¥1000（余额 999000/1001000 ✓）→ 买入 600519×10@1300（-13005 含 5 元佣金，余额 985995 ✓）→ 快照权益 985995+12988.8=998983.8 ✓。
- 负向护栏 5 项全对：超卖拒（持10卖100）、T+1 拒（当日买入当日卖 10 股 → 可卖 0）、超额转账拒（转 500 万 > 余额）、超买拒（需 1,300,390 > 985,995）、拒绝后余额分毫不差。
- 遗留标记扫描：data_source.py:34/38 的 NotImplementedError 是抽象基类（正确设计）；notifier.py:313 明文 target 是 Round 4 加的老数据兼容 fallback（迁移后可移除）——均非 bug。

### 服务健康
- 10:00 定时大盘复盘 cron 正常触发（run-log id 883，24s success）；09-04 复盘落库 ok（88.8/100，上涨 4577/下跌 813，真实数据）。
- 白天数据源 0 ERROR（夜间 RemoteDisconnected 属已知外部问题）。
- 60W318 结论：LLM agent 分析 601318 时自推的幻觉代码，被 7 级数据源链优雅拒绝 → 降级正确，非应用 bug。

### 验证
- 后端回归：**337 passed / 0 failed**（6:03）。
- 前端：vue-tsc 0 错误 + vite build 14.6s 成功（仅 chunk size 警告，预存）。

### 迭代1 结论
**无新 bug 修复。** 未测 API 模块（异动/组合/社区/通知/审计/webhook/cron）边界与正向均健壮，资金路径护栏全部生效。测试数据 u_iter1（账户 4194/4195、持仓、规则 1677、渠道 1544）保留，供后续迭代复用。

### 迭代2：服务层深挖 — 发现并修复 CRITICAL 回归

**触发**：迭代1 覆盖扫描全部 clean 后，进入服务层代码审查；比对 v2/v3/v4 的 `alerts_engine.py` 修复标记，发现 v2 Round 53 修过的 `validate_rule_params` 在 v3/v4 复制时**丢失**。

**CRITICAL 回归（v3/v4 复制时丢失 v2 Round 53 修复）**：
- 异动规则 `params` 非法数值未校验即入库：实测 `params={"multiplier":"abc"}` 创建 `volume_surge` 规则返回 200 入库（rule 1678），`params={"threshold_pct":"xyz"}` 创建 `opening_spike` 返回 200（rule 1679）。
- 后果链：扫描时 `float("abc")` 抛 `ValueError` 被 scan 循环吞掉 → 规则永不触发且用户无感知（只有 `/alerts/scan` 摘要的 errors 计数会涨，但用户看不到原因）。
- 根因：v4 从 v3 复制、v3 从 v2 复制时，`validate_rule_params` / `_PARAM_NUMERIC_FIELDS` / `create_rule` 入库前校验 / `update_rule` 校验均未继承（对比 v2/v3/v4 修复标记仅 `alerts_engine` 缺）。

**修复（最小改动，对齐 v2 验证过的实现）**：
- `modules/alerts_engine.py`：在 `RULE_TYPES` 之后新增 `_PARAM_NUMERIC_FIELDS`（8 种规则的数值字段清单）+ `validate_rule_params(rule_type, params)` 函数；`create_rule` 入库前调用；`update_rule` 在 `if "params" in fields` 分支中调用（rule_type 用新值或读库旧值）。
- 端点层（`api/endpoints_v2.py:241` create、`278` update）已有 `except ValueError → HTTPException(400)` 转换，引擎层抛错即可转 400，无需改端点。

**验证**：
- API 实测：非法 `multiplier:"abc"` 创建 → 400 `参数 multiplier 必须是数字`；非法 `period:-5` 创建 → 400；PUT 更新 params=非法 → 400；合法 `multiplier:2.5` 仍 200（rule 1680）。
- 端到端：`validate_rule_params` / `create_rule` 拒绝非法 / `update_rule` 拒绝非法 三个 unit 测试全过。
- 全量回归：**340 passed / 0 failed**（基线 337 + 新增 3），无回归。
- DB 存量扫描：`alert_rule` 表 4 条记录无非法 params（之前测试脏数据 rule 1678/1679/1680 已清理），修复前如有过脏数据也会在每次 `scan_all_alerts` 静默失败。

**交付物**：
- 修改文件：`update_version_v4/backend/modules/alerts_engine.py`
- 新增测试：`update_version_v4/backend/tests/test_phase24_round_rule_params.py`（3 用例，固化防回归）
- 清理：测试用规则 1678/1679/1680 已删

**同类风险扫描**：比对 v2/v3/v4 各模块 `★ 修复` 标记（alerts_engine / paper_trading / portfolio / community / backtest_engine / hyperopt_engine / cron / data_source / notifier），仅 `alerts_engine` 缺该修复，其余模块的 v2 关键修复（TOCTOU、share 归属、cron 调度、leaderboard 除零等）均已继承到 v4。

**经验沉淀**：v3/v4 从 v2 复制时，应建立"修复继承对照表"——下次 v5 创建或 v3 复活时先做一次 v2-vs-新版 ★ 标记 diff，避免同类修复丢失。本轮已在记忆索引中注明。

### 迭代3：复制修复继承全量审计（结论：仅误报，v4 修复继承完整）

**触发**：迭代2 修复 v4 alerts_engine `validate_rule_params` 后，担心同类"v3/v4 复制时丢失 v2 修复"风险扩散。

**审计范围**：v2 vs v4 backend 全 py 文件（含 api/ core/ db/ modules/ services/），提取所有 `★ 修复` 标记行，去编号后对比。

**全量 diff 结果**：仅 1 处 v2 标记 v4 未匹配——`api/endpoints_v2.py` 的「（第五轮）修复：pf.add_holding 对 cost/quantity <= 0 抛 ValueError」。

**核实结论（误报）**：v4 的 `add_portfolio_holding` 端点已实现相同逻辑（以「迭代4 修复」命名），仅是迭代周期标签不同。API 实测确认行为一致：
- `quantity=0` → 400 `cost 和 quantity 必须 > 0`（非 500）
- `cost=-100` → 400
- 合法持仓仍 200（trade 4057）

**结论**：v4 backend 修复继承完整。`validate_rule_params` 是 v3/v4 复制链路上**唯一**真正丢失的修复（已在迭代2 修复并固化回归测试）。v4 历史上积累了自身的修复链（v4-loop-iteration4 等），修复继承无其它遗漏。

**清理**：测试用 `000858` 持仓（trade 4057）已删；保留 `600519` 测试持仓供后续扫描/概览回归验证。


### 迭代4：计算引擎边界实测（clean，无新 bug）

**触发**：迭代3 确认修复继承完整后，转向未实锤的计算引擎（backtest / hyperopt / technical_analysis）——这些模块除零/空数据/全平盘路径容易藏 500。

**审查**：`_execute_backtest` 主循环（entry_price 除零防御、initial_capital>0 校验）、`_ma/_ema/_macd/_rsi/_volume_ratio`（RSI 平盘=50、量比 avg>0 防除零）、`_eval_condition`（macd None 预热期防护、ema/rsi 按需补算 Round 19 H3）、hyperopt loss（sharpe/sortino/multi_metric 无交易/无负收益均 MAX_LOSS 兜底防"从不亏"假最优）、KDJ spread `.clip(lower=epsilon)`、BOLL/MACD 极少数据、peaks/troughs 空数据 ValueError。

**边界实测 10 项（合成 K 线，不联网）**：
- 回测：空 K 线 IndexError——**入口 `len(kline) < 30` 拦截，不可达**（hyperopt 入口 `< 60` 拦截）；1 根/全平盘/空条件 DSL 均不炸；initial_capital=0 抛 ValueError；compare 不存在结果 → error dict。
- 技术分析：KDJ 全平盘（spread=0→epsilon）不炸且 series 长度正确；BOLL 3 根 / MACD 2 根 / peaks 全平盘 30 根均不炸；peaks 空数据抛 ValueError（设计内）。
- 两个"失败"均为测试自身问题（peaks 签名需 4 参 + 合成数据 1 月 32 号非法日期），修正后全过。

**结论**：三个计算引擎边界防护全部到位，**无新 bug**。_execute_backtest 空列表不可达，不加防御代码（避免为不可达路径增肥）。

### 迭代5：最终验证（clean）

- 服务健康：后端 8002 ready（db/cron ok）、前端 5174 200；重启后端后修复已加载。
- 链路冒烟：600519 日K（真实数据 1343.03）、技术指标 MACD（real series）、异动规则列表（rule 1677 正常，last_triggered 有值）、模拟盘 4194（重启后数据保留，cash 985995）。
- 全量回归：**340 passed / 0 failed**（4:59），代码自上一轮全绿后无改动，重复确认稳定。
- akshare failure_count 139 但 success 持续发生（11 次成功），是已知间歇性外部网络问题，fallback 链正常，非应用 bug。

---

# 第二轮 /loop 总结（2026-09-04，5 次迭代）

## 战果：修复 1 项 CRITICAL + 2 项审计确认 clean

| 迭代 | 内容 | 结果 |
|------|------|------|
| 1 | 未测 API 模块覆盖扫描（异动/组合/社区/通知/审计/webhook/cron）+ 资金护栏实测 | clean，26 边界探测 + 12 正向链路 + 5 负向护栏全对 |
| 2 | 服务层深挖 | **CRITICAL 修复**：v4 `validate_rule_params` 丢失（v3/v4 复制 v2 时遗漏 Round 53 修复），非法 params 入库 → 规则永不触发 |
| 3 | 复制修复继承全量审计（v2 vs v4 全 py ★ 标记 diff） | clean（1 处误报：add_holding 已在 v4 迭代4 实现） |
| 4 | 计算引擎边界实测（backtest/hyperopt/technical） | clean，10 项边界探测（2 项测试自身问题修正后全过） |
| 5 | 最终验证 | 340/0 全绿 + 链路冒烟 + 服务健康 |

## CRITICAL 修复详情
- `modules/alerts_engine.py`：移植 v2 的 `_PARAM_NUMERIC_FIELDS` + `validate_rule_params`，`create_rule`/`update_rule` 入库前校验。
- 验证：非法 `multiplier:"abc"`/`period:-5` 创建/更新 → 400；合法 200；3 用例回归测试；全量 340/0；DB 存量无脏数据。
- 回归测试：`backend/tests/test_phase24_round_rule_params.py`（3 用例）。

## 测试战绩
- 全量回归三次：337 → 340（+3 新用例）→ 340，全部 **0 failed**。
- 前端 vue-tsc 0 错误 + vite build 14.6s（迭代1 期间验证）。

## 经验沉淀（本轮新增）
- **版本复制的修复继承审计**：v3/v4 复制 v2 时不是所有修复都跟随复制（alerts_engine 的 validate_rule_params 是唯一丢失项）。建新版本时先做一次旧版 vs 新版 `★ 修复` 标记全文件 diff。
- **修复标记命名漂移**：同一修复在不同版本用不同迭代标签命名（v2"（第五轮）修复" vs v4"迭代4 修复"），文本 diff 会产生误报 → 用 API 实测确认行为，不只看注释。
- **计算引擎入口校验**：`_execute_backtest` 空列表 IndexError 不可达（run_backtest `<30`/hyperopt `<60` 入口拦截），不为此加防御代码。

## 待后续关注
- v3（已退役目录）同样缺 validate_rule_params，若复活需补。
- akshare 夜间间歇 RemoteDisconnected（外部，fallback 正常）。
