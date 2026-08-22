"""限速器测试。"""

from __future__ import annotations

import asyncio
from time import monotonic

from astrbot_lark_kit.rate_limit import RateLimiter


async def test_allows_burst_up_to_rate() -> None:
    limiter = RateLimiter(rate=5.0)
    start = monotonic()
    for _ in range(5):
        await limiter.acquire()
    assert monotonic() - start < 0.5  # 前 5 个应立即放行


async def test_sixth_call_throttled() -> None:
    limiter = RateLimiter(rate=5.0)
    for _ in range(5):
        await limiter.acquire()
    start = monotonic()
    await limiter.acquire()
    elapsed = monotonic() - start
    assert elapsed >= 0.3  # 第 6 个必须等窗口滑动


async def test_invalid_rate_rejected() -> None:
    try:
        RateLimiter(rate=0)
    except ValueError:
        return
    raise AssertionError("rate=0 应抛 ValueError")


async def test_concurrent_acquire_respects_capacity() -> None:
    limiter = RateLimiter(rate=3.0)

    async def hit() -> None:
        await limiter.acquire()

    start = monotonic()
    await asyncio.gather(*(hit() for _ in range(6)))
    assert monotonic() - start >= 0.5
