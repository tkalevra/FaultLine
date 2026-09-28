"""Regression gate for the SPINE deriver's SELF-NAMING capture (``_chain_self_name``).

── THE GAP (LME c5e8278d "What was my last name before I changed it?" → gold "Johnson") ────────
The direct first-person self-naming construction — "my [mods] name is/was <ProperName>" — had NO
owning chain in ``derive_sentence_facts``. ``_chain_genitive_name`` owns the KIN case
("my mother's name is Carol") and ``_chain_copula_name`` owns the role case ("my sister is Sarah"),
but the pure self case where the SUBJECT ITSELF is the naming noun "name" possessed by "my" fell
through: the deriver dropped it as uncovered residue, and the main.py preference seam
(``analyze_possessive_predication`` → ``_detect_preference_states``) mis-minted a junk grown rel —
"my old name was Johnson" → (user, old_name, johnson), "my last name is Winters" → (user,
last_name, winters) — burying the name under a non-walkable preference predicate and resolving the
proper name as a phantom relationship-object entity. So "Johnson" never landed as a user alias and
recall's name-intent gate ("my last name") had nothing to surface.

── THE FIX ────────────────────────────────────────────────────────────────────────────────────
  * ``_chain_self_name`` (src/extraction/linguistics.py) captures the construction as
    (user, also_known_as, <name>) — the canonical naming rel that files the object as an alias of
    the user entity (THE HARD LINE: a name is FILED via the alias registry, never classified into
    L4). Modifiers ("old"/"former"/"last"/"first"/"maiden") are dropped — the value is the user's
    name regardless of naming facet — so the former last name lands as a user alias and recall
    surfaces it.
  * ``analyze_possessive_predication`` (the preference seam's detector) now DECLINES the naming
    construction (possessed head noun "name") so the preference seam no longer mints the junk
    (user, old_name/last_name/name, <name>) twin. Genuine preferences ("my favorite color is
    blue") are untouched.

All inputs are SUBJECT-AGNOSTIC (grammar + the "name" naming-noun language primitive that every
naming chain already keys on) and use NON-personal reference data.
"""
import datetime

import pytest

from src.extraction import linguistics as m

pytestmark = pytest.mark.skipif(
    not m.linguistics_available(),
    reason="spaCy linguistic layer unavailable (SPACY_MODEL unset) — spine deriver no-ops",
)

_REF = datetime.date(2023, 6, 1)


def _triples(facts):
    return [(f.subject, f.rel_type, f.object) for f in facts]


# ── SELF-NAMING CAPTURE → (user, also_known_as, <name>) ─────────────────────────────────────

@pytest.mark.parametrize("text,name", [
    ("my name is Johnson.", "johnson"),
    ("my old name was Johnson.", "johnson"),        # the LME former-name case (gold Johnson)
    ("my last name was Johnson.", "johnson"),
    ("my last name is Winters.", "winters"),
    ("my former name was Baker.", "baker"),
    ("my maiden name was Alvarez.", "alvarez"),
])
def test_self_name_captures_user_alias(text, name):
    triples = _triples(m.derive_sentence_facts(text, _REF))
    assert ("user", "also_known_as", name) in triples, triples
    # THE HARD LINE / no preference junk: the naming noun must NOT become a grown preference rel,
    # and the name must NOT be filed under owns / a *_name predicate.
    for subj, rel, obj in triples:
        assert not (subj == "user" and rel.endswith("name")), (subj, rel, obj)
        assert not (subj == "user" and rel == "owns" and obj == name), (subj, rel, obj)


def test_former_name_is_the_gold_answer_case():
    """The exact LME construction: former last name stated, then the new name via a pronoun.
    Only the explicitly-named former last name binds as a user alias (the gold answer)."""
    text = "my old name was Johnson, but now it is Winters."
    triples = _triples(m.derive_sentence_facts(text, _REF))
    assert ("user", "also_known_as", "johnson") in triples, triples


# ── THE PREFERENCE SEAM DECLINES THE NAMING CONSTRUCTION ────────────────────────────────────

@pytest.mark.parametrize("text", [
    "my name is Johnson.",
    "my old name was Johnson.",
    "my last name is Winters.",
])
def test_possessive_predication_declines_naming_noun(text):
    # analyze_possessive_predication (the preference/attribute detector) must NOT read the naming
    # construction as a preference — else _detect_preference_states mints the junk grown rel.
    assert m.analyze_possessive_predication(text) is None


@pytest.mark.parametrize("text,possessed,value", [
    ("my favorite color is blue.", "favorite color", "blue"),
    ("my favorite food is sushi.", "favorite food", "sushi"),
])
def test_genuine_preference_still_captured(text, possessed, value):
    # The naming-noun guard must not disturb genuine preferences.
    pp = m.analyze_possessive_predication(text)
    assert pp is not None
    assert pp.possessed == possessed
    assert pp.value == value


# ── NEGATIVE / GUARD CASES ──────────────────────────────────────────────────────────────────

def test_interrogative_name_not_captured():
    # "what is my name?" is a QUESTION, not a naming statement — no alias minted.
    triples = _triples(m.derive_sentence_facts("what is my name", _REF))
    assert not any(rel == "also_known_as" for _, rel, _ in triples), triples


def test_negated_self_name_not_captured():
    # "my name is not Johnson" → absence; skip (parity with the sibling naming chains).
    triples = _triples(m.derive_sentence_facts("my name is not Johnson.", _REF))
    assert ("user", "also_known_as", "johnson") not in triples, triples


def test_kin_genitive_name_still_binds_to_named_person():
    # "my mother's name is Carol" must remain owned by _chain_genitive_name (bind Carol as the
    # named person + kin rel), NOT hijacked by the self-name chain onto the user.
    triples = _triples(m.derive_sentence_facts("my mother's name is Carol.", _REF))
    assert ("user", "also_known_as", "carol") not in triples, triples
    assert any(subj == "carol" for subj, _, _ in triples), triples
