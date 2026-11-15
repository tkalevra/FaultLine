"""Agent Cortex — the operating agent's OWN persistent OPERATIONAL memory (firewalled).

This is NOT user memory. It is the serving agent's private, Postgres-backed record of
how to OPERATE — gotchas, failed commands, pitfalls, corrections, how-tos, command
overruns — captured MANUALLY (via /iremember) so the agent stops repeating its own
mistakes. Think "Claude's ``.claude`` memory, but Postgres-backed, per-seat, and
separate from user memory." It is NEVER a user fact and NEVER overrides the grounded
knowledge graph.

THE TRUTH FIREWALL (inviolable, structural):
  The cortex lives in its OWN schema ``flagent_<uuid_slug>`` (``derive_cortex_schema_name``),
  DISJOINT from the user-fact schema ``faultline_<uuid_slug>``. The user-fact walk derives
  only the ``faultline_`` name and binds ``SET search_path`` to it WITHOUT ``public`` — so
  it can never reference ``cortex_episodes``. Every cortex read/write here opens its OWN
  short-lived connection bound to ``flagent_<uuid>`` WITHOUT ``public``. The distinct
  schema-name string IS the firewall.

HARD RAILS:
  * MANUAL capture only — no auto-scrape from the tenant query path.
  * FAIL-SAFE / NON-BLOCKING — every public entry point swallows its own errors and returns
    a bool/None/[]; a cortex failure must NEVER raise into a caller.
  * FAIL-LOUD REFUSE — a write whose schema resolves to a ``public``-shaped or otherwise
    unsafe name is REFUSED and ``log_crit``-logged; it never silently writes ``public``.
  * HYBRID RECALL, CORTEX-ONLY SEMANTICS — recall combines the deterministic keyword+tag
    signal (token/substring, normalized-lower) with a SEMANTIC signal from a PER-SEAT Qdrant
    collection (HNSW ANN over nomic embeddings) reranked by a RELATIVE score (NOT a flat
    absolute floor) AND a PROVENANCE weight (how the note was learned). The semantic lane
    lives ONLY here, in the agent's private scratchpad, in its OWN ``flagent-<uuid>``
    collection DISTINCT from the user's ``faultline-<uuid>`` — it NEVER touches the
    user-truth walk (``fetch_facts_from_anchor`` / ``recall_memory`` / ``/query`` stay
    deterministic-not-fuzzy) and never reads/writes the user's collection. Embeddings are
    generated best-effort at write (upserted to the cortex collection) and reconciled in the
    lifecycle sweep; if Qdrant or the embedding backend is unavailable recall DEGRADES
    GRACEFULLY to keyword+tag+provenance — never errors, never blocks. Given fixed
    embeddings the ranking is deterministic (relative rerank over the fixed candidate field).
  * METADATA-DRIVEN — only ``category``/``source``/``lifecycle_state`` are fixed enums;
    ``note``/``tags``/``context`` are CAPTURED content, never enumerated in code. The
    provenance weights are keyed on the fixed ``category`` enum only (no domain vocab).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time as _time
from typing import Any, Iterator, Optional

import structlog

log = structlog.get_logger()

try:  # pragma: no cover - import shim so the module is usable outside the API app
    from src.api.logging_config import log_crit
except Exception:  # pragma: no cover
    def log_crit(logger, msg: str, **kwargs):  # type: ignore
        logger.critical(msg, **kwargs)

# The seeded HOW-YOU-OPERATE usage-pattern set (single source of truth for both the active
# recall block and the passive tool descriptions). Category constant + seed rows.
from src.api.cortex_usage_seed import (  # noqa: E402
    USAGE_PATTERN_CATEGORY, SOURCE_SEED, SEED_VERSION, iter_seed_rows,
    superseded_seed_notes,
)

# ── FIXED enums (the ONLY closed classes; everything else is captured content) ─────────
# Operational-note categories — what KIND of lesson this is.
CATEGORY_GOTCHA = "gotcha"
CATEGORY_FAILED_COMMAND = "failed_command"
CATEGORY_PITFALL = "pitfall"
CATEGORY_CORRECTION = "correction"
CATEGORY_HOWTO = "howto"
CATEGORY_OVERRUN = "overrun"
_VALID_CATEGORY = frozenset({
    CATEGORY_GOTCHA, CATEGORY_FAILED_COMMAND, CATEGORY_PITFALL,
    CATEGORY_CORRECTION, CATEGORY_HOWTO, CATEGORY_OVERRUN,
})
_DEFAULT_CATEGORY = CATEGORY_GOTCHA

# CORRECTION-SHAPE INFERENCE (seam-5, ctx-bench corpus A): most agent-written corrections
# arrive WITHOUT an explicit category — the ctx-bench driver (and real agents mid-work) call
# /iremember with bare prose like "it's 8010 now, not 8001" — and the Build-5 supersession
# gate at note() used to key on the explicit CATEGORY_CORRECTION only, so those corrections
# never tombstoned the stale note and recall kept rendering the OLD value (measured: cortex-lane
# stale exposure 1.0 on corpus A). The shape is deterministic and conservative: an explicit
# old-value negation or replacement cue. NOTE the canonical gotcha lesson "restart the
# worker, not the whole stack" DOES match the shape (it contains "not") — the shape alone
# is NOT the safety property; the gate's >= 2 CONTENT-token overlap with the victim
# (stopwords + sub-4-char fragments stripped, victim set capped) is what makes a wrong
# link unlikely, and a wrong link is recoverable (superseded_by audit trail, PG row kept).
# Fail-safe direction: a missed inference keeps the pre-fix behaviour (stale body renders);
# a wrong inference hides a superseded row that stays queryable in PG (auditable).
_CORRECTION_SHAPE_RE = re.compile(
    r"\b(?:not|never|no longer|instead of|changed from|renamed (?:from|to)"
    r"|replaced (?:by|with)|supersedes?|correction)\b",
    re.IGNORECASE,
)


# Shape-marker words: they MARK a correction (and fire _CORRECTION_SHAPE_RE) but are
# not SUBJECT MATTER. Counting them as content let "never a bare --force" tombstone
# the --force-recreate compose note (shared: force + never) at corpus scale (A5 seat
# 5a07d2c4: 78 tombstones, 47 of them false links). Excluded from the link bar.
_SHAPE_MARKERS = frozenset({
    "never", "not", "changed", "instead", "renamed", "replaced",
    "correction", "supersedes", "longer", "actually",
})


def _supersede_link_decision(note_ordered, note_tokens, victim_note, victor_note) -> tuple:
    """SAME-FACT test for a supersession link (seam-5, A5 corpus-scale fix).

    A correction may only tombstone a note that restates the SAME FACT with a new
    value. Two independent signals, either of which suffices — validated against the
    78 real tombstone pairs on kept seat 5a07d2c4 (47 false / 31 true):
    (a) TEMPLATE MATCH: >= 4 shared content tokens, the first 3 content tokens of
        both notes agree (same sentence opening), and Jaccard >= 0.5 over the union —
        the coffee-count / changelog-rant / runbook-pin chains all pass; generic
        domain pairs ({port, staging}, {mirror, sync}, {honest, lunch}) all fail.
    (b) VALUE RESTATEMENT: a true correction embeds the old value it replaces
        ("8010 now, not 8001") — every digit-bearing token of the victim appears in
        the victor, plus >= 2 shared content tokens. This is what admits the short
        port/backups corrections whose overlap is only {port, staging}.
    Shape-marker words are stripped from BOTH token sets before any of this (they
    fire the entry gate; they are not subject matter). Returns (link, reason).
    """
    try:
        vt = _tokenize(victim_note or "")
        wt = _tokenize(victor_note or "")
        vs = {t for t in vt if t not in _STOPWORDS and t not in _SHAPE_MARKERS and len(t) >= 4}
        ns = {t for t in note_tokens if t not in _SHAPE_MARKERS}
        shared = ns & vs
        if len(shared) < 2:
            return (False, "bar<2")
        vlist = [t for t in vt if t not in _STOPWORDS and t not in _SHAPE_MARKERS and len(t) >= 4]
        nlist = [t for t in note_ordered if t not in _STOPWORDS and t not in _SHAPE_MARKERS and len(t) >= 4]
        head_match = nlist[:3] == vlist[:3]
        jaccard = len(shared) / max(1, len(ns | vs))
        if len(shared) >= 4 and head_match and jaccard >= 0.5:
            return (True, "template-match")
        victim_values = {t for t in vt if any(ch.isdigit() for ch in t) and len(t) >= 2}
        victor_tokens = set(wt)
        if victim_values and victim_values <= victor_tokens and len(shared) >= 2:
            return (True, "value-restated")
        return (False, "below-head-or-value")
    except Exception:  # noqa: BLE001 - a linking signal never sinks capture
        return (False, "error")

# A DELIBERATE lesson — one the agent CHOSE to record, as opposed to `failed_command`, which is
# auto-captured. The distinction is the whole point of the reflection nudge: machinery must not
# be able to stand in for thought.
_DELIBERATE_CATEGORIES = frozenset({
    CATEGORY_GOTCHA, CATEGORY_PITFALL, CATEGORY_CORRECTION, CATEGORY_HOWTO, CATEGORY_OVERRUN,
})

# Source of a note. v1 has one door: the agent's manual /iremember. (Kept as a fixed
# enum so a future automated source is a deliberate, gated addition — not a silent one.)
SOURCE_AGENT_MANUAL = "agent_manual"

LIFECYCLE_OBSERVED = "observed"    # a normal note
LIFECYCLE_HARDENED = "hardened"    # promoted to a RULE at observation_count >= threshold
LIFECYCLE_DECAYED = "decayed"      # swept out (reserved; manual notes are durable)
_VALID_LIFECYCLE = frozenset({LIFECYCLE_OBSERVED, LIFECYCLE_HARDENED, LIFECYCLE_DECAYED})

HARDEN_THRESHOLD = 3               # a repeated note hardens into a rule at >= 3

# ── PROVENANCE weight (Build 2) — rank by HOW the note was learned ─────────────────────
# A user CORRECTION is the gold (the user told me I was wrong → do it this way); a
# hardened rule is proven-by-repetition; an inferred auto-capture (`failed_command`) is
# the weakest signal. This is an ADDITIVE ranking bonus applied ONLY to notes that already
# have keyword/semantic relevance — it reorders relevant notes, it never manufactures a
# match for an unrelated query. Keyed on the FIXED `category` enum only (no domain vocab).
_PROVENANCE_WEIGHT = {
    CATEGORY_CORRECTION:      3.0,   # gold — the user corrected me
    CATEGORY_HOWTO:           1.5,   # a working procedure worth keeping
    CATEGORY_PITFALL:         1.2,
    CATEGORY_GOTCHA:          1.0,
    CATEGORY_OVERRUN:         0.8,
    CATEGORY_FAILED_COMMAND:  0.5,   # weakest — an inferred/auto-capture-shaped lesson
}
_DEFAULT_PROVENANCE_WEIGHT = 1.0
# A hardened note (proven by repetition) gets a further additive bump.
_HARDENED_BONUS = float(os.getenv("CORTEX_HARDENED_BONUS", "2.0") or 2.0)
# ── EVIDENCE bonus — a lesson you can CHECK beats a lesson you must take on faith ────────
# A note is a CLAIM. A claim with no proof rots into a TRAP: on 2026-07-13 a stale "Known
# Bug" (fixed weeks earlier) and two test files asserting DELETED code each manufactured
# phantom work, because nothing recorded what the claim was checked against or when. A note
# carrying `evidence_sha` (the commit it was verified against) is verifiable — an agent can
# ask `git merge-base --is-ancestor <sha> HEAD` and know whether it is still current — so it
# ranks above an unevidenced one. ADDITIVE, applied only to notes that already cleared the
# real-match bar: evidence REORDERS relevant notes, it can never lift an irrelevant one.
_EVIDENCE_BONUS = float(os.getenv("CORTEX_EVIDENCE_BONUS", "1.5") or 1.5)

# ── TIME + BRANCH ranking signals (Build 4) — recency completes the canonical triad ─────
# THE DEFECT THIS CLOSES (replayed on real rows, DEV/replay_stale_vs_fresh.py, 2026-08-16):
# ranking knew RELEVANCE (keyword/tag + semantic) and IMPORTANCE (provenance/evidence) but
# was blind to TIME. A stale howto — hardened by repetition, carrying an old evidence sha —
# outranked the fresh correction that superseded it, because nothing in the score knew the
# correction was newer. Park et al., "Generative Agents" (2023) score agent-memory retrieval
# as recency × importance × relevance; we had two of three. The recorded production failure
# ("the hook picks one note by relevance, never the newest, so it hands you the obsolete
# half of a contradicting pair") is what a missing recency term looks like in the field.
#
# Signals (ALL additive ranking terms, applied ONLY to rows that already cleared the
# real-match bar — they REORDER, never admit, never gate, never drop):
#   * RECENCY — exponential decay on the note's WRITE clock, half-life env-tunable. The
#     forgetting-curve shape is Ebbinghaus (1885/1915); the APPLIED idiom (a frequency/age
#     counter that never decays lets historically-hot items pin the head forever) is Redis
#     LFU's lfu-decay-time. Concretely: _HARDENED_BONUS is now DECAYED by the same factor —
#     repetition is evidence accumulated in the past, and past evidence of hotness is not
#     present practice.
#   * CORRECTION DOMINANCE — a correction is the LATEST WORD (the user said "no, do it this
#     way"); Koren, "Collaborative Filtering with Temporal Dynamics" (KDD 2009): the newest
#     interaction is the strongest evidence of current preference/behavior, and older
#     parameter estimates drift. Weight raised 3.0 -> 4.0.
#   * SUPERSESSION — within one candidate field (which is query-scoped = one topic), a
#     procedural note that PREDATES the newest correction is presumptively the procedure the
#     correction replaced: procedure written BEFORE being corrected could not have known;
#     procedure written AFTER a correction is presumptively already-corrected knowledge (so
#     the demotion is deliberately one-directional). Additive demotion, ranking-only.
#   * BRANCH — a note carries the branch it was learned on; a recall that states the branch
#     being worked gives same-branch notes a small bonus. Mismatched/absent branch = no
#     bonus and NO penalty (never a gate — a note from another line of work still surfaces).
_RECENCY_HALFLIFE_DAYS = float(os.getenv("CORTEX_NOTE_DECAY_HALF_LIFE_D", os.getenv("CORTEX_RECENCY_HALFLIFE_DAYS", "180")) or 180)  # owner-directive default 180d (was 30); legacy env honored when explicitly set
_FRESHNESS_BONUS = float(os.getenv("CORTEX_FRESHNESS_BONUS", "2.0") or 2.0)
_SUPERSEDED_DEMOTE = float(os.getenv("CORTEX_SUPERSEDED_DEMOTE", "2.5") or 2.5)
_BRANCH_MATCH_BONUS = float(os.getenv("CORTEX_BRANCH_MATCH_BONUS", "1.0") or 1.0)
# ── ENV scoping (Build 5): env-BOUND knowledge (a deploy procedure true on truenas, wrong
# on p70) ranks first when the recall states its env, demotes slightly when it names
# ANOTHER env (and is STAMPED at render so the agent sees why), and stays neutral when
# untagged (conceptual knowledge transfers everywhere). All additive; nothing gated —
# an other-env note still surfaces when it is the only match, visibly stamped.
_ENV_MATCH_BONUS = float(os.getenv("CORTEX_ENV_MATCH_BONUS", "1.5") or 1.5)
_ENV_MISMATCH_DEMOTE = float(os.getenv("CORTEX_ENV_MISMATCH_DEMOTE", "0.75") or 0.75)
# Procedural categories a correction can tombstone (see note()'s supersession link).
_SUPERSESSIBLE_CATEGORIES = (CATEGORY_HOWTO, CATEGORY_GOTCHA, CATEGORY_PITFALL,
                             CATEGORY_FAILED_COMMAND)
_PROVENANCE_WEIGHT[CATEGORY_CORRECTION] = 4.0   # was 3.0 — see CORRECTION DOMINANCE above


def _recency_factor(last_seen) -> float:
    """2^(-age_days / halflife) on the note's WRITE clock. 1.0 for a note written now,
    0.5 at one half-life, -> 0 for old notes. Deterministic; never raises (a NULL/unparsable
    clock reads as fresh — the note at least keeps its base provenance weight)."""
    try:
        if last_seen is None:
            return 1.0
        age_days = max(0.0, (_time.time() - last_seen.timestamp()) / 86400.0)
        return 2.0 ** (-age_days / max(_RECENCY_HALFLIFE_DAYS, 1e-9))
    except Exception:
        return 1.0

# ── SEMANTIC recall (mnemory pattern) — per-seat Qdrant ANN + RELATIVE rerank ──────────
# The semantic lane is now a PER-SEAT Qdrant collection (HNSW ANN), DISTINCT from the
# user's `faultline-<uuid>` collection: the cortex owns `flagent-<uuid>`. Notes embed with
# nomic-embed-text-v1.5 (768-d, asymmetric: `search_document` at write, `search_query` at
# recall) — the SAME embeddings as the user's vector, reused via `embed_text`. The Postgres
# row stays source-of-truth for text + counters + lifecycle; Qdrant holds ONLY the vector
# (point id == the `cortex_episodes` row id, payload = seat/category/tags).
#
# RELATIVE RERANK, NOT A FLAT ABSOLUTE FLOOR (this is the whole point). The retired flat
# floor (`CORTEX_SEM_FLOOR=0.50`) was brittle for TWO reasons: (1) nomic's absolute cosine
# baseline TRAVELS across embedder builds/environments, so a fixed 0.50 that separated
# noise here would admit noise (or drop truth) elsewhere; (2) a per-seat note set is a
# CLUSTER of similar infra notes, so an oblique query barely separates the right one on
# ABSOLUTE cosine (measured oblique-related 0.535 vs unrelated ceiling 0.486 — a ~0.05
# margin). The new gate is RELATIVE: over the ANN candidate FIELD we compute the mean μ and
# spread σ of scores, and a note's semantic signal counts ONLY when its score STANDS OUT
# from the field — margin `(score-μ)` above an absolute floor AND a z-score `(score-μ)/σ`
# above a spread threshold. This is invariant to an additive baseline shift (μ shifts with
# the field), so an oblique query surfaces its one clearly-separated note while an unrelated
# query — whose field is flat, no spike — surfaces nothing on the semantic lane. Env-tunable.
#
# The gate is TWO shift/scale-invariant tests, BOTH computed against the live field (self-
# calibrating, no constant compared to a raw cosine):
#   * MARGIN  `(score - μ) >= _SEM_REL_MIN_MARGIN` — the PRIMARY separator. Invariant to an
#     additive baseline shift (μ shifts with the field), so it kills the cross-environment
#     threshold-travel a flat raw-cosine floor suffered. Measured on the local infra-gotcha
#     set (2026-07): true oblique wins separate at margin 0.07–0.10, a wildly-unrelated query
#     that spuriously resembles a dense/"sticky" note tops out ~0.056, and a flat-field noise
#     spike ~0.019 — so a 0.06 margin floor keeps the real wins and drops both noise classes.
#   * Z-SCORE `(score - μ)/σ >= _SEM_REL_Z` — a spread/SCALE guard (an embedder that
#     compresses cosines still separates on standard deviations), and it rejects a spike in a
#     tight field.
# RESIDUAL (honest): in a TINY per-seat corpus a single semantically-dense note is "sticky"
# (elevated cosine to arbitrary text); a wildly-unrelated query can still nose it just under
# the margin floor. This dilutes as the corpus grows, and the keyword/tag + provenance gate
# is the final surface arbiter. A genuine NEAR-TIE between two operationally-similar notes is
# an honest miss (fail-safe) — it surfaces on a tag/keyword instead.
_SEM_REL_MIN_MARGIN = float(os.getenv("CORTEX_SEM_REL_MIN_MARGIN", "0.06") or 0.06)
_SEM_REL_Z = float(os.getenv("CORTEX_SEM_REL_Z", "1.4") or 1.4)
_SEM_REL_WEIGHT = float(os.getenv("CORTEX_SEM_REL_WEIGHT", "2.0") or 2.0)
_ANN_TOP_K = int(os.getenv("CORTEX_ANN_TOP_K", "64") or 64)  # small per-seat set → whole field
_EMBED_TIMEOUT = float(os.getenv("CORTEX_EMBED_TIMEOUT", "10") or 10)
_EMBED_BACKFILL_CAP = int(os.getenv("CORTEX_EMBED_BACKFILL_CAP", "100") or 100)
_EMBED_DIM = 768  # nomic-embed-text-v1.5

# Per-seat cortex Qdrant collection — DISTINCT prefix `flagent-` so it can NEVER be the
# user's `faultline-<uuid>` collection. The distinct string IS the vector-side firewall.
_CORTEX_COLLECTION_PREFIX = "flagent-"
# Firewall = the `flagent-` PREFIX (disjoint from the user `faultline-` family). The suffix is
# either a per-seat dashed-hex uuid (collection_per_seat) OR the shared name `cortex`
# (shared_payload → `flagent-cortex`), so the class admits `[a-z0-9-]` after the prefix. It can
# still NEVER match a `faultline-` collection — the vector-side firewall is intact.
_SAFE_CORTEX_COLLECTION_RE = re.compile(r"^flagent-[a-z0-9-]+$")


def _qdrant_url() -> str:
    return os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")


def _cortex_partition(user_id: str):
    """Resolve (collection, tenant_filter) for this seat's cortex via the partition
    choke-point. collection_per_seat → (`flagent-<uuid>`, None); shared_payload →
    (`flagent-cortex`, {must:[tenant_id]}). Returns (None, None) on an unresolvable seat so
    callers no-op (never fall back to an unfiltered / wrong collection)."""
    from src.api.qdrant_partition import resolve_partition, require_tenant
    coll, tflt = resolve_partition(user_id, "cortex")
    if not coll or not _SAFE_CORTEX_COLLECTION_RE.match(coll):
        return None, None
    # shared_payload: a resolved cortex op MUST carry the tenant filter (poison-test guard).
    require_tenant(tflt, op="cortex", collection=coll)
    return coll, tflt

# ── DECAY tuning (Build 3) — quality over volume, deterministic (counters + age) ───────
# Low-value auto-capture-shaped notes (`failed_command`) that are OLD and RARELY recalled
# decay; corrections / hardened / high-hit_count notes NEVER decay (the gold stays durable).
# Deterministic: a category filter + observation/hit counters + a last_seen age cut — no
# fuzzy scoring in the decay decision.
_DECAY_AGE_DAYS = int(os.getenv("CORTEX_DECAY_AGE_DAYS", "30") or 30)
_DECAY_MAX_HITS = int(os.getenv("CORTEX_DECAY_MAX_HITS", "1") or 1)
# Only these categories are decay-eligible when stale+low-hit (the weakest, auto-capture
# shaped tiers). Corrections/how-tos/gotchas/pitfalls are curated and stay durable.
_DECAY_ELIGIBLE_CATEGORIES = (CATEGORY_FAILED_COMMAND,)

# Cortex schema-name shape — belt-and-suspenders before any interpolation. The prefix
# is INTENTIONALLY `flagent_` (never the user `faultline_` prefix): a public-shaped or
# user-shaped name must never pass this gate.
_SAFE_CORTEX_SCHEMA_RE = re.compile(r"^flagent_[a-z0-9_]+$")

# Query tokenization for deterministic recall. Split on any non-alphanumeric; keep tokens
# of length >= 2. This is generic language tokenization (NOT domain/subject vocab) — the
# discriminating signal is tag/keyword OVERLAP, not any enumerated word list.
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MIN_TOKEN_LEN = 2
_RECALL_CANDIDATE_CAP = 500        # per-seat operational memory is small; bound the scan

# Generic English function words dropped from recall scoring so they cannot manufacture
# spurious matches (e.g. every note containing "the"). This is a LANGUAGE-layer stoplist
# (standard IR), NOT domain/subject vocab — the discriminating signal remains tag/keyword
# overlap on content words.
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "to", "and", "or", "in", "on", "at", "for", "is", "it",
    "be", "am", "are", "was", "were", "do", "does", "did", "my", "me", "i", "we",
    "you", "your", "that", "this", "with", "as", "by", "from", "how", "why", "what",
    "when", "where", "which", "who", "so", "if", "up", "out", "can", "will", "wont",
    "not", "no", "yes", "get", "got", "new", "old",
})


def _dsn() -> Optional[str]:
    return os.environ.get("POSTGRES_DSN")


def cortex_schema_for(user_id: str) -> Optional[str]:
    """Derive + VALIDATE the cortex schema name from a UUID. Returns None on any bad id.

    Never raises — callers are fail-safe. A None result means "cannot safely resolve a
    cortex schema" → the caller no-ops (never falls back to public)."""
    try:
        from src.provisioning.schema_manager import derive_cortex_schema_name
        schema = derive_cortex_schema_name(user_id)  # raises on non-UUID
    except Exception:
        return None
    if not _SAFE_CORTEX_SCHEMA_RE.match(schema or ""):
        return None
    return schema


@contextlib.contextmanager
def _cortex_conn(schema: str) -> Iterator[Any]:
    """Open a short-lived connection bound to the CORTEX schema, WITHOUT ``public``.

    FAIL-LOUD REFUSE: if ``schema`` is empty / ``public`` / not ``flagent_``-shaped, this
    raises BEFORE any statement runs — a cortex write can never land in ``public``.
    """
    if not schema or schema == "public" or not _SAFE_CORTEX_SCHEMA_RE.match(schema):
        log_crit(log, "cortex.refuse_unsafe_schema",
                 schema=str(schema)[:40],
                 reason="cortex bind must be flagent_-shaped, never public")
        raise ValueError("refusing to bind an unsafe/public-shaped cortex schema")
    dsn = _dsn()
    if not dsn:
        raise RuntimeError("POSTGRES_DSN not set")
    import psycopg2
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            # FIREWALL: bind the cortex schema WITHOUT public.
            cur.execute(f"SET search_path TO {schema}")  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.sqli.psycopg-sqli.psycopg-sqli, python.lang.security.audit.formatted-sql-query.formatted-sql-query schema validated by _SAFE_CORTEX_SCHEMA_RE ^flagent_[a-z0-9_]+$ before this line
        # ONE yield, OUTSIDE the cursor block. A @contextmanager generator must yield exactly
        # once; a second yield makes contextlib raise RuntimeError("generator didn't stop") at
        # __exit__ — AFTER the body ran and committed — so every caller's post-with work was
        # skipped and every in-with `return` was replaced by the exception. Introduced by the
        # 654d9382 nosemgrep pass, measured live on pre-prod 2026-09-14
        # (cortex.seed_usage_patterns.failed error="generator didn't stop"). Pinned by
        # tests/test_contextmanager_single_yield.py.
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _current_search_path(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SHOW search_path")
        return (cur.fetchone() or [""])[0]


# ── helpers ────────────────────────────────────────────────────────────────────────────

def _normalize_category(category: Optional[str]) -> str:
    c = (category or "").strip().lower()
    return c if c in _VALID_CATEGORY else _DEFAULT_CATEGORY


def _normalize_tags(tags: Any) -> list[str]:
    """Coerce tags into a clean, lowercased, de-duplicated list. Accepts a list or a
    comma/space-separated string. Never raises."""
    out: list[str] = []
    seen: set[str] = set()
    try:
        if tags is None:
            items: list[str] = []
        elif isinstance(tags, str):
            items = re.split(r"[,\s]+", tags)
        else:
            items = [str(t) for t in tags]
        for t in items:
            tag = str(t).strip().lower()
            if tag and tag not in seen:
                seen.add(tag)
                out.append(tag)
    except Exception:
        return []
    return out[:32]


def _tokenize(text: Optional[str]) -> list[str]:
    if not text:
        return []
    return [t for t in _TOKEN_RE.findall(str(text).lower()) if len(t) >= _MIN_TOKEN_LEN]


def _tag_subtokens(tag: str) -> list[str]:
    """A tag plus its hyphen/underscore-split parts (so `llm-endpoint` also matches
    `llm` and `endpoint`)."""
    parts = [tag] + [p for p in re.split(r"[-_]+", tag) if len(p) >= _MIN_TOKEN_LEN]
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


_SUBSTR_MIN_LEN = 4  # substring match needs a contentful shorter token (avoids "on"⊂"won")


def _bi_substr_match(a: str, b: str) -> bool:
    """True if the tokens match. Exact equality always counts. A substring match (either
    direction) is allowed only when the SHORTER token is >= 4 chars — so query `rebuild`
    matches note token `build` and `test` matches `tests`, but a short accidental
    substring like `on` ⊂ `won` does NOT. Deterministic, no fuzzy."""
    if a == b:
        return True
    if min(len(a), len(b)) < _SUBSTR_MIN_LEN:
        return False
    return a in b or b in a


def _keyword_hits(q_tokens, ntext: Optional[str], tags) -> tuple[int, int]:
    """(tag_hits, note_hits) — how many DISTINCT query tokens matched this note's TAGS and,
    failing that, its NOTE text. One query token contributes to at most one bucket (tags win),
    so the counts are disjoint and ``tag_hits + note_hits`` is a distinct-token count.

    Factored out of ``_score_candidate_rows`` so the ranking lane and the experience-push gate
    score keyword corroboration with the SAME function — two copies of this formula would drift,
    and the push gate's whole job is to be stricter than the ranking lane on the SAME signal."""
    if not q_tokens:
        return 0, 0
    tag_subtokens: set = set()
    for t in (tags or []):
        tag_subtokens.update(_tag_subtokens(str(t).lower()))
    note_tokens = set(_tokenize(ntext))
    tag_hits = note_hits = 0
    for qt in set(q_tokens):
        if any(_bi_substr_match(qt, st) for st in tag_subtokens):
            tag_hits += 1
        elif any(_bi_substr_match(qt, nt) for nt in note_tokens):
            note_hits += 1
    return tag_hits, note_hits


# ── SEMANTIC helpers (CORTEX-ONLY — never the user-truth walk) ─────────────────────────

def _embed_cortex_text(text: Optional[str], task: str = "search_document") -> Optional[list[float]]:
    """Best-effort embedding of a cortex note / query, reusing the re_embedder's embedding
    path (local fastembed first, backend ``/v1/embeddings`` fallback). FAIL-SAFE: any
    failure / unavailable backend returns None (NEVER a hash vector — a meaningless vector
    would pollute cosine), so recall degrades to keyword+tag. ``task`` is the nomic
    asymmetric prefix: stored notes use ``search_document``, queries use ``search_query``."""
    if not text or not str(text).strip():
        return None
    try:
        from src.re_embedder.embedder import embed_text
        from src.api.llm_client import get_llm_chat_url, get_embedding_url
        try:
            chat_url = get_llm_chat_url() or ""
        except Exception:
            chat_url = ""
        try:
            embed_url = get_embedding_url(chat_url) or ""
        except Exception:
            embed_url = ""
        vec = embed_text(
            str(text), chat_url, timeout=_EMBED_TIMEOUT, fallback=False,
            embedding_url=(embed_url or None), task=task,
        )
        if vec and isinstance(vec, (list, tuple)) and len(vec) > 0:
            return [float(x) for x in vec]
    except Exception as e:  # pragma: no cover - defensive
        log.debug("cortex.embed.failed", task=task, error=str(e)[:120])
    return None


# ── CORTEX Qdrant collection (mnemory pattern — per-seat HNSW ANN) ─────────────────────
# The RESERVED in-row `embedding BYTEA` column is now DORMANT (never written, never read for
# recall): the vector lives in the per-seat Qdrant collection, not the row. Leaving the
# column in place (vs a DROP migration) keeps the template/firewall stable and reversible —
# no schema change, no migration, and the reconcile re-embeds the handful of drift rows
# cheaply so a cached blob buys nothing.

def cortex_collection_for(user_id: str) -> Optional[str]:
    """Derive + VALIDATE the per-seat cortex Qdrant collection name. Returns None on any bad
    id (caller no-ops). NEVER returns the user's ``faultline-<uuid>`` collection.

    The cortex SCHEMA name (``flagent_<uuid>``) is validated first via ``cortex_schema_for``
    (which proves a real UUID); the collection reuses the raw dashed UUID with the DISTINCT
    ``flagent-`` prefix. The distinct prefix is the vector-side firewall."""
    if not cortex_schema_for(user_id):  # validates the UUID + refuses public/user-shaped
        return None
    uid = str(user_id).strip().lower()
    coll = f"{_CORTEX_COLLECTION_PREFIX}{uid}"
    if not _SAFE_CORTEX_COLLECTION_RE.match(coll):
        return None
    # belt-and-suspenders: never collide with the user's collection prefix.
    if coll.startswith("faultline-"):
        return None
    return coll


def _ensure_cortex_collection(collection: str) -> bool:
    """Idempotently create the per-seat cortex collection (768-d cosine). Fail-safe: any
    failure returns False (caller degrades to keyword+tag / defers the vector). NEVER raises."""
    if not collection or not _SAFE_CORTEX_COLLECTION_RE.match(collection):
        return False
    try:
        import httpx
        from src.api.qdrant_partition import qdrant_headers, ensure_tenant_index, shared_mode
        url = _qdrant_url()
        r = httpx.get(f"{url}/collections/{collection}", headers=qdrant_headers(), timeout=10.0)
        if r.status_code == 200:
            if shared_mode():
                ensure_tenant_index(collection, url)
            return True
        if r.status_code == 404:
            c = httpx.put(
                f"{url}/collections/{collection}",
                json={"vectors": {"size": _EMBED_DIM, "distance": "Cosine"}},
                headers=qdrant_headers(),
                timeout=10.0,
            )
            if c.status_code == 200 and shared_mode():
                ensure_tenant_index(collection, url)
            return c.status_code == 200
        return False
    except Exception as e:  # pragma: no cover - defensive
        log.debug("cortex.qdrant.ensure_failed", collection=collection, error=str(e)[:120])
        return False


def _upsert_cortex_point(collection: str, point_id, vector: list[float],
                         payload: dict, user_id: str = None) -> bool:
    """Upsert ONE note's vector into the cortex collection. In collection_per_seat the point id
    is the bare `cortex_episodes` row id (today); in shared_payload the caller passes a
    tenant-namespaced id and the payload is tenant-stamped. Fail-safe: False on any failure
    (the PG row is already durable). NEVER raises."""
    if not collection or not vector:
        return False
    try:
        import httpx
        from src.api.qdrant_partition import (
            stamp_tenant, qdrant_headers, shared_mode, resolve_cortex_point_id,
        )
        # `point_id` is always the cortex_episodes ROW id. In shared mode fold the seat uuid
        # into a namespaced point id (collision-free) + stamp tenant_id; per-seat keeps the
        # bare int (today).
        if shared_mode():
            _payload = stamp_tenant(payload, user_id)
            _pid = resolve_cortex_point_id(user_id, int(point_id))
        else:
            _payload = payload
            _pid = int(point_id)
        r = httpx.put(
            f"{_qdrant_url()}/collections/{collection}/points",
            json={"points": [{"id": _pid, "vector": vector, "payload": _payload}]},
            headers=qdrant_headers(),
            timeout=15.0,
        )
        return r.status_code == 200
    except Exception as e:  # pragma: no cover - defensive
        log.debug("cortex.qdrant.upsert_failed", collection=collection, error=str(e)[:120])
        return False


def _search_cortex_ann(collection: str, query_vector: list[float],
                       tenant_filter: dict = None) -> list[tuple[int, float]]:
    """ANN over the cortex collection → list of (row_id, cosine_score) across the FIELD (top
    ``_ANN_TOP_K``; a per-seat set is small so this is effectively the whole collection —
    what the RELATIVE rerank needs to compute the field μ/σ). Fail-safe: [] on any
    failure / absent collection → the caller degrades to keyword+tag. NEVER raises."""
    if not collection or not query_vector:
        return []
    try:
        import httpx
        from src.api.qdrant_partition import apply_tenant_filter, qdrant_headers, shared_mode
        _shared = shared_mode()
        # shared_payload: scope to this tenant AND fetch payload so row_id (not the
        # tenant-namespaced point UUID) can be returned to the row-keyed rerank.
        body = apply_tenant_filter(
            {"query": query_vector, "limit": _ANN_TOP_K,
             "with_payload": bool(_shared), "with_vectors": False},
            tenant_filter,
        )
        r = httpx.post(
            f"{_qdrant_url()}/collections/{collection}/points/query",
            json=body,
            headers=qdrant_headers(),
            timeout=10.0,
        )
        if r.status_code != 200:
            return []
        out: list[tuple[int, float]] = []
        for p in (r.json().get("result", {}) or {}).get("points", []) or []:
            try:
                _rid = (p.get("payload", {}) or {}).get("row_id") if _shared else p["id"]
                out.append((int(_rid), float(p.get("score", 0.0))))
            except Exception:
                continue
        return out
    except Exception as e:  # pragma: no cover - defensive
        log.debug("cortex.qdrant.search_failed", collection=collection, error=str(e)[:120])
        return []


def _cortex_point_ids(collection: str, tenant_filter: dict = None) -> Optional[set[int]]:
    """Scroll the cortex collection for its existing ROW ids (for reconcile). Returns None
    on any failure (unknown → reconcile skips, never re-upserts blindly). NEVER raises.

    collection_per_seat → the point id IS the row id. shared_payload → scoped to this tenant,
    and the row id is read from the payload (the point id is a tenant-namespaced UUID)."""
    if not collection:
        return None
    try:
        import httpx
        from src.api.qdrant_partition import apply_tenant_filter, qdrant_headers, shared_mode
        _shared = shared_mode()
        ids: set[int] = set()
        offset = None
        for _ in range(50):  # bounded page loop; per-seat set is small
            body: dict = {"limit": 256, "with_payload": bool(_shared), "with_vectors": False}
            if offset is not None:
                body["offset"] = offset
            body = apply_tenant_filter(body, tenant_filter)
            r = httpx.post(
                f"{_qdrant_url()}/collections/{collection}/points/scroll",
                json=body, headers=qdrant_headers(), timeout=10.0,
            )
            if r.status_code == 404:
                return set()  # collection absent → nothing synced yet (known-empty)
            if r.status_code != 200:
                return None
            res = r.json().get("result", {}) or {}
            for p in res.get("points", []) or []:
                try:
                    _rid = (p.get("payload", {}) or {}).get("row_id") if _shared else p["id"]
                    ids.add(int(_rid))
                except Exception:
                    continue
            offset = res.get("next_page_offset")
            if not offset:
                break
        return ids
    except Exception as e:  # pragma: no cover - defensive
        log.debug("cortex.qdrant.scroll_failed", collection=collection, error=str(e)[:120])
        return None


def _cortex_point_payload(user_id: str, rid: int, category: str, tags: list[str],
                          note_text: str, hardened: bool, obs_count: int,
                          model: Optional[str] = None, note_type: Optional[str] = None,
                          project: Optional[str] = None) -> dict:
    """Payload carried alongside the vector (seat/category/tags for traceability + optional
    future server-side filtering). Postgres remains source-of-truth for text/counters.
    Uplift provenance (model/note_type/project) rides only when present — payloads stay
    compact and legacy-shaped points never gain empty keys."""
    payload = {
        "seat_id": str(user_id),
        "row_id": int(rid),
        "category": category,
        "tags": list(tags or []),
        "note": (note_text or "")[:400],
        "hardened": bool(hardened),
        "observation_count": int(obs_count or 0),
    }
    if model:
        payload["model"] = str(model)[:64]
    if note_type:
        payload["type"] = str(note_type)[:64]
    if project:
        payload["project"] = str(project)[:64]
    return payload


def delete_cortex_collection(user_id: str) -> bool:
    """Tenant-wipe the seat's cortex vectors. Idempotent: a 404 is success. Fail-safe: False on
    a real failure, NEVER raises. Refuses any name not ``flagent-``-shaped (can never touch the
    user's ``faultline-<uuid>`` collection).

    collection_per_seat → DELETE the per-seat ``flagent-<uuid>`` collection (today).
    shared_payload → the shared ``flagent-cortex`` collection is NOT dropped (it holds every
    seat); instead ONLY this tenant's points are deleted via the tenant filter."""
    from src.api.qdrant_partition import shared_mode, qdrant_headers
    collection, tflt = _cortex_partition(user_id)
    if not collection:
        return False
    try:
        import httpx
        if shared_mode():
            # Delete ONLY this tenant's points from the shared collection (never drop it).
            from src.api.qdrant_partition import build_delete_body
            r = httpx.post(
                f"{_qdrant_url()}/collections/{collection}/points/delete",
                json=build_delete_body(tflt, must=[]),
                headers=qdrant_headers(),
                timeout=15.0,
            )
            ok = r.status_code in (200, 404)
            log.info("cortex.qdrant.tenant_points_deleted", collection=collection, status=r.status_code)
            return ok
        r = httpx.delete(f"{_qdrant_url()}/collections/{collection}",
                         headers=qdrant_headers(), timeout=15.0)
        ok = r.status_code in (200, 404)
        log.info("cortex.qdrant.collection_deleted", collection=collection, status=r.status_code)
        return ok
    except Exception as e:
        log.warning("cortex.qdrant.collection_delete_failed",
                    collection=collection, error=str(e)[:120])
        return False


def delete_cortex_schema(user_id: str) -> bool:
    """Tenant-wipe the seat's cortex SCHEMA (``flagent_<uuid>``). The Postgres half of the
    teardown whose Qdrant half is ``delete_cortex_collection`` directly above.

    WHY THIS EXISTS — MEASURED, NOT INFERRED (2026-08-27, this box). ``purge_user_data``
    (``saas/memory_ops.py``) drops the tenant schema, the ``faultline-<uuid>`` collection AND
    the ``flagent-<uuid>`` collection — and NOTHING drops the ``flagent_<uuid>`` SCHEMA. No
    product function did. The result on this box: **1,632 ``flagent_`` schemas against 8 live
    ``faultline_`` tenant schemas** — 1,624 orphans, one per seat ever purged, each holding
    that seat's private operational notes forever. Its own call-site comment claimed "the
    cortex owns its OWN teardown so a full wipe leaves no cortex vectors behind": true of the
    VECTORS, and the schema was never in scope. A GDPR/"forget me" path that leaves a seat's
    notes readable in Postgres has not forgotten them.

    WHY IT LIVES HERE AND NOT IN ``purge_user_data``. The ``flagent_``-shape guard
    (``_SAFE_CORTEX_SCHEMA_RE``) is the truth firewall, and it is defined in this module. A
    ``DROP SCHEMA`` built from an interpolated identifier in the SaaS layer would be a SECOND
    place that has to get the guard right — and the guard is the only thing standing between
    this statement and ``public`` or a user's ``faultline_<uuid>``. One owner, one regex:
    ``cortex_schema_for`` derives AND validates, and we re-assert the shape immediately before
    interpolation. ``purge_user_data`` gets one line next to its collection sibling.

    NOT ``_cortex_conn``: that helper binds ``search_path`` TO the schema, and binding a
    session to a schema in order to drop it is backwards. This opens a plain connection and
    interpolates a name that has passed the guard TWICE.

    Idempotent (``DROP SCHEMA IF EXISTS`` — a second call is success). Fail-safe: returns
    False on a real failure, NEVER raises. Fail-LOUD: a name that fails the guard is a
    ``log_crit``, never a silent skip."""
    schema = cortex_schema_for(user_id)
    if not schema:
        log_crit(log, "cortex.schema_drop_refused",
                 user=str(user_id)[:8],
                 reason="could not derive a safe flagent_-shaped cortex schema for this seat")
        return False
    # Belt-and-suspenders: re-assert the shape at the point of interpolation. `public` and any
    # `faultline_`-shaped name are structurally unreachable from here.
    if schema == "public" or not _SAFE_CORTEX_SCHEMA_RE.match(schema):
        log_crit(log, "cortex.schema_drop_refused",
                 schema=str(schema)[:40],
                 reason="cortex DROP must be flagent_-shaped, never public/user-shaped")
        return False
    dsn = _dsn()
    if not dsn:
        log.warning("cortex.schema_drop_failed", schema=schema, error="POSTGRES_DSN not set")
        return False
    try:
        import psycopg2
        conn = psycopg2.connect(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.sqli.psycopg-sqli.psycopg-sqli, python.lang.security.audit.formatted-sql-query.formatted-sql-query schema validated by _SAFE_CORTEX_SCHEMA_RE ^flagent_[a-z0-9_]+$ two lines above
            conn.commit()
        finally:
            conn.close()
        log.info("cortex.schema_dropped", schema=schema)
        return True
    except Exception as e:
        log.warning("cortex.schema_drop_failed", schema=schema, error=str(e)[:120])
        return False


def _delete_cortex_points(collection: str, point_ids: list[int],
                          user_id: str = None, tenant_filter: dict = None) -> bool:
    """Delete points from the cortex collection by ROW id (decayed-row cleanup). Fail-safe.

    collection_per_seat → bare-int id delete (point id == row id, today). shared_payload → the
    row ids are re-derived to tenant-namespaced point ids AND the delete is tenant-filtered
    (has_id + tenant), so it can never touch another seat's colliding row id."""
    if not collection or not point_ids:
        return False
    try:
        import httpx
        from src.api.qdrant_partition import (
            shared_mode, build_delete_body, resolve_cortex_point_id, qdrant_headers,
        )
        if shared_mode():
            _pids = [resolve_cortex_point_id(user_id, int(i)) for i in point_ids]
            body = build_delete_body(tenant_filter, point_ids=_pids)
        else:
            body = {"points": [int(i) for i in point_ids]}
        r = httpx.post(
            f"{_qdrant_url()}/collections/{collection}/points/delete",
            json=body,
            headers=qdrant_headers(),
            timeout=15.0,
        )
        return r.status_code == 200
    except Exception as e:  # pragma: no cover - defensive
        log.debug("cortex.qdrant.point_delete_failed", collection=collection, error=str(e)[:120])
        return False


def ensure_cortex_collection_for(user_id: str) -> bool:
    """Public: ensure the per-seat cortex collection exists (called on seat provisioning).
    Fail-safe: False if unresolved/unreachable; the write path re-ensures lazily. NEVER raises."""
    collection, _ = _cortex_partition(user_id)
    if not collection:
        return False
    return _ensure_cortex_collection(collection)


def _relative_semantic_components(hits: list[tuple[int, float]]) -> dict[int, float]:
    """RELATIVE rerank (the mnemory pattern's core, minus multi-query LLM expansion —
    DEFERRED as too heavy for the cortex). Given the ANN field ``hits`` = [(row_id, cosine)],
    admit a note's semantic signal ONLY when its score clearly STANDS OUT from the field:
      * margin ``(score - μ) >= _SEM_REL_MIN_MARGIN``  (an absolute-shift-invariant spike), and
      * z-score ``(score - μ)/σ >= _SEM_REL_Z``        (stands out from the spread).
    Returns {row_id: semantic_component} for admitted rows only (component scaled by how far
    the note stands out, NOT by absolute cosine — so it is invariant to the embedder's
    baseline). A flat field (unrelated query) yields NO admissions → honest miss. Deterministic."""
    if not hits:
        return {}
    scores = [s for _, s in hits]
    n = len(scores)
    mu = sum(scores) / n
    sigma = (sum((s - mu) ** 2 for s in scores) / n) ** 0.5
    out: dict[int, float] = {}
    for rid, s in hits:
        margin = s - mu
        if margin < _SEM_REL_MIN_MARGIN:
            continue
        if sigma > 0.0:
            z = margin / sigma
            if z < _SEM_REL_Z:
                continue
            out[rid] = z * _SEM_REL_WEIGHT
        else:
            # degenerate: identical scores → no separation possible → only a lone spike
            # (margin already cleared the absolute floor above) counts, at unit weight.
            out[rid] = _SEM_REL_WEIGHT
    return out


# ── NOTE-ADMISSION GATE (the operate-lane poison guard) ─────────────────────────────────
#
# THE HOW YOU OPERATE lane holds LESSONS, not data. It must carry real operating
# notes — advisory prose the agent itself wrote about how to work (a gotcha, a fix, a
# procedure, a correction) — and NEVER data: terminal output, stack tracebacks, command
# logs, pasted file/config content, or conversation transcripts. Without a gate, anything
# pasted into a turn can be written through ``/iremember`` and surface inside the HOW YOU
# OPERATE block — a data-hygiene break (and whatever the block's advisory framing does not
# neutralize becomes an injection surface).
#
# The cortex is the agent's OWN working memory, and the tool layer instructs the agent to
# write notes in its own words — an agent-authored note cannot carry a pasted payload. That
# own-words mandate is the primary injection defense, so admission needs NO semantic
# judgement here. This deterministic, fail-closed SHAPE gate exists only as a free backstop
# against direct/non-agent writes, and it guarantees the cortex write path makes ZERO
# external LLM calls and resolves NO tenant brain. It is deliberately NOT a shell-syntax
# blacklist: enumerating ``export`` / ``cd`` / ``Traceback`` / ``.yml`` is brittle (a new
# tool, a new flag, a wrapper script and it leaks) and is the wrong layer. Instead it
# POSITIVELY identifies NATURAL-LANGUAGE ADVISORY PROSE — grammatical structure (function
# words), bounded shape (a lesson is a few sentences, a dump is many lines / a fenced
# block), and letter dominance — and refuses everything else.
#
# FAIL-CLOSED, AND THAT IS THE POINT. The old behaviour had no gate at all — every text was
# admitted, which is exactly "default text into the operate lane", shipping pasted data as
# instruction. The gate's safe default is REFUSE: ambiguous or data-shaped text is kept OUT
# of the lane, not admitted on the hope it might be a lesson. A refused real lesson is
# recoverable (rephrase in your own words); a poisoned lane is not.

# Function words / common verbs — LANGUAGE structure (the same standard-IR stopword notion as
# ``_STOPWORDS`` above), NOT shell/domain syntax. A real operational lesson is rich in these; a
# command line, an env export, a YAML dump are poor in them.
_NOTE_FUNCWORDS = frozenset({
    "the", "a", "an", "of", "to", "and", "or", "in", "on", "at", "for", "is", "are",
    "was", "be", "do", "does", "not", "no", "never", "always", "when", "if", "before",
    "after", "with", "that", "this", "it", "you", "your", "use", "run", "call", "check",
    "make", "set", "keep", "skip", "add", "under", "new", "once", "then", "so", "but",
    "because", "all", "any", "more", "need", "must", "should",
})
_NOTE_MAX_LINES = 8             # a lesson is a few sentences; a traceback/config dump is many lines
_NOTE_MAX_SYMBOL_RATIO = 0.60   # letters must dominate; code/log lines are symbol-heavy
_NOTE_MIN_FUNCWORDS = 2         # grammatical structure => at least two function words
_NOTE_BLOCK_MIN_LINES = 3       # >=3 lines is a "block" — tested for prose shape, not just tokens
_NOTE_MAX_CODE_LINES = 1        # a multi-line prose lesson has at most one incidental code-shaped line
_NOTE_MIN_SPEAKER_TURNS = 2     # >=2 "<speaker>:" turns (and a majority of lines) = a chat transcript
_CODE_FENCE_RE = re.compile(r"```")
# A REAL sentence terminator: . ! ? followed by whitespace or end-of-string. The dots inside a
# path (``verify.py``), a method call (``ansible.main``) or an extension (``.yml``) are NOT followed
# by space/EOL, so a traceback — whose only dots are internal — matches NOTHING here, while real
# advisory prose (``...under /opt.``) matches. This is the clean structural seam between a
# sentence and a code/log line, and it is punctuation-class detection, not a shell-token list.
_SENTENCE_END_RE = re.compile(r"[.!?](?:\s|$)")
# A CONVERSATION TURN label: a short alpha label at the start of a line followed by ": " and text
# (``user:``, ``assistant:``, ``Alice:``, ``customer:``). A BLOCK dominated by these is a pasted
# chat transcript — data, not a lesson. The label is alpha-only (so ``Step 1:`` / ``qwen3.5-9b:``
# with digits do NOT match) and this is CONVERSATION-STRUCTURE detection (turn-taking shape), not a
# shell/domain vocabulary list. Catches the one prose-shaped data type the symbol/line checks miss.
_SPEAKER_TURN_RE = re.compile(r"^\s*[A-Za-z][A-Za-z._-]{0,20}:\s+\S")


def _line_is_code_shaped(line: str) -> bool:
    """Is this line shaped like COMPUTER OUTPUT (a stack frame, a log line, an assignment,
    a config entry) rather than a prose sentence?

    Scores PUNCTUATION/SHAPE classes — leading indent, path separators, digits, quotes,
    brackets, ``=``, comment/pipe/arrow markers — NOT shell command tokens. A line carrying
    two or more of these is code/log-shaped regardless of the English words it happens to
    contain (a traceback frame ``File "x.py", line 42`` is full of English yet is not prose)."""
    ln = line or ""
    score = 0
    stripped = ln.lstrip()
    if len(stripped) < len(ln):           # leading whitespace = continuation/code indent
        score += 1
    if "/" in ln or "\\" in ln:           # path separator
        score += 1
    if any(ch.isdigit() for ch in ln):    # line number / exit code / error code
        score += 1
    if '"' in ln or "'" in ln:            # quoted path / string
        score += 1
    if any(b in ln for b in ("()", "[]", "{}")):   # call / index / mapping
        score += 1
    if "=" in ln:                         # assignment / flag
        score += 1
    if any(tok in ln for tok in ("->", "=>", "#", "|", ";")):   # comment / pipe / arrow
        score += 1
    return score >= 2


def _looks_like_operational_prose(text: str) -> tuple[bool, str]:
    """Deterministic, FAIL-CLOSED prose test: is ``text`` natural-language advisory prose
    (a real operating lesson) or DATA (terminal output / code / log / dump)?

    Returns ``(admit, reason)``. ADMITS only on POSITIVE evidence of prose and refuses
    everything else. Ambiguous or data-shaped text is REFUSED: this is the ONLY gate
    (deterministic, LLM-free by construction — the cortex never makes an external LLM
    call), and it errs toward refusing (the lane stays lessons-only) over admitting.

    NOT a shell-syntax blacklist: it detects LANGUAGE + SHAPE — function words, a real
    sentence terminator (punctuation followed by space/EOL, which internal path dots are not),
    code-shaped line density, symbol ratio, and code fences — never enumerating shell tokens.
    """
    t = (text or "").strip()
    if not t:
        return False, "empty"
    # A fenced code block is computer output by definition.
    if _CODE_FENCE_RE.search(t):
        return False, "code_block"
    lines = [ln for ln in t.splitlines() if ln.strip()]
    if len(lines) > _NOTE_MAX_LINES:
        return False, "multi_line_dump"
    # A CONVERSATION TRANSCRIPT (multi-turn speaker labels: ``user:``/``assistant:``/``Alice:``) is
    # pasted chat — DATA, not a lesson. It is the one prose-shaped data type the symbol/line checks
    # below miss (it has function words, sentence punctuation, no paths/brackets). Detected here by
    # turn-taking SHAPE: >=2 lines are "<alpha-label>: <text>" AND those are at least half the block.
    # A single ``Note: foo`` line, a ``Step 1:`` procedure (digit), and a ``qwen3.5-9b:`` model id
    # all do NOT match, so genuine lessons are not collateral-damaged.
    if len(lines) >= _NOTE_MIN_SPEAKER_TURNS:
        sp = sum(1 for ln in lines if _SPEAKER_TURN_RE.match(ln))
        if sp >= _NOTE_MIN_SPEAKER_TURNS and sp * 2 >= len(lines):
            return False, "conversation_transcript"
    # A multi-line BLOCK (>=3 lines) must read as PROSE. A real multi-line lesson is sentences
    # (it carries sentence terminators + at most one incidental code-shaped line); a stack
    # traceback / log / config dump has NO sentence terminators (its dots are inside paths) and
    # many code-shaped frames. This is the structural seam that catches a multi-line shell
    # traceback even though its frames contain English words (File/line/in/main/call).
    if len(lines) >= _NOTE_BLOCK_MIN_LINES:
        if not _SENTENCE_END_RE.search(t):
            return False, "block_no_sentence_punct"
        code_lines = sum(1 for ln in lines if _line_is_code_shaped(ln))
        if code_lines > _NOTE_MAX_CODE_LINES:
            return False, "block_code_shaped"
    # Letter vs symbol density over the whole note (whitespace excluded).
    letters = sum(1 for ch in t if ch.isalpha())
    digits = sum(1 for ch in t if ch.isdigit())
    symbols = sum(1 for ch in t if (not ch.isalnum()) and (not ch.isspace()))
    alnum = letters + digits
    symbol_ratio = (symbols / alnum) if alnum else 1.0
    if symbol_ratio > _NOTE_MAX_SYMBOL_RATIO:
        return False, "symbol_heavy"
    # Grammatical structure: function-word hits on lowercased tokens.
    toks = _TOKEN_RE.findall(t.lower())
    fw = len({w for w in toks if w in _NOTE_FUNCWORDS})
    if fw < _NOTE_MIN_FUNCWORDS:
        return False, "no_grammar"
    return True, "prose"


def classify_note_admission(
    text: str,
    category: Optional[str] = None,
) -> dict:
    """Decide whether ``text`` may enter the operate lane (a real operating instruction) or
    must be REFUSED as DATA.

    Returns ``{"admit": bool, "reason": str, "lane": "instruction"|"data", "via": str}``
    where ``via`` is one of ``"gate"`` / ``"heuristic"``.

    Deterministic-only and LLM-free by construction: the cortex never makes an external LLM
    call and never resolves a tenant brain, even indirectly. The own-words tool mandate at
    the MCP layer is the primary injection defense (an agent-authored note cannot carry a
    pasted payload); this shape gate is the free backstop against direct/non-agent writes.

    The test is ``_looks_like_operational_prose``, FAIL-CLOSED: the safe default is REFUSE —
    the operate lane stays lessons-only, it does not default to admitting (the old
    accept-all behaviour that shipped pasted data into the lane). NEVER raises.
    """
    t = (text or "").strip()
    if not t:
        return {"admit": False, "reason": "empty", "lane": "data", "via": "gate"}
    # NO CATEGORY BYPASS. A caller-controlled ``category`` string must never skip the only
    # gate (with the LLM stage gone this IS the gate). The seeded usage_pattern rows are
    # written DIRECTLY by ``seed_usage_patterns`` (raw SQL, never through this function), so
    # the old ``usage_pattern`` fast-path admitted nothing legitimate — it was a magic-string
    # bypass on a model-reachable parameter. ``category`` is informational here only.
    admit, reason = _looks_like_operational_prose(t)
    return {"admit": bool(admit), "reason": reason,
            "lane": "instruction" if admit else "data", "via": "heuristic"}


# ── WRITE (manual capture) ─────────────────────────────────────────────────────────────

def _ensure_cortex_schema(user_id: str, schema: str) -> bool:
    """Make sure ``flagent_<uuid>.cortex_episodes`` EXISTS before we try to write to it.

    Returns True when the table is present (already, or after provisioning it here).

    Why this exists: the cortex schema is created at seat provisioning, but a seat that predates
    cortex — or whose provisioning half-ran — never got one. `note()` then INSERTed into a missing
    schema, the exception was swallowed by the outer fail-safe, and the note vanished. HTTP 200,
    "I couldn't save that operational note right now", no reason, no log. Silent data loss.
    `create_cortex_schema` is idempotent, so provisioning on first write is safe and cheap.
    """
    try:
        with _cortex_conn(schema) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s)", (f"{schema}.cortex_episodes",))
                if (cur.fetchone() or [None])[0] is not None:
                    return True
    except Exception as e:  # noqa: BLE001
        log.warning("cortex.note.schema_probe_failed", schema=schema, error=str(e)[:160])

    try:
        from src.provisioning.schema_manager import create_cortex_schema
        _schema, status = create_cortex_schema(user_id)
        if status != "ready":
            log_crit(log, "cortex.note.schema_provision_failed",
                     schema=schema, status=status,
                     detail="operational notes for this seat are being DROPPED")
            return False
        log.info("cortex.note.schema_lazy_provisioned", schema=schema)
        return True
    except Exception as e:  # noqa: BLE001
        log_crit(log, "cortex.note.schema_provision_error",
                 schema=schema, error=str(e)[:160],
                 detail="operational notes for this seat are being DROPPED")
        return False


def note(
    user_id: str,
    note: str,
    category: Optional[str] = None,
    tags: Any = None,
    context: Optional[dict] = None,
    evidence: Optional[dict] = None,
    client_source: Optional[str] = None,
    branch: Optional[str] = None,
    env: Optional[str] = None,
    owner_quote: Optional[str] = None,
    model: Optional[str] = None,
    note_type: Optional[str] = None,
    project: Optional[str] = None,
) -> bool:
    """Record ONE operational note (the /iremember door). Dedup-UPSERT: the SAME lesson
    (same category + normalized note text) strengthens ONE row and hardens it into a rule
    at ``observation_count >= HARDEN_THRESHOLD``.

    note is the DISTILLED RULE - "when <situation> arises, do <action>, because
    <mechanism>" - written by the agent in its own words. owner_quote (the user's exact
    words) is SECONDARY BY SCHEMA: it rides in context.owner_quote, never in the note
    body, so the stored row always LEADS with the rule (and the semantic lane embeds the
    distilled rule, not the user's chatter).

    branch is the line of work this lesson was learned on. Stored on the row and used
    ONLY as an additive ranking signal at recall (a same-branch note ranks above an
    equally-relevant other-branch one); it NEVER gates, filters, or excludes anything.

    Returns True iff a row was written/bumped. NEVER raises — a failure logs and returns
    False so the caller is never broken.
    """
    note_text = (note or "").strip()
    if not note_text:
        return False
    # CORRECTION-SHAPE INFERENCE (seam-5, critic B1): the caller's RAW category is what
    # carries "uncategorised" — _normalize_category maps None to the default ("gotcha")
    # below, so the inference MUST be captured BEFORE normalization or the gate at the
    # supersession block can never fire (measured: bare prose corrections stored as gotcha
    # and never tombstoned — the exact corpus-A defect, unfixed at runtime).
    _category_was_none = category is None
    category = _normalize_category(category)
    tag_list = _normalize_tags(tags)
    schema = cortex_schema_for(user_id)
    if not schema:
        log.debug("cortex.note.unresolved_schema", user=str(user_id)[:8])
        return False
    # LAZY PROVISION — the cortex schema may never have been created for this seat. It used to
    # simply not exist, and the INSERT below would raise, get swallowed by the fail-safe `except`,
    # and return False: EVERY note SILENTLY DROPPED, with the caller told only "couldn't save".
    # That is a "fail loud, never silent" violation and it quietly ate operational memory for a
    # whole seat. Provision on first write (create_cortex_schema is idempotent) and, if that still
    # fails, say so LOUDLY rather than pretending it was a transient hiccup.
    if not _ensure_cortex_schema(user_id, schema):
        return False
    # OWNER QUOTE rides in context, never in the note body (the schema IS the fix: a
    # quote demoted to a field cannot be the lesson). Bounded; empty means no quote carried.
    if isinstance(owner_quote, str) and owner_quote.strip():
        context = {**(context or {}), "owner_quote": owner_quote.strip()[:400]}
    ctx_json = None
    try:
        ctx_json = json.dumps(context) if context is not None else None
    except Exception:
        ctx_json = None
    # EVIDENCE — the artifact that produced this claim. `sha` is lifted into its own column so
    # staleness is DECIDABLE without parsing JSON. Everything else (ref, command, output) rides
    # the JSONB. A note without evidence is still recorded — never lose the lesson — it simply
    # carries less weight at recall and cannot self-date.
    ev_json = ev_sha = None
    # Defence in depth: an object param can reach us as a JSON STRING (MCP clients serialize
    # structured args inconsistently). Coerce rather than drop — never lose a lesson to a
    # transport quirk.
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except Exception:  # noqa: BLE001
            evidence = None
    if isinstance(evidence, dict) and evidence:
        try:
            ev_sha = (evidence.get("sha") or evidence.get("git_sha") or None)
            ev_sha = str(ev_sha)[:64] if ev_sha else None
            ev_json = json.dumps(evidence)
        except Exception:
            ev_json = ev_sha = None
    # ── client_source — WHICH MCP client (LLM/tool) wrote this note. Normalized to a trimmed
    # string; None on empty so the column stays NULL for untagged notes (recall's `IS NULL` lane
    # keeps them visible to everyone — never lose a lesson to a missing tag). The MCP layer does
    # not yet auto-populate from `clientInfo.name` (it is discarded at the initialize handshake),
    # so today this arrives ONLY when the model passes it explicitly.
    client_src = None
    if isinstance(client_source, str):
        client_src = client_source.strip() or None
    # branch - the line of work this lesson was learned on. Ranking signal only (see the
    # TIME + BRANCH block); trimmed and bounded, never munged (a branch is an opaque label
    # the caller knows: a git branch, a matter number, a ward name - all equally valid).
    branch_val = None
    if isinstance(branch, str):
        branch_val = branch.strip()[:64] or None
    # env - the ENVIRONMENT this lesson is true in ('truenas', 'p70', a ward, a lab).
    # Ranking signal only (see the ENV block above): same-env ranks first, other-env
    # demotes slightly and renders STAMPED, untagged stays neutral. Never a gate.
    env_val = None
    if isinstance(env, str):
        env_val = env.strip()[:64] or None
    # ── SCHEMA UPLIFT provenance: model / note_type / project. Free strings, trimmed and
    # bounded like branch/env; never an enum (note_type is deliberately LOOSE — emoji,
    # hyphens, spaces all pass; the fixed engine enum stays `category`). NULL on omit so
    # the columns stay honest about provenance that was never stated.
    def _free_meta(v) -> Optional[str]:
        return v.strip()[:64] or None if isinstance(v, str) else None
    model_val = _free_meta(model)
    ntype_val = _free_meta(note_type)
    project_val = _free_meta(project)

    # Branch column lazy-ensure (migration 261 back-fill for pre-existing seats).
    _ensure_branch_column(schema)
    # Postgres is source-of-truth for text/counters/lifecycle. The `embedding BYTEA` column
    # is now DORMANT (always NULL) — the vector lives in the per-seat Qdrant collection, not
    # the row. RETURN the row id so the point id == cortex_episodes.id.
    rid = obs_count = None
    lifecycle = LIFECYCLE_OBSERVED
    try:
        with _cortex_conn(schema) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO cortex_episodes"
                    "  (user_id, category, note, tags, context, source, client_source,"
                    "   lifecycle_state, observation_count, hit_count,"
                    "   evidence, evidence_sha, verified_at, branch, env,"
                    "   model, note_type, project)"
                    " VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, 1, 0,"
                    "         %s::jsonb, %s, CASE WHEN %s IS NULL THEN NULL ELSE now() END,"
                    "         %s, %s, %s, %s, %s)"
                    " ON CONFLICT (category, md5(lower(note))) DO UPDATE SET"
                    "   observation_count = cortex_episodes.observation_count + 1,"
                    "   last_seen_at = now(),"
                    # A RE-NOTE carrying fresh evidence RE-VERIFIES the claim against a newer
                    # commit — that is exactly how a note stays trustworthy instead of rotting.
                    # Never CLEAR existing evidence with a bare re-note (COALESCE keeps the old).
                    "   evidence = COALESCE(EXCLUDED.evidence, cortex_episodes.evidence),"
                    "   evidence_sha = COALESCE(EXCLUDED.evidence_sha,"
                    "                           cortex_episodes.evidence_sha),"
                    "   verified_at = COALESCE(EXCLUDED.verified_at,"
                    "                          cortex_episodes.verified_at),"
                    # client_source — never CLEAR an existing tag with a bare re-note that omits
                    # it (COALESCE keeps the old). A re-note carrying a fresh tag updates it.
                    "   client_source = COALESCE(EXCLUDED.client_source,"
                    "                            cortex_episodes.client_source),"
                    # branch - same COALESCE rule: a re-note carrying the branch re-stamps
                    # the line of work; a bare re-note never clears it.
                    "   branch = COALESCE(EXCLUDED.branch, cortex_episodes.branch),"
                    # env - same never-clear rule as branch/client_source.
                    "   env = COALESCE(EXCLUDED.env, cortex_episodes.env),"
                    # model / note_type / project — same never-clear rule; a re-note carrying
                    # the provenance re-stamps it, a bare re-note never clears it.
                    "   model = COALESCE(EXCLUDED.model, cortex_episodes.model),"
                    "   note_type = COALESCE(EXCLUDED.note_type, cortex_episodes.note_type),"
                    "   project = COALESCE(EXCLUDED.project, cortex_episodes.project),"
                    # a re-note may enrich tags/context; UNION tags, keep newest context.
                    "   tags = (SELECT ARRAY(SELECT DISTINCT unnest("
                    "             cortex_episodes.tags || EXCLUDED.tags))),"
                    "   context = COALESCE(EXCLUDED.context, cortex_episodes.context),"
                    # harden a repeated note into a RULE (never downgrade a hardened row).
                    "   lifecycle_state = CASE"
                    "     WHEN cortex_episodes.observation_count + 1 >= %s THEN 'hardened'"
                    "     WHEN cortex_episodes.lifecycle_state = 'decayed' THEN 'observed'"
                    "     ELSE cortex_episodes.lifecycle_state END"
                    " RETURNING id, observation_count, lifecycle_state, tags",
                    (user_id, category, note_text, tag_list, ctx_json,
                     SOURCE_AGENT_MANUAL, client_src, LIFECYCLE_OBSERVED,
                     ev_json, ev_sha, ev_sha,
                     branch_val, env_val, model_val, ntype_val, project_val,
                     HARDEN_THRESHOLD),
                )
                row = cur.fetchone()
                if row:
                    rid, obs_count, lifecycle, tag_list = row[0], row[1], row[2], list(row[3] or tag_list)
                # ── EXPLICIT SUPERSESSION (Build 5): a correction that shares a tag AND
                # >= 2 distinct CONTENT tokens with an older procedural note presumptively
                # REPLACES it — tombstone-link the old note to this row (never delete:
                # it stays queryable, recall EXCLUDES the tombstoned body — seam-5).
                # Deterministic and conservative on purpose: disjoint notes never link.
                # SEAM-5: the gate used to demand (explicit correction category) AND
                # (non-empty tags) — a conjunction real corrections routinely fail (the
                # ctx-bench driver writes bare prose, no category, no tags; measured:
                # stale 1.0 on corpus A). Now: correction-shape inference covers the
                # uncategorised case (_CORRECTION_SHAPE_RE above; captured pre-normalize
                # — critic B1: normalize maps None to gotcha, the None check was dead),
                # and the tag overlap is demoted from MANDATORY gate to a bonus
                # narrowing. CRITIC B2 HARDENING: the victim set is CAPPED (LIMIT 25,
                # newest-first) and the >= 2 bar counts CONTENT tokens only (stopwords
                # and sub-4-char fragments stripped BEFORE the count) — otherwise "not,
                # the" or a substring like deploy⊂deployment tombstones unrelated notes.
                _eff_correction = (
                    category == CATEGORY_CORRECTION
                    or (_category_was_none
                        and _CORRECTION_SHAPE_RE.search(note_text) is not None)
                )
                if rid is not None and _eff_correction:
                    try:
                        # CRITIC (SAME-FACT round, MAJOR): the plain newest-25 window
                        # structurally misses true old-value partners at corpus scale
                        # (652-note seat: untagged corrections narrow nothing, the window
                        # covers ~4% of the pool, and recency can never float a wrong
                        # note). Widen the window by VALUE: the correction's digit-bearing
                        # tokens (8010, 02:30, 512mb…) are exactly the old values a true
                        # correction restates — candidates whose note carries one enter
                        # the window regardless of age. Template-chain partners still
                        # come from the newest-N arm of the UNION.
                        _digit_toks = sorted({
                            _t for _t in _tokenize(note_text)
                            if any(_ch.isdigit() for _ch in _t) and len(_t) >= 2
                        })[:6]
                        _base = (
                            "SELECT id, note, last_seen_at FROM cortex_episodes"
                            " WHERE id <> %s AND superseded_by IS NULL"
                            "   AND lifecycle_state <> 'decayed'"
                            "   AND category = ANY(%s)"
                        )
                        _arms: list = []
                        _args: list = []
                        _arm1 = _base
                        _a1 = [rid, list(_SUPERSESSIBLE_CATEGORIES)]
                        if tag_list:
                            _arm1 += " AND tags && %s"
                            _a1.append(list(tag_list))
                        _arms.append(_arm1 + " ORDER BY last_seen_at DESC LIMIT 25")
                        _args.extend(_a1)
                        if _digit_toks:
                            _arm2 = (_base + " AND ("
                                     + " OR ".join(["note ILIKE %s" for _ in _digit_toks])
                                     + ")")
                            _a2 = ([rid, list(_SUPERSESSIBLE_CATEGORIES)]
                                   + [f"%{_t}%" for _t in _digit_toks])
                            if tag_list:
                                _arm2 += " AND tags && %s"
                                _a2.append(list(tag_list))
                            _arms.append(_arm2 + " ORDER BY last_seen_at DESC LIMIT 25")
                            _args.extend(_a2)
                        _victim_sql = ("SELECT id, note FROM (" + " UNION ".join(
                            "(" + _arm + ")" for _arm in _arms
                        ) + ") _w ORDER BY last_seen_at DESC LIMIT 40")
                        _victim_args = _args
                        cur.execute(_victim_sql, _victim_args)
                        # CONTENT tokens of THIS note (shape markers stripped — they mark
                        # the correction, they are not subject matter). The link decision
                        # is the _supersede_link_decision SAME-FACT test (template match
                        # or value restatement), validated against the A5 kept-seat
                        # corpus pairs: 47 false links die, all true chains survive.
                        _content_ordered = [
                            _t for _t in _tokenize(note_text)
                            if _t not in _STOPWORDS and len(_t) >= 4
                        ]
                        _content_tokens = set(_content_ordered)
                        _linked = []
                        for _cid, _cnote in (cur.fetchall() or []):
                            _ok, _why = _supersede_link_decision(
                                _content_ordered, _content_tokens, _cnote, note_text)
                            if _ok:
                                _linked.append(int(_cid))
                        if _linked:
                            cur.execute(
                                "UPDATE cortex_episodes SET superseded_by = %s"
                                " WHERE id = ANY(%s) AND superseded_by IS NULL",
                                (rid, _linked),
                            )
                    except Exception as _se:  # noqa: BLE001 — linking is best-effort
                        log.debug("cortex.note.supersession_failed", error=str(_se)[:120])
            conn.commit()
    except Exception as e:
        log.warning("cortex.note.failed", category=category,
                    note=note_text[:60], error=str(e)[:160])
        return False

    # ── SEMANTIC sync (mnemory pattern): embed + upsert to the PER-SEAT cortex collection.
    # STRICTLY best-effort AFTER the durable PG write — a Qdrant/embed failure never fails the
    # note (the reconcile sweep back-fills it later). Never the user's `faultline-<uuid>`.
    if rid is not None:
        try:
            collection, _ctflt = _cortex_partition(user_id)
            if collection:
                vec = _embed_cortex_text(note_text, task="search_document")
                if vec:
                    _ensure_cortex_collection(collection)  # lazy create (idempotent)
                    hardened = (lifecycle == LIFECYCLE_HARDENED) or \
                        (int(obs_count or 0) >= HARDEN_THRESHOLD)
                    _upsert_cortex_point(
                        collection, int(rid), vec,
                        _cortex_point_payload(user_id, int(rid), category, tag_list,
                                              note_text, hardened, int(obs_count or 1),
                                              model=model_val, note_type=ntype_val,
                                              project=project_val),
                        user_id=user_id,
                    )
        except Exception as e:  # pragma: no cover - defensive; PG row already durable
            log.debug("cortex.note.qdrant_sync_soft_fail", error=str(e)[:120])
    return True


# ── ENGAGEMENT METER — "is this agent WORKING?", measured independently of whether it LEARNS ──
#
# WHY THIS EXISTS (root cause, 2026-08-03)
# ---------------------------------------
# The reflection nudge below was built to catch the one failure that matters: a seat that is
# WORKING but never LEARNING. It measured "working" as `count(failed_command) >= 3`.
#
# `failed_command` rows only ever arrive through `/iremember`. So "is this agent working?" was
# being measured by *cortex writes* — the very thing whose absence is the defect. A seat that has
# never called `iremember` is therefore INDISTINGUISHABLE from a seat that does not exist, and
# lane (1) can never fire for it. Lane (2) needs `deliberate >= 3`, i.e. a seat ALREADY writing.
#
# NET EFFECT, and it is exactly backwards: **the nudge is silent for every seat that has never
# written a note, and speaks only to seats that already write.** The population it was built for
# is the one population it cannot reach. Confirmed in production: the owner's OpenWebUI seat
# (irecall 5x, iremember 0x, no client-side hook) sits precisely in the dead zone.
#
# THE FIX IS TO MEASURE THE TWO THINGS SEPARATELY:
#   * WORKING  ← tool CALLS, which happen whether or not the agent ever writes (this meter);
#   * LEARNING ← cortex writes (`cortex_episodes`, as before).
# A ratio between two independent signals is a real signal. A ratio of a table against itself is
# not. This is also, verbatim, the measurable the owner asked for: irecall : iremember, per seat.
#
# WHERE THE COUNT HAPPENS: inside calls the backend ALREADY serves, on a connection it ALREADY
# has open — `recall_usage_patterns` (rides every `recall_memory` turn) and `recall_notes` (rides
# every `/irecall`). No new transport, no new endpoint, no per-call round trip added.
#
# FIREWALL: the counter lives in the seat's own `flagent_<uuid>` cortex schema, bound WITHOUT
# `public` by `_cortex_conn`. It is agent-operational telemetry — never a user fact, never
# reachable from the user-fact walk.

# Signals counted. Deliberately NOT an open string set: an unknown signal is dropped, so a typo
# at a call site can never mint a phantom row that skews the nudge gate.
ENGAGEMENT_RECALL_MEMORY = "recall_memory"
ENGAGEMENT_IRECALL = "irecall"
ENGAGEMENT_NUDGE_SHOWN = "nudge_shown"
# WRITE-SIDE signals (reach-metric round 1 finding 4): the ratio "recalls before acting"
# vs "writes after correcting" is the metric, and it cannot be computed mechanically
# unless the WRITE half is counted too. Bumped backend-side at the always-hit write seams.
ENGAGEMENT_REMEMBER_FACTS = "remember_facts"
ENGAGEMENT_IREMEMBER = "iremember"
_VALID_ENGAGEMENT = frozenset({
    ENGAGEMENT_RECALL_MEMORY, ENGAGEMENT_IRECALL, ENGAGEMENT_NUDGE_SHOWN,
    ENGAGEMENT_REMEMBER_FACTS, ENGAGEMENT_IREMEMBER,
})

# Schemas whose engagement table this process has already ensured (one DDL probe per schema per
# process — same idiom as `_BACKSEEDED_SCHEMAS`, for the same reason: an ALREADY-PROVISIONED seat
# must pick the capability up without waiting for a re-provision).
_ENGAGEMENT_READY: set[str] = set()

_ENGAGEMENT_DDL = """
CREATE TABLE IF NOT EXISTS cortex_engagement (
    signal      text PRIMARY KEY,
    n           bigint      NOT NULL DEFAULT 0,
    first_at    timestamptz NOT NULL DEFAULT now(),
    last_at     timestamptz NOT NULL DEFAULT now()
)
"""


def _ensure_engagement_table(cur) -> None:
    """Create the per-seat counter table if absent. Caller supplies a cursor already bound to
    the cortex schema (``_cortex_conn`` refuses anything not ``flagent_``-shaped)."""
    cur.execute(_ENGAGEMENT_DDL)


def record_engagement(user_id: str, signal: str, cur=None) -> None:
    """Count one engagement signal for this seat. NEVER raises, NEVER blocks a caller.

    ``cur`` — an OPEN cursor already bound to this seat's cortex schema. Pass it whenever the
    caller already holds one (the normal case) so this costs one UPSERT and no new connection.
    Omit it and a short-lived connection is opened.

    A telemetry write must never be able to break recall, so every failure path here is a
    debug-log-and-return. It is also why the counter is not authoritative for anything: it
    informs a nudge and a report, never a product decision about user truth.
    """
    if signal not in _VALID_ENGAGEMENT:
        return
    schema = cortex_schema_for(user_id)
    if not schema:
        return

    def _bump(c) -> None:
        if schema not in _ENGAGEMENT_READY:
            _ensure_engagement_table(c)
            _ENGAGEMENT_READY.add(schema)
        c.execute(
            "INSERT INTO cortex_engagement (signal, n) VALUES (%s, 1)"
            " ON CONFLICT (signal) DO UPDATE"
            "   SET n = cortex_engagement.n + 1, last_at = now()",
            (signal,),
        )

    try:
        if cur is not None:
            _bump(cur)
            return
        with _cortex_conn(schema) as conn:
            with conn.cursor() as c:
                _bump(c)
            conn.commit()
    except Exception as e:  # noqa: BLE001 — telemetry must never break the caller
        # The table may simply not exist yet on a read-only replica, or the schema may be
        # mid-provision. Drop the schema from the ready-set so the next call re-probes.
        _ENGAGEMENT_READY.discard(schema)
        log.debug("cortex.engagement.bump_failed", signal=signal, error=str(e)[:120])


def engagement_snapshot(user_id: str) -> dict:
    """THE MEASURABLE. Per-seat: how often the agent REACHED for memory vs how often it WROTE.

    Returns ``{"calls": {...}, "deliberate_notes": int, "auto_notes": int,
    "usage_patterns": int, "starving": bool}``. Fail-safe: unresolved/absent cortex → zeros.

    ``starving`` is the product question in one boolean: this agent is demonstrably working and
    has recorded nothing of its own. It is what `reflection_nudge` gates on and what the
    per-seat report prints.
    """
    empty = {"calls": {}, "deliberate_notes": 0, "auto_notes": 0,
             "usage_patterns": 0, "starving": False}
    schema = cortex_schema_for(user_id)
    if not schema:
        return empty
    try:
        with _cortex_conn(schema) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s)", (f"{schema}.cortex_episodes",))
                if (cur.fetchone() or [None])[0] is None:
                    return empty
                # Pure read, like the nudge: never create, never commit. An absent table means
                # this seat has not been through a counted call yet — zeros, not an error.
                calls: dict[str, int] = {}
                cur.execute("SELECT to_regclass(%s)", (f"{schema}.cortex_engagement",))
                if (cur.fetchone() or [None])[0] is not None:
                    cur.execute("SELECT signal, n FROM cortex_engagement")
                    calls = {str(s): int(n or 0) for s, n in (cur.fetchall() or [])}
                cur.execute(
                    "SELECT"
                    "  count(*) FILTER (WHERE category = ANY(%s)),"
                    "  count(*) FILTER (WHERE category = %s),"
                    "  count(*) FILTER (WHERE category = %s)"
                    " FROM cortex_episodes WHERE lifecycle_state <> 'decayed'",
                    (list(_DELIBERATE_CATEGORIES), CATEGORY_FAILED_COMMAND,
                     USAGE_PATTERN_CATEGORY),
                )
                row = cur.fetchone() or (0, 0, 0)
    except Exception as e:  # noqa: BLE001
        log.debug("cortex.engagement.snapshot_failed", error=str(e)[:140])
        return empty
    deliberate, auto, seeded = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
    reached = (calls.get(ENGAGEMENT_RECALL_MEMORY, 0) + calls.get(ENGAGEMENT_IRECALL, 0))
    return {
        "calls": calls,
        "deliberate_notes": deliberate,
        "auto_notes": auto,
        "usage_patterns": seeded,
        "starving": bool(deliberate == 0 and reached >= _NUDGE_MIN_REACHED),
    }


# ── REFLECTION NUDGE — the model-agnostic third lane ─────────────────────────────────────
# Two capture lanes are naturally automatic in any client: a command FAILED (auto
# `failed_command`), and the USER CORRECTED you (auto `correction`). The lane that is always
# lost is the one you WORK OUT BY READING — nothing failed, nobody corrected you, and the
# insight evaporates on session end. A client-side hook can force it (Claude Code), but
# FaultLine is model-agnostic: it cannot reach into an arbitrary model's turn loop.
#
# It can do something better. FaultLine can SEE what no client can: a seat whose cortex is
# accruing auto-captured failures while its DELIBERATE lane sits at zero — an agent that is
# working but never reflecting. One line in the HOW-YOU-OPERATE brief fixes that, for ANY
# model over MCP.
#
# SELF-LIMITING BY CONSTRUCTION (a nag is worse than nothing — the model learns to ignore it):
# the nudge STOPS PERMANENTLY the moment the seat records a single deliberate note. It only
# fires when the signal is REAL (the agent has actually been failing at things and learning
# nothing from it), never on a fresh/idle seat.
_REFLECTION_NUDGE = (os.getenv("CORTEX_REFLECTION_NUDGE", "1").strip().lower()
                     not in ("0", "false", "no", "off"))
# How many auto-captured failures constitute "this agent is working and hitting things".
_NUDGE_MIN_AUTO = int(os.getenv("CORTEX_NUDGE_MIN_AUTO", "3") or 3)

# ── LANE 0 — the hook-less seat (the one the nudge could never reach) ─────────────────────
# How many times a seat must have REACHED for memory before "working but not learning" is a
# real signal rather than a fresh seat. Counted from the engagement meter above, which does not
# depend on the agent ever writing — the whole point.
_NUDGE_MIN_REACHED = int(os.getenv("CORTEX_NUDGE_MIN_REACHED", "5") or 5)

# HARD DELIVERY CAP — the anti-wallpaper rule, and it is not optional.
#
# The existing nudge is self-limiting only in the sense that it stops once the seat writes ONE
# note. If the model simply ignores it, it repeats on EVERY recall turn forever — which is the
# exact failure mode of "just instruct it harder": unasked content that costs tokens every
# interaction and gets tuned out. A nudge that has been shown N times and changed nothing is not
# going to work on attempt N+1; it has become wallpaper, and wallpaper devalues every other line
# in the brief it sits next to.
#
# So the nudge is capped ABSOLUTELY, per seat, for the life of the seat. After the cap it goes
# quiet permanently whether or not the seat ever wrote anything. If a capped seat is still
# starving, that is a real finding for the owner — visible in `engagement_snapshot` — and the
# answer is a product change, not a louder nag.
_NUDGE_MAX_SHOWS = int(os.getenv("CORTEX_NUDGE_MAX_SHOWS", "3") or 3)

_NUDGE_STARVING = "You have reached for memory {n} times this seat and never recorded a note of " \
                  "your own. The moment you learn something about how to work with this user — " \
                  "they correct you, they want it done differently, something worked on the " \
                  "second try — call iremember with it in one sentence. What you do not write " \
                  "down, they must teach you again next session."

_NUDGE_DELIBERATE = "You have hit {n} failures but recorded no lessons of your own. When you " \
                    "work something out — a trap, a wrong assumption, a procedure — record it " \
                    "with iremember (gotcha|pitfall|howto). A failure you learn nothing from is " \
                    "one you will repeat."
_NUDGE_EVIDENCE = "Your notes carry no evidence. When you record a lesson, bind it to its proof " \
                  "(evidence: sha/ref/command/output) — an unverifiable claim rots into a trap " \
                  "that sends a future you chasing work that was already done."


def reflection_nudge(user_id: str) -> Optional[str]:
    """ONE operating line when a seat's cortex shows it is working but not learning.

    Returns None (silent) unless the signal is real. Never raises — this is a nudge, and a
    nudge that breaks recall is a bug.
    """
    if not _REFLECTION_NUDGE:
        return None
    schema = cortex_schema_for(user_id)
    if not schema:
        return None
    try:
        with _cortex_conn(schema) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s)", (f"{schema}.cortex_episodes",))
                if (cur.fetchone() or [None])[0] is None:
                    return None
                cur.execute(
                    "SELECT"
                    # deliberate = a lesson the agent CHOSE to record. `failed_command` is
                    # excluded on purpose: it is auto-captured, so counting it would let the
                    # machinery satisfy the gate instead of the thinking.
                    "  count(*) FILTER (WHERE category = ANY(%s)),"
                    "  count(*) FILTER (WHERE category = %s),"
                    "  count(*) FILTER (WHERE evidence_sha IS NOT NULL)"
                    " FROM cortex_episodes"
                    " WHERE lifecycle_state <> 'decayed' AND category <> %s",
                    (list(_DELIBERATE_CATEGORIES), CATEGORY_FAILED_COMMAND,
                     USAGE_PATTERN_CATEGORY),
                )
                row = cur.fetchone() or (0, 0, 0)
                deliberate, auto, evidenced = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
                # The engagement meter — "has this agent been WORKING", measured independently
                # of whether it has ever written. Same connection, no extra round trip.
                #
                # PURE READ: this function never creates the table and never commits. A nudge
                # that can fail is a nudge that can break recall, and DDL on the read path would
                # also mean an aborted transaction poisons the counts fetched just above. The
                # writer (`record_engagement`) owns creation; an absent table simply reads zero,
                # which correctly reports a seat that has not been counted yet as "not working".
                reached = shown = 0
                cur.execute("SELECT to_regclass(%s)", (f"{schema}.cortex_engagement",))
                if (cur.fetchone() or [None])[0] is not None:
                    cur.execute(
                        "SELECT"
                        "  coalesce(sum(n) FILTER (WHERE signal = ANY(%s)), 0),"
                        "  coalesce(sum(n) FILTER (WHERE signal = %s), 0)"
                        " FROM cortex_engagement",
                        ([ENGAGEMENT_RECALL_MEMORY, ENGAGEMENT_IRECALL],
                         ENGAGEMENT_NUDGE_SHOWN),
                    )
                    erow = cur.fetchone() or (0, 0)
                    reached, shown = int(erow[0] or 0), int(erow[1] or 0)
    except Exception as e:  # noqa: BLE001 — a nudge must never break recall
        log.debug("cortex.reflection_nudge.failed", error=str(e)[:120])
        return None

    # (0) THE HOOK-LESS SEAT — working (it keeps reaching for memory) and learning nothing.
    #
    # This is the lane that reaches an OpenWebUI-hosted model, a local Qwen, or any client with
    # no auto-capture hook: it gates on tool CALLS, which every client makes, instead of on
    # `failed_command` rows, which only a hooked client produces. Ordered FIRST because it is
    # strictly the more general signal — a seat that satisfies (1) satisfies this too.
    #
    # Capped absolutely (see _NUDGE_MAX_SHOWS): silence is the correct end-state for a nudge
    # that has been read and ignored.
    if deliberate == 0 and reached >= _NUDGE_MIN_REACHED and shown < _NUDGE_MAX_SHOWS:
        record_engagement(user_id, ENGAGEMENT_NUDGE_SHOWN)
        return _NUDGE_STARVING.format(n=reached)
    # (1) Working, but never reflecting — the hooked-client lane (auto-captured failures).
    if deliberate == 0 and auto >= _NUDGE_MIN_AUTO:
        return _NUDGE_DELIBERATE.format(n=auto)
    # (2) Reflecting, but on faith — no note can be checked, so every note is a future trap.
    if deliberate >= _NUDGE_MIN_AUTO and evidenced == 0:
        return _NUDGE_EVIDENCE
    return None


# ── READ (deterministic keyword + tag recall) ────────────────────────────────────────────

def _score_candidate_rows(
    rows: list,
    q_tokens: list[str],
    q_emb_present: bool,
    sem_by_id: dict[int, float],
    limit: int,
    branch_now: Optional[str] = None,
    env_now: Optional[str] = None,
) -> list[dict]:
    """Score + rank a fetched candidate field (shared by recall_notes and
    recall_usage_patterns). Deterministic given fixed embeddings.

    rows are (id, category, note, tags, context, source, lifecycle_state,
    observation_count, last_seen_at[, evidence_sha, evidence, verified_at][, branch,
    first_seen_at]) tuples (length-tolerant). Signals: keyword/tag overlap + the RELATIVE
    semantic component (from sem_by_id) form the REAL-MATCH BAR (a discriminating query
    with zero relevance is dropped - honest miss); provenance, time and branch are ADDITIVE
    reorder terms applied ONLY to rows that already cleared the bar - they can never admit,
    drop, or gate a note (see the TIME + BRANCH block above for the citations). Returns the
    top limit recs."""
    is_discriminating = bool(q_tokens or q_emb_present)

    # -- FIELD-LEVEL time signal: the newest correction in THIS candidate field ----------
    # A candidate field is query-scoped = one topic. A procedural note older than the newest
    # correction on the same topic is presumptively the procedure that correction replaced
    # (Koren 2009: the newest interaction is the strongest evidence of current behaviour).
    # Precomputed over CREATION time (first_seen_at), which - unlike the write clock - is
    # stable, so a note that keeps surfacing can never age-out the correction that replaced it.
    newest_correction_first_seen = None
    _corr_ts = [
        r[13].timestamp() for r in rows
        if len(r) > 13 and r[13] is not None and r[1] == CATEGORY_CORRECTION
    ]
    if _corr_ts:
        newest_correction_first_seen = max(_corr_ts)

    scored: list[tuple[tuple, dict]] = []
    for r in rows:
        # LENGTH-TOLERANT unpack: recall_notes fetches evidence + branch + first_seen (14),
        # while recall_usage_patterns (the HOW-YOU-OPERATE brief) still fetches the base 9.
        # Both share this scorer, so a fixed-arity unpack would break the operating-brief
        # channel.
        (rid, category, ntext, tags, context, source,
         lifecycle, obs_count, last_seen) = r[:9]
        ev_sha = r[9] if len(r) > 9 else None
        evidence = r[10] if len(r) > 10 else None
        verified_at = r[11] if len(r) > 11 else None
        branch = r[12] if len(r) > 12 else None
        first_seen = r[13] if len(r) > 13 else None
        env = r[14] if len(r) > 14 else None
        sup_note = r[15] if len(r) > 15 else None
        model = r[16] if len(r) > 16 else None
        note_type = r[17] if len(r) > 17 else None
        project = r[18] if len(r) > 18 else None
        tags = tags or []
        hardened = (lifecycle == LIFECYCLE_HARDENED) or (int(obs_count or 0) >= HARDEN_THRESHOLD)
        rec = {
            "category": category,
            "note": ntext,
            "tags": list(tags),
            "context": context,
            "source": source,
            "lifecycle_state": lifecycle,
            "observation_count": int(obs_count or 0),
            "hardened": bool(hardened),
            "last_seen_at": last_seen.isoformat() if last_seen else None,
            # EVIDENCE travels WITH the note. The reader can now check whether the claim is
            # still current (git merge-base --is-ancestor <evidence_sha> HEAD) instead of
            # taking it on faith - which is the whole point: a stale claim must be checkable,
            # not merely believable.
            "evidence_sha": ev_sha,
            "evidence": evidence,
            "verified_at": verified_at.isoformat() if verified_at else None,
            # TIME + BRANCH ride along IN THE RECORD as ranking signals only - surfaced so a
            # reader can see why a note ranked where it did (and on which line of work it was
            # learned), never used to gate anything.
            "branch": branch,
            "first_seen_at": first_seen.isoformat() if first_seen else None,
            # ENV + SUPERSESSION ride along as ranking signals and render stamps.
            "env": env,
            "superseded_by_note": (sup_note or "").strip() or None if sup_note else None,
            # SCHEMA UPLIFT provenance (owner directive): WHO recorded it, the owner-facing
            # free-form type, the project — plus the note's DATE as a computable age so the
            # renderer can stamp '(180d)'. Schema-tolerant: legacy rows (NULL) read fine.
            "model": model,
            "type": note_type,
            "project": project,
            "age_days": (max(0, (_time.time() - last_seen.timestamp()) / 86400.0)
                         if last_seen is not None else None),
        }
        # -- keyword/tag overlap (deterministic) --
        _tag_hits, _note_hits = _keyword_hits(q_tokens, ntext, tags)
        kw_score = _tag_hits * 3 + _note_hits
        # -- semantic (CORTEX-ONLY; RELATIVE-rerank component from the ANN field) --
        sem_component = float(sem_by_id.get(int(rid), 0.0)) if rid is not None else 0.0
        # -- REAL-MATCH BAR (the drop gate) --
        # A note clears the bar ONLY on genuine relevance: keyword/tag overlap OR a semantic
        # cosine ABOVE the floor. Provenance/time/branch are DELIBERATELY excluded here -
        # they are ranking bonuses, never admission. A discriminating query (tokens and/or an
        # embedding) with zero relevance is an honest miss -> drop. A bare recall (no tokens,
        # no embedding) keeps everything (fail-safe when the embedder is down).
        relevance = kw_score + sem_component
        if is_discriminating and relevance <= 0.0:
            continue
        # -- provenance weight (Build 2) - applied ONLY to notes that ALREADY cleared the
        # real-match bar above, so it can REORDER relevant notes but can NEVER lift a
        # below-threshold note into the results (a hardened/correction note with no real
        # match to the query does not surface). Keyed on the FIXED category enum only. --
        prov = _PROVENANCE_WEIGHT.get(category, _DEFAULT_PROVENANCE_WEIGHT)
        # -- TIME (Build 4): the decay factor on the WRITE clock --
        f = _recency_factor(last_seen)
        # Repetition earned in the PAST is past evidence of hotness, not present practice -
        # the hardened bump DECAYS with the same factor (Redis LFU's decaying counter; the
        # undecayed version is exactly how a stale-but-once-hot note pinned the head).
        if hardened:
            prov += _HARDENED_BONUS * f
        # A lesson you can CHECK beats a lesson you must take on faith. Same rule as provenance:
        # additive, applied ONLY to rows that already cleared the real-match bar, so evidence
        # REORDERS relevant notes and can never lift an irrelevant one into the results.
        # (Evidence does NOT decay with wall-clock time: verified_at/sha make staleness
        # DECIDABLE at read time, which is strictly better than guessing it at rank time.)
        if ev_sha:
            prov += _EVIDENCE_BONUS
        # RECENCY completes the Park et al. triad (recency x importance x relevance): a note
        # whose clock says it was just re-earned outranks an equally-relevant stale one.
        prov += _FRESHNESS_BONUS * f
        # -- BRANCH (Build 4): same-line-of-work notes rank higher; mismatch NEVER penalizes --
        if branch_now and branch and str(branch).strip() == str(branch_now).strip():
            prov += _BRANCH_MATCH_BONUS
        # ── ENV (Build 5): same-env ranks first; ANOTHER env demotes slightly (the note is
        # env-BOUND knowledge that may simply not apply here — but its mechanism often
        # transfers, so demote-not-drop); untagged stays neutral. The render stamps the
        # env either way, so the agent SEES why a note ranked where it did.
        if env_now:
            _env_s = str(env).strip() if env else ""
            if _env_s and _env_s == str(env_now).strip():
                prov += _ENV_MATCH_BONUS
            elif _env_s:
                prov -= _ENV_MISMATCH_DEMOTE
        # -- SUPERSESSION (Build 4): procedure written BEFORE the newest correction on this
        # topic is presumptively superseded by it - demote, never drop. One-directional on
        # purpose: procedure written AFTER a correction is presumptively already-corrected
        # knowledge. This is the term that makes the replayed stale-vs-fresh case resolve for
        # the correction at ANY age gap, including a same-day one.
        if (
            newest_correction_first_seen is not None
            and category != CATEGORY_CORRECTION
            and first_seen is not None
            and first_seen.timestamp() < newest_correction_first_seen
        ):
            prov -= _SUPERSEDED_DEMOTE
        # Bare-recall (non-discriminating) notes have relevance 0; provenance orders them but
        # since NONE cleared a match bar they all stand equally on provenance/recency - this
        # is the intended "show me everything, best-provenance first" path, not a lift.
        final_score = relevance + prov
        # sort key: score desc, hardened desc, obs_count desc, recency desc
        _ls = last_seen.timestamp() if last_seen else 0.0
        sort_key = (final_score, 1 if hardened else 0, int(obs_count or 0), _ls)
        scored.append((sort_key, rec))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [rec for _k, rec in scored[:limit]]


def _fetch_lesson_candidates(schema: str, client_source: Optional[str] = None) -> list:
    """The LESSON slice candidate field (the rows ``/irecall`` serves): every non-decayed
    cortex episode EXCEPT the seeded HOW-YOU-OPERATE usage patterns.

    Factored out so the pull lane (``recall_notes``) and the push lane (``experience_lines``)
    read the SAME slice through the SAME query — a second hand-written SELECT is how the two
    lanes would silently diverge on what "the cortex" means. Returns [] on any error (never
    raises). Bound to ``flagent_<uuid>`` WITHOUT ``public`` by ``_cortex_conn`` — the firewall.

    ``client_source`` (optional) — when provided, restricts the field to notes tagged with
    that client PLUS untagged notes (``client_source = %s OR client_source IS NULL``). The
    "mine" lane never loses a lesson to a missing tag (legacy rows stay visible); it only
    EXCLUDES notes explicitly tagged to ANOTHER client. When None (today's behaviour), no
    filter — all notes from the seat.
    """
    try:
        with _cortex_conn(schema) as conn:
            with conn.cursor() as cur:
                if client_source:
                    cur.execute(
                        # ENV + SUPERSESSION ride along (Build 5): env as a ranking
                        # signal + render stamp; sup.note so an outdated note can render
                        # WHAT replaced it without a second lookup. Aliased/qualified
                        # because the join brings a second copy of every column.
                        "SELECT c.id, c.category, c.note, c.tags, c.context, c.source,"
                        "       c.lifecycle_state, c.observation_count, c.last_seen_at,"
                        "       c.evidence_sha, c.evidence, c.verified_at,"
                        "       c.branch, c.first_seen_at, c.env, sup.note,"
                        "       c.model, c.note_type, c.project"
                        " FROM cortex_episodes c"
                        " LEFT JOIN cortex_episodes sup ON sup.id = c.superseded_by"
                        " WHERE c.lifecycle_state <> 'decayed'"
                        # SEAM-5: a TOMBSTONED note (superseded_by set) no longer renders.
                        # Build 5 used to render the stale body with an "OUTDATED" stamp —
                        # measured: the stale value still poisoned contexts (cortex-lane
                        # stale exposure 1.0 on corpus A). The replacement note now carries
                        # the truth alone; the tombstone stays queryable in PG for audit.
                        "   AND c.superseded_by IS NULL"
                        # EXCLUDE the seeded HOW-YOU-OPERATE usage-pattern slice — those are the
                        # model's operating manual, delivered by recall_usage_patterns on the
                        # recall path, NOT the agent's private /irecall lessons. Keeping them out
                        # here keeps the two slices firewalled by category as well as by delivery.
                        "   AND category <> %s"
                        # MINE lane: notes from THIS client + untagged (never lose a lesson to a
                        # missing tag). Untagged rows are visible to EVERY client because the
                        # column is NULL on legacy rows and on any note the model didn't tag.
                        "   AND (c.client_source = %s OR c.client_source IS NULL)"
                        " ORDER BY c.last_seen_at DESC"
                        " LIMIT %s",
                        (USAGE_PATTERN_CATEGORY, client_source, _RECALL_CANDIDATE_CAP),
                    )
                else:
                    cur.execute(
                        # ENV + SUPERSESSION ride along (Build 5): env as a ranking
                        # signal + render stamp; sup.note so an outdated note can render
                        # WHAT replaced it without a second lookup. Aliased/qualified
                        # because the join brings a second copy of every column.
                        "SELECT c.id, c.category, c.note, c.tags, c.context, c.source,"
                        "       c.lifecycle_state, c.observation_count, c.last_seen_at,"
                        "       c.evidence_sha, c.evidence, c.verified_at,"
                        "       c.branch, c.first_seen_at, c.env, sup.note,"
                        "       c.model, c.note_type, c.project"
                        " FROM cortex_episodes c"
                        " LEFT JOIN cortex_episodes sup ON sup.id = c.superseded_by"
                        " WHERE c.lifecycle_state <> 'decayed'"
                        # SEAM-5: tombstoned bodies never render (see the twin branch above).
                        "   AND c.superseded_by IS NULL"
                        # EXCLUDE the seeded HOW-YOU-OPERATE usage-pattern slice — those are the
                        # model's operating manual, delivered by recall_usage_patterns on the
                        # recall path, NOT the agent's private /irecall lessons. Keeping them out
                        # here keeps the two slices firewalled by category as well as by delivery.
                        "   AND c.category <> %s"
                        " ORDER BY c.last_seen_at DESC"
                        " LIMIT %s",
                        (USAGE_PATTERN_CATEGORY, _RECALL_CANDIDATE_CAP,),
                    )
                return cur.fetchall()
    except Exception as e:
        log.debug("cortex.fetch_lesson_candidates.failed", error=str(e)[:160])
        return []


def recall_notes(user_id: str, query: Optional[str] = None, limit: int = 8,
                 bump: bool = True, client_source: Optional[str] = None,
                 branch: Optional[str] = None,
                 env: Optional[str] = None) -> list[dict]:
    """HYBRID recall of operational notes (keyword+tag + semantic ANN rerank + provenance).

    Signals COMBINED into one score (semantic ADDS to keyword; it does not replace it):
      * keyword/tag  — a query's tokens vs each note's ``tags`` (strong, x3) and ``note``
        text (bidirectional substring, x1).
      * semantic     — a PER-SEAT Qdrant ANN (HNSW) top-K over the cortex collection,
        RERANKED by a RELATIVE score: a note's vector signal counts only when its cosine
        clearly STANDS OUT from the candidate FIELD (margin + z-score), NOT above a flat
        absolute floor (``_relative_semantic_components``). CORTEX-ONLY; the user-truth walk
        and the user's ``faultline-<uuid>`` collection are never touched. This is what finds
        "what port for the model" → the ``:8080`` note even when it was NOT tagged.
      * provenance   — an additive weight by HOW the note was learned (a ``correction`` /
        hardened rule outranks an inferred ``failed_command``), applied only to relevant
        notes so it reorders, never manufactures a match.

    A discriminating query keeps a note iff keyword OR (relatively-admitted) semantic
    relevance > 0 (else an honest miss → dropped). Bare query (no tokens) →
    most-relevant/recent by provenance-then-hardened-then-recency. FAIL-SAFE: Qdrant/embedder
    unreachable → the semantic term is 0 and recall degrades to keyword+tag+provenance.
    NEVER raises. Given fixed embeddings the ranking is deterministic.

    ``bump`` (default True = today's behaviour) controls the recall-relevance write-back:
    it advances ``hit_count`` ONLY. It used to also set ``last_seen_at = now()``, which let a
    READ launder a note age (a stale note that kept surfacing never aged and pinned itself at
    the head of the ``ORDER BY last_seen_at DESC LIMIT _RECALL_CANDIDATE_CAP`` window forever)
    — retired by the write-clock discipline; age now advances only through WRITES (a re-note
    or an evidence refresh). A DELIBERATE call (a model, or a human, asking /irecall) is a
    genuine relevance signal and keeps the bump; an unattended lane must pass ``bump=False``
    (see ``experience_lines``, which never bumps).

    ``client_source`` (default None = today's behaviour — all notes from the seat). When
    provided, the candidate field is restricted to notes tagged with that client PLUS
    untagged notes (the "mine" lane: ``client_source = %s OR client_source IS NULL``).
    Untagged notes stay visible to every client so a missing tag never loses a lesson; the
    lane only EXCLUDES notes explicitly tagged to ANOTHER client.
    """
    schema = cortex_schema_for(user_id)
    if not schema:
        return []
    try:
        limit = max(1, min(int(limit or 8), 50))
    except Exception:
        limit = 8
    q_tokens = [t for t in _tokenize(query) if t not in _STOPWORDS]
    has_query = bool(query and str(query).strip())
    # Embed the RAW query once (best-effort; None → semantic lane off for this call).
    q_emb = _embed_cortex_text(query, task="search_query") if has_query else None
    # ── SEMANTIC lane: ANN over the per-seat cortex collection → RELATIVE rerank ──
    # {row_id: semantic_component} for notes that clearly stand out of the ANN field. Empty
    # on any Qdrant/embed miss → recall degrades to keyword+tag (fail-safe, never errors).
    sem_by_id: dict[int, float] = {}
    if q_emb is not None:
        collection, _ctflt = _cortex_partition(user_id)
        if collection:
            sem_by_id = _relative_semantic_components(_search_cortex_ann(collection, q_emb, _ctflt))
    rows = _fetch_lesson_candidates(schema)

    # ENGAGEMENT: count only an ATTENDED read (``bump=True``) — the model, or a human, actually
    # ASKING /irecall. An unattended per-turn client hook passes ``bump=False`` and must not
    # inflate "this agent is working": the whole signal is about what the AGENT chose to do.
    if bump:
        record_engagement(user_id, ENGAGEMENT_IRECALL)

    top = _score_candidate_rows(rows, q_tokens, q_emb is not None, sem_by_id, limit,
                                 branch_now=branch, env_now=env)

    # bump hit_count on the surfaced notes (recall-relevance signal) — best-effort.
    if bump and has_query and top:
        try:
            with _cortex_conn(schema) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        # WRITE-CLOCK DISCIPLINE: bump hit_count ONLY. The old bump also
                        # set last_seen_at = now(), which let a READ launder a note age -
                        # a stale note that kept surfacing never aged, and pinned itself at
                        # the head of the ORDER BY last_seen_at window forever (replayed in
                        # DEV/replay_stale_vs_fresh.py). Age now advances only through WRITES.
                        "UPDATE cortex_episodes SET hit_count = hit_count + 1"
                        " WHERE category = ANY(%s) AND note = ANY(%s)",
                        ([t["category"] for t in top], [t["note"] for t in top]),
                    )
                conn.commit()
        except Exception:
            pass
    return top


# ── Lifecycle sweep (harden / decay) ─────────────────────────────────────────────────────

def sweep_lifecycle_for_user(user_id: str) -> dict:
    """Harden / decay / embedding-back-fill sweep for ONE seat's cortex (called by the
    re_embedder poll loop).

    * HARDEN: observed rows with observation_count >= threshold → 'hardened'.
    * DECAY (Build 3, quality-over-volume, DETERMINISTIC): rows past ``expires_at`` →
      'decayed'; PLUS stale low-value auto-capture: ``observed`` + decay-eligible category
      (``failed_command``) + ``observation_count`` below the harden threshold +
      ``hit_count <= _DECAY_MAX_HITS`` + ``last_seen_at`` older than ``_DECAY_AGE_DAYS``.
      Corrections / hardened rules / high-hit notes are NEVER decayed (the gold stays
      durable). No fuzzy scoring — a category filter + counters + an age cut.
    * RECONCILE (mnemory sync back-fill): re-upsert to the per-seat cortex Qdrant collection
      any non-decayed row whose point is MISSING from the collection (covers notes written
      while Qdrant or the embedder was down). Cheap: scroll the small collection once, embed
      only the drift rows. No-op / fail-safe if Qdrant is still unreachable. Also drops the
      Qdrant points for rows that decayed this sweep (keep the vector field clean).

    Opens its OWN flagent_<uuid> connection WITHOUT public (and its OWN cortex collection).
    NEVER raises — returns ``{"hardened": n, "decayed": n, "embedded": n}`` (zeros on any
    error / absent schema). ``embedded`` counts Qdrant re-upserts this sweep.
    """
    out = {"hardened": 0, "decayed": 0, "embedded": 0}
    schema = cortex_schema_for(user_id)
    if not schema:
        return out
    try:
        with _cortex_conn(schema) as conn:
            # Guard: the schema may not exist yet (fail-safe until provisioned).
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s)", (f"{schema}.cortex_episodes",))
                if (cur.fetchone() or [None])[0] is None:
                    return out
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE cortex_episodes SET lifecycle_state = 'hardened'"
                    " WHERE lifecycle_state = 'observed'"
                    # CRITIC B6: a TOMBSTONED note is outdated truth — repetition hardens
                    # live rules, never superseded ones (audit honesty; render is already
                    # tombstone-free, this keeps the lifecycle column truthful too).
                    "   AND superseded_by IS NULL"
                    "   AND observation_count >= %s",
                    (HARDEN_THRESHOLD,),
                )
                out["hardened"] = cur.rowcount or 0
                # (i) explicit-expiry decay (a future decay-eligible source may set expires_at).
                cur.execute(
                    "UPDATE cortex_episodes SET lifecycle_state = 'decayed'"
                    " WHERE lifecycle_state <> 'decayed'"
                    "   AND expires_at IS NOT NULL AND expires_at <= now()",
                )
                out["decayed"] = cur.rowcount or 0
                # (ii) quality decay: stale + low-hit + not-near-a-rule, only for the
                # decay-eligible (weakest) categories. Never touches hardened/correction/
                # high-hit rows.
                cur.execute(
                    "UPDATE cortex_episodes SET lifecycle_state = 'decayed'"
                    " WHERE lifecycle_state = 'observed'"
                    "   AND category = ANY(%s)"
                    "   AND observation_count < %s"
                    "   AND hit_count <= %s"
                    "   AND last_seen_at < now() - (%s || ' days')::interval",
                    (list(_DECAY_ELIGIBLE_CATEGORIES), HARDEN_THRESHOLD,
                     _DECAY_MAX_HITS, str(_DECAY_AGE_DAYS)),
                )
                out["decayed"] += cur.rowcount or 0
            conn.commit()
            # (iii) Qdrant RECONCILE — re-upsert any non-decayed row missing from the per-seat
            # cortex collection (write-time Qdrant/embedder outage back-fill). Fail-safe.
            try:
                collection, _ctflt = _cortex_partition(user_id)
                if collection:
                    _ensure_cortex_collection(collection)
                    existing = _cortex_point_ids(collection, _ctflt)
                    if existing is not None:  # None = scroll failed → skip (don't blind-upsert)
                        with conn.cursor() as cur:
                            cur.execute(
                                "SELECT id, category, note, tags,"
                                "       lifecycle_state, observation_count"
                                " FROM cortex_episodes"
                                # CRITIC B7: tombstoned notes are EXCLUDED from back-fill —
                                # re-upserting a superseded body re-pollutes the ANN field
                                # every sweep (μ/σ admission margin computes field-wide).
                                " WHERE lifecycle_state <> 'decayed' AND superseded_by IS NULL"
                                " ORDER BY last_seen_at DESC LIMIT %s",
                                (_EMBED_BACKFILL_CAP,),
                            )
                            drift = [row for row in cur.fetchall() if int(row[0]) not in existing]
                        for rid, category, ntext, tags, lifecycle, obs_count in drift:
                            vec = _embed_cortex_text(ntext, task="search_document")
                            if not vec:
                                continue  # embedder down → retry next sweep
                            hardened = (lifecycle == LIFECYCLE_HARDENED) or \
                                (int(obs_count or 0) >= HARDEN_THRESHOLD)
                            if _upsert_cortex_point(
                                collection, int(rid), vec,
                                _cortex_point_payload(user_id, int(rid), category,
                                                      list(tags or []), ntext, hardened,
                                                      int(obs_count or 1)),
                                user_id=user_id,
                            ):
                                out["embedded"] += 1
                        # (iv) drop points for rows that are now decayed (keep field clean).
                        with conn.cursor() as cur:
                            cur.execute(
                                "SELECT id FROM cortex_episodes WHERE lifecycle_state = 'decayed'"
                            )
                            decayed_ids = [int(x[0]) for x in cur.fetchall()]
                        stale = [i for i in decayed_ids if i in existing]
                        if stale:
                            _delete_cortex_points(collection, stale, user_id=user_id, tenant_filter=_ctflt)
                        # (v) CRITIC B7: drop points for TOMBSTONED rows too — the victim's
                        # stale vector otherwise sits in the ANN field forever (near
                        # correction queries) and drags μ/σ against the CORRECTED note's
                        # admission. Same contract as decayed: row stays in PG for audit,
                        # its vector leaves the field.
                        with conn.cursor() as cur:
                            cur.execute(
                                "SELECT id FROM cortex_episodes WHERE superseded_by IS NOT NULL"
                            )
                            tomb_ids = [int(x[0]) for x in cur.fetchall()]
                        tombed = [i for i in tomb_ids if i in existing]
                        if tombed:
                            _delete_cortex_points(collection, tombed, user_id=user_id, tenant_filter=_ctflt)
                            out["tombstoned_points_dropped"] = len(tombed)
            except Exception as be:  # pragma: no cover - defensive
                log.debug("cortex.sweep.reconcile_failed", error=str(be)[:120])
        if out["hardened"] or out["decayed"] or out["embedded"]:
            log.info("cortex.sweep", schema=schema, **out)
        return out
    except Exception as e:
        log.warning("cortex.sweep.failed", user=str(user_id)[:8], error=str(e)[:160])
        return out


# ── USAGE PATTERNS (the seeded HOW-YOU-OPERATE slice) ─────────────────────────────────────
# A DISTINCT, curated, SEEDED slice of the cortex (category `usage_pattern`): generic FaultLine
# operating best-practices every connected model needs from turn one. Same firewalled substrate
# (`flagent_<uuid>`) + same hybrid recall machinery as the private /iremember lessons, but its
# OWN category + delivery path (the labeled HOW YOU OPERATE recall block). NEVER a user fact
# (different schema); NEVER surfaced by /irecall (excluded by category in recall_notes).

def seed_usage_patterns(user_id: str) -> int:
    """Seed the HOW-YOU-OPERATE usage-pattern set into a cortex schema (per-tenant, at
    cortex-schema creation — mirroring the ontology/linguistic-cue seed rail: seed INTO the
    tenant, never read public at runtime). Idempotent — ON CONFLICT DO NOTHING on the
    (category, md5(lower(note))) dedup key, so re-provisioning / a template refresh is safe.

    Best-effort embeds each new row into the per-seat cortex collection (semantic lane), exactly
    like note(); an embedder/Qdrant outage never fails the seed (the reconcile sweep back-fills).

    Returns the count of rows inserted (0 if already seeded / on any failure). NEVER raises."""
    schema = cortex_schema_for(user_id)
    if not schema:
        log.debug("cortex.seed_usage_patterns.unresolved_schema", user=str(user_id)[:8])
        return 0
    inserted = 0
    to_embed: list[tuple[int, str, list[str]]] = []
    _purged_ids: list[int] = []
    try:
        with _cortex_conn(schema) as conn:
            with conn.cursor() as cur:
                # SUPERSEDED-SEED PURGE (v4). The seed is versioned INTO each tenant
                # (never read from public at runtime), and the dedup key is the note TEXT —
                # so a re-seed after a wording change INSERTS the new rows while the OLD
                # rows linger and keep surfacing. That silently defeats seed-version fixes:
                # v3 taught the verbatim paste mandate and "follow it" authority framing;
                # every existing seat would keep teaching both. Delete the superseded rows
                # (seed-sourced usage_pattern rows only — an agent lesson can never match:
                # different source) in the SAME transaction as the re-seed.
                _superseded = superseded_seed_notes()
                if _superseded:
                    cur.execute(
                        "DELETE FROM cortex_episodes"
                        " WHERE category = %s AND source = %s AND note = ANY(%s)"
                        " RETURNING id",
                        (USAGE_PATTERN_CATEGORY, SOURCE_SEED, list(_superseded)),
                    )
                    # Purged row ids — their Qdrant points are deleted post-commit below.
                    # Without RETURNING, a deleted row's vector would orphan forever: the
                    # reconcile sweep only removes points for rows still present in PG with
                    # lifecycle_state='decayed', so a DELETEd row's point is invisible to it
                    # and pollutes the ANN field the relative rerank computes μ/σ over.
                    _purged_ids = [int(r[0]) for r in (cur.fetchall() or [])]
                for row in iter_seed_rows():
                    note_text = (row.get("note") or "").strip()
                    if not note_text:
                        continue
                    tag_list = _normalize_tags(row.get("tags"))
                    ctx = {
                        "seed_version": SEED_VERSION,
                        "tools": list(row.get("tools") or []),
                        "standing": bool(row.get("standing")),
                    }
                    cur.execute(
                        "INSERT INTO cortex_episodes"
                        "  (user_id, category, note, tags, context, source,"
                        "   lifecycle_state, observation_count, hit_count)"
                        " VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, 1, 0)"
                        " ON CONFLICT (category, md5(lower(note))) DO NOTHING"
                        " RETURNING id",
                        (user_id, USAGE_PATTERN_CATEGORY, note_text, tag_list,
                         json.dumps(ctx), SOURCE_SEED, LIFECYCLE_OBSERVED),
                    )
                    r = cur.fetchone()
                    if r:
                        inserted += 1
                        to_embed.append((int(r[0]), note_text, tag_list))
            conn.commit()
    except Exception as e:
        log.warning("cortex.seed_usage_patterns.failed",
                    user=str(user_id)[:8], error=str(e)[:160])
        return 0
    # ── SEMANTIC sync (best-effort, AFTER the durable PG write) — never the user collection.
    if to_embed or _purged_ids:
        try:
            collection, _ = _cortex_partition(user_id)
            if collection:
                if _purged_ids:
                    # Drop the purged rows' vectors (best-effort; PG is already authoritative).
                    _delete_cortex_points(collection, _purged_ids, user_id=user_id)
                _ensure_cortex_collection(collection)
                for rid, note_text, tag_list in to_embed:
                    vec = _embed_cortex_text(note_text, task="search_document")
                    if vec:
                        _upsert_cortex_point(
                            collection, int(rid), vec,
                            _cortex_point_payload(user_id, int(rid), USAGE_PATTERN_CATEGORY,
                                                  tag_list, note_text, False, 1),
                            user_id=user_id,
                        )
        except Exception as e:  # pragma: no cover - defensive; PG rows already durable
            log.debug("cortex.seed_usage_patterns.qdrant_soft_fail", error=str(e)[:120])
    if inserted:
        log.info("cortex.seed_usage_patterns", schema=schema,
                 inserted=inserted, version=SEED_VERSION)
    return inserted


# Schemas whose cortex_episodes ranking columns this process has already ensured (see the
# lazy ensure inside note(); same idiom as _ENGAGEMENT_READY, for the same reason: an
# ALREADY-PROVISIONED seat must pick the columns up without waiting for a re-provision).
# Covers branch (migration 261) and env + superseded_by (migration 262) in one probe —
# the missing-column failure mode is identical for all three.
_BRANCH_READY: set[str] = set()


def _ensure_branch_column(schema: str) -> None:
    if schema in _BRANCH_READY:
        return
    try:
        with _cortex_conn(schema) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = %s AND table_name = 'cortex_episodes'"
                    "   AND column_name = ANY(%s)",
                    (schema, ["branch", "env", "superseded_by",
                              "model", "note_type", "project"]),
                )
                present = {r[0] for r in (cur.fetchall() or [])}
                if "branch" not in present:
                    cur.execute("ALTER TABLE cortex_episodes ADD COLUMN IF NOT EXISTS branch TEXT")
                if "env" not in present:
                    cur.execute("ALTER TABLE cortex_episodes ADD COLUMN IF NOT EXISTS env TEXT")
                if "superseded_by" not in present:
                    cur.execute("ALTER TABLE cortex_episodes"
                                " ADD COLUMN IF NOT EXISTS superseded_by BIGINT")
                # SCHEMA UPLIFT (owner directive): model / note_type / project provenance.
                # Qdrant payloads are schemaless and Postgres rows are schema-tolerant (all
                # nullable), so this lazy idempotent ALTER is the whole migration — existing
                # seats pick the fields up on their next note write, legacy rows read fine
                # with the fields NULL.
                if "model" not in present:
                    cur.execute("ALTER TABLE cortex_episodes ADD COLUMN IF NOT EXISTS model TEXT")
                if "note_type" not in present:
                    cur.execute("ALTER TABLE cortex_episodes ADD COLUMN IF NOT EXISTS note_type TEXT")
                if "project" not in present:
                    cur.execute("ALTER TABLE cortex_episodes ADD COLUMN IF NOT EXISTS project TEXT")
            conn.commit()
        _BRANCH_READY.add(schema)
    except Exception as e:  # noqa: BLE001 - best-effort; the INSERT will surface a loud error
        _BRANCH_READY.discard(schema)
        log.debug("cortex.branch_column.ensure_failed", schema=schema, error=str(e)[:120])


# Schemas back-seeded this process (see the lazy back-seed inside recall_usage_patterns).
_BACKSEEDED_SCHEMAS: set[str] = set()


def recall_usage_patterns(user_id: str, query: Optional[str] = None,
                          limit: int = 4) -> list[dict]:
    """Surface the FEW seeded operating rules (HOW YOU OPERATE) relevant to THIS turn.

    Reads ONLY the ``usage_pattern`` slice of the firewalled cortex (``flagent_<uuid>``): it can
    NEVER return a user fact (different schema) and NEVER a private /iremember lesson (different
    category). Same hybrid keyword+tag + RELATIVE-semantic machinery + REAL-MATCH BAR as
    recall_notes, so it returns SIGNAL not wallpaper — a discriminating query with no match yields
    [] (the passive tool-description surface then carries the standing rules at zero per-turn
    cost). ``limit`` is small by default (bloat guard). FAIL-SAFE: unresolved/absent schema or any
    error → []. NEVER raises. Given fixed embeddings the ranking is deterministic."""
    schema = cortex_schema_for(user_id)
    if not schema:
        return []

    # LAZY BACK-SEED. `seed_usage_patterns` ran ONLY at provisioning (schema_manager.py), so an
    # EXISTING seat was frozen on whatever rules shipped the day it was created — a new operating
    # rule could never reach an already-provisioned agent. That is not academic: the cortex WRITE
    # lane (iremember) had NO rule at all, a live model called irecall 5x and iremember 0x, and
    # adding the rule would have changed nothing for the seat that needed it.
    #
    # seed_usage_patterns is idempotent (ON CONFLICT (category, md5(lower(note))) DO NOTHING), so
    # re-running it INSERTS the new rows and skips the existing ones. Gated to once per schema per
    # process, so the cost is one no-op insert batch at first use, not per turn. Fail-safe: a seed
    # failure must never break the brief.
    if schema not in _BACKSEEDED_SCHEMAS:
        _BACKSEEDED_SCHEMAS.add(schema)      # add FIRST: a failure must not retry every turn
        try:
            _n = seed_usage_patterns(user_id)
            if _n:
                log.info("cortex.usage_patterns.backseeded", schema=schema, added=_n,
                         version=SEED_VERSION)
        except Exception as e:  # noqa: BLE001 — a back-seed must never break recall
            log.debug("cortex.usage_patterns.backseed_failed", error=str(e)[:120])

    try:
        limit = max(1, min(int(limit or 4), 12))
    except Exception:
        limit = 4
    q_tokens = [t for t in _tokenize(query) if t not in _STOPWORDS]
    has_query = bool(query and str(query).strip())
    q_emb = _embed_cortex_text(query, task="search_query") if has_query else None
    sem_by_id: dict[int, float] = {}
    if q_emb is not None:
        collection, _ctflt = _cortex_partition(user_id)
        if collection:
            sem_by_id = _relative_semantic_components(_search_cortex_ann(collection, q_emb, _ctflt))
    try:
        with _cortex_conn(schema) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, category, note, tags, context, source,"
                    "       lifecycle_state, observation_count, last_seen_at"
                    " FROM cortex_episodes"
                    " WHERE lifecycle_state <> 'decayed'"
                    # SEAM-5: a tombstoned (superseded) rule is OUTDATED truth — the brief
                    # must not teach it even when hardened. The replacement note, once it
                    # hardens, rides instead.
                    "   AND superseded_by IS NULL"
                    # THE SEEDED MANUAL **∪ THE SEAT'S OWN HARDENED RULES**.
                    #
                    # This channel used to serve ONLY the seeded usage patterns, so a lesson the
                    # agent LEARNED — even one HARDENED by repetition (observation_count >= 3,
                    # i.e. proven, not a guess) — NEVER reached it unless it remembered to call
                    # /irecall. And the entire failure mode is that it does NOT remember to ask:
                    # the owner had to poke me twice in one day for ignoring a rule I had myself
                    # written down and recorded. A rule that is RECORDED but never FIRES is worth
                    # nothing.
                    #
                    # A hardened note IS an operating rule — that is precisely what hardening
                    # MEANS here. So it rides the operating brief alongside the seeded manual.
                    # Still relevance-gated by the same REAL-MATCH BAR (an unrelated rule cannot
                    # surface), still cortex-only (flagent_ schema — NEVER user memory), and an
                    # `observed` note stays private to /irecall until it earns its way in.
                    "   AND (category = %s OR lifecycle_state = 'hardened')"
                    " ORDER BY last_seen_at DESC"
                    " LIMIT %s",
                    (USAGE_PATTERN_CATEGORY, _RECALL_CANDIDATE_CAP,),
                )
                rows = cur.fetchall()
    except Exception as e:
        log.debug("cortex.recall_usage_patterns.failed", error=str(e)[:160])
        return []

    # ENGAGEMENT: this call rides every `recall_memory` turn, so it is the honest per-seat count
    # of "the agent reached for memory". Deliberately on its OWN short connection rather than
    # folded into the read above: a failed telemetry statement would otherwise ABORT the read
    # transaction, and this codebase has already paid for that exact class of bug once (an
    # UndefinedColumn in a probe poisoned the caller's transaction and surfaced as "no facts
    # found" on a memory that had just been read). Cost is one upsert on a turn that already
    # costs hundreds of milliseconds; correctness here is worth more than the round trip.
    record_engagement(user_id, ENGAGEMENT_RECALL_MEMORY)

    top = _score_candidate_rows(rows, q_tokens, q_emb is not None, sem_by_id, limit)

    # bump hit_count on the surfaced patterns (which rules the model actually needed) — best-effort.
    if has_query and top:
        try:
            with _cortex_conn(schema) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE cortex_episodes SET hit_count = hit_count + 1"  # WRITE-CLOCK: a read must never reset last_seen_at
                        " WHERE category = %s AND note = ANY(%s)",
                        (USAGE_PATTERN_CATEGORY, [t["note"] for t in top]),
                    )
                conn.commit()
        except Exception:
            pass
    return top


# ── EXPERIENCE PUSH LANE (model-independent) ──────────────────────────────────────────────
#
# The PULL lane (/irecall) requires the agent to first RECOGNISE that it is in a familiar
# situation — which is exactly what a model with no memory is worst at, and is the reason the
# cortex kept being written to and never read. This lane inverts it: the MEMORY decides what to
# PUSH, riding the response of a call the model already makes WHILE ACTING (the write path).
#
# SUBJECT-AGNOSTIC BY CONSTRUCTION. The situation signal is simply "the text describing what is
# being done right now" — a developer deploying a build, a physician opening a workup, a vet
# running pre-op bloodwork are the SAME event shape. Nothing here inspects the domain: it is
# token overlap against the seat's own recorded lessons plus the existing relative-semantic
# rerank. No domain vocabulary, no persona, no client assumption.
#
# WHY THIS NEEDS ITS OWN GATE, STRICTER THAN /irecall's. Measured: /irecall is a top-K lane, not
# a relevance gate — "bake sourdough bread" returned 9 bullets about docker and benchmarks. A
# PULL lane may be generous (the agent asked). A PUSH lane that fires on everything gets ignored
# exactly as a firmer instruction does, so SILENCE IS THE DEFAULT OUTCOME and the bar is:
#
#   (1) KEYWORD CORROBORATION IS MANDATORY — >= _EXP_MIN_KW_TOKENS DISTINCT query tokens must
#       match the note. Semantic-only admission is precisely what produces the sourdough result.
#   (2) A SECOND, INDEPENDENT SIGNAL IS MANDATORY — either
#         (a) the RELATIVE-semantic component (the note's cosine STANDS OUT of the ANN field:
#             margin + z-score, `_relative_semantic_components`), or
#         (b) a TAG-anchored keyword case: >= 1 hit on the note's curated TAGS *and*
#             >= _EXP_STRICT_KW_TOKENS distinct token hits overall.
#       (b) exists because the embedder is best-effort: fastembed absent / the embedding backend
#       unreachable makes the semantic term 0 for EVERY row, and a semantic-mandatory gate would
#       make the lane permanently dead-silent in exactly the deployments that have no vector
#       lane at all. A TAG hit is a genuinely independent signal — tags are hand-curated at
#       write time, they are not the note prose the free-text overlap already read.
#
# SCORE ALL, THEN PICK — never pick then score. The client-side hook this replaces did the
# opposite (it preferred any `[rule]` bullet, THEN judged relevance) and threw away the exactly-
# correct note, retrieved and ranked first, for an unrelated one with zero overlap.
#
# READ-ONLY: no hit_count / last_seen_at bump (an automatic per-turn lane that bumps re-ranks
# the cortex it reads — see `recall_notes(bump=...)`).
#
# FIREWALL: reads ONLY the `flagent_<uuid>` lesson slice through `_fetch_lesson_candidates`,
# bound WITHOUT `public`. It cannot reach a user fact — different schema, different connection.
# These are the AGENT's working notes and are never rendered as user memory.

_EXP_MAX_LINES = int(os.getenv("CORTEX_EXPERIENCE_MAX_LINES", "2") or 2)
_EXP_MAX_CHARS = int(os.getenv("CORTEX_EXPERIENCE_MAX_CHARS", "320") or 320)
_EXP_MIN_KW_TOKENS = int(os.getenv("CORTEX_EXPERIENCE_MIN_KW", "2") or 2)
_EXP_STRICT_KW_TOKENS = int(os.getenv("CORTEX_EXPERIENCE_STRICT_KW", "3") or 3)
# Per-seat suppression window. The design calls for "never the same line twice in a session",
# but a memory server has no session: MCP tool calls carry a seat, not a conversation. A TTL
# window is the honest approximation — stated as such, not dressed up as session tracking.
_EXP_DEDUPE_TTL_S = float(os.getenv("CORTEX_EXPERIENCE_DEDUPE_TTL", "21600") or 21600)
_EXP_DEDUPE_MAX = 4096
_EXP_SEEN: dict = {}


def experience_surfacing_enabled() -> bool:
    """Gate for the experience push lane. PURE env read — zero import, zero cost when off.

    DEFAULT ON (2026-08-16, reach-metric gauntlet): the lane has earned the room — it is
    relevance-gated (silent when nothing matches), hard-capped (<=2 lines / 320 chars),
    out-of-band (its own response key), and fail-open, all pinned by tests. More to the
    point: PROSPECTIVE REACH is the cortex's product metric now, and this lane is the one
    mechanism that delivers a lesson BEFORE the agent needs telling — shipping it dark was
    shipping from behind (owner: "why ship it starting from behind? note it, fix it, then
    criticize it"). KILL SWITCH: EXPERIENCE_SURFACING=0 restores byte-for-byte legacy
    behaviour (explicit env always wins)."""
    v = os.getenv("EXPERIENCE_SURFACING")
    if v is None:
        return True
    return v.strip().lower() in ("1", "true", "yes", "on")


def _exp_dedupe_seen(user_id: str, fingerprint: str, now: float) -> bool:
    """True if (seat, line) was already pushed inside the TTL window. Prunes as it goes.
    Process-local and best-effort — a restart or a second worker re-allows a line once; that is
    a re-surface, not a leak, and is preferable to a shared-state dependency on this path."""
    try:
        if len(_EXP_SEEN) > _EXP_DEDUPE_MAX:
            for k, ts in list(_EXP_SEEN.items()):
                if now - ts > _EXP_DEDUPE_TTL_S:
                    _EXP_SEEN.pop(k, None)
            if len(_EXP_SEEN) > _EXP_DEDUPE_MAX:
                _EXP_SEEN.clear()
        key = (str(user_id), fingerprint)
        ts = _EXP_SEEN.get(key)
        if ts is not None and (now - ts) <= _EXP_DEDUPE_TTL_S:
            return True
        _EXP_SEEN[key] = now
        return False
    except Exception:
        return False


def _exp_fingerprint(note: str) -> str:
    import hashlib
    return hashlib.blake2b(str(note).encode("utf-8", "replace"), digest_size=8).hexdigest()


def _exp_clip(lines: list[str], max_chars: int) -> list[str]:
    """Hard budget. Every line here is rented, not owned: fill up to ``max_chars`` total,
    clipping the LAST admitted line at a word boundary rather than dropping it whole."""
    out: list[str] = []
    used = 0
    for ln in lines:
        ln = " ".join(str(ln).split())
        if not ln:
            continue
        room = max_chars - used
        if room <= 0:
            break
        if len(ln) > room:
            if room < 60:            # too little room to say anything useful — stop cleanly
                break
            cut = ln[:room]
            sp = cut.rfind(" ")
            ln = (cut[:sp] if sp > 40 else cut).rstrip(" ,.;:") + "…"
        out.append(ln)
        used += len(ln)
    return out


def experience_lines(user_id: str, text: Optional[str],
                     limit: int = _EXP_MAX_LINES, dedupe: bool = True) -> list[str]:
    """PUSH the seat's own prior operational lessons relevant to what it is doing RIGHT NOW.

    ``text`` is the activity text (the same text the write path already received). Returns at
    most ``limit`` note strings, total <= ``_EXP_MAX_CHARS``; **[] means silence and the caller
    must render nothing** — never a "no relevant notes" line.

    Gate: score ALL candidates, then pick; mandatory keyword corroboration PLUS a second
    independent signal (relative-semantic, or a tag-anchored strong keyword case). READ-ONLY —
    no counter bump. FAIL-SAFE: unresolvable seat / absent cortex / any error → []. NEVER raises.
    Deterministic given fixed embeddings."""
    try:
        schema = cortex_schema_for(user_id)
        if not schema or not text or not str(text).strip():
            return []
        try:
            limit = max(1, min(int(limit or _EXP_MAX_LINES), 4))
        except Exception:
            limit = _EXP_MAX_LINES
        q_tokens = [t for t in _tokenize(text) if t not in _STOPWORDS]
        # Cheap pre-gate: a turn with fewer salient tokens than the floor can never clear the
        # keyword requirement, so skip the embed + the DB read entirely (this is the common
        # case for short chit-chat writes — the lane must cost nothing when it will say nothing).
        if len(set(q_tokens)) < _EXP_MIN_KW_TOKENS:
            return []

        q_emb = _embed_cortex_text(text, task="search_query")
        sem_by_id: dict[int, float] = {}
        if q_emb is not None:
            collection, _ctflt = _cortex_partition(user_id)
            if collection:
                sem_by_id = _relative_semantic_components(
                    _search_cortex_ann(collection, q_emb, _ctflt))

        rows = _fetch_lesson_candidates(schema)
        if not rows:
            return []

        # ── SCORE ALL CANDIDATES (never pick-then-score) ──
        scored: list[tuple[tuple, str]] = []
        for r in rows:
            (rid, category, ntext, tags, _context, _source,
             lifecycle, obs_count, last_seen) = r[:9]
            ev_sha = r[9] if len(r) > 9 else None
            if not ntext or not str(ntext).strip():
                continue
            tag_hits, note_hits = _keyword_hits(q_tokens, ntext, tags or [])
            distinct = tag_hits + note_hits
            if distinct < _EXP_MIN_KW_TOKENS:
                continue                                   # signal 1 — mandatory
            sem = float(sem_by_id.get(int(rid), 0.0)) if rid is not None else 0.0
            second_signal = (sem > 0.0) or (tag_hits >= 1 and distinct >= _EXP_STRICT_KW_TOKENS)
            if not second_signal:
                continue                                   # signal 2 — mandatory
            hardened = (lifecycle == LIFECYCLE_HARDENED) or (int(obs_count or 0) >= HARDEN_THRESHOLD)
            _ls = last_seen.timestamp() if last_seen else 0.0
            # Rank on RELEVANCE first (the two signals), then on how much the note has EARNED:
            # tag-anchored > hardened > evidenced > repeated > recent. `[rule]`-ness is a
            # TIE-BREAK here, never an admission ticket.
            key = (round(distinct + sem, 6), tag_hits, 1 if hardened else 0,
                   1 if ev_sha else 0, int(obs_count or 0), _ls)
            scored.append((key, str(ntext).strip()))
        if not scored:
            return []
        scored.sort(key=lambda x: x[0], reverse=True)

        now = _time.time()
        picked: list[str] = []
        for _key, ntext in scored:
            if len(picked) >= limit:
                break
            if dedupe and _exp_dedupe_seen(user_id, _exp_fingerprint(ntext), now):
                continue
            picked.append(ntext)
        return _exp_clip(picked, _EXP_MAX_CHARS)
    except Exception as e:  # noqa: BLE001 — a push lane must never break its host
        log.debug("cortex.experience_lines.failed", error=str(e)[:160])
        return []
