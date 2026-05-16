import os
import re
import time
import json
import httpx
import psycopg2
import logging
from src.fact_store.store import FactStoreManager

log = logging.getLogger(__name__)


class RelTypeRegistry:
    def __init__(self, dsn: str, ttl_seconds: int = 60):
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
                    cur.execute(
                        "SELECT rel_type, head_types, tail_types FROM rel_types"
                    )
                    self._cache = set()
                    self._ontology = {}
                    for row in cur.fetchall():
                        rel_type = row[0]
                        self._cache.add(rel_type)
                        self._ontology[rel_type] = {
                            "head_types": row[1],  # list or None
                            "tail_types": row[2],  # list or None
                        }
                    self._loaded_at = time.time()
        except Exception:
            # If DB unavailable, fall back to SEED_ONTOLOGY
            self._cache = set(SEED_ONTOLOGY.keys())
            self._ontology = {
                rt: {"head_types": None, "tail_types": None}
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
SEED_ONTOLOGY = {
    "is_a":           {"subject_role": "entity",     "object_role": "type"},       # P31/P279 (deprecated: use instance_of or subclass_of)
    "instance_of":    {"subject_role": "entity",     "object_role": "type"},       # Wikidata P31 (instance of)
    "subclass_of":    {"subject_role": "type",       "object_role": "type"},       # Wikidata P279 (subclass of)
    "part_of":        {"subject_role": "component",  "object_role": "whole"},      # Wikidata P361 (part of)
    "created_by":     {"subject_role": "creation",   "object_role": "creator"},    # Wikidata P170 (creator, inv)
    "works_for":      {"subject_role": "employee",   "object_role": "employer"},   # Wikidata P108 (employer, inv)
    "parent_of":      {"subject_role": "parent",     "object_role": "child"},      # Wikidata P40 (child)
    "child_of":       {"subject_role": "child",      "object_role": "parent"},     # Wikidata P40 (child, inv)
    "spouse":         {"subject_role": "partner",    "object_role": "partner"},    # Wikidata P26 (spouse, symmetric)
    "sibling_of":     {"subject_role": "sibling",    "object_role": "sibling"},    # Wikidata P3373 (sibling, symmetric)
    "also_known_as":  {"subject_role": "canonical",  "object_role": "alias"},      # Wikidata P742/P1449 (skos:altLabel)
    "pref_name":      {"subject_role": "entity",     "object_role": "name"},       # preferred display name (skos:prefLabel)
    "same_as":        {"subject_role": "entity",     "object_role": "entity"},     # owl:sameAs (symmetric, identity equiv)
    "related_to":     {"subject_role": "entity",     "object_role": "entity"},     # Wikidata P1659 (see also)
    "likes":          {"subject_role": "subject",    "object_role": "target"},     # domain-specific
    "dislikes":       {"subject_role": "subject",    "object_role": "target"},     # domain-specific
    "prefers":        {"subject_role": "subject",    "object_role": "target"},     # domain-specific
    "owns":           {"subject_role": "owner",      "object_role": "property"},   # Wikidata P1830 (owner of, inv)
    "located_in":     {"subject_role": "entity",     "object_role": "location"},   # Wikidata P131 (located in)
    "educated_at":    {"subject_role": "student",    "object_role": "institution"},# Wikidata P69 (educated at)
    "nationality":    {"subject_role": "person",     "object_role": "country"},    # Wikidata P27 (citizenship)
    "occupation":     {"subject_role": "person",     "object_role": "profession"}, # Wikidata P106 (occupation)
    "born_on":        {"subject_role": "person",     "object_role": "date"},       # Wikidata P569 (date of birth)
    "age":            {"subject_role": "person",     "object_role": "value"},      # domain-specific
    "knows":          {"subject_role": "person",     "object_role": "person"},     # Wikidata P1891 (influenced, symmetric)
    "friend_of":      {"subject_role": "person",     "object_role": "person"},     # domain-specific (symmetric)
    "met":            {"subject_role": "person",     "object_role": "person"},     # domain-specific (symmetric)
    "lives_in":       {"subject_role": "person",     "object_role": "location"},   # Wikidata P551 (residence)
    "born_in":        {"subject_role": "person",     "object_role": "location"},   # Wikidata P19 (place of birth)
    "has_gender":     {"subject_role": "person",     "object_role": "gender"},     # Wikidata P21 (sex or gender)
}

# Symmetric relationships: storing A→B implies B→A
_SYMMETRIC_TYPES = {"spouse", "sibling_of", "same_as", "friend_of", "knows", "met"}

# UUID regex for canonical ID validation
_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE
)

class WGMValidationGate:
    def __init__(self, db_conn, registry: RelTypeRegistry = None):
        self.db_conn = db_conn
        self.registry = registry
        # Load ontology at startup (includes type constraints)
        self._ontology = registry.get_ontology() if registry else {
            rt: {"head_types": None, "tail_types": None}
            for rt in SEED_ONTOLOGY.keys()
        }

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
        # Look up head_types and tail_types from ontology
        ontology_entry = self._ontology.get(rel_type.lower())
        if not ontology_entry:
            return (True, "unconstrained")

        head_types = ontology_entry.get("head_types")
        tail_types = ontology_entry.get("tail_types")

        # None or ARRAY['ANY'] means unconstrained
        if (head_types is None or head_types == ["ANY"]) and (tail_types is None or tail_types == ["ANY"]):
            return (True, "unconstrained")

        # SCALAR tail type: skip object type check entirely
        if tail_types == ["SCALAR"]:
            # Still validate head_types for subject
            if head_types and head_types != ["ANY"]:
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
                if subject_type not in head_types and "ANY" not in head_types:
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

        # Constraint checks
        head_ok = head_types is None or "ANY" in head_types or subject_type in head_types
        tail_ok = tail_types is None or "ANY" in tail_types or object_type in tail_types

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
    # When user corrects a fact (e.g., "Aurora is a computer"), archive conflicting facts
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

    def _is_user_correction(self, edge_dict: dict) -> bool:
        """Check if edge is marked as user correction (high confidence or explicit flag)."""
        # Explicit flag
        if edge_dict.get("is_correction"):
            return True
        # High confidence (1.0 or 0.9+) implies user-stated
        confidence = edge_dict.get("confidence", 0.5)
        return confidence >= 0.9

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

    # ── end dprompt-90 ──────────────────────────────────────────────────

    def validate_edge(self, subject_id, object_id, rel_type: str,
                      user_id=None, provenance=None, subject_type: str = None,
                      object_type: str = None, **edge_kwargs) -> dict:
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
        if user_id and self._is_user_correction(edge_kwargs):
            conflicting_rels = self._find_conflicting_relationships(user_id, subject_id, rel_type)
            if conflicting_rels:
                self._supersede_conflicting_facts(
                    user_id, subject_id, conflicting_rels,
                    reason=f"user_correction:{rel_type.lower()}"
                )

        rt = rel_type.lower().strip()

        # Check against registry or SEED_ONTOLOGY
        valid_types = self.registry.get_valid_types() if self.registry else set(SEED_ONTOLOGY.keys())

        if rt not in valid_types:
            # Novel type: do NOT attempt LLM approval at ingest time.
            # Return "unknown" so the ingest layer stores as Class C (ephemeral)
            # and records the candidate in ontology_evaluations for async review
            # by the re-embedder. See dprompt-17.
            return {"status": "unknown"}

        # Check type constraints
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

        if user_id is None:
            return {"status": "valid"}

        # Check for symmetric duplicates: if A→B exists and rel_type is symmetric,
        # do not insert B→A again (it's implicitly the same fact in both directions)
        if rt in _SYMMETRIC_TYPES:
            with self.db_conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM facts"
                    " WHERE user_id = %s AND subject_id = %s AND object_id = %s AND rel_type = %s",
                    (user_id, subject_id, object_id, rt),
                )
                if cur.fetchone():
                    return {"status": "valid", "note": "duplicate_exact"}

                cur.execute(
                    "SELECT id FROM facts"
                    " WHERE user_id = %s AND subject_id = %s AND object_id = %s AND rel_type = %s",
                    (user_id, object_id, subject_id, rt),
                )
                if cur.fetchone():
                    return {"status": "valid", "note": "symmetric_duplicate"}

        with self.db_conn.cursor() as cur:
            cur.execute(
                "SELECT id, confidence FROM facts"
                " WHERE user_id = %s AND subject_id = %s AND rel_type = %s AND object_id != %s",
                (user_id, subject_id, rt, object_id),
            )
            old_rows = cur.fetchall()
            if not old_rows:
                return {"status": "valid"}

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
            # Refresh cache so the new type is immediately visible
            if self.registry:
                self.registry._refresh()
                self._ontology = self.registry.get_ontology()
            return True
        except Exception:
            return False

    def _try_approve_novel_type(self, rel_type: str) -> bool:
        """
        Call Qwen to validate a novel rel_type. If Qwen is unavailable or fails,
        auto-approves the type so facts are not silently dropped.
        If approved with confidence >= 0.7, inserts into rel_types and returns True.
        Otherwise, inserts into pending_types and returns False.
        """
        qwen_url = os.getenv("QWEN_API_URL")
        if not qwen_url:
            return self._auto_approve_novel_type(rel_type)

        try:
            response = httpx.post(
                qwen_url,
                json={
                    "model": os.getenv("WGM_LLM_MODEL", "qwen/qwen3.5-9b"),
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are a knowledge graph ontology validator. Respond only with valid JSON, no markdown."
                        },
                        {
                            "role": "user",
                            "content": f'Is \'{rel_type}\' a valid relationship type for a personal knowledge graph? Consider Wikidata properties as reference. Respond with exactly: {{"valid": true/false, "label": "human readable label", "wikidata_pid": "Pxxx or null", "confidence": 0.0-1.0, "reason": "one sentence"}}'
                        }
                    ],
                    "temperature": 0.0,
                    "max_tokens": 200,
                    "thinking": {"type": "disabled"},
                },
                timeout=10.0,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"].strip()
            result = json.loads(content)

            if result.get("valid") and result.get("confidence", 0) >= 0.7:
                # Insert into rel_types
                with self.db_conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO rel_types (rel_type, label, wikidata_pid, engine_generated, confidence)"
                        " VALUES (%s, %s, %s, true, %s)"
                        " ON CONFLICT (rel_type) DO NOTHING",
                        (rel_type, result.get("label", rel_type), result.get("wikidata_pid"), result.get("confidence", 0.7))
                    )
                self.db_conn.commit()
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
