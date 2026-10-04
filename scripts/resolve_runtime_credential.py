"""Launch asm-exec without exposing its SSRF token or resolved values in operator output."""

import os
from pathlib import Path


def main():
    env = os.environ.copy()
    env["AWS_TOKEN"] = Path("/var/run/awssmatoken").read_text().strip()
    os.execve(
        "/opt/veyquant/asm-exec",
        [
            "asm-exec",
            "--",
            "/opt/veyquant/venv/bin/python",
            "/opt/veyquant/runtime/scripts/cache_runtime_credential.py",
        ],
        env,
    )


if __name__ == "__main__":
    main()
