"""Owner provider connections: only KMS ciphertext is persisted or exported internally."""

import asyncio
import json
import os
import pwd
import socket
import struct
from contextlib import suppress

import boto3
from botocore.config import Config

from veyquant.research_sources import connection_error
from veyquant.shadow_inference import PROVIDER_CAPABILITIES, credential_selection

SOCKET_PATH = "/run/veyquant-provider/bridge.sock"


def initialize(store):
    store.db.execute(
        "CREATE TABLE IF NOT EXISTS provider_connections "
        "(provider TEXT PRIMARY KEY, credential TEXT NOT NULL)"
    )


def credentials(store):
    initialize(store)
    return credential_selection(
        {
            row["provider"]: json.loads(row["credential"])
            for row in store.db.execute("SELECT * FROM provider_connections")
        }
    )


def public_connections(store):
    saved = credentials(store)
    return {
        provider: {
            "connected": provider in saved,
            "models": saved.get(provider, {}).get("models", []),
            "verified_at": saved.get(provider, {}).get("verified_at"),
            "verification": "service_access"
            if provider in {"brave", "dart", "krx"}
            else "model_access",
        }
        for provider in PROVIDER_CAPABILITIES
    }


async def request_connection(provider, key, path=SOCKET_PATH):
    async def exchange():
        reader, writer = await asyncio.open_unix_connection(path, limit=8192)
        try:
            writer.write(
                json.dumps(
                    {"operation": "configure_provider", "provider": provider, "api_key": key}
                ).encode()
                + b"\n"
            )
            await writer.drain()
            raw = await reader.readline()
            if not raw or len(raw) > 8192:
                raise ValueError("provider_connection_failed")
            result = json.loads(raw)
            if isinstance(result, dict) and set(result) == {"error"}:
                raise ValueError(connection_error(provider, result["error"]))
            credential_selection({provider: result})
            return result
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    return await asyncio.wait_for(exchange(), timeout=100)


async def serve_bridge():
    """Private owner-config bridge. No HTTP listener, storage, provider key logging or retries."""
    function_arn = os.environ["VEYQUANT_ANALYSIS_ARN"]
    owner_uid = pwd.getpwnam("veyweb").pw_uid
    client = boto3.Session(region_name="ap-southeast-2").client(
        "lambda",
        config=Config(connect_timeout=5, read_timeout=95, retries={"total_max_attempts": 1}),
    )
    lock = asyncio.Lock()

    def invoke(data):
        response = client.invoke(
            FunctionName=function_arn,
            InvocationType="RequestResponse",
            LogType="None",
            Payload=json.dumps(data).encode(),
        )
        with response["Payload"] as stream:
            raw = stream.read(8193)
        if response.get("FunctionError") or len(raw) > 8192:
            raise ValueError("provider_connection_failed")
        result = json.loads(raw)
        if isinstance(result, dict) and set(result) == {"error"}:
            return {"error": connection_error(data["provider"], result["error"])}
        credential_selection({data["provider"]: result})
        return result

    async def handle(reader, writer):
        result = {"error": "provider_connection_failed"}
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
                data = json.loads(raw)
                if (
                    len(raw) > 4096
                    or set(data) != {"operation", "provider", "api_key"}
                    or data["operation"] != "configure_provider"
                    or data["provider"] not in PROVIDER_CAPABILITIES
                ):
                    return
                result = await asyncio.to_thread(invoke, data)
        except Exception:
            # Never log key-bearing requests or reflected SDK/HTTP errors.
            pass
        finally:
            writer.write(json.dumps(result).encode() + b"\n")
            with suppress(OSError):
                await writer.drain()
            writer.close()

    with suppress(FileNotFoundError):
        os.unlink(SOCKET_PATH)
    server = await asyncio.start_unix_server(handle, path=SOCKET_PATH, limit=8192)
    os.chmod(SOCKET_PATH, 0o660)
    async with server:
        await server.serve_forever()


def main():
    asyncio.run(serve_bridge())


if __name__ == "__main__":
    main()
