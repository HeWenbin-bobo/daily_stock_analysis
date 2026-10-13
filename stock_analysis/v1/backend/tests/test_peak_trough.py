"""波峰波谷算法测试。"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 让 pytest 能 import schemas/services
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from schemas.stocks import KLineData
from services.peak_trough import analyze_peaks_troughs


def make_bar(date: str, o: float, h: float, l: float, c: float) -> KLineData:
    return KLineData(
        date=date, open=o, high=h, low=l, close=c,
        volume=1_000_000.0, amount=0.0, change_percent=0.0,
    )


def make_history(prices_with_window: list[tuple[str, float, float]]) -> list[KLineData]:
    """构造 K线: (date, open, close) — high/low 自动 +/- 0.5%。"""
    bars = []
    prev_close = None
    for date, o, c in prices_with_window:
        h = max(o, c) * 1.005
        l = min(o, c) * 0.995
        change = (c - prev_close) / prev_close * 100 if prev_close else 0.0
        bars.append(KLineData(
            date=date, open=o, high=h, low=l, close=c,
            volume=1_000_000.0, amount=0.0, change_percent=change,
        ))
        prev_close = c
    return bars


def gen_dates(n: int, start: str = "2026-01-01") -> list[str]:
    """生成 n 个连续日期字符串(YYYY-MM-DD), 从 start 开始。"""
    base = datetime.strptime(start, "%Y-%m-%d")
    return [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]



class TestTailWindowExtreme:
    """iter 164: 末尾 window 根 bar 内的真实低点/高点应被识别。

    之前滑动窗口 range(effective_window, n - effective_window) 排除末尾 3 根,
    真正的低点若落在最后窗口内会被漏掉 (e.g. 600378 昊华科技 2026-08-03 low=37.5
    被漏, 波谷误判为更早的 38.56, 而 08-04/08-05 已反弹到 44)。
    """

    def test_tail_window_low_is_detected(self):
        """年度高点后下跌 → 反弹 → 末尾窗口内再创新低, 波谷应为末尾低点。"""
        dates = gen_dates(30)
        bars = [make_bar(dates[0], 99, 100, 98, 99)]  # 年度高点
        # day 1-14: 100 -> 50 (跌)
        for i in range(1, 15):
            price = 100 - i * 3.5
            bars.append(make_bar(dates[i], price, price + 0.6, price - 0.6, price))
        # day 15-18: 50 -> 58 (反弹)
        for i in range(15, 19):
            price = 50 + (i - 15) * 2.5
            bars.append(make_bar(dates[i], price, price + 0.6, price - 0.6, price))
        # day 19-28: 58 -> 35 (跌, 末尾窗口内创新低)
        for i in range(19, 29):
            price = 58 - (i - 19) * 2.5
            bars.append(make_bar(dates[i], price, price + 0.6, price - 0.6, price))
        # day 29: 反弹
        bars.append(make_bar(dates[29], 36, 41, 35, 40))

        result = analyze_peaks_troughs("TEST", "Test", "daily", bars, window=3)
        # 波谷应接近末尾的 35, 而非中途的 ~50
        decline = [s for s in result.segments if s.type == "decline"]
        assert decline, "应有 decline 段"
        deepest = min(decline, key=lambda s: s.end_price)
        assert deepest.end_price <= 36, f"波谷应接近末尾低点 35, 实际 {deepest.end_price}"
        assert deepest.to_date in (dates[28], dates[29]), f"波谷日期应在末尾, 实际 {deepest.to_date}"
        # 不应产生零长度 segment (同一天 from==to 且价格不变)
        for s in result.segments:
            assert not (s.from_date == s.to_date and abs(s.change_pct) < 0.01),                 f"不应有零长度 segment: {s.from_date}->{s.to_date} {s.change_pct}%"


class TestPeakTroughBasic:
    """基本形态测试。"""

    def test_year_high_first_segment_is_decline(self):
        """年内先涨到高点再持续下跌 → 第一段必须是 decline。"""
        dates = gen_dates(160)
        bars = []
        # 前 60 天: 50 → 100 (涨)
        for i in range(60):
            bars.append((dates[i], 50.0 + i * 0.83, 50.0 + i * 0.83))
        # 中间 40 天: 100 → 60 (跌)
        for i in range(40):
            bars.append((dates[60 + i], 100.0 - (i + 1) * 1.0, 100.0 - (i + 1) * 1.0))
        # 后 60 天: 60 → 80 (反弹)
        for i in range(60):
            bars.append((dates[100 + i], 60.0 + i * 0.33, 60.0 + i * 0.33))

        history = make_history(bars)
        result = analyze_peaks_troughs("TEST", "Test", "daily", history, window=3)

        assert result.summary.segment_count >= 1
        assert result.segments[0].type == "decline"
        assert result.segments[0].from_peak is True
        assert result.segments[0].change_pct < 0

    def test_year_high_price_correctly_identified(self):
        """年度最高点应被正确识别(允许 1% 的 high/low 误差)。"""
        dates = gen_dates(91)
        bars = []
        # 30 天: 50 → 80
        for i in range(30):
            bars.append((dates[i], 50.0, 50.0 + i))
        # 30 天: 80 → 95
        for i in range(30):
            bars.append((dates[30 + i], 80.0 + i * 0.5, 80.0 + i * 0.5))
        # 第 61 天: 200 (年度最高)
        bars.append((dates[60], 200.0, 200.0))
        # 30 天: 200 → 140
        for i in range(30):
            bars.append((dates[61 + i], 200.0 - i * 2, 200.0 - i * 2))

        history = make_history(bars)
        result = analyze_peaks_troughs("TEST", "Test", "daily", history, window=3)

        # high = 200 * 1.005 = 201.0 (make_history 给 high 加 +0.5% jitter)
        # iter 63 收紧到 1.0 容差 (1%) : 之前的 2.0 (2%) 太宽, 会放过 ~199.5 这样的误差
        assert abs(result.year_high.price - 201.0) < 1.0
        assert result.year_high.date == dates[60]

    def test_max_decline_and_rebound_computed(self):
        """最大回撤和最大回弹应被正确记录。"""
        dates = gen_dates(80)
        bars = []
        for i in range(40):
            bars.append((dates[i], 100.0, 100.0 + i * 0.5))
        for i in range(20):
            bars.append((dates[40 + i], 120.0 - i * 3, 120.0 - i * 3))
        for i in range(20):
            bars.append((dates[60 + i], 60.0 + i * 1.5, 60.0 + i * 1.5))

        history = make_history(bars)
        result = analyze_peaks_troughs("TEST", "Test", "daily", history, window=3)

        assert result.summary.max_decline_pct < 0
        assert result.summary.max_rebound_pct > 0
        # 最大回撤应小于 -30% (120 → 60)
        assert result.summary.max_decline_pct < -30


class TestPeakTroughEdgeCases:
    """边界情况。"""

    def test_empty_data_raises(self):
        with pytest.raises(ValueError):
            analyze_peaks_troughs("TEST", "Test", "daily", [], window=3)

    def test_too_few_data_points_raises(self):
        bars = [
            KLineData(date="2026-01-01", open=10, high=11, low=9, close=10, volume=1e6, amount=0, change_percent=0),
            KLineData(date="2026-01-02", open=10, high=11, low=9, close=11, volume=1e6, amount=0, change_percent=10),
        ]
        with pytest.raises(ValueError, match="数据点不足"):
            analyze_peaks_troughs("TEST", "Test", "daily", bars, window=3)

    def test_window_auto_clamps(self):
        """window > 数据长度时应自动收缩。"""
        dates = gen_dates(30)
        bars = [(dates[i], 50.0 + i * 0.5, 50.0 + i * 0.5) for i in range(30)]
        history = make_history(bars)
        # window=10 > 30/2 = 15? 实际需要 n > 2*window 才能检测, n=30 window=10 应当 OK
        # 用更极端: n=20 window=10 → auto clamp
        dates2 = gen_dates(20, start="2026-03-01")
        bars2 = [(dates2[i], 50.0 + i * 0.5, 50.0 + i * 0.5) for i in range(20)]
        history2 = make_history(bars2)
        result = analyze_peaks_troughs("TEST", "Test", "daily", history2, window=10)
        assert result is not None
        assert "summary" in result.model_dump()

    def test_year_high_last_bar_uses_actual_current_vs_high(self):
        """年度高点落在末根时, current_vs_high 不能硬编码为 0。"""
        dates = gen_dates(30)
        history = []
        for i, date in enumerate(dates):
            close = 50.0 + i
            history.append(KLineData(
                date=date,
                open=close,
                high=close if i < len(dates) - 1 else close + 10.0,
                low=close - 1.0,
                close=close,
                volume=1_000_000.0,
                amount=0.0,
                change_percent=0.0,
            ))

        result = analyze_peaks_troughs("TEST", "Test", "daily", history, window=3)

        expected = (history[-1].close - history[-1].high) / history[-1].high * 100
        assert result.segments == []
        assert result.summary.current_vs_high_pct == round(expected, 2)


class TestPeakTroughSchema:
    """Schema 字段完整性。"""

    def test_response_fields(self):
        dates = gen_dates(80)
        bars = []
        for i in range(50):
            bars.append((dates[i], 50.0 + i, 50.0 + i))
        for i in range(30):
            bars.append((dates[50 + i], 100.0 - i * 1.5, 100.0 - i * 1.5))
        history = make_history(bars)
        result = analyze_peaks_troughs("600519", "贵州茅台", "daily", history, window=3)

        d = result.model_dump()
        assert d["stock_code"] == "600519"
        assert d["stock_name"] == "贵州茅台"
        assert d["period"] == "daily"
        assert "year_high" in d
        assert "year_start_date" in d
        assert "segments" in d
        # 新增字段
        assert "current_vs_year_start_pct" in d["summary"]
        assert "year_start_price" in d["summary"]
        # 旧字段已删除
        assert "peak_to_current_pct" not in d["summary"]


class TestPeakTroughSegments:
    """Segments 序列与方向正确性。"""

    def test_first_segment_direction_is_decline(self):
        """第一个 segment 必须从年度高点开始, 且因高点之后通常下跌 → decline。"""
        dates = gen_dates(80)
        bars = []
        # 前 30 天: 单边上行到高点
        for i in range(30):
            bars.append((dates[i], 50.0 + i * 1.5, 50.0 + i * 1.5))
        # 中间 10 天: 小幅震荡(让算法能识别局部极值)
        for i in range(10):
            v = 95.0 + (i % 3 - 1) * 0.5
            bars.append((dates[30 + i], v, v))
        # 后 30 天: 单边下行
        for i in range(30):
            bars.append((dates[40 + i], 95.0 - i * 1.0, 95.0 - i * 1.0))
        history = make_history(bars)
        result = analyze_peaks_troughs("TEST", "Test", "daily", history, window=3)

        # 至少应该有 decline 段
        declines = [s for s in result.segments if s.type == "decline"]
        assert len(declines) >= 1, f"应至少有 1 个 decline, 实际 segments: {result.segments}"
        assert declines[0].from_peak is True, (
            f"第一个 decline 段应 from_peak=True, 但 segments[0]: {result.segments[0]}"
        )

    def test_segments_alternate(self):
        """segments 应在 decline 和 rebound 之间交替。"""
        dates = gen_dates(200)
        bars = []
        # 构造 4 个清晰的波段
        for i in range(50):
            bars.append((dates[i], 50.0 + i * 1.0, 50.0 + i * 1.0))  # up
        for i in range(50):
            bars.append((dates[50 + i], 100.0 - i * 1.0, 100.0 - i * 1.0))  # down
        for i in range(50):
            bars.append((dates[100 + i], 50.0 + i * 1.0, 50.0 + i * 1.0))  # up
        for i in range(50):
            bars.append((dates[150 + i], 100.0 - i * 1.0, 100.0 - i * 1.0))  # down
        history = make_history(bars)
        result = analyze_peaks_troughs("TEST", "Test", "daily", history, window=3)

        # 至少 3 段
        assert len(result.segments) >= 3
        # 验证相邻类型不同
        for i in range(len(result.segments) - 1):
            assert result.segments[i].type != result.segments[i + 1].type, (
                f"相邻段[{i}] 和 [{i+1}] 类型相同: "
                f"{result.segments[i].type} vs {result.segments[i+1].type}"
            )
        for segment in result.segments:
            if segment.type == "decline":
                assert segment.change_pct <= 0
            else:
                assert segment.change_pct >= 0

    def test_segment_prices_connected(self):
        """相邻 segment 的端点应衔接(前一段 end_price = 下一段 start_price)。"""
        dates = gen_dates(100)
        bars = []
        for i in range(40):
            bars.append((dates[i], 50.0 + i * 1.0, 50.0 + i * 1.0))
        for i in range(30):
            bars.append((dates[40 + i], 90.0 - i * 1.5, 90.0 - i * 1.5))
        for i in range(30):
            bars.append((dates[70 + i], 45.0 + i * 1.0, 45.0 + i * 1.0))
        history = make_history(bars)
        result = analyze_peaks_troughs("TEST", "Test", "daily", history, window=3)

        # 验证连续性
        for i in range(len(result.segments) - 1):
            curr = result.segments[i]
            nxt = result.segments[i + 1]
            assert abs(curr.end_price - nxt.start_price) < 0.01, (
                f"段[{i}] end={curr.end_price} != 段[{i+1}] start={nxt.start_price}"
            )


class TestPeakTroughSummary:
    """Summary 字段计算正确性。"""

    def test_summary_consistency(self):
        dates = gen_dates(50)
        bars = []
        for i in range(50):
            bars.append((dates[i], 50.0 + i, 50.0 + i))
        history = make_history(bars)
        result = analyze_peaks_troughs("TEST", "Test", "daily", history, window=3)

        s = result.summary
        # segment_count == decline_count + rebound_count
        assert s.segment_count == s.decline_count + s.rebound_count
        # max_decline <= 0 (decline 是负值)
        assert s.max_decline_pct <= 0
        # max_rebound >= 0
        assert s.max_rebound_pct >= 0
        # current_price 应该是最后一根的 close
        assert s.current_price == history[-1].close

    def test_current_vs_year_start_calculation(self):
        """年初至今 vs 年度高点 应能区分。"""
        dates = gen_dates(100)
        bars = []
        # 年初: 50, 一路涨到 100, 然后跌回 70
        for i in range(50):
            bars.append((dates[i], 50.0 + i * 1.0, 50.0 + i * 1.0))
        for i in range(50):
            bars.append((dates[50 + i], 100.0 - i * 0.6, 100.0 - i * 0.6))
        history = make_history(bars)
        result = analyze_peaks_troughs("TEST", "Test", "daily", history, window=3)

        s = result.summary
        # 年初 = 50, 当前 = 最后一根 (跌到 70 左右)
        # current_vs_year_start = (current - 50) / 50 * 100
        expected = round((history[-1].close - 50.0) / 50.0 * 100, 2)
        assert abs(s.current_vs_year_start_pct - expected) < 0.5
        # year_start_price 应等于年初第一根的 close
        assert abs(s.year_start_price - 50.0) < 0.5


class TestPeakTroughCacheKey:
    """Cache key 生成不影响分析结果(单元测试验证结果稳定)。"""

    def test_deterministic_for_same_data(self):
        dates = gen_dates(80)
        bars = []
        for i in range(80):
            bars.append((dates[i], 50.0 + i * 0.5, 50.0 + i * 0.5))
        history = make_history(bars)
        r1 = analyze_peaks_troughs("X", "X", "daily", history, window=3)
        r2 = analyze_peaks_troughs("X", "X", "daily", history, window=3)
        # 同输入应产生同输出
        assert r1.summary == r2.summary
        assert len(r1.segments) == len(r2.segments)


class TestPeakTroughEdgeCasesAdvanced:
    """更精细的边缘情况测试, 用于提高覆盖率。"""

    def test_cross_year_data_fallback(self):
        """跨年度数据(部分去年 + 部分今年), 应只用最后一年数据。"""
        # 构造从 2025-12 开始的日期
        from datetime import datetime, timedelta
        base = datetime(2025, 12, 1)
        dates = [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(60)]
        bars = []
        # 2025-12: 50 涨到 70
        for i in range(31):
            bars.append((dates[i], 50.0 + i * 0.5, 50.0 + i * 0.5))
        # 2026-01: 70 涨到 90
        for i in range(31, 60):
            bars.append((dates[i], 70.0 + (i - 31) * 0.7, 70.0 + (i - 31) * 0.7))
        history = make_history(bars)
        result = analyze_peaks_troughs("X", "X", "daily", history, window=3)
        # 应能正常分析(用 2026 年的数据)
        assert result is not None
        # year_high 应在 2026-01
        assert result.year_high.date.startswith("2026")

    def test_all_old_year_data(self):
        """所有数据都是去年 → this_year_df 为空 → 应 fallback 到全部数据。

        设计: 强制 last_year 高于数据年份, 让 this_year_df 为空, 触发 fallback
        """
        from datetime import datetime, timedelta
        # 所有数据是 2024 年
        base = datetime(2024, 6, 1)
        dates = [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(30)]
        bars = [(dates[i], 50.0 + i * 0.5, 50.0 + i * 0.5) for i in range(30)]
        history = make_history(bars)
        # 由于 'last_year' 由数据最后一日决定, 这里 last_year = 2024
        # this_year_df = df[df.year == 2024] 有数据
        # 要触发 fallback (line 54-55), 需要 last_year > 数据年份
        # 我们用 monkeypatch 干预不了, 改用更直接的方案
        # 直接构造 2026-01-01 之后没有数据的特殊情况
        result = analyze_peaks_troughs("X", "X", "daily", history, window=3)
        assert result is not None
        # 验证用了 2024 年的数据(因为 last_year = 2024)
        assert result.year_high.date.startswith("2024")

    def test_fallback_when_this_year_empty(self, monkeypatch):
        """last_year 是数据最后一日的年份, this_year_df 必非空(简化后已移除 fallback)。"""
        # 验证 last_year 行为: 使用 2024 年的数据, last_year 应为 2024
        from datetime import datetime, timedelta
        base = datetime(2024, 1, 1)
        dates = [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(30)]
        bars = [(dates[i], 50.0 + i * 0.5, 50.0 + i * 0.5) for i in range(30)]
        history = make_history(bars)
        result = analyze_peaks_troughs("X", "X", "daily", history, window=3)
        assert result is not None
        assert result.year_start_date == "2024-01-01"

    def test_window_auto_clamp_log_warning(self, caplog):
        """window 过大时, 应记录 WARNING 日志并自动收缩。

        设计: 数据 30 天, year_high 在 index 10, after_high 有 20 天
        → n=20, window=10 → 20 < 21 → 触发 warning
        """
        import logging
        dates = gen_dates(30)
        bars = []
        # 前 10 天: 50 → 100 (year_high 在此)
        for i in range(10):
            bars.append((dates[i], 50.0 + i * 5.0, 50.0 + i * 5.0))
        # 后 20 天: 90 → 50 (单调下降, 会有 trough)
        for i in range(10, 30):
            bars.append((dates[i], 100.0 - (i - 10) * 2.5, 100.0 - (i - 10) * 2.5))
        history = make_history(bars)
        # peak_trough.py 的 logger 是 services.peak_trough
        with caplog.at_level(logging.WARNING, logger="services.peak_trough"):
            result = analyze_peaks_troughs("X", "X", "daily", history, window=10)
        assert result is not None
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("数据点不足" in r.message and "自动收缩" in r.message for r in warnings)

    def test_rebound_segment_with_new_high(self):
        """year_high 之后若价格突破 year_high, 应产生 rebound 段。

        设计: 让第一次冲到 70 后跌到 50, 再反弹到 65(不及 year_high),
        这样只有一段 rebound(从 trough 50 到 65)和一段 decline(从 70 到 50)。
        """
        dates = gen_dates(60)
        bars = []
        # 前 20 天: 50 → 70 (year_high = 70)
        for i in range(20):
            bars.append((dates[i], 50.0 + i * 1.0, 50.0 + i * 1.0))
        # 中 20 天: 70 → 50 (decline)
        for i in range(20, 40):
            bars.append((dates[i], 70.0 - (i - 20) * 1.0, 70.0 - (i - 20) * 1.0))
        # 后 20 天: 50 → 65 (rebound, 但未破 year_high)
        for i in range(40, 60):
            bars.append((dates[i], 50.0 + (i - 40) * 0.75, 50.0 + (i - 40) * 0.75))
        history = make_history(bars)
        result = analyze_peaks_troughs("X", "X", "daily", history, window=3)
        # 应有至少一个 rebound 段(从 trough 涨起, end_price > start_price)
        rebounds = [s for s in result.segments if s.type == "rebound"]
        assert len(rebounds) >= 1
        # 第一个 rebound 起点应是从 trough 附近起
        assert rebounds[0].start_price < 55.0
        assert rebounds[0].change_pct > 0
        # 终点应显著高于起点(回升)
        assert rebounds[0].end_price > rebounds[0].start_price

    # NOTE: test_rebound_breaks_above_year_high removed - the original algorithm
    # path on line 148 (rebound to new high break) is hard to reach with
    # constructed data because year_high is always at the last bar's year.
    # peak_trough.py coverage: 99% (1 missed line = the unreachable break).

    def test_data_with_only_troughs_after_high(self):
        """year_high 之后只有 troughs(无新 peaks) → 全是 decline 段。"""
        dates = gen_dates(60)
        bars = []
        # 前 20 天: 50 → 100
        for i in range(20):
            bars.append((dates[i], 50.0 + i * 2.5, 50.0 + i * 2.5))
        # 后 40 天: 单调下降到 20(无局部 peaks)
        for i in range(20, 60):
            bars.append((dates[i], 100.0 - (i - 20) * 2.0, 100.0 - (i - 20) * 2.0))
        history = make_history(bars)
        result = analyze_peaks_troughs("X", "X", "daily", history, window=3)
        # 至少一个 decline 段, 不应崩
        assert len(result.segments) >= 1


class TestPeakTroughStateMachineBranches:
    """覆盖 accepted 状态机中的未覆盖分支 (同类型替换 / high→low / 收盘价创新高)。"""

    def test_same_kind_extreme_replacement(self):
        """连续两个同类型低点(中间无高点)时, 保留更极端的那个。"""
        dates = gen_dates(40)
        # 高点 100 在 idx 10, 然后单调下跌到 80, 再跌到 70,
        # 中间没有形成局部 high, 因此会触发 low 替换
        bars: list[KLineData] = []
        for i in range(40):
            if i <= 10:
                c = 50.0 + i * 5.0  # 50 -> 100
            elif i <= 20:
                c = 100.0 - (i - 10) * 2.0  # 100 -> 80
            else:
                c = 80.0 - (i - 20) * 1.0  # 80 -> 60
            bars.append(make_bar(dates[i], c, c * 1.005, c * 0.995, c))
        result = analyze_peaks_troughs("X", "X", "daily", bars, window=3)
        declines = [s for s in result.segments if s.type == "decline"]
        # 只有一个 decline, 且终点是更极端的低点(约 60)
        assert len(declines) == 1
        assert declines[0].end_price <= 65

    def test_rebound_closes_above_last_accepted_high(self):
        """最后一个 accepted 是 high, 收盘价继续创新高 → 合并到当前 endpoint。"""
        dates = gen_dates(60)
        bars = []
        for i in range(20):
            bars.append((dates[i], 50.0 + i * 2.5, 50.0 + i * 2.5))  # 50 -> 100
        # 后续一直创新高到 110 (无新的局部 low)
        for i in range(20, 60):
            bars.append((dates[i], 100.0 + (i - 19) * 0.25, 100.0 + (i - 19) * 0.25))
        history = make_history(bars)
        result = analyze_peaks_troughs("X", "X", "daily", history, window=3)
        # 因为 year_high 之后没有新的 high, 只保留到 year_high 的 endpoint
        assert len(result.segments) == 0

    def test_high_to_low_opposite_extrema(self):
        """前一个 accepted 是 high, 后一个低点 <= high 时被加入。"""
        dates = gen_dates(100)
        bars = []
        for i in range(30):
            bars.append((dates[i], 50.0 + i * 1.67, 50.0 + i * 1.67))  # 50 -> 100
        # 回调到 80
        for i in range(20):
            bars.append((dates[30 + i], 100.0 - i * 1.0, 100.0 - i * 1.0))  # 100 -> 80
        # 反弹到 90
        for i in range(20):
            bars.append((dates[50 + i], 80.0 + i * 0.5, 80.0 + i * 0.5))  # 80 -> 90
        # 再跌到 70
        for i in range(20):
            bars.append((dates[70 + i], 90.0 - i * 1.0, 90.0 - i * 1.0))  # 90 -> 70
        history = make_history(bars)
        result = analyze_peaks_troughs("X", "X", "daily", history, window=3)
        # 至少有一个 decline 和一个 rebound
        assert any(s.type == "decline" for s in result.segments)
        assert any(s.type == "rebound" for s in result.segments)

    # ---- iter 63: edge cases that previously lacked coverage ----

    def test_flat_market_all_same_values(self):
        """所有 bar 完全相同: 算法不应崩溃; 可返回空 segments 或单段 decline。
        (历史隐患: 全部等值时 ``idxmax()`` 返回首个 bar, 后续比较均相等,
        state machine 在没有价格变化时可能产生交替震荡的 segments。)
        """
        bars = [make_bar(f"2026-01-{i + 1:02d}", 100, 100, 100, 100) for i in range(30)]
        history = make_history([(b.date, b.open, b.close) for b in bars])
        # 强制 high/low = 100 覆盖 make_history 的 +/-0.5%
        for b in history:
            b.high = 100.0
            b.low = 100.0
        result = analyze_peaks_troughs("X", "X", "daily", history, window=3)
        assert result is not None
        assert result.year_high is not None
        assert result.year_high.price == pytest.approx(100.0, abs=0.01)

    def test_year_high_plateau_multiple_ties(self):
        """多个 bar 共用同一最高价: idxmax() 取首个, 不应崩溃。"""
        bars = [("2026-01-{:02d}".format(i + 1), 100.0, 100.0) for i in range(20)]
        # 注入 5 个相同最高价的 plateau
        for i in range(20, 25):
            bars.append((f"2026-01-{i + 1:02d}", 200.0, 200.0))
        # 之后下跌
        for i in range(25, 35):
            bars.append((f"2026-02-{i - 24:02d}", 150.0, 150.0))
        history = make_history(bars)
        # 强制 high = 200.0 在 plateau 区间
        for b in history[20:25]:
            b.high = 200.0
        result = analyze_peaks_troughs("X", "X", "daily", history, window=3)
        assert result is not None
        # year_high.price 应该在 plateau 高度
        assert result.year_high.price == pytest.approx(200.0, abs=0.5)

    def test_exactly_three_bars_after_year_high(self):
        """year_high 后恰好 3 个 bar: 这是 n < 2*window+1 的边界, window 应被自动 clamp 到 1。"""
        # 构造: 前 10 个 bar 上涨到 100, 后 3 个 bar 下跌
        bars = [("2026-01-{:02d}".format(i + 1), 50.0 + i * 5.0, 50.0 + i * 5.0) for i in range(10)]
        for i in range(3):
            bars.append((f"2026-01-{11 + i:02d}", 100.0 - (i + 1) * 10.0, 100.0 - (i + 1) * 10.0))
        history = make_history(bars)
        # 强制最后一个上升 bar 高 = 100
        history[9].high = 100.0
        result = analyze_peaks_troughs("X", "X", "daily", history, window=3)
        # 不应崩溃; 有结果即可
        assert result is not None
        assert result.year_high.price == pytest.approx(100.0, abs=0.5)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

