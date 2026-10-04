import json
import subprocess
import sys
from pathlib import Path


def test_cms_envelope_roundtrip_excludes_bot_token_and_plaintext_files(tmp_path):
    certificate, key = tmp_path / "recipient.pem", tmp_path / "key.pem"
    generated = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=test-only",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
        ],
        capture_output=True,
        timeout=20,
    )
    assert generated.returncode == 0
    env = tmp_path / "fixture.env"
    env.write_text(
        "TOSS_CLIENT_ID=private-client\nTOSS_CLIENT_SECRET=private-secret\nTOSS_ACCOUNT_SEQ=7\nTELEGRAM_BOT_TOKEN=excluded-bot-token\n"
    )
    envelope = tmp_path / "envelope.der"
    command = [
        sys.executable,
        "scripts/seal_probe.py",
        "--certificate",
        str(certificate),
        "--env-file",
        str(env),
        "--expected-ip",
        "192.0.2.1",
        "--output",
        str(envelope),
    ]
    sealed = subprocess.run(command, capture_output=True, timeout=20)
    assert sealed.returncode == 0 and json.loads(sealed.stdout)["encrypted"] is True
    assert b"private-" not in sealed.stdout + sealed.stderr + envelope.read_bytes()
    assert envelope.stat().st_mode & 0o777 == 0o600
    decoded = subprocess.run(
        [
            "openssl",
            "cms",
            "-decrypt",
            "-binary",
            "-inform",
            "DER",
            "-in",
            str(envelope),
            "-inkey",
            str(key),
            "-recip",
            str(certificate),
        ],
        capture_output=True,
        timeout=20,
    )
    assert decoded.returncode == 0
    payload = json.loads(decoded.stdout)
    assert payload["credentials"]["TOSS_CLIENT_SECRET"] == "private-secret"
    assert b"excluded-bot-token" not in decoded.stdout
    assert set(payload["credentials"]) == {
        "TOSS_CLIENT_ID",
        "TOSS_CLIENT_SECRET",
        "TOSS_ACCOUNT_SEQ",
    }
    original = envelope.read_bytes()
    repeated = subprocess.run(command, capture_output=True, timeout=20)
    assert repeated.returncode != 0 and envelope.read_bytes() == original
    assert b"private-" not in repeated.stdout + repeated.stderr
    assert {p.name for p in Path(tmp_path).iterdir()} == {
        "recipient.pem",
        "key.pem",
        "fixture.env",
        "envelope.der",
    }
