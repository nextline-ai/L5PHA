"""Generate a first-owner code locally; output only its hash for trusted provisioning."""

import json
import os
import secrets

from dotenv import set_key

from veyquant.telegram_auth import digest


def main():
    code = secrets.token_urlsafe(32)
    set_key(".env", "VEYQUANT_SETUP_CODE", code)
    os.chmod(".env", 0o600)
    print(
        json.dumps(
            {"invitation_hash": digest(code), "raw_code_saved_in": ".env", "expires_in": 3600}
        )
    )


if __name__ == "__main__":
    main()
