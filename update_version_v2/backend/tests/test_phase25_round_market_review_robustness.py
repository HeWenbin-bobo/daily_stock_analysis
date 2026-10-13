# -*- coding: utf-8 -*-
"""第五轮 /loop 迭代5（Round 61）修复回归测试：
固化迭代2/迭代5 的大盘复盘健壮性修复，防止未来回归：

[迭代2 - Round58]
1. 覆盖保护：degraded/failed 生成不覆盖当天已有 ok 记录（坏数据不覆盖好数据）

[迭代3 - Round59]
2. 覆盖保护修正：仅 ok 视为"好数据"需保护，degraded/failed 可互相覆盖更新

[迭代5 - Round61]
3. 状态判定修复：指数成功但宽度（stocks）缺失 → 标 degraded（而非 ok），
   防止 breadth 全 0 的"伪 ok"绕过覆盖保护覆盖好数据

测试全部 mock 数据源，永不联网、永不真跑全市场抓取。
"""
import os
import sys
import importlib.util
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BACKEND = Path(__file__).resolve().parent.parent  # update_version_v2/backend/
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

PASS = 0
FAIL = 0


def check(name: str, cond: bool):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}")


def _load_service():
    """按路径加载 market_review_service（绕过包冲突）。"""
    svc_path = _BACKEND.parent / "services" / "market_review_service.py"
    spec = importlib.util.spec_from_file_location("mrs_round61", str(svc_path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mrs_round61"] = mod
    spec.loader.exec_module(mod)
    return mod


# ============================================================================
# 1. 状态判定修复（迭代5）：宽度缺失 → degraded
# ============================================================================
def test_status_requires_both_indices_and_width():
    print("\n== [迭代5] 状态判定：宽度缺失 → degraded ==")
    mrs = _load_service()

    # 场景A：指数成功 + 宽度失败 → degraded（此前错误标 ok）
    with patch.object(mrs, "_fetch_indices", return_value=({"上证指数": {"price": 3956}}, None)), \
         patch.object(mrs, "_fetch_full_market_snapshot", return_value=([], "all sources failed", 5.0)), \
         patch.object(mrs, "_fetch_sector_board", return_value=([], "fail")):
        payload, warnings = mrs._build_payload("2026-08-27")
        check("指数ok+宽度失败 → degraded", payload["status"] == "degraded")
        check("有宽度缺失警告", any("宽度统计不可用" in w for w in warnings))

    # 场景B：指数成功 + 宽度成功 → ok
    with patch.object(mrs, "_fetch_indices", return_value=({"上证指数": {"price": 3956}}, None)), \
         patch.object(mrs, "_fetch_full_market_snapshot", return_value=([{"code": "1", "change_pct": 1.0}], None, 5.0)), \
         patch.object(mrs, "_fetch_sector_board", return_value=([], "fail")):
        payload, _ = mrs._build_payload("2026-08-27")
        check("指数ok+宽度ok → ok", payload["status"] == "ok")

    # 场景C：指数失败 + 宽度成功 → degraded
    with patch.object(mrs, "_fetch_indices", return_value=({}, "indices fail")), \
         patch.object(mrs, "_fetch_full_market_snapshot", return_value=([{"code": "1", "change_pct": 1.0}], None, 5.0)), \
         patch.object(mrs, "_fetch_sector_board", return_value=([], "fail")):
        payload, _ = mrs._build_payload("2026-08-27")
        check("指数失败+宽度ok → degraded", payload["status"] == "degraded")


# ============================================================================
# 2. 覆盖保护（迭代2/3）：仅 ok 视为好数据需保护
# ============================================================================
def test_overwrite_protection():
    print("\n== [迭代2/3] 覆盖保护：degraded/failed 不覆盖 ok ==")
    mrs = _load_service()

    degraded = {"review_date": "2026-08-27", "region": "cn", "status": "degraded",
                "indices": {}, "breadth": {"advance": 0, "decline": 0},
                "sectors": {}, "signals": {}, "report_markdown": "x",
                "summary": "弱势30", "duration_seconds": 11.0}
    ok_payload = {"review_date": "2026-08-27", "region": "cn", "status": "ok",
                  "indices": {}, "breadth": {"advance": 3000, "decline": 2000},
                  "sectors": {}, "signals": {}, "report_markdown": "y",
                  "summary": "偏暖90", "duration_seconds": 25.0}

    def _run(existing_status, new_payload):
        from unittest.mock import MagicMock
        mock_db = MagicMock()
        mock_db.get_market_review.return_value = (
            {"review_date": "2026-08-27", "status": existing_status, "summary": "x"}
            if existing_status else None
        )
        with patch.object(mrs, "_build_payload", return_value=(new_payload, [])), \
             patch.object(mrs, "datetime") as mock_dt, \
             patch.dict(sys.modules, {"v2_root_db_history": mock_db}):
            mock_dt.now.return_value.strftime.return_value = "2026-08-27"
            mrs.generate_market_review(review_date="2026-08-27", persist=True)
            return mock_db.save_market_review.call_count

    check("已有ok + degraded生成 → 不覆盖(保护)", _run("ok", degraded) == 0)
    check("已有ok + ok生成 → 覆盖更新", _run("ok", ok_payload) == 1)
    check("无记录 + degraded生成 → 落库", _run(None, degraded) == 1)
    check("已有degraded + degraded生成 → 覆盖(无损失)", _run("degraded", degraded) == 1)
    check("已有failed + degraded生成 → 覆盖(占位→有数据)", _run("failed", degraded) == 1)
    check("已有failed + ok生成 → 覆盖", _run("failed", ok_payload) == 1)


if __name__ == "__main__":
    test_status_requires_both_indices_and_width()
    test_overwrite_protection()
    print(f"\n=== Round 61 regression: {PASS} PASSED, {FAIL} FAILED ===")
    sys.exit(1 if FAIL else 0)
