"""Public installation metadata and owner-only initial broker setup transport."""

import ipaddress
import json
import os
import re
from contextlib import suppress
from pathlib import Path

from veyquant.research_context import exchange

SOCKET = "/run/veyquant-setup/setup.sock"
CONFIG = "/etc/veyquant-installation.json"
STATUS = "/var/lib/veyquant/setup/status.json"


def installation_view():
    try:
        data = json.loads(Path(os.environ.get("L5PHA_INSTALLATION_FILE", CONFIG)).read_text())
        ip = ipaddress.IPv4Address(data["ip"])
        if not ip.is_global or not re.fullmatch(r"[a-f0-9]{32}", data["deployment_id"]):
            return None
        result = {k: data[k] for k in ("deployment_id", "bot_id", "version", "ip")}
        result.update(product="L5PHA", origin=f"https://{ip}", broker_configured=False)
        with suppress(OSError, ValueError, TypeError):
            status = json.loads(Path(os.environ.get("L5PHA_SETUP_STATUS", STATUS)).read_text())
            result["broker_configured"] = status.get("broker_configured") is True
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return None


def validate_broker_input(data):
    if set(data) != {"client_id", "client_secret", "account_seq"}:
        raise ValueError("invalid_broker_credentials")
    for key in ("client_id", "client_secret"):
        if not isinstance(data[key], str) or not re.fullmatch(
            r"[A-Za-z0-9._~+/-]{8,512}={0,2}", data[key]
        ):
            raise ValueError("invalid_broker_credentials")
    if not isinstance(data["account_seq"], str) or not re.fullmatch(
        r"[0-9]{0,20}", data["account_seq"]
    ):
        raise ValueError("invalid_broker_credentials")
    return data


async def connect_broker(data):
    validate_broker_input(data)
    return await exchange(
        SOCKET, {"operation": "initial_broker_setup", **data}, timeout=90, maximum=4096
    )
