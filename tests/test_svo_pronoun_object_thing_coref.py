"""Regression: SVO OBJECT-PRONOUN thing-coref rescue (LME gpt4_483dd43c "which show first").

A dated activity clause whose direct object is a NEUTER/PLURAL pronoun ("I finally started IT
about a month ago") is not a mergeable entity, so ``_svo_object_head`` returns None. The existing
object-pronoun rescue only tried ``_person_coref`` (a PERSON antecedent — "…working with HER" →
a named person), so a THING pronoun ("it" → "Game of Thrones") fell through and the whole edge —
WITH its resolved event_date — was DROPPED. That is an INGEST capture defect: the started-watching
event never existed, so the "which show did I start FIRST" ordering had nothing to compute on.

The fix falls back to the SAME recency thing-anaphora (``_coref``: it/they/them → most-recent prior
NP) the resolved-object path already uses, so the clause grounds to ``(user, start, <prior NP>)`` @
event_date. Anaphora resolution by recency (Mitkov; sentic.net anaphora survey). Deterministic,
grammar-only, fail-safe (no antecedent → still a clean drop, never a guessed object).

These assert at the DERIVER (``derive_sentence_facts``) — the same seam ``_harvest_via_sentence_pipeline``
drives — so the pin fails on the pre-fix code (object pronoun → no edge) and passes on the fix.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.extraction.linguistics import derive_sentence_facts, linguistics_available

pytestmark = pytest.mark.skipif(
    not linguistics_available(), reason="spaCy linguistics layer unavailable (set SPACY_MODEL)"
)


def _ref(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _edges(sent: str, prior=None):
    return [
        (f.subject, f.rel_type, f.object, (f.event_date or "")[:10])
        for f in derive_sentence_facts(sent, _ref("2023-08-15"), prior_nps=prior)
    ]


def test_thing_pronoun_object_resolves_to_prior_np():
    # "I finally started it about a month ago" with the prior-clause antecedent available → the
    # started-watching event is captured with its relative event_date. FAILS pre-fix (no edge).
    e = _edges("I finally started it about a month ago", prior=["Game of Thrones"])
    assert ("user", "start", "game of thrones", "2023-07-15") in e


def test_thing_pronoun_object_generalizes_second_exemplar():
    # A different verb/title/relative-date — subject-agnostic, no word list.
    e = _edges("I started watching it last week", prior=["The Crown"])
    assert ("user", "watch", "the crown", "2023-08-08") in e


def test_thing_pronoun_object_no_antecedent_is_clean_drop():
    # Fail-safe: no antecedent of any kind → the edge is still dropped (never a guessed object).
    e = _edges("I finally started it about a month ago", prior=None)
    assert e == []


def test_explicit_object_unaffected():
    # Behavior-preserving: an explicit named object is captured exactly as before (no coref path).
    e = _edges("I watched Game of Thrones about a month ago")
    assert any(r == "watch" and "game" in o for (_s, r, o, _d) in e)


def test_person_pronoun_object_still_resolves_to_person():
    # The pre-existing PERSON path is untouched: "…working with her" → the named person.
    e = _edges("I started working with her on 2/15", prior=["Rachel"])
    assert ("user", "work_with", "rachel", "2023-02-15") in e
