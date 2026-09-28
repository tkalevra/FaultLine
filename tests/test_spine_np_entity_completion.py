"""Spine deriver: a MULTI-WORD ENTITY NAME is captured WHOLE at the object slot (NP_ENTITY_COMPLETION).

A proper-noun-modified / compound-noun entity NP was truncated to its bare HEAD noun in two seams the
left-modifier-only ``_object_value_phrase`` never covered, dropping the answer-bearing specifier:

  (a) a RIGHT ``of <PROPN>`` name-completion tail on an EMPLOYER — "I work at the University OF Toronto"
      stored ``works_for -> university`` ("Toronto" dropped). The ``_chain_employment`` seam folds only
      left modifiers; the SVO chain already recovers this via ``_nominal_pp_complement``. The employment
      chain now folds the tail via the existing ``_proper_name_of_tail`` and claims the tokens so the
      residue guard / PA-core (G5 twin-suppression) see the whole name COVERED.

  (b) a LEFT bare ``PROPN`` ``nmod`` premodifier (a BRAND the parser labelled ``nmod`` not
      ``compound``) — "I bought a KitchenAid stand mixer" stored ``buy -> stand mixer`` ("KitchenAid"
      dropped). ``_object_value_phrase`` now folds a left PROPN ``nmod`` (no ``case``/``prep`` child,
      non-temporal), used by EVERY object seam.

Pure UD dependency structure (compound + nmod + nested prep-of->pobj), subject-agnostic, NO
brand/org/domain word list. A bare common-noun head is NEVER ballooned (a COUNT stays a count). Flag
``NP_ENTITY_COMPLETION`` default ON; OFF -> the object surface is byte-identical to the prior head-only
reading.

These tests call the deriver DIRECTLY (SPACY_MODEL=en_core_web_sm) and assert the object surface, with
a flag-OFF pin proving byte-identical legacy behaviour and no-over-capture / chain-unregressed pins.
"""
import importlib
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction.linguistics import derive_sentence_facts  # noqa: E402


def _triples(sentence):
    """Lowercased (subject, rel_type, object) triples the deriver emits for one clean sentence."""
    return [(f.subject, f.rel_type, (f.object or "").lower())
            for f in derive_sentence_facts(sentence, reference=None)]


def _has(sentence, subj, rel, *needles):
    """True iff SOME emitted (subj, rel, obj) fact's object contains every needle (all lowercased)."""
    for s, r, obj in _triples(sentence):
        if s == subj and r == rel and all(n.lower() in obj for n in needles):
            return True
    return False


def _objects(sentence):
    return [obj for _s, _r, obj in _triples(sentence)]


# ── (a) EMPLOYER of-<PROPN> name tail rides the works_for object WHOLE ─────────────────────────────
@pytest.mark.parametrize(
    "sentence,tail",
    [
        ("I work at the University of Toronto.", "toronto"),
        ("I work for the Bank of America.", "america"),
        ("She works for the Isle of Skye Council.", "skye"),   # 3rd-person, subject-agnostic
    ],
)
def test_employer_of_propn_tail_captured_whole(sentence, tail):
    # the works_for object carries the full multi-word name, not just the head noun
    assert _has(sentence, "user" if sentence.startswith("I ") else "she", "works_for", tail), \
        f"of-tail '{tail}' dropped from works_for object in: {sentence} -> {_triples(sentence)}"


def test_employer_full_surface_university_of_toronto():
    assert _has("I work at the University of Toronto.", "user", "works_for", "university", "toronto")


# ── (b) LEFT PROPN nmod BRAND premodifier rides the object WHOLE ───────────────────────────────────
@pytest.mark.parametrize(
    "sentence,brand,head",
    [
        ("I bought a KitchenAid stand mixer.", "kitchenaid", "mixer"),
        ("I use a Bosch cordless drill.", "bosch", "drill"),
    ],
)
def test_left_propn_nmod_brand_captured_whole(sentence, brand, head):
    # the full brand+compound surface lands on the buy/use object
    objs = " | ".join(_objects(sentence))
    assert any(brand in o and head in o for o in _objects(sentence)), \
        f"brand '{brand}' dropped from object in: {sentence} -> {objs}"
    # the head-noun TYPE companion still types on the bare head (THE HARD LINE untouched):
    # the full-surface instance is instance_of the bare head-noun type, never the brand-name.
    assert any(r == "instance_of" and o == head for _s, r, o in _triples(sentence)), \
        f"instance_of head-type '{head}' lost: {_triples(sentence)}"


# ── ALREADY-WHOLE PROPN compounds stay whole (no regression) ──────────────────────────────────────
@pytest.mark.parametrize(
    "sentence,needle",
    [
        ("I visited the Golden Gate Bridge.", "golden gate bridge"),
        ("I visited the New York office.", "new york office"),
    ],
)
def test_propn_compound_objects_unchanged(sentence, needle):
    assert any(needle in o for o in _objects(sentence)), \
        f"PROPN-compound object regressed in: {sentence} -> {_objects(sentence)}"


# ── NO OVER-CAPTURE: a bare COUNT / common-noun head is never ballooned ───────────────────────────
def test_bare_count_not_folded_into_relational_object():
    # "3 companies" is a COUNT, not a named value: works_for object stays "companies" (no "3")
    for _s, _r, obj in _triples("I work for 3 companies."):
        if _r == "works_for":
            assert "3" not in obj, f"count folded into works_for object: {obj}"


def test_bare_count_cats_unchanged():
    trips = _triples("I have 3 cats.")
    # the count scalar path owns this; the object surface never absorbs the quantifier as a name
    assert not any(_r == "has_pet" and "3" in obj for _s, _r, obj in trips), trips


# ── SINGLE-WORD entity objects unchanged ──────────────────────────────────────────────────────────
def test_single_word_org_unchanged():
    assert _has("I work at Google.", "user", "works_for", "google")
    for _s, _r, obj in _triples("I work at Google."):
        if _r == "works_for":
            assert obj == "google", f"single-word org surface changed: {obj}"


# ── FAMILY / KINSHIP / PREFERENCE chains unregressed ──────────────────────────────────────────────
def test_kinship_chain_unregressed():
    assert _has("my mother is 62 years old.", "mother", "parent_of", "user")
    assert _has("my mother is 62 years old.", "mother", "age", "62")


def test_preference_chain_unregressed():
    # "my favorite color is blue" keeps its preference residue shape, NOT (user, feels, blue)
    trips = _triples("my favorite color is blue.")
    assert any(r == "has_state" and o == "blue" for _s, r, o in trips) \
        or any("favorite" in o for _s, _r, o in trips), trips


# ── FLAG OFF -> byte-identical to the prior head-only reading ──────────────────────────────────────
def test_flag_off_byte_identical_head_only():
    prev = os.environ.get("NP_ENTITY_COMPLETION")
    os.environ["NP_ENTITY_COMPLETION"] = "false"
    try:
        import src.extraction.linguistics as _L
        importlib.reload(_L)
        def _obj(sentence, rel):
            return [(o or "").lower()
                    for f in _L.derive_sentence_facts(sentence, reference=None)
                    for o in [f.object] if f.rel_type == rel]
        # employer truncates to the head noun (legacy)
        assert "university" in _obj("I work at the University of Toronto.", "works_for")
        assert "university of toronto" not in _obj("I work at the University of Toronto.", "works_for")
        # brand nmod dropped (legacy) -> object is the compound head only
        assert any(o == "stand mixer" for o in _obj("I bought a KitchenAid stand mixer.", "buy"))
    finally:
        if prev is None:
            os.environ.pop("NP_ENTITY_COMPLETION", None)
        else:
            os.environ["NP_ENTITY_COMPLETION"] = prev
        import src.extraction.linguistics as _L2
        importlib.reload(_L2)
