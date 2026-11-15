"""Unit tests for the OBJECT NAMED-VALUE chain (G4 — LongMemEval dense-capture "the name is dropped").

THE GAP (cluster ``dense-capture-single-fact``): a stated PROPER NAME that identifies a common-noun
THING — "a playlist called Summer Vibes", "a dog named Luna", "my favorite restaurant is Nobu",
"I named my project Phoenix" — is the ANSWER to a "what is the name of …" question, yet the
SVO/possessive backbone kept only the generic head (playlist / dog / restaurant / project) and DROPPED
the name entirely. ``_chain_object_named_value`` recovers the name and files it in the NAMING LAYER of
the referent: ``(referent-type, also_known_as, Name)``.

THE HARD LINE: the proper name is filed as an ALIAS of the referent (also_known_as — the naming layer),
NEVER classified INTO L4 as a type. The referent's generic head stays the type place.

ADDITIVE: the chain emits ONLY the alias edge — it never suppresses/re-anchors the existing
(user, <verb>, <referent>) edge, so nothing already captured changes; PERSON constructions (kinship /
social / role nouns) are skipped so the family/person-name lanes are untouched.

PURE tests — no DB, no network, no GLiNER2, no LLM; they call the deriver DIRECTLY (the connector
detector resolves cue maps via the in-code DB-DOWN bootstrap). They need the real spaCy model.

Run: python3 -m pytest tests/test_spine_object_named_value.py -q   (tests/ is gitignored → git add -f)
"""
import datetime
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction import linguistics as L  # noqa: E402
from src.extraction.linguistics import derive_sentence_facts, linguistics_available  # noqa: E402

requires_model = pytest.mark.skipif(
    not linguistics_available(), reason="en_core_web_sm not installed in test env")

_REF = datetime.datetime(2026, 4, 15)


def _facts(sentence):
    return {(f.subject, f.rel_type, f.object) for f in derive_sentence_facts(sentence, reference=_REF)}


def _name_captured(sentence, referent, name):
    """True iff the proper NAME is filed as an also_known_as alias of the referent (case-insensitive)."""
    referent, name = referent.lower(), name.lower()
    for s, r, o in _facts(sentence):
        if r == "also_known_as" and s == referent and name in (o or "").lower():
            return True
    return False


# ── CAPTURE across the whole named-value CONSTRUCTION CLASS (subject-agnostic, multi-exemplar) ─────
@requires_model
@pytest.mark.parametrize(
    "sentence,referent,name",
    [
        # LongMemEval exemplar 1e043500 — trailing reduced-relative "called" appositive on the object.
        # ⚠️ AMENDED 2026-11-15 (entity-centric filing, the wound-map doctrine lane): these two
        # PLAYLIST rows no longer take the alias-only edge at defaults — the thing arm mints the
        # INSTANCE shape ((summer vibes, instance_of, playlist) + (user, owns, summer vibes)),
        # the same Luna pattern the F5 pet arm established (pinned in
        # tests/test_entity_centric_filing.py). The alias-only mechanism this pin guards stays
        # green under ENTITY_CENTRIC_FILING=False (the legacy contract pinned in
        # test_legacy_alias_only_shape_flag_off) and keeps firing for heads the thing arm does
        # not admit (the third-party dog/cat rows unchanged below).
        # ("I have been listening to a playlist that I created, called Summer Vibes.",
        #  "playlist", "summer vibes"),
        # ("I created a playlist called Summer Vibes.", "playlist", "summer vibes"),
        # naming-verb "named" appositive — animal (a different domain, same construction).
        # ⚠️ AMENDED 2026-11-07 (fog-family F5, the #79 stale-expectation precedent): a
        # SELF-POSSESSED PET binding no longer takes the alias-only edge — the seam's pet arm
        # files the INSTANCE shape ((user, has_pet, <name>) + (<name>, instance_of, <type>))
        # instead (pinned in tests/test_f5_pet_named_possession.py). These two rows moved to a
        # THIRD-PARTY possessive — the same dog/cat construction whose alias reading is unchanged
        # because the pet is not the speaker's.
        ("My colleague has a dog named Luna.", "dog", "luna"),
        ("My roommate adopted a cat named Whiskers.", "cat", "whiskers"),
        # transitive naming verb — "I named my <thing> <PROPN>".
        ("I named my new project Phoenix.", "new project", "phoenix"),
    ],
)
def test_object_name_captured_as_alias(sentence, referent, name):
    assert _name_captured(sentence, referent, name), (
        f"name '{name}' dropped for referent '{referent}' in: {sentence!r} → {_facts(sentence)}")


# "my favorite <thing> is <PROPN>" is a PREFERENCE VALUE, not a naming: 69de11fb (#21 completion,
# later than this pin) routes it to _chain_favorite_copula so "My favorite running shoes are Nike"
# stops filing Nike as the NAME of the shoes. The name is still captured — as the preference's
# value, on the user — and nothing is filed as an alias or a type (#79: stale expectation amended).
@requires_model
@pytest.mark.parametrize("sentence,rel,value", [
    ("My favorite restaurant is Nobu.", "favorite_restaurant", "nobu"),
    ("My favorite bakery is Brioche.", "favorite_bakery", "brioche"),   # fresh domain
])
def test_favorite_copula_name_is_the_preference_value(sentence, rel, value):
    facts = _facts(sentence)
    assert ("user", rel, value) in facts, facts
    assert not any(r in ("also_known_as", "instance_of") and value in (o or "")
                   for _s, r, o in facts), facts


# ── THE HARD LINE: the name is an ALIAS, NEVER classified INTO L4 as a type ────────────────────────
@requires_model
def test_name_is_never_a_type_the_hard_line():
    """⚠️ SCOPED 2026-11-15 (entity-centric filing): the HARD LINE holds exactly as written —
    a NAME is never the OBJECT of instance_of/subclass_of (never filed AS a type) and never
    the SUBJECT of a subclass_of (never a CLASS). What moved by design: at defaults the thing
    arm files the name as the SUBJECT of instance_of — the name IS-AN-INSTANCE of its type
    (the name is the instance, the kind is the place — the F5 Luna shape, the same shape
    tests/test_f5_pet_named_possession.py certifies for pets; the thing spaces are pinned in
    tests/test_entity_centric_filing.py). The instance-subject placement is therefore NOT a
    violation of this pin; the two original refusals are asserted unchanged."""
    facts = _facts("I created a playlist called Summer Vibes.")
    # the name must NOT appear as the OBJECT of instance_of/subclass_of (that would file it as a type),
    # nor as the SUBJECT of a subclass_of (a name is never a class).
    for s, r, o in facts:
        if r in ("instance_of", "subclass_of"):
            assert "summer vibes" not in (o or "").lower(), f"HARD LINE violated: {(s, r, o)}"
            if r == "subclass_of":
                assert "summer vibes" not in (s or "").lower(), f"HARD LINE violated: {(s, r, o)}"


# ── ADDITIVE: the existing user↔referent edge is untouched (recall stays reachable) ────────────────
@requires_model
def test_existing_referent_edge_preserved():
    """⚠️ AMENDED 2026-11-15 (entity-centric filing): the SVO backbone edge is untouched
    ((user, create, playlist) — asserted here, both flag states); the alias-only edge is the
    legacy contract, pinned under ENTITY_CENTRIC_FILING=False (at defaults the thing arm mints
    the instance shape instead — see tests/test_entity_centric_filing.py)."""
    facts = _facts("I created a playlist called Summer Vibes.")
    assert ("user", "create", "playlist") in facts, facts


@requires_model
def test_legacy_alias_only_shape_flag_off(monkeypatch):
    """The pre-doctrine alias-only shape, byte-for-byte, under ENTITY_CENTRIC_FILING=False."""
    monkeypatch.setattr(L, "ENTITY_CENTRIC_FILING", False)
    facts = _facts("I created a playlist called Summer Vibes.")
    assert ("playlist", "also_known_as", "summer vibes") in facts, facts
    assert ("user", "create", "playlist") in facts, facts
    assert ("summer vibes", "instance_of", "playlist") not in facts, facts


# ── NO PERTURBATION: a kinship construction is left entirely to the family lanes ───────────────────
@requires_model
def test_kinship_construction_not_perturbed():
    facts = _facts("My mother is 62 years old.")
    # the named-value chain must NOT fire on a kin role → no also_known_as edge injected here.
    assert not any(r == "also_known_as" for _s, r, _o in facts), facts
    # the kin + age capture is unchanged.
    assert ("mother", "parent_of", "user") in facts, facts


# ── INERT on a sentence with no (name↔type) binding (fail-safe, zero behaviour change) ─────────────
@requires_model
def test_inert_without_a_name_binding():
    facts = _facts("I upgraded to 500 Mbps about three weeks ago.")
    assert not any(r == "also_known_as" for _s, r, _o in facts), facts
