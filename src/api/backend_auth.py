"""Backend (:8000) service authentication: defence in depth behind the port binding.

SPDX-License-Identifier: AGPL-3.0-only

The backend API trusts the ``user_id`` its caller sends. The MCP server (:8002) is the front
door that authenticates people (seat tokens / the MCP key) and resolves the tenant, so only
trusted callers may reach the backend. There are two layers:

1. **Network.** docker-compose publishes :8000 on 127.0.0.1 only.
2. **Service secret, always on.** Every API request must carry ``X-FaultLine-Backend-Secret``.
   The value is ``FAULTLINE_BACKEND_SECRET`` when the operator sets it. Otherwise the backend
   mints a random one at boot and stores it in ``public.backend_service_secret``, the one place
   every trusted caller (the MCP container, the in-container re-embedder, the backend's own
   self-calls) can already reach. Zero config, never logged.

   Exempt: ``/health`` (liveness probes), ``/api/dashboard/*`` (its own operator bearer), the
   operator console's static files, and a request carrying the operator bearer
   (``FAULTLINE_ADMIN_TOKEN``). **A source address is never a credential.** Under rootless
   podman a host process arrives with the container's own IP, and with host networking every
   local process is 127.0.0.1. If no secret can be resolved, API requests are refused (fail
   closed).
"""

from __future__ import annotations

import hmac
import os
import secrets as _secrets
import socket
from functools import lru_cache
from typing import Optional

from fastapi import HTTPException, Request, status

BACKEND_SECRET_ENV = "FAULTLINE_BACKEND_SECRET"
BACKEND_SECRET_HEADER = "X-FaultLine-Backend-Secret"

_EXEMPT_PATHS = frozenset({"/health", "/api/dashboard"})
_EXEMPT_PREFIXES = ("/api/dashboard/",)

_cached_db_secret: Optional[str] = None


def safe_equals(presented: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time compare that never raises.

    ``hmac.compare_digest`` raises TypeError on a non-ASCII ``str``, and Starlette decodes
    header bytes as latin-1, so a single byte >= 0x80 in a credential header used to turn into
    a 500. Comparing UTF-8 bytes turns it into a clean mismatch."""
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented.encode("utf-8", "surrogateescape"),
                               expected.encode("utf-8", "surrogateescape"))


def _dsn() -> str:
    return (os.environ.get("POSTGRES_DSN") or "").strip()


def _read_db_secret() -> Optional[str]:
    dsn = _dsn()
    if not dsn:
        return None
    try:
        import psycopg2
        with psycopg2.connect(dsn, connect_timeout=3) as db, db.cursor() as cur:
            cur.execute("SELECT secret FROM public.backend_service_secret WHERE id = 1")
            row = cur.fetchone()
            return row[0] if row and row[0] else None
    except Exception:  # noqa: BLE001: unreachable store means no secret, so callers fail closed
        return None


def backend_secret() -> Optional[str]:
    """Resolve the service secret: the env value, else the persisted row (cached once found)."""
    global _cached_db_secret
    env = (os.environ.get(BACKEND_SECRET_ENV) or "").strip()
    if env:
        return env
    if _cached_db_secret:
        return _cached_db_secret
    found = _read_db_secret()
    if found:
        _cached_db_secret = found
    return found


def ensure_backend_secret() -> bool:
    """Backend boot: make sure a secret exists. Returns True when one is resolvable.

    With FAULTLINE_BACKEND_SECRET set, nothing is stored. Otherwise one random value is
    inserted (first boot wins, ON CONFLICT DO NOTHING) and read back. The value is never
    logged or printed."""
    global _cached_db_secret
    if (os.environ.get(BACKEND_SECRET_ENV) or "").strip():
        return True
    dsn = _dsn()
    if not dsn:
        return False
    try:
        import psycopg2
        with psycopg2.connect(dsn, connect_timeout=5) as db, db.cursor() as cur:
            cur.execute(
                "INSERT INTO public.backend_service_secret (id, secret) VALUES (1, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (_secrets.token_urlsafe(32),),
            )
            cur.execute("SELECT secret FROM public.backend_service_secret WHERE id = 1")
            row = cur.fetchone()
        _cached_db_secret = row[0] if row and row[0] else None
    except Exception:  # noqa: BLE001
        return False
    return bool(_cached_db_secret)


def _reset_cache_for_tests() -> None:
    global _cached_db_secret
    _cached_db_secret = None


def backend_headers() -> dict:
    """Headers every trusted caller sends on every backend request ({} if unresolvable)."""
    s = backend_secret()
    return {BACKEND_SECRET_HEADER: s} if s else {}


def _operator_bearer_ok(authorization: Optional[str]) -> bool:
    if not authorization:
        return False
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    from src.api.operator_token import operator_token_ok
    return operator_token_ok(parts[1].strip())


@lru_cache(maxsize=1)
def _own_addresses() -> frozenset:
    """This container's own addresses. DIAGNOSTIC ONLY: they are logged next to a refusal,
    and are NEVER an authentication input (see the module docstring)."""
    addrs = {"127.0.0.1", "::1"}
    try:
        addrs.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return frozenset(addrs)


def caller_is_trusted(path: str, client_host: Optional[str], headers, *, static_files=()) -> bool:
    """The single decision. ``client_host`` is accepted for logging symmetry and deliberately
    IGNORED: an address is not a credential. Fails closed when no secret is resolvable."""
    if path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES):
        return True
    if path == "/" or path.lstrip("/") in static_files:
        return True
    if _operator_bearer_ok(headers.get("authorization")):
        return True
    return safe_equals(headers.get(BACKEND_SECRET_HEADER), backend_secret())


def require_service(request: Request) -> str:
    """FastAPI dependency for service-to-service /internal routes (MCP, re-embedder).

    Same rule as the middleware, stated on the route so the release gate's AST walk can see
    that every /internal route is gated."""
    host = request.client.host if request.client else None
    if not caller_is_trusted(request.url.path, host, request.headers):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="backend service credential required")
    return "service"
