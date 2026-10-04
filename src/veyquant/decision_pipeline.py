"""Deadline-bound brief -> refreshed context -> read-only research -> trade intents.

Dependencies are capabilities, not databases, credentials or broker transports. Only
the local orchestrator sees these capabilities; models receive fixed JSON schemas.
"""

import asyncio
import copy
import hashlib
import time
from decimal import Decimal

from veyquant import memory_book as memory
from veyquant import model_context as views
from veyquant.shadow_contract import domestic_symbol

SEVERITIES = {"NORMAL", "WARN", "CRITICAL"}
TOPICS = {"earnings", "disclosure", "valuation", "business", "macro", "litigation"}
REVIEW_BATCH_SIZE = 40


def text(value, maximum=3000):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ValueError("invalid_analysis_text")
    return value


def severity(value):
    if not isinstance(value, dict) or value.get("severity") not in SEVERITIES:
        raise ValueError("invalid_severity")
    text(value.get("summary"))
    return value["severity"]


def fingerprint(context):
    return (
        tuple(sorted((h["symbol"], h["quantity"]) for h in context["holdings"])),
        context["order_generation"],
        context["settings_revision"],
    )


def blocked(context):
    return bool(
        context["open_orders"]
        or context["unresolved_submission"]
        or context.get("conditional_orders")
    )


def brief(value, symbols, evidence_ids):
    severity(value)
    if set(value) != {"severity", "summary", "candidates", "evidence_ids", "uncertainties"}:
        raise ValueError("invalid_decision_brief")
    candidates = value["candidates"]
    if not isinstance(candidates, list) or len(candidates) > 12:
        raise ValueError("too_many_candidates")
    seen = set()
    for candidate in candidates:
        if (
            not isinstance(candidate, dict)
            or set(candidate) != {"symbol", "reason"}
            or candidate["symbol"] not in symbols
            or candidate["symbol"] in seen
        ):
            raise ValueError("invalid_candidate")
        seen.add(candidate["symbol"])
        text(candidate["reason"], 1000)
    for key in ("evidence_ids", "uncertainties"):
        if not isinstance(value[key], list) or len(value[key]) > 100:
            raise ValueError("invalid_brief_evidence")
        for item in value[key]:
            text(item, 1000)
    if not set(value["evidence_ids"]).issubset(evidence_ids):
        raise ValueError("unverified_citation")
    return value


BRIEF_SCHEMA = {
    "severity": "NORMAL | WARN | CRITICAL",
    "summary": "Korean factual summary",
    "candidates": [{"symbol": "supplied symbol", "reason": "why review"}],
    "evidence_ids": ["supplied evidence ID"],
    "uncertainties": ["missing or conflicting facts"],
}
DECISION_SCHEMA = {
    "action": "NO_ACTION | SUBMIT",
    "summary": "Korean conclusion in 1–2 sentences, 1–300 characters",
    "detailed_explanation": "Korean reviewable explanation, 1–6000 characters: evidence, "
    "alternatives, portfolio/risk fit, why trade or wait, and invalidation conditions",
    "counterargument": "strongest contrary evidence",
    "uncertainty": "unknowns",
    "memory_book": "Updated complete Korean memory book, 1–2000 characters; not an append",
    "intents": [
        {
            "symbol": "BUY candidate or SELL AI-managed holding symbol",
            "side": "BUY | SELL",
            "quantity": 1,
            "limit_price": "KRW decimal",
            "rationale": "reason and validity conditions",
            "evidence_ids": ["supplied evidence ID"],
        }
    ],
}
TOOL_SCHEMA = {
    "action": "READ",
    "tool": "evidence | market | news | search | briefs",
    "arguments": {
        "briefs": {
            "after": "next cursor from intervening_briefs",
            "memory_book": "Updated 1–2000 character memory of all pages read so far",
        },
        "evidence": {"ids": ["supplied evidence ID"]},
        "market": {
            "symbol": "supplied symbol",
            "fields": [
                "quote",
                "indicators",
                "warnings",
                "comparison",
                "daily_bars",
                "orderbook",
                "trades",
            ],
        },
        "news": {"symbols": ["supplied symbol"], "topics": sorted(TOPICS)},
        "search": {"symbols": ["supplied symbol"], "topics": sorted(TOPICS)},
    },
}


def validate_decision(value, candidate_symbols, evidence_ids, context):
    if not isinstance(value, dict) or set(value) != set(DECISION_SCHEMA):
        raise ValueError("invalid_final_decision")
    if value["action"] not in {"NO_ACTION", "SUBMIT"}:
        raise ValueError("invalid_final_decision")
    text(value["summary"], 300)
    for key in ("counterargument", "uncertainty"):
        text(value[key])
    text(value["detailed_explanation"], 6000)
    memory.validate_content(value["memory_book"])
    intents = value["intents"]
    if not isinstance(intents, list) or len(intents) > 12:
        raise ValueError("invalid_trade_intents")
    if bool(intents) != (value["action"] == "SUBMIT"):
        raise ValueError("invalid_trade_intents")
    seen = set()
    for intent in intents:
        if not isinstance(intent, dict) or set(intent) != set(DECISION_SCHEMA["intents"][0]):
            raise ValueError("invalid_trade_intent")
        symbol = intent["symbol"]
        if (
            not domestic_symbol(symbol)
            or symbol in seen
            or symbol not in candidate_symbols | views.managed_symbols(context)
        ):
            raise ValueError("invalid_intent_symbol")
        seen.add(symbol)
        if intent["side"] not in {"BUY", "SELL"}:
            raise ValueError("invalid_intent_side")
        if type(intent["quantity"]) is not int or not 0 < intent["quantity"] <= 10**9:
            raise ValueError("invalid_intent_quantity")
        if intent["side"] == "BUY" and symbol not in candidate_symbols:
            raise ValueError("invalid_intent_symbol")
        if intent["side"] == "SELL":
            owned = next(
                (
                    h.get("managed_quantity", 0)
                    for h in context["holdings"]
                    if h["symbol"] == symbol
                ),
                0,
            )
            sellable = views.number(
                context.get("order_constraints", {})
                .get("stocks", {})
                .get(symbol, {})
                .get("sellable_quantity")
            )
            if (
                type(owned) is not int
                or intent["quantity"] > owned
                or sellable is None
                or intent["quantity"] > sellable
            ):
                raise ValueError("invalid_sell_quantity")
        price = intent["limit_price"]
        if not isinstance(price, str) or len(price) > 20:
            raise ValueError("invalid_intent_price")
        number = Decimal(price)
        if not number.is_finite() or number <= 0 or number > 10**10:
            raise ValueError("invalid_intent_price")
        if intent["side"] == "BUY" and number * intent["quantity"] > Decimal(
            context["risk_limits"]["max_order_krw"]
        ):
            raise ValueError("intent_exceeds_order_limit")
        text(intent["rationale"])
        ids = intent["evidence_ids"]
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids):
            raise ValueError("invalid_intent_citations")
        if not set(ids).issubset(evidence_ids):
            raise ValueError("unverified_citation")
        if "market:" + symbol not in ids:
            raise ValueError("missing_trade_evidence")
    return value


class DecisionPipeline:
    def __init__(self, model, context, news, store, clock=time.time, monotonic=time.monotonic):
        self.model, self.context, self.news, self.store = model, context, news, store
        self.clock, self.monotonic = clock, monotonic
        self.trace = []
        self.summary_calls = 0
        self.tools = self.news_calls = self.middle_news_calls = 0
        self.identity = None
        self.data = None
        self.citation_aliases = {}
        self.search_cache = {}
        self.search_cache_hits = 0

    def bind_citations(self, ids):
        known = set(self.citation_aliases.values())
        for identity in sorted(ids):
            if identity not in known and not identity.startswith("market:"):
                alias = "E_" + hashlib.sha256(identity.encode()).hexdigest()[:16]
                if alias in self.citation_aliases and self.citation_aliases[alias] != identity:
                    raise ValueError("citation_collision")
                self.citation_aliases[alias] = identity
                known.add(identity)

    def citation_values(self, value, reverse=False):
        mapping = (
            self.citation_aliases
            if reverse
            else {identity: alias for alias, identity in self.citation_aliases.items()}
        )

        def translate(item):
            if isinstance(item, str):
                return mapping.get(item, item)
            if isinstance(item, list):
                return [translate(v) for v in item]
            if isinstance(item, dict):
                return {k: translate(v) for k, v in item.items()}
            return item

        return translate(value)

    async def call(self, role, task, payload, deadline, count_brief=True):
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise TimeoutError("analysis_deadline")
        if role == "middle" and count_brief:
            if self.summary_calls >= 16:
                raise ValueError("brief_call_budget")
            self.summary_calls += 1
        trace = {"role": role, "task": task, "status": "uncertain", "at": self.clock()}
        if self.data:
            trace["model"] = self.data["configuration"].get("models", {}).get(role)
        self.trace.append(trace)
        model_payload = self.citation_values(
            {
                "protocol": "decision-v2",
                "task": task,
                "citation_policy": (
                    "Copy supplied stable evidence IDs exactly (E_hash or market:symbol). "
                    "Legacy E0001-style IDs in historical memory are local to that old run, "
                    "not current evidence. Do not compare or resolve them against current IDs; "
                    "identify facts by symbol, date and source. Never invent IDs."
                ),
                **payload,
            }
        )
        if (
            task == "merge_decision_brief"
            and (self.data or {}).get("configuration", {}).get("input_limit_override") is not True
        ):
            from veyquant.shadow_inference import model_selection

            config = (self.data or {}).get("configuration", {})
            try:
                model_payload = views.fit_merge_input(
                    model_payload,
                    model_selection(config.get("models"))["middle"],
                    config.get("strategy"),
                    config.get("prompts"),
                )
            except ValueError as error:
                trace.update(getattr(error, "usage", {}))
                if self.data is not None and hasattr(error, "model_payload"):
                    self.data.setdefault("model_inputs", []).append(
                        {"role": role, "task": task, "payload": copy.deepcopy(error.model_payload)}
                    )
                if self.identity:
                    self.store.progress(self.identity, self.data)
                raise
        if self.data is not None:
            self.data.setdefault("model_inputs", []).append(
                {"role": role, "task": task, "payload": copy.deepcopy(model_payload)}
            )
        if self.identity:
            self.store.progress(self.identity, self.data)
        for attempt in range(2):
            try:
                result, usage = await asyncio.wait_for(
                    self.model(role, model_payload),
                    max(0, deadline - self.monotonic()),
                )
                break
            except Exception as error:
                trace.update(getattr(error, "usage", {}))
                recoverable = str(error) in {
                    "provider_connection_lost",
                    "provider_transport_failure",
                    "provider_timeout",
                    "openai_http_429",
                    "openai_http_429_rate_limit_exceeded",
                    "openai_http_500",
                    "openai_http_502",
                    "openai_http_503",
                    "openai_http_504",
                    "gemini_http_429",
                    "gemini_http_500",
                    "gemini_http_503",
                }
                if (
                    attempt
                    or not recoverable
                    or deadline - self.monotonic() <= 5
                    or (role == "middle" and count_brief and self.summary_calls >= 16)
                ):
                    raise
                # Resume only this uncompleted model turn. Never replay earlier
                # analysis/tools, refund unknown usage, or retry broker orders.
                trace.update(status="failed", error=str(error))
                if self.identity:
                    self.store.progress(self.identity, self.data)
                await asyncio.sleep(1)
                if role == "middle" and count_brief:
                    if self.summary_calls >= 16:
                        raise
                    self.summary_calls += 1
                trace = {
                    "role": role,
                    "task": task,
                    "status": "uncertain",
                    "at": self.clock(),
                    "attempt": 2,
                    "model": trace.get("model"),
                }
                self.trace.append(trace)
        result = self.citation_values(result, reverse=True)
        trace.update(usage)
        trace["decision"] = result
        trace["status"] = "received"
        trace["finished_at"] = self.clock()
        if self.identity:
            self.store.progress(self.identity, self.data)
        return result

    async def run(self, identity, kind, request, configuration):
        data = {
            "kind": kind,
            "review_batch_size": REVIEW_BATCH_SIZE,
            "request": request,
            "trace": self.trace,
            "settings_revision": configuration["settings_revision"],
            "configuration": {
                k: configuration[k]
                for k in ("models", "reasoning", "strategy", "prompts", "input_limit_override")
                if k in configuration
            },
            "citation_aliases": self.citation_aliases,
        }
        self.identity, self.data = identity, data
        catalogue = {}
        try:
            # This timeout includes initial broker context and every batch/merge call.
            start = self.monotonic()
            async with asyncio.timeout(600):
                data["phase"] = "context_collection"
                frozen = await self.context(
                    "initial", request | {"review_mode": "event" if kind == "critical" else "broad"}
                )
                data["phase"] = "brief"
                if blocked(frozen):
                    raise ValueError("pending_orders")
                if frozen["settings_revision"] != configuration["settings_revision"]:
                    raise ValueError("settings_changed")
                data["initial_context"] = frozen
                gate_inputs = {
                    "holding_context": {
                        "as_of": frozen.get("as_of"),
                        "positions": [
                            views.scalars(h, ("symbol", "quantity", "managed_quantity"))
                            for h in frozen["holdings"]
                        ],
                        "scope": "quantity is total account holdings; "
                        "managed_quantity is AI-owned. "
                        "Only AI-owned shares may be proposed for sale. This snapshot is for "
                        "review relevance; the decision layer refreshes account state later.",
                    }
                }
                if kind == "critical":
                    book = self.store.memory_book()
                    gate_inputs |= {
                        "critical_review_policy": views.CRITICAL_REVIEW_POLICY,
                        "decision_gate_context": {
                            "holding_symbols": sorted({h["symbol"] for h in frozen["holdings"]}),
                            "previous_memory": {
                                "as_of": book["updated_at"],
                                "content": book["content"],
                            },
                        },
                    }
                evidence = self.store.recent(self.clock())
                pending_warn = self.store.get("pending_warn", [])
                evidence.extend(self.store.lookup(pending_warn))
                evidence.extend(self.store.lookup(request.get("evidence_ids", [])))
                evidence.extend(self.store.mandatory(self.clock(), set(frozen["details"])))
                # Optional disconnect stops using that feed in new analyses. Past reports remain.
                if not frozen.get("optional_sources", {}).get("dart", True):
                    evidence = [
                        e for e in evidence if e.get("kind") not in {"dart", "dart_important"}
                    ]
                evidence = [e for e in evidence if e.get("source") != "Google Search"]
                evidence = views.current_evidence(evidence, self.clock(), views.health_view(frozen))
                # Keep global facts and every restriction relevant to this reviewed universe.
                evidence = [
                    e
                    for e in evidence
                    if e.get("kind") not in {"dart_important", "new_warning"}
                    or not views.evidence_symbols(e)
                    or views.evidence_symbols(e).intersection(frozen["details"])
                ]
                mandatory = {e["id"]: e for e in evidence if e.get("kind") == "dart_important"}
                mandatory.update({e["id"]: e for e in frozen.get("mandatory_evidence", [])})
                frozen["mandatory_evidence"] = list(mandatory.values())
                for symbol, item in frozen["details"].items():
                    evidence.append(
                        {
                            "id": "market:" + symbol,
                            "kind": "market",
                            "source": "Toss frozen public data",
                            **item,
                        }
                    )
                evidence.extend(frozen.get("mandatory_evidence", []))
                catalogue = {e["id"]: e for e in evidence}
                self.bind_citations(catalogue)
                data["evidence"] = list(catalogue.values())
                symbols = set(frozen["details"])
                rows = frozen["review_universe"]
                if not rows or len(rows) > 200:
                    raise ValueError("invalid_review_universe")
                semaphore = asyncio.Semaphore(3)

                async def review(batch):
                    async with semaphore:
                        batch_symbols = {r["symbol"] for r in batch}
                        scoped_evidence = views.relevant_evidence(
                            list(catalogue.values()), batch_symbols, mandatory
                        )
                        market_ids = {"market:" + s for s in batch_symbols}
                        available_ids = market_ids | {e["id"] for e in scoped_evidence}
                        result = await self.call(
                            "middle",
                            "review_batch",
                            {
                                "stocks": views.stock_table(
                                    [views.stock(r, frozen["details"][r["symbol"]]) for r in batch]
                                ),
                                "recent_evidence": scoped_evidence,
                                "available_evidence_ids": sorted(available_ids),
                                "request_kind": kind,
                                **gate_inputs,
                                "data_health": views.health_view(frozen),
                                "market_data_policy": views.MARKET_DATA_POLICY,
                                "severity_policy": views.SEVERITY_POLICY,
                                "review_scope": (
                                    "Review public market evidence and select research candidates. "
                                    "Holding symbols and quantities are in holding_context. "
                                    "Cash and risk limits are supplied "
                                    "separately to the decision layer. Their absence here "
                                    "is expected, not missing evidence or a reason "
                                    "to raise severity."
                                ),
                                "manual_instruction": request.get("instruction"),
                                "schema": BRIEF_SCHEMA,
                                "max_candidates": 12,
                                "allowed_candidate_symbols": sorted(batch_symbols),
                                "candidate_policy": (
                                    "Select candidates only from allowed_candidate_symbols. "
                                    "Global evidence may mention other stocks; those cannot be "
                                    "candidates in this batch. Copy symbols exactly."
                                ),
                            },
                            start + 600,
                        )
                        return brief(result, batch_symbols, available_ids)

                # Columnar features allow five batches + one merge within the same input budget.
                async with asyncio.TaskGroup() as group:
                    reviews = [
                        group.create_task(review(rows[n : n + REVIEW_BATCH_SIZE]))
                        for n in range(0, len(rows), REVIEW_BATCH_SIZE)
                    ]
                batches = [task.result() for task in reviews]
                if len(batches) == 1:
                    # The validated batch already is a DecisionBrief over the entire scope.
                    merged = batches[0]
                    data["merge_skipped"] = "single_batch_already_complete"
                else:
                    merged = await self.call(
                        "middle",
                        "merge_decision_brief",
                        {
                            "request_kind": kind,
                            **gate_inputs,
                            "batches": [
                                views.compact_brief(
                                    b,
                                    merge=True,
                                    details=frozen["details"],
                                    critical_review=kind == "critical",
                                )
                                for b in batches
                            ],
                            "data_health": views.health_view(frozen),
                            "market_data_policy": views.MARKET_DATA_POLICY,
                            "severity_policy": views.SEVERITY_POLICY,
                            "mandatory_evidence": views.compact_evidence(
                                frozen.get("mandatory_evidence", [])
                            ),
                            "schema": BRIEF_SCHEMA,
                            "max_candidates": 12,
                            "allowed_candidate_symbols": sorted(symbols),
                            "review_scope": (
                                "Merge public evidence into a DecisionBrief, not a trade decision. "
                                "The decision layer will receive the latest private account "
                                "and risk context. Do not repeat the expected absence of "
                                "that private context "
                                "as a research uncertainty."
                            ),
                        },
                        start + 600,
                    )
                data["brief"] = copy.deepcopy(brief(merged, symbols, set(catalogue)))
                if kind == "critical":
                    # Server metadata, not a model-controlled routing field.
                    data["brief"]["handoff"] = (
                        "decision" if data["brief"]["severity"] == "CRITICAL" else "deferred"
                    )
                self.store.progress(identity, data)
            if kind == "critical" and data["brief"]["severity"] != "CRITICAL":
                # A deferred review is not a final AI investment decision. Its brief
                # remains in the memory interval for the next genuine decision.
                data["phase"] = "review_deferred"
            else:
                deadline = self.monotonic() + 600
                async with asyncio.timeout(600):
                    data["phase"] = "decision"
                    candidates = {c["symbol"] for c in data["brief"]["candidates"]}
                    current = await self.context("refresh", {"symbols": sorted(candidates)})
                    if blocked(current) or fingerprint(current) != fingerprint(frozen):
                        raise ValueError("account_changed_during_brief")
                    data["decision_context"] = current
                    related_symbols = candidates | {h["symbol"] for h in current["holdings"]}
                    evidence_index = views.relevant_evidence(
                        list(catalogue.values()), related_symbols, mandatory
                    )
                    visible_ids = (
                        set(data["brief"]["evidence_ids"])
                        | {e["id"] for e in evidence_index}
                        | {e["id"] for e in frozen.get("mandatory_evidence", [])}
                        | {"market:" + s for s in related_symbols if s in frozen["details"]}
                    )
                    book = self.store.memory_book()
                    self.brief_before = self.store.run_cursor(identity)
                    page = self.store.brief_interval(book["through"], self.brief_before)
                    self.brief_next, self.briefs_more = page["next"], page["has_more"]
                    data["memory_book_before"] = book
                    data["memory_brief_before"] = self.brief_before
                    data["intervening_briefs"] = [page]
                    rows_by_symbol = {r["symbol"]: r for r in rows}
                    payload = {
                        "brief": views.compact_brief(data["brief"]),
                        "market_data_policy": views.MARKET_DATA_POLICY,
                        "severity_policy": views.SEVERITY_POLICY,
                        "account": views.account_view(current),
                        "market_coverage": views.market_coverage(frozen),
                        "sell_eligible_symbols": sorted(views.managed_symbols(current)),
                        "candidates": [
                            views.stock(rows_by_symbol[s], frozen["details"][s])
                            for s in sorted(candidates)
                        ],
                        "held_market": views.stock_table(
                            [
                                views.stock(rows_by_symbol[s], frozen["details"][s])
                                for s in sorted(views.managed_symbols(current) - candidates)
                                if s in rows_by_symbol
                            ]
                        ),
                        "mandatory_evidence": views.compact_evidence(
                            frozen.get("mandatory_evidence", [])
                        ),
                        "evidence_index": [e for e in evidence_index if e["id"] not in mandatory],
                        "memory_book": memory.with_execution(
                            book, current.get("execution_feedback", [])
                        ),
                        "intervening_briefs": page,
                        "manual_instruction": request.get("instruction")
                        if kind == "manual"
                        else None,
                        "instruction_policy": "Owner requests are proposals. Decide independently. "
                        "Never bypass risk, evidence, ownership or order constraints.",
                        "schema": DECISION_SCHEMA,
                        "tool_schema": TOOL_SCHEMA,
                        "retrieval_policy": (
                            "Request a fact only if it could materially change a feasible "
                            "buy/sell/hold decision. Quotes, indicators and warnings already "
                            "supplied for a symbol are the same frozen data returned by market; "
                            "reading them again cannot make them fresher. Combine missing fields "
                            "for a symbol and related evidence IDs within the tool schema. "
                            "Prefer delegated news for routine public fact gathering. If an "
                            "existing hard constraint already excludes an action, do not research "
                            "solely to lengthen its explanation. Missing decisive evidence can "
                            "justify NO_ACTION; do not invent evidence. Necessary reads and new "
                            "direct research remain available. Middle-layer news is limited "
                            "to two investigations per pipeline."
                        ),
                        "tool_availability": {
                            "briefs": True,
                            "evidence": True,
                            "market": True,
                            "news": configuration.get("web_search_connected", False),
                            "search": configuration.get("decision_search_connected", False),
                            "search_policy": (
                                "search uses your provider for direct public research; "
                                "news delegates to the middle layer at most twice per pipeline. "
                                "Use existing evidence first; request news only for a decisive "
                                "missing fact, not general company background. "
                                "Direct search has no count cap; "
                                "finish within the deadline."
                            ),
                        },
                        "available_evidence_ids": sorted(visible_ids),
                        "tool_results": [],
                    }
                    data["tool_results"] = []
                    while True:
                        value = await self.call(
                            "research", "investment_decision", payload, deadline
                        )
                        if value.get("action") != "READ":
                            data["decision"] = validate_decision(
                                value, candidates, set(catalogue), current
                            )
                            for intent in value["intents"]:
                                if not frozen["details"][intent["symbol"]].get("daily_bars"):
                                    raise ValueError("missing_trade_evidence")
                            break
                        if value.get("tool") not in {"news", "search"}:
                            if self.tools >= 6:
                                raise ValueError("read_tool_budget")
                            self.tools += 1
                        result = await self.read_tool(value, frozen, catalogue, deadline)
                        data["tool_results"].append(result)
                        if value["tool"] == "briefs":
                            payload["memory_book"] = payload["memory_book"] | {
                                "content": value["arguments"]["memory_book"],
                                "draft": True,
                            }
                            payload["intervening_briefs"] = result["result"]
                        payload["tool_results"] = views.working_results(
                            [r for r in data["tool_results"] if r["tool"] != "briefs"]
                        )
                        if isinstance(result.get("result"), dict):
                            visible_ids.update(
                                s["id"]
                                for s in result["result"].get("sources", [])
                                if isinstance(s, dict)
                            )
                        payload["available_evidence_ids"] = sorted(visible_ids)
                        payload["retrieval_history"] = [
                            {"tool": r["tool"], "arguments": r["arguments"]}
                            for r in data["tool_results"][-6:]
                        ]
                        payload["remaining_reads"] = 6 - self.tools
                        payload["search_calls"] = self.news_calls
                        payload["tool_availability"]["news"] = (
                            configuration.get("web_search_connected", False)
                            and self.middle_news_calls < 2
                        )
                    # Inference may take minutes. Re-read again before publishing any intent.
                    final = await self.context("refresh", {"symbols": sorted(candidates)})
                    if blocked(final) or fingerprint(final) != fingerprint(current):
                        raise ValueError("account_changed_during_decision")
                    data["final_context"] = final
                    data["memory_update"] = {
                        "base_revision": book["revision"],
                        "through": self.brief_next,
                        "content": data["decision"]["memory_book"],
                    }
            data["evidence"] = list(catalogue.values())
            data["tool_calls"], data["news_calls"] = self.tools, self.news_calls
            self.store.finish(identity, "complete", data, self.clock())
            if kind == "scheduled":
                self.store.put(
                    "pending_warn",
                    [i for i in self.store.get("pending_warn", []) if i not in pending_warn],
                )
            return data
        except Exception as error:
            while isinstance(error, ExceptionGroup):
                error = error.exceptions[0]
            if isinstance(error, TimeoutError) and data.get("phase") == "context_collection":
                error = ValueError("context_collection_timeout")
            # No raw-data fallback, blind retry, or provider error text in owner-visible output.
            data["error"] = (
                str(error)
                if isinstance(error, ValueError) and str(error).replace("_", "").isalnum()
                else type(error).__name__
            )
            if getattr(error, "context_failure", None):
                data["context_failure"] = error.context_failure
            data["evidence"] = list(catalogue.values())
            data["tool_calls"], data["news_calls"] = self.tools, self.news_calls
            self.store.finish(identity, "aborted", data, self.clock())
            return None

    async def read_tool(self, value, frozen, catalogue, deadline):
        if set(value) != {"action", "tool", "arguments"} or not isinstance(
            value["arguments"], dict
        ):
            raise ValueError("invalid_read_tool")
        tool, args = value["tool"], value["arguments"]
        if tool == "briefs" and set(args) == {"after", "memory_book"}:
            if (
                type(args["after"]) is not int
                or not getattr(self, "briefs_more", False)
                or args["after"] != self.brief_next
            ):
                raise ValueError("invalid_brief_cursor")
            draft = memory.validate_content(args["memory_book"])
            self.data.setdefault("memory_drafts", []).append(
                {"through": args["after"], "content": draft}
            )
            args = {"after": args["after"]}
            result = self.store.brief_interval(self.brief_next, self.brief_before)
            self.brief_next, self.briefs_more = result["next"], result["has_more"]
            self.data["intervening_briefs"].append(result)
        elif tool == "evidence" and set(args) == {"ids"}:
            ids = args["ids"]
            if not isinstance(ids, list) or not 1 <= len(ids) <= 5:
                raise ValueError("invalid_evidence_read")
            if any(not isinstance(i, str) or i not in catalogue for i in ids):
                raise ValueError("unavailable_evidence")
            ids = list(dict.fromkeys(ids))
            args = args | {"ids": ids}
            result = [views.evidence_view(catalogue[i], excerpt=True) for i in ids]
        elif tool == "market" and set(args) == {"symbol", "fields"}:
            if args["symbol"] not in frozen["details"]:
                raise ValueError("unavailable_frozen_symbol")
            fields = args["fields"]
            if (
                not isinstance(fields, list)
                or not fields
                or any(
                    f
                    not in {
                        "quote",
                        "indicators",
                        "warnings",
                        "comparison",
                        "daily_bars",
                        "orderbook",
                        "trades",
                    }
                    for f in fields
                )
            ):
                raise ValueError("invalid_market_read")
            fields = list(dict.fromkeys(fields))
            args = args | {"fields": fields}
            result = views.market_read(frozen, args["symbol"], fields)
        elif tool in {"news", "search"} and set(args) == {"symbols", "topics"}:
            if (
                not isinstance(args["symbols"], list)
                or not args["symbols"]
                or any(s not in frozen["details"] for s in args["symbols"])
                or not isinstance(args["topics"], list)
                or not args["topics"]
                or any(t not in TOPICS for t in args["topics"])
            ):
                raise ValueError("invalid_news_request")
            args = {k: sorted(set(args[k])) for k in ("symbols", "topics")}
            cache_key = (tool, tuple(args["symbols"]), tuple(args["topics"]))
            cached = self.search_cache.get(cache_key)
            if cached and 0 <= self.monotonic() - cached[0] < 60:
                for source in cached[2]:
                    catalogue[source["id"]] = copy.deepcopy(source)
                self.bind_citations(s["id"] for s in cached[2])
                self.search_cache_hits += 1
                if self.data is not None:
                    self.data["search_cache_hits"] = self.search_cache_hits
                return copy.deepcopy(cached[1]) | {"cache_hit": True}
            if tool == "news":
                if self.middle_news_calls >= 2:
                    return {
                        "tool": tool,
                        "arguments": args,
                        "result": {
                            "status": "middle_search_limit",
                            "instruction": (
                                "Both middle investigations are already used. "
                                "Review saved evidence; "
                                "do not repeat news. Missing evidence can justify NO_ACTION."
                            ),
                        },
                    }
                self.middle_news_calls += 1
                if self.data is not None:
                    self.data["middle_news_calls"] = self.middle_news_calls
            self.news_calls += 1
            # Only public stock names and allowlisted topics can leave in a search query.
            try:
                found = await self.news(
                    args | ({"_role": "research"} if tool == "search" else {}), frozen, deadline
                )
            except Exception as error:
                if getattr(error, "usage", None):
                    self.trace.append(error.usage | {"at": self.clock()})
                raise
            native = found if isinstance(found, dict) else None
            sources = native["sources"] if native else found
            for source in sources:
                catalogue[source["id"]] = source
                if source.get("source") != "Google Search":
                    self.store.evidence("web", source, self.clock(), source["id"])
            self.bind_citations(s["id"] for s in sources)
            # This is a separate news budget; do not silently borrow brief's 16 calls.
            if native:
                result = native["summary"]
                self.trace.extend(
                    t | {"at": self.clock(), "decision": result} for t in native["trace"]
                )
                if self.data is not None:
                    self.data.setdefault("grounded_news", []).append(native["grounding"])
            else:
                result = await self.call(
                    "research" if tool == "search" else "middle",
                    "web_search_summary" if tool == "search" else "news_research",
                    {
                        "sources": [views.evidence_view(s, excerpt=True) for s in sources],
                        "evaluation_policy": (
                            "Prefer issuer IR, original filings, regulators "
                            "and official statistics. "
                            "Community posts are leads, not verified primary evidence. Distinguish "
                            "publication dates from retrieval dates, and a fetched page "
                            "from a search "
                            "snippet. The same source_group is not independent corroboration. "
                            "Explicitly report unrelated, stale, unavailable "
                            "and conflicting evidence."
                        ),
                        "schema": {
                            "summary": "Korean grounded summary",
                            "evidence_ids": ["source ID"],
                            "uncertainties": ["missing / conflicting evidence"],
                        },
                    },
                    deadline,
                    count_brief=False,
                )
            if (
                set(result) != {"summary", "evidence_ids", "uncertainties"}
                or not isinstance(result["evidence_ids"], list)
                or any(not isinstance(i, str) for i in result["evidence_ids"])
                or not set(result["evidence_ids"]).issubset({s["id"] for s in sources})
                or not isinstance(result["uncertainties"], list)
                or len(result["uncertainties"]) > 100
            ):
                raise ValueError("invalid_news_summary")
            text(result["summary"])
            for uncertainty in result["uncertainties"]:
                text(uncertainty, 1000)
            # The decision receives the middle layer's synthesis and citations. Full
            # fetched text stays frozen in the evidence catalogue for an explicit read.
            result = {
                "summary": result,
                "sources": [views.evidence_view(source) for source in sources],
            }
        else:
            raise ValueError("unapproved_read_tool")
        response = {"tool": tool, "arguments": copy.deepcopy(args), "result": result}
        if tool in {"news", "search"}:
            response["cache_hit"] = False
        from veyquant.input_budget import compact_json

        if len(compact_json(response).encode()) > 8000:
            response["result"] = {
                "status": "page_too_large",
                "instruction": (
                    "Request fewer fields, IDs, or symbols; full material remains stored."
                ),
            }
        if tool in {"news", "search"} and response["result"].get("status") != "page_too_large":
            self.search_cache[cache_key] = (
                self.monotonic(),
                copy.deepcopy(response),
                copy.deepcopy(sources),
            )
        return response
