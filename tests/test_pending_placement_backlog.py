"""Pins for the GROWN-REL PLACEMENT chain: the grouping mint, the eligibility trigger it
feeds, and the pending-placement backlog nothing drains.

Background (measured, not assumed — see the session report):
  99.2% of engine-grown rel_types sit at `category='pending_placement'`, and ZERO
  engine-minted rel has ever entered an `entity_taxonomies` grouping on any tenant. A rel
  stuck at that category is never appended to `rel_types_defining_group`, so the query
  path's `rel_type = ANY(allowed_rels)` projection can never admit it and every fact filed
  under it is INVISIBLE to a scoped recall.

Three independent defects are pinned here, each ablatable (revert the term → the named
test goes RED):

  A. `_mint_groupings_from_membership` derived `member_entity_types` from the MEMBERS only,
     never the GROUP OBJECT. Migration-122 INVARIANT 2 ("every CONCRETE head/tail type must
     be a member") then strips a defining rel with a concrete TAIL type — which is exactly
     `located_in`, 33 of the 39 permitted-class edges graph-wide. The mint annihilated the
     case migration 122 was written to unblock.
  B. `determine_path` treated a taxonomy row with an EMPTY defining group as
     indistinguishable from a missing row (`if row and row[0]`), so the single most
     informative state in the growth pipeline was silent.
  C. `drain_pending_placement_by_morphology` has produced `reconciled=0` since it shipped —
     its predicate (morphology-match a SEEDED canonical) is disjoint from the population it
     is fed (semantically novel predicates). "Ran and matched nothing" and "never ran" were
     byte-identical in the data; the backlog is now counted and CRIT-logged.

Subject-agnostic throughout: no assertion keys on a domain word. `located_in` appears only
because it is the rel whose SEEDED metadata carries a concrete tail type — the property
under test — and it is read from the DB, never asserted as a business rule.
"""
import importlib
import os

import pytest

main = importlib.import_module("src.api.main")

DSN = os.environ.get("POSTGRES_DSN", "")
_db = pytest.mark.skipif(not DSN, reason="POSTGRES_DSN unset — DB-bound pin cannot run")


# ══════════════════════════════════════════════════════════════════════════════════════
# A. the mint must put the GROUP OBJECT's own type into member_entity_types
# ══════════════════════════════════════════════════════════════════════════════════════
class _ParamAwareCursor:
    """Cursor whose entity_type result depends on the IDs actually queried.

    This is the whole point of the fixture: the pre-existing
    tests/test_grouping_automint.py fake returns a canned type list regardless of params,
    so it CANNOT observe whether the group object was included. Keying on the params is
    what makes defect A detectable.
    """

    def __init__(self, state):
        self.state = state
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        self.state["executed"].append((s, params))
        if "show search_path" in s:
            self._last = [("faultline_probe_tenant",)]
        elif "from rel_types" in s and "is_hierarchy_rel" in s:
            self._last = [(r,) for r in self.state["candidate_rels"]]
        elif "count(distinct subject_id)" in s and "group by" in s:
            self._last = self.state["comembership"]
        elif "from entity_aliases" in s and "where entity_id" in s:
            # (entity_id, alias) — the L3 bulk-write DISTINCT ON row shape
            self._last = [(self.state["group_id"], self.state["group_alias"])]
        elif "from entities" in s and "entity_type is not null" in s:
            # THE DISCRIMINATING BRANCH: resolve each queried id through the entity-type
            # table, so the result reflects exactly which ids the caller asked about.
            # Row shape (id, entity_type) — what the batch mint's `for _mid, _mt in`
            # loop unpacks since the L3 bulk-write rewrite.
            ids = list(params[0]) if params else []
            self.state["type_query_ids"] = ids
            self._last = sorted((i, self.state["entity_types"][i]) for i in ids
                                if self.state["entity_types"].get(i))
        elif "insert into entity_taxonomies" in s:
            self.state["inserts"].append(params)
            self._last = []
        else:
            self._last = []

    def fetchone(self):
        return self._last[0] if self._last else None

    def fetchall(self):
        return list(self._last or [])


class _ParamAwareDB:
    def __init__(self, state):
        self.state = state

    def cursor(self):
        return _ParamAwareCursor(self.state)

    def commit(self):
        pass

    def rollback(self):
        pass


def _mint_state():
    grp = "group-object-uuid"
    members = ["member-a", "member-b"]
    return {
        "candidate_rels": ["located_in"],
        "comembership": [(grp, "located_in", 2, members)],
        "group_alias": "Waterloo",
        # members are People; the GROUP OBJECT is a Location — the asymmetry that
        # INVARIANT 2 arbitrates on.
        "entity_types": {"member-a": "Person", "member-b": "Person", grp: "Location"},
        "executed": [], "inserts": [], "type_query_ids": None,
        "group_id": grp, "members": members,
    }


def test_mint_resolves_types_for_the_group_object_as_well_as_the_members(monkeypatch):
    """ABLATION PIN for defect A (the write side).

    Revert `(list(members) + [object_id],)` to `(list(members),)` in
    `_mint_groupings_from_membership` and this goes RED: the group object's id is no longer
    in the entity_type lookup, so its type can never reach member_entity_types.
    """
    st = _mint_state()
    monkeypatch.setattr(main, "_GROUPING_MINT_LAST_RUN", [0.0])  # disarm the cadence gate
    minted = main._mint_groupings_from_membership(_ParamAwareDB(st), "user-uuid",
                                                  min_members=2)
    assert minted == 1, "the co-membership row cleared the freq gate — a grouping is due"
    assert st["type_query_ids"] is not None, "the entity_type lookup never ran"
    assert st["group_id"] in st["type_query_ids"], (
        "the GROUP OBJECT was excluded from the entity_type lookup — its type can never "
        "reach member_entity_types, so a defining rel with a concrete tail type is "
        "guaranteed to be stripped by migration-122 INVARIANT 2 (hollow grouping)")
    for m in st["members"]:
        assert m in st["type_query_ids"], "members must still be resolved (no regression)"


def test_minted_member_entity_types_cover_both_sides_of_the_membership_edge(monkeypatch):
    """The grouping written must carry BOTH the member type and the group type.

    This is the SEEDED CONVENTION, verified against the live seed: every seeded
    (taxonomy, defining-rel) pair covers head AND tail — `household`/`pets` = {Person,
    Animal} for has_pet, `work` = {Person, Organization} for works_for. The engine mint was
    the only producer violating it.
    """
    st = _mint_state()
    monkeypatch.setattr(main, "_GROUPING_MINT_LAST_RUN", [0.0])  # disarm the cadence gate
    main._mint_groupings_from_membership(_ParamAwareDB(st), "user-uuid", min_members=2)
    assert st["inserts"], "no grouping was written"
    member_types = set(st["inserts"][0][2])
    assert "Person" in member_types, "the members' own type must be present"
    assert "Location" in member_types, (
        "the GROUP OBJECT's type is missing — the row violates the convention the seed "
        "obeys and migration-122 INVARIANT 2 assumes")


# ══════════════════════════════════════════════════════════════════════════════════════
# A (consequence). Drive the REAL migration-122 trigger and show the annihilation.
# ══════════════════════════════════════════════════════════════════════════════════════
def _a_tenant_schema(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT n.nspname FROM pg_namespace n"
            " JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = 'entity_taxonomies'"
            " JOIN pg_trigger t ON t.tgrelid = c.oid AND NOT t.tgisinternal"
            " WHERE n.nspname LIKE 'faultline\\_%'"
            " LIMIT 1")
        row = cur.fetchone()
    return row[0] if row else None


@_db
def test_invariant2_hollows_the_grouping_unless_the_group_type_is_a_member():
    """ABLATION PIN for defect A (the consequence side) — against the REAL trigger.

    Everything happens inside a transaction that is ROLLED BACK; no tenant data is
    modified. If this goes RED, migration 122's INVARIANT 2 changed — RE-MEASURE before
    touching it (a type gate on this codebase was previously deleted after corpus
    measurement showed it scoring 0 true vetoes and 9 false ones).
    """
    psycopg2 = pytest.importorskip("psycopg2")
    conn = psycopg2.connect(DSN)
    try:
        schema = _a_tenant_schema(conn)
        if not schema:
            pytest.skip("no local tenant schema carries the eligibility trigger")
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
            # The rel under test is chosen BY ITS METADATA (a non-classification hierarchy
            # rel with a concrete tail type), never by name.
            cur.execute(
                "SELECT rel_type, tail_types FROM rel_types"
                " WHERE is_hierarchy_rel = true"
                "   AND (wikidata_pid IS NULL OR wikidata_pid NOT IN ('P31','P279'))"
                "   AND tail_types IS NOT NULL"
                "   AND EXISTS (SELECT 1 FROM unnest(tail_types) t"
                "                WHERE lower(t) NOT IN ('any','scalar'))"
                " LIMIT 1")
            row = cur.fetchone()
            if not row:
                pytest.skip("this tenant seeds no concrete-tail membership rel")
            rel, tails = row[0], [t for t in row[1] if t.lower() not in ("any", "scalar")]
            group_type = tails[0]

            def _store(members):
                cur.execute("SAVEPOINT probe")
                cur.execute(
                    "INSERT INTO entity_taxonomies (taxonomy_name, description,"
                    " member_entity_types, rel_types_defining_group, has_transitivity,"
                    " transitive_rel_types, is_hierarchical, parent_rel_type, source)"
                    " VALUES ('zz_probe','probe',%s::text[],%s::text[],false,"
                    "         '{}'::text[],false,NULL,'engine_grown_ingest')",
                    (members, [rel]))
                cur.execute("SELECT rel_types_defining_group FROM entity_taxonomies"
                            " WHERE taxonomy_name = 'zz_probe'")
                got = cur.fetchone()[0]
                cur.execute("ROLLBACK TO SAVEPOINT probe")
                return list(got or [])

            members_only = _store(["Person"])
            with_group_type = _store(["Person", group_type])

        assert members_only == [], (
            f"expected INVARIANT 2 to strip '{rel}' when the group type "
            f"'{group_type}' is not a member — the trigger's behaviour changed")
        assert with_group_type == [rel], (
            f"'{rel}' must SURVIVE once the group object's own type '{group_type}' is a "
            f"member — this is what the mint fix supplies, and it is the difference "
            f"between a walkable grouping and a hollow one")
    finally:
        conn.rollback()
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════════════
# C. the undrainable pending backlog must be counted, not silent
# ══════════════════════════════════════════════════════════════════════════════════════
@_db
def test_drain_counts_the_pending_backlog_it_cannot_place():
    """ABLATION PIN for defect C.

    Remove the UNDRAINABLE BACKLOG probe from
    `drain_pending_placement_by_morphology` and this goes RED: `stats` carries no
    `unplaceable_*` keys, so "the fold matched nothing" stays indistinguishable from "the
    lane never ran".
    """
    psycopg2 = pytest.importorskip("psycopg2")
    embedder = importlib.import_module("src.re_embedder.embedder")
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT n.nspname, ("
                "  SELECT count(*) FROM pg_class c2 WHERE c2.relnamespace = n.oid"
                ") FROM pg_namespace n WHERE n.nspname LIKE 'faultline\\_%'")
            schemas = [r[0] for r in cur.fetchall()]
        target = None
        for s in schemas:
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{s}"')
                try:
                    cur.execute(
                        "SELECT count(*) FROM rel_types r WHERE r.category ="
                        " 'pending_placement' AND EXISTS ("
                        "   SELECT 1 FROM facts f WHERE f.rel_type = r.rel_type)")
                    if (cur.fetchone() or [0])[0] > 0:
                        target = s
                        break
                except Exception:
                    conn.rollback()
        if not target:
            pytest.skip("no local tenant has a pending rel carrying facts")

        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{target}"')
        conn.commit()
        stats = embedder.drain_pending_placement_by_morphology(conn, DSN, target)

        assert "unplaceable_rels" in stats and "unplaceable_facts" in stats, (
            "the drain reported no backlog — a lane that matched nothing is once again "
            "indistinguishable from a lane that never ran")
        assert stats["unplaceable_rels"] > 0
        assert stats["unplaceable_facts"] > 0, (
            "the backlog must be measured in USER FACTS made unreachable, not just rels")
    finally:
        conn.rollback()
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════════════
# B. a hollow grouping must FAIL LOUD, not read as "no such taxonomy"
# ══════════════════════════════════════════════════════════════════════════════════════
@_db
def test_hollow_defining_group_fails_loud_in_determine_path(monkeypatch):
    """ABLATION PIN for defect B.

    REAL CURSOR on purpose — this family false-greens under mocks (see the module
    docstring of tests/test_rel_scope_grounding.py). The probe taxonomy is inserted inside
    a transaction that is ROLLED BACK; no tenant data is modified.

    Remove the `if row is not None and not row[0]:` CRIT branch in `determine_path` and
    this goes RED. The assertion is deliberately about the LOG, not the QueryPath: the fix
    is observability-only and the resulting path must be byte-identical to before (pinned
    below), so the log is the only observable.
    """
    psycopg2 = pytest.importorskip("psycopg2")
    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    try:
        schema = _a_tenant_schema(conn)
        if not schema:
            pytest.skip("no local tenant schema available")
        user_id = schema[len("faultline_"):].replace("_", "-")
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
            # A hollow row: the trigger returns NEW untouched for an EMPTY defining group,
            # so this reproduces the post-strip state exactly.
            cur.execute(
                "INSERT INTO entity_taxonomies (taxonomy_name, description,"
                " member_entity_types, rel_types_defining_group, has_transitivity,"
                " transitive_rel_types, is_hierarchical, parent_rel_type, source)"
                " VALUES ('zzprobe','probe grouping','{}'::text[],'{}'::text[],false,"
                "         '{}'::text[],false,NULL,'engine_grown_ingest')"
                " ON CONFLICT (taxonomy_name) DO NOTHING")

        seen = []
        real_log_crit = main.log_crit
        monkeypatch.setattr(
            main, "log_crit",
            lambda lg, ev, **kw: (seen.append((ev, kw)), real_log_crit(lg, ev, **kw))[0])

        path = main.determine_path("tell me about my zzprobe", conn, user_id=user_id)

        events = [ev for ev, _ in seen]
        assert "determine_path.hollow_taxonomy_defining_group" in events, (
            "a taxonomy row EXISTS with an empty rel_types_defining_group and the walk "
            "said nothing — `if row and row[0]` made a MINTED-then-EMPTIED grouping "
            f"indistinguishable from a missing one. events={events}")
        payload = dict(next(kw for ev, kw in seen
                            if ev == "determine_path.hollow_taxonomy_defining_group"))
        assert payload.get("taxonomy") == "zzprobe", "the CRIT must name the hollow grouping"

        # CONTROL-FLOW INVARIANT: the fix is observability-only. A hollow grouping still
        # contributes no rels and still defers to the unscoped lane (the RC3 guard), exactly
        # as before. If this flips, the change stopped being observability-only.
        assert not path.scalar_rels and not path.relationship_rels
        assert path.fetch_all_details is True
    finally:
        conn.rollback()
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════════════
# D. the rel_type mint's `source` must satisfy the DB CHECK — or the ontology never grows
# ══════════════════════════════════════════════════════════════════════════════════════
import re as _re
import pathlib as _pathlib

_MAIN_PY = _pathlib.Path(main.__file__)


def _mint_call_site_sources() -> list[tuple[int, str]]:
    """Every literal `source=` passed to `_create_rel_type_in_flow`, with its line number.

    Static scan ON PURPOSE. The defect was a call-site ARGUMENT, not the callee: the mint
    itself is correct and simply propagates whatever it is handed. A behavioural test that
    calls the function directly with a good source would stay green with the bad call site
    still in place, so the pin has to read what the CALL SITES actually pass.
    """
    src = _MAIN_PY.read_text().splitlines()
    out = []
    for i, line in enumerate(src):
        if "_create_rel_type_in_flow(" in line and not line.lstrip().startswith("def "):
            # the call spans up to ~6 lines; scan forward to its source= kwarg
            for j in range(i, min(i + 8, len(src))):
                m = _re.search(r'source\s*=\s*["\']([a-z0-9_]+)["\']', src[j])
                if m:
                    out.append((i + 1, m.group(1)))
                    break
    return out


@_db
def test_every_rel_type_mint_call_site_passes_a_constraint_valid_source():
    """ABLATION PIN for defect D.

    Revert the in-flow mint to `source="llm_miss_inference"` and this goes RED.

    WHY IT MATTERS: `rel_types.source` carries a CHECK constraint. A non-conforming value
    makes the INSERT raise, the handler rolls back and returns False, and the branch never
    creates a rel_type — silently. That is what killed the LLM-metadata mint (the only one
    that supplies real head_types/tail_types), leaving every novel rel as an ANY/ANY stub
    and therefore unplaceable. Measured: 0 rows with a non-conforming source on any of the
    1,625 local tenant schemas.

    The allowed set is read from the LIVE constraint, never hardcoded here — if the schema
    widens the vocabulary, this pin follows it instead of going stale.
    """
    psycopg2 = pytest.importorskip("psycopg2")
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conname LIKE %s LIMIT 1", ("%rel_types_source%",))
            row = cur.fetchone()
        if not row:
            pytest.skip("rel_types_source_check not present on this database")
        allowed = set(_re.findall(r"'([a-z0-9_]+)'::text", row[0]))
        assert allowed, f"could not parse the CHECK definition: {row[0]!r}"

        sites = _mint_call_site_sources()
        assert sites, "found no _create_rel_type_in_flow call sites — the scan broke"
        bad = [(ln, s) for ln, s in sites if s not in allowed]
        assert not bad, (
            f"these mint call sites pass a `source` the DB CHECK rejects, so their INSERT "
            f"raises and the ontology silently does not grow: {bad}. Allowed: {sorted(allowed)}")
    finally:
        conn.rollback()
        conn.close()


@_db
def test_the_metadata_mint_actually_commits_and_lands_non_wildcard_types():
    """The consequence side of defect D, against a live tenant (transaction ROLLED BACK).

    The whole point of the LLM-metadata mint is a rel with REAL head/tail types instead of
    the ANY/ANY stub — that is what makes WGM validation meaningful and keeps the fact out
    of the Class-C graveyard. Pin that the write the call site now performs is accepted.
    """
    psycopg2 = pytest.importorskip("psycopg2")
    conn = psycopg2.connect(DSN)
    try:
        schema = _a_tenant_schema(conn)
        if not schema:
            pytest.skip("no local tenant schema available")
        sources = {s for _, s in _mint_call_site_sources()}
        assert sources, "no call-site sources found"
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
            for src_val in sorted(sources):
                cur.execute("SAVEPOINT probe")
                cur.execute(
                    "INSERT INTO rel_types (rel_type, label, confidence, source, category,"
                    " is_symmetric, is_hierarchy_rel, head_types, tail_types, fact_class,"
                    " engine_generated)"
                    " VALUES ('zz_src_probe','Zz Src Probe',0.8,%s,'pending_placement',"
                    "         false,false,ARRAY['Person'],ARRAY['Location'],'B',true)",
                    (src_val,))
                cur.execute("SELECT head_types, tail_types FROM rel_types"
                            " WHERE rel_type = 'zz_src_probe'")
                head, tail = cur.fetchone()
                cur.execute("ROLLBACK TO SAVEPOINT probe")
                assert head == ["Person"] and tail == ["Location"], (
                    f"source={src_val!r}: the mint did not land the supplied types")
    finally:
        conn.rollback()
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════════════
# E. TYPE-MATCH placement abstains by default (measured unsound over 6 coarse types)
# ══════════════════════════════════════════════════════════════════════════════════════
@_db
def test_type_match_placement_abstains_rather_than_misfiling(monkeypatch):
    """ABLATION PIN for defect E.

    Remove the `_taxonomy_type_match_placement_on()` gate from Pass 2 and this goes RED.

    Repairing the mint (defect D) makes real argument types reach Pass 2 for the first
    time. Measured on this corpus, Pass 2 then files `hike`/`wash`/`commute`/`earn`/`visit`
    ALL under whichever taxonomy contains `Person`. A rel mis-filed into a taxonomy's
    defining group projects into every scoped query for that grouping — a permanent
    correctness loss — whereas quarantine costs only recall. So the default abstains.

    The pin asserts BOTH directions: OFF abstains, ON still resolves. Asserting only the
    abstention would pass just as well against a Pass 2 that had been deleted outright.
    """
    psycopg2 = pytest.importorskip("psycopg2")
    from src.api import rel_type_overlay
    conn = psycopg2.connect(DSN)
    try:
        schema = _a_tenant_schema(conn)
        if not schema:
            pytest.skip("no local tenant schema available")
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute("SELECT count(*) FROM entity_taxonomies"
                        " WHERE 'Person' = ANY(member_entity_types)")
            if (cur.fetchone() or [0])[0] == 0:
                pytest.skip("tenant seeds no Person-typed taxonomy — nothing to mis-file into")
        conn.rollback()

        token = rel_type_overlay.set_current_schema(schema)
        try:
            monkeypatch.delenv("TAXONOMY_TYPE_MATCH_PLACEMENT", raising=False)
            abstained = main._resolve_category_via_taxonomy("zz_probe_verb",
                                                            ["Person"], ["Location"])
            assert abstained is None, (
                f"TYPE MATCH placed a coarse-typed rel into {abstained!r} by default — that "
                f"is the mis-filing the abstention exists to prevent")

            monkeypatch.setenv("TAXONOMY_TYPE_MATCH_PLACEMENT", "true")
            enabled = main._resolve_category_via_taxonomy("zz_probe_verb",
                                                          ["Person"], ["Location"])
            assert enabled is not None, (
                "with the flag ON, Pass 2 must still resolve — otherwise the gate is not a "
                "gate, the pass has simply been deleted")
        finally:
            rel_type_overlay.reset_current_schema(token)
    finally:
        conn.rollback()
        conn.close()
