import psycopg2
from src.fact_store.store import FactStoreManager

SEED_ONTOLOGY = {
    "is_a":           {"subject_role": "subtype",   "object_role": "supertype"},  # Wikidata P31 (instance of)
    "part_of":        {"subject_role": "component", "object_role": "whole"},      # Wikidata P361 (part of)
    "created_by":     {"subject_role": "creation",  "object_role": "creator"},    # Wikidata P170 (creator, inv)
    "works_for":      {"subject_role": "employee",  "object_role": "employer"},   # Wikidata P108 (employer, inv)
    "parent_of":      {"subject_role": "parent",    "object_role": "child"},      # Wikidata P40 (child)
    "child_of":       {"subject_role": "child",     "object_role": "parent"},     # Wikidata P40 (child, inv)
    "spouse":         {"subject_role": "partner",   "object_role": "partner"},    # Wikidata P26 (spouse)
    "sibling_of":     {"subject_role": "sibling",   "object_role": "sibling"},    # Wikidata P3373 (sibling)
    "also_known_as":  {"subject_role": "canonical", "object_role": "alias"},      # Wikidata P742/P1449 (pseudonym/nickname)
    "related_to":     {"subject_role": "entity",    "object_role": "entity"},     # Wikidata P1659 (see also) - loose mapping, domain-specific
    "likes":          {"subject_role": "subject",   "object_role": "target"},     # domain-specific
    "dislikes":       {"subject_role": "subject",   "object_role": "target"},     # domain-specific
    "prefers":        {"subject_role": "subject",   "object_role": "target"},     # domain-specific
    "owns":           {"subject_role": "owner",     "object_role": "property"},    # Wikidata P1830 (owner of, inv)
    "located_in":     {"subject_role": "entity",    "object_role": "location"},    # Wikidata P131 (located in admin entity)
    "educated_at":    {"subject_role": "student",   "object_role": "institution"}, # Wikidata P69 (educated at)
    "nationality":    {"subject_role": "person",    "object_role": "country"},     # Wikidata P27 (country of citizenship)
    "occupation":     {"subject_role": "person",    "object_role": "profession"},  # Wikidata P106 (occupation)
    "born_on":        {"subject_role": "person",    "object_role": "date"},        # Wikidata P569 (date of birth)
    "age":            {"subject_role": "person",    "object_role": "value"},       # domain-specific
    "knows":          {"subject_role": "person",    "object_role": "person"},      # Wikidata P1891 (influenced) - loose, domain-specific
    "friend_of":      {"subject_role": "person",    "object_role": "person"},      # domain-specific
    "met":            {"subject_role": "person",    "object_role": "person"},      # domain-specific
    "lives_in":       {"subject_role": "person",    "object_role": "location"},    # Wikidata P551 (residence)
    "born_in":        {"subject_role": "person",    "object_role": "location"},    # Wikidata P19 (place of birth)
    "has_gender":     {"subject_role": "person",    "object_role": "gender"},      # Wikidata P21 (sex or gender)
}

class WGMValidationGate:
    def __init__(self, db_conn):
        self.db_conn = db_conn

    def validate_edge(self, subject_id, object_id, rel_type: str,
                      user_id=None, provenance=None) -> dict:
        """
        Validate an incoming edge against the ontology and existing DB state.
        Returns {"status": "novel"} if rel_type not in ontology.
        Returns {"status": "valid"} when no contradiction exists.
        When user_id is supplied and a contradiction is detected (same user+subject+rel,
        different object): inserts the new fact, penalizes all superseded facts via
        mark_contradicted, and returns a conflict dict with the penalty details.
        """
        rt = rel_type.lower().strip()
        if rt not in SEED_ONTOLOGY:
            return {"status": "novel"}

        if user_id is None:
            return {"status": "valid"}

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
