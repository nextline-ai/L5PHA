import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from test_decision_pipeline import CALENDAR, NOW

from veyquant import collector
from veyquant.adapters.toss import TossError, TossHTTPError
from veyquant.decision_runtime import Runtime, relevant_signals
from veyquant.decision_store import DecisionStore


def test_cursor_pagination_preserves_ties_and_concurrent_new_records(tmp_path):
    store = DecisionStore(tmp_path / "db")
    for i in range(135):
        row = {"id": f"{i:032x}:middle", "started": i // 3, "role": "middle"}
        store.db.execute(
            "INSERT INTO analysis_records VALUES(?,?,?,?,?)",
            (row["id"], None, "middle", row["started"], json.dumps(row)),
        )
    first = store.layer_page(limit=30)
    cursor = first["next_cursor"]
    ids = [r["id"] for r in first["records"]]
    store.db.execute(
        "INSERT INTO analysis_records VALUES(?,?,?,?,?)",
        ("new:middle", None, "middle", 999, json.dumps({"id": "new:middle"})),
    )
    while cursor:
        page = store.layer_page(cursor, limit=30)
        ids.extend(r["id"] for r in page["records"])
        cursor = page["next_cursor"]
    assert len(ids) == len(set(ids)) == 135 and "new:middle" not in ids
    for invalid in ("%%%", base64.urlsafe_b64encode(b'[NaN,"x"]').decode(), "x" * 401):
        with pytest.raises(ValueError, match="invalid_history_cursor"):
            store.layer_page(invalid)
    store.close() if hasattr(store, "close") else store.db.close()


def test_relevance_filter_never_suppresses_exposure_or_fresh_opportunity():
    events = [
        {"symbol": s, "kind": "dart_important"} for s in ("held", "eligible", "halted", "unknown")
    ]
    events.append({"condition": "realtime_gap"})
    status = {"exposure_as_of": 100, "exposed_symbols": ["held"], "eligible_symbols": ["eligible"]}
    accepted, deferred = relevant_signals(events, status, 110)
    assert accepted == events[:2] + events[4:]
    assert deferred == events[2:4]
    assert relevant_signals(events, status, 140) == (events, [])
    assert relevant_signals(events, {}, 110) == (events, [])


async def test_transient_collector_backoff_respects_broker_and_stops_cleanly(monkeypatch):
    stop, attempts, delays = asyncio.Event(), [], []
    store = SimpleNamespace(connected=True, account_view={"old": True}, publish=lambda now: None)

    async def collect(*args):
        attempts.append(1)
        if len(attempts) == 1:
            raise TossHTTPError(429, 95)
        stop.set()

    async def wait(coro, timeout):
        delays.append(timeout)
        coro.close()
        raise TimeoutError

    monkeypatch.setattr(collector, "collect", collect)
    monkeypatch.setattr(collector.asyncio, "wait_for", wait)
    await collector.supervised_collect({}, "fixture", store, stop)
    assert len(attempts) == 2 and sum(delays) == 95 and max(delays) <= 10
    assert store.connected is False and store.account_view is None


@pytest.mark.parametrize(
    "error",
    [
        TossHTTPError(401),
        TossHTTPError(403),
        ValueError("SECRET"),
        TossError("invalid_read_response"),
    ],
)
async def test_permanent_failures_are_not_retried_or_logged_with_private_text(
    monkeypatch, capsys, error
):
    attempts = []

    async def collect(*args):
        attempts.append(1)
        raise error

    monkeypatch.setattr(collector, "collect", collect)
    with pytest.raises(type(error)):
        await collector.supervised_collect({}, "fixture", None, asyncio.Event())
    assert attempts == [1]
    log = capsys.readouterr().out
    assert "collector_failure" in log and "SECRET" not in log


async def test_after_hours_critical_is_preserved_without_paid_pipeline(tmp_path):
    runtime = Runtime.__new__(Runtime)
    runtime.store = DecisionStore(tmp_path / "db")
    runtime.clock = lambda: NOW + 12 * 3600
    runtime.calendar = CALENDAR
    runtime.configuration = lambda: {}
    result = await runtime.launch("test", "critical", {"evidence_ids": ["e1"]})
    assert result == {"accepted": False, "reason": "outside_session"}
    assert runtime.store.get("pending_critical") == ["e1"]
    assert runtime.store.history() == []
    runtime.store.db.close()


def test_regular_session_gate_does_not_treat_unknown_or_closed_market_as_open():
    assert collector.regular_market_open(CALENDAR, NOW)
    assert not collector.regular_market_open(CALENDAR, NOW + 12 * 3600)
    assert not collector.regular_market_open(None, NOW)


def test_recovered_immediate_market_alarm_does_not_spend_ai_calls(tmp_path):
    store = DecisionStore(tmp_path / "db")
    store.edge("realtime_gap", True, 100, {"condition": "realtime_gap"}, immediate=True)
    store.edge("realtime_gap", False, 110, {"condition": "realtime_gap"}, immediate=True)
    assert store.take_signals(110) == []
    store.db.close()


async def test_closed_market_tick_keeps_signals_queued_without_ai(tmp_path, monkeypatch):
    from veyquant import decision_runtime

    runtime = Runtime.__new__(Runtime)
    runtime.store = DecisionStore(tmp_path / "db")
    runtime.clock = lambda: NOW + 12 * 3600
    runtime.configuration = lambda: {}
    runtime.monitor_task = None
    runtime.publish = lambda: None
    runtime.market_path = str(tmp_path / "market.json")
    (tmp_path / "market.json").write_text(
        json.dumps({"updated_at": runtime.clock(), "metrics": []})
    )
    runtime.store.signal_once("event", NOW, {"kind": "dart_important", "symbol": "005930"})

    async def status(*args, **kwargs):
        return {"calendar": CALENDAR, "blocked": False}

    async def forbidden(*args):
        pytest.fail("closed-market tick must not invoke AI")

    monkeypatch.setattr(decision_runtime, "exchange", status)
    runtime.surveillance = forbidden
    await runtime.tick()
    assert runtime.state == "outside_session"
    assert runtime.monitor_task is None
    assert len(runtime.store.take_signals(runtime.clock())) == 1
    runtime.store.db.close()
