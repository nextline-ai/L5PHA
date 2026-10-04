"""Read-only offline surveillance/native-search input audit; no provider or broker calls."""

import argparse
import ast
import copy
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from audit_model_context import encoded, measurement, private_directory, private_json, visible_input

from veyquant import model_context, native_search, shadow_inference


def surveillance_payload(source, signals):
    """Compile only pure projection functions and the capability payload expression.

    Runtime.__init__, imports and service methods are never executed. Source is the
    trusted local checkout under audit, not an expression from an archived record.
    """
    tree = ast.parse(Path(source).read_text())
    definitions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"surveillance_inputs", "surveillance_summary", "corroborated_signals"}
    ]
    namespace = {"SEVERITY_POLICY": model_context.SEVERITY_POLICY}
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(source), "exec"), namespace)
    runtime = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Runtime"
    )
    method = next(
        node
        for node in runtime.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "surveillance"
    )
    expression = next(
        keyword.value
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "capability"
        for keyword in node.keywords
        if keyword.arg == "payload"
    )
    return eval(
        compile(ast.Expression(expression), str(source), "eval"),
        namespace,
        {"signals": copy.deepcopy(signals)},
    )


def archived_rows(database, run_id=None):
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        surveillance = None
        for row in db.execute(
            "SELECT data FROM decision_evidence WHERE kind='surveillance' ORDER BY at DESC"
        ):
            event = json.loads(row[0])
            if isinstance(event.get("signals"), list) and event["signals"]:
                surveillance = event
                break
        query = (
            "SELECT * FROM decision_runs WHERE id=?"
            if run_id
            else (
                "SELECT * FROM decision_runs WHERE state IN ('complete','verified') "
                "ORDER BY finished DESC"
            )
        )
        runs = db.execute(query, (run_id,) if run_id else ())
        latest = searched = None
        for row in runs:
            run = dict(row)
            run["data"] = json.loads(run["data"])
            if not run["data"].get("initial_context"):
                continue
            latest = latest or run
            if any(
                item.get("tool") in {"search", "news"}
                for item in run["data"].get("tool_results", [])
            ):
                searched = run
                break
        if latest is None:
            raise ValueError("archived_run_unavailable")
        return surveillance, latest, searched


def native_capture(provider, model, effort, queries, now, role):
    captured, trace = {}, []

    class Captured(Exception):
        pass

    def request(_provider, _key, _path, body, **_kwargs):
        captured.update(copy.deepcopy(body))
        raise Captured

    try:
        native_search.native_search(
            provider,
            model,
            effort,
            "offline-audit-placeholder",
            queries,
            now,
            request,
            trace,
            shadow_inference.reasoning_budget(model, effort),
            role,
        )
    except Captured:
        pass
    if not captured:
        raise ValueError("offline_native_capture_failed")
    if provider == "openai":
        system, message = captured["instructions"], captured["input"]
    else:
        system = captured["systemInstruction"]["parts"][0]["text"]
        message = captured["contents"][0]["parts"][0]["text"]
    return {
        "system": system,
        "message": message,
        "specification": captured["tools"],
        "measurement": (trace[0].get("input_context") or {}) if trace else {},
    }


def usage(entry):
    return {
        key: entry[key]
        for key in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "search_queries",
            "search_tool_calls",
        )
        if type(entry.get(key)) is int
    }


def audit(database, runtime_source, run_id=None, output_dir=None, label="offline_auxiliary"):
    event, latest, searched = archived_rows(database, run_id)
    output = private_directory(output_dir) if output_dir else None
    records = []
    configuration = copy.deepcopy(latest["data"].get("configuration", {}))
    if event:
        entry = (event.get("trace") or [{}])[0]
        models = shadow_inference.model_selection(configuration.get("models"))
        if entry.get("model"):
            models["cheap"] = entry["model"]
        configuration["models"] = models
        payload = surveillance_payload(runtime_source, event["signals"])
        visible = visible_input("cheap", payload, configuration)
        records.append(
            measurement("cheap", "surveillance", payload, visible)
            | {
                "archived_signal_count": len(event["signals"]),
                "archived_provider_usage_not_new_billing": usage(entry),
            }
        )
        if output:
            private_json(
                output, "surveillance-input.json", {"payload": payload, "visible": visible}
            )
    if searched:
        data = searched["data"]
        config = data.get("configuration", {})
        models = shadow_inference.model_selection(config.get("models"))
        efforts = shadow_inference.reasoning_selection(models, config.get("reasoning"))
        names = {row[0]: row[1] for row in data["initial_context"]["comparison_table"]["rows"]}
        historical = list(data.get("trace") or [])
        used = set()
        for item in data.get("tool_results", []):
            if item.get("tool") not in {"search", "news"}:
                continue
            role = "research" if item["tool"] == "search" else "middle"
            model = models[role]
            provider = native_search.search_provider(model)
            if provider == "agentcore":
                continue
            args = item["arguments"]
            queries = [
                {"symbol": symbol, "name": names[symbol], "topic": topic}
                for symbol in dict.fromkeys(args["symbols"])
                for topic in dict.fromkeys(args["topics"])
            ]
            task = "web_search" if role == "research" else "news_research"
            entry = {}
            for index, old in enumerate(historical):
                if index not in used and old.get("role") == role and old.get("task") == task:
                    entry = old
                    used.add(index)
                    break
            visible = native_capture(
                provider, model, efforts[role], queries, searched["started"], role
            )
            payload = json.loads(visible["message"])
            records.append(
                measurement(role, "native_search", payload, visible)
                | {
                    "public_query_count": len(queries),
                    "archived_provider_usage_not_new_billing": usage(entry),
                }
            )
            if output:
                private_json(
                    output,
                    f"native-input-{len(records):03d}.json",
                    {"role": role, "payload": payload, "visible": visible},
                )
    for row in records:
        row["budget_exceeded"] = (
            row["total_bytes"] > row["budget_bytes"]
            if type(row.get("budget_bytes")) is int
            else None
        )
    summary = {
        "label": label,
        "scope": "offline_visible_input_only_no_network_or_new_provider_usage",
        "surveillance_configuration_source": "archived_model_plus_latest_completed_run_strategy",
        "native_provider_internal_context": (
            "unobservable; archived usage includes provider-generated retrieval context"
        ),
        "surveillance_found": event is not None,
        "native_search_run_found": searched is not None,
        "calls": records,
    }
    if output:
        private_json(output, "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--runtime-source", required=True)
    parser.add_argument("--run")
    parser.add_argument("--output-dir")
    parser.add_argument("--label", default="offline_auxiliary")
    args = parser.parse_args()
    try:
        result = audit(args.database, args.runtime_source, args.run, args.output_dir, args.label)
    except Exception as error:
        print(encoded({"error": type(error).__name__, "scope": "offline_auxiliary_audit_failed"}))
        return 1
    print(encoded(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
