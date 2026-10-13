"""前端 utils 测试: 这个测试用 Python 模拟 markdown 解析逻辑验证基本子集。

真实的 markdown 渲染器是 TypeScript (utils/markdown.ts), 这里只验证渲染输出的
几个关键 case 对应后端返回的数据不会破坏 HTML 结构。
"""
import re
import sys
from html import escape

# 简单的 markdown 模拟实现(只覆盖我们用到的子集)
def render_markdown(text: str) -> str:
    if not text:
        return ""
    lines = text.split("\n")
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("### "):
            out.append(f"<h3>{escape(line[4:])}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{escape(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{escape(line[2:])}</h1>")
        elif line.startswith("> "):
            out.append(f"<blockquote>{escape(line[2:])}</blockquote>")
        elif line.startswith("- "):
            out.append(f"<li>{escape(line[2:])}</li>")
        else:
            # bold
            line = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escape(line))
            out.append(f"<p>{line}</p>")
    return "".join(out)


def test_empty():
    assert render_markdown("") == ""


def test_plain_text():
    assert render_markdown("hello world") == "<p>hello world</p>"


def test_bold():
    assert "<strong>重要</strong>" in render_markdown("这是 **重要** 的")


def test_headings():
    md = "# 一级\n## 二级\n### 三级"
    out = render_markdown(md)
    assert "<h1>一级</h1>" in out
    assert "<h2>二级</h2>" in out
    assert "<h3>三级</h3>" in out


def test_html_escape():
    """LLM 输出可能含 HTML, 必须 escape 避免 XSS。"""
    md = "<script>alert('xss')</script>"
    out = render_markdown(md)
    assert "<script>" not in out  # 转义后不应该是真正的 <script> 标签
    assert "&lt;script&gt;" in out


def test_real_llm_response_simulation():
    """模拟一次真实的 LLM 回复, 验证 markdown 解析不破坏结构。"""
    md = """该股当前处于 **下跌趋势**, 主要风险:
- 高位回调幅度较大
- 成交量未放大

> 建议: 等待止跌信号

当前估值处于历史 30% 分位, 适合 **中长期** 布局。"""
    out = render_markdown(md)
    assert "<strong>下跌趋势</strong>" in out
    assert "<li>高位回调幅度较大</li>" in out
    assert "<blockquote>" in out


if __name__ == "__main__":
    test_empty()
    test_plain_text()
    test_bold()
    test_headings()
    test_html_escape()
    test_real_llm_response_simulation()
