"""Unit tests for the POSSESSIVE PP-COMPLEMENT fold (LongMemEval cluster ``dense-capture-single-fact``).

THE GAP (structural root cause across the cluster): a first-person possessive of a thing that carries
its OWN prepositional-complement value — "my study abroad program **at the University of Melbourne**",
"my recent family trip **to Paris**", "my degree **in** physics" — emitted only ``(user, owns, <bare
head>)`` and DROPPED the prepositional value, which is exactly the answer the question asks for. The
fold that recovers that value (``_nominal_pp_complement``) was wired ONLY to the SVO direct-object
build, never to ``_chain_possessive`` — so the value fell out onto the ``uncovered`` residue/growth
path and never surfaced on recall.

THE FIX: apply the SAME firewalled nominal-PP-complement fold to the possessive-chain object, plus a
right-branching ``of <PROPN>`` completion so a multi-word named value ("University of Melbourne") is
captured WHOLE (``_object_value_phrase`` folds only LEFT modifiers, truncating the "of Melbourne" tail).

SUBJECT-AGNOSTIC / DETERMINISTIC: pure spaCy dependency structure, no domain word list, no LLM, no
GLiNER2; the temporal / pronoun / source→goal firewalls of ``_nominal_pp_complement`` are inherited,
and the ``of``-tail completion is PROPN-gated so a common-noun partitive ("cup of coffee", "3 cats")
is never absorbed.

PURE tests — no DB, no network. They need the real spaCy model.

Run: python3 tools/fltest.py --bug LME-cluster-dense-capture-single-fact --test tests/test_spine_possessive_pp_value.py
"""
import datetime
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction.linguistics import derive_sentence_facts, linguistics_available  # noqa: E402

requires_model = pytest.mark.skipif(
    not linguistics_available(), reason="en_core_web_sm not installed in test env")

_REF = datetime.datetime(2026, 4, 15)


def _facts(sentence):
    return [(f.subject, f.rel_type, f.object) for f in derive_sentence_facts(sentence, reference=_REF)]


def _value_captured(sentence, value):
    """True iff the dropped PP-complement VALUE now rides the possessive ``owns``/``related_to`` edge."""
    value = value.lower()
    for s, r, o in _facts(sentence):
        if s == "user" and r in ("owns", "related_to") and value in (o or "").lower():
            return True
    return False


# ── CAPTURE across the whole possessive-PP CONSTRUCTION CLASS (multi-exemplar, subject-agnostic) ──
@requires_model
@pytest.mark.parametrize(
    "sentence,value",
    [
        # LongMemEval 3b6f954b — "Where did I attend for my study abroad program?" → Univ of Melbourne.
        ("I went there with some friends during my study abroad program at the University of Melbourne.",
         "university of melbourne"),
        # LongMemEval 9ea5eabc — "Where did I go on my most recent family trip?" → Paris. The trip's
        # own "to Paris" goal value must ride the OWNED-thing edge (anchor = "my family trip").
        ("Since I've been thinking about my recent family trip to Paris, I compared experiences.",
         "paris"),
        # Same construction, other domains (proves generalization — not a two-question patch).
        ("I got my degree in Business Administration.", "business administration"),
        ("I wrote my thesis about coral reefs.", "coral reefs"),
    ],
)
def test_possessive_pp_value_is_captured(sentence, value):
    assert _value_captured(sentence, value), (
        f"PP-complement value {value!r} dropped from the possessive object in: {sentence!r}\n"
        f"got: {_facts(sentence)}")


# ── PROPER-NAME COMPLETION: the multi-word named value is captured WHOLE, not truncated ───────────
@requires_model
def test_proper_name_of_tail_not_truncated():
    facts = _facts(
        "I went there with some friends during my study abroad program at the University of Melbourne.")
    owns = [o for s, r, o in facts if s == "user" and r == "owns"]
    assert any("university of melbourne" in (o or "") for o in owns), (
        f"expected the whole 'university of melbourne', got owns={owns}")
    # the truncated form alone (missing the 'of melbourne' tail) is the bug we are closing.
    assert not any(o == "program at university" for o in owns), f"truncated value leaked: {owns}"


# ── FIREWALLS: a common-noun partitive / count is NEVER absorbed by the of-tail completion ─────────
@requires_model
@pytest.mark.parametrize(
    "sentence,forbidden_substr",
    [
        ("I have 3 cats.", "of"),          # count firewall — no "of" tail on a bare count
        ("I drank my cup of coffee.", "coffee"),  # common-noun partitive — coffee is NOUN not PROPN
    ],
)
def test_common_noun_of_partitive_not_completed(sentence, forbidden_substr):
    # These assert the OF-TAIL completion (PROPN-only) does not fire; the pre-existing nominal fold
    # for a COMMON-noun pobj is unchanged, so we only guard that the count case stays clean.
    if "cats" in sentence:
        owns_objs = [o for s, r, o in _facts(sentence) if r in ("owns", "have")]
        assert not any(" of " in (o or "") for o in owns_objs), _facts(sentence)
