"""BUG-05 pins (adversarial bench 2026-10-24): junk-class staged facts are quarantined
and no longer surface — in recall reads AND in SHELF growth.

What this pins, in the order it matters:

 1. THE CLASSIFIER. The quarantine's four deterministic junk classes select exactly
    the observed debris and spare the known-legit shapes (a real document stub, an
    entity with a real surface that also carries a short alias).
 2. THE TOMBSTONE. Soft-delete via ``deleted_at`` (never a hard DELETE — forensics
    preserved) makes the row invisible to the staged recall lanes (they already filter
    ``deleted_at IS NULL``) and, since this lane, to the grouping-mint co-membership
    SELECT too.
 3. THE RECALL LANE SQL. The staged fetch in ``fetch_facts_from_anchor`` keeps the
    tombstone filter on BOTH scope shapes — the pin that quarantined junk can never
    surface in recall again.

Run with:
  python3 tools/fltest.py --bug BUG05 --test tests/test_quarantine_junk_staged_facts.py

The DB half is SKIPPED without POSTGRES_DSN (scratch schema, dropped unconditionally).
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "quarantine_junk_staged_facts",
    _REPO / "scripts" / "repairs" / "quarantine_junk_staged_facts.py",
)
Q = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(Q)  # operator script: importable without side effects

# ── pure-classifier pins (no DB) ───────────────────────────────────────────────


def test_temperature_pattern_matches_only_bare_temperature_surfaces():
    import re
    rx = re.compile(Q.TEMPERATURE_FRAGMENT_SQL, re.IGNORECASE)
    for junk in ("° f", "°f", "75°c", "230 °C", "4 °c", "°", "1.5°k"):
        assert rx.search(junk), junk
    for legit in ("Heating at 75°C suppressed spoilage",
                  "Degrees of freedom", "Temperature control"):
        assert not rx.search(legit), legit


def test_quarantine_constants_pin_the_legit_stub_shape():
    """The classifier must keep sparing the ONE shape the stub lane writes against the
    document node (server.py::_mint_document_stub_edges): instance_of + affirmed."""
    assert Q.FRAGMENT_MAX_ALIAS_CHARS == 3
    assert Q.DOCUMENT_TYPE_SURFACE == "document"
    assert (Q.LEGIT_STUB_REL, Q.LEGIT_STUB_POLARITY) == ("instance_of", "affirmed")


def test_apply_is_a_guarded_soft_delete_never_hard_delete():
    assert "UPDATE staged_facts" in Q._APPLY_SQL
    assert "SET deleted_at = now()" in Q._APPLY_SQL
    assert "AND deleted_at IS NULL" in Q._APPLY_SQL  # guarded, idempotent
    assert not any(kw in Q._APPLY_SQL.upper() for kw in ("DELETE FROM", "TRUNCATE"))


# ── DB-backed pins: selection precision + tombstone effect ─────────────────────


def _dsn() -> str | None:
    return os.getenv("POSTGRES_DSN")


@pytest.fixture()
def tenant_cursor():
    """Throwaway tenant schema with ONLY the tables the quarantine touches (built from
    the REAL template DDL slices), search_path bound WITHOUT public. Dropped always."""
    dsn = _dsn()
    if not dsn:
        pytest.skip("no POSTGRES_DSN — DB pins skipped")
    psycopg2 = pytest.importorskip("psycopg2")
    template = (_REPO / "src/provisioning/templates/user_schema.sql").read_text()

    def _slice(table: str) -> str:
        start = template.index(f"CREATE TABLE IF NOT EXISTS {table} (")
        # terminate on a LINE-START ");" — comment text inside the DDL can contain ");"
        end = template.index("\n);", start) + 3
        return template[start:end]

    ddl = "\n".join([_slice("staged_facts"), _slice("entities"), _slice("entity_aliases")])
    schema = "faultline_quarantine_test_" + uuid.uuid4().hex[:12]
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(ddl)
            yield cur
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            conn.close()


def _mk_entity(cur, eid, aliases, preferred=None):
    cur.execute("INSERT INTO entities (id) VALUES (%s) ON CONFLICT DO NOTHING", (eid,))
    for a in aliases:
        cur.execute(
            "INSERT INTO entity_aliases (entity_id, alias, is_preferred) "
            "VALUES (%s, %s, %s)",
            (eid, a, a == (preferred if preferred is not None else aliases[0])))


def _mk_staged(cur, subj, rel, obj, polarity="affirmed"):
    cur.execute(
        "INSERT INTO staged_facts (subject_id, object_id, rel_type, fact_class, "
        "polarity, fact_provenance) VALUES (%s, %s, %s, 'B', %s, 'llm_inferred') "
        "RETURNING id",
        (subj, obj, rel, polarity))
    return cur.fetchone()[0]


def test_quarantine_selects_junk_and_spares_legit_rows(tenant_cursor):
    """THE CLASSIFIER PIN, against real tables: every observed junk class is selected;
    the legit document-stub row and a known-alias entity are NOT."""
    cur = tenant_cursor
    e = {k: str(uuid.uuid4()) for k in
         ("temp", "frag", "empty", "doc_node", "stub_doc", "known", "debris", "user")}

    _mk_entity(cur, e["temp"], ["° f"])                     # junk: temperature fragment
    _mk_entity(cur, e["frag"], ["ie"])                      # junk: fragment-only entity
    _mk_entity(cur, e["empty"], [""])                       # junk: empty surface
    _mk_entity(cur, e["doc_node"], ["document"])            # the seeded type node
    _mk_entity(cur, e["stub_doc"], ["My Real Paper"])
    _mk_entity(cur, e["known"], ["cj", "Sam Jay"])  # KNOWN: also has a real surface
    _mk_entity(cur, e["debris"], ["First Soap"])
    _mk_entity(cur, e["user"], ["user"])

    # ── junk rows (each endpoint touches a junk surface / debris shape) ──
    _mk_staged(cur, e["user"], "member_of", e["temp"])            # ° f member_of temperature
    _mk_staged(cur, e["user"], "related_to", e["frag"])           # … —han→ ie
    _mk_staged(cur, e["user"], "located_in", e["empty"])          # empty object
    _mk_staged(cur, e["frag"], "located_in", e["user"])           # fragment subject
    _mk_staged(cur, e["debris"], "subclass_of", e["doc_node"])    # First Soap … Document
    _mk_staged(cur, e["debris"], "instance_of", e["doc_node"],
               polarity="negated")                                # "First Soap is not Document"
    # ── legit rows that MUST survive ──
    _mk_staged(cur, e["stub_doc"], "instance_of", e["doc_node"])  # real stub, affirmed
    _mk_staged(cur, e["user"], "knows", e["known"])               # entity with real surface
    _mk_staged(cur, e["user"], "owns", e["stub_doc"])             # reachability stub edge

    rows = Q.junk_staged_fact_ids(cur)
    selected_endpoints = {r[1] for r in rows} | {r[2] for r in rows}
    junk_endpoints = {e["temp"], e["frag"], e["empty"], e["doc_node"], e["debris"], e["user"]}
    legit_endpoints = {e["stub_doc"], e["known"]}
    assert selected_endpoints & junk_endpoints, "no junk selected — classifier dead"
    assert not selected_endpoints & legit_endpoints, (
        f"legit endpoint selected: {selected_endpoints & legit_endpoints}")
    # all six junk rows selected, all three legit rows spared
    assert len(rows) == 6, f"expected 6 junk rows, got {len(rows)}"


def test_soft_delete_hides_rows_from_recall_lanes_and_growth(tenant_cursor):
    """THE TOMBSTONE PIN: after the quarantine's UPDATE, the staged row is invisible to
    (a) the recall staged SELECT (deleted_at IS NULL — the fetch_facts_from_anchor
    predicate) and (b) the grouping-mint co-membership predicate (tombstone parity
    added this lane). The row itself survives for forensics."""
    cur = tenant_cursor
    e_temp, e_user = str(uuid.uuid4()), str(uuid.uuid4())
    _mk_entity(cur, e_temp, ["° f"])
    _mk_entity(cur, e_user, ["user"])
    sid = _mk_staged(cur, e_user, "member_of", e_temp)

    rows = Q.junk_staged_fact_ids(cur)
    assert [r[0] for r in rows] == [sid]
    cur.execute(Q._APPLY_SQL, ([r[0] for r in rows],))

    # (a) recall staged lane predicate
    cur.execute(
        "SELECT count(*) FROM staged_facts "
        " WHERE (subject_id = %s) AND (expires_at IS NULL OR expires_at > now()) "
        "   AND promoted_at IS NULL AND deleted_at IS NULL", (e_user,))
    assert cur.fetchone()[0] == 0, "quarantined row still visible to the recall lane"

    # (b) grouping-mint co-membership predicate (tombstone parity)
    cur.execute(
        "SELECT count(*) FROM staged_facts "
        " WHERE rel_type = ANY(%s) AND promoted_at IS NULL AND deleted_at IS NULL",
        (["member_of"],))
    assert cur.fetchone()[0] == 0, "quarantined row still feeds the grouping-mint lane"

    # forensics: the row is still THERE, marked — no hard delete
    cur.execute("SELECT count(*) FROM staged_facts WHERE deleted_at IS NOT NULL")
    assert cur.fetchone()[0] == 1


def test_recall_staged_lane_sql_carries_tombstone_both_shapes():
    """Source-shape pin: both staged fetches in fetch_facts_from_anchor filter
    deleted_at IS NULL (the mechanism that makes the quarantine effective in recall)."""
    import inspect
    from src.api import main as api_main

    src = inspect.getsource(api_main.fetch_facts_from_anchor)
    assert src.count("AND deleted_at IS NULL") >= 3, (
        "the staged recall lanes lost the Phase-4 tombstone filter — quarantined junk "
        "would resurface in recall")


def test_grouping_mint_lane_carries_tombstone_parity():
    """Source-shape pin: the grouping-mint co-membership SELECT (this lane's fix) keeps
    deleted_at IS NULL on the staged arm so quarantined junk cannot grow the SHELF."""
    import inspect
    from src.api import main as api_main

    src = inspect.getsource(api_main)
    anchor = src.index("SELECT subject_id, object_id, rel_type FROM staged_facts")
    window = src[anchor:anchor + 700]
    assert "deleted_at IS NULL" in window, (
        "grouping-mint staged arm lost the tombstone filter — quarantined junk feeds growth")
