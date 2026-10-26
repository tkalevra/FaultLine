"""Growth work queue — the DURABLE BREADCRUMB for brain-starved growth work (gauntlet R8).

WHY THIS MODULE EXISTS
----------------------
When ``_reembedder_llm_lane_open`` (embedder.py) closes — the tenant's brain is
rate-limited, circuit-broken, posture-cooled, or simply down — the growth loops
(ontology enrichment, synonym convergence, what-is classify / ±6 climb, Class-C
promotion's classify-before-promote half) skip UNRECORDED. The sweep ledger keeps
the subsystem DUE, but the system forgets WHAT was starved: which subsystem for
which tenant, which staged row was left unpromoted. That is the BUG-005 wedge
shape — work due but invisible. Document-derived staged facts have sat unpromoted
and unconverged for hours because the deferral was silent.

This module is the "ingest now, enrich later" pattern (DEV/RESEARCH/
recall-uplift-sota-20261024.md: Letta sleep-time compute, LightRAG's KV-cache —
defer the LLM-bound work durably, do it when the brain returns):

  * WRITE PATH — ``record_deferred``: called at every lane-closed deferral point.
    Fail-safe by construction: a queue failure NEVER raises into the growth loop;
    it falls through to today's skip-unrecorded behaviour (the ledger still keeps
    the subsystem due, so nothing is lost — only the breadcrumb is).
  * DRAIN PATH — ``drain``: when the lane opens, the next growth cycle drains the
    queue PRIORITY-FIRST before doing new work. Each item's deferred operation is
    re-attempted via a caller-supplied runner; completed_at is stamped on success,
    left pending on failure (rate-limited retry — one drain attempt per cycle).
  * BOUNDED — at most GROWTH_QUEUE_MAX pending items per tenant (default 5000),
    oldest evicted. Completed rows are prunable (prune_completed; drain prunes
    rows completed more than GROWTH_QUEUE_PRUNE_DAYS ago).

THE QUEUE IS NOT THE LEDGER. The sweep ledger (sweep_ledger.py) decides whether
work is DUE; this table records WHICH work was due but brain-starved, so recovery
is ordered and observable. The partial unique index (operation_type, target_id)
WHERE completed_at IS NULL means the same target is never re-deferred while a
pending item exists — the write path is idempotent across cycles of outage.

NO LLM IN THE QUEUE MECHANICS — this module is pure SQL bookkeeping. The
re-attempt itself runs the growth subsystem's own bounded function via the
caller's runner.

PER-TENANT: the table lives in each tenant schema (search_path-bound, NO public —
schema = scope). Migration 300 fans it out to existing tenants; the provisioning
template (src/provisioning/templates/user_schema.sql) creates it for fresh ones.
"""

import logging
import os
import time

import psycopg2

from src.api.db_read import release_read_transaction  # READ-BARRIER — the re-attempted
# operations block on the tenant brain; the claim SELECT must not ride across that wait

log = logging.getLogger(__name__)


# ── Operation families (the growth-loop deferral points) ─────────────────────
#
# The four families the defect statement names. The precise subsystem being
# deferred rides in target_id (the lane gate defers a WHOLE subsystem run for a
# tenant; the Class-C promotion gate defers one staged row). The subsystem→family
# map below is the single translation table — SUBSYSTEMS (sweep_ledger.py) states
# which lanes bear LLM spend; every llm=True subsystem must appear here or its
# deferral is silently unrecorded (the exact defect this module exists to close).
OPERATION_TYPES = ("promotion", "enrichment", "synonym_convergence", "whatis_climb",
                   "recall_miss_reexamine", "recall_miss_brain")

SUBSYSTEM_OPERATION = {
    "ontology_eval": "enrichment",
    "aspect_synonym": "synonym_convergence",
    "synonym_convergence": "synonym_convergence",
    "whatis_classify": "whatis_climb",
    "classify_climb": "whatis_climb",
    "head_tail_sweep": "enrichment",
    "taxonomy_discovery": "enrichment",
    "orphan_stub_and_nl_fill": "enrichment",
    "name_conflicts": "enrichment",
}

# Drain order (1 = highest): promotion is recall-visible (a user-seen miss — the
# EXCLUDED-ledger lore "never put a marker in front of it" applies to GATING, not
# to a retry breadcrumb), then the plan-doc order enrichment → synonym
# convergence → whatis-climb. 5 stays the column DEFAULT for unrecognised types.
# recall_miss_reexamine (gauntlet R7) rides at 2 beside promotion — a REAL USER
# asked and the graph could not answer; that is the same recall-visible urgency.
# recall_miss_brain (gauntlet R9, deterministic-first uplift) rides at 5: it is
# the SECOND phase of a recall-miss marker whose deterministic pass (spine deriver
# + GLiNER2, in-process, free) yielded ZERO edges on real content — the brain
# re-examination is the FALLBACK, deliberately BELOW the deterministic phase in
# drain order (owner ruling: GLiNER2 + spaCy primary, BYO brain fallback).
OPERATION_PRIORITY = {
    "promotion": 2,
    "enrichment": 5,
    "synonym_convergence": 6,
    "whatis_climb": 7,
    "recall_miss_reexamine": 2,
    "recall_miss_brain": 5,
}

#: Operations whose DRAIN RUNNER spends ZERO brain budget (BUG-08): their
#: re-attempt is pure SQL + in-process deterministic work (the R9 spine deriver
#: + GLiNER2 run IN-PROCESS; the only wire call is the /ingest commit door,
#: which is brain-free). A brain-lane gate must NEVER block these — on prod the
#: document drain keeps the tenant brain's lane permanently closed (posture
#: tightened / fail-closed), and one blanket lane check gating the whole drain
#: left 8 recall_miss markers pending for 3+ hours while the deterministic
#: phase that could have resolved them cost nothing. Brain-BEARING operations
#: (promotion's classify pass, enrichment, synonym convergence, whatis-climb,
#: and recall_miss_brain — whose flip hands the chunk BACK to the paced brain's
#: document drain) stay lane-gated.
DETERMINISTIC_OPERATIONS = frozenset({"recall_miss_reexamine"})

# ── R7 recall-miss markers (gauntlet R7; the query side's feedback into growth) ─
#
# Written by the /query wrapper when Layer 1 (the graph walk) returns ZERO facts
# while Layer 2 (the R6 passage lane) hits: a user asked, the answer was IN a
# retained chunk, but the graph walk could not reach it — so extraction missed
# something in THAT chunk. The marker names the chunk ('{schema}:{document_id}:
# {chunk_index}') and the growth drain (embedder._recall_miss_reexamine_chunk)
# re-owes exactly that chunk to the NORMAL document drain under the R2 replay
# marker, so the next time the question is asked the graph walk finds it.
RECALL_MISS_OPERATION = "recall_miss_reexamine"

#: R9 second phase: the brain re-examination of a recall-miss chunk whose
#: deterministic pass (spine + GLiNER2) yielded zero edges on real content.
RECALL_MISS_BRAIN_OPERATION = "recall_miss_brain"

#: R7 rate limit: at most ONE marker per (schema, document, chunk) per hour —
#: re-queried misses within the window write NOTHING (plan §R7 "rate-limited").
_RECALL_MISS_RATE_INTERVAL = "1 hour"


def recall_miss_target_id(schema_name, document_id, chunk_index) -> str:
    """The marker's target_id: '{schema}:{document_id}:{chunk_index}' (plan §R7)."""
    return f"{schema_name}:{int(document_id)}:{int(chunk_index)}"


def record_recall_miss(schema_name, document_id, chunk_index) -> bool:
    """Persist ONE recall_miss marker. NEVER RAISES (the /query fire-and-forget contract).

    Rate limit is IN THE SQL, not the process: the INSERT is guarded by a NOT
    EXISTS on any row (pending OR completed) for the same target created inside
    the last hour, and the partial unique (ON CONFLICT DO NOTHING) collapses
    concurrent/duplicate pending writes. So a chunk re-queried inside the hour —
    same process, another worker, or after a fast drain completed the row —
    writes NOTHING. Markers are bounded by GROWTH_QUEUE_MAX like every other
    pending row (oldest evicted).

    Shares the recorder breaker with record_deferred: after consecutive connect
    failures the marker path backs off (fail-safe direction is SKIP — the query
    response has already gone out; a missed marker costs one growth opportunity,
    never a broken turn).

    Returns True when a PENDING row for this target exists on exit. A throttled
    or breaker-skipped attempt returns False WITHOUT touching the row — the
    SQL guard stays authoritative across processes; the throttle only bounds
    this process's connection churn.
    """
    global _connect_failures, _breaker_until
    if not schema_name or document_id is None or chunk_index is None:
        return False
    try:
        target_id = recall_miss_target_id(schema_name, document_id, chunk_index)
    except (TypeError, ValueError):
        return False
    if time.monotonic() < _breaker_until:
        return False
    dsn = os.environ.get("POSTGRES_DSN", "")
    if not dsn:
        return False
    # CONNECTION THROTTLE (round-1 critic, record_deferred's exact pattern): the
    # 1-hour SQL guard is authoritative, but a miss-heavy tenant would pay a full
    # psycopg2.connect per refusal to LEARN what the guard then refuses. At most
    # one recording attempt per (schema, op, target) per _RECORD_MIN_INTERVAL_S —
    # a fresh marker for a chunk not seen this minute always goes through; only
    # re-queried misses inside the window skip the connect. Fail-safe direction
    # unchanged: skip = one missed growth breadcrumb, never a broken turn.
    _tkey = (str(schema_name), RECALL_MISS_OPERATION, target_id)
    _now = time.monotonic()
    if _last_record.get(_tkey, 0.0) and (_now - _last_record.get(_tkey, 0.0)) < _RECORD_MIN_INTERVAL_S:
        return False
    _last_record[_tkey] = _now
    # CARDINALITY CAP (round-2 critic MINOR 2): record_deferred's keys are one
    # per (tenant, subsystem) — bounded. The recall-miss key domain is one per
    # DISTINCT MISSED CHUNK — unbounded on a long-lived process. When the map
    # crosses the cap it DROPS THE OLDEST _LAST_RECORD_KEEP_ENTRIES (dicts are
    # insertion-ordered; slicing [N:] keeps the newer len−N entries — the
    # constant is a DROP count, not a keep count): the dropped entries merely
    # expire early, which degrades to one extra connect — never a wrong write.
    if len(_last_record) > _LAST_RECORD_MAX_ENTRIES:
        _recent = list(_last_record.items())[_LAST_RECORD_KEEP_ENTRIES:]
        _last_record.clear()
        _last_record.update(_recent)
    conn = None
    try:
        conn = psycopg2.connect(dsn, connect_timeout=3)
        with conn.cursor() as cur:
            # Bind the tenant schema (NO public — schema = scope; a bare connection
            # would resolve the INSERT to public.growth_work_queue, which does not
            # exist).
            cur.execute(f"SET search_path TO {schema_name}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query — schema from UUID-derived source with validation
            # Hourly rate limit + pending dedup in ONE statement: NOT EXISTS refuses
            # the insert when ANY row (pending or completed) for this target was
            # created inside the window; ON CONFLICT refuses a duplicate PENDING row
            # that raced past the guard.
            cur.execute(
                "INSERT INTO growth_work_queue (operation_type, target_id, priority) "
                "SELECT %s, %s, %s "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM growth_work_queue"
                "  WHERE operation_type = %s AND target_id = %s"
                "    AND created_at > now() - INTERVAL '" + _RECALL_MISS_RATE_INTERVAL + "') "
                "ON CONFLICT (operation_type, COALESCE(target_id, '')) "
                "WHERE completed_at IS NULL DO NOTHING",
                (RECALL_MISS_OPERATION, target_id,
                 OPERATION_PRIORITY[RECALL_MISS_OPERATION],
                 RECALL_MISS_OPERATION, target_id),
            )
            fresh = cur.rowcount == 1
            _evict_beyond_cap(cur)
            cur.execute(
                "SELECT 1 FROM growth_work_queue"
                " WHERE operation_type = %s AND target_id = %s"
                "   AND completed_at IS NULL",
                (RECALL_MISS_OPERATION, target_id),
            )
            pending = cur.fetchone() is not None
        conn.commit()
        _connect_failures = 0
        if fresh:
            log.info(
                "growth_work_queue.recall_miss_marker "
                f"schema={schema_name} target={target_id} "
                f"priority={OPERATION_PRIORITY[RECALL_MISS_OPERATION]} "
                "(graph walk missed; passage hit names the chunk to re-examine)"
            )
        else:
            log.debug(
                "growth_work_queue.recall_miss_marker_skipped "
                f"schema={schema_name} target={target_id} "
                f"(rate-limited to one per {_RECALL_MISS_RATE_INTERVAL} or already pending)"
            )
        return pending
    except Exception as e:  # noqa: BLE001 — fail-safe: never breaks the query turn
        _connect_failures += 1
        if _connect_failures >= _BREAKER_FAILURES:
            _breaker_until = time.monotonic() + _BREAKER_BACKOFF_S
            _connect_failures = 0
            log.warning(
                "growth_work_queue.recorder_breaker_open "
                f"backoff_s={_BREAKER_BACKOFF_S:.0f} reason={type(e).__name__} "
                "note=recall-miss markers skip (fail-safe) while Postgres is unreachable"
            )
        else:
            log.debug(
                "growth_work_queue.record_recall_miss_failed "
                f"schema={schema_name} target={target_id}: {e}"
            )
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def record_brain_defer_on_conn(conn, schema_name: str, target_id: str):
    """R9 deterministic-first uplift: persist the BRAIN-phase deferral for ONE
    recall-miss target, on the GROWTH DRAIN'S OWN connection (search_path already
    bound — no second connect mid-drain).

    Written by the reexamine runner when its deterministic pass (spine deriver +
    GLiNER2, in-process) yielded ZERO edges on real content: the marker's
    deterministic phase is RESOLVED (completed), and this row carries the brain
    re-examination to a later drain cycle at priority 5 — below every
    deterministic phase, per the owner ruling (brain is the fallback, never the
    primary).

    Bounded without an hourly guard of its own: the spawning recall_miss marker
    is already rate-limited to one per (schema, doc, chunk) per hour in SQL, the
    partial unique (ON CONFLICT DO NOTHING, the identical COALESCE expression
    form — see uq_growth_work_pending) collapses concurrent/duplicate pending
    writes, and the brain runner's own flip refuses at the R2 reextract-attempts
    ceiling. COMMITs on success so a later fault in the SAME drain cycle cannot
    roll the deferral back while the spawning marker is already completed.
    Enforces GROWTH_QUEUE_MAX via the same eviction the sibling write paths run.

    NEVER RAISES (fail-safe contract).

    Returns True when the INSERT stamped a fresh row; False on the CONFLICT arm
    (a pending row for this target already exists — the desired end state, so
    the caller may complete); None on a FAULT (nothing was written — the caller
    must NOT complete, or the marker retires without effect: the round-3 critic
    'completes-without-effect' class).
    """
    if not conn or not schema_name or not target_id:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO growth_work_queue (operation_type, target_id, priority) "
                "SELECT %s, %s, %s "
                "ON CONFLICT (operation_type, COALESCE(target_id, '')) "
                "WHERE completed_at IS NULL DO NOTHING",
                (RECALL_MISS_BRAIN_OPERATION, str(target_id),
                 OPERATION_PRIORITY[RECALL_MISS_BRAIN_OPERATION]),
            )
            fresh = cur.rowcount == 1
            if fresh:
                _evict_beyond_cap(cur)
        conn.commit()
        if fresh:
            log.info(
                "growth_work_queue.recall_miss_brain_defer "
                f"schema={schema_name} target={target_id} "
                f"priority={OPERATION_PRIORITY[RECALL_MISS_BRAIN_OPERATION]} "
                "(deterministic pass empty on real content; brain re-examination deferred)"
            )
        return fresh
    except Exception as e:  # noqa: BLE001 — never break the drain
        try:
            conn.rollback()
            if schema_name:
                with conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {schema_name}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query — schema from UUID-derived source with validation
        except Exception:  # noqa: BLE001
            pass
        log.warning(
            "growth_work_queue.recall_miss_brain_defer_failed "
            f"schema={schema_name} target={target_id}: {type(e).__name__}: {str(e)[:120]}")
        return None


def _queue_max() -> int:
    """Live read so a deployment (or test) can retune the cap without a restart."""
    try:
        v = int(os.environ.get("GROWTH_QUEUE_MAX", "5000") or "5000")
    except (TypeError, ValueError):
        return 5000
    return v if v > 0 else 5000


def _drain_batch() -> int:
    try:
        v = int(os.environ.get("GROWTH_DRAIN_BATCH", "50") or "50")
    except (TypeError, ValueError):
        return 50
    return v if v > 0 else 50


def _prune_days() -> int:
    try:
        v = int(os.environ.get("GROWTH_QUEUE_PRUNE_DAYS", "7") or "7")
    except (TypeError, ValueError):
        return 7
    return v if v > 0 else 7


def _evict_beyond_cap(cur) -> None:
    """Bound the queue: at most GROWTH_QUEUE_MAX pending rows — evict the OLDEST
    overflow. DESC + OFFSET keeps the newest cap rows: created_at is the
    TRANSACTION clock, so a batch inserted together ties to the microsecond and
    the id tiebreak is what actually orders them. Shared by every write path
    (record_deferred, record_recall_miss) so the cap is one rule, not two."""
    cur.execute(
        "DELETE FROM growth_work_queue WHERE id IN ("
        "  SELECT id FROM growth_work_queue"
        "  WHERE completed_at IS NULL"
        "  ORDER BY created_at DESC, id DESC"
        "  OFFSET %s"
        ")",
        (_queue_max(),),
    )


# ── Recorder hygiene ──────────────────────────────────────────────────────────
#
# The lane-closed hook (inside _reembedder_llm_lane_open) has NO connection in
# scope, so record_deferred opens a short-lived one. Two guards keep that cheap
# and outage-proof:
#   * THROTTLE — at most one recording attempt per (schema, op, target) per
#     GROWTH_QUEUE_RECORD_MIN_INTERVAL_S. The partial unique already makes
#     re-deferred work a no-op; the throttle only bounds connection churn.
#   * BREAKER — after GROWTH_QUEUE_BREAKER_FAILURES consecutive connect failures
#     (e.g. the whole stack is down, not just the brain), the recorder backs off
#     for GROWTH_QUEUE_BREAKER_S seconds. Fail-safe direction is SKIP (= today's
#     unrecorded behaviour), never raise.
_RECORD_MIN_INTERVAL_S = 55.0
_BREAKER_FAILURES = 2
_BREAKER_BACKOFF_S = 300.0
# Throttle-map cardinality cap (see record_recall_miss): keys are per distinct
# missed chunk on the recall-miss path, so the map needs a hard bound on a
# long-lived process. At >4096 entries the OLDEST 1024 (by insertion order)
# are dropped — early expiry degrades to one extra connect, never a wrong
# write.
_LAST_RECORD_MAX_ENTRIES = 4096
_LAST_RECORD_KEEP_ENTRIES = 1024
_last_record: dict = {}
_connect_failures = 0
_breaker_until = 0.0


def _recorder_hygiene_state():
    """Test seam: the throttle/breaker dicts are module state — reset between tests."""
    return _last_record, _connect_failures, _breaker_until


def reset_recorder_hygiene():
    global _connect_failures, _breaker_until
    _last_record.clear()
    _connect_failures = 0
    _breaker_until = 0.0


def _resolve_operation(subsystem_or_operation: str) -> str | None:
    """Subsystem name → operation family; a bare operation name passes through."""
    if not subsystem_or_operation:
        return None
    if subsystem_or_operation in SUBSYSTEM_OPERATION:
        return SUBSYSTEM_OPERATION[subsystem_or_operation]
    if subsystem_or_operation in OPERATION_TYPES:
        return subsystem_or_operation
    return None


def record_deferred(schema_name, subsystem_or_operation, target_id=None,
                    user_id: str = "", conn=None, priority: int = None) -> bool:
    """Persist one deferred work item. NEVER RAISES (fail-safe contract).

    Args:
        schema_name: the tenant schema the work belongs to (search_path scope).
        subsystem_or_operation: the subsystem being deferred (mapped through
            SUBSYSTEM_OPERATION) or a bare operation family name. Unknown keys
            no-op — only growth-loop deferral points are recorded.
        target_id: WHAT was deferred — the subsystem name at a lane-gate
            deferral (the deferred unit is that subsystem's run), or the staged
            row id for a Class-C promotion deferral.
        conn: caller-supplied connection (its search_path is already bound);
            when None a short-lived connection is opened from POSTGRES_DSN.

    Returns True when a PENDING row for this (operation, target) exists on exit.
    """
    global _connect_failures, _breaker_until
    operation = _resolve_operation(subsystem_or_operation)
    if not operation or not schema_name:
        return False
    if target_id is None:
        target_id = subsystem_or_operation if subsystem_or_operation in SUBSYSTEM_OPERATION else ""
    if priority is None:
        priority = OPERATION_PRIORITY.get(operation, 5)

    own_conn = conn is None
    if own_conn:
        key = (str(schema_name), operation, str(target_id))
        now = time.monotonic()
        if now < _breaker_until:
            return False
        if _last_record.get(key, 0.0) and (now - _last_record.get(key, 0.0)) < _RECORD_MIN_INTERVAL_S:
            return False
        dsn = os.environ.get("POSTGRES_DSN", "")
        if not dsn:
            return False
        _last_record[key] = now
        try:
            conn = psycopg2.connect(dsn, connect_timeout=3)
            with conn.cursor() as cur:
                # Bind the tenant schema (NO public — schema = scope, the house
                # isolation rule); a bare connection would resolve the INSERT to
                # public.growth_work_queue, which does not exist.
                cur.execute(f"SET search_path TO {schema_name}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query — schema from UUID-derived source with validation
            conn.commit()
        except Exception as e:  # noqa: BLE001 — fail-safe: never break the growth loop
            _connect_failures += 1
            if _connect_failures >= _BREAKER_FAILURES:
                _breaker_until = time.monotonic() + _BREAKER_BACKOFF_S
                _connect_failures = 0
                log.warning(
                    "growth_work_queue.recorder_breaker_open "
                    f"backoff_s={_BREAKER_BACKOFF_S:.0f} reason={type(e).__name__} "
                    "note=queue writes skip (fail-safe) while Postgres is unreachable; "
                    "the sweep ledger still keeps the work due"
                )
            else:
                log.debug(f"growth_work_queue.record_deferred_connect_failed schema={schema_name}: {e}")
            return False
        _connect_failures = 0

    try:
        with conn.cursor() as cur:
            # Partial-unique match: no duplicate PENDING work for the same target;
            # completed rows are exempt (a NEW deferral after completion re-arms).
            # COALESCE form follows the template's NULL-safe house style
            # (uq_artefacts_identity) — portable to every PG in the fleet.
            cur.execute(
                "INSERT INTO growth_work_queue (operation_type, target_id, priority) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (operation_type, COALESCE(target_id, '')) "
                "WHERE completed_at IS NULL DO NOTHING",
                (operation, str(target_id), priority),
            )
            # Bounded: at most GROWTH_QUEUE_MAX pending rows (shared cap rule).
            _evict_beyond_cap(cur)
            cur.execute(
                "SELECT 1 FROM growth_work_queue"
                " WHERE operation_type = %s AND target_id = %s"
                "   AND completed_at IS NULL",
                (operation, str(target_id)),
            )
            pending = cur.fetchone() is not None
        conn.commit()
        return pending
    except Exception as e:  # noqa: BLE001 — fail-safe: a queue failure never breaks
        try:                    # the growth loop; fall through to today's skip
            conn.rollback()
            if schema_name:
                with conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {schema_name}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query — schema from UUID-derived source with validation
        except Exception:  # noqa: BLE001 — hygiene best-effort; the caller's own
            pass            # except arm re-applies search_path if this also failed
        log.debug(
            "growth_work_queue.record_deferred_failed "
            f"schema={schema_name} operation={operation} target={target_id} "
            f"user={str(user_id)[:8]}: {e}"
        )
        return False
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def pending_count(conn) -> int:
    """Cheap probe used once per tenant per cycle: is there anything to drain?"""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM growth_work_queue WHERE completed_at IS NULL")
            row = cur.fetchone()
            return int(row[0]) if row else 0
    except Exception as e:  # noqa: BLE001 — observability must never break the loop
        log.debug(f"growth_work_queue.pending_count_failed: {e}")
        return 0


def mark_completed(conn, item_ids) -> int:
    """Stamp completed_at on the given queue ids (idempotent; returns rows stamped)."""
    ids = [int(i) for i in item_ids if i is not None]
    if not ids:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE growth_work_queue SET completed_at = now() "
                "WHERE id = ANY(%s) AND completed_at IS NULL",
                (ids,),
            )
            n = cur.rowcount
        conn.commit()
        return n
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        log.debug(f"growth_work_queue.mark_completed_failed: {e}")
        return 0


def prune_completed(conn, older_than_days: int = None) -> int:
    """Completed rows are prunable — the partial unique only guards PENDING rows."""
    days = older_than_days if older_than_days is not None else _prune_days()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM growth_work_queue"
                " WHERE completed_at IS NOT NULL"
                "   AND completed_at < now() - (%s || ' days')::interval",
                (str(days),),
            )
            n = cur.rowcount
        conn.commit()
        return n
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        log.debug(f"growth_work_queue.prune_completed_failed: {e}")
        return 0


def drain(conn, runner, item_done=None, schema_name: str = "",
          lane_probe=None, on_attempted=None) -> dict:
    """Drain pending work PRIORITY-FIRST (1 highest), before the cycle's new work.

    Each pending item's deferred operation is re-attempted via ``runner(operation_type,
    target_id) -> bool`` (True = re-attempted cleanly). When the runner succeeds,
    ``item_done(operation_type, target_id) -> bool`` (optional) decides per-item
    completion — the default marks the item completed. A failed or raising runner
    leaves the item PENDING with a priority BACKOFF (+1, capped 9): rate-limited
    retry next cycle, and a deterministically-broken item cannot head-of-line-block
    the queue forever.

    AMPLIFICATION GUARDS (round-1 critic): (a) one re-attempt per DISTINCT deferred
    operation per drain — ten pending promotion rows cost ONE promotion pass, not
    ten (item_done still adjudicates every row); (b) ``lane_probe(operation_type)``
    (optional, BUG-08: the probe is consulted PER OPERATION TYPE) — if the lane the
    operation needs is closed (the brain flapped mid-drain for a brain-bearing op),
    the rest of the batch stays pending instead of burning fail-closed calls. A
    probe MUST NOT blanket-veto deterministic operations (see
    DETERMINISTIC_OPERATIONS) — the caller-side gate
    (embedder._growth_drain_lane_open) is the one translation layer and is pinned;
    (c) ``on_attempted(operation_type, target_id, ok)`` (optional) lets the caller
    record the ledger run for a subsystem it re-ran, so the normal cycle below does
    not immediately re-spend on the same input.

    The queue mechanics stay LLM-free: the runner is the growth loop's own
    bounded subsystem call, supplied by the caller. Single-poller by design (one
    re_embedder loop per box, the same assumption the sweep ledger rides) — no
    FOR UPDATE SKIP LOCKED is needed.

    Observability: one log line per drain cycle —
        growth_work_drain drained=N remaining=M by_type={...}
    (remaining=-1 means the census itself failed; the count is unknown, never zero.)
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, operation_type, target_id FROM growth_work_queue "
                "WHERE completed_at IS NULL "
                "ORDER BY priority ASC, created_at ASC, id ASC "
                "LIMIT %s",
                (_drain_batch(),),
            )
            items = [(int(r[0]), str(r[1]), (str(r[2]) if r[2] is not None else "")) for r in cur.fetchall()]
        # READ BARRIER: the claim SELECT just opened a read on the caller's shared
        # connection and the runner below blocks on the tenant brain — the exact
        # idle-in-transaction incident shape (see the invariant block in embedder's
        # per-tenant loop). The barrier is deliberately INSIDE drain so every caller
        # gets it, not just the growth loop.
        release_read_transaction(conn, context="growth_work_queue.drain.claim")
    except Exception as e:  # noqa: BLE001 — a drain failure never breaks the cycle
        try:
            conn.rollback()
            if schema_name:
                with conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {schema_name}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query — schema from UUID-derived source with validation
        except Exception:  # noqa: BLE001
            pass
        log.warning(
            f"growth_work_drain claim_failed schema={schema_name} "
            f"reason={type(e).__name__}: {str(e)[:120]}"
        )
        # remaining is UNKNOWN on a failed claim, never zero (the census-failure
        # rule); the warning above is the cycle's observability line.
        return {"drained": 0, "remaining": -1, "by_type": {}}

    drained = 0
    attempted: dict = {}   # distinct-op key -> ok verdict (ONE pass per operation)
    probe_closed: set = set()   # operation types whose lane probed closed this drain
    for item_id, operation, target in items:
        key = (operation, "promotion" if operation == "promotion" else target)
        if key in attempted:
            ok = attempted[key]
        elif operation in probe_closed:
            # BUG-08: this operation's lane probed closed on an EARLIER item this
            # drain — skip (stays pending, rate-limited retry next cycle) without
            # re-probing, and WITHOUT blocking the operations after it: a closed
            # brain lane must not head-of-line-block a deterministic operation the
            # way the old blanket `break` did.
            continue
        else:
            # Mid-drain lane probe: the brain can flap closed again — stop spending;
            # the un-attempted items keep their pending rows (rate-limited retry).
            # BUG-08: the probe receives the OPERATION TYPE so a deterministic
            # operation (DETERMINISTIC_OPERATIONS) is never blocked by a brain-lane
            # verdict it does not need. A closed verdict skips THIS operation's
            # items only; the drain continues to later operations.
            if lane_probe is not None:
                try:
                    lane_open = bool(lane_probe(operation))
                except Exception:  # noqa: BLE001 — a raising probe is a closed lane
                    lane_open = False
                if not lane_open:
                    probe_closed.add(operation)
                    log.info(
                        f"growth_work_drain lane_closed_mid_drain schema={schema_name} "
                        f"operation={operation} items stay pending "
                        "(rate-limited retry next cycle)"
                    )
                    continue
            # READ BARRIER before every re-attempt: an early-returning runner (e.g.
            # "nothing due") can leave its SELECT's read open on this shared
            # connection; the next operation must not inherit it across its LLM hop.
            release_read_transaction(conn, context="growth_work_queue.drain.reattempt")
            try:
                ok = bool(runner(operation, target))
            except Exception as e:  # noqa: BLE001 — a raising re-attempt is a retry, not a crash
                log.debug(f"growth_work_queue.drain_runner_error op={operation} target={target}: {e}")
                ok = False
            attempted[key] = ok
            if on_attempted is not None:
                try:
                    on_attempted(operation, target, ok)
                except Exception as e:  # noqa: BLE001 — telemetry must never break the drain
                    log.debug(f"growth_work_queue.drain_on_attempted_error: {e}")
        if not ok:
            # Priority backoff: a failing item sinks toward 9 so it cannot
            # head-of-line-block the drain forever (no schema change needed).
            _bump_priority(conn, item_id)
            continue
        done = True
        if item_done is not None:
            try:
                done = bool(item_done(operation, target))
            except Exception as e:  # noqa: BLE001
                log.debug(f"growth_work_queue.drain_item_done_error op={operation} target={target}: {e}")
                done = False
        if done:
            drained += mark_completed(conn, [item_id])

    remaining_by_type: dict = {}
    census_ok = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT operation_type, count(*) FROM growth_work_queue "
                "WHERE completed_at IS NULL GROUP BY operation_type"
            )
            for op, n in cur.fetchall():
                remaining_by_type[str(op)] = int(n)
    except Exception as e:  # noqa: BLE001 — the census is read-only; prune's commit (or
        census_ok = False   # the caller's own barrier) ends the read it opened
        try:
            conn.rollback()
            if schema_name:
                with conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {schema_name}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query — schema from UUID-derived source with validation
        except Exception:  # noqa: BLE001
            pass
        log.debug(f"growth_work_queue.drain_census_failed schema={schema_name}: {e}")

    pruned = prune_completed(conn)
    remaining = sum(remaining_by_type.values()) if census_ok else -1
    log.info(
        f"growth_work_drain drained={drained} remaining={remaining} "
        f"by_type={remaining_by_type} schema={schema_name}"
        + (f" pruned_completed={pruned}" if pruned else "")
        + ("" if census_ok else " census_error=true")
    )
    return {"drained": drained, "remaining": remaining, "by_type": remaining_by_type}


def _bump_priority(conn, item_id) -> None:
    """Failed-re-attempt backoff: sink toward priority 9 (lowest). Never raises."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE growth_work_queue SET priority = LEAST(9, priority + 1) "
                "WHERE id = %s AND completed_at IS NULL",
                (item_id,),
            )
        conn.commit()
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        log.debug(f"growth_work_queue.bump_priority_failed id={item_id}: {e}")
