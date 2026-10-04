"""Explicit owner-authorized GPT-6 migration; never runs automatically on startup.

Re-verifies existing ciphertext in Lambda, then changes only model/reasoning and
selected strategy preset. Keep management stopped while invoking this utility.
"""

import argparse
import json
import time
from contextlib import closing

import boto3
from botocore.config import Config

from veyquant.operating_policy import OperatingPolicy
from veyquant.provider_connections import credentials
from veyquant.shadow_inference import MODEL_PRESETS, credential_selection, reasoning_selection
from veyquant.store import Store


def migrate(store, client, function, expected_revision, now):
    policy = OperatingPolicy(store)
    current = policy.view()
    if current["revision"] != expected_revision or not current["onboarding_completed"]:
        raise ValueError("owner_policy_changed")
    selected = MODEL_PRESETS["chatgpt"]
    response = client.invoke(
        FunctionName=function,
        InvocationType="RequestResponse",
        LogType="None",
        Payload=json.dumps(
            {
                "operation": "refresh_provider",
                "provider": "openai",
                "issued_at": now,
                "credential": credentials(store)["openai"],
            }
        ).encode(),
    )
    with response["Payload"] as stream:
        raw = stream.read(8193)
    if response.get("FunctionError") or len(raw) > 8192:
        raise ValueError("provider_refresh_failed")
    verified = json.loads(raw)
    credential_selection({"openai": verified})
    if not set(selected.values()).issubset(verified["models"]):
        raise ValueError("requested_models_unavailable")
    with store.transaction():
        if policy.view()["revision"] != expected_revision:
            raise ValueError("owner_policy_changed")
        store.db.execute(
            "UPDATE provider_connections SET credential=? WHERE provider='openai'",
            (json.dumps(verified),),
        )
    result = policy.save(
        current["limits"],
        str(expected_revision),
        now,
        models=selected,
        reasoning=reasoning_selection(selected),
        strategy=current["strategy"],
    )
    return {k: result[k] for k in ("revision", "models", "reasoning", "model_connection")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--function", required=True)
    parser.add_argument("--expected-revision", required=True, type=int)
    args = parser.parse_args()
    client = boto3.Session(region_name="ap-southeast-2").client(
        "lambda",
        config=Config(connect_timeout=5, read_timeout=180, retries={"total_max_attempts": 1}),
    )
    with closing(Store(args.db)) as store:
        result = migrate(store, client, args.function, args.expected_revision, time.time())
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
