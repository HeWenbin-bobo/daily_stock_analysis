# -*- coding: utf-8 -*-
"""LLM 输出解析的公共工具。

大模型返回结构化结果时经常夹带散文：前置"好的，结果如下："、后置"以上是分析"，
甚至在说明里随手写个大括号。用 `find("{")` + `rfind("}")` 取范围会被这些
无关字符撑坏，`json.loads` 直接抛错，调用方只能把整份结果当成解析失败。
这里提供按**括号配平**抽取第一个完整 JSON 对象的实现。

两个使用方：`services/deep_research.py`（四大师报告）与
`modules/backtest_engine.py`（自然语言 → 回测 DSL）。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """从 LLM 输出里抽出**第一个完整**的 JSON 对象；抽不到返回 None。

    ★ 探索期修复(LLM 合法输出被误判为解析失败)：旧实现用
      `cleaned.find("{")` + `cleaned.rfind("}")` 取范围。只要 LLM 在 JSON
      前后多写一句带大括号的话，范围就被撑坏，json.loads 抛
      `Extra data` / `Expecting property name`，整份报告降级成
      「数据不足」——而真正的结构化结果其实就在响应里。实测两种常见形态：
        - `{"a":1}\\n\\n说明：这里的 {占位符} 需要替换`  → Extra data
        - `分析如下 {注意}：\\n{"a":1}`                  → Expecting property name
      改为从第一个 `{` 起做**括号配平扫描**（跳过字符串字面量与转义），
      取第一个配平的片段解析；失败则继续往后找下一个 `{`。
    """
    if not text:
        return None
    # 剥掉可能的 markdown 代码围栏
    cleaned = text.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        # ```json\n{...}\n``` → parts[1] 是内容；没有闭合围栏时退回全文
        cleaned = (parts[1] if len(parts) >= 2 else cleaned).strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    start = cleaned.find("{")
    while start >= 0:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(cleaned)):
            ch = cleaned[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        candidate = json.loads(cleaned[start : i + 1])
                    except Exception:
                        break  # 这段不是合法 JSON，换下一个起点
                    if isinstance(candidate, dict):
                        return candidate
                    break
        start = cleaned.find("{", start + 1)
    return None
