import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Store:
    """Local single-account store. Separate connections serialize risk reservations."""

    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
                stage TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reservations (
                proposal_id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
                amount TEXT NOT NULL, currency TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS controls (
                id INTEGER PRIMARY KEY CHECK(id=1), stopped INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO controls VALUES (1, 0);
        """)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def record(self, event_id: str, stage: str, payload: dict):
        # Only application-curated payloads enter here; never HTTP headers or raw auth.
        self.db.execute(
            "INSERT INTO audit(event_id, stage, payload) VALUES (?, ?, ?)",
            (event_id, stage, json.dumps(payload, default=str, ensure_ascii=False)),
        )

    def claim(self, event_id: str) -> bool:
        with self.transaction():
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO events VALUES (?, 'processing')", (event_id,)
            ).rowcount
            self.record(event_id, "observation" if inserted else "duplicate", {})
            return bool(inserted)

    def finish(self, event_id: str, status: str):
        self.db.execute("UPDATE events SET status=? WHERE id=?", (status, event_id))

    def stop(self):
        with self.transaction():
            self.db.execute("UPDATE controls SET stopped=1 WHERE id=1")
            self.record("system", "stop", {"new_proposals_stopped": True})

    def rows(self):
        return [
            dict(r) | {"payload": json.loads(r["payload"])}
            for r in self.db.execute("SELECT * FROM audit ORDER BY seq")
        ]
