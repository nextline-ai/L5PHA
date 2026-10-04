"""Small, validated owner-only export; never an order authorization or AI input."""

import json
import math
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from veyquant.toss_probe import validate_snapshot

MONEY_FIELDS = {"cash_buying_power_krw", "domestic_market_value_krw"}
COUNT_FIELDS = {
    "domestic_positions",
    "open_orders",
    "conditional_orders",
    "tracked_orders",
    "order_events",
    "orders_needing_review",
}


def amount(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,24}(?:\.[0-9]{1,6})?", value):
        raise ValueError("invalid_account_amount")
    return Decimal(value)


def build_account_view(holdings, orders, buying_power, conditionals, calendar, now):
    validate_snapshot(holdings, orders)
    if not isinstance(buying_power, dict) or buying_power.get("currency") != "KRW":
        raise ValueError("invalid_buying_power")
    cash = amount(buying_power["cashBuyingPower"])
    value, positions = Decimal(0), 0
    seen = set()
    for item in holdings["items"]:
        if not isinstance(item, dict) or item.get("marketCountry") not in {"KR", "US"}:
            raise ValueError("invalid_holding")
        symbol = item.get("symbol")
        if (
            not isinstance(symbol, str)
            or not re.fullmatch(r"[A-Za-z0-9.-]{1,20}", symbol)
            or symbol in seen
        ):
            raise ValueError("invalid_holding_symbol")
        seen.add(symbol)
        if item["marketCountry"] == "KR":
            if item.get("currency") != "KRW":
                raise ValueError("invalid_holding_currency")
            quantity = amount(item["quantity"])
            value += amount(item["marketValue"]["amount"])
            positions += quantity > 0
    if (
        not isinstance(conditionals, dict)
        or conditionals.get("hasNext") is not False
        or conditionals.get("nextCursor") is not None
        or not isinstance(conditionals.get("conditionalOrders"), list)
    ):
        raise ValueError("incomplete_conditional_orders")
    if any(
        not isinstance(o, dict)
        or o.get("status") not in {"WATCHING", "PAUSED", "ORDERING", "ORDERED"}
        for o in conditionals["conditionalOrders"]
    ):
        raise ValueError("invalid_conditional_order")
    today = calendar["today"]
    local_date = datetime.fromtimestamp(now, ZoneInfo("Asia/Seoul")).date().isoformat()
    if today["date"] != local_date:
        raise ValueError("stale_market_calendar")
    regular = today["integrated"]["regularMarket"] if today["integrated"] else None
    start, end = None, None
    if regular:
        # Deliberate launch scope: regular continuous trading, excluding auctions.
        def stamp(key):
            parsed = datetime.fromisoformat(regular[key])
            if (
                parsed.tzinfo is None
                or parsed.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat() != local_date
            ):
                raise ValueError("invalid_market_calendar")
            return parsed.timestamp()

        start, end = stamp("startTime"), stamp("singlePriceAuctionStartTime")
        if start >= end:
            raise ValueError("invalid_market_calendar")
    return {
        "snapshot_at": now,
        "cash_buying_power_krw": str(cash),
        "domestic_market_value_krw": str(value),
        "domestic_positions": positions,
        "open_orders": len(orders["orders"]),
        "conditional_orders": len(conditionals["conditionalOrders"]),
        "regular_start": start,
        "regular_end": end,
    }


def read_account_view(path: str | None, now: float) -> dict:
    unavailable = {"state": "unavailable", "full_account_reconciled": False}
    if not path:
        return unavailable
    try:
        with Path(path).open("rb") as f:
            data = json.loads(f.read(16001))
        if data.get("available") is not True:
            return unavailable
        for key in ("updated_at", "snapshot_at"):
            if type(data[key]) not in (float, int) or not math.isfinite(data[key]):
                raise ValueError
        if not 0 <= now - data["updated_at"] <= 10 or not 0 <= now - data["snapshot_at"] <= 90:
            return unavailable | {"state": "stale"}
        result = {k: str(amount(data[k])) for k in MONEY_FIELDS}
        for key in COUNT_FIELDS:
            if type(data[key]) is not int or not 0 <= data[key] <= 10**9:
                raise ValueError
            result[key] = data[key]
        start, end = data["regular_start"], data["regular_end"]
        if (start is None) != (end is None):
            raise ValueError
        if start is not None and (
            any(type(v) not in (float, int) or not math.isfinite(v) for v in (start, end))
            or start >= end
            or end - start > 86400
        ):
            raise ValueError
        return result | {
            "state": "fresh",
            "snapshot_at": data["snapshot_at"],
            "regular_session_open": start is not None and start <= now < end,
            "full_account_reconciled": False,
            "order_coverage": "api_supported_types_only",
        }
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return unavailable


def readiness(policy: dict, account: dict, connected: bool, stopped: bool) -> dict:
    # KRX/DART are deferred. They are not hard-coded prerequisites for operation.
    checks = [
        ("policy", "운용 한도 설정", policy["configured"]),
        ("connection", "토스 실시간 연결", connected),
        ("account", "주문 가능 금액·보유·미체결·조건주문 조회", account["state"] == "fresh"),
        ("order_review", "추적 주문 상태 확인", account.get("orders_needing_review") == 0),
        ("controls", "신규 판단 허용", not stopped),
        ("loss_ledger", "실현·평가손익을 합산한 일일 손실 감시", False),
        ("execution", "자동 주문·취소 및 장애 복구 검증", False),
    ]
    return {
        "live_enabled": False,
        "checks": [{"id": key, "label": label, "passed": passed} for key, label, passed in checks],
    }
