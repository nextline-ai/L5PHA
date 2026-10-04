from dataclasses import replace

import pytest

from veyquant.cli import DemoModel, demo
from veyquant.domain import Event, Evidence
from veyquant.pipeline import Decision, Pipeline, ResearchCatalog
from veyquant.risk import PaperRiskEngine


class ScriptedModel:
    version = "test-v1"

    def __init__(self, decisions):
        self.decisions = iter(decisions)

    def decide(self, role, event, evidence):
        return next(self.decisions)


def test_demo_complete_audit_and_duplicate(store):
    demo(store)
    stages = [r["stage"] for r in store.rows()]
    assert stages == [
        "observation",
        "rules",
        "cheap",
        "middle",
        "research",
        "evidence",
        "research",
        "proposal",
        "risk",
    ]
    assert store.rows()[-1]["payload"]["reason"] == "accepted_paper"
    demo(store)
    assert store.rows()[-1]["stage"] == "duplicate"


def test_budget_holds_without_risk(store, policy, account, quote):
    p = Pipeline(
        store, (DemoModel(),) * 3, ResearchCatalog([]), PaperRiskEngine(store, policy), max_calls=2
    )
    assert p.run(Event("e1", "DEMO", "KRW", 1000, True), account, quote, 1000) is None
    assert store.rows()[-1]["payload"]["reason"] == "research_budget_exhausted"


@pytest.mark.parametrize("status", ["missing", "failed", "conflicting"])
def test_unavailable_evidence_is_distinct(store, policy, account, quote, status):
    source = Evidence(
        "fixture-source", "fixture://source", 1000, 1000, "DEMO", "KRW", "per_share", "data", status
    )
    p = Pipeline(
        store, (DemoModel(),) * 3, ResearchCatalog([source]), PaperRiskEngine(store, policy)
    )
    assert p.run(Event("e1", "DEMO", "KRW", 1000, True), account, quote, 1000) is None
    assert any(r["payload"].get("status") == status for r in store.rows())


def test_unverified_citation_rejected(store, policy, proposal, account, quote):
    model = ScriptedModel([Decision("propose", "untrusted", proposal=proposal)])
    p = Pipeline(
        store,
        (DemoModel(), DemoModel(), model),
        ResearchCatalog([]),
        PaperRiskEngine(store, policy),
    )
    with pytest.raises(ValueError, match="unverified_citation"):
        p.run(Event("e1", "DEMO", "KRW", 1000, True), account, quote, 1000)
    assert store.rows()[-1]["stage"] == "hold"


def test_research_tool_cannot_accept_external_url():
    catalog = ResearchCatalog([])
    for target in ["http://169.254.169.254/latest/meta-data", "file:///etc/passwd", "submit_order"]:
        with pytest.raises(ValueError, match="unapproved_evidence_id"):
            catalog.read(target)


def test_invalid_stage_and_nonmaterial_filter(store, policy, account, quote):
    model = ScriptedModel([Decision("submit_order", "invalid")])
    p = Pipeline(store, (model, model, model), ResearchCatalog([]), PaperRiskEngine(store, policy))
    event = Event("e1", "DEMO", "KRW", 1000, False)
    assert p.run(event, account, quote, 1000) is None
    with pytest.raises(ValueError, match="invalid_stage_transition"):
        p.run(replace(event, id="e2", material=True), account, quote, 1000)
