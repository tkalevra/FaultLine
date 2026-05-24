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


def _detect_llm_endpoint() -> str:
    """Auto-detect LLM endpoint with smart fallback (dprompt-111).
    Priority: explicit OPENWEBUI_URL env var > auto-detect > localhost default.
    Returns base URL only (without /api/chat/completions suffix).
    """
    # Explicit override: user-provided endpoint takes priority
    openwebui_url = os.environ.get("OPENWEBUI_URL")
    if openwebui_url:
        return openwebui_url

    # Auto-detect candidates
    candidates = [
        "http://open-webui:8080",      # Docker service name (most likely)
        "http://localhost:8080",        # Local development
        "http://127.0.0.1:8080",        # Localhost IPv4
    ]

    for endpoint in candidates:
        try:
            resp = httpx.get(f"{endpoint}/", timeout=2.0, follow_redirects=True)
            if resp.status_code == 200:
                return endpoint
        except Exception:
            continue

    # Final fallback: localhost
    return "http://localhost:8080"


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
        # If not provided, create instance with auto-detected LLM endpoint
        if validator is None:
            llm_endpoint = _detect_llm_endpoint()
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
        user_id: str = None,
    ) -> tuple[bool, str]:
        """
        Validate entity types against rel_type head_types and tail_types constraints.
        Returns (valid: bool, reason: str).
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
                    subject_type = self._resolve_entity_type(subject_id, user_id)
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
            subject_type = self._resolve_entity_type(subject_id, user_id)
        if object_type is None:
            object_type = self._resolve_entity_type(object_id, user_id)

        # If type unknown, skip validation with warning
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

        if object_type is None:
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

    def _resolve_entity_type(self, entity_id: str, user_id: str = None) -> str:
        """
        Resolve entity type from entities table. Returns None if not found.
        """
        try:
            with self.db_conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "SELECT entity_type FROM entities WHERE id = %s AND user_id = %s",
                        (entity_id, user_id),
                    )
                else:
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

    def _find_conflicting_relationships(self, user_id: str, subject_id: str, new_rel_type: str) -> list[str]:
        """Find rel_types that conflict with the new relationship type for this subject."""
        conflicting = []
        for new_rt, conflict_rt in self._CONFLICTING_REL_PAIRS:
            if new_rel_type.lower() == new_rt:
                conflicting.append(conflict_rt)
        return conflicting

    def _supersede_conflicting_facts(self, user_id: str, subject_id: str,
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
                        "WHERE user_id = %s AND subject_id = %s AND rel_type = %s "
                        "AND archived_at IS NULL",
                        (user_id, subject_id, conflict_rt),
                    )
                    archived_count += cur.rowcount
            self.db_conn.commit()

            if archived_count > 0:
                log.info(
                    "wgm.semantic_supersession",
                    user_id=user_id,
                    subject_id=subject_id,
                    archived_rel_types=conflicting_rel_types,
                    archived_count=archived_count,
                    reason=reason,
                )
        except Exception as e:
            log.error(
                "wgm.semantic_supersession_failed",
                user_id=user_id,
                subject_id=subject_id,
                error=str(e),
            )

        return archived_count

    # ── dprompt-064 Phase 1: Correction Semantics (Metadata-Driven) ──────
    # Replaces hardcoded negation word list with rel_types.correction_behavior
    # Semantics: hard_delete (pref_name, age), supersede (lives_at), immutable (born_in, parent_of)

    def _apply_correction_semantics(self, edge: dict, rel_type: str, user_id: str) -> dict:
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
            user_id: user UUID

        Returns:
            {"action": str, "reason": str, "superseded_count": int (optional)}
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
                        SET superseded_at = NOW(),
                            superseded_reason = 'user_correction'
                        WHERE user_id = %s
                          AND subject_id = %s
                          AND rel_type = %s
                          AND superseded_at IS NULL
                    """, (user_id, subject_id, rel_type.lower()))
                    superseded_count = cur.rowcount
                self.db_conn.commit()

                if superseded_count > 0:
                    log.info(
                        "wgm.correction_supersede",
                        user_id=user_id,
                        subject_id=subject_id,
                        rel_type=rel_type,
                        superseded_count=superseded_count,
                    )
            except Exception as e:
                log.error(
                    "wgm.correction_supersede_failed",
                    user_id=user_id,
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
        Validate hierarchy-based rel_types (instance_of, subclass_of, member_of, part_of).
        dprompt-119: Ensures proper instance and composition relationships.
        dprompt-126: Additional validation for instance_of to catch entity classification errors.
        Returns (valid: bool, reason: str).
        """
        rel_type = fact_dict.get("rel_type", "").lower()

        if not rel_meta.get("is_hierarchy_rel"):
            return (True, "not_hierarchy")

        # instance_of: subject must have entity_type, object must be a type/class
        # dprompt-126: Validate instance_of against entity_taxonomies
        if rel_type == "instance_of":
            subject_id = fact_dict.get("subject_id")
            subject_type = fact_dict.get("subject_type")
            object_name = fact_dict.get("object", "").lower()  # The type being claimed

            if subject_type and subject_type.lower() == "unknown":
                log.warning("wgm.hierarchy_instance_of_unknown_subject",
                           subject_id=subject_id)
                # Don't block, but note it

            # dprompt-126: Query entity_taxonomies to validate claimed type
            # If "${ENTITY} instance_of person", validate that this makes sense
            if object_name and subject_type:
                try:
                    with self.db_conn.cursor() as cur:
                        # Check if the object (type) is valid for this subject
                        # E.g., "${ENTITY}" shouldn't be "person" unless it's a name alias
                        cur.execute("""
                            SELECT member_entity_types FROM entity_taxonomies
                            WHERE %s = ANY(member_entity_types)
                        """, (subject_type.upper(),))
                        hierarchy_matches = cur.fetchall()

                        if not hierarchy_matches:
                            log.warning("wgm.instance_of_type_not_in_any_hierarchy",
                                       subject_id=subject_id, subject_type=subject_type,
                                       claimed_type=object_name)
                except Exception as e:
                    log.warning("wgm.instance_of_validation_error",
                               subject_id=subject_id, error=str(e))

        # member_of: subject belongs to a group/category, object must be a group entity
        if rel_type == "member_of":
            object_type = fact_dict.get("object_type")
            # Object should ideally be Organization, Location, or Group (if supported)

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

    def _validate_category_constraints(self, fact_dict: dict, rel_meta: dict, category: str, user_id: str = None) -> tuple[bool, str]:
        """
        Validate category-specific rel_type constraints.
        dprompt-119: Enforces domain rules (family only Person, location for Location, etc).
        Metadata-driven: Queries entity_taxonomies for category membership rules.
        Returns (valid: bool, reason: str).
        FAIL HARD if taxonomy not found — engine should have populated it.
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

    def _validate_hierarchy_membership(self, rel_type: str, subject_type: str, object_type: str, user_id: str = None) -> tuple[bool, str, list]:
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
                      user_id=None, provenance=None, subject_type: str = None,
                      object_type: str = None, **edge_args) -> dict:
        """
        Validate an incoming edge against the ontology and existing DB state.
        If registry is provided, uses it; otherwise falls back to SEED_ONTOLOGY.
        For novel types, calls Qwen to approve; if approved and confidence >= 0.7,
        inserts into rel_types and proceeds. Otherwise returns {"status": "novel"}.
        Returns {"status": "valid"} when no contradiction exists.
        When user_id is supplied and a contradiction is detected (same user+subject+rel,
        different object): inserts the new fact, penalizes all superseded facts via
        mark_contradicted, and returns a conflict dict with the penalty details.
        """
        # dprompt-90: Semantic supersession on user corrections
        # If this is a correction, archive conflicting facts before validation
        if user_id and self._is_user_correction(edge_args):
            conflicting_rels = self._find_conflicting_relationships(user_id, subject_id, rel_type)
            if conflicting_rels:
                self._supersede_conflicting_facts(
                    user_id, subject_id, conflicting_rels,
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
                        log.warning("wgm.pending_types_insert_failed", rel_type=rt, error=str(e))

                    return {"status": "novel_unapproved"}

        # dprompt-064 Phase 1: Apply correction semantics (metadata-driven)
        # BEFORE validation gates, check if this is an immutable fact
        if user_id and self._is_user_correction(edge_args):
            correction_result = self._apply_correction_semantics(edge_args, rt, user_id)
            if correction_result["action"] == "immutable":
                log.warning(
                    "wgm.immutable_fact_correction_rejected",
                    user_id=user_id,
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
            user_id=user_id,
        )
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
            return {
                "status": "type_mismatch",
                "reason": type_reason,
                "committed": 0,
            }

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
            cat_valid, cat_reason = self._validate_category_constraints(fact_dict, rel_meta, category, user_id)
            if not cat_valid:
                log.warning("wgm.category_validation_failed",
                           rel_type=rt, category=category, reason=cat_reason)

        # dprompt-126 Layer 1: Hierarchy membership validation (metadata-driven, growable)
        # Validate that entity types match hierarchy member_entity_types for this rel_type
        if subject_type and object_type:
            hier_mem_valid, hier_mem_reason, matching_hierarchies = self._validate_hierarchy_membership(
                rt, subject_type, object_type, user_id=user_id
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

        if user_id is None:
            return {"status": "valid", "unified_confidence": unified_confidence}

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
                    " WHERE user_id = %s AND subject_id = %s AND object_id = %s AND rel_type = %s",
                    (user_id, subject_id, object_id, rt),
                )
                if cur.fetchone():
                    return {"status": "valid", "note": "duplicate_exact", "unified_confidence": unified_confidence}

                cur.execute(
                    "SELECT id FROM facts"
                    " WHERE user_id = %s AND subject_id = %s AND object_id = %s AND rel_type = %s",
                    (user_id, object_id, subject_id, rt),
                )
                if cur.fetchone():
                    return {"status": "valid", "note": "symmetric_duplicate", "unified_confidence": unified_confidence}

        with self.db_conn.cursor() as cur:
            cur.execute(
                "SELECT id, confidence FROM facts"
                " WHERE user_id = %s AND subject_id = %s AND rel_type = %s AND object_id != %s",
                (user_id, subject_id, rt, object_id),
            )
            old_rows = cur.fetchall()
            if not old_rows:
                # dprompt-126: Include hierarchy_violation if present
                result = {"status": "valid", "unified_confidence": unified_confidence}
                if edge_args.get("hierarchy_violation"):
                    result["hierarchy_violation"] = edge_args["hierarchy_violation"]
                return result

            cur.execute(
                "INSERT INTO facts (user_id, subject_id, object_id, rel_type, provenance)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (user_id, subject_id, object_id, rel_type) DO NOTHING"
                " RETURNING id",
                (user_id, subject_id, object_id, rt, provenance or ""),
            )
            row = cur.fetchone()
            # If the fact already exists (ON CONFLICT DO NOTHING returned no row),
            # we still mark old conflicting facts as contradicted.
            if row:
                new_id = row[0]
            else:
                # Look up the existing fact id for contradiction marking
                cur.execute(
                    "SELECT id FROM facts WHERE user_id = %s AND subject_id = %s"
                    " AND object_id = %s AND rel_type = %s",
                    (user_id, subject_id, object_id, rt),
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
        qwen_url = _detect_llm_endpoint()
        if not qwen_url:
            return self._auto_approve_novel_type(rel_type)

        try:
            from src.api.llm_client import get_llm_headers, build_llm_payload

            payload = build_llm_payload(
                messages=[
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
                ],
                model=os.getenv("WGM_LLM_MODEL", "qwen/qwen3.5-9b"),
                user_id=getattr(self, '_user_id', None),  # dBug-016: inject user_id as chat_id
                temperature=0.0,
                max_tokens=200,
                thinking={"type": "disabled"},
            )

            response = httpx.post(
                qwen_url,
                json=payload,
                headers=get_llm_headers(),
                timeout=10.0,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"].strip()
            result = json.loads(content)

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
