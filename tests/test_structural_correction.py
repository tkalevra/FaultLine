"""Unit tests for STRUCTURAL CORRECTION
(DESIGN-hierarchy-ladder-and-growth.md §"user correction is REAL" → "Correction
works at BOTH levels").

A user statement that edits the HIERARCHY itself ("my pets are not part of my
family") severs the nesting — it is NOT a fact retraction and NOT a junk does_not_*
fact. The system ACTS on the structure: removes the child group from the parent's
member_taxonomies, records it in severed_taxonomies (the durable, NOT-superseded
lock), and marks source='user_corrected'. The growth engine must consult that lock
and refuse to re-link.

These cover the pure-logic + DB-bound surfaces with a fake cursor (no live DB):
  - _detect_structural_correction  (groups + nesting rel + negation → structural)
                                    (a normal fact correction → None, unchanged path)
  - _apply_structural_correction   (member_taxonomies sever + severed_taxonomies +
                                    source='user_corrected')
  - embedder.taxonomy_link_is_severed / add_member_taxonomy_if_not_severed
                                    (engine can NEVER re-link a user-severed pair)

Subject-agnostic: no hardcoded group/rel names are asserted; the fakes drive the
metadata so "Mara's pets aren't her family" works the same as "my pets ...".
"""
from src.api import main as m
from src.re_embedder import embedder as e


# ── Fake DB plumbing ─────────────────────────────────────────────────────────
class FakeCursor:
    """Minimal cursor: routes on a substring of the SQL to canned results, and
    records UPDATE statements + params for assertion."""

    def __init__(self, *, taxonomies, nesting_rels, classification_rels, severed=None):
        # taxonomies: list[(taxonomy_name, member_taxonomies_list)]
        self._taxonomies = taxonomies
        # nesting_rels: list[(rel_type, natural_language, natural_language_2p)]
        self._nesting_rels = nesting_rels
        # classification_rels: set of rel_type strings (instance_of/subclass_of/is_a)
        self._classification_rels = classification_rels
        # severed: dict[taxonomy_name] -> list[child] (for the engine guard query)
        self._severed = severed or {}
        self._last = None
        self._result = None
        self.rowcount = 0
        self.executed = []  # (sql, params)

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        s = " ".join(sql.split())  # normalize whitespace
        self._last = s
        low = s.lower()

        # _get_classification_rels: SELECT rel_type ... WHERE is_hierarchy_rel = true AND wikidata_pid = ANY
        if "wikidata_pid = any" in low:
            self._result = [(r,) for r in self._classification_rels]
        # _get_membership_nesting_rels: SELECT rel_type, natural_language, natural_language_2p ...
        elif "natural_language_2p" in low and "from rel_types" in low:
            self._result = list(self._nesting_rels)
        # _detect_structural_correction: SELECT taxonomy_name, member_taxonomies FROM entity_taxonomies
        elif "select taxonomy_name" in low and "member_taxonomies" in low and "from entity_taxonomies" in low:
            self._result = [(n, list(mt)) for n, mt in self._taxonomies]
        # engine guard: SELECT %s = ANY(... severed_taxonomies ...) FROM entity_taxonomies WHERE taxonomy_name
        elif "severed_taxonomies" in low and low.startswith("select"):
            child = params[0]
            parent = params[1]
            self._result = [(child in self._severed.get(parent, []),)]
        # UPDATE statements
        elif low.startswith("update entity_taxonomies"):
            self.rowcount = 1
            self._result = None
        else:
            self._result = []

    def fetchall(self):
        return list(self._result or [])

    def fetchone(self):
        if self._result:
            return self._result[0]
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def _make_conn(*, family_nests_pets=True, severed=None):
    cur = FakeCursor(
        taxonomies=[
            ("family", ["pets"] if family_nests_pets else []),
            ("pets", ["animal"]),
            ("animal", []),
        ],
        nesting_rels=[
            ("part_of", "is part of", "are part of"),
            ("member_of", "is a member of", "are a member of"),
        ],
        classification_rels={"instance_of", "subclass_of", "is_a"},
        severed=severed,
    )
    return FakeConn(cur), cur


# ── Detection: structural vs normal correction ───────────────────────────────
def test_detect_structural_correction_groups_nesting_negation():
    conn, _ = _make_conn()
    det = m._detect_structural_correction("my pets are not part of my family", conn)
    assert det is not None
    assert det["parent"] == "family"
    assert det["child"] == "pets"
    assert det["rel"] == "part_of"


def test_detect_subject_agnostic_other_anchor():
    # "Mara's pets aren't her family" — same op, different anchor.
    conn, _ = _make_conn()
    det = m._detect_structural_correction("Mara's pets are not part of her family", conn)
    assert det is not None
    assert det["parent"] == "family"
    assert det["child"] == "pets"


def test_detect_not_structural_when_not_negated():
    # A positive nesting statement is NOT a sever.
    conn, _ = _make_conn()
    det = m._detect_structural_correction("my pets are part of my family", conn)
    assert det is None


def test_detect_not_structural_when_only_one_group():
    # Only one taxonomy present → normal correction, not structural.
    conn, _ = _make_conn()
    det = m._detect_structural_correction("my pets are not blue", conn)
    assert det is None


def test_detect_not_structural_when_no_nesting_rel():
    # Two groups but no membership/nesting rel referenced → not structural.
    conn, _ = _make_conn()
    det = m._detect_structural_correction("my family is not my pets", conn)
    assert det is None


def test_detect_normal_fact_correction_unaffected():
    # "Luna is a dog not a bunny" — a leaf fact correction, NOT a group sever.
    conn, _ = _make_conn()
    det = m._detect_structural_correction("Luna is a dog not a bunny", conn)
    assert det is None


# ── Act: member_taxonomies sever + lock ──────────────────────────────────────
def test_apply_structural_correction_severs_and_locks():
    conn, cur = _make_conn()
    ok = m._apply_structural_correction(
        {"parent": "family", "child": "pets", "rel": "part_of"},
        conn, "faultline_test")
    assert ok is True
    assert conn.committed is True
    # The UPDATE must remove from member_taxonomies, add to severed_taxonomies, lock source.
    upd = [s for s, p in cur.executed
           if " ".join(s.split()).lower().startswith("update entity_taxonomies")][-1]
    low = " ".join(upd.split()).lower()
    assert "array_remove" in low and "member_taxonomies" in low
    assert "severed_taxonomies" in low
    assert "source = 'user_corrected'" in low


def test_apply_uses_no_public_qualifier():
    # Per-tenant: the UPDATE must NOT reference public.entity_taxonomies.
    conn, cur = _make_conn()
    m._apply_structural_correction(
        {"parent": "family", "child": "pets", "rel": "part_of"}, conn, "s")
    for s, _ in cur.executed:
        assert "public." not in s.lower()


# ── Engine guard: can NEVER re-link a user-severed pair ──────────────────────
def test_engine_refuses_to_relink_user_severed():
    conn, cur = _make_conn(family_nests_pets=False, severed={"family": ["pets"]})
    added = e.add_member_taxonomy_if_not_severed(cur, "family", "pets")
    assert added is False  # user severed it → engine refuses


def test_engine_may_link_when_not_severed():
    conn, cur = _make_conn(family_nests_pets=False, severed={})
    added = e.add_member_taxonomy_if_not_severed(cur, "family", "pets")
    assert added is True


def test_taxonomy_link_is_severed_reports_lock():
    conn, cur = _make_conn(severed={"family": ["pets"]})
    assert e.taxonomy_link_is_severed(cur, "family", "pets") is True
    assert e.taxonomy_link_is_severed(cur, "family", "cars") is False


def test_engine_guard_fails_closed_on_error():
    # A cursor that raises on the severed-check must fail CLOSED (treat as severed).
    class Boom:
        def execute(self, *a, **k):
            raise RuntimeError("db down")
        def fetchone(self):
            return None
    assert e.taxonomy_link_is_severed(Boom(), "family", "pets") is True
