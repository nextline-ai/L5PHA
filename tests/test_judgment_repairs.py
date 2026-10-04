import hashlib
from datetime import datetime

import pytest
from test_decision_pipeline import NOW, context, final, make_brief, run_pipeline
from test_decision_pipeline import store as store
from test_live_worker import worker as worker

from veyquant import model_context as views
from veyquant.decision_pipeline import validate_decision
from veyquant.decision_runtime import surveillance_summary
from veyquant.research_context import ResearchContext


def test_incident_age_and_recovery_do_not_hide_current_warning_or_mandatory_filing():
    def item(identity, kind, at, conditions=()):
        return {
            "id": identity,
            "kind": kind,
            "as_of": at,
            "severity": "CRITICAL",
            "signals": [{"condition": c} for c in conditions],
        }

    records = [
        item("yesterday", "surveillance", NOW - 86400, ["realtime_gap"]),
        item("failed", "surveillance_failure", NOW - 100),
        item("success", "surveillance", NOW - 50, ["spread_50bp"]),
        item("recovered", "surveillance", NOW - 200, ["heartbeat", "realtime_gap"]),
        item("filing", "dart_important", NOW - 86400),
    ]
    kept = views.current_evidence(records, NOW, {"realtime_state": "healthy"})
    assert {e["id"] for e in kept} == {"success", "filing"}
    assert len(records) == 5
    unresolved = records + [item("new_failure", "surveillance_failure", NOW - 1)]
    assert "new_failure" in {e["id"] for e in views.current_evidence(unresolved, NOW)}
    assert "recovered" in {e["id"] for e in views.current_evidence(records, NOW)}
    assert views.evidence_view(records[0])["as_of_kst"].startswith("2026-09-08T10:00:00+09:00")


def test_prior_proposal_feedback_preserves_original_intent_index_and_uncertainty():
    record = {
        "id": "run",
        "decision": {
            "action": "SUBMIT",
            "intents": [{"symbol": "000001", "side": "BUY"}, {"symbol": "000002", "side": "SELL"}],
        },
    }
    identity = hashlib.sha256(b"run:1").hexdigest()
    memory = views.memory_view(
        [record],
        {"000002"},
        [
            {
                "event_id": identity,
                "reason": "report_expired",
                "state": None,
                "secret": "never_expose",
            }
        ],
    )
    assert memory[0]["relevant_intents"][0]["execution"] == {
        "reason": "report_expired",
        "state": None,
    }
    assert views.memory_view([record], {"000002"})[0]["relevant_intents"][0]["execution"] == {
        "state": "unconfirmed"
    }


async def test_pipeline_can_sell_managed_holding_not_selected_by_middle(store):
    def managed(value, _):
        value["holdings"][0]["managed_quantity"] = 2
        value["order_constraints"] = {"stocks": {"000001": {"sellable_quantity": "2"}}}

    async def model(role, payload):
        if role == "middle":
            return make_brief() | {"candidates": []}
        assert payload["sell_eligible_symbols"] == ["000001"]
        assert "market:000001" in payload["available_evidence_ids"]
        decision = final("SUBMIT")
        decision["intents"][0]["side"] = "SELL"
        return decision

    result, _, _, _ = await run_pipeline(store, mutate=managed, model_override=model)
    assert result["decision"]["intents"][0]["side"] == "SELL"


@pytest.mark.parametrize(
    "side,owned,sellable", [("BUY", 2, "2"), ("SELL", 0, "2"), ("SELL", 1, "2"), ("SELL", 2, "1")]
)
def test_non_candidate_buy_and_external_or_unavailable_sell_remain_rejected(side, owned, sellable):
    account = context()
    account["holdings"][0]["managed_quantity"] = owned
    account["order_constraints"] = {"stocks": {"000001": {"sellable_quantity": sellable}}}
    decision = final("SUBMIT")
    decision["intents"][0]["side"] = side
    with pytest.raises(ValueError):
        validate_decision(decision, set(), {"market:000001"}, account)


async def test_risk_uses_newer_stream_without_refreshing_stale_timestamp(worker):
    w = worker

    async def prices(symbols):
        return [
            {
                "symbol": s,
                "currency": "KRW",
                "lastPrice": "100",
                "timestamp": datetime.fromtimestamp(w.clock() - 60).astimezone().isoformat(),
            }
            for s in symbols
        ]

    async def limits(symbol):
        return {"lowerLimitPrice": "70", "upperLimitPrice": "130"}

    w.broker.prices, w.broker.price_limits = prices, limits
    await w.reconcile()
    w.db.execute("INSERT INTO execution_positions VALUES('005930',2,'200')")
    w.observations.metrics.last_realtime_at = w.clock()
    candidates = [f"{i:06d}" for i in range(1, 13)]
    result = await ResearchContext(w, w.clock).account(candidates)
    assert len(result["order_constraints"]["stocks"]) == 13
    assert result["order_constraints"]["stocks"]["005930"]["sellable_quantity"] == "2"
    assert result["risk_status"]["state"] == "within_limit"
    for age in (9, 16, 27, 30):
        w.observations.quotes["005930"]["as_of"] = w.clock() - age
        result = await ResearchContext(w, w.clock).account()
        assert result["risk_status"]["state"] == "within_limit"
        assert result["risk_status"]["price_observations"][0]["age_seconds"] == age
        assert result["quotes"][0]["as_of"] == w.clock() - age
        from dataclasses import replace

        strict = replace(
            w.snapshot,
            as_of=w.clock(),
            managed_quantities={"005930": 2},
            quotes={"005930": ("100", w.clock() - age)},
        )
        with pytest.raises(ValueError, match="stale_price"):
            w.core._valuation(strict, w.clock())
    w.observations.quotes["005930"]["as_of"] = w.clock() - 31
    w.observations.db.execute("DELETE FROM latest WHERE topic='orderbook:kr:005930'")
    result = await ResearchContext(w, w.clock).account()
    assert result["risk_status"]["state"] == "unavailable"
    assert result["risk_status"]["reason"] == "stale_price"
    assert result["risk_status"]["stale_symbols"] == "005930"
    assert result["data_health"]["realtime_state"] == "healthy"
    assert result["quotes"][0]["as_of"] == w.clock() - 60
    w.db.execute("DELETE FROM execution_days")
    w.observations.quotes["005930"]["as_of"] = w.clock()
    result = await ResearchContext(w, w.clock).account()
    assert result["risk_status"]["reason"] == "daily_baseline_missing"
    w.snapshot = None
    result = await ResearchContext(w, w.clock).account()
    assert result["risk_status"]["reason"] == "reconciliation_required"
    assert result["risk_status"]["stale_symbols"] == ""


def test_surveillance_ranges_cover_omitted_examples_with_explicit_units():
    signals = [
        {"condition": "price_3pct", "symbol": f"{n:06d}", "metrics": {"change_5m": v}}
        for n, v in enumerate([0.03, 0.04, -0.08, 0.1])
    ]
    result = surveillance_summary(signals)
    assert result["conditions"][0]["metric_ranges"]["change_5m"] == [-0.08, 0.1]
    assert result["conditions"][0]["omitted_count"] == 1
    assert "0.03 = 3%" in result["units"]


def test_return_bases_and_corporate_action_discontinuity_are_explicit():
    source = {
        "as_of": NOW,
        "quote": {"price": "5100", "as_of": NOW},
        "daily_price_basis": "adjusted",
        "daily_bars": [
            {"date": "2026-09-10", "close": "733", "volume": "0"},
            {"date": "2026-09-11", "close": "4760", "volume": "156535"},
        ],
        "warnings": [],
    }
    value = views.stock({"symbol": "285800"}, source)
    features = value["indicators"]
    assert features["previous_close"] == "733"
    assert features["return_1d_pct"] == 549.386
    assert features["quote_vs_last_close_pct"] == 7.143
    assert features["price_basis"] == "adjusted"
    assert features["daily_discontinuity"]["status"] == "verification_required"
    assert features["daily_discontinuity"]["latest"]["date"] == "2026-09-11"
    assert value["warnings_as_of"] == NOW
    assert views.indicators({})["price_basis"] == "unknown"


async def test_initial_health_is_sampled_after_public_collection(worker, monkeypatch):
    w = worker
    await w.reconcile()
    ctx = ResearchContext(w, w.clock)
    initial = {
        "as_of": w.clock(),
        "data_health": ctx.health(),
        "holdings": [],
        "open_orders": [],
        "unresolved_submission": False,
        "conditional_orders": 0,
    }
    w.observations.universe.stocks["005930"].update(
        {"name": "삼성전자", "market": "KOSPI", "eligibility": "eligible"}
    )
    now = [w.clock()]
    ctx.clock = lambda: now[0]

    async def account(*_):
        return initial

    async def book(symbol):
        now[0] += 120
        w.observations.metrics.last_realtime_at = now[0]
        return {"timestamp": now[0], "bids": [], "asks": []}

    monkeypatch.setattr(ctx, "account", account)
    monkeypatch.setattr(w.broker, "orderbook", book, raising=False)
    monkeypatch.setattr(w.broker, "trades", lambda s: empty(), raising=False)

    async def empty():
        return []

    result = await ctx.handle({"operation": "initial", "request": {}})
    health = views.health_view(result)
    assert health["collection_started_at"] == initial["data_health"]["as_of"]
    assert health["collection_completed_at"] == now[0] > health["collection_started_at"]
    assert health["as_of"] == now[0]
    assert result["as_of"] == initial["as_of"]
    assert all(d["as_of"] <= health["as_of"] for d in result["details"].values())
