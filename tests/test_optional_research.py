import copy
import json
from datetime import datetime

import pytest
from test_decision_pipeline import CALENDAR, NOW, context, final, make_brief

from veyquant import research_sources as sources
from veyquant.collector import ObservationStore
from veyquant.decision_pipeline import DecisionPipeline
from veyquant.decision_runtime import Runtime
from veyquant.decision_store import DecisionStore
from veyquant.native_search import native_search
from veyquant.surveillance import observe


def native_response(provider, queries=15):
    if provider == "openai":
        return {
            "status": "completed",
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "output": [
                {
                    "type": "web_search_call",
                    "status": "completed",
                    "action": {"type": "search", "queries": ["public"] * queries},
                },
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "공식 실적 [1]",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://issuer.example/ir",
                                    "title": "IR",
                                }
                            ],
                        }
                    ],
                },
            ],
        }
    return {
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20},
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "parts": [{"text": "공식 실적"}, {"thought": True, "text": "private reasoning"}]
                },
                "groundingMetadata": {
                    "webSearchQueries": ["public"] * queries,
                    "groundingChunks": [
                        {"web": {"uri": "https://issuer.example/ir", "title": "IR"}}
                    ],
                    "searchEntryPoint": {"renderedContent": "<div>Google</div>"},
                },
            }
        ],
    }


@pytest.mark.parametrize(
    "provider,model", [("openai", "gpt-5.6-sol"), ("gemini", "gemini-3.8-flash")]
)
@pytest.mark.parametrize("role", ["middle", "research"])
def test_native_search_is_public_only_and_has_no_search_count_cap(provider, model, role):
    requests = []

    def request(*args):
        requests.append(args)
        return native_response(provider)

    trace = []
    result = native_search(
        provider,
        model,
        "high",
        "fixture_key",
        [{"symbol": "005930", "name": "삼성전자", "topic": "earnings"}] * 13,
        NOW,
        request,
        trace,
        8192,
        role,
    )
    payload = requests[0][3]
    if provider == "openai" and role == "middle":
        assert payload["max_tool_calls"] == 6
    else:
        assert "max_tool_calls" not in payload
    assert "responseMimeType" not in payload.get("generationConfig", {})
    assert payload["store"] is False
    assert (
        ("web_search" in json.dumps(payload))
        if provider == "openai"
        else ("googleSearch" in json.dumps(payload))
    )
    assert "fixture_key" not in json.dumps(result)
    assert "private reasoning" not in json.dumps(result)
    assert result["sources"][0]["status"] == "provider_grounded_citation"
    assert trace[0]["role"] == role and trace[0]["search_queries"] == 15
    assert trace[0]["input_tokens"] == 100
    assert result["grounding"]["text"]


def test_arbitrary_queries_fail_before_provider_call():
    with pytest.raises(ValueError):
        native_search(
            "openai",
            "gpt-5.6-sol",
            "low",
            "fixture",
            [{"symbol": "005930", "name": "삼성전자", "topic": "cash account 123"}],
            NOW,
            lambda *a: pytest.fail("must not call"),
            [],
            2048,
        )


def test_ungrounded_native_response_keeps_usage_and_is_not_valid_evidence():
    trace = []
    data = native_response("gemini")
    data["candidates"][0]["groundingMetadata"] = {}
    with pytest.raises(ValueError, match="search_not_grounded"):
        native_search(
            "gemini",
            "gemini-3.8-flash",
            "high",
            "fixture",
            [{"symbol": "005930", "name": "삼성전자", "topic": "earnings"}],
            NOW,
            lambda *a, value=data: value,
            trace,
            8192,
        )
    assert trace[0]["input_tokens"] == 100


@pytest.mark.parametrize("tool,role", [("news", "middle"), ("search", "research")])
async def test_native_summary_is_not_sent_to_another_summarizer(tmp_path, tool, role):
    store = DecisionStore(tmp_path / "db")
    requests = []

    async def search(args, *a):
        requests.append(args)
        return {
            "sources": [{"id": "source", "source": "Google Search"}],
            "summary": {"summary": "공식 근거", "evidence_ids": ["source"], "uncertainties": []},
            "grounding": {"text": "공식 근거"},
            "trace": [{"role": role, "input_tokens": 10}],
        }

    async def model(*args):
        pytest.fail("native search already summarized")

    elapsed = [0]
    pipeline = DecisionPipeline(model, None, search, store, monotonic=lambda: elapsed[0])
    pipeline.data = {}
    request = {
        "action": "READ",
        "tool": tool,
        "arguments": {"symbols": ["000001"], "topics": ["earnings"]},
    }
    for _ in range(7):
        elapsed[0] += 61
        await pipeline.read_tool(request, context(), {}, pipeline.monotonic() + 10)
    expected = 2 if tool == "news" else 7
    assert pipeline.news_calls == expected and pipeline.tools == 0
    assert requests[0].get("_role", "middle") == role
    assert len(pipeline.data["grounded_news"]) == expected
    assert not store.recent(NOW)  # Google evidence remains in owner run history, not reusable feed.
    store.db.close()


def krx_row(market, day="20260909"):
    return {
        "BAS_DD": day,
        "ISU_CD": "005930" if market == "KOSPI" else "000660",
        "MKT_NM": market,
        "TDD_CLSPRC": "70,000",
        "ACC_TRDVOL": "1000",
        "ACC_TRDVAL": "-",
        "MKTCAP": "1,000,000",
        "LIST_SHRS": "100",
    }


def test_krx_requires_both_services_and_uses_header_auth(monkeypatch):
    calls = []

    def get(host, path, headers):
        calls.append((host, path, headers))
        return {"OutBlock_1": [krx_row("KOSDAQ" if "ksq_" in path else "KOSPI")]}

    monkeypatch.setattr(sources, "get_json", get)
    now = datetime.fromisoformat("2026-09-10T08:30:00+09:00").timestamp()
    result = sources.krx_daily("fixture", now)
    assert len(calls) == 2 and result["as_of"] == "20260909"
    assert calls[0][0] == "data-dbg.krx.co.kr"
    assert all(c[2] == {"AUTH_KEY": "fixture"} and "fixture" not in c[1] for c in calls)
    assert result["rows"][0]["close"] == "70000" and result["rows"][0]["turnover_krw"] is None


def test_krx_bad_auth_or_stale_day_cannot_validate_a_key(monkeypatch):
    now = datetime.fromisoformat("2026-09-10T08:30:00+09:00").timestamp()
    for data in [{"error": "unauthorized"}, {"OutBlock_1": [krx_row("KOSPI", "20200101")]}]:
        monkeypatch.setattr(sources, "get_json", lambda *a, value=data: value)
        with pytest.raises(ValueError):
            sources.verify_service("krx", "fixture", now)


def test_existing_warning_bootstrap_is_silent_but_recurrence_survives_restart(tmp_path):
    path = tmp_path / "observations"
    observer = ObservationStore(str(path), str(tmp_path / "status"))
    store = DecisionStore(tmp_path / "analysis")
    calendar = copy.deepcopy(CALENDAR)
    calendar["today"]["integrated"] = None
    warning = {"warningType": "INVESTMENT_WARNING", "startDate": None}
    observer.update_warnings("005930", [warning], NOW)
    observe(store, {}, calendar, NOW, observer.warning_events.values())
    assert not store.take_signals(NOW)
    observer.update_warnings("005930", [], NOW + 1)
    observe(store, {}, calendar, NOW + 1, observer.warning_events.values())
    observer.db.close()
    observer = ObservationStore(str(path), str(tmp_path / "status"))
    observer.update_warnings("005930", [warning], NOW + 2)
    observe(store, {}, calendar, NOW + 2, observer.warning_events.values())
    assert len(store.take_signals(NOW + 2)) == 1
    observe(store, {}, calendar, NOW + 3, observer.warning_events.values())
    assert not store.take_signals(NOW + 3)
    observer.db.close()
    store.db.close()


async def test_surveillance_failure_circuit_and_usage_accounting(tmp_path):
    runtime = Runtime.__new__(Runtime)
    runtime.store = DecisionStore(tmp_path / "db")
    runtime.clock = lambda: NOW

    async def failed(*a, **k):
        error = ValueError("openai_http_429_rate_limit_exceeded")
        error.usage = {"input_tokens": 5, "output_tokens": 1}
        raise error

    runtime.capability = failed
    await runtime.surveillance([{"condition": "heartbeat"}], {})
    assert runtime.store.get("surveillance_retry_at") == NOW + 300
    assert runtime.store.recent(NOW)[0]["trace"][0]["input_tokens"] == 5
    call = runtime.store.usage_start(NOW, None, "stage", "cheap", "fixture")
    runtime.store.usage_finish(call, {"input_tokens": 5, "output_tokens": 1}, "invalid_severity")
    runtime.store.usage_start(NOW, None, "stage", "cheap", "fixture")  # transport result unknown
    group = runtime.store.usage_summary(NOW)["groups"][0]
    assert group["attempts"] == 2 and group["unknown_usage"] == 1 and group["input_tokens"] == 5
    assert group["failed"] == 1  # Unknown/in-flight transport is not a confirmed failure.
    runtime.store.db.close()


async def test_optional_sources_missing_do_not_block_pipeline_and_bars_are_not_duplicated(tmp_path):
    store = DecisionStore(tmp_path / "db")
    seen = []

    async def model(role, payload):
        seen.append(payload)
        return (make_brief() if role == "middle" else final()), {}

    async def snapshot(*a):
        c = context()
        c["optional_sources"] = {"dart": False, "krx": False}
        c["review_universe"][0]["daily_bars"] = c["details"]["000001"]["daily_bars"]
        return c

    store.evidence("dart_important", {"kind": "dart_important", "symbol": "000001"}, NOW, "filing")
    identity = store.admit("fixture", "manual", NOW, {})
    p = DecisionPipeline(model, snapshot, None, store, clock=lambda: NOW)
    assert await p.run(identity, "manual", {}, {"settings_revision": 1})
    review = next(v for v in seen if v["task"] == "review_batch")
    assert "daily_bars" not in json.dumps(review["stocks"])
    assert review["stocks"]["shared"]["indicators.completed_days"] == 1
    assert all("daily_bars" not in e for e in review["recent_evidence"])
    assert all(e["id"] != "filing" for e in review["recent_evidence"])
    store.db.close()


async def test_krx_enriches_snapshot_without_repeating_the_date(monkeypatch, tmp_path):
    runtime = Runtime.__new__(Runtime)
    runtime.clock = lambda: NOW
    runtime.configuration = lambda: {"provider_credentials": {"krx": {}}}
    runtime.krx_cache = {
        "collected_at": NOW,
        "as_of": "20260908",
        "rows": [{"symbol": "000001", "market_cap_krw": "1000000"}],
    }

    async def exchange(*args):
        return context()

    monkeypatch.setattr("veyquant.decision_runtime.exchange", exchange)
    result = await runtime.context("initial", {})
    assert result["review_universe"][0]["krx_daily"]["as_of"] == "20260908"
    assert result["comparison_table"]["krx_as_of"] == "20260908"
    assert result["comparison_table"]["rows"][0] == ["000001", "1000000"]
    runtime.configuration = lambda: {"provider_credentials": {}}
    result = await runtime.context("initial", {})
    assert "krx_daily" not in result["review_universe"][0]
    assert result["comparison_table"]["rows"][0] == ["000001"]


def test_full_market_usage_can_exceed_legacy_100k_threshold():
    from types import SimpleNamespace

    from veyquant.shadow_inference import call

    client = SimpleNamespace(
        converse=lambda **kw: {
            "usage": {"inputTokens": 120000, "outputTokens": 20},
            "stopReason": "end_turn",
            "output": {
                "message": {"content": [{"text": '{"severity":"NORMAL","summary":"확인"}'}]}
            },
        }
    )
    trace = []
    assert call(client, "research", {"protocol": "decision-v2"}, trace)["severity"] == "NORMAL"
    assert trace[0]["input_tokens"] == 120000
