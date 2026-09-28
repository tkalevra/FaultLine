"""Read-only Postgres access seam — the ONE way a metadata/cache helper reads the DB.

WHY THIS EXISTS — the "idle in transaction" incident (2026-08-01, PRODUCTION)
-----------------------------------------------------------------------------
A connection from the ``faultline-api`` container sat ``idle in transaction`` for **10h08m**
holding ``AccessShareLock`` on ``rel_types`` and ``ontology_evaluations`` (+ their indexes),
with ``backend_xid`` NULL — i.e. a **pure READ that never ended its transaction**.

A read lock alone is harmless. The damage is queueing: ``DROP SCHEMA … CASCADE`` (tenant
deprovision) needs ``AccessExclusiveLock``, so it parks behind the stuck reader — and once an
exclusive waiter is queued, *every subsequent request queues behind it, including other
readers*. Observed on prod: deprovision stuck, ``pg_dump`` of a 225 MB DB producing a 0-byte
file for 10+ minutes (100% lock-wait, 0% work), the metrics collector blocked, every
``LOCK TABLE public.facts`` blocked. With ``faultline-api`` stopped the same dump finished in
seconds.

TWO DISTINCT DEFECTS, ONE PRINCIPLE
-----------------------------------
**(1) A read that owns a transaction.** psycopg2 opens an implicit transaction on the FIRST
statement and holds it until an explicit ``commit()`` / ``rollback()`` or until the connection
is closed. **Closing the CURSOR does not end the transaction.** Any helper that does
``cur = conn.cursor(); cur.execute(SELECT); cur.close()`` and then blocks — on an LLM call, on
a slow HTTP hop, on the next loop iteration — is holding ``AccessShareLock`` on every table it
touched for the whole of that block.

**(2) A read that owns a connection.** ``with psycopg2.connect(dsn) as conn:`` is NOT
``closing()``: psycopg2's connection context manager commits (or rolls back) the transaction
and **leaves the connection open**. The backend only goes away when the object is garbage
collected, which is *not* a guarantee — a traceback, a cache, or a reference cycle pins it
indefinitely. Measured on the local stack: 11 abandoned ``idle`` backends from one container,
the oldest **6 days** old.

THE SEAM
--------
``read_only_connection()`` makes both impossible by construction:

  • ``autocommit = True`` → each statement is its own transaction, so **a SELECT can never
    leave a transaction open**. There is no ``commit()`` a future edit can forget.
  • ``readonly = True``   → the session refuses to write. A write through the read seam is a
    LOUD ``ReadOnlySqlTransaction`` error, not a silent surprise.
  • ``close()`` in ``finally`` → the backend is released on every path, exception included.

``release_read_transaction()`` is the retrofit for the connections this seam cannot own: a
long-lived, caller-supplied connection (the re_embedder's per-tenant sweep connection) that
must not carry a read transaction across a blocking call. It ends a **pure-read** transaction
and FAILS LOUD on a transaction that has written — a write is never silently discarded.

CONSTRAINTS PRESERVED
---------------------
This seam does not touch tenant binding. The overlays address their tables with an explicit
``{schema}.table`` qualifier and never rely on ``search_path``; callers that DO need a binding
pass ``schema=`` and get a session-level ``SET search_path TO "<schema>"`` with **no public** —
identical to today, and unaffected by ``autocommit`` (``SET`` is session state, not
transaction state). The ContextVar contract (``set_current_schema`` / ``reset_current_schema``)
lives in the overlay modules and is untouched.
"""

from __future__ import annotations

import contextlib
import re
from typing import Iterator, Optional

import psycopg2
import psycopg2.extensions as _pg_ext

try:  # structlog is always present in the app; keep the import defensive for tooling.
    import structlog

    _log = structlog.get_logger()
except Exception:  # pragma: no cover
    import logging

    _log = logging.getLogger(__name__)


# A bare SQL identifier. Schema names reach this seam from `user_provisioning` /
# `derive_schema_name`, never from user text — but a read helper that interpolates a schema
# into SQL validates it anyway (fail LOUD, never a silently mis-scoped read).
_SAFE_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class UnsafeSchemaError(ValueError):
    """A schema identifier that is not a bare SQL identifier reached the read seam."""


def validate_schema(schema_name: str) -> str:
    s = (schema_name or "").strip()
    if not _SAFE_SCHEMA_RE.match(s):
        raise UnsafeSchemaError(
            f"refusing to bind a non-identifier schema name: {schema_name!r}"
        )
    return s


@contextlib.contextmanager
def read_only_connection(
    dsn: str,
    *,
    schema: Optional[str] = None,
    connect_timeout: Optional[int] = None,
) -> Iterator[psycopg2.extensions.connection]:
    """Yield a psycopg2 connection that CANNOT leak a transaction or a backend.

    The connection is opened ``autocommit=True`` + ``readonly=True`` and is **always** closed
    on exit — success, exception, or generator abandonment.

    ``schema``  — bind ``SET search_path TO "<schema>"`` (NO ``public``; per-tenant isolation).
                  Omit for callers that fully qualify their tables (the overlays do).
    ``connect_timeout`` — CONNECTION guard only (never an operation timeout). A momentarily
                  slow Postgres must not block a turn unboundedly on a cold metadata read;
                  on timeout psycopg2 raises and the caller's own fail-safe applies.

    Raises whatever psycopg2 raises — this seam NEVER swallows a DB error. Callers own their
    fail-safe (seed-only / bootstrap floor), and that decision must stay visible to them.
    """
    kwargs = {}
    if connect_timeout is not None:
        kwargs["connect_timeout"] = connect_timeout
    conn = psycopg2.connect(dsn, **kwargs)
    try:
        # autocommit BEFORE anything else: from here on no statement can open a lingering
        # transaction, so there is no window in which this connection holds AccessShareLock
        # while the caller is doing something slow.
        conn.set_session(readonly=True, autocommit=True)
        if schema is not None:
            s = validate_schema(schema)
            with conn.cursor() as cur:
                # Tenant schema ONLY — no public fallthrough (per-tenant isolation).
                cur.execute(f'SET search_path TO "{s}"')  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.lang.security.audit.sqli.psycopg-sqli.psycopg-sqli — schema from UUID-derived source with validation
        yield conn
    finally:
        # Unconditional release. This is the line that ends the 6-day stranded backend and
        # the 10-hour AccessShareLock alike: a closed connection cannot hold either.
        try:
            conn.close()
        except Exception:  # pragma: no cover - close() on an already-dead socket
            pass


def release_read_transaction(conn, *, context: str = "") -> bool:
    """End a PURE-READ transaction on a caller-owned connection. Returns True if it ended one.

    THE RETROFIT for connections this seam does not own — chiefly the re_embedder's per-tenant
    sweep connection, which runs ``SELECT`` … then makes LLM calls on the same connection while
    the read transaction is still open. Call this immediately before any blocking/network
    operation, and in the ``finally`` of any read helper handed a long-lived connection.

    Safety, and why it is not just ``conn.rollback()``:
      • Nothing in progress (``IDLE``) → no-op, False.
      • ``INTRANS`` **with no xid assigned** → the transaction has written NOTHING (exactly the
        production signature: ``backend_xid`` NULL). Rolling back cannot lose data. → rollback,
        True.
      • ``INTRANS`` **with an xid assigned** → the transaction HAS written. We do **NOT** roll
        back — silently discarding a write would be a worse bug than the lock. FAIL LOUD and
        return False; the caller has a commit/rollback it is failing to reach.
      • ``INERROR`` → the transaction is already aborted and holds locks until released.
        Rollback is the only legal move. → rollback, True.

    ``pg_current_xact_id_if_assigned()`` is PG13+; ``txid_current_if_assigned()`` is the
    PG10–12 spelling. Both are non-assigning by definition — probing does not itself start a
    write transaction.

    **TENANT BINDING IS SAFE — verified against PG16, not assumed.** ``ROLLBACK`` does NOT
    clear a ``SET search_path`` that was COMMITTED (the shape every per-tenant caller uses:
    ``SET search_path TO <schema>`` then ``commit()``). Measured: bind → commit → SELECT →
    ``ROLLBACK`` leaves ``search_path`` on the tenant schema. A ``SET`` issued *inside* the
    rolled-back transaction reverts to the previously COMMITTED value — i.e. back to the tenant
    schema, never to a bare ``public`` default. So this barrier can never produce the silent
    public-fallthrough that ``src/api/data_cursor.py`` warns about.
    (``re_embedder._rollback_and_reapply_search_path``'s comment claims rollback resets
    search_path; that is not true of a committed binding. Its re-apply is harmless, not needed.)
    """
    if conn is None or getattr(conn, "closed", 1):
        return False
    if getattr(conn, "autocommit", False):
        return False

    try:
        status = conn.get_transaction_status()
    except Exception as e:
        _log.warning("db_read.txn_status_unavailable", context=context, error=str(e)[:160])
        return False

    if status == _pg_ext.TRANSACTION_STATUS_INERROR:
        # Aborted transaction: still holds every lock it took. Rollback is the only exit.
        conn.rollback()
        return True

    if status != _pg_ext.TRANSACTION_STATUS_INTRANS:
        return False

    xid = None
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT pg_current_xact_id_if_assigned()")
            except psycopg2.errors.UndefinedFunction:
                conn.rollback()  # the failed probe aborted the txn; it wrote nothing
                return True
            xid = cur.fetchone()[0]
    except Exception as e:
        # Probe failed for any other reason — the transaction is now aborted (INERROR) and
        # holding locks. Roll back; a read transaction has nothing to lose.
        _log.warning("db_read.xid_probe_failed", context=context, error=str(e)[:160])
        try:
            conn.rollback()
            return True
        except Exception:  # pragma: no cover
            return False

    if xid is None:
        # Pure read. This is the leak we are killing.
        conn.rollback()
        return True

    # A WRITE is pending. Never discard it — fail loud instead.
    _log.critical(
        "db_read.write_txn_at_read_barrier",
        context=context,
        xid=str(xid),
        detail=(
            "release_read_transaction() reached a transaction that has WRITTEN (xid assigned). "
            "Refusing to roll back — a write must never be silently discarded. The caller is "
            "holding an uncommitted write across a blocking call; commit or roll back at the "
            "write's own seam."
        ),
    )
    return False


def end_read_txn_if_pure(conn, *, context: str = "") -> bool:
    """End a PURE-READ transaction, and stay QUIET when the transaction has written.

    The designed-shape sibling of :func:`release_read_transaction` for call sites that sit
    INSIDE a caller whose transaction legitimately holds writes — per-edge probes inside the
    single-transaction ``/ingest`` loop (``classify_fact_3d``'s l4 alias probe, the retype
    subject-type probe, the subject-type self-heal lookup). At those sites a write transaction
    is the DESIGNED state, not a misuse, so the barrier's CRIT is noise — measured 15 CRITs on
    one cold first ingest (round-24 harness, 2026-09-16) — while the pure-read half (kill the
    idle-in-transaction before a thread hop) is still wanted.

    Semantics:
      • Nothing in progress / autocommit / closed → no-op, False (same as the barrier).
      • INTRANS, no xid → the transaction wrote NOTHING: rollback, True (the barrier's own
        pure-read branch — nothing can be lost).
      • INTRANS, xid assigned → the caller's write transaction: DO NOTHING, return False.
        No CRIT — this helper was called BECAUSE the caller knows the txn may have written.
        The write-txn-held-across-hops shape on /ingest is the caller's own transaction
        design (single-transaction ingest), owned and committed at the ingest's seam.

    The barrier itself (:func:`release_read_transaction`) keeps its fail-loud contract for
    every caller that does NOT already know — that is the misuse detector; this helper only
    removes the sites that mis-called it on a designed write transaction.
    """
    if conn is None or getattr(conn, "closed", 1):
        return False
    if getattr(conn, "autocommit", False):
        return False
    try:
        status = conn.get_transaction_status()
    except Exception as e:
        _log.warning("db_read.txn_status_unavailable", context=context, error=str(e)[:160])
        return False
    if status == _pg_ext.TRANSACTION_STATUS_INERROR:
        # Aborted transaction: still holds every lock it took. Rollback is the only exit.
        conn.rollback()
        return True
    if status != _pg_ext.TRANSACTION_STATUS_INTRANS:
        return False
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT pg_current_xact_id_if_assigned()")
            except psycopg2.errors.UndefinedFunction:
                conn.rollback()  # the failed probe aborted the txn; it wrote nothing
                return True
            xid = cur.fetchone()[0]
    except Exception as e:
        _log.warning("db_read.xid_probe_failed", context=context, error=str(e)[:160])
        try:
            conn.rollback()
            return True
        except Exception:  # pragma: no cover
            return False
    if xid is None:
        conn.rollback()
        return True
    # The caller's own write transaction — designed state on this path. Quiet no-op.
    _log.debug("db_read.write_txn_held_by_design", context=context, xid=str(xid))
    return False
