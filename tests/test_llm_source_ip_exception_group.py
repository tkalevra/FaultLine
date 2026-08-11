"""REGRESSION: the source-IP fail-safe must see a bind errno inside an ExceptionGroup.

THE BUG THIS PINS. ``llm_source_ip`` promises that a bad source IP can never break an LLM
call: if the configured address cannot be bound, fall back to the caller's unbound client,
retire the IP, and let the call through. That promise held only for destination hosts with
ONE address.

anyio's ``connect_tcp`` tries every address a host resolves to. With ONE candidate it
re-raises that lone ``OSError``, so its errno sits directly on the ``__cause__`` chain.
With TWO OR MORE it raises ``OSError("All connection attempts failed")`` whose own
``errno is None`` and hangs the real per-address errors off an ``ExceptionGroup`` in
``__cause__``. ``_errno_in`` walked only ``__cause__``/``__context__``, so on any
dual-stack or multi-A host — every Cloudflare-fronted API — the bind errno sat one level
below the walk, ``_is_bind_error`` answered False, and the error was RE-RAISED instead of
falling back.

The failure this produces is total and silent: a deployment whose configured source
addresses are not present in its network namespace (e.g. an app container listing the
HOST's public addresses) loses 100% of its outbound LLM calls to
``ConnectError: All connection attempts failed`` in ~0.02s, while an unbound client
reaches the same endpoint perfectly.

The exception chain, as measured with httpx 0.28.1 / anyio 4.14.2 / CPython 3.11:

    ConnectError → ConnectError → OSError(errno=None) → ExceptionGroup
                                                        → [OSError(99), OSError(99)]

WHY A GREEN SUITE MISSED IT. Every test in ``test_llm_source_ip_spread.py`` builds the flat
single-address chain (``raise ConnectError(...) from OSError(errno, ...)``). None built a
group. A passing suite proves nothing about a shape it never constructs — so this file
constructs the real one.

Addresses below are RFC 5737 documentation addresses (192.0.2.0/24, TEST-NET-1). No socket
is ever opened; the per-IP client factory is monkeypatched.
"""

import errno
import sys

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


# ── the shapes anyio actually produces ────────────────────────────────────────

class _GroupLike(Exception):
    """Duck-typed stand-in for ExceptionGroup.

    ``_errno_in`` traverses ``.exceptions`` by duck-typing rather than isinstance, so the
    traversal is identical on interpreters without the 3.11 builtin. This exercises that
    path on every Python version.
    """

    def __init__(self, message, exceptions):
        super().__init__(message)
        self.exceptions = list(exceptions)


def _nested_chain(sub_errnos, *, group_factory):
    """Rebuild the measured exception chain for the given per-address errnos."""
    subs = [OSError(code, "error while attempting to bind on address") for code in sub_errnos]
    group = group_factory("multiple connection attempts failed", subs)
    try:
        try:
            raise group
        except BaseException as g:
            # anyio: the outer OSError carries NO errno; the real ones live in the group.
            raise OSError("All connection attempts failed") from g
    except OSError as os_err:
        # httpcore then httpx each re-wrap, preserving __cause__.
        inner = httpx.ConnectError("All connection attempts failed")
        inner.__cause__ = os_err
        outer = httpx.ConnectError("All connection attempts failed")
        outer.__cause__ = inner
        return outer


_real_group = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="builtin ExceptionGroup requires Python 3.11+"
)


# ── (1) the core defect: the errno must be found inside the group ─────────────

@_real_group
def test_bind_errno_inside_a_real_exceptiongroup_is_detected():
    """THE regression. Before the fix both assertions were False and the call was lost."""
    exc = _nested_chain([errno.EADDRNOTAVAIL, errno.EADDRNOTAVAIL],
                        group_factory=lambda m, s: ExceptionGroup(m, s))  # noqa: F821
    assert llm_source_ip._is_bind_error(exc) is True
    assert llm_source_ip._should_retire(exc) is True


def test_bind_errno_inside_a_duck_typed_group_is_detected():
    """Same traversal, no 3.11 builtin required — pins the duck-typing contract."""
    exc = _nested_chain([errno.EADDRNOTAVAIL, errno.EADDRNOTAVAIL], group_factory=_GroupLike)
    assert llm_source_ip._is_bind_error(exc) is True
    assert llm_source_ip._should_retire(exc) is True


def test_address_family_mismatch_inside_a_group_falls_back_without_retiring():
    """A dual-stack host: an IPv4 source simply cannot reach the AAAA addresses.

    Fall back for THIS call, but keep the IP — it is fine for IPv4 destinations, and
    retiring it would drain the pool one IP per AAAA host until spreading silently
    stopped."""
    exc = _nested_chain([errno.ENETUNREACH, errno.ENETUNREACH], group_factory=_GroupLike)
    assert llm_source_ip._is_bind_error(exc) is True
    assert llm_source_ip._should_retire(exc) is False


def test_remote_faults_inside_a_group_are_still_reraised():
    """A genuinely refused remote must NOT be silently retried unbound.

    This guards against the fix over-reaching: widening the traversal must not turn every
    grouped failure into a fallback, or the caller's circuit breaker stops seeing real
    outages."""
    exc = _nested_chain([errno.ECONNREFUSED, errno.ECONNREFUSED], group_factory=_GroupLike)
    assert llm_source_ip._is_bind_error(exc) is False
    assert llm_source_ip._should_retire(exc) is False


def test_mixed_group_with_one_bind_fault_is_treated_as_a_bind_fault():
    """One address refused the bind, another was merely unreachable — still our socket."""
    exc = _nested_chain([errno.ECONNREFUSED, errno.EADDRNOTAVAIL], group_factory=_GroupLike)
    assert llm_source_ip._is_bind_error(exc) is True
    assert llm_source_ip._should_retire(exc) is True


def test_nested_groups_are_traversed():
    """anyio may nest; the traversal is a graph walk, not a fixed two-level peek."""
    inner = _GroupLike("inner", [OSError(errno.EADDRNOTAVAIL, "bind")])
    outer = _GroupLike("outer", [inner])
    assert llm_source_ip._is_bind_error(outer) is True


def test_traversal_terminates_on_a_cyclic_chain():
    """A self-referential chain must not spin inside the error path of a failing call."""
    a = OSError(errno.ECONNREFUSED, "a")
    b = OSError(errno.ECONNREFUSED, "b")
    a.__cause__ = b
    b.__cause__ = a
    assert llm_source_ip._is_bind_error(a) is False  # returns, does not hang


# ── (2) end-to-end: the call must SURVIVE, which is the actual promise ────────

class _FakeResponse:
    def __init__(self, tag):
        self.tag = tag


class _RecordingClient:
    def __init__(self, tag):
        self.tag = tag
        self.calls = 0

    def post(self, url, *, json, headers, timeout):
        self.calls += 1
        return _FakeResponse(self.tag)


class _AsyncRecordingClient:
    def __init__(self, tag):
        self.tag = tag
        self.calls = 0

    async def post(self, url, *, json, headers, timeout):
        self.calls += 1
        return _FakeResponse(self.tag)


def test_grouped_bind_failure_falls_back_to_default_client_sync(monkeypatch):
    """The whole point: a call is never lost to source-IP spreading."""
    ip = "192.0.2.10"
    monkeypatch.setenv("LLM_SOURCE_IPS", ip)

    class _GroupBindFailClient:
        def post(self, url, *, json, headers, timeout):
            raise _nested_chain([errno.EADDRNOTAVAIL, errno.EADDRNOTAVAIL],
                                group_factory=_GroupLike)

    monkeypatch.setattr(llm_source_ip, "_build_sync_client", lambda _ip: _GroupBindFailClient())

    default = _RecordingClient("DEFAULT")
    resp = llm_source_ip.post_sync(default, "http://llm/endpoint",
                                   json={}, headers={}, timeout=1.0)

    assert resp.tag == "DEFAULT"       # before the fix this raised ConnectError
    assert default.calls == 1
    assert ip in llm_source_ip._bad_ips  # unusable source address, retired


@pytest.mark.asyncio
async def test_grouped_bind_failure_falls_back_to_default_client_async(monkeypatch):
    """Async twin — the lane the retrying async LLM helper actually uses."""
    ip = "192.0.2.10"
    monkeypatch.setenv("LLM_SOURCE_IPS", ip)

    class _GroupBindFailClient:
        async def post(self, url, *, json, headers, timeout):
            raise _nested_chain([errno.EADDRNOTAVAIL, errno.EADDRNOTAVAIL],
                                group_factory=_GroupLike)

    monkeypatch.setattr(llm_source_ip, "_build_async_client", lambda _ip: _GroupBindFailClient())

    default = _AsyncRecordingClient("DEFAULT")
    resp = await llm_source_ip.post_async(default, "http://llm/endpoint",
                                          json={}, headers={}, timeout=1.0)

    assert resp.tag == "DEFAULT"
    assert default.calls == 1
    assert ip in llm_source_ip._bad_ips


@pytest.mark.asyncio
async def test_pool_self_empties_then_passes_through(monkeypatch):
    """After every configured IP is retired, spreading is a pure pass-through.

    This is the self-healing property that bounds the blast radius of a misconfigured pool
    to one failed bind per IP per process, instead of one per call forever."""
    ips = ["192.0.2.10", "192.0.2.12", "192.0.2.13"]
    monkeypatch.setenv("LLM_SOURCE_IPS", ",".join(ips))

    class _GroupBindFailClient:
        async def post(self, url, *, json, headers, timeout):
            raise _nested_chain([errno.EADDRNOTAVAIL], group_factory=_GroupLike)

    monkeypatch.setattr(llm_source_ip, "_build_async_client", lambda _ip: _GroupBindFailClient())

    default = _AsyncRecordingClient("DEFAULT")
    for _ in range(6):
        resp = await llm_source_ip.post_async(default, "http://llm/endpoint",
                                              json={}, headers={}, timeout=1.0)
        assert resp.tag == "DEFAULT"

    assert default.calls == 6                       # no call was ever lost
    assert set(ips) <= llm_source_ip._bad_ips       # all three retired
    assert llm_source_ip.is_enabled() is False      # pool empty → pure pass-through
