"""Narrow local provisioning service: first broker credential only, no trading commands.

Only the authenticated web process can use the Unix socket. Keys go directly to the
owner's Secrets Manager and machine-encrypted systemd credential; never to a model,
operator, database, command line, log, or public endpoint.
"""

import asyncio
import json
import os
import pwd
import socket
import struct
import subprocess
from contextlib import suppress
from pathlib import Path

import boto3
from botocore.config import Config

from veyquant.adapters.toss import Credentials, TokenManager, TossReadOnly, http_client
from veyquant.installation import SOCKET, STATUS, validate_broker_input
from veyquant.shadow_contract import atomic_json
from veyquant.toss_probe import select_account

CREDENTIAL = Path("/var/lib/veyquant-secrets/toss.cred")


async def verify(data):
    async with http_client() as http:
        tokens = TokenManager(Credentials(data["client_id"], data["client_secret"]), http)
        broker = TossReadOnly(tokens, http)
        return select_account(await broker.accounts(), data["account_seq"])


def persist(data, secret, client, credential=CREDENTIAL):
    value = json.dumps(
        {
            "TOSS_CLIENT_ID": data["client_id"],
            "TOSS_CLIENT_SECRET": data["client_secret"],
            "TOSS_ACCOUNT_SEQ": data["account_seq"],
        }
    )
    temporary = credential.with_name("toss.cred.next")
    try:
        sealed = subprocess.run(
            ["systemd-creds", "encrypt", "--name=toss", "-", str(temporary)],
            input=value.encode(),
            capture_output=True,
            timeout=20,
        )
        if sealed.returncode:
            raise ValueError("credential_sealing_failed")
        temporary.chmod(0o600)
        client.put_secret_value(SecretId=secret, SecretString=value)
        temporary.replace(credential)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


async def provision(data, *, client, secret, verify_fn=verify, persist_fn=persist):
    if CREDENTIAL.exists():
        atomic_json(STATUS, {"broker_configured": True})
        Path(STATUS).chmod(0o644)
        return {"configured": True, "connecting": True}
    validate_broker_input(data)
    try:
        account = await verify_fn(data)
    except ValueError as error:
        return {
            "error": "account_selection_required"
            if str(error) == "account_selection_required"
            else "broker_verification_failed"
        }
    except Exception:
        return {"error": "broker_verification_failed"}
    await asyncio.to_thread(persist_fn, data | {"account_seq": account}, secret, client)
    # Credentials are durable before the service is started. Retries never replace keys.
    atomic_json(STATUS, {"broker_configured": True})
    Path(STATUS).chmod(0o644)
    subprocess.run(
        ["systemctl", "reset-failed", "veyquant-collector"], capture_output=True, timeout=10
    )
    started = subprocess.run(
        ["systemctl", "start", "veyquant-collector"], capture_output=True, timeout=30
    )
    return {"configured": True, "connecting": started.returncode == 0}


async def serve():
    owner_uid = pwd.getpwnam("veyweb").pw_uid
    client = boto3.Session(region_name=os.environ["AWS_REGION"]).client(
        "secretsmanager",
        config=Config(connect_timeout=5, read_timeout=10, retries={"total_max_attempts": 2}),
    )
    lock = asyncio.Lock()

    async def handle(reader, writer):
        result = {"error": "broker_setup_unavailable"}
        try:
            _, uid, _ = struct.unpack(
                "3i",
                writer.get_extra_info("socket").getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, 12
                ),
            )
            if uid != owner_uid or lock.locked():
                return
            async with lock:
                raw = await asyncio.wait_for(reader.readline(), timeout=5)
                if len(raw) > 4096:
                    return
                data = json.loads(raw)
                if data.pop("operation", None) != "initial_broker_setup":
                    return
                result = await asyncio.wait_for(
                    provision(data, client=client, secret=os.environ["VEYQUANT_SECRET_ARN"]),
                    timeout=80,
                )
        except Exception:
            # No reflected broker/SDK exception can contain a submitted secret.
            pass
        finally:
            writer.write(json.dumps(result).encode() + b"\n")
            with suppress(OSError):
                await writer.drain()
            writer.close()

    with suppress(FileNotFoundError):
        os.unlink(SOCKET)
    server = await asyncio.start_unix_server(handle, path=SOCKET, limit=4096)
    os.chmod(SOCKET, 0o660)
    async with server:
        await server.serve_forever()


def main():
    asyncio.run(serve())


if __name__ == "__main__":
    main()
