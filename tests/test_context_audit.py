import copy
import importlib
import importlib.util
import json
import stat
from pathlib import Path

import pytest
from test_decision_pipeline import run_pipeline

from veyquant.decision_store import DecisionStore

spec = importlib.util.spec_from_file_location(
    "audit_model_context", Path(__file__).parents[1] / "scripts" / "audit_model_context.py"
)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


@pytest.fixture
def archive_store(tmp_path):
    store = DecisionStore(tmp_path / "db")
    yield store
    store.db.close()


async def test_offline_replay_private_artifacts_and_readonly_database(archive_store, tmp_path):
    result, calls, _, _ = await run_pipeline(archive_store)
    assert result
    row = dict(archive_store.db.execute("SELECT * FROM decision_runs").fetchone())
    before = archive_store.db.execute("SELECT data FROM decision_runs").fetchone()[0]
    record = audit.read_archive(database=tmp_path / "db", identity=row["id"])
    output = tmp_path / "private"
    summary = await audit.replay(record, output)
    assert summary["result"] == {"state": "complete", "error": None}
    assert summary["model_payloads"] == len(calls)
    assert summary["archived_provider_usage_not_new_billing"]["research"]["input_tokens"] == 1
    assert "검토 제안" not in json.dumps(summary, ensure_ascii=False)
    assert "기다립니다" not in json.dumps(summary, ensure_ascii=False)
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in output.iterdir())
    final = json.loads((output / "input-002.json").read_text())
    assert final["payload"]["account"]["cash"] == "10000"
    assert before == archive_store.db.execute("SELECT data FROM decision_runs").fetchone()[0]


def test_batch_matching_uses_recorded_symbol_groups_not_completion_order():
    data = {
        "initial_context": {"review_universe": [{"symbol": str(n)} for n in range(40)]},
        "trace": [
            {"role": "middle", "task": "review_batch", "decision": {"index": n}} for n in range(2)
        ],
    }
    replay = audit.TraceReplay(data)
    second = replay.take(
        "middle", "review_batch", {"stocks": [{"symbol": str(n)} for n in range(20, 40)]}
    )
    first = replay.take(
        "middle", "review_batch", {"stocks": [{"symbol": str(n)} for n in range(20)]}
    )
    assert second["decision"]["index"] == 1 and first["decision"]["index"] == 0


def test_output_symlink_rejected_and_market_tool_history_can_be_a_list(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe_output_directory"):
        audit.private_directory(link)
    store = audit.ReplayStore({"tool_results": [{"result": [{"id": "e"}]}]})
    assert store.recent() == []


async def test_historical_citation_adaptation_is_explicit_and_context_only(archive_store):
    result, _, _, _ = await run_pipeline(archive_store)
    result["evidence"].append(
        {"id": "omitted-history", "kind": "surveillance", "severity": "NORMAL", "summary": "기록"}
    )
    result["trace"][0]["decision"]["evidence_ids"].append("omitted-history")
    record = {"data": result}
    original = copy.deepcopy(record)
    strict = await audit.replay(record)
    assert strict["result"] == {"state": "aborted", "error": "unverified_citation"}
    assert strict["recorded_output_adaptation"]["modified_outputs"] == 0
    adapted = await audit.replay(record, adapt_recorded_citations=True)
    assert adapted["result"] == {"state": "complete", "error": None}
    assert adapted["validation_scope"] == "context_shape_only_not_investment_quality"
    assert adapted["recorded_output_adaptation"] == {
        "enabled": True,
        "policy": "remove_unavailable_review_batch_citations_only",
        "modified_outputs": 1,
        "removed_citation_references": 1,
    }
    assert record == original


async def test_audit_reports_each_call_over_budget_without_provider(archive_store, monkeypatch):
    result, _, _, _ = await run_pipeline(archive_store)
    original = audit.visible_input

    def very_small_budget(*args):
        value = original(*args)
        value["measurement"]["budget_bytes"] = 1
        return value

    monkeypatch.setattr(audit, "visible_input", very_small_budget)
    summary = await audit.replay({"data": result})
    assert all(call["budget_exceeded"] is True for call in summary["call_budgets"])
    assert all(group["calls_exceeding_budget"] == group["calls"] for group in summary["groups"])


async def test_auxiliary_audit_captures_real_serializers_without_network(
    archive_store, tmp_path, monkeypatch
):
    scripts = Path(__file__).parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    auxiliary = importlib.import_module("audit_auxiliary_context")
    result, _, _, _ = await run_pipeline(archive_store)
    result["configuration"]["models"] = {
        "cheap": "gpt-5.6-luna",
        "middle": "gpt-5.6-luna",
        "research": "gpt-5.6-sol",
    }
    result["initial_context"]["comparison_table"] = {
        "columns": ["symbol", "name"],
        "rows": [["000001", "공개 회사"]],
    }
    result["tool_results"] = [
        {"tool": "search", "arguments": {"symbols": ["000001"], "topics": ["earnings"]}}
    ]
    result["trace"].append({"role": "research", "task": "web_search", "input_tokens": 90000})
    archive_store.db.execute("UPDATE decision_runs SET data=?", (json.dumps(result),))
    archive_store.evidence(
        "surveillance",
        {
            "signals": [{"condition": "heartbeat", "secret_noise": "never_forward"}],
            "trace": [{"model": "gpt-5.6-luna", "input_tokens": 500}],
        },
        1,
    )
    monkeypatch.setattr(
        auxiliary.shadow_inference, "provider_request", lambda *a, **k: pytest.fail("network")
    )
    before = archive_store.db.execute("SELECT data FROM decision_runs").fetchone()[0]
    summary = auxiliary.audit(
        tmp_path / "db",
        scripts.parent / "src" / "veyquant" / "decision_runtime.py",
        output_dir=tmp_path / "aux-private",
    )
    assert summary["surveillance_found"] and summary["native_search_run_found"]
    assert len(summary["calls"]) == 2
    assert summary["calls"][0]["archived_provider_usage_not_new_billing"]["input_tokens"] == 500
    assert summary["calls"][1]["archived_provider_usage_not_new_billing"]["input_tokens"] == 90000
    assert "공개 회사" not in json.dumps(summary, ensure_ascii=False)
    artifacts = list((tmp_path / "aux-private").glob("*input*.json"))
    assert len(artifacts) == 2
    assert all("never_forward" not in path.read_text() for path in artifacts)
    assert before == archive_store.db.execute("SELECT data FROM decision_runs").fetchone()[0]
