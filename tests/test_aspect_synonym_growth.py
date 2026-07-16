"""ASPECT-SYNONYM GROWTH — query-aspect resolution via the growth engine (real cursor).

Recall OVER-SCOPES ("how tall am I" dumps the whole profile) when the query's aspect word
is neither the attribute's NAME nor a word in its natural_language template — "tall" ∉
height's name and ∉ "X's height is Y". ATTRSCOPE then admits nothing → scope empty →
fetch-all → dump.

FIX (growth engine, NOT a hardcoded map / seed): on the MISS, determine_path asks the
tenant brain ONCE "does '<aspect word>' refer to ONE of {the anchor's ACTUAL scalar
attributes} or NONE?"; on a confident hit it GROWS a per-tenant rel_type_aliases row
(tall → height) AND resolves THIS query in-place. Every future query reads that alias via
the EXISTING keyword→rel_type alias lane — deterministic, NO model call (the dumb walk).

These tests exercise:
  • CONSUMPTION (deterministic, model-free): a grown tall→height alias narrows "how tall am
    I" to height, NOT a profile dump.
  • CANDIDATE BOUNDING: the mapper's candidate set is the anchor's OWN scalar rel_type attrs.
  • GUARDRAIL: an off-list / NONE answer grows nothing (no invented links, no sprawl).
  • INLINE GROW WIRING: on a fresh miss, a confident map both writes the alias and resolves
    the query (mapper monkeypatched for determinism; the LIVE-LLM map is validated e2e).

REAL CURSOR: mock-drift has produced false greens on this family, so these run
determine_path against the LIVE local Postgres tenant. Requires POSTGRES_DSN pointing at the
stack DB and a provisioned disposable tenant seeded with height/age/weight (+ lives_in,
works_for as non-scalar decoys).
"""

import os
import uuid as _uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
import src.api.main as main


# Disposable tenant seeded by the repro (My height is 6 feet 2 inches; age 41; weight 190;
# live in Toronto; work for Acme Corp).
_USER = "aa11bb22-cc33-4d44-8e55-ff6677889900"
_SCHEMA = "faultline_aa11bb22_cc33_4d44_8e55_ff6677889900"
_DSN = os.environ.get(
    "POSTGRES_DSN",
    "postgresql://faultline:faultline@172.20.0.2:5432/faultline",
)


@pytest.fixture(scope="module")
def db():
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute(f"SET search_path TO {_SCHEMA}")
        c.execute("SELECT attribute FROM entity_attributes WHERE entity_id = %s", (_USER,))
        attrs = {r[0] for r in c.fetchall()}
    missing = {"height", "age", "weight"} - attrs
    if missing:
        pytest.skip(f"disposable tenant not seeded (missing scalar attrs: {sorted(missing)})")
    conn.cursor().execute(f"SET search_path TO {_SCHEMA}")
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


# ── CANDIDATE BOUNDING ────────────────────────────────────────────────────────────────
def test_candidate_attrs_bounded_to_anchor_scalar_reltypes(db):
    """The mapper may only pick from the anchor's OWN scalar attributes that are real
    SCALAR rel_types — the valid alias-target set. Non-scalar rels (lives_in/works_for)
    are excluded (they are not FK-valid alias targets and belong to other lanes)."""
    _bind(db)
    with db.cursor() as c:
        c.execute(f"SET search_path TO {_SCHEMA}")
        cands = {a for a, _ in main._aspect_candidate_attrs(c, _USER)}
    assert {"height", "age", "weight"} <= cands, cands
    assert "lives_in" not in cands and "works_for" not in cands


# ── CONSUMPTION (deterministic, model-free) ─────────────────────────────────────────────
def test_grown_alias_narrows_how_tall_am_i(db):
    """Steady state: with tall→height grown, 'how tall am I' resolves height via the
    keyword→rel_type alias lane — NO model call — and narrows (no age/weight/profile dump)."""
    _set_alias(db, "tall", "height")
    try:
        _bind(db)
        path = _dp(db, "how tall am I")
        assert "height" in path.scalar_rels, path.scalar_rels
        assert path.scope_active is True
        assert path.fetch_all_details is False, "must NOT collapse to a fetch-all dump"
        # Aspect-precise: siblings must not ride along.
        assert "age" not in path.scalar_rels
        assert "weight" not in path.scalar_rels
        # Deterministic path did NOT invoke the grow engine (alias already there).
        assert path.aspect_grown is None
    finally:
        _del_alias(db, "tall")


# ── GUARDRAIL (bounding — no invented links) ────────────────────────────────────────────
def test_mapper_rejects_off_list_attribute(db, monkeypatch):
    """The brain can only map to an attribute the anchor HOLDS. An off-list answer
    ('salary' — not one of height/age/weight) is rejected → (None, conf, called_ok)."""
    monkeypatch.setattr(
        main, "call_llm_no_retry_sync",
        lambda **kw: {"attribute": "salary", "confidence": 0.99},
    )
    cands = [("height", "X's height is Y"), ("age", "X is Y years old"), ("weight", "X's weight is Y")]
    attr, conf, called = main._aspect_map_via_llm("tall", cands)
    assert attr is None, "off-list attribute must not be admitted (no invented link)"
    assert called is True  # brain answered → memoize path, not the async-backstop path


def test_mapper_accepts_on_list_attribute(db, monkeypatch):
    """A valid on-list answer is returned with its confidence for the gate."""
    monkeypatch.setattr(
        main, "call_llm_no_retry_sync",
        lambda **kw: {"attribute": "height", "confidence": 0.95},
    )
    cands = [("height", "X's height is Y"), ("age", "X is Y years old")]
    attr, conf, called = main._aspect_map_via_llm("tall", cands)
    assert attr == "height" and conf == 0.95 and called is True


# ── INLINE GROW WIRING (fresh miss → grow + resolve now) ────────────────────────────────
def test_inline_miss_grows_and_resolves(db, monkeypatch):
    """On a FRESH miss (no tall alias), a confident map GROWS the per-tenant alias AND
    resolves THIS query in-place (first query gets the real answer). Mapper monkeypatched
    for determinism — the live-LLM map is validated end-to-end separately."""
    _del_alias(db, "tall")
    monkeypatch.setattr(main, "get_gliner_model", lambda: None)  # infra-free
    monkeypatch.setattr(
        main, "_aspect_map_via_llm",
        lambda word, cands: ("height", 0.95, True),
    )
    try:
        _bind(db)
        path = _dp(db, "how tall am I")
        # Resolved THIS query.
        assert "height" in path.scalar_rels, path.scalar_rels
        assert path.aspect_grown == "height"
        assert path.scope_active is True
        # GREW the per-tenant link for every future (model-free) query.
        assert _has_alias(db, "tall") == "height"
    finally:
        _del_alias(db, "tall")


def test_inline_miss_none_answer_grows_nothing(db, monkeypatch):
    """Guardrail e2e: a NONE map on a fresh miss grows no alias and leaves scope broad
    (no sprawl) — the query falls back to today's broad behavior."""
    _del_alias(db, "spicy")
    monkeypatch.setattr(main, "get_gliner_model", lambda: None)
    monkeypatch.setattr(main, "_aspect_map_via_llm", lambda word, cands: (None, 1.0, True))
    # Do not actually write the left_unmapped memo to the shared tenant during the test.
    monkeypatch.setattr(main, "_aspect_record_miss", lambda *a, **k: None)
    _bind(db)
    path = _dp(db, "how spicy am I")
    assert path.aspect_grown is None
    assert _has_alias(db, "spicy") is None, "nothing may be grown for an unmappable word"
    assert not path.scalar_rels, path.scalar_rels
