"""Owner-authored KRW limits and explicit live intent; worker readiness is independent."""

import json
import re

from veyquant.model_catalog import connection_view
from veyquant.model_prompts import prompt_selection
from veyquant.provider_connections import credentials, initialize
from veyquant.shadow_inference import (
    STRATEGY_PRESETS,
    model_selection,
    reasoning_selection,
    strategy_selection,
)
from veyquant.store import Store

FIELDS = {"capital_krw", "max_order_krw", "max_daily_loss_krw"}


class PolicyConflict(ValueError):
    pass


def validate_limits(data: dict) -> dict:
    if set(data) != FIELDS:
        raise ValueError("invalid_policy")
    # Money enters as canonical whole-won strings, never floats or exponents.
    if any(
        not isinstance(v, str) or not re.fullmatch(r"[1-9][0-9]{0,11}", v) for v in data.values()
    ):
        raise ValueError("invalid_policy_amount")
    if int(data["max_order_krw"]) > int(data["capital_krw"]):
        raise ValueError("order_exceeds_capital")
    if int(data["max_daily_loss_krw"]) > int(data["capital_krw"]):
        raise ValueError("loss_exceeds_capital")
    return dict(data)


def saved_strategy(value):
    if isinstance(value, dict) and value.get("preset") in STRATEGY_PRESETS:
        preset = value["preset"]
        return strategy_selection({"preset": preset, "prompt": STRATEGY_PRESETS[preset]["prompt"]})
    return strategy_selection(value)


class OperatingPolicy:
    def __init__(self, store: Store):
        self.store = store
        initialize(store)
        store.db.execute("""
            CREATE TABLE IF NOT EXISTS operating_policy (
                id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL,
                limits TEXT NOT NULL, updated_at REAL NOT NULL
            )
        """)
        columns = {row["name"] for row in store.db.execute("PRAGMA table_info(operating_policy)")}
        if "prompts" not in columns:
            store.db.execute("ALTER TABLE operating_policy ADD COLUMN prompts TEXT")
        if "models" not in columns:
            store.db.execute("ALTER TABLE operating_policy ADD COLUMN models TEXT")

        if "strategy" not in columns:
            store.db.execute("ALTER TABLE operating_policy ADD COLUMN strategy TEXT")
        if "reasoning" not in columns:
            store.db.execute("ALTER TABLE operating_policy ADD COLUMN reasoning TEXT")
        if "live_requested" not in columns:
            store.db.execute(
                "ALTER TABLE operating_policy ADD COLUMN live_requested INTEGER NOT NULL DEFAULT 0"
            )
        if "execution_consent_version" not in columns:
            # 0.9 explicitly promised that its pending preference could not place orders.
            # Require a fresh owner choice after installing an actual execution worker.
            with store.transaction():
                store.db.execute(
                    "ALTER TABLE operating_policy ADD COLUMN execution_consent_version "
                    "INTEGER NOT NULL DEFAULT 1"
                )
                changed = store.db.execute(
                    "UPDATE operating_policy SET live_requested=0,revision=revision+1 "
                    "WHERE live_requested=1"
                ).rowcount
                if changed:
                    store.record("operating_policy", "live_worker_requires_fresh_opt_in", {})

    def view(self) -> dict:
        row = self.store.db.execute("SELECT * FROM operating_policy WHERE id=1").fetchone()
        models = json.loads(row["models"]) if row and row["models"] else model_selection()
        legacy = any(m.startswith("amazon.nova") for m in models.values())
        reasoning = (
            {}
            if legacy
            else reasoning_selection(
                models, json.loads(row["reasoning"]) if row and row["reasoning"] else None
            )
        )
        return {
            "prompts": prompt_selection(
                json.loads(row["prompts"]) if row and row["prompts"] else None
            ),
            "reasoning": reasoning,
            "model_reselection_required": legacy,
            "strategy": saved_strategy(json.loads(row["strategy"]))
            if row and row["strategy"]
            else strategy_selection(),
            "strategy_configured": bool(row and row["strategy"]),
            "model_connection": connection_view(models, credentials(self.store)),
            "revision": row["revision"] if row else 0,
            "configured": bool(row),
            "limits": validate_limits(json.loads(row["limits"])) if row else None,
            "updated_at": row["updated_at"] if row else None,
            "models": models,
            "onboarding_completed": bool(row and row["models"] and not legacy),
            "order_mode": "automatic_within_limits",
            "market": "KR",
            "currency": "KRW",
            "live_enabled": False,
            "live_requested": bool(row and row["live_requested"]),
            "live_state": "activation_pending" if row and row["live_requested"] else "disabled",
            "live_message": "실거래를 요청했습니다. 계좌·손실 한도 확인 후 자동 운용을 시작합니다."
            if row and row["live_requested"]
            else "실거래를 활성화하면 설정한 한도 안에서 자동 주문합니다.",
        }

    def save(
        self,
        data: dict,
        expected_revision: str,
        now: float,
        models: dict | None = None,
        strategy: dict | None = None,
        reasoning: dict | None = None,
        live_requested: str | None = None,
        prompts: dict | None = None,
    ) -> dict:
        limits = validate_limits(data)
        if prompts is not None:
            prompts = prompt_selection(prompts)
        if models is not None:
            models = model_selection(models)
        if strategy is not None:
            strategy = strategy_selection(strategy)
        if live_requested is not None and live_requested not in {"true", "false"}:
            raise ValueError("invalid_live_preference")
        if not re.fullmatch(r"0|[1-9][0-9]{0,9}", expected_revision):
            raise ValueError("invalid_policy_revision")
        with self.store.transaction():
            previous = self.view()
            if previous["revision"] != int(expected_revision):
                raise PolicyConflict("policy_changed")
            revision = previous["revision"] + 1
            selected = (
                models
                if models is not None
                else previous["models"]
                if previous["onboarding_completed"] or previous["model_reselection_required"]
                else None
            )
            selected_strategy = (
                strategy
                if strategy is not None
                else previous["strategy"]
                if previous["strategy_configured"]
                else None
            )
            selected_reasoning = (
                {}
                if selected and any(m.startswith("amazon.nova") for m in selected.values())
                else reasoning_selection(
                    selected,
                    reasoning
                    if reasoning is not None
                    else previous["reasoning"]
                    if selected == previous["models"] and previous["reasoning"]
                    else None,
                )
            )
            requested = (
                previous["live_requested"] if live_requested is None else live_requested == "true"
            )
            self.store.db.execute(
                "INSERT INTO operating_policy(id,revision,limits,updated_at,models,strategy) "
                "VALUES (1, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET revision=excluded.revision, "
                "limits=excluded.limits, updated_at=excluded.updated_at, "
                "models=excluded.models, strategy=excluded.strategy",
                (
                    revision,
                    json.dumps(limits),
                    now,
                    json.dumps(selected) if selected else None,
                    json.dumps(selected_strategy) if selected_strategy else None,
                ),
            )
            self.store.db.execute(
                "UPDATE operating_policy SET reasoning=?, live_requested=?,prompts=? WHERE id=1",
                (
                    json.dumps(selected_reasoning),
                    int(requested),
                    json.dumps(prompts if prompts is not None else previous["prompts"]),
                ),
            )
            self.store.record(
                "operating_policy",
                "policy_saved",
                {
                    "revision": revision,
                    "previous_revision": previous["revision"],
                    "limits": limits,
                    "models": selected,
                    "reasoning": selected_reasoning,
                    "live_requested": requested,
                    "strategy_preset": selected_strategy["preset"] if selected_strategy else None,
                    "updated_at": now,
                    "live_enabled": False,
                },
            )
        return self.view()

    def set_live_preference(self, enabled: str, expected_revision: str, now: float):
        if enabled not in {"true", "false"}:
            raise ValueError("invalid_live_preference")
        with self.store.transaction():
            previous = self.view()
            if str(previous["revision"]) != expected_revision:
                raise PolicyConflict("policy_changed")
            if not previous["onboarding_completed"]:
                raise ValueError("onboarding_required")
            self.store.db.execute(
                "UPDATE operating_policy SET live_requested=?,revision=revision+1,"
                "updated_at=? WHERE id=1",
                (int(enabled == "true"), now),
            )
            self.store.record(
                "operating_policy",
                "live_preference_saved",
                {
                    "live_requested": enabled == "true",
                    "live_enabled": False,
                    "revision": previous["revision"] + 1,
                },
            )
        return self.view()
