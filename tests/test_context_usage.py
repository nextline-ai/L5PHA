import io
import json

import pytest

from veyquant.decision_runtime import Runtime, surveillance_inputs, surveillance_summary
from veyquant.decision_store import DecisionStore
from veyquant.input_budget import measure_input


def test_surveillance_never_passes_raw_signal_extensions_and_groups_worst_examples():
    signals = [
        {
            "condition": "price_3pct",
            "symbol": f"{i:06d}",
            "at": 100,
            "account": {"secret": "private_account_fixture"},
            "daily_bars": [{"raw": "never_in_model"}] * 24,
            "metrics": {"change_5m": i / 1000, "price_as_of": 100, "raw": "never_in_model"},
        }
        for i in range(200)
    ]
    signals.extend(
        [
            {
                "kind": "dart_important",
                "symbol": "000001",
                "title": "주요사항보고서",
                "raw_filing": "never_in_model",
                "filing_text": "never_in_model" * 10000,
            },
            {
                "kind": "new_warning",
                "symbol": "000002",
                "warning": {"warningType": "INVESTMENT_WARNING", "raw": "never_in_model"},
            },
        ]
    )
    projected = surveillance_inputs(signals)
    summary = surveillance_summary(signals)
    assert "never_in_model" not in json.dumps(projected)
    assert "private_account_fixture" not in json.dumps(summary)
    assert summary["signal_count"] == 202
    price = next(g for g in summary["conditions"] if g["condition"] == "price_3pct")
    assert price["count"] == 200 and price["omitted_count"] == 197
    assert [s["symbol"] for s in price["examples"]] == ["000199", "000198", "000197"]
    assert "never_in_model" in json.dumps(signals)  # Private originals remain complete.


def test_market_wide_surveillance_input_remains_bounded_without_extra_model_calls():
    signals = [
        {
            "condition": condition,
            "symbol": f"{i:06d}",
            "at": 1789010400,
            "metrics": {
                "change_5m": 0.1,
                "price_as_of": 1789010400,
                "price_source": "completed_minutes",
                "volume_change_5m": 0.02,
                "volume_zscore": 5,
                "volume_as_of": 1789010400,
                "volume_baseline": "20 preceding complete 5-minute windows",
                "spread_bp": 80,
                "book_as_of": 1789010400,
            },
        }
        for condition in ("price_3pct", "price_volume", "spread_50bp")
        for i in range(200)
    ]
    signals.extend(
        {"kind": "dart_important", "title": "주요사항보고서" * 50, "symbol": f"{i:06d}"}
        for i in range(200)
    )
    payload = {
        "protocol": "decision-v2",
        "task": "surveillance",
        "signals": surveillance_summary(signals),
    }
    measured = measure_input(
        "cheap", payload, system="x" * 1000, schema={"description": "x" * 1000}
    )
    assert measured["total_bytes"] < measured["budget_bytes"]
    assert payload["signals"]["signal_count"] == 800


def test_usage_preserves_exact_private_payload_but_only_publishes_measurements(tmp_path):
    store = DecisionStore(tmp_path / "db")
    payload = {"account": {"cash": "private_cash_fixture"}, "brief": "필요한 요약"}
    submitted = measure_input("research", payload)
    identity = store.usage_start(
        100, "run", "stage", "research", "fixture", input_context=submitted, model_payload=payload
    )
    full = measure_input("research", payload, system="system", schema={"strict": True})
    store.usage_finish(
        identity,
        {
            "input_context": full,
            "input_tokens": 20,
            "output_tokens": 3,
            "provider_called": True,
            "unexpected_private": "should_not_be_saved",
        },
    )
    archived = json.loads(store.db.execute("SELECT data FROM ai_usage").fetchone()[0])
    assert archived["model_payload"] == payload and archived["input_context"] == full
    assert "unexpected_private" not in archived
    summary = store.usage_summary(100)
    assert "private_cash_fixture" not in json.dumps(summary)
    group = summary["groups"][0]
    assert group["visible_input_bytes"] == full["total_bytes"]
    assert group["input_fields_bytes"]["account"] == full["fields"]["account"]["bytes"]
    assert group["input_tokens"] == 20 and group["input_context_unknown"] == 0
    store.db.close()


def test_usage_distinguishes_budget_rejection_from_unknown_transport_billing(tmp_path):
    store = DecisionStore(tmp_path / "db")
    payload = {"brief": "summary"}
    context = measure_input("research", payload)
    context["scope"] = "submitted_payload_excludes_system_schema_and_provider_internal_context"
    blocked = store.usage_start(100, None, "stage", "research", "fixture", input_context=context)
    store.usage_finish(
        blocked,
        {
            "status": "blocked_input",
            "provider_called": False,
            "input_tokens": 0,
            "output_tokens": 0,
        },
        "input_budget_exceeded",
    )
    store.usage_start(101, None, "stage", "research", "fixture", input_context=context)
    group = store.usage_summary(100)["groups"][0]
    assert group["requests"] == 2 and group["attempts"] == 1
    assert group["blocked_before_provider"] == 1 and group["unknown_usage"] == 1
    assert group["failed"] == 0 and group["input_context_partial"] == 2
    store.db.close()


@pytest.mark.parametrize("operation", ["stage", "native_search"])
async def test_runtime_archives_only_model_payload_not_credential_envelope(tmp_path, operation):
    runtime = Runtime.__new__(Runtime)
    runtime.store = DecisionStore(tmp_path / "db")
    runtime.clock = lambda: 100
    runtime.function_arn = "fixture"

    class Client:
        def invoke(self, **request):
            event = json.loads(request["Payload"])
            assert event["provider_credentials"]["openai"] == "private_key_fixture"
            return {
                "Payload": io.BytesIO(
                    json.dumps({"result": {}, "trace": [{"input_tokens": 5}]}).encode()
                )
            }

    runtime.client = Client()
    fields = (
        {"payload": {"brief": "owner_input_fixture"}}
        if operation == "stage"
        else {"queries": [{"symbol": "000001", "name": "회사", "topic": "earnings"}]}
    )
    await runtime.capability(
        {
            "models": {"research": "fixture"},
            "provider_credentials": {"openai": "private_key_fixture"},
        },
        operation,
        role="research",
        **fields,
    )
    record = runtime.store.db.execute("SELECT data FROM ai_usage").fetchone()[0]
    assert "private_key_fixture" not in record
    assert json.loads(record)["model_payload"] == (
        fields["payload"] if operation == "stage" else fields
    )
    assert json.loads(record)["input_context"]["payload_bytes"] > 0
    runtime.store.db.close()
