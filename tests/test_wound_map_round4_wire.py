"""THE WOUND-MAP ROUND-4 WIRE PINS (CORRECTION_SCHEDULE_RETIRE — item 5, the custody/
schedule-class supersession on the corrections wire).

THE WOUND: the residence-class correction retires the old address's fragment rows
(CORRECTION_RESIDENCE_RETIRE, the location-category arm); the CUSTODY/SCHEDULE class
stores its old value in other shapes — a value-node object on a supersede-class rel
((kids, has_setting, 'every other weekend')) or an entity_attributes value_text row —
and the wire's slot cascade keys on (subject, attribute), never on the VALUE, so the old
cadence row stayed live beside the corrected one and the plain query served BOTH regimes
whenever the correction rides the wire (the in-turn negation shape is covered by
SUPERSEDE_NEGATED_VALUES; the wire shape was not).

THE FIX under pin: `_supersede_wire_old_value_rows` — storage-shape-agnostic VALUE
supersession keyed on the correction's own old_value, sharing the in-turn arm's guards
(≥4-char span, in-order token-subsequence containment, ≤8-row cap, R7 denial-retire
marker on facts; supersede-class rels only) and its honesty contract (caller commits,
count follows the commit).
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


def test_w5w_flag_off_returns_zero_without_touching_the_db(monkeypatch):
    """OFF = byte-parity: the probe never runs — a sentinel connection is never read."""
    import src.api.main as M

    class _Boom:
        def cursor(self):
            raise AssertionError("the OFF-state probe must never open a cursor")

    monkeypatch.setattr(M, "CORRECTION_SCHEDULE_RETIRE", False)
    assert M._supersede_wire_old_value_rows(_Boom(), "u", "every other weekend") == 0


def test_w5w_short_span_fail_safe(monkeypatch):
    """A <4-char old_value span never probes (the 'not 3' class — never retire on a
    token-shorted denial)."""
    import src.api.main as M

    class _Boom:
        def cursor(self):
            raise AssertionError("a short span must never open a cursor")

    monkeypatch.setattr(M, "CORRECTION_SCHEDULE_RETIRE", True)
    assert M._supersede_wire_old_value_rows(_Boom(), "u", "no") == 0
    assert M._supersede_wire_old_value_rows(_Boom(), "u", "   ") == 0
    assert M._supersede_wire_old_value_rows(None, "u", "every other weekend") == 0


def test_w5w_span_normalization_and_token_guard():
    """The span is whitespace-normalized, lowercased, and tokenized by the SAME
    character class as the in-turn arm — the two lanes share one containment contract."""
    from src.api.main import _is_ordered_token_subsequence

    assert _is_ordered_token_subsequence(
        ["every", "other"], ["every", "other", "weekend"]) is True
    assert _is_ordered_token_subsequence(
        ["every", "weekend"], ["every", "other", "weekend"]) is True
    # NOT a subsequence: order matters (the bare-substring fuzz guard).
    assert _is_ordered_token_subsequence(
        ["weekend", "every"], ["every", "other", "weekend"]) is False


def test_w5w_shape1_requires_supersede_class_join():
    """The facts-shape query joins rel_types on correction_behavior='supersede' — an
    immutable/class edge can never be swept by a value match (assert on the SQL text so
    the guard survives refactors)."""
    import inspect
    import src.api.main as M

    src_text = inspect.getsource(M._supersede_wire_old_value_rows)
    assert "COALESCE(r.correction_behavior, '') = 'supersede'" in src_text
    assert "JOIN rel_types r ON r.rel_type = f.rel_type" in src_text
    # THE R7 DENIAL-RETIRE MARKER on the facts shape (the revive-refusal contract).
    assert "polarity = 'negated'" in src_text
    # THE ROW CAP (bounded sweep, same cap constant as the in-turn arm).
    assert "_NEGATED_VALUE_MAX_ROWS" in src_text
    # THE TENANT entity_attributes CARRIES NO id/polarity COLUMN — ctid handle only.
    assert "ctid = ANY(%s::tid[])" in src_text
    # The helper itself never commits (the caller commits before counting — the
    # SEC-BUG-1 honesty contract).
    assert "db.commit" not in src_text.replace("db_conn.commit", "db.commit")


_DSN = os.environ.get("POSTGRES_DSN", "")
_w5w_db = pytest.mark.skipif(
    not _DSN, reason="POSTGRES_DSN unset — the wire DB gate stays dark")


@_w5w_db
def test_w5w_db_both_shapes_retired():
    """DB-gated end-to-end on TEMP tables (they shadow the tenant names through
    search_path; the transaction rolls back): a live (kids, has_setting,
    'every other weekend') fact on a value-node entity AND a live entity_attributes
    value_text row both retire under the R7 marker; an immutable-class fact carrying
    the same value does NOT (the supersede-join guard); the count is returned for the
    caller's post-commit report."""
    import psycopg2
    import src.api.main as M

    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO pg_temp, public")
            cur.execute(
                "CREATE TEMP TABLE rel_types (rel_type text, category text,"
                " correction_behavior text, tail_types text[] DEFAULT '{}')")
            cur.execute(
                "CREATE TEMP TABLE entity_aliases (entity_id uuid, alias text,"
                " is_preferred boolean DEFAULT true)")
            cur.execute(
                "CREATE TEMP TABLE facts (id serial, subject_id uuid, rel_type text,"
                " object_id uuid, superseded_at timestamptz, polarity text,"
                " archived_at timestamptz, deleted_at timestamptz,"
                " qdrant_synced boolean DEFAULT true)")
            cur.execute(
                "CREATE TEMP TABLE entity_attributes (ctid_dummy int,"
                " value_text text, superseded_at timestamptz)")
            cur.execute("INSERT INTO rel_types VALUES"
                        " ('has_setting','system','supersede'),"
                        " ('instance_of','identity','immutable')")
            _kids = "11111111-1111-1111-1111-111111111111"
            _eow = "22222222-2222-2222-2222-222222222222"   # 'every other weekend' node
            cur.execute("INSERT INTO entity_aliases VALUES"
                        f" ('{_kids}','kids'), ('{_eow}','every other weekend')")
            cur.execute(
                f"INSERT INTO facts (subject_id, rel_type, object_id) VALUES"
                # the supersede-class cadence row AND an immutable-class row on the
                # SAME value node — the rel-class join must retire only the first.
                f" ('{_kids}','has_setting','{_eow}'),"
                f" ('{_kids}','instance_of','{_eow}')")
            cur.execute(
                "INSERT INTO entity_attributes (ctid_dummy, value_text) VALUES"
                " (0, 'every other weekend'), (1, 'weekday custody')")
        conn.commit()
        retired = M._supersede_wire_old_value_rows(conn, "u", "Every  Other Weekend")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT subject_id, polarity FROM facts WHERE superseded_at IS NOT NULL")
            rows = cur.fetchall()
            cur.execute(
                "SELECT value_text FROM entity_attributes"
                " WHERE superseded_at IS NOT NULL")
            arows = cur.fetchall()
        conn.rollback()
    finally:
        conn.close()
    # THE SUPERSHAPE retired, the immutable twin untouched, count honest.
    assert retired == 2, retired
    assert rows == [(_kids, "negated")], rows
    assert arows == [("every other weekend",)], arows

