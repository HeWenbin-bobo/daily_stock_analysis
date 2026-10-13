---
name: stock-analysis-v1-iteration-164-peak-trough-tail-window
description: 修复波峰波谷漏掉末尾窗口内的真实低点 (600378 昊华科技 37.5 被漏)
metadata:
  type: project
---

# iter 164 — 波峰波谷末尾窗口低点修复

## 用户报告的问题
"昊华科技近期最低应该是37块多, 但程序将38块多的某一天数据当成波谷来计算"

## 根因
`services/peak_trough.py` 滑动窗口极值检测:
```python
for i in range(effective_window, n - effective_window):
```
**排除最后 effective_window (3) 根 bar 的极值检测**。600378 昊华科技 2026-08-03 (low=37.5)
正好是倒数第 3 根, 被跳过; 结尾逻辑又只用最后一根 (08-05) 的 close=44.07, 导致 37.5
这个真实低点完全漏掉。程序波谷误判为 07-21 的 38.56, 而 08-04/08-05 已反弹到 44。

真实数据 (tencent):
- 07-21 low=38.56 (程序误判的波谷)
- 07-30 low=39.09, 07-31 low=39.78 (中途反弹)
- **08-03 low=37.5** (真正最低, 被漏)
- 08-04 low=37.82, 08-05 close=44.07 (大幅反弹)

## 修复 (2 处)
1. **滑动窗口覆盖末尾**: `range(effective_window, n)` — rolling(min_periods=1) 对末尾 bar
   只用左侧窗口比较 (右侧无数据不参与), 末尾窗口内的真实低点/高点也能被识别
2. **结尾逻辑防重复**: `current_idx == previous_idx` (末根已是滑动窗口识别的极值) 时不再
   重复添加 — 之前末根被识别为 high 会再 append 同 idx 的 low, 产生零长度 segment

## 修复后结果 (600378)
- decline: 07-02 → **08-03**, 88.1 → **37.5**, -57.43% ✓
- rebound: 08-03 → 08-05, 37.5 → 44.07, 17.52% ✓
- 无零长度段 ✓

## 测试
- 新增 `TestTailWindowExtreme.test_tail_window_low_is_detected` (+1): 年度高点后下跌→反弹→
  末尾窗口内创新低, 波谷应为末尾低点, 且无零长度 segment
- Backend: 485 → **486 passed**
- 26 个 peak_trough 测试全过 (无破坏)

## 文件
- 修改: `backend/services/peak_trough.py` (滑动窗口 range + 结尾逻辑)
- 修改: `backend/tests/test_peak_trough.py` (+1 回归测试)
- 修改: `README.md` (波峰波谷算法说明)

## 关键点
- 滑动窗口找局部极值需要"两侧窗口", 末尾 bar 天然缺右侧数据 — 但 min_periods=1
  rolling 用左侧窗口比较即可, 不应整个跳过末尾窗口
- 结尾逻辑 (最后一根 close 作为当前价) 无法表达"末尾窗口内比 close 更极端的 low/high",
  所以必须让滑动窗口覆盖末尾

## 相关链接
- [[stock-analysis-v1-iteration-163-full-ashare-search]] — 上 iter, A股全量搜索
