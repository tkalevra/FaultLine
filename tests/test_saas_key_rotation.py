"""Master-key ROTATION support — Fernet cred key + session secret (no DB).

Proves an operator can rotate a master key WITHOUT locking everyone out: new writes use the
new primary key, while data/sessions produced under a previous key keep working.

  Fernet (saas/crypto.py):
    1. no keys → fail-closed (crypto unavailable, no plaintext leak).
    2. round-trip with a single key (backward compatible).
    3. after rotation, ciphertext from the OLD key still decrypts via _PREVIOUS.
    4. new encryptions use the NEW primary (old key alone can no longer decrypt them).
    5. reencrypt() re-wraps an old token onto the new primary.
    6. a malformed entry in the PREVIOUS list does not break decryption under a good primary.

  Session secret (saas/dashboard_auth.py):
    7. a session signed under the OLD secret still verifies after rotation (secret in _PREVIOUS).
    8. after the old secret is dropped, its tokens no longer verify (clean expiry of trust).

Run: python3 -m pytest tests/test_saas_key_rotation.py -q
"""

import importlib

import pytest

pytest.importorskip("cryptography")


# ── Fernet cred-key rotation ─────────────────────────────────────────────────────────

def _reload_crypto():
    from saas import crypto as c
    return importlib.reload(c)


_KEY_A = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="  # 44-char urlsafe-b64 (32 bytes)
_KEY_B = "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB="


def test_no_key_fail_closed(monkeypatch):
    monkeypatch.delenv("SAAS_LLM_CRED_KEY", raising=False)
    monkeypatch.delenv("SAAS_LLM_CRED_KEY_PREVIOUS", raising=False)
    c = _reload_crypto()
    assert c.crypto_available() is False
    with pytest.raises(c.CryptoUnavailable):
        c.encrypt("secret")
    assert c.decrypt("anything") is None  # deny, never plaintext


def test_single_key_roundtrip(monkeypatch):
    monkeypatch.setenv("SAAS_LLM_CRED_KEY", _KEY_A)
    monkeypatch.delenv("SAAS_LLM_CRED_KEY_PREVIOUS", raising=False)
    c = _reload_crypto()
    tok = c.encrypt("hunter2")
    assert c.decrypt(tok) == "hunter2"


def test_old_ciphertext_still_decrypts_after_rotation(monkeypatch):
    # encrypt under A
    monkeypatch.setenv("SAAS_LLM_CRED_KEY", _KEY_A)
    monkeypatch.delenv("SAAS_LLM_CRED_KEY_PREVIOUS", raising=False)
    c = _reload_crypto()
    tok_a = c.encrypt("byo-llm-key")

    # rotate: B is primary, A moves to previous
    monkeypatch.setenv("SAAS_LLM_CRED_KEY", _KEY_B)
    monkeypatch.setenv("SAAS_LLM_CRED_KEY_PREVIOUS", _KEY_A)
    c = _reload_crypto()
    assert c.decrypt(tok_a) == "byo-llm-key"        # old token still readable
    tok_b = c.encrypt("byo-llm-key")                # new writes use B

    # drop A entirely → the NEW token (B) still decrypts, the OLD token (A) no longer does
    monkeypatch.setenv("SAAS_LLM_CRED_KEY", _KEY_B)
    monkeypatch.delenv("SAAS_LLM_CRED_KEY_PREVIOUS", raising=False)
    c = _reload_crypto()
    assert c.decrypt(tok_b) == "byo-llm-key"
    assert c.decrypt(tok_a) is None                 # trust in A fully retired


def test_reencrypt_moves_token_to_primary(monkeypatch):
    monkeypatch.setenv("SAAS_LLM_CRED_KEY", _KEY_A)
    monkeypatch.delenv("SAAS_LLM_CRED_KEY_PREVIOUS", raising=False)
    c = _reload_crypto()
    tok_a = c.encrypt("v")

    monkeypatch.setenv("SAAS_LLM_CRED_KEY", _KEY_B)
    monkeypatch.setenv("SAAS_LLM_CRED_KEY_PREVIOUS", _KEY_A)
    c = _reload_crypto()
    tok_migrated = c.reencrypt(tok_a)
    assert tok_migrated is not None

    # migrated token decrypts under B alone (A dropped)
    monkeypatch.setenv("SAAS_LLM_CRED_KEY", _KEY_B)
    monkeypatch.delenv("SAAS_LLM_CRED_KEY_PREVIOUS", raising=False)
    c = _reload_crypto()
    assert c.decrypt(tok_migrated) == "v"


def test_bad_previous_entry_is_skipped(monkeypatch):
    monkeypatch.setenv("SAAS_LLM_CRED_KEY", _KEY_A)
    monkeypatch.delenv("SAAS_LLM_CRED_KEY_PREVIOUS", raising=False)
    c = _reload_crypto()
    tok = c.encrypt("z")
    # a garbage previous entry alongside empty items must not break the good primary
    monkeypatch.setenv("SAAS_LLM_CRED_KEY_PREVIOUS", ",,not-a-valid-fernet-key,,")
    c = _reload_crypto()
    assert c.decrypt(tok) == "z"


# ── session-secret rotation ──────────────────────────────────────────────────────────

def _reload_auth(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    from saas import dashboard_auth as a
    return importlib.reload(a)


def test_session_signed_under_old_secret_verifies_after_rotation(monkeypatch):
    monkeypatch.setenv("SAAS_SESSION_SECRET", "old-secret")
    monkeypatch.delenv("SAAS_SESSION_SECRET_PREVIOUS", raising=False)
    a = _reload_auth(monkeypatch)
    tok = a.issue_session(dashboard_user_id="u1", account_id="acct", username="bob")
    assert a.verify_session(tok)["sub"] == "u1"

    # rotate: new primary, old secret moved to previous
    monkeypatch.setenv("SAAS_SESSION_SECRET", "new-secret")
    monkeypatch.setenv("SAAS_SESSION_SECRET_PREVIOUS", "old-secret")
    a = _reload_auth(monkeypatch)
    assert a.verify_session(tok)["sub"] == "u1"          # old token still valid
    tok_new = a.issue_session(dashboard_user_id="u2", account_id="acct", username="ana")
    assert a.verify_session(tok_new)["sub"] == "u2"      # new token signs under new primary


def test_old_session_dies_after_secret_dropped(monkeypatch):
    monkeypatch.setenv("SAAS_SESSION_SECRET", "old-secret")
    monkeypatch.delenv("SAAS_SESSION_SECRET_PREVIOUS", raising=False)
    a = _reload_auth(monkeypatch)
    tok = a.issue_session(dashboard_user_id="u1", account_id="acct", username="bob")

    # fully rotate away from old-secret (dropped from _PREVIOUS)
    monkeypatch.setenv("SAAS_SESSION_SECRET", "new-secret")
    monkeypatch.delenv("SAAS_SESSION_SECRET_PREVIOUS", raising=False)
    a = _reload_auth(monkeypatch)
    assert a.verify_session(tok) is None   # trust in the old secret fully retired
