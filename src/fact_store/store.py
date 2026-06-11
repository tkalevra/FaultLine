import psycopg2


class FactStoreManager:
    def __init__(self, db_conn):
        self.db_conn = db_conn

    def commit(self, connections: list[tuple], confidence: float = 1.0, unified_confidence: float = None, fact_class: str = "A", fact_provenance: str = "llm_inferred") -> int:
        """
        Insert edges into facts with temporal metadata.
        connections: list of tuples with varying length:
                     (user_id, subject_id, object_id, rel_type, provenance) - 5 elements
                     (user_id, subject_id, object_id, rel_type, provenance, is_preferred_label) - 6 elements
                     (user_id, subject_id, object_id, rel_type, provenance, is_preferred_label, definition) - 7 elements
                     (user_id, subject_id, object_id, rel_type, provenance, is_preferred_label, definition, storage_type, is_hierarchy_rel, taxonomies) - 13 elements
                     (with temporal: ...taxonomies, statement_date, valid_until, temporal_confidence) - 16 elements
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
                    statement_date = None
                    valid_until = None
                    temporal_confidence = None

                    if len(row) >= 16:
                        # Full row with temporal metadata
                        user_id, sub, obj, rel, prov, is_preferred, definition, storage_type, is_hierarchy_rel, taxonomies = row[:10]
                        # Temporal fields follow
                        if len(row) > 13:
                            statement_date = row[13]
                            valid_until = row[14] if len(row) > 14 else None
                            temporal_confidence = row[15] if len(row) > 15 else None
                    elif len(row) >= 13:
                        # Row with taxonomies but no temporal fields
                        user_id, sub, obj, rel, prov, is_preferred, definition, storage_type, is_hierarchy_rel, taxonomies = row[:10]
                    elif len(row) >= 10:
                        user_id, sub, obj, rel, prov, is_preferred, definition, storage_type, is_hierarchy_rel, taxonomies = row[:10]
                    elif len(row) >= 7:
                        user_id, sub, obj, rel, prov, is_preferred, definition = row
                    elif len(row) == 6:
                        user_id, sub, obj, rel, prov, is_preferred = row
                    else:
                        user_id, sub, obj, rel, prov = row
                        is_preferred = False

                    # dprompt-130: Populate taxonomies live from entity_taxonomies metadata
                    # If taxonomies not provided in tuple, look them up based on rel_type
                    # This ensures facts are tagged with their taxonomies as the system grows
                    if not taxonomies:
                        try:
                            cur.execute(
                                "SELECT array_agg(DISTINCT taxonomy_name) FROM entity_taxonomies "
                                "WHERE rel_types_defining_group @> ARRAY[%s]",
                                (rel,)
                            )
                            result = cur.fetchone()
                            taxonomies = result[0] if result and result[0] else []
                        except Exception:
                            # Silently fail if lookup fails - insert will proceed with empty taxonomies
                            taxonomies = []

                    cur.execute(
                        "INSERT INTO facts"
                        " (subject_id, object_id, rel_type, provenance, confidence, unified_confidence, is_preferred_label, rel_type_definition, storage_type, is_hierarchy_rel, taxonomies, valid_from, valid_until, fact_class, fact_provenance)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                        " ON CONFLICT (subject_id, object_id, rel_type)"
                        " DO UPDATE SET"
                        "   fact_class = CASE"
                        "       WHEN 'A' IN (facts.fact_class, EXCLUDED.fact_class) THEN 'A'"
                        "       WHEN 'B' IN (facts.fact_class, EXCLUDED.fact_class) THEN 'B'"
                        "       ELSE 'C'"
                        "   END,"
                        "   confirmed_count = facts.confirmed_count + 1,"
                        "   last_seen_at    = now(),"
                        "   updated_at      = now(),"
                        "   superseded_at   = NULL,"
                        "   unified_confidence = EXCLUDED.unified_confidence,"
                        "   rel_type_definition = EXCLUDED.rel_type_definition,"
                        "   storage_type = COALESCE(EXCLUDED.storage_type, facts.storage_type),"
                        "   taxonomies = COALESCE(EXCLUDED.taxonomies, facts.taxonomies),"
                        "   valid_from = COALESCE(EXCLUDED.valid_from, facts.valid_from),"
                        "   valid_until = COALESCE(EXCLUDED.valid_until, facts.valid_until),"
                        "   fact_provenance = EXCLUDED.fact_provenance",
                        (sub, obj, rel, prov, confidence, unified_confidence, is_preferred, definition, storage_type, is_hierarchy_rel, taxonomies, statement_date, valid_until, fact_class, fact_provenance),
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
        Uses GREATEST to floor confidence at 0.0.
        """
        with self.db_conn.cursor() as cur:
            cur.execute(
                "UPDATE facts SET"
                "  confidence = GREATEST(confidence - %s, 0.0),"
                "  contradicted_by = %s,"
                "  updated_at = now()"
                " WHERE id = %s",
                (penalty, new_id, old_id),
            )
        self.db_conn.commit()

    def retract(self, cur, user_id: str, subject: str, rel_type: str | None,
                old_value: str | None, mode: str) -> list[int]:
        """
        Retract facts matching the given criteria. Returns list of affected fact IDs.
        Behavior is controlled by mode: 'hard_delete' (DELETE), 'supersede' (set superseded_at).
        subject and old_value are pre-resolved UUIDs (already lowercase).

        Searches both subject-side and object-side: if "forget marla" is issued,
        marla's UUID may be the object (user → spouse → marla). Searches subject
        first, falls back to object-side if no match.

        CLAUDE.md Compliance: Per-user schema isolation means NO user_id column.
        Caller must set search_path to user's schema before calling this method.
        """
        ids = []

        # Try subject-side match first
        if subject:
            conditions = ["subject_id = %s", "superseded_at IS NULL"]
            params = [subject]
            if rel_type:
                conditions.append("rel_type = %s")
                params.append(rel_type.lower())
            if old_value:
                conditions.append("object_id = %s")
                params.append(old_value)
            where = " AND ".join(conditions)
            cur.execute(f"SELECT id FROM facts WHERE {where}", params)
            ids = [r[0] for r in cur.fetchall()]

        # Fallback: object-side match (entity being retracted is the object)
        if not ids and subject:
            conditions = ["object_id = %s", "superseded_at IS NULL"]
            params = [subject]
            if rel_type:
                conditions.append("rel_type = %s")
                params.append(rel_type.lower())
            where = " AND ".join(conditions)
            cur.execute(f"SELECT id FROM facts WHERE {where}", params)
            ids = [r[0] for r in cur.fetchall()]

        # Also check staged_facts for Class B/C
        if not ids and subject:
            conditions = ["(subject_id = %s OR object_id = %s)", "promoted_at IS NULL"]
            params = [subject, subject]
            if rel_type:
                conditions.append("rel_type = %s")
                params.append(rel_type.lower())
            where = " AND ".join(conditions)
            cur.execute(f"SELECT id FROM staged_facts WHERE {where}", params)
            ids = [r[0] for r in cur.fetchall()]
            if ids:
                if mode == "hard_delete":
                    cur.execute(f"DELETE FROM staged_facts WHERE id = ANY(%s)", (ids,))
                else:
                    cur.execute(
                        "UPDATE staged_facts SET promoted_at = now() WHERE id = ANY(%s)", (ids,)
                    )
                return ids

        if not ids:
            return []

        if mode == "hard_delete":
            placeholders = ",".join(["%s"] * len(ids))
            cur.execute(f"DELETE FROM facts WHERE id IN ({placeholders})", ids)
        else:
            placeholders = ",".join(["%s"] * len(ids))
            cur.execute(
                f"UPDATE facts SET superseded_at = now(), qdrant_synced = false WHERE id IN ({placeholders})",
                ids,
            )
        return ids
