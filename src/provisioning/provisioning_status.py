"""Check user provisioning status.

Used by inlet filter to determine if user is ready to query/ingest.
"""

import structlog
import psycopg2
import psycopg2.extensions
from typing import Optional, Dict, Any
from .schema_manager import get_postgres_connection

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
        >>> assert result['schema_name'] == 'faultline_christopher'
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
        ...     user_slug="christopher",
        ...     user_name="Christopher"
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
