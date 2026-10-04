"""Owner management with encrypted credentials and a bounded execution command queue."""

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, closing, suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from veyquant.account_readiness import read_account_view, readiness
from veyquant.collector import read_status
from veyquant.installation import installation_view
from veyquant.model_catalog import ROLE_FIELDS, catalog_view
from veyquant.operating_policy import FIELDS, OperatingPolicy, PolicyConflict
from veyquant.provider_connections import credentials, public_connections, request_connection
from veyquant.shadow_contract import atomic_json
from veyquant.shadow_inference import credential_selection
from veyquant.shadow_runner import read_view
from veyquant.store import Store
from veyquant.telegram_auth import (
    SESSION_TTL,
    AuthenticationError,
    OwnerAuth,
    TelegramVerifier,
    digest,
)
from veyquant.trading_state import read_execution
from veyquant.universe import MARKETS, read_universe


@dataclass(frozen=True)
class ManagementConfig:
    db_path: str
    public_origin: str
    bot_id: int
    development: bool = False
    collector_status_path: str | None = None
    shadow_control_path: str | None = None
    shadow_reports_path: str | None = None
    account_status_path: str | None = None
    execution_control_path: str | None = None
    execution_status_path: str | None = None
    universe_path: str | None = None

    def __post_init__(self):
        url = urlsplit(self.public_origin)
        local = url.hostname in {"localhost", "127.0.0.1", "::1"}
        if self.development and not local:
            raise ValueError("development mode is loopback only")
        if (
            not url.hostname
            or url.username
            or url.password
            or url.path
            or url.query
            or url.fragment
        ):
            raise ValueError("public_origin must be an exact origin without path")
        if url.scheme != "https" and not (self.development and local and url.scheme == "http"):
            raise ValueError("HTTPS required; HTTP development is loopback only")
        if type(self.bot_id) is not int or self.bot_id <= 0 or self.db_path == ":memory:":
            raise ValueError("persistent database and positive bot_id required")

    @property
    def cookie_name(self):
        return "veyquant_dev_session" if self.development else "__Host-veyquant_session"


def csrf_token(session: str) -> str:
    return hmac.new(session.encode(), b"veyquant-management-csrf-v1", hashlib.sha256).hexdigest()


async def body(
    request: Request, required: set[str], optional: set[str] | None = None, *, maximum: int = 20000
) -> dict:
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415, "json_required")
    if request.headers.get("content-encoding"):
        raise HTTPException(415, "encoded_body_unsupported")

    async def read():
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > maximum:
                raise HTTPException(413, "request_too_large")
        return data

    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError
        return result

    try:
        data = json.loads(await asyncio.wait_for(read(), timeout=5), object_pairs_hook=unique)
        if (
            not isinstance(data, dict)
            or not required.issubset(data)
            or set(data) - required - (optional or set())
        ):
            raise ValueError
        if any(not isinstance(v, str) or not v for v in data.values()):
            raise ValueError
        return data
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(400, "invalid_request") from None
    except TimeoutError:
        raise HTTPException(408, "request_timeout") from None


def limit_auth(store: Store, address: str, now: int):
    with store.transaction():
        store.db.execute("DELETE FROM auth_limits WHERE window < ?", (now // 60,))
        for key, maximum in [("all", 100), (digest(address), 10)]:
            count = store.db.execute(
                "SELECT count FROM auth_limits WHERE key=? AND window=?", (key, now // 60)
            ).fetchone()
            if count and count[0] >= maximum:
                raise HTTPException(429, "authentication_rate_limited")
        for key in ["all", digest(address)]:
            store.db.execute(
                "INSERT INTO auth_limits VALUES (?, ?, 1) "
                "ON CONFLICT(key, window) DO UPDATE SET count=count+1",
                (key, now // 60),
            )


class SecurityBoundary:
    def __init__(self, app, config):
        self.app, self.config = app, config

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        public_asset = request.method in {"GET", "HEAD"} and request.url.path in {
            "/",
            "/app.js",
            "/app.css",
            "/install",
            "/install/install.js",
            "/install/install.css",
        }

        async def secure_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers += [
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (
                        b"content-security-policy",
                        (
                            b"default-src 'none'; script-src 'self' https://telegram.org; "
                            b"style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                            b"img-src 'self' data:; "
                            b"base-uri 'none'; form-action 'none'; "
                            b"frame-ancestors https://web.telegram.org https://*.telegram.org"
                            if public_asset
                            else b"default-src 'none'; frame-ancestors 'none'"
                        ),
                    ),
                ]
                if not self.config.development:
                    headers.append((b"strict-transport-security", b"max-age=31536000"))
                message = message | {"headers": headers}
            await send(message)

        host = urlsplit(self.config.public_origin).netloc
        if len(request.headers.getlist("host")) != 1 or request.headers.get("host") != host:
            return await JSONResponse({"error": "invalid_host"}, 400)(scope, receive, secure_send)
        origins = request.headers.getlist("origin")
        unsafe = request.method not in {"GET", "HEAD"}
        if (
            len(origins) > 1
            or (origins and origins[0] != self.config.public_origin)
            or (unsafe and not origins)
            or (not public_asset and request.headers.get("sec-fetch-site") == "cross-site")
        ):
            return await JSONResponse({"error": "invalid_origin"}, 403)(scope, receive, secure_send)
        if (
            request.url.query
            and not public_asset
            and not (
                request.method in {"GET", "HEAD"}
                and request.url.path in {"/v1/universe", "/v1/analysis/history"}
            )
        ):
            return await JSONResponse({"error": "query_not_allowed"}, 400)(
                scope, receive, secure_send
            )
        return await self.app(scope, receive, secure_send)


def create_app(config: ManagementConfig, *, verifier=None, clock=time.time):
    verifier = verifier if verifier is not None else TelegramVerifier(config.bot_id)
    with closing(Store(config.db_path)) as store:
        OwnerAuth(store, verifier)
        OperatingPolicy(store)
        store.db.execute(
            "CREATE TABLE IF NOT EXISTS execution_requests "
            "(id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        if "result" not in {
            r["name"] for r in store.db.execute("PRAGMA table_info(execution_requests)")
        }:
            store.db.execute("ALTER TABLE execution_requests ADD COLUMN result TEXT")
        store.db.execute(
            "CREATE TABLE IF NOT EXISTS auth_limits (key TEXT, window INTEGER, "
            "count INTEGER NOT NULL, PRIMARY KEY(key, window))"
        )

    def publish_control():
        if config.shadow_control_path:
            with closing(Store(config.db_path)) as store:
                policy = OperatingPolicy(store).view()
                atomic_json(
                    config.shadow_control_path,
                    {
                        "updated_at": clock(),
                        "stopped": bool(
                            store.db.execute("SELECT stopped FROM controls WHERE id=1").fetchone()[
                                0
                            ]
                        ),
                        "owner_bound": bool(store.db.execute("SELECT 1 FROM owner").fetchone()),
                        "settings_revision": policy["revision"],
                        "models": policy["models"],
                        "reasoning": policy["reasoning"],
                        "strategy": policy["strategy"],
                        "prompts": policy["prompts"],
                        "provider_credentials": credentials(store),
                    },
                )
        if config.execution_control_path:
            with closing(Store(config.db_path)) as store:
                p = OperatingPolicy(store).view()
                view = read_execution(config.execution_status_path, clock())
                for command in view.get("commands", []):
                    if command.get("result") in {
                        "cancel_requested",
                        "attached",
                        "owner_confirmed_absent",
                        "not_applicable",
                        "review_required",
                        "lookup_failed",
                    }:
                        store.db.execute(
                            "UPDATE execution_requests SET result=? WHERE id=?",
                            (command["result"], command["id"]),
                        )
                atomic_json(
                    config.execution_control_path,
                    {
                        "version": 1,
                        "updated_at": clock(),
                        "owner_bound": bool(store.db.execute("SELECT 1 FROM owner").fetchone()),
                        "stopped": bool(
                            store.db.execute("SELECT stopped FROM controls WHERE id=1").fetchone()[
                                0
                            ]
                        ),
                        "policy": {
                            k: p[k]
                            for k in (
                                "configured",
                                "revision",
                                "updated_at",
                                "limits",
                                "onboarding_completed",
                                "live_requested",
                                "model_connection",
                            )
                        },
                        "commands": [
                            json.loads(r[0])
                            for r in store.db.execute(
                                "SELECT payload FROM execution_requests WHERE result IS NULL "
                                "ORDER BY created_at LIMIT 30"
                            )
                        ],
                    },
                )

    @asynccontextmanager
    async def lifespan(app):
        async def heartbeat():
            while True:
                publish_control()
                await asyncio.sleep(2)

        publish_control()
        task = (
            asyncio.create_task(heartbeat())
            if (config.shadow_control_path or config.execution_control_path)
            else None
        )
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                for path in (config.shadow_control_path, config.execution_control_path):
                    if path:
                        atomic_json(
                            path, {"updated_at": clock(), "stopped": True, "owner_bound": False}
                        )

    def authenticated(request, store):
        session = request.cookies.get(config.cookie_name, "")
        if len(session) != 43:
            raise AuthenticationError("invalid_session")
        auth = OwnerAuth(store, verifier)
        auth.authorize(session, int(clock()))
        if request.method == "POST" and not hmac.compare_digest(
            request.headers.get("x-veyquant-csrf", "").encode(), csrf_token(session).encode()
        ):
            raise HTTPException(403, "invalid_csrf")
        return auth, session

    async def installation(request):
        data = installation_view()
        if data is None:
            raise HTTPException(404, "installation_unavailable")
        return JSONResponse(
            {k: data[k] for k in ("product", "deployment_id", "bot_id", "version", "origin")}
        )

    async def setup_broker(request):
        from veyquant.installation import connect_broker, installation_view

        data = await body(request, {"client_id", "client_secret"}, {"account_seq"})
        data.setdefault("account_seq", "")
        with closing(Store(config.db_path)) as store:
            authenticated(request, store)
            limit_auth(store, "broker-setup", int(clock()))
        if installation_view() is None:
            raise HTTPException(404, "installation_unavailable")
        try:
            result = await connect_broker(data)
        except (OSError, ValueError, TimeoutError):
            raise HTTPException(503, "broker_setup_unavailable") from None
        return JSONResponse(result, status_code=409 if "error" in result else 200)

    async def health(request):
        return JSONResponse(
            {
                "status": "alive",
                "mode": "shadow" if config.shadow_reports_path else "paper",
                "live_order_supported": bool(config.execution_status_path),
            }
        )

    async def asset(request):
        name = {"/": "index.html", "/app.js": "app.js", "/app.css": "app.css"}[request.url.path]
        return FileResponse(Path(__file__).parent / "web" / name)

    async def install_asset(request):
        names = {
            "/install": "index.html",
            "/install/install.js": "install.js",
            "/install/install.css": "install.css",
            "/install/release.json": "release.json",
        }
        path = Path(__file__).resolve().parents[2] / "installer" / names[request.url.path]
        if not path.is_file():
            raise HTTPException(404, "installation_unavailable")
        return FileResponse(path)

    async def authenticate(request):
        now = int(clock())
        with closing(Store(config.db_path)) as store:
            # Ignore X-Forwarded-For. TLS proxy trust must be configured explicitly later.
            limit_auth(store, request.client.host if request.client else "unknown", now)
        required = (
            {"init_data", "invitation"} if request.url.path.endswith("bind") else {"init_data"}
        )
        data = await body(request, required)
        with closing(Store(config.db_path)) as store:
            auth = OwnerAuth(store, verifier)
            session = (
                auth.bind(data["invitation"], data["init_data"], now)
                if "invitation" in data
                else auth.login(data["init_data"], now)
            )
        response = session_response(session, SESSION_TTL)
        publish_control()
        return response

    def session_response(session, ttl):
        response = JSONResponse({"authenticated": True, "csrf_token": csrf_token(session)})
        response.set_cookie(
            config.cookie_name,
            session,
            max_age=ttl,
            path="/",
            secure=not config.development,
            httponly=True,
            samesite="strict",
        )
        return response

    async def renew(request):
        await body(request, set())
        with closing(Store(config.db_path)) as store:
            auth, session = authenticated(request, store)
            ttl = auth.renew(session, int(clock()))
        return session_response(session, ttl)

    async def universe_view(request):
        with closing(Store(config.db_path)) as store:
            authenticated(request, store)
        q, market, page = (request.query_params.get(k, "") for k in ("q", "market", "page"))
        if (
            set(request.query_params) - {"q", "market", "page"}
            or len(request.query_params.multi_items()) != len(request.query_params)
            or len(q) > 100
            or (market and market not in MARKETS)
            or (page and not re.fullmatch(r"[0-9]{1,3}", page))
        ):
            raise HTTPException(400, "invalid_universe_query")
        return JSONResponse(
            read_universe(config.universe_path, clock(), q.strip(), market, int(page or 0))
        )

    async def status(request):
        with closing(Store(config.db_path)) as store:
            _, session = authenticated(request, store)
            stopped = bool(
                store.db.execute("SELECT stopped FROM controls WHERE id=1").fetchone()[0]
            )
            count = store.db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]
            last = store.db.execute("SELECT MAX(seq) FROM audit").fetchone()[0]
            expires = store.db.execute(
                "SELECT expires_at FROM sessions WHERE token_hash=?", (digest(session),)
            ).fetchone()[0]
            policy = OperatingPolicy(store).view()
            provider_status = public_connections(store)
            command_results = [
                dict(r)
                for r in store.db.execute(
                    "SELECT id,result FROM execution_requests WHERE result IS NOT NULL "
                    "ORDER BY created_at DESC LIMIT 30"
                )
            ]
            catalog = catalog_view(credentials(store))
        collector = read_status(config.collector_status_path, clock())
        account = read_account_view(config.account_status_path, clock())
        execution = read_execution(config.execution_status_path, clock())
        execution["command_results"] = command_results
        if config.execution_status_path:
            matching = execution.get("settings_revision") == policy["revision"]
            policy["live_enabled"] = bool(
                matching and execution["live_enabled"] and policy["live_requested"] and not stopped
            )
            policy["live_state"] = (
                "active"
                if policy["live_enabled"]
                else "activation_pending"
                if policy["live_requested"]
                else "disabled"
            )
            policy["live_message"] = execution["message"]
        checks = readiness(policy, account, collector["connected"], stopped)
        if config.execution_status_path:
            for check in checks["checks"]:
                if check["id"] == "loss_ledger":
                    check["passed"] = (
                        execution.get("loss", {}).get("state") == "within_limit"
                        if execution.get("loss")
                        else False
                    )
                elif check["id"] == "execution":
                    check["passed"] = execution["ready"]
            checks["live_enabled"] = policy["live_enabled"]
        return JSONResponse(
            {
                "mode": "live"
                if policy["live_enabled"]
                else "shadow"
                if config.shadow_reports_path
                else "paper",
                "new_proposals_stopped": stopped,
                "paper_reservations": count,
                "last_audit_seq": last,
                "broker_connected": collector["connected"],
                "installation": installation_view(),
                "collector": collector,
                "analysis": read_view(config.shadow_reports_path, clock()),
                "operating_policy": policy,
                "model_catalog": catalog,
                "provider_connections": provider_status,
                "account": account,
                "readiness": checks,
                "execution": execution,
                "universe": {
                    k: v
                    for k, v in read_universe(config.universe_path, clock()).items()
                    if k != "items"
                },
                "live_order_supported": bool(config.execution_status_path),
                "csrf_token": csrf_token(session),
                "session_expires_at": expires,
            }
        )

    async def save_policy(request):
        data = await body(request, FIELDS | {"expected_revision"})
        with closing(Store(config.db_path)) as store:
            authenticated(request, store)
            expected = data.pop("expected_revision")
            try:
                policy = OperatingPolicy(store).save(data, expected, clock())
            except PolicyConflict:
                raise HTTPException(409, "policy_changed") from None
            except ValueError as error:
                raise HTTPException(400, str(error)) from None
        return JSONResponse({"operating_policy": policy})

    async def save_settings(request):
        data = await body(
            request,
            FIELDS | set(ROLE_FIELDS) | {"expected_revision"},
            {
                "strategy_preset",
                "strategy_prompt",
                "cheap_reasoning",
                "middle_reasoning",
                "research_reasoning",
                "cheap_prompt",
                "middle_prompt",
                "research_prompt",
                "live_requested",
            },
            maximum=32768,
        )
        prompt_fields = {r + "_prompt" for r in ROLE_FIELDS.values()}
        prompts = None
        if prompt_fields.intersection(data):
            if not prompt_fields.issubset(data):
                raise HTTPException(400, "incomplete_role_prompts")
            prompts = {r: data.pop(r + "_prompt") for r in ROLE_FIELDS.values()}
        strategy = None
        live_requested = data.pop("live_requested", None)
        reasoning_fields = {role + "_reasoning" for role in ROLE_FIELDS.values()}
        reasoning = None
        if reasoning_fields.intersection(data):
            if not reasoning_fields.issubset(data):
                raise HTTPException(400, "incomplete_reasoning")
            reasoning = {role: data.pop(role + "_reasoning") for role in ROLE_FIELDS.values()}
        if "strategy_preset" in data or "strategy_prompt" in data:
            if not {"strategy_preset", "strategy_prompt"}.issubset(data):
                raise HTTPException(400, "incomplete_strategy")
            strategy = {
                "preset": data.pop("strategy_preset"),
                "prompt": data.pop("strategy_prompt"),
            }
        with closing(Store(config.db_path)) as store:
            authenticated(request, store)
            expected = data.pop("expected_revision")
            models = {role: data.pop(field) for field, role in ROLE_FIELDS.items()}
            try:
                policy = OperatingPolicy(store).save(
                    data, expected, clock(), models, strategy, reasoning, live_requested, prompts
                )
            except PolicyConflict:
                raise HTTPException(409, "policy_changed") from None
            except ValueError as error:
                raise HTTPException(400, str(error)) from None
        publish_control()
        return JSONResponse({"operating_policy": policy})

    connection_lock = asyncio.Lock()

    async def live_preference(request):
        data = await body(request, {"enabled", "expected_revision"})
        with closing(Store(config.db_path)) as store:
            authenticated(request, store)
            try:
                policy = OperatingPolicy(store).set_live_preference(
                    data["enabled"], data["expected_revision"], clock()
                )
            except PolicyConflict:
                raise HTTPException(409, "policy_changed") from None
            except ValueError as error:
                raise HTTPException(400, str(error)) from None
        publish_control()
        return JSONResponse({"operating_policy": policy})

    async def execution_command(request):
        data = await body(
            request, {"order_id", "action", "expected_revision"}, {"broker_id", "confirmation"}
        )
        if not config.execution_control_path:
            raise HTTPException(409, "execution_unavailable")
        with closing(Store(config.db_path)) as store:
            authenticated(request, store)
            policy = OperatingPolicy(store).view()
            if str(policy["revision"]) != data["expected_revision"]:
                raise HTTPException(409, "policy_changed")
            action = data["action"]
            if action not in {"cancel", "attach", "confirm_absent"} or not re.fullmatch(
                r"vq[a-f0-9]{32}", data["order_id"]
            ):
                raise HTTPException(400, "invalid_execution_command")
            view = read_execution(config.execution_status_path, clock())
            order = next((o for o in view["orders"] if o["id"] == data["order_id"]), None)
            if order is None:
                raise HTTPException(409, "order_not_available")
            if action != "cancel":
                if policy["live_requested"] or order["state"] != "UNKNOWN":
                    raise HTTPException(409, "disable_before_recovery")
                if action == "confirm_absent" and clock() - order["created_at"] < 86400:
                    raise HTTPException(409, "wait_before_absence_confirmation")
                if data.get("confirmation") != "confirmed_in_toss":
                    raise HTTPException(400, "recovery_confirmation_required")
                if action == "attach" and not re.fullmatch(
                    r"[A-Za-z0-9_-]{1,200}", data.get("broker_id", "")
                ):
                    raise HTTPException(400, "invalid_broker_order")
            elif order["state"] not in {"ACKNOWLEDGED", "PARTIAL"}:
                raise HTTPException(409, "order_not_cancellable")
            if (
                store.db.execute(
                    "SELECT COUNT(*) FROM execution_requests WHERE result IS NULL"
                ).fetchone()[0]
                >= 30
            ):
                raise HTTPException(429, "execution_queue_full")
            command = {"id": secrets.token_hex(16), "order_id": data["order_id"], "action": action}
            if action == "attach":
                command["broker_id"] = data["broker_id"]
            store.db.execute(
                "INSERT INTO execution_requests(id,payload,created_at) VALUES (?,?,?)",
                (command["id"], json.dumps(command), clock()),
            )
            store.record(data["order_id"], "owner_execution_request", command)
        publish_control()
        return JSONResponse({"request_id": command["id"], "state": "queued"})

    async def connect_provider(request):
        with closing(Store(config.db_path)) as store:
            authenticated(request, store)
            limit_auth(store, "provider-connection", int(clock()))
        data = await body(request, {"provider", "api_key"})
        provider = data["provider"]
        if provider not in {"openai", "gemini", "brave", "dart", "krx"} or not re.fullmatch(
            r"[A-Za-z0-9_\-]{20,512}", data["api_key"]
        ):
            raise HTTPException(400, "invalid_provider_key")
        if connection_lock.locked():
            raise HTTPException(409, "provider_connection_busy")
        async with connection_lock:
            try:
                verified = await request_connection(provider, data.pop("api_key"))
                credential_selection({provider: verified})
            except (OSError, ValueError, TimeoutError) as error:
                from veyquant.research_sources import connection_error

                code = connection_error(provider, error)
                with closing(Store(config.db_path)) as store:
                    store.record(
                        "owner", "provider_connection_failed", {"provider": provider, "error": code}
                    )
                raise HTTPException(422, code) from None
            with closing(Store(config.db_path)) as store:
                authenticated(request, store)
                with store.transaction():
                    store.db.execute(
                        "INSERT INTO provider_connections VALUES(?,?) ON CONFLICT(provider) "
                        "DO UPDATE SET credential=excluded.credential",
                        (provider, json.dumps(verified)),
                    )
                    store.record(
                        "owner",
                        "provider_connected",
                        {"provider": provider, "verified_at": verified["verified_at"]},
                    )
                status = public_connections(store)
        publish_control()
        return JSONResponse({"provider_connections": status})

    async def disconnect_provider(request):
        data = await body(request, {"provider"})
        if data["provider"] not in {"dart", "krx"}:
            raise HTTPException(400, "invalid_optional_provider")
        # Serialize with key verification so a late connect cannot undo a disconnect.
        async with connection_lock:
            with closing(Store(config.db_path)) as store:
                authenticated(request, store)
                with store.transaction():
                    store.db.execute(
                        "DELETE FROM provider_connections WHERE provider=?", (data["provider"],)
                    )
                    store.record("owner", "provider_disconnected", {"provider": data["provider"]})
                status = public_connections(store)
        publish_control()
        return JSONResponse({"provider_connections": status})

    async def manual_analysis(request):
        from veyquant.decision_runtime import API_SOCKET
        from veyquant.research_context import exchange

        data = await body(request, {"instruction", "request_id"})
        with closing(Store(config.db_path)) as store:
            authenticated(request, store)
            limit_auth(store, "manual-analysis", int(clock()))
        if not 1 <= len(data["instruction"].strip()) <= 3000 or not re.fullmatch(
            r"[a-f0-9]{32}", data["request_id"]
        ):
            raise HTTPException(400, "invalid_manual_instruction")
        try:
            result = await exchange(API_SOCKET, {"operation": "manual", **data}, timeout=15)
        except (OSError, ValueError, TimeoutError):
            raise HTTPException(409, "analysis_not_ready") from None
        return JSONResponse(result, status_code=202 if result["accepted"] else 409)

    async def retry_analysis(request):
        from veyquant.decision_runtime import API_SOCKET
        from veyquant.research_context import exchange

        data = await body(request, {"request_id"})
        with closing(Store(config.db_path)) as store:
            authenticated(request, store)
            limit_auth(store, "manual-analysis", int(clock()))
        identity = request.path_params["identity"]
        if not all(
            isinstance(v, str) and re.fullmatch(r"[a-f0-9]{32}", v)
            for v in (identity, data["request_id"])
        ):
            raise HTTPException(400, "invalid_retry_request")
        try:
            result = await exchange(
                API_SOCKET, {"operation": "retry_input_limit", "id": identity, **data}, timeout=15
            )
        except (OSError, ValueError, TimeoutError):
            raise HTTPException(409, "analysis_not_ready") from None
        return JSONResponse(result, status_code=202 if result.get("accepted") else 409)

    async def analysis_history(request):
        from veyquant.decision_runtime import API_SOCKET
        from veyquant.research_context import exchange

        with closing(Store(config.db_path)) as store:
            authenticated(request, store)
        if set(request.query_params) - {"cursor"} or len(request.query_params.multi_items()) > 1:
            raise HTTPException(400, "invalid_history_cursor")
        cursor = request.query_params.get("cursor")
        if cursor is not None and len(cursor) > 400:
            raise HTTPException(400, "invalid_history_cursor")
        try:
            result = await exchange(
                API_SOCKET,
                {"operation": "history_page", "cursor": cursor},
                timeout=10,
                maximum=16 * 1024 * 1024,
            )
        except ValueError:
            raise HTTPException(400, "invalid_history_cursor") from None
        except (OSError, TimeoutError):
            raise HTTPException(503, "analysis_not_ready") from None
        return JSONResponse(result)

    async def analysis_detail(request):
        from veyquant.decision_runtime import API_SOCKET
        from veyquant.research_context import exchange

        with closing(Store(config.db_path)) as store:
            authenticated(request, store)
        identity = request.path_params["identity"]
        if not re.fullmatch(r"[a-f0-9]{32}", identity):
            raise HTTPException(400, "invalid_analysis_id")
        role = request.path_params.get("role")
        if role is not None and role not in {"middle", "research"}:
            raise HTTPException(400, "invalid_analysis_role")
        try:
            result = await exchange(
                API_SOCKET,
                {"operation": "layer_history", "id": identity, "role": role}
                if role
                else {"operation": "history", "id": identity},
                timeout=10,
                maximum=16 * 1024 * 1024,
            )
        except (OSError, ValueError, TimeoutError):
            raise HTTPException(404, "analysis_not_found") from None
        return JSONResponse(result)

    async def stop(request):
        await body(request, set())
        with closing(Store(config.db_path)) as store:
            authenticated(request, store)
            store.stop()
        publish_control()
        return JSONResponse({"new_proposals_stopped": True})

    async def resume(request):
        await body(request, set())
        with closing(Store(config.db_path)) as store:
            authenticated(request, store)
            with store.transaction():
                store.db.execute("UPDATE controls SET stopped=0 WHERE id=1")
                store.record("system", "resume", {"scope": "shadow_analysis"})
        publish_control()
        return JSONResponse({"new_proposals_stopped": False, "live_order_supported": False})

    async def revoke(request):
        await body(request, set())
        with closing(Store(config.db_path)) as store:
            auth, _ = authenticated(request, store)
            auth.revoke_sessions()
        response = JSONResponse({"sessions_revoked": True})
        response.delete_cookie(
            config.cookie_name,
            path="/",
            secure=not config.development,
            httponly=True,
            samesite="strict",
        )
        return response

    async def handle_error(request, error):
        if isinstance(error, HTTPException):
            return JSONResponse({"error": error.detail}, error.status_code)
        if isinstance(error, AuthenticationError):
            code = str(error)
            allowed = {
                "binding_required",
                "already_bound",
                "invalid_invitation",
                "owner_mismatch",
                "telegram_auth_expired",
                "authentication_replayed",
                "invalid_session",
            }
            return JSONResponse(
                {"error": code if code in allowed else "authentication_failed"}, 401
            )
        return JSONResponse({"error": "temporarily_unavailable"}, 503)

    app = Starlette(
        debug=False,
        lifespan=lifespan,
        routes=[
            Route("/", asset, methods=["GET"]),
            *[
                Route(path, install_asset, methods=["GET"])
                for path in (
                    "/install",
                    "/install/install.js",
                    "/install/install.css",
                    "/install/release.json",
                )
            ],
            Route("/app.js", asset, methods=["GET"]),
            Route("/app.css", asset, methods=["GET"]),
            Route("/healthz", health, methods=["GET"]),
            Route("/installation.json", installation, methods=["GET"]),
            Route("/v1/installation/broker", setup_broker, methods=["POST"]),
            Route("/v1/auth/bind", authenticate, methods=["POST"]),
            Route("/v1/auth/login", authenticate, methods=["POST"]),
            Route("/v1/auth/renew", renew, methods=["POST"]),
            Route("/v1/status", status, methods=["GET"]),
            Route("/v1/universe", universe_view, methods=["GET"]),
            Route("/v1/policy", save_policy, methods=["POST"]),
            Route("/v1/settings", save_settings, methods=["POST"]),
            Route("/v1/live-preference", live_preference, methods=["POST"]),
            Route("/v1/execution-command", execution_command, methods=["POST"]),
            Route("/v1/providers/connect", connect_provider, methods=["POST"]),
            Route("/v1/providers/disconnect", disconnect_provider, methods=["POST"]),
            Route("/v1/analysis/history", analysis_history, methods=["GET"]),
            Route("/v1/analysis/manual", manual_analysis, methods=["POST"]),
            Route("/v1/analysis/{identity}/retry-input-limit", retry_analysis, methods=["POST"]),
            Route("/v1/analysis/{identity}/layers/{role}", analysis_detail, methods=["GET"]),
            Route("/v1/analysis/{identity}", analysis_detail, methods=["GET"]),
            Route("/v1/control/stop", stop, methods=["POST"]),
            Route("/v1/control/resume", resume, methods=["POST"]),
            Route("/v1/auth/revoke", revoke, methods=["POST"]),
        ],
        exception_handlers={
            HTTPException: handle_error,
            AuthenticationError: handle_error,
            sqlite3.Error: handle_error,
            OSError: handle_error,
        },
    )
    return SecurityBoundary(app, config)
