import os

import psycopg2
import structlog

log = structlog.get_logger()

# BUG-R4-L (2026-11-14 04:28:51 wound): bind old_value on retract's object-side
# fallback probe and re-key the surgical tombstone guard. ON (default) = the fix;
# OFF = byte-for-byte legacy behavior (unbound fallback sweep + `not old_value`
# guard) for rollback parity. Same env-bool convention as the api flags
# (e.g. FORGET_ENTITY_SWEEP in src/api/main.py).
RETRACT_OBJECT_SIDE_VALUE_BIND: bool = os.environ.get(
    "RETRACT_OBJECT_SIDE_VALUE_BIND", "true"
).strip().lower() not in ("0", "false", "no")

# Canonical fact_provenance values (see main.py provenance routing, ~line 10282).
# Anything else is a caller bug — coerce to the weakest tier so a malformed
# value can never masquerade as (or overwrite) user_stated.
_CANONICAL_FACT_PROVENANCE = ("user_stated", "llm_inferred", "llm_learned")


def _rel_is_mutable_residence_rel(rel_type) -> bool:
    """TRUE when ``rel_type`` is a MUTABLE RESIDENCE predicate (F8 fresh-row lane).

    ONE authority: ``linguistics._residence_predicate_identities()`` — the SAME set the
    ingest decider's F6 branch (``main._rel_is_mutable_current_residence``) reads, normalized
    identity compare so lives_in/lives_at/live_in fold together. Any failure → False
    (fail-safe: the non-residence revive route, the shipped behavior)."""
    try:
        from src.extraction.linguistics import (  # deferred: avoid import cycle / hard dep
            _norm_rel_identity as _nri,
            _residence_predicate_identities as _rpi,
        )
        _rt = _nri(rel_type or "")
        return bool(_rt) and _rt in _rpi()
    except Exception:  # noqa: BLE001 — fail-safe: shipped revive contract
        return False


def _facts_natural_key_state(cur, sub, obj, rel):
    """'live' | 'retired' | None for the natural key's most relevant row (F8 routing probe).

    A live row always wins (post-migration-309 a triple may carry BOTH a retired history row
    and a live row; the live one is what an upsert must bump). Fail-safe: None → the plain
    predicate-arbiter insert, which is also correct for a no-row key."""
    try:
        cur.execute(
            "SELECT superseded_at FROM facts "
            "WHERE subject_id = %s AND object_id = %s AND rel_type = %s "
            "ORDER BY (superseded_at IS NULL) DESC, id DESC LIMIT 1",
            (sub, obj, rel))
        _row = cur.fetchone()
    except Exception as e:  # noqa: BLE001 — fail-safe: plain insert path
        log.warning("fact_store.natural_key_probe_failed", rel_type=rel, error=str(e)[:160])
        return None
    if _row is None:
        return None
    return "live" if _row[0] is None else "retired"


def _facts_natural_key_retired_id(cur, sub, obj, rel):
    """The newest RETIRED row's (id, polarity) for the natural key (revive route only —
    called when the probe returned 'retired', so no live row exists). Fail-safe:
    (None, None) → plain insert.

    R7 (critic R5-2): polarity rides along — 'negated' marks a DENIAL-RETIRED row (the
    denial-exact gate stamps it at retire time), and such a row must NEVER be un-retired
    in place by the F8 revive CASE. The caller plants a fresh row instead."""
    try:
        cur.execute(
            "SELECT id, polarity FROM facts "
            "WHERE subject_id = %s AND object_id = %s AND rel_type = %s "
            "  AND superseded_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (sub, obj, rel))
        _row = cur.fetchone()
        return (_row[0], _row[1]) if _row else (None, None)
    except Exception as e:  # noqa: BLE001 — fail-safe: plain insert path
        log.warning("fact_store.natural_key_retired_id_failed", rel_type=rel, error=str(e)[:160])
        return None


# The shared DO UPDATE clause — byte-identical to the shipped upsert (the deferred-replay
# guard CASEs stay as race defense for the live-row upsert path; the revive route is
# unreachable on replay because that route is skipped entirely).
_FACTS_UPSERT_DO_UPDATE = (
    " DO UPDATE SET"
    "   fact_class = CASE"
    "       WHEN 'A' IN (facts.fact_class, EXCLUDED.fact_class) THEN 'A'"
    "       WHEN 'B' IN (facts.fact_class, EXCLUDED.fact_class) THEN 'B'"
    "       ELSE 'C'"
    "   END,"
    # R8 (critic RG-C): an AFFIRMATION may never increase a NEGATED row's confirm
    # count. Measured wound: row 469 (negated) went cnt 2->3 when the affirmative
    # re-statement's natural-key BUMP path found the negated row live-by-index —
    # an affirmation strengthened a negation. Doctrine pick: (b) FLIP IN PLACE —
    # when the conflicted twin carries polarity='negated', the DO UPDATE flips it
    # to 'affirmed', RESTARTS the count at 1 (never +1 on the negated tally), and
    # refreshes provenance from the fresh affirmation (EXCLUDED). Re-denials are
    # unaffected: a negated edge re-lands here, flips transiently, and the API's
    # post-commit polarity stamp re-stamps 'negated' in the same request. The
    # retired-twin routes are guarded separately (the R7 revive refusal plants a
    # fresh row for negated twins — this CASE is unreachable there).
    "   confirmed_count = CASE WHEN facts.superseded_at IS NOT NULL AND %s THEN facts.confirmed_count"
    "       WHEN facts.polarity = 'negated' THEN 1"
    "       ELSE facts.confirmed_count + 1 END,"
    "   polarity        = CASE WHEN facts.polarity = 'negated' THEN 'affirmed' ELSE facts.polarity END,"
    "   provenance      = CASE WHEN facts.polarity = 'negated' THEN EXCLUDED.provenance ELSE facts.provenance END,"
    "   last_seen_at    = now(),"
    "   updated_at      = now(),"
    "   superseded_at   = CASE WHEN facts.superseded_at IS NOT NULL AND %s THEN facts.superseded_at ELSE NULL END,"
    "   unified_confidence = EXCLUDED.unified_confidence,"
    "   rel_type_definition = EXCLUDED.rel_type_definition,"
    "   storage_type = COALESCE(EXCLUDED.storage_type, facts.storage_type),"
    "   taxonomies = COALESCE(EXCLUDED.taxonomies, facts.taxonomies),"
    "   valid_from = COALESCE(EXCLUDED.valid_from, facts.valid_from),"
    "   valid_until = COALESCE(EXCLUDED.valid_until, facts.valid_until),"
    "   source_ref = COALESCE(EXCLUDED.source_ref, facts.source_ref),"
    "   fact_provenance = CASE"
    "       WHEN EXCLUDED.fact_provenance = 'user_stated' THEN 'user_stated'"
    "       ELSE facts.fact_provenance"
    "   END,"
    "   deleted_at   = CASE"
    "       WHEN facts.deleted_at IS NOT NULL AND (NOT %s)"
    "            AND EXCLUDED.fact_provenance = 'user_stated' THEN NULL"
    "       ELSE facts.deleted_at END,"
    "   archived_at  = CASE"
    "       WHEN facts.deleted_at IS NOT NULL AND (NOT %s)"
    "            AND EXCLUDED.fact_provenance = 'user_stated' THEN NULL"
    "       ELSE facts.archived_at END,"
    "   qdrant_synced = CASE"
    "       WHEN facts.deleted_at IS NOT NULL AND (NOT %s)"
    "            AND EXCLUDED.fact_provenance = 'user_stated' THEN false"
    "       ELSE facts.qdrant_synced END"
)

# F8 LIVE route: the arbiter is migration 309's PARTIAL unique index (live rows only) — a
# retired twin does NOT conflict, so a genuine residence re-statement files a FRESH LIVE row
# (copy-or-new-row) and the archived row keeps its validity span untouched.
_FACTS_INSERT_LIVE_SQL = (
    "INSERT INTO facts"
    " (subject_id, object_id, rel_type, provenance, confidence, unified_confidence, is_preferred_label, rel_type_definition, storage_type, is_hierarchy_rel, taxonomies, valid_from, valid_until, fact_class, fact_provenance, source_ref)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    " ON CONFLICT (subject_id, object_id, rel_type) WHERE superseded_at IS NULL"
    + _FACTS_UPSERT_DO_UPDATE
)

# SHIPPED revive route (non-residence rels only): insert with the retired row's OWN id so the
# conflict fires on facts_pkey and the SAME DO UPDATE CASEs run against that row — byte-
# equivalent to the old full-constraint upsert (superseded_at=NULL on a genuine non-replay
# re-statement; the never-resurrect correction contract outside the residence lane unchanged).
_FACTS_INSERT_REVIVE_SQL = (
    "INSERT INTO facts"
    " (id, subject_id, object_id, rel_type, provenance, confidence, unified_confidence, is_preferred_label, rel_type_definition, storage_type, is_hierarchy_rel, taxonomies, valid_from, valid_until, fact_class, fact_provenance, source_ref)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    " ON CONFLICT (id)"
    + _FACTS_UPSERT_DO_UPDATE
)


class FactStoreManager:
    def __init__(self, db_conn):
        self.db_conn = db_conn

    def commit(self, connections: list[tuple], confidence: float = 1.0, unified_confidence: float = None, fact_class: str = "A", fact_provenance: str = "llm_inferred", source_ref: str | None = None, replay: bool = False) -> int:
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

                    # ── F8 ROUTE (fog-family R2; migration 309 partial live-unique index) ──
                    # Migration 309 replaced the FULL unique constraint on (subject_id,
                    # object_id, rel_type) with a PARTIAL unique index on LIVE rows
                    # (WHERE superseded_at IS NULL). Routing, probed per row:
                    #   replay  + any existing row → SKIP: the deferred-replay freeze contract
                    #                 (retirement and count preserved; a replay of a never-written
                    #                 row is a first write and inserts normally).
                    #   genuine + retired + RESIDENCE rel → FRESH LIVE ROW (plain insert; the
                    #                 partial index does not fire on the retired twin — the
                    #                 archived row is never resurrected, its validity span stays
                    #                 honest, and the render gets a current row again).
                    #   genuine + retired + other rel → in-place revive via ON CONFLICT (id):
                    #                 the shipped non-replay CASEs evaluate byte-identically
                    #                 (facts.* IS that row), so the never-resurrect correction
                    #                 contract outside the residence lane is unchanged.
                    #   live row / no row → the normal predicate-arbiter upsert.
                    _f8_state = _facts_natural_key_state(cur, sub, obj, rel)
                    if _f8_state and replay:
                        log.info("fact_store.replay_frozen_existing_row",
                                 rel_type=rel, row_state=_f8_state,
                                 note="replay landing on an existing row — write skipped; "
                                      "retirement and count preserved (deferred-replay guard)")
                        continue
                    if _f8_state == "retired":
                        if _rel_is_mutable_residence_rel(rel):
                            cur.execute(
                                _FACTS_INSERT_LIVE_SQL,
                                (sub, obj, rel, prov, confidence, unified_confidence, is_preferred, definition, storage_type, is_hierarchy_rel, taxonomies, statement_date, valid_until, fact_class, fact_provenance, source_ref, bool(replay), bool(replay), bool(replay), bool(replay), bool(replay)),
                            )
                            log.info("fact_store.residence_fresh_live_row",
                                     rel_type=rel,
                                     note="genuine re-statement landed on a retired residence row "
                                          "— a NEW live row was filed; the archived row keeps its "
                                          "history span (F8 copy-or-new-row, never resurrect)")
                        else:
                            _f8_old_id, _f8_old_polarity = \
                                _facts_natural_key_retired_id(cur, sub, obj, rel)
                            if _f8_old_id is not None and _f8_old_polarity == "negated":
                                # R7 (critic R5-2): DENIAL-RETIRED REVIVE REFUSAL. The
                                # newest retired row for this key was retired by a DENIAL
                                # (the denial-exact gate stamps polarity='negated' at
                                # retire time — the negative provenance). The F8 revive
                                # CASE is for superseded-by-CORRECTION rows only: a
                                # genuine re-statement — even an affirmative one — plants
                                # a FRESH live row with fresh provenance and the negated
                                # row stays dead. Measured wound: the tautology
                                # 'My wife is Wife.' alias-resolved onto the role-mirror
                                # entity, hit this route, and un-retired row 469 IN PLACE
                                # (confirmed_count 1->2, no marker) — it then rendered
                                # contradictory (negated in the family walk, AFFIRMATIVE
                                # in the possessed-role render).
                                log.info("fact_store.denial_retired_revive_refused",
                                         rel_type=rel,
                                         note="newest retired row carries polarity='negated' "
                                              "(denial-retired) — revive refused; a fresh "
                                              "row is filed with fresh provenance (R5-2)")
                                cur.execute(
                                    _FACTS_INSERT_LIVE_SQL,
                                    (sub, obj, rel, prov, confidence, unified_confidence, is_preferred, definition, storage_type, is_hierarchy_rel, taxonomies, statement_date, valid_until, fact_class, fact_provenance, source_ref, bool(replay), bool(replay), bool(replay), bool(replay), bool(replay)),
                                )
                            elif _f8_old_id is not None:
                                cur.execute(
                                    _FACTS_INSERT_REVIVE_SQL,
                                    (_f8_old_id, sub, obj, rel, prov, confidence, unified_confidence, is_preferred, definition, storage_type, is_hierarchy_rel, taxonomies, statement_date, valid_until, fact_class, fact_provenance, source_ref, bool(replay), bool(replay), bool(replay), bool(replay), bool(replay)),
                                )
                            else:
                                cur.execute(
                                    _FACTS_INSERT_LIVE_SQL,
                                    (sub, obj, rel, prov, confidence, unified_confidence, is_preferred, definition, storage_type, is_hierarchy_rel, taxonomies, statement_date, valid_until, fact_class, fact_provenance, source_ref, bool(replay), bool(replay), bool(replay), bool(replay), bool(replay)),
                                )
                    else:
                        cur.execute(
                            _FACTS_INSERT_LIVE_SQL,
                            (sub, obj, rel, prov, confidence, unified_confidence, is_preferred, definition, storage_type, is_hierarchy_rel, taxonomies, statement_date, valid_until, fact_class, fact_provenance, source_ref, bool(replay), bool(replay), bool(replay), bool(replay), bool(replay)),
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

        Searches both subject-side and object-side: if "forget ada" is issued,
        ada's UUID may be the object (user → spouse → ada). Searches subject
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
            cur.execute(f"SELECT id FROM facts WHERE {where}", params)  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — where clause built from fixed column names; all values passed as %s params
            ids = [r[0] for r in cur.fetchall()]

        # Fallback: object-side match (entity being retracted is the object)
        # BUG-R4-L: this fallback used to drop old_value entirely, so a truthy-but-
        # unresolvable sentence old_value swept EVERY live (X, rel, subject) row.
        # With RETRACT_OBJECT_SIDE_VALUE_BIND ON we bind it, mirroring the
        # subject-side probe's semantics on the FLIPPED arm: the subject-side probe
        # binds `object_id = old_value`; on this arm the retracted VALUE occupies
        # the SUBJECT slot of the flipped row, so the binding is
        # `subject_id = old_value` (binding object_id again would be a contradiction
        # with the arm's base `object_id = subject`). OFF = the legacy probe,
        # byte-for-byte.
        _object_side_fallback = False
        if not ids and subject:
            conditions = ["object_id = %s", _live_facts_filter]
            params = [subject]
            if mode != "unforget":
                conditions.append("superseded_at IS NULL")
            if rel_type:
                conditions.append("rel_type = %s")
                params.append(rel_type.lower())
            if RETRACT_OBJECT_SIDE_VALUE_BIND and old_value:
                conditions.append("subject_id = %s")
                params.append(old_value)
            where = " AND ".join(conditions)
            cur.execute(f"SELECT id FROM facts WHERE {where}", params)  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — where clause built from fixed column names; all values passed as %s params
            ids = [r[0] for r in cur.fetchall()]
            _object_side_fallback = True

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
            cur.execute(f"SELECT id FROM staged_facts WHERE {where}", params)  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — where clause built from fixed column names; all values passed as %s params
            staged_ids = [r[0] for r in cur.fetchall()]

        # ── SURGICAL TOMBSTONE GUARD (fail-closed) ────────────────────────────────────
        # WOUND (prod seat 10d7d879, 2026-08-19 18:41:13): a surgical_scalar retraction
        # ("I don't have a laptop") resolved its subject to the SPEAKER (user → user_uuid)
        # with no object value bound. The object-side fallback above then matched
        # object_id = user_uuid AND rel_type = child_of — EVERY live kin edge in the seat —
        # and hard_delete stamped archived_at = deleted_at = now() on all of them in ONE
        # UPDATE (the shared-timestamp mass-tombstone signature). A surgical retraction may
        # only tombstone rows it actually TARGETS (the named value/entity/rel); an
        # UNBOUND-object tombstone that resolves to MORE THAN ONE row is by definition a
        # rel-class sweep, not a target. Fail toward retracting LESS: a missed retraction
        # is recoverable by re-asking; a mass tombstone is silent data loss.
        #
        # SCOPE: mode == "hard_delete" ONLY (the forget/tombstone trigger — /forget, and
        # /retract when a rel's correction_behavior metadata says hard_delete). The
        # legitimate broad-retract path ("forget everything about X") routes through /retract
        # in "supersede" mode and is untouched. A FULLY-BOUND target (old_value present) or
        # a match resolving to exactly ONE row is surgical and proceeds unchanged.
        # BUG-R4-L DEFENSE IN DEPTH: `not old_value` alone was DISARMED by a truthy-but-
        # unresolvable sentence string (the 04:28:51 mass tombstone). With
        # RETRACT_OBJECT_SIDE_VALUE_BIND ON the guard ALSO refuses when the object-side
        # FALLBACK itself captured MORE THAN ONE row — a single value can never
        # legitimately sweep a row class through the fallback arm (cap at 1; explicit
        # old_value=None is the multi-row intent). A fallback that captured nothing
        # (the bound probe narrowing to 0 rows) does NOT arm the guard: a staged-side
        # multi-row match with old_value bound is the fully-named dual-write case.
        # OFF = the legacy `not old_value` key.
        _guard_trips = (not old_value) or (
            RETRACT_OBJECT_SIDE_VALUE_BIND and _object_side_fallback and len(facts_ids) > 1
        )
        if mode == "hard_delete" and _guard_trips and (len(facts_ids) + len(staged_ids)) > 1:
            log.error(
                "fact_store.surgical_tombstone_guard_refused",
                mode=mode,
                subject=str(subject)[:16] if subject else None,
                rel_type=rel_type,
                facts_matches=len(facts_ids),
                staged_matches=len(staged_ids),
                note="unbound-object tombstone resolved to a multi-row class sweep — "
                     "retracted NOTHING (fail-closed). Ask the user to name the specific "
                     "fact (subject + what + which value) and re-issue.",
            )
            return [], "facts"

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
                    f"WHERE id IN ({placeholders})",  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — placeholders are %s positionals, data in facts_ids param tuple
                    facts_ids,
                )
            elif mode == "unforget":
                # UN-FORGET — clear the tombstone, restore to the live view.
                cur.execute(
                    f"UPDATE facts SET archived_at = NULL, deleted_at = NULL, qdrant_synced = false "
                    f"WHERE id IN ({placeholders})",  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — placeholders are %s positionals, data in facts_ids param tuple
                    facts_ids,
                )
            else:
                cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema from UUID-derived source with validation
                    f"UPDATE facts SET superseded_at = now(), qdrant_synced = false WHERE id IN ({placeholders})",  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — placeholders are %s positionals, data in facts_ids param tuple
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
