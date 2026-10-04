import json
from contextlib import closing

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from starlette.testclient import TestClient
from test_telegram import signed

from veyquant.management import ManagementConfig, create_app
from veyquant.store import Store
from veyquant.telegram_auth import SESSION_TTL, OwnerAuth, TelegramVerifier

ORIGIN = "https://investor.example"


@pytest.fixture
def management(tmp_path):
    signer = Ed25519PrivateKey.generate()
    verifier = TelegramVerifier(123, public_key=signer.public_key().public_bytes_raw())
    path = str(tmp_path / "management.sqlite3")
    store = Store(path)
    invitation = OwnerAuth(store, verifier).issue_invitation(1000)
    store.close()
    config = ManagementConfig(path, ORIGIN, 123)
    client = TestClient(create_app(config, verifier=verifier, clock=lambda: 1000), base_url=ORIGIN)
    with client:
        yield client, signer, invitation, config


def bind(management):
    client, signer, invitation, _ = management
    return client.post(
        "/v1/auth/bind",
        headers={"origin": ORIGIN},
        json={"init_data": signed(signer), "invitation": invitation},
    )


def test_binding_cookie_status_stop_revoke(management):
    client, _, _, config = management
    assert client.get("/v1/status").status_code == 401
    response = bind(management)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert all(word in cookie for word in ["HttpOnly", "Secure", "SameSite=strict", "Path=/"])
    assert config.cookie_name == "__Host-veyquant_session"
    assert "Domain=" not in cookie and "session" not in response.json()
    assert f"Max-Age={SESSION_TTL}" in cookie
    csrf = response.json()["csrf_token"]
    assert client.get("/v1/status").json()["broker_connected"] is False
    response = client.post(
        "/v1/control/stop", headers={"origin": ORIGIN, "x-veyquant-csrf": csrf}, json={}
    )
    assert response.status_code == 200 and response.json()["new_proposals_stopped"] is True
    assert client.get("/v1/status").json()["new_proposals_stopped"] is True
    response = client.post(
        "/v1/auth/revoke", headers={"origin": ORIGIN, "x-veyquant-csrf": csrf}, json={}
    )
    assert response.status_code == 200
    assert client.get("/v1/status").status_code == 401


def test_renew_requires_cookie_and_csrf_and_preserves_revocation(management):
    client, _, _, _ = management
    headers = {"origin": ORIGIN}
    assert client.post("/v1/auth/renew", headers=headers, json={}).status_code == 401
    csrf = bind(management).json()["csrf_token"]
    assert client.post("/v1/auth/renew", headers=headers, json={}).status_code == 403
    headers["x-veyquant-csrf"] = csrf
    renewed = client.post("/v1/auth/renew", headers=headers, json={})
    assert renewed.status_code == 200
    assert renewed.json()["csrf_token"] == csrf
    assert f"Max-Age={SESSION_TTL}" in renewed.headers["set-cookie"]
    assert client.get("/v1/status").json()["session_expires_at"] == 1000 + SESSION_TTL
    client.post("/v1/auth/revoke", headers=headers, json={})
    assert client.post("/v1/auth/renew", headers=headers, json={}).status_code == 401


def test_session_cookie_restores_after_server_restart_and_old_code_is_not_password(management):
    client, signer, _, config = management
    bind(management)
    with TestClient(create_app(config, clock=lambda: 2000), base_url=ORIGIN) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get("/v1/status").status_code == 200
    client.cookies.clear()
    again = bind(management)
    assert again.status_code == 401 and again.json()["error"] == "already_bound"
    response = client.post(
        "/v1/auth/login",
        headers={"origin": ORIGIN},
        json={"init_data": signed(signer, query_id="new-launch")},
    )
    assert response.status_code == 200


def test_expired_launch_has_specific_error_and_never_claims_bad_code(management):
    client, signer, _, _ = management
    response = client.post(
        "/v1/auth/login",
        headers={"origin": ORIGIN},
        json={"init_data": signed(signer, auth_date=600)},
    )
    assert response.status_code == 401
    assert response.json()["error"] == "telegram_auth_expired"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"origin": "https://evil.example"},
        {"origin": "null"},
        {"origin": ORIGIN, "sec-fetch-site": "cross-site"},
    ],
)
def test_origin_required_before_binding(management, headers):
    client, signer, invitation, _ = management
    response = client.post(
        "/v1/auth/bind",
        headers=headers,
        json={"init_data": signed(signer), "invitation": invitation},
    )
    assert response.status_code == 403
    assert bind(management).status_code == 200


def test_host_header_cannot_redirect_trust(management):
    client, _, _, _ = management
    assert client.get("/healthz", headers={"host": "evil.example"}).status_code == 400
    assert client.get("/v1/status?init_data=DO_NOT_LOG").status_code == 400


def test_public_shell_can_open_from_telegram_without_exposing_status(management):
    client, _, _, _ = management
    response = client.get("/", headers={"sec-fetch-site": "cross-site"})
    assert response.status_code == 200 and "L5PHA" in response.text
    assert "https://telegram.org" in response.headers["content-security-policy"]
    assert client.get("/app.js").status_code == 200
    assert client.get("/v1/status", headers={"sec-fetch-site": "cross-site"}).status_code == 403


def test_authenticated_request_still_requires_csrf(management):
    client, _, _, _ = management
    bind(management)
    for headers in [{"origin": ORIGIN}, {"origin": ORIGIN, "x-veyquant-csrf": "wrong"}]:
        assert client.post("/v1/control/stop", headers=headers, json={}).status_code == 403
    assert client.get("/v1/status").json()["new_proposals_stopped"] is False


def test_non_ascii_csrf_is_rejected_without_server_error(management):
    client, _, _, _ = management
    bind(management)
    response = client.post(
        "/v1/control/stop",
        headers=[(b"origin", ORIGIN.encode()), (b"x-veyquant-csrf", b"\xff")],
        json={},
    )
    assert response.status_code == 403
    assert client.get("/v1/status").json()["new_proposals_stopped"] is False


def test_get_does_not_consume_invitation(management):
    client, _, _, _ = management
    assert client.get("/v1/auth/bind").status_code == 405
    assert bind(management).status_code == 200


def test_invalid_data_is_sanitized_and_rate_limited(management):
    client, _, _, _ = management
    for _ in range(10):
        response = client.post(
            "/v1/auth/login", headers={"origin": ORIGIN}, json={"init_data": "private-test-marker"}
        )
        assert response.status_code == 401
        assert "private-test-marker" not in response.text
    response = client.post(
        "/v1/auth/login",
        headers={"origin": ORIGIN, "x-forwarded-for": "different-client"},
        json={"init_data": "invalid"},
    )
    assert response.status_code == 429


@pytest.mark.parametrize(
    ("content", "status"),
    [
        ('{"init_data":"one", "init_data":"two"}', 400),
        ('{"init_data":"abc","extra":"not allowed"}', 400),
        ('{"init_data":null}', 400),
        ('{"init_data":12}', 400),
        ('{"init_data":"' + "a" * 21000 + '"}', 413),
        ('{"init_data":' + "[" * 1100 + "0" + "]" * 1100 + "}", 400),
    ],
)
def test_json_contract_and_body_limit(management, content, status):
    client, _, _, _ = management
    response = client.post(
        "/v1/auth/login",
        headers={"origin": ORIGIN, "content-type": "application/json"},
        content=content,
    )
    assert response.status_code == status


def test_security_headers_on_errors_and_success(management):
    client, _, _, _ = management
    for path in ["/healthz", "/v1/status", "/does-not-exist"]:
        response = client.get(path)
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "strict-transport-security" in response.headers


def test_owner_impostor_and_live_routes_unavailable(management):
    client, signer, _, _ = management
    bind(management)
    response = client.post(
        "/v1/auth/login", headers={"origin": ORIGIN}, json={"init_data": signed(signer, user_id=9)}
    )
    assert response.status_code == 401
    for path in ["/v1/orders", "/v1/control/live", "/v1/keys"]:
        assert client.post(path, headers={"origin": ORIGIN}, json={}).status_code == 404


def test_local_admin_can_revoke_without_telegram(management):
    client, _, _, config = management
    bind(management)
    store = Store(config.db_path)
    try:
        OwnerAuth(store, TelegramVerifier(123)).revoke_sessions()
        store.stop()
    finally:
        store.close()
    assert client.get("/v1/status").status_code == 401


def test_authentication_limit_survives_app_restart(management):
    client, _, _, config = management
    for _ in range(10):
        client.post("/v1/auth/login", headers={"origin": ORIGIN}, json={"init_data": "invalid"})
    restarted = TestClient(create_app(config, clock=lambda: 1000), base_url=ORIGIN)
    with restarted:
        response = restarted.post(
            "/v1/auth/login", headers={"origin": ORIGIN}, json={"init_data": "invalid"}
        )
        assert response.status_code == 429


@pytest.mark.parametrize(
    "origin",
    [
        "http://public.example",
        "https://public.example",
        "https://example.com/path",
        "https://user@example.com",
        "https://example.com?foo=bar",
    ],
)
def test_bad_origins_rejected_even_in_development(origin, tmp_path):
    with pytest.raises(ValueError):
        ManagementConfig(str(tmp_path / "db"), origin, 123, development=True)


def test_json_schema_has_no_hidden_token_intake(management):
    client, _, _, _ = management
    response = client.post(
        "/v1/auth/login",
        headers={"origin": ORIGIN},
        content=json.dumps({"bot_token": "private-marker"}),
    )
    assert response.status_code == 415
    assert "private-marker" not in response.text


def test_resume_requires_owner_and_csrf(management):
    client, _, _, _ = management
    assert client.post("/v1/control/resume", headers={"origin": ORIGIN}, json={}).status_code == 401
    csrf = bind(management).json()["csrf_token"]
    assert client.post("/v1/control/resume", headers={"origin": ORIGIN}, json={}).status_code == 403
    headers = {"origin": ORIGIN, "x-veyquant-csrf": csrf}
    client.post("/v1/control/stop", headers=headers, json={})
    result = client.post("/v1/control/resume", headers=headers, json={})
    assert result.status_code == 200
    assert result.json() == {"new_proposals_stopped": False, "live_order_supported": False}


def test_analysis_control_export_has_no_identity_or_tokens(tmp_path):
    from veyquant.shadow_contract import read_control

    signer = Ed25519PrivateKey.generate()
    verifier = TelegramVerifier(123, public_key=signer.public_key().public_bytes_raw())
    path, control = str(tmp_path / "db"), str(tmp_path / "control")
    store = Store(path)
    invitation = OwnerAuth(store, verifier).issue_invitation(1000)
    store.close()
    config = ManagementConfig(path, ORIGIN, 123, shadow_control_path=control)
    with TestClient(
        create_app(config, verifier=verifier, clock=lambda: 1000), base_url=ORIGIN
    ) as client:
        assert not read_control(control, 1000)
        response = client.post(
            "/v1/auth/bind",
            headers={"origin": ORIGIN},
            json={"init_data": signed(signer), "invitation": invitation},
        )
        assert response.status_code == 200
        assert read_control(control, 1000)
        exported = json.loads(__import__("pathlib").Path(control).read_text())
        assert set(exported) == {
            "updated_at",
            "stopped",
            "owner_bound",
            "models",
            "settings_revision",
            "strategy",
            "prompts",
            "provider_credentials",
            "reasoning",
        }
        assert invitation not in json.dumps(exported)
        client.post(
            "/v1/control/stop",
            headers={"origin": ORIGIN, "x-veyquant-csrf": response.json()["csrf_token"]},
            json={},
        )
        assert not read_control(control, 1000)
    assert not read_control(control, 1000)


def test_operating_policy_owner_csrf_and_revision(management):
    from test_operating_policy import LIMITS

    client, _, _, config = management
    data = LIMITS | {"expected_revision": "0"}
    headers = {"origin": ORIGIN}
    assert client.post("/v1/policy", headers=headers, json=data).status_code == 401
    login = bind(management)
    assert client.post("/v1/policy", headers=headers, json=data).status_code == 403
    headers["x-veyquant-csrf"] = login.json()["csrf_token"]
    response = client.post("/v1/policy", headers=headers, json=data)
    assert response.status_code == 200
    assert response.json()["operating_policy"]["revision"] == 1
    assert client.post("/v1/policy", headers=headers, json=data).status_code == 409
    assert (
        client.post("/v1/policy", headers=headers, json=data | {"live_enabled": "true"}).status_code
        == 400
    )
    assert (
        client.post("/v1/policy", headers=headers, json=data | {"capital_krw": "1e8"}).status_code
        == 400
    )
    status = client.get("/v1/status").json()
    assert status["operating_policy"]["limits"] == LIMITS
    assert not status["readiness"]["live_enabled"]
    assert status["new_proposals_stopped"] is False
    client.post("/v1/auth/revoke", headers=headers, json={})
    assert client.get("/v1/status").status_code == 401
    assert client.post("/v1/policy", headers=headers, json=data).status_code == 401


def test_account_summary_is_owner_only(tmp_path):
    from test_account_readiness import NOW, export_data

    from veyquant.shadow_contract import atomic_json

    path = str(tmp_path / "account.json")
    atomic_json(path, export_data())
    config = ManagementConfig(str(tmp_path / "db"), ORIGIN, 123, account_status_path=path)
    with TestClient(create_app(config, clock=lambda: NOW), base_url=ORIGIN) as client:
        assert client.get("/v1/status").status_code == 401
        assert "500000" not in client.get("/healthz").text


def test_onboarding_atomic_settings_and_legacy_limits_preserve_models(management):
    from test_operating_policy import LIMITS

    from veyquant.model_catalog import ROLE_FIELDS
    from veyquant.shadow_inference import model_selection

    client, _, _, _ = management
    login = bind(management)
    headers = {"origin": ORIGIN, "x-veyquant-csrf": login.json()["csrf_token"]}
    selection = model_selection() | {"cheap": "au.anthropic.claude-sonnet-4-6"}
    data = (
        LIMITS
        | {"expected_revision": "0"}
        | {field: selection[role] for field, role in ROLE_FIELDS.items()}
    )
    assert not client.get("/v1/status").json()["operating_policy"]["onboarding_completed"]
    result = client.post("/v1/settings", headers=headers, json=data)
    assert result.status_code == 200
    saved = result.json()["operating_policy"]
    assert saved["models"] == selection and saved["onboarding_completed"]
    assert not saved["live_enabled"]
    assert client.post("/v1/settings", headers=headers, json=data).status_code == 409
    invalid = data | {"expected_revision": "1", "research_model": "unapproved-model"}
    assert client.post("/v1/settings", headers=headers, json=invalid).status_code == 400
    assert client.get("/v1/status").json()["operating_policy"] == saved
    legacy = client.post("/v1/policy", headers=headers, json=LIMITS | {"expected_revision": "1"})
    assert legacy.json()["operating_policy"]["models"] == selection
    assert client.post("/v1/settings", headers={"origin": ORIGIN}, json=data).status_code == 403


def test_strategy_settings_atomic_conflicts_and_legacy_preservation(management):
    from veyquant.shadow_inference import MODEL_PRESETS, STRATEGY_PRESETS

    client, signer, invitation, _ = management
    client.post(
        "/v1/auth/bind",
        headers={"origin": ORIGIN},
        json={"init_data": signed(signer), "invitation": invitation},
    )
    csrf = client.get("/v1/status").json()["csrf_token"]
    headers = {"origin": ORIGIN, "x-veyquant-csrf": csrf}
    limits = {"capital_krw": "5000000", "max_order_krw": "500000", "max_daily_loss_krw": "50000"}
    data = {
        **limits,
        "expected_revision": "0",
        "cheap_model": MODEL_PRESETS["claude"]["cheap"],
        "middle_model": MODEL_PRESETS["claude"]["middle"],
        "research_model": MODEL_PRESETS["claude"]["research"],
        "strategy_preset": "active",
        "strategy_prompt": STRATEGY_PRESETS["active"]["prompt"],
    }
    invalid = data | {"strategy_prompt": "preset mismatch"}
    assert client.post("/v1/settings", headers=headers, json=invalid).status_code == 400
    assert client.get("/v1/status").json()["operating_policy"]["revision"] == 0
    result = client.post("/v1/settings", headers=headers, json=data)
    assert result.status_code == 200
    saved = result.json()["operating_policy"]
    assert saved["strategy_configured"] and saved["strategy"]["preset"] == "active"
    assert saved["model_connection"]["ready"]
    assert client.post("/v1/settings", headers=headers, json=data).status_code == 409
    # Older clients must not erase strategy when changing only limits/models.
    assert (
        client.post(
            "/v1/policy", headers=headers, json=limits | {"expected_revision": "1"}
        ).status_code
        == 200
    )
    old = {key: value for key, value in data.items() if not key.startswith("strategy_")}
    old["expected_revision"] = "2"
    assert (
        client.post("/v1/settings", headers=headers, json=old).json()["operating_policy"][
            "strategy"
        ]
        == saved["strategy"]
    )
    current = data | {
        "expected_revision": "3",
        "strategy_preset": "custom",
        "strategy_prompt": "가" * 3000,
    }
    assert client.post("/v1/settings", headers=headers, json=current).status_code == 200
    assert client.post("/v1/settings", headers={"origin": ORIGIN}, json=current).status_code == 403


def test_owner_provider_key_intake_is_encrypted_and_failed_replacement_preserves_connection(
    management, monkeypatch
):
    import base64

    from veyquant.provider_connections import credentials
    from veyquant.shadow_inference import MODEL_PRESETS

    client, _, _, config = management
    data = {"provider": "openai", "api_key": "fixture_key_only_not_real_123456"}
    headers = {"origin": ORIGIN}
    assert client.post("/v1/providers/connect", json=data, headers=headers).status_code == 401
    headers["x-veyquant-csrf"] = bind(management).json()["csrf_token"]
    assert (
        client.post("/v1/providers/connect", json=data, headers={"origin": ORIGIN}).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/providers/connect",
            json=data,
            headers={**headers, "origin": "https://evil.invalid"},
        ).status_code
        == 403
    )
    saved = {
        "ciphertext": base64.b64encode(b"fixture-encrypted-provider-key").decode(),
        "models": list(MODEL_PRESETS["chatgpt"].values()),
        "verified_at": 1000,
    }

    async def connect(provider, key):
        assert provider == "openai" and key == data["api_key"]
        return saved

    monkeypatch.setattr("veyquant.management.request_connection", connect)
    response = client.post("/v1/providers/connect", json=data, headers=headers)
    assert response.status_code == 200
    assert response.json()["provider_connections"]["openai"]["connected"]
    for value in [response.text, client.get("/v1/status").text]:
        assert (
            "ciphertext" not in value
            and saved["ciphertext"] not in value
            and data["api_key"] not in value
        )
    with closing(Store(config.db_path)) as store:
        assert credentials(store)["openai"] == saved
        assert store.db.execute("SELECT count(*) FROM operating_policy").fetchone()[0] == 0
        audit = [dict(r) for r in store.db.execute("SELECT * FROM audit")]
        assert data["api_key"] not in json.dumps(audit) and saved["ciphertext"] not in json.dumps(
            audit
        )

    async def fail(*args):
        raise ValueError(data["api_key"])

    monkeypatch.setattr("veyquant.management.request_connection", fail)
    response = client.post("/v1/providers/connect", json=data, headers=headers)
    assert response.status_code == 422 and data["api_key"] not in response.text
    with closing(Store(config.db_path)) as store:
        assert credentials(store)["openai"] == saved
    with TestClient(create_app(config, clock=lambda: 1001), base_url=ORIGIN) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get("/v1/status").json()["provider_connections"]["openai"]["connected"]


def test_session_revoked_during_provider_verification_cannot_store_key(management, monkeypatch):
    from test_provider_connections import credential

    client, _, _, config = management
    csrf = bind(management).json()["csrf_token"]

    async def connect(*args):
        with closing(Store(config.db_path)) as store:
            OwnerAuth(store, TelegramVerifier(config.bot_id)).revoke_sessions()
        return credential()

    monkeypatch.setattr("veyquant.management.request_connection", connect)
    response = client.post(
        "/v1/providers/connect",
        headers={"origin": ORIGIN, "x-veyquant-csrf": csrf},
        json={"provider": "openai", "api_key": "fixture_key_not_real_123456789"},
    )
    assert response.status_code == 401
    with closing(Store(config.db_path)) as store:
        assert store.db.execute("SELECT count(*) FROM provider_connections").fetchone()[0] == 0


@pytest.mark.parametrize(
    "provider,capabilities", [("dart", ["disclosures"]), ("krx", ["kospi-daily", "kosdaq-daily"])]
)
def test_optional_connection_and_disconnect_are_authenticated_and_preserve_ai(
    management, monkeypatch, provider, capabilities
):
    import base64

    from veyquant.provider_connections import credentials

    client, _, _, config = management
    headers = {"origin": ORIGIN, "x-veyquant-csrf": bind(management).json()["csrf_token"]}
    before = client.get("/v1/status").json()["operating_policy"]

    async def connect(p, key):
        assert p == provider
        return {
            "ciphertext": base64.b64encode(b"fixture-encrypted-only").decode(),
            "models": capabilities,
            "verified_at": 1000,
        }

    monkeypatch.setattr("veyquant.management.request_connection", connect)
    response = client.post(
        "/v1/providers/connect",
        headers=headers,
        json={"provider": provider, "api_key": "fixture_key_only_not_real_12345"},
    )
    assert response.status_code == 200
    assert response.json()["provider_connections"][provider]["verification"] == "service_access"
    assert (
        client.post(
            "/v1/providers/disconnect", headers={"origin": ORIGIN}, json={"provider": provider}
        ).status_code
        == 403
    )
    response = client.post("/v1/providers/disconnect", headers=headers, json={"provider": provider})
    assert (
        response.status_code == 200
        and not response.json()["provider_connections"][provider]["connected"]
    )
    assert client.get("/v1/status").json()["operating_policy"] == before
    with closing(Store(config.db_path)) as store:
        assert provider not in credentials(store)
    assert (
        client.post(
            "/v1/providers/disconnect", headers=headers, json={"provider": "openai"}
        ).status_code
        == 400
    )


def test_three_role_prompts_save_atomically_and_harness_is_server_owned(management):
    from test_operating_policy import LIMITS

    from veyquant.model_prompts import DEFAULT_PROMPTS, harness
    from veyquant.shadow_inference import model_selection

    client, _, _, _ = management
    headers = {"origin": ORIGIN, "x-veyquant-csrf": bind(management).json()["csrf_token"]}
    prompts = {role: "판단" * 1000 for role in DEFAULT_PROMPTS}
    data = LIMITS | {"expected_revision": "0"}
    data |= {role + "_model": value for role, value in model_selection().items()}
    data |= {role + "_prompt": value for role, value in prompts.items()}
    data |= {"strategy_preset": "custom", "strategy_prompt": "전략" * 1500}
    # UTF-8 27KB of instructions exceeds the old 20KB request envelope.
    result = client.post("/v1/settings", headers=headers, json=data)
    assert result.status_code == 200
    assert result.json()["operating_policy"]["prompts"] == prompts
    assert client.post("/v1/settings", headers=headers, json=data).status_code == 409
    for extra in ({"cheap_harness": "override"}, {"harness": "override"}):
        assert client.post("/v1/settings", headers=headers, json=data | extra).status_code == 400
    incomplete = {key: value for key, value in data.items() if key != "middle_prompt"}
    assert client.post("/v1/settings", headers=headers, json=incomplete).status_code == 400
    status = client.get("/v1/status").json()
    assert status["operating_policy"]["prompts"] == prompts
    assert status["model_catalog"]["role_prompts"]["harnesses"]["cheap"] == harness("cheap")


def test_dart_failure_exposes_safe_code_and_audits_without_key(management, monkeypatch):
    client, _, _, config = management
    headers = {"origin": ORIGIN, "x-veyquant-csrf": bind(management).json()["csrf_token"]}

    async def fail(*args):
        raise ValueError("dart_rate_limited")

    monkeypatch.setattr("veyquant.management.request_connection", fail)
    key = "fixture_key_not_real_123456789"
    response = client.post(
        "/v1/providers/connect", headers=headers, json={"provider": "dart", "api_key": key}
    )
    assert response.status_code == 422
    assert response.json() == {"error": "dart_rate_limited"}
    with closing(Store(config.db_path)) as store:
        assert store.db.execute("select count(*) from provider_connections").fetchone()[0] == 0
        rows = store.rows()
        assert rows[-1]["payload"] == {"provider": "dart", "error": "dart_rate_limited"}
        assert key not in json.dumps(rows)


def test_layer_detail_route_requires_auth_and_valid_role(management, monkeypatch):
    client = management[0]
    url = "/v1/analysis/" + "a" * 32 + "/layers/middle"
    assert client.get(url).status_code == 401
    bind(management)
    calls = []

    async def exchange(socket, request, **kwargs):
        calls.append(request)
        return {"data": {"role": request["role"], "trace": []}}

    monkeypatch.setattr("veyquant.research_context.exchange", exchange)
    assert client.get(url).json()["data"]["role"] == "middle"
    assert calls == [{"operation": "layer_history", "id": "a" * 32, "role": "middle"}]
    assert client.get(url.replace("middle", "arbitrary")).status_code == 400


def test_history_page_authentication_and_cursor_forwarding(management, monkeypatch):
    client = management[0]
    assert client.get("/v1/analysis/history").status_code == 401
    bind(management)
    calls = []

    async def exchange(socket, request, **kwargs):
        calls.append(request)
        return {"records": [], "next_cursor": None}

    monkeypatch.setattr("veyquant.research_context.exchange", exchange)
    assert client.get("/v1/analysis/history?cursor=fixture").status_code == 200
    assert calls == [{"operation": "history_page", "cursor": "fixture"}]
    assert client.get("/v1/analysis/history?cursor=" + "a" * 401).status_code == 400
