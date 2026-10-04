import json

import pytest
from test_optional_research import native_response

from veyquant.decision_store import DecisionStore
from veyquant.input_budget import compact_json
from veyquant.native_search import native_search, public_research_view
from veyquant.shadow_inference import openai_converse


@pytest.mark.parametrize(
    "name,static",
    [
        ("veyquant_cheap", True),
        ("veyquant_research", True),
        ("veyquant_middle", False),
        (None, False),
    ],
)
def test_openai_cache_excludes_dynamic_payload_and_middle_enum_schema(monkeypatch, name, static):
    bodies = []

    def request(*args, **kwargs):
        bodies.append(args[3])
        value = {"result": {"summary": "ok"}} if name else {"summary": "ok"}
        return {
            "status": "completed",
            "usage": {},
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": json.dumps(value)}],
                }
            ],
        }

    monkeypatch.setattr("veyquant.shadow_inference.provider_request", request)
    for market in ("quote one", "quote two"):
        result = openai_converse(
            "gpt-5.6-sol",
            "fixed harness",
            market,
            100,
            "fixture",
            output_format={"name": name} if name else None,
        )
        assert "cacheWriteInputTokens" not in result["usage"]
    first, second = bodies
    assert first["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    if static:
        assert first["input"][0] == second["input"][0]
        assert "prompt_cache_breakpoint" in first["input"][0]["content"][0]
        assert "prompt_cache_breakpoint" not in first["input"][1]["content"][0]
    else:
        assert "prompt_cache_breakpoint" not in compact_json(first)
        assert first["input"] == "quote one" and second["input"] == "quote two"


def test_prior_research_is_recent_small_same_company_public_projection():
    queries = [{"symbol": "005930", "name": "삼성전자", "topic": "earnings"}]
    row = {
        "symbols": ["005930"],
        "collected_at": 100,
        "summary": "공개 실적" * 200,
        "urls": ["https://example.com/ir", "http://localhost/secret"],
        "cash": "PRIVATE",
        "strategy": "PRIVATE",
    }
    records = [
        row,
        row | {"symbols": ["005930", "000001"]},
        row | {"collected_at": -2000},
        row | {"collected_at": 101},
        row | {"symbols": [[]]},
        row | {"urls": None},
    ]
    projected = public_research_view(records, queries, 100)
    assert len(compact_json(projected).encode()) <= 2400
    assert all(r["symbols"] == ["005930"] for r in projected)
    assert "PRIVATE" not in compact_json(projected) and "localhost" not in compact_json(projected)
    assert public_research_view([row], queries, 1901) == []
    assert public_research_view(None, queries, 100) == []


@pytest.mark.parametrize("role", ["middle", "research"])
def test_search_action_accounting_and_middle_only_limit(role):
    bodies, trace = [], []

    def request(*args):
        bodies.append(args[3])
        answer = native_response("openai")
        answer["usage"]["input_tokens_details"] = {"cached_tokens": 10, "cache_write_tokens": 0}
        answer["output"][1:1] = [
            {"type": "web_search_call", "status": "completed", "action": {"type": action}}
            for action in ("open_page", "find_in_page")
        ]
        return answer

    native_search(
        "openai",
        "gpt-5.6-sol",
        "high",
        "fixture",
        [{"symbol": "005930", "name": "삼성전자", "topic": "earnings"}],
        100,
        request,
        trace,
        8192,
        role,
        prior_research=[
            {
                "symbols": ["005930"],
                "collected_at": 99,
                "summary": "공개 실적",
                "urls": ["https://example.com/ir"],
            }
        ],
    )
    assert trace[0]["search_tool_calls"] == 3
    assert trace[0]["web_search_actions"] == 1
    assert trace[0]["web_open_actions"] == trace[0]["web_find_actions"] == 1
    assert trace[0]["cache_write_input_tokens"] == 0
    body = bodies[0]
    assert body.get("max_tool_calls") == (6 if role == "middle" else None)
    assert (
        body["tool_choice"] == "required"
    )  # Prior evidence never silently replaces fresh research.
    assert body["prompt_cache_options"]["mode"] == "explicit"
    assert "prompt_cache_breakpoint" not in compact_json(body)
    assert json.loads(body["input"])["prior_public_research"][0]["collected_at"] == 99


def test_usage_keeps_legacy_search_and_cache_unknown_without_double_counting(tmp_path):
    store = DecisionStore(tmp_path / "db")
    for extra in (
        {},
        {
            "cache_write_input_tokens": 40,
            "cached_input_tokens": 10,
            "web_search_actions": 2,
            "web_open_actions": 3,
        },
    ):
        identity = store.usage_start(100, None, "native_search", "middle", "fixture")
        store.usage_finish(
            identity, {"input_tokens": 100, "output_tokens": 10, "search_tool_calls": 5, **extra}
        )
    group = store.usage_summary(100)["groups"][0]
    assert group["input_tokens"] == 200  # Includes cache reads/writes; never add them twice.
    assert group["cache_write_input_tokens"] == 40 and group["cache_write_usage_unknown"] == 1
    assert group["search_tool_calls"] == 10 and group["web_search_actions"] == 2
    assert group["search_action_usage_unknown"] == 1
    store.db.close()
