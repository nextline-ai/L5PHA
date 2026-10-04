from scripts.check_public_tree import inspect


def test_publication_guard_rejects_credentials_and_private_artifacts():
    assert inspect(".env", b"empty") == ["private_artifact"]
    assert inspect("var/history.json", b"{}") == ["private_artifact"]
    assert inspect("docs/evidence/live.json", b"{}") == ["private_artifact"]
    assert inspect("test.py", b"AKIA" + b"A" * 16) == ["aws_access_key"]
    assert inspect("test.py", b"123456789:" + b"a" * 35) == ["telegram_bot_token"]
    assert inspect("test.py", b"sk-proj-" + b"a" * 50) == ["openai_key"]
    assert inspect("test.py", b"-----BEGIN " + b"PRIVATE KEY-----") == ["private_key"]


def test_public_identifiers_and_empty_env_template_are_allowed():
    assert inspect(".env.example", b"TOSS_CLIENT_SECRET=\nTELEGRAM_BOT_TOKEN=\n") == []
    assert inspect("installer/release.json", b'{"bot_id": 123456789}') == []
