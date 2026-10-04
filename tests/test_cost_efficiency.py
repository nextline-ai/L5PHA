"""Cost reductions preserve evidence, holdings, freshness and admission semantics."""

import copy

import pytest
from test_decision_pipeline import NOW, final, make_brief, run_pipeline
from test_decision_pipeline import store as store
from test_live_worker import worker as worker
from test_model_context import frozen_context

from veyquant import model_context as views
from veyquant.decision_pipeline import DecisionPipeline
from veyquant.decision_store import DecisionStore
from veyquant.input_budget import compact_json
from veyquant.research_context import ResearchContext


def restore_table(table):
    result = []
    for row in table["rows"]:
        record = {}
        for path, value in (
            table["shared"] | dict(zip(table["columns"], row, strict=True))
        ).items():
            if path in table["columns"] and path.startswith("indicators."):
                value = value[path]
            current = record
            parts = path.split(".")
            for part in parts[:-1]:
                current = current.setdefault(part, {})
            current[parts[-1]] = value
        result.append(record)
    return result


def test_columnar_stock_facts_roundtrip_and_reduce_repeated_input():
    frozen = frozen_context(20)
    projected = [views.stock(r, frozen["details"][r["symbol"]]) for r in frozen["review_universe"]]
    table = views.stock_table(projected)
    assert restore_table(table) == projected
    assert len(compact_json(table).encode()) < len(compact_json(projected).encode()) * 0.6
    assert restore_table(views.stock_table(projected[:1])) == projected[:1]
    assert restore_table(views.stock_table([])) == []
    assert frozen["details"]  # Projection never mutates the raw archive.


def test_columnar_partial_quotes_and_optional_indicators_remain_unambiguous():
    records = [
        {"symbol": "000001", "frozen_quote": {}, "indicators": {"krx": {}}},
        {
            "symbol": "000002",
            "frozen_quote": {"price": "100"},
            "indicators": {"krx": {"close": "99"}, "daily_discontinuity": {"count": 1}},
        },
    ]
    table = views.stock_table(records)
    assert "frozen_quote" not in table["columns"]
    restored = restore_table(table)
    assert restored[0]["frozen_quote"]["price"] is None
    assert restored[0]["indicators"]["krx"] == {}
    assert restored[1] == records[1]


@pytest.mark.parametrize("mode", ["event", "broad"])
@pytest.mark.parametrize("scoped", [True, False])
@pytest.mark.parametrize("throttled", [False, True])
async def test_event_scope_collects_holdings_and_events_without_padding(
    worker, monkeypatch, mode, scoped, throttled
):
    await worker.reconcile()
    ctx = ResearchContext(worker, worker.clock)
    universe = worker.observations.universe
    universe.stocks = {
        f"{i:06d}": {
            "symbol": f"{i:06d}",
            "name": f"종목{i}",
            "market": "KOSPI",
            "eligibility": "eligible",
        }
        for i in range(1, 251)
    }
    universe.watch = ("000003", "000004")

    async def account(*_):
        return {
            "holdings": [{"symbol": "000250", "quantity": "2"}],
            "open_orders": [],
            "unresolved_submission": False,
            "conditional_orders": 0,
        }

    books = []

    async def book(symbol):
        books.append(symbol)
        if throttled:
            from veyquant.adapters.toss import TossHTTPError

            raise TossHTTPError(429)
        return {"timestamp": worker.clock(), "bids": [], "asks": []}

    async def trades(symbol):
        return []

    monkeypatch.setattr(ctx, "account", account)
    monkeypatch.setattr(worker.broker, "orderbook", book, raising=False)
    monkeypatch.setattr(worker.broker, "trades", trades, raising=False)
    request = {"review_mode": mode, "symbols": ["000249"] if scoped else []}
    result = await ctx.handle({"operation": "initial", "request": request})
    expected = {"000250", "000249"} if scoped else {"000250", "000003", "000004"}
    assert expected <= set(result["details"])
    assert len(books) == len(set(books)) == (len(expected) if mode == "event" else 200)
    assert len(result["comparison_table"]["rows"]) == 250
    if throttled:
        symbol = next(iter(result["details"]))
        assert result["details"][symbol]["orderbook"] is None
        assert (
            views.market_read(result, symbol, ["orderbook"])["orderbook"]["state"] == "unavailable"
        )
        assert "orderbook" in views.indicators(result["details"][symbol])["unavailable"]


async def test_irrelevant_filings_excluded_but_global_and_held_evidence_survive(store):
    for identity, symbol in [("held", "000001"), ("outside", "999999"), ("global", None)]:
        store.evidence(
            "dart_important",
            {"kind": "dart_important", "symbol": symbol, "summary": "fact:" + identity},
            NOW,
            identity,
        )

    async def model(role, payload):
        if role == "middle":
            summaries = {e.get("summary") for e in payload["recent_evidence"]}
            assert {"fact:held", "fact:global"} <= summaries and "fact:outside" not in summaries
            return make_brief() | {"candidates": []}
        assert not payload["candidates"]
        assert restore_table(payload["held_market"])[0]["symbol"] == "000001"
        return final()

    def managed(value, _):
        value["holdings"][0]["managed_quantity"] = 2

    result, _, _, _ = await run_pipeline(store, mutate=managed, model_override=model)
    assert result and result["merge_skipped"]
    assert (
        store.db.execute("SELECT count(*) FROM decision_evidence WHERE id='outside'").fetchone()[0]
        == 1
    )


@pytest.mark.parametrize("tool", ["news", "search"])
async def test_search_cache_exact_scope_ttl_and_role_isolation(tmp_path, tool):
    store = DecisionStore(tmp_path / "db")
    now, calls = [100], []

    async def news(arguments, *_):
        calls.append(copy.deepcopy(arguments))
        return {
            "sources": [
                {
                    "id": "source1",
                    "kind": "web",
                    "source": "OpenAI Web Search",
                    "title": "공식 공시",
                    "url": "https://example.com/ir",
                }
            ],
            "summary": {"summary": "검토 결과", "evidence_ids": ["source1"], "uncertainties": []},
            "trace": [],
            "grounding": {},
        }

    pipeline = DecisionPipeline(
        None, None, news, store, clock=lambda: NOW, monotonic=lambda: now[0]
    )
    catalogue, frozen = {}, frozen_context(2)
    request = {
        "action": "READ",
        "tool": tool,
        "arguments": {"symbols": ["000002", "000001"], "topics": ["earnings"]},
    }
    first = await pipeline.read_tool(request, frozen, catalogue, 700)
    first["result"]["summary"]["summary"] = "caller mutation"
    now[0] += 59
    request["arguments"]["symbols"] = ["000001", "000002", "000001"]
    cached = await pipeline.read_tool(request, frozen, catalogue, 700)
    assert cached["cache_hit"] and cached["result"]["summary"]["summary"] == "검토 결과"
    assert len(calls) == pipeline.news_calls == 1 and pipeline.search_cache_hits == 1
    assert "source1" in catalogue
    now[0] += 1
    assert not (await pipeline.read_tool(request, frozen, catalogue, 700)).get("cache_hit")
    request["tool"] = "search" if tool == "news" else "news"
    await pipeline.read_tool(request, frozen, catalogue, 700)
    request["arguments"]["symbols"] = ["000001"]
    await pipeline.read_tool(request, frozen, catalogue, 700)
    assert len(calls) == pipeline.news_calls == 4
    assert pipeline.middle_news_calls == 2
    store.db.close()


async def test_failed_search_is_retryable(tmp_path):
    store = DecisionStore(tmp_path / "db")
    calls = []

    async def fail(*_):
        calls.append(1)
        raise ValueError("unavailable")

    pipeline = DecisionPipeline(None, None, fail, store)
    for _ in range(2):
        with pytest.raises(ValueError, match="unavailable"):
            await pipeline.read_tool(
                {
                    "action": "READ",
                    "tool": "search",
                    "arguments": {"symbols": ["000001"], "topics": ["earnings"]},
                },
                frozen_context(1),
                {},
                pipeline.monotonic() + 600,
            )
    assert len(calls) == 2 and not pipeline.search_cache
    store.db.close()


def test_usage_poll_cache_invalidates_on_updates_day_change_and_expiry(tmp_path, monkeypatch):
    store = DecisionStore(tmp_path / "db")
    now, statements = [100], []
    monkeypatch.setattr("veyquant.decision_store.time.monotonic", lambda: now[0])
    store.db.set_trace_callback(statements.append)
    assert store.usage_summary(100)["groups"] == []
    store.usage_summary(100)
    assert sum("FROM ai_usage WHERE" in s for s in statements) == 1
    identity = store.usage_start(100, "run", "stage", "middle", "fixture")
    summary = store.usage_summary(100)
    assert summary["groups"][0]["unknown_usage"] == 1
    summary["groups"][0]["attempts"] = 999
    assert store.usage_summary(100)["groups"][0]["attempts"] == 1
    store.usage_finish(identity, {"input_tokens": 3, "output_tokens": 2})
    assert store.usage_summary(100)["groups"][0]["input_tokens"] == 3
    assert store.usage_summary(101)["groups"] == []
    before = len(statements)
    now[0] += 30
    store.usage_summary(101)
    assert len(statements) > before
    store.db.close()


async def test_requested_research_refreshes_expired_catalogue_without_market_scan(
    worker, monkeypatch
):
    await worker.reconcile()
    ctx = ResearchContext(worker, worker.clock)
    universe = worker.observations.universe
    fresh, calls = [False], []
    universe.stocks = {
        "005930": {
            "symbol": "005930",
            "name": "테스트",
            "market": "KOSPI",
            "eligibility": "eligible",
        }
    }

    async def account():
        return {
            "holdings": [{"symbol": "005930", "quantity": "2"}],
            "open_orders": [],
            "unresolved_submission": False,
            "conditional_orders": 0,
        }

    monkeypatch.setattr(ctx, "account", account)

    async def catalogue():
        calls.append("catalogue")
        fresh[0] = True

    async def book(symbol):
        return {"timestamp": worker.clock(), "bids": [], "asks": []}

    async def trades(symbol):
        return []

    monkeypatch.setattr(universe, "fresh", lambda: fresh[0])
    monkeypatch.setattr(universe, "catalogue", catalogue)
    monkeypatch.setattr(worker.broker, "orderbook", book, raising=False)
    monkeypatch.setattr(worker.broker, "trades", trades, raising=False)
    result = await ctx.handle({"operation": "initial", "request": {"review_mode": "event"}})
    assert calls == ["catalogue"]
    assert result["details"]
