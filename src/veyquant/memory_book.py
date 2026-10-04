"""Bounded decision memory and incremental briefs; no broker or model capabilities."""

import hashlib
import json

from veyquant.input_budget import compact_json
from veyquant.model_context import scalars, short

MAX_CHARS = 2000
PAGE_BYTES = 5000


def validate_content(value):
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_CHARS:
        raise ValueError("invalid_memory_book")
    return value


def proposals(identity, decision):
    return [
        scalars(i, ("symbol", "side", "quantity"))
        | {"event_id": hashlib.sha256(f"{identity}:{n}".encode()).hexdigest()}
        for n, i in enumerate(decision.get("intents", []))
    ]


def load_book(db):
    row = db.execute("SELECT data FROM decision_memory ORDER BY revision DESC LIMIT 1").fetchone()
    if row:
        return json.loads(row[0])
    # Bootstrap only once from small stored projections, never from raw archives.
    rows = db.execute(
        "SELECT r.rowid AS cursor,r.id,r.finished,json_extract(p.data,'$.decision') AS decision,"
        "json_extract(p.data,'$.brief.summary') AS brief FROM decision_runs r "
        "JOIN decision_previews p ON p.id=r.id WHERE r.state='complete' "
        "AND r.kind IN ('scheduled','manual','critical') AND EXISTS "
        "(SELECT 1 FROM json_each(p.data,'$.trace') t "
        "WHERE json_extract(t.value,'$.role')='research' "
        "AND json_extract(t.value,'$.task')='investment_decision' "
        "AND json_extract(t.value,'$.status')='received') "
        "ORDER BY r.rowid DESC LIMIT 3"
    ).fetchall()
    seed = []
    for r in reversed(rows):
        decision = json.loads(r["decision"] or "{}")
        seed.append(
            f"과거 판단 {r['id']}: {short(decision.get('summary'), 380) or ''}\n"
            f"정리: {short(r['brief'], 180) or ''}"
        )
    latest = rows[0] if rows else None
    return {
        "revision": 0,
        "through": latest["cursor"] if latest else 0,
        "source_run_id": latest["id"] if latest else None,
        "updated_at": latest["finished"] if latest else None,
        "origin": "legacy_seed" if latest else "empty",
        "content": "\n\n".join(seed)[:MAX_CHARS] if seed else "아직 기록된 메모리가 없습니다.",
        "last_proposals": proposals(latest["id"], json.loads(latest["decision"])) if latest else [],
    }


def brief_page(db, after, before):
    items, cursor, has_more = [], after, False
    for row in db.execute(
        "SELECT r.rowid AS cursor,r.id,r.finished,r.state,"
        "json_extract(p.data,'$.brief') AS brief FROM decision_runs r "
        "JOIN decision_previews p ON p.id=r.id WHERE r.rowid>? AND r.rowid<? "
        "AND r.finished IS NOT NULL AND r.kind IN ('scheduled','manual','critical') "
        "AND json_type(p.data,'$.brief')='object' ORDER BY r.rowid",
        (after, before),
    ):
        brief = json.loads(row["brief"])
        item = {
            "id": row["id"],
            "at": row["finished"],
            "run_state": row["state"],
            "severity": brief["severity"],
            "summary": short(brief["summary"], 300),
            "candidates": [c["symbol"] for c in brief["candidates"]],
            "uncertainties": [short(v, 100) for v in brief["uncertainties"][:3]],
        }
        if len(compact_json(items + [item]).encode()) > PAGE_BYTES:
            has_more = True
            break
        items.append(item)
        cursor = row["cursor"]
    # before identifies the current decision, whose brief is already a separate input.
    return {
        "items": items,
        "next": cursor if has_more else before,
        "has_more": has_more,
        "scope": "Only DecisionBrief summaries after memory coverage; not original analyses.",
    }


def with_execution(book, feedback):
    result = {k: v for k, v in book.items() if k != "last_proposals"}
    outcomes = {v["event_id"]: v for v in feedback}
    result["last_execution"] = [
        {k: v for k, v in p.items() if k != "event_id"}
        | {
            "execution": scalars(
                outcomes.get(p["event_id"]), ("reason", "state", "filled_quantity", "at")
            )
            or {"state": "unconfirmed"}
        }
        for p in book.get("last_proposals", [])
    ]
    return result


def commit_book(db, identity, update, decision, now):
    """Caller owns the same transaction that commits the validated decision."""
    current = load_book(db)
    if current["revision"] != update["base_revision"]:
        raise ValueError("memory_book_conflict")
    run_cursor = db.execute("SELECT rowid FROM decision_runs WHERE id=?", (identity,)).fetchone()[0]
    if (
        type(update["through"]) is not int
        or not current["through"] <= update["through"] <= run_cursor
    ):
        raise ValueError("invalid_memory_cursor")
    content = validate_content(update["content"])
    if content != decision.get("memory_book"):
        raise ValueError("memory_book_output_mismatch")
    book = {
        "revision": current["revision"] + 1,
        "through": update["through"],
        "source_run_id": identity,
        "updated_at": now,
        "content": content,
        "origin": "decision_model",
        "last_proposals": proposals(identity, decision),
    }
    db.execute(
        "INSERT INTO decision_memory VALUES(?,?,?,?)",
        (book["revision"], identity, now, compact_json(book)),
    )
    return book
