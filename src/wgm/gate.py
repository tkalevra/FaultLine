import os
import time
import json
import httpx
import psycopg2
from src.fact_store.store import FactStoreManager


class RelTypeRegistry:
    def __init__(self, dsn: str, ttl_seconds: int = 60):
        self.dsn = dsn
        self.ttl = ttl_seconds
        self._cache: set[str] = set()
        self._loaded_at: float = 0.0

    def get_valid_types(self) -> set[str]:
        now = time.time()
        if now - self._loaded_at > self.ttl or not self._cache:
            self._refresh()
        return self._cache

    def _refresh(self) -> None:
        try:
            with psycopg2.connect(self.dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT rel_type FROM rel_types")
                    self._cache = {row[0] for row in cur.fetchall()}
                    self._loaded_at = time.time()
        except Exception:
            # If DB unavailable, fall back to SEED_ONTOLOGY
            self._cache = set(SEED_ONTOLOGY.keys())
            self._loaded_at = time.time()

    def is_valid(self, rel_type: str) -> bool:
        return rel_type.lower() in self.get_valid_types()

    def all_types(self) -> list[str]:
        return sorted(self.get_valid_types())


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

class WGMValidationGate:
    def __init__(self, db_conn, registry: RelTypeRegistry = None):
        self.db_conn = db_conn
        self.registry = registry

    def validate_edge(self, subject_id, object_id, rel_type: str,
                      user_id=None, provenance=None) -> dict:
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
        rt = rel_type.lower().strip()

        # Check against registry or SEED_ONTOLOGY
        valid_types = self.registry.get_valid_types() if self.registry else set(SEED_ONTOLOGY.keys())

        if rt not in valid_types:
            # Novel type: try Qwen approval
            if not self._try_approve_novel_type(rt):
                return {"status": "novel"}
            # Refresh cache if registry exists
            if self.registry:
                self.registry._refresh()

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
                " VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (user_id, subject_id, object_id, rt, provenance or ""),
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

    def _try_approve_novel_type(self, rel_type: str) -> bool:
        """
        Call Qwen to validate a novel rel_type. If approved with confidence >= 0.7,
        insert into rel_types and return True. Otherwise, insert into pending_types
        and return False. On Qwen failure, always fall through gracefully (return False).
        """
        qwen_url = os.getenv("QWEN_API_URL")
        if not qwen_url:
            return False

        try:
            response = httpx.post(
                qwen_url,
                json={
                    "model": "qwen/qwen3.5-9b@q4_k_m",
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
            # Qwen failure: fall through gracefully, don't approve
            return False
