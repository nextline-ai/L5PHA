"""Child of asm-exec: seal resolved JSON for systemd, with no plaintext file or output."""

import json
import os
import subprocess


def main():
    value = os.environ.pop("VEYQUANT_TOSS_JSON")
    keys = json.loads(value)
    if set(keys) != {"TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_SEQ"}:
        raise SystemExit("invalid_credential_schema")
    result = subprocess.run(
        [
            "systemd-creds",
            "encrypt",
            "--name=toss",
            "-",
            "/var/lib/veyquant-secrets/toss.cred.next",
        ],
        input=value.encode(),
        capture_output=True,
        timeout=15,
    )
    if result.returncode:
        raise SystemExit("credential_sealing_failed")
    os.chmod("/var/lib/veyquant-secrets/toss.cred.next", 0o600)
    os.replace("/var/lib/veyquant-secrets/toss.cred.next", "/var/lib/veyquant-secrets/toss.cred")
    print("runtime_credential_ready")


if __name__ == "__main__":
    main()
