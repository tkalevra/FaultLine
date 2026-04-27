import psycopg2

SEED_ONTOLOGY = {
    "is_a":       {"subject_role": "subtype",   "object_role": "supertype"},
    "part_of":    {"subject_role": "component", "object_role": "whole"},
    "kills":      {"subject_role": "agent",     "object_role": "target"},
    "created_by": {"subject_role": "creation",  "object_role": "creator"},
    "works_for":  {"subject_role": "employee",  "object_role": "employer"},
    "parent_of":  {"subject_role": "parent",    "object_role": "child"},
    "related_to": {"subject_role": "entity",    "object_role": "entity"},
    "test_type":  {"subject_role": "subject",   "object_role": "object"},
}


class WGMValidationGate:
    def __init__(self, db_conn):
        self.db_conn = db_conn

    def validate_edge(self, subject_id, object_id, rel_type: str) -> dict:
        """
        Validate an incoming edge against the ontology and existing DB state.
        Returns {"status": "novel"} if rel_type not in ontology.
        Returns {"status": "conflict"} if a different rel_type already exists for this pair.
        Returns {"status": "valid"} otherwise.
        """
        if rel_type.lower() not in SEED_ONTOLOGY:
            return {"status": "novel"}

        with self.db_conn.cursor() as cur:
            cur.execute(
                "SELECT rel_type FROM facts WHERE subject_id = %s AND object_id = %s",
                (subject_id, object_id),
            )
            rows = cur.fetchall()

            cur.execute(
                "SELECT 1 FROM facts WHERE subject_id = %s AND object_id = %s AND rel_type = %s",
                (object_id, subject_id, rel_type.lower()),
            )
            reverse = cur.fetchone()

        if reverse:
            return {"status": "conflict"}

        existing_rels = {r[0].lower() for r in rows}
        if existing_rels and rel_type.lower() not in existing_rels:
            return {"status": "conflict"}

        return {"status": "valid"}
