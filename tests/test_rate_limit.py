import pytest

from hs_crawler.rate_limit import AsyncTokenBucket, PoliteGate


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.mark.asyncio
async def test_token_bucket_waits_for_refill_after_capacity_is_consumed() -> None:
    fake = FakeTime()
    bucket = AsyncTokenBucket(
        rate=1.0,
        capacity=2,
        clock=fake.clock,
        sleeper=fake.sleep,
    )

    await bucket.acquire()
    await bucket.acquire()
    await bucket.acquire()

    assert fake.sleeps == [pytest.approx(1.0)]


@pytest.mark.asyncio
async def test_polite_gate_applies_configured_jitter_before_token() -> None:
    fake = FakeTime()
    bucket = AsyncTokenBucket(
        rate=10.0,
        capacity=1,
        clock=fake.clock,
        sleeper=fake.sleep,
    )
    gate = PoliteGate(
        bucket,
        min_delay=1.0,
        max_delay=2.0,
        sleeper=fake.sleep,
        uniform=lambda minimum, maximum: 1.5,
    )

    await gate.wait()

    assert fake.sleeps == [1.5]


def test_polite_gate_rejects_delay_outside_required_order() -> None:
    bucket = AsyncTokenBucket(rate=1.0, capacity=1)

    with pytest.raises(ValueError, match="min_delay"):
        PoliteGate(bucket, min_delay=2.0, max_delay=1.0)
