"""Focused tests for engine-grown grouping auto-mint (subject-agnostic, deterministic).

The producer `_mint_groupings_from_membership` forms an entity_taxonomies grouping node
from OBSERVED membership edges at ingest. These tests drive it through a scripted fake
DB cursor (no live Postgres) to lock the deterministic, literal-free contract:

  • grouping name comes from the OBSERVED group object's alias — never a hardcoded map;
  • candidate defining rels = hierarchy rels MINUS type-classification rels (P31/P279);
  • the freq gate (>= min_members distinct members) is enforced in SQL (HAVING);
  • the write is per-tenant (refuses a non-`faultline_` search_path), source != 'seeded'.
"""
import re
import importlib

main = importlib.import_module("src.api.main")


class _FakeCursor:
    def __init__(self, script):
        self._script = script
        self.executed = []  # (sql, params)
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._last = None
        for matcher, result in self._script:
            if matcher(sql):
                self._last = result
                return
        self._last = None

    def fetchone(self):
        if self._last is None:
            return None
        return self._last[0] if self._last else None

    def fetchall(self):
        return list(self._last) if self._last else []


class _FakeDB:
    def __init__(self, script):
        self._script = script
        self.committed = 0
        self.rolled_back = 0
        self.cursors = []

    def cursor(self):
        c = _FakeCursor(self._script)
        self.cursors.append(c)
        return c

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


def _has(*subs):
    def _m(sql):
        s = " ".join(sql.split()).lower()
        return all(x.lower() in s for x in subs)
    return _m


def _build_script(*, candidate_rels, comembership_rows, alias_rows, type_rows):
    return [
        (_has("show search_path"), [("faultline_test_tenant",)]),
        (_has("from rel_types", "is_hierarchy_rel"),
         [(r,) for r in candidate_rels]),
        (_has("count(distinct subject_id)", "group by"), comembership_rows),
        (_has("from entity_aliases", "where entity_id"), alias_rows),
        (_has("from entities", "entity_type is not null"), type_rows),
        (_has("insert into entity_taxonomies"), []),
    ]


def test_mints_grouping_from_observed_membership(monkeypatch):
    # ORDER ISOLATION (r20): the mint stamps the module-global cadence clock whenever a
    # pass scans; an earlier suite file (test_ingest_bulkwrite_lanes) can open the gate
    # within GROUPING_MINT_CADENCE_S and leave LAST_RUN fresh, which would skip this
    # test's no-batch-rels mint in any non-canonical file order. Reset the clock so the
    # test asserts the MINT, not the process's import order.
    grp = "11111111-1111-1111-1111-111111111111"
    members = ["aaa", "bbb", "ccc"]
    db = _FakeDB(_build_script(
        candidate_rels=["member_of", "part_of", "located_in"],
        comembership_rows=[(grp, "member_of", 3, members)],
        alias_rows=[("11111111-1111-1111-1111-111111111111", "Band")],  # (entity_id, alias) — the L3 bulk-write DISTINCT ON shape
        type_rows=[("aaa", "Person"), ("bbb", "Person"), ("ccc", "Person"),
                   (grp, "Organization")],  # (entity_id, entity_type) — L3 batch shape
    ))
    monkeypatch.setattr(main, "_GROUPING_MINT_LAST_RUN", [0.0])  # r20: cadence due regardless of file order
    minted = main._mint_groupings_from_membership(db, "user-uuid", min_members=2)
    assert minted == 1
    inserts = [(s, p) for c in db.cursors for (s, p) in c.executed
               if "insert into entity_taxonomies" in " ".join(s.split()).lower()]
    assert len(inserts) == 1
    _, params = inserts[0]
    # taxonomy_name derived from the observed group alias, lowercased — not a literal map
    assert params[0] == "band"
    # member_entity_types: the union of BOTH sides of the membership edge
    assert params[2] == ["Organization", "Person"]
    assert params[3] == ["member_of"]
    # grown source, never seeded
    insert_sql = " ".join(inserts[0][0].split()).lower()
    assert "engine_grown_ingest" in insert_sql
    assert "source <> 'seeded'" in insert_sql  # seeds protected on conflict


def test_freq_gate_below_floor_mints_nothing(monkeypatch):
    # HAVING enforces the gate in SQL; an empty co-membership result → no mint.
    # ORDER ISOLATION (r21): without a clock reset, an earlier file's cadence stamp
    # (bulkwrite stamps _GROUPING_MINT_LAST_RUN directly) makes the pass return 0 at
    # the cadence gate BEFORE the co-membership scan - the pin would pass vacuously
    # (0 == 0 from the skip, never red, never exercising the HAVING it documents).
    monkeypatch.setattr(main, "_GROUPING_MINT_LAST_RUN", [0.0])
    db = _FakeDB(_build_script(
        candidate_rels=["member_of"],
        comembership_rows=[],            # nothing cleared the gate
        alias_rows=[("Band",)],
        type_rows=[("Person",)],
    ))
    assert main._mint_groupings_from_membership(db, "user-uuid", min_members=3) == 0


def test_refuses_non_tenant_search_path():
    # Per-tenant invariant: never grow into the public template.
    db = _FakeDB([
        (_has("show search_path"), [('"$user", public',)]),
    ])
    assert main._mint_groupings_from_membership(db, "user-uuid") == 0


def test_candidate_rel_query_excludes_classification_pids():
    db = _FakeDB([
        (_has("from rel_types", "is_hierarchy_rel"), [("member_of",), ("part_of",)]),
    ])
    rels = main._grouping_candidate_rels(db)
    assert rels == ["member_of", "part_of"]
    sql = " ".join(db.cursors[0].executed[0][0].split()).lower()
    # the filter is metadata-driven on the classification PIDs, not rel-name literals
    assert "wikidata_pid" in sql
    assert main._CLASSIFICATION_PIDS == ("P31", "P279")
    params = db.cursors[0].executed[0][1]
    assert list(params[0]) == ["P31", "P279"]


def test_no_hardcoded_grouping_name_literals_in_producer():
    """The producer must contain zero domain/grouping-name literals."""
    import inspect
    src = inspect.getsource(main._mint_groupings_from_membership)
    for banned in ("band", "team", "club", "fleet", "family", "household"):
        # allow the word only inside comments/docstring prose, not as a string literal
        assert not re.search(rf'["\']{banned}["\']', src), f"literal {banned!r} found"
