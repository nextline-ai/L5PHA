import json
from unittest.mock import Mock

import pytest

from veyquant.input_budget import INPUT_BUDGETS, compact_json, enforce_input, measure_input
from veyquant.native_search import native_search
from veyquant.shadow_inference import (
    MODEL_PRESETS,
    call,
    model_system,
    openai_output_format,
    prepare_model_input,
)


@pytest.mark.parametrize("role", INPUT_BUDGETS)
def test_budget_blocks_before_provider_without_truncating_or_losing_audit(role):
    client = Mock()
    payload = {"protocol": "decision-v2", "raw": "가" * INPUT_BUDGETS[role]}
    trace = []
    with pytest.raises(ValueError, match="input_budget_exceeded"):
        call(client, role, payload, trace)
    client.converse.assert_not_called()
    assert payload["raw"] == "가" * INPUT_BUDGETS[role]
    assert trace[0]["status"] == "blocked_input"
    assert trace[0]["provider_called"] is False
    assert trace[0]["input_context"]["fields"]["raw"]["bytes"] > INPUT_BUDGETS[role]
    assert "가" not in json.dumps(trace, ensure_ascii=False)


def test_budget_includes_unicode_system_and_provider_schema_at_exact_boundary():
    payload = {"value": "가"}
    base = measure_input("cheap", payload, "system", {"schema": "text"})
    remaining = INPUT_BUDGETS["cheap"] - base["total_bytes"]
    accepted = measure_input("cheap", payload, "system" + "x" * remaining, {"schema": "text"})
    assert accepted["total_bytes"] == INPUT_BUDGETS["cheap"]
    enforce_input(accepted)
    rejected = measure_input("cheap", payload, "system" + "x" * (remaining + 1), {"schema": "text"})
    with pytest.raises(ValueError, match="input_budget_exceeded"):
        enforce_input(rejected)
    assert base["payload_bytes"] == len(compact_json(payload).encode("utf-8"))
    assert base["fields"]["value"]["bytes"] == 5
    assert base["estimate_kind"].endswith("not_provider_billing")


def test_strategy_is_relevant_only_to_decision_without_changing_its_owner_prompt():
    strategy = {"preset": "custom", "prompt": "개별 전략은 최종 판단에 적용합니다."}
    payload = {"protocol": "decision-v2"}
    assert strategy["prompt"] not in model_system("cheap", payload, strategy)
    assert strategy["prompt"] not in model_system("middle", payload, strategy)
    assert strategy["prompt"] in model_system("research", payload, strategy)


def test_prepared_input_matches_actual_openai_text_and_schema(monkeypatch):
    requests = []
    answer = {"severity": "NORMAL", "summary": "확인했습니다."}

    def request(provider, key, path, body, **kwargs):
        requests.append(body)
        return {
            "status": "completed",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "input_tokens_details": {"cached_tokens": 30, "cache_write_tokens": 40},
            },
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": json.dumps({"result": answer})}],
                }
            ],
        }

    monkeypatch.setattr("veyquant.shadow_inference.provider_request", request)
    payload = {"protocol": "decision-v2", "task": "surveillance", "events": []}
    models = MODEL_PRESETS["chatgpt"]
    prepared = prepare_model_input("cheap", payload, models["cheap"])
    trace = []
    assert call(None, "cheap", payload, trace, models, keys={"openai": "fixture"}) == answer
    body = requests[0]
    developer, user = body["input"]
    assert developer["content"][0]["text"] == prepared["system"]
    assert developer["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert user == {
        "role": "user",
        "content": [{"type": "input_text", "text": prepared["message"]}],
    }
    assert body["text"]["format"] == prepared["output_format"]
    measured = trace[0]["input_context"]
    actual = sum(
        len(item.encode("utf-8"))
        for item in (
            developer["content"][0]["text"],
            user["content"][0]["text"],
            compact_json(body["text"]["format"]),
        )
    )
    assert measured["total_bytes"] == actual
    assert measured == prepared["input_context"]
    assert trace[0]["provider_called"] is True
    assert trace[0]["cached_input_tokens"] == 30
    assert trace[0]["cache_write_input_tokens"] == 40
    assert trace[0]["cache_policy"] == "static_prefix"


def test_model_read_schema_allows_small_market_sections_and_five_evidence_ids():
    schema = openai_output_format(
        "research", {"protocol": "decision-v2", "task": "investment_decision"}
    )
    alternatives = schema["schema"]["properties"]["result"]["anyOf"]
    reads = {item["properties"]["tool"]["enum"][0]: item for item in alternatives[1:]}
    evidence_ids = reads["evidence"]["properties"]["arguments"]["properties"]["ids"]
    assert evidence_ids["maxItems"] == 5
    fields = reads["market"]["properties"]["arguments"]["properties"]["fields"]["items"]["enum"]
    assert {"indicators", "warnings", "comparison"}.issubset(fields)
    for name in ("news", "search"):
        args = reads[name]["properties"]["arguments"]["properties"]
        assert "maxItems" not in args["symbols"] and "maxItems" not in args["topics"]


def test_native_public_search_blocks_oversized_question_before_provider():
    request = Mock()
    trace = []
    queries = [
        {"symbol": f"{index:06d}", "name": "회사", "topic": "earnings"} for index in range(400)
    ]
    with pytest.raises(ValueError, match="input_budget_exceeded"):
        native_search("openai", "gpt-5.6-sol", "high", "fixture", queries, 0, request, trace, 8192)
    request.assert_not_called()
    assert trace[0]["provider_called"] is False
    assert trace[0]["input_context"]["budget_bytes"] == 8000


def test_native_search_low_context_keeps_search_count_unlimited_and_usage_separate():
    requests = []

    def request(provider, key, path, body):
        requests.append(body)
        return {
            "status": "completed",
            "usage": {"input_tokens": 90000, "output_tokens": 50},
            "output": [
                {
                    "type": "web_search_call",
                    "status": "completed",
                    "action": {"type": "search", "queries": ["q"] * 20},
                },
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "공식 자료를 확인했습니다.",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://example.com/ir",
                                    "title": "IR",
                                }
                            ],
                        }
                    ],
                },
            ],
        }

    trace = []
    native_search(
        "openai",
        "gpt-5.6-sol",
        "high",
        "fixture",
        [{"symbol": "005930", "name": "삼성전자", "topic": "earnings"}],
        0,
        request,
        trace,
        8192,
        "research",
    )
    assert requests[0]["tools"] == [{"type": "web_search", "search_context_size": "low"}]
    assert "max_tool_calls" not in requests[0]
    assert trace[0]["search_queries"] == 20
    assert trace[0]["input_tokens"] == 90000
    assert trace[0]["input_context"]["total_bytes"] < 8000


def test_middle_budget_has_room_for_required_disclosures_but_keeps_a_hard_boundary():
    assert INPUT_BUDGETS == {"cheap": 12000, "middle": 64000, "research": 96000}
    for size in (33653, 37678, 64000):
        overhead = measure_input("middle", {"summary": ""})["total_bytes"]
        measured = measure_input("middle", {"summary": "x" * (size - overhead)})
        assert measured["total_bytes"] == size
        enforce_input(measured)
    with pytest.raises(ValueError, match="input_budget_exceeded"):
        enforce_input(measure_input("middle", {"summary": "x" * (64001 - overhead)}))


def test_input_budget_is_server_policy_never_a_model_output_instruction():
    from veyquant.model_prompts import harness

    for role in INPUT_BUDGETS:
        text = harness(role)
        assert "bytes" not in text
        assert "입력 상한" not in text
        assert "600초" not in text
        assert "JSON" in text
    assert "2000자" in harness("research")
    overhead = measure_input("research", {})["total_bytes"]
    enforce_input(measure_input("research", {"x": "a" * 95000}))
    with pytest.raises(ValueError, match="input_budget_exceeded"):
        enforce_input(measure_input("research", {"x": "a" * (96001 - overhead)}))
