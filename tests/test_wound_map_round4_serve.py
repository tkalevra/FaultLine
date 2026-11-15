"""THE WOUND-MAP ROUND-4 SERVE PINS (the two serve-side renders the round-4 critic
measured live — capture was already CORRECT, the rows land, the recalls still miss).

WOUND 1 (vm-backup): the store holds (proxmox, back='2am' attr) AND (plex vm,
has_setting='nightly to the nas at 2am'), but the relational neighborhood's
fact-list render carried neither — the walk's scoped projection drops the attr rows
(CS16) and never fetches the has_setting value-node edges. THE FIX under pin:
``_setting_rides_neighborhood_rows`` + the Step 3d-2 ride in ``_query_impl``, flag
``RECALL_SETTING_RIDES_NEIGHBORHOOD`` default ON, OFF = byte-parity.

WOUND 2 (rowan-grade): rowan.grade='7' renders "Rowan's grade is 7" and the gold
tokens are ['seventh', '7th']. THE FIX under pin: ``_scalar_attr_possessive_prose``
(the pure render helper factored out of convert_to_prose's scalar-attribute block)
appends the closed ordinal suffix for the grade class ONLY, flag
``RECALL_GRADE_ORDINAL_RENDER`` default ON, OFF = byte-parity.
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


# ── WOUND 2: the grade ordinal render (pure, DB-free) ──────────────────────────────


def test_w4s_ordinal_suffix_closed_function():
    from src.api.main import _ordinal_suffix

    assert _ordinal_suffix(1) == "st"
    assert _ordinal_suffix(2) == "nd"
    assert _ordinal_suffix(3) == "rd"
    assert _ordinal_suffix(4) == "th"
    assert _ordinal_suffix(7) == "th"
    # THE TEENS ARE ALL -th AHEAD OF THE LAST-DIGIT RULE.
    assert _ordinal_suffix(11) == "th"
    assert _ordinal_suffix(12) == "th"
    assert _ordinal_suffix(13) == "th"
    assert _ordinal_suffix(21) == "st"
    assert _ordinal_suffix(22) == "nd"
    assert _ordinal_suffix(23) == "rd"
    assert _ordinal_suffix(101) == "st"
    assert _ordinal_suffix(112) == "th"


def test_w4s_grade_surface_value_shaped():
    """Only a PURE-DIGIT value gains the suffix; anything already voiced (or
    non-numeric) returns None → the stored value renders verbatim."""
    from src.api.main import _grade_ordinal_surface

    assert _grade_ordinal_surface("7") == "7th"
    assert _grade_ordinal_surface("12") == "12th"
    assert _grade_ordinal_surface("1") == "1st"
    # NOT pure digits → None (already-ordinal, word forms, empty, None).
    assert _grade_ordinal_surface("7th") is None
    assert _grade_ordinal_surface("seventh") is None
    assert _grade_ordinal_surface("kindergarten") is None
    assert _grade_ordinal_surface("") is None
    assert _grade_ordinal_surface(None) is None
    assert _grade_ordinal_surface("7 am") is None


def test_w4s_grade_ordinal_flag_off_is_byte_parity(monkeypatch):
    import src.api.main as M

    monkeypatch.setattr(M, "RECALL_GRADE_ORDINAL_RENDER", False)
    assert M._grade_ordinal_surface("7") is None
    # The possessive render keeps the stored value verbatim (the legacy line).
    assert (M._scalar_attr_possessive_prose("Rowan", "grade", "7", False, "grade")
            == "Rowan's grade is 7")


def test_w4s_scalar_possessive_render_gold_token():
    """THE RENDERED ROW SATISFIES THE GOLD TOKEN '7th' — both perspectives."""
    from src.api.main import _scalar_attr_possessive_prose as _sp

    assert _sp("Rowan", "grade", "7", False, "grade") == "Rowan's grade is 7th"
    assert _sp("rowan", "Grade", "7", False, "grade") == "rowan's grade is 7th"
    # 2p: "Your grade is 7th".
    assert _sp("you", "grade", "7", True, "grade") == "Your grade is 7th"
    # A subject ending in 's' takes the bare apostrophe (legacy convention kept).
    assert _sp("The Jess", "grade", "7", False, "grade") == "The Jess' grade is 7th"
    # Empty label declines (caller keeps the template path).
    assert _sp("Rowan", "", "7", False, "grade") is None
    assert _sp(None, "grade", "7", False, "grade") is None


def test_w4s_ordinal_applies_ONLY_to_the_grade_class():
    """NEGATIVE GUARDS: an age '7' stays '7'; a quantity '7' stays '7'; every
    non-grade rel keeps the stored value verbatim — no rel-name list beyond the
    grade class the store types."""
    from src.api.main import _scalar_attr_possessive_prose as _sp

    assert _sp("Amara", "age", "7", False, "age") == "Amara's age is 7"
    assert _sp("The pack", "count", "7", False, "quantity") == (
        "The pack's count is 7")
    assert _sp("Server", "ram", "64 gigabytes", False, "ram") == (
        "Server's ram is 64 gigabytes")
    # An age-valued fact whose rel HAPPENS to be named grade-like but is not the
    # grade class token does not pick the suffix up.
    assert _sp("Team", "grade point", "7", False, "grade_point") == (
        "Team's grade point is 7")


def test_w4s_convert_to_prose_routes_the_scalar_block_through_the_helper():
    """The walk's render (convert_to_prose) calls the pinned pure helper at the
    engine-grown-attribute seam — assert on the source so the routing survives
    refactors (same discipline as the wire pins)."""
    import inspect
    import src.api.main as M

    src_text = inspect.getsource(M.convert_to_prose)
    assert "_scalar_attr_possessive_prose(" in src_text


# ── WOUND 1: the setting ride (fetch helper + call-site gate) ──────────────────────


def test_w4s_ride_helper_empty_targets_return_empty():
    from src.api.main import _setting_rides_neighborhood_rows

    class _Boom:
        def cursor(self):
            raise AssertionError("no targets → no cursor")

    assert _setting_rides_neighborhood_rows(_Boom(), set()) == []


def test_w4s_ride_call_site_is_flag_gated_and_neighborhood_coupled():
    """The Step 3d-2 block in _query_impl fires ONLY under the flag AND only when
    a relational neighborhood was actually served (db_facts non-empty — no live
    row → nothing rides → the abstention stands). Assert on the source."""
    import inspect
    import src.api.main as M

    src_text = inspect.getsource(M._query_impl)
    assert "if _RECALL_SETTING_RIDES_NEIGHBORHOOD and db and db_facts:" in src_text
    assert "_setting_rides_neighborhood_rows(db, _ride_uuids)" in src_text


_DSN = os.environ.get("POSTGRES_DSN", "")
_w4s_db = pytest.mark.skipif(
    not _DSN, reason="POSTGRES_DSN unset — the serve DB gate stays dark")


@_w4s_db
def test_w4s_db_ride_carries_clock_rows_not_plain_scalars():
    """DB-gated on TEMP tables (they shadow the tenant names through search_path;
    the transaction rolls back): the neighborhood (proxmox + plex vm) rides the
    clock-valued attr (back='2am'), the has_setting facts edge, and the staged
    has_setting twin — while a plain scalar (age='7') and a superseded has_setting
    row do NOT ride. No live row → the helper returns nothing (abstention stands)."""
    import psycopg2
    import src.api.main as M

    _prox = "33333333-3333-3333-3333-333333333333"   # proxmox
    _plex = "44444444-4444-4444-4444-444444444444"   # plex vm
    _val = "55555555-5555-5555-5555-555555555555"    # 'nightly to the nas at 2am' node
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO pg_temp, public")
            cur.execute(
                "CREATE TEMP TABLE entity_attributes (entity_id text, attribute text,"
                " value_text text, value_int int, value_float float,"
                " value_date timestamptz, superseded_at timestamptz, provenance text)")
            cur.execute(
                "CREATE TEMP TABLE facts (subject_id uuid, rel_type text,"
                " object_id uuid, superseded_at timestamptz, archived_at timestamptz,"
                " deleted_at timestamptz, valid_until timestamptz,"
                " confidence float, fact_class text, fact_provenance text)")
            cur.execute(
                "CREATE TEMP TABLE staged_facts (subject_id uuid, rel_type text,"
                " object_id uuid, expires_at timestamptz, promoted_at timestamptz,"
                " deleted_at timestamptz, confidence float, fact_class text,"
                " fact_provenance text)")
            cur.execute(
                "INSERT INTO entity_attributes VALUES"
                f" ('{_prox}','back','2am',NULL,NULL,NULL,NULL,'user_stated'),"
                f" ('{_prox}','age','7',NULL,NULL,NULL,NULL,'user_stated'),"
                f" ('{_plex}','has_setting','nightly to the nas at 2am',"
                " NULL,NULL,NULL,NULL,'user_stated')")
            cur.execute(
                "INSERT INTO facts VALUES"
                # live has_setting edge on plex + a SUPERSEDED twin that must not ride
                f" ('{_plex}','has_setting','{_val}',NULL,NULL,NULL,NULL,0.9,'A',"
                "'user_stated'),"
                f" ('{_plex}','has_setting','{_val}',now(),NULL,NULL,NULL,0.9,'A',"
                "'user_stated')")
            cur.execute(
                "INSERT INTO staged_facts VALUES"
                f" ('{_prox}','has_setting','{_val}',NULL,NULL,NULL,0.6,'B',NULL)")
        conn.commit()
        rows = M._setting_rides_neighborhood_rows(conn, {_prox, _plex})
        with conn.cursor() as cur:
            cur.execute("DROP TABLE entity_attributes, facts, staged_facts")
        conn.commit()
    finally:
        conn.close()
    keys = sorted((str(r["subject"])[:8], r["rel_type"],
                   str(r["object"])) for r in rows)
    # THE CLOCK ATTR rides ('back'='2am'), the has_setting ATTR rides, the live
    # facts edge rides, the staged twin rides — the plain 'age' scalar does NOT.
    assert ("33333333", "back", "2am") in keys
    assert ("44444444", "has_setting", "nightly to the nas at 2am") in keys
    assert ("33333333", "has_setting", _val) in keys
    assert ("44444444", "has_setting", _val) in keys
    assert ("33333333", "age", "7") not in keys
    # The superseded facts twin did not double-ride: exactly one facts-edge row
    # for (plex, has_setting, val) beyond the attr row.
    plex_fact_edges = [k for k in keys
                       if k[0] == "44444444" and k[1] == "has_setting"
                       and k[2] == _val]
    assert len(plex_fact_edges) == 1
    # Every riding row carries the standard walk shape the render consumes.
    for r in rows:
        assert r.get("subject") and r.get("rel_type") and r.get("object")
        assert r.get("source") in ("attributes", "db", "staged")


@_w4s_db
def test_w4s_db_no_live_row_nothing_rides():
    """METADATA-COUPLED negative: a neighborhood whose entities hold no
    has_setting / clock rows rides nothing — the abstention stands."""
    import psycopg2
    import src.api.main as M

    _e = "66666666-6666-6666-6666-666666666666"
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO pg_temp, public")
            cur.execute(
                "CREATE TEMP TABLE entity_attributes (entity_id text, attribute text,"
                " value_text text, value_int int, value_float float,"
                " value_date timestamptz, superseded_at timestamptz, provenance text)")
            cur.execute(
                "CREATE TEMP TABLE facts (subject_id uuid, rel_type text,"
                " object_id uuid, superseded_at timestamptz, archived_at timestamptz,"
                " deleted_at timestamptz, valid_until timestamptz,"
                " confidence float, fact_class text, fact_provenance text)")
            cur.execute(
                "CREATE TEMP TABLE staged_facts (subject_id uuid, rel_type text,"
                " object_id uuid, expires_at timestamptz, promoted_at timestamptz,"
                " deleted_at timestamptz, confidence float, fact_class text,"
                " fact_provenance text)")
            cur.execute(
                "INSERT INTO entity_attributes VALUES"
                f" ('{_e}','color','teal',NULL,NULL,NULL,NULL,'user_stated')")
        conn.commit()
        rows = M._setting_rides_neighborhood_rows(conn, {_e})
        with conn.cursor() as cur:
            cur.execute("DROP TABLE entity_attributes, facts, staged_facts")
        conn.commit()
    finally:
        conn.close()
    assert rows == []


@_w4s_db
def test_w4s_db_ride_text_entity_id_tenant_shape_and_txn_survives():
    """REGRESSION PIN (the bench-press blank, 2026-11-13): the REAL tenant schema's
    entity_attributes.entity_id is TEXT, not uuid — the ride originally compared it
    to ANY(%s::uuid[]) which raised 'operator does not exist: text = uuid'. The
    helper swallowed that error but the aborted transaction then poisoned the
    SHARED connection: every later lookup in _query_impl failed with 'current
    transaction is aborted' and a served CS16 traversed-attr (the 140-pound
    bench-press row) rendered facts []. This pin holds BOTH invariants in the
    bench-press shape — a scalar/quantity attr alongside a relational
    neighborhood: (a) with the flag ON the ride runs clean on the TEXT-typed
    tenant shape and carries its rows, and (b) after any failure the connection
    is left USABLE (fail-safe must not blank the neighboring attr serve)."""
    import psycopg2
    import src.api.main as M

    assert M._RECALL_SETTING_RIDES_NEIGHBORHOOD is True  # the ON default
    _anchor = "77777777-7777-7777-7777-777777777777"     # relational neighbour
    _self = "88888888-8888-8888-8888-888888888888"
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO pg_temp, public")
            # THE TENANT SHAPE: entity_id TEXT (18-col table's real typing), the
            # uuid[] cast this pin guards against would fail right here.
            cur.execute(
                "CREATE TEMP TABLE entity_attributes (entity_id text, attribute text,"
                " value_text text, value_int int, value_float float,"
                " value_date timestamptz, superseded_at timestamptz, provenance text)")
            cur.execute(
                "CREATE TEMP TABLE facts (subject_id uuid, rel_type text,"
                " object_id uuid, superseded_at timestamptz, archived_at timestamptz,"
                " deleted_at timestamptz, valid_until timestamptz,"
                " confidence float, fact_class text, fact_provenance text)")
            cur.execute(
                "CREATE TEMP TABLE staged_facts (subject_id uuid, rel_type text,"
                " object_id uuid, expires_at timestamptz, promoted_at timestamptz,"
                " deleted_at timestamptz, confidence float, fact_class text,"
                " fact_provenance text)")
            # The bench-press shape: a QUANTITY attr on the neighbour (must be
            # untouched by the ride lane — it neither carries nor drops it) plus
            # the ride's own clock row.
            cur.execute(
                "INSERT INTO entity_attributes VALUES"
                f" ('{_anchor}','bench_press_record','140 pounds',NULL,NULL,NULL,"
                "NULL,'user_stated'),"
                f" ('{_anchor}','back','2am',NULL,NULL,NULL,NULL,'user_stated')")
        conn.commit()
        rows = M._setting_rides_neighborhood_rows(conn, {_anchor, _self})
        # (a) the ride carries its clock row on the TEXT-typed tenant shape...
        keys = {(str(r["subject"])[:8], r["rel_type"], str(r["object"]))
                for r in rows}
        assert ("77777777", "back", "2am") in keys
        # ...and does NOT swallow the plain quantity attr's slot either way
        # (it is simply not a ride row — the CS16 lane owns it).
        assert ("77777777", "bench_press_record", "140 pounds") not in keys
        with conn.cursor() as cur:
            cur.execute("DROP TABLE entity_attributes, facts, staged_facts")
        conn.commit()
        # (b) THE TXN POISON GUARD: a query AFTER the ride on the same connection
        # must succeed — the swallowed-error path must rollback, never abort.
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone()[0] == 1
        # And the forced-failure path (helper raising mid-SQL) leaves the
        # connection usable: value_text typed int makes the clock-token regex
        # raise 'operator does not exist: integer ~* text' INSIDE the helper.
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE entity_attributes (entity_id text, attribute text,"
                " value_text int, value_int int, value_float float,"
                " value_date timestamptz, superseded_at timestamptz, provenance text)")
        conn.commit()
        assert M._setting_rides_neighborhood_rows(
            conn, {_anchor}) == []  # fail-safe: nothing rides after rollback
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone()[0] == 1
        with conn.cursor() as cur:
            cur.execute("DROP TABLE entity_attributes")
        conn.commit()
    finally:
        conn.close()


@_w4s_db
def test_w4s_db_ride_text_subject_id_tenant_shape_carries_edges():
    """THE ROUND-4 LIVE-GAP PIN (measured on the real tenant schema, 2026-11-13):
    the tenant facts AND staged_facts subject_id columns are TEXT, not uuid — the
    ride's has_setting arms originally compared them to ANY(%s::uuid[]) which
    raised 'operator does not exist: text = uuid' on EVERY live serve. The
    fail-safe rollback kept the relational answer alive (the earlier pin's
    discipline) but the ride returned [] — so the measured wound stood: "when do
    the homelab backups run?" served only run/run_on/back_to and never the
    '2am' row the store held, and even the arm-1 attr rows were discarded with
    the aborted arm. This pin holds the tenant TEXT shape for BOTH id columns:
    the has_setting facts edge, the staged twin, AND the arm-1 clock attr all
    ride (the cast is shape-agnostic: subject_id::text matches uuid columns
    too — the uuid-shaped leg above stays the cross-shape guard)."""
    import psycopg2
    import src.api.main as M

    _prox = "33333333-3333-3333-3333-333333333333"   # proxmox
    _plex = "44444444-4444-4444-4444-444444444444"   # plex vm
    _val = "55555555-5555-5555-5555-555555555555"    # value-node object
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO pg_temp, public")
            # THE REAL TENANT SHAPE: entity_id AND subject_id/object_id TEXT.
            cur.execute(
                "CREATE TEMP TABLE entity_attributes (entity_id text, attribute text,"
                " value_text text, value_int int, value_float float,"
                " value_date timestamptz, superseded_at timestamptz, provenance text)")
            cur.execute(
                "CREATE TEMP TABLE facts (subject_id text, rel_type text,"
                " object_id text, superseded_at timestamptz, archived_at timestamptz,"
                " deleted_at timestamptz, valid_until timestamptz,"
                " confidence float, fact_class text, fact_provenance text)")
            cur.execute(
                "CREATE TEMP TABLE staged_facts (subject_id text, rel_type text,"
                " object_id text, expires_at timestamptz, promoted_at timestamptz,"
                " deleted_at timestamptz, confidence float, fact_class text,"
                " fact_provenance text)")
            cur.execute(
                "INSERT INTO entity_attributes VALUES"
                f" ('{_prox}','back','2am',NULL,NULL,NULL,NULL,'user_stated')")
            cur.execute(
                "INSERT INTO facts VALUES"
                f" ('{_plex}','has_setting','{_val}',NULL,NULL,NULL,NULL,0.9,'A',"
                "'user_stated')")
            cur.execute(
                "INSERT INTO staged_facts VALUES"
                f" ('{_prox}','has_setting','{_val}',NULL,NULL,NULL,0.6,'B',NULL)")
        conn.commit()
        rows = M._setting_rides_neighborhood_rows(conn, {_prox, _plex})
        with conn.cursor() as cur:
            cur.execute("DROP TABLE entity_attributes, facts, staged_facts")
        conn.commit()
    finally:
        conn.close()
    keys = sorted((str(r["subject"])[:8], r["rel_type"], str(r["object"]))
                  for r in rows)
    # THE WOUND'S ROW RIDES: arm 1's clock attr survived the arm-2 fetch (the
    # aborted-transaction discard is gone), and both has_setting edges ride on
    # the TEXT-typed tenant shape.
    assert ("33333333", "back", "2am") in keys
    assert ("44444444", "has_setting", _val) in keys
    assert ("33333333", "has_setting", _val) in keys


def test_w4s_ride_sql_is_shape_agnostic_no_uuid_array_cast():
    """SOURCE PIN: the ride's has_setting arms must cast the COLUMN to text
    (``subject_id::text = ANY(%s)``), never the parameter to uuid[] — the
    uuid[] cast is exactly the operator error that blanked the live ride."""
    import inspect
    import src.api.main as M
    src_text = inspect.getsource(M._setting_rides_neighborhood_rows)
    assert "ANY(%s::uuid[])" not in src_text
    assert src_text.count("subject_id::text = ANY(%s)") == 2
