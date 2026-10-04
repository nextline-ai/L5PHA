import io
import json
import subprocess
import tarfile
from types import SimpleNamespace

import httpx
import pytest
from test_management import ORIGIN, bind
from test_management import management as management

from scripts.build_install_release import build
from scripts.recover_installation import recover
from veyquant.access_bot import deployment, handle, verify_deployment
from veyquant.installation import installation_view, validate_broker_input
from veyquant.store import Store


@pytest.fixture
def installation(tmp_path, monkeypatch):
    config = tmp_path / "installation.json"
    config.write_text(
        json.dumps(
            {
                "ip": "15.134.164.178",
                "deployment_id": "a" * 32,
                "bot_id": 123,
                "version": "test",
                "secret": "never exposed",
            }
        )
    )
    status = tmp_path / "status.json"
    monkeypatch.setenv("L5PHA_INSTALLATION_FILE", str(config))
    monkeypatch.setenv("L5PHA_SETUP_STATUS", str(status))
    return config, status


def test_public_metadata_never_exposes_keys_owner_or_account(management, installation):
    client, *_ = management
    result = client.get("/installation.json").json()
    assert set(result) == {"product", "deployment_id", "bot_id", "version", "origin"}
    assert client.get("/v1/status").status_code == 401
    assert installation_view()["broker_configured"] is False
    installation[1].write_text('{"broker_configured":true}')
    assert installation_view()["broker_configured"] is True


def test_broker_setup_requires_owner_csrf_and_accepts_omitted_account(
    management, installation, monkeypatch
):
    client, *_ = management
    values = {"client_id": "example-id", "client_secret": "example-secret"}
    assert (
        client.post("/v1/installation/broker", headers={"origin": ORIGIN}, json=values).status_code
        == 401
    )
    csrf = bind(management).json()["csrf_token"]
    assert (
        client.post("/v1/installation/broker", headers={"origin": ORIGIN}, json=values).status_code
        == 403
    )
    seen = []

    async def fake(data):
        seen.append(data)
        return {"configured": True}

    monkeypatch.setattr("veyquant.installation.connect_broker", fake)
    r = client.post(
        "/v1/installation/broker", headers={"origin": ORIGIN, "x-veyquant-csrf": csrf}, json=values
    )
    assert r.status_code == 200
    assert seen == [values | {"account_seq": ""}]
    assert "example-secret" not in r.text


@pytest.mark.parametrize(
    "change",
    [{"client_id": "a\nvalue"}, {"client_secret": 42}, {"account_seq": "1;reboot"}, {"extra": "x"}],
)
def test_broker_input_has_no_command_or_unknown_fields(change):
    with pytest.raises(ValueError):
        validate_broker_input(
            {"client_id": "example-id", "client_secret": "example-secret", "account_seq": ""}
            | change
        )


@pytest.mark.parametrize(
    "text",
    [
        "/start l5_127_0_0_1_" + "a" * 32,
        "/start l5_169_254_169_254_" + "a" * 32,
        "/start l5_10_0_0_1_" + "a" * 32,
        "/start https://evil.example",
        "/start l5_999_1_1_1_" + "a" * 32,
    ],
)
def test_access_bot_rejects_internal_or_unstructured_destinations(text):
    with pytest.raises(ValueError):
        deployment(text)


async def test_access_bot_only_sets_requesters_menu_after_matching_manifest():
    origin, identity = deployment("/start l5_15_134_164_178_" + "a" * 32)
    calls = []

    async def call(name, data):
        calls.append((name, data))

    def mock(request):
        assert str(request.url) == origin + "/installation.json"
        return httpx.Response(
            200,
            json={"product": "L5PHA", "deployment_id": identity, "bot_id": 123, "origin": origin},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(mock)) as http:
        update = {
            "message": {
                "chat": {"type": "private", "id": 77},
                "from": {"id": 77},
                "text": "/start l5_15_134_164_178_" + identity,
            }
        }
        await handle(
            update, http, call, bot_id=123, install_url="https://installer.example/install"
        )
        assert calls[0] == (
            "setChatMenuButton",
            {
                "chat_id": 77,
                "menu_button": {"type": "web_app", "text": "내 L5PHA", "web_app": {"url": origin}},
            },
        )
        assert all(c[1]["chat_id"] == 77 for c in calls)
        calls.clear()
        update["message"]["text"] = "example-secret-pasted-accidentally"
        await handle(
            update, http, call, bot_id=123, install_url="https://installer.example/install"
        )
        assert calls == []
        with pytest.raises(ValueError):
            await verify_deployment(http, origin, "b" * 32, 123)


async def test_initial_broker_setup_is_one_time_and_does_not_order(tmp_path, monkeypatch):
    import veyquant.setup_bridge as bridge

    credential = tmp_path / "encrypted"
    status = tmp_path / "status.json"
    monkeypatch.setattr(bridge, "CREDENTIAL", credential)
    monkeypatch.setattr(bridge, "STATUS", str(status))
    commands = []
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda args, **kwargs: commands.append(args) or SimpleNamespace(returncode=0),
    )
    saved = []

    async def verify(data):
        return "123"

    def persist(data, secret, client):
        saved.append(data)
        credential.write_text("encrypted only")

    values = {"client_id": "example-id", "client_secret": "example-secret", "account_seq": ""}
    first = await bridge.provision(
        values, client=object(), secret="secret-arn", verify_fn=verify, persist_fn=persist
    )
    assert first["configured"] is True
    assert saved[0]["account_seq"] == "123"
    assert json.loads(status.read_text()) == {"broker_configured": True}
    assert all("example-secret" not in str(c) for c in commands)
    again = await bridge.provision(
        values, client=object(), secret="secret-arn", verify_fn=verify, persist_fn=persist
    )
    assert again["configured"] is True and len(saved) == 1


def test_fresh_install_release_excludes_local_secrets_and_compiles_userdata(tmp_path):
    build(tmp_path, "https://example.s3.ap-southeast-2.amazonaws.com/test")
    with tarfile.open(
        fileobj=io.BytesIO((tmp_path / "runtime.tar.gz").read_bytes()), mode="r:gz"
    ) as tar:
        names = tar.getnames()
        assert all(not n.startswith((".env", "var/", "tests/", "docs/")) for n in names)
        assert "scripts/recover_installation.py" in names
        assert "deploy/requirements.lock" in names
    template = json.loads((tmp_path / "install.json").read_text())
    resources = template["Resources"]
    host = resources["Host"]
    assert "CreationPolicy" not in host
    assert resources["InstallationReady"]["CreationPolicy"]["ResourceSignal"]["Timeout"] == "PT25M"
    assert resources["InstallationReady"]["DependsOn"] == ["AddressAssociation", "DataAttachment"]
    assert host["Properties"]["MetadataOptions"]["HttpTokens"] == "required"
    assert resources["DataVolume"]["Properties"]["Encrypted"] is True
    assert resources["DataVolume"]["DeletionPolicy"] == "RetainExceptOnCreate"
    assert {
        r["FromPort"] for r in resources["HostSecurityGroup"]["Properties"]["SecurityGroupIngress"]
    } == {80, 443}
    script = host["Properties"]["UserData"]["Fn::Base64"]["Fn::Sub"]
    subprocess.run(["bash", "-n"], input=script.encode(), check=True)
    compile(script.split("<<'L5PHA_FETCH'\n")[1].split("\nL5PHA_FETCH")[0], "<userdata>", "exec")
    assert "TOSS_CLIENT_SECRET" not in json.dumps(template)
    assert "TELEGRAM_BOT_TOKEN" not in json.dumps(template)
    assert "get-secret-value" not in script


def test_aws_recovery_preserves_stop_across_owner_disconnect(tmp_path):
    store = Store(str(tmp_path / "state.db"))
    try:
        result = recover(store, "new-code", 123, 1000)
        assert len(result["one_time_code"]) == 43
        store.db.execute("INSERT INTO owner VALUES (1,77)")
        recover(store, "disconnect-telegram", 123, 1001)
        assert store.db.execute("SELECT stopped FROM controls").fetchone()[0] == 1
        assert store.db.execute("SELECT * FROM owner").fetchone() is None
        assert recover(store, "new-code", 123, 1002)["expires_in_seconds"] == 600
    finally:
        store.close()


def test_fresh_install_requires_direct_provider_credentials(monkeypatch):
    from veyquant.model_catalog import connection_view
    from veyquant.shadow_inference import MODEL_PRESETS, models_ready

    monkeypatch.setenv("L5PHA_DIRECT_PROVIDERS_ONLY", "true")
    assert models_ready(MODEL_PRESETS["claude"]) is False
    assert connection_view(MODEL_PRESETS["claude"])["ready"] is False
    assert "추가 구성" in connection_view(MODEL_PRESETS["claude"])["message"]


def test_failed_secret_storage_does_not_install_broker_credential(tmp_path, monkeypatch):
    from veyquant import setup_bridge as bridge

    credential = tmp_path / "toss.cred"

    def seal(command, **kwargs):
        assert "example-secret" not in str(command)
        (tmp_path / "toss.cred.next").write_bytes(b"encrypted")
        return SimpleNamespace(returncode=0)

    class Unavailable:
        def put_secret_value(self, **kwargs):
            raise RuntimeError("unavailable")

    monkeypatch.setattr(bridge.subprocess, "run", seal)
    with pytest.raises(RuntimeError):
        bridge.persist(
            {"client_id": "example-id", "client_secret": "example-secret", "account_seq": "123"},
            "test-secret",
            Unavailable(),
            credential,
        )
    assert not credential.exists()
    assert not (tmp_path / "toss.cred.next").exists()
