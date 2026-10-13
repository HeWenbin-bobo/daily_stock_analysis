"""Defensive helpers: input validation + admin auth dependency.

放这里的原因 (而非 services/security.py):
  - services/__init__.py 在包初始化阶段会 eager-import services.data_provider,
    而 data_provider 又 import schemas.stocks。
  - 如果 security 放在 services 包内, schemas.stocks 在导入时通过
    services.security 触发 services 包初始化, 形成循环导入。
  - 因此把 security 放在顶层 backend.security, 不触发 services/__init__。

设计目标:
  - 默认本地开发不破坏现有测试/前端 (TestClient 使用 127.0.0.1 / 'testclient')。
  - ADMIN_API_KEY 未配置且来自非 loopback → 403, 避免被无意识暴露。
  - ADMIN_API_KEY 配置后必须 Bearer, 否则 401; 错误响应不泄露 key 内容。
  - 校验失败一律 400/422, 文案清晰, 不泄露内部状态。
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import re
from typing import Optional

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)


# -------- 输入限制常量 (公开可调) --------

# 股票代码: A股 6 位数字, 港股 1-7 位数字 (含 0 前缀), 美股 1-10 位字母数字或 BRK-B 形式。
# 允许带 .SS/.SZ/.HK 后缀和 HK 前缀, 总长度上限 20。
STOCK_CODE_MAX_LENGTH = 20
# 字符组成: 大写字母/数字, 允许可选的 HK 前缀, 可选的 .SS/.SZ/.HK 后缀, 可选 - (BRK-B)。
# 主 token 最长 18 (含 HK 前缀 2 + .HK 后缀 3 后总长度仍 ≤ 20, 由 STOCK_CODE_MAX_LENGTH 二次兜底)。
STOCK_CODE_PATTERN = re.compile(
    r"^(?:HK)?[0-9A-Z-]{1,18}(?:\.(?:SS|SZ|HK))?$",
    re.IGNORECASE,
)

# 搜索关键字: 中英文混合, 上限 50 字符, 防止缓存基数爆掉。
SEARCH_Q_MAX_LENGTH = 50

# LLM 用户附加问题: 防止 LLM 上下文/token 滥用, 上限 500 字符。
LLM_EXTRA_PROMPT_MAX_LENGTH = 500


# -------- 校验函数 --------

def normalize_and_validate_stock_code(code: str) -> str:
    """校验 stock_code, 返回 strip+upper 后的字符串。

    抛出 HTTPException(400) 当:
      - 超过最大长度
      - 字符不在允许集合内 (空白/控制字符/特殊符号)
      - 纯空白输入
    """
    if code is None:
        raise HTTPException(status_code=400, detail="stock_code 不能为空")
    if not isinstance(code, str):
        code = str(code)
    if len(code) > STOCK_CODE_MAX_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"stock_code 长度超限 (上限 {STOCK_CODE_MAX_LENGTH})",
        )
    # 拒绝控制字符 (检查 strip 之前, 防止末尾 \\n 绕过)
    if any(ord(ch) < 32 for ch in code):
        raise HTTPException(status_code=400, detail="stock_code 含非法控制字符")
    code = code.strip()
    if not code:
        raise HTTPException(status_code=400, detail="stock_code 不能为空")
    # 拒绝内部空白 (strip 后只可能有 middle whitespace)
    if any(ch.isspace() for ch in code):
        raise HTTPException(status_code=400, detail="stock_code 含空白字符")
    upper = code.upper()
    if not STOCK_CODE_PATTERN.match(upper):
        raise HTTPException(
            status_code=400,
            detail="stock_code 格式不合法 (期望 A股 6 位数字 / 港股 1-7 位数字 / 美股 1-10 位字母数字或 BRK-B, 可带 .SS/.SZ/.HK 后缀)",
        )
    return upper


def validate_search_q(q: Optional[str]) -> str:
    """校验搜索关键字, 返回 strip 后的字符串 (允许空字符串)。"""
    if q is None:
        return ""
    if not isinstance(q, str):
        q = str(q)
    if len(q) > SEARCH_Q_MAX_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"搜索关键字长度超限 (上限 {SEARCH_Q_MAX_LENGTH})",
        )
    # 拒绝控制字符
    if any(ord(ch) < 32 for ch in q):
        raise HTTPException(status_code=400, detail="搜索关键字含非法控制字符")
    return q.strip()


def validate_extra_prompt(prompt: Optional[str]) -> Optional[str]:
    """校验 LLM 用户附加 prompt; None/空 -> None。"""
    if prompt is None:
        return None
    if not isinstance(prompt, str):
        prompt = str(prompt)
    if len(prompt) > LLM_EXTRA_PROMPT_MAX_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"extra_prompt 长度超限 (上限 {LLM_EXTRA_PROMPT_MAX_LENGTH})",
        )
    # 拒绝控制字符 (保留 \\n \\t 给多行 prompt 用)
    if any(ord(ch) < 32 and ch not in ("\n", "\t") for ch in prompt):
        raise HTTPException(status_code=400, detail="extra_prompt 含非法控制字符")
    stripped = prompt.strip()
    return stripped or None


# -------- Admin 鉴权 --------

def _client_is_loopback(request: Request) -> bool:
    """判断 client 是否在 loopback 接口 (127.0.0.0/8, ::1, 测试 host)。"""
    client = request.client
    if not client:
        return False
    host = client.host
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback
    except ValueError:
        # 主机名 (例如 'localhost') / starlette TestClient ('testclient')
        # iter 105: 用 frozenset 替代 tuple — O(1) 查找 + 不可变, 符合编码规范
        # "Prefer immutable data structures"。迭代器不会修改 host 集合。
        return host in _LOOPBACK_HOSTNAMES


# iter 105: 模块级 frozenset 缓存 (避免每次调用重建集合)
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "testclient", "testserver"})


def _bearer_token(request: Request) -> Optional[str]:
    """从 Authorization header 抽取 Bearer token (大小写不敏感)。"""
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


def _safe_equals(a: str, b: str) -> bool:
    """常数时间比较, 避免时序泄露。"""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def get_admin_api_key() -> Optional[str]:
    """读取 ADMIN_API_KEY 环境变量, strip 后返回 (未设置则 None)。"""
    key = os.environ.get("ADMIN_API_KEY")
    if key is None:
        return None
    key = key.strip()
    return key or None


def require_admin(request: Request) -> None:
    """Admin 端点鉴权依赖。

    规则 (按部署风险递增):
      1. ADMIN_API_KEY 已配置 → 必须 `Authorization: Bearer <key>`, 否则 401。
      2. 未配置 + 来自 loopback (TestClient / 本机访问) → 放行 (开发友好)。
      3. 未配置 + 来自非 loopback → 403 (避免无意识暴露)。
    """
    expected = get_admin_api_key()
    if expected:
        token = _bearer_token(request)
        # iter 135: 当 token 为空时也跑 hmac.compare_digest (用 expected 占位) — 避免
        # "无 token" 路径 vs "有 token 错误" 路径时间差异被用来推断 key 是否配置。
        token_to_check = token or ""
        if not _safe_equals(token_to_check, expected):
            # 注意: 错误响应不泄露 key 长度或内容
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="需要有效的管理员 Bearer token",
                headers={"WWW-Authenticate": 'Bearer realm="admin"'},
            )
        return
    # 未配置 key
    if _client_is_loopback(request):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="该端点仅限 loopback 访问; 如需远程访问请配置 ADMIN_API_KEY",
    )