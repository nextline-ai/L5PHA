import base64
import json
from urllib.parse import urlencode

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from veyquant.telegram_auth import SESSION_TTL, AuthenticationError, OwnerAuth, TelegramVerifier


@pytest.fixture
def signer():
    return Ed25519PrivateKey.generate()


def signed(key, user_id=42, auth_date=1000, bot_id=123, query_id="query1"):
    data = {
        "user": json.dumps({"id": user_id, "first_name": "한글 이름"}, ensure_ascii=False),
        "auth_date": str(auth_date),
        "query_id": query_id,
    }
    message = f"{bot_id}:WebAppData\n" + "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    signature = base64.urlsafe_b64encode(key.sign(message.encode())).decode().rstrip("=")
    return urlencode(data | {"signature": signature, "hash": "not_used_by_ed25519"})


def verifier(signer):
    return TelegramVerifier(123, public_key=signer.public_key().public_bytes_raw())


def test_valid_telegram_data(signer):
    assert verifier(signer).verify(signed(signer), 1000).user_id == 42


@pytest.mark.parametrize(
    "changes",
    [
        {"bot_id": 456},
        {"auth_date": 600},
        {"auth_date": 1001},
        {"user_id": True},
        {"user_id": "42"},
        {"user_id": -1},
    ],
)
def test_wrong_bot_expiry_future_invalid_user(signer, changes):
    with pytest.raises(AuthenticationError):
        verifier(signer).verify(signed(signer, **changes), 1000)


def test_tampering_and_duplicate_fields(signer):
    raw = signed(signer)
    for value in [
        raw.replace("1000", "999"),
        raw + "&auth_date=1000",
        "signature=%%%",
        "x=" + "a" * 20000,
        "signature=a&auth_date=1000",
    ]:
        with pytest.raises(AuthenticationError):
            verifier(signer).verify(value, 1000)


def test_production_key_rejects_test_signature(signer):
    with pytest.raises(AuthenticationError):
        TelegramVerifier(123).verify(signed(signer), 1000)


def test_one_time_binding_owner_and_revocation(store, signer):
    auth = OwnerAuth(store, verifier(signer))
    invitation = auth.issue_invitation(1000)
    with pytest.raises(AuthenticationError, match="invalid_invitation"):
        auth.bind("wrong", signed(signer), 1000)
    session = auth.bind(invitation, signed(signer), 1000)
    assert auth.authorize(session, 1001) == 42
    with pytest.raises(AuthenticationError, match="already_bound"):
        auth.bind(invitation, signed(signer, user_id=99), 1001)
    with pytest.raises(AuthenticationError, match="owner_mismatch"):
        auth.login(signed(signer, user_id=99), 1001)
    with pytest.raises(AuthenticationError, match="authentication_replayed"):
        auth.login(signed(signer), 1001)
    with pytest.raises(AuthenticationError, match="already_bound"):
        auth.issue_invitation(1002)
    auth.revoke_sessions()
    with pytest.raises(AuthenticationError, match="invalid_session"):
        auth.authorize(session, 1002)
    assert invitation not in str(store.rows()) and session not in str(store.rows())


def test_invitation_rotation_expiry_and_session_expiry(store, signer):
    auth = OwnerAuth(store, verifier(signer))
    old = auth.issue_invitation(1000)
    fresh = auth.issue_invitation(1000)
    with pytest.raises(AuthenticationError, match="invalid_invitation"):
        auth.bind(old, signed(signer), 1000)
    with pytest.raises(AuthenticationError, match="invalid_invitation"):
        auth.bind(fresh, signed(signer, auth_date=1600), 1600)
    session = auth.bind(fresh, signed(signer), 1000)
    assert auth.authorize(session, 1900) == 42
    with pytest.raises(AuthenticationError, match="invalid_session"):
        auth.authorize(session, 1000 + SESSION_TTL)


def test_renewal_requires_active_session_and_has_absolute_limit(store, signer):
    auth = OwnerAuth(store, verifier(signer))
    session = auth.bind(auth.issue_invitation(1000), signed(signer), 1000)
    for day in [6, 12, 18, 24]:
        ttl = auth.renew(session, 1000 + day * 86400)
        assert 0 < ttl <= SESSION_TTL
    assert ttl == 6 * 86400
    assert auth.authorize(session, 1000 + 30 * 86400 - 1) == 42
    with pytest.raises(AuthenticationError, match="invalid_session"):
        auth.renew(session, 1000 + 30 * 86400)
    auth.revoke_sessions()
    with pytest.raises(AuthenticationError, match="invalid_session"):
        auth.renew(session, 1001)


def test_old_session_schema_migrates_without_reviving_expired_sessions(store, signer):
    from veyquant.telegram_auth import digest

    store.db.execute(
        "CREATE TABLE sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER, expires_at INTEGER)"
    )
    store.db.execute("INSERT INTO sessions VALUES(?,?,?)", (digest("old-valid"), 42, 1900))
    store.db.execute("INSERT INTO sessions VALUES(?,?,?)", (digest("old-expired"), 42, 999))
    auth = OwnerAuth(store, verifier(signer))
    store.db.execute("INSERT INTO owner VALUES(1,42)")
    assert auth.renew("old-valid", 1000) == SESSION_TTL
    with pytest.raises(AuthenticationError, match="invalid_session"):
        auth.renew("old-expired", 1000)


def test_binding_required_is_only_returned_after_validating_telegram(store, signer):
    auth = OwnerAuth(store, verifier(signer))
    with pytest.raises(AuthenticationError, match="invalid_telegram_authentication"):
        auth.login("invalid", 1000)
    with pytest.raises(AuthenticationError, match="binding_required"):
        auth.login(signed(signer), 1000)
    session = auth.bind(auth.issue_invitation(1000), signed(signer), 1000)
    assert auth.authorize(session, 1001) == 42


def test_reordered_query_and_changed_hash_cannot_bypass_replay(store, signer):
    auth = OwnerAuth(store, verifier(signer))
    raw = signed(signer)
    auth.bind(auth.issue_invitation(1000), raw, 1000)
    replay = "&".join(reversed(raw.replace("not_used_by_ed25519", "changed").split("&")))
    with pytest.raises(AuthenticationError, match="authentication_replayed"):
        auth.login(replay, 1001)


def test_hash_only_invitation_provisioning(store, signer):
    from veyquant.telegram_auth import digest

    auth = OwnerAuth(store, verifier(signer))
    auth.install_invitation_hash(digest("local-generated-test-code"), 1000)
    row = store.db.execute("SELECT * FROM invitations").fetchone()
    assert row["expires_at"] == 4600
    assert row["token_hash"] != "local-generated-test-code"
    session = auth.bind("local-generated-test-code", signed(signer), 1000)
    assert auth.authorize(session, 1001) == 42
    with pytest.raises(AuthenticationError, match="already_bound"):
        auth.install_invitation_hash(digest("replacement"), 1001)


@pytest.mark.parametrize(
    "token_hash,ttl", [("x" * 64, 600), ("a" * 63, 600), ("a" * 64, 3601), ("a" * 64, True)]
)
def test_hash_only_invitation_rejects_invalid_parameters(store, signer, token_hash, ttl):
    auth = OwnerAuth(store, verifier(signer))
    with pytest.raises(ValueError):
        auth.install_invitation_hash(token_hash, 1000, ttl)
    assert store.db.execute("SELECT COUNT(*) FROM invitations").fetchone()[0] == 0
