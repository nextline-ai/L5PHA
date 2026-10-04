"""AWS-admin-only recovery. No HTTP endpoint and no broker/order operation."""

import argparse
import json
import time
from pathlib import Path

from veyquant.store import Store
from veyquant.telegram_auth import OwnerAuth, TelegramVerifier


def recover(store, action, bot_id, now):
    auth = OwnerAuth(store, TelegramVerifier(bot_id))
    if action == "pause":
        store.stop()
        return {
            "new_proposals_stopped": True,
            "message": "신규 판단 중단을 요청했습니다. 토스에서 미체결 상태도 확인하세요.",
        }
    if action == "revoke-sessions":
        auth.revoke_sessions()
        return {"sessions_revoked": True}
    if action == "new-code":
        code = auth.issue_invitation(now)
        return {
            "one_time_code": code,
            "expires_in_seconds": 600,
            "message": "Telegram 미니앱의 최초 연결 입력란에만 사용하세요.",
        }
    if action == "disconnect-telegram":
        with store.transaction():
            store.db.execute("UPDATE controls SET stopped=1 WHERE id=1")
            store.db.execute("DELETE FROM sessions")
            store.db.execute("DELETE FROM owner")
            store.db.execute("DELETE FROM invitations")
            # Stop persists through rebinding; a new owner cannot auto-resume trading.
            store.record("aws-admin", "disconnect_telegram", {"stopped": True})
        return {"telegram_disconnected": True, "new_proposals_stopped": True}
    raise ValueError("unsupported_recovery_action")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=["pause", "revoke-sessions", "new-code", "disconnect-telegram"]
    )
    args = parser.parse_args()
    config = json.loads(Path("/etc/veyquant-installation.json").read_text())
    store = Store("/var/lib/veyquant/management/state.sqlite3")
    try:
        print(
            json.dumps(
                recover(store, args.action, int(config["bot_id"]), int(time.time())),
                ensure_ascii=False,
            )
        )
    finally:
        store.close()


if __name__ == "__main__":
    main()
