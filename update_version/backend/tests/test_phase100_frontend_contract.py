"""Phase 100: 前端调用的每个 API 路径都必须真实存在于后端

前端 `src/api/hotAnalysis.ts` 里写死了大量端点，后端有 158 条路由。两边分头
改动时最容易出现「前端点了没反应 / 404」，而且这类问题单测抓不到（前端不跑
TypeScript 类型与后端路由的交叉校验）。

做法：从后端 app 导出真实路由表，从前端源码提取字符串形式的路径，
双向归一化后比对。

归一化要处理两件事：
  - 后端 OpenAPI 风格 `{run_id}`；
  - 前端模板字符串 `${detail.value.id}`。
两者统一成 `{}` 再比。
"""
import os
import re
import sys

import pytest

BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
FRONTEND_SRC = os.path.normpath(os.path.join(BACKEND, "..", "frontend", "src"))

if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

# 不是 API 的字符串（静态资源、路由页、前缀常量）
_IGNORE_EXT = re.compile(r"\.(png|svg|css|json|ico|woff2?|ttf|md)$", re.I)
_IGNORE_EXACT = {
    "/",
    "/api",
    "/api/v1",
    "/api/v2",
    "/api/v1/api/v2/",  # 拼接产物，不是真实路径
}


def _norm(path: str) -> str:
    path = re.sub(r"\$\{[^}]*\}", "{}", path)
    path = re.sub(r"\{[^}]+\}", "{}", path)
    return path.rstrip("/") or "/"


def _backend_paths():
    from app import app

    paths = set()
    for route in app.routes:
        p = getattr(route, "path", None)
        if p:
            paths.add(_norm(p))
    return paths


def _frontend_paths():
    found = set()
    if not os.path.isdir(FRONTEND_SRC):
        return found
    for root, _, files in os.walk(FRONTEND_SRC):
        for fn in files:
            if not fn.endswith((".ts", ".js", ".vue")):
                continue
            txt = open(os.path.join(root, fn), encoding="utf-8", errors="ignore").read()
            for m in re.finditer(r"""['"`](/[A-Za-z0-9/_\-{}$:.]*)['"`]""", txt):
                found.add(m.group(1))
    return found


def test_frontend_api_paths_exist_in_backend():
    backend = _backend_paths()
    frontend = _frontend_paths()

    if not frontend:
        pytest.skip("前端源码不可用")

    missing = []
    for rp in sorted(frontend):
        if _IGNORE_EXT.search(rp) or rp in _IGNORE_EXACT:
            continue
        candidates = [rp] if rp.startswith("/api/") else [f"/api/v2{rp}", f"/api/v1{rp}"]
        if not any(_norm(c) in backend for c in candidates):
            missing.append(rp)

    assert not missing, (
        "前端调用了后端不存在的路径（点击会 404）：\n  " + "\n  ".join(missing)
    )


def test_backend_still_serves_the_core_endpoints():
    """守住几条最关键路径，避免重构时把别名路由一起删掉"""
    backend = _backend_paths()
    required = [
        "/health",
        "/ready",
        "/api/v2/hot-analysis/run",
        "/api/v2/hot-analysis/run-async",
        "/api/v2/hot-analysis/analyze-single",
        "/api/v2/hot-analysis/market-review/generate",
        "/api/v2/cron/user-jobs",
        "/api/v2/community/hot-list",
        "/api/v2/paper/leaderboard",
        "/api/v2/audit/recent",
    ]
    missing = [p for p in required if _norm(p) not in backend]
    assert not missing, f"核心端点缺失: {missing}"


def test_v1_alias_still_mounted():
    """前端走 /api/v1/hot-analysis/*，两套前缀必须都挂着"""
    backend = _backend_paths()
    v1 = [p for p in backend if p.startswith("/api/v1/")]
    v2 = [p for p in backend if p.startswith("/api/v2/")]
    assert v1, "v1 兼容前缀整体消失了"
    assert v2, "v2 前缀整体消失了"
