"""全局配置 helper。"""
from __future__ import annotations

import os


def is_force_mock() -> bool:
    """FORCE_MOCK 开关: '1'/'true'/'yes' 视为启用 (case-insensitive, strip 空白)。

    iter 69 复盘: 改为 module-level 缓存会让 monkeypatch.setenv 在测试中失效,
    而 fixture autouse 又无法在 body 中途 reload。最终结论: 直接每次读 env —
    os.environ.get 是 dict 查表, 单次 ~50ns, 比起 fetch_history 的网络开销可忽略。

    iter 159 默认值: 从 ``"1"`` 改为 ``"0"`` — 借鉴 v2 daily_stock_analysis_v2 的
    设计: 启动默认尝试真实数据源, 失败时按 chain fallback (yfinance → Finnhub →
    AlphaVantage → mock_fallback), 用户不需要知道 FORCE_MOCK 这个开关就能用。
    离线开发时显式设 ``FORCE_MOCK=1`` 走 mock。
    """
    val = os.environ.get("FORCE_MOCK", "0").strip().lower()
    return val in ("1", "true", "yes")
