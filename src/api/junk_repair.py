"""One-shot, idempotent repair of pre-#36 degree-state junk (issue #48 part c). NEVER auto-run.

Before #36, "<N> <unit> long" filed ``(sailboat, has_state, long)`` beside the measure, minting a
shared entity named after the degree adjective. The ingest fix stops new rows; existing seats keep
the old ones, and the node then bleeds across subjects ("Sailboat is long / Workbench is long").

The retirement rule is grounded in the SAME rules ingest now applies — no word list, no rel name:
  a live relational edge (facts / staged_facts) whose OBJECT entity is named only by a bare
  gradable adjective carrying a WordNet ATTRIBUTE pointer (``degree_adjective_dimensions``), on a
  SUBJECT that already holds a live ``entity_attributes`` row IN one of that adjective's
  dimensions. The measure row is the fact the sentence stated; the degree edge is its junk twin.

Retirement is a soft stamp (``facts.superseded_at`` / ``staged_facts.deleted_at``), never a DELETE,
so it is reversible and re-running it touches nothing (only live rows are candidates). The object
entity and its alias are left in place (another, legitimate edge may still reference them).

Usage (operator, per tenant):  ``retire_degree_state_junk(conn, schema, apply=False)`` returns the
candidates; pass ``apply=True`` to stamp them. The caller owns the transaction (commit/rollback).
"""
from __future__ import annotations

import re

_SAFE_SCHEMA_RE = re.compile(r"^faultline_[a-z0-9_]+$")


def _degree_dimensions(word: str) -> frozenset:
    from src.api.wordnet_ladder import degree_adjective_dimensions
    return degree_adjective_dimensions(word)


def retire_degree_state_junk(conn, schema: str, *, apply: bool = False) -> list[dict]:
    """Find (and with ``apply=True`` retire) degree-state junk edges in ONE tenant schema.

    Returns one dict per candidate: ``{table, id, subject_id, rel_type, degree, dimension}``.
    Raises ValueError on a schema name that is not a tenant schema (the name is interpolated into
    ``SET search_path``, so it is validated against the tenant-slug shape first)."""
    if not schema or not _SAFE_SCHEMA_RE.match(schema):
        raise ValueError(f"not a tenant schema: {schema!r}")
    out: list[dict] = []
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {schema}")
        # live subject → set of held attribute names (space-form, the degree lexicon's form)
        cur.execute("SELECT entity_id, attribute FROM entity_attributes "
                    "WHERE superseded_at IS NULL")
        held: dict[str, set] = {}
        for eid, attr in cur.fetchall():
            if eid and attr:
                held.setdefault(str(eid), set()).add(str(attr).strip().lower().replace("_", " "))
        if not held:
            return out
        # object entities named ONLY by bare single-word aliases (a degree node has no other name)
        cur.execute("SELECT entity_id, array_agg(alias) FROM entity_aliases GROUP BY entity_id")
        degree_obj: dict[str, tuple[str, frozenset]] = {}
        for eid, aliases in cur.fetchall():
            names = {str(a).strip().lower() for a in (aliases or []) if a}
            if len(names) != 1:
                continue
            (name,) = names
            if not name.isalpha():
                continue
            dims = _degree_dimensions(name)
            if dims:
                degree_obj[str(eid)] = (name, dims)
        if not degree_obj:
            return out
        for table, live, stamp in (("facts", "superseded_at IS NULL", "superseded_at"),
                                   ("staged_facts", "deleted_at IS NULL", "deleted_at")):
            cur.execute(f"SELECT id, subject_id, object_id, rel_type FROM {table} "
                        f"WHERE {live} AND object_id = ANY(%s)", (list(degree_obj),))
            for fid, sid, oid, rel in cur.fetchall():
                name, dims = degree_obj[str(oid)]
                hit = sorted(held.get(str(sid), set()) & dims)
                if not hit:
                    continue  # the subject holds no measure in this dimension → not a twin
                out.append({"table": table, "id": fid, "subject_id": sid, "rel_type": rel,
                            "degree": name, "dimension": hit[0]})
                if apply:
                    cur.execute(f"UPDATE {table} SET {stamp} = now() "
                                f"WHERE id = %s AND {live}", (fid,))
    return out


# ── #64: names and content a DOCUMENT import filed on the USER ────────────────────────────────────
# A document ingest (2026-08-21 on the owner's seat) filed "what" / "does" / "every" / "matt shumer" /
# "gauntlet loop" as aliases of the user (preference_source='rel_default'), plus document content as
# the user's own facts/attributes ((user, want, first-person shooter), appeal='over 10k tokens…').
# The ingest gate (main.py::_user_alias_write_refusal) stops new rows; this retires the old ones by
# the SAME grammar the gate applies — never by a word list, never by hand:
#   • an alias of the user entity that is DOCUMENT-SOURCED (its surface in a document chunk retained
#     just before the alias row, and in no chat turn) — owner ruling #86, chat aliases untouched (#93);
#   • a live user-subject row (facts / staged_facts / entity_attributes) is retired when its object
#     surface appears in a DOCUMENT chunk (episodic_log.source='document', retained before
#     extraction, so the row was minted within ``window_minutes`` after it; source_ref must agree
#     when both carry one) AND no chunk carrying it predicates it of the speaker
#     (linguistics.speaker_attributed) AND no non-document turn carries it at all.
# Facts/attributes are soft-stamped (superseded_at / deleted_at). entity_aliases has no retirement
# column any reader honours, so an alias is DELETED on apply — every candidate dict carries the
# full row so the caller can back it up first. Idempotent: only live rows are candidates, and a
# re-run finds nothing. NEVER auto-run; the caller owns the transaction. A rel_default name whose
# turn was never retained (pre-migration-127 history) is also a candidate — review the dry run
# before ``apply=True``.

_DOC_SOURCE = "document"


def _user_entity_id(cur) -> str | None:
    cur.execute("SELECT entity_id FROM entity_aliases WHERE alias = 'user' "
                "AND preference_source = 'provisioned' LIMIT 2")
    rows = cur.fetchall()
    return str(rows[0][0]) if len(rows) == 1 else None


def _turns_containing(cur, surface: str, *, document: bool) -> list[tuple]:
    op = "=" if document else "IS DISTINCT FROM"
    like = "%" + surface.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    cur.execute(f"SELECT raw_text, source_ref, created_at FROM episodic_log "
                f"WHERE source {op} %s AND raw_text ILIKE %s", (_DOC_SOURCE, like))
    return cur.fetchall()


def retire_user_document_junk(conn, schema: str, *, apply: bool = False,
                              user_entity_id: str | None = None,
                              window_minutes: int = 120) -> list[dict]:
    """Find (and with ``apply=True`` retire) #64 junk on the user entity of ONE tenant schema.

    Returns one dict per candidate: ``{table, id, …row fields…, reason}``. Raises ValueError on a
    non-tenant schema name, or when the user entity cannot be resolved unambiguously (pass
    ``user_entity_id`` explicitly then)."""
    if not schema or not _SAFE_SCHEMA_RE.match(schema):
        raise ValueError(f"not a tenant schema: {schema!r}")
    from src.extraction.linguistics import speaker_attributed
    out: list[dict] = []
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {schema}")
        uid = user_entity_id or _user_entity_id(cur)
        if not uid:
            raise ValueError("user entity not resolvable; pass user_entity_id")

        # (1) DOCUMENT-SOURCED aliases on the user entity (owner ruling #86: the user's identity is
        # set only from chat). An alias is document-sourced when its surface appears in a document
        # chunk retained (episodic source='document') within the window BEFORE the alias row was
        # written, and in NO chat turn. A chat-sourced alias is never touched (#93), whatever its
        # preference_source; the provisioned placeholders are never candidates.
        cur.execute("SELECT id, entity_id, alias, is_preferred, preference_source, created_at "
                    "FROM entity_aliases WHERE entity_id = %s "
                    "AND preference_source IS DISTINCT FROM 'provisioned'", (uid,))
        for aid, eid, alias, pref, src, created in cur.fetchall():
            if not alias or created is None:
                continue
            if _turns_containing(cur, alias, document=False):
                continue  # the user said it in chat → chat-sourced, keep
            docs = [c for c in _turns_containing(cur, alias, document=True)
                    if c[2] is not None and c[2] <= created
                    and (created - c[2]).total_seconds() <= window_minutes * 60]
            if not docs:
                continue
            out.append({"table": "entity_aliases", "id": aid, "entity_id": eid, "alias": alias,
                        "is_preferred": pref, "preference_source": src, "created_at": created,
                        "reason": "document-sourced alias on the user (identity is set only "
                                  "from chat, #86)"})
            if apply:
                cur.execute("DELETE FROM entity_aliases WHERE id = %s", (aid,))

        # (2) document content attributed to the user
        def _doc_sourced(surface: str, created, row_ref) -> bool:
            if not surface or created is None:
                return False
            chunks = [c for c in _turns_containing(cur, surface, document=True)
                      if c[2] is not None and c[2] <= created
                      and (created - c[2]).total_seconds() <= window_minutes * 60
                      and (not row_ref or not c[1] or row_ref == c[1])]
            if not chunks:
                return False
            if any(speaker_attributed(t, surface) for t, _r, _c in chunks):
                return False
            # the user also said it in conversation → not the document's alone
            return not _turns_containing(cur, surface, document=False)

        def _surfaces(entity_id) -> list[str]:
            cur.execute("SELECT alias FROM entity_aliases WHERE entity_id = %s", (entity_id,))
            return [str(a[0]) for a in cur.fetchall() if a and a[0]]

        for table, created_col, live, stamp in (
                ("facts", "created_at", "superseded_at IS NULL", "superseded_at"),
                ("staged_facts", "first_seen_at", "deleted_at IS NULL", "deleted_at")):
            cur.execute(f"SELECT id, object_id, rel_type, {created_col}, source_ref FROM {table} "
                        f"WHERE subject_id = %s AND {live}", (uid,))
            for fid, oid, rel, created, ref in cur.fetchall():
                surf = next((s for s in _surfaces(oid) if _doc_sourced(s, created, ref)), None)
                if surf is None:
                    continue
                out.append({"table": table, "id": fid, "subject_id": uid, "rel_type": rel,
                            "object": surf, "created_at": created,
                            "reason": "document content attributed to the user without a "
                                      "first-person frame"})
                if apply:
                    cur.execute(f"UPDATE {table} SET {stamp} = now() WHERE id = %s AND {live}",
                                (fid,))

        cur.execute("SELECT attribute, value_text, created_at FROM entity_attributes "
                    "WHERE entity_id = %s AND superseded_at IS NULL AND value_text IS NOT NULL",
                    (uid,))
        for attr, val, created in cur.fetchall():
            if not _doc_sourced(str(val), created, None):
                continue
            out.append({"table": "entity_attributes", "id": [uid, attr], "entity_id": uid,
                        "attribute": attr, "value_text": val, "created_at": created,
                        "reason": "document content attributed to the user without a "
                                  "first-person frame"})
            if apply:
                cur.execute("UPDATE entity_attributes SET superseded_at = now() "
                            "WHERE entity_id = %s AND attribute = %s AND superseded_at IS NULL",
                            (uid, attr))
    return out


# ── #67: census of engine-grown rel aliases whose surface is only ever a MODIFIER ──────────────────
# The aspect-synonym lane grew 'favorite' → want (source=engine) from "What is my favorite color?".
# The query-side gate (main.py::_aspect_word_growth_eligible) stops new ones; seats may already carry
# them. READ-ONLY: for every source='engine' alias, parse the tenant's retained turns that contain the
# surface (episodic_log) and count how it occurs. ``suspect`` = seen, and never as a head (every
# occurrence an amod/advmod dependent or a closed-class token). ``unverified`` = no retained turn
# carries it (nothing to judge by). Retiring a row is an owner decision — this only lists.

def census_modifier_grown_aliases(conn, schema: str) -> list[dict]:
    if not schema or not _SAFE_SCHEMA_RE.match(schema):
        raise ValueError(f"not a tenant schema: {schema!r}")
    from src.extraction.linguistics import modifier_only_evidence
    out: list[dict] = []
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {schema}")
        cur.execute("SELECT alias, canonical_rel_type, created_at FROM rel_type_aliases "
                    "WHERE source = 'engine' ORDER BY alias")
        for alias, canonical, created in cur.fetchall():
            surface = str(alias or "").replace("_", " ").strip()
            if not surface or " " in surface:
                continue  # a compound alias is a rel-name variant, not a lone modifier word
            like = "%" + surface.replace("%", "\\%").replace("_", "\\_") + "%"
            cur.execute("SELECT raw_text FROM episodic_log WHERE raw_text ILIKE %s LIMIT 50",
                        (like,))
            texts = [r[0] for r in cur.fetchall()]
            mod, head = modifier_only_evidence(texts, surface)
            verdict = ("suspect" if mod and not head
                       else "unverified" if not (mod or head) else "head_seen")
            if verdict == "head_seen":
                continue
            out.append({"schema": schema, "alias": alias, "canonical": canonical,
                        "created_at": created, "modifier_occurrences": mod,
                        "head_occurrences": head, "verdict": verdict})
    return out


def census_modifier_grown_aliases_all(conn) -> list[dict]:
    """``census_modifier_grown_aliases`` across every tenant schema (read-only)."""
    with conn.cursor() as cur:
        cur.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'faultline\\_%' "
                    "ORDER BY nspname")
        schemas = [r[0] for r in cur.fetchall() if _SAFE_SCHEMA_RE.match(r[0])]
    out: list[dict] = []
    for s in schemas:
        try:
            out.extend(census_modifier_grown_aliases(conn, s))
        except Exception as e:  # noqa: BLE001 — one unreadable seat never hides the others
            conn.rollback()
            out.append({"schema": s, "verdict": "error", "error": str(e)[:160]})
    return out


# ── #103: bare role words minted as the user's kin by a pre-fix QUESTION harvest ───────────────────
# Recall's possessive-presupposition harvest of a question ("Who is my dad?", "Where does my brother
# live?") kept the bare role word as the subject: /ingest minted a person named after the role
# (``(dad, parent_of, user)``, rendering "You are the child of Dad") or bound the instance_of TYPE
# node (``(user, sibling_of, brother)``). The ingest fix stops new rows; this retires the old ones.
# A live facts / staged_facts row is a candidate only when ALL hold — no word list, no rel name:
#   • its rel is a kinship / social-role rel: a ``description`` of the tenant's live ``kinship_noun``
#     / ``social_role`` cue rows (the map the binding chain itself reads), or that rel's inverse in
#     the tenant's ``rel_types`` — AND its rel_types row constrains BOTH arguments (non-empty
#     head_types / tail_types, neither holding the unconstrained 'ANY' sentinel). The kin class maps
#     uncle/aunt/cousin to the generic skos:related ``related_to`` ({ANY}→{ANY}); measured on
#     pre-prod that rel reaches non-kin rows ((user, related_to, policy)), so a role rel is one
#     whose metadata says it relates typed arguments, never the catch-all;
#   • exactly one endpoint is the seat's own entity (the uuid the schema name carries — never an
#     alias string);
#   • the other endpoint is (a) an entity EVERY alias of which is a cue word of those classes and
#     which nothing else references (no other live fact / staged row / attribute — a role-word
#     entity with facts of its own is the user's real, unnamed relative and is left alone), or (b) a
#     TYPE node — the object of some live instance_of — whose every alias is ALSO such a cue word
#     (measured on prod: a stray instance_of made a real named child "lucas" and a product
#     "portainer" type nodes; a name is never a role word, so neither qualifies);
#   • it is unattested: ``fact_provenance`` is never 'user_stated' (whatever its class or table).
# Rows are soft-stamped (facts.superseded_at / staged_facts.deleted_at). The (a) entity's aliases are
# DELETED (entity_aliases has no retirement column a reader honours — the #64 convention; every
# candidate dict carries the full alias row so the caller can back it up), and only while no live
# row or attribute references the entity after the stamp. Idempotent; NEVER auto-run; the caller
# owns the transaction. The candidate query is ONE template shared with the standalone SQL the
# operator runs (scripts/repairs/repair103_{census,apply}.sql, rendered by
# scripts/repairs/render_repair103.py): ``%1$I`` is the tenant schema and ``%2$L`` the seat uuid,
# expanded by PostgreSQL's own format() in both places, so the helper and the SQL cannot drift.

ROLE_WORD_KIN_CANDIDATES_SQL = """\
WITH cue AS (
    SELECT lower(btrim(c.cue)) AS word, lower(btrim(c.description)) AS rel
      FROM %1$I.linguistic_cues c
     WHERE c.category IN ('kinship_noun', 'social_role')
       AND c.is_active IS NOT FALSE AND c.archived_at IS NULL
), kin_rel AS (
    SELECT k.rel FROM (
        SELECT rel FROM cue WHERE rel IS NOT NULL AND rel <> ''
        UNION
        SELECT lower(rt.inverse_rel_type) FROM %1$I.rel_types rt JOIN cue ON rt.rel_type = cue.rel
         WHERE rt.inverse_rel_type IS NOT NULL
        UNION
        SELECT lower(rt.inverse_of) FROM %1$I.rel_types rt JOIN cue ON rt.rel_type = cue.rel
         WHERE rt.inverse_of IS NOT NULL
    ) k
    JOIN %1$I.rel_types rt ON rt.rel_type = k.rel
     WHERE cardinality(rt.head_types) > 0 AND cardinality(rt.tail_types) > 0
       AND NOT EXISTS (SELECT 1 FROM unnest(rt.head_types || rt.tail_types) AS t(v)
                        WHERE lower(v) = 'any')
), live AS (
    SELECT 'facts'::text AS tbl, f.id::bigint AS row_id, f.subject_id, f.rel_type, f.object_id,
           f.fact_class, f.fact_provenance
      FROM %1$I.facts f WHERE f.superseded_at IS NULL AND f.deleted_at IS NULL
    UNION ALL
    SELECT 'staged_facts'::text, s.id::bigint, s.subject_id, s.rel_type, s.object_id,
           s.fact_class, s.fact_provenance
      FROM %1$I.staged_facts s WHERE s.deleted_at IS NULL
), shape AS (
    SELECT l.*, CASE WHEN l.subject_id = %2$L THEN l.object_id ELSE l.subject_id END AS other_id
      FROM live l
     WHERE l.rel_type IN (SELECT rel FROM kin_rel)
       AND (l.subject_id = %2$L) <> (l.object_id = %2$L)
       AND l.fact_provenance IS DISTINCT FROM 'user_stated'
), type_node AS (
    SELECT DISTINCT l.object_id AS id FROM live l WHERE l.rel_type = 'instance_of'
), role_ent AS (
    SELECT a.entity_id AS id FROM %1$I.entity_aliases a
     GROUP BY a.entity_id
    HAVING bool_and(lower(btrim(a.alias)) IN (SELECT word FROM cue))
), isolated AS (
    SELECT r.id FROM role_ent r
     WHERE r.id <> %2$L
       AND NOT EXISTS (SELECT 1 FROM live l
                        WHERE (l.subject_id = r.id OR l.object_id = r.id)
                          AND NOT EXISTS (SELECT 1 FROM shape s
                                           WHERE s.tbl = l.tbl AND s.row_id = l.row_id))
       AND NOT EXISTS (SELECT 1 FROM %1$I.entity_attributes ea
                        WHERE ea.entity_id = r.id AND ea.superseded_at IS NULL)
), cand AS (
    SELECT s.*, CASE WHEN s.other_id IN (SELECT id FROM isolated) THEN 'role_word_entity'
                     ELSE 'type_node' END AS endpoint
      FROM shape s
     WHERE s.other_id IN (SELECT id FROM isolated)
        OR (s.other_id IN (SELECT id FROM type_node) AND s.other_id IN (SELECT id FROM role_ent))
)
SELECT 'row'::text AS kind, c.tbl, c.row_id, c.subject_id, c.rel_type, c.object_id,
       c.fact_class, c.fact_provenance, c.endpoint,
       (SELECT string_agg(a.alias, '|' ORDER BY a.alias) FROM %1$I.entity_aliases a
         WHERE a.entity_id = c.subject_id) AS subject_aliases,
       (SELECT string_agg(a.alias, '|' ORDER BY a.alias) FROM %1$I.entity_aliases a
         WHERE a.entity_id = c.object_id) AS object_aliases,
       NULL::text AS entity_id, NULL::text AS alias, NULL::boolean AS is_preferred,
       NULL::text AS preference_source, NULL::timestamptz AS created_at
  FROM cand c
UNION ALL
SELECT 'alias'::text, 'entity_aliases'::text, a.id::bigint, NULL, NULL, NULL, NULL, NULL,
       'role_word_entity', NULL, NULL, a.entity_id, a.alias, a.is_preferred,
       a.preference_source, a.created_at
  FROM %1$I.entity_aliases a
 WHERE a.entity_id IN (SELECT other_id FROM cand WHERE endpoint = 'role_word_entity')
 ORDER BY 1 DESC, 2, 3
"""

# The alias DELETE re-checks, after the row stamps, that nothing live references the entity.
# ``%1$I`` = tenant schema, ``%2$s`` = the SQL expression naming the entity id (``a.entity_id``).
ROLE_WORD_ALIAS_UNREFERENCED_SQL = """\
NOT EXISTS (SELECT 1 FROM %1$I.facts f WHERE (f.subject_id = %2$s OR f.object_id = %2$s)
               AND f.superseded_at IS NULL AND f.deleted_at IS NULL)
AND NOT EXISTS (SELECT 1 FROM %1$I.staged_facts s WHERE (s.subject_id = %2$s OR s.object_id = %2$s)
               AND s.deleted_at IS NULL)
AND NOT EXISTS (SELECT 1 FROM %1$I.entity_attributes ea WHERE ea.entity_id = %2$s
               AND ea.superseded_at IS NULL)"""

_ROLE_KIN_COLS = ("kind", "table", "id", "subject_id", "rel_type", "object_id", "fact_class",
                  "fact_provenance", "endpoint", "subject_aliases", "object_aliases", "entity_id",
                  "alias", "is_preferred", "preference_source", "created_at")


def seat_uuid_from_schema(schema: str) -> str:
    """The seat uuid a tenant schema name carries (inverse of derive_user_slug_from_uuid)."""
    return schema[len("faultline_"):].replace("_", "-")


def _formatted(cur, template: str, *args: str) -> str:
    """Expand ``template`` with PostgreSQL's format() — the same expansion the SQL files use."""
    placeholders = ", ".join(["%s::text"] * len(args))
    cur.execute(f"SELECT format(%s, {placeholders})", (template, *args))
    return cur.fetchone()[0]


def retire_role_word_kin_junk(conn, schema: str, *, apply: bool = False) -> list[dict]:
    """Find (and with ``apply=True`` retire) #103 role-word kin junk in ONE tenant schema.

    Returns one dict per change: ``kind='row'`` (``table`` facts|staged_facts, ``id``, subject /
    object ids and aliases, ``rel_type``, ``fact_class``, ``fact_provenance``, ``endpoint``
    role_word_entity|type_node) or ``kind='alias'`` (the full entity_aliases row to be deleted).
    Raises ValueError on a non-tenant schema name or a schema whose seat entity is absent."""
    if not schema or not _SAFE_SCHEMA_RE.match(schema):
        raise ValueError(f"not a tenant schema: {schema!r}")
    seat = seat_uuid_from_schema(schema)
    with conn.cursor() as cur:
        cur.execute(_formatted(cur, "SELECT 1 FROM %1$I.entities WHERE id = %2$L", schema, seat))
        if not cur.fetchone():
            raise ValueError(f"seat entity {seat!r} absent in {schema!r} (legacy slug?)")
        cur.execute(_formatted(cur, ROLE_WORD_KIN_CANDIDATES_SQL, schema, seat))
        out = [dict(zip(_ROLE_KIN_COLS, r)) for r in cur.fetchall()]
        if not apply:
            return out
        for c in out:
            if c["kind"] != "row":
                continue
            stamp, live = (("superseded_at", "superseded_at IS NULL AND deleted_at IS NULL")
                           if c["table"] == "facts" else ("deleted_at", "deleted_at IS NULL"))
            cur.execute(f'UPDATE "{schema}".{c["table"]} SET {stamp} = now() '
                        f"WHERE id = %s AND {live}", (c["id"],))
        guard = _formatted(cur, ROLE_WORD_ALIAS_UNREFERENCED_SQL, schema, "a.entity_id")
        for c in out:
            if c["kind"] == "alias":
                cur.execute(f'DELETE FROM "{schema}".entity_aliases a WHERE a.id = %s AND {guard}',
                            (c["id"],))
    return out


def census_role_word_kin_junk_all(conn, *, sample: int = 5) -> list[dict]:
    """READ-ONLY #103 census over every tenant schema: per schema, counts plus a sample.

    Schemas with nothing to retire are omitted; a schema whose seat entity is absent (legacy slug)
    or that errors is reported ``status='skipped'``. Writes nothing; ends on a rollback."""
    with conn.cursor() as cur:
        cur.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'faultline\\_%' "
                    "ORDER BY nspname")
        schemas = [r[0] for r in cur.fetchall() if _SAFE_SCHEMA_RE.match(r[0])]
    out: list[dict] = []
    for s in schemas:
        try:
            cands = retire_role_word_kin_junk(conn, s, apply=False)
        except Exception as e:  # noqa: BLE001 — one unreadable seat never hides the others
            conn.rollback()
            out.append({"schema": s, "status": "skipped", "error": str(e)[:160]})
            continue
        if not cands:
            continue
        rows_ = [c for c in cands if c["kind"] == "row"]
        out.append({
            "schema": s, "status": "ok",
            "facts": sum(1 for c in rows_ if c["table"] == "facts"),
            "staged_facts": sum(1 for c in rows_ if c["table"] == "staged_facts"),
            "role_word_entity": sum(1 for c in rows_ if c["endpoint"] == "role_word_entity"),
            "type_node": sum(1 for c in rows_ if c["endpoint"] == "type_node"),
            "aliases": sum(1 for c in cands if c["kind"] == "alias"),
            "sample": rows_[:sample],
        })
    conn.rollback()  # read-only: never leave a transaction open
    return out
