import copy
from contextlib import closing
from datetime import datetime

import pytest
from starlette.testclient import TestClient
from test_live_worker import NOW, Broker

from veyquant.collector import ObservationStore
from veyquant.management import ManagementConfig, create_app
from veyquant.shadow_contract import read_market
from veyquant.shadow_inference import analyze, validate
from veyquant.shadow_runner import validated_report
from veyquant.store import Store
from veyquant.telegram_auth import OwnerAuth, TelegramVerifier, digest
from veyquant.universe import MAX_WATCH, Universe, read_universe, validate_trade_stock


class MarketBroker(Broker):
    def __init__(self, count=470):
        super().__init__()
        self.symbols = [f"{i:06d}" for i in range(count)] + ["0101N0"]
        self.calls, self.missing, self.null_time = [], None, None

    def market(self, symbol):
        return ("KOSPI", "KOSDAQ", "KR_ETC")[self.symbols.index(symbol) % 3]

    async def list_stocks(self, market):
        self.calls.append(("list", market))
        return [
            {"symbol": s, "securityType": "ETF" if s == "000002" else "STOCK"}
            for s in self.symbols
            if self.market(s) == market
        ]

    async def stock_info(self, symbols):
        self.calls.append(("info", tuple(symbols)))
        rows = await super().stock_info(symbols)
        for row in rows:
            row["market"] = self.market(row["symbol"])
            row["isCommonShare"] = row["symbol"] != "000001"
            if row["symbol"] == "000002":
                row["securityType"] = "ETF"
            if row["symbol"] == "000003":
                row["koreanMarketDetail"]["krxTradingSuspended"] = True
        return [r for r in rows if r["symbol"] != self.missing]

    async def prices(self, symbols):
        self.calls.append(("prices", tuple(symbols)))
        return [
            {
                "symbol": s,
                "currency": "KRW",
                "lastPrice": "100",
                "timestamp": None
                if s == self.null_time
                else datetime.fromtimestamp(NOW).astimezone().isoformat(),
            }
            for s in symbols
        ]


@pytest.fixture
def universe(tmp_path):
    obs = ObservationStore(str(tmp_path / "db"), str(tmp_path / "status"), str(tmp_path / "market"))
    obs.connected = True
    t = [NOW]
    u = Universe(obs, MarketBroker(), str(tmp_path / "universe.json"), clock=lambda: t[0])
    u.test_time = t
    obs.universe = u
    yield u
    obs.db.close()


async def test_every_domestic_market_and_batch_are_scanned_and_preferences_supported(universe):
    u = universe
    await u.scan()
    assert {v for k, v in u.broker.calls if k == "list"} == {"KOSPI", "KOSDAQ", "KR_ETC"}
    assert len(u.stocks) == 470
    assert "000002" not in u.stocks  # ETF is not a cash equity.
    assert u.stocks["000001"]["common_share"] is False
    assert "0101N0" in u.stocks  # Newly introduced alphanumeric domestic codes.
    assert u.stocks["000003"]["eligibility"] == "suspended"
    assert "000003" not in u.watch
    batches = [v for k, v in u.broker.calls if k == "prices"]
    assert len(batches) == 3 and max(map(len, batches)) <= 200
    assert set().union(*map(set, batches)) == set(u.stocks)
    assert len(u.watch) == MAX_WATCH
    assert u.covered == 470
    assert all(b["symbol"] == s for s, b in u.observations.daily_bars.items())
    view = read_universe(u.path, NOW, query="0101n0")
    assert view["matched"] == 1 and view["items"][0]["symbol"] == "0101N0"
    assert len(read_universe(u.path, NOW)["items"]) == 40


async def test_rotation_reaches_outside_first_watch_and_never_drops_managed_stock(universe):
    u = universe
    await u.scan(("000469",))
    first = set(u.watch)
    assert "000469" in first
    u.test_time[0] += 301
    await u.scan(("000469",))
    assert "000469" in u.watch and set(u.watch) - first
    assert len([c for c in u.broker.calls if c[0] == "list"]) == 3  # Cached for this date.


async def test_catalogue_refresh_is_atomic_and_restart_does_not_refresh_timestamp(universe):
    u = universe
    await u.catalogue()
    original, stamp = copy.deepcopy(u.stocks), u.catalog_at
    u.test_time[0] += 86400
    u.broker.missing = "000001"
    with pytest.raises(ValueError, match="incomplete_stock_details"):
        await u.catalogue()
    assert u.stocks == original and u.catalog_at == stamp and not u.fresh()
    resumed = Universe(u.observations, u.broker, clock=u.clock)
    assert resumed.catalog_at == stamp and not resumed.fresh()


async def test_missing_quote_never_inherits_previous_price_or_timestamp(universe):
    u = universe
    await u.scan()
    u.broker.null_time = "000000"
    u.test_time[0] += 301
    await u.scan()
    assert "000000" not in u.prices
    assert u.covered == 469 and u.state == "partial_prices"
    assert read_universe(u.path, u.clock(), "000000")["items"][0]["price"] is None


async def test_newly_filled_stock_is_pinned_after_slow_candle_reads(universe):
    u = universe
    pins = []
    original = u.broker.daily_candles

    async def fill_during_scan(symbol):
        pins[:] = ["000469"]
        return await original(symbol)

    u.broker.daily_candles = fill_during_scan
    await u.scan(lambda: pins)
    assert u.watch[0] == "000469" and len(u.watch) == MAX_WATCH


@pytest.mark.parametrize(
    "condition", ["suspended", "liquidation", "etf", "foreign", "unknown_warning", "vi"]
)
async def test_execution_checks_actual_instrument_and_current_warnings(condition):
    row = (await Broker().stock_info(("000660",)))[0]
    warnings = []
    if condition == "suspended":
        row["koreanMarketDetail"]["krxTradingSuspended"] = True
    elif condition == "liquidation":
        row["koreanMarketDetail"]["liquidationTrading"] = True
    elif condition == "etf":
        row["securityType"] = "ETF"
    elif condition == "foreign":
        row["currency"] = "USD"
    else:
        warnings = [{"warningType": "VI_STATIC" if condition == "vi" else "NEW_UNKNOWN_CODE"}]
    with pytest.raises(ValueError):
        validate_trade_stock([row], warnings, "000660", "BUY")
    with pytest.raises(ValueError):
        validate_trade_stock([row], warnings, "000660", "SELL")


async def test_risk_designation_allows_only_reducing_owned_position():
    row = (await Broker().stock_info(("000660",)))[0]
    warnings = [{"warningType": "INVESTMENT_RISK"}]
    with pytest.raises(ValueError):
        validate_trade_stock([row], warnings, "000660", "BUY")
    assert validate_trade_stock([row], warnings, "000660", "SELL")["symbol"] == "000660"


async def test_symbol_bound_candles_cannot_be_substituted_between_instruments():
    from test_shadow import NOW as ANALYSIS_NOW
    from test_shadow import Model, event, gate, report

    from veyquant.trading_state import completed_bars

    e = event()
    e["quote"]["symbol"] = "0101N0"
    e["instrument"] = {"symbol": "0101N0", "name": "새 국내 종목", "market": "KOSDAQ"}
    e["daily_bars"] = {
        "symbol": "0101N0",
        "updated_at": ANALYSIS_NOW,
        "bars": completed_bars(await Broker().daily_candles(), ANALYSIS_NOW),
    }
    decision = report() | {"outcome": "buy", "evidence_ids": ["quote", "daily_bars"]}
    raw = analyze(e, Model([gate(), gate(), decision]), ANALYSIS_NOW)
    safe = validated_report(raw, e, ANALYSIS_NOW)
    assert safe["proposal"] == {"side": "BUY"} and safe["name"] == "새 국내 종목"
    e["daily_bars"]["symbol"] = "005930"
    with pytest.raises(ValueError, match="symbol_mismatch"):
        validate(e, ANALYSIS_NOW)
    with pytest.raises(ValueError, match="symbol_mismatch"):
        validated_report(raw, e, ANALYSIS_NOW)


async def test_universe_search_is_owner_only_bounded_and_market_filtered(universe, tmp_path):
    u = universe
    await u.scan()
    path = str(tmp_path / "management.db")
    with closing(Store(path)) as store:
        OwnerAuth(store, TelegramVerifier(123))
        store.db.execute("INSERT INTO owner VALUES(1,123)")
        store.db.execute(
            "INSERT INTO sessions VALUES(?,?,?,?)", (digest("x" * 43), 123, NOW + 1000, NOW + 2000)
        )
    origin = "https://investor.example"
    config = ManagementConfig(path, origin, 123, universe_path=u.path)
    with TestClient(create_app(config, clock=lambda: NOW), base_url=origin) as client:
        assert client.get("/v1/universe").status_code == 401
        client.cookies.set("__Host-veyquant_session", "x" * 43)
        page = client.get("/v1/universe?market=KOSDAQ&page=1").json()
        assert len(page["items"]) == 40 and all(i["market"] == "KOSDAQ" for i in page["items"])
        assert client.get("/v1/universe?page=-1").status_code == 400
        assert client.get("/v1/universe?market=NASDAQ").status_code == 400
        summary = client.get("/v1/status").json()["universe"]
        assert summary["total"] == 470 and "items" not in summary


async def test_ineligible_catalogue_stock_never_becomes_analysis_candidate(universe):
    u = universe
    await u.scan()
    u.observations.quotes = dict(u.prices)
    u.observations.watch_symbols = tuple(u.prices)
    u.observations.instruments = dict(u.stocks)
    u.observations.publish(NOW)
    quotes = read_market(u.observations.market_path, NOW)
    assert "000003" not in {q["symbol"] for q in quotes}
    assert "0101N0" in {q["symbol"] for q in quotes}
    u.test_time[0] += 86400
    u.observations.publish(u.clock())
    with pytest.raises(ValueError, match="universe_unavailable"):
        read_market(u.observations.market_path, u.clock())


def test_analysis_budget_is_shared_across_the_whole_market(tmp_path):
    from test_shadow import event

    from veyquant.shadow_runner import ShadowStore

    store = ShadowStore(str(tmp_path / "analysis.sqlite3"))
    try:
        admitted = []
        for i in range(25):
            q = event()["quote"] | {"symbol": f"{i:06d}"}
            admitted.append(store.reserve(q, NOW))
        assert sum(r is not None for r in admitted) == 12
        assert store.used(NOW) == 12
        assert store.used(NOW + 86400) == 0
    finally:
        store.db.close()


async def test_scan_yields_to_research_then_resumes_complete_coverage(universe, monkeypatch):
    import asyncio

    u = universe
    await u.catalogue()
    u.broker.calls.clear()
    u.broker.research_reads_active = 1
    waiting = asyncio.Event()
    original_sleep = asyncio.sleep

    async def sleep(delay):
        waiting.set()
        await original_sleep(0)

    monkeypatch.setattr("veyquant.universe.asyncio.sleep", sleep)
    task = asyncio.create_task(u.scan())
    try:
        await waiting.wait()
        assert not u.broker.calls
        u.broker.research_reads_active = 0
        await task
        assert u.covered == 470
    finally:
        task.cancel()
