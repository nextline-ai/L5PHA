import asyncio
import json
from contextlib import aclosing

import httpx
import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from veyquant.adapters.toss import (
    REST_URL,
    WS_URL,
    Credentials,
    Subscription,
    SubscriptionRejected,
    TokenManager,
    TossError,
    TossHTTPError,
    TossReadOnly,
    TossStream,
    declaration,
    parse_frame,
)


async def test_token_singleflight_expiry_and_rest_contract():
    requests = []
    now = [1000]

    def handle(request):
        requests.append(request)
        assert str(request.url).startswith(REST_URL)
        if request.url.path == "/oauth2/token":
            assert request.method == "POST"
            assert request.headers["content-type"] == "application/x-www-form-urlencoded"
            assert b"grant_type=client_credentials" in request.content
            return httpx.Response(
                200,
                json={"access_token": "test-only-token", "token_type": "Bearer", "expires_in": 100},
            )
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer test-only-token"
        if request.url.path in {"/api/v1/holdings", "/api/v1/orders"}:
            assert request.headers["X-Tossinvest-Account"] == "7"
        if request.url.path == "/api/v1/orders":
            assert dict(request.url.params) == {"status": "OPEN"}
        return httpx.Response(200, json={"result": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        tokens = TokenManager(Credentials("fixture", "fixture-secret"), http, lambda: now[0])
        assert len(set(await asyncio.gather(*(tokens.get() for _ in range(10))))) == 1
        assert len(requests) == 1
        client = TossReadOnly(tokens, http)
        await client.accounts()
        await client.prices(("AAPL", "0101N0"))
        await client.holdings("7")
        await client.open_orders("7")
        assert len([r for r in requests if r.method == "POST"]) == 1
        now[0] += 101
        await tokens.get()
        assert len([r for r in requests if r.method == "POST"]) == 2


async def test_queued_read_uses_token_rotated_while_waiting():
    now, issued, sent = [1000], [], []

    def handle(request):
        if request.url.path == "/oauth2/token":
            issued.append(1)
            return httpx.Response(
                200,
                json={
                    "access_token": "fixture-" + str(len(issued)),
                    "token_type": "Bearer",
                    "expires_in": 100,
                },
            )
        sent.append(request.headers["authorization"])
        return httpx.Response(200, json={"result": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        tokens = TokenManager(Credentials("fixture", "fixture"), http, lambda: now[0])
        await tokens.get()
        broker = TossReadOnly(tokens, http)
        lock = broker._read_locks["market"] = asyncio.Lock()
        await lock.acquire()
        pending = asyncio.create_task(broker.prices(("005930",)))
        await asyncio.sleep(0)
        now[0] += 101
        await tokens.get()  # A different REST/WS task rotates the single valid token.
        lock.release()
        await pending
        assert sent == ["Bearer fixture-2"]


async def test_read_throttle_honors_reset_and_preserves_learned_pace(monkeypatch):
    from types import SimpleNamespace

    import veyquant.adapters.toss as module

    now, sent = [1000.0], []

    async def sleep(delay):
        now[0] += delay

    class Tokens:
        async def get(self):
            return "fixture"

    def handle(request):
        sent.append(now[0])
        if len(sent) == 1:
            return httpx.Response(
                429,
                headers={
                    "X-RateLimit-Limit": "20",
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": "120",
                    "Retry-After": "61",
                },
            )
        return httpx.Response(
            200,
            json={"result": []},
            headers={
                "X-RateLimit-Limit": "20",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "2",
            },
        )

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        broker = TossReadOnly(Tokens(), http)
        with pytest.raises(TossHTTPError) as error:
            await broker._get_once("/api/v1/candles")
        assert error.value.retry_after == 120
        await broker.minute_candles("005930")
        await broker.daily_candles("000660")
        assert sent == [1000, 1120, 1122]
        assert broker.read_limits["candles"]["paced_per_second"] == 12.5
        assert broker.read_limits["candles"]["throttled"] == 1
        for _ in range(18):
            await broker.daily_candles("005930")
        assert broker.read_limits["candles"]["paced_per_second"] == 12.5
        assert all(
            b - a >= 2 for a, b in zip(sent[1:-1], sent[2:], strict=True)
        )  # Reset always wins over recovery.


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_http_failures_are_not_empty_data(status):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, json={"private": "must-not-leak"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        tokens = TokenManager(Credentials("test", "secret"), http)
        with pytest.raises(TossHTTPError) as error:
            await tokens.get()
        assert str(error.value) == f"toss_http_{status}"
        assert len(requests) == 1


def test_declaration_is_full_replace_and_bounded():
    subs = (Subscription("trade:us", ("AAPL",)), Subscription("personal:order", ("7",)))
    assert json.loads(declaration(subs)) == [
        {"type": "trade:us", "codes": ["AAPL"]},
        {"type": "personal:order", "codes": ["7"]},
    ]
    assert declaration(()) == "[]"
    with pytest.raises(ValueError):
        declaration(subs + subs)
    with pytest.raises(ValueError):
        declaration((Subscription("trade:us", tuple(f"X{i}" for i in range(101))),))
    with pytest.raises(ValueError):
        Subscription("personal:order", ("AAPL",))


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        "[]",
        '{"type":"message"}',
        '{"type":"subscriptions","rejected":[]}',
        '{"type":"error"}',
        "not-json",
    ],
)
def test_invalid_frames(raw):
    with pytest.raises(TossError, match="invalid_websocket_frame"):
        parse_frame(raw)


async def test_redirect_cannot_forward_credentials():
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(307, headers={"Location": "https://example.invalid/collect"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), follow_redirects=True
    ) as http:
        with pytest.raises(TossHTTPError) as error:
            await TokenManager(Credentials("fixture", "fixture"), http).get()
        assert error.value.status == 307
    assert calls == [REST_URL + "/oauth2/token"]


class FakeTokens:
    async def get(self):
        return "fixture-access"


class Socket:
    def __init__(self, frames, events):
        self.incoming = iter(frames)
        self.sent = []
        self.events = events
        self.closed = False

    async def __aenter__(self):
        self.events.append("connect")
        return self

    async def __aexit__(self, *args):
        self.closed = True
        self.events.append("close")

    async def close(self):
        self.closed = True

    async def send(self, raw):
        self.sent.append(raw)

    async def recv(self):
        return json.dumps(next(self.incoming))

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = next(self.incoming, None)
        if value is None:
            raise StopAsyncIteration
        return json.dumps(value)


ACK = {"type": "subscriptions", "subscribed": ["trade:us:AAPL"], "rejected": []}


async def test_stream_reconnect_closes_then_redeclares_and_reconciles():
    events, sleeps, sockets = [], [], []

    async def gap():
        events.append("gap")

    async def reconcile():
        events.append("REST")

    async def sleep(delay):
        sleeps.append(delay)

    def connector(url, **kwargs):
        assert url == WS_URL
        assert kwargs["additional_headers"] == {"Authorization": "Bearer fixture-access"}
        assert kwargs["proxy"] is None
        if sockets:
            assert sockets[-1].closed
        s = Socket(
            [
                ACK,
                {
                    "type": "message",
                    "topic": "trade:us:AAPL",
                    "data": {"price": "123", "currency": "USD"},
                },
                {"type": "error", "error": {"code": "server-shutdown"}},
            ],
            events,
        )
        sockets.append(s)
        return s

    stream = TossStream(
        FakeTokens(),
        (Subscription("trade:us", ("AAPL",)),),
        gap,
        reconcile,
        connector=connector,
        sleep=sleep,
        jitter=lambda: 0,
        max_reconnects=1,
    )
    received = []
    with pytest.raises(TossError, match="reconnect_budget_exhausted"):
        async for frame in stream.frames():
            received.append(frame)
    assert len(received) == 4
    assert sleeps == [1]
    assert events.count("REST") == 2
    assert all(s.closed and len(s.sent) == 1 for s in sockets)
    assert events.index("REST") > events.index("connect")


async def test_partial_ack_is_not_silently_successful():
    events = []
    s = Socket(
        [ACK | {"rejected": [{"target": "trade:us:NOPE", "code": "stock-not-found"}]}], events
    )

    async def gap():
        events.append("gap")

    async def reconcile():
        pytest.fail("partial ack must not reconcile")

    stream = TossStream(
        FakeTokens(),
        (Subscription("trade:us", ("AAPL",)),),
        gap,
        reconcile,
        connector=lambda *a, **k: s,
    )
    with pytest.raises(SubscriptionRejected):
        await anext(stream.frames())
    assert s.closed


async def test_dynamic_domestic_full_replace_keeps_personal_orders_without_new_oauth():
    events, tokens_called = [], []

    class Tokens:
        async def get(self):
            tokens_called.append(1)
            return "fixture"

    class DynamicSocket(Socket):
        def __init__(self):
            super().__init__([], events)
            self.queue = asyncio.Queue()

        async def send(self, raw):
            self.sent.append(raw)
            rows = json.loads(raw)
            await self.queue.put(
                {
                    "type": "subscriptions",
                    "rejected": [],
                    "subscribed": [f"{r['type']}:{s}" for r in rows for s in r["codes"]],
                }
            )

        async def recv(self):
            return json.dumps(await self.queue.get())

        async def __anext__(self):
            return await self.recv()

    socket = DynamicSocket()

    async def gap():
        events.append("gap")

    async def reconcile():
        events.append("REST")

    personal = Subscription("personal:order", ("7",))
    stream = TossStream(Tokens(), (personal,), gap, reconcile, connector=lambda *a, **k: socket)
    async with aclosing(stream.frames()) as frames:
        assert (await anext(frames))["subscribed"] == ["personal:order:7"]
        stream.update_subscriptions((Subscription("trade:kr", ("0101N0", "000660")), personal))
        second = await asyncio.wait_for(anext(frames), 2)
        assert set(second["subscribed"]) == {
            "trade:kr:0101N0",
            "trade:kr:000660",
            "personal:order:7",
        }
        assert events.count("REST") == 2 and len(tokens_called) == 1
        await socket.queue.put(
            {"type": "message", "topic": "personal:order:7", "data": {"status": "FILLED"}}
        )
        assert (await anext(frames))["topic"] == "personal:order:7"
    assert socket.closed


async def test_partial_domestic_rejection_is_removed_but_personal_rejection_remains_fatal():
    rejected = []

    async def noop():
        pass

    subs = (Subscription("trade:kr", ("000660", "0101N0")), Subscription("personal:order", ("7",)))
    stream = TossStream(
        FakeTokens(), subs, noop, noop, on_rejected=lambda keys: rejected.extend(keys)
    )
    accepted = stream._accept(
        {
            "type": "subscriptions",
            "subscribed": ["trade:kr:000660", "personal:order:7"],
            "rejected": [{"target": "trade:kr:0101N0", "code": "stock-not-found"}],
        },
        {"trade:kr:000660", "trade:kr:0101N0", "personal:order:7"},
    )
    assert accepted == {"trade:kr:000660", "personal:order:7"}
    assert rejected == ["trade:kr:0101N0"] and "0101N0" not in stream._declaration
    with pytest.raises(SubscriptionRejected):
        stream._accept(
            {
                "type": "subscriptions",
                "subscribed": [],
                "rejected": [{"target": "personal:order:7"}],
            },
            {"personal:order:7"},
        )


async def test_catalogue_update_during_handshake_cannot_mix_old_expected_and_new_declaration():
    async def noop():
        pass

    class ChangingSocket(Socket):
        async def __aenter__(self):
            stream.update_subscriptions((Subscription("trade:kr", ("000660",)),))
            return self

    socket = ChangingSocket([ACK], [])
    stream = TossStream(
        FakeTokens(),
        (Subscription("trade:us", ("AAPL",)),),
        noop,
        noop,
        connector=lambda *a, **k: socket,
    )
    async with aclosing(stream.frames()) as frames:
        assert (await anext(frames))["subscribed"] == ["trade:us:AAPL"]
        assert json.loads(socket.sent[0]) == [{"type": "trade:us", "codes": ["AAPL"]}]
        assert "000660" in stream._declaration


@pytest.mark.parametrize("status", [401, 403])
async def test_ws_auth_failures_do_not_reconnect(status):
    calls = []

    def connector(*args, **kwargs):
        calls.append(1)
        raise InvalidStatus(Response(status, "Denied", Headers()))

    async def noop():
        pass

    stream = TossStream(FakeTokens(), (), noop, noop, connector=connector)
    with pytest.raises(TossHTTPError) as error:
        await anext(stream.frames())
    assert error.value.status == status
    assert calls == [1]


async def test_closing_consumer_closes_socket():
    s = Socket([ACK], [])

    async def noop():
        pass

    stream = TossStream(
        FakeTokens(),
        (Subscription("trade:us", ("AAPL",)),),
        noop,
        noop,
        connector=lambda *a, **k: s,
    )
    async with aclosing(stream.frames()) as frames:
        assert (await anext(frames))["type"] == "subscriptions"
    assert s.closed


async def test_missing_pong_marks_gap_and_closes(monkeypatch):
    s = Socket([], [])
    gaps = []

    async def gap():
        gaps.append(1)

    async def noop():
        pass

    async def immediate_sleep(_):
        pass

    async def timeout(awaitable, timeout):
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr("veyquant.adapters.toss.asyncio.sleep", immediate_sleep)
    monkeypatch.setattr("veyquant.adapters.toss.asyncio.wait_for", timeout)
    stream = TossStream(FakeTokens(), (), gap, noop)
    await stream._heartbeat(s, asyncio.Event())
    assert s.sent == ["PING"] and s.closed and gaps == [1]


async def test_readiness_uses_get_only_and_pages_all_open_conditionals():
    seen = []

    def handle(request):
        assert request.method == "GET"
        seen.append(request.url.path)
        if request.url.path != "/api/v1/market-calendar/KR":
            assert request.headers["X-Tossinvest-Account"] == "7"
        if request.url.path == "/api/v1/conditional-orders":
            assert request.url.params["status"] == "OPEN"
            assert request.url.params["limit"] == "100"
            more = "cursor" not in request.url.params
            if not more:
                assert request.url.params["cursor"] == "next-page"
            return httpx.Response(
                200,
                json={
                    "result": {
                        "conditionalOrders": [{"fixture": len(seen)}],
                        "hasNext": more,
                        "nextCursor": "next-page" if more else None,
                    }
                },
            )
        if request.url.path == "/api/v1/buying-power":
            assert dict(request.url.params) == {"currency": "KRW"}
        return httpx.Response(200, json={"result": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = TossReadOnly(FakeTokens(), http)
        result = await client.conditional_orders("7")
        assert len(result["conditionalOrders"]) == 2
        assert result["hasNext"] is False
        await client.buying_power("7")
        await client.order("7", "fixture-order")
        await client.market_calendar()
        with pytest.raises(ValueError):
            await client.order("7", "../other")
    assert len(seen) == 5


@pytest.mark.parametrize(
    "page",
    [
        {"conditionalOrders": [], "hasNext": True, "nextCursor": None},
        {"conditionalOrders": [], "hasNext": False, "nextCursor": "next"},
        {"conditionalOrders": [], "hasNext": True, "nextCursor": "repeated"},
    ],
)
async def test_incomplete_conditionals_never_look_like_empty_orders(page):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"result": page}))
    ) as http:
        with pytest.raises(TossError, match="incomplete_conditional_orders"):
            await TossReadOnly(FakeTokens(), http).conditional_orders("7")


@pytest.mark.parametrize("path", ["/api/v1/prices", "/api/v1/price-limits", "/api/v1/holdings"])
async def test_every_read_retries_throttles_with_shared_backoff(monkeypatch, path):
    from types import SimpleNamespace

    import veyquant.adapters.toss as module

    now, sent = [1000.0], []

    async def sleep(delay):
        now[0] += delay

    def handle(request):
        sent.append(now[0])
        return httpx.Response(
            429 if len(sent) < 3 else 200,
            headers={"Retry-After": "4", "X-RateLimit-Reset": "5"},
            json={"result": [], "error": {"code": "rate-limit-exceeded", "message": "private"}},
        )

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    monkeypatch.setattr(module.random, "random", lambda: 0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        broker = TossReadOnly(FakeTokens(), http)
        assert await broker._get(path) == []
        assert sent == [1000, 1005, 1010]
        diagnostics = json.dumps(broker.read_limits)
        assert "rate-limit-exceeded" in diagnostics and "private" not in diagnostics


@pytest.mark.parametrize("status,attempts", [(429, 3), (401, 1), (403, 1), (500, 1)])
async def test_read_retry_is_bounded_and_preserves_failures(monkeypatch, status, attempts):
    sent = []

    async def sleep(_):
        pass

    def handle(request):
        sent.append(request.method)
        return httpx.Response(status)

    monkeypatch.setattr("veyquant.adapters.toss.asyncio.sleep", sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        broker = TossReadOnly(FakeTokens(), http)
        with pytest.raises(TossHTTPError) as error:
            await broker.prices(("005930",))
        assert error.value.status == status
        assert sent == ["GET"] * attempts


async def test_cancelled_inflight_read_keeps_reserved_capacity():
    entered = asyncio.Event()

    async def handle(request):
        entered.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        broker = TossReadOnly(FakeTokens(), http)
        pending = asyncio.create_task(broker.prices(("005930",)))
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert broker._read_next["market"] > 0
        assert not broker._read_locks["market"].locked()


async def test_slow_learned_rate_recovers_after_quiet_window(monkeypatch):
    from types import SimpleNamespace

    import veyquant.adapters.toss as module

    now = [1000.0]

    async def sleep(delay):
        now[0] += delay

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"result": []}, headers={"X-RateLimit-Limit": "15"})
        )
    ) as http:
        broker = TossReadOnly(FakeTokens(), http)
        broker._read_rates["market"] = 0.5
        broker._read_adjusted["market"] = 970
        await broker.prices(("005930",))
        assert broker.read_limits["market"]["paced_per_second"] == 0.5
        now[0] = 1031
        await broker.prices(("005930",))
        assert broker.read_limits["market"]["paced_per_second"] == 7.5


@pytest.mark.parametrize(
    "invalid_topic,expired",
    [("trade:kr:999999", False), ("personal:order:8", False), ("trade:kr:005930", True)],
)
async def test_rotation_discards_only_declared_transition_market_frames(
    invalid_topic, expired, monkeypatch
):
    from types import SimpleNamespace

    import veyquant.adapters.toss as module

    now = [1000.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    personal = Subscription("personal:order", ("7",))
    old, new = "trade:kr:005930", "trade:kr:000660"
    events = []

    class RotatingSocket(Socket):
        def __init__(self):
            super().__init__([], events)
            self.queue = asyncio.Queue()

        async def send(self, raw):
            self.sent.append(raw)
            rows = json.loads(raw)
            if len(self.sent) == 2:
                await self.queue.put({"type": "message", "topic": new, "data": {}})
            await self.queue.put(
                {
                    "type": "subscriptions",
                    "rejected": [],
                    "subscribed": [f"{r['type']}:{s}" for r in rows for s in r["codes"]],
                }
            )
            if len(self.sent) == 2:
                for topic in (old, "personal:order:7", new, invalid_topic):
                    await self.queue.put({"type": "message", "topic": topic, "data": {}})

        async def recv(self):
            return json.dumps(await self.queue.get())

        async def __anext__(self):
            return await self.recv()

    async def noop():
        pass

    sock = RotatingSocket()
    stream = TossStream(
        FakeTokens(),
        (Subscription("trade:kr", ("005930",)), personal),
        noop,
        noop,
        connector=lambda *a, **k: sock,
    )
    async with aclosing(stream.frames()) as frames:
        await anext(frames)
        stream.update_subscriptions((Subscription("trade:kr", ("000660",)), personal))
        assert (await asyncio.wait_for(anext(frames), 2))["type"] == "subscriptions"
        assert (await anext(frames))["topic"] == "personal:order:7"
        assert (await anext(frames))["topic"] == new
        if expired:
            now[0] += 31
        with pytest.raises(TossError, match="unexpected_topic"):
            await anext(frames)


async def test_upstream_429_with_available_tokens_does_not_collapse_shared_rate(monkeypatch):
    from types import SimpleNamespace

    import veyquant.adapters.toss as module

    now, calls = [1000.0], []

    async def sleep(delay):
        now[0] += delay

    def handle(request):
        calls.append((request.url.path, now[0]))
        return httpx.Response(
            429 if len(calls) <= 3 else 200,
            headers={
                "X-RateLimit-Limit": "15",
                "X-RateLimit-Remaining": "14",
                "X-RateLimit-Reset": "1",
            },
            json={"result": [], "error": {"code": "rate-limit-exceeded"}},
        )

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        broker = TossReadOnly(FakeTokens(), http)
        with pytest.raises(TossHTTPError):
            await broker.orderbook("005930")
        assert broker.read_limits["market"]["paced_per_second"] == 15
        assert broker.read_limits["market"]["last_throttle"]["scope"] == "upstream"
        assert await broker.prices(("005930",)) == []
        assert all(b[1] - a[1] >= 1 for a, b in zip(calls, calls[1:], strict=False))
