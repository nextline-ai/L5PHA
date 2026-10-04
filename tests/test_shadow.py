import copy
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from veyquant.shadow_contract import atomic_json, market_quote, read_control, read_market
from veyquant.shadow_inference import analyze, parse, validate
from veyquant.shadow_runner import ShadowStore, independent_risk, tick, validated_report

NOW = 1788937000


def event():
    return {
        "version": "shadow-v1",
        "event_id": "a" * 64,
        "quote": {
            "symbol": "005930",
            "currency": "KRW",
            "price": "70000",
            "as_of": NOW - 1,
            "received_at": NOW,
        },
        "history": [{"price": "69000", "as_of": NOW - 60}],
    }


class Model:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "stopReason": "end_turn",
            "usage": {"inputTokens": 100, "outputTokens": 50},
            "output": {"message": {"content": [{"text": json.dumps(next(self.replies))}]}},
        }


def gate():
    return {"action": "escalate", "summary": "새로운 관찰을 검토합니다."}


def report():
    return {
        "action": "report",
        "outcome": "watch",
        "summary": "시세를 관찰했습니다.",
        "counterargument": "장기 투자 가치는 알 수 없습니다.",
        "uncertainty": "뉴스와 재무자료가 없습니다.",
        "evidence_ids": ["quote", "coverage"],
    }


def test_real_model_contract_bounded_research_and_independent_risk():
    model = Model([gate(), gate(), {"action": "read_evidence", "ids": ["history"]}, report()])
    result = analyze(event(), model, NOW)
    assert len(model.calls) == 4
    assert [c["inferenceConfig"]["maxTokens"] for c in model.calls] == [2432, 4736, 9792, 9792]
    assert result["outcome"] == "watch"
    safe = validated_report(result, event(), NOW + 2)
    assert not safe["risk"]["accepted"] and not safe["risk"]["order_enabled"]
    assert "independent_execution_check_required" in safe["risk"]["reasons"]
    assert {e["id"] for e in safe["evidence"]} == {"quote", "coverage", "history"}
    assert "stale_quote" in independent_risk(event()["quote"], NOW + 500)["reasons"]


@pytest.mark.parametrize(
    "change", ["secret", "us_symbol", "stale", "future", "invalid_number", "history_order"]
)
def test_boundary_rejects_unapproved_inputs_before_model_call(change):
    e = event()
    if change == "secret":
        e["credentials"] = "must-never-be-sent"
    if change == "us_symbol":
        e["quote"]["symbol"], e["quote"]["currency"] = "AAPL", "USD"
    if change == "stale":
        e["quote"]["as_of"] = NOW - 241
    if change == "future":
        e["quote"]["received_at"] = NOW + 1
    if change == "invalid_number":
        e["quote"]["price"] = "NaN"
    if change == "history_order":
        e["history"] *= 2
    with pytest.raises(ValueError):
        validate(e, NOW)


@pytest.mark.parametrize(
    "bad",
    [
        {"action": "read_evidence", "ids": ["http://169.254.169.254"]},
        report() | {"evidence_ids": ["invented"]},
        report() | {"action": "buy"},
    ],
)
def test_model_cannot_choose_url_or_invent_citation_or_order(bad):
    result = analyze(event(), Model([gate(), gate(), bad]), NOW)
    assert result["outcome"] == "error"
    assert result["error_type"] == "ValueError"


def test_gate_short_circuits_and_repeated_research_stops():
    model = Model([{"action": "hold", "summary": "추가 분석을 보류합니다."}])
    assert analyze(event(), model, NOW)["outcome"] == "no_action"
    assert len(model.calls) == 1
    model = Model(
        [
            gate(),
            gate(),
            {"action": "read_evidence", "ids": ["history"]},
            {"action": "read_evidence", "ids": ["history"]},
        ]
    )
    assert analyze(event(), model, NOW)["outcome"] == "error"
    assert len(model.calls) == 4


def test_duplicate_json_and_unexpected_response_rejected():
    with pytest.raises(ValueError):
        parse('{"action":"hold","action":"buy"}')
    assert parse('```json\n{"action":"hold"}\n```') == {"action": "hold"}
    model = Mock()
    model.converse.side_effect = RuntimeError("sensitive-provider-detail")
    result = analyze(event(), model, NOW)
    assert result["stages"][0]["status"] == "uncertain"
    assert "sensitive" not in json.dumps(result)


def test_market_minimizes_data_and_excludes_us(tmp_path):
    q = market_quote(
        "005930",
        {
            "currency": "KRW",
            "price": "70000",
            "timestamp": "2026-09-09T06:56:39+00:00",
            "accountSecret": "private",
        },
        NOW,
    )
    assert "accountSecret" not in q
    path = tmp_path / "market"
    us = q | {"symbol": "AAPL", "currency": "USD"}
    atomic_json(path, {"updated_at": NOW, "connected": True, "quotes": [q, us]})
    assert [x["symbol"] for x in read_market(path, NOW)] == ["005930"]
    with pytest.raises(ValueError, match="stale_market_export"):
        read_market(path, NOW + 181)
    atomic_json(path, {"updated_at": NOW + 181, "connected": True, "quotes": [q]})
    assert not read_market(path, NOW + 181)


def test_control_stale_or_missing_blocks_work(tmp_path):
    p = tmp_path / "control"
    assert not read_control(p, NOW)
    atomic_json(p, {"updated_at": NOW, "owner_bound": True, "stopped": False})
    assert read_control(p, NOW)
    assert not read_control(p, NOW + 16)
    atomic_json(p, {"updated_at": NOW, "owner_bound": True, "stopped": True})
    assert not read_control(p, NOW)


def test_reservation_deduplicates_across_threads_and_restarts(tmp_path):
    path = str(tmp_path / "db")
    initial = ShadowStore(path)
    initial.db.close()

    def reserve():
        s = ShadowStore(path)
        try:
            return s.reserve(event()["quote"], NOW)
        finally:
            s.db.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: reserve(), range(4)))
    assert sum(r is not None for r in results) == 1
    s = ShadowStore(path)
    try:
        assert s.used(NOW) == 1
        assert s.reserve(event()["quote"], NOW + 4000) is None
        assert s.reserve(event()["quote"] | {"price": "72000"}, NOW + 4000) is not None
    finally:
        s.db.close()


def test_daily_budget_counts_uncertain_and_reserved_jobs(tmp_path):
    s = ShadowStore(str(tmp_path / "db"))
    try:
        for i in range(12):
            s.db.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,NULL)",
                (str(i), "OTHER", int(NOW // 86400), NOW, "uncertain", "{}"),
            )
        assert s.reserve(event()["quote"], NOW) is None
        assert s.used(NOW) == 12
    finally:
        s.db.close()


def test_stop_during_provider_call_discards_result_and_never_retries(tmp_path, monkeypatch):
    import veyquant.shadow_runner as module

    control, market, reports = [tmp_path / n for n in ("control", "market", "reports")]
    atomic_json(control, {"updated_at": NOW, "owner_bound": True, "stopped": False})
    atomic_json(market, {"updated_at": NOW, "connected": True, "quotes": [event()["quote"]]})
    s = ShadowStore(str(tmp_path / "db"))
    calls = []

    def invoke(*args):
        calls.append(True)
        atomic_json(control, {"updated_at": NOW, "owner_bound": True, "stopped": True})
        return {}

    monkeypatch.setattr(module, "invoke", invoke)
    try:
        tick(s, None, "arn", market, control, reports, clock=lambda: NOW)
        assert s.db.execute("SELECT status FROM jobs").fetchone()[0] == "cancelled"
        assert not json.loads(reports.read_text())["reports"]
        tick(s, None, "arn", market, control, reports, clock=lambda: NOW)
        assert len(calls) == 1
    finally:
        s.db.close()


def test_tampered_lambda_quote_cannot_reach_report():
    raw = analyze(event(), Model([gate(), gate(), report()]), NOW)
    raw["quote"] = copy.deepcopy(raw["quote"]) | {"price": "1"}
    with pytest.raises(ValueError):
        validated_report(raw, event(), NOW)


def test_dispatch_failure_preserves_count_without_exception_payload(tmp_path):
    from botocore.exceptions import ClientError

    s = ShadowStore(str(tmp_path / "db"))
    try:
        reserved = s.reserve(event()["quote"], NOW)
        s.failed(
            reserved["event_id"],
            ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "private-provider-detail"}},
                "Invoke",
            ),
        )
        assert s.used(NOW) == 1
        assert s.db.execute("SELECT code FROM failures").fetchone()[0] == "AccessDeniedException"
        assert s.db.execute("SELECT status FROM jobs").fetchone()[0] == "uncertain"
        assert "private-provider-detail" not in "\n".join(s.db.iterdump())
        assert s.reserve(event()["quote"], NOW + 30) is None
    finally:
        s.db.close()


def test_rendered_lambda_matches_reviewed_python_source():
    import ast
    import base64
    import hashlib
    import zlib
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    template = yaml.load((root / "infra/analysis.yaml").read_text(), Loader=yaml.BaseLoader)
    rendered = template["Resources"]["AnalysisFunction"]["Properties"]["Code"]["ZipFile"]
    assignments = {
        n.targets[0].id: ast.literal_eval(n.value)
        for n in ast.parse(rendered).body
        if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Constant)
        and isinstance(n.targets[0], ast.Name)
    }
    raw = zlib.decompress(base64.b64decode(assignments["BUNDLE"]))
    assert hashlib.sha256(raw).hexdigest() == assignments["DIGEST"]
    files = json.loads(raw)
    assert set(files) == {
        "__init__.py",
        "shadow_inference.py",
        "research_sources.py",
        "native_search.py",
        "input_budget.py",
        "model_prompts.py",
    }
    for name, source in files.items():
        assert source == (root / "src/veyquant" / name).read_text()
    assert len((root / "infra/analysis.yaml").read_bytes()) <= 51200


def test_owner_selected_models_are_pinned_and_report_identity_must_match():
    from veyquant.shadow_inference import model_selection

    selected = model_selection() | {
        "cheap": "au.anthropic.claude-opus-4-6-v1",
        "research": "au.anthropic.claude-sonnet-4-6",
    }
    request = event() | {"models": selected, "settings_revision": 4}
    model = Model([gate(), gate(), report()])
    result = analyze(request, model, NOW)
    assert [c["modelId"] for c in model.calls] == list(selected.values())
    assert [c["inferenceConfig"]["maxTokens"] for c in model.calls] == [2432, 4736, 9792]
    assert validated_report(result, request, NOW + 1)["settings_revision"] == 4
    altered = copy.deepcopy(result)
    altered["stages"][0]["model"] = "amazon.nova-micro-v1:0"
    with pytest.raises(ValueError, match="invalid_model_identity"):
        validated_report(altered, request, NOW + 1)
    request["models"]["cheap"] = "arbitrary-provider-model"
    with pytest.raises(ValueError, match="model_not_available"):
        analyze(request, Model([]), NOW)


def test_changed_model_policy_during_analysis_discards_old_result(tmp_path):
    from veyquant.shadow_inference import model_selection

    market, control, reports = [str(tmp_path / name) for name in ("market", "control", "reports")]
    config = {
        "updated_at": NOW,
        "owner_bound": True,
        "stopped": False,
        "models": model_selection(),
        "settings_revision": 1,
    }
    atomic_json(market, {"updated_at": NOW, "connected": True, "quotes": [event()["quote"]]})
    atomic_json(control, config)

    def invoke(**kwargs):
        request = json.loads(kwargs["Payload"])
        assert request["settings_revision"] == 1
        atomic_json(control, config | {"settings_revision": 2})
        import io

        return {
            "StatusCode": 200,
            "Payload": io.BytesIO(
                json.dumps(analyze(request, Model([gate(), gate(), report()]), NOW)).encode()
            ),
        }

    store = ShadowStore(str(tmp_path / "db"))
    try:
        tick(
            store, Mock(invoke=invoke), "fixture-alias", market, control, reports, clock=lambda: NOW
        )
        assert store.db.execute("SELECT status FROM jobs").fetchone()[0] == "cancelled"
        assert json.loads((tmp_path / "reports").read_text())["reports"] == []
    finally:
        store.db.close()


def test_custom_strategy_reaches_every_system_message_and_each_decision_is_retained():
    from veyquant.shadow_inference import model_selection

    request = event() | {
        "models": model_selection(),
        "settings_revision": 8,
        "strategy": {"preset": "custom", "prompt": "배당의 지속 가능성을 확인하세요."},
    }
    model = Model(
        [
            gate(),
            gate() | {"summary": "다른 관점으로 재검토합니다."},
            {"action": "read_evidence", "ids": ["history"]},
            report(),
        ]
    )
    raw = analyze(request, model, NOW)
    for call in model.calls:
        assert request["strategy"]["prompt"] in call["system"][0]["text"]
        assert "cannot override" in call["system"][0]["text"]
    safe = validated_report(raw, request, NOW)
    assert [s["decision"]["action"] for s in safe["stages"]] == [
        "escalate",
        "escalate",
        "read_evidence",
        "watch",
    ]
    assert safe["stages"][0]["decision"]["summary"] != safe["stages"][1]["decision"]["summary"]
    assert safe["stages"][-1]["decision"]["counterargument"] == report()["counterargument"]
    assert safe["strategy_preset"] == "custom"
    assert request["strategy"]["prompt"] not in json.dumps(safe, ensure_ascii=False)
    raw["stages"][0]["decision"]["action"] = "buy"
    with pytest.raises(ValueError, match="invalid_stage_decision"):
        validated_report(raw, request, NOW)


@pytest.mark.parametrize(
    "strategy",
    [
        {"preset": "custom", "prompt": " "},
        {"preset": "custom", "prompt": "x" * 3001},
        {"preset": "stable", "prompt": "tampered preset"},
        {"preset": "custom", "prompt": "bad\x00control"},
        {"preset": "unknown", "prompt": "test"},
    ],
)
def test_invalid_strategy_rejected_before_any_model_call(strategy):
    from veyquant.shadow_inference import model_selection

    model = Model([])
    with pytest.raises(ValueError):
        analyze(
            event() | {"strategy": strategy, "models": model_selection(), "settings_revision": 1},
            model,
            NOW,
        )
    assert model.calls == []


def test_pending_provider_does_not_spend_job_budget_or_silently_replace_model(tmp_path):
    from veyquant.shadow_inference import MODEL_PRESETS, strategy_selection

    control, market, reports = [tmp_path / n for n in ("control", "market", "reports")]
    atomic_json(market, {"updated_at": NOW, "connected": True, "quotes": [event()["quote"]]})
    store = ShadowStore(str(tmp_path / "db"))
    client = Mock()
    try:
        for preset in ("chatgpt", "gemini"):
            atomic_json(
                control,
                {
                    "updated_at": NOW,
                    "owner_bound": True,
                    "stopped": False,
                    "models": MODEL_PRESETS[preset],
                    "settings_revision": 1,
                    "strategy": strategy_selection(),
                },
            )
            tick(store, client, "arn", market, control, reports, clock=lambda: NOW)
            assert json.loads(reports.read_text())["state"] == "model_setup_required"
            assert store.used(NOW) == 0
        client.invoke.assert_not_called()
    finally:
        store.db.close()


def test_maximum_unicode_strategy_survives_control_boundary(tmp_path):
    from veyquant.shadow_contract import read_model_configuration
    from veyquant.shadow_inference import model_selection

    value = {
        "updated_at": NOW,
        "owner_bound": True,
        "stopped": False,
        "models": model_selection(),
        "settings_revision": 1,
        "strategy": {"preset": "custom", "prompt": "가" * 3000},
    }
    target = tmp_path / "control"
    atomic_json(target, value)
    assert read_control(target, NOW)
    assert read_model_configuration(target, NOW)["strategy"] == value["strategy"]


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_gemini_errors_do_not_retry_follow_redirects_or_expose_key(monkeypatch, status):
    import veyquant.shadow_inference as module

    monkeypatch.setenv("GEMINI_API_KEY", "fixture-private-key")
    response = Mock(status=status)
    connection = Mock()
    connection.getresponse.return_value = response
    factory = Mock(return_value=connection)
    monkeypatch.setattr(module.http.client, "HTTPSConnection", factory)
    with pytest.raises(ValueError, match="^provider_request_failed$"):
        module.gemini_converse("gemini-3.8-flash", "system fixture", "message fixture", 384)
    assert connection.request.call_count == 1
    args, kwargs = connection.request.call_args
    assert factory.call_args.args[0] == "generativelanguage.googleapis.com"
    assert "fixture-private-key" not in str(args) + str(kwargs["body"])
    assert kwargs["headers"]["x-goog-api-key"] == "fixture-private-key"
    response.read.assert_not_called()
    connection.close.assert_called_once()


def test_gemini_normalizes_usage_and_refuses_tools_or_thought_content(monkeypatch):
    import veyquant.shadow_inference as module

    monkeypatch.setenv("GEMINI_API_KEY", "fixture-private-key")
    data = {
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {"parts": [{"text": '{"action":"hold","summary":"관찰"}'}]},
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 20,
            "candidatesTokenCount": 8,
            "thoughtsTokenCount": 10,
        },
    }
    response = Mock(status=200)
    response.read.side_effect = lambda size: json.dumps(data).encode()
    conn = Mock()
    conn.getresponse.return_value = response
    monkeypatch.setattr(module.http.client, "HTTPSConnection", Mock(return_value=conn))
    result = module.gemini_converse("gemini-2.5-pro", "system", "message", 1600)
    assert result["usage"] == {"inputTokens": 20, "outputTokens": 18}
    data["candidates"][0]["content"]["parts"] = [{"functionCall": {"name": "order"}}]
    with pytest.raises(ValueError, match="unexpected_model_content"):
        module.gemini_converse("gemini-2.5-pro", "system", "message", 1600)


def test_long_role_summaries_do_not_break_bounded_management_export(tmp_path):
    from veyquant.shadow_runner import read_view

    store = ShadowStore(str(tmp_path / "db"))
    try:
        for i in range(10):
            raw = analyze(event(), Model([gate(), gate(), report()]), NOW)
            safe = validated_report(raw, event(), NOW)
            safe["summary"] = "가" * 1200
            for stage in safe["stages"]:
                stage["decision"]["summary"] = "나" * 1200
            store.db.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?)",
                (str(i), "005930", int(NOW // 86400), NOW + i, "done", "{}", json.dumps(safe)),
            )
        target = tmp_path / "report.json"
        store.export(target, NOW, "model_setup_required")
        assert target.stat().st_size <= 131072
        value = read_view(target, NOW)
        assert value["state"] == "model_setup_required"
        assert 0 < len(value["reports"]) < 10
        assert store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 10
    finally:
        store.db.close()


def test_rotated_provider_ciphertext_cancels_inflight_analysis_without_resetting_budget(tmp_path):
    import base64
    import io

    from test_provider_connections import credential

    from veyquant.shadow_inference import model_selection

    market, control, reports = [str(tmp_path / name) for name in ("market", "control", "reports")]
    config = {
        "updated_at": NOW,
        "owner_bound": True,
        "stopped": False,
        "models": model_selection(),
        "settings_revision": 1,
        "provider_credentials": {"openai": credential()},
    }
    atomic_json(market, {"updated_at": NOW, "connected": True, "quotes": [event()["quote"]]})
    atomic_json(control, config)

    def invoke(**kwargs):
        request = json.loads(kwargs["Payload"])
        config["provider_credentials"]["openai"]["ciphertext"] = base64.b64encode(
            b"replacement-encrypted-fixture"
        ).decode()
        atomic_json(control, config)
        result = analyze(request, Model([gate(), gate(), report()]), NOW)
        assert "ciphertext" not in json.dumps(result)
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(result).encode())}

    store = ShadowStore(str(tmp_path / "db"))
    try:
        tick(
            store, Mock(invoke=invoke), "fixture-alias", market, control, reports, clock=lambda: NOW
        )
        assert store.db.execute("SELECT status FROM jobs").fetchone()[0] == "cancelled"
        assert store.used(NOW) == 1
        assert json.loads((tmp_path / "reports").read_text())["reports"] == []
    finally:
        store.db.close()
