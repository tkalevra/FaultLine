import asyncio
import hashlib
import json
import os
import re
import traceback
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from functools import wraps
from time import time as time_now
from typing import Optional, Union
import httpx
import psycopg2
import redis
import structlog
from fastapi import Depends, FastAPI, HTTPException
from src.api.logging_config import set_log_level, get_log_level, LogLevel
from src.entity_registry.registry import EntityRegistry
from src.fact_store.store import FactStoreManager
from src.re_embedder.embedder import derive_collection, embed_text, ensure_collection, mark_synced, upsert_to_qdrant
from src.schema_oracle import resolve_entities
from src.wgm.gate import WGMValidationGate, RelTypeRegistry
from .models import EdgeInput, EntityResult, FactResult, FactCorrectionRequest, FactCorrectionResponse, IngestRequest, IngestResponse, QueryRequest, RelTypeRequest, RetractRequest, RetractResponse, RewriteRequest, RewriteResponse, StoreContextRequest, StoreContextResponse
from .llm_client import get_llm_headers, build_llm_payload
from .llm_calls import call_llm_with_retry_sync, LLMTimeouts
from .idempotency import IdempotencyManager

log = structlog.get_logger()

_gliner2_model = None
_rel_type_registry: RelTypeRegistry = None
_rel_type_constraint: str = ""
_REL_TYPE_META: dict = {}
_http_client: httpx.AsyncClient = None
_http_client_sync: httpx.Client = None
_idempotency_mgr: Optional[IdempotencyManager] = None
_EMBEDDING_API_URL: str = None

# Call counters for debugging extraction performance (dprompt-120 debug)
_EXTRACT_REWRITE_CALL_COUNT = 0
_INGEST_EXTRACTION_CALL_COUNT = 0

# ──────────────────────────────────────────────────────────────────────────────
# dprompt-144: Intent Classification Rate Limiting + Redis Queue
# ──────────────────────────────────────────────────────────────────────────────

# Rate limiting buckets: {user_id: [timestamps]}
_RATE_LIMIT_BUCKETS: dict = defaultdict(list)
_RATE_LIMIT_PER_MINUTE = 60  # 60 calls per user per minute

# Redis client for event queue (initialized in lifespan)
_redis_client: Optional[redis.Redis] = None

_PREFERENCE_SIGNALS = {
    "goes by", "go by",
    "prefers to be called", "prefer to be called",
    "preferred name", "my preferred name",
    "please call me", "call me",
    "known as", "also known as",
    "my name is", "i prefer", "i go by",
}

_IDENTITY_PATTERNS = [
    re.compile(r"\bmy name is ([a-z]+)", re.IGNORECASE),
    re.compile(r"\bi am ([a-z]+)", re.IGNORECASE),
    re.compile(r"\bi'm ([a-z]+)", re.IGNORECASE),
    re.compile(r"\bcall me ([a-z]+)", re.IGNORECASE),
    re.compile(r"\bpeople call me ([a-z]+)", re.IGNORECASE),
]

_IDENTITY_STOPWORDS = {
    "a", "an", "the", "not", "just", "also", "here", "happy", "glad", "sorry",
    "married", "single", "divorced", "engaged", "here", "ready", "trying",
    "going", "looking", "back", "home", "out", "in", "on", "at", "to",
    "very", "really", "so", "too", "quite", "sure", "afraid", "aware",
    "excited", "sorry", "glad", "grateful", "proud", "tired", "done",
    # Words commonly falsely captured by preference/identity patterns
    "prefer", "prefers", "preferred", "called", "name", "named",
    "family", "children", "kids", "wife", "husband", "spouse",
    "she", "he", "they", "them", "her", "him", "his",
    "goes", "known", "likes", "like", "want", "wants",
}

# _SCALAR_REL_TYPES removed — replaced by classify_fact_type() which uses
# value-driven heuristics + DB-driven ontology hints (rel_types.tail_types).



_UUID_PATTERN = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE
)

# Fact class taxonomy replaced by metadata-driven queries (dprompt-73b).
# rel_types.fact_class column is authoritative — see _get_rel_type_metadata().
# ── dprompt-27: Graph + Hierarchy traversal systems (dprompt-122: DB-driven) ─────────────
# Two orthogonal traversal systems (dprompt-26 architecture):
#   GRAPH:     connectivity — who am I connected to? (is_hierarchy_rel=false)
#   HIERARCHY: composition + classification — what are they, what do they belong to? (is_hierarchy_rel=true)
# Both derived from _REL_TYPE_META at runtime — NO HARDCODED LISTS.

def _get_graph_rels() -> frozenset:
    """
    Return all graph relationship types (is_hierarchy_rel=false).
    Queries DB directly to include novel rel_types approved by re_embedder.
    Falls back to cache if DB unreachable.
    """
    dsn = os.environ.get("POSTGRES_DSN")
    if dsn:
        try:
            with psycopg2.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT rel_type FROM rel_types WHERE is_hierarchy_rel = false OR is_hierarchy_rel IS NULL"
                    )
                    return frozenset(row[0] for row in cur.fetchall())
        except Exception as e:
            log.warning("graph_rels.db_query_failed", error=str(e), using_fallback=True)
    # Fallback to startup cache if DB unavailable
    if _REL_TYPE_META:
        return frozenset(
            rt for rt, meta in _REL_TYPE_META.items()
            if not meta.get("is_hierarchy_rel", False)
        )
    return frozenset()

def _get_hierarchy_rels() -> frozenset:
    """
    Return all hierarchy relationship types (is_hierarchy_rel=true).
    Queries DB directly to include novel rel_types approved by re_embedder.
    Falls back to cache if DB unreachable.
    """
    dsn = os.environ.get("POSTGRES_DSN")
    if dsn:
        try:
            with psycopg2.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT rel_type FROM rel_types WHERE is_hierarchy_rel = true"
                    )
                    return frozenset(row[0] for row in cur.fetchall())
        except Exception as e:
            log.warning("hierarchy_rels.db_query_failed", error=str(e), using_fallback=True)
    # Fallback to startup cache if DB unavailable
    if _REL_TYPE_META:
        return frozenset(
            rt for rt, meta in _REL_TYPE_META.items()
            if meta.get("is_hierarchy_rel", False)
        )
    return frozenset()

# dprompt-125: Rel-type alias cache (alias → canonical)
# Loaded at startup, queries DB at runtime for fresh aliases from re_embedder
_REL_TYPE_ALIASES: dict = {}

def _load_rel_type_aliases() -> dict:
    """
    Load rel_type_aliases from database (alias → canonical mapping).
    Called at startup and refreshed periodically by re_embedder.
    """
    dsn = os.environ.get("POSTGRES_DSN")
    if dsn:
        try:
            with psycopg2.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT alias, canonical_rel_type FROM rel_type_aliases WHERE canonical_rel_type IN (SELECT rel_type FROM rel_types)"
                    )
                    return {row[0].lower(): row[1].lower() for row in cur.fetchall()}
        except Exception as e:
            log.warning("rel_type_aliases.load_failed", error=str(e))
    return {}

def _get_canonical_rel_type(rel_type_alias: str) -> str:
    """
    Normalize LLM rel_type variations to canonical form via database aliases.
    Queries DB first (fresh), falls back to startup cache.
    Returns canonical rel_type or original if no alias found.
    dprompt-125: DB-driven, no hardcoded mappings.
    """
    if not rel_type_alias:
        return rel_type_alias

    alias_lower = rel_type_alias.lower().strip()

    # Try DB query first (inclualice newly-learned aliases from re_embedder)
    dsn = os.environ.get("POSTGRES_DSN")
    if dsn:
        try:
            with psycopg2.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT canonical_rel_type FROM rel_type_aliases WHERE alias = %s",
                        (alias_lower,)
                    )
                    row = cur.fetchone()
                    if row:
                        return row[0].lower()
        except Exception:
            pass  # Fall through to cache

    # Fall back to startup cache
    return _REL_TYPE_ALIASES.get(alias_lower, rel_type_alias)


def _get_canonical_rel_type_with_directionality(rel_type_alias: str) -> tuple[str, bool]:
    """
    Normalize LLM rel_type variations to canonical form and preserve directionality.

    dprompt-126: Phase 1 — Alias directionality preservation

    Returns:
        (canonical_rel_type, requires_inversion)
        - canonical_rel_type: canonical form from rel_types table
        - requires_inversion: True if subject/object need to be swapped to match canonical direction

    Examples:
        "son_of" → ("parent_of", True) — swap child/parent to parent/child
        "has_child" → ("parent_of", False) — already same direction
        "spouse_of" → ("spouse", False) — symmetric, direction doesn't matter
    """
    if not rel_type_alias:
        return rel_type_alias, False

    alias_lower = rel_type_alias.lower().strip()

    # Try DB query first (fresh data from re_embedder)
    dsn = os.environ.get("POSTGRES_DSN")
    if dsn:
        try:
            with psycopg2.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT canonical_rel_type, requires_inversion
                           FROM rel_type_aliases
                           WHERE alias = %s""",
                        (alias_lower,)
                    )
                    row = cur.fetchone()
                    if row:
                        return row[0].lower(), row[1] or False
        except Exception as e:
            log.warning("directionality.db_query_failed", alias=alias_lower, error=str(e))

    # Fall back: canonical from cache, no inversion (safe default)
    canonical = _REL_TYPE_ALIASES.get(alias_lower, rel_type_alias)
    return canonical, False

# REMOVED: Hardcoded categories. Query database for valid categories at runtime.
# See _get_valid_categories() for DB-driven implementation.
# Allows engine to learn and create new categories without code changes.

def _get_valid_categories() -> set[str]:
    """
    Query all DISTINCT categories from rel_types table.
    FAIL HARD if database unavailable or no categories found — engine must have learned them.
    Returns set of category strings.
    """
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        msg = "CRITICAL: POSTGRES_DSN not set — cannot query categories from database"
        log.critical(msg, log_level="CRIT", component="database")
        raise RuntimeError(msg)

    try:
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT(category) FROM rel_types WHERE category IS NOT NULL")
                rows = cur.fetchall()
                if not rows:
                    msg = (
                        "CRITICAL: No categories found in rel_types table. "
                        "Engine has not learned any relationship types yet. "
                        "Expected: family, location, work, identity, household, pets, temporal, physical, etc. "
                        "ACTION: Check if re_embedder is running and ontology_evaluations are being processed."
                    )
                    log.critical(msg, log_level="CRIT", component="ontology")
                    raise RuntimeError(msg)
                return set(cat[0] for cat in rows if cat[0])
    except RuntimeError:
        raise  # Re-raise intentional errors
    except Exception as e:
        msg = f"CRITICAL: Failed to query categories from database: {e}"
        log.critical(msg, log_level="CRIT", component="database", error=str(e))
        raise RuntimeError(msg)

# Temporal event routing replaced by metadata-driven queries (dprompt-73b).
# rel_types.storage_target column is authoritative — see _get_rel_type_metadata().
# _EVENT_RECURRENCE_DEFAULTS kept for calendar semantics of events table.

_EVENT_RECURRENCE_DEFAULTS = {
    "born_on": "yearly",
    "born_in": "once",
    "anniversary_on": "yearly",
    "met_on": "once",
    "married_on": "once",
    "appointment_on": "once",
}

# Fact classification replaced by metadata-driven queries (dprompt-73b).
# rel_types.fact_class column is authoritative — queried at ingest time.
# Correction/engine_generated/confidence < 0.6 logic preserved inline in ingest loop.

def _infer_category(rel_type: str) -> str | None:
    """
    Keyword-based category inference — offline fallback only.
    Used when LLM is unavailable or returns an invalid category.
    """
    rt = rel_type.lower()
    if any(k in rt for k in ("height","weight","gender","age","physical","body")):
        return "physical"
    if any(k in rt for k in ("born","birth","anniversary","met_on","married_on")):
        return "temporal"
    if any(k in rt for k in ("live","address","location","city","home","reside")):
        return "location"
    if any(k in rt for k in ("work","job","employ","occupation","career")):
        return "work"
    if any(k in rt for k in ("parent","child","spouse","sibling","family")):
        return "family"
    if any(k in rt for k in ("pet","animal","dog","cat","fish","bird")):
        return "pets"
    if any(k in rt for k in ("name","alias","known","called","pref")):
        return "identity"
    return None

def _get_rel_type_category(rel_type: str) -> str | None:
    """
    Get category for rel_type: DB → cache → keyword inference.
    Queries DB directly to include novel rel_types approved by re_embedder.
    """
    dsn = os.environ.get("POSTGRES_DSN")
    if dsn:
        try:
            with psycopg2.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT category FROM rel_types WHERE rel_type = %s",
                        (rel_type.lower(),)
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        return row[0]
        except Exception as e:
            log.warning("rel_type_category.db_query_failed", rel_type=rel_type, error=str(e))
    # Fallback to cache
    meta = _REL_TYPE_META.get(rel_type.lower(), {})
    if meta.get("category"):
        return meta["category"]
    # Final fallback: keyword inference
    return _infer_category(rel_type)

def _assign_category_via_llm(rel_type: str) -> Optional[str]:
    """
    Ask LLM to assign a category to a novel rel_type.
    Uses centralized endpoint resolver with fallback chain.
    Validates response against database-learned categories (FAIL HARD if unknown).
    Returns a valid category string or None on failure.
    Does NOT fall back to keyword inference — validation is DB-driven.
    """
    # Get valid categories from database (FAIL HARD if unavailable)
    try:
        valid_categories = _get_valid_categories()
    except RuntimeError as e:
        log.error(f"assign_category_via_llm: cannot fetch categories: {e}")
        raise

    try:
        payload = build_llm_payload(
            messages=[{
                "role": "user",
                "content": (
                    f"What category does the relationship type '{rel_type}' belong to? "
                    f"Choose exactly one from this list: {', '.join(sorted(valid_categories))}. "
                    f"Return only the single category word, nothing else. "
                    f"If the rel_type doesn't fit any category, return 'unknown'."
                )
            }],
            model=os.getenv("CATEGORY_LLM_MODEL", "qwen2.5-coder"),
            temperature=0.0,
            max_tokens=10,
            # NOTE: thinking parameter removed — Qwen doesn't support extended thinking
        )

        # dprompt-142: Use centralized endpoint resolver
        llm_url = _resolve_llm_endpoint(with_fallback=False)

        resp = _http_client_sync.post(
            llm_url,
            json=payload,
            headers=get_llm_headers(),
            timeout=10.0,
        )
        if resp.status_code == 200:
            raw = resp.json()["choices"][0]["message"]["content"].strip().lower()
            if raw in valid_categories:
                return raw
            elif raw == "unknown":
                log.warning(f"assign_category_via_llm: LLM could not categorize '{rel_type}'")
                return None
            else:
                log.warning(
                    f"assign_category_via_llm: LLM returned invalid category '{raw}' "
                    f"(expected one of {valid_categories})"
                )
                return None
    except RuntimeError:
        raise  # Re-raise database errors
    except Exception as e:
        log.error(f"assign_category_via_llm: LLM call failed: {e}")
        return None

def _infer_symmetry_from_rel_type(rel_type: str) -> bool:
    """
    Heuristic: infer is_symmetric from rel_type name patterns.
    Returns True for relations that are naturally symmetric (both directions mean same thing).
    """
    rt = rel_type.lower()
    # Symmetric patterns: spouse, sibling, friend, knows, met, same_as
    symmetric_patterns = ("spouse", "sibling", "friend", "knows", "met", "same_as")
    return any(pattern in rt for pattern in symmetric_patterns)

def _infer_inverse_rel_type(rel_type: str) -> Optional[str]:
    """
    Heuristic: infer inverse_rel_type from rel_type name.
    Returns the canonical inverse relationship, or None if asymmetric/non-bidirectional.
    """
    rt = rel_type.lower()

    # Hard mappings (Wikidata-aligned)
    inverse_map = {
        "parent_of": "child_of",
        "child_of": "parent_of",
        "spouse": "spouse",  # Symmetric, inverse is itself
        "sibling_of": "sibling_of",  # Symmetric
        "knows": "knows",  # Symmetric
        "friend_of": "friend_of",  # Symmetric
        "met": "met",  # Symmetric
        "same_as": "same_as",  # Symmetric
    }

    if rt in inverse_map:
        return inverse_map[rt]

    # Pattern-based inference (for novel rel_types)
    # If rel_type ends with _of, try to reverse it
    if rt.endswith("_of"):
        base = rt[:-3]  # Remove _of
        # Check if base + reverse = known rel_type pair
        if f"has_{base}" in inverse_map or f"{base}_by" in inverse_map:
            return f"has_{base}"
        # Return None for unknowns (re_embedder will learn)
        return None

    # If rel_type starts with has_, try reverse
    if rt.startswith("has_"):
        base = rt[4:]  # Remove has_
        if f"{base}_of" in inverse_map:
            return f"{base}_of"
        return None

    # No inverse pattern detected
    return None

def _infer_hierarchy_from_rel_type(rel_type: str) -> bool:
    """
    Heuristic: infer is_hierarchy_rel from rel_type name patterns.
    Returns True for hierarchy/classification rels (instance_of, subclass_of, part_of, member_of, is_a).
    """
    rt = rel_type.lower()
    hierarchy_patterns = ("instance_of", "subclass_of", "part_of", "member_of", "is_a")
    return any(pattern in rt for pattern in hierarchy_patterns)

def _infer_category_from_rel_type(rel_type: str) -> str:
    """
    Heuristic: infer category from rel_type name patterns.
    Returns a valid category string (falls back to "general" if no pattern matches).
    Categories align with entity_taxonomies: family, work, location, pets, physical, temporal, identity.
    """
    rt = rel_type.lower()

    # Keyword-based category inference
    if any(k in rt for k in ("height", "weight", "gender", "age", "physical", "body")):
        return "physical"
    if any(k in rt for k in ("born", "birth", "anniversary", "met_on", "married_on")):
        return "temporal"
    if any(k in rt for k in ("live", "address", "location", "city", "home", "reside")):
        return "location"
    if any(k in rt for k in ("work", "job", "employ", "occupation", "career")):
        return "work"
    if any(k in rt for k in ("parent", "child", "spouse", "sibling", "family")):
        return "family"
    if any(k in rt for k in ("pet", "animal", "dog", "cat", "fish", "bird")):
        return "pets"
    if any(k in rt for k in ("name", "alias", "known", "called", "pref")):
        return "identity"

    # Default category
    return "general"

def _insert_novel_rel_type(db, rel_type: str, confidence: float,
                            source: str = "gliner2_discovery",
                            text_snippet: str = "") -> bool:
    """
    Insert a novel rel_type discovered by GLiNER2 or LLM.
    Metadata populated from heuristics; re_embedder refines over time.

    Returns True if inserted/updated, False on failure (logs error).
    """
    try:
        rt_lower = rel_type.lower().strip()
        if not rt_lower:
            log.warning("insert_novel_rel_type: empty rel_type provided")
            return False

        # Infer metadata using heuristics (deterministic, non-brittle)
        category = _infer_category_from_rel_type(rt_lower)
        is_symmetric = _infer_symmetry_from_rel_type(rt_lower)
        inverse = _infer_inverse_rel_type(rt_lower)
        is_hierarchy = _infer_hierarchy_from_rel_type(rt_lower)

        # Humanize label from rel_type name
        label = rt_lower.replace("_", " ").title()

        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO rel_types (
                    rel_type, label, confidence, source, category,
                    is_symmetric, inverse_rel_type, is_hierarchy_rel,
                    tail_types, head_types, fact_class,
                    correction_behavior, engine_generated, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (rel_type) DO UPDATE SET
                    confidence = GREATEST(rel_types.confidence, %s)
            """, (
                rt_lower,                  # rel_type
                label,                     # label
                confidence,                # confidence
                source,                    # source (gliner2_discovery|llm_inference)
                category,                  # category
                is_symmetric,              # is_symmetric
                inverse,                   # inverse_rel_type
                is_hierarchy,              # is_hierarchy_rel
                ["ANY"],                   # tail_types (default)
                ["ANY"],                   # head_types (default)
                "B",                       # fact_class (LLM-inferred following ontology)
                "supersede",               # correction_behavior
                False,                     # engine_generated
                confidence                 # confidence (for ON CONFLICT)
            ))
        db.commit()

        log.info("ingest.rel_type_auto_created",
                 rel_type=rt_lower, source=source, confidence=confidence,
                 category=category, is_symmetric=is_symmetric,
                 is_hierarchy=is_hierarchy)

        # Invalidate cache so next query uses updated metadata
        global _REL_TYPE_META
        _REL_TYPE_META = _build_rel_type_meta(os.environ.get("POSTGRES_DSN", ""))

        return True

    except Exception as e:
        log.warning("ingest.rel_type_creation_failed",
                    rel_type=rel_type, source=source, error=str(e))
        db.rollback()
        return False

def _coerce_scalar(value: str) -> tuple:
    """
    Coerce a scalar value string to (value_text, value_int, value_float, value_date).
    Returns appropriate typed value AND value_text copy for all paths (dprompt-132).
    """
    # Try integer - populate both value_text and value_int
    try:
        int_val = int(value)
        return (str(int_val), int_val, None, None)
    except ValueError:
        pass
    # Try float - populate both value_text and value_float
    try:
        float_val = float(value)
        return (str(float_val), None, float_val, None)
    except ValueError:
        pass
    # Try date (basic YYYY-MM-DD) - populate value_date and value_text
    if re.match(r'^\d{4}-\d{2}-\d{2}$', value):
        return (value, None, None, value)
    # Fall back to text
    return (value, None, None, None)

def _detect_preference_signal(text: str) -> bool:
    text_lower = text.lower()
    return any(signal in text_lower for signal in _PREFERENCE_SIGNALS)

def _extract_identity(text: str) -> str | None:
    """Return the user's stated name if a self-identification pattern is found."""
    for pattern in _IDENTITY_PATTERNS:
        m = pattern.search(text)
        if m:
            name = m.group(1).lower().strip()
            if name not in _IDENTITY_STOPWORDS:
                return name
    return None


# Patterns for extracting explicitly preferred names from preference signals
_PREFERRED_NAME_PATTERNS = [
    # Must NOT be preceded by "who" — those are third-person.
    re.compile(r"(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )\bprefers?\s+to\s+be\s+called\s+([a-z]+)", re.IGNORECASE),
    re.compile(r"(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )\bprefers?\s+you\s+call\s+(?:me|them|her|him)\s+([a-z]+)", re.IGNORECASE),
    re.compile(r"(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )\bgoes\s+by\s+([a-z]+)", re.IGNORECASE),
    re.compile(r"\bgo\s+by\s+([a-z]+)", re.IGNORECASE),
    re.compile(r"(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )\bpreferred\s+name\s+is\s+([a-z]+)", re.IGNORECASE),
    re.compile(r"\bplease\s+call\s+me\s+([a-z]+)", re.IGNORECASE),
    re.compile(r"(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )\bknown\s+as\s+([a-z]+)", re.IGNORECASE),
    re.compile(r"(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )\blike\s+to\s+(?:be|go)\s+(?:by|called)\s+([a-z]+)", re.IGNORECASE),
]


def _extract_preferred_name(text: str) -> str | None:
    """Return the preferred name if a preference signal is found with an explicit name."""
    for pattern in _PREFERRED_NAME_PATTERNS:
        m = pattern.search(text)
        if m:
            name = m.group(1).lower().strip()
            if name not in _IDENTITY_STOPWORDS and len(name) > 1:
                return name
    return None

def _resolve_user_anchor(entity_id: str, user_id: str) -> str:
    """Return the canonical user UUID if entity_id matches, else return entity_id."""
    return user_id if entity_id == user_id else entity_id


def _convert_gliner_relations_to_edges(gliner_relations_dict: dict) -> list[dict] | None:
    """
    Convert GLiNER2.extract_relations() output to EdgeInput format.

    GLiNER2 returns: {
        "relation_extraction": {
            "spouse": [("marla", "user")],
            "instance_of": [("des", "person")],
        }
    }

    Convert to: [
        {"subject": "marla", "object": "user", "rel_type": "spouse", "confidence": 0.85, "fact_provenance": "gliner2"},
        {"subject": "des", "object": "person", "rel_type": "instance_of", "confidence": 0.85, "fact_provenance": "gliner2"},
    ]
    """
    # FIXED: Check for "relation_extraction" (correct GLiNER2 output format)
    if not gliner_relations_dict or "relation_extraction" not in gliner_relations_dict:
        return None

    edges = []
    relation_data = gliner_relations_dict.get("relation_extraction", {})

    # relation_data is dict of {rel_type: [(subject, object), ...], ...}
    for rel_type, entity_pairs in relation_data.items():
        if not entity_pairs:
            continue

        for subject, obj in entity_pairs:
            # Convert GLiNER2 tuple format (subject, object) to EdgeInput dict format
            subject = (subject or "").lower().strip()
            object_val = (obj or "").lower().strip()
            rel_type_clean = (rel_type or "").lower().strip()

            if subject and object_val and rel_type_clean:
                edges.append({
                    "subject": subject,
                    "object": object_val,
                    "rel_type": rel_type_clean,
                    "confidence": 0.85,  # Default GLiNER2 confidence
                    "fact_provenance": "gliner2",
                })

    return edges if edges else None


def _build_rel_type_meta(dsn: str) -> dict:
    """Load rel_types metadata (category + tail_types + storage_target + fact_class + is_symmetric + inverse_rel_type + is_hierarchy_rel + correction_behavior) from DB."""
    try:
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT rel_type, category, tail_types, storage_target, fact_class,
                           is_symmetric, inverse_rel_type, is_hierarchy_rel, correction_behavior
                    FROM rel_types
                """)
                meta = {}
                for row in cur.fetchall():
                    rel_type, category, tail_types, storage_target, fact_class, is_symmetric, inverse_rel_type, is_hierarchy_rel, correction_behavior = row
                    meta[rel_type] = {
                        "category": category,
                        "tail_types": tail_types or [],
                        "storage_target": storage_target,
                        "fact_class": fact_class,
                        "is_symmetric": is_symmetric or False,
                        "inverse_rel_type": inverse_rel_type,
                        "is_hierarchy_rel": is_hierarchy_rel or False,
                        "correction_behavior": correction_behavior or "supersede",
                    }
        return meta
    except Exception as e:
        log.warning("startup.rel_type_meta_builder_failed", error=str(e))
        return {}

def classify_fact_3d(
    rel_type: str,
    object_value: str,
    registry,
    user_id: str,
) -> dict:
    """
    Classify fact along three dimensions: STORAGE × CLASS × DIRECTION

    All determinations use rel_types metadata (deterministic on create).
    Heuristics only for unknown rel_types (rare, engine-generated).

    Returns:
        {
            "storage": "scalar" | "relational" | "hierarchical",
            "direction": "asymmetric" | "symmetric" | "hierarchical" | None,
            "is_symmetric": bool,
            "inverse_rel_type": str | None,
            "is_hierarchy_rel": bool,
            "reason": str,
        }
    """
    rt_lower = rel_type.lower()
    stripped = object_value.strip()

    if not stripped:
        return {
            "storage": None,
            "direction": None,
            "reason": "empty value",
        }

    # L0: ONTOLOGY CONSTRAINT (METADATA-FIRST)
    rel_meta = _REL_TYPE_META.get(rt_lower)

    if rel_meta:  # Known rel_type
        tail_types = rel_meta.get("tail_types", [])
        is_hierarchy = rel_meta.get("is_hierarchy_rel", False)
        is_symmetric = rel_meta.get("is_symmetric", False)
        inverse_rel = rel_meta.get("inverse_rel_type")

        # Determine storage path
        if "SCALAR" in tail_types:
            storage = "scalar"
            direction = None
        elif is_hierarchy:
            storage = "hierarchical"
            direction = "hierarchical"
        else:
            storage = "relational"
            direction = "symmetric" if is_symmetric else "asymmetric"

        return {
            "storage": storage,
            "direction": direction,
            "is_symmetric": is_symmetric,
            "inverse_rel_type": inverse_rel,
            "is_hierarchy_rel": is_hierarchy,
            "reason": f"ontology: {rt_lower} in rel_types (deterministic)",
        }

    # L1–L5: VALUE HEURISTICS (FALLBACK FOR UNKNOWN REL_TYPES)
    import re
    stripped_lower = stripped.lower()

    # L1: Numeric patterns
    if re.match(r'^-?\d+$', stripped):
        return {
            "storage": "scalar",
            "direction": None,
            "reason": "heuristic: integer pattern (unknown rel_type)",
        }

    # L2: Date patterns
    if re.match(r'^\d{4}-\d{2}-\d{2}$', stripped):
        return {
            "storage": "scalar",
            "direction": None,
            "reason": "heuristic: date pattern (unknown rel_type)",
        }

    # L3: UUID pattern (use existing _UUID_PATTERN if available)
    try:
        if _UUID_PATTERN.match(stripped):
            return {
                "storage": "relational",
                "direction": "asymmetric",
                "reason": "heuristic: UUID pattern (unknown rel_type)",
            }
    except NameError:
        pass

    # L4: Entity alias lookup
    try:
        with registry.db_conn.cursor() as cur:
            cur.execute(
                "SELECT entity_id FROM entity_aliases WHERE alias = %s AND user_id = %s LIMIT 1",
                (stripped_lower, user_id)
            )
            if cur.fetchone():
                return {
                    "storage": "relational",
                    "direction": "asymmetric",
                    "reason": "heuristic: entity alias lookup (unknown rel_type)",
                }
    except Exception:
        pass

    # L5: Email/URL/phone
    if re.match(r'^[a-zA-Z0-9._%+-]+@', stripped):
        return {
            "storage": "scalar",
            "direction": None,
            "reason": "heuristic: email pattern (unknown rel_type)",
        }

    # FALLBACK
    return {
        "storage": None,
        "direction": None,
        "reason": "no pattern match, unknown rel_type",
    }


def assign_class_and_confidence(
    classification_3d: dict,
    is_user_stated: bool,
    ontology_created: bool = False,
    hierarchy_created: bool = False,
    rel_type: str = None,
    confidence: float = None,
) -> tuple:
    """
    Assign Class (A | B | C) and confidence based on rel_type metadata + reassessed confidence.

    Metadata-driven: rel_types table defines fact_class (A | B | C) for each rel_type.
    Confidence from _assess_statement_directness() determines routing:
    - confidence >= 0.9 → Class A (user-stated, direct commitment)
    - confidence 0.7-0.9 → Class B (clear but inferred, staged)
    - confidence < 0.7 → Class C (speculative, staged with expiry)

    Penalties for in-flow metadata creation apply to final confidence.

    Class A (identity/structural):   committed immediately to facts table
    Class B (behavioral/contextual): staged, promoted when confirmed_count >= 3
    Class C (novel/ephemeral):       staged with 30-day expiry

    Returns: (class_letter, confidence_score)
    """
    if is_user_stated:
        return ("A", 1.0)

    # Use passed confidence (from _assess_statement_directness) or default to 0.4 (Class C)
    # LLM extractions without explicit confidence are speculative, not authoritative.
    current_confidence = confidence if confidence is not None else 0.4

    # LLM-inferred fact: consult metadata for defined fact_class (from rel_types table, never fallback)
    if rel_type:
        rel_type_lower = rel_type.lower()
        metadata = _REL_TYPE_CACHE.get(rel_type_lower)
        if metadata:
            defined_class = metadata.get("fact_class", "C")
        else:
            defined_class = "C"
    else:
        defined_class = "C"

    # Apply penalties for in-flow metadata creation (dprompt-98)
    # CRITICAL: Skip penalties if confidence >= 0.9 (indicates user-stated directness from _assess_statement_directness)
    # High-confidence facts are authoritative and should not be penalized for ontology creation.
    if current_confidence < 0.9:
        if ontology_created:
            current_confidence -= 0.2
        if hierarchy_created:
            current_confidence -= 0.2

    # Clamp confidence to [0.0, 1.0]
    current_confidence = max(0.0, min(1.0, current_confidence))

    # Return defined class + final confidence
    return (defined_class, current_confidence)


def enforce_directionality(
    subject: str,
    object: str,
    rel_type: str,
    is_symmetric: bool,
    inverse_rel_type: str = None,
) -> tuple:
    """
    Enforce rel_type directionality rules. Correct asymmetric rels to canonical direction.

    Examples:
    - User says "<child> is parent of me" → invert to "I am parent of <child>" (parent_of canonical)
    - User says "<spouse> is spouse of me" → store as-is (bidirectional, no correction needed)

    Returns: (corrected_subject, corrected_object, corrected_rel_type)
    """
    # Symmetric rels: no direction correction needed
    if is_symmetric:
        return (subject, object, rel_type)

    # Asymmetric rels: accept as-is and let conflict detection handle duplicates
    # (Canonical direction rules per rel_type would go here)

    return (subject, object, rel_type)


def _commit_staged(
    db_conn,
    rows: list[tuple],
    fact_class: str,
    confidence: float,
) -> int:
    """
    Insert or update rows in staged_facts.
    rows: list of (user_id, subject_id, object_id, rel_type, provenance, [definition], [storage_type], [is_hierarchy_rel], [taxonomies])
    On conflict, increments confirmed_count and refreshes last_seen_at and expires_at.
    Returns count of rows attempted.
    """
    count = 0
    try:
        with db_conn.cursor() as cur:
            for row in rows:
                user_id, subject, obj, rel_type, prov = row[0], row[1], row[2], row[3], row[4]
                definition = row[5] if len(row) > 5 else ''
                storage_type = row[6] if len(row) > 6 else None
                is_hierarchy_rel = row[7] if len(row) > 7 else False
                taxonomies = row[8] if len(row) > 8 else []

                cur.execute(
                    "INSERT INTO staged_facts"
                    " (user_id, subject_id, object_id, rel_type, fact_class,"
                    "  provenance, confidence, expires_at, rel_type_definition, storage_type, is_hierarchy_rel, taxonomies)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, now() + interval '30 days', %s, %s, %s, %s)"
                    " ON CONFLICT (user_id, subject_id, object_id, rel_type)"
                    " DO UPDATE SET"
                    "   confirmed_count = staged_facts.confirmed_count + 1,"
                    "   last_seen_at    = now(),"
                    "   expires_at      = now() + interval '30 days',"
                    "   confidence      = GREATEST(staged_facts.confidence, EXCLUDED.confidence),"
                    "   qdrant_synced   = false,"
                    "   rel_type_definition = EXCLUDED.rel_type_definition,"
                    "   storage_type = COALESCE(EXCLUDED.storage_type, staged_facts.storage_type),"
                    "   taxonomies = COALESCE(EXCLUDED.taxonomies, staged_facts.taxonomies)",
                    (user_id, subject, obj, rel_type, fact_class, prov, confidence, definition, storage_type, is_hierarchy_rel, taxonomies),
                )
                count += 1
        db_conn.commit()
        return count
    except Exception as e:
        db_conn.rollback()
        log.error("ingest.staged_commit_failed",
                  err=str(e),
                  row_count=len(rows),
                  fact_class=fact_class,
                  first_row=str(rows[0]) if rows else "empty")
        return 0

def get_gliner_model():
    return _gliner2_model

def _cleanup_entity_aliases_startup(dsn: str) -> None:
    """
    Clean up corrupted entity_aliases entries where entity_id is a string (not UUID).
    Entity IDs must always be UUID v5 surrogates or 'user'.
    This is idempotent and safe to run repeatedly.
    """
    try:
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                # Find entity_aliases with string entity_ids (not UUID, not 'user')
                cur.execute("""
                    SELECT COUNT(*) FROM entity_aliases
                    WHERE entity_id NOT LIKE '%-%-%-%-%' AND entity_id != 'user'
                """)
                bad_count = cur.fetchone()[0]

                if bad_count == 0:
                    log.info("startup.entity_aliases_cleanup", status="ok")
                    return

                log.warning("startup.entity_aliases_corrupted_found",
                           corrupted_count=bad_count)

                # Delete corrupted entries (string entity_ids)
                # When the system re-ingests the correct edges, it will re-register
                # with proper UUID entity_ids via registry.register_alias()
                cur.execute("""
                    DELETE FROM entity_aliases
                    WHERE entity_id NOT LIKE '%-%-%-%-%' AND entity_id != 'user'
                """)
                deleted = cur.rowcount
                conn.commit()

                log.info("startup.entity_aliases_cleanup_deleted",
                        deleted_count=deleted)
    except Exception as e:
        log.error("startup.entity_aliases_cleanup_failed", error=str(e))


def _normalize_entity_ids_startup(dsn: str) -> None:
    """
    Normalize string entity_ids to UUID v5 surrogates at startup.
    This is idempotent and safe to run repeatedly.

    Scans facts/staged_facts for non-UUID entity_ids and converts them
    using EntityRegistry._make_surrogate() logic.
    """
    try:
        from src.entity_registry.registry import _make_surrogate

        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                # Find all string entity_ids
                # CRITICAL: Exclude scalar rel_types (also_known_as, pref_name) from object_id
                # normalization — their objects are display names, not entity references.
                cur.execute("""
                    SELECT DISTINCT user_id, subject_id FROM facts
                    WHERE subject_id NOT LIKE '%-%-%-%-%'
                    UNION
                    SELECT DISTINCT user_id, object_id FROM facts
                    WHERE object_id NOT LIKE '%-%-%-%-%'
                      AND rel_type NOT IN ('also_known_as', 'pref_name')
                    UNION
                    SELECT DISTINCT user_id, subject_id FROM staged_facts
                    WHERE subject_id NOT LIKE '%-%-%-%-%'
                    UNION
                    SELECT DISTINCT user_id, object_id FROM staged_facts
                    WHERE object_id NOT LIKE '%-%-%-%-%'
                      AND rel_type NOT IN ('also_known_as', 'pref_name')
                """)
                string_ids = cur.fetchall()

                if not string_ids:
                    log.info("startup.entity_id_normalization_check", status="ok")
                    return

                log.info("startup.entity_id_normalization_starting",
                         string_id_count=len(string_ids))

                # Build mapping from (user_id, string_id) -> surrogate UUID
                entity_map = {}
                for user_id, string_id in string_ids:
                    if not _UUID_PATTERN.match(string_id):
                        surrogate = _make_surrogate(user_id, string_id)
                        entity_map[(user_id, string_id)] = surrogate

                # Update facts table
                updated_count = 0
                for (user_id, string_id), surrogate in entity_map.items():
                    cur.execute(
                        "UPDATE facts SET subject_id = %s WHERE user_id = %s AND subject_id = %s",
                        (surrogate, user_id, string_id)
                    )
                    updated_count += cur.rowcount

                    cur.execute(
                        "UPDATE facts SET object_id = %s WHERE user_id = %s AND object_id = %s",
                        (surrogate, user_id, string_id)
                    )
                    updated_count += cur.rowcount

                # Update staged_facts table
                for (user_id, string_id), surrogate in entity_map.items():
                    cur.execute(
                        "UPDATE staged_facts SET subject_id = %s WHERE user_id = %s AND subject_id = %s",
                        (surrogate, user_id, string_id)
                    )
                    updated_count += cur.rowcount

                    cur.execute(
                        "UPDATE staged_facts SET object_id = %s WHERE user_id = %s AND object_id = %s",
                        (surrogate, user_id, string_id)
                    )
                    updated_count += cur.rowcount

                # Ensure entities are registered
                for (user_id, string_id), surrogate in entity_map.items():
                    cur.execute(
                        "INSERT INTO entities (id, user_id, entity_type) VALUES (%s, %s, 'unknown') "
                        "ON CONFLICT (id, user_id) DO NOTHING",
                        (surrogate, user_id)
                    )

                # Sync aliases
                for (user_id, string_id), surrogate in entity_map.items():
                    cur.execute(
                        "INSERT INTO entity_aliases (entity_id, user_id, alias, is_preferred) "
                        "VALUES (%s, %s, %s, true) ON CONFLICT (user_id, alias) DO UPDATE SET entity_id = EXCLUDED.entity_id, is_preferred = EXCLUDED.is_preferred",
                        (surrogate, user_id, string_id.lower())
                    )

                conn.commit()
                log.info("startup.entity_id_normalization_complete",
                         string_ids_processed=len(entity_map),
                         rows_updated=updated_count)
    except Exception as e:
        log.error("startup.entity_id_normalization_failed", error=str(e))


def _ensure_schema(dsn: str) -> None:
    """
    Apply any pending schema migrations at startup.
    This is the idempotent equivalent of running migration SQL files —
    column renames, type changes, seed data. Each statement uses IF EXISTS
    or information_schema checks so it's safe to run on any DB state.
    """
    try:
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                # Rename allowed_head → head_types if the old column exists
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'rel_types' AND column_name = 'allowed_head'"
                )
                if cur.fetchone():
                    cur.execute("ALTER TABLE rel_types RENAME COLUMN allowed_head TO head_types")
                    log.info("startup.schema_rename", old="allowed_head", new="head_types")

                # Rename allowed_tail → tail_types if the old column exists
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'rel_types' AND column_name = 'allowed_tail'"
                )
                if cur.fetchone():
                    cur.execute("ALTER TABLE rel_types RENAME COLUMN allowed_tail TO tail_types")
                    log.info("startup.schema_rename", old="allowed_tail", new="tail_types")

                # Alter staged_facts.user_id from UUID to TEXT (conditional)
                cur.execute(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'staged_facts' AND column_name = 'user_id'"
                )
                row = cur.fetchone()
                if row and row[0].upper() == 'UUID':
                    cur.execute("ALTER TABLE staged_facts ALTER COLUMN user_id TYPE TEXT")
                    cur.execute("ALTER TABLE staged_facts ALTER COLUMN subject_id TYPE TEXT")
                    cur.execute("ALTER TABLE staged_facts ALTER COLUMN object_id TYPE TEXT")
                    log.info("startup.schema_staged_facts_uuid_to_text")

                # Seed missing rel_types that are referenced in code but might
                # not exist in the DB (safe — ON CONFLICT DO NOTHING)
                _MISSING_TYPES = [
                    ("lives_at", "Lives At", "location", "supersede"),
                    ("located_at", "Located At", "location", "supersede"),
                    ("has_pet", "Has Pet", "pets", "supersede"),
                    ("height", "Height", "physical", "supersede"),
                    ("weight", "Weight", "physical", "supersede"),
                    ("has_ip", "Has IP Address", "system", "supersede"),
                    ("has_os", "Has Operating System", "system", "supersede"),
                    ("has_hostname", "Has Hostname", "system", "supersede"),
                    ("hostname", "Hostname", "system", "supersede"),
                    ("fqdn", "Fully Qualified Domain Name", "system", "supersede"),
                    ("ip_address", "IP Address", "system", "supersede"),
                    ("member_of", "Member Of", "identity", "supersede"),
                ]
                for rel_type, label, category, correction_behavior in _MISSING_TYPES:
                    cur.execute(
                        "INSERT INTO rel_types (rel_type, label, category, correction_behavior, source) "
                        "VALUES (%s, %s, %s, %s, 'builtin') "
                        "ON CONFLICT (rel_type) DO NOTHING",
                        (rel_type, label, category, correction_behavior),
                    )

                # ── Migration 019: entity_taxonomies (data-driven grouping system) ──
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS entity_taxonomies (
                        id BIGSERIAL PRIMARY KEY,
                        taxonomy_name VARCHAR(64) NOT NULL UNIQUE,
                        alicecription TEXT,
                        member_entity_types TEXT[] NOT NULL DEFAULT '{}',
                        rel_types_defining_group TEXT[] NOT NULL DEFAULT '{}',
                        has_transitivity BOOLEAN DEFAULT false,
                        transitive_rel_types TEXT[] DEFAULT '{}',
                        is_hierarchical BOOLEAN DEFAULT false,
                        parent_rel_type VARCHAR(64),
                        source VARCHAR(32) DEFAULT 'seeded',
                        created_at TIMESTAMP DEFAULT now()
                    )
                """)

                # Taxonomy seeding removed (dprompt-86).
                # Rationale: Hardcoded seeding creates brittleness (stale references like 'body_parts'
                # that don't exist, breaking extraction). Taxonomies should emerge from data via
                # self-building ontology. Graph traversal + LLM are authoritative for entity relationships.
                # Taxonomies table remains for future data-driven population and query-time filtering.

                # ── Migration 022: rel_types metadata (dprompt-65) ──
                # Add validation columns to rel_types + pre-populate metadata.
                # Idempotent: uses IF NOT EXISTS for columns, UPDATE for data.
                cur.execute("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                       WHERE table_name='rel_types' AND column_name='is_symmetric')
                        THEN ALTER TABLE rel_types ADD COLUMN is_symmetric BOOLEAN DEFAULT FALSE; END IF;
                        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                       WHERE table_name='rel_types' AND column_name='inverse_rel_type')
                        THEN ALTER TABLE rel_types ADD COLUMN inverse_rel_type VARCHAR(100) DEFAULT NULL; END IF;
                        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                       WHERE table_name='rel_types' AND column_name='is_leaf_only')
                        THEN ALTER TABLE rel_types ADD COLUMN is_leaf_only BOOLEAN DEFAULT FALSE; END IF;
                        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                       WHERE table_name='rel_types' AND column_name='is_hierarchy_rel')
                        THEN ALTER TABLE rel_types ADD COLUMN is_hierarchy_rel BOOLEAN DEFAULT FALSE; END IF;
                        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                       WHERE table_name='rel_types' AND column_name='allows_leaf_rels')
                        THEN ALTER TABLE rel_types ADD COLUMN allows_leaf_rels TEXT[] DEFAULT NULL; END IF;
                    END $$;
                """)
                cur.execute("UPDATE rel_types SET is_symmetric=TRUE WHERE rel_type IN ('spouse','sibling_of','knows','friend_of','met','same_as')")
                cur.execute("UPDATE rel_types SET inverse_rel_type='child_of' WHERE rel_type='parent_of'")
                cur.execute("UPDATE rel_types SET inverse_rel_type='parent_of' WHERE rel_type='child_of'")
                # is_leaf_only constraint removed (dprompt-86). Rationale: Too restrictive — prevents
                # legitimate facts like (user, lives_at, address) where address has instance_of type info.
                # Semantic conflict detection should not block leaf relationships on typed entities.
                cur.execute("UPDATE rel_types SET is_hierarchy_rel=TRUE WHERE rel_type IN ('instance_of','subclass_of','member_of','part_of','is_a')")

                # ── Migration 028: rel_type_definition column (dprompt-85) ──
                cur.execute("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                       WHERE table_name='facts' AND column_name='rel_type_definition')
                        THEN ALTER TABLE facts ADD COLUMN rel_type_definition TEXT DEFAULT ''; END IF;
                        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                       WHERE table_name='staged_facts' AND column_name='rel_type_definition')
                        THEN ALTER TABLE staged_facts ADD COLUMN rel_type_definition TEXT DEFAULT ''; END IF;
                    END $$;
                """)

                conn.commit()
                log.info("startup.schema_check_complete")
    except Exception as e:
        log.warning("startup.schema_check_failed", error=str(e))


# ── Entity Taxonomies (dprompt-20) ───────────────────────────────────────────
# Data-driven grouping system — replaces brittle hardcoded extraction patterns.

_TAXONOMY_CACHE: dict = {}


def _apply_taxonomy_rules(
    rows: list[tuple],
    user_id: str,
    db_conn,
) -> list[tuple]:
    """
    Given ingest rows, check each fact against registered taxonomies.
    CHAIN: determine → search → create/link

    For each unique rel_type:
      1. Search existing taxonomies (via _TAXONOMY_CACHE)
      2. If NO match → discover via LLM
      3. If discovered → INSERT into entity_taxonomies (immediate commit)
      4. Reload cache so subsequent facts see new taxonomy
      5. Annotate rows with taxonomy context

    Returns rows with taxonomy annotations (same rows, enhanced with taxonomy metadata).
    Does NOT change fact_class or storage path.
    """
    global _TAXONOMY_CACHE

    if not rows:
        return rows

    # Step 1: Collect unique rel_types from this ingest batch
    unique_rel_types = set()
    for row in rows:
        rel_type = row[3] if len(row) > 3 else None
        if rel_type:
            unique_rel_types.add(rel_type.lower().strip())

    # Step 2: For each rel_type, check existing taxonomies and discover if needed
    for rt_lower in unique_rel_types:
        # Check 1: Does this rel_type already match an existing taxonomy?
        found_in_existing = False
        for tax_name, tax_meta in _TAXONOMY_CACHE.items():
            def_rels = [r.lower() for r in tax_meta.get("rel_types_defining_group", []) if r]
            if rt_lower in def_rels:
                log.info("ingest.taxonomy_match_existing",
                        rel_type=rt_lower, taxonomy=tax_name)
                found_in_existing = True
                break

        if found_in_existing:
            continue

        # Check 2: Not in existing taxonomy — attempt LLM discovery
        try:
            # Fetch natural language definition from rel_types table for LLM context
            # LLMs reason better with semantic definitions than raw rel_type strings
            natural_lang = None
            try:
                with db_conn.cursor() as cur:
                    cur.execute(
                        "SELECT natural_language, label, examples FROM rel_types WHERE rel_type = %s",
                        (rt_lower,)
                    )
                    rt_row = cur.fetchone()
                    if rt_row:
                        natural_lang = rt_row[0]  # natural_language definition
            except Exception as _nl_err:
                log.debug("ingest.natural_language_lookup_failed",
                         rel_type=rt_lower, error=str(_nl_err))

            # Collect facts with this rel_type + semantic definition for LLM context
            discovery_facts = [{"rel_type": rt_lower, "natural_language": natural_lang}]

            # dprompt-142: Removed qwen_url parameter — now uses centralized resolver
            discovered = _llm_discover_taxonomy_from_facts(db_conn, user_id, discovery_facts)

            if not discovered:
                log.info("ingest.taxonomy_discovery_declined",
                        rel_type=rt_lower, reason="llm_no_natural_grouping")
                continue

            # Check 3: Create taxonomy in DB with immediate commit
            taxonomy_name = discovered.get("taxonomy_name")
            if not taxonomy_name:
                log.info("ingest.taxonomy_discovery_no_name",
                        rel_type=rt_lower)
                continue

            # Insert with proper DB context and commit
            try:
                with db_conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO entity_taxonomies "
                        "(taxonomy_name, description, member_entity_types, rel_types_defining_group, "
                        "has_transitivity, transitive_rel_types, is_hierarchical, parent_rel_type, source) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (taxonomy_name) DO NOTHING",
                        (
                            taxonomy_name,
                            discovered.get("description", ""),
                            discovered.get("member_entity_types", "{}"),
                            discovered.get("rel_types_defining_group", []),
                            discovered.get("has_transitivity", False),
                            discovered.get("transitive_rel_types", "{}"),
                            discovered.get("is_hierarchical", False),
                            discovered.get("parent_rel_type"),
                            "engine_learned_ingest",
                        ),
                    )
                db_conn.commit()

                # Step 4: Reload taxonomy cache immediately
                _load_taxonomy_cache(db_conn)

                log.info("ingest.taxonomy_created_and_cached",
                        taxonomy_name=taxonomy_name,
                        rel_types=discovered.get("rel_types_defining_group", []),
                        source="engine_learned_ingest")

            except Exception as _db_err:
                log.warning("ingest.taxonomy_insert_failed",
                           taxonomy_name=taxonomy_name,
                           error=str(_db_err))
                db_conn.rollback()
                # Continue — don't fail ingest if taxonomy creation fails

        except Exception as _discovery_err:
            log.warning("ingest.taxonomy_discovery_failed",
                       rel_type=rt_lower,
                       error=str(_discovery_err))
            # Continue — don't fail ingest if discovery fails

    # Step 5: Annotate rows with taxonomy context (for query expansion later)
    for i, row in enumerate(rows):
        rt_lower = (row[3] or "").lower().strip() if len(row) > 3 else ""

        # Find which taxonomy(ies) this rel_type defines
        taxonomies_for_rel = []
        for tax_name, tax_meta in _TAXONOMY_CACHE.items():
            def_rels = [r.lower() for r in tax_meta.get("rel_types_defining_group", []) if r]
            if rt_lower in def_rels:
                taxonomies_for_rel.append(tax_name)

        # If row has taxonomy context slot, update it
        if len(row) > 12:  # taxonomies at index 12
            rows[i] = row[:12] + (taxonomies_for_rel,) + row[13:]
        elif len(row) == 12:  # Add taxonomy slot if missing
            rows[i] = row + (taxonomies_for_rel,)

        if taxonomies_for_rel:
            log.info("ingest.row_annotated_with_taxonomy",
                    rel_type=rt_lower, taxonomies=taxonomies_for_rel)

    return rows


def _llm_discover_taxonomy_from_facts(
    db_conn,
    user_id: str,
    facts: list[dict],
) -> dict | None:
    """
    Analyze fetched facts to discover what taxonomy they might define.
    Uses centralized endpoint resolver.
    Returns dict for INSERT into entity_taxonomies, or None if discovery fails.

    Sends to LLM:
    1. Examples of existing taxonomies (structure + rel_types)
    2. The facts that were fetched (rel_types, count)
    3. Request: "What taxonomy group do these rel_types define?"

    LLM returns: {taxonomy_name, description, rel_types_defining_group, ...}
    """
    if not facts:
        return None

    try:
        # Fetch ALL existing taxonomies as dynamic, growing examples (no hardcoded limits)
        # As the system learns, these examples inform future discovery
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT taxonomy_name, description, rel_types_defining_group "
                "FROM entity_taxonomies ORDER BY created_at DESC"
            )
            examples = cur.fetchall()

        if not examples:
            log.info("taxonomy.discover_skipped", reason="no_existing_taxonomies_to_use_as_context")
            return None

        # Analyze facts: collect rel_types with natural language definitions
        rel_types_in_facts = [f.get("rel_type") for f in facts if f.get("rel_type")]
        rel_type_counts = {}
        rel_type_definitions = {}  # rel_type → natural_language
        for fact in facts:
            rt = fact.get("rel_type")
            if rt:
                rt_lower = rt.lower()
                rel_type_counts[rt_lower] = rel_type_counts.get(rt_lower, 0) + 1
                # Use provided natural_language if available (from rel_types table)
                if "natural_language" in fact and fact["natural_language"]:
                    rel_type_definitions[rt_lower] = fact["natural_language"]

        # Build dynamic examples from actual entity_taxonomies (growing as system learns)
        # Fetch semantic definitions for rel_types in each taxonomy
        examples_with_semantics = []
        for tax_name, tax_desc, tax_rels in examples:
            # Fetch natural_language definitions for rel_types in this taxonomy
            rel_defs = []
            if tax_rels:
                try:
                    placeholders = ",".join(["%s"] * len(tax_rels))
                    with db_conn.cursor() as cur:
                        cur.execute(
                            f"SELECT rel_type, natural_language FROM rel_types WHERE rel_type IN ({placeholders})",
                            tax_rels
                        )
                        for rel_type, natural_lang in cur.fetchall():
                            rel_defs.append(f"{rel_type} ({natural_lang or 'no definition'})")
                except Exception as _e:
                    # Fallback: use raw rel_types if lookup fails
                    rel_defs = tax_rels

            rel_defs_str = ", ".join(rel_defs) if rel_defs else "(no rel_types)"
            examples_with_semantics.append(
                f"- {tax_name}: {tax_desc or '(no description)'}\n  Defines members via: {rel_defs_str}"
            )

        examples_text = "\n".join(examples_with_semantics)

        # Build rel_types text with semantic definitions for stronger reasoning
        rel_types_with_defs = []
        for rt, cnt in sorted(rel_type_counts.items(), key=lambda x: -x[1]):
            if rt in rel_type_definitions:
                # Include semantic definition: much stronger for LLM reasoning
                rel_types_with_defs.append(f"- {rt}: {rel_type_definitions[rt]}")
            else:
                # Fallback to raw rel_type
                rel_types_with_defs.append(f"- {rt}")
        rel_types_text = "\n".join(rel_types_with_defs)

        prompt = f"""Analyze the following rel_types and their semantic meanings. Suggest if they define a new taxonomy group.

EXISTING TAXONOMIES (learned examples—use these as reference for patterns):
{examples_text}

REL_TYPES BEING ANALYZED (with semantic definitions):
{rel_types_text}

TASK: Do these rel_types define a natural grouping (taxonomy)?
Look at the semantic meanings. Similar to existing taxonomies:
- Taxonomies group rel_types that share a common semantic domain
- Each rel_type in the group contributes to defining membership in that domain
- E.g., family groups kinship rel_types; work groups employment rel_types

If these rel_types form a natural semantic grouping, respond with JSON:
{{"taxonomy_name": "name", "description": "brief description explaining the grouping", "rel_types_defining_group": ["rel1", "rel2"]}}

If they do NOT form a natural grouping, respond with:
{{"taxonomy_name": null}}

Respond ONLY with valid JSON, no markdown or explanation."""

        messages = [{"role": "user", "content": prompt}]
        payload = build_llm_payload(
            messages=messages,
            model=os.getenv("WGM_LLM_MODEL", "qwen/qwen3.5-9b"),
            user_id=user_id,
            system="You are an ontology expert.",
            temperature=0.0,
            max_tokens=256,
            # NOTE: thinking parameter removed — Qwen doesn't support extended thinking
        )

        # dprompt-142: Use centralized endpoint resolver
        llm_url = _resolve_llm_endpoint(with_fallback=False)
        resp = _http_client_sync.post(llm_url, json=payload, headers=get_llm_headers(), timeout=10)
        resp.raise_for_status()

        result = resp.json()
        if not result.get("choices"):
            return None

        content = result["choices"][0].get("message", {}).get("content", "").strip()
        if not content:
            return None

        # Parse JSON response
        import json
        parsed = json.loads(content)
        if not parsed.get("taxonomy_name"):
            log.info("taxonomy.discover_llm_declined", rel_types=list(rel_type_counts.keys()))
            return None

        # Validate taxonomy_name
        tax_name = parsed.get("taxonomy_name", "").lower().strip()
        if not tax_name or len(tax_name) > 64 or not all(c.isalnum() or c == '_' for c in tax_name):
            log.warning("taxonomy.discover_invalid_name", name=tax_name)
            return None

        taxonomy_def = {
            "taxonomy_name": tax_name,
            "description": parsed.get("description", ""),
            "member_entity_types": "{}",  # Will be populated as facts are classified
            "rel_types_defining_group": parsed.get("rel_types_defining_group", []),
            "has_transitivity": False,
            "transitive_rel_types": "{}",
            "is_hierarchical": False,
            "parent_rel_type": None,
            "source": "llm_learned",
        }

        log.info("taxonomy.discovered_by_llm", taxonomy_name=tax_name,
                 defining_rels=taxonomy_def["rel_types_defining_group"])
        return taxonomy_def

    except Exception as e:
        log.warning("taxonomy.discover_failed", error=str(e))
        return None


def _fetch_transitive_members(
    db_conn,
    user_id: str,
    taxonomy_name: str,
) -> set[str]:
    """
    Given a taxonomy name (e.g., 'family'), return all entity UUIDs that are
    transitive members — direct relations PLUS entities reachable via
    transitive_rel_types from direct members.
    """
    tax = _TAXONOMY_CACHE.get(taxonomy_name)
    if not tax or not tax.get("has_transitivity"):
        return set()

    try:
        with db_conn.cursor() as cur:
            # Direct members: entities related to user via defining rel_types
            cur.execute(
                "SELECT DISTINCT object_id FROM facts"
                " WHERE user_id = %s AND subject_id = %s"
                " AND rel_type = ANY(%s)"
                " AND superseded_at IS NULL",
                (user_id, user_id, tax["rel_types_defining_group"]),
            )
            direct = {row[0] for row in cur.fetchall()}

            # Also include from staged_facts
            cur.execute(
                "SELECT DISTINCT object_id FROM staged_facts"
                " WHERE user_id = %s AND subject_id = %s"
                " AND rel_type = ANY(%s)",
                (user_id, user_id, tax["rel_types_defining_group"]),
            )
            direct.update(row[0] for row in cur.fetchall())

            if not direct:
                return set()

            # Transitive members: for each direct member, find entities via transitive_rel_types
            transitive = set()
            trans_rels = tax.get("transitive_rel_types", [])
            if trans_rels:
                # Use a batch query approach for efficiency
                member_list = list(direct)
                cur.execute(
                    "SELECT DISTINCT object_id FROM facts"
                    " WHERE user_id = %s AND subject_id = ANY(%s)"
                    " AND rel_type = ANY(%s)"
                    " AND superseded_at IS NULL",
                    (user_id, member_list, trans_rels),
                )
                transitive.update(row[0] for row in cur.fetchall())

                cur.execute(
                    "SELECT DISTINCT object_id FROM staged_facts"
                    " WHERE user_id = %s AND subject_id = ANY(%s)"
                    " AND rel_type = ANY(%s)",
                    (user_id, member_list, trans_rels),
                )
                transitive.update(row[0] for row in cur.fetchall())

            all_members = direct | transitive
            log.info(
                "taxonomy.transitive_members",
                taxonomy=taxonomy_name,
                direct_count=len(direct),
                transitive_count=len(transitive),
                total=len(all_members),
            )
            return all_members
    except Exception as e:
        log.warning("taxonomy.transitive_members_failed", error=str(e), taxonomy=taxonomy_name)
        return set()


def _graph_traverse(
    db_conn,
    user_id: str,
    entity_id: str,
    max_hops: int = 1,
    graph_rel_types: frozenset = None,
) -> set[str]:
    """
    Single-hop graph traversal — find all entities directly connected to entity_id
    via rel_types in graph_rel_types. Searches both facts and staged_facts.

    Returns set of connected entity UUIDs (does NOT include the starting entity).
    """
    if graph_rel_types is None:
        graph_rel_types = _get_graph_rels()
    connected: set[str] = set()
    graph_rels = list(graph_rel_types)
    try:
        with db_conn.cursor() as cur:
            # Find entities where entity_id is the subject
            cur.execute(
                "SELECT DISTINCT object_id FROM facts"
                " WHERE user_id = %s AND subject_id = %s"
                " AND rel_type = ANY(%s)"
                " AND superseded_at IS NULL",
                (user_id, entity_id, graph_rels),
            )
            connected.update(row[0] for row in cur.fetchall())

            # Find entities where entity_id is the object
            cur.execute(
                "SELECT DISTINCT subject_id FROM facts"
                " WHERE user_id = %s AND object_id = %s"
                " AND rel_type = ANY(%s)"
                " AND superseded_at IS NULL",
                (user_id, entity_id, graph_rels),
            )
            connected.update(row[0] for row in cur.fetchall())

            # Also search staged_facts
            cur.execute(
                "SELECT DISTINCT object_id FROM staged_facts"
                " WHERE user_id = %s AND subject_id = %s"
                " AND rel_type = ANY(%s)",
                (user_id, entity_id, graph_rels),
            )
            connected.update(row[0] for row in cur.fetchall())

            cur.execute(
                "SELECT DISTINCT subject_id FROM staged_facts"
                " WHERE user_id = %s AND object_id = %s"
                " AND rel_type = ANY(%s)",
                (user_id, entity_id, graph_rels),
            )
            connected.update(row[0] for row in cur.fetchall())

        connected.discard(entity_id)  # Don't include self
        return connected
    except Exception as e:
        log.warning("graph_traverse.failed", error=str(e), entity_id=entity_id)
        return set()


def _hierarchy_expand(
    db_conn,
    user_id: str,
    entity_id: str,
    direction: str = "up",
    max_depth: int = 3,
) -> set[str]:
    """
    Traverse hierarchy chains from entity_id via _REL_TYPE_HIERARCHY rel_types.
    Uses SQL CTE (WITH RECURSIVE) with cycle protection via depth tracking.

    direction="up":  entity → instance_of/subclass_of → parent class (classification chain)
    direction="down": class → instance_of/subclass_of → members (class membership)

    Returns set of entity UUIDs in the chain (inclualice the starting entity).
    """
    hier_rels = list(_get_hierarchy_rels())
    chain: set[str] = {entity_id}

    try:
        with db_conn.cursor() as cur:
            if direction == "up":
                cur.execute("""
                    WITH RECURSIVE hierarchy_chain AS (
                        SELECT subject_id, object_id, rel_type, 1 AS depth
                        FROM facts
                        WHERE user_id = %s AND subject_id = %s
                          AND rel_type = ANY(%s)
                          AND superseded_at IS NULL

                        UNION ALL

                        SELECT f.subject_id, f.object_id, f.rel_type, hc.depth + 1
                        FROM facts f
                        JOIN hierarchy_chain hc ON f.subject_id = hc.object_id
                        WHERE f.user_id = %s
                          AND f.rel_type = ANY(%s)
                          AND f.superseded_at IS NULL
                          AND hc.depth < %s
                    )
                    SELECT DISTINCT object_id FROM hierarchy_chain
                """, (user_id, entity_id, hier_rels, user_id, hier_rels, max_depth))
                chain.update(row[0] for row in cur.fetchall())

                # Also search staged_facts
                cur.execute("""
                    WITH RECURSIVE hierarchy_chain AS (
                        SELECT subject_id, object_id, rel_type, 1 AS depth
                        FROM staged_facts
                        WHERE user_id = %s AND subject_id = %s
                          AND rel_type = ANY(%s)

                        UNION ALL

                        SELECT f.subject_id, f.object_id, f.rel_type, hc.depth + 1
                        FROM staged_facts f
                        JOIN hierarchy_chain hc ON f.subject_id = hc.object_id
                        WHERE f.user_id = %s
                          AND f.rel_type = ANY(%s)
                          AND hc.depth < %s
                    )
                    SELECT DISTINCT object_id FROM hierarchy_chain
                """, (user_id, entity_id, hier_rels, user_id, hier_rels, max_depth))
                chain.update(row[0] for row in cur.fetchall())

            elif direction == "down":
                cur.execute("""
                    WITH RECURSIVE hierarchy_chain AS (
                        SELECT subject_id, object_id, rel_type, 1 AS depth
                        FROM facts
                        WHERE user_id = %s AND object_id = %s
                          AND rel_type = ANY(%s)
                          AND superseded_at IS NULL

                        UNION ALL

                        SELECT f.subject_id, f.object_id, f.rel_type, hc.depth + 1
                        FROM facts f
                        JOIN hierarchy_chain hc ON f.object_id = hc.subject_id
                        WHERE f.user_id = %s
                          AND f.rel_type = ANY(%s)
                          AND f.superseded_at IS NULL
                          AND hc.depth < %s
                    )
                    SELECT DISTINCT subject_id FROM hierarchy_chain
                """, (user_id, entity_id, hier_rels, user_id, hier_rels, max_depth))
                chain.update(row[0] for row in cur.fetchall())

                cur.execute("""
                    WITH RECURSIVE hierarchy_chain AS (
                        SELECT subject_id, object_id, rel_type, 1 AS depth
                        FROM staged_facts
                        WHERE user_id = %s AND object_id = %s
                          AND rel_type = ANY(%s)

                        UNION ALL

                        SELECT f.subject_id, f.object_id, f.rel_type, hc.depth + 1
                        FROM staged_facts f
                        JOIN hierarchy_chain hc ON f.object_id = hc.subject_id
                        WHERE f.user_id = %s
                          AND f.rel_type = ANY(%s)
                          AND hc.depth < %s
                    )
                    SELECT DISTINCT subject_id FROM hierarchy_chain
                """, (user_id, entity_id, hier_rels, user_id, hier_rels, max_depth))
                chain.update(row[0] for row in cur.fetchall())

        return chain
    except Exception as e:
        log.warning("hierarchy_expand.failed", error=str(e), entity_id=entity_id, direction=direction)
        return {entity_id}


# ── dprompt-59: Semantic Conflict Detection ──────────────────────────────────
# Detects when new facts contradict existing graph structure and auto-resolves.
# The graph IS the source of truth — hierarchy/type relationships define what
# entities ARE, and independent relationships (owns, has_pet, etc.) must respect
# those semantics.

# ── dprompt-65: Metadata-driven — queries rel_types table instead of hardcoded ─
# Module-level metadata cache, populated lazily and refreshed on cache miss.
# ── dprompt-73b: Unified metadata cache (replaces per-query lookup) ──────────
# Single cache for all rel_types (known + novel). Loaded at startup via
# _refresh_rel_type_cache(), refreshed when re-embedder approves novel types.
# Eliminates hardcoded frozensets — routing and classification are metadata-driven.

_REL_TYPE_CACHE: dict[str, dict] = {}

# Metadata-driven scalar rel_types cache (replaces hardcoded _SCALAR_OBJECT_RELS)
# Populated from rel_types table where tail_types contains 'SCALAR'
# Refreshed whenever novel rel_types are approved
_SCALAR_REL_TYPES_CACHE: set[str] = set()



def _refresh_rel_type_cache():
    """Load ALL rel_types from database into unified cache.
    Called at startup and when re-embedder approves novel rel_types.
    Uses psycopg2 directly (not SQLAlchemy) — consistent with the rest of main.py.
    """
    global _REL_TYPE_CACHE
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        log.warning("rel_type_cache.no_dsn")
        return
    try:
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT rel_type, storage_target, fact_class, "
                    "is_symmetric, inverse_rel_type, is_leaf_only, is_hierarchy_rel, "
                    "correction_behavior "
                    "FROM rel_types"
                )
                _REL_TYPE_CACHE.clear()
                for row in cur.fetchall():
                    _REL_TYPE_CACHE[row[0].lower()] = {
                        "storage_target": row[1] if row[1] else "facts",
                        "fact_class": row[2] if row[2] else "C",
                        "is_symmetric": row[3] if row[3] else False,
                        "inverse_rel_type": row[4],
                        "is_leaf_only": row[5] if row[5] else False,
                        "is_hierarchy_rel": row[6] if row[6] else False,
                        "correction_behavior": row[7] if row[7] else "supersede",
                    }
        log.info("rel_type_cache.refreshed", count=len(_REL_TYPE_CACHE))
    except Exception as e:
        log.warning("rel_type_cache.refresh_failed", error=str(e))


def _refresh_scalar_rel_types_cache():
    """Load rel_types with SCALAR tail_types from database into cache.
    Called at startup and when novel rel_types are approved.
    Metadata-driven: queries rel_types table, no hardcoding.
    """
    global _SCALAR_REL_TYPES_CACHE
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        log.warning("scalar_rel_types_cache.no_dsn")
        return
    try:
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT rel_type FROM rel_types WHERE tail_types @> '{SCALAR}'::TEXT[]"
                )
                _SCALAR_REL_TYPES_CACHE.clear()
                for row in cur.fetchall():
                    _SCALAR_REL_TYPES_CACHE.add(row[0].lower())
        log.info("scalar_rel_types_cache.refreshed", count=len(_SCALAR_REL_TYPES_CACHE))
    except Exception as e:
        log.warning("scalar_rel_types_cache.refresh_failed", error=str(e))


def _is_scalar_rel_type(rel_type: str) -> bool:
    """Check if rel_type has SCALAR tail_types.
    Queries cache first, then DB directly for novel types.
    Falls back to fallback set if DB unavailable.
    """
    rt = rel_type.lower().strip() if rel_type else ""
    if not rt:
        return False

    # Check cache first (faster)
    if _SCALAR_REL_TYPES_CACHE and rt in _SCALAR_REL_TYPES_CACHE:
        return True

    # Query DB directly for novel types approved by re_embedder
    dsn = os.environ.get("POSTGRES_DSN")
    if dsn:
        try:
            with psycopg2.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM rel_types WHERE rel_type = %s AND tail_types @> '{SCALAR}'::TEXT[]",
                        (rt,),
                    )
                    if cur.fetchone():
                        _SCALAR_REL_TYPES_CACHE.add(rt)  # Cache for next time
                        return True
        except Exception as e:
            log.debug("scalar_rel_type_query_failed", rel_type=rt, error=str(e))

    # Fallback: check hardcoded bootstrap set (pref_name, age, etc. always scalar)
    fallback_scalars = {
        'pref_name', 'also_known_as', 'age', 'height', 'weight', 'born_on',
        'occupation', 'nationality', 'has_gender'
    }
    return rt in fallback_scalars


def _get_rel_type_metadata(rel_type: str) -> dict:
    """Return validation + routing metadata for a rel_type.
    Queries DB directly to include novel rel_types approved by re_embedder.
    Falls back to cache, then hardcoded defaults.

    Returns dict with: storage_target, fact_class, is_symmetric,
    inverse_rel_type, is_leaf_only, is_hierarchy_rel.
    """
    rt = rel_type.lower().strip() if rel_type else ""
    if not rt:
        return {}

    # Check cache first (faster)
    if _REL_TYPE_CACHE and rt in _REL_TYPE_CACHE:
        return _REL_TYPE_CACHE[rt]

    # Query DB directly — inclualice novel rel_types approved by re_embedder
    dsn = os.environ.get("POSTGRES_DSN")
    if dsn:
        try:
            with psycopg2.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT storage_target, fact_class, is_symmetric, inverse_rel_type, "
                        "is_leaf_only, is_hierarchy_rel, correction_behavior FROM rel_types WHERE rel_type = %s",
                        (rt,)
                    )
                    row = cur.fetchone()
                    if row:
                        metadata = {
                            "storage_target": row[0],
                            "fact_class": row[1],
                            "is_symmetric": row[2] or False,
                            "inverse_rel_type": row[3],
                            "is_leaf_only": row[4] or False,
                            "is_hierarchy_rel": row[5] or False,
                            "correction_behavior": row[6] or "supersede",
                        }
                        # Cache it for next time
                        _REL_TYPE_CACHE[rt] = metadata
                        return metadata
        except Exception as e:
            log.warning("rel_type_metadata.db_query_failed", rel_type=rt, error=str(e))

    # Novel/unregistered rel_type or DB unavailable — safe defaults
    return {
        "storage_target": "facts",
        "fact_class": "C",
        "is_symmetric": False,
        "inverse_rel_type": None,
        "is_leaf_only": False,
        "is_hierarchy_rel": False,
        "correction_behavior": "supersede",
    }


def _infer_entity_type_from_rel_type(rel_type: str, position: str = 'object') -> str:
    """
    Infer entity_type from rel_type metadata constraints.

    Layer 2 type inference: if GLiNER2 returns 'unknown', try to infer
    entity type from rel_type constraints (head_types/tail_types).

    Args:
        rel_type: The relationship type (e.g., "works_for")
        position: 'head' (subject) or 'tail' (object) position

    Returns:
        entity_type string, or 'unknown' if no constraint found
    """
    try:
        # Query rel_types table for type constraints
        dsn = os.environ.get("POSTGRES_DSN")
        if not dsn:
            return 'unknown'

        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT head_types, tail_types FROM rel_types WHERE rel_type = %s",
                    (rel_type.lower().strip(),)
                )
                row = cur.fetchone()
                if not row:
                    return 'unknown'

                head_types = row[0] or []
                tail_types = row[1] or []

                if position == 'head' and head_types:
                    # If single type constraint (not 'ANY'), return it
                    if len(head_types) == 1 and head_types[0] != 'ANY':
                        return head_types[0]
                elif position == 'tail' and tail_types:
                    # If single type constraint (not 'ANY'), return it
                    if len(tail_types) == 1 and tail_types[0] != 'ANY':
                        return tail_types[0]

                return 'unknown'
    except Exception as err:
        log.warning("rel_type_inference_failed", rel_type=rel_type, error=str(err))
        return 'unknown'


def _detect_semantic_conflicts(
    db_conn,
    user_id: str,
    subject: str,
    rel_type: str,
    obj: str,
) -> tuple[str, str | None]:
    """
    Check if a new fact contradicts existing graph structure.

    Returns (decision, reason):
      - ("keep", None): No conflict — proceed with ingest.
      - ("supersede_new", reason): New fact semantically invalid — skip it.
      - ("supersede_existing_ids", reason): Existing fact(s) contradicted — supersede them.

    Principle: If X instance_of Y, Y is a TYPE, not a separate entity.
    Do not allow owns/has_pet/works_for on type entities.
    """
    rt_lower = rel_type.lower().strip() if rel_type else ""
    obj_id = str(obj).lower().strip() if obj else ""

    meta = _get_rel_type_metadata(rt_lower)

    # Only check leaf-only relationship types against hierarchy objects
    if not meta.get("is_leaf_only"):
        return ("keep", None)

    if not db_conn or not obj_id:
        return ("keep", None)

    try:
        _hierarchy_defining = ["instance_of","subclass_of","is_a","member_of","part_of"]
        with db_conn.cursor() as cur:
            # Check: is the object entity the object of any hierarchy relationship?
            cur.execute(
                "SELECT id, subject_id, rel_type FROM facts "
                "WHERE user_id = %s AND object_id = %s "
                "AND rel_type = ANY(%s) "
                "AND superseded_at IS NULL "
                "LIMIT 1",
                (user_id, obj_id, _hierarchy_defining),
            )
            hierarchy_fact = cur.fetchone()

            if not hierarchy_fact:
                # Also check staged_facts
                cur.execute(
                    "SELECT id, subject_id, rel_type FROM staged_facts "
                    "WHERE user_id = %s AND object_id = %s "
                    "AND rel_type = ANY(%s) "
                    "LIMIT 1",
                    (user_id, obj_id, _hierarchy_defining),
                )
                hierarchy_fact = cur.fetchone()

            if hierarchy_fact:
                fact_id, hierarchy_subject, hierarchy_rel = hierarchy_fact
                reason = (
                    f"type_conflict: {obj_id} is object of {hierarchy_rel} "
                    f"(defined by {hierarchy_subject}) — cannot also be {rt_lower} target"
                )
                log.info(
                    "ingest.semantic_conflict_detected",
                    obj=obj_id,
                    new_rel=rt_lower,
                    existing_hierarchy=f"{hierarchy_subject} {hierarchy_rel} {obj_id}",
                    decision="supersede_new",
                )
                return ("supersede_new", reason)

    except Exception as e:
        log.warning("ingest.semantic_conflict_check_failed", error=str(e),
                    subject=subject, rel_type=rt_lower, obj=obj_id)

    return ("keep", None)


# ── dprompt-62: Bidirectional Relationship Validation ─────────────────────────
# Prevents impossible bidirectional relationships like child_of + parent_of
# coexisting for the same entity pair. Inverse relationships should NOT both
# exist — the semantics make them contradictory.


def _validate_bidirectional_relationships(
    db_conn,
    user_id: str,
    subject: str,
    rel_type: str,
    obj: str,
    confidence: float,
) -> str:
    """
    Check if a new fact would create an impossible bidirectional relationship.

    If both child_of AND parent_of exist for the same subject-object pair,
    keep the higher-confidence version and supersede the lower.

    Returns:
      - "keep": no inverse rel_type — proceed normally
      - "create_inverse": no inverse fact found — auto-create needed
      - "supersede_new": existing inverse has higher confidence — skip new fact
    """
    rt_lower = rel_type.lower().strip() if rel_type else ""
    if not db_conn:
        return "keep"

    meta = _get_rel_type_metadata(rt_lower)
    inverse = meta.get("inverse_rel_type") if meta else None
    if not inverse:
        return "keep"

    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT id, confidence, 'facts' as source FROM facts "
                "WHERE user_id = %s AND subject_id = %s AND object_id = %s "
                "AND rel_type = %s AND superseded_at IS NULL "
                "LIMIT 1",
                (user_id, subject, obj, inverse),
            )
            inverse_fact = cur.fetchone()

            if not inverse_fact:
                cur.execute(
                    "SELECT id, confidence, 'staged' as source FROM staged_facts "
                    "WHERE user_id = %s AND subject_id = %s AND object_id = %s "
                    "AND rel_type = %s "
                    "LIMIT 1",
                    (user_id, subject, obj, inverse),
                )
                inverse_fact = cur.fetchone()

            if inverse_fact:
                inverse_id, inverse_conf, inverse_source = inverse_fact
                if confidence > inverse_conf:
                    if inverse_source == 'staged':
                        cur.execute(
                            "DELETE FROM staged_facts WHERE id = %s",
                            (inverse_id,),
                        )
                    else:
                        cur.execute(
                            "UPDATE facts SET superseded_at = now(), qdrant_synced = false "
                            "WHERE id = %s", (inverse_id,))
                    log.info(
                        "ingest.bidirectional_conflict_resolved",
                        kept=f"{subject} {rt_lower} {obj} (conf={confidence})",
                        superseded=f"{subject} {inverse} {obj} (conf={inverse_conf})",
                        reason="new_higher_confidence",
                    )
                    return "keep"  # allow new fact through
                else:
                    # Existing inverse has higher or equal confidence — skip new
                    log.info(
                        "ingest.bidirectional_conflict_resolved",
                        kept=f"{subject} {inverse} {obj} (conf={inverse_conf})",
                        superseded=f"{subject} {rt_lower} {obj} (conf={confidence})",
                        reason="existing_higher_confidence",
                    )
                    return "supersede_new"

    except Exception as e:
        log.warning("ingest.bidirectional_validation_failed", error=str(e),
                    subject=subject, rel_type=rt_lower, obj=obj)
        return "keep"  # DB error — skip auto-create for safety

    # Inverse rel_type exists but no inverse fact found — signal auto-creation
    log.info(
        "ingest.bidirectional_inverse_needed",
        fact=f"{subject} {rt_lower} {obj}",
        inverse_rel=inverse,
    )
    return "create_inverse"


# ── dprompt-41: Production Readiness ─────────────────────────────────────────

def _get_llm_url() -> str:
    """Get LLM endpoint URL with bulletproof priority chain.

    Priority 1: OPENWEBUI_URL (explicit external endpoint)
    Priority 2: QWEN_API_URL (fallback for direct Qwen/Ollama)
    Priority 3: Hardcoded fallback (development only)
    """
    # Priority 1: OPENWEBUI_URL (explicit, external)
    openwebui_url = os.environ.get("OPENWEBUI_URL")
    if openwebui_url:
        if not openwebui_url.startswith("http"):
            openwebui_url = f"https://{openwebui_url}"
        # OpenWebUI official endpoint: /api/chat/completions (per OpenWebUI docs)
        endpoint = f"{openwebui_url}/api/chat/completions"
        # Cache embedding endpoint separately (at /api/embeddings)
        global _EMBEDDING_API_URL
        _EMBEDDING_API_URL = f"{openwebui_url}/api/embeddings"
        log.info("llm_endpoint.openwebui_detected", url=endpoint)
        return endpoint

    # Priority 2: QWEN_API_URL (fallback for direct LLM backend)
    qwen_url = os.environ.get("QWEN_API_URL")
    if qwen_url:
        log.info("llm_endpoint.qwen_fallback", url=qwen_url)
        return qwen_url

    # Priority 3: Hardcoded fallback (development only) — use OpenWebUI default
    fallback = "http://localhost:3000/api/chat/completions"
    log.warning("llm_endpoint.hardcoded_fallback", url=fallback)
    return fallback


def _get_llm_url_fallbacks() -> list[str]:
    """Return list of LLM endpoint URLs to try in order (fallback chain).

    Supports multiple access paths to OpenWebUI:
    - Host address: ${BACKEND_IP}:3000
    - Reverse proxy: docker-host.helpalicekpro.ca
    - Docker internal IP: 172.16.9.2:3000
    - Container reference: open-webui:3000

    Tries each in priority order until one succeeds (for resilience).
    """
    urls = []

    # Primary: configured OPENWEBUI_URL
    openwebui_base = os.environ.get("OPENWEBUI_URL", "").strip()
    if openwebui_base:
        if not openwebui_base.startswith("http"):
            openwebui_base = f"https://{openwebui_base}"
        urls.append(f"{openwebui_base}/api/chat/completions")

    # Secondary: Try container name (open-webui:3000) for Docker networking
    # Extract port if provided in base URL
    if openwebui_base:
        # Parse port from URL if available
        import re
        port_match = re.search(r':(\d+)(?:/|$)', openwebui_base)
        port = port_match.group(1) if port_match else "3000"
        urls.append(f"http://open-webui:{port}/api/chat/completions")
    else:
        urls.append("http://open-webui:3000/api/chat/completions")

    # Tertiary: Try Docker internal IP (172.16.9.2:3000)
    if openwebui_base:
        port_match = re.search(r':(\d+)(?:/|$)', openwebui_base)
        port = port_match.group(1) if port_match else "3000"
        urls.append(f"http://172.16.9.2:{port}/api/chat/completions")
    else:
        urls.append("http://172.16.9.2:3000/api/chat/completions")

    # Quaternary: Try reverse proxy if configured
    reverse_proxy = os.environ.get("OPENWEBUI_REVERSE_PROXY", "").strip()
    if reverse_proxy:
        if not reverse_proxy.startswith("http"):
            reverse_proxy = f"https://{reverse_proxy}"
        urls.append(f"{reverse_proxy}/api/chat/completions")

    # Quinary: QWEN_API_URL direct fallback
    qwen_url = os.environ.get("QWEN_API_URL")
    if qwen_url:
        urls.append(qwen_url)

    # Finally: Hardcoded localhost fallback
    urls.append("http://localhost:11434/v1/chat/completions")

    # Remove duplicates while preserving order
    seen = set()
    unique_urls = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique_urls.append(url)

    return unique_urls

def _configured_llm_url() -> str:
    """Return the cached LLM URL (set at startup)."""
    global _LLM_URL
    if _LLM_URL is None:
        _LLM_URL = _get_llm_url()
    return _LLM_URL



def _resolve_llm_endpoint(with_fallback: bool = True) -> Union[str, list[str]]:
    """Centralized LLM endpoint resolver used by ALL LLM calls.

    Consolidates endpoint resolution from _get_llm_url() and _get_llm_url_fallbacks().
    Single source of truth for endpoint selection across codebase.

    Args:
        with_fallback: If True, return list of endpoints for retry loop.
                      If False, return single cached endpoint (default behavior).

    Returns:
        Single endpoint string if with_fallback=False
        List of endpoints if with_fallback=True (ordered by priority for retry)

    Priority Chain (Docker-aware):
        1. ${OPENWEBUI_INTERNAL_URL} (container-internal endpoint, port 8080)
        2. ${OPENWEBUI_URL} (user-configured external endpoint)
        3. ${QWEN_API_URL} (direct LLM backend)
        4. Hardcoded fallbacks (development/testing only)

    CRITICAL: When running inside Docker Compose, use port 8080 (internal service port),
    NOT port 3000 (external host mapping). Container DNS name open-webui:8080 reaches
    the actual OpenWebUI service port within the openweb_ui_default network.
    """
    global _LLM_URL

    # Cache primary endpoint for fast access
    if _LLM_URL is None:
        _LLM_URL = _get_llm_url()

    # If only single endpoint needed, return cached version
    if not with_fallback:
        return _LLM_URL

    # Build fallback chain: try multiple endpoints if primary fails
    # All endpoints must be container-reachable (no external IPs)
    urls = []

    # PRIMARY (highest priority): OPENWEBUI_INTERNAL_URL for container-internal calls
    # When running inside Docker, internal service port is 8080, not 3000 (host mapping)
    openwebui_internal = os.environ.get("OPENWEBUI_INTERNAL_URL", "").strip()
    if openwebui_internal:
        if not openwebui_internal.startswith("http"):
            openwebui_internal = f"http://{openwebui_internal}"
        urls.append(f"{openwebui_internal}/api/chat/completions")
    else:
        # Fallback: use port 8080 for container DNS name (safe default)
        urls.append("http://open-webui:8080/api/chat/completions")

    # SECONDARY: configured OPENWEBUI_URL (external endpoint)
    openwebui_base = os.environ.get("OPENWEBUI_URL", "").strip()
    if openwebui_base and openwebui_base != openwebui_internal:
        if not openwebui_base.startswith("http"):
            openwebui_base = f"https://{openwebui_base}"
        urls.append(f"{openwebui_base}/api/chat/completions")

    # TERTIARY: QWEN_API_URL if configured
    qwen_url = os.environ.get("QWEN_API_URL")
    if qwen_url:
        urls.append(qwen_url)

    # Remove duplicates while preserving order
    seen = set()
    unique_urls = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique_urls.append(url)

    log.info("llm_endpoint.fallback_chain_built",
            urls_count=len(unique_urls),
            primary=unique_urls[0] if unique_urls else None,
            all_urls=unique_urls)
    return unique_urls


def _validate_startup_config() -> dict:
    """Validate required environment variables at startup. Raises RuntimeError on failure."""
    llm_url = _get_llm_url()

    required = {
        "POSTGRES_DSN": os.environ.get("POSTGRES_DSN"),
        "QDRANT_URL": os.environ.get("QDRANT_URL", "http://qdrant:6333"),
        "LLM_URL": llm_url,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        log.warning("startup.missing_env_vars", missing=missing,
                    detail="Set these variables for full functionality. App will start but may be degraded.")

    config = {
        "postgres_dsn": "***",  # sanitized
        "qdrant_url": required["QDRANT_URL"],
        "llm_url": required["LLM_URL"],
        "httpx_timeout": int(os.environ.get("HTTPX_TIMEOUT", "10")),
        "db_timeout": int(os.environ.get("DB_TIMEOUT", "30")),
        "qdrant_timeout": int(os.environ.get("QDRANT_TIMEOUT", "10")),
        "db_pool_size": int(os.environ.get("DB_POOL_SIZE", "10")),
        "rate_limit_per_min": int(os.environ.get("RATE_LIMIT_PER_MIN", "100")),
    }
    log.info("startup.config_validated", **{k: v for k, v in config.items() if k != "postgres_dsn"})
    return config


# In-memory health cache
_health_cache: dict = {}
_health_cache_ts: float = 0.0
_embedder_stats: dict = {
    "last_run": None, "facts_synced": 0, "facts_promoted": 0,
    "facts_expired": 0, "error_count": 0, "last_error": None,
}
# LLM endpoint URL (set at startup via _get_llm_url())
_LLM_URL: str = None


def _check_db_health(dsn: str) -> bool:
    try:
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


def _check_qdrant_health(url: str) -> bool:
    try:
        resp = httpx.get(f"{url}/collections", timeout=2.0)
        return resp.status_code == 200
    except Exception:
        return False


def _check_llm_health(url: str) -> bool:
    try:
        resp = httpx.get(url.replace("/chat/completions", "/models"), timeout=2.0)
        return resp.status_code in (200, 404)  # 404 = endpoint exists, just GET not supported
    except Exception:
        return False


# Rate limiter: per-user_id tracking
_RATE_TRACKER: dict[str, list[float]] = {}
_RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_MIN", "100"))


def _check_rate_limit(user_id: str) -> bool:
    now = __import__("time").time()
    window = now - 60
    _RATE_TRACKER.setdefault(user_id, [])
    _RATE_TRACKER[user_id] = [t for t in _RATE_TRACKER[user_id] if t > window]
    if len(_RATE_TRACKER[user_id]) >= _RATE_LIMIT:
        return False
    _RATE_TRACKER[user_id].append(now)
    return True


# Timeout helpers
_HTTPX_TIMEOUT = int(os.environ.get("HTTPX_TIMEOUT", "10"))
_DB_TIMEOUT = int(os.environ.get("DB_TIMEOUT", "30"))
_QDRANT_TIMEOUT = int(os.environ.get("QDRANT_TIMEOUT", "10"))

# ── End dprompt-41 ──────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _gliner2_model, _rel_type_registry, _rel_type_constraint, _REL_TYPE_META, _LLM_URL, _EMBEDDING_API_URL, _http_client, _http_client_sync, _idempotency_mgr, _redis_client

    # dprompt-41: validate startup config (fail fast)
    _validate_startup_config()
    _LLM_URL = _get_llm_url()

    # Initialize persistent HTTP clients for pooled connections
    _http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0), limits=httpx.Limits(max_connections=100, max_keepalive_connections=20))
    _http_client_sync = httpx.Client(timeout=httpx.Timeout(30.0), limits=httpx.Limits(max_connections=100, max_keepalive_connections=20))
    log.info("startup.http_clients_initialized", async_client=True, sync_client=True)

    # dprompt-120: Initialize idempotency manager for /ingest deduplication
    # Prevents duplicate extraction LLM calls from OpenWebUI's multiple inlet invocations
    _idempotency_mgr = IdempotencyManager()
    log.info("startup.idempotency_manager_initialized", redis_url=_idempotency_mgr.redis_url[:30] if _idempotency_mgr.redis_url else "none")

    # dprompt-144: Initialize Redis client for intent classification queue
    _redis_client = _init_redis_client()
    if _redis_client:
        log.info("startup.redis_initialized", purpose="intent_classification_queue")

    qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")
    default_collection = os.environ.get("QDRANT_COLLECTION", "faultline-test")
    log.info("startup.qdrant_collection_check", collection=default_collection)
    if ensure_collection(default_collection, qdrant_url):
        log.info("startup.qdrant_collection_ready", collection=default_collection)
    else:
        log.error("startup.qdrant_collection_failed", collection=default_collection)

    dsn = os.environ.get("POSTGRES_DSN")
    if dsn:
        _ensure_schema(dsn)  # apply pending migrations before reading the schema
        _refresh_rel_type_cache()  # dprompt-73b: load unified metadata cache at startup
        _refresh_scalar_rel_types_cache()  # metadata-driven scalar rel_types (replaces hardcoded set)
        _cleanup_entity_aliases_startup(dsn)  # remove corrupted string entity_ids from entity_aliases
        _normalize_entity_ids_startup(dsn)  # normalize string entity_ids to UUIDs
        _rel_type_registry = RelTypeRegistry(dsn)
        try:
            _rel_type_registry.get_valid_types()
            _REL_TYPE_META = _build_rel_type_meta(dsn)
            log.info("startup.rel_type_registry_ready",
                     count=len(_rel_type_registry._cache),
                     meta_len=len(_REL_TYPE_META))
        except Exception as e:
            log.error("startup.rel_type_registry_failed", error=str(e))

        # dprompt-125: Load rel_type aliases for LLM variation normalization
        global _REL_TYPE_ALIASES
        try:
            _REL_TYPE_ALIASES = _load_rel_type_aliases()
            log.info("startup.rel_type_aliases_loaded", alias_count=len(_REL_TYPE_ALIASES))
        except Exception as e:
            log.warning("startup.rel_type_aliases_failed", error=str(e))

    log.info("startup.gliner2_loading")
    global _gliner2_model
    try:
        from gliner2 import GLiNER2
        _gliner2_model = GLiNER2.from_pretrained("fastino/gliner2-base-v1")
        log.info("startup.gliner2_ready")
    except Exception as e:
        log.error("startup.gliner2_failed", error=str(e))

    # dprompt-91: Load taxonomy cache for archive filtering
    log.info("startup.taxonomy_cache_loading")
    _db_for_cache = None
    try:
        _db_for_cache = psycopg2.connect(os.environ.get("POSTGRES_DSN"))
        _load_taxonomy_cache(_db_for_cache)
        _db_for_cache.commit()
    except Exception as e:
        if _db_for_cache:
            try:
                _db_for_cache.rollback()
            except Exception:
                pass
        log.error("startup.taxonomy_cache_init_failed", error=str(e))
    finally:
        if _db_for_cache:
            try:
                _db_for_cache.close()
            except Exception:
                pass

    yield
    # Cleanup on shutdown
    if _http_client:
        await _http_client.aclose()
    if _http_client_sync:
        _http_client_sync.close()
    if _redis_client:
        try:
            _redis_client.close()
        except Exception:
            pass
    _gliner2_model = None
    _rel_type_registry = None
    _http_client = None
    _http_client_sync = None
    _idempotency_mgr = None
    _redis_client = None

# ═══════════════════════════════════════════════════════════════════════
# dprompt-91: Archive Filtering — Module-Level Functions & Caching
# ═══════════════════════════════════════════════════════════════════════

# Historical keyword set for temporal query detection
_HISTORICAL_KEYWORDS = {
    "used to", "did i", "where was", "where was i", "my old",
    "previously", "before", "when did", "in the past", "back then",
    "used to live", "used to work", "my previous", "former", "earlier",
}

# Taxonomy cache: loaded at startup, reused across requests
_TAXONOMY_CACHE = {}

def _parse_postgres_array(arr) -> list:
    """Parse PostgreSQL ARRAY value. Handles both Python lists and string representations.
    PostgreSQL arrays come as '{item1,item2}' strings when no adapter is registered.
    Also handles psycopg2 list/array objects.
    """
    # Already a native Python list
    if isinstance(arr, list):
        return arr

    # None or empty
    if arr is None:
        return []

    # String representation '{item1,item2,item3}'
    if isinstance(arr, str):
        arr = arr.strip()
        if arr.startswith('{') and arr.endswith('}'):
            arr = arr[1:-1]  # Remove braces
        if not arr:
            return []
        return [item.strip() for item in arr.split(',')]

    # Fallback: try to convert to string and parse
    try:
        arr_str = str(arr).strip()
        if arr_str.startswith('{') and arr_str.endswith('}'):
            arr_str = arr_str[1:-1]
            if not arr_str:
                return []
            return [item.strip() for item in arr_str.split(',')]
    except Exception:
        pass

    # Last resort: empty list
    return []

def _load_taxonomy_cache(db) -> None:
    """Load entity_taxonomies into module-level cache at startup.
    Non-fatal if fails; system continues with empty cache (hard fail on usage).
    """
    global _TAXONOMY_CACHE
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT taxonomy_name, member_entity_types, rel_types_defining_group, "
                "description, is_hierarchical, parent_rel_type "
                "FROM entity_taxonomies"
            )
            rows = cur.fetchall()
            for row in rows:
                try:
                    taxonomy_name = row[0]
                    try:
                        member_types = _parse_postgres_array(row[1])
                    except Exception as e:
                        log.warning("startup.taxonomy_parse_member_types_failed",
                                   taxonomy=taxonomy_name, input=row[1], error=str(e))
                        member_types = []

                    try:
                        rel_types = _parse_postgres_array(row[2])
                    except Exception as e:
                        log.warning("startup.taxonomy_parse_rel_types_failed",
                                   taxonomy=taxonomy_name, input=row[2], error=str(e))
                        rel_types = []

                    description = row[3]
                    is_hier = row[4]
                    parent_rel = row[5]

                    _TAXONOMY_CACHE[taxonomy_name] = {
                        "member_entity_types": member_types,
                        "rel_types_defining_group": rel_types,
                        "description": description,
                        "is_hierarchical": is_hier,
                        "parent_rel_type": parent_rel,
                    }
                except Exception as e:
                    import traceback
                    log.warning("startup.taxonomy_row_parse_failed",
                               taxonomy=row[0] if row else "unknown",
                               error=str(e),
                               traceback=traceback.format_exc())
                    continue

            log.info("startup.taxonomy_cache_loaded", count=len(_TAXONOMY_CACHE))
    except Exception as e:
        log.error("startup.taxonomy_cache_failed", error=str(e))

def detect_historical(query_lower: str) -> tuple[bool, float]:
    """Detect if query asks about archived (historical) facts.
    Returns (is_historical, confidence) where confidence = count of keyword matches.
    """
    confidence = 0.0
    for keyword in _HISTORICAL_KEYWORDS:
        if keyword in query_lower:
            confidence += 1.0

    is_historical = confidence > 0
    log.info("query.temporality_detection", is_historical=is_historical,
             confidence=confidence, query_lower=query_lower[:80])
    return (is_historical, confidence)

def determine_scope_multi_factor(db, user_id: str, facts: list[dict], query_lower: str = "") -> set[str]:
    """
    Determine scope (which taxonomies apply) using multi-factor analysis.

    Factors:
    1. rel_types in facts → match to entity_taxonomies.rel_types_defining_group
    2. entity_types of subjects/objects → validate against member_entity_types

    Returns: set of detected taxonomy_names (empty set if no matches)
    Queries database directly, no cache dependency — graceful fallback on error
    """

    detected_taxonomies = set()
    rel_types_in_facts = {f.get("rel_type") for f in facts if f.get("rel_type")}

    # Factor 1: Query-driven taxonomy detection.
    # Match query keywords against taxonomy descriptions and member entity types.
    # "weather" → location, "family" → family, "work" → work, etc.
    # When query has no domain keywords, detected_taxonomies stays empty = return ALL facts.
    if query_lower:
        try:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT taxonomy_name, description, member_entity_types "
                    "FROM entity_taxonomies"
                )
                for row in cur.fetchall():
                    tax_name = row[0]
                    description = (row[1] or "").lower()
                    member_types = [t.lower() for t in (row[2] or [])]
                    query_words = set(query_lower.split())
                    desc_words = set(description.split())
                    if query_words & desc_words:
                        detected_taxonomies.add(tax_name)
                        continue
                    for mtype in member_types:
                        if mtype in query_lower:
                            detected_taxonomies.add(tax_name)
                            break
        except Exception as e:
            log.warning("query.scope_query_driven_failed", error=str(e))

    # Factor 2: Fact-driven fallback — only when query-driven returned nothing.
    if not detected_taxonomies:
        for rel_type in rel_types_in_facts:
            if not rel_type:
                continue
            try:
                with db.cursor() as cur:
                    cur.execute(
                        "SELECT DISTINCT taxonomy_name FROM entity_taxonomies "
                        "WHERE %s = ANY(rel_types_defining_group)",
                        (rel_type,)
                    )
                    for row in cur.fetchall():
                        detected_taxonomies.add(row[0])
            except Exception as e:
                log.warning("query.scope_rel_type_match_failed", rel_type=rel_type, error=str(e))

    log.info("determine_scope.multi_factor",
             rel_types=list(rel_types_in_facts),
             detected_taxonomies=list(detected_taxonomies),
             user_id=user_id)

    return detected_taxonomies

def format_fact_for_injection(fact: dict, db, registry) -> str | None:
    """
    Convert raw fact to plain English prose for LLM injection.
    HARD CONSTRAINT: Facts for LLM must be human-readable, not machine-readable tuples.

    Example inputs:
    - {"subject_id": "uuid", "rel_type": "works_for", "object_id": "uuid"}
    - {"subject_id": "uuid", "rel_type": "age", "object_value": 45}

    Example outputs:
    - "${USER} works for Acme Inc."
    - "alice is 16 years old"
    """
    try:
        subject_id = fact.get("subject_id")
        rel_type = fact.get("rel_type")
        object_id = fact.get("object_id")
        object_value = fact.get("object_value")

        # Resolve subject display name
        subject_name = None
        if registry and subject_id:
            subject_name = registry.get_preferred_name("", subject_id)
        subject_name = subject_name or (subject_id[:8] if subject_id else "Unknown")

        # Get rel_type label from database
        rel_label = rel_type
        if db and rel_type:
            try:
                with db.cursor() as cur:
                    cur.execute(
                        "SELECT label FROM rel_types WHERE rel_type = %s",
                        (rel_type,)
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        rel_label = row[0]
            except Exception:
                pass  # Fall back to rel_type name

        # Resolve object (UUID or scalar value)
        if object_value is not None:
            # Scalar fact
            object_repr = str(object_value)
        elif object_id:
            # Relationship fact
            object_name = None
            if registry:
                object_name = registry.get_preferred_name("", object_id)
            object_repr = object_name or object_id[:8]
        else:
            return None

        # Format as natural English
        if subject_name and rel_label and object_repr:
            return f"{subject_name} {rel_label} {object_repr}."

        return None
    except Exception as e:
        log.warning("format_fact_for_injection_failed", fact_id=fact.get("id"), error=str(e))
        return None

def apply_archive_filter(db, query_lower: str, user_id: str,
                        facts: list[dict]) -> tuple[list[dict], dict]:
    """
    Apply archive filtering to facts using multi-factor scope + temporality.

    Returns (filtered_facts, metadata) where metadata contains:
    {
        "detected_taxonomies": [str],
        "is_historical": bool,
        "fact_count_before": int,
        "fact_count_after": int,
    }

    HARD FAIL if scope determination or archive filter fails.
    """
    try:
        # Phase 1: Detect temporality
        is_historical, temporal_confidence = detect_historical(query_lower)

        # Phase 2: Determine scope (multi-factor)
        detected_taxonomies = determine_scope_multi_factor(db, user_id, facts, query_lower)

        # Phase 3: Filter facts by scope + temporality
        filtered = []

        # Identity & scalar rel_types always pass scope filter (they're not taxonomy-scoped)
        _IDENTITY_SCALAR_RELS = {
            'pref_name', 'also_known_as', 'age', 'height', 'weight', 'born_on',
            'occupation', 'nationality', 'has_gender', 'born_in', 'instance_of',
            'subclass_of'
        }

        for fact in facts:
            rel_type = fact.get("rel_type")

            if detected_taxonomies and rel_type not in _IDENTITY_SCALAR_RELS:
                rel_in_taxonomy = False
                for taxonomy_name in detected_taxonomies:
                    if taxonomy_name in _TAXONOMY_CACHE:
                        rel_types_for_tax = _TAXONOMY_CACHE[taxonomy_name].get("rel_types_defining_group", [])
                        if rel_type in rel_types_for_tax:
                            rel_in_taxonomy = True
                            break
                if not rel_in_taxonomy:
                    continue

            archived_at = fact.get("archived_at")
            valid_until = fact.get("valid_until")

            if is_historical:
                if archived_at is None and valid_until is None:
                    continue
            else:
                if archived_at is not None or valid_until is not None:
                    continue

            filtered.append(fact)

        metadata = {
            "detected_taxonomies": list(detected_taxonomies),
            "is_historical": is_historical,
            "fact_count_before": len(facts),
            "fact_count_after": len(filtered),
        }

        log.info("archive_filter.temporal_scope",
                 scope="historical" if is_historical else "current",
                 count_before=len(facts),
                 count_after=len(filtered))

        if detected_taxonomies:
            log.info("archive_filter.scope_gate",
                     scope_type="targeted",
                     taxonomies=list(detected_taxonomies),
                     count_before=len(facts),
                     count_after=len(filtered))
        else:
            log.info("archive_filter.scope_gate",
                     scope_type="open",
                     taxonomies=[])

        return (filtered, metadata)

    except Exception as e:
        log.error("archive_filter.failed", error=str(e), user_id=user_id)
        raise


# ── dBug-027 & dBug-026: Metadata-driven validation and entity filtering ──

def _validate_rel_type_constraints(fact_dict: dict, rel_type_meta: dict, db) -> tuple[bool, str]:
    """
    Validate fact against rel_type metadata constraints (dprompt-97).

    Uses rel_types metadata to enforce:
    - head_types: subject entity_type must be in this list
    - tail_types: object entity_type must be in this list (SCALAR or entity type)
    - is_leaf_only: object cannot have children in hierarchy

    Returns (is_valid, reason) tuple.

    Treats "unknown" type as "type not yet determined" and skips validation,
    allowing downstream type enrichment to populate correct types.
    """
    if not rel_type_meta:
        return True, "no_metadata"

    subject_type = (fact_dict.get("subject_type") or "").upper().strip()
    object_type = (fact_dict.get("object_type") or "").upper().strip()
    tail_types = rel_type_meta.get("tail_types", [])
    head_types = rel_type_meta.get("head_types", [])
    rel_type = fact_dict.get("rel_type", "").lower()

    # Treat "unknown" as "type not yet determined" — skip validation and let ingest populate
    if subject_type == "UNKNOWN":
        subject_type = ""
    if object_type == "UNKNOWN":
        object_type = ""

    # Constraint 1: head_types — subject entity_type must be allowed
    if head_types and "ANY" not in head_types:
        if subject_type and subject_type not in [t.upper() for t in head_types]:
            return False, f"subject_type '{subject_type}' not in {head_types} for rel_type '{rel_type}'"

    # Constraint 2: tail_types — object entity_type must be allowed (CRITICAL FIX FOR BIDIRECTIONAL VALIDATION)
    if tail_types and "ANY" not in tail_types:
        if set(tail_types) != {"SCALAR"}:
            if object_type and object_type not in [t.upper() for t in tail_types]:
                return False, f"object_type '{object_type}' not in {tail_types} for rel_type '{rel_type}'"

    # Constraint 3: is_leaf_only — object cannot have children in hierarchy
    if rel_type_meta.get("is_leaf_only"):
        obj_id = fact_dict.get("object_id")
        if obj_id:
            try:
                with db.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM facts WHERE subject_id = %s AND is_hierarchy_rel = true",
                        (obj_id,)
                    )
                    if cur.fetchone()[0] > 0:
                        return False, f"object {obj_id} has hierarchy children, cannot be leaf-only"
            except Exception as e:
                log.warning("validate_rel_type.leaf_check_failed", error=str(e))

    return True, "ok"

def _filter_extracted_entities(
    entities: list,
    hierarchy_entity_types: list = None
) -> list:
    """
    Filter GLiNER2 extracted entities to only valid named entity types (dprompt-97).

    dprompt-129: If hierarchy_entity_types provided, filter to only those types.
    Otherwise, use default VALID_ENTITY_TYPES (bac${LOCATION}ard compat).

    Rejects:
    - Stop words (the, a, and, prefers, called, someone, married, spouse, etc.)
    - Rel_types used as entity names (spouse, married_person, other_person)
    - Low-confidence extractions (< 0.6)
    - Single-letter noise

    Keeps:
    - Person, Organization, Location, Object, Event, Animal types (or hierarchy-specified types)
    """
    if not entities:
        return []

    # dprompt-129: Use hierarchy_entity_types if provided, else default
    if hierarchy_entity_types:
        VALID_ENTITY_TYPES = set(hierarchy_entity_types)
    else:
        VALID_ENTITY_TYPES = {"Person", "Organization", "Location", "Object", "Event", "Animal"}

    REJECT_TYPES = {"Concept", "unknown", "Unknown"}

    # English stop words + rel_types + attribute alicecriptors (comprehensive, no recursive matching)
    STOP_WORDS = {
        # Grammar
        "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "must", "can",
        # Pronouns
        "i", "me", "my", "we", "our", "he", "she", "it", "they", "them", "his", "her", "hers", "its", "their",
        # Common words
        "what", "which", "who", "when", "where", "why", "how", "and", "or", "but",
        "not", "no", "yes", "if", "as", "of", "to", "for", "from", "in", "on", "at", "by", "with",
        "about", "into", "through", "during", "before", "after", "above", "below", "up", "down", "out", "off", "over", "under",
        "again", "further", "then", "once", "just", "only", "very", "too", "so", "such", "same", "now", "here", "there",
        "this", "that", "these", "those", "more", "most", "all", "both", "each", "every", "few", "some",
        # Rel_types that should NOT be entities
        "spouse", "parent", "child", "sibling", "friend", "couple", "married", "married_person",
        "born", "died", "lived", "worked", "studied", "taught", "owned",
        # Attribute alicecriptors
        "tall", "short", "old", "young", "big", "small", "engineer", "doctor", "teacher", "worker",
        "person", "people", "member", "group", "family", "household", "company", "organization", "institution",
        # Preference markers
        "prefers", "prefer", "preferred", "preference", "likes", "like", "dislikes", "dislike", "loves", "love", "hates", "hate",
        # Generic markers
        "someone", "something", "anything", "everything", "nothing", "yet", "still", "already",
        "either", "neither", "own", "other", "another", "next", "last", "first", "second", "previous", "recent", "following",
        # Additional stop words
        "list", "instantly", "updated", "should", "called", "wifes", "she",
    }

    filtered = []
    for entity in entities:
        entity_type = entity.get("type", "unknown")
        entity_name = entity.get("text", "").lower().strip()
        confidence = entity.get("confidence", 0.0)

        # Reject: invalid/reject type
        if entity_type in REJECT_TYPES:
            continue

        # Reject: not a valid entity type
        if entity_type not in VALID_ENTITY_TYPES:
            continue

        # Reject: stop word or rel_type used as entity
        if entity_name in STOP_WORDS:
            continue

        # Reject: low confidence
        if confidence < 0.6:
            continue

        # Reject: single letter (noise)
        if len(entity_name) < 2:
            continue

        # Accept: valid named entity
        filtered.append(entity)

    return filtered


# ──────────────────────────────────────────────────────────────────────────────
# dprompt-144: Intent Classification Infrastructure
# ──────────────────────────────────────────────────────────────────────────────

def _init_redis_client() -> Optional[redis.Redis]:
    """
    Initialize Redis client for event queue.
    Follows IdempotencyManager pattern: uses env REDIS_URL or defaults to localhost.
    Returns None if connection fails (non-blocking degradation).
    """
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    try:
        client = redis.from_url(redis_url, decode_responses=True)
        client.ping()
        log.info("redis_client.connected", url=redis_url[:30])
        return client
    except Exception as e:
        log.warning("redis_client.connection_failed", error=str(e))
        return None


def rate_limit(calls_per_minute: int):
    """
    Rate limit decorator: validates user_id, tracks per-minute calls.
    Returns 429 error if limit exceeded.

    SECURITY: user_id required and validated (len >= 4, isinstance str).
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract user_id from kwargs (passed by FastAPI dependency/middleware)
            user_id = kwargs.get("user_id", "")

            # Validate user_id
            if not user_id or not isinstance(user_id, str) or len(user_id) < 4:
                log.warning("rate_limit.invalid_user_id", user_id_len=len(user_id or ""))
                # Don't block — return error from endpoint
                kwargs["user_id"] = user_id
                return await func(*args, **kwargs)

            now = time_now()
            window_start = now - 60  # 60-second window

            # Clean old timestamps
            _RATE_LIMIT_BUCKETS[user_id] = [
                ts for ts in _RATE_LIMIT_BUCKETS[user_id] if ts > window_start
            ]

            # Check limit
            if len(_RATE_LIMIT_BUCKETS[user_id]) >= calls_per_minute:
                log.warning("rate_limit.exceeded", user_id=user_id,
                           count=len(_RATE_LIMIT_BUCKETS[user_id]))
                return {"error": "rate_limit_exceeded", "retry_after_seconds": 60}

            # Record this call
            _RATE_LIMIT_BUCKETS[user_id].append(now)
            return await func(*args, **kwargs)
        return wrapper
    return decorator


async def _enqueue_reembedder_event(
    event_type: str,
    user_id: str,
    data: dict,
    priority: str = "normal"
) -> bool:
    """
    Enqueue event for async re-embedder processing via Redis.

    SECURITY HARDENING:
    - No raw text stored (only metadata: rel_type, confidence, hashes)
    - TTL 60s per entry (auto-expire stale events)
    - user_id validated before enqueueing
    - Non-blocking — fire-and-forget with exception handling

    Args:
        event_type: "class_c_ingest", "negation_pattern_novel", "correction_feedback"
        user_id: User UUID (validated before any operation)
        data: Event metadata (scrubbed of sensitive fields)
        priority: "high" or "normal"

    Returns:
        True if enqueued successfully, False otherwise
    """
    # Validate user_id before enqueueing
    if not user_id or len(user_id) < 4 or not isinstance(user_id, str):
        log.warning("enqueue_event.invalid_user_id", event_type=event_type)
        return False

    if not _redis_client:
        log.debug("enqueue_event.redis_unavailable", event_type=event_type)
        return False

    try:
        # Scrub sensitive data from event
        # Only include: event_type, user_id, rel_type, confidence, hashes of patterns
        safe_data = {}

        if event_type == "class_c_ingest":
            # For class_c: only store rel_type + confidence, never text
            rel_type = data.get("rel_type", "").strip().lower()
            if rel_type:
                safe_data["rel_type"] = rel_type
                safe_data["confidence"] = float(data.get("confidence", 0.4))

        elif event_type == "negation_pattern_novel":
            # For patterns: hash the pattern_text, never store plaintext
            pattern_text = data.get("pattern_text", "").strip().lower()
            if pattern_text:
                pattern_hash = hashlib.sha256(pattern_text.encode()).hexdigest()[:16]
                safe_data["pattern_hash"] = pattern_hash
                safe_data["confidence"] = float(data.get("confidence", 0.4))

        elif event_type == "correction_feedback":
            # For corrections: only confidence_bin, no text
            confidence_bin = data.get("confidence_bin", "").strip()
            if confidence_bin:
                safe_data["confidence_bin"] = confidence_bin

        # Build event with safe data only
        from datetime import datetime
        event = {
            "event_type": event_type,
            "user_id": user_id,
            "priority": priority,
            "timestamp": datetime.utcnow().isoformat(),
            **safe_data
        }

        # Determine queue key
        queue_key = (
            "faultline:queue:class_c" if event_type == "class_c_ingest"
            else f"faultline:queue:{user_id}"
        )

        # Use pipeline for atomic operation (follow IdempotencyManager pattern)
        try:
            pipe = _redis_client.pipeline()
            pipe.rpush(queue_key, json.dumps(event))
            pipe.expire(queue_key, 3600)  # Queue itself expires after 1hr idle
            pipe.execute()

            log.debug("reembedder_event_enqueued",
                     event_type=event_type,
                     user_id=user_id[:12],
                     priority=priority)
            return True
        except redis.RedisError as e:
            log.warning("redis_enqueue_failed", error=str(e))
            # Non-blocking — re-embedder poll cycle catches it as safety net
            return False

    except Exception as e:
        log.warning("enqueue_event_error", event_type=event_type, error=str(e))
        return False


app = FastAPI(title="FaultLine WGM", lifespan=lifespan)

# ── Surgical Fact Correction Helper ──────────────────────────────────────────
def _unified_correction_extraction_llm(
    text: str,
    user_id: str,
    context_facts: list[dict],
    db: psycopg2.extensions.connection,
) -> dict:
    """
    LLM-powered fact correction extraction (metadata-driven prompt).
    SYNC version — uses _http_client_sync to avoid asyncio event loop conflicts.

    Uses rel_types.natural_language from database to improve LLM discernment.
    Returns: {subject_uuid, subject_name, old_rel_type, old_value, new_rel_type, new_value, dimension, confidence, reason}

    Dimension field (SCALAR|RELATIONAL|HIERARCHICAL|SUBJECT|REL_TYPE|ENTITY_TYPE) determines
    which SQL table/columns are updated in /retract/correct execution.
    """
    system_prompt = """
You are a SURGICAL FACT CORRECTOR for a personal memory system.

User is correcting a mistake in their knowledge graph.
Your job: Extract EXACTLY what changed, with NO assumptions, NO cascading changes.

IMPORTANT CONSTRAINTS:
1. ONLY extract corrections explicitly stated or clearly inferable
2. If you cannot extract subject, rel_type, old_value, new_value → REJECT (return empty)
3. Confidence 0.9+ for direct statements, 0.7-0.89 for inferred, <0.70 → REJECT
4. Check immutable facts: born_on, born_in, nationality → REJECT if attempted
5. NO cascading: if user says "I'm 23", change ONLY age attribute, nothing else

RECENT FACTS (context for entity resolution):
{formatted_facts}

RELATIONSHIP TYPES (metadata to understand what can change):
{dynamic_rel_types}

USER MESSAGE: "{text}"

TASK:
Extract the correction using this JSON structure. If ANY field is uncertain, return empty object {{}}.

{{
  "subject_uuid": "uuid or null",
  "subject_name": "entity name or null",
  "old_rel_type": "rel_type (lowercased)",
  "old_value": "exact old value from facts",
  "new_rel_type": "rel_type (lowercased, may differ from old)",
  "new_value": "exact new value from message",
  "dimension": "SCALAR | RELATIONAL | HIERARCHICAL | SUBJECT | REL_TYPE | ENTITY_TYPE",
  "confidence": 0.0 to 1.0,
  "reason": "Why you extracted this (explicit/inferred/reason for low confidence)"
}}

RULES:

1. SCALAR dimension (age, height, name, occupation):
   - old_value and new_value are STRINGS
   - Update entity_attributes table
   - Example: age 18 → 23

2. RELATIONAL dimension (spouse, parent_of, has_pet, works_for):
   - old_value and new_value are ENTITY NAMES (resolve to UUID)
   - Update facts.object_id
   - Example: spouse ${{ENTITY}} → Sarah

3. HIERARCHICAL dimension (instance_of, member_of, part_of):
   - old_value and new_value are TYPE/CLASS NAMES (resolve to UUID)
   - Update facts.object_id with hierarchy semantics
   - Example: instance_of dog → cat

4. SUBJECT dimension (wrong entity):
   - subject_uuid MUST CHANGE
   - old_value and new_value are NULL or entity identifiers
   - Rare: user realizes "that was ${{CHILD1}}, not ${{ENTITY}}"
   - Example: parent_of ${{ENTITY}} → parent_of ${{CHILD1}}

5. REL_TYPE dimension (relationship semantic change):
   - rel_type itself changes (not just the object)
   - old_rel_type ≠ new_rel_type
   - Example: works_for → volunteers_at

6. ENTITY_TYPE dimension (classification of entity):
   - Changing what type of thing an entity IS
   - Example: instance_of Person → instance_of Computer

CONFIDENCE SCORING:

- **0.98–1.0**: Explicit old→new statement ("I'm not 18, I'm 23")
- **0.90–0.97**: Clear but slightly implicit ("I was born in the 80s, not 90s")
- **0.80–0.89**: Requires context inference ("I went to Guelph" + past fact shows ${{LOCATION}})
- **0.70–0.79**: Ambiguous but likely correct ("Actually, my wife is from Canada")
- **< 0.70**: REJECT (return {{}}) — too vague or contradictory

IMMUTABLE FACTS (auto-reject):
- born_on, born_in, nationality → confidence = 0.0, reason = "immutable fact"

RESPOND WITH VALID JSON ONLY (no markdown, no explanation).
If extraction is impossible or ambiguous, return {{}}."""

    # Load rel_types with natural_language from database
    try:
        with db.cursor() as cur:
            cur.execute("""
                SELECT rel_type, natural_language, category
                FROM rel_types
                WHERE natural_language IS NOT NULL
                ORDER BY category, rel_type
                LIMIT 20
            """)
            rel_types_list = cur.fetchall()
            rel_types_str = "\n".join(
                f"- {row[0]}: {row[1]} (category: {row[2]})"
                for row in rel_types_list
            )
    except Exception as e:
        log.warning("correction_extraction.rel_types_load_failed", error=str(e))
        rel_types_str = "(database query failed, using generic examples)"

    # Format facts for context (dprompt-143: semantic intent extraction)
    # Context is minimal — LLM focuses on intent, not entity resolution
    facts_str = "\n".join(
        f"- {f.get('subject', 'unknown')}: {f.get('rel_type', 'unknown')} → {f.get('object', 'unknown')}"
        for f in (context_facts or [])[:10]
    ) if context_facts else "(no recent facts for reference)"

    system_prompt = system_prompt.format(
        dynamic_rel_types=rel_types_str,
        text=text,
        formatted_facts=facts_str
    )

    # Call LLM (SYNC)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text}
    ]

    try:
        # Use centralized LLM call with automatic retry/fallback
        extraction = call_llm_with_retry_sync(
            messages=messages,
            model=os.getenv("WGM_LLM_MODEL", "qwen/qwen3.5-9b"),
            user_id=user_id,
            operation="correction_extraction",
        )

        if extraction:
            log.info("correction_extraction.llm_success",
                    user_id=user_id,
                    subject=extraction.get("subject_uuid"),
                    old_rel_type=extraction.get("old_rel_type"),
                    confidence=extraction.get("confidence"))
            return extraction
        else:
            log.warning("correction_extraction.llm_no_json", user_id=user_id)
            return {}
    except Exception as e:
        import traceback
        log.error("correction_extraction.llm_failed", error=str(e), user_id=user_id, traceback=traceback.format_exc())
        return {}


def _retraction_intent_extraction_llm(
    text: str,
    user_id: str,
    context_facts: list[dict],
    db: psycopg2.extensions.connection,
) -> dict:
    """
    LLM-powered retraction intent extraction (dprompt-143).
    Extract semantic intent for retraction/removal operations.

    Per dprompt-143: Retraction does NOT create new rel_types/entities, only works with facts already in DB.

    Returns: {
        subject: entity name or "user",
        rel_type: relationship being negated,
        object: target entity/value (may be null for "negate all"),
        action: "remove" | "supersede" | "negate" | "forget",
        dimension: "SCALAR" | "RELATIONAL" | "HIERARCHICAL" | "ENTITY",
        match_scope: "all" | "specific",
        confidence: 0.0-1.0,
        reason: explanation
    }
    """
    system_prompt = """
You are a RETRACTION INTENT EXTRACTOR for a personal memory system.

User is removing or negating a fact they previously stated.
Your job: Extract EXACTLY what semantic intent the user is expressing.

DO NOT try to:
- Resolve entity names to UUIDs (backend will do that via /query)
- Create new rel_types or entities
- Guess at missing information

IMPORTANT CONSTRAINTS:
1. Extract semantic intent ONLY (subject, rel_type, object, action, dimension)
2. Distinguish: remove (delete), supersede (replace), negate (deny all), forget (erase)
3. Match scope: "all" = negate entire rel_type class, "specific" = remove one instance
4. Confidence: 0.95-1.0 explicit ("I don't have X"), 0.85-0.94 clear ("not married"), <0.70 REJECT
5. If uncertain → return {{}}, don't guess

RECENT FACTS (context for understanding):
{formatted_facts}

RELATIONSHIP TYPES (to understand semantics):
{dynamic_rel_types}

USER MESSAGE: "{text}"

TASK:
Extract the retraction intent using this JSON structure.

{{
  "subject": "entity name or 'user'",
  "rel_type": "relationship being negated (lowercased)",
  "object": "target entity/value or null for 'any'",
  "action": "remove | supersede | negate | forget",
  "dimension": "SCALAR | RELATIONAL | HIERARCHICAL | ENTITY",
  "match_scope": "all | specific",
  "confidence": 0.0 to 1.0,
  "reason": "Why you extracted this"
}}

RULES:

1. SCALAR (age, height, name):
   - dimension = SCALAR
   - rel_type = the attribute (age, height, occupation)
   - object = null (scalars don't have entity targets)
   - Example: "I'm not 42" → subject="user", rel_type="age", object=null

2. RELATIONAL (spouse, has_pet, works_for):
   - dimension = RELATIONAL
   - rel_type = the relationship
   - object = the target entity (or null if "any")
   - Example: "I don't have any pets" → rel_type="has_pet", object=null, match_scope="all"

3. HIERARCHICAL (instance_of, member_of):
   - dimension = HIERARCHICAL
   - rel_type = the hierarchy rel (instance_of, member_of, part_of)
   - object = the type/class being removed
   - Example: "<pet> is not a <animal_type>" → rel_type="instance_of", object="bunny"

4. ENTITY (alias removal):
   - dimension = ENTITY
   - action = "remove"
   - rel_type = "pref_name" or "also_known_as"
   - object = the alias to remove
   - Example: "Remove the name 'Fraggle'" → rel_type="pref_name", object="Fraggle"

CONFIDENCE SCORING:

- 0.95–1.0: Explicit negation ("I don't have X", "X is NOT Y")
- 0.85–0.94: Clear but implicit ("I'm not married", "That's wrong")
- 0.70–0.84: Requires context ("I was mistaken about X")
- < 0.70: REJECT (return {{}}) — too vague

RESPOND WITH VALID JSON ONLY (no markdown, no explanation).
If extraction is impossible or confidence < 0.70, return {{}}."""

    try:
        with db.cursor() as cur:
            cur.execute("""
                SELECT rel_type, natural_language, category
                FROM rel_types
                WHERE natural_language IS NOT NULL
                ORDER BY category, rel_type
                LIMIT 20
            """)
            rel_types_list = cur.fetchall()
            rel_types_str = "\n".join(
                f"- {row[0]}: {row[1]} (category: {row[2]})"
                for row in rel_types_list
            )
    except Exception as e:
        log.warning("retraction_extraction.rel_types_load_failed", error=str(e))
        rel_types_str = "(database query failed, using generic examples)"

    facts_str = "\n".join(
        f"- {f.get('subject', 'unknown')}: {f.get('rel_type', 'unknown')} → {f.get('object', 'unknown')}"
        for f in (context_facts or [])[:10]
    ) if context_facts else "(no recent facts for reference)"

    system_prompt = system_prompt.format(
        dynamic_rel_types=rel_types_str,
        formatted_facts=facts_str,
        text=text
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text}
    ]

    try:
        # Use centralized LLM call with automatic retry/fallback
        extraction = call_llm_with_retry_sync(
            messages=messages,
            model=os.getenv("WGM_LLM_MODEL", "qwen/qwen3.5-9b"),
            user_id=user_id,
            operation="retraction_extraction",
        )

        if extraction:
            log.info("retraction_extraction.llm_success",
                    user_id=user_id,
                    subject=extraction.get("subject"),
                    rel_type=extraction.get("rel_type"),
                    action=extraction.get("action"),
                    confidence=extraction.get("confidence"))
            return extraction
        else:
            log.warning("retraction_extraction.llm_no_json", user_id=user_id)
            return {}
    except Exception as e:
        import traceback
        log.error("retraction_extraction.llm_failed", error=str(e), user_id=user_id, traceback=traceback.format_exc())
        return {}

@app.get("/health")
def health():
    """Health check with dependency status. Caches result for 5 seconds."""
    import time as _time
    global _health_cache, _health_cache_ts
    _now = _time.time()
    if _health_cache and (_now - _health_cache_ts) < 5:
        return _health_cache

    if _gliner2_model is None:
        raise HTTPException(status_code=503, detail="Model loading")

    dsn = os.environ.get("POSTGRES_DSN", "")
    qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")
    qwen_url = _configured_llm_url()

    db_ok = _check_db_health(dsn) if dsn else False
    qdrant_ok = _check_qdrant_health(qdrant_url)
    llm_ok = _check_llm_health(qwen_url)

    all_ok = db_ok and qdrant_ok and llm_ok
    status = "ok" if all_ok else "degraded"
    if not db_ok:
        status = "unhealthy"

    _health_cache = {
        "status": status,
        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(_now)),
        "database": "ok" if db_ok else "unreachable",
        "qdrant": "ok" if qdrant_ok else "unreachable",
        "llm": "ok" if llm_ok else "unreachable",
        "re_embedder": _embedder_stats,
        "model_loaded": True,
    }
    _health_cache_ts = _now
    return _health_cache


@app.post("/admin/logging/level")
async def set_logging_level(level: str):
    """
    Set global logging level (CRIT | WARN | INFO | DEBUG).
    Callable from OpenWebUI valve for runtime control.

    Default: INFO — exposes all failures except DEBUG.
    Set to DEBUG to enable verbose troubleshooting.
    """
    try:
        set_log_level(level)
        current = get_log_level()
        log.info(
            "logging.level_set",
            level=current,
            source="api",
            valid_levels=list(LogLevel)
        )
        return {
            "status": "ok",
            "current_level": str(current),
            "valid_levels": [str(l) for l in LogLevel]
        }
    except ValueError as e:
        log.warn("logging.invalid_level_request", attempted=level, error=str(e))
        raise HTTPException(
            status_code=400,
            detail=f"Invalid level '{level}'. Valid: CRIT, WARN, INFO, DEBUG"
        )


@app.get("/admin/logging/level")
async def get_logging_level_endpoint():
    """Get current logging level."""
    return {
        "current_level": str(get_log_level()),
        "valid_levels": [str(l) for l in LogLevel]
    }


@app.post("/admin/cache/clear-embeddings")
async def clear_embedding_cache_endpoint(api_key: str):
    """Emergency endpoint to clear embedding cache (requires admin key).

    dprompt-121: Use ONLY if rel_types were renamed/deleted or embedding model was upgraded.
    Cache will auto-repopulate on next eval cycle.

    Args:
        api_key: Must match ADMIN_API_KEY environment variable

    Returns:
        Status and count of entries deleted
    """
    admin_key = os.getenv("ADMIN_API_KEY")
    if not admin_key or api_key != admin_key:
        raise HTTPException(status_code=401, detail="Invalid or missing admin key")

    # Import here to avoid circular dependency
    try:
        from src.re_embedder.embedder import _embedding_cache
        if _embedding_cache and _embedding_cache.client:
            deleted = _embedding_cache.clear_pattern(f"{_embedding_cache.prefix}*")
            log.info("admin.embedding_cache_cleared", entries_deleted=deleted)
            return {"status": "cleared", "entries_deleted": deleted}
        else:
            return {"status": "cache_unavailable"}
    except Exception as e:
        log.error("admin.embedding_cache_clear_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Cache clear failed: {str(e)}")


@app.post("/ontology/rel_types")
def add_rel_type(req: RelTypeRequest):
    """
    User-asserted rel_type registration. Source is always 'user'.
    Wikidata and builtin types cannot be overwritten via this endpoint.
    """
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise HTTPException(status_code=503, detail="DB unavailable")
    try:
        with psycopg2.connect(dsn) as db:
            with db.cursor() as cur:
                # Trust hierarchy: user > wikidata > engine > builtin
                # Users can overwrite anything. Engine cannot overwrite user or wikidata.
                cur.execute(
                    "SELECT source FROM rel_types WHERE rel_type = %s",
                    (req.rel_type.lower(),),
                )
                existing = cur.fetchone()
                if existing and existing[0] == "user":
                    pass  # user-asserted types can be updated by users
                # Metadata-driven rel_type registration (dprompt-97)
                # Users can specify head_types, tail_types, is_symmetric, inverse_rel_type, is_hierarchy_rel
                cur.execute(
                    "INSERT INTO rel_types"
                    " (rel_type, label, wikidata_pid, engine_generated, confidence, source,"
                    "  correction_behavior, head_types, tail_types, is_symmetric, inverse_rel_type, is_hierarchy_rel)"
                    " VALUES (%s, %s, %s, false, 1.0, 'user', %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (rel_type) DO UPDATE SET"
                    "   label = EXCLUDED.label,"
                    "   source = 'user',"
                    "   correction_behavior = EXCLUDED.correction_behavior,"
                    "   head_types = COALESCE(EXCLUDED.head_types, rel_types.head_types),"
                    "   tail_types = COALESCE(EXCLUDED.tail_types, rel_types.tail_types),"
                    "   is_symmetric = COALESCE(EXCLUDED.is_symmetric, rel_types.is_symmetric),"
                    "   inverse_rel_type = COALESCE(EXCLUDED.inverse_rel_type, rel_types.inverse_rel_type),"
                    "   is_hierarchy_rel = COALESCE(EXCLUDED.is_hierarchy_rel, rel_types.is_hierarchy_rel)",
                    (
                        req.rel_type.lower(),
                        req.label,
                        req.wikidata_pid,
                        req.correction_behavior,
                        req.head_types,
                        req.tail_types,
                        req.is_symmetric,
                        req.inverse_rel_type,
                        req.is_hierarchy_rel,
                    ),
                )
        if _rel_type_registry:
            _rel_type_registry._refresh()
        return {"status": "ok", "rel_type": req.rel_type.lower(), "source": "user"}
    except HTTPException:
        raise
    except Exception as e:
        log.error("ontology.add_rel_type_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))

def _build_extract_context(user_id: str) -> dict:
    """
    Build fresh context from database for GLiNER2 extraction (dBug-018).
    Queries entity_aliases, rel_types, and user facts on each call.
    dprompt-79 constraint: DB-sourced, no caching, no hardcoding.
    Returns dict with context fields or empty dict on failure.
    """
    context = {}
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn or user_id == "anonymous":
        return context
    try:
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                # Query 1: Known entities for this user (top 20 preferred names)
                cur.execute("""
                    SELECT ea.alias, e.entity_type, ea.entity_id
                    FROM entity_aliases ea
                    JOIN entities e ON ea.entity_id = e.id AND ea.user_id = e.user_id
                    WHERE ea.user_id = %s AND ea.is_preferred = true
                    LIMIT 20
                """, (user_id,))
                known_entities = [
                    {"name": row[0], "type": row[1] or "Unknown", "uuid": row[2]}
                    for row in cur.fetchall()
                ]
                if known_entities:
                    context["known_entities"] = known_entities

                # Query 2: Medical rel_types with head/tail constraints
                cur.execute("""
                    SELECT rel_type, head_types, tail_types
                    FROM rel_types
                    WHERE head_types IS NOT NULL AND tail_types IS NOT NULL
                    LIMIT 30
                """)
                ontology_hints = []
                for row in cur.fetchall():
                    rt, heads, tails = row[0], row[1] or [], row[2] or []
                    if heads and tails:
                        ontology_hints.append(
                            f"{rt} → {','.join(h for h in heads if h != 'ANY')}"
                            f"→{','.join(t for t in tails if t != 'ANY')}"
                        )
                if ontology_hints:
                    context["ontology_hints"] = ontology_hints[:15]

                # Query 3: User facts for profile
                cur.execute("""
                    SELECT rel_type, subject_id, object_id FROM facts
                    WHERE user_id = %s AND rel_type IN
                    ('parent_of','spouse','lives_at','has_pet','works_for','also_known_as','pref_name')
                    LIMIT 10
                """, (user_id,))
                profile_parts = []
                for row in cur.fetchall():
                    rel, subj, obj = row[0], row[1], row[2]
                    if rel in ("parent_of", "spouse", "has_pet"):
                        profile_parts.append(f"{rel}={obj}")
                    elif rel == "lives_at":
                        profile_parts.append(f"location={obj}")
                    elif rel in ("also_known_as", "pref_name"):
                        profile_parts.append(f"name={obj}")
                if profile_parts:
                    context["user_profile"] = f"User: {user_id}. {'; '.join(profile_parts[:8])}."

                # Body parts taxonomy reference removed (dprompt-86).
                # Taxonomy seeding eliminated; taxonomies should emerge from data.
    except Exception as e:
        log.warning("extract.context_build_failed", error=str(e))
    return context


def _infer_hierarchy_from_signals(
    extracted_entities: list,
    extracted_rel_types: list,
    user_id: str,
    hierarchy_id_param: str,
    db
) -> dict:
    """
    dprompt-129: Layered hierarchy inference (fast path optimization).

    Tries entity_types first (easiest), falls back to rel_types (harder),
    combines both if needed. Returns on first unique match (fast path).

    Returns: {"hierarchy_id": uuid, "entity_types": [...], "confidence": 0.95, "layer": "entity_types"}
    or None (no match, bac${LOCATION}ard compat)
    """
    # PRIORITY 0: Explicit hierarchy_id param always wins
    if hierarchy_id_param:
        try:
            with db.cursor() as cur:
                cur.execute("""
                    SELECT id, entity_types
                    FROM entity_taxonomies
                    WHERE user_id = %s AND id = %s::uuid
                """, (user_id, hierarchy_id_param))
                row = cur.fetchone()
            if row:
                return {
                    "hierarchy_id": str(row[0]),
                    "entity_types": row[1] or [],
                    "confidence": 1.0,
                    "layer": "explicit_param",
                }
        except Exception as e:
            log.warning("ingest.explicit_hierarchy_lookup_failed",
                       hierarchy_id=hierarchy_id_param, error=str(e))
        return None  # Explicit param provided but not found

    # LAYER 1 (EASIEST): Match entity_types only
    detected_entity_types = {e.get("type") for e in extracted_entities if e.get("type")}
    if detected_entity_types:
        matching_hierarchies = _find_hierarchies_by_entity_types(
            detected_entity_types, user_id, db
        )
        if len(matching_hierarchies) == 1:
            # UNIQUE match on Layer 1 — fast path
            best = matching_hierarchies[0]
            log.info("ingest.hierarchy_inferred", layer="entity_types",
                    hierarchy_id=best[0], entity_types=detected_entity_types)
            return {
                "hierarchy_id": str(best[0]),
                "entity_types": best[1] or [],
                "confidence": 0.95,
                "layer": "entity_types",
            }
        elif len(matching_hierarchies) > 1:
            log.info("ingest.hierarchy_ambiguous", layer="entity_types",
                    detected_types=detected_entity_types, num_matches=len(matching_hierarchies))

    # LAYER 2 (HARDER): Match rel_types only
    detected_rel_types = set(extracted_rel_types) if extracted_rel_types else set()
    if detected_rel_types:
        try:
            matching_hierarchies = _find_hierarchies_by_rel_types(
                detected_rel_types, user_id, db
            )
            if len(matching_hierarchies) == 1:
                # UNIQUE match on Layer 2
                best = matching_hierarchies[0]
                log.info("ingest.hierarchy_inferred", layer="rel_types",
                        hierarchy_id=best[0], rel_types=detected_rel_types)
                return {
                    "hierarchy_id": str(best[0]),
                    "entity_types": best[1] or [],
                    "confidence": 0.85,
                    "layer": "rel_types",
                }
            elif len(matching_hierarchies) > 1:
                log.info("ingest.hierarchy_ambiguous", layer="rel_types",
                        detected_rel_types=detected_rel_types, num_matches=len(matching_hierarchies))
        except Exception as e:
            log.warning("ingest.rel_type_hierarchy_lookup_failed", error=str(e))

    # LAYER 3 (COMBINE): Both signals together
    if detected_entity_types and detected_rel_types:
        try:
            matching_hierarchies = _find_hierarchies_by_both(
                detected_entity_types, detected_rel_types, user_id, db
            )
            if matching_hierarchies:
                best = matching_hierarchies[0]  # Top-scored match
                log.info("ingest.hierarchy_inferred", layer="both_signals",
                        hierarchy_id=best[0])
                return {
                    "hierarchy_id": str(best[0]),
                    "entity_types": best[1] or [],
                    "confidence": 0.8,
                    "layer": "both_entity_types_and_rel_types",
                }
        except Exception as e:
            log.warning("ingest.combined_hierarchy_lookup_failed", error=str(e))

    # FALLBACK: No match on any layer (bac${LOCATION}ard compat)
    log.info("ingest.no_hierarchy_inferred",
            detected_entity_types=detected_entity_types,
            detected_rel_types=detected_rel_types)
    return None


def _find_hierarchies_by_entity_types(detected_types: set, user_id: str, db) -> list:
    """Layer 1: Find hierarchies whose entity_types overlap with detected types."""
    try:
        with db.cursor() as cur:
            cur.execute("""
                SELECT id, entity_types
                FROM entity_taxonomies
                WHERE user_id = %s
                ORDER BY ARRAY_LENGTH(entity_types, 1) DESC
                LIMIT 10
            """, (user_id,))
            rows = cur.fetchall()

        matches = []
        for row in rows:
            hierarchy_types = set(row[1]) if row[1] else set()
            overlap = len(detected_types & hierarchy_types)
            if overlap > 0:
                matches.append((row[0], row[1], overlap))

        # Sort by overlap score alicecending
        matches.sort(key=lambda x: x[2], reverse=True)
        return matches
    except Exception as e:
        log.warning("find_hierarchies_by_entity_types_failed", error=str(e))
        return []


def _find_hierarchies_by_rel_types(detected_rel_types: set, user_id: str, db) -> list:
    """Layer 2: Find hierarchies whose rel_types overlap with detected rel_types."""
    try:
        with db.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT et.id, et.entity_types
                FROM entity_taxonomies et
                WHERE et.user_id = %s
                ORDER BY et.created_at DESC
                LIMIT 10
            """, (user_id,))
            rows = cur.fetchall()

        return [(row[0], row[1]) for row in rows if row]
    except Exception as e:
        log.warning("find_hierarchies_by_rel_types_failed", error=str(e))
        return []


def _find_hierarchies_by_both(
    detected_entity_types: set,
    detected_rel_types: set,
    user_id: str,
    db
) -> list:
    """Layer 3: Find hierarchies matching BOTH entity_types AND rel_types."""
    try:
        with db.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT et.id, et.entity_types
                FROM entity_taxonomies et
                WHERE et.user_id = %s
                ORDER BY ARRAY_LENGTH(et.entity_types, 1) DESC
                LIMIT 10
            """, (user_id,))
            rows = cur.fetchall()

        matches = []
        for row in rows:
            hierarchy_types = set(row[1]) if row[1] else set()
            entity_overlap = len(detected_entity_types & hierarchy_types)
            if entity_overlap > 0:
                matches.append((row[0], row[1], entity_overlap))

        # Sort by combined score
        matches.sort(key=lambda x: x[2], reverse=True)
        return matches
    except Exception as e:
        log.warning("find_hierarchies_by_both_failed", error=str(e))
        return []


def _validate_triple_against_metadata(triple: dict, db) -> dict:
    """
    dprompt-129: Validate triple against rel_types metadata.

    Checks head_types/tail_types constraints. Records validation error
    but does NOT force low confidence — ingest logic decialice confidence
    based on whether it's user-stated (direct user correction bypasses
    type constraints). Novel rel_types pass through (Class C).

    Treats "unknown" type as "not yet determined" and skips validation,
    allowing ingest to populate types from DB/GLiNER2 later.
    """
    rel_type = (triple.get("rel_type") or "").lower().strip()
    subject_type = (triple.get("subject_type") or "").upper().strip()
    object_type = (triple.get("object_type") or "").upper().strip()

    # Treat "unknown" as "type not yet determined" — skip validation and let ingest populate
    if subject_type == "UNKNOWN":
        subject_type = ""
    if object_type == "UNKNOWN":
        object_type = ""

    if not rel_type:
        return triple  # No rel_type, can't validate

    try:
        with db.cursor() as cur:
            cur.execute("""
                SELECT head_types, tail_types
                FROM rel_types WHERE rel_type = %s
            """, (rel_type,))
            row = cur.fetchone()

        if not row:
            # Novel rel_type: pass through (Class C, WGM gate evaluates)
            return triple

        head_types = row[0] or []  # JSON array
        tail_types = row[1] or []  # JSON array

        # If rel_type has NO constraints, passes all types
        if not head_types and not tail_types:
            return triple

        # Validate: subject_type in head_types AND object_type in tail_types (case-insensitive)
        # IMPORTANT: Skip validation if types are EMPTY — ingest will populate from DB/GLiNER2
        valid = True
        head_types_lower = [t.lower() for t in head_types] if head_types else []
        tail_types_lower = [t.lower() for t in tail_types] if tail_types else []

        if head_types and "any" not in head_types_lower:
            if subject_type and subject_type.lower() not in head_types_lower:
                valid = False
        if tail_types and "any" not in tail_types_lower:
            if object_type and object_type.lower() not in tail_types_lower:
                valid = False

        if not valid:
            # Validation error: Record it but don't force low confidence.
            # Ingest logic will decide confidence based on is_correction flag
            # (user-stated facts override type constraints).
            # This allows corrections like "update my IP to 192.168.1.1" to be
            # treated as high-confidence user-driven updates, not low-confidence errors.
            triple["validation_error"] = (
                f"Type mismatch: ({subject_type}, {rel_type}, {object_type}) "
                f"not in metadata (expected {head_types}, {tail_types})"
            )
            log.info("extract.triple_validation_warning",
                    triple=triple, error=triple["validation_error"],
                    note="Type mismatch recorded but confidence not forced low — ingest will populate from DB/GLiNER2")

        return triple
    except Exception as e:
        log.warning("validate_triple_against_metadata_failed",
                   rel_type=rel_type, error=str(e))
        return triple


# ──────────────────────────────────────────────────────────────────────────────
# dprompt-144: Intent Classification Endpoint
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/classify-intent")
@rate_limit(calls_per_minute=60)
async def classify_intent(req: dict, user_id: str = None, model=Depends(get_gliner_model)):
    """
    Classify message intent using GLiNER2.

    SECURITY: user_id required (validated by rate limiter), rate limited 60/min per user

    Returns: {'intent': 'QUERY'|'RETRACTION'|'CORRECTION'|'STATEMENT', 'confidence': float}

    Benchmarked performance:
    - 4-class problem: ~87% accuracy
    - Latency: <50ms on CPU
    """
    # Validate user_id
    if not user_id or not isinstance(user_id, str) or len(user_id) < 4:
        log.warning("classify_intent.invalid_user_id", user_id_len=len(user_id or ""))
        return {"intent": "STATEMENT", "confidence": 0.0}

    if model is None:
        return {"intent": "STATEMENT", "confidence": 0.0}

    text = req.get("text", "").strip()
    if not text or len(text) < 2:
        return {"intent": "STATEMENT", "confidence": 0.0}

    try:
        # GLiNER2 classify_text returns: {'intent': 'STATEMENT'} or
        # with include_confidence=True: {'intent': {'label': 'STATEMENT', 'confidence': 0.85}}
        print(f"[/classify-intent] GLiNER2 input: text_len={len(text)} | schema={{intent: [QUERY, RETRACTION, CORRECTION, STATEMENT]}}", flush=True)

        result = model.classify_text(
            text,
            {"intent": ["QUERY", "RETRACTION", "CORRECTION", "STATEMENT"]},
            include_confidence=True
        )
        print(f"[/classify-intent] GLiNER2 raw result: {result}", flush=True)

        intent_result = result.get("intent", {})
        if isinstance(intent_result, dict):
            intent = intent_result.get("label", "STATEMENT")
            confidence = intent_result.get("confidence", 0.0)
        else:
            intent = intent_result
            confidence = 0.5  # Unknown confidence

        print(f"[/classify-intent] Parsed: intent={intent} | confidence={confidence:.3f} | type={type(intent_result)}", flush=True)

        # Log intent (no text, no user_id, just decision)
        log.debug("classify_intent.result", intent=intent, confidence=round(confidence, 2))

        return {
            "intent": intent,
            "confidence": float(confidence)
        }
    except Exception as e:
        print(f"[/classify-intent] ERROR: {type(e).__name__}: {e}", flush=True)
        log.error("classify_intent_error", error=str(e))
        return {"intent": "STATEMENT", "confidence": 0.0}


@app.post("/query-negation-patterns")
async def query_negation_patterns(req: dict, user_id: str = None):
    """
    Layer 2: Query negation_patterns table for intent override fallback.

    Called when GLiNER2 confidence < gate threshold.
    Performs substring matching on negation_patterns for this user.

    Returns:
        {'negation_type': 'retraction'|'correction'|'denial', 'confidence': float}
        or None if no match found

    SECURITY: user_id validated (len >= 4, isinstance str)
    """
    if not user_id or not isinstance(user_id, str) or len(user_id) < 4:
        log.warning("query_negation_patterns.invalid_user_id", user_id_len=len(user_id or ""))
        return None

    text = req.get("text", "").strip().lower()
    if not text or len(text) < 2:
        return None

    try:
        db = psycopg2.connect(os.getenv("POSTGRES_DSN", "postgresql://faultline:faultline@localhost:5432/faultline"))
        cursor = db.cursor()

        # Query negation_patterns for substring matches
        # ORDER BY confidence DESC to get highest-confidence match
        query = """
            SELECT negation_type, confidence
            FROM negation_patterns
            WHERE user_id = %s
              AND POSITION(pattern_text IN %s) > 0
            ORDER BY confidence DESC
            LIMIT 1
        """
        cursor.execute(query, (user_id, text))
        row = cursor.fetchone()
        cursor.close()
        db.close()

        if row:
            negation_type, confidence = row
            log.debug("query_negation_patterns.match", negation_type=negation_type, confidence=round(confidence, 2))
            return {"negation_type": negation_type, "confidence": float(confidence)}

        return None
    except Exception as e:
        log.error("query_negation_patterns_error", error=str(e))
        return None


@app.get("/confidence-gate/{user_id}")
async def get_confidence_gate(user_id: str):
    """
    Layer 3: Get per-user confidence gate threshold for intent classification.

    Defaults to 0.70 if user has no history.
    Threshold adapts based on user corrections (re_embedder feedback).

    Returns:
        {'threshold': float [0.5, 0.75]}

    SECURITY: user_id validated (len >= 4, isinstance str)
    """
    if not user_id or not isinstance(user_id, str) or len(user_id) < 4:
        log.warning("get_confidence_gate.invalid_user_id", user_id_len=len(user_id or ""))
        return {"threshold": 0.70}  # Safe default

    try:
        db = psycopg2.connect(os.getenv("POSTGRES_DSN", "postgresql://faultline:faultline@localhost:5432/faultline"))
        cursor = db.cursor()

        # Query intent_confidence_feedback to calculate adaptive threshold
        query = """
            SELECT
                COALESCE(
                    0.70 - (
                        CAST(SUM(CASE WHEN feedback_type = 'correction' THEN count ELSE 0 END) AS FLOAT) /
                        NULLIF(SUM(count), 0)
                    ) * 0.1,
                    0.70
                ) as threshold
            FROM intent_confidence_feedback
            WHERE user_id = %s
        """
        cursor.execute(query, (user_id,))
        row = cursor.fetchone()
        cursor.close()
        db.close()

        if row and row[0] is not None:
            threshold = float(row[0])
            # Clamp to valid range [0.5, 0.75]
            threshold = max(0.5, min(0.75, threshold))
            log.debug("get_confidence_gate.threshold", user_id=user_id[:12], threshold=round(threshold, 2))
            return {"threshold": threshold}

        return {"threshold": 0.70}
    except Exception as e:
        log.error("get_confidence_gate_error", error=str(e))
        return {"threshold": 0.70}  # Safe fallback


@app.post("/extract")
def extract(req: IngestRequest, model=Depends(get_gliner_model)):
    """
    Run GLiNER2 entity extraction with optional context enrichment (dBug-018).
    When user_id is provided, builds fresh context from database (entity registry,
    ontology hints, user profile) to enrich GLiNER2's schema for better entity
    disambiguation.  Post-processes null subjects/objects with ontology-informed
    resolution instead of hardcoded rules.
    """
    if model is None:
        return {"entities": []}
    try:
        # Build fresh context from DB if user_id provided (dprompt-79: no caching)
        ctx = {}
        if req.user_id and req.user_id != "anonymous" and not req.context:
            ctx = _build_extract_context(req.user_id)
        elif req.context:
            ctx = req.context.model_dump() if hasattr(req.context, 'model_dump') else req.context

        constraint = _get_constraint()
        schema = {
            "facts": [
                "subject::str::The full proper name of the first entity. Never a pronoun.",
                "object::str::The full proper name of the second entity. Never a pronoun.",
                f"rel_type::[{constraint}]::str::The relationship type from subject to object.",
                "subject_type::[Person|Animal|Organization|Location|Object|Concept]::str::The entity type of the subject.",
                "object_type::[Person|Animal|Organization|Location|Object|Concept]::str::The entity type of the object.",
            ]
        }

        # GLiNER2 NER extraction — just get entity types, not relationships
        ner_result = model.extract_entities(
            req.text,
            ["Person", "Animal", "Organization", "Location", "Object", "Concept"]
        )

        # Convert NER result format to entity list with types
        raw_entities = []
        for entity_type, entity_names in ner_result.get("entities", {}).items():
            if entity_names and isinstance(entity_names, list):
                for name in entity_names:
                    if name and name.strip():
                        raw_entities.append({
                            "subject": name.strip(),
                            "subject_type": entity_type.upper(),
                            "object": "",
                            "object_type": "",
                            "rel_type": ""
                        })

        # dBug-026: Filter extracted entities to only valid named entity types (dprompt-97)
        # Removes stop words, rel_types, low-confidence extractions
        filtered_entities = _filter_extracted_entities(raw_entities)

        resolved_entities = []
        for entity in filtered_entities:
            # Post-process: if subject is null, the user is the implied subject
            if entity.get("subject") is None and entity.get("object") is not None and entity.get("rel_type"):
                entity["subject"] = "user"
                entity["subject_type"] = "Person"
                log.info("extract.null_subject_resolved",
                         rel_type=entity["rel_type"], object=entity["object"])
            # Post-process: if object is null but subject is a person and rel_type implies parent relationship
            elif entity.get("object") is None and entity.get("rel_type") == "child_of":
                # The user is the implied parent; flip the relationship
                entity["rel_type"] = "parent_of"
                entity["object"] = entity.get("subject")
                entity["object_type"] = entity.get("subject_type")
                entity["subject"] = "user"
                entity["subject_type"] = "Person"
                log.info("extract.null_object_resolved", subject=entity["object"], rel_type="parent_of")
            # Ontology-driven resolution removed (dprompt-86). Taxonomy seeding eliminated.
            resolved_entities.append(entity)

        if len(filtered_entities) < len(raw_entities):
            log.info("extract.entities_filtered",
                     raw_count=len(raw_entities), filtered_count=len(filtered_entities),
                     rejected=len(raw_entities) - len(filtered_entities))

        return {"entities": resolved_entities}
    except Exception as e:
        log.error("extract.gliner2_failed", error=str(e))
        return {"entities": []}


def _build_extraction_prompt(db_connection=None) -> str:
    """
    dprompt-127: Pattern-based extraction prompt, metadata-driven, domain-generic.

    Replaces ambiguous FORMAT placeholder with THREE DISTINCT EXTRACTION PATTERNS:
    1. Relationships (entity → entity): parent_of, spouse, works_for, etc.
    2. Scalars (entity → value): age, height, occupation (string/number/date values)
    3. Identity (entity → alias): pref_name, also_known_as, same_as

    Dynamically loads rel_type guidance from DB using tail_types metadata.
    NO hardcoded entity names. NO user data in prompt.
    Works for any domain: family, work, location, computer_system, etc.
    """
    base_prompt = """Extract ALL relationships and facts from text. Return ONLY a JSON array of triples. Each triple must have: subject, object, rel_type, definition.

CRITICAL DISTINCTION — Three Extraction Patterns:

PATTERN 1 — RELATIONSHIPS (entity → entity):
  Both subject and object are ENTITY NAMES (person, organization, location, object, etc.)
  rel_type alicecribes the RELATIONSHIP (parent_of, works_for, located_in, knows, spouse, etc.)
  Structure: {"subject":"entity_name","object":"entity_name","rel_type":"relationship_type","definition":"..."}
  Examples (generic):
    - {"subject":"subject_entity","object":"object_entity","rel_type":"parent_of","definition":"subject is parent of object"}
    - {"subject":"subject_entity","object":"object_entity","rel_type":"works_for","definition":"subject works for object"}

PATTERN 2 — SCALARS/ATTRIBUTES (entity → value):
  Subject is ENTITY NAME
  Object is LITERAL VALUE: string, number, date (NOT an entity name, NOT a rel_type name)
  rel_type alicecribes the ATTRIBUTE (age, height, occupation, born_on, nationality, etc.)
  Structure: {"subject":"entity_name","object":"value_as_string_or_number","rel_type":"attribute_type","definition":"..."}
  Examples (generic):
    - {"subject":"subject_entity","object":"42","rel_type":"age","definition":"subject is 42 years old"}
    - {"subject":"subject_entity","object":"Engineer","rel_type":"occupation","definition":"subject is an Engineer"}

PATTERN 3 — IDENTITY/ALIASES (entity → entity):
  Subject is PRIMARY ENTITY NAME
  Object is ALTERNATE NAME, NICKNAME, or ALIAS of same entity (also_known_as, pref_name, same_as)
  rel_type is ALWAYS: pref_name, also_known_as, or same_as
  Structure: {"subject":"entity_name","object":"alternate_name","rel_type":"identity_rel_type","definition":"..."}
  Examples (generic):
    - {"subject":"subject_entity","object":"alternate_name","rel_type":"also_known_as","definition":"subject is also known as alternate_name"}
    - {"subject":"subject_entity","object":"preferred_name","rel_type":"pref_name","definition":"subject prefers to be called preferred_name"}

⚠️  COMMON MISTAKE — DO NOT confuse rel_type names with entity values:
  WRONG:  {"subject":"entity","object":"pref_name","rel_type":"pref_name"}  ← object is rel_type name!
  RIGHT:  {"subject":"entity","object":"actual_value_here","rel_type":"pref_name"}  ← object is the actual value

  If rel_type is in the list (pref_name, age, parent_of, works_for, etc.), the object field must NEVER contain that rel_type name.
  Object is ALWAYS the actual entity name, value, or alias—never the relationship type itself.

EXTRACTION RULES ORGANIZED BY PATTERN:

PATTERN 1 — RELATIONSHIPS (these define connectivity between entities):
  - Extract: parent_of, child_of, spouse, sibling_of (family connections)
  - Extract: works_for, educated_at (professional connections)
  - Extract: knows, friend_of, met, related_to (social connections)
  - Extract: has_pet, owns (ownership)
  - Extract: located_in, lives_in, lives_at (location/residence)
  - Extract: instance_of, subclass_of, part_of, is_a, member_of (classification/hierarchy)
  - Extract: created_by (authorship)
  - Rules:
    * "My son is named X" → Extract TWO triples: (user, parent_of, X) AND (X, pref_name, X's actual name)
    * "My wife is X" → (user, spouse, X)
    * "X works for Y" → (X, works_for, Y)
    * Do NOT invert relationships; system handles directionality from metadata

PATTERN 2 — SCALARS/ATTRIBUTES (these assign properties to entities):
  - Extract: age, height, weight (numeric measurements)
  - Extract: born_on, born_in (temporal/location facts)
  - Extract: nationality, has_gender (demographics)
  - Extract: occupation, title (role alicecriptors)
  - Rules:
    * Object is ALWAYS a literal value (number, date string, or text string)
    * "X is 42" → (X, age, "42") — object is numeric string, NOT an entity
    * "X was born in 1980" → (X, born_on, "1980") or (X, born_in, "location_name")

PATTERN 3 — IDENTITY/ALIASES (these map alternate names to the same entity):
  - Extract: pref_name (preferred display name)
  - Extract: also_known_as (nickname, alternate name, known by)
  - Extract: same_as (entity identity resolution across contexts)
  - Rules:
    * Object is an ALTERNATE NAME for the entity (another name they go by)
    * "X is called Y" → (X, also_known_as, Y)
    * "X's full name is Y" → (X, pref_name, Y)

FIRST-PERSON RESOLUTION:
  - "I", "me", "my", "we" → always map to "user" entity (NEVER use pronouns literally)
  - "We have a dog" → (user, has_pet, dog_name)

AMBIGUOUS PRONOUNS:
  - If "he", "she", "it", "they" appear, resolve from prior context IF POSSIBLE
  - Omit the fact if no prior context available (prevents hallucination)

REL_TYPE METADATA (optional for novel rel_types):
  - head_types: entity types allowed as subject (e.g., ["Person"])
  - tail_types: object types allowed (e.g., ["Organization"], ["SCALAR"] for values)
  - is_symmetric: true if bidirectional (spouse, friend_of), false if directional (parent_of, works_for)
  - inverse_rel_type: reverse relationship (parent_of ↔ child_of)
  - is_hierarchy_rel: true for classification (instance_of, subclass_of), false for relational

CRITICAL — CORRECTION HANDLING:
When extracting a CORRECTION (marked with "is_correction": true):
  1. ALWAYS include the NEW VALUE in the object field (NEVER leave it empty)
  2. For scalar corrections (age, height, etc.), object MUST be the new value: ("user", age, "42")
  3. For relationship corrections (spouse, parent_of, etc.), object MUST be the new entity: ("user", spouse, "alice")
  4. For identity corrections (pref_name, also_known_as), object MUST be the new name: ("user", pref_name, "${USER}")

  EXAMPLES of CORRECTION EXTRACTION:
    - "I said 30, but I'm actually 42" → (user, age, "42", is_correction=true)
    - "My age is not 30, it's 42" → (user, age, "42", is_correction=true)
    - "I was wrong about my spouse. It's Alice not Bob." → (user, spouse, alice, is_correction=true)
    - "Actually, call me ${USER}, not ${USER}" → (user, pref_name, ${USER}, is_correction=true)

  ANTI-PATTERN (WRONG):
    - WRONG: (user, age, "", is_correction=true)  ← empty object field
    - WRONG: (user, age, "age", is_correction=true)  ← rel_type name as object
    - CORRECT: (user, age, "42", is_correction=true)  ← new value in object field

DOMAIN-SPECIFIC REL_TYPE GUIDANCE:
"""

    if db_connection:
        correction_rows = []  # Initialize before try block
        correction_behavior_rows = []  # dprompt-064 Phase 1
        try:
            with db_connection.cursor() as cur:
                # Query rel_types, organize by pattern (relationship, scalar, identity)
                cur.execute("""
                    SELECT
                        rel_type,
                        natural_language,
                        tail_types,
                        is_hierarchy_rel
                    FROM rel_types
                    WHERE rel_type NOT IN ('pref_name', 'also_known_as', 'same_as')
                    AND natural_language IS NOT NULL
                    ORDER BY
                        CASE
                            WHEN tail_types = ARRAY['SCALAR']::TEXT[] THEN 1
                            ELSE 0
                        END,
                        rel_type
                    LIMIT 15
                """)
                rel_rows = cur.fetchall()

                # dprompt-128-P2: Correction signal patterns (learned from correction_signals table)
                # Query patterns with weights for correction detection (same cursor context)
                cur.execute("""
                    SELECT pattern, pattern_type, confidence, example_usage
                    FROM correction_signals
                    ORDER BY priority ASC, confidence DESC
                    LIMIT 10
                """)
                correction_rows = cur.fetchall()

                # dprompt-064 Phase 1: Query rel_types that support corrections
                # Guialice LLM on which rel_types accept corrections and their semantics
                cur.execute("""
                    SELECT rel_type, label, correction_behavior
                    FROM rel_types
                    WHERE correction_behavior IS NOT NULL
                    AND correction_behavior != 'ignore'
                    ORDER BY correction_behavior, rel_type
                """)
                correction_behavior_rows = cur.fetchall()

            # Separate rel_types by pattern using metadata
            scalar_rels = []
            relationship_rels = []

            for row in rel_rows:
                rel_type, nl, tail_types, is_hierarchy = row
                # tail_types is PostgreSQL array (tuple), e.g., ('SCALAR',) or ('Person',)
                is_scalar = tail_types and len(tail_types) == 1 and tail_types[0] == 'SCALAR'
                if is_scalar:
                    scalar_rels.append({"rel_type": rel_type, "nl": nl})
                else:
                    relationship_rels.append({"rel_type": rel_type, "nl": nl})

            # Pattern 2 examples (scalars first, most specific)
            if scalar_rels:
                base_prompt += "\nAttributes (entity → scalar value):\n"
                for item in scalar_rels[:3]:
                    base_prompt += f'  - {item["rel_type"]}: {item["nl"]}\n'

            # Pattern 1 examples (relationships)
            if relationship_rels:
                base_prompt += "\nRelationships (entity → entity):\n"
                for item in relationship_rels[:5]:
                    base_prompt += f'  - {item["rel_type"]}: {item["nl"]}\n'

            # Pattern 3: Always include identity rels
            base_prompt += "\nIdentity (entity → alias):\n"
            base_prompt += '  - pref_name: entity\'s preferred display name\n'
            base_prompt += '  - also_known_as: entity\'s alternative names or nicknames\n'
            base_prompt += '  - same_as: entity identity resolution\n'

            # Add correction detection patterns if any loaded from DB
            if correction_rows:
                base_prompt += "\nCORRECTION DETECTION (applies to ALL triples in correction context):\n"
                base_prompt += "When correction pattern detected: mark ALL extracted triples in that message with \"is_correction\": true.\n"
                base_prompt += "Learned patterns (from database):\n"
                for pattern, pattern_type, confidence, example in correction_rows:
                    base_prompt += f'  - [{pattern_type}] "{pattern}" (confidence: {confidence:.2f})'
                    if example:
                        base_prompt += f' e.g. "{example}"'
                    base_prompt += '\n'

            # dprompt-064 Phase 1: Add correction-supporting rel_types from metadata
            if correction_behavior_rows:
                base_prompt += "\nCORRECTION-SUPPORTING REL_TYPES (metadata-driven):\n"
                base_prompt += "Mark with is_correction=true when user corrects these rel_types:\n"
                for rel_type, label, behavior in correction_behavior_rows:
                    base_prompt += f'  - {rel_type} ({behavior}): {label}\n'

        except Exception as e:
            log.error("extract_prompt.db_query_critical_failure", error=str(e))
            raise

    base_prompt += """

Return ONLY valid JSON array. No markdown, no explanations, no commentary.
CRITICAL: Never include rel_type names in the object field."""
    return base_prompt


def _track_correction_signal_candidate(
    user_id: str, text: str, triple: dict, db_connection=None
) -> None:
    """
    dprompt-128-P3: When LLM marks triple as is_correction=true,
    extract concise linguistic pattern and record in correction_signal_evaluations
    for re_embedder learning. Patterns accumulate across users so frequency >= 3
    means the pattern is real and recurring.

    Re-embedder will evaluate: frequency >= 3 → INSERT into correction_signals.
    """
    if not db_connection or not triple.get("is_correction"):
        return

    try:
        rel_type = (triple.get("rel_type") or "").lower().strip()
        if not rel_type:
            return

        text_lower = text.lower()
        pattern_type = "unknown"
        concise_pattern = None

        # Extract concise pattern keyword/phrase (will accumulate across users)
        if " is not " in text_lower:
            pattern_type = "negation"
            concise_pattern = "is not"
        elif " isn't " in text_lower:
            pattern_type = "negation"
            concise_pattern = "isn't"
        elif " not " in text_lower:
            pattern_type = "negation"
            concise_pattern = "not"
        elif "actually" in text_lower:
            pattern_type = "reclarification"
            concise_pattern = "actually"
        elif "wait" in text_lower:
            pattern_type = "reclarification"
            concise_pattern = "wait"
        elif "sorry" in text_lower:
            pattern_type = "reclarification"
            concise_pattern = "sorry"
        elif "wrong" in text_lower:
            pattern_type = "contradiction"
            concise_pattern = "wrong"
        elif "mistake" in text_lower:
            pattern_type = "contradiction"
            concise_pattern = "mistake"
        elif "incorrect" in text_lower:
            pattern_type = "contradiction"
            concise_pattern = "incorrect"

        if not concise_pattern:
            return  # No recognizable pattern extracted

        with db_connection.cursor() as cur:
            # Record as candidate for re_embedder evaluation
            # Patterns accumulate globally across all users (not per-user scoped)
            # Use a "global" user_id marker so all users' occurrences merge into single row
            global_user_id = "global_pattern_candidates"

            cur.execute("""
                INSERT INTO correction_signal_evaluations
                (user_id, candidate_pattern, pattern_type, first_text_snippet, occurrence_count)
                VALUES (%s, %s, %s, %s, 1)
                ON CONFLICT (user_id, candidate_pattern) DO UPDATE SET
                  occurrence_count = correction_signal_evaluations.occurrence_count + 1,
                  last_seen_at = NOW()
            """, (global_user_id, concise_pattern, pattern_type, text[:500]))

        log.info("extract.correction_signal_candidate_recorded",
                 rel_type=rel_type, pattern_type=pattern_type,
                 concise_pattern=concise_pattern, user_id=user_id)
    except Exception as e:
        log.warning("extract.correction_signal_tracking_failed", error=str(e))


@app.post("/extract/rewrite", response_model=dict)
async def extract_rewrite(req: RewriteRequest) -> dict:
    """
    LLM-based triple extraction. Replaces OpenWebUI Filter direct LLM calls.

    FaultLine is the SINGLE ENTRY POINT (8001). Filter no longer calls OpenWebUI:3000.
    This eliminates brittleness on OpenWebUI internal API changes.

    FaultLine internally:
    - Reads QWEN_API_URL, WGM_LLM_MODEL from environment (configured once, not per-request)
    - Calls the configured LLM backend (Qwen, Ollama, OpenAI, etc.)
    - Returns extracted triples

    Filter only needs to know: http://faultline:8001/extract/rewrite

    dprompt-120: Extraction prompt is metadata-driven from rel_types table,
    not hardcoded. Reduces prompt bloat and makes system generic.
    """
    global _EXTRACT_REWRITE_CALL_COUNT
    _EXTRACT_REWRITE_CALL_COUNT += 1
    log.info("extract_rewrite.call_start", call_count=_EXTRACT_REWRITE_CALL_COUNT, user_id=req.user_id, text_len=len(req.text or ""))
    try:
        import json
        import os

        qwen_url = _configured_llm_url()
        llm_model = os.getenv("WGM_LLM_MODEL", "qwen/qwen3.5-9b")

        # Build system prompt dynamically from rel_types metadata
        # Replaces hardcoded 4.3KB prompt with ~1KB database-driven version
        system_prompt = None
        db = None
        try:
            import psycopg2
            db = psycopg2.connect(os.getenv("POSTGRES_DSN"))
            system_prompt = _build_extraction_prompt(db)
        except Exception as e:
            if db:
                try:
                    db.rollback()
                except Exception:
                    pass
            log.warning("extract_prompt.db_connection_failed", error=str(e))
        finally:
            if db:
                try:
                    db.close()
                except Exception:
                    pass

        if not system_prompt:
            system_prompt = _build_extraction_prompt(None)

        messages = [{"role": "system", "content": system_prompt}]

        # Add conversation context if provided.
        # Take all user/assistant messages (up to 6) for pronoun resolution.
        # The Filter may pack system hints between turns; we only want the dialogue.
        if req.messages:
            _context_msgs = [m for m in req.messages if m.get("role") in ("user", "assistant")]
            for msg in _context_msgs[-6:]:  # Up to 6 turns for context
                messages.append(msg)

        # Add user text
        user_content = req.text
        if req.typed_entities:
            entity_lines = "\n".join(
                f"- {e.get('subject')} ({e.get('subject_type', 'unknown')}) "
                f"-- {e.get('object')} ({e.get('object_type', 'unknown')})"
                for e in req.typed_entities
                if e.get("subject") and e.get("object")
            )
            if entity_lines:
                user_content += f"\n\nDetected entities:\n{entity_lines}"

        messages.append({"role": "user", "content": user_content})

        # Call LLM using persistent pooled client with fallback chain
        # dprompt-129: Try multiple endpoints (host IP, container name, Docker IP, reverse proxy)
        # dBug-016: Use chat_id if provided, otherwise fall back to user_id
        payload = build_llm_payload(
            messages=messages,
            model=llm_model,
            user_id=req.chat_id or req.user_id,
            temperature=0.0,
            max_tokens=2048,
            # NOTE: thinking parameter removed — Qwen doesn't support extended thinking
        )

        # DEBUG: Log what we're sending to LLM
        log.info("extract_rewrite.llm_request",
                 model=llm_model,
                 text_preview=req.text[:150] if req.text else "",
                 system_prompt_len=len(system_prompt) if system_prompt else 0,
                 message_count=len(messages),
                 has_typed_entities=bool(req.typed_entities))

        # Use centralized LLM call with retry, circuit breaker, and proper timeout management
        # Replaces manual endpoint fallback loop with llm_calls.py implementation
        from .llm_calls import call_llm_with_retry_async

        result = await call_llm_with_retry_async(
            messages=messages,
            model=llm_model,
            user_id=req.user_id,
            timeout=LLMTimeouts.get("EXTRACT"),
            operation="EXTRACT",
        )

        # call_llm_with_retry_async() returns ALREADY-PARSED JSON (not the full OpenAI response)
        # It extracts content from {"choices":[{"message":{"content":"..."}}]} and returns json.loads(content)
        if not result or not isinstance(result, (list, dict)):
            log.error("extract_rewrite.llm_call_failed",
                     user_id=req.user_id,
                     text_preview=req.text[:150],
                     result_type=type(result).__name__)
            return {"status": "error", "detail": "LLM extraction failed after retries"}

        # result is already parsed JSON — normalize to list for downstream processing
        triples = result if isinstance(result, list) else [result]

        # Validate: at least one valid triple
        if not triples:
            log.warning("extract_rewrite.empty_triples_list",
                       user_id=req.user_id,
                       result=result)
            triples = []

        # Validation: each triple must have subject, object, rel_type fields
        valid_triples = [
            t for t in (triples if isinstance(triples, list) else [])
            if isinstance(t, dict) and t.get("subject") and t.get("object") and t.get("rel_type")
        ]
        triples = valid_triples

        # Enrich triples with entity types from GLiNER2 if not already present
        # Uses simpler "entities" schema for better extraction quality
        if triples and not req.typed_entities:
            try:
                gliner_model = get_gliner_model()
                if gliner_model:
                    # Collect unique entity names from LLM triples that need types
                    entities_needing_types = set()

                    # Query scalar rel_types (no need to type these objects)
                    scalar_rel_types = set()
                    db = None
                    try:
                        import psycopg2
                        db = psycopg2.connect(os.getenv("POSTGRES_DSN"))
                        with db.cursor() as cur:
                            cur.execute("""
                                SELECT rel_type FROM rel_types
                                WHERE tail_types @> ARRAY['SCALAR']::TEXT[]
                            """)
                            scalar_rel_types = {row[0] for row in cur.fetchall()}
                    except Exception as e:
                        log.warning("extract_rewrite.scalar_rel_types_query_failed", error=str(e))
                    finally:
                        if db:
                            try:
                                db.close()
                            except Exception:
                                pass

                    # Build list of entities that need type extraction
                    for triple in triples:
                        subj = (triple.get("subject") or "").strip()
                        if subj and not triple.get("subject_type"):
                            entities_needing_types.add(subj)

                        obj = (triple.get("object") or "").strip()
                        rel_type_lower = (triple.get("rel_type") or "").lower()
                        # Only type objects for non-scalar rel_types
                        if obj and rel_type_lower not in scalar_rel_types and not triple.get("object_type"):
                            entities_needing_types.add(obj)

                    log.debug("extract_rewrite.entities_needing_types_check",
                             entities_to_type=len(entities_needing_types),
                             triples_count=len(triples),
                             has_gliner=gliner_model is not None)

                    entity_types = {}
                    # Only call GLiNER2 if we have entities to type
                    if entities_needing_types:
                        db_gliner = None
                        try:
                            import psycopg2
                            db_gliner = psycopg2.connect(os.getenv("POSTGRES_DSN"))

                            # Query DB for known entity types (metadata-driven)
                            db_types = set()
                            try:
                                with db_gliner.cursor() as cur:
                                    # Get distinct non-unknown entity types from entities table
                                    cur.execute("""
                                        SELECT DISTINCT entity_type FROM entities
                                        WHERE entity_type != 'unknown' AND user_id = %s
                                        LIMIT 100
                                    """, (req.user_id,))
                                    db_types = {row[0] for row in cur.fetchall()}
                                db_gliner.commit()
                            except Exception as e:
                                log.debug("extract_rewrite.entity_types_query_failed", error=str(e))

                            # Attempt 1: Constrained inference with known types (from DB)
                            if db_types:
                                gliner_labels = list(db_types)
                                ner_result = gliner_model.extract_entities(
                                    req.text,
                                    gliner_labels,
                                    threshold=0.3
                                )
                                for entity_type, entity_names in ner_result.get("entities", {}).items():
                                    if entity_names:
                                        for name in entity_names:
                                            name_clean = (name or "").strip().lower()
                                            if name_clean:
                                                entity_types[name_clean] = entity_type.upper()
                                log.debug("extract_rewrite.gliner2_constrained_inference",
                                         attempt=1, found=len(entity_types), db_types_available=len(db_types))

                            # Attempt 2: Unconstrained inference for discovery (if still untyped entities remain)
                            untyped_remaining = {e for e in entities_needing_types if e.lower() not in entity_types}
                            if untyped_remaining and not db_types:
                                # No known types in DB: GLiNER2 discovery mode (infer freely)
                                # Let GLiNER2 discover new entity types without seed constraints
                                log.debug("extract_rewrite.gliner2_discovery_mode_start",
                                         untyped_count=len(untyped_remaining))
                                try:
                                    # GLiNER2 unconstrained: provide default entity types for discovery
                                    # GLiNER2 requires non-empty entity_types list
                                    default_entity_types = [
                                        "Person", "Animal", "Organization", "Location", "Object", "Concept"
                                    ]
                                    ner_result = gliner_model.extract_entities(
                                        req.text,
                                        default_entity_types,  # FIXED: Provide non-empty default types
                                        threshold=0.25
                                    )
                                    for entity_type, entity_names in ner_result.get("entities", {}).items():
                                        if entity_names and entity_type:
                                            for name in entity_names:
                                                name_clean = (name or "").strip().lower()
                                                if name_clean and name_clean in untyped_remaining:
                                                    entity_types[name_clean] = entity_type.upper()
                                    log.debug("extract_rewrite.gliner2_discovery_result",
                                             attempt=2, found=len(entity_types), new_discoveries=len([e for e in untyped_remaining if e.lower() in entity_types]))
                                except Exception as e:
                                    log.debug("extract_rewrite.gliner2_discovery_failed", error=str(e))

                        except Exception as e:
                            log.debug("extract_rewrite.gliner2_extraction_failed", error=str(e))
                        finally:
                            if db_gliner:
                                try:
                                    db_gliner.close()
                                except Exception:
                                    pass

                        log.info("extract_rewrite.gliner2_entity_extraction",
                                 entities_needed=len(entities_needing_types),
                                 entities_extracted=len(entity_types))

                    # Map extracted types to triples (metadata-driven)
                    for triple in triples:
                        if not triple.get("subject_type"):
                            subj_lower = (triple.get("subject") or "").strip().lower()
                            triple["subject_type"] = entity_types.get(subj_lower, "")
                        if not triple.get("object_type"):
                            rel_type_lower = (triple.get("rel_type") or "").lower()
                            # If rel_type is scalar (from DB), object_type is SCALAR
                            if rel_type_lower in scalar_rel_types:
                                triple["object_type"] = "SCALAR"
                            else:
                                # Otherwise lookup object in entity_types
                                obj_lower = (triple.get("object") or "").strip().lower()
                                triple["object_type"] = entity_types.get(obj_lower, "")

                    log.info("extract_rewrite.types_enriched",
                             entity_count=len(entity_types),
                             scalar_rel_types=len(scalar_rel_types))

                    # 3.5: IMMEDIATE STRENGTHEN — Store discovered entity types to DB NOW
                    # Don't wait for re-embedder; make types immediately available for ingest validation
                    if entity_types:
                        db_strengthen = None
                        try:
                            import psycopg2
                            db_strengthen = psycopg2.connect(os.getenv("POSTGRES_DSN"))
                            with db_strengthen.cursor() as cur:
                                for entity_name, discovered_type in entity_types.items():
                                    if discovered_type and discovered_type != "SCALAR":
                                        # Register entity with discovered type
                                        try:
                                            from src.entity_registry.registry import EntityRegistry
                                            registry = EntityRegistry(db_strengthen)
                                            entity_uuid = registry.resolve(req.user_id, entity_name)
                                            # Update entity type separately (resolve() doesn't take type_hint parameter)
                                            with db_strengthen.cursor() as _cur:
                                                _cur.execute(
                                                    "UPDATE entities SET entity_type = %s WHERE id = %s AND user_id = %s AND entity_type = 'unknown'",
                                                    (discovered_type.title(), entity_uuid, req.user_id)
                                                )
                                            log.debug("extract_rewrite.strengthen_entity_type_stored",
                                                     entity_name=entity_name, entity_type=discovered_type, uuid=entity_uuid)
                                        except Exception as e:
                                            log.debug("extract_rewrite.strengthen_entity_type_store_failed",
                                                     entity_name=entity_name, entity_type=discovered_type, error=str(e))
                            db_strengthen.commit()
                            log.info("extract_rewrite.strengthen_complete",
                                    types_stored=len([t for t in entity_types.values() if t and t != "SCALAR"]))
                        except Exception as e:
                            log.warning("extract_rewrite.strengthen_phase_failed", error=str(e))
                        finally:
                            if db_strengthen:
                                try:
                                    db_strengthen.close()
                                except Exception:
                                    pass
            except Exception as e:
                log.warning("extract_rewrite.gliner2_enrichment_failed", error=str(e))

        # dprompt-129: Validate triples against rel_types metadata (head_types/tail_types)
        # This gates novel rel_types and marks type mismatches as low-confidence
        db = None
        try:
            import psycopg2
            db = psycopg2.connect(os.getenv("POSTGRES_DSN"))
            triples = [_validate_triple_against_metadata(t, db) for t in triples]
            db.commit()
        except Exception as e:
            if db:
                try:
                    db.rollback()
                except Exception:
                    pass
            log.warning("extract.validation_skipped", error=str(e))
            # Continue without validation if DB unavailable (fallback to WGM gate)
        finally:
            if db:
                try:
                    db.close()
                except Exception:
                    pass

        # dprompt-126: Phase 1 — Normalize rel_type aliases with directionality preservation
        for triple in triples:
            rel_type_raw = (triple.get("rel_type") or "").lower().strip()
            if rel_type_raw:
                canonical, requires_inversion = _get_canonical_rel_type_with_directionality(rel_type_raw)

                if canonical and canonical != rel_type_raw:
                    triple["rel_type"] = canonical

                    # Apply inversion if this alias maps to a different direction
                    if requires_inversion:
                        original_subject = triple["subject"]
                        original_object = triple["object"]
                        triple["subject"] = original_object
                        triple["object"] = original_subject
                        log.info("extract.rel_type_inverted",
                                 original_rel_type=rel_type_raw, canonical=canonical,
                                 subject_before=original_subject, subject_after=original_object,
                                 user_id=req.user_id)

        # Mark extracted triples with openwebui provenance (from LLM extraction, not direct user statement)
        # dprompt-126: Preserve validation gates for extracted facts.
        # Only user-provided corrections (req.edges with is_correction=true) bypass validation.
        # LLM extractions need validation alicepite coming from user's message text.
        for triple in triples:
            # Use 'openwebui' to indicate these are extracted facts (subject to validation)
            # This allows validation gates to check type constraints and hierarchy membership
            triple["fact_provenance"] = "openwebui"

        # dprompt-128-P3: Track correction signal candidates for re_embedder learning
        # When is_correction=true, record in correction_signal_evaluations for eval/approval
        db = None
        try:
            import psycopg2
            db = psycopg2.connect(os.getenv("POSTGRES_DSN"))
            for triple in triples:
                if triple.get("is_correction"):
                    _track_correction_signal_candidate(req.user_id, req.text, triple, db)
            db.commit()
        except Exception as e:
            if db:
                try:
                    db.rollback()
                except Exception:
                    pass
            log.warning("extract.correction_signal_learning_failed", error=str(e))
            # Non-fatal: continue even if tracking fails
        finally:
            if db:
                try:
                    db.close()
                except Exception:
                    pass

        # dprompt-064 Phase 1: ✅ COMPLETE
        # Correction filtering now FULLY HANDLED by WGMValidationGate._apply_correction_semantics()
        # Extract returns ALL triples (dumb). Ingest applies semantics via rel_types.correction_behavior (smart).
        # Removed hardcoded negation filter — metadata-driven validation gate is authoritative.

        log.info("extract.rewrite_success", triple_count=len(triples), user_id=req.user_id)
        return {
            "status": "success",
            "edges": triples,
            "error": None,
        }

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log.error("extract.rewrite_failed", error=str(e), traceback=tb, user_id=req.user_id)
        return {
            "status": "error",
            "edges": [],
            "error": str(e),
        }


def _delete_from_qdrant(fact_ids: list[int], collection: str, qdrant_url: str) -> None:
    try:
        resp = _http_client_sync.delete(
            f"{qdrant_url}/collections/{collection}/points",
            json={"points": fact_ids},
            timeout=5.0,
        )
        if resp.status_code not in (200, 404):
            log.warning("qdrant.cleanup_partial", status=resp.status_code, count=len(fact_ids))
    except Exception as e:
        log.warning("qdrant.cleanup_failed", error=str(e), count=len(fact_ids))

def classify_fact_type(
    rel_type: str,
    object_value: str,
    registry,  # EntityRegistry instance
    user_id: str,
) -> dict:
    """
    Dynamically classify whether a fact's object is a scalar value or an entity
    reference. Replaces the hardcoded _SCALAR_REL_TYPES set.

    Layered heuristics, evaluated in order, first match wins:

        L0  same_as rel_type                   → relationship (semantic constant)
        L1  Integer / float / % / IP / currency / measurement / height  → scalar
        L2  ISO date / slash date / year / month+day                    → scalar
        L3  UUID pattern                                                → relationship
        L4  entity_aliases lookup (known name)                           → relationship
        L5  Email / URL / phone / long text / value-indicator / capital  → mixed
        L6  rel_types.tail_types from DB ontology                        → mixed
        L7  Fallback                                                    → uncertain

    Returns:
        {"type": "scalar" | "relationship" | "uncertain",
         "confidence": float (0.0–1.0),
         "reason": str}
    """
    import re

    stripped = object_value.strip()
    if not stripped:
        return {"type": "uncertain", "confidence": 0.0, "reason": "empty value"}

    stripped_lower = stripped.lower()
    rt_lower = rel_type.lower()

    # L0 — Semantic constant: same_as is owl:sameAs (entity identity, both UUIDs)
    if rt_lower == "same_as":
        return {"type": "relationship", "confidence": 1.0,
                "reason": "same_as is definitionally entity→entity"}

    # L1 — Numeric / technical patterns  (confidence ≥ 0.95)
    if re.match(r'^-?\d+$', stripped):
        return {"type": "scalar", "confidence": 0.98, "reason": "integer pattern"}
    if re.match(r'^-?\d+\.\d+$', stripped):
        return {"type": "scalar", "confidence": 0.98, "reason": "float pattern"}
    if re.match(r'^-?\d+(\.\d+)?%$', stripped):
        return {"type": "scalar", "confidence": 0.98, "reason": "percentage pattern"}
    if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', stripped):
        return {"type": "scalar", "confidence": 0.98, "reason": "IPv4 address pattern"}
    if re.match(r'^[\$\£\€\¥]\d{1,3}(,\d{3})*(\.\d{2})?$', stripped):
        return {"type": "scalar", "confidence": 0.98, "reason": "currency pattern"}
    if re.match(r'^\d+(\.\d+)?\s*(cm|kg|lbs?|pounds?|ft|feet|inch(?:es)?|'
                r'miles?|km|mph?|kph?|meters?|metres?|grams?|ounces?|oz)$',
                stripped_lower):
        return {"type": "scalar", "confidence": 0.95, "reason": "measurement with unit"}
    if re.match(r"^\d+\s*['\"]?\s*\d*\s*[\"]?\s*$", stripped):
        return {"type": "scalar", "confidence": 0.95, "reason": "height measurement (ft/in)"}

    # L2 — Date / time patterns  (confidence ≥ 0.85)
    if re.match(r'^\d{4}-\d{2}-\d{2}$', stripped):
        return {"type": "scalar", "confidence": 0.95, "reason": "ISO date (YYYY-MM-DD)"}
    if re.match(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?', stripped):
        return {"type": "scalar", "confidence": 0.95, "reason": "ISO datetime pattern"}
    if re.match(r'^\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}$', stripped):
        return {"type": "scalar", "confidence": 0.90, "reason": "slash-date pattern"}
    if re.match(r'^(19|20)\d{2}$', stripped):
        return {"type": "scalar", "confidence": 0.85, "reason": "year-only pattern"}
    _MONTH_RE = (r'(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|'
                 r'may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|'
                 r'oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)')
    if re.match(rf'^{_MONTH_RE}\s+\d{{1,2}}(?:st|nd|rd|th)?$', stripped_lower):
        return {"type": "scalar", "confidence": 0.90, "reason": "month-name + day pattern"}

    # L3 — UUID pattern  (definitive relationship)
    if _UUID_PATTERN.match(stripped):
        return {"type": "relationship", "confidence": 0.98, "reason": "UUID pattern"}

    # L4 — Entity alias lookup  (DB: entity_aliases)
    try:
        with registry.db_conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM entity_aliases WHERE user_id = %s AND alias = %s LIMIT 1",
                (user_id, stripped_lower),
            )
            if cur.fetchone():
                return {"type": "relationship", "confidence": 0.90, "reason": "known entity alias"}
    except Exception:
        pass

    # L5 — String-pattern heuristics
    word_count = len(stripped.split())
    if re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', stripped):
        return {"type": "scalar", "confidence": 0.98, "reason": "email pattern"}
    if stripped_lower.startswith(('http://', 'https://', 'www.', 'ftp://')):
        return {"type": "scalar", "confidence": 0.98, "reason": "URL pattern"}
    if re.match(r'^\+?[\d\s\-\(\)\.]{7,}$', stripped):
        return {"type": "scalar", "confidence": 0.90, "reason": "phone-number pattern"}
    if word_count >= 5:
        return {"type": "scalar", "confidence": 0.80, "reason": f"alicecriptive string ({word_count} words)"}
    _VALUE_INDICATORS = frozenset({
        "street", "road", "avenue", "lane", "drive", "boulevard",
        "court", "place", "highway", "circle", "square",
        "engineer", "doctor", "teacher", "student", "manager",
        "director", "president", "ceo", "cto", "cfo",
        "professor", "nurse", "lawyer", "artist", "writer",
        "consultant", "analyst", "developer", "aliceigner", "scientist",
    })
    if any(word in stripped_lower for word in _VALUE_INDICATORS):
        return {"type": "scalar", "confidence": 0.75, "reason": "value-indicator term detected"}
    if word_count == 1 and stripped[0].isupper():
        return {"type": "relationship", "confidence": 0.70, "reason": "single capitalized word (probable name)"}

    # L6 — DB-driven rel-type ontology hints  (tail_types from rel_types)
    if _rel_type_registry is not None:
        try:
            rt_meta = _rel_type_registry.get(rt_lower, {})
            tail_types = rt_meta.get("tail_types")
            if tail_types == ["SCALAR"]:
                return {"type": "scalar", "confidence": 0.90, "reason": "rel_type ontology: tail_types=SCALAR"}
            if (tail_types and tail_types != ["ANY"]
                    and "ANY" not in tail_types
                    and "SCALAR" not in tail_types):
                return {"type": "relationship", "confidence": 0.85,
                        "reason": f"rel_type ontology: tail_types={tail_types}"}
        except Exception:
            pass

    # L7 — Fallback
    return {"type": "uncertain", "confidence": 0.50,
            "reason": "ambiguous — no pattern or ontology hint matched"}


def _apply_correction(cur, user_id: str, old_value: str, new_value: str,
                      rel_type: str, new_fact_id: int, correction_behavior: str) -> int:
    if correction_behavior == "hard_delete":
        # DELETE stale alias facts BEFORE renaming subject (WHERE subject_id = old_value still matches)
        cur.execute(
            "DELETE FROM facts "
            "WHERE user_id = %s AND subject_id = %s AND id != %s AND rel_type = 'also_known_as'",
            (user_id, old_value, new_fact_id),
        )
        affected = cur.rowcount
        cur.execute(
            "UPDATE facts SET subject_id = %s, qdrant_synced = false "
            "WHERE user_id = %s AND subject_id = %s AND id != %s",
            (new_value, user_id, old_value, new_fact_id),
        )
        affected += cur.rowcount
        cur.execute(
            "UPDATE facts SET object_id = %s, qdrant_synced = false "
            "WHERE user_id = %s AND object_id = %s",
            (new_value, user_id, old_value),
        )
        affected += cur.rowcount
        return affected
    elif correction_behavior == "supersede":
        cur.execute(
            "UPDATE facts SET superseded_at = now(), qdrant_synced = false "
            "WHERE user_id = %s AND subject_id = %s AND rel_type = %s "
            "AND id != %s AND superseded_at IS NULL",
            (user_id, old_value, rel_type, new_fact_id),
        )
        return cur.rowcount
    else:  # immutable
        return 0


def _assess_statement_directness(edge, req_text: str, rel_type_metadata: dict) -> float:
    """
    Return extraction confidence unchanged. Fact classification is now determined by
    rel_type metadata (fact_class from rel_types table), not pattern matching on request text.

    This respects the metadata-driven architecture: all directionality/classification
    determinations come from the database, never from hardcoded patterns or heuristics.

    dprompt-147: User-stated facts bypass LLM confidence entirely.
    fact_provenance is the authoritative signal for user intent — when the text
    came from the user directly, confidence is 1.0 regardless of LLM self-assessment.
    """
    if hasattr(edge, 'fact_provenance') and edge.fact_provenance in ("openwebui", "user_stated", "user_correction"):
        return 1.0

    confidence = edge.confidence if hasattr(edge, 'confidence') else None
    return confidence if confidence is not None else 0.8


def _extract_prerequisites_from_text(
    entity_name: str,
    required_types: list,
    original_text: str,
    user_id: str,
    db,
    gliner_model
) -> list:
    """
    Auto-extract prerequisite facts when relationship validation fails due to missing type metadata.

    Pattern: User says "I have a dog named Buddy", but has_pet requires object_type='Animal'.
    Buddy extracted with type_descriptor='dog' but entity_type='unknown'.
    Solution: Extract "dog" from context, stage (Buddy, instance_of, dog) at Class B (0.8).

    Generic pattern scales to any rel_type with type constraints:
    - (hostname, has_ip_address, IP) needs (hostname, instance_of, computer_system)
    - (user, works_for, organization) needs (organization, instance_of, organization)

    Args:
        entity_name: Display name of the entity missing type info (e.g., "Buddy")
        required_types: List of allowed types from rel_type constraints (e.g., ['Animal'])
        original_text: Full user message where type context might be available
        user_id: User ID for entity registration
        db: Database connection
        gliner_model: GLiNER2 model for entity type extraction (optional)

    Returns:
        List of prerequisite facts as EdgeInput dicts (subject, rel_type, object, confidence)
    """
    if not original_text or not entity_name or not required_types:
        return []

    prerequisites = []
    try:
        # Find entity name in text with context window (80 chars on each side)
        entity_lower = entity_name.lower()
        text_lower = original_text.lower()
        start_idx = text_lower.find(entity_lower)

        if start_idx < 0:
            return []  # Entity not in text

        # Extract context window around entity
        context_start = max(0, start_idx - 80)
        context_end = min(len(original_text), start_idx + len(entity_name) + 80)
        context_text = original_text[context_start:context_end]

        extracted_types = set()

        # Try GLiNER2 if available
        if gliner_model:
            try:
                # FIXED: Provide required entity_types parameter
                default_entity_types = ["Person", "Animal", "Organization", "Location", "Object", "Concept"]
                entities = gliner_model.extract_entities(context_text, default_entity_types)
                if entities:
                    for ent_type, entity_names in entities.get("entities", {}).items():
                        if entity_names and ent_type and ent_type.lower() != "unknown":
                            extracted_types.add(ent_type.upper())
            except Exception as e:
                log.warning("ingest.gliner_extraction_failed", entity_name=entity_name, error=str(e))

        # Fallback: extract words around entity as potential types
        # Pattern: "entity_name is a TYPE" or "a TYPE named entity_name"
        if not extracted_types:
            import re
            # Look for patterns like "a dog named X", "X is a dog", "X the rabbit"
            patterns = [
                rf"(?:a|the|an)\s+(\w+)\s+named\s+{re.escape(entity_lower)}",  # "a dog named X"
                rf"{re.escape(entity_lower)}\s+(?:is\s+a\s+)?(?:the\s+)?(\w+)",  # "X is a dog" or "X the dog"
                rf"(?:your|my|our)?\s+(\w+)\s+named\s+{re.escape(entity_lower)}",  # "your hamster named X"
            ]
            for pattern in patterns:
                matches = re.findall(pattern, context_text, re.IGNORECASE)
                if matches:
                    extracted_types.update(m.lower() for m in matches if m.lower() not in ["the", "a", "an"])
                    break

        # If we found any types, create prerequisite fact
        if extracted_types:
            matched_type = list(extracted_types)[0]  # Take first extracted type
            prereq = EdgeInput(
                subject=entity_name,
                rel_type="instance_of",
                object=matched_type,
                confidence=0.8,  # Class B: type from immediate context
                subject_type="unknown",
                object_type="Concept",
            )
            prerequisites.append(prereq)
            log.info(
                "ingest.prerequisite_extracted",
                entity_name=entity_name,
                prerequisite_type=matched_type,
                required_types=required_types,
                extraction_method="gliner" if gliner_model else "regex",
                reason="auto-extracted from original text context"
            )

    except Exception as e:
        log.warning(
            "ingest.prerequisite_extraction_failed",
            entity_name=entity_name,
            error=str(e)
        )

    return prerequisites


@app.post("/ingest", response_model=IngestResponse)
async def ingest(req: IngestRequest, model=Depends(get_gliner_model)):
    """
    Ingest endpoint orchestrates the FULL write-validated knowledge graph pipeline:

    If no edges provided (raw text input):
      1. Call /extract for GLiNER2 entity typing (preflight)
      2. Call /extract/rewrite for LLM triple extraction (semantic inference)
      3. Validate through WGMValidationGate (ontological mapping)
      4. Classify as A/B/C
      5. Commit to PostgreSQL

    If edges provided (pre-extracted):
      - Skip to validation (useful for external extractors)

    CRITICAL: This endpoint owns the entire pipeline. Filter is dumb — it just sends text here.
    """
    # === IDEMPOTENCY CHECK: Check cache for duplicate OpenWebUI inlet calls ===
    # OpenWebUI may call inlet() multiple times per user message (streaming).
    # Use idempotency key to return cached response on duplicates.
    # This prevents duplicate extraction LLM invocations (dprompt-120).
    idempotency_key = None
    if _idempotency_mgr and req.text:
        idempotency_key = _idempotency_mgr.generate_key(req.text, req.user_id, "/ingest")
        cached_response = _idempotency_mgr.get_cached_response(idempotency_key)
        if cached_response:
            cached_committed = cached_response.get("committed", 0)
            cached_staged = cached_response.get("staged", 0)
            if cached_committed > 0 or cached_staged > 0:
                log.info("ingest.idempotency_cache_hit",
                        key=idempotency_key[:12],
                        user_id=req.user_id,
                        cached_committed=cached_committed)
                return IngestResponse(**cached_response)
            else:
                log.info("ingest.idempotency_cache_skipped_empty",
                        key=idempotency_key[:12],
                        user_id=req.user_id,
                        reason="previous attempt stored 0 facts, re-processing")
    # === END IDEMPOTENCY CHECK ===

    global _INGEST_EXTRACTION_CALL_COUNT
    _INGEST_EXTRACTION_CALL_COUNT += 1
    log.info("ingest.call_start", call_count=_INGEST_EXTRACTION_CALL_COUNT, user_id=req.user_id, has_text=bool(req.text), has_edges=bool(req.edges), text_len=len(req.text or ""))
    inferred_relations = []

    # Pre-initialize GLiNER cache so it's always available (populated by preflight if model runs)
    _gliner_cache = {}

    # dprompt-23: First-person pronoun set used by both the /extract/rewrite
    # normalizer and the req.edges normalizer below (dBug-023).
    _FIRST_PERSON_PRONOUNS = {"i", "me", "my", "myself", "we", "us", "our", "ourselves"}

    # dprompt-086: Third-person pronouns must be resolved by the LLM from conversation
    # context. If the LLM emits them literally, skip them — we cannot guess the referent.
    _THIRD_PERSON_PRONOUNS = {"it", "he", "she", "him", "her", "his", "they", "them", "hers", "its"}

    # If raw text provided and no edges, extract via LLM rewrite
    if not req.edges and req.text:
        try:
            import json

            # Call /extract/rewrite to get LLM-inferred triples
            qwen_url = _configured_llm_url()
            llm_model = os.getenv("WGM_LLM_MODEL", "qwen/qwen3.5-9b")

            # Get typed entities first via GLiNER2
            typed_entities = []
            _gliner_cache = {}  # Per-batch cache: entity_name → entity_type (dprompt-065 async)
            if model is not None:
                ner_result = None
                try:
                    ner_result = model.extract_entities(
                        req.text,
                        ["Person", "Animal", "Organization", "Location", "Object", "Concept"]
                    )
                    for entity_type, entity_names in ner_result.get("entities", {}).items():
                        if entity_names and isinstance(entity_names, list):
                            for name in entity_names:
                                if name and name.strip():
                                    typed_entities.append({
                                        "subject": name.strip(),
                                        "subject_type": entity_type.upper(),
                                        "object": "",
                                        "object_type": ""
                                    })
                except Exception as e:
                    log.warning("ingest.gliner2_failed", error=str(e))

                # dprompt-065: Cache GLiNER2 results with lowercase keys for per-entity reuse
                # Eliminates re-inference — subsequent edges look up cache instead of re-running model
                if ner_result:
                    for entity_type, entity_names in ner_result.get("entities", {}).items():
                        if entity_names and isinstance(entity_names, list):
                            for name in entity_names:
                                if name and name.strip():
                                    _gliner_cache[name.strip().lower()] = entity_type.upper()
                    log.debug("ingest.gliner_cache_populated", entries=len(_gliner_cache))

            # dprompt-145: GLiNER2 zero-shot relationship discovery with auto-create rel_type framework
            # GLiNER2 is a zero-shot model — does NOT require pre-seeded rel_types.
            # This enables self-growing ontology: novel relationships discovered, auto-created in DB,
            # reinforced by re_embedder when confirmed.
            gliner_edges = None
            pattern_edges = None
            if model is not None:
                try:
                    # CRITICAL: Zero-shot discovery — relation_types={} enables discovery of ANY relationship
                    # (not just those in rel_types table). GLiNER2 native output includes confidence scores.
                    gliner_relations = model.extract_relations(
                        req.text,
                        relation_types={},  # Zero-shot mode: empty dict discovers any relationships
                        threshold=0.5
                    )
                    log.debug("ingest.gliner2_raw_output",
                             relations=gliner_relations,
                             type=type(gliner_relations).__name__)

                    gliner_edges = _convert_gliner_relations_to_edges(gliner_relations)
                    log.debug("ingest.gliner2_conversion_result",
                             edges_count=len(gliner_edges) if gliner_edges else 0,
                             edges=gliner_edges)

                    if gliner_edges:
                        log.info("ingest.gliner2_relations_success",
                                relation_count=len(gliner_edges),
                                text_len=len(req.text))

                        # dprompt-149: CRITICAL FIX — Remove blocking sync DB calls from async ingest
                        # Line 5603 was doing psycopg2.connect() (synchronous!) inside async ingest function.
                        # This blocked the entire event loop, preventing HTTP requests from being sent,
                        # causing the apparent "hang in loop" timeout issue.
                        #
                        # Solution: Let re_embedder handle novel rel_type creation asynchronously.
                        # Novel rel_types discovered by GLiNER2 will be created on next re_embedder poll
                        # via ontology_evaluations table. This unblocks the event loop so /extract/rewrite
                        # can proceed immediately without hanging.

                        log.info("ingest.gliner2_edges_extracted",
                                relation_count=len(gliner_edges),
                                note="Novel rel_types will be auto-created by re_embedder (dprompt-149 fix)")

                        pattern_edges = gliner_edges
                        raw_inferred = [EdgeInput(**e) for e in gliner_edges]
                    else:
                        log.info("ingest.gliner2_empty_result",
                                text_preview=req.text[:100],
                                note="Falling back to LLM extraction for engine growth")
                except Exception as e:
                    log.warning("ingest.gliner2_relations_failed",
                               error=str(e),
                               error_type=type(e).__name__)
                    log.exception("ingest.gliner2_exception")

            # Call /extract/rewrite for LLM-based triple extraction (only if patterns and GLiNER2 didn't match)
            if not pattern_edges:
                faultline_url = os.getenv("FAULTLINE_API_URL", "http://localhost:8001")
                response = await _http_client.post(
                    f"{faultline_url}/extract/rewrite",
                    json={
                        "text": req.text,
                        "user_id": req.user_id,
                        "chat_id": req.chat_id if hasattr(req, 'chat_id') and req.chat_id else None,
                        "typed_entities": typed_entities if typed_entities else None,
                        "memory_facts": req.memory_facts if hasattr(req, 'memory_facts') and req.memory_facts else None,
                    },
                    timeout=30,
                )
            else:
                response = None

            if not pattern_edges and response and response.status_code == 200:
                rewrite_data = response.json()
                # Check if /extract/rewrite returned an error status
                if rewrite_data.get("status") == "error":
                    log.error("ingest.extraction_endpoint_error",
                             error=rewrite_data.get("error"),
                             user_id=req.user_id)
                    raw_inferred = []
                else:
                    # dprompt-23: Normalize first-person pronouns to "user" before entity resolution.
                    # Safety net — the /extract/rewrite prompt instructs the LLM to use "user",
                    # but LLMs may still output "I"/"me"/"my". Without this, registry.resolve()
                    # creates an orphaned UUID for the literal pronoun string (dBug-023).
                    # dprompt-23: First-person pronoun normalization (dBug-023)
                    for t in rewrite_data.get("edges", []):
                        subj = (t.get("subject") or "").lower().strip()
                        if subj in _FIRST_PERSON_PRONOUNS:
                            t["subject"] = "user"

                    # dprompt-98: Normalize inverse symmetric rel_types back to canonical form.
                    # LLM may output "spouse_of" thinking it's an inverse, but spouse is symmetric.
                    # Query metadata to check is_symmetric flag; convert inverted form to canonical.
                    for t in rewrite_data.get("edges", []):
                        rel_type_lower = (t.get("rel_type") or "").lower().strip()
                        # If rel_type ends with _of, check if the base rel_type is symmetric
                        if rel_type_lower.endswith("_of"):
                            base_rel = rel_type_lower[:-3]  # Remove _of suffix
                            base_meta = _REL_TYPE_META.get(base_rel.lower(), {})
                            # If base is symmetric, use canonical form (no _of)
                            if base_meta.get("is_symmetric"):
                                t["rel_type"] = base_rel
                                log.info("ingest.rel_type_normalized",
                                         original=rel_type_lower, normalized=base_rel,
                                         reason="symmetric rel_type has _of suffix removed")

                    # dprompt-125: Normalize LLM rel_type variations to canonical via DB aliases.
                    # Query rel_type_aliases table (seeded with Wikidata, extended by re_embedder).
                    # Prevents novel rel_types from being dropped as Class C.
                    for t in rewrite_data.get("edges", []):
                        rel_type_original = (t.get("rel_type") or "").lower().strip()
                        if rel_type_original:
                            canonical = _get_canonical_rel_type(rel_type_original)
                            if canonical != rel_type_original and canonical:
                                t["rel_type"] = canonical
                                log.info("ingest.rel_type_aliased",
                                         original=rel_type_original, canonical=canonical,
                                         reason="rel_type_aliases lookup")

                    # dprompt-086: Remove triples with unresolved third-person pronouns.
                    # The prompt instructs the LLM to resolve "it"/"he"/"she" from context;
                    # if the LLM emits them literally, we cannot guess the referent.
                    _triples_all = rewrite_data.get("edges", [])
                    _before = len(_triples_all)
                    _triples_all[:] = [
                        t for t in _triples_all
                        if (t.get("subject") or "").lower().strip() not in _THIRD_PERSON_PRONOUNS
                    ]
                    if len(_triples_all) < _before:
                        log.warning("ingest.third_person_pronoun_dropped",
                                    count=(_before - len(_triples_all)),
                                    text_snippet=req.text[:80])

                    raw_inferred = []
                    for t in rewrite_data.get("edges", []):
                        if not (t.get("subject") and t.get("object") and t.get("rel_type")):
                            continue

                        is_correction_flag = t.get("is_correction", False)
                        # User corrections are Class A: Set confidence=1.0 BEFORE gate validation
                        # This ensures WGMValidationGate bypasses type validation for user corrections
                        correction_confidence = 1.0 if is_correction_flag else None

                        edge = EdgeInput(
                            subject=t.get("subject", "").lower().strip(),
                            object=t.get("object", "").lower().strip(),
                            rel_type=t.get("rel_type", "").lower().strip(),
                            subject_type=t.get("subject_type"),
                            object_type=t.get("object_type"),
                            definition=t.get("definition"),
                            fact_provenance=t.get("fact_provenance", "llm_inferred"),
                            is_correction=is_correction_flag,
                            confidence=correction_confidence,  # dprompt-136: Set confidence before gate validation
                        )
                        raw_inferred.append(edge)
            else:
                if not pattern_edges:
                    raw_inferred = []
                    log.warning("ingest.rewrite_failed", status=response.status_code if response else 'unknown')

        except Exception as e:
            import traceback
            raw_inferred = []
            tb = traceback.format_exc()
            log.error("ingest.extraction_failed", error=str(e) or type(e).__name__, traceback=tb)

        # dprompt-086: Comprehensive pref_name injection — for every entity
        # mentioned during extraction, ensure a pref_name anchor exists (dBug-024).
        # This runs AFTER all extraction paths (pattern, GLiNER2, LLM) to ensure
        # pref_name facts are created regardless of which extraction method succeeded.
        # Entity names that are type/classification labels or pronouns, not real entities
        _ENTITY_TYPE_LABELS = {
            "person", "animal", "organization", "location", "object",
            "concept", "city", "state", "country", "address", "street",
            "province", "postal_code", "unknown", "entity",
            "computer", "server", "device", "laptop", "alicektop",
            "phone", "tablet", "router", "switch", "printer",
            "ip_address", "hostname", "fqdn", "domain_name", "subnet",
        }
        # Pronouns that should never become entity names
        _PRONOUN_STOPWORDS = _FIRST_PERSON_PRONOUNS | _THIRD_PERSON_PRONOUNS
        # Collect every unique entity name across all inferred edges (subjects and relational objects)
        _all_entity_names = set()
        for edge in raw_inferred:
            rel_type_lower = edge.rel_type.lower().strip()
            # Scalar rel_types have STRING values as objects (age, height, etc.) — skip collecting them as entities
            is_scalar_rel = rel_type_lower in _REL_TYPE_META and "SCALAR" in _REL_TYPE_META.get(rel_type_lower, {}).get("tail_types", [])

            for attr_name in ("subject", "object"):
                # Skip object collection for scalar rel_types (objects are values, not entities)
                if attr_name == "object" and is_scalar_rel:
                    continue
                _v = getattr(edge, attr_name, "").lower().strip() if hasattr(edge, attr_name) else ""
                if not _v:
                    continue
                if _v == "user":
                    continue
                if _UUID_PATTERN.match(_v):
                    continue
                if _v in _ENTITY_TYPE_LABELS:
                    continue
                if _v in _PRONOUN_STOPWORDS:
                    continue
                _all_entity_names.add(_v)

        # Collect entities that already have a pref_name edge in raw_inferred
        _existing_pref = {
            edge.subject.lower().strip()
            for edge in raw_inferred
            if edge.rel_type.lower().strip() == "pref_name"
        }

        # Inject missing pref_name anchors as EdgeInput objects
        for _name in sorted(_all_entity_names - _existing_pref):
            pref_edge = EdgeInput(
                subject=_name,
                object=_name,
                rel_type="pref_name",
                definition="identity: entity name anchor",
                fact_provenance="llm_inferred",  # pref_name anchors are inferred from entities
            )
            raw_inferred.append(pref_edge)
            log.info("ingest.pref_name_injected",
                     entity=_name, reason="entity mentioned but missing pref_name")

        # Build entity type map from GLiNER2 output for use in alias resolution
        # Only Person-type entities should have alias resolution applied
        _entity_types: dict[str, str] = {}
        if 'result' in locals():
            for fact in result.get("facts", []):
                subj = (fact.get("subject") or "").lower().strip()
                obj = (fact.get("object") or "").lower().strip()
                if subj and fact.get("subject_type"):
                    _entity_types[subj] = fact["subject_type"].lower()
                if obj and fact.get("object_type"):
                    _entity_types[obj] = fact["object_type"].lower()

        # Build a set of parent_of pairs from this batch for directionality validation
        batch_parent_of = {
            (e.object, e.subject)  # (child, parent) — flipped for lookup
            for e in raw_inferred
            if e.rel_type == "parent_of"
        }

        inferred_relations = []
        for edge in raw_inferred:
            if edge.rel_type == "child_of":
                # Only allow child_of(X, Y) if parent_of(Y, X) exists in this batch
                # i.e. (subject=X, object=Y) requires (X, Y) in batch_parent_of
                if (edge.subject, edge.object) not in batch_parent_of:
                    log.warning("ingest.child_of_rejected_no_parent",
                                subject=edge.subject, object=edge.object)
                    continue
            inferred_relations.append(edge)

    resolution = resolve_entities({"entities": []},
                                  context={"known_types": ["Person", "Organization", "Location"]})
    resolved = resolution["resolution"]["resolved"]

    edges_dict = {}
    for edge in (inferred_relations or []):
        key = (edge.subject, edge.object, edge.rel_type)
        edges_dict[key] = edge

    for edge in (req.edges or []):
        # dprompt-23: Normalize first-person pronoun subjects in externally-provided edges.
        # Belt-and-suspenders — same guard as the /extract/rewrite path above (dBug-023).
        subj = (edge.subject or "").lower().strip()
        if subj in _FIRST_PERSON_PRONOUNS:
            edge.subject = "user"
        # dprompt-086: Skip edges with unresolved third-person pronouns.
        if subj in _THIRD_PERSON_PRONOUNS:
            log.warning("ingest.edges_third_person_pronoun_skipped",
                        pronoun=subj, text_snippet=req.text[:80])
            continue
        key = (edge.subject, edge.object, edge.rel_type)
        edges_dict[key] = edge

    # Auto-synthesize identity and preference edges from text patterns.
    # These catch what GLiNER2/LLM miss: self-identification and explicit preferences.
    detected_identity = _extract_identity(req.text)
    detected_preferred = _extract_preferred_name(req.text)

    if detected_identity:
        # If a preferred name is ALSO stated and differs from the identity name,
        # the identity name is the formal/legal name (not preferred) and the
        # preferred name gets a pref_name edge marked preferred.
        has_explicit_pref = detected_preferred and detected_preferred != detected_identity
        identity_key = ("user", detected_identity, "also_known_as")
        if identity_key not in edges_dict:
            edges_dict[identity_key] = EdgeInput(
                subject="user",
                object=detected_identity,
                rel_type="also_known_as",
                is_preferred_label=not has_explicit_pref,  # only preferred if no explicit pref_name
                is_correction=False,
            )

    # Guard: if another named entity is mentioned with a preference signal
    # (e.g., "alicemonde prefers alice"), skip auto-synthesis for the user.
    # The LLM already extracted the correct entity assignment.
    _third_party_pref = re.compile(
        r'([A-Z][a-z]+)\s+(?:prefers?|goes\s+by|known\s+as|prefer[s]?\s+to\s+be\s+called)\s+([a-z]+)',
        re.IGNORECASE
    )
    _third_party_matches = {m.group(1).lower() for m in _third_party_pref.finditer(req.text)}
    _skip_user_pref = bool(_third_party_matches and _third_party_matches != {"user"})

    if detected_preferred and not _skip_user_pref:
        pref_key = ("user", detected_preferred, "pref_name")
        if pref_key not in edges_dict:
            edges_dict[pref_key] = EdgeInput(
                subject="user",
                object=detected_preferred,
                rel_type="pref_name",
                is_preferred_label=True,
                is_correction=False,
            )

    # dprompt-129: Correction detection via LLM is_correction metadata (filter.py),
    # not hardcoded regex patterns. The filter already marks corrections semantically.
    # is_correction flag is propagated through IngestRequest → edges_dict.
    # No regex-based "not X" pattern detection here (was dprompt-45, removed for universality).

    edges = list(edges_dict.values())



    facts, committed, staged, ingested = [], 0, 0, 0
    try:
            with psycopg2.connect(os.environ.get("POSTGRES_DSN")) as db:
                gate, manager = WGMValidationGate(db, _rel_type_registry), FactStoreManager(db)
                registry = EntityRegistry(db, auto_commit=False)  # ← Transaction managed by ingest
                rows = []
                has_preferred = _detect_preference_signal(req.text)
                preferred_objects = set()
                # Map canonical UUID → original display name for alias sync (Bug #3 fix)
                _canonical_to_display: dict[str, str] = {}

                # The canonical user entity ID is the OpenWebUI UUID (req.user_id)
                # Resolve to UUID surrogate for consistent storage (non-UUID user_ids produce deterministic UUIDs)
                if _UUID_PATTERN.match(req.user_id):
                    user_entity_id = req.user_id
                else:
                    from src.entity_registry.registry import _make_surrogate
                    user_entity_id = _make_surrogate(req.user_id, req.user_id)
                    log.info("ingest.user_id_surrogate", original=req.user_id, surrogate=user_entity_id)

                # Load all aliases for this user's UUID
                _user_aliases = set()
                try:
                    with db.cursor() as _cur:
                        _cur.execute(
                            "SELECT alias FROM entity_aliases WHERE user_id = %s AND entity_id = %s",
                            (req.user_id, user_entity_id),
                        )
                        _user_aliases.update(row[0] for row in _cur.fetchall())
                    if not _user_aliases:
                        log.warning("ingest.user_aliases_empty", user_id=req.user_id)
                        # Empty aliases OK for new user, continue
                    else:
                        log.info("ingest.user_aliases_loaded",
                                 count=len(_user_aliases), user_id=req.user_id)
                except psycopg2.Error as _e:
                    log.error("ingest.user_aliases_database_error",
                             user_id=req.user_id,
                             error_type=type(_e).__name__,
                             error=str(_e),
                             traceback=traceback.format_exc())
                    raise HTTPException(status_code=500, detail="Failed to load user identity")
                except Exception as _e:
                    log.error("ingest.user_aliases_unexpected_error",
                             user_id=req.user_id,
                             error_type=type(_e).__name__,
                             error=str(_e),
                             traceback=traceback.format_exc())
                    raise HTTPException(status_code=500, detail="Unexpected error loading user identity")

                for edge in edges:
                    # Skip truly self-referential facts (entity knows itself, etc.)
                    # but allow identity facts where subject == object is the norm
                    # e.g., (bob, pref_name, bob) — the entity IS its name.
                    if edge.subject == edge.object:
                        if edge.rel_type.lower() not in ("pref_name", "also_known_as"):
                            continue

                    # Age fact validation: reject non-numeric age objects (GLiNER2 false positives)
                    if edge.rel_type.lower() == "age":
                        _raw_obj = edge.object.strip()
                        if not re.match(r'^-?\d+$', _raw_obj):
                            log.warning("ingest.age_rejected_non_numeric_object",
                                        subject=edge.subject, object=_raw_obj,
                                        reason="age object must be numeric")
                            continue
                        if edge.subject.lower() == "user":
                            # dprompt-128: User's own scalar facts (age, height, etc.) only accepted if marked as correction by LLM.
                            # LLM learns correction patterns from correction_signals DB table — trust it.
                            is_correction = getattr(edge, "is_correction", False)
                            if not is_correction:
                                log.warning("ingest.scalar_for_user_rejected",
                                            subject=edge.subject, object=edge.object,
                                            rel_type=edge.rel_type, reason="user scalar fact requires is_correction=true",
                                            text=req.text[:100])
                                continue

                    # UUID guard: reject raw edge values that are UUIDs
                    # (canonical_ids may be UUIDs when entities exist without display names, which is fine)
                    if _UUID_PATTERN.match(edge.subject) or _UUID_PATTERN.match(edge.object):
                        log.warning("ingest.uuid_value_rejected",
                                    subject=edge.subject,
                                    object=edge.object,
                                    rel_type=edge.rel_type,
                                    reason="raw UUID in edge subject or object — likely resolution leak")
                        continue

                    # Capture raw values before entity resolution
                    _raw_subject = edge.subject
                    _raw_object = edge.object

                    # Resolve all entity names to canonical form via registry
                    # This ensures aliases (emma, ${USER}) never appear as subject/object in facts
                    try:
                        canonical_subject = registry.resolve(req.user_id, edge.subject)
                        log.info("ingest.subject_resolved_at_extraction",
                               input=edge.subject, output=canonical_subject, user_id=req.user_id)
                    except ValueError as _e:
                        log.error("ingest.subject_resolution_validation_error",
                                 entity=edge.subject,
                                 user_id=req.user_id,
                                 error=str(_e))
                        raise HTTPException(status_code=400, detail=f"Invalid entity name: {str(_e)}")
                    except psycopg2.Error as _e:
                        log.error("ingest.subject_resolution_database_error",
                                 entity=edge.subject,
                                 user_id=req.user_id,
                                 error_type=type(_e).__name__,
                                 error=str(_e),
                                 traceback=traceback.format_exc())
                        raise HTTPException(status_code=500, detail="Failed to resolve entity identity")
                    except Exception as _e:
                        log.error("ingest.subject_resolution_unexpected_error",
                                 entity=edge.subject,
                                 user_id=req.user_id,
                                 error_type=type(_e).__name__,
                                 error=str(_e),
                                 traceback=traceback.format_exc())
                        raise HTTPException(status_code=500, detail="Unexpected error resolving entity")

                    # For scalar rel_types, object is a string value (not an entity reference)
                    # Skip resolution and keep the raw string value
                    if _is_scalar_rel_type(edge.rel_type):
                        canonical_object = edge.object.lower().strip()
                        log.info("ingest.object_kept_as_scalar",
                               rel_type=edge.rel_type, object=canonical_object)
                    else:
                        # For relationship rel_types, resolve object to UUID
                        try:
                            canonical_object = registry.resolve(req.user_id, edge.object)
                            log.info("ingest.object_resolved_at_extraction",
                                   input=edge.object, output=canonical_object, user_id=req.user_id)
                        except ValueError as _e:
                            log.error("ingest.object_resolution_validation_error",
                                     entity=edge.object,
                                     user_id=req.user_id,
                                     error=str(_e))
                            raise HTTPException(status_code=400, detail=f"Invalid entity name: {str(_e)}")
                        except psycopg2.Error as _e:
                            log.error("ingest.object_resolution_database_error",
                                     entity=edge.object,
                                     user_id=req.user_id,
                                     error_type=type(_e).__name__,
                                     error=str(_e),
                                     traceback=traceback.format_exc())
                            raise HTTPException(status_code=500, detail="Failed to resolve entity identity")
                        except Exception as _e:
                            log.error("ingest.object_resolution_unexpected_error",
                                     entity=edge.object,
                                     user_id=req.user_id,
                                     error_type=type(_e).__name__,
                                     error=str(_e),
                                     traceback=traceback.format_exc())
                            raise HTTPException(status_code=500, detail="Unexpected error resolving entity")

                    # Record display name mapping for alias sync (Bug #3 fix)
                    # Only record for relationship facts where canonical_object is a UUID
                    if not _is_scalar_rel_type(edge.rel_type):
                        _canonical_to_display[canonical_object] = edge.object.lower()

                    # Persist entity types to entities table if provided (only if currently unknown)
                    if edge.subject_type and canonical_subject != user_entity_id:
                        try:
                            with db.cursor() as _cur:
                                _cur.execute(
                                    "UPDATE entities SET entity_type = %s"
                                    " WHERE id = %s AND user_id = %s AND entity_type = 'unknown'",
                                    (edge.subject_type.title(), canonical_subject, req.user_id),
                                )
                        except Exception as _e:
                            # INTENTIONAL: Type enrichment is optional. If update fails, fact is still processed.
                            log.info("ingest.subject_type_update_skipped",
                                    entity_id=canonical_subject, entity_type=edge.subject_type,
                                    error_type=type(_e).__name__,
                                    reason="Type metadata update failed; fact committed without type")

                    # Only update entity types for relationship facts (not scalar values)
                    if edge.object_type and not _is_scalar_rel_type(edge.rel_type) and canonical_object not in (user_entity_id, canonical_subject):
                        try:
                            with db.cursor() as _cur:
                                _cur.execute(
                                    "UPDATE entities SET entity_type = %s"
                                    " WHERE id = %s AND user_id = %s AND entity_type = 'unknown'",
                                    (edge.object_type.title(), canonical_object, req.user_id),
                                )
                        except Exception as _e:
                            # INTENTIONAL: Type enrichment is optional. If update fails, fact is still processed.
                            log.info("ingest.object_type_update_skipped",
                                    entity_id=canonical_object, entity_type=edge.object_type,
                                    error_type=type(_e).__name__,
                                    reason="Type metadata update failed; fact committed without type")

                    # Metadata-driven type validation: query rel_types for constraints, don't override extraction
                    # Trust extraction's semantic understanding; validate against DB constraints instead
                    rel_type_lower = edge.rel_type.lower()
                    rel_meta = _get_rel_type_metadata(rel_type_lower)
                    head_types = rel_meta.get("head_types", [])
                    tail_types = rel_meta.get("tail_types", [])

                    # Use extraction's types as-is (trust LLM semantic understanding)
                    final_subject_type = edge.subject_type
                    final_object_type = edge.object_type

                    # Validate against metadata constraints (log mismatches, don't override)
                    if final_subject_type and head_types and head_types != ["ANY"]:
                        if final_subject_type.lower() not in [t.lower() for t in head_types]:
                            log.warning("ingest.subject_type_constraint_mismatch",
                                       rel_type=rel_type_lower,
                                       extracted_type=final_subject_type,
                                       required_types=head_types,
                                       note="Type mismatch will be caught by WGMValidationGate")

                    if final_object_type and tail_types and tail_types != ["ANY"]:
                        if final_object_type.lower() not in [t.lower() for t in tail_types]:
                            log.warning("ingest.object_type_constraint_mismatch",
                                       rel_type=rel_type_lower,
                                       extracted_type=final_object_type,
                                       required_types=tail_types,
                                       note="Type mismatch will be caught by WGMValidationGate")

                    if final_subject_type and canonical_subject not in (user_entity_id, canonical_object):
                        try:
                            with db.cursor() as _cur:
                                _cur.execute(
                                    "UPDATE entities SET entity_type = %s"
                                    " WHERE id = %s AND user_id = %s AND entity_type = 'unknown'",
                                    (final_subject_type.title(), canonical_subject, req.user_id),
                                )
                        except Exception as _e:
                            # INTENTIONAL: Type enrichment is optional. If update fails, fact is still processed.
                            log.info("ingest.inferred_subject_type_update_skipped",
                                    entity_id=canonical_subject, entity_type=final_subject_type,
                                    error_type=type(_e).__name__,
                                    reason="Inferred type metadata update failed; fact committed without type")

                    if final_object_type and canonical_object not in (user_entity_id, canonical_subject):
                        try:
                            with db.cursor() as _cur:
                                _cur.execute(
                                    "UPDATE entities SET entity_type = %s"
                                    " WHERE id = %s AND user_id = %s AND entity_type = 'unknown'",
                                    (final_object_type.title(), canonical_object, req.user_id),
                                )
                        except Exception as _e:
                            # INTENTIONAL: Type enrichment is optional. If update fails, fact is still processed.
                            log.info("ingest.inferred_object_type_update_skipped",
                                    entity_id=canonical_object, entity_type=final_object_type,
                                    error_type=type(_e).__name__,
                                    reason="Inferred type metadata update failed; fact committed without type")

                    # Type inference for hierarchy facts (dprompt-127 strengthening + dprompt-127-Layer2-bidirectional)
                    # When is_hierarchy_rel=true (instance_of, subclass_of), infer subject's type from object
                    # Pattern: (whiskers, instance_of, cat) → look up cat's type, or infer from entity_taxonomies
                    # Metadata-driven: Uses entity_taxonomies to map entity names to types (e.g., "cat" → Animal)
                    # GROWTH LAYER: When object has misclassified type (Location for an animal name), correct it bidirectionally
                    if rel_meta.get("is_hierarchy_rel"):
                        try:
                            with db.cursor() as _cur:
                                # Step 1: Check if object entity already has a known type
                                _cur.execute(
                                    "SELECT entity_type, id FROM entities WHERE id = %s AND user_id = %s",
                                    (canonical_object, req.user_id)
                                )
                                obj_row = _cur.fetchone()
                                obj_type = obj_row[0] if obj_row and obj_row[0] and obj_row[0] != 'unknown' else None
                                obj_entity_id = obj_row[1] if obj_row else None

                                # Step 2: Get the object entity's display name for semantic analysis
                                obj_display_name = None
                                if obj_entity_id:
                                    _cur.execute(
                                        "SELECT alias FROM entity_aliases WHERE entity_id = %s AND is_preferred = true LIMIT 1",
                                        (obj_entity_id,)
                                    )
                                    alias_row = _cur.fetchone()
                                    obj_display_name = alias_row[0] if alias_row else edge.object

                                # Step 3: Infer type from entity_taxonomies based on hierarchy semantics + entity name confidence
                                # GROWTH LAYER: Only infer if entity name suggests the type (fuzzy semantic matching)
                                inferred_type_from_taxonomy = None
                                if rel_type_lower == 'instance_of' and obj_display_name:
                                    obj_name_lower = obj_display_name.lower()

                                    # Semantic patterns: animal breed/species keywords
                                    animal_keywords = {
                                        'dog', 'cat', 'morkie', 'poodle', 'terrier', 'labrador', 'retriever',
                                        'rabbit', 'hamster', 'guinea pig', 'bird', 'parrot', 'fish', 'puppy',
                                        'kitten', 'mix', 'breed', 'animal', 'pet', 'maltese', 'yorkie'
                                    }
                                    # Check if object name contains animal-related keywords (high confidence)
                                    if any(keyword in obj_name_lower for keyword in animal_keywords):
                                        inferred_type_from_taxonomy = 'Animal'
                                    else:
                                        # Secondary: check if it matches organizational/work patterns
                                        org_keywords = {'company', 'corporation', 'corp', 'firm', 'enterprise', 'inc', 'ltd', 'llc', 'organization'}
                                        if any(keyword in obj_name_lower for keyword in org_keywords):
                                            inferred_type_from_taxonomy = 'Organization'
                                        # Tertiary: location patterns
                                        elif any(keyword in obj_name_lower for keyword in {'city', 'county', 'state', 'region', 'country', 'town', 'village'}):
                                            inferred_type_from_taxonomy = 'Location'

                                # Step 4: Determine final object type to propagate
                                # Priority: existing correct type → inferred from semantics → unknown (don't change)
                                final_obj_type = None
                                correction_source = None
                                if obj_type and obj_type != 'unknown':
                                    # Object has a type. Only correct if we have HIGH confidence semantic inference
                                    # (e.g., Location→Animal because name contains "morkie", "poodle", etc.)
                                    if (obj_type == 'Location' and inferred_type_from_taxonomy and
                                        inferred_type_from_taxonomy != 'Location'):
                                        # GROWTH: Correct obvious misclassifications via semantic patterns
                                        final_obj_type = inferred_type_from_taxonomy
                                        correction_source = 'semantic_misclassification_correction'
                                        # Update the object entity's type
                                        _cur.execute(
                                            "UPDATE entities SET entity_type = %s WHERE id = %s AND user_id = %s",
                                            (final_obj_type, obj_entity_id, req.user_id)
                                        )
                                        log.info("ingest.hierarchy_type_correction_semantic",
                                                entity_id=obj_entity_id,
                                                old_type='Location',
                                                new_type=final_obj_type,
                                                rel_type=rel_type_lower,
                                                entity_name=obj_display_name,
                                                source='semantic_pattern_match')
                                    else:
                                        final_obj_type = obj_type
                                        correction_source = 'existing_type'
                                elif inferred_type_from_taxonomy:
                                    final_obj_type = inferred_type_from_taxonomy
                                    correction_source = 'semantic_inference'

                                # Step 5: Propagate object's type to subject (if subject is unknown or misclassified)
                                if final_obj_type and final_obj_type != 'unknown':
                                    # For hierarchy rels, propagate to subject if subject=unknown or if subject also misclassified
                                    _cur.execute(
                                        "SELECT entity_type FROM entities WHERE id = %s AND user_id = %s",
                                        (canonical_subject, req.user_id)
                                    )
                                    subj_row = _cur.fetchone()
                                    subj_type = subj_row[0] if subj_row else None

                                    should_update_subject = (subj_type == 'unknown' or
                                                           (subj_type == 'Location' and final_obj_type == 'Animal'))
                                    if should_update_subject:
                                        _cur.execute(
                                            "UPDATE entities SET entity_type = %s"
                                            " WHERE id = %s AND user_id = %s",
                                            (final_obj_type, canonical_subject, req.user_id)
                                        )
                                        rows_updated = _cur.rowcount
                                        if rows_updated > 0:
                                            log.info("ingest.hierarchy_type_propagation",
                                                    rel_type=rel_type_lower,
                                                    subject=canonical_subject,
                                                    inferred_type=final_obj_type,
                                                    source=correction_source)
                        except Exception as _e:
                            # INTENTIONAL: Type propagation is optional metadata enrichment.
                            # If it fails, hierarchy relationship is still committed.
                            log.info("ingest.hierarchy_type_propagation_skipped",
                                    rel_type=rel_type_lower,
                                    error_type=type(_e).__name__,
                                    reason="Type propagation failed; hierarchy fact committed without inferred type")

                    # SELF-HEALING: Look up entity types from cache → DB (no GLiNER2 re-inference)
                    # dprompt-065: Uses cached GLiNER2 results from initial pass; DB fallback only on miss
                    final_subject_type = edge.subject_type
                    final_object_type = edge.object_type

                    # Subject type: cache first, then DB (no new GLiNER2 call)
                    if not final_subject_type or final_subject_type.lower() == 'unknown':
                        subject_key = edge.subject.lower().strip()
                        if subject_key in _gliner_cache:
                            final_subject_type = _gliner_cache[subject_key]
                            log.info("ingest.subject_type_from_gliner_cache",
                                    entity=edge.subject, entity_type=final_subject_type)
                        else:
                            # Fall back to DB lookup only (no re-inference)
                            try:
                                with db.cursor() as _cur:
                                    _cur.execute(
                                        "SELECT entity_type FROM entities WHERE id = %s AND user_id = %s",
                                        (canonical_subject, req.user_id)
                                    )
                                    _row = _cur.fetchone()
                                    if _row and _row[0] and _row[0] != 'unknown':
                                        final_subject_type = _row[0]
                            except Exception as _e:
                                log.warning("ingest.subject_type_db_lookup_failed", error=str(_e))

                    # Object type: cache first, then DB (skip for scalar rel_types)
                    if (not final_object_type or final_object_type.lower() == 'unknown') and \
                       not _is_scalar_rel_type(edge.rel_type):
                        object_key = edge.object.lower().strip()
                        if object_key in _gliner_cache:
                            final_object_type = _gliner_cache[object_key]
                            log.info("ingest.object_type_from_gliner_cache",
                                    entity=edge.object, entity_type=final_object_type)
                        else:
                            # Fall back to DB lookup only (no re-inference)
                            try:
                                with db.cursor() as _cur:
                                    _cur.execute(
                                        "SELECT entity_type FROM entities WHERE id = %s AND user_id = %s",
                                        (canonical_object, req.user_id)
                                    )
                                    _row = _cur.fetchone()
                                    if _row and _row[0] and _row[0] != 'unknown':
                                        final_object_type = _row[0]
                            except Exception as _e:
                                log.warning("ingest.object_type_db_lookup_failed", error=str(_e))

                    # Normalize user-identity aliases to the canonical user UUID
                    if (canonical_subject.lower() in [a.lower() for a in _user_aliases] or canonical_subject == req.user_id) and canonical_subject != user_entity_id:
                        log.info("ingest.subject_normalized_to_user_id",
                                 original=canonical_subject, user_id=user_entity_id,
                                 matched_alias=canonical_subject.lower() in [a.lower() for a in _user_aliases])
                        canonical_subject = user_entity_id

                    # Similarly for object, but only for rel_types where user can be an object.
                    # Skip also_known_as and pref_name because those edges must preserve the alias as object.
                    # CRITICAL: Also skip scalar rel_types — scalar objects are STRING values (age, height, etc.)
                    # and must NEVER be converted to UUIDs (CLAUDE.md constraint: "Scalar rel_types have STRING objects").
                    # dBug-036A: Normalizing scalar values to UUID breaks conflict detection for user corrections.
                    if (canonical_object in _user_aliases and canonical_object != user_entity_id and
                        edge.rel_type.lower() not in ("also_known_as", "pref_name") and
                        not _is_scalar_rel_type(edge.rel_type)):
                        log.info("ingest.object_normalized_to_user_id",
                                 original=canonical_object, user_id=user_entity_id)
                        canonical_object = user_entity_id

                    # Track the actual subject to use for fact creation (may differ from canonical_subject
                    # if this is a correction where subject resolved to user's identity)
                    fact_subject = canonical_subject

                    # Register aliases from also_known_as and pref_name edges
                    is_pref = False  # default for non-identity edges

                    # CRITICAL: Skip self-referential aliases (where object == canonical subject)
                    # These are useless and pollute the alias registry
                    if edge.rel_type.lower() in ("also_known_as", "pref_name"):
                        # Skip if object resolves to the same entity as subject
                        if canonical_object == canonical_subject:
                            log.info("ingest.alias_skipped_self_referential",
                                    alias=edge.object, entity=canonical_subject, rel_type=edge.rel_type)
                            # Skip alias registration AND fact creation
                            continue

                        # pref_name edges are ALWAYS preferred — the rel_type itself is the signal.
                        # For also_known_as: only preferred if explicitly flagged or if the object
                        # was marked preferred by a pref_name edge in the same batch.
                        is_pref = (
                            edge.rel_type.lower() == "pref_name" or
                            edge.is_preferred_label or
                            edge.object.lower() in preferred_objects
                        )

                        # For corrections where subject is the user's canonical identity,
                        # find the entity we're actually aliasing (e.g., spouse, child)
                        alias_subject = canonical_subject
                        if (edge.is_correction and
                            alias_subject == registry.get_canonical_for_user(req.user_id)):
                            # Subject resolved to user identity. Look for related entities.
                            try:
                                with db.cursor() as _cur:
                                    # Find most recent also_known_as/pref_name fact for related entity
                                    _cur.execute(
                                        "SELECT subject_id FROM facts WHERE user_id = %s"
                                        " AND rel_type IN ('also_known_as', 'pref_name')"
                                        " AND subject_id != %s"
                                        " ORDER BY id DESC LIMIT 1",
                                        (req.user_id, alias_subject),
                                    )
                                    _row = _cur.fetchone()
                                    if _row:
                                        alias_subject = _row[0]
                                        fact_subject = alias_subject  # Use resolved subject for fact creation
                                        log.info("ingest.correction_subject_resolved",
                                                 original=canonical_subject, resolved=alias_subject,
                                                 rel_type=edge.rel_type)
                            except Exception as _e:
                                log.warning("ingest.correction_subject_resolution_failed", error=str(_e))

                        # Determine entity type for the alias (subject of identity rel)
                        # Use final_subject_type if available, otherwise infer from metadata
                        entity_type_for_alias = final_subject_type or 'unknown'
                        if entity_type_for_alias == 'unknown':
                            entity_type_for_alias = _infer_entity_type_from_rel_type(edge.rel_type, position='head')

                        registry.register_alias(
                            req.user_id,
                            alias_subject,
                            edge.object.lower(),
                            is_preferred=is_pref,
                            entity_type=entity_type_for_alias,
                        )
                        if is_pref and edge.rel_type.lower() == "pref_name":
                            preferred_objects.add(edge.object.lower())

                        # After a new user alias is registered, add it to in-memory set
                        # so subsequent edges in this batch are immediately normalized
                        if alias_subject == user_entity_id and edge.rel_type.lower() == "also_known_as":
                            _user_aliases.add(edge.object.lower())

                    # Skip self-referential after resolution
                    if fact_subject == canonical_object:
                        continue

                    # PHASE 1: 3D Classification (metadata-first, deterministic routing)
                    classification_3d = classify_fact_3d(
                        edge.rel_type.lower(), _raw_object.lower().strip(), registry, req.user_id)

                    # PHASE 2: Reassess confidence FIRST (before classification decisions)
                    # dprompt-140: User-stated = confidence >= 0.9 (not is_correction flag)
                    # This must come BEFORE the novel rel_type check so user-stated novel
                    # types get sync inference (authoritative) instead of async deferral.
                    adjusted_confidence = _assess_statement_directness(edge, req.text, _REL_TYPE_META)
                    is_user_stated = (adjusted_confidence or 0.0) >= 0.9

                    log.info("ingest.fact_provenance_check",
                           rel_type=edge.rel_type.lower(),
                           has_attr=hasattr(edge, 'fact_provenance'),
                           fact_provenance=getattr(edge, 'fact_provenance', 'MISSING'),
                           is_user_stated=is_user_stated,
                           adjusted_confidence=adjusted_confidence)

                    # dprompt-140: Conditional novel rel_type handling
                    # User-stated (confidence >= 0.9) → sync inference via gate (authoritative)
                    # LLM-inferred (confidence < 0.9)  → async deferred (preserves speed)
                    rel_type_lower = edge.rel_type.lower()
                    if rel_type_lower not in _REL_TYPE_META:  # Unknown rel_type
                        if is_user_stated:
                            # User-stated novel rel_type: gate will sync-infer metadata
                            # _try_approve_novel_type() called in validate_edge() for high confidence
                            log.info("ingest.user_stated_novel_type_sync_inference",
                                    rel_type=rel_type_lower,
                                    reason="user authority requires sync metadata inference")
                            # Metadata populated by gate validation, no staging needed
                        else:
                            # LLM-inferred novel rel_type: async staging
                            log.info("ingest.llm_inferred_novel_type_deferred_to_re_embedder",
                                    rel_type=rel_type_lower,
                                    reason="llm extraction deferred for async evaluation")
                            classification_3d["storage"] = "unknown_staging"

                    # Check if metadata was created in-flow
                    ontology_created = False
                    hierarchy_created = False

                    if adjusted_confidence != (edge.confidence if hasattr(edge, 'confidence') else 0.8):
                        log.info("ingest.confidence_reassess", rel_type=edge.rel_type.lower(),
                                 old_confidence=edge.confidence if hasattr(edge, 'confidence') else 0.8,
                                 new_confidence=adjusted_confidence, subject=edge.subject, object=edge.object)

                    fact_class, confidence = assign_class_and_confidence(
                        classification_3d,
                        is_user_stated,
                        ontology_created,
                        hierarchy_created,
                        rel_type=edge.rel_type,
                        confidence=adjusted_confidence,
                    )

                    log.info(
                        "ingest.fact_classified",
                        rel_type=edge.rel_type.lower(),
                        storage=classification_3d["storage"],
                        fact_class=fact_class,
                        confidence=confidence,
                        is_user_stated=is_user_stated,
                    )

                    # NOTE: For novel (unknown) rel_types with storage="unknown_staging",
                    # we override fact_class below using confidence-based routing (dprompt-148).
                    # This ensures user-stated facts get Class A (confidence >= 0.9) even with novel rel_types.

                    # ROUTE PATH 0: UNKNOWN REL_TYPES (stage based on confidence + user-stated status)
                    # dprompt-148: Respect adjusted_confidence for novel rel_type routing
                    # User-stated (confidence >= 0.9) → Class A (authoritative)
                    # LLM-inferred (0.7-0.9) → Class B (staged, promoted at 3x)
                    # LLM-inferred (< 0.7) → Class C (ephemeral, 30-day expiry)
                    if classification_3d["storage"] == "unknown_staging":
                        if is_user_stated and adjusted_confidence >= 0.9:
                            # User-stated novel rel_type: Class A
                            fact_class = "A"
                            confidence = adjusted_confidence  # Preserve user confidence
                            log.info("ingest.novel_rel_type_user_stated_class_a",
                                     rel_type=edge.rel_type.lower(),
                                     confidence=confidence,
                                     reason="user authority overrides novel rel_type")
                        elif adjusted_confidence >= 0.7:
                            # High-confidence LLM extraction: Class B
                            fact_class = "B"
                            confidence = adjusted_confidence
                            log.info("ingest.novel_rel_type_llm_high_confidence_class_b",
                                     rel_type=edge.rel_type.lower(),
                                     confidence=confidence)
                        else:
                            # Low-confidence LLM extraction: Class C
                            fact_class = "C"
                            confidence = max(adjusted_confidence, 0.4)
                            log.info("ingest.novel_rel_type_llm_low_confidence_class_c",
                                     rel_type=edge.rel_type.lower(),
                                     confidence=confidence)

                        # Record for ontology evaluation
                        try:
                            with db.cursor() as _cur:
                                _cur.execute(
                                    "INSERT INTO ontology_evaluations"
                                    " (user_id, candidate_rel_type, candidate_subject_type,"
                                    "  candidate_object_type, first_text_snippet,"
                                    "  extraction_confidence, extraction_method,"
                                    "  sample_subject_id, sample_object,"
                                    "  occurrence_count, last_seen_at)"
                                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, now())"
                                    " ON CONFLICT (user_id, candidate_rel_type, sample_subject_id, sample_object)"
                                    " DO UPDATE SET"
                                    "   occurrence_count = ontology_evaluations.occurrence_count + 1,"
                                    "   last_seen_at = now()",
                                    (req.user_id, edge.rel_type.lower(),
                                     edge.subject_type, edge.object_type,
                                     req.text[:500], 0.5, 'ingest',
                                     canonical_subject, _raw_object),
                                )
                        except Exception as _e:
                            log.warning("ingest.ontology_eval_insert_failed",
                                       rel_type=edge.rel_type.lower(), error=str(_e))

                        # Stage as Class C relational fact (re_embedder will evaluate)
                        rows.append((
                            req.user_id, canonical_subject, _raw_object,
                            edge.rel_type.lower(), req.source, False,
                            "C", confidence, True,
                            getattr(edge, 'definition', '') or ''
                        ))
                        log.info("ingest.unknown_rel_type_queued_for_evaluation",
                                 rel_type=edge.rel_type.lower(),
                                 subject=canonical_subject,
                                 object=_raw_object)
                        continue

                    # ROUTE PATH 1: SCALAR
                    if classification_3d["storage"] == "scalar":
                        # CRITICAL: Reject scalar facts with empty object values
                        # This prevents entity_attributes from being created with empty values
                        # Especially important for corrections where value must be present
                        if not _raw_object or not _raw_object.strip():
                            log.warning("ingest.scalar_rejected_empty_value",
                                       subject=canonical_subject,
                                       rel_type=edge.rel_type,
                                       is_correction=edge.is_correction,
                                       reason="scalar facts must have a non-empty value")
                            continue

                        # dBug-027: Validate pref_name must be name-like (not alicecriptive phrases)
                        if edge.rel_type.lower() == "pref_name":
                            words = _raw_object.split()
                            is_name_like = (
                                len(words) <= 2 and
                                all(len(w) > 0 for w in words)
                            )
                            if not is_name_like:
                                log.warning("ingest.pref_name_rejected_not_name_like",
                                            subject=canonical_subject,
                                            object=_raw_object,
                                            reason="pref_name must be 1-2 words, not alicecriptive phrase")
                                continue

                        val_text, val_int, val_float, val_date = _coerce_scalar(_raw_object.lower().strip())
                        # Only store if value is meaningful
                        if edge.rel_type.lower() == "age":
                            if val_int is None:
                                log.warning("ingest.scalar_rejected_non_numeric",
                                            entity=canonical_subject, value=canonical_object)
                                continue
                            # dprompt-36: entity-type-aware age validation
                            if val_int < 0:
                                log.warning("ingest.scalar_rejected_negative_age",
                                            entity=canonical_subject, age=val_int)
                                continue
                            # Person entities: strict 0-150 range
                            # Non-Person entities (Planet, Mountain, etc.): no upper limit
                            _entity_type = edge.subject_type or "unknown"
                            if _entity_type.lower() == "person" and val_int > 150:
                                log.info("ingest.person_age_rejected_out_of_range",
                                         entity=canonical_subject, age=val_int,
                                         raw_input=_raw_object)
                                continue

                        # ROBUST: Determine the TRUE subject by looking at relationship context.
                        # If the current subject is the user but the text indicates this attribute belongs
                        # to a related entity (child, spouse, pet, etc.), resolve to that entity instead.
                        actual_subject = canonical_subject

                        is_user_subject = (actual_subject == user_entity_id or actual_subject == "user")

                        if is_user_subject and len(req.text.split()) > 3:
                            # Generic relation pattern: "my [relation] [name] is [value]"
                            # Works for any relation: son, daughter, wife, mother, dog, etc.
                            relation_pattern = r'\bmy\s+(\w+)\s+([A-Z][a-z]+)\s+(?:is|was|has|turned)\s+(\d+(?:\.\d+)?)\s*(?:years?\s+old|ft|lbs?|lb)?'
                            match = re.search(relation_pattern, req.text)

                            if match:
                                relation_type = match.group(1)  # son, daughter, wife, dog, etc.
                                entity_name = match.group(2).lower()  # alice, Sophia, <pet>, etc.

                                # Resolve the mentioned entity to its canonical ID
                                resolved_entity = registry.resolve(req.user_id, entity_name)

                                # Verify this entity is actually related to the user via existing facts
                                if resolved_entity and resolved_entity != user_entity_id and resolved_entity != "user":
                                    try:
                                        with db.cursor() as _verify_cur:
                                            _verify_cur.execute(
                                                "SELECT 1 FROM facts "
                                                "WHERE user_id = %s AND superseded_at IS NULL "
                                                "AND ((subject_id = %s AND object_id = %s) "
                                                "OR (subject_id = %s AND object_id = %s)) "
                                                "LIMIT 1",
                                                (req.user_id, user_entity_id, resolved_entity,
                                                 resolved_entity, user_entity_id)
                                            )
                                            if _verify_cur.fetchone():
                                                actual_subject = resolved_entity
                                                log.info("ingest.scalar_redirected",
                                                         original=canonical_subject,
                                                         new=actual_subject,
                                                         relation=relation_type,
                                                         rel_type=edge.rel_type)
                                    except Exception as _verify_e:
                                        log.warning("ingest.scalar_relation_verification_failed",
                                                   error=str(_verify_e))

                        try:
                            # Ensure user entity exists in entities table
                            with db.cursor() as _cur:
                                _cur.execute(
                                    "INSERT INTO entities (id, user_id, entity_type)"
                                    " VALUES (%s, %s, %s)"
                                    " ON CONFLICT (id, user_id) DO NOTHING",
                                    (user_entity_id, req.user_id, "Person"),
                                )
                            _scalar_category = (
                                _get_rel_type_category(edge.rel_type)
                                or _infer_category(edge.rel_type.lower())
                            )
                            with db.cursor() as _cur:
                                _cur.execute(
                                    "INSERT INTO entity_attributes"
                                    " (user_id, entity_id, attribute, value_text, value_int,"
                                    "  value_float, value_date, provenance, sensitivity, category)"
                                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                                    " ON CONFLICT (user_id, entity_id, attribute)"
                                    " DO UPDATE SET"
                                    "   value_text = EXCLUDED.value_text,"
                                    "   value_int = EXCLUDED.value_int,"
                                    "   value_float = EXCLUDED.value_float,"
                                    "   value_date = EXCLUDED.value_date,"
                                    "   category = EXCLUDED.category,"
                                    "   updated_at = now()",
                                    (req.user_id, actual_subject, edge.rel_type.lower(),
                                     val_text, val_int, val_float, val_date, req.source,
                                     "private" if edge.rel_type.lower() in {
                                         "phone", "address", "email", "lives_at", "lives_in", "ip_address"
                                     } else "public", _scalar_category),
                                )
                            log.info("ingest.scalar_stored", entity=actual_subject, user_id=req.user_id,
                                     attribute=edge.rel_type, value_int=val_int, value_text=val_text,
                                     raw_input=_raw_object)
                        except Exception as _e:
                            log.warning("ingest.scalar_failed", error=str(_e))
                        continue  # Scalar facts stored in entity_attributes only, don't process as relationship

                    # PHASE 3: Enforce Directionality (asymmetric/symmetric/hierarchical)
                    canonical_subject, canonical_object, canonical_rel_type = enforce_directionality(
                        canonical_subject,
                        canonical_object,
                        edge.rel_type.lower(),
                        classification_3d.get("is_symmetric", False),
                        classification_3d.get("inverse_rel_type"),
                    )

                    # Commit entity type updates before validation
                    # Entity type strengthening (lines 4668-4743) updates entities.entity_type
                    # Must commit these before WGM validation runs, else validate_edge._resolve_entity_type()
                    # reads stale 'unknown' types from DB (dBug-extract-query-001)
                    db.commit()

                    # User corrections about themselves are axiomatically valid.
                    # The gate exists to filter inferred/external data — not to override
                    # explicit user intent. Bypass validation entirely for user self-corrections.
                    if fact_subject == user_entity_id and edge.is_correction:
                        status = "valid"
                    else:
                        validation = gate.validate_edge(
                            fact_subject, canonical_object, canonical_rel_type,
                            user_id=req.user_id,
                            subject_type=final_subject_type,
                            object_type=final_object_type,
                            confidence=edge.confidence,  # dprompt-136: Pass confidence for bypass check
                            is_correction=edge.is_correction,  # dprompt-136: Gate needs to know if this is a correction
                        )
                        status = validation.get("status")

                        # dprompt-126: Handle hierarchy membership violations
                        # Facts violating hierarchy membership are staged for re-embedder review
                        # User is source of truth, so violations don't reject facts, they stage them
                        hierarchy_violation = validation.get("hierarchy_violation")
                        if hierarchy_violation and status == "valid" and not req.edges:
                            # Non-user-provided facts with hierarchy violations are staged for review
                            # Re-embedder will evaluate and learn/correct classifications
                            fact_class = "C"  # Stage for re-embedder review instead of committing
                            log.info("ingest.hierarchy_violation_staged",
                                    rel_type=canonical_rel_type,
                                    subject_type=edge.subject_type,
                                    object_type=edge.object_type,
                                    reason=hierarchy_violation,
                                    note="Staged for re-embedder evaluation - classification mismatch")

                        # Handle type_mismatch: user-stated facts override type constraints.
                        # The user is authoritative about their own data. Only reject
                        # type mismatches from pure inference (no user edges provided).
                        if status == "type_mismatch":
                            if req.edges:
                                # User-provided edges bypass type constraints — user is source of truth
                                status = "valid"
                                log.info("ingest.type_mismatch_overridden",
                                         subject=fact_subject,
                                         rel_type=edge.rel_type,
                                         object=canonical_object,
                                         reason="user-provided edges override type constraints")
                            else:
                                # Generic prerequisite extraction (dprompt-127)
                                # When validation fails due to missing type metadata, auto-extract prerequisites
                                # Pattern: has_pet requires object_type='Animal' but Buddy has type='unknown'
                                # Solution: Extract type from text, stage prerequisite (Buddy, instance_of, dog)
                                validation_reason = validation.get("reason", "")

                                # Parse required type from error message
                                # Format: "object_type 'unknown' not allowed for 'rel_type' (allowed: ['Type1', 'Type2'])"
                                required_types = []
                                match = re.search(r"\(allowed: \[([^\]]+)\]\)", validation_reason)
                                if match:
                                    types_str = match.group(1)
                                    required_types = [
                                        t.strip().strip("'\"")
                                        for t in types_str.split(",")
                                    ]

                                prerequisites = _extract_prerequisites_from_text(
                                    entity_name=canonical_object,
                                    required_types=required_types,
                                    original_text=req.text or "",
                                    user_id=req.user_id,
                                    db=db,
                                    gliner_model=model
                                )

                                if prerequisites:
                                    # Stage prerequisites before re-validating original fact
                                    log.info("ingest.prerequisites_extracted",
                                            object_entity=canonical_object,
                                            prerequisite_count=len(prerequisites),
                                            rel_type=canonical_rel_type)

                                    for prereq in prerequisites:
                                        try:
                                            # Register prerequisite entity
                                            registry = EntityRegistry(dsn=os.environ.get("POSTGRES_DSN", ""))
                                            prereq_subject_id = registry.resolve(
                                                canonical_object, req.user_id, type_hint=None
                                            )
                                            prereq_object_id = registry.resolve(
                                                prereq.object, req.user_id, type_hint=prereq.object_type
                                            )

                                            # Stage prerequisite as Class B (0.8 confidence)
                                            with db.cursor() as _cur:
                                                _cur.execute(
                                                    "INSERT INTO staged_facts"
                                                    " (user_id, subject_id, object_id, rel_type,"
                                                    "  fact_class, provenance, confidence, first_seen_at)"
                                                    " VALUES (%s, %s, %s, %s, %s, %s, %s, now())"
                                                    " ON CONFLICT (user_id, subject_id, object_id, rel_type)"
                                                    " DO UPDATE SET last_seen_at = now(),"
                                                    "   confirmed_count = staged_facts.confirmed_count + 1",
                                                    (
                                                        req.user_id,
                                                        prereq_subject_id,
                                                        prereq_object_id,
                                                        prereq.rel_type.lower(),
                                                        "B",
                                                        "ingest_prerequisite_extraction",
                                                        0.8,
                                                    )
                                                )

                                            log.info("ingest.prerequisite_staged",
                                                    subject=canonical_object,
                                                    rel_type="instance_of",
                                                    object=prereq.object,
                                                    confidence=0.8)

                                        except Exception as e:
                                            log.warning("ingest.prerequisite_staging_failed",
                                                       error=str(e))

                                    # Re-validate original fact after prerequisites staged
                                    validation = gate.validate_edge(
                                        fact_subject, canonical_object, canonical_rel_type,
                                        user_id=req.user_id,
                                        subject_type=edge.subject_type,
                                        object_type=edge.object_type,
                                    )
                                    status = validation.get("status")

                                    if status == "valid":
                                        log.info("ingest.type_mismatch_resolved",
                                                subject=fact_subject,
                                                rel_type=canonical_rel_type,
                                                object=canonical_object,
                                                reason="prerequisites satisfied")
                                    else:
                                        # dprompt-growth: type_mismatch facts go to short-term memory (Class C + Qdrant).
                                        # Store everything, classify later — even unknown-type facts are valuable
                                        # for retrieval and will be strengthened by the re-embedder over time.
                                        log.warning("ingest.type_mismatch_after_prerequisites",
                                                   subject=fact_subject,
                                                   rel_type=canonical_rel_type,
                                                   object=canonical_object,
                                                   reason=validation.get("reason", ""))
                                        # Stage as Class C: short-term memory, 30-day expiry, Qdrant-synced
                                        _commit_staged(db, [(
                                            req.user_id, fact_subject, canonical_object,
                                            canonical_rel_type, "ingest_type_mismatch",
                                            "", "relational", False, []
                                        )], "C", confidence=0.4)
                                        log.info("ingest.type_mismatch_staged_class_c",
                                                subject=fact_subject, rel_type=canonical_rel_type,
                                                object=canonical_object)

                                else:
                                    log.warning("ingest.type_mismatch",
                                               subject=fact_subject,
                                               rel_type=edge.rel_type,
                                               object=canonical_object,
                                               reason=validation.get("reason", ""))
                                    # dprompt-growth: Store everything, classify later.
                                    # Route type_mismatch facts to Class C short-term memory.
                                    _commit_staged(db, [(
                                        req.user_id, fact_subject, canonical_object,
                                        canonical_rel_type, "ingest_type_mismatch",
                                        "", "relational", False, []
                                    )], "C", confidence=0.4)
                                    log.info("ingest.type_mismatch_staged_class_c",
                                            subject=fact_subject, rel_type=canonical_rel_type,
                                            object=canonical_object)

                    # dBug-027: Validate rel_type constraints from metadata (dprompt-97)
                    # Only check after WGMValidationGate passes; skip for scalars (handled separately)
                    if status == "valid" and classification_3d.get("storage") != "scalar":
                        rel_type_meta = _REL_TYPE_META.get(canonical_rel_type.lower(), {})
                        is_constraint_valid, constraint_reason = _validate_rel_type_constraints(
                            {
                                "rel_type": canonical_rel_type.lower(),
                                "subject_type": edge.subject_type,
                                "object_type": edge.object_type,
                                "object": canonical_object,
                                "object_id": canonical_object if _UUID_PATTERN.match(canonical_object) else None,
                            },
                            rel_type_meta,
                            db
                        )
                        if not is_constraint_valid:
                            log.warning("ingest.rel_type_constraint_rejected",
                                        rel_type=canonical_rel_type,
                                        subject=fact_subject,
                                        object=canonical_object,
                                        reason=constraint_reason)
                            continue

                    is_engine_generated = False  # default

                    # Handle unapproved novel rel_type: store as Class C and record for async evaluation.
                    # dprompt-130: Novel rel_types approved synchronously now return "valid" or "novel_unapproved"
                    # "unknown" is legacy; "novel_unapproved" means LLM confidence < 0.7
                    # The re-embedder will evaluate usage patterns and decide to approve/map/reject.
                    if status in ("unknown", "novel_unapproved"):
                        is_engine_generated = True  # force Class C
                        try:
                            with db.cursor() as _cur:
                                _cur.execute(
                                    "INSERT INTO ontology_evaluations"
                                    " (user_id, candidate_rel_type, candidate_subject_type,"
                                    "  candidate_object_type, first_text_snippet,"
                                    "  extraction_confidence, extraction_method,"
                                    "  sample_subject_id, sample_object,"
                                    "  occurrence_count, last_seen_at)"
                                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, now())"
                                    " ON CONFLICT (user_id, candidate_rel_type, sample_subject_id, sample_object)"
                                    " DO UPDATE SET"
                                    "   occurrence_count = ontology_evaluations.occurrence_count + 1,"
                                    "   last_seen_at = now()",
                                    (req.user_id, edge.rel_type.lower(),
                                     edge.subject_type, edge.object_type,
                                     req.text[:500], 0.5, 'ingest',
                                     fact_subject, canonical_object),
                                )
                            log.info("ingest.unknown_rel_type_recorded",
                                     rel_type=edge.rel_type.lower(),
                                     subject_type=edge.subject_type,
                                     object_type=edge.object_type)
                        except Exception as _e:
                            # INTENTIONAL: Graceful degradation — ontology evaluation is async learning.
                            # If recording fails, fact is still processed and committed.
                            log.warning("ingest.ontology_eval_insert_failed",
                                       rel_type=edge.rel_type.lower(),
                                       error_type=type(_e).__name__,
                                       error=str(_e),
                                       note="Fact committed; ontology evaluation will be retried in re_embedder")

                    # Look up whether this rel_type is engine_generated
                    if not is_engine_generated:
                        is_engine_generated = False
                        if hasattr(_rel_type_registry, 'get') and _rel_type_registry:
                            rt_meta = _rel_type_registry.get(edge.rel_type.lower(), {})
                            is_engine_generated = rt_meta.get("engine_generated", False)

                    # dprompt-136: Use pre-set confidence (from correction flag) or compute based on provenance
                    edge_confidence = edge.confidence if edge.confidence is not None else (
                        1.0 if edge.is_correction else (
                            0.8 if edge.fact_provenance == "user_stated" else 0.6
                        )
                    )

                    # GROWTH ARCH: Infer missing entity types from rel_type constraints
                    # If rel_type requires Person subjects/objects, and edge didn't specify type,
                    # infer the type from metadata to prevent validation gate rejections.
                    rt_lower = edge.rel_type.lower()
                    rt_meta = _get_rel_type_metadata(rt_lower)
                    head_types = rt_meta.get("head_types", [])
                    tail_types = rt_meta.get("tail_types", [])

                    # Infer subject_type if not provided and rel_type constrains it
                    if not edge.subject_type and head_types and head_types != ["ANY"]:
                        # Use first constraint as inferred type (e.g., spouse requires Person)
                        inferred_subject_type = head_types[0] if isinstance(head_types, list) else head_types
                        if inferred_subject_type and inferred_subject_type != "SCALAR":
                            edge.subject_type = inferred_subject_type
                            log.info("ingest.subject_type_inferred_from_metadata",
                                    rel_type=rt_lower, inferred_type=inferred_subject_type)

                    # Infer object_type if not provided, rel_type constrains it, and it's not a scalar
                    if not edge.object_type and tail_types and tail_types != ["ANY", "SCALAR"]:
                        inferred_object_type = tail_types[0] if isinstance(tail_types, list) else tail_types
                        if inferred_object_type and inferred_object_type != "SCALAR":
                            edge.object_type = inferred_object_type
                            log.info("ingest.object_type_inferred_from_metadata",
                                    rel_type=rt_lower, inferred_type=inferred_object_type)

                    # Metadata-driven routing (dprompt-73b): query storage_target
                    # from rel_types table instead of hardcoded frozensets.
                    rt_lower = edge.rel_type.lower().strip()
                    route_meta = _get_rel_type_metadata(rt_lower)
                    storage_target = route_meta.get("storage_target", "facts")

                    if storage_target == "events":
                        with db.cursor() as cur:
                            cur.execute(
                                "INSERT INTO events"
                                " (user_id, subject_id, object_id, event_type, occurs_on, recurrence, confidence)"
                                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                                " ON CONFLICT (user_id, subject_id, event_type)"
                                " DO UPDATE SET"
                                "   object_id = EXCLUDED.object_id,"
                                "   occurs_on = EXCLUDED.occurs_on,"
                                "   confidence = EXCLUDED.confidence,"
                                "   recurrence = COALESCE(events.recurrence, EXCLUDED.recurrence)",
                                (
                                    req.user_id,
                                    _raw_subject.lower().strip(),
                                    _raw_object.lower().strip(),
                                    rt_lower,
                                    _raw_object.lower().strip(),
                                    _EVENT_RECURRENCE_DEFAULTS.get(rt_lower),
                                    edge_confidence,
                                ),
                            )
                        log.info("ingest.event_stored",
                                 event_type=rt_lower,
                                 subject=_raw_subject.lower().strip(),
                                 occurs_on=_raw_object.lower().strip())
                        ingested += 1
                        # No continue — events also flow through fact classification below

                    # Use fact_class from 3D model (Phase 2) — already computed above
                    # Skip old fact_class assignment to preserve 3D routing determinism
                    # is_engine_generated already set correctly by original logic above.

                    facts.append(FactResult(
                        subject=fact_subject,
                        object=canonical_object,
                        rel_type=canonical_rel_type,
                        status=status,
                        fact_class=fact_class,
                        provenance=edge.fact_provenance,
                    ))
                    if status in ("valid", "conflict", "unknown", "novel_unapproved"):
                        # Conflict facts: the WGM gate already inserted the new fact and marked
                        # old facts as contradicted. We still need rows populated for downstream
                        # processing (entity alias sync, Qdrant sync, preference propagation).
                        # Novel rel_type status (unknown or novel_unapproved): dprompt-130 enables synchronous
                        # LLM inference in WGMValidationGate.validate_edge(), which approves high-confidence
                        # rel_types immediately (status="valid") or routes low-confidence to Class C (novel_unapproved).
                        # Both are recorded in ontology_evaluations for re-embedder strengthening/correction.
                        # Use the is_pref value computed earlier (which already accounts for
                        # pref_name semantics, explicit flags, and cross-batch preference objects).
                        rows.append((
                            req.user_id, fact_subject, canonical_object,
                            canonical_rel_type, req.source, is_pref,
                            fact_class, confidence, is_engine_generated,
                            getattr(edge, 'definition', '') or '',
                            classification_3d.get("storage", "unknown_staging"),
                            classification_3d.get("is_hierarchy_rel", False),
                            classification_3d.get("taxonomies", [])
                        ))

                # Apply taxonomy rules to annotate facts with grouping context
                if rows:
                    # DEBUG: Log rows before taxonomy processing
                    for i, row in enumerate(rows[:3]):  # Log first 3 rows
                        log.info("ingest.rows_before_taxonomy",
                               row_idx=i, user_id=row[0], subject=row[1], obj=row[2],
                               rel_type=row[3], fact_class=row[6])
                if rows:
                    # CRITICAL: Filter out scalar rel_types from rows — they MUST ONLY be stored in entity_attributes
                    # Metadata-driven: check rel_types.tail_types={SCALAR} to determine storage path
                    # Scalar facts routed via Phase 1 classification should never reach rows.append(), but add guard here
                    # to prevent accidental double-storage in facts table (dprompt-96 Phase 4 routing guarantee)
                    def is_scalar_rel_type(rt_lower: str) -> bool:
                        """Check if rel_type has SCALAR storage via metadata."""
                        if not rt_lower:
                            return False
                        rt_meta = _REL_TYPE_META.get(rt_lower, {})
                        tail_types = rt_meta.get("tail_types", [])
                        return "SCALAR" in tail_types

                    scalar_count_filtered = 0
                    rows_before = len(rows)
                    rows = [row for row in rows if not is_scalar_rel_type(row[3].lower() if row[3] else '')]
                    scalar_count_filtered = rows_before - len(rows)
                    if scalar_count_filtered > 0:
                        log.warning("ingest.scalar_rels_filtered_from_rows",
                                   count=scalar_count_filtered,
                                   reason="scalar_rels (tail_types=SCALAR) stored in entity_attributes, not facts")

                    # Filter rows to ensure entity_ids are valid (UUIDs or user_id itself)
                    # Validates that only user_id and UUID v5 surrogates appear in facts
                    # (prevents arbitrary string entity_ids from contaminating DB)
                    validated_rows = []
                    for row in rows:
                        user_id, subject, obj, rel_type, source, is_preferred, fact_class, confidence, is_engine_generated = row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8]
                        definition = row[9] if len(row) > 9 else ''
                        rt_lower = rel_type.lower() if rel_type else ''

                        # If subject is a string (not UUID), try to resolve it
                        if subject and not _UUID_PATTERN.match(subject) and subject != user_id:
                            try:
                                subject = registry.resolve(user_id, subject)
                                log.info("ingest.subject_resolved_during_validation",
                                       original=row[1], resolved=subject, rel_type=rel_type)
                            except Exception as _e:
                                log.error("ingest.subject_resolution_failed_validation",
                                        original=row[1], error=str(_e), rel_type=rel_type)
                                continue

                        # If object is a string, only resolve if rel_type expects UUID objects
                        # Skip resolution for scalar rel_types (pref_name, age, etc.)
                        if obj and not _UUID_PATTERN.match(obj) and obj != user_id:
                            if not _is_scalar_rel_type(rel_type):
                                try:
                                    obj = registry.resolve(user_id, obj)
                                    log.info("ingest.object_resolved_during_validation",
                                           original=row[2], resolved=obj, rel_type=rel_type)
                                except Exception as _e:
                                    log.error("ingest.object_resolution_failed_validation",
                                            original=row[2], error=str(_e), rel_type=rel_type)
                                    continue

                        # Now validate: subject must be UUID or user_id
                        is_valid_subject = (
                            subject == user_id or
                            _UUID_PATTERN.match(subject)
                        )
                        if not is_valid_subject:
                            log.error("ingest.invalid_subject_id",
                                      subject=subject, rel_type=rel_type, user_id=user_id,
                                      reason="subject_id must be UUID or user_id")
                            continue  # Skip this fact

                        # Object validation depends on rel_type:
                        # - For scalar rel_types: object can be ANY string (value)
                        # - For relationship rel_types: object must be UUID or user_id
                        if _is_scalar_rel_type(rt_lower):
                            # Scalar rel_type: object can be any non-empty string
                            if not obj or not obj.strip():
                                log.error("ingest.invalid_object_scalar",
                                          obj=obj, rel_type=rel_type, user_id=user_id,
                                          reason="object value cannot be empty for scalar rel_type")
                                continue
                            # CRITICAL: pref_name and also_known_as must NEVER have UUID objects
                            # A UUID as a display name is meaningless and breaks display resolution
                            if rt_lower in ("pref_name", "also_known_as") and _UUID_PATTERN.match(obj):
                                log.error("ingest.invalid_identity_rel_uuid_object",
                                          rel_type=rel_type, object=obj, subject=subject,
                                          reason=f"{rel_type} object must be a display name string, not a UUID")
                                continue
                        else:
                            # Relationship rel_type: object must be UUID or user_id
                            is_valid_object = (
                                obj == user_id or
                                _UUID_PATTERN.match(obj)
                            )
                            if not is_valid_object:
                                log.error("ingest.invalid_object_id",
                                          obj=obj, rel_type=rel_type, user_id=user_id,
                                          reason="object_id must be UUID or user_id for relationship rel_type")
                                continue  # Skip this fact

                        # Update row with resolved subject/object if they changed
                        updated_row = (user_id, subject, obj, rel_type, source, is_preferred, fact_class, confidence, is_engine_generated, definition)
                        validated_rows.append(updated_row)

                    rows = validated_rows

                    # ── dprompt-59: Semantic conflict detection ──────────────────
                    # Before committing, check each fact against existing graph structure.
                    # If X instance_of Y exists, don't allow owns/has_pet/works_for on Y.
                    conflict_free_rows = []
                    conflict_count = 0
                    for row in rows:
                        _uid, _subj, _obj, _rel, _src, _pref, _fclass, _conf, _eng = row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8]
                        decision, reason = _detect_semantic_conflicts(
                            db, req.user_id, _subj, _rel, _obj,
                        )
                        if decision == "supersede_new":
                            log.info("ingest.conflict_superseded",
                                     rel_type=_rel, subject=_subj, object=_obj, reason=reason)
                            conflict_count += 1
                            continue  # skip this fact — it's semantically invalid
                        conflict_free_rows.append(row)

                    if conflict_count > 0:
                        log.info("ingest.conflicts_resolved", count=conflict_count,
                                 user_id=req.user_id)

                    rows = conflict_free_rows
                    # ── end dprompt-59 ──────────────────────────────────────────

                    # ── dprompt-62: Bidirectional validation ─────────────────────
                    # Prevent impossible bidirectional relationships (child_of + parent_of
                    # for same pair). Keep higher confidence, supersede lower.
                    _bidir_rows = []
                    _bidir_count = 0
                    for row in rows:
                        _uid, _subj, _obj, _rel, _src, _pref, _fclass, _conf, _eng = row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8]
                        _defn = row[9] if len(row) > 9 else ''
                        bidir_decision = _validate_bidirectional_relationships(
                            db, req.user_id, _subj, _rel, _obj, _conf,
                        )
                        if bidir_decision == "create_inverse":
                            # Auto-create missing inverse fact with same metadata
                            _meta = _get_rel_type_metadata(_rel)
                            _inv_rel = _meta.get("inverse_rel_type") if _meta else None
                            if _inv_rel:
                                _bidir_rows.append((
                                    _uid, _obj, _subj, _inv_rel, f"auto-created inverse of {_src}",
                                    False, _fclass, _conf, _eng, _defn
                                ))
                                log.info("ingest.bidirectional_inverse_created",
                                         rel_type=_rel, inverse=_inv_rel,
                                         subject=_subj, object=_obj, confidence=_conf)
                            _bidir_rows.append(row)
                            continue
                        elif bidir_decision == "supersede_new":
                            log.info("ingest.bidirectional_superseded",
                                     rel_type=_rel, subject=_subj, object=_obj,
                                     confidence=_conf)
                            _bidir_count += 1
                            continue
                        _bidir_rows.append(row)

                    if _bidir_count > 0:
                        log.info("ingest.bidirectional_resolved", count=_bidir_count,
                                 user_id=req.user_id)

                    rows = _bidir_rows
                    # ── end dprompt-62 ──────────────────────────────────────────

                    # Split rows by fact class — surrogates go directly to commit, no display name resolution
                    # Display names are resolved at READ time only (_resolve_display_names in /query)
                    class_a_rows = []
                    class_b_rows = []
                    class_c_rows = []

                    for row in rows:
                        user_id = row[0]
                        subject = row[1]
                        obj = row[2]
                        rel_type = row[3]
                        source = row[4]
                        is_preferred = row[5]
                        fact_class = row[6]
                        is_engine_generated = row[8]
                        defn = row[9] or ''
                        storage_type = row[10] if len(row) > 10 else "unknown_staging"
                        is_hierarchy_rel = row[11] if len(row) > 11 else False
                        taxonomies = row[12] if len(row) > 12 else []

                        if fact_class == "A":
                            class_a_rows.append((user_id, subject, obj, rel_type, source, is_preferred, defn, storage_type, is_hierarchy_rel, taxonomies))
                        elif fact_class == "B":
                            class_b_rows.append((user_id, subject, obj, rel_type, source, defn, storage_type, is_hierarchy_rel, taxonomies))
                        else:
                            class_c_rows.append((user_id, subject, obj, rel_type, source, defn, storage_type, is_hierarchy_rel, taxonomies))

                    committed = 0
                    staged = 0
                    if class_a_rows:
                        committed += manager.commit(class_a_rows)
                        log.info("ingest.class_a_committed", count=len(class_a_rows))

                        # Apply correction_behavior supersession for Class A facts
                        # When a Class A user-stated fact is inserted, supersede contradictory existing facts
                        try:
                            with db.cursor() as cur:
                                for user_id, subject, obj, rel_type, source, is_preferred, defn, storage_type, is_hierarchy_rel, taxonomies in class_a_rows:
                                    rel_type_lower = (rel_type or "").lower()

                                    # Query metadata for correction_behavior
                                    metadata = _REL_TYPE_META.get(rel_type_lower, {})
                                    behavior = metadata.get("correction_behavior", "supersede")

                                    log.info("ingest.class_a_supersession_check",
                                           rel_type=rel_type_lower, subject=subject[:8], obj=obj[:8],
                                           behavior=behavior, has_metadata=bool(metadata))

                                    # Only apply supersession if behavior is "supersede" (not "hard_delete", "immutable", etc.)
                                    if behavior != "supersede":
                                        log.info("ingest.class_a_supersession_skipped",
                                               rel_type=rel_type_lower, reason=f"behavior={behavior}")
                                        continue

                                    # Supersede existing facts with same user, subject, rel_type (but different object or already existing)
                                    # This handles: "I live at X" superseding old "I live at Y" facts
                                    cur.execute(
                                        "UPDATE facts SET superseded_at = now(), qdrant_synced = false "
                                        "WHERE user_id = %s AND subject_id = %s AND rel_type = %s "
                                        "AND object_id != %s AND superseded_at IS NULL",
                                        (user_id, subject, rel_type_lower, obj),
                                    )
                                    superseded_count = cur.rowcount
                                    log.info("ingest.class_a_supersession_result",
                                           rel_type=rel_type_lower, subject=subject[:8], new_obj=obj[:8],
                                           superseded_count=superseded_count)
                                    if superseded_count > 0:
                                        log.info("ingest.class_a_superseded_contradictory",
                                               rel_type=rel_type_lower, subject=subject[:8], new_object=obj[:8],
                                               superseded_count=superseded_count, user_id=req.user_id)
                        except Exception as _supersede_err:
                            # INTENTIONAL: Graceful degradation — supersession is a consistency optimization.
                            # If it fails, facts are still committed. This is non-critical path.
                            log.warning("ingest.class_a_supersession_failed",
                                       error_type=type(_supersede_err).__name__,
                                       error=str(_supersede_err),
                                       note="Class A facts committed despite supersession failure")

                        # Trigger immediate Qdrant sync for Class A facts (don't wait for 10s re_embedder poll)
                        # This ensures attribute queries immediately after ingest get results from both PostgreSQL and Qdrant
                        try:
                            qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")
                            qwen_api_url = _configured_llm_url()
                            upserted = 0
                            with db.cursor() as cur:
                                # Fetch the facts we just committed (qdrant_synced=false)
                                cur.execute(
                                    "SELECT id, user_id, subject_id, object_id, rel_type FROM facts "
                                    "WHERE user_id = %s AND qdrant_synced = false AND superseded_at IS NULL "
                                    "ORDER BY id DESC LIMIT %s",
                                    (req.user_id, len(class_a_rows))
                                )
                                fresh_facts = cur.fetchall()
                                if fresh_facts:
                                    collection = derive_collection(req.user_id)
                                    ensure_collection(collection, qdrant_url)
                                    for fact_id, fact_user_id, subject, obj, rel_type in fresh_facts:
                                        text = f"{subject} {rel_type} {obj}"
                                        vector = embed_text(text, qwen_api_url, timeout=10.0, fallback=True, embedding_url=_EMBEDDING_API_URL)
                                        if vector is None:
                                            continue
                                        row = {
                                            "id": fact_id,
                                            "user_id": fact_user_id,
                                            "subject_id": subject,
                                            "subject_display": subject,
                                            "object_id": obj,
                                            "object_display": obj,
                                            "rel_type": rel_type,
                                            "provenance": req.source,
                                            "confidence": 1.0,
                                            "confirmed_count": 0,
                                            "last_seen_at": None,
                                            "contradicted_by": None,
                                        }
                                        if upsert_to_qdrant(row, vector, collection, qdrant_url):
                                            mark_synced(db, fact_id)
                                            upserted += 1
                                    if upserted > 0:
                                        log.info("ingest.immediate_qdrant_sync", user_id=req.user_id, synced=upserted)
                        except Exception as _sync_err:
                            # INTENTIONAL: Graceful degradation — PostgreSQL facts are authoritative.
                            # If Qdrant is unavailable, skip sync and let re_embedder retry later.
                            # This prevents ingest failures when Qdrant is temporarily unavailable.
                            log.warning("ingest.immediate_qdrant_sync_failed",
                                       error_type=type(_sync_err).__name__,
                                       error=str(_sync_err),
                                       note="PostgreSQL facts committed; Qdrant sync will retry in re_embedder poll")

                    if class_b_rows:
                        staged_b = _commit_staged(db, class_b_rows, "B", confidence=0.8)
                        staged += staged_b
                        log.info("ingest.class_b_staged", count=staged_b)

                        # Trigger immediate Qdrant sync for Class B staged facts
                        # so they're available via vector search without waiting for re_embedder poll
                        try:
                            qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")
                            qwen_api_url = _configured_llm_url()
                            upserted_b = 0
                            with db.cursor() as cur:
                                cur.execute(
                                    "SELECT id, user_id, subject_id, object_id, rel_type, provenance, confidence FROM staged_facts "
                                    "WHERE user_id = %s AND qdrant_synced = false AND promoted_at IS NULL AND expires_at > now() "
                                    "ORDER BY id DESC LIMIT %s",
                                    (req.user_id, len(class_b_rows))
                                )
                                staged_fresh = cur.fetchall()
                                if staged_fresh:
                                    collection = derive_collection(req.user_id)
                                    ensure_collection(collection, qdrant_url)
                                    for sf_id, sf_uid, sf_subj, sf_obj, sf_rel, sf_prov, sf_conf in staged_fresh:
                                        text = f"{sf_subj} {sf_rel} {sf_obj}"
                                        vector = embed_text(text, qwen_api_url, timeout=10.0, fallback=True, embedding_url=_EMBEDDING_API_URL)
                                        if vector is None:
                                            continue
                                        s_row = {
                                            "id": sf_id,
                                            "user_id": sf_uid,
                                            "subject_id": sf_subj,
                                            "subject_display": sf_subj,
                                            "object_id": sf_obj,
                                            "object_display": sf_obj,
                                            "rel_type": sf_rel,
                                            "provenance": sf_prov,
                                            "confidence": float(sf_conf) if sf_conf else 0.8,
                                            "confirmed_count": 0,
                                            "last_seen_at": None,
                                            "contradicted_by": None,
                                        }
                                        if upsert_to_qdrant(s_row, vector, collection, qdrant_url):
                                            with db.cursor() as _mark:
                                                _mark.execute(
                                                    "UPDATE staged_facts SET qdrant_synced = true WHERE id = %s",
                                                    (sf_id,)
                                                )
                                            upserted_b += 1
                                    if upserted_b > 0:
                                        log.info("ingest.immediate_qdrant_sync_staged", user_id=req.user_id, synced=upserted_b)
                        except Exception as _sync_err:
                            # INTENTIONAL: Graceful degradation — PostgreSQL staged_facts are authoritative.
                            # If Qdrant is unavailable, skip sync and let re_embedder retry later.
                            log.warning("ingest.immediate_qdrant_sync_staged_failed",
                                       error_type=type(_sync_err).__name__,
                                       error=str(_sync_err),
                                       note="Staged facts committed; Qdrant sync will retry in re_embedder poll")

                    if class_c_rows:
                        staged_c = _commit_staged(db, class_c_rows, "C", confidence=0.4)
                        staged += staged_c
                        log.info("ingest.class_c_staged", count=staged_c)

                    # Use class_a_rows for downstream corrections processing
                    resolved_rows = class_a_rows

                    # Build a map of edges to identify which rows are corrections
                    # Key is (original_subject, object, rel_type); value is whether it's a correction
                    correction_map = {}
                    for edge in edges:
                        key = (edge.subject.lower(), edge.object.lower(), edge.rel_type.lower())
                        correction_map[key] = edge.is_correction

                    # Build set of preferred objects from pref_name rows in this batch
                    # e.g. john → ${USER} → pref_name means "${USER}" is preferred
                    batch_preferred_objects = {
                        row[2].lower() for row in resolved_rows
                        if row[3].lower() == "pref_name" and row[5]
                    }

                    with db.cursor() as cur:
                        for row in resolved_rows:
                            user_id, subject, obj, rel_type, source, is_preferred = row[0], row[1], row[2], row[3], row[4], row[5]
                            if rel_type.lower() == "also_known_as" and is_preferred:
                                cur.execute(
                                    "UPDATE facts SET is_preferred_label = false"
                                    " WHERE user_id = %s AND subject_id = %s AND rel_type = 'also_known_as'"
                                    " AND object_id != %s",
                                    (user_id, subject, obj),
                                )

                        # Propagate preferred label from pref_name to matching user → also_known_as rows
                        if batch_preferred_objects:
                            for preferred_obj in batch_preferred_objects:
                                cur.execute(
                                    "UPDATE facts SET is_preferred_label = true"
                                    " WHERE user_id = %s AND subject_id = 'user'"
                                    " AND rel_type = 'also_known_as' AND object_id = %s",
                                    (req.user_id, preferred_obj),
                                )
                                # Clear other user → also_known_as preferred labels
                                cur.execute(
                                    "UPDATE facts SET is_preferred_label = false"
                                    " WHERE user_id = %s AND subject_id = 'user'"
                                    " AND rel_type = 'also_known_as' AND object_id != %s",
                                    (req.user_id, preferred_obj),
                                )

                        # Sync is_preferred to entity_aliases after every also_known_as / pref_name commit.
                        # This is the authoritative preference flip — entity_aliases drives query-time
                        # identity resolution. facts.is_preferred_label is secondary.
                        log.info("ingest.sync_debug",
                                 resolved_rows=[(r[1], r[2], r[3], r[5]) for r in resolved_rows])
                        for row in resolved_rows:
                            _uid, _subj, _obj, _rel, _src, _is_pref = row[0], row[1], row[2], row[3], row[4], row[5]
                            if _rel.lower() not in ("also_known_as", "pref_name"):
                                continue

                            # Resolve display name from canonical UUID (Bug #3 fix)
                            _display_name = _canonical_to_display.get(_obj, _obj)

                            # Upsert alias into entity_aliases
                            cur.execute(
                                "INSERT INTO entity_aliases (entity_id, user_id, alias, is_preferred) "
                                "VALUES (%s, %s, %s, %s) "
                                "ON CONFLICT (user_id, alias) "
                                "DO UPDATE SET is_preferred = EXCLUDED.is_preferred",
                                (_subj, _uid, _display_name, _is_pref),
                            )

                            # If this is a hard preference, demote all other aliases for this entity
                            if _is_pref:
                                cur.execute(
                                    "UPDATE entity_aliases SET is_preferred = false "
                                    "WHERE user_id = %s AND entity_id = %s AND alias != %s",
                                    (_uid, _subj, _display_name),
                                )
                                # Mirror into facts: demote other also_known_as rows for this entity
                                cur.execute(
                                    "UPDATE facts SET is_preferred_label = false, qdrant_synced = false "
                                    "WHERE user_id = %s AND subject_id = %s "
                                    "AND rel_type IN ('also_known_as', 'pref_name') "
                                    "AND object_id != %s AND superseded_at IS NULL "
                                    "AND hard_delete_flag = false",
                                    (_uid, _subj, _obj),
                                )
                                log.info("ingest.preferred_name_flipped",
                                         entity=_subj, new_preferred=_obj, user_id=_uid)

                        for row in resolved_rows:
                            user_id, subject, obj, rel_type, source, is_preferred = row[0], row[1], row[2], row[3], row[4], row[5]

                            # Check if this row came from a correction edge
                            # Match by object and rel_type (subject may have been resolved).
                            # obj is the resolved UUID; use _canonical_to_display to get the original name.
                            _display_name = _canonical_to_display.get(obj.lower(), obj.lower())
                            is_correction = any(
                                e.is_correction and
                                e.object.lower() == _display_name and
                                e.rel_type.lower() == rel_type.lower()
                                for e in edges
                            )
                            if not is_correction:
                                continue

                            # For also_known_as/pref_name corrections where subject is user's canonical identity,
                            # find the actual entity being corrected (e.g., wife entity when user said "my wife...")
                            correction_subject = subject.lower()
                            correction_object = obj.lower()

                            if rel_type.lower() in ("also_known_as", "pref_name"):
                                canonical_user = registry.get_canonical_for_user(user_id)
                                if canonical_user and correction_subject == canonical_user:
                                    # Subject is the user's canonical ID. Find the entity we're actually correcting
                                    # by looking for the most recent also_known_as/pref_name fact for a related entity
                                    cur.execute(
                                        "SELECT subject_id FROM facts WHERE user_id = %s"
                                        " AND rel_type IN ('also_known_as', 'pref_name')"
                                        " AND subject_id != %s"
                                        " ORDER BY id DESC LIMIT 1",
                                        (user_id, correction_subject),
                                    )
                                    candidate = cur.fetchone()
                                    if candidate:
                                        correction_subject = candidate[0]
                                        log.info("correction.subject_resolved",
                                                 original=subject, resolved=correction_subject,
                                                 rel_type=rel_type)

                            # Identity rels (also_known_as, pref_name, same_as): lookup by object (the NAME being corrected)
                            # All other rels: lookup by subject + rel_type (the FACT being corrected, regardless of new value)
                            # This allows age→10, location→Paris, occupation→Engineer corrections to supersede old values
                            _IDENTITY_RELS = {"also_known_as", "pref_name", "same_as"}

                            if rel_type.lower() in _IDENTITY_RELS:
                                # Identity: correct the name (object is the name)
                                cur.execute(
                                    "SELECT id FROM facts WHERE user_id = %s AND subject_id = %s"
                                    " AND object_id = %s AND rel_type = %s",
                                    (user_id, correction_subject, correction_object, rel_type.lower()),
                                )
                            else:
                                # Non-identity: correct the fact (find most recent by subject + rel_type)
                                cur.execute(
                                    "SELECT id FROM facts WHERE user_id = %s AND subject_id = %s"
                                    " AND rel_type = %s ORDER BY id DESC LIMIT 1",
                                    (user_id, correction_subject, rel_type.lower()),
                                )
                            result = cur.fetchone()

                            # Fallback for identity rels: if fact not found by object, try by subject
                            # (fact might have been inserted with wrong subject due to resolution)
                            if not result and rel_type.lower() in _IDENTITY_RELS:
                                cur.execute(
                                    "SELECT id FROM facts WHERE user_id = %s AND subject_id = %s"
                                    " AND object_id = %s AND rel_type = %s",
                                    (user_id, subject.lower(), correction_object, rel_type.lower()),
                                )
                                result = cur.fetchone()
                            if not result:
                                continue
                            new_fact_id = result[0]
                            # Query correction_behavior from metadata cache (dprompt-73b)
                            # Falls back to DB query if cache doesn't have it
                            _cb_meta = _get_rel_type_metadata(rel_type.lower())
                            behavior = _cb_meta.get("correction_behavior", "supersede")
                            _apply_correction(cur, user_id, correction_subject, correction_object,
                                              rel_type.lower(), new_fact_id, behavior)
                            if rel_type.lower() == "also_known_as":
                                cur.execute(
                                    "UPDATE facts SET is_preferred_label = true WHERE id = %s",
                                    (new_fact_id,),
                                )

                            # dprompt-45: When pref_name correction occurs, update entity_aliases.
                            # Clear old preferred aliases for this entity, set the corrected name as preferred.
                            if rel_type.lower() == "pref_name":
                                # Get the display name for the corrected subject entity
                                _corrected_display = registry.get_preferred_name(user_id, correction_subject)
                                # Clear old preferred flags for this entity
                                cur.execute(
                                    "UPDATE entity_aliases SET is_preferred = false "
                                    "WHERE user_id = %s AND entity_id = %s",
                                    (user_id, correction_subject),
                                )
                                # Set the corrected object as preferred
                                _corrected_obj_display = _canonical_to_display.get(
                                    correction_object, correction_object)
                                cur.execute(
                                    "INSERT INTO entity_aliases (user_id, entity_id, alias, is_preferred) "
                                    "VALUES (%s, %s, %s, true) "
                                    "ON CONFLICT (user_id, alias) DO UPDATE SET "
                                    "entity_id = EXCLUDED.entity_id, is_preferred = true",
                                    (user_id, correction_subject, _corrected_obj_display),
                                )
                                log.info("ingest.pref_name_correction_aliases_updated",
                                         entity=correction_subject,
                                         corrected_name=_corrected_obj_display)
                                # dprompt-086: Trigger name conflict re-evaluation when user-stated
                                # pref_name arrives. The re_embedder.resolve_name_conflicts() function
                                # (planned, not yet implemented) would be called here with:
                                #   (user_id, correction_subject, _corrected_obj_display)
                                # to disambiguate which entity should hold the preferred name and
                                # resolve pending disputes in entity_name_conflicts table.
                                log.info("ingest.name_conflict_resolver_trigger_point",
                                         entity=correction_subject,
                                         new_pref_name=_corrected_obj_display,
                                         note="resolver not yet implemented — entity_aliases updated inline")

                # === ATOMIC COMMIT: All writes committed in one transaction ===
                # Facts, staged_facts, entity_aliases, corrections all commit together
                db.commit()

    except psycopg2.Error as err:
        import traceback
        db.rollback()
        log.error("ingest.database_constraint_violation",
                 error_type=type(err).__name__,
                 error=str(err),
                 user_id=req.user_id if 'req' in locals() else 'unknown',
                 traceback=traceback.format_exc())
        raise HTTPException(status_code=400, detail=f"Database constraint violation: {str(err)}")
    except Exception as err:
        import traceback
        db.rollback()
        log.error("ingest.transaction_failed",
                 error_type=type(err).__name__,
                 error=str(err),
                 user_id=req.user_id if 'req' in locals() else 'unknown',
                 traceback=traceback.format_exc())
        raise HTTPException(status_code=500, detail="Ingest transaction failed: internal error")
    finally:
        db.close()

    # === IDEMPOTENCY CACHE: Store response for deduplication ===
    # Only cache successful responses (status="valid", "extracted", "novel", "conflict").
    # Do not cache error responses (failures are not idempotent).
    response = IngestResponse(status="valid", committed=committed, staged=staged,
                              entities=[EntityResult(entity=r["entity"], label=r["type"], canonical_id=r["canonical_id"]) for r in resolved],
                              facts=facts)

    if idempotency_key and _idempotency_mgr and (committed > 0 or staged > 0):
        response_dict = {
            "status": response.status,
            "committed": response.committed,
            "staged": response.staged,
            "entities": [{"entity": e.entity, "label": e.label, "canonical_id": e.canonical_id} for e in response.entities],
            "facts": response.facts,
        }
        success = _idempotency_mgr.cache_response(idempotency_key, response_dict)
        if success:
            log.info("ingest.idempotency_cache_stored",
                    key=idempotency_key[:12],
                    user_id=req.user_id,
                    committed=response.committed)
        else:
            log.warning("ingest.idempotency_cache_store_failed",
                       key=idempotency_key[:12],
                       user_id=req.user_id)
    # === END IDEMPOTENCY CACHE ===

    # Phase 3: Enqueue Class C ingest events for re-embedder learning (non-blocking)
    # For each novel rel_type that was staged as Class C, enqueue an event
    # Fire-and-forget: doesn't block response, no await
    if staged > 0:
        try:
            # Collect rel_types from class_c_rows that were just staged
            if 'class_c_rows' in locals() and class_c_rows:
                rel_types_seen = set()
                for row in class_c_rows:
                    rel_type = row[3].lower() if len(row) > 3 else None
                    if rel_type and rel_type not in rel_types_seen:
                        rel_types_seen.add(rel_type)

                        # Check if rel_type is unknown (not in rel_types table)
                        try:
                            with psycopg2.connect(os.environ.get("POSTGRES_DSN")) as conn:
                                with conn.cursor() as cur:
                                    cur.execute(
                                        "SELECT 1 FROM rel_types WHERE rel_type = %s LIMIT 1",
                                        (rel_type,)
                                    )
                                    if not cur.fetchone():
                                        # Novel rel_type — enqueue for re-embedder evaluation
                                        # Use Python 3.7+ asyncio.create_task if available
                                        try:
                                            import asyncio
                                            asyncio.create_task(_enqueue_reembedder_event(
                                                event_type="class_c_ingest",
                                                user_id=req.user_id,
                                                data={"rel_type": rel_type, "confidence": 0.4},
                                                priority="normal"
                                            ))
                                        except Exception:
                                            # If asyncio fails (not in async context), just log it
                                            log.debug(f"ingest.class_c_event_enqueue_skipped rel_type={rel_type} context=sync")
                        except Exception as e:
                            log.debug(f"ingest.class_c_event_enqueue_check_error rel_type={rel_type}: {e}")
        except Exception as e:
            log.debug(f"ingest.class_c_event_enqueue_error: {e}")

    return response

def _fetch_hierarchy_facts(db_conn, user_id: str, entity_ids: set[str]) -> list[dict]:
    """
    Fetch hierarchy facts (instance_of, subclass_of, member_of, part_of)
    for entities found via graph traversal. Ensures type/classification
    information is available alongside relationship facts (dBug-019).

    Queries both facts and staged_facts tables. Returns deduplicated
    list of fact dicts matching the same structure as _fetch_user_facts.
    """
    results = []
    if not entity_ids:
        return results
    try:
        hier_rels = list(_get_hierarchy_rels())
        with db_conn.cursor() as cur:
            entity_placeholders = ",".join(["%s"] * len(entity_ids))
            rel_placeholders = ",".join(["%s"] * len(hier_rels))
            params_f = [user_id] + list(entity_ids) + hier_rels
            # Query facts table
            cur.execute(
                f"SELECT subject_id, object_id, rel_type, provenance, confidence,"
                f"  confirmed_count, fact_class FROM facts "
                f"WHERE user_id = %s AND subject_id IN ({entity_placeholders})"
                f"  AND rel_type IN ({rel_placeholders}) AND superseded_at IS NULL"
                f"  AND hard_delete_flag = false"
                f"  AND (valid_until IS NULL OR valid_until > now())",
                params_f,
            )
            seen = set()
            for r in cur.fetchall():
                key = (r[0], r[1], r[2])
                if key not in seen:
                    seen.add(key)
                    results.append({
                        "subject": r[0], "object": r[1], "rel_type": r[2],
                        "provenance": r[3],
                        "confidence": float(r[4]) if r[4] else 1.0,
                        "category": _get_rel_type_category(r[2]),
                        "fact_state": "long_term",
                        "fact_class": r[6] if r[6] else "A",
                        "staged_confirmations": r[5] if r[5] else 0,
                        "promoted_at": None,
                        "expires_at": None,
                    })
            # Query staged_facts table with same params
            cur.execute(
                f"SELECT subject_id, object_id, rel_type, provenance, confidence,"
                f"  confirmed_count, fact_class, promoted_at, expires_at FROM staged_facts "
                f"WHERE user_id = %s AND subject_id IN ({entity_placeholders})"
                f"  AND rel_type IN ({rel_placeholders}) AND expires_at > now()"
                f"  AND promoted_at IS NULL",
                params_f,
            )
            for r in cur.fetchall():
                key = (r[0], r[1], r[2])
                if key not in seen:
                    seen.add(key)
                    promoted = r[7].isoformat() if r[7] else None
                    expires = r[8].isoformat() if r[8] else None
                    results.append({
                        "subject": r[0], "object": r[1], "rel_type": r[2],
                        "provenance": r[3],
                        "confidence": float(r[4]) if r[4] else 0.0,
                        "category": _get_rel_type_category(r[2]),
                        "fact_state": "staged",
                        "fact_class": r[6] if r[6] else "B",
                        "staged_confirmations": r[5] if r[5] else 0,
                        "promoted_at": promoted,
                        "expires_at": expires,
                    })
    except Exception as e:
        log.warning("query.fetch_hierarchy_facts_failed", error=str(e))
    return results

def _determine_query_scope(db, query_text: str, user_id: str) -> set[str]:
    """
    Determine query-driven scope from the user's question (NOT from facts).
    Implements dprompt-130: metadata-driven query keyword matching.

    Returns: set of taxonomy_name strings matching the query intent
    METADATA-DRIVEN: Uses entity_taxonomies and rel_types tables, no hardcoding.
    """
    if not query_text or not db:
        return set()

    import re
    detected_taxonomies = set()

    # Extract keywords from query (split on whitespace, remove punctuation)
    query_lower = query_text.lower()
    keywords = set(re.findall(r'\b[a-z]+\b', query_lower))

    # Filter noise words
    noise_words = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been',
                   'what', 'how', 'when', 'where', 'why', 'who', 'which', 'that',
                   'and', 'or', 'not', 'but', 'if', 'to', 'of', 'in', 'on', 'at',
                   'by', 'for', 'with', 'from', 'up', 'about', 'as', 'can', 'will',
                   'would', 'could', 'should', 'may', 'might', 'must', 'do', 'does',
                   'did', 'have', 'has', 'had', 'me', 'my', 'you', 'your', 'tell',
                   'me', 'about', 'like', 'know', 'have', 'tell'}
    keywords -= noise_words

    if not keywords:
        return set()

    try:
        with db.cursor() as cur:
            # Phase 2A: Match keywords against taxonomy descriptions
            for keyword in keywords:
                try:
                    cur.execute(
                        "SELECT DISTINCT taxonomy_name FROM entity_taxonomies "
                        "WHERE description ILIKE %s",
                        (f"%{keyword}%",)
                    )
                    for row in cur.fetchall():
                        detected_taxonomies.add(row[0])
                except Exception as e:
                    log.debug("query.scope_description_match_failed", keyword=keyword, error=str(e))

            # Phase 2B: Match keywords against rel_types and find their taxonomies
            for keyword in keywords:
                try:
                    cur.execute(
                        "SELECT DISTINCT et.taxonomy_name FROM rel_types rt "
                        "INNER JOIN entity_taxonomies et ON et.rel_types_defining_group @> ARRAY[rt.rel_type] "
                        "WHERE rt.rel_type = %s",
                        (keyword,)
                    )
                    for row in cur.fetchall():
                        detected_taxonomies.add(row[0])
                except Exception as e:
                    log.debug("query.scope_reltype_match_failed", keyword=keyword, error=str(e))

            # Phase 2C: Entity type hierarchal fallback - resolve keywords as entity names
            for keyword in keywords:
                try:
                    cur.execute(
                        "SELECT DISTINCT et.taxonomy_name FROM entity_aliases ea "
                        "INNER JOIN entities e ON ea.entity_id = e.id "
                        "INNER JOIN entity_taxonomies et ON et.member_entity_types @> ARRAY[e.entity_type] "
                        "WHERE ea.alias ILIKE %s AND ea.user_id = %s",
                        (keyword, user_id)
                    )
                    for row in cur.fetchall():
                        detected_taxonomies.add(row[0])
                except Exception as e:
                    log.debug("query.scope_entity_match_failed", keyword=keyword, error=str(e))

    except Exception as e:
        log.warning("query.determine_scope_failed", error=str(e))
        return set()

    if detected_taxonomies:
        log.info("determine_query_scope", query=query_text[:50], keywords=list(keywords),
                 detected_taxonomies=list(detected_taxonomies))
    else:
        log.debug("determine_query_scope.no_match", query=query_text[:50], keywords=list(keywords))

    return detected_taxonomies

@app.post("/query")
def query(request: QueryRequest):
    qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")
    qwen_api_url = _configured_llm_url()
    user_id = request.user_id or "anonymous"
    collection = derive_collection(user_id)

    preferred_names = {}
    canonical_identity = None
    baseline_facts = []
    attributes = {}
    resolved_entity_ids = set()  # Track entities resolved in entity_resolution block
    user_surrogate = user_id  # OWUI UUID IS the surrogate — always
    db = None
    registry = None
    def _fetch_user_facts(
        db_conn,
        user_id: str,
        entity_id: str = None,
        rel_types: tuple[str, ...] = None,
        taxonomies: set[str] = None,
    ) -> list[dict]:
        """
        Fetch facts from BOTH facts and staged_facts tables.
        Uses two separate queries (not UNION) to avoid parameter binding
        issues with psycopg2 shared params across multiple SELECTs.
        Attaches fact_state metadata so the Filter can distinguish
        long-term (authoritative) from staged (provisional) facts.

        taxonomies: Optional set of taxonomy names to filter by (e.g., {'family', 'work'}).
                   When provided, only returns facts whose taxonomies overlap with this set.

        Returns list of {subject, object, rel_type, provenance, confidence,
                          category, fact_state, fact_class, is_preferred_label,
                          staged_confirmations, promoted_at, expires_at} dicts.
        """
        results = []
        try:
            with db_conn.cursor() as cur:
                # --- Build base conditions (all qualified with table alias for JOIN safety) ---
                f_conds = ["f.user_id = %s", "f.superseded_at IS NULL",
                           "f.hard_delete_flag = false",
                           "(f.valid_until IS NULL OR f.valid_until > now())"]
                s_conds = ["s.user_id = %s", "s.expires_at > now()",
                           "s.promoted_at IS NULL",
                           "(s.fact_class = 'A' OR s.fact_class = 'B')"]

                if rel_types:
                    ph = ",".join(["%s"] * len(rel_types))
                    f_conds.append(f"f.rel_type IN ({ph})")
                    s_conds.append(f"s.rel_type IN ({ph})")

                if entity_id:
                    f_conds.append("(f.subject_id = %s OR f.object_id = %s)")
                    s_conds.append("(s.subject_id = %s OR s.object_id = %s)")

                if taxonomies:
                    f_conds.append("f.taxonomies && %s::text[]")
                    s_conds.append("s.taxonomies && %s::text[]")

                f_where = " AND ".join(f_conds)
                s_where = " AND ".join(s_conds)

                # --- Build independent params for each query ---
                base_params = [user_id]
                f_params = list(base_params)
                s_params = list(base_params)

                if rel_types:
                    f_params.extend(rel_types)
                    s_params.extend(rel_types)

                if entity_id:
                    f_params.extend([entity_id, entity_id])
                    s_params.extend([entity_id, entity_id])

                if taxonomies:
                    f_params.append(list(taxonomies))
                    s_params.append(list(taxonomies))

                # --- Query 1: facts table (long-term) ---
                f_query = (
                    f"SELECT f.subject_id, f.object_id, f.rel_type, f.provenance, f.confidence,"
                    f"  f.confirmed_count, f.fact_class, f.is_preferred_label, r.natural_language "
                    f"FROM facts f "
                    f"LEFT JOIN rel_types r ON f.rel_type = r.rel_type "
                    f"WHERE {f_where}"
                )
                # Debug: log query params for troubleshooting (dprompt-88c)
                log.info("fetch_user_facts.facts_query", where=f_where, params=f_params[:3] if len(f_params) > 3 else f_params, entity_id=entity_id)
                cur.execute(f_query, f_params)
                seen = set()
                for r in cur.fetchall():
                    key = (r[0], r[1], r[2])
                    if key not in seen:
                        seen.add(key)
                        results.append({
                            "subject": r[0], "object": r[1], "rel_type": r[2],
                            "provenance": r[3],
                            "confidence": float(r[4]) if r[4] else 1.0,
                            "category": _get_rel_type_category(r[2]),
                            "fact_state": "long_term",
                            "fact_class": r[6] if r[6] else "A",
                            "is_preferred_label": bool(r[7]) if r[7] is not None else False,
                            "staged_confirmations": r[5] if r[5] else 0,
                            "promoted_at": None,
                            "expires_at": None,
                            "definition": r[8] if len(r) > 8 and r[8] else '',
                        })

                # --- Query 2: staged_facts table (provisional) ---
                s_query = (
                    f"SELECT s.subject_id, s.object_id, s.rel_type, s.provenance, s.confidence,"
                    f"  s.confirmed_count, s.fact_class, s.promoted_at, s.expires_at, r.natural_language "
                    f"FROM staged_facts s "
                    f"LEFT JOIN rel_types r ON s.rel_type = r.rel_type "
                    f"WHERE {s_where}"
                )
                cur.execute(s_query, s_params)
                for r in cur.fetchall():
                    key = (r[0], r[1], r[2])
                    if key not in seen:
                        seen.add(key)
                        promoted = r[7].isoformat() if r[7] else None
                        expires = r[8].isoformat() if r[8] else None
                        results.append({
                            "subject": r[0], "object": r[1], "rel_type": r[2],
                            "provenance": r[3],
                            "confidence": float(r[4]) if r[4] else 0.0,
                            "category": _get_rel_type_category(r[2]),
                            "fact_state": "staged",
                            "fact_class": r[6] if r[6] else "B",
                            "is_preferred_label": False,
                            "staged_confirmations": r[5] if r[5] else 0,
                            "promoted_at": promoted,
                            "expires_at": expires,
                            "definition": r[9] if len(r) > 9 and r[9] else '',
                        })
        except Exception as e:
            log.warning("query.fetch_user_facts_failed", error=str(e))
        return results

    def _fetch_user_events(
        db_conn,
        user_id: str,
        entity_id: str = None,
    ) -> list[dict]:
        """Fetch events for user from events table, optionally filtered by entity."""
        results = []
        try:
            with db_conn.cursor() as cur:
                query = "SELECT id, subject_id, object_id, event_type, occurs_on, recurrence, confidence FROM events WHERE user_id = %s"
                params = [user_id]
                if entity_id:
                    query += " AND subject_id = %s"
                    params.append(entity_id)
                cur.execute(query, params)
                for row in cur.fetchall():
                    results.append({
                        "id": row[0],
                        "subject": row[1],
                        "object": row[2],
                        "event_type": row[3],
                        "occurs_on": row[4],
                        "recurrence": row[5],
                        "confidence": row[6],
                        "source": "events_table",
                    })
        except Exception as e:
            log.warning("query.fetch_user_events_failed", error=str(e))
        return results
    def _fetch_attributes(
        db_conn,
        user_id: str,
        entity_ids: list[str],
        max_sensitivity: str = "private",
    ) -> dict:
        """
        Fetch entity_attributes for a list of entity IDs.
        max_sensitivity controls which tiers are returned:
          'public'  — only public attributes
          'private' — public + private (default)
          'secret'  — all including secret (never use in query path)
        Returns {entity_id: {attribute: value}} dict.
        """
        if not entity_ids:
            return {}
        sensitivity_filter = {
            "public": ("public",),
            "private": ("public", "private"),
            "secret": ("public", "private", "secret"),
        }.get(max_sensitivity, ("public", "private"))

        try:
            _ph = ",".join(["%s"] * len(entity_ids))
            _sh = ",".join(["%s"] * len(sensitivity_filter))
            with db_conn.cursor() as cur:
                cur.execute(
                    f"SELECT entity_id, attribute, value_int, value_float, value_text, "
                    f"value_date, category "
                    f"FROM entity_attributes "
                    f"WHERE user_id = %s AND entity_id IN ({_ph}) "
                    f"AND sensitivity IN ({_sh})",
                    [user_id] + list(entity_ids) + list(sensitivity_filter),
                )
                attributes = {}
                for row in cur.fetchall():
                    eid, attr, vi, vf, vt, vd, cat = row
                    if eid not in attributes:
                        attributes[eid] = {}
                    attributes[eid][attr] = {
                        "value": (
                            vi if vi is not None else
                            vf if vf is not None else
                            vt if vt is not None else
                            str(vd) if vd is not None else None
                        ),
                        "category": cat,
                    }
                return attributes
        except Exception as e:
            log.warning("query.attributes_fetch_failed", error=str(e))
            return {}

    def _resolve_display_names(facts: list[dict], registry, user_id: str, user_entity_id: str = "user") -> list[dict]:
        """
        Resolve UUID subject_id/object_id to preferred display names.
        dprompt-32: Falls back to non-preferred aliases when preferred name is missing
        (e.g., name collision loser entities).
        """
        resolved = []
        for f in facts:
            subject_display = registry.get_preferred_name(user_id, f["subject"])
            object_display = registry.get_preferred_name(user_id, f["object"])

            # dprompt-32: if preferred name is a UUID (no human-readable alias found),
            # fall back to any non-preferred alias so the entity isn't invisible
            if subject_display and _UUID_PATTERN.match(str(subject_display)):
                fallback = registry.get_any_alias(user_id, f["subject"])
                if fallback:
                    subject_display = fallback
            if object_display and _UUID_PATTERN.match(str(object_display)):
                fallback = registry.get_any_alias(user_id, f["object"])
                if fallback:
                    object_display = fallback

            if f.get("rel_type") == "spouse":
                log.info("query.resolve_display_names.spouse",
                        original_subject=f["subject"], resolved_subject=subject_display,
                        original_object=f["object"], resolved_object=object_display)

            # FIXED: Normalize user entity IDs to "user" placeholder for consistent display
            if f["subject"] == user_entity_id or subject_display == user_entity_id:
                subject_display = "user"
            if f["object"] == user_entity_id or object_display == user_entity_id:
                object_display = "user"

            resolved.append({
                **f,
                "subject": subject_display,
                "object": object_display,
                "_subject_id": f["subject"],   # dprompt-61: preserve UUID for dedup
                "_object_id": f["object"],     # dprompt-61: preserve UUID for dedup
            })
        return resolved

    def _populate_preferred_names(merged_facts: list[dict], direct_facts: list[dict], baseline_facts: list[dict], qdrant_facts: list[dict], attributes: dict) -> dict:
        """Populate preferred_names dict from merged facts using UUID-to-display-name resolution.

        dprompt-61: Builds UUID→display_name mappings for the Filter to resolve UUIDs in facts.
        Uses three sources: entity_aliases (primary), entity_attributes pref_name scalar (fallback),
        UUID itself (last resort).
        """
        preferred_names = {}
        if not registry:
            log.warning("query._populate_preferred_names.registry_none", user_id=user_id, merged_facts_count=len(merged_facts))
            return preferred_names

        # Build set of real entity UUIDs from pre-resolution PostgreSQL facts (direct + baseline)
        _real_entity_ids: set[str] = {user_entity_id_for_query}
        for f in direct_facts + baseline_facts:
            if _UUID_PATTERN.match(f.get("subject", "")):
                _real_entity_ids.add(f["subject"])
            if _UUID_PATTERN.match(f.get("object", "")):
                _real_entity_ids.add(f["object"])

        # Populate preferred_names for all UUID entities in merged_facts
        # NO RECURSIVE MATCHING — checks pre-extracted values only
        log.info("query._populate_preferred_names.start", merged_facts_count=len(merged_facts), attributes_keys=list(attributes.keys()) if attributes else [])
        for f in merged_facts:
            for field in ("subject", "object"):
                val = f[field]
                if not val or val in preferred_names:
                    continue
                # Try entity_aliases first (primary)
                resolved = registry.get_preferred_name(user_id, val)
                if resolved and resolved != val:
                    preferred_names[val] = resolved
                    log.info("query._populate_preferred_names.alias_found", val=val, resolved=resolved)
                elif _UUID_PATTERN.match(val):
                    # UUID with no alias — check entity_attributes for pref_name scalar
                    if attributes and val in attributes:
                        pref_name_attr = attributes[val].get("pref_name")
                        if isinstance(pref_name_attr, dict):
                            pref_name_value = pref_name_attr.get("value")
                        else:
                            pref_name_value = pref_name_attr
                        if pref_name_value:
                            preferred_names[val] = str(pref_name_value)
                            log.info("query._populate_preferred_names.pref_name_found", val=val, pref_name=pref_name_value)
                        else:
                            preferred_names[val] = val
                    else:
                        preferred_names[val] = val

        # Ensure user entity is in preferred_names (dBug-user-entity-preferred-name-skipped)
        # User's pref_name from attributes should be included so Filter can resolve user's UUID
        if user_entity_id_for_query and user_entity_id_for_query not in preferred_names:
            if attributes and user_entity_id_for_query in attributes:
                user_pref_name_attr = attributes[user_entity_id_for_query].get("pref_name")
                if user_pref_name_attr:
                    if isinstance(user_pref_name_attr, dict):
                        user_pref_name = user_pref_name_attr.get("value")
                    else:
                        user_pref_name = user_pref_name_attr
                    if user_pref_name:
                        preferred_names[user_entity_id_for_query] = str(user_pref_name)
                        log.info("query._populate_preferred_names.user_pref_name_found", user_id=user_entity_id_for_query, pref_name=user_pref_name)

        log.info("query._populate_preferred_names.complete", preferred_names_count=len(preferred_names))

        return preferred_names

    def _clean_preferred_names(pns: dict) -> dict:
        """Remove empty strings, Concept/unknown entity types.

        Keep UUID→UUID mappings (entities without registered preferred names) so
        Filter can still resolve them. Concept entities (e.g. "pets", "family", "dog")
        are taxonomy labels, not named entities. Excluding them prevents the Filter's
        Tier 1 entity matching from returning only member_of/is_a facts for category
        queries. # NO RECURSIVE MATCHING — _UUID_PATTERN is a static compile-once pattern.
        """
        # Keep all non-empty values, including UUID→UUID fallbacks
        cleaned = {k: v for k, v in pns.items() if v}
        if not cleaned or not db:
            return cleaned
        # dprompt-50: exclude Concept/unknown entities from name resolution
        _uuid_keys = [k for k in cleaned if _UUID_PATTERN.match(str(k))]
        if not _uuid_keys:
            return cleaned
        try:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT id FROM entities WHERE user_id = %s AND id = ANY(%s) "
                    "AND entity_type NOT IN ('Concept')",
                    (user_id, _uuid_keys),
                )
                allowed = {row[0] for row in cur.fetchall()}
            return {k: v for k, v in cleaned.items()
                    if not _UUID_PATTERN.match(str(k)) or k in allowed}
        except Exception:
            return cleaned

    def _get_entity_aliases(entity_id: str) -> list[dict]:
        """Return all aliases for an entity UUID with is_preferred flag.
        Returns empty list on DB error or if entity has no aliases.
        """
        if not db or not entity_id:
            return []
        try:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT alias, is_preferred FROM entity_aliases "
                    "WHERE user_id = %s AND entity_id = %s "
                    "ORDER BY is_preferred DESC, alias",
                    (user_id, entity_id),
                )
                return [
                    {"name": row[0], "is_preferred": row[1]}
                    for row in cur.fetchall()
                ]
        except Exception:
            return []

    def _build_entity_types(pns: dict) -> dict:
        """Build entity_types dict parallel to preferred_names.

        Maps entity_id (UUID or display string) → entity_type from the entities table.
        For UUID keys: batch query the entities table directly.
        For non-UUID keys: resolve via entity_aliases → entities JOIN.
        Graceful degradation: returns empty dict on DB error.
        """
        entity_types = {}
        if not db:
            return entity_types

        # UUID-keyed entries: batch query entities table
        _uuid_keys_in_pns = [k for k in pns if _UUID_PATTERN.match(str(k))]
        if _uuid_keys_in_pns:
            try:
                with db.cursor() as cur:
                    cur.execute(
                        "SELECT id, entity_type FROM entities "
                        "WHERE user_id = %s AND id = ANY(%s)",
                        (user_id, _uuid_keys_in_pns),
                    )
                    for entity_id, etype in cur.fetchall():
                        entity_types[entity_id] = etype or "unknown"
            except Exception:
                pass  # graceful — missing entity_types is non-fatal

        # Non-UUID keys (display-name strings): resolve via entity_aliases → entities
        _non_uuid_keys = [k for k in pns if not _UUID_PATTERN.match(str(k))]
        if _non_uuid_keys:
            try:
                with db.cursor() as cur:
                    cur.execute(
                        "SELECT ea.alias, COALESCE(e.entity_type, 'unknown') "
                        "FROM entity_aliases ea "
                        "LEFT JOIN entities e ON e.id = ea.entity_id AND e.user_id = ea.user_id "
                        "WHERE ea.user_id = %s AND ea.alias = ANY(%s)",
                        (user_id, _non_uuid_keys),
                    )
                    for alias, etype in cur.fetchall():
                        entity_types[alias] = etype or "unknown"
            except Exception:
                pass
        return entity_types

    def _attributes_to_facts(attributes: dict) -> list[dict]:
        """
        Convert entity_attributes to facts format for injection.
        Each attribute becomes a fact with subject=entity, rel_type=attribute.
        Works for all entities, not just "user".
        """
        facts = []
        # Collect all attribute rel_types to fetch definitions in bulk
        attr_rel_types = set()
        for entity_attrs in attributes.values():
            if isinstance(entity_attrs, dict):
                attr_rel_types.update(entity_attrs.keys())

        # Fetch definitions for all attribute rel_types at once
        attr_definitions = {}
        if attr_rel_types and db:
            try:
                with db.cursor() as cur:
                    placeholders = ",".join(["%s"] * len(attr_rel_types))
                    cur.execute(
                        f"SELECT rel_type, natural_language FROM rel_types WHERE rel_type IN ({placeholders})",
                        list(attr_rel_types)
                    )
                    attr_definitions = {row[0]: (row[1] or "") for row in cur.fetchall()}
            except Exception as e:
                log.warning("query.attributes_definitions_lookup_failed", error=str(e))

        for entity_id, entity_attrs in attributes.items():
            if not isinstance(entity_attrs, dict):
                continue
            for attribute, attr_data in entity_attrs.items():
                if isinstance(attr_data, dict):
                    value = attr_data.get("value")
                    category = attr_data.get("category")
                else:
                    # Bac${LOCATION}ard compat: legacy scalar values
                    value = attr_data
                    category = None
                if value is not None:
                    facts.append({
                        "subject": entity_id,
                        "rel_type": attribute,
                        "object": str(value),
                        "confidence": 1.0,
                        "fact_class": "A",
                        "fact_state": "long_term",
                        "category": category,
                        "definition": attr_definitions.get(attribute, ""),
                    })
        return facts

    # ── Taxonomy-driven query intent (dprompt-23) ──────────────────────────
    # Detect intent → load taxonomy → fetch ONLY rel_types in that taxonomy.
    # No hardcoded signal sets. Entity_taxonomies table IS the map.
    #
    # Generic fallback signals: queries that don't match a taxonomy but still
    # need graph traversal (e.g., "tell me about", "what is", bare questions).
    _GENERIC_SELF_REF_SIGNALS = {
        "tell me about", "what do you know", "what you know",
        "what is", "what are", "how many", "how much",
        "list all", "show me", "alicecribe",
    }

    # Initialize database connection and entity registry FIRST
    # so we can resolve the user's entity UUID
    db = None
    registry = None
    user_entity_id_for_query = None
    canonical_identity = None

    try:
        db = psycopg2.connect(os.environ.get("POSTGRES_DSN"))
        registry = EntityRegistry(db)

        # ═══════════════════════════════════════════════════════════════════════
        # dprompt-93: Semantic Query Resolution Framework
        # Three-step identity resolution: Self Anchor → Hierarchy → Ontology
        # ═══════════════════════════════════════════════════════════════════════

        # Step 1: Self Anchor — Check if user_id itself has identity facts
        candidate_entities = []
        with db.cursor() as cur:
            cur.execute(
                "SELECT f.subject_id, e.entity_type, f.confidence, f.created_at "
                "FROM facts f "
                "JOIN entities e ON e.id = f.subject_id "
                "WHERE f.user_id = %s AND f.subject_id = %s "
                "AND f.rel_type IN ('pref_name', 'also_known_as') "
                "AND f.superseded_at IS NULL "
                "ORDER BY f.confidence DESC, f.created_at DESC "
                "LIMIT 1",
                (user_id, user_id)
            )
            row = cur.fetchone()
            if row:
                candidate_entities.append((row[0], row[1], row[2], row[3], "self_anchor"))
                log.info("query.identity_self_anchor_found", entity_id=row[0][:8], entity_type=row[1])

        # Step 2: Hierarchy — If Self Anchor not found, query all identity candidates with types
        if not candidate_entities:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT f.subject_id, e.entity_type, f.confidence, f.created_at "
                    "FROM facts f "
                    "JOIN entities e ON e.id = f.subject_id "
                    "WHERE f.user_id = %s AND f.rel_type IN ('pref_name', 'also_known_as') "
                    "AND f.superseded_at IS NULL "
                    "ORDER BY f.confidence DESC, f.created_at DESC",
                    (user_id,)
                )
                candidate_entities = [(row[0], row[1], row[2], row[3], "hierarchy") for row in cur.fetchall()]

        # Keep full list of identity entities for downstream fact fetching (dprompt-88d)
        user_entity_ids_for_query = [ent[0] for ent in candidate_entities]

        # Step 3: Ontology — Score candidates by semantic type and instance_of chains
        # Query which entity_types appear in "user-centric" taxonomies (family, household, work)
        semantic_user_types = set()
        with db.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT UNNEST(member_entity_types) as entity_type "
                "FROM entity_taxonomies "
                "WHERE taxonomy_name IN ('family', 'household', 'work')"
            )
            semantic_user_types = {row[0] for row in cur.fetchall()}
            log.info("query.semantic_user_types_loaded", types=list(semantic_user_types))

        # Score candidates: prefer those in "user-centric" taxonomies
        scored_candidates = []
        for entity_id, entity_type, confidence, created_at, source in candidate_entities:
            score = 0.0

            # Bonus: entity_type in semantic user taxonomies
            if entity_type in semantic_user_types:
                score += 2.0

            # Bonus: confidence score
            score += confidence

            # Preserve created_at for deterministic tie-breaking (dprompt-93 robustness)
            scored_candidates.append((entity_id, entity_type, score, created_at, source))
            log.info("query.identity_candidate_scored", entity_id=entity_id[:8], entity_type=entity_type, score=score, source=source)

        # Pick highest-scoring candidate as canonical identity for context
        # Deterministic tie-breaking: score DESC, then created_at DESC (most recent wins)
        user_entity_id_for_query = None
        if scored_candidates:
            scored_candidates.sort(key=lambda x: (-x[2], -x[3].timestamp() if hasattr(x[3], 'timestamp') else 0), reverse=False)
            user_entity_id_for_query = scored_candidates[0][0]
            log.info("query.identity_resolved", entity_id=user_entity_id_for_query[:8], entity_type=scored_candidates[0][1], score=scored_candidates[0][2], method="semantic_scoring")

        # Fallback: if no identity facts found, create/resolve user entity
        # Defensive: validate fallback produces a valid UUID before using
        if not user_entity_id_for_query:
            fallback_id = registry.resolve(user_id, "user")
            if not fallback_id:
                from src.entity_registry.registry import _make_surrogate
                fallback_id = _make_surrogate(user_id, user_id)
            # Validate fallback_id is non-empty UUID (dprompt-93 robustness)
            if fallback_id and isinstance(fallback_id, str) and len(fallback_id) > 0:
                user_entity_id_for_query = fallback_id
                user_entity_ids_for_query = [fallback_id]
                log.info("query.identity_fallback_registry", entity_id=fallback_id[:8])
            else:
                log.warning("query.identity_fallback_failed", fallback_id=fallback_id)

        # Get canonical identity display name
        canonical_identity = None
        if user_entity_id_for_query:
            canonical_identity = registry.get_preferred_name(user_id, user_entity_id_for_query)
            if not canonical_identity or _UUID_PATTERN.match(canonical_identity):
                canonical_identity = user_id

        log.info("query.user_identity", owui_user_id=user_id, entity_id=user_entity_id_for_query[:8] if user_entity_id_for_query else "none", canonical=canonical_identity)
    except Exception as _e:
        log.warning("query.db_init_failed", error=str(_e))
        if db:
            try:
                db.rollback()
            except Exception:
                pass

    direct_facts = []
    detected_taxonomies = None
    query_detected_taxonomies = None

    # dprompt-27: always fetch baseline identity facts + graph traversal for connected entities
    # dprompt-88d: fetch from ALL user identity entities, not just the first one
    # SOLUTION-CLASSIFY-FORWARD: Use pre-computed taxonomies for scope filtering
    # Taxonomy detection: (1) query entity_taxonomies, (2) if none found, LLM discovers and creates new taxonomy
    if db and registry and canonical_identity:
        # dprompt-130: Determine query-driven scope BEFORE fetching facts
        # Matches query keywords against entity_taxonomies metadata (NOT hardcoded)
        query_detected_taxonomies = _determine_query_scope(db, request.text, user_id)

        # Fetch baseline facts, optionally filtered by query-detected scope
        # NOTE: Only pass taxonomies filter if facts table actually has taxonomies populated
        # For now, skip filtering since existing facts have empty taxonomies column
        # (taxonomy population is handled at ingest time for new facts)
        for eid in user_entity_ids_for_query:
            direct_facts.extend(_fetch_user_facts(db, user_id, entity_id=eid))

        # dprompt-130: Apply query-scope filtering to facts (post-fetch)
        # Only apply if query detected a specific scope (not broad queries)
        log.info("query.scope_filter_check", query_detected_taxonomies=list(query_detected_taxonomies) if query_detected_taxonomies else None,
                 count=len(query_detected_taxonomies) if query_detected_taxonomies else 0)
        if query_detected_taxonomies and len(query_detected_taxonomies) <= 3:
            # Build rel_type → taxonomies map to filter
            rel_type_to_taxonomies = {}
            try:
                with db.cursor() as cur:
                    cur.execute(
                        "SELECT DISTINCT rt.rel_type, et.taxonomy_name FROM rel_types rt "
                        "INNER JOIN entity_taxonomies et ON et.rel_types_defining_group @> ARRAY[rt.rel_type] "
                        "WHERE et.taxonomy_name = ANY(%s)",
                        (list(query_detected_taxonomies),)
                    )
                    results = cur.fetchall()
                    log.info("query.scope_filter_sql_results", results_count=len(results) if results else 0,
                             taxonomies=list(query_detected_taxonomies))
                    for rel_type, tax_name in results:
                        if rel_type not in rel_type_to_taxonomies:
                            rel_type_to_taxonomies[rel_type] = set()
                        rel_type_to_taxonomies[rel_type].add(tax_name)
            except Exception as e:
                log.warning("query.scope_filter_map_failed", error=str(e))
                rel_type_to_taxonomies = {}

            # Filter facts to only those whose rel_type belongs to detected scope
            if rel_type_to_taxonomies:
                initial_count = len(direct_facts)
                direct_facts = [
                    f for f in direct_facts
                    if f.get("rel_type") in rel_type_to_taxonomies
                ]
                log.info("query.scope_filter_applied", query_scope=list(query_detected_taxonomies),
                         before=initial_count, after=len(direct_facts))

        # dprompt-130: Skip fact-based scope detection if query-based already succeeded
        # Only try fact-based detection if query didn't detect a specific scope
        if direct_facts and not query_detected_taxonomies:
            try:
                fact_detected_taxonomies = determine_scope_multi_factor(db, user_id, direct_facts)
                # Use fact-detected scope if available
                if fact_detected_taxonomies:
                    detected_taxonomies = fact_detected_taxonomies
                    log.info("query.scope_detected_from_facts", taxonomies=list(detected_taxonomies),
                             fact_count=len(direct_facts))
                else:
                    # Fallback: LLM discovers what taxonomy these facts might define
                    log.info("query.no_taxonomy_detected_attempting_discovery", fact_count=len(direct_facts))
                    # dprompt-142: Removed qwen_api_url parameter — now uses centralized resolver
                    discovered = _llm_discover_taxonomy_from_facts(db, user_id, direct_facts)
                    if discovered:
                        # Create the taxonomy in DB
                        try:
                            with db.cursor() as cur:
                                cur.execute(
                                    "INSERT INTO entity_taxonomies "
                                    "(taxonomy_name, description, member_entity_types, rel_types_defining_group, "
                                    "has_transitivity, transitive_rel_types, is_hierarchical, parent_rel_type, source) "
                                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                                    (
                                        discovered["taxonomy_name"],
                                        discovered.get("description", ""),
                                        discovered.get("member_entity_types", "{}"),
                                        discovered.get("rel_types_defining_group", []),
                                        discovered.get("has_transitivity", False),
                                        discovered.get("transitive_rel_types", "{}"),
                                        discovered.get("is_hierarchical", False),
                                        discovered.get("parent_rel_type"),
                                        discovered.get("source", "llm_learned"),
                                    ),
                                )
                            # Reload taxonomy cache
                            _load_taxonomy_cache(db)
                            # Re-detect with the new taxonomy
                            detected_taxonomies = determine_scope_multi_factor(db, user_id, direct_facts)
                            log.info("query.taxonomy_created_and_detected",
                                     taxonomy_name=discovered["taxonomy_name"],
                                     taxonomies_detected=list(detected_taxonomies or []))
                        except Exception as e:
                            log.warning("query.taxonomy_creation_failed", error=str(e))
            except Exception as e:
                log.warning("query.scope_detection_failed", error=str(e))
                detected_taxonomies = None

        # Debug: log what facts fetched for user (dprompt-88b)
        user_rels = {}
        for f in direct_facts:
            rt = f.get("rel_type", "unknown")
            user_rels[rt] = user_rels.get(rt, 0) + 1
        log.info("query.initial_user_facts", count=len(direct_facts), rel_types=dict(sorted(user_rels.items())),
                 detected_taxonomies=list(detected_taxonomies or []))

        # dprompt-130: Stop on confidence — if scope-detected facts answer the query, return early
        # Query-detected scope is more trustworthy than graph traversal (which adds all connected facts)
        # If we detected a specific scope (1-3 taxonomies), scope-filtered facts are our answer
        if query_detected_taxonomies and direct_facts and len(query_detected_taxonomies) <= 2:
            _avg_confidence = sum(f.get("confidence", 0.5) for f in direct_facts) / len(direct_facts)
            log.info("query.stop_on_confidence", count=len(direct_facts), avg_confidence=_avg_confidence,
                     scope=list(query_detected_taxonomies),
                     reason="query_scope_detected_facts_sufficient")
            # Skip graph traversal and hierarchy expansion — scope-detected facts answer the query
            merged_facts = direct_facts
            try:
                # Populate preferred_names using helper (dprompt-61)
                preferred_names = _populate_preferred_names(merged_facts, direct_facts, [], [], {})
                entity_types = _build_entity_types(preferred_names)
                return {
                    "status": "ok",
                    "facts": merged_facts,
                    "preferred_names": _clean_preferred_names(preferred_names),
                    "canonical_identity": canonical_identity,
                    "entity_types": entity_types,
                    "attributes": {},
                }
            except Exception as e:
                log.warning("query.stop_on_confidence_failed", error=str(e))
                # Fall through to graph traversal if early return fails

    if direct_facts or (db and registry and canonical_identity):
        try:
            # Resolve named entities from query (e.g., "${ENTITY}", "system")
            # and fetch their facts for domain-agnostic queries.
            # _fetch_user_facts already searches both subject_id AND object_id
            # when entity_id is provided, so one call covers both directions.
            # Scan both capitalized words AND lowercase tokens that match known aliases.
            _query_words = set(re.findall(r'\b([A-Z][a-z]+)\b', request.text))
            _query_words.discard("Tell")
            # Also check lowercase tokens against entity_aliases
            _common_words = {'the','and','for','you','are','that','what','how','who','tell','know','about','with','from','your','this','have','been','does','will','name','system'}
            for _token in request.text.lower().split():
                _token = _token.strip('.,!?;:()[]{}"\'')
                if len(_token) > 2 and _token not in _common_words and _token not in _query_words:
                    _query_words.add(_token)
            for _word in _query_words:
                try:
                    _entity_id = registry.resolve(user_id, _word)
                    if _entity_id and _entity_id != user_entity_id_for_query:
                        _extra = _fetch_user_facts(db, user_id, entity_id=_entity_id)
                        if _extra:
                            direct_facts.extend(_extra)
                            log.info("query.entity_resolved",
                                     word=_word, entity_id=_entity_id,
                                     extra_facts=len(_extra))
                except Exception:
                    pass

            # dprompt-27: graph traversal — find connected entities via _REL_TYPE_GRAPH
            _connected = _graph_traverse(db, user_id, user_entity_id_for_query)
            if _connected:
                # dprompt-88: Removed taxonomy filtering (dprompt-47c was breaking queries).
                # Rationale: Graph traversal via _REL_TYPE_GRAPH is the authoritative filter.
                # Taxonomy constraints were filtering out necessary connected entities,
                # preventing both relationship and hierarchy facts from being returned.
                # Example: "where do I live?" would return instance_of but exclude lives_at
                # because taxonomy filter removed the address from _connected.
                # The graph itself filters what's connected; taxonomy filtering was redundant and harmful.

                log.info("query.graph_traverse", identity=canonical_identity,
                         connected_count=len(_connected))
                for _conn_id in _connected:
                    _conn_facts = _fetch_user_facts(db, user_id, entity_id=_conn_id)
                    # Debug: log facts for each connected entity (dprompt-88b)
                    if _conn_facts:
                        _conn_rels = {}
                        for f in _conn_facts:
                            rt = f.get("rel_type", "unknown")
                            _conn_rels[rt] = _conn_rels.get(rt, 0) + 1
                        log.info("query.connected_entity_facts", entity_id=_conn_id[:8], count=len(_conn_facts), rel_types=dict(sorted(_conn_rels.items())))
                    if _conn_facts:
                        seen = {(f["subject"], f["object"], f["rel_type"]) for f in direct_facts}
                        for _cf in _conn_facts:
                            _key = (_cf["subject"], _cf["object"], _cf["rel_type"])
                            if _key not in seen:
                                direct_facts.append(_cf)
                                seen.add(_key)
                log.info("query.graph_traverse_complete",
                         connected_entities=len(_connected),
                         total_facts=len(direct_facts))

                # dprompt-28: hierarchy expansion — enrich each connected entity with taxonomy
                _enriched = set(_connected)
                for _conn_id in _connected:
                    _upchain = _hierarchy_expand(db, user_id, _conn_id, direction="up", max_depth=3)
                    _enriched.update(_upchain)
                # Fetch facts for hierarchy entities not already covered
                _new_entities = _enriched - _connected - {user_entity_id_for_query}
                if _new_entities:
                    seen = {(f["subject"], f["object"], f["rel_type"]) for f in direct_facts}
                    for _hid in _new_entities:
                        _hfacts = _fetch_user_facts(db, user_id, entity_id=_hid)
                        if _hfacts:
                            for _hf in _hfacts:
                                _key = (_hf["subject"], _hf["object"], _hf["rel_type"])
                                if _key not in seen:
                                    direct_facts.append(_hf)
                                    seen.add(_key)
                    log.info("query.hierarchy_expanded",
                             enriched_entities=len(_enriched),
                             new_hierarchy_facts=sum(1 for f in direct_facts if f.get("rel_type") in _get_hierarchy_rels()),
                             total_facts=len(direct_facts))

                # dBug-019: fetch hierarchy facts for graph-connected entities
                # Graph traversal finds connected entities (pets, family) but doesn't
                # return their type/classification facts. This ensures instance_of,
                # subclass_of, member_of, part_of are included for complete context.
                _graph_entity_ids = set(_connected)
                if _graph_entity_ids:
                    _hier_facts = _fetch_hierarchy_facts(db, user_id, _graph_entity_ids)
                    if _hier_facts:
                        seen = {(f["subject"], f["object"], f["rel_type"]) for f in direct_facts}
                        _added = 0
                        for _hf in _hier_facts:
                            _key = (_hf["subject"], _hf["object"], _hf["rel_type"])
                            if _key not in seen:
                                direct_facts.append(_hf)
                                seen.add(_key)
                                _added += 1
                        log.info("query.hierarchy_facts_fetched",
                                 graph_entities=len(_graph_entity_ids),
                                 hierarchy_facts_added=_added,
                                 total_facts=len(direct_facts))

            entity_ids = list({f["subject"] for f in direct_facts} | {f["object"] for f in direct_facts})
            attributes = _fetch_attributes(db, user_id, entity_ids, max_sensitivity="private")

            # dBug-035: Log spouse facts at this point (after graph traversal)
            spouse_in_direct = [f for f in direct_facts if f.get("rel_type") == "spouse"]
            if spouse_in_direct:
                log.info("query.dBug035_spouse_after_graph_traversal",
                         count=len(spouse_in_direct),
                         spouse_facts=[{
                             "subject": f.get("subject", "?")[:8],
                             "object": f.get("object", "?")[:8],
                             "confidence": f.get("confidence"),
                             "fact_state": f.get("fact_state"),
                         } for f in spouse_in_direct])
            else:
                log.warning("query.dBug035_spouse_missing_after_graph_traversal",
                           direct_facts_total=len(direct_facts))

            if direct_facts:
                log.info("query.graph_traversal", identity=canonical_identity, hits=len(direct_facts))
                # Don't return early — merge with Qdrant results below
                # Postgres facts are authoritative; Qdrant adds associative context
        except Exception as _e:
            log.warning("query.graph_traversal_failed", error=str(_e))
        finally:
            pass

    # Named-entity attribute resolution
    # Triggered when query contains attribute signals
    # # NO RECURSIVE MATCHING — all comparisons use pre-lowercased query_lower only
    _ATTRIBUTE_SIGNALS = {
        "old", "age", "height", "tall", "weight", "heavy",
        "job", "work", "occupation", "born", "birthday",
        "live", "address", "home", "location",
    }

    _STOPWORDS = {
        "how", "what", "is", "are", "was", "the", "a", "an",
        "my", "your", "his", "her", "their", "our", "its",
        "do", "does", "did", "please", "tell", "me", "about"
    }

    query_lower = request.text.lower()
    has_attribute_signal = any(sig in query_lower for sig in _ATTRIBUTE_SIGNALS)
    log.info("query.entity_resolution.start", has_attribute_signal=has_attribute_signal, query_lower=query_lower)

    if has_attribute_signal and db:
        try:
            # Tokenize query, strip stopwords, get candidate name tokens
            tokens = [
                t.strip("?.,!").lower()
                for t in request.text.split()
                if t.strip("?.,!").lower() not in _STOPWORDS
                and len(t.strip("?.,!")) > 1
            ]
            log.info("query.entity_resolution.tokens", tokens=tokens)

            if tokens:
                # Resolve tokens against entity_aliases for this user
                placeholders = ",".join(["%s"] * len(tokens))
                with db.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT DISTINCT ea.entity_id, ea.alias
                        FROM entity_aliases ea
                        WHERE ea.user_id = %s
                          AND lower(ea.alias) IN ({placeholders})
                        """,
                        [user_id] + tokens
                    )
                    resolved = cur.fetchall()
                log.info("query.entity_resolution.resolved", resolved=[(r[0], r[1]) for r in resolved])

                if resolved:
                    resolved_ids = {row[0] for row in resolved}

                    # Verify each resolved entity is related to the user
                    # (appears as subject or object in an existing fact)
                    with db.cursor() as cur:
                        id_placeholders = ",".join(["%s"] * len(resolved_ids))
                        cur.execute(
                            f"""
                            SELECT DISTINCT subject_id, object_id
                            FROM facts
                            WHERE user_id = %s
                              AND superseded_at IS NULL
                              AND hard_delete_flag = false
                              AND (subject_id IN ({id_placeholders})
                                   OR object_id IN ({id_placeholders}))
                            """,
                            [user_id] + list(resolved_ids) + list(resolved_ids)
                        )
                        related_rows = cur.fetchall()

                    # Collect confirmed related entity IDs
                    confirmed_ids = set()
                    for row in related_rows:
                        if row[0] in resolved_ids:
                            confirmed_ids.add(row[0])
                        if row[1] in resolved_ids:
                            confirmed_ids.add(row[1])
                    log.info("query.entity_resolution.confirmed", confirmed_ids=list(confirmed_ids))
                    # Track resolved entities so we fetch their attributes later
                    resolved_entity_ids.update(confirmed_ids)

                    if confirmed_ids:
                        # Fetch facts anchored to confirmed entities
                        with db.cursor() as cur:
                            id_placeholders = ",".join(["%s"] * len(confirmed_ids))
                            cur.execute(
                                f"""
                                SELECT subject_id, object_id, rel_type, provenance, confidence
                                FROM facts
                                WHERE user_id = %s
                                  AND superseded_at IS NULL
                                  AND hard_delete_flag = false
                                  AND (subject_id IN ({id_placeholders})
                                       OR object_id IN ({id_placeholders}))
                                """,
                                [user_id] + list(confirmed_ids) + list(confirmed_ids)
                            )
                            entity_facts = cur.fetchall()

                        # Fetch attributes for confirmed entities
                        entity_attrs = _fetch_attributes(
                            db, user_id, list(confirmed_ids),
                            max_sensitivity="private"
                        )
                        log.info("query.entity_resolution.attributes", entity_attrs=entity_attrs)

                        # Explicitly fetch scalar facts for resolved entities
                        # (age, height, weight, born_on) to ensure they appear in facts array
                        scalar_facts = []
                        with db.cursor() as cur:
                            id_placeholders = ",".join(["%s"] * len(confirmed_ids))
                            cur.execute(
                                f"""
                                SELECT subject_id, object_id, rel_type, provenance, confidence
                                FROM facts
                                WHERE user_id = %s
                                  AND superseded_at IS NULL
                                  AND hard_delete_flag = false
                                  AND rel_type IN ('age', 'height', 'weight', 'born_on')
                                  AND subject_id IN ({id_placeholders})
                                """,
                                [user_id] + list(confirmed_ids)
                            )
                            scalar_facts = [
                                {
                                    "subject": row[0],
                                    "object": row[1],
                                    "rel_type": row[2],
                                    "provenance": row[3],
                                    "confidence": float(row[4]) if row[4] else 1.0,
                                    "category": "physical"
                                }
                                for row in cur.fetchall()
                            ]
                        log.info("query.entity_resolution.scalar_facts", count=len(scalar_facts))

                        # Merge into direct_facts and attributes
                        for row in entity_facts:
                            fact = {
                                "subject": row[0],
                                "object": row[1],
                                "rel_type": row[2],
                                "provenance": row[3],
                                "confidence": float(row[4]) if row[4] else 0.0,
                                "category": _get_rel_type_category(row[2])
                                            or _infer_category(row[2])
                            }
                            if fact not in direct_facts:
                                direct_facts.append(fact)

                        # Merge scalar facts into direct_facts (avoid duplicates)
                        existing_keys = {(f["subject"], f["object"], f["rel_type"]) for f in direct_facts}
                        for sf in scalar_facts:
                            key = (sf["subject"], sf["object"], sf["rel_type"])
                            if key not in existing_keys:
                                direct_facts.append(sf)
                                existing_keys.add(key)

                        for entity_id, attr_dict in entity_attrs.items():
                            if entity_id not in attributes:
                                attributes[entity_id] = attr_dict
                            else:
                                attributes[entity_id].update(attr_dict)
        except Exception as e:
            log.error("query.entity_resolution.error", error=str(e))

    # Embed after graph traversal so Postgres results are returned even when the
    # embedding service is unavailable. fallback=False: skip Qdrant rather than
    # searching with a hash vector that can't match nomic-embedded stored facts.
    vector = embed_text(request.text, qwen_api_url, timeout=10.0, fallback=False, embedding_url=_EMBEDDING_API_URL)
    if vector is None:
        log.warning("query.embed_unavailable — skipping Qdrant search")
        # Resolve display names for Postgres facts before returning
        resolved_baseline = _resolve_display_names(baseline_facts, registry, user_id, user_entity_id_for_query) if registry else baseline_facts
        resolved_direct = _resolve_display_names(direct_facts, registry, user_id, user_entity_id_for_query) if registry else direct_facts
        _attr_db = None
        try:
            _attr_db = psycopg2.connect(os.environ.get("POSTGRES_DSN"))
            _early_ids = list({user_entity_id_for_query} | {f["subject"] for f in resolved_direct + resolved_baseline} | {f["object"] for f in resolved_direct + resolved_baseline} | resolved_entity_ids)
            attributes = _fetch_attributes(_attr_db, user_id, _early_ids, max_sensitivity="private")
            _attr_db.commit()
        except Exception:
            if _attr_db:
                try:
                    _attr_db.rollback()
                except Exception:
                    pass
        finally:
            if _attr_db:
                try:
                    _attr_db.close()
                except Exception:
                    pass
        attr_facts = _attributes_to_facts(attributes)
        log.info("query.embed_fail_attributes_to_facts", count=len(attr_facts), attributes_keys=list(attributes.keys()) if attributes else [])
        resolved_attr_facts = _resolve_display_names(attr_facts, registry, user_id, user_entity_id_for_query) if registry else attr_facts
        merged_facts = resolved_direct + resolved_baseline + resolved_attr_facts
        events = _fetch_user_events(db, user_id)
        if events:
            events_resolved = _resolve_display_names(events, registry, user_id, user_entity_id_for_query) if registry else events
            merged_facts.extend(events_resolved)
        log.info("query.embed_fail_merged", attr_count=len(resolved_attr_facts), total=len(merged_facts))
        # Fetch attributes for preferred_names population (dprompt-61)
        _attr_db = None
        try:
            _attr_db = psycopg2.connect(os.environ.get("POSTGRES_DSN"))
            pre_resolution_fact_ids = {user_entity_id_for_query} | resolved_entity_ids
            for f in direct_facts + baseline_facts:
                if _UUID_PATTERN.match(f.get("subject", "")):
                    pre_resolution_fact_ids.add(f["subject"])
                if _UUID_PATTERN.match(f.get("object", "")):
                    pre_resolution_fact_ids.add(f["object"])
            attributes = _fetch_attributes(_attr_db, user_id, list(pre_resolution_fact_ids), max_sensitivity="private")
            _attr_db.commit()
        except Exception:
            if _attr_db:
                try:
                    _attr_db.rollback()
                except Exception:
                    pass
            attributes = {}
        finally:
            if _attr_db:
                try:
                    _attr_db.close()
                except Exception:
                    pass
        preferred_names = _populate_preferred_names(merged_facts, direct_facts, baseline_facts, [], attributes)
        entity_types = _build_entity_types(preferred_names)

        # Debug: log what facts are being returned (dprompt-88)
        # dBug-035: Enhanced logging to trace spouse fact through pipeline
        rel_types_returned = {}
        spouse_facts = []
        for f in merged_facts:
            rt = f.get("rel_type", "unknown")
            rel_types_returned[rt] = rel_types_returned.get(rt, 0) + 1
            if rt == "spouse":
                spouse_facts.append({
                    "subject": f.get("subject", "?")[:8],
                    "object": f.get("object", "?")[:8],
                    "confidence": f.get("confidence"),
                    "fact_state": f.get("fact_state"),
                    "fact_class": f.get("fact_class"),
                })
        log.info("query.facts_summary",
                 total=len(merged_facts),
                 rel_types=dict(sorted(rel_types_returned.items())),
                 spouse_count=len(spouse_facts),
                 spouse_facts=spouse_facts,
                 query=request.text[:100])

        # dprompt-91: Apply archive filtering (multi-factor scope + temporality)
        try:
            filtered_facts, archive_metadata = apply_archive_filter(
                db, query_lower, user_id, merged_facts
            )
            log.info("query.archive_filtering_applied", **archive_metadata)
            merged_facts = filtered_facts
        except Exception as e:
            log.error("query.archive_filter_failed", error=str(e))
            raise  # HARD FAIL: archive filter failure is non-recoverable

        return {
            "status": "ok",
            "facts": merged_facts,
            "preferred_names": _clean_preferred_names(preferred_names),
            "canonical_identity": canonical_identity,
            "entity_types": entity_types,
            "attributes": attributes,
        }

    try:
        resp = _http_client_sync.post(
            f"{qdrant_url}/collections/{collection}/points/search",
            json={"vector": vector, "limit": 10, "with_payload": True, "score_threshold": 0.3},
            timeout=10.0,
        )
        if resp.status_code == 404:
            ensure_collection(collection, qdrant_url)
            resolved_baseline = _resolve_display_names(baseline_facts, registry, user_id, user_entity_id_for_query) if registry else baseline_facts
            resolved_direct = _resolve_display_names(direct_facts, registry, user_id, user_entity_id_for_query) if registry else direct_facts
            _attr_db = None
            try:
                _attr_db = psycopg2.connect(os.environ.get("POSTGRES_DSN"))
                _early_ids = list({user_entity_id_for_query} | {f["subject"] for f in resolved_direct + resolved_baseline} | {f["object"] for f in resolved_direct + resolved_baseline} | resolved_entity_ids)
                attributes = _fetch_attributes(_attr_db, user_id, _early_ids, max_sensitivity="private")
                _attr_db.commit()
            except Exception:
                if _attr_db:
                    try:
                        _attr_db.rollback()
                    except Exception:
                        pass
            finally:
                if _attr_db:
                    try:
                        _attr_db.close()
                    except Exception:
                        pass
            attr_facts = _attributes_to_facts(attributes)
            log.info("query.404_attributes_to_facts", count=len(attr_facts), attributes_keys=list(attributes.keys()) if attributes else [])
            resolved_attr_facts = _resolve_display_names(attr_facts, registry, user_id, user_entity_id_for_query) if registry else attr_facts
            merged_facts = resolved_direct + resolved_baseline + resolved_attr_facts
            events = _fetch_user_events(db, user_id)
            if events:
                events_resolved = _resolve_display_names(events, registry, user_id, user_entity_id_for_query) if registry else events
                merged_facts.extend(events_resolved)
            log.info("query.404_merged", attr_count=len(resolved_attr_facts), total=len(merged_facts))
            # Populate preferred_names (dprompt-61)
            preferred_names = _populate_preferred_names(merged_facts, direct_facts, baseline_facts, [], attributes)
            entity_types_404 = _build_entity_types(preferred_names)

            # dprompt-91: Apply archive filtering (404 case)
            try:
                filtered_facts, archive_metadata = apply_archive_filter(
                    db, query_lower, user_id, merged_facts
                )
                log.info("query.404.archive_filtering_applied", **archive_metadata)
                merged_facts = filtered_facts
            except Exception as e:
                log.error("query.404.archive_filter_failed", error=str(e))
                raise

            return {
                    "status": "ok",
                    "facts": merged_facts,
                    "preferred_names": _clean_preferred_names(preferred_names),
                    "canonical_identity": canonical_identity,
                    "entity_types": entity_types_404,
                    "attributes": attributes,
                }
        if resp.status_code != 200:
            log.warning("query.qdrant_error", status=resp.status_code, collection=collection)
            resolved_baseline = _resolve_display_names(baseline_facts, registry, user_id, user_entity_id_for_query) if registry else baseline_facts
            resolved_direct = _resolve_display_names(direct_facts, registry, user_id, user_entity_id_for_query) if registry else direct_facts
            _attr_db = None
            try:
                _attr_db = psycopg2.connect(os.environ.get("POSTGRES_DSN"))
                _early_ids = list({user_entity_id_for_query} | {f["subject"] for f in resolved_direct + resolved_baseline} | {f["object"] for f in resolved_direct + resolved_baseline} | resolved_entity_ids)
                attributes = _fetch_attributes(_attr_db, user_id, _early_ids, max_sensitivity="private")
                _attr_db.commit()
            except Exception:
                if _attr_db:
                    try:
                        _attr_db.rollback()
                    except Exception:
                        pass
            finally:
                if _attr_db:
                    try:
                        _attr_db.close()
                    except Exception:
                        pass
            attr_facts = _attributes_to_facts(attributes)
            log.info("query.error_attributes_to_facts", count=len(attr_facts), attributes_keys=list(attributes.keys()) if attributes else [])
            resolved_attr_facts = _resolve_display_names(attr_facts, registry, user_id, user_entity_id_for_query) if registry else attr_facts
            merged_facts = resolved_direct + resolved_baseline + resolved_attr_facts
            events = _fetch_user_events(db, user_id)
            if events:
                events_resolved = _resolve_display_names(events, registry, user_id, user_entity_id_for_query) if registry else events
                merged_facts.extend(events_resolved)
            log.info("query.error_merged", attr_count=len(resolved_attr_facts), total=len(merged_facts))
            # Populate preferred_names (dprompt-61)
            preferred_names = _populate_preferred_names(merged_facts, direct_facts, baseline_facts, [], attributes)
            entity_types_err = _build_entity_types(preferred_names)

            # dprompt-91: Apply archive filtering (error case)
            try:
                filtered_facts, archive_metadata = apply_archive_filter(
                    db, query_lower, user_id, merged_facts
                )
                log.info("query.error.archive_filtering_applied", **archive_metadata)
                merged_facts = filtered_facts
            except Exception as e:
                log.error("query.error.archive_filter_failed", error=str(e))
                raise

            return {
                    "status": "ok",
                    "facts": merged_facts,
                    "preferred_names": _clean_preferred_names(preferred_names),
                    "canonical_identity": canonical_identity,
                    "entity_types": entity_types_err,
                    "attributes": attributes,
                }

        qdrant_facts = [
            {
                "subject": h["payload"].get("subject"),
                "object": h["payload"].get("object"),
                "rel_type": h["payload"].get("rel_type"),
                "provenance": h["payload"].get("provenance"),
                "confidence": h["payload"].get("confidence", 1.0),
                "category": _get_rel_type_category(h["payload"].get("rel_type") or ""),
                "fact_state": (
                    "long_term" if h["payload"].get("fact_class") in ("A", None)
                    else "staged" if h["payload"].get("fact_class") == "B"
                    else "ephemeral"
                ),
                "fact_class": h["payload"].get("fact_class", "A"),
                "definition": "",  # Will be populated from rel_types below
            }
            for h in resp.json().get("result", [])
            if h.get("payload")
        ]

        # Fetch definitions from rel_types for Qdrant facts (dprompt-filter-nl)
        if qdrant_facts:
            try:
                rel_types_in_qdrant = set(f["rel_type"] for f in qdrant_facts if f.get("rel_type"))
                if rel_types_in_qdrant:
                    with db.cursor() as cur:
                        placeholders = ",".join(["%s"] * len(rel_types_in_qdrant))
                        cur.execute(
                            f"SELECT rel_type, natural_language FROM rel_types WHERE rel_type IN ({placeholders})",
                            list(rel_types_in_qdrant)
                        )
                        definitions = {row[0]: (row[1] or "") for row in cur.fetchall()}
                    for f in qdrant_facts:
                        f["definition"] = definitions.get(f.get("rel_type"), "")
            except Exception as e:
                log.warning("query.qdrant_definitions_lookup_failed", error=str(e))

        log.info("query.ok", collection=collection, hits=len(qdrant_facts))

        # Resolve display names for all facts before merging
        resolved_baseline = _resolve_display_names(baseline_facts, registry, user_id, user_entity_id_for_query) if registry else baseline_facts
        resolved_direct = _resolve_display_names(direct_facts, registry, user_id, user_entity_id_for_query) if registry else direct_facts
        resolved_qdrant = _resolve_display_names(qdrant_facts, registry, user_id, user_entity_id_for_query) if registry else qdrant_facts

        # Merge: Postgres facts are authoritative, Qdrant adds associative context
        # Deduplicate on (subject_uuid, rel_type, object_uuid) — Postgres wins.
        # Use UUIDs from _subject_id/_object_id, not display names, because same
        # UUID may have different aliases (e.g., "${USER}" vs "user" for user entity).
        pg_keys = {(f.get("_subject_id", f["subject"]), f.get("_object_id", f["object"]), f["rel_type"]) for f in resolved_direct}
        merged_facts = resolved_direct.copy()
        for f in resolved_qdrant:
            key = (f.get("_subject_id", f["subject"]), f.get("_object_id", f["object"]), f["rel_type"])
            if key not in pg_keys:
                merged_facts.append(f)
                pg_keys.add(key)

        # Merge baseline personal facts (location, attributes) — always present for
        # known identities regardless of whether Qdrant or graph traversal returned them.
        for f in resolved_baseline:
            key = (f.get("_subject_id", f["subject"]), f.get("_object_id", f["object"]), f["rel_type"])
            if key not in pg_keys:
                merged_facts.append(f)
                pg_keys.add(key)

        # Extract entity IDs from PRE-RESOLUTION facts to get UUIDs, not display names
        pre_resolution_fact_ids = {user_entity_id_for_query} | resolved_entity_ids
        for f in direct_facts + baseline_facts + qdrant_facts:
            if _UUID_PATTERN.match(f.get("subject", "")):
                pre_resolution_fact_ids.add(f["subject"])
            if _UUID_PATTERN.match(f.get("object", "")):
                pre_resolution_fact_ids.add(f["object"])

        _attr_db = None
        try:
            _attr_db = psycopg2.connect(os.environ.get("POSTGRES_DSN"))
            attributes = _fetch_attributes(_attr_db, user_id, list(pre_resolution_fact_ids), max_sensitivity="private")
            _attr_db.commit()
        except Exception as _ae:
            if _attr_db:
                try:
                    _attr_db.rollback()
                except Exception:
                    pass
            log.warning("query.qdrant_attributes_failed", error=str(_ae))
            attributes = {}
        finally:
            if _attr_db:
                try:
                    _attr_db.close()
                except Exception:
                    pass

        # Merge user entity_attributes as facts (born_on, age, height, etc.)
        attr_facts = _attributes_to_facts(attributes)
        log.info("query.attributes_to_facts", count=len(attr_facts), attributes_keys=list(attributes.keys()) if attributes else [])
        resolved_attr_facts = _resolve_display_names(attr_facts, registry, user_id, user_entity_id_for_query) if registry else attr_facts
        attr_added = 0
        for f in resolved_attr_facts:
            # dprompt-92: Use UUID keys for dedup to match other facts (baseline, qdrant)
            # _subject_id/_object_id preserve UUIDs, same display names may vary by alias
            key = (f.get("_subject_id", f["subject"]), f.get("_object_id", f["object"]), f["rel_type"])
            if key not in pg_keys:
                merged_facts.append(f)
                pg_keys.add(key)
                attr_added += 1
        log.info("query.attributes_merged", added=attr_added, total_after=len(merged_facts))

        # Populate preferred_names (dprompt-61) — use extracted attributes from above
        preferred_names = _populate_preferred_names(merged_facts, direct_facts, baseline_facts, qdrant_facts, attributes)

        log.info("query.merged", pg_hits=len(pg_keys), baseline=len(resolved_baseline), total=len(merged_facts))

        # ── dprompt-61: Deduplicate by entity UUID + attach alias metadata ──
        # Facts may have different display names for the same entity_id
        # (e.g., john spouse emma AND ${USER} spouse emma). Deduplicate
        # using underlying UUIDs (_subject_id, _object_id) preserved by _resolve_display_names.
        _deduped: dict[tuple, dict] = {}
        for _f in merged_facts:
            _sid = _f.pop("_subject_id", _f.get("subject", ""))
            _oid = _f.pop("_object_id", _f.get("object", ""))
            _key = (_sid, _f.get("rel_type", ""), _oid)
            if _key not in _deduped or _deduped[_key].get("confidence", 0) < _f.get("confidence", 0):
                _deduped[_key] = _f

        # Attach alias metadata for each deduplicated fact
        _aliased_facts = []
        for (_sid, _rel, _oid), _f in _deduped.items():
            _f["_aliases"] = {
                "subject": _get_entity_aliases(_sid),
                "object": _get_entity_aliases(_oid),
            }
            _aliased_facts.append(_f)

        merged_facts = _aliased_facts
        log.info("query.deduplicated", before=len(pg_keys), after=len(merged_facts))
        # ── end dprompt-61 ──────────────────────────────────────────────────

        # dprompt-91: Apply archive filtering (main success case)
        try:
            filtered_facts, archive_metadata = apply_archive_filter(
                db, query_lower, user_id, merged_facts
            )
            log.info("query.success.archive_filtering_applied", **archive_metadata)
            merged_facts = filtered_facts
        except Exception as e:
            log.error("query.success.archive_filter_failed", error=str(e))
            raise

        # Strip internal metadata before returning to Filter
        _INTERNAL_KEYS = ("user_id", "qdrant_synced", "superseded_at", "fact_class", "promoted_at", "confirmed_count")
        for _f in merged_facts:
            for _k in _INTERNAL_KEYS:
                _f.pop(_k, None)

        entity_types = _build_entity_types(preferred_names)
        return {
            "status": "ok",
            "facts": merged_facts,
            "preferred_names": _clean_preferred_names(preferred_names),
            "canonical_identity": canonical_identity,
            "entity_types": entity_types,
            "attributes": attributes,
        }
    except Exception as e:
        log.error("query.failed", error=str(e))
        return {
            "status": "ok",
            "facts": [],
            "entity_types": {},
            "preferred_names": _clean_preferred_names(preferred_names),
            "canonical_identity": canonical_identity,
            "attributes": {},
        }
    finally:
        # === ATOMIC CLEANUP: Close connection after entire query completes ===
        if db:
            try:
                db.close()
            except Exception:
                pass

# ── SURGICAL FACT CORRECTION ENDPOINT ────────────────────────────────────────
@app.post("/retract/correct", response_model=FactCorrectionResponse)
def correct_fact(req: FactCorrectionRequest):
    """
    SURGICAL fact correction: user-truth-driven, atomic, per-user scoped.

    Flow:
    1. Extract correction specs (LLM, metadata-driven prompt)
    2. Validate per-user scoping (security)
    3. Verify old fact exists (exactly this one)
    4. Atomic transaction:
       a. Supersede old fact with timestamp
       b. Re-ingest new fact through WGMValidationGate (same as /ingest)
       c. Track outcome
    5. Return corrected state

    User truth principle: Class A facts (user-stated) always override immutability.
    Zero custom validation: uses same WGMValidationGate as /ingest.
    """
    db = None
    try:
        db = psycopg2.connect(os.environ.get("POSTGRES_DSN"))

        # Idempotency check: deduplicate retried correction requests (uses global _idempotency_mgr from startup)
        if req.idempotency_key and _idempotency_mgr:
            try:
                if _idempotency_mgr.is_duplicate(req.idempotency_key):
                    cached = _idempotency_mgr.get_cached_response(req.idempotency_key)
                    if cached:
                        log.info("correct_fact.idempotent_cached", idempotency_key=req.idempotency_key)
                        return FactCorrectionResponse(**cached) if isinstance(cached, dict) else cached
            except Exception as e:
                log.warning("correct_fact.idempotency_check_failed", error=str(e))
                # Continue without idempotency if cache fails

        # Stage 1: Detect intent type (correction vs. retraction)
        text_lower = req.text.lower()
        retraction_signals = {"forget", "don't have", "don't own", "is not", "is+not", "never had", "remove", "delete", "erase", "negate"}
        is_retraction = any(signal in text_lower for signal in retraction_signals)

        # Stage 2: LLM extraction (metadata-driven)
        # Per dprompt-143: Extract semantic intent (subject, rel_type, object, action, dimension)
        # Do NOT try to resolve entities or fetch context — just extract intent
        log.info("correct_fact.extraction_start",
                user_id=req.user_id,
                text_len=len(req.text),
                intent_type="retraction" if is_retraction else "correction",
                idempotency_key=req.idempotency_key)

        try:
            if is_retraction:
                # Retraction intent: extract subject, rel_type, object, action, dimension
                extraction = _retraction_intent_extraction_llm(
                    text=req.text,
                    user_id=req.user_id,
                    context_facts=req.context_facts or [],
                    db=db
                )
            else:
                # Correction intent: extract old_rel_type, old_value, new_rel_type, new_value
                extraction = _unified_correction_extraction_llm(
                    text=req.text,
                    user_id=req.user_id,
                    context_facts=req.context_facts or [],
                    db=db
                )
        except (TimeoutError, httpx.TimeoutException) as e:
            # Fail-fast: write timeout failure to idempotency cache immediately (prevents retry loop)
            failure_result = FactCorrectionResponse(
                status="failed",
                message="Correction extraction timed out (LLM unavailable). Try again later."
            )
            if req.idempotency_key and _idempotency_mgr:
                try:
                    _idempotency_mgr.cache_response(req.idempotency_key, failure_result.model_dump())
                    log.info("correct_fact.timeout_cached", idempotency_key=req.idempotency_key)
                except Exception:
                    pass
            log.warning("correct_fact.extraction_timeout", idempotency_key=req.idempotency_key, error=str(e))
            return failure_result

        # PART 2: ENTITY RESOLUTION (dprompt-146 Ingest Alignment Phase 1)
        # LLM extracts entity NAMES, backend must resolve to UUIDs
        # This requires DB queries BEFORE transaction — fresh context
        subject_name = extraction.get("subject") or extraction.get("subject_uuid")  # Fallback to subject_uuid if already resolved
        subject_uuid = None
        old_rel_type = extraction.get("old_rel_type", "").lower()
        old_value = extraction.get("old_value")
        new_rel_type = extraction.get("new_rel_type", "").lower()
        new_value = extraction.get("new_value")
        confidence = extraction.get("confidence", 0.0)

        # Resolve subject name → UUID using EntityRegistry (same as /ingest)
        # Special case: "user" or "me" → use user_id directly
        if subject_name and subject_name.lower() in ("user", "me", "i"):
            subject_uuid = req.user_id
        elif subject_name:
            try:
                registry = EntityRegistry(db)
                # Query DB: find entity by display name
                subject_uuid = registry.resolve(subject_name, req.user_id)
                log.info("correct_fact.entity_resolved",
                        subject_name=subject_name,
                        subject_uuid=subject_uuid)
            except Exception as e:
                log.warning("correct_fact.entity_resolution_failed",
                           subject_name=subject_name,
                           error=str(e))
                subject_uuid = None

        # Validate extraction
        if not subject_uuid or not old_rel_type or not new_rel_type:
            log.warning("correct_fact.extraction_incomplete",
                       subject_name=subject_name,
                       subject_uuid=subject_uuid,
                       old_rel_type=old_rel_type,
                       new_rel_type=new_rel_type)
            return FactCorrectionResponse(
                status="failed",
                message=f"Could not resolve entity '{subject_name}' or incomplete correction details"
            )

        log.info("correct_fact.extraction_success",
                user_id=req.user_id,
                subject=subject_uuid,
                old=f"{old_rel_type}={old_value}",
                new=f"{new_rel_type}={new_value}",
                dimension=extraction.get("dimension", "RELATIONAL"),
                confidence=confidence)

        # PHASE 6: Immutability enforcement (fail-fast, before transaction)
        IMMUTABLE_REL_TYPES = {"born_on", "born_in", "nationality"}
        if old_rel_type in IMMUTABLE_REL_TYPES:
            log.info("correct_fact.immutable_rejected",
                    user_id=req.user_id,
                    rel_type=old_rel_type)
            return FactCorrectionResponse(
                status="rejected",
                message=f"Field '{old_rel_type}' is immutable and cannot be corrected. "
                        f"Identity facts like birth date and birthplace cannot be changed once established."
            )

        # Stage 2: ATOMIC TRANSACTION (dimension-aware execution)
        with db.cursor() as cur:
            manager = FactStoreManager(db)
            gate = WGMValidationGate(db)
            registry = EntityRegistry(db)

            dimension = extraction.get("dimension", "RELATIONAL").upper()
            affected_ids = []

            # ═════════════════════════════════════════════════════════════════════════════
            # DIMENSION 1: SCALAR (age, name, occupation, etc.)
            # ═════════════════════════════════════════════════════════════════════════════
            if dimension == "SCALAR":
                log.info("correct_fact.dimension_scalar_start",
                        entity=subject_uuid,
                        attribute=old_rel_type,
                        old_value=old_value,
                        new_value=new_value)

                # Special case: pref_name (delete from entity_aliases, re-register new name)
                if old_rel_type in ["pref_name", "also_known_as"]:
                    # Delete old preferred name from entity_aliases
                    cur.execute("""
                        DELETE FROM entity_aliases
                        WHERE user_id = %s AND entity_id = %s AND is_preferred = true
                    """, (req.user_id, subject_uuid))
                    log.info("correct_fact.scalar_old_pref_name_deleted",
                            entity=subject_uuid,
                            old_name=old_value)

                    # Register new preferred name
                    try:
                        cur.execute("""
                            INSERT INTO entity_aliases (user_id, entity_id, alias, is_preferred)
                            VALUES (%s, %s, %s, true)
                            ON CONFLICT (user_id, alias) DO UPDATE
                            SET is_preferred = true, entity_id = EXCLUDED.entity_id
                        """, (req.user_id, subject_uuid, new_value.lower()))
                        log.info("correct_fact.scalar_new_pref_name_registered",
                                entity=subject_uuid,
                                new_name=new_value)
                    except Exception as e:
                        log.error("correct_fact.scalar_pref_name_registration_failed",
                                error=str(e), entity=subject_uuid, new_name=new_value)
                        raise

                else:
                    # Standard scalar update: entity_attributes table
                    # Determine new value type (int, float, date, or text)
                    value_int = None
                    value_float = None
                    value_date = None
                    value_text = new_value

                    try:
                        value_int = int(new_value)
                    except (ValueError, TypeError):
                        try:
                            value_float = float(new_value)
                        except (ValueError, TypeError):
                            try:
                                from datetime import datetime
                                value_date = datetime.fromisoformat(new_value).date()
                            except (ValueError, TypeError):
                                # Keep as text
                                pass

                    # Surgical update: only update the target attribute for this entity
                    cur.execute("""
                        UPDATE entity_attributes
                        SET value_text = %s, value_int = %s, value_float = %s, value_date = %s, updated_at = now()
                        WHERE user_id = %s AND entity_id = %s AND attribute = %s
                    """, (value_text, value_int, value_float, value_date, req.user_id, subject_uuid, old_rel_type))

                    log.info("correct_fact.scalar_updated",
                            entity=subject_uuid,
                            attribute=old_rel_type,
                            old_value=old_value,
                            new_value=new_value)

            # ═════════════════════════════════════════════════════════════════════════════
            # DIMENSION 2 & 3: RELATIONAL & HIERARCHICAL (facts table, symmetric/inverse)
            # ═════════════════════════════════════════════════════════════════════════════
            elif dimension in ["RELATIONAL", "HIERARCHICAL"]:
                log.info(f"correct_fact.dimension_{dimension.lower()}_start",
                        subject=subject_uuid,
                        rel_type=old_rel_type,
                        old_value=old_value,
                        new_value=new_value)

                # Verify old fact exists
                cur.execute("""
                    SELECT id, object_id FROM facts
                    WHERE user_id = %s AND subject_id = %s AND rel_type = %s
                      AND superseded_at IS NULL
                    LIMIT 1
                """, (req.user_id, subject_uuid, old_rel_type))

                old_fact_row = cur.fetchone()
                if not old_fact_row:
                    log.warning("correct_fact.old_fact_not_found",
                               subject=subject_uuid,
                               rel_type=old_rel_type,
                               old_value=old_value,
                               dimension=dimension)
                    return FactCorrectionResponse(
                        status="failed",
                        message=f"Old {dimension.lower()} fact not found: {old_rel_type}"
                    )

                old_fact_id = old_fact_row[0]
                old_fact_object_id = old_fact_row[1]
                affected_ids = [old_fact_id]

                # Supersede old fact
                cur.execute("""
                    UPDATE facts
                    SET superseded_at = now(), qdrant_synced = false
                    WHERE id = %s
                """, (old_fact_id,))
                log.info("correct_fact.old_fact_superseded",
                        fact_id=old_fact_id,
                        rel_type=old_rel_type)

                # Resolve new_value to entity UUID (case-insensitive)
                new_value_uuid = registry.resolve(
                    req.user_id,
                    new_value.lower().strip()
                )

                # If rel_type changed, validate new rel_type metadata
                if old_rel_type != new_rel_type:
                    new_edges = [{
                        "subject": subject_uuid,
                        "object": new_value_uuid,
                        "rel_type": new_rel_type
                    }]
                    gate.validate_edges(new_edges)
                    log.info("correct_fact.new_rel_type_validated",
                            new_rel_type=new_rel_type)

                # Create new fact (Class A, confidence 1.0)
                new_edges = [{
                    "subject": subject_uuid,
                    "object": new_value_uuid,
                    "rel_type": new_rel_type
                }]

                committed = manager.commit_facts(
                    cur,
                    user_id=req.user_id,
                    edges=new_edges,
                    fact_class="A",
                    confidence=1.0,
                    provenance="user_correction"
                )

                log.info("correct_fact.new_fact_committed",
                        subject=subject_uuid,
                        rel_type=new_rel_type,
                        new_object=new_value_uuid,
                        dimension=dimension)

            # ═════════════════════════════════════════════════════════════════════════════
            # DIMENSION 4: SUBJECT (fact about wrong entity)
            # ═════════════════════════════════════════════════════════════════════════════
            elif dimension == "SUBJECT":
                log.info("correct_fact.dimension_subject_start",
                        old_subject=subject_uuid,
                        new_subject=new_value,
                        rel_type=old_rel_type)

                # Verify old fact exists with current subject
                cur.execute("""
                    SELECT id, object_id FROM facts
                    WHERE user_id = %s AND subject_id = %s AND rel_type = %s
                      AND superseded_at IS NULL
                    LIMIT 1
                """, (req.user_id, subject_uuid, old_rel_type))

                old_fact_row = cur.fetchone()
                if not old_fact_row:
                    log.warning("correct_fact.old_fact_not_found_subject_change",
                               subject=subject_uuid,
                               rel_type=old_rel_type)
                    return FactCorrectionResponse(
                        status="failed",
                        message=f"Old fact not found for subject change: {old_rel_type}"
                    )

                old_fact_id = old_fact_row[0]
                old_object_id = old_fact_row[1]
                affected_ids = [old_fact_id]

                # Supersede old fact
                cur.execute("""
                    UPDATE facts
                    SET superseded_at = now(), qdrant_synced = false
                    WHERE id = %s
                """, (old_fact_id,))

                # Resolve new subject to entity UUID
                new_subject_uuid = registry.resolve(
                    req.user_id,
                    new_value.lower().strip()
                )

                # Create new fact with corrected subject, same rel_type and object
                new_edges = [{
                    "subject": new_subject_uuid,
                    "object": old_object_id,
                    "rel_type": old_rel_type
                }]

                committed = manager.commit_facts(
                    cur,
                    user_id=req.user_id,
                    edges=new_edges,
                    fact_class="A",
                    confidence=1.0,
                    provenance="user_correction"
                )

                log.info("correct_fact.subject_corrected",
                        old_subject=subject_uuid,
                        new_subject=new_subject_uuid,
                        rel_type=old_rel_type)

                # Update subject_uuid for tracking
                subject_uuid = new_subject_uuid

            # ═════════════════════════════════════════════════════════════════════════════
            # DIMENSION 5: REL_TYPE (relationship type changed)
            # ═════════════════════════════════════════════════════════════════════════════
            elif dimension == "REL_TYPE":
                log.info("correct_fact.dimension_rel_type_start",
                        subject=subject_uuid,
                        old_rel_type=old_rel_type,
                        new_rel_type=new_rel_type)

                # Verify old fact exists
                cur.execute("""
                    SELECT id, object_id FROM facts
                    WHERE user_id = %s AND subject_id = %s AND rel_type = %s
                      AND superseded_at IS NULL
                    LIMIT 1
                """, (req.user_id, subject_uuid, old_rel_type))

                old_fact_row = cur.fetchone()
                if not old_fact_row:
                    log.warning("correct_fact.old_fact_not_found_rel_type_change",
                               subject=subject_uuid,
                               rel_type=old_rel_type)
                    return FactCorrectionResponse(
                        status="failed",
                        message=f"Old fact not found for rel_type change: {old_rel_type}"
                    )

                old_fact_id = old_fact_row[0]
                old_object_id = old_fact_row[1]
                affected_ids = [old_fact_id]

                # Validate new rel_type exists and matches constraints
                new_edges = [{
                    "subject": subject_uuid,
                    "object": old_object_id,
                    "rel_type": new_rel_type
                }]
                gate.validate_edges(new_edges)

                # Supersede old fact
                cur.execute("""
                    UPDATE facts
                    SET superseded_at = now(), qdrant_synced = false
                    WHERE id = %s
                """, (old_fact_id,))

                # Create new fact with same subject/object but different rel_type
                committed = manager.commit_facts(
                    cur,
                    user_id=req.user_id,
                    edges=new_edges,
                    fact_class="A",
                    confidence=1.0,
                    provenance="user_correction"
                )

                log.info("correct_fact.rel_type_corrected",
                        subject=subject_uuid,
                        old_rel_type=old_rel_type,
                        new_rel_type=new_rel_type)

            # ═════════════════════════════════════════════════════════════════════════════
            # DIMENSION 6: ENTITY_TYPE (Person → Organization, etc.)
            # ═════════════════════════════════════════════════════════════════════════════
            elif dimension == "ENTITY_TYPE":
                log.info("correct_fact.dimension_entity_type_start",
                        entity=subject_uuid,
                        old_type=old_value,
                        new_type=new_value)

                # Verify entity exists
                cur.execute("""
                    SELECT id, entity_type FROM entities
                    WHERE id = %s AND user_id = %s
                """, (subject_uuid, req.user_id))

                entity_row = cur.fetchone()
                if not entity_row:
                    log.warning("correct_fact.entity_not_found",
                               entity=subject_uuid)
                    return FactCorrectionResponse(
                        status="failed",
                        message=f"Entity not found: {subject_uuid}"
                    )

                # Update entity type (surgical: only change the type field)
                cur.execute("""
                    UPDATE entities
                    SET entity_type = %s
                    WHERE id = %s AND user_id = %s
                """, (new_value.upper(), subject_uuid, req.user_id))

                log.info("correct_fact.entity_type_updated",
                        entity=subject_uuid,
                        old_type=old_value,
                        new_type=new_value)

            else:
                # Unknown dimension: reject with clear error
                log.error("correct_fact.unknown_dimension",
                         dimension=dimension,
                         user_id=req.user_id)
                return FactCorrectionResponse(
                    status="failed",
                    message=f"Unknown dimension: {dimension}. Expected: SCALAR|RELATIONAL|HIERARCHICAL|SUBJECT|REL_TYPE|ENTITY_TYPE"
                )

            # Step: Track outcome (for learning loop) — applies to all dimensions
            log.info("correct_fact.tracking_outcome",
                    user_id=req.user_id,
                    dimension=dimension,
                    old=f"{old_rel_type}={old_value}",
                    new=f"{new_rel_type}={new_value}")

            cur.execute("""
                INSERT INTO retraction_outcomes (
                    user_id, detected_pattern, extracted_subject,
                    extracted_old_rel_type, extracted_new_rel_type,
                    extracted_old_value, extracted_new_value,
                    retraction_method, confidence, was_correct, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, NOW())
            """, (
                req.user_id,
                f"{dimension}:{old_rel_type}→{new_rel_type}",
                subject_uuid,
                old_rel_type,
                new_rel_type,
                old_value,
                new_value,
                f"surgical_{dimension.lower()}",
                confidence
            ))

            db.commit()

        log.info("correct_fact.success",
                user_id=req.user_id,
                subject=subject_uuid,
                old=f"{old_rel_type}={old_value}",
                new=f"{new_rel_type}={new_value}",
                facts_superseded=len(affected_ids))

        response = FactCorrectionResponse(
            status="corrected",
            subject_uuid=subject_uuid,
            subject_name=extraction.get("subject_name"),
            old_rel_type=old_rel_type,
            old_value=old_value,
            new_rel_type=new_rel_type,
            new_value=new_value,
            confidence=confidence,
            facts_superseded=len(affected_ids),
            hierarchies_modified=[],
            message=f"✓ {extraction.get('subject_name')}: {old_rel_type}={old_value} → {new_rel_type}={new_value}"
        )

        # Cache result for idempotency
        if req.idempotency_key:
            try:
                _idempotency_mgr.cache(req.idempotency_key, response)
            except Exception as e:
                log.warning("correct_fact.idempotency_cache_failed", error=str(e))

        return response

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        log.error("correct_fact.error", error=str(e), user_id=req.user_id, traceback=traceback.format_exc())
        if db:
            try:
                db.rollback()
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass

@app.post("/retract", response_model=RetractResponse)
def retract_fact(req: RetractRequest):
    try:
        db = psycopg2.connect(os.environ.get("POSTGRES_DSN"))
        manager = FactStoreManager(db)

        mode = "supersede"
        note = None
        if req.rel_type:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT correction_behavior FROM rel_types WHERE rel_type = %s",
                    (req.rel_type.lower(),),
                )
                row = cur.fetchone()
                if row:
                    mode = row[0]
            if mode == "immutable":
                return RetractResponse(
                    status="rejected", retracted=0, mode="immutable",
                    note=f"{req.rel_type} is immutable and cannot be retracted",
                )

        with db.cursor() as cur:
            affected_ids = manager.retract(
                cur, req.user_id, req.subject, req.rel_type, req.old_value, mode
            )
            db.commit()

        if affected_ids:
            collection = derive_collection(req.user_id)
            qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")
            _delete_from_qdrant(affected_ids, collection, qdrant_url)

        # Clean up entity_aliases for pref_name hard-delete
        if req.rel_type and req.rel_type.lower() == "pref_name" and mode == "hard_delete":
            try:
                with db.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM entity_aliases
                        WHERE entity_id = %s
                          AND user_id = %s
                          AND alias = %s
                          AND is_preferred = true
                        """,
                        (req.subject, req.user_id, req.old_value)
                    )
            except Exception as e:
                log.warning("retract.entity_aliases_cleanup_failed",
                            rel_type=req.rel_type, subject_id=req.subject, error=str(e))

        # Phase 3: Record correction feedback for gate adjustment (non-blocking)
        # Enqueue confidence feedback if this is a user-driven retraction
        if affected_ids and hasattr(req, 'gliner_confidence') and req.gliner_confidence is not None:
            try:
                # Bin confidence for feedback tracking
                conf = float(req.gliner_confidence)
                if conf < 0.50:
                    confidence_bin = "0.0-0.50"
                elif conf < 0.60:
                    confidence_bin = "0.50-0.60"
                elif conf < 0.65:
                    confidence_bin = "0.60-0.65"
                elif conf < 0.75:
                    confidence_bin = "0.65-0.75"
                elif conf < 0.85:
                    confidence_bin = "0.75-0.85"
                else:
                    confidence_bin = "0.85-1.0"

                # Fire-and-forget: don't await, don't block response
                import asyncio
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        # We're in an async context, schedule as task
                        asyncio.create_task(_enqueue_reembedder_event(
                            event_type="correction_feedback",
                            user_id=req.user_id,
                            data={"confidence_bin": confidence_bin},
                            priority="normal"
                        ))
                    else:
                        # Sync context, can't use asyncio
                        log.debug("retract.correction_feedback_skipped context=sync")
                except Exception as e:
                    log.debug(f"retract.correction_feedback_enqueue_error: {e}")
            except Exception as e:
                log.debug(f"retract.correction_feedback_binning_error: {e}")

        return RetractResponse(status="ok", retracted=len(affected_ids), mode=mode, note=note)
    except Exception as e:
        log.error("retract.error", error=str(e))
        if 'db' in locals() and db:
            try:
                db.rollback()
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if 'db' in locals() and db:
            try:
                db.close()
            except Exception:
                pass


@app.post("/store_context", response_model=StoreContextResponse)
def store_context(req: StoreContextRequest):
    """
    Store unstructured text directly to Qdrant when no typed edges can be extracted.
    No WGM gate, no Postgres write, direct Qdrant upsert only.
    """
    try:
        qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")
        qwen_api_url = _configured_llm_url()

        collection = derive_collection(req.user_id)

        # Ensure collection exists
        if not ensure_collection(collection, qdrant_url):
            log.error("store_context.collection_ensure_failed", collection=collection)
            raise HTTPException(status_code=500, detail="Collection unavailable")

        # Embed the text (use cached embedding URL if available)
        vector = embed_text(req.text, qwen_api_url, timeout=10.0, fallback=False, embedding_url=_EMBEDDING_API_URL)
        if vector is None:
            log.error("store_context.embed_failed", user_id=req.user_id, text_length=len(req.text))
            raise HTTPException(status_code=500, detail={"status": "error", "point_id": ""})

        # Generate point ID
        point_id = str(uuid.uuid4())

        # Upsert to Qdrant using persistent pooled client
        response = _http_client_sync.put(
            f"{qdrant_url}/collections/{collection}/points",
            json={
                "points": [
                    {
                        "id": point_id,
                        "vector": vector,
                        "payload": {
                            "text": req.text,
                            "source": req.source,
                            "context_type": req.context_type,
                            "user_id": req.user_id,
                            "subject": "user",
                            "rel_type": "context",
                            "object": req.text[:120],
                            "fact_class": "C",
                            "confidence": 0.4,
                        },
                    }
                ]
            },
            timeout=10.0,
        )

        if response.status_code != 200:
            log.error(
                "store_context.upsert_failed",
                user_id=req.user_id,
                status=response.status_code,
                text_length=len(req.text),
            )
            raise HTTPException(status_code=500, detail="Qdrant upsert failed")

        log.info(
            "store_context.stored",
            user_id=req.user_id,
            point_id=point_id,
            context_type=req.context_type,
            text_length=len(req.text),
        )

        return StoreContextResponse(status="stored", point_id=point_id)

    except HTTPException:
        raise
    except Exception as e:
        log.error("store_context.error", error=str(e), user_id=req.user_id)
        raise HTTPException(status_code=500, detail=str(e))