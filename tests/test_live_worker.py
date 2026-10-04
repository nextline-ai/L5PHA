import copy
import json
from datetime import datetime
from decimal import Decimal

import pytest
from test_account_readiness import CALENDAR, CONDITIONALS, HOLDINGS, NOW, ORDERS
from test_execution import Transport
from test_shadow import Model, gate, report

from veyquant.collector import ObservationStore
from veyquant.live_worker import LiveWorker
from veyquant.shadow_inference import MODEL_PRESETS, ROLE_REASONING, analyze, reasoning_selection
from veyquant.shadow_runner import validated_report
from veyquant.trading_state import completed_bars, read_execution
from veyquant.universe import Universe


class Broker:
    def __init__(self):
        self.quantity = 2
        self.details = {}
        self.external = False
        self.conditionals = copy.deepcopy(CONDITIONALS)
        self.calendar = copy.deepcopy(CALENDAR) | {"previousBusinessDay": {"date": "2026-09-08"}}
        self.calendar["today"]["integrated"]["regularMarket"]["endTime"] = (
            "2026-09-09T15:30:00+09:00"
        )

    async def holdings(self, account):
        result = copy.deepcopy(HOLDINGS)
        result["items"][0]["quantity"] = str(self.quantity)
        return result

    async def open_orders(self, account):
        result = copy.deepcopy(ORDERS)
        result["orders"] = [
            d for d in self.details.values() if d["status"] in {"PENDING", "PARTIAL_FILLED"}
        ]
        if self.external:
            result["orders"].append(self.detail("other", 5))
        return result

    def detail(self, oid, quantity, status="PENDING", filled=0, side="BUY", fees="0"):
        return {
            "orderId": "broker-" + oid,
            "symbol": "005930",
            "side": side,
            "orderType": "LIMIT",
            "timeInForce": "DAY",
            "currency": "KRW",
            "quantity": str(quantity),
            "price": "100",
            "status": status,
            "orderedAt": datetime.fromtimestamp(NOW).astimezone().isoformat(),
            "execution": {
                "filledQuantity": str(filled),
                "filledAmount": str(filled * 100),
                "commission": fees,
                "tax": "0",
            },
        }

    async def order(self, account, oid):
        return copy.deepcopy(self.details[oid])

    async def buying_power(self, account):
        return {"currency": "KRW", "cashBuyingPower": "1000"}

    async def sellable_quantity(self, account, symbol):
        return {"sellableQuantity": str(self.quantity)}

    async def conditional_orders(self, account):
        return copy.deepcopy(self.conditionals)

    async def market_calendar(self):
        return copy.deepcopy(self.calendar)

    async def commissions(self, account):
        return [{"marketCountry": "KR", "commissionRate": "0"}]

    async def stock_info(self, symbols):
        return [
            {
                "symbol": s,
                "name": "테스트 종목 " + s,
                "market": "KOSPI",
                "currency": "KRW",
                "securityType": "STOCK",
                "isCommonShare": True,
                "status": "ACTIVE",
                "koreanMarketDetail": {"liquidationTrading": False, "krxTradingSuspended": False},
            }
            for s in symbols
        ]

    async def stock_warnings(self, symbol):
        return []

    async def daily_candles(self, symbol="005930"):
        return {
            "candles": [
                {
                    "timestamp": f"2026-09-{d}T09:00:00+09:00",
                    "currency": "KRW",
                    "openPrice": "100",
                    "highPrice": "101",
                    "lowPrice": "99",
                    "closePrice": "100",
                    "volume": "1000",
                }
                for d in ["01", "02", "03", "04", "07", "08"]
            ]
        }


@pytest.fixture
def worker(tmp_path):
    obs = ObservationStore(str(tmp_path / "observations.db"), str(tmp_path / "collector.json"))
    obs.connected = True
    t = [NOW]
    broker = Broker()
    transport = Transport()
    paths = [
        str(tmp_path / name)
        for name in ["execution.db", "control.json", "reports.json", "execution.json"]
    ]
    w = LiveWorker(*paths, obs, broker, transport, "1", clock=lambda: t[0])
    obs.universe = Universe(obs, broker, clock=lambda: t[0])
    obs.universe.catalog_at = t[0]
    obs.universe.stocks = {"005930": {"symbol": "005930"}}
    w.test_time = t
    w.test_control = {
        "version": 1,
        "updated_at": NOW,
        "owner_bound": True,
        "stopped": False,
        "commands": [],
        "policy": {
            "revision": 1,
            "updated_at": NOW - 5,
            "configured": True,
            "onboarding_completed": True,
            "live_requested": True,
            "model_connection": {"ready": True},
            "limits": {"capital_krw": "1000", "max_order_krw": "600", "max_daily_loss_krw": "100"},
        },
    }
    update(w)
    yield w
    w.close()
    obs.db.close()


def update(w, seconds=0, side="BUY", event="a", price="100", symbol="005930"):
    w.test_time[0] += seconds
    now = w.clock()
    w.test_control["updated_at"] = now
    open(w.control_path, "w").write(json.dumps(w.test_control))
    q = {"symbol": symbol, "currency": "KRW", "price": price, "as_of": now, "received_at": now}
    w.observations.quotes[symbol] = q
    w.observations.universe.watch = tuple(w.observations.quotes)
    book = {
        "timestamp": datetime.fromtimestamp(now).astimezone().isoformat(),
        "currency": "KRW",
        "asks": [{"price": price, "volume": "100"}],
        "bids": [{"price": price, "volume": "100"}],
    }
    w.observations.db.execute(
        "INSERT OR REPLACE INTO latest VALUES (?,?,?)",
        (f"orderbook:kr:{symbol}", json.dumps(book), now),
    )
    report = {
        "event_id": event * 64,
        "symbol": symbol,
        "quote": q,
        "created_at": now,
        "settings_revision": 1,
        "outcome": side.lower(),
        "proposal": {"side": side},
        "stages": [{"role": r, "status": "received"} for r in ("cheap", "middle", "research")],
    }
    open(w.report_path, "w").write(json.dumps({"updated_at": now, "reports": [report]}))


def output(w):
    return read_execution(w.status_path, w.clock())


def receive(w, status="FILLED", filled=5, side="BUY", fees="0"):
    row = w.db.execute(
        "SELECT * FROM execution_orders WHERE id=?", (w.transport.creates[-1]["clientOrderId"],)
    ).fetchone()
    i = json.loads(row["intent"])
    d = w.broker.detail(row["id"], i["quantity"], status, filled, side, fees)
    w.broker.details[d["orderId"]] = d
    return row["id"]


@pytest.mark.parametrize("preset", list(MODEL_PRESETS))
def test_provider_reasoning_defaults_and_custom_override(preset):
    expected = ROLE_REASONING | ({"middle": "high"} if preset == "chatgpt" else {})
    assert reasoning_selection(MODEL_PRESETS[preset]) == expected
    custom = {"cheap": "high", "middle": "low", "research": "medium"}
    assert reasoning_selection(MODEL_PRESETS[preset], custom) == custom


async def test_buy_fill_sell_and_external_shares_remain_owned_by_user(worker):
    w = worker
    await w.tick()
    assert len(w.transport.creates) == 1, output(w)
    assert w.transport.creates[0]["quantity"] == "5"
    receive(w)
    w.broker.quantity = 7
    update(w, seconds=2)
    await w.tick()
    assert w.core._positions()["005930"] == (5, Decimal(500))
    assert len(w.transport.creates) == 1
    update(w, seconds=2, side="SELL", event="b")
    await w.tick()
    assert len(w.transport.creates) == 2, output(w)
    assert w.transport.creates[1]["quantity"] == "5"
    receive(w, side="SELL", fees="1")
    w.broker.quantity = 2
    update(w, seconds=2, side="SELL", event="b")
    await w.tick()
    assert w.core._positions() == {}
    assert output(w)["loss"]["pnl_krw"] == "-1"
    assert w.broker.quantity == 2


@pytest.mark.parametrize(
    "reason",
    ["disabled", "stopped", "external_orders", "conditional", "expired_control", "model_missing"],
)
async def test_owner_and_broker_gates_prevent_writes(worker, reason):
    w = worker
    if reason == "disabled":
        w.test_control["policy"]["live_requested"] = False
    if reason == "stopped":
        w.test_control["stopped"] = True
    if reason == "external_orders":
        w.broker.external = True
    if reason == "conditional":
        w.broker.conditionals["conditionalOrders"] = [{"status": "WATCHING"}]
    if reason == "model_missing":
        w.test_control["policy"]["model_connection"]["ready"] = False
    update(w)
    if reason == "expired_control":
        w.test_time[0] += 11
    await w.tick()
    assert w.transport.creates == []
    assert not output(w)["live_enabled"]


async def test_unknown_write_is_durable_and_never_retried_after_ten_minutes(worker):
    w = worker
    w.transport.fail = True
    await w.tick()
    assert len(w.transport.creates) == 1
    assert output(w)["orders"][0]["state"] == "UNKNOWN"
    w.core.recover(w.clock())
    update(w, seconds=601, event="b")
    await w.tick()
    assert len(w.transport.creates) == 1
    assert output(w)["state"] == "order_review"


async def test_user_disable_cancels_owned_order_but_never_sells_position(worker):
    w = worker
    await w.tick()
    receive(w, status="PENDING", filled=0)
    w.test_control["policy"]["live_requested"] = False
    update(w, seconds=2)
    await w.tick()
    assert len(w.transport.cancels) == 1
    assert len(w.transport.creates) == 1
    assert output(w)["orders"][0]["state"] == "CANCEL_PENDING"


async def test_loss_breach_latches_through_price_recovery_and_settings_changes(worker):
    w = worker
    await w.tick()
    receive(w)
    w.broker.quantity = 7
    update(w, seconds=2, event="b", price="80")
    await w.tick()
    assert output(w)["loss"]["state"] == "breached"
    assert len(w.transport.creates) == 1
    w.test_control["policy"]["limits"]["max_daily_loss_krw"] = "1000"
    update(w, seconds=2, event="c", price="110")
    await w.tick()
    assert output(w)["loss"]["state"] == "breached"
    assert len(w.transport.creates) == 1


async def test_external_position_change_blocks_and_cannot_become_managed(worker):
    w = worker
    await w.tick()
    receive(w)
    w.broker.quantity = 8
    update(w, seconds=2, event="b")
    await w.tick()
    assert output(w)["state"] == "position_mismatch"
    assert len(w.transport.creates) == 1


async def test_stop_during_token_refresh_vetoes_dispatch(worker):
    w = worker
    original = w.transport.create

    async def changed(intent, *, before_send):
        w.test_control["stopped"] = True
        update(w)
        return await original(intent, before_send=before_send)

    w.transport.create = changed
    await w.tick()
    assert w.transport.creates == []
    assert output(w)["orders"][0]["state"] == "VOID"


async def test_owner_attaches_unknown_by_exact_broker_id_after_disabling(worker):
    w = worker
    w.transport.fail = True
    await w.tick()
    row = w.db.execute("SELECT * FROM execution_orders").fetchone()
    detail = w.broker.detail(row["id"], 5, status="FILLED", filled=5)
    w.broker.details[detail["orderId"]] = detail
    w.broker.quantity = 7
    w.test_control["policy"]["live_requested"] = False
    w.test_control["commands"] = [
        {
            "id": "recovery1",
            "action": "attach",
            "order_id": row["id"],
            "broker_id": detail["orderId"],
        }
    ]
    update(w, seconds=2)
    await w.tick()
    assert output(w)["orders"][0]["state"] == "FILLED", output(w)
    assert w.core._positions()["005930"][0] == 5
    assert len(w.transport.creates) == 1


async def test_daily_bars_allow_direction_but_never_quantity_or_account_input():
    from test_shadow import NOW as ANALYSIS_NOW
    from test_shadow import event

    b = Broker()
    bars = completed_bars(await b.daily_candles(), ANALYSIS_NOW)
    e = event() | {"daily_bars": {"updated_at": ANALYSIS_NOW, "bars": bars}}
    decision = report() | {"outcome": "buy", "evidence_ids": ["quote", "daily_bars"]}
    model = Model([gate(), gate(), decision])
    raw = analyze(e, model, ANALYSIS_NOW)
    safe = validated_report(raw, e, ANALYSIS_NOW + 1)
    assert safe["proposal"] == {"side": "BUY"}
    assert not safe["risk"]["order_enabled"]
    assert "quantity" not in safe["proposal"]
    bad = copy.deepcopy(raw)
    bad["evidence"] = [r for r in bad["evidence"] if r["id"] != "daily_bars"]
    with pytest.raises(ValueError, match="trade_evidence"):
        validated_report(bad, e, ANALYSIS_NOW + 1)
    raw = analyze(event(), Model([gate(), gate(), decision]), ANALYSIS_NOW)
    assert raw["outcome"] == "error"


async def test_new_day_includes_overnight_gap_from_previous_close(worker):
    w = worker
    await w.tick()
    receive(w)
    w.broker.quantity = 7
    update(w, seconds=2)
    await w.tick()
    w.broker.calendar["today"]["date"] = "2026-09-10"
    session = w.broker.calendar["today"]["integrated"]["regularMarket"]
    session["startTime"] = "2026-09-10T09:00:00+09:00"
    session["singlePriceAuctionStartTime"] = "2026-09-10T15:20:00+09:00"
    w.broker.calendar["previousBusinessDay"]["date"] = "2026-09-09"
    original = w.broker.daily_candles

    async def next_day_bars(symbol):
        data = await original(symbol)
        last = copy.deepcopy(data["candles"][-1])
        last["timestamp"] = "2026-09-09T09:00:00+09:00"
        data["candles"].append(last)
        return data

    w.broker.daily_candles = next_day_bars
    update(w, seconds=86400, event="b", price="80")
    await w.tick()
    assert output(w)["loss"]["day"] == "2026-09-10", output(w)
    assert output(w)["loss"]["pnl_krw"] == "-100"
    assert output(w)["loss"]["state"] == "breached"
    assert len(w.transport.creates) == 1


async def test_monitor_blocks_loss_even_when_no_new_analysis_arrives(worker):
    w = worker
    await w.tick()
    receive(w)
    w.broker.quantity = 7
    update(w, seconds=2)
    await w.tick()
    update(w, seconds=1, price="80")
    await w.monitor()
    assert w.db.execute("SELECT breached FROM execution_days").fetchone()[0] == 1


async def test_unknown_absence_cannot_be_cleared_immediately(worker):
    w = worker
    w.transport.fail = True
    await w.tick()
    row = w.db.execute("SELECT * FROM execution_orders").fetchone()
    w.test_control["policy"]["live_requested"] = False
    w.test_control["commands"] = [
        {"id": "early-absence", "action": "confirm_absent", "order_id": row["id"]}
    ]
    update(w, seconds=2)
    await w.tick()
    assert output(w)["orders"][0]["state"] == "UNKNOWN"
    assert output(w)["commands"][0]["result"] == "not_applicable"


async def test_monitor_preserves_fresh_snapshot_during_rest_refresh(worker):
    w = worker
    await w.tick()
    receive(w, status="PENDING", filled=0)
    update(w, seconds=1)
    await w.monitor()
    assert w.transport.cancels == []
    w.test_time[0] += 6
    await w.monitor()
    assert len(w.transport.cancels) == 1


async def test_multi_stock_capital_sell_quantity_and_external_anchors(worker):
    w = worker
    quantities = {"005930": 2, "000660": 11, "0101N0": 0}
    w.observations.universe.stocks.update({s: {"symbol": s} for s in quantities})

    async def holdings(account):
        return {
            "items": [
                copy.deepcopy(HOLDINGS["items"][0]) | {"symbol": s, "quantity": str(q)}
                for s, q in quantities.items()
            ]
        }

    async def sellable(account, symbol):
        return {"sellableQuantity": str(quantities[symbol])}

    w.broker.holdings, w.broker.sellable_quantity = holdings, sellable
    await w.tick()
    receive(w)
    quantities["005930"] = 7
    update(w, seconds=2, event="b", symbol="000660")
    await w.tick()
    assert len(w.transport.creates) == 2, output(w)
    second = w.transport.creates[-1]
    assert second["symbol"] == "000660" and second["quantity"] == "4"
    oid = second["clientOrderId"]
    detail = w.broker.detail(oid, 4, status="FILLED", filled=4) | {"symbol": "000660"}
    w.broker.details[detail["orderId"]] = detail
    quantities["000660"] = 15
    update(w, seconds=1, event="c", symbol="0101N0")
    await w.tick()
    # 500 + 400 already invested, leaving less than one 100-won share plus fees.
    assert len(w.transport.creates) == 2
    assert w.core.capital_used() == 900
    update(w, seconds=1, event="d", side="SELL", symbol="000660")
    await w.tick()
    assert len(w.transport.creates) == 3, output(w)
    assert w.transport.creates[-1]["quantity"] == "4"  # Never the user's 11 external shares.
    assert set(w.pinned_symbols()) == {"005930", "000660"}


async def test_new_symbol_without_any_holding_can_anchor_after_other_symbol_traded(worker):
    w = worker
    w.observations.universe.stocks["0101N0"] = {"symbol": "0101N0"}
    await w.tick()
    receive(w)
    w.broker.quantity = 7
    update(w, seconds=2, event="b", symbol="0101N0")
    await w.tick()
    assert len(w.transport.creates) == 2, output(w)
    assert w.transport.creates[-1]["symbol"] == "0101N0"
    assert (
        w.db.execute("SELECT quantity FROM execution_anchor WHERE symbol='0101N0'").fetchone()[0]
        == 0
    )


async def test_current_stock_warning_vetoes_order_without_consuming_other_assets(worker):
    w = worker

    async def warnings(symbol):
        return [{"warningType": "VI_DYNAMIC"}]

    w.broker.stock_warnings = warnings
    await w.tick()
    assert not w.transport.creates
    assert output(w)["last_action"] == "instrument_restricted"
