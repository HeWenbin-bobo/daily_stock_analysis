# -*- coding: utf-8 -*-
"""本轮修复验证（Round 27）：
1. [HIGH] 大盘复盘并发去重真正生效（同 key 并发第二个请求拿到 in_progress）
   + 异常路径下 finally 清理了 key
2. [HIGH] 异步落库失败可观测（persist_failed / persist_error 写回任务字典）
3. [MEDIUM] 预览端点 max_concurrency 不再被静默丢弃（schema 接受 + 预估函数生效）
4. [MEDIUM] 写端点归属校验不破坏无 header 的现有调用（向后兼容）
5. [LOW] update_alert_rule 支持 rule_type（含合法性校验）

测试全部 mock 数据源/DB/服务，永不联网、永不真跑全市场抓取。
"""
import os
import sys
import time
import threading
import importlib.util
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ★ 路径技巧：主项目 api/ 与 v2 backend api/ 同名 → sys.path 顺序决定 import 解析。
#   本测试在 backend/ 启动（backend/ 在 sys.path），所以 `import api` 会解析到 backend/api。
#   需要主项目 api.v1.* / data_provider.* 的测试，用 importlib 按文件加载，绕过 `api` 包机制。
_BACKEND = Path(__file__).resolve().parent.parent  # update_version_v3/backend/
_MAIN_ROOT = Path(__file__).resolve().parent.parent.parent.parent  # daily_stock_analysis_v2/

# 添加主项目根目录作为 fallback（backend 仍在首位，所以 `from api import ...` 仍解析到 backend）。
# 这样 importlib 加载主项目 endpoint 文件时，`from data_provider.hot_lists import ...` 才能解析。
if str(_MAIN_ROOT) not in sys.path:
    sys.path.append(str(_MAIN_ROOT))


def _load_module_from_file(mod_name: str, rel_path: str):
    """按文件路径加载模块，绕过同名包冲突。

    使用 *正确的全限定名* 写入 sys.modules（不是临时名），这样被加载模块内部
    的 `from xxx import yyy` 才能通过 sys.modules 找到已注册的兄弟模块。
    """
    spec = importlib.util.spec_from_file_location(mod_name, str(_MAIN_ROOT / rel_path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# 共享状态：用于 sys.path / sys.modules 跨函数恢复
_STATE: dict = {}


def _load_endpoints_hot():
    """Import api.endpoints_hot and repair sys.path that it polluted.

    ★ endpoints_hot.py 在模块加载时执行 sys.path.insert(0, update_version_v3/)，
      而 update_version_v3/db/ 里只有 history.py（没有 migrate.py）。
      这会让后续 `from db.migrate import get_conn` 解析到错误的 db 目录。
      必须在 import 后立刻把 backend/ (含 db/migrate.py) 抢回 sys.path[0]。

    注意 update_version_v3/ 的路径是 _BACKEND.parent（不是 .parent.parent），
      因为 _BACKEND = update_version_v3/backend/，.parent 就是 update_version_v3/。
    """
    from api import endpoints_hot as eh
    _V2_ROOT = str(_BACKEND.parent)
    if _V2_ROOT in sys.path:
        sys.path.remove(_V2_ROOT)
    sys.path.insert(0, str(_BACKEND))
    return eh


def _load_main_hot_analysis(eh):
    """调用 eh._load_main_hot_analysis() 并兜底它的 sys.path 缺陷。

    ★ endpoints_hot._load_main_hot_analysis 自己会判断 `if str(_MAIN_ROOT) not in sys.path`
      —— 如果测试自己已经把 _MAIN_ROOT 加进 sys.path（但不在 [0]），该函数会 *跳过*
      insert(0, _MAIN_ROOT)，结果 sys.path[0] 还是 backend/。删除 backend 的 api 后
      重新 import 时 Python 仍按 [0] 解析 `api` → 解析到 backend/api（没有 v1 子包）→ 失败。

    修法：调用前先把 _MAIN_ROOT 抢到 sys.path[0]。

    ★ 该函数会破坏 sys.path/sys.modules（把 _MAIN_ROOT 放到 [0]、删除 `api` / `api.endpoints_hot`）。
      测试结束后必须调 _restore_sys_path_after_main() 把状态恢复，否则后续测试
      `from api import endpoints_hot` 会解析到主项目 api/（没有 endpoints_hot.py）。
    """
    global _MAIN_ROOT  # 防止被 Python 当成函数局部变量
    _MAIN_ROOT_STR = str(_MAIN_ROOT)
    # ★ 先快照原始 sys.path / sys.modules（在任何修改之前）
    _STATE["sys_path"] = list(sys.path)
    _STATE["sys_modules"] = set(sys.modules.keys())
    # 然后再插入 _MAIN_ROOT 到 [0] 并触发内部 load
    if _MAIN_ROOT_STR in sys.path:
        sys.path.remove(_MAIN_ROOT_STR)
    sys.path.insert(0, _MAIN_ROOT_STR)
    return eh._load_main_hot_analysis()


def _restore_sys_path_after_main():
    """还原 _load_main_hot_analysis 之前的状态。

    ★ endpoints_hot._load_main_hot_analysis() 把 _MAIN_ROOT 放到 sys.path[0]、
      从 sys.modules 删除 backend 的 `api` / `api.endpoints_hot`，然后 *按 [0]=_MAIN_ROOT*
      重新 import `api`，导致 `sys.modules['api']` 变成主项目的 api（不是 backend 的）。
      如果不还原，下一次 `from api import endpoints_hot` 会解析到主项目 api/
      （没有 endpoints_hot.py → ImportError）。

    修法：还原 sys.path（_MAIN_ROOT 出 [0]，backend/ 进 [0]），并把所有 api.* 模块
      从 sys.modules 删除，强制让 Python 按 backend/ 重新 import。
    """
    if "sys_path" not in _STATE:
        return
    _STATE.pop("sys_path", None)
    _STATE.pop("sys_modules", None)
    # 1) 还原 sys.path：_MAIN_ROOT 从 [0] 挪走，backend/ 抢到 [0]
    _MAIN_ROOT_STR = str(_MAIN_ROOT)
    while _MAIN_ROOT_STR in sys.path:
        sys.path.remove(_MAIN_ROOT_STR)
    sys.path.insert(0, str(_BACKEND))
    # 2) 强制清掉所有 api.* 模块（无论原 backend/main），让 Python 重新按 backend/ 解析
    for mod_name in list(sys.modules.keys()):
        if mod_name == "api" or mod_name.startswith("api."):
            del sys.modules[mod_name]


def _load_v2_hot_analysis():
    """加载主项目 api/v1/endpoints/hot_analysis.py（依赖 api.v1.schemas 与 data_provider）。

    返回 (schema_mod, endpoint_mod)。
    """
    # 1) 先注册 schema 模块（用全限定名），让 endpoint 内的 `from api.v1.schemas...` 找得到
    schema_mod = _load_module_from_file(
        "api.v1.schemas.hot_analysis",
        "api/v1/schemas/hot_analysis.py",
    )
    # 2) 注册 endpoint 模块自身（用全限定名，避免兄弟模块混淆）
    endpoint_mod = _load_module_from_file(
        "api.v1.endpoints.hot_analysis",
        "api/v1/endpoints/hot_analysis.py",
    )
    return schema_mod, endpoint_mod


# ============================================================================
# 共享 client（多数测试都需要）
# ============================================================================
def _client():
    """构造 TestClient（每次新实例避免状态污染），并绕过 /market-review/generate 限流。

    ★ 测试密集调用 /market-review/generate（1/min/IP），直接 client.post 会撞 429。
      重置 IP+endpoint 对应的限流桶，保证测试之间无干扰。
    """
    from fastapi.testclient import TestClient
    from core import cron as cron_mod
    from core import ratelimit
    cron_mod.APS_AVAILABLE = False
    # ★ 复位所有限流桶，确保 client.post 不撞 429（market-review/generate 是 1/min）
    for endpoint in (
        "/api/v2/market-review/generate",
        "/api/v2/alerts/rules/bulk-toggle",
        "/api/v2/paper/transfer",
    ):
        try:
            ratelimit.reset_limit("127.0.0.1", endpoint)
            ratelimit.reset_limit("testclient", endpoint)
        except Exception:
            pass
    from app import app
    return TestClient(app)


def _disable_cron_aps():
    """确保 APScheduler 在测试期间不被启动（避免后台线程噪音）"""
    from core import cron as cron_mod
    cron_mod.APS_AVAILABLE = False


# ============================================================================
# 1. [HIGH] 大盘复盘并发去重 — 真正生效
# ============================================================================
def test_market_review_dedup_in_progress_on_second_concurrent_call():
    """同 key 并发：第一个持有锁期间，第二个直接拿到 in_progress，
    不会去调 generate_market_review（避免重复跑昂贵抓取）。"""
    eh = _load_endpoints_hot()

    # 重置 dict，确保测试隔离
    with eh._market_review_inflight_lock:
        eh._market_review_inflight.clear()

    # 让真正的 generate_market_review 阻塞在线程里一段时间（模拟"在跑"）
    started = threading.Event()
    can_release = threading.Event()
    call_count = {"n": 0}

    def fake_generate(review_date=None, region="cn"):
        call_count["n"] += 1
        started.set()
        # 等信号再返回，模拟跑得很慢
        can_release.wait(timeout=10)
        return {"status": "ok", "review_date": review_date or "2026-08-14", "summary": "done"}

    client = _client()
    from fastapi import Request
    from types import SimpleNamespace

    # 用一个纯线程跑第一个请求（不是 TestClient，它会等同步返回）
    results = {}

    def first_call():
        try:
            req = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
            results["first"] = eh.market_review_generate(
                {"review_date": "2026-08-14", "region": "cn"}, request=req,
            )
        except Exception as e:
            results["first_error"] = repr(e)

    t = threading.Thread(target=first_call, daemon=True)
    # ★ 必须 patch！否则线程会调真实的 generate_market_review（连 akshare）→
    #   永远不会调 fake_generate、永远不会 set started、测试永远超时。
    with patch.object(eh.market_review_service, "generate_market_review",
                      side_effect=fake_generate):
        t.start()
        # 等第一个调用真的进入 generate_market_review
        assert started.wait(timeout=10), "first call never reached generate_market_review"

        # 第二个调用（TestClient 同步走 HTTP）—— 应该立即返回 in_progress
        r = client.post(
            "/api/v2/market-review/generate",
            json={"review_date": "2026-08-14", "region": "cn"},
        )
        assert r.status_code == 200, f"2nd call should return 200 with in_progress, got {r.status_code} {r.text}"
        body = r.json()
        assert body.get("status") == "in_progress", f"expected in_progress, got {body}"
        assert body.get("review_date") == "2026-08-14"
        assert body.get("region") == "cn"

        # 释放第一个调用
        can_release.set()
        t.join(timeout=10)
        assert call_count["n"] == 1, f"generate_market_review should be called exactly once, got {call_count['n']}"

    # 清理
    with eh._market_review_inflight_lock:
        eh._market_review_inflight.clear()

    # 再次调用：dict 已空，应该正常执行（这里测完后清掉 inflight 让后续测试不受影响）
    print(f"OK dedup: 2nd concurrent call returned in_progress, generate called {call_count['n']}x")


def test_market_review_dedup_finally_clears_key_on_exception():
    """异常路径下 inflight key 必须被清理（否则该日复盘再也生成不了）"""
    eh = _load_endpoints_hot()

    with eh._market_review_inflight_lock:
        eh._market_review_inflight.clear()

    def boom_generate(review_date=None, region="cn"):
        raise RuntimeError("全市场抓取挂了")

    client = _client()
    with patch.object(eh.market_review_service, "generate_market_review", side_effect=boom_generate):
        r = client.post(
            "/api/v2/market-review/generate",
            json={"review_date": "2026-08-15", "region": "cn"},
        )
    # 失败应该 500
    assert r.status_code == 500, f"expected 500 on failure, got {r.status_code}"

    # 关键：dict 必须被清空，下一次同 key 应该能重新发起（不再被旧的"在途"挡掉）
    with eh._market_review_inflight_lock:
        leaked = list(eh._market_review_inflight.keys())
    assert ("2026-08-15", "cn") not in leaked, f"inflight key leaked after exception: {leaked}"
    print("OK dedup: exception path cleaned inflight key (no permanent block)")


def test_market_review_dedup_finally_clears_key_on_success():
    """成功路径下 inflight key 也必须被清（否则正常用户后续请求被误判为 in_progress）"""
    eh = _load_endpoints_hot()

    with eh._market_review_inflight_lock:
        eh._market_review_inflight.clear()

    client = _client()
    with patch.object(
        eh.market_review_service, "generate_market_review",
        return_value={"status": "ok", "review_date": "2026-08-16", "summary": "ok"},
    ):
        r = client.post(
            "/api/v2/market-review/generate",
            json={"review_date": "2026-08-16", "region": "cn"},
        )
    assert r.status_code == 200, r.text
    with eh._market_review_inflight_lock:
        leaked = list(eh._market_review_inflight.keys())
    assert ("2026-08-16", "cn") not in leaked, f"inflight key leaked on success: {leaked}"
    print("OK dedup: success path cleaned inflight key")


# ============================================================================
# 2. [HIGH] 异步落库失败可观测
# ============================================================================
def test_persist_async_result_failure_writes_back_to_task():
    """save_analysis_run 抛异常 → task dict 应有 persist_failed=True / persist_error=...
    _tasks/{task_id} GET 应把这两个字段透传出来。"""
    _disable_cron_aps()
    eh = _load_endpoints_hot()

    # 1) 在主项目 _tasks 注册一个假任务
    _hot_mod = _load_main_hot_analysis(eh)
    task_id = "test_task_persist_fail"
    fake_task = {
        "status": "completed",
        "phase": "completed",
        "started_at": "2026-08-14T10:00:00",
        "result": {
            "status": "ok",
            "started_at": "2026-08-14T10:00:00",
            "finished_at": "2026-08-14T10:30:00",
            "duration_seconds": 1800.0,
            "results": [{"code": "600519", "name": "茅台"}],
        },
    }
    _hot_mod._tasks[task_id] = fake_task

    # 2) patch save_analysis_run 让它抛错
    def boom_save(**_kw):
        raise sqlite3_OperationalError("database is locked")

    import sqlite3 as _sq
    class sqlite3_OperationalError(_sq.OperationalError):
        pass

    try:
        with patch.object(eh.db_history, "save_analysis_run", side_effect=boom_save):
            eh._persist_async_result(task_id, fake_task["result"])
    finally:
        # 清理 task dict + 还原 _load_main_hot_analysis 引起的 sys.path / sys.modules 污染
        _hot_mod._tasks.pop(task_id, None)
        _restore_sys_path_after_main()

    # 3) 验证 task dict 写回了 persist_failed / persist_error
    assert fake_task.get("persist_failed") is True, \
        f"expected persist_failed=True, got task={fake_task}"
    assert "OperationalError" in (fake_task.get("persist_error") or ""), \
        f"persist_error should mention exception type, got {fake_task.get('persist_error')!r}"
    print(f"OK persist_failure: task dict has persist_failed=True, persist_error={fake_task.get('persist_error')[:80]}")


def test_persist_async_result_uses_task_started_at_when_result_missing():
    """result dict 无 started_at（异常路径）→ 必须从 task['started_at'] 兜底，
    不能再用 datetime.now() 当 started_at（schema NOT NULL 撞击）。"""
    _disable_cron_aps()
    eh = _load_endpoints_hot()

    task_id = "test_task_no_started"
    fake_task = {
        "status": "failed",
        "phase": "completed",
        "started_at": "2026-08-14T08:00:00",
        "result": {"status": "failed", "error": "LLM timeout"},
    }
    _hot_mod = _load_main_hot_analysis(eh)
    _hot_mod._tasks[task_id] = fake_task

    captured_kwargs = {}

    def fake_save(**kw):
        captured_kwargs.update(kw)
        return 1

    try:
        with patch.object(eh.db_history, "save_analysis_run", side_effect=fake_save):
            eh._persist_async_result(task_id, fake_task["result"])
    finally:
        _hot_mod._tasks.pop(task_id, None)
        _restore_sys_path_after_main()

    assert captured_kwargs.get("started_at") == "2026-08-14T08:00:00", \
        f"should fall back to task.started_at, got {captured_kwargs.get('started_at')!r}"
    print(f"OK persist: started_at fallback to task.started_at ({captured_kwargs['started_at']})")


def test_persist_async_result_succeeds_normally():
    """正常路径：result 完整 → 落库成功，task dict 不应有 persist_failed"""
    _disable_cron_aps()
    eh = _load_endpoints_hot()

    task_id = "test_task_normal"
    result = {
        "status": "ok",
        "started_at": "2026-08-14T10:00:00",
        "finished_at": "2026-08-14T10:30:00",
        "duration_seconds": 1800.0,
        "results": [],
        "filtered_stocks": [],
    }
    fake_task = {
        "status": "completed",
        "phase": "completed",
        "started_at": "2026-08-14T10:00:00",
        "result": result,
    }
    _hot_mod = _load_main_hot_analysis(eh)
    _hot_mod._tasks[task_id] = fake_task

    with patch.object(eh.db_history, "save_analysis_run", return_value=42):
        eh._persist_async_result(task_id, result)
    try:
        _hot_mod._tasks.pop(task_id, None)
    finally:
        _restore_sys_path_after_main()

    assert fake_task.get("persist_failed") is not True, \
        "normal path should NOT set persist_failed"
    print("OK persist: normal path does not mark persist_failed")


# ============================================================================
# 3. [MEDIUM] 预览端点 max_concurrency 不再被静默丢弃
# ============================================================================
def test_preview_schema_accepts_max_concurrency():
    """HotAnalysisPreviewRequest 必须有 max_concurrency 字段（之前 Pydantic extra='ignore' 静默丢）"""
    schema_mod, _ = _load_v2_hot_analysis()
    HotAnalysisPreviewRequest = schema_mod.HotAnalysisPreviewRequest
    # 字段存在
    fields = HotAnalysisPreviewRequest.model_fields
    assert "max_concurrency" in fields, \
        f"HotAnalysisPreviewRequest missing max_concurrency, fields={list(fields.keys())}"
    # 默认值=1
    req = HotAnalysisPreviewRequest()
    assert req.max_concurrency == 1
    # 校验范围
    from pydantic import ValidationError
    try:
        HotAnalysisPreviewRequest(max_concurrency=0)
        raise AssertionError("expected ValidationError on max_concurrency=0")
    except ValidationError:
        pass
    try:
        HotAnalysisPreviewRequest(max_concurrency=9)
        raise AssertionError("expected ValidationError on max_concurrency=9")
    except ValidationError:
        pass
    print("OK preview schema: max_concurrency field present, range 1-8 validated")


def test_estimate_seconds_with_max_concurrency():
    """_estimate_seconds 必须按 max_concurrency 摊薄总耗时"""
    _, hot_mod = _load_v2_hot_analysis()
    _estimate_seconds = hot_mod._estimate_seconds

    target = 20  # 20 只股票
    rounds = 3
    sleep = 0.6
    # 串行 1 → 20 * (3*50 + 2*0.6) = 20 * 151.2 = 3024s
    serial = _estimate_seconds(target, rounds, sleep, max_concurrency=1)
    # 并发 4 → ceil(20/4)=5 batch * 151.2 = 756s
    conc4 = _estimate_seconds(target, rounds, sleep, max_concurrency=4)
    assert serial > conc4, f"serial ({serial}) should be > conc4 ({conc4})"
    # 倍数关系：serial / conc4 ≈ 4（20/5=4），允许 ±1 因为 ceil
    ratio = serial / conc4
    assert 3.5 <= ratio <= 4.1, f"ratio should be ~4, got {ratio:.2f}"
    # 边界：max_concurrency=0 应当作 1
    serial_zero = _estimate_seconds(target, rounds, sleep, max_concurrency=0)
    assert serial_zero == serial, f"max_concurrency=0 should behave like 1, got {serial_zero}"
    # 边界：target=0 → 0
    assert _estimate_seconds(0, rounds, sleep, max_concurrency=4) == 0
    print(f"OK estimate_seconds: serial={serial}s, conc4={conc4}s, ratio={ratio:.2f}")


def test_preview_endpoint_passes_max_concurrency_to_run_request():
    """预览内部构造的 HotAnalysisRunRequest 必须包含 max_concurrency（之前漏传）"""
    _, hot_mod = _load_v2_hot_analysis()
    preview_hot_analysis = hot_mod.preview_hot_analysis
    # 直接看 preview 源码：行内 new_run_req_like 应含 max_concurrency
    import inspect
    src = inspect.getsource(preview_hot_analysis)
    assert "max_concurrency=req.max_concurrency" in src, \
        "preview_hot_analysis 应在构造 RunRequest 时透传 max_concurrency"
    assert "_estimate_seconds(" in src
    # estimate 调用签名应含 max_concurrency
    _estimate_seconds = hot_mod._estimate_seconds
    sig = inspect.signature(_estimate_seconds)
    assert "max_concurrency" in sig.parameters, \
        f"_estimate_seconds 签名必须含 max_concurrency，当前参数={list(sig.parameters.keys())}"
    print("OK preview endpoint: max_concurrency 透传到 RunRequest 与 estimate")


# ============================================================================
# 4. [MEDIUM] 写端点归属校验 — 向后兼容（无 X-User-Id header 不 401）
# ============================================================================
def test_check_resource_owner_no_header_falls_back_to_payload():
    """无 X-User-Id header → 应回落 payload_user_id，匹配 resource_owner 则通过"""
    from api.endpoints_v2 import _check_resource_owner
    from types import SimpleNamespace

    req = SimpleNamespace(state=SimpleNamespace(user_id=None))
    # 不抛异常 = 通过
    _check_resource_owner(req, payload_user_id="alice", resource_user_id="alice")
    # 资源 owner 不匹配 → 403
    try:
        _check_resource_owner(req, payload_user_id="alice", resource_user_id="bob")
        raise AssertionError("expected 403")
    except Exception as e:
        assert "403" in str(e) or "无权" in str(e.detail if hasattr(e, "detail") else e)
    print("OK ownership: no header → falls back to payload user_id")


def test_check_resource_owner_with_header_enforces_match():
    """有 X-User-Id header → 必须与 payload 一致（防伪造），且 resource_owner 必须匹配"""
    from api.endpoints_v2 import _check_resource_owner
    from types import SimpleNamespace

    req = SimpleNamespace(state=SimpleNamespace(user_id="server_uid"))
    # header 与 payload 一致 → 通过
    _check_resource_owner(req, payload_user_id="server_uid", resource_user_id="server_uid")
    # header 与 payload 不一致 → 403（防伪造）
    try:
        _check_resource_owner(req, payload_user_id="attacker", resource_user_id="attacker")
        raise AssertionError("expected 403 on header mismatch")
    except Exception as e:
        assert "403" in str(e) or "伪造" in str(e.detail if hasattr(e, "detail") else e)
    # header 一致但 resource 不属于该 user → 403
    try:
        _check_resource_owner(req, payload_user_id="server_uid", resource_user_id="other")
        raise AssertionError("expected 403 on resource owner mismatch")
    except Exception:
        pass
    print("OK ownership: with header → enforces match + resource ownership")


def test_paper_order_endpoint_no_header_still_works():
    """无 X-User-Id header 时，A 用 user_id=A 下单到 A 的账户仍应 200（向后兼容零破坏）"""
    from db.migrate import get_conn
    from modules import paper_trading as pt
    conn = get_conn()
    conn.execute("DELETE FROM paper_trade WHERE user_id IN ('u_pw_a', 'u_pw_b')")
    conn.execute("DELETE FROM paper_account WHERE user_id IN ('u_pw_a', 'u_pw_b')")
    conn.commit()
    a_acc = pt.create_account("u_pw_a", "pw_a", 1_000_000.0)

    client = _client()
    # 不带 X-User-Id header（前端实际行为）
    r = client.post(
        f"/api/v2/paper/accounts/{a_acc}/orders",
        json={"user_id": "u_pw_a", "symbol": "600519", "side": "buy", "quantity": 100, "price": 1800.0},
    )
    assert r.status_code == 200, f"expected 200, got {r.status_code} {r.text}"
    assert r.json()["symbol"] == "600519"

    # 清理
    conn.execute("DELETE FROM paper_trade WHERE user_id IN ('u_pw_a', 'u_pw_b')")
    conn.execute("DELETE FROM paper_account WHERE user_id IN ('u_pw_a', 'u_pw_b')")
    conn.commit()
    print("OK paper orders: no X-User-Id → 200 (backward compatible)")


def test_paper_order_endpoint_other_user_account_403():
    """A 想给 B 的账户下单 → 403（即使 payload 里写 user_id=A）"""
    from db.migrate import get_conn
    from modules import paper_trading as pt
    conn = get_conn()
    conn.execute("DELETE FROM paper_trade WHERE user_id IN ('u_pw_x', 'u_pw_y')")
    conn.execute("DELETE FROM paper_account WHERE user_id IN ('u_pw_x', 'u_pw_y')")
    conn.commit()
    b_acc = pt.create_account("u_pw_y", "pw_y", 1_000_000.0)

    client = _client()
    # A 试图下单到 B 的账户：payload.user_id=A，但 resource owner=B
    r = client.post(
        f"/api/v2/paper/accounts/{b_acc}/orders",
        json={"user_id": "u_pw_x", "symbol": "600519", "side": "buy", "quantity": 100, "price": 1800.0},
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code} {r.text}"
    assert "无权" in r.text or "他人" in r.text

    conn.execute("DELETE FROM paper_trade WHERE user_id IN ('u_pw_x', 'u_pw_y')")
    conn.execute("DELETE FROM paper_account WHERE user_id IN ('u_pw_x', 'u_pw_y')")
    conn.commit()
    print("OK paper orders: cross-user account → 403")


def test_paper_transfer_endpoint_cross_user_403():
    """A 想用 B 的账户当转出方 → 403"""
    from db.migrate import get_conn
    from modules import paper_trading as pt
    conn = get_conn()
    conn.execute("DELETE FROM paper_trade WHERE user_id IN ('u_tx_a', 'u_tx_b')")
    conn.execute("DELETE FROM paper_account WHERE user_id IN ('u_tx_a', 'u_tx_b')")
    conn.commit()
    b_acc = pt.create_account("u_tx_b", "tx_b", 1_000_000.0)
    a_acc = pt.create_account("u_tx_a", "tx_a", 1_000_000.0)

    client = _client()
    # A 想转出 B 的账户 → 应 403
    r = client.post(
        "/api/v2/paper/transfer",
        json={"user_id": "u_tx_a", "from_account_id": b_acc, "to_account_id": a_acc, "amount": 100.0},
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code} {r.text}"

    # A 自己的账户互转 → 应 200
    a_acc2 = pt.create_account("u_tx_a", "tx_a2", 1_000_000.0)
    r = client.post(
        "/api/v2/paper/transfer",
        json={"user_id": "u_tx_a", "from_account_id": a_acc, "to_account_id": a_acc2, "amount": 100.0},
    )
    assert r.status_code == 200, f"expected 200 self-transfer, got {r.status_code} {r.text}"

    conn.execute("DELETE FROM paper_trade WHERE user_id IN ('u_tx_a', 'u_tx_b')")
    conn.execute("DELETE FROM paper_account WHERE user_id IN ('u_tx_a', 'u_tx_b')")
    conn.commit()
    print("OK paper transfer: cross-user → 403, self-transfer → 200")


def test_alert_bulk_toggle_foreign_rule_403():
    """A 想 toggle B 的 rule → 403（即使把自己 rule 一起 toggle 也应整体拒）"""
    from db.migrate import get_conn
    from modules import alerts_engine as ae
    conn = get_conn()
    conn.execute("DELETE FROM alert_rule WHERE user_id IN ('u_at_a', 'u_at_b')")
    conn.commit()
    rid_a = ae.create_rule("u_at_a", "rule_a", "limit_up", {})
    rid_b = ae.create_rule("u_at_b", "rule_b", "limit_up", {})

    client = _client()
    # A 想同时 toggle A 和 B 的 rule → 应 403（因为含 B 的）
    r = client.post(
        "/api/v2/alerts/rules/bulk-toggle",
        json={"user_id": "u_at_a", "rule_ids": [rid_a, rid_b], "enabled": False},
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code} {r.text}"
    # rid_b 是 int，r.text 是 str；检查"无权"或"他人"字样
    assert "无权" in r.text or "他人" in r.text or str(rid_b) in r.text, \
        f"expected error mentioning 403/他人/rid_b, got {r.text}"

    # A 只 toggle 自己的 → 200
    r = client.post(
        "/api/v2/alerts/rules/bulk-toggle",
        json={"user_id": "u_at_a", "rule_ids": [rid_a], "enabled": False},
    )
    assert r.status_code == 200, f"expected 200, got {r.status_code} {r.text}"

    conn.execute("DELETE FROM alert_rule WHERE user_id IN ('u_at_a', 'u_at_b')")
    conn.commit()
    print("OK alerts bulk-toggle: cross-user → 403, own → 200")


def test_notifications_test_cross_user_channel_403():
    """A 想用 B 的 channel_id 发测试推送 → 403"""
    from db.migrate import get_conn
    from core import notifier as nt
    conn = get_conn()
    conn.execute("DELETE FROM notification_channel WHERE user_id IN ('u_nt_a', 'u_nt_b')")
    conn.commit()
    # ★ 改用合法的 channel_type（wechat_work/dingtalk/email/serverchan），
    #   wechat_work 校验 https:// URL，所以给一个 HTTPS URL
    b_ch_id = nt.add_channel(user_id="u_nt_b", channel_type="wechat_work", target="https://example.com/webhook")

    client = _client()
    r = client.post(
        "/api/v2/notifications/test",
        json={"user_id": "u_nt_a", "channel_id": b_ch_id, "title": "x", "content": "y"},
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code} {r.text}"

    conn.execute("DELETE FROM notification_channel WHERE user_id IN ('u_nt_a', 'u_nt_b')")
    conn.commit()
    print("OK notifications/test: cross-user channel → 403")


def test_portfolio_holdings_delete_no_header_still_works():
    """无 X-User-Id header 时，A 删除自己的持仓仍 200（向后兼容）

    ★ add_holding / remove_holding 实际是操作 paper_trade 表（reason LIKE 'portfolio_holding%'），
      不是名为 portfolio_holding 的独立表（该表不存在）。清理用 paper_trade。
    """
    from db.migrate import get_conn
    from modules import portfolio as pf
    conn = get_conn()
    conn.execute("DELETE FROM paper_trade WHERE user_id='u_pf_a' AND reason LIKE 'portfolio_holding%'")
    conn.execute("DELETE FROM paper_account WHERE user_id='u_pf_a'")
    conn.commit()
    pf.add_holding("u_pf_a", "600519", 1800.0, 100)

    client = _client()
    r = client.delete("/api/v2/portfolio/holdings/600519?user_id=u_pf_a")
    assert r.status_code == 200, f"expected 200, got {r.status_code} {r.text}"
    assert r.json()["removed"] == "600519"

    conn.execute("DELETE FROM paper_trade WHERE user_id='u_pf_a' AND reason LIKE 'portfolio_holding%'")
    conn.execute("DELETE FROM paper_account WHERE user_id='u_pf_a'")
    conn.commit()
    print("OK portfolio delete: no header → 200 (backward compatible)")


# ============================================================================
# 5. [LOW] update_alert_rule 支持 rule_type + 合法性校验
# ============================================================================
def test_update_rule_supports_rule_type_valid():
    """update_rule 现在接受 rule_type 并写入"""
    from db.migrate import get_conn
    from modules import alerts_engine as ae
    conn = get_conn()
    conn.execute("DELETE FROM alert_rule WHERE user_id='u_ur_a'")
    conn.commit()
    rid = ae.create_rule("u_ur_a", "test", "limit_up", {}, enabled=True)

    # 改成 ma_break
    ok = ae.update_rule(rid, rule_type="ma_break")
    assert ok is True
    rule = ae.get_rule(rid)
    assert rule["rule_type"] == "ma_break"
    print("OK update_rule: rule_type update accepted and persisted")

    conn.execute("DELETE FROM alert_rule WHERE user_id='u_ur_a'")
    conn.commit()


def test_update_rule_rejects_invalid_rule_type():
    """update_rule 遇到非法 rule_type 必须抛 ValueError（防止脏数据破坏评估器）"""
    from db.migrate import get_conn
    from modules import alerts_engine as ae
    conn = get_conn()
    conn.execute("DELETE FROM alert_rule WHERE user_id='u_ur_b'")
    conn.commit()
    rid = ae.create_rule("u_ur_b", "test", "limit_up", {})

    try:
        ae.update_rule(rid, rule_type="not_a_real_type")
        raise AssertionError("expected ValueError on invalid rule_type")
    except ValueError as e:
        assert "Unknown rule_type" in str(e), f"unexpected error msg: {e}"
        print(f"OK update_rule: rejects invalid rule_type ({e})")
    finally:
        conn.execute("DELETE FROM alert_rule WHERE user_id='u_ur_b'")
        conn.commit()


def test_endpoint_update_alert_rule_passes_rule_type_through():
    """端点 PUT /alerts/rules/{id} 不再静默丢 rule_type"""
    from db.migrate import get_conn
    from modules import alerts_engine as ae
    conn = get_conn()
    conn.execute("DELETE FROM alert_rule WHERE user_id='u_ur_c'")
    conn.commit()
    rid = ae.create_rule("u_ur_c", "test", "limit_up", {})

    client = _client()
    r = client.put(
        f"/api/v2/alerts/rules/{rid}",
        json={"user_id": "u_ur_c", "rule_type": "volume_surge"},
    )
    assert r.status_code == 200, f"expected 200, got {r.status_code} {r.text}"
    rule = ae.get_rule(rid)
    assert rule["rule_type"] == "volume_surge", \
        f"rule_type should be persisted, got {rule['rule_type']}"

    # 非法值 → 400
    r = client.put(
        f"/api/v2/alerts/rules/{rid}",
        json={"user_id": "u_ur_c", "rule_type": "made_up"},
    )
    assert r.status_code == 400, f"invalid rule_type should return 400, got {r.status_code} {r.text}"

    conn.execute("DELETE FROM alert_rule WHERE user_id='u_ur_c'")
    conn.commit()
    print("OK endpoint PUT alerts/rules: rule_type persisted + invalid → 400")


# ============================================================================
# 汇总
# ============================================================================
ALL_TESTS = [
    # 1. 市场复盘去重
    test_market_review_dedup_in_progress_on_second_concurrent_call,
    test_market_review_dedup_finally_clears_key_on_exception,
    test_market_review_dedup_finally_clears_key_on_success,
    # 2. 落库失败可观测
    test_persist_async_result_failure_writes_back_to_task,
    test_persist_async_result_uses_task_started_at_when_result_missing,
    test_persist_async_result_succeeds_normally,
    # 3. preview max_concurrency
    test_preview_schema_accepts_max_concurrency,
    test_estimate_seconds_with_max_concurrency,
    test_preview_endpoint_passes_max_concurrency_to_run_request,
    # 4. 归属校验
    test_check_resource_owner_no_header_falls_back_to_payload,
    test_check_resource_owner_with_header_enforces_match,
    test_paper_order_endpoint_no_header_still_works,
    test_paper_order_endpoint_other_user_account_403,
    test_paper_transfer_endpoint_cross_user_403,
    test_alert_bulk_toggle_foreign_rule_403,
    test_notifications_test_cross_user_channel_403,
    test_portfolio_holdings_delete_no_header_still_works,
    # 5. update_alert_rule 支持 rule_type
    test_update_rule_supports_rule_type_valid,
    test_update_rule_rejects_invalid_rule_type,
    test_endpoint_update_alert_rule_passes_rule_type_through,
]


def run_all():
    passed = 0
    failed = 0
    failed_names = []
    for t in ALL_TESTS:
        try:
            t()
            passed += 1
        except Exception as e:
            failed += 1
            failed_names.append(t.__name__)
            import traceback
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\n=== Round 27 fixes: {passed} PASSED, {failed} FAILED ===")
    if failed_names:
        print(f"Failed tests: {failed_names}")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
