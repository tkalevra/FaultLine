"""App-level outbound source-IP spreading for LLM HTTP calls (src/api/llm_source_ip.py).

WHAT THIS PINS. FaultLine's prod box has several public IPs (a /29). To keep any single
egress IP from being provider-rate-flagged, outbound LLM connections rotate ROUND-ROBIN
across a configured pool, each bound to a distinct source address via
``httpx.HTTPTransport(local_address=ip)``. It is a pure OPT-IN: ``LLM_SOURCE_IPS`` unset
→ the caller's default client is used with NO binding (byte-for-byte current behavior).
And it is FAIL-SAFE: an invalid / unbindable IP must never lose an LLM call.

The three contracts, matching the build spec:
  (a) 3 IPs set → successive POSTs rotate through them round-robin (local_address captured).
  (b) unset → no local_address binding; the default client is used directly.
  (c) an invalid / bind-failing IP falls back safely without raising.

Run: python3 tools/fltest.py --bug source-ip-spread --test tests/test_llm_source_ip_spread.py
"""

import errno

import httpx
import pytest

from src.api import llm_source_ip


@pytest.fixture(autouse=True)
def _clean_pool(monkeypatch):
    """Each test starts with a fresh counter/cache/bad-set and no env pool."""
    monkeypatch.delenv("LLM_SOURCE_IPS", raising=False)
    llm_source_ip.reset()
    yield
    llm_source_ip.reset()


class _FakeResponse:
    def __init__(self, tag):
        self.tag = tag


class _RecordingClient:
    """Stand-in for a source-IP-bound client; records the source IP it represents."""

    def __init__(self, ip):
        self.ip = ip
        self.calls = 0

    def post(self, url, *, json, headers, timeout):
        self.calls += 1
        return _FakeResponse(self.ip)


# ── (a) round-robin rotation across the pool ──────────────────────────────────

def test_three_ips_rotate_round_robin(monkeypatch):
    ips = ["192.0.2.10", "192.0.2.12", "192.0.2.13"]
    monkeypatch.setenv("LLM_SOURCE_IPS", ",".join(ips))

    # Capture which source IP each successive call binds to. Patch the per-IP client
    # factory so no real socket is created; the returned recorder tags its response.
    built = {}

    def _fake_build(ip):
        c = _RecordingClient(ip)
        built[ip] = c
        return c

    monkeypatch.setattr(llm_source_ip, "_build_sync_client", _fake_build)

    default = _RecordingClient("DEFAULT")
    chosen = []
    for _ in range(6):
        resp = llm_source_ip.post_sync(
            default, "http://llm/endpoint", json={}, headers={}, timeout=1.0
        )
        chosen.append(resp.tag)

    # Round-robin over the pool, twice through.
    assert chosen == [ips[0], ips[1], ips[2], ips[0], ips[1], ips[2]]
    # Default (no-bind) client was never used while the pool is healthy.
    assert default.calls == 0
    # One cached client per IP (built once, reused on the second lap).
    assert set(built.keys()) == set(ips)
    assert all(c.calls == 2 for c in built.values())


def test_build_sync_client_binds_local_address(monkeypatch):
    """The real factory wires local_address into the httpx transport."""
    captured = {}

    class _FakeTransport:
        def __init__(self, *args, **kwargs):
            captured["local_address"] = kwargs.get("local_address")

        def close(self):
            pass

    monkeypatch.setattr(httpx, "HTTPTransport", _FakeTransport)
    client = llm_source_ip._build_sync_client("192.0.2.14")
    try:
        assert captured["local_address"] == "192.0.2.14"
    finally:
        client.close()


# ── (b) unset env → default client, no binding ────────────────────────────────

def test_unset_env_uses_default_client_no_binding(monkeypatch):
    # No LLM_SOURCE_IPS (cleared by fixture). The factory must NEVER be consulted.
    def _boom(ip):  # pragma: no cover - must not be called
        raise AssertionError("source-IP client built while LLM_SOURCE_IPS unset")

    monkeypatch.setattr(llm_source_ip, "_build_sync_client", _boom)

    assert llm_source_ip.is_enabled() is False
    default = _RecordingClient("DEFAULT")
    resp = llm_source_ip.post_sync(
        default, "http://llm/endpoint", json={}, headers={}, timeout=1.0
    )
    assert resp.tag == "DEFAULT"
    assert default.calls == 1


def test_empty_env_uses_default_client(monkeypatch):
    monkeypatch.setenv("LLM_SOURCE_IPS", "   ,  ,")  # whitespace/commas only → empty pool
    assert llm_source_ip.is_enabled() is False
    default = _RecordingClient("DEFAULT")
    resp = llm_source_ip.post_sync(
        default, "http://llm/endpoint", json={}, headers={}, timeout=1.0
    )
    assert resp.tag == "DEFAULT"


# ── (c) invalid / bind-failing IP falls back safely ───────────────────────────

def test_invalid_ip_string_skipped_and_falls_back(monkeypatch):
    # A malformed IP is dropped from the pool up front; with no valid IP left the call
    # falls back to the default client WITHOUT raising.
    monkeypatch.setenv("LLM_SOURCE_IPS", "999.999.999.999")

    def _boom(ip):  # pragma: no cover - malformed IP must never reach the factory
        raise AssertionError("built a client for a malformed IP")

    monkeypatch.setattr(llm_source_ip, "_build_sync_client", _boom)

    default = _RecordingClient("DEFAULT")
    resp = llm_source_ip.post_sync(
        default, "http://llm/endpoint", json={}, headers={}, timeout=1.0
    )
    assert resp.tag == "DEFAULT"
    assert default.calls == 1


def test_bind_failure_falls_back_and_retires_ip(monkeypatch):
    # A VALID IP that is not bindable on this host raises EADDRNOTAVAIL at connect time.
    # The call must fall back to the default client (never lost) and retire the bad IP.
    good = "192.0.2.10"
    bad = "192.0.2.11"
    monkeypatch.setenv("LLM_SOURCE_IPS", f"{bad},{good}")

    class _BindFailClient:
        def post(self, url, *, json, headers, timeout):
            raise httpx.ConnectError("cannot assign") from OSError(
                errno.EADDRNOTAVAIL, "Cannot assign requested address"
            )

    class _GoodClient:
        def __init__(self, ip):
            self.ip = ip

        def post(self, url, *, json, headers, timeout):
            return _FakeResponse(self.ip)

    def _fake_build(ip):
        return _BindFailClient() if ip == bad else _GoodClient(ip)

    monkeypatch.setattr(llm_source_ip, "_build_sync_client", _fake_build)

    default = _RecordingClient("DEFAULT")

    # First call selects the bad IP → bind fails → falls back to default (no raise).
    resp1 = llm_source_ip.post_sync(
        default, "http://llm/endpoint", json={}, headers={}, timeout=1.0
    )
    assert resp1.tag == "DEFAULT"
    assert default.calls == 1
    assert bad in llm_source_ip._bad_ips  # retired

    # Subsequent calls use only the surviving good IP — the bad one is gone for good.
    tags = []
    for _ in range(3):
        r = llm_source_ip.post_sync(
            default, "http://llm/endpoint", json={}, headers={}, timeout=1.0
        )
        tags.append(r.tag)
    assert tags == [good, good, good]
    assert default.calls == 1  # no further fallbacks


def test_non_bind_error_is_reraised_and_ip_kept(monkeypatch):
    # A REMOTE failure (endpoint refused/timeout) is NOT the source IP's fault: it must
    # propagate to the caller's existing retry/circuit-breaker and NOT retire the IP.
    ip = "192.0.2.10"
    monkeypatch.setenv("LLM_SOURCE_IPS", ip)

    class _RemoteFailClient:
        def post(self, url, *, json, headers, timeout):
            raise httpx.ConnectError("connection refused") from OSError(
                errno.ECONNREFUSED, "Connection refused"
            )

    monkeypatch.setattr(llm_source_ip, "_build_sync_client", lambda _ip: _RemoteFailClient())

    default = _RecordingClient("DEFAULT")
    with pytest.raises(httpx.ConnectError):
        llm_source_ip.post_sync(
            default, "http://llm/endpoint", json={}, headers={}, timeout=1.0
        )
    assert ip not in llm_source_ip._bad_ips  # NOT retired — remote fault, not bind fault
    assert default.calls == 0


# ── async twin: rotation + unset passthrough ──────────────────────────────────

class _AsyncRecordingClient:
    def __init__(self, ip):
        self.ip = ip
        self.calls = 0

    async def post(self, url, *, json, headers, timeout):
        self.calls += 1
        return _FakeResponse(self.ip)


@pytest.mark.asyncio
async def test_async_rotates_round_robin(monkeypatch):
    ips = ["192.0.2.10", "192.0.2.12", "192.0.2.13"]
    monkeypatch.setenv("LLM_SOURCE_IPS", ",".join(ips))
    monkeypatch.setattr(
        llm_source_ip, "_build_async_client", lambda ip: _AsyncRecordingClient(ip)
    )

    default = _AsyncRecordingClient("DEFAULT")
    chosen = []
    for _ in range(4):
        resp = await llm_source_ip.post_async(
            default, "http://llm/endpoint", json={}, headers={}, timeout=1.0
        )
        chosen.append(resp.tag)
    assert chosen == [ips[0], ips[1], ips[2], ips[0]]
    assert default.calls == 0


@pytest.mark.asyncio
async def test_async_unset_uses_default(monkeypatch):
    default = _AsyncRecordingClient("DEFAULT")
    resp = await llm_source_ip.post_async(
        default, "http://llm/endpoint", json={}, headers={}, timeout=1.0
    )
    assert resp.tag == "DEFAULT"
    assert default.calls == 1


# ── address-family regression (production outage 2026-08-08) ─────────────────
# A source-IP-bound client is pinned to an IPv4 address. Any destination that resolves
# to IPv6 (any Cloudflare-fronted API) is then unreachable FROM
# THAT SOCKET. errno -9 / 97 / 101 were absent from the fallback set, so the error was
# RE-RAISED rather than retried unbound, and a tenant's brain probe reported a hard
# failure against a host the unbound client reaches perfectly.
import errno as _errno
import socket as _socket

import pytest as _pytest

from src.api import llm_source_ip as _lsi


@_pytest.mark.parametrize("code", [
    _socket.EAI_ADDRFAMILY,   # -9  getaddrinfo refused the bound address's family
    _errno.EAFNOSUPPORT,      # 97  socket()/connect() rejected the family
    _errno.ENETUNREACH,       # 101 no route from this local address
])
def test_address_family_mismatch_falls_back_but_never_retires_the_ip(code):
    """Falls back for THIS call, but the IP stays in the pool.

    The IP is fine for IPv4 destinations — it simply cannot reach IPv6 ones. Retiring it
    would drain the whole pool, one IP per AAAA-only host, until spreading silently
    stopped."""
    exc = OSError(code, "simulated address-family mismatch")
    assert _lsi._is_bind_error(exc) is True
    assert _lsi._should_retire(exc) is False


@_pytest.mark.parametrize("code", [
    _errno.EADDRNOTAVAIL,     # the IP is not assigned to this host
    _errno.EADDRINUSE,
    _errno.EACCES,
])
def test_real_bind_faults_still_retire_the_ip(code):
    """Unchanged behaviour: these mean the SOURCE IP itself is unusable."""
    exc = OSError(code, "simulated bind fault")
    assert _lsi._is_bind_error(exc) is True
    assert _lsi._should_retire(exc) is True


@_pytest.mark.parametrize("code", [_errno.ECONNREFUSED, _errno.ETIMEDOUT])
def test_remote_faults_are_still_reraised(code):
    """A refused/timed-out REMOTE is not our socket's fault — it must reach the
    caller's retry/circuit-breaker, not be silently retried unbound."""
    exc = OSError(code, "simulated remote fault")
    assert _lsi._is_bind_error(exc) is False
    assert _lsi._should_retire(exc) is False
