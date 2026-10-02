"""Check user provisioning status.

Used by inlet filter to determine if user is ready to query/ingest.
"""

import structlog
import psycopg2
import psycopg2.extensions
from typing import Optional, Dict, Any
from .schema_manager import get_postgres_connection

log = structlog.get_logger()


# ─── FOSS seat cap at the point a tenant is BORN ─────────────────────────────
# The dashboard caps MINTED seats at FOSS_MAX_SEATS, but a tenant schema is created
# here, and every tenant-schema endpoint (and the MCP shared key / anonymous dev
# posture, and a direct caller on :8000) reaches this with whatever user_id the
# client sent. Without a gate here the cap only limited seat TOKENS, not tenants.
# The cap constant and the advisory-lock key are read from src/api/dashboard.py so
# there is exactly one source constant and mint + admission serialize on one lock.

SEAT_LIMIT_MESSAGE = "seat limit — mint a seat in the dashboard"
SEAT_REQUIRED_MESSAGE = "seat required — this user_id holds no active seat; mint a seat for it in the dashboard (re-minting restores access)"


class SeatLimitError(Exception):
    """A tenant was refused by the FOSS seat cap (maps to HTTP 403).

    ``kind`` is ``seat_required`` (seats are in use and this user_id holds none) or
    ``seat_limit`` (open posture, FOSS_MAX_SEATS tenants already exist)."""

    status_code = 403

    def __init__(self, detail: str, kind: str = "seat_limit") -> None:
        base = SEAT_REQUIRED_MESSAGE if kind == "seat_required" else SEAT_LIMIT_MESSAGE
        super().__init__(f"{base} ({detail})")
        self.detail = detail
        self.kind = kind
        self.message = str(self)


def _seat_cap() -> tuple:
    from src.api.dashboard import FOSS_MAX_SEATS, _SEAT_MINT_LOCK_KEY
    return FOSS_MAX_SEATS, _SEAT_MINT_LOCK_KEY


def seat_refusal(cur, user_id: str) -> Optional[SeatLimitError]:
    """Return None if ``user_id`` may act / hold a tenant, else the refusal (not raised).

    Read-only. SEAT POSTURE = a seat was EVER minted (revoked rows count, #120: revoking the
    last seat must not re-open the open posture). In seat posture ONLY an active seat's
    user_id is admitted —
    for a new tenant AND for an existing one (a revoked seat, a tenant from before the first
    seat): nothing is deleted, re-minting a seat for that user_id restores access.
    OPEN POSTURE (no seat ever minted): an existing tenant is admitted; a new one only while
    fewer than FOSS_MAX_SEATS tenants exist.
    """
    cap, _ = _seat_cap()
    uid = str(user_id or "").strip().lower()
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM public.dashboard_seats), "
        "EXISTS (SELECT 1 FROM public.dashboard_seats WHERE active AND user_id::text = %s)",
        (uid,),
    )
    seats_in_use, is_seat = cur.fetchone()
    if seats_in_use:
        return None if is_seat else SeatLimitError("seats have been minted on this instance", kind="seat_required")
    cur.execute("SELECT 1 FROM public.user_provisioning WHERE user_id::text = %s", (uid,))
    if cur.fetchone():
        return None
    cur.execute("SELECT COUNT(*) FROM public.user_provisioning")
    n = cur.fetchone()[0]
    if n >= cap:
        return SeatLimitError(f"{n} of {cap} tenants already exist and no seat is minted")
    return None


def seat_admission_refusal(cur, user_id: str) -> Optional[str]:
    """String form of :func:`seat_refusal` (the full refusal message, or None)."""
    err = seat_refusal(cur, user_id)
    return err.message if err else None


def admit_new_tenant(cur, user_id: str) -> None:
    """Atomic seat-cap admission for a NEW tenant; raises SeatLimitError on refusal.

    Takes the same transaction-scoped advisory lock as the dashboard seat mint, so
    the count + the caller's INSERT (same transaction) cannot race a concurrent
    admission or mint. The caller must INSERT and COMMIT in this transaction.
    """
    _, lock_key = _seat_cap()
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
    err = seat_refusal(cur, user_id)
    if err:
        log.warning("seat_cap.refused", user_id=str(user_id)[:8], kind=err.kind, reason=err.detail)
        raise err


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
        >>> assert result['schema_name'] == 'faultline_alexander'
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

            return result

    except Exception as e:
        log.error(f"provisioning_status_check_failed", user_id=user_id, error=str(e))
        return {
            "status": "error",
            "user_id": user_id,
            "error_message": f"Failed to check status: {str(e)}",
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
        ...     user_slug="alexander",
        ...     user_name="Alexander"
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

            # One transaction: seat-cap admission (advisory lock) + both INSERTs, so the
            # cap count and the new tenant row commit atomically.
            db.commit()  # close the status-probe read txn; the lock txn starts clean
            try:
                with db.cursor() as cur:
                    admit_new_tenant(cur, user_id)
                    # Ensure user exists in public.users table first
                    # Use ON CONFLICT DO NOTHING for idempotency
                    cur.execute("""
                        INSERT INTO public.users (user_id, email, display_name, slug)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (user_id) DO NOTHING
                    """, (user_id, f"{user_id}@local", display_name, user_slug))
                    # Now create provisioning record
                    cur.execute("""
                        INSERT INTO public.user_provisioning (user_id, schema_name, status)
                        VALUES (%s, %s, 'provisioning')
                        ON CONFLICT (user_id) DO NOTHING
                    """, (user_id, schema_name))
                db.commit()
            except Exception:
                db.rollback()
                raise

            log.info(f"created_user_record", user_id=user_id, slug=user_slug, display_name=display_name)
            log.info(f"created_provisioning_record", user_id=user_id, schema=schema_name)

            return False

        # User not found, no slug — can't provision
        log.error(f"user_not_found_no_slug", user_id=user_id)
        return False

    except SeatLimitError:
        raise  # a refusal is a decision, not a hiccup: the caller maps it to 403
    except Exception as e:
        log.error(f"ensure_provisioned_failed", user_id=user_id, error=str(e))
        return False

    finally:
        if close_conn and db:
            db.close()
