# -*- coding: utf-8 -*-
"""审计日志：所有写操作记录"""
import json
import logging
import re
import time
from contextlib import contextmanager
from typing import Optional, Any, Dict

from db.migrate import get_conn


@contextmanager
def _conn():
    """必定关闭的连接。

    ★ 探索期修复(fd 泄漏)：record / list_recent / cleanup_old 三处都把
      `get_conn()` 的结果直接丢掉，从不 close。`sqlite3.Connection` 不会因为
      引用消失就释放 fd（容器内实测：drop 引用后 fd 不变，gc.collect() 才归 0）。
      每条连接占 2 个 fd（db + wal），实测 `GET /audit/recent` 每次 +1，
      连续调用后进程里堆积了几十个 v2_dev.db 句柄。

      更严重的是 `record()`：它被**每个写操作**调用（几乎所有 POST/DELETE 端点），
      属于全站写路径的公共开销。
    """
    conn = get_conn()
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass

logger = logging.getLogger(__name__)

# ★ Round 4 安全加固：以下字段在 request_body 落库前要 redact，避免凭据泄露
_SENSITIVE_KEYS = {"target", "password", "passwd", "pwd", "token", "api_key", "secret", "webhook_url"}
# ★ 探索期修复(脱敏漏网)：上面那组是「完整字段名」，而旧实现只做
#   `k.lower() in _SENSITIVE_KEYS` 精确匹配 —— 实测 `api_keys`、`access_token`、
#   `smtp_password`、`apiKey`、`private_key` 全部原样落库，与
#   "避免 /audit/recent 泄露 webhook URL / SMTP 密码 / API key" 的承诺不符。
#   这里改成**按词切分后匹配**：把 key 拆成 token（下划线/连字符/点/驼峰），
#   任一 token 命中敏感词或它的复数形式就脱敏。
_SENSITIVE_TOKENS = {
    "password", "passwd", "pwd", "secret", "token", "key", "keys",
    "credential", "credentials", "auth", "authorization", "signature",
}


def _key_is_sensitive(key: str) -> bool:
    """判断字段名是否可能承载凭据。

    - 完整名命中 `_SENSITIVE_KEYS` → 敏感
    - 否则按下划线/连字符/点/驼峰切成 token，任一 token 落在
      `_SENSITIVE_TOKENS` 里 → 敏感（覆盖 api_keys / access_token /
      smtp_password / apiKey / privateKey / X-Api-Key 等写法）

    例外：`key` 单独出现时过于宽泛（如 `keyboard`），因此只在整个 token 等于
    `key`/`keys` 时才算——`monkey` 这种会被切成一个 token 而不匹配。
    """
    lowered = (key or "").lower()
    if lowered in _SENSITIVE_KEYS:
        return True
    # 驼峰与分隔符混合 → 统一成小写 token 列表
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key or "")
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", normalized.lower()) if t]
    for tok in tokens:
        if tok in _SENSITIVE_TOKENS:
            return True
        # 复数：api_keys → key(s)、credentials → credential(s)
        if tok.endswith("s") and tok[:-1] in _SENSITIVE_TOKENS:
            return True
        if tok.endswith("es") and tok[:-2] in _SENSITIVE_TOKENS:
            return True
    return False


def _redact_body(body: Optional[Any]) -> Optional[Any]:
    """递归脱敏：遇到敏感 key 替换为 '***'。dict / list 都处理。"""
    if body is None:
        return None
    if isinstance(body, dict):
        out: Dict[str, Any] = {}
        for k, v in body.items():
            if isinstance(k, str) and _key_is_sensitive(k):
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
        with _conn() as conn:
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
    with _conn() as conn:
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
    with _conn() as conn:
        cur = conn.cursor()
        cutoff = time.time() - days * 86400
        cur.execute("DELETE FROM audit_log WHERE created_at < ?", (cutoff,))
        deleted = cur.rowcount
    logger.info("Cleaned up %d old audit log entries", deleted)
    return deleted