import json

from test_decision_pipeline import context, make_brief, run_pipeline

from veyquant.analysis_records import layer_detail
from veyquant.decision_store import DecisionStore


async def test_middle_completion_survives_research_failure_and_restart(tmp_path):
    path = tmp_path / "records.db"
    store = DecisionStore(path)

    async def model(role, payload):
        if role == "research":
            raise ValueError("fixture_failure")
        return make_brief()

    result, _, _, _ = await run_pipeline(store, model_override=model)
    assert result is None
    records = {r["role"]: r for r in store.layer_history()}
    assert records["middle"]["state"] == "complete"
    assert records["middle"]["error"] is None
    assert "decision" not in records["middle"]
    assert records["research"]["state"] == "aborted"
    assert records["research"]["error"] == "fixture_failure"
    assert "brief" not in records["research"]
    assert records["research"]["upstream_ids"] == [records["middle"]["id"]]
    store.db.close()
    store = DecisionStore(path)
    assert {r["role"]: r for r in store.layer_history()} == records
    store.db.close()


async def test_deferred_review_migrates_without_inventing_decision(tmp_path):
    path = tmp_path / "records.db"
    store = DecisionStore(path)
    result, _, _, _ = await run_pipeline(store, "critical", "WARN")
    assert [r["role"] for r in store.layer_history()] == ["middle"]
    identity = store.history()[0]["id"]
    result["decision"] = {"action": "NO_ACTION", "summary": "synthetic", "intents": []}
    store.db.execute("UPDATE decision_runs SET data=? WHERE id=?", (json.dumps(result), identity))
    store.db.execute("DROP TABLE analysis_records")
    store.db.close()
    store = DecisionStore(path)
    assert [r["role"] for r in store.layer_history()] == ["middle"]
    store.db.close()


def test_surveillance_has_own_identity_and_failure_record(tmp_path):
    store = DecisionStore(tmp_path / "records.db")
    store.evidence("surveillance", {"severity": "WARN", "summary": "watch"}, 100, "watch")
    store.evidence("surveillance_failure", {"summary": "failed", "error": "timeout"}, 101, "failed")
    records = store.layer_history()
    assert [r["state"] for r in records] == ["aborted", "complete"]
    assert all(r["role"] == "cheap" and r["run_id"] is None for r in records)
    assert "signals" not in records[0]
    store.db.close()


def test_layer_detail_omits_other_role_outputs_prompts_and_raw_archive():
    data = {
        "initial_context": context(),
        "decision": {"summary": "final"},
        "brief": make_brief(),
        "configuration": {
            "models": {"middle": "m", "research": "r"},
            "prompts": {"middle": "middle instruction", "research": "research instruction"},
        },
        "trace": [{"role": r, "decision": {"summary": r}} for r in ["middle", "research"]],
        "model_inputs": [{"role": r, "payload": {"role_input": r}} for r in ["middle", "research"]],
    }
    middle = layer_detail(data, "middle")
    research = layer_detail(data, "research")
    assert "decision" not in middle and "brief" not in research
    assert "initial_context" not in middle and "initial_context" not in research
    assert set(middle["configuration"]["prompts"]) == {"middle"}
    assert [t["role"] for t in middle["trace"]] == ["middle"]
    assert [t["role"] for t in research["model_inputs"]] == ["research"]
    middle["brief"]["summary"] = "changed"
    assert data["brief"]["summary"] != "changed"


def test_status_exports_independent_records_and_only_workflow_metadata(tmp_path):
    from veyquant.shadow_runner import read_view

    path = tmp_path / "report.json"
    data = {
        "protocol": "decision-v2",
        "updated_at": 1000,
        "state": "observing",
        "active": False,
        "runs": [
            {
                "id": "run",
                "kind": "scheduled",
                "state": "complete",
                "brief": {"summary": "combined"},
                "decision": {"summary": "combined"},
            }
        ],
        "layers": [{"id": "run:middle", "role": "middle", "brief": {"summary": "review"}}],
        "sources": {},
        "schedule": [],
        "orders_blocked": False,
        "pending_critical": 0,
        "surveillance": [],
        "usage": {"groups": []},
    }
    path.write_text(json.dumps(data))
    view = read_view(path, 1000)
    assert "brief" not in view["runs"][0] and "decision" not in view["runs"][0]
    assert view["layers"] == data["layers"]
    assert view["usage"] == data["usage"]


def test_legacy_cheap_only_run_is_visible_without_inventing_later_layers(tmp_path):
    store = DecisionStore(tmp_path / "db")
    run = {
        "legacy_report": {"symbol": "005930", "summary": "초기 감시 원본"},
        "trace": [{"role": "cheap", "status": "received", "input_tokens": 10}],
    }
    store.db.execute(
        "INSERT INTO decision_runs VALUES(?,?,?,?,?,?,?)",
        ("a" * 32, "old", "legacy", 100, 101, "historical", json.dumps(run)),
    )
    from veyquant.analysis_records import initialize

    initialize(store.db)
    rows = store.layer_page()["records"]
    assert len(rows) == 1 and rows[0]["role"] == "cheap"
    assert rows[0]["summary"] == "초기 감시 원본"
    assert "decision" not in rows[0] and "brief" not in rows[0]
    initialize(store.db)
    assert len(store.layer_history()) == 1
    store.db.close()
