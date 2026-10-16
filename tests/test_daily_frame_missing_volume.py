# -*- coding: utf-8 -*-
"""Regression tests for daily frames whose source omits the volume column.

AkShare's ``stock_zh_a_hist_tx`` no longer returns ``volume``. The shared
``_clean_data`` / ``_calculate_indicators`` steps used to index that column
unconditionally, so the whole fallback chain died with a bare
``KeyError: ['volume']`` instead of moving on to the next source.

Driven through a minimal ``BaseFetcher`` subclass on purpose: the helpers under
test live on the base class, and importing a concrete AkShare fetcher would pull
in ``src.config`` (and with it the local ``.env``) into ``os.environ``.
"""

import pandas as pd
import pytest

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

from data_provider.base import BaseFetcher


class _ProbeFetcher(BaseFetcher):
    """Exercises only the shared cleaning/indicator helpers."""

    def _fetch_raw_data(self, stock_code, start_date, end_date):
        raise NotImplementedError

    def _normalize_data(self, df, stock_code):
        return df


def _fetcher() -> _ProbeFetcher:
    return _ProbeFetcher()


def _normalized_without_volume() -> pd.DataFrame:
    """Shaped like AkShare's tencent payload after column renaming."""
    return pd.DataFrame(
        {
            "code": ["300124"] * 3,
            "date": pd.to_datetime(["2026-08-19", "2026-08-20", "2026-08-21"]),
            "open": [61.0, 61.5, 62.0],
            "high": [62.0, 62.5, 63.0],
            "low": [60.5, 61.0, 61.5],
            "close": [61.5, 62.0, 62.5],
            "amount": [1.0e8, 1.1e8, 1.2e8],
            "pct_chg": [0.5, 0.8, 0.8],
        }
    )


def _normalized_with_volume() -> pd.DataFrame:
    frame = _normalized_without_volume()
    frame["volume"] = [27912140.0, 27846701.0, 27304656.0]
    return frame


def test_clean_data_tolerates_a_source_without_volume() -> None:
    cleaned = _fetcher()._clean_data(_normalized_without_volume())

    # The regression: this used to raise KeyError: ['volume'].
    assert len(cleaned) == 3
    assert "volume" not in cleaned.columns
    assert cleaned["close"].notna().all()


def test_calculate_indicators_skips_volume_ratio_without_volume() -> None:
    fetcher = _fetcher()
    cleaned = fetcher._clean_data(_normalized_without_volume())

    indicators = fetcher._calculate_indicators(cleaned)

    # No volume means no honest ratio; leaving it absent lets downstream
    # consumers fall back to their own "missing data" handling instead of
    # reporting a fabricated value.
    assert "volume_ratio" not in indicators.columns
    assert indicators["ma5"].notna().all()


def test_calculate_indicators_still_builds_volume_ratio_when_present() -> None:
    fetcher = _fetcher()
    cleaned = fetcher._clean_data(_normalized_with_volume())

    indicators = fetcher._calculate_indicators(cleaned)

    assert "volume_ratio" in indicators.columns
    assert indicators["volume_ratio"].notna().all()
    # First row uses the one-period average, so the ratio is exactly 1.0.
    assert indicators["volume_ratio"].iloc[0] == pytest.approx(1.0)


def test_clean_data_still_drops_rows_with_a_missing_close() -> None:
    frame = _normalized_without_volume()
    frame.loc[1, "close"] = None

    cleaned = _fetcher()._clean_data(frame)

    assert len(cleaned) == 2
