"""App-level outbound source-IP spreading for FaultLine's LLM HTTP calls.

WHY. A prod box with several public IPs (e.g. a /29) can spread its outbound LLM
connections across them so no single egress IP gets provider-rate-flagged/blocked and
load is shared. This is a PER-CONNECTION egress concern and is ORTHOGONAL to the
account-wide rate limiter in ``llm_calls`` (that PACES calls; this SPREADS their source
address). The two compose cleanly and neither touches the other.

HOW. Opt-in via the ``LLM_SOURCE_IPS`` env var (comma-separated source IPs). When it is
UNSET or empty this module is a pure pass-through — every LLM POST uses the caller's
existing default client with NO socket binding, byte-for-byte the current behavior. When
it is set, successive LLM POSTs rotate ROUND-ROBIN across the pool, each served by a
cached httpx client whose outbound socket is bound to one pool IP
(``httpx.HTTPTransport(local_address=ip)`` / ``AsyncHTTPTransport``). Only the local
source address changes — timeouts, headers, auth and payload are supplied by the caller
exactly as before.

FAIL-SAFE (a bad IP must never break an LLM call). A malformed IP is dropped from the
pool up front (``log_crit`` once, then skipped). If a socket bind fails at connect time
(the IP is not assigned to this host / not bindable), the call transparently falls back
to the caller's default (no-bind) client and the offending IP is retired from the pool.
A call is never lost to source-IP spreading.

⚠️ THAT PROMISE WAS BROKEN IN PRODUCTION (2026-08-11) AND THE SUITE STAYED GREEN. The
fail-safe hinges entirely on ``_errno_in`` recognising the bind errno, and it walked only
``__cause__``/``__context__``. anyio raises a FLAT OSError when a host has ONE candidate
address but wraps N>=2 candidates in an ``ExceptionGroup`` — so on any dual-stack or
multi-A host the errno sat one level below the walk, ``_is_bind_error`` said False, and
every call was re-raised instead of falling back. The affected deployment ran a
bridge-networked container whose netns owns none of the configured host addresses, so
100% of its outbound LLM calls failed as ``ConnectError: All connection attempts
failed`` — sustained, with zero successes, while an unbound client reached the same
endpoint perfectly. Every existing test built the flat single-address chain, so nothing
caught it. If you touch ``_errno_in``, keep the group traversal and keep the test that
builds a REAL ``ExceptionGroup``, not a hand-chained OSError.
"""

import errno
import ipaddress
import os
import socket
import threading
from typing import Optional

import httpx
import structlog

log = structlog.get_logger(__name__)

_ENV = "LLM_SOURCE_IPS"

# Client construction params MIRROR the module-default clients in ``llm_calls`` /
# ``main`` so a source-bound client is configured identically — only local_address
# differs. Per-call timeout is passed at ``.post(timeout=...)`` and overrides these.
_SYNC_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)
_ASYNC_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)
_TIMEOUT = httpx.Timeout(30.0)

# Local-socket bind failures that mean "this source address is the problem" — the IP
# is retired from the pool. Anything else (remote refused/timeout) is NOT the IP's
# fault and is re-raised so the caller's normal retry/circuit-breaker handles it.
#
# ADDRESS-FAMILY MISMATCH belongs here too, and its absence was a live outage.
# Binding an IPv4 source address makes every IPv6 destination unreachable FROM THIS
# SOCKET — the remote is fine, our chosen local address simply cannot get there. A host
# with AAAA records (any Cloudflare-fronted API) therefore failed
# with "[Errno -9] Address family for hostname not supported"; because -9 was not in this
# set, the error was RE-RAISED instead of falling back, and the tenant's brain probe
# reported a hard failure even though the unbound client reaches the host perfectly.
#
#   EAI_ADDRFAMILY (-9)  getaddrinfo refused the family implied by the bound address
#   EAFNOSUPPORT  (97)   socket()/connect() rejected the family outright
#   ENETUNREACH   (101)  no route from this local address (what an IPv4-bound socket
#                        gets connecting to an IPv6 literal)
#
# Falling back is always safe: the unbound client is the SAME call minus the source
# pin, so a genuinely unreachable remote still fails there and reaches the caller's
# existing retry/breaker. Losing the source pin for one call beats losing the call.
# The IP itself is unusable — retire it from the pool permanently.
_RETIRE_ERRNOS = {errno.EADDRNOTAVAIL, errno.EADDRINUSE, errno.EACCES}

# Address-family mismatches are DESTINATION-specific, not IP-specific: an IPv4 source is
# perfectly good for IPv4 hosts and merely cannot reach IPv6 ones. Falling back for the
# call is right; RETIRING the IP would be wrong — one Cloudflare-fronted host would drain
# the entire pool, one IP per AAAA-only destination, until spreading silently stopped.
_FAMILY_ERRNOS = {socket.EAI_ADDRFAMILY, errno.EAFNOSUPPORT, errno.ENETUNREACH}

# Anything in either set falls back to the unbound client for THIS call.
_BIND_ERRNOS = _RETIRE_ERRNOS | _FAMILY_ERRNOS

# Traversal bound for _errno_in. A real chain is 3-6 nodes; a group adds one node per
# address tried. 64 is far above anything genuine and stops a pathological chain from
# spinning inside the error path of a failing call.
_CHAIN_MAX_NODES = 64

_lock = threading.Lock()
_counter = 0
_sync_clients: dict[str, httpx.Client] = {}
_async_clients: dict[str, httpx.AsyncClient] = {}
_bad_ips: set[str] = set()  # invalid or bind-failed — permanently skipped


# ── configuration / selection ────────────────────────────────────────────────

def _configured_ips() -> list[str]:
    """Parse ``LLM_SOURCE_IPS`` (comma-separated) into a clean, ordered list."""
    raw = os.environ.get(_ENV, "")
    return [ip.strip() for ip in raw.split(",") if ip.strip()]


def _is_valid_ip(ip: str) -> bool:
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


def _valid_pool() -> list[str]:
    """Current usable pool: configured IPs minus malformed/bind-failed ones.

    A malformed IP is log_crit'd once and retired (added to ``_bad_ips``). An IP that is
    valid but not assigned in this process's network namespace is retired the first time
    a call actually tries it — the bind fails with EADDRNOTAVAIL, ``post_sync`` /
    ``post_async`` log_crit ``bind_failed_fallback`` and mark it bad, and the call itself
    still completes on the caller's default client.
    """
    pool: list[str] = []
    for ip in _configured_ips():
        if ip in _bad_ips:
            continue
        if _is_valid_ip(ip):
            pool.append(ip)
        else:
            log.critical("llm_source_ip.invalid_ip", ip=ip, env=_ENV)
            with _lock:
                _bad_ips.add(ip)
    return pool


def is_enabled() -> bool:
    """True when at least one usable source IP is configured."""
    return bool(_valid_pool())


def _next_ip() -> Optional[str]:
    """Round-robin the next usable source IP, or None when the pool is empty."""
    global _counter
    pool = _valid_pool()
    if not pool:
        return None
    with _lock:
        ip = pool[_counter % len(pool)]
        _counter += 1
    return ip


def _mark_bad(ip: str) -> None:
    with _lock:
        _bad_ips.add(ip)
        _sync_clients.pop(ip, None)
        _async_clients.pop(ip, None)


def _errno_in(exc: BaseException, codes: set) -> bool:
    """True if any OSError REACHABLE from ``exc`` carries one of ``codes``.

    Traverses THREE links, not one: ``__cause__``, ``__context__``, and — the one whose
    absence was a 100%-of-calls outage — ``ExceptionGroup.exceptions``.

    WHY THE GROUP MATTERS. anyio's ``connect_tcp`` tries EVERY address a host resolves
    to. With ONE candidate it re-raises that lone OSError, so its errno sits directly on
    the ``__cause__`` chain and the old linear walk found it. With TWO OR MORE it raises
    ``OSError("All connection attempts failed")`` whose own ``errno is None`` and hangs
    the real per-address errors off a ``ExceptionGroup`` in ``__cause__``. So on any
    dual-stack or multi-A host — every Cloudflare-fronted API —
    the bind errno was one level deeper than this function could see, ``_is_bind_error``
    answered False, and the bind failure was RE-RAISED instead of falling back: the exact
    outcome this module's docstring promises can never happen.

    Measured on a real deployment (httpx 0.28.1 / anyio 4.14.2 / CPython 3.11)::

        ConnectError → ConnectError → OSError(errno=None, "All connection attempts
        failed") → ExceptionGroup → [OSError(99), OSError(99)]

    and every outbound LLM call died there, with zero successes, because the container's
    netns owns none of the host IPs configured in the pool.

    Duck-typed on ``.exceptions`` rather than ``isinstance(BaseExceptionGroup, ...)`` so
    behaviour is identical on Python < 3.11 where that builtin does not exist. Bounded by
    ``_CHAIN_MAX_NODES`` and id-deduplicated, so a cyclic or pathological chain cannot
    spin here.
    """
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack and len(seen) < _CHAIN_MAX_NODES:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, OSError) and cur.errno in codes:
            return True
        # ExceptionGroup members (the anyio multi-address shape). Guarded by isinstance
        # so a non-group object that merely happens to carry `.exceptions` is ignored.
        subs = getattr(cur, "exceptions", None)
        if subs:
            try:
                stack.extend(s for s in subs if isinstance(s, BaseException))
            except TypeError:      # not iterable — not a group after all
                pass
        for nxt in (cur.__cause__, cur.__context__):
            if nxt is not None:
                stack.append(nxt)
    return False


def _is_bind_error(exc: BaseException) -> bool:
    """True when this call should fall back to the caller's unbound client."""
    return _errno_in(exc, _BIND_ERRNOS)


def _should_retire(exc: BaseException) -> bool:
    """True only when the SOURCE IP is at fault and must leave the pool. An
    address-family mismatch is about the DESTINATION, so it never retires an IP."""
    return _errno_in(exc, _RETIRE_ERRNOS)


# ── client factories (test seams) ────────────────────────────────────────────

def _build_sync_client(ip: str) -> httpx.Client:
    """Sync client whose outbound socket binds to ``ip``. Config mirrors the default."""
    return httpx.Client(
        transport=httpx.HTTPTransport(local_address=ip, limits=_SYNC_LIMITS),
        timeout=_TIMEOUT,
    )


def _build_async_client(ip: str) -> httpx.AsyncClient:
    """Async client whose outbound socket binds to ``ip``. Config mirrors the default."""
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(local_address=ip, limits=_ASYNC_LIMITS),
        timeout=_TIMEOUT,
    )


def _sync_client_for(ip: str) -> httpx.Client:
    client = _sync_clients.get(ip)
    if client is not None:
        return client
    with _lock:
        client = _sync_clients.get(ip)
        if client is None:
            client = _build_sync_client(ip)
            _sync_clients[ip] = client
    return client


def _async_client_for(ip: str) -> httpx.AsyncClient:
    client = _async_clients.get(ip)
    if client is not None:
        return client
    with _lock:
        client = _async_clients.get(ip)
        if client is None:
            client = _build_async_client(ip)
            _async_clients[ip] = client
    return client


# ── public POST wrappers ──────────────────────────────────────────────────────

def post_sync(default_client: httpx.Client, url: str, *, json, headers, timeout):
    """POST via a rotating source-IP-bound sync client; fall back to ``default_client``.

    UNSET/empty ``LLM_SOURCE_IPS`` → uses ``default_client`` directly (no binding).
    A bind failure retires the IP and falls back to ``default_client`` for this call.
    Any non-bind error is re-raised for the caller's existing retry/breaker to handle.
    """
    ip = _next_ip()
    if ip is None:
        return default_client.post(url, json=json, headers=headers, timeout=timeout)
    try:
        client = _sync_client_for(ip)
    except Exception as e:  # client build failed — never lose the call
        log.critical("llm_source_ip.build_failed", ip=ip, mode="sync", error=str(e)[:200])
        _mark_bad(ip)
        return default_client.post(url, json=json, headers=headers, timeout=timeout)
    try:
        return client.post(url, json=json, headers=headers, timeout=timeout)
    except Exception as e:
        if _is_bind_error(e):
            log.critical("llm_source_ip.bind_failed_fallback", ip=ip, mode="sync",
                         error=str(e)[:200], retired=_should_retire(e))
            if _should_retire(e):
                _mark_bad(ip)
            return default_client.post(url, json=json, headers=headers, timeout=timeout)
        raise


async def post_async(default_client: httpx.AsyncClient, url: str, *, json, headers, timeout):
    """Async twin of :func:`post_sync`."""
    ip = _next_ip()
    if ip is None:
        return await default_client.post(url, json=json, headers=headers, timeout=timeout)
    try:
        client = _async_client_for(ip)
    except Exception as e:  # client build failed — never lose the call
        log.critical("llm_source_ip.build_failed", ip=ip, mode="async", error=str(e)[:200])
        _mark_bad(ip)
        return await default_client.post(url, json=json, headers=headers, timeout=timeout)
    try:
        return await client.post(url, json=json, headers=headers, timeout=timeout)
    except Exception as e:
        if _is_bind_error(e):
            log.critical("llm_source_ip.bind_failed_fallback", ip=ip, mode="async",
                         error=str(e)[:200], retired=_should_retire(e))
            if _should_retire(e):
                _mark_bad(ip)
            return await default_client.post(url, json=json, headers=headers, timeout=timeout)
        raise


# ── lifecycle / test hooks ────────────────────────────────────────────────────

def reset() -> None:
    """Clear rotation counter, cached clients and the bad-IP set (TEST/diagnostic hook)."""
    global _counter
    with _lock:
        _counter = 0
        for c in _sync_clients.values():
            try:
                c.close()
            except Exception:
                pass
        _sync_clients.clear()
        _async_clients.clear()  # async clients closed by process shutdown / GC
        _bad_ips.clear()


def close_all() -> None:
    """Close all cached sync source-IP clients. Call from process shutdown paths."""
    with _lock:
        for c in _sync_clients.values():
            try:
                c.close()
            except Exception:
                pass
        _sync_clients.clear()
