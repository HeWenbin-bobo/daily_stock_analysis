# -*- coding: utf-8 -*-
"""Regression test: market-cap verification must not reuse another company's shares.

``deep_research`` passed ``shares=12.56e8`` — Kweichow Moutai's approximate share
count — for **every** symbol. 000858 (Wuliangye) at ¥130 therefore came out at
163.3B, against a real market cap near 500B, and the frontend rendered that
number under "市值验算（financial_rigor）".

v2's quote adapters expose price/change/volume/amount only — no share count and
no total market cap — so the honest answer is "not verified" rather than
somebody else's capital structure.

Run: ``python -m pytest update_version/backend/tests/test_phase78_deep_research_market_cap.py``
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest

from services import deep_research as dr


def test_no_hardcoded_share_count_in_the_code() -> None:
    """A literal share count can only ever be right for one company."""
    src = inspect.getsource(dr.deep_research)
    code_lines = [
        line for line in src.splitlines()
        if not line.lstrip().startswith("#") and "12.56e8" in line
    ]
    assert not code_lines, f"hardcoded share count still in code: {code_lines}"


def test_market_cap_is_reported_as_skipped(monkeypatch) -> None:
    """With no share data the result must say so instead of guessing."""
    monkeypatch.setattr(
        dr, "_call_llm",
        lambda *a, **k: {"content": "{}", "provider": "mock", "mocked": True},
    )
    monkeypatch.setattr("core.data_source.fetch_quote_with_fallback",
                        lambda code: {"code": code, "name": "test", "price": 130.0})

    result = dr.deep_research("000858")

    assert result["cap_verify"] is not None
    assert result["cap_verify"]["skipped"] is True
    assert result["cap_verify"]["ok"] is False
    assert "未做市值验算" in result["cap_verify"]["note"]


def test_verify_market_cap_still_computes_when_shares_are_known() -> None:
    """The helper itself is fine — the defect was the caller's constant."""
    out = dr.verify_market_cap(price=1330.0, shares=12.56e8, reported_cap=0)
    assert out["ok"] is True
    assert out["skipped"] is True
    assert "1.67" in out["computed_cap"] or "1,6" in out["computed_cap"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
