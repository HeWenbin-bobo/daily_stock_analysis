"""Lightweight perf benchmark against a live backend.

Measures p50/p95/p99 latency for each public endpoint, both cold (fresh cache)
and warm (cached). Reports cache hit rate after the run.

Usage: python scripts/perf_bench.py [--host http://127.0.0.1:8100] [--n 100]
"""
import sys
import time
import statistics
import argparse
import json
from pathlib import Path

import urllib.request
import urllib.error


def http_get(url: str, timeout: float = 30.0) -> tuple[int, float]:
    """Returns (status_code, elapsed_ms)."""
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = r.status
            r.read()
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            e.read()
        except Exception:
            pass
    except Exception as e:
        print(f"  [ERROR] {e}")
        return 0, 0.0
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return status, elapsed_ms


def http_post(url: str, body: dict, timeout: float = 30.0) -> tuple[int, float]:
    t0 = time.perf_counter()
    try:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = r.status
            r.read()
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            e.read()
        except Exception:
            pass
    except Exception as e:
        print(f"  [ERROR] {e}")
        return 0, 0.0
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return status, elapsed_ms


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] * (c - k) + s[c] * (k - f)


def bench(label: str, fn, n: int) -> dict:
    print(f"\n--- {label} (n={n}) ---")
    times = []
    statuses = []
    for _ in range(n):
        status, ms = fn()
        times.append(ms)
        statuses.append(status)
    if not times:
        return {"label": label, "n": 0}
    ok = sum(1 for s in statuses if 200 <= s < 400)
    err = sum(1 for s in statuses if s == 0 or s >= 500)
    return {
        "label": label,
        "n": n,
        "ok": ok,
        "err": err,
        "p50_ms": round(percentile(times, 0.50), 2),
        "p95_ms": round(percentile(times, 0.95), 2),
        "p99_ms": round(percentile(times, 0.99), 2),
        "max_ms": round(max(times), 2),
        "avg_ms": round(statistics.mean(times), 2),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="http://127.0.0.1:8100")
    parser.add_argument("--n", type=int, default=100)
    args = parser.parse_args()

    base = args.host
    n = args.n

    print(f"Host: {base}, n={n} per endpoint")

    # Warm up caches (1 call each)
    print("\n--- warm-up ---")
    http_get(f"{base}/api/v1/stocks/600519/history?period=daily&days=180")
    http_post(f"{base}/api/v1/analysis/peak-trough",
              {"stock_code": "600519", "period": "daily", "days": 180})
    http_get(f"{base}/api/v1/stocks/600519/indicators/MACD?period=daily&days=180")
    http_get(f"{base}/api/v1/stocks/search?q=600519")

    results = []
    results.append(bench(
        "GET /api/v1/stocks/600519/history?period=daily&days=180 (warm)",
        lambda: http_get(f"{base}/api/v1/stocks/600519/history?period=daily&days=180"),
        n,
    ))
    results.append(bench(
        "GET /api/v1/stocks/AAPL/history?period=weekly&days=365 (warm)",
        lambda: http_get(f"{base}/api/v1/stocks/AAPL/history?period=weekly&days=365"),
        n,
    ))
    results.append(bench(
        "GET /api/v1/stocks/00700/history?period=monthly&days=730 (warm)",
        lambda: http_get(f"{base}/api/v1/stocks/00700/history?period=monthly&days=730"),
        n,
    ))
    results.append(bench(
        "POST /api/v1/analysis/peak-trough (warm)",
        lambda: http_post(f"{base}/api/v1/analysis/peak-trough",
                          {"stock_code": "600519", "period": "daily", "days": 180}),
        n,
    ))
    results.append(bench(
        "GET /api/v1/stocks/600519/indicators/MACD (warm)",
        lambda: http_get(f"{base}/api/v1/stocks/600519/indicators/MACD?period=daily&days=180"),
        n,
    ))
    results.append(bench(
        "GET /api/v1/stocks/search?q=600519 (warm)",
        lambda: http_get(f"{base}/api/v1/stocks/search?q=600519"),
        n,
    ))
    results.append(bench(
        "GET /health",
        lambda: http_get(f"{base}/health"),
        n,
    ))

    # Cold cache: each call uses a unique stock code (forces real fetch)
    # NOTE: under FORCE_MOCK=1 these are still fast; under FORCE_MOCK=0
    # they would hit real APIs. We measure cold-cache by clearing first.
    print("\n--- cold-cache pass (reset caches between calls) ---")
    # Reset kline cache via admin endpoint
    try:
        req = urllib.request.Request(f"{base}/admin/stats/reset", method="POST")
        req.add_header("Authorization", "Bearer dev-admin-key")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"  [skip] reset caches: {e}")

    # Run cold-cache: each iteration clears the cache first
    def cold_history():
        try:
            req = urllib.request.Request(f"{base}/admin/stats/reset", method="POST")
            req.add_header("Authorization", "Bearer dev-admin-key")
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass
        return http_get(f"{base}/api/v1/stocks/600519/history?period=daily&days=180")

    results.append(bench(
        "GET /history COLD-CACHE (cache reset each call)",
        cold_history,
        10,  # fewer iterations since each call is slow
    ))

    # Print results table
    print("\n=== Results ===")
    print(f"{'Endpoint':<70} {'n':>5} {'OK':>5} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>8}")
    for r in results:
        print(f"{r['label']:<70} {r['n']:>5} {r['ok']:>5} {r['p50_ms']:>8} {r['p95_ms']:>8} {r['p99_ms']:>8} {r['max_ms']:>8}")

    # Fetch final /admin/stats for cache stats
    print("\n--- Final cache + rate limit stats ---")
    try:
        req = urllib.request.Request(f"{base}/admin/stats")
        req.add_header("Authorization", "Bearer dev-admin-key")
        with urllib.request.urlopen(req, timeout=10) as r:
            stats = json.loads(r.read())
        print(json.dumps(stats, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"  [skip] admin/stats requires auth: {e}")

    # Save results
    out_path = Path("F:/stock_analysis/v1/_perf_results.json")
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    sys.exit(main() or 0)
