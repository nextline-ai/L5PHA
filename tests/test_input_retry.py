import copy
import json
from unittest.mock import AsyncMock, Mock

import pytest
from test_decision_pipeline import NOW, context, final, make_brief

from veyquant.decision_runtime import Runtime
from veyquant.decision_store import DecisionStore
from veyquant.input_budget import INPUT_BUDGETS, enforce_input, measure_input
from veyquant.shadow_inference import MODEL_PRESETS, call


def test_explicit_override_is_measured_and_does_not_change_global_or_next_call(monkeypatch):
    answer = {"severity": "NORMAL", "summary": "done"}
    provider = Mock(
        return_value={
            "output": {"message": {"content": [{"text": json.dumps(answer)}]}},
            "usage": {"inputTokens": 1, "outputTokens": 1},
            "stopReason": "end_turn",
        }
    )
    client = Mock(converse=provider)
    payload = {"protocol": "decision-v2", "summary": "가" * 30000}
    trace = []
    before = copy.deepcopy(INPUT_BUDGETS)
    call(client, "middle", payload, trace, input_limit_override=True)
    assert trace[0]["input_context"]["limit_overridden"] is True
    assert trace[0]["provider_called"] is True
    assert trace[0]["input_context"]["total_bytes"] > 64000
    with pytest.raises(ValueError, match="input_budget_exceeded"):
        call(client, "middle", payload, [])
    assert provider.call_count == 1 and INPUT_BUDGETS == before
    with pytest.raises(ValueError, match="invalid_input_limit_override"):
        enforce_input(measure_input("middle", {}), override="true")


async def test_retry_preserves_instruction_deduplicates_and_scopes_override(tmp_path):
    store = DecisionStore(tmp_path / "retry.db")
    runtime = Runtime.__new__(Runtime)
    runtime.store, runtime.clock = store, lambda: NOW
    config = {"settings_revision": 1, "models": MODEL_PRESETS["chatgpt"]}
    runtime.configuration = lambda: config
    runtime.orders_blocked, runtime.status_at = False, NOW
    runtime.search_ready = lambda *_: False
    runtime.context = AsyncMock(side_effect=lambda *_: context())
    runtime.publish = Mock()
    calls = []

    async def capability(config, operation, **fields):
        calls.append(fields)
        role = fields["role"]
        return {
            "result": make_brief() if role == "middle" else final(),
            "trace": [{"status": "received", "model": "fixture"}],
        }

    runtime.capability = capability
    source = store.admit("original", "manual", NOW, {})
    store.finish(
        source,
        "aborted",
        {"error": "input_budget_exceeded", "request": {"instruction": "원래 투자 제안"}},
        NOW,
    )
    request = {"operation": "retry_input_limit", "id": source, "request_id": "b" * 32}
    response = await runtime.owner_request(request)
    duplicate = await runtime.owner_request(request | {"request_id": "c" * 32})
    assert duplicate["duplicate"] and duplicate["id"] == response["id"]
    await runtime.task
    assert len(calls) == 2 and all(c["input_limit_override"] for c in calls)
    retry = next(r for r in store.history() if r["id"] == response["id"])
    assert retry["state"] == "complete", retry["data"].get("error")
    assert retry["data"]["request"]["instruction"] == "원래 투자 제안"
    assert retry["data"]["request"]["retry_of"] == source
    runtime.status_at = NOW
    await runtime.launch("normal", "manual", {"instruction": "일반 판단"})
    await runtime.task
    assert len(calls) == 4 and all(not c["input_limit_override"] for c in calls[2:])
    assert "input_limit_override" not in config
    store.db.close()


@pytest.mark.parametrize(
    "error,state,kind",
    [
        ("provider_timeout", "aborted", "scheduled"),
        ("input_budget_exceeded", "complete", "scheduled"),
        ("input_budget_exceeded", "aborted", "verification"),
    ],
)
async def test_retry_cannot_override_other_failures_or_verification(tmp_path, error, state, kind):
    store = DecisionStore(tmp_path / "source.db")
    identity = store.admit("source", kind, NOW, {})
    store.finish(identity, state, {"error": error}, NOW)
    runtime = Runtime.__new__(Runtime)
    runtime.store = store
    runtime.launch = AsyncMock()
    with pytest.raises(ValueError, match="analysis_not_retryable"):
        await runtime.owner_request(
            {"operation": "retry_input_limit", "id": identity, "request_id": "a" * 32}
        )
    runtime.launch.assert_not_called()
    store.db.close()


@pytest.mark.parametrize("busy,orders", [(True, False), (False, True)])
async def test_forced_retry_still_respects_pipeline_and_order_admission(tmp_path, busy, orders):
    store = DecisionStore(tmp_path / "blocked.db")
    identity = store.admit("source", "scheduled", NOW, {})
    store.finish(identity, "aborted", {"error": "input_budget_exceeded"}, NOW)
    if busy:
        store.admit("active", "manual", NOW, {})
    runtime = Runtime.__new__(Runtime)
    runtime.store, runtime.clock = store, lambda: NOW
    runtime.configuration = lambda: {"settings_revision": 1}
    runtime.orders_blocked, runtime.status_at = orders, NOW
    runtime.capability = AsyncMock()
    result = await runtime.owner_request(
        {"operation": "retry_input_limit", "id": identity, "request_id": "c" * 32}
    )
    assert result["accepted"] is False
    runtime.capability.assert_not_called()
    store.db.close()


def test_native_search_override_keeps_public_query_validation_and_measurement():
    from veyquant.native_search import native_search

    queries = [{"symbol": f"{i:06d}", "name": "회사", "topic": "earnings"} for i in range(400)]
    request = Mock(side_effect=RuntimeError("fixture_transport"))
    trace = []
    with pytest.raises(RuntimeError, match="fixture_transport"):
        native_search(
            "openai",
            "gpt-5.6-sol",
            "medium",
            "fixture",
            queries,
            NOW,
            request,
            trace,
            0,
            input_limit_override=True,
        )
    assert request.call_count == 1
    assert trace[0]["input_context"]["limit_overridden"] is True
    assert trace[0]["input_context"]["total_bytes"] > 8000
    with pytest.raises(ValueError, match="invalid_search"):
        native_search(
            "openai",
            "gpt-5.6-sol",
            "medium",
            "fixture",
            [{"private": "account"}],
            NOW,
            request,
            [],
            0,
            input_limit_override=True,
        )
    assert request.call_count == 1
