"""No model calls here: broker-session schedules and persistent condition edges."""

import math
import statistics
from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def session(calendar, now):
    today = calendar["today"]
    date = datetime.fromtimestamp(now, KST).date().isoformat()
    if today["date"] != date:
        raise ValueError("stale_market_calendar")
    regular = (today.get("integrated") or {}).get("regularMarket")
    if not regular:
        return None
    values = []
    # endTime includes the closing auction; execution still uses its separate continuous gate.
    for key in ("startTime", "endTime"):
        dt = datetime.fromisoformat(regular[key])
        if dt.tzinfo is None or dt.astimezone(KST).date().isoformat() != date:
            raise ValueError("invalid_market_calendar")
        values.append(dt.timestamp())
    if not values[0] < values[1]:
        raise ValueError("invalid_market_calendar")
    return {"date": date, "open": values[0], "close": values[1]}


def schedule(calendar, now):
    current = session(calendar, now)
    if not current or datetime.fromtimestamp(now, KST).weekday() >= 5:
        return []
    start, end = current["open"], current["close"]
    # Keep two distinct intraday slots even on an unusually short regular session.
    offset = min(5400, (end - start) / 3)
    return [
        {"id": current["date"] + ":" + name, "at": at, "name": name}
        for name, at in [
            ("morning", start + offset),
            ("afternoon", end - offset),
        ]
    ]


def volume_zscore(current, preceding):
    if len(preceding) < 20 or any(not math.isfinite(x) or x < 0 for x in preceding + [current]):
        return None
    baseline = preceding[-20:]
    mean, deviation = statistics.mean(baseline), statistics.pstdev(baseline)
    if deviation == 0:
        return 0.0 if current <= mean else None
    return (current - mean) / deviation


def observe(store, market, calendar, now, external=()):
    current = session(calendar, now)
    is_open = current is not None and current["open"] <= now < current["close"]
    if is_open:
        slot = int((now - current["open"]) // 1800)
        store.signal_once(
            f"heartbeat:{current['date']}:{slot}", now, {"condition": "heartbeat", "symbol": None}
        )
        for item in market.get("metrics", []):
            symbol = item["symbol"]
            movement, zscore = item.get("change_5m"), item.get("volume_zscore")
            spread = item.get("spread_bp")
            fresh = (
                0
                <= now - item.get("price_as_of", item.get("as_of", 0))
                <= (90 if item.get("price_source") == "completed_minutes" else 30)
            )
            book_fresh = 0 <= now - item.get("book_as_of", item.get("as_of", 0)) <= 30
            volume_fresh = 0 <= now - (item.get("volume_as_of") or item.get("as_of", 0)) <= 90
            volume_movement = item.get("volume_change_5m", movement)
            predicates = {
                # Preserve durable legacy edge keys across upgrades; event labels name
                # the new thresholds. Entry and recovery differ to avoid boundary chatter.
                "price_3pct": (
                    "price_5pct",
                    abs(movement) >= 0.05 if fresh and movement is not None else None,
                    abs(movement) < 0.02 if fresh and movement is not None else False,
                ),
                "price_volume": (
                    "price_volume",
                    abs(volume_movement) >= 0.02 and zscore >= 6
                    if volume_fresh and volume_movement is not None and zscore is not None
                    else None,
                    abs(volume_movement) < 0.01 or zscore < 3
                    if volume_fresh and volume_movement is not None and zscore is not None
                    else False,
                ),
                "spread_50bp": (
                    "spread_100bp",
                    spread >= 100 if book_fresh and spread is not None else None,
                    spread < 60 if book_fresh and spread is not None else False,
                ),
            }
            for direction, sign in (("up", 1), ("down", -1)):
                shock = sign * movement >= 0.08 if fresh and movement is not None else None
                store.edge(
                    f"price_shock_{direction}:{symbol}",
                    shock,
                    now,
                    {"condition": "price_shock", "symbol": symbol, "metrics": item},
                    immediate=True,
                    reset_allowed=sign * movement < 0.05
                    if fresh and movement is not None
                    else False,
                )
            for key, (condition, active, recovered) in predicates.items():
                store.edge(
                    f"{key}:{symbol}",
                    active,
                    now,
                    {"condition": condition, "symbol": symbol, "metrics": item},
                    reset_allowed=recovered,
                )
        # Stream silence is distinct from a stock having no trades. Heartbeats alone do not
        # make market data fresh, and changing the subscription cannot reset this timer.
        last = max(market.get("last_realtime_at") or 0, current["open"])
        store.edge(
            "realtime_gap",
            now - last >= 30,
            now,
            {"condition": "realtime_gap", "symbol": None, "last_at": last},
            immediate=True,
        )
    for event in external:
        if event.get("kind") == "new_warning":
            if event.get("baseline"):
                store.db.execute(
                    "INSERT OR IGNORE INTO decision_edges VALUES(?,?,?)",
                    (event["id"], int(event.get("active", False)), now),
                )
            store.edge(event["id"], event.get("active"), now, event, immediate=True)
        elif event.get("kind") == "dart_important":
            store.signal_once(event["id"], now, event)
