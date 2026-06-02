import os
import re
import time
import json
import httpx
import psycopg2
import logging
import structlog
from src.fact_store.store import FactStoreManager
from src.api.llm_output_validator import LLMOutputValidator
from src.api.llm_calls import call_llm_with_retry_sync, LLMTimeouts

log = structlog.get_logger()

# Import logging classification
try:
    from src.api.logging_config import log_crit, log_warn, log_info, log_debug
except ImportError:
    # Fallback if logging_config not available (must match real signature: logger, msg, **)
    def log_crit(logger, msg, **kwargs): logger.critical(msg, **kwargs)
    def log_warn(logger, msg, **kwargs): logger.warning(msg, **kwargs)
    def log_info(logger, msg, **kwargs): logger.info(msg, **kwargs)
    def log_debug(logger, msg, **kwargs): logger.debug(msg, **kwargs)

# Marker for internal FaultLine prompts (dprompt-128) — prevents context bloat if looped back
_FAULTLINE_INTERNAL_PREFIX = "[FaultLine-Internal]"


# DEAD CODE REMOVED: _detect_llm_endpoint()
# All LLM endpoint logic consolidated in src/api/llm_calls._get_endpoint_list()
# Use llm_calls module for all endpoint detection


class RelTypeRegistry:
    def __init__(self, dsn: str, ttl_seconds: int = 5):  # CHANGED: 60s → 5s for live refresh
        self.dsn = dsn
        self.ttl = ttl_seconds
        self._cache: set[str] = set()
        self._ontology: dict = {}  # Stores full ontology with type constraints
        self._loaded_at: float = 0.0

    def get_valid_types(self) -> set[str]:
        now = time.time()
        if now - self._loaded_at > self.ttl or not self._cache:
            self._refresh()
        return self._cache

    def get_ontology(self) -> dict:
        """Return full ontology including type constraints."""
        now = time.time()
        if now - self._loaded_at > self.ttl or not self._ontology:
            self._refresh()
        return self._ontology

    def _refresh(self) -> None:
        try:
            with psycopg2.connect(self.dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT rel_type, head_types, tail_types,
                               correction_behavior, inverse_rel_type, is_symmetric,
                               is_hierarchy_rel, category
                        FROM rel_types
                    """)
                    self._cache = set()
                    self._ontology = {}
                    for row in cur.fetchall():
                        rel_type = row[0]
                        self._cache.add(rel_type)
                        self._ontology[rel_type] = {
                            "head_types": row[1],
                            "tail_types": row[2],
                            "correction_behavior": row[3],  # hard_delete, supersede, immutable
                            "inverse_rel_type": row[4],
                            "is_symmetric": row[5],
                            "is_hierarchy_rel": row[6],
                            "category": row[7],
                        }
                    self._loaded_at = time.time()
        except Exception as e:
            log.warning(f"RelTypeRegistry refresh failed: {e}, using SEED_ONTOLOGY fallback")
            # If DB unavailable, fall back to SEED_ONTOLOGY
            self._cache = set(SEED_ONTOLOGY.keys())
            self._ontology = {
                rt: SEED_ONTOLOGY[rt]
                for rt in SEED_ONTOLOGY.keys()
            }
            self._loaded_at = time.time()

    def is_valid(self, rel_type: str) -> bool:
        return rel_type.lower() in self.get_valid_types()

    def all_types(self) -> list[str]:
        return sorted(self.get_valid_types())

    def get(self, rel_type: str, default=None):
        """Get ontology entry for a rel_type (inclualice head_types, tail_types, engine_generated)."""
        self.get_valid_types()  # ensure cache is fresh
        return self._ontology.get(rel_type.lower(), default or {})


# DEPRECATED: Kept for test compatibility. RelTypeRegistry reads from Postgres at runtime.
# Added W3C-aligned types (instance_of, subclass_of, pref_name, same_as)
# See migrations/006_split_is_a.sql for standards alignment details.
# dprompt-064 Phase 1: Added correction_behavior, inverse_rel_type, is_symmetric fields
SEED_ONTOLOGY = {
    "is_a":           {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": True, "category": None},
    "instance_of":    {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": True, "category": None},
    "subclass_of":    {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": True, "category": None},
    "part_of":        {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": True, "category": None},
    "created_by":     {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": None},
    "works_for":      {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": "work"},
    "parent_of":      {"correction_behavior": "immutable", "inverse_rel_type": "child_of", "is_symmetric": False, "is_hierarchy_rel": False, "category": "family"},
    "child_of":       {"correction_behavior": "immutable", "inverse_rel_type": "parent_of", "is_symmetric": False, "is_hierarchy_rel": False, "category": "family"},
    "spouse":         {"correction_behavior": "supersede", "inverse_rel_type": "spouse", "is_symmetric": True, "is_hierarchy_rel": False, "category": "family"},
    "sibling_of":     {"correction_behavior": "immutable", "inverse_rel_type": "sibling_of", "is_symmetric": True, "is_hierarchy_rel": False, "category": "family"},
    "also_known_as":  {"correction_behavior": "hard_delete", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": "identity"},
    "pref_name":      {"correction_behavior": "hard_delete", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": "identity"},
    "same_as":        {"correction_behavior": "supersede", "inverse_rel_type": "same_as", "is_symmetric": True, "is_hierarchy_rel": False, "category": "identity"},
    "related_to":     {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": None},
    "likes":          {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": None},
    "dislikes":       {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": None},
    "prefers":        {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": None},
    "owns":           {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": None},
    "located_in":     {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": "location"},
    "educated_at":    {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": "work"},
    "nationality":    {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": None},
    "occupation":     {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": "work"},
    "born_on":        {"correction_behavior": "immutable", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": None},
    "age":            {"correction_behavior": "hard_delete", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": None},
    "knows":          {"correction_behavior": "supersede", "inverse_rel_type": "knows", "is_symmetric": True, "is_hierarchy_rel": False, "category": "family"},
    "friend_of":      {"correction_behavior": "supersede", "inverse_rel_type": "friend_of", "is_symmetric": True, "is_hierarchy_rel": False, "category": "family"},
    "met":            {"correction_behavior": "supersede", "inverse_rel_type": "met", "is_symmetric": True, "is_hierarchy_rel": False, "category": None},
    "lives_in":       {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": "location"},
    "born_in":        {"correction_behavior": "immutable", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": "location"},
    "has_gender":     {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": None},
}


# UUID regex for canonical ID validation
_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE
)

class WGMValidationGate:
    def __init__(self, db_conn, registry: RelTypeRegistry = None, validator: LLMOutputValidator = None):
        self.db_conn = db_conn
        self.registry = registry
        # Initialize unified LLM output validator (dBug-046 Phase 2a)
        # If not provided, create instance with auto-detected LLM endpoint via centralized logic
        if validator is None:
            from src.api.llm_calls import _get_endpoint_list
            endpoints = _get_endpoint_list()
            llm_endpoint = endpoints[0] if endpoints else "http://open-webui:8080/api/chat/completions"
            self.validator = LLMOutputValidator(db_conn=db_conn, llm_endpoint=llm_endpoint)
        else:
            self.validator = validator
        # REMOVED: No longer cache ontology at init
        # Every validation query gets fresh ontology via self.get_current_ontology()
        # This ensures rel_types learned by re_embedder are immediately available
        # See get_current_ontology() for fallback chain

    def _check_type_constraints(
        self,
        rel_type: str,
        subject_id: str,
        object_id: str,
        subject_type: str = None,
        object_type: str = None,
    ) -> tuple[bool, str]:
        """
        Validate entity types against rel_type head_types and tail_types constraints.
        Returns (valid: bool, reason: str).

        Per-user schema isolation: user_id parameter removed (schema itself provides isolation).
        """
        # Normalize empty strings to None (extraction produces empty strings for unknown types)
        subject_type = subject_type if subject_type and subject_type.strip() else None
        object_type = object_type if object_type and object_type.strip() else None

        # Look up head_types and tail_types from FRESH ontology
        ontology = self.get_current_ontology()
        ontology_entry = ontology.get(rel_type.lower())
        if not ontology_entry:
            return (True, "unconstrained")

        head_types = ontology_entry.get("head_types")
        tail_types = ontology_entry.get("tail_types")

        # None or ARRAY['ANY'] means unconstrained (case-insensitive)
        head_any = head_types is None or (len(head_types) == 1 and head_types[0].lower() == "any")
        tail_any = tail_types is None or (len(tail_types) == 1 and tail_types[0].lower() == "any")
        if head_any and tail_any:
            return (True, "unconstrained")

        # SCALAR tail type: skip object type check entirely (case-insensitive)
        is_scalar = tail_types is not None and len(tail_types) == 1 and tail_types[0].lower() == "scalar"
        if is_scalar:
            # Still validate head_types for subject
            if head_types and not (len(head_types) == 1 and head_types[0].lower() == "any"):
                if subject_type is None:
                    subject_type = self._resolve_entity_type(subject_id)
                if subject_type is None:
                    log.warning(
                        "wgm.type_check_skipped",
                        extra={
                            "rel_type": rel_type,
                            "entity_id": subject_id,
                            "reason": "entity_type unknown",
                        },
                    )
                    return (True, "type_unknown")
                subject_type_lower = subject_type.lower() if subject_type else None
                head_types_lower = [t.lower() for t in head_types] if head_types else []
                if subject_type_lower not in head_types_lower and "any" not in head_types_lower:
                    return (
                        False,
                        f"subject_type '{subject_type}' not allowed for '{rel_type}' (allowed: {head_types})",
                    )
            return (True, "ok")

        # Type resolution for subject and object
        if subject_type is None:
            subject_type = self._resolve_entity_type(subject_id)
        if object_type is None:
            object_type = self._resolve_entity_type(object_id)

        # If type unknown, attempt hierarchy-aware resolution before falling back.
        # NOTE: entity_type='unknown' (string) is the DB default for newly-created entities.
        # Treat the string 'unknown' identically to None — blocking facts on unknown-type
        # entities creates a chicken-and-egg problem where the entity can never be typed.
        subject_is_unknown = (subject_type is None or (isinstance(subject_type, str) and subject_type.lower() == 'unknown'))
        if subject_is_unknown:
            # Hierarchy-1 lookup: walk instance_of/subclass_of upward from subject_id
            # to find the effective entity type before falling back to infer-from-constraint.
            # Hierarchy rel_types fetched from DB — never hardcoded.
            hierarchy_type = self._resolve_entity_type_via_hierarchy(subject_id)
            if hierarchy_type:
                subject_type = hierarchy_type
                subject_is_unknown = False
                log.info("wgm.entity_type_resolved_via_hierarchy",
                         entity_id=str(subject_id)[:16], resolved_type=hierarchy_type,
                         rel_type=rel_type.lower(), role="subject")
                # Check resolved type against head_types — mismatch signals wrong rel_type
                if head_types and not head_any:
                    head_types_lower = [h.lower() for h in head_types]
                    if subject_type.lower() not in head_types_lower:
                        log.warning("wgm.hierarchy_type_mismatch",
                                    entity_id=str(subject_id)[:16],
                                    discovered_type=subject_type,
                                    required_types=head_types,
                                    rel_type=rel_type.lower(), role="subject")
                        return (False, "hierarchy_type_mismatch")
            else:
                # Growth engine: infer subject type from head_types constraint
                if head_types and not head_any:
                    inferred = head_types[0]
                    self._infer_entity_type(subject_id, inferred)
                    log.info("wgm.entity_type_inferred",
                             entity_id=subject_id, inferred_type=inferred,
                             rel_type=rel_type.lower(), role="subject")
                log.warning(
                    "wgm.type_check_skipped",
                    extra={
                        "rel_type": rel_type.lower(),
                        "entity_id": subject_id,
                        "reason": "entity_type unknown",
                    },
                )
                return (True, "type_unknown")

        object_is_unknown = (object_type is None or
                             (isinstance(object_type, str) and object_type.lower() == 'unknown'))
        if object_is_unknown:
            # Hierarchy-1 lookup for object entity type.
            hierarchy_type = self._resolve_entity_type_via_hierarchy(object_id)
            if hierarchy_type:
                object_type = hierarchy_type
                object_is_unknown = False
                log.info("wgm.entity_type_resolved_via_hierarchy",
                         entity_id=str(object_id)[:16], resolved_type=hierarchy_type,
                         rel_type=rel_type.lower(), role="object")
                # Check resolved type against tail_types — mismatch signals wrong rel_type
                if tail_types and not tail_any and not is_scalar:
                    tail_types_lower = [t.lower() for t in tail_types]
                    if object_type.lower() not in tail_types_lower:
                        log.warning("wgm.hierarchy_type_mismatch",
                                    entity_id=str(object_id)[:16],
                                    discovered_type=object_type,
                                    required_types=tail_types,
                                    rel_type=rel_type.lower(), role="object")
                        return (False, "hierarchy_type_mismatch")
            else:
                # Growth engine: infer object type from tail_types constraint
                if tail_types and not tail_any and not is_scalar:
                    inferred = tail_types[0]
                    self._infer_entity_type(object_id, inferred)
                    log.info("wgm.entity_type_inferred",
                             entity_id=object_id, inferred_type=inferred,
                             rel_type=rel_type.lower(), role="object")
                log.warning(
                    "wgm.type_check_skipped",
                    extra={
                        "rel_type": rel_type,
                        "entity_id": object_id,
                        "reason": "entity_type unknown",
                    },
                )
                return (True, "type_unknown")


        # Constraint checks (case-insensitive type comparison)
        head_types_lower = [t.lower() for t in head_types] if head_types else []
        tail_types_lower = [t.lower() for t in tail_types] if tail_types else []
        subject_type_lower = subject_type.lower() if subject_type else None
        object_type_lower = object_type.lower() if object_type else None

        head_ok = head_types is None or "any" in head_types_lower or subject_type_lower in head_types_lower
        tail_ok = tail_types is None or "any" in tail_types_lower or object_type_lower in tail_types_lower

        if not head_ok:
            return (
                False,
                f"subject_type '{subject_type}' not allowed for '{rel_type}' (allowed: {head_types})",
            )
        if not tail_ok:
            return (
                False,
                f"object_type '{object_type}' not allowed for '{rel_type}' (allowed: {tail_types})",
            )

        return (True, "ok")

    def _resolve_entity_type(self, entity_id: str) -> str:
        """
        Resolve entity type from entities table. Returns None if not found.

        Per-user schema isolation: user_id parameter removed (schema itself provides isolation).
        """
        try:
            with self.db_conn.cursor() as cur:
                cur.execute(
                    "SELECT entity_type FROM entities WHERE id = %s",
                    (entity_id,),
                )
                row = cur.fetchone()
                return row[0] if row else None
        except Exception as e:
            log.warning(
                "wgm.type_resolve_error",
                extra={"entity_id": entity_id, "error": str(e)},
            )
            return None

    def _resolve_entity_type_via_hierarchy(self, entity_id: str) -> str | None:
        """Walk hierarchy facts upward to find effective entity type.

        Queries both facts and staged_facts. Hierarchy rel_types are read from
        the rel_types table (is_hierarchy_rel=true) — NOT hardcoded — so new
        hierarchy rel_types registered in the ontology automatically participate.

        Returns the most specific type found (shallowest hop), or None.

        Per-user schema isolation: self.db_conn already has schema context set.
        """
        try:
            with self.db_conn.cursor() as cur:
                # Fetch hierarchy rel_types from DB — growth model, never hardcode
                cur.execute(
                    "SELECT rel_type FROM rel_types WHERE is_hierarchy_rel = true"
                )
                hier_rels = [r[0] for r in cur.fetchall()]
                if not hier_rels:
                    log.warning("wgm.hierarchy_type_resolve_no_hier_rels",
                                entity_id=str(entity_id)[:16])
                    return None

                # Walk hierarchy upward via CTE, check both facts and staged_facts
                cur.execute("""
                    WITH RECURSIVE hier(entity_id, depth) AS (
                        SELECT f.object_id, 1
                        FROM (
                            SELECT object_id FROM facts
                            WHERE subject_id = %s AND rel_type = ANY(%s)
                              AND superseded_at IS NULL
                            UNION ALL
                            SELECT object_id FROM staged_facts
                            WHERE subject_id = %s AND rel_type = ANY(%s)
                              AND promoted_at IS NULL
                        ) f
                        UNION ALL
                        SELECT f2.object_id, h.depth + 1
                        FROM (
                            SELECT subject_id, object_id FROM facts
                            WHERE rel_type = ANY(%s) AND superseded_at IS NULL
                            UNION ALL
                            SELECT subject_id, object_id FROM staged_facts
                            WHERE rel_type = ANY(%s) AND promoted_at IS NULL
                        ) f2
                        JOIN hier h ON f2.subject_id = h.entity_id
                        WHERE h.depth < 4
                    )
                    SELECT e.entity_type
                    FROM hier h
                    JOIN entities e ON e.id = h.entity_id
                    WHERE e.entity_type IS NOT NULL AND e.entity_type != 'unknown'
                    ORDER BY h.depth ASC
                    LIMIT 1
                """, (
                    entity_id, hier_rels,
                    entity_id, hier_rels,
                    hier_rels, hier_rels,
                ))
                row = cur.fetchone()
                return row[0] if row else None
        except Exception as e:
            log.warning("wgm.hierarchy_type_resolve_failed",
                        entity_id=str(entity_id)[:16], error=str(e))
            return None

    def _infer_entity_type(self, entity_id: str, inferred_type: str) -> bool:
        """
        Growth engine: infer entity type from rel_type tail_types/head_types constraint.

        When a fact uses a known rel_type that constrains entity types
        (e.g., lives_at.tail_types=['Location']), and the entity has type 'unknown',
        auto-assign the constraint's first allowed type.

        Only updates entity_type='unknown' — never overwrites known types.
        Returns True if an update was performed.

        Per-user schema isolation: user_id parameter removed (schema itself provides isolation).
        """
        try:
            with self.db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE entities SET entity_type = %s WHERE id = %s AND entity_type = 'unknown'",
                    (inferred_type, entity_id),
                )
                updated = cur.rowcount > 0
            self.db_conn.commit()
            return updated
        except Exception as e:
            log.warning("wgm.entity_type_infer_failed", entity_id=entity_id, error=str(e))
            return False

    # ── dprompt-90: Semantic Supersession on User Corrections ─────────
    # When user corrects a fact (e.g., "${ENTITY} is a computer"), archive conflicting facts
    # This ensures corrections are authoritative at write-time, not post-hoc filtering

    _CONFLICTING_REL_PAIRS = {
        # (new_rel_type, conflicting_rel_type) pairs that trigger archival
        ("instance_of", "has_pet"),
        ("instance_of", "owns"),
        ("instance_of", "likes"),
        ("instance_of", "dislikes"),
        ("instance_of", "prefers"),
        ("subclass_of", "has_pet"),
        ("subclass_of", "owns"),
    }

    def get_current_ontology(self) -> dict:
        """
        Get fresh ontology from registry (bypasses WGMValidationGate-level caching).
        RelTypeRegistry handles its own 5s TTL cache, so this is lightweight.
        Always returns current state (picks up rel_types added by re_embedder).
        """
        if self.registry:
            return self.registry.get_ontology()
        return SEED_ONTOLOGY

    def _is_user_correction(self, edge_dict: dict) -> bool:
        """Check if edge is marked as user correction (high confidence or explicit flag)."""
        # Explicit flag
        if edge_dict.get("is_correction"):
            return True
        # High confidence (1.0 or 0.9+) implies user-stated
        confidence = edge_dict.get("confidence", 0.5)
        return (confidence or 0.0) >= 0.9

    def _find_conflicting_relationships(self, subject_id: str, new_rel_type: str) -> list[str]:
        """Find rel_types that conflict with the new relationship type for this subject.

        Per-user schema isolation: user_id parameter removed (schema itself provides isolation).
        """
        conflicting = []
        for new_rt, conflict_rt in self._CONFLICTING_REL_PAIRS:
            if new_rel_type.lower() == new_rt:
                conflicting.append(conflict_rt)
        return conflicting

    def _supersede_conflicting_facts(self, subject_id: str,
                                     conflicting_rel_types: list[str], reason: str) -> int:
        """Archive facts with conflicting rel_types for this subject. Returns count archived."""
        if not conflicting_rel_types:
            return 0

        archived_count = 0
        try:
            with self.db_conn.cursor() as cur:
                for conflict_rt in conflicting_rel_types:
                    cur.execute(
                        "UPDATE facts SET archived_at = NOW() "
                        "WHERE subject_id = %s AND rel_type = %s "
                        "AND archived_at IS NULL",
                        (subject_id, conflict_rt),
                    )
                    archived_count += cur.rowcount
            self.db_conn.commit()

            if archived_count > 0:
                log.info(
                    "wgm.semantic_supersession",
                    subject_id=subject_id,
                    archived_rel_types=conflicting_rel_types,
                    archived_count=archived_count,
                    reason=reason,
                )
        except Exception as e:
            try:
                self.db_conn.rollback()
            except Exception:
                pass
            log.error(
                "wgm.semantic_supersession_failed",
                subject_id=subject_id,
                error=str(e),
            )

        return archived_count

    # ── dprompt-064 Phase 1: Correction Semantics (Metadata-Driven) ──────
    # Replaces hardcoded negation word list with rel_types.correction_behavior
    # Semantics: hard_delete (pref_name, age), supersede (lives_at), immutable (born_in, parent_of)

    def _apply_correction_semantics(self, edge: dict, rel_type: str) -> dict:
        """
        Apply correction semantics based on rel_type.correction_behavior metadata.
        REPLACES dprompt-128 P4 hardcoded negation filter.

        Queries rel_types.correction_behavior to determine action:
        - hard_delete: DELETE old values, INSERT new (pref_name, also_known_as, age, height)
        - supersede: Mark old values as superseded_at=now() (lives_at, works_for, spouse, etc)
        - immutable: Ignore correction, reject fact (born_in, born_on, parent_of, sibling_of)

        Args:
            edge: dict with is_correction flag, confidence, subject_id, object_id
            rel_type: lowercase rel_type

        Returns:
            {"action": str, "reason": str, "superseded_count": int (optional)}

        Per-user schema isolation: user_id parameter removed (schema itself provides isolation).
        """
        # Is this a correction?
        if not self._is_user_correction(edge):
            return {"action": "accept", "reason": "not_a_correction"}

        # Query rel_types.correction_behavior from FRESH ontology
        ontology = self.get_current_ontology()
        rel_meta = ontology.get(rel_type.lower(), {})
        behavior = rel_meta.get("correction_behavior", "supersede")  # Safe default
        subject_id = edge.get("subject_id")

        if behavior == "hard_delete":
            # pref_name, also_known_as, age, height: DELETE old value, INSERT new
            # Action happens via ON CONFLICT DO UPDATE in ingest layer
            return {
                "action": "hard_delete",
                "reason": f"correction_behavior=hard_delete",
            }

        elif behavior == "supersede":
            # lives_at, works_for, spouse, occupation, etc: Mark old as superseded_at=now()
            superseded_count = 0
            try:
                with self.db_conn.cursor() as cur:
                    cur.execute("""
                        UPDATE facts
                        SET superseded_at = NOW()
                        WHERE subject_id = %s
                          AND rel_type = %s
                          AND superseded_at IS NULL
                    """, (subject_id, rel_type.lower()))
                    superseded_count = cur.rowcount
                self.db_conn.commit()

                if superseded_count > 0:
                    log.info(
                        "wgm.correction_supersede",
                        subject_id=subject_id,
                        rel_type=rel_type,
                        superseded_count=superseded_count,
                    )
            except Exception as e:
                log.error(
                    "wgm.correction_supersede_failed",
                    subject_id=subject_id,
                    rel_type=rel_type,
                    error=str(e),
                )
                self.db_conn.rollback()

            return {
                "action": "supersede",
                "reason": f"correction_behavior=supersede",
                "superseded_count": superseded_count,
            }

        elif behavior == "immutable":
            # born_in, born_on, parent_of, sibling_of: User cannot correct these
            # Reject the correction attempt
            return {
                "action": "immutable",
                "reason": f"correction_behavior=immutable — this fact is unchangeable",
            }

        # Unknown behavior: safe default is accept
        return {
            "action": "accept",
            "reason": f"unknown_correction_behavior={behavior}",
        }

    # ── end dprompt-064 Phase 1 ──────────────────────────────────────────

    # ── end dprompt-90 ──────────────────────────────────────────────────

    # ── dprompt-119: INGEST Strengthening — Ontology, Hierarchy, Category ──
    # Metadata-driven routing using rel_types columns

    def _find_inverse_rel_type(self, rel_type: str) -> str | None:
        """
        Find canonical form of a rel_type by checking for inverse relationships.
        If rel_type not found in rel_types, check if its inverse exists.
        Returns canonical rel_type name, or None if no equivalent found.
        dprompt-119: Enables has_child → parent_of mapping.
        """
        rt_lower = rel_type.lower().strip()

        # First check if rel_type exists directly
        if self.registry and rt_lower in [r.lower() for r in self.registry.get_valid_types()]:
            return rt_lower

        # Check if inverse exists
        try:
            with self.db_conn.cursor() as cur:
                # Check if this rel_type is the inverse_rel_type of something else
                cur.execute(
                    "SELECT rel_type FROM rel_types WHERE inverse_rel_type = %s AND inverse_rel_type IS NOT NULL",
                    (rt_lower,),
                )
                row = cur.fetchone()
                if row:
                    canonical = row[0].lower()
                    log.info("wgm.ontology_inverse_found",
                             original=rt_lower, canonical=canonical, action="mapping_to_canonical")
                    return canonical
        except Exception as e:
            log.warning("wgm.inverse_lookup_failed", rel_type=rt_lower, error=str(e))

        return None

    def _validate_hierarchy_rules(self, fact_dict: dict, rel_meta: dict) -> tuple[bool, str]:
        """
        Validate hierarchy-based rel_types (instance_of, subclass_of, member_of, part_of, is_a).
        dprompt-119: Ensures proper instance and composition relationships.
        dprompt-126: Additional validation for typing rel_types to catch entity classification errors.
        Returns (valid: bool, reason: str).
        """
        rel_type = fact_dict.get("rel_type", "").lower()

        if not rel_meta.get("is_hierarchy_rel"):
            return (True, "not_hierarchy")

        # Metadata-driven hierarchy rel_type handling
        # All hierarchy rel_types validated against entity_taxonomies where applicable
        # dprompt-126: Validate typing rel_types (instance_of, subclass_of, is_a) for entity classification correctness
        subject_id = fact_dict.get("subject_id")
        subject_type = fact_dict.get("subject_type")
        object_name = fact_dict.get("object", "").lower()
        object_type = fact_dict.get("object_type")

        if subject_type and subject_type.lower() == "unknown":
            log.warning("wgm.hierarchy_unknown_subject_type",
                       subject_id=subject_id, rel_type=rel_type)
            # Don't block, but note it

        # For all hierarchy relations, validate against entity_taxonomies if types are present
        if object_name and subject_type:
            try:
                with self.db_conn.cursor() as cur:
                    # Check if the subject type is valid in any taxonomy
                    # E.g., "${ENTITY}" with type "person" should be valid for instance_of/subclass_of/is_a
                    cur.execute("""
                        SELECT member_entity_types FROM entity_taxonomies
                        WHERE %s = ANY(member_entity_types)
                    """, (subject_type.upper(),))
                    hierarchy_matches = cur.fetchall()

                    if not hierarchy_matches:
                        log.warning("wgm.hierarchy_type_not_in_any_taxonomy",
                                   subject_id=subject_id, subject_type=subject_type,
                                   rel_type=rel_type, object_name=object_name)
            except Exception as e:
                log.warning("wgm.hierarchy_validation_error",
                           subject_id=subject_id, rel_type=rel_type, error=str(e))

        return (True, "hierarchy_valid")

    def _get_taxonomy_rel_types(self, taxonomy_name: str) -> set[str]:
        """
        Query entity_taxonomies for rel_types that define a given taxonomy.
        FAIL HARD if taxonomy not found — engine should have learned this.
        Returns set of lowercase rel_type strings.
        """
        try:
            with self.db_conn.cursor() as cur:
                cur.execute(
                    "SELECT rel_types_defining_group FROM entity_taxonomies WHERE taxonomy_name = %s",
                    (taxonomy_name,)
                )
                row = cur.fetchone()
                if not row or not row[0]:
                    msg = (
                        f"CRITICAL: taxonomy '{taxonomy_name}' not found in entity_taxonomies. "
                        f"Engine failed to learn this taxonomy. "
                        f"Available taxonomies: family, household, location, work, computer_system. "
                        f"ACTION: Run /skill troubleshoot-faultline-pipeline to diagnose taxonomy discovery failure."
                    )
                    log_crit(log, msg, taxonomy=taxonomy_name, component="entity_taxonomies")
                    raise RuntimeError(msg)
                return set(rt.lower() for rt in row[0])
        except RuntimeError:
            raise  # Re-raise intentional errors
        except Exception as e:
            msg = f"Database query failed for taxonomy '{taxonomy_name}': {e}"
            log_crit(log, msg, taxonomy=taxonomy_name, error=str(e), component="entity_taxonomies")
            raise RuntimeError(msg)

    def _validate_category_constraints(self, fact_dict: dict, rel_meta: dict, category: str) -> tuple[bool, str]:
        """
        Validate category-specific rel_type constraints.
        dprompt-119: Enforces domain rules (family only Person, location for Location, etc).
        Metadata-driven: Queries entity_taxonomies for category membership rules.
        Returns (valid: bool, reason: str).
        FAIL HARD if taxonomy not found — engine should have populated it.

        Per-user schema isolation: user_id parameter removed (schema itself provides isolation).
        """
        if not category:
            return (True, "no_category_defined")

        rel_type = fact_dict.get("rel_type", "").lower()
        subject_type = fact_dict.get("subject_type", "").lower() if fact_dict.get("subject_type") else None
        object_type = fact_dict.get("object_type", "").lower() if fact_dict.get("object_type") else None

        # Query entity_taxonomies for this category → get member_entity_types + rel_types_defining_group
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT member_entity_types, rel_types_defining_group
                    FROM entity_taxonomies
                    WHERE taxonomy_name = %s
                """, (category,))
                row = cur.fetchone()
                if not row:
                    # dprompt-LLM-Fix: Taxonomy not found — allow fact, re-embedder discovers async
                    # No synchronous LLM call in validation hot path
                    log.info("wgm.taxonomy_missing_deferred_to_re_embedder",
                            category=category, rel_type=rel_type,
                            reason="re_embedder async discovery prevents ingest blocking")
                    return (False, "valid")

                member_entity_types = set(t.lower() for t in (row[0] or []))
                category_rels = set(rt.lower() for rt in (row[1] or []))

                if rel_type not in category_rels:
                    return (True, "rel_type_not_in_category")

                # Validate subject_type against member_entity_types
                if member_entity_types and subject_type and subject_type != "unknown":
                    if subject_type not in member_entity_types:
                        log.warning(
                            "wgm.category_invalid_subject",
                            category=category,
                            rel_type=rel_type,
                            subject_type=subject_type,
                            allowed_types=member_entity_types
                        )

                # Validate object_type against member_entity_types
                if member_entity_types and object_type and object_type != "unknown":
                    if object_type not in member_entity_types:
                        log.warning(
                            "wgm.category_invalid_object",
                            category=category,
                            rel_type=rel_type,
                            object_type=object_type,
                            allowed_types=member_entity_types
                        )

        except RuntimeError:
            raise  # Re-raise intentional errors
        except Exception as e:
            log.error(f"wgm.category_validation_error: category={category}, error={e}")
            raise RuntimeError(f"Failed to validate category constraints for '{category}': {e}")

        return (True, "category_valid")

    # ── end dprompt-119 ──────────────────────────────────────────────────

    def _validate_hierarchy_membership(self, rel_type: str, subject_type: str, object_type: str) -> tuple[bool, str, list]:
        """
        dprompt-126 Layer 1 (Hierarchy Scoping): Validate entity types against hierarchy membership.

        Queries entity_taxonomies to find all hierarchies that define this rel_type.
        Checks if subject_type and object_type are in member_entity_types for ANY matching hierarchy.

        Metadata-driven: No hardcoded hierarchy rules. All hierarchy definitions come from DB.
        Growth-engine compatible: New hierarchies + rel_types automatically validated.

        Returns: (valid: bool, reason: str, matching_hierarchies: list)
        - valid: True if entity types match at least one hierarchy, False otherwise
        - reason: explanation of what was found or violated
        - matching_hierarchies: list of hierarchy_names that match both entity types

        Per-user schema isolation: user_id parameter removed (schema itself provides isolation).
        """
        if not subject_type or not object_type or not rel_type:
            return (True, "missing_types_skip", [])  # Can't validate without types

        rt_lower = rel_type.lower().strip()
        subject_type_upper = subject_type.upper().strip() if subject_type else None
        object_type_upper = object_type.upper().strip() if object_type else None

        try:
            with self.db_conn.cursor() as cur:
                # Query hierarchies that define this rel_type
                cur.execute("""
                    SELECT taxonomy_name, member_entity_types, rel_types_defining_group
                    FROM entity_taxonomies
                    WHERE rel_types_defining_group @> ARRAY[%s]
                    ORDER BY taxonomy_name
                """, (rt_lower,))

                matching_hierarchies = []
                violations = []

                for row in cur.fetchall():
                    hierarchy_name = row[0]
                    member_types = row[1] or []
                    defining_rels = row[2] or []

                    # Convert member_types to uppercase for comparison
                    member_types_upper = {t.upper() for t in member_types if t}

                    # Check if both subject and object types are in this hierarchy's members
                    subject_in_hierarchy = subject_type_upper in member_types_upper
                    object_in_hierarchy = object_type_upper in member_types_upper

                    if subject_in_hierarchy and object_in_hierarchy:
                        # Both types match this hierarchy
                        matching_hierarchies.append(hierarchy_name)
                        log.info("wgm.hierarchy_membership_valid",
                                rel_type=rt_lower, hierarchy=hierarchy_name,
                                subject_type=subject_type_upper, object_type=object_type_upper,
                                member_types=member_types_upper)
                    elif subject_in_hierarchy or object_in_hierarchy:
                        # One matches, one doesn't — partial match (violation)
                        violations.append({
                            "hierarchy": hierarchy_name,
                            "reason": f"Mixed types: subject={subject_type_upper}({subject_in_hierarchy}), object={object_type_upper}({object_in_hierarchy}), hierarchy_members={list(member_types_upper)}"
                        })

                if matching_hierarchies:
                    # Entity types match at least one hierarchy that defines this rel_type
                    return (True, f"hierarchy_valid:{','.join(matching_hierarchies)}", matching_hierarchies)

                if violations:
                    # Partial matches found (one entity type matches, other doesn't)
                    reason = f"partial_hierarchy_match: {'; '.join(v['reason'] for v in violations)}"
                    log.warning("wgm.hierarchy_membership_partial",
                               rel_type=rt_lower, violations=violations)
                    return (False, reason, [])

                # No hierarchies define this rel_type (novel or generic rel_type)
                # This is OK — generic rel_types don't belong to a specific hierarchy
                return (True, "no_hierarchy_defines_rel_type", [])

        except Exception as e:
            log.warning("wgm.hierarchy_membership_validation_failed",
                       rel_type=rt_lower, error=str(e))
            return (True, f"validation_error:{str(e)}", [])  # Graceful fallback

    def validate_edge(self, subject_id, object_id, rel_type: str,
                      provenance=None, subject_type: str = None,
                      object_type: str = None, **edge_args) -> dict:
        """
        Validate an incoming edge against the ontology and existing DB state.
        If registry is provided, uses it; otherwise falls back to SEED_ONTOLOGY.
        For novel types, calls Qwen to approve; if approved and confidence >= 0.7,
        inserts into rel_types and proceeds. Otherwise returns {"status": "novel"}.
        Returns {"status": "valid"} when no contradiction exists.

        Per-user schema isolation: user_id parameter removed (schema itself provides isolation).
        """
        # dprompt-90: Semantic supersession on user corrections
        # If this is a correction, archive conflicting facts before validation
        if self._is_user_correction(edge_args):
            conflicting_rels = self._find_conflicting_relationships(subject_id, rel_type)
            if conflicting_rels:
                self._supersede_conflicting_facts(
                    subject_id, conflicting_rels,
                    reason=f"user_correction:{rel_type.lower()}"
                )

        rt = rel_type.lower().strip()

        # dprompt-119: Check for inverse rel_type mapping
        # If rel_type not found, try to find canonical form via inverse relationship
        valid_types = self.registry.get_valid_types() if self.registry else set(SEED_ONTOLOGY.keys())
        if rt not in valid_types:
            canonical = self._find_inverse_rel_type(rt)
            if canonical:
                rt = canonical
                log.info("wgm.validate_edge_canonical_form", original=rel_type.lower(), canonical=rt)
            else:
                # dprompt-140: User-stated facts get sync inference; others defer to async
                is_high_confidence = (edge_args.get("confidence", 0.8) or 0.0) >= 0.9

                if is_high_confidence:
                    # User-stated novel rel_type: sync LLM inference (authoritative)
                    log.info("wgm.novel_rel_type_sync_inference_for_user_stated",
                            rel_type=rt, confidence=edge_args.get("confidence"))
                    approved = self._try_approve_novel_type(rt)
                    if approved:
                        # Metadata now in cache and DB, continue validation
                        log.info("wgm.novel_rel_type_approved_sync", rel_type=rt)
                    else:
                        # LLM declined or unavailable, auto-approve with fallback metadata
                        log.warning("wgm.novel_rel_type_auto_approved", rel_type=rt)
                else:
                    # LLM-inferred novel rel_type: async deferred (preserves speed)
                    log.info("wgm.novel_rel_type_deferred_to_async",
                            rel_type=rt, confidence=edge_args.get("confidence"),
                            reason="low confidence extraction deferred")

                    # Insert into pending_types for re_embedder async evaluation
                    try:
                        with self.db_conn.cursor() as cur:
                            cur.execute(
                                "INSERT INTO pending_types (rel_type) VALUES (%s) ON CONFLICT DO NOTHING",
                                (rel_type,)
                            )
                        self.db_conn.commit()
                    except Exception as e:
                        try:
                            self.db_conn.rollback()
                        except Exception:
                            pass
                        log.warning("wgm.pending_types_insert_failed", rel_type=rt, error=str(e))

                    return {"status": "novel_unapproved"}

        # dprompt-064 Phase 1: Apply correction semantics (metadata-driven)
        # BEFORE validation gates, check if this is an immutable fact
        if self._is_user_correction(edge_args):
            correction_result = self._apply_correction_semantics(edge_args, rt)
            if correction_result["action"] == "immutable":
                log.warning(
                    "wgm.immutable_fact_correction_rejected",
                    rel_type=rt,
                    reason=correction_result["reason"],
                )
                return {
                    "status": "immutable_fact",
                    "reason": correction_result["reason"],
                    "committed": 0,
                }

        # CONFIDENCE-GATED BYPASS LOGIC (dprompt-124):
        # High-confidence facts (>= 0.95) bypass validation gates entirely.
        # User-stated facts (confidence 1.0) and clear extractions (0.95+) are
        # trusted and skip type constraints, hierarchy, and category validation.
        raw_confidence = edge_args.get("confidence", 0.8) or 0.0
        is_user_correction = self._is_user_correction(edge_args)
        if raw_confidence >= 0.95 or (is_user_correction and raw_confidence >= 0.9):
            # Growth engine: for bypassed high-confidence facts, still infer entity types
            # from rel_type constraints. Prevents entities staying 'unknown' forever.
            ontology = self.get_current_ontology()
            entry = ontology.get(rt, {})
            head_types = entry.get("head_types") or []
            tail_types = entry.get("tail_types") or []
            head_any = not head_types or "any" in [t.lower() for t in head_types]
            tail_any = not tail_types or "any" in [t.lower() for t in tail_types]
            is_scalar = tail_types and "scalar" in [t.lower() for t in tail_types]
            if not head_any and head_types:
                st = self._resolve_entity_type(subject_id)
                if st and st.lower() == 'unknown':
                    self._infer_entity_type(subject_id, head_types[0])
            if not tail_any and not is_scalar and tail_types:
                ot = self._resolve_entity_type(object_id)
                if ot and ot.lower() == 'unknown':
                    self._infer_entity_type(object_id, tail_types[0])
            log.info("wgm.confidence_bypass_early",
                     rel_type=rt, confidence=raw_confidence,
                     is_user_correction=is_user_correction,
                     reason="skip_type_constraints_hierarchy_category")
            # Return immediately with placeholder unified_confidence
            # (will be computed properly in ingest layer if needed)
            return {"status": "valid", "unified_confidence": raw_confidence}

        # Check type constraints (only for confidence < 0.95)
        type_ok, type_reason = self._check_type_constraints(
            rt, subject_id, object_id,
            subject_type=subject_type,
            object_type=object_type,
        )

        # Hierarchy-aware mismatch: entity type was resolved via hierarchy facts but
        # conflicts with rel_type's head/tail type constraints. Return valid but flag
        # for downgrade to Class C in the ingest loop — never silently pass.
        if not type_ok and type_reason == "hierarchy_type_mismatch":
            log.info("wgm.hierarchy_type_mismatch_staged",
                     rel_type=rt, subject_id=str(subject_id)[:16],
                     object_id=str(object_id)[:16],
                     note="entity type resolved via hierarchy chain; conflicts with rel_type constraint; downgrading to Class C")
            return {
                "status": "valid",
                "hierarchy_type_mismatch": True,
            }

        if not type_ok:
            log.warning(
                "wgm.type_mismatch",
                extra={
                    "rel_type": rt,
                    "reason": type_reason,
                    "subject_id": subject_id,
                    "object_id": object_id,
                },
            )
            # Ask LLM whether this rel_type applies for these actual entity types.
            # If so, expand the constraints in rel_types (ON CONFLICT DO UPDATE).
            # This is the self-growth path — no hardcoded fallback rel_types.
            inferred = self._infer_novel_rel_type_metadata(
                rt,
                subject_type=subject_type,
                object_type=object_type,
                context=f"{subject_id} {rt} {object_id}",
            )
            if inferred:
                stored = self._store_inferred_rel_type(rt, inferred)
                if stored:
                    try:
                        from src.api.main import _refresh_rel_type_cache
                        _refresh_rel_type_cache()
                    except Exception:
                        pass
                    type_ok2, type_reason2 = self._check_type_constraints(
                        rt, subject_id, object_id,
                        subject_type=subject_type,
                        object_type=object_type,
                    )
                    if type_ok2:
                        log.info("wgm.type_constraint_expanded_by_llm",
                                 extra={"rel_type": rt, "subject_type": subject_type,
                                        "object_type": object_type})
                        # Fall through — constraint now passes, continue with validation
                    else:
                        return {"status": "type_mismatch", "reason": type_reason2, "committed": 0}
                else:
                    return {"status": "type_mismatch", "reason": type_reason, "committed": 0}
            else:
                return {"status": "type_mismatch", "reason": type_reason, "committed": 0}

        # dprompt-119: Validate hierarchy and category constraints (FRESH ontology)
        ontology = self.get_current_ontology()
        rel_meta = ontology.get(rt, {})

        # Hierarchy validation (instance_of, subclass_of, member_of, part_of)
        if rel_meta.get("is_hierarchy_rel"):
            fact_dict = {
                "rel_type": rt,
                "subject_id": subject_id,
                "subject_type": subject_type,
                "object_id": object_id,
                "object_type": object_type,
            }
            hier_valid, hier_reason = self._validate_hierarchy_rules(fact_dict, rel_meta)
            if not hier_valid:
                log.warning("wgm.hierarchy_validation_failed",
                           rel_type=rt, reason=hier_reason)

        # Category constraints (family, location, work, household domains)
        category = rel_meta.get("category")
        if category:
            fact_dict = {
                "rel_type": rt,
                "subject_id": subject_id,
                "subject_type": subject_type,
                "object_id": object_id,
                "object_type": object_type,
            }
            cat_valid, cat_reason = self._validate_category_constraints(fact_dict, rel_meta, category)
            if not cat_valid:
                log.warning("wgm.category_validation_failed",
                           rel_type=rt, category=category, reason=cat_reason)

        # dprompt-126 Layer 1: Hierarchy membership validation (metadata-driven, growable)
        # Validate that entity types match hierarchy member_entity_types for this rel_type
        if subject_type and object_type:
            hier_mem_valid, hier_mem_reason, matching_hierarchies = self._validate_hierarchy_membership(
                rt, subject_type, object_type
            )
            if not hier_mem_valid:
                log.warning("wgm.hierarchy_membership_violation",
                           rel_type=rt, subject_type=subject_type, object_type=object_type,
                           reason=hier_mem_reason)
                # Mark for low confidence/Class C if not user-corrected
                # User-stated facts override this check (handled by confidence bypass above)
                if not is_user_correction and raw_confidence < 0.95:
                    edge_args["hierarchy_violation"] = hier_mem_reason
                    log.info("wgm.hierarchy_violation_low_confidence",
                            rel_type=rt, reason=hier_mem_reason,
                            note="Will be stored as Class C (staged) for review")
            elif matching_hierarchies:
                log.info("wgm.hierarchy_membership_confirmed",
                        rel_type=rt, hierarchies=matching_hierarchies)

        # dBug-046 Phase 2a: Compute unified confidence score via LLMOutputValidator
        # This allows the ingest layer to make storage routing decisions (direct vs staged)
        # based on a consistent confidence algorithm across all output types
        try:
            import asyncio
            edge_confidence = edge_args.get("confidence", 0.8) or 0.0
            is_user_correction = self._is_user_correction(edge_args)

            # For async validator in sync context, create a task if event loop exists
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # If loop is already running, we can't use run_until_complete
                    # Fall back to using edge_confidence directly
                    unified_confidence = 1.0 if is_user_correction else edge_confidence
                else:
                    validation = loop.run_until_complete(
                        self.validator.validate_output(
                            output_type='fact',
                            payload={
                                'subject_id': subject_id,
                                'object_id': object_id,
                                'rel_type': rt,
                                'subject_type': subject_type,
                                'object_type': object_type,
                            },
                            source='user' if is_user_correction else 'llm',
                            llm_confidence=edge_confidence,
                            frequency=1
                        )
                    )
                    unified_confidence = validation.confidence
            except RuntimeError:
                # No event loop, use edge_confidence directly
                unified_confidence = 1.0 if is_user_correction else edge_confidence
        except Exception as e:
            log.warning(f"wgm.validator_confidence_failed: {e}")
            unified_confidence = edge_args.get("confidence", 0.8) or 0.0

        # Check for symmetric duplicates: if A→B exists and rel_type is symmetric,
        # do not insert B→A again (it's implicitly the same fact in both directions)
        # Query rel_types.is_symmetric metadata (FAIL HARD if not found — engine should have learned this)
        ontology = self.get_current_ontology()
        rel_meta = ontology.get(rt.lower(), {})
        if not rel_meta:
            msg = (
                f"CRITICAL: rel_type '{rt}' not found in rel_types ontology. "
                f"Engine failed to learn this relationship type. "
                f"ACTION: Check rel_types table or run /skill troubleshoot-faultline-pipeline."
            )
            log_crit(log, msg, rel_type=rt, component="rel_types_ontology")
            raise RuntimeError(msg)
        is_symmetric = rel_meta.get("is_symmetric", False)

        if is_symmetric:
            with self.db_conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM facts"
                    " WHERE subject_id = %s AND object_id = %s AND rel_type = %s",
                    (subject_id, object_id, rt),
                )
                if cur.fetchone():
                    return {"status": "valid", "note": "duplicate_exact", "unified_confidence": unified_confidence}

                cur.execute(
                    "SELECT id FROM facts"
                    " WHERE subject_id = %s AND object_id = %s AND rel_type = %s",
                    (object_id, subject_id, rt),
                )
                if cur.fetchone():
                    return {"status": "valid", "note": "symmetric_duplicate", "unified_confidence": unified_confidence}

        with self.db_conn.cursor() as cur:
            cur.execute(
                "SELECT id, confidence FROM facts"
                " WHERE subject_id = %s AND rel_type = %s AND object_id != %s",
                (subject_id, rt, object_id),
            )
            old_rows = cur.fetchall()
            if not old_rows:
                # dprompt-126: Include hierarchy_violation if present
                result = {"status": "valid", "unified_confidence": unified_confidence}
                if edge_args.get("hierarchy_violation"):
                    result["hierarchy_violation"] = edge_args["hierarchy_violation"]
                return result

            cur.execute(
                "INSERT INTO facts (subject_id, object_id, rel_type, provenance)"
                " VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (subject_id, object_id, rel_type) DO NOTHING"
                " RETURNING id",
                (subject_id, object_id, rt, provenance or ""),
            )
            row = cur.fetchone()
            # If the fact already exists (ON CONFLICT DO NOTHING returned no row),
            # we still mark old conflicting facts as contradicted.
            if row:
                new_id = row[0]
            else:
                # Look up the existing fact id for contradiction marking
                cur.execute(
                    "SELECT id FROM facts WHERE subject_id = %s"
                    " AND object_id = %s AND rel_type = %s",
                    (subject_id, object_id, rt),
                )
                new_id = cur.fetchone()[0]

        self.db_conn.commit()

        manager = FactStoreManager(self.db_conn)
        for old_id, _ in old_rows:
            manager.mark_contradicted(old_id, new_id, penalty=0.5)

        first_old_id, first_old_confidence = old_rows[0]
        return {
            "status": "conflict",
            "new_fact_id": new_id,
            "superseded_fact_id": first_old_id,
            "old_confidence_after_penalty": max(first_old_confidence - 0.5, 0.0),
            "unified_confidence": unified_confidence,
        }

    def _auto_approve_novel_type(self, rel_type: str) -> bool:
        """
        Auto-approve a novel rel_type when LLM validation is unavailable.
        Inserts into rel_types as engine-generated with moderate confidence.
        The type will be available immediately; subsequent Qwen runs can
        upgrade its metadata if needed.
        Returns True if the type was inserted (or already existed).
        """
        try:
            with self.db_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO rel_types"
                    " (rel_type, label, engine_generated, confidence, source)"
                    " VALUES (%s, %s, true, 0.7, 'engine')"
                    " ON CONFLICT (rel_type) DO NOTHING",
                    (rel_type, rel_type.replace('_', ' ').title()),
                )
            self.db_conn.commit()
            # Registry cache will refresh automatically (5s TTL)
            # No need to manually refresh — get_current_ontology() queries fresh
            if self.registry:
                self.registry._refresh()
            return True
        except Exception:
            return False

    def _try_approve_novel_type(self, rel_type: str) -> bool:
        """
        Call LLM to validate a novel rel_type. If unavailable or fails,
        auto-approves the type so facts are not silently dropped.
        If approved with confidence >= 0.7, inserts into rel_types and returns True.
        Otherwise, inserts into pending_types and returns False.
        """
        try:
            # Use centralized retry logic instead of raw httpx.post()
            messages = [
                {
                    "role": "system",
                    "content": f"{_FAULTLINE_INTERNAL_PREFIX} You are a knowledge graph ontology validator. Respond only with valid JSON, no markdown."
                },
                {
                    "role": "user",
                    "content": f"""For the relationship type '{rel_type}', provide complete ontology metadata.

Respond with EXACTLY this JSON structure, no markdown:
{{
  "valid": true or false (is this a valid personal KG relationship?),
  "label": "human readable description",
  "wikidata_pid": "Pxxx" or null,
  "is_symmetric": true or false (e.g., spouse/sibling_of are symmetric, parent_of is not),
  "inverse_rel_type": "inverse_type_name" or null (e.g., inverse of parent_of is child_of),
  "head_types": ["Person", "Organization", "Location", "Any"] (who can be the subject?),
  "tail_types": ["Person", "Organization", "Location", "Any", "SCALAR"] (what can be the object?),
  "is_hierarchy_rel": true or false (classification like instance_of, subclass_of?),
  "category": "family" or "work" or "medical" or "location" or "other",
  "confidence": 0.0 to 1.0 (confidence in above metadata),
  "reasoning": "one sentence explanation"
}}

Examples:
- spouse: symmetric=true, inverse=spouse, head=[Person], tail=[Person], hierarchy=false
- parent_of: symmetric=false, inverse=child_of, head=[Person], tail=[Person], hierarchy=false
- instance_of: symmetric=false, inverse=null, head=[Any], tail=[Any], hierarchy=true
- age: symmetric=false, inverse=null, head=[Any], tail=[SCALAR], hierarchy=false

Respond with ONLY the JSON, no explanation."""
                }
            ]

            result = call_llm_with_retry_sync(
                messages=messages,
                model=os.getenv("WGM_LLM_MODEL", "qwen/qwen3.5-9b"),
                user_id=getattr(self, '_user_id', None),
                timeout=LLMTimeouts.get("ENRICHMENT"),
                operation="rel_type_enrichment",
            )

            if result.get("valid") and result.get("confidence", 0) >= 0.7:
                # Insert into rel_types with full metadata
                with self.db_conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO rel_types"
                        " (rel_type, label, wikidata_pid, engine_generated, confidence,"
                        "  is_symmetric, inverse_rel_type, head_types, tail_types, is_hierarchy_rel, category, source)"
                        " VALUES (%s, %s, %s, true, %s, %s, %s, %s, %s, %s, %s, %s)"
                        " ON CONFLICT (rel_type) DO UPDATE SET"
                        "  label = COALESCE(EXCLUDED.label, rel_types.label),"
                        "  is_symmetric = COALESCE(EXCLUDED.is_symmetric, rel_types.is_symmetric),"
                        "  inverse_rel_type = COALESCE(EXCLUDED.inverse_rel_type, rel_types.inverse_rel_type),"
                        "  head_types = COALESCE(EXCLUDED.head_types, rel_types.head_types),"
                        "  tail_types = COALESCE(EXCLUDED.tail_types, rel_types.tail_types),"
                        "  is_hierarchy_rel = COALESCE(EXCLUDED.is_hierarchy_rel, rel_types.is_hierarchy_rel),"
                        "  category = COALESCE(EXCLUDED.category, rel_types.category),"
                        "  confidence = GREATEST(rel_types.confidence, EXCLUDED.confidence)",
                        (
                            rel_type,
                            result.get("label", rel_type),
                            result.get("wikidata_pid"),
                            result.get("confidence", 0.7),
                            result.get("is_symmetric", False),
                            result.get("inverse_rel_type"),
                            result.get("head_types", ["Any"]),
                            result.get("tail_types", ["Any"]),
                            result.get("is_hierarchy_rel", False),
                            result.get("category", "other"),
                            "engine"
                        )
                    )
                self.db_conn.commit()
                # Refresh cache so updated metadata is available immediately
                if hasattr(self, 'registry') and hasattr(self.registry, '_refresh'):
                    self.registry._refresh()
                # Refresh main.py metadata caches (metadata-driven validation)
                try:
                    from src.api.main import _refresh_rel_type_cache, _refresh_scalar_rel_types_cache
                    _refresh_rel_type_cache()
                    _refresh_scalar_rel_types_cache()
                except ImportError:
                    pass  # main.py not available (test context)
                return True
            else:
                # Insert into pending_types
                with self.db_conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO pending_types (rel_type) VALUES (%s)"
                        " ON CONFLICT DO NOTHING",
                        (rel_type,)
                    )
                self.db_conn.commit()
                return False
        except Exception:
            # Qwen failure: auto-approve so facts are not silently dropped
            return self._auto_approve_novel_type(rel_type)

    def _infer_novel_rel_type_metadata(self, rel_type: str, subject_type: str = None,
                                       object_type: str = None, context: str = None) -> dict:
        """
        Infer ontology metadata for novel rel_type using LLM (synchronous).

        Args:
            rel_type: novel relationship name
            subject_type: entity type of subject (e.g., "Person")
            object_type: entity type of object (e.g., "Person")
            context: surrounding text where rel_type appeared (first 500 chars)

        Returns:
            dict with: is_symmetric, inverse_rel_type, head_types, tail_types,
                       is_hierarchy_rel, category, confidence
            or None if inference failed
        """
        try:
            # Prepare context for LLM
            subject_context = f"Subject type: {subject_type}" if subject_type else "Subject type: Unknown"
            object_context = f"Object type: {object_type}" if object_type else "Object type: Unknown"
            text_context = f"Context: {context[:300]}" if context else "Context: None provided"

            messages = [
                {
                    "role": "system",
                    "content": f"{_FAULTLINE_INTERNAL_PREFIX} You are an ontology expert. Infer relationship properties. Respond only with valid JSON, no markdown."
                },
                {
                    "role": "user",
                    "content": f"""Infer complete ontology metadata for this novel relationship type:

Relationship: {rel_type}
{subject_context}
{object_context}
{text_context}

Respond with EXACTLY this JSON structure, no markdown:
{{
  "is_symmetric": true or false,
  "inverse_rel_type": "inverse_type_name" or null,
  "head_types": ["array", "of", "allowed", "subject", "types"],
  "tail_types": ["array", "of", "allowed", "object", "types"],
  "is_hierarchy_rel": true or false,
  "category": "family|work|location|physical|temporal|identity|medical|other",
  "confidence": 0.0 to 1.0,
  "natural_language": "short human-readable phrase for this relationship, e.g. 'has IP address' or 'is the parent of'"
}}

Respond with ONLY the JSON, no explanation."""
                }
            ]

            result = call_llm_with_retry_sync(
                messages=messages,
                model=os.getenv("WGM_LLM_MODEL", "qwen/qwen3.5-9b"),
                user_id=getattr(self, '_user_id', None),
                timeout=LLMTimeouts.get("ENRICHMENT"),
                operation="novel_rel_type_inference",
            )

            # Validate required fields
            required = ["is_symmetric", "inverse_rel_type", "head_types", "tail_types",
                       "is_hierarchy_rel", "category", "confidence", "natural_language"]
            for field in required:
                if field not in result:
                    log.warning(
                        "wgm.inference_missing_field",
                        rel_type=rel_type,
                        field=field,
                    )
                    return None

            return result

        except Exception as e:
            log.warning(
                "wgm.novel_rel_type_inference_failed",
                rel_type=rel_type,
                error=str(e),
            )
            return None

    def _store_inferred_rel_type(self, rel_type: str, metadata: dict) -> bool:
        """
        Store inferred metadata in rel_types table.
        Uses ON CONFLICT DO UPDATE to handle race conditions.

        Args:
            rel_type: novel relationship name
            metadata: inferred metadata dict

        Returns:
            True if stored successfully, False otherwise
        """
        try:
            with self.db_conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO rel_types
                       (rel_type, label, natural_language, is_symmetric, inverse_rel_type,
                        head_types, tail_types, is_hierarchy_rel, category, confidence,
                        engine_generated, source)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, 'wgm_gate_inference')
                       ON CONFLICT (rel_type) DO UPDATE SET
                           is_symmetric      = EXCLUDED.is_symmetric,
                           inverse_rel_type  = EXCLUDED.inverse_rel_type,
                           head_types        = EXCLUDED.head_types,
                           tail_types        = EXCLUDED.tail_types,
                           is_hierarchy_rel  = EXCLUDED.is_hierarchy_rel,
                           category          = EXCLUDED.category,
                           confidence        = EXCLUDED.confidence,
                           natural_language  = COALESCE(rel_types.natural_language, EXCLUDED.natural_language)
                    """,
                    (
                        rel_type,
                        metadata.get("label") or rel_type.replace("_", " ").title(),
                        metadata.get("natural_language"),
                        metadata.get("is_symmetric", False),
                        metadata.get("inverse_rel_type"),
                        metadata.get("head_types", ["ANY"]),
                        metadata.get("tail_types", ["ANY"]),
                        metadata.get("is_hierarchy_rel", False),
                        metadata.get("category", "other"),
                        metadata.get("confidence", 0.5),
                    )
                )

            self.db_conn.commit()

            # Refresh metadata cache
            if hasattr(self, 'registry') and hasattr(self.registry, '_refresh'):
                self.registry._refresh()

            # Refresh main.py caches
            try:
                from src.api.main import _refresh_rel_type_cache, _refresh_scalar_rel_types_cache
                _refresh_rel_type_cache()
                _refresh_scalar_rel_types_cache()
            except ImportError:
                pass  # main.py not available (test context)

            log.info(
                "wgm.inferred_rel_type_stored",
                rel_type=rel_type,
                confidence=metadata.get("confidence"),
            )
            return True

        except Exception as e:
            log.error(
                "wgm.inferred_rel_type_storage_failed",
                rel_type=rel_type,
                error=str(e),
            )
            self.db_conn.rollback()
            return False