"""
FaultLine Re-Embedder Service

Polls the facts table for unsynced rows, embeds them, and upserts to per-user Qdrant collections.
This is the only service that writes to Qdrant.
"""
import atexit
import hashlib
import json
import logging
import os
import re
import time
import uuid
from typing import Optional

import httpx
import psycopg2
import redis
from src.api.llm_client import get_llm_headers, GATE_MIN, GATE_MAX, GATE_DEFAULT, clamp_gate
from src.api.llm_calls import (
    call_llm_with_retry_sync,
    close_llm_http_client,
    generate_rel_type_phrasing,
    LLMTimeouts,
    LLMModels,
)
from src.entity_registry.registry import preference_rank, EntityRegistry

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
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
            _spc.execute(f"SET search_path TO {schema_name}")
    except Exception as sp_err:
        log.warning(f"re_embedder.search_path_reapply_failed schema={schema_name}: {sp_err}")


# Embedding model name — PURE CONFIG, read from env (no code literal). Default lives in
# .env.example. Empty when unset; the LOCAL fastembed CPU path needs no model name, and the
# external-API fallback fail-safes (hash vector / None) rather than crashing the loop.
_EMBEDDING_MODEL = (os.getenv("EMBEDDING_MODEL") or "").strip()


def _flag(name: str, default: str = "true") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# ── RUNG-6 bounded-growth flags (DEV/DESIGN-hierarchy-ladder-and-growth.md) ─────────────────────
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

# Hierarchy rel_types (rung-4 closed set, DESIGN §"The deterministic resolution ladder"). Used by
# convergence + bridging validation. Mechanism, not ontology CONTENT — these are the structural
# classification rels, identical to the closed set the canonical ladder enforces.
_HIERARCHY_RELS = ("instance_of", "is_a", "subclass_of", "part_of", "member_of")

# THE HARD LINE — the SKOS naming/label rels whose OBJECT is a NAME (a memory: Rex,
# Apollo, "Alex"), NOT a type. An alias registered as the object of one of these edges is a
# proper name and is NEVER a valid L4 classification subject — the subclass_of ladder hangs
# off the TYPE node (dog, computer), never off the name. Same fixed SKOS-identity invariant the
# rest of the codebase pins (main._ALIAS_BACKED_NAME_RELS = pref_name/also_known_as, the
# skos:prefLabel/skos:altLabel pair) — NOT a growable ontology axis, so naming it here is the
# existing SKOS convention, not a domain-specific hardcode. Used by the climb to (a) pick the
# TYPE-bearing alias for classification and (b) refuse to climb a pure named instance into L4.
_NAMING_RELS = ("pref_name", "also_known_as")

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
        log.info("re_embedder.nesting_growth_refused_user_severed",
                 parent=parent_taxonomy, child=child_taxonomy)
        return False
    try:
        cur.execute(
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
        log.warning("re_embedder.add_member_taxonomy_failed",
                    parent=parent_taxonomy, child=child_taxonomy, error=str(e)[:120])
        return False


def fetch_unsynced(db_conn, user_id: str, confidence_threshold: float = 0.0) -> list[dict]:
    """Fetch all non-superseded facts where qdrant_synced = false and confidence >= threshold.

    Per-user schema context: user_id is passed as parameter (schema provides isolation).
    """
    with db_conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, subject_id, object_id, rel_type, provenance,
                   confidence, confirmed_count, last_seen_at, contradicted_by,
                   fact_class
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
        }
        for row in rows
    ]


def fetch_unsynced_staged(db_conn, user_id: str) -> list[dict]:
    """Fetch staged_facts where qdrant_synced = false and not yet promoted or expired.

    Per-user schema context: user_id is passed as parameter (schema provides isolation).
    """
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, subject_id, object_id, rel_type, provenance,
                   confidence, confirmed_count, last_seen_at, fact_class
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
        embed_url = qwen_api_url.replace("/chat/completions", "/embeddings")

    try:
        if _http_client_sync:
            response = _http_client_sync.post(
                embed_url,
                json={"model": _EMBEDDING_MODEL, "input": text},
                headers=get_llm_headers(),
                timeout=timeout,
            )
        else:
            response = _http_client.post(
                embed_url,
                json={"model": _EMBEDDING_MODEL, "input": text},
                headers=get_llm_headers(),
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

    def _create_collection() -> bool:
        """PUT the collection with the correct anonymous-vector schema."""
        create_response = httpx.put(
            f"{qdrant_url}/collections/{collection}",
            json={"vectors": _CORRECT_SCHEMA},
            timeout=10.0,
        )
        if create_response.status_code == 200:
            log.info(f"re_embedder.collection_created collection={collection}")
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
            timeout=10.0
        )

        if response.status_code == 200:
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
    }
    # Collision-free point id derived from (source_table, fact_id) — never the bare
    # table id, which would alias facts#N onto staged#N in the shared collection.
    point_id = derive_qdrant_point_id(source_table, row["id"])
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
                timeout=30.0
            )

        if response.status_code == 200:
            return True

        log.error(f"re_embedder.qdrant_error fact_id={row['id']} status={response.status_code} body={response.text}")
        return False

    except Exception as e:
        log.error(f"re_embedder.qdrant_error fact_id={row['id']}: {e}")
        return False


def mark_synced(db_conn, fact_id: int) -> None:
    """Mark a fact as synced to Qdrant."""
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE facts SET qdrant_synced = true WHERE id = %s",
            (fact_id,)
        )
    db_conn.commit()


def promote_facts(db_conn) -> None:
    """Promote facts to long-term memory by increasing confidence for eligible facts."""
    with db_conn.cursor() as cur:
        # Increase confidence for facts that have been confirmed multiple times or are old
        cur.execute(
            """
            UPDATE facts
            SET confidence = LEAST(confidence + 0.1, 1.0)
            WHERE superseded_at IS NULL
            AND (confirmed_count >= 2 OR last_seen_at < now() - interval '7 days')
            AND confidence < 1.0
            """
        )
        promoted_count = cur.rowcount
        if promoted_count > 0:
            log.info(f"re_embedder.promoted {promoted_count} facts to long-term memory")
    db_conn.commit()


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
                cur.execute(f"SET search_path TO {schema_name}")

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
                        log.error("reconcile_hierarchy.candidate_query_failed",
                                  schema=schema_name, type_name=type_name, error=str(_qe))
                        continue

                    for (entity_id,) in cur.fetchall():
                        try:
                            cur.execute("""
                                INSERT INTO staged_facts
                                    (subject_id, object_id, rel_type, fact_class, provenance, confidence,
                                     first_seen_at, expires_at)
                                VALUES (%s, %s, 'instance_of', 'B', 'hierarchy_reconciliation', 0.6,
                                        now(), now() + interval '30 days')
                                ON CONFLICT (subject_id, object_id, rel_type)
                                DO UPDATE SET last_seen_at = now(),
                                    confirmed_count = staged_facts.confirmed_count + 1,
                                    expires_at = COALESCE(staged_facts.expires_at,
                                                          now() + interval '30 days')
                            """, (entity_id, node_id))
                            created += 1
                        except Exception as _ie:
                            log.error("reconcile_hierarchy.insert_failed",
                                      schema=schema_name, entity_id=entity_id, node_id=node_id,
                                      error=str(_ie))
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
                cur.execute(f"SET search_path TO {schema_name}")
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
        schema_name: User schema name (e.g., "faultline_christopher"). If provided, sets search_path.
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
                    cur.execute(f"SET search_path TO {schema_name}")
            except Exception as e:
                log.warning(f"re_embedder.search_path_setup_failed schema={schema_name}: {e}")
                # Continue with current search_path

        # C→B upgrade: Class C facts that have accumulated enough confirmations graduate
        # to Class B and enter the B→facts promotion pipeline in this same cycle.
        # expires_at extended so they survive long enough to reach the next threshold.
        try:
            with db_conn.cursor() as cur:
                cur.execute(
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
            cur.execute(
                """
                SELECT id, subject_id, object_id, rel_type,
                       provenance, COALESCE(fact_provenance, 'llm_inferred'), confidence,
                       temporal_status, event_date, event_date_granularity
                FROM staged_facts
                WHERE fact_class = 'B'
                  AND confirmed_count >= %s
                  AND promoted_at IS NULL
                """,
                (promotion_threshold,)
            )
            candidates = cur.fetchall()

        for row in candidates:
            sid, subject, obj, rel_type, prov, fact_prov, conf, temporal_status, event_date, event_date_granularity = row
            # user_id is implicit in per-user schema context (set by SET search_path)
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
                        "  temporal_status, event_date, event_date_granularity)"
                        " VALUES (%s, %s, %s, %s, %s, 'B', %s, false,"
                        "  COALESCE(%s, 'now'), %s, %s)"
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
                        "   event_date_granularity = COALESCE(EXCLUDED.event_date_granularity, facts.event_date_granularity)",
                        (subject, obj, rel_type, prov, conf, fact_prov,
                         temporal_status, event_date, event_date_granularity)
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
                    collection = derive_collection(user_id)
                    httpx.post(
                        f"{qdrant_url}/collections/{collection}/points/delete",
                        json={"points": [derive_qdrant_point_id("staged_facts", sid)]},
                        timeout=5.0
                    )
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
            cur.execute(
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
            cur.execute(
                """
                SELECT id FROM staged_facts
                WHERE fact_class    = 'C'
                  AND confirmed_count <= 0
                  AND expires_at   <= now()
                  AND promoted_at IS NULL
                """
            )
            stale = cur.fetchall()

        collection = derive_collection(user_id) if user_id else os.getenv("QDRANT_COLLECTION", "faultline-test")
        for (staged_id,) in stale:
            try:
                httpx.post(
                    f"{qdrant_url}/collections/{collection}/points/delete",
                    json={"points": [derive_qdrant_point_id("staged_facts", staged_id)]},
                    timeout=10.0,
                )
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
            cur.execute(
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

        if not idle_rows:
            return stats

        collection = derive_collection(user_id) if user_id else os.getenv("QDRANT_COLLECTION", "faultline-test")

        for staged_id, hit_count in idle_rows:
            try:
                new_hits = (hit_count if hit_count is not None else 1) - 1
                if new_hits <= 0:
                    # DROP — best-effort Qdrant delete first (match expiry pattern), then row.
                    try:
                        httpx.post(
                            f"{qdrant_url}/collections/{collection}/points/delete",
                            json={"points": [derive_qdrant_point_id("staged_facts", staged_id)]},
                            timeout=10.0,
                        )
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
                        cur.execute(
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
                    cur.execute(f"SET search_path TO {schema_name}")
                db_conn.commit()
            except Exception as e:
                log.warning(f"re_embedder.class_c_promote_search_path_failed schema={schema_name}: {e}")
                # Continue with current search_path

        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, subject_id, object_id, rel_type, provenance,
                       COALESCE(fact_provenance, 'llm_inferred'),
                       confidence, hit_count, rel_type_definition,
                       temporal_status, event_date, event_date_granularity
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

        if not candidates:
            return promoted

        for row in candidates:
            sid, subject, obj, rel_type, prov, fact_prov, conf, hits, rel_def, temporal_status, event_date, event_date_granularity = row
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
                        "  temporal_status, event_date, event_date_granularity)"
                        " VALUES (%s, %s, %s, %s, %s, 'B', %s, false,"
                        "  COALESCE(%s, 'now'), %s, %s)"
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
                        "   event_date_granularity = COALESCE(EXCLUDED.event_date_granularity, facts.event_date_granularity)",
                        (subject, obj, rel_type, prov, promote_conf, fact_prov,
                         temporal_status, event_date, event_date_granularity)
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
                    collection = derive_collection(user_id)
                    httpx.post(
                        f"{qdrant_url}/collections/{collection}/points/delete",
                        json={"points": [derive_qdrant_point_id("staged_facts", sid)]},
                        timeout=5.0
                    )
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
    except Exception as e:
        log.warning(f"re_embedder.reconcile_active_users_failed (using all collections): {e}")
        try:
            db_conn.rollback()
        except Exception:
            pass
        active_user_ids = None

    try:
        response = httpx.get(f"{qdrant_url}/collections", timeout=10.0)
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

    if not collections:
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
    except Exception as e:
        log.warning(f"re_embedder.reconcile_schema_enum_failed (processing all collections): {e}")
        try:
            db_conn.rollback()
        except Exception:
            pass
        existing_schemas = None

    log.info(f"re_embedder.reconcile_start collections={len(collections)}")

    # Process each collection
    for collection in collections:
        try:
            # ORPHAN-SKIP: per-tenant collection whose PG schema is gone → don't scroll it.
            if not should_reconcile_collection(collection, existing_schemas):
                log.info(f"re_embedder.reconcile_skip_orphan collection={collection} "
                         f"reason=no_pg_schema")
                continue
            # Step 2: Scroll all points with payload but no vectors
            all_points = []
            next_page_offset = None

            while True:
                scroll_body = {
                    "limit": 250,
                    "with_payload": True,
                    "with_vector": False,
                }
                if next_page_offset is not None:
                    scroll_body["offset"] = next_page_offset

                response = httpx.post(
                    f"{qdrant_url}/collections/{collection}/points/scroll",
                    json=scroll_body,
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

            # Set search_path to user's schema derived from collection name
            # Collection format: "faultline-{user_id}" or "faultline-test"
            _collection_user_id = collection.replace("faultline-", "", 1)
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

                placeholders = ",".join(["%s"] * len(fact_ids))
                # Query BOTH facts and staged_facts (Class B/C live in staged_facts)
                cur.execute(
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
                for row in cur.fetchall():
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

            # Step 4: Reconcile each point
            for point in all_points:
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
                            json={"points": [point_id]},
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
                            json={"points": [point_id]},
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
                            json={"points": [point_id]},
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
                        try:
                            httpx.put(
                                f"{qdrant_url}/collections/{collection}/points",
                                json={"points": [{"id": point_id, "vector": vector, "payload": _reupsert_payload}]},
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
                                      snippet: str, qwen_api_url: str) -> dict:
    """
    dprompt-126: Phase 2 — Query LLM for natural language metadata during ontology evaluation.

    When a novel rel_type reaches occurrence_count >= 3, query the LLM to generate:
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
            user_id="re_embedder",
            timeout=LLMTimeouts.get("ENRICHMENT"),
            operation="ENRICHMENT",
        )

        # call_llm_with_retry_sync() returns PARSED JSON (not raw OpenAI response)
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
# (DEV/DESIGN-hierarchy-ladder-and-growth.md §"On a MISS: push back to the LLM and CLASSIFY")
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
            user_id="re_embedder",
            timeout=LLMTimeouts.get("ENRICHMENT"),
            operation="ENRICHMENT",
        )
        if not result or not isinstance(result, dict):
            return None

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
            user_id="re_embedder",
            timeout=LLMTimeouts.get("CLASSIFY_CHAIN"),
            operation="CLASSIFY_CHAIN",
            max_tokens=LLMMaxTokens.get("CLASSIFY_CHAIN"),
        )
        if not result or not isinstance(result, dict):
            return None

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
    except Exception as e:
        log.warning(f"re_embedder.whatis_chain_query_failed concept={c[:40]} error={type(e).__name__}: {str(e)[:100]}")
        return None


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
    except Exception as e:
        if schema_name:
            _rollback_and_reapply_search_path(db_conn, schema_name)
        log.error(f"re_embedder.whatis_fetch_failed: {e}")
        return stats

    if not rows:
        return stats

    log.info(f"re_embedder.whatis_candidates count={len(rows)}")

    for _row_id, _concept, _snippet in rows:
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
            if _cid and _is_named_instance(db_conn, _cid):
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

            proposal = _query_llm_what_is(concept, qwen_api_url, context=_context)
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


def _is_named_instance(db_conn, entity_id: str) -> bool:
    """THE HARD LINE — is `entity_id` a NAMED INSTANCE (a memory) rather than a TYPE (an L4 place)?

    A named instance must NEVER receive a `subclass_of` edge: a name never becomes a place. The
    test is purely STRUCTURAL (edge/graph + naming-layer membership) — NO proper-noun word list,
    NO capitalization heuristic, NO entity-name literal — so it is subject-agnostic and deterministic:

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
    out = {"placed": 0, "cycle_skipped": 0, "terminated": False, "quarantined": False}
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
            # Nothing new to place (all rungs cycle-skipped or pre-existing) → no supersede.
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
        return {"placed": 0, "cycle_skipped": 0, "terminated": False, "quarantined": False}

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
             "skipped_cached": 0, "errors": 0}
    if not _ENGINE_CLASSIFY_CLIMB:
        return stats
    if not user_id or not schema_name:
        return stats  # need a bound tenant + user to resolve UUIDs / overlay

    # DEFENSIVE ENTRY-ROLLBACK (same rationale as classify_unknown_concepts): the climb runs LAST on
    # the shared poll-loop connection, so an aborted txn inherited from an earlier subsystem makes the
    # subclass_of check below fail "current transaction is aborted" and the climb returns early EVERY
    # cycle — the un-laddered concepts never deepen. Clear inherited aborted state up front. Fail-safe.
    _rollback_and_reapply_search_path(db_conn, schema_name)

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

    advanced = 0
    for leaf_id in leaves:
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
            if _is_named_instance(db_conn, leaf_id):
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
                full_chain = _query_llm_full_chain(leaf_name, qwen_api_url)
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
                    if fc.get("placed", 0) > 0:
                        stats["climbed"] += 1
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
                proposal = _query_llm_what_is(leaf_name, qwen_api_url)
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
            # BACKSTOP termination: ±6 hop cap reached without a seeded root → quarantine
            # this chain's tip (queue for whatis reclassify) and STOP generating on it.
            if hit_cap:
                _quarantine_climb_tip(db_conn, tip_id, tip_name)
                stats["quarantined"] += 1
                _climb_state_record(db_conn, leaf_id, "unplaceable", "cap_hit", _fp)
                continue

            # ONE-SHOT FULL CHAIN from the tip (PRIMARY): ask the LLM ONCE for the COMPLETE
            # remaining ladder above the tip and place EVERY rung in one pass (cycle-guarded,
            # terminate at a seeded root, else quarantine the final tip). No too-direct edge to
            # supersede here (old_root_name=None) — we are EXTENDING an un-grounded tip upward.
            full_chain = _query_llm_full_chain(tip_name, qwen_api_url)
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
                if fc.get("placed", 0) > 0:
                    stats["climbed"] += 1
                    if fc.get("terminated"):
                        stats["terminated"] += 1
                    if fc.get("quarantined"):
                        stats["quarantined"] += 1
                    _climb_state_record(db_conn, leaf_id, "placed", "placed", _fp)
                    continue
                # else fall through to the single-rung climb FALLBACK below.

            # SINGLE-RUNG CLIMB (FALLBACK): ask the LLM "what is <tip>?" (proposes a parent).
            proposal = _query_llm_what_is(tip_name, qwen_api_url)
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
                # Unresolved parent → MINT-AND-QUARANTINE (never auto-place). The async
                # whatis classifier types/places it next cycle; once it becomes a real
                # backbone node, this chain advances onto it on a later pass (identity).
                # CACHE 'unplaceable'/no_info_root — the proposed parent is not (yet) a backbone
                # node; re-open when growth makes it one (fingerprint change) or after backoff.
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


def _quarantine_climb_tip(db_conn, tip_id: Optional[str], tip_name: str) -> None:
    """MINT-AND-QUARANTINE a chain tip (or an unresolved proposed parent) for later grounding.

    Writes the SAME `ingest_miss_pushback` ontology_evaluations row the async whatis classifier
    consumes (extraction_method='ingest_miss_pushback', re_embedder_decision IS NULL,
    sample_object=<name>) so it is TYPED/PLACED on a later cycle — never auto-placed here.
    Idempotent-ish (only inserts when no undecided row exists for the name). Fail-safe."""
    name = (tip_name or "").strip().lower()
    if not name:
        return
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
# RUNG-6 self-assembling backbone (DEV/DESIGN-hierarchy-ladder-and-growth.md §"Growth")
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
            cur.execute(
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
            cur.execute(
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
                    cur.execute(
                        """
                        UPDATE facts SET object_id = %s, qdrant_synced = false
                        WHERE object_id = %s AND rel_type = ANY(%s)
                          AND superseded_at IS NULL AND subject_id <> %s
                        """,
                        (survivor, loser, _rels, survivor),
                    )
                    repointed += cur.rowcount
                    cur.execute(
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

    Per-rel_type aggregation (Frequency Analysis, DEV/SELF-GROWTH-ENGINE.md): the live
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
                "   ORDER BY oe.occurrence_count DESC, oe.last_seen_at DESC"
                "   LIMIT 1"
                " ) rep ON true"
                " ORDER BY agg.total_occ DESC"
            )
            candidates = cur.fetchall()
    except Exception as e:
        log.error(f"re_embedder.ontology_eval_fetch_failed: {e}")
        return stats

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
        candidate_rel, subj_type, obj_type, snippet, occ, subj_id, obj = row
        try:
            decision = None
            reason = ""
            best_fit = None
            best_score = 0.0

            # ── Decision 1: Pattern frequency (per-rel_type aggregate) ──
            # occ is SUM(occurrence_count) over all undecided sibling rows of this
            # rel_type — the aggregate frequency, not a single sample triple's count.
            if occ >= 3:
                decision = "approved"
                reason = f"aggregate_occurrence={occ} >= 3"

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
                    llm_metadata = _query_llm_for_rel_type_metadata(
                        candidate_rel, subj_type, obj_type, snippet, qwen_api_url
                    )

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
                                _tax_cur.execute(
                                    """
                                    UPDATE entity_taxonomies
                                    SET rel_types_defining_group = array_append(rel_types_defining_group, %s)
                                    WHERE taxonomy_name = %s
                                      AND NOT (rel_types_defining_group @> ARRAY[%s]::TEXT[])
                                    """,
                                    (candidate_rel, category, candidate_rel),
                                )
                                if _tax_cur.rowcount > 0:
                                    log.info("re_embedder.taxonomy_rel_type_appended",
                                             rel_type=candidate_rel, taxonomy=category)
                                else:
                                    # Exact match found no row — try ILIKE fallback
                                    _tax_cur.execute(
                                        """
                                        UPDATE entity_taxonomies
                                        SET rel_types_defining_group = array_append(rel_types_defining_group, %s)
                                        WHERE taxonomy_name ILIKE %s
                                          AND NOT (rel_types_defining_group @> ARRAY[%s]::TEXT[])
                                        """,
                                        (candidate_rel, category, candidate_rel),
                                    )
                                    if _tax_cur.rowcount > 0:
                                        log.info("re_embedder.taxonomy_rel_type_appended_ilike",
                                                 rel_type=candidate_rel, taxonomy_pattern=category)
                                    else:
                                        log.debug("re_embedder.taxonomy_no_match_for_category",
                                                  rel_type=candidate_rel, category=category)
                        except Exception as _tax_err:
                            log.warning("re_embedder.taxonomy_append_failed",
                                        rel_type=candidate_rel, error=str(_tax_err)[:100])
                    elif is_hierarchy:
                        log.debug("re_embedder.taxonomy_append_skipped_hierarchy",
                                  rel_type=candidate_rel)

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
            cur.execute(
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
            cur.execute(
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
LINGUISTIC_CUE_GROWTH_THRESHOLD = 3


def grow_linguistic_cue_candidates(db_conn, schema_name: str = None) -> dict:
    """Grow CARVED cue classes (social_role / problem_noun) PER-TENANT from observed, freq-gated
    candidates.

    THE CARVE-OUT (lean-seed): social_role and problem_noun are DOMAIN-FLAVORED classes that are no
    longer seeded — they are GROWN from the OBSERVED construction. The ingest/harvest seam records a
    candidate into ``ontology_evaluations`` (extraction_method='linguistic_cue_candidate',
    candidate_object_type=<category>, sample_object=<cue lemma>) and bumps occurrence_count on each
    re-sighting (ON CONFLICT). This sweep reads candidates that crossed the freq gate (≥3) and writes
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
                        _r.execute(f"SET search_path TO {schema_name}")
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
        return False, f"invalid_regex {str(_re_err)[:60]}"
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
                "SELECT entity_id, alias, preference_source FROM entity_aliases "
                "WHERE is_preferred = true "
                "AND preference_source IN ('inferred', 'provisioned', 'merge', 'unspecified')"
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
        try:
            with db_conn.cursor() as _uc:
                _uc.execute("SELECT current_schema()")
                _sch = (_uc.fetchone() or [None])[0] or ""
            if _sch.startswith("faultline_"):
                _tenant_user_id = _sch[len("faultline_"):].replace("_", "-")
        except Exception:
            _tenant_user_id = None

        # ────────────────────────────────────────────────────────────────
        # Step 2: Process each conflict
        # ────────────────────────────────────────────────────────────────
        for conflict_id, entity_id_1, entity_name_1, entity_id_2, entity_name_2, disputed_name in conflicts:
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
                payload = build_llm_payload(
                    messages=messages,
                    model=LLMModels.get("NAME_CONFLICT"),
                    user_id="re_embedder",
                    temperature=0.2,
                    max_tokens=10
                )

                # Use global HTTP client with timeout
                try:
                    response = _http_client_sync.post(
                        llm_url,
                        json=payload,
                        headers=get_llm_headers(),
                        timeout=10.0
                    )
                    response.raise_for_status()
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
                    result = response.json()
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
                "embedding_cache.model_version_changed",
                old=stored_version,
                new=embedding_model_version,
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
                    if not cur.fetchone():
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
            cur.execute(
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
                cur.execute(
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
                cur.execute(
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
            cur.execute(f"SET search_path TO {_fb_schema}")
            cur.execute(
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
                    cur.execute(f"SET search_path TO {schema_name}")
                db.commit()

                with db.cursor() as cur:
                    cur.execute(
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
                        (_SYSTEM_ALIASES,),
                    )
                    deleted_ids = [r[0] for r in cur.fetchall()]
                db.commit()

                if deleted_ids:
                    log.info(
                        f"re_embedder.inverted_sweep.deleted "
                        f"user_id={user_id[:8]} schema={schema_name} count={len(deleted_ids)}"
                    )
                    total_deleted += len(deleted_ids)

                    # Best-effort Qdrant cleanup for deleted rows
                    try:
                        collection = derive_collection(user_id)
                        for _did in deleted_ids:
                            _http_client.post(
                                f"{qdrant_url}/collections/{collection}/points/delete",
                                json={
                                    "filter": {
                                        "must": [
                                            {"key": "source_table", "match": {"value": "staged_facts"}},
                                            {"key": "fact_id",     "match": {"value": _did}},
                                        ]
                                    }
                                },
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
                            json={"points": [derive_qdrant_point_id("staged_facts", _did) for _did in deleted_ids]},
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


def main():
    """Main poll loop."""
    global _http_client_sync

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
    from src.api.llm_client import get_backend_endpoint, get_endpoint_list as _get_llm_endpoint_list
    _typed_endpoint = get_backend_endpoint()
    if _typed_endpoint:
        qwen_api_url = _typed_endpoint
    else:
        _endpoints = _get_llm_endpoint_list()
        qwen_api_url = _endpoints[0] if _endpoints else "http://localhost:11434/v1/chat/completions"
    interval = int(os.getenv("REEMBED_INTERVAL", "60"))  # dprompt-121: Changed from 10 to 60
    confidence_threshold = float(os.getenv("QDRANT_SYNC_CONFIDENCE_THRESHOLD", "0.0"))

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

    log.info(f"re_embedder.start interval={interval}s qdrant_url={qdrant_url} confidence_threshold={confidence_threshold} loglevel=INFO")
    log.info("re_embedder.entering_main_loop")

    # One-time startup sweep: remove pre-existing inverted staged_facts hierarchy rows
    # (e.g. "person instance_of alexander") that were created before the ingest-path
    # cleanup was in place.  Gated here so it runs once per process start, not every
    # 60-second poll cycle.
    try:
        _sweep_inverted_staged_hierarchy_rows(postgres_dsn, qdrant_url)
    except Exception as _sweep_err:
        # Never block startup — sweep failure is non-fatal.
        log.warning(f"re_embedder.inverted_sweep.startup_error error={_sweep_err}")

    # Initialize Redis client for event queue
    redis_url = os.getenv("REDIS_URL")
    redis_client = _get_redis_client(redis_url)
    if redis_client:
        log.info("re_embedder.redis_client_initialized")
    else:
        log.warning("re_embedder.redis_client_unavailable queue_events_disabled")

    while True:
        try:
            # Phase 3c: Check circuit breaker health for awareness
            breaker_status = _get_circuit_breaker_status()
            if breaker_status["is_open"]:
                log.warning("re_embedder.circuit_breaker_open skipping_llm_work_this_cycle")

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

            # PHASE 2b: Process each user schema independently
            for user_id, schema_name in ready_schemas:
                try:
                    with psycopg2.connect(postgres_dsn) as db_per_user:
                        with db_per_user.cursor() as cur:
                            cur.execute(f"SET search_path TO {schema_name}")
                        db_per_user.commit()

                        # Promote staged facts for this user
                        n_promoted = promote_staged_facts(db_per_user, qdrant_url, user_id=user_id, schema_name=schema_name)
                        if n_promoted:
                            log.info(f"re_embedder.promotion_complete user_id={user_id[:8]} schema={schema_name} promoted={n_promoted}")

                        # Expire stale Class C facts for this user
                        n_expired = expire_staged_facts(db_per_user, qdrant_url, user_id=user_id)
                        if n_expired:
                            log.info(f"re_embedder.expiry_complete user_id={user_id[:8]} expired={n_expired}")

                        # JOB 2 — Promote Class C rows that earned hit_count >= 3 (query-scoped
                        # hits) to Class B. Classify-if-needed (default B-2). Run BEFORE the
                        # hit-decay sweep so a row that just reached threshold promotes instead
                        # of being decremented in the same cycle.
                        n_c_promoted = promote_class_c_hits(
                            db_per_user, qdrant_url, qwen_api_url,
                            user_id=user_id, schema_name=schema_name
                        )
                        if n_c_promoted:
                            log.info(f"re_embedder.class_c_promotion_complete user_id={user_id[:8]} promoted={n_c_promoted}")

                        # JOB 1 — Decay Class C query-hit counter for idle rows (30d window
                        # elapsed with no hit): hit_count -= 1, reset window, DROP at <= 0.
                        c_decay = decay_class_c_hits(db_per_user, qdrant_url, user_id=user_id)
                        if c_decay["decremented"] or c_decay["dropped"]:
                            log.info(
                                f"re_embedder.class_c_decay user_id={user_id[:8]} "
                                f"decremented={c_decay['decremented']} dropped={c_decay['dropped']}"
                            )

                        # Fetch and embed unsynced facts for this user.
                        # TIER REALIGNMENT: the `facts` table is the HARD A/B tier, served by
                        # the deterministic postgres walk — it need NOT be in the vector. When
                        # VECTOR_CLASS_C_ONLY is on (default) we SKIP the A/B (facts-table) sync
                        # entirely; only staged_facts (Class B/C) are embedded below. The query
                        # already drops any A/B Qdrant result (VECTOR_CLASS_C_ONLY in main.py),
                        # so leaving A/B out of the vector is safe and shrinks the C catch-all
                        # toward zero as more grounds into A/B. Flag off → legacy: sync both.
                        if _VECTOR_CLASS_C_ONLY:
                            log.debug(f"re_embedder.facts_sync_skipped_class_c_only user_id={user_id[:8]}")
                        else:
                            rows = fetch_unsynced(db_per_user, user_id, confidence_threshold)
                            if rows:
                                log.info(f"re_embedder.batch_start user_id={user_id[:8]} count={len(rows)}")
                                collection = derive_collection(user_id)
                                ensure_collection(collection, qdrant_url)

                                # Resolve display names for batch
                                rows = resolve_display_names_for_facts(db_per_user, rows)

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
                        if staged_rows:
                            log.info(f"re_embedder.staged_batch user_id={user_id[:8]} count={len(staged_rows)}")
                            collection = derive_collection(user_id)
                            ensure_collection(collection, qdrant_url)

                            staged_rows = resolve_display_names_for_facts(db_per_user, staged_rows)
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
                                cur.execute(f"SET search_path TO {_gate_schema}")
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
                _sweep_updated_total = 0
                # Per-tenant overlay coordination: collect the schemas whose rel_types
                # actually changed so the refresh endpoint invalidates ONLY those
                # tenants' overlays (isolation + minimal rebuild cost). Empty set →
                # endpoint falls back to a full overlay reset (backward-compatible).
                _changed_schemas: set = set()
                for _user_id, _schema in ready_schemas:
                    try:
                        with psycopg2.connect(postgres_dsn) as _ont_db:
                            with _ont_db.cursor() as _spc:
                                _spc.execute(f"SET search_path TO {_schema}")
                            _ont_db.commit()

                            # ── Ontology candidate evaluation (per-user schema) ──
                            try:
                                if has_pending_ontology_work(_ont_db):
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
                            except Exception as e:
                                _rollback_and_reapply_search_path(_ont_db, _schema)
                                log.error(f"re_embedder.ontology_eval_subsystem_error schema={_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                            # ── PENDING-PLACEMENT morphology DRAIN (deterministic, per-user schema) ──
                            # Reconcile EXISTING `pending_placement` rels onto their SEEDED canonical
                            # IN PLACE (alias + adopt category + join the seed's taxonomy) so their
                            # already-stored facts become walkable WITHOUT re-ingest. Companion to the
                            # in-flow morphology fold (main.py 48c3200 seam) which only catches FRESH
                            # ingests. SEEDED morphology match ONLY (no cosine); a non-matching pending
                            # rel is LEFT for the freq>=3/LLM path. Self-isolating (failure never
                            # crashes the tenant sweep). Touches rel_types/entity_taxonomies → mark
                            # the schema changed so its overlay refreshes.
                            try:
                                _drain = drain_pending_placement_by_morphology(
                                    _ont_db, postgres_dsn, _schema)
                                if _drain.get("reconciled", 0) > 0:
                                    log.info(
                                        f"re_embedder.pending_placement_drain schema={_schema} "
                                        f"reconciled={_drain['reconciled']} "
                                        f"scanned={_drain['scanned']} errors={_drain['errors']}"
                                    )
                                    _changed_schemas.add(_schema)
                            except Exception as e:
                                _rollback_and_reapply_search_path(_ont_db, _schema)
                                log.error(f"re_embedder.pending_placement_drain_subsystem_error schema={_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                            # ── MISS-PUSHBACK "what is X?" concept classify (per-user schema) ──
                            # SECONDARY strengthen for the ingest miss-pushback path: type+ground
                            # the unknown user-derived concepts that landed C-raw at first-fire so
                            # the structure can resolve on a later cycle. Background + preemptible
                            # (runs in the engine, never on the ingest hot path), bounded, and
                            # self-isolating (failure never crashes the tenant sweep). Touching the
                            # schema's rel_types/entities/staged_facts → mark it changed so its
                            # overlay refreshes.
                            try:
                                if _ENGINE_WHATIS_CLASSIFY:
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
                            try:
                                if _ENGINE_CLASSIFY_CLIMB:
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
                            except Exception as e:
                                _rollback_and_reapply_search_path(_ont_db, _schema)
                                log.error(f"re_embedder.classify_climb_subsystem_error schema={_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                            # ── RUNG-6 convergence-by-identity (deterministic, per-user schema) ──
                            # Fuse separately-grown hierarchy branches that reached a same-canonical-
                            # name node by IDENTITY (no cosine). Runs every cycle; cheap (one node
                            # scan + targeted edge repoints). Self-isolating: failure never crashes
                            # the tenant sweep. This is the primary collapse that retires cosine-map.
                            try:
                                if _RUNG6_CONVERGENCE:
                                    _conv = converge_hierarchy_by_identity(_ont_db, schema_name=_schema)
                                    if _conv.get("edges_repointed", 0) > 0:
                                        log.info(
                                            f"re_embedder.rung6_convergence schema={_schema} "
                                            f"merged_nodes={_conv['merged_nodes']} "
                                            f"edges_repointed={_conv['edges_repointed']}"
                                        )
                                        _changed_schemas.add(_schema)
                            except Exception as e:
                                _rollback_and_reapply_search_path(_ont_db, _schema)
                                log.error(f"re_embedder.rung6_convergence_subsystem_error schema={_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                            # ── Reinforce-or-decay sweep for novel rel_type candidates ──
                            # Mirrors expire_staged_facts (Class C score-decay), keyed on
                            # ontology_evaluations.occurrence_count + last_seen_at. Runs every
                            # cycle (NOT gated by has_pending_ontology_work) so un-reinforced
                            # one-off candidates age out even when nothing is at threshold.
                            # Per-user schema context: search_path already set above.
                            try:
                                _ont_decay = decay_ontology_candidates(_ont_db, user_id=_user_id)
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
                                _cue_grow = grow_linguistic_cue_candidates(_ont_db, schema_name=_schema)
                                if _cue_grow.get("grown", 0) > 0:
                                    log.info(
                                        f"re_embedder.cue_class_growth schema={_schema} "
                                        f"grown={_cue_grow['grown']} errors={_cue_grow['errors']}"
                                    )
                                    _changed_schemas.add(_schema)
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
                                _corr_stats = evaluate_correction_signal_candidates(_ont_db, qwen_api_url)
                                if any(v > 0 for v in _corr_stats.values()):
                                    log.info(
                                        f"re_embedder.correction_eval schema={_schema} "
                                        f"approved={_corr_stats['approved']} "
                                        f"promoted={_corr_stats['promoted']} "
                                        f"rejected={_corr_stats['rejected']} "
                                        f"errors={_corr_stats['errors']}"
                                    )
                            except Exception as e:
                                _rollback_and_reapply_search_path(_ont_db, _schema)
                                log.error(f"re_embedder.correction_signal_subsystem_error schema={_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                            # ── Retroactive head_types/tail_types sweep (per-user schema) ──
                            # Severance #2, Phase 2: self-heal rel_types rows with NULL/empty
                            # head_types/tail_types. Bounded batch per cycle (LIMIT 10). Uses the
                            # SAME LLM metadata call so type constraints come from inference.
                            try:
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
                            except Exception as e:
                                _rollback_and_reapply_search_path(_ont_db, _schema)
                                log.warning(f"re_embedder.head_tail_sweep_error schema={_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")
                    except Exception as e:
                        log.error(f"re_embedder.ontology_per_user_error schema={_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                # Phase 3 (Severance #3): newly-approved/mapped rel_types or filled type
                # constraints live in the DB but are invisible to the backend uvicorn process
                # (separate OS process) until it reloads _REL_TYPE_META. Trigger the cross-process
                # refresh endpoint ONCE per cycle if anything changed across any user schema.
                if _approved_any or _sweep_updated_total > 0:
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

                    for _tx_user_id, _tx_schema in ready_schemas:
                        try:
                            with psycopg2.connect(postgres_dsn) as _tx_db:
                                with _tx_db.cursor() as _spc:
                                    _spc.execute(f"SET search_path TO {_tx_schema}")
                                _tx_db.commit()

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
                                                log.info("re_embedder.taxonomy_discovered_async",
                                                        schema=_tx_schema,
                                                        rel_type=rel_type,
                                                        taxonomy=discovered.get("taxonomy_name"))
                                        except Exception as e:
                                            log.warning("re_embedder.taxonomy_discovery_failed",
                                                       schema=_tx_schema, rel_type=rel_type, error=str(e))
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
                    try:
                        with psycopg2.connect(postgres_dsn) as _gw_db:
                            with _gw_db.cursor() as _spc:
                                _spc.execute(f"SET search_path TO {_gw_schema}")
                            _gw_db.commit()

                            # dprompt-137: Evaluate retraction outcomes for continuous learning
                            # Auto-register high-frequency patterns, update metrics for existing patterns
                            try:
                                if has_pending_retraction_outcomes(_gw_db):
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
                            except Exception as e:
                                _rollback_and_reapply_search_path(_gw_db, _gw_schema)
                                log.error(f"re_embedder.retraction_outcomes_subsystem_error schema={_gw_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                            # dprompt-121: Resolve name conflicts via LLM context evaluation
                            # Event-driven: only run if there are pending conflicts
                            try:
                                if has_pending_name_conflicts(_gw_db):
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
                            except Exception as e:
                                _rollback_and_reapply_search_path(_gw_db, _gw_schema)
                                log.error(f"re_embedder.name_conflict_subsystem_error schema={_gw_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                            # ALIAS-PROVENANCE-DESIGN §3: Flag suspect preferred names (preferred
                            # aliases nobody ever chose). Flag-only — never auto-mutates names.
                            try:
                                suspect_stats = flag_suspect_preferred_names(_gw_db)
                                if suspect_stats["flagged"] > 0:
                                    log.info(
                                        f"re_embedder.suspect_preferred_names_flagged "
                                        f"schema={_gw_schema} "
                                        f"count={suspect_stats['flagged']}"
                                    )
                            except Exception as e:
                                _rollback_and_reapply_search_path(_gw_db, _gw_schema)
                                log.error(f"re_embedder.suspect_preferred_names_subsystem_error schema={_gw_schema} (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                            # Job 6: Evaluate extraction patterns for accuracy and bootstrap confidence
                            # Scoring phase: analyze user feedback on extraction patterns, update confidence scores
                            try:
                                pattern_stats = evaluate_extraction_patterns(_gw_db)
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
                                cur.execute(f"SET search_path TO {_ev_schema}")
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
                        try:
                            with psycopg2.connect(postgres_dsn) as _stub_db:
                                with _stub_db.cursor() as _spc:
                                    _spc.execute(f"SET search_path TO {_us_schema}")
                                with _stub_db.cursor() as _scur:
                                    _scur.execute(
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
                                with _stub_db.cursor() as cur:
                                    cur.execute(
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
                                        _phrasing = generate_rel_type_phrasing(rt, user_id="re_embedder")
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
                        except Exception as _stub_err:
                            log.warning(f"re_embedder.orphan_rel_stub_job_failed (non-fatal) schema={_us_schema}: {_stub_err}")
                except Exception as e:
                    log.warning(f"re_embedder.natural_language_job_error (non-fatal): {e}")

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

                # Reconciliation pass — sync stale payloads and orphaned points
                stats = reconcile_qdrant(db, qdrant_url, qwen_api_url)
                if any(v > 0 for v in [stats["deleted"], stats["reupserted"], stats["errors"]]):
                    log.info(f"re_embedder.reconcile deleted={stats['deleted']} reupserted={stats['reupserted']} ok={stats['ok']} errors={stats['errors']}")

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
            cur.execute(
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
    main()