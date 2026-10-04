"""SSM host runner: consume a one-time CMS envelope, then delete temporary key material."""

import asyncio
import json
import logging
import os
import re
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path

from veyquant.toss_probe import probe_toss


def main():
    logging.disable(logging.CRITICAL)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    directory = Path(sys.argv[1])
    if (
        not re.fullmatch(r"/run/veyquant-probe\.[A-Za-z0-9]{8}", str(directory))
        or directory.is_symlink()
    ):
        raise SystemExit("Invalid probe directory")
    report = {"status": "blocked", "stage": "envelope", "reason": "invalid_or_expired_envelope"}
    try:
        # Atomic claim: a replay cannot consume the same recipient key twice.
        os.rename(directory / "key.pem", directory / "consumed-key.pem")
        result = subprocess.run(
            [
                "openssl",
                "cms",
                "-decrypt",
                "-binary",
                "-inform",
                "DER",
                "-in",
                str(directory / "envelope.der"),
                "-inkey",
                str(directory / "consumed-key.pem"),
                "-recip",
                str(directory / "cert.pem"),
            ],
            capture_output=True,
            timeout=15,
        )
        (directory / "consumed-key.pem").unlink()
        if result.returncode:
            raise ValueError
        payload = json.loads(result.stdout)
        if payload["version"] != 1 or not time.time() < payload["expires_at"] <= time.time() + 660:
            raise ValueError
        report = asyncio.run(probe_toss(payload["credentials"], payload["expected_ip"]))
    except Exception:
        # No exception text: subprocess responses and credentials must never reach SSM logs.
        pass
    finally:
        shutil.rmtree(directory)
    report["temporary_material_removed"] = not directory.exists()
    print(json.dumps(report))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
