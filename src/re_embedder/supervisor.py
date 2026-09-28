"""Event-driven per-tenant supervisor for the re-embedder (Deliverable C).

OPT-IN via ``REEMBEDDER_SUPERVISOR=true``. With the flag unset/false (default) the legacy
single-threaded ``embedder.main()`` loop runs byte-for-byte unchanged — this module is not
even imported on that path.

WHY THIS EXISTS
───────────────
The legacy loop sweeps EVERY ready tenant every ``REEMBED_INTERVAL`` regardless of whether
anything changed, and it never yields to a user turn in flight on the same brain. On a busy
box that is measurable waste (LLM calls against idle tenants) and measurable harm (background
work queued behind a live interactive turn on a low-ceiling brain).

This supervisor replaces that cadence with three structural properties:

  1. ONE asyncio worker per active tenant (cheap, isolated, restartable). Per-task gives
     FAULT isolation (a wedged tenant is caught and cannot stall the others) and FAIRNESS.
     Outbound LLM pacing is delivered by the shared rate budget (``src/api/llm_rate.py``,
     process-independent); per-task does not have to re-deliver it.

  2. EVENT-DRIVEN via an honest comparator. Before ANY LLM/Qdrant work each worker computes a
     cheap per-tenant signal — a small tuple of counts + a max-timestamp — and compares it to
     the last signal that DID work. If the tuple is UNCHANGED the worker idles (NO LLM call,
     NO Qdrant call) and re-checks after a growing backoff. **"Nothing changed ⇒ nothing
     spent, costs nothing"** — the load-bearing bar.

  3. YIELDS to interactive traffic. This process declares itself the BACKGROUND lane
     (``llm_lane``), so every LLM call a phase makes DEFERS instead of fail-opening when the
     shared rate budget is short (``llm_rate``) — interactive traffic keeps the pace. The
     phase-level yield gate below (``_brain_busy``) is the hook for a non-consuming budget
     peek; the open core's ``redis_coord`` offers none, so it never defers a whole phase.

The supervisor DELEGATES the actual per-tenant work to the EXISTING module-level functions in
``embedder.py`` — it does not reimplement them. It is structurally the event-driven + yielding
twin of ``main()``'s per-tenant loop.

The opt-in is the ``REEMBEDDER_SUPERVISOR`` flag.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Optional, Tuple

import psycopg2

try:  # pragma: no cover - import guard matching the embedder.py convention
    import structlog
    log = structlog.get_logger()
except Exception:  # pragma: no cover
    log = logging.getLogger("re_embedder.supervisor")

from src.re_embedder import embedder


# ── env helpers ────────────────────────────────────────────────────────────────────────
_TRUTHY = {"1", "true", "yes", "on"}


def _truthy(v: Optional[str]) -> bool:
    return str(v or "").strip().lower() in _TRUTHY


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() not in ("false", "0", "no", "off")


def supervisor_enabled() -> bool:
    """OPT-IN gate. Default OFF → ``embedder.__main__`` runs the legacy ``main()`` unchanged."""
    return _truthy(os.environ.get("REEMBEDDER_SUPERVISOR", "false"))


def _idle_start_s() -> float:
    return max(1.0, _env_float("REEMBEDDER_IDLE_S", 60.0))


def _idle_max_s() -> float:
    return max(_idle_start_s(), _env_float("REEMBEDDER_IDLE_MAX_S", 300.0))


def _reextract_batch_size() -> int:
    return _env_int("REEXTRACT_BATCH_SIZE", 5)


def _restart_backoff_s() -> float:
    return _env_float("REEMBEDDER_RESTART_BACKOFF_S", 30.0)


# ── THE COMPARATOR — deterministic, exact, cheap ───────────────────────────────────────
# A small tuple of counts + a max-timestamp. Tuple EQUALITY is the "nothing changed" test:
# identical tuple ⇒ no work ⇒ NO LLM/Qdrant spend this pass. This is intentionally a COARSE
# HONEST signal — it does not need to be clever, it needs to be honest about whether there is
# work. A false "changed" only costs one extra pass (the delegated per-tenant functions
# re-detect idle cheaply); a false "unchanged" is the one that must NEVER happen, and it
# cannot: any row that lands / promotes / expires / is re-extracted flips a count or the
# max(updated_at). Fail-safe on error → "changed" (run work; WE DON'T FORGET).

_SIGNAL_ERROR = ("__signal_error__",)


def tenant_work_signal(db_conn, schema_name: str) -> Tuple[Any, ...]:
    """Compute the per-tenant work signal on ``db_conn``.

    The caller opens the admin connection; this binds the tenant ``search_path`` (NO public —
    runtime metadata lives in the tenant schema, never read from public at runtime) and runs
    five cheap indexed aggregates. Returns a 6-tuple::

        (staged_count, staged_max_ts, episodic_pending, ontology_pending,
         nameconflict_pending, documents_pending)

    HONESTY CONTRACT (spec §3): if every element is unchanged since the last work pass, there
    is genuinely nothing for the supervised phases to do → idle (zero LLM calls). Each element
    covers a work-driving input of a delegated phase. ``documents_pending`` covers
    ``drain_pending_documents`` — without it, a document enqueued on an otherwise-idle tenant
    would change NOTHING the comparator sees and be deferred indefinitely (critic C D1).

    NEVER raises — any error returns the ``_SIGNAL_ERROR`` sentinel (a 1-tuple that never
    compares equal to a real 6-tuple), so a probe failure fails-safe toward RUNNING the work.
    The ``documents`` table is optional per tenant (only present when a tenant has uploaded);
    its absence is treated as 0 pending, NOT an error.
    """
    try:
        with db_conn.cursor() as cur:
            # SET search_path TO {schema} (NO public) — per-tenant isolation hard rule.
            cur.execute(f"SET search_path TO {schema_name}")  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.formatted-sql-query.formatted-sql-query schema_name from tenant provisioning (UUID-derived)
            cur.execute(
                "SELECT count(*), coalesce(max(last_seen_at), 'epoch')::text "
                "FROM staged_facts "
                "WHERE fact_class IN ('B','C') "
                "  AND (promoted_at IS NULL OR expires_at IS NOT NULL)"
            )
            staged = cur.fetchone()
            cur.execute("SELECT count(*) FROM episodic_log WHERE reextracted_at IS NULL")
            ep = cur.fetchone()
            # ontology_evaluations has no `status` column on the real per-tenant schema — a
            # candidate is "pending" (work to do) iff the re_embedder has not adjudicated it yet
            # (re_embedder_decision IS NULL). Verified against the live schema.
            cur.execute("SELECT count(*) FROM ontology_evaluations WHERE re_embedder_decision IS NULL")
            ont = cur.fetchone()
            cur.execute("SELECT count(*) FROM entity_name_conflicts WHERE status='pending'")
            nc = cur.fetchone()
            # documents is OPTIONAL per tenant — guard via to_regclass so a table-less tenant
            # reads 0 pending rather than throwing UndefinedTable (which would force the whole
            # comparator to its fail-safe "run" branch every cycle and defeat the idle win).
            cur.execute("SELECT to_regclass('documents')")
            docs_table = cur.fetchone()
            docs_pending = 0
            if docs_table and docs_table[0] is not None:
                cur.execute("SELECT count(*) FROM documents WHERE status='pending'")
                _d = cur.fetchone()
                docs_pending = int(_d[0]) if _d and _d[0] is not None else 0
        db_conn.commit()
        return (
            int(staged[0]) if staged and staged[0] is not None else 0,
            str(staged[1]) if staged and staged[1] is not None else "epoch",
            int(ep[0]) if ep and ep[0] is not None else 0,
            int(ont[0]) if ont and ont[0] is not None else 0,
            int(nc[0]) if nc and nc[0] is not None else 0,
            docs_pending,
        )
    except Exception as e:  # noqa: BLE001 — comparator must never raise; fail-safe to "run"
        try:
            db_conn.rollback()
        except Exception:
            pass
        log.warning(
            "re_embedder.supervisor.signal_error",
            schema=schema_name,
            error=str(e)[:200],
            note="fail-safe: treating as changed so work runs (we don't forget)",
        )
        return _SIGNAL_ERROR


def _compute_signal(postgres_dsn: str, schema_name: str) -> Tuple[Any, ...]:
    """Open a short-lived ADMIN connection, compute the signal, close.

    Isolated per call (fresh connection + SET search_path) so no cross-tenant search_path
    leakage can ever occur. Fail-safe to ``_SIGNAL_ERROR`` on any connect/exec error.
    """
    try:
        with psycopg2.connect(postgres_dsn) as conn:
            return tenant_work_signal(conn, schema_name)
    except Exception as e:  # noqa: BLE001 — never raise; fail-safe to "run"
        log.warning(
            "re_embedder.supervisor.signal_connect_failed",
            schema=schema_name,
            error=str(e)[:200],
            note="fail-safe: treating as changed so work runs",
        )
        return _SIGNAL_ERROR


# ── brain scope + yield gate ───────────────────────────────────────────────────────────


def _resolve_brain_scope_safe():
    """The scope of a non-consuming shared-budget peek, or None.

    The open core's rate governance (``llm_rate`` + ``redis_coord``) paces per CALL — a
    background-lane call defers when the shared bucket is short — and exposes no
    non-consuming "tokens available" peek. There is therefore no phase-level scope to consult:
    None, and ``_brain_busy`` never defers a whole phase. Never raises.
    """
    return None


def _brain_busy(brain_scope) -> bool:
    """Phase-level YIELD gate. True iff a whole LLM-bearing phase should be deferred.

    None scope → False (do NOT yield a phase; per-call deferral in the BACKGROUND lane still
    applies). Never raises. It gates only the WORK-level LLM phase, never the comparator
    (the comparator makes no LLM call).
    """
    if brain_scope is None:
        return False
    return False


def _resolve_statement_route(backend_api_url: str) -> str:
    """Resolve the STATEMENT extractor route once per work pass (mirrors ``main()``'s per-cycle
    ``GET /internal/ingest-route``). Fail-safe → ``"rewrite"``. Cheap (local backend)."""
    try:
        import httpx
        r = httpx.get(f"{backend_api_url}/internal/ingest-route", timeout=5.0)
        r.raise_for_status()
        route = (r.json().get("statement_extractor") or "rewrite").strip().lower()
        return route if route in ("spine", "rewrite") else "rewrite"
    except Exception as e:  # noqa: BLE001 — route resolution must never block upkeep
        log.debug("re_embedder.supervisor.route_fallback", error=str(e)[:120])
        return "rewrite"


def _resolve_qwen_api_url() -> str:
    """Resolve the chat-LLM endpoint hint the same way ``main()`` does.

    This value is only the embedding-URL hint threaded through the delegated functions; every
    LLM call routes through the centralized stack.
    """
    try:
        from src.api.llm_client import (
            get_backend_endpoint,
            get_endpoint_list,
        )
        ep = get_backend_endpoint()
        if ep:
            return ep
        eps = get_endpoint_list()
        if eps:
            return eps[0]
        return "http://localhost:11434/v1/chat/completions"
    except Exception:  # noqa: BLE001
        return ""


# ── THE PER-TENANT WORK — delegates to embedder module functions ───────────────────────
# Each phase calls an EXISTING module-level function in embedder.py (the same functions
# ``main()``'s per-tenant loop calls). This supervisor does NOT reimplement them. Phases are
# fault-isolated (one phase raising never aborts the tenant's cycle) and LLM-bearing phases
# consult the YIELD gate first.


def _run_phases_on_conn(
    db_conn,
    *,
    user_id: str,
    schema_name: str,
    postgres_dsn: str,
    backend_api_url: str,
    qdrant_url: str,
    qwen_api_url: str,
    statement_route: str,
    brain_scope,
    ingest_enabled: bool,
    reextract_enabled: bool,
    batch_size: int = 5,
) -> None:
    """Run the per-tenant work phases on an OPEN per-tenant connection (caller has SET
    search_path, NO public).

    LLM-bearing phases consult the YIELD gate and DEFER when the brain is busy. Every phase is
    wrapped so a raise is caught, logged loud, and the cycle continues (fault isolation). All
    work is skipped under an ingest freeze (mirrors ``main()``'s ``INGEST_ENABLED`` contract).
    """
    if not ingest_enabled:
        # Knowledge-store freeze: skip every knowledge-mutating phase (mirrors main()).
        return

    def _phase(name: str, llm_bearing: bool, run: Callable[[], None]) -> None:
        if llm_bearing and _brain_busy(brain_scope):
            log.info(
                "re_embedder.supervisor.yielded_phase",
                phase=name,
                schema=schema_name,
                reason="brain_bucket_low",
                note="deferred to next cycle; comparator still shows it pending",
            )
            return
        try:
            run()
        except Exception as e:  # noqa: BLE001 — fault isolation: one phase never aborts the cycle
            log.error(
                "re_embedder.supervisor.phase_error",
                phase=name,
                schema=schema_name,
                error_type=type(e).__name__,
                error=str(e)[:200],
                note="non-fatal, continuing to next phase",
            )

    # ── non-LLM staged-fact lifecycle (DB + local embed housekeeping) ──
    _phase("promote_staged", False, lambda: embedder.promote_staged_facts(
        db_conn, qdrant_url, user_id=user_id, schema_name=schema_name))
    _phase("expire_staged", False, lambda: embedder.expire_staged_facts(
        db_conn, qdrant_url, user_id=user_id))

    # ── LLM-bearing: episodic re-extraction + document drain (backend extraction call) ──
    if reextract_enabled:
        _phase("reextract_episodic", True, lambda: embedder.reextract_episodic(
            db_conn, backend_api_url, user_id=user_id, schema_name=schema_name,
            batch_size=batch_size, statement_route=statement_route))
    _phase("drain_documents", True, lambda: embedder.drain_pending_documents(
        db_conn, backend_api_url, user_id=user_id, schema_name=schema_name,
        statement_route=statement_route))

    # ── non-LLM Class-C counter/tier phases ──
    _phase("promote_class_c", False, lambda: embedder.promote_class_c_hits(
        db_conn, qdrant_url, qwen_api_url, user_id=user_id, schema_name=schema_name))
    _phase("decay_class_c", False, lambda: embedder.decay_class_c_hits(
        db_conn, qdrant_url, user_id=user_id))

    # ── LLM-bearing: ontology growth + name-conflict arbitration ──
    _phase("ontology_eval", True, lambda: embedder.evaluate_ontology_candidates(
        db_conn, qwen_api_url))
    _phase("name_conflicts", True, lambda: embedder.resolve_name_conflicts(
        db_conn, qwen_api_url))

    # ── non-LLM reconciliation (DB only; take the DSN, not the per-tenant conn) ──
    _phase("hierarchy_reconcile", False, lambda: embedder._reconcile_hierarchy_links(
        postgres_dsn, schema_name))
    _phase("staged_rel_upgrade", False, lambda: embedder._upgrade_staged_facts_with_known_rels(
        postgres_dsn, schema_name))


def _run_tenant_work_cycle(
    *,
    user_id: str,
    schema_name: str,
    postgres_dsn: str,
    backend_api_url: str,
    qdrant_url: str,
    qwen_api_url: str,
    statement_route: str,
    ingest_enabled: bool = True,
    reextract_enabled: bool = True,
    batch_size: int = 5,
    _bind_brain: bool = True,
) -> None:
    """Bind the tenant attribution, open ONE per-tenant connection (SET search_path, NO
    public), run the phases (yield-gated + fault-isolated), clear the attribution.

    Called under the supervisor's ``work_lock`` (see ``_tenant_worker``) so the re-embedder's
    MODULE-GLOBAL tenant identity (``embedder._current_tenant_user_id``), which several
    delegated functions read as an attribution fallback, stays coherent across concurrent
    tenant tasks. DELEGATES to embedder module functions.
    """
    if _bind_brain:
        embedder._reembedder_bind_tenant(schema_name)
    try:
        brain_scope = _resolve_brain_scope_safe()
        with psycopg2.connect(postgres_dsn) as db_conn:
            with db_conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema_name}")  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.formatted-sql-query.formatted-sql-query schema_name from tenant provisioning (UUID-derived)
            db_conn.commit()
            _run_phases_on_conn(
                db_conn,
                user_id=user_id,
                schema_name=schema_name,
                postgres_dsn=postgres_dsn,
                backend_api_url=backend_api_url,
                qdrant_url=qdrant_url,
                qwen_api_url=qwen_api_url,
                statement_route=statement_route,
                brain_scope=brain_scope,
                ingest_enabled=ingest_enabled,
                reextract_enabled=reextract_enabled,
                batch_size=batch_size,
            )
            db_conn.commit()
    finally:
        if _bind_brain:
            embedder._reembedder_clear_tenant()


# ── THE PER-TENANT WORKER — one async task per tenant ──────────────────────────────────


async def _tenant_worker(
    user_id: str,
    schema_name: str,
    *,
    postgres_dsn: str,
    backend_api_url: str,
    qdrant_url: str,
    qwen_api_url: str,
    reextract_enabled: bool,
    ingest_enabled: bool,
    work_lock: asyncio.Lock,
    signal_fn: Optional[Callable[[str, str], Tuple[Any, ...]]] = None,
    work_fn: Optional[Callable[..., None]] = None,
    _max_passes: Optional[int] = None,
) -> None:
    """One async worker per tenant. Loops: comparator → idle-or-work.

    The comparator runs LOCK-FREE (cheap, no shared mutable state) inside ``asyncio.to_thread``
    so all tenants' idle probes run concurrently. The WORK runs SERIALIZED under ``work_lock``
    (acquired only when work is actually due) so the re-embedder's module-global tenant identity
    stays coherent — idle tenants, the common case, never contend.

    Per-iteration errors are caught, logged loud, and backed off (the task stays alive); the
    supervisor's watcher restarts the task if it ever exits anyway. ``_max_passes`` (test-only)
    bounds the loop so a test can run a worker for a fixed number of passes and return.
    """
    _signal = signal_fn or _compute_signal
    _work = work_fn or _run_tenant_work_cycle
    last_signal: Optional[Tuple[Any, ...]] = None
    idle_start = _idle_start_s()
    idle_max = _idle_max_s()
    consecutive_idle = 0
    passes = 0

    while _max_passes is None or passes < _max_passes:
        passes += 1
        try:
            signal = await asyncio.to_thread(_signal, postgres_dsn, schema_name)

            if signal == last_signal:
                # UNCHANGED ⇒ idle. NO LLM/Qdrant work this pass. Grow the backoff up to the
                # ceiling; reset on the next pass that does work.
                consecutive_idle += 1
                idle_backoff = min(idle_max, idle_start * consecutive_idle)
                log.debug(
                    "re_embedder.supervisor.idle",
                    schema=schema_name,
                    consecutive=consecutive_idle,
                    backoff_s=round(idle_backoff, 1),
                )
                await asyncio.sleep(idle_backoff)
                continue

            # CHANGED (or first pass) ⇒ run work. Reset idle state and remember the signal.
            consecutive_idle = 0
            last_signal = signal
            statement_route = await asyncio.to_thread(_resolve_statement_route, backend_api_url)
            async with work_lock:
                await asyncio.to_thread(
                    _work,
                    user_id=user_id,
                    schema_name=schema_name,
                    postgres_dsn=postgres_dsn,
                    backend_api_url=backend_api_url,
                    qdrant_url=qdrant_url,
                    qwen_api_url=qwen_api_url,
                    statement_route=statement_route,
                    ingest_enabled=ingest_enabled,
                    reextract_enabled=reextract_enabled,
                    batch_size=_reextract_batch_size(),
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — never let a worker die quietly
            log.error(
                "re_embedder.supervisor.worker_iteration_error",
                schema=schema_name,
                error_type=type(e).__name__,
                error=str(e)[:200],
                note="non-fatal, backing off and continuing next pass",
            )
            await asyncio.sleep(_restart_backoff_s())
            continue

    log.info("re_embedder.supervisor.worker_done", schema=schema_name, passes=passes)


# ── ACTIVE TENANT RESOLUTION — reuses embedder's existing helpers ──────────────────────


def _resolve_active_tenants(postgres_dsn: str):
    """Return ``[(user_id, schema_name), ...]`` for every ACTIVE tenant.

    REUSES the existing helpers in ``embedder.py`` (no reimplementation): the
    ``public.user_provisioning WHERE status='ready'`` source, the ghost-skip
    ``tenant_schema_is_live`` probe. Fail-safe: any resolution error → empty list (no workers
    spawned) and a loud log, never a crash.
    """
    try:
        with psycopg2.connect(postgres_dsn) as admin:
            with admin.cursor() as cur:
                cur.execute(
                    "SELECT user_id, schema_name FROM public.user_provisioning "
                    "WHERE status = 'ready' ORDER BY ready_at ASC"
                )
                ready = [(r[0], r[1]) for r in cur.fetchall()]
        if not ready:
            return []
        live = []
        with psycopg2.connect(postgres_dsn) as admin:
            for uid, schema in ready:
                if embedder.tenant_schema_is_live(admin, schema):
                    live.append((uid, schema))
        return live
    except Exception as e:  # noqa: BLE001 — never crash the supervisor entry point
        log.error(
            "re_embedder.supervisor.tenant_resolve_failed",
            error_type=type(e).__name__,
            error=str(e)[:200],
            note="no workers spawned this start",
        )
        return []


# ── THE SUPERVISOR — spawns + watches one task per tenant ──────────────────────────────


async def run_supervisor(
    postgres_dsn: Optional[str],
    backend_api_url: str,
    qdrant_url: str,
    *,
    _tenants=None,
    _max_passes: Optional[int] = None,
    _supervise_for: Optional[float] = None,
    _signal_fn: Optional[Callable[[str, str], Tuple[Any, ...]]] = None,
    _work_fn: Optional[Callable[..., None]] = None,
) -> None:
    """Opt-in entry point (``REEMBEDDER_SUPERVISOR=true``).

    Resolves the active tenant list, spawns ONE ``_tenant_worker`` task per tenant, and
    supervises: a per-tenant watcher restarts a crashed worker (with backoff) so a wedged
    tenant cannot stall the others.

    Test seams (all underscore-prefixed, never set in prod): ``_tenants`` overrides tenant
    resolution; ``_max_passes`` bounds each worker; ``_supervise_for`` runs the supervisor for
    a wall-clock duration then cancels; ``_signal_fn`` / ``_work_fn`` inject fakes so the
    worker logic is exercisable with no DB / no Redis / no LLM.
    """
    if not postgres_dsn:
        log.error("re_embedder.supervisor.no_postgres_dsn")
        return

    qwen_api_url = _resolve_qwen_api_url()
    ingest_enabled = _env_bool("INGEST_ENABLED", True)
    reextract_enabled = _env_bool("REEXTRACT_ENABLED", True)

    tenants = _tenants if _tenants is not None else _resolve_active_tenants(postgres_dsn)
    if not tenants:
        log.warning("re_embedder.supervisor.no_tenants")
        return

    log.info(
        "re_embedder.supervisor.start",
        tenants=len(tenants),
        ingest_enabled=ingest_enabled,
        reextract_enabled=reextract_enabled,
    )

    work_lock = asyncio.Lock()
    tasks = {}

    async def _spawn_and_watch(uid, schema):
        """Restart loop for ONE tenant. A worker that exits (crash) is re-spawned after backoff."""
        backoff = _restart_backoff_s()
        while True:
            try:
                await _tenant_worker(
                    uid, schema,
                    postgres_dsn=postgres_dsn,
                    backend_api_url=backend_api_url,
                    qdrant_url=qdrant_url,
                    qwen_api_url=qwen_api_url,
                    reextract_enabled=reextract_enabled,
                    ingest_enabled=ingest_enabled,
                    work_lock=work_lock,
                    signal_fn=_signal_fn,
                    work_fn=_work_fn,
                    _max_passes=_max_passes,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — supervise: never lose a tenant silently
                log.error(
                    "re_embedder.supervisor.worker_crashed",
                    schema=schema,
                    error_type=type(e).__name__,
                    error=str(e)[:200],
                    restart_in_s=round(backoff, 1),
                )
            if _max_passes is not None:
                break  # test mode: don't loop-restart a finite worker
            await asyncio.sleep(backoff)

    for uid, schema in tenants:
        tasks[schema] = asyncio.create_task(_spawn_and_watch(uid, schema), name=f"reembed:{schema}")

    try:
        if _supervise_for is not None:
            await asyncio.sleep(_supervise_for)
        else:
            # Prod: run forever — the watcher tasks never return.
            await asyncio.gather(*tasks.values())
    finally:
        for t in tasks.values():
            if not t.done():
                t.cancel()
        for t in tasks.values():
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        log.info("re_embedder.supervisor.stop", tenants=len(tasks))
