import json
from datetime import datetime

import pytest

from veyquant.account_readiness import build_account_view, read_account_view, readiness

NOW = datetime.fromisoformat("2026-09-09T10:00:00+09:00").timestamp()
HOLDINGS = {
    "items": [
        {
            "symbol": "005930",
            "marketCountry": "KR",
            "currency": "KRW",
            "quantity": "2",
            "marketValue": {"amount": "140000"},
        }
    ]
}
ORDERS = {"orders": [], "hasNext": False, "nextCursor": None}
CONDITIONALS = {"conditionalOrders": [], "hasNext": False, "nextCursor": None}
CALENDAR = {
    "today": {
        "date": "2026-09-09",
        "integrated": {
            "regularMarket": {
                "startTime": "2026-09-09T09:00:00+09:00",
                "singlePriceAuctionStartTime": "2026-09-09T15:20:00+09:00",
            }
        },
    }
}


def account_data():
    return build_account_view(
        HOLDINGS,
        ORDERS,
        {"currency": "KRW", "cashBuyingPower": "500000"},
        CONDITIONALS,
        CALENDAR,
        NOW,
    )


def export_data():
    return account_data() | {
        "available": True,
        "updated_at": NOW,
        "tracked_orders": 0,
        "order_events": 0,
        "orders_needing_review": 0,
    }


def test_owner_view_whitelists_fields_and_hides_stale_amounts(tmp_path):
    path = tmp_path / "account.json"
    path.write_text(json.dumps(export_data() | {"private_order_id": "secret-opaque"}))
    result = read_account_view(str(path), NOW + 1)
    assert result["cash_buying_power_krw"] == "500000"
    assert result["domestic_market_value_krw"] == "140000"
    assert result["regular_session_open"]
    assert "secret" not in json.dumps(result)
    assert not result["full_account_reconciled"]
    for stamp in [NOW - 1, NOW + 11, NOW + 91]:
        assert "cash_buying_power_krw" not in read_account_view(str(path), stamp)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("cash_buying_power_krw", "NaN"),
        ("regular_end", "secret"),
        ("orders_needing_review", True),
        ("snapshot_at", float("nan")),
    ],
)
def test_corrupt_private_export_fails_closed(tmp_path, field, bad):
    path = tmp_path / "account.json"
    path.write_text(json.dumps(export_data() | {field: bad}))
    assert read_account_view(str(path), NOW)["state"] == "unavailable"


def test_holiday_valid_but_previous_calendar_invalid():
    args = (HOLDINGS, ORDERS, {"currency": "KRW", "cashBuyingPower": "0"}, CONDITIONALS)
    result = build_account_view(*args, {"today": {"date": "2026-09-09", "integrated": None}}, NOW)
    assert result["regular_start"] is None
    with pytest.raises(ValueError, match="stale_market_calendar"):
        build_account_view(*args, CALENDAR, NOW + 86400)


def test_setup_cannot_claim_execution_or_loss_monitor_verified():
    view = readiness(
        {"configured": True}, {"state": "fresh", "orders_needing_review": 0}, True, False
    )
    assert not view["live_enabled"]
    assert {c["id"] for c in view["checks"] if not c["passed"]} == {"loss_ledger", "execution"}
    assert all("DART" not in c["id"] and "KRX" not in c["id"] for c in view["checks"])
