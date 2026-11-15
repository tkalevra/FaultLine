"""Cross-process coordination over Redis — ONE client, ONE key builder, always tenant-scoped.

WHY THIS MODULE EXISTS
──────────────────────
An audit of every Redis touch-point in this repo found nine independent
``redis.from_url(...)`` construction sites, **three different fallback URLs**
(``faultline-redis``, ``localhost``, ``redis``), fifteen unregistered key prefixes, and
exactly one real key-builder (``chunk_queue.RedisWorkQueue._k``). Keys are otherwise inline
f-strings, so "is this key tenant-scoped?" is answered by reading fifteen call sites.

That is the shape a cross-tenant leak hides in. This module is the convergence point:

* ``client()``     — one cached, timeout-bounded, fail-safe connection.
* ``Scope``        — a value object you cannot construct from a bare string, so an
                     unscoped key is not merely discouraged, it is **unrepresentable**.
* ``key()``        — the only sanctioned key builder. Rejects a ``str`` scope by TYPE.

THREE HARD RULES (each has a test)
──────────────────────────────────
1. **Redis is OPTIONAL.** Absent, unreachable, or erroring → every entry point here returns
   the "no shared opinion" answer and the caller keeps its in-process behaviour. A
   coordination bug must never become an outage. PostgreSQL remains the authoritative
   coordinator; Redis coordinates, it does not decide.
2. **Every key is scoped.** ``key()`` takes a ``Scope``, never a string. The factories
   (``scope_for_account`` / ``scope_for_endpoint`` / ``scope_single_tenant``) each validate
   and each raise ``UnscopedKeyError`` on anything empty, placeholder-ish, or unknown.
3. **Nothing is enabled by default.** Every capability here is independently flag-gated and
   defaults OFF, so this file changes no behaviour until an operator turns a lever on.

WHAT IS DELIBERATELY *NOT* HERE
───────────────────────────────
No lock around ``confirmed_count`` — Postgres ``ON CONFLICT DO UPDATE`` is already atomic
there, and a Redis lock would add a failure mode to a correct path. Refuted, not forgotten.

PRIVACY NOTE
────────────
A BYO-brain ``base_url`` can carry an API key in the URL (some providers do this). Endpoint
identity therefore enters a key ONLY as a salted digest — ``scope_for_endpoint`` hashes, and
the plaintext URL is never written to Redis nor logged from this module.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from typing import Any, Optional

try:  # pragma: no cover - import guard: redis is a core dep, guarded for import-safety
    import redis as _redis
except Exception:  # pragma: no cover
    _redis = None  # type: ignore

try:  # pragma: no cover
    import structlog
    log = structlog.get_logger()
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger("redis_coord")


# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────
# One URL, one default. The audit found main.py defaulting to `localhost` and the
# re_embedder to `redis` while everyone else used `faultline-redis`; compose always sets
# REDIS_URL so it never bit, but three defaults means one of them is wrong by construction.
_DEFAULT_URL = "redis://faultline-redis:6379/0"


def _truthy(v: Optional[str]) -> bool:
    return str(v or "").strip().lower() not in ("", "false", "0", "no", "off")


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def redis_url() -> str:
    return os.environ.get("REDIS_URL", _DEFAULT_URL)


def enabled() -> bool:
    """Master lever. OFF (default) → this module is inert and nothing here talks to Redis."""
    return _truthy(os.environ.get("REDIS_COORD", "false"))


def _socket_timeout() -> float:
    # Bounded so a wedged Redis can never add unbounded latency to an LLM call.
    return _num("REDIS_COORD_SOCKET_TIMEOUT", 1.0)


# ──────────────────────────────────────────────────────────────────────────────
# CLIENT — cached, fail-safe, never raises outward
# ──────────────────────────────────────────────────────────────────────────────
_client: Any = None
_client_built = False
_client_lock = threading.Lock()
_last_fail_log = 0.0


def client() -> Any:
    """The shared client, or None. NEVER raises — None means "no shared opinion available"."""
    global _client, _client_built
    if not enabled():
        return None
    if _client_built:
        return _client
    with _client_lock:
        if _client_built:
            return _client
        _client_built = True
        if _redis is None:
            log.warning("redis_coord.library_missing")
            _client = None
            return None
        try:
            c = _redis.from_url(
                redis_url(),
                decode_responses=True,
                socket_connect_timeout=_socket_timeout(),
                socket_timeout=_socket_timeout(),
                health_check_interval=30,
            )
            c.ping()
            _client = c
            log.info("redis_coord.connected")
        except Exception as e:  # noqa: BLE001 — Redis is OPTIONAL, by contract
            _note_degraded("connect", e)
            _client = None
    return _client


def reset_client() -> None:
    """Test/diagnostic hook — drop the cached client so a new URL/flag takes effect."""
    global _client, _client_built
    with _client_lock:
        _client = None
        _client_built = False
    # Cached Lua Script objects are bound to the client they were registered on — drop them
    # with the client or a swapped URL would run against a dead connection.
    _reset_scripts()


def _note_degraded(op: str, exc: BaseException) -> None:
    """Log a Redis failure LOUDLY but at most once per minute (a dead Redis fails hot)."""
    global _last_fail_log
    now = time.time()
    if now - _last_fail_log < 60:
        return
    _last_fail_log = now
    log.warning("redis_coord.degraded_to_in_process",
                op=op, error_type=type(exc).__name__, error=str(exc)[:200],
                note="coordination is unavailable; every caller keeps its in-process "
                     "behaviour. This is a DEGRADATION, not a failure.")


# ──────────────────────────────────────────────────────────────────────────────
# SCOPE — an unscoped key must be UNREPRESENTABLE, not merely discouraged
# ──────────────────────────────────────────────────────────────────────────────
class UnscopedKeyError(ValueError):
    """Raised when a key would be built without a real, validated tenant/brain scope."""


# Values that LOOK like a scope but are the absence of one. A caller that passes any of
# these has a resolution bug upstream; silently accepting it writes one tenant's state to a
# key another tenant also resolves to. Fail loud.
_PLACEHOLDERS = frozenset({
    "none", "null", "nil", "undefined", "unknown", "anonymous", "default",
    "all", "any", "global", "shared", "*", "-", "n/a", "na", "test", "user",
})

_SCOPE_TOKEN = "\x00scope\x00"  # unforgeable-ish marker; a plain str can never carry it


class Scope:
    """A validated coordination scope. Construct ONLY via the module factories.

    ``key()`` accepts a ``Scope`` and rejects a ``str`` by type, so the poison case
    (``key("flbrk", "", ...)`` / ``key("flbrk", account_id_that_is_None, ...)``) cannot
    reach Redis — it raises before a command is issued.
    """

    __slots__ = ("kind", "ident", "_tok")

    def __init__(self, kind: str, ident: str, _tok: str = "") -> None:
        if _tok != _SCOPE_TOKEN:
            raise UnscopedKeyError(
                "Scope() is not directly constructible — use scope_for_account(), "
                "scope_for_endpoint() or scope_single_tenant() so the value is VALIDATED."
            )
        self.kind = kind
        self.ident = ident
        self._tok = _tok

    def __str__(self) -> str:
        return f"{self.kind}.{self.ident}"

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Scope {self.kind}.{self.ident}>"

    def __eq__(self, other: object) -> bool:
        return (isinstance(other, Scope)
                and other.kind == self.kind and other.ident == self.ident)

    def __hash__(self) -> int:
        return hash((self.kind, self.ident))


def _validate(raw: Any, what: str) -> str:
    if raw is None:
        raise UnscopedKeyError(f"{what} is None — refusing to build an unscoped key")
    s = str(raw).strip()
    if not s:
        raise UnscopedKeyError(f"{what} is empty — refusing to build an unscoped key")
    if s.lower() in _PLACEHOLDERS:
        raise UnscopedKeyError(
            f"{what}={s!r} is a placeholder, not a real scope — refusing to build a key "
            f"that two different tenants could resolve to"
        )
    return s


def _digest(raw: str, n: int = 20) -> str:
    """Short SHA-256 hex of an IDENTIFIER, used to build Redis key names — not a verifier.

    HASHING DECISION (CodeQL py/weak-sensitive-data-hashing, 2026-09-14): NOT password
    hashing. Every caller in this module (the closed set is pinned by
    tests/test_hashing_decision.py) passes one of:
      * ``account_id`` / ``user_id``          — opaque tenant identifiers (``scope_for_account``,
                                               ``scope_for_seat``);
      * ``"<base_url>|<model>"``               — a brain's endpoint identity (``scope_for_endpoint``);
      * the tenant's BYO provider ``api_key``  — a provider-minted random credential (never a
                                               human password), truncated to 16 hex = 64 bits
                                               and folded into ``scope_for_brain``'s identity
                                               tuple so two customers on the same public
                                               endpoint never share a cache;
      * that identity tuple itself.
    Nothing is ever AUTHENTICATED against a ``_digest`` output: it partitions rate budgets,
    breakers and caches, it grants no access, and the input is either non-secret or a
    high-entropy random key (no "limited input space" — the precondition for a slow KDF in
    CodeQL's rule text and in NIST SP 800-63B-4 §3.1.2.2, which requires a password-hashing
    scheme only below 112 bits). Truncation to ``n`` hex chars is deliberate: a partition
    key, not a preimage-resistant store of the secret.
    """
    # codeql[py/weak-sensitive-data-hashing] FALSE-BY-NAME: the source `raw` is an identifier (account_id / user_id / "base_url|model" / the identity tuple) or a provider-minted random api_key — never a password; the truncated digest partitions Redis keys and is never authenticated against; closed caller set pinned by tests/test_hashing_decision.py. CQ-hardening adjudication (codeql-hardening round_1/REPORT.md, Family E): migrating to blake2b/sha3 would change EVERY digest byte for zero security gain — the outputs are volatile Redis partition keys (rate budgets/breakers/caches) and are never compared against a stored secret; the only PERSISTED hash in the sha256 family is saas/keys.py::hash_key → account_api_keys.key_hash, which cannot be migrated at all because the full keys are never stored (a migration = re-minting every issued customer key), and CodeQL's own rule text scopes slow hashing to inputs with LIMITED input space (NIST SP 800-63B-4 §3.1.2.2) — CSPRNG 256-bit keys and opaque ids have none.
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:n]


def scope_for_account(account_id: Any) -> Scope:
    """The SaaS control-plane grain: one account, many seats."""
    return Scope("acct", _digest(_validate(account_id, "account_id")), _SCOPE_TOKEN)


def scope_for_seat(user_id: Any) -> Scope:
    """The data-plane grain: one seat. Use when the blast radius should be a single seat."""
    return Scope("seat", _digest(_validate(user_id, "user_id")), _SCOPE_TOKEN)


def scope_for_endpoint(base_url: Any, model: Any = "") -> Scope:
    """A BRAIN's identity (endpoint + model), hashed.

    Hashed, never plaintext: some providers carry the API key inside the base URL, and this
    value would otherwise be written to Redis and appear in key dumps.
    """
    url = _validate(base_url, "base_url")
    mdl = str(model or "").strip()
    return Scope("brain", _digest(f"{url.rstrip('/')}|{mdl}"), _SCOPE_TOKEN)


def scope_for_brain(cfg: Any) -> Optional[Scope]:
    """A RESOLVED brain's identity — the scope that follows the BRAIN, not the account row.

    This is the scope the rate budget, the circuit breaker, and the ingest cache key on. It is
    what makes the owner's scoping rule hold BY CONSTRUCTION:

      * corporate/team account, one shared brain  → every seat resolves the SAME cfg
        → SAME brain scope → SHARED budget + SHARED cache. (``brain_scope == 'account'``;
        owner is the ``account_id``.)
      * house/individual account, per-seat brains  → each seat resolves its OWN cfg
        → DISTINCT brain scopes → ISOLATED budgets + caches even though they share the
        ``account_id`` of the "Singles" house account. (``brain_scope == 'seat'``; owner is
        the ``seat_user_id``.)
      * two customers on the same public endpoint+model → DISTINCT api_keys → DISTINCT
        brain scopes → NEVER share a cache (never serve one an answer computed for another).

    Identity tuple (all normalized, then digested):
    ``(owner_kind, owner_id, base_url, model, sha256(api_key)[:16])``.

    ``cfg`` is the dict returned by ``saas.seat_brain.resolve_brain`` /
    ``saas.llm_config._build_config`` (carries ``brain_scope``/``seat_user_id``/``account_id``
    /``base_url``/``model``/``api_key``), or the ``{"no_brain": True}`` sentinel, or None.

    FAIL-SAFE: any missing critical field, any placeholder, the no-brain sentinel, a non-dict,
    or any construction error → None. A None here means the caller falls back (local bucket,
    no cache, no shared breaker state) — it NEVER builds an unscoped key and NEVER raises.
    """
    if not isinstance(cfg, dict):
        return None
    try:
        if cfg.get("no_brain"):
            return None
        base = str(cfg.get("base_url") or "").strip().rstrip("/")
        model = str(cfg.get("model") or "").strip()
        if not base or not model:
            return None
        # Owner discrimination is read FROM the cfg the resolver already stamped — no saas
        # import here (this module is FOSS-stripped on public push; the discrimination logic
        # lives in saas/seat_brain and travels in the dict).
        if str(cfg.get("brain_scope") or "").strip().lower() == "seat":
            owner = str(cfg.get("seat_user_id") or "").strip().lower()
        else:
            owner = str(cfg.get("account_id") or "").strip()
        # _validate raises UnscopedKeyError on placeholder/empty — convert to None (fail-safe).
        try:
            owner = _validate(owner, "brain_owner")
        except UnscopedKeyError:
            return None
        api_key = str(cfg.get("api_key") or "")
        # A digest of the key material is part of the identity so the same endpoint+model with
        # DIFFERENT keys (different customers on a public provider) cannot share a cache. The
        # digest is one-way; the plaintext key never reaches a key string or Redis.
        key_hash = _digest(api_key, 16) if api_key else "noky"
        ident = f"{owner}|{base}|{model}|{key_hash}"
        return Scope("brain", _digest(ident), _SCOPE_TOKEN)
    except Exception:  # noqa: BLE001 — a scope that cannot be proven must not be invented
        return None


def scope_single_tenant() -> Scope:
    """The open-core, single-deployment scope.

    This exists so the non-SaaS path has an EXPLICIT scope instead of an empty one. It is a
    deliberate act to call it — you cannot arrive here by passing ``""`` to a factory.
    """
    return Scope("solo", "single_tenant", _SCOPE_TOKEN)


_NS_RE_OK = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


def key(namespace: str, scope: Scope, *parts: Any) -> str:
    """THE key builder. ``fl:{namespace}:{scope}:{parts...}``.

    Raises ``UnscopedKeyError`` unless ``scope`` is a validated ``Scope`` — passing a bare
    string (even a correct-looking account uuid) is refused, because the point of the type
    is that validation happened somewhere auditable.
    """
    if not isinstance(scope, Scope):
        raise UnscopedKeyError(
            f"key() requires a Scope, got {type(scope).__name__}. Build one with "
            f"scope_for_account()/scope_for_seat()/scope_for_endpoint()/scope_single_tenant()."
        )
    ns = str(namespace or "").strip().lower()
    if not ns or not set(ns) <= _NS_RE_OK:
        raise UnscopedKeyError(f"namespace={namespace!r} must be [a-z0-9_]+")
    tail = ":".join(_validate(p, "key part") for p in parts) if parts else ""
    base = f"fl:{ns}:{scope.kind}:{scope.ident}"
    return f"{base}:{tail}" if tail else base


# ──────────────────────────────────────────────────────────────────────────────
# CAPABILITY 1 — SHARED CIRCUIT-BREAKER EVIDENCE
# ──────────────────────────────────────────────────────────────────────────────
# The breaker is per-PROCESS today, so N API workers hold N independent opinions about the
# same brain: worker A's open breaker does not protect workers B..N, and the half-open
# recovery probe fires N times at an endpoint that is already struggling. `/health` reports
# only the process that answered the request, so the health surface is not truthful either.
#
# Shared state is an OVERLAY, never a replacement: the local breaker still decides, and the
# shared view can only ADD an open verdict it would not otherwise have had. Redis down →
# the overlay contributes nothing and behaviour is exactly the in-process breaker.

_NS_BREAKER = "brk"


def breaker_enabled() -> bool:
    return enabled() and _truthy(os.environ.get("REDIS_COORD_BREAKER", "false"))


def _open_cache_ttl() -> float:
    """Seconds to reuse a shared open/closed read. Bounds round-trips on the hot path."""
    return _num("REDIS_COORD_OPEN_CACHE_MS", 1000.0) / 1000.0


_open_cache: dict[str, tuple[float, float]] = {}   # key -> (checked_at, open_until)
_open_cache_lock = threading.Lock()


def publish_open(scope: Scope, open_for_seconds: float) -> bool:
    """Announce "this brain is out" to every other process. False when unavailable."""
    if not breaker_enabled():
        return False
    c = client()
    if c is None:
        return False
    k = key(_NS_BREAKER, scope, "open")   # scope validated BEFORE touching the client
    try:
        ttl = max(1, int(open_for_seconds))
        c.set(k, str(time.time() + open_for_seconds), ex=ttl)
        with _open_cache_lock:
            _open_cache.pop(k, None)
        return True
    except Exception as e:  # noqa: BLE001
        _note_degraded("publish_open", e)
        return False


def shared_open_until(scope: Scope) -> Optional[float]:
    """Epoch seconds this brain is considered open until, per the SHARED view. None = no
    shared opinion (Redis off/down, or nobody has reported it open)."""
    if not breaker_enabled():
        return None
    c = client()
    if c is None:
        return None
    k = key(_NS_BREAKER, scope, "open")   # scope validated BEFORE touching the client
    now = time.time()
    with _open_cache_lock:
        hit = _open_cache.get(k)
        if hit and (now - hit[0]) < _open_cache_ttl():
            return hit[1] if hit[1] > now else None
    try:
        raw = c.get(k)
        until = float(raw) if raw else 0.0
    except Exception as e:  # noqa: BLE001
        _note_degraded("shared_open_until", e)
        return None
    with _open_cache_lock:
        _open_cache[k] = (now, until)
    return until if until > now else None


def clear_shared_open(scope: Scope) -> None:
    """A success anywhere clears the shared verdict — recovery must not wait for the TTL."""
    if not breaker_enabled():
        return
    c = client()
    if c is None:
        return
    k = key(_NS_BREAKER, scope, "open")   # scope validated BEFORE touching the client
    try:
        c.delete(k)
        with _open_cache_lock:
            _open_cache.pop(k, None)
    except Exception as e:  # noqa: BLE001
        _note_degraded("clear_shared_open", e)


def reset_open_cache() -> None:
    """Test hook."""
    with _open_cache_lock:
        _open_cache.clear()


# ──────────────────────────────────────────────────────────────────────────────
# CAPABILITY 2 — LEARNED CONTEXT-WINDOW CACHE
# ──────────────────────────────────────────────────────────────────────────────
# `context_window.py` learns a brain's real `n_ctx` from the server's own 400 rejection.
# That knowledge is per-PROCESS, so every worker must overrun ONCE to learn it — N workers,
# N wasted oversized requests, N capture misses, and the number is re-learned from scratch
# after every deploy. It is small, it is not secret (a token count), and it is a property of
# the BRAIN, so it caches cleanly per endpoint+model.

_NS_WINDOW = "ctxwin"


def window_cache_enabled() -> bool:
    return enabled() and _truthy(os.environ.get("REDIS_COORD_WINDOW", "false"))


def _window_ttl() -> int:
    return int(_num("REDIS_COORD_WINDOW_TTL", 86400))


def publish_window(scope: Scope, window: int) -> bool:
    """Share an AUTHORITATIVE learned window (server_rejection only — never a guess)."""
    if not window_cache_enabled():
        return False
    c = client()
    if c is None:
        return False
    # Key FIRST, outside the swallowing try: a scoping bug must be loud no matter what the
    # client does. (Found by test: `c.set(key(...))` evaluates the attribute lookup on `c`
    # before the argument, so a broken client could mask an UnscopedKeyError.)
    k = key(_NS_WINDOW, scope, "n_ctx")
    try:
        w = int(window)
        if w <= 0:
            return False
        c.set(k, str(w), ex=_window_ttl())
        return True
    except Exception as e:  # noqa: BLE001
        _note_degraded("publish_window", e)
        return False


def read_window(scope: Scope) -> Optional[int]:
    """A window another process already learned the hard way. None = nobody knows yet."""
    if not window_cache_enabled():
        return None
    c = client()
    if c is None:
        return None
    k = key(_NS_WINDOW, scope, "n_ctx")   # scope validated BEFORE touching the client
    try:
        raw = c.get(k)
        if not raw:
            return None
        w = int(raw)
        return w if w > 0 else None
    except Exception as e:  # noqa: BLE001
        _note_degraded("read_window", e)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# CAPABILITY 3 — CROSS-PROCESS LLM CONCURRENCY CEILING
# ──────────────────────────────────────────────────────────────────────────────
# `_EXTRACT_SEMAPHORE = asyncio.Semaphore(8)` is constructed INSIDE the request handler, so
# it bounds ONE request to 8 in-flight calls and bounds N concurrent requests to 8N — with
# no global cap at all, against a llama.cpp server with 4 slots. Measured: 11 in-flight on
# one lightly-loaded seat; 316 ConnectTimeouts at 3ms host-level connect latency. Those
# timeouts are DEPENDENCY-class failures, so self-inflicted over-subscription is exactly the
# thing that opens the breaker for everyone on the account.
#
# A lease-scored ZSET is the right primitive: a crashed holder's slot expires by SCORE, so a
# process death cannot permanently consume capacity (which a plain INCR/DECR counter would).
#
# FAIL-**OPEN** is deliberate and load-bearing: if Redis is down, a call proceeds
# unthrottled — exactly today's behaviour. Fail-closed here would convert a monitoring
# outage into an ingest outage, which the brief forbids.

_NS_SEM = "llmsem"

_LUA_SEM_ACQUIRE = """
redis.call('zremrangebyscore', KEYS[1], '-inf', ARGV[1])
if redis.call('zcard', KEYS[1]) < tonumber(ARGV[3]) then
  redis.call('zadd', KEYS[1], ARGV[2], ARGV[4])
  redis.call('expire', KEYS[1], ARGV[5])
  return 1
end
return 0
"""


def semaphore_enabled() -> bool:
    return enabled() and _truthy(os.environ.get("REDIS_COORD_LLM_SEMAPHORE", "false"))


def llm_concurrency_limit() -> int:
    """Global in-flight LLM ceiling for one brain. Default 4 == a stock llama.cpp -np 4."""
    try:
        return max(1, int(os.environ.get("LLM_GLOBAL_CONCURRENCY", "4")))
    except (TypeError, ValueError):
        return 4


def _lease_seconds() -> float:
    return _num("REDIS_COORD_LLM_LEASE_SECONDS", 120.0)


_sem_script: Any = None
_sem_lock = threading.Lock()


def _acquire_script(c: Any) -> Any:
    global _sem_script
    if _sem_script is None:
        with _sem_lock:
            if _sem_script is None:
                _sem_script = c.register_script(_LUA_SEM_ACQUIRE)
    return _sem_script


def try_acquire_slot(scope: Scope, holder: str) -> bool:
    """Take one global in-flight slot. True = held (or Redis unavailable → fail-OPEN)."""
    if not semaphore_enabled():
        return True
    c = client()
    if c is None:
        return True
    k = key(_NS_SEM, scope, "inflight")   # scope validated BEFORE touching the client
    now = time.time()
    lease = _lease_seconds()
    try:
        got = _acquire_script(c)(
            keys=[k],
            args=[now, now + lease, llm_concurrency_limit(), holder, int(lease * 2)],
        )
        return bool(got)
    except Exception as e:  # noqa: BLE001 — fail-OPEN: never lose a call to a Redis blip
        _note_degraded("try_acquire_slot", e)
        return True


def release_slot(scope: Scope, holder: str) -> None:
    """Give the slot back. A missed release expires by lease score — never a permanent leak."""
    if not semaphore_enabled():
        return
    c = client()
    if c is None:
        return
    k = key(_NS_SEM, scope, "inflight")   # scope validated BEFORE touching the client
    try:
        c.zrem(k, holder)
    except Exception as e:  # noqa: BLE001
        _note_degraded("release_slot", e)


def inflight_count(scope: Scope) -> Optional[int]:
    """Observability: how many slots are held right now. None when unavailable."""
    if not semaphore_enabled():
        return None
    c = client()
    if c is None:
        return None
    k = key(_NS_SEM, scope, "inflight")   # scope validated BEFORE touching the client
    try:
        c.zremrangebyscore(k, "-inf", time.time())
        return int(c.zcard(k))
    except Exception as e:  # noqa: BLE001
        _note_degraded("inflight_count", e)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# CAPABILITY 5 — SHARED OUTBOUND RATE BUDGET (the account's TOTAL call count)
# ──────────────────────────────────────────────────────────────────────────────
# `llm_calls._rate_buckets` is a module-level dict, so the bucket is PER-PROCESS. The API
# container runs TWO python processes against the SAME account key — `uvicorn src.api.main`
# and `python -m src.re_embedder.embedder` — so a provider limit of N RPM is enforced twice
# INDEPENDENTLY and real outbound is up to 2N. Measured on prod 2026-08-11: two
# `llm_rate_limit.bucket_created` lines for one account key in one container's log, both at
# effective_rps=0.083 (5 RPM), against a Cerebras Free tier whose ceiling IS 5 RPM.
#
# The re_embedder is the volume: CLASSIFY_CHAIN 176 log-lines/hour vs ~2 for user traffic.
# So the process that the account limit was NOT counting is the one doing nearly all the
# calling — which is exactly how an account "within its limit" 429s continuously.
#
# This makes the budget an ACCOUNT-WIDE total: one bucket in Redis, refilled by elapsed time,
# decremented atomically in Lua so two processes cannot double-spend the same token.
#
# FAIL-**OPEN**, like the semaphore above: Redis down/absent → returns "no shared opinion" and
# the caller keeps its in-process bucket, i.e. today's behaviour. A coordination outage must
# never become an ingest outage. Postgres/live traffic remain authoritative; this coordinates.

_NS_RATE = "llmrate"

# Refill by elapsed time, then take one token if the balance allows. Returns
# {wait_ms, paused} (wait 0 = token granted). Single round-trip, atomic, so concurrent
# processes cannot double-spend the same token.
#   KEYS[1] bucket   ARGV[1] now_ms  ARGV[2] rate_per_sec  ARGV[3] capacity  ARGV[4] ttl_s
#
# PERSISTS rate+cap INTO THE HASH so read-only probes (brain_available_tokens /
# brain_rate_per_sec) can answer "is there capacity?" and "when does a token mint?" WITHOUT
# spending a token and WITHOUT a second source of truth. Backward-compat: a hash written by
# an older build (no `rate`/`cap` fields) → the probes return None → callers fail-safe.
#
# THE LOOP, CLOSED IN THE SCRIPT (this is what makes the learned pace cross-process):
#   * `floor_rps`  — a ceiling ANY process learned (measured from headers, or assumed from
#     the 429 fallback table) CLAMPS the refill rate for EVERY process. Without this, the
#     process that saw the 429 paces at the learned rate while every other process keeps
#     refilling (and re-persisting) the un-learned rate — the shared bucket would flap
#     between two opinions and the tighter one would only hold between the learner's takes.
#   * `pause_until_ms` — a hard pause any process observed (429 with the budget exhausted)
#     gates EVERY process: no token is spent until the provider's window resets. Waiters
#     see the pause remaining, so they adopt it locally and extend their acquire cap.
#
# The CLAMPED rate is what gets persisted, so `rate` in the hash is always the pace every
# process is actually being refilled at — the number the Brain Health bar shows as "live".
# The take also honours a BRAIN-SCOPED MAINTENANCE window (`maint_until_ms`, returned as the
# paused flag 2): a user-driven capacity probe (Detect-cap) quiesces the brain so its burst
# measures the PROVIDER, not the background traffic. Same no-token-spent semantics as a pause,
# but a DISTINCT field so the Brain Health bar (which reads `pause_until_ms` to say
# "rate-limited") never renders a user-driven probe as a provider throttling. A real provider
# pause outranks it in the return so a genuine 429-window is never misread as maintenance.
_LUA_RATE_TAKE = """
local st = redis.call('hmget', KEYS[1], 'tokens', 'ts', 'floor_rps', 'pause_until_ms',
                      'maint_until_ms', 'floor_cap')
local now = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local cap = tonumber(ARGV[3])
local floor = tonumber(st[3])
local pause_ms = tonumber(st[4])
local maint_ms = tonumber(st[5])
local fcap = tonumber(st[6])
-- GUARDED ON THE FLOOR'S EXISTENCE, NOT ON rate > floor. Conditioning the CAP decision
-- on that comparison made this entire block dead in the live path: _get_rate_bucket sets
-- target = min(rate, floor) before _shared_budget_wait forwards self.rate, so the two
-- arrive EQUAL and nothing here ran. The rate clamp below is still comparison-guarded --
-- that one genuinely only applies when the caller is faster than the floor.
if floor ~= nil and floor > 0 then
  -- A FLOOR TIGHTENS THE SUSTAINED RATE. IT IS NOT A STATEMENT ABOUT BURST.
  -- This used to read `cap = min(cap, max(1, ceil(rate)))`, which collapsed capacity to 1
  -- for any floor below 1 rps -- i.e. for every per-minute provider. And this is the
  -- AUTHORITATIVE path (see _TokenBucket._try_acquire: "when the shared budget has an
  -- opinion it is AUTHORITATIVE"), so it silently overrode the correctly-sized local
  -- bucket and re-imposed the metronome under exactly the bare-429 condition the local
  -- fix was written for. Capacity 1 with a 12s refill is not a token bucket.
  --
  -- Capacity is a WINDOW ALLOWANCE -- a request count per window -- so it is preserved,
  -- with the same precedence the in-process path uses: the burst learned WITH the floor
  -- (floor_cap) when we have one, otherwise the allowance the caller already resolved
  -- from config/registry and passed in ARGV[3]. The rate is still clamped to the floor
  -- below, untouched: sustained throughput stays exactly what the provider permits and
  -- only burst-within-window is restored. No limit is widened.
  --
  -- PRECEDENCE. Only ONE thing justifies a burst under a bound floor: floor_cap, the
  -- window allowance learned from real headers TOGETHER WITH that floor. It describes the
  -- provider's policy AS OF the 429 that taught us the floor.
  --
  -- A config/registry allowance does NOT qualify, and an earlier revision of this block
  -- wrongly accepted it. Once a floor binds, the provider has told us it is refusing us;
  -- an allowance resolved before that is stale by definition. Worse, it is unbounded --
  -- the registry publishes rpm 1000 for Anthropic and Cerebras, so a learned back-off of
  -- 0.025 rps (1.5 req/MINUTE) still granted 1000 instant permits, and the operator's own
  -- LLM_RATE_BURST / LLM_MAX_RPM were honoured locally and discarded here, on the
  -- authoritative path. That inverted the guard into a hole.
  --
  -- Both surviving branches are bounded by the caller's capacity, which already encodes
  -- the operator's knobs. A floor can only ever TIGHTEN burst, never widen it.
  if rate > floor then
    rate = floor
  end
  if fcap ~= nil and fcap > 0 then
    cap = math.min(cap, math.max(1.0, fcap))
  else
    cap = math.min(cap, math.max(1.0, math.ceil(rate)))
  end
end
local ttl = tonumber(ARGV[4])
-- NEVER SHORTEN. This ttl is derived from the caller's capacity/rate and is typically 60s,
-- while publish_brain_floor deliberately sets 86400 so a learned back-off outlives an idle
-- period. The expire below is unconditional, so a single take was collapsing the floor's
-- lifetime to the taker's -- silently discarding a ceiling the system had just learned, and
-- with it the floor_cap that the burst decision depends on. Extend only, matching the
-- max()-extend semantics _LUA_FLOOR_SET and _LUA_PAUSE_SET already use. (ttl returns -1 for
-- "no expiry" and -2 for "no key", both correctly below any positive ttl.)
local cur_ttl = redis.call('ttl', KEYS[1])
if cur_ttl ~= nil and cur_ttl > ttl then
  ttl = cur_ttl
end
if pause_ms ~= nil and pause_ms > now then
  ttl = math.max(ttl, math.ceil((pause_ms - now) / 1000.0) + 60)
  redis.call('expire', KEYS[1], ttl)
  return {math.ceil(pause_ms - now), 1}
end
if maint_ms ~= nil and maint_ms > now then
  ttl = math.max(ttl, math.ceil((maint_ms - now) / 1000.0) + 60)
  redis.call('expire', KEYS[1], ttl)
  return {math.ceil(maint_ms - now), 2}
end
local tokens = tonumber(st[1])
local ts = tonumber(st[2])
if tokens == nil or ts == nil then
  tokens = cap
  ts = now
end
local elapsed = math.max(0, now - ts) / 1000.0
tokens = math.min(cap, tokens + (elapsed * rate))
local wait_ms = 0
if tokens >= 1.0 then
  tokens = tokens - 1.0
else
  if rate <= 0 then
    return {-1, 0}
  end
  wait_ms = math.ceil(((1.0 - tokens) / rate) * 1000.0)
end
redis.call('hmset', KEYS[1], 'tokens', tokens, 'ts', now, 'rate', rate, 'cap', cap)
redis.call('expire', KEYS[1], ttl)
return {wait_ms, 0}
"""

# Publish a learned/assumed ceiling INTO the bucket hash, with rank-based precedence so an
# ASSUMPTION can never overwrite a MEASUREMENT even when two processes race (the check and
# the write are one atomic script). A clear (rps <= 0) is UNCONDITIONAL — same semantics as
# ``_TokenBucket.set_learner_floor``: a clear is how an endpoint switch drops a stale floor.
# TTL is only ever EXTENDED (never shortened below what a running pause/bucket already has).
#   KEYS[1] bucket   ARGV[1] floor_rps  ARGV[2] rank  ARGV[3] provenance  ARGV[4] ttl_s
_LUA_FLOOR_SET = """
local rps = tonumber(ARGV[1])
if rps ~= nil and rps > 0 then
  local cur = tonumber(redis.call('hget', KEYS[1], 'floor_rank'))
  if cur ~= nil and cur > tonumber(ARGV[2]) then
    return 0
  end
  redis.call('hset', KEYS[1], 'floor_rps', rps, 'floor_rank', tonumber(ARGV[2]),
             'floor_prov', ARGV[3])
else
  redis.call('hdel', KEYS[1], 'floor_rps', 'floor_rank', 'floor_prov')
end
local ttl = tonumber(ARGV[4])
local cur_ttl = redis.call('ttl', KEYS[1])
if cur_ttl < ttl then
  redis.call('expire', KEYS[1], ttl)
end
return 1
"""

# Publish a hard pause: NO tokens from this brain until the provider's window resets.
# max()-extend semantics (a second exhausted-window report cannot cut an earlier pause
# short), TTL stretched to outlive the pause so an idle brain cannot forget mid-pause.
#   KEYS[1] bucket   ARGV[1] now_ms  ARGV[2] pause_seconds  ARGV[3] min_ttl_s
_LUA_PAUSE_SET = """
local now = tonumber(ARGV[1])
local secs = math.max(0, tonumber(ARGV[2]))
local until_ms = now + secs * 1000.0
local cur = tonumber(redis.call('hget', KEYS[1], 'pause_until_ms'))
if cur ~= nil and cur > until_ms then
  until_ms = cur
end
redis.call('hset', KEYS[1], 'pause_until_ms', until_ms)
local ttl = math.max(tonumber(ARGV[3]), math.ceil(secs) + 60)
local cur_ttl = redis.call('ttl', KEYS[1])
if cur_ttl < ttl then
  redis.call('expire', KEYS[1], ttl)
end
return 1
"""


def shared_rate_enabled() -> bool:
    """Flag-gated and OFF by default, per this module's third hard rule."""
    return enabled() and _truthy(os.environ.get("REDIS_COORD_RATE_BUDGET", "false"))


_rate_scripts: dict[str, Any] = {}
_rate_script_lock = threading.Lock()


def _script(c: Any, name: str, body: str) -> Any:
    """Register (once) a named Lua script. Same caching pattern as the semaphore script."""
    global _rate_scripts
    s = _rate_scripts.get(name)
    if s is None:
        with _rate_script_lock:
            s = _rate_scripts.get(name)
            if s is None:
                s = c.register_script(body)
                _rate_scripts[name] = s
    return s


def _reset_scripts() -> None:
    """Test hook — a new client must re-register its scripts against the new connection."""
    with _rate_script_lock:
        _rate_scripts.clear()


def take_token_state(scope: Scope, rate_per_sec: float, capacity: float) -> Optional[dict]:
    """Spend one token from the BRAIN-WIDE budget shared by every process (rich form).

    The Lua script enforces the two cross-process invariants BEFORE spending:
    a learned/assumed ``floor_rps`` clamps the refill rate for every taker, and an active
    ``pause_until_ms`` refuses the take entirely (no token spent) until the pause lifts.

    Returns:
        ``{"wait_s": 0.0, "paused": False}``  — token granted, caller may send.
        ``{"wait_s": >0.0, "paused": bool}``  — seconds to wait (paced, or paused out).
        ``None``  — no shared opinion (disabled, Redis down, or a limiter error). The
        caller MUST fall back to its in-process bucket; never treat None as "granted"
        and never as "denied".
    """
    if not shared_rate_enabled():
        return None
    if rate_per_sec <= 0:
        return None
    c = client()
    if c is None:
        return None
    k = key(_NS_RATE, scope, "bucket")   # scope validated BEFORE touching the client
    try:
        # TTL: generous enough that an idle account's balance is not reset mid-pace, bounded
        # so a deleted account cannot leave a key behind forever.
        ttl = max(60, int((capacity / rate_per_sec) * 4) if rate_per_sec > 0 else 60)
        out = _script(c, "take", _LUA_RATE_TAKE)(
            keys=[k],
            args=[int(time.time() * 1000), float(rate_per_sec), float(max(1.0, capacity)), ttl],
        )
        wait_ms, flag = float(out[0]), int(out[1])
        if wait_ms < 0:
            return None
        # flag: 0 = granted/paced normally, 1 = provider hard-pause, 2 = brain maintenance
        # (user-driven probe). BOTH hold kinds report ``paused=True`` so every caller that
        # only knows the old boolean still holds rather than failing open; ``maintenance``
        # lets the pacer adopt the window WITHOUT poisoning the local provider-pause state
        # (a maintenance window is released by us, a provider pause by their clock).
        return {"wait_s": wait_ms / 1000.0, "paused": flag != 0,
                "maintenance": flag == 2}
    except Exception as e:  # noqa: BLE001 — fail-OPEN to the local bucket, never lose a call
        _note_degraded("take_token_state", e)
        return None


def try_take_token(scope: Scope, rate_per_sec: float, capacity: float) -> Optional[float]:
    """Back-compat thin form of ``take_token_state`` — the wait in seconds, or None."""
    st = take_token_state(scope, rate_per_sec, capacity)
    return None if st is None else st["wait_s"]


# ── learned ceiling + hard pause — the fields that make the loop survive processes ─────
# The pacer writes these the moment it LEARNS them (a measured header, a 429 fallback, an
# exhausted window), not on its next take, so a second process — or the Tenant Admin's
# Brain Health poll — sees the new ceiling immediately. The take script enforces both.

# How long a learned/assumed ceiling stays in force after the last activity on the bucket.
# Takes refresh the underlying hash TTL, so this is the floor's IDLE lifetime — a brain
# nobody paces for a day re-learns its ceiling from its next response, which is the honest
# behavior (a ceiling is a property of the provider's CURRENT tier, not of history).
def _brain_floor_ttl() -> int:
    try:
        v = int(float(os.environ.get("REDIS_COORD_FLOOR_TTL_S", "86400")))
        return v if v > 0 else 86400
    except (ValueError, TypeError):
        return 86400


def publish_brain_floor(scope: Scope, floor_rps: float, rank: int,
                        provenance: str = "", capacity: float = 0.0,
                        source_tag: str = "") -> bool:
    """Record THIS brain's learned/assumed ceiling in the shared bucket hash. Never raises.

    ``rank`` is the caller's evidence rank (llm_calls._FLOOR_RANK); the Lua refuses a
    rank-LOWER write so an assumption can never overwrite a measurement even when two
    processes race. ``floor_rps <= 0`` CLEARS the floor (unconditional, mirroring
    ``_TokenBucket.set_learner_floor``). False = unavailable/refused, never an exception.

    ``capacity`` (>0) is the burst allowance learned WITH the floor (the provider's
    per-window request count, e.g. 5-per-minute) — written as ``floor_cap`` so the other
    process in the container can adopt the same burst shape instead of re-metronoming to
    capacity=1. Not rank-gated: it rides whatever write the Lua accepted; a refused
    rank leaves any existing floor_cap in place. ``source_tag`` is the ENDPOINT TAG the
    floor was learned from (written as ``floor_src``) — the seed-on-create path reads it
    back verbatim so the stale-floor clear keeps working across process restarts.
    """
    if not shared_rate_enabled() or not isinstance(scope, Scope):
        return False
    c = client()
    if c is None:
        return False
    try:
        k = key(_NS_RATE, scope, "bucket")   # scope validated BEFORE touching the client
    except Exception:  # noqa: BLE001 — an unscoped/invalid key must never reach Redis
        return False
    try:
        got = _script(c, "floor", _LUA_FLOOR_SET)(
            keys=[k],
            args=[float(max(0.0, floor_rps or 0.0)), int(rank), str(provenance or ""),
                  _brain_floor_ttl()],
        )
        cap = float(capacity or 0.0)
        if floor_rps <= 0:
            c.hdel(k, "floor_cap", "floor_src")
        elif got and cap > 0:
            # floor_src: the endpoint tag the floor was learned FROM, persisted so a
            # restarted process seeds the tag verbatim — re-tagging with the CURRENT
            # endpoint would make the stale-floor clear (endpoint mismatch) unfireable
            # and permanently under-pace a switched-to provider (critic, 2026-08-15).
            c.hset(k, mapping={"floor_cap": cap, "floor_src": str(source_tag or "")})
        elif got:
            # A NEW FLOOR WITH NO CAPACITY RETIRES THE OLD BURST. floor_cap only means
            # anything as the window allowance learned WITH the floor it shipped with, and
            # this write has replaced that floor. Leaving it made a burst outlive the floor
            # that justified it: the bare-429 decay path publishes a TIGHTER rate with no
            # capacity, so each successive 429 halved the rate while the burst learned
            # before any of them stayed put — the pacing tightens and the burst never does.
            # floor_cap ONLY. floor_src is a property of the FLOOR, not of the burst:
            # it is the endpoint tag the seed-on-create path matches on, and dropping it
            # here made every capacity-less tightening -- i.e. every bare-429 decay, the
            # common case -- orphan the floor so no restarted or sibling process could
            # ever adopt it again for its whole 86400s life.
            c.hdel(k, "floor_cap")
        return bool(got)
    except Exception as e:  # noqa: BLE001 — learning must never break a live call
        _note_degraded("publish_brain_floor", e)
        return False


def brain_floor_state(scope: Scope) -> Optional[dict]:
    """The learned/assumed ceiling in force for this brain, or None. Never raises.

    ``{"floor_rps": float, "rank": int, "provenance": str}`` — ``provenance`` is the
    label the caller passed at publish (``measured`` / ``assumed_table`` / …), so a
    reader can tell a MEASURED ceiling from an ASSUMED one without a second key.
    """
    if not shared_rate_enabled() or not isinstance(scope, Scope):
        return None
    c = client()
    if c is None:
        return None
    try:
        k = key(_NS_RATE, scope, "bucket")
    except Exception:  # noqa: BLE001
        return None
    try:
        rps, rank, prov, fcap, fsrc = c.hmget(
            k, "floor_rps", "floor_rank", "floor_prov", "floor_cap", "floor_src")
        if rps is None:
            return None
        out = {"floor_rps": float(rps), "rank": int(rank or 0),
               "provenance": str(prov or "")}
        if fcap is not None:
            try:
                out["floor_cap"] = float(fcap)
            except (TypeError, ValueError):
                pass
        if fsrc is not None:
            out["floor_src"] = str(fsrc)
        return out
    except Exception as e:  # noqa: BLE001
        _note_degraded("brain_floor_state", e)
        return None


def publish_brain_pause(scope: Scope, seconds: float) -> bool:
    """Record THIS brain's hard pause (429 with the budget exhausted). Never raises.

    max()-extend semantics inside the script: a second exhausted-window report cannot cut
    an earlier pause short. The hash TTL is stretched to outlive the pause, so an idle
    brain cannot forget mid-pause.
    """
    if not shared_rate_enabled() or not isinstance(scope, Scope):
        return False
    c = client()
    if c is None:
        return False
    try:
        k = key(_NS_RATE, scope, "bucket")   # scope validated BEFORE touching the client
    except Exception:  # noqa: BLE001
        return False
    try:
        got = _script(c, "pause", _LUA_PAUSE_SET)(
            keys=[k],
            args=[int(time.time() * 1000), float(max(0.0, seconds)), 60],
        )
        return bool(got)
    except Exception as e:  # noqa: BLE001 — a pause must never break a live call
        _note_degraded("publish_brain_pause", e)
        return False


def brain_pause_remaining(scope: Scope) -> Optional[float]:
    """Seconds left in this brain's shared hard pause, or None/0.0 when not paused.

    None = no shared opinion (off/Redis down/hash absent) → caller fails safe.
    """
    if not shared_rate_enabled() or not isinstance(scope, Scope):
        return None
    c = client()
    if c is None:
        return None
    try:
        k = key(_NS_RATE, scope, "bucket")
    except Exception:  # noqa: BLE001
        return None
    try:
        raw = c.hget(k, "pause_until_ms")
        if raw is None:
            return None
        remaining_ms = float(raw) - time.time() * 1000.0
        return max(0.0, remaining_ms / 1000.0)
    except Exception as e:  # noqa: BLE001
        _note_degraded("brain_pause_remaining", e)
        return None


# ── brain-scoped MAINTENANCE — the quiesce behind a user-driven capacity probe ─────────
#
# Detect-cap (the tenant console's "detect ceiling" control) needs EVERY outbound LLM lane
# on this brain to stop while it measures the provider's real request ceiling: the rate gate
# (the take script above), the re-embedder yield gate (supervisor._brain_busy), and ingest
# admission (ceiling_admission.acquire_interactive) all consult this window. Cached calls are
# untouched by construction — the cache fires BEFORE the gate.
#
# DISTINCTIONS THAT MATTER:
#   * ``maint_until_ms`` is NOT ``pause_until_ms``. A pause is the PROVIDER's verdict (a 429
#     exhausted the window); maintenance is OUR act (a user clicked a button). The bar and the
#     logs must never confuse the two, so they live in separate fields of the same hash.
#   * The window is a TIMESTAMP, so it is SELF-EXPIRING: a probe whose process dies mid-flight
#     cannot leave the brain paused forever — the field is inert the moment its deadline passes.
#   * Release (``end_brain_maintenance``) is a plain HDEL — immediate, cross-process, and
#     idempotent. Best-effort callers may call it in a ``finally`` as often as they like.

# Publish/extend a maintenance window: max()-extend semantics (a second extend cannot cut an
# earlier window short), TTL stretched to outlive the window.
#   KEYS[1] bucket   ARGV[1] now_ms  ARGV[2] seconds  ARGV[3] min_ttl_s
_LUA_MAINT_SET = """
local now = tonumber(ARGV[1])
local secs = math.max(0, tonumber(ARGV[2]))
local until_ms = now + secs * 1000.0
local cur = tonumber(redis.call('hget', KEYS[1], 'maint_until_ms'))
if cur ~= nil and cur > until_ms then
  until_ms = cur
end
redis.call('hset', KEYS[1], 'maint_until_ms', until_ms)
local ttl = math.max(tonumber(ARGV[3]), math.ceil(secs) + 60)
local cur_ttl = redis.call('ttl', KEYS[1])
if cur_ttl < ttl then
  redis.call('expire', KEYS[1], ttl)
end
return 1
"""


def begin_brain_maintenance(scope: Scope, seconds: float) -> bool:
    """Open/extend THIS brain's maintenance window (user-driven probe in flight). Never raises.

    Cross-process by construction — the window lives in the shared bucket hash, so the API
    process, the re-embedder, and any other worker all see it on their next take/probe.
    """
    if not shared_rate_enabled() or not isinstance(scope, Scope):
        return False
    c = client()
    if c is None:
        return False
    try:
        k = key(_NS_RATE, scope, "bucket")   # scope validated BEFORE touching the client
    except Exception:  # noqa: BLE001
        return False
    try:
        got = _script(c, "maint", _LUA_MAINT_SET)(
            keys=[k],
            args=[int(time.time() * 1000), float(max(0.0, seconds)), 60],
        )
        return bool(got)
    except Exception as e:  # noqa: BLE001 — maintenance must never break its own caller
        _note_degraded("begin_brain_maintenance", e)
        return False


def end_brain_maintenance(scope: Scope) -> bool:
    """Release THIS brain's maintenance window (probe ended — cancelled, finished, or failed).

    Idempotent and unconditional: a ``finally`` may call it as often as it likes. Returns
    False only when coordination is unavailable (the window then self-expires at its
    deadline — the fail-safe is time, not this call). Never raises.
    """
    if not shared_rate_enabled() or not isinstance(scope, Scope):
        return False
    c = client()
    if c is None:
        return False
    try:
        k = key(_NS_RATE, scope, "bucket")
    except Exception:  # noqa: BLE001
        return False
    try:
        c.hdel(k, "maint_until_ms")
        return True
    except Exception as e:  # noqa: BLE001
        _note_degraded("end_brain_maintenance", e)
        return False


def brain_maintenance_remaining(scope: Scope) -> Optional[float]:
    """Seconds left in this brain's maintenance window, or None/0.0 when none is in force.

    None = no shared opinion (off/Redis down/hash absent/no window ever set) → the caller
    fails safe (does not yield/defer on maintenance). Never raises.
    """
    if not shared_rate_enabled() or not isinstance(scope, Scope):
        return None
    c = client()
    if c is None:
        return None
    try:
        k = key(_NS_RATE, scope, "bucket")
    except Exception:  # noqa: BLE001
        return None
    try:
        raw = c.hget(k, "maint_until_ms")
        if raw is None:
            return None
        remaining_ms = float(raw) - time.time() * 1000.0
        return max(0.0, remaining_ms / 1000.0)
    except Exception as e:  # noqa: BLE001
        _note_degraded("brain_maintenance_remaining", e)
        return None


def brain_available_tokens(scope: Scope) -> Optional[float]:
    """Read-only probe of the shared brain bucket's CURRENT (refill-adjusted) token balance.

    Lets the re-embedder supervisor YIELD to interactive traffic (Deliverable C) AND lets the
    ceiling-admission gate decide whether capacity exists for an interactive call (Deliverable D).
    None = coordination unavailable (Redis down / no shared budget / local-only bucket / a hash
    written before rate+cap were persisted) → the caller fails safe (upkeep runs; admission
    proceeds). Never raises.

    Reads the SAME Redis hash the Lua script writes. Since the Lua `_LUA_RATE_TAKE` now persists
    `rate` and `cap` alongside `tokens`+`ts`, this probe applies the SAME refill-by-elapsed-time
    the limiter does — so the value reflects capacity that has minted since the last take, without
    spending a token. (Before rate+cap were persisted this returned a stale last-committed value;
    the old behaviour is the fallback when the fields are absent.) A shared HARD PAUSE in force
    returns 0.0 — the take script spends no tokens until the window resets, and a probe that
    reported phantom available capacity would make the ceiling-admission gate and the re-embedder
    supervisor disagree with the pacer. A MAINTENANCE window (user-driven probe) returns 0.0 for
    the same reason: nothing can be spent, so no reader may report spendable capacity.
    """
    if not shared_rate_enabled():
        return None
    if not isinstance(scope, Scope):
        return None
    c = client()
    if c is None:
        return None
    try:
        k = key(_NS_RATE, scope, "bucket")  # scope validated BEFORE touching the client
    except Exception:  # noqa: BLE001 — an unscoped/invalid key must never reach Redis
        return None
    try:
        tokens, ts, rate, cap, pause_ms, maint_ms = c.hmget(
            k, "tokens", "ts", "rate", "cap", "pause_until_ms", "maint_until_ms")
        if tokens is None:
            return None  # bucket absent → no shared budget in force → no signal
        now_ms = time.time() * 1000.0
        if pause_ms is not None and float(pause_ms) > now_ms:
            return 0.0  # paused → the pacer can spend nothing; neither can anyone else
        if maint_ms is not None and float(maint_ms) > now_ms:
            return 0.0  # maintenance → same: the take script refuses every spend
        tokens = float(tokens)
        # Apply refill-on-read when the persisted rate+cap are available (post-fix hashes).
        # This mirrors the Lua script's refill exactly so the probe and the limiter agree.
        if rate is not None and cap is not None and ts is not None:
            try:
                r = float(rate)
                cp = float(cap)
                elapsed = max(0.0, (time.time() * 1000.0 - float(ts)) / 1000.0)
                return min(cp, tokens + elapsed * r)
            except (TypeError, ValueError):
                return tokens  # malformed fields → fall back to raw last-committed balance
        return tokens  # pre-fix hash (no rate/cap) → conservative last-committed value
    except Exception as e:  # noqa: BLE001 — fail-safe: no opinion, never break the caller
        _note_degraded("brain_available_tokens", e)
        return None


def brain_rate_per_sec(scope: Scope) -> Optional[float]:
    """Read-only probe of the shared brain bucket's configured refill rate (tokens/sec), or None.

    The ceiling-admission gate (Deliverable D) needs this to answer "will a token mint within
    the turn budget?" — without it the gate cannot tell a SATURATED brain (defer) from a brain
    with capacity arriving shortly (wait), so it cannot safely defer and the fail-open it
    replaces stays live. None = coordination unavailable / hash absent / pre-fix hash without
    the persisted `rate` field → caller fails safe (proceed). Never raises.
    """
    if not shared_rate_enabled():
        return None
    if not isinstance(scope, Scope):
        return None
    c = client()
    if c is None:
        return None
    try:
        k = key(_NS_RATE, scope, "bucket")
    except Exception:  # noqa: BLE001
        return None
    try:
        rate = c.hget(k, "rate")
        if rate is None:
            return None
        return float(rate)
    except Exception as e:  # noqa: BLE001
        _note_degraded("brain_rate_per_sec", e)
        return None


def brain_bucket_state(scope: Scope) -> Optional[dict]:
    """Read-only snapshot of a brain's shared token bucket, or None. Never raises.

    ``{"tokens": float, "rate_per_sec": float, "capacity": float, "saturation": float,
    "pause_remaining_s": float}``

    Exists so a MONITORING surface can show "how close is this brain to its ceiling" without
    reaching into this module's private ``_NS_RATE`` key name or spending a token to find out.
    ``brain_available_tokens`` answers "is there capacity" and ``brain_rate_per_sec`` answers
    "when does one mint"; neither exposes the CAPACITY, so neither can express utilisation as a
    fraction — a bare "3 tokens left" means nothing without the burst size it is 3 out of.

    ``saturation`` is ``1 - tokens/capacity`` clamped to [0,1]: 0.0 = fully replenished (idle),
    1.0 = burst exhausted and every further call is paced by the refill rate. Returns None
    unless tokens, rate AND cap are all present — a partial answer would be read as a real
    measurement. Refill-on-read matches ``brain_available_tokens`` exactly so the two probes
    can never disagree about the same bucket. ``pause_remaining_s`` > 0 → a shared hard pause
    is in force (a 429 taught the brain its window was exhausted); the take script spends no
    tokens until it lifts.
    """
    if not shared_rate_enabled():
        return None
    if not isinstance(scope, Scope):
        return None
    c = client()
    if c is None:
        return None
    try:
        k = key(_NS_RATE, scope, "bucket")  # scope validated BEFORE touching the client
    except Exception:  # noqa: BLE001
        return None
    try:
        tokens, ts, rate, cap, pause_ms = c.hmget(
            k, "tokens", "ts", "rate", "cap", "pause_until_ms")
        if tokens is None or rate is None or cap is None or ts is None:
            return None  # absent or pre-fix hash → no honest snapshot to give
        tok = float(tokens)
        r = float(rate)
        cp = float(cap)
        if cp <= 0:
            return None
        elapsed = max(0.0, (time.time() * 1000.0 - float(ts)) / 1000.0)
        tok = min(cp, tok + elapsed * r)
        sat = 1.0 - (tok / cp)
        sat = 0.0 if sat < 0.0 else (1.0 if sat > 1.0 else sat)
        pause_s = 0.0
        if pause_ms is not None:
            pause_s = max(0.0, (float(pause_ms) - time.time() * 1000.0) / 1000.0)
        return {"tokens": tok, "rate_per_sec": r, "capacity": cp, "saturation": sat,
                "pause_remaining_s": pause_s}
    except Exception as e:  # noqa: BLE001 — fail-safe: no opinion, never break the caller
        _note_degraded("brain_bucket_state", e)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# CAPABILITY 4 — PER-TENANT SWEEP WORK FINGERPRINTS
# ──────────────────────────────────────────────────────────────────────────────
# The re_embedder's per-tenant sweep re-does identical work every REEMBED_INTERVAL regardless
# of whether anything changed — measured 153 LLM calls / 20 min on pre-prod, and on prod 12
# calls that ALL FAILED against an unroutable BYO brain, every cycle, forever. The fix is a
# cheap per-subsystem digest of the rows that subsystem reads, compared against the digest of
# its last completed run. That comparison needs somewhere to remember the previous value; this
# is that somewhere.
#
# WHY THIS AND NOT A POSTGRES TABLE: the marker is pure coordination, not truth. Losing it is
# free (the work runs once more), it must never take a lock on a tenant schema — the sweep's
# lock-holding is the very thing this reduces — and Redis expiry gives the staleness ceiling
# for nothing. Postgres stays the authoritative coordinator; this is an optimisation hint.
#
# WHY THE VALUE IS PLAINTEXT: a marker is a tuple of CARDINALITIES ("25|…|31|214"). Nothing
# secret, and an operator asking "why did this skip?" can read the answer directly instead of
# reproducing a hash. (Contrast scope_for_endpoint, which hashes because a base_url can carry
# an API key.)
#
# FAIL DIRECTION IS "RUN": every read here returns None on any failure, and None means the
# caller has no shared opinion and must do the work.

# ──────────────────────────────────────────────────────────────────────────────
# CAPABILITY — PUBLISHED RATE-CEILING ASSUMPTION (transparency, not enforcement)
# ──────────────────────────────────────────────────────────────────────────────
# When a provider 429s WITHOUT any rate headers, the pacer falls back to a published-limits
# table (or a blind back-off) and the resulting ceiling is an ASSUMPTION, not a measurement.
# The tenant has to be told that, so the assumption is published here for the console to read.
#
# THIS IS STATE, NOT A GATE. Nothing in the request path reads it; the pace itself lives on
# the in-process token bucket. It exists because the pacer runs in whichever uvicorn worker
# took the call while the console read runs in another, so a process-local flag would show
# the disclaimer to some tenants and not others at random.
#
# SCOPE IS THE ISOLATION. The key is built by ``key()`` from a validated ``Scope``, so an
# assumption recorded for one brain is unreadable under any other — a tenant-supplied brain's
# assumption covers exactly the seats that share that brain, and a seat with its own brain
# gets its own. There is no unscoped variant of this call by construction.
#
# Gated on ``enabled()`` ALONE — deliberately NOT on ``shared_rate_enabled()``, which is off
# by default. The disclaimer must reach the tenant whether or not the cross-process rate
# BUDGET is switched on, because the assumption is applied either way.
_NS_RATE_ASSUME = "llmrateassume"


def _rate_assumption_ttl() -> int:
    """Seconds an unrefreshed assumption stays visible. Default 24h.

    Bounded so a disclaimer cannot outlive the condition that produced it forever, but long
    enough that a brain which 429s once a day still reads as assumed rather than flickering
    between "assumed" and "no reading".
    """
    try:
        v = int(float(os.environ.get("LLM_RATE_ASSUMPTION_TTL_S", "86400")))
        return v if v > 0 else 86400
    except (ValueError, TypeError):
        return 86400


def publish_rate_assumption(scope: Scope, payload: dict) -> bool:
    """Record that THIS brain's ceiling is an assumption (or a refusal). Never raises."""
    if not enabled() or not isinstance(scope, Scope) or not isinstance(payload, dict):
        return False
    c = client()
    if c is None:
        return False
    try:
        k = key(_NS_RATE_ASSUME, scope, "state")
    except Exception:  # noqa: BLE001
        return False
    try:
        flat = {str(kk): ("" if vv is None else str(vv)) for kk, vv in payload.items()}
        c.hset(k, mapping=flat)
        c.expire(k, _rate_assumption_ttl())
        return True
    except Exception as e:  # noqa: BLE001 — transparency must never break a live call
        _note_degraded("publish_rate_assumption", e)
        return False


def read_rate_assumption(scope: Scope) -> Optional[dict]:
    """The assumption/refusal recorded for this brain, or None. Never raises."""
    if not enabled() or not isinstance(scope, Scope):
        return None
    c = client()
    if c is None:
        return None
    try:
        k = key(_NS_RATE_ASSUME, scope, "state")
    except Exception:  # noqa: BLE001
        return None
    try:
        raw = c.hgetall(k)
        if not raw:
            return None
        out = {}
        for kk, vv in raw.items():
            kk = kk.decode() if isinstance(kk, bytes) else str(kk)
            vv = vv.decode() if isinstance(vv, bytes) else str(vv)
            out[kk] = vv
        return out or None
    except Exception as e:  # noqa: BLE001
        _note_degraded("read_rate_assumption", e)
        return None


def clear_rate_assumption(scope: Scope) -> None:
    """Drop the assumption — called when a REAL measured header replaces it. Never raises."""
    if not enabled() or not isinstance(scope, Scope):
        return
    c = client()
    if c is None:
        return
    try:
        c.delete(key(_NS_RATE_ASSUME, scope, "state"))
    except Exception as e:  # noqa: BLE001
        _note_degraded("clear_rate_assumption", e)


_NS_SWEEP = "swpfp"


def sweep_marker_enabled() -> bool:
    return enabled() and _truthy(os.environ.get("REEMBED_SWEEP_SKIP", "false"))


def read_sweep_marker(scope: Scope, subsystem: str) -> Optional[str]:
    """The fingerprint recorded by this scope's last completed run of ``subsystem``.

    None = no shared opinion: the capability is off, Redis is unavailable, the marker expired,
    or this tenant/subsystem has never been recorded. Every one of those means RUN THE WORK.
    """
    if not sweep_marker_enabled():
        return None
    c = client()
    if c is None:
        return None
    k = key(_NS_SWEEP, scope, subsystem)   # scope validated BEFORE touching the client
    try:
        raw = c.get(k)
        return str(raw) if raw else None
    except Exception as e:  # noqa: BLE001
        _note_degraded("read_sweep_marker", e)
        return None


def write_sweep_marker(scope: Scope, subsystem: str, fingerprint: str,
                       ttl_seconds: int) -> bool:
    """Record a completed run's input fingerprint. False when unavailable (never raises).

    The TTL is a STALENESS CEILING, and the caller must not refresh it on a skip — a marker
    that is renewed every time it is honoured never expires, and a subsystem stuck on unchanged
    input (a transient LLM failure that left its rows undecided) would then be skipped forever.
    Writing only on a completed run is what keeps the ceiling real.
    """
    if not sweep_marker_enabled():
        return False
    c = client()
    if c is None:
        return False
    # Key FIRST, outside the swallowing try — a scoping bug must be loud regardless of the
    # client's state (same lesson as publish_window).
    k = key(_NS_SWEEP, scope, subsystem)
    try:
        fp = str(fingerprint or "")
        if not fp:
            return False
        c.set(k, fp, ex=max(1, int(ttl_seconds)))
        return True
    except Exception as e:  # noqa: BLE001
        _note_degraded("write_sweep_marker", e)
        return False


def clear_sweep_marker(scope: Scope, subsystem: str) -> None:
    """Force the next cycle to run this subsystem (ops lever / test hook). Never raises."""
    c = client()
    if c is None:
        return
    k = key(_NS_SWEEP, scope, subsystem)
    try:
        c.delete(k)
    except Exception as e:  # noqa: BLE001
        _note_degraded("clear_sweep_marker", e)


def purge_scope(scope: Scope) -> int:
    """Delete every coordination key for one scope (tenant teardown / test cleanup)."""
    c = client()
    if c is None:
        return 0
    n = 0
    patterns = [f"{key(ns, scope)}*"
                for ns in (_NS_BREAKER, _NS_WINDOW, _NS_SEM, _NS_SWEEP, _NS_RATE,
                           _NS_RATE_ASSUME)]
    try:
        for pat in patterns:
            for k in c.scan_iter(match=pat, count=200):
                c.delete(k)
                n += 1
    except Exception as e:  # noqa: BLE001
        _note_degraded("purge_scope", e)
    return n
