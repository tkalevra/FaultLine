"""PER-SEAT SWEEP WORK LEDGER — a quiet seat must cost nothing.

THE PROBLEM, MEASURED ON LIVE BOXES 2026-08-01
──────────────────────────────────────────────
The re_embedder's per-tenant sweep fires every ``REEMBED_INTERVAL`` and re-does identical work
for EVERY ready seat regardless of whether that seat did anything:

* **STAGING**: 153 LLM calls started / 152 succeeded in 20 minutes — ~8 calls/min, sustained,
  indefinitely. Observed work: ``synonym_conv_type_reject`` (263), ``whatis_chain`` (50),
  ``cosine_map_suggestion_demoted`` (57).
* **PROD**: 12 calls, ALL FAILED, plus 101 ``llm_metadata_query_failed`` — the LLM endpoint
  sat on an address the box could not route to, and the sweep retried it every cycle, feeding
  the circuit breaker.
* The same subsystems hold a read transaction across those calls — the confirmed shape of the
  10h08m ``idle in transaction`` that jammed prod. Firing less is defence in depth for that too.

The cost is **O(seats) per interval, forever**, while the useful work is O(seats that changed).
That is compounding in the wrong direction: it grows with the business, the value does not.
On a metered LLM endpoint the LLM half is spent on the operator's bill — money, for nothing.

THE MECHANISM — A CHANGE TOKEN THE WRITER MUTATES, NOT A SCAN
─────────────────────────────────────────────────────────────
``public.sweep_work_state`` holds one row per (seat, subsystem): ``work_token``,
``last_run_token``, ``last_run_at``. **The path that CREATES work bumps the token.** The sweep
reads every seat's rows in ONE query per cycle and runs a subsystem only when:

    no row yet (never run)  OR  work_token <> last_run_token (dirty)
                            OR  last_run_at older than the MAX-RUN interval (time bound)

A quiet seat matches none of those: no tenant connection, no ``search_path`` bind, no probe, no
LLM call. Zero.

This is deliberately **not** a scan-and-compare fingerprint. Deriving "did the input change?"
by scanning each subsystem's input rows every cycle is itself per-cycle work that scales with
data volume — the same cost, relocated rather than removed. The writer already knows it created
work; it says so once, in the transaction that created it, for free.

**BOTH BOUNDS, AND WHY.** The hash provides the SKIP; time provides the MAX-RUN bound. The time
bound is the safety net for a writer we missed: it converts "this seat silently never sweeps"
— the one real failure mode of a dirty-flag design — into "this seat sweeps late", which is
survivable and observable. It also covers the genuinely clock-driven re-opens that no token can
represent (e.g. the climb's ``unplaceable`` backoff window).

WHO IS AUTHORITATIVE FOR WHAT
─────────────────────────────
* **PostgreSQL is authoritative.** The ledger row is the truth. It survives a deploy, a Redis
  flush, and a restart. Every decision is ultimately made from it.
* **Redis is an OPTIONAL read-through cache of the per-cycle snapshot only** (flag
  ``REEMBED_SWEEP_SNAPSHOT_CACHE``, default OFF). It never holds a decision, is never written
  by the mark-dirty path, and is never consulted for correctness. Absent, slow, flushed or
  erroring → the snapshot comes from Postgres and behaviour is identical. It exists so that at
  seat scale, with more than one sweep process, the snapshot read never becomes the bottleneck.
  Its staleness is bounded below one interval, so at worst a newly-dirty seat waits one cycle.

INVARIANTS (each has a test)
────────────────────────────
1. **FAIL-SAFE DIRECTION IS "RUN".** Flag off, table missing, query failed, unknown subsystem,
   unparseable seat, no row → RUN. Skipping needed work is a correctness bug; running unneeded
   work is only waste. Never inverted.
2. **RECORD ONLY AFTER SUCCESS**, and record the token OBSERVED AT CLAIM. A write landing
   mid-run bumps ``work_token`` past the observed value, so it is dirty again the instant the
   run ends. An exception means ``record_run`` is never reached and the next cycle retries.
3. **THE MAX-RUN BOUND ALWAYS WINS.** No amount of "clean" can suppress a run past
   ``REEMBED_SWEEP_MAX_INTERVAL``.
4. **EXCLUSIONS ARE UNCONDITIONAL.** A subsystem that legitimately progresses on identical
   input (decay, retry queue, age-floored backfill) is named in ``EXCLUDED`` and ``claim()``
   returns True for it even with the flag on.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Iterable, Optional

try:  # pragma: no cover - import guard
    import structlog
    log = structlog.get_logger()
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger("sweep_ledger")

from src.api import redis_coord as _rc


_TABLE = "public.sweep_work_state"


# ──────────────────────────────────────────────────────────────────────────────
# FLAGS — OFF by default. Flag-OFF is byte-for-byte the legacy sweep.
# ──────────────────────────────────────────────────────────────────────────────
def _truthy(v: Optional[str]) -> bool:
    return str(v or "").strip().lower() not in ("", "false", "0", "no", "off")


def enabled() -> bool:
    """Master lever ``REEMBED_SWEEP_SKIP``. OFF (default) → ``claim()`` always returns True,
    ``snapshot()`` returns the always-run sentinel, and nothing is read or written."""
    return _truthy(os.environ.get("REEMBED_SWEEP_SKIP", "false"))


def marking_enabled() -> bool:
    """Whether WRITERS bump tokens. Independently gated (``REEMBED_SWEEP_MARK``, default ON)
    so the ledger can be populated and observed for a while BEFORE any skipping is switched on
    — turn marking on first, watch the tokens move, then turn skipping on. Marking with
    skipping off is harmless: it costs one statement inside a transaction that is already open.
    """
    return _truthy(os.environ.get("REEMBED_SWEEP_MARK", "true"))


def max_interval_seconds() -> int:
    """MAX-RUN bound: run a subsystem at least this often even when its token is unchanged.

    Default 3600s. Rationale, stated rather than assumed: at the production
    ``REEMBED_INTERVAL=300`` this makes a perfectly quiet seat run 1 cycle in 12 instead of
    12 in 12 — a 91.7% reduction in the WORST case (a seat with no writer coverage at all) and
    100% between ticks. Shorter would erode the saving for little safety; much longer would
    stretch "sweeps late" past the point where an operator would notice a missed writer.
    """
    try:
        return max(1, int(os.environ.get("REEMBED_SWEEP_MAX_INTERVAL", "3600") or "3600"))
    except (TypeError, ValueError):
        return 3600


def _jitter_fraction() -> float:
    """Spread of the deterministic per-(seat, subsystem) jitter applied to the max-run bound.

    WITHOUT this, every seat's max-run timer is anchored to the same deploy and 5,000 seats
    wake in the same cycle — a self-inflicted thundering herd against one shared brain, which
    is precisely the over-subscription that opens the breaker for everyone. The jitter is
    DETERMINISTIC (hash of seat+subsystem), so a seat's slot is stable across restarts.
    """
    try:
        return min(0.9, max(0.0, float(os.environ.get("REEMBED_SWEEP_MAX_JITTER", "0.25"))))
    except (TypeError, ValueError):
        return 0.25


def _effective_max_interval(user_id: Any, subsystem: str) -> int:
    base = max_interval_seconds()
    frac = _jitter_fraction()
    if frac <= 0:
        return base
    h = hashlib.sha256(f"{user_id}|{subsystem}".encode("utf-8", "replace")).digest()
    # h[0]/255 ∈ [0,1] → offset ∈ [-frac, +frac]
    offset = ((h[0] / 255.0) * 2.0 - 1.0) * frac
    return max(1, int(base * (1.0 + offset)))


def snapshot_cache_enabled() -> bool:
    """Optional Redis read-through cache of the per-cycle snapshot. Default OFF — one small
    query per cycle against a table proportional to live seats is not a bottleneck today, and
    an unnecessary cache is an unnecessary way to be wrong. It exists for the seat-scale /
    multi-process case, and for that case only."""
    return _truthy(os.environ.get("REEMBED_SWEEP_SNAPSHOT_CACHE", "false")) and _rc.enabled()


# ──────────────────────────────────────────────────────────────────────────────
# THE SUBSYSTEM REGISTRY — what each one reads, hence which writers must mark it
# ──────────────────────────────────────────────────────────────────────────────
# `tables` is the set of tables whose CONTENT that subsystem's decisions depend on. It is the
# routing key for `mark_dirty_for_tables`: a writer states which tables it touched and the fan
# out is derived, so a writer never has to know the subsystem list. `audit_sql` is the
# subsystem's real input predicate — NOT used on the hot path; it is the MISSED-WRITER DETECTOR
# (see `audit_seat`), which is how the one real risk of a dirty-flag design is made observable
# instead of silent.

SUBSYSTEMS: dict[str, dict] = {

    # ── evaluate_ontology_candidates (LLM on approval) ─────────────────────────
    "ontology_eval": {
        "llm": True,
        "tables": ("ontology_evaluations", "rel_types"),
        "why": "undecided ontology_evaluations rows excluding the three carve-out markers; "
               "rel_types too, because a new rel flips a candidate from novel-mint to "
               "already-known-suppress",
        "audit_sql": """
            SELECT (SELECT count(*) FROM ontology_evaluations
                     WHERE re_embedder_decision IS NULL
                       AND extraction_method IS DISTINCT FROM 'ingest_miss_pushback'
                       AND extraction_method IS DISTINCT FROM 'linguistic_cue_candidate'
                       AND extraction_method IS DISTINCT FROM 'aspect_synonym_miss'),
                   (SELECT coalesce(sum(occurrence_count),0) FROM ontology_evaluations
                     WHERE re_embedder_decision IS NULL
                       AND extraction_method IS DISTINCT FROM 'ingest_miss_pushback'
                       AND extraction_method IS DISTINCT FROM 'linguistic_cue_candidate'
                       AND extraction_method IS DISTINCT FROM 'aspect_synonym_miss'),
                   (SELECT count(*) FROM rel_types)
        """,
    },

    # ── evaluate_aspect_synonym_candidates (LLM) ───────────────────────────────
    "aspect_synonym": {
        "llm": True,
        "tables": ("ontology_evaluations", "rel_type_aliases"),
        "why": "queued aspect misses (extraction_method='aspect_synonym_miss'); rel_type_aliases "
               "because an alias grown by any other lane resolves a queued row with NO LLM call",
        "audit_sql": """
            SELECT (SELECT count(*) FROM ontology_evaluations
                     WHERE extraction_method = 'aspect_synonym_miss'
                       AND re_embedder_decision IS NULL),
                   (SELECT count(*) FROM rel_type_aliases)
        """,
    },

    # ── drain_pending_placement_by_morphology (deterministic, no LLM) ──────────
    "pending_placement_drain": {
        "llm": False,
        "tables": ("rel_types", "rel_type_aliases", "entity_taxonomies"),
        "why": "pending_placement rels AND the seeded canonical universe they fold against — a "
               "newly seeded canonical is new input even when the pending set is unchanged",
        "audit_sql": """
            SELECT (SELECT count(*) FROM rel_types WHERE category = 'pending_placement'),
                   (SELECT count(*) FROM rel_types),
                   (SELECT count(*) FROM rel_type_aliases),
                   (SELECT count(*) FROM entity_taxonomies)
        """,
    },

    # ── converge_lifted_synonyms (LLM at Gate 2) ───────────────────────────────
    "synonym_convergence": {
        "llm": True,
        "tables": ("rel_types", "rel_type_aliases", "entity_taxonomies",
                   "ontology_evaluations", "facts", "staged_facts"),
        "why": "novel + seeded rel universes, the LLM memo ledger, AND the live facts Gate 1 "
               "resolves observed head/tail types from — a supersede is an UPDATE, so facts "
               "must be marked on retraction too, not only on insert",
        "audit_sql": """
            SELECT (SELECT count(*) FROM rel_types),
                   (SELECT count(*) FROM ontology_evaluations
                     WHERE extraction_method = 'synonym_convergence'),
                   (SELECT count(*) FROM facts
                     WHERE superseded_at IS NULL AND archived_at IS NULL),
                   (SELECT count(*) FROM staged_facts WHERE promoted_at IS NULL)
        """,
    },

    # ── classify_unknown_concepts — "what is X?" (LLM per concept) ─────────────
    "whatis_classify": {
        "llm": True,
        "tables": ("ontology_evaluations", "facts", "staged_facts", "climb_state"),
        "why": "queued ingest_miss_pushback concepts, plus the per-tenant ONTOLOGY VERSION "
               "(distinct backbone parents) that _concept_fingerprint uses to re-open a cached "
               "'unplaceable' — so hierarchy growth must mark it, not just new concepts",
        "audit_sql": """
            SELECT (SELECT count(*) FROM ontology_evaluations
                     WHERE extraction_method = 'ingest_miss_pushback'
                       AND re_embedder_decision IS NULL
                       AND sample_object IS NOT NULL AND sample_object <> ''),
                   (SELECT count(DISTINCT object_id) FROM facts
                     WHERE rel_type = ANY(%(hier)s)
                       AND superseded_at IS NULL AND archived_at IS NULL)
                 + (SELECT count(DISTINCT object_id) FROM staged_facts
                     WHERE rel_type = ANY(%(hier)s) AND promoted_at IS NULL)
        """,
    },

    # ── climb_classification_chains — ±6 climb + splice (LLM per rung) ─────────
    "classify_climb": {
        "llm": True,
        "tables": ("facts", "staged_facts", "climb_state"),
        "why": "the hierarchy edge set it walks (edges, distinct leaves, distinct parents) plus "
               "the climb_state cache. NOTE the deliberate gap: climb_state's BACKOFF WINDOW "
               "re-opens an under-cap 'unplaceable' purely by the CLOCK. No token can represent "
               "that; it is covered by the max-run bound, which is one of the reasons that bound "
               "exists",
        "audit_sql": """
            SELECT (SELECT count(*) FROM facts
                     WHERE rel_type = ANY(%(hier)s)
                       AND superseded_at IS NULL AND archived_at IS NULL),
                   (SELECT count(*) FROM staged_facts
                     WHERE rel_type = ANY(%(hier)s) AND promoted_at IS NULL),
                   (SELECT count(*) FROM climb_state)
        """,
    },

    # ── converge_hierarchy_by_identity — rung 6 (deterministic, no LLM) ────────
    "rung6_convergence": {
        "llm": False,
        "tables": ("facts", "staged_facts", "entity_aliases"),
        "why": "hierarchy parent nodes, plus entity_aliases — the canonical name it groups BY "
               "comes from there, so a new alias is new input even with no new edge",
        "audit_sql": """
            SELECT (SELECT count(DISTINCT object_id) FROM facts
                     WHERE rel_type = ANY(%(hier)s) AND superseded_at IS NULL),
                   (SELECT count(DISTINCT object_id) FROM staged_facts
                     WHERE rel_type = ANY(%(hier)s) AND promoted_at IS NULL),
                   (SELECT count(*) FROM entity_aliases)
        """,
    },

    # ── grow_linguistic_cue_candidates (deterministic, no LLM) ────────────────
    "cue_class_growth": {
        "llm": False,
        "tables": ("ontology_evaluations", "linguistic_cues"),
        "why": "carved cue candidates AND their occurrence sum — the >=3 freq gate can flip on a "
               "counter bump with no new row, so a re-sighting must mark it",
        "audit_sql": """
            SELECT (SELECT count(*) FROM ontology_evaluations
                     WHERE extraction_method = 'linguistic_cue_candidate'
                       AND re_embedder_decision IS NULL),
                   (SELECT coalesce(sum(occurrence_count),0) FROM ontology_evaluations
                     WHERE extraction_method = 'linguistic_cue_candidate'
                       AND re_embedder_decision IS NULL),
                   (SELECT count(*) FROM linguistic_cues)
        """,
    },

    # ── evaluate_correction_signal_candidates ─────────────────────────────────
    "correction_eval": {
        "llm": False,
        "tables": ("correction_signal_evaluations", "correction_signals", "correction_patterns"),
        "why": "undecided candidates + occurrence sum (>=3 gate), plus both growth targets so an "
               "out-of-band insert re-opens the growth→firing bridge",
        "audit_sql": """
            SELECT (SELECT count(*) FROM correction_signal_evaluations
                     WHERE re_embedder_decision IS NULL),
                   (SELECT coalesce(sum(occurrence_count),0) FROM correction_signal_evaluations
                     WHERE re_embedder_decision IS NULL),
                   (SELECT count(*) FROM correction_signals)
        """,
    },

    # ── retroactive head_types/tail_types sweep (LLM PER ROW, LIMIT 10/cycle) ──
    # The single biggest measured waste: when the brain cannot answer, the rows stay NULL and
    # the IDENTICAL 10-row batch is re-sent every cycle forever. This is the prod shape — 12
    # calls, all failing, every cycle, against an unroutable endpoint.
    "head_tail_sweep": {
        "llm": True,
        "tables": ("rel_types",),
        "why": "rel_types rows with NULL/empty head_types or tail_types — its entire input is "
               "one table, so its writer set is exactly 'whoever mints or edits a rel_type'",
        "audit_sql": """
            SELECT (SELECT count(*) FROM rel_types
                     WHERE (head_types IS NULL OR head_types = ARRAY[]::TEXT[]
                            OR tail_types IS NULL OR tail_types = ARRAY[]::TEXT[])),
                   (SELECT count(*) FROM rel_types)
        """,
    },

    # ── async taxonomy discovery (LLM per novel rel) ───────────────────────────
    "taxonomy_discovery": {
        "llm": True,
        "tables": ("staged_facts", "rel_types", "entity_taxonomies"),
        "why": "the staged_facts↔rel_types anti-join that drives it, plus both sides of that "
               "anti-join so either changing re-opens it",
        "audit_sql": """
            SELECT (SELECT count(DISTINCT rel_type) FROM staged_facts
                     WHERE rel_type NOT IN (SELECT rel_type FROM rel_types)),
                   (SELECT count(*) FROM entity_taxonomies),
                   (SELECT count(*) FROM rel_types)
        """,
    },

    # ── evaluate_retraction_outcomes ──────────────────────────────────────────
    "retraction_outcomes": {
        "llm": False,
        "tables": ("retraction_outcomes", "retraction_signals", "negation_patterns"),
        "why": "the feedback rows its own has_pending_* guard probes, plus the signal/pattern "
               "tables it grows",
        "audit_sql": """
            SELECT (SELECT count(*) FROM retraction_outcomes WHERE was_correct IS NOT NULL),
                   (SELECT count(*) FROM retraction_signals)
        """,
    },

    # ── resolve_name_conflicts (LLM arbitration) ──────────────────────────────
    "name_conflicts": {
        "llm": True,
        "tables": ("entity_name_conflicts", "entity_aliases"),
        "why": "pending conflicts, plus entity_aliases — arbitration READS the aliases, so a new "
               "alias is new evidence for an already-pending conflict row",
        "audit_sql": """
            SELECT (SELECT count(*) FROM entity_name_conflicts WHERE status = 'pending'),
                   (SELECT count(*) FROM entity_aliases)
        """,
    },

    # ── flag_suspect_preferred_names (no LLM — but the LOG-VOLUME offender) ────
    # Prod emitted 5,104 `re_embedder.suspect_preferred_name` WARNING lines in 20 minutes
    # (~370k/day): one line PER SUSPECT PER SEAT PER CYCLE, re-emitting an identical census
    # nothing consumes. The local primary tenant alone carries 282 suspects. The pass is
    # flag-ONLY and its output is a pure function of its input, so a clean token means the
    # identical block of lines. Guarding it is the largest single log-volume reduction here.
    "suspect_preferred_names": {
        "llm": False,
        "tables": ("entity_aliases",),
        "why": "the suspect census itself — flag-only pass, output is a pure function of this set",
        "audit_sql": """
            SELECT (SELECT count(*) FROM entity_aliases
                     WHERE is_preferred = true
                       AND preference_source IN
                           ('inferred','lexical','provisioned','merge','unspecified')),
                   (SELECT count(*) FROM entity_aliases)
        """,
    },

    # ── evaluate_extraction_patterns ──────────────────────────────────────────
    "extraction_pattern_eval": {
        "llm": False,
        "tables": ("extraction_patterns", "extraction_pattern_matches"),
        "why": "its own feedback-bearing precheck plus the feedback TOTAL — every decision it "
               "makes (archive/promote/confidence) is a function of those counters",
        "audit_sql": """
            SELECT (SELECT count(*) FROM extraction_patterns
                     WHERE is_active = true
                       AND (coalesce(confirmed_count,0) > 0
                            OR coalesce(rejected_count,0) > 0
                            OR coalesce(correction_count,0) > 0)),
                   (SELECT coalesce(sum(coalesce(confirmed_count,0)
                                      + coalesce(rejected_count,0)
                                      + coalesce(correction_count,0)),0)
                      FROM extraction_patterns WHERE is_active = true)
        """,
    },

    # ── Job 7a orphan rel_type stub mint + Job 7 natural_language FILL ────────
    # LLM per row (generate_rel_type_phrasing), LIMIT 5/seat/cycle. Same failure shape as the
    # head/tail sweep: a rel the brain will not phrase stays NULL and is re-sent every cycle.
    "orphan_stub_and_nl_fill": {
        "llm": True,
        "tables": ("rel_types", "facts", "staged_facts"),
        "why": "the un-phrased rel_types rows AND the facts/staged_facts anti-join that mints "
               "into them — stub-then-fill share a connection and a cycle, so one token",
        "audit_sql": """
            SELECT (SELECT count(*) FROM rel_types
                     WHERE natural_language IS NULL OR natural_language = ''
                        OR natural_language_2p IS NULL OR natural_language_2p = ''),
                   (SELECT count(*) FROM (
                        SELECT DISTINCT lower(rel_type) AS rel_type FROM facts
                         WHERE rel_type IS NOT NULL
                        UNION
                        SELECT DISTINCT lower(rel_type) FROM staged_facts
                         WHERE rel_type IS NOT NULL
                    ) used
                    LEFT JOIN rel_types rt ON rt.rel_type = used.rel_type
                    WHERE rt.rel_type IS NULL AND used.rel_type <> 'context')
        """,
    },
}

_HIERARCHY_RELS = ["instance_of", "is_a", "subclass_of", "part_of", "member_of"]


# ──────────────────────────────────────────────────────────────────────────────
# EXCLUDED — subsystems that legitimately PROGRESS on identical input
# ──────────────────────────────────────────────────────────────────────────────
# `claim()` returns True for every name here UNCONDITIONALLY, even with the flag on. Each is a
# pass whose trigger is the CLOCK or an external store, not a row set. A dirty flag would be
# permanently clean while real work piled up behind it.
EXCLUDED: dict[str, str] = {
    "decay_ontology_candidates":
        "TIME-BASED: `last_seen_at <= now() - interval '30 days'`. The rows do not change; the "
        "CLOCK crosses them. No writer exists to mark it dirty, because nothing is written.",
    "expire_staged_facts":
        "TIME-BASED (`expires_at <= now()`) plus counter decay. Same shape, and already gated by "
        "_tenant_has_reembed_work probe 4.",
    "decay_class_c_hits":
        "TIME-BASED hit-count decay on an elapsed 30-day window. Identical rows become due.",
    "promote_staged_facts":
        "COUNTER-DRIVEN and already event-gated (confirmed_count >= 3, probe 2). A missed "
        "promotion is a USER-VISIBLE recall miss — never put a marker in front of it.",
    "promote_class_c_hits":
        "As above (hit_count >= 3, probe 3). Recall-visible.",
    "reextract_episodic":
        "AGE-FLOORED RETRY QUEUE: `created_at < now() - interval '1 hour'`. A row ineligible last "
        "cycle becomes eligible with no input change whatsoever.",
    "drain_pending_documents":
        "CLAIM/RETRY QUEUE with a lease. A chunk that failed or timed out MUST be re-attempted on "
        "identical rows — that is the entire lane.",
    "fast_drain_pending_documents_prepass":
        "Same lane, run earlier for latency. Never gate a user's document behind a token.",
    "intent_pattern_cache_eviction":
        "TIME-BASED (`expires_at < now()`), pure SQL, no LLM. Nothing to save.",
    "reconcile_qdrant":
        "Converges against an EXTERNAL store whose divergence appears in NO Postgres row, so no "
        "Postgres token can represent its input. Already cadence-gated in main().",
    "gate_adjustment":
        "Pure SQL over intent_confidence_feedback, no LLM, one small aggregate per seat. A marker "
        "round-trip would cost more than it saves and add a way to be wrong.",
    "job7_pattern_promotion":
        "One bounded UPDATE per schema, no LLM. As above.",
    "qdrant_sync":
        "Already event-driven on `qdrant_synced = false` — the row set IS the queue and it drains "
        "as it is consumed. No LLM. A token could only add a way to lose a sync.",
}


# ──────────────────────────────────────────────────────────────────────────────
# TABLE → SUBSYSTEM FAN-OUT
# ──────────────────────────────────────────────────────────────────────────────
# Derived from SUBSYSTEMS so the two can never drift. A writer states which TABLES it touched;
# it never needs to know which subsystems consume them. That is what keeps the writer set
# auditable: "did I mark every table I wrote?" is a question you can answer by reading one
# function, where "did I mark every subsystem?" is not.
def _build_table_map() -> dict[str, tuple]:
    m: dict[str, set] = {}
    for name, spec in SUBSYSTEMS.items():
        for t in spec["tables"]:
            m.setdefault(t, set()).add(name)
    return {t: tuple(sorted(v)) for t, v in m.items()}


TABLE_SUBSYSTEMS: dict[str, tuple] = _build_table_map()


def subsystems_for_tables(*tables: str) -> tuple:
    """Which subsystems must be marked dirty when these tables are written."""
    out: set = set()
    for t in tables:
        out.update(TABLE_SUBSYSTEMS.get(str(t), ()))
    return tuple(sorted(out))


# Convenience bundles for the real write chokepoints, so a call site reads as intent rather
# than as a table list. Each is deliberately COARSE: coarse-but-complete beats fine-but-lossy,
# because the failure mode of "too fine" is a seat that silently never sweeps.
INGEST_TABLES = (
    "facts", "staged_facts", "entities", "entity_aliases", "rel_types",
    "rel_type_aliases", "entity_taxonomies", "ontology_evaluations",
    "entity_name_conflicts", "linguistic_cues", "climb_state",
)
QUERY_TABLES = ("ontology_evaluations", "rel_type_aliases", "extraction_pattern_matches",
                "extraction_patterns", "staged_facts")
RETRACT_TABLES = ("facts", "staged_facts", "retraction_outcomes", "retraction_signals",
                  "negation_patterns", "correction_signal_evaluations", "correction_signals",
                  "correction_patterns")


# ──────────────────────────────────────────────────────────────────────────────
# MARK DIRTY — the WRITER half
# ──────────────────────────────────────────────────────────────────────────────
# COST, stated plainly and measured below in the proof:
#   * ONE statement — a multi-row upsert via `unnest`, not one statement per subsystem.
#   * Written schema-QUALIFIED (`public.sweep_work_state`), so it does not depend on — and does
#     not disturb — the caller's tenant `search_path`, which deliberately excludes `public`.
#   * On the endpoint anchors it runs POST-COMMIT on its own short-lived autocommit connection.
#     That ordering is deliberate: it CANNOT fail the user's write, cannot poison the caller's
#     transaction, and cannot leave an idle-in-transaction backend. If the process dies between
#     the commit and the mark we UNDER-mark, and the max-run bound catches it — the safe
#     direction. (Marking inside the transaction would under-mark on rollback: same direction,
#     more coupling.)
#   * On anchors that already hold an open connection and commit immediately after, the
#     `_conn` variants ride that connection and add no round-trip at all.
#
# CONTENTION: rows are keyed by user_id, so two seats NEVER contend. Two concurrent writes for
# the same seat contend on ~14 narrow rows for microseconds.
#
# THE TOKEN IS ALWAYS BUMPED, never conditionally skipped when the row is already dirty. A
# conditional bump looks like a free optimisation and is a correctness bug: it would swallow a
# write that lands DURING a sweep run, because the run then records `last_run_token` equal to
# the token that write never advanced past.

_UPSERT_SQL = f"""
INSERT INTO {_TABLE} (user_id, subsystem, work_token, updated_at)
SELECT %s::uuid, s, 1, now() FROM unnest(%s::text[]) AS s
ON CONFLICT (user_id, subsystem) DO UPDATE
   SET work_token = sweep_work_state.work_token + 1,
       updated_at = now()
"""

# Same upsert, but the seat is resolved from its SCHEMA NAME inline. Two real writer sites
# (`_aspect_record_miss`, `EntityRegistry.register_alias`) hold only a schema_name — the class
# and the helper were both written without a user_id — and threading one through would be a
# wider, riskier change than a join the control-plane table can do for free.
_UPSERT_BY_SCHEMA_SQL = f"""
INSERT INTO {_TABLE} (user_id, subsystem, work_token, updated_at)
SELECT up.user_id, s, 1, now()
  FROM public.user_provisioning up
  CROSS JOIN unnest(%s::text[]) AS s
 WHERE up.schema_name = %s
ON CONFLICT (user_id, subsystem) DO UPDATE
   SET work_token = sweep_work_state.work_token + 1,
       updated_at = now()
"""


def _names(subsystems: Iterable[str]) -> list:
    return sorted({s for s in subsystems if s in SUBSYSTEMS})


def mark_dirty_conn(conn: Any, user_id: Any, *tables: str) -> bool:
    """Mark on a connection the caller ALREADY holds. Does NOT commit — the caller's own commit
    carries it, so the mark is transactional with the write it describes.

    Never raises. On failure the caller's transaction is left INERROR by Postgres, which is why
    every call site for this variant is one that commits (or rolls back) immediately after and
    already handles that; sites without that property use ``mark_dirty_dsn`` instead.
    """
    if not marking_enabled():
        return False
    names = _names(subsystems_for_tables(*tables))
    if not names or user_id is None or conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(_UPSERT_SQL, (str(user_id), names))  # nosemgrep: python.lang.security.audit.sqli.psycopg-sqli.psycopg-sqli — _UPSERT_SQL is a module-constant; params are user_id (UUID) and string array
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("sweep_ledger.mark_failed "
                    f"user={str(user_id)[:8]} tables={','.join(tables)} "
                    f"error={type(e).__name__}: {str(e)[:160]} "
                    "(seat still sweeps — the max-run bound covers a missed mark)")
        return False


def mark_dirty_schema_conn(conn: Any, schema_name: str, *tables: str) -> bool:
    """As above, for a writer that holds a schema_name but no user_id. Resolves the seat via
    ``public.user_provisioning`` in the same statement — no extra round-trip, no lookup cache,
    and no user_id threaded through a constructor that never had one."""
    if not marking_enabled():
        return False
    names = _names(subsystems_for_tables(*tables))
    if not names or not schema_name or conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(_UPSERT_BY_SCHEMA_SQL, (names, str(schema_name)))  # nosemgrep: python.lang.security.audit.sqli.psycopg-sqli.psycopg-sqli — schema validated by _SAFE_SCHEMA_RE; names from tenant provisioning
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("sweep_ledger.mark_by_schema_failed "
                    f"schema={schema_name} error={type(e).__name__}: {str(e)[:160]}")
        return False


def mark_dirty_dsn(dsn: str, user_id: Any, *tables: str) -> bool:
    """POST-COMMIT mark on its own short-lived AUTOCOMMIT connection.

    The endpoint-anchor variant. Autocommit so no transaction is ever open (this codebase has
    paid 10 hours for a connection that held one), and closed in a ``finally`` so nothing is
    stranded. Cannot fail the user's request: every failure is logged and swallowed.
    """
    if not marking_enabled():
        return False
    names = _names(subsystems_for_tables(*tables))
    if not names or user_id is None or not dsn:
        return False
    import psycopg2
    conn = None
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(_UPSERT_SQL, (str(user_id), names))  # nosemgrep: python.lang.security.audit.sqli.psycopg-sqli.psycopg-sqli — _UPSERT_SQL is a module-constant; params are user_id (UUID) and string array
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("sweep_ledger.mark_dsn_failed "
                    f"user={str(user_id)[:8]} error={type(e).__name__}: {str(e)[:160]} "
                    "(seat still sweeps — the max-run bound covers a missed mark)")
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # pragma: no cover
                pass


# Back-compat alias for the subsystem-name form, used by the re_embedder's own cross-subsystem
# marking (where the caller genuinely knows the subsystem, not the table).
def mark_dirty_subsystems_conn(conn: Any, user_id: Any, *subsystems: str) -> bool:
    if not marking_enabled():
        return False
    names = _names(subsystems)
    if not names or user_id is None or conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(_UPSERT_SQL, (str(user_id), names))  # nosemgrep: python.lang.security.audit.sqli.psycopg-sqli.psycopg-sqli — _UPSERT_SQL is a module-constant; params are user_id (UUID) and string array
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("sweep_ledger.mark_subsystems_failed "
                    f"user={str(user_id)[:8]} error={type(e).__name__}: {str(e)[:160]}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# SNAPSHOT + CLAIM — the SWEEP half
# ──────────────────────────────────────────────────────────────────────────────
# ONE admin query per cycle answers "what is due, for every seat". That is what lets a quiet
# seat cost NOTHING: the sweep never opens its tenant connection, never binds its search_path,
# never spins an embedder, never touches its brain.

class Snapshot:
    """One cycle's view of the ledger. ``due`` maps str(user_id) → {subsystem: observed_token}.

    ``always_run`` is the fail-safe sentinel: flag off, table missing, query failed. In that
    state every ``claim()`` returns True and the sweep behaves exactly as it does today. It is
    a distinct state from "an empty ledger" on purpose — an empty ledger means every seat is
    due for its FIRST run, which is also True, but for a reason worth logging differently.
    """

    __slots__ = ("due", "always_run", "reason", "seats_seen", "seats_due", "taken_at")

    def __init__(self, due: dict, always_run: bool = False, reason: str = "",
                 seats_seen: int = 0) -> None:
        self.due = due
        self.always_run = always_run
        self.reason = reason
        self.seats_seen = seats_seen
        self.seats_due = len(due)
        self.taken_at = time.time()

    def observed_token(self, user_id: Any, subsystem: str) -> Optional[int]:
        return (self.due.get(str(user_id)) or {}).get(subsystem)

    def is_due(self, user_id: Any, subsystem: str) -> bool:
        if self.always_run:
            return True
        return subsystem in (self.due.get(str(user_id)) or {})

    def seat_has_work(self, user_id: Any) -> bool:
        """True when ANY fingerprinted subsystem is due for this seat. The gate that lets the
        sweep skip a quiet seat WITHOUT opening its connection."""
        if self.always_run:
            return True
        return bool(self.due.get(str(user_id)))


_ALWAYS_RUN_DISABLED = Snapshot({}, always_run=True, reason="flag_off")


def snapshot(dsn: str, user_ids: Iterable[Any]) -> Snapshot:
    """Read the whole ledger for these seats in ONE query and compute what is due.

    Due when: no row (never run) OR work_token <> last_run_token (dirty) OR last_run_at older
    than the jittered max-run interval. Any failure → the always-run sentinel (invariant 1).

    The connection is opened and CLOSED here; nothing is left holding a read transaction.
    """
    if not enabled():
        return _ALWAYS_RUN_DISABLED

    ids = [str(u) for u in user_ids if u]
    if not ids:
        return Snapshot({}, reason="no_seats", seats_seen=0)

    cached = _snapshot_cache_read(ids)
    if cached is not None:
        return cached

    import psycopg2  # local import: this module must be importable without a live DB driver
    rows = []
    try:
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT user_id::text, subsystem, work_token, last_run_token,"
                    f"       EXTRACT(EPOCH FROM (now() - last_run_at))"
                    f"  FROM {_TABLE} WHERE user_id = ANY(%s::uuid[])",
                    (ids,),
                )
                rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001 — table missing / DB blip → RUN EVERYTHING
        log.warning(
            "re_embedder.sweep_ledger.snapshot_failed "
            f"error={type(e).__name__}: {str(e)[:160]} "
            "(fail-safe -> every subsystem RUNS this cycle, i.e. legacy behaviour)"
        )
        return Snapshot({}, always_run=True, reason=f"snapshot_failed:{type(e).__name__}")

    state: dict = {}
    for uid, sub, tok, last_tok, age in rows:
        state.setdefault(uid, {})[sub] = (int(tok or 0),
                                          None if last_tok is None else int(last_tok),
                                          None if age is None else float(age))

    due: dict = {}
    for uid in ids:
        seat = state.get(uid, {})
        for name in SUBSYSTEMS:
            row = seat.get(name)
            if row is None:
                # Never recorded → first run. Observed token 0 so record_run writes the token
                # the row will actually carry by then.
                due.setdefault(uid, {})[name] = 0
                continue
            tok, last_tok, age = row
            if last_tok is None or last_tok != tok:
                due.setdefault(uid, {})[name] = tok          # DIRTY
            elif age is None or age >= _effective_max_interval(uid, name):
                due.setdefault(uid, {})[name] = tok          # MAX-RUN BOUND
    snap = Snapshot(due, seats_seen=len(ids))
    _snapshot_cache_write(ids, snap)
    return snap


def claim(snap: Snapshot, user_id: Any, subsystem: str) -> bool:
    """True = RUN this subsystem for this seat. False = clean AND inside the max-run interval.

    Excluded subsystems always return True, even with the flag on (invariant 4).
    """
    if subsystem in EXCLUDED:
        return True
    if snap is None or snap.always_run or not enabled():
        return True
    if subsystem not in SUBSYSTEMS:
        # Unknown name → we cannot reason about it → RUN (invariant 1).
        return True
    return snap.is_due(user_id, subsystem)


def log_skip(snap: Snapshot, user_id: Any, subsystem: str, schema_name: str = "") -> None:
    """ONE visible line per skipped subsystem, with the reason and the evidence.

    A silent skip is indistinguishable from a broken sweep, and this codebase has already paid
    for that ("written where nothing reads it" hit seven surfaces in one day). Emitted at INFO
    so it survives a default log level.
    """
    log.info(
        "re_embedder.sweep_skip "
        f"subsystem={subsystem} schema={schema_name} user={str(user_id)[:8]} "
        f"reason=work_token_unchanged_and_within_max_interval "
        f"max_interval_s={_effective_max_interval(user_id, subsystem)} "
        f"llm={'yes' if SUBSYSTEMS.get(subsystem, {}).get('llm') else 'no'} "
        "note=zero LLM calls and zero tenant queries for this subsystem this cycle"
    )


def log_seat_skip(snap: Snapshot, user_id: Any, schema_name: str = "") -> None:
    """ONE line for a wholly-quiet seat — the case the owner is paying for today."""
    log.info(
        "re_embedder.sweep_skip_seat "
        f"schema={schema_name} user={str(user_id)[:8]} "
        f"reason=no_subsystem_dirty_and_none_past_max_interval "
        "note=no tenant connection opened, no search_path bind, no brain bind, zero LLM calls"
    )


# ──────────────────────────────────────────────────────────────────────────────
# RECORD RUN — close the loop
# ──────────────────────────────────────────────────────────────────────────────
# NOTE — the inserted `work_token` is the OBSERVED token, NOT `GREATEST(observed, 1)`.
# Measured defect, caught by the proof and worth recording: seeding the first row with
# work_token=1 while last_run_token=observed(=0) leaves the row DIRTY the instant it is
# written, so EVERY seat re-ran EVERY subsystem on cycle 2 and the ledger appeared inert.
# Inserting BOTH as the observed value makes a first completed run genuinely clean, while a
# writer that bumps the row (to 1) during or after that run still leaves 1 <> 0 = dirty. The
# column DEFAULT of 1 is for rows minted by the WRITER path, which is a different first-touch.
_RECORD_SQL = f"""
INSERT INTO {_TABLE} (user_id, subsystem, work_token, last_run_token, last_run_at, updated_at)
VALUES (%s::uuid, %s, %s, %s, now(), now())
ON CONFLICT (user_id, subsystem) DO UPDATE
   SET last_run_token = EXCLUDED.last_run_token,
       last_run_at    = now(),
       updated_at     = now()
"""


def record_run(conn: Any, user_id: Any, subsystem: str,
               observed_token: Optional[int]) -> bool:
    """Mark a COMPLETED run: ``last_run_token = observed_token``, ``last_run_at = now()``.

    Call ONLY after the subsystem returned normally (invariant 2). Recording the token OBSERVED
    AT CLAIM — not the token as it stands now — is what makes a write that landed DURING the run
    survive: that write bumped ``work_token`` past ``observed_token``, so the row is dirty again
    the instant this commits.

    COMMITS the connection. That is deliberate and load-bearing in the sweep loop: the
    ``release_read_transaction`` barrier before the next subsystem probes for an ASSIGNED xid
    and, finding a pending WRITE, would REFUSE to roll back and ``log_crit`` — correct behaviour
    on its part, and noise we must not create. Committing here leaves the connection clean.

    Never raises. On failure it rolls back and re-applies the tenant ``search_path``, then
    returns False; the subsystem simply runs again next cycle, which is the safe direction.
    """
    if not enabled() or user_id is None or subsystem not in SUBSYSTEMS or conn is None:
        return False
    tok = 0 if observed_token is None else int(observed_token)
    try:
        with conn.cursor() as cur:
            cur.execute(_RECORD_SQL, (str(user_id), subsystem, tok, tok))  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.sqli.psycopg-sqli.psycopg-sqli — _RECORD_SQL is module-constant; params are user_id + subsystem + token
        conn.commit()
        return True
    except Exception as e:  # noqa: BLE001
        log.warning(
            "sweep_ledger.record_failed "
            f"user={str(user_id)[:8]} subsystem={subsystem} "
            f"error={type(e).__name__}: {str(e)[:160]} "
            "(the subsystem simply runs again next cycle — fail-safe direction)"
        )
        try:
            conn.rollback()
        except Exception:  # pragma: no cover
            pass
        return False


def record_run_dsn(dsn: str, user_id: Any, subsystem: str,
                   observed_token: Optional[int]) -> bool:
    """``record_run`` on its own short-lived AUTOCOMMIT connection, for call sites whose tenant
    connection is closed or in an unknown state. Nothing is left holding a transaction."""
    if not enabled() or not dsn:
        return False
    import psycopg2
    conn = None
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(_RECORD_SQL, (str(user_id), subsystem,  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.sqli.psycopg-sqli.psycopg-sqli — _RECORD_SQL is module-constant; params are user_id + subsystem + token
                                      0 if observed_token is None else int(observed_token),
                                      0 if observed_token is None else int(observed_token)))
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("sweep_ledger.record_dsn_failed "
                    f"error={type(e).__name__}: {str(e)[:120]}")
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # pragma: no cover
                pass


# ──────────────────────────────────────────────────────────────────────────────
# MISSED-WRITER DETECTOR — make the design's one real risk OBSERVABLE
# ──────────────────────────────────────────────────────────────────────────────
# A dirty-flag design fails silently when a writer forgets to mark. The max-run bound turns
# that into "sweeps late" rather than "never sweeps", but late is still a defect and nobody
# would notice it. So: an OFF-HOT-PATH audit that computes each subsystem's REAL input digest
# and compares it with the digest captured at its last recorded run. A digest that MOVED while
# the token stayed CLEAN is a missed writer, named, with the tables to go look at.
#
# This is the honest counterpart to "report the writer set": rather than claim the set is
# complete, ship the thing that proves whether it is.

def audit_seat(db_conn, dsn: str, user_id: Any, schema_name: str) -> dict:
    """Compute every subsystem's real input digest for one seat (caller has bound search_path).

    Returns {subsystem: digest}. Read-only. Intended for an ops lever / a periodic check, NOT
    for the sweep loop — running it every cycle would reintroduce exactly the per-cycle scanning
    cost this design removes.
    """
    out: dict = {}
    for name, spec in SUBSYSTEMS.items():
        sql = spec.get("audit_sql")
        if not sql:
            continue
        try:
            with db_conn.cursor() as cur:
                cur.execute(sql, {"hier": _HIERARCHY_RELS})
                row = cur.fetchone()
            out[name] = "|".join("" if v is None else str(v) for v in (row or ()))
        except Exception as e:  # noqa: BLE001
            out[name] = f"ERR:{type(e).__name__}"
            try:
                db_conn.rollback()
                with db_conn.cursor() as c2:
                    c2.execute(f"SET search_path TO {schema_name}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema_name from tenant provisioning
            except Exception:  # pragma: no cover
                pass
    return out


# ──────────────────────────────────────────────────────────────────────────────
# OPTIONAL REDIS SNAPSHOT CACHE — never authoritative, never blocking
# ──────────────────────────────────────────────────────────────────────────────
def _cache_scope():
    # The snapshot spans every seat, so it is NOT seat-scoped data; it is deployment-scoped
    # coordination. `scope_single_deployment()` is redis_coord's explicit "this deployment"
    # scope — deliberately reached by CALLING it, never by passing an empty string.
    return _rc.scope_single_deployment()


def _cache_key_parts(ids: list) -> str:
    return hashlib.sha256("|".join(sorted(ids)).encode()).hexdigest()[:16]


def _snapshot_cache_ttl() -> int:
    """Bounded BELOW one sweep interval, so a newly-dirty seat waits at most one cycle."""
    try:
        interval = int(os.environ.get("REEMBED_INTERVAL", "60") or "60")
    except (TypeError, ValueError):
        interval = 60
    return max(1, interval // 2)


def _snapshot_cache_read(ids: list) -> Optional[Snapshot]:
    if not snapshot_cache_enabled():
        return None
    c = _rc.client()
    if c is None:
        return None
    try:
        raw = c.get(_rc.key("swpsnap", _cache_scope(), _cache_key_parts(ids)))
        if not raw:
            return None
        return Snapshot(json.loads(raw), reason="redis_cache", seats_seen=len(ids))
    except Exception:  # noqa: BLE001 — cache miss is always a legal answer
        return None


def _snapshot_cache_write(ids: list, snap: Snapshot) -> None:
    if not snapshot_cache_enabled():
        return
    c = _rc.client()
    if c is None:
        return
    try:
        c.set(_rc.key("swpsnap", _cache_scope(), _cache_key_parts(ids)),
              json.dumps(snap.due), ex=_snapshot_cache_ttl())
    except Exception:  # noqa: BLE001
        pass


def reset_caches() -> None:
    """Test hook — drop any cached snapshot so a new ledger state takes effect immediately."""
    _rc.reset_client()
