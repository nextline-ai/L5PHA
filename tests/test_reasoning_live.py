import json
from contextlib import closing

import pytest
from test_management import ORIGIN, bind, management  # noqa: F401
from test_operating_policy import LIMITS
from test_shadow import NOW, Model, event, gate, report

from veyquant.model_catalog import catalog_view
from veyquant.operating_policy import OperatingPolicy, PolicyConflict
from veyquant.shadow_inference import (
    MODEL_PRESETS,
    analyze,
    call,
    model_selection,
    reasoning_options,
    reasoning_selection,
)
from veyquant.shadow_runner import validated_report
from veyquant.store import Store


def test_nova_removed_without_overwriting_existing_policy(tmp_path):
    with closing(Store(str(tmp_path / "db"))) as store:
        policy = OperatingPolicy(store)
        policy.save(LIMITS, "0", NOW, model_selection())
        old = dict.fromkeys(model_selection(), "amazon.nova-pro-v1:0")
        store.db.execute("UPDATE operating_policy SET models=?", (json.dumps(old),))
        view = policy.view()
        assert view["models"] == old and view["limits"] == LIMITS
        assert view["model_reselection_required"] and not view["onboarding_completed"]
        assert not view["model_connection"]["ready"]
        unchanged = policy.save(LIMITS, "1", NOW)
        assert unchanged["models"] == old and unchanged["model_reselection_required"]
        with pytest.raises(ValueError, match="model_not_available"):
            policy.save(LIMITS, "1", NOW, old)
        assert not any("nova" in m["id"] for m in catalog_view()["models"])


def test_reasoning_persists_and_money_only_changes_preserve_it(tmp_path):
    with closing(Store(str(tmp_path / "db"))) as store:
        policy = OperatingPolicy(store)
        levels = {"cheap": "low", "middle": "medium", "research": "max"}
        policy.save(LIMITS, "0", NOW, model_selection(), reasoning=levels, live_requested="true")
        saved = policy.save(LIMITS | {"capital_krw": "2000000"}, "1", NOW)
        assert saved["reasoning"] == levels and saved["live_requested"]
        assert not saved["live_enabled"] and saved["live_state"] == "activation_pending"
    with closing(Store(str(tmp_path / "db"))) as store:
        policy = OperatingPolicy(store)
        assert policy.view() == saved
        with pytest.raises(PolicyConflict):
            policy.set_live_preference("false", "1", NOW)
        assert policy.set_live_preference("false", "2", NOW)["live_state"] == "disabled"


@pytest.mark.parametrize(
    "model,level",
    [
        ("gpt-6-astra", "none"),
        ("gemini-3.8-flash", "minimal"),
        (MODEL_PRESETS["claude"]["cheap"], "max"),
        ("gemini-2.5-pro", "none"),
    ],
)
def test_unsupported_reasoning_rejected_before_call(model, level):
    models = dict.fromkeys(model_selection(), model)
    with pytest.raises(ValueError, match="unsupported_reasoning"):
        reasoning_selection(models, dict.fromkeys(models, level))


def test_claude_reasoning_fields_budget_and_report_identity():
    levels = {"cheap": "low", "middle": "medium", "research": "max"}
    request = event() | {"models": model_selection(), "settings_revision": 1, "reasoning": levels}
    client = Model([gate(), gate(), report()])
    raw = analyze(request, client, NOW)
    assert client.calls[0]["additionalModelRequestFields"] == {
        "thinking": {"type": "enabled", "budget_tokens": 2048}
    }
    for i, level in [(1, "medium"), (2, "max")]:
        assert client.calls[i]["additionalModelRequestFields"] == {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": level},
        }
    assert [c["inferenceConfig"]["maxTokens"] for c in client.calls] == [2432, 4736, 17984]
    safe = validated_report(raw, request, NOW)
    assert [s["reasoning"] for s in safe["stages"]] == list(levels.values())
    raw["stages"][0]["reasoning"] = "none"
    with pytest.raises(ValueError, match="invalid_reasoning_identity"):
        validated_report(raw, request, NOW)


def test_provider_reasoning_is_discarded_not_rendered_or_stored():
    class Thinking:
        def converse(self, **kwargs):
            return {
                "stopReason": "end_turn",
                "usage": {"inputTokens": 30, "outputTokens": 40},
                "output": {
                    "message": {
                        "content": [
                            {"reasoningContent": {"reasoningText": {"text": "PRIVATE_THOUGHT"}}},
                            {"text": json.dumps(gate())},
                        ]
                    }
                },
            }

    trace = []
    assert call(Thinking(), "cheap", {}, trace) == gate()
    assert "PRIVATE_THOUGHT" not in json.dumps(trace)


def test_live_preference_requires_auth_csrf_and_revision(management):  # noqa: F811
    client, _, _, _ = management
    payload = {"enabled": "true", "expected_revision": "1"}
    assert (
        client.post("/v1/live-preference", headers={"origin": ORIGIN}, json=payload).status_code
        == 401
    )
    login = bind(management)
    headers = {"origin": ORIGIN, "x-veyquant-csrf": login.json()["csrf_token"]}
    models = {role + "_model": model for role, model in model_selection().items()}
    settings = LIMITS | models | {"expected_revision": "0", "live_requested": "false"}
    assert client.post("/v1/settings", headers=headers, json=settings).status_code == 200
    assert (
        client.post("/v1/live-preference", headers={"origin": ORIGIN}, json=payload).status_code
        == 403
    )
    result = client.post("/v1/live-preference", headers=headers, json=payload)
    assert result.status_code == 200
    policy = result.json()["operating_policy"]
    assert policy["live_requested"] and not policy["live_enabled"]
    assert client.post("/v1/live-preference", headers=headers, json=payload).status_code == 409
    assert (
        client.post(
            "/v1/live-preference",
            headers=headers,
            json=payload | {"enabled": "false", "expected_revision": "2"},
        ).status_code
        == 200
    )


def test_catalog_reasoning_options_and_new_limits():
    catalog = catalog_view()
    for m in catalog["models"]:
        assert m["reasoning_options"] == reasoning_options(m["id"])
    for preset in catalog["limit_presets"]:
        limits = preset["limits"]
        assert int(limits["max_order_krw"]) * 2 == int(limits["capital_krw"])
        assert int(limits["max_daily_loss_krw"]) * 10 == int(limits["capital_krw"])


@pytest.mark.parametrize("preset", ["chatgpt", "gemini"])
def test_every_direct_provider_reasoning_option_reaches_request(monkeypatch, preset):
    from unittest.mock import Mock

    import veyquant.shadow_inference as module

    for model in list(MODEL_PRESETS[preset].values()) + (
        ["gpt-6-astra"] if preset == "chatgpt" else []
    ):
        for effort in reasoning_options(model):
            models = dict.fromkeys(model_selection(), model)
            levels = dict.fromkeys(models, effort)
            if preset == "chatgpt":
                request = Mock(
                    return_value={
                        "status": "completed",
                        "usage": {"input_tokens": 10, "output_tokens": 20},
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "status": "completed",
                                "content": [{"type": "output_text", "text": json.dumps(gate())}],
                            }
                        ],
                    }
                )
                monkeypatch.setattr(module, "provider_request", request)
                call(
                    None,
                    "cheap",
                    {},
                    [],
                    models=models,
                    keys={"openai": "fixture"},
                    reasoning=levels,
                )
                body = request.call_args.args[3]
                assert body["reasoning"] == {"effort": effort}
                assert body["max_output_tokens"] >= 384
            else:
                reply = {
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {"parts": [{"text": json.dumps(gate())}]},
                        }
                    ],
                    "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 20},
                }
                conn = Mock()
                conn.getresponse.return_value = Mock(
                    status=200, read=Mock(return_value=json.dumps(reply).encode())
                )
                monkeypatch.setattr(module.http.client, "HTTPSConnection", Mock(return_value=conn))
                call(
                    None,
                    "cheap",
                    {},
                    [],
                    models=models,
                    keys={"gemini": "fixture"},
                    reasoning=levels,
                )
                body = json.loads(conn.request.call_args.kwargs["body"])
                config = body["generationConfig"]["thinkingConfig"]
                assert config == (
                    {"thinkingBudget": {"low": 128, "medium": 4096, "high": 16384}[effort]}
                    if model == "gemini-2.5-pro"
                    else {"thinkingLevel": effort.upper()}
                )
