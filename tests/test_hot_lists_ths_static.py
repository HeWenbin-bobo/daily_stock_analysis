# -*- coding: utf-8 -*-
"""Regression tests for the browser-free THS hot-list parser.

`fetch_ths_hot` used to depend on Playwright, which container images do not
install; the 同花顺 list was then always empty and `/hot-analysis/preview`
silently degraded to 2/4 sources. The rank pages are server-rendered GBK
tables, so they are parsed directly now. These tests pin the HTML contract.
"""

from __future__ import annotations

from data_provider.hot_lists import fetch_ths_hot_static, parse_ths_rank_html

_HEADER = (
    "<tr><th>序号</th><th>股票代码</th><th>股票简称</th>"
    "<th>涨跌幅（%）</th><th>最新价（元）</th><th>成交量（股）</th></tr>"
)


def _page(*rows: str) -> str:
    return f"<html><body><table>{_HEADER}{''.join(rows)}</table></body></html>"


def test_parse_ths_rank_html_extracts_code_and_name() -> None:
    html = _page(
        "<tr><td>1</td><td>300226</td><td>上海钢联</td><td>2.25%</td><td>17.70</td><td>1173.90万</td></tr>",
        "<tr><td>2</td><td>600495</td><td>晋西车轴</td><td>1.47%</td><td>4.15</td><td>1420.45万</td></tr>",
    )

    rows = parse_ths_rank_html(html, "持续放量")

    assert [row["code"] for row in rows] == ["300226", "600495"]
    assert [row["rank"] for row in rows] == [1, 2]
    assert rows[0]["name"] == "上海钢联"
    assert rows[0]["source"] == "同花顺热度榜(持续放量)"


def test_parse_ths_rank_html_drops_star_market_and_non_a_shares() -> None:
    """688* 科创板与北交所不在四榜交集范围内，解析阶段就剔除。"""
    html = _page(
        "<tr><td>1</td><td>688111</td><td>金山办公</td><td>1%</td><td>10</td><td>1</td></tr>",
        "<tr><td>2</td><td>830799</td><td>艾融软件</td><td>1%</td><td>10</td><td>1</td></tr>",
        "<tr><td>3</td><td>300226</td><td>上海钢联</td><td>2%</td><td>17</td><td>1</td></tr>",
    )

    rows = parse_ths_rank_html(html, "持续高")

    assert [row["code"] for row in rows] == ["300226"]


def test_parse_ths_rank_html_handles_missing_table() -> None:
    """反爬页 / 改版页没有表格时返回空列表，而不是抛异常。"""
    assert parse_ths_rank_html("<html><body>blocked</body></html>", "新上榜") == []


def test_fetch_ths_hot_static_returns_empty_when_all_pages_fail(monkeypatch) -> None:
    """三个 rank 页全失败时必须优雅降级（交给 Playwright / akshare 后备）。"""
    import urllib.request

    def _boom(*_args, **_kwargs):
        raise OSError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    assert fetch_ths_hot_static(200) == []
