"""Timestamped portfolio marks; never refresh an old price by changing its timestamp."""

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation


def bid_mark(symbol, book, now, max_age):
    try:
        stamp = datetime.fromisoformat(book["timestamp"])
        if stamp.tzinfo is None or not 0 <= now - stamp.timestamp() <= max_age:
            return None
        if book["currency"] != "KRW":
            return None
        levels = []
        for side in ("bids", "asks"):
            prices = []
            for row in book[side]:
                price, volume = Decimal(row["price"]), Decimal(row["volume"])
                if not price.is_finite() or not volume.is_finite() or price <= 0 or volume < 0:
                    return None
                if volume > 0:
                    prices.append(price)
            if not prices:
                return None
            levels.append(prices)
        bid, ask = max(levels[0]), min(levels[1])
        if bid > ask:
            return None
        return {
            "symbol": symbol,
            "price": str(bid),
            "as_of": stamp.timestamp(),
            "basis": "best_bid",
        }
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None


def portfolio_mark(observations, symbol, trade, now, max_age):
    if trade and 0 <= now - trade.get("as_of", 0) <= max_age:
        return {
            "symbol": symbol,
            "price": trade["price"],
            "as_of": trade["as_of"],
            "basis": "last_trade",
        }
    row = observations.db.execute(
        "SELECT payload FROM latest WHERE topic=?", ("orderbook:kr:" + symbol,)
    ).fetchone()
    if row:
        return bid_mark(symbol, json.loads(row[0]), now, max_age)
    return None
