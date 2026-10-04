import copy
import json
import sqlite3
import uuid

import pytest
from test_decision_pipeline import NOW, context, final, make_brief

from veyquant.decision_pipeline import DecisionPipeline
from veyquant.decision_store import DecisionStore
from veyquant.input_budget import compact_json
from veyquant.memory_book import MAX_CHARS, PAGE_BYTES, with_execution


@pytest.fixture
def store(tmp_path):
    s = DecisionStore(tmp_path / "memory.sqlite3")
    yield s
    s.db.close()


async def run(store, *, kind="manual", answer=None, fail_refresh=False):
    calls = []
    refreshes = 0

    async def model(role, payload):
        calls.append((role, copy.deepcopy(payload)))
        output = make_brief() if role == "middle" else answer or final()
        if callable(output):
            output = output(payload)
        return output, {"status": "received", "model": "fixture"}

    async def collect(*_):
        nonlocal refreshes
        refreshes += 1
        value = context()
        if fail_refresh and refreshes == 3:
            value["order_generation"] += 1
        return value

    async def news(*_):
        raise AssertionError("no extra search or AI calls")

    identity = store.admit(uuid.uuid4().hex, kind, NOW, {})
    pipeline = DecisionPipeline(model, collect, news, store, clock=lambda: NOW)
    result = await pipeline.run(identity, kind, {}, {"settings_revision": 1})
    return result, calls, identity


def add_brief(store, n):
    identity = store.admit(f"interval-{n}", "critical", NOW, {})
    brief = make_brief("WARN")
    brief["summary"] = f"정리 {n} " + "자료" * 150
    store.finish(identity, "aborted", {"brief": brief, "raw": "RAW_MUST_NOT_ENTER"}, NOW)
    return identity


async def test_next_decision_uses_book_and_only_new_briefs_without_extra_ai(store):
    store.memory = lambda *_: pytest.fail("must not load past decision originals")
    first, calls, identity = await run(store)
    assert [r for r, _ in calls] == ["middle", "research"]
    assert first["memory_book_after"]["revision"] == 1
    between = add_brief(store, 1)
    second, calls, _ = await run(store)
    payload = calls[-1][1]
    assert "previous_investment_rationales" not in payload
    assert payload["memory_book"]["content"] == first["decision"]["memory_book"]
    assert [v["id"] for v in payload["intervening_briefs"]["items"]] == [between]
    assert identity not in [v["id"] for v in payload["intervening_briefs"]["items"]]
    assert "RAW_MUST_NOT_ENTER" not in compact_json(payload)
    assert second["memory_book_after"]["revision"] == 2
    assert store.db.execute("SELECT count(*) FROM decision_memory").fetchone()[0] == 2


async def test_failure_and_critical_downgrade_preserve_memory(store):
    await run(store)
    previous = store.memory_book()
    result, _, _ = await run(store, fail_refresh=True)
    assert result is None
    assert store.memory_book() == previous
    result, calls, _ = await run(store, kind="critical")
    assert result is not None and all(r == "middle" for r, _ in calls)
    assert store.memory_book() == previous
    for content in ["", " ", None, "가" * (MAX_CHARS + 1)]:
        result, _, _ = await run(store, answer=final() | {"memory_book": content})
        assert result is None
        assert store.memory_book() == previous


async def test_unread_briefs_survive_and_pages_are_sequential_bounded(store):
    await run(store)
    ids = [add_brief(store, n) for n in range(25)]
    result, calls, _ = await run(store)
    page = calls[-1][1]["intervening_briefs"]
    assert page["has_more"]
    assert len(compact_json(page["items"]).encode()) <= PAGE_BYTES
    assert result["memory_book_after"]["through"] == page["next"]
    seen = [i["id"] for i in page["items"]]

    def answer(payload):
        interval = payload["intervening_briefs"]
        for item in payload.get("tool_results", []):
            if item["tool"] == "briefs":
                interval = item["result"]
        if interval["has_more"]:
            return {
                "action": "READ",
                "tool": "briefs",
                "arguments": {"after": interval["next"], "memory_book": "읽은 정리의 요약"},
            }
        return final()

    result, _, _ = await run(store, answer=answer)
    assert result is not None
    for page in result["intervening_briefs"]:
        assert len(compact_json(page["items"]).encode()) <= PAGE_BYTES
        seen.extend(i["id"] for i in page["items"])
    assert all(seen.count(i) == 1 for i in ids)
    assert not result["intervening_briefs"][-1]["has_more"]


async def test_forged_read_cursor_aborts_without_memory_change(store):
    await run(store)
    for n in range(10):
        add_brief(store, n)
    before = store.memory_book()
    result, _, identity = await run(
        store,
        answer={
            "action": "READ",
            "tool": "briefs",
            "arguments": {"after": 999999, "memory_book": "정리 요약"},
        },
    )
    assert result is None and store.memory_book() == before
    assert (
        json.loads(
            store.db.execute("SELECT data FROM decision_runs WHERE id=?", (identity,)).fetchone()[0]
        )["error"]
        == "invalid_brief_cursor"
    )


async def test_atomic_commit_rolls_back_memory_and_run_on_serialization_failure(store):
    await run(store)
    before = store.memory_book()
    identity = store.admit("atomic", "manual", NOW, {})
    data = {
        "decision": final(),
        "memory_update": {
            "base_revision": before["revision"],
            "through": store.run_cursor(identity),
            "content": final()["memory_book"],
        },
        "cannot_encode": object(),
    }
    with pytest.raises(TypeError):
        store.finish(identity, "complete", data, NOW)
    assert store.memory_book() == before
    assert store.active()[0] == identity
    assert "memory_book_after" not in data
    del data["cannot_encode"]
    data["memory_update"]["through"] = 999999
    with pytest.raises(ValueError, match="invalid_memory_cursor"):
        store.finish(identity, "complete", data, NOW)
    data["memory_update"]["through"] = store.run_cursor(identity)
    data["memory_update"]["base_revision"] = 0
    with pytest.raises(ValueError, match="memory_book_conflict"):
        store.finish(identity, "complete", data, NOW)
    assert store.memory_book() == before


def test_seed_and_intervals_read_only_small_previews(store):
    for n in range(5):
        identity = store.admit(f"legacy-{n}", "manual", NOW + n, {})
        store.finish(
            identity,
            "complete",
            {
                "brief": make_brief(),
                "decision": final(),
                "trace": [
                    {"role": "research", "task": "investment_decision", "status": "received"}
                ],
                "raw": "archive" * 10000,
            },
            NOW + n,
        )

    def authorize(op, table, column, *_):
        return (
            sqlite3.SQLITE_DENY
            if (op == sqlite3.SQLITE_READ and table == "decision_runs" and column == "data")
            else sqlite3.SQLITE_OK
        )

    store.db.set_authorizer(authorize)
    seed = store.memory_book()
    assert seed["revision"] == 0 and seed["origin"] == "legacy_seed"
    assert seed["content"].count("과거 판단") == 3
    assert len(seed["content"]) <= MAX_CHARS
    assert "archive" not in json.dumps(seed)
    assert store.brief_interval(0, 999)["items"]
    store.db.set_authorizer(None)
    assert store.db.execute("SELECT count(*) FROM decision_memory").fetchone()[0] == 0


async def test_memory_proposals_are_not_reported_as_fills(store):
    result, _, _ = await run(store, answer=final("SUBMIT"))
    book = result["memory_book_after"]
    view = with_execution(book, [])
    assert view["last_execution"][0]["execution"]["state"] == "unconfirmed"
    event_id = book["last_proposals"][0]["event_id"]
    view = with_execution(book, [{"event_id": event_id, "state": "rejected", "reason": "limit"}])
    assert view["last_execution"][0]["execution"]["state"] == "rejected"
