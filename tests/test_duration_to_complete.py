"""DURATION-TO-COMPLETE capture cluster (LongMemEval temporal-reasoning).

The user states how long an ACTIVITY on a target ENTITY took — a "took me <N> <time-unit> to <VERB>"
completion frame:

  "I recently finished 'The Nightingale' by Kristin Hannah, which took me three weeks to finish."
  "It took me three weeks to finish the book."
  "'The Seven Husbands of Evelyn Hugo', which took me two weeks to read."

That duration is a SCALAR of the COMPLETED THING (so a downstream "how long did X take / sum the
durations" walk can read it), NOT of the light-verb clause subject (the expletive "it" / the relative
pronoun "which" / the appositive author).

CAPTURE BEFORE THIS FIX: the deterministic measure-verb chain (``_chain_verb_measure``) already
captured the measure as a scalar, but for the COMPLETION frame it hung the value on the light-verb
subject (expletive "it" → ``(it, take, "three weeks")``; or the misparsed author) and named the
attribute with the raw verb lemma ("take"/"owns") — not identifiable as a DURATION.

THE FIX (deterministic, subject-agnostic, NO verb/unit/domain word list): when the measure verb
governs a "to <VERB>" completion xcomp, the scalar is redirected to the COMPLETED entity (the xcomp's
own direct object, else the relative antecedent — climbing a post-nominal / agent "by <Author>" PP to
the work) and routed to the SEEDED semantic ``duration`` rel (migration 190, tail_types={SCALAR} →
entity_attributes; object_datatype="duration", shape-free so a worded span stores verbatim). Fires
ONLY on this construction → every other measure-verb capture is byte-identical.

fail-on-old:
  * Pre-fix, ``derive_sentence_facts("It took me three weeks to finish the book.")`` emitted
    ``(it, take, "three weeks")`` (junk expletive subject, opaque rel). The ``(book, duration, ...)``
    assertion below FAILS on old code, PASSES on the fix.
"""
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction.linguistics import derive_sentence_facts, linguistics_available  # noqa: E402

requires_model = pytest.mark.skipif(
    not linguistics_available(), reason="en_core_web_sm not installed in test env")


def _facts(sentence, reference=None):
    return [(f.subject, f.rel_type, (f.object or "").lower(), f.scalar_datatype)
            for f in derive_sentence_facts(sentence, reference=reference)]


# (sentence, expected_completed_entity, expected_duration_value)
_COMPLETIONS = [
    ("It took me three weeks to finish the book.", "book", "three weeks"),
    ("The Nightingale, which took me three weeks to finish.", "nightingale", "three weeks"),
    ("I recently finished The Nightingale by Kristin Hannah, which took me three weeks to finish.",
     "nightingale", "three weeks"),
    ("The Seven Husbands of Evelyn Hugo, which took me two weeks to read.", "husbands", "two weeks"),
]


@requires_model
@pytest.mark.parametrize("sentence,entity,value", _COMPLETIONS)
def test_duration_lands_on_completed_entity(sentence, entity, value):
    """The duration is a ``duration`` SCALAR on the COMPLETED entity — never the expletive/author."""
    facts = _facts(sentence)
    dur = [(s, r, o) for (s, r, o, sdt) in facts
           if r == "duration" and sdt == "duration"]
    assert dur, f"no duration scalar captured: {facts}"
    assert any(entity in s and value in o for (s, r, o) in dur), \
        f"duration not on {entity!r} with value {value!r}: {dur}"


@requires_model
def test_duration_never_attaches_to_expletive_it():
    """An expletive 'it took <dur> to <VERB>' with NO completed object mints NO junk (it, ...) edge."""
    facts = _facts("It took me three weeks to recover.")
    assert not any(s == "it" for (s, r, o, sdt) in facts), \
        f"expletive 'it' became a memory: {facts}"


@requires_model
def test_duration_never_attaches_to_person_author():
    """The '<work> by <Author>, which took…' misparse must not file the duration on the author."""
    facts = _facts(
        "I recently finished The Nightingale by Kristin Hannah, which took me three weeks to finish.")
    dur = [(s, r, o) for (s, r, o, sdt) in facts if r == "duration"]
    assert dur and not any("hannah" in s or "kristin" in s for (s, r, o) in dur), \
        f"duration wrongly filed on the author: {dur}"


@requires_model
@pytest.mark.parametrize("sentence", [
    "The project took three years.",
    "My commute takes 45 minutes.",
])
def test_non_completion_measure_unchanged(sentence):
    """PARITY: a measure verb with NO 'to <VERB>' completion xcomp keeps the general measure capture
    (verb-lemma rel, scalar_datatype='string') — the duration rel fires ONLY on the completion frame."""
    facts = _facts(sentence)
    assert not any(r == "duration" for (s, r, o, sdt) in facts), \
        f"duration rel wrongly fired on a non-completion measure: {facts}"
    assert any(sdt == "string" for (s, r, o, sdt) in facts), \
        f"general measure capture regressed: {facts}"
