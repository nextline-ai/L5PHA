from contextlib import closing
from datetime import datetime

import pytest

from veyquant.execution import ExecutionCore
from veyquant.performance import monthly_performance
from veyquant.store import Store

NOW = datetime.fromisoformat("2026-10-02T09:00:00+09:00").timestamp()


@pytest.fixture
def store():
    with closing(Store(":memory:")) as store:
        ExecutionCore(store)
        store.db.execute("CREATE TABLE performance_marks(month TEXT PRIMARY KEY, value TEXT)")
        store.db.execute(
            "CREATE TABLE performance_months(month TEXT PRIMARY KEY, "
            "baseline TEXT, source TEXT, created_at REAL)"
        )
        store.db.execute("INSERT INTO execution_days VALUES ('2026-10-02','120',1)")
        yield store


def result(store, bars=None, **changes):
    loss = {"day": "2026-10-02", "state": "breached", "pnl_krw": "5"} | changes
    return monthly_performance(store, loss, NOW, bars or {})


def fill(store, at, realized="50"):
    store.db.execute(
        "INSERT INTO execution_fills VALUES (?,?,?,?,?,?,?)",
        (str(at), "order", 10, "900", "2", realized, at),
    )


def test_month_open_baseline_and_fee_inclusive_daily_valuation(store):
    store.db.execute("INSERT INTO execution_days VALUES ('2026-10-01','100',0)")
    assert result(store)["pnl_krw"] == "25"
    assert result(store, pnl_krw="-25")["pnl_krw"] == "-5"
    assert store.db.execute(
        "SELECT baseline,breached FROM execution_days WHERE day='2026-10-02'"
    ).fetchone()[:] == ("120", 1)
    # Baseline survives restarts and an unavailable opening-day history.
    store.db.execute("DELETE FROM execution_days WHERE day='2026-10-01'")
    assert result(store)["pnl_krw"] == "25"


def test_new_managed_portfolio_starts_from_zero(store):
    fill(store, NOW - 3600, "-2")
    assert result(store)["pnl_krw"] == "125"


def test_carry_positions_use_previous_month_close_then_freeze(store):
    fill(store, NOW - 86400 * 4)
    store.db.execute("INSERT INTO execution_positions VALUES ('005930',10,'900')")
    bars = {
        "005930": {
            "updated_at": NOW,
            "bars": [
                {"date": "2026-09-30", "close": "100"},
                {"date": "2026-10-01", "close": "999"},
            ],
        }
    }
    assert result(store, bars)["pnl_krw"] == "-25"  # 125 - (50 + 100)
    fill(store, NOW, "20")
    assert result(store)["pnl_krw"] == "-25"


def test_missing_opening_cannot_present_partial_month_as_full_month(store):
    fill(store, NOW - 86400 * 4)
    fill(store, NOW)
    assert result(store)["state"] == "month_baseline_required"


def test_orphan_holdings_cannot_assume_zero_opening(store):
    store.db.execute("INSERT INTO execution_positions VALUES ('005930',10,'900')")
    assert result(store)["state"] == "month_baseline_required"


@pytest.mark.parametrize(
    "changes",
    [
        {"day": "2026-10-01"},
        {"state": "reconciliation_required"},
        {"pnl_krw": "NaN"},
        {"pnl_krw": "Infinity"},
    ],
)
def test_untrusted_valuation_is_not_a_return(store, changes):
    assert result(store, **changes)["state"] == "valuation_required"


def test_missing_or_stale_closes_are_not_used(store):
    fill(store, NOW - 86400 * 4)
    store.db.execute("INSERT INTO execution_positions VALUES ('005930',10,'900')")
    assert result(store)["state"] == "month_baseline_required"
    bars = {"005930": {"updated_at": NOW - 3600, "bars": [{"date": "2026-09-30", "close": "100"}]}}
    assert result(store, bars)["state"] == "month_baseline_required"


def test_korean_month_boundary_does_not_reuse_previous_month(store):
    store.db.execute("INSERT INTO performance_months VALUES ('2026-09','999','test',0)")
    at = datetime.fromisoformat("2026-09-30T15:00:00+00:00").timestamp()
    store.db.execute("INSERT INTO execution_days VALUES ('2026-10-01','100',0)")
    value = monthly_performance(
        store, {"day": "2026-10-01", "state": "within_limit", "pnl_krw": "7"}, at, {}
    )
    assert value["month"] == "2026-10"
    assert value["pnl_krw"] == "7"


def test_display_database_error_never_interrupts_execution(store):
    store.db.execute("DROP TABLE performance_months")
    assert result(store)["state"] == "valuation_required"


def test_pre_session_uses_confirmed_previous_close_without_changing_loss(store):
    store.db.execute("INSERT INTO execution_days VALUES ('2026-10-01','100',0)")
    loss = {"state": "reconciliation_required"}
    value = monthly_performance(store, loss, NOW, {}, opening_date="2026-10-01")
    assert value["pnl_krw"] == "20"
    assert value["valuation_date"] == "2026-10-01"
    assert loss == {"state": "reconciliation_required"}
    # A later refresh or process restart keeps the last verified mark and its timestamp.
    saved = monthly_performance(store, loss, NOW + 60, {})
    assert saved["pnl_krw"] == "20"
    assert saved["as_of"] == NOW
    assert saved["refreshing"] is True


def test_pre_session_baseline_not_used_after_same_day_fill(store):
    fill(store, NOW)
    assert (
        monthly_performance(store, None, NOW, {}, opening_date="2026-10-01")["state"]
        == "valuation_required"
    )
