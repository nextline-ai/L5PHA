"""Five-second public-market bins; volume baselines require contiguous real coverage."""

import json
import math
from datetime import datetime
from decimal import Decimal

from veyquant.surveillance import volume_zscore


class MarketMetrics:
    def __init__(self, db):
        self.db = db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS market_bins(
                symbol TEXT, bin INTEGER, price REAL, volume REAL, at REAL,
                PRIMARY KEY(symbol,bin));
        """)
        self.last_realtime_at = 0
        self.continuous_since = {}
        self.last_trade = {}
        self.last_cleanup = 0
        self.minute_baselines = {}

    def seed(self, symbol, response, now):
        rows = response["candles"]
        if not isinstance(rows, list) or len(rows) > 120:
            raise ValueError("invalid_minute_candles")
        values = {}
        for row in rows:
            dt = datetime.fromisoformat(row["timestamp"])
            if dt.tzinfo is None or row["currency"] != "KRW":
                raise ValueError("invalid_minute_candle_scope")
            at = dt.timestamp()
            if at + 60 > now:
                continue
            price, volume = Decimal(row["closePrice"]), Decimal(row["volume"])
            if not price.is_finite() or not volume.is_finite() or price <= 0 or volume < 0:
                raise ValueError("invalid_minute_candle")
            if not all(math.isfinite(float(v)) for v in (price, volume)):
                raise ValueError("invalid_minute_candle")
            if at in values:
                raise ValueError("duplicate_minute_candle")
            values[at] = (float(price), float(volume))
        self.minute_baselines[symbol] = (now, values)

    def frame(self, symbol, channel, data, now):
        at = datetime.fromisoformat(data["timestamp"]).timestamp()
        if not 0 <= now - at <= 30:
            return
        self.last_realtime_at = now
        if not channel.startswith("trade:"):
            return
        previous = self.last_trade.get(symbol)
        if previous is not None and at < previous:
            return
        self.last_trade[symbol] = at
        price, volume = float(Decimal(data["price"])), float(Decimal(data["volume"]))
        self.db.execute(
            "INSERT INTO market_bins VALUES(?,?,?,?,?) "
            "ON CONFLICT(symbol,bin) DO UPDATE SET price=excluded.price,"
            "volume=market_bins.volume+excluded.volume,at=excluded.at",
            (symbol, int(at // 5), price, volume, at),
        )

    def export(self, symbols, connected, now):
        selected = set(symbols)
        self.continuous_since = {
            s: t for s, t in self.continuous_since.items() if connected and s in selected
        }
        if connected:
            for s in selected:
                self.continuous_since.setdefault(s, now)
        if now - self.last_cleanup > 300:
            self.db.execute("DELETE FROM market_bins WHERE bin<?", (int((now - 7500) // 5),))
            self.last_cleanup = now
        result = []
        for symbol in symbols:
            rows = self.db.execute(
                "SELECT bin,price,volume,at FROM market_bins WHERE symbol=? ORDER BY bin", (symbol,)
            ).fetchall()
            movement, zscore, spread = None, None, None
            price_at, price_source, book_at = 0, "websocket", 0
            volume_movement, baseline_at = None, None
            if rows:
                target = now - 300
                old = [r for r in rows if target - 10 <= r[3] <= target]
                if old and self.continuous_since.get(symbol, now) <= target:
                    movement = rows[-1][1] / old[-1][1] - 1
                    price_at = rows[-1][3]
                boundary = now
                if self.continuous_since.get(symbol, now) <= boundary - 6300:
                    windows = [
                        sum(
                            r[2]
                            for r in rows
                            if boundary - 300 * (i + 1) <= r[3] < boundary - 300 * i
                        )
                        for i in range(21)
                    ]
                    zscore = volume_zscore(windows[0], list(reversed(windows[1:])))
                    volume_movement, baseline_at = movement, rows[-1][3]
            seeded = self.minute_baselines.get(symbol)
            if seeded and 0 <= now - seeded[0] <= 90:
                bars = seeded[1]
                boundary = int(now // 60) * 60
                stamps = [boundary - n * 60 for n in range(1, 106)]
                if all(at in bars for at in stamps):
                    windows = [
                        sum(bars[at][1] for at in stamps[i : i + 5]) for i in range(0, 105, 5)
                    ]
                    zscore = volume_zscore(windows[0], list(reversed(windows[1:])))
                    baseline_at = boundary
                # Completed 1m data also provides an exact dated 5m comparison when a
                # subscription has just rotated. Do not compare across a trading gap.
                if all(boundary - n * 60 in bars for n in range(1, 7)):
                    volume_movement = bars[boundary - 60][0] / bars[boundary - 360][0] - 1
                    if movement is None:
                        movement = volume_movement
                        price_at, price_source = boundary, "completed_minutes"
            raw = self.db.execute(
                "SELECT payload FROM latest WHERE topic=?", (f"orderbook:kr:{symbol}",)
            ).fetchone()
            if raw:
                book = json.loads(raw[0])
                at = (
                    datetime.fromisoformat(book["timestamp"]).timestamp()
                    if book.get("timestamp")
                    else 0
                )
                bids = [float(r["price"]) for r in book["bids"] if Decimal(r["volume"]) > 0]
                asks = [float(r["price"]) for r in book["asks"] if Decimal(r["volume"]) > 0]
                if bids and asks and 0 <= now - at <= 30:
                    bid, ask = max(bids), min(asks)
                    if 0 < bid <= ask:
                        spread = (ask - bid) / ((ask + bid) / 2) * 10000
                        book_at = at
            result.append(
                {
                    "symbol": symbol,
                    "as_of": rows[-1][3] if rows else 0,
                    "change_5m": movement,
                    "price_as_of": price_at,
                    "price_source": price_source,
                    "volume_change_5m": volume_movement,
                    "volume_zscore": zscore,
                    "spread_bp": spread,
                    "book_as_of": book_at,
                    "volume_baseline": "20 preceding complete 5-minute windows",
                    "continuous_since": self.continuous_since.get(symbol),
                    "volume_as_of": baseline_at,
                }
            )
        return result
