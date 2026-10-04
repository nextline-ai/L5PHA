"""Independent layer records; run IDs only correlate the guarded workflow."""

import copy
import json


def project_run(run, data):
    traces = data.get("trace") or []
    request = data.get("request") or {}
    records = []
    legacy = data.get("legacy_report") or {}
    if run["kind"] == "legacy" and traces and all(t.get("role") == "cheap" for t in traces):
        # Early reports stopped after surveillance, before independent records existed.
        # Their sole model's saved summary is not a decision-layer output.
        return [
            {
                "id": run["id"] + ":cheap",
                "run_id": run["id"],
                "role": "cheap",
                "kind": "legacy",
                "state": "historical",
                "started": run["started"],
                "finished": run["finished"],
                "severity": "이전 형식",
                "summary": (traces[-1].get("decision") or {}).get("summary")
                or legacy.get("summary")
                or "이전 감시 기록입니다.",
                "symbols": [legacy["symbol"]] if legacy.get("symbol") else [],
                "trace": [{k: v for k, v in t.items() if k != "decision"} for t in traces],
            }
        ]
    for role in ("middle", "research"):
        own = [t for t in traces if t.get("role") == role]
        result = data.get("brief" if role == "middle" else "decision")
        # Old critical deferrals contained a server-generated NO_ACTION, never AI output.
        if role == "research" and run["kind"] == "critical" and not own:
            result = None
        if (
            not own
            and not result
            and not (
                role == "middle"
                and not traces
                or role == "research"
                and data.get("phase") == "decision"
            )
        ):
            continue
        state = run["state"]
        if role == "middle" and data.get("brief"):
            state = "complete"
        error = data.get("error") if state in {"aborted", "verification_failed"} else None
        primary = [t for t in own if t.get("task") in {"review_batch", "merge_decision_brief"}]
        finished = (
            max(
                (t.get("finished_at", t.get("at", run["started"])) for t in primary),
                default=run["finished"],
            )
            if role == "middle" and state == "complete"
            else run["finished"]
        )
        record = {
            "id": run["id"] + ":" + role,
            "run_id": run["id"],
            "role": role,
            "kind": run["kind"],
            "requested_at": run["started"],
            "state": state,
            "started": min((t.get("at", run["started"]) for t in own), default=run["started"]),
            "finished": finished,
            "error": error,
            "instruction": request.get("instruction"),
            "retry_of": request.get("retry_of"),
            "input_limit_override": request.get("input_limit_override") is True,
            "trace": [{k: v for k, v in t.items() if k != "decision"} for t in own],
            "upstream_ids": request.get("evidence_ids", [])
            if role == "middle"
            else [run["id"] + ":middle"],
        }
        if result:
            record["brief" if role == "middle" else "decision"] = result
        records.append(record)
    return records


def save_records(db, records):
    for record in records:
        db.execute(
            "INSERT OR REPLACE INTO analysis_records VALUES(?,?,?,?,?)",
            (
                record["id"],
                record.get("run_id"),
                record["role"],
                record["started"],
                json.dumps(record, ensure_ascii=False),
            ),
        )


def sync_run(db, identity, data=None):
    row = db.execute(
        "SELECT id,kind,state,started,finished FROM decision_runs WHERE id=?"
        if data is not None
        else "SELECT r.id,r.kind,r.state,r.started,r.finished,p.data FROM decision_runs r "
        "JOIN decision_previews p ON p.id=r.id WHERE r.id=?",
        (identity,),
    ).fetchone()
    if row:
        save_records(
            db, project_run(dict(row), data if data is not None else json.loads(row["data"]))
        )


def sync_surveillance(db, identity):
    row = db.execute(
        "SELECT at,kind,data FROM decision_evidence WHERE id=? "
        "AND kind IN ('surveillance','surveillance_failure')",
        (identity,),
    ).fetchone()
    if not row:
        return
    value = json.loads(row["data"])
    save_records(
        db,
        [
            {
                "id": identity + ":cheap",
                "run_id": None,
                "evidence_id": identity,
                "role": "cheap",
                "kind": "surveillance",
                "state": "aborted" if row["kind"] == "surveillance_failure" else "complete",
                "error": value.get("error"),
                "started": row["at"],
                "finished": row["at"],
                "severity": value.get("severity"),
                "summary": value.get("summary"),
                "symbols": value.get("symbols", []),
                "trace": value.get("trace", []),
            }
        ],
    )


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS analysis_records(
            id TEXT PRIMARY KEY, run_id TEXT, role TEXT NOT NULL, started REAL, data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS analysis_records_started ON analysis_records(started DESC);
        CREATE INDEX IF NOT EXISTS analysis_records_cursor
            ON analysis_records(started DESC,id DESC);
        CREATE INDEX IF NOT EXISTS analysis_records_run ON analysis_records(run_id);
    """)
    # Small projections only: never replay AI or duplicate the raw archives during migration.
    for row in db.execute(
        "SELECT id FROM decision_runs WHERE id NOT IN "
        "(SELECT run_id FROM analysis_records WHERE run_id IS NOT NULL)"
    ).fetchall():
        sync_run(db, row[0])
    for row in db.execute(
        "SELECT id FROM decision_evidence WHERE kind IN ('surveillance','surveillance_failure') "
        "AND id||':cheap' NOT IN (SELECT id FROM analysis_records)"
    ).fetchall():
        sync_surveillance(db, row[0])


def layer_detail(data, role):
    """Allowlisted detail response, with no other layer's results or full account archive."""
    if role not in {"middle", "research"}:
        raise ValueError("invalid_analysis_role")
    result = {
        "role": role,
        "trace": [t for t in data.get("trace", []) if t.get("role") == role],
        "model_inputs": [t for t in data.get("model_inputs", []) if t.get("role") == role],
    }
    keys = (
        ("brief",)
        if role == "middle"
        else (
            "decision",
            "memory_book_before",
            "memory_book_after",
            "intervening_briefs",
            "tool_results",
        )
    )
    for key in keys:
        if key in data:
            result[key] = data[key]
    configuration = data.get("configuration", {})
    result["configuration"] = {
        key: {role: value[role]}
        for key, value in configuration.items()
        if key in {"models", "reasoning", "prompts"} and role in value
    }
    return copy.deepcopy(result)
