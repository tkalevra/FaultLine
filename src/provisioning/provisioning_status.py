"""Check user provisioning status.

Used by inlet filter to determine if user is ready to query/ingest.

Also the ONE harness-side wait on that status (``wait_until_ready``): the in-container
self-benchmark (``saas/bench_selftest.py``) and the laptop-side claims harness
(``benchmarks/harness/run_claims.py``) both wait for a freshly minted seat through this loop —
each supplies only its own PROBE (the in-process reader here, or ``GET /provisioning/status``
over HTTP), never a second copy of the poll.
"""

import time
import structlog
import psycopg2
import psycopg2.extensions
from typing import Any, Callable, Dict, Optional, Type
from .schema_manager import get_postgres_connection
from src.api import errors as _errors  # THE ONE ERROR SEAM — this helper's dict IS a response body

log = structlog.get_logger()


def check_provisioning_status(user_id: str, db: Optional[psycopg2.extensions.connection] = None) -> Dict[str, Any]:
    """Check if user schema is provisioned and ready.

    Args:
        user_id: UUID of user
        db: Optional connection. Creates new if not provided.

    Returns:
        Dict with keys:
            status: 'ready' | 'provisioning' | 'error' | 'not_found'
            schema_name: str (if provisioned)
            error_message: str (if status='error')
            ready_at: str (if status='ready')

    Examples:
        >>> result = check_provisioning_status("550e8400-e29b-41d4-a716-446655440000")
        >>> assert result['status'] == 'ready'
        >>> assert result['schema_name'] == 'faultline_daniel'
    """
    close_conn = False

    try:
        if not db:
            db = get_postgres_connection()
            close_conn = True

        with db.cursor() as cur:
            cur.execute("""
                SELECT status, schema_name, error_message, ready_at
                FROM public.user_provisioning
                WHERE user_id = %s
            """, (user_id,))

            row = cur.fetchone()

            if not row:
                return {
                    "status": "not_found",
                    "user_id": user_id,
                }

            status, schema_name, error_message, ready_at = row

            result = {
                "status": status,
                "user_id": user_id,
                "schema_name": schema_name,
            }

            if status == "error" and error_message:
                result["error_message"] = error_message

            if status == "ready" and ready_at:
                result["ready_at"] = ready_at.isoformat()

            if status not in ("ready", "provisioning"):
                # first-touch-cold-path round 8: a ROW in a terminal state (`error` after a failed
                # mint, `failed`, or anything else) is a FACT about this seat that the backend
                # will not retry on its own (the queue drains `provisioning` only). Name it —
                # the row's error_message plus a correlation id tied to THIS CRIT line — so the
                # transport can answer a terminal envelope instead of polling into a fabricated
                # "being set up" sentence. The seat row is left untouched.
                import uuid as _uuid
                _cid = _uuid.uuid4().hex[:12]
                result["error_message"] = error_message or f"provisioning row in state {status!r}"
                result["correlation_id"] = _cid
                log.critical("provisioning_row_terminal", user_id=user_id[:8], status=status,
                             schema=schema_name, error_message=(error_message or "")[:200],
                             correlation_id=_cid,
                             note="terminal provisioning state; not auto-retried by the queue")

            return result

    except Exception as e:
        log.error(f"provisioning_status_check_failed", user_id=user_id, error=str(e))
        # This dict is RETURNED VERBATIM by ``GET /provisioning/status`` (src/api/main.py) — a
        # helper's except-dict is a response body. Authored phrase + correlation id; the driver
        # sentence is on the seam's CRIT line under that id (src/api/errors.py, ASVS V7.4.1).
        _pe = _errors.public_error(e, where="provisioning.check_status", what="Failed to check status")
        return {
            "status": "error",
            "user_id": user_id,
            "error_message": _pe["error"],
            "correlation_id": _pe["correlation_id"],
        }

    finally:
        if close_conn and db:
            db.close()


def ensure_user_provisioned(user_id: str, user_slug: str = None, db: Optional[psycopg2.extensions.connection] = None, user_name: str = None) -> bool:
    """Ensure user is provisioned, creating record if necessary.

    Called when new user logs in to OpenWebUI. If user doesn't have a provisioning
    record, create one and return False (not yet ready). If ready, return True.

    Args:
        user_id: UUID of user
        user_slug: Optional slug. If provided and user not found, create provisioning record.
        db: Optional connection. Creates new if not provided.
        user_name: Optional human-readable name from OpenWebUI for display_name.

    Returns:
        True if user is ready, False if provisioning in progress

    Examples:
        >>> is_ready = ensure_user_provisioned(
        ...     user_id="550e8400-e29b-41d4-a716-446655440000",
        ...     user_slug="daniel",
        ...     user_name="Daniel"
        ... )
    """
    close_conn = False

    try:
        if not db:
            db = get_postgres_connection()
            close_conn = True

        # Check current status
        status_result = check_provisioning_status(user_id, db)

        if status_result["status"] == "ready":
            return True

        if status_result["status"] in ["provisioning", "error"]:
            return False

        # User not found — create provisioning record if slug provided
        if status_result["status"] == "not_found" and user_slug:
            from .schema_manager import derive_schema_name

            schema_name = derive_schema_name(user_slug)
            # Use provided user_name for display, or fall back to slug
            display_name = user_name if user_name else user_slug

            with db.cursor() as cur:
                # Ensure user exists in public.users table first
                # Use ON CONFLICT DO NOTHING for idempotency
                cur.execute("""
                    INSERT INTO public.users (user_id, email, display_name, slug)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (user_id) DO NOTHING
                """, (user_id, f"{user_id}@local", display_name, user_slug))
                db.commit()

                log.info(f"created_user_record", user_id=user_id, slug=user_slug, display_name=display_name)

                # Now create provisioning record
                cur.execute("""
                    INSERT INTO public.user_provisioning (user_id, schema_name, status)
                    VALUES (%s, %s, 'provisioning')
                    ON CONFLICT (user_id) DO NOTHING
                """, (user_id, schema_name))
                db.commit()

                log.info(f"created_provisioning_record", user_id=user_id, schema=schema_name)

            return False

        # User not found, no slug — can't provision
        log.error(f"user_not_found_no_slug", user_id=user_id)
        return False

    except Exception as e:
        log.error(f"ensure_provisioned_failed", user_id=user_id, error=str(e))
        return False

    finally:
        if close_conn and db:
            db.close()


# ══════════════════════════════════════════════════════════════════════════════════════════
# The ONE harness-side wait on provisioning status
# ══════════════════════════════════════════════════════════════════════════════════════════

class ProvisioningNotReady(RuntimeError):
    """The seat never reached provisioning ``status='ready'`` inside the caller's budget, or its
    provisioning row landed in a TERMINAL state the queue will not retry.

    ``public_reason`` is an authored sentence safe to render on a status door (the backend's own
    ``error_message`` is already the public shape from ``src/api/errors.py``); it never carries a
    DSN, a URL with credentials, or a stack. ``last_status`` is the raw ``status`` string of the
    last answer; ``correlation_id`` the backend's, when it gave one."""

    def __init__(self, reason: str, *, last_status: Optional[str] = None,
                 correlation_id: Optional[str] = None):
        super().__init__(reason)
        self.public_reason = reason
        self.last_status = last_status
        self.correlation_id = correlation_id


def wait_until_ready(probe: Callable[[], Any], *, seat_uuid: str, timeout: float, poll_s: float,
                     log: Optional[Callable[[str], None]] = None,
                     not_ready: Type[ProvisioningNotReady] = ProvisioningNotReady,
                     clock: Callable[[], float] = time.monotonic,
                     sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """Poll ``probe()`` until the seat's provisioning STATUS is ``ready``; return that answer.

    ``probe`` returns the dict shape ``check_provisioning_status`` produces — read in-process, or
    fetched from ``GET /provisioning/status`` (src/api/main.py), which returns that dict verbatim.
    Which door a harness has decides its probe; the LOOP and the READING of the answer are here,
    once, and the reading is the MCP's own ``_classify_probe_body`` (src/mcp/server.py) — the
    classifier ``_ensure_provisioned`` keys every real turn on — so a harness and the transport
    can never disagree about what "ready" means:

      ready       → return the body (the whole job — tables, ledger stamp, cortex — is behind this
                    flag: ``schema_manager.create_user_schema`` commits ``status='ready'`` LAST);
      failed      → raise ``not_ready`` NOW: a terminal row (``error`` WITH a seat row, ``failed``,
                    unknown) is never retried by the queue, so waiting on it is a lie;
      wait / not_found / unreachable
                  → keep polling inside ``timeout``; the probe's own failure (an exception from
                    ``probe``, or ``status: error`` WITHOUT a seat row — the DB refusing, the HTTP
                    door down) is transient, not a verdict on the seat.

    Budget exhausted → raise ``not_ready`` naming the last answer. No settle sleep after ready:
    ready means ready. ``poll_s`` is a cadence, ``timeout`` the only bound (the loop stops when the
    NEXT sleep would cross it, so it never overshoots by a whole poll).

    WHY "schema exists" is the wrong signal (the wound both harnesses carried — pre-prod, 2026-09-15,
    gauntlet lanes bench-sandbox-ready-wait / claims-harness-ready-wait): ``create_user_schema``
    commits ``CREATE SCHEMA`` first and applies the table template afterwards in a psql subprocess
    that takes tens of seconds on the pre-prod ZFS pool. PostgreSQL makes a transaction's effects
    visible to other sessions as a unit at COMMIT ("the committed actions become visible as a
    unit to other sessions" — PostgreSQL docs, Tutorial §3.4 Transactions), so a catalog poll sees
    the schema seconds before its tables exist; a TRUNCATE on a relation that does not exist yet
    is SQLSTATE ``42P01 undefined_table`` (PostgreSQL docs, Appendix A), which psycopg2 raises as
    ``psycopg2.errors.UndefinedTable`` (psycopg2 docs, "psycopg2.errors"). A harness that returned
    on the schema then reset a seat with no tables (3/3 UndefinedTable, claim 1 graded ERROR) —
    or, worse, gave up on a timeout and CONTINUED into a dirty-seat baseline.

    ``not_ready`` lets a caller brand the raise with its own subclass (the bench renders
    ``SandboxNotReady`` on its status door) without a second loop; it must subclass
    ``ProvisioningNotReady``. ``clock``/``sleep`` are injectable for tests only.
    """
    from src.mcp.server import _classify_probe_body  # the transport's reading — reused, not copied

    if not issubclass(not_ready, ProvisioningNotReady):
        raise TypeError("not_ready must subclass ProvisioningNotReady")
    _say = log or (lambda msg: None)
    tag = (seat_uuid or "")[:8]
    t_start = clock()
    deadline = t_start + float(timeout)
    poll_s = max(0.0, float(poll_s))
    attempt = 0
    last_kind, last_detail, last_raw, last_corr = "unprobed", "", None, None
    while True:
        attempt += 1
        try:
            body = probe()
        except Exception as exc:  # noqa: BLE001 — a probe failure is UNREACHABLE (keep polling), never a verdict
            body = {"status": "error", "error_message": type(exc).__name__}
        kind, detail, corr = _classify_probe_body(body)
        raw = body.get("status") if isinstance(body, dict) else None
        if kind == "ready":
            _say(f"seat {tag} ready (attempt {attempt}, waited {clock() - t_start:.1f}s) "
                 f"schema={body.get('schema_name') if isinstance(body, dict) else None}")
            return body
        if kind == "failed":
            reason = (f"seat {tag} provisioning is in a TERMINAL state {raw!r}: {detail}"
                      + (f" (correlation_id={corr})" if corr else "")
                      + " — the backend does not retry this; run ERRORED, no claim scored")
            _say(reason)
            raise not_ready(reason, last_status=raw, correlation_id=corr)
        last_kind, last_detail, last_raw, last_corr = kind, detail, raw, corr
        if clock() + poll_s >= deadline:
            break
        sleep(poll_s)

    reason = (f"seat {tag} not ready after {float(timeout):.0f}s "
              f"({attempt} status probes; last answer {last_raw!r} → {last_kind}"
              + (f": {last_detail}" if last_detail else "")
              + (f", correlation_id={last_corr}" if last_corr else "")
              + ") — run ERRORED, no claim scored on an unprovisioned seat")
    _say(reason)
    raise not_ready(reason, last_status=last_raw, correlation_id=last_corr)
