from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable


Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


class AsyncTokenBucket:
    def __init__(
        self,
        *,
        rate: float,
        capacity: float,
        clock: Clock = time.monotonic,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.rate = float(rate)
        self.capacity = float(capacity)
        self._clock = clock
        self._sleeper = sleeper
        self._tokens = float(capacity)
        self._updated_at = clock()
        self._lock = asyncio.Lock()

    async def acquire(self, amount: float = 1.0) -> None:
        if amount <= 0 or amount > self.capacity:
            raise ValueError("amount must be positive and no greater than capacity")

        while True:
            async with self._lock:
                now = self._clock()
                elapsed = max(0.0, now - self._updated_at)
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                self._updated_at = now
                if self._tokens >= amount:
                    self._tokens -= amount
                    return
                wait_seconds = (amount - self._tokens) / self.rate
            await self._sleeper(wait_seconds)


class PoliteGate:
    def __init__(
        self,
        bucket: AsyncTokenBucket,
        *,
        min_delay: float,
        max_delay: float,
        sleeper: Sleeper = asyncio.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if min_delay < 0 or max_delay < min_delay:
            raise ValueError("min_delay must be non-negative and no greater than max_delay")
        self._bucket = bucket
        self._min_delay = min_delay
        self._max_delay = max_delay
        self._sleeper = sleeper
        self._uniform = uniform

    async def wait(self) -> None:
        await self._sleeper(self._uniform(self._min_delay, self._max_delay))
        await self._bucket.acquire()
