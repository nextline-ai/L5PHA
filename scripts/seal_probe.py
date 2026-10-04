"""Local CMS envelope builder. Plaintext goes only to OpenSSL stdin, never a file/stdout."""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

from veyquant.local_config import read_local_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--certificate", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected-ip", required=True)
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    values = read_local_config(args.env_file)
    payload = {
        "version": 1,
        "expires_at": int(time.time()) + 600,
        "expected_ip": args.expected_ip,
        "credentials": {
            k: values.get(k, "")
            for k in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_SEQ")
        },
    }
    if not all(payload["credentials"][k] for k in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET")):
        raise SystemExit("Toss credentials missing")
    result = subprocess.run(
        ["openssl", "cms", "-encrypt", "-binary", "-aes256", "-outform", "DER", args.certificate],
        input=json.dumps(payload).encode(),
        capture_output=True,
        timeout=15,
    )
    if result.returncode:
        raise SystemExit("Envelope encryption failed")
    with os.fdopen(
        os.open(Path(args.output), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
    ) as f:
        f.write(result.stdout)
    print(json.dumps({"encrypted": True, "bot_token_included": False}))


if __name__ == "__main__":
    main()
