"""Guarded KRW cash trading inside the collector's single OAuth session.

The owner-only control file supplies policy, never broker facts. Decision-v2
exports exact trade intents; this worker validates them against fresh broker
state and owner limits without resizing or treating model output as authority.
"""

import asyncio
import hashlib
import json
import time
from dataclasses import replace
from datetime import datetime
from decimal import ROUND_CEILING, Decimal

from veyquant.account_readiness import amount, build_account_view
from veyquant.adapters.toss import TossError
from veyquant.execution import ACTIVE, ExecutionCore, ExecutionSnapshot, Intent, day_key
from veyquant.operating_policy import validate_limits
from veyquant.performance import monthly_performance
from veyquant.shadow_contract import atomic_json, bounded_json, domestic_symbol, finite_time
from veyquant.store import Store
from veyquant.trading_state import MESSAGES, completed_bars
from veyquant.universe import MAX_WATCH, validate_trade_stock
from veyquant.valuation_marks import bid_mark, portfolio_mark

D = Decimal


def shares(value):
    n = amount(value)
    if n != n.to_integral_value() or n > 10**9:
        raise ValueError("invalid_share_quantity")
    return int(n)


class LiveWorker:
    def __init__(
        self,
        path,
        control,
        reports,
        status,
        observations,
        broker,
        transport,
        account,
        clock=time.time,
    ):
        self.store = Store(path)
        self.db = self.store.db
        self.control_path, self.report_path, self.status_path = control, reports, status
        self.observations, self.broker, self.transport, self.account = (
            observations,
            broker,
            transport,
            account,
        )
        self.clock = clock
        self.generation = -1
        self.core = ExecutionCore(self.store, current_policy=self.current_policy)
        self.core.recover(clock())
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS execution_anchor(
                symbol TEXT PRIMARY KEY, quantity INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS execution_decisions(
                id TEXT PRIMARY KEY, reason TEXT NOT NULL, at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS performance_months(
                month TEXT PRIMARY KEY, baseline TEXT NOT NULL,
                source TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS performance_marks(
                month TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS execution_commands(
                id TEXT PRIMARY KEY, result TEXT NOT NULL, at REAL NOT NULL);
        """)
        self.metadata = None
        self.metadata_at = 0
        self.last_reason = "waiting_for_analysis"
        self.snapshot = None
        self.bars = {}
        self.execution_pins = {}
        self.rest_valuation_marks = {}
        self.valuation_attempts = {}

    def control(self):
        data = bounded_json(self.control_path, 32768)
        now = self.clock()
        if (
            not finite_time(data["updated_at"])
            or not 0 <= now - data["updated_at"] <= 10
            or data.get("owner_bound") is not True
            or data.get("version") != 1
        ):
            raise ValueError("control_unavailable")
        policy = data["policy"]
        if type(policy["revision"]) is not int or type(policy["live_requested"]) is not bool:
            raise ValueError("control_unavailable")
        if policy["configured"]:
            validate_limits(policy["limits"])
        return data

    def current_policy(self):
        try:
            data = self.control()
            p = data["policy"]
            allowed = (
                p["live_requested"]
                and p["onboarding_completed"]
                and p["model_connection"]["ready"] is True
                and data["stopped"] is False
                and self.observations.connected
                and self.generation == self.observations.order_generation
            )
            return p | {"live_enabled": bool(allowed)}
        except (OSError, ValueError, KeyError, TypeError):
            return {"live_requested": False, "live_enabled": False}

    async def metadata_refresh(self):
        now = self.clock()
        if (
            self.metadata is None
            or now - self.metadata_at >= 1800
            or day_key(now) != day_key(self.metadata_at)
        ):
            calendar, conditionals, commissions = await asyncio.gather(
                self.broker.market_calendar(),
                self.broker.conditional_orders(self.account),
                self.broker.commissions(self.account),
            )
            rates = [
                r
                for r in commissions
                if r["marketCountry"] == "KR"
                and (r.get("startDate") is None or r["startDate"] <= day_key(now))
                and (r.get("endDate") is None or r["endDate"] >= day_key(now))
            ]
            if len(rates) != 1 or not 0 <= amount(rates[0]["commissionRate"]) <= D("0.05"):
                raise ValueError("invalid_commission")
            self.metadata = {
                "calendar": calendar,
                "conditionals": conditionals,
                "rate": amount(rates[0]["commissionRate"]),
            }
            self.metadata_at = now

    def pinned_symbols(self):
        symbols = set(self.core._positions())
        for row in self.db.execute("SELECT intent,state FROM execution_orders"):
            if row["state"] in ACTIVE:
                symbols.add(json.loads(row["intent"])["symbol"])
        self.execution_pins = {s: t for s, t in self.execution_pins.items() if t > self.clock()}
        symbols.update(self.execution_pins)
        return tuple(sorted(symbols))

    async def daily_bars(self, symbol):
        now = self.clock()
        cached = self.bars.get(symbol)
        if (
            cached is None
            or now - cached["updated_at"] >= 1800
            or day_key(now) != day_key(cached["updated_at"])
        ):
            rows = completed_bars(await self.broker.daily_candles(symbol), now)
            cached = {"symbol": symbol, "updated_at": now, "bars": rows}
            self.bars[symbol] = cached
        return cached["bars"]

    def valuation_quotes(self):
        now = self.clock()
        quotes = {s: (q["price"], q["as_of"]) for s, q in self.observations.quotes.items()}
        for symbol, (quantity, _) in self.core._positions().items():
            if quantity > 0:
                mark = portfolio_mark(
                    self.observations, symbol, self.observations.quotes.get(symbol), now, 5
                )
                if mark is None and self.observations.connected:
                    cached = self.rest_valuation_marks.get(symbol)
                    if cached and 0 <= now - cached["as_of"] <= 5:
                        mark = cached
                if mark:
                    quotes[symbol] = (mark["price"], mark["as_of"])
        return quotes

    async def refresh_valuation_marks(self, positions):
        # Supplement quiet WS channels with a bounded read of the broker's book.
        # Never substitute HTTP receipt time for the source's market timestamp.
        if not self.observations.connected or not callable(getattr(self.broker, "orderbook", None)):
            return
        from veyquant.surveillance import session

        now = self.clock()
        current = session(self.metadata["calendar"], now) if self.metadata else None
        if not current or not current["open"] <= now < current["close"]:
            return
        self.rest_valuation_marks = {
            s: m for s, m in self.rest_valuation_marks.items() if s in positions
        }
        self.valuation_attempts = {
            s: t for s, t in self.valuation_attempts.items() if s in positions
        }
        gate = asyncio.Semaphore(3)

        async def refresh(symbol):
            if portfolio_mark(
                self.observations, symbol, self.observations.quotes.get(symbol), now, 5
            ):
                return
            cached = self.rest_valuation_marks.get(symbol)
            if cached and 0 <= now - cached["as_of"] <= 3:
                return
            if now - self.valuation_attempts.get(symbol, 0) < 3:
                return
            async with gate:
                self.valuation_attempts[symbol] = self.clock()
                try:
                    async with asyncio.timeout(2):
                        book = await self.broker.orderbook(symbol)
                    mark = bid_mark(symbol, book, self.clock(), 5)
                except (TossError, OSError, ValueError, KeyError, TypeError, TimeoutError):
                    mark = None
                if mark:
                    self.rest_valuation_marks[symbol] = mark
                else:
                    self.rest_valuation_marks.pop(symbol, None)

        try:
            async with asyncio.timeout(3):
                await asyncio.gather(*(refresh(s) for s in positions))
        except TimeoutError:
            pass  # Missing/stale marks still fail valuation and block execution.

    async def reconcile(self):
        await self.metadata_refresh()
        # Reconcile known orders before reading holdings, including terminal fee corrections.
        rows = self.db.execute("SELECT * FROM execution_orders ORDER BY updated_at DESC").fetchall()
        active_rows = [r for r in rows if r["state"] in ACTIVE and r["broker_id"]]
        corrections = [
            r
            for r in reversed(rows)
            if r["broker_id"]
            and r["state"] not in ACTIVE
            and self.clock() - r["updated_at"] >= 300
            and self.clock() - json.loads(r["intent"])["created_at"] < 30 * 86400
        ]
        for r in active_rows + corrections[:1]:
            if r["broker_id"]:
                detail = await self.broker.order(self.account, r["broker_id"])
                self.core.observe(r["id"], detail, self.clock())
                self.observations.journal.observe(detail, self.clock())
        positions = self.core._positions()
        if len(positions) > MAX_WATCH:
            raise ValueError("subscription_capacity")
        day = day_key(self.clock())
        if self.db.execute("SELECT 1 FROM execution_days WHERE day=?", (day,)).fetchone() is None:
            for symbol in positions:
                await self.daily_bars(symbol)
        await self.refresh_valuation_marks(positions)
        started = self.clock()
        generation = self.observations.order_generation
        holdings, orders, cash, conditionals = await asyncio.gather(
            self.broker.holdings(self.account),
            self.broker.open_orders(self.account),
            self.broker.buying_power(self.account),
            self.broker.conditional_orders(self.account),
        )
        now = self.clock()
        view = build_account_view(
            holdings, orders, cash, conditionals, self.metadata["calendar"], started
        )
        self.observations.snapshot(holdings, orders, started)
        self.observations.account_view = view
        self.observations.account_issue = None
        if generation != self.observations.order_generation or not self.observations.connected:
            raise ValueError("reconciliation_required")
        if now - started > 5:
            raise ValueError("account_unavailable")
        self.generation = generation
        quantities = {}
        for row in holdings["items"]:
            if row["currency"] == "KRW" and domestic_symbol(row["symbol"]):
                symbol = row["symbol"]
                quantities[symbol] = quantities.get(symbol, 0) + shares(row["quantity"])
        positions = self.core._positions()
        own = {s: q for s, (q, _) in positions.items()}
        active = any(r["state"] in ACTIVE for r in rows)
        if any(
            r["state"] in {"UNKNOWN", "REVIEW", "SENDING", "CANCEL_UNKNOWN", "CANCEL_SENDING"}
            for r in rows
        ):
            raise ValueError("order_review")
        anchors = {
            r["symbol"]: r["quantity"] for r in self.db.execute("SELECT * FROM execution_anchor")
        }
        # Persist per-symbol external ownership. A new symbol can establish its
        # anchor even when other symbols already have managed trades.
        for symbol in set(quantities) | set(own) | set(anchors):
            quantity, managed = quantities.get(symbol, 0), own.get(symbol, 0)
            symbol_rows = [r for r in rows if json.loads(r["intent"])["symbol"] == symbol]
            symbol_active = any(r["state"] in ACTIVE for r in symbol_rows)
            anchor = anchors.get(symbol)
            if anchor is None:
                if managed or symbol_active:
                    raise ValueError("position_mismatch")
                anchor = quantity
                self.db.execute("INSERT INTO execution_anchor VALUES (?,?)", (symbol, anchor))
            if not managed and not symbol_active and quantity != anchor:
                anchor = quantity
                self.db.execute(
                    "UPDATE execution_anchor SET quantity=? WHERE symbol=?", (anchor, symbol)
                )
            if quantity != anchor + managed:
                raise ValueError("position_mismatch")
        ids = {r["broker_id"] for r in rows if r["broker_id"]}
        if (
            any(o["orderId"] not in ids for o in orders["orders"])
            or conditionals["conditionalOrders"]
        ):
            raise ValueError("external_orders")
        self.snapshot = ExecutionSnapshot(
            as_of=started,
            cash_buying_power=view["cash_buying_power_krw"],
            external_buy_commitments="0",
            quotes=self.valuation_quotes(),
            managed_quantities=own,
            sellable_quantities={},
            known_open_order_ids=frozenset(o["orderId"] for o in orders["orders"]),
            reconciled=True,
            regular_session_open=view["regular_start"] is not None
            and view["regular_start"] <= now < view["regular_end"],
        )
        day = day_key(now)
        if self.db.execute("SELECT 1 FROM execution_days WHERE day=?", (day,)).fetchone() is None:
            # DAY orders from earlier sessions must settle before the new day's baseline.
            if active or any(day_key(json.loads(r["intent"])["created_at"]) == day for r in rows):
                raise ValueError("daily_baseline_required")
            realized = sum(
                (D(r[0]) for r in self.db.execute("SELECT realized FROM execution_fills")), D(0)
            )
            baseline = realized
            for symbol, (quantity, basis) in positions.items():
                bars = await self.daily_bars(symbol)
                previous = self.metadata["calendar"]["previousBusinessDay"]["date"]
                if not bars or bars[-1]["date"] != previous:
                    raise ValueError("daily_baseline_required")
                baseline += quantity * D(bars[-1]["close"]) - basis
            self.core.install_loss_baseline(day, baseline)
        return self.snapshot

    def remember(self, event_id, reason):
        self.db.execute(
            "INSERT OR IGNORE INTO execution_decisions VALUES (?,?,?)",
            (event_id, reason, self.clock()),
        )
        self.last_reason = reason

    def proposal(self, policy):
        data = bounded_json(self.report_path, 2 * 1024 * 1024)
        now = self.clock()
        if not finite_time(data["updated_at"]) or not 0 <= now - data["updated_at"] <= 360:
            return None
        if data.get("protocol") == "decision-v2":
            return self.decision_proposal(data, policy)
        reports = data["reports"]
        if not isinstance(reports, list) or len(reports) > 10:
            raise ValueError("invalid_reports")
        for report in reports:
            event_id = report.get("event_id")
            if (
                not isinstance(event_id, str)
                or len(event_id) != 64
                or any(c not in "0123456789abcdef" for c in event_id)
            ):
                continue
            if self.db.execute(
                "SELECT 1 FROM execution_decisions WHERE id=?", (event_id,)
            ).fetchone():
                continue
            if report.get("settings_revision") != policy["revision"]:
                self.remember(event_id, "settings_changed")
                continue
            if (
                not finite_time(report.get("created_at"))
                or not 0 <= now - report["created_at"] <= 90
                or not 0 <= now - report["quote"]["as_of"] <= 420
                or report["created_at"] < policy["updated_at"]
            ):
                self.remember(event_id, "report_expired")
                continue
            if report.get("outcome") not in {"buy", "sell"}:
                self.remember(event_id, "provider_hold")
                continue
            if not domestic_symbol(report.get("symbol")) or report.get("proposal") != {
                "side": report["outcome"].upper()
            }:
                self.remember(event_id, "provider_hold")
                continue
            stages = report.get("stages", [])
            if (
                len(stages) < 3
                or stages[-1].get("role") != "research"
                or any(s.get("status") != "received" for s in stages)
            ):
                self.remember(event_id, "provider_hold")
                continue
            return report
        return None

    def pending_decision(self):
        try:
            if not self.control()["policy"]["live_requested"]:
                return False
            data = bounded_json(self.report_path, 2 * 1024 * 1024)
            return any(
                0 <= self.clock() - p["created_at"] <= 90
                and any(
                    not self.db.execute(
                        "SELECT 1 FROM execution_decisions WHERE id=?",
                        (hashlib.sha256(f"{p['id']}:{n}".encode()).hexdigest(),),
                    ).fetchone()
                    for n in range(len(p["intents"]))
                )
                for p in data.get("proposals", [])
            )
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def decision_proposal(self, data, policy):
        for batch in data.get("proposals", []):
            now = self.clock()
            intents = batch["intents"]
            if not isinstance(intents, list) or not 1 <= len(intents) <= 12:
                raise ValueError("invalid_trade_intents")
            identities = [
                hashlib.sha256(f"{batch['id']}:{n}".encode()).hexdigest()
                for n in range(len(intents))
            ]
            basket_orders = {"vq" + identity[:32] for identity in identities}
            active_orders = self.db.execute("SELECT id,state FROM execution_orders").fetchall()
            if any(
                row["state"] in ACTIVE
                and (
                    row["id"] not in basket_orders
                    or row["state"] not in {"ACKNOWLEDGED", "PARTIAL"}
                )
                for row in active_orders
            ):
                continue
            if (
                batch["settings_revision"] != policy["revision"]
                or not 0 <= now - batch["created_at"] <= 90
            ):
                for identity in identities:
                    self.remember(identity, "report_expired")
                continue
            context = batch["context"]
            current = self.snapshot
            if current is None:
                return None
            remaining = [
                (identity, intent)
                for identity, intent in zip(identities, intents, strict=True)
                if not self.db.execute(
                    "SELECT 1 FROM execution_decisions WHERE id=?", (identity,)
                ).fetchone()
            ]
            if not remaining:
                continue
            # The entire remaining basket must fit. No partial acceptance through resizing.
            spend = D(0)
            valid = True
            seen = set()
            for _, intent in remaining:
                try:
                    number, quantity = (
                        amount(intent["limit_price"]),
                        shares(str(intent["quantity"])),
                    )
                    if type(intent["quantity"]) is not int or quantity <= 0:
                        raise ValueError("invalid_quantity")
                    if (
                        set(intent)
                        != {
                            "symbol",
                            "side",
                            "quantity",
                            "limit_price",
                            "rationale",
                            "evidence_ids",
                        }
                        or not domestic_symbol(intent["symbol"])
                        or intent["symbol"] in seen
                        or number <= 0
                        or number != number.to_integral_value()
                        or not isinstance(intent["evidence_ids"], list)
                        or "market:" + intent["symbol"] not in intent["evidence_ids"]
                    ):
                        raise ValueError("invalid_trade_intent")
                    seen.add(intent["symbol"])
                    cost = number * quantity * (1 + self.metadata["rate"] + D("0.01")) + 1
                    if intent["side"] == "BUY":
                        valid &= cost <= min(D(policy["limits"]["max_order_krw"]), D("99999999"))
                        spend += cost
                    elif intent["side"] == "SELL":
                        valid &= quantity <= current.managed_quantities.get(intent["symbol"], 0)
                    else:
                        valid = False
                except (ValueError, KeyError, TypeError):
                    valid = False
            exposure = sum(
                q * D(current.quotes[s][0]) for s, (q, _) in self.core._positions().items()
            )
            valid &= spend == 0 or spend <= min(
                D(current.cash_buying_power),
                D(policy["limits"]["capital_krw"]) - self.core.capital_used(),
                D(policy["limits"]["capital_krw"]) - exposure,
            )
            if not valid:
                for identity, _ in remaining:
                    self.remember(identity, "capital_limit")
                continue
            identity, intent = remaining[0]
            quotes = {q["symbol"]: q for q in context["quotes"]}
            if intent["symbol"] not in quotes:
                self.remember(identity, "stale_price")
                continue
            return {
                "event_id": identity,
                "symbol": intent["symbol"],
                "quote": quotes[intent["symbol"]],
                "proposal": {"side": intent["side"]},
                "trade_intent": intent,
                "settings_revision": batch["settings_revision"],
                "created_at": batch["created_at"],
            }
        return None

    def make_intent(self, report, policy, snapshot):
        now = self.clock()
        if "trade_intent" in report and not 0 <= now - report["created_at"] <= 90:
            raise ValueError("report_expired")
        symbol = report["symbol"]
        q = snapshot.quotes[symbol]
        if not 0 <= now - q[1] <= 5:
            raise ValueError("stale_price")
        if abs(D(q[0]) / amount(report["quote"]["price"]) - 1) > D("0.005"):
            raise ValueError("price_changed")
        raw = self.observations.db.execute(
            "SELECT payload FROM latest WHERE topic=?", (f"orderbook:kr:{symbol}",)
        ).fetchone()
        if raw is None:
            raise ValueError("stale_price")
        book = json.loads(raw[0])
        dt = datetime.fromisoformat(book["timestamp"])
        if dt.tzinfo is None or not 0 <= now - dt.timestamp() <= 5 or book["currency"] != "KRW":
            raise ValueError("stale_price")
        side = report["proposal"]["side"]
        prices = [
            amount(r["price"])
            for r in book["asks" if side == "BUY" else "bids"]
            if amount(r["volume"]) > 0
        ]
        if not prices:
            raise ValueError("stale_price")
        price = min(prices) if side == "BUY" else max(prices)
        limits = policy["limits"]
        rate = self.metadata["rate"] + (D("0.01") if side == "SELL" else D(0))
        if "trade_intent" in report:
            proposed = report["trade_intent"]
            quantity, requested = proposed["quantity"], amount(proposed["limit_price"])
            if (
                type(quantity) is not int
                or quantity <= 0
                or requested != requested.to_integral_value()
            ):
                raise ValueError("invalid_quantity")
            if abs(requested / price - 1) > D("0.005"):
                raise ValueError("price_changed")
            fee = (quantity * requested * rate).to_integral_value(rounding=ROUND_CEILING) + 1
            return Intent(
                "vq" + report["event_id"][:32],
                symbol,
                side,
                quantity,
                str(requested),
                str(fee),
                policy["revision"],
                now,
                now + 15,
            )
        # The sell cost reserve is explicitly a conservative buffer, not a claimed tax rate.
        # Actual commissions and tax enter P&L exclusively from broker execution details.
        cap = min(D(limits["max_order_krw"]), D("99999999"))
        positions_value = sum(
            qty * D(snapshot.quotes[s][0]) for s, (qty, _) in self.core._positions().items()
        )
        if side == "BUY":
            cap = min(
                cap,
                D(limits["capital_krw"]) - positions_value,
                D(limits["capital_krw"]) - self.core.capital_used(),
                D(snapshot.cash_buying_power),
            )
        quantity = max(0, int((cap - D(1)) / (price * (1 + rate))))
        if side == "SELL":
            quantity = min(
                snapshot.managed_quantities.get(symbol, 0),
                snapshot.sellable_quantities[symbol],
            )
        if quantity <= 0:
            raise ValueError("no_quantity")
        fee = (quantity * price * rate).to_integral_value(rounding=ROUND_CEILING) + 1
        return Intent(
            "vq" + report["event_id"][:32],
            symbol,
            side,
            quantity,
            str(price),
            str(fee),
            policy["revision"],
            now,
            now + 15,
        )

    async def order_snapshot(self, report):
        symbol, side = report["symbol"], report["proposal"]["side"]
        if report["quote"]["symbol"] != symbol or report["quote"]["currency"] != "KRW":
            raise ValueError("instrument_unavailable")
        universe = self.observations.universe
        if (
            "trade_intent" in report
            and universe is not None
            and symbol not in universe.watch
            and callable(self.observations.promote)
        ):
            self.execution_pins[symbol] = self.clock() + 120
            await self.observations.promote(symbol)
        if universe is not None and (symbol in universe.rejected or symbol not in universe.watch):
            raise ValueError("stale_price")
        if side == "BUY":
            if universe is None or not universe.fresh() or symbol not in universe.stocks:
                raise ValueError("universe_unavailable")
            if symbol not in self.core._positions() and len(self.pinned_symbols()) >= MAX_WATCH:
                raise ValueError("subscription_capacity")
        checked_at = self.clock()
        info, warnings = await asyncio.gather(
            self.broker.stock_info((symbol,)), self.broker.stock_warnings(symbol)
        )
        if "trade_intent" in report:
            price_limits = await self.broker.price_limits(symbol)
            price = amount(report["trade_intent"]["limit_price"])
            lower, upper = price_limits.get("lowerLimitPrice"), price_limits.get("upperLimitPrice")
            if lower is None or upper is None or not amount(lower) <= price <= amount(upper):
                raise ValueError("instrument_restricted")
        record = validate_trade_stock(info, warnings, symbol, side)
        self.observations.instruments[symbol] = record
        sellable = (
            await self.broker.sellable_quantity(self.account, symbol) if side == "SELL" else None
        )
        # A new symbol has no external anchor yet. Install it from the current
        # holdings before preparing its first order, even when it has zero shares.
        self.db.execute(
            "INSERT OR IGNORE INTO execution_anchor SELECT ?,0 WHERE NOT EXISTS "
            "(SELECT 1 FROM execution_anchor WHERE symbol=?)",
            (symbol, symbol),
        )
        snapshot = await self.reconcile()
        if self.clock() - checked_at > 5:
            raise ValueError("instrument_unavailable")
        return replace(
            snapshot,
            sellable_quantities={symbol: shares(sellable["sellableQuantity"])} if sellable else {},
        )

    async def commands(self, data):
        for command in data.get("commands", [])[:30]:
            cid = command["id"]
            if self.db.execute("SELECT 1 FROM execution_commands WHERE id=?", (cid,)).fetchone():
                continue
            oid, action = command["order_id"], command["action"]
            row = self.db.execute("SELECT * FROM execution_orders WHERE id=?", (oid,)).fetchone()
            result = "not_applicable"
            if row and action == "cancel":
                self.core.armed = (
                    True  # Only owned-order cancellations; new orders still need policy.
                )
                await self.core.cancel(oid, self.transport, self.clock())
                result = "cancel_requested"
            elif row and row["state"] == "UNKNOWN" and not data["policy"]["live_requested"]:
                if (
                    action == "confirm_absent"
                    and self.clock() - json.loads(row["intent"])["created_at"] >= 86400
                ):
                    self.db.execute("UPDATE execution_orders SET state='VOID' WHERE id=?", (oid,))
                    result = "owner_confirmed_absent"
                elif action == "attach":
                    try:
                        detail = await self.broker.order(self.account, command["broker_id"])
                    except (TossError, ValueError, KeyError, TypeError):
                        self.db.execute(
                            "INSERT INTO execution_commands VALUES (?,?,?)",
                            (cid, "lookup_failed", self.clock()),
                        )
                        self.store.record(oid, "owner_execution_lookup_failed", {"command": cid})
                        continue
                    intent = Intent(**json.loads(row["intent"]))
                    ordered = datetime.fromisoformat(detail["orderedAt"])
                    if (
                        detail["orderId"] == command["broker_id"]
                        and detail.get("timeInForce") == "DAY"
                        and ordered.tzinfo is not None
                        and abs(ordered.timestamp() - intent.created_at) <= 120
                        and (
                            detail["symbol"],
                            detail["side"],
                            detail["quantity"],
                            detail["price"],
                            detail["orderType"],
                            detail["currency"],
                        )
                        == (
                            intent.symbol,
                            intent.side,
                            str(intent.quantity),
                            intent.limit_price,
                            "LIMIT",
                            "KRW",
                        )
                        and not self.db.execute(
                            "SELECT 1 FROM execution_orders WHERE broker_id=?", (detail["orderId"],)
                        ).fetchone()
                    ):
                        self.db.execute(
                            "UPDATE execution_orders SET broker_id=? WHERE id=?",
                            (detail["orderId"], oid),
                        )
                        result = (
                            "attached"
                            if self.core.observe(oid, detail, self.clock())
                            else "review_required"
                        )
            self.db.execute(
                "INSERT INTO execution_commands VALUES (?,?,?)", (cid, result, self.clock())
            )
            self.store.record(
                oid, "owner_execution_command", {"command": cid, "action": action, "result": result}
            )

    async def cancel_pending(self):
        self.core.armed = True
        for r in self.db.execute(
            "SELECT id FROM execution_orders WHERE state IN ('ACKNOWLEDGED','PARTIAL')"
        ).fetchall():
            await self.core.cancel(r["id"], self.transport, self.clock())

    async def monitor(self):
        """Fast loss/heartbeat check independent of slow REST refresh or AI invocation."""
        try:
            policy = self.current_policy()
            snap = self.snapshot
            if snap is None or not policy.get("live_enabled"):
                await self.cancel_pending()
                return
            fresh = replace(
                snap,
                quotes=self.valuation_quotes(),
            )
            result = self.core.monitor_loss(fresh, policy, self.clock())
            if result["state"] != "within_limit":
                await self.cancel_pending()
        except (OSError, ValueError, KeyError, TypeError):
            await self.cancel_pending()

    async def tick(self):
        policy, state, ready, loss = {}, "account_unavailable", False, None
        try:
            data = self.control()
            policy = data["policy"]
            await self.commands(data)
            snapshot = await self.reconcile()
            now = self.clock()
            if not policy["onboarding_completed"]:
                state = "onboarding_required"
            elif not policy["model_connection"]["ready"]:
                state = "model_setup_required"
            else:
                loss = self.core.monitor_loss(snapshot, policy, now)
                ready = loss["state"] == "within_limit"
                state = (
                    "ready"
                    if ready
                    else "daily_loss_limit"
                    if loss["state"] == "breached"
                    else loss["state"]
                )
                if not snapshot.regular_session_open and loss["state"] == "reconciliation_required":
                    state = "market_closed"
                if data["stopped"]:
                    state, ready = "stopped", False
                if not policy["live_requested"] or not ready:
                    await self.cancel_pending()
                if ready and policy["live_requested"]:
                    state = "active" if snapshot.regular_session_open else "market_closed"
                    rows = self.db.execute("SELECT * FROM execution_orders").fetchall()
                    active = [r for r in rows if r["state"] in ACTIVE]
                    # Cancel resting orders after 90 seconds; acknowledgments are not fills.
                    for r in active:
                        if now - json.loads(r["intent"])["created_at"] >= 90:
                            self.core.armed = True
                            await self.core.cancel(r["id"], self.transport, now)
                    if snapshot.regular_session_open and all(
                        r["state"] in {"ACKNOWLEDGED", "PARTIAL"} for r in active
                    ):
                        report = self.proposal(policy)
                        if active and report and "trade_intent" not in report:
                            report = None
                        if report:
                            try:
                                snapshot = await self.order_snapshot(report)
                                intent = self.make_intent(report, policy, snapshot)
                                self.core.armed = True
                                current = self.current_policy()
                                reason = self.core.prepare(intent, snapshot, current, self.clock())
                                if reason == "accepted":
                                    await self.core.submit(
                                        intent.id, self.transport, snapshot, current, self.clock()
                                    )
                                    reason = (
                                        "order_submitted"
                                        if self.db.execute(
                                            "SELECT state FROM execution_orders WHERE id=?",
                                            (intent.id,),
                                        ).fetchone()[0]
                                        == "ACKNOWLEDGED"
                                        else "order_review"
                                    )
                                if reason == "order_review":
                                    ready, state = False, "order_review"
                                self.remember(report["event_id"], reason)
                            except (ValueError, KeyError, TypeError) as error:
                                self.remember(
                                    report["event_id"],
                                    str(error)
                                    if str(error) in MESSAGES
                                    else "reconciliation_required",
                                )
                    elif active:
                        self.last_reason = "order_pending"
            if not policy.get("live_requested") and ready:
                state = "disabled"
        except (OSError, ValueError, KeyError, TypeError) as error:
            ready = False
            self.snapshot = None
            state = str(error) if str(error) in MESSAGES else "account_unavailable"
            await self.cancel_pending()
        except Exception:
            ready = False
            self.snapshot = None
            state = "account_unavailable"
            await self.cancel_pending()
        finally:
            self.core.armed = False
            self.publish(state, ready, policy, loss)

    def publish(self, state, ready, policy, loss):
        opening_date = None
        now = self.clock()
        view = self.observations.account_view or {}
        if (
            self.snapshot is not None
            and 0 <= now - self.snapshot.as_of <= 10
            and self.metadata is not None
            and (view.get("regular_start") is None or now < view["regular_start"])
        ):
            opening_date = self.metadata["calendar"]["previousBusinessDay"]["date"]
        orders = []
        for r in self.db.execute(
            "SELECT * FROM execution_orders ORDER BY updated_at DESC LIMIT 30"
        ):
            i = json.loads(r["intent"])
            orders.append(
                {
                    k: r[k]
                    for k in (
                        "id",
                        "state",
                        "filled_quantity",
                        "filled_amount",
                        "fees",
                        "tax",
                        "updated_at",
                    )
                }
                | {k: i[k] for k in ("symbol", "side", "quantity", "limit_price", "created_at")}
                | {
                    "broker_id": r["broker_id"],
                    "name": self.observations.instruments.get(i["symbol"], {}).get(
                        "name", i["symbol"]
                    ),
                }
            )
        if state not in MESSAGES:
            state = "reconciliation_required"
        atomic_json(
            self.status_path,
            {
                "updated_at": self.clock(),
                "state": state,
                "ready": ready,
                "settings_revision": policy.get("revision"),
                "live_enabled": bool(ready and policy.get("live_requested")),
                "orders": orders,
                "loss": loss,
                "monthly_performance": monthly_performance(
                    self.store, loss, now, self.bars, opening_date=opening_date
                ),
                "last_action": self.last_reason,
                "last_action_message": MESSAGES.get(
                    self.last_reason, "주문 전 확인을 진행하고 있습니다."
                ),
                "managed_positions": [
                    {
                        "symbol": s,
                        "name": self.observations.instruments.get(s, {}).get("name", s),
                        "quantity": q,
                        "cost_basis_krw": str(b),
                    }
                    for s, (q, b) in self.core._positions().items()
                ],
                "commands": [
                    dict(r)
                    for r in self.db.execute(
                        "SELECT * FROM execution_commands ORDER BY at DESC LIMIT 30"
                    )
                ],
                "scope": "KR domestic cash equities / LIMIT DAY",
            },
        )

    def close(self):
        self.core.armed = False
        self.publish("account_unavailable", False, {}, None)
        self.store.close()
