"""Small public-data contract shared by collector, shadow runner and web boundary."""

import json
import math
import os
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

SYMBOLS = {"005930": ("KRW", "삼성전자"), "AAPL": ("USD", "Apple")}


def domestic_symbol(symbol):
    return isinstance(symbol, str) and re.fullmatch(r"[0-9A-Z]{6}", symbol) is not None


def atomic_json(path, value, mode=0o640):
    target = Path(path)
    temporary = target.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
    temporary.chmod(mode)
    os.replace(temporary, target)


def bounded_json(path, maximum=131072):
    with Path(path).open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("oversize_document")
    return json.loads(raw)


def finite_time(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def money(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,8})?", value):
        raise ValueError("invalid_price")
    if Decimal(value) <= 0:
        raise ValueError("invalid_price")
    return value


def market_quote(symbol, data, received_at):
    currency = "KRW" if domestic_symbol(symbol) else SYMBOLS[symbol][0]
    if data.get("currency") != currency:
        raise ValueError("currency_mismatch")
    stamp = datetime.fromisoformat(data["timestamp"])
    if stamp.tzinfo is None:
        raise ValueError("naive_market_time")
    as_of = stamp.timestamp()
    if not finite_time(as_of) or as_of > received_at + 5:
        raise ValueError("future_market_time")
    return {
        "symbol": symbol,
        "currency": currency,
        "price": money(data["price"]),
        "as_of": as_of,
        "received_at": received_at,
    }


def read_control(path, now):
    try:
        value = bounded_json(path, 32768)
        if not finite_time(value["updated_at"]) or not 0 <= now - value["updated_at"] <= 15:
            return False
        return value.get("owner_bound") is True and value.get("stopped") is False
    except (OSError, ValueError, KeyError, TypeError):
        return False


def read_model_configuration(path, now):
    from veyquant.model_prompts import prompt_selection
    from veyquant.shadow_inference import (
        credential_selection,
        model_selection,
        reasoning_selection,
        strategy_selection,
    )

    data = bounded_json(path, 65536)
    if (
        not finite_time(data["updated_at"])
        or not 0 <= now - data["updated_at"] <= 15
        or data.get("owner_bound") is not True
        or data.get("stopped") is not False
    ):
        raise ValueError("inactive_control")
    revision = data.get("settings_revision", 0)
    if type(revision) is not int or revision < 0:
        raise ValueError("invalid_settings_revision")
    return {
        "models": model_selection(data.get("models")),
        "reasoning": reasoning_selection(data.get("models"), data.get("reasoning")),
        "settings_revision": revision,
        "strategy": strategy_selection(data.get("strategy")),
        "prompts": prompt_selection(data.get("prompts")),
        **(
            {"provider_credentials": credential_selection(data["provider_credentials"])}
            if data.get("provider_credentials")
            else {}
        ),
    }


def read_market(path, now):
    value = bounded_json(path, 1048576)
    if value.get("connected") is not True or not 0 <= now - value["updated_at"] <= 15:
        raise ValueError("stale_market_export")
    if value.get("universe_ready") is False:
        raise ValueError("universe_unavailable")
    result = []
    for q in value["quotes"]:
        symbol = q["symbol"]
        if not domestic_symbol(symbol):
            continue
        instrument = value.get("instruments", {}).get(symbol)
        if instrument is not None and instrument.get("eligibility") != "eligible":
            continue
        if q["currency"] != "KRW":
            raise ValueError("invalid_market_scope")
        money(q["price"])
        for key in ("as_of", "received_at"):
            if not finite_time(q[key]) or not 0 <= now - q[key] <= 180:
                break
        else:
            result.append(
                {k: q[k] for k in ("symbol", "currency", "price", "as_of", "received_at")}
            )
    return result
