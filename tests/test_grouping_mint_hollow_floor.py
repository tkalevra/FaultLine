"""HOLLOW-BIRTH FLOOR for the grouping mint (bug 05 fix 3) - MEASURED regression tests.

Measured on prod (tenant faultline_10d7d879, 2026-11-03): 61/600 entity_taxonomies rows
have an empty rel_types_defining_group, 57 of them source=engine_grown_ingest, dominated
by place heads (databricks, honolulu, austin-tx-usa ...) and fragments. Root cause: the
membership mint (_mint_groupings_from_membership) writes the observed candidate rel into
rel_types_defining_group, and the schema trigger enforce_defining_group_eligibility
(INVARIANT 2 cross-type guard) strips it AFTER the write when the rel's concrete
head/tail type is outside the group member set - the row is then BORN hollow and the
mint reports it as grown. The floor re-reads each write via RETURNING and refuses a
birth that came out with an empty defining array (DELETE in the same transaction).
Creation-time only: pre-existing hollow rows stay (surfaced by /structure/grown inert).

PURE tests - no DB, no network; scripted fake cursor per the house pattern
(tests/test_grouping_automint.py).

Run: python3 tools/fltest.py --bug GROUPFLOOR --test tests/test_grouping_mint_hollow_floor.py
     (tests/ is gitignored -> git add -f)
"""
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

    def fetchone(self):
        if not self._last:
            return None
        return self._last[0]

    def fetchall(self):
        return list(self._last) if self._last else []


class _FakeDB:
    def __init__(self, script):
        self._script = script
        self.committed = 0
        self.cursors = []

    def cursor(self):
        c = _FakeCursor(self._script)
        self.cursors.append(c)
        return c

    def commit(self):
        self.committed += 1

    def rollback(self):
        pass


def _has(*subs):
    def _m(sql):
        s = " ".join(sql.split()).lower()
        return all(x.lower() in s for x in subs)
    return _m


GRP = "11111111-1111-1111-1111-111111111111"


def _script(born_rows, candidate_rels=("member_of", "part_of")):
    return [
        (_has("show search_path"), [("faultline_test_tenant",)]),
        (_has("from rel_types", "is_hierarchy_rel"), [(r,) for r in candidate_rels]),
        (_has("count(distinct subject_id)", "group by"), [(GRP, "member_of", 3, ["aaa", "bbb", "ccc"])]),
        (_has("from entity_aliases", "where entity_id"), [(GRP, "Band")]),
        (_has("from entities", "entity_type is not null"), [("aaa", "Person"), ("bbb", "Person"), ("ccc", "Person"), (GRP, "Organization")]),
        (_has("insert into entity_taxonomies", "returning"), born_rows),
        (_has("delete from entity_taxonomies"), []),
    ]


def _run(born_rows, monkeypatch):
    monkeypatch.setattr(main, "_GROUPING_MINT_LAST_RUN", [0.0])
    db = _FakeDB(_script(born_rows))
    minted = main._mint_groupings_from_membership(db, "user-uuid", min_members=2,
                                                  _batch_edge_rel_types=["member_of"])
    return db, minted


def _deletes(db):
    out = []
    for c in db.cursors:
        for sql, params in c.executed:
            if "delete from entity_taxonomies" in " ".join(sql.split()).lower():
                out.append(params)
    return out



# ---- salvage lane ----

def test_salvage_refuses_hollow_birth():
    db = _FakeDB([
        (_has("insert into entity_taxonomies", "returning"), [("band", [], True)]),
        (_has("delete from entity_taxonomies"), []),
    ])
    n = main._mint_groupings_salvage(db, {"band": {"rels": {"member_of"}, "types": {"Person"}, "members": 2}})
    assert n == 0  # the birth was refused, not salvaged
    dels = _deletes(db)
    assert len(dels) == 1 and dels[0] == ("band",)


def test_salvage_keeps_healthy_birth():
    db = _FakeDB([
        (_has("insert into entity_taxonomies", "returning"), [("band", ["member_of"], True)]),
    ])
    n = main._mint_groupings_salvage(db, {"band": {"rels": {"member_of"}, "types": {"Person"}, "members": 2}})
    assert n == 1
    assert _deletes(db) == []


def test_salvage_floor_is_creation_time_only():
    # an EXTENDED pre-existing hollow row (born=False) is kept: the floor refuses only
    # rows BORN this statement - pre-existing hollows are surfaced via /structure/grown.
    db = _FakeDB([
        (_has("insert into entity_taxonomies", "returning"), [("band", [], False)]),
    ])
    n = main._mint_groupings_salvage(db, {"band": {"rels": {"member_of"}, "types": {"Person"}, "members": 2}})
    assert n == 1
    assert _deletes(db) == []


def test_salvage_insert_without_returning_rows_still_commits():
    # a driver whose fetchone returns None after INSERT (no RETURNING support in a
    # fake/test double) must not crash the salvage lane.
    db = _FakeDB([
        (_has("insert into entity_taxonomies", "returning"), []),
    ])
    n = main._mint_groupings_salvage(db, {"band": {"rels": {"member_of"}, "types": {"Person"}, "members": 2}})
    assert n == 1
    assert _deletes(db) == []



# ---- batch lane ----

def test_batch_refuses_hollow_birth(monkeypatch):
    db, minted = _run([("band", [], True)], monkeypatch)
    assert minted == 0
    dels = _deletes(db)
    assert len(dels) == 1
    assert list(dels[0][0]) == ["band"]  # taxonomy_name = ANY([...])


def test_batch_keeps_healthy_birth(monkeypatch):
    db, minted = _run([("band", ["member_of"], True)], monkeypatch)
    assert minted == 1
    assert _deletes(db) == []


def test_batch_extension_of_hollow_row_is_kept(monkeypatch):
    # ON CONFLICT extension (born=False) of a pre-existing hollow row: heal-or-keep,
    # never delete - the floor is creation-time only.
    db, minted = _run([("band", [], False)], monkeypatch)
    assert minted == 1
    assert _deletes(db) == []


GRP2 = "22222222-2222-2222-2222-222222222222"


def test_batch_counts_only_survivors(monkeypatch):
    # two births, one hollow: the reported minted count must say 1, not 2 - the
    # count previously lied by reporting every pending group as grown.
    monkeypatch.setattr(main, "_GROUPING_MINT_LAST_RUN", [0.0])
    script = [
        (_has("show search_path"), [("faultline_test_tenant",)]),
        (_has("from rel_types", "is_hierarchy_rel"), [("member_of",)]),
        (_has("count(distinct subject_id)", "group by"),
         [(GRP, "member_of", 3, ["aaa", "bbb", "ccc"]),
          (GRP2, "member_of", 3, ["ddd", "eee", "fff"])]),
        (_has("from entity_aliases", "where entity_id"),
         [(GRP, "Band"), (GRP2, "Crew")]),
        (_has("from entities", "entity_type is not null"),
         [("aaa", "Person"), ("ddd", "Person")]),
        (_has("insert into entity_taxonomies", "returning"),
         [("band", [], True), ("crew", ["member_of"], True)]),
        (_has("delete from entity_taxonomies"), []),
    ]
    db = _FakeDB(script)
    minted = main._mint_groupings_from_membership(db, "user-uuid", min_members=2,
                                                  _batch_edge_rel_types=["member_of"])
    assert minted == 1
    assert list(_deletes(db)[0][0]) == ["band"]


def test_floor_sql_is_returning_scoped(monkeypatch):
    # the INSERT must carry the RETURNING clause (with the (xmax = 0) birth column and
    # its 3-tuple arity - critic note: the scripted fakes hardcode the verdicts, so THIS
    # pin is what catches a PostgreSQL-semantics regression in the emitted SQL) and the
    # DELETE must be scoped to engine_grown_ingest source rows only - never a seed or a
    # user_corrected row.
    db, _ = _run([("band", [], True)], monkeypatch)
    inserts = [s for c in db.cursors for s, _p in c.executed
               if "insert into entity_taxonomies" in " ".join(s.split()).lower()]
    joined = [" ".join(s.split()).lower() for s in inserts]
    assert any("returning taxonomy_name" in s and "(xmax = 0)" in s for s in joined)
    for sql, _p in [(s, p) for c in db.cursors for (s, p) in c.executed
                    if "delete from entity_taxonomies" in " ".join(s.split()).lower()]:
        assert "engine_grown_ingest" in " ".join(sql.split()).lower()

class TestOtherBirthLanesFloored:
    """Re-adjudication census: the membership mint floor (the measured birth site)
    is pinned above; the census finds TWO MORE growth lanes that can mint a hollow
    grouping and must carry the same birth-veto:

    - the /expand lane (_seed_domain_operational_ontology): the mint condition used to
      fire on seeded_rel_types OR containment_rel - containment-without-rels births a
      grouping whose defining list is EMPTY (the axis is not the defining group),
      hollow with a tree attached.
    - the re_embedder document-lane discovery (engine_learned_re_embedder source):
      the growth-drain mirror declines hollow discoveries but this sibling site did not.

    Both are structural pins per the TestMintSeamWiring pattern (the lanes are LLM+DB
    heavy; the decision lines are the load-bearing surface)."""

    def _src(self, rel):
        from pathlib import Path
        p = Path(__file__).resolve().parents[1] / rel
        return p.read_text()

    def test_expand_lane_mint_is_defining_group_gated(self):
        s = self._src("src/api/main.py")
        # the guard: no operational rels -> declined, BEFORE the mint
        assert "learn_topic.hollow_taxonomy_declined" in s
        assert "refusing to mint a " in s  # the guard's note text
        # the mint condition no longer admits the containment-without-rels path:
        # the branch is gated on seeded_rel_types ALONE (elif taxonomy_name)
        assert "if not seeded_rel_types:" in s
        assert "(seeded_rel_types or containment_rel) and taxonomy_name" not in s

    def test_embedder_doc_lane_declines_hollow_discovery(self):
        s = self._src("src/re_embedder/embedder.py")
        # the growth-drain mirror (existing) AND the doc-lane sibling (new) both decline:
        assert s.count("taxonomy_hollow_declined") >= 2
        # the doc-lane neutralizes the discovery before the INSERT:
        assert "discovered = None  # nothing mintable" in s

    def test_birth_site_census_is_accounted(self):
        # every growth INSERT INTO entity_taxonomies site is floored, seeded, or the
        # membership mint (pinned above). schema_manager is provisioning (seed authority).
        import re
        pairs = [("src/api/main.py", 4), ("src/re_embedder/embedder.py", 2)]
        for rel, expected in pairs:
            n = len(re.findall(r"INSERT INTO entity_taxonomies", self._src(rel)))
            assert n == expected, (rel, n, expected)
