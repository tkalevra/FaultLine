"""RELSCOPE — relational-aspect query scope resolution (real cursor).

Relationship twin of ATTRSCOPE. Closes a determine_path gap for a RELATIONSHIP
question about the anchor whose aspect verb is neither a SCALAR attribute nor an
exact rel_type NAME nor a taxonomy name:

  • "what did I switch to" resolved NOTHING — "switch" is no SCALAR attribute
    (ATTRSCOPE skips it), no exact rel_type NAME ("switch" ≠ the grown compound
    rel "switch_to"), no taxonomy name → scope_active False → fetch_all → the
    WHOLE profile (spouse, lives_in, works_for, …) dumped instead of narrowing to
    the switch target (user, switch_to, gcp).

FIX: RELATIONAL-ASPECT ANCHOR GROUNDING in determine_path admits a RELATIONSHIP
rel into relationship_rels ONLY when a query content-word names the rel (a
content token of the rel's OWN name — "switch_to" → {switch} — OR a content word
of its natural_language template) AND the ANCHOR actually holds a fact under that
rel (facts ∪ staged_facts, subject OR object side). Grounded + single-rel +
never-a-group = no cross-group leak; deterministic, metadata-driven, subject-
agnostic. Mirrors ATTRSCOPE's tokeniser/_tmpl_stop.

REAL CURSOR: mock-drift produces false greens on this family, so these tests run
determine_path against the LIVE local Postgres tenant. Requires POSTGRES_DSN
pointing at the local stack's DB and a provisioned disposable tenant seeded with
the relational facts below (self anchor holds switch_to→gcp, spouse, lives_in,
works_for).
"""

import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")
import src.api.main as main


# Disposable tenant seeded by the repro (see module docstring): self anchor holds
# (user, switch_to, gcp) [Class-B staged] plus spouse / lives_in / works_for.
_USER = "cccc1111-0003-4a03-8a03-c33333333333"
_SCHEMA = "faultline_cccc1111_0003_4a03_8a03_c33333333333"
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
        c.execute(
            "SELECT DISTINCT rel_type FROM ("
            "  SELECT rel_type, subject_id, object_id FROM facts"
            "  UNION SELECT rel_type, subject_id, object_id FROM staged_facts"
            ") f WHERE subject_id = %s OR object_id = %s",
            (_USER, _USER),
        )
        rels = {r[0] for r in c.fetchall()}
    missing = {"switch_to", "spouse", "lives_in", "works_for"} - rels
    if missing:
        pytest.skip(f"disposable tenant not seeded (missing rels: {sorted(missing)})")
    conn.cursor().execute(f"SET search_path TO {_SCHEMA}")
    yield conn
    conn.close()


def _bind(db):
    with db.cursor() as c:
        c.execute(f"SET search_path TO {_SCHEMA}")


def _dp(db, query):
    # Anchor is the user (self) — the reported case is a user-relationship question.
    return main.determine_path(
        query, db, user_id=_USER,
        anchor_is_concrete_entity=False,
        anchor_uuid=None,
        anchor_resolved_uuid=_USER,
    )


def test_switch_narrows_to_relationship_rel(db):
    """'what did I switch to' narrows to the switch_to rel via a content token of the
    grown compound rel's own name ({switch}), NOT a fetch-all profile dump."""
    _bind(db)
    path = _dp(db, "what did I switch to")
    assert "switch_to" in path.relationship_rels, path.relationship_rels
    assert path.scope_active is True
    assert path.fetch_all_details is False, "must NOT collapse to fetch-all dump"


def test_switch_no_cross_group_leak(db):
    """The crux: admitting switch_to must NOT drag in the anchor's UNRELATED rels
    (spouse / lives_in / works_for) or any taxonomy group — aspect-precision firewall."""
    _bind(db)
    path = _dp(db, "what did I switch to")
    assert "spouse" not in path.relationship_rels, path.relationship_rels
    assert "lives_in" not in path.relationship_rels, path.relationship_rels
    assert "works_for" not in path.relationship_rels, path.relationship_rels
    # No group widening — the relational grounding never touches taxonomy_groups.
    assert not path.taxonomy_groups, path.taxonomy_groups
    # Only the single named aspect (+ any metadata inverse) resolved.
    assert set(path.relationship_rels) <= {"switch_to"}, path.relationship_rels


def test_nonmatching_word_stays_broad(db, monkeypatch):
    """A relationship word the anchor does NOT hold must resolve nothing → broad lane
    preserved (grounding on the anchor's own facts is the leak bound)."""
    # Neutralise GLiNER2 taxonomy fallback so a genuine no-match falls broad, not narrowed.
    monkeypatch.setattr(main, "get_gliner_model", lambda: None)
    _bind(db)
    # "purchase" is a real verb but the anchor holds no such rel → no admission.
    path = _dp(db, "what did I purchase")
    assert "switch_to" not in path.relationship_rels, path.relationship_rels
    assert path.scope_active is False, path.relationship_rels


def test_broad_self_query_not_hijacked(db, monkeypatch):
    """A genuinely-broad 'tell me about myself' must NOT be pinned to a single rel."""
    monkeypatch.setattr(main, "get_gliner_model", lambda: None)
    _bind(db)
    path = _dp(db, "tell me about myself")
    assert not path.relationship_rels, path.relationship_rels
    assert not path.taxonomy_groups, path.taxonomy_groups
    assert path.scope_active is False
