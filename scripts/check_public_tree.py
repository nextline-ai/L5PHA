"""Check only Git-tracked files; never read or print local credential values."""

import json
import re
import subprocess
from pathlib import Path

BLOCKED_PARTS = {".git", "var", ".venv", "node_modules", "__pycache__"}
BLOCKED_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".log", ".pem", ".key", ".p12", ".pfx", ".bundle"}
PATTERNS = {
    "private_key": re.compile(rb"(?m)^-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    "aws_access_key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "telegram_bot_token": re.compile(rb"\b[0-9]{8,12}:[A-Za-z0-9_-]{35}\b"),
    "openai_key": re.compile(rb"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{40,}\b"),
}


def inspect(path, data):
    p = Path(path)
    issues = []
    if (
        any(part in BLOCKED_PARTS for part in p.parts)
        or p.suffix.lower() in BLOCKED_SUFFIXES
        or (p.name.startswith(".env") and p.name != ".env.example")
        or p.parts[:2] == ("docs", "evidence")
    ):
        issues.append("private_artifact")
    issues += [name for name, pattern in PATTERNS.items() if pattern.search(data)]
    return issues


def main():
    paths = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
    findings = []
    count = 0
    for path in filter(None, paths):
        file = Path(path)
        if not file.is_file():
            continue
        count += 1
        for issue in inspect(path, file.read_bytes()):
            findings.append({"file": path, "kind": issue})
    print(json.dumps({"files_checked": count, "findings": findings}))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
