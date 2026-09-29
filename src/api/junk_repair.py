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
