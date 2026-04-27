import psycopg2


class FactStoreManager:
    def __init__(self, db_conn):
        self.db_conn = db_conn

    def commit(self, connections: list[tuple]) -> int:
        """
        Insert edges into fact_store_relationships.
        connections: list of (user_id, subject_id, object_id, rel_type, provenance).
        Returns count of rows committed. Rolls back and re-raises on psycopg2.Error.
        """
        count = 0
        try:
            with self.db_conn.cursor() as cur:
                for user_id, sub, obj, rel, prov in connections:
                    cur.execute(
                        "INSERT INTO facts (user_id, subject_id, object_id, rel_type, provenance)"
                        " VALUES (%s, %s, %s, %s, %s)"
                        " ON CONFLICT (user_id, subject_id, object_id, rel_type) DO NOTHING",
                        (user_id, sub, obj, rel, prov),
                    )
                    count += 1
            self.db_conn.commit()
            return count
        except psycopg2.Error:
            self.db_conn.rollback()
            raise
