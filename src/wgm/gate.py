import psycopg2
from fact_store.store import FactStoreManager

SEED_ONTOLOGY = {
    "is_a":           {"subject_role": "subtype",   "object_role": "supertype"},
    "part_of":        {"subject_role": "component", "object_role": "whole"},
    "created_by":     {"subject_role": "creation",  "object_role": "creator"},
    "works_for":      {"subject_role": "employee",  "object_role": "employer"},
    "parent_of":      {"subject_role": "parent",    "object_role": "child"},
    "child_of":       {"subject_role": "child",     "object_role": "parent"},
    "spouse":         {"subject_role": "partner",   "object_role": "partner"},
    "sibling_of":     {"subject_role": "sibling",   "object_role": "sibling"},
    "also_known_as":  {"subject_role": "canonical", "object_role": "alias"},
    "related_to":     {"subject_role": "entity",    "object_role": "entity"},
    "likes":          {"subject_role": "subject",   "object_role": "target"},
    "dislikes":       {"subject_role": "subject",   "object_role": "target"},
    "prefers":        {"subject_role": "subject",   "object_role": "target"},
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
        different object): inserts the new fact, marks the old one as contradicted, and
        returns {"status": "conflict", "new_id": int, "old_id": int}.
        """
        rt = rel_type.lower().strip()
        if rt not in SEED_ONTOLOGY:
            return {"status": "novel"}

        if user_id is None:
            return {"status": "valid"}

        with self.db_conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM facts"
                " WHERE user_id = %s AND subject_id = %s AND rel_type = %s AND object_id != %s",
                (user_id, subject_id, rt, object_id),
            )
            old_row = cur.fetchone()
            if old_row is None:
                return {"status": "valid"}

            old_id = old_row[0]
            cur.execute(
                "INSERT INTO facts (user_id, subject_id, object_id, rel_type, provenance)"
                " VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (user_id, subject_id, object_id, rt, provenance or ""),
            )
            new_id = cur.fetchone()[0]

        self.db_conn.commit()
        FactStoreManager(self.db_conn).mark_contradicted(old_id, new_id)
        return {"status": "conflict", "new_id": new_id, "old_id": old_id}
