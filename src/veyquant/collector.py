"""Single OAuth session for market data, reconciliation and guarded execution."""

import asyncio
import hashlib
import json
import math
import os
import signal
import socket
import sqlite3
import time
from contextlib import aclosing, suppress
from pathlib import Path

from veyquant.account_readiness import build_account_view
from veyquant.adapters.toss import (
    Credentials,
    Subscription,
    TokenManager,
    TossError,
    TossHTTPError,
    TossReadOnly,
    TossStream,
    http_client,
)
from veyquant.order_journal import OrderJournal, order_record
from veyquant.shadow_contract import atomic_json, market_quote
from veyquant.toss_probe import select_account, validate_market_frame, validate_snapshot


def regular_market_open(calendar, now):
    from veyquant.surveillance import session

    try:
        current = session(calendar, now)
        return bool(current and current["open"] <= now < current["close"])
    except (ValueError, KeyError, TypeError):
        return False


def recoverable_failure(error):
    import httpx

    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code == 429 or error.response.status_code >= 500
    if isinstance(error, TossHTTPError):
        return error.status == 429 or 500 <= error.status < 600
    if isinstance(error, TossError):
        return str(error) in {
            "websocket_reconnect_budget_exhausted",
            "toss_read_transport_failure",
            "toss_token_transport_failure",
        }
    return isinstance(error, (httpx.TransportError, TimeoutError, ConnectionError))


def failure_summary(error):
    import sys
    import traceback

    # Deliberately omit exception text, local variables, request headers and response bodies.
    return {
        "event": "collector_failure",
        "type": type(error).__name__,
        "http_status": error.status if isinstance(error, TossHTTPError) else None,
        "recoverable": recoverable_failure(error),
        "location": [
            f"{Path(f.filename).name}:{f.lineno}"
            for f in traceback.extract_tb(sys.exc_info()[2])[-4:]
        ],
    }


def watchdog_notify():
    """systemd detects an event loop or shutdown that no longer makes progress."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.settimeout(1)
        with suppress(OSError):
            sock.sendto(b"WATCHDOG=1", address)


async def cancel_tasks(tasks, *, timeout=10):
    for task in tasks:
        task.cancel()
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in done:
        with suppress(asyncio.CancelledError, Exception):
            task.result()
    if pending:
        # Starting a second broker session beside leaked tasks is unsafe. Restart
        # the process; SQLite journals and uncertain-order records survive it.
        print(
            json.dumps(
                {
                    "event": "collector_shutdown_stalled",
                    "tasks": sorted(t.get_name() for t in pending),
                }
            ),
            flush=True,
        )
        os._exit(1)


async def supervised_collect(values, expected_ip, store, stop, execution=None):
    failures = 0
    while not stop.is_set():
        started = time.monotonic()
        try:
            await collect(values, expected_ip, store, stop, execution)
            return
        except Exception as error:
            report = failure_summary(error)
            print(json.dumps(report), flush=True)
            if not report["recoverable"]:
                raise
            failures = 1 if time.monotonic() - started >= 300 else failures + 1
            delay = max(min(300, 30 * 2 ** min(failures - 1, 4)), getattr(error, "retry_after", 0))
            store.connected = False
            store.account_view = None
            store.reason = "reconnecting"
            store.publish(time.time())
            remaining = delay
            while remaining > 0 and not stop.is_set():
                watchdog_notify()
                interval = min(10, remaining)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                except TimeoutError:
                    pass
                remaining -= interval


class ObservationStore:
    def __init__(
        self,
        path: str,
        status_path: str,
        market_path: str | None = None,
        account_path: str | None = None,
        universe_path: str | None = None,
    ):
        self.status_path = Path(status_path)
        self.market_path = market_path
        self.account_path = account_path
        self.account_view = None
        self.account_issue = "awaiting_snapshot"
        self.order_generation = 0
        self.portfolio_generation = 0
        self.promote = None
        self.warning_events = {}
        self.quotes = {}
        self.daily_bars = None
        self.universe_path = universe_path
        self.universe = None
        self.instruments = {}
        self.watch_symbols = None
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS latest(
                topic TEXT PRIMARY KEY, payload TEXT, received_at REAL);
            CREATE TABLE IF NOT EXISTS account(
                kind TEXT PRIMARY KEY, payload TEXT, received_at REAL);
            CREATE TABLE IF NOT EXISTS warning_snapshot(symbol TEXT PRIMARY KEY, data TEXT);
        """)
        self.journal = OrderJournal(self.db)
        from veyquant.market_metrics import MarketMetrics

        self.metrics = MarketMetrics(self.db)
        self.connected = False
        self.snapshot_at = None
        self.last_frame_at = None
        self.market_at = {}
        self.frame_count = 0
        self.reason = "starting"

    def update_warnings(self, symbol, rows, now):
        prior = self.db.execute(
            "SELECT data FROM warning_snapshot WHERE symbol=?", (symbol,)
        ).fetchone()
        previous = json.loads(prior[0]) if prior else {}
        found = {}
        for warning in rows:
            identity = hashlib.sha256(
                json.dumps([symbol, warning], sort_keys=True).encode()
            ).hexdigest()
            found[identity] = {
                "id": identity,
                "kind": "new_warning",
                "symbol": symbol,
                "warning": warning,
                "active": True,
                "source": "Toss warnings",
                "collected_at": now,
                "baseline": prior is None,
            }
        for identity, event in previous.items():
            if identity not in found:
                found[identity] = event | {"active": False, "baseline": False}
        self.db.execute(
            "INSERT OR REPLACE INTO warning_snapshot VALUES(?,?)", (symbol, json.dumps(found))
        )
        self.warning_events.update(found)

    def snapshot(self, holdings, orders, now):
        validate_snapshot(holdings, orders)
        for order in orders["orders"]:
            order_record(order)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for kind, data in [("holdings", holdings), ("open_orders", orders)]:
                self.db.execute(
                    "INSERT OR REPLACE INTO account VALUES (?, ?, ?)", (kind, json.dumps(data), now)
                )
            for order in orders["orders"]:
                self.journal.observe(order, now)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        self.snapshot_at = now

    def frame(self, frame, now):
        self.last_frame_at = now
        if frame["type"] != "message":
            return
        channel = frame["topic"].rsplit(":", 1)[0]
        if channel == "personal:order":
            self.journal.event("websocket", frame["data"], now)
            self.order_generation += 1
            self.portfolio_generation += 1
            # The next REST refresh verifies the broker state independently.
            self.account_view = None
            self.account_issue = "order_event_pending"
        else:
            validate_market_frame(channel, frame["data"])
            self.metrics.frame(frame["topic"].rsplit(":", 1)[1], channel, frame["data"], now)
            self.market_at[channel] = now
            if channel.startswith("trade:"):
                symbol = frame["topic"].rsplit(":", 1)[1]
                self.quotes[symbol] = market_quote(symbol, frame["data"], now)
        # Only the latest record per subscribed topic is retained: bounded disk usage.
        self.db.execute(
            "INSERT OR REPLACE INTO latest VALUES (?, ?, ?)",
            (frame["topic"], json.dumps(frame["data"]), now),
        )
        self.frame_count += 1

    def publish(self, now):
        safe = {
            "updated_at": now,
            "connected": self.connected,
            "reason": self.reason,
            "snapshot_at": self.snapshot_at,
            "last_frame_at": self.last_frame_at,
            "market_at": self.market_at,
            "frames_received": self.frame_count,
            "full_account_reconciled": False,
            "live_order_supported": False,
        }
        temporary = self.status_path.with_suffix(".tmp")
        with open(temporary, "w") as f:
            json.dump(safe, f)
        temporary.chmod(0o640)
        os.replace(temporary, self.status_path)
        if self.market_path:
            atomic_json(
                self.market_path,
                {
                    "updated_at": now,
                    "connected": self.connected,
                    "last_realtime_at": self.metrics.last_realtime_at,
                    "metrics": self.metrics.export(self.watch_symbols or (), self.connected, now),
                    "warning_events": list(self.warning_events.values()),
                    "quotes": [
                        self.quotes[s]
                        for s in (
                            self.watch_symbols if self.watch_symbols is not None else self.quotes
                        )
                        if s in self.quotes
                    ],
                    "instruments": self.instruments,
                    **(
                        {
                            "universe_ready": self.universe.fresh()
                            and self.universe.state in {"observing", "partial_prices"}
                            and 0 <= now - self.universe.scan_at <= 900
                        }
                        if self.universe
                        else {}
                    ),
                    **({"daily_bars": self.daily_bars} if self.daily_bars else {}),
                },
            )
        if self.account_path:
            atomic_json(
                self.account_path,
                {
                    "updated_at": now,
                    "available": self.connected and self.account_view is not None,
                    "reason": self.account_issue,
                    **(self.account_view or {}),
                    **self.journal.counts(),
                },
            )


def read_status(path: str | None, now: float) -> dict:
    unavailable = {"connected": False, "reason": "unavailable", "full_account_reconciled": False}
    if not path:
        return unavailable
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 16000:
            return unavailable
        data = json.loads(raw)
        for key in ("updated_at", "snapshot_at", "last_frame_at"):
            if data.get(key) is not None and (
                type(data[key]) not in (int, float) or not math.isfinite(data[key])
            ):
                return unavailable
        if type(data.get("frames_received")) is not int or data["frames_received"] < 0:
            return unavailable
        age = now - data["updated_at"]
        if not 0 <= age <= 10:
            return unavailable | {"reason": "collector_stale"}
        # All output fields are fixed, even if the status file is corrupted.
        return {
            "connected": data["connected"] is True,
            "reason": "observing" if data["connected"] is True else "disconnected",
            "snapshot_at": data.get("snapshot_at"),
            "last_frame_at": data.get("last_frame_at"),
            "frames_received": data.get("frames_received"),
            "full_account_reconciled": False,
        }
    except (OSError, ValueError, KeyError, TypeError):
        return unavailable


async def collect(values, expected_ip, store, stop, execution=None):
    async with http_client() as http:
        response = await http.get("https://checkip.amazonaws.com", follow_redirects=False)
        response.raise_for_status()
        if response.text.strip() != expected_ip:
            raise ValueError("egress_mismatch")
        tokens = TokenManager(
            Credentials(values["TOSS_CLIENT_ID"], values["TOSS_CLIENT_SECRET"]), http
        )
        broker = TossReadOnly(tokens, http)
        account = select_account(await broker.accounts(), values.get("TOSS_ACCOUNT_SEQ", ""))
        from veyquant.universe import SCAN_INTERVAL, Universe

        universe = Universe(store, broker, store.universe_path)
        store.universe = universe
        lock = asyncio.Lock()
        orders_changed = asyncio.Event()
        worker = None
        if execution:
            from veyquant.adapters.toss_execution import TossExecutionTransport
            from veyquant.live_worker import LiveWorker

            worker = LiveWorker(
                *execution, store, broker, TossExecutionTransport(tokens, http, account), account
            )

        worker_progress = time.monotonic()

        async def execute():
            nonlocal worker_progress
            while True:
                # This task is the sole tick caller. Do not hold the REST refresh
                # lock here: promoting a new symbol waits for a WS acknowledgment,
                # whose reconciliation needs refresh(). The worker independently
                # re-reads its account and checks the stream/order generation.
                if os.environ.get("VEYQUANT_RECOVERY_READ_ONLY") == "1":
                    try:
                        await worker.reconcile()
                    except (TossError, ValueError, KeyError, TypeError):
                        # WS subscription and its initial account snapshot race this task.
                        # Keep receiving; no execution/cancellation is allowed in this mode.
                        store.account_issue = "reconciliation_required"
                else:
                    await worker.tick()
                worker_progress = time.monotonic()
                await asyncio.sleep(2)

        async def monitor():
            while True:
                if os.environ.get("VEYQUANT_RECOVERY_READ_ONLY") != "1":
                    await worker.monitor()
                await asyncio.sleep(1)

        async def refresh():
            async with lock:
                started = time.time()
                generation = store.order_generation
                holdings = await broker.holdings(account)
                orders = await broker.open_orders(account)
                store.snapshot(holdings, orders, started)
                if store.account_path:
                    store.account_view = None
                    stage = "order_details"
                    try:
                        missing = store.journal.missing_from_open(orders["orders"])
                        for oid in missing[:10]:
                            queried_at = time.time()
                            detail = await broker.order(account, oid)
                            if detail.get("orderId") != oid:
                                raise ValueError("order_detail_mismatch")
                            store.journal.observe(detail, queried_at)
                        stage = "buying_power"
                        power = await broker.buying_power(account)
                        stage = "conditional_orders"
                        conditionals = await broker.conditional_orders(account)
                        stage = "market_calendar"
                        calendar = await broker.market_calendar()
                        stage = "validation"
                        store.account_view = build_account_view(
                            holdings, orders, power, conditionals, calendar, started
                        )
                        if generation != store.order_generation:
                            store.account_view = None
                            store.account_issue = "order_event_pending"
                        else:
                            store.account_issue = "available"
                    except (TossError, ValueError, KeyError, TypeError) as error:
                        # Extra readiness reads must not stop market/order event collection.
                        store.account_view = None
                        suffix = (
                            str(error.status) if isinstance(error, TossHTTPError) else "invalid"
                        )
                        store.account_issue = f"{stage}_{suffix}"

        async def gap():
            store.connected = False
            store.order_generation += 1
            store.reason = "connection_gap"
            store.publish(time.time())

        def subscriptions(symbols):
            return tuple(
                Subscription(ch, tuple(symbols)) for ch in ("trade:kr", "orderbook:kr") if symbols
            ) + (Subscription("personal:order", (account,)),)

        connection = TossStream(
            tokens,
            subscriptions(worker.pinned_symbols() if worker else ()),
            gap,
            refresh,
            max_reconnects=5,
            on_rejected=universe.reject,
        )

        async def promote(symbol):
            from veyquant.universe import MAX_WATCH

            pins = worker.pinned_symbols()
            if (
                len(pins) > MAX_WATCH
                or symbol not in universe.stocks
                or symbol in universe.rejected
            ):
                raise ValueError("subscription_capacity")
            watch = tuple(dict.fromkeys(pins + universe.watch))[:MAX_WATCH]
            universe.watch = store.watch_symbols = watch
            store.instruments.update({s: universe.stocks[s] for s in watch})
            connection.update_subscriptions(subscriptions(watch))
            started = time.time()
            async with asyncio.timeout(45):
                while True:
                    quote = store.quotes.get(symbol, {})
                    book = store.db.execute(
                        "SELECT received_at FROM latest WHERE topic=?", (f"orderbook:kr:{symbol}",)
                    ).fetchone()
                    if symbol in universe.rejected:
                        raise ValueError("instrument_unavailable")
                    if (
                        store.connected
                        and quote.get("as_of", 0) >= started
                        and book
                        and book[0] >= started
                    ):
                        return
                    await asyncio.sleep(0.25)

        store.promote = promote

        def market_open():
            return (
                regular_market_open(
                    (getattr(worker, "metadata", None) or {}).get("calendar"), time.time()
                )
                if worker
                else True
            )

        async def scan():
            while True:
                if universe.stocks and not market_open():
                    await asyncio.sleep(30)
                    continue
                try:
                    pins = worker.pinned_symbols if worker else lambda: ()
                    await universe.scan(pins)
                    # Seed with timestamped REST data; it does not become fresh
                    # just because a new subscription was acknowledged.
                    watch = set(universe.watch)
                    store.quotes = {s: q for s, q in store.quotes.items() if s in watch}
                    for s in watch:
                        q = universe.prices.get(s)
                        if q and q["as_of"] > store.quotes.get(s, {}).get("as_of", 0):
                            store.quotes[s] = q
                    connection.update_subscriptions(subscriptions(universe.watch))
                except Exception:
                    universe.state = "scan_unavailable"
                    universe.publish()
                await asyncio.sleep(SCAN_INTERVAL)

        async def stream():
            async with aclosing(connection.frames()) as frames:
                async for frame in frames:
                    if frame["type"] == "subscriptions":
                        store.connected, store.reason = True, "observing"
                    store.frame(frame, time.time())
                    if frame.get("topic", "").startswith("personal:order:"):
                        orders_changed.set()

        async def periodic_refresh():
            while True:
                with suppress(TimeoutError):
                    await asyncio.wait_for(orders_changed.wait(), timeout=60)
                await asyncio.sleep(2)
                orders_changed.clear()
                await refresh()

        async def publish():
            while True:
                store.publish(time.time())
                if not worker or time.monotonic() - worker_progress < 120:
                    watchdog_notify()
                await asyncio.sleep(2)

        async def warnings():
            while True:
                if not market_open():
                    await asyncio.sleep(30)
                    continue
                for symbol in tuple(universe.stocks):
                    if not market_open():
                        break
                    try:
                        rows = await broker.stock_warnings(symbol)
                        store.update_warnings(symbol, rows, time.time())
                    except Exception:
                        pass  # A failed read is not normalization of a known warning.
                    await asyncio.sleep(0.2)
                await asyncio.sleep(60)

        async def minute_history():
            semaphore = asyncio.Semaphore(3)

            async def update(symbol):
                async with semaphore:
                    try:
                        while getattr(broker, "research_reads_active", 0):
                            await asyncio.sleep(0.5)
                        rows = await broker.minute_candles(symbol)
                        store.metrics.seed(symbol, rows, time.time())
                    except Exception:
                        pass  # Preserve the original timestamp; stale baselines expire.

            while True:
                if not market_open():
                    await asyncio.sleep(30)
                    continue
                symbols = tuple(universe.watch)
                await asyncio.gather(*(update(s) for s in symbols))
                store.metrics.minute_baselines = {
                    s: value
                    for s, value in store.metrics.minute_baselines.items()
                    if s in universe.watch
                }
                await asyncio.sleep(60)

        tasks = [
            asyncio.create_task(f(), name=f.__name__)
            for f in (stream, scan, periodic_refresh, publish, stop.wait)
        ]
        if worker:
            tasks.extend(
                [
                    asyncio.create_task(execute(), name="execute"),
                    asyncio.create_task(monitor(), name="monitor"),
                ]
            )
            if os.environ.get("VEYQUANT_DECISION_V2") == "1":
                from veyquant.research_context import ResearchContext

                tasks.append(
                    asyncio.create_task(ResearchContext(worker).serve(), name="research_context")
                )
                tasks.append(asyncio.create_task(warnings(), name="warnings"))
                tasks.append(asyncio.create_task(minute_history(), name="minute_history"))
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                try:
                    task.result()
                except Exception as error:
                    # Record the initiating failure before cleanup can stall.
                    print(
                        json.dumps(failure_summary(error) | {"task": task.get_name()}), flush=True
                    )
                    raise
        finally:
            await cancel_tasks(tasks)
            await gap()
            if worker:
                worker.close()


def main():
    import argparse
    import logging
    import resource

    logging.disable(logging.CRITICAL)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--expected-ip", required=True)
    parser.add_argument("--market-export")
    parser.add_argument("--account-export")
    parser.add_argument("--universe-export")
    for name in ("execution-db", "execution-control", "execution-reports", "execution-status"):
        parser.add_argument("--" + name)
    args = parser.parse_args()
    import fcntl

    lease = open(args.db + ".lock", "a")
    try:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lease.close()
        return 0
    execution = (
        args.execution_db,
        args.execution_control,
        args.execution_reports,
        args.execution_status,
    )
    if any(execution) and not all(execution):
        parser.error("all execution paths are required together")
    store = ObservationStore(
        args.db, args.status, args.market_export, args.account_export, args.universe_export
    )
    code = 0
    try:
        values = json.loads((Path(os.environ["CREDENTIALS_DIRECTORY"]) / "toss").read_text())

        async def run():
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, stop.set)
            await supervised_collect(
                values, args.expected_ip, store, stop, execution if all(execution) else None
            )

        asyncio.run(run())
    except TossHTTPError as error:
        code = 78 if error.status in (400, 401, 403) else 1
    except (ValueError, KeyError):
        code = 78
    except Exception:
        code = 1
    finally:
        store.connected = False
        store.reason = "stopped" if code == 0 else "operator_check_required"
        store.publish(time.time())
        store.db.close()
        lease.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
