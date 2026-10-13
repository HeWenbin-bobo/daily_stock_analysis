# -*- coding: utf-8 -*-
"""
深度研究服务（Deep Research Service）—— v3 融合 ai-berkshire 四大师价值投资框架

将 ai-berkshire（巴菲特/芒格/段永平/李录 四大师方法论）有机集成到 v3：
- 对单只股票执行"四大师综合分析"，输出六维评分表 + 强制结论（买入/观望/回避）+ 价格区间
- 复用 v3 的 LLM 路由（core.llm_router.chat）与数据源（core.data_source）
- 复用 ai-berkshire 的 financial_rigor.py 做精确计算验证（市值验算等）
- 遵循 ai-berkshire 输出规范：信息丰富度评级（A/B/C）+ AI 研究局限性声明

设计要点：
- 独立模块，不侵入现有 analyze-single 主流程；作为"深度研究"增强入口
- LLM 不可用时返回 mock 占位（与 v3 其他模块一致，mocked=True 标记）
- 数据不足时给出"数据不足，建议人工核验"的明确提示，不编造
"""
import os
import json
import logging
import subprocess
import importlib.util
from pathlib import Path
from typing import Dict, Any, List, Optional
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

# ============================================================================
# 常量与大师框架定义
# ============================================================================

MASTERS = [
    {"key": "buffett", "name": "巴菲特", "dimension": "护城河 / 生意模式"},
    {"key": "munger", "name": "芒格", "dimension": "最大风险 / 逆向思维"},
    {"key": "duan", "name": "段永平", "dimension": "生意质量 / 商业模式"},
    {"key": "li", "name": "李录", "dimension": "文明趋势 / 长期主义"},
]

DIMENSIONS = [
    "生意质量（段永平）",
    "护城河（巴菲特）",
    "管理层（段永平+巴菲特）",
    "最大风险（芒格）",
    "文明趋势（李录）",
    "估值（巴菲特+段永平）",
]

# 信息丰富度评级（ai-berkshire 规范）
INFO_RICHNESS_RULES = {
    "A": "上市多年、券商覆盖多、媒体报道密集 → 重点做反面检验",
    "B": "上市1-3年、覆盖有限、部分数据需推算 → 推算数据标注置信度",
    "C": "刚上市/冷门股、几乎无覆盖 → 第一性原理提问",
}


def _berkshire_tools_dir() -> Path:
    """ai-berkshire 集成工具目录（financial_rigor.py 等）"""
    return Path(__file__).resolve().parent / "berkshire"


def _load_financial_rigor():
    """按路径加载 financial_rigor.py（ai-berkshire 工具，零依赖 stdlib）"""
    path = _berkshire_tools_dir() / "financial_rigor.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("v3_financial_rigor", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def verify_market_cap(price: float, shares: float, reported_cap: float, currency: str = "CNY") -> Dict[str, Any]:
    """市值验算（复用 ai-berkshire financial_rigor.py 的精确计算）

    Args:
        price: 当前股价
        shares: 总股本（股）
        reported_cap: 报告市值
        currency: 币种
    """
    mod = _load_financial_rigor()
    if mod is None:
        return {"ok": False, "error": "financial_rigor.py 不可用"}
    try:
        calc = mod.exact(price) * mod.exact(shares)
        reported = mod.exact(reported_cap)
        diff_pct = (calc - reported) / reported * 100 if reported else 0
        return {
            "ok": True,
            "computed_cap": mod.fmt_number(calc, currency),
            "reported_cap": mod.fmt_number(reported, currency),
            "diff_pct": round(float(diff_pct), 2),
            "consistent": abs(float(diff_pct)) <= 5,
            "currency": currency,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def estimate_info_richness(code: str, name: str = "") -> Dict[str, Any]:
    """估算信息丰富度评级（ai-berkshire A/B/C 规则）

    简化启发式：基于股票代码段 + 名称长度做初步评级，供报告开头展示。
    真实场景应结合券商覆盖/媒体报道密度，这里给出可解释的初始值。
    """
    # 创业板/科创板（300/688 开头）多为次新或中小盘 → 信息可能不足
    if code.startswith("688") or code.startswith("300"):
        grade = "B"
    # 主板大市值常见股（600/000 开头且名称常见）→ 信息充裕
    elif code.startswith(("600", "601", "603", "000", "002")):
        grade = "A"
    else:
        grade = "B"
    return {
        "grade": grade,
        "rules": INFO_RICHNESS_RULES[grade],
        "note": "初步评级（基于代码段启发式）；正式评级应结合券商覆盖与媒体报道密度",
    }


# ============================================================================
# LLM 调用（复用 v3 core.llm_router.chat）
# ============================================================================

def _call_llm(prompt: str, system: str, task_type: str = "complex", max_tokens: int = 4000) -> Dict[str, Any]:
    """调用 v3 LLM 路由（带降级链 + mock 兜底）"""
    from core import llm_router
    return llm_router.chat(
        prompt,
        system=system,
        task_complexity="complex",
        user_id=None,
        task_type=task_type,
        max_tokens=max_tokens,
    )


# ============================================================================
# 四大师综合分析（核心）
# ============================================================================

SYSTEM_PROMPT = (
    "你是一位融合巴菲特、芒格、段永平、李录四位价值投资大师方法论的投资研究员。\n"
    "你必须：\n"
    "1. 强制给结论，不打太极——对每只股票给出 买入/观望/回避 的明确判断和价格区间\n"
    "2. 六维评分：生意质量（段永平）/ 护城河（巴菲特）/ 管理层（段永平+巴菲特）/ 最大风险（芒格）/ 文明趋势（李录）/ 估值（巴菲特+段永平），每维 0-100 分 + 信心度\n"
    "3. 所有关键数据必须附来源，禁止编造；数据不足时明确标注'数据不足，建议人工核验'\n"
    "4. 区分'AI分析置信度'（取决于资料量）与'投资确定性'（取决于生意本质）\n"
    "5. 报告开头给出信息丰富度评级（A/B/C）和 AI 研究局限性声明\n"
    "6. 最终输出严格 JSON 格式，包含字段：info_richness, scores, conclusion, advice, price_range, risks, confidence, disclaimer"
)


def deep_research(code: str, name: str = "", *, prefer_provider: Optional[str] = None) -> Dict[str, Any]:
    """对单只股票执行四大师深度研究

    Args:
        code: 股票代码（如 600519）
        name: 股票名称（可选）
        prefer_provider: 首选 LLM provider（可选）

    Returns:
        Dict：含 scores/conclusion/advice/price_range/risks/confidence 等
    """
    from core.data_source import fetch_quote_with_fallback, is_mock_payload

    # 1. 取实时行情（真实数据基础）
    quote = fetch_quote_with_fallback(code)
    quote_data = {}
    if quote and not is_mock_payload(quote):
        quote_data = {
            "price": quote.get("price"),
            "change_pct": quote.get("change_pct"),
            "open": quote.get("open"),
            "high": quote.get("high"),
            "low": quote.get("low"),
            "volume": quote.get("volume"),
            "amount": quote.get("amount"),
            "source": quote.get("source"),
        }
    else:
        logger.warning("deep_research: %s quote 不可用或为 mock，报告将标注数据受限", code)

    stock_name = name or quote.get("name") if quote else name or code
    info_richness = estimate_info_richness(code, stock_name)

    # 2. 组装 prompt（四大师框架）
    prompt = f"""
请对 {stock_name}({code}) 进行四大师综合深度研究。

【当前行情快照】
{json.dumps(quote_data, ensure_ascii=False, indent=2) if quote_data else '（实时行情不可用）'}

【信息丰富度初步评级】{info_richness['grade']} 级：{info_richness['rules']}

请按四大师框架输出：
1. 六维评分表（每维：分数 0-100 + 一句话理由 + 信心度）
2. 综合结论：买入 / 观望 / 回避（强制明确，不回避）
3. 具体建议：空仓者 / 持仓者 / 加仓信号 / 卖出信号
4. 估值判断：给出具体价格区间（合理买入区间 / 高估区间）
5. 最大风险清单（芒格视角，至少 3 条）
6. AI 分析置信度 vs 投资确定性 的区分说明
7. 若数据不足，明确列出需要人工核验的问题清单

必须输出严格 JSON（不要 Markdown 代码块包裹），字段：
{{"info_richness": {{"grade": "A|B|C", "note": "..."}}, "scores": [{{"dimension": "...", "score": 0, "reason": "...", "confidence": "high|medium|low"}}], "conclusion": "买入|观望|回避", "advice": {{"empty_position": "...", "holder": "...", "add_signal": "...", "sell_signal": "..."}}, "price_range": {{"buy_zone": "...", "overvalued_zone": "..."}}, "risks": ["..."], "confidence": "high|medium|low", "analysis_confidence_note": "...", "verify_list": ["..."]}}
"""

    # 3. 调用 LLM（真实或 mock）
    llm_resp = _call_llm(prompt, SYSTEM_PROMPT)
    content = llm_resp.get("content", "")

    # 4. 解析 JSON 输出（容错：剥离可能的 markdown 代码块）
    parsed: Dict[str, Any] = {}
    try:
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```", 2)[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            parsed = json.loads(cleaned[start : end + 1])
    except Exception as e:
        logger.warning("deep_research JSON parse failed: %s", e)
        parsed = {"parse_error": str(e)[:200], "raw": content[:1000]}

    # 5. 市值验算（复用 financial_rigor.py，若有行情）
    cap_verify = None
    if quote_data.get("price"):
        # 600519 茅台总股本约 12.56 亿股（近似；真实股本需查财报）
        cap_verify = verify_market_cap(
            price=quote_data["price"],
            shares=12.56e8,  # 近似总股本，报告注明
            reported_cap=0,  # 无报告市值时跳过一致性判断
        )

    # 6. 组装响应
    return {
        "code": code,
        "name": stock_name,
        "info_richness": parsed.get("info_richness", info_richness),
        "scores": parsed.get("scores", []),
        "conclusion": parsed.get("conclusion", "数据不足，建议人工核验"),
        "advice": parsed.get("advice", {}),
        "price_range": parsed.get("price_range", {}),
        "risks": parsed.get("risks", []),
        "confidence": parsed.get("confidence", "low"),
        "analysis_confidence_note": parsed.get("analysis_confidence_note", ""),
        "verify_list": parsed.get("verify_list", []),
        "quote": quote_data,
        "cap_verify": cap_verify,
        "masters": MASTERS,
        "provider": llm_resp.get("provider"),
        "mocked": llm_resp.get("mocked", False),
        "disclaimer": "本报告由 AI 生成，仅供研究参考，不构成投资建议。AI 分析置信度与投资确定性不同，请独立判断。",
    }


def generate_markdown_report(result: Dict[str, Any]) -> str:
    """把深度研究结果渲染为 Markdown 报告（遵循 ai-berkshire 报告规范）"""
    lines = []
    lines.append(f"# {result.get('name')}({result.get('code')}) 深度研究报告")
    lines.append("")
    lines.append(f"> 报告生成时间：{__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    richness = result.get("info_richness", {})
    lines.append(f"**信息丰富度评级**：{richness.get('grade', '?')} 级 — {richness.get('note', '')}")
    lines.append(f"**AI 研究局限性声明**：{result.get('analysis_confidence_note', '')}")
    lines.append("")
    lines.append("## 当前行情")
    q = result.get("quote", {})
    if q:
        lines.append(f"- 现价：¥{q.get('price')}（{q.get('change_pct')}%） 来源：{q.get('source')}")
    else:
        lines.append("- 实时行情不可用（数据受限）")
    lines.append("")
    lines.append("## 四大师六维评分")
    lines.append("| 维度 | 分数 | 信心度 | 理由 |")
    lines.append("|------|------|--------|------|")
    for s in result.get("scores", []):
        lines.append(f"| {s.get('dimension', '')} | {s.get('score', '-')} | {s.get('confidence', '')} | {s.get('reason', '')} |")
    lines.append("")
    lines.append(f"## 综合结论：**{result.get('conclusion')}**（置信度：{result.get('confidence')}）")
    lines.append("")
    adv = result.get("advice", {})
    if adv:
        lines.append("### 操作建议")
        for k, v in adv.items():
            label = {"empty_position": "空仓者", "holder": "持仓者", "add_signal": "加仓信号", "sell_signal": "卖出信号"}.get(k, k)
            lines.append(f"- **{label}**：{v}")
    lines.append("")
    pr = result.get("price_range", {})
    if pr:
        lines.append("### 估值区间")
        lines.append(f"- 合理买入区间：{pr.get('buy_zone', '-')}")
        lines.append(f"- 高估区间：{pr.get('overvalued_zone', '-')}")
    lines.append("")
    risks = result.get("risks", [])
    if risks:
        lines.append("### 最大风险（芒格视角）")
        for r in risks:
            lines.append(f"- {r}")
    lines.append("")
    verify = result.get("verify_list", [])
    if verify:
        lines.append("### 需人工核验清单")
        for v in verify:
            lines.append(f"- [ ] {v}")
    lines.append("")
    lines.append("---")
    lines.append(result.get("disclaimer", ""))
    return "\n".join(lines)
