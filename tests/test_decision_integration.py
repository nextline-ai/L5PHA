import copy
import json
from dataclasses import replace
from datetime import datetime

import pytest
from test_decision_pipeline import (  # noqa: F401
    CALENDAR,
    NOW,
    context,
    final,
    make_brief,
    run_pipeline,
)
from test_decision_pipeline import (
    store as store,
)
from test_live_worker import worker as worker

from veyquant.decision_pipeline import DecisionPipeline
from veyquant.market_metrics import MarketMetrics
from veyquant.research_context import ResearchContext
from veyquant.surveillance import observe


def test_spread_surveillance_does_not_present_missing_price_timestamp_as_a_price():
    from veyquant.decision_runtime import surveillance_inputs

    signals = [
        {
            "condition": "spread_50bp",
            "symbol": "005930",
            "metrics": {
                "as_of": 0,
                "change_5m": None,
                "price_as_of": 0,
                "volume_zscore": None,
                "spread_bp": 70,
                "book_as_of": NOW,
            },
        }
    ]
    sent = surveillance_inputs(signals)
    assert sent[0]["metrics"] == {"spread_bp": 70, "book_as_of": NOW}
    assert sent[0]["condition"] == "spread_50bp" and sent[0]["symbol"] == "005930"
    assert signals[0]["metrics"]["price_as_of"] == 0  # Full original evidence is retained.


async def test_private_context_failure_retains_safe_component_without_provider_body():
    from veyquant.adapters.toss import TossHTTPError
    from veyquant.research_context import context_error, context_read

    async def failed():
        raise TossHTTPError(429)

    with pytest.raises(ValueError) as error:
        await context_read("daily_bars", "005930", failed)
    grouped = ExceptionGroup("private fixture", [error.value])
    assert context_error(grouped) == "context_daily_bars_toss_http_429"
    assert error.value.symbol == "005930"
    assert context_error(ValueError("invalid_ohlc")) == "invalid_ohlc"
    assert context_error(ValueError("private_api_key_fixture")) == "context_unavailable"
    assert context_error(TimeoutError()) == "context_timeout"


@pytest.mark.parametrize("status", [429, 403])
async def test_context_does_not_multiply_broker_transport_retries(status):
    from veyquant.adapters.toss import TossHTTPError
    from veyquant.research_context import context_read

    attempts = []

    async def exhausted():
        attempts.append(1)
        raise TossHTTPError(status)

    with pytest.raises(ValueError, match=f"context_daily_bars_toss_http_{status}"):
        await context_read("daily_bars", "005930", exhausted)
    assert len(attempts) == 1


def test_dashboard_previews_are_bounded_without_changing_full_decision():
    from veyquant.decision_runtime import brief_preview, decision_preview

    full_brief = {
        "severity": "WARN",
        "summary": "가" * 3000,
        "candidates": [{"symbol": "000001", "reason": "나" * 1000}],
        "uncertainties": ["다" * 1000] * 100,
    }
    preview = brief_preview(full_brief)
    assert len(preview["summary"]) == 1000
    assert "uncertainties" not in preview and len(full_brief["uncertainties"]) == 100
    full_decision = {"action": "NO_ACTION", "summary": "라" * 3000, "intents": []}
    assert len(decision_preview(full_decision)["summary"]) == 1000
    assert len(full_decision["summary"]) == 3000


async def test_context_sanitizes_account_and_includes_fresh_constraints(worker):
    w = worker

    async def prices(symbols):
        return [
            {
                "symbol": s,
                "currency": "KRW",
                "lastPrice": "100",
                "timestamp": datetime.fromtimestamp(w.clock()).astimezone().isoformat(),
            }
            for s in symbols
        ]

    async def limits(symbol):
        return {"lowerLimitPrice": "70", "upperLimitPrice": "130"}

    w.broker.prices, w.broker.price_limits = prices, limits
    await w.reconcile()
    result = await ResearchContext(w, w.clock).account(["005930"])
    assert result["cash"] == "1000"
    assert result["holdings"][0]["quantity"] == "2"
    assert result["order_constraints"]["stocks"]["005930"]["sellable_quantity"] == "0"
    assert result["risk_status"]["state"] == "within_limit"
    assert result["risk_status"]["daily_pnl_krw"] == "0"
    trading = result["order_constraints"]["session"]
    assert trading["is_open"] is True
    assert datetime.fromtimestamp(trading["ends_at"]).astimezone().minute == 20
    assert len(result["quotes"]) == 1
    assert "accountId" not in json.dumps(result) and "orderId" not in json.dumps(result)
    before = result["order_generation"]
    w.observations.order_generation += 1  # Subscription rotation, not a portfolio change.
    assert (await ResearchContext(w, w.clock).account())["order_generation"] == before
    w.observations.portfolio_generation += 1
    assert (await ResearchContext(w, w.clock).account())["order_generation"] == before + 1


def basket(w, side="BUY", quantity=2):
    intent = final("SUBMIT")["intents"][0] | {
        "symbol": "005930",
        "side": side,
        "quantity": quantity,
        "evidence_ids": ["market:005930"],
    }
    return {
        "proposals": [
            {
                "id": "fixture-basket",
                "created_at": w.clock(),
                "settings_revision": 1,
                "intents": [intent],
                "context": {"quotes": [w.observations.quotes["005930"]]},
            }
        ]
    }


async def test_intent_basket_rejected_as_whole_and_not_resized(worker):
    await worker.reconcile()
    data = basket(worker, quantity=20)
    assert worker.decision_proposal(data, worker.control()["policy"]) is None
    assert worker.db.execute("SELECT COUNT(*) FROM execution_decisions").fetchone()[0] == 1
    assert not worker.transport.creates


async def test_sell_proposal_can_reduce_position_when_over_capital(worker):
    await worker.reconcile()
    worker.snapshot = replace(worker.snapshot, managed_quantities={"005930": 2})
    worker.core.capital_used = lambda: 1200
    assert (
        worker.decision_proposal(basket(worker, "SELL"), worker.control()["policy"])[
            "trade_intent"
        ]["quantity"]
        == 2
    )


async def test_completed_unconsumed_intent_blocks_next_pipeline(worker):
    data = basket(worker)
    with open(worker.report_path, "w") as f:
        json.dump(data, f)
    assert worker.pending_decision()
    await worker.reconcile()
    proposal = worker.decision_proposal(data, worker.control()["policy"])
    worker.remember(proposal["event_id"], "fixture_no_order")
    assert not worker.pending_decision()


async def test_v2_exact_quantity_partial_fill_and_repeated_report_do_not_duplicate(worker):
    from test_live_worker import output, receive, update

    w = worker

    async def limits(symbol):
        return {"lowerLimitPrice": "70", "upperLimitPrice": "130"}

    w.broker.price_limits = limits
    data = basket(w, quantity=2) | {"protocol": "decision-v2", "updated_at": w.clock()}

    def publish():
        with open(w.report_path, "w") as f:
            json.dump(data | {"updated_at": w.clock()}, f)

    publish()
    await w.tick()
    assert len(w.transport.creates) == 1, output(w)
    assert w.transport.creates[0]["quantity"] == "2"  # Legacy sizing would have bought five.
    receive(w, status="PARTIAL_FILLED", filled=1)
    w.broker.quantity = 3
    update(w, seconds=2)
    publish()
    await w.tick()
    assert output(w)["orders"][0]["state"] == "PARTIAL"
    assert w.core._positions()["005930"][0] == 1
    receive(w, filled=2)
    w.broker.quantity = 4
    update(w, seconds=2)
    publish()
    await w.tick()
    assert output(w)["orders"][0]["state"] == "FILLED"
    assert w.core._positions()["005930"][0] == 2
    assert len(w.transport.creates) == 1


async def test_preopen_intent_is_not_reused_at_market_open(worker):
    from test_live_worker import update

    w = worker
    start = w.clock() + 1200
    w.broker.calendar["today"]["integrated"]["regularMarket"]["startTime"] = (
        datetime.fromtimestamp(start).astimezone().isoformat()
    )
    data = basket(w) | {"protocol": "decision-v2", "updated_at": w.clock()}
    with open(w.report_path, "w") as f:
        json.dump(data, f)
    await w.tick()
    assert not w.transport.creates
    update(w, seconds=1201)
    with open(w.report_path, "w") as f:
        json.dump(data | {"updated_at": w.clock()}, f)
    await w.tick()
    assert not w.transport.creates
    assert w.last_reason == "report_expired"


async def test_empty_completed_candles_prevent_submit(store):
    def mutate(value, n):
        value["details"]["000001"]["daily_bars"] = []

    async def model(role, payload):
        return make_brief() if role == "middle" else final("SUBMIT")

    result, _, _, _ = await run_pipeline(store, mutate=mutate, model_override=model)
    assert result is None
    assert store.history()[0]["data"]["error"] == "missing_trade_evidence"


async def test_dart_evidence_not_displaced_by_recent_web_results(store):
    store.evidence(
        "dart_important",
        {"kind": "dart_important", "symbol": "000001", "title": "유상증자"},
        NOW - 60,
        "dart:1",
    )
    for i in range(501):
        store.evidence("web", {"kind": "web"}, NOW, "web:" + str(i))
    result, calls, _, _ = await run_pipeline(store)
    assert result
    decision_input = calls[-1][1]
    assert any(
        result["citation_aliases"].get(e["id"], e["id"]) == "dart:1"
        for group in decision_input["mandatory_evidence"]["groups"]
        for row in group["rows"]
        for e in [group["shared"] | dict(zip(group["columns"], row, strict=True))]
    )


async def test_context_collection_consumes_middle_deadline(store):
    monotonic = [0]
    calls = []

    async def get_context(*args):
        monotonic[0] = 601
        return context()

    async def model(*args):
        calls.append(args)
        return make_brief(), {}

    p = DecisionPipeline(
        model, get_context, None, store, clock=lambda: NOW, monotonic=lambda: monotonic[0]
    )
    identity = store.admit("deadline", "manual", NOW, {})
    assert await p.run(identity, "manual", {}, {"settings_revision": 1}) is None
    assert not calls
    assert store.history()[0]["state"] == "aborted"


def test_fresh_book_triggers_without_a_recent_trade(store):
    metric = {"symbol": "000001", "as_of": NOW - 900, "book_as_of": NOW, "spread_bp": 100}
    observe(store, {"metrics": [metric], "last_realtime_at": NOW}, CALENDAR, NOW)
    store.take_signals(NOW)  # Heartbeat only.
    assert any(s["condition"] == "spread_100bp" for s in store.take_signals(NOW + 300))


def test_immediate_event_does_not_drain_unfinished_price_window(store):
    store.edge("price", True, NOW, {"condition": "price"})
    store.signal_once("dart", NOW + 10, {"condition": "dart"})
    assert [s["condition"] for s in store.take_signals(NOW + 10)] == ["dart"]
    assert [s["condition"] for s in store.take_signals(NOW + 300)] == ["price"]


def minutes(now):
    boundary = int(now // 60) * 60
    rows = []
    for n in range(1, 121):
        rows.append(
            {
                "timestamp": datetime.fromtimestamp(boundary - n * 60).astimezone().isoformat(),
                "currency": "KRW",
                "closePrice": "104" if n <= 5 else "100",
                "volume": str(1000 if n <= 5 else 10 + n % 5),
            }
        )
    return {"candles": rows}


def test_rotated_subscription_uses_complete_minute_baseline_and_expires(worker):
    metrics = worker.observations.metrics
    metrics.seed("005930", minutes(NOW), NOW)
    result = metrics.export(["005930"], True, NOW)[0]
    assert result["volume_zscore"] is None  # Identical prior 5m totals: undefined deviation.
    varied = minutes(NOW)
    varied["candles"][70]["volume"] = "40"
    metrics.seed("005930", varied, NOW)
    result = metrics.export(["005930"], True, NOW)[0]
    assert result["change_5m"] == pytest.approx(0.04)
    assert result["volume_zscore"] > 4
    assert result["price_source"] == "completed_minutes"
    assert metrics.export(["005930"], True, NOW + 91)[0]["volume_zscore"] is None


def test_missing_minute_bar_does_not_become_zero_volume(worker):
    rows = minutes(NOW)
    rows["candles"].pop(40)
    metrics = MarketMetrics(worker.observations.db)
    metrics.seed("005930", rows, NOW)
    assert metrics.export(["005930"], True, NOW)[0]["volume_zscore"] is None
    malformed = copy.deepcopy(rows)
    malformed["candles"][0]["volume"] = "NaN"
    with pytest.raises(ValueError):
        metrics.seed("005930", malformed, NOW)


def test_legacy_history_imported_once_without_executable_proposals(store, tmp_path):
    import sqlite3

    legacy = tmp_path / "old.db"
    db = sqlite3.connect(legacy)
    db.execute("CREATE TABLE jobs(id TEXT,created_at REAL,report TEXT)")
    report = {"outcome": "buy", "symbol": "005930", "stages": [{"role": "research"}]}
    db.execute("INSERT INTO jobs VALUES(?,?,?)", ("old-analysis", NOW - 100, json.dumps(report)))
    db.commit()
    db.close()
    store.import_legacy(legacy)
    store.import_legacy(legacy)
    records = store.history()
    assert len(records) == 1
    assert records[0]["state"] == "historical"
    assert "decision" not in records[0]["data"]
    assert store.memory()[0]["legacy_analysis"] == report
    assert not store.active()


@pytest.mark.parametrize("state", ["complete", "aborted"])
def test_archived_verification_never_exports_orders_or_strategy_memory(store, tmp_path, state):
    from veyquant.decision_runtime import Runtime

    store.record_verification(
        {
            "id": "a" * 32,
            "state": state,
            "started": NOW - 10,
            "finished": NOW,
            "data": {"decision": final("SUBMIT"), "settings_revision": 1},
        }
    )
    runtime = Runtime.__new__(Runtime)
    runtime.store, runtime.clock = store, lambda: NOW
    runtime.state, runtime.source_state = "observing", {}
    runtime.configuration = lambda: {
        "models": {"middle": "gpt-5.6-luna", "research": "gpt-5.6-sol"}
    }
    runtime.next_sessions, runtime.orders_blocked = [], False
    runtime.report_path = str(tmp_path / "report.json")
    runtime.publish()
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["proposals"] == []
    assert report["runs"][0]["kind"] == "verification"
    assert report["runs"][0]["decision"]["action"] == "SUBMIT"
    assert not store.active() and store.memory() == []


@pytest.mark.parametrize("state", ["running", "skipped_busy", "skipped_orders", "interrupted"])
def test_runtime_publishes_admission_before_first_model_trace(store, tmp_path, state):
    from veyquant.decision_runtime import Runtime

    identity = store.admit("waiting-context", "manual", NOW, {"instruction": "검토"})
    if state != "running":
        store.db.execute("UPDATE decision_runs SET state=? WHERE id=?", (state, identity))
    runtime = Runtime.__new__(Runtime)
    runtime.store, runtime.clock = store, lambda: NOW
    runtime.state, runtime.source_state = "observing", {}
    runtime.configuration = lambda: {
        "models": {"middle": "gpt-5.6-luna", "research": "gpt-5.6-sol"}
    }
    runtime.next_sessions, runtime.orders_blocked = [], False
    runtime.report_path = str(tmp_path / "report.json")
    runtime.publish()
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["runs"][0]["state"] == state
    assert report["runs"][0]["trace"] == []
    assert report["proposals"] == []
    assert report["active"] == (state == "running")


@pytest.mark.parametrize("sides", [("BUY", "BUY"), ("SELL", "SELL"), ("SELL", "BUY")])
async def test_two_stock_basket_dispatches_before_first_fill_without_duplicates(worker, sides):
    from test_live_worker import output, update

    w = worker
    w.observations.universe.stocks["000660"] = {"symbol": "000660"}
    update(w, symbol="000660")

    async def limits(symbol):
        return {"lowerLimitPrice": "70", "upperLimitPrice": "130"}

    w.broker.price_limits = limits
    data = basket(w, side=sides[0]) | {"protocol": "decision-v2", "updated_at": w.clock()}
    for symbol, side in zip(("005930", "000660"), sides, strict=True):
        if side == "SELL":
            w.db.execute("INSERT INTO execution_positions VALUES (?,2,'200')", (symbol,))
            w.db.execute("INSERT INTO execution_anchor VALUES (?,0)", (symbol,))
    original_holdings = w.broker.holdings

    async def holdings(account):
        value = await original_holdings(account)
        value["items"].append(value["items"][0] | {"symbol": "000660"})
        return value

    w.broker.holdings = holdings
    batch = data["proposals"][0]
    batch["intents"].append(
        batch["intents"][0]
        | {"symbol": "000660", "side": sides[1], "evidence_ids": ["market:000660"]}
    )
    batch["context"]["quotes"].append(w.observations.quotes["000660"])

    def publish():
        with open(w.report_path, "w") as f:
            json.dump(data | {"updated_at": w.clock()}, f)

    publish()
    await w.tick()
    assert len(w.transport.creates) == 1, output(w)
    first = w.transport.creates[0]
    oid = first["clientOrderId"]
    w.broker.details["broker-" + oid] = w.broker.detail(oid, 2, side=sides[0])
    update(w, seconds=2)
    update(w, symbol="000660")
    publish()
    await w.tick()
    assert len(w.transport.creates) == 2, output(w)
    second = w.transport.creates[1]
    assert second["symbol"] == "000660"
    oid = second["clientOrderId"]
    w.broker.details["broker-" + oid] = w.broker.detail(oid, 2, side=sides[1]) | {
        "symbol": "000660"
    }
    publish()
    await w.tick()
    assert len(w.transport.creates) == 2
