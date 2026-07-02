import psycopg2
import structlog

log = structlog.get_logger()

# Canonical fact_provenance values (see main.py provenance routing, ~line 10282).
# Anything else is a caller bug — coerce to the weakest tier so a malformed
# value can never masquerade as (or overwrite) user_stated.
_CANONICAL_FACT_PROVENANCE = ("user_stated", "llm_inferred", "llm_learned")


class FactStoreManager:
    def __init__(self, db_conn):
        self.db_conn = db_conn

    def commit(self, connections: list[tuple], confidence: float = 1.0, unified_confidence: float = None, fact_class: str = "A", fact_provenance: str = "llm_inferred", source_ref: str | None = None) -> int:
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
        source_ref: citable provenance (URL/filename/title) applied to EVERY row in this
                    batch (migration 128) — a kwarg, NOT a tuple element, because the tuples
                    are positionally unpacked at several call sites. None (default) leaves
                    behavior byte-identical; ON CONFLICT never nulls an existing citation.
        Returns count of rows attempted. Rolls back and re-raises on psycopg2.Error.
        """
        if unified_confidence is None:
            unified_confidence = confidence
        # Canonical-value guard: a non-canonical fact_provenance is a caller bug.
        # Coerce to 'llm_inferred' (weakest reasonable tier) and log loudly —
        # never let a malformed value reach the upgrade-only ON CONFLICT logic.
        if fact_provenance not in _CANONICAL_FACT_PROVENANCE:
            log.error(
                "fact_store.non_canonical_fact_provenance",
                fact_provenance=fact_provenance,
                coerced_to="llm_inferred",
                allowed=list(_CANONICAL_FACT_PROVENANCE),
            )
            fact_provenance = "llm_inferred"
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
                        " (subject_id, object_id, rel_type, provenance, confidence, unified_confidence, is_preferred_label, rel_type_definition, storage_type, is_hierarchy_rel, taxonomies, valid_from, valid_until, fact_class, fact_provenance, source_ref)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
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
                        # CITABLE PROVENANCE (migration 128): a citation-less re-ingest never
                        # nulls an existing citation; a new citation wins.
                        "   source_ref = COALESCE(EXCLUDED.source_ref, facts.source_ref),"
                        # UPGRADE-ONLY provenance: user_stated is a one-way door. A later
                        # llm_inferred/llm_learned re-ingest of the same triple must NEVER
                        # downgrade a fact the user personally stated (the fact_class CASE
                        # above already keeps class A sticky; this keeps provenance sticky).
                        "   fact_provenance = CASE"
                        "       WHEN EXCLUDED.fact_provenance = 'user_stated' THEN 'user_stated'"
                        "       ELSE facts.fact_provenance"
                        "   END",
                        (sub, obj, rel, prov, confidence, unified_confidence, is_preferred, definition, storage_type, is_hierarchy_rel, taxonomies, statement_date, valid_until, fact_class, fact_provenance, source_ref),
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
                old_value: str | None, mode: str) -> tuple[list[int], str]:
        """
        Retract facts matching the given criteria. Returns (affected fact IDs,
        source_table) where source_table is 'facts', 'staged_facts', or 'both'.
        The source_table is REQUIRED for collision-safe Qdrant cleanup: facts and
        staged_facts share a per-user Qdrant collection and use independent
        BIGSERIAL sequences, so an integer id can exist in both tables. The
        caller must filter Qdrant deletes by (source_table, fact_id), not bare
        point ids — see _delete_from_qdrant / /retract/correct cleanup. A 'both'
        label tells the caller to run the collision-safe per-table filtered
        delete for BOTH labels over the returned union of ids (each pass is
        conjunctive on (fact_id, source_table), so a colliding id is only removed
        from the table whose payload actually matches — see _delete_from_qdrant).
        Empty id list returns ([], 'facts') by convention (no points to delete).

        DUAL-WRITE NOTE (hard_delete / unforget only): a single fact can be LIVE
        in BOTH `facts` AND `staged_facts` (the known feeling dual-write). For the
        TOMBSTONE modes ('hard_delete' = forget, 'unforget' = restore) we must hit
        BOTH copies or recall can still surface a "forgotten" fact (or unforget can
        leave a stale tombstone). These two modes therefore accumulate facts ids
        AND staged ids and apply the tombstone/restore UPDATE to BOTH tables. ALL
        OTHER modes (supersede/promote — ordinary correction/retraction) keep the
        EXACT first-match behavior: facts-first, staged only if facts matched
        nothing — correction semantics are unchanged.
        Behavior is controlled by mode:
          'hard_delete' — FORGET via TOMBSTONE (set archived_at + deleted_at; NOT a physical
                          DELETE). Recoverable for the grace window; the physical purge of
                          aged tombstones is a separate background phase. Sets qdrant_synced
                          = false so the re-embedder reconciles the Qdrant point away.
          'supersede'   — soft delete (set superseded_at).
          'unforget'    — UN-FORGET: clear the tombstone (archived_at = NULL, deleted_at =
                          NULL, qdrant_synced = false) on a SPECIFIC tombstoned target, within
                          the grace window. Restores the fact to the live view.
        subject and old_value are pre-resolved UUIDs (already lowercase).

        Searches both subject-side and object-side: if "forget jordan" is issued,
        jordan's UUID may be the object (user → spouse → jordan). Searches subject
        first, falls back to object-side if no match.

        BOUNDED TARGET ONLY: a forget/un-forget operates on the SPECIFIC resolved
        (subject_id, rel_type[, object_id]) target — there is no wildcard / "delete all" verb.

        CLAUDE.md Compliance: Per-user schema isolation means NO user_id column.
        Caller must set search_path to user's schema before calling this method.
        """
        ids = []

        # TOMBSTONE modes (forget/unforget) must reconcile the known feeling dual-write
        # where one fact is LIVE in BOTH `facts` AND `staged_facts`. For these modes we
        # process BOTH tables; every other mode keeps the original first-match behavior
        # (facts-first, staged only if facts matched nothing) so correction semantics are
        # untouched.
        _dual = mode in ("hard_delete", "unforget")

        # un-forget hunts for ALREADY-tombstoned rows; forget/supersede hunt for LIVE rows.
        # A tombstone sets deleted_at, so the live-row filter is `deleted_at IS NULL`.
        _live_facts_filter = "deleted_at IS NOT NULL" if mode == "unforget" else "deleted_at IS NULL"

        # Try subject-side match first
        if subject:
            conditions = ["subject_id = %s", _live_facts_filter]
            params = [subject]
            if mode != "unforget":
                conditions.append("superseded_at IS NULL")
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
            conditions = ["object_id = %s", _live_facts_filter]
            params = [subject]
            if mode != "unforget":
                conditions.append("superseded_at IS NULL")
            if rel_type:
                conditions.append("rel_type = %s")
                params.append(rel_type.lower())
            where = " AND ".join(conditions)
            cur.execute(f"SELECT id FROM facts WHERE {where}", params)
            ids = [r[0] for r in cur.fetchall()]

        facts_ids = ids

        # Also check staged_facts for Class B/C.
        #   • non-dual modes: ONLY when facts matched nothing (original first-match).
        #   • dual modes (forget/unforget): ALWAYS, so the dual-write second copy is
        #     reconciled even when a `facts` copy already matched.
        staged_ids = []
        if subject and (_dual or not facts_ids):
            # staged_facts has no archived_at; it leaves the live view via promoted_at.
            # A tombstoned staged row sets deleted_at (frozen from lifecycle bumps).
            _staged_filter = "deleted_at IS NOT NULL" if mode == "unforget" else \
                             "promoted_at IS NULL AND deleted_at IS NULL"
            conditions = ["(subject_id = %s OR object_id = %s)", _staged_filter]
            params = [subject, subject]
            if rel_type:
                conditions.append("rel_type = %s")
                params.append(rel_type.lower())
            # SCOPE TO THE NAMED OBJECT (mirror the facts query above): without this the
            # staged match was subject+rel only, so forget(feels, drained) tombstoned EVERY
            # staged feeling, not just drained. When a specific object is resolved, bound to it.
            if old_value:
                conditions.append("object_id = %s")
                params.append(old_value)
            where = " AND ".join(conditions)
            cur.execute(f"SELECT id FROM staged_facts WHERE {where}", params)
            staged_ids = [r[0] for r in cur.fetchall()]

        # Apply the staged-table UPDATE (tombstone/restore/promote).
        if staged_ids:
            if mode == "hard_delete":
                # TOMBSTONE (recoverable) — NOT a physical DELETE.
                cur.execute(
                    "UPDATE staged_facts SET deleted_at = now(), qdrant_synced = false "
                    "WHERE id = ANY(%s)", (staged_ids,)
                )
            elif mode == "unforget":
                # Clear the tombstone — restore to the live view.
                cur.execute(
                    "UPDATE staged_facts SET deleted_at = NULL, qdrant_synced = false "
                    "WHERE id = ANY(%s)", (staged_ids,)
                )
            else:
                cur.execute(
                    "UPDATE staged_facts SET promoted_at = now() WHERE id = ANY(%s)", (staged_ids,)
                )

        # Apply the facts-table UPDATE (tombstone/restore/supersede).
        if facts_ids:
            placeholders = ",".join(["%s"] * len(facts_ids))
            if mode == "hard_delete":
                # FORGET via TOMBSTONE — set archived_at + deleted_at (recoverable). Existing
                # `archived_at IS NULL` read filters hide it immediately; the distinct deleted_at
                # marks it forgotten (vs superseded) and is the eventual purge target. NOT a DELETE.
                cur.execute(
                    f"UPDATE facts SET archived_at = now(), deleted_at = now(), qdrant_synced = false "
                    f"WHERE id IN ({placeholders})",
                    facts_ids,
                )
            elif mode == "unforget":
                # UN-FORGET — clear the tombstone, restore to the live view.
                cur.execute(
                    f"UPDATE facts SET archived_at = NULL, deleted_at = NULL, qdrant_synced = false "
                    f"WHERE id IN ({placeholders})",
                    facts_ids,
                )
            else:
                cur.execute(
                    f"UPDATE facts SET superseded_at = now(), qdrant_synced = false WHERE id IN ({placeholders})",
                    facts_ids,
                )

        # Return (union ids, source_table). 'both' tells the caller to run the
        # collision-safe per-table filtered Qdrant delete for BOTH labels over the
        # union — each pass is conjunctive on (fact_id, source_table) so a colliding
        # id is only removed from the table whose payload matches.
        if facts_ids and staged_ids:
            return facts_ids + staged_ids, "both"
        if facts_ids:
            return facts_ids, "facts"
        if staged_ids:
            return staged_ids, "staged_facts"
        return [], "facts"
