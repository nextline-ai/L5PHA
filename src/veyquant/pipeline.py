from dataclasses import asdict, dataclass
from typing import Protocol

from veyquant.domain import AccountSnapshot, Event, Evidence, Proposal, Quote, RiskResult
from veyquant.risk import PaperRiskEngine
from veyquant.store import Store


@dataclass(frozen=True)
class Decision:
    action: str  # escalate, research, propose, hold
    summary: str
    evidence_id: str | None = None
    proposal: Proposal | None = None


class Model(Protocol):
    version: str

    def decide(self, role: str, event: Event, evidence: tuple[Evidence, ...]) -> Decision: ...


class ResearchCatalog:
    """Fixed local read-only catalog; no URL fetching, credentials or shell tools."""

    def __init__(self, evidence: list[Evidence]):
        self._items = {e.id: e for e in evidence}

    def read(self, evidence_id: str) -> Evidence:
        if evidence_id not in self._items:
            raise ValueError("unapproved_evidence_id")
        return self._items[evidence_id]


class Pipeline:
    def __init__(
        self,
        store: Store,
        models: tuple[Model, Model, Model],
        research: ResearchCatalog,
        risk: PaperRiskEngine,
        max_calls: int = 6,
    ):
        if max_calls < 1:
            raise ValueError("invalid research budget")
        self.store, self.models, self.research, self.risk = store, models, research, risk
        self.max_calls = max_calls

    def run(
        self, event: Event, account: AccountSnapshot, quote: Quote, now: int
    ) -> RiskResult | None:
        if not self.store.claim(event.id):
            return None
        try:
            result = self._run(event, account, quote, now)
        except Exception as error:
            # Error text from a provider may contain sensitive data; only record the class.
            self.store.record(
                event.id, "hold", {"reason": "pipeline_error", "error_type": type(error).__name__}
            )
            self.store.finish(event.id, "held")
            raise
        self.store.finish(event.id, "evaluated" if result else "held_or_filtered")
        return result

    def _run(self, event, account, quote, now):
        if not 0 <= now - event.observed_at <= 30:
            self.store.record(event.id, "hold", {"reason": "stale_event"})
            return None
        self.store.record(event.id, "rules", {"material": event.material})
        if not event.material:
            return None
        evidence: list[Evidence] = []
        calls = 0
        for role, model in zip(("cheap", "middle", "research"), self.models, strict=True):
            while True:
                if calls >= self.max_calls:
                    self.store.record(event.id, "hold", {"reason": "research_budget_exhausted"})
                    return None
                calls += 1
                decision = model.decide(role, event, tuple(evidence))
                if not isinstance(decision, Decision):
                    raise ValueError("invalid_model_output")
                self.store.record(
                    event.id,
                    role,
                    {
                        "model_version": model.version,
                        "action": decision.action,
                        "summary": decision.summary,
                        "call": calls,
                    },
                )
                if decision.action == "hold":
                    return None
                if decision.action == "escalate" and role != "research":
                    break
                if decision.action == "research" and role == "research":
                    item = self.research.read(decision.evidence_id)
                    self.store.record(event.id, "evidence", asdict(item))
                    if item.status != "available":
                        self.store.record(event.id, "hold", {"reason": "evidence_unavailable"})
                        return None
                    if item.symbol != event.symbol or item.currency != event.currency:
                        raise ValueError("evidence_scope_mismatch")
                    if item.as_of > item.collected_at or item.collected_at > now:
                        raise ValueError("future_evidence")
                    if any(e.id == item.id for e in evidence):
                        raise ValueError("duplicate_evidence_request")
                    evidence.append(item)
                    continue
                if decision.action == "propose" and role == "research":
                    p = decision.proposal
                    if (
                        not isinstance(p, Proposal)
                        or p.event_id != event.id
                        or (p.symbol != event.symbol or p.currency != event.currency)
                    ):
                        raise ValueError("invalid_proposal")
                    if not set(p.evidence_ids).issubset({e.id for e in evidence}):
                        raise ValueError("unverified_citation")
                    self.store.record(event.id, "proposal", asdict(p))
                    return self.risk.evaluate(p, account, quote, now)
                raise ValueError("invalid_stage_transition")
        return None
