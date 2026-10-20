# -*- coding: utf-8 -*-
"""API 限流：滑动窗口日志（每 IP 每端点每分钟上限）"""
import os
import time
import logging
from collections import defaultdict, deque
from threading import Lock
from typing import Deque, Dict, Tuple

logger = logging.getLogger(__name__)

# 配置：每 (ip, endpoint) 每 N 秒 M 次
LIMITS: Dict[str, Tuple[int, int]] = {
    # (count, window_seconds)
    "default": (60, 60),       # 默认 60 次/分钟
    "/api/v2/notifications/test": (5, 60),  # 推送测试 5 次/分钟
    "/api/v2/backtest/run": (5, 60),        # 回测 5 次/分钟（贵）
    "/api/v2/alerts/rules": (20, 60),       # 异动规则管理
    "/api/v2/alerts/scan": (3, 60),         # 异动扫描 3 次/分钟（很贵）
    "/api/v2/llm/chat": (10, 60),           # LLM 调用 10 次/分钟（贵）
    "/api/v2/webhooks/verify": (20, 60),     # Webhook 验签 20 次/分钟
    # ★ Round 4 新增：昂贵端点限流
    "/api/v2/market-review/generate": (1, 60),       # 单日复盘 1 次/分钟（重）
    "/api/v2/market-review/batch": (1, 600),         # 批量复盘 1 次/10 分钟
    "/api/v2/etf-rotation/run": (2, 300),            # ETF 轮动（数据抓取重）2 次/5 分钟
    "/api/v2/web-intent/resolve": (30, 60),          # 意图解析（可能触发 LLM）30 次/分钟
    # ★ 修复(M7)：删除 alias 路径死配置（/hot-analysis/market-review/* alias 透传
    #   request 到原生函数，限流 key 是 /api/v2/market-review/generate|batch，
    #   下面两条 /api/v2/hot-analysis/... 永远不生效——alias 和原生共享同一桶，
    #   保留只会造成"配置存在但从未生效"的错觉。
    "/api/v2/hot-analysis/run": (1, 60),             # 完整四榜分析 1 次/分钟
    "/api/v2/hot-analysis/run-async": (3, 60),       # 后台任务 3 次/分钟
    "/api/v2/community/hot-list/refresh": (1, 600),  # 全网榜单刷新 1 次/10 分钟
    "/api/v2/cron/user-jobs": (5, 3600),             # cron 注册 5 次/小时（防滥用）
    "/api/v2/cron/user-jobs/add": (5, 3600),
    "/api/v2/cron/trigger": (10, 60),                # 手动触发 10 次/分钟
    "/api/v2/alerts/rules/bulk-toggle": (5, 60),     # 批量启停 5 次/分钟
    "/api/v2/data/quote": (120, 60),                 # 实时行情 120 次/分钟
    "/api/v2/data/kline": (60, 60),                  # K 线 60 次/分钟
    "/api/v2/paper/transfer": (10, 60),              # 转账 10 次/分钟
    "/api/v2/portfolio/rebalance": (5, 60),          # 再平衡建议 5 次/分钟
    # ★ 探索期修复：下面 5 个端点会调 LLM 或抓大量 K 线，但代码里用了这些 key
    #   却从未在 LIMITS 中定义 → 全部落到 "default": 60/min，等于没限流。
    #   实测 deep-research 单次要走 16000 max_tokens 的 LLM 调用，60 次/分钟足以
    #   烧掉大量额度；hyperopt 更是 CPU 密集（最多 5000 epochs）。
    "/api/v2/deep-research": (3, 60),                # 深度研究 3 次/分钟（LLM 重）
    "/api/v2/backtest/hyperopt": (2, 60),            # 参数优化 2 次/分钟（CPU 重）
    "/api/v2/technical/llm-insight": (5, 60),        # AI 解读 5 次/分钟（LLM）
    "/api/v2/technical/indicators": (60, 60),        # 指标计算 60 次/分钟（纯计算）
    "/api/v2/technical/peak-trough": (60, 60),       # 波峰波谷 60 次/分钟（纯计算）
}

_buckets: Dict[Tuple[str, str], Deque[float]] = defaultdict(deque)
_buckets_lock = Lock()
# ★ 修复(L1)：内存桶上限，防止大量攻击性 IP 让 _buckets 只增不减（内存泄漏）
_BUCKETS_MAX = int(os.environ.get("RATELIMIT_BUCKETS_MAX", "50000"))
_last_sweep = time.time()


def _sweep_buckets(now: float) -> None:
    """清空已过期的空桶（每 60s 最多一次，防频繁扫描开销）。

    旧实现：每个桶 deque 会 popleft 清空，但 _buckets 的 key 永不删除，
    长期运行大量攻击性 IP → dict 无限膨胀。
    """
    global _last_sweep
    if now - _last_sweep < 60:
        return
    _last_sweep = now
    empty = [k for k, v in _buckets.items() if not v]
    for k in empty:
        del _buckets[k]


def check_limit(ip: str, endpoint: str) -> Tuple[bool, int]:
    """检查是否超限。返回 (allowed, retry_after_seconds)"""
    key = (ip, endpoint)
    count, window = LIMITS.get(endpoint, LIMITS["default"])
    now = time.time()
    with _buckets_lock:
        _sweep_buckets(now)
        # 超过上限时按 FIFO 淘汰最旧的桶（防内存失控）
        # ★ 探索期修复(Critical)：`_buckets` 是 defaultdict(deque)，不是 OrderedDict。
        #   `dict.popitem(last=False)` 只存在于 OrderedDict —— 撞上 BUCKETS_MAX 时抛
        #   `TypeError: dict.popitem() takes no keyword arguments`，而这里在持锁的
        #   check_limit 内、异常未被捕获 → 之后**每一个**限流端点（quote/backtest/
        #   llm/chat…）全部 500，且只要桶数仍在上限附近就永久不可恢复。
        #   dict 自 3.7 起保插入序，取第一个键删除即可等价 FIFO。
        if len(_buckets) >= _BUCKETS_MAX:
            while _buckets and len(_buckets) >= _BUCKETS_MAX:
                del _buckets[next(iter(_buckets))]
        bucket = _buckets[key]

        # 清掉窗口外的
        while bucket and bucket[0] < now - window:
            bucket.popleft()

        if len(bucket) >= count:
            retry_after = int(window - (now - bucket[0])) + 1
            return False, retry_after

        bucket.append(now)
        return True, 0


def get_client_ip(request) -> str:
    """从 FastAPI Request 拿客户端 IP。

    ★ Round 4 修复：默认不信任 X-Forwarded-For，避免攻击者伪造 IP 绕过限流桶。
    仅当 TRUST_PROXY=1 时才读取 XFF，且取**最右（最靠近服务端）的可信 IP**
    — 标准做法是从右往左跳过所有 XFF 中不可信段。
    """
    peer_ip = request.client.host if request.client else "unknown"
    if os.environ.get("TRUST_PROXY", "").lower() not in ("1", "true", "yes"):
        return peer_ip
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # 取最右侧（最近经过的反代记录的 IP）。如果反代链是 trusted
        # → 攻击者无法在最右侧塞自己的 IP（最右侧是反代自己写的）。
        return forwarded.split(",")[-1].strip()
    return peer_ip


def reset_limit(ip: str, endpoint: str) -> None:
    """重置某 IP 端点的限流（管理用）"""
    key = (ip, endpoint)
    with _buckets_lock:
        if key in _buckets:
            _buckets[key].clear()