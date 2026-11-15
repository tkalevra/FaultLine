"""A compound noun is ONE referent — the state edge and the ownership edge must name one node.

THE BUG THIS PINS. Six chains built their subject from the BARE HEAD TOKEN:

    subject = (subj_tok.text or subj_tok.lemma_ or "").strip().lower()

while the possessive/object chains build the full nominal with `_np_phrase`. So a single
sentence produced TWO nodes for one referent:

    "My stand mixer broke down last month."
        (mixer,       has_state, break) @2023-04-22    <- state on the BARE HEAD
        (user,        owns, stand mixer)               <- ownership on the FULL NP

The dated malfunction hung off one node and ownership off another, so the walk
`user -owns-> stand mixer` could NEVER reach `-has_state-> break`. The fact was stored and
structurally unreachable — the worst shape, because nothing looks lost.

This is not a possessive quirk: a definite article does it too ("The stand mixer broke down"),
and it applies to any compound. The fix reuses the module's OWN builder (`_np_phrase`), which
appends `compound`/`amod` dependents — the correct reading of the UD `compound` relation
(de Marneffe et al. 2021, "Universal Dependencies", Computational Linguistics 47(2)): a
compound noun's referent is the whole nominal, head plus its compound children.

Parametrized over unrelated compounds ON PURPOSE — a word list could never satisfy this, which
is what keeps the fix subject-agnostic.
"""
import datetime

import pytest

REF = datetime.datetime(2023, 5, 22)


def _derive(sentence):
    from src.extraction.linguistics import derive_sentence_facts
    out = derive_sentence_facts(sentence, REF) or []
    return [(getattr(f, "subject", None), getattr(f, "rel_type", None),
             getattr(f, "object", None)) for f in out]


@pytest.mark.parametrize("sentence,nominal,head", [
    ("My stand mixer broke down last month.", "stand mixer", "mixer"),
    ("My golden retriever ran away last month.", "golden retriever", "retriever"),
    ("My training pads arrived last week.", "training pads", "pads"),
    ("My coffee maker stopped working last month.", "coffee maker", "maker"),
])
def test_state_and_ownership_converge_on_the_full_nominal(sentence, nominal, head):
    facts = _derive(sentence)
    subjects = {s for s, _r, _o in facts}
    objects = {o for _s, _r, o in facts}

    # The referent must appear as the FULL nominal somewhere...
    assert nominal in subjects | objects, f"{nominal!r} missing entirely from {facts}"

    # ...and the BARE HEAD must never appear as a rival subject node beside it. That split is
    # exactly what made the state edge unreachable from the owner.
    if nominal in subjects or nominal in objects:
        assert head not in subjects or head == nominal, (
            f"bare head {head!r} appears as a rival node alongside {nominal!r} — the walk "
            f"user->owns->{nominal} cannot reach a state hung off {head!r}. facts={facts}")


def test_the_owned_node_is_the_stateful_node():
    """The load-bearing assertion: one node carries BOTH edges, so the walk connects them."""
    facts = _derive("My stand mixer broke down last month.")
    owned = {o for s, r, o in facts if r == "owns" and s == "user"}
    stateful = {s for s, r, _o in facts if r == "has_state"}
    assert owned, f"expected an ownership edge, got {facts}"
    assert stateful, f"expected a state edge, got {facts}"
    assert owned & stateful, (
        f"ownership names {owned} but the state hangs off {stateful} — two nodes for one "
        f"referent; the walk cannot traverse between them. facts={facts}")


def test_single_word_subject_is_unchanged():
    """Fail-safe: a non-compound subject must behave exactly as before (no regression)."""
    facts = _derive("My laptop broke last month.")
    stateful = {s for s, r, _o in facts if r == "has_state"}
    assert stateful, f"expected a state edge, got {facts}"
    assert "laptop" in stateful, facts
