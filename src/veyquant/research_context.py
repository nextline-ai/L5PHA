"""Read-only collector capability: sanitized broker snapshots over a private Unix socket."""

import asyncio
import hashlib
import json
import os
import pwd
import socket
import struct
import time
import traceback
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from veyquant.account_readiness import build_account_view
from veyquant.adapters.toss import TossHTTPError
from veyquant.execution import ACTIVE, day_key
from veyquant.shadow_contract import domestic_symbol, market_quote
from veyquant.surveillance import KST, session
from veyquant.trading_state import completed_bars
from veyquant.valuation_marks import bid_mark, portfolio_mark

SOCKET = "/run/veyquant-context/context.sock"
MAX_BYTES = 4 * 1024 * 1024
# Advisory portfolio marking; execution still requires five-second market data.
RESEARCH_PRICE_MAX_AGE = 30

CONTEXT_ERRORS = {
    "onboarding_required",
    "portfolio_context_exceeds_limit",
    "account_changed_during_snapshot",
    "invalid_context_request",
    "unapproved_context_operation",
    "invalid_context_symbols",
    "context_busy",
    "too_many_candidates",
    "universe_unavailable",
    "invalid_manual_instruction",
    "mandatory_universe_exceeds_limit",
    "unknown_requested_symbol",
    "invalid_candles",
    "invalid_candle_scope",
    "invalid_ohlc",
    "invalid_volume",
    "duplicate_candle",
    "invalid_price",
    "currency_mismatch",
    "naive_market_time",
    "future_market_time",
    "invalid_account_amount",
    "invalid_buying_power",
    "invalid_holding",
    "invalid_holding_symbol",
    "invalid_holding_currency",
    "incomplete_conditional_orders",
    "invalid_conditional_order",
    "stale_market_calendar",
    "invalid_market_calendar",
    "toss_read_transport_failure",
    "toss_token_transport_failure",
    "invalid_read_response",
    "invalid_token_response",
}


def context_error(error):
    while isinstance(error, ExceptionGroup):
        error = error.exceptions[0]
    if isinstance(error, ContextReadError):
        return str(error)
    if (
        isinstance(error, TossHTTPError)
        and type(error.status) is int
        and 400 <= error.status <= 599
    ):
        return f"toss_http_{error.status}"
    if str(error) in CONTEXT_ERRORS:
        return str(error)
    return "context_timeout" if isinstance(error, TimeoutError) else "context_unavailable"


class ContextReadError(ValueError):
    def __init__(self, component, symbol, error):
        self.component, self.symbol = component, symbol
        super().__init__(f"context_{component}_{context_error(error)}")


async def context_read(component, symbol, request):
    # The broker GET adapter owns retries for all read paths. Do not multiply
    # three transport attempts by another three here; keep one total deadline.
    try:
        async with asyncio.timeout(90):
            return await request()
    except Exception as error:
        raise ContextReadError(component, symbol, error) from error


async def exchange(path, message, timeout=585, maximum=MAX_BYTES):
    async with asyncio.timeout(timeout):
        reader, writer = await asyncio.open_unix_connection(path, limit=maximum + 1)
        try:
            writer.write(json.dumps(message, allow_nan=False).encode() + b"\n")
            await writer.drain()
            raw = await reader.readline()
            if not raw or len(raw) > maximum:
                raise ValueError("context_unavailable")
            data = json.loads(raw)
            if "error" in data:
                error = ValueError(data["error"])
                error.context_failure = data.get("diagnostic")
                raise error
            return data
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()


async def serve(path, allowed_uid, handler, maximum=MAX_BYTES):
    async def handle(reader, writer):
        result = {"error": "context_unavailable"}
        try:
            _, uid, _ = struct.unpack(
                "3i",
                writer.get_extra_info("socket").getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, 12
                ),
            )
            if uid != allowed_uid:
                return
            raw = await asyncio.wait_for(reader.readline(), 5)
            if not raw or len(raw) > 16384:
                return
            async with asyncio.timeout(570):
                result = await handler(json.loads(raw))
        except Exception as error:
            result = {"error": context_error(error)}
            while isinstance(error, ExceptionGroup):
                error = error.exceptions[0]
            # Return only our error code, public symbol and code locations. Never
            # serialize exception messages, request bodies, account data or credentials.
            result["diagnostic"] = {
                "type": type(error).__name__,
                "symbol": getattr(error, "symbol", None),
                "frames": [(f.name, f.lineno) for f in traceback.extract_tb(error.__traceback__)],
            }
        finally:
            raw = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
            if len(raw) > maximum:
                raw = b'{"error":"context_budget_exceeded"}'
            writer.write(raw + b"\n")
            with suppress(OSError):
                await writer.drain()
            writer.close()

    with suppress(FileNotFoundError):
        os.unlink(path)
    server = await asyncio.start_unix_server(handle, path=path, limit=16385)
    os.chmod(path, 0o660)
    async with server:
        await server.serve_forever()


@asynccontextmanager
async def research_priority(broker):
    previous = getattr(broker, "research_reads_active", 0)
    broker.research_reads_active = previous + 1
    try:
        yield
    finally:
        broker.research_reads_active = previous


class ResearchContext:
    def __init__(self, worker, clock=time.time):
        self.worker, self.clock = worker, clock
        self.busy = asyncio.Lock()
        worker.observations.db.execute(
            "CREATE TABLE IF NOT EXISTS research_visits(symbol TEXT PRIMARY KEY, at REAL)"
        )

    async def account(self, symbols=()):
        w, broker = self.worker, self.worker.broker
        await w.metadata_refresh()
        managed = w.core._positions()
        constraints = {}
        constrained = tuple(
            dict.fromkeys(list(symbols) + [s for s, (q, _) in managed.items() if q > 0])
        )
        if len(constrained) > 200:
            raise ValueError("portfolio_context_exceeds_limit")
        for symbol in constrained:
            # Current broker restrictions are separate from the frozen market research tools.
            limits, warning, sellable = await asyncio.gather(
                broker.price_limits(symbol),
                broker.stock_warnings(symbol),
                broker.sellable_quantity(w.account, symbol)
                if symbol in managed
                else asyncio.sleep(0, result={"sellableQuantity": "0"}),
            )
            constraints[symbol] = {
                "price_limits": limits,
                "warnings": warning,
                "sellable_quantity": sellable["sellableQuantity"],
            }
        now, generation = self.clock(), w.observations.portfolio_generation
        holdings, orders, cash, conditionals = await asyncio.gather(
            broker.holdings(w.account),
            broker.open_orders(w.account),
            broker.buying_power(w.account),
            broker.conditional_orders(w.account),
        )
        view = build_account_view(holdings, orders, cash, conditionals, w.metadata["calendar"], now)
        policy = w.control()["policy"]
        if not policy["configured"]:
            raise ValueError("onboarding_required")
        managed = w.core._positions()
        sanitized = []
        for h in holdings["items"]:
            if h["currency"] == "KRW" and domestic_symbol(h["symbol"]):
                sanitized.append(
                    {
                        "symbol": h["symbol"],
                        "quantity": h["quantity"],
                        "managed_quantity": managed.get(h["symbol"], (0, 0))[0],
                    }
                )
        quote_symbols = tuple(dict.fromkeys(list(symbols) + [h["symbol"] for h in sanitized]))
        if len(quote_symbols) > 200:
            raise ValueError("portfolio_context_exceeds_limit")
        quotes = await broker.prices(quote_symbols) if quote_symbols else []
        quotes = [
            market_quote(q["symbol"], q | {"price": q["lastPrice"]}, self.clock())
            for q in quotes
            if q.get("timestamp")
        ]
        if generation != w.observations.portfolio_generation or self.clock() - now > 10:
            raise ValueError("account_changed_during_snapshot")
        # REST timestamps describe the last trade, not receipt of an HTTP response.
        # Prefer a newer validated stream trade without inventing a fresh timestamp.
        quote_map = {q["symbol"]: q for q in quotes}
        for symbol in quote_symbols:
            streamed = w.observations.quotes.get(symbol)
            if (
                streamed
                and 0 <= self.clock() - streamed["as_of"] <= RESEARCH_PRICE_MAX_AGE
                and streamed["as_of"] > quote_map.get(symbol, {}).get("as_of", 0)
            ):
                quote_map[symbol] = dict(streamed)
        quotes = list(quote_map.values())
        marks = {}
        gate = asyncio.Semaphore(3)

        async def mark_position(symbol):
            mark = portfolio_mark(
                w.observations, symbol, quote_map.get(symbol), self.clock(), RESEARCH_PRICE_MAX_AGE
            )
            if mark is None and callable(getattr(broker, "orderbook", None)):
                async with gate:
                    try:
                        async with asyncio.timeout(2):
                            book = await broker.orderbook(symbol)
                        mark = bid_mark(symbol, book, self.clock(), RESEARCH_PRICE_MAX_AGE)
                    except Exception:
                        mark = None  # Failure stays unavailable, never fabricated freshness.
            if mark:
                marks[symbol] = mark

        try:
            async with asyncio.timeout(3):
                await asyncio.gather(*(mark_position(s) for s, (q, _) in managed.items() if q > 0))
        except TimeoutError:
            pass  # Keep completed marks; unresolved positions remain explicitly unavailable.
        if generation != w.observations.portfolio_generation or self.clock() - now > 5:
            raise ValueError("account_changed_during_snapshot")
        risk = {"state": "unavailable", "as_of": now, "reason": "daily_baseline_missing"}
        try:
            if w.snapshot is None:
                raise ValueError("reconciliation_required")
            if any(q > 0 and s not in marks for s, (q, _) in managed.items()):
                raise ValueError("stale_price")
            snapshot = replace(
                w.snapshot,
                as_of=now,
                quotes={s: (q["price"], q["as_of"]) for s, q in marks.items()},
                managed_quantities={s: q for s, (q, _) in managed.items()},
            )
            _, pnl = w.core._valuation(snapshot, self.clock(), max_price_age=RESEARCH_PRICE_MAX_AGE)
            baseline = w.db.execute(
                "SELECT baseline,breached FROM execution_days WHERE day=?", (day_key(now),)
            ).fetchone()
            if baseline:
                daily = pnl - Decimal(baseline[0])
                risk = {
                    "state": "breached"
                    if baseline[1] or daily <= -Decimal(policy["limits"]["max_daily_loss_krw"])
                    else "within_limit",
                    "as_of": now,
                    "daily_pnl_krw": str(daily),
                }
        except (ValueError, TypeError, KeyError) as error:
            reasons = {
                "stale_account",
                "stale_price",
                "reconciliation_required",
                "invalid_account_quantity",
                "invalid_price",
            }
            risk["reason"] = str(error) if str(error) in reasons else "valuation_data_missing"
            risk["stale_symbols"] = ",".join(
                s
                for s, (q, _) in managed.items()
                if q > 0
                and not 0
                <= self.clock() - marks.get(s, {}).get("as_of", 0)
                <= RESEARCH_PRICE_MAX_AGE
            )
        marked_at = self.clock()
        risk["price_basis"] = (
            "last trade, or fresh best bid when trade is stale; indicative, not guaranteed proceeds"
        )
        risk["max_price_age_seconds"] = RESEARCH_PRICE_MAX_AGE
        risk["execution_check"] = "Orders independently require account and prices within 5 seconds"
        risk["price_observations"] = [
            {
                "symbol": s,
                "as_of": marks.get(s, {}).get("as_of"),
                "price": marks.get(s, {}).get("price"),
                "basis": marks.get(s, {}).get("basis"),
                "age_seconds": round(marked_at - marks[s]["as_of"], 2) if s in marks else None,
            }
            for s, (quantity, _) in managed.items()
            if quantity > 0
        ]
        health = self.health()
        feedback = [
            dict(row)
            for row in w.db.execute(
                "SELECT d.id AS event_id,d.reason,d.at,o.state,o.filled_quantity "
                "FROM execution_decisions d LEFT JOIN execution_orders o "
                "ON o.id='vq'||substr(d.id,1,32) ORDER BY d.at DESC LIMIT 36"
            )
        ]
        return {
            "as_of": now,
            "currency": "KRW",
            "cash": view["cash_buying_power_krw"],
            "holdings": sanitized,
            "open_orders": [
                {k: o[k] for k in ("symbol", "side", "quantity", "status")}
                for o in orders["orders"]
            ],
            "conditional_orders": len(conditionals["conditionalOrders"]),
            "unresolved_submission": w.pending_decision()
            or any(r[0] in ACTIVE for r in w.db.execute("SELECT state FROM execution_orders")),
            "order_generation": generation,
            "settings_revision": policy["revision"],
            "risk_limits": policy["limits"],
            "risk_status": risk,
            "data_health": health,
            "execution_feedback": feedback,
            "capital_used": str(w.core.capital_used()),
            "live_requested": policy["live_requested"],
            "quotes": quotes,
            "order_constraints": {
                "type": "LIMIT",
                "time_in_force": "DAY",
                "currency": "KRW",
                "sell_scope": "AI-managed shares only",
                "commission_rate": str(w.metadata["rate"]),
                "sell_cost_reserve_rate": "0.01",
                "session": {
                    "scope": "regular continuous trading; excludes closing auction",
                    "starts_at": view["regular_start"],
                    "ends_at": view["regular_end"],
                    "is_open": view["regular_start"] is not None
                    and view["regular_start"] <= self.clock() < view["regular_end"],
                },
                "stocks": constraints,
            },
        }

    async def handle(self, data):
        if set(data) != {"operation", "request"} or not isinstance(data["request"], dict):
            raise ValueError("invalid_context_request")
        operation, request = data["operation"], data["request"]
        if operation not in {"initial", "refresh", "status"}:
            raise ValueError("unapproved_context_operation")
        if operation == "status":
            w = self.worker
            calendar = w.metadata["calendar"] if w.metadata else await w.broker.market_calendar()
            session(calendar, self.clock())
            return {
                "calendar": calendar,
                "read_limits": getattr(w.broker, "read_limits", {}),
                "exposure_as_of": w.snapshot.as_of if w.snapshot else None,
                "exposed_symbols": sorted(set(w.pinned_symbols())),
                "eligible_symbols": sorted(
                    s
                    for s, row in w.observations.universe.stocks.items()
                    if row.get("eligibility") == "eligible"
                )
                if w.observations.universe
                else None,
                "blocked": bool(
                    w.pending_decision()
                    or w.snapshot is None
                    or not w.observations.connected
                    or not 0 <= self.clock() - w.snapshot.as_of <= 30
                    or w.snapshot.known_open_order_ids
                    or any(
                        r[0] in ACTIVE for r in w.db.execute("SELECT state FROM execution_orders")
                    )
                ),
            }
        symbols = request.get("symbols", [])
        if (
            not isinstance(symbols, list)
            or len(symbols) > 200
            or any(not domestic_symbol(s) for s in symbols)
        ):
            raise ValueError("invalid_context_symbols")
        if self.busy.locked():
            raise ValueError("context_busy")
        async with self.busy, research_priority(self.worker.broker):
            if operation == "refresh":
                if len(symbols) > 12:
                    raise ValueError("too_many_candidates")
                return await self.account(symbols)
            collection_started_at = self.clock()
            account = await self.account()
            if (
                account["open_orders"]
                or account["unresolved_submission"]
                or account["conditional_orders"]
            ):
                return account
            w, universe = self.worker, self.worker.observations.universe
            if universe is None:
                raise ValueError("universe_unavailable")
            if not universe.fresh():
                # The overnight collector deliberately skips market scans. A
                # requested read-only analysis must refresh the catalogue from
                # the broker instead of requiring a restart or the next session.
                await context_read("catalogue", None, universe.catalogue)
            if not universe.fresh():
                raise ValueError("universe_unavailable")
            # Explicit structured selections and recognized public names/codes are priorities,
            # not instructions to trade. Existing holdings and mandatory event symbols come first.
            instruction = request.get("instruction", "")
            if not isinstance(instruction, str) or len(instruction) > 3000:
                raise ValueError("invalid_manual_instruction")
            mentioned = [
                s
                for s, item in universe.stocks.items()
                if s in instruction or item["name"] in instruction
            ]
            prioritized = (
                [h["symbol"] for h in account["holdings"]]
                + symbols
                + mentioned
                + ([] if request.get("review_mode") == "event" else list(universe.watch))
            )
            selected = list(dict.fromkeys(prioritized))
            if len(selected) > 200:
                raise ValueError("mandatory_universe_exceeds_limit")
            visits = dict(w.observations.db.execute("SELECT symbol,at FROM research_visits"))
            ranked = sorted(
                universe.stocks,
                key=lambda s: (
                    universe.stocks[s]["eligibility"] != "eligible",
                    s not in universe.prices,
                    visits.get(s, 0),
                    s,
                ),
            )
            if request.get("review_mode") == "event":
                # An unscoped market-wide event still reviews current watch coverage.
                if not symbols and not mentioned:
                    selected = list(dict.fromkeys(selected + (list(universe.watch) or ranked[:20])))
            else:
                selected = list(dict.fromkeys(selected + ranked))[:200]
            if len(selected) > 200:
                raise ValueError("mandatory_universe_exceeds_limit")
            if any(s not in universe.stocks for s in selected):
                raise ValueError("unknown_requested_symbol")
            gate = asyncio.Semaphore(3)

            async def detail(symbol):
                async with gate:
                    unavailable = {}

                    async def read(name, method):
                        try:
                            return await context_read(name, symbol, lambda: method(symbol))
                        except ContextReadError as error:
                            cause = error.__cause__
                            if name not in {"orderbook", "trades"} or not (
                                isinstance(cause, TossHTTPError) and cause.status == 429
                            ):
                                raise
                            # Optional microstructure is unknown, not an empty
                            # book or zero trades. Account/constraints, daily bars
                            # and warnings still fail the snapshot on error.
                            unavailable[name] = str(error)
                            return None

                    async with asyncio.TaskGroup() as group:
                        tasks = [
                            group.create_task(read(name, method))
                            for name, method in (
                                ("daily_bars", w.broker.daily_candles),
                                ("orderbook", w.broker.orderbook),
                                ("trades", w.broker.trades),
                                ("warnings", w.broker.stock_warnings),
                            )
                        ]
                    candles, book, trades, warnings = [task.result() for task in tasks]
                    at = self.clock()
                    try:
                        bars = completed_bars(candles, at)
                    except Exception as error:
                        raise ContextReadError("daily_bars", symbol, error) from error
                    return symbol, {
                        "symbol": symbol,
                        "as_of": at,
                        "quote": universe.prices.get(symbol),
                        "daily_bars": bars,
                        "daily_price_basis": "adjusted",
                        "orderbook": book,
                        "trades": trades[:30] if trades is not None else None,
                        **({"unavailable": unavailable} if unavailable else {}),
                        "warnings": warnings,
                    }

            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(detail(s)) for s in selected]
            details = dict(task.result() for task in tasks)
            w.observations.db.executemany(
                "INSERT OR REPLACE INTO research_visits VALUES(?,?)",
                [(s, self.clock()) for s in selected],
            )
            comparison = [
                [
                    s,
                    item["name"],
                    item["market"],
                    item["eligibility"],
                    universe.prices.get(s, {}).get("price"),
                    universe.prices.get(s, {}).get("as_of"),
                ]
                for s, item in sorted(universe.stocks.items())
            ]
            mandatory = []
            for s, detail in details.items():
                for warning in detail["warnings"]:
                    identity = hashlib.sha256(
                        json.dumps([s, warning], sort_keys=True).encode()
                    ).hexdigest()
                    mandatory.append(
                        {
                            "id": identity,
                            "kind": "new_warning",
                            "symbol": s,
                            "source": "Toss stock warnings",
                            "as_of": detail["as_of"],
                            "warning": warning,
                        }
                    )
            completed_health = self.health()
            return account | {
                "review_mode": request.get("review_mode", "broad"),
                "data_health": completed_health
                | {
                    "collection_started_at": collection_started_at,
                    "collection_completed_at": completed_health["as_of"],
                },
                "details": details,
                "review_universe": [
                    universe.stocks[s]
                    | {
                        "market_evidence_id": "market:" + s,
                        "quote": details[s]["quote"],
                        "daily_bars": details[s]["daily_bars"],
                    }
                    for s in selected
                ],
                "comparison_table": {
                    "columns": ["symbol", "name", "market", "eligibility", "price", "as_of"],
                    "rows": comparison,
                },
                "mandatory_evidence": mandatory,
            }

    def health(self):
        w = self.worker
        health_at = self.clock()
        regular = session(w.metadata["calendar"], health_at)
        is_open = regular is not None and regular["open"] <= health_at < regular["close"]
        last = w.observations.metrics.last_realtime_at
        age = max(0, health_at - max(last, regular["open"] if is_open else 0))
        return {
            "as_of": health_at,
            "as_of_kst": datetime.fromtimestamp(health_at, KST).isoformat(timespec="seconds"),
            "connected": w.observations.connected,
            "realtime_state": (
                "outside_session"
                if not is_open
                else "healthy"
                if w.observations.connected and last >= regular["open"] and age < 30
                else "gap"
            ),
            "realtime_age_seconds": round(age, 1) if is_open else None,
        }

    async def serve(self):
        await serve(SOCKET, pwd.getpwnam("veyanalysis").pw_uid, self.handle)
