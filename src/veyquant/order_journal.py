"""Durable broker observations. A vanished OPEN order is never assumed filled.

WS events are evidence only; REST detail is the canonical observed state.
This journal is not an execution ledger and cannot authorize or resend orders.
"""

import hashlib
import json
import re

from veyquant.account_readiness import amount

STATES = {
    "PENDING",
    "PARTIAL_FILLED",
    "PENDING_CANCEL",
    "PENDING_REPLACE",
    "FILLED",
    "CANCELED",
    "REJECTED",
    "REPLACED",
    "CANCEL_REJECTED",
    "REPLACE_REJECTED",
}
ACTIVE = {
    "PENDING",
    "PARTIAL_FILLED",
    "PENDING_CANCEL",
    "PENDING_REPLACE",
    "CANCEL_REJECTED",
    "REPLACE_REJECTED",
}


def order_record(order):
    if not isinstance(order, dict):
        raise ValueError("invalid_order")
    oid = order.get("orderId")
    if not isinstance(oid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", oid):
        raise ValueError("invalid_order_id")
    if order.get("status") not in STATES or order.get("side") not in {"BUY", "SELL"}:
        raise ValueError("invalid_order_state")
    if order.get("currency") not in {"KRW", "USD"}:
        raise ValueError("invalid_order_currency")
    if not isinstance(order.get("symbol"), str) or not re.fullmatch(
        r"[A-Za-z0-9.-]{1,20}", order["symbol"]
    ):
        raise ValueError("invalid_order_symbol")
    filled = amount(order["execution"]["filledQuantity"])
    quantity = amount(order["quantity"]) if order.get("quantity") is not None else None
    if quantity is None:
        amount(order["orderAmount"])
    if quantity is not None and filled > quantity:
        raise ValueError("invalid_filled_quantity")
    if order["status"] == "FILLED" and quantity is not None and filled != quantity:
        raise ValueError("invalid_filled_quantity")
    return oid, str(filled)


class OrderJournal:
    def __init__(self, db):
        self.db = db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS broker_orders (
                order_id TEXT PRIMARY KEY, state TEXT NOT NULL, filled_quantity TEXT NOT NULL,
                payload TEXT NOT NULL, observed_at REAL NOT NULL, needs_review INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS broker_order_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT UNIQUE NOT NULL,
                source TEXT NOT NULL, payload TEXT NOT NULL, received_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS order_followups (
                order_id TEXT PRIMARY KEY, queued_at REAL NOT NULL
            );
        """)

    def event(self, source, data, now):
        raw = json.dumps(data, sort_keys=True, separators=(",", ":"))
        if len(raw) > 32000:
            raise ValueError("order_event_too_large")
        fingerprint = hashlib.sha256((source + raw).encode()).hexdigest()
        self.db.execute(
            "INSERT OR IGNORE INTO broker_order_events(fingerprint,source,payload,received_at) "
            "VALUES (?,?,?,?)",
            (fingerprint, source, raw, now),
        )
        if source == "websocket" and isinstance(data.get("order"), dict):
            oid = data["order"].get("orderId")
            if not isinstance(oid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", oid):
                raise ValueError("invalid_order_id")
            self.db.execute("INSERT OR REPLACE INTO order_followups VALUES (?,?)", (oid, now))

    def observe(self, order, now):
        oid, filled = order_record(order)
        old = self.db.execute(
            "SELECT state,filled_quantity,observed_at FROM broker_orders WHERE order_id=?", (oid,)
        ).fetchone()
        self.event("rest", order, now)
        if old and (
            now < old[2]
            or amount(filled) < amount(old[1])
            or (old[0] not in ACTIVE and order["status"] in ACTIVE)
        ):
            # Never silently replace a later/filled state with an older observation.
            self.db.execute("UPDATE broker_orders SET needs_review=1 WHERE order_id=?", (oid,))
            return
        self.db.execute(
            "INSERT INTO broker_orders VALUES (?,?,?,?,?,0) ON CONFLICT(order_id) DO UPDATE SET "
            "state=excluded.state,filled_quantity=excluded.filled_quantity,payload=excluded.payload,"
            "observed_at=excluded.observed_at,needs_review=0",
            (oid, order["status"], filled, json.dumps(order), now),
        )
        self.db.execute("DELETE FROM order_followups WHERE order_id=? AND queued_at<=?", (oid, now))

    def missing_from_open(self, orders):
        present = {order_record(o)[0] for o in orders}
        missing = []
        for oid, state, review in self.db.execute(
            "SELECT order_id,state,needs_review FROM broker_orders ORDER BY observed_at"
        ):
            if (state in ACTIVE and oid not in present) or review:
                missing.append(oid)
        for oid in missing:
            self.db.execute("UPDATE broker_orders SET needs_review=1 WHERE order_id=?", (oid,))
        queued = [
            r[0] for r in self.db.execute("SELECT order_id FROM order_followups ORDER BY queued_at")
        ]
        return list(dict.fromkeys(missing + queued))

    def counts(self):
        return {
            "tracked_orders": self.db.execute("SELECT COUNT(*) FROM broker_orders").fetchone()[0],
            "order_events": self.db.execute("SELECT COUNT(*) FROM broker_order_events").fetchone()[
                0
            ],
            "orders_needing_review": self.db.execute(
                "SELECT COUNT(*) FROM (SELECT order_id FROM broker_orders WHERE needs_review=1 "
                "UNION SELECT order_id FROM order_followups)"
            ).fetchone()[0],
        }
