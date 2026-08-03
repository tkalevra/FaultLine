"""FOSS control-plane dashboard API (operator-bearer-authenticated).

SPDX-License-Identifier: AGPL-3.0-only
License: GNU AGPL v3 — see ./LICENSE in the repo root.

This is the BACKEND the ``webui/`` console talks to. It drives the running
FaultLine stack: mint/revoke seats (per-end-user memory scopes with hashed
tokens), rotate the MCP key, persist the LLM Brain config so it takes effect,
and surface stack health. Every count, identity, and cap decision is derived
server-side from the DB; the webui is a view, never a source of truth.

Auth: a single operator bearer token (``FAULTLINE_ADMIN_TOKEN``). Constant-time
compared. If unset at boot, the lifespan loader mints a random one and logs it
once (the webui's login help documents this "first boot" behaviour). Every
``/api/dashboard/*`` endpoint requires it; 401 on missing/mismatch.

The seat cap (``FOSS_MAX_SEATS = 5``) is a SOURCE CONSTANT — deliberately NOT an
env var. Bumping it requires editing this file and redeploying (the bar for a
public FOSS repo). The cap check + insert are atomic via a Postgres transaction
advisory lock, so two concurrent mints cannot both pass count=4.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
import uuid
from collections import deque
from typing import Any, Optional

import psycopg2
import psycopg2.extras
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

log = structlog.get_logger(__name__)

# ─── THE SOURCE-LEVEL CAP ────────────────────────────────────────────────────
# Deliberately a module constant, NOT an env var. On a public FOSS repo an env-
# configurable cap lets any operator set FOSS_MAX_SEATS=99999 with one line in
# .env — "stupid silly to override". Bumping the cap must require editing source
# + redeploying. Truly un-bypassable is impossible for FOSS (anyone can edit the
# source) but the bar is: don't be lazy. Require a source edit, not a curl.
FOSS_MAX_SEATS: int = 5

# Honest pointer to the hosted offering, returned verbatim in the 409 body so the
# webui can surface it without baking product copy into two places.
_CAP_REACHED_HINT = (
    "FOSS = up to 5 seats; unlimited in the SaaS offering at https://faultline.ca"
)

FAULTLINE_VERSION = "1.0.0"

# Postgres advisory-lock key serializing seat mint (transaction-scoped). Keeps
# the cap count + insert atomic across concurrent callers.
_SEAT_MINT_LOCK_KEY = 147001

# sha256 of a credential — the only thing we ever persist or compare. The
# plaintext is returned exactly once (mint/rotate) and then forgotten.
def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ─── Operator bearer auth ────────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False, description="FAULTLINE_ADMIN_TOKEN bearer")

# Resolved once at import: the env value if set, else None. The lifespan loader
# in main.py may seed os.environ with an auto-minted token at boot; we re-read
# os.environ on each request so the auto-minted value is honoured immediately.
def _admin_token() -> Optional[str]:
    return (os.environ.get("FAULTLINE_ADMIN_TOKEN") or "").strip() or None


def require_operator(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> str:
    """Constant-time operator-token check. 401 on missing/mismatch/unconfigured."""
    expected = _admin_token()
    presented = creds.credentials if creds is not None else None
    # No token configured at all → fail loud (do not run an open control plane).
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="operator auth not configured: set FAULTLINE_ADMIN_TOKEN",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not presented:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return "operator"


# ─── Per-operator rate limiter (in-memory sliding window) ────────────────────
# Simple, FOSS-native. Keyed by operator (single operator today) + action class.
# 10/min on the mutating endpoints (mint / rotate / llm-save). GETs are cheap
# and unmetered. 429 + Retry-After on overflow.
_RATE_LIMIT = 10          # actions per window
_RATE_WINDOW_S = 60       # 60s window → 10/min
_rate_buckets: dict[str, deque[float]] = {}


def _check_rate(bucket_key: str, limit: int = _RATE_LIMIT, window_s: int = _RATE_WINDOW_S) -> None:
    now = time.time()
    dq = _rate_buckets.get(bucket_key)
    if dq is None:
        dq = deque()
        _rate_buckets[bucket_key] = dq
    cutoff = now - window_s
    while dq and dq[0] < cutoff:
        dq.popleft()
    if len(dq) >= limit:
        retry = max(1, int(dq[0] + window_s - now))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate limit exceeded — retry shortly",
            headers={"Retry-After": str(retry)},
        )
    dq.append(now)


# ─── DB helpers ──────────────────────────────────────────────────────────────

def _dsn() -> str:
    dsn = os.environ.get("POSTGRES_DSN", "")
    if not dsn:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="POSTGRES_DSN not configured",
        )
    return dsn


def _log_action(action: str, target_user_id: Optional[str] = None, detail: Any = None) -> None:
    """Append to the operator action log. Best-effort; never fails a request."""
    try:
        with psycopg2.connect(_dsn()) as db, db.cursor() as cur:
            cur.execute(
                "INSERT INTO public.dashboard_action_log (action, target_user_id, detail) "
                "VALUES (%s, %s, %s::jsonb)",
                (
                    action,
                    target_user_id,
                    psycopg2.extras.Json(detail) if detail is not None else None,
                ),
            )
    except Exception as exc:  # noqa: BLE001 — audit log must never break an op
        log.warning("dashboard.action_log.failed", action=action, error=str(exc)[:160])


# ─── LLM Brain persistence ───────────────────────────────────────────────────
# The override row (dashboard_llm_config, singleton id=1) is the authority the
# backend reads at startup AND whenever the operator PUTs a new config. Applying
# a PUT mutates the running process's os.environ and refreshes the cached chat
# URL in main so the very next LLM call uses it — no restart needed for the
# backend process. restart_required:true is still returned honestly because the
# re-embedder subprocess and the MCP container have their own env and only pick
# the new value up after a full stack restart.

def _load_llm_override_row() -> Optional[dict]:
    try:
        with psycopg2.connect(_dsn()) as db, db.cursor() as cur:
            cur.execute(
                "SELECT backend_type, base_url, model, api_key FROM public.dashboard_llm_config WHERE id = 1"
            )
            row = cur.fetchone()
            if not row:
                return None
            return {"backend_type": row[0], "base_url": row[1], "model": row[2], "api_key": row[3]}
    except Exception as exc:  # noqa: BLE001
        log.warning("dashboard.llm_override.load_failed", error=str(exc)[:160])
        return None


def apply_persisted_llm_override() -> None:
    """Read the persisted LLM override back into os.environ at startup.

    Called from the FastAPI lifespan BEFORE main resolves _LLM_URL, so a config
    saved from the webui survives a restart and is authoritative. Missing values
    are left untouched (env wins). Idempotent and fail-safe.
    """
    row = _load_llm_override_row()
    if not row:
        return
    if row.get("backend_type"):
        os.environ["LLM_BACKEND_TYPE"] = row["backend_type"]
    if row.get("base_url"):
        os.environ["LLM_BASE_URL"] = row["base_url"].rstrip("/")
    if row.get("model"):
        os.environ["WGM_LLM_MODEL"] = row["model"]
        os.environ["PATTERN_EXTRACTION_MODEL"] = row["model"]
    if row.get("api_key"):
        os.environ["LLM_API_KEY"] = row["api_key"]
    log.info("dashboard.llm_override.applied",
             backend_type=row.get("backend_type"), base_url=row.get("base_url"))


def _apply_llm_to_running_process(backend_type: str, base_url: str,
                                  model: Optional[str], api_key: Optional[str]) -> None:
    """Mutate os.environ + refresh main's cached chat URL so the next call uses it."""
    os.environ["LLM_BACKEND_TYPE"] = backend_type
    os.environ["LLM_BASE_URL"] = (base_url or "").rstrip("/")
    if model:
        os.environ["WGM_LLM_MODEL"] = model
        os.environ["PATTERN_EXTRACTION_MODEL"] = model
    if api_key is not None:  # empty string is a deliberate "clear the key"
        os.environ["LLM_API_KEY"] = api_key
    # Refresh main's cached chat URL + health cache so /health reflects it too.
    try:
        from src.api import main as _main  # local import avoids circular at module load
        _main._LLM_URL = _main._get_llm_url()
        _main._health_cache = None  # invalidate; rebuilt on next /health
    except Exception as exc:  # noqa: BLE001
        log.warning("dashboard.llm_apply.refresh_failed", error=str(exc)[:160])


# ─── MCP URL + filter script ─────────────────────────────────────────────────

def _resolve_mcp_url(request: Request) -> str:
    explicit = (os.environ.get("MCP_EXTERNAL_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    # Derive from the request host, swapping the port to the MCP port (8002),
    # mirroring the webui's own guessMcpBase() so the two never disagree.
    host = request.headers.get("host", "")
    if host:
        host_no_port = host.split(":", 1)[0]
        scheme = "https" if (request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https") else "http"
        return f"{scheme}://{host_no_port}:8002"
    return "http://localhost:8002"


def _read_filter_script() -> str:
    """Read the legacy OpenWebUI inlet/outlet filter the operator pastes in."""
    candidates = [
        os.path.join(os.getcwd(), "openwebui", "faultline_function.py"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "openwebui", "faultline_function.py"),
        "/app/openwebui/faultline_function.py",
    ]
    for path in candidates:
        try:
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as fh:
                    return fh.read()
        except Exception as exc:  # noqa: BLE001
            log.warning("dashboard.filter_script.read_failed", path=path, error=str(exc)[:120])
    return "-- filter script not found on disk; see openwebui/faultline_function.py in the repo --"


# ─── Request models ──────────────────────────────────────────────────────────

class SeatMintRequest(BaseModel):
    label: Optional[str] = Field(None, max_length=120)


class LLMConfigUpdate(BaseModel):
    backend_type: str = Field(..., max_length=40)
    base_url: str = Field(..., max_length=400)
    model: Optional[str] = Field(None, max_length=200)
    api_key: Optional[str] = Field(None, max_length=400)


# ─── Router ──────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/health")
def dashboard_health(_op: str = Depends(require_operator)) -> dict:
    """Aggregate the existing /health data for the dashboard pills."""
    try:
        from src.api import main as _main
        health = _main.health()  # reuse the live, cached collector
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — degrade gracefully
        log.warning("dashboard.health.aggregate_failed", error=str(exc)[:160])
        return {"database": "unreachable", "qdrant": "unreachable",
                "llm": "unreachable", "re_embedder": "unknown", "llm_config": "unknown"}
    return {
        "database": health.get("database", "unknown"),
        "qdrant": health.get("qdrant", "unknown"),
        "llm": health.get("llm", "unknown"),
        "re_embedder": health.get("re_embedder", "unknown"),
        "llm_config": health.get("llm_config", "unknown"),
    }


@router.get("/config")
def dashboard_config(request: Request, _op: str = Depends(require_operator)) -> dict:
    """Non-secret instance summary. The webui renders every key except those
    matching secret/api_key/token/password — so only safe fields are included."""
    from src.api.llm_client import get_llm_config
    try:
        llm = get_llm_config()
    except Exception:  # noqa: BLE001
        llm = {}
    return {
        "version": FAULTLINE_VERSION,
        "backend_port": 8000,
        "mcp_port": 8002,
        "backend_type": llm.get("backend_type", "unknown"),
        "model": llm.get("model", "(unset)"),
        "embedding_model": os.environ.get("EMBEDDING_MODEL", "(default)"),
        "api_url": os.environ.get("FAULTLINE_API_URL", ""),
        "seat_cap": FOSS_MAX_SEATS,
        "mcp_url": _resolve_mcp_url(request),
    }


@router.get("/seats")
def dashboard_seats_list(_op: str = Depends(require_operator)) -> dict:
    """Authoritative active-seat roster from the DB.

    Returns ACTIVE seats only: the webui derives the usage meter from
    ``seats.length``, and the server cap counts active seats, so returning only
    active keeps the two in lock-step (revoking a seat visibly frees the slot).
    Revoked seats remain tombstoned in the table for the action log; they simply
    leave the roster."""
    seats: list[dict] = []
    try:
        with psycopg2.connect(_dsn()) as db, db.cursor() as cur:
            cur.execute(
                "SELECT user_id, label, created_at, active FROM public.dashboard_seats "
                "WHERE active = TRUE ORDER BY created_at ASC"
            )
            for uid, label, created_at, active in cur.fetchall():
                seats.append({
                    "user_id": str(uid),
                    "label": label or "",
                    "created_at": created_at.isoformat() if created_at else None,
                    "active": bool(active),
                })
    except Exception as exc:  # noqa: BLE001
        log.warning("dashboard.seats.list_failed", error=str(exc)[:160])
        raise HTTPException(status_code=503, detail="seat store unavailable")
    return {"seats": seats, "limit": FOSS_MAX_SEATS}


@router.post("/seats", status_code=status.HTTP_201_CREATED)
def dashboard_seat_mint(body: SeatMintRequest, _op: str = Depends(require_operator)) -> JSONResponse:
    """Atomic cap-checked seat mint. Token shown ONCE; stored hashed.

    Race-free: a Postgres transaction advisory lock serializes the count+insert,
    so two concurrent mints cannot both observe count<5 and both insert. The
    count is server-derived (never trusts any client-sent value). 409 at cap.
    """
    _check_rate("operator:mint")
    label = (body.label or "").strip()[:120]
    user_id = str(uuid.uuid4())
    token = secrets.token_urlsafe(32)
    token_hash = _sha256_hex(token)

    dsn = _dsn()
    # Single connection, single transaction → advisory lock held until commit.
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_SEAT_MINT_LOCK_KEY,))
            cur.execute("SELECT COUNT(*) FROM public.dashboard_seats WHERE active = TRUE")
            active_count = cur.fetchone()[0]
            if active_count >= FOSS_MAX_SEATS:
                conn.rollback()
                _log_action("mint_seat.blocked", detail={"active": active_count})
                return JSONResponse(
                    status_code=status.HTTP_409_CONFLICT,
                    content={
                        "detail": _CAP_REACHED_HINT,
                        "active": active_count,
                        "limit": FOSS_MAX_SEATS,
                    },
                )
            cur.execute(
                "INSERT INTO public.dashboard_seats (user_id, label, token_hash) VALUES (%s, %s, %s)",
                (user_id, label, token_hash),
            )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        log.error("dashboard.seat.mint_failed", error=str(exc)[:160])
        raise HTTPException(status_code=503, detail="seat mint failed")
    finally:
        conn.close()

    _log_action("mint_seat", target_user_id=user_id, detail={"label": label})
    # Plaintext token returned EXACTLY once; only the hash is persisted.
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"user_id": user_id, "token": token, "label": label,
                 "created_at": _now_iso()},
    )


@router.delete("/seats/{user_id}")
def dashboard_seat_revoke(user_id: str, _op: str = Depends(require_operator)) -> dict:
    """Tombstone a seat + invalidate its token. Idempotent (200 if already revoked)."""
    try:
        uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="malformed user_id")
    try:
        with psycopg2.connect(_dsn()) as db, db.cursor() as cur:
            cur.execute(
                "UPDATE public.dashboard_seats SET active = FALSE, revoked_at = NOW() "
                "WHERE user_id = %s",
                (user_id,),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("dashboard.seat.revoke_failed", error=str(exc)[:160])
        raise HTTPException(status_code=503, detail="seat revoke failed")
    _log_action("revoke_seat", target_user_id=user_id)
    return {"revoked": True, "user_id": user_id}


@router.get("/llm")
def dashboard_llm_get(_op: str = Depends(require_operator)) -> dict:
    """Current LLM Brain config (non-secret). api_key presence is a bool only."""
    from src.api.llm_client import get_llm_config
    try:
        llm = get_llm_config()
    except Exception as exc:  # noqa: BLE001
        log.warning("dashboard.llm.get_failed", error=str(exc)[:160])
        raise HTTPException(status_code=503, detail="llm config unavailable")
    backend = llm.get("backend_type", os.environ.get("LLM_BACKEND_TYPE", "openwebui"))
    base_url = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    return {
        "backend_type": backend,
        "base_url": base_url,
        "model": llm.get("model", os.environ.get("WGM_LLM_MODEL") or ""),
        "api_key_set": bool(llm.get("api_key_set") or os.environ.get("LLM_API_KEY", "").strip()),
    }


@router.put("/llm")
def dashboard_llm_put(body: LLMConfigUpdate, _op: str = Depends(require_operator)) -> dict:
    """Persist the LLM Brain config so it takes effect on the running stack.

    Writes the singleton override row AND applies it to the running process
    (os.environ + cached URL refresh) so the next LLM call uses it. Survives
    restart via the lifespan loader. restart_required:true is honest — the
    re-embedder subprocess and MCP container pick it up only after full restart.
    """
    _check_rate("operator:llm")
    backend_type = (body.backend_type or "").strip().lower()
    base_url = (body.base_url or "").strip()
    model = (body.model or "").strip() or None
    # api_key semantics: None = leave untouched; "" = clear; value = set.
    if body.api_key is None:
        existing = _load_llm_override_row() or {}
        api_key = existing.get("api_key") or os.environ.get("LLM_API_KEY", "")
    else:
        api_key = body.api_key

    try:
        with psycopg2.connect(_dsn()) as db, db.cursor() as cur:
            cur.execute(
                "UPDATE public.dashboard_llm_config "
                "SET backend_type = %s, base_url = %s, model = %s, api_key = %s, updated_at = NOW() "
                "WHERE id = 1",
                (backend_type, base_url, model, api_key or None),
            )
    except Exception as exc:  # noqa: BLE001
        log.error("dashboard.llm.persist_failed", error=str(exc)[:160])
        raise HTTPException(status_code=503, detail="llm config persist failed")

    _apply_llm_to_running_process(backend_type, base_url, model, api_key or None)
    _log_action("llm_change", detail={"backend_type": backend_type, "base_url": base_url, "model": model,
                                       "key_changed": body.api_key is not None})
    return {"ok": True, "restart_required": True}


@router.post("/llm/test")
def dashboard_llm_test(_op: str = Depends(require_operator)) -> dict:
    """Probe the configured LLM backend with a minimal request. Returns latency or error."""
    import httpx
    from src.api.llm_client import get_llm_chat_url, get_backend_type, get_health_check_url
    try:
        chat_url = get_llm_chat_url()
        backend = get_backend_type()
        probe_url = get_health_check_url(chat_url)
        api_key = (os.environ.get("LLM_API_KEY") or "").strip()
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        t0 = time.time()
        with httpx.Client(timeout=8.0) as client:
            if backend == "anthropic":
                headers["x-api-key"] = api_key
                headers.setdefault("anthropic-version", "2023-06-01")
            resp = client.get(probe_url, headers=headers)
        latency_ms = int((time.time() - t0) * 1000)
        # 200/401/404 all prove the backend is reachable; only network/5xx = down.
        if resp.status_code < 500:
            return {"ok": True, "latency_ms": latency_ms, "status": resp.status_code}
        return {"ok": False, "error": f"backend returned HTTP {resp.status_code}", "latency_ms": latency_ms}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"unreachable: {exc}"[:200]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"probe failed: {exc}"[:200]}


@router.get("/openwebui")
def dashboard_openwebui(request: Request, _op: str = Depends(require_operator)) -> dict:
    """MCP wiring URL, key-set flag, and the legacy filter script body."""
    api_key_set = bool(os.environ.get("MCP_API_KEY", "").strip()) or _has_active_mcp_key()
    return {
        "mcp_url": _resolve_mcp_url(request),
        "api_key_set": api_key_set,
        "filter_script": _read_filter_script(),
    }


@router.post("/openwebui/rotate-key")
def dashboard_openwebui_rotate_key(_op: str = Depends(require_operator)) -> JSONResponse:
    """Rotate the MCP/OWUI key: mint new, revoke all others, return new ONCE.

    The MCP server consults the dashboard_mcp_keys table on every auth, so the
    old key stops working immediately (no cache, single indexed lookup)."""
    _check_rate("operator:rotate")
    new_key = secrets.token_urlsafe(32)
    key_hash = _sha256_hex(new_key)
    try:
        with psycopg2.connect(_dsn()) as db, db.cursor() as cur:
            cur.execute("UPDATE public.dashboard_mcp_keys SET is_active = FALSE, revoked_at = NOW() WHERE is_active = TRUE")
            cur.execute(
                "INSERT INTO public.dashboard_mcp_keys (key_hash) VALUES (%s)",
                (key_hash,),
            )
    except Exception as exc:  # noqa: BLE001
        log.error("dashboard.mcp_key.rotate_failed", error=str(exc)[:160])
        raise HTTPException(status_code=503, detail="key rotation failed")
    _log_action("rotate_key")
    return JSONResponse(status_code=200, content={"api_key": new_key, "created_at": _now_iso()})


# ─── Operator action log (additive; surfaced for curl / future webui) ─────────
@router.get("/actions")
def dashboard_actions(_op: str = Depends(require_operator)) -> dict:
    """Last N operator actions (mint/revoke/rotate/llm-change)."""
    try:
        with psycopg2.connect(_dsn()) as db, db.cursor() as cur:
            cur.execute(
                "SELECT ts, action, target_user_id, detail "
                "FROM public.dashboard_action_log ORDER BY id DESC LIMIT 25"
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("dashboard.actions.list_failed", error=str(exc)[:160])
        raise HTTPException(status_code=503, detail="action log unavailable")
    import json as _json
    return {"actions": [
        {"ts": ts.isoformat() if ts else None, "action": action,
         "target_user_id": str(t) if t else None,
         "detail": detail if isinstance(detail, dict) else (_json.loads(detail) if isinstance(detail, str) else None)}
        for ts, action, t, detail in rows
    ]}


# ─── Small helpers ───────────────────────────────────────────────────────────

def _has_active_mcp_key() -> bool:
    try:
        with psycopg2.connect(_dsn()) as db, db.cursor() as cur:
            cur.execute("SELECT 1 FROM public.dashboard_mcp_keys WHERE is_active = TRUE LIMIT 1")
            return cur.fetchone() is not None
    except Exception:  # noqa: BLE001
        return False


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
