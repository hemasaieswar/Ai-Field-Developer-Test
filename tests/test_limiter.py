import asyncio

import pytest

from assessment.limiter import RateLimited, TokenLimiter


async def test_concurrent_instances(tmp_path):
    path = str(tmp_path / "usage.db")
    instances = [TokenLimiter(path, limit=100) for _ in range(20)]
    await instances[0].initialize()
    results = await asyncio.gather(
        *(item.reserve("tenant", 10) for item in instances), return_exceptions=True
    )
    assert sum(isinstance(r, str) for r in results) == 10
    assert sum(isinstance(r, RateLimited) for r in results) == 10
    assert await instances[0].reserve("other-tenant", 100)


async def test_window_settlement_and_restart(tmp_path):
    now = [100.0]
    path = str(tmp_path / "usage.db")
    limiter = TokenLimiter(path, limit=100, clock=lambda: now[0])
    await limiter.initialize()
    token = await limiter.reserve("key", 100)
    now[0] += 61
    with pytest.raises(RateLimited):
        await limiter.reserve("key", 1)
    await limiter.settle(token, 40)
    restarted = TokenLimiter(path, limit=100, clock=lambda: now[0])
    with pytest.raises(RateLimited):
        await restarted.reserve("key", 61)
    now[0] += 60
    assert await restarted.reserve("key", 100)


async def test_crash_reservation_recovery(tmp_path):
    now = [0.0]
    limiter = TokenLimiter(str(tmp_path / "usage.db"), limit=100, clock=lambda: now[0])
    await limiter.initialize()
    await limiter.reserve("key", 100)
    now[0] = 120
    with pytest.raises(RateLimited):
        await limiter.reserve("key", 1)
    now[0] = 180
    assert await limiter.reserve("key", 100)
