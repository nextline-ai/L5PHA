"""Shared access bot: only an explicit /start selects that user's HTTPS deployment.

This process has no investment/broker API and never receives an invitation code.
Telegram supplies the requesting chat identity. The bot token stays with the operator.
"""

import asyncio
import ipaddress
import json
import os
import re
import time
from pathlib import Path

import httpx

from veyquant.shadow_contract import atomic_json


def deployment(text):
    match = re.fullmatch(
        r"/start(?:@[A-Za-z0-9_]+)? l5_(\d{1,3})_(\d{1,3})_(\d{1,3})_(\d{1,3})_([a-f0-9]{32})", text
    )
    if not match:
        raise ValueError("invalid_deployment_link")
    ip = ipaddress.IPv4Address(".".join(match.groups()[:4]))
    if not ip.is_global:
        raise ValueError("invalid_deployment_link")
    return "https://" + str(ip), match[5]


async def verify_deployment(http, origin, identity, bot_id):
    # Literal public IPv4 only, fixed HTTPS port/path, no redirects or DNS rebinding.
    async with http.stream(
        "GET", origin + "/installation.json", follow_redirects=False
    ) as response:
        response.raise_for_status()
        data = bytearray()
        async for part in response.aiter_bytes():
            data.extend(part)
            if len(data) > 2048:
                raise ValueError("invalid_deployment")
    payload = json.loads(data)
    if (
        payload.get("product") != "L5PHA"
        or payload.get("deployment_id") != identity
        or str(payload.get("bot_id")) != str(bot_id)
        or payload.get("origin") != origin
    ):
        raise ValueError("deployment_mismatch")


async def handle(update, http, call, *, bot_id, install_url):
    message = update.get("message", {})
    chat = message.get("chat", {})
    sender = message.get("from", {})
    if (
        chat.get("type") != "private"
        or type(chat.get("id")) is not int
        or chat.get("id") != sender.get("id")
        or sender.get("is_bot")
    ):
        return
    text = message.get("text", "")
    if not text.startswith("/start"):
        return  # Never echo pasted keys or treat free-text as commands.
    if text in {"/start", "/start@veyquant_bot"}:
        await call(
            "sendMessage",
            {
                "chat_id": chat["id"],
                "text": "L5PHA를 내 AWS에 설치하세요. 연결 코드와 API 키는 채팅에 보내지 마세요.",
                "reply_markup": {
                    "inline_keyboard": [
                        [{"text": "L5PHA 설치 시작", "web_app": {"url": install_url}}]
                    ]
                },
            },
        )
        return
    try:
        origin, identity = deployment(text)
        await verify_deployment(http, origin, identity, bot_id)
    except (ValueError, httpx.HTTPError):
        await call(
            "sendMessage",
            {
                "chat_id": chat["id"],
                "text": (
                    "설치 주소를 확인하지 못했습니다. AWS가 CREATE_COMPLETE인지 확인하고 "
                    "출력 탭의 OpenTelegram 링크를 다시 열어주세요."
                ),
            },
        )
        return
    await call(
        "setChatMenuButton",
        {
            "chat_id": chat["id"],
            "menu_button": {"type": "web_app", "text": "내 L5PHA", "web_app": {"url": origin}},
        },
    )
    await call(
        "sendMessage",
        {
            "chat_id": chat["id"],
            "text": "내 AWS 주소: "
            + origin
            + "\n아래 화면에서 최초 연결 코드를 입력하세요. 코드는 채팅에 보내지 마세요.",
            "reply_markup": {
                "inline_keyboard": [[{"text": "내 L5PHA 열기", "web_app": {"url": origin}}]]
            },
        },
    )


async def run():
    token = (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "bot").read_text().strip()
    base = "https://api.telegram.org/bot" + token + "/"
    state = Path(os.environ.get("L5PHA_BOT_STATE", "/var/lib/l5pha-access/offset.json"))
    offset = json.loads(state.read_text()).get("offset", 0) if state.exists() else 0
    install_url = os.environ["L5PHA_INSTALL_URL"]
    last = {}
    async with (
        httpx.AsyncClient(timeout=40, trust_env=False, follow_redirects=False) as api,
        httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as public,
    ):

        async def call(method, data):
            result = await api.post(base + method, json=data)
            if result.status_code != 200 or result.json().get("ok") is not True:
                raise ValueError("telegram_request_failed")
            return result.json()["result"]

        me = await call("getMe", {})
        if me.get("username") != "veyquant_bot":
            raise ValueError("unexpected_bot")
        while True:
            try:
                updates = await call(
                    "getUpdates",
                    {"offset": offset, "timeout": 25, "limit": 20, "allowed_updates": ["message"]},
                )
                for update in updates:
                    user = update.get("message", {}).get("from", {}).get("id")
                    now = time.monotonic()
                    if user and now - last.get(user, 0) > 10:
                        last[user] = now
                        await handle(update, public, call, bot_id=me["id"], install_url=install_url)
                    offset = update["update_id"] + 1
                    atomic_json(str(state), {"offset": offset})
                last = {k: v for k, v in last.items() if time.monotonic() - v < 60}
            except (ValueError, httpx.HTTPError, OSError):
                # Never log Telegram URLs, token, user text or response bodies.
                await asyncio.sleep(5)


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
