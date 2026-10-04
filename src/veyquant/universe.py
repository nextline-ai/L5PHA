"""Toss domestic equity catalogue, whole-market scans and bounded live candidates.

All requests share the collector's TokenManager. A full catalogue is replaced only
after every market and detail batch succeeds; cached timestamps never become fresh
merely because the process restarted. The catalogue is not order authorization.
"""

import asyncio
import json
import time
from datetime import datetime
from decimal import Decimal

from veyquant.shadow_contract import (
    atomic_json,
    bounded_json,
    domestic_symbol,
    finite_time,
    market_quote,
)
from veyquant.trading_state import KST, completed_bars

MARKETS = ("KOSPI", "KOSDAQ", "KR_ETC")
EQUITIES = {"STOCK", "FOREIGN_STOCK", "DEPOSITARY_RECEIPT", "REIT", "INFRASTRUCTURE_FUND"}
MAX_STOCKS = 12000
MAX_WATCH = 49  # trade + orderbook for each, plus one personal order channel <= 100.
SCAN_INTERVAL = 300
CATALOG_BYTES = 8 * 1024 * 1024


def stock_record(row):
    if (
        not isinstance(row, dict)
        or not domestic_symbol(row.get("symbol"))
        or row.get("market") not in MARKETS
        or row.get("currency") != "KRW"
        or not isinstance(row.get("name"), str)
        or not 1 <= len(row["name"]) <= 100
        or row.get("status") not in {"ACTIVE", "SCHEDULED", "DELISTED"}
        or not isinstance(row.get("securityType"), str)
        or type(row.get("isCommonShare")) is not bool
    ):
        raise ValueError("invalid_stock_metadata")
    detail = row.get("koreanMarketDetail")
    reason = (
        "not_equity"
        if row["securityType"] not in EQUITIES
        else "not_active"
        if row["status"] != "ACTIVE"
        else "metadata_missing"
        if not isinstance(detail, dict)
        else "liquidation"
        if detail.get("liquidationTrading") is not False
        else "suspended"
        if detail.get("krxTradingSuspended") is not False
        else "eligible"
    )
    return {
        "symbol": row["symbol"],
        "name": row["name"],
        "market": row["market"],
        "security_type": row["securityType"],
        "common_share": row["isCommonShare"],
        "eligibility": reason,
    }


def validate_trade_stock(rows, warnings, symbol, side):
    if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("symbol") != symbol:
        raise ValueError("instrument_unavailable")
    record = stock_record(rows[0])
    if record["eligibility"] != "eligible":
        raise ValueError("instrument_restricted")
    if not isinstance(warnings, list) or len(warnings) > 100:
        raise ValueError("instrument_unavailable")
    for warning in warnings:
        code = warning.get("warningType") if isinstance(warning, dict) else None
        # Reducing owned positions is allowed for warning/risk designations, but
        # never during VI, liquidation, overheating or an unrecognized condition.
        if side != "SELL" or code not in {"INVESTMENT_WARNING", "INVESTMENT_RISK"}:
            raise ValueError("instrument_restricted")
    return record


class Universe:
    def __init__(self, observations, broker, path=None, clock=time.time):
        self.observations, self.broker, self.path, self.clock = observations, broker, path, clock
        self.db = observations.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS universe_cache(key TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS universe_visits(symbol TEXT PRIMARY KEY, at REAL NOT NULL);
        """)
        self.stocks, self.prices, self.bars, self.watch = {}, {}, {}, ()
        self.catalog_at = self.scan_at = 0
        self.covered = 0
        self.state = "starting"
        self.rejected = set()
        row = self.db.execute("SELECT data FROM universe_cache WHERE key='catalog'").fetchone()
        if row:
            try:
                data = json.loads(row[0])
                if not finite_time(data["at"]) or not 0 < len(data["stocks"]) <= MAX_STOCKS:
                    raise ValueError("invalid_cached_catalogue")
                if any(not domestic_symbol(s) for s in data["stocks"]):
                    raise ValueError("invalid_cached_catalogue")
                self.stocks, self.catalog_at = data["stocks"], data["at"]
            except (ValueError, TypeError, KeyError):
                self.state = "catalogue_unavailable"

    def fresh(self):
        now = self.clock()
        return (
            0 <= now - self.catalog_at <= 30 * 3600
            and datetime.fromtimestamp(now, KST).date()
            == datetime.fromtimestamp(self.catalog_at, KST).date()
        )

    async def catalogue(self):
        if self.fresh():
            return
        listed = {}
        for market in MARKETS:
            rows = await self.broker.list_stocks(market)
            if not isinstance(rows, list) or len(rows) > MAX_STOCKS:
                raise ValueError("invalid_stock_list")
            for row in rows:
                if row.get("securityType") not in EQUITIES:
                    continue
                symbol = row.get("symbol")
                if not domestic_symbol(symbol) or symbol in listed:
                    raise ValueError("duplicate_or_invalid_symbol")
                listed[symbol] = market
        if not 0 < len(listed) <= MAX_STOCKS:
            raise ValueError("incomplete_catalogue")
        stocks = {}
        symbols = sorted(listed)
        for start in range(0, len(symbols), 200):
            batch = tuple(symbols[start : start + 200])
            rows = await self.broker.stock_info(batch)
            if not isinstance(rows, list) or len(rows) != len(batch):
                raise ValueError("incomplete_stock_details")
            seen = set()
            for row in rows:
                record = stock_record(row)
                s = record["symbol"]
                if s not in batch or s in seen or record["market"] != listed[s]:
                    raise ValueError("stock_metadata_mismatch")
                seen.add(s)
                # Funds/warrants are visible neither as cash stocks nor as candidates.
                if record["security_type"] in EQUITIES:
                    stocks[s] = record
        if not stocks:
            raise ValueError("empty_equity_catalogue")
        self.stocks, self.catalog_at = stocks, self.clock()
        self.rejected.clear()
        self.db.execute(
            "INSERT OR REPLACE INTO universe_cache VALUES ('catalog',?)",
            (json.dumps({"at": self.catalog_at, "stocks": stocks}, ensure_ascii=False),),
        )

    async def scan(self, pinned=()):
        await self.catalogue()
        previous, scanned = self.prices, {}
        symbols = sorted(self.stocks)
        for start in range(0, len(symbols), 200):
            while getattr(self.broker, "research_reads_active", 0):
                await asyncio.sleep(0.5)
            batch = tuple(symbols[start : start + 200])
            rows = await self.broker.prices(batch)
            if not isinstance(rows, list) or len(rows) > len(batch):
                raise ValueError("invalid_price_batch")
            seen = set()
            for row in rows:
                s = row.get("symbol")
                if s not in batch or s in seen:
                    raise ValueError("price_symbol_mismatch")
                seen.add(s)
                try:
                    scanned[s] = market_quote(s, row | {"price": row["lastPrice"]}, self.clock())
                except (ValueError, KeyError, TypeError):
                    continue  # Missing timestamp is unavailable, never a fresh zero price.
        self.prices, self.scan_at, self.covered = scanned, self.clock(), len(scanned)
        pins = tuple(
            s for s in (pinned() if callable(pinned) else pinned) if s not in self.rejected
        )
        watch = self.select(pins, previous)
        # Only the finite live candidate set needs candles. The whole universe is
        # still scanned by REST; no thousands-of-model-calls fan-out is introduced.
        for s in watch:
            if getattr(self.broker, "research_reads_active", 0):
                continue  # Reserve the shared chart bucket for the active research snapshot.
            cached = self.bars.get(s)
            if (
                cached
                and 0 <= self.clock() - cached["updated_at"] < 1800
                and datetime.fromtimestamp(cached["updated_at"], KST).date()
                == datetime.fromtimestamp(self.clock(), KST).date()
            ):
                continue
            try:
                bars = completed_bars(await self.broker.daily_candles(s), self.clock())
                self.bars[s] = {"symbol": s, "updated_at": self.clock(), "bars": bars}
            except Exception:
                self.bars.pop(s, None)
        # A fill may arrive while REST/candle reads are in progress. Re-pin from
        # the current ledger before publishing or replacing the subscription.
        pins = tuple(
            s for s in (pinned() if callable(pinned) else pinned) if s not in self.rejected
        )
        if len(pins) > MAX_WATCH:
            raise ValueError("subscription_capacity")
        watch = tuple(s for s in dict.fromkeys(pins + watch) if s not in self.rejected)[:MAX_WATCH]
        self.watch = watch
        self.bars = {s: b for s, b in self.bars.items() if s in watch}
        self.observations.daily_bars = dict(self.bars)
        self.observations.instruments = {s: self.stocks[s] for s in watch if s in self.stocks}
        self.observations.watch_symbols = watch
        for s in watch:
            if s not in pins:
                self.db.execute(
                    "INSERT OR REPLACE INTO universe_visits VALUES (?,?)", (s, self.scan_at)
                )
        self.state = "observing" if self.covered == len(self.stocks) else "partial_prices"
        self.publish()
        return watch

    def select(self, pinned, previous):
        pins = list(dict.fromkeys(pinned))
        if len(pins) > MAX_WATCH or any(not domestic_symbol(s) for s in pins):
            raise ValueError("subscription_capacity")
        visits = dict(self.db.execute("SELECT symbol,at FROM universe_visits"))
        available = [
            s
            for s, row in self.stocks.items()
            if row["eligibility"] == "eligible"
            and s not in pins
            and s not in self.rejected
            and s in self.prices
        ]
        capacity = MAX_WATCH - len(pins)
        # Half of the spare slots explore least-recently-watched stocks. Absolute
        # price changes prioritize the other half; no buy direction is implied.
        rotation = sorted(available, key=lambda s: (visits.get(s, 0), s))[: (capacity + 1) // 2]

        def movement(s):
            old = previous.get(s)
            return (
                abs(Decimal(self.prices[s]["price"]) / Decimal(old["price"]) - 1)
                if old
                else Decimal(0)
            )

        ranked = sorted(
            (s for s in available if s not in rotation),
            key=lambda s: (-movement(s), visits.get(s, 0), s),
        )
        return tuple(pins + rotation + ranked[: capacity - len(rotation)])

    def reject(self, keys):
        self.rejected.update(
            k.rsplit(":", 1)[-1] for k in keys if k.startswith(("trade:kr:", "orderbook:kr:"))
        )
        self.watch = tuple(s for s in self.watch if s not in self.rejected)
        self.observations.watch_symbols = self.watch
        self.publish()

    def publish(self):
        if not self.path:
            return
        rows = []
        for s, row in sorted(self.stocks.items()):
            q = self.prices.get(s)
            rows.append(
                row
                | {
                    "watched": s in self.watch,
                    "subscription_rejected": s in self.rejected,
                    "price": q["price"] if q else None,
                    "as_of": q["as_of"] if q else None,
                }
            )
        atomic_json(
            self.path,
            {
                "updated_at": self.clock(),
                "catalogue_at": self.catalog_at,
                "scan_at": self.scan_at,
                "state": self.state,
                "catalogue_fresh": self.fresh(),
                "covered": self.covered,
                "watch_count": len(self.watch),
                "stocks": rows,
            },
        )


def read_universe(path, now, query="", market="", page=0):
    empty = {
        "available": False,
        "state": "starting",
        "total": 0,
        "eligible": 0,
        "covered": 0,
        "watch_count": 0,
        "items": [],
        "matched": 0,
        "page": page,
        "has_next": False,
    }
    if not path:
        return empty
    try:
        data = bounded_json(path, CATALOG_BYTES)
        if not finite_time(data["updated_at"]) or not 0 <= now - data["updated_at"] <= 900:
            return empty | {"state": "stale"}
        rows = data["stocks"]
        if not isinstance(rows, list) or len(rows) > MAX_STOCKS:
            return empty
        # Only the trusted collector writes this file; export a bounded page.
        selected = [
            r
            for r in rows
            if (not market or r["market"] == market)
            and (not query or query.casefold() in (r["name"] + r["symbol"]).casefold())
        ]
        return {
            "available": True,
            "state": data["state"],
            "catalogue_fresh": data["catalogue_fresh"],
            "catalogue_at": data["catalogue_at"],
            "scan_at": data["scan_at"],
            "total": len(rows),
            "eligible": sum(r["eligibility"] == "eligible" for r in rows),
            "covered": data["covered"],
            "watch_count": data["watch_count"],
            "items": selected[page * 40 : (page + 1) * 40],
            "matched": len(selected),
            "page": page,
            "has_next": (page + 1) * 40 < len(selected),
        }
    except (OSError, ValueError, KeyError, TypeError):
        return empty
