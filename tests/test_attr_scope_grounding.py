"""ATTRSCOPE — user-attribute query scope resolution (real cursor).

Closes a determine_path gap for scalar-attribute questions about the anchor:

  • UNDER-SCOPE: "how much do I earn" / "what car do I have" returned
    "No relevant facts found" when the growth-minted attribute rel_type is not
    SCALAR-flagged (routes to relationship_rels) — the value lives in
    entity_attributes but the user-anchor scalar fetch projects WHERE
    attribute = ANY(scalar_rels), which never carried it.
  • OVER-SCOPE: "how old am I" resolved nothing ("old" is no rel_type NAME) →
    fetch_all → the whole profile dumped instead of narrowing to age.

FIX: SCALAR-ASPECT ANCHOR GROUNDING in determine_path admits an attribute into
scalar_rels when a query content-word matches the anchor's stored attribute NAME
(word-boundary) OR a whole content-word of that attribute's rel_type
natural_language template ("old" ∈ "X is Y years old" → age). Scalar-only
(no cross-group leak), narrowing (not widening — respects the aspect firewall),
deterministic, metadata-driven, subject-agnostic.

REAL CURSOR: mock-drift has produced false greens on this family, so these tests
run determine_path against the LIVE local Postgres tenant. Requires POSTGRES_DSN
pointing at the local stack's DB and a provisioned disposable tenant seeded with
the attributes below (self entity + earn/car/occupation/age).
"""

import os
import re
import uuid as _uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
import src.api.main as main


# Disposable tenant seeded by the harness setup (see module docstring / repro).
_USER = "bbbb1111-0002-4a02-8a02-b22222222222"
_SCHEMA = "faultline_bbbb1111_0002_4a02_8a02_b22222222222"
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
        # Guard: the seed must be present, else the test is meaningless (not a false green).
        c.execute("SELECT attribute FROM entity_attributes WHERE entity_id = %s", (_USER,))
        attrs = {r[0] for r in c.fetchall()}
    missing = {"earn", "car", "age", "occupation"} - attrs
    if missing:
        pytest.skip(f"disposable tenant not seeded (missing attrs: {sorted(missing)})")
    # Bind search_path for every cursor determine_path opens.
    conn.cursor().execute(f"SET search_path TO {_SCHEMA}")
    yield conn
    conn.close()


def _dp(db, query):
    # Anchor is the user (self) — the reported case is user-attribute questions.
    return main.determine_path(
        query, db, user_id=_USER,
        anchor_is_concrete_entity=False,
        anchor_uuid=None,
        anchor_resolved_uuid=_USER,
    )


def _bind(db):
    with db.cursor() as c:
        c.execute(f"SET search_path TO {_SCHEMA}")


def test_earn_under_scope_resolves_to_attribute(db):
    """UNDER-SCOPE: 'how much do I earn' narrows to the earn attribute even when
    the grown rel_type is not SCALAR-flagged (value lives in entity_attributes)."""
    _bind(db)
    path = _dp(db, "how much do I earn")
    assert "earn" in path.scalar_rels, path.scalar_rels
    assert path.scope_active is True
    assert path.fetch_all_details is False
    # Aspect-precise: does NOT drag in unrelated aspects.
    assert "age" not in path.scalar_rels
    assert "occupation" not in path.scalar_rels


def test_car_under_scope_resolves_to_attribute(db):
    """UNDER-SCOPE: 'what car do I have' narrows to the car attribute."""
    _bind(db)
    path = _dp(db, "what car do I have")
    assert "car" in path.scalar_rels, path.scalar_rels
    assert path.scope_active is True
    assert path.fetch_all_details is False
    assert "age" not in path.scalar_rels


def test_age_over_scope_narrows_not_dump(db):
    """OVER-SCOPE: 'how old am I' narrows to age via the rel_type natural_language
    template ('X is Y years old'), NOT a fetch-all profile dump."""
    _bind(db)
    path = _dp(db, "how old am I")
    assert "age" in path.scalar_rels, path.scalar_rels
    assert path.scope_active is True
    assert path.fetch_all_details is False, "must NOT collapse to fetch-all dump"
    # No cross-scope leak — earn/car/occupation must not ride along.
    assert "earn" not in path.scalar_rels
    assert "car" not in path.scalar_rels
    assert "occupation" not in path.scalar_rels


def test_broad_self_query_still_returns_profile(db, monkeypatch):
    """A genuinely-broad 'tell me about myself' must NOT be hijacked into a single
    scalar aspect — no attribute/template word matches, so scope stays broad."""
    # Neutralise the GLiNER2 taxonomy fallback (infra-free): a broad query resolves
    # no scalar aspect, so it must fall to the broad lane, not a narrowed one.
    monkeypatch.setattr(main, "get_gliner_model", lambda: None)
    _bind(db)
    path = _dp(db, "tell me about myself")
    # The scalar-aspect grounding must NOT have pinned a single attribute.
    assert not path.scalar_rels, path.scalar_rels
    assert not path.taxonomy_groups
    # Broad: scope is not active on a concrete aspect.
    assert path.scope_active is False


def test_subject_agnostic_no_attribute_literals(db):
    """The fix must resolve WHATEVER the anchor stored — proven by a word that only
    matches via the stored attribute name, with no in-code literal for it."""
    _bind(db)
    # 'occupation' is a seed SCALAR rel AND a stored attribute; the exact-name path
    # or the grounding path both resolve it. Either way it must narrow to occupation.
    path = _dp(db, "what is my occupation")
    assert "occupation" in path.scalar_rels, path.scalar_rels
    assert path.scope_active is True
    assert "earn" not in path.scalar_rels and "car" not in path.scalar_rels
