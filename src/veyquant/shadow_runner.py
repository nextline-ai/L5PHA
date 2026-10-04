"""Trusted local dispatcher: public exports to a separate Lambda, never broker data."""

import argparse
import fcntl
import hashlib
import json
import logging
import sqlite3
import time
from decimal import Decimal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from veyquant.shadow_contract import (
    SYMBOLS,
    atomic_json,
    bounded_json,
    finite_time,
    read_control,
    read_market,
    read_model_configuration,
)
from veyquant.shadow_inference import (
    VERSION,
    model_selection,
    models_ready,
    output_limit,
    reasoning_selection,
    sentence,
    strategy_selection,
    validate_daily_bars,
)

DAILY_LIMIT = 12
COOLDOWN = 3600


def independent_risk(quote, now):
    reasons = [
        "independent_execution_check_required",
    ]
    if not 0 <= now - quote["as_of"] <= 180:
        reasons.insert(0, "stale_quote")
    return {"accepted": False, "order_enabled": False, "reasons": reasons}


def validated_report(raw, event, now):
    selected = model_selection(event.get("models"))
    reasoning = reasoning_selection(selected, event.get("reasoning"))
    if not isinstance(raw, dict) or raw.get("event_id") != event["event_id"]:
        raise ValueError("report_event_mismatch")
    if raw.get("version") != VERSION or raw.get("symbol") != event["quote"]["symbol"]:
        raise ValueError("report_scope_mismatch")
    if raw.get("quote") != event["quote"]:
        raise ValueError("report_quote_mismatch")
    if raw.get("outcome") not in {
        "watch",
        "insufficient_evidence",
        "no_action",
        "error",
        "buy",
        "sell",
    }:
        raise ValueError("invalid_report_outcome")
    stages = raw.get("stages")
    if not isinstance(stages, list) or not 1 <= len(stages) <= 4:
        raise ValueError("invalid_stages")
    safe_stages = []
    roles = [s.get("role") for s in stages if isinstance(s, dict)]
    if roles != ["cheap", "middle", "research", "research"][: len(stages)]:
        raise ValueError("invalid_stage_order")
    for s in stages:
        role = s["role"]
        if s.get("model") != selected[role] or s.get("max_output_tokens") != output_limit(
            role, selected[role], reasoning[role]
        ):
            raise ValueError("invalid_model_identity")
        if s.get("reasoning", None if "reasoning" in event else reasoning[role]) != reasoning[role]:
            raise ValueError("invalid_reasoning_identity")
        safe = {"role": role, "model": s["model"], "status": s.get("status")}
        safe["reasoning"] = reasoning[role]
        if safe["status"] not in {"received", "uncertain"}:
            raise ValueError("invalid_stage_status")
        for k in ("input_tokens", "output_tokens"):
            value = s.get(k)
            if value is not None and (type(value) is not int or not 0 <= value <= 100000):
                raise ValueError("invalid_token_usage")
            safe[k] = value
        safe["decision_state"] = "unverified" if raw["outcome"] == "error" else "not_recorded"
        if "decision" in s:
            decision = s["decision"]
            allowed = (
                {"hold", "escalate"}
                if role != "research"
                else {"read_evidence", "watch", "insufficient_evidence", "no_action", "buy", "sell"}
            )
            if (
                not isinstance(decision, dict)
                or decision.get("action") not in allowed
                or safe["status"] != "received"
            ):
                raise ValueError("invalid_stage_decision")
            safe["decision_state"] = "recorded"
            safe["decision"] = {
                "action": decision["action"],
                "summary": sentence(decision.get("summary")),
            }
            for key in ("counterargument", "uncertainty"):
                if key in decision:
                    safe["decision"][key] = sentence(decision[key])
        safe_stages.append(safe)
    evidence = raw.get("evidence")
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 4:
        raise ValueError("invalid_evidence")
    # Return source metadata only; evidence content is retained in the original job payload.
    safe_evidence = []
    for e in evidence:
        if not isinstance(e, dict) or e.get("id") not in {
            "quote",
            "history",
            "coverage",
            "daily_bars",
        }:
            raise ValueError("invalid_evidence")
        if not finite_time(e.get("as_of")) or e["as_of"] > now + 5:
            raise ValueError("invalid_evidence_time")
        if e.get("status") not in {"available", "missing"}:
            raise ValueError("invalid_evidence_status")
        sources = {
            "quote": "토스 시각이 확인된 현재가·체결 시세",
            "history": "로컬 시세 관찰 이력",
            "coverage": "자료 수집 범위 선언",
            "daily_bars": "토스 완료 일봉 · 미수정 OHLCV",
        }
        safe_evidence.append(
            {
                "id": e["id"],
                "source": sources[e["id"]],
                "as_of": e["as_of"],
                "status": e.get("status"),
            }
        )
    proposal = None
    if raw["outcome"] in {"buy", "sell"}:
        validate_daily_bars(event.get("daily_bars"), now)
        if event["daily_bars"].get("symbol", "005930") != event["quote"]["symbol"]:
            raise ValueError("trade_bar_symbol_mismatch")
        if any(s.get("decision", {}).get("action") != "escalate" for s in stages[:2]):
            raise ValueError("trade_gate_not_passed")
        if (
            len(stages) < 3
            or roles[-1] != "research"
            or any(s["status"] != "received" for s in stages)
        ):
            raise ValueError("incomplete_trade_review")
        if not any(
            e["id"] == "daily_bars" and e.get("data") == event["daily_bars"]["bars"]
            for e in evidence
        ):
            raise ValueError("trade_evidence_mismatch")
        if stages[-1].get("decision", {}).get("action") != raw["outcome"]:
            raise ValueError("trade_decision_mismatch")
        proposal = {"side": raw["outcome"].upper()}
    return {
        "proposal": proposal,
        "event_id": event["event_id"],
        "symbol": event["quote"]["symbol"],
        "name": event.get("instrument", {}).get(
            "name", SYMBOLS.get(event["quote"]["symbol"], ("KRW", event["quote"]["symbol"]))[1]
        ),
        "quote": event["quote"],
        "created_at": now,
        "settings_revision": event.get("settings_revision", 0),
        "selected_models": selected,
        "strategy_preset": strategy_selection(event.get("strategy"))["preset"],
        "outcome": raw["outcome"],
        **{k: sentence(raw.get(k)) for k in ("summary", "counterargument", "uncertainty")},
        "stages": safe_stages,
        "evidence": safe_evidence,
        "risk": independent_risk(event["quote"], now),
    }


class ShadowStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS samples(symbol TEXT, minute INTEGER, price TEXT,
                as_of REAL, PRIMARY KEY(symbol,minute));
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, symbol TEXT, day INTEGER,
                created_at REAL, status TEXT, payload TEXT NOT NULL, report TEXT);
            CREATE INDEX IF NOT EXISTS jobs_day ON jobs(day);
            CREATE TABLE IF NOT EXISTS failures(id TEXT PRIMARY KEY, code TEXT NOT NULL);
        """)

    def sample(self, q, now):
        self.db.execute(
            "INSERT OR IGNORE INTO samples VALUES (?,?,?,?)",
            (q["symbol"], int(q["as_of"] // 60), q["price"], q["as_of"]),
        )
        self.db.execute("DELETE FROM samples WHERE as_of<?", (now - 86400,))

    def reserve(self, q, now, configuration=None):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            last = self.db.execute(
                "SELECT * FROM jobs WHERE symbol=? ORDER BY created_at DESC LIMIT 1", (q["symbol"],)
            ).fetchone()
            if last:
                age = now - last["created_at"]
                old_price = Decimal(json.loads(last["payload"])["quote"]["price"])
                movement = abs(Decimal(q["price"]) / old_price - 1)
                if age < COOLDOWN or (age < 21600 and movement < Decimal("0.005")):
                    return None
            if self.used(now) >= DAILY_LIMIT:
                return None
            history = [
                dict(r)
                for r in self.db.execute(
                    "SELECT price,as_of FROM (SELECT price,as_of FROM samples "
                    "WHERE symbol=? AND as_of<? ORDER BY as_of DESC LIMIT 24) ORDER BY as_of",
                    (q["symbol"], q["as_of"]),
                )
            ]
            event_id = hashlib.sha256(
                f"{VERSION}:{q['symbol']}:{int(now // COOLDOWN)}".encode()
            ).hexdigest()
            event = {"version": VERSION, "event_id": event_id, "quote": q, "history": history}
            if configuration is not None:
                event |= configuration
            row = self.db.execute(
                "INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?,?,NULL)",
                (event_id, q["symbol"], int(now // 86400), now, "reserved", json.dumps(event)),
            )
            return event if row.rowcount else None
        finally:
            self.db.execute("COMMIT")

    def used(self, now):
        return self.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE day=?", (int(now // 86400),)
        ).fetchone()[0]

    def complete(self, event_id, status, report=None):
        self.db.execute(
            "UPDATE jobs SET status=?,report=? WHERE id=?",
            (status, json.dumps(report, ensure_ascii=False) if report else None, event_id),
        )

    def failed(self, event_id, error):
        code = "dispatch_or_validation_error"
        if isinstance(error, ClientError):
            candidate = error.response.get("Error", {}).get("Code")
            code = (
                candidate
                if candidate
                in {
                    "AccessDeniedException",
                    "ThrottlingException",
                    "TooManyRequestsException",
                    "ResourceNotFoundException",
                    "ServiceException",
                    "ValidationException",
                }
                else "provider_error"
            )
        # Never retain exception messages or arbitrary provider fields.
        self.db.execute("INSERT OR REPLACE INTO failures VALUES(?,?)", (event_id, code))
        self.complete(event_id, "uncertain")

    def export(self, path, now, state):
        reports = [
            json.loads(r[0])
            for r in self.db.execute(
                "SELECT report FROM jobs WHERE report IS NOT NULL ORDER BY created_at DESC LIMIT 10"
            )
        ]
        pending = self.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('reserved','uncertain')"
        ).fetchone()[0]
        view = {
            "updated_at": now,
            "state": state,
            "daily_jobs": self.used(now),
            "daily_limit": DAILY_LIMIT,
            "max_calls_per_job": 4,
            "uncertain_jobs": pending,
            "reports": reports,
        }
        # Longer per-stage summaries must not make the entire management view unreadable.
        # Full reports remain in SQLite; the bounded export favors the newest records.
        while reports and len(json.dumps(view, ensure_ascii=False).encode()) > 131072:
            reports.pop()
        atomic_json(path, view)


def invoke(client, function_arn, event):
    response = client.invoke(
        FunctionName=function_arn,
        InvocationType="RequestResponse",
        LogType="None",
        Payload=json.dumps(event).encode(),
    )
    with response["Payload"] as payload:
        raw = payload.read(131073)
    if response.get("FunctionError") or response.get("StatusCode") != 200 or len(raw) > 131072:
        raise ValueError("analysis_function_failed")
    return json.loads(raw)


def tick(store, client, function_arn, market_path, control_path, report_path, clock=time.time):
    now = clock()
    if not read_control(control_path, now):
        store.export(report_path, now, "paused")
        return
    try:
        quotes = read_market(market_path, now)
        configuration = read_model_configuration(control_path, now)
    except (OSError, ValueError, KeyError, TypeError):
        store.export(report_path, now, "waiting_for_market")
        return
    if not models_ready(configuration["models"], configuration.get("provider_credentials")):
        store.export(report_path, now, "model_setup_required")
        return
    for q in quotes:
        now = clock()
        if not read_control(control_path, now):
            break
        if not 0 <= now - q["as_of"] <= 180:
            continue
        store.sample(q, now)
        event_configuration = dict(configuration)
        try:
            market = bounded_json(market_path, 1048576)
            instrument = market.get("instruments", {}).get(q["symbol"])
            if instrument:
                event_configuration["instrument"] = {
                    k: instrument[k] for k in ("symbol", "name", "market")
                }
            all_bars = market.get("daily_bars", {})
            bars = all_bars.get(q["symbol"])
            if bars is None and q["symbol"] == "005930" and "bars" in all_bars:
                bars = all_bars  # Read-only migration of the previous single-stock export.
            if bars is not None:
                if bars.get("symbol", "005930") != q["symbol"]:
                    raise ValueError("daily_bar_symbol_mismatch")
                event_configuration["daily_bars"] = validate_daily_bars(bars, now)
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Observation can continue, but cannot produce a trade proposal.
        event = store.reserve(q, now, event_configuration)
        if not event:
            continue
        store.export(report_path, now, "analyzing")
        try:
            if read_model_configuration(control_path, clock()) != configuration:
                store.complete(event["event_id"], "cancelled")
                continue
            result = invoke(client, function_arn, event)
            finished = clock()
            if not read_control(control_path, finished):
                store.complete(event["event_id"], "cancelled")
                continue
            if read_model_configuration(control_path, finished) != configuration:
                store.complete(event["event_id"], "cancelled")
                continue
            report = validated_report(result, event, finished)
            store.complete(event["event_id"], "complete", report)
        except Exception as error:
            # No retry: the provider may have billed an invocation whose response was lost.
            store.failed(event["event_id"], error)
        # One invocation chain per pass keeps the heartbeat and owner controls
        # responsive across many candidates; the daily budget is global.
        break
    active = read_control(control_path, clock())
    state = (
        "paused"
        if not active
        else "budget_reached"
        if store.used(clock()) >= DAILY_LIMIT
        else "observing"
        if quotes
        else "waiting_for_market"
    )
    store.export(report_path, clock(), state)


def read_view(path, now):
    empty = {"state": "unavailable", "reports": [], "daily_jobs": 0, "daily_limit": DAILY_LIMIT}
    if not path:
        return empty
    try:
        data = bounded_json(path, 2 * 1024 * 1024)
        if data.get("protocol") == "decision-v2":
            if not 0 <= now - data["updated_at"] <= 30 or not isinstance(data.get("runs"), list):
                return empty
            if isinstance(data.get("layers"), list):
                # Run metadata coordinates retries; layer outputs have their own records.
                data["runs"] = [
                    {k: r[k] for k in ("id", "kind", "state", "started", "retry_of") if k in r}
                    for r in data["runs"]
                ]
            return {
                k: data[k]
                for k in (
                    "protocol",
                    "updated_at",
                    "state",
                    "active",
                    "runs",
                    "sources",
                    "schedule",
                    "orders_blocked",
                    "pending_critical",
                    "surveillance",
                )
            } | {
                k: data.get(k)
                for k in (
                    "memory_book",
                    "layers",
                    "usage",
                    "surveillance_retry_at",
                    "history_cursor",
                )
            }
        if not finite_time(data["updated_at"]) or not 0 <= now - data["updated_at"] <= 360:
            return empty
        if data["state"] not in {
            "paused",
            "analyzing",
            "budget_reached",
            "observing",
            "waiting_for_market",
            "model_setup_required",
        }:
            return empty
        if type(data["daily_jobs"]) is not int or not 0 <= data["daily_jobs"] <= DAILY_LIMIT:
            return empty
        if not isinstance(data["reports"], list) or len(data["reports"]) > 10:
            return empty
        # The export is writable only by the trusted dispatcher. Web rendering uses textContent.
        return {
            k: data[k]
            for k in (
                "state",
                "reports",
                "daily_jobs",
                "daily_limit",
                "max_calls_per_job",
                "uncertain_jobs",
            )
        }
    except (OSError, ValueError, KeyError, TypeError):
        return empty


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser()
    for name in ("db", "market", "control", "reports", "function-arn"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    # Prevent two dispatch processes from overlapping even across service restarts.
    lease = open(args.db + ".lock", "a")
    try:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lease.close()
        return 0
    store = ShadowStore(args.db)
    client = boto3.Session(region_name="ap-southeast-2").client(
        "lambda",
        config=Config(
            connect_timeout=5,
            read_timeout=270,
            retries={"total_max_attempts": 1, "mode": "standard"},
        ),
    )
    try:
        while True:
            tick(store, client, args.function_arn, args.market, args.control, args.reports)
            if args.once:
                return 0
            time.sleep(30)
    finally:
        store.db.close()
        lease.close()


if __name__ == "__main__":
    raise SystemExit(main())
