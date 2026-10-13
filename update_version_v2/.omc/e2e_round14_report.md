# Round 14 E2E 报告（2026-08-05）

## 环境
- 前端 http://127.0.0.1:5173（vite dev，PID 21624）
- 后端 http://127.0.0.1:8001（uvicorn，PID 7408 → 修复后重启）
- agent-browser 0.30.1 + 系统 Chrome（`AGENT_BROWSER_EXECUTABLE_PATH` + `--no-sandbox`）
- 网络可用：akshare 实时 + LLM（anthropic/MiniMax-M3）真实调用

## 一、市场分析（四榜交集）
1. 配置改为「1只」+「1轮」（控制时长）
2. 预览：4 榜抓取 69s（东财 100 / 雪球 5617 / 同花顺降级雪球deals / 全市场活跃），交集 1 只
3. 确认 → SSE async 分析 → **完成**：1 只通过（601138 工业富联，169.65s）
4. ✅ PDF 导出（新端点，无错误）
5. ✅ 历史 tab 出现 #72（async 落库修复验证）
- 截图：r14_01~r14_03

## 二、单股分析（并发）
1. 输入 600519, 000858 → 并发分析（concurrency 3）
2. 两股并行，各走完整 agent pipeline（真实 LLM）
3. ✅ 600519：72 分 / 持有 / 第2轮 看多（MiniMax-M3）；000858：持有
4. ✅ 清空已完成按钮可用
- 截图：r14_05~r14_08

## 三、大盘复盘
1. ✅ 生成今日复盘 2026-08-05（18.99s）：盘面信号 67.29%，指数结构，行业板块 84 个，领涨/领跌 Top5
2. ✅ .md 导出 200（curl 验证返回 markdown）
3. ✅ 对比 2026-08-03 vs 2026-08-05（GET 修复后）：120 个字段 diff，前端弹窗展示
- 截图：r14_09~r14_10

## 四、可视化
1. ✅ 日 K → 周 K 周期切换（周K 聚合渲染）
2. ✅ 热力图（treemap，红涨绿跌）
3. ✅ 资金流向（donut：主力/散户净流入）
- 截图：r14_12~r14_15

## 附加验证
- ✅ 自选股：添加 300750 → 移除（正则校验生效）
- ✅ 模板：新建 E2E测试模板 → 应用（跳回分析视图）
- ✅ 定时任务：新建 E2E定时任务 → 触发返回 `{"started":9}`（修复前 500）
- ✅ 历史 tab：60 条记录加载，详情/对比/删除按钮齐全

## Console 错误
- 每个 tab 检查均 **0 error / 0 uncaught**

## 已知限制（非产品 bug）
- 对比 tab 的 naive-ui NSelect 虚拟列表下拉，agent-browser 点击被 `v-vl-items` 遮挡；键盘导航可部分操作。compare 端点已在 API 层用真实 run_id 验证返回正确结构化 diff

## 测试
19 套件全绿（含新增 test_phase19_round14_fixes.py 11 项）：
```
test_phase1..18 + test_phase19_round14 + test_audit_round1 → ALL PASSED, 0 FAILED
```
