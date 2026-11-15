"""M3 — real client IP behind Caddy for the signup rate-limit.

Behind Caddy the peer is loopback, so the real client is the LAST X-Forwarded-For hop (Caddy
appends it); the FIRST hop is attacker-spoofable (audit M3) and must NOT be trusted.

(Signup is now always OPEN — no invite gate; the approval valve governs active-vs-pending. The old
_signup_open flag was removed.)

Pure unit tests — no DB / TestClient. SAAS_MODE + the test secrets are the suite-wide baseline
set once in tests/conftest.py, not written into os.environ here (that is process-wide and made
this file's import order decide other modules' configuration).
Run: python3 tools/fltest.py --bug SIGNUP-OPEN --test tests/test_saas_signup_open_and_clientip.py
"""

from types import SimpleNamespace

from saas import user_console as uc


def _req(headers=None, peer="127.0.0.1"):
    return SimpleNamespace(
        headers=headers or {},
        client=SimpleNamespace(host=peer) if peer else None,
    )


def test_client_ip_uses_last_xff_hop_behind_caddy():
    # client spoofs the first hop; Caddy appends the REAL peer as the last hop.
    r = _req(headers={"x-forwarded-for": "9.9.9.9, 203.0.113.7"}, peer="127.0.0.1")
    assert uc._client_ip(r) == "203.0.113.7"     # last hop, NOT the spoofed 9.9.9.9


def test_client_ip_single_hop():
    r = _req(headers={"x-forwarded-for": "203.0.113.7"}, peer="127.0.0.1")
    assert uc._client_ip(r) == "203.0.113.7"


def test_client_ip_falls_back_to_peer_without_xff():
    r = _req(headers={}, peer="198.51.100.4")
    assert uc._client_ip(r) == "198.51.100.4"


def test_client_ip_failsafe():
    assert uc._client_ip(_req(headers={}, peer=None)) == "?"
