"""Explicit, bounded checks. Availability is never reported as invocation success."""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: dict


def failure(error: Exception) -> dict:
    if isinstance(error, ClientError):
        code = error.response.get("Error", {}).get("Code", "Unknown")
        message = error.response.get("Error", {}).get("Message", "").lower()
        if code == "AccessDeniedException" and "account is currently being verified" in message:
            return {"code": code, "reason": "account_verification_pending", "retry": False}
        return {
            "code": code,
            "reason": "aws_request_failed",
            "retry": code in {"ThrottlingException", "ServiceUnavailableException"},
        }
    return {"code": type(error).__name__, "reason": "aws_connection_unavailable", "retry": False}


def aws_checks(
    region: str,
    model_id: str | None = None,
    *,
    invoke: bool = False,
    expected_account: str | None = None,
    session=None,
) -> dict:
    if not region or (invoke and (not model_id or not expected_account)):
        raise ValueError("invocation requires region, model-id and expected-account")
    results: list[Check] = []
    config = Config(
        connect_timeout=5, read_timeout=30, retries={"mode": "adaptive", "total_max_attempts": 1}
    )
    try:
        session = session if session is not None else boto3.Session(region_name=region)
        identity = session.client("sts", config=config).get_caller_identity()
        account = identity["Account"]
        if expected_account and account != expected_account:
            results.append(Check("aws_identity", "blocked", {"reason": "account_mismatch"}))
            return report(region, results)
        results.append(
            Check(
                "aws_identity",
                "passed",
                {
                    "account_suffix": account[-4:],
                    "principal_type": "root"
                    if identity["Arn"].endswith(":root")
                    else "role_or_user",
                },
            )
        )
        bedrock = session.client("bedrock", config=config)
        models = bedrock.list_foundation_models()["modelSummaries"]
        results.append(
            Check(
                "model_catalog",
                "passed",
                {
                    "models": [
                        {
                            "id": m["modelId"],
                            "inference_types": m.get("inferenceTypesSupported", []),
                        }
                        for m in models
                    ]
                },
            )
        )
        if model_id:
            availability = bedrock.get_foundation_model_availability(modelId=model_id)
            details = {
                "model_id": model_id,
                "authorization": availability.get("authorizationStatus"),
                "agreement": availability.get("agreementAvailability", {}).get("status"),
                "entitlement": availability.get("entitlementAvailability"),
                "region": availability.get("regionAvailability"),
            }
            available = all(
                details[k] == v
                for k, v in {
                    "authorization": "AUTHORIZED",
                    "agreement": "AVAILABLE",
                    "entitlement": "AVAILABLE",
                    "region": "AVAILABLE",
                }.items()
            )
            results.append(
                Check("model_availability", "passed" if available else "blocked", details)
            )
            model = next((m for m in models if m["modelId"] == model_id), {})
            direct = "ON_DEMAND" in model.get("inferenceTypesSupported", [])
            if invoke and available and direct:
                response = session.client("bedrock-runtime", config=config).converse(
                    modelId=model_id,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "text": "Integration connectivity test. "
                                    "No personal data. Reply READY only."
                                }
                            ],
                        }
                    ],
                    inferenceConfig={"maxTokens": 32},
                )
                text = "".join(
                    b.get("text", "")
                    for b in response.get("output", {}).get("message", {}).get("content", [])
                )
                passed = response.get("stopReason") == "end_turn" and text.strip() == "READY"
                results.append(
                    Check(
                        "model_invocation",
                        "passed" if passed else "blocked",
                        {
                            "stop_reason": response.get("stopReason"),
                            "usage": response.get("usage", {}),
                            "expected_response": passed,
                        },
                    )
                )
            else:
                results.append(
                    Check(
                        "model_invocation",
                        "not_run",
                        {
                            "reason": "explicit_invoke_required"
                            if not invoke
                            else "availability_blocked"
                            if not available
                            else "inference_profile_required",
                        },
                    )
                )
    except (ClientError, BotoCoreError) as error:
        results.append(Check("aws_request", "blocked", failure(error)))
    return report(region, results)


def report(region: str, checks: list[Check]) -> dict:
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "region": region,
        "level": "actual_aws",
        "ready_for_live": False,
        "checks": [asdict(c) for c in checks],
        "invocation_verified": any(
            c.name == "model_invocation" and c.status == "passed" for c in checks
        ),
    }
