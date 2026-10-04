import json

import pytest

from veyquant.collector import ObservationStore, read_status


def test_latest_storage_bounded_and_status_contains_no_account_data(tmp_path):
    path = tmp_path / "status.json"
    store = ObservationStore(str(tmp_path / "db"), str(path))
    store.snapshot(
        {"items": [{"secret-holding": "private-value"}]},
        {"orders": [], "hasNext": False, "nextCursor": None},
        1000,
    )
    for _ in range(50):
        store.frame(
            {
                "type": "message",
                "topic": "personal:order:7",
                "data": {"private-order": "private-value"},
            },
            1001,
        )
    assert store.db.execute("SELECT COUNT(*) FROM latest").fetchone()[0] == 1
    store.connected = True
    store.publish(1001)
    assert "private" not in path.read_text()
    assert read_status(str(path), 1002)["connected"] is True
    assert read_status(str(path), 1012)["connected"] is False
    assert read_status(str(path), 999)["connected"] is False
    store.db.close()


def test_invalid_snapshot_preserves_last_good_account(tmp_path):
    store = ObservationStore(str(tmp_path / "db"), str(tmp_path / "status"))
    store.snapshot({"items": []}, {"orders": [], "hasNext": False, "nextCursor": None}, 1000)
    with pytest.raises(ValueError):
        store.snapshot({"items": []}, {"orders": [], "hasNext": True, "nextCursor": "opaque"}, 1100)
    assert store.snapshot_at == 1000
    assert store.db.execute("SELECT MIN(received_at) FROM account").fetchone()[0] == 1000
    store.db.close()


@pytest.mark.parametrize("field", ["snapshot_at", "last_frame_at", "frames_received", "updated_at"])
def test_status_file_cannot_forward_arbitrary_data(tmp_path, field):
    p = tmp_path / "status"
    p.write_text(
        json.dumps(
            {"updated_at": 1000, "connected": True, "frames_received": 5, field: "private-secret"}
        )
    )
    result = read_status(str(p), 1001)
    assert not result["connected"]
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("stream_fails", [False, True])
async def test_collector_closes_tasks_and_marks_gap(tmp_path, monkeypatch, stream_fails):
    import asyncio
    from contextlib import asynccontextmanager

    import veyquant.collector as module

    stop = asyncio.Event()
    closed = []

    class Response:
        text = "15.134.164.178"

        def raise_for_status(self):
            pass

    class Http:
        async def get(self, *args, **kwargs):
            return Response()

    @asynccontextmanager
    async def client():
        try:
            yield Http()
        finally:
            closed.append("http")

    class Broker:
        def __init__(self, *args):
            pass

        async def accounts(self):
            return {}

        async def holdings(self, account):
            return {"items": []}

        async def open_orders(self, account):
            return {"orders": [], "hasNext": False, "nextCursor": None}

    class Stream:
        def __init__(self, tokens, subscriptions, gap, refresh, **kwargs):
            self.refresh = refresh
            assert len(subscriptions) == 1
            assert subscriptions[0].channel == "personal:order"
            assert kwargs["max_reconnects"] == 5

        async def frames(self):
            try:
                await self.refresh()
                yield {"type": "subscriptions"}
                if stream_fails:
                    raise ValueError("fixture_invalid_stream")
                stop.set()
                await asyncio.Event().wait()
            finally:
                closed.append("stream")

    monkeypatch.setattr(module, "http_client", client)
    monkeypatch.setattr(module, "TossReadOnly", Broker)
    monkeypatch.setattr(module, "select_account", lambda *args: "7")
    monkeypatch.setattr(module, "TossStream", Stream)
    store = ObservationStore(str(tmp_path / "db"), str(tmp_path / "status"))
    try:
        call = module.collect(
            {"TOSS_CLIENT_ID": "fixture", "TOSS_CLIENT_SECRET": "fixture"},
            "15.134.164.178",
            store,
            stop,
        )
        if stream_fails:
            with pytest.raises(ValueError, match="fixture_invalid_stream"):
                await asyncio.wait_for(call, 2)
        else:
            await asyncio.wait_for(call, 2)
        assert not store.connected
        assert store.snapshot_at is not None
        assert sorted(closed) == ["http", "stream"]
        assert json.loads((tmp_path / "status").read_text())["connected"] is False
    finally:
        store.db.close()


@pytest.mark.parametrize("read_only", [False, True])
async def test_order_preparation_can_promote_stock_through_ws_reconciliation(
    tmp_path, monkeypatch, read_only
):
    import asyncio
    from contextlib import asynccontextmanager
    from datetime import datetime

    import veyquant.collector as module
    import veyquant.live_worker as execution
    import veyquant.universe as market

    stop, changed, connected = asyncio.Event(), asyncio.Event(), asyncio.Event()
    refreshed = []
    store = ObservationStore(str(tmp_path / "db"), str(tmp_path / "status"))

    class Response:
        text = "15.134.164.178"

        def raise_for_status(self):
            pass

    class Http:
        async def get(self, *args, **kwargs):
            return Response()

    @asynccontextmanager
    async def client():
        yield Http()

    class Broker:
        def __init__(self, *args):
            pass

        async def accounts(self):
            return [{"accountSeq": 7}]

        async def holdings(self, account):
            refreshed.append(1)
            return {"items": []}

        async def open_orders(self, account):
            return {"orders": [], "hasNext": False, "nextCursor": None}

    class Universe:
        def __init__(self, *args):
            self.stocks = {"000660": {"name": "fixture"}}
            self.rejected, self.watch = set(), ()

        async def scan(self, pins):
            await asyncio.Event().wait()

        def reject(self, topics):
            raise AssertionError("unexpected rejection")

    class Worker:
        def __init__(self, *args):
            self.pins = ()
            self.reads = 0

        async def reconcile(self):
            self.reads += 1
            if self.reads == 1:
                raise ValueError("reconciliation_required")
            assert connected.is_set() and store.connected
            stop.set()

        def pinned_symbols(self):
            return self.pins

        async def tick(self):
            assert not read_only, "read-only recovery cannot execute orders"
            await connected.wait()
            self.pins = ("000660",)
            await store.promote("000660")
            assert len(refreshed) == 2
            assert store.connected and "000660" in store.universe.watch
            stop.set()

        async def monitor(self):
            assert not read_only, "read-only recovery cannot cancel orders"

        def close(self):
            pass

    class Stream:
        def __init__(self, tokens, subscriptions, gap, refresh, **kwargs):
            self.gap, self.refresh = gap, refresh

        def update_subscriptions(self, subscriptions):
            assert any("000660" in s.codes for s in subscriptions)
            changed.set()

        async def frames(self):
            await self.refresh()
            yield {"type": "subscriptions"}
            connected.set()
            await changed.wait()
            await self.gap()
            await self.refresh()  # Same required sequence as TossStream on a new ack.
            yield {"type": "subscriptions"}
            now = module.time.time()
            store.quotes["000660"] = {"as_of": now}
            store.db.execute(
                "INSERT INTO latest VALUES(?,?,?)",
                (
                    "orderbook:kr:000660",
                    json.dumps({"timestamp": datetime.fromtimestamp(now).astimezone().isoformat()}),
                    now,
                ),
            )
            await asyncio.Event().wait()

    if read_only:
        monkeypatch.setenv("VEYQUANT_RECOVERY_READ_ONLY", "1")
    else:
        monkeypatch.delenv("VEYQUANT_RECOVERY_READ_ONLY", raising=False)
    monkeypatch.delenv("VEYQUANT_DECISION_V2", raising=False)
    monkeypatch.setattr(module, "http_client", client)
    monkeypatch.setattr(module, "TossReadOnly", Broker)
    monkeypatch.setattr(module, "TossStream", Stream)
    monkeypatch.setattr(market, "Universe", Universe)
    monkeypatch.setattr(execution, "LiveWorker", Worker)
    try:
        await asyncio.wait_for(
            module.collect(
                {"TOSS_CLIENT_ID": "fixture", "TOSS_CLIENT_SECRET": "fixture"},
                "15.134.164.178",
                store,
                stop,
                execution=("db", "control", "reports", "status"),
            ),
            4,
        )
        assert stop.is_set()
    finally:
        store.db.close()


@pytest.mark.parametrize("scenario", ["success", "read_failure", "event_during_read"])
async def test_account_refresh_is_read_only_and_rejects_interleaved_order_event(
    tmp_path, monkeypatch, scenario
):
    import asyncio
    from contextlib import asynccontextmanager

    from test_account_readiness import CALENDAR, CONDITIONALS, HOLDINGS, NOW, ORDERS

    import veyquant.collector as module
    from veyquant.adapters.toss import TossHTTPError

    stop = asyncio.Event()
    store = ObservationStore(
        str(tmp_path / "db"), str(tmp_path / "status"), account_path=str(tmp_path / "account.json")
    )

    class Response:
        text = "15.134.164.178"

        def raise_for_status(self):
            pass

    class Http:
        async def get(self, *args, **kwargs):
            return Response()

    @asynccontextmanager
    async def client():
        yield Http()

    class Broker:
        def __init__(self, *args):
            pass

        async def accounts(self):
            return [{"accountSeq": 7}]

        async def holdings(self, account):
            return HOLDINGS

        async def open_orders(self, account):
            return ORDERS

        async def buying_power(self, account):
            if scenario == "read_failure":
                raise TossHTTPError(429)
            if scenario == "event_during_read":
                store.frame(
                    {
                        "type": "message",
                        "topic": "personal:order:7",
                        "data": {"order": {"orderId": "new-order"}},
                    },
                    NOW,
                )
            return {"currency": "KRW", "cashBuyingPower": "500000"}

        async def conditional_orders(self, account):
            return CONDITIONALS

        async def market_calendar(self):
            return CALENDAR

    class Stream:
        def __init__(self, tokens, subscriptions, gap, refresh, **kwargs):
            self.refresh = refresh

        async def frames(self):
            await self.refresh()
            assert (store.account_view is not None) == (scenario == "success")
            if scenario == "read_failure":
                assert store.account_issue == "buying_power_429"
            if scenario == "event_during_read":
                assert store.journal.counts()["orders_needing_review"] == 1
            yield {"type": "subscriptions"}
            assert store.connected
            stop.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(module, "http_client", client)
    monkeypatch.setattr(module, "TossReadOnly", Broker)
    monkeypatch.setattr(module, "TossStream", Stream)
    monkeypatch.setattr(module.time, "time", lambda: NOW)
    try:
        await asyncio.wait_for(
            module.collect(
                {"TOSS_CLIENT_ID": "fixture", "TOSS_CLIENT_SECRET": "fixture"},
                "15.134.164.178",
                store,
                stop,
            ),
            2,
        )
        assert not store.connected
        assert json.loads((tmp_path / "account.json").read_text())["available"] is False
    finally:
        store.db.close()
