"""Durable cash-equity execution core, called only by the local trusted worker.

The trusted caller must supply a fresh, independently reconciled account snapshot,
a dated loss baseline, the current owner policy, and an explicitly armed worker.
Model output cannot set any of these. Ambiguous writes are never automatically retried.
"""

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from time import monotonic
from zoneinfo import ZoneInfo

from veyquant.operating_policy import validate_limits
from veyquant.shadow_contract import domestic_symbol
from veyquant.store import Store

D = Decimal
ACTIVE = {
    "PREPARED",
    "SENDING",
    "UNKNOWN",
    "ACKNOWLEDGED",
    "PARTIAL",
    "CANCEL_SENDING",
    "CANCEL_PENDING",
    "CANCEL_UNKNOWN",
    "REVIEW",
}
TERMINAL = {"FILLED", "CANCELED", "REJECTED", "VOID"}


class DispatchVeto(RuntimeError):
    """The transport has not written an order because final authorization failed."""


def money(value):
    if not isinstance(value, (str, Decimal)) or not re.fullmatch(
        r"[0-9]{1,18}(?:\.[0-9]{1,8})?", str(value)
    ):
        raise ValueError("invalid_amount")
    return D(value)


def day_key(now):
    return datetime.fromtimestamp(now, ZoneInfo("Asia/Seoul")).date().isoformat()


@dataclass(frozen=True)
class Intent:
    id: str
    symbol: str
    side: str
    quantity: int
    limit_price: str
    fee_buffer: str
    policy_revision: int
    created_at: float
    expires_at: float

    def validate(self):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,36}", self.id):
            raise ValueError("invalid_intent_id")
        if not domestic_symbol(self.symbol) or self.side not in {"BUY", "SELL"}:
            raise ValueError("unsupported_scope")
        if type(self.quantity) is not int or not 0 < self.quantity <= 10**9:
            raise ValueError("invalid_quantity")
        price = money(self.limit_price)
        if price <= 0 or price != price.to_integral_value():
            raise ValueError("invalid_limit_price")
        money(self.fee_buffer)
        if type(self.policy_revision) is not int or self.policy_revision <= 0:
            raise ValueError("invalid_policy_revision")
        if not 0 < self.expires_at - self.created_at <= 120:
            raise ValueError("invalid_intent_lifetime")

    def body(self):
        self.validate()
        return {
            "clientOrderId": self.id,
            "symbol": self.symbol,
            "side": self.side,
            "orderType": "LIMIT",
            "timeInForce": "DAY",
            "quantity": str(self.quantity),
            "price": self.limit_price,
        }


@dataclass(frozen=True)
class ExecutionSnapshot:
    as_of: float
    cash_buying_power: str
    external_buy_commitments: str
    quotes: dict[str, tuple[str, float]]
    managed_quantities: dict[str, int]
    sellable_quantities: dict[str, int]
    known_open_order_ids: frozenset[str]
    reconciled: bool
    regular_session_open: bool


class ExecutionCore:
    def __init__(self, store: Store, *, armed=False, current_policy=None):
        self.store, self.db, self.armed = store, store.db, armed
        self.current_policy = current_policy
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS execution_orders (
                id TEXT PRIMARY KEY, intent TEXT NOT NULL, state TEXT NOT NULL,
                broker_id TEXT UNIQUE, filled_quantity INTEGER NOT NULL DEFAULT 0,
                filled_amount TEXT NOT NULL DEFAULT '0', fees TEXT NOT NULL DEFAULT '0',
                tax TEXT NOT NULL DEFAULT '0', updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS execution_positions (
                symbol TEXT PRIMARY KEY, quantity INTEGER NOT NULL, basis TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS execution_fills (
                id TEXT PRIMARY KEY, order_id TEXT NOT NULL, quantity INTEGER NOT NULL,
                amount TEXT NOT NULL, costs TEXT NOT NULL, realized TEXT NOT NULL,
                observed_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS execution_days (
                day TEXT PRIMARY KEY, baseline TEXT NOT NULL, breached INTEGER NOT NULL DEFAULT 0
            );
        """)

    def install_loss_baseline(self, day: str, total_pnl: Decimal):
        """Trusted reconciler only; once per KST day, never reset after loss or restart."""
        if (
            datetime.strptime(day, "%Y-%m-%d").date().isoformat() != day
            or not total_pnl.is_finite()
        ):
            raise ValueError("invalid_loss_baseline")
        with self.store.transaction():
            previous = self.db.execute(
                "SELECT baseline FROM execution_days WHERE day=?", (day,)
            ).fetchone()
            if previous and D(previous[0]) != total_pnl:
                raise ValueError("baseline_already_installed")
            self.db.execute(
                "INSERT OR IGNORE INTO execution_days(day,baseline) VALUES (?,?)",
                (day, str(total_pnl)),
            )

    def _positions(self):
        return {
            r["symbol"]: (r["quantity"], D(r["basis"]))
            for r in self.db.execute("SELECT * FROM execution_positions WHERE quantity>0")
        }

    def capital_used(self):
        """Net cash committed from the owner's budget, including actual trading costs.

        Falling prices must not make unrelated cash in the broker account available.
        Sales release only the proceeds actually received, after fees and tax.
        """
        total = D(0)
        for row in self.db.execute("SELECT intent,filled_amount,fees,tax FROM execution_orders"):
            amount, costs = D(row["filled_amount"]), D(row["fees"]) + D(row["tax"])
            total += (
                amount + costs if json.loads(row["intent"])["side"] == "BUY" else costs - amount
            )
        return total

    def _valuation(self, snapshot, now, *, max_price_age=5):
        if type(max_price_age) not in (int, float) or not 0 < max_price_age <= 30:
            raise ValueError("invalid_valuation_age")
        positions = self._positions()
        if snapshot.reconciled is not True or {s: q for s, (q, _) in positions.items()} != {
            s: q for s, q in snapshot.managed_quantities.items() if q
        }:
            raise ValueError("reconciliation_required")
        if any(type(q) is not int or q < 0 for q in snapshot.managed_quantities.values()):
            raise ValueError("invalid_account_quantity")
        if not 0 <= now - snapshot.as_of <= 5:
            raise ValueError("stale_account")
        value, unrealized = D(0), D(0)
        for symbol, (qty, basis) in positions.items():
            price, stamp = snapshot.quotes[symbol]
            if not 0 <= now - stamp <= max_price_age or money(price) <= 0:
                raise ValueError("stale_price")
            marked = qty * money(price)
            value += marked
            unrealized += marked - basis
        realized = sum(
            (D(r[0]) for r in self.db.execute("SELECT realized FROM execution_fills")), D(0)
        )
        return value, realized + unrealized

    def _reason(self, intent, snapshot, policy, now, exclude=None):
        if self.armed is not True:
            return "worker_not_armed"
        if policy.get("live_requested") is not True or policy.get("live_enabled") is not True:
            return "live_not_enabled"
        if self.db.execute("SELECT stopped FROM controls WHERE id=1").fetchone()[0]:
            return "stopped"
        if not policy.get("onboarding_completed") or intent.policy_revision != policy.get(
            "revision"
        ):
            return "settings_changed"
        limits = validate_limits(policy["limits"])
        if not intent.created_at <= now < intent.expires_at:
            return "intent_expired"
        if snapshot.regular_session_open is not True:
            return "outside_regular_session"
        try:
            value, pnl = self._valuation(snapshot, now)
            price, stamp = snapshot.quotes[intent.symbol]
            if not 0 <= now - stamp <= 5 or money(price) <= 0:
                return "stale_price"
            if abs(money(price) - money(intent.limit_price)) / money(intent.limit_price) > D(
                "0.005"
            ):
                return "price_changed"
            cash = money(snapshot.cash_buying_power)
            external = money(snapshot.external_buy_commitments)
        except (ValueError, KeyError, TypeError):
            return "reconciliation_required"
        day = self.db.execute(
            "SELECT * FROM execution_days WHERE day=?", (day_key(now),)
        ).fetchone()
        if day is None:
            return "daily_baseline_required"
        if pnl - D(day["baseline"]) <= -D(limits["max_daily_loss_krw"]):
            self.db.execute("UPDATE execution_days SET breached=1 WHERE day=?", (day_key(now),))
        if day["breached"] or pnl - D(day["baseline"]) <= -D(limits["max_daily_loss_krw"]):
            return "daily_loss_limit"
        pending = self.db.execute("SELECT * FROM execution_orders").fetchall()
        if any(
            r["state"] in {"UNKNOWN", "CANCEL_UNKNOWN", "REVIEW", "SENDING", "CANCEL_SENDING"}
            and r["id"] != exclude
            for r in pending
        ):
            return "unresolved_order"
        spend = intent.quantity * money(intent.limit_price) + money(intent.fee_buffer)
        if intent.side == "BUY" and spend > D(limits["max_order_krw"]):
            return "order_limit"
        committed, unreflected, sell_reserved = D(0), D(0), 0
        for row in pending:
            if row["id"] == exclude or row["state"] not in ACTIVE:
                continue
            old = Intent(**json.loads(row["intent"]))
            remaining = old.quantity - row["filled_quantity"]
            if old.side == "BUY":
                amount = remaining * money(old.limit_price) + money(old.fee_buffer)
                committed += amount
                if row["broker_id"] not in snapshot.known_open_order_ids:
                    unreflected += amount
            elif old.symbol == intent.symbol:
                sell_reserved += remaining
        if intent.side == "BUY":
            if spend + unreflected > cash:
                return "cash_limit"
            if spend + committed + external + self.capital_used() > D(limits["capital_krw"]):
                return "capital_limit"
            if spend + committed + external + value > D(limits["capital_krw"]):
                return "capital_limit"
        else:
            owned = self._positions().get(intent.symbol, (0, D(0)))[0]
            sellable = snapshot.sellable_quantities.get(intent.symbol)
            if type(sellable) is not int or sellable < 0:
                return "sellable_unknown"
            # Conservative: outstanding sell reservations may also be reflected by broker.
            if intent.quantity + sell_reserved > min(owned, sellable):
                return "managed_quantity_limit"
        return "accepted"

    def monitor_loss(self, snapshot, policy, now):
        """Independent of model completion; stale/missing state never reports a zero loss."""
        limits = validate_limits(policy["limits"])
        with self.store.transaction():
            try:
                _, total_pnl = self._valuation(snapshot, now)
            except (ValueError, KeyError, TypeError):
                return {"state": "reconciliation_required"}
            day = day_key(now)
            baseline = self.db.execute(
                "SELECT * FROM execution_days WHERE day=?", (day,)
            ).fetchone()
            if baseline is None:
                return {"state": "daily_baseline_required"}
            pnl = total_pnl - D(baseline["baseline"])
            breached = bool(baseline["breached"]) or pnl <= -D(limits["max_daily_loss_krw"])
            if breached:
                self.db.execute("UPDATE execution_days SET breached=1 WHERE day=?", (day,))
            return {
                "state": "breached" if breached else "within_limit",
                "day": day,
                "pnl_krw": str(pnl),
                "new_orders_blocked": breached,
            }

    def prepare(self, intent: Intent, snapshot, policy, now):
        intent.validate()
        with self.store.transaction():
            if self.db.execute(
                "SELECT 1 FROM execution_orders WHERE id=?", (intent.id,)
            ).fetchone():
                return "duplicate_intent"
            reason = self._reason(intent, snapshot, policy, now)
            self.store.record(
                intent.id,
                "execution_preflight",
                {"reason": reason, "revision": intent.policy_revision, "at": now},
            )
            if reason == "accepted":
                self.db.execute(
                    "INSERT INTO execution_orders(id,intent,state,updated_at) "
                    "VALUES (?,?,'PREPARED',?)",
                    (intent.id, json.dumps(asdict(intent)), now),
                )
            return reason

    def begin_dispatch(self, oid, snapshot, policy, now):
        with self.store.transaction():
            row = self.db.execute("SELECT * FROM execution_orders WHERE id=?", (oid,)).fetchone()
            if row is None or row["state"] != "PREPARED":
                return None
            intent = Intent(**json.loads(row["intent"]))
            reason = self._reason(intent, snapshot, policy, now, exclude=oid)
            state = "SENDING" if reason == "accepted" else "VOID"
            self.db.execute(
                "UPDATE execution_orders SET state=?,updated_at=? WHERE id=?", (state, now, oid)
            )
            self.store.record(
                oid, "execution_dispatch", {"state": state, "reason": reason, "at": now}
            )
            return intent if state == "SENDING" else None

    async def submit(self, oid, transport, snapshot, policy, now):
        started = monotonic()
        intent = self.begin_dispatch(oid, snapshot, policy, now)
        if intent is None:
            return

        def before_send():
            # Called synchronously after token refresh, immediately before HTTP order write.
            with self.store.transaction():
                row = self.db.execute(
                    "SELECT state FROM execution_orders WHERE id=?", (oid,)
                ).fetchone()
                if row is None or row[0] != "SENDING" or self.current_policy is None:
                    return False
                fresh_policy = self.current_policy()
                reason = self._reason(
                    intent, snapshot, fresh_policy, now + monotonic() - started, exclude=oid
                )
                self.store.record(oid, "execution_final_check", {"reason": reason})
                return reason == "accepted"

        try:
            result = await transport.create(intent, before_send=before_send)
            broker_id = result["orderId"]
            if (
                not isinstance(broker_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", broker_id)
                or result.get("clientOrderId") != oid
            ):
                raise ValueError("invalid_order_ack")
            with self.store.transaction():
                self.db.execute(
                    "UPDATE execution_orders SET state='ACKNOWLEDGED',broker_id=? "
                    "WHERE id=? AND state='SENDING'",
                    (broker_id, oid),
                )
                self.store.record(oid, "order_acknowledged", {"at": now})
        except DispatchVeto:
            with self.store.transaction():
                self.db.execute(
                    "UPDATE execution_orders SET state='VOID' WHERE id=? AND state='SENDING'",
                    (oid,),
                )
                self.store.record(oid, "execution_final_check_veto", {"at": now})
        except Exception:
            with self.store.transaction():
                self.db.execute(
                    "UPDATE execution_orders SET state='UNKNOWN' WHERE id=? AND state='SENDING'",
                    (oid,),
                )
                self.store.record(
                    oid, "order_outcome_unknown", {"at": now, "automatic_retry": False}
                )

    async def cancel(self, oid, transport, now):
        if self.armed is not True:
            return
        with self.store.transaction():
            row = self.db.execute("SELECT * FROM execution_orders WHERE id=?", (oid,)).fetchone()
            if (
                row is None
                or row["state"] not in {"ACKNOWLEDGED", "PARTIAL"}
                or not row["broker_id"]
            ):
                return
            self.db.execute(
                "UPDATE execution_orders SET state='CANCEL_SENDING',updated_at=? WHERE id=?",
                (now, oid),
            )
            self.store.record(oid, "cancel_requested", {"at": now})
        try:
            result = await transport.cancel(row["broker_id"])
            if not isinstance(result.get("orderId"), str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,200}", result["orderId"]
            ):
                raise ValueError("invalid_cancel_ack")
            state = "CANCEL_PENDING"
        except Exception:
            state = "CANCEL_UNKNOWN"
        with self.store.transaction():
            # A cancellation acknowledgment is not proof that the original order is canceled.
            self.db.execute(
                "UPDATE execution_orders SET state=? WHERE id=? AND state='CANCEL_SENDING'",
                (state, oid),
            )
            self.store.record(oid, "cancel_waiting_for_reconciliation", {"state": state, "at": now})

    def recover(self, now):
        self.armed = False
        with self.store.transaction():
            self.db.execute("UPDATE execution_orders SET state='VOID' WHERE state='PREPARED'")
            self.db.execute("UPDATE execution_orders SET state='UNKNOWN' WHERE state='SENDING'")
            self.db.execute(
                "UPDATE execution_orders SET state='CANCEL_UNKNOWN' WHERE state='CANCEL_SENDING'"
            )
            self.store.record("execution", "recovery_requires_reconciliation", {"at": now})
        return [
            r["broker_id"]
            for r in self.db.execute("SELECT * FROM execution_orders")
            if r["state"] in ACTIVE and r["broker_id"]
        ]

    def observe(self, oid, detail, now):
        """Apply a verified cumulative REST fill exactly once; never match unknowns by price."""
        try:
            with self.store.transaction():
                row = self.db.execute(
                    "SELECT * FROM execution_orders WHERE id=?", (oid,)
                ).fetchone()
                if row is None or not row["broker_id"] or detail["orderId"] != row["broker_id"]:
                    raise ValueError("order_identity_mismatch")
                intent = Intent(**json.loads(row["intent"]))
                if (
                    now < row["updated_at"]
                    or detail.get("orderType") != "LIMIT"
                    or money(detail.get("price")) != money(intent.limit_price)
                ):
                    raise ValueError("order_scope_or_time_changed")
                if (detail["symbol"], detail["side"], detail["currency"], detail["quantity"]) != (
                    intent.symbol,
                    intent.side,
                    "KRW",
                    str(intent.quantity),
                ):
                    raise ValueError("order_scope_changed")
                status = detail["status"]
                states = {
                    "PENDING": "ACKNOWLEDGED",
                    "PARTIAL_FILLED": "PARTIAL",
                    "FILLED": "FILLED",
                    "CANCELED": "CANCELED",
                    "REJECTED": "REJECTED",
                    "PENDING_CANCEL": "CANCEL_PENDING",
                }
                if status not in states:
                    raise ValueError("unsupported_order_transition")
                execution = detail["execution"]
                quantity = money(execution["filledQuantity"])
                if (
                    quantity != quantity.to_integral_value()
                    or not row["filled_quantity"] <= quantity <= intent.quantity
                ):
                    raise ValueError("filled_quantity_regressed")
                filled = int(quantity)
                if status == "FILLED" and filled != intent.quantity:
                    raise ValueError("incomplete_fill")
                if row["state"] in TERMINAL and states[status] != row["state"]:
                    raise ValueError("terminal_state_changed")
                amount = money(execution["filledAmount"]) if filled else D(0)
                fees = money(execution["commission"]) if filled else D(0)
                tax = money(execution["tax"]) if filled else D(0)
                dq = filled - row["filled_quantity"]
                da = amount - D(row["filled_amount"])
                dc = fees + tax - D(row["fees"]) - D(row["tax"])
                if da < 0 or (dq == 0 and da != 0) or (dq > 0 and (da <= 0 or dc < 0)):
                    raise ValueError("fill_correction_requires_review")
                if dq == 0 and dc:
                    # Later broker fee/tax corrections are cash expenses or refunds.
                    # They change total P&L once, without inventing another share fill.
                    adjustment = self.db.execute("SELECT COUNT(*) FROM execution_fills").fetchone()[
                        0
                    ]
                    self.db.execute(
                        "INSERT INTO execution_fills VALUES (?,?,?,?,?,?,?)",
                        (
                            f"{oid}:cost:{adjustment}",
                            oid,
                            0,
                            "0",
                            str(dc),
                            str(-dc),
                            now,
                        ),
                    )
                if dq:
                    positions = self._positions()
                    owned, basis = positions.get(intent.symbol, (0, D(0)))
                    realized = D(0)
                    if intent.side == "BUY":
                        owned += dq
                        basis += da + dc
                    else:
                        if dq > owned:
                            raise ValueError("unmanaged_sale")
                        removed = basis if dq == owned else basis * D(dq) / D(owned)
                        owned -= dq
                        basis -= removed
                        realized = da - dc - removed
                    self.db.execute(
                        "INSERT INTO execution_positions VALUES (?,?,?) "
                        "ON CONFLICT(symbol) DO UPDATE "
                        "SET quantity=excluded.quantity,basis=excluded.basis",
                        (intent.symbol, owned, str(basis)),
                    )
                    self.db.execute(
                        "INSERT INTO execution_fills VALUES (?,?,?,?,?,?,?)",
                        (f"{oid}:{filled}", oid, dq, str(da), str(dc), str(realized), now),
                    )
                self.db.execute(
                    "UPDATE execution_orders SET state=?,filled_quantity=?,filled_amount=?,fees=?,"
                    "tax=?,updated_at=? WHERE id=?",
                    (states[status], filled, str(amount), str(fees), str(tax), now, oid),
                )
                self.store.record(
                    oid,
                    "order_reconciled",
                    {"state": states[status], "filled_quantity": filled, "at": now},
                )
            return True
        except (ValueError, KeyError, TypeError):
            with self.store.transaction():
                self.db.execute("UPDATE execution_orders SET state='REVIEW' WHERE id=?", (oid,))
                self.store.record(oid, "order_review_required", {"at": now})
            return False
