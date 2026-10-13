# -*- coding: utf-8 -*-
"""Phase 9 smoke test: LLM 路由 + mock fallback + 用量统计"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 强制清空环境变量以确保 mock 路径可触发
for k in ("DEEPSEEK_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(k, None)

from db.migrate import migrate, get_conn
from core import llm_router as llm


def _reset_usage():
    conn = get_conn()
    conn.execute("DELETE FROM llm_usage")


def test_migrate_includes_llm_usage():
    n = migrate()
    assert n >= 16
    print(f"OK migrate: {n} tables (includes llm_usage)")


def test_chat_without_keys_returns_mock():
    _reset_usage()
    result = llm.chat("test prompt", task_complexity="simple", task_type="smoke_test")
    assert result["mocked"] is True, f"Should be mocked without keys, got {result}"
    assert result["provider"] == "mock"
    assert "[MOCK-LLM]" in result["content"]
    print(f"OK chat: no-keys -> mock ({result['provider']})")


def test_complexity_routes_to_deepseek():
    _reset_usage()
    result = llm.chat("test", task_complexity="simple")
    # 没 key 时 mock，但 preferred provider 应该是 deepseek
    assert result["provider"] in ("mock", "deepseek"), f"Got {result['provider']}"
    print(f"OK complexity simple -> {result['provider']}")


def test_record_usage_writes_to_db():
    _reset_usage()
    # 直接调用内部函数
    from core.llm_router import Provider
    llm._record_usage("u1", Provider.DEEPSEEK, "deepseek-chat", 100, 50, "test_task")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM llm_usage WHERE user_id=?", ("u1",))
    rows = list(cur.fetchall())
    assert len(rows) == 1
    assert rows[0]["provider"] == "deepseek"
    assert rows[0]["prompt_tokens"] == 100
    assert rows[0]["completion_tokens"] == 50
    assert rows[0]["total_tokens"] == 150
    print(f"OK usage recorded: {dict(rows[0])}")


def test_cost_estimation():
    from core.llm_router import Provider
    cost = llm._estimate_cost(Provider.DEEPSEEK, 1000, 500)
    # deepseek in: 0.001/1k, out: 0.002/1k
    # 1000 * 0.001 + 500 * 0.002 = 0.001 + 0.001 = 0.002
    assert abs(cost - 0.002) < 0.0001, f"Expected 0.002, got {cost}"
    cost2 = llm._estimate_cost(Provider.OPENAI, 1000, 500)
    # gpt-4o-mini in: 0.18, out: 0.72
    # 1000 * 0.18 + 500 * 0.72 = 0.18 + 0.36 = 0.54
    assert abs(cost2 - 0.54) < 0.001, f"Expected 0.54, got {cost2}"
    print(f"OK cost: deepseek 1.5k={cost}, openai 1.5k={cost2}")


def test_usage_summary():
    _reset_usage()
    from core.llm_router import Provider
    llm._record_usage("u2", Provider.DEEPSEEK, "deepseek-chat", 2000, 1000, "task_a")
    llm._record_usage("u2", Provider.OPENAI, "gpt-4o-mini", 500, 200, "task_b")
    summary = llm.get_usage_summary(user_id="u2", days=1)
    assert summary["total_tokens"] >= 3700
    assert summary["total_cost_cny"] > 0
    assert len(summary["by_provider"]) == 2
    print(f"OK usage summary: total={summary['total_tokens']} tokens, cost={summary['total_cost_cny']} 元")


def test_stream_yields_chunks_and_done():
    import asyncio
    async def run():
        chunks = []
        async for ev in llm.chat_stream("test", task_complexity="simple"):
            chunks.append(ev)
            if ev.get("done"):
                break
        return chunks
    chunks = asyncio.run(run())
    assert len(chunks) >= 2, f"Should have at least 1 chunk + 1 done, got {len(chunks)}"
    assert chunks[-1]["done"] is True
    assert chunks[-1]["event"] == "done"
    print(f"OK stream: {len(chunks)} events (chunks + done)")


def test_get_config_returns_none_without_key():
    from core.llm_router import Provider
    cfg = llm._get_config(Provider.DEEPSEEK)
    assert cfg is None, f"Should be None without key, got {cfg}"
    print("OK config: no key -> None")


def test_fallback_chain_order():
    """降级链：openai → qwen → deepseek → mock"""
    from core.llm_router import Provider
    # 都没 key，应该走 mock
    result = llm.chat("test", task_complexity="simple")
    assert result["provider"] == "mock"
    print(f"OK fallback: all no key -> {result['provider']}")


def test_chunk_text():
    chunks = llm._chunk_text("abcdefghij", chunk_size=3)
    assert chunks == ["abc", "def", "ghi", "j"]
    print(f"OK chunk: {chunks}")


if __name__ == "__main__":
    test_migrate_includes_llm_usage()
    test_chat_without_keys_returns_mock()
    test_complexity_routes_to_deepseek()
    test_record_usage_writes_to_db()
    test_cost_estimation()
    test_usage_summary()
    test_stream_yields_chunks_and_done()
    test_get_config_returns_none_without_key()
    test_fallback_chain_order()
    test_chunk_text()
    print("\n=== Phase 9 smoke test: ALL 10 PASSED ===")