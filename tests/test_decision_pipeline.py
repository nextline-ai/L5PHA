import asyncio
import copy
from datetime import datetime

import pytest

from veyquant.decision_pipeline import DecisionPipeline, validate_decision
from veyquant.decision_store import DecisionStore
from veyquant.surveillance import observe, schedule, volume_zscore

NOW = datetime.fromisoformat("2026-09-09T10:00:00+09:00").timestamp()
CALENDAR = {
    "today": {
        "date": "2026-09-09",
        "integrated": {
            "regularMarket": {
                "startTime": "2026-09-09T09:00:00+09:00",
                "endTime": "2026-09-09T15:30:00+09:00",
            }
        },
    }
}


@pytest.fixture
def store(tmp_path):
    value = DecisionStore(tmp_path / "decisions.db")
    yield value
    value.db.close()


def context(count=1):
    symbols = [f"{i:06d}" for i in range(1, count + 1)]
    details = {
        s: {
            "symbol": s,
            "quote": {"price": "100"},
            "daily_bars": [{"date": "2026-09-08", "close": "100"}],
            "orderbook": {"asks": []},
            "trades": [],
        }
        for s in symbols
    }
    return {
        "as_of": NOW,
        "cash": "10000",
        "holdings": [{"symbol": symbols[0], "quantity": "2"}],
        "order_generation": 1,
        "settings_revision": 1,
        "open_orders": [],
        "unresolved_submission": False,
        "risk_limits": {"max_order_krw": "1000"},
        "details": details,
        "review_universe": [{"symbol": s} for s in symbols],
        "comparison_table": {"columns": ["symbol"], "rows": [[s] for s in symbols]},
        "mandatory_evidence": [],
    }


def make_brief(level="NORMAL"):
    return {
        "severity": level,
        "summary": "근거 검토",
        "candidates": [{"symbol": "000001", "reason": "검토 대상"}],
        "evidence_ids": ["market:000001"],
        "uncertainties": ["향후 실적"],
    }


def final(action="NO_ACTION"):
    return {
        "action": action,
        "summary": "기다립니다",
        "detailed_explanation": "위험 한도를 확인했지만 실적 근거가 충분하지 않아 관망합니다.",
        "counterargument": "가격 상승 가능",
        "uncertainty": "실적 변화",
        "memory_book": "테스트 투자 근거와 미해결 사항. 체결은 계좌에서 확인.",
        "intents": []
        if action == "NO_ACTION"
        else [
            {
                "symbol": "000001",
                "side": "BUY",
                "quantity": 2,
                "limit_price": "100",
                "rationale": "근거와 한도 확인",
                "evidence_ids": ["market:000001"],
            }
        ],
    }


async def run_pipeline(
    store, kind="scheduled", level="NORMAL", count=1, mutate=None, model_override=None
):
    calls, snapshots = [], []

    async def model(role, payload):
        calls.append((role, payload))
        if model_override:
            result = await model_override(role, payload)
        else:
            result = make_brief(level) if role == "middle" else final()
        return result, {
            "model": "fixture",
            "status": "received",
            "input_tokens": 1,
            "output_tokens": 1,
        }

    async def get_context(operation, request):
        snapshots.append(operation)
        value = context(count)
        if mutate:
            mutate(value, len(snapshots))
        return value

    async def news(*args):
        return []

    identity = store.admit("test", kind, NOW, {"instruction": "검토 제안"})
    pipeline = DecisionPipeline(model, get_context, news, store, clock=lambda: NOW)
    result = await pipeline.run(
        identity, kind, {"instruction": "검토 제안"}, {"settings_revision": 1}
    )
    return result, calls, snapshots, pipeline


async def test_failed_batch_cancels_other_batches_before_pipeline_returns(store):
    started, cancelled = [], []

    async def model(role, payload):
        index = len(started)
        started.append(index)
        if index == 0:
            await asyncio.sleep(0.01)
            raise ValueError("fixture_failure")
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(index)
        return make_brief()

    result, calls, _, pipeline = await run_pipeline(store, count=200, model_override=model)
    assert result is None
    assert len(started) <= 4
    assert len(cancelled) == len(started) - 1
    count = len(calls)
    await asyncio.sleep(0.02)
    assert len(calls) == count and all(role == "middle" for role, _ in calls)
    assert len(pipeline.trace) == len(calls)


async def test_batch_evidence_and_explicit_candidate_pool_have_matching_scope(store):
    store.evidence(
        "new_warning", {"symbol": "000041", "warning": {"type": "fixture"}}, NOW, "warning:41"
    )
    store.evidence("surveillance", {"summary": "시장 전체 경고"}, NOW, "global-warning")

    async def model(role, payload):
        if role == "research":
            return final()
        if payload["task"] == "review_batch":
            table = payload["stocks"]
            symbols = (
                {table["shared"]["symbol"]}
                if "symbol" in table["shared"]
                else {row[table["columns"].index("symbol")] for row in table["rows"]}
            )
            assert table["count"] == len(symbols)
            assert set(payload["allowed_candidate_symbols"]) == symbols
            assert all(
                not e.get("symbol") or e["symbol"] in symbols for e in payload["recent_evidence"]
            )
            assert any(e.get("summary") == "시장 전체 경고" for e in payload["recent_evidence"])
            assert any(e.get("symbol") == "000041" for e in payload["recent_evidence"]) == (
                "000041" in symbols
            )
            s = sorted(symbols)[0]
            return make_brief() | {
                "candidates": [{"symbol": s, "reason": "해당 묶음 후보"}],
                "evidence_ids": ["market:" + s],
            }
        assert len(payload["allowed_candidate_symbols"]) == 80
        return make_brief()

    result, _, _, _ = await run_pipeline(store, count=80, model_override=model)
    assert result


async def test_short_citations_round_trip_to_exact_original_evidence(store):
    canonical = "7a" * 32
    import hashlib

    alias = "E_" + hashlib.sha256(canonical.encode()).hexdigest()[:16]
    store.evidence("warning", {"kind": "warning", "summary": "확인할 자료"}, NOW, canonical)

    async def model(role, payload):
        if role == "research":
            return final()
        value = make_brief()
        value["evidence_ids"].append(alias)
        if payload["task"] == "review_batch":
            assert alias in payload["available_evidence_ids"]
            assert canonical not in str(payload)
        return value

    result, _, _, pipeline = await run_pipeline(store, model_override=model)
    assert result["citation_aliases"] == {alias: canonical}
    assert result["brief"]["evidence_ids"] == ["market:000001", canonical]
    assert pipeline.citation_values("E0001x", reverse=True) == "E0001x"


@pytest.mark.parametrize("kind", ["scheduled", "manual"])
@pytest.mark.parametrize("level", ["NORMAL", "WARN", "CRITICAL"])
async def test_regular_and_manual_always_brief_then_decision(store, kind, level):
    result, calls, snapshots, _ = await run_pipeline(store, kind, level)
    assert result["decision"]["action"] == "NO_ACTION"
    assert [r for r, _ in calls] == ["middle", "research"]
    assert result["merge_skipped"] == "single_batch_already_complete"
    assert snapshots == ["initial", "refresh", "refresh"]
    payload = calls[-1][1]
    assert payload["account"]["cash"] == "10000"
    assert "risk_limits" in payload["account"]
    assert payload["manual_instruction"] == ("검토 제안" if kind == "manual" else None)


@pytest.mark.parametrize("level,expected", [("NORMAL", False), ("WARN", False), ("CRITICAL", True)])
async def test_critical_requires_critical_brief(store, level, expected):
    result, calls, snapshots, _ = await run_pipeline(store, "critical", level)
    assert result
    assert any(r == "research" for r, _ in calls) is expected
    assert result["brief"]["handoff"] == ("decision" if expected else "deferred")
    assert "handoff" not in result["trace"][0]["decision"]
    if not expected:
        assert "decision" not in result and "memory_update" not in result
        assert snapshots == ["initial"]
        assert store.history(compact=True)[0]["data"]["brief"]["handoff"] == "deferred"
        page = store.brief_interval(0, store.run_cursor(store.history()[0]["id"]) + 1)
        assert page["items"][0]["summary"] == result["brief"]["summary"]


async def test_critical_merge_can_defer_all_critical_batches(store):
    async def model(role, payload):
        assert role == "middle"
        assert payload["request_kind"] == "critical"
        assert "independent gate" in payload["critical_review_policy"]
        gate = payload["decision_gate_context"]
        assert gate["holding_symbols"] == ["000001"]
        assert set(gate) == {"holding_symbols", "previous_memory"}
        assert set(gate["previous_memory"]) == {"as_of", "content"}
        assert "cash" not in gate and "holdings" not in gate
        if payload["task"] == "merge_decision_brief":
            assert all(b["severity"] == "CRITICAL" for b in payload["batches"])
            return make_brief("WARN")
        value = make_brief("CRITICAL")
        value["candidates"] = []
        value["evidence_ids"] = payload["available_evidence_ids"][:1]
        return value

    result, calls, snapshots, _ = await run_pipeline(
        store, "critical", count=41, model_override=model
    )
    assert result["brief"]["handoff"] == "deferred"
    assert len(calls) == 3 and snapshots == ["initial"]
    assert "decision" not in result


@pytest.mark.parametrize("mutation", ["holding", "order", "pending", "generation", "policy"])
async def test_account_changes_abort_before_decision(store, mutation):
    def mutate(c, n):
        if n < 2:
            return
        if mutation == "holding":
            c["holdings"].append({"symbol": "005930", "quantity": "1"})
        elif mutation == "order":
            c["open_orders"] = [{"symbol": "000001"}]
        elif mutation == "pending":
            c["unresolved_submission"] = True
        elif mutation == "generation":
            c["order_generation"] += 1
        else:
            c["settings_revision"] += 1

    result, calls, _, _ = await run_pipeline(store, mutate=mutate)
    assert result is None
    assert not any(r == "research" for r, _ in calls)
    assert store.history()[0]["state"] == "aborted"


async def test_account_change_during_final_model_discards_intents(store):
    def mutate(c, n):
        if n == 3:
            c["order_generation"] += 1

    result, calls, _, _ = await run_pipeline(store, mutate=mutate)
    assert result is None
    assert calls[-1][0] == "research"


@pytest.mark.parametrize("failure", [ValueError("bad_brief"), TimeoutError()])
async def test_brief_failure_has_no_raw_data_fallback(store, failure):
    async def model(role, payload):
        raise failure

    result, calls, _, _ = await run_pipeline(store, model_override=model)
    assert result is None
    assert all(r == "middle" for r, _ in calls)


async def test_two_hundred_stocks_batched_with_at_most_three_parallel(store):
    concurrent = peak = 0

    async def model(role, payload):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.001)
        concurrent -= 1
        if payload.get("task") == "review_batch":
            result = make_brief()
            symbol = payload["allowed_candidate_symbols"][0]
            result["candidates"][0]["symbol"] = symbol
            result["evidence_ids"] = ["market:" + symbol]
            return result
        return make_brief() if role == "middle" else final()

    result, calls, _, pipeline = await run_pipeline(store, count=200, model_override=model)
    assert result and peak == 3
    assert pipeline.summary_calls == 6
    assert sum(p["task"] == "review_batch" for _, p in calls) == 5


async def test_six_reads_then_reject_seventh_without_executing(store):
    async def model(role, payload):
        return (
            make_brief()
            if role == "middle"
            else {
                "action": "READ",
                "tool": "market",
                "arguments": {"symbol": "000001", "fields": ["quote"]},
            }
        )

    result, _, _, pipeline = await run_pipeline(store, model_override=model)
    assert result is None and pipeline.tools == 6
    assert store.history()[0]["data"]["error"] == "read_tool_budget"


@pytest.mark.parametrize(
    "tool,args",
    [
        ("sql", {"query": "select *"}),
        ("market", {"symbol": "999999", "fields": ["quote"]}),
        ("market", {"symbol": "000001", "fields": ["credentials"]}),
        ("news", {"symbols": ["000001"], "topics": ["account cash private"]}),
    ],
)
async def test_no_unapproved_tools_or_queries(store, tool, args):
    async def model(role, payload):
        return (
            make_brief()
            if role == "middle"
            else {"action": "READ", "tool": tool, "arguments": args}
        )

    result, _, _, _ = await run_pipeline(store, model_override=model)
    assert result is None


def test_single_pipeline_manual_skip_critical_coalescing_and_crash(store):
    first = store.admit("first", "manual", NOW, {})
    assert first
    assert not store.admit("other", "scheduled", NOW, {})
    for identity in ["c1", "c1", "c2"]:
        assert not store.admit(identity, "critical", NOW, {"evidence_ids": [identity]})
    assert store.get("pending_critical") == ["c1", "c2"]
    store.recover(NOW + 1)
    assert not store.active()
    assert not store.admit("first", "manual", NOW + 2, {})


def test_anomaly_coalescing_hysteresis_persists_restart(store):
    store.edge("price:s", True, NOW, {"condition": "price"})
    store.edge("volume:s", True, NOW + 20, {"condition": "volume"})
    assert not store.take_signals(NOW + 299)
    assert len(store.take_signals(NOW + 300)) == 2
    store.edge("price:s", True, NOW + 400, {})
    store.edge("price:s", None, NOW + 410, {})
    store.edge("price:s", True, NOW + 420, {})
    assert not store.take_signals(NOW + 800)
    store.edge("price:s", False, NOW + 810, {})
    store.edge("price:s", True, NOW + 820, {})
    assert len(store.take_signals(NOW + 1120)) == 1


def test_schedule_uses_actual_close_including_auction():
    times = [
        datetime.fromtimestamp(s["at"]).astimezone().strftime("%H:%M")
        for s in schedule(CALENDAR, NOW)
    ]
    from zoneinfo import ZoneInfo

    times = [
        datetime.fromtimestamp(s["at"], ZoneInfo("Asia/Seoul")).strftime("%H:%M")
        for s in schedule(CALENDAR, NOW)
    ]
    assert times == ["10:30", "14:00"]
    delayed = copy.deepcopy(CALENDAR)
    delayed["today"]["integrated"]["regularMarket"]["startTime"] = "2026-09-09T10:00:00+09:00"
    assert schedule(delayed, NOW)[0]["at"] == schedule(CALENDAR, NOW)[0]["at"] + 3600
    assert schedule({"today": {"date": "2026-09-09", "integrated": None}}, NOW) == []


def test_short_session_has_two_distinct_intraday_slots():
    shortened = copy.deepcopy(CALENDAR)
    shortened["today"]["integrated"]["regularMarket"]["endTime"] = "2026-09-09T11:00:00+09:00"
    slots = schedule(shortened, NOW)
    opening = datetime.fromisoformat("2026-09-09T09:00:00+09:00").timestamp()
    assert [s["at"] - opening for s in slots] == [2400, 4800]
    assert [s["name"] for s in slots] == ["morning", "afternoon"]


@pytest.mark.parametrize("date", ["2026-09-12", "2026-09-13"])
def test_no_scheduled_decision_on_weekends_even_with_calendar_hours(date):
    weekend = copy.deepcopy(CALENDAR)
    weekend["today"]["date"] = date
    regular = weekend["today"]["integrated"]["regularMarket"]
    regular.update(startTime=f"{date}T09:00:00+09:00", endTime=f"{date}T15:30:00+09:00")
    assert schedule(weekend, datetime.fromisoformat(f"{date}T10:00:00+09:00").timestamp()) == []


def test_current_thresholds_detect_falling_price_with_volume_confirmation(store):
    market = {
        "last_realtime_at": NOW,
        "metrics": [
            {
                "symbol": "000001",
                "as_of": NOW,
                "change_5m": -0.05,
                "volume_zscore": 9,
                "spread_bp": 100,
                "book_as_of": NOW,
            }
        ],
    }
    observe(store, market, CALENDAR, NOW)
    signals = store.take_signals(NOW + 300)
    conditions = {s["condition"] for s in signals}
    assert "price_5pct" in conditions and "spread_100bp" in conditions
    assert "price_volume" in conditions
    assert volume_zscore(20, [1.0, 2.0] * 10) > 4
    assert volume_zscore(20, [1.0] * 19) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda i: i.update(quantity=100000),
        lambda i: i.update(quantity=True),
        lambda i: i.update(limit_price="NaN"),
        lambda i: i.update(evidence_ids=["fabricated"]),
        lambda i: i.update(symbol="005930"),
    ],
)
def test_trade_intents_are_strict_and_never_resized(mutate):
    value = final("SUBMIT")
    mutate(value["intents"][0])
    with pytest.raises(ValueError):
        validate_decision(value, {"000001"}, {"market:000001"}, context())


async def test_initial_collection_timeout_is_identified_without_any_model_fallback(store):
    calls = []

    async def model(*args):
        calls.append(args)
        raise AssertionError("must not call a model")

    async def stalled(*args):
        raise TimeoutError()

    identity = store.admit("timeout-test", "manual", NOW, {})
    pipeline = DecisionPipeline(model, stalled, stalled, store, clock=lambda: NOW)
    assert await pipeline.run(identity, "manual", {}, {"settings_revision": 1}) is None
    record = store.history()[0]["data"]
    assert record["error"] == "context_collection_timeout"
    assert record["phase"] == "context_collection"
    assert calls == []


@pytest.mark.parametrize("bad", [None, "", "가" * 6001, {"text": "invalid"}])
def test_detail_explanation_required_and_validated(bad):
    value = final() | {"detailed_explanation": bad}
    with pytest.raises(ValueError, match="invalid_analysis_text"):
        validate_decision(value, {"000001"}, {"market:000001"}, context())


async def test_detailed_explanation_is_archived_but_not_automatically_reinjected(store):
    from veyquant.decision_runtime import decision_preview

    result, _, _, _ = await run_pipeline(store)
    detail = result["decision"]["detailed_explanation"]
    assert store.history()[0]["data"]["decision"]["detailed_explanation"] == detail
    assert "detailed_explanation" not in decision_preview(result["decision"])
    assert store.memory_book()["content"] == result["decision"]["memory_book"]
    assert detail not in str(store.memory_book())


def test_decision_summary_is_short_while_detailed_explanation_can_be_long():
    value = final() | {"summary": "요" * 300, "detailed_explanation": "설" * 6000}
    validate_decision(value, {"000001"}, {"market:000001"}, context())
    with pytest.raises(ValueError, match="invalid_analysis_text"):
        validate_decision(value | {"summary": "요" * 301}, set(), set(), context())


def test_citation_ids_are_stable_across_different_evidence_order():
    a = DecisionPipeline(None, None, None, None)
    b = DecisionPipeline(None, None, None, None)
    a.bind_citations({"first-filing"})
    a.bind_citations({"second-filing"})
    b.bind_citations({"second-filing"})
    b.bind_citations({"first-filing"})
    assert a.citation_aliases == b.citation_aliases
    assert a.citation_values("first-filing") != a.citation_values("second-filing")
    assert a.citation_values("E0421", reverse=True) == "E0421"


def test_final_sell_can_exceed_buy_cap_but_cannot_exceed_owned_quantity():
    from veyquant.decision_pipeline import validate_decision

    current = context()
    current["risk_limits"]["max_order_krw"] = "100"
    current["holdings"][0]["managed_quantity"] = 2
    current["order_constraints"] = {"stocks": {"000001": {"sellable_quantity": 2}}}
    value = final("SUBMIT")
    value["intents"][0]["side"] = "SELL"
    assert validate_decision(value, {"000001"}, {"market:000001"}, current) == value
    value["intents"][0]["quantity"] = 3
    with pytest.raises(ValueError, match="invalid_sell_quantity"):
        validate_decision(value, {"000001"}, {"market:000001"}, current)


@pytest.mark.parametrize("kind", ["scheduled", "manual", "critical"])
async def test_every_middle_review_and_merge_sees_only_minimal_holdings(store, kind):
    calls = []

    async def model(role, payload):
        if role == "middle":
            holding = payload["holding_context"]
            assert holding["positions"] == [{"symbol": "000001", "quantity": "2"}]
            assert "cash" not in holding and "risk_limits" not in holding
            calls.append(payload["task"])
            symbol = payload["allowed_candidate_symbols"][0]
            return make_brief("WARN") | {
                "candidates": [{"symbol": symbol, "reason": "보유종목 대조"}],
                "evidence_ids": ["market:" + symbol],
            }
        return final()

    result, _, _, _ = await run_pipeline(store, kind=kind, count=80, model_override=model)
    assert result is not None
    assert calls.count("review_batch") == 2
    assert calls.count("merge_decision_brief") == 1


async def test_transient_followup_resumes_only_current_turn_not_tools_or_brief(store):
    attempts = []

    async def model(role, payload):
        if role == "middle":
            return make_brief()
        attempts.append(copy.deepcopy(payload))
        if len(attempts) == 1:
            return {
                "action": "READ",
                "tool": "market",
                "arguments": {"symbol": "000001", "fields": ["orderbook"]},
            }
        if len(attempts) == 2:
            raise ValueError("provider_connection_lost")
        return final()

    result, calls, snapshots, pipeline = await run_pipeline(store, model_override=model)
    assert result["decision"]["action"] == "NO_ACTION"
    assert attempts[1] == attempts[2]
    assert pipeline.tools == 1 and pipeline.summary_calls == 1
    assert snapshots == ["initial", "refresh", "refresh"]  # Final account check remains mandatory.
    assert [t["status"] for t in pipeline.trace] == ["received", "received", "failed", "received"]


@pytest.mark.parametrize(
    "code,expected",
    [("provider_timeout", 2), ("openai_http_429_insufficient_quota", 1), ("invalid_model_json", 1)],
)
async def test_model_recovery_is_bounded_and_does_not_retry_billing_or_invalid_output(
    store, code, expected
):
    attempts = []

    async def model(role, payload):
        if role == "middle":
            return make_brief()
        attempts.append(1)
        raise ValueError(code)

    result, calls, snapshots, pipeline = await run_pipeline(store, model_override=model)
    assert result is None and len(attempts) == expected
