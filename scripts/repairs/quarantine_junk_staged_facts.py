#!/usr/bin/env python3
"""BUG-05 repair (adversarial bench 2026-10-24): quarantine junk-class staged facts.

PROBLEM (measured read-only on prod, owner seat, 2026-10-25):
    19,644 live Class-B staged rows are extraction debris served in the recall held
    band. Deterministic junk classes observed:
      * temperature fragments as entities      — "° f is a member of temperature"
      * the "is not Document" class debris     — "First Soap is not Document"
      * fragment entities with no real name    — "Temples is located in Top"
      * empty surfaces                          — rows whose entity resolves to ''

WHAT THIS DOES:
    Identifies the junk CLASS deterministically (pattern over entity_aliases surfaces,
    never an LLM, never a word zoo) and SOFT-DELETES the matching staged rows via
    ``deleted_at`` — the Phase-4 tombstone every recall lane already honours
    (``fetch_facts_from_anchor`` staged lanes filter ``deleted_at IS NULL``). Rows are
    preserved for forensics: NO hard DELETE, ever.

JUNK CLASSES (the selection is deliberately conservative — each class is a shape that
has NO legitimate reading, so a false positive needs a real-entity counterexample):

    1. temperature_surface — an entity whose alias is ONLY a temperature token
       (``° f``, ``75°c``, ``230 °C``): regex ``^\\s*\\d*[.,]?\\d*\\s*°\\s*[cfk]?\\s*$``.
       A real title containing a temperature ("Heating at 75°C suppressed…") has other
       words and does not match.
    2. empty_surface — btrim(alias) = '': the bench sampled 6+ EMPTY subjects in 25.
    3. fragment_only_entity — EVERY alias of the entity is <3 chars after trim
       ("ie", "kg", "6", "r", "="). An entity that ALSO carries a >=3-char surface is
       KNOWN (initials "cj" as a secondary alias survive on their longer primary).
    4. document_class_debris — the row files something UNDER the ``document`` type node
       other than the legit stub shape. The stub lane
       (server.py::_mint_document_stub_edges) files a REAL title as
       ``(title, instance_of, document)`` affirmed; debris is anything else pointing
       there (``subclass_of document``, negated instance_of — the "X is not Document"
       render). THE HARD LINE backstop: a named document is instance_of its type,
       never subclass_of it.

USAGE (the orchestrator runs this; dry-run is the DEFAULT — no flag, no writes):
    python3 scripts/repairs/quarantine_junk_staged_facts.py --user-id <uuid>          # dry-run
    python3 scripts/repairs/quarantine_junk_staged_facts.py --user-id <uuid> --apply  # write

Exit codes: 0 = success (dry-run or applied), 2 = no junk found, 3 = DB error.
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg2

# ── deterministic pattern constants (shared with the pin tests) ──────────────────

#: Class 1 — an alias that is ONLY a temperature: optional number, the degree sign,
#: optional unit letter. Case-insensitive, whitespace-tolerant.
TEMPERATURE_FRAGMENT_SQL = r"^\s*\d*[.,]?\d*\s*°\s*[cfk]?\s*$"

#: Class 3 — the longest surviving alias length (trimmed) below which an entity has
#: no real name anywhere and is a fragment.
FRAGMENT_MAX_ALIAS_CHARS = 3

#: Class 4 — the seeded type-node surface the stub lane files real titles under.
DOCUMENT_TYPE_SURFACE = "document"

#: The ONE rel/polarity shape the legit stub lane writes against the document node.
LEGIT_STUB_REL = "instance_of"
LEGIT_STUB_POLARITY = "affirmed"

_JUNK_ENTITIES_SQL = """
WITH junk_ents AS (
    -- 1. temperature fragments + 2. empty surfaces: alias-level shapes
    SELECT DISTINCT a.entity_id
      FROM entity_aliases a
     WHERE a.alias ~* %(_temp)s
        OR btrim(a.alias) = ''
    UNION
    -- 3. fragment-only entities: NO alias of the entity reaches 3 trimmed chars —
    --    an entity that also carries a real surface (a "known alias") survives.
    SELECT e.entity_id
      FROM (SELECT entity_id, max(length(btrim(alias))) AS longest
              FROM entity_aliases GROUP BY entity_id) e
     WHERE e.longest < %(frag_len)s
)
SELECT s.id, s.subject_id, s.object_id, s.rel_type, s.polarity, s.fact_class
  FROM staged_facts s
 WHERE s.deleted_at IS NULL
   AND s.promoted_at IS NULL
   AND (
        s.subject_id IN (SELECT entity_id FROM junk_ents)
     OR s.object_id IN (SELECT entity_id FROM junk_ents)
     OR (
        -- 4. document-class debris: pointing at the document type node in any shape
        --    OTHER than the legit stub (instance_of + affirmed). Protects the stub
        --    lane ("do I have a document about X") while removing the subclass/negated
        --    debris ("First Soap is not Document").
        s.object_id IN (SELECT entity_id FROM entity_aliases
                         WHERE alias = %(doc_surface)s)
        AND NOT (s.rel_type = %(legit_rel)s AND s.polarity = %(legit_pol)s)
       )
   )
"""

_APPLY_SQL = """
UPDATE staged_facts
   SET deleted_at = now()
 WHERE id = ANY(%s)
   AND deleted_at IS NULL
"""


def junk_staged_fact_ids(cur) -> list[tuple]:
    """SELECT the junk-class staged rows on the CALLER-BOUND tenant cursor.

    Returns the raw id-bearing rows; the caller renders/updates. Pure read.
    """
    cur.execute(
        _JUNK_ENTITIES_SQL,
        {
            "_temp": TEMPERATURE_FRAGMENT_SQL,
            "frag_len": FRAGMENT_MAX_ALIAS_CHARS,
            "doc_surface": DOCUMENT_TYPE_SURFACE,
            "legit_rel": LEGIT_STUB_REL,
            "legit_pol": LEGIT_STUB_POLARITY,
        },
    )
    return cur.fetchall()


def _resolve_surfaces(cur, ids: list[int], limit: int = 8) -> dict[int, tuple[str, str]]:
    """Best-effort subject/object surface names for the report (forensics aid)."""
    out: dict[int, tuple[str, str]] = {}
    if not ids:
        return out
    cur.execute(
        """
        SELECT s.id,
               (SELECT alias FROM entity_aliases a WHERE a.entity_id = s.subject_id
                 ORDER BY is_preferred DESC, length(alias) DESC LIMIT 1),
               (SELECT alias FROM entity_aliases a WHERE a.entity_id = s.object_id
                 ORDER BY is_preferred DESC, length(alias) DESC LIMIT 1)
          FROM staged_facts s WHERE s.id = ANY(%s)
        """,
        (ids[:limit],),
    )
    for row in cur.fetchall():
        out[row[0]] = (row[1] or "?", row[2] or "?")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--user-id", required=True,
                    help="seat UUID whose tenant schema holds the staged facts")
    ap.add_argument("--dsn", default=os.environ.get("POSTGRES_DSN"),
                    help="PostgreSQL DSN (default: POSTGRES_DSN env)")
    ap.add_argument("--apply", action="store_true",
                    help="WRITE the soft-delete. Default is dry-run (no writes).")
    ap.add_argument("--sample", type=int, default=20,
                    help="how many sample rows to print in the report")
    args = ap.parse_args()

    if not args.dsn:
        print("ERROR: no --dsn and no POSTGRES_DSN env", file=sys.stderr)
        return 3

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    from src.provisioning.schema_manager import derive_user_slug_from_uuid

    schema = f"faultline_{derive_user_slug_from_uuid(args.user_id)}"
    print(f"target schema: {schema}   mode: {'APPLY' if args.apply else 'DRY-RUN'}")

    conn = None
    try:
        conn = psycopg2.connect(args.dsn)
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            rows = junk_staged_fact_ids(cur)
            if not rows:
                print("no junk-class staged facts found — nothing to quarantine")
                return 2
            ids = [r[0] for r in rows]
            surfaces = _resolve_surfaces(cur, ids, limit=args.sample)
            print(f"\nquarantine candidate rows: {len(ids)}")
            for rid in ids[: args.sample]:
                subj, obj = surfaces.get(rid, ("?", "?"))
                row = next(r for r in rows if r[0] == rid)
                print(f"  id={rid}  {subj!r} --{row[3]}{'(neg)' if row[4] == 'negated' else ''}--> {obj!r}")
            if len(ids) > args.sample:
                print(f"  … and {len(ids) - args.sample} more")

            if not args.apply:
                print("\nDRY-RUN: no rows written. Re-run with --apply to soft-delete "
                      "(deleted_at, preserved for forensics).")
                return 0

            with conn.cursor() as wcur:
                wcur.execute(f"SET search_path TO {schema}")
                wcur.execute(_APPLY_SQL, (ids,))
                n = wcur.rowcount
            conn.commit()
            print(f"\nAPPLIED: {n} staged facts soft-deleted (deleted_at=now()); "
                  f"rows retained for forensics — NO hard delete.")
            return 0
    except Exception as exc:  # noqa: BLE001 — operator script: report, never traceback-spam
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        print(f"ERROR: {exc!r}", file=sys.stderr)
        return 3
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
