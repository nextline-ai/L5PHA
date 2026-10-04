"""Deterministic visible model input budgets; no network, credentials or raw logs."""

import hashlib
import json
import math

INPUT_BUDGETS = {"cheap": 12_000, "middle": 64_000, "research": 96_000}
SEARCH_INPUT_BUDGET = 8_000


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def measure_input(role, payload, system="", schema=None, task=None):
    """Count all supplied text plus the schema/tools, excluding provider internals.

    Token estimates are deliberately labeled, not used to claim billable savings.
    Actual provider token usage includes hidden framing and hosted search context.
    Field measurements contain sizes and types only, never the original values.
    """
    budget = SEARCH_INPUT_BUDGET if task == "native_search" else INPUT_BUDGETS[role]
    message = compact_json(payload)
    specification = compact_json(schema) if schema is not None else ""
    parts = [system, message, specification]
    sizes = [len(value.encode("utf-8")) for value in parts]
    total = sum(sizes)
    fields = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            item = {"bytes": len(compact_json(value).encode("utf-8")), "type": type(value).__name__}
            if isinstance(value, (dict, list)):
                item["items"] = len(value)
            fields[key] = item
    return {
        "payload_bytes": sizes[1],
        "system_bytes": sizes[0],
        "schema_bytes": sizes[2],
        "total_bytes": total,
        "budget_bytes": budget,
        "fields": fields,
        "sha256": hashlib.sha256(compact_json(parts).encode("utf-8")).hexdigest(),
        "estimated_tokens": math.ceil(total / 3),
        "estimate_kind": "utf8_bytes_div_3_not_provider_billing",
        "scope": "visible_input_excludes_provider_internal_context",
    }


def enforce_input(measurement, *, override=False):
    """Reject the entire call instead of silently losing risk or evidence fields."""
    if type(override) is not bool:
        raise ValueError("invalid_input_limit_override")
    if override:
        measurement["limit_overridden"] = True
        return
    if measurement["total_bytes"] > measurement["budget_bytes"]:
        raise ValueError("input_budget_exceeded")
