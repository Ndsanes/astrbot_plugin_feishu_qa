"""异步滑动窗口限速器。

默认 5 req/s,与 bili_verify_feishu 插件既定的飞书调用限速约定一致。
"""

from __future__ import annotations

import asyncio
from collections import deque
from time import monotonic


class RateLimiter:
    """协程间共享的滑动窗口限速器:每 1 秒窗口内最多 rate 次放行。

    用法:
        limiter = RateLimiter(rate=5.0)
        await limiter.acquire()
        ...  # 受限操作
    """

    def __init__(self, rate: float = 5.0) -> None:
        if rate <= 0:
            raise ValueError("rate 必须 > 0")
        self._rate = int(rate) if rate == int(rate) else rate
        self._capacity = float(rate)
        self._window: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = monotonic()
                while self._window and now - self._window[0] >= 1.0:
                    self._window.popleft()
                if len(self._window) < self._capacity:
                    self._window.append(now)
                    return
                wait = 1.0 - (now - self._window[0])
            await asyncio.sleep(max(wait, 0.01))
