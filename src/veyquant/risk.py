from dataclasses import asdict
from decimal import Decimal

from veyquant.domain import (
    AccountSnapshot,
    Proposal,
    Quote,
    RiskPolicy,
    RiskResult,
    valid_money,
)
from veyquant.store import Store


class PaperRiskEngine:
    """Atomic paper reservation, never an authorization to send a broker order."""

    def __init__(self, store: Store, policy: RiskPolicy):
        self.store, self.policy = store, policy

    def evaluate(self, p: Proposal, a: AccountSnapshot, q: Quote, now: int) -> RiskResult:
        with self.store.transaction():
            reason = self._reason(p, a, q, now)
            if reason == "accepted_paper":
                self.store.db.execute(
                    "INSERT INTO reservations VALUES (?, ?, ?, ?)",
                    (p.id, p.symbol, str(p.limit_price * p.quantity), p.currency),
                )
            result = RiskResult(reason == "accepted_paper", reason, p.id)
            self.store.record(
                p.event_id,
                "risk",
                asdict(result)
                | {
                    "policy_version": self.policy.version,
                    "checked_at": now,
                    "account_as_of": a.as_of,
                    "quote_as_of": q.as_of,
                },
            )
            return result

    def _reason(self, p, a, q, now):
        policy = self.policy
        if self.store.db.execute("SELECT stopped FROM controls WHERE id=1").fetchone()[0]:
            return "stopped"
        if self.store.db.execute(
            "SELECT 1 FROM reservations WHERE proposal_id=?", (p.id,)
        ).fetchone():
            return "duplicate_proposal"
        if p.side != "BUY" or type(p.quantity) is not int or p.quantity <= 0:
            return "unsupported_order"
        if not valid_money(p.limit_price, q.price) or min(p.limit_price, q.price) <= 0:
            return "invalid_price"
        if not valid_money(
            a.cash,
            a.exposure,
            a.external_reserved,
            *a.symbol_exposure.values(),
            *a.external_symbol_reserved.values(),
        ):
            return "invalid_account"
        if sum(a.symbol_exposure.values()) != a.exposure or (
            sum(a.external_symbol_reserved.values()) != a.external_reserved
        ):
            return "inconsistent_account"
        if p.symbol not in policy.allowed_symbols or q.symbol != p.symbol:
            return "outside_scope"
        if len({p.currency, a.currency, q.currency, policy.currency}) != 1:
            return "currency_mismatch"
        if p.policy_version != policy.version:
            return "policy_changed"
        if not p.created_at <= now < p.expires_at:
            return "proposal_expired_or_future"
        if not all(0 <= now - t <= policy.max_age_seconds for t in [q.as_of, a.as_of]):
            return "stale_or_future_snapshot"
        if not a.reconciled:
            return "reconciliation_required"
        if abs(q.price - p.limit_price) / p.limit_price > policy.max_price_drift:
            return "price_drift"
        if not p.evidence_ids or not all([p.summary, p.counterargument, p.uncertainty]):
            return "missing_evidence"
        rows = self.store.db.execute("SELECT * FROM reservations").fetchall()
        if any(r["currency"] != policy.currency for r in rows):
            return "reservation_currency_mismatch"
        total = sum((Decimal(r["amount"]) for r in rows), Decimal(0))
        symbol = sum((Decimal(r["amount"]) for r in rows if r["symbol"] == p.symbol), Decimal(0))
        amount = p.quantity * p.limit_price
        if amount > policy.max_order:
            return "order_limit"
        if amount + total + a.external_reserved > a.cash:
            return "cash_limit"
        if amount + total + a.external_reserved + a.exposure > policy.max_exposure:
            return "account_limit"
        if (
            amount
            + symbol
            + a.symbol_exposure.get(p.symbol, Decimal(0))
            + a.external_symbol_reserved.get(p.symbol, Decimal(0))
            > policy.max_symbol_exposure
        ):
            return "symbol_limit"
        return "accepted_paper"
