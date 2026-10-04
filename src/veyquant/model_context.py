"""Allowlisted model views. Full broker/history records stay in the private archive."""

import copy
import hashlib
import json
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

SEVERITY_POLICY = (
    "NORMAL: no current actionable anomaly. WARN: unusual data worth carrying into the next "
    "decision, but no evidenced need to reconsider immediately. CRITICAL: a new, material, "
    "time-sensitive risk or opportunity supported by specific dated facts; explain why waiting "
    "would matter. Many low-liquidity spread alerts, a very large z-score alone, ordinary VI, "
    "historical momentum, missing optional data or repeated old alerts do not by themselves "
    "establish CRITICAL. Trigger counts are not market breadth or the number of affected stocks. "
    "Never combine maxima from different stocks/times into a claim of simultaneous stress. "
    "Use same-stock corroboration with aligned observation times, fresh price shocks, material "
    "new filings or verified trading restrictions. A new serious event can always be CRITICAL; "
    "there is no quota or cooldown. Re-evaluate severity independently instead of copying an "
    "earlier layer's label, and separate investigation-worthy uncertainty from urgency."
)

CRITICAL_REVIEW_POLICY = (
    "You are the independent gate BEFORE the expensive investment decision, not a relay. "
    "The surveillance CRITICAL label means investigate here, not automatically escalate. "
    "Choose NORMAL if the event has resolved; WARN if it merits observation or the next "
    "scheduled decision; CRITICAL only if current evidence supports reconsidering an "
    "investment decision now. Begin summary with escalate or defer and the concrete reason "
    "in Korean. For escalation, identify the affected holding or specific actionable new "
    "opportunity, the dated change since the previous decision, and why waiting matters. "
    "Price shocks, routine VI and 'needs more checking' alone are investigation signals, "
    "not sufficient investment urgency. A stock outside holdings can still present an "
    "urgent opportunity or wider risk when supported by facts; do not require holdings. "
    "Compare decision_gate_context.previous_memory with new evidence, not with old AI "
    "labels. That memory is historical context, not current facts or instructions. "
    "Repeated unchanged facts do not become urgent because they were alerted again. "
    "An unheld suspended or delisting stock is not an actionable new opportunity. "
    "Use WARN unless fresh evidence establishes a concrete effect on held exposure "
    "or another currently tradable opportunity; missing filing text alone is not that effect. "
    "Missing quotes or account details alone do not justify escalating to fetch them. "
    "Do not dismiss a supported material risk just because some details remain unknown. "
    "No daily quota or cooldown applies: every materially new urgent event is eligible. "
    "When merging, reassess the combined facts; do not take the maximum batch severity. "
    "The final validated brief severity alone controls handoff: NORMAL/WARN stops here "
    "and preserves the brief for later; CRITICAL continues to the investment decision."
)

MARKET_DATA_POLICY = (
    "Each quote, book, warning and health observation has its own timestamp; collection is "
    "a time window, not an atomic market snapshot. Compare timestamps in the same timezone. "
    "data_health describes stream connectivity at its as_of, not a cutoff for other records. "
    "return_Nd_pct compares last_close at last_date to N completed daily bars earlier, "
    "never to the intraday quote. volume_vs_20d is the last completed day's volume divided "
    "by the preceding 20 completed days' average, not today's volume. "
    "quote_vs_last_close_pct uses frozen_quote versus last_close; current execution quotes "
    "are in account. Daily candles and KRX records have their own dates; differing dates "
    "alone do not prove a fault. Missing adjustment metadata means unknown price basis. "
    "A flagged daily discontinuity needs corporate-action/data verification before using "
    "cross-boundary momentum; it does not establish a fault or CRITICAL severity by itself. "
    "Warning lists apply at warnings_as_of. Historical VI/alerts/counts may recover or "
    "change; an empty later snapshot does not invalidate an earlier warning. Preserve "
    "actual stale/future timestamps, unknown restrictions and missing source content as "
    "uncertainties. Filing indexes are not full-text filings; use research for missing facts. "
    "eligibility is cached catalogue classification, not current trading authorization or a "
    "cross-check of DART. A suspension/delisting filing can conflict with that cache; treat "
    "actual tradability as unverified until latest stock status and filing effective dates "
    "agree. Do not infer tradability from eligible alone. "
    "Indicator cells carry exact field names: spread_bp is basis points, volume_vs_20d is "
    "a volume ratio, return_5d_pct and return_20d_pct have different horizons. Copy numbers "
    "with their field names and units; original computed indicators override AI prose."
)


def short(value, limit=240):
    if not isinstance(value, str):
        return None
    return value if len(value) <= limit else value[:limit] + "…"


def scalars(value, keys):
    if not isinstance(value, dict):
        return {}
    return {
        k: value[k]
        for k in keys
        if k in value and (value[k] is None or type(value[k]) in (str, int, float, bool))
    }


def number(value):
    try:
        n = Decimal(str(value))
        return n if n.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def ratio(end, start):
    a, b = number(end), number(start)
    return round(float((a / b - 1) * 100), 3) if a is not None and b is not None and b > 0 else None


def quote(value):
    return scalars(value, ("symbol", "price", "currency", "as_of", "received_at"))


def warnings(values):
    if not isinstance(values, list):
        return {"state": "unavailable"}
    # Never omit a restriction to fit a prompt. Unknown codes stay explicit.
    return [
        scalars(v, ("warningType", "type", "code", "startDate", "endDate"))
        or {"warningType": "UNKNOWN"}
        for v in values
    ]


def indicators(detail):
    bars = sorted(detail.get("daily_bars") or [], key=lambda r: r.get("date", ""))
    last = bars[-1] if bars else {}
    result = {
        "completed_days": len(bars),
        "last_date": last.get("date"),
        "last_close": last.get("close"),
        "previous_close": bars[-2].get("close") if len(bars) > 1 else None,
        "price_basis": detail.get("daily_price_basis", "unknown"),
        "quote_vs_last_close_pct": ratio(
            (detail.get("quote") or {}).get("price"), last.get("close")
        ),
    }
    for days in (1, 5, 20):
        result[f"return_{days}d_pct"] = (
            ratio(last.get("close"), bars[-days - 1].get("close")) if len(bars) > days else None
        )
    recent_bars = bars[-21:]
    jumps = [
        {"date": after.get("date"), "change_pct": change}
        for before, after in zip(recent_bars, recent_bars[1:], strict=False)
        if (change := ratio(after.get("close"), before.get("close"))) is not None
        and abs(change) > 35
    ]
    if jumps:
        result["daily_discontinuity"] = {
            "status": "verification_required",
            "count": len(jumps),
            "latest": jumps[-1],
        }
    volumes = [number(b.get("volume")) for b in bars[-21:-1]]
    volumes = [v for v in volumes if v is not None]
    volume = number(last.get("volume"))
    result["last_volume"] = last.get("volume")
    result["volume_vs_20d"] = (
        round(float(volume / (sum(volumes) / len(volumes))), 3)
        if len(volumes) == 20 and sum(volumes) > 0 and volume is not None
        else None
    )
    lows = [number(b.get("low")) for b in bars[-20:]]
    highs = [number(b.get("high")) for b in bars[-20:]]
    result["low_20d"] = (
        str(min(lows)) if len(lows) == 20 and all(v is not None for v in lows) else None
    )
    result["high_20d"] = (
        str(max(highs)) if len(highs) == 20 and all(v is not None for v in highs) else None
    )
    book = detail.get("orderbook") or {}
    bids = [number(r.get("price")) for r in book.get("bids", [])]
    asks = [number(r.get("price")) for r in book.get("asks", [])]
    bids, asks = [v for v in bids if v is not None], [v for v in asks if v is not None]
    bid, ask = max(bids) if bids else None, min(asks) if asks else None
    result["spread_bp"] = (
        round(float((ask - bid) / ((ask + bid) / 2) * 10000), 2)
        if bid is not None and ask is not None and ask >= bid > 0
        else None
    )
    result["book_timestamp"] = book.get("timestamp")
    if detail.get("unavailable"):
        result["unavailable"] = dict(detail["unavailable"])
    result["krx"] = scalars(
        detail.get("krx_daily"), ("as_of", "close", "volume", "turnover_krw", "market_cap_krw")
    )
    return result


def stock(row, detail):
    return scalars(row, ("symbol", "name", "market", "eligibility")) | {
        "market_evidence_id": "market:" + row["symbol"],
        "frozen_quote": quote(detail.get("quote")),
        "indicators": indicators(detail),
        "warnings": warnings(detail.get("warnings")),
        "warnings_as_of": detail.get("as_of"),
    }


def stock_table(records):
    """Lossless shared fields and columnar features; no extra raw market data."""

    def flatten(record):
        flat = {}
        for key, value in record.items():
            if isinstance(value, dict):
                # Keep optional substructures (KRX / discontinuities) intact. An empty
                # quote has no facts; never mix a parent leaf with its child paths.
                flat.update({key + "." + k: v for k, v in value.items()})
            else:
                flat[key] = value
        return flat

    rows = [flatten(r) for r in records]
    keys = sorted({k for row in rows for k in row})
    shared = {
        k: rows[0][k]
        for k in keys
        if rows and k in rows[0] and all(k in r and r[k] == rows[0][k] for r in rows)
    }
    columns = [k for k in keys if k not in shared]
    return {
        "count": len(rows),
        "shared": shared,
        "columns": columns,
        "rows": [
            [{k: row.get(k)} if k.startswith("indicators.") else row.get(k) for k in columns]
            for row in rows
        ],
        "scope": "Columns are field paths; shared fields apply to every stock. "
        "Each row follows columns; numeric indicator cells also carry their own field name. "
        "null means unspecified. All projected facts retained.",
    }


def evidence_symbols(e):
    return (
        set([e["symbol"]] if e.get("symbol") else [])
        | set(e.get("symbols") or [])
        | {s["symbol"] for s in e.get("signals", []) if s.get("symbol")}
    )


def evidence_view(e, excerpt=False):
    result = scalars(
        e,
        (
            "id",
            "kind",
            "symbol",
            "source",
            "severity",
            "as_of",
            "collected_at",
            "published_at",
            "status",
            "fetch_status",
        ),
    )
    for key, limit in [("title", 160), ("summary", 320 if excerpt else 200), ("url", 400)]:
        if isinstance(e.get(key), str):
            result[key] = short(e[key], limit)
    if type(e.get("as_of")) in (int, float):
        result["as_of_kst"] = datetime.fromtimestamp(e["as_of"], ZoneInfo("Asia/Seoul")).isoformat(
            timespec="seconds"
        )
    if e.get("kind") in {"surveillance", "surveillance_failure"}:
        result["scope"] = "historical observation at as_of; current health is data_health"
    if "warning" in e:
        result["warning"] = warnings([e["warning"]])[0]
    if e.get("kind") == "market":
        result["available"] = [
            "indicators",
            "quote",
            "daily_bars",
            "orderbook",
            "trades",
            "warnings",
        ]
    if excerpt:
        for field in ("page_excerpt", "excerpt"):
            if e.get(field):
                result[field] = short(e[field], 800)
                result["excerpt_limited"] = len(e[field]) > 800
                break
    return result


def compact_evidence(evidence):
    """Factor repeated metadata without dropping any mandatory record or projected fact."""
    groups = {}
    for item in evidence:
        view = evidence_view(item)
        shared = {k: view[k] for k in ("kind", "source", "warning") if k in view}
        key = json.dumps(shared, sort_keys=True, ensure_ascii=False)
        group = groups.setdefault(key, {"shared": shared, "items": []})
        group["items"].append({k: v for k, v in view.items() if k not in shared})
    packed = []
    for group in groups.values():
        columns = sorted({k for item in group["items"] for k in item})
        packed.append(
            {
                "shared": group["shared"],
                "columns": columns,
                "rows": [[item.get(k) for k in columns] for item in group["items"]],
            }
        )
    return {
        "count": len(evidence),
        "groups": packed,
        "scope": "All mandatory records retained. Shared facts apply to every row; "
        "row values follow columns, null means unspecified.",
    }


def fit_merge_input(payload, model, strategy=None, prompts=None):
    """Prefer full compact findings; keep mandatory facts and numbers when space is tight."""
    from veyquant.input_budget import enforce_input
    from veyquant.shadow_inference import prepare_model_input

    def measured(value):
        return prepare_model_input("middle", value, model, strategy, prompts=prompts)[
            "input_context"
        ]

    measurement = measured(payload)
    if measurement["total_bytes"] <= measurement["budget_bytes"]:
        return payload
    reduced = copy.deepcopy(payload)
    for batch in reduced.get("batches", []):
        columns = batch["candidate_columns"]
        if "reason" in columns:
            index = columns.index("reason")
            batch["candidate_columns"] = [k for k in columns if k != "reason"]
            batch["candidates"] = [row[:index] + row[index + 1 :] for row in batch["candidates"]]
    reduced["compaction"] = (
        "Candidate reason fragments omitted to fit input. Batch findings, candidate symbols, "
        "numeric indicators, citations and every mandatory record remain. Full reasons archived."
    )
    measurement = measured(reduced)
    try:
        enforce_input(measurement)
    except ValueError as error:
        # Preserve the rejected projection for auditing; never send it to a provider.
        error.usage = {
            "status": "blocked_input",
            "provider_called": False,
            "input_context": measurement,
        }
        error.model_payload = reduced
        raise
    return reduced


def current_evidence(evidence, now, health=None):
    """Keep the archive intact; prior-session incidents are not current trading evidence."""
    day = datetime.fromtimestamp(now, ZoneInfo("Asia/Seoul")).date()
    latest_success = max(
        (e.get("as_of", 0) for e in evidence if e.get("kind") == "surveillance"), default=0
    )
    result = []
    for e in evidence:
        if e.get("kind") in {"surveillance", "surveillance_failure"}:
            at = e.get("as_of")
            if (
                not isinstance(at, (int, float))
                or datetime.fromtimestamp(at, ZoneInfo("Asia/Seoul")).date() != day
            ):
                continue
            if e["kind"] == "surveillance_failure" and latest_success > at:
                continue
            conditions = {v.get("condition") for v in e.get("signals", [])}
            if (
                conditions
                and conditions <= {"heartbeat", "realtime_gap"}
                and "realtime_gap" in conditions
                and (health or {}).get("realtime_state") == "healthy"
            ):
                continue
        result.append(e)
    return result


def health_view(current):
    return scalars(
        current.get("data_health"),
        (
            "as_of",
            "as_of_kst",
            "realtime_state",
            "realtime_age_seconds",
            "connected",
            "collection_started_at",
            "collection_completed_at",
        ),
    ) or {"realtime_state": "unknown"}


def managed_symbols(current):
    return {
        h["symbol"]
        for h in current["holdings"]
        if type(h.get("managed_quantity")) is int and h["managed_quantity"] > 0
    }


def relevant_evidence(evidence, symbols, mandatory_ids=()):
    """Mandatory facts survive; routine NORMAL history and duplicate alerts do not grow inputs."""
    mandatory_ids = set(mandatory_ids)
    mandatory, optional, seen = [], [], set()
    for e in evidence:
        scope = evidence_symbols(e)
        if scope and not scope.intersection(symbols):
            continue
        if e.get("kind") == "market":
            continue  # Already supplied as computed stock features.
        if e["id"] in mandatory_ids or e.get("kind") in {"dart_important", "new_warning"}:
            mandatory.append(evidence_view(e))
            continue
        if e.get("kind") == "surveillance" and e.get("severity") == "NORMAL":
            continue
        conditions = tuple(sorted({s.get("condition", "") for s in e.get("signals", [])}))
        key = (e.get("kind"), tuple(sorted(scope)), conditions, e.get("severity"))
        if e.get("kind") == "surveillance":
            if key in seen:
                continue
            seen.add(key)
        optional.append(evidence_view(e))
    return list({e["id"]: e for e in mandatory + optional[:8]}.values())


def compact_brief(value, merge=False, details=None, critical_review=False):
    return {
        "severity": value["severity"],
        "summary": short(value["summary"], (300 if critical_review else 48) if merge else 600),
        "candidates": [
            (
                [c["symbol"], short(c["reason"], 24)]
                + [
                    {k: indicators((details or {}).get(c["symbol"], {})).get(k)}
                    for k in ("return_5d_pct", "volume_vs_20d", "spread_bp")
                ]
                if merge
                else {"symbol": c["symbol"], "reason": short(c["reason"], 180)}
            )
            for c in value["candidates"]
        ],
        **(
            {
                "candidate_columns": [
                    "symbol",
                    "reason",
                    "return_5d_pct",
                    "volume_vs_20d",
                    "spread_bp",
                ]
            }
            if merge
            else {}
        ),
        "evidence_ids": list(dict.fromkeys(value["evidence_ids"])),
        "uncertainties": [
            short(u, 16 if merge else 160) for u in value["uncertainties"][: 1 if merge else 6]
        ],
        "additional_uncertainties": max(0, len(value["uncertainties"]) - (1 if merge else 6)),
    }


def account_view(current):
    result = scalars(
        current,
        (
            "as_of",
            "currency",
            "cash",
            "capital_used",
            "live_requested",
            "conditional_orders",
            "unresolved_submission",
            "order_generation",
            "settings_revision",
        ),
    )
    result["holdings"] = [
        scalars(h, ("symbol", "quantity", "managed_quantity")) for h in current["holdings"]
    ]
    result["open_orders"] = [
        scalars(o, ("symbol", "side", "quantity", "status")) for o in current["open_orders"]
    ]
    result["risk_limits"] = scalars(
        current.get("risk_limits"), ("capital_krw", "max_order_krw", "max_daily_loss_krw")
    )
    result["risk_status"] = scalars(
        current.get("risk_status"),
        (
            "state",
            "as_of",
            "daily_pnl_krw",
            "reason",
            "stale_symbols",
            "price_basis",
            "max_price_age_seconds",
            "execution_check",
        ),
    ) or {"state": "unavailable"}
    result["risk_status"]["price_observations"] = [
        scalars(q, ("symbol", "as_of", "age_seconds", "price", "basis"))
        for q in (current.get("risk_status") or {}).get("price_observations", [])
    ]
    result["data_health"] = health_view(current)
    result["quotes"] = [quote(q) for q in current.get("quotes", [])]
    constraints = current.get("order_constraints") or {}
    result["order_constraints"] = scalars(
        constraints,
        (
            "type",
            "time_in_force",
            "currency",
            "sell_scope",
            "commission_rate",
            "sell_cost_reserve_rate",
        ),
    ) | {
        "session": scalars(
            constraints.get("session"), ("scope", "starts_at", "ends_at", "is_open")
        ),
        "stocks": {
            s: {
                "price_limits": scalars(
                    c.get("price_limits"), ("lowerLimitPrice", "upperLimitPrice")
                ),
                "warnings": warnings(c.get("warnings")),
                "sellable_quantity": scalars(c, ("sellable_quantity",)).get("sellable_quantity"),
            }
            for s, c in constraints.get("stocks", {}).items()
        },
    }
    return result


def market_coverage(frozen):
    table = frozen["comparison_table"]
    columns = table["columns"]
    counts = {}
    for key in ("market", "eligibility"):
        if key in columns:
            counts[key] = dict(Counter(r[columns.index(key)] for r in table["rows"]))
    return {
        "total_stocks": len(table["rows"]),
        "reviewed_stocks": len(frozen["details"]),
        "counts": counts,
        "scope": (
            "Only reviewed stocks can be candidates; other stocks are not fully assessed. "
            "Frozen market/comparison tool supports reviewed symbols."
        ),
    }


def memory_view(records, symbols, feedback=()):
    outcomes = {r["event_id"]: r for r in feedback}
    result = []
    for record in records:
        decision = record.get("decision") or {}
        intents = [
            (n, i) for n, i in enumerate(decision.get("intents", [])) if i.get("symbol") in symbols
        ]
        if not intents and result:
            continue
        result.append(
            {
                "id": record.get("id"),
                "at": record.get("at"),
                "action": decision.get("action"),
                "summary": short(decision.get("summary"), 300),
                "relevant_intents": [
                    scalars(i, ("symbol", "side", "quantity", "limit_price"))
                    | {
                        "rationale": short(i.get("rationale"), 180),
                        "execution": scalars(
                            outcomes.get(
                                hashlib.sha256(f"{record.get('id')}:{n}".encode()).hexdigest(), {}
                            ),
                            ("reason", "state", "filled_quantity", "at"),
                        )
                        or {"state": "unconfirmed"},
                    }
                    for n, i in intents
                ],
            }
        )
        if len(result) == 3:
            break
    return result


def market_read(frozen, symbol, fields):
    detail = frozen["details"][symbol]
    out = {
        "symbol": symbol,
        "scope": "frozen research data; latest executable quote is in account.quotes",
    }
    for field in fields:
        if field in detail.get("unavailable", {}):
            out[field] = {
                "state": "unavailable",
                "reason": detail["unavailable"][field],
                "as_of": detail.get("as_of"),
            }
        elif field == "quote":
            out[field] = quote(detail.get(field))
        elif field == "indicators":
            out[field] = indicators(detail)
        elif field == "warnings":
            out[field] = warnings(detail.get(field))
        elif field == "comparison":
            table = frozen["comparison_table"]
            index = table["columns"].index("symbol")
            rows = [
                dict(zip(table["columns"], r, strict=True))
                for r in table["rows"]
                if r[index] == symbol
            ]
            out[field] = [
                scalars(r, ("symbol", "name", "market", "eligibility", "price", "as_of"))
                for r in rows
            ]
        elif field == "daily_bars":
            rows = detail.get(field) or []
            out[field] = {
                "total": len(rows),
                "returned": min(len(rows), 10),
                "scope": "latest 10 completed days; 20d indicators available separately",
                "rows": [
                    scalars(r, ("date", "open", "high", "low", "close", "volume"))
                    for r in rows[-10:]
                ],
            }
        elif field == "orderbook":
            book = detail.get(field) or {}
            out[field] = (
                scalars(book, ("timestamp", "currency"))
                | {
                    side: [scalars(r, ("price", "volume")) for r in book.get(side, [])[:3]]
                    for side in ("asks", "bids")
                }
                | {"scope": "top 3 levels per side"}
            )
        elif field == "trades":
            rows = sorted(
                detail.get(field) or [], key=lambda r: str(r.get("timestamp", "")), reverse=True
            )
            out[field] = {
                "total": len(rows),
                "scope": "latest 5 trades",
                "rows": [
                    scalars(r, ("price", "volume", "timestamp", "side", "tradeType"))
                    for r in rows[:5]
                ],
            }
        else:
            raise ValueError("invalid_market_read")
    return out


def working_results(results, max_bytes=12000):
    """Keep at most two compact pages; full retrieval history remains server-side."""
    pages = []
    for item in reversed(results[-2:]):
        if (
            len(json.dumps([item] + pages, ensure_ascii=False, separators=(",", ":")).encode())
            <= max_bytes
        ):
            pages.insert(0, item)
    return pages
