import sqlite3

import pytest

from veyquant.order_journal import OrderJournal


def order(state="PENDING", filled="0"):
    return {
        "orderId": "fixture-order",
        "symbol": "005930",
        "currency": "KRW",
        "side": "BUY",
        "status": state,
        "quantity": "10",
        "execution": {"filledQuantity": filled},
    }


def test_restart_and_partial_fill_then_cancellation_are_durable(tmp_path):
    path = tmp_path / "journal.db"
    db = sqlite3.connect(path, isolation_level=None)
    journal = OrderJournal(db)
    journal.observe(order(), 100)
    journal.observe(order("PARTIAL_FILLED", "3"), 101)
    journal.observe(order("CANCELED", "3"), 102)
    db.close()
    db = sqlite3.connect(path, isolation_level=None)
    journal = OrderJournal(db)
    assert db.execute("SELECT state,filled_quantity FROM broker_orders").fetchone() == (
        "CANCELED",
        "3",
    )
    assert journal.counts()["order_events"] == 3
    assert journal.missing_from_open([]) == []
    db.close()


def test_missing_order_requires_detail_never_assumes_filled():
    db = sqlite3.connect(":memory:", isolation_level=None)
    journal = OrderJournal(db)
    journal.observe(order(), 100)
    assert journal.missing_from_open([]) == ["fixture-order"]
    assert journal.counts()["orders_needing_review"] == 1
    assert db.execute("SELECT state FROM broker_orders").fetchone()[0] == "PENDING"
    journal.observe(order("FILLED", "10"), 101)
    assert journal.counts()["orders_needing_review"] == 0


def test_duplicates_out_of_order_and_ws_during_rest_cannot_clear_pending_review():
    db = sqlite3.connect(":memory:", isolation_level=None)
    journal = OrderJournal(db)
    event = {"order": {"orderId": "fixture-order"}, "timestamp": "fixture"}
    journal.event("websocket", event, 102)
    journal.event("websocket", event, 102)
    assert journal.counts()["order_events"] == 1
    assert journal.missing_from_open([]) == ["fixture-order"]
    journal.observe(order("FILLED", "10"), 101)  # REST began before the WS event
    assert journal.counts()["orders_needing_review"] == 1
    journal.observe(order("FILLED", "10"), 103)
    assert journal.counts()["orders_needing_review"] == 0
    journal.observe(order("PARTIAL_FILLED", "3"), 104)
    assert journal.counts()["orders_needing_review"] == 1
    assert db.execute("SELECT state,filled_quantity FROM broker_orders").fetchone() == (
        "FILLED",
        "10",
    )


@pytest.mark.parametrize(
    "state,filled", [("FILLED", "3"), ("PARTIAL_FILLED", "11"), ("invented", "0")]
)
def test_malformed_order_never_enters_canonical_state(state, filled):
    db = sqlite3.connect(":memory:", isolation_level=None)
    journal = OrderJournal(db)
    with pytest.raises(ValueError):
        journal.observe(order(state, filled), 100)
    assert journal.counts()["tracked_orders"] == 0
