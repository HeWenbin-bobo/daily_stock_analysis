# -*- coding: utf-8 -*-
"""
自适应并发节流器 —— 网络抖动降级

用于数据源并发抓取：当连续失败次数超过阈值（网络抖动 / 反爬 / 并发过高），
自动把并发上限降一级；持续成功后缓慢恢复。线程安全，供线程池内调用。

设计要点：
- acquire 带超时兜底：即使并发上限已满也不无限阻塞业务（避免长任务饿死）。
- 失败计数按"单次数据需求全部数据源失败"计（不是单次 HTTP 失败），
  避免把一个数据源的小失败放大成全局降级。
- 恢复是"慢"的（需较多连续成功），避免抖动用期反复横跳。

未接入：截至 2026-10 未被任何入口 import 或注册，保留待评估。
重新接入前先确认调用点与并发语义，勿直接改并发上限默认值。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)


class AdaptiveThrottle:
    """线程安全的自适应并发节流器（限流 + 失败降级 + 成功慢恢复）。"""

    def __init__(
        self,
        initial_limit: int = 3,
        min_limit: int = 1,
        failure_threshold: int = 3,
        recovery_threshold: int = 20,
        acquire_timeout: float = 60.0,
    ) -> None:
        self._min_limit = max(1, int(min_limit))
        self._limit = max(self._min_limit, int(initial_limit))
        self._initial_limit = self._limit
        self._failure_threshold = int(failure_threshold)
        self._recovery_threshold = int(recovery_threshold)
        self._acquire_timeout = float(acquire_timeout)
        self._in_flight = 0
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._cond = threading.Condition()

    # ------------------------------------------------------------------ API
    def set_limit(self, limit: int) -> None:
        """设置并发上限（并重置为初始值 / 清空失败计数）。"""
        with self._cond:
            self._limit = max(self._min_limit, int(limit))
            self._initial_limit = self._limit
            self._consecutive_failures = 0
            self._consecutive_successes = 0
            self._cond.notify_all()

    def current_limit(self) -> int:
        with self._cond:
            return self._limit

    def acquire(self) -> None:
        """获取并发许可（并发满时等待；超时兜底放行避免饿死）。"""
        deadline = time.monotonic() + self._acquire_timeout
        with self._cond:
            while self._in_flight >= self._limit:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # 超时兜底：放行（软限流，不阻塞业务）
                    self._in_flight += 1
                    return
                self._cond.wait(min(remaining, 5.0))
            self._in_flight += 1

    def release(self) -> None:
        with self._cond:
            self._in_flight = max(0, self._in_flight - 1)
            self._cond.notify_all()

    def record_success(self) -> None:
        """一次数据抓取成功 → 连续失败清零；连续成功足够则恢复一级并发。"""
        with self._cond:
            self._consecutive_successes += 1
            self._consecutive_failures = 0
            if (
                self._consecutive_successes >= self._recovery_threshold
                and self._limit < self._initial_limit
            ):
                self._limit += 1
                self._consecutive_successes = 0
                logger.info(
                    "[自适应并发] 数据抓取连续成功，数据并发恢复为 %d", self._limit
                )
                self._cond.notify_all()

    def record_failure(self) -> None:
        """一次数据需求全部数据源失败 → 连续失败计数；超阈值则降一级并发。"""
        with self._cond:
            self._consecutive_failures += 1
            self._consecutive_successes = 0
            if (
                self._consecutive_failures >= self._failure_threshold
                and self._limit > self._min_limit
            ):
                self._limit -= 1
                self._consecutive_failures = 0
                logger.warning(
                    "[自适应并发] 数据抓取连续失败 %d 次，数据并发降为 %d（网络抖动降级）",
                    self._failure_threshold,
                    self._limit,
                )
                self._cond.notify_all()

    def stats(self) -> dict:
        with self._cond:
            return {
                "limit": self._limit,
                "initial_limit": self._initial_limit,
                "in_flight": self._in_flight,
                "consecutive_failures": self._consecutive_failures,
                "consecutive_successes": self._consecutive_successes,
            }
