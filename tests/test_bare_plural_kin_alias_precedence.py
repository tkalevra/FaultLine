"""BARE-PLURAL-KIN ALIAS PRECEDENCE — the "who are my children?" recall wound.

THE WOUND (prod seat 3f9c21ae, saas 5dd91ad0): capture was FIXED —
(marlow|dax|marina, child_of, user) + the three ages stored; migration 296
seeded the bare-plural kin aliases children/kids/sons/daughters -> parent_of into
public + every faultline_% schema — yet recall of "who are my children and how old
are they?" still rendered stale junk ("Daughter is 12 years old", "Code is 4408
years old"). Ground-truth prod log:

    determine_path  keywords=['they','children']
                    relationship_rels=['member_of','children']
                    scalar_rels=['age','children']

THREE root causes, all on the READ side of determine_path:

  (C) keyword loop: the UNION listed the exact rel_type-name arm FIRST, so a grown
      rel_type shell named `children` shadowed the seeded alias children->parent_of
      under LIMIT 1 — parent_of never landed in relationship_rels. (The suspected
      guards are innocent: the Phase 0 skip fires only when keywords is EMPTY, and
      _keyword_names_the_anchor returns False when the anchor IS the user — the loop
      ran and resolved the wrong row.) Fix: alias probe first, exact name second.
  (A) ATTRSCOPE admitted the junk stored attribute (user, children, 'three') by RAW
      NAME EQUALITY through a LEFT JOIN — any stored attribute qualified even with
      NO rel_types row behind it. The GROWTH lane already enforces the right bound
      (_aspect_candidate_attrs: rel_type-backed AND tail_types holds SCALAR); the
      READ side now obeys the SAME bound (JOIN + SCALAR). The compound head-token
      lane ('daily_commute' -> commute) and the natural_language lane ('old' ->
      age) operate on the rows the bound query returns, so captured-not-surfaced
      keeps working.
  (B) PRECEDENCE / the HARD LINE: places/typed rels outrank name-shaped residue.
      A word that resolves through rel_type_aliases to a NON-scalar rel must not
      ALSO admit the same-named attribute into scalar_rels, and the alias rel must
      land in relationship_rels AND aspect_bound_rels so the topic gate exempts the
      walked rows (topic_grounding_filtered dropped 8 -> 2 real family rows).

ROOT DESIGN RULE (owner-ratified): deterministic, metadata-driven, NO word lists,
NO per-domain literals — generalizes to all subjects. Fail-safe direction: on any
DB error the resolution is UNCHANGED (the alias probe and ATTRSCOPE sit inside
existing guarded lanes; a probe failure admits nothing new and revokes nothing).

REAL CURSOR: mock-drift has produced false greens on this family
(test_attr_scope_grounding docstring), so the DB-bound pins run determine_path
against the LIVE Postgres on a throwaway schema cloned structurally from public
(CREATE TABLE ... (LIKE public.%s INCLUDING ALL) — structure + uniques, no seed
rows, no FKs). Requires POSTGRES_DSN; SKIPS without one — disclosed, never a
silent pass. The structural pins at the bottom run dependency-free anywhere.
"""

import os
import uuid as _uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

_DSN = os.environ.get("POSTGRES_DSN", "")


# --------------------------------------------------------------------------- #
# 0. Structural pins — the fix shape itself, no DB required (seconds anywhere)
# --------------------------------------------------------------------------- #

def _dp_fallback(path, node):
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    return "".join(lines[node.lineno - 1:node.end_lineno])


def _determine_path_source():
    """Import-FREE extraction: the API module's dependency set (httpx/fastapi/spacy)
    is not installed on a code-only box, so pull determine_path's source straight out
    of the file text via ast — no import executes, runs in seconds anywhere."""
    import ast
    path = os.path.join(os.path.dirname(__file__), "..", "src", "api", "main.py")
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "determine_path":
            return _dp_fallback(path, node)
    pytest.fail("determine_path not found in src/api/main.py")


def _keyword_loop_slice(src):
    """The keyword->rel loop ONLY: from its for-statement to the ATTRSCOPE block,
    so the later alias probe (the (B) ATTRSCOPE lane, which ALSO reads
    rel_type_aliases) cannot confuse the ordering pin."""
    start = src.find("for keyword in keywords:")
    end = src.find("SCALAR-ASPECT ANCHOR GROUNDING", start)
    assert start != -1 and end != -1 and start < end
    return src[start:end]


def test_attrscope_uses_inner_join_with_scalar_bound_not_left_join():
    """(A): the ATTRSCOPE admission query must be rel_type-backed (JOIN) and
    SCALAR-bounded — the growth lane's own bound (_aspect_candidate_attrs)."""
    src = _determine_path_source()
    assert "FROM entity_attributes ea " in src and "JOIN rel_types rt ON rt.rel_type = ea.attribute " in src
    assert "LEFT JOIN rel_types rt ON rt.rel_type = ea.attribute" not in src
    assert "ILIKE '%%SCALAR%%'" in src


def test_keyword_loop_probes_the_alias_before_the_exact_name():
    """(C): the alias-resolved typed reading must be probed FIRST; the exact-name
    arm only runs when no alias matched (the grown `children` shell must never
    shadow children->parent_of again)."""
    src = _keyword_loop_slice(_determine_path_source())
    alias_at = src.find("FROM rel_type_aliases a")
    exact_at = src.find("WHERE rel_type = %s LIMIT 1")
    assert alias_at != -1 and exact_at != -1
    assert alias_at < exact_at


def test_attrscope_alias_nonscalar_probe_precedes_admission_and_seals():
    """(B): the alias-nonscalar probe runs BEFORE the attribute admission loop, and
    the precedence seal (a word that alias-resolves to a NON-scalar rel never admits
    the same-named attribute) is present inside the admission loop."""
    src = _determine_path_source()
    probe_at = src.find("_alias_nonscalar_words")
    seal_at = src.find("if _attr_lc in _alias_nonscalar_words:")
    loop_at = src.find("for _attr, _nl in _anchor_attr_rows:")
    assert probe_at != -1, "the alias-nonscalar probe is missing"
    assert loop_at != -1 and seal_at != -1
    assert probe_at < loop_at < seal_at


def test_component2_inverse_inherits_keyword_binding():
    """(4) downstream gap, determine_path side: COMPONENT 2 expands
    relationship_rels with the metadata-declared inverse (parent_of -> child_of),
    but the captured family rows are stored ON the inverse — (child, child_of,
    user) — so the Phase-5 lexical gate (which exempts only rels listed in
    aspect_bound_rels) would re-judge and drop exactly those rows. The declared
    inverse must inherit the keyword-binding exemption — and ONLY when the base
    rel is itself keyword-bound (plain inverse expansion gains nothing)."""
    src = _determine_path_source()
    _c2_at = src.find("COMPONENT 2")
    c2 = src[_c2_at:src.find("HOLLOW-SCOPE GUARD", _c2_at)]
    assert c2, "COMPONENT 2 block not found"
    assert "aspect_bound_rels.append(inverse_lower)" in c2
    # conditional: the inheritance fires only for a keyword-bound base rel
    assert "rel_type_lower in (path.aspect_bound_rels or [])" in c2


def test_alias_rel_lands_in_relationship_and_aspect_bound_rels():
    """(B): the probe must add the alias-resolved NON-scalar rel to BOTH
    relationship_rels (the walk reaches the family rows) AND aspect_bound_rels
    (the topic gate exempts them — topic_grounding_filtered dropped 8->2)."""
    src = _determine_path_source()
    probe = src[src.find("_alias_nonscalar_words"):src.find("(A) ATTRSCOPE admission")]
    assert probe and "path.relationship_rels.append" in probe
    assert "path.aspect_bound_rels.append" in probe


# --------------------------------------------------------------------------- #
# 1. DB pins — the wound and the captured-not-surfaced regressions
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def seat():
    if not _DSN:
        pytest.skip("POSTGRES_DSN not set — DB pins require the live stack (disclosed skip)")
    try:
        conn = psycopg2.connect(_DSN)
    except Exception as e:
        pytest.skip(f"Postgres unreachable ({e}) — DB pins require the live stack")
    conn.autocommit = True
    schema = "faultline_kinpin_" + _uuid.uuid4().hex[:8]
    user_id = str(_uuid.uuid5(_uuid.NAMESPACE_URL, schema + "/user"))
    with conn.cursor() as c:
        c.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
        c.execute(f'CREATE SCHEMA {schema}')
        c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        tables = [r[0] for r in c.fetchall()]
        for t in tables:
            c.execute(f'CREATE TABLE {schema}."{t}" (LIKE public."{t}" INCLUDING ALL)')
        c.execute(f'SET search_path TO {schema}')
        # ── the tenant metadata ──────────────────────────────────────────────
        # the typed kin rel + the migration-296 alias + the GROWN SHELL 'children'
        # (the prod shape: an engine-grown rel_type sharing the alias surface)
        for rel, tail in (("parent_of", "{ANY}"), ("age", "{SCALAR}"),
                          ("children", "{ANY}"),           # the name-shaped shell (C)
                          ("daily_commute", "{SCALAR}")):  # the compound scalar (regression)
            c.execute(
                "INSERT INTO rel_types (rel_type, label, tail_types, category, source,"
                " engine_generated) VALUES (%s, %s, %s, 'pending_placement', 'engine', true)"
                " ON CONFLICT DO NOTHING", (rel, rel, tail))
        c.execute(
            "INSERT INTO rel_type_aliases (canonical_rel_type, alias, source, confidence)"
            " VALUES ('parent_of', 'children', 'ontology', 1.0) ON CONFLICT DO NOTHING")
        # the anchor (user) entity + the stored rows
        # entities.user_id / entity_aliases.user_id are NOT NULL on prod — the
        # bare inserts failed the constraint in the container (fixture measured
        # on pre-prod); both columns are populated explicitly here.
        c.execute("INSERT INTO entities (id, user_id, entity_type) VALUES (%s, %s, 'Person')",
                  (user_id, user_id))
        c.execute("INSERT INTO entity_aliases (user_id, entity_id, alias, is_preferred)"
                  " VALUES (%s, %s, 'user', true)", (user_id, user_id))
        # THE WOUND: junk attribute 'children' — its rel_types backing is the
        # NON-scalar shell, and (shape A-plain) the attribute would previously be
        # admitted by raw name equality through the LEFT JOIN.
        c.execute(
            "INSERT INTO entity_attributes (user_id, entity_id, attribute, value_text, provenance)"
            " VALUES (%s, %s, 'children', 'three', 'user_stated')"
            " ON CONFLICT (user_id, entity_id, attribute) DO NOTHING", (user_id, user_id))
        # genuine scalars the anchor really holds
        c.execute(
            "INSERT INTO entity_attributes (user_id, entity_id, attribute, value_text, provenance)"
            " VALUES (%s, %s, 'age', '44 years', 'user_stated')"
            " ON CONFLICT (user_id, entity_id, attribute) DO NOTHING", (user_id, user_id))
        c.execute(
            "INSERT INTO entity_attributes (user_id, entity_id, attribute, value_text, provenance)"
            " VALUES (%s, %s, 'daily_commute', '45 minutes', 'user_stated')"
            " ON CONFLICT (user_id, entity_id, attribute) DO NOTHING", (user_id, user_id))
    yield {"conn": conn, "schema": schema, "user_id": user_id}
    with conn.cursor() as c:
        c.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
    conn.close()


def _dp(seat, monkeypatch, query):
    import src.api.main as main
    monkeypatch.setattr(main, "get_gliner_model", lambda: None)
    db = seat["conn"]
    with db.cursor() as c:
        c.execute(f'SET search_path TO {seat["schema"]}')
    return main.determine_path(
        query, db, user_id=seat["user_id"],
        anchor_is_concrete_entity=False,
        anchor_uuid=None,
        anchor_resolved_uuid=seat["user_id"],
    )


def test_wound_alias_rel_outranks_same_named_attribute(seat, monkeypatch):
    """THE WOUND PIN: attribute 'children' must NOT shadow the alias row
    children->parent_of. parent_of lands in relationship_rels AND
    aspect_bound_rels (topic-gate exemption); 'children' never enters scalar_rels;
    the age aspect still narrows."""
    path = _dp(seat, monkeypatch, "who are my children and how old are they?")
    assert "parent_of" in path.relationship_rels, (
        path.relationship_rels, path.scalar_rels)
    assert "parent_of" in (path.aspect_bound_rels or []), path.aspect_bound_rels
    assert "children" not in path.scalar_rels, path.scalar_rels
    assert "age" in path.scalar_rels, path.scalar_rels
    assert path.scope_active is True
    assert path.fetch_all_details is False


def test_wound_plain_shape_no_rel_types_backing_at_all(seat, monkeypatch):
    """Shape (A)-plain: the same wound where the junk attribute has NO rel_types row
    behind it at all (the LEFT JOIN admitted it by raw name equality). The JOIN +
    SCALAR bound must leave it inadmissible; the alias rel still binds."""
    import src.api.main as main
    # remove the grown shell so the attribute is purely unbacked
    with seat["conn"].cursor() as c:
        c.execute(f'SET search_path TO {seat["schema"]}')
        c.execute("DELETE FROM rel_types WHERE rel_type = 'children'")
    try:
        path = _dp(seat, monkeypatch, "who are my children and how old are they?")
        assert "parent_of" in path.relationship_rels, path.relationship_rels
        assert "children" not in path.scalar_rels, path.scalar_rels
        assert "age" in path.scalar_rels, path.scalar_rels
    finally:
        with seat["conn"].cursor() as c:
            c.execute(f'SET search_path TO {seat["schema"]}')
            c.execute(
                "INSERT INTO rel_types (rel_type, label, tail_types, category, source,"
                " engine_generated) VALUES ('children', 'children', '{ANY}',"
                " 'pending_placement', 'engine', true) ON CONFLICT DO NOTHING")


def test_regression_compound_attr_daily_commute_still_surfaces(seat, monkeypatch):
    """CAPTURED-NOT-SURFACED pin: the compound pure-NP scalar 'daily_commute'
    (rel_type-backed, SCALAR-tailed) must still be admitted via its HEAD TOKEN
    ('commute') — the (A) bound must not amputate the a2 lane."""
    path = _dp(seat, monkeypatch, "how long is my daily commute")
    assert "daily_commute" in path.scalar_rels, path.scalar_rels
    assert path.scope_active is True
    assert path.fetch_all_details is False


def test_regression_nl_template_age_still_surfaces(seat, monkeypatch):
    """CAPTURED-NOT-SURFACED pin: 'how old am I' still narrows to age (the
    natural_language admission lane rides on the same bound query)."""
    import src.api.main as main
    with seat["conn"].cursor() as c:
        c.execute(f'SET search_path TO {seat["schema"]}')
        c.execute("UPDATE rel_types SET natural_language = %s WHERE rel_type = 'age'",
                  ("X is Y years old",))
    path = _dp(seat, monkeypatch, "how old am I")
    assert "age" in path.scalar_rels, path.scalar_rels
    assert "children" not in path.scalar_rels, path.scalar_rels
    assert "daily_commute" not in path.scalar_rels, path.scalar_rels


def test_db_error_is_fail_safe_resolution_unchanged(seat, monkeypatch):
    """Fail-safe direction: when the alias probe's DB read fails, the resolution is
    UNCHANGED — no admission, no crash (the guarded lane logs and moves on)."""
    import src.api.main as main
    class _BoomCursor:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): raise RuntimeError("boom")
    class _BoomDB:
        def cursor(self): return _BoomCursor()
    # determine_path degrades to fetch-all on a dead DB — it must NOT raise and
    # must NOT half-admit: the legacy fail-safe shape (D1) is fetch_all_details.
    try:
        path = main.determine_path(
            "who are my children and how old are they?", _BoomDB(),
            user_id=seat["user_id"], anchor_is_concrete_entity=False,
            anchor_uuid=None, anchor_resolved_uuid=seat["user_id"])
        assert path.fetch_all_details is True or not path.scalar_rels
    except RuntimeError:
        pytest.fail("determine_path raised through the fail-safe boundary")


# --------------------------------------------------------------------------- #
# 2. ROUND-3 pins — the anchor killer (possessed head grounding a junk entity)
#    and the last unguarded read (entity_taxonomies exact-match loop)
# --------------------------------------------------------------------------- #

def _fn_source(fn_name):
    """Import-free source of ONE function from src/api/main.py (ast, no import —
    the API dep set is absent on a code-only box; same discipline as above)."""
    import ast
    path = os.path.join(os.path.dirname(__file__), "..", "src", "api", "main.py")
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            return "".join(lines[node.lineno - 1:node.end_lineno])
    pytest.fail(fn_name + " not found in src/api/main.py")


def test_resolve_anchor_declines_alias_rel_head_before_entity_probe():
    """The possessed-entity lane must consult the tenant's own rel_type_aliases
    BEFORE accepting an entity whose alias merely shares the possessed head's
    surface, and decline (log + stay on the speaker path) when the head resolves
    to a NON-scalar rel — the head names a SLOT the speaker owns, never a thing
    to anchor on. The guard must be gated on the grammatical first-person
    possessive flag, not a token list."""
    src = _fn_source("resolve_anchor")
    guard_at = src.find("resolve_anchor.alias_rel_head_stays_speaker")
    probe_at = src.find("resolve_anchor.possessive_entity")
    assert guard_at != -1, "the rel-alias head guard is missing from resolve_anchor"
    assert probe_at != -1 and guard_at < probe_at, "guard must precede the entity probe"
    assert "_head_aliases_to_rel" in src


def test_head_aliases_to_rel_helper_reads_tenant_alias_table():
    """The helper must probe rel_type_aliases JOIN rel_types on the SAME indexed
    equality the keyword loop uses (raw + normalized forms), and fail safe to
    None (guard silent, resolution unchanged) on any error."""
    src = _fn_source("_head_aliases_to_rel")
    assert "FROM rel_type_aliases" in src and "JOIN rel_types" in src
    assert "normalize_rel" in src
    assert "return None" in src


def test_third_party_scoping_declines_role_word_self_reference():
    """A first-person possessive whose head alias-resolves to a NON-scalar rel is
    the speaker's own slot, not a named third party — _third_party_answer_strays
    must decline third-party scoping (return the empty set) for it, BEFORE the
    reference-to-speaker fallback, and fail safe to scoping-unchanged on error."""
    src = _fn_source("_third_party_answer_strays")
    guard_at = src.find("ROLE-WORD SELF-REFERENCE IS NOT A NAMED THIRD PARTY")
    assert guard_at != -1, "the role-word self-reference guard is missing"
    assert "return set()" in src[guard_at:guard_at + 2400]
    assert "_head_aliases_to_rel" in src[guard_at:]


def test_taxonomy_exact_loop_is_fail_safe_per_keyword():
    """The entity_taxonomies exact-match loop is a per-keyword DB read inside a
    try whose only handler is 'except psycopg2.Error' — a non-psycopg2 failure
    (live-verified on pre-prod) escaped determine_path. Each keyword's probe must
    be guarded: log + continue, resolution unchanged, no raise."""
    src = _fn_source("determine_path")
    dbg_at = src.find("determine_path.taxonomy_exact_probe_failed")
    assert dbg_at != -1, "the per-keyword fail-safe log is missing"
    window = src[max(0, dbg_at - 800):dbg_at + 200]
    assert "continue" in window, "the guard must continue, not abort"
    assert 'WHERE taxonomy_name = %s LIMIT 1' in window, "the exact-match probe must be the guarded one"
    assert "except Exception" in window, "the guard must be per-keyword, not the tail handler"


# ---- DB-bound round-3 pins (live tenant; skip disclosed without DSN) -------

def test_round3_my_children_anchors_speaker_never_junk_grouping(seat, monkeypatch):
    """THE ANCHOR KILLER PIN: even with junk rows present in entity_aliases that
    share the possessed head's surface, 'who are my children?' must resolve the
    anchor to the SPEAKER (children->parent_of names the speaker's slot) via the
    default possessive path — never an alias/synonym grounding on the head."""
    import src.api.main as main
    monkeypatch.setattr(main, "get_gliner_model", lambda: None)
    resolution = {}
    anchor = main.resolve_anchor(
        "who are my children and how old are they?",
        [], seat["user_id"], seat["conn"], resolution_out=resolution)
    assert anchor == seat["user_id"], (anchor, resolution)
    assert resolution.get("method") not in ("alias", "synonym"), resolution


def test_round3_singular_my_mother_behaviour_unchanged(seat, monkeypatch):
    """REGRESSION BOUNDARY: a singular role noun WITHOUT an alias row ('mother')
    never enters the guard — "when is my mother's birthday?" keeps today's
    resolution (falls through the possessed lane to the speaker default)."""
    import src.api.main as main
    monkeypatch.setattr(main, "get_gliner_model", lambda: None)
    resolution = {}
    anchor = main.resolve_anchor(
        "when is my mother's birthday?",
        [], seat["user_id"], seat["conn"], resolution_out=resolution)
    assert anchor == seat["user_id"], (anchor, resolution)
    assert resolution.get("method") not in ("alias", "synonym"), resolution


def test_round3_junk_grouping_named_children_cannot_hijack(seat, monkeypatch):
    """Explicit hijack shape: a DEDICATED junk entity (not the speaker) aliased
    'children' exists and the alias row children->parent_of exists — the anchor
    must still be the speaker."""
    import src.api.main as main
    import uuid as _u2
    with seat["conn"].cursor() as c:
        c.execute('SET search_path TO ' + seat["schema"])
        junk = str(_u2.uuid4())
        c.execute("INSERT INTO entities (id, user_id, entity_type)"
                  " VALUES (%s, %s, 'Concept') ON CONFLICT DO NOTHING",
                  (junk, seat["user_id"]))
        c.execute("INSERT INTO entity_aliases (user_id, entity_id, alias, is_preferred)"
                  " VALUES (%s, %s, 'children', true) ON CONFLICT DO NOTHING",
                  (seat["user_id"], junk))
    monkeypatch.setattr(main, "get_gliner_model", lambda: None)
    anchor = main.resolve_anchor(
        "who are my children and how old are they?",
        [], seat["user_id"], seat["conn"])
    assert anchor == seat["user_id"], anchor
