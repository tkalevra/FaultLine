"""Open-core data-plane cursor seam — the incremental on-ramp from session ``SET
search_path`` to transaction-scoped ``SET LOCAL`` (per-account grain Phase **1b.0**).

> Ratified plan: ``DEV/DESIGN-per-account-grain-1b-execution.md`` §2.2 / §8 (1b.0). This is
> the **open-core** seam every swept data-path site will eventually call instead of
> hand-rolling ``cur.execute("SET search_path TO {schema}")``. It lives in ``src/`` (NOT
> ``saas/``) because the 104 legacy bind sites are open-core and MUST work with
> ``SAAS_MODE`` off. The SaaS structural guard (``saas/subject_cursor.py``) is the
> DESTINATION; this seam is the BRIDGE — it lets the legacy sites flip onto the one
> ``SET LOCAL`` mechanism WITHOUT being rewritten onto the builder API in a single heroic step.

WHY THIS EXISTS — the PgBouncer transaction-pooling leak
--------------------------------------------------------
Every tenant is bound by a **session-level** ``SET search_path TO faultline_<tenant>`` (104
``execute`` sites; zero use ``SET LOCAL``). Under PgBouncer ``pool_mode = transaction`` the
server connection is handed to the NEXT client when a transaction ends, and a **session-level
``SET`` survives that handoff** — tenant B inherits tenant A's ``search_path`` and reads A's
schema. That is a cross-tenant read in the authoritative store.

WHY THE OBVIOUS FIX IS WORSE — the silent public fallthrough
------------------------------------------------------------
A mechanical ``SET`` → ``SET LOCAL`` sweep is *actively dangerous*: ``SET LOCAL`` dies at
**COMMIT** (not just rollback), and this codebase commits mid-request then keeps querying on
the SAME connection (see ``query`` / ``correct_fact`` / ``fetch_facts_from_anchor`` and the
re_embedder poll loop). After such a commit the binding evaporates and unqualified SQL falls
through to the DEFAULT ``search_path`` → ``public`` — which **is a real, seeded template
schema** (``public.facts`` / ``public.entity_aliases`` exist). That is a *silent wrong-schema
read*: plausible data, no error, no log. Strictly worse than the leak. ``src/wgm/gate.py``
already documents this exact failure (``_reapply_search_path`` / ``SearchPathError``).

THE FIX SHAPE (this module = 1b.0, the seam ONLY)
-------------------------------------------------
- **SAAS off** → the else-branch is *literally today's two lines* (``cur.execute("SET
  search_path TO {schema}")``, non-LOCAL). Byte-for-byte unchanged; ``saas/`` is never
  imported (the pure-env gate short-circuits). This is the open-core guarantee.
- **SAAS on + bound** → route to the structural factory ``saas.subject_cursor.subject_cursor``
  (``SET LOCAL`` inside a txn → nothing survives to the pool → PgBouncer footgun killed).
- **SAAS on + UNBOUND at a data site** → **fail LOUD** (``UnboundSubjectError``). NEVER a
  silent fall-through to an unscoped/``public`` cursor. This is the whole point of the
  structural guard: an unbound data access is a hard error, not "return everything".

**Wired into ZERO sites in 1b.0** (mirrors how ``subject_cursor`` shipped in 1a). The
104-site sweep is 1b.3–1b.5, module-by-module behind the poison-lint ratchet — see the
execution plan §3/§8 and the REPORT. Fail-LOUD throughout; never a silent wrong-schema read.
"""

from __future__ import annotations

import contextlib
import os
import re
from typing import Iterator, Optional

try:  # pragma: no cover - import guard (mirrors saas/db.py)
    import psycopg2
except Exception:  # pragma: no cover
    psycopg2 = None  # type: ignore

try:
    from src.api.logging_config import log_crit
except Exception:  # pragma: no cover - logging is best-effort, never a hard dep
    import logging as _logging

    def log_crit(logger, msg, **kw):  # type: ignore
        try:
            logger.critical("%s %s", msg, kw)
        except Exception:
            pass


import structlog

log = structlog.get_logger(__name__)


# ── schema-name shape guard (fail-loud BEFORE any interpolation) ──────────────────────
# Schema names (``faultline_{slug}`` / ``faultline_acct_{slug}``) are interpolated into
# ``SET search_path TO {schema}`` via f-string (identifiers cannot be parameterized), so the
# shape MUST be validated first. Identical contract to ``src/wgm/gate.py``'s ``_SCHEMA_NAME_RE``
# and ``saas/subject_cursor.py``'s ``_SAFE_SCHEMA_RE`` — a bad schema can never widen scope.
_SCHEMA_NAME_RE = re.compile(r"^[a-z0-9_]+$")


class SearchPathError(RuntimeError):
    """The tenant ``search_path`` is not bound / could not be verified on this connection.

    Mirrors ``src/wgm/gate.py::SearchPathError`` (kept independent here to avoid an import
    cycle — ``gate`` pulls in the fact store + LLM stack). A ``SET LOCAL search_path`` reverts
    at COMMIT/rollback, so an unverified binding leaves the connection pointing at ``public``
    (the seed template). Continuing would be a silent cross-tenant/seed read — this must
    propagate (fail the request) and MUST NEVER be swallowed. A failed request (500) is
    strictly better than a wrong-schema read.
    """


def _saas_mode_enabled() -> bool:
    """SAAS_MODE gate — a pure env read that imports NOTHING (mirrors
    ``src/api/main.py::_saas_mode_enabled`` / ``saas.config._truthy``). This is what keeps the
    SAAS-off path a zero-``saas``-import, byte-for-byte-today path.
    """
    return str(os.environ.get("SAAS_MODE") or "").strip().lower() not in (
        "",
        "false",
        "0",
        "no",
        "off",
    )


def _validate_schema(schema_name: str) -> str:
    schema = (schema_name or "").strip()
    if not schema or not _SCHEMA_NAME_RE.match(schema):
        # Fail-loud before interpolation: a malformed schema can never reach SET search_path.
        raise SearchPathError(
            f"refusing to bind unsafe schema_name {schema_name!r} "
            f"(must match ^[a-z0-9_]+$) — fail-closed"
        )
    return schema


# ── low-level emitter — the ONE place that decides session-SET vs SET LOCAL ───────────
def bind_search_path(
    cur,
    schema_name: str,
    *,
    local: Optional[bool] = None,
    include_public: bool = False,
) -> None:
    """Emit the tenant ``search_path`` bind on ``cur``.

    THE single seam the 104 legacy sites migrate onto. ``local`` selects the mechanism:

      • ``local=False`` (or ``None`` with the flag off) → ``SET search_path TO {schema}`` —
        **today's exact behavior**, byte-for-byte. This is the safe default.
      • ``local=True`` → ``SET LOCAL search_path TO {schema}`` — scoped to the current
        transaction; nothing survives to the PgBouncer pool. Dies at COMMIT, so the caller
        MUST re-bind after any mid-request commit (see ``commit_and_rebind``) or verify with
        ``verify_bound`` before trusting a subsequent unqualified read.

    When ``local`` is ``None`` the mechanism follows ``SEARCH_PATH_LOCAL`` (env, default OFF →
    session SET). The default is deliberately the safe, unchanged path: flipping the whole
    fleet to ``SET LOCAL`` is gated on the sweep completing (the mid-request-commit sites must
    be handled first) — see the execution plan §8.

    ``include_public`` appends ``, public`` (provisioning/seed paths that legitimately read the
    template — class S in the sweep taxonomy). Per-tenant DATA binds MUST NOT set it.
    """
    schema = _validate_schema(schema_name)
    if local is None:
        local = _search_path_local_default()
    kw = "SET LOCAL" if local else "SET"
    suffix = ", public" if include_public else ""
    cur.execute(f"{kw} search_path TO {schema}{suffix}")  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.formatted-sql-query.formatted-sql-query schema validated by _validate_schema (^{a-z0-9_}+$) before this line


def _search_path_local_default() -> bool:
    """Fleet default for ``bind_search_path(local=None)``. OFF (session SET) until the sweep
    lands — see execution plan §8.6 (Phase 2 PgBouncer is gated on the complete ``SET LOCAL``
    sweep, NOT on flipping this default early)."""
    return str(os.environ.get("SEARCH_PATH_LOCAL") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def verify_bound(cur, schema_name: str) -> None:
    """Fail-LOUD assertion that ``cur``'s connection is bound to ``schema_name``.

    Runs ``SHOW search_path`` and raises ``SearchPathError`` (log_crit + raise) if the tenant
    schema is not present — closing the silent public-fallthrough window. Mirrors the
    verification half of ``gate._reapply_search_path``. Use after a ``SET LOCAL`` bind whenever
    a subsequent unqualified read must be trusted (esp. after a mid-request commit).
    """
    schema = _validate_schema(schema_name)
    try:
        cur.execute("SHOW search_path")
        row = cur.fetchone()
        current_path = row[0] if row else ""
    except Exception as e:  # pragma: no cover - exercised via fakes
        msg = (
            f"data_cursor.search_path_verify_failed: could not read search_path while "
            f"expecting tenant schema '{schema}': {e}. Connection tenant binding is unknown "
            f"— refusing to continue (public-schema fallthrough / cross-tenant read risk)."
        )
        log_crit(log, msg, schema=schema, error=str(e), component="search_path")
        raise SearchPathError(msg) from e
    if schema not in (current_path or ""):
        msg = (
            f"data_cursor.search_path_verify_failed: SHOW search_path returned "
            f"{current_path!r}, expected it to contain '{schema}'. Connection tenant binding "
            f"is wrong — refusing to continue (public-schema fallthrough / cross-tenant read)."
        )
        log_crit(log, msg, schema=schema, search_path=current_path, component="search_path")
        raise SearchPathError(msg)


def commit_and_rebind(
    conn,
    cur,
    schema_name: str,
    *,
    local: Optional[bool] = None,
    include_public: bool = False,
) -> None:
    """Commit ``conn`` and, in ``SET LOCAL`` mode, immediately re-establish the binding.

    THE mid-request-commit landmine handler. ``SET LOCAL`` dies at COMMIT; several hot paths
    (``query``, ``correct_fact``, ``fetch_facts_from_anchor``, the re_embedder poll loop)
    commit then keep querying on the same connection. Replacing their bare ``conn.commit()``
    with this keeps them correct the instant the fleet flips to ``SET LOCAL``.

    In session-SET mode (default) this is a plain ``conn.commit()`` — the session binding
    already survives the commit, so the re-bind is skipped and behavior is byte-for-byte today.
    """
    conn.commit()
    if local is None:
        local = _search_path_local_default()
    if local:
        # Under PgBouncer transaction pooling the next statement may land on a different server
        # connection with no search_path — re-bind at the start of the new transaction.
        bind_search_path(cur, schema_name, local=True, include_public=include_public)


# ── (B) the site-level seam — the ONE thing swept sites call ──────────────────────────
@contextlib.contextmanager
def data_plane_cursor(schema_name: str, conn=None) -> Iterator:
    """Yield a data-plane cursor bound to ``schema_name``, choosing the mechanism by mode.

    Execution plan §2.2. Three branches, fail-LOUD:

      • **SAAS off** → today's path, ZERO ``saas`` import: open/borrow a psycopg2 connection,
        ``cur.execute("SET search_path TO {schema}")`` (non-LOCAL), yield the raw cursor.
        Byte-for-byte unchanged.
      • **SAAS on + bound** (``current_bound_subject() is not None``) → delegate to the
        structural factory ``saas.subject_cursor.subject_cursor()`` (``SET LOCAL`` + subject
        filter). ``schema_name`` is ignored — the factory binds the bound account schema.
      • **SAAS on + UNBOUND** → raise ``UnboundSubjectError``. A data site reached with no bound
        subject is a wiring bug; we NEVER silently open an unscoped cursor (plan §2.4).

    ``conn`` (SAAS-off path only): borrow an existing connection instead of opening one. When
    ``None`` a short-lived connection is opened from ``POSTGRES_DSN`` and closed on exit.
    """
    if _saas_mode_enabled():
        # Lazy import — ONLY when SAAS is on. Open-core with SAAS off never touches ``saas/``.
        from saas.subject_cursor import (  # noqa: PLC0415 - intentional lazy import
            current_bound_subject,
            subject_cursor,
            UnboundSubjectError,
        )

        if current_bound_subject() is not None:
            with subject_cursor() as sc:
                yield sc
            return
        # SAAS on but no bound subject at a data site = missed-bind wiring bug → fail LOUD.
        raise UnboundSubjectError(
            "data_plane_cursor reached with SAAS_MODE on but NO bound subject — refusing to "
            "open an unscoped cursor (fail-closed). A data access requires set_bound_subject() "
            "at the tenant-bind chokepoint (see execution plan §1/§2.4)."
        )

    # ── SAAS OFF — today's path, unchanged, no saas import ─────────────────────────────
    schema = _validate_schema(schema_name)
    _own_conn = False
    if conn is None:
        if psycopg2 is None:  # pragma: no cover
            raise SearchPathError("psycopg2 unavailable — cannot open a data-plane cursor")
        dsn = os.environ.get("POSTGRES_DSN")
        if not dsn:  # pragma: no cover
            raise SearchPathError("POSTGRES_DSN not set — cannot open a data-plane cursor")
        conn = psycopg2.connect(dsn)
        _own_conn = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.formatted-sql-query.formatted-sql-query schema validated by _validate_schema (^{a-z0-9_}+$) before this line
            yield cur
        if _own_conn:
            conn.commit()
    except Exception:
        if _own_conn:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if _own_conn:
            try:
                conn.close()
            except Exception:
                pass
