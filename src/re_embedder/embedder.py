"""
FaultLine Re-Embedder Service

Polls the facts table for unsynced rows, embeds them, and upserts to per-user Qdrant collections.
This is the only service that writes to Qdrant.
"""
import atexit
import hashlib
import json
import logging
import math
import os
import re
import time
import uuid
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from src.api.backend_auth import backend_headers as _backend_auth_headers  # #121: every backend call carries the service secret
from src.api import errors as _errors  # THE ONE ERROR SEAM — persisted error columns are rendered bodies

import httpx
import psycopg2
import redis
from src.api.db_read import release_read_transaction  # READ-BARRIER — see the "idle in transaction" incident block below
from src.api.llm_client import get_llm_headers, get_embedding_headers, GATE_MIN, GATE_MAX, GATE_DEFAULT, clamp_gate
from src.api.llm_calls import (
    call_llm_with_retry_sync,
    close_llm_http_client,
    generate_rel_type_phrasing,
    LLMTimeouts,
    LLMModels,
)
from src.api.llm_lane import LLMUnavailable
from src.api import llm_lane, llm_rate
from src.api import llm_lane as _llm_lane
from src.api import ingest_transport as _ingest_transport  # replay marker contract — the reextract lane is a replaying writer (reextract-replay-resurrection)

# Sent on every LLM-bearing call this process makes to the BACKEND API. The lane is a
# property of the CALLER (dprompt charter: never an operation-name list): this process is
# deferrable upkeep, so the extraction it asks the backend to perform must run in the
# backend's BACKGROUND lane — defer when there is no capacity, never fail open. The header
# is what carries that declaration across the HTTP boundary (see the API's
# _llm_lane_from_header middleware); without it the call executes INTERACTIVE in the API
# process and fires UNCAPPED after LLM_RATE_MAX_WAIT_S (the wedged_failopen storm shape).
_BACKEND_LANE_HEADERS = {_llm_lane.LANE_HEADER: _llm_lane.LANE_BACKGROUND}
from src.entity_registry.registry import preference_rank, EntityRegistry
from src.entity_registry import weld_guard


class _MergeRefused(Exception):
    """The arrival weld guard declined one of the surfaces an entity merge would move.

    Raised inside the merge's own transaction so the EXISTING rollback path undoes every step
    already applied — a merge is one identity claim and must apply whole or not at all. Caught
    explicitly (never as a generic merge failure) so a deliberate refusal is not reported as an
    error; see the merge block in ``resolve_name_conflicts``.
    """
from src.api import node_role as _node_role  # FIRST-CLASS value/place property (THE HARD LINE, migration 192); flag VALUE_PLACE_FIRST_CLASS
from src.api import hardline_guard as _ladder_hardline  # THE HARD-LINE ingest guard for subclass_of rungs (ladder-on-value); FAIL-CLOSED, flag HARDLINE_LADDER_GUARD default OFF
from src.ingest import document_structure as _docstruct  # shared transcript line shapes
from src.re_embedder import sweep_ledger as _sweep  # per-seat work ledger (migration 207)

logging.basicConfig(level=getattr(logging, os.getenv("FAULTLINE_LOG_LEVEL", "INFO").upper(), logging.INFO), format="%(levelname)s:%(name)s:%(message)s")
log = logging.getLogger(__name__)


def _rollback_and_reapply_search_path(db_conn, schema_name: str) -> None:
    """Per-tenant transaction-abort recovery for the poll loop's SHARED-connection
    subsystem loops.

    When a per-tenant subsystem hits a missing/incomplete relation (e.g. a throwaway
    schema with no `staged_facts`/`intent_confidence_feedback`), the Postgres
    transaction ABORTS. On a connection shared across subsystems/tenants, every
    subsequent statement then fails with "current transaction is aborted" — one bad
    tenant poisons the whole cycle. Calling this in the subsystem's `except` rolls the
    connection back to a clean state so the NEXT subsystem / tenant proceeds normally.

    psycopg2 resets `search_path` on rollback, so we re-apply the tenant search_path
    (NO public — per-tenant isolation) for the next unit of work on this connection.
    Best-effort and never raises: failure here must not crash the loop (fail-safe).
    """
    try:
        db_conn.rollback()
    except Exception as rollback_err:
        log.warning(f"re_embedder.rollback_failed schema={schema_name}: {rollback_err}")
        return
    try:
        with db_conn.cursor() as _spc:
            _spc.execute(f"SET search_path TO {schema_name}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema from UUID-derived source with validation
        # READ BARRIER — COMMIT THE RE-BIND. `SET` is a statement like any other: psycopg2
        # opened a NEW transaction to run it and holds it open until commit/rollback. Every
        # caller of this helper is an `except` arm that goes straight back to work — often a
        # network or LLM call, or the next tenant's whole pass — so an uncommitted re-bind put
        # the connection back into `idle in transaction` for the whole of it. Under
        # `idle_in_transaction_session_timeout` (1min on pre-prod) that is the connection being
        # KILLED, and every later statement on it fails "connection already closed": the
        # recovery helper was itself re-arming the failure it exists to recover from.
        # Committing is also what makes the bind survive a later ROLLBACK — a COMMITTED
        # `SET search_path` is not undone by rollback, an uncommitted one is.
        db_conn.commit()
    except Exception as sp_err:
        log.warning(f"re_embedder.search_path_reapply_failed schema={schema_name}: {sp_err}")


def tenant_schema_is_live(admin_conn, schema_name: str) -> bool:
    """Deterministic GHOST-TENANT health probe for the poll loop's per-tenant fan-out.

    Returns True iff ``schema_name`` is a REAL, provisioned tenant schema — it EXISTS and
    carries the core per-tenant table ``staged_facts``. Returns False for a GHOST: a schema
    that was DROPPED (or never finished provisioning → 0 tables) yet is still flagged
    ``status='ready'`` in ``public.user_provisioning`` (e.g. leftover benchmark/throwaway
    tenants).

    WHY THIS EXISTS: a ghost schema makes EVERY per-tenant pass in ``main()`` throw
    ``UndefinedTable`` on its first ``SELECT ... FROM staged_facts`` → the psycopg2 txn ABORTS →
    on any pass that shares one connection across tenants, every SUBSEQUENT statement then fails
    with "current transaction is aborted, commands ignored" — the "one bad tenant aborts the
    loop" cascade that STALLS Class-B promotion + Class-C sync for HEALTHY tenants. Dropping
    ghosts from ``ready_schemas`` ONCE per cycle (a single structural chokepoint feeding all the
    per-tenant loops) makes the loop RESILIENT to such rows existing — the durable code fix.
    (Deleting the orphan ``user_provisioning`` rows is a separate DATA/ops cleanup; the code
    must not depend on it.)

    Deterministic — a ``pg_catalog`` lookup via ``to_regclass`` (schema-qualified: NULL when the
    schema OR the table is absent; NO fuzzy/ILIKE, NO cosine). Runs on the ADMIN connection with
    a fully-qualified name (search_path-independent), so it never touches/dirties a per-tenant
    connection.

    FAIL-SAFE toward INCLUSION (WE DON'T FORGET): any probe error → True (treat as live and let
    the per-tenant isolation below handle it) so a transient catalog hiccup can NEVER mass-skip
    healthy tenants and stall promotion. Never raises.
    """
    if not schema_name:
        return False
    try:
        with admin_conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"{schema_name}.staged_facts",))
            row = cur.fetchone()
        return bool(row and row[0] is not None)
    except Exception as probe_err:
        # Fail-SAFE toward inclusion: never let a probe error mass-skip live tenants. Clean the
        # admin txn (a raised probe may have aborted it) so the next tenant's probe runs clean.
        try:
            admin_conn.rollback()
        except Exception:
            pass
        log.warning(
            f"re_embedder.tenant_health_probe_failed schema={schema_name} "
            f"(fail-safe: treating tenant as live): {probe_err}"
        )
        return True


# ── Per-tenant ATTRIBUTION for the background LLM calls ─────────────────────────────────────
# The re_embedder runs many per-tenant `for … in ready_schemas` loops. Every centralized LLM call
# takes a `user_id=` it is ATTRIBUTED to (circuit-breaker / rate bucketing, the OpenWebUI
# `chat_id` stamped by `build_llm_payload`). It used to be a hardcoded literal ("re_embedder") on
# every background call site. The schema the loop already holds IS the tenant —
# `faultline_<uuid-with-underscores>`, the same derivation `resolve_name_conflicts` does off
# `current_schema()` — so the identity costs nothing to carry. It is set by
# `_reembedder_bind_tenant` and cleared by `_reembedder_clear_tenant`. The open core runs ONE
# env-configured LLM, so the bind decides only WHO a call is made as, never WHERE it goes.
#
# A MODULE GLOBAL, deliberately, NOT a ContextVar: these growth loops are single-threaded, and the
# one thread-pool fan-out in this file (document chunks) calls the BACKEND over HTTP rather than the
# in-process LLM stack. A worker thread would read a ContextVar's DEFAULT — the wrong answer —
# where it reads the correct current tenant from a global.
_current_tenant_user_id: Optional[str] = None

# Attribution fallback when the bound schema names no tenant (a non-`faultline_<uuid>` schema, or
# nothing bound yet). Byte-for-byte the pre-fix literal.
_NO_TENANT_USER_ID = "re_embedder"


def _tenant_user_id_for_schema(schema_name: str) -> Optional[str]:
    """``faultline_<uuid-with-underscores>`` → that tenant's user uuid, or None.

    Pure string derivation — no DB read — plus a UUID SHAPE CHECK, so it can never hand a junk
    identity downstream. Anything that is not a tenant schema (`public`, an unset search_path)
    → None → the caller falls back to `_NO_TENANT_USER_ID`. Never raises.
    """
    try:
        s = (schema_name or "").strip().lower()
        if not s.startswith("faultline_"):
            return None
        uid = s[len("faultline_"):].replace("_", "-")
        return str(uuid.UUID(uid))
    except Exception:  # noqa: BLE001 — a malformed schema name is "no tenant", never a crash
        return None


def _reembedder_bind_tenant(schema_name: str) -> None:
    global _current_tenant_user_id
    _current_tenant_user_id = _tenant_user_id_for_schema(schema_name)


def _reembedder_clear_tenant() -> None:
    global _current_tenant_user_id
    _current_tenant_user_id = None


def _reembedder_llm_user_id() -> str:
    """The identity the CURRENT background LLM call is made as — the bound tenant, or the
    no-tenant literal. Read at the call site (not captured at import) so it always describes the
    tenant the loop is actually working."""
    return _current_tenant_user_id or _NO_TENANT_USER_ID


def _reembedder_claim(snap, user_id, subsystem: str, schema_name: str = "") -> bool:
    """The ONE gate a sweep subsystem passes: the tenant's input CHANGED (sweep ledger).
    Callers must NOT `record_run` when this returns False."""
    if not _sweep.claim(snap, user_id, subsystem):
        _sweep.log_skip(snap, user_id, subsystem, schema_name)
        return False
    return True


# Embedding model name — PURE CONFIG, read from env (no code literal). Default lives in
# .env.example. Empty when unset; the LOCAL fastembed CPU path needs no model name, and the
# external-API fallback fail-safes (hash vector / None) rather than crashing the loop.
_EMBEDDING_MODEL = (os.getenv("EMBEDDING_MODEL") or "").strip()


def _flag(name: str, default: str = "true") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# ── RUNG-6 bounded-growth flags (the internal design record) ─────────────────────
# RUNG6_CONVERGENCE: deterministic convergence-by-identity in the ontology growth sweep — two
#   hierarchy branches that reach a node with the SAME canonical name connect by identity (no
#   cosine, no LLM). Default ON; free + deterministic. Disable to revert to no convergence.
_RUNG6_CONVERGENCE = _flag("RUNG6_CONVERGENCE", "true")
# ONTOLOGY_COSINE_MAP: the legacy `cosine > 0.85 → rewrite staged_facts.rel_type` collapse. The
#   design RETIRES this as the PRIMARY collapse mechanism (deterministic convergence + curated
#   rel_type_aliases are primary). Default OFF — when OFF the cosine match is computed but NOT
#   applied; it is logged as a gated SUGGESTION only (gated by hierarchy-rule validity +
#   type-consistency). Set to "true" to restore the old auto-rewrite behaviour.
_ONTOLOGY_COSINE_MAP = _flag("ONTOLOGY_COSINE_MAP", "false")
# RUNG6_BRIDGING: LLM-proposed lowest-common-ancestor bridging between two close-but-disjoint
#   branches. DESIGN-TARGET — implemented as a flagged STUB (contract documented in
#   _propose_lca_bridge). Default OFF; turning it on currently logs intent only (no LLM call,
#   no structure mint) until the full proposal→validation→in-chain-growth path is built.
_RUNG6_BRIDGING = _flag("RUNG6_BRIDGING", "false")
# Cosine threshold retained for the (now demoted) suggestion path.
_ONTOLOGY_COSINE_THRESHOLD = float(os.environ.get("ONTOLOGY_COSINE_THRESHOLD", "0.85"))

# Frequency required to APPROVE a novel rel_type into <tenant>.rel_types. Default 1 = usable on
# derivation, per the engine-structure ruling (see LINGUISTIC_CUE_GROWTH_THRESHOLD). Set to 3 to
# restore the legacy freq gate. This is ENGINE STRUCTURE ONLY — it is unrelated to, and must never be
# confused with, the `staged_facts.confirmed_count >= 3` C→B promotion of INFERRED USER MEMORY.
REL_TYPE_APPROVAL_THRESHOLD = max(1, int(os.environ.get("REL_TYPE_APPROVAL_THRESHOLD", "1") or 1))

# Layer-placement sentinel: a novel rel that no taxonomy covered is minted with this
# category so it is a TRACKED candidate (never a silent "general" orphan). MUST match
# main._CATEGORY_PENDING — the in-flow quarantine writes it, this background drain reads it.
_CATEGORY_PENDING_RE = "pending_placement"

# TIER REALIGNMENT (DESIGN-hierarchy-ladder-and-growth.md §"Strong ingest / brain-dead
# query — the hierarchy IS the index"): A/B are HARD in postgres and served SOLELY by the
# deterministic walk; the vector exists FOR Class C (the rough catch-all + the cosine TALLY
# that earns C its way up to B or lets it decay). So the re_embedder need only sync Class C
# (staged_facts) to Qdrant — A/B facts (the `facts` table) need NOT be in the vector because
# the query no longer reads A/B from it (VECTOR_CLASS_C_ONLY in main.py drops any A/B Qdrant
# result). When ON (default), the facts-table (A/B) sync loop is SKIPPED; only staged_facts
# (Class B/C) are embedded. NOTE: staged_facts still carries Class B rows pre-promotion; they
# are query-visible from postgres (the staged UNION) AND dropped from the Qdrant lane at query
# by VECTOR_CLASS_C_ONLY, so syncing them is harmless (the tally only bumps fact_class='C').
# Set false (0/no) → legacy behaviour (sync BOTH facts and staged_facts). Fail-safe: this only
# gates an extra sync; existing A/B points are left in place (reconcile_qdrant keeps them while
# their PG row lives) and are simply never SERVED to the query lane.
_VECTOR_CLASS_C_ONLY = _flag("VECTOR_CLASS_C_ONLY", "true")

# User-memory vector lane retirement (ratified 2026-07-31). When OFF (default), the re-embedder
# stops embedding Class-C user-memory rows to Qdrant. C stays queryable from staged_facts
# (Postgres) via fetch_facts_from_anchor. Default OFF = retirement active (the ratified direction).
_USER_MEMORY_VECTOR_LANE = _flag("USER_MEMORY_VECTOR_LANE", "false")

# Hierarchy rel_types (rung-4 closed set, DESIGN §"The deterministic resolution ladder"). Used by
# convergence + bridging validation. Mechanism, not ontology CONTENT — these are the structural
# classification rels, identical to the closed set the canonical ladder enforces.
_HIERARCHY_RELS = ("instance_of", "is_a", "subclass_of", "part_of", "member_of")


# ── REFLEXIVE HIERARCHY TAUTOLOGY — async growth-write guard ──
# The ASYNC growth writers do not run through `wgm.gate.validate_edge`, so the reflexive-hierarchy
# rejection that landed at the gate (`WGM_REJECT_REFLEXIVE_HIERARCHY`) does not cover them. `X
# subclass_of X` is RDFS-ENTAILED by X being a class at all (RDF 1.1 Semantics entailment rule
# **rdfs10**, https://www.w3.org/TR/rdf11-mt/#patterns-of-rdfs-entailment-informative): it carries
# ZERO information, cycles what must be a DAG, and renders as "Parent is a subclass of Parent".
# SAME FLAG as the gate (decisions live once — one contract, two enforcement points).
# Purely STRUCTURAL: id identity + the row's own `is_hierarchy_rel` flag (already carried on the
# staged row from the ingest-time rel_types metadata). No rel-name literal, no vocabulary, no fuzzy
# match. Fail-safe → False (promote as today) on any error.
def _reflexive_hierarchy_blocked(subject_id, object_id, is_hierarchy_rel) -> bool:
    """True iff this edge is a hierarchy SELF-LOOP and the rejection flag is ON."""
    try:
        if not _flag("WGM_REJECT_REFLEXIVE_HIERARCHY", "false"):
            return False
        if not is_hierarchy_rel:
            return False
        s = str(subject_id or "").strip().lower()
        o = str(object_id or "").strip().lower()
        return bool(s) and s == o
    except Exception:  # noqa: BLE001 — fail-safe: never break the growth loop on the guard
        return False

# THE HARD LINE — the SKOS naming/label rels whose OBJECT is a NAME (a memory: Rex,
# Apollo, "Alex"), NOT a type. An alias registered as the object of one of these edges is a
# proper name and is NEVER a valid L4 classification subject — the subclass_of ladder hangs
# off the TYPE node (dog, computer), never off the name. Same fixed SKOS-identity invariant the
# rest of the codebase pins (main._ALIAS_BACKED_NAME_RELS = pref_name/also_known_as, the
# skos:prefLabel/skos:altLabel pair) — NOT a growable ontology axis, so naming it here is the
# existing SKOS convention, not a domain-specific hardcode. Used by the climb to (a) pick the
# TYPE-bearing alias for classification and (b) refuse to climb a pure named instance into L4.
_NAMING_RELS = ("pref_name", "also_known_as")

# ── AUTHORITY GUARD — engine growth must never re-classify a SEEDED rel's structure ──
# Structural fields of a rel_type that are IMMUTABLE to engine growth (user > SEED > growth).
# head_types is DELIBERATELY excluded — growth may WIDEN it by union (additive). Mirrors
# main._SEED_STRUCTURAL_FIELDS (this is a separate OS process, so the constant is duplicated).
_SEED_STRUCTURAL_FIELDS = (
    "is_hierarchy_rel", "category", "tail_types", "fact_class",
    "storage_target", "inverse_rel_type", "is_symmetric",
)


def _seed_structural_flags(db_conn, rel_type: str) -> dict | None:
    """Return the AUTHORITATIVE structural flags of a SEEDED rel from the ``public``
    template, or ``None`` when the rel is not seeded (genuinely novel — growth owns it).

    AUTHORITY ORDER — user > SEED > engine-growth. A rel present in ``public.rel_types`` has
    an IMMUTABLE structural classification against the re-embedder's ontology-evaluation /
    class-C-promotion / synonym-convergence writes: they may STRENGTHEN confidence and
    WIDEN head_types (additive) but must never re-classify a seeded rel's structure
    (is_hierarchy_rel / category / tail_types / fact_class / storage_target /
    inverse_rel_type / is_symmetric). Extends "user is truth" to the ontology STRUCTURE —
    the engine grows PLACES, never corrupts the grounded structural definition.

    KG grounding: RDFS ``rdf:type`` (Wikidata P31, is_hierarchy_rel) and ``rdfs:subClassOf``
    (P279) are DEFINITIONAL axes fixed by the ontology author, not mutable from instance data
    — https://www.w3.org/TR/rdf-schema/#ch_type , #ch_subclassof.

    ``public`` is read ONLY here as the seed-authority reference. Subject-agnostic — keyed on
    public PRESENCE, never a rel-name literal. Fail-safe: any error → log + None (caller keeps
    its computed values; the reconcile migration is the backstop), never crashes the sweep.
    """
    rt = (rel_type or "").strip().lower()
    if not rt:
        return None
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT is_hierarchy_rel, category, tail_types, fact_class,"
                "       storage_target, inverse_rel_type, is_symmetric"
                "  FROM public.rel_types WHERE rel_type = %s",
                (rt,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return dict(zip(_SEED_STRUCTURAL_FIELDS, row))
    except Exception as e:
        log.error(f"re_embedder.seed_structural_flags_read_failed rel_type={rt}: {str(e)[:160]}")
        return None


# Lazy-loaded local embedder — initialized on first use, None if fastembed not installed
_local_embedder = None


def _get_local_embedder():
    global _local_embedder
    if _local_embedder is not None:
        return _local_embedder
    try:
        from fastembed import TextEmbedding
        _cache = os.getenv("FASTEMBED_CACHE_PATH")
        # PURE CONFIG — HF model id from env (no code literal); default lives in .env.example
        # and matches the Dockerfile bake. Unset → no-op local embedder (fail-safe, external path).
        _fe_model = (os.getenv("FASTEMBED_MODEL") or "").strip()
        if not _fe_model:
            log.warning("local_embedder.unavailable reason=FASTEMBED_MODEL_unset")
            _local_embedder = False
            return _local_embedder
        _local_embedder = TextEmbedding(_fe_model,
                                        cache_dir=_cache) if _cache else TextEmbedding(_fe_model)
        log.info(f"local_embedder.initialized model={_fe_model} cache_dir={_cache or 'default'}")
    except ImportError:
        log.warning("local_embedder.unavailable reason=fastembed_not_installed")
        _local_embedder = False
    except Exception as e:
        log.warning(f"local_embedder.init_failed error={e}")
        _local_embedder = False
    return _local_embedder


# Global pooled HTTP client for embedding and Qdrant calls (dBug-051 fix)
# Prevents connection churn from bare httpx.post() calls
_http_client = httpx.Client(timeout=30.0, limits=httpx.Limits(max_connections=10))


def _get_circuit_breaker_status() -> dict:
    """Get circuit breaker status for LLM calls (for awareness in background loop).

    Returns:
        Dict with 'is_open' key indicating if circuit breaker is active
    """
    try:
        from src.api.llm_calls import _llm_circuit_breaker
        return {"is_open": _llm_circuit_breaker.is_open()}
    except Exception:
        # If import fails, assume circuit is closed (normal operation)
        return {"is_open": False}

# Marker for internal FaultLine prompts (dprompt-128) — prevents context bloat if looped back
_FAULTLINE_INTERNAL_PREFIX = "[FaultLine-Internal]"

_http_client_sync: httpx.Client = None


def _detect_redis_endpoint() -> str:
    """Auto-detect Redis endpoint (container-aware).

    Priority chain:
    1. REDIS_URL env var override
    2. Docker service name (redis) on default port 6379
    3. Localhost (dev fallback)

    This allows the same code to work in Docker containers (service name)
    and local dev environments (localhost).
    """
    # Explicit override
    if os.getenv("REDIS_URL"):
        return os.getenv("REDIS_URL")

    # Docker service name (most likely in container)
    candidates = [
        "redis://redis:6379/0",           # Docker service name (most reliable)
        "redis://localhost:6379/0",       # Local development fallback
        "redis://127.0.0.1:6379/0",       # Localhost IPv4 fallback
    ]

    for url in candidates:
        try:
            test_client = redis.from_url(url, decode_responses=True, socket_timeout=2)
            test_client.ping()
            log.info(f"redis_detection.success url={url[:30]}")
            return url
        except Exception:
            continue

    # If all fail, return Docker service name (will be retried with exponential backoff)
    log.warning("redis_detection.all_failed using_service_name=redis")
    return "redis://redis:6379/0"


class EmbeddingCache:
    """Redis-backed cache for rel_type embeddings (GROWS WITH SYSTEM).

    Caches embeddings of rel_type name strings to avoid re-embedding during
    ontology evaluation. Survives restarts, scales horizontally.
    """

    def __init__(self, redis_url: Optional[str] = None):
        """Initialize Redis connection for embedding cache.

        Args:
            redis_url: Redis connection URL (auto-detects if not provided)
        """
        self.redis_url = redis_url or _detect_redis_endpoint()
        self.ttl = int(os.getenv("EMBEDDING_CACHE_TTL", "86400"))  # 1 day default
        self.prefix = "embedding:relationship:"
        self.client = None

        try:
            self.client = redis.from_url(self.redis_url, decode_responses=True)
            self.client.ping()
            log.info(f"embedding_cache.redis_connected url={self.redis_url[:30]} ttl_seconds={self.ttl}")
        except Exception as e:
            log.warning(f"embedding_cache.redis_connection_failed error={str(e)}")
            self.client = None

    def get(self, text: str) -> Optional[list]:
        """Retrieve cached embedding (returns None on miss or error)."""
        if not self.client:
            return None
        try:
            key = f"{self.prefix}{text}"
            cached = self.client.get(key)
            if cached:
                return json.loads(cached)
        except Exception as e:
            log.warning(f"embedding_cache.get_error error={str(e)} key={text[:40]}")
        return None

    def set(self, text: str, vector: list) -> bool:
        """Cache an embedding with TTL (returns success flag)."""
        if not self.client:
            return False
        try:
            key = f"{self.prefix}{text}"
            self.client.setex(key, self.ttl, json.dumps(vector))
            return True
        except Exception as e:
            log.warning(f"embedding_cache.set_error error={str(e)} key={text[:40]}")
            return False

    def clear_pattern(self, pattern: str) -> int:
        """Clear all keys matching pattern (e.g., 'embedding:relationship:*')."""
        if not self.client:
            return 0
        try:
            # Use SCAN to avoid blocking on large keyspaces
            deleted = 0
            cursor = 0
            while True:
                cursor, keys = self.client.scan(cursor, match=pattern, count=1000)
                if keys:
                    deleted += self.client.delete(*keys)
                if cursor == 0:
                    break
            return deleted
        except Exception as e:
            log.warning(f"embedding_cache.clear_error error={str(e)} pattern={pattern}")
            return 0


_embedding_cache = EmbeddingCache()


def derive_collection(user_id: str) -> str:
    """Derive Qdrant collection name from user_id."""
    if user_id in ("", "anonymous", "legacy"):
        return os.getenv("QDRANT_COLLECTION", "faultline-test")
    return f"faultline-{user_id}"


def collection_to_schema_name(collection: str) -> Optional[str]:
    """PURE: map a per-user Qdrant collection name → its tenant PG schema name.

    Collection form is `faultline-<user_id>` where user_id is a dashed uuid; the paired PG
    schema is `faultline_<user_id_underscores>` (see src.provisioning.schema_manager.
    derive_schema_name ∘ derive_user_slug_from_uuid, which simply rewrites '-'→'_'). We
    mirror that derivation here without importing so this stays a cheap pure helper.

    Returns None for the shared/test/main collections (no per-tenant schema to check) so the
    caller never skips those — they are processed exactly as today.
    """
    if not collection or not collection.startswith("faultline-"):
        return None
    user_id = collection[len("faultline-"):]
    # Shared/legacy collections have no dedicated per-tenant schema — don't try to skip them.
    if user_id in ("", "test", "main", "anonymous", "legacy"):
        return None
    return "faultline_" + user_id.replace("-", "_")


def should_reconcile_collection(collection: str, existing_schemas) -> bool:
    """PURE skip/process decision for the reconcile loop — ORPHAN-SKIP.

    Returns True (process/reconcile) unless the collection is a per-tenant collection whose
    PG schema does NOT exist (an orphan) — in which case returns False (skip).

    FAIL TOWARD PROCESSING (never silently drop work on a check failure):
      - `existing_schemas is None` (schema-existence check errored/unavailable) → True.
      - Shared/test/main/legacy collections (no derivable per-tenant schema) → True.
      - Schema present in the set → True.
      - Per-tenant collection whose schema is absent from a KNOWN-GOOD set → False (orphan).
    """
    if existing_schemas is None:
        return True  # couldn't check → process, never silently skip
    schema = collection_to_schema_name(collection)
    if schema is None:
        return True  # shared/test/legacy — process as today
    return schema in existing_schemas  # orphan (schema gone) → False → skip


def taxonomy_link_is_severed(cur, parent_taxonomy: str, child_taxonomy: str) -> bool:
    """STRUCTURAL CORRECTION lock (DESIGN-hierarchy-ladder §"user correction is REAL").

    True iff the user has SEVERED the nesting `parent ⊃ child` — i.e. `child` is in
    `parent.severed_taxonomies`. The user's structural directive is Class A and
    NOT-superseded: the background nesting-growth engine MUST consult this before
    adding any member_taxonomy and refuse to re-add a user-severed link. User > engine,
    durably — the engine never gets to "re-discover" what the user severed.

    Per-tenant: `cur` must already be bound to the tenant search_path (no public).
    Fail-safe: on any error returns True (refuse the link) — a missing-check must never
    silently let the engine override a user severance.
    """
    try:
        cur.execute(
            "SELECT %s = ANY(COALESCE(severed_taxonomies, '{}')) "
            "FROM entity_taxonomies WHERE taxonomy_name = %s",
            (child_taxonomy, parent_taxonomy),
        )
        row = cur.fetchone()
        return bool(row[0]) if row and row[0] is not None else False
    except Exception:
        # Fail CLOSED: if we cannot verify, do NOT re-link (user authority wins).
        return True


def add_member_taxonomy_if_not_severed(cur, parent_taxonomy: str, child_taxonomy: str) -> bool:
    """The ONLY sanctioned path for the growth engine to nest `child` under `parent`
    (add to member_taxonomies). Consults the user-severance lock first; refuses if the
    user severed the link or the parent row is user_corrected for this child. Returns
    True iff the link was added. Per-tenant (caller sets search_path; no public).

    Any future nesting-growth (rung-6 convergence/bridging) MUST route through here so
    the structural-correction lock can never be bypassed.
    """
    if taxonomy_link_is_severed(cur, parent_taxonomy, child_taxonomy):
        log.info("re_embedder.nesting_growth_refused_user_severed "
                 f"parent={parent_taxonomy} child={child_taxonomy}")
        return False
    try:
        cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

            """
            UPDATE entity_taxonomies
               SET member_taxonomies =
                   CASE WHEN %s = ANY(COALESCE(member_taxonomies,'{}'))
                        THEN COALESCE(member_taxonomies,'{}')
                        ELSE array_append(COALESCE(member_taxonomies,'{}'), %s)
                   END
             WHERE taxonomy_name = %s
               -- never clobber a user-corrected row's nesting for the severed child
               AND NOT (%s = ANY(COALESCE(severed_taxonomies,'{}')))
            """,
            (child_taxonomy, child_taxonomy, parent_taxonomy, child_taxonomy),
        )
        return cur.rowcount > 0
    except Exception as e:
        log.warning("re_embedder.add_member_taxonomy_failed "
                    f"parent={parent_taxonomy} child={child_taxonomy} error={str(e)[:120]}")
        return False


def fetch_unsynced(db_conn, user_id: str, confidence_threshold: float = 0.0) -> list[dict]:
    """Fetch all non-superseded facts where qdrant_synced = false and confidence >= threshold.

    Per-user schema context: user_id is passed as parameter (schema provides isolation).
    """
    with db_conn.cursor() as cur:
        cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

            f"""
            SELECT id, subject_id, object_id, rel_type, provenance,
                   confidence, confirmed_count, last_seen_at, contradicted_by,
                   fact_class, source_ref
            FROM facts
            WHERE qdrant_synced = false AND (superseded_at IS NULL)
            AND confidence >= %s
            ORDER BY id ASC
            """,
            (confidence_threshold,)
        )
        rows = cur.fetchall()

    return [
        {
            "id": row[0],
            "subject_id": row[1],
            "object_id": row[2],
            "rel_type": row[3],
            "provenance": row[4],
            "user_id": user_id,
            "confidence": row[5] if row[5] is not None else 1.0,
            "confirmed_count": row[6] if row[6] is not None else 0,
            "last_seen_at": row[7],
            "contradicted_by": row[8],
            # facts-table rows default to 'A' (only Class A/promoted-B live here);
            # carry it into the Qdrant payload so read-back tiering is correct.
            "fact_class": row[9] or "A",
            # Citable provenance (migration 128) — NULL for conversational facts.
            "source_ref": row[10],
        }
        for row in rows
    ]


def fetch_unsynced_staged(db_conn, user_id: str) -> list[dict]:
    """Fetch staged_facts where qdrant_synced = false and not yet promoted or expired.

    Per-user schema context: user_id is passed as parameter (schema provides isolation).
    """
    with db_conn.cursor() as cur:
        cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

            """
            SELECT id, subject_id, object_id, rel_type, provenance,
                   confidence, confirmed_count, last_seen_at, fact_class, source_ref
            FROM staged_facts
            WHERE qdrant_synced = false
              AND promoted_at IS NULL
              AND expires_at > now()
              AND deleted_at IS NULL
            ORDER BY id ASC
            """
        )
        rows = cur.fetchall()
    return [
        {
            "id": row[0],
            "subject_id": row[1],
            "object_id": row[2],
            "rel_type": row[3],
            "provenance": row[4],
            "user_id": user_id,
            "confidence": row[5] if row[5] is not None else 0.6,
            "confirmed_count": row[6] if row[6] is not None else 0,
            "last_seen_at": row[7],
            "contradicted_by": None,
            "staged_id": row[0],
            "fact_class": row[8],
            # Citable provenance (migration 128) — NULL for conversational facts.
            "source_ref": row[9],
        }
        for row in rows
    ]


def resolve_display_names_for_facts(db_conn, rows: list[dict]) -> list[dict]:
    """
    For each fact row, resolve subject_id and object_id UUID surrogates
    to their preferred display names via entity_aliases.
    Falls back to the UUID string if no alias is found.
    Returns a new list of dicts with added 'subject_display' and 'object_display' keys.
    """
    if not rows:
        return rows

    # Collect all unique entity IDs across all rows
    entity_ids = set()
    for row in rows:
        entity_ids.add(row["subject_id"])
        entity_ids.add(row["object_id"])

    # Batch lookup preferred aliases
    display_map = {}
    try:
        placeholders = ",".join(["%s"] * len(entity_ids))
        with db_conn.cursor() as cur:
            cur.execute(
                f"SELECT entity_id, alias FROM entity_aliases "
                f"WHERE entity_id IN ({placeholders}) AND is_preferred = true",
                list(entity_ids),
            )
            for entity_id, alias in cur.fetchall():
                display_map[entity_id] = alias
    except Exception as e:
        log.warning(f"re_embedder.display_name_lookup_failed: {e}")

    # Attach display names to each row
    resolved = []
    for row in rows:
        resolved.append({
            **row,
            "subject_display": display_map.get(row["subject_id"], row["subject_id"]),
            "object_display": display_map.get(row["object_id"], row["object_id"]),
        })
    return resolved


def embed_text(text: str, qwen_api_url: str, timeout: float = 30.0, fallback: bool = True, embedding_url: str = None, task: str = "search_document") -> list[float] | None:
    """
    Embed text using nomic-embed-text-v1.5.

    Priority: local CPU fastembed first (zero network cost), external API as fallback.
    fallback=True  (default, used by re_embedder): returns a hash vector on failure so
                   the re_embedder loop keeps running.
    fallback=False (used by /query):               returns None on failure so the caller
                   can skip the Qdrant search rather than searching with a meaningless vector.
    embedding_url: Explicit embedding endpoint (optional, overrides inferred path).
    task:          nomic-embed-text-v1.5 TASK PREFIX (asymmetric retrieval): STORED text uses
                   ``search_document`` (default), QUERY/recall text uses ``search_query``. The
                   prefix is applied ONCE here so every backend (local fastembed + external API)
                   sees the same prefixed input — both sides MUST agree or the cosine space
                   diverges. Class-C lane only; A/B never embed.
    """
    # nomic asymmetric-retrieval task prefix — applied to the RAW text exactly once, before any
    # backend sees it. (``embedding:relationship:`` elsewhere is a Redis CACHE KEY, not this.)
    _prefix = f"{task}: " if task else ""
    text = f"{_prefix}{text}" if _prefix and not text.startswith(_prefix) else text
    # LOCAL CPU FIRST: fastembed nomic-embed-text (pre-cached in Docker image)
    local = _get_local_embedder()
    if local:
        try:
            vectors = list(local.embed([text]))
            if vectors:
                return list(vectors[0])
        except Exception as local_err:
            log.warning(f"local_embedder.failed: {local_err}")

    # EXTERNAL FALLBACK: hit LLM backend /v1/embeddings
    if embedding_url:
        embed_url = embedding_url
    else:
        # THE ONE JOIN, not a string replace. `.replace("/chat/completions", "/embeddings")`
        # is one of the four hand-rolled builders llm_client's endpoint header names by name:
        # it silently produces "" for any endpoint that does not literally contain that
        # substring (anthropic's /v1/messages, openwebui's /api/chat/completions → wrong or
        # unchanged), and it cannot know the backend. get_embedding_url IS the join's
        # embeddings sibling and is backend-aware.
        try:
            from src.api.llm_client import get_embedding_url
            embed_url = get_embedding_url(qwen_api_url or "")
        except Exception:  # noqa: BLE001 — fail-safe: no resolvable endpoint → skip the call
            embed_url = ""

    # No endpoint to fall back to — skip the call entirely rather than POST to an
    # empty/derived URL and eat an exception per row.
    if not embed_url:
        if fallback:
            return hash_vector(text)
        return None

    try:
        if _http_client_sync:
            response = _http_client_sync.post(
                embed_url,
                json={"model": _EMBEDDING_MODEL, "input": text},
                headers=get_embedding_headers(),
                timeout=timeout,
            )
        else:
            response = _http_client.post(
                embed_url,
                json={"model": _EMBEDDING_MODEL, "input": text},
                headers=get_embedding_headers(),
                timeout=timeout,
            )
        response.raise_for_status()
        data = response.json()

        if "data" in data and len(data["data"]) > 0:
            return data["data"][0]["embedding"]

        raise ValueError("Invalid embedding response format")

    except Exception as e:
        if fallback:
            log.warning(f"re_embedder.embed_failed text_preview={text[:50]} falling back to hash vector: {e}")
            return hash_vector(text)
        log.error(f"re_embedder.embed_failed text_preview={text[:50]} no fallback: {e}")
        return None


def hash_vector(text: str, size: int = 768) -> list[float]:
    """
    Generate deterministic hash-based vector from text.
    Same text always produces same vector.
    """
    # Use SHA256 hash of text as seed
    hash_bytes = hashlib.sha256(text.encode('utf-8')).digest()

    # Convert to deterministic float values in range [-1, 1]
    vector = []
    for i in range(size):
        # Use modulo to cycle through hash bytes
        byte_val = hash_bytes[i % len(hash_bytes)]
        # Normalize to [-1, 1]
        normalized = (byte_val / 255.0) * 2.0 - 1.0
        vector.append(normalized)

    return vector


# #154: after a 409 on create, how long to wait for the winning creator's collection to appear.
_RACE_RECHECK_ATTEMPTS = 20
_RACE_RECHECK_DELAY_S = 0.5


def ensure_collection(collection: str, qdrant_url: str) -> bool:
    """
    Check if Qdrant collection exists with the correct anonymous vector schema, create or
    recreate if not.

    Validates that an existing collection uses anonymous-vector schema
    {"size": 768, "distance": "Cosine"}.  Collections pre-created by OpenWebUI or other
    tools use named-vector schema ({"vectors": {}}) which causes Qdrant to return 400 on
    every bare-list search.  When a schema mismatch is detected the collection is deleted
    and recreated with the correct schema.

    Returns True if collection exists with correct schema or was created/recreated.
    Returns False on any unrecoverable failure.
    """
    _EXPECTED_DIM = 768
    _CORRECT_SCHEMA = {"size": _EXPECTED_DIM, "distance": "Cosine"}

    from src.api.qdrant_partition import qdrant_headers, ensure_tenant_index, shared_mode

    def _create_collection() -> bool:
        """PUT the collection with the correct anonymous-vector schema."""
        create_response = httpx.put(
            f"{qdrant_url}/collections/{collection}",
            json={"vectors": _CORRECT_SCHEMA},
            headers=qdrant_headers(),
            timeout=10.0,
        )
        if create_response.status_code == 200:
            log.info(f"re_embedder.collection_created collection={collection}")
            # Shared-collection model: co-locate each tenant's points via the is_tenant index.
            if shared_mode():
                ensure_tenant_index(collection, qdrant_url)
            return True
        if create_response.status_code == 409:
            # Lost a create race (#154): the API lifespan and the re-embedder process start
            # together and both create the collection on a fresh install. "Already exists"
            # is success once the winner's collection is confirmed to have our schema. The
            # winner's create can still be in flight when our 409 arrives (measured on a fresh
            # install: the immediate GET answered 500), so poll briefly.
            vectors_cfg = None
            for _attempt in range(_RACE_RECHECK_ATTEMPTS):
                try:
                    check = httpx.get(f"{qdrant_url}/collections/{collection}",
                                      headers=qdrant_headers(), timeout=10.0)
                    if check.status_code == 200:
                        vectors_cfg = (check.json().get("result", {}).get("config", {})
                                       .get("params", {}).get("vectors", None))
                        break
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(_RACE_RECHECK_DELAY_S)
            if isinstance(vectors_cfg, dict) and vectors_cfg.get("size") == _EXPECTED_DIM:
                log.info(f"re_embedder.collection_already_exists collection={collection}")
                if shared_mode():
                    ensure_tenant_index(collection, qdrant_url)
                return True
        log.error(
            f"re_embedder.collection_create_failed collection={collection} "
            f"status={create_response.status_code}"
        )
        return False

    try:
        # Use pooled client instead of bare httpx.get() (dBug-051: prevent connection churn)
        response = _http_client.get(
            f"{qdrant_url}/collections/{collection}",
            headers=qdrant_headers(),
            timeout=10.0
        )

        if response.status_code == 200:
            # Shared-collection model: ensure the tenant index exists even when the
            # collection was created by an earlier run / another writer (idempotent).
            if shared_mode():
                ensure_tenant_index(collection, qdrant_url)
            # Validate that the existing collection uses anonymous-vector schema.
            # OpenWebUI may pre-create collections with named-vector schema ("vectors": {})
            # which causes Qdrant to return 400 on bare-list searches.
            try:
                body = response.json()
                vectors_cfg = (
                    body.get("result", {})
                        .get("config", {})
                        .get("params", {})
                        .get("vectors", None)
                )
            except Exception as parse_err:
                log.warning(
                    f"re_embedder.collection_schema_parse_failed collection={collection} "
                    f"error={parse_err} — treating as valid to avoid data loss"
                )
                return True

            # Anonymous-vector schema: vectors_cfg is a dict with a top-level "size" key.
            schema_ok = (
                isinstance(vectors_cfg, dict)
                and vectors_cfg.get("size") == _EXPECTED_DIM
            )

            if schema_ok:
                return True

            # Schema mismatch — log, delete, recreate.
            log.warning(
                f"re_embedder.collection_schema_mismatch "
                f"collection={collection} "
                f"found={vectors_cfg!r} "
                f"expected=\"anonymous {_EXPECTED_DIM}-dim cosine\""
            )

            delete_response = _http_client.delete(
                f"{qdrant_url}/collections/{collection}",
                headers=qdrant_headers(),
                timeout=10.0,
            )
            if delete_response.status_code not in (200, 404):
                log.error(
                    f"re_embedder.collection_delete_failed collection={collection} "
                    f"status={delete_response.status_code} — cannot recreate"
                )
                return False

            result = _create_collection()
            if result:
                log.info(
                    f"re_embedder.collection_recreated_after_schema_fix "
                    f"collection={collection}"
                )
            return result

        if response.status_code == 404:
            return _create_collection()

        log.error(
            f"re_embedder.collection_check_unexpected collection={collection} "
            f"status={response.status_code}"
        )
        return False

    except Exception as e:
        log.error(f"re_embedder.collection_check_failed collection={collection} error={e}")
        return False


# Stable namespace for Qdrant point-id derivation. Fixed UUID — do NOT change once
# data exists under it, or every derived point id shifts.
_QDRANT_POINT_NS = uuid.UUID("9f3c5b1e-7a42-5d6e-8c0a-1b2c3d4e5f60")


def derive_qdrant_point_id(source_table: str, fact_id) -> str:
    """SINGLE source of truth for Qdrant point ids.

    `facts` and `staged_facts` are independent BIGSERIAL sequences that share ONE
    per-user Qdrant collection, so the bare integer id N can exist in BOTH tables
    and a raw `"id": N` point aliases facts#N onto staged#N (collision / data loss).

    Derive a collision-free, deterministic UUIDv5 over (source_table, fact_id).
    Deterministic so the same row always maps to the same point (idempotent upsert,
    and deletes can recompute the id without scrolling). Qdrant accepts UUID-string
    point ids. Both keys are also carried in the payload (`source_table`, `fact_id`)
    so filtered deletes remain possible.

    Every write/re-upsert/delete that addresses a point BY id must route through this
    helper; never key by the bare table id.
    """
    return str(uuid.uuid5(_QDRANT_POINT_NS, f"{source_table}:{int(fact_id)}"))


def upsert_to_qdrant(row: dict, vector: list[float], collection: str, qdrant_url: str, source_table: str = "facts") -> bool:
    """
    Upsert fact embedding to Qdrant collection.

    Args:
        source_table: Which DB table this row came from — "facts" or "staged_facts".
            Stored in the Qdrant payload so filtered deletes can target the correct
            source without risking ID collisions (both tables share independent SERIAL
            sequences; integer ID=N can exist in both tables simultaneously).
    Returns True on success, False on failure.
    """
    payload = {
        "subject": row.get("subject_display", row["subject_id"]),
        "object": row.get("object_display", row["object_id"]),
        "rel_type": row["rel_type"],
        "provenance": row["provenance"],
        "user_id": row["user_id"],
        "fact_id": int(row["id"]),
        "source_table": source_table,
        "confidence": row.get("confidence", 1.0),
        "confirmed_count": row.get("confirmed_count", 0),
        "last_seen_at": row["last_seen_at"].isoformat() if row.get("last_seen_at") else None,
        "contradicted": row.get("contradicted_by") is not None,
        # fact_class persisted so /query read-back tiers A/B correctly instead of
        # blanket-defaulting Qdrant hits to Class C. facts-table rows → 'A' (or
        # promoted 'B'); staged rows carry their own 'B'/'C'. Fall back by table:
        # facts → 'A' (only A/promoted-B live there), staged → 'C'.
        "fact_class": row.get("fact_class") or ("A" if source_table == "facts" else "C"),
        # Citable provenance (migration 128): document/web citation carried into the
        # vector payload so a Qdrant-lane recall can surface where a fact came from.
        # None for conversational facts.
        "source_ref": row.get("source_ref"),
    }
    # Partition choke-point: stamp tenant_id (no-op in collection_per_seat) + derive a
    # collision-free point id. In collection_per_seat this is byte-for-byte the legacy
    # UUIDv5(source_table:fact_id); in shared_payload it folds the seat uuid in so two
    # seats' facts#N never alias onto one shared point.
    from src.api.qdrant_partition import stamp_tenant, resolve_point_id, qdrant_headers
    payload = stamp_tenant(payload, row.get("user_id"))
    point_id = resolve_point_id(row.get("user_id"), source_table, row["id"])
    try:
        # Use persistent pooled client if available, fallback to httpx.put() for backward compatibility
        if _http_client_sync:
            response = _http_client_sync.put(
                f"{qdrant_url}/collections/{collection}/points",
                json={
                    "points": [
                        {
                            "id": point_id,
                            "vector": vector,
                            "payload": payload,
                        }
                    ]
                },
                headers=qdrant_headers(),
                timeout=30.0
            )
        else:
            response = httpx.put(
                f"{qdrant_url}/collections/{collection}/points",
                json={
                    "points": [
                        {
                            "id": point_id,
                            "vector": vector,
                            "payload": payload,
                        }
                    ]
                },
                headers=qdrant_headers(),
                timeout=30.0
            )

        if response.status_code == 200:
            return True

        log.error(f"re_embedder.qdrant_error fact_id={row['id']} status={response.status_code} body={response.text}")
        return False

    except Exception as e:
        log.error(f"re_embedder.qdrant_error fact_id={row['id']}: {e}")
        return False


def _qdrant_delete_fact_point(qdrant_url: str, user_id, source_table: str, fact_id,
                              *, timeout: float = 10.0):
    """Partition-aware delete of ONE fact/staged point (choke-point routed).

    collection_per_seat → byte-for-byte the legacy bare-id `{"points":[uuid]}` delete on the
    per-seat collection. shared_payload → the id is tenant-namespaced AND the delete is a
    filtered `{filter:{must:[tenant, {has_id:[id]}]}}` so it can never touch a colliding id
    from another tenant; an unbound seat THROWS (require_tenant). Fail-safe: best-effort at
    the call sites (they wrap in try/except), so this returns the response or raises for the
    unbound-tenant poison case (a bug, surfaced loud)."""
    from src.api.qdrant_partition import (
        resolve_partition, require_tenant, build_delete_body, resolve_point_id, qdrant_headers,
    )
    collection, tflt = resolve_partition(user_id, "memory")
    require_tenant(tflt, op="delete", collection=collection)
    pid = resolve_point_id(user_id, source_table, fact_id, "memory")
    return httpx.post(
        f"{qdrant_url}/collections/{collection}/points/delete",
        json=build_delete_body(tflt, point_ids=[pid]),
        headers=qdrant_headers(),
        timeout=timeout,
    )


def mark_synced(db_conn, fact_id: int) -> None:
    """Mark a fact as synced to Qdrant."""
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE facts SET qdrant_synced = true WHERE id = %s",
            (fact_id,)
        )
    db_conn.commit()


# REMOVED 2026-08-12 — `promote_facts(db_conn)`. Zero callers anywhere in the repo (verified
# across src/, tests/, benchmarks/, tools/), so it never ran. Deleted rather than
# left lying around because if anything ever HAD wired it up it would have been wrong twice
# over, and its name sits one keystroke from the two functions that are real:
#   • It inflated `facts.confidence` by +0.1 for any row with `confirmed_count >= 2` OR simply
#     older than 7 days — i.e. confidence rising with AGE, on an unbounded blanket UPDATE of
#     every live row in the bound schema. Confidence is set from PROVENANCE at ingest
#     (`assign_class_and_confidence`); nothing is entitled to drift it upward later.
#   • It called that "promotion to long-term memory", which is not what promotion means here.
#     The real mechanism is the C→B tier transition at `confirmed_count >= 3` — see
#     `promote_staged_facts` and `promote_class_c_hits` in this module. A and B never promote.


def _reconcile_hierarchy_links(dsn: str, schema_name: str) -> int:
    """Post-expand reconciliation: create missing instance_of links for entities
    whose entity_type matches a hierarchy node alias.

    Called periodically by re_embedder. Finds entities that should be linked
    to hierarchy nodes but aren't yet (because they were ingested before /expand).

    Returns count of new instance_of facts created.
    """
    created = 0
    try:
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema_name}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema from UUID-derived source with validation

                # Find hierarchy node IDs (things that appear as objects of subclass_of/instance_of)
                cur.execute("""
                    SELECT DISTINCT object_id FROM facts
                    WHERE rel_type IN ('subclass_of', 'instance_of', 'part_of')
                      AND superseded_at IS NULL
                    UNION
                    SELECT DISTINCT object_id FROM staged_facts
                    WHERE rel_type IN ('subclass_of', 'instance_of', 'part_of')
                      AND promoted_at IS NULL
                """)
                hierarchy_node_ids = {row[0] for row in cur.fetchall()}
                if not hierarchy_node_ids:
                    return 0

                # Get hierarchy node aliases
                cur.execute("""
                    SELECT entity_id, alias FROM entity_aliases
                    WHERE entity_id = ANY(%s)
                """, (list(hierarchy_node_ids),))
                alias_to_node = {}
                for row in cur.fetchall():
                    alias_to_node[row[1].lower()] = row[0]

                if not alias_to_node:
                    return 0

                # Find entities whose entity_type (lowercased) matches a hierarchy alias
                # but don't already have an instance_of to that node, and haven't had
                # that classification explicitly retracted by the user (superseded_at IS NOT NULL
                # on an instance_of fact means the user corrected/retracted it — skip those).
                for type_name, node_id in alias_to_node.items():
                    try:
                        cur.execute("""
                            SELECT e.id FROM entities e
                            WHERE LOWER(e.entity_type) = %s
                              AND e.id != %s
                              -- HARD LINE / binding: only place a GENUINELY UNPLACED entity. Coarse
                              -- GLiNER2 entity_type must never override or duplicate a real placement:
                              --  * an existing instance_of ⇒ a named instance / already-typed (e.g.
                              --    `rex instance_of poodle`) — adding `instance_of animal` is
                              --    non-transitive pollution welded onto the memory;
                              --  * an existing subclass_of ⇒ a TYPE node (dog/poodle) — a type is
                              --    classified by subclass_of, never `instance_of` its supertype.
                              AND NOT EXISTS (
                                  SELECT 1 FROM facts f WHERE f.subject_id = e.id
                                    AND f.rel_type = 'instance_of' AND f.superseded_at IS NULL)
                              AND NOT EXISTS (
                                  SELECT 1 FROM staged_facts sf WHERE sf.subject_id = e.id
                                    AND sf.rel_type = 'instance_of' AND sf.promoted_at IS NULL)
                              AND NOT EXISTS (
                                  SELECT 1 FROM facts fs WHERE fs.subject_id = e.id
                                    AND fs.rel_type = 'subclass_of' AND fs.superseded_at IS NULL)
                              AND NOT EXISTS (
                                  SELECT 1 FROM staged_facts sfs WHERE sfs.subject_id = e.id
                                    AND sfs.rel_type = 'subclass_of' AND sfs.promoted_at IS NULL)
                              AND NOT EXISTS (
                                  SELECT 1 FROM facts f2 WHERE f2.subject_id = e.id
                                    AND f2.rel_type = 'instance_of' AND f2.superseded_at IS NOT NULL)
                        """, (type_name, node_id))
                    except Exception as _qe:
                        log.error("reconcile_hierarchy.candidate_query_failed "
                                  f"schema={schema_name} type_name={type_name} error={str(_qe)}")
                        continue

                    for (entity_id,) in cur.fetchall():
                        # FIRST-CLASS value/place stamp (migration 192, flag VALUE_PLACE_FIRST_CLASS
                        # default OFF). THE primary ASYNC gap this closes: reconcile_hierarchy
                        # auto-places any entity whose entity_type matches a hierarchy alias with NO
                        # value/name guard. A user VALUE node (stamped favorite_colour/scalar object)
                        # must NEVER be auto-typed `instance_of` — the async tier has no in-flight
                        # edge, so the persisted stamp is the only guard. OFF → False (byte-identical).
                        if _node_role.is_protected_value(conn, entity_id):
                            continue
                        try:
                            cur.execute("""
                                -- STATE THE AUTHORSHIP, DO NOT INHERIT IT: reconciliation is the
                                -- engine typing a node, never the person saying so. Recall's
                                -- type-assertion gate reads fact_provenance, so this row must
                                -- carry it explicitly rather than inherit the column default.
                                INSERT INTO staged_facts
                                    (subject_id, object_id, rel_type, fact_class, provenance,
                                     fact_provenance, confidence, first_seen_at, expires_at)
                                VALUES (%s, %s, 'instance_of', 'B', 'hierarchy_reconciliation',
                                        'llm_inferred', 0.6, now(), now() + interval '30 days')
                                ON CONFLICT (subject_id, object_id, rel_type)
                                DO UPDATE SET last_seen_at = now(),
                                    confirmed_count = staged_facts.confirmed_count + 1,
                                    expires_at = COALESCE(staged_facts.expires_at,
                                                          now() + interval '30 days')
                            """, (entity_id, node_id))
                            created += 1
                        except Exception as _ie:
                            log.error("reconcile_hierarchy.insert_failed "
                                      f"schema={schema_name} entity_id={entity_id} "
                                      f"node_id={node_id} error={str(_ie)}")
                            continue

                if created:
                    conn.commit()
    except Exception as e:
        log.warning(f"reconcile_hierarchy.failed schema={schema_name} error={e}")

    return created


def _upgrade_staged_facts_with_known_rels(dsn: str, schema_name: str) -> int:
    """Upgrade Class C staged_facts to Class B when their rel_type now exists in rel_types table."""
    upgraded = 0
    try:
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema_name}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema from UUID-derived source with validation
                # PER-TENANT: join against the tenant's OWN rel_types (seeded + grown).
                # public is template-only — a rel approved into THIS tenant must trigger the
                # C→B upgrade, which a public-only join would miss. UNQUALIFIED resolves to
                # <schema>.rel_types under the bound search_path.
                cur.execute("""
                    UPDATE staged_facts sf
                    SET fact_class = 'B', confidence = GREATEST(sf.confidence, 0.6)
                    FROM rel_types rt
                    WHERE sf.rel_type = rt.rel_type
                      AND sf.fact_class = 'C'
                      AND sf.promoted_at IS NULL
                      AND (sf.expires_at IS NULL OR sf.expires_at > now())
                """)
                upgraded = cur.rowcount
                if upgraded:
                    conn.commit()
    except Exception as e:
        log.warning(f"upgrade_staged.failed schema={schema_name} error={e}")
    return upgraded


def promote_staged_facts(db_conn, qdrant_url: str, user_id: str = None, schema_name: str = None, promotion_threshold: int = 3) -> int:
    """
    Promote Class B staged facts to facts table when confirmed_count >= threshold.

    STAGED-FACT LIFECYCLE — confirmed_count counter (one of TWO). This job pairs
    with expire_staged_facts(); together they drive the confirmed_count lifecycle:
      • confirmed_count (starts 0): re-ingest + scoped-query driven. Writers:
        main.py _commit_staged() (ON CONFLICT) and the scoped staged-fact recall
        bump (~main.py:13342). Lifecycle: expire_staged_facts / promote_staged_facts.
      • hit_count (starts 1): recall-relevance-hit driven (~main.py:14753).
        Lifecycle: decay_class_c_hits / promote_class_c_hits.
    Do NOT conflate the two counters. Both promote C→B at >= 3.

    This function promotes C→B: it first upgrades C→B in staged_facts (the
    UPDATE ... SET fact_class='B' below), then INSERTs the now-Class-B rows into
    the facts table with fact_class='B' (NOT Class A) — the row keeps its
    Class-B confidence; promotion does not elevate it to user-stated authority.

    Confirmation Mechanism (Source: _commit_staged in main.py, lines 1017-1075):
    ────────────────────────────────────────────────────────────────────────────
    Staged facts accumulate a confirmed_count every time they're re-ingested.
    The count increments via PostgreSQL ON CONFLICT clauses in main.py _commit_staged().

    When confirmed_count >= promotion_threshold (default: 3):
    • Fact has appeared in >= 4 separate ingest calls (calls 0→1→2→3)
    • System confidence increases with each occurrence
    • promote_staged_facts() moves the row to the facts table with fact_class='B'
      (Class-B confidence floor — NOT Class A; Class A is user-stated only)
    • Staged row marked as promoted (non-destructive soft delete via promoted_at)

    Full Workflow:
    1. Fact inserted 4 times → confirmed_count increments: 0→1→2→3
    2. Re-embedder polls every 60 seconds → calls promote_staged_facts()
    3. Queries: SELECT ... FROM staged_facts WHERE confirmed_count >= 3
    4. For each candidate: INSERT into facts table with ON CONFLICT (increments facts.confirmed_count)
    5. UPDATE staged_facts SET promoted_at = now() (marks row as promoted)
    6. DELETE from Qdrant staged collection (cleanup, best-effort)
    7. Log confirmation to observability system

    Threshold Rationale (3 confirmations):
    • 1 occurrence: Could be typo, one-off phrasing, random utterance
    • 2 occurrences: Still vulnerable to coincidence or misunderstanding
    • 3 occurrences: Sweet spot — filters noise, captures recurring patterns
    • Higher threshold: Would miss important recurring facts, delay promotion

    Edge Cases & Safety:
    • Promotion is **non-cascading** — only the matching triple is promoted
    • Other facts about same entities unaffected (e.g., promoting "works_for" doesn't touch "spouse")
    • Per-user isolation maintained (no cross-user promotion, schema_name isolates via search_path)
    • Archived facts (superseded_at IS NOT NULL) excluded from promotion (WHERE clause)
    • Race-safe: PostgreSQL ACID guarantees atomicity between concurrent ingest calls

    Args:
        db_conn: PostgreSQL connection (per-user schema context via search_path)
        qdrant_url: Qdrant service URL for staged collection cleanup
        user_id: User UUID for collection naming (optional, derived from schema context)
        schema_name: User schema name (e.g., "faultline_alice"). If provided, sets search_path.
        promotion_threshold: Confirmed count threshold for promotion (default 3, configurable)

    Returns:
        Count of promoted facts successfully moved to facts table.

    Grounding Documents:
    • Self_Growth.md Section "Phase 2: Confirmation Tracking" (Mechanism #9, lines 1060-1100)
    • main.py _commit_staged() (lines 1017-1075, ON CONFLICT confirmation increment)
    • CLAUDE.md "Ingest Pipeline: Three-Stage Intent-Aware Pipeline" (staged facts lifecycle)
    • CLAUDE.md "Fact Classification, Storage & Retrieval" (Class A/B/C promotion flow)
    """
    promoted = 0
    try:
        # CRITICAL: Set search_path per-user schema if schema_name provided
        if schema_name:
            try:
                with db_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {schema_name}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema from UUID-derived source with validation
            except Exception as e:
                log.warning(f"re_embedder.search_path_setup_failed schema={schema_name}: {e}")
                # Continue with current search_path

        # C→B upgrade: Class C facts that have accumulated enough confirmations graduate
        # to Class B and enter the B→facts promotion pipeline in this same cycle.
        # expires_at extended so they survive long enough to reach the next threshold.
        try:
            with db_conn.cursor() as cur:
                cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                    """
                    UPDATE staged_facts
                    SET fact_class   = 'B',
                        expires_at   = GREATEST(expires_at, now() + interval '30 days'),
                        qdrant_synced = false
                    WHERE fact_class     = 'C'
                      AND confirmed_count >= %s
                      AND promoted_at IS NULL
                      AND expires_at   > now()
                    """,
                    (promotion_threshold,)
                )
                n_c_to_b = cur.rowcount
            db_conn.commit()
            if n_c_to_b:
                log.info(f"re_embedder.class_c_upgraded_to_b count={n_c_to_b} threshold={promotion_threshold}")
        except Exception as e:
            try:
                db_conn.rollback()
            except Exception:
                pass
            log.error(f"re_embedder.class_c_upgrade_failed: {e}")

        with db_conn.cursor() as cur:
            cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                """
                SELECT id, subject_id, object_id, rel_type,
                       provenance, COALESCE(fact_provenance, 'llm_inferred'), confidence,
                       temporal_status, event_date, event_date_granularity, source_ref,
                       COALESCE(is_hierarchy_rel, false)
                FROM staged_facts
                WHERE fact_class = 'B'
                  AND confirmed_count >= %s
                  AND promoted_at IS NULL
                """,
                (promotion_threshold,)
            )
            candidates = cur.fetchall()
        # READ BARRIER: the work set is materialised; end the transaction BEFORE the
        # per-row loop, which makes Qdrant calls on this same connection.
        release_read_transaction(db_conn, context="re_embedder.promote_staged_facts.fetch")

        for row in candidates:
            # READ BARRIER (per iteration): this loop body blocks on the brain/Qdrant, and a
            # read left open by the PREVIOUS iteration would ride across it. A batch-level
            # barrier alone does not cover this — measured live: climb_state and the
            # taxonomy reads were each caught idle-in-transaction at 58-59s inside a loop.
            release_read_transaction(db_conn, context="re_embedder.promote_staged_facts.iteration")
            sid, subject, obj, rel_type, prov, fact_prov, conf, temporal_status, event_date, event_date_granularity, source_ref, is_hierarchy_rel = row
            # user_id is implicit in per-user schema context (set by SET search_path)
            # ASYNC-WRITE GUARD: promotion is a COPY that never re-validates, so a hierarchy
            # self-loop already staged would land in `facts`. Reject it here (same flag/contract
            # as the gate) and TOMBSTONE the staged row so it is not re-examined every cycle. No
            # user content is lost — a self-loop names exactly ONE entity, already registered.
            if _reflexive_hierarchy_blocked(subject, obj, is_hierarchy_rel):
                log.warning("re_embedder.reflexive_hierarchy_promotion_rejected",
                            extra={"staged_id": sid, "rel_type": rel_type,
                                   "entity_id": str(subject)[:16]})
                try:
                    with db_conn.cursor() as cur:
                        cur.execute(
                            "UPDATE staged_facts SET promoted_at = now() WHERE id = %s", (sid,))
                    db_conn.commit()
                except Exception:  # noqa: BLE001 — fail-safe: leave the row, never break the loop
                    try:
                        db_conn.rollback()
                    except Exception:
                        pass
                continue
            try:
                log.info(
                    f"re_embedder.promoting_staged_fact staged_id={sid}"
                    f" subject_id={subject[:8] if subject else '?'}"
                    f" rel_type={rel_type} threshold={promotion_threshold}"
                )

                with db_conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO facts"
                        " (subject_id, object_id, rel_type, provenance,"
                        "  confidence, fact_class, fact_provenance, qdrant_synced,"
                        "  temporal_status, event_date, event_date_granularity, source_ref,"
                        # STRUCTURAL FLAG PASSTHROUGH: promote the staged row's own
                        # is_hierarchy_rel, set at ingest from the rel_types metadata (the
                        # SAME authoritative source store.py writes into facts). Without it
                        # the INSERT took the column DEFAULT false, so a promoted hierarchy
                        # rung (subclass_of/part_of/…) landed with is_hierarchy_rel=false —
                        # latent-wrong. Subject-agnostic: driven by the rel_type's metadata
                        # carried on the staged row, no rel-name literals. Structural flag,
                        # so ON CONFLICT keeps the existing row's value (mirrors store.py).
                        "  is_hierarchy_rel)"
                        " VALUES (%s, %s, %s, %s, %s, 'B', %s, false,"
                        "  COALESCE(%s, 'now'), %s, %s, %s, %s)"
                        " ON CONFLICT (subject_id, object_id, rel_type)"
                        " DO UPDATE SET"
                        "   confirmed_count = facts.confirmed_count + 1,"
                        "   last_seen_at    = now(),"
                        "   updated_at      = now(),"
                        # never-downgrade-to-NULL: a stamped staged row promotes its
                        # event_date INTO facts; an undated promotion never clobbers an
                        # already-stamped facts row back to NULL/'now' (COALESCE/keep).
                        "   temporal_status = CASE WHEN EXCLUDED.temporal_status = 'now'"
                        "                          THEN facts.temporal_status"
                        "                          ELSE EXCLUDED.temporal_status END,"
                        "   event_date = COALESCE(EXCLUDED.event_date, facts.event_date),"
                        "   event_date_granularity = COALESCE(EXCLUDED.event_date_granularity, facts.event_date_granularity),"
                        # CITABLE PROVENANCE (migration 128): promotion carries the staged
                        # citation into facts; a citation-less promotion never nulls one.
                        "   source_ref = COALESCE(EXCLUDED.source_ref, facts.source_ref)",
                        (subject, obj, rel_type, prov, conf, fact_prov,
                         temporal_status, event_date, event_date_granularity, source_ref,
                         is_hierarchy_rel)
                    )
                    cur.execute(
                        "UPDATE staged_facts SET promoted_at = now() WHERE id = %s",
                        (sid,)
                    )
                db_conn.commit()
                promoted += 1

                log.debug(
                    f"re_embedder.promoted_fact_committed staged_id={sid}"
                    f" subject_id={subject[:8] if subject else '?'}"
                    f" rel_type={rel_type} object_id={obj[:8] if obj else '?'}"
                )

                # Best-effort: delete staged Qdrant point after promotion commits
                try:
                    _qdrant_delete_fact_point(qdrant_url, user_id, "staged_facts", sid, timeout=5.0)
                except Exception as e:
                    log.warning(f"Failed to delete staged Qdrant point {sid} after promotion: {e}")

                log.info(
                    f"re_embedder.promoted fact staged_id={sid} "
                    f"subject={subject} rel_type={rel_type}"
                )
            except Exception as e:
                try:
                    db_conn.rollback()
                except Exception as rollback_err:
                    log.warning(f"re_embedder.promote_rollback_failed: {rollback_err}")
                log.error(f"re_embedder.promote_failed staged_id={sid}: {e}")

    except Exception as e:
        # Per-tenant isolation: a tenant with a missing/incomplete relation aborts the
        # Postgres transaction here. Roll back so the SHARED per-user connection is clean
        # for the next job (expire/class_c/...) — otherwise every subsequent statement in
        # this cycle fails with "current transaction is aborted". Fail-loud, never poison.
        try:
            db_conn.rollback()
        except Exception as rollback_err:
            log.warning(f"re_embedder.promote_staged_rollback_failed: {rollback_err}")
        log.error(f"re_embedder.promote_staged_error: {e}")

    return promoted


def expire_staged_facts(db_conn, qdrant_url: str, user_id: str = None) -> int:
    """
    Score-decay model for staged facts (C and B).

    STAGED-FACT LIFECYCLE — confirmed_count counter (one of TWO). Pairs with
    promote_staged_facts(). Operates on confirmed_count (re-ingest + scoped-query
    driven). Do NOT conflate with the hit_count lifecycle (decay_class_c_hits /
    promote_class_c_hits), which is recall-relevance driven. Both promote C→B at >= 3.

    Every 30-day window without a new confirmation:
      - confirmed_count > 0  → decrement by 1, reset window (+30 days)
      - confirmed_count <= 0 → delete (score hit zero, no evidence to keep)

    This means a fact must be re-observed every ~30 days per point of confidence
    it has accumulated, or it decays back to zero and is removed.

    Per-user schema context: user_id is passed as parameter (schema provides isolation).
    Returns count of facts removed (decayed to zero and deleted).
    """
    expired = 0
    try:
        # Step 1: Decay — Class C facts past their window with remaining score.
        # Class B is long-term memory; once a fact earns B it does not decay.
        # Only Class C (short-term/speculative) participates in the decay cycle.
        with db_conn.cursor() as cur:
            cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                """
                UPDATE staged_facts
                SET confirmed_count = confirmed_count - 1,
                    expires_at      = now() + interval '30 days',
                    qdrant_synced   = false
                WHERE fact_class    = 'C'
                  AND expires_at   <= now()
                  AND confirmed_count > 0
                  AND promoted_at IS NULL
                """
            )
            n_decayed = cur.rowcount
        db_conn.commit()
        if n_decayed:
            log.info(f"re_embedder.staged_facts_decayed count={n_decayed} user_id={user_id}")

        # Step 2: Remove — Class C facts at score zero AND past their expiry window.
        # Fresh rows start at confirmed_count=0 but have expires_at = now()+30d — keep them.
        # Only delete when the window has expired AND score is zero (never confirmed or fully decayed).
        # Class B facts are never removed here; retraction handles their lifecycle.
        with db_conn.cursor() as cur:
            cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                """
                SELECT id FROM staged_facts
                WHERE fact_class    = 'C'
                  AND confirmed_count <= 0
                  AND expires_at   <= now()
                  AND promoted_at IS NULL
                """
            )
            stale = cur.fetchall()
        # READ BARRIER: the very next statement is a Qdrant delete with a 10s timeout,
        # once per stale row. Holding this SELECT's transaction across them is the wound.
        release_read_transaction(db_conn, context="re_embedder.expire_staged_facts.fetch")

        for (staged_id,) in stale:
            # READ BARRIER (per iteration): this loop body blocks on the brain/Qdrant, and a
            # read left open by the PREVIOUS iteration would ride across it. A batch-level
            # barrier alone does not cover this — measured live: climb_state and the
            # taxonomy reads were each caught idle-in-transaction at 58-59s inside a loop.
            release_read_transaction(db_conn, context="re_embedder.expire_staged_facts.iteration")
            try:
                _qdrant_delete_fact_point(qdrant_url, user_id, "staged_facts", staged_id, timeout=10.0)
            except Exception:
                pass  # Best effort Qdrant cleanup

            try:
                with db_conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM staged_facts WHERE id = %s",
                        (staged_id,)
                    )
                db_conn.commit()
                expired += 1
                log.info(f"re_embedder.removed staged_id={staged_id} reason=score_zero user_id={user_id}")
            except Exception as e:
                db_conn.rollback()
                log.error(f"re_embedder.expire_failed staged_id={staged_id}: {e}")

    except Exception as e:
        # Per-tenant isolation: roll back the aborted transaction so the shared per-user
        # connection stays clean for the next job in this cycle (see promote_staged_facts).
        try:
            db_conn.rollback()
        except Exception as rollback_err:
            log.warning(f"re_embedder.expire_staged_rollback_failed: {rollback_err}")
        log.error(f"re_embedder.expire_staged_error: {e}")

    return expired


def decay_class_c_hits(db_conn, qdrant_url: str, user_id: str = None, limit: int = 100) -> dict:
    """
    JOB 1 — Class C HIT-LIFECYCLE DECAY sweep (Part B state machine, Part D4).

    Distinct from expire_staged_facts(): that decays the ingest-side `confirmed_count`.
    THIS decays the query-side `hit_count` — the query-hit counter incremented by the
    query path (other agent) on a genuine scoped relevance match. hit_count and
    confirmed_count are NEVER conflated.

    State-machine rule (IDLE 30d, no hit in the window):
        hit_count = hit_count - 1
        expires_at = now() + 30d      # decrement buys another 30-day window
        if hit_count <= 0:  DROP (delete row + best-effort Qdrant point)

    A hit (other agent) pushes expires_at forward, so any row whose expires_at <= now()
    has gone a full window with no hit and is owed a decrement.

    Bounded with LIMIT per cycle so the poll loop stays responsive.
    Returns {"decremented": int, "dropped": int}.
    Per-user schema context: user_id is passed as parameter (schema provides isolation).
    """
    stats = {"decremented": 0, "dropped": 0}
    try:
        # Step 1: Select the idle Class C rows (window elapsed, no hit), bounded.
        # A row with hit_count <= 1 will hit zero on this decrement and must be dropped
        # (need its id + qdrant point), so we select all eligible and branch per-row.
        with db_conn.cursor() as cur:
            cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                """
                SELECT id, hit_count
                FROM staged_facts
                WHERE fact_class  = 'C'
                  AND expires_at <= now()
                  AND promoted_at IS NULL
                ORDER BY expires_at ASC
                LIMIT %s
                """,
                (limit,)
            )
            idle_rows = cur.fetchall()
        # READ BARRIER: same shape as expire_staged_facts — a 10s Qdrant delete per row.
        release_read_transaction(db_conn, context="re_embedder.decay_class_c_hits.fetch")

        if not idle_rows:
            return stats

        for staged_id, hit_count in idle_rows:
            # READ BARRIER (per iteration): this loop body blocks on the brain/Qdrant, and a
            # read left open by the PREVIOUS iteration would ride across it. A batch-level
            # barrier alone does not cover this — measured live: climb_state and the
            # taxonomy reads were each caught idle-in-transaction at 58-59s inside a loop.
            release_read_transaction(db_conn, context="re_embedder.decay_class_c_hits.iteration")
            try:
                new_hits = (hit_count if hit_count is not None else 1) - 1
                if new_hits <= 0:
                    # DROP — best-effort Qdrant delete first (match expiry pattern), then row.
                    try:
                        _qdrant_delete_fact_point(qdrant_url, user_id, "staged_facts", staged_id, timeout=10.0)
                    except Exception:
                        pass  # Best-effort Qdrant cleanup
                    with db_conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM staged_facts WHERE id = %s",
                            (staged_id,)
                        )
                    db_conn.commit()
                    stats["dropped"] += 1
                    log.info(
                        f"re_embedder.class_c_dropped staged_id={staged_id} "
                        f"reason=hit_count_zero user_id={user_id}"
                    )
                else:
                    # Decrement hit_count, reset the 30-day window.
                    with db_conn.cursor() as cur:
                        cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                            """
                            UPDATE staged_facts
                            SET hit_count   = %s,
                                expires_at  = now() + interval '30 days'
                            WHERE id = %s
                            """,
                            (new_hits, staged_id)
                        )
                    db_conn.commit()
                    stats["decremented"] += 1
            except Exception as e:
                try:
                    db_conn.rollback()
                except Exception:
                    pass
                log.error(f"re_embedder.class_c_decay_failed staged_id={staged_id}: {e}")

        if stats["decremented"] or stats["dropped"]:
            log.info(
                f"re_embedder.class_c_decay_complete "
                f"decremented={stats['decremented']} dropped={stats['dropped']} user_id={user_id}"
            )

    except Exception as e:
        # Per-tenant isolation: roll back the aborted transaction so the shared per-user
        # connection stays clean for the next job in this cycle (see promote_staged_facts).
        try:
            db_conn.rollback()
        except Exception as rollback_err:
            log.warning(f"re_embedder.class_c_decay_rollback_failed: {rollback_err}")
        log.error(f"re_embedder.class_c_decay_error: {e}")

    return stats


def promote_class_c_hits(db_conn, qdrant_url: str, qwen_api_url: str, user_id: str = None,
                         schema_name: str = None, hit_threshold: int = 3, limit: int = 50) -> int:
    """
    JOB 2 — Class C HIT-LIFECYCLE PROMOTION (Part B state machine, Part D4, default B-2).

    When a Class C row reaches hit_count >= 3 (earned via genuine query-scoped hits, NOT
    ingest confirmations), promote it to Class B and route it into the facts table using
    the SAME mechanism as promote_staged_facts() (INSERT ... ON CONFLICT, set promoted_at,
    enqueue Qdrant re-sync, best-effort delete the staged Qdrant point).

    Default B-2 — an UNCLASSIFIED Class C row (rel_type IS NULL, rough memory) is FIRST
    classified at this moment via the existing LLM metadata path
    (_query_llm_for_rel_type_metadata) to derive a structured rel_type + metadata from the
    fact's stored text/context. If classification fails we leave it as Class C and skip
    promotion (do not promote a thing we cannot structure). An already-classified Class C
    just flips to B.

    Bounded with LIMIT per cycle. Fails loud per-row, continues the loop.
    Returns count of rows promoted to facts.
    Per-user schema context: user_id is passed as parameter (schema provides isolation).
    """
    promoted = 0
    try:
        # Optional per-user search_path (mirror promote_staged_facts house style).
        if schema_name:
            try:
                with db_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {schema_name}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema from UUID-derived source with validation
                db_conn.commit()
            except Exception as e:
                log.warning(f"re_embedder.class_c_promote_search_path_failed schema={schema_name}: {e}")
                # Continue with current search_path

        with db_conn.cursor() as cur:
            cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                """
                SELECT id, subject_id, object_id, rel_type, provenance,
                       COALESCE(fact_provenance, 'llm_inferred'),
                       confidence, hit_count, rel_type_definition,
                       temporal_status, event_date, event_date_granularity, source_ref,
                       COALESCE(is_hierarchy_rel, false)
                FROM staged_facts
                WHERE fact_class = 'C'
                  AND hit_count >= %s
                  AND promoted_at IS NULL
                ORDER BY hit_count DESC
                LIMIT %s
                """,
                (hit_threshold, limit)
            )
            candidates = cur.fetchall()
        # READ BARRIER: the loop below calls the tenant brain
        # (_query_llm_for_rel_type_metadata) per candidate.
        release_read_transaction(db_conn, context="re_embedder.promote_class_c_hits.fetch")

        if not candidates:
            return promoted

        for row in candidates:
            # READ BARRIER (per iteration): this loop body blocks on the brain/Qdrant, and a
            # read left open by the PREVIOUS iteration would ride across it. A batch-level
            # barrier alone does not cover this — measured live: climb_state and the
            # taxonomy reads were each caught idle-in-transaction at 58-59s inside a loop.
            release_read_transaction(db_conn, context="re_embedder.promote_class_c_hits.iteration")
            sid, subject, obj, rel_type, prov, fact_prov, conf, hits, rel_def, temporal_status, event_date, event_date_granularity, source_ref, is_hierarchy_rel = row
            # ASYNC-WRITE GUARD (see promote_staged_facts): a C→B hit promotion is also a COPY
            # that never re-validates — reject a hierarchy self-loop and tombstone the staged row.
            if _reflexive_hierarchy_blocked(subject, obj, is_hierarchy_rel):
                log.warning("re_embedder.reflexive_hierarchy_promotion_rejected",
                            extra={"staged_id": sid, "rel_type": rel_type,
                                   "entity_id": str(subject)[:16]})
                try:
                    with db_conn.cursor() as cur:
                        cur.execute(
                            "UPDATE staged_facts SET promoted_at = now() WHERE id = %s", (sid,))
                    db_conn.commit()
                except Exception:  # noqa: BLE001 — fail-safe: leave the row, never break the loop
                    try:
                        db_conn.rollback()
                    except Exception:
                        pass
                continue
            required_classification = False
            try:
                # ── Default B-2: classify rough/unclassified memory before promotion ──
                if not rel_type:
                    required_classification = True
                    # Build a candidate rel_type + snippet from stored text/context so the
                    # existing LLM metadata path can derive a structured rel_type. We reuse
                    # the same helper the ontology evaluator uses — no divergent path.
                    resolved = resolve_display_names_for_facts(db_conn, [{
                        "subject_id": subject, "object_id": obj,
                    }])[0]
                    subj_disp = resolved.get("subject_display", subject)
                    obj_disp = resolved.get("object_display", obj)
                    snippet = (rel_def or prov or f"{subj_disp} {obj_disp}").strip()
                    candidate_rel = "related_to"  # rough seed; LLM infers the real rel_type metadata
                    # READ BARRIER (immediately before the blocking call — the RE-ARM case). A barrier at
                    # the top of the enclosing block is NOT enough: a per-row read helper opens a FRESH
                    # transaction after it, and that read then rides across this hop. Measured live on the
                    # deployed image — climb_classification_chains was killed twice this way (02:42:25 and
                    # 02:45:06), its whole _ont_db subsystem chain failing 'connection already closed' four
                    # seconds later.
                    release_read_transaction(db_conn, context="re_embedder.promote_class_c_hits.pre_blocking_call")
                    llm_md = _query_llm_for_rel_type_metadata(
                        candidate_rel, "unknown", "unknown", snippet, qwen_api_url
                    )
                    if not llm_md or not llm_md.get("llm_natural_language"):
                        # FAIL LOUD: cannot structure → leave as Class C, do not promote.
                        log.warning(
                            f"re_embedder.class_c_promote_classify_failed staged_id={sid} "
                            f"hit_count={hits} reason=no_llm_metadata — left as Class C"
                        )
                        continue

                    # Register the inferred rel_type into rel_types so the structured fact has
                    # a real ontology entry (mirrors evaluate_ontology_candidates INSERT shape).
                    natural_language = llm_md.get("llm_natural_language", "")
                    natural_language_2p = llm_md.get("llm_natural_language_2p") or None
                    is_symmetric = llm_md.get("llm_is_symmetric", False)
                    inverse_rel_type = llm_md.get("llm_inverse_rel_type")
                    category = llm_md.get("llm_category", "other")
                    head_types = llm_md.get("llm_head_types") or ["ANY"]
                    tail_types = llm_md.get("llm_tail_types") or ["ANY"]
                    # AUTHORITY GUARD (user > SEED > growth): candidate_rel here is the SEED
                    # 'related_to'. Pin the structural fields to the public seed so the
                    # unconditional `category = EXCLUDED.category` (and the is_symmetric/inverse
                    # writes) below can NEVER re-classify the seed — they converge it back to the
                    # authoritative definition instead. head_types stays LLM-inferred (backfill
                    # only). See _seed_structural_flags.
                    _seed_pin = _seed_structural_flags(db_conn, candidate_rel)
                    if _seed_pin is not None:
                        category = _seed_pin["category"]
                        is_symmetric = _seed_pin["is_symmetric"]
                        inverse_rel_type = _seed_pin["inverse_rel_type"]
                        if _seed_pin["tail_types"]:
                            tail_types = _seed_pin["tail_types"]
                    label = candidate_rel.replace('_', ' ').title()
                    with db_conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO rel_types"
                            " (rel_type, label, natural_language, natural_language_2p, engine_generated, confidence, source,"
                            "  head_types, tail_types, is_hierarchy_rel, is_symmetric, inverse_rel_type, category, fact_class)"
                            " VALUES (%s, %s, %s, %s, true, %s, 'engine', %s, %s, false, %s, %s, %s, 'B')"
                            " ON CONFLICT (rel_type) DO UPDATE SET"
                            # FIX #2: COALESCE — a NULL/blank generated value never nukes a
                            # good existing template (candidate_rel here is 'related_to', a seed).
                            "  natural_language = COALESCE(NULLIF(btrim(EXCLUDED.natural_language), ''), rel_types.natural_language),"
                            "  natural_language_2p = COALESCE(EXCLUDED.natural_language_2p, rel_types.natural_language_2p),"
                            "  category = EXCLUDED.category,"
                            "  head_types = CASE WHEN (rel_types.head_types IS NULL"
                            "                          OR rel_types.head_types = ARRAY[]::TEXT[])"
                            "                    THEN EXCLUDED.head_types ELSE rel_types.head_types END,"
                            "  tail_types = CASE WHEN (rel_types.tail_types IS NULL"
                            "                          OR rel_types.tail_types = ARRAY[]::TEXT[])"
                            "                    THEN EXCLUDED.tail_types ELSE rel_types.tail_types END",
                            (candidate_rel, label, natural_language, natural_language_2p, 0.8, head_types, tail_types,
                             is_symmetric, inverse_rel_type, category),
                        )
                    rel_type = candidate_rel

                # ── Promote C → B via the SAME mechanism as promote_staged_facts() ──
                promote_conf = max(conf if conf is not None else 0.0, 0.6)  # ensure >= 0.6 (Class B floor)
                with db_conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO facts"
                        " (subject_id, object_id, rel_type, provenance,"
                        "  confidence, fact_class, fact_provenance, qdrant_synced,"
                        "  temporal_status, event_date, event_date_granularity, source_ref,"
                        # STRUCTURAL FLAG PASSTHROUGH (mirrors promote_staged_facts): promote
                        # the staged row's own is_hierarchy_rel — set at ingest from the
                        # rel_types metadata, the SAME authoritative source store.py writes
                        # into facts — instead of taking the column DEFAULT false. A promoted
                        # hierarchy rung must land is_hierarchy_rel=true, a relational one
                        # false. (In the rel-typed-here branch the rel_type is minted as the
                        # non-hierarchy seed 'related_to', so the stored false flag stays
                        # correct.) Subject-agnostic; ON CONFLICT keeps the existing value.
                        "  is_hierarchy_rel)"
                        " VALUES (%s, %s, %s, %s, %s, 'B', %s, false,"
                        "  COALESCE(%s, 'now'), %s, %s, %s, %s)"
                        " ON CONFLICT (subject_id, object_id, rel_type)"
                        " DO UPDATE SET"
                        "   confirmed_count = facts.confirmed_count + 1,"
                        "   last_seen_at    = now(),"
                        "   updated_at      = now(),"
                        # never-downgrade-to-NULL: carry the stamped event_date into facts;
                        # an undated promotion never clobbers a stamped facts row to NULL/'now'.
                        "   temporal_status = CASE WHEN EXCLUDED.temporal_status = 'now'"
                        "                          THEN facts.temporal_status"
                        "                          ELSE EXCLUDED.temporal_status END,"
                        "   event_date = COALESCE(EXCLUDED.event_date, facts.event_date),"
                        "   event_date_granularity = COALESCE(EXCLUDED.event_date_granularity, facts.event_date_granularity),"
                        # CITABLE PROVENANCE (migration 128): promotion carries the staged
                        # citation into facts; a citation-less promotion never nulls one.
                        "   source_ref = COALESCE(EXCLUDED.source_ref, facts.source_ref)",
                        (subject, obj, rel_type, prov, promote_conf, fact_prov,
                         temporal_status, event_date, event_date_granularity, source_ref,
                         is_hierarchy_rel)
                    )
                    cur.execute(
                        "UPDATE staged_facts SET fact_class = 'B', promoted_at = now() WHERE id = %s",
                        (sid,)
                    )
                db_conn.commit()
                promoted += 1

                # Best-effort: delete staged Qdrant point after promotion commits
                # (match promote_staged_facts cleanup pattern). New facts-table point is
                # re-synced next cycle via qdrant_synced=false above.
                try:
                    _qdrant_delete_fact_point(qdrant_url, user_id, "staged_facts", sid, timeout=5.0)
                except Exception as e:
                    log.warning(f"re_embedder.class_c_promote_qdrant_delete_failed staged_id={sid}: {e}")

                log.info(
                    f"re_embedder.class_c_promoted staged_id={sid} rel_type={rel_type} "
                    f"hit_count={hits} required_classification={required_classification} user_id={user_id}"
                )
            except Exception as e:
                try:
                    db_conn.rollback()
                except Exception as rollback_err:
                    log.warning(f"re_embedder.class_c_promote_rollback_failed: {rollback_err}")
                log.error(f"re_embedder.class_c_promote_failed staged_id={sid}: {e}")

    except Exception as e:
        # Per-tenant isolation: roll back the aborted transaction so the shared per-user
        # connection stays clean for the next job in this cycle (see promote_staged_facts).
        try:
            db_conn.rollback()
        except Exception as rollback_err:
            log.warning(f"re_embedder.class_c_promote_rollback_failed: {rollback_err}")
        log.error(f"re_embedder.class_c_promote_error: {e}")

    return promoted


# Schemas already logged as missing episodic_log (pre-migration-127) — log ONCE per
# schema per process, then silently skip (the table appears after the migration runs).
_episodic_log_missing_schemas: set = set()


class RateDeferred(RuntimeError):
    """This pass never asked the model — the rate gate deferred it.

    Distinct from every other failure BECAUSE THE POISON-ROW GUARD must not stamp
    it: a rate-deferred row has not had its best-effort pass (the model was never
    consulted), so stamping reextracted_at would terminally record "uncastable"
    from a call that did not happen. The guard in reextract_episodic exempts this
    exception explicitly; anything else keeps today's semantics.
    """


# === FLAG: REEXTRACT_PRESERVE_PROVENANCE (default ON) ========================
#
# THE DEFECT IT FIXES. `reextract_episodic` re-mines a retained turn and re-ingests it
# with `source="reextract"`, which falls through the /ingest provenance router's
# else-branch to `fact_provenance="llm_inferred"` (`main.py` — `llm_learn` → llm_learned,
# `mcp`/`assistant` → user_stated, ELSE → llm_inferred). `_reextract_row_edges`
# additionally FORCED llm_inferred onto every edge. So a fact the USER STATED came back
# DEMOTED to Class B/C instead of the Class A it earned the first time.
#
# OWNER RULING (binding, a founding principle): "Re-stated should be able to go from B→A.
# User is truth should be respected." A fact does not become less true because OUR
# pipeline had to read it twice.
#
# ⛔ THIS OVERRULES the note under REEXTRACT_BACKLOG_DRAIN below ("`source='reextract'` →
# llm_inferred stays exactly as it is … a re-derivation lacks the live turn's context, so
# it genuinely IS inference"). That reasoning CONFLATED EXTRACTION QUALITY WITH
# PROVENANCE, and that conflation is the whole bug. A weaker re-derivation may produce a
# WORSE READING of the user's words — that is a confidence/extraction concern, already
# handled by the low-confidence filter and `_assess_statement_directness`. It does not
# change WHO SAID IT. The text in `episodic_log` is the user's verbatim turn either way;
# re-reading our own record of their words is not the engine inventing something.
#
# WHY PROVENANCE-CORRECT RE-INGEST AND *NOT* A B→A PROMOTION JOB. Promotion today is a
# C-tier mechanism (C→B at confirmed_count >= 3) and `promote_staged_facts` writes
# fact_class='B'; A/B never promote. A genuine B→A promotion keyed on REPETITION would let
# the engine promote its OWN inferred content to sacred simply by re-deriving it — the
# exact inversion the tier model exists to prevent, and catastrophic. Repetition is not
# testimony. So there is NO new promotion path here and no new authority mechanism: the
# fact was never legitimately B, it was an A misfiled as B by a router that had LOST the
# turn's origin. Restoring the origin makes it land through the SAME
# `assign_class_and_confidence` the live path uses — Class A when the rel's defined class
# is A, else Class B, exactly as first time. B→A then happens as a CONSEQUENCE of the
# corrected write, not as a rule anything can game (and the staged upsert's provenance
# ladder is already upgrade-only: `main.py` — only an incoming user_stated may raise the
# stored value).
#
# THE SAFETY PROPERTY — PRESERVING, NEVER ELEVATING.
# We re-ingest under the ORIGINAL INGEST SOURCE, so /ingest's own router does the mapping
# and there is no second copy of the provenance rules to drift. A row whose origin lane is
# NOT known to have been user-stated keeps the legacy `source="reextract"` → llm_inferred
# path. Unknown origin is NEVER guessed: guessing would FABRICATE AUTHORITY, which is the
# precise opposite of user-is-truth. Fail-safe direction is "stay at the lower tier".
#
# WHAT `episodic_log.source` ACTUALLY RECORDS (verified against every writer, 2026-08-01):
#   'mcp'                     — `_episodic_capture` (`src/mcp/server.py`), the chat turn as
#                               handed to remember_facts. The LIVE path ingests this same
#                               text with source="mcp" → user_stated. PRESERVING.
#   'document'                — the document lane (`src/mcp/server.py`, and
#                               `_process_document_chunk` below). Its LIVE ingest is
#                               `_extract_and_ingest(chunk, "document")` (owner ruling
#                               2026-08-21: machine-extracted document facts tier at
#                               staged B) and the router maps it to llm_inferred —
#                               re-mining the chunk under "document" reproduces the
#                               live class outcome. TIER-PRESERVING (was "mcp"/PRESERVING
#                               before the ruling).
#   'store_context_deferred'  — `_defer_context_to_episodic` (`main.py`): Class-C residue
#                               that never produced a typed triple. NOT elevatable.
#   NULL / anything else      — origin unknown. NOT elevatable.
# Measured on the local stack: 1678 rows across 79 tenant schemas, all source='mcp', no
# NULLs. The signal is present and populated — no new column is needed, and adding one
# would create a SECOND source of truth for the same fact (this project has already paid
# for that once with three drifting copies of the tool descriptions).
#
# ⚠️ CONTRACT FOR FUTURE WRITERS: `episodic_log.source` is now LOAD-BEARING for provenance.
# A new writer MUST set it honestly. Note the footgun: `EpisodicAppendRequest.source`
# defaults to "mcp" (`src/api/models.py`), so a caller that simply OMITS the field is
# recorded as a user turn. All three current writers set it explicitly. Migration 206
# records this contract as a COMMENT ON COLUMN.
#
# FLAG OFF → byte-for-byte the legacy behaviour (source="reextract" + forced llm_inferred
# on every edge, for every row). Pinned by test.
REEXTRACT_PRESERVE_PROVENANCE = os.getenv(
    "REEXTRACT_PRESERVE_PROVENANCE", "true").strip().lower() in ("true", "1", "yes", "on")

# episodic_log.source  →  the /ingest `source` to re-mine it under.
# ONLY origin lanes whose LIVE ingest provably routed to user_stated appear here; the
# value is an EXISTING live source string (never a new one) so every downstream seam
# keyed on it — the provenance router, the user-is-truth retype exemption, the
# commit() provenance derivation — behaves exactly as it did on the live path.
# Absent from this map == not elevatable == legacy "reextract" lane.
#
# DOWN-ROUTING ENTRIES (authorship-honesty, 2026-08-15) — unlike the preserving entries
# above, these route an origin that was NEVER attested to the explicit "unattested"
# ingest lane, where the /ingest provenance router forces llm_inferred and the class
# force lands staged Class B (never A, never the C 30-day clock):
#   • "store_context_deferred" — the store_context lane's residue/verbatim (the
#     dispatchable store_context TOOL and the internal remainder captures). Its text
#     never produced an attested triple; without this entry the legacy re-mine lane
#     could land a defined-A rel at facts Class A off a machine side-effect.
#   • "unattested" — a recall divert's verbatim, captured by _episodic_capture with
#     source="unattested" (attested=False). Without this entry the re-mine would take
#     the legacy lane and could re-elevate the demoted capture.
_EPISODIC_ORIGIN_INGEST_SOURCE: dict = {
    "mcp": "mcp",
    # DOCUMENT re-mines reproduce the document tier (owner direction 2026-08-21): a re-mined
    # document chunk re-ingests under source="document" → staged B, exactly as the fresh
    # drain would. The previous "mcp" mapping re-elevated re-mined document facts to
    # user_stated/Class A — the exact defect class the unattested down-routes below close.
    "document": "document",
    "store_context_deferred": "unattested",
    "unattested": "unattested",
}


def _reextract_ingest_source(episodic_source) -> str | None:
    """The /ingest `source` that PRESERVES this retained turn's original provenance.

    Returns None when the origin lane is unknown or is not known to have been
    user-stated — the caller then uses the legacy `source="reextract"` lane and keeps
    forcing llm_inferred. Never guesses: an unknown origin stays at the lower tier.
    """
    if not REEXTRACT_PRESERVE_PROVENANCE:
        return None
    if not isinstance(episodic_source, str):
        return None
    return _EPISODIC_ORIGIN_INGEST_SOURCE.get(episodic_source.strip().lower())


def _reextract_row_edges(raw_text: str, user_id: str, backend_url: str,
                         statement_route: str,
                         preserve_source: str | None = None) -> list:
    """Extract edges for ONE episodic_log row through the normal front door.

    Mirrors the document lane's route-once-then-per-row pattern (server.py
    _ingest_statement_via_spine): when the backend brain routed "spine", try the
    deterministic spine extractor (/harvest-spans) FIRST and fall back to the
    legacy LLM extractor (/extract/rewrite) when the spine yields nothing;
    route "rewrite" (default / brain unreachable) goes straight to /extract/rewrite
    — byte-identical to the plain backfill. Low-confidence edges are dropped
    (same filter the MCP applies before /ingest).

    PROVENANCE HARD RULE (legacy lane, `preserve_source is None`): every returned edge
    has fact_provenance FORCED to 'llm_inferred'. Spine edges arrive stamped user_stated
    and the /ingest provenance router PRESERVES a canonical fact_provenance carried on
    the edge, so `source="reextract"` alone is not enough to hold a row down — without
    this override a backfilled spine edge would sneak in at user_stated → Class A. This
    force is what makes "not elevatable" actually mean it, and it stays exactly as-is for
    every row whose origin lane is unknown or non-user-stated.

    PROVENANCE PRESERVATION (`preserve_source` set — REEXTRACT_PRESERVE_PROVENANCE):
    the row's origin lane IS known to have been user-stated, so the edges are left
    UNSTAMPED and the caller re-ingests under that original source; /ingest's own
    provenance router then assigns exactly what it assigned the first time. We do not
    stamp user_stated ourselves — the router owns that decision, here as on the live
    path, so there is no second copy of the rule. See the flag block above.
    """
    edges: list = []
    # The LANE crosses HTTP with us: this process is BACKGROUND (declared at startup), and
    # the extraction runs in the API process — without the header it would arrive
    # INTERACTIVE and fail open under pressure instead of deferring. The CURRENT lane (not a
    # constant) so user-driven work wrapped in llm_lane.use_lane(interactive) keeps its lane.
    _lane_headers = {llm_lane.LANE_HEADER: llm_lane.current_lane()}
    if statement_route == "spine":
        try:
            resp = httpx.post(
                f"{backend_url}/harvest-spans",
                json={"text": raw_text, "user_id": user_id},
                headers={**_lane_headers, **_backend_auth_headers()},
                # NOT a bare literal. Prod diagnosis 2026-07-31: the reextract path is the lane
                # that actually times out (183 stall minutes overlapped 98.4% with
                # `reextract_*: timed out`), and it still carried 60.0 while the DOC lane was
                # raised to 180. Same env knob so both move together.
                timeout=_DOC_INGEST_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            _data = resp.json()
            # Split-brain guard: the embedder's own INGEST_ENABLED said "enabled" but
            # the BACKEND is frozen (separate containers, separate env). A gated
            # extraction returns zero edges — stamping the row as "uncastable" would
            # be a silent loss. Raise → the row stays NULL and retries next cycle.
            if _data.get("status") == "ingest_disabled":
                raise RuntimeError("backend ingest disabled (knowledge-store mode)")
            # Rate-deferral guard (the rewrite twin below has the same): this pass
            # did NOT ask the model. Falling through to /extract/rewrite would
            # burn more rate budget on a second call the same pass; and treating
            # the zero/partial edges as "spine found nothing" would be a verdict
            # from a call that never happened. Raise RateDeferred → the row stays
            # NULL and retries next cycle; the poison-row guard exempts it.
            if _data.get("rate_deferred"):
                raise RateDeferred("spine harvest rate-deferred")
            # Same class of silent loss, different cause: with NO tenant brain bound the
            # backend fails closed and answers HTTP 200 with zero edges. That reads as
            # "successfully uncastable" and stamps reextracted_at, and since eligibility
            # is `reextracted_at IS NULL` the turn is frozen out FOREVER. Raise → the row
            # stays NULL and is re-mined on the first cycle after an endpoint is bound.
            if _data.get("extraction_degraded") or _data.get("status") == "degraded":
                # Same reason-carry as the rewrite route (critic pass 5 hardening): the
                # pacer's defer token must survive the envelope or the poison guard
                # upstream cannot tell a paced day from a genuine outage.
                _swhy = (_data.get("error")
                         or _data.get("llm_first_unanswered_reason")
                         or "unknown")
                raise RuntimeError("extraction degraded (brain unavailable) — "
                                   f"{_swhy}")
            # POSITIVE-SUCCESS GATE for the spine seam (the re-mine truth rule). The
            # named guards above were each added AFTER a specific 200-shaped failure
            # lied in production; a failure shape they do not name would slip through
            # the same way — its edges (or its empty edge list) trusted as a real
            # harvest. /harvest-spans success bodies carry NO status key at all (unlike
            # /extract/rewrite, whose success says status="success"), so the positive
            # set here is "no status", plus the explicit success tokens in case the
            # endpoint ever grows one. An UNRECOGNIZED status is not trusted: the
            # harvest result is discarded and the row falls through to /extract/rewrite,
            # whose own positive gate decides the row's fate — a stamp can never rest on
            # a spine body that did not affirmatively succeed.
            if _data.get("status") not in (None, "", "success", "ok"):
                log.warning(f"re_embedder.reextract_spine_unrecognized_status "
                            f"user_id={user_id[:8]} "
                            f"status={_data.get('status')!r} "
                            f"(edges discarded, falling back to /extract/rewrite)")
            else:
                edges = [e for e in (_data.get("edges", []) or [])
                         if not e.get("low_confidence", False)]
        except RuntimeError:
            raise
        except Exception as e:
            log.warning(f"re_embedder.reextract_spine_failed user_id={user_id[:8]} "
                        f"(falling back to /extract/rewrite): {e}")
            edges = []
    if not edges:
        # Spine yielded nothing (or route == "rewrite") → the legacy LLM extractor.
        resp = httpx.post(
            f"{backend_url}/extract/rewrite",
            json={"text": raw_text, "user_id": user_id},
            headers={**_lane_headers, **_backend_auth_headers()},
            timeout=_DOC_INGEST_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        _data = resp.json()
        # Same split-brain guard as the spine branch above.
        if _data.get("status") == "ingest_disabled":
            raise RuntimeError("backend ingest disabled (knowledge-store mode)")
        # Rate-deferral guard: the backend's rate gate deferred some/all chunk
        # calls — this pass did NOT ask the model. Zero or partial edges from a
        # deferred pass are not a verdict; raising leaves reextracted_at NULL so
        # the row retries next cycle instead of being stamped "uncastable".
        if _data.get("rate_deferred"):
            raise RateDeferred("extraction rate-deferred (no capacity this pass)")
        # POSITIVE-SUCCESS GATE (the re-mine truth rule). /extract/rewrite answers
        # SEVERAL failures as HTTP 200 with a status-bearing body: ingest_disabled
        # (frozen store), degraded + extraction_degraded (no brain / every chunk
        # failed) — and at least two more the old named guards never knew:
        # status="error" (the handler's broad except) and status="processing"
        # (idempotency lock held by another request — that body has NO edges key at
        # all, so it read as "zero edges = successfully uncastable" and STAMPED the
        # row). Each guard below was added one name at a time after a specific
        # failure shape lied; a shape that does not exist yet would lie the same way.
        # So this gate is the POSITIVE set: only a body that affirmatively says
        # status="success" (the ONLY status /extract/rewrite returns on success,
        # including idempotency cache hits, which cache success only) may yield
        # edges. Anything else raises → the row stays NULL → re-mined next cycle.
        # raise_for_status() cannot help: none of these failures is an HTTP error.
        if _data.get("status") != "success":
            # Carry the REASON (critic pass 4, preserved): the pacer's defer envelope
            # is {"error": "rate_deferred"} — losing it here made a merely-paced day
            # read as "unknown" upstream, where the poison-row guard stamped user
            # turns terminally skipped for a request that never reached the provider.
            _why = (_data.get("error")
                    or _data.get("llm_first_unanswered_reason")
                    or f"status={_data.get('status')!r}")
            raise RuntimeError(f"extraction did not confirm success ({_why})")
        edges = [e for e in (_data.get("edges", []) or [])
                 if not e.get("low_confidence", False)]
    if preserve_source is None:
        # Legacy / not-elevatable lane — hold every edge down to llm_inferred.
        for e in edges:
            e["fact_provenance"] = "llm_inferred"
    return edges


# === FLAG: REEXTRACT_BACKLOG_DRAIN (CTIER increment 1, default OFF) ===========
#
# WHAT IT GATES. Three knobs on the episodic re-extraction backfill, ALL inert when the
# flag is OFF (flag-OFF is byte-for-byte the shipped behaviour: LIMIT REEXTRACT_BATCH_SIZE,
# a literal `INTERVAL '1 hour'` age gate, and no early batch abort):
#   1. AGE GATE          — `REEXTRACT_MIN_AGE_MINUTES` replaces the hardcoded 1 hour.
#   2. BACKLOG-PROPORTIONAL BATCH — a tenant that is BEHIND drains faster, bounded hard by
#      `REEXTRACT_MAX_BATCH`; a tenant that is caught up still drains at the base size.
#   3. CONSECUTIVE-FAILURE ABORT — the storm bound (see below), `REEXTRACT_FAIL_ABORT`.
#
# WHY THE RATE MATTERS (measured 2026-07-31 on pre-prod, 34 tenants).
# The retained-turn tier is the ONLY home for a turn the ontology could not cast, and
# `episodic_log` has exactly ONE consumer: this drain. Nothing reads it at query time.
# So the drain rate IS the tier's usefulness. Measured: `REEMBED_INTERVAL=300` but the
# OBSERVED per-tenant cycle interval was ~11.5-15.0 min (13:22:24 → 13:37:22 → 13:49:59 →
# 14:01:30 for one tenant) — the loop's own work across 34 tenants dominates the configured
# sleep, so the effective rate was 5 rows / ~13 min ≈ 23 rows/hour/tenant. The worst tenant
# carried 358 eligible rows → ~15.5 h to catch up. That gap is EMERGENT and grows linearly
# with tenant count: as tenants are added the cycle stretches and a fixed
# 5-rows-per-cycle becomes decorative. Hence a backlog-proportional batch rather than a
# bigger constant — it spends the extra budget only where there IS a backlog.
#
# ⚠️ STORM CASE (why the abort exists, and why the ceiling is low).
# Every row costs at least one extraction LLM call plus an /ingest that does its own LLM
# work in the gate. A sick brain does NOT fail fast: the observed failure mode is
# `reextract_row_failed: timed out` against a 60 s httpx timeout. So `batch_size` sick rows
# cost `batch_size × 60 s` PER TENANT PER CYCLE — the cycle stretches, which stretches every
# other tenant's interval, which grows every backlog, which is self-feeding. Raising the
# batch without a failure bound multiplies exactly that. The abort stops a tenant's batch
# after N CONSECUTIVE row failures, so a dead brain costs N timeouts per tenant per cycle
# instead of `batch_size` of them, and the loop keeps its cadence. It also protects the
# shared circuit breaker (`LLM_CIRCUIT_BREAKER_THRESHOLD`, default 5, `src/api/llm_calls.py`):
# burning 25 doomed calls per tenant is how one backlogged tenant trips the breaker for
# every other tenant's LIVE traffic.
# The retained turn is NOT lost by aborting — an un-stamped row is retried next cycle by
# construction; aborting only declines to pay for calls we have evidence will fail.
#
# ⚠️ MONEY. On a metered brain the ceiling is the bill. Ceiling 25 × 34 tenants × ~5
# cycles/h ≈ 4 250 extractions/hour worst case, vs ~850 today. That is why the flag is OFF
# by default and the ceiling is deliberately modest — turn it on per-deploy, measure, raise.
#
# ⚠️ SUPERSEDED 2026-08-01 by REEXTRACT_PRESERVE_PROVENANCE (see the flag block above).
# This used to read: "NOT CHANGED: `source='reextract'` → `llm_inferred` stays exactly as it
# is. A re-derivation lacks the live turn's context, so it genuinely IS inference, not user
# testimony." The OWNER OVERRULED that ("Re-stated should be able to go from B→A. User is
# truth should be respected."), and the reasoning was wrong on its own terms: it conflated
# EXTRACTION QUALITY with PROVENANCE. Re-reading our own verbatim record of the user's words
# does not change who said them.
# What DOES survive from that note, and is still binding: DRAINING FASTER MUST NOT LAUNDER
# PROVENANCE. Rate and authority stay orthogonal — the drain knobs below decide HOW MANY rows
# are re-mined, never WHAT AUTHORITY they come back with. Authority is decided solely by the
# row's recorded origin lane, and an origin that cannot be established is never elevated.
REEXTRACT_BACKLOG_DRAIN = os.getenv(
    "REEXTRACT_BACKLOG_DRAIN", "false").strip().lower() in ("true", "1", "yes", "on")

# Age gate in MINUTES (flag ON only). Default 60 == today's `INTERVAL '1 hour'`.
_REEXTRACT_MIN_AGE_MINUTES = max(0, int(os.getenv("REEXTRACT_MIN_AGE_MINUTES", "60")))
# Hard ceiling on the backlog-proportional batch (flag ON only).
_REEXTRACT_MAX_BATCH = max(1, int(os.getenv("REEXTRACT_MAX_BATCH", "25")))
# Fraction of a tenant's eligible backlog to attempt per cycle (flag ON only).
_REEXTRACT_DRAIN_FRACTION = max(0.0, float(os.getenv("REEXTRACT_DRAIN_FRACTION", "0.10")))
# Consecutive per-row failures that abort this tenant's batch (flag ON only). 0 disables.
_REEXTRACT_FAIL_ABORT = max(0, int(os.getenv("REEXTRACT_FAIL_ABORT", "3")))


# === THE PREDECESSOR BUDGET (unflagged; `=0` on either knob is the legacy behaviour) =====
#
# WHAT WENT WRONG, MEASURED ON THIS BOX 2026-08-27 (container up 02:46, one cycle observed):
# `reextract_episodic` spent 403.4 SECONDS on ONE tenant (`e9bf22d4`) — 45% of the entire
# elapsed PHASE-2b pass — while the other 1,212 tenants in the same pass cost a MEDIAN of
# 0.03 s each. The whole 403 s is two serial LLM timeouts on a SINGLE row:
#     02:58:01  llm_call_async.attempt_start operation=REFRAME timeout_seconds=180.0
#     03:01:01  reextract_spine_failed  (falling back to /extract/rewrite): timed out
#     03:04:01  reextract_row_failed episodic_id=1: timed out
# The spine path times out at 180 s, the fail-safe /extract/rewrite times out at 180 s again,
# and the batch then keeps going and pays it per row. THE PER-ROW COST IS UNBOUNDED, and the
# thing it starves is everything after it — for this tenant (document drain, Class-C promotion
# and decay, the strike evaluator all sit BELOW reextract in the same per-tenant block) and for
# every tenant behind it in the serial pass, including the ontology-growth sweeps that only
# begin once the whole pass is done.
#
# ⛔ WHY THE EXISTING STORM BOUND DOES NOT COVER THIS — AND WHY IT USED TO.
# `REEXTRACT_FAIL_ABORT` was built (63cdd1cf) for EXACTLY this shape; its own comment names it:
# "a sick brain fails SLOW … so `batch_size` sick rows cost `batch_size × 60 s` PER TENANT PER
# CYCLE". It arms off `consecutive_failures`. Then 28215456 — correctly, fixing a real data-loss
# bug where a lane outage stamped `reextracted_at` and froze turns out of eligibility FOREVER —
# introduced `_exempt` and wired it to that SAME counter: `if not _exempt: consecutive_failures
# += 1`. A timeout is an `httpx.TransportError` and a 500 is an `HTTPStatusError >= 500`, so both
# set `_backend_down` → `_exempt` → the counter never moves. The abort's DESIGNED TRIGGER BECAME
# ITS EXCLUSION. The live log says so in as many words: "not stamped, not counted toward the
# storm bound". Flipping `REEXTRACT_BACKLOG_DRAIN=true` does not restore it — the flag arms a
# counter that no longer counts, and its backlog-proportional batch (ceiling 25) would multiply
# the 403 s by five.
#
# THE SPLIT THIS RESTORES: the exemption is a STAMPING decision (do not burn a row's one
# best-effort pass on an outage that was not its fault) and it stays exactly as it is. Aborting
# is a SCHEDULING decision (do not pay a 360 s timeout again for a lane we have just proven is
# down). They were conflated because they shared one counter; they are now separate, and the
# scheduling half is unflagged because the storm defence it restores has been dark since it
# shipped and the wedge is live.
#
# WHY WALL-CLOCK AND NOT A ROW COUNT. Both existing bounds count ROWS. Rows are not the quantity
# that starves anyone — seconds are, and the per-row cost varies by four orders of magnitude
# here (0.03 s to 360 s). A budget in seconds bounds the predecessor's contribution to cycle
# time regardless of per-row latency, which a row count cannot do.
#
# NOT A DRAIN THROTTLE. Neither knob reduces the steady-state drain rate on a HEALTHY brain: a
# tenant that drains its batch inside the budget is untouched, and the budget is only consulted
# BETWEEN rows, never mid-row. What they remove is the ability of one tenant, or one sick brain,
# to consume the cycle.
#
# Per-tenant wall-clock budget for the episodic drain, checked BETWEEN rows. 0 disables (legacy).
_REEXTRACT_TENANT_BUDGET_S = max(0.0, float(os.getenv("REEXTRACT_TENANT_BUDGET_S", "90")))
# Cycle-wide wall-clock budget for the episodic drain ACROSS all tenants. 0 disables (legacy).
# Bounds the aggregate: per-tenant bounding alone still permits N_tenants × tenant_budget.
_REEXTRACT_CYCLE_BUDGET_S = max(0.0, float(os.getenv("REEXTRACT_CYCLE_BUDGET_S", "600")))


def _reextract_cycle_gate(armed: bool, cursor, user_id, spent: float, budget: float):
    """The rotation + cycle-budget decision for ONE tenant. Pure — no clock, no DB, no I/O.

    Extracted from the loop deliberately: this is the term whose FAILURE MODE is a silent
    starvation (deny everyone forever), and a decision buried in a 1,700-line cycle body is a
    decision nobody can ablate. Here it can be, row by row.

    Returns ``(armed, may_drain)``.

      * ``armed`` — has the rotation cursor been reached? Until it has, this cycle deliberately
        declines to spend budget, so the seats the LAST cycle never got to are served first.
        ``cursor is None`` means "start from the top" and arms immediately.
      * ``may_drain`` — armed AND the cycle budget is not yet spent. ``budget == 0`` disables
        the cap entirely (legacy behaviour: every armed tenant drains).

    The budget is compared with ``<``, so a cycle that has spent EXACTLY its budget stops. The
    comparison is against spend ALREADY BOOKED, never a prediction — the loop cannot know what
    the next tenant will cost, and guessing would either over-admit (no bound) or under-admit
    (starve a cheap tenant behind an expensive one).
    """
    if not armed and (cursor is None or str(user_id) == cursor):
        armed = True
    may_drain = armed and (not budget or spent < budget)
    return armed, may_drain


# === FLAG: REEXTRACT_ONTOLOGY_REGROWTH (CTIER increment 2, default OFF) ======
#
# THE GAP. The retained-turn tier's entire advantage over an embedding is that a turn can
# be RE-READ against a GROWN ontology — /expand adds a place, and a turn that was
# untypeable last month becomes typeable now. That is the growth engine running BACKWARDS
# over history. It does not currently run backwards: a turn that yields zero edges is
# stamped `reextracted_at = now(), extracted_fact_count = 0`, and the eligibility scan is
# `WHERE reextracted_at IS NULL`, so it is frozen out PERMANENTLY however much the ontology
# grows afterwards. MEASURED (pre-prod 2026-07-31, 34 tenants): 102 of 542 drained turns
# (18.8%) are stamped zero-edge. Those 102 are the tier's whole reason to exist and not one
# of them will ever be looked at again.
#
# WHAT THIS FLAG DOES. Re-opens a zero-edge turn — but ONLY on evidence that the tenant
# gained somewhere to file it, and only a bounded number of times:
#     eligible again  ⇔  extracted_fact_count = 0
#                        AND reextract_attempts < REEXTRACT_MAX_ATTEMPTS
#                        AND walkable_places_now >= mark_at_last_attempt + MIN_GROWTH
# The existing "retry every cycle forever is waste" objection is correct and is preserved:
# without growth there is no retry.
#
# ⚠️ WHY THE MARK COUNTS *WALKABLE* PLACES — this is the VERIFY-THE-READ half.
# `_tenant_walkable_place_count` counts rel_types whose category is NOT 'pending_placement',
# plus entity_taxonomies rows. A novel rel minted in-flow lands `category='pending_placement'`
# and is NEVER appended to `entity_taxonomies.rel_types_defining_group` (src/api/main.py:5502),
# so the query path's `rel_type = ANY(allowed_rels)` projection can never admit it and a fact
# filed under it does NOT come back from a scoped recall. It becomes retrievable only after
# `drain_pending_placement_by_morphology` (below, :6701) folds it onto a seeded canonical,
# where it adopts a real category and enters a taxonomy. Marking on WALKABLE places means a
# turn is re-mined when there is somewhere RETRIEVABLE to put it — never merely somewhere to
# write it. Re-mining into a pending rel would be a write with no reader.
#
# ⚠️ PRIMARY-SOURCE WARNING THAT SHAPED THIS DESIGN. Zhang et al., "Useful Memories Become
# Faulty When Continuously Updated by LLMs" (arXiv 2605.12978), report that agents preserving
# raw episodes roughly DOUBLED the accuracy of forced-consolidation counterparts, and
# recommend treating raw episodes as first-class evidence and GATING CONSOLIDATION EXPLICITLY.
# Unbounded re-mining is precisely the continuous-update failure they measure. Hence: an
# explicit growth gate (not a timer), a hard attempt bound, and — untouched — the
# `llm_inferred` provenance on every re-mined edge, which keeps a re-derivation from ever
# overwriting user testimony. The raw turn itself is never mutated or deleted.
#
# DETERMINISTIC: two integer counts and a comparison. No cosine, no embedding, no LLM in the
# eligibility decision. Subject-agnostic: it COUNTS places, it never names one.
#
# Requires migration 197 (episodic_log.reextract_ontology_mark / .reextract_attempts).
# Fail-safe: if those columns are absent the lane self-disables for that tenant and the job
# runs exactly as it does today.
REEXTRACT_ONTOLOGY_REGROWTH = os.getenv(
    "REEXTRACT_ONTOLOGY_REGROWTH", "false").strip().lower() in ("true", "1", "yes", "on")

# How many NEW walkable places must appear before a zero-edge turn is retried.
_REEXTRACT_MIN_GROWTH = max(1, int(os.getenv("REEXTRACT_MIN_GROWTH", "5")))
# Hard bound on re-mining passes for one turn (the consolidation-churn bound).
_REEXTRACT_MAX_ATTEMPTS = max(1, int(os.getenv("REEXTRACT_MAX_ATTEMPTS", "5")))

# Tenants already found to lack migration 197 — log once, then self-disable the lane.
_regrowth_columns_missing_schemas: set = set()


def _tenant_walkable_place_count(db_conn, schema_name: str = None) -> Optional[int]:
    """Count this tenant's WALKABLE places — the ontology-growth mark.

    walkable places = rel_types with a real (non-'pending_placement') category
                    + entity_taxonomies rows

    Deterministic, subject-agnostic, two counts. Returns None on any failure, which the
    caller treats as "no growth evidence" → today's behaviour (never a spurious retry).
    """
    try:
        with db_conn.cursor() as cur:
            cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                """
                SELECT (SELECT count(*) FROM rel_types
                          WHERE category IS DISTINCT FROM %s)
                     + (SELECT count(*) FROM entity_taxonomies)
                """,
                (_CATEGORY_PENDING_RE,),
            )
            return int(cur.fetchone()[0] or 0)
    except Exception as e:
        log.warning(f"re_embedder.reextract_place_count_failed schema={schema_name}: {e}")
        _rollback_and_reapply_search_path(db_conn, schema_name)
        return None


def _regrowth_predicate_sql(place_mark: Optional[int]) -> tuple[str, tuple]:
    """Return (sql_fragment, params) for the episodic eligibility predicate.

    Flag OFF (or no usable mark) → the shipped `reextracted_at IS NULL`, byte-identical.
    Flag ON → that, OR a zero-edge turn whose tenant has grown enough walkable places
    since its last attempt and which has attempts left.
    """
    if not REEXTRACT_ONTOLOGY_REGROWTH or place_mark is None:
        return "reextracted_at IS NULL", ()
    return (
        "(reextracted_at IS NULL"
        " OR (extracted_fact_count = 0"
        "     AND reextract_attempts < %s"
        "     AND (reextract_ontology_mark IS NULL"
        "          OR %s >= reextract_ontology_mark + %s)))",
        (_REEXTRACT_MAX_ATTEMPTS, place_mark, _REEXTRACT_MIN_GROWTH),
    )


def _reextract_age_interval_sql() -> tuple[str, tuple]:
    """Return (sql_fragment, params) for the eligibility age gate.

    Flag OFF → the literal `INTERVAL '1 hour'` this job has always used (byte-identical).
    Flag ON  → a parameterised minute interval so the gate is tunable per deploy.

    WHY THE GATE EXISTS AT ALL (checked before touching it — the stated reason and the
    unstated one are different, and only one of them is now covered elsewhere):
      • STATED (docstring below): "re-mining text the live pipeline just processed with the
        SAME ontology has no value; the value comes from ontology growth in between." That
        reason STANDS and is not covered by anything else — it is an economics argument, and
        it is why the default stays 60 rather than dropping to 0. Below a few minutes the
        drain is pure spend against an ontology that has not moved.
      • UNSTATED, and the one worth naming: the gate also kept the backfill off a turn whose
        LIVE ingest was still in flight or transiently failing. Re-mining THAT turn is worse
        than wasteful — the live path would have stored it `user_stated` (Class A/B) while
        the backfill stores it `llm_inferred` (Class B/C), i.e. a PROVENANCE DOWNGRADE won by
        whichever raced first. `_ingest_with_retry` (`src/mcp/server.py`, shipped 00e69b5e)
        now retries transient live-path failures IN-BAND at the correct provenance, so this
        second job is largely covered — which is what makes the gate safe to shorten. It is
        not a reason to remove it: the deferred-drain arm of that retry can still be in
        flight, so the floor should stay comfortably above the retry budget, not at zero.
    CONCLUSION: make it tunable, keep the default at today's 60 minutes, and do not
    recommend going below single-digit minutes.
    """
    if not REEXTRACT_BACKLOG_DRAIN:
        return "created_at < now() - INTERVAL '1 hour'", ()
    return "created_at < now() - (%s * INTERVAL '1 minute')", (_REEXTRACT_MIN_AGE_MINUTES,)


def _reextract_effective_batch(db_conn, base_batch: int, age_sql: str, age_params: tuple) -> int:
    """Backlog-proportional batch size (flag ON only); `base_batch` verbatim when OFF.

    A tenant that is caught up keeps today's modest batch. A tenant that is BEHIND gets
    `ceil(backlog × REEXTRACT_DRAIN_FRACTION)` rows, clamped to [base, REEXTRACT_MAX_BATCH].
    Proportional rather than constant so the extra LLM spend lands only where there is a
    backlog to clear, and CLAMPED so a huge backlog cannot convert one cycle into an
    unbounded bill (see the storm note on the flag).

    Fail-safe: any failure counting the backlog returns `base_batch` — never a bigger one.
    """
    if not REEXTRACT_BACKLOG_DRAIN:
        return base_batch
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                f"SELECT count(*) FROM episodic_log "
                f"WHERE reextracted_at IS NULL AND {age_sql}",
                age_params,
            )
            backlog = int(cur.fetchone()[0] or 0)
        db_conn.commit()
    except Exception as e:
        log.warning(f"re_embedder.reextract_backlog_count_failed (using base batch): {e}")
        try:
            db_conn.rollback()
        except Exception:
            pass
        return base_batch
    if backlog <= base_batch:
        return base_batch
    proportional = int(math.ceil(backlog * _REEXTRACT_DRAIN_FRACTION))
    return max(base_batch, min(proportional, _REEXTRACT_MAX_BATCH))


def reextract_episodic(db_conn, backend_url: str, user_id: str, schema_name: str = None,
                       batch_size: int = 5, statement_route: str = "rewrite") -> int:
    """
    Periodic episodic re-extraction backfill (the "retry the cast" operator).

    FaultLine's memory IS the per-tenant PostgreSQL knowledge graph; Qdrant is only
    the fallback net for content the ontology could not cast into the graph yet.
    Text that failed to structure at time T failed because the ontology AT T could
    not type it — but the per-tenant ontology self-grows (new rel_types, hierarchy,
    taxonomies). This job re-mines raw episodic_log text through the NORMAL front
    door (spine /harvest-spans or /extract/rewrite → /ingest) so previously-
    uncastable content can land in the graph, shrinking the fallback net over time.

    Batch discipline: LIMIT `batch_size` rows per cycle (each row costs an LLM
    extraction call — keep it modest). Only rows older than 1 hour are eligible:
    re-mining text the live pipeline just processed with the SAME ontology has no
    value; the value comes from ontology growth in between.

    Under REEXTRACT_BACKLOG_DRAIN (default OFF) both of those become tunable and a
    consecutive-failure abort bounds the storm case — see the flag block above
    (`_reextract_age_interval_sql` / `_reextract_effective_batch`). Flag OFF is
    byte-for-byte the behaviour described in this docstring.

    Row outcomes:
      • intent RETRACTION/CORRECTION → stamped WITHOUT re-ingest (re-ingesting a
        retraction as a statement would resurrect the retracted fact).
      • extract + ingest CONFIRM success → stamped, extracted_fact_count recorded.
        "Confirm" is the POSITIVE success set, not HTTP 2xx: /ingest must answer
        status="valid" and the extractor status="success" (harvest-spans: no
        status key). A 200 carrying any other status — known failure or a shape
        not yet invented — is NOT a success and leaves the row unstamped.
      • zero non-low-confidence edges → stamped as SUCCESS with count 0: the
        ontology still can't cast it. The raw text stays in episodic_log forever
        (raw substrate is never deleted); a future "re-mine all" admin action can
        clear reextracted_at in bulk. Retrying every cycle forever is waste.
      • extract/ingest FAILURE      → reextracted_at left NULL (retry next cycle),
        EXCEPT rows older than 30 days, which get stamped after this one
        best-effort pass so a poison row cannot clog the LIMIT-N batch forever.

    Provenance (REEXTRACT_PRESERVE_PROVENANCE, default ON — see the flag block above):
    a retained turn is re-ingested under its ORIGINAL ingest source, so a user-stated
    turn comes back user_stated and lands Class A exactly as it would have first time.
    Only rows whose origin lane is KNOWN to have been user-stated ('mcp', 'document')
    are preserved; an unknown or non-user-stated origin (NULL, 'store_context_deferred')
    keeps the legacy source="reextract" → llm_inferred lane, with _reextract_row_edges
    still FORCING llm_inferred on every edge so a spine edge stamped user_stated cannot
    sneak through. PRESERVING, NEVER ELEVATING — an origin we cannot establish is left
    at its lower tier rather than guessed, because guessing fabricates authority.
    Flag OFF → every row takes the legacy lane, byte-for-byte.
    The cast always pays the validation toll: this function never touches the WGM gate,
    class assignment, or query code.

    Facts are NOT forced to be user-tied: raw_text passes through untouched
    (e.g. network diagrams describe machines, not the user).

    Caller contract: runs INSIDE the main loop's INGEST_ENABLED guard (Feature 1) —
    a frozen store never re-mines (and the gated backend would refuse the /ingest
    anyway). Per-tenant error isolation mirrors promote_staged_facts: any failure
    rolls back this tenant's connection and returns; it never poisons the loop.

    Args:
        db_conn: PostgreSQL connection (per-user schema context via search_path)
        backend_url: FaultLine backend API base URL (FAULTLINE_API_URL)
        user_id: tenant user UUID (scoping key for extraction + /ingest)
        schema_name: user schema; if provided, sets search_path (house style)
        batch_size: max rows re-mined per cycle (REEXTRACT_BATCH_SIZE, default 5)
        statement_route: "spine" | "rewrite" — the backend brain's STATEMENT
            extractor decision (GET /internal/ingest-route), resolved ONCE per
            cycle by the caller (document-lane parity; default "rewrite").

    Returns:
        Count of rows stamped (re-extracted, skipped-as-retraction, or zero-edge).
    """
    processed = 0
    try:
        # Optional per-user search_path (mirror promote_staged_facts house style).
        if schema_name:
            try:
                with db_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {schema_name}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema from UUID-derived source with validation
                db_conn.commit()
            except Exception as e:
                log.warning(f"re_embedder.reextract_search_path_failed schema={schema_name}: {e}")
                # Continue with current search_path

        # Eligibility age gate + batch size. Flag OFF (default) → the literal
        # `INTERVAL '1 hour'` and the caller's batch_size, byte-identical to the shipped
        # behaviour. Flag ON → REEXTRACT_MIN_AGE_MINUTES and a backlog-proportional batch.
        _age_sql, _age_params = _reextract_age_interval_sql()
        _effective_batch = batch_size

        # Increment 2: ontology-regrowth re-eligibility. Resolve the tenant's walkable-place
        # mark ONCE per cycle; None (flag OFF, count failed, or migration 197 absent) keeps
        # the shipped `reextracted_at IS NULL` predicate exactly.
        _place_mark = None
        if (REEXTRACT_ONTOLOGY_REGROWTH
                and (schema_name or user_id) not in _regrowth_columns_missing_schemas):
            _place_mark = _tenant_walkable_place_count(db_conn, schema_name)
        _elig_sql, _elig_params = _regrowth_predicate_sql(_place_mark)

        try:
            with db_conn.cursor() as cur:
                if REEXTRACT_BACKLOG_DRAIN:
                    _effective_batch = _reextract_effective_batch(
                        db_conn, batch_size, _age_sql, _age_params)
                    if _effective_batch != batch_size:
                        log.info(f"re_embedder.reextract_backlog_drain user_id={user_id[:8]} "
                                 f"batch={_effective_batch} (base={batch_size})")
                cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                    f"""
                    SELECT id, raw_text, source_ref, intent, created_at, source
                    FROM episodic_log
                    WHERE {_elig_sql}
                      AND {_age_sql}
                    ORDER BY created_at ASC
                    LIMIT %s
                    """,
                    _elig_params + _age_params + (_effective_batch,)
                )
                rows = cur.fetchall()
            # READ BARRIER — THE PRIMARY WOUND. The loop below calls
            # _reextract_row_edges (a backend /harvest-spans or /extract/rewrite hop that
            # has been measured at 180s on the background lane) once per row, on THIS
            # connection. The batch SELECT's transaction was held across all of it.
            #
            # Releasing costs nothing in claim safety: this SELECT takes no row locks
            # (no FOR UPDATE) and the session is READ COMMITTED, so the open transaction
            # never reserved these rows from another worker in the first place. Every
            # per-row write below is idempotent and id-addressed, so a row another cycle
            # already stamped is simply re-stamped, never double-applied.
            release_read_transaction(
                db_conn, context=f"re_embedder.reextract_episodic.fetch schema={schema_name}")
        except psycopg2.Error as e:
            # Older schemas may predate migration 127 — not an error, just skip.
            # Roll back + re-apply the tenant search_path so the aborted txn cannot
            # poison the rest of this tenant's cycle (per-tenant isolation, f4839d4).
            _rollback_and_reapply_search_path(db_conn, schema_name)
            if getattr(e, "pgcode", None) == "42703" and _place_mark is not None:
                # undefined_column — migration 197 not applied on this tenant. Self-disable
                # the regrowth lane for this schema (log ONCE) and fall back to the shipped
                # predicate rather than skipping the tenant's drain entirely. The flag must
                # never be able to STOP the drain that already works.
                _key = schema_name or user_id
                if _key not in _regrowth_columns_missing_schemas:
                    _regrowth_columns_missing_schemas.add(_key)
                    log.warning(
                        f"re_embedder.reextract_regrowth_columns_missing schema={schema_name} "
                        f"(migration 197 not applied — regrowth lane disabled for this tenant)")
                # READ BARRIER (immediately before the blocking call — the RE-ARM case). A barrier at
                # the top of the enclosing block is NOT enough: a per-row read helper opens a FRESH
                # transaction after it, and that read then rides across this hop. Measured live on the
                # deployed image — climb_classification_chains was killed twice this way (02:42:25 and
                # 02:45:06), its whole _ont_db subsystem chain failing 'connection already closed' four
                # seconds later.
                release_read_transaction(db_conn, context="re_embedder.reextract_episodic.pre_blocking_call")
                return reextract_episodic(db_conn, backend_url, user_id, schema_name,
                                          batch_size, statement_route)
            if getattr(e, "pgcode", None) == "42P01":  # undefined_table
                key = schema_name or user_id
                if key not in _episodic_log_missing_schemas:
                    _episodic_log_missing_schemas.add(key)
                    log.info(f"re_embedder.reextract_no_episodic_log schema={schema_name} "
                             f"(migration 127 not applied — skipping)")
                return 0
            raise

        if not rows:
            return 0

        # STORM BOUND (flag ON only): consecutive row failures abort this tenant's batch.
        # A sick brain fails SLOW (observed: `reextract_row_failed: timed out` against a 60 s
        # httpx timeout), so a doomed batch costs batch_size × 60 s per tenant per cycle and
        # stretches the cycle for every OTHER tenant — the self-feeding case. Bail after N in
        # a row rather than paying for calls we have evidence will fail, and stop feeding the
        # shared circuit breaker (threshold 5) with doomed backfill calls that would
        # fail-fast LIVE traffic. Nothing is lost: an
        # un-stamped row is retried next cycle by construction.
        consecutive_failures = 0
        # PER-TENANT WALL-CLOCK BUDGET (see "THE PREDECESSOR BUDGET" block above). Checked
        # BETWEEN rows only — a row already in flight always finishes, so this can never
        # truncate a write or leave a half-applied ingest. Un-attempted rows keep
        # `reextracted_at IS NULL` and are re-selected next cycle by construction, so the
        # budget defers work, never drops it.
        _rx_t0 = time.monotonic()
        _rx_attempted = 0

        for row_id, raw_text, source_ref, intent, created_at, ep_source in rows:
            # `_rx_attempted` gates the check so EVERY tenant always attempts at least one
            # row: a budget that can deny a tenant its first row would let a slow neighbour
            # starve it outright, which is the failure this whole block exists to prevent.
            # READ BARRIER (per iteration): this loop body blocks on the brain/Qdrant, and a
            # read left open by the PREVIOUS iteration would ride across it. A batch-level
            # barrier alone does not cover this — measured live: climb_state and the
            # taxonomy reads were each caught idle-in-transaction at 58-59s inside a loop.
            release_read_transaction(db_conn, context="re_embedder.reextract_episodic.iteration")
            if _REEXTRACT_TENANT_BUDGET_S and _rx_attempted:
                _rx_elapsed = time.monotonic() - _rx_t0
                if _rx_elapsed >= _REEXTRACT_TENANT_BUDGET_S:
                    log.warning(
                        f"re_embedder.reextract_tenant_budget_exhausted user_id={user_id[:8]} "
                        f"schema={schema_name} elapsed={_rx_elapsed:.1f}s "
                        f"budget={_REEXTRACT_TENANT_BUDGET_S:.0f}s processed={processed} "
                        f"deferred={len(rows) - processed} "
                        f"(un-stamped rows retry next cycle; the rest of this tenant's "
                        f"lifecycle and every tenant behind it get their turn)"
                    )
                    break
            _rx_attempted += 1
            # RETRACTION/CORRECTION rows must NOT be re-ingested as statements —
            # that would resurrect the very facts they retracted. Stamp and skip.
            if (intent or "").upper() in ("RETRACTION", "CORRECTION"):
                with db_conn.cursor() as cur:
                    cur.execute(
                        "UPDATE episodic_log SET reextracted_at = now() WHERE id = %s",
                        (row_id,)
                    )
                db_conn.commit()
                processed += 1
                log.info(f"re_embedder.reextract_skipped_retraction episodic_id={row_id} "
                         f"user_id={user_id[:8]} intent={intent} (stamped without re-ingest)")
                continue

            # QUERY / STATEMENT / NULL intents are fair game — queries often carry
            # embedded facts and were misrouted; that's part of why we captured them.
            try:
                # PROVENANCE PRESERVATION (REEXTRACT_PRESERVE_PROVENANCE, default ON).
                # Resolve the ORIGINAL ingest source for this retained turn. A known
                # user-stated origin lane ('mcp'/'document') re-ingests under that same
                # live source string, so /ingest's router assigns the provenance it
                # assigned first time and assign_class_and_confidence lands Class A when
                # the rel's defined class is A. An unknown origin (NULL, anything new)
                # returns None and keeps the legacy "reextract" → llm_inferred lane
                # UNCHANGED. Preserving, never elevating: an origin we cannot establish
                # is never guessed. The DOWN-ROUTING entries ('store_context_deferred',
                # 'unattested') return the explicit "unattested" lane instead — the
                # router forces llm_inferred there and the class force lands staged B.
                _preserve_source = _reextract_ingest_source(ep_source)
                _ingest_source = _preserve_source or "reextract"

                edges = _reextract_row_edges(raw_text, user_id, backend_url,
                                             statement_route, _preserve_source)

                if edges:
                    ingest_resp = httpx.post(
                        f"{backend_url}/ingest",
                        json={
                            "text": raw_text,
                            "user_id": user_id,
                            "edges": edges,
                            # Preserved origin source (user-stated lane) or the legacy
                            # "reextract" → provenance router else-branch → llm_inferred.
                            "source": _ingest_source,
                            # Citable provenance rides along (migration 128).
                            "source_ref": source_ref,
                        },
                        # Lane header so /ingest's own LLM-fallback self-call (GLiNER2
                        # empty/miss) stays in the BACKGROUND lane end-to-end.
                        #
                        # REPLAY MARKER (gauntlet reextract-replay-resurrection): a re-mined
                        # episodic row is BY CONSTRUCTION a replay — the turn's live ingest
                        # already ran when the row was captured. Without the marker this POST
                        # arrived at /ingest indistinguishable from a FRESH user statement
                        # (source preserved 'mcp' -> user_stated rank 3, tie passes the SS5
                        # gate), so a value /retract/correct had retired in the interleaving
                        # RESURRECTED: superseded_at -> NULL, history 'ingest_restate'
                        # (live wound 2026-09-04). The marker extends
                        # the shipped replay-transport contract to this third writer: the
                        # backend's retired-value freeze treats this POST as the re-send it is.
                        # The lane hint header names the sender for attribution/logging; it is
                        # informational only (ingest_transport.py). Header-only, never the
                        # body — the idempotency key hashes edges and must stay byte-identical
                        # to any live ingest of the same turn.
                        headers={
                            **_BACKEND_LANE_HEADERS,
                            **_backend_auth_headers(),
                            _ingest_transport.REPLAY_MARKER_HEADER:
                                _ingest_transport.REPLAY_MARKER_VALUE,
                            _ingest_transport.REPLAY_LANE_HEADER:
                                _ingest_transport.REPLAY_LANE_REEXTRACT,
                        },
                        timeout=30.0,
                    )
                    ingest_resp.raise_for_status()
                    # POSITIVE-SUCCESS GATE — the re-mine's core honesty rule. /ingest
                    # answers some failures as HTTP 200 with a status-bearing body:
                    # ingest_disabled (frozen store) AND status="error" (provisioning /
                    # schema-context failure — nothing stored). The old guard checked the
                    # ONE named failure, so an "error" body fell through to the stamp
                    # below with extracted_fact_count = len(edges) > 0 — and the only
                    # predicate that ever re-opens a stamped row (REEXTRACT_ONTOLOGY_
                    # REGROWTH) requires extracted_fact_count = 0, so the turn was
                    # closed PERMANENTLY as "successfully processed" with nothing stored.
                    # Gate on the ONE success status instead: /ingest returns
                    # status="valid" on its only success path (and on idempotency cache
                    # hits, which cache success only). Any other body — a known failure
                    # or a shape that does not exist yet — raises, the stamp never runs,
                    # and the row is re-mined next cycle. raise_for_status() cannot
                    # help: none of these failures is an HTTP error.
                    _ingest_body = ingest_resp.json()
                    if _ingest_body.get("status") != "valid":
                        _iwhy = (_ingest_body.get("error")
                                 or f"status={_ingest_body.get('status')!r}")
                        raise RuntimeError(
                            f"ingest did not confirm storage ({_iwhy}) — not stamping")
                    # `ingest_source` is the greppable evidence of which lane a row took:
                    # "mcp" == provenance PRESERVED (origin was user-stated), "reextract"
                    # == held at llm_inferred. Without it a demotion is invisible again.
                    log.info(f"re_embedder.reextract_ingested episodic_id={row_id} "
                             f"user_id={user_id[:8]} edges={len(edges)} route={statement_route} "
                             f"origin={ep_source} ingest_source={_ingest_source} "
                             f"provenance_preserved={_preserve_source is not None}")
                else:
                    # Zero-edge = SUCCESS: the ontology still can't cast it. Stamp it
                    # (raw text is retained forever; bulk re-mine can reset the stamp).
                    log.info(f"re_embedder.reextract_zero_edges episodic_id={row_id} "
                             f"user_id={user_id[:8]} (stamped — still uncastable)")

                with db_conn.cursor() as cur:
                    if _place_mark is not None:
                        # Increment 2: record the walkable-place mark at THIS attempt and
                        # bump the attempt counter, so a zero-edge turn re-opens only once
                        # the tenant has gained MIN_GROWTH more places — and only
                        # MAX_ATTEMPTS times ever. A turn that DID cast (len(edges) > 0) is
                        # terminal exactly as before: the predicate only re-opens
                        # extracted_fact_count = 0.
                        cur.execute(
                            "UPDATE episodic_log SET reextracted_at = now(), "
                            "extracted_fact_count = %s, reextract_ontology_mark = %s, "
                            "reextract_attempts = reextract_attempts + 1 WHERE id = %s",
                            (len(edges), _place_mark, row_id)
                        )
                    else:
                        cur.execute(
                            "UPDATE episodic_log SET reextracted_at = now(), extracted_fact_count = %s WHERE id = %s",
                            (len(edges), row_id)
                        )
                db_conn.commit()
                processed += 1
                consecutive_failures = 0

            except RateDeferred:
                # The pass never asked the model (no rate capacity). NOT a failure
                # verdict and NOT a poison-row: stamping here — including via the
                # 30-day guard below — would terminally record "uncastable" from a
                # call that did not happen. Leave the row NULL; retry next cycle.
                _rollback_and_reapply_search_path(db_conn, schema_name)
                log.info(f"re_embedder.reextract_row_deferred episodic_id={row_id} "
                         f"user_id={user_id[:8]} (rate-deferred; will retry next cycle)")
                continue

            except Exception as e:
                log.error(f"re_embedder.reextract_row_failed episodic_id={row_id} user_id={user_id[:8]}: {e}")
                _rollback_and_reapply_search_path(db_conn, schema_name)
                # PACED ≠ FAILED (critic pass 4): a rate_deferred defer never reached the
                # provider — the turn is merely waiting out the daily budget window. It
                # must NOT count toward the failure storm bound, and the poison-row guard
                # below must NOT stamp it: pre-diff this lane rode interactive and never
                # deferred; post-diff a spent day is a DAILY CERTAINTY on free tiers, and
                # stamping terminally skips retained user turns (>30d) for a pace, not an
                # outage. Younger rows already stay NULL and retry; now paced ones do too.
                # ⚠️ AN ABSENT BRAIN IS NOT A POISON ROW EITHER, and it is the case that
                # actually bites. The guard below permanently retires any row older than 30
                # days that fails — and `reextracted_at IS NULL` is the eligibility
                # predicate, so a stamped row is frozen out FOREVER. A tenant whose
                # LLM endpoint is unreachable fails EVERY row for as long as the condition
                # lasts, and nothing tells the user. Thirty days of that and their retained
                # history is silently discarded — by a guard whose entire purpose is to stop
                # ONE bad row clogging the batch, applied to a fault that affects ALL rows.
                #
                # The distinction the guard needs is "did this row fail, or did the whole
                # lane never run": a no-brain / degraded raise is the second, exactly as
                # `rate_deferred` is. Nothing is being made unstampable — a row that genuinely
                # fails extraction against a WORKING brain still gets its one pass and its
                # stamp. This matters more now that the harvest withholds degraded edges: it
                # is what makes "retained for re-extraction" true rather than "retained for up
                # to 30 days".
                #
                # ONE PREDICATE WAS DOING TWO JOBS, and it graded on the wrong thing.
                # "extraction degraded (brain unavailable) - " is the CONSTANT PREFIX every
                # degraded raise carries (see the two raise sites above); only the suffix
                # after the dash names the cause. So matching the prefix matched EVERY
                # failure, including a genuine per-row one, and the poison guard stopped
                # firing at all. It also silently disarmed the control arm of
                # test_paced_reextract_does_not_stamp_poison_rows, whose two arms differ
                # only in that suffix - the test could no longer tell the two apart either.
                #
                # So classify on the REASON, and keep the two conditions SEPARATE because
                # they are different facts about the world:
                #   * PACED     - pacing deferred us; the request never reached the provider.
                #   * LANE DOWN - the brain was unusable for EVERY row, not just this one.
                # They share one consequence (neither is this row's fault, so neither may
                # burn its single best-effort pass) and that shared consequence is exactly
                # what the previous widening was reaching for - it is preserved here. What
                # is restored is the third case: a row that genuinely fails against a
                # WORKING brain still gets stamped and still counts toward the storm bound.
                _err = str(e)
                _reason = (_err.rsplit("—", 1)[-1].strip().lower()
                           if "—" in _err else _err.lower())
                # WHAT EARNS THE EXEMPTION IS THE ENVELOPE, NOT THE REASON WORD.
                # "extraction degraded (brain unavailable)" is raised ONLY when the LLM lane
                # failed to answer — every reason that rides it (rate_deferred,
                # circuit_breaker_open,
                # all_retries_exhausted:*, and the bare "unknown" default) describes the LANE,
                # never this row. A row's OWN fault surfaces as a different exception entirely,
                # and that is the only thing the poison guard should ever stamp.
                #
                # Enumerating reason words instead was my error: it stamped every lane failure
                # whose word was not on the list, and `or "unknown"` is the DEFAULT at the two
                # raise sites — so an outage carrying no reason burned the row's single pass and
                # froze it out of `reextracted_at IS NULL` forever. That is the exact silent
                # loss the block comment above exists to prevent, re-opened from the other side.
                # Default-safe is the only correct bias here: an unattributable failure must
                # never cost a tenant their retained history.
                _degraded_lane = "extraction degraded" in _err or "brain unavailable" in _err
                _paced = "rate_deferred" in _reason
                # THE BACKEND IS A LANE TOO, and it is not on the degraded envelope.
                # `backend ingest disabled` is raised with the comment "nothing stored ->
                # do NOT stamp" a few hundred lines above, and the guard was stamping it
                # anyway: a frozen backend answers that for EVERY row, so one freeze burned
                # the single best-effort pass of every >30d row in the seat. Transport
                # failures and 5xx are the same fault -- lane-wide, nothing to do with this
                # row's content. A 4xx is left to stamp: that IS a per-row rejection.
                _backend_down = "backend ingest disabled" in _err
                try:
                    # httpx.TransportError is the COMMON BASE of all thirteen transport
                    # failures. Enumerating five of them missed WriteTimeout, ReadError,
                    # WriteError, LocalProtocolError, ProxyError and UnsupportedProtocol --
                    # and a stalled backend produces ReadTimeout or WriteTimeout/ReadError
                    # depending purely on where in the exchange it stalls, so the same
                    # outage was exempt or row-fatal by coin flip.
                    if isinstance(e, httpx.TransportError):
                        _backend_down = True
                    elif (isinstance(e, httpx.HTTPStatusError)
                          and getattr(e, "response", None) is not None
                          and e.response.status_code >= 500):
                        _backend_down = True
                except Exception:  # noqa: BLE001 - classification must never raise
                    pass
                _lane_down = (_degraded_lane or _backend_down) and not _paced
                _exempt = _degraded_lane or _paced or _backend_down
                if not _exempt:
                    consecutive_failures += 1
                # Poison-row guard: rows > 30 days old get exactly one best-effort
                # pass — stamp on failure so they can't clog the LIMIT-N batch
                # forever. Younger rows stay NULL and retry next cycle.
                if not _exempt:
                    try:
                        with db_conn.cursor() as cur:
                            cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                                """
                                UPDATE episodic_log SET reextracted_at = now()
                                WHERE id = %s AND created_at < now() - INTERVAL '30 days'
                                """,
                                (row_id,)
                            )
                            stamped_old = cur.rowcount
                        db_conn.commit()
                        if stamped_old:
                            log.warning(f"re_embedder.reextract_poison_row_stamped episodic_id={row_id} "
                                        f"user_id={user_id[:8]} (>30d old, one best-effort pass done)")
                    except Exception as guard_err:
                        log.error(f"re_embedder.reextract_poison_guard_failed episodic_id={row_id}: {guard_err}")
                        _rollback_and_reapply_search_path(db_conn, schema_name)
                else:
                    # Log the two apart - an operator needs to know whether they are waiting
                    # out a pace or looking at an outage; the consequence is identical but
                    # the thing to go and fix is not.
                    _kind = "paced" if _paced else "lane_down"
                    _note = ("waiting out the daily budget window" if _paced
                             else "brain unusable for the whole lane")
                    log.info(f"re_embedder.reextract_row_{_kind} episodic_id={row_id} "
                             f"user_id={user_id[:8]} reason={_reason} note={_note}; "
                             f"not stamped, not counted toward the storm bound")
                # ── LANE-DOWN ABORT (unflagged; the scheduling half of `_exempt`) ──────────
                # `_lane_down` is, by its own definition above, the statement "the brain was
                # unusable for EVERY row, not just this one". Having established that, paying
                # the next row's full timeout is buying the same answer again — 360 s a row on
                # the measured shape (180 s spine + 180 s rewrite fail-safe). Stop the batch.
                #
                # This does NOT touch the stamping decision: `_exempt` still holds, the row is
                # still un-stamped, and every un-attempted row keeps `reextracted_at IS NULL`,
                # so the whole batch is re-selected on the first cycle after a brain returns.
                # Nothing is lost; the only thing declined is spend on calls we have evidence
                # will fail — which is verbatim the justification the storm bound shipped with
                # (63cdd1cf), for the case its own exemption later removed from it.
                #
                # `_paced` is deliberately NOT a trigger: pacing is a deferral we are already
                # observing correctly, and treating it as an outage would abort a healthy lane.
                if _lane_down:
                    _doc_log_crit(
                        "re_embedder.reextract_lane_down_abort",
                        user_id=user_id[:8], schema=schema_name,
                        episodic_id=row_id, reason=_reason[:160],
                        processed=processed, remaining=max(0, len(rows) - processed),
                        note="brain/backend unusable for the whole lane — batch stopped; "
                             "no row stamped, all retried next cycle",
                    )
                    break
                # Storm bound — flag ON only. FAIL LOUD: a batch we declined to finish is a
                # visible CRITICAL, never a success-shaped short count.
                if (REEXTRACT_BACKLOG_DRAIN and _REEXTRACT_FAIL_ABORT
                        and consecutive_failures >= _REEXTRACT_FAIL_ABORT):
                    _doc_log_crit(
                        "re_embedder.reextract_batch_aborted",
                        user_id=user_id[:8], schema=schema_name,
                        consecutive_failures=consecutive_failures,
                        batch=_effective_batch, processed=processed,
                        remaining=max(0, len(rows) - processed),
                        reason="brain_unhealthy_backing_off",
                    )
                    break
                continue

    except Exception as e:
        log.error(f"re_embedder.reextract_error user_id={user_id[:8] if user_id else 'unknown'} schema={schema_name}: {e}")
        _rollback_and_reapply_search_path(db_conn, schema_name)

    return processed


# ── Async document-ingestion worker (documents registry, migration 183) ───────

# How many pending documents to drain per tenant per cycle. Each document can be
# many chunks (each an LLM extraction) — keep it modest so one big corpus dump
# does not starve the rest of the loop.
_DOC_DRAIN_BATCH = int(os.getenv("DOC_DRAIN_BATCH", "2"))
# Chunks within ONE document are INDEPENDENT (each → its own /extract/rewrite + /ingest;
# the worker's db_conn is untouched until the per-doc finalize UPDATE), so draining them
# concurrently turns a serial ~N*Xs pass into ~N/W. W is the fan-out; the LLM-bound
# /extract/rewrite dominates, and the configured LLM endpoint serves concurrent calls.
# 1 = strictly serial (legacy behavior). Env-tunable per deploy.
_DOC_CHUNK_CONCURRENCY = max(1, int(os.getenv("DOC_CHUNK_CONCURRENCY", "6")))

# === FLAG: DOC_CHUNK_FAILURE_LOUD (DOCLOSS-B, default ON) =====================
# ON  → a chunk whose extraction was REJECTED (open circuit breaker), blocked, or
#       answered unparseably is treated as a FAILURE, not as "a chunk with no facts":
#         • brain-level unavailability (breaker open / transport dead) RE-PENDS the whole
#           document, reusing the existing backend-freeze deferral (at-least-once
#           redelivery), bounded by `attempts`;
#         • any other per-chunk failure increments chunks_failed and the document
#           terminates as 'partial' (never 'ready') with a log_crit.
# OFF → byte-identical legacy behaviour: every failure collapses to [] edges, the chunk
#       is counted as a clean success, and the document finalizes 'ready' with
#       chunks_failed=0 — i.e. silent loss (the defect).
DOC_CHUNK_FAILURE_LOUD = os.getenv(
    "DOC_CHUNK_FAILURE_LOUD", "true").strip().lower() not in ("false", "0", "no", "off")

# How many times one document may be re-pended after a brain-level outage before it is
# terminated as 'error' (its verbatim chunks are retained for a later re-mine). Bounds the
# at-least-once redelivery so a permanently-sick document cannot spin the drain forever.
#
# ⚠️ LEGACY-ONLY TERMINATOR (see _DOC_BRAIN_DEFER_MAX_AGE below). A COUNT of claims is a
# proxy for outage DURATION sized by the drain interval: at one claim per cycle, 5 attempts
# = ~5 minutes at REEMBED_INTERVAL=60 — and 95fb517b measured brains unavailable for HOURS
# (all day). The count bound therefore
# terminated not-their-fault uploads ~5 minutes into an hours-long outage. On migrated
# schemas the wall-clock bound below decides; this count remains the guard only where the
# brain_deferred_since column does not exist yet (pre-migration-264).
_DOC_MAX_ATTEMPTS = max(1, int(os.getenv("DOC_MAX_ATTEMPTS", "5")))


def _parse_interval_env(value: str) -> float:
    """Parse "<N> <unit>" interval env strings into SECONDS. Strict, never invents a value.

    Understands exactly the units this module's interval envs already use
    (DOC_CLAIM_LEASE "30 minutes", this bound's "30 days"): seconds, minutes, hours, days.
    Anything else raises — a bound nobody can parse must fail loudly at import, not
    silently become a different number than the operator wrote.
    """
    parts = str(value or "").strip().lower().split()
    if len(parts) != 2:
        raise ValueError(f"interval must be '<N> <unit>': {value!r}")
    n = float(parts[0])
    unit = parts[1]
    mult = {"second": 1.0, "seconds": 1.0, "minute": 60.0, "minutes": 60.0,
            "hour": 3600.0, "hours": 3600.0, "day": 86400.0, "days": 86400.0}.get(unit)
    if mult is None or n < 0:
        raise ValueError(f"unknown unit or negative count: {value!r}")
    return n * mult


# === THE WALL-CLOCK ANSWER to a question 95fb517b deliberately deferred ================
#
#   "How long may a document sit unprocessable (brain unavailable, deferral after deferral)
#    before it is genuinely the DOCUMENT's problem and terminates 'error'?"
#
# The answer is a DURATION, not a count, because every cause of "unprocessable" the system
# has actually observed is a duration:
#   • deploy/restart unavailability: minutes (6377e7c1: 1153/1166 chunk failures in a 6h
#     window were the worker beating on its own not-yet-listening backend);
#   • event-loop stalls: ≤ ~21 minutes measured (e3584a30; 32.4% of minutes had ZERO
#     health completions during that incident);
#   • daily-budget pacing: ≤ 24h per rolling window BY DESIGN — and deliberately FREE
#     (rate_deferred defers are refunded and never touch this clock; a user's upload must
#     not terminally fail behind a spent day);
#   • breaker-open / no-brain provider outages: the comment on _DOC_MAX_ATTEMPTS sized
#     them at MINUTES; the logs measured HOURS to ALL DAY.
#
# MAGNITUDE, DERIVED — not a round number that looked reasonable. The system has already
# reasoned, elsewhere, about how long USER CONTENT may sit unprocessed: the Class-C staged
# clock is 30 days (staged_facts.expires_at = now()+30d, migration 012's original intent)
# and the episodic re-mine poison-stamps retained turns older than 30 days. 30 days is the
# one constant this codebase has decided means "past this, unprocessed content is no longer
# 'in flight'". Aligning the document lane's deferral horizon with it means:
#   • it strictly exceeds EVERY observed deferral cause — including the worst ever seen
#     (an all-day brain outage) by a factor of ~30 — so a document blocked through no
#     fault of its own always outlives the outage that blocked it, drains when the brain
#     returns, and can never be terminated by an outage this system has actually seen;
#   • the silent-forever-pending failure the guard exists to close still closes, in the
#     same horizon the system already promised its users for unprocessed content.
# The clock measures the CURRENT CONTINUOUS no-progress episode: set (COALESCE) on the
# first non-paced brain-unavailable defer AND on an unproductive deadline requeue;
# cleared only when the document makes PROGRESS — a terminal finalize or a PRODUCTIVE
# deadline requeue (one whose owed-chunk count actually decreased). A CLAIM deliberately
# does not clear it (a claim is a pure DB write that succeeds mid-outage), so two short
# outages weeks apart, with no progress between them, still never exceed one episode —
# but any real progress resets the clock honestly. Both bounded lanes (brain-unavailable
# defer, owed-chunks deadline) measure the SAME thing: how long since this document
# last demonstrably moved.
_DOC_BRAIN_DEFER_MAX_AGE = os.getenv("DOC_BRAIN_DEFER_MAX_AGE", "30 days")
try:
    _DOC_BRAIN_DEFER_MAX_AGE_S = _parse_interval_env(_DOC_BRAIN_DEFER_MAX_AGE)
except ValueError as _iv:
    raise RuntimeError(
        f"DOC_BRAIN_DEFER_MAX_AGE={_DOC_BRAIN_DEFER_MAX_AGE!r} is not a parseable "
        f"<N> <unit> interval (e.g. '30 days') — refusing to guess a document-lifetime bound"
    ) from _iv

# How long a 'processing' claim may be held before another worker may RECLAIM it.
# Must comfortably exceed the worst-case drain of one document (chunks / concurrency ×
# per-chunk timeout) so a healthy slow document is never stolen mid-flight; 30 minutes is
# ~10x the observed worst case for a 200-chunk segment. Env-tunable, never hardcoded inline.
_DOC_CLAIM_LEASE = os.getenv("DOC_CLAIM_LEASE", "30 minutes")

# HTTP timeout for one doc chunk's /extract/rewrite call. Must sit ABOVE the backend's own
# worst case for a single chunk (REFRAME + retrying EXTRACT sub-chunks + post-passes), or a
# healthy-but-slow chunk is aborted client-side and recorded as a failure. Kept well under the
# claim lease so a stuck chunk still surfaces long before the document is reclaimed.
_DOC_CHUNK_HTTP_TIMEOUT = float(os.getenv("DOC_CHUNK_HTTP_TIMEOUT", "180"))

# HTTP timeout for one doc chunk's /ingest call. SAME RULE AS ABOVE, and it was violated:
# this was a bare `timeout=30.0` literal at both /ingest call sites while the endpoint's own
# worst case is far higher — /ingest runs the WGM gate, entity resolution, the occurrence
# classifier (an LLM op whose own budget is 45s) and the L4 build-out, all under the
# document lane's own chunk concurrency.
#
# MEASURED 2026-07-31 (DOCDRAIN, seat 8162c435, doc_id=9, two chunks): chunk 1 answered
# inside the window and reported committed=4; chunk 0's POST /ingest was aborted CLIENT-SIDE
# at 30s and the chunk was recorded failed — yet its six facts (rex/instance_of/beagle,
# carol/sibling_of, lives_in/toronto, works_for/shopify, …) landed in `facts` 30s later.
# So the work SUCCEEDED and we recorded a failure: facts_committed under-counted 4 vs 10 and
# the document terminated 'partial' (a TERMINAL status) with every fact actually in memory.
# A client timeout below the server's worst case does not prevent loss — it manufactures a
# phantom one and poisons the accounting the whole lane is judged by.
# Env-tunable, never a bare literal.
_DOC_INGEST_HTTP_TIMEOUT = float(os.getenv("DOC_INGEST_HTTP_TIMEOUT", "180"))

# Cheap liveness probe of the BACKEND (not the brain) before a document is claimed.
# See _backend_is_reachable / DOC_BACKEND_READY_PROBE below.
_DOC_BACKEND_PROBE_TIMEOUT = float(os.getenv("DOC_BACKEND_PROBE_TIMEOUT", "5"))

# === FLAG: DOC_BACKEND_READY_PROBE (DOCDRAIN, default ON) =====================
# ON  → the drain probes GET {backend}/health before claiming ANY document, and skips the
#       whole tenant for this cycle when the backend is not answering. Nothing is claimed,
#       no `attempts` are burned, no chunk is recorded failed: the documents stay 'pending'
#       and are drained on a later tick.
# OFF → legacy: claim first, discover the outage one chunk at a time.
#
# WHY IT DEFAULTS ON — this is the single biggest silent-loss source measured in this lane.
# The re_embedder runs INSIDE the API container, so on every deploy/restart its poll loop
# comes up BEFORE uvicorn is serving. Measured 2026-07-31 over one 6h window: 1153 of 1166
# chunk failures were `ConnectError: [Errno 111] Connection refused` — i.e. 99% of all
# document-lane chunk loss was the worker talking to its own not-yet-listening backend.
# Live proof of the damage (seat 2295c6ce, docs 1 and 2): claimed at 01:03:16.9, finalized
# 'partial' at 01:03:18.4 — 400 chunks "processed" in 1.5 SECONDS, chunks_failed=400,
# facts_committed=0, status TERMINAL. Two whole documents destroyed by a container restart,
# with a healthy-looking API and one log line. Skipping a cycle costs one poll interval;
# not skipping costs the user's corpus.
DOC_BACKEND_READY_PROBE = os.getenv(
    "DOC_BACKEND_READY_PROBE", "true").strip().lower() not in ("false", "0", "no", "off")


# Bound to the REAL exception classes at import, not looked up through the module attribute
# on every call: the drain's own test doubles replace `emb.httpx` wholesale, and a classifier
# that resolves its types through that attribute would raise AttributeError inside the very
# error path it exists to classify. The types themselves never change at runtime.
_UNREACHABLE_EXC_TYPES = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_HTTPX_ERROR_TYPE = httpx.HTTPError


def _is_backend_unreachable(exc: BaseException) -> bool:
    """Did this exception mean WE NEVER REACHED THE BACKEND AT ALL?

    The distinction is the whole ballgame for this lane's failure accounting:

      • connect-level (refused / DNS / connect timeout / pool exhaustion) → the request was
        never delivered. NOTHING was read, so this is not a property of the chunk — the same
        chunk will fail identically for every OTHER chunk in the document. Treating it as a
        per-chunk failure burns the entire document in one pass and terminates it 'partial'.
        The correct response is the deferral this lane already implements (Nygard,
        *Release It!* 2nd ed., "Circuit Breaker": while the remote end is unavailable the
        work is RETRIED after it recovers, not discarded).

      • read-level (the backend accepted the request and then didn't answer in time) → the
        request WAS delivered and may well have completed server-side. That stays a
        chunk-scoped, uncertain outcome and is handled where it is raised.

    Deterministic type/errno inspection — no string sniffing of a message that can change."""
    if isinstance(exc, _UNREACHABLE_EXC_TYPES):
        return True
    # A bare OSError can surface when the transport fails below httpx (e.g. a socket refused
    # while the event loop is tearing down). ECONNREFUSED/EHOSTUNREACH/ENETUNREACH/ENOTCONN.
    if isinstance(exc, (ConnectionRefusedError, ConnectionError)) and not isinstance(
            exc, _HTTPX_ERROR_TYPE):
        return True
    return False


# Backend states the pre-claim probe can distinguish. A boolean cannot express the difference
# between "the process is gone" and "the process is alive but its event loop is blocked", and on
# 2026-07-31 that conflation is exactly what let a production stall run 13 days unnoticed: DOWN and
# STALLED looked identical in the logs.
BACKEND_DOWN = "down"        # connect refused/unroutable -> nothing was delivered; defer is right
BACKEND_STALLED = "stalled"  # TCP connect SUCCEEDED, HTTP read timed out -> alive, loop blocked
BACKEND_READY = "ready"


def _backend_state(backend_url: str) -> str:
    """Three-state probe: DOWN / STALLED / READY. Deterministic, type-based, no string sniffing.

    WHY THREE STATES. The first cut of this returned a bool and caught `except Exception: return
    False`, collapsing connect-refused with read-timeout — even though `_is_backend_unreachable`
    THREE FUNCTIONS ABOVE draws exactly that distinction on a cited Nygard rationale. The probe did
    not use its own sibling. Measured consequence on prod: `/health` answers in 0.001s during ~67%
    of minutes and stalls for the rest (32.4% of minutes had ZERO health completions; queued probes
    released 42-at-once). A boolean read that as "unreachable" and could not say why.

    A TCP CONNECT THAT SUCCEEDS WHILE THE HTTP READ TIMES OUT IS POSITIVE PROOF the process is alive
    and its event loop is blocked — the cheapest exact signal we have for that condition, and one a
    boolean structurally cannot carry."""
    try:
        # Split the budget so the two failures are distinguishable at all: a short connect proves
        # reachability; the read is what a blocked loop starves.
        r = httpx.get(f"{backend_url}/health",
                      timeout=httpx.Timeout(connect=2.0, read=_DOC_BACKEND_PROBE_TIMEOUT,
                                            write=2.0, pool=2.0))
        return BACKEND_READY if 200 <= r.status_code < 300 else BACKEND_STALLED
    except Exception as exc:  # noqa: BLE001 — classified below, never swallowed silently
        if _is_backend_unreachable(exc):
            return BACKEND_DOWN
        # Connected, then no answer in time: alive but not serving.
        if isinstance(exc, (httpx.ReadTimeout, httpx.PoolTimeout)):
            # `_doc_log_crit(event, **fields)` takes KEYWORD fields only — this call used to pass
            # the message POSITIONALLY and raised TypeError on the very stall it was reporting
            # (gauntlet ingest-replay-loop-deadlock, 2026-09-15): the loud path was itself broken,
            # so a blocked backend loop surfaced as a probe crash, not a CRIT line. Pinned by
            # tests/test_doc_log_crit_loud_path.py.
            _doc_log_crit("doc_drain.backend_stalled",
                          probe_timeout_s=_DOC_BACKEND_PROBE_TIMEOUT,
                          note="backend accepted the connection but did not answer in time "
                               "- event loop likely blocked; deferring rather than burning chunks")
            return BACKEND_STALLED
        return BACKEND_DOWN


def _backend_is_reachable(backend_url: str) -> bool:
    """Boolean wrapper kept for existing call sites: READY is reachable, DOWN/STALLED are not.

    Deferring on STALLED is CORRECT — the re_embedder's own 60s timeouts during those windows prove
    the backend genuinely cannot serve then. ⚠️ KNOWN LIMIT, do not oversell this gate: the probe is
    a POINT-IN-TIME sample and measured stalls run up to ~21 minutes, far past the chunk timeout, so
    a document claimed in a healthy window can still burn chunks mid-flight. This mitigates; it does
    not make the doc lane safe against a blocked loop."""
    return _backend_state(backend_url) == BACKEND_READY


def _backend_is_reachable_legacy(backend_url: str) -> bool:
    """Superseded by _backend_state; retained only so the old shape is greppable."""
    try:
        r = httpx.get(f"{backend_url}/health", timeout=_DOC_BACKEND_PROBE_TIMEOUT)
        return 200 <= r.status_code < 300
    except Exception:
        return False


def _doc_log_crit(event: str, **fields) -> None:
    """FAIL LOUD for the document lane — a CRITICAL, greppable one-liner.

    `log` here is a STDLIB logger (not structlog), so src.api.logging_config.log_crit's
    kwargs-passthrough shape would raise TypeError; this renders the same structured
    key=value payload at CRITICAL through the logger the rest of this module uses."""
    _payload = " ".join(f"{k}={v}" for k, v in fields.items())
    log.critical(f"CRITICAL {event} log_level=CRIT {_payload}")


class DocumentBrainUnavailable(_errors.PublicRefusal, RuntimeError):
    """The tenant's extraction brain REJECTED the call (breaker open / endpoint dead).

    A RuntimeError subclass ON PURPOSE: the document worker already treats RuntimeError as
    "defer this whole document back to pending and stop the cycle" (the backend-freeze
    path), which is exactly the correct response to a tripped breaker — Nygard,
    *Release It!* (2nd ed.), "Circuit Breaker": while the breaker is open the caller must
    take the fallback path, and work must be retried after the breaker's reset timeout, not
    discarded. Reusing that existing deferral is deliberate: there is exactly ONE
    retry/deferral mechanism in this lane, not a second hand-rolled one."""


class DocumentChunkExtractionFailed(_errors.PublicRefusal):
    """This ONE chunk's extraction failed (transport error / degraded extractor response).

    Distinct from DocumentBrainUnavailable: the brain is up, this chunk did not come back.
    The chunk is counted failed (→ terminal status 'partial'), and its verbatim text stays
    in documents.chunks AND the episodic log for a later re-mine. It is never counted as a
    successfully-read chunk with no facts.

    `partial_edges` carries any edges that DID extract before the failure, so the caller can
    still commit them: dropping successfully-extracted facts in order to report a failure
    would be a second silent loss. Empty unless the extractor answered in part."""

    partial_edges: list = []

# ── Assistant-turn capture: THE INPUT LANE IS THE USER'S ──────────────────────
# When a document is a CONVERSATION transcript, its turns are role-prefixed
# ("user: ...", "assistant: ..."). BOTH the user's turns and the assistant's turns
# are the user's grounded session (owner-ratified correction of the earlier R3
# firewall): assistant content is user_stated too, correctable like any user fact.
# When this flag is ON, the document worker PARTITIONS each chunk by speaker role
# (so the assistant lane still inherits the shared date header for grounding) and
# ingests BOTH lanes as user_stated:
#   • user / unprefixed / header lines  → source="mcp"       → user_stated
#   • assistant lines                   → source="assistant" → user_stated (router
#     maps source="assistant" → user_stated, equal authority to the user's turns)
# When OFF (default) the chunk is ingested WHOLE with source="mcp" exactly as before
# (byte-identical legacy behavior; the measurement run flips this ON). A plain
# (non-conversational) document has no "assistant:" lines, so the partition is a
# no-op and its whole text still rides the user_stated lane whether the flag is on
# or off. Deterministic role split — no LLM, subject-agnostic. The TRUST FIREWALL
# (_decide_supersede_or_coexist) is UNCHANGED and still guards the ENGINE'S OWN
# growth (llm_learned/llm_inferred) — NOT chat content — from overriding user facts.
INGEST_ASSISTANT_TURNS = os.getenv(
    "INGEST_ASSISTANT_TURNS", "false").strip().lower() not in ("false", "0", "no", "off")

# The two structural line shapes — a turn line ("user: …"/"assistant: …") and a bracketed
# header line ("[Date: 2023-05-01]"). Defined ONCE in src/ingest/document_structure.py and
# imported here so the worker and the MCP chunker cannot drift apart (they previously held
# private copies of the same two regexes). Header lines are shared context, prepended to
# BOTH sub-extractions so assistant facts get dated too.
_ROLE_LINE_RE = _docstruct.ROLE_LINE_RE
_DOC_HEADER_RE = _docstruct.DOC_HEADER_RE

# ── DOC LANE: a lane that carries ONLY structural scaffolding is not text to extract ──────────
#
# `_partition_chunk_by_role` prepends the SHARED header lines ("[Date: 2023/05/22 (Mon) 18:21]")
# to BOTH lanes so assistant facts get dated too. For a chunk that happens to contain lines from
# only ONE speaker — which is the COMMON case, because the chunker splits on size, not on turn
# boundaries — the other lane comes back as the bare header and nothing else.
#
# MEASURED on the product's own chunks (2026-07-31, real 200-chunk document):
#   381 non-empty extraction lanes, of which **172 (45.1%) were header-only** — the literal
#   30-character string "[Date: 2023/05/22 (Mon) 18:21]".
# Every one of those 172 lanes then: called /harvest-spans (0 edges, by construction), logged
# `document_chunk_spine_empty`, and fell through to a full /extract/rewrite LLM call on a bare
# date. That is ~45% of this lane's extractor traffic spent on text containing no assertion —
# and under DOC_CHUNK_FAILURE_LOUD a timeout on one of those pointless calls is charged to the
# CHUNK, so a content-free lane could fail a chunk that had nothing to fail at.
#
# Nothing is lost by skipping it: the header is CONTEXT, and it is still prepended to the lane
# that does carry content (see `_partition_chunk_by_role`), so the date still reaches the facts.
# Deterministic (`DOC_HEADER_RE`, a whole line inside square brackets), subject-agnostic, no LLM.
# Rollback: DOC_CHUNK_SKIP_CONTENTLESS=false restores the byte-for-byte legacy behaviour.
DOC_CHUNK_SKIP_CONTENTLESS = os.getenv(
    "DOC_CHUNK_SKIP_CONTENTLESS", "true").strip().lower() not in ("false", "0", "no", "off")

# DOC LANE: pin the spine's DETERMINISTIC segmenter for a document chunk (per-REQUEST, via
# RewriteRequest.deterministic_segmentation) instead of the LLM atomizer.
#
# WHY the shape boundary is real: `reframe_to_atomic` is prompted for "one chat message" and
# budgeted as one — LLMMaxTokens REFRAME=256, LLMTimeouts REFRAME=6.0s. A document chunk is
# multi-sentence expository prose, 300-1500 chars, and its atom JSON does not fit in 256 tokens.
# Document prose is ALSO the better fit for the deterministic decomposer: it is well-formed and
# punctuated, with none of the disfluency the atomizer exists to absorb.
# Rollback: DOC_CHUNK_SPINE_DETSEG=false → the field is never sent → the backend's process-wide
# SPINE_DETERMINISTIC_SEGMENTATION decides, exactly as today.
DOC_CHUNK_SPINE_DETSEG = os.getenv(
    "DOC_CHUNK_SPINE_DETSEG", "true").strip().lower() not in ("false", "0", "no", "off")

# === FLAG: DOC_CHUNK_QUEUE (PARALLEL, default OFF) ============================
# ON  → a document's chunks become CLAIMED WORK ITEMS on a Redis queue
#       (src/ingest/chunk_queue.py) instead of a fire-and-wait ThreadPoolExecutor fan-out,
#       and three things become true that are not true today:
#         • a crashed/overrunning worker's chunk RETURNS TO THE QUEUE (visibility-timeout
#           lease + reap) instead of vanishing with the worker;
#         • a chunk's terminal state is RECORDED per chunk (documents.chunk_state,
#           migration 205), so a reclaim re-runs ONLY what is still owed — and a failed
#           chunk is RE-MINABLE instead of burned into a terminal 'partial';
#         • concurrency is resolved PER TENANT from that tenant's own measured endpoint
#           latency + rate limit, not from one global constant.
# OFF → byte-identical legacy behaviour: the ThreadPoolExecutor path below, one global
#       _DOC_CHUNK_CONCURRENCY, chunk_state never read or written.
#
# WHY IT IS OFF BY DEFAULT: this is a WRITE path, and the lane it accelerates is the first
# thing a new tenant does. It ships dark and is turned on per-deploy after measurement.
#
# DEGRADATION IS NOT OPTIONAL: if Redis is unreachable the flag is a no-op — chunk_queue
# .available() returns False and the legacy pool runs. A memory engine must never be harder
# to run than the accelerator bolted to it.
DOC_CHUNK_QUEUE = os.getenv(
    "DOC_CHUNK_QUEUE", "false").strip().lower() not in ("false", "0", "no", "off")

# How many chunk results are collected before `documents.chunk_state` is flushed to Postgres.
# Bounds the re-work a hard process loss can cost: at 8, a crash re-runs at most 8 already-done
# chunks instead of the whole document. Written on the MAIN thread only (a psycopg2 connection
# is not thread-safe — the pre-existing comment in _process_one_chunk says so, and it is right).
_DOC_CHUNK_STATE_FLUSH = max(1, int(os.getenv("DOC_CHUNK_STATE_FLUSH", "8")))


def _lane_has_no_content(text: str) -> bool:
    """True iff every non-blank line of `text` is a bracketed structural header.

    Deterministic and content-free by construction: it asks only whether a line is
    scaffolding, never what the line says."""
    for line in (text or "").splitlines():
        if line.strip() and not _DOC_HEADER_RE.match(line):
            return False
    return True


def _partition_chunk_by_role(chunk: str) -> tuple[str, str]:
    """Split a role-prefixed transcript chunk into (user_text, assistant_text).

    Deterministic, no LLM. Each line is bucketed by its speaker prefix:
      • "assistant: X"           → assistant bucket (content X, prefix stripped)
      • "user: X"                → user bucket
      • "[Date: ...]" / headers  → SHARED context (prepended to both buckets)
      • unprefixed / continuation → follows the CURRENT speaker (default: user)

    A plain document with no "assistant:" prefix yields (whole_text, "") so the
    caller's user lane is byte-identical to the legacy whole-chunk behavior.
    Shared header lines are prepended to the assistant text only when there IS
    assistant content, so temporal (event_date) grounding still applies to the
    assistant facts. Returns ("", "") only for an empty/whitespace chunk.
    """
    shared: list[str] = []
    user_lines: list[str] = []
    asst_lines: list[str] = []
    current = "user"  # unprefixed content defaults to the user lane
    for raw in (chunk or "").splitlines():
        m = _ROLE_LINE_RE.match(raw)
        if m:
            current = "assistant" if m.group(1).lower() == "assistant" else "user"
            content = m.group(2)
            (asst_lines if current == "assistant" else user_lines).append(content)
        elif _DOC_HEADER_RE.match(raw):
            # A header (date) resets the speaker context and is shared by both lanes.
            shared.append(raw.strip())
            current = "user"
        else:
            (asst_lines if current == "assistant" else user_lines).append(raw)
    user_text = "\n".join(shared + user_lines).strip()
    asst_text = "\n".join(shared + asst_lines).strip() if asst_lines else ""
    return user_text, asst_text


def _document_chunk_edges(chunk: str, user_id: str, backend_url: str,
                          statement_route: str) -> list:
    """LLM-PRIMARY extraction for ONE document chunk (the configured LLM).

    Documents are dense DOMAIN prose that the deterministic spine SYSTEMATICALLY
    mis-parses — and a mis-parse yields JUNK edges, not zero. That is exactly why the
    old residual-only hybrid was broken for the case this lane exists to fix: a
    mis-POS'd sentence produced non-zero junk, so it never fell into the residual set
    and the LLM never re-covered it → the junk was kept while the real facts were LOST
    (live proof: "Photosynthesis converts carbon dioxide and water into glucose and
    oxygen" — the small parser mis-POS-tags "converts" as a noun and the spine emits
    (dioxide, use, light energy) + (water, use, light energy); those are edges, so the
    sentence is not residual → the LLM never fires → glucose/oxygen never captured).

    ⚠️ THE TWO PARAGRAPHS ABOVE DESCRIBE A LANE THAT NO LONGER EXISTS — kept because the
    Photosynthesis failure they record is real and still un-fixed, not because the ruling
    they end in still holds. `DOC_CHUNK_SPINE_FIRST` (2026-07-31) made the deterministic
    spine the FIRST extractor here, so "the spine is NOT trusted / LLM-only" is now false
    of this function. Read the DOC_CHUNK_SPINE_FIRST block below for what actually runs;
    the union objection is still the reason there is no UNION — spine edges win outright or
    the rewrite runs alone, never both. `statement_route` is retained for caller/signature
    stability, but the document lane's route is decided here, not by the brain's STATEMENT
    route.

    The extraction runs in the backend on its configured LLM (the lane header keeps it
    in the BACKGROUND lane). A frozen backend (ingest_disabled) RAISES so the worker
    defers the chunk instead of dropping it. Low-confidence edges are dropped (WGM-gate
    hygiene, same filter the MCP applies). Provenance is left ALONE: document facts are
    user-submitted, so /ingest source="mcp" → user_stated (durable Class A/B), unlike
    the episodic-reextract backfill which forces llm_inferred.

    DOCLOSS-B — A FAILED EXTRACTION IS NOT AN EMPTY ONE. This function used to swallow every
    exception and return [], and /extract/rewrite answered 200 with edges=[] when its LLM call
    was rejected by an OPEN circuit breaker. The two were therefore indistinguishable, so a
    chunk the extractor NEVER READ was tallied as a clean chunk with no facts and the document
    still finalized 'ready'. Under DOC_CHUNK_FAILURE_LOUD the failure is now raised:
    DocumentBrainUnavailable for a brain-level outage (→ the whole document is re-pended and
    retried, at-least-once) and DocumentChunkExtractionFailed for a single-chunk failure
    (→ chunks_failed++, terminal status 'partial'). Nothing is EVER counted as read-and-empty
    unless the extractor actually answered."""
    # A lane that is nothing but structural scaffolding (a bare "[Date: …]" header) has no
    # assertion in it. Skip BOTH extractors rather than burn an LLM call — and, more importantly,
    # rather than let a timeout on that pointless call be charged to the chunk. See
    # DOC_CHUNK_SKIP_CONTENTLESS. Returning [] here is a TRUE read-and-empty: we read the lane
    # deterministically and there was nothing in it, which is exactly what [] means on this path.
    if DOC_CHUNK_SKIP_CONTENTLESS and _lane_has_no_content(chunk):
        log.debug(f"re_embedder.document_chunk_contentless user_id={user_id[:8]} "
                  f"(header-only lane — no extractor call)")
        return []
    try:
        # CLIENT TIMEOUT MUST EXCEED THE SERVER'S OWN WORST CASE, or we kill work that would
        # have succeeded and record it as a chunk failure.
        #
        # One chunk costs, server-side: REFRAME (6s) + per-sub-chunk EXTRACT (30s) with up to
        # 3 retries in call_llm_with_retry_async — ~90s+ for a single retrying sub-chunk,
        # before the completeness pass, the GLiNER2 lanes and two synchronous psycopg2
        # connects made from inside the endpoint. The old 60s was BELOW that floor, so a
        # healthy-but-slow chunk was aborted client-side and counted as failed, even though
        # the backend usually finished and CACHED the result moments later.
        # Measured 2026-07-30: 30 ReadTimeouts in six minutes on a 128-chunk document while
        # the provider itself answered in ~1s.
        # Env-tunable, never a bare literal (the 60.0 above was exactly that).
        # SPINE FIRST — the document lane was the ONLY ingest path that never tried the
        # deterministic extractor. `_reextract_row_edges` (this same file, ~:1917) has done
        # spine-then-rewrite since it shipped; the document chunk path went straight to the
        # LLM. Consequence, measured 2026-07-31: with the bench in DOCUMENT mode, the whole
        # benchmark exercised /extract/rewrite and NEVER the spine — so every spine capture
        # lane (counts, durations, label-anaphora, temporal units) was invisible to it, and
        # the run inherited the LLM's full cost, latency and run-to-run variance. 2205
        # `document_rewrite_failed: timed out` in five minutes against a local brain.
        # Same shape as the episodic path: try the spine, fall back on nothing/error. The
        # provenance difference is deliberate and PRESERVED — document facts stay
        # source="mcp" -> user_stated, unlike the episodic backfill which forces llm_inferred.
        # Rollback: DOC_CHUNK_SPINE_FIRST=false restores the byte-for-byte legacy path.
        _spine_first = os.getenv("DOC_CHUNK_SPINE_FIRST", "true").strip().lower() \
            not in ("false", "0", "no")
        if _spine_first:
            try:
                _sbody = {"text": chunk, "user_id": user_id}
                if DOC_CHUNK_SPINE_DETSEG:
                    # THE SHAPE BOUNDARY. The caller knows this text is a document chunk, not a
                    # chat turn, so it pins the deterministic segmenter for THIS request. Flag
                    # off → the field is absent → the backend's own flag decides (legacy).
                    _sbody["deterministic_segmentation"] = True
                _sresp = httpx.post(
                    f"{backend_url}/harvest-spans",
                    json=_sbody,
                    headers={**_BACKEND_LANE_HEADERS, **_backend_auth_headers()},
                    timeout=_DOC_CHUNK_HTTP_TIMEOUT,
                )
                _sresp.raise_for_status()
                _sdata = _sresp.json()
                # Same split-brain guard the episodic path uses: a FROZEN backend answers
                # with zero edges, which must never be recorded as "read and empty".
                if _sdata.get("status") == "ingest_disabled":
                    raise DocumentBrainUnavailable(
                        "backend ingest disabled (knowledge-store mode)")
                if _sdata.get("status") == "doc_lane_paused":
                    # Operator pause (the fairness knob): same class as a brain outage for
                    # THIS lane — defer the document, burn nothing. Never charged to a chunk.
                    raise DocumentBrainUnavailable(
                        "document CPU lane paused by operator (doc_lane_paused)")
                _sedges = [e for e in (_sdata.get("edges", []) or [])
                           if not e.get("low_confidence", False)]
                if _sedges:
                    return _sedges
                log.info(f"re_embedder.document_chunk_spine_empty user_id={user_id[:8]} "
                         f"(falling back to /extract/rewrite)")
            except DocumentBrainUnavailable:
                raise
            except Exception as _se:  # noqa: BLE001 — fall back, never lose the chunk
                # DOCDRAIN: a CONNECT-level failure is not a spine problem — the backend is
                # not there. /extract/rewrite lives on the SAME host, so falling back only
                # buys a second identical refusal and then charges it to the chunk. Defer the
                # whole document instead (bounded at-least-once redelivery).
                if _is_backend_unreachable(_se):
                    raise DocumentBrainUnavailable(
                        f"backend unreachable at {backend_url}: {type(_se).__name__}") from _se
                log.warning(f"re_embedder.document_chunk_spine_failed user_id={user_id[:8]} "
                            f"(falling back to /extract/rewrite): {_se!r}")
        resp = httpx.post(
            f"{backend_url}/extract/rewrite",
            json={"text": chunk, "user_id": user_id, "force_relation_extraction": True},
            headers={**_BACKEND_LANE_HEADERS, **_backend_auth_headers()},
            timeout=_DOC_CHUNK_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        _data = resp.json()
        if _data.get("status") == "ingest_disabled":
            raise RuntimeError("backend ingest disabled (knowledge-store mode)")
        if _data.get("status") == "doc_lane_paused":
            # Operator pause on the rewrite door too — defer, never a chunk failure.
            raise DocumentBrainUnavailable(
                "document CPU lane paused by operator (doc_lane_paused)")
        if DOC_CHUNK_FAILURE_LOUD and _data.get("extraction_degraded"):
            # The backend told us how much of THIS chunk it could not read (the
            # EXTRACTION_FAILURE_SURFACED envelope). A breaker rejection is a brain-level
            # outage → defer the document; anything else is a single-chunk failure.
            _reason = str(_data.get("error") or "").strip() or str(
                ((_data.get("failures") or [{}])[0] or {}).get("reason") or "extraction_degraded")
            _n_failed = int(_data.get("chunks_failed") or 0)
            _n_total = int(_data.get("chunks_total") or 0)
            log.warning(f"re_embedder.document_extraction_degraded user_id={user_id[:8]} "
                        f"failed={_n_failed}/{_n_total} reason={_reason}")
            if "circuit_breaker_open" in _reason or "rate_deferred" in _reason:
                # rate_deferred: the brain is MERELY PACED (background daily budget /
                # minute tokens spent). This is not a chunk failure — the document goes
                # back for bounded at-least-once redelivery instead of terminating
                # 'partial' with zero facts on a day the pacer is deliberately parking
                # background work (critic pass 2: a user's upload must never terminally
                # fail behind a spent day).
                raise DocumentBrainUnavailable(_reason)
            # PARTIAL degradation: some sub-chunks answered. KEEP those edges — discarding
            # successfully-extracted facts in order to report a failure would be a second,
            # smaller silent loss. They ride ON the exception so the caller ingests them
            # AND still counts the chunk failed.
            _err = DocumentChunkExtractionFailed(
                f"{_reason} ({_n_failed}/{_n_total} sub-chunks unread)")
            _err.partial_edges = [e for e in (_data.get("edges", []) or [])
                                  if not e.get("low_confidence", False)]
            raise _err
        # FAIL CLOSED on any non-success extractor status.
        #
        # /extract/rewrite has a second empty-shaped exit: its outer `except` returns
        # {"status": "error", "edges": [], "error": ...} WITHOUT the extraction_degraded
        # envelope. That matched neither branch above and fell through to "return the edges"
        # → [] → the chunk was counted as a CLEAN SUCCESS, chunks_failed was not incremented,
        # and the document finalized `ready` having stored nothing from it.
        #
        # Same class as the fenced-JSON breaker bug, one door over: the worker was taught
        # about `extraction_degraded`, but the endpoint kept an error path that never sets it.
        # Treat ANY unrecognized status as a failure rather than assuming success — an empty
        # edge list is only trustworthy when the extractor says it succeeded.
        _status = _data.get("status")
        if DOC_CHUNK_FAILURE_LOUD and _status not in (None, "success"):
            raise DocumentChunkExtractionFailed(
                f"extractor_status={_status}: {str(_data.get('error') or '')[:160]}")
        return [e for e in (_data.get("edges", []) or [])
                if not e.get("low_confidence", False)]
    except (DocumentChunkExtractionFailed, RuntimeError):
        # RuntimeError covers the freeze signal AND DocumentBrainUnavailable (a subclass).
        raise
    except Exception as e:
        log.warning(f"re_embedder.document_rewrite_failed user_id={user_id[:8]}: {e}")
        # DOCDRAIN — WHO FAILED DECIDES WHAT WE DO. A connect-level error means the request
        # was never delivered: it says nothing about THIS chunk and everything about the
        # backend, so charging it to the chunk destroys the document one chunk at a time
        # (measured: 400 chunks burned in 1.5s across two documents during a deploy restart).
        # Defer the document instead — the same bounded redelivery the breaker path uses.
        if _is_backend_unreachable(e):
            # No URL in the sentence: this message is persisted into documents.error and rendered
            # by GET /documents/status — topology goes to the seam's CRIT line under the ref.
            raise DocumentBrainUnavailable(
                "backend unreachable: " + _errors.public_detail(
                    e, where="doc.chunk.backend_unreachable", what=type(e).__name__)) from e
        if DOC_CHUNK_FAILURE_LOUD:
            # Transport/HTTP failure: the chunk was NOT read. Never report it as empty.
            raise DocumentChunkExtractionFailed(_errors.public_detail(
                e, where="doc.chunk.transport", what=type(e).__name__)) from e
        return []


def _process_document_chunk(chunk, *, idx, doc_id, user_id: str, backend_url: str,
                            source_ref, statement_route: str = "rewrite") -> tuple:
    """Episodic-retain → extract → WGM-ingest ONE document chunk.

    Returns ``(committed, staged, failed, reason)`` — ``reason`` is the REASON CODE for a
    failed chunk (the DOCLOSS-B evidence this lane used to discard: extractor status, transport
    error class, degraded-envelope reason), or ``None`` on success. It is persisted into
    ``documents.chunk_state`` by BOTH lanes so a terminated-'partial' document can say WHY
    each unread chunk failed and the retry lane can name what it re-owes.

    LIFTED VERBATIM out of the nested `_process_one_chunk` closure so that the SAME code runs
    on both lanes — the legacy ThreadPoolExecutor fan-out and the queue worker. A second copy
    would drift, and this lane has already paid for drift once (three copies of the tool
    descriptions). Nothing about its behaviour changed in the lift.

    RAISES RuntimeError (incl. DocumentBrainUnavailable) on a backend freeze / brain outage so
    the caller aborts the whole document → re-pending. Thread-safe: no shared mutable state,
    every network call is per-invocation, and the worker's db_conn is NOT touched here."""
    if not isinstance(chunk, str) or not chunk.strip():
        return (0, 0, 0, None)
    # Verbatim episodic retention BEFORE extraction (the safety net that
    # feeds the reextract backfill for anything still uncastable). Non-fatal.
    try:
        httpx.post(
            f"{backend_url}/episodic/append",
            json={"user_id": user_id, "raw_text": chunk,
                  "source": "document", "source_ref": source_ref,
                  "intent": None, "extracted_fact_count": None},
            headers=_backend_auth_headers(),
            timeout=5.0,
        )
    except Exception:
        pass

    def _extract_and_ingest(text: str, source: str) -> tuple:
        """Extract edges from `text` and ingest them under `source`
        (which the backend maps to fact_provenance). Returns
        (committed, staged). RAISES RuntimeError on a backend freeze."""
        if not text or not text.strip():
            return (0, 0)
        try:
            _edges = _document_chunk_edges(text, user_id, backend_url, statement_route)
        except DocumentChunkExtractionFailed as _cf:
            # DOCLOSS-B: the chunk FAILED, but any edges that DID extract are still
            # the user's facts — commit them, then re-raise so the chunk is counted
            # failed and the document terminates 'partial'. Losing good edges to
            # report a failure would just be a smaller silent loss.
            _salvage = list(getattr(_cf, "partial_edges", None) or [])
            if _salvage:
                try:
                    httpx.post(
                        f"{backend_url}/ingest",
                        json={"text": text, "user_id": user_id, "edges": _salvage,
                              "source": source, "source_ref": source_ref},
                        headers={**_BACKEND_LANE_HEADERS, **_backend_auth_headers()},
                        timeout=_DOC_INGEST_HTTP_TIMEOUT,
                    ).raise_for_status()
                except Exception as _se:
                    log.warning(f"re_embedder.document_salvage_ingest_failed "
                                f"user_id={user_id[:8]}: {_se}")
            raise
        if not _edges:
            return (0, 0)
        try:
            _resp = httpx.post(
                f"{backend_url}/ingest",
                json={"text": text, "user_id": user_id, "edges": _edges,
                      "source": source, "source_ref": source_ref},
                headers={**_BACKEND_LANE_HEADERS, **_backend_auth_headers()},
                timeout=_DOC_INGEST_HTTP_TIMEOUT,
            )
        except Exception as _ie:
            if _is_backend_unreachable(_ie):
                raise DocumentBrainUnavailable(
                    f"backend unreachable at {backend_url} during /ingest: "
                    f"{type(_ie).__name__}") from _ie
            # A READ timeout here is an UNCERTAIN outcome, NOT a clean failure:
            # the edges were extracted AND delivered, and /ingest very often
            # commits them anyway (proven — DOCDRAIN doc_id=9: aborted at 30s,
            # six facts in `facts` 30s later). Say so LOUDLY and exactly, so the
            # next reader does not re-derive it from a bare "timed out", and so
            # nobody reads the resulting 'partial' as "nothing was stored".
            _doc_log_crit(
                "re_embedder.document_ingest_outcome_unknown",
                user_id=user_id[:8], source=source, edges=len(_edges),
                timeout_seconds=_DOC_INGEST_HTTP_TIMEOUT,
                error=f"{type(_ie).__name__}: {str(_ie)[:120]}",
                note="edges_were_DELIVERED-facts_may_have_committed_server_side"
                     "-counted_failed_conservatively")
            raise DocumentChunkExtractionFailed(
                f"ingest outcome unknown ({type(_ie).__name__} after "
                f"{_DOC_INGEST_HTTP_TIMEOUT}s)") from _ie
        _resp.raise_for_status()
        _j = _resp.json()
        if _j.get("status") == "ingest_disabled":
            raise RuntimeError("backend ingest disabled (knowledge-store mode)")
        return (int(_j.get("committed", 0) or 0), int(_j.get("staged", 0) or 0))

    try:
        if INGEST_ASSISTANT_TURNS:
            # Split by speaker. DOCUMENT-TIERED (owner direction 2026-08-21): BOTH partitions
            # are machine readings of a user-supplied DOCUMENT, so both ingest under
            # source="document" → provenance llm_inferred, staged Class B (never A — an
            # uploaded file must not outrank the user's own conversational corrections;
            # promotes on confirmation). The legacy "mcp"/"assistant" split presumed the
            # document lane was conversational-attested; it is content-attested instead.
            _user_text, _asst_text = _partition_chunk_by_role(chunk)
            _uc, _us = _extract_and_ingest(_user_text, "document")
            _ac, _as = _extract_and_ingest(_asst_text, "document")
            return (_uc + _ac, _us + _as, 0, None)
        # Legacy (flag OFF): whole chunk → same document tier.
        _c, _s = _extract_and_ingest(chunk, "document")
        return (_c, _s, 0, None)
    except RuntimeError:
        # Split-brain freeze: backend refused. Propagate → caller re-pends the doc.
        raise
    except DocumentChunkExtractionFailed as ce:
        # THE REASON CODE ESCAPES HERE (was: collapsed into a bare `failed=1`). The drain's
        # per-chunk ledger persists it so a partial document can name WHY each chunk failed
        # (the Aug-8 docs 3/4/7 evidence loss — counts survived, reasons did not).
        log.warning(f"re_embedder.document_chunk_failed doc_id={doc_id} "
                    f"chunk={idx} user_id={user_id[:8]}: {str(ce)[:200]}")
        # ``DocumentChunkExtractionFailed`` is a PublicRefusal: its sentence is authored (the
        # reason CODE), so the seam passes it through; it is what document_status renders.
        return (0, 0, 1, _errors.public_detail(ce, where="doc.chunk.failed"))
    except Exception as ce:
        log.warning(f"re_embedder.document_chunk_failed doc_id={doc_id} "
                    f"chunk={idx} user_id={user_id[:8]} (non-fatal): {ce}")
        return (0, 0, 1, _errors.public_detail(ce, where="doc.chunk.unexpected", what=type(ce).__name__))


# ── PARALLEL: the queue lane ─────────────────────────────────────────────────
#
# The handler is MODULE-LEVEL and its item carries everything it needs (text, backend url,
# user, source_ref). That is what makes a worker in ANOTHER PROCESS possible later without
# restructuring anything: nothing here closes over the drain's local state.


def _run_doc_chunk_item(item):
    """chunk_queue handler for kind='doc_chunk'. Returns (committed, staged, failed).

    Raises FatalBatchError for a brain-level outage so the batch STOPS and the item goes back
    to the queue WITHOUT consuming an attempt — an outage is not a property of the chunk, and
    charging it to the chunk is exactly how 400 chunks were burned in 1.5 seconds."""
    from src.ingest.chunk_queue import FatalBatchError
    p = item.payload or {}
    try:
        return _process_document_chunk(
            p.get("text"), idx=p.get("idx"), doc_id=p.get("doc_id"),
            user_id=p.get("user_id") or item.tenant,
            backend_url=p.get("backend_url"), source_ref=p.get("source_ref"),
            statement_route=p.get("statement_route") or "rewrite")
    except RuntimeError as fe:      # freeze / DocumentBrainUnavailable (a RuntimeError)
        raise FatalBatchError(str(fe), cause=fe) from fe


def _register_doc_chunk_handler() -> bool:
    """Register the doc-chunk handler. False when src/ingest/chunk_queue.py is unavailable."""
    try:
        from src.ingest import chunk_queue as _cq
        _cq.register_handler("doc_chunk", _run_doc_chunk_item)
        return True
    except Exception as e:  # noqa: BLE001 — the accelerator never breaks its host
        log.info(f"re_embedder.chunk_queue_unavailable ({type(e).__name__}) — legacy pool")
        return False


def _read_chunk_state(db_conn, doc_id, schema_name) -> dict:
    """The per-chunk terminal ledger for one document, or {}. Never raises.

    Its own guarded SELECT rather than an extra RETURNING column on the claim: a tenant that
    has not taken migration 205 yet must keep draining, and the claim UPDATE is the one
    statement in this lane that absolutely must not acquire a new failure mode."""
    try:
        with db_conn.cursor() as cur:
            cur.execute("SELECT chunk_state FROM documents WHERE id = %s", (doc_id,))
            row = cur.fetchone()
        db_conn.commit()
        val = row[0] if row else None
        if isinstance(val, str):
            val = json.loads(val)
        return dict(val or {})
    except Exception as e:  # noqa: BLE001 — pre-205 schema, or anything else: no ledger
        log.info(f"re_embedder.chunk_state_unavailable doc_id={doc_id} "
                 f"({type(e).__name__}) — every chunk is treated as owed")
        _rollback_and_reapply_search_path(db_conn, schema_name)
        return {}


def _drain_document_via_queue(db_conn, doc_id, chunks, chunk_state: dict, *, user_id: str,
                              backend_url: str, source_ref, statement_route: str,
                              schema_name) -> tuple:
    """Drain ONE document's still-owed chunks through the Redis work queue.

    Returns ``(committed, staged, chunks_failed, state, fatal)`` where ``state`` is the merged
    per-chunk ledger and ``fatal`` is a RuntimeError to re-raise (brain outage) or None.

    THE TWO DURABILITY LAYERS, both live here:
      • Postgres `chunk_state` says what is still OWED — so a reclaim re-runs only that, and a
        chunk that failed is re-minable rather than burned.
      • The Redis lease says what is IN FLIGHT — so a worker that dies mid-chunk has its work
        returned to the queue rather than lost with it.
    """
    from src.ingest import chunk_queue as _cq

    state = dict(chunk_state or {})
    batch = f"doc:{doc_id}"
    queue = _cq.RedisWorkQueue()
    # A previous crashed run may have left residue for THIS batch. Purge before enqueueing so
    # a stale item cannot be processed alongside the fresh one (the DOCUMENT claim already
    # guarantees a single owner, so this is scoped and safe).
    queue.purge(user_id, batch)

    owed = [(i, c) for i, c in enumerate(chunks)
            if (state.get(str(i)) or {}).get("s") not in ("done", "failed")]
    if not owed:
        return (0, 0, 0, state, None)

    queue.enqueue([
        _cq.WorkItem(kind="doc_chunk", tenant=user_id, batch=batch, key=f"chunk:{i}",
                     payload={"idx": i, "text": c, "doc_id": doc_id, "user_id": user_id,
                              "backend_url": backend_url, "source_ref": source_ref,
                              "statement_route": statement_route})
        for i, c in owed
    ])

    conc, reason = _cq.resolve_tenant_concurrency(user_id, _DOC_CHUNK_CONCURRENCY)
    conc = min(conc, max(1, len(owed)))
    log.info(f"re_embedder.document_queue_drain doc_id={doc_id} user_id={user_id[:8]} "
             f"owed={len(owed)}/{len(chunks)} concurrency={conc} ({reason})")

    tally = {"committed": 0, "staged": 0, "failed": 0, "since_flush": 0}

    def _flush(force: bool = False) -> None:
        """Persist the ledger. MAIN THREAD ONLY — psycopg2 connections are not thread-safe."""
        if not force and tally["since_flush"] < _DOC_CHUNK_STATE_FLUSH:
            return
        tally["since_flush"] = 0
        try:
            with db_conn.cursor() as cur:
                cur.execute("UPDATE documents SET chunk_state = %s::jsonb WHERE id = %s",
                            (json.dumps(state), doc_id))
            db_conn.commit()
        except Exception as e:  # noqa: BLE001 — the ledger is an OPTIMISATION, never the truth
            log.warning(f"re_embedder.chunk_state_flush_failed doc_id={doc_id}: {e}")
            _rollback_and_reapply_search_path(db_conn, schema_name)

    def _on_result(item, outcome, value):
        idx = str((item.payload or {}).get("idx"))
        if outcome == "ok":
            # 4-tuple (committed, staged, failed, reason) since the reason-code persistence
            # pass — tolerate the 3-tuple from an in-flight older worker for robustness.
            if isinstance(value, tuple) and len(value) >= 4:
                c, s, f = value[0], value[1], value[2]
                _reason = value[3]
            else:
                c, s, f = value
                _reason = None
            tally["committed"] += c
            tally["staged"] += s
            tally["failed"] += f
            # `f == 1` is a chunk the extractor could not read (it did not raise, it reported).
            # Record it as FAILED-terminal WITH its reason code: it has already consumed its own
            # retry budget inside _document_chunk_edges, and re-queueing it would spin.
            state[idx] = ({"s": "done", "c": c, "g": s} if not f
                          else {"s": "failed", "a": item.attempts + 1,
                                "e": (_errors.public_detail(_reason, where="doc.chunk.reason", what=type(_reason).__name__)
                                      if isinstance(_reason, BaseException) else str(_reason or "unread")[:200])})
        elif outcome == "dead":
            tally["failed"] += 1
            # ``value`` may be the exception OBJECT the queue handed back (chunk_queue._run_one)
            # — this row is rendered by GET /documents/status, so it gets the public shape.
            state[idx] = {"s": "failed", "a": item.attempts + 1,
                          "e": (_errors.public_detail(value, where="doc.chunk.dead", what=type(value).__name__)
                                if isinstance(value, BaseException) else str(value)[:200])}
            _doc_log_crit("re_embedder.chunk_dead_lettered", doc_id=doc_id,
                          user_id=user_id[:8], chunk=idx, attempts=item.attempts + 1,
                          error=str(value)[:200],
                          note="verbatim_text_retained_in_documents.chunks+episodic_for_remine")
        elif outcome == "failed":
            # Requeued with attempts+1 — NOT terminal, NOT counted. This is the burned-chunk
            # fix: the chunk is still owed and will be claimed again.
            log.info(f"re_embedder.chunk_requeued doc_id={doc_id} chunk={idx} "
                     f"attempt={item.attempts + 1}: {str(value)[:120]}")
            return
        elif outcome == "fatal":
            return          # already back on the queue; the batch is stopping
        else:               # nohandler — a registration bug, not a data problem
            tally["failed"] += 1
            state[idx] = {"s": "failed", "a": item.attempts + 1, "e": "no handler"}
        tally["since_flush"] += 1
        _flush()

    # READ BARRIER — THE DURATION-DEPENDENT WOUND (TX2). run_batch is the long pass: it
    # can run for tens of minutes over a many-chunk document, and the incremental `_flush`
    # below is what keeps the evidence ledger alive. If this connection enters run_batch
    # inside a transaction, `idle_in_transaction_session_timeout` kills it mid-batch — every
    # later flush AND the terminal finalize then fail on a dead connection, which is exactly
    # how a long document is left stranded at status='processing' with chunk_state={} and
    # chunks_failed=0 while its chunks demonstrably failed in the logs.
    release_read_transaction(db_conn, context=f"re_embedder.document_queue_drain doc_id={doc_id}")
    result = _cq.run_batch(queue, user_id, batch, conc, on_result=_on_result)
    _flush(force=True)

    fatal = None
    if result.get("fatal"):
        err = result.get("fatal_error")
        cause = getattr(err, "cause", None)
        fatal = cause if isinstance(cause, RuntimeError) else RuntimeError(str(err))
    else:
        queue.purge(user_id, batch)

    return (tally["committed"], tally["staged"], tally["failed"], state, fatal)


def drain_pending_documents(db_conn, backend_url: str, user_id: str,
                            schema_name: str = None, statement_route: str = "rewrite",
                            batch_size: int = _DOC_DRAIN_BATCH) -> int:
    """Drain pending rows from the per-tenant `documents` registry (async lane).

    For each pending document: claim it (pending → processing, atomic), run the
    per-chunk HYBRID extraction (_document_chunk_edges) and ingest through the WGM
    gate (source="mcp" → user_stated, durable), then flip status → 'ready' with the
    per-chunk tallies. Per-chunk failure NEVER aborts the document (chunks_failed++);
    a catastrophic failure marks the row 'error' with the message so it stops being
    re-claimed (the chunks JSONB is retained verbatim for a future re-mine).

    Runs INSIDE the caller's INGEST_ENABLED guard (a frozen store never drains, and
    the gated backend would refuse the /ingest anyway). Per-tenant error isolation
    mirrors reextract_episodic: any failure rolls back this tenant's connection and
    returns; it never poisons the loop. Returns the count of documents finalized
    (ready or error) this cycle."""
    finalized = 0
    try:
        if schema_name:
            try:
                with db_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {schema_name}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema from UUID-derived source with validation
                db_conn.commit()
            except Exception as e:
                log.warning(f"re_embedder.document_search_path_failed schema={schema_name}: {e}")

        # Migration 264 capability, resolved ONCE per drain call: pre-264 schemas have no
        # documents.brain_deferred_since, and the finalize / deadline-requeue UPDATEs below
        # name it. They CANNOT fall back via except-and-retry the way the claim does — an
        # exception inside that try is what marks a document status='error', so an
        # unmigrated schema would turn every healthy finalize into a false terminal error.
        # One catalog probe keeps both sites branchable with no mid-flight abort.
        _has_defer_clock = False
        try:
            with db_conn.cursor() as cur:
                # Prefer the EXPLICIT tenant schema over current_schema(): the SET
                # search_path above can fail (logged, not raised), and a probe that
                # then read current_schema() would silently grade a MIGRATED tenant
                # as pre-264 and fall it back to the count bound (critic round 3,
                # latent). schema_name is bound as a PARAMETER, never interpolated.
                if schema_name:
                    cur.execute(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema = %s "
                        "AND table_name = 'documents' "
                        "AND column_name = 'brain_deferred_since'",
                        (schema_name,))
                else:
                    cur.execute(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = 'documents' "
                        "AND column_name = 'brain_deferred_since'")
                _has_defer_clock = cur.fetchone() is not None
                # Same probe for `chunk_state` (migration 205): the legacy-lane ledger write in
                # the terminal finalize names the column, and an UndefinedColumn there would
                # abort the finalize and mis-mark a healthy document 'error' (the exact failure
                # mode the defer-clock probe above was added to prevent).
                if schema_name:
                    cur.execute(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema = %s "
                        "AND table_name = 'documents' "
                        "AND column_name = 'chunk_state'",
                        (schema_name,))
                else:
                    cur.execute(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = 'documents' "
                        "AND column_name = 'chunk_state'")
                _has_chunk_state = cur.fetchone() is not None
        except Exception:
            _has_defer_clock = False
            _has_chunk_state = False

        # Fetch a modest batch of pending docs (oldest first).
        try:
            with db_conn.cursor() as cur:
                # Also RECLAIM documents stranded in 'processing' past the lease.
                #
                # Nothing else in the codebase ever reads a 'processing' row back: the claim
                # flips pending -> processing, and only the finalize UPDATE moves it on. So if
                # the worker dies between those two points the row is stranded FOREVER — never
                # re-claimed, never terminal. The trigger is routine: a container restart mid
                # drain, or a db_conn that dies before finalize (in which case the outer
                # handler tries to write status='error' on the same broken connection, fails,
                # and swallows it).
                #
                # User-visible effect: /documents/pending counts ('pending','processing'), so
                # recall tells the user "still importing…" forever and the document's facts
                # never land. An entire document lost, silently, with a healthy-looking API.
                #
                # This deliberately reuses the EXISTING bounded-redelivery machinery rather
                # than adding a second one: the claim below bumps `attempts`, and
                # _DOC_MAX_ATTEMPTS already terminates a document that cannot make progress,
                # so a poisoned row is reaped a bounded number of times and then fails LOUD.
                cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                    """
                    SELECT id FROM documents
                    WHERE status = 'pending'
                       OR (status = 'processing'
                           AND started_at IS NOT NULL
                           AND started_at < now() - %s::interval)
                    ORDER BY created_at ASC
                    LIMIT %s
                    """,
                    (_DOC_CLAIM_LEASE, batch_size),
                )
                doc_ids = [r[0] for r in cur.fetchall()]
            # READ BARRIER: the backend reachability probe and then the whole per-document
            # chunk drain run on this connection; the pending-docs SELECT must not ride along.
            release_read_transaction(
                db_conn, context=f"re_embedder.drain_pending_documents.fetch schema={schema_name}")
        except psycopg2.Error as e:
            # Pre-migration-183 schema (undefined_table) — not an error, just skip.
            _rollback_and_reapply_search_path(db_conn, schema_name)
            if getattr(e, "pgcode", None) == "42P01":
                return 0
            raise

        if not doc_ids:
            return 0

        # DOCDRAIN — DO NOT CLAIM WHAT WE CANNOT PROCESS. The re_embedder shares a container
        # with the API, so on every deploy/restart this loop wakes while uvicorn is still
        # binding. Claiming then means every chunk connect-refuses and the document is burned
        # to TERMINAL 'partial' in seconds (measured: 1153/1166 chunk failures in one 6h
        # window were ECONNREFUSED; two 200-chunk documents destroyed in 1.5s).
        # One cheap probe converts that from permanent loss into a one-interval delay.
        if DOC_BACKEND_READY_PROBE and not _backend_is_reachable(backend_url):
            log.warning(
                f"re_embedder.document_drain_backend_unreachable user_id={user_id[:8]} "
                f"schema={schema_name} pending={len(doc_ids)} backend={backend_url} "
                f"(nothing claimed — retrying next cycle)")
            return 0

        for doc_id in doc_ids:
            # Claim atomically: pending → processing. Another worker/cycle may have
            # taken it — rowcount 0 → skip. RETURNING gives us the chunk list.
            # READ BARRIER (per iteration): this loop body blocks on the brain/Qdrant, and a
            # read left open by the PREVIOUS iteration would ride across it. A batch-level
            # barrier alone does not cover this — measured live: climb_state and the
            # taxonomy reads were each caught idle-in-transaction at 58-59s inside a loop.
            release_read_transaction(db_conn, context="re_embedder.drain_pending_documents.iteration")
            _attempts = 0
            try:
                with db_conn.cursor() as cur:
                    try:
                        # DOCLOSS-B: count the claim. `attempts` bounds the at-least-once
                        # redelivery below so a document whose brain never recovers cannot
                        # re-pend forever. Pre-migration-195 schema → legacy claim.
                        #
                        # brain_deferred_since (migration 264) rides out UNCHANGED. Two
                        # reasons, and the first is mechanical: RETURNING yields
                        # POST-UPDATE values, so a SET ... = NULL here could never hand
                        # the defer handler the clock it needs to judge age (caught by a
                        # live one-off-container probe against real Postgres — a fake-conn
                        # unit test had scripted the pre-UPDATE value, a state the real
                        # SQL cannot produce). The second is semantic: a claim does NOT
                        # end a deferral episode — it is a pure DB write that succeeds
                        # while the brain is still down. The episode ends on PROGRESS: a
                        # terminal finalize or a deadline requeue clears the clock there;
                        # every defer of the episode reads it here, and the re-pend below
                        # stamps it with COALESCE(now()) on the first defer only.
                        cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                            """
                            UPDATE documents
                            SET status = 'processing', started_at = now(),
                                attempts = attempts + 1
                            WHERE id = %s
                              AND (status = 'pending'
                                   OR (status = 'processing'
                                       AND started_at IS NOT NULL
                                       AND started_at < now() - %s::interval))
                            RETURNING chunks, source_ref, attempts, brain_deferred_since
                            """,
                            (doc_id, _DOC_CLAIM_LEASE),
                        )
                    except psycopg2.errors.UndefinedColumn:
                        # A THREE-RUNG CLAIM LADDER — the rung is decided by which columns
                        # exist, and getting the middle rung wrong is how a termination
                        # guard dies silently (fresh-critic round 1: the primary claim
                        # names brain_deferred_since, so EVERY post-195/pre-264 schema
                        # landed here — and this fallback did not increment attempts, so
                        # the "retained" legacy count bound could NEVER fire and documents
                        # re-pended forever at INFO strength. Measured live: all 41
                        # pre-prod schemas are exactly post-195/pre-264 today).
                        #
                        # rung 2 — attempts exists, brain_deferred_since does not
                        # (post-195, pre-264): claim WITH the attempt increment AND the
                        # SAME reclaim-lease predicate as rung 1 — these schemas HAD
                        # working reclaim through the old primary claim, and a rung with
                        # only status='pending' would silently strand every document
                        # left in 'processing' past the lease until 264 applies. The
                        # defer handler then sees clock=None and its legacy count bound
                        # works exactly as before migration 264 existed.
                        # rollback resets search_path — re-apply the tenant bind (NO public).
                        _rollback_and_reapply_search_path(db_conn, schema_name)
                        try:
                            with db_conn.cursor() as _lc:
                                _lc.execute(
                                    """
                                    UPDATE documents
                                    SET status = 'processing', started_at = now(),
                                        attempts = attempts + 1
                                    WHERE id = %s
                                      AND (status = 'pending'
                                           OR (status = 'processing'
                                               AND started_at IS NOT NULL
                                               AND started_at < now() - %s::interval))
                                    RETURNING chunks, source_ref, attempts
                                    """,
                                    (doc_id, _DOC_CLAIM_LEASE),
                                )
                                _claimed = _lc.fetchone()
                            db_conn.commit()
                            # 4th element None = no deferral clock on this schema.
                            claimed = (_claimed[0], _claimed[1], _claimed[2], None) \
                                if _claimed else None
                        except psycopg2.errors.UndefinedColumn:
                            # rung 3 — pre-195: no attempts column either. Legacy shape.
                            _rollback_and_reapply_search_path(db_conn, schema_name)
                            with db_conn.cursor() as _lc:
                                _lc.execute(
                                    """
                                    UPDATE documents
                                    SET status = 'processing', started_at = now()
                                    WHERE id = %s AND status = 'pending'
                                    RETURNING chunks, source_ref
                                    """,
                                    (doc_id,),
                                )
                                _claimed = _lc.fetchone()
                            db_conn.commit()
                            claimed = (_claimed[0], _claimed[1], 0, None) \
                                if _claimed else None
                    else:
                        claimed = cur.fetchone()
                        db_conn.commit()
            except Exception as e:
                log.error(f"re_embedder.document_claim_failed doc_id={doc_id} user_id={user_id[:8]}: {e}")
                _rollback_and_reapply_search_path(db_conn, schema_name)
                continue
            if not claimed:
                continue

            chunks, source_ref = claimed[0], claimed[1]
            _attempts = int(claimed[2] or 0) if len(claimed) > 2 else 0
            # The deferral-episode clock as it stood at claim time (None until a
            # brain-unavailable defer sets it; None on a pre-migration-264 schema).
            _brain_deferred_since = claimed[3] if len(claimed) > 3 else None
            chunks = chunks or []
            committed = staged = chunks_failed = 0
            # LEGACY-LANE LEDGER (None until the legacy pool below populates it; the queue
            # lane maintains its own state via _drain_document_via_queue's incremental flush).
            _legacy_state: Optional[dict] = None

            def _process_one_chunk(idx, chunk):
                """Thin delegate to the module-level `_process_document_chunk`.

                The body was LIFTED to module scope (unchanged) so the queue lane and this
                legacy lane run the SAME code instead of two copies that drift."""
                return _process_document_chunk(
                    chunk, idx=idx, doc_id=doc_id, user_id=user_id,
                    backend_url=backend_url, source_ref=source_ref,
                    statement_route=statement_route)

            try:
                # ── PARALLEL: the QUEUE lane (flag ON + Redis reachable) ──────────────
                # Flag OFF or Redis down → fall straight through to the legacy pool below.
                # `_queue_state` is None whenever the queue lane did not run, and every
                # queue-specific branch downstream is guarded on it, so the OFF path is
                # byte-identical.
                _queue_state = None
                _use_queue = DOC_CHUNK_QUEUE and _register_doc_chunk_handler()
                if _use_queue:
                    try:
                        from src.ingest import chunk_queue as _cq_probe
                        _use_queue = _cq_probe.available()
                    except Exception:  # noqa: BLE001
                        _use_queue = False
                if _use_queue:
                    _prior = _read_chunk_state(db_conn, doc_id, schema_name)
                    # Carry forward what a PREVIOUS attempt already landed, so the finalize
                    # tallies describe the whole document and not just this pass.
                    for _st in (_prior or {}).values():
                        if (_st or {}).get("s") == "done":
                            committed += int(_st.get("c") or 0)
                            staged += int(_st.get("g") or 0)
                        elif (_st or {}).get("s") == "failed":
                            chunks_failed += 1
                    # READ BARRIER (immediately before the blocking call — the RE-ARM case). A barrier at
                    # the top of the enclosing block is NOT enough: a per-row read helper opens a FRESH
                    # transaction after it, and that read then rides across this hop. Measured live on the
                    # deployed image — climb_classification_chains was killed twice this way (02:42:25 and
                    # 02:45:06), its whole _ont_db subsystem chain failing 'connection already closed' four
                    # seconds later.
                    release_read_transaction(db_conn, context="re_embedder.drain_pending_documents.pre_blocking_call")
                    _c, _s, _f, _queue_state, _fatal = _drain_document_via_queue(
                        db_conn, doc_id, chunks, _prior, user_id=user_id,
                        backend_url=backend_url, source_ref=source_ref,
                        statement_route=statement_route, schema_name=schema_name)
                    committed += _c
                    staged += _s
                    chunks_failed += _f
                    if _fatal is not None:
                        raise _fatal
                else:
                    # PARALLEL chunk drain: chunks are independent, so a bounded thread pool
                    # turns the serial per-chunk /extract+/ingest pass into ~len/W wall-clock.
                    # A RuntimeError from ANY chunk (backend freeze) re-raises out of .result()
                    # → the outer handler re-pends the whole doc, exactly as the serial path did.
                    #
                    # LEGACY-LANE LEDGER (the Aug-8 evidence fix): this lane used to persist
                    # COUNTS ONLY (chunks_failed) and discard the per-chunk evidence — the
                    # shape measured on production docs 3/4/7 (chunk_state='{}', error='').
                    # It now builds the SAME per-chunk terminal ledger the queue lane writes
                    # ({"i": {"s": "done"|"failed", "c": n, "g": n, "e": reason}}) and flushes
                    # it at the terminal finalize below, so a partial document names WHICH
                    # chunks failed and WHY on BOTH lanes, and the retry lane can re-owe them.
                    _legacy_state: dict = {}
                    _conc = min(_DOC_CHUNK_CONCURRENCY, max(1, len(chunks)))
                    if _conc <= 1:
                        _results = [_process_one_chunk(i, c) for i, c in enumerate(chunks)]
                    else:
                        with ThreadPoolExecutor(max_workers=_conc) as _ex:
                            _futs = [_ex.submit(_process_one_chunk, i, c)
                                     for i, c in enumerate(chunks)]
                            _results = [f.result() for f in _futs]  # ordered; re-raises freeze
                    for _i, _res in enumerate(_results):
                        if isinstance(_res, tuple) and len(_res) >= 4:
                            _c, _s, _f, _reason = _res[0], _res[1], _res[2], _res[3]
                        else:
                            _c, _s, _f, _reason = _res[0], _res[1], _res[2], None
                        committed += _c
                        staged += _s
                        chunks_failed += _f
                        _legacy_state[str(_i)] = (
                            {"s": "done", "c": _c, "g": _s} if not _f
                            else {"s": "failed", "e": str(_reason or "unread")[:200]})

                # DOCLOSS-B: THE TERMINAL STATUS MUST REFLECT REALITY. A document that
                # finished with unread chunks is NOT 'ready' — 'ready' is the signal every
                # downstream reader (recall's not-ready probe, the bench's terminal poll, a
                # tenant console) treats as "this document is fully in memory". Reporting
                # 'ready' over failed chunks is the same silent-success lie as the truncation
                # cap, one layer down. Terminal 'partial' + log_crit; the failed chunks stay
                # verbatim in documents.chunks AND the episodic log for a later re-mine.
                _terminal = "partial" if (DOC_CHUNK_FAILURE_LOUD and chunks_failed > 0) else "ready"

                # ── PARALLEL: A CHUNK THAT WAS NEVER REACHED MUST NOT BE STAMPED TERMINAL ──
                # This is the burned-chunk fix at the document grain. Under the queue lane a
                # chunk with NO entry in the ledger was never given a terminal outcome — the
                # run hit its deadline, or a worker died and its lease had not yet been reaped.
                # 'partial' is TERMINAL: every downstream reader stops looking, and ~1000
                # chunks were destroyed exactly that way in one day. Put the document back to
                # 'pending' instead; the next claim re-queues ONLY the owed chunks (the ledger
                # says which), so nothing already done is re-spent. Bounded by the SAME
                # `attempts` counter that bounds the brain-outage deferral — a document that
                # cannot finish still terminates, it just is not terminated on the first miss.
                _owed = 0
                if _queue_state is not None:
                    _owed = sum(1 for i in range(len(chunks))
                                if (_queue_state.get(str(i)) or {}).get("s")
                                not in ("done", "failed"))
                #
                # THE OWED LANE IS BOUNDED BY THE SAME DOCTRINE AS THE FREEZE LANE
                # (fresh-critic round 3). This gate used the LIFETIME `attempts` count
                # (`_attempts < _DOC_MAX_ATTEMPTS`), and a queue pass can run ~56 min
                # against a 30-min claim lease — so deadline requeues and lease
                # reclaims alone, with a HEALTHY brain and real partial progress,
                # walked a migrated-schema document to a terminal 'partial' at
                # attempts>=5: blaming the document for system conditions, the exact
                # 95fb517b/round-2 defect through a second doorway. The bound here is
                # now PROGRESS-AWARE: a deadline requeue counts as progress ONLY when
                # the owed count actually DECREASED this pass. A productive requeue
                # clears the episode clock; an UNPRODUCTIVE one keeps/stamps it, and
                # the 30-day wall clock — never the lifetime count — terminates. On
                # pre-264 schemas the legacy count bound stays (nothing else exists).
                _prior_owed = len(chunks)
                if _queue_state is not None:
                    _prior = _prior or {}
                    _prior_owed = sum(1 for i in range(len(chunks))
                                      if (_prior.get(str(i)) or {}).get("s")
                                      not in ("done", "failed"))
                _progress = _queue_state is not None and _owed < _prior_owed
                _unproductive_age_s = None
                if _has_defer_clock and _owed and not _progress \
                        and _brain_deferred_since is not None:
                    try:
                        _cs = _brain_deferred_since
                        if not _cs.tzinfo:
                            _cs = _cs.replace(tzinfo=timezone.utc)
                        _unproductive_age_s = max(
                            0.0, (datetime.now(timezone.utc) - _cs).total_seconds())
                    except (AttributeError, TypeError, ValueError):
                        _unproductive_age_s = None
                if _owed and _has_defer_clock:
                    _owed_exhausted = (DOC_CHUNK_FAILURE_LOUD
                                       and _unproductive_age_s is not None
                                       and _unproductive_age_s >= _DOC_BRAIN_DEFER_MAX_AGE_S)
                elif _owed:
                    _owed_exhausted = (DOC_CHUNK_FAILURE_LOUD
                                       and _attempts >= _DOC_MAX_ATTEMPTS)
                else:
                    _owed_exhausted = False
                if _owed and not _owed_exhausted:
                    with db_conn.cursor() as cur:
                        # _has_defer_clock gates every column reference: pre-264
                        # schemas must not see the column at all (see the capability
                        # probe above for why not try/except).
                        if _has_defer_clock and _progress:
                            cur.execute(
                                "UPDATE documents SET status = 'pending', started_at = NULL, "
                                "chunk_state = %s::jsonb, brain_deferred_since = NULL "
                                "WHERE id = %s",
                                (json.dumps(_queue_state), doc_id))
                        elif _has_defer_clock:
                            # unproductive: START (or keep) the no-progress episode
                            # clock — this is what makes the wall clock able to bound
                            # this lane at all.
                            cur.execute(
                                "UPDATE documents SET status = 'pending', started_at = NULL, "
                                "chunk_state = %s::jsonb, "
                                "brain_deferred_since = COALESCE(brain_deferred_since, now()) "
                                "WHERE id = %s",
                                (json.dumps(_queue_state), doc_id))
                        else:
                            cur.execute(
                                "UPDATE documents SET status = 'pending', started_at = NULL, "
                                "chunk_state = %s::jsonb WHERE id = %s",
                                (json.dumps(_queue_state), doc_id))
                    db_conn.commit()
                    _p_note = "progress" if _progress else "no progress this pass"
                    log.warning(
                        f"re_embedder.document_requeued doc_id={doc_id} user_id={user_id[:8]} "
                        f"owed={_owed}/{len(chunks)} attempt={_attempts} ({_p_note}; "
                        f"returned to the queue, not burned)")
                    continue
                if _owed:
                    # Terminal on the bound that decided — and it says so loudly.
                    chunks_failed += _owed
                    _terminal = "partial" if DOC_CHUNK_FAILURE_LOUD else "ready"
                    _owed_bound = ("unproductive episode "
                                   + str(int(_unproductive_age_s or 0) // 86400) + " days "
                                   + ">= DOC_BRAIN_DEFER_MAX_AGE="
                                   + _DOC_BRAIN_DEFER_MAX_AGE
                                   if _has_defer_clock else
                                   "max_attempts=" + str(_DOC_MAX_ATTEMPTS))
                    _doc_log_crit("re_embedder.document_owed_chunks_exhausted",
                                  doc_id=doc_id, user_id=user_id[:8], owed=_owed,
                                  attempts=_attempts, bound=_owed_bound,
                                  note="verbatim_text_retained_in_documents.chunks+episodic")

                with db_conn.cursor() as cur:
                    # brain_deferred_since = NULL: terminal — any deferral episode is
                    # over (migration 264). Gated on the capability probe: naming the
                    # column on a pre-264 schema would abort this UPDATE and the except
                    # below would mis-mark a healthy document as status='error'.
                    # LEGACY-LANE LEDGER: when the legacy pool ran, persist the per-chunk
                    # terminal ledger it built (reasons included) alongside the counts —
                    # gated on the chunk_state capability probe for the same reason.
                    _legacy_ledger_sql = ""
                    _legacy_ledger_args: list = []
                    if _legacy_state is not None and _has_chunk_state:
                        _legacy_ledger_sql = ", chunk_state = %s::jsonb"
                        _legacy_ledger_args = [json.dumps(_legacy_state)]
                    if _has_defer_clock:
                        cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                            """
                            UPDATE documents
                            SET status = %s, ready_at = now(),
                                facts_committed = %s, facts_staged = %s, chunks_failed = %s,
                                brain_deferred_since = NULL
                            """ + _legacy_ledger_sql + """
                            WHERE id = %s
                            """,
                            (_terminal, committed, staged, chunks_failed,
                             *_legacy_ledger_args, doc_id),
                        )
                    else:
                        cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                            """
                            UPDATE documents
                            SET status = %s, ready_at = now(),
                                facts_committed = %s, facts_staged = %s, chunks_failed = %s
                            """ + _legacy_ledger_sql + """
                            WHERE id = %s
                            """,
                            (_terminal, committed, staged, chunks_failed,
                             *_legacy_ledger_args, doc_id),
                        )
                db_conn.commit()
                finalized += 1
                if _terminal == "partial":
                    _doc_log_crit("re_embedder.document_partial",
                                  doc_id=doc_id, user_id=user_id[:8], chunks=len(chunks),
                                  chunks_failed=chunks_failed, committed=committed, staged=staged,
                                  note="unread_chunks_retained_verbatim_in_documents.chunks+episodic")
                else:
                    log.info(f"re_embedder.document_ready doc_id={doc_id} user_id={user_id[:8]} "
                             f"chunks={len(chunks)} committed={committed} staged={staged} "
                             f"failed={chunks_failed} route={statement_route}")

            except RuntimeError as freeze_err:
                # Backend frozen OR the tenant's brain is unavailable (DocumentBrainUnavailable,
                # a RuntimeError subclass — an OPEN circuit breaker). Same correct response:
                # put the whole document back to 'pending' and retry on a later cycle, which is
                # the at-least-once redelivery this lane already implements (Nygard, Release It!:
                # while the breaker is open the caller takes the fallback path and the work is
                # retried after the reset timeout — it is NOT discarded).
                # DOCLOSS-B: BOUND the redelivery. Without a bound, a document whose brain never
                # comes back re-pends forever and its 'pending' state is indistinguishable from
                # "queued and fine" — a slow-motion silent loss. Past _DOC_MAX_ATTEMPTS the
                # document TERMINATES as 'error' with the reason (chunks retained verbatim).
                # EXCEPTION — rate_deferred (critic pass 3): that bound was sized for a
                # breaker-open outage (MINUTES); a background daily-budget park lasts HOURS by
                # design (until reset_ms). Burning an attempt per 60s cycle terminally errored
                # a user's upload ~5 minutes into a spent day — pass 2's hole reopened with a
                # different terminal status. A PACED defer has a provider-bounded recovery
                # (the window rolls), so it re-pends WITHOUT consuming the attempt budget.
                # A FROZEN BACKEND IS NOT A DOCUMENT THAT CANNOT BE READ. DOCLOSS-B's
                # attempt bound exists so a document whose BRAIN never returns cannot pend
                # forever invisibly -- that rationale is about the brain. `ingest_disabled`
                # is the BACKEND declining to store anything, for every document equally,
                # and it is raised as a plain RuntimeError rather than
                # DocumentBrainUnavailable, so it was silently inheriting the brain's bound
                # and terminating uploads as 'error' during a freeze that had nothing to do
                # with them. Same lane-versus-row split as the episodic poison guard.
                _err_s = str(freeze_err)
                _paced = "rate_deferred" in _err_s
                _backend_frozen = "backend ingest disabled" in _err_s
                _rollback_and_reapply_search_path(db_conn, schema_name)
                #
                # THE BOUND IS WALL CLOCK, NOT A COUNT — the question 95fb517b deliberately
                # deferred, answered (see _DOC_BRAIN_DEFER_MAX_AGE for the magnitude). A claim
                # COUNT is outage duration sized by the drain interval, and 95fb517b measured
                # that sizing empirically wrong: brains sit unavailable for HOURS while five
                # claims at a 60s cadence terminated an innocent upload in ~5 minutes. On a
                # migrated schema (brain_deferred_since came back from the claim) the verdict
                # is: has THIS document been CONTINUOUSLY deferred longer than every outage
                # class the system has observed? If yes, no outage this deployment has ever
                # seen explains it — genuinely unprocessable, terminate loudly, verbatim
                # retained. Pre-migration-264 schema (clock is None): legacy attempt bound.
                # THE VERDICT KEYS ON SCHEMA CAPABILITY, NOT ON THE CLOCK VALUE
                # (fresh-critic round 2). The claim RETURNINGs the clock as it stood
                # AT CLAIM TIME, so the FIRST defer of a fresh episode always sees
                # NULL — the previous progress event cleared it. `attempts`, by
                # contrast, is a LIFETIME counter (never reset; every claim
                # increments it, including lease-reclaims and deadline requeues on a
                # healthy-but-slow brain). Branching a NULL clock to the count bound
                # therefore terminated a migrated-schema document carrying 4 lifetime
                # claims on the first defer of a NEW outage, ~0 seconds in — the
                # exact 95fb517b defect (blaming the document for the brain's
                # outage) alive inside the mechanism that retires it. On a migrated
                # schema NULL means "episode not started" = age 0 = survive; the
                # count bound exists ONLY for schemas without the column.
                _clock_age_s = None
                if _brain_deferred_since is not None:
                    try:
                        _cs = _brain_deferred_since
                        if not _cs.tzinfo:
                            _cs = _cs.replace(tzinfo=timezone.utc)
                        _clock_age_s = max(
                            0.0, (datetime.now(timezone.utc) - _cs).total_seconds())
                    except (AttributeError, TypeError, ValueError):
                        _clock_age_s = None
                if _has_defer_clock:
                    # MIGRATED SCHEMA: the wall clock is the ONLY bound. A NULL or
                    # unusable clock means age 0 / unknown — fail toward re-pend,
                    # never toward the count bound: a TIMESTAMPTZ RETURNING only
                    # ever yields a datetime or None, so "unusable" is paranoia,
                    # and the count bound is the defect this migration retires.
                    _exhausted = (DOC_CHUNK_FAILURE_LOUD and not _paced
                                  and not _backend_frozen
                                  and _clock_age_s is not None
                                  and _clock_age_s >= _DOC_BRAIN_DEFER_MAX_AGE_S)
                    _bound_note = ("deferred_since=" + str(_brain_deferred_since)
                                   + " defer_max_age_s=" + str(_DOC_BRAIN_DEFER_MAX_AGE_S))
                    _err_msg = ("brain unavailable after " + str(_attempts) + " attempts "
                                + "(deferred " + str(int(_clock_age_s or 0) // 86400)
                                + " days, exceeds DOC_BRAIN_DEFER_MAX_AGE="
                                + _DOC_BRAIN_DEFER_MAX_AGE + "): "
                                + _errors.public_detail(freeze_err, where="doc.drain.deferred_exhausted",
                                                        what=type(freeze_err).__name__))
                else:
                    # PRE-MIGRATION-264 SCHEMA: the legacy count bound is the only
                    # bound that exists here (and on pre-195 schemas, where rung 3
                    # yields attempts=0, it cannot fire — the documented honest
                    # limit pinned by test_pre195_schema_documents_its_honest_limit).
                    _exhausted = (DOC_CHUNK_FAILURE_LOUD and not _paced
                                  and not _backend_frozen
                                  and _attempts >= _DOC_MAX_ATTEMPTS)
                    _bound_note = ("max_attempts=" + str(_DOC_MAX_ATTEMPTS))
                    _err_msg = ("brain unavailable after " + str(_attempts) + " attempts: "
                                + _errors.public_detail(freeze_err, where="doc.drain.attempts_exhausted",
                                                        what=type(freeze_err).__name__))
                try:
                    with db_conn.cursor() as cur:
                        if _exhausted:
                            cur.execute(
                                "UPDATE documents SET status = 'error', ready_at = now(), "
                                "error = %s WHERE id = %s",
                                (_err_msg, doc_id),
                            )
                        else:
                            if _paced or _backend_frozen:
                                # Give the burned claim-attempt back: the document waits out
                                # the pace window at the SAME attempt count, so real failures
                                # (which still count) retain the full budget.
                                #
                                # _backend_frozen belongs here for the same reason, and
                                # suppressing _exhausted without refunding was not enough:
                                # the claim query increments attempts every cycle, so a
                                # five-minute freeze silently drained the whole budget and
                                # the FIRST genuine brain hiccup after it lifted -- a breaker
                                # open precisely because everything just reconnected --
                                # terminated the upload as 'error' on its first attempt. The
                                # fix without the refund only moved the loss later.
                                #
                                # The deferral clock is deliberately untouched here
                                # too: a designed park must never age a document
                                # toward a terminal verdict.
                                cur.execute(
                                    "UPDATE documents SET status = 'pending', started_at = NULL, "
                                    "attempts = GREATEST(attempts - 1, 0) WHERE id = %s",
                                    (doc_id,),
                                )
                            else:
                                # Start (or keep) the CONTINUOUS deferral-episode clock.
                                # COALESCE is exactly "first defer of this episode": the
                                # clock is NULL since the last PROGRESS cleared it (terminal
                                # finalize / productive requeue — the claim deliberately does
                                # NOT clear it) or it was never set. A schema without the
                                # column raises and lands in the except below — the plain
                                # re-pend still happens there, so nothing is lost.
                                cur.execute(
                                    "UPDATE documents SET status = 'pending', started_at = NULL, "
                                    "brain_deferred_since = COALESCE(brain_deferred_since, now()) "
                                    "WHERE id = %s",
                                    (doc_id,),
                                )
                    db_conn.commit()
                    if _exhausted:
                        finalized += 1
                except Exception:
                    _rollback_and_reapply_search_path(db_conn, schema_name)
                    try:
                        with db_conn.cursor() as cur:
                            cur.execute(
                                "UPDATE documents SET status = 'pending', started_at = NULL WHERE id = %s",
                                (doc_id,),
                            )
                        db_conn.commit()
                    except Exception:
                        _rollback_and_reapply_search_path(db_conn, schema_name)
                if _exhausted:
                    _doc_log_crit("re_embedder.document_redelivery_exhausted",
                                  doc_id=doc_id, user_id=user_id[:8], attempts=_attempts,
                                  bound=_bound_note, reason=str(freeze_err)[:200],
                                  note="chunks_retained_verbatim_for_remine_NOT_ingested")
                elif _paced:
                    log.info(f"re_embedder.document_paced doc_id={doc_id} user_id={user_id[:8]} "
                             f"attempts={_attempts} reason={str(freeze_err)[:120]} "
                             f"note=re-pended without consuming the attempt budget; "
                             f"retries when the daily budget window rolls")
                elif _backend_frozen:
                    log.info(f"re_embedder.document_backend_frozen doc_id={doc_id} "
                             f"user_id={user_id[:8]} attempts={_attempts} "
                             f"note=backend declined to store for EVERY document; re-pended "
                             f"without consuming the attempt budget: {freeze_err}")
                else:
                    _age_note = (" deferred_for_s=" + str(int(_clock_age_s))
                                 if _clock_age_s is not None else "")
                    log.info(f"re_embedder.document_deferred doc_id={doc_id} user_id={user_id[:8]} "
                             f"attempt={_attempts}{_age_note} "
                             f"(brain unavailable — retry next cycle): {freeze_err}")
                # Stop this cycle — the whole backend/brain is unavailable, no point continuing.
                break
            except Exception as e:
                # Catastrophic per-document failure → mark 'error' so it isn't re-claimed.
                _rollback_and_reapply_search_path(db_conn, schema_name)
                try:
                    with db_conn.cursor() as cur:
                        cur.execute(
                            "UPDATE documents SET status = 'error', ready_at = now(), error = %s WHERE id = %s",
                            # PERSISTED then RENDERED (GET /documents/status, document_status tool):
                            # the public shape goes in the row, the sentence to the CRIT line.
                            (_errors.public_detail(e, where="doc.drain.document_failed", what="document processing failed"), doc_id),
                        )
                    db_conn.commit()
                    finalized += 1
                except Exception:
                    _rollback_and_reapply_search_path(db_conn, schema_name)
                log.error(f"re_embedder.document_error doc_id={doc_id} user_id={user_id[:8]}: {e}")

    except Exception as e:
        log.error(f"re_embedder.document_drain_error user_id={user_id[:8] if user_id else 'unknown'} "
                  f"schema={schema_name}: {e}")
        _rollback_and_reapply_search_path(db_conn, schema_name)

    return finalized


def fast_drain_pending_documents_prepass(postgres_dsn: str, ready_schemas,
                                         backend_api_url: str,
                                         statement_route: str = "rewrite") -> int:
    """Top-of-cycle FAST pre-pass for the async document lane (LATENCY FIX).

    ``drain_pending_documents`` also runs in the in-order per-tenant walk, but there
    it sits BEHIND that tenant's heavy passes (promotion, expiry, episodic
    re-extraction, synonym convergence, whatis-climb, …) AND behind every earlier
    active tenant's heavy passes — so with a slow brain over ~N schemas a freshly
    enqueued document waited a FULL cycle to be reached (measured >6 min live), while
    ingest_document promised ~seconds.

    This pre-pass runs BEFORE any heavy pass and touches ONLY pending documents: a
    cheap indexed EXISTS probe per tenant, and a drain ONLY for the tenants that
    actually have a pending doc. So a queued doc is claimed on the very next poll tick
    regardless of its position in the walk, never waiting behind unrelated lifecycle
    work.

    SAFETY / NO-REGRESSION: it is a STRICT SUBSET of what the in-order walk already
    does (same drain, same claim/finalize SQL). A doc drained here is already 'ready'
    when the in-order pass reaches it (its pending probe returns nothing → 0), so the
    heavy passes are untouched. Per-tenant isolation mirrors the heavy walk: one shared
    connection with ``_rollback_and_reapply_search_path`` on any per-tenant error, so a
    broken / pre-migration-183 tenant is skipped and NEVER poisons the sweep. Callers
    gate on ingest_enabled — a frozen store
    never drains. Returns the number of documents finalized this pre-pass; never raises.
    """
    finalized = 0
    _db = None
    try:
        _db = psycopg2.connect(postgres_dsn)
        for _user, _schema in (ready_schemas or []):
            try:
                # Cheap pending probe — skip tenants with nothing to drain so a busy
                # rig's pre-pass stays fast (one indexed EXISTS per tenant).
                _has_pending = False
                try:
                    with _db.cursor() as _c:
                        _c.execute(f"SET search_path TO {_schema}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema from UUID-derived source with validation
                        _c.execute("SELECT 1 FROM documents WHERE status = 'pending' LIMIT 1")
                        _has_pending = _c.fetchone() is not None
                    _db.commit()
                except psycopg2.Error as _probe_err:
                    # Pre-migration-183 schema (undefined_table) or any probe error →
                    # nothing to drain here; clean the txn and move on.
                    _rollback_and_reapply_search_path(_db, _schema)
                    if getattr(_probe_err, "pgcode", None) != "42P01":
                        log.debug(f"re_embedder.fast_doc_probe_error schema={_schema}: {_probe_err}")
                    continue
                if not _has_pending:
                    continue

                _reembedder_bind_tenant(_schema)
                _n = drain_pending_documents(
                    _db, backend_api_url, user_id=_user,
                    schema_name=_schema, statement_route=statement_route,
                )
                if _n:
                    finalized += _n
                    log.info(f"re_embedder.fast_doc_drain user_id={_user[:8]} "
                             f"schema={_schema} docs={_n}")
            except Exception as _fast_err:
                # Per-tenant fail-safe: never let one tenant abort the sweep.
                _rollback_and_reapply_search_path(_db, _schema)
                log.warning(f"re_embedder.fast_doc_drain_error user_id={_user[:8]}: {_fast_err}")
    except Exception as _prepass_err:
        log.warning(f"re_embedder.fast_doc_drain_prepass_error: {_prepass_err}")
    finally:
        if _db is not None:
            try:
                _db.close()
            except Exception:
                pass
    return finalized


def reconcile_qdrant(db_conn, qdrant_url: str, qwen_api_url: str) -> dict:
    """
    Full reconciliation pass across all FaultLine Qdrant collections.
    Scrolls all points, compares payloads to PostgreSQL ground truth,
    deletes orphaned/superseded points, re-upserts diverged payloads.
    Returns: {"deleted": int, "reupserted": int, "ok": int, "errors": int}
    """
    stats = {"deleted": 0, "reupserted": 0, "ok": 0, "errors": 0}

    # Step 1: Discover active FaultLine collections only — skip stale/test collections
    # that have no provisioned users to avoid scrolling dead data every cycle.
    try:
        with db_conn.cursor() as _cur:
            _cur.execute(
                "SELECT user_id FROM public.user_provisioning WHERE status = 'ready'"
            )
            active_user_ids = {row[0] for row in _cur.fetchall()}
            # READ BARRIER: the collection enumeration below is an HTTP hop to Qdrant.
            release_read_transaction(db_conn, context="re_embedder.reconcile_qdrant.seats")
    except Exception as e:
        log.warning(f"re_embedder.reconcile_active_users_failed (using all collections): {e}")
        try:
            db_conn.rollback()
        except Exception:
            pass
        active_user_ids = None

    # Partition-aware work list: each item is (collection, recon_user_id, tenant_filter).
    #   collection_per_seat → one item per per-seat `faultline-<uuid>` collection; user_id is
    #     derived from the collection name (below); tenant_filter is None (byte-for-byte today).
    #   shared_payload → one item per ACTIVE seat, all pointing at the single `faultline-memory`
    #     collection but each carrying its own tenant filter so the scroll/delete/reupsert only
    #     ever touches that seat's points.
    from src.api.qdrant_partition import (
        shared_mode as _qp_shared_mode, SHARED_MEMORY_COLLECTION as _QP_SHARED_MEM,
        tenant_filter_for as _qp_tenant_filter_for, apply_tenant_filter as _qp_apply_filter,
        build_delete_body as _qp_delete_body, qdrant_headers as _qp_headers,
        stamp_tenant as _qp_stamp,
    )
    _shared = _qp_shared_mode()
    work_items = []
    if _shared:
        # Enumerate seats: prefer the caller's active set; else derive from PG schemas.
        _seat_ids = None
        if active_user_ids is not None:
            _seat_ids = [str(u) for u in active_user_ids]
        else:
            try:
                with db_conn.cursor() as _scur:
                    _scur.execute(
                        "SELECT schema_name FROM information_schema.schemata "
                        "WHERE schema_name LIKE 'faultline\\_%' ESCAPE '\\'"
                    )
                    _seat_ids = []
                    for (_sn,) in _scur.fetchall():
                        _uid = _sn[len("faultline_"):].replace("_", "-")
                        if _uid and _uid not in ("test", "main"):
                            _seat_ids.append(_uid)
            except Exception as e:
                log.error(f"re_embedder.reconcile_seat_enum_failed: {e}")
                try:
                    db_conn.rollback()
                except Exception:
                    pass
                return stats
        for _uid in (_seat_ids or []):
            work_items.append((_QP_SHARED_MEM, _uid, _qp_tenant_filter_for(_uid)))
    else:
        try:
            response = httpx.get(f"{qdrant_url}/collections", headers=_qp_headers(), timeout=10.0)
            response.raise_for_status()
            data = response.json()
            all_collections = [
                c["name"] for c in data.get("result", {}).get("collections", [])
                if c["name"].startswith("faultline-")
            ]
            if active_user_ids is not None:
                active_collection_names = {derive_collection(uid) for uid in active_user_ids}
                collections = [c for c in all_collections if c in active_collection_names]
                skipped = len(all_collections) - len(collections)
                if skipped:
                    log.debug(f"re_embedder.reconcile_skipped_inactive collections={skipped}")
            else:
                collections = all_collections
        except Exception as e:
            log.error(f"re_embedder.reconcile_discover_failed: {e}")
            return stats
        work_items = [(c, None, None) for c in collections]

    if not work_items:
        log.info("re_embedder.reconcile no collections found")
        return stats

    # ORPHAN-SKIP: a swept tenant's PG schema is DROP SCHEMA CASCADE'd but its per-user
    # Qdrant collection can be left behind (141 such orphans flooded this loop). Fetch the
    # set of existing tenant schemas ONCE per cycle (cheap) so we can skip scrolling/
    # reconciling any collection whose schema no longer exists. FAIL-SAFE: on any error the
    # set is None and should_reconcile_collection() falls through to PROCESS (never silently
    # skips work on a check failure). Deleting the orphan is the sweep's job, NOT ours.
    existing_schemas = None
    try:
        with db_conn.cursor() as _scur:
            _scur.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name LIKE 'faultline_%'"
            )
            existing_schemas = {row[0] for row in _scur.fetchall()}
        # READ BARRIER: everything after this is per-collection scroll/delete/upsert HTTP.
        release_read_transaction(db_conn, context="re_embedder.reconcile_qdrant.schemas")
    except Exception as e:
        log.warning(f"re_embedder.reconcile_schema_enum_failed (processing all collections): {e}")
        try:
            db_conn.rollback()
        except Exception:
            pass
        existing_schemas = None

    log.info(f"re_embedder.reconcile_start collections={len(work_items)}")

    # Process each work item
    for collection, _recon_uid, _recon_tflt in work_items:
        try:
            # ORPHAN-SKIP: per-seat collection whose PG schema is gone → don't scroll it.
            # (shared_payload has no orphan collections — one shared collection, seat-filtered.)
            if not _shared and not should_reconcile_collection(collection, existing_schemas):
                log.info(f"re_embedder.reconcile_skip_orphan collection={collection} "
                         f"reason=no_pg_schema")
                continue
            # Step 2: Scroll all points with payload but no vectors
            all_points = []
            next_page_offset = None

            while True:
                # READ BARRIER (per iteration): this loop body blocks on the brain/Qdrant, and a
                # read left open by the PREVIOUS iteration would ride across it. A batch-level
                # barrier alone does not cover this — measured live: climb_state and the
                # taxonomy reads were each caught idle-in-transaction at 58-59s inside a loop.
                release_read_transaction(db_conn, context="re_embedder.reconcile_qdrant.iteration")
                scroll_body = {
                    "limit": 250,
                    "with_payload": True,
                    "with_vector": False,
                }
                if next_page_offset is not None:
                    scroll_body["offset"] = next_page_offset
                # shared_payload: scope the scroll to THIS seat (no-op in collection_per_seat).
                scroll_body = _qp_apply_filter(scroll_body, _recon_tflt)

                response = httpx.post(
                    f"{qdrant_url}/collections/{collection}/points/scroll",
                    json=scroll_body,
                    headers=_qp_headers(),
                    timeout=10.0
                )
                response.raise_for_status()
                data = response.json()

                points = data.get("result", {}).get("points", [])
                all_points.extend(points)

                next_page_offset = data.get("result", {}).get("next_page_offset")
                if next_page_offset is None:
                    break

            if not all_points:
                continue

            log.info(f"re_embedder.reconcile_scroll collection={collection} count={len(all_points)}")

            # Step 3: Batch fetch PostgreSQL ground truth
            # Derive user schema from collection name (faultline-{user_id})
            fact_ids = [
                p["payload"]["fact_id"] for p in all_points
                if "fact_id" in p.get("payload", {})
            ]

            if not fact_ids:
                continue

            # Set search_path to user's schema. collection_per_seat → derive from the
            # collection name; shared_payload → the seat uuid is carried in _recon_uid.
            _collection_user_id = _recon_uid if _shared else collection.replace("faultline-", "", 1)
            _schema_name = None
            if _collection_user_id and _collection_user_id != "test" and _collection_user_id != "main":
                try:
                    from src.provisioning.schema_manager import derive_schema_name, derive_user_slug_from_uuid
                    _user_slug = derive_user_slug_from_uuid(_collection_user_id)
                    _schema_name = derive_schema_name(_user_slug)
                except Exception as _e:
                    log.warning(f"re_embedder.reconcile_schema_derivation_failed collection={collection} error={_e}")

            # Key the PG-truth map by (source_table, fact_id), NOT the bare id.
            # facts and staged_facts have independent BIGSERIAL sequences, so id=N can
            # exist in both tables; a bare-id key lets staged#N clobber facts#N in the
            # dict and a collided point reconciles against the WRONG table's row (which
            # then re-upserts a crossed payload — the fabricated-fact defect). Each
            # SELECT tags its own table; lookup uses the point's payload source_table.
            pg_facts = {}
            with db_conn.cursor() as cur:
                if _schema_name:
                    cur.execute(f"SET search_path TO {_schema_name}, public")
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema from UUID-derived source with validation

                placeholders = ",".join(["%s"] * len(fact_ids))
                # Query BOTH facts and staged_facts (Class B/C live in staged_facts)
                cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                    f"""
                    SELECT 'facts' AS source_table, id, subject_id, object_id, rel_type,
                           confidence, superseded_at, deleted_at
                    FROM facts
                    WHERE id IN ({placeholders})
                    UNION ALL
                    SELECT 'staged_facts' AS source_table, id, subject_id, object_id, rel_type,
                           confidence, NULL as superseded_at, deleted_at
                    FROM staged_facts
                    WHERE id IN ({placeholders}) AND promoted_at IS NULL
                    """,
                    fact_ids + fact_ids
                )
                _rq_rows = cur.fetchall()
                # READ BARRIER: the divergence pass below re-embeds and re-upserts each
                # point over HTTP, once per row, on this same connection.
                release_read_transaction(db_conn, context="re_embedder.reconcile_qdrant.pg_facts")
                for row in _rq_rows:
                    pg_facts[(row[0], row[1])] = {
                        "source_table": row[0],
                        "id": row[1],
                        "subject_id": row[2],
                        "object_id": row[3],
                        "rel_type": row[4],
                        "confidence": row[5],
                        "superseded_at": row[6],
                        "deleted_at": row[7],
                    }

            # END THE READ TRANSACTION NOW — do not hold it across Step 4.
            #
            # Closing the cursor (the `with` above) does NOT end the transaction: under
            # psycopg2's default non-autocommit mode a bare SELECT opens one that lives until
            # an explicit commit/rollback. Everything below this point is pure Qdrant HTTP
            # (no further DB access in this function), so the transaction was being held open
            # for the ENTIRE reconcile loop — leaving the connection `idle in transaction` for
            # minutes at a time.
            #
            # That is not merely untidy: an open transaction blocks ACCESS EXCLUSIVE lock
            # acquisition, so it stalls every `TRUNCATE` on the tenant schema, and holds back
            # the xmin horizon so autovacuum cannot reclaim dead tuples database-wide.
            # Observed 2026-07-30: one such connection, idle-in-transaction for 16 minutes,
            # blocked a chain of ~13 backends waiting on `SET search_path`.
            #
            # rollback() rather than commit(): this is a read-only snapshot, so rollback ends
            # the transaction without any possibility of persisting something unintended.
            try:
                db_conn.rollback()
            except Exception as _e:      # never let hygiene break reconciliation
                log.warning(f"re_embedder.reconcile_txn_release_failed error={_e}")

            # Step 4: Reconcile each point
            for point in all_points:
                # READ BARRIER (per iteration): this loop body blocks on the brain/Qdrant, and a
                # read left open by the PREVIOUS iteration would ride across it. A batch-level
                # barrier alone does not cover this — measured live: climb_state and the
                # taxonomy reads were each caught idle-in-transaction at 58-59s inside a loop.
                release_read_transaction(db_conn, context="re_embedder.reconcile_qdrant.iteration")
                try:
                    point_id = point["id"]
                    payload = point.get("payload", {})
                    fact_id = payload.get("fact_id")
                    point_source_table = payload.get("source_table")

                    # store_context facts have no Postgres backing — intentionally Qdrant-only, never orphans
                    if payload.get("rel_type") == "context":
                        stats["ok"] += 1
                        continue

                    # Legacy points (synced before the source_table payload field) can't be
                    # safely keyed to a specific table — both facts#N and staged#N may exist.
                    # Fail SAFE: skip rather than risk reconciling/deleting against the wrong
                    # row. The new-id re-sync re-upserts the canonical point; the bare-int
                    # legacy point is left for the deterministic orphan path / collection wipe.
                    if point_source_table is None:
                        stats["ok"] += 1
                        log.debug(
                            f"re_embedder.reconcile_skip_legacy point_id={point_id} "
                            f"fact_id={fact_id} reason=no_source_table collection={collection}"
                        )
                        continue

                    pg_key = (point_source_table, fact_id)

                    # 4a: Check if fact exists in PostgreSQL (keyed by its OWN table)
                    if pg_key not in pg_facts:
                        httpx.post(
                            f"{qdrant_url}/collections/{collection}/points/delete",
                            json=_qp_delete_body(_recon_tflt, point_ids=[point_id]),
                            headers=_qp_headers(),
                            timeout=10.0
                        )
                        stats["deleted"] += 1
                        log.info(f"re_embedder.reconcile_deleted point_id={point_id} reason=not_in_pg collection={collection}")
                        continue

                    pg_row = pg_facts[pg_key]

                    # 4b: Check if fact is superseded — delete from Qdrant
                    if pg_row.get("superseded_at") is not None:
                        httpx.post(
                            f"{qdrant_url}/collections/{collection}/points/delete",
                            json=_qp_delete_body(_recon_tflt, point_ids=[point_id]),
                            headers=_qp_headers(),
                            timeout=10.0
                        )
                        stats["deleted"] += 1
                        log.info(f"re_embedder.reconcile_deleted point_id={point_id} reason=superseded collection={collection}")
                        continue

                    # 4b': Check if fact is TOMBSTONED (user FORGOT it) — delete from Qdrant.
                    # forget() sets deleted_at + qdrant_synced=false; the sync fetch now skips
                    # deleted_at rows, but a point re-added before this fix (or by a racing
                    # sync) is reaped here. pg_key is (source_table, fact_id) → collision-safe.
                    if pg_row.get("deleted_at") is not None:
                        httpx.post(
                            f"{qdrant_url}/collections/{collection}/points/delete",
                            json=_qp_delete_body(_recon_tflt, point_ids=[point_id]),
                            headers=_qp_headers(),
                            timeout=10.0
                        )
                        stats["deleted"] += 1
                        log.info(f"re_embedder.reconcile_deleted point_id={point_id} reason=tombstoned collection={collection}")
                        continue

                    # 4c: Fact exists and is active — check payload drift
                    expected_rel_type = pg_row.get("rel_type")
                    expected_confidence = float(pg_row.get("confidence") or 0.8)

                    payload_matches = (
                        payload.get("rel_type") == expected_rel_type
                        and abs((payload.get("confidence") or 0.0) - expected_confidence) <= 0.01
                    )

                    if not payload_matches:
                        # 4d: Re-embed and re-upsert with corrected payload
                        text = f"{payload.get('subject', '')} {expected_rel_type} {payload.get('object', '')}"
                        vector = embed_text(text, qwen_api_url, timeout=30.0, fallback=True)
                        _reupsert_payload = {**payload, "rel_type": expected_rel_type, "confidence": expected_confidence}
                        # shared_payload: re-stamp the seat's tenant_id (no-op in per-seat).
                        _reupsert_payload = _qp_stamp(_reupsert_payload, _recon_uid)
                        try:
                            httpx.put(
                                f"{qdrant_url}/collections/{collection}/points",
                                json={"points": [{"id": point_id, "vector": vector, "payload": _reupsert_payload}]},
                                headers=_qp_headers(),
                                timeout=10.0
                            )
                            stats["reupserted"] += 1
                            log.info(f"re_embedder.reconcile_reupserted point_id={point_id} fact_id={fact_id} collection={collection}")
                        except Exception as _re:
                            log.warning(f"re_embedder.reconcile_reupsert_failed point_id={point_id}: {_re}")
                            stats["errors"] += 1
                    else:
                        stats["ok"] += 1

                except Exception as e:
                    fact_id = point.get("payload", {}).get("fact_id", "unknown")
                    stats["errors"] += 1
                    log.error(f"re_embedder.reconcile_point_error point_id={point.get('id')} fact_id={fact_id} collection={collection}: {e}")

        except Exception as e:
            log.error(f"re_embedder.reconcile_collection_error collection={collection}: {e}")

    return stats


def _query_llm_for_rel_type_metadata(candidate_rel: str, subj_type: str, obj_type: str,
                                      snippet: str, qwen_api_url: str,
                                      raise_on_nonanswer: bool = False) -> dict:
    """
    dprompt-126: Phase 2 — Query LLM for natural language metadata during ontology evaluation.

    Called from the `decision == "approved"` branch of evaluate_ontology_candidates, i.e. when a
    novel rel_type reaches REL_TYPE_APPROVAL_THRESHOLD (default 1 since 2026-08-27 — engine
    structure is usable on derivation; it was a hardcoded 3). This function has NO gate of its own:
    its frequency behaviour is entirely the caller's. Generates:
    - natural_language: human-readable description
    - is_symmetric: whether the relationship is bidirectional
    - inverse_rel_type: the opposite relationship (if asymmetric)
    - category: classification (family, work, behavioral, etc.)
    - fact_class: confidence → A/B/C assignment
    - confidence: 0.0-1.0 assessment
    - examples: sample usages for extraction prompt

    Returns dict with llm_* fields or empty dict on failure (non-blocking).

    Phase 3c: Uses call_llm_with_retry_sync() for resilient LLM calls with
    automatic retry and circuit breaker support.
    """
    try:
        # Mark prompt with FaultLine prefix to prevent context bloat if it loops back (dprompt-128)
        prompt = f"""{_FAULTLINE_INTERNAL_PREFIX} You are an ontology expert analyzing a relationship pattern from conversation data.

Pattern: {candidate_rel}
Subject Type: {subj_type or 'unknown'}
Object Type: {obj_type or 'unknown'}
Sample: "{snippet}"

Respond with ONLY valid JSON (no markdown, no extra text):
{{
  "natural_language": "X {candidate_rel.replace('_', ' ')} Y  ← MUST use X for subject and Y for object (e.g., 'X and Y are friends', 'X has IP address Y')",
  "natural_language_2p": "SECOND-PERSON form of natural_language with the subject baked in as 'you'/'your' and ONLY the object kept as the Y slot (e.g. 'X is the parent of Y' → 'You are the parent of Y'; 'X has IP address Y' → 'You have IP address Y'; symmetric 'X and Y are friends' → 'You and Y are friends'). MUST contain Y, MUST NOT contain X.",
  "is_symmetric": boolean,
  "inverse_rel_type": "opposite rel_type or null",
  "category": "family|work|location|identity|temporal|behavioral|physical|social|network",
  "head_types": ["entity types allowed as SUBJECT, e.g. Person or Object; use [\\"ANY\\"] if unconstrained, [\\"SCALAR\\"] never applies to subject"],
  "tail_types": ["entity types allowed as OBJECT, e.g. Organization; use [\\"SCALAR\\"] if the object is a literal value (number/string/date), [\\"ANY\\"] if unconstrained"],
  "fact_class": "A|B|C",
  "confidence": 0.0-1.0,
  "examples": [{{"subject": "Person1", "object": "Person2"}}]
}}"""

        # Phase 3c: Use centralized LLM retry logic instead of raw httpx call
        result = call_llm_with_retry_sync(
            messages=[{"role": "user", "content": prompt}],
            model=LLMModels.get("ENRICHMENT"),
            # PER-TENANT (was the hardcoded "re_embedder" literal): every caller of this helper
            # — the Class-C promotion classifier, the ontology-evaluation mint and the
            # head/tail backfill — runs inside a `ready_schemas` iteration that has already
            # bound this tenant's brain, so the seat that OWNS the rel_type being described is
            # the seat this call must be attributed to. See _reembedder_llm_user_id.
            user_id=_reembedder_llm_user_id(),
            timeout=LLMTimeouts.get("ENRICHMENT"),
            operation="ENRICHMENT",
        )

        # call_llm_with_retry_sync() returns PARSED JSON (not raw OpenAI response)
        if raise_on_nonanswer and not _brain_answered(result):
            # A NON-ANSWER IS NOT "no constraints". Falling through here returns {}, the caller
            # reads llm_head_types/llm_tail_types as None and substitutes the ANY/ANY wildcard —
            # and ANY/ANY is precisely the shape that quarantines a rel into
            # category='pending_placement' forever (_lc_set discards wildcards, so type-match
            # placement can never fire). So a transient outage would mint a permanently
            # unplaceable rel AND stamp its evaluation row 'approved' so it is never revisited.
            # Opt-in per call site: the two callers that can tolerate defaults are unchanged.
            raise LLMUnavailable("nonanswer_shape", "ENRICHMENT")
        if not result or not isinstance(result, dict):
            log.warning(f"re_embedder.llm_metadata_query_failed rel_type={candidate_rel} reason=no_valid_response")
            return {}

        # Result is already parsed JSON — validate expected fields
        if not result.get("natural_language"):
            log.warning(f"re_embedder.llm_metadata_query_failed rel_type={candidate_rel} reason=missing_natural_language")
            return {}

        # FIX #2: a 3p natural_language template MUST carry the "X" subject placeholder
        # (mirror of the 2p "Y" check below). A placeholderless 3p (e.g. the LLM baked
        # the instance "You participated in Workshop" or "unknown" into it) is REJECTED
        # outright — never persisted — so a malformed template can never overwrite a
        # clean seed. Returning {} makes the caller skip the UPSERT entirely.
        _nl3p = (result.get("natural_language") or "").strip()
        if "X" not in _nl3p:
            log.warning(
                f"re_embedder.natural_language_3p_invalid rel_type={candidate_rel} "
                f"value={_nl3p!r} reason=missing_X_placeholder — rejecting metadata"
            )
            return {}

        # Extract metadata directly from parsed result
        metadata = result

        def _as_type_list(v):
            # Normalize LLM type field (list | comma-string | single string) → list or None.
            if v is None:
                return None
            if isinstance(v, str):
                parts = [p.strip() for p in v.split(",") if p.strip()]
                return parts or None
            if isinstance(v, (list, tuple)):
                parts = [str(p).strip() for p in v if str(p).strip()]
                return parts or None
            return None

        # Validate 2p form: must keep Y, must not reintroduce X. Drop it if malformed
        # (render falls back to the 3p template + agreement fixup — never broken state).
        nl_2p = (metadata.get("natural_language_2p") or "").strip()
        if nl_2p and ("Y" not in nl_2p or "X" in nl_2p):
            log.warning(
                f"re_embedder.natural_language_2p_invalid rel_type={candidate_rel} "
                f"value={nl_2p!r} reason=missing_Y_or_has_X — dropping 2p form"
            )
            nl_2p = ""

        return {
            "llm_natural_language": metadata.get("natural_language", ""),
            "llm_natural_language_2p": nl_2p,
            "llm_is_symmetric": metadata.get("is_symmetric", False),
            "llm_inverse_rel_type": metadata.get("inverse_rel_type"),
            "llm_category": metadata.get("category", "other"),
            "llm_head_types": _as_type_list(metadata.get("head_types")),
            "llm_tail_types": _as_type_list(metadata.get("tail_types")),
            "llm_fact_class": metadata.get("fact_class", "B"),
            "llm_confidence": float(metadata.get("confidence", 0.6)),
            "llm_metadata_json": json.dumps(metadata),
        }

    except Exception as e:
        log.warning(f"re_embedder.llm_metadata_query_failed rel_type={candidate_rel} error={type(e).__name__}: {str(e)[:100]}")

    return {}


# ════════════════════════════════════════════════════════════════════════════════════════════════
# MISS-PUSHBACK — background "what is X?" CONCEPT classification
# (the internal design record §"On a MISS: push back to the LLM and CLASSIFY")
# ════════════════════════════════════════════════════════════════════════════════════════════════
#
# When ingest cannot structure a USER-DERIVED thing (GLiNER2-miss on the subject, or a type-
# inconsistent head-constrained scalar whose object is not a known class), the FIRST-FIRE path
# already stored the raw statement Class C (returnable, NEVER a false-confident B, NEVER dropped)
# and queued the unknown CONCEPT into ontology_evaluations with extraction_method
# ='ingest_miss_pushback' (the concept sits in sample_object, candidate_object_type='unknown').
#
# This sweep is the SECONDARY strengthen: it fires ONE bounded LLM "what is <X>?" classification
# per unknown concept (background, preemptible — never on the ingest hot path), TYPES the concept
# into the canonical 6 + grounds it with a deterministically-validated `subclass_of` placement
# edge BORN CLASS C. Once the concept is a known class, the C-raw fact can re-type / re-structure
# into proper A/B on a subsequent ingest (signals A/B in main._object_resolves_to_known_class now
# hold from the DB alone). GUARDS: classify the CONCEPT, NEVER edit the user's fact; bounded +
# fail-safe; born Class C; LLM proposes / deterministic validates (canonical types, hierarchy-only).

# ENGINE_WHATIS_CLASSIFY: background "what is X?" concept classifier (this sweep). Default ON;
#   fail-safe + bounded. Disable to revert to leaving miss-pushback concepts un-classified in C.
_ENGINE_WHATIS_CLASSIFY = _flag("ENGINE_WHATIS_CLASSIFY", "true")
# Bound on concepts classified per tenant per cycle (one LLM call each — keep the loop cheap).
_WHATIS_BATCH_LIMIT = int(os.environ.get("ENGINE_WHATIS_BATCH_LIMIT", "5") or "5")

# Canonical detection roots (the Pitfall-11 closed set). The "what is X?" classifier may only
# TYPE a concept into one of these — finer placement is the learned subclass_of chain beneath.
_CANONICAL_ENTITY_TYPES = ("Person", "Animal", "Organization", "Location", "Object", "Concept")
_CANONICAL_ENTITY_TYPES_LC = {t.lower(): t for t in _CANONICAL_ENTITY_TYPES}

# ── ±6 async classification CLIMB (rung-fill toward a seeded backbone root) ──
# After the eager leaf-anchor (main._attach_to_seeded_backbone) and the first async
# "what is X?" rung, the chain still has a HOLE: dog -> animal exists, but the REAL
# classification chain dog -> canine -> mammal -> animal does not. This climb fills the
# middle rungs ONE PER PASS so recall never blocks and the chain materializes over time.
#
# MECHANISM (per pass, per tenant): for each concept that already has a placed parent
# whose parent is NOT yet a seeded backbone root, ask the LLM ONE "what is <parent>?"
# (LLM PROPOSES the next parent name only). ACCEPT the proposed parent ONLY if it
# resolves BY IDENTITY (canonicalization) to an existing backbone node; else MINT-AND-
# QUARANTINE via the existing ontology_evaluations miss-pushback model — NEVER auto-place.
# Convergence is by identity (two branches reaching the same canonical "mammal" fuse via
# converge_hierarchy_by_identity). NO cosine / difflib / fuzzy on the durable backbone.
#
# TERMINATION: PRIMARY = the parent is a seeded backbone root (animal/location/person/…
# from the seeded hierarchical entity_taxonomies). BACKSTOP = ±6 hops; a chain that hits
# the hop cap without reaching a seeded root QUARANTINES (stops generating). Build ONLY
# the single vertical leaf->root path, never sideways into siblings (sprawl control).
_ENGINE_CLASSIFY_CLIMB = _flag("ENGINE_CLASSIFY_CLIMB", "true")
# Bound on chains advanced per tenant per cycle (one LLM call each — keep the loop cheap).
_CLIMB_BATCH_LIMIT = int(os.environ.get("ENGINE_CLASSIFY_CLIMB_BATCH", "5") or "5")
# ±6 hop backstop (DESIGN: terminate at a seeded root PRIMARILY, ±6 is the safety stop).
_CLIMB_MAX_HOPS = int(os.environ.get("ENGINE_CLASSIFY_CLIMB_MAX_HOPS", "6") or "6")

# ── CLASSIFY-VERDICT CACHE (DB = cache) — kills the every-cycle re-sweep runaway ──────────
# The ±6 climb + the miss-pushback what-is classifier persist their verdict per CONCEPT ENTITY
# into the per-tenant `climb_state` table and READ it BEFORE any LLM call. A 'placed' or
# 'unplaceable' verdict is SKIPPED (no LLM) until either (a) the concept's input FINGERPRINT
# changes — additive new info: a fresh ingest touched it OR the ontology grew a candidate parent
# — or (b) for 'unplaceable', the backoff window elapsed AND attempt_count is below the cap.
# Past the cap the verdict stays 'unplaceable' until a genuine new-info fingerprint change.
_CLIMB_MAX_ATTEMPTS = int(os.environ.get("ENGINE_CLASSIFY_MAX_ATTEMPTS", "3") or "3")
# Backoff: do not re-attempt an under-cap 'unplaceable' concept within this many MINUTES even
# if its fingerprint is unchanged (a coarse re-validation safety valve; the real re-open is the
# fingerprint change). 0 disables time-backoff (rely on fingerprint + cap only).
_CLIMB_BACKOFF_MIN = int(os.environ.get("ENGINE_CLASSIFY_BACKOFF_MIN", "60") or "60")

# LIFETIME CEILING — the cap above is bounded by the FINGERPRINT, and the fingerprint contains a
# TENANT-GLOBAL term (`_concept_fingerprint` component (b): the count of distinct hierarchy PARENT
# nodes across the whole tenant). That term moves whenever ANY concept anywhere gains a parent, so
# on a growing tenant EVERY cached verdict is invalidated at once and `_CLIMB_MAX_ATTEMPTS` NEVER
# BINDS. Measured on a live deployment: with cap = 3, individual concepts sat at
# attempt_count in the high hundreds — each one a re-classification burning LLM calls — with
# dozens of rows over cap and thousands of recorded attempts on a single tenant.
# This ceiling is checked BEFORE the fingerprint comparison and is therefore the only bound that a
# global-ontology bump cannot reset. It does NOT replace the additive re-validation: below the
# ceiling a fingerprint change still re-opens a concept exactly as before. `attempt_count` is a
# LIFETIME failure counter (reset to 0 only on a 'placed' verdict), so a concept that ever succeeds
# starts over. 0 disables the ceiling (pure legacy behavior).
_CLIMB_LIFETIME_MAX_ATTEMPTS = int(
    os.environ.get("ENGINE_CLASSIFY_LIFETIME_MAX_ATTEMPTS", "25") or "25")


def _concept_fingerprint(db_conn, entity_id: str) -> str:
    """Cheap, deterministic per-concept input fingerprint for additive re-validation.

    Combines (a) the concept's own EVIDENCE — count of LIVE hierarchy edges where it is the
    subject (bumps when a new ingest touches the concept), with (b) a per-tenant ONTOLOGY
    VERSION — the count of distinct backbone PARENT nodes (objects of live hierarchy edges),
    which bumps when /expand or natural growth adds a candidate parent that could now place a
    previously-unplaceable concept. When the current fingerprint != the cached one the inputs
    changed → the verdict is re-opened. NO LLM, NO cosine — pure deterministic counts.

    Read-only, fail-safe: on any error returns "" (an empty fingerprint always differs from a
    stored one, so we err toward re-opening rather than silently pinning a stale verdict)."""
    rels = list(_HIERARCHY_RELS)
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT"
                "  (SELECT count(*) FROM facts"
                "     WHERE subject_id = %s AND rel_type = ANY(%s)"
                "       AND superseded_at IS NULL AND archived_at IS NULL AND deleted_at IS NULL)"
                "  + (SELECT count(*) FROM staged_facts"
                "     WHERE subject_id = %s AND rel_type = ANY(%s)"
                "       AND promoted_at IS NULL AND deleted_at IS NULL),"
                "  (SELECT count(DISTINCT object_id) FROM facts"
                "     WHERE rel_type = ANY(%s)"
                "       AND superseded_at IS NULL AND archived_at IS NULL AND deleted_at IS NULL)"
                "  + (SELECT count(DISTINCT object_id) FROM staged_facts"
                "     WHERE rel_type = ANY(%s) AND promoted_at IS NULL AND deleted_at IS NULL)",
                (str(entity_id), rels, str(entity_id), rels, rels, rels),
            )
            row = cur.fetchone()
        if not row:
            return ""
        return f"e{int(row[0] or 0)}:o{int(row[1] or 0)}"
    except Exception:
        try:
            db_conn.rollback()
        except Exception:
            pass
        return ""


def _climb_state_should_skip(db_conn, entity_id: str, fingerprint: str) -> bool:
    """Cache READ — True iff this concept has a cached verdict we must HONOUR (skip = no LLM).

    Skip when a `climb_state` row exists AND its fingerprint == the current one AND:
      - verdict == 'placed'  → done, never re-attempt on unchanged input; OR
      - verdict == 'unplaceable' AND attempt_count >= cap (gave up until new info); OR
      - verdict == 'unplaceable' AND still inside the backoff window.
    Re-open (return False → allow an LLM attempt) when: no cached row, the fingerprint CHANGED
    (additive new info), or an under-cap 'unplaceable' whose backoff window elapsed.
    Read-only, fail-safe: on error return False (attempt) — the cap/backoff still bound runaway."""
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT verdict, attempt_count, fingerprint, last_attempt_at"
                "  FROM climb_state WHERE entity_id = %s",
                (str(entity_id),),
            )
            row = cur.fetchone()
    except Exception:
        try:
            db_conn.rollback()
        except Exception:
            pass
        return False
    if not row:
        return False
    verdict, attempts, cached_fp, last_at = row[0], int(row[1] or 0), row[2], row[3]
    # LIFETIME CEILING — checked BEFORE the fingerprint comparison, because the fingerprint carries
    # a tenant-GLOBAL term that a growing ontology bumps constantly, which re-opens every capped
    # concept and makes `_CLIMB_MAX_ATTEMPTS` unreachable (see the constant's note for the measured
    # production numbers). A concept that has failed this many times is not one more ontology
    # parent away from resolving; it needs its own evidence. `attempt_count` resets to 0 on a
    # 'placed' verdict, so this never permanently retires a concept that can be placed.
    if (_CLIMB_LIFETIME_MAX_ATTEMPTS > 0
            and verdict != "placed"
            and attempts >= _CLIMB_LIFETIME_MAX_ATTEMPTS):
        return True
    # Additive re-validation: inputs changed → re-open regardless of prior verdict.
    if (cached_fp or "") != (fingerprint or ""):
        return False
    if verdict == "placed":
        return True
    if verdict == "unplaceable":
        if attempts >= _CLIMB_MAX_ATTEMPTS:
            return True  # capped — wait for a genuine new-info fingerprint change
        # Under cap: honour the backoff window (skip if we attempted recently).
        if _CLIMB_BACKOFF_MIN > 0 and last_at is not None:
            try:
                with db_conn.cursor() as cur:
                    cur.execute(
                        "SELECT %s > (now() - make_interval(mins => %s))",
                        (last_at, _CLIMB_BACKOFF_MIN),
                    )
                    fresh = cur.fetchone()
                if fresh and fresh[0]:
                    return True  # attempted within the backoff window → skip this cycle
            except Exception:
                try:
                    db_conn.rollback()
                except Exception:
                    pass
        return False
    return False


def _climb_state_record(db_conn, entity_id: str, verdict: str, reason: str,
                        fingerprint: str) -> None:
    """Cache WRITE — persist the verdict for this concept; bump attempt_count + last_attempt_at.

    'placed' resets attempt_count to 0 (a clean success); 'unplaceable' increments it (so the
    cap can fire). UPSERT keyed by entity_id, idempotent. Fail-safe (never raises)."""
    if not entity_id:
        return
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO climb_state"
                "  (entity_id, verdict, reason, attempt_count, last_attempt_at, fingerprint, updated_at)"
                "  VALUES (%s, %s, %s, %s, now(), %s, now())"
                "  ON CONFLICT (entity_id) DO UPDATE SET"
                "    verdict = EXCLUDED.verdict,"
                "    reason = EXCLUDED.reason,"
                "    attempt_count = CASE WHEN EXCLUDED.verdict = 'placed' THEN 0"
                "                         ELSE climb_state.attempt_count + 1 END,"
                "    last_attempt_at = now(),"
                "    fingerprint = EXCLUDED.fingerprint,"
                "    updated_at = now()",
                (str(entity_id), verdict, reason,
                 0 if verdict == "placed" else 1, fingerprint),
            )
        db_conn.commit()
    except Exception as e:
        try:
            db_conn.rollback()
        except Exception:
            pass
        log.debug(f"re_embedder.climb_state_record_failed entity={str(entity_id)[:12]} "
                  f"verdict={verdict}: {e}")


def _concept_entity_id(db_conn, concept_name: str) -> Optional[str]:
    """Resolve a concept's registered entity UUID by its (lowercased) alias. None if unregistered.
    Read-only, fail-safe."""
    name = (concept_name or "").strip().lower()
    if not name:
        return None
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT entity_id FROM entity_aliases WHERE lower(alias) = %s LIMIT 1",
                (name,),
            )
            row = cur.fetchone()
        return str(row[0]) if row and row[0] else None
    except Exception:
        try:
            db_conn.rollback()
        except Exception:
            pass
        return None


# ── OE-ROW cap/backoff (for miss-pushback concepts with no registered entity to fingerprint) ──
# The what-is classifier reads undecided ontology_evaluations rows. A concept that the LLM can
# never classify AND that never registers as an entity has nothing to fingerprint in climb_state,
# so we cap/back-off ON THE OE ROW itself: occurrence_count is the attempt counter, last_seen_at
# the backoff clock, and at the cap we set re_embedder_decision='concept_unplaceable' so the row
# drops out of the undecided fetch (re-opened later only if a fresh quarantine/ingest bumps it
# back to NULL — additive). All fail-safe, never raise.

def _whatis_row_is_capped(db_conn, row_id) -> bool:
    """True iff this OE row should be SKIPPED this cycle: occurrence_count >= cap, OR it was
    attempted within the backoff window. Read-only, fail-safe (False on error = attempt)."""
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT occurrence_count >= %s,"
                "       (%s > 0 AND last_seen_at IS NOT NULL"
                "        AND last_seen_at > (now() - make_interval(mins => %s)))"
                "  FROM ontology_evaluations WHERE id = %s",
                (_CLIMB_MAX_ATTEMPTS, _CLIMB_BACKOFF_MIN, _CLIMB_BACKOFF_MIN, row_id),
            )
            row = cur.fetchone()
        return bool(row and (row[0] or row[1]))
    except Exception:
        try:
            db_conn.rollback()
        except Exception:
            pass
        return False


def _bump_whatis_row_attempt(db_conn, row_id) -> None:
    """Increment the OE row's attempt counter + backoff clock; at the cap, set the give-up
    decision so it drops out of the undecided fetch. Fail-safe."""
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE ontology_evaluations SET"
                "  occurrence_count = occurrence_count + 1,"
                "  last_seen_at = now(),"
                "  re_embedder_decision = CASE WHEN occurrence_count + 1 >= %s"
                "                              THEN 'concept_unplaceable' ELSE re_embedder_decision END,"
                "  decision_reason = CASE WHEN occurrence_count + 1 >= %s"
                "                         THEN 'what-is unplaceable: attempt cap reached (additive re-open on new info)'"
                "                         ELSE decision_reason END"
                " WHERE id = %s",
                (_CLIMB_MAX_ATTEMPTS, _CLIMB_MAX_ATTEMPTS, row_id),
            )
        db_conn.commit()
    except Exception:
        try:
            db_conn.rollback()
        except Exception:
            pass


def _mark_whatis_row_capped(db_conn, row_id) -> None:
    """Resolve this OE row as cached-skip (climb_state already holds the verdict) so it stops
    being re-fetched every cycle. Fail-safe."""
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE ontology_evaluations SET"
                "  re_embedder_decision = 'concept_unplaceable',"
                "  decision_timestamp = now(),"
                "  decision_reason = 'what-is skipped: cached climb_state verdict (additive re-open on new info)'"
                " WHERE id = %s AND re_embedder_decision IS NULL",
                (row_id,),
            )
        db_conn.commit()
    except Exception:
        try:
            db_conn.rollback()
        except Exception:
            pass


def _query_llm_what_is(concept: str, qwen_api_url: str, context: str | None = None) -> Optional[dict]:
    """Bounded LLM "what is <concept>?" — propose a TYPE + a shallow parent placement.

    LLM = CLASSIFIER (proposes), deterministic rules VALIDATE. Returns:
      {"entity_type": <one of the canonical 6>, "parent": <snake_case category or None>}
    or None on failure / unclassifiable (→ the concept stays Class C, the last resort).

    The parent is the immediate canonical category one rung up (poodle → "dog"), bounded to a
    single shallow rung (connect-to-known, depth ≈1; the engine's convergence/bridging grows the
    rest). NEVER edits the user's fact — it answers "what is the concept" only.

    ``context`` (additive, fail-safe): the full sentence the concept appeared in (the
    persisted ``first_text_snippet``). When present a bounded context line is prepended so an
    ambiguous state ("broke") grounds against its sentence ("what broke? → the GPS, a device")
    instead of a stripped bare word ("broke" → bankrupt → finance). NULL/absent → identical to
    today's bare-word behavior (graceful degradation, never a regression). The LLM still only
    CLASSIFIES the concept itself; deterministic validation is unchanged.
    """
    c = (concept or "").strip()
    if not c:
        return None
    try:
        from src.api.llm_calls import call_llm_with_retry_sync, LLMTimeouts

        _ctx = (context or "").strip()[:500]
        _ctx_line = (
            f"The concept appeared in this sentence: \"{_ctx}\". Classify the CONCEPT itself "
            f"(e.g. a STATE that befell a thing), not the sentence.\n\n"
            if _ctx else ""
        )
        prompt = (
            f"{_FAULTLINE_INTERNAL_PREFIX} You are an ontology classifier. Classify the CONCEPT "
            f"below into exactly ONE general type and (optionally) its immediate parent category. "
            f"Answer about the concept itself — do NOT invent facts about any person.\n\n"
            f"{_ctx_line}"
            f"The parent is the MOST SPECIFIC immediate kind the concept IS — the very NEXT rung "
            f"up the is-a ladder, NOT a broad class that skips rungs. For natural kinds give the "
            f"scientific/taxonomic immediate parent (dog -> canine, NOT animal; cat -> feline; "
            f"salmon -> fish). For made/technical/abstract things and feelings give the most "
            f"specific category (router -> network_device; anxiety -> mood; democracy -> "
            f"political_system). NEVER a top abstract catch-all (thing/entity/concept), and NEVER "
            f"a far-up class when a closer one exists (animal is WRONG for dog — canine is closer). "
            f"If the concept is already a top-level category, or a proper name of a specific "
            f"person/place, answer null — do NOT invent one.\n\n"
            f"Examples (concept -> immediate parent):\n"
            f"  poodle -> dog\n"
            f"  dog -> canine            (NOT animal — animal is too far up)\n"
            f"  canine -> mammal\n"
            f"  router -> network_device\n"
            f"  worried -> fear\n"
            f"  fence -> structure\n"
            f"  democracy -> political_system\n"
            f"  alexander -> null\n\n"
            f"Concept: \"{c}\"\n\n"
            f"Respond with ONLY valid JSON (no markdown, no extra text):\n"
            f"{{\n"
            f'  "entity_type": "ONE of: Person | Animal | Organization | Location | Object | Concept",\n'
            f'  "parent": "the MOST SPECIFIC immediate parent (see examples), written as a single '
            f'lowercase snake_case category token — single-token is a FORMAT rule for matching, '
            f'NOT a hint to pick a broader/shorter word; null if none / already top-level"\n'
            f"}}"
        )
        result = call_llm_with_retry_sync(
            messages=[{"role": "user", "content": prompt}],
            model=LLMModels.get("ENRICHMENT"),
            # PER-TENANT: the concept being classified came out of THIS tenant's
            # ontology_evaluations / hierarchy walk, and the caller (classify_unknown_concepts /
            # climb_classification_chains) has already bound this tenant's brain.
            user_id=_reembedder_llm_user_id(),
            timeout=LLMTimeouts.get("ENRICHMENT"),
            operation="ENRICHMENT",
            # See _query_llm_full_chain: a call that never happened must not be cached as
            # a classification verdict.
            raise_on_unavailable=True,
        )
        if not _brain_answered(result):
            # THE CALL PRODUCED NO ANSWER (never reached / empty / error envelope). Convert it
            # into the LLMUnavailable this helper already promises for the RAISING half of the
            # same failure family, so every existing caller's "no verdict cached, no attempt
            # burned" handler covers it too. Without this the shape falls through as
            # "unclassifiable" and a transient outage is spent as the concept's retry budget.
            raise LLMUnavailable("nonanswer_shape", "ENRICHMENT")

        # Validate the proposed type against the canonical closed set (Pitfall 11). An
        # off-set / 'unknown' type means the LLM could NOT classify → return None (C is the
        # last resort). LLM proposes, deterministic rules accept/reject.
        et = (result.get("entity_type") or "").strip().lower()
        if et not in _CANONICAL_ENTITY_TYPES_LC:
            return None
        entity_type = _CANONICAL_ENTITY_TYPES_LC[et]

        # Parent placement is OPTIONAL and deterministically gated as a one-rung subclass_of
        # bridge (reuse the rung-4 validator: not scalar, one category token, not a universal
        # root, differs from the child). A bad/absent parent simply drops the placement edge —
        # the type alone still unblocks the re-type (signals A/C in main hold on the type).
        parent = (result.get("parent") or "").strip().lower()
        if parent in ("null", "none", ""):
            parent = None
        if parent:
            _ok, _why = _validate_bridge_placement(
                c.lower(), c.lower(), parent,
                child_a_type=entity_type, child_b_type=entity_type,
            )
            if not _ok:
                log.debug(f"re_embedder.whatis_parent_rejected concept={c} parent={parent} reason={_why}")
                parent = None
        log.info(f"re_embedder.whatis concept={c[:40]} type={entity_type} parent={parent}")
        return {"entity_type": entity_type, "parent": parent}
    except LLMUnavailable:
        # Never asked → the caller must NOT record a verdict for this concept.
        raise
    except Exception as e:
        log.warning(f"re_embedder.whatis_query_failed concept={c[:40]} error={type(e).__name__}: {str(e)[:100]}")
        return None


def _query_llm_full_chain(concept: str, qwen_api_url: str, context: str | None = None) -> Optional[list]:
    """ONE-SHOT FULL is-a ladder: ask the LLM ONCE for the COMPLETE ordered chain.

    WHY (proven live): asking ONE rung at a time STALLS — qwen answers `dog -> canine`
    but then refuses `canine -> ?`. Asking for the WHOLE ladder in one shot returns the
    complete taxonomy ("Canis lupus familiaris, Canis, Canidae, Carnivora, Mammalia").
    So we ask ONCE and place every rung; `_query_llm_what_is` stays as the single-rung
    FALLBACK.

    LLM = CLASSIFIER (proposes the ordered names), deterministic rules VALIDATE/PLACE.
    SUBJECT-AGNOSTIC: natural kinds → the scientific/taxonomic chain; technical/abstract/
    feeling concepts → the domain is-a chain. Returns an ORDERED list of snake_case
    category tokens, MOST-SPECIFIC FIRST, up to a general root (the concept itself is NOT
    included; the first element is its immediate parent). Returns None on failure or for a
    PROPER-NAME instance (prefer null over a speculative chain — `apollo` the program must
    not be force-typed as `spacecraft`).

    Bounded via the centralized stack (CLASSIFY_CHAIN op — bigger token budget than a single
    rung, never hardcoded). NEVER edits the user's fact. Fail-safe (returns None, never raises).

    ``context`` (additive, fail-safe): the full sentence the concept appeared in (the persisted
    ``first_text_snippet``); when present a bounded context line is prepended so an ambiguous
    state grounds against its sentence, not a bare word. NULL → today's behavior.
    """
    c = (concept or "").strip()
    if not c:
        return None
    try:
        from src.api.llm_calls import call_llm_with_retry_sync, LLMTimeouts, LLMMaxTokens

        _ctx = (context or "").strip()[:500]
        _ctx_line = (
            f"The concept appeared in this sentence: \"{_ctx}\". Classify the CONCEPT itself "
            f"(e.g. a STATE that befell a thing), not the sentence.\n\n"
            if _ctx else ""
        )
        prompt = (
            f"{_FAULTLINE_INTERNAL_PREFIX} You are an ontology classifier. For the CONCEPT below, "
            f"give the COMPLETE ordered is-a ladder: every category the concept IS, from the MOST "
            f"SPECIFIC immediate parent up to a GENERAL top category — do NOT skip rungs. Answer "
            f"about the concept itself; do NOT invent facts about any person.\n\n"
            f"{_ctx_line}"
            f"For natural kinds give the scientific/taxonomic chain. For made/technical/abstract "
            f"things and feelings give the CONCISE domain is-a chain. Each rung is a SINGLE lowercase "
            f"snake_case category token (single-token is a FORMAT rule for matching, NOT a hint to "
            f"pick a broader word). Do NOT include the concept itself; start at its immediate parent. "
            f"Give the SHORTEST CORRECT classification ladder to the NATURAL top category and STOP "
            f"there. Prefer the common-noun classification a PERSON would give, not the exhaustive "
            f"scientific taxonomy: STOP at the everyday top category (animal / device / emotion / "
            f"location) and do NOT climb into scientific phylum/clade rungs (e.g. vertebrate, "
            f"chordate, eukaryote) NOR cross-domain abstractions (service -> business_activity -> "
            f"economic_activity) that drift off the concept's own domain. Keep it short (the biology "
            f"example is 5 rungs; most are 2-4). Stop at a general root (e.g. "
            f"animal / device / emotion / location). NEVER continue past the natural top into "
            f"upper-ontology placeholders: do NOT emit a bare catch-all (thing/entity/concept/object/"
            f"item/stuff) NOR generic '..._entity' / '..._phenomenon' / '..._concept' / 'abstract_*' "
            f"/ 'cognitive_*' tokens — those carry no classification and must NEVER appear.\n\n"
            f"If the concept is a PROPER NAME of a specific named instance (a person, a place, a "
            f"named program/product/mission), answer with an EMPTY chain [] — do NOT invent a "
            f"speculative ladder for a specific named thing.\n\n"
            f"Examples (concept -> ordered chain, most specific first):\n"
            f"  dog      -> [\"canine\", \"canidae\", \"carnivora\", \"mammal\", \"animal\"]\n"
            f"  poodle   -> [\"dog\", \"canine\", \"canidae\", \"mammal\", \"animal\"]\n"
            f"  router   -> [\"network_device\", \"networking_hardware\", \"device\"]\n"
            f"  anxiety  -> [\"fear\", \"emotion\"]\n"
            f"  anxious  -> [\"fear\", \"emotion\"]   (STOP at emotion — do NOT climb into "
            f"\"affective_state\"/\"psychological_phenomenon\"/\"mental_concept\"/\"cognitive_entity\""
            f"/\"abstract_entity\"; those are upper-ontology junk, NOT a real category)\n"
            f"  apollo   -> []\n"
            f"  alexander -> []\n\n"
            f"Concept: \"{c}\"\n\n"
            f"Respond with ONLY valid JSON (no markdown, no extra text):\n"
            f"{{\n"
            f'  "entity_type": "ONE of: Person | Animal | Organization | Location | Object | Concept",\n'
            f'  "chain": ["immediate_parent", "next_up", "...", "general_root"]   (EMPTY [] for a proper name / specific instance)\n'
            f"}}"
        )
        result = call_llm_with_retry_sync(
            messages=[{"role": "user", "content": prompt}],
            model=LLMModels.get("CLASSIFY_CHAIN"),
            # PER-TENANT: same lane as _query_llm_what_is — the concept is the tenant's, and
            # the enclosing growth loop bound the tenant's brain before reaching here.
            user_id=_reembedder_llm_user_id(),
            timeout=LLMTimeouts.get("CLASSIFY_CHAIN"),
            operation="CLASSIFY_CHAIN",
            max_tokens=LLMMaxTokens.get("CLASSIFY_CHAIN"),
            # DIDN'T-HAPPEN must not read as "answered nothing": this function's callers
            # CACHE A VERDICT on a None return (climb_state 'placed'/'unplaceable') and
            # burn the concept's retry budget doing it. Raising keeps an unreachable brain
            # from writing permanent conclusions the model never actually supplied.
            raise_on_unavailable=True,
        )
        if not _brain_answered(result):
            # NO ANSWER (never reached / empty / error envelope) — same conversion as
            # _query_llm_what_is. _chain_for re-raises this deliberately: caching an empty
            # chain here would make an unreachable brain look like "this concept has no is-a
            # ladder" and the specificity comparison would be decided on that.
            raise LLMUnavailable("nonanswer_shape", "ENRICHMENT")

        # PROPER-NAME GUARD: an explicit empty chain means "specific named instance" — prefer
        # null over a speculative ladder (apollo → spacecraft was wrong; qwen knows the program).
        raw = result.get("chain")
        if not isinstance(raw, (list, tuple)):
            return None

        cl = c.lower()
        ordered: list = []
        seen: set = {cl}
        for item in raw:
            tok = (str(item) if item is not None else "").strip().lower()
            if not tok or tok in seen:
                continue  # drop blanks + de-dup (identity, no fuzzy)
            # ABSTRACTION-TOWER STOP: a no-information upper-ontology rung (`abstract_entity`,
            # `cognitive_entity`, `psychological_phenomenon`, `mental_concept`, …) means the ladder
            # has climbed PAST its real top category into the upper ontology. TRUNCATE here — the
            # chain terminates at the real category we already collected (e.g. `emotion`), it does
            # NOT drop-and-continue into yet more abstraction. (Pattern-based, subject-agnostic.)
            if _is_no_information_upper_root(tok):
                log.debug(f"re_embedder.whatis_chain_truncated_at_abstraction concept={cl} "
                          f"rung={tok} kept={ordered}")
                break
            # Each rung must be a clean category token (reuse the rung-4 validator: not scalar,
            # one token, not a universal root, differs from the concept). A bad rung is dropped,
            # never aborts the rest of the chain.
            _ok, _why = _validate_bridge_placement(cl, cl, tok, child_a_type="", child_b_type="")
            if not _ok:
                log.debug(f"re_embedder.whatis_chain_rung_dropped concept={cl} rung={tok} reason={_why}")
                continue
            ordered.append(tok)
            seen.add(tok)

        if not ordered:
            log.info(f"re_embedder.whatis_chain concept={cl[:40]} chain=[] (proper-name/empty)")
            return None
        log.info(f"re_embedder.whatis_chain concept={cl[:40]} chain={ordered}")
        return ordered
    except LLMUnavailable:
        # The brain was NEVER ASKED (rate-deferred / breaker open / retries exhausted).
        # Propagate: the caller must skip this concept WITHOUT recording a verdict.
        raise
    except Exception as e:
        log.warning(f"re_embedder.whatis_chain_query_failed concept={c[:40]} error={type(e).__name__}: {str(e)[:100]}")
        return None


# Sentinel: "the brain was never asked — abandon this sweep", distinct from every real
# answer INCLUDING None (which legitimately means "asked, and there is no classification").
_BRAIN_UNAVAILABLE = object()


def _ask_brain(fn, *args, stats: Optional[dict] = None, what: str = "", **kwargs):
    """Call an LLM helper, converting LLMUnavailable into an explicit ABORT sentinel.

    The whole point of the sentinel is that it is NOT None. Every caller in this module
    treats None as a decision ("the model could not classify this") and caches it; a call
    that never reached the model has produced no such evidence and must not be recorded.
    """
    try:
        return fn(*args, **kwargs)
    except LLMUnavailable as e:
        if stats is not None:
            stats["brain_unavailable"] = stats.get("brain_unavailable", 0) + 1
        log.warning(f"re_embedder.sweep_aborted_brain_unavailable lane={what} "
                    f"reason={e.reason} operation={e.operation} "
                    f"note=no verdict cached, no attempt burned; retried on a later sweep")
        return _BRAIN_UNAVAILABLE


# THE C-TIER AUTHORITY LADDER, read from the codebase's OWN declared ordering rather than a
# number chosen here. This is the same ladder main._PROVENANCE_AUTHORITY declares; it is the
# only "weighting" in this file, and it is the store's, not ours.
_FACT_PROVENANCE_RANK: dict = {"user_stated": 3, "llm_learned": 2, "llm_inferred": 1}


def evaluate_struck_class_c(db_conn, user_id: str = None) -> dict:
    """A STRUCK Class C row is evaluated against the durable A/B tier, and takes one of two
    exits — reaped as already-covered, or given extended life to try again.

    THE LOOP THIS CLOSES. Recall consults the short-term tier and the durable tier. When a C row
    is struck, that surfacing already increments its relevance counter (the recall half of the
    gate, in main.fetch_facts_from_anchor). What was missing is the SECOND half of the strike:
    deciding what the strike MEANS. If the durable tier already carries the same edge with at
    least the same authority, the C row has served its purpose — the fact is durable now, and
    the short-term copy is redundant, so it is retired. If it is merely a weak hit — nothing in
    A/B corroborates it, or only something the store itself ranks LOWER — it gets a chance at
    life: the clock is extended and it goes round again for another attempt at being typed.

    NO THRESHOLD IS CHOSEN HERE, AND THAT IS DELIBERATE. "Covered by A/B" is an IDENTITY question
    the store can already answer — is there a live `facts` row for this exact triple — not a
    similarity score. The only ordering applied is `fact_provenance`, which is the store's own
    declared authority ladder (user_stated > llm_learned > llm_inferred). A durable row that the
    store ranks BELOW the staged one does not cover it, so the staged row survives. There is no
    similarity floor, no confidence cutoff, and no per-seat tuning, because none could be derived
    from what the graph has observed — and inventing one would be exactly the brittleness this
    work exists to remove.

    RETIREMENT IS A TOMBSTONE, NEVER A DELETE (migration 097): `deleted_at` drops the row from
    the live walk while staying recoverable. Class A/B are never touched — only `fact_class='C'`
    rows are candidates, so the A-B-C model and the single C→B tier change are untouched.

    Returns {"struck": n, "reaped": n, "extended": n}. Fail-safe: never raises.
    """
    out = {"struck": 0, "reaped": 0, "extended": 0}
    try:
        with db_conn.cursor() as cur:
            # STRUCK = surfaced by recall since it was staged. The recall writers bump
            # last_seen_at on every strike and nothing else moves it, so `last_seen_at >
            # first_seen_at` IS the strike record — no new column, no new state.
            cur.execute(
                "SELECT s.id, s.subject_id, s.object_id, s.rel_type, s.fact_provenance"
                "  FROM staged_facts s"
                " WHERE s.fact_class = 'C'"
                "   AND s.promoted_at IS NULL AND s.deleted_at IS NULL"
                "   AND s.last_seen_at > s.first_seen_at"
            )
            struck = cur.fetchall()
        out["struck"] = len(struck)
        for sid, subj, obj, rel, sprov in struck:
            try:
                with db_conn.cursor() as cur:
                    cur.execute(
                        "SELECT fact_provenance FROM facts"
                        " WHERE subject_id = %s AND object_id = %s AND rel_type = %s"
                        "   AND superseded_at IS NULL AND archived_at IS NULL"
                        "   AND deleted_at IS NULL",
                        (subj, obj, rel),
                    )
                    durable = [r[0] for r in cur.fetchall()]
                _s_rank = _FACT_PROVENANCE_RANK.get((sprov or "").strip().lower(), 0)
                _covered = any(
                    _FACT_PROVENANCE_RANK.get((d or "").strip().lower(), 0) >= _s_rank
                    for d in durable
                )
                with db_conn.cursor() as cur:
                    if _covered:
                        cur.execute(
                            "UPDATE staged_facts SET deleted_at = now(), qdrant_synced = false"
                            " WHERE id = %s AND deleted_at IS NULL", (sid,))
                        out["reaped"] += 1
                    else:
                        # A CHANCE AT LIFE: extend by the tier's OWN window (the same interval
                        # the C clock is defined in — not a new number) and leave everything
                        # else alone so the next classification pass can try it again.
                        cur.execute(
                            "UPDATE staged_facts"
                            "   SET expires_at = now() + interval '30 days'"
                            " WHERE id = %s AND deleted_at IS NULL", (sid,))
                        out["extended"] += 1
                db_conn.commit()
            except Exception as _e:  # noqa: BLE001 — one row must never abort the pass
                try:
                    db_conn.rollback()
                except Exception:
                    pass
                log.debug(f"re_embedder.class_c_strike_eval_row_failed staged_id={sid}: {_e}")
        if out["struck"]:
            log.info(f"re_embedder.class_c_strike_evaluated user_id={str(user_id or '')[:8]} "
                     f"struck={out['struck']} reaped={out['reaped']} extended={out['extended']}")
    except Exception as e:  # noqa: BLE001
        try:
            db_conn.rollback()
        except Exception:
            pass
        log.warning(f"re_embedder.class_c_strike_eval_failed: {e}")
    return out


def _rung_exists(db_conn, subj_id: str, obj_id: str, rel_type: str = "subclass_of") -> bool:
    """True iff a LIVE staged row for exactly this triple exists. Read-only, fail-safe (False on
    error → the caller falls through to its ordinary placement path)."""
    if not subj_id or not obj_id:
        return False
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM staged_facts"
                " WHERE subject_id = %s AND object_id = %s AND rel_type = %s"
                "   AND promoted_at IS NULL AND deleted_at IS NULL LIMIT 1",
                (str(subj_id), str(obj_id), rel_type),
            )
            return cur.fetchone() is not None
    except Exception:
        try:
            db_conn.rollback()
        except Exception:
            pass
        return False


def _reconfirm_existing_rung(db_conn, subj_id: str, obj_id: str,
                            rel_type: str = "subclass_of", commit: bool = True) -> bool:
    """RELEVANCE GATE, ingest half: the producer re-derived an edge that ALREADY exists → COUNT
    the re-encounter. Returns True iff a LIVE staged row for exactly this triple was bumped.

    WHY THIS EXISTS. `confirmed_count` is the promotion gate, and its intent is relevance: a
    thing that keeps recurring earns its way in. Every producer below already carries the right
    ON CONFLICT (`confirmed_count + 1`) — but each one runs its CYCLE-GUARD first, and the
    cheapest way for `_is_ancestor_or_descendant(X, Y)` to be true is that `X subclass_of Y`
    IS ITSELF THE EDGE IT FINDS: the guard walks facts ∪ staged, so the moment a rung is staged
    it makes its own parent reachable and the producer returns BEFORE the counter can move. A
    rung could therefore never re-confirm itself. Measured on a live seat: 432 grown rungs, none
    eligible, highest count anywhere 2.

    PREVENTING A DUPLICATE ROW AND REFUSING TO RECORD A RE-ENCOUNTER ARE DIFFERENT THINGS, and
    the guard conflates them. This separates them: an EXACT re-derivation is a confirmation and
    is counted here; anything else (a genuine cycle through some OTHER path, a redundant edge)
    still falls through to the cycle-guard and is still rejected.

    THIS IS NOT A PER-CYCLE TICK. The producers do not re-derive on a timer — the classify climb
    is gated by `_climb_state_should_skip`, which honours a cached 'placed' verdict until the
    concept's deterministic input FINGERPRINT changes (additive new evidence on the concept, or
    a new candidate parent in the tenant ontology). So reaching this function at all means the
    engine looked again BECAUSE NEW INFORMATION ARRIVED, and re-derived the same shape. That is
    exactly the signal the counter is meant to measure, and it is why the fix belongs here and
    not in the threshold.

    TIER-PRESERVING: bumps `confirmed_count` and `last_seen_at` only. It never inserts, never
    promotes, never touches `fact_class` — the A/B/C tier and the C→B / B→facts stages are
    untouched. Tombstoned (`deleted_at`) and already-promoted rows are excluded, matching every
    other lifecycle writer. Fail-safe: False on any error, so the caller behaves exactly as it
    does today.
    """
    if not subj_id or not obj_id or str(subj_id) == str(obj_id):
        return False
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE staged_facts"
                "   SET confirmed_count = confirmed_count + 1,"
                "       last_seen_at = now()"
                " WHERE subject_id = %s AND object_id = %s AND rel_type = %s"
                "   AND promoted_at IS NULL AND deleted_at IS NULL"
                " RETURNING confirmed_count",
                (str(subj_id), str(obj_id), rel_type),
            )
            row = cur.fetchone()
        if not row:
            if commit:
                try:
                    db_conn.rollback()
                except Exception:
                    pass
            return False
        if commit:
            db_conn.commit()
        log.info("re_embedder.rung_reconfirmed",
                 extra={"subject": str(subj_id)[:12], "object": str(obj_id)[:12],
                        "rel_type": rel_type, "confirmed_count": int(row[0] or 0)})
        return True
    except Exception as e:  # noqa: BLE001 — fail-safe: never break a producer
        try:
            db_conn.rollback()
        except Exception:
            pass
        log.debug(f"re_embedder.rung_reconfirm_failed subj={str(subj_id)[:8]} "
                  f"obj={str(obj_id)[:8]}: {e}")
        return False


def _is_ancestor_or_descendant(db_conn, x_id: str, y_id: str, max_walk: int = 64) -> bool:
    """CYCLE-GUARD: True iff `y_id` is already a transitive ANCESTOR or DESCENDANT of `x_id`.

    Before placing `X subclass_of Y` we MUST reject when `Y -> ... -> X` (or `X -> ... -> Y`)
    already exists, else we mint a circular subclass_of (the live `machine <-> mechanical_device`
    corruption). Walks the EXISTING hierarchy edges in BOTH directions over facts ∪ staged
    (live filters only — superseded/archived/deleted/promoted excluded), by IDENTITY (UUID
    edges), NO cosine / fuzzy. Read-only, fail-safe (returns True on error → SKIP the rung, the
    safe choice — a corrupt cycle is worse than a missed rung)."""
    xs, ys = str(x_id), str(y_id)
    if not xs or not ys:
        return False
    if xs == ys:
        return True  # self-loop is a degenerate cycle
    rels = list(_HIERARCHY_RELS)

    def _reachable(start: str, target: str, up: bool) -> bool:
        # up=True  → walk parents (subject->object): is `target` an ANCESTOR of `start`?
        # up=False → walk children (object->subject): is `target` a DESCENDANT of `start`?
        seen: set = {start}
        frontier = [start]
        steps = 0
        while frontier and steps < max_walk:
            steps += 1
            cur = frontier.pop()
            try:
                with db_conn.cursor() as cur_db:
                    if up:
                        cur_db.execute(
                            "SELECT object_id FROM facts"
                            "  WHERE subject_id = %s AND rel_type = ANY(%s)"
                            "    AND superseded_at IS NULL AND archived_at IS NULL"
                            " UNION"
                            " SELECT object_id FROM staged_facts"
                            "  WHERE subject_id = %s AND rel_type = ANY(%s)"
                            "    AND promoted_at IS NULL AND deleted_at IS NULL",
                            (cur, rels, cur, rels),
                        )
                    else:
                        cur_db.execute(
                            "SELECT subject_id FROM facts"
                            "  WHERE object_id = %s AND rel_type = ANY(%s)"
                            "    AND superseded_at IS NULL AND archived_at IS NULL"
                            " UNION"
                            " SELECT subject_id FROM staged_facts"
                            "  WHERE object_id = %s AND rel_type = ANY(%s)"
                            "    AND promoted_at IS NULL AND deleted_at IS NULL",
                            (cur, rels, cur, rels),
                        )
                    rows = [r[0] for r in cur_db.fetchall() if r[0]]
            except Exception:
                try:
                    db_conn.rollback()
                except Exception:
                    pass
                return True  # fail-safe: treat as "would cycle" → skip the rung
            for nxt in rows:
                ns = str(nxt)
                if ns == target:
                    return True
                if ns not in seen:
                    seen.add(ns)
                    frontier.append(ns)
        return False

    # Y already an ancestor of X (Y -> ... -> X would close on X subclass_of Y) OR
    # Y already a descendant of X (X -> ... -> Y; re-adding X subclass_of Y is redundant/loops).
    return _reachable(xs, ys, up=True) or _reachable(xs, ys, up=False)


def _node_under_seeded_root(db_conn, node_id: str, roots: set, max_walk: int = 64) -> bool:
    """SEEDED-ROOT CONVERGENCE: True iff `node_id` IS a seeded root, or already sits transitively
    BENEATH one via the EXISTING hierarchy backbone.

    The seeded taxonomy roots (`roots` — canonical lowercased NAMES from the per-tenant taxonomy
    overlay, NOT a hardcoded literal list) are the intended convergence CEILING. When a climb places
    a rung whose node already resolves under a seeded root (e.g. `mammal` is already
    `mammal subclass_of animal`, and `animal` is seeded), the chain is GROUNDED there — anything the
    LLM proposed ABOVE that node (`vertebrate -> chordate`) is overshoot past the backbone and must
    be dropped. Walks UP the live subclass_of/instance_of edges (facts ∪ staged, live filters only),
    matching ancestor NAMES against the seeded-root set by IDENTITY (UUID edges + normalized-name
    match — NO cosine / fuzzy).

    Subject-agnostic: a domain with no seeded root simply never matches → this returns False and the
    existing ±6 / emergent-root termination still applies (it does NOT force a stop). Read-only,
    fail-safe (False on error → caller falls back to the existing termination; never crashes)."""
    if not node_id or not roots:
        return False
    rels = list(_HIERARCHY_RELS)
    seen: set = {str(node_id)}
    frontier = [str(node_id)]
    steps = 0
    try:
        while frontier and steps < max_walk:
            steps += 1
            cur = frontier.pop()
            nm = _name_of_entity(db_conn, cur)
            if nm and nm in roots:
                return True
            with db_conn.cursor() as cur_db:
                cur_db.execute(
                    "SELECT object_id FROM facts"
                    "  WHERE subject_id = %s AND rel_type = ANY(%s)"
                    "    AND superseded_at IS NULL AND archived_at IS NULL"
                    " UNION"
                    " SELECT object_id FROM staged_facts"
                    "  WHERE subject_id = %s AND rel_type = ANY(%s)"
                    "    AND promoted_at IS NULL AND deleted_at IS NULL",
                    (cur, rels, cur, rels),
                )
                parents = [str(r[0]) for r in cur_db.fetchall() if r and r[0]]
            for p in parents:
                if p not in seen:
                    seen.add(p)
                    frontier.append(p)
    except Exception:
        try:
            db_conn.rollback()
        except Exception:
            pass
        return False
    return False


def classify_unknown_concepts(db_conn, qwen_api_url: str, user_id: str = None, schema_name: str = None) -> dict:
    """Background SECONDARY strengthen: classify miss-pushback concepts (per-user schema).

    Reads undecided `ingest_miss_pushback` rows from ontology_evaluations (the concept in
    sample_object), fires ONE bounded "what is X?" LLM call each, and on success:
      1. TYPES the concept entity (entities.entity_type, ONLY when currently 'unknown' —
         respects the entity-lifecycle hard rule; classify the concept, never override).
      2. GROUNDS it with a deterministically-validated `<concept> subclass_of <parent>` edge,
         BORN CLASS C (staged_facts) — the map gains the slot; freq/convergence grows the chain.
         BOTH ends are resolved to entity UUID surrogates via EntityRegistry BEFORE the write,
         so the is-a ladder lives in the SAME UUID keyspace the hierarchy walker traverses
         (`main._resolve_type_signals` joins by UUID: `f.subject_id = c.ancestor`). Writing the
         display strings into `*_id` (the prior behavior) made the ladder an ISLAND unreachable
         from the entity UUID that instance edges (e.g. `feels`) point at, and violated the
         entity-lifecycle hard rule "never store display names in `*_id` columns". `resolve()` is
         idempotent (UUID v5 from the normalized name) so the subject UUID is byte-identical to
         the existing entity the instance edge already references.
      3. Marks the ontology_evaluations row resolved (re_embedder_decision='concept_classified')
         so it is not re-classified every cycle; on failure leaves it UNDECIDED (re-tries next
         cycle, or decays via decay_ontology_candidates → C stays the last resort).

    Caller passes (user_id, schema_name) for the bound tenant and sets search_path TO the tenant
    schema (NO public). Bounded (_WHATIS_BATCH_LIMIT), fail-safe (never raises — background work
    must not crash the loop). Returns stats dict.
    """
    stats = {"classified": 0, "grounded": 0, "deferred": 0, "errors": 0}
    if not _ENGINE_WHATIS_CLASSIFY:
        return stats
    # DEFENSIVE ENTRY-ROLLBACK: this runs on the poll loop's SHARED connection AFTER several other
    # per-tenant subsystems. If a prior subsystem left the txn ABORTED without rolling back (the
    # known throwaway-tenant cascade), our very first SELECT fails "current transaction is aborted"
    # and the whole concept-classify sweep silently no-ops every cycle — the climb then starves. Clear
    # any inherited aborted state up front so this consumer always starts clean. Best-effort, fail-safe.
    if schema_name:
        _rollback_and_reapply_search_path(db_conn, schema_name)
    # The seeded backbone roots (animal/person/organization/…) — the TYPE→ROOT fallback ceiling below.
    _roots = _seeded_backbone_roots(os.environ.get("POSTGRES_DSN", ""), schema_name)
    try:
        with db_conn.cursor() as cur:
            # first_text_snippet (additive): the full sentence that surfaced the concept,
            # persisted by _queue_concept_for_grounding so the grounder classifies the concept
            # AGAINST its sentence ("what broke? → the GPS, a device"), never a bare word. NULL
            # for legacy rows / unfilled callers → today's bare-word behavior (fail-safe).
            cur.execute(
                "SELECT id, sample_object, first_text_snippet FROM ontology_evaluations"
                " WHERE extraction_method = 'ingest_miss_pushback'"
                "   AND re_embedder_decision IS NULL"
                "   AND sample_object IS NOT NULL AND sample_object <> ''"
                " ORDER BY last_seen_at DESC"
                " LIMIT %s",
                (_WHATIS_BATCH_LIMIT,),
            )
            rows = cur.fetchall()
        # READ BARRIER: the loop below asks the tenant brain (_query_llm_what_is) per row.
        release_read_transaction(
            db_conn, context=f"re_embedder.classify_unknown_concepts.fetch schema={schema_name}")
    except Exception as e:
        if schema_name:
            _rollback_and_reapply_search_path(db_conn, schema_name)
        log.error(f"re_embedder.whatis_fetch_failed: {e}")
        return stats

    if not rows:
        return stats

    log.info(f"re_embedder.whatis_candidates count={len(rows)}")

    for _row_id, _concept, _snippet in rows:
        # READ BARRIER (per iteration): this loop body blocks on the brain/Qdrant, and a
        # read left open by the PREVIOUS iteration would ride across it. A batch-level
        # barrier alone does not cover this — measured live: climb_state and the
        # taxonomy reads were each caught idle-in-transaction at 58-59s inside a loop.
        release_read_transaction(db_conn, context="re_embedder.classify_unknown_concepts.iteration")
        concept = (_concept or "").strip().lower()
        if not concept:
            continue
        _context = (_snippet or "").strip() or None  # full-sentence grounding context (additive)
        try:
            # ── CACHE READ (DB = cache) — skip BEFORE the LLM call ──────────────────
            # Resolve the concept's entity (cheap alias lookup). If it has a cached verdict on
            # the same input fingerprint that we must honour (placed / capped-or-backed-off
            # unplaceable), skip the LLM AND mark this OE row resolved so it stops being fetched
            # every cycle (the deferred-row runaway). When the entity isn't registered yet we
            # cannot fingerprint it → fall through and let occurrence/decay bound it.
            _cid = _concept_entity_id(db_conn, concept)
            # THE HARD LINE — never give a NAMED INSTANCE a subclass_of. A concept that is the
            # subject of an instance_of edge (e.g. `rex instance_of poodle`) is an INSTANCE of
            # a type, not a type; classifying it would climb the NAME up the type ladder (`rex
            # subclass_of animal`) — the category error the founding distinction forbids (a name
            # never becomes a place). A concept may be queued for grounding before its instance_of
            # edge commits (edge ordering on /ingest), so the queue's un-laddered gate can leak a
            # name to this consumer; this STRUCTURAL guard (edge/naming-layer, no word list) is the
            # authoritative stop. Mark the OE row resolved so it isn't re-fetched every cycle. Only
            # checked once the concept is a REGISTERED entity (an unregistered concept has no edges
            # to inspect → can't be a known named instance yet; falls through to occurrence/decay).
            if _cid and _is_named_instance(db_conn, _cid, user_id=user_id):
                stats["deferred"] += 1
                _mark_whatis_row_capped(db_conn, _row_id)
                log.debug("re_embedder.whatis_skipped_named_instance",
                          extra={"concept": concept, "reason": "hard_line_name_never_a_place"})
                continue
            _cfp = ""
            if _cid:
                _cfp = _concept_fingerprint(db_conn, _cid)
                if _climb_state_should_skip(db_conn, _cid, _cfp):
                    stats["deferred"] += 1
                    log.debug("re_embedder.climb.skipped_cached",
                              extra={"concept": concept, "fingerprint": _cfp, "path": "whatis"})
                    _mark_whatis_row_capped(db_conn, _row_id)
                    continue
            elif _whatis_row_is_capped(db_conn, _row_id):
                # No registered entity to fingerprint (e.g. a quarantined-parent 'climb' row):
                # fall back to an OE-ROW cap/backoff so an unclassifiable concept that never
                # registers still can't re-LLM every cycle (the apparel_item runaway).
                stats["deferred"] += 1
                log.debug("re_embedder.climb.skipped_cached",
                          extra={"concept": concept, "path": "whatis_oe_capped"})
                continue

            try:
                # READ BARRIER (immediately before the blocking call — the RE-ARM case). A barrier at
                # the top of the enclosing block is NOT enough: a per-row read helper opens a FRESH
                # transaction after it, and that read then rides across this hop. Measured live on the
                # deployed image — climb_classification_chains was killed twice this way (02:42:25 and
                # 02:45:06), its whole _ont_db subsystem chain failing 'connection already closed' four
                # seconds later.
                release_read_transaction(db_conn, context="re_embedder.classify_unknown_concepts.pre_blocking_call")
                proposal = _query_llm_what_is(concept, qwen_api_url, context=_context)
            except LLMUnavailable as _unavail:
                # THE BRAIN WAS NEVER ASKED. Do NOT record a verdict, do NOT bump the
                # attempt counter, and do NOT keep sweeping — every remaining concept in
                # this batch would hit the same unavailable brain. The rows stay UNDECIDED
                # and are picked up unchanged on a later cycle.
                stats["brain_unavailable"] = stats.get("brain_unavailable", 0) + 1
                log.warning(f"re_embedder.whatis_aborted_brain_unavailable "
                            f"reason={_unavail.reason} concept={concept[:40]} "
                            f"batch_size={len(rows)} "
                            f"note=no verdict cached; batch abandoned until the brain returns")
                break
            if not proposal:
                # LLM could not classify → record 'unplaceable' so we DON'T re-LLM it every
                # cycle (the apparel_item 9x/40s runaway). The OE row stays UNDECIDED (C is the
                # last resort; the C-raw fact stays returnable) but the cache + cap/backoff now
                # gate the re-attempt — re-opened only on a fingerprint change (new info) or
                # after the backoff window while under the attempt cap.
                stats["deferred"] += 1
                if _cid:
                    _climb_state_record(db_conn, _cid, "unplaceable", "no_parent", _cfp)
                else:
                    _bump_whatis_row_attempt(db_conn, _row_id)
                log.debug(f"re_embedder.whatis_deferred concept={concept} (unclassifiable this cycle)")
                continue

            entity_type = proposal["entity_type"]
            parent = proposal.get("parent")

            # TYPE→ROOT GROUNDING (closes the GAP-1 climb-coverage hole): the LLM reliably gives a
            # TYPE but often returns parent=None for agent/role concepts ("a guitarist"/"a violinist"/
            # "a potter" → type=Person, parent=null). With no parent the concept never gets a first
            # subclass_of rung, so it is NEVER a climb leaf and NEVER reaches a seeded root — the role
            # types stall un-laddered. When the LLM proposes no intermediate parent, ground the concept
            # DIRECTLY to its TYPE's SEEDED BACKBONE ROOT by identity (Person→person, Organization→
            # organization, …): a one-rung subclass_of that TERMINATES at a seeded root. This is the
            # eager-attach ceiling — deterministic (type→root name identity, NO cosine/LLM), and the
            # seeded root is the convergence ceiling so we never overshoot. The async climb's Option-A
            # splice later inserts intermediate rungs (guitarist→musician→…→person) IF the LLM proposes
            # them; until then the concept is grounded to a real root, meeting the bar. Subject-agnostic:
            # the type→root set is the seeded hierarchical taxonomies, NO entity/role/domain literal.
            if not parent:
                _type_root = (entity_type or "").strip().lower()
                if _type_root and _type_root in _roots and _type_root != concept:
                    parent = _type_root

            # 1. TYPE the concept entity (unknown-only — never override an existing type).
            #    Resolve the concept's entity via its alias; if it isn't a registered entity
            #    yet, the type still grounds via the subclass_of edge below + on re-ingest.
            try:
                with db_conn.cursor() as cur:
                    cur.execute(
                        "UPDATE entities SET entity_type = %s"
                        "  WHERE entity_type = 'unknown'"
                        "    AND id IN (SELECT entity_id FROM entity_aliases WHERE alias = %s)",
                        (entity_type, concept),
                    )
                db_conn.commit()
            except Exception:
                try:
                    db_conn.rollback()
                except Exception:
                    pass

            # 2. GROUND with a one-rung subclass_of placement (BORN CLASS C), if a validated
            #    parent was proposed. subclass_of must be a known hierarchy rel (never invent).
            grounded = False
            # UUID-resolution failure → leave the candidate UNDECIDED to retry next cycle
            # (never write a string-keyed island, the bug this fix closes).
            _ground_deferred = False
            if parent:
                # Resolve BOTH ends to entity UUID surrogates BEFORE the write, so the is-a
                # ladder lands in the SAME UUID keyspace the hierarchy walker traverses
                # (main._resolve_type_signals joins by UUID). resolve() is idempotent (UUID v5
                # from the normalized name): the subject UUID is byte-identical to the existing
                # entity that instance edges (e.g. feels) already reference; the parent entity
                # is registered if absent. FAIL-SAFE: if we can't resolve BOTH to DISTINCT UUIDs,
                # SKIP the edge and DEFER (retry next cycle) — never fall back to display strings.
                subj_uuid = obj_uuid = None
                if not user_id:
                    _ground_deferred = True
                    log.debug(f"re_embedder.whatis_ground_deferred concept={concept} reason=no_user_id")
                else:
                    try:
                        _reg = EntityRegistry(db_conn, schema_name=schema_name)
                        subj_uuid = _reg.resolve(user_id, concept)
                        obj_uuid = _reg.resolve(user_id, parent)
                    except Exception as _re:
                        try:
                            db_conn.rollback()
                        except Exception:
                            pass
                        subj_uuid = obj_uuid = None
                        log.debug(f"re_embedder.whatis_ground_resolve_failed concept={concept} parent={parent}: {_re}")
                    if not subj_uuid or not obj_uuid or subj_uuid == obj_uuid:
                        # No two DISTINCT UUIDs (unresolvable, or would self-loop) → defer.
                        _ground_deferred = True
                if subj_uuid and obj_uuid and subj_uuid != obj_uuid:
                    # THE HARD-LINE LADDER GUARD (src/api/hardline_guard.py — FAIL-CLOSED). The
                    # what-is classifier minted 244+6 census rows on user VALUE / named-instance
                    # nodes (blue, zzcanaryglyph, krellin) because _is_named_instance is POS/edge
                    # dependent and node_role fails OPEN. The guard decides from the HARD-LINE
                    # evidence (P31 subject / attribute holder / naming surface / value-slot object
                    # / stamp corroboration / common-noun morphology of a user-asserted surface)
                    # and REFUSES on uncertainty — a memory never becomes a place.
                    _hl_refuse, _hl_reason = _ladder_hardline.refuses_subclass_rung(
                        db_conn, subj_uuid, concept, user_id=user_id)
                    if _hl_refuse:
                        stats["deferred"] += 1
                        _mark_whatis_row_capped(db_conn, _row_id)
                        log.debug("re_embedder.hardline_ladder_refused",
                                   extra={"concept": concept, "reason": _hl_reason,
                                          "lane": "whatis_classify"})
                        continue
                    try:
                        with db_conn.cursor() as cur:
                            cur.execute(
                                "SELECT is_hierarchy_rel FROM rel_types WHERE rel_type = 'subclass_of'"
                            )
                            _sc = cur.fetchone()
                        if _sc and _sc[0]:
                            # Stage Class C: <concept> subclass_of <parent>, UUID-keyed (the map
                            # slot in the SAME keyspace as instance edges — never display strings
                            # in *_id). UNIQUE-safe upsert mirrors the established staged path.
                            with db_conn.cursor() as cur:
                                cur.execute(
                                    "INSERT INTO staged_facts"
                                    "  (subject_id, object_id, rel_type, fact_class, provenance,"
                                    "   fact_provenance, confidence, confirmed_count, is_hierarchy_rel)"
                                    "  VALUES (%s, %s, 'subclass_of', 'C', 'engine_whatis_classify',"
                                    "          'llm_inferred', 0.4, 1, true)"
                                    "  ON CONFLICT (subject_id, object_id, rel_type) DO UPDATE SET"
                                    "    confirmed_count = staged_facts.confirmed_count + 1,"
                                    "    last_seen_at = now()",
                                    (subj_uuid, obj_uuid),
                                )
                            db_conn.commit()
                            grounded = True
                            stats["grounded"] += 1
                    except Exception as _ge:
                        try:
                            db_conn.rollback()
                        except Exception:
                            pass
                        log.debug(f"re_embedder.whatis_ground_failed concept={concept} parent={parent}: {_ge}")

            # 3. Mark the candidate resolved so it is not re-classified each cycle — UNLESS
            #    grounding was DEFERRED for want of a UUID (leave it UNDECIDED to retry, like the
            #    unclassifiable path above). The step-1 TYPE update is idempotent on retry.
            if _ground_deferred:
                # Couldn't resolve BOTH ends to distinct UUIDs this cycle. Leave the OE row
                # UNDECIDED to retry, but bump the OE-row attempt/backoff so an end that NEVER
                # resolves can't re-LLM forever (caps out → 'concept_unplaceable').
                stats["deferred"] += 1
                _bump_whatis_row_attempt(db_conn, _row_id)
                log.debug(f"re_embedder.whatis_ground_deferred_retry concept={concept} parent={parent}")
                continue
            try:
                with db_conn.cursor() as cur:
                    cur.execute(
                        "UPDATE ontology_evaluations SET"
                        "  re_embedder_decision = 'concept_classified',"
                        "  decision_timestamp = now(),"
                        "  candidate_object_type = %s,"
                        "  decision_reason = %s"
                        " WHERE id = %s",
                        (entity_type,
                         f"what-is classify: type={entity_type} parent={parent or 'none'} "
                         f"(grounded={grounded})",
                         _row_id),
                    )
                db_conn.commit()
            except Exception:
                try:
                    db_conn.rollback()
                except Exception:
                    pass

            # CACHE: success → 'placed' (so the climb path also skips it until new info).
            if _cid:
                _climb_state_record(db_conn, _cid, "placed", "whatis_classified", _cfp)
            stats["classified"] += 1
            log.info("re_embedder.whatis_classified",
                     extra={"concept": concept, "entity_type": entity_type,
                            "parent": parent, "grounded": grounded})
        except Exception as e:
            stats["errors"] += 1
            log.warning(f"re_embedder.whatis_concept_failed concept={concept} error={type(e).__name__}: {str(e)[:120]}")
            try:
                db_conn.rollback()
            except Exception:
                pass

    return stats


# ════════════════════════════════════════════════════════════════════════════════════════════════
# ±6 ASYNC CLASSIFICATION CLIMB — fill the middle rungs leaf->…->seeded-root, one per pass.
# ════════════════════════════════════════════════════════════════════════════════════════════════

def _seeded_backbone_roots(dsn: str, schema_name: str | None) -> set:
    """The SEEDED backbone root node-names that TERMINATE a climb (PRIMARY stop).

    Reads the per-tenant seeded HIERARCHICAL taxonomies via the taxonomy overlay
    (seed ∪ tenant, tenant-only — never public for a bound tenant) and returns the set
    of their canonical anchor names, lowercased (animal, location, …). These are
    the roots _attach_to_seeded_backbone connects leaves to; a climb that reaches one is
    DONE. Metadata-driven (NO hardcoded type/entity list); subject-agnostic — a new domain
    needs only a seeded hierarchical taxonomy row.

    UNIVERSAL TYPE BACKBONE: the seeded DOMAIN taxonomies (family/animal/location/…) cover
    only some entity classes — a tenant has no 'person'/'organization'/'object'/'concept'
    hierarchical taxonomy, so an agent/role concept (a violinist → … → person) would have NO
    seeded root to terminate at and would stall un-rooted. So we ALSO admit the CANONICAL
    entity-type names (person/animal/organization/location/object/concept) as universal
    backbone roots — the fixed ontology backbone (the SAME closed canonical set GLiNER2 types
    into and the whatis classifier emits), i.e. the seeded canonical backbone / attach ceiling
    referenced throughout. This is NOT a domain/role word list: it is the closed entity-type
    set, the universal top of the is-a backbone every type ultimately roots at.

    Fail-safe: any error / unreadable tenant → the canonical type roots alone (the climb still
    has a universal ceiling to terminate at; never crashes).
    """
    roots: set = {t.lower() for t in _CANONICAL_ENTITY_TYPES}
    if not dsn:
        return roots
    try:
        from src.api import taxonomy_overlay
        taxes = taxonomy_overlay.resolve_meta(dsn, schema_name) or {}
    except Exception as e:
        log.warning(f"re_embedder.climb_roots_resolve_failed schema={schema_name} error={str(e)[:120]}")
        return roots
    for _name, _meta in taxes.items():
        try:
            if not _meta.get("is_hierarchical"):
                continue
            n = (_name or "").strip().lower()
            if n:
                roots.add(n)
        except Exception:
            continue
    return roots


def _name_of_entity(db_conn, entity_id: str) -> Optional[str]:
    """Canonical (preferred, else any) lowercased alias of an entity UUID, or None.

    Read-only, fail-safe. Mirrors converge_hierarchy_by_identity's name resolution so the
    climb names match the convergence keyspace exactly (identity, not fuzzy)."""
    if not entity_id:
        return None
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT alias FROM entity_aliases"
                " WHERE entity_id = %s ORDER BY is_preferred DESC, alias ASC LIMIT 1",
                (entity_id,),
            )
            row = cur.fetchone()
        if row and row[0]:
            return row[0].strip().lower()
    except Exception:
        try:
            db_conn.rollback()
        except Exception:
            pass
    return None


def _type_word_of_entity(db_conn, entity_id: str) -> Optional[str]:
    """THE HARD LINE — the TYPE-bearing alias of an entity (the common noun to classify), or
    None for a PURE NAMED INSTANCE (a memory with no type-word — never enters L4).

    The climb must classify a TYPE (dog → canine → … → animal), NEVER a NAME (rex →
    fictional_character). One pet entity can carry BOTH a type-word alias (`dog`) and a proper
    name (`rex`, registered via the naming path as the OBJECT of an also_known_as/pref_name
    edge — `_NAMING_RELS`). `_name_of_entity` returns the PREFERRED alias, which for a named pet
    is `rex` → the climb wrongly classifies the NAME. This selector excludes every alias that
    is a naming-edge object (a NAME = a memory, the naming layer) and returns the surviving
    TYPE-word; ties broken by the SAME deterministic order `_name_of_entity` uses (is_preferred
    DESC, alias ASC) so the keyspace stays identity-consistent. If EVERY alias is a name (a pure
    named instance, no type-word), returns None → the caller does NOT build a subclass_of ladder
    off it (it stays instance_of whatever type it already has; names never become places).

    Deterministic, identity-not-fuzzy, subject-agnostic (no entity/type literal; naming rels are
    the fixed SKOS skos:prefLabel/skos:altLabel pair, the same invariant pinned elsewhere). The
    naming-object set is per-entity (an alias that is THIS entity's naming-object is a name of
    THIS entity). Read-only, fail-safe (None on error → caller skips, never a bad place)."""
    if not entity_id:
        return None
    naming = list(_NAMING_RELS)
    try:
        with db_conn.cursor() as cur:
            # Aliases that are NAMES of this entity: the alias text equals (case-insensitively)
            # the OBJECT-side alias of a naming edge whose SUBJECT or OBJECT is this entity.
            # Equivalently — an alias of THIS entity that is the object of a pref_name/also_known_as
            # edge pointing AT this entity. Resolved by UUID join (object_id), not string guess.
            cur.execute(
                "SELECT lower(ea.alias) FROM entity_aliases ea"
                "  WHERE ea.entity_id = %s"
                "    AND ea.entity_id IN ("
                "      SELECT object_id FROM facts"
                "        WHERE rel_type = ANY(%s)"
                "          AND superseded_at IS NULL AND archived_at IS NULL"
                "      UNION"
                "      SELECT object_id FROM staged_facts"
                "        WHERE rel_type = ANY(%s)"
                "          AND promoted_at IS NULL AND deleted_at IS NULL"
                "    )",
                (entity_id, naming, naming),
            )
            name_aliases = {r[0].strip().lower() for r in cur.fetchall() if r and r[0]}
            # All aliases, in the SAME deterministic order _name_of_entity uses.
            cur.execute(
                "SELECT alias FROM entity_aliases"
                "  WHERE entity_id = %s ORDER BY is_preferred DESC, alias ASC",
                (entity_id,),
            )
            all_aliases = [r[0].strip().lower() for r in cur.fetchall() if r and r[0]]
    except Exception:
        try:
            db_conn.rollback()
        except Exception:
            pass
        return None
    # The TYPE-word = the first (deterministic order) alias that is NOT a name. If every alias is
    # a name (pure named instance), return None — a name is a memory, NEVER an L4 chain subject.
    for al in all_aliases:
        if al and al not in name_aliases:
            return al
    return None


def _is_named_instance(db_conn, entity_id: str, user_id: str = None) -> bool:
    """THE HARD LINE — is `entity_id` a NAMED INSTANCE (a memory) rather than a TYPE (an L4 place)?

    A named instance must NEVER receive a `subclass_of` edge: a name never becomes a place. The
    test is purely STRUCTURAL (edge/graph + naming-layer membership) — NO proper-noun word list,
    NO capitalization heuristic, NO entity-name literal — so it is subject-agnostic and deterministic:

      (0) the entity IS THE USER ANCHOR (entity_id == the tenant user_id). The anchor is the
          grounded self ("I"/"me") — the one specific speaking subject, a NAMED INSTANCE by
          identity, never a TYPE. It uniquely evades (a) and (b): the self is grounded directly
          (it carries NO `instance_of` edge — "I" is never classified as an instance-of-a-type),
          and its `user` alias reads as a bare TYPE-word to `_type_word_of_entity`, so both checks
          below return "not a name" and the what-is climb mints `(user, subclass_of, role)` — the
          exact category error the founding distinction forbids. Recognizing the anchor by IDENTITY
          is subject-agnostic: `user_id` is the runtime tenant anchor — the system's sole grounding
          hook ("I/me = the user") — NOT a hardcoded subject name or word list.
      (a) the entity is the SUBJECT of a live `instance_of` edge → it is an INSTANCE *of* a type
          (e.g. `rex instance_of poodle`). An instance is classified by climbing its TYPE
          (poodle), never by giving the instance itself a `subclass_of`. This is the founding
          distinction: a named instance hangs off its type via instance_of; the subclass_of ladder
          hangs off the TYPE node, never off the instance.
      (b) `_type_word_of_entity` returns None → every alias of the entity is a naming-layer object
          (pref_name / also_known_as object — a proper name). A pure name with no type-word is a
          memory, never an L4 chain subject.

    FAIL-SAFE: on any error, return True (treat as a name → SKIP the subclass_of mint). The HARD
    LINE forbids minting subclass_of for a name, so when uncertain we skip rather than risk the
    category error — better to leave a concept un-laddered than to file a name as a place."""
    if not entity_id:
        return True
    # (0) THE USER ANCHOR — the grounded self is a named instance by identity (never a type).
    if user_id and str(entity_id) == str(user_id):
        return True
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM facts"
                "  WHERE subject_id = %s AND rel_type = 'instance_of'"
                "    AND superseded_at IS NULL AND archived_at IS NULL"
                " UNION ALL"
                " SELECT 1 FROM staged_facts"
                "  WHERE subject_id = %s AND rel_type = 'instance_of'"
                "    AND promoted_at IS NULL AND deleted_at IS NULL"
                " LIMIT 1",
                (entity_id, entity_id),
            )
            if cur.fetchone():
                return True  # (a) subject of instance_of → a named instance, not a type
    except Exception:
        try:
            db_conn.rollback()
        except Exception:
            pass
        return True  # fail-safe: uncertain → treat as a name, never mint subclass_of
    # (b) no type-word survives the naming-layer exclusion → a pure name.
    return _type_word_of_entity(db_conn, entity_id) is None


def _climb_walk_to_tip(db_conn, leaf_id: str, max_hops: int) -> tuple:
    """Walk the SINGLE vertical subclass_of/instance_of chain UP from a leaf to its tip.

    Returns (tip_entity_id, tip_name, hops_walked, hit_cap). Follows ONE parent per node
    (the chain, not the sibling fan-out — sprawl control); if a node has multiple hierarchy
    parents we take the deterministic lowest-UUID one (stable, identity-not-fuzzy). Stops at
    a node with no further hierarchy parent (the current tip) or at `max_hops` (the ±6 cap).
    Cycle-guarded. Read-only, fail-safe (returns what it walked so far)."""
    rels = list(_HIERARCHY_RELS)
    seen: set = {str(leaf_id)}
    cur_id = leaf_id
    # THE HARD LINE — the leaf's chain name must be its TYPE-word, never a proper name (a
    # name+type-merged entity would otherwise surface `rex` as the tip). Parents are pure
    # type nodes (objects of subclass_of), so _name_of_entity is correct for them.
    cur_name = _type_word_of_entity(db_conn, leaf_id) or _name_of_entity(db_conn, leaf_id)
    hops = 0
    while hops < max_hops:
        parent_id = None
        try:
            with db_conn.cursor() as cur:
                cur.execute(
                    "SELECT object_id FROM facts"
                    "  WHERE subject_id = %s AND rel_type = ANY(%s)"
                    "    AND superseded_at IS NULL AND archived_at IS NULL"
                    " UNION"
                    " SELECT object_id FROM staged_facts"
                    "  WHERE subject_id = %s AND rel_type = ANY(%s)"
                    "    AND promoted_at IS NULL AND deleted_at IS NULL"
                    " ORDER BY object_id ASC",
                    (cur_id, rels, cur_id, rels),
                )
                rows = [r[0] for r in cur.fetchall() if r[0]]
        except Exception:
            try:
                db_conn.rollback()
            except Exception:
                pass
            break
        # ONE parent — the chain, not the fan-out. Pick the first unseen (lowest-UUID).
        for r in rows:
            if str(r) not in seen:
                parent_id = r
                break
        if parent_id is None:
            break  # current node is the tip (no further hierarchy parent)
        seen.add(str(parent_id))
        cur_id = parent_id
        cur_name = _name_of_entity(db_conn, parent_id)
        hops += 1
    hit_cap = hops >= max_hops
    return cur_id, cur_name, hops, hit_cap


def _existing_depth_below(db_conn, anchor_id: str, max_walk: int = 64) -> int:
    """How many hierarchy hops ALREADY exist BELOW `anchor_id` down to its deepest descendant.

    The fact's RESIDENCE is its lowest/most-specific classification node; the ±6 bound is measured
    FROM that residence, not from wherever this pass happens to anchor. So before we place new rungs
    ABOVE an anchor we must know how far the chain already descends below it — the remaining hop
    budget is `_CLIMB_MAX_HOPS - existing_depth_below(anchor)`. Walks DOWN the existing subclass_of/
    instance_of edges (object->subject), longest path, by IDENTITY (UUID), live filters only, no
    fuzzy. Read-only, fail-safe (returns 0 on error → conservative: never INFLATES the budget)."""
    rels = list(_HIERARCHY_RELS)
    best = 0

    def _descend(node_id: str, depth: int, seen: set) -> None:
        nonlocal best
        if depth > best:
            best = depth
        if depth >= max_walk:
            return
        try:
            with db_conn.cursor() as cur:
                cur.execute(
                    "SELECT subject_id FROM facts"
                    "  WHERE object_id = %s AND rel_type = ANY(%s)"
                    "    AND superseded_at IS NULL AND archived_at IS NULL"
                    " UNION"
                    " SELECT subject_id FROM staged_facts"
                    "  WHERE object_id = %s AND rel_type = ANY(%s)"
                    "    AND promoted_at IS NULL AND deleted_at IS NULL",
                    (node_id, rels, node_id, rels),
                )
                kids = [r[0] for r in cur.fetchall() if r[0]]
        except Exception:
            try:
                db_conn.rollback()
            except Exception:
                pass
            return
        for k in kids:
            ks = str(k)
            if ks in seen:
                continue  # cycle-guard
            seen.add(ks)
            _descend(ks, depth + 1, seen)

    try:
        _descend(str(anchor_id), 0, {str(anchor_id)})
    except Exception:
        return 0
    return best


def _stage_rung(db_conn, subj_id: str, obj_id: str) -> bool:
    """INSERT ONE `<subj> subclass_of <obj>` rung, BORN CLASS B / llm_learned / engine_classify_climb.

    UUID-keyed (both ends already resolved by the caller — same keyspace the walker traverses).
    UNIQUE-safe upsert mirrors the established staged path. Caller owns commit/rollback (so a
    whole chain + the supersede are ONE atomic transaction). Returns True on a clean execute.
    Refuses a self-loop. Fail-safe (False on error; caller rolls back)."""
    if not subj_id or not obj_id or str(subj_id) == str(obj_id):
        return False
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO staged_facts"
                "  (subject_id, object_id, rel_type, fact_class, provenance,"
                "   fact_provenance, confidence, confirmed_count, is_hierarchy_rel)"
                "  VALUES (%s, %s, 'subclass_of', 'B', 'engine_classify_climb',"
                "          'llm_learned', 0.6, 1, true)"
                "  ON CONFLICT (subject_id, object_id, rel_type) DO UPDATE SET"
                "    confirmed_count = staged_facts.confirmed_count + 1, last_seen_at = now()",
                (subj_id, obj_id),
            )
        return True
    except Exception as e:
        log.debug(f"re_embedder.stage_rung_failed subj={str(subj_id)[:8]} obj={str(obj_id)[:8]}: {e}")
        return False


def _place_full_chain(
    db_conn, registry, user_id: str, leaf_id: str, leaf_name: str,
    chain: list, roots: set, old_root_name: Optional[str] = None,
    max_hops: Optional[int] = None,
) -> dict:
    """ONE-SHOT placement of the COMPLETE is-a ladder leaf->r1->r2->…->root, cycle-guarded.

    `chain` is the ordered list of snake_case parent tokens (most-specific first) from
    `_query_llm_full_chain`. We resolve each to its idempotent UUID v5 surrogate (convergence
    by identity — dog and wolf both proposing `canine` land on the SAME node) and place each
    consecutive rung as `subclass_of`, BORN CLASS B / llm_learned / engine_classify_climb.

    CYCLE-GUARD (the bug fix): before EACH rung `X subclass_of Y` we reject when Y is already a
    transitive ancestor OR descendant of X (`_is_ancestor_or_descendant`) — this kills the
    `machine <-> mechanical_device` reciprocal-parent corruption. A rejected rung is SKIPPED
    (logged) and does NOT abort the rest of the chain; we simply re-anchor from the last placed
    node to the next token.

    TERMINATION (seeded backbone is the convergence CEILING):
      • If a placed token IS a seeded backbone root we wire to it and STOP (chain grounded).
      • SEEDED-ROOT CEILING: if the LLM chain names a seeded root mid-list, the chain is truncated
        THERE before placing — overshoot rungs above it (`animal -> vertebrate -> chordate`) are
        dropped, never minted.
      • SEEDED-ROOT CONVERGENCE: if a just-placed node already sits transitively BENEATH a seeded
        root in the existing backbone (`mammal` already `subclass_of animal`), terminate THERE and
        drop the rungs the LLM proposed above it. Identity, not fuzzy.
      • SPLICE RECONNECT: when superseding a too-direct `leaf -> SEEDED ROOT` edge and the LLM chain
        never reaches that seeded root (and wasn't budget-truncated), the final placed rung is wired
        straight to the seeded root so the new ladder re-grounds under the ceiling.
    If the chain runs out WITHOUT reaching a seeded root (non-seeded domain), the final tip is the
    emergent-root / quarantine path below — never an island.

    ±6 FROM THE RESIDENCE (HARD BOUND): the fact's residence is its lowest/most-specific node, and
    the ±6 cap is measured FROM THERE — not from this pass's anchor. `max_hops` is the REMAINING
    budget (`_CLIMB_MAX_HOPS - existing_depth_below(anchor)`) the caller computed; this is what
    stops the non-physical tower (anchor already 3 deep + 6 new rungs = a 9-level chain). When the
    budget is exhausted the chain is TRUNCATED at the bound (quarantine the bounded tip), never
    extended past ±6 into the upper ontology.

    SUPERSEDE: if `old_root_name` is given (the SPLICE case — a too-direct leaf->root edge), the
    old edge is soft-superseded in the SAME transaction (facts: superseded_at+archived_at;
    staged: deleted_at — staged tombstone is deleted_at ONLY, never superseded_at/archived_at on
    a staged query) so nothing dangles. Atomic per leaf: any failure rolls the whole chain back.

    Returns {"placed": int, "cycle_skipped": int, "terminated": bool, "quarantined": bool}.
    Fail-safe: never raises; rolls back on error and returns what it attempted."""
    out = {"placed": 0, "cycle_skipped": 0, "reconfirmed": 0,
           "terminated": False, "quarantined": False}
    ln = (leaf_name or "").strip().lower()
    if not ln or not chain:
        return out

    # Resolve the ordered token chain to UUIDs up front (identity convergence). Bound to the
    # RESIDENCE-anchored remaining budget: ±6 measured from the fact's lowest node, minus the depth
    # already below this anchor. A negative/zero budget (anchor already at/over the bound) places
    # NOTHING — the chain is already as tall as it may be. Clamp to [0, _CLIMB_MAX_HOPS].
    # SEEDED-ROOT CEILING (overshoot guard, primary): the seeded backbone is the convergence
    # ceiling. If the LLM chain itself names a seeded root, TRUNCATE the chain THERE — anything the
    # LLM proposed ABOVE the seeded root (`animal -> vertebrate -> chordate`) is overshoot past the
    # backbone and is dropped before we place a single rung. Identity, not fuzzy (normalized-token
    # membership in the metadata-driven seeded-root set). The first seeded root encountered wins; we
    # keep it as the chain terminus (the loop's `t in roots` check then grounds on it).
    work_chain = list(chain)
    for _i, _tok in enumerate(work_chain):
        if (str(_tok) if _tok is not None else "").strip().lower() in roots:
            work_chain = work_chain[: _i + 1]
            break
    budget = _CLIMB_MAX_HOPS if max_hops is None else max_hops
    budget = max(0, min(int(budget), _CLIMB_MAX_HOPS))
    bounded = work_chain[:budget]
    # Whether the LLM's chain was longer than our budget allowed → we are TRUNCATING at the ±6
    # bound, so the bounded tip is NOT a real root: it must be quarantined (handled at chain-top
    # termination below), never silently terminated as if grounded. (Measured against the
    # seeded-root-truncated work_chain so a legit stop-at-root is NOT mistaken for a budget cut.)
    truncated_by_budget = len(work_chain) > len(bounded)
    # SPLICE RECONNECT: when we are superseding a too-direct `leaf -> SEEDED ROOT` edge and the LLM
    # chain does NOT itself reach that (or any) seeded root, the new ladder would orphan from the
    # seeded ceiling. We RECONNECT the final placed rung to that seeded root at the end so the chain
    # stays grounded under the backbone (deterministic; only when old_root_name is a seeded root).
    splice_to_seeded = bool(old_root_name) and (old_root_name or "").strip().lower() in roots
    try:
        cur_id = leaf_id
        cur_name = ln
        last_placed_id = None
        last_placed_name = None
        reached_root = False
        for tok in bounded:
            t = (tok or "").strip().lower()
            if not t or t == cur_name:
                continue
            try:
                nxt_id = registry.resolve(user_id, t)
            except Exception as _re:
                log.debug(f"re_embedder.full_chain_resolve_failed token={t}: {_re}")
                try:
                    db_conn.rollback()
                except Exception:
                    pass
                return out
            if not nxt_id or str(nxt_id) == str(cur_id):
                continue
            # RELEVANCE GATE (before the cycle-guard): the one-shot chain is re-asked only when
            # `_climb_state_should_skip` re-opened this concept — new evidence changed its
            # fingerprint — and the LLM returning the SAME ladder on new evidence is the single
            # strongest confirmation this engine produces. Today every such rung dies as
            # `cycle_skipped`, because the rung's own presence makes its parent reachable. Count
            # it and CLIMB ON (advance to nxt_id) so the rungs ABOVE are re-confirmed too;
            # skipping without advancing would strand the walk at the leaf. No commit here — the
            # caller owns the transaction so the whole chain stays atomic.
            if _rung_exists(db_conn, cur_id, nxt_id, "subclass_of"):
                if _reconfirm_existing_rung(db_conn, cur_id, nxt_id, "subclass_of", commit=False):
                    out["reconfirmed"] = out.get("reconfirmed", 0) + 1
                last_placed_id, last_placed_name = nxt_id, t
                cur_id, cur_name = nxt_id, t
                if t in roots:
                    reached_root = True
                    out["terminated"] = True
                    break
                continue
            # CYCLE-GUARD: reject `cur subclass_of nxt` if nxt is already an ancestor/descendant
            # of cur. Skip this rung (don't abort) and re-anchor from the same node to the next.
            if _is_ancestor_or_descendant(db_conn, cur_id, nxt_id):
                out["cycle_skipped"] += 1
                log.info(f"re_embedder.climb_cycle_rejected child={cur_name} parent={t} "
                         f"reason=already_ancestor_or_descendant")
                continue
            if not _stage_rung(db_conn, cur_id, nxt_id):
                # A single rung failing to stage rolls the whole chain back (atomic per leaf).
                try:
                    db_conn.rollback()
                except Exception:
                    pass
                return out
            out["placed"] += 1
            last_placed_id, last_placed_name = nxt_id, t
            cur_id, cur_name = nxt_id, t
            if t in roots:
                reached_root = True
                out["terminated"] = True
                break  # PRIMARY termination — grounded at a seeded root.
            # SEEDED-ROOT CONVERGENCE (overshoot guard): the just-placed node is NOT itself a
            # seeded root by NAME, but it already sits transitively BENEATH one in the existing
            # backbone (e.g. `mammal` is already `subclass_of animal`, and `animal` is seeded).
            # The seeded backbone is the convergence CEILING — terminate HERE and DROP whatever the
            # LLM proposed above (`vertebrate -> chordate`). Identity, not fuzzy (UUID walk +
            # normalized-name match against the metadata-driven seeded-root set).
            if _node_under_seeded_root(db_conn, nxt_id, roots):
                reached_root = True
                out["terminated"] = True
                log.info("re_embedder.climb_converged_on_seeded_backbone",
                         extra={"leaf": ln, "stopped_at": t})
                break  # convergence termination — node already grounded under the seeded ceiling.

        # SPLICE RECONNECT: we superseded a too-direct `leaf -> SEEDED ROOT` edge but the LLM ladder
        # did NOT reach a seeded root and was NOT budget-truncated — so the new tip would orphan from
        # the seeded backbone. Wire the final placed rung straight to the seeded root (cycle-guarded)
        # so the chain re-grounds under the ceiling instead of towering off into `chordate`. This is
        # the deterministic guarantee that the spliced chain converges on the seeded backbone.
        if (splice_to_seeded and out["placed"] > 0 and not reached_root
                and not truncated_by_budget and last_placed_id is not None):
            try:
                seeded_uuid = registry.resolve(user_id, (old_root_name or "").strip().lower())
            except Exception:
                seeded_uuid = None
            if (seeded_uuid and str(seeded_uuid) != str(last_placed_id)
                    and not _is_ancestor_or_descendant(db_conn, last_placed_id, seeded_uuid)):
                if _stage_rung(db_conn, last_placed_id, seeded_uuid):
                    out["placed"] += 1
                    reached_root = True
                    out["terminated"] = True
                    log.info("re_embedder.climb_reconnected_to_seeded_root",
                             extra={"leaf": ln, "tip": last_placed_name,
                                    "seeded_root": (old_root_name or "").strip().lower()})

        if out["placed"] == 0:
            # Nothing NEW to place → no supersede. But "nothing new" is not "nothing happened":
            # if the LLM re-derived rungs that already exist, those RE-CONFIRMATIONS are the
            # relevance signal and must survive. Commit them; roll back only a genuinely empty
            # pass (all rungs cycle-skipped / proper-name rejected).
            try:
                if out.get("reconfirmed", 0) > 0:
                    db_conn.commit()
                    log.info("re_embedder.climb_full_chain_reconfirmed",
                             extra={"leaf": ln, "rungs_reconfirmed": out["reconfirmed"],
                                    "cycle_skipped": out["cycle_skipped"]})
                else:
                    db_conn.rollback()
            except Exception:
                try:
                    db_conn.rollback()
                except Exception:
                    pass
            return out

        # SUPERSEDE the old too-direct leaf->root edge (SPLICE case only), same transaction.
        if old_root_name:
            rels = list(_HIERARCHY_RELS)
            try:
                old_root_uuid = registry.resolve(user_id, old_root_name)
            except Exception:
                old_root_uuid = None
            if old_root_uuid and str(old_root_uuid) != str(leaf_id):
                with db_conn.cursor() as cur:
                    cur.execute(
                        "UPDATE facts SET superseded_at = now(), archived_at = now(), qdrant_synced = false"
                        "  WHERE subject_id = %s AND object_id = %s AND rel_type = ANY(%s)"
                        "    AND superseded_at IS NULL AND archived_at IS NULL",
                        (leaf_id, old_root_uuid, rels),
                    )
                    # staged tombstone = deleted_at ONLY (NEVER superseded_at/archived_at on a
                    # staged query — that was the bug we fixed). deleted_at removes it from the
                    # live walk/recall while staying recoverable (non-destructive, not hard-delete).
                    cur.execute(
                        "UPDATE staged_facts SET deleted_at = now(), qdrant_synced = false"
                        "  WHERE subject_id = %s AND object_id = %s AND rel_type = ANY(%s)"
                        "    AND promoted_at IS NULL AND deleted_at IS NULL",
                        (leaf_id, old_root_uuid, rels),
                    )

        db_conn.commit()
        log.info("re_embedder.climb_full_chain_placed",
                 extra={"leaf": ln, "rungs_placed": out["placed"],
                        "cycle_skipped": out["cycle_skipped"],
                        "terminated_at_root": reached_root,
                        "superseded_direct": bool(old_root_name)})
    except Exception as e:
        try:
            db_conn.rollback()
        except Exception:
            pass
        log.debug(f"re_embedder.full_chain_failed leaf={ln}: {e}")
        return {"placed": 0, "cycle_skipped": 0, "reconfirmed": 0,
                "terminated": False, "quarantined": False}

    # CHAIN-TOP TERMINATION (non-physical domains): the chain ran out without reaching a
    # PRE-SEEDED root, but the LLM's OWN top category is where the is-a ladder genuinely tops
    # out (anxiety -> [fear, emotion]; emotion has no real parent). L4 asks "can we classify
    # this into a real category," NOT "is it physical" — so an emergent top-level category is a
    # VALID L4 placement, not a quarantine. We GATE it through the SAME rung-4 validator the
    # seeded roots implicitly satisfy: a genuine category token (not scalar, not a loose phrase,
    # and NOT a universal upper-ontology catch-all — thing/entity/object/concept/item/stuff are
    # rejected by _validate_bridge_placement). Convergence by identity: every chain reaching the
    # same top token (`emotion`) landed on the SAME UUID node above (registry.resolve), so this
    # is a self-assembling non-physical backbone — no fuzzy, no cosine.
    #
    # QUARANTINE remains ONLY for the legitimate "couldn't ladder to a real category yet" case
    # (the top is a catch-all / scalar / phrase → validator rejects it → retry via whatis).
    if out["placed"] and not out["terminated"] and last_placed_name and last_placed_name not in roots:
        # ±6 BOUND HIT: the LLM chain was longer than the residence-anchored budget, so the tip we
        # stopped at is NOT the real top — it's just where we ran out of budget. QUARANTINE it (do
        # NOT terminate as grounded); a later pass continues from a real residence-anchored budget
        # if the chain is genuinely longer, or the whatis path reclassifies. This is the hard ±6
        # stop that prevents the abstraction tower from being mistaken for a grounded root.
        if truncated_by_budget:
            _quarantine_climb_tip(db_conn, last_placed_id, last_placed_name)
            out["quarantined"] = True
            log.info("re_embedder.climb_quarantined_at_hop_bound",
                     extra={"leaf": ln, "tip": last_placed_name,
                            "rungs_placed": out["placed"], "max_hops": budget})
            return out
        _top_ok, _top_why = _validate_bridge_placement(
            last_placed_name, last_placed_name, last_placed_name,
            child_a_type="", child_b_type="",
        )
        # _validate_bridge_placement rejects lca==child (self-bridge) with "lca_equals_child";
        # here child==lca by construction (we validate the tip AS a candidate root), so that
        # specific reason is expected and is NOT a real failure — only the catch-all/scalar/
        # phrase rejections (the genuine-category guards) block emergent-root termination.
        if _top_ok or _top_why == "lca_equals_child":
            out["terminated"] = True
            log.info("re_embedder.climb_terminated_at_chain_top",
                     extra={"leaf": ln, "emergent_root": last_placed_name,
                            "rungs_placed": out["placed"]})
        else:
            _quarantine_climb_tip(db_conn, last_placed_id, last_placed_name)
            out["quarantined"] = True
            log.info("re_embedder.climb_quarantined_non_category_top",
                     extra={"leaf": ln, "tip": last_placed_name, "reason": _top_why})
    return out


def climb_classification_chains(
    db_conn, qwen_api_url: str, user_id: str = None, schema_name: str = None
) -> dict:
    """±6 ASYNC CLIMB + OPTION-A SPLICE: deepen is-a chains ONE rung per pass toward a seeded root.

    TWO advance modes per laddered concept (the SHARED hierarchy mechanism for BOTH engine-ingest
    AND /expand — they write identical hierarchy edges into facts/staged_facts and flow through
    THIS one path; the only difference is per-row `fact_provenance`):

      (A) SPLICE a too-direct edge. The eager leaf-anchor attaches a concept DIRECTLY to a far
          seeded ROOT (`dog subclass_of animal`), skipping the real intermediate rungs. When a
          leaf has a 1-hop edge straight to a seeded root (and is not itself a root), ask the LLM
          ONCE for the COMPLETE is-a ladder (`_query_llm_full_chain`: dog → canine → canidae →
          carnivora → mammal → animal) and place EVERY rung in ONE pass (`_place_full_chain`),
          cycle-guarded, terminating at the seeded root and SUPERSEDING the old too-direct edge
          atomically (soft — never dangling, never hard-deleted). FALLBACK: if the one-shot chain
          yields nothing usable, the legacy single-rung splice (`_query_llm_what_is` → immediate
          parent → `_splice_intermediate_rung`) runs instead.

      (B) CLIMB a non-root-tipped chain. For a chain whose TIP is not yet a seeded root (within
          the ±6 hop budget), ask the LLM ONCE for the COMPLETE remaining ladder above the tip and
          place it in ONE pass (cycle-guarded; terminate at a seeded root, else quarantine the
          final tip). FALLBACK: a single-rung "what is <tip>?" whose proposed parent must resolve
          BY IDENTITY to an EXISTING backbone node; else MINT-AND-QUARANTINE — NEVER auto-place.

    WHY ONE-SHOT: rung-by-rung STALLS (qwen returns `dog → canine` but refuses `canine → ?` asked
    one at a time, yet returns the whole taxonomy in a single shot), so the primary path asks ONCE
    and places all rungs. CYCLE-GUARD (`_is_ancestor_or_descendant`) rejects any rung that would
    close a loop (the live `machine <-> mechanical_device` reciprocal-parent corruption); a rejected
    rung is skipped and does not abort the rest of the chain.

    Terminates a chain when the tip IS a seeded root (PRIMARY) or the ±6 hop cap is hit
    (BACKSTOP → quarantine, stop generating). Builds ONLY the single vertical path (no sideways
    siblings). Grown rungs are born CLASS B at the CORRECTABLE MID-TIER (fact_provenance
    'llm_learned', rank 2: below user_stated, above llm_inferred) — durable + promotable, and a
    user statement always supersedes them. UUID-keyed (same keyspace the walker traverses).
    Convergence across branches is handled separately by converge_hierarchy_by_identity.

    Async background work: bounded (_CLIMB_BATCH_LIMIT), fail-safe (never raises — must not
    crash the re_embedder loop), per-tenant (caller has SET search_path TO {schema}, NO public).
    Returns stats dict.
    """
    stats = {"climbed": 0, "terminated": 0, "quarantined": 0, "deferred": 0,
             "skipped_cached": 0, "errors": 0, "reconfirmed": 0}
    if not _ENGINE_CLASSIFY_CLIMB:
        return stats
    if not user_id or not schema_name:
        return stats  # need a bound tenant + user to resolve UUIDs / overlay

    # DEFENSIVE ENTRY-ROLLBACK (same rationale as classify_unknown_concepts): the climb runs LAST on
    # the shared poll-loop connection, so an aborted txn inherited from an earlier subsystem makes the
    # subclass_of check below fail "current transaction is aborted" and the climb returns early EVERY
    # cycle — the un-laddered concepts never deepen. Clear inherited aborted state up front. Fail-safe.
    _rollback_and_reapply_search_path(db_conn, schema_name)

    # LEVER 2 (GRACE TO INTENT): break any hierarchy 2-CYCLES (A⊆B ∧ B⊆A — duplicate roots like
    # `vulnerability` ⇄ `security_vulnerability`) BEFORE we walk/climb, so the cycle-guard no longer
    # rejects good rungs (`cycle_rejected`) and islands the chain. Deterministic identity merge
    # (survivor = higher-evidence node); fail-safe (never raises). Runs FIRST each pass so the climb
    # below advances onto the single collapsed survivor node this same cycle.
    try:
        _cyc = collapse_hierarchy_2cycles(db_conn, schema_name=schema_name)
        if _cyc.get("cycles_collapsed", 0) > 0:
            log.info(f"re_embedder.climb_precollapse schema={schema_name} "
                     f"cycles_collapsed={_cyc['cycles_collapsed']} "
                     f"edges_repointed={_cyc['edges_repointed']}")
    except Exception as e:
        _rollback_and_reapply_search_path(db_conn, schema_name)
        log.warning(f"re_embedder.climb_precollapse_failed schema={schema_name} "
                    f"error={type(e).__name__}: {str(e)[:120]}")

    dsn = os.environ.get("POSTGRES_DSN", "")
    roots = _seeded_backbone_roots(dsn, schema_name)

    # subclass_of must be a known hierarchy rel; never invent it.
    try:
        with db_conn.cursor() as cur:
            cur.execute("SELECT is_hierarchy_rel FROM rel_types WHERE rel_type = 'subclass_of'")
            _sc = cur.fetchone()
        if not _sc or not _sc[0]:
            return stats
    except Exception as e:
        log.error(f"re_embedder.climb_subclass_check_failed: {e}")
        try:
            db_conn.rollback()
        except Exception:
            pass
        return stats

    # Candidate leaves: distinct SUBJECTS of a hierarchy edge (the laddered concepts). A
    # concept that is itself the chain tip will simply have no parent and we climb FROM it.
    rels = list(_HIERARCHY_RELS)
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT subject_id FROM facts"
                "  WHERE rel_type = ANY(%s) AND superseded_at IS NULL AND archived_at IS NULL"
                " UNION"
                " SELECT DISTINCT subject_id FROM staged_facts"
                "  WHERE rel_type = ANY(%s) AND promoted_at IS NULL AND deleted_at IS NULL",
                (rels, rels),
            )
            leaves = [r[0] for r in cur.fetchall() if r[0]]
        # READ BARRIER: co-instance grounding and the per-leaf climb below are both LLM lanes.
        release_read_transaction(
            db_conn, context=f"re_embedder.climb_classification_chains.leaves schema={schema_name}")
    except Exception as e:
        log.error(f"re_embedder.climb_fetch_leaves_failed: {e}")
        try:
            db_conn.rollback()
        except Exception:
            pass
        return stats

    if not leaves:
        return stats

    try:
        _reg = EntityRegistry(db_conn, schema_name=schema_name)
    except Exception as e:
        log.error(f"re_embedder.climb_registry_failed: {e}")
        return stats

    # CO-INSTANCE SPECIFICITY (the real breed miss, ingest-side): when the SAME named instance is
    # `instance_of` two type nodes the eager anchor left as SIBLINGS under the seeded root
    # (poodle & dog both `subclass_of animal`), ground the more-specific type UNDER the more-general
    # (`poodle subclass_of dog`) so the shipped most-specific recall collapse has a ladder to act on.
    # Runs BEFORE the per-leaf splice so the subsequent climb sees the already-grounded ordering.
    # Bounded, cache-gated, fail-safe (never crashes the loop).
    try:
        # READ BARRIER (immediately before the blocking call — the RE-ARM case). A barrier at
        # the top of the enclosing block is NOT enough: a per-row read helper opens a FRESH
        # transaction after it, and that read then rides across this hop. Measured live on the
        # deployed image — climb_classification_chains was killed twice this way (02:42:25 and
        # 02:45:06), its whole _ont_db subsystem chain failing 'connection already closed' four
        # seconds later.
        release_read_transaction(db_conn, context="re_embedder.climb_classification_chains.pre_blocking_call")
        _coi = _ground_coinstance_specificity(db_conn, qwen_api_url, user_id, roots)
        if _coi.get("grounded", 0) > 0:
            log.info(f"re_embedder.coinstance_specificity schema={schema_name} "
                     f"grounded={_coi['grounded']} undetermined={_coi['undetermined']} "
                     f"skipped_cached={_coi['skipped_cached']}")
    except LLMUnavailable as _unavail:
        # Brain never reached — this lane grounds nothing rather than grounding on absence.
        _rollback_and_reapply_search_path(db_conn, schema_name)
        log.warning(f"re_embedder.coinstance_specificity_deferred schema={schema_name} "
                    f"reason={_unavail.reason} note=brain unavailable; nothing grounded")
    except Exception as e:
        _rollback_and_reapply_search_path(db_conn, schema_name)
        log.warning(f"re_embedder.coinstance_specificity_failed schema={schema_name} "
                    f"error={type(e).__name__}: {str(e)[:120]}")

    advanced = 0
    for leaf_id in leaves:
        # READ BARRIER (per iteration): this loop body blocks on the brain/Qdrant, and a
        # read left open by the PREVIOUS iteration would ride across it. A batch-level
        # barrier alone does not cover this — measured live: climb_state and the
        # taxonomy reads were each caught idle-in-transaction at 58-59s inside a loop.
        release_read_transaction(db_conn, context="re_embedder.climb_classification_chains.iteration")
        if advanced >= _CLIMB_BATCH_LIMIT:
            break
        try:
            # THE HARD LINE — classify the TYPE, never a NAME. The climb subject MUST be the
            # entity's TYPE-word alias (dog), not its proper name (rex, the preferred alias
            # _name_of_entity would return). `_type_word_of_entity` excludes every naming-edge
            # object (a name = a memory) and returns the type-word; None for a PURE named instance
            # (no type-word) — which we SKIP entirely: a name never becomes an L4 chain subject, it
            # stays instance_of whatever type it already has. Names stay in the naming layer.
            # PRIMARY HARD-LINE guard: a NAMED INSTANCE (subject of a live instance_of edge, e.g.
            # `rex instance_of poodle`) must NEVER be climbed. _type_word_of_entity alone MISSES
            # this when the also_known_as object is a SEPARATE entity — the name alias then survives
            # as a false "type-word" and the leaf gets handed to the LLM, which (live) classified the
            # NAME into `rex subclass_of fictional_character`. _is_named_instance closes the gap
            # via the instance_of-subject check (committed Class A by climb time → no race).
            if _is_named_instance(db_conn, leaf_id, user_id=user_id):
                stats["deferred"] += 1
                continue
            leaf_name = _type_word_of_entity(db_conn, leaf_id)
            if not leaf_name:
                # Pure named instance (only proper-name aliases) → not an L4 type. Do NOT build a
                # subclass_of ladder off a name; leave it as-is (a memory, not a place).
                stats["deferred"] += 1
                continue

            # ── CACHE READ (DB = cache) — skip BEFORE any LLM call ──────────────────
            # Compute the cheap deterministic input fingerprint, then honour a cached verdict:
            # a 'placed' / capped-or-backed-off 'unplaceable' on the SAME fingerprint is skipped
            # (no LLM). Re-open only on a fingerprint change (additive new info) or an under-cap
            # 'unplaceable' past its backoff window. This is the actual loop-killer.
            _fp = _concept_fingerprint(db_conn, leaf_id)
            if _climb_state_should_skip(db_conn, leaf_id, _fp):
                stats["skipped_cached"] += 1
                log.debug("re_embedder.climb.skipped_cached",
                          extra={"leaf": leaf_name, "fingerprint": _fp})
                continue

            # ── OPTION A — SPLICE INTERMEDIATE RUNGS ────────────────────────────────
            # The eager leaf-anchor (_attach_to_seeded_backbone, main.py) attaches a
            # concept DIRECTLY to a far seeded ROOT: `dog subclass_of animal`. Such a
            # "too-direct" edge (a leaf that, in ONE hop, jumps straight to a seeded root,
            # while the leaf itself is NOT a root) skips the real biological/ontological
            # rungs. We INSERT the missing immediate rung and RE-PARENT so the chain
            # deepens dog→canine→…→animal, superseding the old direct edge (never dangling,
            # never hard-deleted). ONE rung spliced per pass; the next pass continues
            # upward from the new intermediate until a seeded root or the ±6 backstop.
            direct_root = _leaf_direct_root_edge(db_conn, leaf_id, leaf_name, roots)
            if direct_root is not None:
                if advanced >= _CLIMB_BATCH_LIMIT:
                    break
                # ONE-SHOT FULL CHAIN (PRIMARY): ask the LLM ONCE for the COMPLETE is-a ladder
                # (dog → canine → canidae → carnivora → mammal → animal) and place EVERY rung in
                # one pass, cycle-guarded, superseding the too-direct edge atomically. This fixes
                # the rung-by-rung STALL (qwen returns the whole taxonomy in one shot but refuses
                # the next rung asked one at a time).
                # READ BARRIER (immediately before the blocking call — the RE-ARM case). A barrier at
                # the top of the enclosing block is NOT enough: a per-row read helper opens a FRESH
                # transaction after it, and that read then rides across this hop. Measured live on the
                # deployed image — climb_classification_chains was killed twice this way (02:42:25 and
                # 02:45:06), its whole _ont_db subsystem chain failing 'connection already closed' four
                # seconds later.
                release_read_transaction(db_conn, context="re_embedder.climb_classification_chains.pre_blocking_call")
                full_chain = _ask_brain(_query_llm_full_chain, leaf_name, qwen_api_url,
                                        stats=stats, what="climb_splice_full_chain")
                if full_chain is _BRAIN_UNAVAILABLE:
                    break
                advanced += 1
                if full_chain:
                    # ±6 FROM THE RESIDENCE: the budget for NEW rungs above this anchor is 6 minus
                    # the depth already below it (the chain's existing descent toward its residence).
                    # In the splice case the anchor IS the leaf, so depth-below is usually 0 — but a
                    # leaf that is itself mid-chain still gets the residence-correct budget.
                    _budget = _CLIMB_MAX_HOPS - _existing_depth_below(db_conn, leaf_id)
                    fc = _place_full_chain(
                        db_conn, _reg, user_id, leaf_id, leaf_name, full_chain,
                        roots, old_root_name=direct_root, max_hops=_budget,
                    )
                    # RE-CONFIRMATION IS A SUCCESSFUL OUTCOME. A chain whose rungs all already
                    # exist is grounded — the leaf IS placed. Treating that as failure sent it
                    # down the single-rung fallback (a second LLM call) and then recorded
                    # 'unplaceable', burning an attempt against the cap for a concept that is
                    # correctly laddered. Count it as placed and stop.
                    if fc.get("placed", 0) > 0 or fc.get("reconfirmed", 0) > 0:
                        stats["climbed"] += 1
                        stats["reconfirmed"] = stats.get("reconfirmed", 0) + fc.get("reconfirmed", 0)
                        if fc.get("terminated"):
                            stats["terminated"] += 1
                        if fc.get("quarantined"):
                            stats["quarantined"] += 1
                        # CACHE: chain placed → 'placed' (reaching the backbone counts even if the
                        # final tip quarantined — the leaf itself is now grounded upward).
                        _climb_state_record(db_conn, leaf_id, "placed", "placed", _fp)
                        continue
                    # Full-chain placed nothing (all rungs cycle-skipped / proper-name) → fall
                    # through to the single-rung splice FALLBACK below.

                # SINGLE-RUNG SPLICE (FALLBACK): LLM PROPOSES the leaf's IMMEDIATE parent only
                # (dog → canine). Used when the one-shot full chain returned nothing usable.
                # READ BARRIER (immediately before the blocking call — the RE-ARM case). A barrier at
                # the top of the enclosing block is NOT enough: a per-row read helper opens a FRESH
                # transaction after it, and that read then rides across this hop. Measured live on the
                # deployed image — climb_classification_chains was killed twice this way (02:42:25 and
                # 02:45:06), its whole _ont_db subsystem chain failing 'connection already closed' four
                # seconds later.
                release_read_transaction(db_conn, context="re_embedder.climb_classification_chains.pre_blocking_call")
                proposal = _ask_brain(_query_llm_what_is, leaf_name, qwen_api_url,
                                      stats=stats, what="climb_splice_single_rung")
                if proposal is _BRAIN_UNAVAILABLE:
                    break
                inter = ((proposal or {}).get("parent") or "").strip().lower() if proposal else ""
                if not inter or inter == leaf_name or inter == direct_root:
                    # No genuine intermediate (leaf is already one rung below the root, or the
                    # LLM proposed the root itself) → the direct edge is legitimately correct.
                    # CACHE: 'placed' — the existing direct edge IS its correct grounding.
                    stats["deferred"] += 1
                    _climb_state_record(db_conn, leaf_id, "placed", "no_intermediate", _fp)
                    continue
                # IDENTITY GATE: the intermediate must be placeable as a CANONICAL node — it
                # either already resolves to a backbone node (convergence by identity), OR it is
                # a clean category token we can MINT as a new node wired to the former root parent
                # (kept walkable: <inter> subclass_of <root>, so never an island). Unresolved /
                # non-category proposals are mint-and-quarantined, NEVER auto-placed. No fuzzy.
                if _splice_intermediate_rung(
                    db_conn, _reg, user_id, leaf_id, leaf_name, inter, direct_root, roots
                ):
                    stats["climbed"] += 1
                    _climb_state_record(db_conn, leaf_id, "placed", "spliced", _fp)
                else:
                    # Mint-and-quarantined / unresolvable intermediate → unplaceable (capped).
                    stats["deferred"] += 1
                    _climb_state_record(db_conn, leaf_id, "unplaceable", "cycle_rejected", _fp)
                continue

            tip_id, tip_name, hops, hit_cap = _climb_walk_to_tip(db_conn, leaf_id, _CLIMB_MAX_HOPS)
            if not tip_name:
                continue
            # PRIMARY termination: the chain tip is already a seeded backbone root.
            if tip_name in roots:
                stats["terminated"] += 1
                # CACHE: already grounded to the backbone → 'placed', no LLM ever needed again
                # (until new info changes the fingerprint).
                _climb_state_record(db_conn, leaf_id, "placed", "rooted", _fp)
                continue
            # BACKSTOP termination: ±6 hop cap reached without a seeded root. LEVER 1 (GRACE TO
            # INTENT, over ISLANDS): if the tip the walk reached is itself a COHERENT GROWN ROOT (a
            # genuine, already-grounded category node — passes the same rung-4 category gate the
            # seeded roots satisfy, is NOT a named instance, NOT a no-info upper placeholder), TERM-
            # INATE GRACEFULLY there: the chain is placed + walkable, it just tops out at a GROWN
            # root (minecraft→…→program, program is a real category) instead of a SEEDED one — a
            # coherent standalone grouping is first-class. Only a NON-coherent tip (junk / no-info
            # tower) still QUARANTINES for later what-is reclassify. Structural/identity, no cosine.
            if hit_cap:
                if _is_coherent_grown_root(db_conn, tip_id, tip_name, roots):
                    stats["terminated"] += 1
                    _climb_state_record(db_conn, leaf_id, "placed", "grown_root", _fp)
                    log.info("re_embedder.climb_terminated_at_grown_root",
                             extra={"leaf": leaf_name, "grown_root": tip_name, "at": "hop_cap"})
                    continue
                _quarantine_climb_tip(db_conn, tip_id, tip_name)
                stats["quarantined"] += 1
                _climb_state_record(db_conn, leaf_id, "unplaceable", "cap_hit", _fp)
                continue

            # ONE-SHOT FULL CHAIN from the tip (PRIMARY): ask the LLM ONCE for the COMPLETE
            # remaining ladder above the tip and place EVERY rung in one pass (cycle-guarded,
            # terminate at a seeded root, else quarantine the final tip). No too-direct edge to
            # supersede here (old_root_name=None) — we are EXTENDING an un-grounded tip upward.
            # READ BARRIER (immediately before the blocking call — the RE-ARM case). A barrier at
            # the top of the enclosing block is NOT enough: a per-row read helper opens a FRESH
            # transaction after it, and that read then rides across this hop. Measured live on the
            # deployed image — climb_classification_chains was killed twice this way (02:42:25 and
            # 02:45:06), its whole _ont_db subsystem chain failing 'connection already closed' four
            # seconds later.
            release_read_transaction(db_conn, context="re_embedder.climb_classification_chains.pre_blocking_call")
            full_chain = _ask_brain(_query_llm_full_chain, tip_name, qwen_api_url,
                                    stats=stats, what="climb_tip_full_chain")
            if full_chain is _BRAIN_UNAVAILABLE:
                break
            advanced += 1
            if full_chain:
                # ±6 FROM THE RESIDENCE: the tip sits `hops` above the residence-leaf, so its
                # existing depth-below already counts those hops. Remaining budget for NEW rungs
                # above the tip = 6 minus that depth → leaf-to-top can NEVER exceed ±6 (this is the
                # bug fix: previously the tip got a FRESH 6-rung budget, so leaf→tip(6)+tip→top(6)
                # could tower to 12 / the 9-level anxiety chain).
                _budget = _CLIMB_MAX_HOPS - _existing_depth_below(db_conn, tip_id)
                fc = _place_full_chain(
                    db_conn, _reg, user_id, tip_id, tip_name, full_chain, roots,
                    old_root_name=None, max_hops=_budget,
                )
                # Re-confirmation counts as placement — see the note at the splice caller above.
                if fc.get("placed", 0) > 0 or fc.get("reconfirmed", 0) > 0:
                    stats["climbed"] += 1
                    stats["reconfirmed"] = stats.get("reconfirmed", 0) + fc.get("reconfirmed", 0)
                    if fc.get("terminated"):
                        stats["terminated"] += 1
                    if fc.get("quarantined"):
                        stats["quarantined"] += 1
                    _climb_state_record(db_conn, leaf_id, "placed", "placed", _fp)
                    continue
                # else fall through to the single-rung climb FALLBACK below.

            # SINGLE-RUNG CLIMB (FALLBACK): ask the LLM "what is <tip>?" (proposes a parent).
            # READ BARRIER (immediately before the blocking call — the RE-ARM case). A barrier at
            # the top of the enclosing block is NOT enough: a per-row read helper opens a FRESH
            # transaction after it, and that read then rides across this hop. Measured live on the
            # deployed image — climb_classification_chains was killed twice this way (02:42:25 and
            # 02:45:06), its whole _ont_db subsystem chain failing 'connection already closed' four
            # seconds later.
            release_read_transaction(db_conn, context="re_embedder.climb_classification_chains.pre_blocking_call")
            proposal = _ask_brain(_query_llm_what_is, tip_name, qwen_api_url,
                                  stats=stats, what="climb_tip_single_rung")
            if proposal is _BRAIN_UNAVAILABLE:
                break
            if not proposal or not proposal.get("parent"):
                # LLM had no genuine parent (already general) → leave it; converge/decay
                # handle the rest. Not an error. CACHE 'unplaceable'/no_parent — re-open only
                # when new info (fingerprint) arrives or the backoff window elapses (under cap).
                stats["deferred"] += 1
                _climb_state_record(db_conn, leaf_id, "unplaceable", "no_parent", _fp)
                continue
            parent = (proposal.get("parent") or "").strip().lower()
            if not parent or parent == tip_name:
                stats["deferred"] += 1
                _climb_state_record(db_conn, leaf_id, "unplaceable", "no_parent", _fp)
                continue

            # IDENTITY GATE: accept the proposed parent ONLY if it resolves to an EXISTING
            # backbone node — either a seeded root, or an entity that already participates as
            # a hierarchy node (object of a hierarchy edge). Convergence is by identity: the
            # parent UUID is idempotent (UUID v5 of the normalized name) so two branches
            # reaching the same canonical name land on the same node. NO cosine / fuzzy.
            if _parent_resolves_to_backbone(db_conn, parent, roots):
                if _place_climb_rung(db_conn, _reg, user_id, tip_id, tip_name, parent):
                    stats["climbed"] += 1
                    _climb_state_record(db_conn, leaf_id, "placed", "rung_placed", _fp)
                else:
                    stats["deferred"] += 1
                    _climb_state_record(db_conn, leaf_id, "unplaceable", "cycle_rejected", _fp)
            else:
                # Proposed parent is NOT (yet) a backbone node. LEVER 1 (GRACE TO INTENT, over
                # ISLANDS): the LLM (full-chain AND single-rung) could not connect this tip to the
                # seeded backbone — so if the TIP ITSELF is a COHERENT GROWN ROOT (genuine, grounded
                # category, not a named instance / no-info placeholder), TERMINATE GRACEFULLY at it:
                # a coherent standalone grouping is first-class, placed + walkable, topping out at a
                # grown root. Cache 'placed' so we stop re-LLMing forever; a later growth that mints
                # the real parent changes the fingerprint and re-opens the climb (identity).
                if _is_coherent_grown_root(db_conn, tip_id, tip_name, roots):
                    stats["terminated"] += 1
                    _climb_state_record(db_conn, leaf_id, "placed", "grown_root", _fp)
                    log.info("re_embedder.climb_terminated_at_grown_root",
                             extra={"leaf": leaf_name, "grown_root": tip_name,
                                    "at": "single_rung_fallback"})
                    continue
                # Non-coherent tip → MINT-AND-QUARANTINE the proposed parent (never auto-place). The
                # async whatis classifier types/places it next cycle; once it becomes a real backbone
                # node, this chain advances onto it on a later pass (identity). CACHE 'unplaceable'/
                # no_info_root — re-open when growth makes it a node (fingerprint) or after backoff.
                _quarantine_climb_tip(db_conn, None, parent)
                stats["quarantined"] += 1
                _climb_state_record(db_conn, leaf_id, "unplaceable", "no_info_root", _fp)
        except Exception as e:
            stats["errors"] += 1
            log.warning(f"re_embedder.climb_leaf_failed leaf={str(leaf_id)[:12]} "
                        f"error={type(e).__name__}: {str(e)[:120]}")
            try:
                db_conn.rollback()
            except Exception:
                pass

    return stats


def _parent_resolves_to_backbone(db_conn, parent_name: str, roots: set) -> bool:
    """IDENTITY gate: True iff `parent_name` IS an existing backbone node.

    Accept when the name is a seeded root, OR it names an entity that already appears as a
    hierarchy NODE (the object of a hierarchy edge in facts ∪ staged). Pure identity — exact
    canonical-name membership against existing structure; NO cosine / difflib / similarity.
    Read-only, fail-safe (False on error → falls through to quarantine, never a bad place)."""
    p = (parent_name or "").strip().lower()
    if not p:
        return False
    if p in roots:
        return True
    rels = list(_HIERARCHY_RELS)
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM entity_aliases ea"
                "  WHERE lower(ea.alias) = %s"
                "    AND ea.entity_id IN ("
                "      SELECT object_id FROM facts"
                "        WHERE rel_type = ANY(%s) AND superseded_at IS NULL AND archived_at IS NULL"
                "      UNION"
                "      SELECT object_id FROM staged_facts"
                "        WHERE rel_type = ANY(%s) AND promoted_at IS NULL AND deleted_at IS NULL"
                "    ) LIMIT 1",
                (p, rels, rels),
            )
            return cur.fetchone() is not None
    except Exception:
        try:
            db_conn.rollback()
        except Exception:
            pass
        return False


def _place_climb_rung(db_conn, registry, user_id: str, tip_id: str, tip_name: str,
                      parent_name: str) -> bool:
    """Stage ONE validated climb rung `<tip> subclass_of <parent>`, BORN CLASS B, UUID-keyed.

    Both ends resolved to idempotent UUID v5 surrogates so the rung lands in the SAME
    keyspace the hierarchy walker traverses (never a string-keyed island). The subject UUID
    is pinned to the tip's existing entity UUID.

    PROVENANCE TIER (correctable mid-tier): a GROWN/ENGINE-LEARNED hierarchy rung is NOT an
    ephemeral Class-C throwaway and NOT user truth — it lands at the CORRECTABLE MIDDLE of the
    provenance ladder (`user_stated` 3 > `llm_learned` 2 > `llm_inferred` 1, main._PROVENANCE_AUTHORITY).
    fact_provenance='llm_learned' (rank 2) + fact_class='B' so the rung is DURABLE + PROMOTABLE
    (doesn't expire while used) and a USER statement (rank 3) can always supersede/override it.
    confidence 0.6 mirrors assign_class_and_confidence's llm_learned→B floor. `provenance` keeps the
    discernible source label 'engine_classify_climb' (the only difference between engine-climb and
    /expand-grown rungs — both share THIS placement path). Fail-safe: never raises, returns success."""
    try:
        obj_uuid = registry.resolve(user_id, parent_name)
    except Exception:
        try:
            db_conn.rollback()
        except Exception:
            pass
        return False
    if not obj_uuid or str(obj_uuid) == str(tip_id):
        return False
    # RELEVANCE GATE (before the cycle-guard, deliberately): the climb only re-derives a rung
    # when `_climb_state_should_skip` re-opened the concept — i.e. new information changed its
    # fingerprint. Re-deriving the SAME parent on new evidence is a confirmation, not a cycle,
    # and the cycle-guard below would otherwise swallow it (a staged rung makes its own parent
    # reachable). Count it and stop; nothing is inserted and no tier changes.
    if _reconfirm_existing_rung(db_conn, tip_id, obj_uuid, "subclass_of"):
        log.info("re_embedder.climb_rung_reconfirmed",
                 extra={"tip": tip_name, "parent": parent_name})
        return True
    # CYCLE-GUARD: never place `tip subclass_of parent` if parent is already a transitive
    # ancestor/descendant of tip (kills the machine<->mechanical_device reciprocal cycle).
    if _is_ancestor_or_descendant(db_conn, tip_id, obj_uuid):
        log.info(f"re_embedder.climb_cycle_rejected child={tip_name} parent={parent_name} "
                 f"reason=already_ancestor_or_descendant")
        return False
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO staged_facts"
                "  (subject_id, object_id, rel_type, fact_class, provenance,"
                "   fact_provenance, confidence, confirmed_count, is_hierarchy_rel)"
                "  VALUES (%s, %s, 'subclass_of', 'B', 'engine_classify_climb',"
                "          'llm_learned', 0.6, 1, true)"
                "  ON CONFLICT (subject_id, object_id, rel_type) DO UPDATE SET"
                "    confirmed_count = staged_facts.confirmed_count + 1,"
                "    last_seen_at = now()",
                (tip_id, obj_uuid),
            )
        db_conn.commit()
        log.info("re_embedder.climb_rung_placed",
                 extra={"tip": tip_name, "parent": parent_name})
        return True
    except Exception as e:
        try:
            db_conn.rollback()
        except Exception:
            pass
        log.debug(f"re_embedder.climb_place_failed tip={tip_name} parent={parent_name}: {e}")
        return False


def _leaf_direct_root_edge(db_conn, leaf_id: str, leaf_name: str, roots: set) -> Optional[str]:
    """Detect a TOO-DIRECT edge: leaf --(1 hop)--> SEEDED ROOT, leaf itself not a root.

    Returns the seeded-root NAME the leaf is directly parented to (the splice will insert an
    intermediate between leaf and this root), or None when there is no such too-direct edge.

    A too-direct edge is a `subclass_of`/`instance_of`/… hierarchy edge whose subject is the
    leaf and whose object is a SEEDED backbone root (animal/location/…) — i.e. the eager
    leaf-anchor jumped straight to the root, skipping the real intermediate rungs. The leaf
    must NOT itself be a seeded root (a root has no parent to splice). Read-only, fail-safe
    (None on error → the leaf just falls through to the ordinary tip-climb, never a bad place).
    Identity-only: exact canonical-name root membership, NO cosine / similarity."""
    ln = (leaf_name or "").strip().lower()
    if not ln or ln in roots or not roots:
        return None
    rels = list(_HIERARCHY_RELS)
    try:
        with db_conn.cursor() as cur:
            # Direct parents of the leaf (the OBJECTS of the leaf's own hierarchy edges), with
            # their canonical names, from both tables — same live filters as the walk/candidate
            # queries so a spliced-out (superseded) edge is never re-detected.
            cur.execute(
                "SELECT ea.alias FROM entity_aliases ea"
                "  WHERE ea.entity_id IN ("
                "    SELECT object_id FROM facts"
                "      WHERE subject_id = %s AND rel_type = ANY(%s)"
                "        AND superseded_at IS NULL AND archived_at IS NULL"
                "    UNION"
                "    SELECT object_id FROM staged_facts"
                "      WHERE subject_id = %s AND rel_type = ANY(%s)"
                "        AND promoted_at IS NULL AND deleted_at IS NULL"
                "  )"
                "  ORDER BY ea.is_preferred DESC, ea.alias ASC",
                (leaf_id, rels, leaf_id, rels),
            )
            for (alias,) in cur.fetchall():
                a = (alias or "").strip().lower()
                if a and a in roots:
                    return a
    except Exception:
        try:
            db_conn.rollback()
        except Exception:
            pass
        return None
    return None


def _splice_intermediate_rung(db_conn, registry, user_id: str, leaf_id: str, leaf_name: str,
                              inter_name: str, root_name: str, roots: set) -> bool:
    """OPTION A SPLICE: turn `leaf --> ROOT` into `leaf --> inter --> ROOT`, supersede the old.

    Inserts ONE intermediate rung and re-parents, deterministically and by IDENTITY:
      1. IDENTITY GATE the proposed intermediate (`inter_name`). Accept ONLY when it is a clean
         category token (reuse the rung-4 _validate_bridge_placement: not scalar, one token, not
         a universal root, differs from child) AND it either already resolves to a backbone node
         OR is mintable as a NEW canonical node we will WIRE to the former root (so it is never an
         island). A proposal that fails the validator → MINT-AND-QUARANTINE for the async whatis
         classifier, NEVER auto-place. NO cosine / difflib / semantic.
      2. Resolve `inter` to its idempotent UUID v5 surrogate (convergence by identity: dog and
         wolf proposing `canine` land on the SAME node). Refuse self-loops.
      3. INSERT `leaf subclass_of inter` (born Class B, llm_learned mid-tier via _place_climb_rung
         semantics) and `inter subclass_of root` (wire the new node UP so the chain stays walkable
         leaf→inter→…→root) in ONE transaction.
      4. SUPERSEDE the old too-direct `leaf --(hierarchy)--> root` edge — soft (superseded_at +
         archived_at), NEVER hard-delete — so the chain has no dangling/duplicate parent and the
         walk now climbs through `inter`.

    Returns True iff the splice was applied. Fail-safe: never raises; rolls back on any error so a
    partial splice never leaves a dangling re-parent."""
    li = (leaf_name or "").strip().lower()
    inter = (inter_name or "").strip().lower()
    root = (root_name or "").strip().lower()
    if not li or not inter or not root:
        return False
    # IDENTITY/category gate (deterministic; reuse rung-4 validator — no cosine).
    _ok, _why = _validate_bridge_placement(li, li, inter, child_a_type="", child_b_type="")
    if not _ok:
        log.debug(f"re_embedder.splice_intermediate_rejected leaf={li} inter={inter} reason={_why}")
        _quarantine_climb_tip(db_conn, None, inter)
        return False
    rels = list(_HIERARCHY_RELS)
    try:
        inter_uuid = registry.resolve(user_id, inter)
    except Exception:
        try:
            db_conn.rollback()
        except Exception:
            pass
        return False
    if not inter_uuid or str(inter_uuid) == str(leaf_id):
        return False
    try:
        root_uuid = registry.resolve(user_id, root)
    except Exception:
        try:
            db_conn.rollback()
        except Exception:
            pass
        return False
    if not root_uuid or str(root_uuid) == str(inter_uuid):
        # inter resolved to the root itself → no genuine intermediate; leave the direct edge.
        return False
    # RELEVANCE GATE (before the cycle-guard): a splice re-proposed on new evidence whose BOTH
    # rungs already exist is the same structure recurring — count both and stop, rather than
    # letting the cycle-guard read the rungs' own existence as a loop and drop the signal. Only
    # a COMPLETE re-derivation counts; a half-present splice falls through to place the rest.
    _l_i = _rung_exists(db_conn, leaf_id, inter_uuid, "subclass_of")
    _i_r = _rung_exists(db_conn, inter_uuid, root_uuid, "subclass_of")
    if _l_i and _i_r:
        _reconfirm_existing_rung(db_conn, leaf_id, inter_uuid, "subclass_of")
        _reconfirm_existing_rung(db_conn, inter_uuid, root_uuid, "subclass_of")
        log.info("re_embedder.splice_reconfirmed",
                 extra={"leaf": li, "inter": inter, "root": root})
        return True
    # CYCLE-GUARD: reject either spliced rung if it would close a loop (inter already an
    # ancestor/descendant of leaf, or root already one of inter). A loop in the splice is the
    # same reciprocal-parent corruption — skip the splice, leave the direct edge intact.
    if _is_ancestor_or_descendant(db_conn, leaf_id, inter_uuid) or \
       _is_ancestor_or_descendant(db_conn, inter_uuid, root_uuid):
        log.info(f"re_embedder.climb_cycle_rejected child={li} parent={inter} root={root} "
                 f"reason=splice_would_cycle")
        return False
    try:
        with db_conn.cursor() as cur:
            # 3a. leaf subclass_of inter (Class B, llm_learned mid-tier — correctable, durable).
            cur.execute(
                "INSERT INTO staged_facts"
                "  (subject_id, object_id, rel_type, fact_class, provenance,"
                "   fact_provenance, confidence, confirmed_count, is_hierarchy_rel)"
                "  VALUES (%s, %s, 'subclass_of', 'B', 'engine_classify_climb',"
                "          'llm_learned', 0.6, 1, true)"
                "  ON CONFLICT (subject_id, object_id, rel_type) DO UPDATE SET"
                "    confirmed_count = staged_facts.confirmed_count + 1, last_seen_at = now()",
                (leaf_id, inter_uuid),
            )
            # 3b. inter subclass_of root (wire the new node UP — keeps the chain walkable).
            cur.execute(
                "INSERT INTO staged_facts"
                "  (subject_id, object_id, rel_type, fact_class, provenance,"
                "   fact_provenance, confidence, confirmed_count, is_hierarchy_rel)"
                "  VALUES (%s, %s, 'subclass_of', 'B', 'engine_classify_climb',"
                "          'llm_learned', 0.6, 1, true)"
                "  ON CONFLICT (subject_id, object_id, rel_type) DO UPDATE SET"
                "    confirmed_count = staged_facts.confirmed_count + 1, last_seen_at = now()",
                (inter_uuid, root_uuid),
            )
            # 4. SUPERSEDE the old too-direct leaf-->root hierarchy edge (soft, both tables).
            #    NEVER hard-delete; the row stays recoverable, just no longer walked/served.
            cur.execute(
                "UPDATE facts SET superseded_at = now(), archived_at = now(), qdrant_synced = false"
                "  WHERE subject_id = %s AND object_id = %s AND rel_type = ANY(%s)"
                "    AND superseded_at IS NULL AND archived_at IS NULL",
                (leaf_id, root_uuid, rels),
            )
            # staged_facts: the LIVE recall/walk reads filter on `deleted_at IS NULL` (NOT
            # archived_at), so set the recoverable tombstone trio superseded_at + archived_at +
            # deleted_at together (migration 097's "archived_at + deleted_at together = fully
            # recoverable" — non-destructive, NOT a hard delete) so the spliced-out edge is no
            # longer SERVED yet stays recoverable. superseded_at records the semantic intent.
            cur.execute(
                "UPDATE staged_facts SET deleted_at = now(), qdrant_synced = false"
                "  WHERE subject_id = %s AND object_id = %s AND rel_type = ANY(%s)"
                "    AND promoted_at IS NULL AND deleted_at IS NULL",
                (leaf_id, root_uuid, rels),
            )
        db_conn.commit()
        log.info("re_embedder.climb_rung_spliced",
                 extra={"leaf": li, "intermediate": inter, "former_root_parent": root})
        return True
    except Exception as e:
        try:
            db_conn.rollback()
        except Exception:
            pass
        log.debug(f"re_embedder.splice_failed leaf={li} inter={inter} root={root}: {e}")
        return False


# Bound on co-instance specificity pairs grounded per tenant per cycle (worst case one classifier
# call each — keep the async loop cheap; the pass self-terminates once a pair is ordered).
_COINSTANCE_BATCH_LIMIT = int(os.environ.get("ENGINE_COINSTANCE_BATCH", "5") or "5")


def _place_coinstance_rung(db_conn, child_id: str, child_name: str,
                           parent_id: str, parent_name: str, roots: set) -> bool:
    """Stage `child subclass_of parent` (an EXISTING type node) and RETIRE the child's too-direct
    `child subclass_of <seeded root>` sibling edge, in ONE atomic transaction. Cycle-guarded.

    This is the placement half of co-instance specificity grounding: `parent` already exists in
    the tenant L4 (it is a co-classifying type of the same instance) and already carries its own
    ladder toward the root, so we only insert the ONE missing rung `child -> parent` and soft-
    supersede the child's direct-to-root edge (`poodle subclass_of animal`) so the walk climbs
    child -> parent -> ... -> root instead of leaving `child` a sibling of `parent` under the root.
    The new `child -> parent` edge is NEVER superseded (object_id <> parent_id). Root membership is
    by NAME identity against the seeded-root set (no cosine). Fail-safe: rolls back on any error."""
    if not child_id or not parent_id or str(child_id) == str(parent_id):
        return False
    # RELEVANCE GATE (before the cycle-guard): co-instance grounding re-proposing a rung that
    # already exists is the same specificity ordering observed again — count it. The retirement
    # of the too-direct sibling edge below has already happened on the first placement, so
    # there is nothing further to do.
    if _reconfirm_existing_rung(db_conn, child_id, parent_id, "subclass_of"):
        return True
    # CYCLE-GUARD: never place child -> parent if parent is already an ancestor/descendant of child
    # (would loop or is redundant). The same reciprocal-parent protection the splice uses.
    if _is_ancestor_or_descendant(db_conn, child_id, parent_id):
        return False
    rels = list(_HIERARCHY_RELS)
    _root_list = [r for r in roots if r]
    try:
        if not _stage_rung(db_conn, child_id, parent_id):
            try:
                db_conn.rollback()
            except Exception:
                pass
            return False
        # Retire ONLY the child's too-direct edge(s) whose OBJECT is a SEEDED ROOT (the sibling
        # artefact `child subclass_of animal`) — never the just-placed `child -> parent` edge and
        # never a non-root intermediate. Soft supersede (recoverable), both tables.
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE facts SET superseded_at = now(), archived_at = now(), qdrant_synced = false"
                "  WHERE subject_id = %s AND rel_type = ANY(%s)"
                "    AND superseded_at IS NULL AND archived_at IS NULL"
                "    AND object_id <> %s"
                "    AND object_id IN (SELECT entity_id FROM entity_aliases"
                "                        WHERE lower(alias) = ANY(%s))",
                (child_id, rels, parent_id, _root_list),
            )
            cur.execute(
                "UPDATE staged_facts SET deleted_at = now(), qdrant_synced = false"
                "  WHERE subject_id = %s AND rel_type = ANY(%s)"
                "    AND promoted_at IS NULL AND deleted_at IS NULL"
                "    AND object_id <> %s"
                "    AND object_id IN (SELECT entity_id FROM entity_aliases"
                "                        WHERE lower(alias) = ANY(%s))",
                (child_id, rels, parent_id, _root_list),
            )
        db_conn.commit()
        log.info("re_embedder.coinstance_grounded",
                 extra={"child": child_name, "parent": parent_name})
        return True
    except Exception as e:
        try:
            db_conn.rollback()
        except Exception:
            pass
        log.debug(f"re_embedder.coinstance_place_failed child={child_name} parent={parent_name}: {e}")
        return False


def _ground_coinstance_specificity(db_conn, qwen_api_url: str, user_id: str, roots: set) -> dict:
    """CO-INSTANCE SPECIFICITY — ground the more-SPECIFIC of two co-classifying types UNDER the
    more-general one, so the ±6 produces `poodle subclass_of dog` instead of leaving poodle and
    dog as SIBLINGS both attached straight to the broad seeded root (`animal`).

    THE GAP THIS CLOSES (the real breed miss, ingest-side): when a named instance is `instance_of`
    MULTIPLE type nodes (`rex instance_of poodle` + `rex instance_of dog`), the eager
    leaf-anchor (`_attach_to_seeded_backbone`) attaches EACH type DIRECTLY to the seeded backbone
    root — `poodle subclass_of animal` AND `dog subclass_of animal` — as SIBLINGS. The per-leaf
    splice deepens each ladder toward the root but never, on its own, re-parents one sibling UNDER
    the other, so the specific breed never lands under its parent type and the (shipped, correct)
    most-specific recall collapse has no `poodle subclass_of dog` ladder to act on → recall shows
    BOTH "instance of dog" and "instance of poodle". The CO-INSTANCE signal — the SAME instance
    IS-A both types — is the ground truth that one type SUBSUMES the other; this pass grounds it.

    SUBJECT-AGNOSTIC / ENGINE-DRIVEN (no literal, no breed/type word list, no seed additive):
      • Candidates are purely STRUCTURAL — an instance with >= 2 live `instance_of` type-objects
        that are currently UNORDERED (no `subclass_of`/`is_a` path between them).
      • DIRECTION is decided by the SAME classifier the splice already uses
        (`_query_llm_full_chain`): A is-a B iff B appears in A's is-a ladder. The LLM CLASSIFIES;
        deterministic rules PLACE/validate. NO cosine / similarity — the hypernym is exactly what
        the classifier returns, and if it cannot relate the pair we SKIP (never guess a direction).

    Off the ingest hot path (async re_embedder), bounded (`_COINSTANCE_BATCH_LIMIT`), cache-gated
    (`climb_state` on the INSTANCE entity — a fingerprint bump when a new type is added or the
    ontology grows a parent re-opens it) and FAIL-SAFE (never raises; the loop must not crash)."""
    out = {"grounded": 0, "undetermined": 0, "skipped_cached": 0}
    if not user_id or not roots:
        return out
    # Instances with >= 2 distinct live instance_of type-objects (the co-classification signal).
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT subject_id, array_agg(DISTINCT object_id) FROM ("
                "  SELECT subject_id, object_id FROM facts"
                "    WHERE rel_type = 'instance_of'"
                "      AND superseded_at IS NULL AND archived_at IS NULL"
                "  UNION"
                "  SELECT subject_id, object_id FROM staged_facts"
                "    WHERE rel_type = 'instance_of'"
                "      AND promoted_at IS NULL AND deleted_at IS NULL"
                ") q GROUP BY subject_id HAVING count(DISTINCT object_id) >= 2",
            )
            rows = cur.fetchall()
        # READ BARRIER: _query_llm_full_chain is called per type name below.
        release_read_transaction(
            db_conn, context="re_embedder.ground_coinstance_specificity.fetch")
    except Exception:
        try:
            db_conn.rollback()
        except Exception:
            pass
        return out
    if not rows:
        return out

    _chain_cache: dict = {}

    def _chain_for(name: str) -> set:
        n = (name or "").strip().lower()
        if not n:
            return set()
        if n in _chain_cache:
            return _chain_cache[n]
        try:
            ch = _query_llm_full_chain(n, qwen_api_url) or []
        except LLMUnavailable:
            # Never asked → do NOT cache an empty chain for this name. Caching "" here would
            # make an unreachable brain look like "this concept has no is-a ladder" for the
            # rest of the run, and the specificity comparison would be decided on that.
            raise
        except Exception:
            ch = []
        s = {(str(t) if t is not None else "").strip().lower() for t in ch}
        _chain_cache[n] = s
        return s

    grounded = 0
    for inst_id, type_ids in rows:
        # READ BARRIER (per iteration): this loop body reaches the brain through a NESTED
        # helper (`_chain_for` -> _query_llm_full_chain), which a call-graph scan that stops
        # at module scope does not see. The per-row `_concept_fingerprint` /
        # `_climb_state_should_skip` reads would otherwise ride across that call.
        release_read_transaction(db_conn, context="re_embedder._ground_coinstance_specificity.iteration")
        if grounded >= _COINSTANCE_BATCH_LIMIT:
            break
        tids = [t for t in (type_ids or []) if t]
        if len(tids) < 2:
            continue
        # CACHE (DB = cache): honour a prior verdict on THIS instance unless its input changed
        # (a new type added / the ontology grew a parent → fingerprint bumps → re-open). Keying on
        # the INSTANCE uuid is a distinct keyspace from the type-leaf climb (which keys on TYPE
        # uuids) — no collision.
        _fp = _concept_fingerprint(db_conn, inst_id)
        if _climb_state_should_skip(db_conn, inst_id, _fp):
            out["skipped_cached"] += 1
            continue
        # Resolve each type node to its canonical TYPE-word (poodle/dog). Exclude a named instance
        # or a pure name (a name never orders an L4 ladder — THE HARD LINE) and the seeded roots
        # themselves (a root is not a child to re-parent).
        typed = []
        for tid in tids:
            if _is_named_instance(db_conn, tid, user_id=user_id):
                continue
            # FIRST-CLASS value/place stamp (migration 192, flag VALUE_PLACE_FIRST_CLASS default OFF).
            # THE HARD LINE in the ASYNC tier: a user VALUE node (stamped favorite_colour/scalar
            # object) has no in-flight edge here to re-derive from, so the persisted stamp is the only
            # guard — never order/ladder it into an L4 type. OFF → False (byte-identical).
            if _node_role.is_protected_value(db_conn, tid):
                continue
            nm = _type_word_of_entity(db_conn, tid) or _name_of_entity(db_conn, tid)
            if nm and nm not in roots:
                # THE HARD-LINE LADDER GUARD (fail-closed; src/api/hardline_guard.py). The climb
                # lane minted census rows on value/name nodes (krellin → krelling) because the
                # stamp is partial and node_role fails OPEN. Guards the LEAF before any rung is
                # ordered off it; deeper rung subjects are canonical type words already filtered.
                _hl_refuse, _hl_reason = _ladder_hardline.refuses_subclass_rung(
                    db_conn, tid, nm, user_id=user_id)
                if _hl_refuse:
                    log.debug("re_embedder.hardline_ladder_refused",
                              extra={"concept": nm, "reason": _hl_reason,
                                     "lane": "classify_climb"})
                    continue
                typed.append((tid, nm))
        if len(typed) < 2:
            _climb_state_record(db_conn, inst_id, "unplaceable", "no_ordered_types", _fp)
            continue
        did = False           # grounded at least one rung THIS pass
        unresolved = False     # >=1 pair the classifier could not order (leave for a later pass)
        for i in range(len(typed)):
            # READ BARRIER (per iteration): this loop body reaches the brain through a NESTED
            # helper (`_chain_for` -> _query_llm_full_chain), which a call-graph scan that stops
            # at module scope does not see. The per-row `_concept_fingerprint` /
            # `_climb_state_should_skip` reads would otherwise ride across that call.
            release_read_transaction(db_conn, context="re_embedder._ground_coinstance_specificity.iteration")
            for j in range(i + 1, len(typed)):
                # READ BARRIER (per iteration): this loop body reaches the brain through a NESTED
                # helper (`_chain_for` -> _query_llm_full_chain), which a call-graph scan that stops
                # at module scope does not see. The per-row `_concept_fingerprint` /
                # `_climb_state_should_skip` reads would otherwise ride across that call.
                release_read_transaction(db_conn, context="re_embedder._ground_coinstance_specificity.iteration")
                a_id, a_nm = typed[i]
                b_id, b_nm = typed[j]
                # Already ordered by an existing ladder → a resolved (done) pair, no LLM call.
                if _is_ancestor_or_descendant(db_conn, a_id, b_id):
                    continue
                child = parent = None
                # READ BARRIER (immediately before the blocking call — the RE-ARM case). A barrier at
                # the top of the enclosing block is NOT enough: a per-row read helper opens a FRESH
                # transaction after it, and that read then rides across this hop. Measured live on the
                # deployed image — climb_classification_chains was killed twice this way (02:42:25 and
                # 02:45:06), its whole _ont_db subsystem chain failing 'connection already closed' four
                # seconds later.
                release_read_transaction(db_conn, context="re_embedder._ground_coinstance_specificity.pre_blocking_call")
                if b_nm in _chain_for(a_nm):        # A is-a B → A is the more-specific child.
                    child, parent = (a_id, a_nm), (b_id, b_nm)
                elif a_nm in _chain_for(b_nm):       # B is-a A → B is the more-specific child.
                    child, parent = (b_id, b_nm), (a_id, a_nm)
                if not child:
                    out["undetermined"] += 1
                    unresolved = True
                    continue
                if _place_coinstance_rung(db_conn, child[0], child[1],
                                          parent[0], parent[1], roots):
                    grounded += 1
                    out["grounded"] += 1
                    did = True
                else:
                    unresolved = True  # cycle-guarded / stage failure → retry a later pass
        # 'placed' = every pair is resolved (ordered by an existing ladder or grounded this pass);
        # only a genuinely un-orderable pair leaves the instance 'unplaceable' (cap/backoff bounds
        # the re-tries; a new type or a grown parent bumps the fingerprint and re-opens it).
        _climb_state_record(
            db_conn, inst_id,
            "unplaceable" if unresolved else "placed",
            "coinstance_grounded" if did else
            ("coinstance_undetermined" if unresolved else "coinstance_resolved"), _fp,
        )
    return out


def _quarantine_climb_tip(db_conn, tip_id: Optional[str], tip_name: str) -> None:
    """MINT-AND-QUARANTINE a chain tip (or an unresolved proposed parent) for later grounding.

    Writes the SAME `ingest_miss_pushback` ontology_evaluations row the async whatis classifier
    consumes (extraction_method='ingest_miss_pushback', re_embedder_decision IS NULL,
    sample_object=<name>) so it is TYPED/PLACED on a later cycle — never auto-placed here.
    Idempotent-ish (only inserts when no undecided row exists for the name). Fail-safe."""
    name = (tip_name or "").strip().lower()
    if not name:
        return
    # THE HARD LINE — TERM ADMISSIBILITY (third and last writer of `ingest_miss_pushback` rows;
    # the two ingest-side writers in main.py carry the same gate). The names arriving here are
    # LLM-PROPOSED parents/tips, so they are normally well-formed snake_case type names and pass
    # untouched — this is the defense-in-depth arm that stops a malformed proposal (a path, an
    # expression, a lifted phrase) from being minted as a concept and re-entering the very
    # classifier that proposed it. Fail-safe: any import/parse error → today's behavior (insert).
    try:
        from src.extraction.linguistics import type_term_shape
        _ok_term, _term_why = type_term_shape(name)
        if not _ok_term:
            log.info("climb.tip_quarantine_refused_not_a_term "
                     f"tip={name[:80]} reason={_term_why}")
            return
    except Exception:  # noqa: BLE001 — never break the sweep over the shape check
        pass
    # sample_subject_id is part of the UNIQUE key (candidate_rel_type, sample_subject_id,
    # sample_object); the tip's own entity UUID keys it (or 'climb' when an unresolved
    # proposed parent has no node yet). The async whatis consumer reads sample_object.
    subj_key = str(tip_id) if tip_id else "climb"
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ontology_evaluations"
                "  (candidate_rel_type, candidate_subject_type, candidate_object_type,"
                "   sample_subject_id, sample_object, extraction_method,"
                "   decision_reason, occurrence_count, last_seen_at)"
                "  VALUES ('subclass_of', 'unknown', 'unknown', %s, %s,"
                "          'ingest_miss_pushback',"
                "          'climb backstop/unresolved parent — queued for what-is classify',"
                "          1, now())"
                "  ON CONFLICT (candidate_rel_type, sample_subject_id, sample_object)"
                "  DO UPDATE SET occurrence_count = ontology_evaluations.occurrence_count + 1,"
                "    last_seen_at = now()",
                (subj_key, name),
            )
        db_conn.commit()
        log.debug("re_embedder.climb_quarantined", extra={"name": name})
    except Exception as e:
        try:
            db_conn.rollback()
        except Exception:
            pass
        log.debug(f"re_embedder.climb_quarantine_failed name={name}: {e}")


# ════════════════════════════════════════════════════════════════════════════════════════════════
# RUNG-6 self-assembling backbone (the internal design record §"Growth")
# ════════════════════════════════════════════════════════════════════════════════════════════════

def converge_hierarchy_by_identity(db_conn, schema_name: str = "") -> dict:
    """RUNG-6 CONVERGENCE (deterministic, FREE — no cosine, no LLM).

    Two separately-grown hierarchy branches that reach a node bearing the SAME CANONICAL NAME
    (e.g. both reach a `mammal` node) are the SAME node — connect them by IDENTITY. This is the
    primary collapse mechanism that, with curated `rel_type_aliases`, RETIRES the cosine-map.

    Mechanism (deterministic, per-tenant; search_path already bound by caller):
      1. Find all hierarchy NODES — distinct entities that appear as the OBJECT (parent) of a
         hierarchy edge (`subclass_of`/`instance_of`/`part_of`/`member_of`/`is_a`) in either
         `facts` or `staged_facts`.
      2. Group those node entities by their CANONICAL (preferred, else any) alias, lowercased.
      3. For any group with ≥2 distinct entity UUIDs sharing a canonical name → they are duplicate
         representations of one backbone node. Pick a canonical survivor deterministically
         (lowest UUID string — stable, subject-agnostic) and REPOINT the other branches' hierarchy
         edges (object_id) onto the survivor. The islands fuse where the shared ancestor already
         exists — exactly the design's "built incrementally by real demand."

    This is a STRUCTURAL merge of hierarchy edges only — it does NOT touch the entity rows, aliases,
    membership/composition facts, or scalars (entity-level dedup is owned by resolve_name_conflicts).
    Idempotent: once repointed, the duplicate node no longer appears as a fresh parent.

    Returns {"merged_nodes": int, "edges_repointed": int}.
    Fail-soft: any error returns the running stats; never crashes the sweep.
    """
    stats = {"merged_nodes": 0, "edges_repointed": 0}
    if not _RUNG6_CONVERGENCE:
        return stats

    _rels = list(_HIERARCHY_RELS)
    try:
        with db_conn.cursor() as cur:
            # 1. Candidate hierarchy node ids (objects of hierarchy edges), both tables.
            cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                """
                SELECT DISTINCT object_id FROM facts
                  WHERE rel_type = ANY(%s) AND superseded_at IS NULL
                UNION
                SELECT DISTINCT object_id FROM staged_facts
                  WHERE rel_type = ANY(%s) AND promoted_at IS NULL
                """,
                (_rels, _rels),
            )
            node_ids = [r[0] for r in cur.fetchall() if r[0]]
        if len(node_ids) < 2:
            return stats

        # 2. Canonical name per node — prefer the is_preferred alias, else any alias.
        with db_conn.cursor() as cur:
            cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                """
                SELECT entity_id, alias, is_preferred FROM entity_aliases
                WHERE entity_id = ANY(%s)
                """,
                (node_ids,),
            )
            # name_of[entity] = (preferred_alias or first_alias)
            name_of: dict = {}
            pref_seen: set = set()
            for ent, alias, is_pref in cur.fetchall():
                if not alias:
                    continue
                al = alias.strip().lower()
                if ent not in name_of or (is_pref and ent not in pref_seen):
                    name_of[ent] = al
                if is_pref:
                    pref_seen.add(ent)

        # 3. Group entities by canonical name; merge groups of ≥2.
        by_name: dict = {}
        for ent, nm in name_of.items():
            by_name.setdefault(nm, []).append(ent)

        for nm, ents in by_name.items():
            uniq = sorted(set(ents))  # deterministic order
            if len(uniq) < 2:
                continue
            survivor = uniq[0]
            losers = uniq[1:]
            repointed = 0
            with db_conn.cursor() as cur:
                for loser in losers:
                    # Repoint hierarchy edges that POINT AT the duplicate node onto the survivor,
                    # in both tables. Guard against creating a self-loop (subject == survivor).
                    cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                        """
                        UPDATE facts SET object_id = %s, qdrant_synced = false
                        WHERE object_id = %s AND rel_type = ANY(%s)
                          AND superseded_at IS NULL AND subject_id <> %s
                        """,
                        (survivor, loser, _rels, survivor),
                    )
                    repointed += cur.rowcount
                    cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                        """
                        UPDATE staged_facts SET object_id = %s, qdrant_synced = false
                        WHERE object_id = %s AND rel_type = ANY(%s)
                          AND promoted_at IS NULL AND subject_id <> %s
                        """,
                        (survivor, loser, _rels, survivor),
                    )
                    repointed += cur.rowcount
            if repointed:
                stats["merged_nodes"] += 1
                stats["edges_repointed"] += repointed
                log.info(
                    "re_embedder.rung6_converged "
                    f"schema={schema_name} canonical_name={nm} survivor={str(survivor)[:8]} "
                    f"merged={len(losers)} edges_repointed={repointed}"
                )
        if stats["edges_repointed"]:
            db_conn.commit()
    except Exception as e:
        db_conn.rollback()
        log.warning(f"re_embedder.rung6_converge_failed schema={schema_name} error={type(e).__name__}: {str(e)[:120]}")
    return stats


def _pick_survivor_by_evidence(a_id, a_deg: int, b_id, b_deg: int) -> tuple:
    """PURE deterministic survivor pick for a hierarchy-node merge (no DB, no cosine).

    Higher hierarchy DEGREE (more live edges touching it = the more-established node) survives; on a
    tie the lexicographically-lower UUID string wins (stable, subject-agnostic). Returns
    (survivor_id, loser_id) as strings. Unit-testable."""
    a, b = str(a_id), str(b_id)
    if int(a_deg) != int(b_deg):
        return (a, b) if int(a_deg) > int(b_deg) else (b, a)
    return (a, b) if a <= b else (b, a)


def _hierarchy_degree(db_conn, node_id, rels) -> int:
    """Count of LIVE hierarchy edges touching ``node_id`` (as subject OR object), both tables.
    Read-only, fail-safe (0 on error)."""
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT"
                "  (SELECT count(*) FROM facts"
                "     WHERE (subject_id = %s OR object_id = %s) AND rel_type = ANY(%s)"
                "       AND superseded_at IS NULL AND archived_at IS NULL)"
                " +(SELECT count(*) FROM staged_facts"
                "     WHERE (subject_id = %s OR object_id = %s) AND rel_type = ANY(%s)"
                "       AND promoted_at IS NULL AND deleted_at IS NULL)",
                (str(node_id), str(node_id), rels, str(node_id), str(node_id), rels),
            )
            row = cur.fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        try:
            db_conn.rollback()
        except Exception:
            pass
        return 0


def collapse_hierarchy_2cycles(db_conn, schema_name: str = "") -> dict:
    """Lever 2 — COLLAPSE hierarchy 2-CYCLES so the climb stops islanding on ``cycle_rejected``.

    GROUNDING: antisymmetry of a partial order (RDFS subClassOf pre-order / SKOS broader semantics) —
    a mutual subclass relation entails identity, not a similarity guess. See
    the internal design record

    ``subclass_of`` (and the hierarchy rels) is a partial ORDER: A⊆B ∧ B⊆A ⟹ A=B. So a LIVE
    reciprocal hierarchy-edge pair between two DISTINCT entities (``vulnerability`` ⇄
    ``security_vulnerability``) is a proof-BY-IDENTITY that they are the SAME backbone node — NOT a
    similarity guess. Un-collapsed, that loop makes ``_is_ancestor_or_descendant`` reject every new
    rung (``cycle_rejected``) and QUARANTINE otherwise-good chains. This is the convergence case
    ``converge_hierarchy_by_identity`` MISSES because the two nodes carry DIFFERENT canonical names
    (synonym / compound variants), so name-identity never groups them; the reciprocal-edge identity
    does.

    MERGE (deterministic): survivor = the higher-EVIDENCE node (``_pick_survivor_by_evidence`` —
    more live hierarchy edges; tie → lowest UUID). REPOINT every live hierarchy edge of the loser
    onto the survivor (object_id AND subject_id), skipping rows that would COLLIDE with an existing
    survivor edge (``NOT EXISTS`` guard — no unique violation), then TOMBSTONE every remaining live
    loser edge + the reciprocal pair + any self-loop (facts: superseded+archived; staged: deleted_at
    — non-destructive, recoverable). Hierarchy EDGES only — never entity rows / aliases / scalars
    (mirrors ``converge_hierarchy_by_identity``). Idempotent; once merged the loop is gone and the
    climb advances onto the single survivor node.

    Fail-safe: per-pair try/except + rollback so one bad pair never aborts the rest; never crashes
    the sweep. Deterministic — identity / evidence-order only, NO cosine / difflib / similarity.
    Guarded by ``_RUNG6_CONVERGENCE`` (same convergence family). Returns stats dict."""
    stats = {"cycles_collapsed": 0, "edges_repointed": 0}
    if not _RUNG6_CONVERGENCE:
        return stats
    rels = list(_HIERARCHY_RELS)
    try:
        with db_conn.cursor() as cur:
            cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                """
                WITH live AS (
                    SELECT subject_id AS s, object_id AS o FROM facts
                      WHERE rel_type = ANY(%s) AND superseded_at IS NULL AND archived_at IS NULL
                    UNION
                    SELECT subject_id AS s, object_id AS o FROM staged_facts
                      WHERE rel_type = ANY(%s) AND promoted_at IS NULL AND deleted_at IS NULL
                )
                SELECT DISTINCT l1.s, l1.o FROM live l1
                  JOIN live l2 ON l2.s = l1.o AND l2.o = l1.s
                 WHERE l1.s <> l1.o
                """,
                (rels, rels),
            )
            raw_pairs = cur.fetchall()
    except Exception:
        try:
            db_conn.rollback()
        except Exception:
            pass
        return stats
    if not raw_pairs:
        return stats

    seen: set = set()
    for s, o in raw_pairs:
        key = tuple(sorted((str(s), str(o))))
        if key in seen:
            continue
        seen.add(key)
        a_id, b_id = key
        try:
            survivor, loser = _pick_survivor_by_evidence(
                a_id, _hierarchy_degree(db_conn, a_id, rels),
                b_id, _hierarchy_degree(db_conn, b_id, rels),
            )
            repointed = 0
            with db_conn.cursor() as cur:
                # Repoint object_id (X -> loser  ⇒  X -> survivor), skipping self-loops and rows
                # that would collide with an existing live (X -> survivor) edge of the same rel.
                cur.execute(
                    "UPDATE facts f SET object_id = %s, qdrant_synced = false"
                    "  WHERE f.object_id = %s AND f.rel_type = ANY(%s)"
                    "    AND f.superseded_at IS NULL AND f.archived_at IS NULL"
                    "    AND f.subject_id <> %s"
                    "    AND NOT EXISTS (SELECT 1 FROM facts g WHERE g.subject_id = f.subject_id"
                    "        AND g.object_id = %s AND g.rel_type = f.rel_type"
                    "        AND g.superseded_at IS NULL AND g.archived_at IS NULL)",
                    (survivor, loser, rels, survivor, survivor),
                )
                repointed += cur.rowcount
                cur.execute(
                    "UPDATE staged_facts f SET object_id = %s, qdrant_synced = false"
                    "  WHERE f.object_id = %s AND f.rel_type = ANY(%s)"
                    "    AND f.promoted_at IS NULL AND f.deleted_at IS NULL"
                    "    AND f.subject_id <> %s"
                    "    AND NOT EXISTS (SELECT 1 FROM staged_facts g WHERE g.subject_id = f.subject_id"
                    "        AND g.object_id = %s AND g.rel_type = f.rel_type"
                    "        AND g.promoted_at IS NULL AND g.deleted_at IS NULL)",
                    (survivor, loser, rels, survivor, survivor),
                )
                repointed += cur.rowcount
                # Repoint subject_id (loser -> Y  ⇒  survivor -> Y), same collision/self-loop guard.
                cur.execute(
                    "UPDATE facts f SET subject_id = %s, qdrant_synced = false"
                    "  WHERE f.subject_id = %s AND f.rel_type = ANY(%s)"
                    "    AND f.superseded_at IS NULL AND f.archived_at IS NULL"
                    "    AND f.object_id <> %s"
                    "    AND NOT EXISTS (SELECT 1 FROM facts g WHERE g.object_id = f.object_id"
                    "        AND g.subject_id = %s AND g.rel_type = f.rel_type"
                    "        AND g.superseded_at IS NULL AND g.archived_at IS NULL)",
                    (survivor, loser, rels, survivor, survivor),
                )
                repointed += cur.rowcount
                cur.execute(
                    "UPDATE staged_facts f SET subject_id = %s, qdrant_synced = false"
                    "  WHERE f.subject_id = %s AND f.rel_type = ANY(%s)"
                    "    AND f.promoted_at IS NULL AND f.deleted_at IS NULL"
                    "    AND f.object_id <> %s"
                    "    AND NOT EXISTS (SELECT 1 FROM staged_facts g WHERE g.object_id = f.object_id"
                    "        AND g.subject_id = %s AND g.rel_type = f.rel_type"
                    "        AND g.promoted_at IS NULL AND g.deleted_at IS NULL)",
                    (survivor, loser, rels, survivor, survivor),
                )
                repointed += cur.rowcount
                # TOMBSTONE every remaining live edge that still touches the loser (the collided
                # duplicates + the reciprocal cycle pair) and any self-loop the repoint produced.
                cur.execute(
                    "UPDATE facts SET superseded_at = now(), archived_at = now(), qdrant_synced = false"
                    "  WHERE rel_type = ANY(%s) AND superseded_at IS NULL AND archived_at IS NULL"
                    "    AND (subject_id = %s OR object_id = %s OR subject_id = object_id)",
                    (rels, loser, loser),
                )
                cur.execute(
                    "UPDATE staged_facts SET deleted_at = now(), qdrant_synced = false"
                    "  WHERE rel_type = ANY(%s) AND promoted_at IS NULL AND deleted_at IS NULL"
                    "    AND (subject_id = %s OR object_id = %s OR subject_id = object_id)",
                    (rels, loser, loser),
                )
            db_conn.commit()
            stats["cycles_collapsed"] += 1
            stats["edges_repointed"] += repointed
            log.info("re_embedder.hierarchy_2cycle_collapsed "
                     f"schema={schema_name} survivor={str(survivor)[:8]} "
                     f"loser={str(loser)[:8]} edges_repointed={repointed}")
        except Exception as e:
            try:
                db_conn.rollback()
            except Exception:
                pass
            log.warning(f"re_embedder.hierarchy_2cycle_collapse_failed schema={schema_name} "
                        f"error={type(e).__name__}: {str(e)[:120]}")
            continue
    return stats


# No-information upper-ontology TOKENS + SUFFIXES (PATTERN-based, subject-agnostic). These are
# generic placeholder categories that carry no real classification signal — a chain must terminate
# at the REAL category one rung BELOW them (e.g. `emotion`), never climb into them. Closed set,
# NOT a branch on any subject domain: the suffix rule rejects `psychological_phenomenon`,
# `mental_concept`, `cognitive_entity`, `physical_entity`, `mental_event` all alike. `_state`/
# `_system`/`_device` etc. are NOT here — those are real domain categories (affective_state,
# political_system, network_device) and must keep passing. Identity-not-fuzzy: exact suffix/token
# match only, no embedding, no domain literal branched-on.
_NO_INFO_ROOT_SUFFIXES = ("_entity", "_phenomenon", "_concept", "_event", "_abstraction")
_NO_INFO_ROOT_PREFIXES = ("abstract_", "cognitive_")
_NO_INFO_ROOT_TOKENS = frozenset({
    "entity", "phenomenon", "concept", "event", "abstraction", "abstract",
    "cognition", "cognitive_entity", "abstract_entity", "mental_concept",
})


def _is_no_information_upper_root(token: str) -> bool:
    """True iff `token` is a no-information upper-ontology placeholder (pattern-based).

    Subject-agnostic: matches by SUFFIX (`*_entity`/`*_phenomenon`/`*_concept`/`*_event`/
    `*_abstraction`), PREFIX (`abstract_*`/`cognitive_*`), or the bare no-info token set. A token
    matching this is NOT a valid emergent root — the chain terminates at the real category below
    it. Deterministic, no fuzzy. Real domain categories (emotion, affective_state, network_device,
    political_system) do NOT match — they carry classification information."""
    t = (token or "").strip().lower()
    if not t:
        return False
    if t in _NO_INFO_ROOT_TOKENS:
        return True
    if t.endswith(_NO_INFO_ROOT_SUFFIXES):
        return True
    if t.startswith(_NO_INFO_ROOT_PREFIXES):
        return True
    return False


def _token_resolves_to_named_instance(db_conn, token: str) -> bool:
    """DB probe: does ``token`` (a candidate hierarchy/LCA node name) resolve to a NAMED INSTANCE?

    A named instance = an entity that carries an ``also_known_as``/``pref_name`` edge (``_NAMING_RELS``)
    — MEMORY content (a specific named thing), NOT a TYPE (a place). Used by the bridge validator's
    symmetric firewall: a proposed parent/LCA that resolves to such an entity is REJECTED (a memory
    can never be a place — THE HARD LINE applied to the parent).

    Resolution is by alias → entity_id → naming-edge membership (UUID joins, no string guessing),
    mirroring the established named-instance detection. Read-only, fail-safe: on any error returns
    True (treat-as-named-instance ⇒ REJECT the bridge) so we never grow a bridge OFF a memory when
    we cannot prove it is a clean type. Subject-agnostic — naming rels are the fixed SKOS pair."""
    t = (token or "").strip().lower()
    if not t:
        return False
    naming = list(_NAMING_RELS)
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM entity_aliases ea"
                "  WHERE lower(ea.alias) = %s"
                "    AND ea.entity_id IN ("
                "      SELECT object_id FROM facts"
                "        WHERE rel_type = ANY(%s)"
                "          AND superseded_at IS NULL AND archived_at IS NULL"
                "      UNION"
                "      SELECT object_id FROM staged_facts"
                "        WHERE rel_type = ANY(%s)"
                "          AND promoted_at IS NULL AND deleted_at IS NULL"
                "    ) LIMIT 1",
                (t, naming, naming),
            )
            return cur.fetchone() is not None
    except Exception:
        try:
            db_conn.rollback()
        except Exception:
            pass
        # Fail-SAFE toward the firewall: cannot prove it's a clean type ⇒ reject the bridge.
        return True


def _validate_bridge_placement(child_a: str, child_b: str, proposed_lca: str,
                               child_a_type: str = "", child_b_type: str = "",
                               lca_is_named_instance: bool = False) -> tuple:
    """Validate a proposed LCA bridge against the RUNG-4 hierarchy rules (DESIGN §Hierarchy).

    A bridge is "close enough" iff it PASSES the deterministic rules, NOT by an embedding score:
      - the LCA token must be a clean snake_case category name (entity-or-subgroup, not a scalar
        literal and not a loose relational phrase);
      - it must NOT be a bare upper-ontology root (thing/entity/object/concept) — connect-to-known,
        don't-extend-to-root;
      - it must differ from both children (no self-bridge);
      - the proposed PARENT/LCA must NOT be a named INSTANCE — a bridge node is a TYPE (the PLACE),
        never a specific named entity carrying ``pref_name``/``also_known_as`` (MEMORY). THE HARD
        LINE, applied SYMMETRICALLY to the parent: just as a child may not be filed at a memory,
        the engine may not propose a memory AS the parent. The caller (which has DB access) resolves
        whether the LCA token resolves to a named-instance entity and passes ``lca_is_named_instance``;
        this keeps the validator pure (no DB) while enforcing the firewall.
      - type-consistency: both children must be groundable under a shared general type (when the
        observed types are known they must agree, modulo unknown).

    Returns (ok: bool, reason: str). Pure/deterministic — no DB, no LLM, no cosine.
    """
    lca = (proposed_lca or "").strip().lower()
    ca = (child_a or "").strip().lower()
    cb = (child_b or "").strip().lower()
    if not lca:
        return False, "empty_lca"
    # named-instance guard (HARD LINE, symmetric) — the parent must be a TYPE, never a specific
    # named MEMORY entity. A memory can never be a place. Caller-resolved (DB) flag keeps this pure.
    if lca_is_named_instance:
        return False, "lca_is_named_instance"
    # scalar / literal guard — a value can never be a hierarchy node.
    if re.search(r"\d", lca) or any(ch in lca for ch in "@/:."):
        return False, "lca_looks_scalar"
    # loose phrase guard — a bridge node is ONE category token, not a sentence.
    if len(lca.split()) > 3:
        return False, "lca_not_a_category_token"
    # upper-ontology root guard — connect-to-known, never extend-to-universal-root. These
    # bare catch-alls are NOT a valid L4 placement (physical OR non-physical): a concept that
    # only ladders up to "abstract_concept"/"thing" genuinely hasn't been classified yet →
    # quarantine + retry, NOT terminate. (A real domain top like emotion/mental_state passes.)
    if lca in ("thing", "entity", "object", "concept", "item", "stuff",
               "abstract_concept", "abstraction", "abstract"):
        return False, "lca_is_upper_root"
    # no-information upper-ontology guard (PATTERN-based, subject-agnostic — NO domain literals).
    # The non-physical climb towered `anxious → … → psychological_phenomenon → mental_concept →
    # cognitive_entity → abstract_entity` instead of stopping at the real category (emotion). These
    # generic "_entity"/"_phenomenon"/"_concept"-suffixed compounds and bare `abstract_*` tokens are
    # upper-ontology placeholders that carry no classification information — a chain must terminate
    # at the REAL category BELOW them, never climb into them. This is a closed no-information TOKEN
    # SET / suffix pattern, not a branch on any subject domain (it rejects `mental_concept`,
    # `cognitive_entity`, `psychological_phenomenon`, `physical_entity` alike — fully symmetric).
    if _is_no_information_upper_root(lca):
        return False, "lca_is_no_information_root"
    if lca == ca or lca == cb:
        return False, "lca_equals_child"
    # type-consistency: when both observed types are known, they must agree.
    ta = (child_a_type or "").strip().lower()
    tb = (child_b_type or "").strip().lower()
    if ta and tb and ta not in ("unknown", "") and tb not in ("unknown", "") and ta != tb:
        return False, f"type_mismatch:{ta}!={tb}"
    return True, "ok"


def _is_coherent_category_token(name: str, roots: set) -> bool:
    """PURE gate — is ``name`` a COHERENT GROWN-ROOT category token (no DB, no LLM, no cosine)?

    A grown root is "coherent" (GRACE TO INTENT, over ISLANDS) iff it is a GENUINE category one
    could file things under — NOT a seeded root (those terminate PRIMARILY, handled by the caller),
    NOT a scalar/loose-phrase, NOT a bare/­no-information upper-ontology placeholder. We reuse the
    SAME rung-4 category validator the seeded roots implicitly satisfy: passing it (or the expected
    self-bridge ``lca_equals_child``, since we validate the token AS its own candidate root) means
    the token carries real classification signal. A SEEDED root returns False here because the
    caller's PRIMARY seeded-root termination owns that case — this gate is only the GROWN backstop.

    Deterministic/pure so it is unit-testable alongside ``_validate_bridge_placement``."""
    n = (name or "").strip().lower()
    if not n:
        return False
    if n in roots:
        return False  # seeded root → PRIMARY termination, not the grown-root backstop
    ok, why = _validate_bridge_placement(n, n, n, child_a_type="", child_b_type="")
    return bool(ok or why == "lca_equals_child")


def _is_coherent_grown_root(db_conn, node_id: Optional[str], node_name: str, roots: set) -> bool:
    """DB gate — is this walked chain TIP a COHERENT GROWN ROOT we may terminate at GRACEFULLY?

    Lever 1 of "GRACE TO INTENT, over ISLANDS": when the ±6 climb builds a coherent vertical chain
    that does NOT reach a SEEDED backbone root within the bound, we accept the deepest coherent
    GROWN root the chain reached as a valid terminus (a coherent standalone grouping is first-class)
    rather than QUARANTINING a perfectly good chain (minecraft→…→program: program is a real category,
    just not a seeded root). The node stays PLACED + walkable; it just tops out at a grown root.

    A tip qualifies iff ALL hold (structural/identity — NO cosine):
      • its name is a coherent category token (``_is_coherent_category_token`` — genuine category,
        not seeded/scalar/phrase/no-info-upper-root);
      • it is NOT a NAMED INSTANCE (``_token_resolves_to_named_instance`` — THE HARD LINE: a name /
        memory can never be an L4 place, even as a terminus);
      • it already PARTICIPATES in the tenant hierarchy as a real node (is the subject OR object of a
        live hierarchy edge) — i.e. the proposed terminus resolved BY IDENTITY to an EXISTING node,
        not a bare LLM token. A walked chain tip satisfies this by construction; we assert it so the
        gate is honest and reusable.
    Read-only, fail-safe (False on any error → fall through to QUARANTINE, never a bad placement)."""
    n = (node_name or "").strip().lower()
    if not _is_coherent_category_token(n, roots):
        return False
    if _token_resolves_to_named_instance(db_conn, n):
        return False  # HARD LINE — a memory/name is never a place, not even a terminus
    rels = list(_HIERARCHY_RELS)
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM facts"
                "  WHERE (subject_id = %s OR object_id = %s) AND rel_type = ANY(%s)"
                "    AND superseded_at IS NULL AND archived_at IS NULL"
                " UNION"
                " SELECT 1 FROM staged_facts"
                "  WHERE (subject_id = %s OR object_id = %s) AND rel_type = ANY(%s)"
                "    AND promoted_at IS NULL AND deleted_at IS NULL"
                " LIMIT 1",
                (str(node_id), str(node_id), rels, str(node_id), str(node_id), rels),
            )
            return cur.fetchone() is not None
    except Exception:
        try:
            db_conn.rollback()
        except Exception:
            pass
        return False


def _propose_lca_bridge(child_a: str, child_b: str, qwen_api_url: str) -> Optional[dict]:
    """RUNG-6 BRIDGING — DESIGN-TARGET STUB (flagged `RUNG6_BRIDGING`, default OFF).

    CONTRACT (when fully implemented):
      Input  : two close-but-disjoint hierarchy branch tips that share no ancestor yet
               (e.g. `dog`-tree and `wolf`-tree, neither at `canid`).
      Step 1 : ask the user's LLM ONE bounded question — "what is the lowest common ancestor of
               <child_a> and <child_b>?" via the centralized LLM stack (a new bounded op, small
               max_tokens, JSON `{"lca": "...", "lca_type": "..."}`). LLM PROPOSES placement only —
               it never mints the tree or names final structure (Roles HARD RULE).
      Step 2 : VALIDATE the proposal with `_validate_bridge_placement` against rung-4 rules
               (entity-or-subgroup, hierarchy-only, no scalar, type-consistent, not a universal
               root). Reject on failure — a wrong bridge cannot pass.
      Step 3 : GROW IN-CHAIN — mint the LCA node + `<child> subclass_of <lca>` edges, BORN CLASS C
               (freq ≥ 3 / curation gates govern promotion); structure rules apply from birth.
      Returns: a validated bridge dict {"lca","lca_type","children":[a,b]} or None.

    Until built, this is a NO-OP that returns None and logs intent (no LLM call, no structure mint)
    so the calling sweep is structurally complete and the contract is exercised by tests. The
    deterministic validator above (`_validate_bridge_placement`) is FULLY implemented so the
    proposal→validation gate can be tested independently of the LLM call.
    """
    if not _RUNG6_BRIDGING:
        return None
    # STUB: structure is wired; the LLM proposal + in-chain mint are intentionally deferred.
    log.info("re_embedder.rung6_bridge_stub child_a=%s child_b=%s status=deferred_stub",
             str(child_a)[:32], str(child_b)[:32])
    return None


def evaluate_ontology_candidates(db_conn, qwen_api_url: str) -> dict:
    """
    dprompt-17: Evaluate novel rel_type candidates from ontology_evaluations.
    Runs each poll cycle. Decisions are made ONCE PER candidate_rel_type per cycle:
      - 'approved': SUM(occurrence_count) over undecided sibling rows >= 3 → INSERT rel_types
      - 'mapped':   cached-embedding cosine similarity to existing type > 0.85 → rewrite staged_facts
      - (sub-threshold, no match): LEFT UNDECIDED — re_embedder_decision stays NULL so the
        candidate keeps accruing on re-sighting OR is forgotten by decay_ontology_candidates().

    Per-rel_type aggregation (Frequency Analysis, the internal design record): the live
    UNIQUE constraint is the 3-column (candidate_rel_type, sample_subject_id, sample_object),
    so each distinct sample triple is its own row. Approval must use the AGGREGATE frequency
    across all sibling rows of the same rel_type, not any single row's occurrence_count.

    Cost guard: the LLM metadata call fires ONLY on an actual approval. Sub-threshold
    candidates never trigger an LLM call (and are never frozen as terminal 'rejected').
    Mapping uses the embedding cache (cheap cosine), not the LLM.

    Decisions for approved/mapped are written to ALL undecided sibling rows of the rel_type.

    Returns: {"approved": int, "mapped": int, "rejected": int, "errors": int}
    """
    stats = {"approved": 0, "mapped": 0, "rejected": 0, "errors": 0}

    try:
        # Aggregate per rel_type: SUM(occurrence_count) is the approval signal, not any
        # single sample triple. Pick a representative sample (highest-occurrence row) for
        # the LLM metadata snippet/types via DISTINCT ON ordering inside the subquery.
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT agg.candidate_rel_type,"
                "       rep.candidate_subject_type,"
                "       rep.candidate_object_type,"
                "       rep.first_text_snippet,"
                "       agg.total_occ,"
                "       rep.sample_subject_id,"
                "       rep.sample_object"
                " FROM ("
                "   SELECT candidate_rel_type, SUM(occurrence_count) AS total_occ"
                "   FROM ontology_evaluations"
                "   WHERE re_embedder_decision IS NULL"
                # CONSUMER FIREWALL — the rel-type evaluator owns rel-type CANDIDATES only.
                # ingest_miss_pushback rows are CONCEPT-classification candidates owned by
                # classify_unknown_concepts; they merely REUSE candidate_rel_type to record the
                # surfacing rel (e.g. `mira instance_of violinist` → candidate_rel_type=instance_of).
                # Without this exclusion a curated surfacing rel (instance_of/member_of) drags the
                # concept rows into the rel aggregate and the curated-rel suppression UPDATE below
                # flips them to 'already_known' — starving the whatis classifier so the role TYPE
                # (violinist) never gets a subclass_of ladder. (Structural extraction_method marker,
                # not a domain literal — same marker the concept consumer keys on.)
                "     AND extraction_method IS DISTINCT FROM 'ingest_miss_pushback'"
                # CARVE-OUT firewall: linguistic_cue_candidate rows are CARVED cue-class growth
                # candidates owned by grow_linguistic_cue_candidates — they REUSE candidate_rel_type to
                # carry the cue CATEGORY (e.g. 'social_role'), NOT a real rel_type. Excluding them here
                # prevents the rel-type evaluator from minting a bogus rel_type named after the category.
                "     AND extraction_method IS DISTINCT FROM 'linguistic_cue_candidate'"
                # CARVE-OUT firewall: aspect_synonym_miss rows are QUERY-ASPECT synonym
                # candidates owned by evaluate_aspect_synonym_candidates — sample_object
                # carries an aspect SURFACE word ("tall"), candidate_rel_type is the fixed
                # sentinel 'aspect_synonym', NOT a novel rel. Excluding them stops the
                # rel-type evaluator minting a bogus rel named after the sentinel.
                "     AND extraction_method IS DISTINCT FROM 'aspect_synonym_miss'"
                "   GROUP BY candidate_rel_type"
                " ) agg"
                " JOIN LATERAL ("
                "   SELECT candidate_subject_type, candidate_object_type,"
                "          first_text_snippet, sample_subject_id, sample_object"
                "   FROM ontology_evaluations oe"
                "   WHERE oe.candidate_rel_type = agg.candidate_rel_type"
                "     AND oe.re_embedder_decision IS NULL"
                "     AND oe.extraction_method IS DISTINCT FROM 'ingest_miss_pushback'"
                "     AND oe.extraction_method IS DISTINCT FROM 'linguistic_cue_candidate'"
                "     AND oe.extraction_method IS DISTINCT FROM 'aspect_synonym_miss'"
                "   ORDER BY oe.occurrence_count DESC, oe.last_seen_at DESC"
                "   LIMIT 1"
                " ) rep ON true"
                " ORDER BY agg.total_occ DESC"
            )
            candidates = cur.fetchall()
    except Exception as e:
        log.error(f"re_embedder.ontology_eval_fetch_failed: {e}")
        return stats
    finally:
        # BELT-AND-BRACES read barrier. `if not candidates: return stats` below returns with
        # this SELECT's transaction still OPEN on the caller's long-lived sweep connection —
        # the next subsystem then makes an LLM call inheriting it, holding AccessShareLock on
        # ontology_evaluations for the whole call. The caller's own barrier already covers
        # this; the `finally` makes the function safe for ANY caller, including the exception
        # path (which returns with the transaction INERROR, still holding every lock it took).
        release_read_transaction(db_conn, context="re_embedder.evaluate_ontology_candidates.fetch")

    if not candidates:
        return stats

    log.info(f"re_embedder.ontology_eval_candidates count={len(candidates)}")

    # Load existing rel_types for similarity comparison
    try:
        with db_conn.cursor() as cur:
            cur.execute("SELECT rel_type FROM rel_types ORDER BY rel_type")
            existing_types = [row[0] for row in cur.fetchall()]
    except Exception:
        existing_types = []

    # ── FIX #1: never RE-APPROVE / REGENERATE an EXISTING curated rel ──────────
    # The approval path exists to MINT a NOVEL rel_type. A KNOWN rel (a seeded /
    # curated / already-grown row — e.g. participated_in, owns, instance_of) MUST
    # NOT be regenerated: re-generation injects an instance snippet into the LLM
    # prompt and the UPSERT clobbers the clean seed template
    # ("You participated in unknown"). Detect "curated" by METADATA, never a
    # rel-name list (subject-agnostic): a row that is seeded by source, OR simply
    # already carries a non-blank natural_language template, is a real curated rel.
    # The concept-OBJECT grounding for these rels is a SEPARATE consumer
    # (classify_unknown_concepts, extraction_method='ingest_miss_pushback') and is
    # NOT touched here — only the rel_type-template regeneration is suppressed.
    curated_rels: set = set()
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT rel_type FROM rel_types"
                " WHERE source IN ('wikidata', 'builtin', 'user', 'seed')"
                "    OR (natural_language IS NOT NULL"
                "        AND btrim(natural_language) <> '')"
            )
            curated_rels = {row[0] for row in cur.fetchall()}
    except Exception as _ce:
        # Fail-safe: if we cannot read the curated set, fall back to the existing
        # set of rel_types (any already-existing rel is treated as known). This is
        # the SAFE direction — it suppresses regeneration of ALL known rels rather
        # than risk clobbering a seed; novel rels (absent from rel_types) still mint.
        log.warning(f"re_embedder.curated_rels_fetch_failed reason={_ce} — "
                    f"falling back to existing_types as the known set")
        curated_rels = set(existing_types)

    for row in candidates:
        # READ BARRIER (per iteration): this loop body blocks on the brain/Qdrant, and a
        # read left open by the PREVIOUS iteration would ride across it. A batch-level
        # barrier alone does not cover this — measured live: climb_state and the
        # taxonomy reads were each caught idle-in-transaction at 58-59s inside a loop.
        release_read_transaction(db_conn, context="re_embedder.evaluate_ontology_candidates.iteration")
        candidate_rel, subj_type, obj_type, snippet, occ, subj_id, obj = row
        try:
            decision = None
            reason = ""
            best_fit = None
            best_score = 0.0

            # ── Decision 1: Pattern frequency (per-rel_type aggregate) ──
            # occ is SUM(occurrence_count) over all undecided sibling rows of this
            # rel_type — the aggregate frequency, not a single sample triple's count.
            # ⚠️ UNGATED 2026-08-27 (owner ruling — see the note on LINGUISTIC_CUE_GROWTH_THRESHOLD).
            # A rel_type is ENGINE STRUCTURE — a SHELF, ours — so it becomes usable the moment it is
            # derived. The old `occ >= 3` held a derived rel in ontology_evaluations for two more
            # sightings, during which the fact that needed it had nowhere walkable to live and the
            # user could not see (and therefore could not correct) the shape the engine had chosen.
            # A NOVEL rel is minted with source='engine'/engine_generated and NEVER overwrites a
            # curated or user rel (the trust hierarchy user > wikidata > engine > builtin is enforced
            # at /ontology/rel_types and by the curated_rels guard above), so ungating cannot damage
            # seeded ontology — it only lets the tenant's own grown shelf exist sooner.
            #
            # ⚠️ COST CONSEQUENCE, STATED PLAINLY (do not discover this in a bill). The `approved`
            # branch below calls `_query_llm_for_rel_type_metadata` — ONE LLM call per approved rel.
            # At the legacy gate that call fired on the THIRD sighting; at threshold 1 it fires on
            # the FIRST. It is one call per DISTINCT NOVEL rel_type per tenant, not per turn, and
            # rel_types converge quickly — but on a slow CPU-hosted LLM it is a
            # real, measurable increase in the first hours of a new tenant, and it front-loads it.
            # If that becomes the binding constraint the right answer is to APPROVE the rel
            # immediately and DEFER only its metadata enrichment, not to re-gate approval.
            # ROLLBACK LEVER: REL_TYPE_APPROVAL_THRESHOLD=3 restores the legacy gate exactly.
            if occ >= REL_TYPE_APPROVAL_THRESHOLD:
                decision = "approved"
                reason = f"aggregate_occurrence={occ} >= {REL_TYPE_APPROVAL_THRESHOLD}"

            # ── Decision 2: Semantic similarity (DEMOTED — RUNG-6) ──────
            # The cosine>0.85 → auto-rewrite collapse is RETIRED as the primary mechanism
            # (DESIGN-hierarchy-ladder-and-growth §Growth: "This retires the re_embedder
            # cosine-map"). Deterministic convergence-by-identity + curated rel_type_aliases
            # are primary. Cosine is now at most a GATED SUGGESTION: by default
            # (`ONTOLOGY_COSINE_MAP` OFF) we compute the score for visibility/logging but do
            # NOT rewrite staged_facts — fuzzy links surface wrong groundings (owns vs rents are
            # cosine-close). The candidate is left UNDECIDED so it can earn approval by frequency
            # or be collapsed deterministically via a curated alias. Set ONTOLOGY_COSINE_MAP=true
            # to restore the legacy auto-rewrite.
            if not decision and existing_types:
                # dprompt-121: Use embedding cache to avoid re-embedding same types
                candidate_text = f"relationship: {candidate_rel}"
                candidate_vector = _embedding_cache.get(candidate_text)
                if not candidate_vector:
                    candidate_vector = embed_text(
                        candidate_text,
                        qwen_api_url, timeout=10.0, fallback=True
                    )
                    if candidate_vector:
                        _embedding_cache.set(candidate_text, candidate_vector)

                if candidate_vector:
                    # Compute cosine similarity to each existing type
                    best_score = 0.0
                    best_fit = None
                    for ext in existing_types:
                        # READ BARRIER (per iteration): this loop body blocks on the brain/Qdrant, and a
                        # read left open by the PREVIOUS iteration would ride across it. A batch-level
                        # barrier alone does not cover this — measured live: climb_state and the
                        # taxonomy reads were each caught idle-in-transaction at 58-59s inside a loop.
                        release_read_transaction(db_conn, context="re_embedder.evaluate_ontology_candidates.iteration")
                        ext_text = f"relationship: {ext}"
                        # dprompt-121: Check cache first
                        ext_vector = _embedding_cache.get(ext_text)
                        if not ext_vector:
                            ext_vector = embed_text(
                                ext_text,
                                qwen_api_url, timeout=10.0, fallback=True
                            )
                            if ext_vector:
                                _embedding_cache.set(ext_text, ext_vector)

                        if ext_vector:
                            sim = _cosine_similarity(candidate_vector, ext_vector)
                            if sim > best_score:
                                best_score = sim
                                best_fit = ext

                    if best_score > _ONTOLOGY_COSINE_THRESHOLD and best_fit:
                        if _ONTOLOGY_COSINE_MAP:
                            # LEGACY behaviour (flag ON): auto-rewrite as before.
                            decision = "mapped"
                            reason = f"similarity={best_score:.3f} to '{best_fit}'"
                        else:
                            # DEMOTED (flag OFF, default): suggestion only. Gate the suggestion by
                            # type-consistency (the cheap deterministic guard) before even logging
                            # it as actionable — a type-mismatched cosine hit is noise, not a synonym.
                            _ok, _why = _validate_bridge_placement(
                                candidate_rel, best_fit, best_fit,
                                child_a_type=subj_type or "", child_b_type=obj_type or "",
                            )
                            log.info(
                                "re_embedder.cosine_map_suggestion_demoted "
                                f"candidate={candidate_rel} best_fit={best_fit} "
                                f"score={best_score:.3f} gate_ok={_ok} reason={_why} "
                                f"(NOT applied — ONTOLOGY_COSINE_MAP off; curated alias is the "
                                f"deterministic path)"
                            )
                            # Leave decision = None → candidate stays UNDECIDED (Decision 3).

            # ── Decision 3: Defer (NOT a terminal reject) ───────────────
            # Sub-threshold candidates with no strong semantic match are LEFT UNDECIDED
            # (re_embedder_decision stays NULL). This is the key behavioural change: a
            # one-off relationship word is no longer frozen as 'rejected'. Instead it
            # remains a live candidate that either keeps accruing on re-sighting (the
            # ingest ON CONFLICT bumps occurrence_count/last_seen_at) or is eventually
            # forgotten by decay_ontology_candidates(). No DB write, no LLM call here.
            if not decision:
                stats["rejected"] += 1  # counted as "deferred this cycle" for visibility
                log.debug(
                    f"re_embedder.ontology_deferred rel_type={candidate_rel} "
                    f"aggregate_occ={occ} best={best_fit}:{best_score:.3f}"
                )
                continue

            # ── FIX #1: EXISTING curated rel reached approval — SUPPRESS regeneration ──
            # A KNOWN curated/seed rel must NEVER be re-minted from a sample instance
            # (that is what clobbered "You participated in Y" → "You participated in
            # unknown"). Resolve the candidate group as DECIDED so it stops re-firing,
            # but DO NOT call the LLM and DO NOT touch the rel_types template. The
            # rel's other growth (object grounding) flows through its own consumer,
            # untouched. Subject-agnostic: gated on the curated-set membership, not a
            # rel-name literal.
            if decision == "approved" and candidate_rel in curated_rels:
                with db_conn.cursor() as cur:
                    cur.execute(
                        "UPDATE ontology_evaluations SET"
                        "  re_embedder_decision = 'already_known',"
                        "  decision_timestamp = now(),"
                        "  decision_reason = %s,"
                        "  created_rel_type = %s"
                        " WHERE candidate_rel_type = %s AND re_embedder_decision IS NULL"
                        # never clobber concept-classification rows (see consumer firewall above)
                        "   AND extraction_method IS DISTINCT FROM 'ingest_miss_pushback'",
                        (f"existing curated rel — regeneration suppressed ({reason})",
                         candidate_rel, candidate_rel),
                    )
                db_conn.commit()
                log.info(
                    "re_embedder.ontology_existing_rel_skipped "
                    f"rel_type={candidate_rel} aggregate_occ={occ} "
                    f"reason=curated_rel_template_preserved (no LLM, no rel_types UPSERT)"
                )
                continue

            # ── Apply decision ──────────────────────────────────────────
            with db_conn.cursor() as cur:
                if decision == "approved":
                    # dprompt-126: Phase 2 — Query LLM for natural language metadata
                    try:
                        # READ BARRIER (immediately before the blocking call — the RE-ARM case). A barrier at
                        # the top of the enclosing block is NOT enough: a per-row read helper opens a FRESH
                        # transaction after it, and that read then rides across this hop. Measured live on the
                        # deployed image — climb_classification_chains was killed twice this way (02:42:25 and
                        # 02:45:06), its whole _ont_db subsystem chain failing 'connection already closed' four
                        # seconds later.
                        release_read_transaction(db_conn, context="re_embedder.evaluate_ontology_candidates.pre_blocking_call")
                        llm_metadata = _query_llm_for_rel_type_metadata(
                            candidate_rel, subj_type, obj_type, snippet, qwen_api_url,
                            raise_on_nonanswer=True,
                        )
                    except LLMUnavailable as _md_unavail:
                        # DEFER, do not decide. Leaving re_embedder_decision NULL is this
                        # lane's own documented non-terminal state ("Decision 3: Defer") — the
                        # candidate keeps accruing occurrences and is re-evaluated on a later
                        # sweep. Writing 'approved' here would be terminal (every fetch in this
                        # lane is `WHERE re_embedder_decision IS NULL`) and would persist the
                        # ANY/ANY wildcard the missing metadata forces.
                        stats["brain_unavailable"] = stats.get("brain_unavailable", 0) + 1
                        log.warning(
                            f"re_embedder.ontology_approve_deferred_brain_unavailable "
                            f"rel_type={candidate_rel} reason={_md_unavail.reason} "
                            f"note=candidate left UNDECIDED; no ANY/ANY rel minted, no "
                            f"'approved' stamped — re-evaluated on a later sweep")
                        continue

                    # Register the new rel_type with full metadata
                    label = llm_metadata.get("llm_natural_language", "").split(" is ")[0].title() if llm_metadata.get("llm_natural_language") else candidate_rel.replace('_', ' ').title()
                    natural_language = llm_metadata.get("llm_natural_language", "")
                    natural_language_2p = llm_metadata.get("llm_natural_language_2p") or None
                    is_symmetric = llm_metadata.get("llm_is_symmetric", False)
                    inverse_rel_type = llm_metadata.get("llm_inverse_rel_type")
                    category = llm_metadata.get("llm_category", "other")

                    # Type constraints (Severance #2): prefer the LLM-inferred head/tail types
                    # from the metadata call. Fall back to observed entity types only when the
                    # LLM was uncertain. NEVER persist NULL head/tail types when inference
                    # succeeded — that is the mechanical cause of the 18 empty-head_types rows.
                    head_types = llm_metadata.get("llm_head_types")
                    tail_types = llm_metadata.get("llm_tail_types")
                    is_hierarchy = False

                    if not head_types and subj_type and subj_type != "unknown":
                        head_types = [subj_type]
                    if not tail_types and obj_type and obj_type != "unknown":
                        tail_types = [obj_type]
                    # Last resort so WGM validation is not a silent no-op: unconstrained ANY.
                    if not head_types:
                        head_types = ["ANY"]
                    if not tail_types:
                        tail_types = ["ANY"]

                    # Heuristic: if rel_type suggests classification/taxonomy, mark as hierarchy
                    if any(keyword in candidate_rel.lower() for keyword in ("instance_of", "subclass_of", "member_of", "is_a", "part_of", "type_of")):
                        is_hierarchy = True

                    # dprompt-148: Assign fact_class based on LLM confidence
                    # High confidence (>= 0.7) → Class B (LLM-inferred, can be promoted)
                    # Medium confidence (0.5-0.7) → Class B (staged)
                    # Low confidence (< 0.5) → Class C (ephemeral)
                    llm_confidence = llm_metadata.get("llm_confidence", 0.6)
                    assigned_fact_class = "B" if llm_confidence >= 0.5 else "C"

                    # AUTHORITY GUARD (user > SEED > growth): if this candidate is actually a
                    # SEED (defense-in-depth — ontology_evaluations should hold novel rels only),
                    # pin the structural fields to the public seed so the unconditional
                    # is_symmetric/inverse/category/fact_class writes below can never re-classify
                    # it. head_types stays backfill-only (additive). See _seed_structural_flags.
                    _seed_pin = _seed_structural_flags(db_conn, candidate_rel)
                    if _seed_pin is not None:
                        is_hierarchy = _seed_pin["is_hierarchy_rel"]
                        is_symmetric = _seed_pin["is_symmetric"]
                        inverse_rel_type = _seed_pin["inverse_rel_type"]
                        category = _seed_pin["category"]
                        assigned_fact_class = _seed_pin["fact_class"] or assigned_fact_class
                        if _seed_pin["tail_types"]:
                            tail_types = _seed_pin["tail_types"]

                    # Severance #2: backfill head/tail types only when currently empty —
                    # never clobber existing good constraints (CASE guards in the UPDATE).
                    cur.execute(
                        "INSERT INTO rel_types"
                        " (rel_type, label, natural_language, natural_language_2p, engine_generated, confidence, source,"
                        "  head_types, tail_types, is_hierarchy_rel, is_symmetric, inverse_rel_type, category, fact_class)"
                        " VALUES (%s, %s, %s, %s, true, %s, 'engine', %s, %s, %s, %s, %s, %s, %s)"
                        " ON CONFLICT (rel_type) DO UPDATE SET"
                        # FIX #2: COALESCE so a NULL/blank EXCLUDED value can NEVER nuke a
                        # good existing template (defense-in-depth behind the FIX #1 skip).
                        "  natural_language = COALESCE(NULLIF(btrim(EXCLUDED.natural_language), ''), rel_types.natural_language),"
                        "  natural_language_2p = COALESCE(EXCLUDED.natural_language_2p, rel_types.natural_language_2p),"
                        "  is_symmetric = EXCLUDED.is_symmetric,"
                        "  inverse_rel_type = EXCLUDED.inverse_rel_type,"
                        "  category = EXCLUDED.category,"
                        "  head_types = CASE WHEN (rel_types.head_types IS NULL"
                        "                          OR rel_types.head_types = ARRAY[]::TEXT[])"
                        "                    THEN EXCLUDED.head_types ELSE rel_types.head_types END,"
                        "  tail_types = CASE WHEN (rel_types.tail_types IS NULL"
                        "                          OR rel_types.tail_types = ARRAY[]::TEXT[])"
                        "                    THEN EXCLUDED.tail_types ELSE rel_types.tail_types END,"
                        "  fact_class = EXCLUDED.fact_class",
                        (candidate_rel, label, natural_language, natural_language_2p, 0.8, head_types, tail_types, is_hierarchy, is_symmetric, inverse_rel_type, category, assigned_fact_class),
                    )
                    stats["approved"] += 1
                    log.info(f"re_embedder.ontology_approved rel_type={candidate_rel} category={category} is_symmetric={is_symmetric} natural_language={natural_language[:50]} {reason}")

                    # Fix B (dprompt-156): propagate new rel_type to entity_taxonomies so
                    # determine_path() can route queries through it immediately.
                    # Category → taxonomy_name mapping is DB-driven: try exact match first,
                    # then ILIKE fallback. No hardcoded category→taxonomy mappings.
                    # Guard: hierarchy rel_types (is_hierarchy=True) must never appear in
                    # rel_types_defining_group — they classify types, not define group membership.
                    if category and not is_hierarchy:
                        try:
                            with db_conn.cursor() as _tax_cur:
                                _tax_cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                                    """
                                    UPDATE entity_taxonomies
                                    SET rel_types_defining_group = array_append(rel_types_defining_group, %s)
                                    WHERE taxonomy_name = %s
                                      AND NOT (rel_types_defining_group @> ARRAY[%s]::TEXT[])
                                    """,
                                    (candidate_rel, category, candidate_rel),
                                )
                                if _tax_cur.rowcount > 0:
                                    log.info("re_embedder.taxonomy_rel_type_appended "
                                             f"rel_type={candidate_rel} taxonomy={category}")
                                else:
                                    # Exact match found no row — try ILIKE fallback
                                    _tax_cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                                        """
                                        UPDATE entity_taxonomies
                                        SET rel_types_defining_group = array_append(rel_types_defining_group, %s)
                                        WHERE taxonomy_name ILIKE %s
                                          AND NOT (rel_types_defining_group @> ARRAY[%s]::TEXT[])
                                        """,
                                        (candidate_rel, category, candidate_rel),
                                    )
                                    if _tax_cur.rowcount > 0:
                                        log.info("re_embedder.taxonomy_rel_type_appended_ilike "
                                                 f"rel_type={candidate_rel} taxonomy_pattern={category}")
                                    else:
                                        log.debug("re_embedder.taxonomy_no_match_for_category "
                                                  f"rel_type={candidate_rel} category={category}")
                        except Exception as _tax_err:
                            log.warning("re_embedder.taxonomy_append_failed "
                                        f"rel_type={candidate_rel} error={str(_tax_err)[:100]}")
                    elif is_hierarchy:
                        log.debug("re_embedder.taxonomy_append_skipped_hierarchy "
                                  f"rel_type={candidate_rel}")

                    # Refresh unified metadata cache so the newly approved rel_type
                    # is immediately available to the ingest pipeline without waiting
                    # for next container restart (dprompt-76b / dBug-015).
                    try:
                        from src.api.main import _refresh_rel_type_cache
                        _refresh_rel_type_cache()
                        log.info(f"re_embedder.cache_refresh trigger=ontology_approved rel_type={candidate_rel}")
                    except Exception as _cache_err:
                        log.warning(f"re_embedder.cache_refresh_failed rel_type={candidate_rel}: {_cache_err}")

                elif decision == "mapped" and best_fit:
                    # Rewrite staged_facts using this rel_type to use best_fit instead
                    cur.execute(
                        "UPDATE staged_facts SET rel_type = %s, qdrant_synced = false"
                        " WHERE rel_type = %s AND promoted_at IS NULL AND expires_at > now()",
                        (best_fit, candidate_rel),
                    )
                    n_rewritten = cur.rowcount
                    stats["mapped"] += 1
                    log.info(
                        f"re_embedder.ontology_mapped "
                        f"from={candidate_rel} to={best_fit} "
                        f"rewritten={n_rewritten} score={best_score:.3f}"
                    )

                # Reject is no longer a terminal decision reached here — sub-threshold
                # candidates `continue` before this apply block (left undecided). Only
                # 'approved' and 'mapped' reach this point.

                # Write the decision to ALL undecided sibling rows of this rel_type
                # (not a single id) so the whole candidate group is resolved together.
                if decision == "approved":
                    cur.execute(
                        "UPDATE ontology_evaluations SET"
                        "  re_embedder_decision = %s,"
                        "  re_embedder_confidence = %s,"
                        "  decision_timestamp = now(),"
                        "  decision_reason = %s,"
                        "  created_rel_type = %s,"
                        "  llm_natural_language = %s,"
                        "  llm_is_symmetric = %s,"
                        "  llm_inverse_rel_type = %s,"
                        "  llm_category = %s,"
                        "  llm_fact_class = %s,"
                        "  llm_confidence = %s,"
                        "  llm_metadata_json = %s"
                        " WHERE candidate_rel_type = %s AND re_embedder_decision IS NULL"
                        # never clobber concept-classification rows (see consumer firewall above)
                        "   AND extraction_method IS DISTINCT FROM 'ingest_miss_pushback'",
                        (decision, 0.8, reason, candidate_rel,
                         llm_metadata.get("llm_natural_language", ""),
                         llm_metadata.get("llm_is_symmetric", False),
                         llm_metadata.get("llm_inverse_rel_type"),
                         llm_metadata.get("llm_category", "other"),
                         llm_metadata.get("llm_fact_class", "B"),
                         llm_metadata.get("llm_confidence", 0.6),
                         llm_metadata.get("llm_metadata_json", "{}"),
                         candidate_rel),
                    )
                else:  # mapped
                    cur.execute(
                        "UPDATE ontology_evaluations SET"
                        "  re_embedder_decision = %s,"
                        "  re_embedder_confidence = %s,"
                        "  decision_timestamp = now(),"
                        "  decision_reason = %s,"
                        "  best_fit_rel_type = %s,"
                        "  best_fit_score = %s,"
                        "  created_rel_type = %s"
                        " WHERE candidate_rel_type = %s AND re_embedder_decision IS NULL"
                        # never clobber concept-classification rows (see consumer firewall above)
                        "   AND extraction_method IS DISTINCT FROM 'ingest_miss_pushback'",
                        (decision, best_score, reason, best_fit, best_score,
                         best_fit, candidate_rel),
                    )

            db_conn.commit()

        except Exception as e:
            db_conn.rollback()
            stats["errors"] += 1
            log.error(f"re_embedder.ontology_eval_error rel_type={candidate_rel}: {e}")

    return stats


def drain_pending_placement_by_morphology(db_conn, dsn: str, schema_name: str) -> dict:
    """BACKGROUND DRAIN — reconcile EXISTING `pending_placement` rels onto their SEEDED
    canonical IN PLACE, deterministically, so their already-stored facts become walkable
    WITHOUT re-ingest.

    Companion to the in-flow morphology fold (the 48c3200 seam / main.py): that seam folds
    a FRESH ingest of a pending rel onto its seed, but a rel already minted as
    `category='pending_placement'` from a PRIOR ingest stays an orphan until it is ingested
    again. This pass walks the EXISTING pending rels each cycle and reconciles any that
    morphology-match a SEEDED canonical (`live_in` → `lives_in`), in place.

    Per pending rel (`rel_types.category = 'pending_placement'`, source NOT a seeded source):
      morphology-fold against the SEEDED canonical set (exact normalized-form membership,
      NEVER cosine). On a SEEDED match:
        1. record_alias(pending → seeded, source='engine')  — deterministic synonym row.
        2. UPDATE the pending rel's `category` to the SEEDED canonical's category (adopt
           the region, e.g. 'location').
        3. APPEND the pending rel to every taxonomy whose `rel_types_defining_group` already
           names the seeded canonical (idempotent array_append, the SAME mechanism
           evaluate_ontology_candidates uses) so determine_path()/query scope anchors it.
      A pending rel that does NOT morphology-match a seed is LEFT pending (the freq>=3 / LLM
      path still owns it — anti-pollution invariant: never auto-place a non-matching rel).

    Deterministic + per-tenant: the caller has bound `SET search_path TO {schema}` (NO public)
    on db_conn; the canonical reads bind the same schema. Cosine stays OFF. Fail-safe: any
    error on a single rel rolls back ONLY that rel (savepoint) and the loop continues; a fatal
    error returns the partial stats without crashing the sweep.

    Returns: {"reconciled": int, "scanned": int, "errors": int}
    """
    stats = {"reconciled": 0, "scanned": 0, "errors": 0}

    try:
        from src.ontology.canonical import (
            resolve_seeded_by_morphology as _resolve_seeded_morph,
            record_alias as _record_alias,
            reset_caches as _reset_canon_caches,
            _SEEDED_SOURCES,
        )
    except Exception as e:
        log.error(f"re_embedder.pending_drain_import_failed: {e}")
        return stats

    # Pull the pending, NON-seeded rels under the bound tenant schema (no public).
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT rel_type FROM rel_types"
                " WHERE category = %s AND lower(COALESCE(source, '')) NOT IN %s",
                (_CATEGORY_PENDING_RE, tuple(_SEEDED_SOURCES)),
            )
            pending_rels = [r[0] for r in cur.fetchall() if r[0]]
    except Exception as e:
        log.error(f"re_embedder.pending_drain_fetch_failed schema={schema_name}: {e}")
        return stats
    finally:
        # BELT-AND-BRACES read barrier — see evaluate_ontology_candidates. `if not
        # pending_rels: return stats` below is one of the three confirmed leak sites: it
        # returns holding AccessShareLock on rel_types, and the next subsystem's LLM call
        # inherits it.
        release_read_transaction(
            db_conn,
            context=f"re_embedder.drain_pending_placement_by_morphology.fetch schema={schema_name}")

    if not pending_rels:
        return stats

    stats["scanned"] = len(pending_rels)

    for pending in pending_rels:
        _pending = (pending or "").strip().lower()
        if not _pending:
            continue
        try:
            # 0. SEEDED morphology match ONLY — never fold onto a tenant-grown rel, never
            #    cosine. A miss leaves the rel pending for the freq>=3/LLM path.
            seeded = _resolve_seeded_morph(_pending, dsn, schema_name)
            if not seeded or seeded == _pending:
                continue

            with db_conn.cursor() as cur:
                cur.execute("SAVEPOINT sp_pending_drain")
                try:
                    # 2. Adopt the seeded canonical's category (the region). Read the
                    #    seed's own category, then stamp it onto the pending rel.
                    cur.execute(
                        "SELECT category FROM rel_types WHERE rel_type = %s",
                        (seeded,),
                    )
                    _seed_row = cur.fetchone()
                    seed_category = _seed_row[0] if _seed_row else None
                    if seed_category and seed_category != _CATEGORY_PENDING_RE:
                        cur.execute(
                            "UPDATE rel_types SET category = %s"
                            " WHERE rel_type = %s AND category = %s",
                            (seed_category, _pending, _CATEGORY_PENDING_RE),
                        )

                    # 3. Join the pending rel into every taxonomy that already names the
                    #    seeded canonical in rel_types_defining_group (idempotent append,
                    #    the SAME mechanism as the approval-path taxonomy propagation).
                    cur.execute(
                        "UPDATE entity_taxonomies"
                        " SET rel_types_defining_group ="
                        "     array_append(rel_types_defining_group, %s)"
                        " WHERE (rel_types_defining_group @> ARRAY[%s]::TEXT[])"
                        "   AND NOT (rel_types_defining_group @> ARRAY[%s]::TEXT[])",
                        (_pending, seeded, _pending),
                    )
                    cur.execute("RELEASE SAVEPOINT sp_pending_drain")
                except Exception as _inner:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_pending_drain")
                    stats["errors"] += 1
                    log.error(
                        f"re_embedder.pending_drain_reconcile_failed "
                        f"rel={_pending} seeded={seeded} schema={schema_name}: {str(_inner)[:160]}"
                    )
                    continue

            # 1. record_alias commits on its own connection (canonical.py) — do it after the
            #    in-band UPDATEs so the category/taxonomy work and the alias row land together
            #    on success. record_alias is fail-soft (returns False, never raises here).
            try:
                _record_alias(_pending, seeded, False, "engine", dsn, schema_name)
            except Exception as _ae:
                log.warning(
                    f"re_embedder.pending_drain_alias_failed rel={_pending} seeded={seeded}: {str(_ae)[:120]}"
                )

            db_conn.commit()
            stats["reconciled"] += 1
            log.info(
                f"re_embedder.pending_placement_drained schema={schema_name} "
                f"rel={_pending} seeded={seeded} category={seed_category}"
            )
        except Exception as e:
            try:
                db_conn.rollback()
            except Exception:
                pass
            stats["errors"] += 1
            log.error(
                f"re_embedder.pending_drain_error rel={_pending} schema={schema_name}: {str(e)[:160]}"
            )

    # Invalidate the canonical/alias cache for this tenant so the new alias rows + folded
    # category are visible to the next in-flow resolve without a restart.
    if stats["reconciled"] > 0:
        try:
            _reset_canon_caches(schema_name)
        except Exception:
            pass

    # ── UNDRAINABLE BACKLOG — make the silence countable (FAIL LOUD) ──────────────────────
    # This drain folds a pending rel ONLY onto a SEEDED canonical it MORPHOLOGY-matches
    # (`live_in` → `lives_in`). That predicate is deliberately narrow, and the population it
    # is fed is dominated by semantically NOVEL predicates (`hike`, `wash`, `commute`) which
    # match no seed by construction. The two states are byte-identical in the data: a drain
    # that never ran, and a drain that ran correctly over a population where its predicate is
    # never true. Returning a bare `reconciled=0` reported neither.
    #
    # This is not bookkeeping. A rel stuck at `category='pending_placement'` is NEVER appended
    # to `entity_taxonomies.rel_types_defining_group`, so the query path's
    # `rel_type = ANY(allowed_rels)` projection can never admit it — every fact filed under it
    # is INVISIBLE to a scoped recall (stated in migrations/197 and mirrored at :2498 above).
    # So the honest unit of the backlog is not "rels" but "USER FACTS currently unreachable".
    #
    # Emitted at CRIT only when real memory is affected (rels that actually carry facts) so a
    # tenant whose pending rels are all factless stays quiet. Fail-safe: any error here is
    # swallowed — an observability probe must never break the sweep. Counts are added to
    # `stats` so a test can pin them without scraping logs.
    try:
        _remaining = max(0, stats["scanned"] - stats["reconciled"])
        if _remaining > 0:
            with db_conn.cursor() as cur:
                cur.execute(
                    "SELECT count(DISTINCT r.rel_type), COALESCE(sum(c.n), 0)"
                    "  FROM rel_types r"
                    "  JOIN LATERAL ("
                    "       SELECT count(*) AS n FROM ("
                    "         SELECT 1 FROM facts f"
                    "          WHERE f.rel_type = r.rel_type AND f.superseded_at IS NULL"
                    "            AND f.archived_at IS NULL AND f.deleted_at IS NULL"
                    "         UNION ALL"
                    "         SELECT 1 FROM staged_facts s"
                    "          WHERE s.rel_type = r.rel_type AND s.promoted_at IS NULL"
                    "            AND s.deleted_at IS NULL"
                    "       ) u"
                    "  ) c ON TRUE"
                    " WHERE r.category = %s AND c.n > 0",
                    (_CATEGORY_PENDING_RE,),
                )
                _row = cur.fetchone() or (0, 0)
            stats["unplaceable_rels"] = int(_row[0] or 0)
            stats["unplaceable_facts"] = int(_row[1] or 0)
            if stats["unplaceable_rels"] > 0:
                # `_doc_log_crit` is this module's ONLY CRITICAL renderer, and it is not
                # document-specific despite the name: `log` here is a STDLIB logger, so
                # src.api.logging_config.log_crit's kwargs-passthrough shape raises
                # TypeError (see its docstring at :3360). Renaming it is out of scope.
                _doc_log_crit(
                    "re_embedder.pending_placement_backlog_undrainable",
                    schema=schema_name,
                    scanned=stats["scanned"], reconciled=stats["reconciled"],
                    unplaceable_rels=stats["unplaceable_rels"],
                    unplaceable_facts=stats["unplaceable_facts"],
                    note="pending rels carrying REAL facts that no drain can place: this "
                         "morphology fold only matches a SEEDED canonical, and a semantically "
                         "novel predicate matches none. Their facts are stored but INVISIBLE "
                         "to scoped recall (never enter rel_types_defining_group, so "
                         "allowed_rels can never admit them). reconciled=0 here means the "
                         "predicate did not match — NOT that the lane is dark.",
                )
    except Exception as _ble:  # noqa: BLE001 — observability must never break the sweep
        try:
            db_conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        log.warning(
            f"re_embedder.pending_backlog_probe_failed schema={schema_name}: {str(_ble)[:160]}")

    return stats


# ════════════════════════════════════════════════════════════════════════════════════════════════
# ENGINE SYNONYM CONVERGENCE — deterministic-gated collapse of a LIFTED novel rel onto a SEEDED rel
# when it is a SEMANTIC synonym the morphology-fold could NOT see ("marry"→spouse, "adopt"→has_pet).
# Companion to drain_pending_placement_by_morphology (which only folds MORPHOLOGY matches).
# ════════════════════════════════════════════════════════════════════════════════════════════════

# Flag-gated (default ON, env-configurable). OFF → the pass is a no-op (leaves every novel rel novel).
_ENGINE_SYNONYM_CONVERGENCE = _flag("ENGINE_SYNONYM_CONVERGENCE", "true")


def _synonym_conv_min_conf() -> float:
    """Confidence floor the LLM equivalence PROPOSAL must clear before Gate-3 write. Env-configurable.
    The LLM only PROPOSES; this is a deterministic accept-gate ON TOP of the type rail — never a
    cosine/similarity score. Fail-safe: unparseable env → 0.75."""
    try:
        return float(os.getenv("ENGINE_SYNONYM_CONVERGENCE_MIN_CONF", "0.75"))
    except (TypeError, ValueError):
        return 0.75


def _synonym_conv_batch() -> int:
    """Bounded count of novel rels that reach the (LLM) equivalence gate per tenant per cycle — an
    LLM-cost bound only; Gate-1 type filtering is deterministic and runs on all of them first."""
    try:
        return max(1, int(os.getenv("ENGINE_SYNONYM_CONVERGENCE_BATCH", "5")))
    except (TypeError, ValueError):
        return 5


# Sentinels for the "attempted → left novel" memo parked in ontology_evaluations so a non-converging
# rel is not re-sent to the LLM every cycle (the unique key is (candidate_rel_type, sample_subject_id,
# sample_object); NULLs are distinct in a UNIQUE index so we use concrete sentinel strings).
_SYNONYM_CONV_METHOD = "synonym_convergence"
_SYNONYM_CONV_MEMO_SUBJ = "__synonym_convergence__"
_SYNONYM_CONV_MEMO_OBJ = "__memo__"
# The decision written when the brain did NOT answer. Deliberately NOT 'left_novel': the
# skip-probe in converge_lifted_synonyms matches 'left_novel' exactly, so this value leaves the
# door OPEN for a later sweep while still being visible to an operator reading the table.
_MEMO_BRAIN_UNAVAILABLE = "llm_unavailable"


# ════════════════════════════════════════════════════════════════════════════════════════════════
# ASPECT-SYNONYM GROWTH — ASYNC BACKSTOP for the query-side inline map-and-grow.
# The inline path (main.determine_path) grows tall→height on the FIRST miss when the tenant brain
# answers within the hard hot-path timeout; when it TIMES OUT it parks the miss in
# ontology_evaluations (extraction_method='aspect_synonym_miss'). This pass drains those so the
# NEXT query is deterministic regardless. Grown link = a rel_type_aliases row read by the EXISTING
# keyword→rel_type alias lane (query walk stays model-free). Per-tenant, confidence-gated, bounded
# to attributes the anchor actually holds, subject-agnostic. Companion to the rel synonym-convergence.
# ════════════════════════════════════════════════════════════════════════════════════════════════

_ASPECT_SYNONYM_GROWTH_ENABLED = _flag("ASPECT_SYNONYM_GROWTH", "true")
_ASPECT_SYN_METHOD_EMB = "aspect_synonym_miss"  # must match main._ASPECT_SYN_METHOD


def _aspect_syn_min_conf_emb() -> float:
    """Confidence floor for the async aspect-map GROW. Env-configurable; fail-safe → 0.75."""
    try:
        return float(os.getenv("ASPECT_SYNONYM_MIN_CONF", "0.75"))
    except (TypeError, ValueError):
        return 0.75


def _aspect_map_via_llm_async(aspect_word: str, candidates: list, qwen_api_url: str):
    """Background (retry-OK) brain call mirroring main._aspect_map_via_llm: does '<aspect_word>'
    refer to EXACTLY ONE of the things this user actually tracks (a scalar attribute OR a
    relationship the anchor holds), or NONE? Returns ``(canonical|None, confidence, called_ok)``.

    ⚠️ THE THIRD ELEMENT IS THE WHOLE POINT, AND ITS ABSENCE WAS A LIVE DEFECT. The INLINE twin
    ``main._aspect_map_via_llm`` has always returned ``called_ok`` and its caller branches on it —
    "Brain judged NONE → memoize" vs "timed out/failed → async backstop". This ASYNC twin returned
    only 2 values, so ``evaluate_aspect_synonym_candidates`` wrote the PERMANENT
    ``left_unmapped`` memo on a brain outage — the very memo the inline path is careful not to
    write, and the one ``main._aspect_word_memoized_unmapped`` honours forever. The correct
    contract already existed one file away; this lane simply did not implement it.

    Fail-safe → (None, 0.0, False): unanswered, so the caller leaves the row UNDECIDED and the
    next sweep retries. Bounded, low-token; existing LLM stack (no hardcoded timeout)."""
    _names = {c[0] for c in candidates}
    _lines = "\n".join(f'  - {a}: {(nl or a.replace("_", " "))}' for a, nl in candidates)
    prompt = (
        f"{_FAULTLINE_INTERNAL_PREFIX} You are a memory-aspect synonym resolver.\n\n"
        f'A user asked about the aspect word: "{aspect_word}".\n\n'
        "That word is NOT the stored name of anything this user tracks. Decide whether it "
        "refers to EXACTLY ONE of the user's ACTUAL memory aspects below — a scalar attribute "
        "or a relationship they hold (a lexical synonym — e.g. \"tall\" refers to a person's "
        "height, \"traveled\" refers to places they visited) — or to NONE of them.\n\n"
        f"The user's memory aspects:\n{_lines}\n\n"
        f'Pick AT MOST ONE name that "{aspect_word}" refers to. If it refers to none of '
        "them, answer null. Do NOT invent one that is not in the list.\n\n"
        "Respond with ONLY valid JSON (no markdown):\n"
        '{"attribute": "<one name exactly, or null>", "confidence": 0.0-1.0}'
    )
    try:
        result = call_llm_with_retry_sync(
            messages=[{"role": "user", "content": prompt}],
            model=LLMModels.get("ENRICHMENT"),
            # PER-TENANT: the aspect word AND the candidate list are this tenant's own memory
            # aspects (evaluate_aspect_synonym_candidates runs under their search_path), so
            # this is unambiguously their spend.
            user_id=_reembedder_llm_user_id(),
            timeout=LLMTimeouts.get("ENRICHMENT"),
            operation="ENRICHMENT",
            # Same reason as the synonym-convergence gate: this caller CACHES A VERDICT.
            raise_on_unavailable=True,
        )
        if _brain_answered(result):
            attr = result.get("attribute")
            try:
                conf = float(result.get("confidence") or 0.0)
            except (TypeError, ValueError):
                conf = 0.0
            if attr and str(attr).strip().lower() in _names:
                return str(attr).strip().lower(), conf, True
            # ANSWERED, and the answer was "none of them" (or a name not in the bound
            # candidate set, which is the same non-match). That IS a verdict — memoize it.
            return None, conf, True
        log.warning(f"re_embedder.aspect_map_brain_nonanswer aspect={aspect_word} "
                    f"shape={type(result).__name__} "
                    f"error={(result or {}).get('error') if isinstance(result, dict) else None} "
                    f"note=NOT memoized; row left undecided for a later sweep")
        return None, 0.0, False
    except LLMUnavailable as e:
        log.warning(f"re_embedder.aspect_map_brain_unavailable aspect={aspect_word} "
                    f"reason={e.reason} operation={e.operation} "
                    f"note=no verdict cached; row left undecided for a later sweep")
    except Exception as e:
        log.debug(f"re_embedder.aspect_map_llm_failed aspect={aspect_word}: {type(e).__name__}: {str(e)[:120]}")
    return None, 0.0, False


def evaluate_aspect_synonym_candidates(db_conn, dsn: str, schema_name: str, qwen_api_url: str) -> dict:
    """Drain enqueued aspect-synonym misses (timed-out inline maps) and GROW the confident ones.

    Per-tenant (caller-bound search_path, NO public); confidence-gated; BOUNDED to the anchor's
    LIVE scalar attributes that are real rel_types (map to one or NONE — no invented links);
    subject-agnostic (no attr/aspect literal). Grown link is a rel_type_aliases row read
    deterministically by the query walk (no query-time model call). Fail-safe per row.
    Returns {"grown","unmapped","skipped","errors"}.
    """
    stats = {"grown": 0, "unmapped": 0, "skipped": 0, "brain_unavailable": 0, "errors": 0}
    if not _ASPECT_SYNONYM_GROWTH_ENABLED:
        return stats
    try:
        from src.ontology.canonical import record_alias as _record_alias
    except Exception as e:
        log.error(f"re_embedder.aspect_synonym_import_failed: {e}")
        return stats
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT id, sample_subject_id, sample_object FROM ontology_evaluations "
                " WHERE extraction_method = %s AND re_embedder_decision IS NULL "
                " ORDER BY last_seen_at DESC LIMIT 25",
                (_ASPECT_SYN_METHOD_EMB,),
            )
            rows = cur.fetchall()
        # READ BARRIER: _aspect_map_via_llm_async runs per candidate row below.
        release_read_transaction(
            db_conn, context=f"re_embedder.aspect_synonym.fetch schema={schema_name}")
    except Exception as e:
        log.error(f"re_embedder.aspect_synonym_fetch_failed schema={schema_name}: {str(e)[:160]}")
        return stats
    if not rows:
        return stats

    min_conf = _aspect_syn_min_conf_emb()
    for (row_id, anchor_uuid, aspect_word) in rows:
        # READ BARRIER (per iteration): this loop body blocks on the brain/Qdrant, and a
        # read left open by the PREVIOUS iteration would ride across it. A batch-level
        # barrier alone does not cover this — measured live: climb_state and the
        # taxonomy reads were each caught idle-in-transaction at 58-59s inside a loop.
        release_read_transaction(db_conn, context="re_embedder.evaluate_aspect_synonym_candidates.iteration")
        aspect_word = (aspect_word or "").strip().lower()
        if not aspect_word or not anchor_uuid:
            stats["skipped"] += 1
            continue
        try:
            with db_conn.cursor() as cur:
                # Already grown (inline path or a prior cycle)? → resolve the row, no LLM.
                cur.execute("SELECT 1 FROM rel_type_aliases WHERE alias = %s", (aspect_word,))
                if cur.fetchone():
                    cur.execute(
                        "UPDATE ontology_evaluations SET re_embedder_decision = 'aspect_grown',"
                        " decision_timestamp = now() WHERE id = %s", (row_id,))
                    db_conn.commit()
                    stats["grown"] += 1
                    continue
                # BOUNDED candidate set (leak bound, identical to the inline path): the
                # anchor's LIVE scalar attributes that are rel_types, UNIONed with the
                # relationship rels it actually HOLDS (facts ∪ staged, subject or object
                # side) — the relational twin. Grow maps to ONE of these or NONE; a grown
                # relationship alias is read back model-free by the query walk's
                # keyword→rel_type alias lane (routed to relationship_rels by tail_types).
                cur.execute(
                    "SELECT DISTINCT ea.attribute, rt.natural_language FROM entity_attributes ea "
                    "  JOIN rel_types rt ON rt.rel_type = ea.attribute "
                    " WHERE ea.entity_id = %s AND rt.tail_types::text ILIKE '%%SCALAR%%'",
                    (anchor_uuid,),
                )
                candidates = [(str(a).strip().lower(), (nl or "")) for a, nl in cur.fetchall() if a]
                cur.execute(
                    "SELECT DISTINCT f.rel_type, rt.natural_language "
                    "  FROM ( "
                    "    SELECT rel_type FROM facts "
                    "     WHERE (subject_id = %s OR object_id = %s) "
                    "       AND superseded_at IS NULL AND archived_at IS NULL "
                    "       AND deleted_at IS NULL "
                    "    UNION "
                    "    SELECT rel_type FROM staged_facts "
                    "     WHERE (subject_id = %s OR object_id = %s) "
                    "  ) f "
                    "  JOIN rel_types rt ON rt.rel_type = f.rel_type "
                    " WHERE rt.tail_types::text NOT ILIKE '%%SCALAR%%'",
                    (anchor_uuid, anchor_uuid, anchor_uuid, anchor_uuid),
                )
                candidates += [(str(r).strip().lower(), (nl or "")) for r, nl in cur.fetchall() if r]
            if not candidates:
                with db_conn.cursor() as cur:
                    cur.execute(
                        "UPDATE ontology_evaluations SET re_embedder_decision = 'no_candidates',"
                        " decision_timestamp = now() WHERE id = %s", (row_id,))
                db_conn.commit()
                stats["skipped"] += 1
                continue

            # READ BARRIER (immediately before the blocking call — the RE-ARM case). A barrier at
            # the top of the enclosing block is NOT enough: a per-row read helper opens a FRESH
            # transaction after it, and that read then rides across this hop. Measured live on the
            # deployed image — climb_classification_chains was killed twice this way (02:42:25 and
            # 02:45:06), its whole _ont_db subsystem chain failing 'connection already closed' four
            # seconds later.
            release_read_transaction(db_conn, context="re_embedder.evaluate_aspect_synonym_candidates.pre_blocking_call")
            attr, conf, called_ok = _aspect_map_via_llm_async(
                aspect_word, candidates, qwen_api_url)
            if not called_ok:
                # THE BRAIN NEVER ANSWERED. Leave re_embedder_decision NULL — that IS this
                # lane's retry mechanism (the drain SELECT above is `WHERE re_embedder_decision
                # IS NULL`), so writing ANY non-NULL value here, including a "retryable" one,
                # would remove the row from its own queue. Never write a decision this lane
                # cannot itself re-read.
                stats["brain_unavailable"] = stats.get("brain_unavailable", 0) + 1
                log.warning(
                    f"re_embedder.aspect_synonym_not_memoized schema={schema_name} "
                    f"aspect={aspect_word} note=a non-answer is not a verdict (RFC 9520 "
                    f"type-3); row left UNDECIDED so the next sweep retries")
                continue
            names = {c[0] for c in candidates}
            if attr and attr in names and conf >= min_conf:
                grew = _record_alias(alias=aspect_word, canonical=attr, requires_inversion=False,
                                     source="engine", dsn=dsn, schema=schema_name)
                with db_conn.cursor() as cur:
                    cur.execute(
                        "UPDATE ontology_evaluations SET re_embedder_decision = 'aspect_grown',"
                        " created_rel_type = %s, re_embedder_confidence = %s, decision_timestamp = now()"
                        " WHERE id = %s", (attr, conf, row_id))
                db_conn.commit()
                stats["grown"] += 1
                log.info(f"re_embedder.aspect_synonym_grown schema={schema_name} "
                         f"aspect={aspect_word} canonical={attr} conf={conf:.2f} fresh={grew}")
            else:
                with db_conn.cursor() as cur:
                    cur.execute(
                        "UPDATE ontology_evaluations SET re_embedder_decision = 'left_unmapped',"
                        " re_embedder_confidence = %s, decision_timestamp = now() WHERE id = %s",
                        (conf, row_id))
                db_conn.commit()
                stats["unmapped"] += 1
        except Exception as e:
            try:
                _rollback_and_reapply_search_path(db_conn, schema_name)
            except Exception:
                pass
            stats["errors"] += 1
            log.warning(f"re_embedder.aspect_synonym_row_failed schema={schema_name} "
                        f"aspect={aspect_word}: {str(e)[:140]}")
    return stats


# ════════════════════════════════════════════════════════════════════════════════════════════════
# ASPECT-SYNONYM GROWTH — ASYNC BACKSTOP for the query-side inline map-and-grow.
# The inline path (main.determine_path) grows tall→height on the FIRST miss when the tenant brain
# answers within the hard hot-path timeout; when it TIMES OUT it parks the miss in
# ontology_evaluations (extraction_method='aspect_synonym_miss'). This pass drains those so the
# NEXT query is deterministic regardless. Grown link = a rel_type_aliases row read by the EXISTING
# keyword→rel_type alias lane (query walk stays model-free). Per-tenant, confidence-gated, bounded
# to attributes the anchor actually holds, subject-agnostic. Companion to the rel synonym-convergence.
# ════════════════════════════════════════════════════════════════════════════════════════════════

_ASPECT_SYNONYM_GROWTH_ENABLED = _flag("ASPECT_SYNONYM_GROWTH", "true")
_ASPECT_SYN_METHOD_EMB = "aspect_synonym_miss"  # must match main._ASPECT_SYN_METHOD


def _aspect_syn_min_conf_emb() -> float:
    """Confidence floor for the async aspect-map GROW. Env-configurable; fail-safe → 0.75."""
    try:
        return float(os.getenv("ASPECT_SYNONYM_MIN_CONF", "0.75"))
    except (TypeError, ValueError):
        return 0.75


def _aspect_map_via_llm_async(aspect_word: str, candidates: list, qwen_api_url: str):
    """Background (retry-OK) brain call mirroring main._aspect_map_via_llm: does '<aspect_word>'
    refer to EXACTLY ONE of the things this user actually tracks (a scalar attribute OR a
    relationship the anchor holds), or NONE? Returns (canonical|None, confidence). Fail-safe →
    (None, 0.0). Bounded, low-token; existing LLM stack (no hardcoded timeout)."""
    _names = {c[0] for c in candidates}
    _lines = "\n".join(f'  - {a}: {(nl or a.replace("_", " "))}' for a, nl in candidates)
    prompt = (
        f"{_FAULTLINE_INTERNAL_PREFIX} You are a memory-aspect synonym resolver.\n\n"
        f'A user asked about the aspect word: "{aspect_word}".\n\n'
        "That word is NOT the stored name of anything this user tracks. Decide whether it "
        "refers to EXACTLY ONE of the user's ACTUAL memory aspects below — a scalar attribute "
        "or a relationship they hold (a lexical synonym — e.g. \"tall\" refers to a person's "
        "height, \"traveled\" refers to places they visited) — or to NONE of them.\n\n"
        f"The user's memory aspects:\n{_lines}\n\n"
        f'Pick AT MOST ONE name that "{aspect_word}" refers to. If it refers to none of '
        "them, answer null. Do NOT invent one that is not in the list.\n\n"
        "Respond with ONLY valid JSON (no markdown):\n"
        '{"attribute": "<one name exactly, or null>", "confidence": 0.0-1.0}'
    )
    try:
        result = call_llm_with_retry_sync(
            messages=[{"role": "user", "content": prompt}],
            model=LLMModels.get("ENRICHMENT"),
            user_id="re_embedder",
            timeout=LLMTimeouts.get("ENRICHMENT"),
            operation="ENRICHMENT",
            # The caller STAMPS the row on the outcome ('left_unmapped' is
            # terminal — the drain only selects NULL rows). A deferral must
            # therefore arrive as "did not ask", never (None, 0.0).
            raise_on_unavailable=True,
        )
        if isinstance(result, dict):
            attr = result.get("attribute")
            try:
                conf = float(result.get("confidence") or 0.0)
            except (TypeError, ValueError):
                conf = 0.0
            if attr and str(attr).strip().lower() in _names:
                return str(attr).strip().lower(), conf
    except LLMUnavailable:
        # Re-raise BEFORE the generic catch: a deferral is not "answered nothing".
        raise
    except Exception as e:
        log.debug(f"re_embedder.aspect_map_llm_failed aspect={aspect_word}: {type(e).__name__}: {str(e)[:120]}")
    return None, 0.0


def evaluate_aspect_synonym_candidates(db_conn, dsn: str, schema_name: str, qwen_api_url: str) -> dict:
    """Drain enqueued aspect-synonym misses (timed-out inline maps) and GROW the confident ones.

    Per-tenant (caller-bound search_path, NO public); confidence-gated; BOUNDED to the anchor's
    LIVE scalar attributes that are real rel_types (map to one or NONE — no invented links);
    subject-agnostic (no attr/aspect literal). Grown link is a rel_type_aliases row read
    deterministically by the query walk (no query-time model call). Fail-safe per row.
    Returns {"grown","unmapped","skipped","errors"}.
    """
    stats = {"grown": 0, "unmapped": 0, "skipped": 0, "errors": 0}
    if not _ASPECT_SYNONYM_GROWTH_ENABLED:
        return stats
    try:
        from src.ontology.canonical import record_alias as _record_alias
    except Exception as e:
        log.error(f"re_embedder.aspect_synonym_import_failed: {e}")
        return stats
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT id, sample_subject_id, sample_object FROM ontology_evaluations "
                " WHERE extraction_method = %s AND re_embedder_decision IS NULL "
                " ORDER BY last_seen_at DESC LIMIT 25",
                (_ASPECT_SYN_METHOD_EMB,),
            )
            rows = cur.fetchall()
    except Exception as e:
        log.error(f"re_embedder.aspect_synonym_fetch_failed schema={schema_name}: {str(e)[:160]}")
        return stats
    if not rows:
        return stats

    min_conf = _aspect_syn_min_conf_emb()
    for (row_id, anchor_uuid, aspect_word) in rows:
        aspect_word = (aspect_word or "").strip().lower()
        if not aspect_word or not anchor_uuid:
            stats["skipped"] += 1
            continue
        try:
            with db_conn.cursor() as cur:
                # Already grown (inline path or a prior cycle)? → resolve the row, no LLM.
                cur.execute("SELECT 1 FROM rel_type_aliases WHERE alias = %s", (aspect_word,))
                if cur.fetchone():
                    cur.execute(
                        "UPDATE ontology_evaluations SET re_embedder_decision = 'aspect_grown',"
                        " decision_timestamp = now() WHERE id = %s", (row_id,))
                    db_conn.commit()
                    stats["grown"] += 1
                    continue
                # BOUNDED candidate set (leak bound, identical to the inline path): the
                # anchor's LIVE scalar attributes that are rel_types, UNIONed with the
                # relationship rels it actually HOLDS (facts ∪ staged, subject or object
                # side) — the relational twin. Grow maps to ONE of these or NONE; a grown
                # relationship alias is read back model-free by the query walk's
                # keyword→rel_type alias lane (routed to relationship_rels by tail_types).
                cur.execute(
                    "SELECT DISTINCT ea.attribute, rt.natural_language FROM entity_attributes ea "
                    "  JOIN rel_types rt ON rt.rel_type = ea.attribute "
                    " WHERE ea.entity_id = %s AND rt.tail_types::text ILIKE '%%SCALAR%%'",
                    (anchor_uuid,),
                )
                candidates = [(str(a).strip().lower(), (nl or "")) for a, nl in cur.fetchall() if a]
                cur.execute(
                    "SELECT DISTINCT f.rel_type, rt.natural_language "
                    "  FROM ( "
                    "    SELECT rel_type FROM facts "
                    "     WHERE (subject_id = %s OR object_id = %s) "
                    "       AND superseded_at IS NULL AND archived_at IS NULL "
                    "       AND deleted_at IS NULL "
                    "    UNION "
                    "    SELECT rel_type FROM staged_facts "
                    "     WHERE (subject_id = %s OR object_id = %s) "
                    "  ) f "
                    "  JOIN rel_types rt ON rt.rel_type = f.rel_type "
                    " WHERE rt.tail_types::text NOT ILIKE '%%SCALAR%%'",
                    (anchor_uuid, anchor_uuid, anchor_uuid, anchor_uuid),
                )
                candidates += [(str(r).strip().lower(), (nl or "")) for r, nl in cur.fetchall() if r]
            if not candidates:
                with db_conn.cursor() as cur:
                    cur.execute(
                        "UPDATE ontology_evaluations SET re_embedder_decision = 'no_candidates',"
                        " decision_timestamp = now() WHERE id = %s", (row_id,))
                db_conn.commit()
                stats["skipped"] += 1
                continue

            try:
                attr, conf = _aspect_map_via_llm_async(aspect_word, candidates, qwen_api_url)
            except LLMUnavailable:
                # The call never happened (no rate capacity this pass): leave the
                # row's decision NULL so a later pass re-attempts it. Stamping
                # 'left_unmapped' here would terminally record a verdict the model
                # never gave.
                stats["skipped"] += 1
                continue
            names = {c[0] for c in candidates}
            if attr and attr in names and conf >= min_conf:
                grew = _record_alias(alias=aspect_word, canonical=attr, requires_inversion=False,
                                     source="engine", dsn=dsn, schema=schema_name)
                with db_conn.cursor() as cur:
                    cur.execute(
                        "UPDATE ontology_evaluations SET re_embedder_decision = 'aspect_grown',"
                        " created_rel_type = %s, re_embedder_confidence = %s, decision_timestamp = now()"
                        " WHERE id = %s", (attr, conf, row_id))
                db_conn.commit()
                stats["grown"] += 1
                log.info(f"re_embedder.aspect_synonym_grown schema={schema_name} "
                         f"aspect={aspect_word} canonical={attr} conf={conf:.2f} fresh={grew}")
            else:
                with db_conn.cursor() as cur:
                    cur.execute(
                        "UPDATE ontology_evaluations SET re_embedder_decision = 'left_unmapped',"
                        " re_embedder_confidence = %s, decision_timestamp = now() WHERE id = %s",
                        (conf, row_id))
                db_conn.commit()
                stats["unmapped"] += 1
        except Exception as e:
            try:
                _rollback_and_reapply_search_path(db_conn, schema_name)
            except Exception:
                pass
            stats["errors"] += 1
            log.warning(f"re_embedder.aspect_synonym_row_failed schema={schema_name} "
                        f"aspect={aspect_word}: {str(e)[:140]}")
    return stats


def _type_set(arr) -> set:
    """Lowercased set of a Postgres TEXT[] type column (None/empty → empty set)."""
    if not arr:
        return set()
    return {str(t).strip().lower() for t in arr if t and str(t).strip()}


def _side_compatible(observed: set, allowed: set) -> bool:
    """Is the OBSERVED entity-type set on one slot compatible with a candidate seed's ALLOWED types?
      • allowed contains 'any'  → unconstrained side, auto-pass (does NOT validate anything).
      • else                    → observed must be NON-EMPTY and EVERY observed type must be allowed.
    Fail-closed by construction: an empty/unknown observed set on a CONSTRAINED side returns False."""
    if "any" in allowed:
        return True
    if not observed:
        return False
    return observed.issubset(allowed)


def _seed_is_discriminating(head_allowed: set, tail_allowed: set) -> bool:
    """A seed is a valid convergence TARGET only if at least one slot carries a CONCRETE type
    constraint (a type other than the 'any' wildcard). A fully-unconstrained ('ANY','ANY') seed —
    e.g. the loose ``related_to`` catch-all — provides NO type rail to validate against, so converging
    onto it would launder a specific lifted verb into a generic link. Excluding it is the type rail
    doing its job WITHOUT a literal rel-name check (subject-agnostic)."""
    concrete_head = head_allowed - {"any"}
    concrete_tail = tail_allowed - {"any"}
    return bool(concrete_head or concrete_tail)


def _observed_entity_types(db_conn, rel_type: str, which: str) -> set:
    """Resolve the DISTINCT entity types actually observed on the LIVE facts of ``rel_type`` for the
    subject (which='subject') or object (which='object') slot, per-tenant (search_path bound by the
    caller). Joins facts+staged_facts → entities.entity_type; drops NULL/'unknown' (unresolved). A
    scalar object_id (a literal value, never registered as an entity) simply does not join, so a
    scalar-tail rel yields an EMPTY object set → fail-closed (never converged). Fail-safe → empty set."""
    col = "subject_id" if which == "subject" else "object_id"
    types: set = set()
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT e.entity_type"
                f"  FROM facts f JOIN entities e ON e.id = f.{col}"
                f" WHERE f.rel_type = %s AND f.superseded_at IS NULL AND f.archived_at IS NULL"
                f" UNION"
                f" SELECT DISTINCT e.entity_type"
                f"  FROM staged_facts sf JOIN entities e ON e.id = sf.{col}"
                f" WHERE sf.rel_type = %s",
                (rel_type, rel_type),
            )
            for (et,) in cur.fetchall():
                _et = (et or "").strip().lower()
                if _et and _et != "unknown":
                    types.add(_et)
    except Exception as e:
        log.debug(f"re_embedder.synonym_conv_observed_types_failed rel={rel_type} slot={which}: {str(e)[:120]}")
        return set()
    return types


# ════════════════════════════════════════════════════════════════════════════════════════════════
# A NON-ANSWER IS NOT A VERDICT (memo honesty)
# ════════════════════════════════════════════════════════════════════════════════════════════════
# Every lane below CACHES A DECISION derived from a brain answer. The LLM stack returns the SAME
# python shape for three different things (llm_calls.call_llm_with_retry_sync):
#
#   (a) the brain ANSWERED               -> the parsed JSON object, carrying the verdict key
#   (b) the brain answered NOTHING       -> {}          (empty content / unparseable response)
#   (c) the brain was NEVER REACHED      -> {}          (no endpoint answered), or an
#                                          {"error": ...} dict (circuit_breaker_open, and
#                                          — measured live — context_overrun_preflight, which
#                                          refuses BEFORE the wire and is NOT covered by
#                                          raise_on_unavailable: llm_calls.py returns `_ctx_block`
#                                          straight out of _context_preflight).
#
# (b) and (c) are indistinguishable from (a)-answering-"null" once you look only at the verdict
# value, which is exactly how a transient outage became a PERMANENT exclusion: measured on
# production 2026-08-27, `llm_call.context_overrun_preflight operation=ENRICHMENT window=448`
# fired 198 times in 72h, and 512 `left_novel` memos across 9 of 22 tenants carry
# `ans='' conf=0.00` — the fingerprint of a call that never happened.
#
# THE STANDARD IS EXPLICIT ABOUT THIS SPLIT. RFC 9520, "Negative Caching of DNS Resolution
# Failures" (Kristoff, Wessels, Wallström, Dec 2023) opens by separating exactly these three:
# "(1) a response containing the requested data, (2) a response indicating the requested data does
# not exist, or (3) a non-response due to a resolution failure in which the resolver does not
# receive any useful information regarding the data's existence. This document concerns itself
# only with the third type." It then rules that "NXDOMAIN and NOERROR/NODATA responses are not
# conditions for resolution failure ... the server is providing a useful response", and that a
# FAILURE may be cached but "MUST NOT be cached for longer than 5 minutes". Our defect is the
# prohibited combination: caching a type-3 non-response AS a type-2 negative answer, forever.
#
# THE PREDICATE: a dict that CARRIES THE VERDICT KEY is an answer. Everything else — {}, an
# {"error": ...} envelope, a non-dict, a raised LLMUnavailable — is a non-answer. This is
# key-PRESENCE, not truthiness: {"equivalent_to": None, "confidence": 0.95} is a considered
# "not equivalent" and MUST still be memoized. Cheap, local, and covers every shape above,
# including the preflight refusal that raise_on_unavailable cannot express.
_ONTOLOGY_MEMO_REQUIRES_ANSWER = _flag("ONTOLOGY_MEMO_REQUIRES_ANSWER", "true")


def _brain_answered(result) -> bool:
    """False when ``result`` is one of the three shapes ``llm_calls`` returns for a call that
    produced NO answer, and True otherwise.

    DELIBERATELY NARROW — it enumerates the shapes, it does not judge the content:
      * not a dict            — nothing came back at all;
      * an EMPTY dict         — empty_llm_content /
                                unparseable_llm_response;
      * an ``{"error": ...}`` envelope — circuit_breaker_open, and context_overrun_preflight,
                                which refuses BEFORE the wire and therefore cannot be expressed
                                by ``raise_on_unavailable``.
    Every prompt in this module asks for a schema with no ``error`` key, so this cannot
    mis-fire on a real answer. It is narrow ON PURPOSE: a WIDER predicate (e.g. "the verdict
    key must be present") would also reject a malformed-but-real answer, and since a non-answer
    is retried rather than memoized, that would mean re-asking a broken model forever.

    Flag ``ONTOLOGY_MEMO_REQUIRES_ANSWER=false`` restores the byte-for-byte legacy behaviour
    (every returned shape treated as an answer). Default TRUE = the SAFE direction: refuse to
    cache a verdict we have no evidence for. Fail-safe on any fault -> True (treat as answered),
    because this predicate must never invent an outage that did not happen.
    """
    if not _ONTOLOGY_MEMO_REQUIRES_ANSWER:
        return True
    try:
        if not isinstance(result, dict):
            return False
        if not result:
            return False
        return "error" not in result
    except Exception:  # noqa: BLE001 — a guard must never break its host
        return True


def _llm_propose_equivalence(
    novel_rel: str,
    novel_nl: str,
    subj_types: set,
    obj_types: set,
    candidates: dict,
    qwen_api_url: str,
) -> dict:
    """GATE 2 — the LLM PROPOSES (never decides) whether ``novel_rel`` is DEFINITIONALLY THE SAME
    relation as ONE of the already type-compatible ``candidates`` (a strict bidirectional synonym —
    "X novel Y" is true IFF "X candidate Y" is true), NOT merely related/narrower/broader. Bounded,
    low-token, existing LLM stack (LLMTimeouts — no hardcoded timeout). Returns the parsed dict or {}.
    Returns ``(proposal, answered)``. ``answered`` is False when the brain was never reached or
    returned nothing — see ``_brain_answered``; the caller MUST NOT memoize a verdict then.
    Fail-safe: any error / no valid JSON → ({}, False) (caller leaves the rel novel AND RETRIES)."""
    _cand_lines = "\n".join(
        f'  - {pk}: {(meta.get("nl") or pk.replace("_", " "))}'
        + (" (symmetric)" if meta.get("is_symmetric") else "")
        for pk, meta in candidates.items()
    )
    _readable = (novel_rel or "").replace("_", " ")
    prompt = f"""{_FAULTLINE_INTERNAL_PREFIX} You are an ontology EQUIVALENCE checker.

A novel relation was observed in data:
  relation: "{novel_rel}"  (reads: "X {_readable} Y")
  {("meaning: " + novel_nl) if novel_nl else ""}
  subject entity types: {sorted(subj_types) or "unknown"}
  object entity types:  {sorted(obj_types) or "unknown"}

Candidate STANDARD relations (all already type-compatible):
{_cand_lines}

Pick AT MOST ONE candidate that is DEFINITIONALLY THE SAME relation as the novel one — i.e.
"X {_readable} Y" is TRUE if and only if "X <candidate> Y" is TRUE (a strict, bidirectional
synonym / redundancy, interchangeable in BOTH directions). Do NOT pick a candidate that is merely
RELATED, a SUB-TYPE, a SUPER-TYPE, a CONSEQUENCE, or only sometimes true. If unsure → answer null.

Respond with ONLY valid JSON (no markdown):
{{"equivalent_to": "<one candidate rel_type exactly, or null>", "confidence": 0.0-1.0, "reason": "<short>"}}"""
    try:
        result = call_llm_with_retry_sync(
            messages=[{"role": "user", "content": prompt}],
            model=LLMModels.get("ENRICHMENT"),
            # PER-TENANT: Gate 1 resolved the observed head/tail types from THIS tenant's live
            # facts, so both the novel rel and the candidate set are their data.
            user_id=_reembedder_llm_user_id(),
            timeout=LLMTimeouts.get("ENRICHMENT"),
            operation="ENRICHMENT",
            # A deferral ("no capacity this pass") is NOT an answer. The caller
            # MEMOIZES non-convergence into ontology_evaluations — recording a
            # verdict from a call that never happened would permanently end
            # convergence for this rel on a merely-paced day. Raise instead.
            raise_on_unavailable=True,
        )
        if _brain_answered(result):
            return result, True
        log.warning(f"re_embedder.synonym_conv_brain_nonanswer rel={novel_rel} "
                    f"shape={type(result).__name__} "
                    f"error={(result or {}).get('error') if isinstance(result, dict) else None} "
                    f"note=NOT memoized; retried on a later sweep")
        return (result if isinstance(result, dict) else {}), False
    except LLMUnavailable as e:
        log.warning(f"re_embedder.synonym_conv_brain_unavailable rel={novel_rel} "
                    f"reason={e.reason} operation={e.operation} "
                    f"note=no verdict cached, no attempt burned; retried on a later sweep")
        # Re-raise BEFORE the generic catch below — a deferral must reach the caller as
        # "did not ask" (it refunds the budget it never spent), never as a shape.
        raise
    except Exception as e:
        log.debug(f"re_embedder.synonym_conv_llm_failed rel={novel_rel}: {type(e).__name__}: {str(e)[:120]}")
    return {}, False


def converge_lifted_synonyms(db_conn, dsn: str, schema_name: str, qwen_api_url: str = None) -> dict:
    """DETERMINISTIC-GATED synonym convergence for LIFTED novel rels the morphology-fold MISSED.

    Companion to ``drain_pending_placement_by_morphology``. The morphology drain folds a novel rel onto
    a seed when their NORMALIZED FORMS match (``live_in`` → ``lives_in``). A novel rel that is a
    SEMANTIC synonym of a seeded rel but NOT a morphological one (``marry`` → ``spouse``, ``adopt`` →
    ``has_pet``) falls through it — this pass catches that class, behind three FAIL-CLOSED gates run
    IN ORDER (any gate fails → LEAVE IT NOVEL, never converge):

      GATE 1 — DETERMINISTIC TYPE-CONSTRAINT MATCH (no LLM, runs first):
        Resolve the novel rel's OBSERVED subject/object entity types from its LIVE facts, then keep
        only SEEDED candidate rels whose head_types/tail_types are (a) DISCRIMINATING (not the ANY/ANY
        catch-all) and (b) compatible with the observed types. ``affect(cve, product)`` → ``located_in``
        dies HERE (product is not a Location) with NO LLM. Unresolvable types → fail-closed (empty
        candidate set → leave novel).

      GATE 2 — EQUIVALENCE, not similarity (LLM PROPOSES ONLY, tightly bounded):
        Only over the small type-compatible set, one bounded LLM call proposes STRICT bidirectional
        equivalence; the answer must resolve BY IDENTITY (canonical resolver) to a candidate PK and
        clear a confidence floor. "Related but distinct"/null/ambiguous → no convergence.

      GATE 3 — STRUCTURAL SANITY before the (non-destructive) write:
        Hierarchy parity (graph↔graph, hierarchy↔hierarchy) and symmetry compatibility (never drop a
        symmetric novel rel's reverse edge onto an asymmetric seed). On pass, mirror the morphology
        drain EXACTLY: adopt the seed's category, join the seed's taxonomies, then
        ``record_alias(novel → seed, source='engine')`` — a REVERSIBLE alias row (the same
        ``rel_type_aliases`` synonym table morphology uses; existing facts convert at read time via
        ``_get_canonical_rel_type``, which is alias-first, so they read back AS the seed). Stored facts
        are NOT rewritten/destroyed.

    A non-converging rel that REACHED the LLM gate is memoized (ontology_evaluations,
    method='synonym_convergence', decision='left_novel') so it is not re-sent to the LLM each cycle.
    Bias: FALSE-NEGATIVE (leave novel) is always preferred over a FALSE-POSITIVE tenant-wide corruption.

    Per-tenant (caller bound search_path, NO public); deterministic gates own the decision; fail-safe
    per rel (savepoint) and per pass. Returns {"converged": int, "scanned": int, "left_novel": int,
    "errors": int}.
    """
    stats = {"converged": 0, "scanned": 0, "left_novel": 0, "brain_unavailable": 0, "errors": 0}
    if not _ENGINE_SYNONYM_CONVERGENCE:
        return stats

    try:
        from src.ontology.canonical import (
            resolve_seeded_by_morphology as _resolve_seeded_morph,
            resolve_canonical as _resolve_canonical,
            record_alias as _record_alias,
            reset_caches as _reset_canon_caches,
            normalize_rel as _normalize_rel,
            _SEEDED_SOURCES,
        )
    except Exception as e:
        log.error(f"re_embedder.synonym_conv_import_failed: {e}")
        return stats

    # 0. Candidate NOVEL rels: NOT seeded, and either quarantined pending OR engine-generated.
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT rel_type, is_symmetric, is_hierarchy_rel, natural_language, category"
                "  FROM rel_types"
                " WHERE lower(COALESCE(source, '')) NOT IN %s"
                "   AND (category = %s OR engine_generated = true)",
                (tuple(_SEEDED_SOURCES), _CATEGORY_PENDING_RE),
            )
            novel_rows = cur.fetchall()
    except Exception as e:
        log.error(f"re_embedder.synonym_conv_fetch_failed schema={schema_name}: {str(e)[:160]}")
        return stats
    finally:
        # BELT-AND-BRACES read barrier — see evaluate_ontology_candidates. Third of the three
        # confirmed leak sites: `if not novel_rows: return stats` below.
        release_read_transaction(
            db_conn, context=f"re_embedder.converge_lifted_synonyms.fetch schema={schema_name}")

    if not novel_rows:
        return stats

    # SEEDED candidate universe (with metadata) — fetched once per pass.
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT rel_type, head_types, tail_types, is_symmetric, is_hierarchy_rel,"
                "       inverse_rel_type, natural_language, category"
                "  FROM rel_types WHERE lower(COALESCE(source, '')) IN %s",
                (tuple(_SEEDED_SOURCES),),
            )
            seeded_rows = cur.fetchall()
    except Exception as e:
        log.error(f"re_embedder.synonym_conv_seed_fetch_failed schema={schema_name}: {str(e)[:160]}")
        return stats
    finally:
        # Same class as the novel_rows fetch above: this `return stats` exits with the
        # transaction INERROR, still holding every lock the SELECT took.
        release_read_transaction(
            db_conn, context=f"re_embedder.converge_lifted_synonyms.seed_fetch schema={schema_name}")

    seeded = {}
    for (pk, ht, tt, sym, hier, inv, nl, cat) in seeded_rows:
        _pk = (pk or "").strip().lower()
        if not _pk:
            continue
        seeded[_pk] = {
            "head": _type_set(ht),
            "tail": _type_set(tt),
            "is_symmetric": bool(sym),
            "is_hierarchy": bool(hier),
            "inverse": (inv or "").strip().lower() or None,
            "nl": nl or "",
            "category": (cat or "").strip().lower() or None,
        }

    llm_budget = _synonym_conv_batch()
    min_conf = _synonym_conv_min_conf()

    for (rel, novel_sym, novel_hier, novel_nl, novel_cat) in novel_rows:
        _rel = (rel or "").strip().lower()
        if not _rel:
            continue
        stats["scanned"] += 1
        try:
            # Skip if the morphology-fold OWNS it (the drain will/does converge it).
            if _resolve_seeded_morph(_rel, dsn, schema_name):
                continue
            # Skip if it is ALREADY aliased (converged on a prior cycle) OR memoized as left-novel.
            with db_conn.cursor() as cur:
                cur.execute("SELECT 1 FROM rel_type_aliases WHERE alias = %s", (_rel,))
                if cur.fetchone():
                    continue
                cur.execute(
                    "SELECT 1 FROM ontology_evaluations"
                    " WHERE candidate_rel_type = %s AND extraction_method = %s"
                    "   AND re_embedder_decision = 'left_novel'",
                    (_rel, _SYNONYM_CONV_METHOD),
                )
                if cur.fetchone():
                    continue

            # ── GATE 1 (deterministic) — observed types + type-compatible discriminating seeds ──
            subj_types = _observed_entity_types(db_conn, _rel, "subject")
            obj_types = _observed_entity_types(db_conn, _rel, "object")
            if not subj_types or not obj_types:
                # No resolvable observed types on one/both slots → fail-closed, leave novel.
                continue

            candidates = {}
            for pk, meta in seeded.items():
                if pk == _rel:
                    continue
                if bool(novel_hier) != meta["is_hierarchy"]:
                    continue  # never collapse a graph rel onto a classification rel (or vice-versa)
                if not _seed_is_discriminating(meta["head"], meta["tail"]):
                    continue  # ANY/ANY catch-all (e.g. related_to) — no type rail → excluded
                if not _side_compatible(subj_types, meta["head"]):
                    continue
                if not _side_compatible(obj_types, meta["tail"]):
                    continue
                candidates[pk] = meta

            if not candidates:
                # Gate 1 killed every candidate deterministically (the affect→located_in class).
                log.info(
                    f"re_embedder.synonym_conv_type_reject schema={schema_name} rel={_rel} "
                    f"subj={sorted(subj_types)} obj={sorted(obj_types)} candidates=0 (Gate1)"
                )
                continue

            if llm_budget <= 0:
                continue  # per-cycle LLM bound reached — retry next cycle
            llm_budget -= 1

            # ── GATE 2 (LLM proposes, deterministic accept) ──
            # PER-ITEM READ BARRIER, immediately before the blocking call. Gate 1 just read
            # the rel's observed entity types (_observed_entity_types → facts/entities), and
            # the alias/memo probes above read rel_type_aliases + ontology_evaluations — all
            # on the caller's long-lived sweep connection, all still INTRANS. Without this the
            # LLM call is made holding those locks for its entire duration; a wedged endpoint
            # makes that unbounded. Pure reads → rolled back; a pending write → log_crit, never
            # silently discarded.
            release_read_transaction(
                db_conn,
                context=f"re_embedder.converge_lifted_synonyms.llm_gate2 schema={schema_name} rel={_rel}")
            try:
                proposal, _answered = _llm_propose_equivalence(
                    _rel, novel_nl or "", subj_types, obj_types, candidates, qwen_api_url
                )
            except LLMUnavailable:
                # The call never happened (no rate capacity / breaker open this pass):
                # refund the budget it never spent and leave the rel UNMEMOIZED — "asked
                # and did not converge" is the only thing the memo may record.
                llm_budget += 1
                continue
            if not _answered:
                # THE BRAIN NEVER ANSWERED — there is no verdict to cache. Record a
                # decision the skip-probe above does NOT honour (it matches
                # `re_embedder_decision = 'left_novel'` exactly), so this rel is RE-OFFERED
                # on the next sweep instead of being excluded forever. The row is written on
                # the SAME (candidate_rel_type, sample_subject_id, sample_object) key, so a
                # later real verdict simply overwrites it via the ON CONFLICT below.
                stats["brain_unavailable"] = stats.get("brain_unavailable", 0) + 1
                try:
                    with db_conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO ontology_evaluations"
                            "  (candidate_rel_type, extraction_method, sample_subject_id, sample_object,"
                            "   occurrence_count, last_seen_at, re_embedder_decision, decision_reason,"
                            "   decision_timestamp)"
                            " VALUES (%s, %s, %s, %s, 1, now(), %s, %s, now())"
                            " ON CONFLICT (candidate_rel_type, sample_subject_id, sample_object)"
                            " DO UPDATE SET last_seen_at = now(),"
                            "   re_embedder_decision = %s,"
                            "   decision_reason = EXCLUDED.decision_reason",
                            (_rel, _SYNONYM_CONV_METHOD, _SYNONYM_CONV_MEMO_SUBJ,
                             _SYNONYM_CONV_MEMO_OBJ, _MEMO_BRAIN_UNAVAILABLE,
                             ("brain did not answer (never reached / empty / error envelope) — "
                              "NOT a verdict; retried on a later sweep")[:500],
                             _MEMO_BRAIN_UNAVAILABLE),
                        )
                    db_conn.commit()
                except Exception:
                    try:
                        db_conn.rollback()
                        with db_conn.cursor() as _spc:
                            _spc.execute(f"SET search_path TO {schema_name}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema from UUID-derived source with validation
                    except Exception:
                        pass
                log.warning(
                    f"re_embedder.synonym_conv_not_memoized schema={schema_name} rel={_rel} "
                    f"decision={_MEMO_BRAIN_UNAVAILABLE} "
                    f"note=a non-answer is not a verdict (RFC 9520 type-3); door stays OPEN"
                )
                continue
            _ans = (proposal.get("equivalent_to") if isinstance(proposal, dict) else None)
            _ans = (_ans or "").strip().lower() if isinstance(_ans, str) else ""
            try:
                _conf = float(proposal.get("confidence", 0.0)) if isinstance(proposal, dict) else 0.0
            except (TypeError, ValueError):
                _conf = 0.0

            # Resolve the proposal BY IDENTITY to a candidate PK (reuse the canonical resolver).
            target = None
            if _ans and _ans not in ("null", "none"):
                if _ans in candidates:
                    target = _ans
                else:
                    _n = _normalize_rel(_ans)
                    if _n in candidates:
                        target = _n
                    else:
                        try:
                            _rc = (_resolve_canonical(_ans, dsn, schema_name) or {}).get("canonical")
                            if _rc in candidates:
                                target = _rc
                        except Exception:
                            target = None

            if not target or _conf < min_conf:
                # Reached the LLM and did NOT converge → memo so we don't re-LLM every cycle.
                stats["left_novel"] += 1
                _reason = (
                    f"no strict-equivalence match (ans={_ans!r} conf={_conf:.2f} "
                    f"floor={min_conf:.2f} candidates={sorted(candidates)})"
                )
                try:
                    with db_conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO ontology_evaluations"
                            "  (candidate_rel_type, extraction_method, sample_subject_id, sample_object,"
                            "   occurrence_count, last_seen_at, re_embedder_decision, decision_reason,"
                            "   decision_timestamp)"
                            " VALUES (%s, %s, %s, %s, 1, now(), 'left_novel', %s, now())"
                            " ON CONFLICT (candidate_rel_type, sample_subject_id, sample_object)"
                            " DO UPDATE SET last_seen_at = now(),"
                            "   re_embedder_decision = 'left_novel',"
                            "   decision_reason = EXCLUDED.decision_reason",
                            (_rel, _SYNONYM_CONV_METHOD, _SYNONYM_CONV_MEMO_SUBJ,
                             _SYNONYM_CONV_MEMO_OBJ, _reason[:500]),
                        )
                    db_conn.commit()
                except Exception:
                    try:
                        db_conn.rollback()
                        with db_conn.cursor() as _spc:
                            _spc.execute(f"SET search_path TO {schema_name}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema from UUID-derived source with validation
                    except Exception:
                        pass
                log.info(
                    f"re_embedder.synonym_conv_left_novel schema={schema_name} rel={_rel} "
                    f"ans={_ans!r} conf={_conf:.2f} floor={min_conf:.2f}"
                )
                continue

            tgt_meta = candidates[target]

            # ── GATE 3 (structural sanity) ──
            if bool(novel_hier) != tgt_meta["is_hierarchy"]:
                stats["left_novel"] += 1
                continue
            # Never collapse a SYMMETRIC novel usage onto an ASYMMETRIC seed (would silently drop the
            # reverse edge). Adopting a seed's OWN symmetry (asymmetric novel → symmetric seed) is fine
            # — the alias inherits the seed's metadata at read time.
            if bool(novel_sym) and not tgt_meta["is_symmetric"]:
                stats["left_novel"] += 1
                log.info(
                    f"re_embedder.synonym_conv_symmetry_reject schema={schema_name} "
                    f"rel={_rel} target={target} (symmetric novel → asymmetric seed)"
                )
                continue

            # ── WRITE (mirror the morphology drain: category adopt → taxonomy join → alias) ──
            with db_conn.cursor() as cur:
                cur.execute("SAVEPOINT sp_synonym_conv")
                try:
                    if tgt_meta["category"] and tgt_meta["category"] != _CATEGORY_PENDING_RE:
                        cur.execute(
                            "UPDATE rel_types SET category = %s"
                            " WHERE rel_type = %s AND category = %s",
                            (tgt_meta["category"], _rel, _CATEGORY_PENDING_RE),
                        )
                    cur.execute(
                        "UPDATE entity_taxonomies"
                        " SET rel_types_defining_group ="
                        "     array_append(rel_types_defining_group, %s)"
                        " WHERE (rel_types_defining_group @> ARRAY[%s]::TEXT[])"
                        "   AND NOT (rel_types_defining_group @> ARRAY[%s]::TEXT[])",
                        (_rel, target, _rel),
                    )
                    cur.execute("RELEASE SAVEPOINT sp_synonym_conv")
                except Exception as _inner:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_synonym_conv")
                    stats["errors"] += 1
                    log.error(
                        f"re_embedder.synonym_conv_write_failed rel={_rel} target={target} "
                        f"schema={schema_name}: {str(_inner)[:160]}"
                    )
                    continue

            # record_alias commits on its own connection (canonical.py); same-direction equivalence
            # (Gate-2 slot order) → requires_inversion=False. Non-destructive + reversible.
            try:
                _record_alias(_rel, target, False, "engine", dsn, schema_name)
            except Exception as _ae:
                log.warning(
                    f"re_embedder.synonym_conv_alias_failed rel={_rel} target={target}: {str(_ae)[:120]}"
                )

            db_conn.commit()
            stats["converged"] += 1
            log.info(
                f"re_embedder.synonym_converged schema={schema_name} rel={_rel} -> {target} "
                f"conf={_conf:.2f} subj={sorted(subj_types)} obj={sorted(obj_types)} "
                f"symmetric={tgt_meta['is_symmetric']} category={tgt_meta['category']}"
            )
        except Exception as e:
            try:
                db_conn.rollback()
                with db_conn.cursor() as _spc:
                    _spc.execute(f"SET search_path TO {schema_name}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema from UUID-derived source with validation
            except Exception:
                pass
            stats["errors"] += 1
            log.error(
                f"re_embedder.synonym_conv_error rel={_rel} schema={schema_name}: {str(e)[:160]}"
            )

    # Invalidate the in-process canonical/alias cache so the new alias rows are visible to the next
    # in-flow resolve without a restart. The caller marks the schema changed so the BACKEND overlay
    # (separate process) is refreshed via /internal/refresh-intent-pattern-caches (mirrors the drain).
    if stats["converged"] > 0:
        try:
            _reset_canon_caches(schema_name)
        except Exception:
            pass

    return stats


def decay_ontology_candidates(db_conn, user_id: str = None) -> dict:
    """
    Reinforce-or-decay sweep for NOVEL rel_type candidates in ontology_evaluations.

    Mirrors expire_staged_facts() (the Class C score-decay model) but keyed on the
    candidate ledger's own counters: `occurrence_count` + `last_seen_at`. Uses the
    partial index idx_ontology_eval_decision (re_embedder_decision, last_seen_at)
    WHERE re_embedder_decision IS NULL — the orphaned aging index this sweep exists for.

    State machine (same 30-day window as the staged-fact decay, literal interval — the
    fact model uses a literal `interval '30 days'`, so we mirror it; no env override):

      Decay (window elapsed, score remaining):
        re_embedder_decision IS NULL
        AND last_seen_at   <= now() - interval '30 days'
        AND occurrence_count > 0
          → occurrence_count -= 1, last_seen_at = now()   (buys another 30-day window)

      Forget (window elapsed, score at zero — a one-off never reinforced):
        re_embedder_decision IS NULL
        AND last_seen_at   <= now() - interval '30 days'
        AND occurrence_count <= 0
          → DELETE  (forgotten; no Qdrant point for candidates — DB delete only)

    Reinforcement is automatic and lives in the ingest path: a re-sighting bumps
    occurrence_count and sets last_seen_at = now() (ON CONFLICT), pushing the window
    forward so a recurring candidate never decays.

    This is internal vocabulary hygiene only — candidates are NOT recalled to the user
    (the underlying fact lives in staged_facts and decays on its own track). Decayed/
    forgotten candidates are NEVER frozen as terminal 'rejected'; an undecided candidate
    that keeps recurring can still reach the aggregate-frequency approval threshold.

    Per-user schema context: search_path is set by the caller (loop sets it before this
    call). Per-user error isolation: one tenant's failure must not crash the sweep.
    Returns {"decayed": int, "forgotten": int}.
    """
    stats = {"decayed": 0, "forgotten": 0}
    try:
        # Step 1: Decay — undecided candidates past their window with remaining score.
        with db_conn.cursor() as cur:
            cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                """
                UPDATE ontology_evaluations
                SET occurrence_count = occurrence_count - 1,
                    last_seen_at     = now()
                WHERE re_embedder_decision IS NULL
                  AND last_seen_at <= now() - interval '30 days'
                  AND occurrence_count > 0
                """
            )
            stats["decayed"] = cur.rowcount
        db_conn.commit()
        if stats["decayed"]:
            log.info(
                f"re_embedder.ontology_candidates_decayed "
                f"count={stats['decayed']} user_id={user_id}"
            )

        # Step 2: Forget — undecided candidates at score zero AND past the window.
        # Fresh rows start at occurrence_count=1 with last_seen_at=now(), so a brand-new
        # candidate is never eligible here; only one decremented to <= 0 a full window
        # ago (i.e. never reinforced) is forgotten. No Qdrant point — DB delete only.
        with db_conn.cursor() as cur:
            cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                """
                DELETE FROM ontology_evaluations
                WHERE re_embedder_decision IS NULL
                  AND last_seen_at <= now() - interval '30 days'
                  AND occurrence_count <= 0
                """
            )
            stats["forgotten"] = cur.rowcount
        db_conn.commit()
        if stats["forgotten"]:
            log.info(
                f"re_embedder.ontology_candidates_forgotten "
                f"count={stats['forgotten']} reason=score_zero user_id={user_id}"
            )

    except Exception as e:
        try:
            db_conn.rollback()
        except Exception:
            pass
        log.error(f"re_embedder.ontology_candidate_decay_error user_id={user_id}: {e}")

    return stats


# Freq gate for CARVED cue-class growth — mirrors the rel_type / correction-signal threshold (≥3).
# ── ENGINE STRUCTURE IS USABLE ON DERIVATION — NOT AT >=3 (owner ruling, 2026-08-27) ────────────
# "those aspects that are ours to control should not require validation to become useable, they
#  should be hinged on the growth. The only aspect requiring 'validation' should be the user's
#  actual memory ie. inferred class C, otherwise everything is provided and tabled straight out."
# and: "We should rely on the actual growth to improve the L4 and useability, not wait for x
#  confirmations. The ability for users to correct that shape directly is the metric, ie. we rely on
#  surfacing to the user to be able to correct if it's wrong."
#
# A cue class is a SHELF (engine scaffolding — ours). A fact about the user is a MEMORY. Shelves get
# built on sight; memories get checked. A frequency gate here does NOT filter noise — it HIDES the
# growth from the only thing that can judge it, so three occurrences of a wrong shelf is still a
# wrong shelf, just later, and uncorrectable in the meantime.
#
# WHY THIS ONE IS SAFE TO UNGATE (the others in this file are NOT — see their own notes):
#   * It writes only <tenant>.linguistic_cues, per-tenant, never public, never another tenant.
#   * The proposal is already firewalled by the PARSE at the proposal site, not by frequency (e.g.
#     the cessative rail proposes only the observed gerundive-complement shape).
#   * It is REVERSIBLE without touching stored memory: `UPDATE linguistic_cues SET is_active=false`
#     retires a member and the 5s overlay TTL applies it on the next turn; no stored fact is
#     rewritten (the three-level kill switch is documented at linguistics.py:6929).
#   * It is NOT the `staged_facts.confirmed_count >= 3` promotion — that is INFERRED USER MEMORY and
#     is deliberately untouched.
# ROLLBACK LEVER: CUE_GROWTH_THRESHOLD=3 restores the legacy freq gate exactly.
LINGUISTIC_CUE_GROWTH_THRESHOLD = max(1, int(os.environ.get("CUE_GROWTH_THRESHOLD", "1") or 1))


def grow_linguistic_cue_candidates(db_conn, schema_name: str = None) -> dict:
    """Grow CARVED cue classes (social_role / problem_noun) PER-TENANT from observed, freq-gated
    candidates.

    ⚠️ AMENDED 2026-08-27. This used to read "social_role and problem_noun are DOMAIN-FLAVORED
    classes that are no longer seeded". HALF OF THAT IS OVERRULED: `social_role`
    (colleague/coworker/teammate/roommate) is CLOSED-CLASS ENGLISH STRUCTURE, the same category as
    the already-seeded kinship_noun, and migration 272 SEEDS it from a WordNet derivation
    (the internal design record). Growth still ADDS a tenant's own roles on top —
    seed the grammar, grow the subject. `problem_noun` remains genuinely domain-flavored and unseeded.
    Classes are GROWN from the OBSERVED construction. The ingest/harvest seam records a
    candidate into ``ontology_evaluations`` (extraction_method='linguistic_cue_candidate',
    candidate_object_type=<category>, sample_object=<cue lemma>) and bumps occurrence_count on each
    re-sighting (ON CONFLICT). This sweep reads candidates at or above LINGUISTIC_CUE_GROWTH_THRESHOLD (default 1 — usable on
    derivation, per the engine-structure ruling) and writes
    them into ``<tenant>.linguistic_cues`` so the overlay resolves them on the next turn — then the
    consumer routes the construction correctly instead of degrading.

    DETERMINISTIC + per-tenant + fail-safe: search_path is set by the caller's per-tenant connection;
    the INSERT is into the bound tenant schema (NO public — growth never pollutes the seed template).
    Convergence-by-identity (the UNIQUE (cue, category) ON CONFLICT), NO cosine/fuzzy. The grown
    rel_type for social_role is the GENERIC person tie ``knows`` (the specific friend_of tie is not
    auto-distinguished — user-correctable). problem_noun is a SET (membership in ``cue``); its
    description is a human note. Decided candidates are marked re_embedder_decision='cue_grown' so they
    never re-grow; un-reinforced candidates age out via the shared decay sweep.

    Returns {"grown": int, "errors": int}. thin_type growth is intentionally NOT here (deferred —
    its only candidate signal is circular with GLiNER2's live typing; see the overlay carve comment)."""
    stats = {"grown": 0, "errors": 0}
    # Per-category grown-row shape (NO domain literals — the cue lemmas come from observation):
    #   social_role → keyed map: description carries the rel_type (generic person tie ``knows``).
    #   problem_noun → set: description carries a note (membership is the cue column).
    _CARVED = {
        "social_role": "knows",
        "problem_noun": "grown problem/fault eventive head (freq-gated, observed)",
        # Possessive-attribute head nouns proposed by the spine when it CONTAINED a construction
        # ("<possessor>'s <attribute-noun> is <adjectival value>") because the noun was not yet a
        # known attribute for this tenant. Without this registration the candidate is fetched,
        # found un-carved, and resolved away — the proposal would be recorded and then discarded,
        # i.e. a growth rail that is built and dark.
        "attribute_noun": "grown possessive-attribute head noun (freq-gated, observed)",
        # CESSATIVE-aspect matrix verbs the spine PROPOSED from the recognised phrasal aspectual
        # frame ("<matrix> <prt> <V-ing> …") when this tenant's class did not yet admit the cue.
        # ⚠️ REGISTRATION IS LOAD-BEARING, NOT BOOKKEEPING: the loop below resolves any candidate
        # whose category is NOT in this dict away as 'cue_skipped'. Without this line the spine
        # proposes, the queue counts, and NOTHING EVER GROWS — a rail that is built and dark, and
        # indistinguishable in the data from a lane that was never called.
        # ⚠️ AND THE VALUE IS NOT A HUMAN NOTE FOR THIS CLASS: cessative_verb rows carry their
        # ADMITTED COMPLEMENT SHAPES in `description` ('|'-joined), which is what
        # linguistic_cue_overlay.resolve_cessative_verb_shapes() parses. A prose note here would
        # grow a member admitting NO shape, i.e. a member that can never fire. The grown member
        # admits ONLY the gerundive-complement shape the proposal actually observed — never
        # direct_object or intransitive, which are the polysemous readings ("I dropped my phone")
        # the seeded class deliberately withholds.
        "cessative_verb": "xcomp_progressive",
    }
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT id, candidate_object_type, sample_object, occurrence_count"
                "  FROM ontology_evaluations"
                " WHERE extraction_method = 'linguistic_cue_candidate'"
                "   AND re_embedder_decision IS NULL"
                "   AND occurrence_count >= %s",
                (LINGUISTIC_CUE_GROWTH_THRESHOLD,),
            )
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001 — fail-safe: no candidates table / read error
        log.debug(f"re_embedder.cue_growth_fetch_failed schema={schema_name}: {str(e)[:140]}")
        return stats

    for (rid, category, cue, occ) in (rows or []):
        _cat = (category or "").strip().lower()
        _cue = (cue or "").strip().lower()
        if not _cat or not _cue or _cat not in _CARVED:
            # Not a carved class we grow (or malformed) → resolve so it stops re-reading.
            try:
                with db_conn.cursor() as cur:
                    cur.execute(
                        "UPDATE ontology_evaluations SET re_embedder_decision = 'cue_skipped',"
                        "  decision_timestamp = now() WHERE id = %s", (rid,))
                db_conn.commit()
            except Exception:  # noqa: BLE001
                try:
                    db_conn.rollback()
                except Exception:
                    pass
            continue
        _desc = _CARVED[_cat]
        try:
            with db_conn.cursor() as cur:
                # Grow the cue into the BOUND tenant schema (search_path = tenant, NO public). Convergence
                # by identity: ON CONFLICT (cue, category) DO NOTHING — a re-grow is a no-op.
                cur.execute(
                    "INSERT INTO linguistic_cues"
                    "  (cue, category, description, source, global_confidence, frequency,"
                    "   confirmed_count, is_active)"
                    " VALUES (%s, %s, %s, 'grown', 0.75, %s, %s, true)"
                    " ON CONFLICT (cue, category) DO NOTHING",
                    (_cue, _cat, _desc, occ, occ),
                )
                cur.execute(
                    "UPDATE ontology_evaluations SET re_embedder_decision = 'cue_grown',"
                    "  decision_timestamp = now(),"
                    "  decision_reason = %s WHERE id = %s",
                    (f"carved cue grown into linguistic_cues (category={_cat}, occ={occ})", rid),
                )
            db_conn.commit()
            stats["grown"] += 1
            log.info(f"re_embedder.cue_grown schema={schema_name} category={_cat} "
                     f"cue={_cue} occ={occ}")
        except Exception as e:  # noqa: BLE001 — per-candidate isolation
            stats["errors"] += 1
            try:
                db_conn.rollback()
                if schema_name:
                    with db_conn.cursor() as _r:
                        _r.execute(f"SET search_path TO {schema_name}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema from UUID-derived source with validation
            except Exception:  # noqa: BLE001
                pass
            log.warning(f"re_embedder.cue_growth_error schema={schema_name} "
                        f"category={_cat} cue={_cue}: {str(e)[:140]}")

    return stats


# Promotion threshold for correction-signal growth. SINGLE SOURCE OF TRUTH —
# reused by the candidate→correction_signals approval gate AND the
# correction_signals→correction_patterns firing promotion below. Do NOT
# fork this into a second literal; both gates approve at the SAME frequency.
CORRECTION_SIGNAL_PROMOTION_THRESHOLD = 3

# Minimum confidence a grown signal must carry before it is allowed to FIRE
# pre-GLiNER2. The firing path short-circuits intent classification, so a weak
# signal must never reach it.
_FIRING_MIN_CONFIDENCE = 0.85

# Bare correction lexemes that are valid *soft* correction_signals (substring
# hints consumed by extraction) but are FAR too greedy to fire pre-GLiNER2 as a
# regex short-circuit. Promoting any of these into correction_patterns would
# re-introduce the exact bug class we just fixed: a STATEMENT like
# "Actually, my favorite editor is vim" or "I was not at work" would short
# circuit to CORRECTION before GLiNER2 ever sees it. These are NEVER promoted to
# the firing table. (Matched whole-pattern, case-insensitive, after trimming.)
_FIRING_BARE_WORD_DENYLIST = frozenset({
    "not", "is not", "isn't", "isnt", "no", "never", "actually", "wait",
    "sorry", "wrong", "mistake", "incorrect", "really", "instead", "rather",
    "well", "hmm", "oops", "nope", "nah", "i meant", "my mistake",
})

# A structural firing pattern MUST contain at least one regex metacharacter that
# makes it match sentence *shape* rather than a bare token (a quantifier, an
# anchor, or an alternation/character class). This is what distinguishes
# "is .+, not (a |an )?" (anchored, bounded — safe) from "actually" (bare,
# greedy — unsafe). Subject-agnostic by construction: no entity names, only
# structure.
_FIRING_STRUCTURE_RE = re.compile(r"\.\+|\.\*|\\b|\^|\$|\[[^\]]+\]|\([^)]*\|[^)]*\)|\\s")


def _is_safe_firing_pattern(pattern: str, confidence: float) -> tuple[bool, str]:
    """Precision guard for promoting a grown correction_signal into the
    pre-GLiNER2 firing table (correction_patterns).

    The firing path (main.py:/classify-intent) runs BEFORE GLiNER2 and
    SHORT-CIRCUITS to CORRECTION on the first regex hit. A bad/greedy grown
    pattern therefore mis-routes legitimate STATEMENTs (e.g. preferences,
    negated facts) to retraction — the precise failure mode just fixed for
    preference statements. So this gate is intentionally strict: PRECISION
    DOMINATES recall. A signal is only allowed to fire if ALL hold:

      1. Non-empty, bounded length (3 <= len <= 200) — no runaway regex.
      2. confidence >= _FIRING_MIN_CONFIDENCE.
      3. The trimmed, lowercased pattern is NOT a bare common correction word
         (denylist) — bare words match anywhere and over-fire.
      4. The pattern contains genuine regex STRUCTURE (quantifier / anchor /
         alternation / char-class) so it matches sentence SHAPE, not a token.
      5. It compiles as a valid regex (a malformed pattern would raise inside
         the firing-path re.search and is useless).

    Returns (ok, reason). reason is logged for auditability either way.
    """
    if not pattern or not isinstance(pattern, str):
        return False, "empty_or_non_string"
    p = pattern.strip()
    if not (3 <= len(p) <= 200):
        return False, f"length_out_of_bounds len={len(p)}"
    if confidence is None or confidence < _FIRING_MIN_CONFIDENCE:
        return False, f"confidence_below_floor conf={confidence}"
    if p.lower() in _FIRING_BARE_WORD_DENYLIST:
        return False, "bare_common_word_denylisted"
    if not _FIRING_STRUCTURE_RE.search(p):
        # No quantifier/anchor/alternation → a bare token that would over-fire.
        return False, "no_regex_structure_bare_token"
    try:
        re.compile(p)
    except re.error as _re_err:
        # re.error's text quotes the pattern (tenant-grown text) — keep the reason a code + ref.
        return False, "invalid_regex " + _errors.public_detail(_re_err, where="firing_pattern.compile", what="re.error")
    return True, "ok"


def evaluate_correction_signal_candidates(db_conn, qwen_api_url: str) -> dict:
    """
    dprompt-128-P3: Evaluate correction signal candidates from correction_signal_evaluations.
    Runs each poll cycle, PER-TENANT (caller sets search_path to the user schema —
    these tables have NO user_id-scoping at the firing path; schema = scope, and
    public.* is a seed template that must never receive growth). Decisions:
      - 'approved': occurrence_count >= 3 → INSERT into correction_signals (soft hint layer)
      - 'rejected': occurrence_count < 3 → leave as candidate for future evaluation

    GROWTH→FIRING bridge: an approved signal that ALSO clears the strict
    precision guard (_is_safe_firing_pattern) is mirrored into correction_patterns,
    the table the pre-GLiNER2 short-circuit in /classify-intent actually reads.
    This closes the gap where the layer that GROWS (correction_signals) was not
    the layer that FIRES (correction_patterns). The guard ensures only bounded,
    anchored, high-confidence structural regexes ever reach the firing path.

    Returns: {"approved": int, "rejected": int, "promoted": int, "errors": int}
    """
    stats = {"approved": 0, "rejected": 0, "promoted": 0, "errors": 0}

    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT id, user_id, candidate_pattern, pattern_type,"
                "       first_text_snippet, occurrence_count"
                " FROM correction_signal_evaluations"
                " WHERE re_embedder_decision IS NULL"
                " ORDER BY occurrence_count DESC, last_seen_at DESC"
            )
            candidates = cur.fetchall()
    except Exception as e:
        log.error(f"re_embedder.correction_eval_fetch_failed: {e}")
        return stats

    if not candidates:
        return stats

    log.info(f"re_embedder.correction_eval_candidates count={len(candidates)}")

    for row in candidates:
        eval_id, user_id, candidate_pattern, pattern_type, snippet, occ = row
        try:
            decision = None
            reason = ""

            # ── Decision 1: Pattern frequency ──────────────────────────
            # Threshold: occurrence_count >= N means pattern is real and recurring.
            # Centralized threshold — same gate frequency for soft-signal approval
            # AND firing promotion below (do NOT invent a second number).
            if occ >= CORRECTION_SIGNAL_PROMOTION_THRESHOLD:
                decision = "approved"
                reason = f"occurrence_count={occ} >= {CORRECTION_SIGNAL_PROMOTION_THRESHOLD}"

                # Insert into correction_signals table (SOFT hint layer — substring
                # signals consumed by extraction; NOT yet a firing regex).
                _signal_conf = 0.7
                with db_conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO correction_signals
                        (pattern, pattern_type, priority, confidence, category, example_usage)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (pattern) DO UPDATE SET
                          occurrence_count = correction_signals.occurrence_count + 1,
                          updated_at = NOW()
                    """, (candidate_pattern, pattern_type, 2, _signal_conf, pattern_type, snippet))
                    log.info(f"re_embedder.correction_signal_approved pattern={candidate_pattern[:50]} type={pattern_type}")

                # ── GROWTH→FIRING bridge ────────────────────────────────
                # Mirror into correction_patterns (the table /classify-intent reads
                # PRE-GLiNER2) ONLY if the pattern clears the strict precision guard.
                # The firing path short-circuits intent classification, so a greedy
                # bare-word signal here would mis-route STATEMENTs to retraction
                # (the preference-misroute bug class). _is_safe_firing_pattern keeps
                # the firing table to bounded, anchored, high-confidence STRUCTURAL
                # regexes only — bare words ("not", "actually", ...) are rejected.
                # NOTE: the current candidate extractor (_track_correction_signal_candidate)
                # emits only bare tokens, so in practice NOTHING is promoted today;
                # this wiring lights up automatically once a structural candidate
                # appears, with no further code change.
                _fire_conf = max(_signal_conf, 0.9 if pattern_type == "negation" else _signal_conf)
                _safe, _why = _is_safe_firing_pattern(candidate_pattern, _fire_conf)
                if _safe:
                    with db_conn.cursor() as cur:
                        # active defaults TRUE; ON CONFLICT no-op keeps idempotent and
                        # never downgrades a hand-curated seed pattern.
                        cur.execute("""
                            INSERT INTO correction_patterns (pattern_text, confidence, active)
                            VALUES (%s, %s, TRUE)
                            ON CONFLICT (pattern_text) DO NOTHING
                        """, (candidate_pattern, _fire_conf))
                        if cur.rowcount > 0:
                            stats["promoted"] += 1
                            log.info(
                                f"re_embedder.correction_pattern_promoted_to_firing "
                                f"pattern={candidate_pattern[:60]} conf={_fire_conf:.2f} "
                                f"type={pattern_type}"
                            )
                else:
                    log.info(
                        f"re_embedder.correction_pattern_firing_promotion_blocked "
                        f"pattern={candidate_pattern[:60]} reason={_why} "
                        f"(stays soft-only — precision guard)"
                    )

            # ── Decision 2: Reject (wait for more occurrences) ──────────
            if not decision:
                decision = "rejected"
                reason = f"occurrence_count={occ} < 3, waiting for more evidence"

            # ── Apply decision ──────────────────────────────────────────
            with db_conn.cursor() as cur:
                cur.execute("""
                    UPDATE correction_signal_evaluations SET
                      re_embedder_decision = %s,
                      re_embedder_confidence = %s
                    WHERE id = %s
                """, (decision, 0.7 if decision == "approved" else 0.3, eval_id))

            stats[decision] += 1
            db_conn.commit()

        except Exception as e:
            db_conn.rollback()
            stats["errors"] += 1
            log.error(f"re_embedder.correction_eval_error eval_id={eval_id} pattern={candidate_pattern[:50]}: {e}")

    return stats


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def has_pending_ontology_work(db_conn) -> bool:
    """Check if there are unevaluated ontology candidates (fast query).

    dprompt-121: Event-driven guard to skip evaluation if no pending work.
    """
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM ontology_evaluations "
                "WHERE re_embedder_decision IS NULL LIMIT 1"
            )
            count = cur.fetchone()[0]
            return count > 0
    except Exception as e:
        log.warning(f"re_embedder.pending_ontology_check_failed error={str(e)}")
        return False


def has_pending_name_conflicts(db_conn) -> bool:
    """Check if there are unresolved name conflicts (fast query).

    dprompt-121: Event-driven guard to skip conflict resolution if no pending work.
    """
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM entity_name_conflicts "
                "WHERE status='pending' LIMIT 1"
            )
            count = cur.fetchone()[0]
            return count > 0
    except Exception as e:
        log.warning(f"re_embedder.pending_conflicts_check_failed error={str(e)}")
        return False


def flag_suspect_preferred_names(db_conn) -> dict:
    """Flag preferred aliases that nobody ever chose (ALIAS-PROVENANCE-DESIGN §3).

    A preferred name whose provenance is weak ('inferred', 'provisioned', 'merge',
    'unspecified') is suspect — it became the display name without a user choosing it,
    which is exactly how dead/legal/placeholder names surface. We FLAG ONLY here — we
    never auto-mutate names (non-destructive; the LLM/review path decides).

    The existing entity_name_conflicts review queue expects TWO entity ids disputing the
    SAME alias. A suspect preferred name is a different shape (one entity, low-trust
    preferred alias) — feeding it into entity_name_conflicts would be a brittle misuse
    of that schema. So we log the suspects at WARNING with a clear event name for review.

    TODO(ALIAS-PROVENANCE-DESIGN §3): combine with the embedding the re-embedder already
    computes — if a suspect preferred alias is also a vector outlier among the entity's
    other aliases, confidence it is wrong is high. Until that vector-outlier signal is
    wired in, this stays flag-only to avoid inventing a brittle auto-resolution mechanism.

    Per-user schema context: entity_aliases is per-user; search_path set by caller.
    Do NOT add user_id filtering.

    Returns dict: {"flagged": int}.
    """
    stats = {"flagged": 0}
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                # 'lexical' belongs in this ENGINE-WEAK set like any other growth source — it
                # ranks EQUAL to 'inferred' (registry._PREFERENCE_RANK); its distinct name
                # records a co-reference WARRANT, not extra trust, so it must never be read as
                # a user-chosen name. Currently unreachable here (the lexical lane hardwires
                # is_preferred=False) but listed so the set stays the whole engine-weak
                # vocabulary — an enumeration that silently omits a member is how this column
                # drifts. Keep in sync with sweep_ledger.py 'suspect_preferred_names'.
                "SELECT entity_id, alias, preference_source FROM entity_aliases "
                "WHERE is_preferred = true "
                "AND preference_source IN "
                "  ('inferred', 'lexical', 'provisioned', 'merge', 'unspecified')"
            )
            suspects = cur.fetchall()
        for entity_id, alias, source in suspects:
            stats["flagged"] += 1
            log.warning(
                "re_embedder.suspect_preferred_name "
                f"entity_id={str(entity_id)[:16]} alias={alias} preference_source={source} "
                "reason=preferred_alias_never_user_chosen (flag-only, see ALIAS-PROVENANCE-DESIGN)"
            )
    except Exception as e:
        # Column may not exist yet on a schema that missed migration 076 — non-fatal.
        log.warning(f"re_embedder.suspect_preferred_names_check_failed error={str(e)[:200]}")
        try:
            db_conn.rollback()
        except Exception:
            pass
    return stats


def has_pending_retraction_outcomes(db_conn) -> bool:
    """Check if there are unevaluated retraction outcomes (fast query).

    dprompt-137: Event-driven guard to skip evaluation if no feedback available.
    Returns True if retraction_outcomes table has any rows with was_correct=true or was_correct=false
    (i.e., user has provided feedback/validation).
    """
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM retraction_outcomes "
                "WHERE was_correct IS NOT NULL LIMIT 1"
            )
            count = cur.fetchone()[0]
            return count > 0
    except Exception as e:
        log.warning(f"re_embedder.pending_retraction_outcomes_check_failed error={str(e)}")
        return False


def _tenant_has_reembed_work(db_conn, schema_name: str = "") -> bool:
    """EXECUTION GATE — True iff ANY re-embedder work is DUE for the tenant bound on
    ``db_conn`` (caller has already ``SET search_path TO {schema}``, NO public).

    Reads REAL DB state via indexed EXISTS/LIMIT-1 probes, ordered cheapest-first with
    early-exit — NOT a dirty-flag watermark. A provably-idle tenant (nothing unsynced,
    nothing promotable, nothing expiring/decaying, no pending ontology/conflicts) is
    detected in a handful of index probes and SKIPPED before ANY embedding-model spin /
    Qdrant scroll / lifecycle pass.

    FAIL-SAFE — "WE DON'T FORGET": any probe error / ambiguity (missing column on an old
    schema, aborted txn, etc.) → return True (PROCESS the tenant). The gate may ONLY ever
    skip a PROVABLY-idle tenant; it never skips due work on a check failure. This function
    NEVER raises — an unexpected error returns True.

    Probe → guarded work map (each maps 1:1 to a real pass in main()'s PHASE 2b lifecycle):
      1. unsynced staged rows (Class-C embed/upsert; non-C mark-synced housekeeping)
                                                  → fetch_unsynced_staged embed/upsert loop
      2. staged Class-B promotable now (cc>=3)    → promote_staged_facts
      3. staged Class-C promotable by hits (>=3)  → promote_class_c_hits
      4. staged Class-C past window (expiry/decay) → expire_staged_facts + decay_class_c_hits
      5. unsynced facts-table (A/B) — legacy only → fetch_unsynced facts sync (VECTOR_CLASS_C_ONLY OFF)
      6. pending ontology_evaluations             → evaluate_ontology_candidates
      7. unresolved entity_name_conflicts         → resolve_name_conflicts

    Reconcile/divergence is deliberately NOT probed here (knowing it requires a full Qdrant
    scroll); it is bounded on a COARSER cadence in main() (activity-driven OR a max-interval
    ceiling) rather than scrolling every cycle just to check.
    """
    def _exists(sql: str, params: tuple = ()) -> bool:
        with db_conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return bool(row and row[0])

    try:
        # 1. Unsynced staged rows awaiting the embed/upsert pass. Predicate is IDENTICAL to
        #    fetch_unsynced_staged() (qdrant_synced=false, not promoted, not expired, live).
        #    Under VECTOR_CLASS_C_ONLY only Class C is embedded and non-C rows are
        #    mark-synced — both are real work the staged loop performs, so gate on either.
        if _exists(
            "SELECT 1 FROM staged_facts "
            "WHERE qdrant_synced = false AND promoted_at IS NULL "
            "  AND expires_at > now() AND deleted_at IS NULL "
            "LIMIT 1"
        ):
            return True

        # 2. Staged Class-B rows promotable now — mirrors promote_staged_facts() candidates.
        if _exists(
            "SELECT 1 FROM staged_facts "
            "WHERE fact_class = 'B' AND confirmed_count >= 3 AND promoted_at IS NULL "
            "LIMIT 1"
        ):
            return True

        # 3. Staged Class-C rows promotable by query hits — mirrors promote_class_c_hits().
        if _exists(
            "SELECT 1 FROM staged_facts "
            "WHERE fact_class = 'C' AND hit_count >= 3 AND promoted_at IS NULL "
            "LIMIT 1"
        ):
            return True

        # 4. Class-C rows past their 30-day window (expiry/decay due). ONE probe covers BOTH
        #    expire_staged_facts (confirmed_count decay/remove) AND decay_class_c_hits
        #    (hit_count decay/drop) — both key on fact_class='C' AND expires_at<=now().
        if _exists(
            "SELECT 1 FROM staged_facts "
            "WHERE fact_class = 'C' AND expires_at <= now() AND promoted_at IS NULL "
            "LIMIT 1"
        ):
            return True

        # 5. Unsynced facts-table (A/B) rows — ONLY work in legacy mode. Under the default
        #    VECTOR_CLASS_C_ONLY the facts-table sync loop is SKIPPED entirely, so unsynced
        #    A/B is NOT work; don't probe it (avoids waking an idle C-only tenant).
        if not _VECTOR_CLASS_C_ONLY:
            if _exists(
                "SELECT 1 FROM facts "
                "WHERE qdrant_synced = false AND superseded_at IS NULL "
                "LIMIT 1"
            ):
                return True

        # 6. Pending ontology candidates awaiting evaluation — mirrors has_pending_ontology_work().
        if _exists(
            "SELECT 1 FROM ontology_evaluations WHERE re_embedder_decision IS NULL LIMIT 1"
        ):
            return True

        # 7. Unresolved name conflicts — mirrors has_pending_name_conflicts().
        if _exists(
            "SELECT 1 FROM entity_name_conflicts WHERE status = 'pending' LIMIT 1"
        ):
            return True

        # 8. Pending async-ingestion documents — mirrors drain_pending_documents().
        #    A brand-new tenant that JUST enqueued a document has NO staged/ontology work,
        #    so without this probe the gate would skip it and the doc would never drain.
        #    Pre-migration-183 schemas lack the table → the _exists probe raises →
        #    fail-safe except-branch returns True (harmless; nothing to drain there anyway).
        if _exists(
            "SELECT 1 FROM documents WHERE status IN ('pending', 'processing') LIMIT 1"
        ):
            return True

        return False
    except Exception as e:
        # FAIL-SAFE ("we don't forget"): any probe error → process the tenant. Roll back the
        # aborted txn and re-apply the tenant search_path so the ensuing pass runs clean.
        log.warning(
            f"re_embedder.gate.probe_error schema={schema_name} "
            f"(fail-safe -> processing tenant): {type(e).__name__}: {str(e)[:160]}"
        )
        try:
            db_conn.rollback()
            if schema_name:
                with db_conn.cursor() as _spc:
                    _spc.execute(f"SET search_path TO {schema_name}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema from UUID-derived source with validation
        except Exception:
            pass
        return True


def evaluate_retraction_outcomes(db_conn, frequency_threshold: int = 3) -> dict:
    """Phase 4: Self-building learning loop for retraction signals (dprompt-137).

    Learn from successful/unsuccessful retraction outcomes in real time.
    Auto-register patterns where frequency >= threshold, update metrics for existing patterns.

    Algorithm:
    1. Query retraction_outcomes for rows with was_correct IS NOT NULL (user feedback)
    2. Group by original_message (pattern proxy) to detect frequency
    3. For each high-frequency pattern (freq >= threshold):
       - Check if pattern exists in retraction_signals table
       - If NOT exists: INSERT new pattern with empirical confidence + priority
       - If EXISTS: UPDATE confidence/priority/false_positive_rate based on outcomes
    4. Invalidate Filter cache to force reload on next request

    Returns: {"discovered": int, "updated": int, "errors": int}

    Design rationale:
    - Frequency threshold of 3: prevents single-shot false learning (1-2 occurrences are noise)
    - Confidence = avg(was_correct=true) / total_outcomes (empirical success rate)
    - Priority = success_rate * 100 (0-100 scale), fed back to signal priority ordering
    - False positive rate = (total - correct) / total, used by Filter for semantic gating
    - No hardcoded patterns — all patterns learned from live data
    """
    stats = {"discovered": 0, "updated": 0, "errors": 0}

    try:
        # ────────────────────────────────────────────────────────────────
        # Step 1: Query outcomes grouped by original_message pattern
        # ────────────────────────────────────────────────────────────────
        with db_conn.cursor() as cur:
            cur.execute("""
                SELECT original_message,
                       COUNT(*) as freq,
                       CAST(COUNT(CASE WHEN was_correct=true THEN 1 END) AS FLOAT) as correct_count,
                       COUNT(*) as total_count,
                       AVG(detected_confidence) as avg_confidence,
                       AVG(CASE WHEN was_correct=true THEN 1.0 ELSE 0.0 END) as success_rate,
                       retraction_method,
                       MAX(created_at) as last_seen
                FROM retraction_outcomes
                WHERE was_correct IS NOT NULL
                GROUP BY original_message, retraction_method
                HAVING COUNT(*) >= %s
                ORDER BY success_rate DESC, freq DESC
            """, (frequency_threshold,))
            outcomes = cur.fetchall()

        if not outcomes:
            log.debug(f"re_embedder.retraction_outcomes_empty no_feedback_found")
            return stats

        log.info(f"re_embedder.retraction_outcomes_eval found={len(outcomes)} patterns_to_evaluate")

        # ────────────────────────────────────────────────────────────────
        # Step 2: Process each high-frequency pattern
        # ────────────────────────────────────────────────────────────────
        for (pattern, freq, correct_count, total_count, avg_confidence,
             success_rate, retraction_method, last_seen) in outcomes:

            try:
                if not pattern or len(pattern.strip()) < 2:
                    continue  # Skip empty/whitespace patterns

                pattern_lower = pattern.lower().strip()

                # DB→Python numeric boundary (prod crash 2026-08-20, pattern
                # 'forget that they have a gender named F'): psycopg2 returns
                # decimal.Decimal for AVG() over a NUMERIC expression — the
                # CASE's 1.0/0.0 literals are numeric, so AVG(numeric) comes
                # back numeric → Decimal, while everything downstream in this
                # lane is float math (1.0 - rate, rate * 100, :.2f formats) and
                # the retraction_signals.false_positive_rate column is FLOAT.
                # Coerce ONCE here at the row-unpack boundary, ONE way, to
                # float: float - Decimal raises TypeError (the
                # retraction_outcome_processing_failed crash this pins), and
                # Decimal's exact-decimal arithmetic is nothing this lane does.
                # None → 0.5 preserves the historical no-data defaults exactly
                # (false_positive_rate 0.5, priority 50) while keeping every
                # downstream read uniformly float — the notes/log ':.2f'
                # formats below crash on None (NoneType.__format__), which the
                # old is-not-None ternaries half-supported.
                success_rate = float(success_rate) if success_rate is not None else 0.5

                # Compute empirical metrics
                false_positive_rate = (1.0 - success_rate) if success_rate is not None else 0.5
                priority = int(success_rate * 100) if success_rate else 50

                # ────────────────────────────────────────────────────────────────
                # Step 2a: Check if pattern already exists in retraction_signals
                # ────────────────────────────────────────────────────────────────
                with db_conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, signal_category, priority, false_positive_rate "
                        "FROM retraction_signals "
                        "WHERE signal = %s AND language = 'en'",
                        (pattern_lower,)
                    )
                    existing = cur.fetchone()

                if not existing:
                    # ────────────────────────────────────────────────────────────────
                    # Step 2b: NEW PATTERN — insert with empirical confidence
                    # ────────────────────────────────────────────────────────────────
                    category = 'inferred'  # Learned from live data, not seeded
                    if retraction_method == 'semantic':
                        category = 'correction'  # LLM-detected semantic patterns
                    elif retraction_method == 'pattern':
                        category = 'implicit_negation'  # Explicit pattern matches

                    try:
                        with db_conn.cursor() as cur:
                            # Insert into retraction_signals (extraction/detection layer)
                            cur.execute("""
                                INSERT INTO retraction_signals
                                (signal, signal_category, language, priority, false_positive_rate, notes, created_at, updated_at)
                                VALUES (%s, %s, 'en', %s, %s, %s, NOW(), NOW())
                                ON CONFLICT (signal, language) DO NOTHING
                            """, (
                                pattern_lower,
                                category,
                                priority,
                                false_positive_rate,
                                f"Auto-learned: freq={freq}, success_rate={success_rate:.2f}, method={retraction_method}"
                            ))

                            # Also insert into negation_patterns (intent classification layer)
                            # Map retraction_signals to negation_patterns for /classify-intent.
                            # PER-TENANT: this runs under the caller's bound tenant search_path
                            # (SET search_path TO {schema}, NO public — see the per-tenant growth
                            # loop). The write is UNQUALIFIED so it lands in <schema>.negation_patterns
                            # (no user_id column — schema provides isolation). public is template-only;
                            # this self-growth write NEVER touches public (cross-tenant pollution).
                            negation_type = 'retraction' if category != 'correction' else 'correction'
                            negation_confidence = min(0.99, priority / 100.0)  # priority 50-100 → confidence 0.5-0.99

                            cur.execute("""
                                INSERT INTO negation_patterns
                                (pattern_text, negation_type, learned_from, confidence, created_at)
                                VALUES (%s, %s, 'retraction_outcome_learning', %s, NOW())
                                ON CONFLICT (pattern_text, negation_type) DO UPDATE
                                SET confidence = GREATEST(negation_patterns.confidence, %s),
                                    created_at = NOW()
                            """, (
                                pattern_lower,
                                negation_type,
                                negation_confidence,
                                negation_confidence
                            ))

                        db_conn.commit()
                        stats["discovered"] += 1
                        log.info(
                            f"re_embedder.pattern_learned_to_both_tables "
                            f"pattern={pattern[:60]} "
                            f"retraction_signals.priority={priority} "
                            f"negation_patterns.confidence={negation_confidence:.2f} "
                            f"freq={freq} success_rate={success_rate:.2f}"
                        )
                    except Exception as e:
                        db_conn.rollback()
                        stats["errors"] += 1
                        log.error(f"re_embedder.pattern_learning_failed pattern={pattern[:60]}: {e}")

                else:
                    # ────────────────────────────────────────────────────────────────
                    # Step 2c: EXISTING PATTERN — update metrics based on empirical data
                    # ────────────────────────────────────────────────────────────────
                    existing_id, existing_category, existing_priority, existing_fpr = existing

                    # Only update if metrics changed significantly (> 5% delta)
                    priority_delta = abs(priority - existing_priority)
                    fpr_delta = abs(false_positive_rate - existing_fpr)

                    if priority_delta > 5 or fpr_delta > 0.05:
                        try:
                            with db_conn.cursor() as cur:
                                # Update retraction_signals (extraction/detection layer)
                                cur.execute("""
                                    UPDATE retraction_signals SET
                                      priority = %s,
                                      false_positive_rate = %s,
                                      updated_at = NOW(),
                                      notes = %s
                                    WHERE id = %s
                                """, (
                                    priority,
                                    false_positive_rate,
                                    f"Updated: freq={freq}, success_rate={success_rate:.2f}, old_priority={existing_priority}",
                                    existing_id
                                ))

                                # Also update negation_patterns (intent classification layer).
                                # PER-TENANT: runs under the bound tenant search_path (NO public);
                                # UNQUALIFIED so it updates <schema>.negation_patterns (schema =
                                # isolation, no user_id column). public is template-only.
                                negation_type = 'retraction' if existing_category != 'correction' else 'correction'
                                negation_confidence = min(0.99, priority / 100.0)

                                cur.execute("""
                                    UPDATE negation_patterns SET
                                      confidence = GREATEST(confidence, %s),
                                      created_at = NOW()
                                    WHERE pattern_text = %s AND negation_type = %s
                                """, (
                                    negation_confidence,
                                    pattern_lower,
                                    negation_type
                                ))

                            db_conn.commit()
                            stats["updated"] += 1
                            log.info(
                                f"re_embedder.pattern_updated_in_both_tables "
                                f"pattern={pattern[:60]} "
                                f"retraction_signals.priority={existing_priority}->{priority} "
                                f"negation_patterns.confidence updated "
                                f"fpr={existing_fpr:.2f}->{false_positive_rate:.2f}"
                            )
                        except Exception as e:
                            db_conn.rollback()
                            stats["errors"] += 1
                            log.error(f"re_embedder.retraction_signal_update_failed pattern={pattern[:60]}: {e}")

            except Exception as e:
                stats["errors"] += 1
                log.error(f"re_embedder.retraction_outcome_processing_failed pattern={pattern[:60]}: {e}")

        # ═══════════════════════════════════════════════════════════════════════════════
        # Step 3: Cache Invalidation via TTL (Passive, Not Aggressive)
        # ═══════════════════════════════════════════════════════════════════════════════
        #
        # Filter caches retraction_signals with a 60-second TTL. When re-embedder discovers
        # new patterns and INSERTs them to retraction_signals, Filter's cache remains valid
        # until expiry. Pattern visibility occurs naturally on next /classify-intent call
        # after TTL expiration (within 60 seconds).
        #
        # DESIGN RATIONALE:
        # ─────────────────
        # TTL-based invalidation is optimal because:
        #
        # 1. INDEPENDENCE: Filter and re-embedder run independently. No IPC/shared memory.
        #    TTL is atomic, simpler than distributed cache coherency.
        #
        # 2. GRACEFUL DEGRADATION: If PostgreSQL unavailable, Filter uses stale cache.
        #    Aggressive invalidation would require external coordination.
        #
        # 3. BALANCED TRADE-OFF: 60s window balances freshness (reasonable for learning)
        #    vs DB load (minimal). Pattern discovery is asynchronous (~10s poll cycle).
        #
        # 4. NO THUNDERING HERD: Cache natural expiry spreads reloads over time.
        #    Explicit invalidation could spike DB hits when many patterns discovered.
        #
        # WHY NOT AGGRESSIVE INVALIDATION:
        # ────────────────────────────────
        # ❌ Redis pubsub: Adds Redis dependency, operational complexity
        # ❌ Explicit endpoint: Creates race conditions (invalidation in-flight)
        # ❌ Shared queue: Requires coordinated polling (defeats purpose)
        #
        # TIMELINE EXAMPLE:
        # ─────────────────
        # t=0s    : Filter loads retraction_signals, caches (timestamp=0, TTL=60s)
        # t=35s   : Re-embedder discovers "i'm not X, i'm Y" pattern (frequency=3)
        # t=35s   : INSERT retraction_signals(signal='i''m not', priority=85, ...)
        # t=60s   : Filter cache still valid (35s < 60s TTL)
        # t=62s   : User message triggers /classify-intent
        # t=62s   : Cache check: (62 - 0) = 62s > 60s TTL → EXPIRED
        # t=62s   : Reload from DB → new pattern 'i''m not' visible
        # t=62s+  : New pattern available for classification
        #
        # CODE REFERENCE:
        # ───────────────
        # Filter cache TTL check: openwebui/faultline_function.py lines 776-778
        # ```python
        # if cache_timestamp > 0 and (time() - cache_timestamp) < _RETRACTION_SIGNALS_TTL:
        #     return cached_data  # Cache valid
        # else:
        #     reload_from_db()   # TTL expired, refresh
        # ```
        #
        # DECISION LOG:
        # ─────────────
        # ✅ KEEP TTL-based model (no code changes needed)
        # ❌ Don't implement Redis invalidation (overengineered)
        # ❌ Don't add explicit invalidation endpoint (adds complexity)
        # ✅ Rely on PostgreSQL + TTL for cache coherency
        #
        # Filter will auto-reload signals on next /classify-intent call when cache
        # expires (typically within 60 seconds of pattern discovery).

        if stats["discovered"] > 0 or stats["updated"] > 0:
            log.info(
                f"re_embedder.retraction_learning_cycle_complete "
                f"discovered={stats['discovered']} "
                f"updated={stats['updated']} "
                f"errors={stats['errors']}"
            )

    except Exception as e:
        log.error(f"re_embedder.retraction_outcomes_eval_failed: {e}")
        stats["errors"] += 1

    return stats


def resolve_name_conflicts(db_conn, llm_url: str) -> dict:
    """
    Resolve pending entity name conflicts via LLM context evaluation.

    dprompt-121: When two entities claim the same preferred name, this function
    evaluates the context (facts) of each entity and uses the LLM to decide which
    entity should own the preferred name. Non-destructive: all names preserved,
    only is_preferred flag changes.

    Per-user schema context: entity_name_conflicts and entity_aliases tables
    are per-user. search_path already set by caller. Do NOT add user_id filtering.

    Args:
        db_conn: Database connection (search_path pre-set to user's schema)
        llm_url: LLM endpoint URL (e.g., http://localhost:11434/v1/chat/completions)

    Returns:
        dict with stats: {"resolved": int, "errors": int, "skipped": int}
    """
    stats = {"resolved": 0, "errors": 0, "skipped": 0}

    try:
        # ────────────────────────────────────────────────────────────────
        # Step 1: Query pending conflicts (limit to avoid overload)
        # ────────────────────────────────────────────────────────────────
        with db_conn.cursor() as cur:
            cur.execute("""
                SELECT c.id, c.entity_id_a, COALESCE(a1.alias, c.entity_id_a),
                       c.entity_id_b, COALESCE(a2.alias, c.entity_id_b), c.alias
                FROM entity_name_conflicts c
                LEFT JOIN entity_aliases a1 ON a1.entity_id = c.entity_id_a AND a1.is_preferred = true
                LEFT JOIN entity_aliases a2 ON a2.entity_id = c.entity_id_b AND a2.is_preferred = true
                WHERE c.status = 'pending'
                ORDER BY c.created_at ASC
                LIMIT 20
            """)
            conflicts = cur.fetchall()
        # READ BARRIER: the arbitration loop below is a per-conflict LLM call.
        release_read_transaction(db_conn, context="re_embedder.resolve_name_conflicts.fetch")

        if not conflicts:
            log.debug("re_embedder.name_conflicts_none_pending")
            return stats

        log.info(f"re_embedder.name_conflict_resolution_start pending={len(conflicts)}")

        # The tenant user entity (id == user_id) is CANONICAL and must win any conflict
        # over its OWN name — never let the LLM adjudicate the user's identity onto a
        # surrogate (registry.py: "user"/first-person always resolves to user_id). Derive
        # user_id from the bound per-tenant schema (faultline_<uuid-with-underscores>).
        # Fail-safe: None → today's LLM-only behavior.
        _tenant_user_id = None
        # The BOUND schema name, kept for the arrival weld guard's seat-anchor exemption.
        # This function takes no schema_name parameter (search_path is pre-set by the caller),
        # and the guard's strongest seat marker is the UUID-shaped suffix of that schema name,
        # so it must be read from the connection rather than guessed. Fail-safe None: the
        # guard then falls back to its second marker (a provisioning-tier alias), which is
        # measured present on 11 of 12 tenants.
        _tenant_schema = None
        try:
            with db_conn.cursor() as _uc:
                _uc.execute("SELECT current_schema()")
                _sch = (_uc.fetchone() or [None])[0] or ""
            if _sch.startswith("faultline_"):
                _tenant_user_id = _sch[len("faultline_"):].replace("_", "-")
                _tenant_schema = _sch
        except Exception:
            _tenant_user_id = None
            _tenant_schema = None

        # ────────────────────────────────────────────────────────────────
        # Step 2: Process each conflict
        # ────────────────────────────────────────────────────────────────
        for conflict_id, entity_id_1, entity_name_1, entity_id_2, entity_name_2, disputed_name in conflicts:
            # READ BARRIER (per iteration): this loop body blocks on the brain/Qdrant, and a
            # read left open by the PREVIOUS iteration would ride across it. A batch-level
            # barrier alone does not cover this — measured live: climb_state and the
            # taxonomy reads were each caught idle-in-transaction at 58-59s inside a loop.
            release_read_transaction(db_conn, context="re_embedder.resolve_name_conflicts.iteration")
            try:
                # ────────────────────────────────────────────────────────────────
                # Build context for Entity 1
                # ────────────────────────────────────────────────────────────────
                context_1 = ""
                with db_conn.cursor() as cur:
                    cur.execute("""
                        SELECT COUNT(*),
                               STRING_AGG(DISTINCT rel_type, ', ' ORDER BY rel_type) as rel_types
                        FROM facts
                        WHERE subject_id = %s OR object_id = %s
                    """, (entity_id_1, entity_id_1))
                    row = cur.fetchone()
                    if row:
                        fact_count, rel_types = row
                        rel_types_str = rel_types or "(none)"
                        context_1 = (
                            f"Entity 1 ('{entity_name_1}', UUID: {entity_id_1[:8]}...) "
                            f"has {fact_count} facts with relationship types: {rel_types_str}"
                        )
                    else:
                        context_1 = f"Entity 1 ('{entity_name_1}') has no facts."

                # ────────────────────────────────────────────────────────────────
                # Build context for Entity 2
                # ────────────────────────────────────────────────────────────────
                context_2 = ""
                with db_conn.cursor() as cur:
                    cur.execute("""
                        SELECT COUNT(*),
                               STRING_AGG(DISTINCT rel_type, ', ' ORDER BY rel_type) as rel_types
                        FROM facts
                        WHERE subject_id = %s OR object_id = %s
                    """, (entity_id_2, entity_id_2))
                    row = cur.fetchone()
                    if row:
                        fact_count, rel_types = row
                        rel_types_str = rel_types or "(none)"
                        context_2 = (
                            f"Entity 2 ('{entity_name_2}', UUID: {entity_id_2[:8]}...) "
                            f"has {fact_count} facts with relationship types: {rel_types_str}"
                        )
                    else:
                        context_2 = f"Entity 2 ('{entity_name_2}') has no facts."

                # ────────────────────────────────────────────────────────────────
                # Call LLM to disambiguate (fail-loud on LLM errors)
                # ────────────────────────────────────────────────────────────────
                from src.api.llm_client import build_llm_payload, get_llm_headers

                prompt = (
                    f"Two entities claim the name '{disputed_name}':\n\n"
                    f"{context_1}\n\n"
                    f"{context_2}\n\n"
                    f"Which entity should have '{disputed_name}' as its preferred display name? "
                    f"Answer with ONLY 'Entity 1' or 'Entity 2' (no explanation)."
                )

                messages = [{"role": "user", "content": prompt}]

                # Routed through the centralized LLM stack (circuit breaker, rate pacing,
                # timeouts) rather than a hand-rolled POST. `llm_url` is deliberately no
                # longer used here; it stays in the signature for the caller.
                try:
                    # READ BARRIER (immediately before the blocking call — the RE-ARM case). A barrier at
                    # the top of the enclosing block is NOT enough: a per-row read helper opens a FRESH
                    # transaction after it, and that read then rides across this hop. Measured live on the
                    # deployed image — climb_classification_chains was killed twice this way (02:42:25 and
                    # 02:45:06), its whole _ont_db subsystem chain failing 'connection already closed' four
                    # seconds later.
                    release_read_transaction(db_conn, context="re_embedder.resolve_name_conflicts.pre_blocking_call")
                    result = call_llm_with_retry_sync(
                        messages=messages,
                        model=LLMModels.get("NAME_CONFLICT"),
                        operation="NAME_CONFLICT",
                        # PER-TENANT attribution: the LOOP-BOUND tenant rather than the
                        # `_tenant_user_id` derived above (that one is the CANONICAL-USER guard,
                        # compared against entity ids and intentionally shape-unchecked); this
                        # one is validated, so a non-`faultline_<uuid>` search_path can never
                        # become a junk identity.
                        user_id=_reembedder_llm_user_id(),
                        # NO `temperature=` here. call_llm_with_retry_sync takes no such
                        # parameter and has no **kwargs, so passing it raised TypeError on
                        # EVERY call — swallowed by the `except Exception` below, which meant
                        # resolve_name_conflicts had never once reached the LLM since this line
                        # was written. Temperature is pinned 0.0 centrally (llm_client.py) and
                        # determinism is what we want for arbitration anyway.
                        max_tokens=10,
                    )
                except Exception as e:
                    log.error(
                        f"re_embedder.name_conflict_llm_call_failed "
                        f"conflict_id={conflict_id} "
                        f"error={str(e)}"
                    )
                    stats["errors"] += 1
                    continue

                # ────────────────────────────────────────────────────────────────
                # Parse LLM decision
                # ────────────────────────────────────────────────────────────────
                try:
                    # Handle both direct JSON and wrapped response
                    if isinstance(result, dict) and "choices" in result:
                        llm_choice = result["choices"][0]["message"]["content"].strip().lower()
                    elif isinstance(result, dict) and "content" in result:
                        llm_choice = result.get("content", "").strip().lower()
                    else:
                        llm_choice = str(result).strip().lower()

                    # Determine winner
                    if "entity 1" in llm_choice:
                        winner_id = entity_id_1
                        loser_id = entity_id_2
                        winner_name = entity_name_1
                        loser_name = entity_name_2
                    elif "entity 2" in llm_choice:
                        winner_id = entity_id_2
                        loser_id = entity_id_1
                        winner_name = entity_name_2
                        loser_name = entity_name_1
                    else:
                        log.warning(
                            f"re_embedder.name_conflict_llm_ambiguous "
                            f"conflict_id={conflict_id} "
                            f"llm_response={llm_choice[:100]}"
                        )
                        stats["skipped"] += 1
                        continue

                except Exception as e:
                    log.error(
                        f"re_embedder.name_conflict_parse_failed "
                        f"conflict_id={conflict_id} "
                        f"error={str(e)}"
                    )
                    stats["errors"] += 1
                    continue

                # CANONICAL USER OVERRIDE (deterministic): if either entity IS the tenant
                # user (id == user_id), the user ALWAYS wins its own name — override the LLM.
                # The user's identity is authoritative and must never be merged onto a
                # surrogate. The recall path looks up the user's name by user_id, so losing
                # it here is exactly what made "what is my name?" return nothing.
                if _tenant_user_id and entity_id_1 == _tenant_user_id and winner_id != entity_id_1:
                    winner_id, loser_id = entity_id_1, entity_id_2
                    winner_name, loser_name = entity_name_1, entity_name_2
                    log.info(f"re_embedder.name_conflict_user_canonical conflict_id={conflict_id} winner_is_user=true")
                elif _tenant_user_id and entity_id_2 == _tenant_user_id and winner_id != entity_id_2:
                    winner_id, loser_id = entity_id_2, entity_id_1
                    winner_name, loser_name = entity_name_2, entity_name_1
                    log.info(f"re_embedder.name_conflict_user_canonical conflict_id={conflict_id} winner_is_user=true")

                # ────────────────────────────────────────────────────────────────
                # Update aliases based on LLM decision
                # ────────────────────────────────────────────────────────────────
                try:
                    with db_conn.cursor() as cur:
                        # Clear any OTHER preferred alias on the winner FIRST — the
                        # one-preferred-per-entity constraint (idx_entity_aliases_one_preferred)
                        # rejects a second preferred row, which was aborting the whole merge.
                        cur.execute(
                            "UPDATE entity_aliases SET is_preferred = false "
                            "WHERE entity_id = %s AND alias <> %s AND is_preferred = true",
                            (winner_id, disputed_name)
                        )
                        # Winner: set preferred
                        cur.execute(
                            "UPDATE entity_aliases SET is_preferred = true "
                            "WHERE entity_id = %s AND alias = %s",
                            (winner_id, disputed_name)
                        )

                        # Loser: unset preferred
                        cur.execute(
                            "UPDATE entity_aliases SET is_preferred = false "
                            "WHERE entity_id = %s AND alias = %s",
                            (loser_id, disputed_name)
                        )

                        # If loser has no other aliases, create fallback
                        cur.execute(
                            "SELECT COUNT(*) FROM entity_aliases WHERE entity_id = %s",
                            (loser_id,)
                        )
                        loser_alias_count = cur.fetchone()[0]

                        if loser_alias_count == 1:
                            # Loser's only alias is the disputed name; create fallback
                            fallback_alias = f"{disputed_name}_entity_{loser_id[:8]}"
                            cur.execute(
                                "INSERT INTO entity_aliases (entity_id, alias, is_preferred) "
                                "VALUES (%s, %s, false) "
                                "ON CONFLICT (entity_id, alias) DO NOTHING",
                                (loser_id, fallback_alias)
                            )

                        # Mark conflict as resolved
                        cur.execute(
                            "UPDATE entity_name_conflicts SET "
                            "status = 'resolved', resolved_by = %s, resolved_at = NOW() "
                            "WHERE id = %s",
                            (f"Winner: {winner_name}; Loser fallback: {fallback_alias if loser_alias_count == 1 else 'existing'}",
                             conflict_id)
                        )

                    # Commit alias resolution first — guaranteed safe even if merge fails
                    db_conn.commit()
                    stats["resolved"] += 1

                    log.info(
                        f"re_embedder.name_conflict_resolved "
                        f"conflict_id={conflict_id} "
                        f"disputed_name={disputed_name} "
                        f"winner={winner_name} "
                        f"loser={loser_name}"
                    )

                    # ────────────────────────────────────────────────────────────────
                    # dBug-076: Entity merge — repoint all loser references to winner.
                    # Runs in a SEPARATE transaction after alias resolution commits,
                    # so merge failures cannot roll back the alias fix.
                    # ────────────────────────────────────────────────────────────────
                    try:
                        with db_conn.cursor() as _mcur:
                            # ── ARRIVAL WELD GUARD — PRE-FLIGHT ADMISSIBILITY ────────────
                            # A merge is a co-reference assertion about EVERY surface the
                            # loser holds: step 5 re-parents each one onto the winner, and it
                            # does so with raw UPDATEs rather than EntityRegistry.register_alias,
                            # so none of them pass the guard that every other alias weld
                            # passes. This is the largest remaining weld vector — one
                            # LLM-arbitrated decision moves N surfaces at once.
                            #
                            # Checked PER ALIAS, but the VERDICT IS MERGE-LEVEL: if any single
                            # surface is inadmissible on the winner, the whole merge is
                            # abandoned. Partially merging is not the safe half-measure it
                            # looks like — steps 1-4 have already repointed facts and
                            # attributes, so skipping just the offending alias would leave the
                            # loser as an empty husk holding a name whose facts now live on a
                            # different entity, which is a WORSE state than either outcome. A
                            # merge is one identity claim; it applies whole or not at all.
                            #
                            # The alias-resolution fix above has already COMMITTED and is
                            # deliberately untouched — refusing here declines the merge only.
                            # Fail-open: any probe error inside the guard returns "allow".
                            _weld_block = None
                            try:
                                _mcur.execute(
                                    "SELECT alias, preference_source FROM entity_aliases "
                                    "WHERE entity_id = %s AND alias NOT IN ("
                                    "  SELECT alias FROM entity_aliases WHERE entity_id = %s)",
                                    (loser_id, winner_id),
                                )
                                _moving = _mcur.fetchall() or []
                                for _m_alias, _m_src in _moving:
                                    if weld_guard.refuse_weld(
                                            _mcur, winner_id, _m_alias, _m_src,
                                            is_preferred=False, schema_name=_tenant_schema):
                                        _weld_block = (_m_alias, _m_src)
                                        break
                            except Exception as _wg_e:  # noqa: BLE001 — never block on our own error
                                log.warning(
                                    "re_embedder.merge_weld_preflight_failed "
                                    f"conflict_id={conflict_id} error={str(_wg_e)[:200]} "
                                    "note=failing OPEN, merge proceeds as before"
                                )
                                _weld_block = None
                            if _weld_block is not None:
                                # `log` in this module is a STDLIB logger, so the structlog
                                # kwargs shape of logging_config.log_crit would raise —
                                # _doc_log_crit renders the same CRITICAL key=value line.
                                _doc_log_crit(
                                    "re_embedder.merge_refused_inadmissible_weld",
                                    conflict_id=conflict_id,
                                    winner=str(winner_id)[:16], loser=str(loser_id)[:16],
                                    alias=str(_weld_block[0])[:64],
                                    preference_source=_weld_block[1],
                                    note="an_LLM_arbitrated_merge_would_have_welded_a_surface_"
                                         "onto_an_entity_it_does_not_denote__nothing_moved_"
                                         "nothing_deleted",
                                )
                                raise _MergeRefused(_weld_block[0])

                            # Step 1: Repoint facts from loser to winner (subject_id)
                            # Handle UNIQUE constraint (subject_id, object_id, rel_type):
                            # delete loser rows that would conflict, then update the rest.
                            _mcur.execute(
                                "DELETE FROM facts WHERE subject_id = %s "
                                "AND EXISTS ("
                                "  SELECT 1 FROM facts f2 "
                                "  WHERE f2.subject_id = %s "
                                "  AND f2.object_id = facts.object_id "
                                "  AND f2.rel_type = facts.rel_type"
                                ")",
                                (loser_id, winner_id),
                            )
                            _mcur.execute(
                                "UPDATE facts SET subject_id = %s "
                                "WHERE subject_id = %s",
                                (winner_id, loser_id),
                            )
                            _repointed_subj = _mcur.rowcount

                            # Step 2: Repoint facts from loser to winner (object_id)
                            _mcur.execute(
                                "DELETE FROM facts WHERE object_id = %s "
                                "AND EXISTS ("
                                "  SELECT 1 FROM facts f2 "
                                "  WHERE f2.object_id = %s "
                                "  AND f2.subject_id = facts.subject_id "
                                "  AND f2.rel_type = facts.rel_type"
                                ")",
                                (loser_id, winner_id),
                            )
                            _mcur.execute(
                                "UPDATE facts SET object_id = %s "
                                "WHERE object_id = %s",
                                (winner_id, loser_id),
                            )
                            _repointed_obj = _mcur.rowcount

                            # Step 3: Repoint staged_facts (same pattern)
                            _mcur.execute(
                                "DELETE FROM staged_facts WHERE subject_id = %s "
                                "AND EXISTS ("
                                "  SELECT 1 FROM staged_facts sf2 "
                                "  WHERE sf2.subject_id = %s "
                                "  AND sf2.object_id = staged_facts.object_id "
                                "  AND sf2.rel_type = staged_facts.rel_type"
                                ")",
                                (loser_id, winner_id),
                            )
                            _mcur.execute(
                                "UPDATE staged_facts SET subject_id = %s "
                                "WHERE subject_id = %s",
                                (winner_id, loser_id),
                            )
                            _mcur.execute(
                                "DELETE FROM staged_facts WHERE object_id = %s "
                                "AND EXISTS ("
                                "  SELECT 1 FROM staged_facts sf2 "
                                "  WHERE sf2.object_id = %s "
                                "  AND sf2.subject_id = staged_facts.subject_id "
                                "  AND sf2.rel_type = staged_facts.rel_type"
                                ")",
                                (loser_id, winner_id),
                            )
                            _mcur.execute(
                                "UPDATE staged_facts SET object_id = %s "
                                "WHERE object_id = %s",
                                (winner_id, loser_id),
                            )

                            # Step 4: Move entity_attributes from loser to winner
                            # Skip attributes that already exist on the winner
                            _mcur.execute(
                                "UPDATE entity_attributes SET entity_id = %s "
                                "WHERE entity_id = %s "
                                "AND NOT EXISTS ("
                                "  SELECT 1 FROM entity_attributes ea2 "
                                "  WHERE ea2.entity_id = %s "
                                "  AND ea2.attribute = entity_attributes.attribute"
                                ")",
                                (winner_id, loser_id, winner_id),
                            )
                            # Delete remaining loser attributes (conflicting keys kept on winner)
                            _mcur.execute(
                                "DELETE FROM entity_attributes WHERE entity_id = %s",
                                (loser_id,),
                            )

                            # Step 5: Move aliases from loser to winner (dead-name safe).
                            #
                            # ALIAS-PROVENANCE-DESIGN: decouple "which UUID survives"
                            # (structural, decided above by fact density) from "which
                            # alias is preferred." Moved aliases keep their ORIGINAL
                            # preference_source — we do NOT blanket-force them. Only
                            # the single is_preferred flag is recomputed across the
                            # UNION of both entities' aliases by preference_rank, so a
                            # user_stated chosen name always beats a rel_default/legal/
                            # dead name regardless of which entity "won." All alias rows
                            # are preserved (non-destructive); only duplicate rows that
                            # already exist on the winner are deduped.
                            #
                            # Move loser aliases (preserving preference_source). Clear
                            # is_preferred on move to avoid a transient two-preferred
                            # state; the correct single preferred is set below.
                            _mcur.execute(
                                "UPDATE entity_aliases SET entity_id = %s, is_preferred = false "
                                "WHERE entity_id = %s "
                                "AND alias NOT IN ("
                                "  SELECT alias FROM entity_aliases WHERE entity_id = %s"
                                ")",
                                (winner_id, loser_id, winner_id),
                            )
                            # Delete remaining loser aliases (already exist on winner — dedup, not history loss)
                            _mcur.execute(
                                "DELETE FROM entity_aliases WHERE entity_id = %s",
                                (loser_id,),
                            )

                            # Recompute the single preferred alias across the winner's
                            # now-unified alias set. Highest preference_rank wins; ties
                            # break by recency (valid_from, then created_at). An alias
                            # whose source rank is below the winner is never promoted.
                            _mcur.execute(
                                "SELECT alias, preference_source FROM entity_aliases "
                                "WHERE entity_id = %s",
                                (winner_id,),
                            )
                            _winner_aliases = _mcur.fetchall()
                            if _winner_aliases:
                                # Refetch with recency columns so we can break rank ties.
                                _mcur.execute(
                                    "SELECT alias, preference_source, valid_from, created_at "
                                    "FROM entity_aliases WHERE entity_id = %s",
                                    (winner_id,),
                                )
                                _rows = _mcur.fetchall()

                                def _sort_key(r):
                                    _alias, _src, _vf, _ca = r
                                    return (
                                        preference_rank(_src),
                                        _vf or _ca,  # recency tie-break
                                    )

                                # Highest rank, then most recent.
                                _best = max(_rows, key=_sort_key)
                                _best_alias = _best[0]
                                _best_src = _best[1]

                                # Clear all, then set exactly one preferred.
                                _mcur.execute(
                                    "UPDATE entity_aliases SET is_preferred = false "
                                    "WHERE entity_id = %s",
                                    (winner_id,),
                                )
                                # If the chosen alias was only ever non-preferred and has
                                # no better source available, it is preferred purely by the
                                # merge structure → record provenance as 'merge'. Otherwise
                                # keep its original source (e.g. a user_stated name stays
                                # user_stated). rel_default/inferred/etc. keep their source.
                                if preference_rank(_best_src) <= preference_rank('merge'):
                                    _mcur.execute(
                                        "UPDATE entity_aliases "
                                        "SET is_preferred = true, preference_source = 'merge' "
                                        "WHERE entity_id = %s AND alias = %s",
                                        (winner_id, _best_alias),
                                    )
                                    _final_src = 'merge'
                                else:
                                    _mcur.execute(
                                        "UPDATE entity_aliases SET is_preferred = true "
                                        "WHERE entity_id = %s AND alias = %s",
                                        (winner_id, _best_alias),
                                    )
                                    _final_src = _best_src
                                log.info(
                                    f"re_embedder.merge_preferred_recomputed "
                                    f"winner={str(winner_id)[:16]} "
                                    f"preferred_alias={_best_alias} "
                                    f"preference_source={_final_src}"
                                )

                            # Step 6: Delete the loser entity record
                            _mcur.execute(
                                "DELETE FROM entities WHERE id = %s",
                                (loser_id,),
                            )

                            # Step 7: Mark merged facts for Qdrant re-sync
                            _mcur.execute(
                                "UPDATE facts SET qdrant_synced = false "
                                "WHERE subject_id = %s OR object_id = %s",
                                (winner_id, winner_id),
                            )

                        db_conn.commit()
                        log.info(
                            f"re_embedder.entity_merge_complete "
                            f"winner={str(winner_id)[:16]} "
                            f"loser={str(loser_id)[:16]} "
                            f"repointed_facts={_repointed_subj + _repointed_obj}"
                        )

                    except _MergeRefused as _refused:
                        # NOT a failure — a deliberate refusal by the arrival weld guard.
                        # Caught before the generic handler so it is never reported as
                        # `entity_merge_failed`; the log_crit at the raise site carries the
                        # detail. The rollback undoes steps 1-4, so the merge is all-or-nothing
                        # and both entities are left exactly as they were.
                        try:
                            db_conn.rollback()
                        except Exception:  # noqa: BLE001
                            pass
                        log.warning(
                            f"re_embedder.entity_merge_declined "
                            f"conflict_id={conflict_id} "
                            f"winner={str(winner_id)[:16]} "
                            f"loser={str(loser_id)[:16]} "
                            f"alias={str(_refused)[:64]} "
                            f"note=inadmissible_weld_nothing_moved"
                        )

                    except Exception as _merge_err:
                        # Merge failed — alias resolution already committed above.
                        # Rollback the failed merge transaction and continue.
                        try:
                            db_conn.rollback()
                        except Exception:
                            pass
                        log.error(
                            f"re_embedder.entity_merge_failed "
                            f"conflict_id={conflict_id} "
                            f"winner={str(winner_id)[:16]} "
                            f"loser={str(loser_id)[:16]} "
                            f"error={str(_merge_err)}"
                        )

                except Exception as e:
                    db_conn.rollback()
                    log.error(
                        f"re_embedder.name_conflict_update_failed "
                        f"conflict_id={conflict_id} "
                        f"error={str(e)}"
                    )
                    stats["errors"] += 1
                    continue

            except Exception as e:
                # Error isolation: don't crash re-embedder if one conflict fails
                log.error(
                    f"re_embedder.name_conflict_processing_failed "
                    f"conflict_id={conflict_id} "
                    f"error={str(e)}"
                )
                stats["errors"] += 1

        if stats["resolved"] > 0 or stats["errors"] > 0:
            log.info(
                f"re_embedder.name_conflict_resolution_complete "
                f"resolved={stats['resolved']} "
                f"errors={stats['errors']} "
                f"skipped={stats['skipped']}"
            )

    except Exception as e:
        log.error(f"re_embedder.name_conflict_resolution_failed (non-fatal): {e}")
        stats["errors"] += 1

    return stats


def detect_embedding_model_change() -> None:
    """Auto-detect if embedding model version changed; clear cache if so.

    dprompt-121: On startup, compare model version to stored version.
    If mismatch, clear embedding cache (v1.5→v2.0 embeddings incomparable).
    """
    if not _embedding_cache.client:
        return

    # PURE CONFIG — version tag from env (no code literal); default lives in .env.example.
    # Empty when unset → the cache version-guard simply no-ops (never crashes the cache path).
    embedding_model_version = (os.getenv("EMBEDDING_MODEL_VERSION") or "").strip()
    if not embedding_model_version:
        return
    try:
        stored_version = _embedding_cache.client.get("_embedding_model_version")
        if stored_version and stored_version != embedding_model_version:
            log.warning(
                "embedding_cache.model_version_changed "
                f"old={stored_version} new={embedding_model_version}"
            )
            deleted = _embedding_cache.clear_pattern(f"{_embedding_cache.prefix}*")
            log.info(f"embedding_cache.cleared_model_change entries_deleted={deleted}")

        # Store current model version
        _embedding_cache.client.set("_embedding_model_version", embedding_model_version)
    except Exception as e:
        log.warning(f"embedding_cache.model_detection_failed error={str(e)}")


def _get_redis_client(redis_url: str = None) -> Optional[redis.Redis]:
    """Get or initialize Redis client for queue operations.

    Args:
        redis_url: Optional explicit Redis URL (auto-detected if not provided)

    Returns:
        Redis client or None if connection fails
    """
    try:
        url = redis_url or os.getenv("REDIS_URL") or _detect_redis_endpoint()
        client = redis.from_url(url, decode_responses=True, socket_timeout=5)
        client.ping()
        return client
    except Exception as e:
        log.warning(f"redis_client_initialization_failed: {e}")
        return None


def consume_reembedder_queue(db_conn, redis_client: redis.Redis, qwen_api_url: str) -> int:
    """
    Consume events from Redis queue and process them.
    Processes high-priority class_c queue first, then per-user queues.
    Non-blocking: if queue empty, returns immediately.

    Args:
        db_conn: PostgreSQL connection
        redis_client: Redis client
        qwen_api_url: LLM endpoint URL

    Returns:
        Number of events processed
    """
    if not redis_client:
        return 0

    processed = 0

    try:
        # Step 1: Check high-priority class_c_ingest queue (blocking pop with timeout)
        try:
            event_json = redis_client.blpop("faultline:queue:class_c", timeout=1)
            if event_json:
                event_data = event_json[1]  # blpop returns (key, value)
                if process_reembedder_event(db_conn, redis_client, qwen_api_url, event_data):
                    processed += 1
                # Continue to next iteration to check for more events
                return processed + consume_reembedder_queue(db_conn, redis_client, qwen_api_url)
        except Exception as e:
            log.debug(f"queue_consumer.class_c_pop_error: {e}")

        # Step 2: Check a few per-user queues (non-blocking)
        # In production, this would enumerate active users, here we try a few
        try:
            # Use KEYS to find active user queues (limited scan)
            cursor = 0
            for _ in range(5):  # Check up to 5 queues per iteration
                cursor, keys = redis_client.scan(cursor, match="faultline:queue:*", count=10)

                for key in keys:
                    if key == "faultline:queue:class_c":
                        continue  # Already handled above

                    try:
                        event_json = redis_client.lpop(key)
                        if event_json:
                            if process_reembedder_event(db_conn, redis_client, qwen_api_url, event_json):
                                processed += 1
                    except Exception as e:
                        log.debug(f"queue_consumer.user_queue_error key={key}: {e}")

                if cursor == 0:
                    break  # Scan complete
        except Exception as e:
            log.debug(f"queue_consumer.scan_error: {e}")

    except Exception as e:
        log.error(f"queue_consumer.error: {e}")

    return processed


def process_reembedder_event(
    db_conn,
    redis_client: redis.Redis,
    qwen_api_url: str,
    event_json: str
) -> bool:
    """
    Process a single re-embedder event from Redis.

    CRITICAL: Validates user_id before any operation.

    Args:
        db_conn: PostgreSQL connection
        redis_client: Redis client
        qwen_api_url: LLM endpoint URL
        event_json: JSON string from Redis

    Returns:
        True if processed successfully, False otherwise
    """
    try:
        event = json.loads(event_json)
    except Exception as e:
        log.error(f"reembedder_event.json_parse_error: {e}")
        return False

    event_type = event.get("event_type")
    user_id = event.get("user_id")

    # CRITICAL: Validate user_id before any DB operation
    if not user_id or not isinstance(user_id, str) or len(user_id) < 4:
        log.error(f"reembedder_event.invalid_user_id event_type={event_type} user_id_len={len(user_id or '')}")
        return False

    try:
        if event_type == "class_c_ingest":
            rel_type = event.get("rel_type", "").lower().strip()
            confidence = event.get("confidence", 0.4)

            if rel_type and len(rel_type) > 0:
                # Evaluate novel rel_type for ontology learning
                with db_conn.cursor() as cur:
                    # Check if rel_type is already known
                    cur.execute(
                        "SELECT rel_type FROM rel_types WHERE rel_type = %s LIMIT 1",
                        (rel_type,)
                    )
                    _rel_type_known = cur.fetchone()
                # CONNECTION HYGIENE (idle-in-transaction leak fix): this branch is
                # READ-ONLY, but `db_conn` is the LONG-LIVED poll-loop connection
                # (autocommit OFF), so the SELECT above opens a transaction and holds
                # ACCESS SHARE on `rel_types`. With no commit/rollback, that transaction
                # lingered idle-in-transaction for the REMAINDER of the poll cycle's
                # `with ... as db:` block — through the cycle's slow (LLM) ontology work
                # — and its lock blocked a concurrent per-tenant `reset_tenant`
                # `TRUNCATE ... CASCADE` (observed during LME: pid holding
                # `SELECT rel_type FROM rel_types WHERE rel_type = '<novel>' LIMIT 1`
                # idle-in-transaction, cascading the run into infra errors). Roll back so
                # the read's implicit transaction is released the instant the event is
                # processed. Nothing is written here → rollback is behaviourally identical
                # to the prior code, minus the leaked transaction. Writers on the
                # negation/correction branches already commit/rollback internally, so this
                # closes the last uncommitted path in this function.
                try:
                    db_conn.rollback()
                except Exception:  # noqa: BLE001 — hygiene rollback is best-effort
                    pass
                if not _rel_type_known:
                    # Novel rel_type — log for re_embedder evaluation
                    log.debug(f"reembedder_event_processed event_type=class_c_ingest "
                             f"rel_type={rel_type} confidence={confidence} user_id={user_id[:8]}")
                    return True
            return True

        elif event_type == "negation_pattern_novel":
            pattern_hash = event.get("pattern_hash")
            confidence = event.get("confidence", 0.4)

            if pattern_hash:
                # Learn negation pattern by hash
                learned = learn_negation_pattern_by_hash(db_conn, user_id, pattern_hash, confidence)
                if learned:
                    log.debug(f"reembedder_event_processed event_type=negation_pattern_novel "
                             f"pattern_hash={pattern_hash} confidence={confidence} user_id={user_id[:8]}")
                return True

        elif event_type == "correction_feedback":
            confidence_bin = event.get("confidence_bin")
            feedback_type = event.get("feedback_type", "correction")

            if confidence_bin:
                # Record correction feedback only. Gate adjustment is consolidated into
                # the single bounded writer in the re_embedder poll loop (bin-reliability
                # algorithm, clamped to [GATE_MIN, GATE_MAX]); the old per-event
                # adjust_confidence_gate writer was removed to stop two writers fighting
                # over the per-tenant confidence_gates row each cycle.
                recorded = record_confidence_feedback(db_conn, user_id, confidence_bin, feedback_type)
                if recorded:
                    log.debug(f"reembedder_event_processed event_type=correction_feedback "
                             f"confidence_bin={confidence_bin} feedback_type={feedback_type} "
                             f"user_id={user_id[:8]}")
                return True

        else:
            log.warning(f"reembedder_event.unknown_type event_type={event_type} user_id={user_id[:8]}")
            return False

    except Exception as e:
        log.error(f"reembedder_event_failed event_type={event_type} "
                 f"user_id_prefix={user_id[:8] if user_id else 'none'} error={str(e)}")
        return False


def learn_negation_pattern_by_hash(
    db_conn,
    user_id: str,
    pattern_hash: str,
    confidence: float = 0.4
) -> bool:
    """
    Learn a negation pattern based on hash match.
    Pattern hash is used to identify patterns without storing raw text in Redis.
    This function logs the pattern learning for re-embedder evaluation.

    Args:
        db_conn: PostgreSQL connection
        user_id: User UUID
        pattern_hash: SHA256[:16] hash of pattern (from Redis event)
        confidence: Starting confidence (typically 0.4)

    Returns:
        True if pattern was learned/updated, False otherwise
    """
    try:
        with db_conn.cursor() as cur:
            # Query for existing pattern by hash (if pattern_hash column exists in DB)
            # Otherwise, this is logged for future evaluation by re_embedder

            # For now, just track the hash as a generic pattern
            # In future, this would match against actual pattern_text via hash comparison
            # Per-user schema: no user_id filter needed — schema provides isolation
            cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                """
                SELECT id FROM negation_patterns
                WHERE pattern_hash IS NOT NULL
                  AND pattern_hash = %s
                LIMIT 1
                """,
                (pattern_hash,)
            )

            existing = cur.fetchone()
            if existing:
                # Pattern already exists — increment confirmed count
                cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                    """
                    UPDATE negation_patterns
                    SET confirmed_count = confirmed_count + 1,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (existing[0],)
                )
                log.debug(f"negation_pattern_confirmed user_id={user_id[:8]} pattern_hash={pattern_hash}")
            else:
                # New pattern — log it as a candidate for future learning
                # The pattern_text field will be populated by the re-embedder when it
                # reconstructs the full pattern from context (if available)
                # Per-user schema: no user_id column needed
                cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                    """
                    INSERT INTO negation_patterns
                    (pattern_text, pattern_hash, negation_type, confidence, confirmed_count, learned_from)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (pattern_text, negation_type) DO UPDATE
                    SET confirmed_count = negation_patterns.confirmed_count + 1,
                        pattern_hash = COALESCE(EXCLUDED.pattern_hash, negation_patterns.pattern_hash),
                        updated_at = now()
                    """,
                    (f"hash_{pattern_hash}", pattern_hash, "retraction", confidence, 1, "re_embedder_inferred")
                )
                log.debug(f"negation_pattern_learned user_id={user_id[:8]} pattern_hash={pattern_hash}")

        db_conn.commit()
        return True

    except Exception as e:
        log.error(f"learn_negation_pattern_error user_id={user_id[:8]} pattern_hash={pattern_hash}: {e}")
        db_conn.rollback()
        return False


def record_confidence_feedback(
    db_conn,
    user_id: str,
    confidence_bin: str,
    feedback_type: str = "correction"
) -> bool:
    """
    Record confidence feedback for gate adjustment.
    Updates the PER-TENANT intent_confidence_feedback table.

    PER-TENANT: feedback is the user's own signal and lives in the user's own schema.
    We bind the tenant search_path from user_id before the UNQUALIFIED INSERT so it
    lands in <schema>.intent_confidence_feedback (NO public). public is template-only.

    Args:
        db_conn: PostgreSQL connection
        user_id: User UUID
        confidence_bin: Bin like "0.65-0.75"
        feedback_type: "correction" or "confirmation"

    Returns:
        True if recorded, False on error
    """
    try:
        from src.provisioning.schema_manager import derive_user_slug_from_uuid as _dslug
        _fb_schema = f"faultline_{_dslug(user_id)}"
        with db_conn.cursor() as cur:
            cur.execute(f"SET search_path TO {_fb_schema}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema from UUID-derived source with validation
            cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                """
                INSERT INTO intent_confidence_feedback
                (user_id, confidence_bin, feedback_type, count)
                VALUES (%s, %s, %s, 1)
                ON CONFLICT (user_id, confidence_bin, feedback_type)
                DO UPDATE SET count = intent_confidence_feedback.count + 1,
                              created_at = now()
                """,
                (user_id, confidence_bin, feedback_type)
            )
        db_conn.commit()
        return True

    except Exception as e:
        log.error(f"record_confidence_feedback_error user_id={user_id[:8]} "
                 f"confidence_bin={confidence_bin}: {e}")
        db_conn.rollback()
        return False


    # NOTE: adjust_confidence_gate (the old "Writer A" — two hardcoded band rules
    # writing only 0.65/0.70/0.75) was DELETED. It competed with the bin-reliability
    # writer in the poll loop over the per-tenant confidence_gates row each cycle. The
    # single bounded writer (clamped to [GATE_MIN, GATE_MAX]) is now the sole gate writer.


def evaluate_extraction_patterns(db_conn) -> dict:
    """
    Job 6: Evaluate extraction pattern accuracy and bootstrap confidence scores.

    Steps:
    1. Query extraction_pattern_matches for user feedback (confirmed/rejected)
    2. Calculate accuracy for each pattern: confirmed / (confirmed + rejected)
    3. Archive underperforming patterns (accuracy < 0.3)
    4. Promote high-confidence patterns (confirmed_count >= 3)
    5. Update global_confidence scores in extraction_patterns table
    6. Log all decisions for monitoring

    Returns: {
        "evaluated": int,
        "archived": int,
        "promoted": int,
        "confidence_updates": int,
        "errors": int
    }
    """
    stats = {
        "evaluated": 0,
        "archived": 0,
        "promoted": 0,
        "confidence_updates": 0,
        "errors": 0,
    }

    try:
        with db_conn.cursor() as cur:
            # NO-OP CHURN TRIM (deterministic, fail-safe): every decision below — archive,
            # confidence-update, promote — REQUIRES accumulated feedback (confirmed_count /
            # rejected_count > 0). A freshly-seeded pattern with zero feedback can NEVER trigger
            # any decision, yet the old unfiltered sweep re-evaluated all ~64 seeded patterns
            # PER TENANT EVERY cycle (incl. a per-pattern `SELECT engine_generated`), logging
            # evaluated=64/archived=0/promoted=0 churn against a DB busy provisioning. So fetch
            # ONLY patterns that carry feedback ("any candidates?" precheck) — a pattern becomes
            # a candidate the moment a match is confirmed/rejected, so no real promotion is ever
            # skipped (correction_count is included as a belt-and-suspenders superset).
            cur.execute("""
                SELECT
                    ep.id,
                    ep.pattern_regex,
                    ep.rel_type,
                    COALESCE(ep.confirmed_count, 0) as confirmed_count,
                    COALESCE(ep.rejected_count, 0) as rejected_count,
                    COALESCE(ep.correction_count, 0) as correction_count,
                    ep.global_confidence,
                    ep.frequency
                FROM extraction_patterns ep
                WHERE ep.is_active = true
                  AND (COALESCE(ep.confirmed_count, 0) > 0
                       OR COALESCE(ep.rejected_count, 0) > 0
                       OR COALESCE(ep.correction_count, 0) > 0)
                ORDER BY ep.frequency DESC, ep.global_confidence DESC
            """)
            patterns = cur.fetchall()

    except Exception as e:
        log.error(f"re_embedder.extraction_pattern_fetch_failed: {e}")
        stats["errors"] += 1
        return stats

    if not patterns:
        # No pattern carries feedback yet → nothing any decision could act on. Skip the whole
        # sweep (no per-pattern queries, no eval_complete churn) until feedback accrues.
        log.debug("re_embedder.extraction_pattern_eval no_candidates_to_evaluate")
        return stats

    log.info(f"re_embedder.extraction_pattern_eval_start count={len(patterns)}")

    for pattern_row in patterns:
        pattern_id, pattern_regex, rel_type, confirmed, rejected, corrections, confidence, frequency = pattern_row
        stats["evaluated"] += 1

        try:
            # Calculate accuracy
            total_feedback = confirmed + rejected
            accuracy = confirmed / total_feedback if total_feedback > 0 else 0.0

            # Decision 1: Archive underperforming patterns
            if accuracy < 0.3 and total_feedback >= 3:
                with db_conn.cursor() as cur:
                    cur.execute("""
                        UPDATE extraction_patterns
                        SET is_active = false, archived_at = NOW()
                        WHERE id = %s
                    """, (pattern_id,))
                stats["archived"] += 1
                log.info(
                    f"re_embedder.extraction_pattern_archived "
                    f"pattern_id={pattern_id} rel_type={rel_type} "
                    f"accuracy={accuracy:.2f} confirmed={confirmed} rejected={rejected}"
                )
                continue

            # Decision 2: Update confidence based on accuracy
            new_confidence = confidence
            if accuracy >= 0.85:
                new_confidence = 0.90
            elif accuracy >= 0.70:
                new_confidence = 0.80
            elif accuracy < 0.50 and total_feedback >= 3:
                new_confidence = 0.50

            if new_confidence != confidence:
                with db_conn.cursor() as cur:
                    cur.execute("""
                        UPDATE extraction_patterns
                        SET global_confidence = %s, updated_at = NOW()
                        WHERE id = %s
                    """, (new_confidence, pattern_id))
                stats["confidence_updates"] += 1
                log.info(
                    f"re_embedder.extraction_pattern_confidence_updated "
                    f"pattern_id={pattern_id} rel_type={rel_type} "
                    f"old={confidence:.2f} new={new_confidence:.2f}"
                )

            # Decision 3: Promote new patterns after sufficient confirmation
            # Metadata-driven: only boost engine_generated (LLM-discovered) rel_types,
            # not system-defined ones (which already have validated Class A/B assignment)
            with db_conn.cursor() as cur:
                cur.execute(
                    "SELECT engine_generated FROM rel_types WHERE rel_type = %s LIMIT 1",
                    (rel_type,)
                )
                rt_row = cur.fetchone()
            is_novel_rel_type = rt_row is None or rt_row[0]  # Novel if missing or engine_generated=true

            if confirmed >= 3 and is_novel_rel_type:
                # Novel patterns (not in original hardcoded list)
                with db_conn.cursor() as cur:
                    cur.execute("""
                        UPDATE extraction_patterns
                        SET global_confidence = 0.80, updated_at = NOW()
                        WHERE id = %s AND global_confidence < 0.80
                    """, (pattern_id,))
                    if cur.rowcount > 0:
                        stats["promoted"] += 1
                        log.info(
                            f"re_embedder.extraction_pattern_promoted "
                            f"pattern_id={pattern_id} rel_type={rel_type} "
                            f"confirmed={confirmed}"
                        )

        except Exception as e:
            log.error(
                f"re_embedder.extraction_pattern_eval_error "
                f"pattern_id={pattern_id}: {e}"
            )
            stats["errors"] += 1

    # Commit all changes
    try:
        db_conn.commit()
        log.info(
            f"re_embedder.extraction_pattern_eval_complete "
            f"evaluated={stats['evaluated']} "
            f"archived={stats['archived']} "
            f"promoted={stats['promoted']} "
            f"confidence_updates={stats['confidence_updates']} "
            f"errors={stats['errors']}"
        )
    except Exception as e:
        log.error(f"re_embedder.extraction_pattern_eval_commit_failed: {e}")
        db_conn.rollback()
        stats["errors"] += 1

    return stats


def _sweep_inverted_staged_hierarchy_rows(postgres_dsn: str, qdrant_url: str) -> int:
    """One-time startup sweep: delete pre-existing inverted staged_facts hierarchy rows.

    These are rows where the subject_id is a generic ontology token
    (person, organization, location, etc.) rather than a real entity UUID.
    They were created by mis-extracted triples with subject/object swapped, e.g.:
        person instance_of alexander
        organization instance_of company

    Runs once per re_embedder startup — not every poll cycle.  The function is
    idempotent; running it a second time with no matching rows is safe.

    SQL uses aliases lookup to find entity IDs whose canonical alias is an ontology
    token — these are the UUID-form subject_ids stored in staged_facts.

    Returns total number of rows deleted across all user schemas.
    """
    _SYSTEM_ALIASES = (
        "person", "organization", "animal", "location",
        "concept", "object", "thing", "entity",
    )
    total_deleted = 0

    try:
        with psycopg2.connect(postgres_dsn) as admin_db:
            with admin_db.cursor() as cur:
                cur.execute("""
                    SELECT user_id, schema_name FROM public.user_provisioning
                    WHERE status = 'ready'
                    ORDER BY ready_at ASC
                """)
                ready_schemas = [(row[0], row[1]) for row in cur.fetchall()]
    except Exception as e:
        log.error(f"re_embedder.inverted_sweep.schema_list_failed error={e}")
        return 0

    for user_id, schema_name in ready_schemas:
        try:
            with psycopg2.connect(postgres_dsn) as db:
                with db.cursor() as cur:
                    cur.execute(f"SET search_path TO {schema_name}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema from UUID-derived source with validation
                db.commit()

                with db.cursor() as cur:
                    cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                        """
                        DELETE FROM staged_facts
                        WHERE rel_type IN ('instance_of', 'subclass_of', 'part_of', 'is_a', 'member_of')
                          AND subject_id IN (
                              SELECT ea.entity_id FROM entity_aliases ea
                              WHERE ea.alias = ANY(%s)
                          )
                          AND promoted_at IS NULL
                        RETURNING id
                        """,
                        # psycopg2 adapts a Python list as a PostgreSQL ARRAY[...] (what
                        # ANY() needs); a tuple adapts as a row constructor ('a','b',...)
                        # → "op ANY/ALL (array) requires array on right side". Pass a list.
                        (list(_SYSTEM_ALIASES),),
                    )
                    deleted_ids = [r[0] for r in cur.fetchall()]
                db.commit()

                if deleted_ids:
                    log.info(
                        f"re_embedder.inverted_sweep.deleted "
                        f"user_id={user_id[:8]} schema={schema_name} count={len(deleted_ids)}"
                    )
                    total_deleted += len(deleted_ids)

                    # Best-effort Qdrant cleanup for deleted rows (partition choke-point).
                    try:
                        from src.api.qdrant_partition import (
                            resolve_partition as _qp_resolve, require_tenant as _qp_require,
                            build_delete_body as _qp_delbody, resolve_point_id as _qp_pid,
                            qdrant_headers as _qp_hdr,
                        )
                        collection, _tflt = _qp_resolve(user_id, "memory")
                        _qp_require(_tflt, op="delete", collection=collection)
                        for _did in deleted_ids:
                            _http_client.post(
                                f"{qdrant_url}/collections/{collection}/points/delete",
                                json=_qp_delbody(_tflt, must=[
                                    {"key": "source_table", "match": {"value": "staged_facts"}},
                                    {"key": "fact_id",     "match": {"value": _did}},
                                ]),
                                headers=_qp_hdr(),
                                timeout=5.0,
                            )
                        # Fallback by derived point ID (new-scheme points). The filtered
                        # delete above already covers payload-tagged points; this removes
                        # the deterministic (source_table, fact_id) point even if its
                        # source_table payload were somehow absent. Legacy bare-int points
                        # are handled by reconcile / collection re-sync.
                        # POST /points/delete with a points-selector body; httpx .delete()
                        # takes no body kwarg (json/content) and would raise.
                        _http_client.post(
                            f"{qdrant_url}/collections/{collection}/points/delete",
                            json=_qp_delbody(_tflt, point_ids=[
                                _qp_pid(user_id, "staged_facts", _did, "memory") for _did in deleted_ids
                            ]),
                            headers=_qp_hdr(),
                            timeout=5.0,
                        )
                    except Exception as qe:
                        log.warning(
                            f"re_embedder.inverted_sweep.qdrant_failed "
                            f"user_id={user_id[:8]} error={qe}"
                        )

        except Exception as e:
            log.error(
                f"re_embedder.inverted_sweep.per_user_failed "
                f"user_id={user_id[:8] if user_id else 'unknown'} schema={schema_name} error={e}"
            )

    if total_deleted:
        log.info(f"re_embedder.inverted_sweep.complete total_deleted={total_deleted}")
    else:
        log.info("re_embedder.inverted_sweep.complete total_deleted=0 (nothing to clean)")

    return total_deleted


def _standalone_doc_drain_loop(postgres_dsn: str, backend_api_url: str):
    """DEDICATED fast doc-drain worker — decouples document latency from the heavy
    per-tenant maintenance cycle (LATENCY + SCALE fix).

    The main re_embedder cycle walks EVERY ready tenant's heavy ontology work
    (enrichment, synonym convergence, whatis-climb, promotion, expiry, reconcile)
    before the top-of-cycle doc pre-pass comes around again. With many tenants — or a
    single slow/failing brain anywhere in the walk — a freshly enqueued document waits
    a FULL cycle (measured multi-minute). But documents are LATENCY-SENSITIVE (a caller
    blocks on READY) while ontology maintenance is BACKGROUND. This thread runs the SAME
    fast pre-pass (``fast_drain_pending_documents_prepass`` — atomic claim, per-tenant
    fail-safe, tenant-brain-bound) on its OWN tight loop, so document draining NEVER
    queues behind unrelated per-tenant work or 999 other seats. It is the first concrete
    step of the per-seat-scaled worker fan-out (see design-scaling-multitenancy).

    Claim safety: the drain's UPDATE…WHERE status='pending' RETURNING is atomic, so this
    thread and the in-order walk can never double-process a doc — whoever claims first
    wins; the loser's pending probe returns nothing. Gated by DOC_DRAIN_STANDALONE
    (default on); tick = DOC_DRAIN_STANDALONE_INTERVAL (default 4s). Re-reads the
    INGEST_ENABLED freeze every tick. Never raises — a failure logs, sleeps, and retries.
    """
    poll = float(os.getenv("DOC_DRAIN_STANDALONE_INTERVAL", "4"))
    log.info(f"re_embedder.standalone_doc_drain_started interval={poll}s")
    while True:
        try:
            frozen = os.getenv("INGEST_ENABLED", "true").strip().lower() in ("false", "0", "no", "off")
            if not frozen:
                with psycopg2.connect(postgres_dsn) as _adb:
                    with _adb.cursor() as _c:
                        _c.execute(
                            "SELECT user_id, schema_name FROM public.user_provisioning "
                            "WHERE status = 'ready' ORDER BY ready_at ASC"
                        )
                        ready = [(r[0], r[1]) for r in _c.fetchall()]
                if ready:
                    fast_drain_pending_documents_prepass(
                        postgres_dsn, ready, backend_api_url, statement_route="rewrite",
                    )
        except Exception as _dd_err:  # noqa: BLE001 — daemon must never die
            log.warning(f"re_embedder.standalone_doc_drain_error: {_dd_err}")
        time.sleep(poll)


def main():
    """Main poll loop."""
    global _http_client_sync

    # THE LANE IS A PROPERTY OF THE CALLER: this process is deferrable upkeep — no
    # user ever waits on a sweep — so it declares itself BACKGROUND once, at startup.
    # The rate gate then DEFERS this process instead of fail-opening it when capacity
    # is short, leaving the pace to interactive traffic (src/api/llm_rate.py). Genuinely
    # user-driven work inside this process wraps itself in llm_lane.use_lane(interactive).
    llm_lane.set_process_default(llm_lane.LANE_BACKGROUND)
    # Say which governance mode this process runs under — the shared-budget fallback
    # warning is the one that matters: per-process budgets multiply outbound by the
    # process count, and this deployment runs two processes.
    llm_rate.announce("re_embedder")

    # Log startup environment for debugging container issues (using extra= for structured data)
    log.info("re_embedder.startup_environment", extra={
        "has_postgres_dsn": bool(os.getenv("POSTGRES_DSN")),
        "has_qdrant_url": bool(os.getenv("QDRANT_URL")),
        "has_redis_url": bool(os.getenv("REDIS_URL")),
        "reembed_interval": os.getenv("REEMBED_INTERVAL", "60"),
        "pythonpath": os.getenv("PYTHONPATH", "not_set")
    })

    postgres_dsn = os.getenv("POSTGRES_DSN")
    qdrant_url = os.getenv("QDRANT_URL", "http://qdrant:6333")
    from src.api.llm_client import (
        get_backend_endpoint,
        get_endpoint_list as _get_llm_endpoint_list,
    )
    # Resolved ONCE at process start; every LLM-calling function below routes through the
    # centralized stack, so this survives only as the embedding-URL hint threaded through
    # `embed_text`.
    _typed_endpoint = get_backend_endpoint()
    if _typed_endpoint:
        qwen_api_url = _typed_endpoint
    else:
        _endpoints = _get_llm_endpoint_list()
        if _endpoints:
            qwen_api_url = _endpoints[0]
        else:
            qwen_api_url = "http://localhost:11434/v1/chat/completions"
    interval = int(os.getenv("REEMBED_INTERVAL", "60"))  # dprompt-121: Changed from 10 to 60
    confidence_threshold = float(os.getenv("QDRANT_SYNC_CONFIDENCE_THRESHOLD", "0.0"))

    # Episodic re-extraction backfill config. backend_api_url is the FaultLine API
    # front door (docker-compose service name `faultline`, port 8000 — the same
    # host the cache-refresh POSTs below already target).
    reextract_enabled = os.getenv("REEXTRACT_ENABLED", "true").strip().lower() not in ("false", "0", "no", "off")
    reextract_batch_size = int(os.getenv("REEXTRACT_BATCH_SIZE", "5"))
    backend_api_url = os.getenv("FAULTLINE_API_URL", "http://faultline:8000").rstrip("/")

    # INGEST_ENABLED — master freeze switch ("knowledge-store mode"). When false, ALL
    # knowledge-mutating lifecycle jobs pause: promotion, expiry/decay, episodic
    # re-extraction, ontology candidate evaluation/decay/type-constraint sweep,
    # pending-placement drain, synonym convergence, what-is classify, classify-climb,
    # rung-6 convergence, async taxonomy discovery, hierarchy reconciliation staging,
    # staged C→B rel_type upgrades, name-conflict resolution, orphan rel_type stub
    # minting, and the inverted-staged-hierarchy startup sweep.
    # KEEPS RUNNING: Qdrant embedding/upsert of already-committed unsynced rows,
    # qdrant_synced bookkeeping, and reconcile_qdrant — these converge the derived
    # index toward the frozen Postgres truth (anti-drift maintenance, not knowledge).
    # Telemetry/pattern/presentation phases also keep running (see per-phase notes).
    ingest_enabled = os.getenv("INGEST_ENABLED", "true").strip().lower() not in ("false", "0", "no", "off")

    if not postgres_dsn:
        log.error("POSTGRES_DSN not configured - re_embedder cannot start")
        log.error("re_embedder.startup_failed reason=missing_postgres_dsn")
        return

    # Initialize persistent HTTP client for pooled connections
    _http_client_sync = httpx.Client(timeout=httpx.Timeout(30.0), limits=httpx.Limits(max_connections=100, max_keepalive_connections=20))
    log.info(f"re_embedder.http_client_initialized")

    # Register LLM HTTP client cleanup so it drains on process exit (SIGTERM or KeyboardInterrupt)
    atexit.register(close_llm_http_client)

    # dprompt-121: Detect if embedding model changed (auto-clear cache if so)
    detect_embedding_model_change()

    # Dedicated fast doc-drain worker — runs the async document lane on its OWN tight
    # loop so document latency is decoupled from the heavy per-tenant maintenance cycle
    # (a doc never waits behind ontology work or other seats). Daemon; gated by
    # DOC_DRAIN_STANDALONE (default on). See _standalone_doc_drain_loop.
    if os.getenv("DOC_DRAIN_STANDALONE", "true").strip().lower() not in ("false", "0", "no", "off"):
        import threading
        threading.Thread(
            target=_standalone_doc_drain_loop, args=(postgres_dsn, backend_api_url),
            daemon=True, name="doc-drain",
        ).start()

    log.info(f"re_embedder.start interval={interval}s qdrant_url={qdrant_url} confidence_threshold={confidence_threshold} loglevel=INFO")
    log.info("re_embedder.entering_main_loop")

    # One-time startup sweep: remove pre-existing inverted staged_facts hierarchy rows
    # (e.g. "person instance_of alexander") that were created before the ingest-path
    # cleanup was in place.  Gated here so it runs once per process start, not every
    # 60-second poll cycle.
    if ingest_enabled:
        try:
            _sweep_inverted_staged_hierarchy_rows(postgres_dsn, qdrant_url)
        except Exception as _sweep_err:
            # Never block startup — sweep failure is non-fatal.
            log.warning(f"re_embedder.inverted_sweep.startup_error error={_sweep_err}")
    else:
        # Freeze: the sweep DELETEs staged_facts rows (knowledge mutation) — skipped.
        log.info("re_embedder.ingest_disabled.inverted_sweep_skipped")

    # Initialize Redis client for event queue
    redis_url = os.getenv("REDIS_URL")
    redis_client = _get_redis_client(redis_url)
    if redis_client:
        log.info("re_embedder.redis_client_initialized")
    else:
        log.warning("re_embedder.redis_client_unavailable queue_events_disabled")

    # ── Reconcile cadence (EXECUTION GATE, task §3) ──────────────────────────────────────
    # reconcile_qdrant scrolls EVERY per-tenant collection each cycle — the expensive
    # cleanup pass. It does NOT need to run every cycle for an idle rig. Bound it:
    # run when ANY tenant had work this cycle (activity-driven) OR when a coarse
    # max-interval ceiling has elapsed (so cleanup NEVER starves even on a perfectly idle
    # rig). `_last_reconcile_at` starts at 0.0 → the very first cycle always reconciles.
    # Monotonic clock so a wall-clock jump can't defer cleanup indefinitely.
    _last_reconcile_at = 0.0
    reconcile_max_interval = int(
        os.getenv("REEMBED_RECONCILE_MAX_INTERVAL", str(max(interval * 10, 3600)))
    )

    # Rotation cursor for the cycle-wide episodic-drain budget (see the block at the top of the
    # per-tenant pass). Single-element list so the cycle body can rebind it without `global`.
    # Holds the user_id of the first tenant DENIED a drain by the budget, or None for "start
    # from the top". Deliberately process-local, not persisted: a restart resuming from the top
    # is correct behaviour, and a persisted cursor would be one more thing to go stale.
    _reextract_cycle_cursor = [None]

    while True:
        try:
            # Phase 3c: Check circuit breaker health for awareness
            breaker_status = _get_circuit_breaker_status()
            if breaker_status["is_open"]:
                log.warning("re_embedder.circuit_breaker_open skipping_llm_work_this_cycle")

            # INGEST_ENABLED freeze — ONE summary line per cycle (not per user/phase):
            # all knowledge-mutating lifecycle phases below are skipped this cycle;
            # Qdrant sync + reconciliation + telemetry/pattern phases keep running.
            if not ingest_enabled:
                log.info(
                    "re_embedder.ingest_disabled.lifecycle_paused "
                    "skipped=promotion,expiry,class_c_decay,reextract_episodic,"
                    "ontology_eval,ontology_decay,head_tail_sweep,pending_placement_drain,"
                    "synonym_convergence,whatis_classify,classify_climb,rung6_convergence,"
                    "taxonomy_discovery,hierarchy_reconcile,staged_rel_upgrade,"
                    "name_conflicts,orphan_rel_stub_mint "
                    "running=qdrant_sync,reconcile_qdrant,gate_adjustment,pattern_jobs,"
                    "natural_language_fill,cache_eviction"
                )

            # Resolve the backend brain's STATEMENT extractor route ONCE per cycle for
            # the episodic re-extraction backfill (document-lane parity: route once,
            # then per-row spine-first with /extract/rewrite fallback). Fail-safe:
            # unreachable brain → "rewrite" (the plain backfill path). Skipped when
            # frozen or backfill-disabled — no point probing a route we won't take.
            reextract_route = "rewrite"
            if ingest_enabled and reextract_enabled:
                try:
                    _route_resp = httpx.get(f"{backend_api_url}/internal/ingest-route", headers=_backend_auth_headers(), timeout=5.0)
                    _route_resp.raise_for_status()
                    _route = (_route_resp.json().get("statement_extractor") or "rewrite").strip().lower()
                    reextract_route = _route if _route in ("spine", "rewrite") else "rewrite"
                except Exception as _route_err:
                    log.debug(f"re_embedder.reextract_route_fallback (rewrite): {_route_err}")

            # At the top of every iteration, before any DB query, ensure the default
            # collection exists. This recovers a deleted collection within one loop
            # cycle regardless of whether there are any unsynced rows.
            default_collection = os.getenv("QDRANT_COLLECTION", "faultline-test")
            ensure_collection(default_collection, qdrant_url)

            # PHASE 2: Get list of all ready user schemas to process independently
            with psycopg2.connect(postgres_dsn) as admin_db:
                with admin_db.cursor() as cur:
                    cur.execute("""
                        SELECT user_id, schema_name FROM public.user_provisioning
                        WHERE status = 'ready'
                        ORDER BY ready_at ASC
                    """)
                    ready_schemas = [(row[0], row[1]) for row in cur.fetchall()]

            if ready_schemas:
                log.info(f"re_embedder.ready_schemas_found count={len(ready_schemas)}")

            # ── GHOST-TENANT HEALTH SKIP ──
            # Some tenants read status='ready' in public.user_provisioning but have had their
            # faultline_<uuid> schema DROPPED (0 tables — leftover benchmark/throwaway tenants). Every per-tenant pass below would throw UndefinedTable on such a
            # schema → aborted txn → on any pass that shares a connection across tenants, the
            # "current transaction is aborted, commands ignored" cascade that STALLS Class-B
            # promotion + Class-C sync for HEALTHY tenants. Deterministically drop ghosts from
            # ready_schemas ONCE per cycle — a SINGLE structural chokepoint feeding ALL the
            # per-tenant loops below (every one of them iterates ready_schemas), so the invariant
            # "ghosts are never processed" holds structurally, not by each loop remembering to
            # guard. Fail-LOUD (log.error) one line per ghost + a total (the orphan public rows
            # are a DATA/ops cleanup, main-loop-owned — the CODE stays resilient to them). The
            # probe is fail-SAFE toward inclusion (any probe error → tenant kept), so a transient
            # catalog hiccup can never mass-skip live tenants. One short-lived admin connection.
            if ready_schemas:
                _live_schemas = []
                _ghost_count = 0
                try:
                    with psycopg2.connect(postgres_dsn) as _health_admin:
                        for _hu, _hs in ready_schemas:
                            if tenant_schema_is_live(_health_admin, _hs):
                                _live_schemas.append((_hu, _hs))
                            else:
                                _ghost_count += 1
                                log.error(
                                    f"re_embedder.ghost_tenant_skipped schema={_hs} "
                                    f"user_id={str(_hu)[:8]} "
                                    f"reason=schema_dropped_or_no_staged_facts_table "
                                    f"(status=ready in public.user_provisioning but schema absent "
                                    f"— orphan row; ops cleanup owed)"
                                )
                    if _ghost_count:
                        log.error(
                            f"re_embedder.ghost_tenants_skipped_total count={_ghost_count} "
                            f"live={len(_live_schemas)} — cycle continues on live tenants only"
                        )
                    ready_schemas = _live_schemas
                except Exception as _ghost_err:
                    # Fail-SAFE: if the whole health sweep fails (e.g. admin connect error), do
                    # NOT drop anyone — keep the full list and let per-tenant isolation handle it.
                    log.warning(
                        f"re_embedder.ghost_tenant_sweep_failed (processing all tenants): "
                        f"{_ghost_err}"
                    )

            # ── PER-SEAT WORK LEDGER (public.sweep_work_state, migration 207) ─────────────
            # ONE query answers "what is due, for EVERY seat", so a seat whose work tokens are
            # all clean AND inside the max-run bound is skipped BEFORE its connection is opened.
            # That is the cost model this exists for: a quiet seat costs NOTHING, instead of
            # O(seats) full sweeps per interval forever. REEMBED_SWEEP_SKIP defaults OFF; flag
            # off, table missing, or a failed query all yield the ALWAYS-RUN sentinel — exactly
            # today's behaviour. The fail-safe direction is RUN and is never inverted.
            _sweep_snap = _sweep.snapshot(postgres_dsn, [u for u, _ in ready_schemas])
            if not _sweep_snap.always_run:
                log.info(f"re_embedder.sweep_ledger.snapshot seats={_sweep_snap.seats_seen} "
                         f"seats_with_work={_sweep_snap.seats_due} "
                         f"max_interval_s={_sweep.max_interval_seconds()}")

            # Count tenants that had work this cycle (drives the reconcile cadence below).
            _active_tenants_this_cycle = 0

            # ── CYCLE-WIDE EPISODIC-DRAIN BUDGET + ROTATION CURSOR ────────────────────────
            # The per-tenant budget bounds ONE tenant; it still permits
            # `len(ready_schemas) × tenant_budget` of predecessor per cycle, and the sweeps
            # below only begin once this whole pass is done. This caps the AGGREGATE.
            #
            # ⚠️ A BUDGET WITHOUT A CURSOR IS A DIFFERENT STARVATION, NOT A FIX. `ready_schemas`
            # is `ORDER BY ready_at ASC` — a STABLE order. Spend the budget from the top every
            # cycle and the same head tenants drain forever while the tail never drains once:
            # the wedge, moved rather than removed. The cursor records the first tenant DENIED
            # this cycle and the next cycle resumes there, so the drain sweeps round-robin over
            # the seat list.
            # FAIL-SAFE DIRECTION IS RUN (sweep_ledger invariant 1): a cursor naming a tenant
            # that is no longer in `ready_schemas` would arm nothing and starve everyone, so if
            # the pass ends still un-armed the cursor is cleared LOUDLY and the next cycle
            # starts from the top.
            _rx_cycle_spent = 0.0
            _rx_cycle_denied_first = None
            _rx_cycle_denied_n = 0
            _rx_armed = (_reextract_cycle_cursor[0] is None)

            # ── PHASE 2a: FAST DOC-DRAIN PRE-PASS (flagship async doc lane) ────────────
            # LATENCY FIX: drain the async document lane at the TOP of every cycle,
            # BEFORE any heavy per-tenant pass, so a freshly-enqueued document is claimed
            # on the very next poll tick instead of waiting a full cycle behind promotion/
            # expiry/reextract/synonym work (measured >6 min live). Strict subset of the
            # in-order walk's drain (same claim/finalize SQL); per-tenant fail-safe; gated
            # on ingest_enabled (a frozen store never drains). See the helper docstring.
            if ingest_enabled and ready_schemas:
                _n_fast = fast_drain_pending_documents_prepass(
                    postgres_dsn, ready_schemas,
                    backend_api_url, statement_route=reextract_route,
                )
                if _n_fast:
                    log.info(f"re_embedder.fast_doc_drain_prepass_complete docs={_n_fast}")

            # PHASE 2b: Process each user schema independently
            for user_id, schema_name in ready_schemas:
                # Attribute this tenant's in-process LLM calls to it (see _reembedder_bind_tenant).
                _reembedder_bind_tenant(schema_name)
                try:
                    with psycopg2.connect(postgres_dsn) as db_per_user:
                        with db_per_user.cursor() as cur:
                            cur.execute(f"SET search_path TO {schema_name}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema from UUID-derived source with validation
                        db_per_user.commit()

                        # ── EXECUTION GATE (task §1/§2): skip a PROVABLY-idle tenant before
                        # any expensive lifecycle work. The gate reads real DB state (indexed
                        # EXISTS probes) and is fail-safe (probe error → True → processed), so
                        # it can only ever skip a tenant with genuinely nothing due. When it
                        # says work IS due the pass below runs byte-for-byte unchanged.
                        if not _tenant_has_reembed_work(db_per_user, schema_name):
                            log.debug(f"re_embedder.gate.skipped_idle schema={schema_name}")
                            continue
                        _active_tenants_this_cycle += 1

                        # ── READ BARRIER, SUBSYSTEM BOUNDARY (`db_per_user` lane) ──
                        # The SAME invariant the `_ont_db` lane below already enforces, and
                        # the reason this campaign exists: it was applied to ONE of the two
                        # long-lived per-tenant connections. NO SUBSYSTEM BEGINS ITS WORK
                        # INHERITING THE PREVIOUS SUBSYSTEM'S READ TRANSACTION. Each lane
                        # barriers its OWN work-set fetch; this is the belt-and-braces at the
                        # seam, so a lane that is added later and forgets its own barrier
                        # cannot poison the lane after it.
                        # release_read_transaction() and NOT rollback(): it probes
                        # pg_current_xact_id_if_assigned() and REFUSES (log_crit) on a
                        # transaction that has written, so a pending write is never discarded.
                        release_read_transaction(
                            db_per_user,
                            context=f"re_embedder.per_user.gate schema={schema_name}")

                        # INGEST_ENABLED freeze: the whole staged-fact lifecycle block
                        # (promotion, expiry, Class C hit promotion/decay, episodic
                        # re-extraction) mutates knowledge rows — paused as a unit.
                        # (Cycle-level pause already logged once above, not per user.)
                        if ingest_enabled:
                            # Promote staged facts for this user
                            release_read_transaction(
                                db_per_user,
                                context=f"re_embedder.per_user.promote schema={schema_name}")
                            n_promoted = promote_staged_facts(db_per_user, qdrant_url, user_id=user_id, schema_name=schema_name)
                            if n_promoted:
                                log.info(f"re_embedder.promotion_complete user_id={user_id[:8]} schema={schema_name} promoted={n_promoted}")

                            # Expire stale Class C facts for this user
                            release_read_transaction(
                                db_per_user,
                                context=f"re_embedder.per_user.expire schema={schema_name}")
                            n_expired = expire_staged_facts(db_per_user, qdrant_url, user_id=user_id)
                            if n_expired:
                                log.info(f"re_embedder.expiry_complete user_id={user_id[:8]} expired={n_expired}")

                            # Episodic re-extraction backfill: re-mine raw episodic_log
                            # text through the normal front door (spine /harvest-spans or
                            # /extract/rewrite → /ingest) so content the ontology couldn't
                            # cast at capture time gets another chance after the per-tenant
                            # ontology has grown. The cast always pays the WGM validation toll.
                            # CYCLE BUDGET + ROTATION. `_rx_armed` implements the cursor: until
                            # the tenant the last cycle stopped at comes round, this cycle's
                            # drain budget is not spent, so the tail of the seat list gets the
                            # turn the head already had. Every OTHER phase in this block runs
                            # unconditionally — the budget gates the PREDECESSOR, never the
                            # lifecycle work it was starving.
                            _rx_armed, _rx_may_drain = _reextract_cycle_gate(
                                _rx_armed, _reextract_cycle_cursor[0], user_id,
                                _rx_cycle_spent, _REEXTRACT_CYCLE_BUDGET_S,
                            )
                            if reextract_enabled and not _rx_may_drain:
                                _rx_cycle_denied_n += 1
                                if _rx_cycle_denied_first is None and _rx_armed:
                                    # Only an ARMED denial sets the cursor: a pre-arm skip is
                                    # this cycle honouring the PREVIOUS cursor, not a new one.
                                    _rx_cycle_denied_first = str(user_id)
                            if reextract_enabled and _rx_may_drain:
                                _rx_t_tenant = time.monotonic()
                                release_read_transaction(
                                    db_per_user,
                                    context=f"re_embedder.per_user.reextract schema={schema_name}")
                                try:
                                    n_reextracted = reextract_episodic(
                                        db_per_user, backend_api_url, user_id=user_id,
                                        schema_name=schema_name, batch_size=reextract_batch_size,
                                        statement_route=reextract_route,
                                    )
                                    if n_reextracted:
                                        log.info(f"re_embedder.reextract_complete user_id={user_id[:8]} schema={schema_name} rows={n_reextracted}")
                                except Exception as _rex_err:
                                    log.warning(f"re_embedder.reextract_cycle_error user_id={user_id[:8]}: {_rex_err}")
                                finally:
                                    # `finally`, not the success path: a tenant that raised
                                    # still SPENT the wall-clock, and the whole point of a time
                                    # budget is that failures are the expensive case.
                                    _rx_cycle_spent += time.monotonic() - _rx_t_tenant

                            # Async document-ingestion lane (flagship): drain pending
                            # `documents` rows the ingest_document tool enqueued. Each
                            # chunk runs the HYBRID extraction (deterministic spine +
                            # tenant-brain LLM /extract/rewrite for the sentences the
                            # spine dropped) → /ingest source="mcp" (user_stated,
                            # durable). Reuses the once-per-cycle brain route. Fail-safe:
                            # per-tenant isolation, never poisons the loop.
                            release_read_transaction(
                                db_per_user,
                                context=f"re_embedder.per_user.doc_drain schema={schema_name}")
                            try:
                                n_docs = drain_pending_documents(
                                    db_per_user, backend_api_url, user_id=user_id,
                                    schema_name=schema_name, statement_route=reextract_route,
                                )
                                if n_docs:
                                    log.info(f"re_embedder.document_drain_complete user_id={user_id[:8]} schema={schema_name} docs={n_docs}")
                            except Exception as _doc_err:
                                log.warning(f"re_embedder.document_drain_cycle_error user_id={user_id[:8]}: {_doc_err}")

                            # JOB 2 — Promote Class C rows that earned hit_count >= 3 (query-scoped
                            # hits) to Class B. Classify-if-needed (default B-2). Run BEFORE the
                            # hit-decay sweep so a row that just reached threshold promotes instead
                            # of being decremented in the same cycle.
                            release_read_transaction(
                                db_per_user,
                                context=f"re_embedder.per_user.class_c_promote schema={schema_name}")
                            n_c_promoted = promote_class_c_hits(
                                db_per_user, qdrant_url, qwen_api_url,
                                user_id=user_id, schema_name=schema_name
                            )
                            if n_c_promoted:
                                log.info(f"re_embedder.class_c_promotion_complete user_id={user_id[:8]} promoted={n_c_promoted}")

                            # JOB 1 — Decay Class C query-hit counter for idle rows (30d window
                            # elapsed with no hit): hit_count -= 1, reset window, DROP at <= 0.
                            release_read_transaction(
                                db_per_user,
                                context=f"re_embedder.per_user.class_c_decay schema={schema_name}")
                            c_decay = decay_class_c_hits(db_per_user, qdrant_url, user_id=user_id)
                            if c_decay["decremented"] or c_decay["dropped"]:
                                log.info(
                                    f"re_embedder.class_c_decay user_id={user_id[:8]} "
                                    f"decremented={c_decay['decremented']} dropped={c_decay['dropped']}"
                                )

                            # JOB 3 — a STRUCK Class C row is evaluated against the durable A/B
                            # tier: reaped when A/B already carries it at equal-or-higher declared
                            # authority, extended for another attempt when it does not. This is
                            # the third exit that keeps the short-term tier moving instead of
                            # accumulating (the other two being promotion and expiry).
                            c_strike = evaluate_struck_class_c(db_per_user, user_id=user_id)
                            if c_strike["struck"]:
                                log.info(
                                    f"re_embedder.class_c_strike user_id={user_id[:8]} "
                                    f"struck={c_strike['struck']} reaped={c_strike['reaped']} "
                                    f"extended={c_strike['extended']}"
                                )

                        # Fetch and embed unsynced facts for this user.
                        # TIER REALIGNMENT: the `facts` table is the HARD A/B tier, served by
                        # the deterministic postgres walk — it need NOT be in the vector. When
                        # VECTOR_CLASS_C_ONLY is on (default) we SKIP the A/B (facts-table) sync
                        # entirely; only staged_facts (Class B/C) are embedded below. The query
                        # already drops any A/B Qdrant result (VECTOR_CLASS_C_ONLY in main.py),
                        # so leaving A/B out of the vector is safe and shrinks the C catch-all
                        # toward zero as more grounds into A/B. Flag off → legacy: sync both.
                        if not _USER_MEMORY_VECTOR_LANE:
                            # LANE GATE (interactive-latency round 2): user-memory vector tier
                            # RETIRED — mirror the staged-row treatment one screen below: mark
                            # unsynced `facts` rows synced WITHOUT embedding so this loop stops
                            # retrying a nonexistent Qdrant forever (observed live: qdrant_error
                            # fact_id=1..3 + collection_check_failed ECONNREFUSED every cycle).
                            # A/B facts are served by the deterministic postgres walk; nothing
                            # is lost.
                            try:
                                _marked = 0
                                with db_per_user.cursor() as _fc:
                                    _fc.execute(
                                        "UPDATE facts SET qdrant_synced = true "
                                        "WHERE qdrant_synced = false AND superseded_at IS NULL"
                                    )
                                    _marked = _fc.rowcount or 0
                                if _marked:
                                    db_per_user.commit()
                                    log.info("re_embedder.facts_mark_synced_lane_off "
                                             f"marked={_marked} user_id={user_id[:8]}")
                            except Exception as _e:  # noqa: BLE001
                                log.warning(f"re_embedder.facts_mark_synced_failed error={str(_e)[:120]}")
                        elif _VECTOR_CLASS_C_ONLY:
                            log.debug(f"re_embedder.facts_sync_skipped_class_c_only user_id={user_id[:8]}")
                        else:
                            rows = fetch_unsynced(db_per_user, user_id, confidence_threshold)
                            if rows:
                                log.info(f"re_embedder.batch_start user_id={user_id[:8]} count={len(rows)}")
                                from src.api.qdrant_partition import resolve_partition as _qp_resolve
                                collection, _ = _qp_resolve(user_id, "memory")
                                # READ BARRIER (RE-ARM): the read directly above re-opened a transaction AFTER the
                                # block-level barrier, and the call below blocks. A latching check cannot see this;
                                # measured live, it killed climb_classification_chains' connection twice.
                                release_read_transaction(db_per_user, context="re_embedder.per_user.facts_vector.pre_ensure")
                                ensure_collection(collection, qdrant_url)

                                # Resolve display names for batch
                                rows = resolve_display_names_for_facts(db_per_user, rows)
                                # READ BARRIER: the per-row embed + Qdrant upsert loop below
                                # is HTTP on every iteration; the name-resolution reads must
                                # not ride across it.
                                release_read_transaction(
                                    db_per_user,
                                    context=f"re_embedder.per_user.facts_vector schema={schema_name}")

                                for row in rows:
                                    try:
                                        text = f"{row['subject_display']} {row['rel_type']} {row['object_display']}"
                                        vector = embed_text(text, qwen_api_url)
                                        if upsert_to_qdrant(row, vector, collection, qdrant_url, source_table="facts"):
                                            mark_synced(db_per_user, row["id"])
                                            log.info(f"re_embedder.synced fact_id={row['id']} user_id={user_id[:8]}")
                                    except Exception as e:
                                        log.error(f"re_embedder.row_error fact_id={row['id']} user_id={user_id[:8]}: {e}")
                                        continue

                        # Fetch and embed unsynced staged facts for this user
                        staged_rows = fetch_unsynced_staged(db_per_user, user_id)
                        if staged_rows and not _USER_MEMORY_VECTOR_LANE:
                            # User-memory vector lane RETIRED: mark these Class-C rows synced WITHOUT
                            # embedding so the idle-probe stops firing and no Qdrant write happens. C
                            # remains queryable from staged_facts (Postgres).
                            try:
                                with db_per_user.cursor() as _mc:
                                    _mc.executemany(
                                        "UPDATE staged_facts SET qdrant_synced = true WHERE id = %s",
                                        [(_r["id"],) for _r in staged_rows],
                                    )
                                db_per_user.commit()
                            except Exception as _e:  # noqa: BLE001
                                log.warning(f"re_embedder.staged_mark_synced_failed error={str(_e)[:120]}")
                            staged_rows = []
                        if staged_rows:
                            log.info(f"re_embedder.staged_batch user_id={user_id[:8]} count={len(staged_rows)}")
                            from src.api.qdrant_partition import resolve_partition as _qp_resolve
                            collection, _ = _qp_resolve(user_id, "memory")
                            # READ BARRIER (RE-ARM): the read directly above re-opened a transaction AFTER the
                            # block-level barrier, and the call below blocks. A latching check cannot see this;
                            # measured live, it killed climb_classification_chains' connection twice.
                            release_read_transaction(db_per_user, context="re_embedder.per_user.staged_vector.pre_ensure")
                            ensure_collection(collection, qdrant_url)

                            staged_rows = resolve_display_names_for_facts(db_per_user, staged_rows)
                            # READ BARRIER: same shape as the facts lane directly above.
                            release_read_transaction(
                                db_per_user,
                                context=f"re_embedder.per_user.staged_vector schema={schema_name}")
                            for row in staged_rows:
                                try:
                                    # TIER REALIGNMENT: the vector is the Class-C catch-all + the
                                    # cosine TALLY. staged_facts holds Class B (pre-promotion) AND
                                    # Class C; B is query-visible from postgres (the staged UNION)
                                    # and is dropped from the Qdrant lane at query, so a B vector
                                    # point serves nothing. When VECTOR_CLASS_C_ONLY is on we embed
                                    # ONLY Class C; non-C staged rows are marked synced (no embed)
                                    # so they don't churn every cycle. Flag off → embed all staged.
                                    if _VECTOR_CLASS_C_ONLY and (row.get("fact_class") or "C") != "C":
                                        with db_per_user.cursor() as cur:
                                            cur.execute(
                                                "UPDATE staged_facts SET qdrant_synced = true WHERE id = %s",
                                                (row["staged_id"],)
                                            )
                                        db_per_user.commit()
                                        log.debug(f"re_embedder.staged_non_c_skipped staged_id={row['staged_id']} class={row.get('fact_class')} user_id={user_id[:8]}")
                                        continue
                                    text = f"{row['subject_display']} {row['rel_type']} {row['object_display']}"
                                    vector = embed_text(text, qwen_api_url)
                                    if upsert_to_qdrant(row, vector, collection, qdrant_url, source_table="staged_facts"):
                                        with db_per_user.cursor() as cur:
                                            cur.execute(
                                                "UPDATE staged_facts SET qdrant_synced = true WHERE id = %s",
                                                (row["staged_id"],)
                                            )
                                        db_per_user.commit()
                                        log.info(f"re_embedder.staged_synced staged_id={row['staged_id']} user_id={user_id[:8]}")
                                except Exception as e:
                                    log.error(f"re_embedder.staged_row_error staged_id={row['staged_id']} user_id={user_id[:8]}: {e}")

                        # INGEST_ENABLED freeze: both blocks below INSERT/UPDATE
                        # knowledge rows (facts/staged_facts) — paused in knowledge-store mode.
                        if ingest_enabled:
                            # Post-expand reconciliation: create missing instance_of links
                            # for entities whose entity_type matches a hierarchy node alias.
                            try:
                                n_reconciled = _reconcile_hierarchy_links(postgres_dsn, schema_name)
                                if n_reconciled:
                                    log.info(f"re_embedder.hierarchy_reconciled user_id={user_id[:8]} schema={schema_name} created={n_reconciled}")
                            except Exception as _recon_err:
                                log.warning(f"re_embedder.hierarchy_reconcile_error user_id={user_id[:8]}: {_recon_err}")

                            # Upgrade Class C staged_facts to Class B when their rel_type
                            # now exists in the rel_types table (approved by ontology eval).
                            try:
                                n_upgraded = _upgrade_staged_facts_with_known_rels(postgres_dsn, schema_name)
                                if n_upgraded:
                                    log.info(f"re_embedder.staged_upgraded_c_to_b user_id={user_id[:8]} schema={schema_name} upgraded={n_upgraded}")
                            except Exception as _upg_err:
                                log.warning(f"re_embedder.staged_upgrade_error user_id={user_id[:8]}: {_upg_err}")

                except Exception as e:
                    # Per-tenant isolation (fail-loud, never poison the loop): a tenant with
                    # a missing/incomplete table aborts its transaction. Roll back the per-user
                    # connection so a left-aborted txn cannot error on context-manager exit, then
                    # `continue` to the next tenant. Each tenant has its OWN db_per_user connection
                    # (fresh per iteration), so a broken tenant can never carry an aborted txn into
                    # another tenant's jobs — the cascade is contained to the offending tenant.
                    try:
                        db_per_user.rollback()
                    except Exception as rollback_err:
                        log.warning(f"re_embedder.per_user_rollback_failed schema={schema_name}: {rollback_err}")
                    log.error(f"re_embedder.per_user_promotion_failed user_id={user_id[:8] if user_id else 'unknown'} schema={schema_name}: {e}")
                    continue

            # ── COMMIT THE ROTATION CURSOR (end of the per-tenant pass) ──────────────────
            # Three outcomes, and the un-armed one is the failure mode a dirty-flag design
            # actually dies of, so it is CRIT rather than a debug line:
            #   * budget never tripped        → cursor cleared; next cycle starts from the top.
            #   * budget tripped              → cursor = first ARMED tenant denied; next cycle
            #                                   skips ahead to it before spending anything.
            #   * cursor never armed          → the tenant it names left `ready_schemas`
            #                                   (deprovisioned, ghost-swept, suspended). Left
            #                                   alone it would deny EVERY tenant forever. Clear
            #                                   it and say so.
            # `ingest_enabled` is part of the condition, not an oversight: on a FROZEN cycle no
            # tenant reaches the gate, so `_rx_armed` cannot advance and the un-armed branch
            # would CRIT every cycle for as long as an operator deliberately holds the freeze.
            # A chosen freeze must not cry wolf (the same rule the episodic-capture freeze
            # follows) — and the cursor is still correct when the freeze lifts.
            if ingest_enabled and reextract_enabled and _REEXTRACT_CYCLE_BUDGET_S:
                if not _rx_armed:
                    _doc_log_crit(
                        "re_embedder.reextract_cursor_unarmed",
                        cursor=str(_reextract_cycle_cursor[0])[:8],
                        seats=len(ready_schemas), denied=_rx_cycle_denied_n,
                        note="rotation cursor named a seat that never reached the drain gate "
                             "this cycle (deprovisioned, ghost-swept, suspended, or skipped by "
                             "the idle gate) — cleared so the drain resumes from the top next "
                             "cycle rather than denying every seat forever",
                    )
                    _reextract_cycle_cursor[0] = None
                else:
                    _reextract_cycle_cursor[0] = _rx_cycle_denied_first
                    if _rx_cycle_denied_first is not None:
                        log.warning(
                            "re_embedder.reextract_cycle_budget_exhausted "
                            f"spent={_rx_cycle_spent:.1f}s budget={_REEXTRACT_CYCLE_BUDGET_S:.0f}s "
                            f"seats_denied={_rx_cycle_denied_n} "
                            f"next_cycle_resumes_at={_rx_cycle_denied_first[:8]} "
                            "(drain deferred, NOT dropped; every other subsystem kept its turn)"
                        )

            # Clear the last-bound tenant attribution so the between-tenant / gate-tuning work
            # below is never attributed to the previous tenant.
            _reembedder_clear_tenant()

            # GROWTH ENGINE WIRE #2: Adjust per-user confidence gates based on feedback
            # Phase 2c: Intent classification gate self-healing (runs every cycle)
            # Enables system to learn from intent classification patterns without hardcoded thresholds.
            #
            # PER-TENANT: intent_confidence_feedback (the signal) and confidence_gates (the
            # output) both live in the user's OWN schema — /classify-intent writes feedback and
            # reads the gate under the tenant search_path; this loop reads feedback and writes
            # the gate under the SAME per-tenant binding. public is template-only; the self-tuning
            # loop never reads or writes public (no cross-tenant pollution). We iterate
            # ready_schemas on a dedicated per-tenant connection (SET search_path TO {schema},
            # NO public) so each tenant's gate is computed from its own feedback only.
            try:
                adjusted_count = 0
                for _gate_user_id, _gate_schema in ready_schemas:
                    try:
                        with psycopg2.connect(postgres_dsn) as db:
                            with db.cursor() as cur:
                                cur.execute(f"SET search_path TO {_gate_schema}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema from UUID-derived source with validation
                            # Require significant feedback history (>= 10 classifications) for
                            # THIS tenant before tuning; otherwise leave the gate at its default.
                            with db.cursor() as cur:
                                cur.execute("""
                                    SELECT COALESCE(SUM(count), 0)
                                    FROM intent_confidence_feedback
                                    WHERE user_id = %s
                                """, (_gate_user_id,))
                                _total = cur.fetchone()[0] or 0
                            if _total < 10:
                                continue

                            user_id = _gate_user_id
                            try:
                                # Query feedback distribution (PER-TENANT: unqualified read under
                                # the bound tenant search_path).
                                with db.cursor() as cur:
                                    cur.execute("""
                                        SELECT confidence_bin, feedback_type, count
                                        FROM intent_confidence_feedback
                                        WHERE user_id = %s
                                        ORDER BY confidence_bin ASC
                                    """, (user_id,))
                                    feedback_rows = cur.fetchall()

                                if not feedback_rows:
                                    continue

                                # Compute optimal gate threshold based on feedback distribution
                                # Strategy: find confidence level where corrections spike (wrong classifications)
                                # Lower threshold where there are many corrections; raise where false positives
                                total_feedback = sum(row[2] for row in feedback_rows)
                                corrections = sum(row[2] for row in feedback_rows if row[1] == 'correction')
                                correction_rate = corrections / total_feedback if total_feedback > 0 else 0.0

                                # Bias DOWNWARD (trust GLiNER2, escalate less / cheaper): no
                                # corrections at all → gate to GATE_MIN; otherwise → the reliability
                                # boundary computed below. PRODUCT DECISION: the downward-bias
                                # direction is intentional (a human may revisit it). Only learn
                                # stricter gates from actual user corrections.
                                if corrections == 0:
                                    recommended_gate = GATE_MIN  # Aggressive: allow more through
                                    log.debug(f"re_embedder.gate_aggressive user_id={user_id[:8]} reason=no_corrections")
                                else:
                                    # Find the boundary below which GLiNER2 stops being reliable, then
                                    # set the gate THERE: trust GLiNER2 above it, escalate only below it.
                                    # Walk DOWN from the highest-confidence bin while bins stay reliable
                                    # (correction rate < 15%) and stop at the first confirmed-UNreliable
                                    # bin — the gate is the bottom of that contiguous reliable region.
                                    # (Taking the *highest* reliable bin instead pinned the gate to the
                                    # ceiling and escalated everything — a saddle on a log, steering
                                    # nothing. Contiguity-from-top also ignores a noisy low bin that
                                    # looks reliable beneath an unreliable one.)
                                    bin_correction_rates = {}
                                    for bin_range, feedback_type, count in feedback_rows:
                                        if bin_range not in bin_correction_rates:
                                            bin_correction_rates[bin_range] = {'corrections': 0, 'total': 0}
                                        bin_correction_rates[bin_range]['total'] += count
                                        if feedback_type == 'correction':
                                            bin_correction_rates[bin_range]['corrections'] += count

                                    recommended_gate = GATE_DEFAULT  # fallback: no significant reliable region
                                    for bin_range in sorted(bin_correction_rates.keys(), reverse=True):
                                        stats = bin_correction_rates[bin_range]
                                        if stats['total'] < 5:  # not enough signal — skip, keep walking down
                                            continue
                                        bin_correction_rate = stats['corrections'] / stats['total']
                                        if bin_correction_rate < 0.15:  # reliable — extend the region downward
                                            recommended_gate = float(bin_range.split('-')[0])
                                        else:  # first confirmed-unreliable bin from the top → boundary found
                                            break

                                # CLAMP to [GATE_MIN, GATE_MAX]. The 5%-bin formula can place
                                # confidence==1.0 classifications into a "1.00-1.05" bin whose
                                # bin_start is 1.00 — without this clamp that leaks an out-of-range
                                # gate the readers reject (the bug this consolidation fixes).
                                recommended_gate = clamp_gate(recommended_gate)

                                # Persist recommended gate to the tenant's OWN confidence_gates
                                # (what /classify-intent and /confidence-gate read per-tenant).
                                # UNQUALIFIED → lands in <schema>.confidence_gates (NO public).
                                with db.cursor() as cur:
                                    cur.execute("""
                                        INSERT INTO confidence_gates (user_id, threshold, adjusted_at)
                                        VALUES (%s, %s, now())
                                        ON CONFLICT (user_id) DO UPDATE
                                        SET threshold = %s, adjusted_at = now()
                                    """, (user_id, recommended_gate, recommended_gate))
                                db.commit()
                                adjusted_count += 1
                                log.info(f"re_embedder.gate_adjusted user_id={user_id[:8]} recommended_gate={recommended_gate:.2f} correction_rate={correction_rate:.2%}")

                            except Exception as e:
                                log.warning(f"re_embedder.gate_adjustment_failed user_id={user_id[:8]}: {e}")
                    except Exception as _gate_tenant_err:
                        log.warning(f"re_embedder.gate_adjustment_tenant_error schema={_gate_schema} (non-fatal): {str(_gate_tenant_err)[:120]}")

                if adjusted_count > 0:
                    log.info(f"re_embedder.gate_adjustment_cycle adjusted={adjusted_count} users")

            except Exception as e:
                log.warning(f"re_embedder.gate_adjustment_phase_error (non-blocking): {e}")

            # GROWTH ENGINE JOB 7: Promote Learned Patterns
            # Issue #2: When LLM fallback learns a pattern and it's confirmed 3+ times,
            # promote its confidence to 0.95 (high confidence). This enables faster
            # pattern matching in future /classify-intent calls.
            try:
                with psycopg2.connect(postgres_dsn) as db:
                    with db.cursor() as cur:
                        # Find all user schemas
                        cur.execute("""
                            SELECT schema_name FROM public.user_provisioning
                            WHERE status = 'ready'
                        """)
                        schemas = [row[0] for row in cur.fetchall()]

                    for schema_name in schemas:
                        try:
                            with db.cursor() as cur:
                                # Promote low-confidence learned patterns when confirmed 3+ times
                                cur.execute(f"""
                                    UPDATE {schema_name}.negation_patterns
                                    SET confidence = 0.95, updated_at = NOW()
                                    WHERE learned_from = 'LLM_FALLBACK'
                                    AND confirmed_count >= 3
                                    AND confidence < 0.95
                                    AND pattern_text IS NOT NULL
                                """)
                                promoted = cur.rowcount
                                if promoted > 0:
                                    db.commit()
                                    log.info(f"re_embedder.job7_patterns_promoted schema={schema_name} count={promoted}")
                        except Exception as e:
                            # Per-tenant isolation: this loop SHARES one `db` connection across
                            # schemas. A schema missing negation_patterns aborts the transaction,
                            # so roll back BEFORE the next schema or every subsequent UPDATE fails
                            # with "current transaction is aborted" (cross-tenant cascade).
                            try:
                                db.rollback()
                            except Exception as rollback_err:
                                log.warning(f"re_embedder.job7_rollback_failed schema={schema_name}: {rollback_err}")
                            log.debug(f"re_embedder.job7_schema_error schema={schema_name}: {str(e)[:100]}")
                            continue
            except Exception as e:
                log.warning(f"re_embedder.job7_pattern_promotion_error (non-blocking): {e}")

            # Continue with default schema for global work (embeddings, ontology, etc)
            with psycopg2.connect(postgres_dsn) as db:
                # Phase 3: Process Redis queue events first (high-priority)
                # Non-blocking: if Redis unavailable, system continues with DB poll
                if redis_client:
                    try:
                        queue_events_processed = consume_reembedder_queue(db, redis_client, qwen_api_url)
                        if queue_events_processed > 0:
                            log.info(f"re_embedder.queue_events_processed count={queue_events_processed}")
                    except Exception as e:
                        log.warning(f"re_embedder.queue_consumer_error (non-blocking): {e}")
                        # Continue to DB poll even if queue fails
                    finally:
                        # CONNECTION HYGIENE (defense-in-depth for the idle-in-transaction
                        # leak fixed in process_reembedder_event): guarantee the shared
                        # poll-loop connection carries NO open read transaction out of the
                        # queue consumer before the cycle's later slow (LLM) ontology work.
                        # Any lingering idle-in-transaction here holds ACCESS SHARE and can
                        # block a concurrent reset_tenant TRUNCATE. Event writers commit/
                        # rollback internally and read-only events persist nothing, so this
                        # rollback cannot lose data (it is a no-op on a clean connection).
                        try:
                            db.rollback()
                        except Exception:  # noqa: BLE001 — hygiene rollback is best-effort
                            pass

                # NOTE: Per-user promotion and fact syncing now happens in PHASE 2b
                # (see per-schema isolation above in the ready_schemas loop)

                # dprompt-121: Event-driven ontology evaluation (skip if no pending work)
                # Phase 3c: Wrap in error isolation to prevent background loop crash
                #
                # PER-USER ONTOLOGY GROWTH (authoritative architecture decision 2026-06-10):
                # ontology_evaluations and rel_types live in the USER's schema (faultline_<slug>),
                # which has NO user_id column — schema = scope. The public.* copies are SEED
                # TEMPLATES ONLY and must NEVER receive growth data. Ingest correctly writes
                # candidates to the per-user ontology_evaluations; therefore the evaluator MUST
                # run with search_path set to each user's schema. Running it on the public `db`
                # connection (the prior bug) read public.ontology_evaluations — always empty —
                # so has_pending_ontology_work() returned False and the loop was permanently
                # severed. We iterate ready_schemas on dedicated per-user connections.
                _approved_any = False
                # Synonym-convergence writes a rel_type_aliases row (cross-process invisible until the
                # backend overlay is refreshed) — track it separately so the refresh POST fires even
                # when NO ontology approval / head-tail sweep happened this cycle.
                _converged_any = False
                _sweep_updated_total = 0
                # Per-tenant overlay coordination: collect the schemas whose rel_types
                # actually changed so the refresh endpoint invalidates ONLY those
                # tenants' overlays (isolation + minimal rebuild cost). Empty set →
                # endpoint falls back to a full overlay reset (backward-compatible).
                _changed_schemas: set = set()
                for _user_id, _schema in ready_schemas:
                    # LEDGER SEAT GATE — the line that makes a quiet seat free. No connection,
                    # no search_path bind, no tenant-brain bind, no probe, no LLM call.
                    if not _sweep_snap.seat_has_work(_user_id):
                        _sweep.log_seat_skip(_sweep_snap, _user_id, _schema)
                        continue
                    try:
                        with psycopg2.connect(postgres_dsn) as _ont_db:
                            with _ont_db.cursor() as _spc:
                                _spc.execute(f"SET search_path TO {_schema}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema from UUID-derived source with validation
                            _ont_db.commit()
                            # Attribute this tenant's LLM calls for the LLM ops in this loop
                            # (ontology eval + the ±6 what-is/climb ENRICHMENT grounder + synonym
                            # convergence). The primary sync loop cleared the attribution.
                            _reembedder_bind_tenant(_schema)

                            # ══ READ BARRIER — the "idle in transaction" invariant ══════════
                            # PRODUCTION INCIDENT 2026-08-01: a connection from this container
                            # sat `idle in transaction` for 10h08m holding AccessShareLock on
                            # rel_types with backend_xid NULL — a PURE READ that never ended its
                            # transaction. It queued a tenant DROP SCHEMA behind it, and once an
                            # AccessExclusive waiter is queued EVERY later request queues too:
                            # the metrics collector blocked, every `LOCK TABLE public.facts`
                            # blocked, and a pg_dump of a 225 MB database produced a 0-byte file
                            # for 10+ minutes of pure lock-wait (it finished in seconds with
                            # faultline-api stopped).
                            #
                            # THIS LOOP IS THE CALL SITE. `_ont_db` is ONE connection shared by
                            # every subsystem below. psycopg2 opens an implicit transaction on
                            # the FIRST statement and holds it until an explicit commit/rollback
                            # — closing the CURSOR does not end it. Several subsystems SELECT and
                            # then hit `if not rows: return stats` with neither, so the NEXT
                            # subsystem starts its LLM HTTP call INHERITING that read transaction
                            # and holds rel_types' locks for the whole call. With a wedged LLM
                            # (this repo has a 6s timeout that logged elapsed=762s as SUCCESS)
                            # that hold is unbounded.
                            #
                            # THE INVARIANT, enforced at the top of every subsystem try-block
                            # below: NO SUBSYSTEM BEGINS ITS WORK INHERITING THE PREVIOUS
                            # SUBSYSTEM'S READ TRANSACTION.
                            #
                            # WHY release_read_transaction() AND NOT _rollback_and_reapply_search_path():
                            # this barrier sits on a SUCCESS path, where a pending write is
                            # possible. An unconditional rollback would silently DISCARD it.
                            # release_read_transaction() probes pg_current_xact_id_if_assigned():
                            # xid NULL (pure read) → rollback; xid ASSIGNED (a write is pending)
                            # → it REFUSES and log_crit()s, so the write survives and the real
                            # defect — a caller not reaching its own commit — is made loud.
                            # `_rollback_and_reapply_search_path` stays where it belongs: the
                            # `except` arms, where the transaction is already doomed.
                            #
                            # TENANT BINDING SURVIVES — verified against PG16, not assumed. The
                            # `SET search_path TO {_schema}` above was COMMITTED (line ~11443), and
                            # ROLLBACK does not clear a committed SET. So no re-apply is needed
                            # here and none is added: re-applying would be noise implying the
                            # barrier is dangerous when it is not. (The re-apply inside
                            # `_rollback_and_reapply_search_path` is harmless, not load-bearing.)

                            # ── Ontology candidate evaluation (per-user schema) ──
                            # INGEST_ENABLED freeze: mutates rel_types/ontology_evaluations
                            # (ontology growth) — paused in knowledge-store mode.
                            try:
                                _sw = "ontology_eval"
                                _sw_run = _reembedder_claim(_sweep_snap, _user_id, _sw, _schema)
                                release_read_transaction(
                                    _ont_db, context=f"re_embedder.ontology_eval schema={_schema}")
                                if _sw_run and ingest_enabled and has_pending_ontology_work(_ont_db):
                                    # READ BARRIER (RE-ARM): the read directly above re-opened a transaction AFTER the
                                    # block-level barrier, and the call below blocks. A latching check cannot see this;
                                    # measured live, it killed climb_classification_chains' connection twice.
                                    release_read_transaction(_ont_db, context="re_embedder.ontology_eval.pre_blocking_call")
                                    ontology_stats = evaluate_ontology_candidates(_ont_db, qwen_api_url)
                                    if any(v > 0 for v in ontology_stats.values()):
                                        log.info(
                                            f"re_embedder.ontology_eval "
                                            f"schema={_schema} "
                                            f"approved={ontology_stats['approved']} "
                                            f"mapped={ontology_stats['mapped']} "
                                            f"rejected={ontology_stats['rejected']} "
                                            f"errors={ontology_stats['errors']}"
                                        )
                                    if ontology_stats.get("approved", 0) > 0 or ontology_stats.get("mapped", 0) > 0:
                                        _approved_any = True
                                        _changed_schemas.add(_schema)
                                else:
                                    log.debug(f"re_embedder.no_pending_ontology_work schema={_schema}")
                                if _sw_run:
                                    _sweep.record_run(_ont_db, _user_id, _sw,
                                                      _sweep_snap.observed_token(_user_id, _sw))
                            except Exception as e:
                                _rollback_and_reapply_search_path(_ont_db, _schema)
                                log.error(f"re_embedder.ontology_eval_subsystem_error schema={_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                            # ── Aspect-synonym GROWTH backstop (per-user schema) ──
                            # Drains timed-out inline aspect misses (tall→height) and grows the
                            # per-tenant rel_type_aliases link so the NEXT query resolves
                            # deterministically. INGEST_ENABLED freeze: grows ontology (aliases) —
                            # paused in knowledge-store/serve mode. Writes rel_type_aliases →
                            # mark the schema changed so its overlay refreshes.
                            try:
                                _sw = "aspect_synonym"
                                _sw_run = _reembedder_claim(_sweep_snap, _user_id, _sw, _schema)
                                release_read_transaction(
                                    _ont_db, context=f"re_embedder.aspect_synonym schema={_schema}")
                                if _sw_run and ingest_enabled:
                                    _asp_stats = evaluate_aspect_synonym_candidates(
                                        _ont_db, postgres_dsn, _schema, qwen_api_url)
                                    if _asp_stats.get("grown", 0) > 0:
                                        log.info(
                                            f"re_embedder.aspect_synonym schema={_schema} "
                                            f"grown={_asp_stats['grown']} "
                                            f"unmapped={_asp_stats['unmapped']} "
                                            f"skipped={_asp_stats['skipped']} "
                                            f"errors={_asp_stats['errors']}")
                                        _changed_schemas.add(_schema)
                                if _sw_run:
                                    _sweep.record_run(_ont_db, _user_id, _sw,
                                                      _sweep_snap.observed_token(_user_id, _sw))
                            except Exception as e:
                                _rollback_and_reapply_search_path(_ont_db, _schema)
                                log.error(f"re_embedder.aspect_synonym_subsystem_error schema={_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                            # ── PENDING-PLACEMENT morphology DRAIN (deterministic, per-user schema) ──
                            # Reconcile EXISTING `pending_placement` rels onto their SEEDED canonical
                            # IN PLACE (alias + adopt category + join the seed's taxonomy) so their
                            # already-stored facts become walkable WITHOUT re-ingest. Companion to the
                            # in-flow morphology fold (main.py 48c3200 seam) which only catches FRESH
                            # ingests. SEEDED morphology match ONLY (no cosine); a non-matching pending
                            # rel is LEFT for the freq>=3/LLM path. Self-isolating (failure never
                            # crashes the tenant sweep). Touches rel_types/entity_taxonomies → mark
                            # the schema changed so its overlay refreshes.
                            # INGEST_ENABLED freeze: mutates rel_types/entity_taxonomies —
                            # paused in knowledge-store mode.
                            try:
                                _sw = "pending_placement_drain"
                                _sw_run = _reembedder_claim(_sweep_snap, _user_id, _sw, _schema)
                                release_read_transaction(
                                    _ont_db, context=f"re_embedder.pending_placement_drain schema={_schema}")
                                _drain = (drain_pending_placement_by_morphology(
                                    _ont_db, postgres_dsn, _schema)
                                    if (_sw_run and ingest_enabled) else {})
                                if _drain.get("reconciled", 0) > 0:
                                    log.info(
                                        f"re_embedder.pending_placement_drain schema={_schema} "
                                        f"reconciled={_drain['reconciled']} "
                                        f"scanned={_drain['scanned']} errors={_drain['errors']}"
                                    )
                                    _changed_schemas.add(_schema)
                                if _sw_run:
                                    _sweep.record_run(_ont_db, _user_id, _sw,
                                                      _sweep_snap.observed_token(_user_id, _sw))
                            except Exception as e:
                                _rollback_and_reapply_search_path(_ont_db, _schema)
                                log.error(f"re_embedder.pending_placement_drain_subsystem_error schema={_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                            # ── ENGINE SYNONYM CONVERGENCE (deterministic-gated, per-user schema) ──
                            # Companion to the morphology drain above: converge a LIFTED novel rel that
                            # is a SEMANTIC synonym of a SEEDED rel the morphology-fold could NOT see
                            # (marry→spouse, adopt→has_pet) onto that seed, behind three FAIL-CLOSED
                            # gates (Gate 1 deterministic type-constraint match kills the fuzzy-coercion
                            # class WITHOUT the LLM; Gate 2 LLM proposes STRICT equivalence only; Gate 3
                            # structural sanity), then mirror the drain's alias + category + taxonomy
                            # write. Bias FALSE-NEGATIVE over FALSE-POSITIVE. Self-isolating (failure
                            # never crashes the tenant sweep). Touches rel_types/entity_taxonomies +
                            # writes a rel_type_aliases row → mark the schema changed AND flip
                            # _converged_any so the cross-process overlay refresh fires.
                            # INGEST_ENABLED freeze: mutates rel_types/entity_taxonomies/
                            # rel_type_aliases — paused in knowledge-store mode.
                            try:
                                _sw = "synonym_convergence"
                                _sw_run = _reembedder_claim(_sweep_snap, _user_id, _sw, _schema)
                                release_read_transaction(
                                    _ont_db, context=f"re_embedder.synonym_convergence schema={_schema}")
                                if _sw_run and ingest_enabled and _ENGINE_SYNONYM_CONVERGENCE:
                                    _synconv = converge_lifted_synonyms(
                                        _ont_db, postgres_dsn, _schema, qwen_api_url)
                                    if _synconv.get("converged", 0) > 0:
                                        log.info(
                                            f"re_embedder.synonym_convergence schema={_schema} "
                                            f"converged={_synconv['converged']} "
                                            f"scanned={_synconv['scanned']} "
                                            f"left_novel={_synconv['left_novel']} "
                                            f"brain_unavailable={_synconv.get('brain_unavailable', 0)} "
                                            f"errors={_synconv['errors']}"
                                        )
                                        _converged_any = True
                                        _changed_schemas.add(_schema)
                                    # FAIL LOUD: a sweep in which the brain never answered
                                    # converges nothing, so it is INVISIBLE at the log line
                                    # above (gated on converged > 0). Without this, "the lane
                                    # is working and found no synonyms" and "the tenant's brain
                                    # has been unreachable for a week" read identically.
                                    if _synconv.get("brain_unavailable", 0) > 0:
                                        log.warning(
                                            f"re_embedder.synonym_convergence_brain_unavailable "
                                            f"schema={_schema} "
                                            f"brain_unavailable={_synconv['brain_unavailable']} "
                                            f"scanned={_synconv['scanned']} "
                                            f"note=no verdicts cached for these rels; they are "
                                            f"re-offered next sweep. Check this tenant's brain."
                                        )
                                if _sw_run:
                                    _sweep.record_run(_ont_db, _user_id, _sw,
                                                      _sweep_snap.observed_token(_user_id, _sw))
                            except Exception as e:
                                _rollback_and_reapply_search_path(_ont_db, _schema)
                                log.error(f"re_embedder.synonym_convergence_subsystem_error schema={_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                            # ── MISS-PUSHBACK "what is X?" concept classify (per-user schema) ──
                            # SECONDARY strengthen for the ingest miss-pushback path: type+ground
                            # the unknown user-derived concepts that landed C-raw at first-fire so
                            # the structure can resolve on a later cycle. Background + preemptible
                            # (runs in the engine, never on the ingest hot path), bounded, and
                            # self-isolating (failure never crashes the tenant sweep). Touching the
                            # schema's rel_types/entities/staged_facts → mark it changed so its
                            # overlay refreshes.
                            # INGEST_ENABLED freeze: mutates rel_types/entities/staged_facts —
                            # paused in knowledge-store mode.
                            try:
                                _sw = "whatis_classify"
                                _sw_run = _reembedder_claim(_sweep_snap, _user_id, _sw, _schema)
                                release_read_transaction(
                                    _ont_db, context=f"re_embedder.whatis_classify schema={_schema}")
                                if _sw_run and ingest_enabled and _ENGINE_WHATIS_CLASSIFY:
                                    _whatis = classify_unknown_concepts(_ont_db, qwen_api_url, user_id=_user_id, schema_name=_schema)
                                    if _whatis.get("classified", 0) > 0:
                                        log.info(
                                            f"re_embedder.whatis_classify schema={_schema} "
                                            f"classified={_whatis['classified']} "
                                            f"grounded={_whatis['grounded']} "
                                            f"deferred={_whatis['deferred']} "
                                            f"errors={_whatis['errors']}"
                                        )
                                        _changed_schemas.add(_schema)
                                if _sw_run:
                                    _sweep.record_run(_ont_db, _user_id, _sw,
                                                      _sweep_snap.observed_token(_user_id, _sw))
                            except Exception as e:
                                _rollback_and_reapply_search_path(_ont_db, _schema)
                                log.error(f"re_embedder.whatis_classify_subsystem_error schema={_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                            # ── ±6 CLASSIFICATION CLIMB + OPTION-A SPLICE (async rung-fill) ──
                            # The SHARED hierarchy mechanism for BOTH engine-ingest AND /expand
                            # (both write hierarchy edges into facts/staged_facts; this is the one
                            # path that deepens them — only per-row provenance differs). After the
                            # eager leaf-anchor, fill the MIDDLE rungs ONE PER PASS: SPLICE a
                            # too-direct edge (dog->animal becomes dog->canine->…->animal,
                            # superseding the direct edge — never dangling, never hard-deleted) and
                            # CLIMB non-root-tipped chains. LLM proposes the next parent; identity
                            # gates; terminate at a SEEDED ROOT (primary) or ±6 hop backstop
                            # (quarantine). Grown rungs born CLASS B at the correctable mid-tier
                            # (llm_learned — below user_stated, above llm_inferred). Background,
                            # bounded, self-isolating: failure never crashes the tenant sweep.
                            # Touches staged_facts/facts/ontology_evaluations → mark schema changed.
                            # INGEST_ENABLED freeze: mutates staged_facts/facts/
                            # ontology_evaluations — paused in knowledge-store mode.
                            try:
                                _sw = "classify_climb"
                                _sw_run = _reembedder_claim(_sweep_snap, _user_id, _sw, _schema)
                                release_read_transaction(
                                    _ont_db, context=f"re_embedder.classify_climb schema={_schema}")
                                if _sw_run and ingest_enabled and _ENGINE_CLASSIFY_CLIMB:
                                    _climb = climb_classification_chains(_ont_db, qwen_api_url, user_id=_user_id, schema_name=_schema)
                                    if _climb.get("climbed", 0) > 0 or _climb.get("quarantined", 0) > 0:
                                        log.info(
                                            f"re_embedder.classify_climb schema={_schema} "
                                            f"climbed={_climb['climbed']} "
                                            f"terminated={_climb['terminated']} "
                                            f"quarantined={_climb['quarantined']} "
                                            f"deferred={_climb['deferred']} "
                                            f"skipped_cached={_climb.get('skipped_cached', 0)} "
                                            f"errors={_climb['errors']}"
                                        )
                                        _changed_schemas.add(_schema)
                                if _sw_run:
                                    _sweep.record_run(_ont_db, _user_id, _sw,
                                                      _sweep_snap.observed_token(_user_id, _sw))
                            except Exception as e:
                                _rollback_and_reapply_search_path(_ont_db, _schema)
                                log.error(f"re_embedder.classify_climb_subsystem_error schema={_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                            # ── RUNG-6 convergence-by-identity (deterministic, per-user schema) ──
                            # Fuse separately-grown hierarchy branches that reached a same-canonical-
                            # name node by IDENTITY (no cosine). Runs every cycle; cheap (one node
                            # scan + targeted edge repoints). Self-isolating: failure never crashes
                            # the tenant sweep. This is the primary collapse that retires cosine-map.
                            # INGEST_ENABLED freeze: repoints facts hierarchy edges —
                            # paused in knowledge-store mode.
                            try:
                                _sw = "rung6_convergence"
                                _sw_run = _reembedder_claim(_sweep_snap, _user_id, _sw, _schema)
                                release_read_transaction(
                                    _ont_db, context=f"re_embedder.rung6_convergence schema={_schema}")
                                if _sw_run and ingest_enabled and _RUNG6_CONVERGENCE:
                                    _conv = converge_hierarchy_by_identity(_ont_db, schema_name=_schema)
                                    if _conv.get("edges_repointed", 0) > 0:
                                        log.info(
                                            f"re_embedder.rung6_convergence schema={_schema} "
                                            f"merged_nodes={_conv['merged_nodes']} "
                                            f"edges_repointed={_conv['edges_repointed']}"
                                        )
                                        _changed_schemas.add(_schema)
                                if _sw_run:
                                    _sweep.record_run(_ont_db, _user_id, _sw,
                                                      _sweep_snap.observed_token(_user_id, _sw))
                            except Exception as e:
                                _rollback_and_reapply_search_path(_ont_db, _schema)
                                log.error(f"re_embedder.rung6_convergence_subsystem_error schema={_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                            # ── Reinforce-or-decay sweep for novel rel_type candidates ──
                            # Mirrors expire_staged_facts (Class C score-decay), keyed on
                            # ontology_evaluations.occurrence_count + last_seen_at. Runs every
                            # cycle (NOT gated by has_pending_ontology_work) so un-reinforced
                            # one-off candidates age out even when nothing is at threshold.
                            # Per-user schema context: search_path already set above.
                            # INGEST_ENABLED freeze: mutates/deletes ontology_evaluations
                            # candidate rows — paused in knowledge-store mode.
                            try:
                                release_read_transaction(
                                    _ont_db, context=f"re_embedder.ontology_candidate_decay schema={_schema}")
                                _ont_decay = (decay_ontology_candidates(_ont_db, user_id=_user_id)
                                              if ingest_enabled else {"decayed": 0, "forgotten": 0})
                                if _ont_decay["decayed"] or _ont_decay["forgotten"]:
                                    log.info(
                                        f"re_embedder.ontology_candidate_decay schema={_schema} "
                                        f"decayed={_ont_decay['decayed']} forgotten={_ont_decay['forgotten']}"
                                    )
                            except Exception as e:
                                _rollback_and_reapply_search_path(_ont_db, _schema)
                                log.error(f"re_embedder.ontology_candidate_decay_subsystem_error schema={_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                            # ── CARVED cue-class growth (social_role / problem_noun, per-user schema) ──
                            # Grow the DOMAIN-FLAVORED cue classes that were carved out of the seed from
                            # freq-gated observed candidates (≥3) into <tenant>.linguistic_cues. Marks the
                            # schema changed so its linguistic_cue overlay is invalidated and the next turn
                            # routes the grown construction correctly. Self-isolating (failure never
                            # crashes the tenant sweep). Per-tenant only (search_path already = _schema).
                            try:
                                _sw = "cue_class_growth"
                                _sw_run = _reembedder_claim(_sweep_snap, _user_id, _sw, _schema)
                                release_read_transaction(
                                    _ont_db, context=f"re_embedder.cue_class_growth schema={_schema}")
                                _cue_grow = (grow_linguistic_cue_candidates(_ont_db, schema_name=_schema)
                                             if _sw_run else {})
                                if _cue_grow.get("grown", 0) > 0:
                                    log.info(
                                        f"re_embedder.cue_class_growth schema={_schema} "
                                        f"grown={_cue_grow['grown']} errors={_cue_grow['errors']}"
                                    )
                                    _changed_schemas.add(_schema)
                                if _sw_run:
                                    _sweep.record_run(_ont_db, _user_id, _sw,
                                                      _sweep_snap.observed_token(_user_id, _sw))
                            except Exception as e:
                                _rollback_and_reapply_search_path(_ont_db, _schema)
                                log.error(f"re_embedder.cue_class_growth_subsystem_error schema={_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                            # ── Correction-signal growth + firing promotion (per-user schema) ──
                            # MUST run here on the per-tenant _ont_db (search_path = _schema),
                            # NOT on the global `db` connection. correction_signal_evaluations
                            # candidates and correction_signals/correction_patterns growth all
                            # live in the USER schema (schema = scope; public.* is a seed
                            # template that must never receive growth). Running this on the
                            # public `db` connection read public.* (always empty) — the same
                            # severance bug documented for ontology_evaluations above.
                            try:
                                _sw = "correction_eval"
                                _sw_run = _reembedder_claim(_sweep_snap, _user_id, _sw, _schema)
                                release_read_transaction(
                                    _ont_db, context=f"re_embedder.correction_eval schema={_schema}")
                                _corr_stats = (evaluate_correction_signal_candidates(_ont_db, qwen_api_url)
                                               if _sw_run else {})
                                if any(v > 0 for v in _corr_stats.values()):
                                    log.info(
                                        f"re_embedder.correction_eval schema={_schema} "
                                        f"approved={_corr_stats['approved']} "
                                        f"promoted={_corr_stats['promoted']} "
                                        f"rejected={_corr_stats['rejected']} "
                                        f"errors={_corr_stats['errors']}"
                                    )
                                if _sw_run:
                                    _sweep.record_run(_ont_db, _user_id, _sw,
                                                      _sweep_snap.observed_token(_user_id, _sw))
                            except Exception as e:
                                _rollback_and_reapply_search_path(_ont_db, _schema)
                                log.error(f"re_embedder.correction_signal_subsystem_error schema={_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                            # ── Retroactive head_types/tail_types sweep (per-user schema) ──
                            # Severance #2, Phase 2: self-heal rel_types rows with NULL/empty
                            # head_types/tail_types. Bounded batch per cycle (LIMIT 10). Uses the
                            # SAME LLM metadata call so type constraints come from inference.
                            # INGEST_ENABLED freeze: the sweep UPDATEs rel_types type
                            # constraints (ontology mutation) — no rows selected when frozen.
                            try:
                                _sw = "head_tail_sweep"
                                _sw_run = _reembedder_claim(_sweep_snap, _user_id, _sw, _schema)
                                release_read_transaction(
                                    _ont_db, context=f"re_embedder.head_tail_sweep schema={_schema}")
                                _sweep_rows = []
                                if _sw_run and ingest_enabled:
                                    with _ont_db.cursor() as cur:
                                        cur.execute(
                                            "SELECT rel_type, head_types, tail_types, natural_language"
                                            " FROM rel_types"
                                            " WHERE (head_types IS NULL OR head_types = ARRAY[]::TEXT[]"
                                            "        OR tail_types IS NULL OR tail_types = ARRAY[]::TEXT[])"
                                            " ORDER BY confidence DESC NULLS LAST"
                                            " LIMIT 10"
                                        )
                                        _sweep_rows = cur.fetchall()

                                for _rt, _ht, _tt, _nl in _sweep_rows:
                                    try:
                                        # PER-ITEM BARRIER. The SELECT that filled _sweep_rows
                                        # left this connection INTRANS holding AccessShareLock on
                                        # rel_types, and the very next statement is a blocking LLM
                                        # call — the exact 10-hour shape. On later iterations the
                                        # previous row's UPDATE has already COMMITTED, so this is a
                                        # no-op; if it ever is NOT, a write is pending and
                                        # release_read_transaction() log_crit()s instead of
                                        # discarding it.
                                        release_read_transaction(
                                            _ont_db,
                                            context=f"re_embedder.head_tail_sweep.row schema={_schema} rel={_rt}")
                                        _md = _query_llm_for_rel_type_metadata(
                                            _rt, "unknown", "unknown", _nl or "", qwen_api_url
                                        )
                                        _new_head = _md.get("llm_head_types")
                                        _new_tail = _md.get("llm_tail_types")
                                        # Only fill what is missing; never overwrite existing non-empty values.
                                        _set_head = (not _ht) and bool(_new_head)
                                        _set_tail = (not _tt) and bool(_new_tail)
                                        if not (_set_head or _set_tail):
                                            continue
                                        with _ont_db.cursor() as cur:
                                            cur.execute(
                                                "UPDATE rel_types SET"
                                                "  head_types = CASE WHEN (head_types IS NULL OR head_types = ARRAY[]::TEXT[])"
                                                "                    THEN %s ELSE head_types END,"
                                                "  tail_types = CASE WHEN (tail_types IS NULL OR tail_types = ARRAY[]::TEXT[])"
                                                "                    THEN %s ELSE tail_types END"
                                                " WHERE rel_type = %s",
                                                (_new_head if _set_head else None,
                                                 _new_tail if _set_tail else None,
                                                 _rt),
                                            )
                                        _ont_db.commit()
                                        _sweep_updated_total += 1
                                        _changed_schemas.add(_schema)
                                        log.info(f"re_embedder.head_tail_sweep_filled schema={_schema} rel_type={_rt} "
                                                 f"head_types={_new_head if _set_head else _ht} "
                                                 f"tail_types={_new_tail if _set_tail else _tt}")
                                    except Exception as _sw_err:
                                        try:
                                            _ont_db.rollback()
                                            # search_path is reset by rollback (psycopg2) — re-apply
                                            # so the next sweep row targets the user schema, not public.
                                            with _ont_db.cursor() as _spc2:
                                                _spc2.execute(f"SET search_path TO {_schema}")
                                        except Exception:
                                            pass
                                        log.warning(f"re_embedder.head_tail_sweep_row_failed schema={_schema} rel_type={_rt}: {_sw_err}")
                                if _sw_run:
                                    _sweep.record_run(_ont_db, _user_id, _sw,
                                                      _sweep_snap.observed_token(_user_id, _sw))
                            except Exception as e:
                                _rollback_and_reapply_search_path(_ont_db, _schema)
                                log.warning(f"re_embedder.head_tail_sweep_error schema={_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")
                            # ENGINE-SIDE WRITER. A subsystem that GREW this tenant's
                            # ontology has created work for the OTHERS (a newly-minted rel_type
                            # needs head/tail types and a natural_language template; a new
                            # hierarchy rung re-opens the climb). `_changed_schemas` is the
                            # loop's existing "this tenant's metadata moved" signal — reuse it
                            # rather than invent a second source of truth.
                            if _schema in _changed_schemas:
                                _sweep.mark_dirty_conn(
                                    _ont_db, _user_id,
                                    "rel_types", "rel_type_aliases", "entity_taxonomies",
                                    "ontology_evaluations", "facts", "staged_facts",
                                    "entity_aliases", "linguistic_cues")
                                _ont_db.commit()
                    except Exception as e:
                        log.error(f"re_embedder.ontology_per_user_error schema={_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                # Phase 3 (Severance #3): newly-approved/mapped rel_types or filled type
                # constraints live in the DB but are invisible to the backend uvicorn process
                # (separate OS process) until it reloads _REL_TYPE_META. Trigger the cross-process
                # refresh endpoint ONCE per cycle if anything changed across any user schema.
                if _approved_any or _converged_any or _sweep_updated_total > 0:
                    try:
                        # Pass the changed schemas so the backend invalidates ONLY those
                        # tenants' rel_type overlays (isolation; minimal rebuild). If the
                        # set is somehow empty, omit it → backend does a full reset.
                        _refresh_body = (
                            {"schemas": sorted(_changed_schemas)} if _changed_schemas else None
                        )
                        _r = httpx.post(
                            "http://faultline:8000/internal/refresh-intent-pattern-caches",
                            json=_refresh_body,
                            headers=_backend_auth_headers(),
                            timeout=5.0,
                        )
                        if _r.status_code == 200:
                            log.info(f"re_embedder.ontology_growth_cache_refresh_triggered "
                                     f"approved_any={_approved_any} sweep_updated={_sweep_updated_total} "
                                     f"changed_schemas={sorted(_changed_schemas)}")
                        else:
                            log.warning(f"re_embedder.ontology_growth_cache_refresh_failed status={_r.status_code}")
                    except Exception as _re:
                        log.warning(f"re_embedder.ontology_growth_cache_refresh_error: {_re}")

                # dprompt-065: Async taxonomy discovery for novel rel_types (deferred from ingest)
                # Runs in poll loop — no blocking LLM call in ingest hot path
                # Phase 3c: Wrap entire subsystem in error isolation to prevent crash
                #
                # PER-TENANT (tenancy-audit Gap 6): staged_facts/rel_types/entity_taxonomies
                # live in each USER schema (schema = scope, NO public). Running the anti-join
                # on the shared `db` (public search_path) reads public.staged_facts (empty
                # seed) → finds no novel rels → the tenant's novel rels never get a taxonomy
                # minted. We iterate ready_schemas on dedicated per-tenant connections with
                # `SET search_path TO {schema}` (NO public), keying the anti-join on the
                # tenant's own staged_facts/rel_types. entity_taxonomies changes → add the
                # touched schema to _changed_schemas so its taxonomy overlay is refreshed.
                try:
                    from src.api.main import _llm_discover_taxonomy_from_facts, _load_taxonomy_cache
                    from src.api.llm_client import build_llm_payload

                    # INGEST_ENABLED freeze: taxonomy discovery INSERTs entity_taxonomies
                    # rows (ontology growth) — iterate no schemas in knowledge-store mode.
                    for _tx_user_id, _tx_schema in (ready_schemas if ingest_enabled else []):
                        # LEDGER GATE — taxonomy discovery is one LLM call PER NOVEL REL.
                        if not _sweep.claim(_sweep_snap, _tx_user_id, "taxonomy_discovery"):
                            _sweep.log_skip(_sweep_snap, _tx_user_id, "taxonomy_discovery", _tx_schema)
                            continue
                        try:
                            with psycopg2.connect(postgres_dsn) as _tx_db:
                                with _tx_db.cursor() as _spc:
                                    _spc.execute(f"SET search_path TO {_tx_schema}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema from UUID-derived source with validation
                                _tx_db.commit()
                                # Attribute this tenant's LLM calls for the taxonomy-discovery
                                # LLM call (_llm_discover_taxonomy_from_facts) below.
                                _reembedder_bind_tenant(_tx_schema)

                                with _tx_db.cursor() as cur:
                                    cur.execute(
                                        "SELECT DISTINCT rel_type FROM staged_facts "
                                        "WHERE rel_type NOT IN (SELECT rel_type FROM rel_types) LIMIT 10"
                                    )
                                    novel_rels = [row[0] for row in cur.fetchall()]

                                if novel_rels:
                                    for rel_type in novel_rels:
                                        try:
                                            discovered = _llm_discover_taxonomy_from_facts(
                                                _tx_db, "system", [{"rel_type": rel_type}]
                                            )
                                            if discovered and discovered.get("taxonomy_name"):
                                                with _tx_db.cursor() as cur:
                                                    cur.execute(
                                                        "INSERT INTO entity_taxonomies "
                                                        "(taxonomy_name, description, member_entity_types, "
                                                        "rel_types_defining_group, has_transitivity, "
                                                        "transitive_rel_types, is_hierarchical, parent_rel_type, source) "
                                                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                                                        "ON CONFLICT (taxonomy_name) DO NOTHING",
                                                        (
                                                            discovered.get("taxonomy_name"),
                                                            discovered.get("description", ""),
                                                            discovered.get("member_entity_types", "{}"),
                                                            discovered.get("rel_types_defining_group", []),
                                                            discovered.get("has_transitivity", False),
                                                            discovered.get("transitive_rel_types", "{}"),
                                                            discovered.get("is_hierarchical", False),
                                                            discovered.get("parent_rel_type"),
                                                            "engine_learned_re_embedder",
                                                        ),
                                                    )
                                                _tx_db.commit()
                                                _load_taxonomy_cache(_tx_db)
                                                _changed_schemas.add(_tx_schema)
                                                log.info("re_embedder.taxonomy_discovered_async "
                                                        f"schema={_tx_schema} rel_type={rel_type} "
                                                        f"taxonomy={discovered.get('taxonomy_name')}")
                                        except Exception as e:
                                            log.warning("re_embedder.taxonomy_discovery_failed "
                                                       f"schema={_tx_schema} rel_type={rel_type} error={str(e)}")
                                _sweep.record_run(
                                    _tx_db, _tx_user_id, "taxonomy_discovery",
                                    _sweep_snap.observed_token(_tx_user_id, "taxonomy_discovery"))
                        except Exception as e:
                            log.error(f"re_embedder.taxonomy_discovery_per_tenant_error schema={_tx_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")
                except Exception as e:
                    log.error(f"re_embedder.taxonomy_discovery_subsystem_error (non-fatal): {type(e).__name__}: {str(e)[:200]}")
                    # Continue with next subsystem even if taxonomy discovery fails

                # dprompt-128-P3: Correction-signal evaluation MOVED into the per-tenant
                # ready_schemas loop above (runs on _ont_db with search_path = _schema).
                # It must NOT run here on the global `db` connection: candidates and
                # correction_signals/correction_patterns growth live in the USER schema,
                # so the public `db` read public.* (always empty) — the same severance
                # bug class documented for ontology_evaluations. Do not re-add it here.

                # PER-TENANT growth jobs (tenancy-audit Gaps 2–5). All four of the
                # following jobs read/write per-tenant tables (retraction_outcomes/
                # retraction_signals, entity_name_conflicts/entity_aliases,
                # extraction_patterns/extraction_pattern_matches) that live in each USER
                # schema (schema = scope, NO public). Their helper docstrings document
                # "search_path set by caller". Running them on the shared `db` (public
                # search_path) read public.* (the empty seed template) — the SAME
                # severance bug class already fixed for ontology_evaluations. We iterate
                # ready_schemas on a dedicated per-tenant connection with
                # `SET search_path TO {schema}` (NO public). Each job body is wrapped in
                # its own try/except (per-job non-fatal) inside the per-tenant try
                # (per-tenant non-fatal) so one failure never crashes the loop.
                #
                # evaluate_retraction_outcomes' negation_patterns writes are UNQUALIFIED
                # and so land in <schema>.negation_patterns under this per-tenant
                # search_path (NO public). Self-growth is per-tenant — it never writes
                # public (public is template/seed-source only).
                _ext_pattern_changed = False
                for _gw_user_id, _gw_schema in ready_schemas:
                    # LEDGER SEAT GATE (see the ontology loop above).
                    if not _sweep_snap.seat_has_work(_gw_user_id):
                        continue
                    try:
                        with psycopg2.connect(postgres_dsn) as _gw_db:
                            with _gw_db.cursor() as _spc:
                                _spc.execute(f"SET search_path TO {_gw_schema}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema from UUID-derived source with validation
                            _gw_db.commit()
                            # Attribute this tenant's LLM calls for the name-conflict
                            # arbitration LLM call (resolve_name_conflicts) below.
                            _reembedder_bind_tenant(_gw_schema)

                            # dprompt-137: Evaluate retraction outcomes for continuous learning
                            # Auto-register high-frequency patterns, update metrics for existing patterns
                            try:
                                _sw = "retraction_outcomes"
                                _sw_run = _reembedder_claim(_sweep_snap, _gw_user_id, _sw, _gw_schema)
                                if _sw_run and has_pending_retraction_outcomes(_gw_db):
                                    retraction_stats = evaluate_retraction_outcomes(_gw_db, frequency_threshold=3)
                                    if any(v > 0 for v in [retraction_stats["discovered"], retraction_stats["updated"]]):
                                        log.info(
                                            f"re_embedder.retraction_learning_complete "
                                            f"schema={_gw_schema} "
                                            f"discovered={retraction_stats['discovered']} "
                                            f"updated={retraction_stats['updated']} "
                                            f"errors={retraction_stats['errors']}"
                                        )
                                else:
                                    log.debug(f"re_embedder.no_pending_retraction_outcomes schema={_gw_schema}")
                                if _sw_run:
                                    _sweep.record_run(_gw_db, _gw_user_id, _sw,
                                                      _sweep_snap.observed_token(_gw_user_id, _sw))
                            except Exception as e:
                                _rollback_and_reapply_search_path(_gw_db, _gw_schema)
                                log.error(f"re_embedder.retraction_outcomes_subsystem_error schema={_gw_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                            # dprompt-121: Resolve name conflicts via LLM context evaluation
                            # Event-driven: only run if there are pending conflicts
                            # INGEST_ENABLED freeze: conflict arbitration mutates entities/
                            # entity_aliases (preferred flags, merges) — paused when frozen.
                            try:
                                _sw = "name_conflicts"
                                _sw_run = _reembedder_claim(_sweep_snap, _gw_user_id, _sw, _gw_schema)
                                if _sw_run and ingest_enabled and has_pending_name_conflicts(_gw_db):
                                    # READ BARRIER (RE-ARM): the read directly above re-opened a transaction AFTER the
                                    # block-level barrier, and the call below blocks. A latching check cannot see this;
                                    # measured live, it killed climb_classification_chains' connection twice.
                                    release_read_transaction(_gw_db, context="re_embedder.name_conflicts.pre_blocking_call")
                                    conflict_stats = resolve_name_conflicts(_gw_db, qwen_api_url)
                                    if conflict_stats["resolved"] > 0:
                                        log.info(
                                            f"re_embedder.name_conflicts_resolved "
                                            f"schema={_gw_schema} "
                                            f"resolved={conflict_stats['resolved']} "
                                            f"errors={conflict_stats['errors']} "
                                            f"skipped={conflict_stats['skipped']}"
                                        )
                                else:
                                    log.debug(f"re_embedder.no_pending_name_conflicts schema={_gw_schema}")
                                if _sw_run:
                                    _sweep.record_run(_gw_db, _gw_user_id, _sw,
                                                      _sweep_snap.observed_token(_gw_user_id, _sw))
                            except Exception as e:
                                _rollback_and_reapply_search_path(_gw_db, _gw_schema)
                                log.error(f"re_embedder.name_conflict_subsystem_error schema={_gw_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                            # ALIAS-PROVENANCE-DESIGN §3: Flag suspect preferred names (preferred
                            # aliases nobody ever chose). Flag-only — never auto-mutates names.
                            try:
                                _sw = "suspect_preferred_names"
                                _sw_run = _reembedder_claim(_sweep_snap, _gw_user_id, _sw, _gw_schema)
                                suspect_stats = (flag_suspect_preferred_names(_gw_db)
                                                 if _sw_run else {"flagged": 0})
                                if suspect_stats["flagged"] > 0:
                                    log.info(
                                        f"re_embedder.suspect_preferred_names_flagged "
                                        f"schema={_gw_schema} "
                                        f"count={suspect_stats['flagged']}"
                                    )
                                if _sw_run:
                                    _sweep.record_run(_gw_db, _gw_user_id, _sw,
                                                      _sweep_snap.observed_token(_gw_user_id, _sw))
                            except Exception as e:
                                _rollback_and_reapply_search_path(_gw_db, _gw_schema)
                                log.error(f"re_embedder.suspect_preferred_names_subsystem_error schema={_gw_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                            # Job 6: Evaluate extraction patterns for accuracy and bootstrap confidence
                            # Scoring phase: analyze user feedback on extraction patterns, update confidence scores
                            try:
                                _sw = "extraction_pattern_eval"
                                _sw_run = _reembedder_claim(_sweep_snap, _gw_user_id, _sw, _gw_schema)
                                pattern_stats = (evaluate_extraction_patterns(_gw_db)
                                                 if _sw_run else {})
                                _mutations = (
                                    pattern_stats.get("archived", 0)
                                    + pattern_stats.get("promoted", 0)
                                    + pattern_stats.get("confidence_updates", 0)
                                )
                                if _mutations > 0:
                                    _ext_pattern_changed = True
                                    log.info(
                                        f"re_embedder.extraction_pattern_eval "
                                        f"schema={_gw_schema} "
                                        f"evaluated={pattern_stats['evaluated']} "
                                        f"archived={pattern_stats['archived']} "
                                        f"promoted={pattern_stats['promoted']} "
                                        f"confidence_updates={pattern_stats['confidence_updates']} "
                                        f"errors={pattern_stats['errors']}"
                                    )
                                else:
                                    log.debug(f"re_embedder.no_pending_extraction_pattern_work schema={_gw_schema}")
                                if _sw_run:
                                    _sweep.record_run(_gw_db, _gw_user_id, _sw,
                                                      _sweep_snap.observed_token(_gw_user_id, _sw))
                            except Exception as e:
                                _rollback_and_reapply_search_path(_gw_db, _gw_schema)
                                log.error(f"re_embedder.extraction_pattern_subsystem_error schema={_gw_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")
                    except Exception as e:
                        try:
                            _gw_db.rollback()
                        except Exception:
                            pass
                        log.error(f"re_embedder.per_tenant_growth_error schema={_gw_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                # Signal backend to reload pattern caches once per cycle if any tenant's
                # extraction patterns actually changed (was posted per-eval before; now
                # coalesced across the per-tenant loop).
                if _ext_pattern_changed:
                    try:
                        refresh_resp = httpx.post(
                            f"http://faultline:8000/internal/refresh-intent-pattern-caches",
                            headers=_backend_auth_headers(),
                            timeout=5.0
                        )
                        if refresh_resp.status_code == 200:
                            log.info("re_embedder.pattern_cache_refresh_triggered")
                        else:
                            log.warning(f"re_embedder.pattern_cache_refresh_failed status={refresh_resp.status_code}")
                    except Exception as refresh_err:
                        log.warning(f"re_embedder.pattern_cache_refresh_error: {refresh_err}")

                # dprompt-153: Evict stale intent_pattern_cache entries
                # TTL-based: delete expired rows with confirmed_count < 3; grace-extend the rest.
                # PER-TENANT: intent_pattern_cache lives in each user's own schema (seeded +
                # grown per-tenant; public is template-only). Iterate ready_schemas on a
                # dedicated per-tenant connection (SET search_path TO {schema}, NO public) so
                # each tenant's cache is evicted in place — never public.
                _evict_deleted = 0
                _evict_extended = 0
                for _ev_user_id, _ev_schema in ready_schemas:
                    try:
                        with psycopg2.connect(postgres_dsn) as _ev_db:
                            with _ev_db.cursor() as cur:
                                cur.execute(f"SET search_path TO {_ev_schema}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema from UUID-derived source with validation
                                cur.execute("""
                                    DELETE FROM intent_pattern_cache
                                    WHERE is_permanent = false
                                      AND expires_at < now()
                                      AND confirmed_count < 3
                                """)
                                _evict_deleted += cur.rowcount
                                cur.execute("""
                                    UPDATE intent_pattern_cache
                                    SET expires_at = now() + INTERVAL '7 days'
                                    WHERE is_permanent = false
                                      AND expires_at IS NOT NULL
                                      AND expires_at < now()
                                      AND confirmed_count >= 3
                                """)
                                _evict_extended += cur.rowcount
                            _ev_db.commit()
                    except Exception as e:
                        log.warning(f"re_embedder.pattern_cache_eviction_failed schema={_ev_schema} (non-fatal): {type(e).__name__}: {str(e)[:100]}")
                if _evict_deleted > 0 or _evict_extended > 0:
                    log.info(f"re_embedder.pattern_cache_eviction deleted={_evict_deleted} extended={_evict_extended}")

                # Job 7: Fill in missing natural_language for rel_types in use.
                # Finds rel_types with NULL natural_language that appear in recent facts,
                # calls LLM to generate the template, stores it. Runs at most 5 per cycle
                # to avoid LLM saturation. Self-limiting: once filled, never runs again
                # for that rel_type.
                try:
                    # Job 7a (FIX 1b self-heal): mint stub rel_types rows for any
                    # rel_type that is REFERENCED by facts/staged_facts but has NO
                    # rel_types row at all (orphaned grown rels written before the
                    # ingest orphan-stub guard, e.g. coworker_of / favorite_*). Without
                    # a row they are invisible to the fill loop below and render
                    # verb-less forever. The stub carries label (de-snaked token) +
                    # NULL templates so the SAME fill loop then generates 3p/2p
                    # generatively next. Metadata-driven, no hardcoded rel names; the
                    # anti-join keys only on "referenced by a fact but missing a row".
                    #
                    # PER-TENANT: facts/staged_facts/rel_types live in each user schema
                    # (schema = scope, NO user_id col, NO public). The shared `db`
                    # connection has the default (public) search_path, so we iterate
                    # ready_schemas on dedicated per-tenant connections with
                    # `SET search_path TO {schema}` (NO public) — never anti-join against
                    # public.* (which is the empty seed template).
                    # PER-TENANT (tenancy-audit Gap 1, root cause of favorite_* NULL
                    # phrasing): the orphan-stub mint (Job 7a) AND the natural_language
                    # FILL (Job 7) both run inside this single per-tenant loop on one
                    # dedicated `_stub_db` connection with `SET search_path TO {schema}`
                    # (NO public). Previously the FILL ran on the shared `db` (public
                    # search_path) → it filled public.rel_types (the seed, always already
                    # complete) and was structurally incapable of seeing a tenant's grown
                    # rels, so favorite_movie/color/food/coworker_of stayed NULL forever.
                    # Stub-then-fill on the same tenant connection guarantees a freshly
                    # minted stub is fill-eligible the same cycle.
                    for _us_id, _us_schema in ready_schemas:
                        # LEDGER GATE — the FILL is one LLM call per un-phrased rel, and a rel
                        # the brain will not phrase stays NULL and is re-sent every cycle.
                        if not _sweep.claim(_sweep_snap, _us_id, "orphan_stub_and_nl_fill"):
                            _sweep.log_skip(_sweep_snap, _us_id, "orphan_stub_and_nl_fill", _us_schema)
                            continue
                        try:
                            with psycopg2.connect(postgres_dsn) as _stub_db:
                                with _stub_db.cursor() as _spc:
                                    _spc.execute(f"SET search_path TO {_us_schema}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema from UUID-derived source with validation
                                # Attribute this tenant's LLM calls for the natural-language
                                # phrasing FILL LLM call (generate_rel_type_phrasing) below.
                                _reembedder_bind_tenant(_us_schema)
                                # The open core runs one env-configured LLM, so the FILL lane is
                                # always open (the per-tenant brain pre-flight is closed-layer).
                                _nl_lane_open = True
                                # INGEST_ENABLED freeze: the stub mint INSERTs new rel_types
                                # rows (ontology growth) — skipped when frozen. The FILL below
                                # (presentation-metadata columns on EXISTING rows) keeps running.
                                _orphan_stubs = 0
                                if ingest_enabled:
                                    with _stub_db.cursor() as _scur:
                                        _scur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                                            """
                                            -- source MUST satisfy rel_types_source_check
                                            -- (wikidata|builtin|engine|user|expand); an
                                            -- engine-minted stub is 'engine'. A non-allowed
                                            -- literal fails the INSERT every cycle → the
                                            -- orphan rel never back-mints, renders verb-less.
                                            INSERT INTO rel_types (rel_type, label, confidence, source, engine_generated, created_at)
                                            SELECT used.rel_type,
                                                   initcap(replace(used.rel_type, '_', ' ')),
                                                   0.6, 'engine', true, now()
                                            FROM (
                                                SELECT DISTINCT lower(rel_type) AS rel_type FROM facts
                                                WHERE rel_type IS NOT NULL
                                                UNION
                                                SELECT DISTINCT lower(rel_type) AS rel_type FROM staged_facts
                                                WHERE rel_type IS NOT NULL
                                            ) AS used
                                            LEFT JOIN rel_types rt ON rt.rel_type = used.rel_type
                                            WHERE rt.rel_type IS NULL
                                              AND used.rel_type <> 'context'
                                            ON CONFLICT (rel_type) DO NOTHING
                                            """
                                        )
                                        _orphan_stubs = _scur.rowcount
                                    _stub_db.commit()
                                if _orphan_stubs and _orphan_stubs > 0:
                                    log.info(f"re_embedder.orphan_rel_stubs_minted schema={_us_schema} count={_orphan_stubs}")
                                    _changed_schemas.add(_us_schema)

                                # Job 7 FILL (per-tenant, same connection/search_path).
                                # Find this tenant's rel_types in active use that lack
                                # EITHER the 3p OR the 2p template. LIMIT 5 per tenant per
                                # cycle to avoid LLM saturation. Self-limiting: once
                                # filled, never re-selected for that rel_type.
                                # Not even SELECTED when the spend pre-flight is closed — the
                                # rows would be re-read and re-sent every cycle for a brain
                                # that cannot answer.
                                missing_nl = []
                                if _nl_lane_open:
                                    with _stub_db.cursor() as cur:
                                        cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                                            """SELECT rel_type FROM rel_types
                                               WHERE (natural_language IS NULL OR natural_language = ''
                                                      OR natural_language_2p IS NULL OR natural_language_2p = '')
                                               ORDER BY confidence DESC
                                               LIMIT 5"""
                                        )
                                        missing_nl = [row[0] for row in cur.fetchall()]

                                _nl_changed = False
                                for rt in missing_nl:
                                    try:
                                        # SINGLE SOURCE OF TRUTH: same generator the ingest
                                        # orphan-stub mint uses (transport-parity — no divergent
                                        # prompt/validation copies, no hardcoded timeout). The
                                        # helper validates placeholders (X in 3p; Y-and-not-X in 2p)
                                        # and returns only the keys that passed; failures yield {}.
                                        # Uses LLMTimeouts/LLMMaxTokens via operation NATURAL_LANGUAGE_FILL.
                                        # PER-TENANT: `_us_id` IS the seat of `_us_schema` —
                                        # the loop already carries it (it is the ledger key one
                                        # screen up), so the phrasing call is attributed to the
                                        # tenant whose rel_type is being phrased and whose brain
                                        # is bound. This is the lane that produced the prod
                                        # `seat=re_embed op=NATURAL_LANGUAGE_FILL` noise.
                                        _phrasing = generate_rel_type_phrasing(rt, user_id=_us_id)
                                        nl = _phrasing.get("natural_language", "")
                                        nl_2p = _phrasing.get("natural_language_2p", "")
                                        if nl:
                                            with _stub_db.cursor() as cur:
                                                cur.execute(
                                                    "UPDATE rel_types SET natural_language = %s"
                                                    " WHERE rel_type = %s AND (natural_language IS NULL OR natural_language = '')",
                                                    (nl, rt),
                                                )
                                            _stub_db.commit()
                                            _nl_changed = True
                                            log.info(f"re_embedder.natural_language_filled schema={_us_schema} rel_type={rt} value={nl!r}")
                                        if nl_2p:
                                            with _stub_db.cursor() as cur:
                                                cur.execute(
                                                    "UPDATE rel_types SET natural_language_2p = %s"
                                                    " WHERE rel_type = %s AND (natural_language_2p IS NULL OR natural_language_2p = '')",
                                                    (nl_2p, rt),
                                                )
                                            _stub_db.commit()
                                            _nl_changed = True
                                            log.info(f"re_embedder.natural_language_2p_filled schema={_us_schema} rel_type={rt} value={nl_2p!r}")
                                    except Exception as nl_err:
                                        try:
                                            _stub_db.rollback()
                                            # search_path is reset by rollback (psycopg2) — re-apply
                                            # so the next fill row targets the tenant schema, not public.
                                            with _stub_db.cursor() as _spc2:
                                                _spc2.execute(f"SET search_path TO {_us_schema}")
                                        except Exception:
                                            pass
                                        log.warning(f"re_embedder.natural_language_fill_failed schema={_us_schema} rel_type={rt}: {nl_err}")
                                if _nl_changed:
                                    _changed_schemas.add(_us_schema)
                                # Recorded ONLY when the FILL half actually ran. A pre-flight
                                # skip leaves the token dirty on purpose: the un-phrased rels are
                                # still owed, and marking them done would hide them for up to
                                # REEMBED_SWEEP_MAX_INTERVAL after the tenant adds a brain.
                                if _nl_lane_open:
                                    _sweep.record_run(
                                        _stub_db, _us_id, "orphan_stub_and_nl_fill",
                                        _sweep_snap.observed_token(_us_id, "orphan_stub_and_nl_fill"))
                        except Exception as _stub_err:
                            log.warning(f"re_embedder.orphan_rel_stub_job_failed (non-fatal) schema={_us_schema}: {_stub_err}")
                except Exception as e:
                    log.warning(f"re_embedder.natural_language_job_error (non-fatal): {e}")

                # The per-tenant LLM growth loops above are done — clear the last tenant's
                # attribution so the between-cycle / reconcile work below is never attributed
                # to a stale tenant.
                _reembedder_clear_tenant()

                # Superseded / hard-delete Qdrant passes REMOVED (tenancy-audit Gap 7).
                # They ran on the shared `db` (public search_path) and did
                # `SELECT id, user_id FROM facts ...` — which resolved to public.facts
                # (the empty seed template), always 0 rows → dead code that never deleted
                # anything for any tenant. They also deleted by BARE point-id
                # (`derive_qdrant_point_id("facts", fact_id)` only), which violates the
                # documented collision-safe `(source_table, fact_id)` payload-filter rule
                # (facts & staged_facts share a per-user collection). Rather than revive a
                # collision-unsafe delete, we rely on `reconcile_qdrant` below, which IS
                # per-collection and deletes `reason=superseded` / `reason=not_in_pg`
                # using the collision-safe filter.

                # Reconciliation pass — sync stale payloads and orphaned points.
                # CADENCE-GATED (task §3): the full per-collection Qdrant scroll runs only
                # when a tenant had work this cycle (activity-driven) OR the coarse
                # max-interval ceiling has elapsed (never starves cleanup). A perfectly
                # idle rig scrolls at most once per `reconcile_max_interval`, not every cycle.
                _now_mono = time.monotonic()
                _reconcile_due = (
                    _active_tenants_this_cycle > 0
                    or (_now_mono - _last_reconcile_at) >= reconcile_max_interval
                )
                if _reconcile_due:
                    stats = reconcile_qdrant(db, qdrant_url, qwen_api_url)
                    _last_reconcile_at = _now_mono
                    if any(v > 0 for v in [stats["deleted"], stats["reupserted"], stats["errors"]]):
                        log.info(f"re_embedder.reconcile deleted={stats['deleted']} reupserted={stats['reupserted']} ok={stats['ok']} errors={stats['errors']}")
                else:
                    log.debug(
                        "re_embedder.reconcile.skipped_idle "
                        f"elapsed={int(_now_mono - _last_reconcile_at)}s "
                        f"max_interval={reconcile_max_interval}s active_tenants=0"
                    )

        except Exception as e:
            log.error(f"re_embedder.loop_error: {e}")

        time.sleep(interval)


def extract_retraction_pattern(text: str, rel_type: str, action: str, user_id: str,
                               llm_url: str, db_conn) -> dict:
    """
    Extract reusable negation pattern from a user retraction.

    Args:
        text: user's retraction message
        rel_type: relationship type being retracted (if known)
        action: "delete", "correct", "negate", "supersede"
        user_id: user UUID
        llm_url: LLM endpoint URL
        db_conn: database connection for context

    Returns:
        dict with: pattern_text, pattern_type, negation_type, confidence
        or None if extraction failed
    """
    try:
        from src.api.llm_client import build_llm_payload, get_llm_headers
        from src.api.llm_calls import call_llm_with_retry_sync, LLMTimeouts

        # READ BARRIER: this helper is handed a caller-owned connection and then blocks on
        # an LLM call. Whatever read the caller left open must not ride across it.
        release_read_transaction(db_conn, context="re_embedder.extract_retraction_pattern")

        messages = [
            {
                "role": "system",
                "content": f"{_FAULTLINE_INTERNAL_PREFIX} You are a pattern learner. Extract reusable negation patterns from retractions. Respond only with valid JSON, no markdown."
            },
            {
                "role": "user",
                "content": f"""Extract the negation pattern from this user retraction:

Text: {text}
Relationship: {rel_type if rel_type else 'unknown'}
Action: {action}

Respond with ONLY this JSON structure:
{{
  "pattern_text": "normalized_pattern_string",
  "pattern_type": "substring|semantic",
  "negation_type": "deletion|negation|correction|general",
  "confidence": 0.0 to 1.0
}}

Rules:
- pattern_text: lowercase, underscores for spaces, no special chars, 4-50 chars
- pattern_type: "substring" for explicit markers like "forget about", "semantic" for complex patterns
- negation_type: "deletion" (remove fact), "negation" (fact is false), "correction" (replace with new), "general" (unclear)
- confidence: 0.90+ for clear patterns, 0.70-0.89 for probable, <0.70 for uncertain

Respond with ONLY the JSON, no explanation."""
            }
        ]

        result = call_llm_with_retry_sync(
            messages=messages,
            model=LLMModels.get("PATTERN_EXTRACTION"),
            user_id=user_id,
            timeout=LLMTimeouts.get("ENRICHMENT"),
            operation="pattern_extraction",
        )

        # Validate required fields
        required = ["pattern_text", "pattern_type", "negation_type", "confidence"]
        for field in required:
            if field not in result:
                log.warning(f"re_embedder.pattern_extraction_missing_field field={field} rel_type={rel_type}")
                return None

        # Normalize pattern_text
        pattern_text = result.get("pattern_text", "").lower()
        pattern_text = re.sub(r'[^a-z0-9_]', '_', pattern_text)  # Only alphanumerics and underscores
        pattern_text = re.sub(r'_+', '_', pattern_text)  # Remove duplicate underscores
        pattern_text = pattern_text.strip('_')  # Strip leading/trailing underscores

        if not pattern_text or len(pattern_text) < 3:
            log.warning(f"re_embedder.pattern_text_invalid_after_normalization original={result.get('pattern_text')}")
            return None

        result["pattern_text"] = pattern_text
        return result

    except Exception as e:
        log.warning(f"re_embedder.pattern_extraction_failed rel_type={rel_type} error={e}")
        return None


def store_retraction_pattern(pattern_text: str, pattern_type: str, negation_type: str,
                             confidence: float, db_conn) -> bool:
    """
    Store learned retraction pattern in negation_patterns table.
    Per-user schema: patterns are isolated by schema, not by user_id column.

    Args:
        pattern_text: normalized pattern string
        pattern_type: "substring" or "semantic" (mapped to learned_from)
        negation_type: "deletion", "negation", "correction", or "general"
        confidence: confidence score (0.0-1.0)
        db_conn: database connection

    Returns:
        True if stored successfully, False otherwise
    """
    try:
        with db_conn.cursor() as cur:
            cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — multi-line execute with schema from UUID-derived source

                """INSERT INTO negation_patterns
                   (pattern_text, negation_type, learned_from, confidence, confirmed_count)
                   VALUES (%s, %s, %s, %s, 1)
                   ON CONFLICT (pattern_text, negation_type) DO UPDATE SET
                       confirmed_count = negation_patterns.confirmed_count + 1,
                       confidence = GREATEST(negation_patterns.confidence, EXCLUDED.confidence)
                """,
                (pattern_text, negation_type, pattern_type, confidence),
            )

        db_conn.commit()
        # stdlib logger (logging.getLogger) — use f-string form, NOT structlog kwargs.
        # Passing pattern=/negation_type= as kwargs reaches Logger._log() which rejects
        # them ("unexpected keyword argument 'pattern'"), raising AFTER the commit and
        # making the caller report pattern_learning_failed though the row was stored.
        log.info(
            f"re_embedder.pattern_stored pattern={pattern_text} "
            f"negation_type={negation_type} confidence={confidence}")
        return True

    except Exception as e:
        log.error(
            f"re_embedder.pattern_storage_failed pattern={pattern_text} error={str(e)}")
        try:
            db_conn.rollback()
        except Exception:
            pass
        return False


if __name__ == "__main__":
    # THIS PROCESS IS DEFERRABLE UPKEEP. Every LLM call it makes must yield to calls a user
    # is actually waiting on: it takes only the capacity interactive traffic left behind,
    # and when there is none it DEFERS to a later sweep instead of firing into a provider
    # that is already refusing us.
    #
    # Declared HERE, not at module import, on purpose — the API process imports symbols from
    # this module, and marking that process background would defer the user's own calls. A
    # process-level default also reaches the sweep's worker threads, which a ContextVar set
    # in main() would not. Genuinely user-driven work inside this process (e.g. draining a
    # document someone just uploaded) re-enters the interactive lane via llm_lane.use_lane.
    _llm_lane.set_process_default(_llm_lane.LANE_BACKGROUND)
    # OBSERVABILITY (load-bearing): this is set at RUNTIME via os.environ, so it is invisible
    # in /proc/<pid>/environ — that file is the env the process was STARTED with and never
    # reflects a later write. Without this line the only way to check whether the sweep is
    # actually deferring is to wait for a rate-limit storm. Log it once, at start-up.
    log.info(f"re_embedder.llm_lane_declared lane={_llm_lane.current_lane()} "
             f"is_background={_llm_lane.is_background()} "
             f"note=deferrable upkeep; yields to interactive traffic and defers when the "
             f"daily budget is spent")
    # STRUCTURAL FLOOD CHECK (dprompt-155 §2.3, the trap that already burned us): at a mint
    # interval of 1/rate, any interactive-lane queue deeper than ~max_wait*rate CANNOT be
    # served within the cap and those callers fail OPEN — uncapped fire into a provider
    # already refusing us. Background callers defer (safe); this names the residual
    # INTERACTIVE exposure at start-up so an operator can see the shape before the storm.
    try:
        # The configured rate (LLM_MAX_RPM) and max wait (LLM_RATE_MAX_WAIT_S), read through
        # the ONE owner of both, src/api/llm_rate.py — no second copy of the defaults.
        _rpm = float(llm_rate.rate_per_sec()) * 60.0
        _mint_s = 60.0 / float(_rpm)
        _wait_s = float(llm_rate.max_wait_s())
        if _wait_s < 2.0 * _mint_s:
            from src.api.logging_config import log_crit
            log_crit(log, "re_embedder.rate_flood_shape_detected",
                     effective_rpm=_rpm, token_mint_interval_s=round(_mint_s, 2),
                     max_wait_s=_wait_s,
                     note=f"LLM_RATE_MAX_WAIT_S={_wait_s}s is under 2x the mint interval "
                          f"(60/{_rpm:.0f}s) at the configured rate — interactive queues "
                          f"deeper than ~{int(_wait_s / _mint_s)} call(s) WILL fail open "
                          f"uncapped; background lanes defer safely. Raise LLM_RATE_MAX_WAIT_S "
                          f"or accept the interactive exposure.")
    except Exception as _flood_err:  # noqa: BLE001 — diagnostics must never block start-up
        log.warning(f"re_embedder.rate_flood_shape_check_skipped: {_flood_err}")
    if str(os.environ.get("REEMBEDDER_SUPERVISOR", "false")).strip().lower() in ("1", "true", "yes", "on"):
        # OPT-IN event-driven supervisor (Deliverable C): one asyncio worker per active tenant,
        # comparator-driven (idle ⇒ zero LLM calls) and yield-gated to interactive traffic.
        # Unset/false (default) → the legacy single-threaded main() loop runs byte-for-byte.
        import asyncio
        from src.re_embedder.supervisor import run_supervisor
        _sup_postgres_dsn = os.getenv("POSTGRES_DSN")
        _sup_qdrant_url = os.getenv("QDRANT_URL", "http://qdrant:6333")
        _sup_backend_api_url = os.getenv("FAULTLINE_API_URL", "http://faultline:8000").rstrip("/")
        asyncio.run(run_supervisor(_sup_postgres_dsn, _sup_backend_api_url, _sup_qdrant_url))
    else:
        main()