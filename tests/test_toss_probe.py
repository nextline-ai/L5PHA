import json

import httpx
import pytest

from veyquant.adapters.toss import TossError
from veyquant.toss_probe import probe_toss, select_account, validate_market_frame, validate_snapshot

VALUES = {
    "TOSS_CLIENT_ID": "private-client",
    "TOSS_CLIENT_SECRET": "private-secret",
    "TOSS_ACCOUNT_SEQ": "7",
}
IP = "192.0.2.1"


@pytest.fixture
def transport():
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.host == "checkip.amazonaws.com":
            return httpx.Response(200, text=IP)
        if request.url.path == "/oauth2/token":
            assert request.method == "POST"
            return httpx.Response(
                200,
                json={"access_token": "private-access", "token_type": "Bearer", "expires_in": 3600},
            )
        assert request.method == "GET"
        values = {
            "/api/v1/accounts": [{"accountSeq": 7, "accountNo": "private-account"}],
            "/api/v1/holdings": {"items": [{"private-holding": "private-value"}]},
            "/api/v1/orders": {
                "orders": [{"orderId": "private-order"}],
                "hasNext": False,
                "nextCursor": None,
            },
            "/api/v1/prices": [{"symbol": "005930"}, {"symbol": "AAPL"}],
        }
        return httpx.Response(200, json={"result": values[request.url.path]})

    return httpx.MockTransport(handle), calls


async def test_probe_shares_token_refreshes_after_each_ack_and_redacts(transport):
    mock, calls = transport
    lifecycle = []

    class Stream:
        def __init__(self, tokens, subscriptions, gap, reconcile, **kwargs):
            assert len(subscriptions) == 5 and kwargs["max_reconnects"] == 0
            self.tokens, self.gap, self.reconcile = tokens, gap, reconcile

        async def frames(self):
            if lifecycle:
                assert lifecycle[-1] == "close"
            lifecycle.append("open")
            assert await self.tokens.get() == "private-access"
            await self.gap()
            await self.reconcile()
            try:
                yield {"type": "subscriptions"}
                yield {"type": "pong"}
                yield {
                    "type": "message",
                    "topic": "trade:kr:005930",
                    "data": {
                        "price": "100",
                        "volume": "1",
                        "timestamp": "2026-09-09T14:00:00+09:00",
                        "currency": "KRW",
                    },
                }
                yield {
                    "type": "message",
                    "topic": "personal:order:7",
                    "data": {"order": "private-event"},
                }
            finally:
                lifecycle.append("close")
                await self.gap()

    async with httpx.AsyncClient(transport=mock) as client:
        report = await probe_toss(VALUES, IP, seconds=1, http=client, stream_factory=Stream)
    assert report["status"] == "passed"
    assert report["checks"]["rest_refreshes"] == 2
    assert report["websocket"]["acknowledgements"] == 2
    assert report["websocket"]["messages_by_channel"]["personal:order"] == 2
    assert report["observation_stale"] is True and report["full_account_reconciled"] is False
    assert "private-" not in json.dumps(report)
    assert len([r for r in calls if r.method == "POST"]) == 1
    assert lifecycle == ["open", "close", "open", "close"]


async def test_wrong_egress_never_sends_credentials(transport):
    mock, calls = transport
    async with httpx.AsyncClient(transport=mock) as client:
        report = await probe_toss(VALUES, "192.0.2.2", http=client)
    assert report["status"] == "blocked"
    assert report["error"]["reason"] == "unexpected_egress_ip"
    assert len(calls) == 1


async def test_ack_alone_does_not_claim_heartbeat_verified(transport):
    mock, _ = transport

    class NoHeartbeat:
        def __init__(self, tokens, subscriptions, gap, reconcile, **kwargs):
            self.reconcile = reconcile

        async def frames(self):
            await self.reconcile()
            yield {"type": "subscriptions"}

    async with httpx.AsyncClient(transport=mock) as client:
        report = await probe_toss(VALUES, IP, http=client, stream_factory=NoHeartbeat)
    assert report["status"] == "partial"
    assert report["checks"]["heartbeat"] is False
    assert report["websocket"]["acknowledgements"] == 2


async def test_transport_exception_text_does_not_leak(transport):
    mock, _ = transport

    def broken(*args, **kwargs):
        raise TossError("private-secret private-account")

    async with httpx.AsyncClient(transport=mock) as client:
        report = await probe_toss(VALUES, IP, http=client, stream_factory=broken)
    assert report["status"] == "blocked"
    assert "private-" not in json.dumps(report)


@pytest.mark.parametrize(
    "accounts,configured",
    [
        ([], ""),
        ([{"accountSeq": 1}, {"accountSeq": 2}], ""),
        ([{"accountSeq": 7}], "12345678901"),
        ([{"accountSeq": True}], "True"),
    ],
)
def test_account_selection_is_unambiguous(accounts, configured):
    with pytest.raises(ValueError):
        select_account(accounts, configured)


def test_account_auto_selection_only_when_single_account():
    assert select_account([{"accountSeq": 7}], "") == "7"


def test_open_orders_pagination_is_not_silently_ignored():
    with pytest.raises(ValueError, match="incomplete_open_orders"):
        validate_snapshot(
            {"items": []}, {"orders": [], "hasNext": True, "nextCursor": "private-cursor"}
        )


@pytest.mark.parametrize("price", ["NaN", "Infinity", "-1", 100])
def test_market_payload_rejects_nonfinite_or_invalid_price(price):
    with pytest.raises(ValueError):
        validate_market_frame(
            "trade:kr",
            {
                "price": price,
                "volume": "1",
                "timestamp": "2026-09-09T14:00:00+09:00",
                "currency": "KRW",
            },
        )
