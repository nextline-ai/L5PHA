import copy
import json
from datetime import date, timedelta

import pytest

from veyquant import model_context as views
from veyquant.decision_pipeline import BRIEF_SCHEMA, DecisionPipeline
from veyquant.decision_store import DecisionStore
from veyquant.input_budget import compact_json, enforce_input
from veyquant.shadow_inference import MODEL_PRESETS, prepare_model_input

NOW = 1789000000
RAW = "RAW_PRIVATE_BROKER_RESPONSE_MUST_STAY_SERVER_SIDE"


def detail(symbol="000001"):
    bars = [
        {
            "date": (date(2026, 8, 1) + timedelta(days=i)).isoformat(),
            "open": str(100 + i),
            "high": str(102 + i),
            "low": str(99 + i),
            "close": str(101 + i),
            "volume": "1000",
            "raw": RAW,
        }
        for i in range(24)
    ]
    return {
        "symbol": symbol,
        "as_of": NOW - 100,
        "quote": {
            "symbol": symbol,
            "price": "124",
            "currency": "KRW",
            "as_of": NOW - 100,
            "raw": RAW,
        },
        "daily_bars": bars,
        "orderbook": {
            "timestamp": NOW - 100,
            "currency": "KRW",
            "raw": RAW,
            "asks": [{"price": str(125 + i), "volume": "100", "raw": RAW} for i in range(10)],
            "bids": [{"price": str(124 - i), "volume": "100", "raw": RAW} for i in range(10)],
        },
        "trades": [
            {"price": "124", "volume": "1", "timestamp": NOW - i, "raw": RAW} for i in range(30)
        ],
        "warnings": [{"warningType": "UNKNOWN_NEW_RESTRICTION", "raw": RAW}],
        "raw": RAW,
    }


def frozen_context(count=200):
    symbols = [f"{i:06d}" for i in range(1, count + 1)]
    constraints = {
        s: {
            "price_limits": {"lowerLimitPrice": "70", "upperLimitPrice": "160", "raw": RAW},
            "warnings": [{"warningType": "INVESTMENT_WARNING", "raw": RAW}],
            "sellable_quantity": "2",
        }
        for s in symbols[:12]
    }
    return {
        "as_of": NOW,
        "currency": "KRW",
        "cash": "10000",
        "capital_used": "2000",
        "live_requested": False,
        "conditional_orders": 0,
        "unresolved_submission": False,
        "order_generation": 1,
        "settings_revision": 1,
        "holdings": [{"symbol": symbols[0], "quantity": "4", "managed_quantity": "2", "raw": RAW}],
        "open_orders": [],
        "risk_limits": {
            "capital_krw": "100000",
            "max_order_krw": "10000",
            "max_daily_loss_krw": "1000",
            "raw": RAW,
        },
        "risk_status": {"state": "within_limit", "as_of": NOW, "daily_pnl_krw": "-200", "raw": RAW},
        "quotes": [
            {"symbol": s, "price": "125", "currency": "KRW", "as_of": NOW, "raw": RAW}
            for s in symbols[:12]
        ],
        "order_constraints": {
            "type": "LIMIT",
            "time_in_force": "DAY",
            "currency": "KRW",
            "sell_scope": "AI-managed shares only",
            "commission_rate": "0.001",
            "sell_cost_reserve_rate": "0.01",
            "session": {
                "scope": "regular",
                "starts_at": NOW - 3600,
                "ends_at": NOW + 3600,
                "is_open": True,
                "raw": RAW,
            },
            "stocks": constraints,
            "raw": RAW,
        },
        "details": {s: detail(s) for s in symbols},
        "review_universe": [
            {
                "symbol": s,
                "name": "회사" + s,
                "market": "KOSPI",
                "eligibility": "eligible",
                "raw": RAW,
            }
            for s in symbols
        ],
        "comparison_table": {
            "columns": ["symbol", "name", "market", "eligibility", "price", "as_of", "raw"],
            "rows": [
                [f"{i:06d}", "회사" + str(i), "KOSPI", "eligible", "124", NOW - 100, RAW]
                for i in range(1, 2765)
            ],
        },
        "mandatory_evidence": [
            {
                "id": "mandatory1",
                "kind": "new_warning",
                "symbol": symbols[0],
                "warning": {"warningType": "INVESTMENT_WARNING", "raw": RAW},
            }
        ],
        "raw": RAW,
    }


def test_stock_projection_computes_features_without_raw_history():
    view = views.stock({"symbol": "000001", "name": "회사", "raw": RAW}, detail())
    assert RAW not in compact_json(view)
    assert not {"daily_bars", "orderbook", "trades"}.intersection(view)
    features = view["indicators"]
    assert features["completed_days"] == 24
    assert features["return_20d_pct"] == round((124 / 104 - 1) * 100, 3)
    assert features["volume_vs_20d"] == 1
    assert features["low_20d"] == "103"
    assert features["high_20d"] == "125"
    assert view["warnings"] == [{"warningType": "UNKNOWN_NEW_RESTRICTION"}]


def test_account_keeps_every_risk_and_fresh_execution_constraint():
    source = frozen_context(12)
    view = views.account_view(source)
    assert RAW not in compact_json(view)
    for key in (
        "cash",
        "capital_used",
        "live_requested",
        "conditional_orders",
        "unresolved_submission",
        "order_generation",
        "settings_revision",
    ):
        assert view[key] == source[key]
    assert view["holdings"] == [{"symbol": "000001", "quantity": "4", "managed_quantity": "2"}]
    assert view["risk_limits"] == {k: v for k, v in source["risk_limits"].items() if k != "raw"}
    assert view["risk_status"]["daily_pnl_krw"] == "-200"
    stock = view["order_constraints"]["stocks"]["000001"]
    assert stock["sellable_quantity"] == "2"
    assert stock["price_limits"] == {"lowerLimitPrice": "70", "upperLimitPrice": "160"}
    assert stock["warnings"][0]["warningType"] == "INVESTMENT_WARNING"
    assert view["quotes"][0]["as_of"] == NOW
    assert view["quotes"][0]["price"] == "125"


def test_evidence_deduplicates_routine_history_but_preserves_restrictions():
    evidence = [
        {
            "id": str(i),
            "kind": "surveillance",
            "severity": "WARN",
            "summary": "관찰" * 500,
            "signals": [{"symbol": "000001", "condition": "spread"}],
            "raw": RAW,
        }
        for i in range(500)
    ]
    evidence += [
        {"id": "normal", "kind": "surveillance", "severity": "NORMAL", "summary": "정상"},
        {
            "id": "outside",
            "kind": "new_warning",
            "symbol": "000002",
            "warning": {"warningType": "OTHER"},
        },
        {
            "id": "mandatory",
            "kind": "new_warning",
            "symbol": "000001",
            "warning": {"warningType": "NEW_CODE", "raw": RAW},
        },
    ]
    result = views.relevant_evidence(evidence, {"000001"})
    assert {item["id"] for item in result} == {"0", "mandatory"}
    assert len(next(item["summary"] for item in result if "summary" in item)) <= 201
    assert RAW not in compact_json(result)
    assert (
        next(item for item in result if item["id"] == "mandatory")["warning"]["warningType"]
        == "NEW_CODE"
    )


def test_market_reads_only_requested_frozen_sections_and_explicit_coverage():
    source = frozen_context(2)
    result = views.market_read(
        source, "000001", ["quote", "daily_bars", "orderbook", "trades", "comparison"]
    )
    assert RAW not in compact_json(result)
    assert result["quote"]["price"] == "124" and result["quote"]["as_of"] == NOW - 100
    assert result["daily_bars"]["total"] == 24 and len(result["daily_bars"]["rows"]) == 10
    assert len(result["orderbook"]["asks"]) == len(result["orderbook"]["bids"]) == 3
    assert result["trades"]["total"] == 30 and len(result["trades"]["rows"]) == 5
    assert len(result["comparison"]) == 1
    assert result["comparison"][0]["symbol"] == "000001"
    coverage = views.market_coverage(source)
    assert coverage["total_stocks"] == 2764 and coverage["reviewed_stocks"] == 2
    assert len(compact_json(coverage).encode()) < 600
    with pytest.raises(ValueError, match="invalid_market_read"):
        views.market_read(source, "000001", ["raw"])


def test_rolling_tool_pages_do_not_resend_old_raw_results_or_grow():
    history = [
        {"tool": "evidence", "arguments": {"ids": [str(i)]}, "result": {"summary": str(i) * 2000}}
        for i in range(6)
    ]
    pages = views.working_results(history)
    assert pages == history[-2:]
    assert len(compact_json(pages).encode()) <= 12000
    assert len(history) == 6
    longer = [{"result": "x" * 7900}, {"result": "y" * 7900}]
    assert views.working_results(longer) == longer[-1:]


def test_memory_does_not_reintroduce_legacy_reports_or_unrelated_intents():
    records = [
        {
            "id": str(i),
            "at": NOW - i,
            "decision": {
                "action": "SUBMIT",
                "summary": "요약",
                "intents": [
                    {
                        "symbol": "000001",
                        "side": "BUY",
                        "quantity": 1,
                        "limit_price": "123",
                        "rationale": "근거",
                        "raw": RAW,
                    },
                    {"symbol": "000002", "rationale": RAW},
                ],
            },
            "brief": {"raw": RAW},
            "legacy_analysis": {"raw": RAW},
        }
        for i in range(12)
    ]
    result = views.memory_view(records, {"000001"})
    assert len(result) == 3
    assert RAW not in compact_json(result)
    assert all(len(item["relevant_intents"]) == 1 for item in result)


@pytest.mark.parametrize("critical_review", [False, True])
def test_maximum_batch_briefs_fit_merge_input_without_dropping_candidates_or_ids(critical_review):
    original = [
        {
            "severity": "WARN",
            "summary": "근" * 3000,
            "candidates": [
                {"symbol": f"{batch * 20 + index + 1:06d}", "reason": "근" * 1000}
                for index in range(12)
            ],
            "evidence_ids": [f"E{batch * 100 + index:04d}" for index in range(100)],
            "uncertainties": ["근" * 1000 for _ in range(100)],
        }
        for batch in range(10)
    ]
    batches = [
        views.compact_brief(value, merge=True, critical_review=critical_review)
        for value in original
    ]
    payload = {
        "protocol": "decision-v2",
        "task": "merge_decision_brief",
        "batches": batches,
        "mandatory_evidence": [],
        "allowed_candidate_symbols": [f"{i:06d}" for i in range(1, 201)],
        "schema": BRIEF_SCHEMA,
        "max_candidates": 12,
        "review_scope": "Merge public evidence into a DecisionBrief. " * 7,
        "citation_policy": (
            "Copy supplied evidence IDs exactly: E0001 or market:symbol. Never invent IDs."
        ),
    }
    if critical_review:
        payload.update(
            critical_review_policy=views.CRITICAL_REVIEW_POLICY,
            decision_gate_context={
                "holding_symbols": [f"{i:06d}" for i in range(200)],
                "previous_memory": {"as_of": 1, "content": "기" * 2000},
            },
        )
        assert all(b["summary"].startswith("근" * 300) for b in batches)
    measured = prepare_model_input("middle", payload, MODEL_PRESETS["chatgpt"]["middle"])[
        "input_context"
    ]
    enforce_input(measured)
    assert sum(len(value["candidates"]) for value in batches) == 120
    assert sum(len(value["evidence_ids"]) for value in batches) == 1000
    assert [candidate[0] for value in batches for candidate in value["candidates"]] == [
        candidate["symbol"] for value in original for candidate in value["candidates"]
    ]
    assert all(
        value["candidate_columns"]
        == ["symbol", "reason", "return_5d_pct", "volume_vs_20d", "spread_bp"]
        for value in batches
    )
    assert all(value["additional_uncertainties"] == 99 for value in batches)


def test_malformed_sellable_quantity_cannot_reintroduce_a_nested_raw_response():
    current = frozen_context(1)
    current["order_constraints"]["stocks"]["000001"]["sellable_quantity"] = {"raw": RAW}
    view = views.account_view(current)
    assert RAW not in compact_json(view)
    assert view["order_constraints"]["stocks"]["000001"].get("sellable_quantity") is None


async def test_oversized_requested_page_returns_explicit_error_and_keeps_private_source(tmp_path):
    store = DecisionStore(tmp_path / "decisions.db")
    frozen = frozen_context(1)
    frozen["details"]["000001"]["warnings"] = [
        {"warningType": "RESTRICTION" + str(i)} for i in range(500)
    ]
    pipeline = DecisionPipeline(None, None, None, store, clock=lambda: NOW)
    response = await pipeline.read_tool(
        {
            "action": "READ",
            "tool": "market",
            "arguments": {"symbol": "000001", "fields": ["warnings"]},
        },
        frozen,
        {},
        pipeline.monotonic() + 600,
    )
    assert response["result"]["status"] == "page_too_large"
    assert len(frozen["details"]["000001"]["warnings"]) == 500
    assert len(compact_json(response).encode()) <= 8000
    assert views.working_results([response]) == [response]
    store.db.close()


async def test_duplicate_market_sections_do_not_make_a_page_exceed_its_budget(tmp_path):
    store = DecisionStore(tmp_path / "db")
    pipeline = DecisionPipeline(None, None, None, store, clock=lambda: NOW)
    request = {
        "action": "READ",
        "tool": "market",
        "arguments": {"symbol": "000001", "fields": ["quote"] * 1000},
    }
    response = await pipeline.read_tool(request, frozen_context(1), {}, pipeline.monotonic() + 600)
    assert response["arguments"]["fields"] == ["quote"]
    assert response["result"]["quote"]["price"] == "124"
    assert len(compact_json(response).encode()) <= 8000
    assert len(request["arguments"]["fields"]) == 1000
    store.db.close()


@pytest.mark.parametrize("tool", ["news", "search"])
async def test_duplicate_search_arguments_are_deduplicated_without_search_count_cap(tmp_path, tool):
    store = DecisionStore(tmp_path / "db")
    captured = []

    async def news(arguments, *args):
        captured.append(arguments)
        return {
            "sources": [
                {
                    "id": "source1",
                    "kind": "web",
                    "title": "공식 자료",
                    "source": "OpenAI Web Search",
                    "url": "https://example.com/ir",
                }
            ],
            "summary": {"summary": "공식 근거", "evidence_ids": ["source1"], "uncertainties": []},
            "trace": [],
            "grounding": {},
        }

    pipeline = DecisionPipeline(None, None, news, store, clock=lambda: NOW)
    request = {
        "action": "READ",
        "tool": tool,
        "arguments": {"symbols": ["000001"] * 500, "topics": ["earnings"] * 500},
    }
    response = await pipeline.read_tool(request, frozen_context(1), {}, pipeline.monotonic() + 600)
    assert captured[0]["symbols"] == ["000001"] and captured[0]["topics"] == ["earnings"]
    assert response["arguments"] == {"symbols": ["000001"], "topics": ["earnings"]}
    assert response["result"]["summary"]["summary"] == "공식 근거"
    assert len(compact_json(response).encode()) <= 8000
    assert len(request["arguments"]["topics"]) == 500
    assert pipeline.news_calls == 1
    store.db.close()


async def test_duplicate_evidence_ids_are_not_repeated_in_tool_result(tmp_path):
    store = DecisionStore(tmp_path / "db")
    pipeline = DecisionPipeline(None, None, None, store, clock=lambda: NOW)
    request = {"action": "READ", "tool": "evidence", "arguments": {"ids": ["source1"] * 5}}
    catalogue = {"source1": {"id": "source1", "kind": "web", "summary": "공식 근거"}}
    response = await pipeline.read_tool(
        request, frozen_context(1), catalogue, pipeline.monotonic() + 600
    )
    assert response["arguments"]["ids"] == ["source1"]
    assert len(response["result"]) == 1
    assert len(request["arguments"]["ids"]) == 5
    store.db.close()


async def test_all_pipeline_inputs_with_200_details_2764_rows_500_events_stay_compact(tmp_path):
    from test_scheduled_budget import warning_events

    frozen = frozen_context()
    frozen["mandatory_evidence"] = warning_events(100)
    store = DecisionStore(tmp_path / "decisions.db")
    for i in range(500):
        store.evidence(
            "surveillance",
            {
                "severity": "WARN",
                "summary": "반복 관찰" * 500,
                "signals": [{"symbol": "000001", "condition": "spread"}],
                "raw": RAW,
            },
            NOW - i,
            str(i),
        )
    calls = []
    research_calls = 0

    async def model(role, payload):
        nonlocal research_calls
        calls.append((role, copy.deepcopy(payload)))
        prepared = prepare_model_input(role, payload, MODEL_PRESETS["chatgpt"][role])
        for preset in MODEL_PRESETS.values():
            enforce_input(
                prepare_model_input(
                    role, payload, preset[role], prompts={r: "한" * 2000 for r in preset}
                )["input_context"]
            )
        enforce_input(prepared["input_context"])
        assert RAW not in prepared["message"]
        if role == "middle":
            symbol = payload["allowed_candidate_symbols"][0]
            return {
                "severity": "WARN",
                "summary": "근거 검토",
                "candidates": [
                    {"symbol": s, "reason": "계산 지표 검토"}
                    for s in payload["allowed_candidate_symbols"][:12]
                ],
                "evidence_ids": ["market:" + symbol],
                "uncertainties": [],
            }, {"input_tokens": 1, "output_tokens": 1}
        research_calls += 1
        if research_calls <= 3:
            return {
                "action": "READ",
                "tool": "market",
                "arguments": {
                    "symbol": f"{research_calls:06d}",
                    "fields": ["daily_bars", "orderbook", "trades"],
                },
            }, {"input_tokens": 1, "output_tokens": 1}
        return {
            "action": "NO_ACTION",
            "summary": "관찰",
            "detailed_explanation": "최신 계좌와 위험 한도를 대조한 뒤 관망합니다.",
            "counterargument": "반대 근거",
            "memory_book": "테스트 메모리",
            "uncertainty": "불확실",
            "intents": [],
        }, {"input_tokens": 1, "output_tokens": 1}

    async def context(operation, request):
        return copy.deepcopy(frozen)

    async def news(*args):
        pytest.fail("No provider searches are necessary for this offline replay")

    # A full Korean memory book plus a backlog page must fit the existing budget.
    store.db.execute(
        "INSERT INTO decision_memory VALUES(1,?,?,?)",
        (
            "seed",
            NOW - 1,
            compact_json(
                {
                    "revision": 1,
                    "through": 0,
                    "source_run_id": "seed",
                    "updated_at": NOW - 1,
                    "origin": "decision_model",
                    "content": "가" * 2000,
                    "last_proposals": [],
                }
            ),
        ),
    )
    for n in range(10):
        prior = store.admit(f"prior-{n}", "critical", NOW - 1, {})
        store.finish(
            prior,
            "aborted",
            {
                "brief": {
                    "severity": "WARN",
                    "summary": "나" * 300,
                    "candidates": [{"symbol": "000001", "reason": "검토"}],
                    "evidence_ids": [],
                    "uncertainties": ["다" * 100] * 3,
                }
            },
            NOW - 1,
        )
    identity = store.admit("compact-replay", "scheduled", NOW, {})
    pipeline = DecisionPipeline(model, context, news, store, clock=lambda: NOW)
    result = await pipeline.run(identity, "scheduled", {}, {"settings_revision": 1})
    assert result is not None, store.history()[0]
    middle = [payload for role, payload in calls if role == "middle"]
    decisions = [payload for role, payload in calls if role == "research"]
    assert len(middle) == 6 and len(decisions) == 4
    first = decisions[0]
    assert "comparison_table" not in first and "candidate_details" not in first
    assert first["market_coverage"]["total_stocks"] == 2764
    assert first["tool_results"] == []
    assert len(first["candidates"]) == 12
    assert first["mandatory_evidence"]["count"] == 100
    assert len(decisions[-1]["tool_results"]) <= 2
    assert {p["arguments"]["symbol"] for p in decisions[-1]["tool_results"]} == {"000002", "000003"}
    assert first["account"]["quotes"][0]["as_of"] == NOW
    assert first["candidates"][0]["frozen_quote"]["as_of"] == NOW - 100
    assert RAW in json.dumps(result["initial_context"])
    assert len(result["tool_results"]) == 3
    store.db.close()


async def test_rejected_merge_preserves_exact_projection_and_size_without_calling_provider(
    tmp_path,
):
    store = DecisionStore(tmp_path / "merge-audit.db")
    identity = store.admit("merge-overflow", "scheduled", NOW, {})

    async def provider(*_):
        pytest.fail("Overflow must be rejected before a provider call")

    pipeline = DecisionPipeline(provider, None, None, store, clock=lambda: NOW)
    pipeline.identity = identity
    pipeline.data = {"configuration": {"models": MODEL_PRESETS["chatgpt"]}, "trace": pipeline.trace}
    payload = {
        "batches": [],
        "allowed_candidate_symbols": ["000001"],
        "mandatory_evidence": [{"id": "large", "summary": "필수 근거" * 20000}],
    }
    original = copy.deepcopy(payload)
    with pytest.raises(ValueError, match="input_budget_exceeded"):
        await pipeline.call("middle", "merge_decision_brief", payload, pipeline.monotonic() + 600)
    trace = pipeline.trace[-1]
    assert trace["status"] == "blocked_input" and trace["provider_called"] is False
    assert trace["input_context"]["total_bytes"] > trace["input_context"]["budget_bytes"]
    assert trace["input_context"]["fields"]["mandatory_evidence"]["bytes"] > 32000
    archived = store.history(1)[0]["data"]
    rejected = archived["model_inputs"][-1]["payload"]
    assert rejected["mandatory_evidence"] == payload["mandatory_evidence"]
    actual = prepare_model_input(
        "middle", pipeline.data["model_inputs"][-1]["payload"], MODEL_PRESETS["chatgpt"]["middle"]
    )
    assert actual["input_context"]["sha256"] == trace["input_context"]["sha256"]
    assert payload == original
    store.db.close()


def test_variable_numeric_cells_keep_the_metric_name_next_to_the_value():
    table = views.stock_table(
        [
            {"symbol": "005930", "indicators": {"spread_bp": 120, "volume_vs_20d": 1.5}},
            {"symbol": "000660", "indicators": {"spread_bp": 30, "volume_vs_20d": 2.0}},
        ]
    )
    cells = table["rows"][0]
    assert {"indicators.spread_bp": 120} in cells
    assert {"indicators.volume_vs_20d": 1.5} in cells
