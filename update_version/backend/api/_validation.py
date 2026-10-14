# -*- coding: utf-8 -*-
"""
★ Round 13：输入验证辅助

将 `payload: dict` 端点从 `payload["key"]` 隐式 KeyError → 500 改为
显式 HTTPException(400) 缺失字段。
"""
from typing import Any
from fastapi import HTTPException


def require_keys(payload: Any, *keys: str) -> dict:
    """确保 payload 是 dict 且包含所有 keys；缺失则抛 400。

    Usage:
        require_keys(payload, "user_id", "name")
        require_keys(payload, "user_id", default_keys={"user_id": "default"})

    返回 payload 本身（dict），便于链式调用 `payload["user_id"]`。
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")
    missing = [k for k in keys if k not in payload or payload[k] in (None, "")]
    if missing:
        raise HTTPException(status_code=400, detail={"missing_fields": missing})
    return payload


def optional_keys(payload: Any, *keys: str) -> dict:
    """确保 payload 是 dict；keys 缺失不报错。"""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")
    return payload
