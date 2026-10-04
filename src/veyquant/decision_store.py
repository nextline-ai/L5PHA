"""Durable analysis admission, evidence, and edge-triggered surveillance state."""

import copy
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


PREVIEW_FIELDS = (
    "brief",
    "decision",
    "error",
    "trace",
    "request",
    "settings_revision",
    "final_context",
    "legacy_report",
)


def preview_sql(column):
    # Only fixed identifiers are passed; no caller-provided SQL or model capability.
    pairs = ",".join(f"'{key}',json_extract({column},'$.{key}')" for key in PREVIEW_FIELDS)
    return f"json_object({pairs})"


class DecisionStore:
    def __init__(self, path):
        self._usage_cache = None
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS decision_runs(
                id TEXT PRIMARY KEY, request_id TEXT UNIQUE, kind TEXT, started REAL,
                finished REAL, state TEXT, data TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS one_decision_run
                ON decision_runs((1)) WHERE state='running';
            CREATE TABLE IF NOT EXISTS decision_evidence(
                id TEXT PRIMARY KEY, at REAL, kind TEXT, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS decision_state(key TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS decision_edges(
                key TEXT PRIMARY KEY, active INTEGER NOT NULL, changed REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS decision_signals(
                id TEXT PRIMARY KEY, at REAL, ready REAL, data TEXT, consumed INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS ai_usage(
                id TEXT PRIMARY KEY, at REAL, run_id TEXT, operation TEXT, role TEXT,
                model TEXT, state TEXT, data TEXT NOT NULL);
        """)

        # Dashboards poll every five seconds. Never repeatedly parse/sort full archives
        # just to show a short preview; maintain a separate projection on each write.
        self.db.executescript(f"""
            CREATE INDEX IF NOT EXISTS decision_runs_started ON decision_runs(started DESC);
            CREATE INDEX IF NOT EXISTS decision_runs_memory ON decision_runs(finished DESC)
                WHERE state IN ('complete','historical');
            CREATE INDEX IF NOT EXISTS decision_evidence_recent ON decision_evidence(at DESC);
            CREATE INDEX IF NOT EXISTS decision_evidence_kind ON decision_evidence(kind,at DESC);
            CREATE INDEX IF NOT EXISTS ai_usage_at ON ai_usage(at);
            CREATE INDEX IF NOT EXISTS decision_signals_open ON decision_signals(at)
                WHERE consumed=0;
            CREATE TABLE IF NOT EXISTS decision_memory(
                revision INTEGER PRIMARY KEY, run_id TEXT UNIQUE, at REAL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS decision_previews(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS decision_preview_insert AFTER INSERT ON decision_runs
            BEGIN
                INSERT OR REPLACE INTO decision_previews VALUES(NEW.id,{preview_sql("NEW.data")});
            END;
            CREATE TRIGGER IF NOT EXISTS decision_preview_update
                AFTER UPDATE OF data ON decision_runs
            BEGIN
                INSERT OR REPLACE INTO decision_previews VALUES(NEW.id,{preview_sql("NEW.data")});
            END;
            CREATE TRIGGER IF NOT EXISTS decision_preview_delete AFTER DELETE ON decision_runs
            BEGIN DELETE FROM decision_previews WHERE id=OLD.id; END;
            INSERT INTO decision_previews
                SELECT id,{preview_sql("data")} FROM decision_runs
                WHERE id NOT IN (SELECT id FROM decision_previews);
        """)

        from veyquant.analysis_records import initialize

        initialize(self.db)

    def recover(self, now):
        from veyquant.analysis_records import sync_run

        active = self.db.execute("SELECT id FROM decision_runs WHERE state='running'").fetchall()
        # A crashed inference may have been billed; never replay it or its trade intents.
        self.db.execute(
            "UPDATE decision_runs SET state='interrupted',finished=? WHERE state='running'", (now,)
        )
        for row in active:
            sync_run(self.db, row[0])

    def usage_start(
        self, now, run_id, operation, role, model, *, input_context=None, model_payload=None
    ):
        self._usage_cache = None
        identity = uuid.uuid4().hex
        data = {}
        if input_context is not None:
            data["input_context"] = input_context
        if model_payload is not None:
            # This database is private. Keep the actual submitted prompt for owner audits,
            # separate from provider credentials and from the public counter projection.
            data["model_payload"] = model_payload
        self.db.execute(
            "INSERT INTO ai_usage VALUES(?,?,?,?,?,?,?,?)",
            (identity, now, run_id, operation, role, model, "uncertain", encoded(data)),
        )
        return identity

    def record_verification(self, run):
        """Archive an isolated operator rehearsal without executable intent or strategy memory."""
        if run["state"] not in {"complete", "aborted"} or run["finished"] is None:
            raise ValueError("unfinished_verification")
        self.db.execute(
            "INSERT OR IGNORE INTO decision_runs VALUES(?,?,?,?,?,?,?)",
            (
                run["id"],
                "verification:" + run["id"],
                "verification",
                run["started"],
                run["finished"],
                "verified" if run["state"] == "complete" else "verification_failed",
                encoded(run["data"] | {"verification_only": True}),
            ),
        )

        from veyquant.analysis_records import sync_run

        sync_run(self.db, run["id"])

    def usage_finish(self, identity, trace, error=None):
        self._usage_cache = None
        row = self.db.execute("SELECT data FROM ai_usage WHERE id=?", (identity,)).fetchone()
        data = json.loads(row[0]) if row else {}
        data.update(
            {
                k: v
                for k, v in trace.items()
                if k
                in {
                    "input_tokens",
                    "output_tokens",
                    "cached_input_tokens",
                    "cache_write_input_tokens",
                    "cache_policy",
                    "task",
                    "reasoning",
                    "search_queries",
                    "search_queries_known",
                    "search_tool_calls",
                    "web_search_actions",
                    "web_open_actions",
                    "web_find_actions",
                    "web_other_actions",
                    "provider",
                    "status",
                    "input_context",
                    "provider_called",
                }
            }
        )
        if error:
            data["error"] = error
        state = (
            "blocked_input"
            if data.get("provider_called") is False
            else "failed"
            if error
            else "received"
        )
        self.db.execute(
            "UPDATE ai_usage SET state=?,data=? WHERE id=?", (state, encoded(data), identity)
        )

    def usage_summary(self, since):
        cached = self._usage_cache
        if cached and cached[0] == since and 0 <= time.monotonic() - cached[1] < 30:
            return copy.deepcopy(cached[2])
        groups = {}
        for row in self.db.execute(
            "SELECT role,model,state,operation,json_remove(data,'$.model_payload') AS data "
            "FROM ai_usage WHERE at>=?",
            (since,),
        ):
            key = (row["role"], row["model"])
            group = groups.setdefault(
                key,
                {
                    "role": key[0],
                    "model": key[1],
                    "attempts": 0,
                    "requests": 0,
                    "blocked_before_provider": 0,
                    "failed": 0,
                    "unknown_usage": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cached_input_tokens": 0,
                    "cache_write_input_tokens": 0,
                    "cache_write_usage_unknown": 0,
                    "search_tool_calls": 0,
                    "web_search_actions": 0,
                    "web_open_actions": 0,
                    "web_find_actions": 0,
                    "web_other_actions": 0,
                    "search_action_usage_unknown": 0,
                    "search_queries": 0,
                    "visible_input_bytes": 0,
                    "max_visible_input_bytes": 0,
                    "input_context_unknown": 0,
                    "input_context_partial": 0,
                    "input_fields_bytes": {},
                },
            )
            data = json.loads(row["data"])
            blocked = data.get("provider_called") is False
            group["requests"] += 1
            group["attempts"] += not blocked
            group["blocked_before_provider"] += blocked
            # An in-flight/transport-unknown request is not a confirmed failure.
            group["failed"] += row["state"] == "failed"
            group["unknown_usage"] += not blocked and type(data.get("input_tokens")) is not int
            group["cache_write_usage_unknown"] += (
                not blocked and type(data.get("cache_write_input_tokens")) is not int
            )
            group["search_action_usage_unknown"] += (
                not blocked
                and row["operation"] == "native_search"
                and type(data.get("web_search_actions")) is not int
            )
            for field in (
                "web_search_actions",
                "web_open_actions",
                "web_find_actions",
                "web_other_actions",
                "input_tokens",
                "output_tokens",
                "cached_input_tokens",
                "cache_write_input_tokens",
                "search_queries",
                "search_tool_calls",
            ):
                if type(data.get(field)) is int:
                    group[field] += data[field]
            context = data.get("input_context") or {}
            measured = context.get("total_bytes")
            if type(measured) is int and measured >= 0:
                group["visible_input_bytes"] += measured
                group["max_visible_input_bytes"] = max(group["max_visible_input_bytes"], measured)
                group["input_context_partial"] += context.get("scope") == (
                    "submitted_payload_excludes_system_schema_and_provider_internal_context"
                )
                for field, detail in context.get("fields", {}).items():
                    size = detail.get("bytes")
                    if type(size) is int and size >= 0:
                        counters = group["input_fields_bytes"]
                        counters[field] = counters.get(field, 0) + size
            else:
                group["input_context_unknown"] += 1
        result = {"since": since, "groups": list(groups.values())}
        self._usage_cache = (since, time.monotonic(), copy.deepcopy(result))
        return result

    def import_legacy(self, path):
        """Keep old analysis visible, never migrate its proposals as executable decisions."""
        if self.get("legacy_imported") or not Path(path).exists():
            return
        old = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            for identity, at, report in old.execute(
                "SELECT id,created_at,report FROM jobs WHERE report IS NOT NULL"
            ):
                original = json.loads(report)
                self.db.execute(
                    "INSERT OR IGNORE INTO decision_runs VALUES(?,?,?,?,?,?,?)",
                    (
                        hashlib.sha256(("legacy:" + identity).encode()).hexdigest()[:32],
                        "legacy:" + identity,
                        "legacy",
                        at,
                        at,
                        "historical",
                        encoded({"legacy_report": original, "trace": original.get("stages", [])}),
                    ),
                )
                from veyquant.analysis_records import sync_run

                sync_run(self.db, hashlib.sha256(("legacy:" + identity).encode()).hexdigest()[:32])
            self.put("legacy_imported", True)
        finally:
            old.close()

    def get(self, key, default=None):
        row = self.db.execute("SELECT data FROM decision_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO decision_state VALUES(?,?)", (key, encoded(value)))

    def evidence(self, kind, value, now, identity=None):
        identity = identity or hashlib.sha256(encoded([kind, value]).encode()).hexdigest()
        self.db.execute(
            "INSERT OR IGNORE INTO decision_evidence VALUES(?,?,?,?)",
            (identity, now, kind, encoded(value | {"id": identity})),
        )
        if kind in {"surveillance", "surveillance_failure"}:
            from veyquant.analysis_records import sync_surveillance

            sync_surveillance(self.db, identity)
        return identity

    def recent(self, now, limit=500):
        return [
            json.loads(r[0])
            for r in self.db.execute(
                "SELECT data FROM decision_evidence WHERE at>=? ORDER BY at DESC LIMIT ?",
                (now - 7 * 86400, limit),
            )
        ]

    def memory(self, limit=12):
        result = []
        for row in self.db.execute(
            "SELECT r.id,r.finished,p.data FROM decision_runs r "
            "JOIN decision_previews p ON p.id=r.id WHERE r.state IN ('complete','historical') "
            "ORDER BY r.finished DESC LIMIT ?",
            (limit,),
        ):
            data = json.loads(row["data"])
            result.append(
                {
                    "id": row["id"],
                    "at": row["finished"],
                    "decision": data.get("decision"),
                    "brief": data.get("brief"),
                    "legacy_analysis": data.get("legacy_report"),
                }
            )
        return result

    def lookup(self, identities):
        result = []
        for identity in dict.fromkeys(identities):
            row = self.db.execute(
                "SELECT data FROM decision_evidence WHERE id=?", (identity,)
            ).fetchone()
            if row:
                result.append(json.loads(row[0]))
        return result

    def mandatory(self, now, symbols):
        result = []
        for row in self.db.execute(
            "SELECT data FROM decision_evidence WHERE kind='dart_important' AND at>=? "
            "ORDER BY at DESC",
            (now - 7 * 86400,),
        ):
            event = json.loads(row[0])
            if not event.get("symbol") or event["symbol"] in symbols:
                result.append(event)
                if len(result) > 1000:
                    raise ValueError("mandatory_evidence_exceeds_budget")
        return result

    def active(self):
        return self.db.execute("SELECT id FROM decision_runs WHERE state='running'").fetchone()

    def admit(self, request_id, kind, now, data, blocked=False):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.db.execute(
                "SELECT 1 FROM decision_runs WHERE request_id=?", (request_id,)
            ).fetchone():
                return None
            busy = bool(self.active())
            if kind == "critical" and (busy or blocked):
                pending = self.get("pending_critical", [])
                self.put("pending_critical", list(dict.fromkeys(pending + data["evidence_ids"])))
                return None
            identity = uuid.uuid4().hex
            state = "skipped_busy" if busy else "skipped_orders" if blocked else "running"
            self.db.execute(
                "INSERT INTO decision_runs VALUES(?,?,?,?,?,?,?)",
                (
                    identity,
                    request_id,
                    kind,
                    now,
                    None if state == "running" else now,
                    state,
                    encoded(data),
                ),
            )
            from veyquant.analysis_records import sync_run

            sync_run(self.db, identity, data)
            return identity if state == "running" else None
        finally:
            self.db.execute("COMMIT")

    def progress(self, identity, data):
        from veyquant.analysis_records import sync_run

        self.db.execute(
            "UPDATE decision_runs SET data=? WHERE id=? AND state='running'",
            (encoded(data), identity),
        )
        sync_run(self.db, identity, data)

    def finish(self, identity, state, data, now):
        from veyquant.analysis_records import sync_run
        from veyquant.memory_book import commit_book

        self.db.execute("BEGIN IMMEDIATE")
        try:
            if state == "complete" and data.get("memory_update"):
                row = self.db.execute(
                    "SELECT state FROM decision_runs WHERE id=?", (identity,)
                ).fetchone()
                if row is None or row[0] != "running":
                    raise ValueError("memory_book_commit_requires_active_run")
                data["memory_book_after"] = commit_book(
                    self.db, identity, data["memory_update"], data["decision"], now
                )
            self.db.execute(
                "UPDATE decision_runs SET state=?,data=?,finished=? WHERE id=?",
                (state, encoded(data), now, identity),
            )
            sync_run(self.db, identity, data)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            data.pop("memory_book_after", None)
            raise

    def memory_book(self):
        from veyquant.memory_book import load_book

        return load_book(self.db)

    def layer_history(self, limit=90):
        return [
            json.loads(r[0])
            for r in self.db.execute(
                "SELECT data FROM analysis_records ORDER BY started DESC,id DESC LIMIT ?", (limit,)
            )
        ]

    def layer_page(self, cursor=None, limit=60):
        import base64
        import math

        if type(limit) is not int or not 1 <= limit <= 90:
            raise ValueError("invalid_history_page")
        params = []
        clause = ""
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 400:
                    raise ValueError
                at, identity = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if type(at) not in (int, float) or not math.isfinite(at):
                    raise ValueError
                if not isinstance(identity, str) or not 1 <= len(identity) <= 128:
                    raise ValueError
            except (ValueError, TypeError, UnicodeError) as error:
                raise ValueError("invalid_history_cursor") from error
            clause = " WHERE (started,id) < (?,?)"
            params = [at, identity]
        rows = self.db.execute(
            "SELECT started,id,data FROM analysis_records"
            + clause
            + " ORDER BY started DESC,id DESC LIMIT ?",
            [*params, limit + 1],
        ).fetchall()
        selected = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            last = selected[-1]
            next_cursor = base64.urlsafe_b64encode(
                encoded([last["started"], last["id"]]).encode()
            ).decode()
        return {"records": [json.loads(r["data"]) for r in selected], "next_cursor": next_cursor}

    def brief_interval(self, after, before):
        from veyquant.memory_book import brief_page

        return brief_page(self.db, after, before)

    def run_cursor(self, identity):
        return self.db.execute(
            "SELECT rowid FROM decision_runs WHERE id=?", (identity,)
        ).fetchone()[0]

    def history(self, limit=30, compact=False):
        query = (
            "SELECT r.id,r.request_id,r.kind,r.started,r.finished,r.state,p.data "
            "FROM decision_runs r JOIN decision_previews p ON p.id=r.id "
            "ORDER BY r.started DESC LIMIT ?"
            if compact
            else "SELECT * FROM decision_runs ORDER BY started DESC LIMIT ?"
        )
        return [dict(r) | {"data": json.loads(r["data"])} for r in self.db.execute(query, (limit,))]

    def edge(self, key, active, now, data, immediate=False, *, reset_allowed=True):
        # Unknown observations do not count as normalization and cannot re-arm an alarm.
        if active is None:
            return
        row = self.db.execute("SELECT active FROM decision_edges WHERE key=?", (key,)).fetchone()
        if row and row[0] and active is False and not reset_allowed:
            return
        if row and bool(row[0]) == active:
            return
        self.db.execute(
            "INSERT OR REPLACE INTO decision_edges VALUES(?,?,?)", (key, int(active), now)
        )
        if not active:
            self.db.execute(
                "UPDATE decision_signals SET consumed=1 WHERE consumed=0 AND at!=ready AND "
                "(json_extract(data,'$.edge_key')=? OR "
                "json_extract(data,'$.condition')||':'||json_extract(data,'$.symbol')=?)",
                (key, key),
            )
        if active:
            identity = uuid.uuid4().hex
            self.db.execute(
                "INSERT INTO decision_signals(id,at,ready,data) VALUES(?,?,?,?)",
                (
                    identity,
                    now,
                    now if immediate else now + 300,
                    encoded(data | {"id": identity, "at": now, "edge_key": key}),
                ),
            )

    def signal_once(self, key, now, data):
        self.edge(key, True, now, data, immediate=True)

    def take_signals(self, now):
        rows = self.db.execute(
            "SELECT * FROM decision_signals WHERE consumed=0 ORDER BY at"
        ).fetchall()
        if not rows or not any(r["ready"] <= now for r in rows):
            return []
        immediate = [r for r in rows if r["at"] == r["ready"]]
        ordinary = [r for r in rows if r["at"] != r["ready"]]
        # An immediate heartbeat/gap/filing must not drain an unfinished price window.
        shocks = {
            item["symbol"]
            for row in immediate
            if (item := json.loads(row["data"])).get("condition") == "price_shock"
        }
        selected = immediate + (
            ordinary
            if ordinary and ordinary[0]["ready"] <= now
            else [row for row in ordinary if json.loads(row["data"]).get("symbol") in shocks]
        )
        for row in selected:
            self.db.execute("UPDATE decision_signals SET consumed=1 WHERE id=?", (row["id"],))
        result = []
        for row in selected:
            item = json.loads(row["data"])
            # Ordinary alarms which recovered while batching must not trigger paid review.
            # Immediate filings/warnings are historical events and remain deliverable.
            if row["at"] != row["ready"] or item.get("condition") in {
                "realtime_gap",
                "price_shock",
            }:
                key = item.get("edge_key")
                if not key and item.get("condition") in {
                    "price_3pct",
                    "price_volume",
                    "spread_50bp",
                }:
                    key = item["condition"] + ":" + item["symbol"]
                state = self.db.execute(
                    "SELECT active FROM decision_edges WHERE key=?", (key,)
                ).fetchone()
                if state is not None and not state[0]:
                    continue
            result.append(item)
        return result
