import base64
import json
from types import SimpleNamespace

import pytest

from veyquant import shadow_inference as inference
from veyquant.model_catalog import connection_view

KEY = "fixture_key_never_a_real_secret_12345"
BLOB = base64.b64encode(b"encrypted-fixture-only").decode()


def credential(provider="openai"):
    return {
        "ciphertext": BLOB,
        "models": sorted(inference.DIRECT_MODELS[provider]),
        "verified_at": 1000,
    }


def test_connection_checks_exact_model_endpoints_and_encrypts_with_provider_context(monkeypatch):
    requests, encrypted = [], []

    def request(provider, key, path, payload=None):
        requests.append((provider, key, path, payload))
        return {"id": path.split("/")[-1]} if not path.endswith("gpt-6-astra") else None

    def encrypt(**kwargs):
        encrypted.append(kwargs)
        return {"CiphertextBlob": b"encrypted-fixture-only"}

    monkeypatch.setattr(inference, "provider_request", request)
    monkeypatch.setattr(inference, "kms_client", lambda: SimpleNamespace(encrypt=encrypt))
    monkeypatch.setenv("PROVIDER_KEY_ARN", "fixture-kms-arn")
    saved = inference.configure_provider(
        {"operation": "configure_provider", "provider": "openai", "api_key": KEY}, 1000
    )
    assert len(requests) == 6 and all(row[3] is None for row in requests)
    assert all(row[2].startswith("/v1/models/gpt-") for row in requests)
    assert encrypted[0]["EncryptionContext"] == inference.credential_context("openai")
    assert encrypted[0]["Plaintext"] == KEY.encode()
    assert KEY not in json.dumps(saved) and "gpt-6-astra" not in saved["models"]
    assert connection_view(inference.MODEL_PRESETS["chatgpt"], {"openai": saved})["ready"]
    assert not connection_view(dict.fromkeys(inference.MODELS, "gpt-6-astra"), {"openai": saved})[
        "ready"
    ]


def test_unavailable_or_invalid_keys_cannot_be_saved(monkeypatch):
    monkeypatch.setattr(inference, "provider_request", lambda *a: None)
    monkeypatch.setattr(
        inference, "kms_client", lambda: pytest.fail("must not encrypt unverified key")
    )
    with pytest.raises(ValueError, match="models_unavailable"):
        inference.configure_provider(
            {"operation": "configure_provider", "provider": "gemini", "api_key": KEY}, 1000
        )
    for key in ("short", KEY + "\r\n", "https://evil.invalid/" + KEY):
        result = inference.handler(
            {"operation": "configure_provider", "provider": "openai", "api_key": key}, None
        )
        assert result == {"error": "provider_connection_failed"}


def test_decrypts_only_selected_provider_with_exact_key_and_bound_context(monkeypatch):
    calls = []

    def decrypt(**kwargs):
        calls.append(kwargs)
        return {"Plaintext": KEY.encode()}

    monkeypatch.setenv("PROVIDER_KEY_ARN", "fixture-kms-arn")
    monkeypatch.setattr(inference, "kms_client", lambda: SimpleNamespace(decrypt=decrypt))
    saved = {"openai": credential(), "gemini": credential("gemini")}
    keys = inference.decrypt_credentials(saved, inference.MODEL_PRESETS["chatgpt"])
    assert keys == {"openai": KEY} and len(calls) == 1
    assert calls[0] == {
        "KeyId": "fixture-kms-arn",
        "CiphertextBlob": b"encrypted-fixture-only",
        "EncryptionContext": inference.credential_context("openai"),
    }


def test_openai_uses_responses_without_storage_and_omits_reasoning_from_trace(monkeypatch):
    requests = []

    def request(*args):
        requests.append(args)
        return {
            "status": "completed",
            "output": [
                {"type": "reasoning", "summary": [{"text": "internal fixture"}]},
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": '{"action":"hold","summary":"관찰"}'}
                    ],
                },
            ],
            "usage": {"input_tokens": 20, "output_tokens": 30},
        }

    monkeypatch.setattr(inference, "provider_request", request)
    trace = []
    result = inference.call(
        None,
        "cheap",
        {"schema": "JSON"},
        trace,
        inference.MODEL_PRESETS["chatgpt"],
        keys={"openai": KEY},
    )
    assert result["action"] == "hold"
    payload = requests[0][3]
    assert payload["store"] is False and payload["max_output_tokens"] == 2432
    assert payload["reasoning"] == {"effort": "low"}
    assert "JSON" in payload["input"]
    assert "tools" not in payload and KEY not in json.dumps(payload)
    assert trace[0]["output_tokens"] == 30 and "internal fixture" not in json.dumps(trace)


def test_openai_strict_brief_requires_full_schema_and_limits_candidate_scope(monkeypatch):
    from jsonschema import ValidationError, validate

    payload = {
        "protocol": "decision-v2",
        "task": "review_batch",
        "allowed_candidate_symbols": ["005930"],
        "available_evidence_ids": ["market:005930", "E0001"],
    }
    fmt = inference.openai_output_format("middle", payload)
    value = {
        "severity": "WARN",
        "summary": "검토",
        "candidates": [{"symbol": "005930", "reason": "제공 자료"}],
        "evidence_ids": ["market:005930", "E0001"],
        "uncertainties": [],
    }
    validate({"result": value}, fmt["schema"])
    for wrong in [
        {"candidates": []},
        value | {"severity": "HIGH"},
        value | {"candidates": [{"symbol": "000660", "reason": "다른 묶음"}]},
        value | {"trigger_decision": True},
        value | {"evidence_ids": ["market:452080"]},
        value | {"evidence_ids": ["E9999"]},
    ]:
        with pytest.raises(ValidationError):
            validate({"result": wrong}, fmt["schema"])

    def request(provider, key, path, body, **kwargs):
        assert body["text"]["format"] == fmt
        assert body["store"] is False and "tools" not in body
        assert kwargs == {"timeout": 200}
        return {
            "status": "completed",
            "usage": {"input_tokens": 20, "output_tokens": 30},
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": json.dumps({"result": value})}],
                }
            ],
        }

    monkeypatch.setattr(inference, "provider_request", request)
    assert (
        inference.call(
            None, "middle", payload, [], inference.MODEL_PRESETS["chatgpt"], keys={"openai": KEY}
        )
        == value
    )


def test_openai_strict_research_keeps_read_only_tools_without_search_count_caps():
    from jsonschema import ValidationError, validate
    from test_decision_pipeline import final

    fmt = inference.openai_output_format(
        "research", {"protocol": "decision-v2", "task": "investment_decision"}
    )
    validate({"result": final("NO_ACTION")}, fmt["schema"])
    validate({"result": final("SUBMIT")}, fmt["schema"])
    validate(
        {
            "result": {
                "action": "READ",
                "tool": "briefs",
                "arguments": {"after": 4, "memory_book": "여기까지 읽은 투자 근거 요약"},
            }
        },
        fmt["schema"],
    )
    for invalid in [
        final() | {"memory_book": "가" * 2001},
        final() | {"detailed_explanation": "가" * 6001},
        {k: v for k, v in final().items() if k != "detailed_explanation"},
        {k: v for k, v in final().items() if k != "memory_book"},
        {"action": "READ", "tool": "briefs", "arguments": {"after": 4}},
    ]:
        with pytest.raises(ValidationError):
            validate({"result": invalid}, fmt["schema"])

    for tool in ("news", "search"):
        validate(
            {
                "result": {
                    "action": "READ",
                    "tool": tool,
                    "arguments": {
                        "symbols": ["005930"] * 201,
                        "topics": ["earnings"] * 20,
                    },
                }
            },
            fmt["schema"],
        )
    with pytest.raises(ValidationError):
        validate({"result": {"action": "READ", "tool": "sql", "arguments": {}}}, fmt["schema"])


@pytest.mark.parametrize(
    "response",
    [
        {"status": "incomplete", "output": []},
        {"status": "completed", "output": [{"type": "function_call", "name": "trade"}]},
        {"status": "completed", "output": []},
    ],
)
def test_openai_rejects_partial_empty_or_tool_responses(monkeypatch, response):
    monkeypatch.setattr(inference, "provider_request", lambda *a: response)
    with pytest.raises(ValueError):
        inference.openai_converse("gpt-5.6-sol", "system", "JSON", 1600, KEY)


def test_provider_transport_never_follows_redirects_or_echoes_error_body(monkeypatch):
    calls = []

    class Connection:
        def __init__(self, host, timeout):
            calls.append((host, timeout))

        def request(self, method, path, body, headers):
            assert method == "POST" and headers["Authorization"] == "Bearer " + KEY
            assert KEY not in body.decode()

        def getresponse(self):
            return SimpleNamespace(
                status=307, read=lambda *a: pytest.fail("error body must not be read")
            )

        def close(self):
            pass

    monkeypatch.setattr(inference.http.client, "HTTPSConnection", Connection)
    with pytest.raises(ValueError, match="provider_request_failed") as error:
        inference.provider_request("openai", KEY, "/v1/responses", {"input": "JSON"})
    assert KEY not in str(error.value) and calls == [("api.openai.com", 55)]


def test_ciphertext_contract_rejects_plain_keys_cross_provider_models_and_extra_fields():
    for saved in [
        credential() | {"ciphertext": KEY},
        credential() | {"api_key": KEY},
        credential() | {"models": ["gemini-2.5-pro"]},
    ]:
        with pytest.raises(ValueError):
            inference.credential_selection({"openai": saved})


def test_dart_lambda_configuration_error_is_safe_and_does_not_encrypt(monkeypatch):
    from veyquant import research_sources

    def fail(*args):
        raise ValueError("dart_ip_denied")

    monkeypatch.setattr(research_sources, "verify_service", fail)
    monkeypatch.setattr(
        inference, "kms_client", lambda: pytest.fail("must not persist invalid key")
    )
    assert inference.handler(
        {"operation": "configure_provider", "provider": "dart", "api_key": KEY}, None
    ) == {"error": "dart_ip_denied"}


@pytest.mark.parametrize(
    "code,expected",
    [
        ("dart_maintenance", "dart_maintenance"),
        ("private-key-reflected", "provider_connection_failed"),
    ],
)
async def test_bridge_response_only_forwards_allowlisted_dart_error(monkeypatch, code, expected):
    from unittest.mock import AsyncMock, Mock

    from veyquant.provider_connections import request_connection

    reader = SimpleNamespace(
        readline=AsyncMock(return_value=json.dumps({"error": code}).encode() + b"\n")
    )
    writer = SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock(), wait_closed=AsyncMock())
    monkeypatch.setattr(
        "veyquant.provider_connections.asyncio.open_unix_connection",
        AsyncMock(return_value=(reader, writer)),
    )
    with pytest.raises(ValueError, match="^" + expected + "$"):
        await request_connection("dart", KEY)
    writer.close.assert_called_once()


def test_strict_citation_scope_stops_before_provider_on_invalid_or_excessive_ids():
    base = {
        "protocol": "decision-v2",
        "task": "review_batch",
        "allowed_candidate_symbols": ["005930"],
    }
    for ids, error in [
        (["invented"], "invalid_citation_scope"),
        ([], "invalid_citation_scope"),
        ([f"E{n:04d}" for n in range(997)], "citation_scope_exceeds_schema_limit"),
    ]:
        with pytest.raises(ValueError, match=error):
            inference.openai_output_format("middle", base | {"available_evidence_ids": ids})


@pytest.mark.parametrize(
    "failure,code",
    [
        (inference.http.client.RemoteDisconnected("secret"), "provider_connection_lost"),
        (OSError("secret"), "provider_transport_failure"),
        (TimeoutError("secret"), "provider_timeout"),
    ],
)
def test_provider_failures_have_safe_diagnostics_without_retry(monkeypatch, failure, code):
    calls = []

    class Connection:
        def request(self, *args, **kwargs):
            calls.append(1)
            raise failure

        def close(self):
            pass

    monkeypatch.setattr(inference.http.client, "HTTPSConnection", lambda *a, **kw: Connection())
    trace = [{"status": "uncertain"}]
    try:
        inference.provider_request("openai", KEY, "/v1/responses", {"input": "private"})
    except Exception as error:
        result = inference.capability_failure(error, trace)
    assert result["error"] == code
    assert trace[0]["failure"]["frames"]
    assert "secret" not in json.dumps(result) and "private" not in json.dumps(result)
    assert len(calls) == 1


def test_invalid_model_json_is_classified_without_echoing_output():
    with pytest.raises(ValueError, match="^invalid_model_json$"):
        inference.parse("{private invalid json")
