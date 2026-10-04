import sqlite3

from veyquant import model_context as views
from veyquant.decision_pipeline import BRIEF_SCHEMA
from veyquant.decision_store import PREVIEW_FIELDS, DecisionStore
from veyquant.input_budget import compact_json, enforce_input
from veyquant.shadow_inference import MODEL_PRESETS, prepare_model_input


def warning_events(count=56):
    return [
        {
            "id": f"E{i:04d}",
            "kind": "new_warning",
            "symbol": f"{i:06d}",
            "source": "Toss stock warnings",
            "as_of": 1789108800 + i,
            "warning": {
                "warningType": "INVESTMENT_WARNING",
                "startDate": "2026-09-11",
                "endDate": "2026-09-18",
            },
        }
        for i in range(count)
    ]


def test_grouped_mandatory_evidence_preserves_every_record_and_fact():
    events = warning_events() + [
        {
            "id": "filing",
            "kind": "dart_important",
            "symbol": "000001",
            "title": "주요 계약 해지",
            "url": "https://example.org/filing",
            "published_at": "2026-09-11",
            "as_of": 1789108800,
        }
    ]
    packed = views.compact_evidence(events)
    restored = {}
    for group in packed["groups"]:
        for row in group["rows"]:
            item = group["shared"] | dict(zip(group["columns"], row, strict=True))
            restored[item["id"]] = item
    assert len(restored) == packed["count"] == len(events)
    for e in events:
        assert all(restored[e["id"]][k] == v for k, v in views.evidence_view(e).items())
    assert len(compact_json(packed).encode()) < 0.65 * len(
        compact_json([views.evidence_view(e) for e in events]).encode()
    )


def test_full_batch_candidate_output_plus_56_required_warnings_fits_merge_budget(monkeypatch):
    batches = [
        views.compact_brief(
            {
                "severity": "WARN",
                "summary": "가" * 3000,
                "candidates": [
                    {"symbol": f"{20 * b + n:06d}", "reason": "나" * 1000} for n in range(12)
                ],
                "evidence_ids": [f"E{b * 100 + n:04d}" for n in range(100)],
                "uncertainties": ["다" * 1000] * 100,
            },
            merge=True,
        )
        for b in range(10)
    ]
    payload = {
        "protocol": "decision-v2",
        "task": "merge_decision_brief",
        "batches": batches,
        "allowed_candidate_symbols": [f"{n:06d}" for n in range(200)],
        "mandatory_evidence": views.compact_evidence(warning_events()),
        "schema": BRIEF_SCHEMA,
        "max_candidates": 12,
    }
    from veyquant.input_budget import INPUT_BUDGETS

    assert views.fit_merge_input(payload, MODEL_PRESETS["chatgpt"]["middle"]) is payload
    monkeypatch.setitem(INPUT_BUDGETS, "middle", 35000)
    original = payload
    payload = views.fit_merge_input(payload, MODEL_PRESETS["chatgpt"]["middle"])
    assert "reason" in original["batches"][0]["candidate_columns"]
    assert "compaction" in payload
    measured = prepare_model_input("middle", payload, MODEL_PRESETS["chatgpt"]["middle"])[
        "input_context"
    ]
    enforce_input(measured)
    assert payload["mandatory_evidence"]["count"] == 56
    assert sum(len(b["candidates"]) for b in batches) == 120
    assert sum(len(b["evidence_ids"]) for b in batches) == 1000


def test_preview_migration_and_updates_keep_archive_out_of_polling_reads(tmp_path):
    path = tmp_path / "decisions.sqlite3"
    store = DecisionStore(path)
    identity = store.admit("old", "scheduled", 1, {})
    data = {"decision": {"action": "NO_ACTION"}, "raw_archive": "private" * 100000}
    store.finish(identity, "complete", data, 2)
    # Simulate a pre-upgrade database, including a multi-megabyte private archive.
    store.db.executescript(
        "DROP TRIGGER decision_preview_insert; DROP TRIGGER decision_preview_update;"
        "DROP TRIGGER decision_preview_delete; DROP TABLE decision_previews;"
    )
    store.db.close()
    store = DecisionStore(path)
    assert store.history()[0]["data"] == data
    assert store.history(compact=True)[0]["data"] == {k: data.get(k) for k in PREVIEW_FIELDS}
    assert store.db.execute("select length(data) from decision_previews").fetchone()[0] < 1000
    store.finish(identity, "complete", data | {"error": "updated"}, 3)
    assert store.history(compact=True)[0]["data"]["error"] == "updated"

    def deny_archive(action, table, column, *_):
        if action == sqlite3.SQLITE_READ and table == "decision_runs" and column == "data":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    store.db.set_authorizer(deny_archive)
    assert store.memory()[0]["decision"] == data["decision"]
    assert store.history(compact=True)[0]["id"] == identity
    plan = list(
        store.db.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM decision_runs ORDER BY started DESC LIMIT 30"
        )
    )
    assert not any("TEMP B-TREE" in r[3] for r in plan)
    store.db.set_authorizer(None)
    store.db.execute("delete from decision_runs where id=?", (identity,))
    assert store.db.execute("select count(*) from decision_previews").fetchone()[0] == 0
    store.db.close()
