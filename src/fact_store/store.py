import psycopg2


class FactStoreManager:
    def __init__(self, db_conn):
        self.db_conn = db_conn

    def commit(self, connections: list[tuple], confidence: float = 1.0, source_weight: float = 1.0) -> int:
        """
        Insert edges into facts.
        connections: list of (user_id, subject_id, object_id, rel_type, provenance) or
                     (user_id, subject_id, object_id, rel_type, provenance, is_preferred_label).
        Returns count of rows attempted. Rolls back and re-raises on psycopg2.Error.
        """
        count = 0
        try:
            with self.db_conn.cursor() as cur:
                for row in connections:
                    if len(row) == 6:
                        user_id, sub, obj, rel, prov, is_preferred = row
                    else:
                        user_id, sub, obj, rel, prov = row
                        is_preferred = False

                    cur.execute(
                        "INSERT INTO facts"
                        " (user_id, subject_id, object_id, rel_type, provenance, confidence, source_weight, is_preferred_label)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                        " ON CONFLICT (user_id, subject_id, object_id, rel_type)"
                        " DO UPDATE SET"
                        "   confirmed_count = facts.confirmed_count + 1,"
                        "   last_seen_at    = now(),"
                        "   updated_at      = now()",
                        (user_id, sub, obj, rel, prov, confidence, source_weight, is_preferred),
                    )
                    count += 1
            self.db_conn.commit()
            return count
        except psycopg2.Error:
            self.db_conn.rollback()
            raise

    def mark_contradicted(self, old_id: int, new_id: int, penalty: float = 0.5) -> None:
        """
        Penalize old_id by reducing its confidence by penalty and linking it to new_id.
        Uses GREATEST to floor confidence at 0.0. Penalty is stored for audit.
        """
        with self.db_conn.cursor() as cur:
            cur.execute(
                "UPDATE facts SET"
                "  confidence = GREATEST(confidence - %s, 0.0),"
                "  contradicted_by = %s,"
                "  contradiction_confidence_penalty = %s,"
                "  updated_at = now()"
                " WHERE id = %s",
                (penalty, new_id, penalty, old_id),
            )
        self.db_conn.commit()
