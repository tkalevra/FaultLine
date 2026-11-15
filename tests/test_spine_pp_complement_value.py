"""Spine deriver: nominal PP-complement value folded onto the MAIN relation (recall-reachable).

Naturalistic prose keeps the clause's main relation but drops the PREPOSITIONAL-COMPLEMENT value
that is usually the answer:

    "I graduated with a degree in Business Administration." → (user, graduate_with, degree)
                                                              — "Business Administration" DROPPED
    "She wrote a book about volcanoes."                    → (she, write, book) — "volcanoes" DROPPED

The dropped span is a NOMINAL PP-complement: a ``prep`` whose GOVERNOR is the clause OBJECT HEAD
NOUN, whose ``pobj`` is a content NOUN/PROPN. A separate ``related_to`` edge is NOT recall-reachable
(the walk never collects a 1-hop object's own outbound ``related_to``, and an active query scope
projects by the clause's main rels), so the value must ride the clause's OWN main relation. Shape A:
compose the value INTO the object phrase — ``degree`` → ``"degree in business administration"``.

These tests call the deriver DIRECTLY (SPACY_MODEL=en_core_web_sm) and assert the object VALUE now
CONTAINS the nominal complement across >=3 cross-domain sentences, and that verb-adjunct PPs,
temporal PPs, and pronoun pobjs are NEVER folded.
"""
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction.linguistics import derive_sentence_facts  # noqa: E402


def _objects_for(sentence):
    """Return the lowercased object surfaces the deriver emits for one clean sentence."""
    facts = derive_sentence_facts(sentence, reference=None)
    return [(f.subject, f.rel_type, f.object) for f in facts]


def _has_object_containing(sentence, *needles):
    """True iff SOME emitted fact's object contains every needle (all lowercased)."""
    for _s, _r, obj in _objects_for(sentence):
        low = (obj or "").lower()
        if all(n.lower() in low for n in needles):
            return True
    return False


def _no_object_contains(sentence, needle):
    """True iff NO emitted fact's object contains the needle (the value was NOT folded in)."""
    return not any(needle.lower() in (obj or "").lower() for _s, _r, obj in _objects_for(sentence))


# ── FOLD: >=3 cross-domain nominal PP-complements ride the main relation ──────────────────────────
@pytest.mark.parametrize(
    "sentence,needle",
    [
        # education
        ("I graduated with a degree in Business Administration.", "business administration"),
        # publishing
        ("She wrote a book about volcanoes.", "volcanoes"),
        # film / documentary
        ("She made a documentary about whales.", "whales"),
        # science / modelling
        ("He built a model of the solar system.", "solar system"),
        # literature
        ("I read a story about dragons.", "dragons"),
    ],
)
def test_nominal_pp_complement_value_rides_main_relation(sentence, needle):
    """The dropped PP-complement value is now part of the MAIN relation's object phrase."""
    assert _has_object_containing(sentence, needle), (
        f"nominal PP value {needle!r} not folded into any object for {sentence!r}: "
        f"{_objects_for(sentence)}"
    )


def test_folded_object_keeps_the_head_noun():
    """Shape A keeps the head noun AND the value — 'degree in business administration'."""
    assert _has_object_containing(
        "I graduated with a degree in Business Administration.",
        "degree", "in", "business administration",
    ), _objects_for("I graduated with a degree in Business Administration.")


# ── NO-FOLD (nominal helper): a COMMON-noun / temporal verb adjunct is not folded ────────────────
# NOTE: a NAMED (PROPN) verb-governed adjunct ("a coupon at Target") IS now folded — by the SEPARATE
# _verb_pp_value lane, pinned in tests/test_spine_verb_pp_value.py. This nominal helper still never
# touches verb adjuncts; and a COMMON-noun verb adjunct stays out end-to-end (structurally
# indistinguishable from manner without a word list → safe under-capture).
@pytest.mark.parametrize(
    "sentence,needle",
    [
        ("I fixed the bug at work.", "work"),      # common-noun verb adjunct — under-captured
        ("She talked to her at noon.", "noon"),    # temporal verb adjunct
    ],
)
def test_verb_adjunct_pp_is_not_folded(sentence, needle):
    """A common-noun / temporal PP that modifies the VERB is never folded into the object."""
    assert _no_object_contains(sentence, needle), (
        f"verb-adjunct {needle!r} was wrongly folded for {sentence!r}: {_objects_for(sentence)}"
    )


# ── NO-FOLD: temporal firewall — a DATE/TIME pobj stays on the temporal lane ──────────────────────
@pytest.mark.parametrize(
    "sentence,needle",
    [
        # "in 2019" attaches to the verb here AND is temporal — never folded.
        ("She wrote a book in 2019.", "2019"),
        # "about January" is a NOMINAL prep governed by the object head "report" — but January is
        # a DATE, so the temporal firewall (_object_candidate_is_temporal) still blocks the fold.
        ("I wrote a report about January.", "january"),
    ],
)
def test_temporal_pobj_is_never_folded(sentence, needle):
    """A date/time value is an event_date scalar, never an object value — even when nominal."""
    assert _no_object_contains(sentence, needle), (
        f"temporal pobj {needle!r} was wrongly folded for {sentence!r}: {_objects_for(sentence)}"
    )


# ── NO-FOLD: pronoun pobjs are excluded (NOUN/PROPN only) ─────────────────────────────────────────
def test_pronoun_pobj_is_not_folded():
    """'a degree in it' — a pronoun pobj is not a mergeable value; the object stays 'degree'."""
    objs = _objects_for("I graduated with a degree in it.")
    assert any(obj == "degree" for _s, _r, obj in objs), objs
    assert _no_object_contains("I graduated with a degree in it.", "it in"), objs
