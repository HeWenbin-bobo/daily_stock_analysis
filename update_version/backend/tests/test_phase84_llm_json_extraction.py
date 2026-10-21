# -*- coding: utf-8 -*-
"""Regression test: extract the JSON object from messy LLM output.

``deep_research`` used ``find("{")`` + ``rfind("}")`` to slice the payload. Any
prose containing a brace before or after the JSON widened that slice, and
``json.loads`` raised ``Extra data`` / ``Expecting property name`` — so a
perfectly good structured answer was thrown away and the report degraded to
"数据不足". Measured, before the fix:

    '{"a": 1}\n\n说明：这里的 {占位符} 需要替换'   -> JSONDecodeError: Extra data
    '分析如下 {注意}：\n{"a": 1}'                  -> JSONDecodeError: Expecting ...

The extractor now scans for the first *balanced* object, skipping string
literals and escapes.

Run: ``python -m pytest update_version/backend/tests/test_phase84_llm_json_extraction.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest

from services.deep_research import _extract_json_object


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        # 旧实现失败的两类
        ('{"a": 1}\n\n说明：这里的 {占位符} 需要替换', {"a": 1}),
        ('分析如下 {注意}：\n{"a": 1}', {"a": 1}),
        # JSON 后跟普通说明（旧实现本来就能过，防止回归）
        ('{"a": 1}\n\n以上是分析结果。', {"a": 1}),
        # 嵌套
        ('{"a": {"b": {"c": 3}}}', {"a": {"b": {"c": 3}}}),
        # 字符串字面量里出现大括号，不能当结构符号
        ('{"note": "含 } 和 { 的文本"}', {"note": "含 } 和 { 的文本"}),
        # 转义引号
        ('{"q": "he said \\"hi\\""}', {"q": 'he said "hi"'}),
        # 前面有无效的 { 片段，应跳到下一个
        ('{ 不是 json } 然后 {"a": 2}', {"a": 2}),
    ],
)
def test_extracts_the_object(text: str, expected: dict) -> None:
    assert _extract_json_object(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "完全没有结构", "只有左括号 {", "[]"])
def test_returns_none_when_there_is_no_object(text: str) -> None:
    assert _extract_json_object(text) is None


def test_non_dict_json_is_rejected() -> None:
    """A bare array or scalar is not the contract; callers expect a mapping."""
    assert _extract_json_object("[1, 2, 3]") is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------------------
# 同一个抽取器也服务于 DSL 编译路径
# ---------------------------------------------------------------------------
def test_shared_extractor_is_used_by_both_callers() -> None:
    """deep_research 与 backtest_engine 必须共用同一实现，避免逻辑分叉。"""
    import modules.backtest_engine as be
    import services.deep_research as dr
    from core.json_utils import _extract_json_object

    assert be._extract_json_object is _extract_json_object
    assert dr._extract_json_object is _extract_json_object
