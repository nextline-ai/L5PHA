import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest

from veyquant.adapters.toss import TossHTTPError
from veyquant.adapters.toss_execution import TossExecutionTransport
from veyquant.execution import DispatchVeto, ExecutionCore, ExecutionSnapshot, Intent, day_key
from veyquant.shadow_inference import model_selection
from veyquant.store import Store

NOW = 1788930000.0
POLICY = {
    "revision": 1,
    "onboarding_completed": True,
    "live_requested": True,
    "live_enabled": True,
    "models": model_selection(),
    "limits": {"capital_krw": "1000", "max_order_krw": "600", "max_daily_loss_krw": "100"},
}


def snapshot(**changes):
    value = ExecutionSnapshot(
        NOW, "1000", "0", {"005930": ("100", NOW)}, {}, {}, frozenset(), True, True
    )
    return replace(value, **changes)


def intent(oid="first", **changes):
    return replace(Intent(oid, "005930", "BUY", 5, "100", "5", 1, NOW, NOW + 60), **changes)


class Transport:
    def __init__(self, fail=False):
        self.creates = []
        self.cancels = []
        self.fail = fail

    async def create(self, order, *, before_send):
        if not before_send():
            raise DispatchVeto()
        self.creates.append(order.body())
        if self.fail:
            raise TimeoutError
        return {"orderId": "broker-" + order.id, "clientOrderId": order.id}

    async def cancel(self, oid):
        self.cancels.append(oid)
        if self.fail:
            raise TimeoutError
        return {"orderId": "cancel-reference"}


def detail(
    oid="first", state="FILLED", filled="5", amount="500", commission="5", tax="0", side="BUY"
):
    return {
        "orderId": "broker-" + oid,
        "symbol": "005930",
        "side": side,
        "currency": "KRW",
        "quantity": "5",
        "orderType": "LIMIT",
        "price": "100",
        "status": state,
        "execution": {
            "filledQuantity": filled,
            "filledAmount": amount,
            "commission": commission,
            "tax": tax,
        },
    }


@pytest.fixture
def core(tmp_path):
    with closing(Store(str(tmp_path / "execution.db"))) as store:
        engine = ExecutionCore(store, armed=True, current_policy=lambda: POLICY)
        engine.install_loss_baseline(day_key(NOW), Decimal(0))
        yield engine


async def buy(core, transport=None):
    t = transport or Transport()
    assert core.prepare(intent(), snapshot(), POLICY, NOW) == "accepted"
    await core.submit("first", t, snapshot(), POLICY, NOW)
    return t


def state(core, oid="first"):
    return core.db.execute("SELECT state FROM execution_orders WHERE id=?", (oid,)).fetchone()[0]


@pytest.mark.parametrize("field", ["live_requested", "live_enabled"])
async def test_disabled_or_pending_live_cannot_prepare_or_send(core, field):
    disabled = POLICY | {field: False}
    assert core.prepare(intent(), snapshot(), disabled, NOW) == "live_not_enabled"
    assert core.prepare(intent(), snapshot(), POLICY, NOW) == "accepted"
    core.current_policy = lambda: disabled
    transport = Transport()
    await core.submit("first", transport, snapshot(), POLICY, NOW)
    assert transport.creates == []
    assert state(core) == "VOID"


def test_concurrent_reservations_never_exceed_capital(tmp_path):
    path = str(tmp_path / "db")
    with closing(Store(path)) as store:
        ExecutionCore(store).install_loss_baseline(day_key(NOW), Decimal(0))

    def reserve(oid):
        with closing(Store(path)) as store:
            return ExecutionCore(store, armed=True).prepare(intent(oid), snapshot(), POLICY, NOW)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(reserve, ["a", "b"]))
    assert sorted(results) == ["accepted", "cash_limit"]


async def test_duplicate_dispatch_and_unknown_result_never_retry_even_after_idempotency_window(
    core,
):
    t = Transport(fail=True)
    await buy(core, t)
    assert state(core) == "UNKNOWN"
    for when in [NOW + 1, NOW + 601]:
        await core.submit("first", t, snapshot(), POLICY, when)
    assert len(t.creates) == 1
    assert (
        core.prepare(intent("second", quantity=1, fee_buffer="0"), snapshot(), POLICY, NOW)
        == "unresolved_order"
    )
    assert core.recover(NOW + 700) == []
    assert not core.armed and state(core) == "UNKNOWN"


async def test_two_submitters_only_one_network_write(core):
    assert core.prepare(intent(), snapshot(), POLICY, NOW) == "accepted"
    t = Transport()
    await asyncio.gather(*(core.submit("first", t, snapshot(), POLICY, NOW) for _ in range(2)))
    assert len(t.creates) == 1


@pytest.mark.parametrize(
    "change,expected",
    [
        (dict(cash_buying_power="100"), "cash_limit"),
        (dict(regular_session_open=False), "outside_regular_session"),
        (dict(as_of=NOW - 6), "reconciliation_required"),
        (dict(reconciled=False), "reconciliation_required"),
        (dict(quotes={"005930": ("102", NOW)}), "price_changed"),
        (dict(external_buy_commitments="600"), "capital_limit"),
    ],
)
def test_pretrade_checks_fail_closed(core, change, expected):
    assert core.prepare(intent(), snapshot(**change), POLICY, NOW) == expected
    assert core.db.execute("SELECT COUNT(*) FROM execution_orders").fetchone()[0] == 0


async def test_policy_change_or_stop_between_preparation_and_send_invalidates_intent(core):
    t = Transport()
    assert core.prepare(intent(), snapshot(), POLICY, NOW) == "accepted"
    await core.submit("first", t, snapshot(), POLICY | {"revision": 2}, NOW)
    assert state(core) == "VOID" and t.creates == []
    assert core.prepare(intent("second"), snapshot(), POLICY, NOW) == "accepted"
    core.store.stop()
    await core.submit("second", t, snapshot(), POLICY, NOW)
    assert state(core, "second") == "VOID" and t.creates == []


async def test_partial_fills_and_cancel_are_idempotent_and_realized_loss_includes_costs(core):
    t = await buy(core)
    partial = detail(state="PARTIAL_FILLED", filled="2", amount="200", commission="2")
    assert core.observe("first", partial, NOW + 1)
    assert core.observe("first", partial, NOW + 1)
    assert core._positions() == {"005930": (2, Decimal("202"))}
    assert core.db.execute("SELECT COUNT(*) FROM execution_fills").fetchone()[0] == 1
    await core.cancel("first", t, NOW + 2)
    assert state(core) == "CANCEL_PENDING"
    assert core.db.execute("SELECT broker_id FROM execution_orders").fetchone()[0] == "broker-first"
    assert core.observe(
        "first", detail(state="CANCELED", filled="2", amount="200", commission="2"), NOW + 3
    )
    assert state(core) == "CANCELED"
    sell = intent("sell", side="SELL", quantity=2, fee_buffer="2")
    snap = snapshot(
        as_of=NOW + 3,
        managed_quantities={"005930": 2},
        sellable_quantities={"005930": 2},
        quotes={"005930": ("100", NOW + 3)},
    )
    assert core.prepare(sell, snap, POLICY, NOW + 3) == "accepted"
    await core.submit("sell", t, snap, POLICY, NOW + 3)
    fill = detail("sell", filled="2", amount="200", commission="1", tax="1", side="SELL") | {
        "quantity": "2"
    }
    assert core.observe("sell", fill, NOW + 4)
    assert core._positions() == {}
    status = core.monitor_loss(snapshot(as_of=NOW + 4), POLICY, NOW + 4)
    assert status["pnl_krw"] == "-4"


async def test_daily_loss_latches_after_price_recovery_and_cannot_reset_baseline(core):
    await buy(core)
    assert core.observe("first", detail(), NOW + 1)
    down = snapshot(
        as_of=NOW + 2, managed_quantities={"005930": 5}, quotes={"005930": ("80", NOW + 2)}
    )
    loss = core.monitor_loss(down, POLICY, NOW + 2)
    assert loss["state"] == "breached" and loss["pnl_krw"] == "-105"
    recovered = replace(down, quotes={"005930": ("110", NOW + 2)})
    assert core.monitor_loss(recovered, POLICY, NOW + 2)["state"] == "breached"
    with pytest.raises(ValueError, match="baseline_already_installed"):
        core.install_loss_baseline(day_key(NOW), Decimal("-105"))
    fresh = snapshot(
        as_of=NOW + 86400, managed_quantities={"005930": 5}, quotes={"005930": ("100", NOW + 86400)}
    )
    assert core.monitor_loss(fresh, POLICY, NOW + 86400)["state"] == "daily_baseline_required"


async def test_unmanaged_holding_or_sale_rejected(core):
    assert (
        core.prepare(
            intent(side="SELL"), snapshot(sellable_quantities={"005930": 100}), POLICY, NOW
        )
        == "managed_quantity_limit"
    )
    assert (
        core.prepare(intent(), snapshot(managed_quantities={"005930": 1}), POLICY, NOW)
        == "reconciliation_required"
    )


async def test_restart_voids_unsubmitted_intents_and_quarantines_sending(core):
    assert core.prepare(intent(quantity=2), snapshot(), POLICY, NOW) == "accepted"
    assert core.prepare(intent("second", quantity=2), snapshot(), POLICY, NOW) == "accepted"
    core.begin_dispatch("first", snapshot(), POLICY, NOW)
    core.recover(NOW + 1)
    assert state(core) == "UNKNOWN" and state(core, "second") == "VOID"
    assert (
        core.prepare(intent("third", quantity=1), snapshot(), POLICY, NOW + 1) == "worker_not_armed"
    )


async def test_cancel_timeout_retains_reservation_and_fill_can_win_race(core):
    t = await buy(core)
    t.fail = True
    await core.cancel("first", t, NOW + 1)
    assert state(core) == "CANCEL_UNKNOWN"
    await core.cancel("first", t, NOW + 2)
    assert len(t.cancels) == 1
    assert core.observe("first", detail(), NOW + 3)
    assert state(core) == "FILLED"


@pytest.mark.parametrize(
    "mutation", [{"filledQuantity": "6"}, {"filledAmount": None}, {"commission": None}]
)
async def test_invalid_cumulative_fill_blocks_further_orders(core, mutation):
    await buy(core)
    raw = detail()
    raw["execution"] |= mutation
    assert not core.observe("first", raw, NOW + 1)
    assert state(core) == "REVIEW"
    assert core._positions() == {}


async def test_fee_corrections_apply_once_but_regressing_fills_are_quarantined(core):
    await buy(core)
    assert core.observe("first", detail(), NOW + 1)
    assert core.observe("first", detail(commission="6"), NOW + 2)
    assert core.observe("first", detail(commission="6"), NOW + 3)
    assert core._positions() == {"005930": (5, Decimal("505"))}
    assert state(core) == "FILLED"
    assert sum(Decimal(r[0]) for r in core.db.execute("SELECT realized FROM execution_fills")) == -1
    assert core.observe("first", detail(commission="5"), NOW + 4)
    assert core.observe("first", detail(commission="6"), NOW + 5)
    assert sum(Decimal(r[0]) for r in core.db.execute("SELECT realized FROM execution_fills")) == -1
    assert not core.observe("first", detail(filled="4", amount="400"), NOW + 6)
    assert state(core) == "REVIEW"


@pytest.mark.parametrize("status", [307, 400, 409, 500])
async def test_toss_write_never_retries_or_follows_redirects(status):
    calls = []

    class Tokens:
        async def get(self):
            return "fixture-token"

    def handle(request):
        calls.append(request)
        assert request.headers["X-Tossinvest-Account"] == "7"
        assert request.method == "POST"
        return httpx.Response(
            status,
            headers={"Location": "https://example.invalid"},
            json={"private": "do-not-print"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), follow_redirects=True
    ) as http:
        transport = TossExecutionTransport(Tokens(), http, "7")
        with pytest.raises(TossHTTPError):
            await transport.create(intent(), before_send=lambda: True)
    assert len(calls) == 1 and calls[0].url.host == "openapi.tossinvest.com"


async def test_token_refresh_delay_expires_final_check_without_order_write(core, monkeypatch):
    import veyquant.execution as module

    clock = iter([0, 6])
    monkeypatch.setattr(module, "monotonic", lambda: next(clock))
    t = await buy(core)
    assert t.creates == [] and state(core) == "VOID"


async def test_policy_change_during_token_refresh_is_checked_again(core):
    class TokenRefreshTransport(Transport):
        async def create(self, order, *, before_send):
            core.current_policy = lambda: POLICY | {"revision": 2}
            return await super().create(order, before_send=before_send)

    t = await buy(core, TokenRefreshTransport())
    assert t.creates == [] and state(core) == "VOID"


async def test_price_losses_do_not_replenish_allocated_capital_from_other_account_cash(core):
    await buy(core)
    assert core.observe("first", detail(), NOW + 1)
    assert core.capital_used() == Decimal(505)
    policy = POLICY | {"limits": POLICY["limits"] | {"max_daily_loss_krw": "500"}}
    snap = snapshot(
        as_of=NOW + 2,
        cash_buying_power="5000",
        quotes={"005930": ("50", NOW + 2)},
        managed_quantities={"005930": 5},
    )
    proposed = intent("second", quantity=12, limit_price="50", fee_buffer="0")
    assert core.prepare(proposed, snap, policy, NOW + 2) == "capital_limit"


def test_sell_above_order_cap_keeps_ownership_guard(core):
    core.db.execute("INSERT INTO execution_positions VALUES ('005930',10,'1000')")
    snap = snapshot(managed_quantities={"005930": 10}, sellable_quantities={"005930": 10})
    assert (
        core.prepare(intent("sell-large", side="SELL", quantity=10), snap, POLICY, NOW)
        == "accepted"
    )
    assert core.prepare(intent("buy-large", quantity=10), snap, POLICY, NOW) == "order_limit"
    assert (
        core.prepare(intent("sell-excess", side="SELL", quantity=11), snap, POLICY, NOW)
        != "accepted"
    )
