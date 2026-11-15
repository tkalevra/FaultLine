"""Q3 — occurrence/ordinal-intent scope admission of EVENT rels.

Root cause: a scoped CONCEPT query ("which vehicle did I take care of first in
February, the bike or the car?") resolves a taxonomy (→ scope_active) whose defining
rels do NOT include the dated EVENT rels (``participated_in``/``temporal_class='event'``).
Those occurrence edges carry the answer but ``QueryPath.allowed_rels`` is
scalar+relationship rels ONLY (by design), so the scoped walk projects them out and the
query returns 0 facts — though the SAME data is reachable from a broad/unscoped anchor.

The fix admits the event/state rel TYPES into a concept scope ONLY when the question's
intent is occurrence/ordinal (``_query_has_occurrence_intent``). It is keyed on
rel_types metadata (``_get_event_state_rels`` → temporal_class/category), NEVER a
hardcoded rel name; a plain scoped query (no occurrence intent) is unchanged.

Runs WITHOUT a live DB: ``_get_event_state_rels`` falls back to the ``_REL_TYPE_META``
startup cache when ``POSTGRES_DSN`` is unset (unit-test env). The admission loop under
test mirrors determine_path's block exactly.
"""

import pytest

import src.api.main as main
from src.api.models import QueryPath


@pytest.fixture
def fake_rel_meta(monkeypatch):
    meta = {
        # Dated occurrence/event rels — admitted under occurrence intent.
        "participated_in": {"temporal_class": "event", "category": "event", "tail_types": ["ANY"]},
        "attended":        {"temporal_class": "event", "category": "event", "tail_types": ["ANY"]},
        "has_state":       {"temporal_class": "event", "category": "state", "tail_types": ["ANY"]},
        # Ordinary rels — NOT event/state.
        "owns":            {"temporal_class": "state", "category": "ownership", "tail_types": ["ANY"]},
        "has_pet":         {"temporal_class": "state", "category": "ownership", "tail_types": ["ANY"]},
        "age":             {"temporal_class": "state", "category": "attribute", "tail_types": ["SCALAR"]},
    }
    monkeypatch.setattr(main, "_REL_TYPE_META", meta)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    return meta


# ── occurrence-intent detector ────────────────────────────────────────────────

def test_occurrence_intent_ordinal_first_last():
    assert main._query_has_occurrence_intent(
        "which vehicle did I take care of first in February, the bike or the car?")
    assert main._query_has_occurrence_intent("what was the last thing I did to my car")
    assert main._query_has_occurrence_intent("my earliest appointment")


def test_occurrence_intent_action_on_entity():
    assert main._query_has_occurrence_intent("what did I do to my bike")
    assert main._query_has_occurrence_intent("what have I done with my car")


def test_occurrence_intent_how_many_days():
    assert main._query_has_occurrence_intent(
        "how many days between servicing the bike and the car")


def test_no_occurrence_intent_plain_scope():
    # A plain membership/scope ask carries NO occurrence/ordinal cue.
    assert not main._query_has_occurrence_intent("tell me about my family")
    assert not main._query_has_occurrence_intent("what are my pets")
    assert not main._query_has_occurrence_intent("who is my mother")
    assert not main._query_has_occurrence_intent("what is my favourite colour")


# ── admission logic (mirrors determine_path's block) ──────────────────────────

def _admit(path: QueryPath, query_text: str) -> QueryPath:
    """Exact mirror of determine_path's occurrence-intent admission block, runnable
    without a DB cursor so the pure scope logic is provable."""
    if path.taxonomy_groups and main._query_has_occurrence_intent(query_text):
        event_state = main._get_event_state_rels()
        for esr in sorted(event_state):
            if main._is_scalar_rel_type(esr):
                if esr not in path.scalar_rels:
                    path.scalar_rels.append(esr)
            elif esr not in path.relationship_rels:
                path.relationship_rels.append(esr)
    return path


def test_occurrence_scope_admits_event_rels(fake_rel_meta):
    """A CONCEPT scope ('vehicle') + occurrence intent → allowed_rels INCLUDES the
    event rels, so the scoped walk can surface participated_in occurrence edges."""
    path = QueryPath(taxonomy_groups=["vehicle"], relationship_rels=["owns"])
    before = set(path.allowed_rels)
    assert "participated_in" not in before          # bug state: projected out

    _admit(path, "which vehicle did I take care of first in February, the bike or the car?")

    after = path.allowed_rels
    assert "participated_in" in after               # FIX: admitted
    assert "attended" in after
    assert "has_state" in after
    assert "owns" in after                          # original scope preserved


def test_plain_scope_unchanged_no_event_admission(fake_rel_meta):
    """A plain CONCEPT scope ('family') with NO occurrence intent is byte-for-byte
    unchanged — no event rels leak in (no unrelated-event regression)."""
    path = QueryPath(taxonomy_groups=["family"], relationship_rels=["has_pet", "parent_of"])
    before = set(path.allowed_rels)

    _admit(path, "tell me about my family")

    assert path.allowed_rels == before              # untouched
    assert "participated_in" not in path.allowed_rels


def test_occurrence_intent_without_concept_scope_no_admission(fake_rel_meta):
    """Occurrence intent but NO concept scope (no taxonomy_groups) → admission does not
    fire here (broad/unscoped queries already surface events via fetch-all; the fix
    only repairs the SCOPED lane). Guards against globally broadening scope."""
    path = QueryPath(relationship_rels=["owns"])   # no taxonomy_groups
    before = set(path.allowed_rels)

    _admit(path, "what did I do to my bike first")

    assert path.allowed_rels == before
    assert "participated_in" not in path.allowed_rels
