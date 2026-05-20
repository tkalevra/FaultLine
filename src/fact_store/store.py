import psycopg2


class FactStoreManager:
    def __init__(self, db_conn):
        self.db_conn = db_conn

    def commit(self, connections: list[tuple], confidence: float = 1.0, source_weight: float = 1.0, unified_confidence: float = None) -> int:
        """
        Insert edges into facts.
        connections: list of tuples with varying length:
                     (user_id, subject_id, object_id, rel_type, provenance) - 5 elements
                     (user_id, subject_id, object_id, rel_type, provenance, is_preferred_label) - 6 elements
                     (user_id, subject_id, object_id, rel_type, provenance, is_preferred_label, definition) - 7 elements
                     (user_id, subject_id, object_id, rel_type, provenance, is_preferred_label, definition, storage_type, is_hierarchy_rel, taxonomies) - 10 elements
        unified_confidence: blended confidence from LLMOutputValidator (frequency + llm_confidence).
                           Defaults to confidence if not provided.
        Returns count of rows attempted. Rolls back and re-raises on psycopg2.Error.
        """
        if unified_confidence is None:
            unified_confidence = confidence
        count = 0
        try:
            with self.db_conn.cursor() as cur:
                for row in connections:
                    definition = ""
                    storage_type = None
                    is_hierarchy_rel = False
                    taxonomies = []

                    if len(row) >= 10:
                        user_id, sub, obj, rel, prov, is_preferred, definition, storage_type, is_hierarchy_rel, taxonomies = row[:10]
                    elif len(row) >= 7:
                        user_id, sub, obj, rel, prov, is_preferred, definition = row
                    elif len(row) == 6:
                        user_id, sub, obj, rel, prov, is_preferred = row
                    else:
                        user_id, sub, obj, rel, prov = row
                        is_preferred = False

                    cur.execute(
                        "INSERT INTO facts"
                        " (user_id, subject_id, object_id, rel_type, provenance, confidence, unified_confidence, source_weight, is_preferred_label, rel_type_definition, storage_type, is_hierarchy_rel, taxonomies)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                        " ON CONFLICT (user_id, subject_id, object_id, rel_type)"
                        " DO UPDATE SET"
                        "   confirmed_count = facts.confirmed_count + 1,"
                        "   last_seen_at    = now(),"
                        "   updated_at      = now(),"
                        "   unified_confidence = EXCLUDED.unified_confidence,"
                        "   rel_type_definition = EXCLUDED.rel_type_definition,"
                        "   storage_type = COALESCE(EXCLUDED.storage_type, facts.storage_type),"
                        "   taxonomies = COALESCE(EXCLUDED.taxonomies, facts.taxonomies)",
                        (user_id, sub, obj, rel, prov, confidence, unified_confidence, source_weight, is_preferred, definition, storage_type, is_hierarchy_rel, taxonomies),
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

    def retract(self, cur, user_id: str, subject: str, rel_type: str | None,
                old_value: str | None, mode: str) -> list[int]:
        """
        Retract facts matching the given criteria. Returns list of affected fact IDs.
        Behavior is controlled by mode: 'hard_delete' (DELETE), 'supersede' (set superseded_at).
        subject and old_value are pre-resolved UUIDs (already lowercase).
        """
        conditions = ["user_id = %s", "subject_id = %s", "superseded_at IS NULL"]
        params = [user_id, subject]

        if rel_type:
            conditions.append("rel_type = %s")
            params.append(rel_type.lower())
        if old_value:
            conditions.append("object_id = %s")
            params.append(old_value)

        where = " AND ".join(conditions)

        cur.execute(f"SELECT id FROM facts WHERE {where}", params)
        ids = [r[0] for r in cur.fetchall()]
        if not ids:
            return []

        if mode == "hard_delete":
            cur.execute(f"DELETE FROM facts WHERE {where}", params)
        else:  # supersede
            cur.execute(
                f"UPDATE facts SET superseded_at = now(), qdrant_synced = false WHERE {where}",
                params,
            )
        return ids
