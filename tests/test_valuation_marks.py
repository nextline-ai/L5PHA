import copy
import json
from datetime import datetime

import pytest
from test_live_worker import worker as worker

from veyquant.valuation_marks import bid_mark, portfolio_mark


def book(now):
    return {
        "currency": "KRW",
        "timestamp": datetime.fromtimestamp(now).astimezone().isoformat(),
        "bids": [{"price": "99", "volume": "5"}],
        "asks": [{"price": "101", "volume": "8"}],
    }


@pytest.mark.parametrize(
    "mutation", ["old", "future", "naive", "empty", "crossed", "nan", "zero_volume"]
)
def test_invalid_books_never_get_receipt_time_as_freshness(mutation):
    now = 1789450914.0
    value = book(now)
    if mutation in ("old", "future"):
        value = book(now + (1 if mutation == "future" else -31))
    elif mutation == "naive":
        value["timestamp"] = "2026-09-15T14:41:54"
    elif mutation == "empty":
        value["asks"] = []
    elif mutation == "crossed":
        value["bids"][0]["price"] = "102"
    elif mutation == "nan":
        value["bids"][0]["price"] = "NaN"
    else:
        value["bids"][0]["volume"] = "0"
    assert bid_mark("005930", value, now, 30) is None


async def test_stale_trade_with_live_book_can_value_but_both_stale_still_block(worker):
    w = worker
    await w.reconcile()
    w.db.execute("INSERT INTO execution_positions VALUES('005930',2,'200')")
    now = w.clock()
    w.observations.quotes["005930"]["as_of"] = now - 64
    original = copy.deepcopy(w.observations.quotes["005930"])
    w.observations.db.execute(
        "INSERT OR REPLACE INTO latest VALUES (?,?,?)",
        ("orderbook:kr:005930", json.dumps(book(now - 2)), now),
    )
    mark = portfolio_mark(w.observations, "005930", original, now, 5)
    assert mark == {"symbol": "005930", "price": "99", "as_of": now - 2, "basis": "best_bid"}
    assert w.valuation_quotes()["005930"] == ("99", now - 2)
    assert w.observations.quotes["005930"] == original
    w.test_time[0] += 4
    assert portfolio_mark(w.observations, "005930", original, w.clock(), 5) is None
    assert w.valuation_quotes()["005930"][1] == now - 64


async def test_research_fallback_reports_valuation_basis_without_rewriting_trade(worker):
    from veyquant.research_context import ResearchContext

    w = worker
    now = w.clock()
    await w.reconcile()
    w.db.execute("INSERT INTO execution_positions VALUES('005930',2,'200')")
    w.observations.quotes["005930"]["as_of"] = now - 64
    w.observations.db.execute("DELETE FROM latest")
    reads = []

    async def prices(symbols):
        return [
            {
                "symbol": s,
                "currency": "KRW",
                "lastPrice": "100",
                "timestamp": book(now - 64)["timestamp"],
            }
            for s in symbols
        ]

    async def limits(symbol):
        return {"lowerLimitPrice": "70", "upperLimitPrice": "130"}

    async def orderbook(symbol):
        reads.append(symbol)
        return book(now - 2)

    w.broker.prices, w.broker.price_limits, w.broker.orderbook = prices, limits, orderbook
    result = await ResearchContext(w, w.clock).account()
    risk = result["risk_status"]
    assert risk["state"] == "within_limit"
    assert risk["daily_pnl_krw"] == "-2"
    assert risk["price_observations"][0]["basis"] == "best_bid"
    assert risk["price_observations"][0]["age_seconds"] == 2
    assert result["quotes"][0]["as_of"] == now - 64
    assert reads == ["005930"]


async def test_quiet_holding_gets_bounded_rest_mark_without_retiming_source(worker):
    w = worker
    await w.reconcile()
    now = w.clock()
    w.db.execute("INSERT INTO execution_positions VALUES('005930',2,'200')")
    w.observations.quotes["005930"]["as_of"] = now - 60
    w.observations.db.execute("DELETE FROM latest")
    calls = []

    async def orderbook(symbol):
        calls.append(symbol)
        return book(w.clock() - 1)

    w.broker.orderbook = orderbook
    await w.refresh_valuation_marks(w.core._positions())
    assert w.valuation_quotes()["005930"] == ("99", now - 1)
    assert w.observations.quotes["005930"]["as_of"] == now - 60
    await w.refresh_valuation_marks(w.core._positions())
    assert calls == ["005930"]
    w.test_time[0] += 7

    async def stale(symbol):
        return book(now - 20)

    w.broker.orderbook = stale
    await w.refresh_valuation_marks(w.core._positions())
    assert w.valuation_quotes()["005930"][1] == now - 60
    assert "005930" not in w.rest_valuation_marks


async def test_disconnected_stream_cannot_use_rest_valuation_cache(worker):
    w = worker
    await w.reconcile()
    w.db.execute("INSERT INTO execution_positions VALUES('005930',2,'200')")
    now = w.clock()
    w.observations.db.execute("DELETE FROM latest")
    w.observations.quotes["005930"]["as_of"] = now - 60
    w.rest_valuation_marks["005930"] = {"price": "99", "as_of": now}
    w.observations.connected = False
    assert w.valuation_quotes()["005930"][1] == now - 60
