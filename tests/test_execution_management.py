import json
from contextlib import closing

from starlette.testclient import TestClient
from test_live_worker import NOW

from veyquant.management import ManagementConfig, create_app
from veyquant.operating_policy import OperatingPolicy
from veyquant.shadow_inference import model_selection
from veyquant.store import Store
from veyquant.telegram_auth import OwnerAuth, TelegramVerifier, digest


def test_live_status_requires_matching_revision_and_commands_require_owner_csrf(tmp_path):
    path = str(tmp_path / "management.db")
    status = tmp_path / "execution.json"
    control = tmp_path / "control.json"
    with closing(Store(path)) as store:
        OwnerAuth(store, TelegramVerifier(123))
        store.db.execute("INSERT INTO owner VALUES (1,123)")
        store.db.execute(
            "INSERT INTO sessions VALUES (?,?,?,?)", (digest("x" * 43), 123, NOW + 1000, NOW + 2000)
        )
        OperatingPolicy(store).save(
            {"capital_krw": "1000", "max_order_krw": "500", "max_daily_loss_krw": "100"},
            "0",
            NOW - 10,
            models=model_selection(),
        )
    order = {"id": "vq" + "a" * 32, "state": "UNKNOWN", "created_at": NOW - 90000}
    view = {
        "updated_at": NOW,
        "state": "ready",
        "ready": True,
        "live_enabled": True,
        "orders": [order],
        "settings_revision": 1,
    }
    status.write_text(json.dumps(view))
    origin = "https://investor.example"
    config = ManagementConfig(
        path, origin, 123, execution_control_path=str(control), execution_status_path=str(status)
    )
    with TestClient(create_app(config, clock=lambda: NOW), base_url=origin) as client:
        assert client.get("/v1/status").status_code == 401
        client.cookies.set("__Host-veyquant_session", "x" * 43)
        first = client.get("/v1/status").json()
        assert first["live_order_supported"]
        assert not first["operating_policy"]["live_enabled"]
        headers = {"Origin": origin, "X-Veyquant-CSRF": first["csrf_token"]}
        enable = {"enabled": "true", "expected_revision": "1"}
        assert (
            client.post("/v1/live-preference", json=enable, headers={"Origin": origin}).status_code
            == 403
        )
        assert client.post("/v1/live-preference", json=enable, headers=headers).status_code == 200
        assert not client.get("/v1/status").json()["operating_policy"]["live_enabled"]
        status.write_text(json.dumps(view | {"settings_revision": 2}))
        assert client.get("/v1/status").json()["operating_policy"]["live_enabled"]
        command = {
            "action": "attach",
            "order_id": order["id"],
            "broker_id": "broker-test",
            "confirmation": "confirmed_in_toss",
            "expected_revision": "2",
        }
        assert (
            client.post("/v1/execution-command", json=command, headers=headers).status_code == 409
        )
        client.post(
            "/v1/live-preference",
            json={"enabled": "false", "expected_revision": "2"},
            headers=headers,
        )
        command["expected_revision"] = "3"
        assert (
            client.post(
                "/v1/execution-command", json=command, headers={"Origin": origin}
            ).status_code
            == 403
        )
        result = client.post("/v1/execution-command", json=command, headers=headers)
        assert result.status_code == 200
        exported = json.loads(control.read_text())
        assert exported["commands"][0]["broker_id"] == "broker-test"
        assert exported["policy"]["live_requested"] is False
        assert "provider_credentials" not in exported
        assert client.post("/v1/control/stop", json={}, headers=headers).status_code == 200
        assert json.loads(control.read_text())["stopped"]
        status.write_text(json.dumps(view | {"updated_at": NOW - 16}))
        assert not client.get("/v1/status").json()["execution"]["available"]
    assert json.loads(control.read_text())["owner_bound"] is False


def test_command_queue_acknowledgments_are_kept_until_management_observes_them(tmp_path):
    path = str(tmp_path / "queue.db")
    status = tmp_path / "execution.json"
    control = tmp_path / "control.json"
    with closing(Store(path)) as store:
        OwnerAuth(store, TelegramVerifier(123))
        store.db.execute("INSERT INTO owner VALUES (1,123)")
        store.db.execute(
            "INSERT INTO sessions VALUES (?,?,?,?)", (digest("x" * 43), 123, NOW + 1000, NOW + 2000)
        )
        OperatingPolicy(store).save(
            {"capital_krw": "1000", "max_order_krw": "500", "max_daily_loss_krw": "100"},
            "0",
            NOW - 10,
            models=model_selection(),
        )
    order = {"id": "vq" + "a" * 32, "state": "UNKNOWN", "created_at": NOW - 90000}
    view = {
        "updated_at": NOW,
        "state": "order_review",
        "ready": False,
        "live_enabled": False,
        "orders": [order],
        "settings_revision": 1,
    }
    status.write_text(json.dumps(view))
    origin = "https://investor.example"
    config = ManagementConfig(
        path, origin, 123, execution_control_path=str(control), execution_status_path=str(status)
    )
    with TestClient(create_app(config, clock=lambda: NOW), base_url=origin) as client:
        client.cookies.set("__Host-veyquant_session", "x" * 43)
        headers = {
            "Origin": origin,
            "X-Veyquant-CSRF": client.get("/v1/status").json()["csrf_token"],
        }
        command = {
            "action": "confirm_absent",
            "order_id": order["id"],
            "confirmation": "confirmed_in_toss",
            "expected_revision": "1",
        }
        ids = [
            client.post("/v1/execution-command", json=command, headers=headers).json()["request_id"]
            for _ in range(30)
        ]
        assert (
            client.post("/v1/execution-command", json=command, headers=headers).status_code == 429
        )
        assert len(json.loads(control.read_text())["commands"]) == 30
        status.write_text(
            json.dumps(view | {"commands": [{"id": i, "result": "not_applicable"} for i in ids]})
        )
        client.post("/v1/control/stop", json={}, headers=headers)
        assert json.loads(control.read_text())["commands"] == []
        result = client.get("/v1/status").json()["execution"]["command_results"]
        assert {r["id"] for r in result} == set(ids)
        assert (
            client.post("/v1/execution-command", json=command, headers=headers).status_code == 200
        )


def test_manual_analysis_requires_owner_csrf_and_validates_payload(tmp_path, monkeypatch):
    import veyquant.research_context

    requests = []

    async def exchange(path, data, **kwargs):
        requests.append(data)
        return {"accepted": True, "id": "a" * 32}

    monkeypatch.setattr(veyquant.research_context, "exchange", exchange)
    path, origin = str(tmp_path / "management.db"), "https://investor.example"
    with closing(Store(path)) as store:
        OwnerAuth(store, TelegramVerifier(123))
        store.db.execute("INSERT INTO owner VALUES (1,123)")
        store.db.execute(
            "INSERT INTO sessions VALUES (?,?,?,?)", (digest("x" * 43), 123, NOW + 1000, NOW + 2000)
        )
    with TestClient(
        create_app(ManagementConfig(path, origin, 123), clock=lambda: NOW), base_url=origin
    ) as client:
        payload = {"instruction": "검토 제안", "request_id": "b" * 32}
        assert client.get("/v1/analysis/" + "a" * 32).status_code == 401
        assert client.post(
            "/v1/analysis/manual", json=payload, headers={"Origin": origin}
        ).status_code in {401, 403}
        client.cookies.set("__Host-veyquant_session", "x" * 43)
        csrf = client.get("/v1/status").json()["csrf_token"]
        assert (
            client.post("/v1/analysis/manual", json=payload, headers={"Origin": origin}).status_code
            == 403
        )
        headers = {"Origin": origin, "X-Veyquant-CSRF": csrf}
        for instruction in ["", " ", "x" * 3001]:
            assert (
                client.post(
                    "/v1/analysis/manual",
                    json=payload | {"instruction": instruction},
                    headers=headers,
                ).status_code
                == 400
            )
        assert not requests
        assert client.post("/v1/analysis/manual", json=payload, headers=headers).status_code == 202
        assert requests == [{"operation": "manual", **payload}]
        retry_url = "/v1/analysis/" + "a" * 32 + "/retry-input-limit"
        retry = {"request_id": "c" * 32}
        assert client.post(retry_url, json=retry, headers={"Origin": origin}).status_code == 403
        assert (
            client.post(
                retry_url, json=retry | {"input_limit_override": True}, headers=headers
            ).status_code
            == 400
        )
        assert client.post(retry_url, json=retry, headers=headers).status_code == 202
        assert requests[-1] == {"operation": "retry_input_limit", "id": "a" * 32, **retry}
