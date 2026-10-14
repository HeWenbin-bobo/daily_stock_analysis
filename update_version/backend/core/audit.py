# -*- coding: utf-8 -*-
"""审计日志：所有写操作记录"""
import json
import logging
import time
from typing import Optional, Any, Dict

from db.migrate import get_conn

logger = logging.getLogger(__name__)

# ★ Round 4 安全加固：以下字段在 request_body 落库前要 redact，避免凭据泄露
_SENSITIVE_KEYS = {"target", "password", "passwd", "pwd", "token", "api_key", "secret", "webhook_url"}


def _redact_body(body: Optional[Any]) -> Optional[Any]:
    """递归脱敏：遇到敏感 key 替换为 '***'。dict / list 都处理。"""
    if body is None:
        return None
    if isinstance(body, dict):
        out: Dict[str, Any] = {}
        for k, v in body.items():
            if k.lower() in _SENSITIVE_KEYS:
                out[k] = "***"
            else:
                out[k] = _redact_body(v)
        return out
    if isinstance(body, list):
        return [_redact_body(x) for x in body]
    return body


def record(
    *,
    action: str,
    user_id: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_body: Optional[dict] = None,
    status: str = "success",
    error_message: Optional[str] = None,
) -> int:
    """记录一条审计日志。

    ★ Round 4 安全修复：request_body 落库前对敏感字段 (target/password/token/...) redact，
    避免 /audit/recent 端点泄露 webhook URL / SMTP 密码 / API key。
    """
    try:
        redacted = _redact_body(request_body) if request_body else None
        body_json = json.dumps(redacted, ensure_ascii=False, default=str) if redacted else None
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO audit_log
               (user_id, action, resource_type, resource_id, ip_address, user_agent,
                request_body, status, error_message, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id, action, resource_type, resource_id, ip_address, user_agent,
                body_json, status, error_message, time.time(),
            ),
        )
        return cur.lastrowid or 0
    except Exception as e:
        logger.exception("Failed to record audit log: %s", e)
        return 0


def list_recent(
    user_id: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """查询最近的审计日志"""
    conn = get_conn()
    cur = conn.cursor()
    sql = "SELECT * FROM audit_log WHERE 1=1"
    params: list = []
    if user_id:
        sql += " AND user_id = ?"
        params.append(user_id)
    if action:
        sql += " AND action = ?"
        params.append(action)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def cleanup_old(days: int = 90) -> int:
    """清理 N 天前的审计日志"""
    conn = get_conn()
    cur = conn.cursor()
    cutoff = time.time() - days * 86400
    cur.execute("DELETE FROM audit_log WHERE created_at < ?", (cutoff,))
    deleted = cur.rowcount
    logger.info("Cleaned up %d old audit log entries", deleted)
    return deleted