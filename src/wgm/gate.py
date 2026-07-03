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
from src.api.llm_calls import call_llm_with_retry_sync, LLMTimeouts, LLMModels

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


def _flag_on(name: str, default: str = "true") -> bool:
    """Truthy env flag (mirrors src.api.main._flag_on so the gate has no import cycle)."""
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "off")


# ── AUTHORITY GUARD — engine growth must never re-classify a SEEDED rel's structure ──
# Structural CLASSIFICATION fields that are IMMUTABLE to engine growth (user > SEED > growth).
# head_types / tail_types are DELIBERATELY excluded: the gate's type-constraint EXPANSION path
# legitimately WIDENS them (union) to admit a new subject/object type — that additive growth
# must keep working. Mirrors main._SEED_STRUCTURAL_FIELDS (a local copy avoids the main<->gate
# import cycle — the same rationale as the re_embedder copy, a separate module boundary).
_SEED_STRUCTURAL_FIELDS = (
    "is_hierarchy_rel", "category", "tail_types", "fact_class",
    "storage_target", "inverse_rel_type", "is_symmetric",
)


def _seed_structural_flags(db_conn, rel_type: str) -> dict | None:
    """Return the AUTHORITATIVE structural flags of a SEEDED rel from the ``public`` template,
    or ``None`` when the rel is not seeded (genuinely novel — engine growth owns its structure).

    AUTHORITY ORDER — user > SEED > engine-growth. A rel present in ``public.rel_types`` has an
    IMMUTABLE structural CLASSIFICATION against the gate's growth writes (novel-type approval +
    type-constraint expansion): the gate may STRENGTHEN confidence and WIDEN head_types/tail_types
    (additive type-admission — the whole point of the expansion path) but must NEVER re-classify a
    seeded rel's structure (is_hierarchy_rel / category / fact_class / storage_target /
    inverse_rel_type / is_symmetric). This is the drift the guard closes: a Person-object ``owns``
    edge made the enrichment LLM re-resolve owns' category → family (a hierarchical taxonomy →
    is_hierarchy_rel=true), and the ``EXCLUDED.is_hierarchy_rel`` / ``EXCLUDED.category`` writes
    below persisted that OVER the clean seed (public owns = relational, uncategorized).

    KG grounding: RDFS ``rdf:type`` (Wikidata P31, is_hierarchy_rel) and ``rdfs:subClassOf``
    (P279) are DEFINITIONAL axes fixed by the ontology author, not derivable/mutable from instance
    data — https://www.w3.org/TR/rdf-schema/#ch_type , #ch_subclassof.

    ``public`` is read ONLY here as the seed-authority reference (fully-qualified, always readable
    regardless of tenant search_path). Subject-agnostic — keyed on public PRESENCE, never a
    rel-name literal. Fail-safe: any error → log + None (caller keeps its computed values; the
    reconcile migration 130 is the backstop), never crashes ingest/gate.
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
        log.warning("wgm.seed_structural_flags_read_failed", rel_type=rt, error=str(e)[:160])
        return None


# TAIL/HEAD-TYPE MISMATCH VETO (lowest-risk slice of the spine strength-gating design):
# A high-confidence edge whose OBJECT (or SUBJECT) entity_type GENUINELY does NOT satisfy
# the rel's tail_types/head_types — after the legitimate hierarchy-reachability check (a
# true subtype IS allowed) — must NOT be enthroned as durable Class A. The pre-existing
# Fix-B pregate only consulted the GROUND-TRUTH (entities-table / hierarchy) type, which is
# edge-ORDER dependent: a freshly-minted object is still 'unknown' when its own
# participated_in edge validates (the co-extracted owns/part_of edge that types it may not
# have run yet), so the mismatch escapes the bypass and commits as A 1.0 — poisoning L4
# (e.g. a car's GPS filed in the event graph, unwalkable from the car). With this flag ON
# (default), the pregate ALSO honors the caller-passed GLiNER2 type (present at validate
# time, order-independent): a CONCRETE passed type that is genuinely incompatible craters
# the cast to Class C (we don't forget — couldn't-classify-yet, reclassify later) rather
# than admitting it as A. Metadata-driven (head_types/tail_types from the live ontology);
# subject-agnostic (no rel/type/name literal). A type-SATISFYING object (Event/Concept for
# participated_in — the reified-event lane) is UNAFFECTED.
def _typecheck_veto_on() -> bool:
    return _flag_on("WGM_TYPE_MISMATCH_VETO", "true")


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
        """Get ontology entry for a rel_type (includes head_types, tail_types, engine_generated)."""
        self.get_valid_types()  # ensure cache is fresh
        return self._ontology.get(rel_type.lower(), default or {})


# DEPRECATED: Kept for test compatibility. RelTypeRegistry reads from Postgres at runtime.
# Added W3C-aligned types (instance_of, subclass_of, pref_name, same_as)
# See migrations/006_split_is_a.sql for standards alignment details.
# dprompt-064 Phase 1: Added correction_behavior, inverse_rel_type, is_symmetric fields
SEED_ONTOLOGY = {
    # Hierarchy rel_types — head_types/tail_types are ANY (classification is unconstrained)
    "is_a":           {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": True, "category": None, "head_types": ["ANY"], "tail_types": ["ANY"]},
    "instance_of":    {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": True, "category": None, "head_types": ["ANY"], "tail_types": ["ANY"]},
    "subclass_of":    {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": True, "category": None, "head_types": ["ANY"], "tail_types": ["ANY"]},
    "part_of":        {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": True, "category": None, "head_types": ["ANY"], "tail_types": ["ANY"]},
    "member_of":      {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": True, "category": None, "head_types": ["ANY"], "tail_types": ["ANY"]},
    # Relational rel_types — constrained head_types/tail_types
    "created_by":     {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": None, "head_types": ["ANY"], "tail_types": ["ANY"]},
    "works_for":      {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": "work", "head_types": ["Person"], "tail_types": ["Person", "Organization"]},
    "parent_of":      {"correction_behavior": "immutable", "inverse_rel_type": "child_of", "is_symmetric": False, "is_hierarchy_rel": False, "category": "family", "head_types": ["Person"], "tail_types": ["Person"]},
    "child_of":       {"correction_behavior": "immutable", "inverse_rel_type": "parent_of", "is_symmetric": False, "is_hierarchy_rel": False, "category": "family", "head_types": ["Person"], "tail_types": ["Person"]},
    "spouse":         {"correction_behavior": "supersede", "inverse_rel_type": "spouse", "is_symmetric": True, "is_hierarchy_rel": False, "category": "family", "head_types": ["Person"], "tail_types": ["Person"]},
    "sibling_of":     {"correction_behavior": "immutable", "inverse_rel_type": "sibling_of", "is_symmetric": True, "is_hierarchy_rel": False, "category": "family", "head_types": ["Person"], "tail_types": ["Person"]},
    "also_known_as":  {"correction_behavior": "hard_delete", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": "identity", "head_types": ["ANY"], "tail_types": ["SCALAR"]},
    "pref_name":      {"correction_behavior": "hard_delete", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": "identity", "head_types": ["ANY"], "tail_types": ["SCALAR"]},
    "same_as":        {"correction_behavior": "supersede", "inverse_rel_type": "same_as", "is_symmetric": True, "is_hierarchy_rel": False, "category": "identity", "head_types": ["ANY"], "tail_types": ["ANY"]},
    "related_to":     {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": None, "head_types": ["ANY"], "tail_types": ["ANY"]},
    "likes":          {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": None, "head_types": ["ANY"], "tail_types": ["ANY"]},
    "dislikes":       {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": None, "head_types": ["ANY"], "tail_types": ["ANY"]},
    "prefers":        {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": None, "head_types": ["ANY"], "tail_types": ["ANY"]},
    "owns":           {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": None, "head_types": ["Person", "Organization"], "tail_types": ["Animal", "Object", "Organization"]},
    "has_pet":        {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": "family", "head_types": ["Person"], "tail_types": ["Animal"]},
    "located_in":     {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": "location", "head_types": ["ANY"], "tail_types": ["Location"]},
    "educated_at":    {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": "work", "head_types": ["Person"], "tail_types": ["Organization"]},
    "lives_in":       {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": "location", "head_types": ["Person"], "tail_types": ["Location"]},
    "lives_at":       {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": "location", "head_types": ["Person"], "tail_types": ["Location", "SCALAR"]},
    "born_in":        {"correction_behavior": "immutable", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": "location", "head_types": ["Person"], "tail_types": ["Location"]},
    # Scalar rel_types
    "nationality":    {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": None, "head_types": ["Person"], "tail_types": ["SCALAR"]},
    "occupation":     {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": "work", "head_types": ["Person"], "tail_types": ["SCALAR"]},
    "born_on":        {"correction_behavior": "immutable", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": None, "head_types": ["Person"], "tail_types": ["SCALAR"]},
    "age":            {"correction_behavior": "hard_delete", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": None, "head_types": ["Person"], "tail_types": ["SCALAR"]},
    "height":         {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": None, "head_types": ["ANY"], "tail_types": ["SCALAR"]},
    "weight":         {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": None, "head_types": ["ANY"], "tail_types": ["SCALAR"]},
    "has_gender":     {"correction_behavior": "supersede", "inverse_rel_type": None, "is_symmetric": False, "is_hierarchy_rel": False, "category": None, "head_types": ["Person"], "tail_types": ["SCALAR"]},
    # Symmetric social rel_types
    "knows":          {"correction_behavior": "supersede", "inverse_rel_type": "knows", "is_symmetric": True, "is_hierarchy_rel": False, "category": "family", "head_types": ["Person"], "tail_types": ["Person"]},
    "friend_of":      {"correction_behavior": "supersede", "inverse_rel_type": "friend_of", "is_symmetric": True, "is_hierarchy_rel": False, "category": "family", "head_types": ["Person"], "tail_types": ["Person"]},
    "met":            {"correction_behavior": "supersede", "inverse_rel_type": "met", "is_symmetric": True, "is_hierarchy_rel": False, "category": None, "head_types": ["ANY"], "tail_types": ["ANY"]},
}


# UUID regex for canonical ID validation
_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE
)

# Schema-name shape guard: schema names (faultline_{slug}) are interpolated into
# "SET search_path TO {schema}" via f-string; restrict to safe identifier chars.
# Defense-in-depth for the interpolation pattern (used repo-wide) — not a refactor of it.
_SCHEMA_NAME_RE = re.compile(r'^[a-z0-9_]+$')


class SearchPathError(RuntimeError):
    """Tenant search_path could not be (re)applied or verified on this connection.

    A non-LOCAL SET search_path issued inside a transaction REVERTS on rollback,
    so a failed re-apply leaves the connection pointing at the public (seed
    template) schema. Continuing would risk silent cross-tenant/seed pollution —
    this error must propagate (fail the request) and must NEVER be swallowed.
    """
    pass


class WGMValidationGate:
    @staticmethod
    def validate_edge_inputs(edges: list) -> str | None:
        """
        Pre-flight shape validation for edge inputs. No DB required.
        Checks structural requirements before edges reach full ontology validation.
        Returns error message string if invalid, None if valid.

        This is the FIRST validation layer. Full ontology validation happens
        in validate_edge() which requires a DB connection.
        """
        if not isinstance(edges, list):
            return "edges must be an array"
        if len(edges) == 0:
            return "edges must not be empty"
        for i, edge in enumerate(edges):
            if not isinstance(edge, dict):
                return f"edges[{i}] must be an object"
            if "subject" not in edge or "object" not in edge or "rel_type" not in edge:
                return f"edges[{i}] missing required field (subject, object, rel_type)"
        return None

    def __init__(self, db_conn, registry: RelTypeRegistry = None, validator: LLMOutputValidator = None, schema_name: str = None):
        self.db_conn = db_conn
        self.registry = registry
        # Guard the schema name shape — it is interpolated into SET search_path
        # via f-string (defense-in-depth; see _SCHEMA_NAME_RE).
        if schema_name and not _SCHEMA_NAME_RE.match(schema_name):
            raise ValueError(
                f"Invalid tenant schema_name {schema_name!r}: must match "
                f"^[a-z0-9_]+$ (expected faultline_{{slug}}); refusing to "
                f"interpolate into SET search_path"
            )
        self._schema_name = schema_name
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

    def _reapply_search_path(self):
        """Re-apply per-user search_path after rollback to prevent public schema fallthrough.

        A non-LOCAL SET search_path issued inside a transaction REVERTS if that
        transaction rolls back, so this re-apply is the ONLY thing keeping
        subsequent unqualified SQL out of the public (seed template) schema.
        Failure here can never be silent: log CRIT and raise SearchPathError —
        a failed request (500) is strictly better than continuing on a
        connection whose tenant binding is unknown.
        """
        if not self._schema_name:
            return
        try:
            with self.db_conn.cursor() as cur:
                cur.execute(f"SET search_path TO {self._schema_name}")
                # Verify the SET actually took effect on this connection —
                # closes the case where SET "succeeds" but connection state
                # is not what we think.
                cur.execute("SHOW search_path")
                row = cur.fetchone()
                current_path = row[0] if row else ""
        except Exception as e:
            msg = (
                f"wgm.search_path_reapply_failed: could not re-apply search_path "
                f"to tenant schema '{self._schema_name}': {e}. Connection tenant "
                f"binding is unknown — refusing to continue (public-schema "
                f"fallthrough / cross-tenant pollution risk)."
            )
            log_crit(log, msg, schema=self._schema_name, error=str(e),
                     component="search_path")
            raise SearchPathError(msg) from e
        if self._schema_name not in (current_path or ""):
            msg = (
                f"wgm.search_path_reapply_failed: SET search_path executed but "
                f"verification failed — SHOW search_path returned "
                f"{current_path!r}, expected it to contain "
                f"'{self._schema_name}'. Connection tenant binding is wrong — "
                f"refusing to continue (public-schema fallthrough / "
                f"cross-tenant pollution risk)."
            )
            log_crit(log, msg, schema=self._schema_name,
                     search_path=current_path, component="search_path")
            raise SearchPathError(msg)

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
                # STRONG-INGEST ENTITY GROUNDING: consult the GLiNER2 batch type the
                # caller passed (subject_type) FIRST; only fall back to the DB type, then
                # to the hierarchy chain, when the caller gave us nothing authoritative.
                # A passed literal 'unknown' is treated as "no type" (DB default sentinel).
                if subject_type is not None and subject_type.lower() == "unknown":
                    subject_type = None
                if subject_type is None:
                    subject_type = self._resolve_entity_type(subject_id)
                if subject_type is None or (isinstance(subject_type, str) and subject_type.lower() == "unknown"):
                    # Walk instance_of/subclass_of upward (e.g. Rex → poodle → Animal)
                    # before giving up — the GLiNER2 root may live one hop away.
                    hierarchy_type = self._resolve_entity_type_via_hierarchy(subject_id)
                    subject_type = hierarchy_type or None
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

    def _passed_type_is_genuine_mismatch(
        self, passed_type: str, allowed_types, entity_id: str = None
    ) -> bool:
        """A CONCRETE caller-passed (GLiNER2) type genuinely violates the constraint when it
        is non-empty / non-'unknown', NOT in the allowed set, and the constraint is real
        (no 'any'). Hierarchy-reachability (a true subtype IS allowed) is honored: if the
        entity already carries hierarchy facts whose resolved type satisfies the constraint,
        it is NOT a mismatch. Metadata-only; subject-agnostic; no rel/type literal.

        Returns True ⇒ crater (genuine incompatible type); False ⇒ leave alone (allowed,
        unknown, hierarchy-reachable, or unconstrained — no new drops)."""
        pt = (passed_type or "").strip().lower()
        if not pt or pt == "unknown":
            return False  # no concrete signal → fail-safe, never crater
        allowed = [str(t).strip().lower() for t in (allowed_types or []) if t and str(t).strip()]
        if not allowed or "any" in allowed:
            return False  # unconstrained role
        if pt in allowed:
            return False  # directly satisfies
        # Hierarchy-reachability: a true subtype IS allowed. If the entity's hierarchy chain
        # resolves to an allowed type (e.g. Rex → poodle → Animal), it is NOT a mismatch.
        if entity_id is not None:
            try:
                resolved = self._resolve_entity_type_via_hierarchy(entity_id)
                if resolved and resolved.strip().lower() in allowed:
                    return False
            except Exception:
                pass  # fail-safe → fall through to mismatch=True only on a concrete conflict
        return True  # concrete, constrained, not allowed, not hierarchy-reachable → genuine

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

    def _recheck_type_authoritative(
        self,
        rel_type: str,
        subject_id: str,
        object_id: str,
        subject_type: str = None,
        object_type: str = None,
    ) -> tuple[bool, str]:
        """Re-validate head/tail type constraints by reading the rel_type row DIRECTLY
        from this gate's OWN db_conn (read-your-own-write) instead of the TTL overlay
        cache. Used ONLY by the post-expansion re-check so a just-committed constraint
        widening is seen DETERMINISTICALLY — the cache path is racy under concurrency
        (a concurrent in-flight read can keep the cache on the pre-widening snapshot,
        making the re-check intermittently still fail and silently DROP the edge).

        Returns (valid, reason). Same semantics as _check_type_constraints' type gate:
        ANY/SCALAR/empty → unconstrained-pass; otherwise the actual subject/object type
        must be in head_types/tail_types (case-insensitive). Fail-safe: on ANY error,
        fall back to the cache-based _check_type_constraints (prior behaviour).
        """
        try:
            subject_type = subject_type if subject_type and subject_type.strip() else None
            object_type = object_type if object_type and object_type.strip() else None
            with self.db_conn.cursor() as cur:
                cur.execute(
                    "SELECT head_types, tail_types FROM rel_types WHERE rel_type = %s",
                    (rel_type.lower(),),
                )
                row = cur.fetchone()
            if not row:
                # No row to read authoritatively — defer to the cache-based check.
                return self._check_type_constraints(
                    rel_type, subject_id, object_id,
                    subject_type=subject_type, object_type=object_type)
            head_types, tail_types = row[0], row[1]

            def _is_any(types) -> bool:
                return types is None or (len(types) == 1 and str(types[0]).lower() == "any")

            def _is_scalar(types) -> bool:
                return types is not None and len(types) == 1 and str(types[0]).lower() == "scalar"

            # Resolve actual types (caller-passed GLiNER2 type first, then DB, then hierarchy).
            if subject_type is None or subject_type.lower() == "unknown":
                subject_type = self._resolve_entity_type(subject_id) or subject_type
            if (subject_type is None or subject_type.lower() == "unknown"):
                subject_type = self._resolve_entity_type_via_hierarchy(subject_id) or subject_type
            if object_type is None or object_type.lower() == "unknown":
                object_type = self._resolve_entity_type(object_id) or object_type
            if (object_type is None or object_type.lower() == "unknown"):
                object_type = self._resolve_entity_type_via_hierarchy(object_id) or object_type

            # HEAD (subject) constraint
            if not _is_any(head_types):
                ht = [str(t).lower() for t in head_types]
                st = subject_type.lower() if subject_type else None
                if st and st != "unknown" and st not in ht and "any" not in ht:
                    return (False,
                            f"subject_type '{subject_type}' not allowed for '{rel_type}' (allowed: {head_types})")

            # TAIL (object) constraint — SCALAR tail skips the object-type check entirely.
            if not _is_any(tail_types) and not _is_scalar(tail_types):
                tt = [str(t).lower() for t in tail_types]
                ot = object_type.lower() if object_type else None
                if ot and ot != "unknown" and ot not in tt and "any" not in tt:
                    return (False,
                            f"object_type '{object_type}' not allowed for '{rel_type}' (allowed: {tail_types})")

            return (True, "ok_authoritative")
        except SearchPathError:
            raise  # tenant binding unknown — must never be swallowed
        except Exception as e:  # noqa: BLE001 — fail-safe to the existing cache-based check
            log.warning("wgm.authoritative_recheck_failed",
                        extra={"rel_type": rel_type, "error": str(e)})
            try:
                return self._check_type_constraints(
                    rel_type, subject_id, object_id,
                    subject_type=subject_type, object_type=object_type)
            except SearchPathError:
                raise  # tenant binding unknown — must never be swallowed
            except Exception:
                return (True, "type_unknown")

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
            try:
                self.db_conn.rollback()
                self._reapply_search_path()
            except SearchPathError:
                raise  # tenant binding unknown — must never be swallowed
            except Exception:
                pass
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
        Get fresh ontology resolved as PER-TENANT OVERLAY: public seed ∪ this gate's
        tenant schema rel_types (grown/curated), tenant rows overriding the seed.

        This closes the per-tenant ontology loop on the validation side: a rel_type
        approved into the tenant's own schema is now "valid" (not novel_unapproved)
        without a backend restart. Isolation holds — we only read self._schema_name's
        schema; growth never writes to public.

        Cache-hit path is DB-FREE (overlay module caches the seed globally and the
        tenant delta per schema, both TTL-refreshed and explicitly invalidated on
        approval). Falls back to the schema-blind RelTypeRegistry (then SEED_ONTOLOGY)
        if the overlay is unavailable, preserving prior behaviour.
        """
        try:
            from src.api import rel_type_overlay
            dsn = os.environ.get("POSTGRES_DSN", "")
            if dsn and self._schema_name:
                merged = rel_type_overlay.resolve_meta(dsn, self._schema_name)
                if merged:
                    return merged
        except Exception as e:
            log.warning(f"gate.overlay_ontology_resolve_failed (falling back): {e}")
        if self.registry:
            return self.registry.get_ontology()
        return SEED_ONTOLOGY

    def _invalidate_overlay(self) -> None:
        """Drop this tenant's rel_type overlay after an in-flow write so the SAME
        request (and subsequent ones) see the freshly approved/created rel_type
        without waiting for the TTL. Tenant-scoped: only this schema is invalidated."""
        try:
            from src.api import rel_type_overlay
            if self._schema_name:
                rel_type_overlay.invalidate(self._schema_name)
        except Exception:
            pass

    def _valid_types(self) -> set:
        """Known rel_types for THIS gate's tenant (public seed ∪ tenant overlay).

        Derived from get_current_ontology() so the per-tenant overlay drives the
        'valid vs novel_unapproved' decision. Lowercased set; cache-hit = DB-free.
        """
        return {rt.lower() for rt in self.get_current_ontology().keys()}

    def _is_user_correction(self, edge_dict: dict) -> bool:
        """Check if edge is an explicit user correction.

        Only returns True when the caller has set is_correction=True.
        High confidence alone does NOT make a fact a correction — a user
        stating a new fact for the first time has confidence=1.0 but is
        NOT correcting anything. The confidence bypass lives at the
        validate_edge level (raw_confidence >= 0.95), not here.
        """
        return bool(edge_dict.get("is_correction"))

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
                self._reapply_search_path()
            except SearchPathError:
                raise  # tenant binding unknown — must never be swallowed
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
                # SearchPathError intentionally propagates from here — tenant
                # binding unknown means we must fail the request, not continue.
                self._reapply_search_path()

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

    # ── Staged-facts hierarchy conflict cleanup ───────────────────────────
    # Mirrors _SYSTEM_ENTITY_NAMES from main.py — any name that is a generic
    # ontology token rather than a real entity.  Kept as a local frozenset to
    # avoid circular imports (gate.py ← main.py).
    _SYSTEM_ENTITY_NAMES: frozenset = frozenset({
        "faultline",
        "knowledgegraph", "knowledge graph", "knowledge_graph",
        "node", "edge", "entitynode", "entity node",
        "relationshipedge", "relationship edge",
        "object", "concept", "thing", "entity",
        "organization", "person", "location", "group", "place",
    })

    def _delete_conflicting_staged_hierarchy_facts(
        self,
        subject_id: str,
        rel_type: str,
        object_id: str,
        qdrant_url: str | None = None,
        user_id: str | None = None,
    ) -> int:
        """Delete staged_facts rows that contradict an incoming hierarchy fact.

        Fires when a hierarchy rel_type (is_hierarchy_rel=true, e.g. instance_of,
        subclass_of, part_of) is being ingested.  Two classes of rows are removed:

        1. Conflicting type rows — same subject, same rel_type, different object
           (e.g. "university instance_of person" when "university instance_of
           organization" is arriving).  Staged_facts has no superseded_at column;
           hard-delete is the correct action.

        2. Inverted rows — subject is a generic ontology token (matches
           _SYSTEM_ENTITY_NAMES) and rel_type is a hierarchy rel.  These are
           mis-extracted facts that have subject/object swapped (e.g. "person
           instance_of alexander").

        Qdrant cleanup is best-effort: a failed delete does not affect the gate
        decision or the DB operation.

        Returns the total number of staged_facts rows deleted.
        """
        rt_lower = rel_type.lower().strip()

        # Only act on hierarchy rel_types — check current ontology (metadata-driven).
        ontology = self.get_current_ontology()
        rel_meta = ontology.get(rt_lower, {})
        if not rel_meta.get("is_hierarchy_rel"):
            return 0

        deleted_ids: list[int] = []

        try:
            with self.db_conn.cursor() as cur:
                # --- Pass 1: conflicting type rows (same subject+rel, different object) ---
                cur.execute(
                    """
                    SELECT id FROM staged_facts
                    WHERE subject_id = %s
                      AND rel_type   = %s
                      AND object_id != %s
                      AND promoted_at IS NULL
                    """,
                    (subject_id, rt_lower, object_id),
                )
                conflict_rows = [r[0] for r in cur.fetchall()]

                # --- Pass 2: inverted rows (subject is a system entity token) ---
                subj_lower = str(subject_id).lower().strip()
                if subj_lower in self._SYSTEM_ENTITY_NAMES:
                    # The incoming fact itself is inverted — log and let the caller
                    # decide, but also purge any pre-existing inverted rows.
                    cur.execute(
                        """
                        SELECT id FROM staged_facts
                        WHERE subject_id = %s
                          AND rel_type   = %s
                          AND promoted_at IS NULL
                        """,
                        (subject_id, rt_lower),
                    )
                    inverted_rows = [r[0] for r in cur.fetchall()]
                else:
                    # Purge previously-written inverted rows (subject is a system token).
                    # These rows have the subject and object swapped; delete them regardless
                    # of which object_id the new fact carries.
                    cur.execute(
                        """
                        SELECT sf.id FROM staged_facts sf
                        WHERE sf.rel_type = %s
                          AND sf.promoted_at IS NULL
                          AND lower(sf.subject_id) = ANY(%s)
                        """,
                        (rt_lower, list(self._SYSTEM_ENTITY_NAMES)),
                    )
                    inverted_rows = [r[0] for r in cur.fetchall()]

                all_to_delete = list(set(conflict_rows + inverted_rows))
                if not all_to_delete:
                    return 0

                # Hard-delete: staged_facts has no superseded_at column.
                cur.execute(
                    "DELETE FROM staged_facts WHERE id = ANY(%s)",
                    (all_to_delete,),
                )
                deleted_count = cur.rowcount

            self.db_conn.commit()
            deleted_ids = all_to_delete

            log.info(
                "wgm.staged_hierarchy_conflicts_deleted",
                subject_id=str(subject_id)[:16],
                rel_type=rt_lower,
                incoming_object=str(object_id)[:16],
                conflict_rows=len(conflict_rows),
                inverted_rows=len(inverted_rows),
                deleted_count=deleted_count,
            )

        except Exception as e:
            log.error(
                "wgm.staged_hierarchy_conflict_delete_failed",
                subject_id=str(subject_id)[:16],
                rel_type=rt_lower,
                error=str(e),
            )
            try:
                self.db_conn.rollback()
                self._reapply_search_path()
            except SearchPathError:
                raise  # tenant binding unknown — must never be swallowed
            except Exception:
                pass
            return 0

        # Best-effort Qdrant cleanup — never affects gate decision.
        # Collision-safe: facts and staged_facts share a per-user collection with
        # independent BIGSERIAL sequences, so a bare-id delete could nuke the WRONG
        # table's point. These ids are staged_facts rows. Pass 1 deletes by the
        # (source_table, fact_id) payload filter (covers payload-tagged points);
        # Pass 2 deletes the deterministic derived point id (new-scheme points).
        # Legacy bare-int points are cleaned by re_embedder reconcile / re-sync.
        if deleted_ids and qdrant_url and user_id:
            try:
                from src.re_embedder.embedder import derive_collection, derive_qdrant_point_id
                import httpx as _httpx
                collection = derive_collection(user_id)
                for _did in deleted_ids:
                    _httpx.post(
                        f"{qdrant_url}/collections/{collection}/points/delete",
                        json={
                            "filter": {
                                "must": [
                                    {"key": "source_table", "match": {"value": "staged_facts"}},
                                    {"key": "fact_id", "match": {"value": _did}},
                                ]
                            }
                        },
                        timeout=5.0,
                    )
                _httpx.post(
                    f"{qdrant_url}/collections/{collection}/points/delete",
                    json={"points": [derive_qdrant_point_id("staged_facts", _did) for _did in deleted_ids]},
                    timeout=5.0,
                )
                log.info(
                    "wgm.staged_hierarchy_qdrant_cleanup",
                    collection=collection,
                    deleted_ids=deleted_ids,
                )
            except Exception as qe:
                log.warning(
                    "wgm.staged_hierarchy_qdrant_cleanup_failed",
                    error=str(qe),
                )

        return deleted_count

    # ── end staged-facts hierarchy conflict cleanup ───────────────────────

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

        # First check if rel_type exists directly (per-tenant: seed ∪ tenant overlay)
        if rt_lower in self._valid_types():
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

        # CROSS-TYPE ASYMMETRIC GUARD (metadata-driven, subject-agnostic — NO rel_type name check).
        # The homogeneous "both ends must be members of the same taxonomy" model below is only
        # correct for SAME-TYPE relations: peer groups / containment hierarchies whose head and
        # tail are the same kind (e.g. located_in Location→Location, sibling_of Person→Person).
        # An ASYMMETRIC CROSS-TYPE rel connects two DIFFERENT kinds BY DESIGN
        # (e.g. lives_in Person→Location, born_in Person→Location): requiring both ends to be
        # members of one homogeneous taxonomy is a category error that falsely demotes the
        # canonical residence/location fact to Class C. Such rels are typed by head_types/
        # tail_types via _check_type_constraints instead, which is the correct asymmetric gate.
        # Detect "cross-type" purely from the live ontology: concrete head set disjoint from
        # concrete tail set (generic ANY/SCALAR sentinels excluded). Fail-safe: any miss falls
        # through to the membership check below (no new drops).
        try:
            _ont = self.get_current_ontology().get(rt_lower, {}) or {}
            _generic = {"any", "scalar"}
            _h = {t.lower() for t in (_ont.get("head_types") or []) if t and t.lower() not in _generic}
            _t = {t.lower() for t in (_ont.get("tail_types") or []) if t and t.lower() not in _generic}
            if _h and _t and _h.isdisjoint(_t):
                return (True, "cross_type_asymmetric_defer_to_type_constraints", [])
        except Exception:
            pass  # fail-safe: fall through to the homogeneous membership check

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
        valid_types = self._valid_types()
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
                            self._reapply_search_path()
                        except SearchPathError:
                            raise  # tenant binding unknown — must never be swallowed
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

        # Staged-facts hierarchy conflict cleanup:
        # For any hierarchy rel_type (is_hierarchy_rel=true), delete staged_facts rows
        # that contradict the incoming fact before we proceed with validation or bypass.
        # This fires unconditionally — not just on corrections — because staged_facts
        # accumulate contradictions from prior incomplete extraction passes.
        # Also purges inverted rows (system-token subjects) left from before the
        # inversion guard was added to the ingest path.
        # qdrant_url and user_id passed via edge_args when available.
        _qdrant_url = edge_args.get("qdrant_url") or os.environ.get("QDRANT_URL")
        _user_id_for_qdrant = edge_args.get("user_id")
        self._delete_conflicting_staged_hierarchy_facts(
            subject_id=subject_id,
            rel_type=rt,
            object_id=object_id,
            qdrant_url=_qdrant_url,
            user_id=_user_id_for_qdrant,
        )

        # CONFIDENCE-GATED BYPASS LOGIC (dprompt-124):
        # High-confidence facts (>= 0.95) bypass validation gates entirely.
        # User-stated facts (confidence 1.0) and clear extractions (0.95+) are
        # trusted and skip type constraints, hierarchy, and category validation.
        raw_confidence = edge_args.get("confidence", 0.8) or 0.0
        is_user_correction = self._is_user_correction(edge_args)
        # USER-IS-TRUTH TYPE-VETO EXEMPTION (keyed on PROVENANCE only, subject-agnostic).
        # A type constraint (head_types/tail_types) is a validator/growth-guide for INFERRED
        # edges — it is NOT a veto on what the USER STATED. The user can validly say "a product
        # was born in 2019" / "my company was founded in 1998" / "the legend was born" — those
        # are TRUE and MUST land durable; the engine GROWS to fit them, it never quarantines
        # them because the subject/object doesn't match a Person-constrained rel. So when the
        # edge is user-authored (fact_provenance == 'user_stated') OR a correction, the
        # head/tail type-mismatch veto below MUST NOT crater/downgrade it — the mismatch is a
        # GROWTH signal, not a rejection. The veto STILL applies to llm_inferred / llm_learned
        # (its legitimate job: crater a bad inference like a car's GPS mis-filed into the event
        # graph). fact_provenance flows in via edge_args (source="mcp" → "user_stated") or the
        # explicit `provenance` param. NO rel/type/name literals — provenance only.
        _edge_provenance = (
            edge_args.get("fact_provenance")
            or (provenance if isinstance(provenance, str) else None)
            or ""
        ).strip().lower()
        _is_user_authored = (_edge_provenance == "user_stated") or is_user_correction
        # Set True when a user-authored edge's type genuinely mismatched (logged as a growth
        # candidate below); used to skip the constraint-driven type INFERENCE so we never stamp
        # a mismatching constraint type (e.g. Person) onto the user's actual entity (a product).
        _user_authored_type_mismatch = False
        # Fix B — TYPE-CONSISTENCY PRE-GATE on the confidence bypass.
        # A high-confidence LLM-extracted edge (e.g. a false `(user, parent_of, Rex)`
        # minted by /extract/rewrite) must NOT skip type constraints when its rel_type
        # carries CONCRETE type constraints (non-ANY, non-SCALAR) and the actual entity
        # types RESOLVE to a CONFLICT (Rex → beagle → Animal vs parent_of tail=Person).
        # Such an edge is type-inconsistent and must be type-checked/quarantined (routed to
        # the hierarchy_type_mismatch → Class C staged lane below), never written as Class A.
        #
        # Metadata-driven (reads head_types/tail_types from the live ontology, NO rel-name
        # literal); subject-agnostic (any kinship/typed rel whose object resolves to a
        # conflicting concrete type is gated identically). NEVER applies to a genuine USER
        # CORRECTION (user-is-truth: corrections are authoritative and grounding) — only the
        # non-correction high-confidence extraction path is type-pre-gated. Fail-safe: any
        # resolution miss / unknown type leaves the bypass intact (no new drops).
        _bypass_type_conflict = False
        if raw_confidence >= 0.95 and not is_user_correction:
            try:
                _ont = self.get_current_ontology().get(rt, {})
                _ht = _ont.get("head_types") or []
                _tt = _ont.get("tail_types") or []
                _ht_l = [t.lower() for t in _ht]
                _tt_l = [t.lower() for t in _tt]
                _head_any = (not _ht) or ("any" in _ht_l)
                _tail_any = (not _tt) or ("any" in _tt_l)
                _tail_scalar = (len(_tt_l) == 1 and _tt_l[0] == "scalar")
                # Only when the rel_type actually CONSTRAINS the offending role.
                if (not _head_any and _ht_l) or (not _tail_any and not _tail_scalar and _tt_l):
                    # GROUND-TRUTH TYPE CHECK: pass subject_type/object_type=None so the
                    # check resolves the AUTHORITATIVE stored/hierarchy type (entities table
                    # ∪ instance_of/subclass_of walk), NOT the extraction-provided type.
                    # This is load-bearing: a false `(user, parent_of, Rex)` is often
                    # CO-EXTRACTED with Rex mistyped as 'Person' (circularly inferred
                    # from the bad rel itself) and that wrong type is threaded straight into
                    # validation — which then "passes". Re-resolving from ground truth
                    # (Rex.entity_type='Animal', set by the real `has_pet`/`instance_of`
                    # edges) exposes the conflict the extraction type masked. A genuinely
                    # unknown object → "type_unknown" (no conflict) keeps the fast path.
                    _ty_ok, _ty_reason = self._check_type_constraints(
                        rt, subject_id, object_id,
                        subject_type=None,
                        object_type=None,
                    )
                    # A RESOLVED type conflict blocks the bypass so the normal path below
                    # routes it to the type-check / Class-C quarantine. TWO resolution
                    # shapes both count as a real conflict (subject-agnostic, metadata-only):
                    #   • "hierarchy_type_mismatch" — the object's REAL type was discovered
                    #     by walking instance_of/subclass_of (Rex → beagle → Animal) and
                    #     it contradicts the rel's tail constraint → routed to the documented
                    #     Class-C downgrade.
                    #   • "... not allowed for ..." — the object's REAL type was already
                    #     stored on the entity (e.g. Rex.entity_type='Animal', set when
                    #     the co-extracted `has_pet` edge typed it) and conflicts directly.
                    #     This is the LIVE ordering: the false `(user, parent_of, Rex)`
                    #     is co-extracted with `has_pet`, which types Rex as Animal first.
                    # We DELIBERATELY exclude "type_unknown"/"unconstrained"/"ok" (no real
                    # type → keep the fast path, no new drops). A genuine USER CORRECTION is
                    # already excluded above (is_user_correction guard). The downstream lane
                    # handles each reason (hierarchy → Class C; direct → growth/inference).
                    if (not _ty_ok) and (
                        _ty_reason == "hierarchy_type_mismatch"
                        or (isinstance(_ty_reason, str) and "not allowed for" in _ty_reason)
                    ):
                        # DETERMINISTIC QUARANTINE: return the documented
                        # hierarchy_type_mismatch signal so the ingest loop downgrades this
                        # type-inconsistent high-confidence edge to Class C (conf ≤0.3) —
                        # the SAME lane the normal hierarchy-resolved mismatch uses. We do
                        # NOT merely fall through to the normal type-check, because the
                        # direct "not allowed for" reason there routes to LLM constraint-
                        # inference → type_mismatch, which the ingest loop then OVERRIDES
                        # back to valid for user-provided edges ("user edges override type
                        # constraints"). That override would re-admit the false edge. The
                        # quarantine keeps the false `(user, parent_of, Rex)` out of the
                        # authoritative Class-A facts while remaining non-destructive
                        # (Class C, user-correctable). Metadata-driven, no rel-name literal.
                        # USER-IS-TRUTH EXEMPTION: a user-authored edge is NOT quarantined —
                        # it lands durable and the mismatch is logged as an ontology-growth
                        # candidate (widen the rel's head/tail_types to admit the user's world).
                        if not _is_user_authored:
                            _bypass_type_conflict = True
                            log.info("wgm.confidence_bypass_type_pregate",
                                     rel_type=rt, confidence=raw_confidence,
                                     reason=_ty_reason,
                                     note="type-inconsistent high-confidence edge quarantined to "
                                          "Class C (bypass denied)")
                            return {"status": "valid", "hierarchy_type_mismatch": True}
                        _user_authored_type_mismatch = True
                        log.info("wgm.type_veto_exempted_user_stated",
                                 rel_type=rt, provenance=_edge_provenance or "user_correction",
                                 reason=_ty_reason,
                                 note="user-authored fact lands durable despite type mismatch; "
                                      "ontology-growth candidate (widen head/tail_types)")

                    # PASSED-TYPE VETO (WGM_TYPE_MISMATCH_VETO, default ON): the ground-truth
                    # check above is edge-ORDER dependent — a freshly-minted object is still
                    # 'unknown' in the entities table when its OWN constrained edge validates
                    # (the co-extracted edge that GLiNER2-types it may not have committed yet),
                    # so a genuine mismatch escapes as "type_unknown" and the bypass enthrones
                    # it as durable Class A 1.0 (e.g. (user, participated_in, gps_system) where
                    # gps_system=Object ∉ participated_in.tail={Concept,Event} — poisoning L4).
                    # Honor the GLiNER2 type the CALLER passed (present at validate time, order-
                    # independent): a CONCRETE passed type that genuinely violates the constraint
                    # (not allowed, not hierarchy-reachable to an allowed subtype) is a confidence-
                    # KILLER → crater to Class C (we don't forget). A type-SATISFYING object
                    # (Event/Concept for participated_in — the reified-event lane) is UNTOUCHED:
                    # _passed_type_is_genuine_mismatch returns False for allowed/unknown/reachable.
                    elif _ty_ok and _typecheck_veto_on():
                        _tail_bad = (
                            (not _tail_any and not _tail_scalar and _tt_l)
                            and self._passed_type_is_genuine_mismatch(object_type, _tt, object_id)
                        )
                        _head_bad = (
                            (not _head_any and _ht_l)
                            and self._passed_type_is_genuine_mismatch(subject_type, _ht, subject_id)
                        )
                        if _tail_bad or _head_bad:
                            # USER-IS-TRUTH EXEMPTION: the veto craters a bad INFERENCE, never a
                            # user-authored fact. A user-stated "a product was born" / "the company
                            # was founded" genuinely violates a Person-constrained head — but it is
                            # TRUE, so it lands durable and the mismatch becomes a growth candidate.
                            if not _is_user_authored:
                                _bypass_type_conflict = True
                                log.info("wgm.confidence_bypass_passed_type_veto",
                                         rel_type=rt, confidence=raw_confidence,
                                         subject_type=subject_type, object_type=object_type,
                                         head_types=_ht, tail_types=_tt,
                                         role=("tail" if _tail_bad else "head"),
                                         note="GLiNER2-passed type genuinely violates constraint; "
                                              "durable-A cast craters to Class C (bypass denied)")
                                return {"status": "valid", "hierarchy_type_mismatch": True}
                            _user_authored_type_mismatch = True
                            log.info("wgm.type_veto_exempted_user_stated",
                                     rel_type=rt, provenance=_edge_provenance or "user_correction",
                                     subject_type=subject_type, object_type=object_type,
                                     head_types=_ht, tail_types=_tt,
                                     role=("tail" if _tail_bad else "head"),
                                     note="user-authored fact lands durable despite type mismatch; "
                                          "ontology-growth candidate (widen head/tail_types)")
            except Exception as _pge:
                log.warning("wgm.confidence_bypass_pregate_failed",
                            rel_type=rt, error=str(_pge)[:120])
        if (raw_confidence >= 0.95 or (is_user_correction and raw_confidence >= 0.9)) \
                and not _bypass_type_conflict:
            # Growth engine: for bypassed high-confidence facts, still infer entity types
            # from rel_type constraints. Prevents entities staying 'unknown' forever.
            ontology = self.get_current_ontology()
            entry = ontology.get(rt, {})
            head_types = entry.get("head_types") or []
            tail_types = entry.get("tail_types") or []
            head_any = not head_types or "any" in [t.lower() for t in head_types]
            tail_any = not tail_types or "any" in [t.lower() for t in tail_types]
            is_scalar = tail_types and "scalar" in [t.lower() for t in tail_types]
            # GROW, DON'T MISTYPE: when a user-authored fact genuinely mismatched the rel's
            # constraints (exempted above), do NOT stamp the constraint type onto the entity —
            # that would poison L4 (e.g. mark the user's product as 'Person' just because
            # born_on.head=Person). The mismatch is a rel-widening candidate, not an entity
            # re-type. Only fill types for user-authored edges whose types actually satisfy.
            if not _user_authored_type_mismatch:
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
        # USER-IS-TRUTH EXEMPTION: a user-authored fact (user_stated / correction) at
        # sub-0.95 confidence is NOT downgraded either — same principle as the bypass pregate.
        if not type_ok and type_reason == "hierarchy_type_mismatch" and _is_user_authored:
            log.info("wgm.type_veto_exempted_user_stated",
                     rel_type=rt, provenance=_edge_provenance or "user_correction",
                     reason="hierarchy_type_mismatch",
                     note="user-authored fact lands durable despite hierarchy type mismatch; "
                          "ontology-growth candidate (widen head/tail_types)")
            return {"status": "valid"}
        elif not type_ok and type_reason == "hierarchy_type_mismatch":
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
                    # DETERMINISM: re-validate against the row we JUST committed, read from
                    # our OWN db_conn (read-your-own-write), NOT the TTL overlay cache. The
                    # cache round-trip is racy under concurrency — a concurrent in-flight read
                    # could keep the cache on the pre-expansion snapshot, so the cache-based
                    # re-check (_check_type_constraints → get_current_ontology → overlay) would
                    # intermittently still report type_mismatch and DROP a just-admitted edge
                    # (the same input → fact flickers). The authoritative DB re-read is immune.
                    # Fail-safe: any error → fall back to the cache-based re-check.
                    type_ok2, type_reason2 = self._recheck_type_authoritative(
                        rt, subject_id, object_id,
                        subject_type=subject_type, object_type=object_type,
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

            # TEMPORAL MODEL: stamp the conflict-path facts INSERT with the request-level
            # temporal carried in edge_args (threaded from /ingest, already temporal_class-gated
            # per edge). Without this, a conflict-superseding fact (e.g. a SECOND `feels`) lands
            # here UNDATED ('now'/NULL) while the same triple is staged DATED elsewhere in the
            # ingest split — two copies of one fact, the undated facts row shadowing the dated
            # staged row at recall. Stamp here so the facts copy is consistently dated.
            # NEVER-DOWNGRADE on conflict (mirrors _commit_staged, main.py:3383): a NULL incoming
            # date must not clobber an existing stamp; a bare 'now' must not overwrite a real
            # past/future. Defaults preserve today's behaviour when no temporal is supplied.
            _tstatus = edge_args.get("temporal_status") or "now"
            if _tstatus not in ("now", "past", "future"):
                _tstatus = "now"
            _tevent = edge_args.get("event_date")
            _tgran = edge_args.get("event_date_granularity")
            # ASSERTION POLARITY (Q1): carry the edge's polarity onto the conflict-path facts INSERT
            # so a negated state superseding a prior fact stays NEGATED here too (parity with the
            # temporal stamp). EXCLUDED wins on conflict — the latest assertion's polarity. Defaults
            # 'affirmed' (today's behavior) when not supplied.
            _polarity = edge_args.get("polarity") or "affirmed"
            if _polarity not in ("affirmed", "negated"):
                _polarity = "affirmed"
            cur.execute(
                "INSERT INTO facts"
                " (subject_id, object_id, rel_type, provenance,"
                "  temporal_status, event_date, event_date_granularity, polarity)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (subject_id, object_id, rel_type) DO UPDATE SET"
                "   temporal_status = CASE WHEN EXCLUDED.temporal_status = 'now'"
                "     THEN facts.temporal_status ELSE EXCLUDED.temporal_status END,"
                "   event_date = COALESCE(EXCLUDED.event_date, facts.event_date),"
                "   event_date_granularity = COALESCE(EXCLUDED.event_date_granularity,"
                "     facts.event_date_granularity),"
                "   polarity = EXCLUDED.polarity"
                " RETURNING id",
                (subject_id, object_id, rt, provenance or "",
                 _tstatus, _tevent, _tgran, _polarity),
            )
            row = cur.fetchone()
            # ON CONFLICT DO UPDATE always RETURNS the row, so `row` is normally truthy
            # (insert or conflict-update). The SELECT fallback below is retained as a
            # belt-and-suspenders path to still mark old conflicting facts as contradicted
            # should RETURNING ever yield nothing.
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
                    " (rel_type, label, engine_generated, confidence, source, fact_class)"
                    " VALUES (%s, %s, true, 0.7, 'engine', 'B')"
                    " ON CONFLICT (rel_type) DO UPDATE SET"
                    # AUTHORITY GUARD: freeze fact_class for a SEEDED rel — a C-tier SEED (e.g. the
                    # inferred-tier `feels` marker) must not be bumped C→B by engine approval. The
                    # C→B promotion is for GROWN rels only. public read = seed authority; subject-agnostic.
                    "  fact_class = CASE WHEN rel_types.fact_class = 'C'"
                    "                     AND NOT EXISTS (SELECT 1 FROM public.rel_types _s WHERE _s.rel_type = rel_types.rel_type)"
                    "                    THEN 'B' ELSE rel_types.fact_class END",
                    (rel_type, rel_type.replace('_', ' ').title()),
                )
            self.db_conn.commit()
            # Per-tenant overlay: drop this schema's overlay so the freshly written
            # rel_type is visible to get_current_ontology() within THIS request.
            self._invalidate_overlay()
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
                model=LLMModels.get("rel_type_enrichment"),
                user_id=getattr(self, '_user_id', None),
                timeout=LLMTimeouts.get("ENRICHMENT"),
                operation="rel_type_enrichment",
            )

            if result.get("valid") and result.get("confidence", 0) >= 0.7:
                # Insert into rel_types with full metadata.
                # fact_class defaults to 'B' — gate-approved novel types enter the C→B→A
                # promotion loop; never default to 'C' which routes to 30-day expiry.
                # AUTHORITY GUARD (user > SEED > growth): freeze the structural CLASSIFICATION
                # of a SEEDED rel to the public seed so this enrichment write cannot re-classify
                # it (is_hierarchy_rel/category/is_symmetric/inverse). head_types/tail_types keep
                # their COALESCE behaviour. Novel rels (pin None) keep the inferred metadata.
                _seed_pin = _seed_structural_flags(self.db_conn, rel_type)
                _is_hier = result.get("is_hierarchy_rel", False)
                _category = result.get("category", "other")
                _is_symmetric = result.get("is_symmetric", False)
                _inverse = result.get("inverse_rel_type")
                if _seed_pin is not None:
                    _is_hier = _seed_pin["is_hierarchy_rel"]
                    _category = _seed_pin["category"]
                    _is_symmetric = _seed_pin["is_symmetric"]
                    _inverse = _seed_pin["inverse_rel_type"]
                with self.db_conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO rel_types"
                        " (rel_type, label, wikidata_pid, engine_generated, confidence,"
                        "  is_symmetric, inverse_rel_type, head_types, tail_types, is_hierarchy_rel, category, source, fact_class)"
                        " VALUES (%s, %s, %s, true, %s, %s, %s, %s, %s, %s, %s, %s, 'B')"
                        " ON CONFLICT (rel_type) DO UPDATE SET"
                        "  label = COALESCE(EXCLUDED.label, rel_types.label),"
                        "  is_symmetric = COALESCE(EXCLUDED.is_symmetric, rel_types.is_symmetric),"
                        "  inverse_rel_type = COALESCE(EXCLUDED.inverse_rel_type, rel_types.inverse_rel_type),"
                        "  head_types = COALESCE(EXCLUDED.head_types, rel_types.head_types),"
                        "  tail_types = COALESCE(EXCLUDED.tail_types, rel_types.tail_types),"
                        "  is_hierarchy_rel = COALESCE(EXCLUDED.is_hierarchy_rel, rel_types.is_hierarchy_rel),"
                        "  category = COALESCE(EXCLUDED.category, rel_types.category),"
                        "  confidence = GREATEST(rel_types.confidence, EXCLUDED.confidence),"
                        # AUTHORITY GUARD: freeze fact_class for a SEEDED rel — a C-tier SEED (e.g. the
                    # inferred-tier `feels` marker) must not be bumped C→B by engine approval. The
                    # C→B promotion is for GROWN rels only. public read = seed authority; subject-agnostic.
                    "  fact_class = CASE WHEN rel_types.fact_class = 'C'"
                    "                     AND NOT EXISTS (SELECT 1 FROM public.rel_types _s WHERE _s.rel_type = rel_types.rel_type)"
                    "                    THEN 'B' ELSE rel_types.fact_class END",
                        (
                            rel_type,
                            result.get("label", rel_type),
                            result.get("wikidata_pid"),
                            result.get("confidence", 0.7),
                            _is_symmetric,                     # seed-pinned when seeded
                            _inverse,                          # seed-pinned when seeded
                            result.get("head_types", ["Any"]),
                            result.get("tail_types", ["Any"]),
                            _is_hier,                          # seed-pinned when seeded
                            _category,                         # seed-pinned when seeded
                            "engine"
                        )
                    )
                self.db_conn.commit()
                # Per-tenant overlay invalidation (see _invalidate_overlay).
                self._invalidate_overlay()
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
                model=LLMModels.get("novel_rel_type_inference"),
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
            # AUTHORITY GUARD (user > SEED > growth): this expansion path exists to WIDEN
            # head_types/tail_types (admit a new subject/object type — additive growth, KEPT
            # below), but it also re-wrote the LLM's is_hierarchy_rel/category/is_symmetric/
            # inverse OVER the row. For a SEEDED rel that structural CLASSIFICATION is immutable
            # to growth — pin it to the public seed so a Person-object `owns` (or any seeded rel
            # used with a new type) can no longer be re-classified to hierarchy/family. Novel
            # rels (absent from public → pin is None) keep the inferred metadata as-is.
            _seed_pin = _seed_structural_flags(self.db_conn, rel_type)
            _is_hier = metadata.get("is_hierarchy_rel", False)
            _category = metadata.get("category", "other")
            _is_symmetric = metadata.get("is_symmetric", False)
            _inverse = metadata.get("inverse_rel_type")
            if _seed_pin is not None:
                _is_hier = _seed_pin["is_hierarchy_rel"]
                _category = _seed_pin["category"]
                _is_symmetric = _seed_pin["is_symmetric"]
                _inverse = _seed_pin["inverse_rel_type"]
            with self.db_conn.cursor() as cur:
                # CONSTRAINT EXPANSION IS WIDEN-ONLY, NEVER NARROW (subject-agnostic, metadata-driven).
                # This path exists to ADMIT an edge a constraint rejected — i.e. WIDEN head/tail to
                # include the new types. Blindly assigning EXCLUDED.{head,tail}_types REPLACES the
                # existing constraint and can NARROW a UNIVERSAL rel: a /expand batch emitting
                # `<concept> subclass_of <concept>` drove instance_of/subclass_of (seeded ANY = "classify
                # ANYTHING") down to {Concept}, after which the ingest type-fallback stamped every
                # instance_of OBJECT (e.g. an Animal "dog") as Concept — cross-domain typing bleed.
                # On UPDATE we therefore:
                #   • PRESERVE UNIVERSAL: if the existing OR incoming side is ANY / empty / NULL, the
                #     result is {ANY} — a universal classifier (instance_of/subclass_of/part_of) can
                #     never be narrowed by an inference event.
                #   • else UNION existing ∪ incoming (genuine widening for a constrained rel).
                # INSERT (brand-new rel) is unaffected — it takes the inferred constraint as-is.
                cur.execute(
                    """INSERT INTO rel_types
                       (rel_type, label, natural_language, is_symmetric, inverse_rel_type,
                        head_types, tail_types, is_hierarchy_rel, category, confidence,
                        engine_generated, source)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, 'engine')
                       ON CONFLICT (rel_type) DO UPDATE SET
                           is_symmetric      = EXCLUDED.is_symmetric,
                           inverse_rel_type  = EXCLUDED.inverse_rel_type,
                           head_types        = CASE
                               WHEN rel_types.head_types IS NULL
                                    OR rel_types.head_types = ARRAY[]::TEXT[]
                                    OR 'ANY' = ANY(rel_types.head_types)
                                    OR EXCLUDED.head_types IS NULL
                                    OR EXCLUDED.head_types = ARRAY[]::TEXT[]
                                    OR 'ANY' = ANY(EXCLUDED.head_types)
                                   THEN ARRAY['ANY']::TEXT[]
                               ELSE ARRAY(SELECT DISTINCT unnest(rel_types.head_types || EXCLUDED.head_types))
                           END,
                           tail_types        = CASE
                               WHEN rel_types.tail_types IS NULL
                                    OR rel_types.tail_types = ARRAY[]::TEXT[]
                                    OR 'ANY' = ANY(rel_types.tail_types)
                                    OR EXCLUDED.tail_types IS NULL
                                    OR EXCLUDED.tail_types = ARRAY[]::TEXT[]
                                    OR 'ANY' = ANY(EXCLUDED.tail_types)
                                   THEN ARRAY['ANY']::TEXT[]
                               ELSE ARRAY(SELECT DISTINCT unnest(rel_types.tail_types || EXCLUDED.tail_types))
                           END,
                           is_hierarchy_rel  = EXCLUDED.is_hierarchy_rel,
                           category          = EXCLUDED.category,
                           confidence        = EXCLUDED.confidence,
                           natural_language  = COALESCE(rel_types.natural_language, EXCLUDED.natural_language)
                    """,
                    (
                        rel_type,
                        metadata.get("label") or rel_type.replace("_", " ").title(),
                        metadata.get("natural_language"),
                        _is_symmetric,                       # seed-pinned when seeded
                        _inverse,                            # seed-pinned when seeded
                        metadata.get("head_types", ["ANY"]),  # WIDEN — kept (type admission)
                        metadata.get("tail_types", ["ANY"]),  # WIDEN — kept (type admission)
                        _is_hier,                            # seed-pinned when seeded
                        _category,                           # seed-pinned when seeded
                        metadata.get("confidence", 0.5),
                    )
                )

            self.db_conn.commit()

            # Per-tenant overlay invalidation (see _invalidate_overlay).
            self._invalidate_overlay()

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
            # SearchPathError intentionally propagates from here — tenant
            # binding unknown means we must fail the request, not continue.
            self._reapply_search_path()
            return False