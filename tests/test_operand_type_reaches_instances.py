"""Q2 reachability fix — a TYPE operand reaches its INSTANCES' dated occurrences.

The bug (LongMemEval Q2, "Which event did I attend first, the 'Effective Time
Management' workshop or the 'Data Analysis using Python' webinar?"): the comparison
resolves the operands to the TYPE words ("workshop"/"webinar") → the TYPE entities. But
the dated occurrence (``user participated_in <instance> @ <date>``) touches the NAMED
INSTANCE, not the type, so ``_operand_scope_facts`` (subject/object UUID ∈ operand_ids)
never reaches the dated event → "no record".

The fix (``_expand_type_operands_to_instances``): for an operand that is the OBJECT of a
classification edge (named instances point AT it via instance_of/subclass_of), walk ONE
classification hop down to its instances so their dated events become reachable.
Metadata-driven (classification rels from rel_types via _get_classification_rels),
deterministic (UUID set membership), one hop, fail-safe.
"""
from src.api import main


# ── deterministic fake DB (no live connection): instance_of edges + rel_types. ────
TYPE_WORKSHOP = "00000000-0000-0000-0000-0000000000aa"   # the TYPE entity "workshop"
INST_ETM = "00000000-0000-0000-0000-0000000000bb"        # instance "effective time mgmt"
TYPE_WEBINAR = "00000000-0000-0000-0000-0000000000cc"
INST_DAP = "00000000-0000-0000-0000-0000000000dd"        # instance "data analysis python"
USER = "00000000-0000-0000-0000-0000000000ff"


class _FakeCursor:
    def __init__(self, rows_for):
        self._rows_for = rows_for
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        if "from rel_types" in s and "is_hierarchy_rel" in s:
            # _get_classification_rels — P31/P279 classification rels
            self._rows = [("instance_of",), ("subclass_of",)]
        elif "select distinct subject_id from facts" in s and "object_id = any" in s:
            # one classification hop down: subjects whose instance_of object ∈ operands
            ops = set(params[0])
            edges = {  # subject instance_of object
                INST_ETM: TYPE_WORKSHOP,
                INST_DAP: TYPE_WEBINAR,
            }
            self._rows = [(subj,) for subj, obj in edges.items() if obj in ops]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows


class _FakeDB:
    def cursor(self):
        return _FakeCursor(None)


def _scope(facts, operand_ids):
    """Mirror of _operand_scope_facts' UUID-membership narrow (the load-bearing step)."""
    return main._operand_scope_facts(facts, operand_ids)


def _dated_events():
    return [
        {"subject": USER, "rel_type": "participated_in", "object": INST_ETM,
         "event_date": "2023-05-27"},
        {"subject": USER, "rel_type": "participated_in", "object": INST_DAP,
         "event_date": "2023-03-28"},
    ]


def test_type_operand_reaches_instances_dated_event():
    db = _FakeDB()
    cand = _dated_events()

    # BEFORE: operand "workshop" is the TYPE entity; the dated event touches the INSTANCE,
    # so the narrow (fail-safe to global) cannot strictly reach it.
    type_only = {TYPE_WORKSHOP}
    strict_before = [f for f in cand
                     if f["subject"] in type_only or f["object"] in type_only]
    assert strict_before == []   # the type entity itself is on no dated edge

    # FIX: one classification hop adds the instance.
    instances = main._expand_type_operands_to_instances(db, type_only)
    assert instances == {INST_ETM}

    operand_set = type_only | instances
    after = _scope(cand, operand_set)
    assert len(after) == 1
    assert after[0]["object"] == INST_ETM
    assert after[0]["event_date"] == "2023-05-27"


def test_both_type_operands_resolve_and_get_first_picks_earlier():
    db = _FakeDB()
    cand = _dated_events()

    ops = {TYPE_WORKSHOP, TYPE_WEBINAR}
    instances = main._expand_type_operands_to_instances(db, ops)
    assert instances == {INST_ETM, INST_DAP}

    operand_set = ops | instances
    # get_first runs the min(event_date) over the operand-scoped dated set.
    sliced = main._apply_get_first_last(cand, "first", operand_ids=operand_set)
    # comparison render returns the operands' events in chronological order, winner first.
    assert sliced[0]["object"] == INST_DAP          # webinar 2023-03-28 is earliest
    assert sliced[0]["event_date"] == "2023-03-28"


def test_failsafe_no_instances_returns_empty_set():
    db = _FakeDB()
    # a type with no instances pointing at it → no hop, unchanged.
    assert main._expand_type_operands_to_instances(db, {"unrelated-type-uuid"}) == set()


def test_failsafe_empty_and_none_operands():
    db = _FakeDB()
    assert main._expand_type_operands_to_instances(db, None) == set()
    assert main._expand_type_operands_to_instances(db, []) == set()
    assert main._expand_type_operands_to_instances(db, set()) == set()


def test_failsafe_no_db_returns_empty():
    assert main._expand_type_operands_to_instances(None, {TYPE_WORKSHOP}) == set()


def test_operand_never_includes_itself_as_instance():
    """A type that (degenerately) instance_of's itself must not be returned as its own
    instance — the helper subtracts the operands from the result."""
    class _SelfLoopCursor(_FakeCursor):
        def execute(self, sql, params=None):
            s = " ".join(sql.split()).lower()
            if "from rel_types" in s and "is_hierarchy_rel" in s:
                self._rows = [("instance_of",)]
            elif "select distinct subject_id from facts" in s:
                # the operand points at itself (self-loop classification edge)
                self._rows = [(o,) for o in params[0]]
            else:
                self._rows = []

    class _SelfLoopDB:
        def cursor(self):
            return _SelfLoopCursor(None)

    assert main._expand_type_operands_to_instances(_SelfLoopDB(), {TYPE_WORKSHOP}) == set()
