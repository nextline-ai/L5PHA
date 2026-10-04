"""Replay archived decisions offline; print sizes only and optionally save private inputs.

No collector, provider, broker, runtime or mutable DecisionStore is instantiated. Recorded
model decisions are reused, so this measures context construction, not investment quality.
Older archives lack the exact pending-evidence state and therefore yield reconstructions.
"""

import argparse
import asyncio
import copy
import json
import os
import sqlite3
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from veyquant import shadow_inference
from veyquant.decision_pipeline import DecisionPipeline


def encoded(value, compact=False):
    options = {"separators": (",", ":")} if compact else {}
    return json.dumps(value, ensure_ascii=False, allow_nan=False, **options)


def read_archive(database=None, identity=None, source=None):
    if source:
        record = json.loads(Path(source).read_text())
    else:
        uri = Path(database).resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            row = db.execute("SELECT * FROM decision_runs WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise ValueError("archive_not_found")
            record = dict(row)
    if isinstance(record.get("data"), str):
        record["data"] = json.loads(record["data"])
    if "data" not in record:
        record = {"data": record}
    if not isinstance(record["data"], dict) or "initial_context" not in record["data"]:
        raise ValueError("archive_context_unavailable")
    return record


def private_directory(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("unsafe_output_directory")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def private_json(directory, name, value):
    path = directory / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(encoded(value))


def visible_input(role, payload, configuration):
    models = shadow_inference.model_selection(configuration.get("models"))
    model, strategy = models[role], configuration.get("strategy")
    if hasattr(shadow_inference, "prepare_model_input"):
        kwargs = {"prompts": configuration["prompts"]} if "prompts" in configuration else {}
        prepared = shadow_inference.prepare_model_input(role, payload, model, strategy, **kwargs)
        return {
            "system": prepared["system"],
            "message": prepared["message"],
            "specification": prepared.get("output_format")
            or ({"type": "json_object"} if model.startswith("gpt-") else None),
            "measurement": prepared["input_context"],
        }

    # Legacy source exposes no pure input builder. Replace every transport before
    # calling its serializer; abort at the first adapter invocation, before any I/O.
    captured = {}

    class Captured(Exception):
        pass

    def direct(provider, _model, system, message, *_args, output_format=None, **_kwargs):
        if provider == "openai":
            if output_format:
                system += "\nWrap the requested response in the required result field."
            message = "Return one JSON object matching the supplied schema.\n" + message
        captured.update(
            system=system,
            message=message,
            specification=(output_format or {"type": "json_object"})
            if provider == "openai"
            else None,
        )
        raise Captured

    def converse(**kwargs):
        captured.update(
            system=kwargs["system"][0]["text"],
            message=kwargs["messages"][0]["content"][0]["text"],
            specification=None,
        )
        raise Captured

    with (
        patch.object(
            shadow_inference, "openai_converse", lambda *a, **k: direct("openai", *a, **k)
        ),
        patch.object(
            shadow_inference, "gemini_converse", lambda *a, **k: direct("gemini", *a, **k)
        ),
    ):
        try:
            shadow_inference.call(
                SimpleNamespace(converse=converse),
                role,
                payload,
                [],
                models=models,
                strategy=strategy,
                keys={},
                reasoning=configuration.get("reasoning"),
            )
        except Captured:
            pass
    if not captured:
        raise ValueError("offline_input_capture_failed")
    return captured


class ReplayStore:
    def __init__(self, data):
        self.data = data
        self.state = {}
        self.finished = None
        introduced = {
            e["id"]
            for item in data.get("tool_results", [])
            if isinstance(item.get("result"), dict)
            for e in item["result"].get("sources", [])
        }
        self.items = [
            e
            for e in data.get("evidence", [])
            if not e.get("id", "").startswith("market:") and e.get("id") not in introduced
        ]

    def recent(self, *_args, **_kwargs):
        return copy.deepcopy(self.items)

    def lookup(self, identities):
        selected = set(identities)
        return [copy.deepcopy(e) for e in self.items if e.get("id") in selected]

    def mandatory(self, _now, symbols):
        return [
            copy.deepcopy(e)
            for e in self.items
            if e.get("kind") == "dart_important" and (not e.get("symbol") or e["symbol"] in symbols)
        ]

    def memory(self, limit=12):
        return copy.deepcopy(self.data.get("previous_investment_rationales", [])[:limit])

    def memory_book(self):
        return copy.deepcopy(
            self.data.get("memory_book_before")
            or {
                "revision": 0,
                "through": 0,
                "source_run_id": None,
                "updated_at": None,
                "origin": "empty",
                "content": "아직 기록된 메모리가 없습니다.",
                "last_proposals": [],
            }
        )

    def run_cursor(self, _identity):
        return self.data.get("memory_brief_before", 10**12)

    def brief_interval(self, after, before):
        cursor = self.memory_book()["through"]
        for page in self.data.get("intervening_briefs", []):
            if after == cursor:
                return copy.deepcopy(page)
            cursor = page["next"]
        if not self.data.get("intervening_briefs"):
            return {
                "items": [],
                "next": before,
                "has_more": False,
                "scope": "No interval archived in this historical record.",
            }
        raise ValueError("unrecorded_brief_page")

    def get(self, key, default=None):
        return copy.deepcopy(self.state.get(key, default))

    def put(self, key, value):
        self.state[key] = copy.deepcopy(value)

    def progress(self, *_args):
        pass

    def finish(self, _identity, state, data, _now):
        self.finished = {"state": state, "error": data.get("error")}

    def evidence(self, _kind, item, _now, identity):
        self.items.append(copy.deepcopy(item))
        return identity


class TraceReplay:
    def __init__(self, data):
        self.entries = copy.deepcopy(data.get("trace") or [])
        self.used = set()
        self.batch_symbols = {}
        rows = data["initial_context"]["review_universe"]
        batch = 0
        batch_size = data.get("review_batch_size", 20)
        for index, entry in enumerate(self.entries):
            if entry.get("task") == "review_batch":
                self.batch_symbols[index] = {r["symbol"] for r in rows[batch : batch + batch_size]}
                batch += batch_size

    def take(self, role, task, payload=None):
        symbols = None
        if task == "review_batch" and payload is not None:
            symbols = (
                set(payload["allowed_candidate_symbols"])
                if "allowed_candidate_symbols" in payload
                else {r["symbol"] for r in payload["stocks"]}
            )
        for index, entry in enumerate(self.entries):
            if index in self.used or entry.get("role") != role or entry.get("task") != task:
                continue
            if symbols is not None and self.batch_symbols.get(index) != symbols:
                continue
            self.used.add(index)
            return entry
        raise ValueError("archived_model_response_unavailable")


def measurement(role, task, payload, visible):
    result = copy.deepcopy(visible.get("measurement") or {})
    if not result:
        specification = visible["specification"]
        result = {
            "payload_bytes": len(visible["message"].encode()),
            "system_bytes": len(visible["system"].encode()),
            "schema_bytes": len(encoded(specification).encode()) if specification else 0,
            "fields": {
                k: {"bytes": len(encoded(v).encode()), "type": type(v).__name__}
                for k, v in payload.items()
            },
        }
        result["total_bytes"] = sum(
            result[k] for k in ("payload_bytes", "system_bytes", "schema_bytes")
        )
    # Hashes, estimates and private values are unnecessary in the public aggregate.
    return {"role": role, "task": task} | {
        k: v for k, v in result.items() if k.endswith("_bytes") or k == "fields"
    }


async def replay(record, output_dir=None, label="offline_replay", adapt_recorded_citations=False):
    data = copy.deepcopy(record["data"])
    store, traces = ReplayStore(data), TraceReplay(data)
    output = private_directory(output_dir) if output_dir else None
    configuration = data.get("configuration", {}) | {
        "settings_revision": data.get(
            "settings_revision", data["initial_context"]["settings_revision"]
        ),
        "web_search_connected": True,
        "decision_search_connected": True,
    }
    calls = []
    adaptation = {"modified_outputs": 0, "removed_citation_references": 0}
    refreshes = 0
    news_index = 0
    clock = record.get("started") or data["initial_context"].get("as_of", 1)
    pipeline = None

    async def model(role, payload):
        entry = traces.take(role, payload["task"], payload)
        visible = visible_input(role, payload, configuration)
        counts = measurement(role, payload["task"], payload, visible)
        counts["budget_exceeded"] = (
            counts["total_bytes"] > counts["budget_bytes"]
            if type(counts.get("budget_bytes")) is int
            else None
        )
        calls.append(counts)
        if output:
            private_json(
                output,
                f"input-{len(calls):03d}.json",
                {
                    "role": role,
                    "task": payload["task"],
                    "payload": payload,
                    "visible": {k: v for k, v in visible.items() if k != "measurement"},
                },
            )
        if not isinstance(entry.get("decision"), dict):
            raise ValueError("archived_model_response_unavailable")
        result = pipeline.citation_values(copy.deepcopy(entry["decision"]))
        if adapt_recorded_citations and payload["task"] == "review_batch":
            allowed = set(payload.get("available_evidence_ids", []))
            retained = [identity for identity in result["evidence_ids"] if identity in allowed]
            removed = len(result["evidence_ids"]) - len(retained)
            if removed:
                adaptation["modified_outputs"] += 1
                adaptation["removed_citation_references"] += removed
                result["evidence_ids"] = retained
        usage = {k: v for k, v in entry.items() if k not in {"decision", "role", "task", "at"}}
        return result, usage

    async def context(operation, _request):
        nonlocal refreshes
        if operation == "initial":
            return copy.deepcopy(data["initial_context"])
        refreshes += 1
        key = "decision_context" if refreshes == 1 else "final_context"
        if key not in data:
            raise ValueError("archived_context_unavailable")
        return copy.deepcopy(data[key])

    async def news(args, _frozen, _deadline):
        nonlocal news_index
        tool = "search" if args.get("_role") == "research" else "news"
        items = [
            item for item in data.get("tool_results", []) if item.get("tool") in {"news", "search"}
        ]
        if news_index >= len(items):
            raise ValueError("archived_search_unavailable")
        item = items[news_index]
        news_index += 1
        if item["tool"] != tool or item["arguments"] != {
            k: v for k, v in args.items() if k != "_role"
        }:
            raise ValueError("archived_search_mismatch")
        result = item["result"]
        identities = {source["id"] for source in result["sources"]}
        sources = [copy.deepcopy(e) for e in data["evidence"] if e.get("id") in identities]
        if len(sources) != len(identities):
            raise ValueError("archived_search_sources_unavailable")
        role = "research" if tool == "search" else "middle"
        provider = next((e.get("source") for e in sources), None)
        if provider not in {"Google Search", "OpenAI Web Search"}:
            return sources
        task = "web_search" if tool == "search" else "news_research"
        entry = traces.take(role, task)
        return {
            "sources": sources,
            "summary": copy.deepcopy(result["summary"]),
            "trace": [{k: v for k, v in entry.items() if k != "decision"}],
            "grounding": copy.deepcopy((data.get("grounded_news") or [{}])[news_index - 1]),
        }

    pipeline = DecisionPipeline(model, context, news, store, clock=lambda: clock)
    await pipeline.run(
        "offline-audit",
        data.get("kind", record.get("kind", "scheduled")),
        data.get("request", {}),
        configuration,
    )
    groups = {}
    for call in calls:
        key = (call["role"], call["task"])
        group = groups.setdefault(
            key,
            {
                "role": key[0],
                "task": key[1],
                "calls": 0,
                "total_bytes": 0,
                "max_call_bytes": 0,
                "payload_bytes": 0,
                "system_bytes": 0,
                "schema_bytes": 0,
                "budget_bytes": call.get("budget_bytes"),
                "calls_exceeding_budget": 0,
                "fields_bytes": Counter(),
            },
        )
        group["calls"] += 1
        group["max_call_bytes"] = max(group["max_call_bytes"], call["total_bytes"])
        group["calls_exceeding_budget"] += (
            type(call.get("budget_bytes")) is int and call["total_bytes"] > call["budget_bytes"]
        )
        for field in ("total_bytes", "payload_bytes", "system_bytes", "schema_bytes"):
            group[field] += call[field]
        group["fields_bytes"].update({k: v["bytes"] for k, v in call["fields"].items()})
    usage = {}
    for entry in data.get("trace") or []:
        group = usage.setdefault(entry.get("role", "unknown"), Counter())
        group["calls"] += 1
        for key in ("input_tokens", "output_tokens", "cached_input_tokens", "search_queries"):
            if type(entry.get(key)) is int:
                group[key] += entry[key]
        group["unknown_input_usage"] += type(entry.get("input_tokens")) is not int
    summary = {
        "label": label,
        "scope": "offline_reconstruction_with_recorded_model_outputs_no_network",
        "fidelity": "archived_frozen_data; original_pending_evidence_duplicates_not_preserved",
        "validation_scope": "context_shape_only_not_investment_quality",
        "recorded_output_adaptation": {
            "enabled": adapt_recorded_citations,
            "policy": "remove_unavailable_review_batch_citations_only",
            **adaptation,
        },
        "result": store.finished,
        "model_payloads": len(calls),
        "archived_trace_entries": len(traces.entries),
        "replayed_trace_entries": len(traces.used),
        "groups": list(groups.values()),
        "call_budgets": [
            {
                k: call.get(k)
                for k in ("role", "task", "total_bytes", "budget_bytes", "budget_exceeded")
            }
            for call in calls
        ],
        "archived_provider_usage_not_new_billing": usage,
        "native_search_inputs_replayed": False,
    }
    if output:
        private_json(output, "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--database")
    sources.add_argument("--input")
    parser.add_argument("--run")
    parser.add_argument("--output-dir")
    parser.add_argument("--label", default="offline_replay")
    parser.add_argument(
        "--adapt-recorded-citations",
        action="store_true",
        help="Context-only comparison: drop historical batch citations absent from new inputs.",
    )
    args = parser.parse_args()
    if args.database and not args.run:
        parser.error("--database requires --run")
    try:
        record = read_archive(args.database, args.run, args.input)
        result = asyncio.run(
            replay(record, args.output_dir, args.label, args.adapt_recorded_citations)
        )
    except Exception as error:
        # Error text, file paths and archived values may be private.
        print(encoded({"error": type(error).__name__, "scope": "offline_audit_failed"}))
        return 1
    print(encoded(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
