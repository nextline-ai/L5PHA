"""Local provisioning helpers; never import these into the management API."""

import os
import re
from pathlib import Path

import httpx
from dotenv import dotenv_values


def read_local_config(path: str = ".env") -> dict[str, str]:
    if not Path(path).is_file():
        raise ValueError("configuration_file_missing")
    # No variable expansion; secret values containing ${...} remain literal.
    file_values = dotenv_values(path, interpolate=False)
    return {k: os.environ.get(k, v or "") for k, v in file_values.items()}


def config_presence(values: dict) -> dict:
    return {
        "configuration": {
            key: bool(values.get(key))
            for key in [
                "AWS_REGION",
                "TELEGRAM_BOT_USERNAME",
                "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_BOT_ID",
                "TOSS_CLIENT_ID",
                "TOSS_CLIENT_SECRET",
                "TOSS_ACCOUNT_SEQ",
                "VEYQUANT_PUBLIC_ORIGIN",
            ]
        },
        "secrets_displayed": False,
    }


async def inspect_telegram(token: str, expected_username: str, *, client=None) -> dict:
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        return {"status": "blocked", "reason": "bot_token_missing_or_invalid"}
    if not re.fullmatch(r"[A-Za-z0-9_]+", expected_username):
        return {"status": "blocked", "reason": "bot_username_missing_or_invalid"}
    owned = client is None
    client = (
        client
        if client is not None
        else httpx.AsyncClient(
            timeout=httpx.Timeout(10, connect=5),
            follow_redirects=False,
            trust_env=False,
        )
    )
    try:
        response = await client.get(
            f"https://api.telegram.org/bot{token}/getMe", follow_redirects=False
        )
        if response.status_code != 200:
            return {
                "status": "blocked",
                "reason": "telegram_request_denied",
                "http_status": response.status_code,
            }
        data = response.json()
        user = data.get("result", {})
        if data.get("ok") is not True or user.get("is_bot") is not True:
            raise ValueError
        if type(user.get("id")) is not int or user["id"] <= 0:
            raise ValueError
        if user.get("username", "").lower() != expected_username.lower():
            return {"status": "blocked", "reason": "unexpected_bot"}
        return {
            "status": "passed",
            "bot_id": user["id"],
            "username": user["username"],
            "level": "actual_telegram",
            "mini_app_verified": False,
        }
    except httpx.HTTPError:
        return {"status": "blocked", "reason": "telegram_transport_error"}
    except (ValueError, TypeError, AttributeError):
        return {"status": "blocked", "reason": "invalid_telegram_response"}
    finally:
        if owned:
            await client.aclose()
