"""Apply L5PHA bot metadata and Mini App menu; never send a chat message."""

import json
import logging
from pathlib import Path

import httpx

from veyquant.local_config import read_local_config


def main():
    logging.disable(logging.CRITICAL)
    values = read_local_config(".env")
    origin = values["VEYQUANT_PUBLIC_ORIGIN"]
    if origin != "https://15.134.164.178":
        raise SystemExit("unexpected_origin")
    try:
        with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
            base = "https://api.telegram.org/bot" + values["TELEGRAM_BOT_TOKEN"]
            identity = client.get(base + "/getMe").json()
            if not identity.get("ok") or identity["result"].get("username") != "veyquant_bot":
                raise ValueError
            before = client.post(base + "/getChatMenuButton", json={}).json()
            if not before.get("ok"):
                raise ValueError
            Path("var").mkdir(exist_ok=True)
            backup = Path("var/telegram-menu-before.json")
            if not backup.exists():
                backup.write_text(json.dumps(before["result"]))
            branding = {
                "Name": ("name", "L5PHA"),
                "ShortDescription": (
                    "short_description",
                    "Investment has reached Level 5. 레벨 5 완전 자율 투자",
                ),
                "Description": (
                    "description",
                    "L5PHA — Level 5 Fully Autonomous Investing\n\n"
                    "Investment has reached Level 5. 레벨 5 완전 자율 투자",
                ),
            }
            previous = {}
            for language in ("", "ko", "en"):
                previous[language] = {}
                for method in branding:
                    response = client.post(
                        base + "/getMy" + method, json={"language_code": language}
                    ).json()
                    if not response.get("ok"):
                        raise ValueError
                    previous[language][method] = response["result"]
            brand_backup = Path("var/telegram-brand-before-l5pha.json")
            if not brand_backup.exists():
                brand_backup.write_text(json.dumps(previous, ensure_ascii=False))
            for language in ("", "ko", "en"):
                for method, (field, value) in branding.items():
                    response = client.post(
                        base + "/setMy" + method,
                        json={"language_code": language, field: value},
                    ).json()
                    checked = client.post(
                        base + "/getMy" + method, json={"language_code": language}
                    ).json()
                    if not response.get("ok") or checked.get("result", {}).get(field) != value:
                        raise ValueError
            menu = {"type": "web_app", "text": "L5PHA", "web_app": {"url": origin + "/"}}
            changed = client.post(base + "/setChatMenuButton", json={"menu_button": menu}).json()
            after = client.get(base + "/getChatMenuButton").json()
            if not changed.get("ok") or after.get("result") != menu:
                raise ValueError
    except Exception:
        raise SystemExit("telegram_menu_configuration_failed") from None
    print(
        json.dumps({"brand": "L5PHA", "menu_configured": True, "url": origin, "messages_sent": 0})
    )


if __name__ == "__main__":
    main()
