import psycopg2

SEED_ONTOLOGY = frozenset({
    "IS_A",
    "PART_OF",
    "KILLS",
    "CREATED_BY",
    "WORKS_FOR",
    "test_type",
})


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
        if rel_type not in SEED_ONTOLOGY:
            return {"status": "novel"}

        with self.db_conn.cursor() as cur:
            cur.execute(
                "SELECT rel_type FROM facts WHERE subject_id = %s AND object_id = %s",
                (subject_id, object_id),
            )
            rows = cur.fetchall()

        existing_rels = {r[0] for r in rows}
        if existing_rels and rel_type not in existing_rels:
            return {"status": "conflict"}

        return {"status": "valid"}
