import asyncio
import json
from types import SimpleNamespace

import pytest

from veyquant import collector
from veyquant import shadow_inference as inference
from veyquant.model_catalog import catalog_view
from veyquant.operating_policy import OperatingPolicy
from veyquant.store import Store


async def test_shutdown_does_not_wait_forever_for_task_that_swallows_cancellation(
    monkeypatch, capsys
):
    started, release = asyncio.Event(), asyncio.Event()

    async def stuck():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    task = asyncio.create_task(stuck(), name="fixture_stuck")
    await started.wait()

    def exit_process(code):
        assert code == 1
        raise RuntimeError("restart")

    monkeypatch.setattr(collector.os, "_exit", exit_process)
    try:
        with pytest.raises(RuntimeError, match="restart"):
            await collector.cancel_tasks([task], timeout=0.01)
        assert "fixture_stuck" in capsys.readouterr().out
    finally:
        release.set()
        await task


async def test_clean_shutdown_drains_tasks_without_process_exit(monkeypatch):
    monkeypatch.setattr(collector.os, "_exit", lambda _: pytest.fail("unexpected hard exit"))
    task = asyncio.create_task(asyncio.sleep(100))
    await collector.cancel_tasks([task], timeout=0.1)
    assert task.cancelled()


def test_token_transport_failure_is_recoverable_but_auth_failure_is_not():
    from veyquant.adapters.toss import TossError, TossHTTPError

    assert collector.recoverable_failure(TossError("toss_token_transport_failure"))
    assert not collector.recoverable_failure(TossHTTPError(401))


def test_watchdog_notification_contains_no_private_data(monkeypatch):
    calls = []

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, value):
            assert value == 1

        def sendto(self, payload, address):
            calls.append((payload, address))

    monkeypatch.setenv("NOTIFY_SOCKET", "@fixture")
    monkeypatch.setattr(collector.socket, "socket", lambda *a: Socket())
    collector.watchdog_notify()
    assert calls == [(b"WATCHDOG=1", "\0fixture")]


def test_new_model_defaults_and_legacy_credentials_remain_usable():
    models = inference.MODEL_PRESETS["chatgpt"]
    assert models == {"cheap": "gpt-6-luna", "middle": "gpt-6-luna", "research": "gpt-6-sol"}
    preset = next(p for p in catalog_view()["presets"] if p["id"] == "chatgpt")
    assert preset["reasoning"] == {"cheap": "low", "middle": "high", "research": "high"}
    assert all(m in inference.ALLOWED_MODELS for m in ("gpt-5.6-sol", "gpt-5.6-luna"))


def test_old_strategy_preset_does_not_break_policy_reads_or_change_other_settings(tmp_path):
    store = Store(str(tmp_path / "db"))
    policy = OperatingPolicy(store)
    limits = {"capital_krw": "1000000", "max_order_krw": "500000", "max_daily_loss_krw": "100000"}
    previous = policy.save(
        limits, "0", 1000, models=inference.MODEL_PRESETS["claude"], live_requested="true"
    )
    store.db.execute(
        "UPDATE operating_policy SET strategy=?",
        (json.dumps({"preset": "aggressive", "prompt": "old preset"}),),
    )
    current = policy.view()
    assert current["strategy"]["prompt"] == inference.STRATEGY_PRESETS["aggressive"]["prompt"]
    for field in ("limits", "live_requested", "models", "reasoning", "revision", "prompts"):
        assert current[field] == previous[field]
    custom = {"preset": "custom", "prompt": "owner text"}
    store.db.execute("UPDATE operating_policy SET strategy=?", (json.dumps(custom),))
    assert policy.view()["strategy"] == custom
    store.close()


def test_refresh_verifies_new_models_with_ciphertext_only(monkeypatch):
    from test_provider_connections import BLOB, KEY

    decrypts = []

    def decrypt(**kwargs):
        decrypts.append(kwargs)
        return {"Plaintext": KEY.encode()}

    monkeypatch.setenv("PROVIDER_KEY_ARN", "fixture")
    monkeypatch.setattr(
        inference,
        "kms_client",
        lambda: SimpleNamespace(
            decrypt=decrypt, encrypt=lambda **kw: {"CiphertextBlob": b"new encrypted fixture"}
        ),
    )
    monkeypatch.setattr(
        inference, "provider_request", lambda provider, key, path: {"id": path.split("/")[-1]}
    )
    event = {
        "operation": "refresh_provider",
        "provider": "openai",
        "issued_at": 1000,
        "credential": {"ciphertext": BLOB, "models": ["gpt-5.6-sol"], "verified_at": 900},
    }
    result = inference.refresh_provider(event, 1001)
    assert {"gpt-6-sol", "gpt-6-luna"}.issubset(result["models"])
    assert len(decrypts) == 1 and KEY not in json.dumps(result)
    with pytest.raises(ValueError):
        inference.refresh_provider(event, 1100)
    assert len(decrypts) == 1


def test_owner_migration_preserves_limits_live_intent_and_role_prompts(tmp_path):
    import importlib.util
    import io
    from pathlib import Path

    from test_provider_connections import BLOB

    spec = importlib.util.spec_from_file_location(
        "upgrade", Path(__file__).resolve().parents[1] / "scripts/upgrade_owner_ai.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    store = Store(str(tmp_path / "db"))
    policy = OperatingPolicy(store)
    limits = {"capital_krw": "1000000", "max_order_krw": "500000", "max_daily_loss_krw": "100000"}
    previous = policy.save(
        limits, "0", 1000, models=inference.MODEL_PRESETS["claude"], live_requested="true"
    )
    credential = {"ciphertext": BLOB, "models": ["gpt-5.6-sol"], "verified_at": 900}
    store.db.execute(
        "INSERT INTO provider_connections VALUES('openai',?)", (json.dumps(credential),)
    )
    available = credential | {"models": ["gpt-6-sol", "gpt-6-luna"], "verified_at": 1001}
    client = SimpleNamespace(
        invoke=lambda **kw: {"Payload": io.BytesIO(json.dumps(available).encode())}
    )
    result = module.migrate(store, client, "fixture-function", 1, 1001)
    assert result["model_connection"]["ready"] and result["revision"] == 2
    current = policy.view()
    for field in ("limits", "live_requested", "prompts"):
        assert current[field] == previous[field]
    assert not store.db.execute("SELECT stopped FROM controls").fetchone()[0]
    store.close()
