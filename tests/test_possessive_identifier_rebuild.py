"""Regression gate for the DB-gated identifier rebuild on the POSSESSIVE-predication value
path (``_complement_value_phrase``), restored so the possessive path captures a shattered
alphanumeric identifier WHOLE — mirroring ``_identifier_context_binding`` — instead of
minting a truncated ``<noun>`` attribute (e.g. postal_code='x9y') alongside the whole
has_reference_id.

WHY THIS IS ITS OWN TEST: ``test_identifier_context_shattered_value`` exercises
``derive_sentence_facts`` (the ``_identifier_context_binding`` lane → has_reference_id). The
possessive path (``analyze_possessive_predication`` → ``_complement_value_phrase``) is a
DIFFERENT lane that produces a ``<noun>`` SCALAR attribute; until the rebuild was restored
here it truncated the same identifier. This pins the possessive lane directly.

GATING + L4 SAFETY: the rebuild fires ONLY when the possessed head-noun is in the
``identifier_noun`` cue class (DB-gated, same gate as ``_identifier_context_binding``), so
non-identifier nouns ("favorite color", "blood type", "dog") are UNCHANGED — the L4 typing
of the possessed noun and the hard-line value/place split are never touched. The copula
sits between the value and the noun, so the run walker cannot cross into the subject side.

NOTE: all fixture values are synthetic placeholders of a structural shape.
"""
import warnings

import pytest

from src.extraction.linguistics import (
    _possessed_head_is_identifier_cue,
    analyze_possessive_predication,
    linguistics_available,
)

warnings.filterwarnings("ignore")

pytestmark = pytest.mark.skipif(
    not linguistics_available(),
    reason="spaCy linguistic layer unavailable (SPACY_MODEL unset) — spine seams no-op",
)


# ── THE FIX: identifier-noun possessive captures the value WHOLE ──────────────────────

@pytest.mark.parametrize("text,possessed,value", [
    ("my code is X9Y 8Z7", "code", "x9y 8z7"),              # whitespace-shattered identifier
    ("my confirmation code is AB 451", "confirmation code", "ab 451"),
    ("my ticket number is T-99-X", "ticket number", "t-99-x"),
])
def test_possessive_identifier_value_whole(text, possessed, value):
    pp = analyze_possessive_predication(text)
    assert pp is not None, f"possessive predication declined for {text!r}"
    assert pp.possessed == possessed
    assert pp.value == value


# ── L4 SAFETY: non-identifier nouns are UNCHANGED (no rebuild, no L4 disturbance) ─────

@pytest.mark.parametrize("text,possessed,value", [
    ("my favorite color is dark blue", "favorite color", "dark blue"),
    ("my favorite color is blue", "favorite color", "blue"),
    ("my blood type is O negative", "blood type", "o negative"),  # the original multi-token fix
])
def test_non_identifier_value_untouched(text, possessed, value):
    pp = analyze_possessive_predication(text)
    assert pp is not None
    assert pp.possessed == possessed
    assert pp.value == value


# ── UNIT: the cue gate mirrors _identifier_context_binding's fire condition ───────────

def test_gate_matches_identifier_context_fire_condition():
    """The rebuild gate is the SAME cue check _identifier_context_binding fires on: a strong
    identifier_noun, or a suffix head with a strong compound. A non-identifier noun never gates."""
    import spacy
    nlp = spacy.load("en_core_web_sm")

    def _nsubj(text):
        doc = nlp(text)
        return next((t for t in doc if t.dep_ in ("nsubj", "nsubjpass")), None)

    # 'code' is a strong identifier_noun in the seed/bootstrap → gates open
    assert _possessed_head_is_identifier_cue(_nsubj("my code is X9Y 8Z7")) is True
    # 'number' is a suffix; with a strong compound 'ticket' → gates open
    assert _possessed_head_is_identifier_cue(_nsubj("my ticket number is T99")) is True
    # 'color' / 'dog' / 'type' are NOT identifier nouns → gates closed
    assert _possessed_head_is_identifier_cue(_nsubj("my favorite color is blue")) is False
    assert _possessed_head_is_identifier_cue(_nsubj("my dog is a poodle")) is False
    assert _possessed_head_is_identifier_cue(_nsubj("my blood type is O negative")) is False
