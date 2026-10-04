from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from threading import Barrier

import pytest

from veyquant.risk import PaperRiskEngine
from veyquant.store import Store


def test_reserves_and_rejects_duplicate(store, policy, proposal, account, quote):
    risk = PaperRiskEngine(store, policy)
    assert risk.evaluate(proposal, account, quote, 1000).accepted
    assert risk.evaluate(proposal, account, quote, 1000).reason == "duplicate_proposal"
    assert store.db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"expires_at": 1000}, "proposal_expired_or_future"),
        ({"created_at": 1001}, "proposal_expired_or_future"),
        ({"policy_version": "old"}, "policy_changed"),
        ({"quantity": 0}, "unsupported_order"),
        ({"quantity": True}, "unsupported_order"),
        ({"side": "SELL"}, "unsupported_order"),
        ({"currency": "USD"}, "currency_mismatch"),
        ({"limit_price": Decimal("NaN")}, "invalid_price"),
        ({"limit_price": Decimal("Infinity")}, "invalid_price"),
        ({"limit_price": Decimal("0")}, "invalid_price"),
        ({"quantity": 6}, "order_limit"),
        ({"evidence_ids": ()}, "missing_evidence"),
    ],
)
def test_bad_proposals_fail_without_reservation(
    store, policy, proposal, account, quote, changes, reason
):
    assert (
        PaperRiskEngine(store, policy)
        .evaluate(replace(proposal, **changes), account, quote, 1000)
        .reason
        == reason
    )
    assert not store.db.execute("SELECT * FROM reservations").fetchall()


@pytest.mark.parametrize("as_of", [969, 1001])
def test_stale_and_future_snapshots(store, policy, proposal, account, quote, as_of):
    assert (
        PaperRiskEngine(store, policy)
        .evaluate(proposal, replace(account, as_of=as_of), quote, 1000)
        .reason
        == "stale_or_future_snapshot"
    )


def test_manual_orders_and_exposure_count(store, policy, proposal, account, quote):
    account = replace(
        account,
        exposure=Decimal("300"),
        symbol_exposure={"DEMO": Decimal("300")},
        external_reserved=Decimal("100"),
        external_symbol_reserved={"DEMO": Decimal("100")},
    )
    assert PaperRiskEngine(store, policy).evaluate(proposal, account, quote, 1000).reason == (
        "symbol_limit"
    )


def test_inconsistent_account_is_not_trusted(store, policy, proposal, account, quote):
    assert (
        PaperRiskEngine(store, policy)
        .evaluate(proposal, replace(account, external_reserved=Decimal("100")), quote, 1000)
        .reason
        == "inconsistent_account"
    )


def test_total_account_limit_across_symbols(store, policy, proposal, account, quote):
    account = replace(account, exposure=Decimal("900"), symbol_exposure={"OTHER": Decimal("900")})
    assert PaperRiskEngine(store, policy).evaluate(proposal, account, quote, 1000).reason == (
        "account_limit"
    )


def test_cash_includes_external_and_internal_reservations(store, policy, proposal, account, quote):
    account = replace(
        account,
        cash=Decimal("450"),
        external_reserved=Decimal("100"),
        external_symbol_reserved={"OTHER": Decimal("100")},
    )
    risk = PaperRiskEngine(store, policy)
    assert risk.evaluate(proposal, account, quote, 1000).accepted
    assert risk.evaluate(replace(proposal, id="p2"), account, quote, 1000).reason == "cash_limit"


def test_unknown_account_and_changed_price(store, policy, proposal, account, quote):
    risk = PaperRiskEngine(store, policy)
    assert risk.evaluate(proposal, replace(account, reconciled=False), quote, 1000).reason == (
        "reconciliation_required"
    )
    assert risk.evaluate(proposal, account, replace(quote, price=Decimal("120")), 1000).reason == (
        "price_drift"
    )


def test_concurrent_proposals_cannot_oversubscribe(tmp_path, policy, proposal, account, quote):
    path = str(tmp_path / "risk.sqlite3")
    Store(path).close()
    barrier = Barrier(2)
    policy = replace(policy, max_symbol_exposure=Decimal("300"))

    def submit(i):
        s = Store(path)
        try:
            barrier.wait(timeout=5)
            return (
                PaperRiskEngine(s, policy)
                .evaluate(replace(proposal, id=f"proposal-{i}"), account, quote, 1000)
                .accepted
            )
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, [1, 2])) == [False, True]
    s = Store(path)
    assert len(s.rows()) == 2
    s.close()


def test_stop_and_reservations_survive_restart(tmp_path, policy, proposal, account, quote):
    path = str(tmp_path / "restart.sqlite3")
    s = Store(path)
    PaperRiskEngine(s, policy).evaluate(proposal, account, quote, 1000)
    s.stop()
    s.close()
    s = Store(path)
    assert (
        PaperRiskEngine(s, policy).evaluate(replace(proposal, id="p2"), account, quote, 1000).reason
        == "stopped"
    )
    assert s.db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1
    s.close()


def test_audit_failure_rolls_back_reservation(store, policy, proposal, account, quote, monkeypatch):
    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(store, "record", fail)
    with pytest.raises(OSError):
        PaperRiskEngine(store, policy).evaluate(proposal, account, quote, 1000)
    assert not store.db.execute("SELECT * FROM reservations").fetchall()
