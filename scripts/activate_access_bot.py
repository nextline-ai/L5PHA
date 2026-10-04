"""Operator-only menu migration. Preserve the existing owner; never send a message."""

import logging
import sqlite3
import subprocess

import httpx


def main():
    logging.disable(logging.CRITICAL)
    try:
        result = subprocess.run(
            [
                "systemd-creds",
                "decrypt",
                "--name=bot",
                "/var/lib/l5pha-access-secret/bot.cred",
                "-",
            ],
            capture_output=True,
            check=True,
            timeout=15,
        )
        token = result.stdout.decode().strip()
        with sqlite3.connect(
            "file:/var/lib/veyquant/management/state.sqlite3?mode=ro", uri=True
        ) as db:
            owner = db.execute("SELECT telegram_id FROM owner").fetchone()
        if not owner:
            raise ValueError("owner_missing")
        with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as http:
            base = "https://api.telegram.org/bot" + token + "/"

            def call(method, payload):
                response = http.post(base + method, json=payload).json()
                if not response.get("ok"):
                    raise ValueError("telegram_failed")
                return response["result"]

            if call("getMe", {}).get("username") != "veyquant_bot":
                raise ValueError("unexpected_bot")
            if call("getWebhookInfo", {}).get("url"):
                raise ValueError("existing_webhook")
            personal = {
                "type": "web_app",
                "text": "내 L5PHA",
                "web_app": {"url": "https://15.134.164.178/"},
            }
            call("setChatMenuButton", {"chat_id": owner[0], "menu_button": personal})
            if call("getChatMenuButton", {"chat_id": owner[0]}) != personal:
                raise ValueError("owner_menu_failed")
            public = {
                "type": "web_app",
                "text": "L5PHA 설치",
                "web_app": {"url": "https://15.134.164.178/install"},
            }
            call("setChatMenuButton", {"menu_button": public})
            if call("getChatMenuButton", {}) != public:
                raise ValueError("public_menu_failed")
    except Exception:
        raise SystemExit("access_bot_activation_failed") from None
    print('{"existing_owner_preserved":true,"public_install_menu":true,"messages_sent":0}')


if __name__ == "__main__":
    main()
