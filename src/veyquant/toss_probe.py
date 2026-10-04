"""Bounded read-only integration check. Reports never contain account data or keys."""

import asyncio
import ipaddress
from contextlib import aclosing
from datetime import UTC, datetime
from decimal import Decimal

from veyquant.adapters.toss import (
    Credentials,
    Subscription,
    TokenManager,
    TossError,
    TossHTTPError,
    TossReadOnly,
    TossStream,
    http_client,
)


def select_account(accounts, configured: str) -> str:
    if not isinstance(accounts, list) or any(
        not isinstance(a, dict) or type(a.get("accountSeq")) is not int for a in accounts
    ):
        raise ValueError("invalid_accounts_schema")
    candidates = [str(a["accountSeq"]) for a in accounts]
    if configured:
        if configured not in candidates:
            raise ValueError("configured_account_not_found")
        return configured
    if len(candidates) != 1:
        raise ValueError("account_selection_required")
    return candidates[0]


def validate_snapshot(holdings, orders):
    if not isinstance(holdings, dict) or not isinstance(holdings.get("items"), list):
        raise ValueError("invalid_holdings_schema")
    if not isinstance(orders, dict) or not isinstance(orders.get("orders"), list):
        raise ValueError("invalid_orders_schema")
    if orders.get("hasNext") is not False or orders.get("nextCursor") is not None:
        raise ValueError("incomplete_open_orders")


def validate_market_frame(channel: str, data: dict):
    def number(value):
        if not isinstance(value, str) or len(value) > 30:
            raise ValueError("invalid_market_payload")
        parsed = Decimal(value)
        if not parsed.is_finite() or parsed < 0:
            raise ValueError("invalid_market_payload")

    if not isinstance(data.get("currency"), str):
        raise ValueError("invalid_market_payload")
    stamp = data.get("timestamp")
    if stamp is not None:
        if not isinstance(stamp, str) or datetime.fromisoformat(stamp).tzinfo is None:
            raise ValueError("invalid_market_payload")
    if channel.startswith("trade:"):
        if stamp is None:
            raise ValueError("invalid_market_payload")
        number(data.get("price"))
        number(data.get("volume"))
    else:
        for side in ("asks", "bids"):
            if not isinstance(data.get(side), list):
                raise ValueError("invalid_market_payload")
            for level in data[side]:
                number(level["price"])
                number(level["volume"])


async def probe_toss(
    values: dict,
    expected_ip: str,
    *,
    seconds: float = 75,
    http=None,
    stream_factory=TossStream,
) -> dict:
    """One token, sequential sockets, five topics, two REST refreshes; never writes orders."""
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "status": "blocked",
        "stage": "configuration",
        "live_order_supported": False,
        "full_account_reconciled": False,
        "observation_stale": True,
        "checks": {},
        "websocket": {"acknowledgements": 0, "pongs": 0, "messages_by_channel": {}},
    }
    owned = http is None
    try:
        ipaddress.IPv4Address(expected_ip)
        if not 1 <= seconds <= 90:
            raise ValueError("invalid_probe_duration")
        if not values.get("TOSS_CLIENT_ID") or not values.get("TOSS_CLIENT_SECRET"):
            raise ValueError("credentials_missing")
        http = http if http is not None else http_client()
        report["stage"] = "egress"
        response = await http.get("https://checkip.amazonaws.com", follow_redirects=False)
        response.raise_for_status()
        if response.text.strip() != expected_ip:
            raise ValueError("unexpected_egress_ip")
        report["checks"]["expected_egress"] = True
        tokens = TokenManager(
            Credentials(values["TOSS_CLIENT_ID"], values["TOSS_CLIENT_SECRET"]), http
        )
        broker = TossReadOnly(tokens, http)
        report["stage"] = "oauth"
        await tokens.get()
        report["checks"]["oauth"] = True
        report["stage"] = "accounts"
        account = select_account(await broker.accounts(), values.get("TOSS_ACCOUNT_SEQ", ""))
        report["checks"]["account_selected"] = True
        report["stage"] = "prices"
        prices = await broker.prices(("005930", "AAPL"))
        if not isinstance(prices, list) or {p.get("symbol") for p in prices} != {"005930", "AAPL"}:
            raise ValueError("invalid_prices_schema")
        report["checks"]["prices"] = True
        subscriptions = (
            Subscription("trade:kr", ("005930",)),
            Subscription("orderbook:kr", ("005930",)),
            Subscription("trade:us", ("AAPL",)),
            Subscription("orderbook:us", ("AAPL",)),
            Subscription("personal:order", (account,)),
        )

        async def gap():
            report["observation_stale"] = True

        async def reconcile():
            report["stage"] = "rest_refresh"
            # Sequential requests keep the probe comfortably below broker read limits.
            holdings = await broker.holdings(account)
            orders = await broker.open_orders(account)
            validate_snapshot(holdings, orders)
            report["checks"]["rest_refreshes"] = report["checks"].get("rest_refreshes", 0) + 1
            # REST excludes some manually placed orders: do not clear uncertainty.
            report["stage"] = "websocket"

        for window in (seconds, 2):
            report["stage"] = "websocket"
            stream = stream_factory(tokens, subscriptions, gap, reconcile, max_reconnects=0)
            async with aclosing(stream.frames()) as frames:
                ack = await anext(frames)
                if ack["type"] != "subscriptions":
                    raise ValueError("missing_ack")
                report["websocket"]["acknowledgements"] += 1
                # Timeout is applied only to observation; connection/ack/REST errors fail.
                try:
                    async with asyncio.timeout(window):
                        async for frame in frames:
                            if frame["type"] == "pong":
                                report["websocket"]["pongs"] += 1
                            if frame["type"] == "message":
                                channel = frame["topic"].rsplit(":", 1)[0]
                                if channel != "personal:order":
                                    validate_market_frame(channel, frame["data"])
                                counts = report["websocket"]["messages_by_channel"]
                                counts[channel] = counts.get(channel, 0) + 1
                except TimeoutError:
                    pass
        report["checks"]["close_reopen_redeclare"] = True
        report["checks"]["heartbeat"] = report["websocket"]["pongs"] > 0
        report["status"] = "passed" if report["checks"]["heartbeat"] else "partial"
        report["stage"] = "complete"
    except Exception as error:
        # This operator boundary must never print transport messages, response bodies or tokens.
        if isinstance(error, TossHTTPError):
            report["error"] = {"kind": "broker_http", "http_status": error.status}
        elif isinstance(error, TossError):
            report["error"] = {"kind": "broker_protocol", "reason": type(error).__name__}
        else:
            safe_reasons = {
                "invalid_accounts_schema",
                "configured_account_not_found",
                "account_selection_required",
                "invalid_holdings_schema",
                "invalid_orders_schema",
                "incomplete_open_orders",
                "invalid_market_payload",
                "invalid_probe_duration",
                "credentials_missing",
                "unexpected_egress_ip",
                "invalid_prices_schema",
                "missing_ack",
            }
            report["error"] = {
                "kind": "probe_failed",
                "reason": str(error) if str(error) in safe_reasons else "unexpected_failure",
            }
    finally:
        if owned and http is not None:
            await http.aclose()
    return report
