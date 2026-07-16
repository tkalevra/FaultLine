"""RELATIONAL ASPECT-SYNONYM GROWTH — the relationship twin of ASPECT-SYNONYM GROWTH.

The scalar engine (test_aspect_synonym_growth.py) grows a query-aspect word onto one of the
anchor's SCALAR attributes on a miss ("tall" -> height). Its blind spot: RELATIONAL aspects.
"where have I traveled" resolves NOTHING when the aspect verb "traveled" is neither the name
nor a template word of any relationship rel the anchor holds (e.g. `visit`), no taxonomy name,
and not a scalar attribute -> scope empty -> fetch-all -> whole-profile dump.

FIX (same growth engine, relationship variant): on the MISS, determine_path unions the
anchor's scalar attrs with the relationship rels it ACTUALLY HOLDS (facts u staged, subject or
object side) and asks the tenant brain ONCE "does '<aspect word>' refer to ONE of these or
NONE?". A confident relationship hit GROWS a per-tenant rel_type_aliases row (traveled ->
visit) AND resolves THIS query in-place by admitting the SINGLE rel to relationship_rels. Every
future query reads that alias via the EXISTING keyword->rel_type alias lane -> deterministic,
NO model call, routed to relationship_rels by tail_types.

THE LEAK BOUND (crux): the relational candidate set is ONLY relationship rels the anchor holds
(RELSCOPE grounding), the mapper picks ONE or NONE (validated in-list), and we admit that
single rel -- NEVER a taxonomy/group. So admitting `works_for` cannot drag in spouse/lives_in.

REAL CURSOR: mock-drift has produced false greens on this family, so these run determine_path
against the LIVE local Postgres tenant. Reuses the disposable tenant seeded by the scalar test
(height/age/weight + lives_in Toronto + works_for Acme Corp -- the latter two are the
relationship rels this twin maps onto).
"""

import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")
import src.api.main as main


# Same disposable tenant as the scalar test.
_USER = "aa11bb22-cc33-4d44-8e55-ff6677889900"
_SCHEMA = "faultline_aa11bb22_cc33_4d44_8e55_ff6677889900"
_DSN = os.environ.get(
    "POSTGRES_DSN",
    "postgresql://faultline:faultline@172.20.0.2:5432/faultline",
)

# A relationship rel the tenant is expected to HOLD (from the seed: works_for Acme Corp).
_REL = "works_for"


@pytest.fixture(scope="module")
def db():
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute(f"SET search_path TO {_SCHEMA}")
        c.execute(
            "SELECT DISTINCT rel_type FROM facts "
            " WHERE (subject_id = %s OR object_id = %s) "
            "   AND superseded_at IS NULL AND archived_at IS NULL AND deleted_at IS NULL "
            "UNION SELECT DISTINCT rel_type FROM staged_facts "
            " WHERE subject_id = %s OR object_id = %s",
            (_USER, _USER, _USER, _USER),
        )
        rels = {r[0] for r in c.fetchall()}
    if _REL not in rels:
        pytest.skip(f"disposable tenant not seeded with relationship rel {_REL!r}; held={sorted(rels)}")
    yield conn
    conn.close()


def _bind(db):
    with db.cursor() as c:
        c.execute(f"SET search_path TO {_SCHEMA}")


def _dp(db, query):
    return main.determine_path(
        query, db, user_id=_USER,
        anchor_is_concrete_entity=False, anchor_uuid=None,
        anchor_resolved_uuid=_USER,
    )


def _set_alias(db, alias, canonical):
    with db.cursor() as c:
        c.execute(f"SET search_path TO {_SCHEMA}")
        c.execute(
            "INSERT INTO rel_type_aliases (alias, canonical_rel_type, requires_inversion, source)"
            " VALUES (%s, %s, false, 'engine') ON CONFLICT (alias) DO NOTHING",
            (alias, canonical),
        )


def _del_alias(db, alias):
    with db.cursor() as c:
        c.execute(f"SET search_path TO {_SCHEMA}")
        c.execute("DELETE FROM rel_type_aliases WHERE alias = %s", (alias,))


def _has_alias(db, alias):
    with db.cursor() as c:
        c.execute(f"SET search_path TO {_SCHEMA}")
        c.execute("SELECT canonical_rel_type FROM rel_type_aliases WHERE alias = %s", (alias,))
        r = c.fetchone()
    return r[0] if r else None


# -- CANDIDATE BOUNDING (the leak bound) ---------------------------------------------------
def test_candidate_rels_bounded_to_anchor_held_relationship_rels(db):
    """The relational candidate set is ONLY relationship rels the anchor ACTUALLY HOLDS;
    SCALAR attrs (height/age/weight) are excluded (they are the scalar lane's)."""
    _bind(db)
    with db.cursor() as c:
        c.execute(f"SET search_path TO {_SCHEMA}")
        cands = {r for r, _ in main._aspect_candidate_rels(c, _USER)}
    assert _REL in cands, cands
    # Scalars never appear in the relational candidate set.
    assert not ({"height", "age", "weight"} & cands), cands


# -- CONSUMPTION (deterministic, model-free) -----------------------------------------------
def test_grown_alias_routes_to_relationship_rels(db):
    """Steady state: a grown employer->works_for alias resolves via the keyword->rel_type
    alias lane -- NO model call -- and lands in relationship_rels (routed by tail_types),
    NOT scalar_rels; scope is active and NOT a fetch-all dump."""
    _set_alias(db, "employer", _REL)
    try:
        _bind(db)
        path = _dp(db, "who is my employer")
        assert _REL in path.relationship_rels, path.relationship_rels
        assert _REL not in path.scalar_rels
        assert path.scope_active is True
        assert path.fetch_all_details is False, "must NOT collapse to a fetch-all dump"
        # Deterministic path did NOT invoke the grow engine (alias already there).
        assert path.aspect_grown is None
    finally:
        _del_alias(db, "employer")


# -- INLINE GROW WIRING (fresh miss -> grow + resolve now, relational lane) -----------------
def test_inline_miss_grows_and_routes_relational(db, monkeypatch):
    """On a FRESH miss, a confident map to a RELATIONSHIP rel GROWS the per-tenant alias AND
    resolves THIS query in-place, routing the rel to relationship_rels (not scalar_rels).
    Mapper monkeypatched for determinism; the live-LLM map is validated end-to-end."""
    _del_alias(db, "employer")
    monkeypatch.setattr(main, "get_gliner_model", lambda: None)  # infra-free
    monkeypatch.setattr(main, "_aspect_map_via_llm", lambda word, cands: (_REL, 0.95, True))
    try:
        _bind(db)
        path = _dp(db, "who is my employer")
        assert _REL in path.relationship_rels, path.relationship_rels
        assert _REL not in path.scalar_rels, "a relationship rel must NOT land in the scalar lane"
        assert path.aspect_grown == _REL
        assert path.scope_active is True
        assert _has_alias(db, "employer") == _REL, "must GROW the per-tenant link"
    finally:
        _del_alias(db, "employer")


def test_inline_relational_candidate_is_grounded_to_anchor(db, monkeypatch):
    """The mapper only ever SEES rels the anchor holds. A relationship the anchor does NOT
    hold ('spouse') is not even a candidate, so a (hypothetical) model naming it is rejected
    in-list -> nothing grown -> no cross-group leak."""
    _del_alias(db, "married")
    monkeypatch.setattr(main, "get_gliner_model", lambda: None)
    # The real bound lives in _aspect_map_via_llm's in-list validation: feed a raw client
    # answer naming an off-list rel the anchor does not hold.
    monkeypatch.setattr(
        main, "call_llm_no_retry_sync",
        lambda **kw: {"attribute": "spouse", "confidence": 0.99},
    )
    _bind(db)
    with db.cursor() as c:
        c.execute(f"SET search_path TO {_SCHEMA}")
        cands = main._aspect_candidate_attrs(c, _USER) + main._aspect_candidate_rels(c, _USER)
    names = {a for a, _ in cands}
    assert "spouse" not in names, "an unheld rel must never be a candidate"
    attr, conf, called = main._aspect_map_via_llm("married", cands)
    assert attr is None, "off-list (unheld) rel must be rejected -> no invented link"
    assert called is True


# -- GUARDRAIL (bounding -- no invented links, no sprawl) -----------------------------------
def test_inline_miss_none_answer_grows_nothing(db, monkeypatch):
    """A NONE map on a fresh relational miss grows no alias and leaves scope broad (no
    sprawl) -- the query falls back to today's broad behavior."""
    _del_alias(db, "commuted")
    monkeypatch.setattr(main, "get_gliner_model", lambda: None)
    monkeypatch.setattr(main, "_aspect_map_via_llm", lambda word, cands: (None, 1.0, True))
    monkeypatch.setattr(main, "_aspect_record_miss", lambda *a, **k: None)
    _bind(db)
    path = _dp(db, "where did I commuted")
    assert path.aspect_grown is None
    assert _has_alias(db, "commuted") is None, "nothing may be grown for an unmappable word"
    assert not path.relationship_rels, path.relationship_rels
    assert not path.scalar_rels, path.scalar_rels
