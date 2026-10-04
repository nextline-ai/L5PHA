"""Read-only Toss adapter against OpenAPI 1.2.19 and AsyncAPI 1.2.2.

There is deliberately no public generic HTTP request or broker order method.
Use one TokenManager for REST and WS in a single process: issuing a new token
invalidates the previous token for the same client.
"""

import asyncio
import copy
import json
import random
import re
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from time import time as wall_time

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from veyquant.shadow_contract import domestic_symbol

REST_URL = "https://openapi.tossinvest.com"
WS_URL = "wss://openapi-ws.tossinvest.com/ws/v1"
SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,19}\Z")


class TossError(RuntimeError):
    pass


class TossHTTPError(TossError):
    def __init__(self, status: int, retry_after=0):
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"toss_http_{status}")


class SubscriptionRejected(TossError):
    pass


@dataclass(frozen=True)
class Credentials:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)


class TokenManager:
    def __init__(
        self,
        credentials: Credentials,
        http: httpx.AsyncClient,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._credentials, self._http, self._clock = credentials, http, clock
        self._token, self._expires = "", 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        async with self._lock:
            if self._token and self._clock() < self._expires:
                return self._token
            try:
                response = await self._http.post(
                    REST_URL + "/oauth2/token",
                    follow_redirects=False,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._credentials.client_id,
                        "client_secret": self._credentials.client_secret,
                    },
                )
            except httpx.HTTPError:
                raise TossError("toss_token_transport_failure") from None
            if response.status_code != 200:
                raise TossHTTPError(response.status_code)
            try:
                body = response.json()
                token, ttl = body["access_token"], body["expires_in"]
                if not isinstance(token, str) or not token or type(ttl) is not int or ttl <= 0:
                    raise ValueError
                if body.get("token_type", "").lower() != "bearer":
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise TossError("invalid_token_response") from None
            self._token = token
            self._expires = self._clock() + max(1, ttl - min(60, ttl / 10))
            return self._token


def http_client() -> httpx.AsyncClient:
    # No redirects or environment proxies can forward credentials to another host.
    return httpx.AsyncClient(
        timeout=httpx.Timeout(15, connect=5), follow_redirects=False, trust_env=False
    )


class TossReadOnly:
    def __init__(self, tokens: TokenManager, http: httpx.AsyncClient):
        self._tokens, self._http = tokens, http
        self._read_locks, self._read_next, self._read_rates = {}, {}, {}
        self._read_adjusted = {}
        self.read_limits = {}
        self._daily_cache = OrderedDict()
        self._daily_locks = {}

    async def _get(self, path, params=None, account_seq=None):
        # Retry only confirmed throttles on GET. The shared group scheduler
        # enforces Retry-After for every caller, including account/constraint reads.
        # Cancellation and the caller's total deadline cover all attempts/waits.
        for attempt in range(3):
            try:
                return await self._get_once(path, params, account_seq)
            except TossHTTPError as error:
                if error.status != 429 or attempt == 2:
                    raise
                await asyncio.sleep(max(error.retry_after, 2**attempt) + random.random() * 0.25)

    async def _get_once(self, path, params=None, account_seq=None):
        headers = {}
        if account_seq is not None:
            if not re.fullmatch(r"[0-9]+", str(account_seq)):
                raise ValueError("invalid accountSeq")
            headers["X-Tossinvest-Account"] = str(account_seq)
        group, rate = (
            ("orders", 5)
            if path.startswith("/api/v1/orders")
            else ("order_info", 3)
            if path in {"/api/v1/buying-power", "/api/v1/sellable-quantity", "/api/v1/commissions"}
            else ("conditionals", 10)
            if path.startswith("/api/v1/conditional-orders")
            else ("accounts", 1)
            if path == "/api/v1/accounts"
            else ("assets", 5)
            if path == "/api/v1/holdings"
            else ("calendar", 3)
            if path.startswith("/api/v1/market-calendar")
            else ("candles", 20)
            if path == "/api/v1/candles"
            else ("stock_all", 1)
            if path == "/api/v1/stocks/all"
            else ("stock", 5)
            if path.startswith("/api/v1/stocks")
            else ("market", 15)
        )
        lock = self._read_locks.setdefault(group, asyncio.Lock())
        try:
            async with lock:
                await asyncio.sleep(max(0, self._read_next.get(group, 0) - time.monotonic()))
                # A queued read may outlive a token rotation by another REST/WS
                # task. Acquire the shared current token immediately before send.
                headers["Authorization"] = f"Bearer {await self._tokens.get()}"
                # A short-lived valuation read can be cancelled in flight. It
                # still consumed capacity; cancellation must not erase its slot.
                self._read_next[group] = time.monotonic() + 1 / self._read_rates.get(group, rate)
                response = await self._http.get(
                    REST_URL + path, params=params, headers=headers, follow_redirects=False
                )
                reported = response.headers.get("X-RateLimit-Limit", "")
                old = self.read_limits.get(group, {})
                successes = (
                    old.get("successes_since_adjustment", 0) + 1
                    if response.status_code == 200
                    else 0
                )
                if reported.isdigit() and int(reported) > 0:
                    self._read_rates[group] = min(
                        self._read_rates.get(group, rate), rate, int(reported)
                    )
                recovered_window = (
                    response.status_code == 200
                    and time.monotonic() - self._read_adjusted.get(group, 0) >= 60
                )
                if successes >= 20 or recovered_window:
                    # Recover gradually after sustained success. Permanently
                    # halving on isolated throttles would eventually starve a
                    # full-market review even after broker capacity recovers.
                    ceiling = (
                        min(rate, int(reported))
                        if reported.isdigit() and int(reported) > 0
                        else rate
                    )
                    self._read_rates[group] = min(
                        ceiling,
                        max(
                            self._read_rates.get(group, rate) * 1.25,
                            ceiling / 2 if recovered_window else 0,
                        ),
                    )
                    self._read_adjusted[group] = time.monotonic()
                    successes = 0
                timing = {}
                for name in ("Retry-After", "X-RateLimit-Reset"):
                    value = response.headers.get(name, "")
                    if re.fullmatch(r"[0-9]{1,5}(\.[0-9]{1,3})?", value):
                        timing[name] = float(value)
                delay = 1 / self._read_rates.get(group, rate)
                if response.headers.get("X-RateLimit-Remaining") == "0":
                    delay = max(delay, timing.get("X-RateLimit-Reset", 0))
                remaining = response.headers.get("X-RateLimit-Remaining", "")
                upstream_throttle = (
                    response.status_code == 429 and remaining.isdigit() and int(remaining) > 0
                )
                if response.status_code == 429:
                    # Server backoff is a lower bound, not a capped delay. Keep
                    # the learned slower pace across subsequent successful reads.
                    # A downstream refusal with bucket tokens remaining is not
                    # evidence that this entire client bucket is exhausted.
                    # Honor backoff, but do not starve unrelated prices/reads.
                    if not upstream_throttle:
                        self._read_rates[group] = max(0.5, self._read_rates.get(group, rate) / 2)
                        self._read_adjusted[group] = time.monotonic()
                    delay = max(
                        1 / self._read_rates[group], *timing.values(), 2 if not timing else 0
                    )
                self._read_next[group] = time.monotonic() + delay
                self.read_limits[group] = {
                    "requests": old.get("requests", 0) + 1,
                    "throttled": old.get("throttled", 0) + (response.status_code == 429),
                    "reported_limit": int(reported) if reported.isdigit() else None,
                    "paced_per_second": self._read_rates.get(group, rate),
                    "last_status": response.status_code,
                    "next_delay_seconds": delay,
                    "successes_since_adjustment": successes,
                }
                if response.status_code == 429:
                    # Diagnostic allowlist: no request parameters, response message,
                    # account identifiers or authorization headers are retained.
                    try:
                        code = response.json().get("error", {}).get("code")
                    except (ValueError, AttributeError):
                        code = None
                    self.read_limits[group]["last_throttle"] = {
                        "at": wall_time(),
                        "path": re.sub(r"(/(?:conditional-)?orders)/.*", r"\1/{id}", path),
                        "code": code
                        if isinstance(code, str) and re.fullmatch(r"[a-z0-9_-]{1,80}", code)
                        else None,
                        "remaining": response.headers.get("X-RateLimit-Remaining", "")[:12],
                        "timing": timing,
                        "scope": "upstream" if upstream_throttle else "group",
                    }
                elif "last_throttle" in old:
                    self.read_limits[group]["last_throttle"] = old["last_throttle"]
        except httpx.HTTPError:
            raise TossError("toss_read_transport_failure") from None
        if response.status_code != 200:
            # Never conceal failed reads as empty data.
            raise TossHTTPError(response.status_code, delay if response.status_code == 429 else 0)
        try:
            body = response.json()
            return body["result"]
        except (ValueError, KeyError, TypeError):
            raise TossError("invalid_read_response") from None

    async def accounts(self):
        return await self._get("/api/v1/accounts")

    async def prices(self, symbols: tuple[str, ...]):
        if not 1 <= len(symbols) <= 200 or any(not SYMBOL.fullmatch(s) for s in symbols):
            raise ValueError("invalid symbols")
        return await self._get("/api/v1/prices", {"symbols": ",".join(symbols)})

    async def holdings(self, account_seq: str):
        return await self._get("/api/v1/holdings", account_seq=account_seq)

    async def open_orders(self, account_seq: str):
        # OPEN is unpaginated; it still excludes unsupported order types in the broker API.
        return await self._get("/api/v1/orders", {"status": "OPEN"}, account_seq)

    async def order(self, account_seq: str, order_id: str):
        if not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", order_id):
            raise ValueError("invalid orderId")
        return await self._get(f"/api/v1/orders/{order_id}", account_seq=account_seq)

    async def buying_power(self, account_seq: str):
        return await self._get("/api/v1/buying-power", {"currency": "KRW"}, account_seq)

    async def conditional_orders(self, account_seq: str):
        items, seen, cursor = [], set(), None
        # OPEN conditionals are paginated, unlike ordinary OPEN orders.
        for _ in range(20):
            params = {"status": "OPEN", "limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = await self._get("/api/v1/conditional-orders", params, account_seq)
            if not isinstance(page, dict) or not isinstance(page.get("conditionalOrders"), list):
                raise TossError("invalid_conditional_page")
            items.extend(page["conditionalOrders"])
            cursor = page.get("nextCursor")
            if page.get("hasNext") is False and cursor is None:
                return {"conditionalOrders": items, "hasNext": False, "nextCursor": None}
            if (
                page.get("hasNext") is not True
                or not isinstance(cursor, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,2000}", cursor)
                or cursor in seen
            ):
                raise TossError("incomplete_conditional_orders")
            seen.add(cursor)
        raise TossError("conditional_page_limit")

    async def market_calendar(self):
        return await self._get("/api/v1/market-calendar/KR")

    async def sellable_quantity(self, account_seq, symbol):
        if not domestic_symbol(symbol):
            raise ValueError("unsupported_execution_symbol")
        return await self._get("/api/v1/sellable-quantity", {"symbol": symbol}, account_seq)

    async def commissions(self, account_seq):
        return await self._get("/api/v1/commissions", account_seq=account_seq)

    async def list_stocks(self, market):
        if market not in {"KOSPI", "KOSDAQ", "KR_ETC"}:
            raise ValueError("unsupported_market")
        return await self._get("/api/v1/stocks/all", {"market": market, "status": "ACTIVE"})

    async def stock_info(self, symbols):
        if not 1 <= len(symbols) <= 200 or any(not domestic_symbol(s) for s in symbols):
            raise ValueError("invalid_domestic_symbols")
        return await self._get("/api/v1/stocks", {"symbols": ",".join(symbols)})

    async def stock_warnings(self, symbol):
        if not domestic_symbol(symbol):
            raise ValueError("invalid_domestic_symbol")
        return await self._get(f"/api/v1/stocks/{symbol}/warnings")

    async def orderbook(self, symbol):
        if not domestic_symbol(symbol):
            raise ValueError("invalid_domestic_symbol")
        return await self._get("/api/v1/orderbook", {"symbol": symbol})

    async def price_limits(self, symbol):
        if not domestic_symbol(symbol):
            raise ValueError("invalid_domestic_symbol")
        return await self._get("/api/v1/price-limits", {"symbol": symbol})

    async def trades(self, symbol):
        if not domestic_symbol(symbol):
            raise ValueError("invalid_domestic_symbol")
        return await self._get("/api/v1/trades", {"symbol": symbol, "count": 30})

    async def daily_candles(self, symbol):
        if not domestic_symbol(symbol):
            raise ValueError("invalid_domestic_symbol")
        # Completed daily bars are reused across universe scans and research. The
        # cache never supplies account/current-price/order reads or crosses a KST day.
        lock = self._daily_locks.setdefault(symbol, asyncio.Lock())
        async with lock:
            now = wall_time()
            cached = self._daily_cache.get(symbol)
            if (
                cached
                and 0 <= now - cached[0] < 1800
                and int((now + 32400) // 86400) == int((cached[0] + 32400) // 86400)
            ):
                self._daily_cache.move_to_end(symbol)
                return copy.deepcopy(cached[1])
            result = await self._get(
                "/api/v1/candles",
                {"symbol": symbol, "interval": "1d", "count": 30, "adjusted": "true"},
            )
            if (
                isinstance(result, dict)
                and isinstance(result.get("candles"), list)
                and result["candles"]
            ):
                self._daily_cache[symbol] = (now, copy.deepcopy(result))
                self._daily_cache.move_to_end(symbol)
                while len(self._daily_cache) > 512:
                    self._daily_cache.popitem(last=False)
            return result

    async def minute_candles(self, symbol):
        if not domestic_symbol(symbol):
            raise ValueError("invalid_domestic_symbol")
        return await self._get(
            "/api/v1/candles",
            {"symbol": symbol, "interval": "1m", "count": 120, "adjusted": "false"},
        )


@dataclass(frozen=True)
class Subscription:
    channel: str
    codes: tuple[str, ...]

    def __post_init__(self):
        if self.channel not in {
            "trade:kr",
            "trade:us",
            "orderbook:kr",
            "orderbook:us",
            "personal:order",
        }:
            raise ValueError("unsupported channel")
        if not self.codes or len(set(self.codes)) != len(self.codes):
            raise ValueError("empty or duplicate codes")
        for code in self.codes:
            if not SYMBOL.fullmatch(code):
                raise ValueError("invalid subscription code")
            if self.channel == "personal:order" and not code.isdigit():
                raise ValueError("personal channel requires accountSeq")


def declaration(subscriptions: tuple[Subscription, ...]) -> str:
    keys = [f"{s.channel}:{code}" for s in subscriptions for code in s.codes]
    if len(keys) > 100 or len(keys) != len(set(keys)):
        raise ValueError("subscription limit or duplicates")
    return json.dumps([{"type": s.channel, "codes": list(s.codes)} for s in subscriptions])


def parse_frame(raw: str) -> dict:
    try:
        frame = json.loads(raw)
        if not isinstance(frame, dict):
            raise ValueError
        kind = frame.get("type")
        if kind == "subscriptions":
            if not isinstance(frame.get("subscribed"), list) or not isinstance(
                frame.get("rejected"), list
            ):
                raise ValueError
            if not all(isinstance(k, str) for k in frame["subscribed"]):
                raise ValueError
        elif kind == "message":
            if not isinstance(frame.get("topic"), str) or not isinstance(frame.get("data"), dict):
                raise ValueError
        elif kind == "error":
            if not isinstance(frame.get("error"), dict) or not isinstance(
                frame["error"].get("code"), str
            ):
                raise ValueError
        elif kind != "pong":
            raise ValueError
        return frame
    except (ValueError, TypeError):
        raise TossError("invalid_websocket_frame") from None


class TossStream:
    """Bounded reconnecting read-only stream.

    on_gap must durably mark observations stale; reconcile must refresh REST
    state. Neither success nor a WS ack proves full account reconciliation.
    Partial subscription rejection fails this PoC closed for operator correction.
    """

    def __init__(
        self,
        tokens: TokenManager,
        subscriptions: tuple[Subscription, ...],
        on_gap: Callable[[], Awaitable[None]],
        reconcile: Callable[[], Awaitable[None]],
        *,
        connector=connect,
        sleep=asyncio.sleep,
        jitter=random.random,
        max_reconnects=5,
        on_rejected=None,
    ):
        if not 0 <= max_reconnects <= 20:
            raise ValueError("invalid reconnect budget")
        self._tokens, self._subscriptions = tokens, subscriptions
        self._declaration = declaration(subscriptions)
        self._on_gap, self._reconcile = on_gap, reconcile
        self._connector, self._sleep, self._jitter = connector, sleep, jitter
        self._max_reconnects = max_reconnects
        self._on_rejected = on_rejected
        self._changed = asyncio.Event()
        self._applied = asyncio.Event()
        self._pending = None

    def update_subscriptions(self, subscriptions):
        payload = declaration(subscriptions)
        if payload != self._declaration:
            self._subscriptions, self._declaration = subscriptions, payload
            self._changed.set()

    def _accept(self, ack, expected):
        if ack["type"] != "subscriptions":
            raise TossError("subscription_ack_required")
        rejected = {r.get("target") for r in ack["rejected"]}
        if rejected:
            if (
                self._on_rejected is None
                or any(
                    not isinstance(k, str) or not k.startswith(("trade:kr:", "orderbook:kr:"))
                    for k in rejected
                )
                or not rejected.issubset(expected)
            ):
                raise SubscriptionRejected("subscription_not_fully_accepted")
        accepted = set(ack["subscribed"])
        if accepted != expected - rejected:
            raise SubscriptionRejected("subscription_not_fully_accepted")
        if rejected:
            self._on_rejected(rejected)
            symbols = {k.rsplit(":", 1)[-1] for k in rejected}
            self.update_subscriptions(
                tuple(
                    Subscription(s.channel, tuple(c for c in s.codes if c not in symbols))
                    for s in self._subscriptions
                    if any(c not in symbols for c in s.codes)
                )
            )
        return accepted

    async def _updates(self, socket):
        while True:
            await self._changed.wait()
            self._changed.clear()
            await self._on_gap()
            self._pending = {f"{s.channel}:{c}" for s in self._subscriptions for c in s.codes}
            self._applied.clear()
            await socket.send(self._declaration)
            try:
                await asyncio.wait_for(self._applied.wait(), timeout=45)
            except TimeoutError:
                await socket.close()
                return
            await asyncio.sleep(1)  # Well below the broker's 5 declarations/second.

    async def frames(self) -> AsyncIterator[dict]:
        for attempt in range(self._max_reconnects + 1):
            expected = {f"{s.channel}:{c}" for s in self._subscriptions for c in s.codes}
            initial_declaration = self._declaration
            self._pending = None
            retired = {}
            self._changed.clear()
            await self._on_gap()
            try:
                token = await self._tokens.get()
                async with self._connector(
                    WS_URL,
                    additional_headers={"Authorization": f"Bearer {token}"},
                    proxy=None,
                    ping_interval=None,
                    open_timeout=10,
                    close_timeout=5,
                    max_size=1024 * 1024,
                    max_queue=16,
                ) as socket:
                    await socket.send(initial_declaration)
                    ack = parse_frame(await asyncio.wait_for(socket.recv(), timeout=10))
                    expected = self._accept(ack, expected)
                    await asyncio.wait_for(self._reconcile(), timeout=30)
                    pong = asyncio.Event()
                    heartbeat = asyncio.create_task(self._heartbeat(socket, pong))
                    updates = asyncio.create_task(self._updates(socket))
                    try:
                        yield ack
                        async for raw in socket:
                            frame = parse_frame(raw)
                            if frame["type"] == "pong":
                                pong.set()
                            if frame["type"] == "error":
                                if frame["error"]["code"] == "server-shutdown":
                                    break
                                raise TossError("websocket_server_error")
                            if frame["type"] == "subscriptions":
                                if self._pending is None:
                                    raise TossError("unexpected_subscription_ack")
                                accepted = self._accept(frame, self._pending)
                                retired = {
                                    topic: until
                                    for topic, until in retired.items()
                                    if until > time.monotonic() and topic not in accepted
                                }
                                retired.update(
                                    {
                                        topic: time.monotonic() + 30
                                        for topic in expected - accepted
                                        if topic.startswith(("trade:", "orderbook:"))
                                    }
                                )
                                expected = accepted
                                self._pending = None
                                await asyncio.wait_for(self._reconcile(), timeout=30)
                                self._applied.set()
                            if frame["type"] == "message" and frame["topic"] not in expected:
                                topic = frame["topic"]
                                if topic.startswith(("trade:", "orderbook:")) and (
                                    topic in (self._pending or ())
                                    or retired.get(topic, 0) > time.monotonic()
                                ):
                                    # Discard in-flight market frames around a full
                                    # replacement. Never publish them as fresh data,
                                    # or suppress an unknown personal-order event.
                                    continue
                                raise TossError("unexpected_topic")
                            yield frame
                    finally:
                        heartbeat.cancel()
                        updates.cancel()
                        with suppress(asyncio.CancelledError, ConnectionClosed):
                            await heartbeat
                        with suppress(asyncio.CancelledError, ConnectionClosed):
                            await updates
            except InvalidStatus as error:
                if error.response.status_code not in {429, 500, 502, 503, 504}:
                    raise TossHTTPError(error.response.status_code) from None
            except (ConnectionClosed, OSError, TimeoutError):
                pass
            finally:
                await self._on_gap()
            if attempt < self._max_reconnects:
                await self._sleep(min(30, 2**attempt) + self._jitter())
        raise TossError("websocket_reconnect_budget_exhausted")

    async def _heartbeat(self, socket, pong):
        # Separate task keeps sending even while data frames arrive continuously.
        while True:
            await asyncio.sleep(60)
            try:
                pong.clear()
                await socket.send("PING")
                await asyncio.wait_for(pong.wait(), timeout=20)
            except TimeoutError:
                await self._on_gap()
                await socket.close()
                return
            except ConnectionClosed:
                return
