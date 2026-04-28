import psycopg2


class FactStoreManager:
    def __init__(self, db_conn):
        self.db_conn = db_conn

    def commit(self, connections: list[tuple], confidence: float = 1.0, source_weight: float = 1.0) -> int:
        """
        Insert edges into facts.
        connections: list of (user_id, subject_id, object_id, rel_type, provenance).
        Returns count of rows attempted. Rolls back and re-raises on psycopg2.Error.
        """
        count = 0
        try:
            with self.db_conn.cursor() as cur:
                for user_id, sub, obj, rel, prov in connections:
                    cur.execute(
                        "INSERT INTO facts"
                        " (user_id, subject_id, object_id, rel_type, provenance, confidence, source_weight)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                        " ON CONFLICT (user_id, subject_id, object_id, rel_type)"
                        " DO UPDATE SET"
                        "   confirmed_count = facts.confirmed_count + 1,"
                        "   last_seen_at    = now(),"
                        "   updated_at      = now()",
                        (user_id, sub, obj, rel, prov, confidence, source_weight),
                    )
                    count += 1
            self.db_conn.commit()
            return count
        except psycopg2.Error:
            self.db_conn.rollback()
            raise

    def mark_contradicted(self, old_id: int, new_id: int) -> None:
        with self.db_conn.cursor() as cur:
            cur.execute(
                "UPDATE facts SET contradicted_by = %s WHERE id = %s",
                (new_id, old_id),
            )
        self.db_conn.commit()
