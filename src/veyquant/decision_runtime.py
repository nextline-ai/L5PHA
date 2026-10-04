"""One local decision pipeline, separate surveillance, and private owner requests."""

import asyncio
import hashlib
import json
import pwd
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3
from botocore.config import Config

from veyquant.decision_pipeline import DecisionPipeline, severity
from veyquant.decision_store import DecisionStore, encoded
from veyquant.input_budget import measure_input
from veyquant.model_context import SEVERITY_POLICY
from veyquant.native_search import PROVIDERS, public_research_view, search_provider
from veyquant.research_context import SOCKET, exchange, serve
from veyquant.shadow_contract import atomic_json, bounded_json, read_model_configuration
from veyquant.shadow_inference import models_ready
from veyquant.surveillance import observe, schedule, session

API_SOCKET = "/run/veyquant-analysis/api.sock"


def relevant_signals(signals, status, now):
    at = status.get("exposure_as_of")
    eligible = status.get("eligible_symbols")
    if type(at) not in (int, float) or not 0 <= now - at <= 30 or eligible is None:
        return signals, []
    exposed = set(status.get("exposed_symbols", []))
    tradable = set(eligible)
    accepted, deferred = [], []
    for signal in signals:
        symbol = signal.get("symbol")
        if (
            symbol
            and symbol not in exposed
            and symbol not in tradable
            and signal.get("kind") in {"dart_important", "new_warning"}
        ):
            deferred.append(signal)
        else:
            accepted.append(signal)
    return accepted, deferred


def surveillance_inputs(signals):
    """Allowlisted trigger facts only; original provider payloads stay in the store."""
    fields = {
        "price_3pct": {"change_5m", "price_as_of", "price_source"},
        "price_volume": {"volume_change_5m", "volume_zscore", "volume_as_of", "volume_baseline"},
        "spread_50bp": {"spread_bp", "book_as_of"},
    }
    fields["price_5pct"] = fields["price_3pct"]
    fields["price_shock"] = fields["price_3pct"]
    fields["spread_100bp"] = fields["spread_50bp"]
    text_limits = {"condition": 32, "kind": 32, "symbol": 12, "title": 180}
    result = []
    for signal in signals:
        item = {
            key: value[: text_limits.get(key, 40)] if isinstance(value, str) else value
            for key, value in signal.items()
            if key in {"condition", "kind", "symbol", "at", "last_at", "as_of", "title", "active"}
            and (value is None or type(value) in {str, int, float, bool})
        }
        condition = signal.get("condition")
        if condition in fields and isinstance(signal.get("metrics"), dict):
            item["metrics"] = {
                k: v[:80] if isinstance(v, str) else v
                for k, v in signal["metrics"].items()
                if k in fields[condition] and (v is None or type(v) in {str, int, float, bool})
            }
        warning = signal.get("warning")
        if signal.get("kind") == "new_warning" and isinstance(warning, dict):
            warning_limits = {
                "warningType": 64,
                "type": 64,
                "name": 120,
                "startDate": 10,
                "endDate": 10,
            }
            item["warning"] = {
                k: v[: warning_limits.get(k, 10)] if isinstance(v, str) else v
                for k, v in warning.items()
                if k in {"warningType", "type", "name", "startDate", "endDate", "isSuspended"}
                and (v is None or type(v) in {str, int, float, bool})
            }
        result.append(item)
    return result


def corroborated_signals(signals):
    """Pair measurements from the same stock and aligned timestamps, never aggregate maxima."""
    pairs = {}
    for signal in signals:
        m = signal.get("metrics") or {}
        symbol = signal.get("symbol")
        change, z = m.get("volume_change_5m"), m.get("volume_zscore")
        if symbol and all(type(v) in (int, float) for v in (change, z)):
            if (
                abs(change) >= 0.05
                and z >= 10
                and type(m.get("volume_as_of")) in (int, float)
                and m["volume_as_of"] > 0
            ):
                pairs[symbol] = {
                    "symbol": symbol,
                    "change_5m": change,
                    "volume_zscore": z,
                    "as_of": m.get("volume_as_of"),
                    "basis": "same_volume_window",
                }
        price, spread = m.get("change_5m"), m.get("spread_bp")
        pt, bt = m.get("price_as_of"), m.get("book_as_of")
        if symbol and all(type(v) in (int, float) for v in (price, spread, pt, bt)):
            if abs(price) >= 0.05 and spread >= 200 and min(pt, bt) > 0 and abs(pt - bt) <= 30:
                pairs[symbol] = {
                    "symbol": symbol,
                    "change_5m": price,
                    "spread_bp": spread,
                    "price_as_of": pt,
                    "book_as_of": bt,
                    "basis": "aligned_price_book",
                }
    return {
        "count": len(pairs),
        "examples": [pairs[s] for s in sorted(pairs)[:3]],
        "scope": "Strong same-stock corroboration examples, not a mandatory CRITICAL verdict.",
    }


def surveillance_summary(signals):
    """A widespread event costs one bounded call, without forwarding every observation."""
    groups = {}
    conditions = {
        "heartbeat",
        "price_3pct",
        "price_5pct",
        "price_shock",
        "spread_100bp",
        "price_volume",
        "spread_50bp",
        "realtime_gap",
        "dart_important",
        "new_warning",
    }
    for signal in surveillance_inputs(signals):
        condition = signal.get("condition") or signal.get("kind")
        groups.setdefault(condition if condition in conditions else "unknown", []).append(signal)

    def priority(signal):
        metrics = signal.get("metrics") or {}
        for field in ("spread_bp", "volume_zscore", "change_5m"):
            if type(metrics.get(field)) in {int, float}:
                return abs(metrics[field])
        return signal.get("at") or 0

    return {
        "signal_count": len(signals),
        "distinct_symbols": len({s["symbol"] for s in signals if s.get("symbol")}),
        "market_breadth": "unknown: selected event samples, not a representative market survey",
        "corroboration": corroborated_signals(signals),
        "units": "change_5m is a fraction (0.03 = 3%); spread_bp is basis points (50 = 0.5%)",
        "conditions": [
            {
                "condition": condition,
                "count": len(items),
                "metric_ranges": {
                    field: [min(values), max(values)]
                    for field in ("change_5m", "volume_change_5m", "volume_zscore", "spread_bp")
                    if (
                        values := [
                            (v.get("metrics") or {}).get(field)
                            for v in items
                            if type((v.get("metrics") or {}).get(field)) in (int, float)
                        ]
                    )
                },
                "examples": sorted(items, key=priority, reverse=True)[:3],
                "omitted_count": max(0, len(items) - 3),
            }
            for condition, items in sorted(groups.items())
        ],
        "scope": "Counts include every trigger; examples are at most three per condition. "
        "Metric ranges can come from different stocks and times; their maxima are not "
        "simultaneous evidence. All original trigger evidence is retained for later review. "
        "Unshown details are unknown, "
        "not evidence of safety; a grouped material event with insufficient details "
        "is at least WARN.",
    }


def brief_preview(value):
    if not value:
        return None
    return {
        "severity": value["severity"],
        **({"handoff": value["handoff"]} if "handoff" in value else {}),
        "summary": value["summary"][:1000],
        "candidates": [
            {"symbol": c["symbol"], "reason": c["reason"][:300]} for c in value["candidates"]
        ],
    }


def decision_preview(value):
    if not value:
        return None
    return {
        "action": value["action"],
        **{k: value.get(k, "")[:1000] for k in ("summary", "counterargument", "uncertainty")},
        "intents": [
            {k: v[:300] if k == "rationale" else v for k, v in item.items() if k != "evidence_ids"}
            for item in value.get("intents", [])
        ],
    }


class Runtime:
    def __init__(
        self, store, function_arn, control_path, market_path, report_path, clock=time.time
    ):
        self.store, self.function_arn = store, function_arn
        self.control_path, self.market_path, self.report_path = (
            control_path,
            market_path,
            report_path,
        )
        self.clock = clock
        self.client = boto3.Session(region_name="ap-southeast-2").client(
            "lambda",
            config=Config(
                connect_timeout=5,
                read_timeout=230,
                max_pool_connections=5,
                retries={"total_max_attempts": 1, "mode": "adaptive"},
            ),
        )
        self.task = None
        self.monitor_task = None
        self.state = "starting"
        self.calendar = None
        self.orders_blocked = True
        self.status_at = 0
        self.next_sessions = []
        self.source_state = {
            "web": "not_connected",
            "dart": "not_connected",
            "krx": "not_connected",
        }
        self.krx_cache = None
        self.public_research = []

    def configuration(self):
        config = read_model_configuration(self.control_path, self.clock())
        if not models_ready(config["models"], config.get("provider_credentials")):
            raise ValueError("model_setup_required")
        return config

    async def capability(self, configuration, operation, **fields):
        usage_id = None
        if operation in {"stage", "native_search"}:
            role = fields.get("role", "middle")
            active = self.store.active() if role != "cheap" else None
            payload = fields["payload"] if operation == "stage" else {"queries": fields["queries"]}
            if operation == "native_search" and fields.get("prior_research"):
                payload["prior_research"] = fields["prior_research"]
            context = measure_input(role, payload, task=operation)
            context["scope"] = (
                "submitted_payload_excludes_system_schema_and_provider_internal_context"
            )
            usage_id = self.store.usage_start(
                self.clock(),
                active[0] if active else None,
                operation,
                role,
                configuration.get("models", {}).get(role),
                input_context=context,
                model_payload=payload,
            )

        def invoke():
            result = self.client.invoke(
                FunctionName=self.function_arn,
                InvocationType="RequestResponse",
                LogType="None",
                Payload=encoded(
                    {"operation": operation, "issued_at": self.clock(), **configuration, **fields}
                ).encode(),
            )
            with result["Payload"] as stream:
                raw = stream.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("provider_call_failed")
            value = json.loads(raw)
            if result.get("FunctionError"):
                code = value.get("errorMessage", "")
                if re.fullmatch(
                    r"(?:openai|gemini)_http_[1-5][0-9]{2}(?:_(?:insufficient_quota|invalid_api_key|model_not_found|rate_limit_exceeded|invalid_request_error|unsupported_parameter|permission_denied|json_input_required))?",
                    code,
                ):
                    raise ValueError(code)
                raise ValueError("provider_call_failed")
            return value

        value = await asyncio.to_thread(invoke)
        if usage_id:
            self.store.usage_finish(usage_id, (value.get("trace") or [{}])[0], value.get("error"))
        if "error" in value:
            error = ValueError(
                value["error"]
                if re.fullmatch(r"[a-z][a-z0-9_]{1,100}", str(value["error"]))
                else "provider_call_failed"
            )
            error.usage = (value.get("trace") or [{}])[0]
            raise error
        return value

    async def context(self, operation, request):
        config = self.configuration()  # Revocation prevents a new broker context read.
        result = await exchange(SOCKET, {"operation": operation, "request": request})
        if operation == "initial":
            result["optional_sources"] = {
                p: p in config.get("provider_credentials", {}) for p in ("dart", "krx")
            }
            cache = self.krx_cache
            if (
                result["optional_sources"]["krx"]
                and cache
                and 0 <= self.clock() - cache["collected_at"] <= 36 * 3600
            ):
                by_symbol = {r["symbol"]: r for r in cache["rows"]}
                for stock in result["review_universe"]:
                    if stock["symbol"] in by_symbol:
                        stock["krx_daily"] = by_symbol[stock["symbol"]] | {"as_of": cache["as_of"]}
                for symbol, detail in result["details"].items():
                    if symbol in by_symbol:
                        detail["krx_daily"] = by_symbol[symbol] | {"as_of": cache["as_of"]}
                table = result["comparison_table"]
                table["columns"] = [*table["columns"], "krx_market_cap_krw"]
                table["krx_as_of"] = cache["as_of"]
                table["rows"] = [
                    r + [by_symbol.get(r[0], {}).get("market_cap_krw")] for r in table["rows"]
                ]
        return result

    async def launch(self, request_id, kind, request):
        config = self.configuration()
        if kind == "critical" and self.calendar:
            current = session(self.calendar, self.clock())
            if not current or not current["open"] <= self.clock() < current["close"]:
                pending = self.store.get("pending_critical", [])
                self.store.put(
                    "pending_critical",
                    list(dict.fromkeys(pending + request.get("evidence_ids", []))),
                )
                return {"accepted": False, "reason": "outside_session"}
        blocked = self.orders_blocked or self.clock() - self.status_at > 15
        identity = self.store.admit(
            request_id, kind, self.clock(), {"request": request}, blocked=blocked
        )
        if identity is None:
            return {
                "accepted": False,
                "reason": "busy" if self.store.active() else "pending_orders",
            }

        async def model(role, payload):
            if self.configuration() != config:
                raise ValueError("settings_changed")
            result = await self.capability(
                config,
                "stage",
                role=role,
                payload=payload,
                input_limit_override=request.get("input_limit_override") is True,
            )
            if self.configuration() != config:
                raise ValueError("settings_changed")
            return result["result"], result["trace"][0]

        async def news(args, frozen, deadline):
            if not self.search_ready(config, args.get("_role", "middle")):
                raise ValueError("web_search_not_connected")
            calls = [
                (s, t)
                for s in dict.fromkeys(args["symbols"])
                for t in dict.fromkeys(args["topics"])
            ]
            names = {r[0]: r[1] for r in frozen["comparison_table"]["rows"]}
            role = args.get("_role", "middle")
            if search_provider(config["models"][role]) != "agentcore":
                if self.configuration() != config:
                    raise ValueError("settings_changed")
                queries = [{"symbol": s, "name": names[s], "topic": t} for s, t in calls]
                async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
                    result = await self.capability(
                        config,
                        "native_search",
                        input_limit_override=request.get("input_limit_override") is True,
                        role=role,
                        queries=queries,
                        prior_research=public_research_view(
                            self.public_research, queries, self.clock()
                        ),
                    )
                if self.configuration() != config:
                    raise ValueError("settings_changed")
                self.public_research.append(
                    {
                        "symbols": sorted(set(args["symbols"])),
                        "collected_at": result["grounding"]["collected_at"],
                        "summary": result.get("summary", {}).get("summary", "")[:1200],
                        "urls": [s["url"] for s in result.get("sources", []) if s.get("url")][:5],
                    }
                )
                self.public_research = self.public_research[-32:]
                return result
            semaphore = asyncio.Semaphore(3)

            async def query(symbol, topic):
                async with semaphore:
                    if self.configuration() != config:
                        raise ValueError("settings_changed")
                    async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
                        result = await self.capability(
                            config,
                            "search",
                            query={"symbol": symbol, "name": names[symbol], "topic": topic},
                        )
                        return result["sources"]

            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(query(s, t)) for s, t in calls]
            groups = [task.result() for task in tasks]
            return list({s["id"]: s for group in groups for s in group}.values())

        async def run():
            pipeline = DecisionPipeline(model, self.context, news, self.store, clock=self.clock)
            await pipeline.run(
                identity,
                kind,
                request,
                config
                | {
                    "input_limit_override": request.get("input_limit_override") is True,
                    "web_search_connected": self.search_ready(config, "middle"),
                    "decision_search_connected": self.search_ready(config, "research"),
                },
            )
            # Export a completed basket before releasing admission to the next tick.
            # The collector then reports unconsumed intents as pending submissions.
            self.publish()
            self.status_at = 0

        self.task = asyncio.create_task(run())
        return {"accepted": True, "id": identity}

    async def owner_request(self, data):
        if data.get("operation") == "history_page" and set(data) == {"operation", "cursor"}:
            return self.store.layer_page(data["cursor"])
        if data.get("operation") == "layer_history" and set(data) == {"operation", "id", "role"}:
            from veyquant.analysis_records import layer_detail

            if (
                data["role"] not in {"middle", "research"}
                or not self.store.db.execute(
                    "SELECT 1 FROM analysis_records WHERE run_id=? AND role=?",
                    (data["id"], data["role"]),
                ).fetchone()
            ):
                raise ValueError("analysis_not_found")
            row = self.store.db.execute(
                "SELECT data FROM decision_runs WHERE id=?", (data["id"],)
            ).fetchone()
            if row is None:
                raise ValueError("analysis_not_found")
            return {"data": layer_detail(json.loads(row[0]), data["role"])}
        if data.get("operation") == "history" and set(data) == {"operation", "id"}:
            row = self.store.db.execute(
                "SELECT * FROM decision_runs WHERE id=?", (data["id"],)
            ).fetchone()
            if row is None:
                raise ValueError("analysis_not_found")
            return dict(row) | {"data": json.loads(row["data"])}
        if data.get("operation") == "retry_input_limit":
            if set(data) != {"operation", "id", "request_id"} or not all(
                isinstance(data[k], str) and re.fullmatch(r"[a-f0-9]{32}", data[k])
                for k in ("id", "request_id")
            ):
                raise ValueError("invalid_retry_request")
            source = self.store.db.execute(
                "SELECT r.state,r.kind,p.data FROM decision_runs r "
                "JOIN decision_previews p ON p.id=r.id WHERE r.id=?",
                (data["id"],),
            ).fetchone()
            if (
                source is None
                or source["state"] != "aborted"
                or source["kind"] not in {"scheduled", "manual", "critical"}
                or json.loads(source["data"]).get("error") != "input_budget_exceeded"
            ):
                raise ValueError("analysis_not_retryable")
            existing = self.store.db.execute(
                "SELECT r.id FROM decision_runs r JOIN decision_previews p ON p.id=r.id "
                "WHERE json_extract(p.data,'$.request.retry_of')=? "
                "AND r.state NOT IN ('skipped_busy','skipped_orders') LIMIT 1",
                (data["id"],),
            ).fetchone()
            if existing:
                return {"accepted": True, "id": existing["id"], "duplicate": True}
            previous = json.loads(source["data"]).get("request") or {}
            request = {"retry_of": data["id"], "input_limit_override": True}
            if previous.get("instruction"):
                request["instruction"] = previous["instruction"]
            return await self.launch("retry-input:" + data["request_id"], "manual", request)
        if set(data) != {"operation", "instruction", "request_id"} or data["operation"] != "manual":
            raise ValueError("invalid_manual_request")
        instruction = data["instruction"]
        if not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 3000:
            raise ValueError("invalid_manual_instruction")
        request_id = data["request_id"]
        if not isinstance(request_id, str) or len(request_id) != 32:
            raise ValueError("invalid_request_id")
        existing = self.store.db.execute(
            "SELECT id,state FROM decision_runs WHERE request_id=?",
            ("manual:" + request_id,),
        ).fetchone()
        if existing and existing["state"] not in {"skipped_busy", "skipped_orders"}:
            return {"accepted": True, "id": existing["id"], "duplicate": True}
        return await self.launch("manual:" + request_id, "manual", {"instruction": instruction})

    async def surveillance(self, signals, configuration):
        try:
            value = await self.capability(
                configuration,
                "stage",
                role="cheap",
                payload={
                    "protocol": "decision-v2",
                    "task": "surveillance",
                    "signals": surveillance_summary(signals),
                    "severity_policy": SEVERITY_POLICY,
                    "evaluation_scope": (
                        "Classify the supplied code-triggered conditions, not a trade. "
                        "Each metric has its own timestamp: book_as_of for spread, "
                        "price_as_of for price change, volume_as_of for the volume baseline. "
                        "These timestamps are not stock prices. Account, cash and company "
                        "research are evaluated by later layers; their absence here is expected."
                    ),
                    "schema": {
                        "severity": "NORMAL | WARN | CRITICAL",
                        "summary": "Korean rationale",
                    },
                },
            )
            result = value["result"]
            level = severity(result)  # Never branch on trigger_decision or another field.
            self.store.put("surveillance_failures", 0)
            self.store.put("surveillance_retry_at", 0)
            identity = self.store.evidence(
                "surveillance",
                {
                    "kind": "surveillance",
                    "severity": level,
                    "summary": result["summary"],
                    "signals": signals,
                    "symbols": sorted({s["symbol"] for s in signals if s.get("symbol")}),
                    "trace": value["trace"],
                    "as_of": self.clock(),
                    "source": "Code-triggered surveillance",
                },
                self.clock(),
            )
            if self.configuration() != configuration:
                return
            if level == "WARN":
                pending = self.store.get("pending_warn", [])
                self.store.put("pending_warn", list(dict.fromkeys(pending + [identity])))
            if level == "CRITICAL":
                await self.launch(
                    "critical:" + identity,
                    "critical",
                    {
                        "evidence_ids": [identity],
                        "symbols": list(
                            dict.fromkeys(s["symbol"] for s in signals if s.get("symbol"))
                        )[:200],
                    },
                )
        except Exception as error:
            failures = self.store.get("surveillance_failures", 0) + 1
            self.store.put("surveillance_failures", failures)
            self.store.put(
                "surveillance_retry_at", self.clock() + min(3600, 300 * 2 ** min(failures - 1, 4))
            )
            self.store.evidence(
                "surveillance_failure",
                {
                    "kind": "surveillance_failure",
                    "summary": "감시 모델의 응답을 확인하지 못했습니다.",
                    "signals": signals,
                    "trace": [getattr(error, "usage", {})],
                    "error": str(error)
                    if isinstance(error, ValueError)
                    else "provider_call_failed",
                    "as_of": self.clock(),
                },
                self.clock(),
                uuid.uuid4().hex,
            )

    async def dart_loop(self):
        while True:
            try:
                config = self.configuration()
                if "dart" not in config.get("provider_credentials", {}):
                    self.source_state["dart"] = "not_connected"
                else:
                    first = await self.capability(config, "disclosures", page=1)
                    pages = first["pages"]
                    if pages > 100:
                        raise ValueError("disclosure_page_budget")
                    events = first["events"]
                    for page in range(2, pages + 1):
                        result = await self.capability(config, "disclosures", page=page)
                        if result["pages"] != pages:
                            raise ValueError("disclosure_list_changed")
                        events.extend(result["events"])
                    for event in events:
                        if self.configuration() != config:
                            raise ValueError("settings_changed")
                        self.store.evidence(event["kind"], event, self.clock(), event["id"])
                        if event["kind"] == "dart_important":
                            self.store.signal_once(event["id"], self.clock(), event)
                    self.source_state["dart"] = "connected"
            except Exception:
                self.source_state["dart"] = "unavailable"
            await asyncio.sleep(60)

    async def krx_loop(self):
        previous = None
        while True:
            try:
                config = self.configuration()
                credential = config.get("provider_credentials", {}).get("krx")
                if not credential:
                    self.krx_cache = previous = None
                    self.source_state["krx"] = "not_connected"
                else:
                    today = datetime.fromtimestamp(self.clock(), ZoneInfo("Asia/Seoul"))
                    period = today.strftime("%Y%m%d") + (
                        ":published" if today.hour >= 8 else ":early"
                    )
                    marker = (credential, period)
                    if marker != previous:
                        result = await self.capability(config, "krx_daily")
                        if self.configuration() != config:
                            raise ValueError("settings_changed")
                        self.krx_cache, previous = result, marker
                    self.source_state["krx"] = "connected"
                    self.source_state["krx_as_of"] = self.krx_cache["as_of"]
            except Exception:
                self.source_state["krx"] = "unavailable"
            await asyncio.sleep(60 if previous is None else 300)

    def publish(self):
        history_page = self.store.layer_page(limit=90)
        runs = self.store.history(compact=True)
        summaries = []
        proposals = []
        for run in runs:
            data = run["data"]
            summaries.append(
                {k: v for k, v in run.items() if k != "data"}
                | {
                    "brief": brief_preview(data.get("brief")),
                    "decision": decision_preview(data.get("decision")),
                    "error": data.get("error"),
                    "trace": [
                        {k: v for k, v in t.items() if k != "decision"}
                        for t in (data.get("trace") or [])
                    ],
                    "instruction": (data.get("request") or {}).get("instruction"),
                    "retry_of": (data.get("request") or {}).get("retry_of"),
                    "input_limit_override": (data.get("request") or {}).get("input_limit_override")
                    is True,
                    "legacy_symbol": (data.get("legacy_report") or {}).get("symbol"),
                    "legacy_name": (data.get("legacy_report") or {}).get("name"),
                    "legacy_summary": (data.get("legacy_report") or {}).get("summary"),
                }
            )
            decision = data.get("decision") or {}
            if (
                run["state"] == "complete"
                and decision.get("action") == "SUBMIT"
                and run["finished"] is not None
                and 0 <= self.clock() - run["finished"] <= 90
            ):
                proposals.append(
                    {
                        "id": run["id"],
                        "created_at": run["finished"],
                        "settings_revision": data["settings_revision"],
                        "intents": decision["intents"],
                        "context": data.get("final_context"),
                        "rationale": decision["summary"],
                    }
                )
        sources = dict(self.source_state)
        try:
            provider = search_provider(self.configuration()["models"]["middle"])
        except Exception:
            provider = "agentcore"
        sources["web_provider"] = PROVIDERS.get(provider, "Amazon Bedrock AgentCore")
        try:
            decision_provider = search_provider(self.configuration()["models"]["research"])
            sources["decision_web_provider"] = PROVIDERS.get(
                decision_provider, "Amazon Bedrock AgentCore"
            )
        except Exception:
            sources["decision_web_provider"] = "연결 확인 필요"
        day = (
            datetime.fromtimestamp(self.clock(), ZoneInfo("Asia/Seoul"))
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        atomic_json(
            self.report_path,
            {
                "protocol": "decision-v2",
                "memory_book": self.store.memory_book(),
                "updated_at": self.clock(),
                "state": self.state,
                "active": bool(self.store.active()),
                "runs": summaries,
                "history_cursor": history_page["next_cursor"],
                "layers": [
                    record
                    | {
                        "brief": brief_preview(record.get("brief")),
                        "decision": decision_preview(record.get("decision")),
                    }
                    for record in history_page["records"]
                ],
                "proposals": proposals,
                "reports": [],
                "sources": sources,
                "usage": self.store.usage_summary(day),
                "surveillance_retry_at": self.store.get("surveillance_retry_at", 0),
                "schedule": self.next_sessions,
                "orders_blocked": self.orders_blocked,
                "pending_critical": len(self.store.get("pending_critical", [])),
                "surveillance": [
                    e
                    for e in self.store.recent(self.clock(), 30)
                    if e.get("kind") == "surveillance"
                ],
            },
        )

    async def tick(self):
        now = self.clock()
        try:
            config = self.configuration()
            status = await exchange(SOCKET, {"operation": "status", "request": {}}, timeout=10)
            self.calendar, self.orders_blocked = status["calendar"], status["blocked"]
            self.status_at = self.clock()
            self.next_sessions = schedule(self.calendar, now)
            regular = session(self.calendar, now)
            market_open = bool(regular and regular["open"] <= now < regular["close"])
            market = bounded_json(self.market_path, 2 * 1024 * 1024)
            if not 0 <= now - market["updated_at"] <= 15:
                market = {"metrics": [], "last_realtime_at": market.get("last_realtime_at", 0)}
            observe(self.store, market, self.calendar, now, market.get("warning_events", []))
            if (
                market_open
                and (not self.monitor_task or self.monitor_task.done())
                and now >= self.store.get("surveillance_retry_at", 0)
            ):
                signals, deferred = relevant_signals(self.store.take_signals(now), status, now)
                for signal in deferred:
                    self.store.evidence(
                        "deferred_signal",
                        {
                            **signal,
                            "kind": "deferred_signal",
                            "source": "Code relevance check",
                            "as_of": now,
                            "deferred_reason": "no_exposure_and_not_eligible",
                        },
                        now,
                        "deferred:" + signal["id"],
                    )
                if signals:
                    self.monitor_task = asyncio.create_task(self.surveillance(signals, config))
            for due in self.next_sessions:
                if 0 <= now - due["at"] <= 120:
                    await self.launch("scheduled:" + due["id"], "scheduled", {"schedule": due})
            pending = self.store.get("pending_critical", [])
            if market_open and pending and not self.store.active() and not self.orders_blocked:
                symbols = list(
                    dict.fromkeys(
                        signal["symbol"]
                        for e in self.store.lookup(pending)
                        for signal in e.get("signals", [])
                        if signal.get("symbol")
                    )
                )[:200]
                result = await self.launch(
                    "critical-merged:" + hashlib.sha256(encoded(pending).encode()).hexdigest(),
                    "critical",
                    {"evidence_ids": pending, "symbols": symbols},
                )
                if result["accepted"]:
                    self.store.put("pending_critical", [])
            self.state = (
                "analyzing"
                if self.store.active()
                else "orders_pending"
                if self.orders_blocked
                else "outside_session"
                if not market_open
                else "observing"
            )
        except Exception:
            self.state = "waiting_for_context"
        self.publish()

    async def run(self):
        self.store.recover(self.clock())

        async def loop():
            while True:
                await self.tick()
                await asyncio.sleep(5)

        async with asyncio.TaskGroup() as group:
            group.create_task(loop())
            group.create_task(self.search_loop())
            group.create_task(self.dart_loop())
            group.create_task(self.krx_loop())
            group.create_task(
                serve(
                    API_SOCKET,
                    pwd.getpwnam("veyweb").pw_uid,
                    self.owner_request,
                    maximum=16 * 1024 * 1024,
                )
            )

    def search_ready(self, config, role):
        provider = search_provider(config["models"][role])
        return (
            self.source_state.get("agentcore") == "connected"
            if provider == "agentcore"
            else provider in config.get("provider_credentials", {})
        )

    async def search_loop(self):
        while True:
            try:
                config = self.configuration()
                if any(
                    search_provider(config["models"][r]) == "agentcore"
                    for r in ("middle", "research")
                ):
                    result = await self.capability({}, "search_status")
                    self.source_state["agentcore"] = (
                        "connected" if result.get("connected") else "unavailable"
                    )
                self.source_state["web"] = (
                    "connected" if self.search_ready(config, "middle") else "not_connected"
                )
                self.source_state["decision_web"] = (
                    "connected" if self.search_ready(config, "research") else "not_connected"
                )
            except Exception:
                self.source_state["web"] = self.source_state["decision_web"] = "unavailable"
            await asyncio.sleep(300)


def main():
    import argparse
    import fcntl
    import logging

    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser()
    for name in ("db", "function-arn", "control", "market", "reports"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    with open(args.db + ".lock", "a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = DecisionStore(args.db)
        store.import_legacy(Path(args.db).parent / "jobs.sqlite3")
        try:
            asyncio.run(
                Runtime(store, args.function_arn, args.control, args.market, args.reports).run()
            )
        finally:
            store.db.close()


if __name__ == "__main__":
    main()
