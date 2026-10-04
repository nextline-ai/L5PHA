import asyncio
from types import SimpleNamespace

import pytest

from veyquant.adapters.toss import TossReadOnly
from veyquant.research_context import ContextReadError, context_read, research_priority


async def test_daily_cache_singleflight_isolated_results_expiry_and_kst_day(monkeypatch):
    import veyquant.adapters.toss as module

    now, reads = [1789088400], []
    monkeypatch.setattr(module, "wall_time", lambda: now[0])
    broker = TossReadOnly(None, None)

    async def get(path, params):
        reads.append(params)
        await asyncio.sleep(0)
        return {"candles": [{"close": "100"}]}

    monkeypatch.setattr(broker, "_get", get)
    results = await asyncio.gather(*(broker.daily_candles("005930") for _ in range(12)))
    assert len(reads) == 1
    assert reads[0]["adjusted"] == "true"
    results[0]["candles"][0]["close"] = "corrupt"
    assert results[1]["candles"][0]["close"] == "100"
    assert (await broker.daily_candles("005930"))["candles"][0]["close"] == "100"
    now[0] += 1800
    await broker.daily_candles("005930")
    assert len(reads) == 2
    now[0] = ((now[0] + 32400) // 86400 + 1) * 86400 - 32400 - 1
    await broker.daily_candles("005930")
    assert len(reads) == 3
    now[0] += 2
    await broker.daily_candles("005930")
    assert len(reads) == 4
    await broker.minute_candles("005930")
    await broker.minute_candles("005930")
    assert len(reads) == 6


async def test_empty_candles_are_not_cached(monkeypatch):
    broker, reads = TossReadOnly(None, None), []

    async def get(*args):
        reads.append(1)
        return {"candles": []}

    monkeypatch.setattr(broker, "_get", get)
    await broker.daily_candles("005930")
    await broker.daily_candles("005930")
    assert len(reads) == 2


async def test_priority_is_released_after_cancellation_and_errors():
    broker = SimpleNamespace()
    entered = asyncio.Event()

    async def collect():
        async with research_priority(broker):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(collect())
    await entered.wait()
    assert broker.research_reads_active == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert broker.research_reads_active == 0
    with pytest.raises(ValueError):
        async with research_priority(broker):
            raise ValueError("fixture")
    assert broker.research_reads_active == 0


async def test_component_timeout_keeps_actionable_error_without_retry():
    reads = []

    async def stalled():
        reads.append(1)
        raise TimeoutError()

    with pytest.raises(ContextReadError, match="context_bars_context_timeout"):
        await context_read("bars", "005930", stalled)
    assert len(reads) == 1
