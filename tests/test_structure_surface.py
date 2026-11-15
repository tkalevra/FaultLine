"""THE ENGINE-GROWN STRUCTURE SURFACE — read the shelf, retire what is wrong.

WHY THIS EXISTS
    `d71e7d2e` ungated the two engine-structure frequency gates (`CUE_GROWTH_THRESHOLD`,
    `REL_TYPE_APPROVAL_THRESHOLD`, both 3 -> 1) on the owner ruling that engine-derived
    structure is usable ON DERIVATION because "the ability for users to correct that shape
    directly is the metric — we rely on SURFACING to the user to be able to correct if it's
    wrong."

    That ruling's second half did not exist. Measured before this change: `saas/` contains
    ZERO references to `linguistic_cues`; no MCP tool touched structure; `src/api/main.py`
    had no GET returning rel_types / taxonomies / cues / ontology state at all; the only
    structure-mutation route, `POST /ontology/rel_types`, writes `public` (the seed template)
    and is not per-tenant. So ungating without surfacing replaced "invisible and unused" with
    "invisible and USED".

WHAT IS PINNED HERE
  * READ — `/structure/grown` returns ENGINE-DERIVED structure only, and per-kind soft-fails
    (a tenant missing one structure table still sees the others).
  * ISOLATION — tenant A's read cannot see tenant B's structure, and A's correction does not
    touch B's. Proven against two REAL schemas, not mocked.
  * DEACTIVATION END TO END — the write flips `is_active`, and the change is then visible
    THROUGH THE RESOLVER (`resolve_social_role_map` -> `_resolve_keyed_map` ->
    `_fetch_keyed_map_with_suppressions`), including the case that motivated the suppression
    set: a deactivated row must NOT be resurrected from the in-code floor.
  * REVERSIBILITY — `reactivate` restores the prior resolution exactly.
  * THE HARD LINE — neither endpoint can read or write a memory row.
  * DEFAULTS pinned separately from behaviour, and every new term ablated.

DB-BOUND BY NECESSITY. The load-bearing claim is "the correction takes effect through the
resolver", and a monkeypatched fetch seam cannot prove that — it would pin the mock. These
tests create and drop their OWN throwaway schemas; they never touch a real tenant.
"""
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("POSTGRES_DSN"),
    reason="structure surface is DB-bound by design — set POSTGRES_DSN",
)

_CUES_DDL = """
CREATE TABLE {s}.linguistic_cues (
    id SERIAL PRIMARY KEY,
    cue VARCHAR(128) NOT NULL,
    category VARCHAR(64) NOT NULL DEFAULT 'naming_verb',
    frequency INT DEFAULT 0, confirmed_count INT DEFAULT 0, rejected_count INT DEFAULT 0,
    correction_count INT DEFAULT 0, global_confidence FLOAT DEFAULT 0.5,
    description TEXT, example_text TEXT, source VARCHAR(64),
    is_active BOOLEAN DEFAULT true, archived_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW(),
    last_matched_at TIMESTAMP,
    UNIQUE (cue, category)
);
"""

_TAX_DDL = """
CREATE TABLE {s}.entity_taxonomies (
    taxonomy_name TEXT PRIMARY KEY,
    transitive_rel_types TEXT[] DEFAULT '{{}}',
    member_taxonomies TEXT[] DEFAULT '{{}}',
    description TEXT,
    member_entity_types TEXT[],
    rel_types_defining_group TEXT[],
    source TEXT
);
"""


def _dsn():
    return os.environ["POSTGRES_DSN"]


class _Tenant:
    """A throwaway tenant schema carrying only the structure tables under test.

    Deliberately does NOT create `rel_types` / `staged_facts`: their absence is what proves
    the read's PER-KIND soft-fail (one missing table must not cost the whole payload).
    """

    def __init__(self, cues, taxonomies=()):
        import psycopg2
        from src.provisioning.schema_manager import derive_user_slug_from_uuid
        self.user_id = str(uuid.uuid4())
        self.schema = "faultline_" + derive_user_slug_from_uuid(self.user_id)
        self._conn = psycopg2.connect(_dsn())
        self._conn.autocommit = True
        with self._conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {self.schema}")
            cur.execute(_CUES_DDL.format(s=self.schema))
            cur.execute(_TAX_DDL.format(s=self.schema))
            for (c, cat, desc, src, active) in cues:
                cur.execute(
                    f"INSERT INTO {self.schema}.linguistic_cues "
                    "(cue, category, description, source, is_active) VALUES (%s,%s,%s,%s,%s)",
                    (c, cat, desc, src, active))
            for row in taxonomies:
                # 4-tuple carries an explicit rel_types_defining_group; the 3-tuple form is
                # kept so every existing exhibit reads unchanged (and lands HOLLOW, which is
                # what the pre-fix production population actually looked like).
                nm, members, src = row[0], row[1], row[2] if len(row) == 3 else row[3]
                defg = list(row[2]) if len(row) == 4 else []
                cur.execute(
                    f"INSERT INTO {self.schema}.entity_taxonomies "
                    "(taxonomy_name, member_entity_types, rel_types_defining_group, source) "
                    "VALUES (%s,%s,%s,%s)",
                    (nm, list(members), defg, src))

    def active(self, cue, category):
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT is_active FROM {self.schema}.linguistic_cues "
                        "WHERE cue=%s AND category=%s", (cue, category))
            row = cur.fetchone()
        return None if row is None else bool(row[0])

    def drop(self):
        try:
            with self._conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {self.schema} CASCADE")
        finally:
            self._conn.close()


@pytest.fixture
def two_tenants():
    """Tenant A and tenant B each hold a GROWN social_role cue. Only the cue STRING differs,
    so any leak between them is unambiguous."""
    a = _Tenant(
        cues=[("colleague", "social_role", "knows", "grown", True),
              ("mentor", "social_role", "knows", "seed_social_role", True)],
        taxonomies=[# 3-tuple => HOLLOW (empty defining group) — the pre-fix prod shape
                    ("living room", ["Animal"], "engine_grown_ingest"),
                    # 4-tuple => carries a defining rel, i.e. actually contributes scope
                    ("book club", ["Person"], ["member_of"], "engine_grown_ingest"),
                    ("family", ["Person"], "seeded")],
    )
    b = _Tenant(cues=[("shipmate", "social_role", "knows", "grown", True)])
    try:
        yield a, b
    finally:
        a.drop()
        b.drop()


# ── READ ────────────────────────────────────────────────────────────────────────────────

def test_read_returns_engine_derived_structure_only(two_tenants):
    from src.api import main as m
    a, _ = two_tenants
    out = m.structure_grown(user_id=a.user_id)
    cues = {c["cue"] for c in out["structure"]["cues"]}
    assert "colleague" in cues, out
    # A SEEDED, still-ACTIVE row is not engine growth and is not a pending correction. If it
    # showed, ~300 untouched seed rows would bury the handful that matter.
    assert "mentor" not in cues, cues
    groupings = {g["name"] for g in out["structure"]["groupings"]}
    assert "living room" in groupings
    assert "family" not in groupings, "the seeded backbone is not engine growth"


def test_the_mapping_a_cue_carries_is_surfaced_because_that_is_what_is_wrong(two_tenants):
    """'colleague' means KNOWS is precisely the claim a user would dispute ("a colleague
    isn't something I own"). Surfacing the cue without its mapping shows the word and hides
    the decision."""
    from src.api import main as m
    a, _ = two_tenants
    row = [c for c in m.structure_grown(user_id=a.user_id)["structure"]["cues"]
           if c["cue"] == "colleague"][0]
    assert row["maps_to"] == "knows", row
    assert row["category"] == "social_role"
    assert row["active"] is True


def test_a_missing_structure_table_costs_only_its_own_kind(two_tenants):
    """The throwaway schema has no `rel_types` / `staged_facts`. Those kinds come back empty
    and the other two still resolve — per-kind soft-fail, not payload-wide."""
    from src.api import main as m
    a, _ = two_tenants
    out = m.structure_grown(user_id=a.user_id)
    assert out["structure"]["rel_types"] == []
    assert out["structure"]["ladder"] == []
    assert out["structure"]["cues"] and out["structure"]["groupings"]


def test_correctable_names_only_the_kind_with_a_real_primitive(two_tenants):
    """`cues` is the ONE kind with a resolver-honoured correction primitive today. Claiming
    more would be advertising a door that does not open."""
    from src.api import main as m
    a, _ = two_tenants
    assert m.structure_grown(user_id=a.user_id)["correctable"] == ["cues"]


@pytest.mark.parametrize("uid", ["", "   ", "anonymous", "not-a-uuid"])
def test_an_unresolvable_user_gets_an_empty_payload_never_another_tenants(uid):
    from src.api import main as m
    out = m.structure_grown(user_id=uid)
    assert out["counts"] == {} or not any(out["counts"].values())
    assert all(v == [] for v in out["structure"].values()), out


def test_kind_filter_narrows_and_an_unknown_kind_is_refused_not_widened(two_tenants):
    from src.api import main as m
    a, _ = two_tenants
    only = m.structure_grown(user_id=a.user_id, kind="cues")
    assert only["structure"]["cues"] and only["structure"]["groupings"] == []
    # An unrecognised filter must NOT silently degrade to "everything".
    assert m.structure_grown(user_id=a.user_id, kind="../facts")["structure"]["cues"] == []


# ── ISOLATION ───────────────────────────────────────────────────────────────────────────

def test_one_tenant_can_never_see_anothers_structure(two_tenants):
    from src.api import main as m
    a, b = two_tenants
    a_cues = {c["cue"] for c in m.structure_grown(user_id=a.user_id)["structure"]["cues"]}
    b_cues = {c["cue"] for c in m.structure_grown(user_id=b.user_id)["structure"]["cues"]}
    assert a_cues == {"colleague"} and b_cues == {"shipmate"}
    assert not (a_cues & b_cues)


def test_a_correction_in_one_tenant_does_not_touch_another(two_tenants):
    """The same cue STRING in two tenants must be independently correctable. Uses a cue both
    hold so a cross-tenant UPDATE would be visible."""
    from src.api import main as m
    from src.api.models import DeactivateCueRequest
    a, b = two_tenants
    import psycopg2
    for t in (a, b):
        with psycopg2.connect(_dsn()) as c:
            c.autocommit = True
            with c.cursor() as cur:
                cur.execute(f"INSERT INTO {t.schema}.linguistic_cues "
                            "(cue, category, description, source, is_active) "
                            "VALUES ('shared','social_role','knows','grown',true)")
    res = m.structure_deactivate_cue(DeactivateCueRequest(
        user_id=a.user_id, cue="shared", category="social_role"))
    assert res["status"] == "ok" and res["changed"] == 1, res
    assert a.active("shared", "social_role") is False
    assert b.active("shared", "social_role") is True, "the UPDATE crossed a tenant boundary"


# ── DEACTIVATION, END TO END THROUGH THE RESOLVER ───────────────────────────────────────

def _resolved_social_roles(schema):
    """Resolve social_role exactly as the spine deriver does: through the ContextVar-bound
    overlay, floor union and suppression included. Caches are dropped first so the assertion
    reads the DB, not a warm 5s TTL entry."""
    from src.api import linguistic_cue_overlay as lco, rel_type_overlay
    lco.invalidate(schema)
    tok = rel_type_overlay.set_current_schema(schema)
    try:
        return dict(lco.resolve_social_role_map(_dsn()))
    finally:
        rel_type_overlay.reset_current_schema(tok)


def test_deactivation_is_visible_through_the_resolver_not_just_in_the_row(two_tenants):
    """THE LOAD-BEARING PIN. A write that flips a column but does not change what the spine
    RESOLVES is not a correction — it is a no-op with a success message."""
    from src.api import main as m
    from src.api.models import DeactivateCueRequest
    a, _ = two_tenants

    before = _resolved_social_roles(a.schema)
    assert before.get("colleague") == "knows", before

    res = m.structure_deactivate_cue(DeactivateCueRequest(
        user_id=a.user_id, cue="colleague", category="social_role"))
    assert res["status"] == "ok" and res["changed"] == 1, res

    after = _resolved_social_roles(a.schema)
    assert "colleague" not in after, (
        "the row flipped but the resolver still returns it — the correction did not take")


def test_the_in_code_floor_cannot_resurrect_a_deactivated_cue(two_tenants):
    """The regression the suppression set exists to prevent. `_resolve_keyed_map` unions an
    IN-CODE floor over the tenant rows, and that floor has no `is_active` column — so a naive
    union would hand back, from code, exactly the cue the user just switched off.

    Chosen deliberately: a cue that IS in the bootstrap floor. Deactivating a tenant-only row
    would pass even if suppression were removed, and would pin nothing.
    """
    from src.api import main as m, linguistic_cue_overlay as lco
    from src.api.models import DeactivateCueRequest
    a, _ = two_tenants
    floor = dict(lco._BOOTSTRAP_SOCIAL_ROLE_MAP)
    victim = next(iter(sorted(floor)))

    assert _resolved_social_roles(a.schema).get(victim) == floor[victim], \
        "the floor member is not resolving at all — this test would prove nothing"

    import psycopg2
    with psycopg2.connect(_dsn()) as c:
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute(f"INSERT INTO {a.schema}.linguistic_cues "
                        "(cue, category, description, source, is_active) "
                        "VALUES (%s,'social_role',%s,'seed_social_role',true)",
                        (victim, floor[victim]))
    m.structure_deactivate_cue(DeactivateCueRequest(
        user_id=a.user_id, cue=victim, category="social_role"))

    assert victim not in _resolved_social_roles(a.schema), (
        f"'{victim}' came back from the in-code floor after being deactivated — "
        "authority order (user > seed > growth) is broken")


def test_reactivate_restores_the_previous_resolution_exactly(two_tenants):
    from src.api import main as m
    from src.api.models import DeactivateCueRequest
    a, _ = two_tenants
    before = _resolved_social_roles(a.schema)
    m.structure_deactivate_cue(DeactivateCueRequest(
        user_id=a.user_id, cue="colleague", category="social_role"))
    assert "colleague" not in _resolved_social_roles(a.schema)
    res = m.structure_deactivate_cue(DeactivateCueRequest(
        user_id=a.user_id, cue="colleague", category="social_role", reactivate=True))
    assert res["status"] == "ok" and res["active"] is True, res
    assert _resolved_social_roles(a.schema) == before


# ── HONEST FAILURE SHAPES ───────────────────────────────────────────────────────────────

def test_an_unknown_cue_is_not_found_never_a_success_shape(two_tenants):
    from src.api import main as m
    from src.api.models import DeactivateCueRequest
    a, _ = two_tenants
    res = m.structure_deactivate_cue(DeactivateCueRequest(
        user_id=a.user_id, cue="no-such-cue", category="social_role"))
    assert res["status"] == "not_found" and res["changed"] == 0, res


def test_a_repeat_deactivation_reports_unchanged_not_ok(two_tenants):
    from src.api import main as m
    from src.api.models import DeactivateCueRequest
    a, _ = two_tenants
    req = DeactivateCueRequest(user_id=a.user_id, cue="colleague", category="social_role")
    assert m.structure_deactivate_cue(req)["status"] == "ok"
    second = m.structure_deactivate_cue(req)
    assert second["status"] == "unchanged" and second["changed"] == 0, second


@pytest.mark.parametrize("cue,category", [("", "social_role"), ("colleague", ""), ("", "")])
def test_a_half_specified_target_is_refused_rather_than_guessed(two_tenants, cue, category):
    """A partial target is exactly where a 'helpful' fuzzy match gets introduced later, and a
    fuzzy match on a structure edit is how it becomes a memory edit."""
    from src.api import main as m
    from src.api.models import DeactivateCueRequest
    a, _ = two_tenants
    res = m.structure_deactivate_cue(DeactivateCueRequest(
        user_id=a.user_id, cue=cue, category=category))
    assert res["status"] == "error" and res["changed"] == 0, res


# ── THE HARD LINE + DEFAULTS ────────────────────────────────────────────────────────────

def _sql_literals(fn):
    """Every string constant in `fn` that reaches the database, with COMMENTS AND THE
    DOCSTRING EXCLUDED.

    The first cut of the pin below grepped raw source and went red on the word 'facts'
    appearing inside a comment that says this lane cannot reach facts — i.e. it failed the
    code for DOCUMENTING the guarantee it was checking. A pin on what the code DOES must read
    the executable text, never the prose around it.
    """
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    body = tree.body[0].body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]          # drop the docstring
    out = []
    for node in body:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                out.append(sub.value)
    return " ".join(out)


def test_neither_endpoint_can_reach_a_memory_row():
    """THE HARD LINE, as a pin on the SQL the endpoints actually issue: this surface edits the
    PLACE, never the MEMORY. `staged_facts` appears once, in the READ, filtered to
    engine-derived ladder rungs; the WRITE names `linguistic_cues` and nothing else."""
    from src.api import main as m
    write_sql = _sql_literals(m.structure_deactivate_cue)
    assert "linguistic_cues" in write_sql, "the write pin is not reading the real SQL"
    for forbidden in ("facts", "entity_attributes", "entity_aliases", "episodic_log",
                      "entities"):
        assert forbidden not in write_sql, \
            f"the structure WRITE path issues SQL naming '{forbidden}' — only linguistic_cues"
    assert "DELETE" not in write_sql.upper(), \
        "cues have no DELETE path by design: a deleted seed row is resurrected by the next " \
        "migration boot (ON CONFLICT DO NOTHING protects an EDITED row, not a deleted one)"

    read_sql = _sql_literals(m.structure_grown)
    for forbidden in ("entity_attributes", "entity_aliases", "episodic_log"):
        assert forbidden not in read_sql, f"the structure READ issues SQL naming '{forbidden}'"
    # The ONLY memory-adjacent table the read may touch is staged_facts, and only for
    # engine-derived ladder rungs. A rung the USER stated is a MEMORY, not a shelf.
    assert "FROM facts" not in read_sql, "the read must not touch the authoritative facts table"
    assert "staged_facts" in read_sql and "user_stated" not in read_sql
    assert "llm_inferred" in read_sql and "llm_learned" in read_sql


def test_the_read_cap_default_is_pinned_separately_from_behaviour():
    from src.api import main as m
    assert m._STRUCTURE_READ_CAP == 50


def test_the_read_is_ordered_newest_first_so_the_cap_never_hides_fresh_growth():
    """What the engine JUST derived is both the most likely to be wrong and the thing the
    user is reacting to. Truncating THAT would defeat the surface."""
    import inspect
    from src.api import main as m
    src = inspect.getsource(m.structure_grown)
    assert "ORDER BY created_at DESC" in src, "cue read must be newest-first"
    assert "ORDER BY last_seen_at DESC" in src, "ladder read must be newest-first"


def test_a_hollow_grouping_is_visibly_INERT_not_indistinguishable_from_a_healthy_one(two_tenants):
    """A grouping with an EMPTY rel_types_defining_group contributes NO scope at
    determine_path -- every fact filed under it is projected out. The surface used to
    SELECT only (taxonomy_name, member_entity_types, source), so an inert row read
    IDENTICALLY to a working one and the user had nothing to correct against.

    Measured on production at 45f9ccb1: 17 of 24 engine-grown groupings are hollow, and the
    SEEDED `animal` row is hollow in the public template itself -- it fires
    `determine_path.hollow_taxonomy_defining_group` on a query as ordinary as "tell me about
    myself".

    This is the ruling's own requirement: surfacing IS the safety mechanism, so the surface
    must not hide the one field that decides whether the row does anything.
    """
    from src.api import main as m
    a, _ = two_tenants
    out = m.structure_grown(user_id=a.user_id)
    by = {g["name"]: g for g in out["structure"]["groupings"]}

    hollow = by["living room"]
    assert hollow["defining_rels"] == [], "the defining group must be surfaced, not hidden"
    assert hollow["inert"] is True, \
        "a grouping that can contribute no scope must SAY so; an empty array the reader has " \
        "to interpret is what let 17/24 hollow rows look healthy"

    healthy = by["book club"]
    assert healthy["defining_rels"] == ["member_of"]
    assert healthy["inert"] is False, "a grouping WITH a defining rel must not be flagged inert"


def test_a_LADDER_group_with_no_defining_rel_is_not_called_inert(two_tenants):
    """REGRESSION on my own fix. `inert` was first computed as `defining_rels == []`, which
    flags a healthy CLASSIFICATION group as broken.

    A taxonomy contributes scope by TWO routes (QueryPath contract, models.py):
    `rel_types_defining_group` -> allowed_rels, AND `transitive_rel_types` U the defining
    rels of `member_taxonomies` -> nesting_rels, re-admitted on the membership axis.

    The seeded `animal` row is the live proof: migration 087 gives it an EMPTY defining
    group DELIBERATELY ("the classification leaf group ... filled reactively"), transitive
    {subclass_of, instance_of}, and entry by nesting family > pets > animal. Nesting is live
    (member_taxonomies has real read sites), so the row works. Applying a relational-group
    predicate to a ladder group is the bug this pins.
    """
    import psycopg2
    from src.api import main as m
    a, _ = two_tenants
    with psycopg2.connect(_dsn()) as c:
        with c.cursor() as cur:
            cur.execute(
                f"INSERT INTO {a.schema}.entity_taxonomies "
                "(taxonomy_name, member_entity_types, rel_types_defining_group, "
                " transitive_rel_types, member_taxonomies, source) "
                "VALUES ('critter', '{Animal}', '{}', '{subclass_of,instance_of}', '{}', "
                "        'engine_grown_ingest')")
        c.commit()
    by = {g["name"]: g for g in m.structure_grown(user_id=a.user_id)["structure"]["groupings"]}
    ladder = by["critter"]
    assert ladder["defining_rels"] == []
    assert ladder["transitive_rels"] == ["subclass_of", "instance_of"]
    assert ladder["inert"] is False, \
        "a LADDER group contributes via nesting_rels; an empty defining group alone is not inert"
    # and a row with NOTHING on either route is still correctly called inert
    assert by["living room"]["inert"] is True
