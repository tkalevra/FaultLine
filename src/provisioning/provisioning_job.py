"""Background job for user schema provisioning.

Runs asynchronously to create user schemas without blocking API requests.
"""

import asyncio
import structlog
import os
import psycopg2
import psycopg2.extensions
from typing import List, Dict, Any, Optional
from .schema_manager import create_user_schema, get_postgres_connection

log = structlog.get_logger()


async def provision_user_schema_background(user_id: str, user_slug: str, dsn: str = None) -> None:
    """Background task to provision a user schema.

    Runs asynchronously without blocking the API. Can be queued by FastAPI dependency
    or triggered from the inlet filter.

    Args:
        user_id: UUID of user
        user_slug: Human-readable slug
        dsn: Optional PostgreSQL connection string

    Examples:
        >>> import asyncio
        >>> asyncio.run(provision_user_schema_background(
        ...     user_id="00000000-0000-0000-0000-000000000000",
        ...     user_slug="christopher"
        ... ))
    """
    try:
        # Run sync provisioning in executor to avoid blocking event loop
        loop = asyncio.get_event_loop()
        schema_name, status = await loop.run_in_executor(
            None,
            create_user_schema,
            user_id,
            user_slug,
            None,  # db parameter (will create new connection)
        )

        if status == "ready":
            log.info(f"user_schema_provisioned", user_id=user_id, schema=schema_name)
        else:
            log.error(f"user_schema_provisioning_failed", user_id=user_id, schema=schema_name, status=status)

    except Exception as e:
        log.error(f"background_provisioning_error", user_id=user_id, user_slug=user_slug, error=str(e))


def get_provisioning_queue_length(db: Optional[psycopg2.extensions.connection] = None) -> int:
    """Get count of schemas awaiting provisioning.

    Args:
        db: Optional connection. Creates new if not provided.

    Returns:
        Number of users with status='provisioning'
    """
    close_conn = False

    try:
        if not db:
            db = get_postgres_connection()
            close_conn = True

        with db.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM public.user_provisioning
                WHERE status = 'provisioning'
            """)
            count = cur.fetchone()[0]
            return count

    except Exception as e:
        log.error(f"failed_to_get_queue_length", error=str(e))
        return -1

    finally:
        if close_conn and db:
            db.close()


def reap_stale_provisioning_jobs(stale_minutes: int = 5) -> Dict[str, Any]:
    """Find provisioning jobs with stale heartbeats and mark as error for retry.

    Called periodically (e.g., every 60 seconds) to detect crashed workers.
    Worker crashes leave status='provisioning' with no heartbeat updates.
    Reaper marks these as status='error' so they can be retried.

    Args:
        stale_minutes: How many minutes without heartbeat = stale (default 5)

    Returns:
        Dict with reaped_count and user_ids affected

    Examples:
        >>> result = reap_stale_provisioning_jobs(stale_minutes=5)
        >>> print(f"Reaped: {result['reaped_count']} jobs")
    """
    db = None
    results = {"reaped_count": 0, "user_ids": []}

    try:
        db = get_postgres_connection()

        with db.cursor() as cur:
            # Find jobs with no heartbeat for > stale_minutes
            # Criteria: status='provisioning' AND (NOW() - heartbeat_at) > interval
            # Also catch jobs where heartbeat_at is NULL (never updated)
            cur.execute("""
                SELECT user_id, schema_name
                FROM public.user_provisioning
                WHERE status = 'provisioning'
                  AND created_at < NOW() - INTERVAL '%s minutes'
                  AND (
                    heartbeat_at IS NULL
                    OR (NOW() - heartbeat_at) > INTERVAL '%s minutes'
                  )
                ORDER BY created_at ASC
            """, (stale_minutes, stale_minutes))

            stale_jobs = cur.fetchall()

            if not stale_jobs:
                log.debug("provisioning_reaper_no_stale_jobs")
                return results

            log.warning(
                "provisioning_reaper_found_stale",
                count=len(stale_jobs),
                stale_minutes=stale_minutes
            )

            # Mark each stale job as error
            for user_id, schema_name in stale_jobs:
                try:
                    error_msg = (
                        f"Worker heartbeat stale (> {stale_minutes} min). "
                        "Worker likely crashed during provisioning. "
                        "Mark for retry."
                    )

                    cur.execute("""
                        UPDATE public.user_provisioning
                        SET status = 'error', error_message = %s
                        WHERE user_id = %s AND status = 'provisioning'
                    """, (error_msg, user_id))
                    db.commit()

                    results["reaped_count"] += 1
                    results["user_ids"].append(user_id[:8])

                    log.error(
                        "provisioning_reaper_marked_error",
                        user_id=user_id[:8],
                        schema=schema_name,
                        minutes_stale=stale_minutes
                    )

                except Exception as e:
                    log.error(
                        "provisioning_reaper_mark_failed",
                        user_id=user_id[:8],
                        schema=schema_name,
                        error=str(e)
                    )

        return results

    except Exception as e:
        log.error("provisioning_reaper_error", error=str(e))
        return results

    finally:
        if db:
            db.close()


def process_provisioning_queue(batch_size: int = 10) -> Dict[str, Any]:
    """Process pending user schema provisioning requests.

    Can be called periodically (e.g., every 5 seconds) to provision schemas
    for new users.

    Args:
        batch_size: Max number of schemas to provision in one batch

    Returns:
        Dict with provisioned/failed/skipped counts

    Examples:
        >>> result = process_provisioning_queue(batch_size=5)
        >>> print(f"Provisioned: {result['provisioned']}")
    """
    db = None
    results = {"provisioned": 0, "failed": 0, "skipped": 0}

    try:
        db = get_postgres_connection()

        with db.cursor() as cur:
            # Get pending users (all columns fully qualified to avoid ambiguity)
            # Use provisioning schema_name directly (don't rely on users.slug)
            cur.execute("""
                SELECT user_provisioning.user_id, user_provisioning.schema_name
                FROM public.user_provisioning
                WHERE user_provisioning.status = %s
                ORDER BY user_provisioning.created_at ASC
                LIMIT %s
            """, ('provisioning', batch_size))

            pending = cur.fetchall()

            if not pending:
                return results

            log.info(f"provisioning_queue_found_pending", count=len(pending))

            for user_id, schema_name in pending:
                try:
                    # Extract slug from schema_name (remove "faultline_" prefix)
                    slug = schema_name.replace("faultline_", "")

                    log.info(f"provisioning_user_schema_start",
                            user_id=user_id[:8],
                            schema=schema_name)

                    # Create new connection for each schema to avoid transaction contamination
                    user_db = get_postgres_connection()
                    try:
                        actual_schema_name, status = create_user_schema(user_id, slug, user_db)
                        if status == "ready":
                            results["provisioned"] += 1
                            log.info(f"provisioned_user_schema",
                                   user_id=user_id[:8],
                                   schema=actual_schema_name)
                        else:
                            results["failed"] += 1
                            log.error(f"provisioning_returned_error",
                                    user_id=user_id[:8],
                                    schema=schema_name,
                                    status=status)
                    finally:
                        user_db.close()

                except Exception as e:
                    results["failed"] += 1
                    log.error(f"provisioning_exception",
                            user_id=user_id[:8],
                            schema=schema_name,
                            error=str(e))

        return results

    except Exception as e:
        log.error(f"provisioning_queue_error", error=str(e))
        return results

    finally:
        if db:
            db.close()