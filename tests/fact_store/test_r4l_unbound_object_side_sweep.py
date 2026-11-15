"""BUG-R4-L PIN — the sentence-old_value disarmed-guard object-side sweep (04:28:51 shape).

WOUND (prod, 2026-11-14 04:28:51.960665): the "Remove Nadia Herrera" forget issued TWO
/forget calls whose extracted old_value was a full SENTENCE ('Nadia Herrera was born on
2026-08-08' / 'Aurora flood is 0 years old') — an alias lookup that can NEVER resolve to
an entity UUID. In FactStoreManager.retract (store.py):

  • subject-side probe binds old_value (`object_id = <sentence>`) → 0 rows (correct);
  • the object-side FALLBACK did NOT bind old_value — it matched
    `object_id = subject(=user_uuid) [AND rel_type]` over every live row, ignoring the
    sentence entirely → ALL five (dax/theo/N-year-old, child_of, user) kin rows;
  • the SURGICAL TOMBSTONE GUARD keyed on `not old_value` — a TRUTHY unresolvable
    sentence string DISARMED it (guard only fired for old_value=None);
  → the mass tombstone UPDATE stamped deleted_at=now() on all five rows in ONE
    transaction; the second same-second call then found nothing → forget reported
    forgotten=0 while the damage was already done.

NOTE: 'Aurora' never textually matched anything — no fuzzy/LIKE arm is involved. The
collateral is purely the unbound object-side sweep + the truthy-old_value guard bypass.

FIXED (RETRACT_OBJECT_SIDE_VALUE_BIND, default ON): the object-side fallback now binds
old_value as `subject_id = old_value` (the flipped-arm mirror of the subject-side
`object_id = old_value` binding — the retracted value occupies the SUBJECT slot of a
flipped row), and the guard additionally refuses when the FALLBACK arm resolves to
more than one row. Test 1 (formerly RED-by-intent) is now GREEN-by-fix: the five kin
rows are NOT swept. Test 2 keeps the guard's old_value=None refusal leg. Test 3 pins
the legitimate path: a RESOLVED object value still retires the correct flipped row.
Test 4 pins the OFF-arm byte-parity: flag OFF reproduces the legacy sweep verbatim.
"""

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import src.fact_store.store as store_module
from src.fact_store.store import FactStoreManager

USER = "11111111-1111-1111-1111-111111111111"   # sam (the speaker; object of the kin rows)
DAX = "33333333-3333-3333-3333-333333333333"
THEO = "55555555-5555-5555-5555-555555555555"
KIN_IDS = [101, 102, 103, 104, 105]             # the five (X, child_of, user) rows
SENTENCE_OLD_VALUE = "aurora flood was born on 2026-08-08"  # unresolvable: no alias row
# a RESOLVED object value (the legitimate flipped-row target): (aurora, child_of, user)
AURORA = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
FLIPPED_ROW_ID = 201


class _R4LCursor:
    """Replays the /forget resolution: alias lookup for the sentence old_value MISSES
    (resolved_object stays the raw string), then answers the store's probes:
      • any facts probe that ALSO binds the sentence (subject-side, or the FIXED
        object-side fallback's subject_id binding) → 0 rows;
      • a facts probe binding ONLY the subject (the legacy object-side fallback) → KIN_IDS;
      • staged probes (dual mode) bind the sentence → 0 rows.
    """

    def __init__(self, resolved_object=None):
        self.executed = []
        self.resolved_object = resolved_object  # a UUID when the value RESOLVES, else None

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), tuple(params or ())))

    def fetchall(self):
        sql, params = self.executed[-1]
        if "FROM staged_facts" in sql:
            return []  # staged probe binds old_value → nothing
        if "FROM facts" in sql:
            where = sql.split("WHERE", 1)[1]
            if self.resolved_object is not None:
                # Legitimate FLIPPED storage: the subject-side probe (WHERE starts
                # with subject_id) finds nothing; the object-side fallback, binding
                # the resolved value as subject_id, matches its ONE flipped row.
                if where.startswith(" subject_id"):
                    return []
                bound_value = params[-1] if params else None
                return [(FLIPPED_ROW_ID,)] if bound_value == self.resolved_object else []
            # The value being retracted, when present as a probe param, is the LAST
            # param on every probe that binds it.
            bound_value = params[-1] if params and params[-1] not in (USER, "child_of") else None
            # Unresolvable sentence: any probe that binds it finds NOTHING.
            return [] if bound_value == SENTENCE_OLD_VALUE else [(i,) for i in KIN_IDS]
        return []  # the alias lookup in /forget — no row for the sentence

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mass_tombstone_fired(cur):
    return [sql for sql, _ in cur.executed
            if sql.startswith("UPDATE facts SET archived_at = now(), deleted_at = now()")]


def _fallback_probe(cur):
    return [sql for sql, _ in cur.executed
            if "FROM facts" in sql and "object_id = %s" in sql and "subject_id = %s" in sql]


def test_r4l_sentence_old_value_does_not_sweep_unrelated_kin_rows():
    """THE 04:28:51 SHAPE, POST-FIX: subject=user, rel=child_of, old_value=<unresolvable
    sentence> → the object-side fallback now BINDS the sentence (subject_id = <sentence>),
    matches nothing, and NO tombstone UPDATE runs — the five kin rows survive."""
    cur = _R4LCursor()
    ids, table = FactStoreManager(MagicMock()).retract(
        cur, USER, USER, "child_of", SENTENCE_OLD_VALUE, "hard_delete")
    # GREEN-by-fix: the five kin rows are NOT swept.
    assert ids == [], "the bound object-side probe matched nothing — no kin row captured"
    assert not _mass_tombstone_fired(cur), "no mass tombstone UPDATE ran"
    assert not [p for s, p in cur.executed if s.startswith("UPDATE facts")], \
        "no facts UPDATE of any kind ran"
    # Mechanism assertions: the fallback probe DID fire and DID bind the sentence.
    probes = _fallback_probe(cur)
    assert probes, "the object-side fallback probe ran"
    probe_params = [p for s, p in cur.executed
                    if "FROM facts" in s and "subject_id = %s" in s and "object_id = %s" in s]
    assert any(SENTENCE_OLD_VALUE in p for p in probe_params), \
        "the object-side fallback bound the sentence old_value (subject_id = %s)"


def test_r4l_guard_still_refuses_when_old_value_is_none():
    """ISOLATION: the identical shape with old_value=None is refused by the guard —
    the original `not old_value` leg of the re-keyed guard."""
    class _GuardCursor(_R4LCursor):
        def fetchall(self):
            sql, params = self.executed[-1]
            if "FROM staged_facts" in sql:
                return []
            if "FROM facts" in sql:
                # subject-side probe without old_value → treat as 0 (subject=user,
                # rel=child_of has no live rows: the kin rows point the other way)
                return [] if "subject_id" in sql.split("WHERE")[1].split("object_id")[0] else [(i,) for i in KIN_IDS]
            return []

    cur = _GuardCursor()
    ids, table = FactStoreManager(MagicMock()).retract(
        cur, USER, USER, "child_of", None, "hard_delete")
    assert ids == [] and table == "facts"
    assert not _mass_tombstone_fired(cur), "guard refused: no UPDATE ran"


def test_r4l_resolved_object_value_still_retires_the_correct_flipped_row():
    """LEGITIMATE PATH UNBROKEN: when the value RESOLVES (a real entity UUID), the
    same object-side fallback retires exactly the one correct flipped row
    (aurora, child_of, user) — the fix narrows the probe, it does not disable it."""
    cur = _R4LCursor(resolved_object=AURORA)
    ids, table = FactStoreManager(MagicMock()).retract(
        cur, USER, USER, "child_of", AURORA, "hard_delete")
    assert ids == [FLIPPED_ROW_ID] and table == "facts"
    updates = _mass_tombstone_fired(cur)
    assert len(updates) == 1, "the single correct row was tombstoned"
    upd_params = [p for s, p in cur.executed if s.startswith("UPDATE facts")]
    assert (FLIPPED_ROW_ID,) == upd_params[0], "the UPDATE targeted the flipped row only"
    # Defense-in-depth interplay: a fallback resolving to exactly ONE row is surgical
    # and proceeds — the guard's cap is >1, not the fallback arm itself.


def test_r4l_flag_off_reproduces_legacy_sweep_byte_for_byte():
    """OFF-ARM BYTE PARITY: with RETRACT_OBJECT_SIDE_VALUE_BIND=OFF the store behaves
    byte-for-byte like the pre-fix build — the fallback probe carries NO subject_id
    binding for old_value and the sweep reproduces (guard disarmed by the truthy
    sentence), which is the documented rollback lane."""
    legacy_fallback_sql = (
        "SELECT id FROM facts WHERE object_id = %s AND deleted_at IS NULL "
        "AND superseded_at IS NULL AND rel_type = %s"
    )
    prev = store_module.RETRACT_OBJECT_SIDE_VALUE_BIND
    store_module.RETRACT_OBJECT_SIDE_VALUE_BIND = False
    try:
        cur = _R4LCursor()
        ids, table = FactStoreManager(MagicMock()).retract(
            cur, USER, USER, "child_of", SENTENCE_OLD_VALUE, "hard_delete")
        assert ids == KIN_IDS, "legacy unbound fallback swept the five kin rows"
        updates = _mass_tombstone_fired(cur)
        assert len(updates) == 1, "one shared-timestamp mass tombstone UPDATE ran"
        upd_params = [p for s, p in cur.executed if s.startswith("UPDATE facts")]
        assert tuple(KIN_IDS) == upd_params[0], \
            "the tombstone UPDATE targeted exactly the five kin rows in ONE statement"
        fallbacks = [(s, p) for s, p in cur.executed
                     if "FROM facts" in s and s.startswith("SELECT id FROM facts WHERE object_id")]
        assert fallbacks and fallbacks[-1][0] == legacy_fallback_sql, \
            "the OFF-arm fallback SQL is byte-identical to the legacy probe"
        assert fallbacks[-1][1] == (USER, "child_of"), \
            "the OFF-arm fallback params are byte-identical to the legacy probe"
    finally:
        store_module.RETRACT_OBJECT_SIDE_VALUE_BIND = prev
