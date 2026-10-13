"""技术指标单元测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import math

import pytest

from schemas.stocks import KLineData
from services.indicators import (
    compute_boll,
    compute_indicator,
    compute_kdj,
    compute_macd,
)


def make_bars(prices: list[float], start_date: str = "2026-01-01") -> list[KLineData]:
    """从价格数组构造 K线 (用于测试)。"""
    from datetime import datetime, timedelta
    base = datetime.strptime(start_date, "%Y-%m-%d")
    bars = []
    for i, p in enumerate(prices):
        h = p * 1.01
        l = p * 0.99
        o = p * 1.005
        c = p
        bars.append(KLineData(
            date=(base + timedelta(days=i)).strftime("%Y-%m-%d"),
            open=round(o, 4), high=round(h, 4), low=round(l, 4), close=round(c, 4),
            volume=1e6, amount=0.0, change_percent=0.0,
        ))
    return bars


class TestMACD:
    """MACD 计算。"""

    def test_macd_series_length(self):
        prices = [10 + i * 0.1 for i in range(50)]  # 50 天缓慢上涨
        result = compute_macd(make_bars(prices))
        assert result["indicator"] == "MACD"
        assert result["params"]["fast"] == 12
        assert result["params"]["slow"] == 26
        assert result["params"]["signal"] == 9
        assert len(result["series"]["dif"]) == 50
        assert len(result["series"]["dea"]) == 50
        assert len(result["series"]["hist"]) == 50

    def test_macd_hist_is_dif_minus_dea(self):
        prices = [10 + i * 0.1 for i in range(50)]
        result = compute_macd(make_bars(prices))
        # 每根柱: HIST = (DIF - DEA) * 2
        for i in range(50):
            dif = result["series"]["dif"][i]["value"]
            dea = result["series"]["dea"][i]["value"]
            hist = result["series"]["hist"][i]["value"]
            assert abs(hist - (dif - dea) * 2) < 1e-3

    def test_macd_uptrend_dif_positive(self):
        """强上涨趋势中, DIF 应大于 0。"""
        prices = [10 + i * 1.0 for i in range(50)]
        result = compute_macd(make_bars(prices))
        # 后期的 DIF 应明显为正
        last_dif = result["series"]["dif"][-1]["value"]
        assert last_dif > 0


class TestBOLL:
    """布林带计算。"""

    def test_boll_three_lines(self):
        prices = [10 + i * 0.1 for i in range(50)]
        result = compute_boll(make_bars(prices))
        assert "mid" in result["series"]
        assert "upper" in result["series"]
        assert "lower" in result["series"]

    def test_boll_upper_above_lower(self):
        prices = [10 + (i % 10 - 5) * 0.1 for i in range(50)]  # 震荡
        result = compute_boll(make_bars(prices))
        # upper > mid > lower 在每个点都成立
        for i in range(min(50, len(result["series"]["upper"]))):
            u = result["series"]["upper"][i]["value"]
            m = result["series"]["mid"][i]["value"]
            l = result["series"]["lower"][i]["value"]
            assert u > m > l

    def test_boll_skip_warmup(self):
        """前 period 个数据应被跳过(NaN)。"""
        prices = [10 + i for i in range(50)]
        result = compute_boll(make_bars(prices), period=20)
        # 第一个有效 mid 索引 = period - 1 = 19
        assert result["series"]["mid"][0]["time"] == "2026-01-20"


class TestKDJ:
    """KDJ 计算。"""

    def test_kdj_initial_values(self):
        prices = [10] * 30  # 全部相同价格 → RSV = 50, K = D = 50
        result = compute_kdj(make_bars(prices))
        # 初始 K 和 D 都应是 50 (或非常接近)
        first_k = result["series"]["k"][0]["value"]
        first_d = result["series"]["d"][0]["value"]
        assert 49 <= first_k <= 51
        assert 49 <= first_d <= 51

    def test_kdj_three_lines(self):
        prices = [10 + i * 0.5 for i in range(30)]
        result = compute_kdj(make_bars(prices))
        assert "k" in result["series"]
        assert "d" in result["series"]
        assert "j" in result["series"]
        assert len(result["series"]["k"]) == 30

    def test_kdj_j_is_3k_minus_2d(self):
        prices = [10 + i * 0.5 for i in range(30)]
        result = compute_kdj(make_bars(prices))
        for i in range(30):
            k = result["series"]["k"][i]["value"]
            d = result["series"]["d"][i]["value"]
            j = result["series"]["j"][i]["value"]
            assert abs(j - (3 * k - 2 * d)) < 1e-3


class TestIndicatorDispatch:
    """compute_indicator 入口。"""

    @pytest.mark.parametrize("indicator", ["MACD", "BOLL", "KDJ"])
    def test_dispatch(self, indicator: str):
        prices = [10 + i for i in range(50)]
        result = compute_indicator(make_bars(prices), indicator)
        assert result["indicator"] == indicator

    def test_invalid_indicator(self):
        prices = [10 + i for i in range(50)]
        with pytest.raises(ValueError, match="不支持的指标"):
            compute_indicator(make_bars(prices), "RSI")

    def test_dispatch_case_insensitive(self):
        """指标名大小写不敏感, 统一大写归一化。"""
        prices = [10 + i for i in range(50)]
        for variant in ("macd", "Macd", "MACD", "macD"):
            result = compute_indicator(make_bars(prices), variant)
            assert result["indicator"] == "MACD"

    def test_macd_empty_data(self):
        """空数据应抛 ValueError 而非 500。"""
        with pytest.raises(ValueError):
            compute_macd([])

    def test_boll_insufficient_data(self):
        """数据点少于 BOLL period 时应返回部分结果或抛错, 不应崩。"""
        prices = [10, 11, 12]
        # 不论实现如何, 都不应抛未捕获异常
        try:
            result = compute_boll(make_bars(prices), period=20)
            # 若返回, series 长度 <= 输入长度
            assert len(result["series"]["mid"]) <= 3
        except ValueError:
            pass  # 抛错也可接受, 不接受 500

    def test_kdj_flat_price_no_division_by_zero(self):
        """所有价格相同时, KDJ 不应出现 NaN/inf。"""
        import math
        prices = [42.0] * 30
        result = compute_kdj(make_bars(prices))
        for series_name in ("k", "d", "j"):
            for point in result["series"][series_name]:
                v = point["value"]
                assert math.isfinite(v), f"{series_name} 含非有限值: {v}"
