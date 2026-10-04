"""Owner display only: month-to-date P&L, isolated from daily order limits."""

import json
import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from veyquant.execution import day_key

D = Decimal


def monthly_performance(store, loss, now, bars, *, opening_date=None):
    """Keep the latest verified display valuation across restarts and closed sessions."""
    month = day_key(now)[:7]
    try:
        value = _monthly_performance(store, loss, now, bars, opening_date=opening_date)
        if value["state"] == "available":
            store.db.execute(
                "INSERT INTO performance_marks(month,value) VALUES (?,?) "
                "ON CONFLICT(month) DO UPDATE SET value=excluded.value",
                (month, json.dumps(value)),
            )
            return value
        row = store.db.execute(
            "SELECT value FROM performance_marks WHERE month=?", (month,)
        ).fetchone()
        if row:
            saved = json.loads(row[0])
            if (
                saved.get("month") == month
                and saved.get("state") == "available"
                and D(saved["pnl_krw"]).is_finite()
                and 0 <= saved["as_of"] <= now
            ):
                return saved | {"refreshing": True}
        return value
    except (ValueError, TypeError, KeyError, InvalidOperation, sqlite3.Error):
        return {"state": "valuation_required", "month": month}


def _monthly_performance(store, loss, now, bars, *, opening_date=None):
    """Reuse a reconciled daily valuation; never query the broker or sum daily marks.

    A persisted monthly opening valuation includes carried unrealized P&L. It can
    be recovered from the first calendar day's trusted daily baseline, or from
    previous-month closes before any current-month fills. Otherwise report missing
    evidence instead of silently presenting a partial month as a full month.
    """
    day = day_key(now)
    month, start = day[:7], day[:7] + "-01"
    empty = {"state": "valuation_required", "month": month}
    try:
        valid = not (
            not loss
            or loss.get("day") != day
            or loss.get("state")
            not in {
                "within_limit",
                "breached",
            }
        )
        if not valid and opening_date is None:
            return empty
        daily_pnl = D(loss["pnl_krw"]) if valid else D(0)
        if not daily_pnl.is_finite():
            return empty
        db = store.db
        with store.transaction():
            daily = db.execute("SELECT baseline FROM execution_days WHERE day=?", (day,)).fetchone()
            if daily is None:
                return empty
            if not valid:
                boundary = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo("Asia/Seoul"))
                # Caller confirms a reconciled pre-session account. Never use this for orders.
                if (
                    not opening_date < day
                    or db.execute(
                        "SELECT 1 FROM execution_fills WHERE observed_at>=? LIMIT 1",
                        (boundary.timestamp(),),
                    ).fetchone()
                ):
                    return empty
            total = D(daily[0]) + daily_pnl
            row = db.execute(
                "SELECT baseline FROM performance_months WHERE month=?", (month,)
            ).fetchone()
            if row is None:
                opening = db.execute(
                    "SELECT baseline FROM execution_days WHERE day=?", (start,)
                ).fetchone()
                boundary = datetime.fromisoformat(start).replace(tzinfo=ZoneInfo("Asia/Seoul"))
                first = db.execute("SELECT MIN(observed_at) FROM execution_fills").fetchone()[0]
                recent = db.execute(
                    "SELECT 1 FROM execution_fills WHERE observed_at>=? LIMIT 1",
                    (boundary.timestamp(),),
                ).fetchone()
                if opening is not None:
                    baseline, source = D(opening[0]), "month_open_daily_baseline"
                elif first is None or first >= boundary.timestamp():
                    if (
                        first is None
                        and db.execute(
                            "SELECT 1 FROM execution_positions WHERE quantity>0 LIMIT 1"
                        ).fetchone()
                    ):
                        return empty | {"state": "month_baseline_required"}
                    baseline, source = D(0), "managed_inception_this_month"
                elif recent is None:
                    baseline = sum(
                        (D(r[0]) for r in db.execute("SELECT realized FROM execution_fills")), D(0)
                    )
                    for position in db.execute(
                        "SELECT symbol,quantity,basis FROM execution_positions WHERE quantity>0"
                    ):
                        cached = bars.get(position["symbol"], {})
                        if not 0 <= now - cached.get("updated_at", 0) <= 1800:
                            return empty | {"state": "month_baseline_required"}
                        previous = [b for b in cached.get("bars", []) if b["date"] < start]
                        if not previous:
                            return empty | {"state": "month_baseline_required"}
                        last = max(previous, key=lambda b: b["date"])
                        distance = (
                            boundary.date() - datetime.fromisoformat(last["date"]).date()
                        ).days
                        close = D(last["close"])
                        if not 1 <= distance <= 14 or not close.is_finite() or close <= 0:
                            return empty | {"state": "month_baseline_required"}
                        baseline += position["quantity"] * close - D(position["basis"])
                    source = "previous_month_completed_closes"
                else:
                    return empty | {"state": "month_baseline_required"}
                if not baseline.is_finite():
                    return empty
                db.execute(
                    "INSERT INTO performance_months(month,baseline,source,created_at) "
                    "VALUES (?,?,?,?)",
                    (month, str(baseline), source, now),
                )
            else:
                baseline = D(row[0])
            pnl = total - baseline
            if not pnl.is_finite():
                return empty
            return {
                "state": "available",
                "month": month,
                "period_start": start,
                "pnl_krw": str(pnl),
                "as_of": now,
            } | ({"valuation_date": opening_date} if not valid else {})
    except (ValueError, TypeError, KeyError, InvalidOperation, sqlite3.Error):
        # Display statistics must never prevent order reconciliation or publishing.
        return empty
