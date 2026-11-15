"""SATISFIABLE-BIRTH GROUPING MINT pins (growth-into-unknown, bug-05 successor).

MEASURED WOUND (fresh seat, eea12a11, pre-prod test DB): _mint_groupings_from_membership
wrote member_entity_types = the OBSERVED entity types only. When the defining rel's
own rel_types constraint carried a concrete type outside that set
("maker collective": Event members + an untyped group object, defining rel
located_in {ANY}->{Location}), enforce_defining_group_eligibility (INVARIANT 2
cross-type guard) STRIPPED the defining rel at write, the born-hollow row was
refused by the hollow-birth floor, and the co-membership facts attached to NO
walkable structure -- forever, since every future extension re-stripped the same
rel. Faceplant rate on the fresh seat: 1 of 3 groupings refused; determine_path
scoped 0 of 3 queries naming the grown groups.

THE FIX pins here: the mint unions the defining rels' CONCRETE constraint ends
(rel_types.head_types/tail_types, ANY/SCALAR exempt) into member_entity_types per
grouping BEFORE the write, so the cross-type guard passes and the grouping is born
CONNECTED. Deterministic, metadata-driven, subject-agnostic: no type is invented,
only ends the engine's own rel vocabulary declares for rels the tenant observed.
Observed member types keep their canonical case; the union is PER GROUPING (a
leaked cross-grouping constraint once widened unrelated groups -- pinned).
The hollow-birth floor stays as the backstop (see
tests/test_grouping_mint_hollow_floor.py); the existing automint contract pins
(tests/test_grouping_automint.py) keep guarding the un-grown path.

Scripted-fake-DB pins in the test_grouping_automint.py style (no live Postgres):
they lock the deterministic SQL contract -- the constraint probe reads rel_types,
and the INSERT params carry the unioned member set.
"""
import importlib

main = importlib.import_module("src.api.main")


class _FakeCursor:
    def __init__(self, script):
        self._script = script
        self.executed = []
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
        self.cursors = []
        self.committed = 0

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


GRP = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"


def _script(*, candidate_rels, comembership_rows, alias_rows, type_rows, rel_constraints):
    return [
        (_has("show search_path"), [("faultline_test_tenant",)]),
        (_has("from rel_types", "is_hierarchy_rel"),
         [(r,) for r in candidate_rels]),
        (_has("count(distinct subject_id)", "group by"), comembership_rows),
        (_has("from entity_aliases", "where entity_id"), alias_rows),
        (_has("from entities", "entity_type is not null"), type_rows),
        (_has("from rel_types", "head_types, tail_types"), rel_constraints),
        (_has("insert into entity_taxonomies"), []),
    ]


def _insert_params(db):
    # The mint writes ONE multi-row upsert via a single execute carrying a FLAT param
    # list -- ROW_WIDTH params per VALUES tuple (taxonomy_name, description,
    # member_entity_types, rel_types_defining_group). Chunk the flat list back into
    # per-row tuples; tolerate an executemany-style list of row tuples too.
    out = []
    for c in db.cursors:
        for (s, p) in c.executed:
            sql_flat = " ".join(s.split()).lower()
            if "insert into entity_taxonomies" not in sql_flat:
                continue
            if isinstance(p, list) and p and isinstance(p[0], (list, tuple)):
                out.extend(p)
                continue
            if not isinstance(p, (list, tuple)) or not p:
                continue
            # Row width from the SQL itself: each VALUES tuple carries exactly one
            # 'engine_grown_ingest' source literal (the mint's fixed row tail).
            nrows = max(1, sql_flat.count("engine_grown_ingest"))
            width = (len(p) // nrows) if len(p) % nrows == 0 else len(p)
            for i in range(0, len(p), width):
                out.append(tuple(p[i:i + width]))
    return out


def test_constraint_union_births_the_grouping_connected(monkeypatch):
    # located_in tails={Location}: the union must ADD 'location' to the member set so
    # the cross-type guard keeps the defining rel and the grouping is born connected
    # (pre-fix this mint was born hollow and REFUSED by the hollow-birth floor).
    members = ["e1", "e2"]
    db = _FakeDB(_script(
        candidate_rels=["member_of", "part_of", "located_in"],
        comembership_rows=[(GRP, "located_in", 2, members)],
        alias_rows=[(GRP, "maker collective")],
        type_rows=[("e1", "Event"), ("e2", "Event"), (GRP, "unknown")],
        rel_constraints=[("located_in", ["ANY"], ["Location"])],
    ))
    monkeypatch.setattr(main, "_GROUPING_MINT_LAST_RUN", [0.0])
    minted = main._mint_groupings_from_membership(db, "user-uuid", min_members=2)
    assert minted == 1
    params = _insert_params(db)
    assert len(params) == 1
    member_types = params[0][2]
    assert "event" in {m.lower() for m in member_types}
    assert "location" in {m.lower() for m in member_types}, (
        "the defining rel's constraint end must be unioned into member_entity_types "
        "so enforce_defining_group_eligibility keeps the rel (born connected, not hollow)")
    assert params[0][3] == ["located_in"]


def test_union_is_per_grouping_and_never_leaks(monkeypatch):
    # A member_of group (ANY/ANY constraints) must gain NOTHING from a sibling
    # located_in group's constraint ends, and vice versa.
    grp_a = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
    grp_b = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbb02"
    db = _FakeDB(_script(
        candidate_rels=["member_of", "part_of", "located_in"],
        comembership_rows=[
            (grp_a, "member_of", 2, ["m1", "m2"]),
            (grp_b, "located_in", 2, ["e1", "e2"]),
        ],
        alias_rows=[(grp_a, "astronomy club"), (grp_b, "maker collective")],
        type_rows=[("m1", "Person"), ("m2", "Person"), (grp_a, "Organization"),
                   ("e1", "Event"), ("e2", "Event"), (grp_b, "unknown")],
        rel_constraints=[("member_of", ["ANY"], ["ANY"]),
                         ("located_in", ["ANY"], ["Location"])],
    ))
    monkeypatch.setattr(main, "_GROUPING_MINT_LAST_RUN", [0.0])
    minted = main._mint_groupings_from_membership(db, "user-uuid", min_members=2)
    assert minted == 2
    by_name = {p[0]: p for p in _insert_params(db)}
    club_types = {m.lower() for m in by_name["astronomy club"][2]}
    coll_types = {m.lower() for m in by_name["maker collective"][2]}
    assert "location" not in club_types, (
        "the located_in constraint leaked into the member_of grouping")
    assert "location" in coll_types and "event" in coll_types
    assert club_types == {"person", "organization"}


def test_observed_member_types_keep_canonical_case(monkeypatch):
    # The automint contract pins ["Organization", "Person"] verbatim (canonical GLiNER2
    # case from entities.entity_type). Only ADDED constraint ends are lowercase.
    db = _FakeDB(_script(
        candidate_rels=["member_of", "part_of", "located_in"],
        comembership_rows=[(GRP, "member_of", 3, ["m1", "m2", "m3"])],
        alias_rows=[(GRP, "astronomy club")],
        type_rows=[("m1", "Person"), ("m2", "Person"), ("m3", "Person"),
                   (GRP, "Organization")],
        rel_constraints=[("member_of", ["ANY"], ["ANY"])],
    ))
    monkeypatch.setattr(main, "_GROUPING_MINT_LAST_RUN", [0.0])
    main._mint_groupings_from_membership(db, "user-uuid", min_members=2)
    params = _insert_params(db)
    assert params[0][2] == ["Organization", "Person"], params[0][2]
    assert params[0][3] == ["member_of"]


def test_any_scalar_constraints_add_nothing(monkeypatch):
    # ANY/SCALAR ends are exempt (the trigger's own exemption): a member_of grouping
    # must write exactly the observed member set -- byte-for-byte the automint output.
    db = _FakeDB(_script(
        candidate_rels=["member_of", "part_of", "located_in"],
        comembership_rows=[(GRP, "member_of", 3, ["m1", "m2", "m3"])],
        alias_rows=[(GRP, "astronomy club")],
        type_rows=[("m1", "Person"), ("m2", "Person"), ("m3", "Person"),
                   (GRP, "Organization")],
        rel_constraints=[("member_of", ["ANY", "scalar"], ["ANY"])],
    ))
    monkeypatch.setattr(main, "_GROUPING_MINT_LAST_RUN", [0.0])
    main._mint_groupings_from_membership(db, "user-uuid", min_members=2)
    params = _insert_params(db)
    assert params[0][2] == ["Organization", "Person"]
