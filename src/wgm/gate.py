import psycopg2

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
}

class WGMValidationGate:
    def __init__(self, db_conn):
        self.db_conn = db_conn

    def validate_edge(self, subject_id, object_id, rel_type: str) -> dict:
        """
        Validate an incoming edge against the ontology and existing DB state.
        Returns {"status": "novel"} if rel_type not in ontology.
        Returns {"status": "conflict"} if a contradicting rel_type exists for this pair.
        Returns {"status": "valid"} otherwise.
        """
        rt = rel_type.lower().strip()
        if rt not in SEED_ONTOLOGY:
            return {"status": "novel"}

        with self.db_conn.cursor() as cur:
            # Check for existing relations in the same direction
            cur.execute("SELECT rel_type FROM facts WHERE subject_id = %s AND object_id = %s", (subject_id, object_id))
            rows = cur.fetchall()
            existing_rels = {r[0].lower() for r in rows}
            
            # Check for reverse relations (conflict detection for directed edges)
            # Symmetric relations and aliases are exempt from reverse-direction conflict checks
            if rt not in ["spouse", "sibling_of", "also_known_as", "related_to"]:
                cur.execute("SELECT 1 FROM facts WHERE subject_id = %s AND object_id = %s AND rel_type = %s", (object_id, subject_id, rt))
                if cur.fetchone():
                    return {"status": "conflict"}

        return {"status": "valid"}