"""Issue #42 — the typed-walk lane's "the question asked for the ladder" exemption was dead.

main.py::_query_impl, Step 1.5: the walk lane returns BEFORE Step 2 builds ``path``, yet its
type-assertion gate called ``_ladder_is_the_question(path, ...)``. ``path`` was unbound there, the
NameError was swallowed by the surrounding ``except``, ``_w_ladder_asked`` stayed False, and every
engine-grown rung the walk returned was silenced — even for "what kind of dog is Rex?", which asks
for exactly that rung. Fix: resolve the path with the same ``determine_path`` call Step 2 makes,
only when the walk answer actually carries a grown rung.

E2E on a throwaway ``*_test`` tenant: the real harvest -> /ingest files the pet, the real
determine_path / axis / interrogative test decide, the real render runs. The only substitution is
the walk's answer (the walk does not answer a type question today), stubbed to carry one
engine-grown rung ON the asked entity — the shape the exemption exists for.

(The #42 sibling — the dead GLiNER2 ``_entity_types`` map in _ingest_impl, built from a ``result``
that is never bound and read by nothing — was deleted; nothing to pin beyond the file compiling.)
"""
import pytest

from tests.test_smoke_ingest_35_36_37 import (  # noqa: F401 — e2e + the production-flag fixture
    L, _spine, e2e, _state, _ask, _rows, _module_flags,
)


def _entity(env, alias):
    rows = _rows(env["schema"], "SELECT entity_id FROM entity_aliases WHERE alias = %s", (alias,))
    return rows[0][0] if rows else None


def _grown_rung_walk(M, mp, subj_id, obj_id, extra=()):
    rung = {"subject": subj_id, "rel_type": "instance_of", "object": obj_id,
            "provenance": "llm_learned", "fact_provenance": "llm_learned", "confidence": 0.6,
            "fact_class": "B"}

    def _walk(query_text, user_id, db):
        return {"status": "answer", "anchor": subj_id,
                "facts": [dict(rung)] + [dict(x) for x in extra]}
    mp.setattr(M, "resolve_typed_walk", _walk)


@_spine
@pytest.mark.parametrize("statement,name,kind", [
    ("I have a dog named Rex.", "rex", "dog"),
    ("I have a cat named Miso.", "miso", "cat"),        # fresh domain
])
def test_walk_lane_speaks_the_rung_when_the_ladder_is_asked(e2e, statement, name, kind):
    M = e2e["M"]
    _state(e2e, statement, [statement])
    subj, obj = _entity(e2e, name), _entity(e2e, kind)
    assert subj and obj, _rows(e2e["schema"], "SELECT entity_id, alias FROM entity_aliases")
    mp = pytest.MonkeyPatch()
    try:
        _grown_rung_walk(M, mp, subj, obj)
        asked = _ask(e2e, f"what kind of {kind} is {name.title()}?")
        told = _ask(e2e, f"tell me about {name.title()}")
    finally:
        mp.undo()
    # the question asked for the ladder → the grown rung on the asked entity is the answer
    assert any(r == "instance_of" for (_s, r, _o) in asked), asked
    # control: an imperative "tell me about" is not a type question → the rung stays silent
    assert not any(r == "instance_of" for (_s, r, _o) in told), told


# ── #91: an error inside the walk-lane determine_path must not poison the lane's later reads ──────
@_spine
def test_walk_lane_path_error_does_not_abort_the_transaction(e2e):
    """determine_path raising a DB error mid-transaction (UndefinedTable here) left the request
    transaction ABORTED, so the walk lane's later reads (preferred names → the rendered subject)
    failed with "current transaction is aborted". The SAVEPOINT fence rolls just that call back."""
    M = e2e["M"]
    s = "I have a parrot named Kiwi."   # fresh domain
    _state(e2e, s, [s])
    subj, obj = _entity(e2e, "kiwi"), _entity(e2e, "parrot")
    assert subj and obj

    def _poisoning_determine_path(query_text, db, **kw):
        with db.cursor() as cur:
            cur.execute("SELECT * FROM no_such_table_lanec_91")   # raises, aborts the txn
        raise AssertionError("unreachable")

    mp = pytest.MonkeyPatch()
    try:
        # the user's own row rides the same answer; rendering it needs the lane's later reads
        # (preferred-name map), which an aborted transaction breaks
        own = {"subject": e2e["user_id"], "rel_type": "has_pet", "object": subj,
               "provenance": "user_stated", "fact_provenance": "user_stated", "confidence": 1.0,
               "fact_class": "A"}
        _grown_rung_walk(M, mp, subj, obj, extra=(own,))
        mp.setattr(M, "determine_path", _poisoning_determine_path)
        got = _ask(e2e, "what kind of parrot is Kiwi?")
    finally:
        mp.undo()
    # the lane still answered and resolved the pet's NAME — its later reads ran on a live txn
    assert any(r == "has_pet" and o.lower() == "kiwi" for (_s, r, o) in got), got
