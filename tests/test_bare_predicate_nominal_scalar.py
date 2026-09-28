"""BARE PREDICATE NOMINAL → SCALAR VALUE, and CONTAIN-AND-PROPOSE on a deferral.

MEASURED DEFECT (gauntlet run-104, 106-turn all-invented-noun corpus, spine extractor).
Of six invented attribute nouns in the SAME construction, the deriver captured four and
ANNIHILATED two — and the discriminator was the VALUE'S DEPENDENCY LABEL, not the construction:

    "my kaldrip's wexlimond is forty five units."  units  NOUN attr  nummod  -> (kaldrip, wexlimond, "forty five units")
    "my krellin's dremmage is seven."              seven  NUM  acomp        -> (krellin,  dremmage,  "seven")
    "my krellin's vantick is teal."                teal   ADJ  acomp        -> (krellin,  vantick,   "teal")
    "my vestrow's quenderal is four."              four   NUM  acomp        -> (vestrow,  quenderal, "four")
    "my petrisk's fenshill is ivory."              ivory  NOUN **attr**     -> (fenshill, related_to, petrisk)   VALUE GONE
    "my kaldrip's belvarn is crimson."             crimson NOUN **attr**    -> (belvarn,  related_to, kaldrip)   VALUE GONE

The BARE (determiner-less) NOUN complement was the one complement category the shape gate refused:
no digit, one content token, not a NUM, not an ADJ. The binding returned None, every twin
suppression guard keyed on that binding went UNARMED, and the possessive chain minted the junk
``(<attribute noun>, related_to, <possessor>)`` — filing the ATTRIBUTE NOUN as a first-class entity
while the user's value was dropped as uncovered residue.

AND THE LABEL IS A COIN FLIP. Measured on this box, en_core_web_sm, two structurally identical
sentences disagree:
    "my petrisk's fenshill is ivory."  -> ivory  NOUN NN **attr**  -> is
    "my car's engine is diesel."       -> diesel NOUN NN **acomp** -> is
Same POS, same Number=Sing, same construction. This is the SAME parser instability already
documented in linguistics.py for measure NPs (``ADJUNCT_MEASURE_SCALAR``: spaCy's dobj-vs-npadvmod
choice for a measure NP is unstable across near-identical sentences). A label-keyed gate therefore
covers an arbitrary half of one construction, which is why the fix admits the NOMINAL PREDICATE BY
CATEGORY and leaves the semantic decision to the bare-vs-determined test.

GROUNDING (both verified by search, not asserted from memory):
  * Isabelle Roy, "Predicate Nominals in Eventive Predication", USC Working Papers in Linguistics
    2: 30-56 (2004), §2.1, verbatim: "They claim that bare predicates appear exclusively in
    predicational sentences, whereas the variant with the article is used in identificational
    statements only." (attributing Kupferman 1979 / Pollock 1983), on the Higgins (1979) four-way
    copular taxonomy "predicational, identificational, specificational and identity (or equative)".
    -> BARE predicate nominal = PREDICATIONAL = ascribes a PROPERTY = the VALUE of the attribute.
       ARTICLE-introduced = IDENTIFICATIONAL = a CLASS, not a value.
  * MIT, "Is an Article Necessary?" (https://www.mit.edu/course/21/21.guide/art-necc.htm), verbatim:
    "Singular countable nouns always refer to a specific amount (one), so they always require an
    article (unless another determiner is present)."
    -> In ENGLISH a determiner-less singular predicate noun CANNOT be a singular count
       classification, so the predicational reading is not merely preferred, it is the only one.

SECOND DEFECT, SAME MECHANISM, DIFFERENT ROAD. ``_attr_scalar_binding`` returning None means two
different things to its six consumers and they cannot tell them apart: "not my construction" and
"my construction, value handed to another seam". A DEFERRAL therefore disarms the guards exactly as
a MISS does. The live case is the preference selector on a GENITIVE possessor ("snerrow's favorite
color is teal") — the selector says PREFERENCE, but the preference/affect seam is FIRST-PERSON-ONLY,
so a third-party genitive preference is owned by nobody and falls to the junk mint. It now returns a
CONTAINMENT binding: capture nothing, guards ARMED, and the attribute head noun carried out on
``growth_out`` for the per-tenant growth queue.

⚠️ THAT GROWTH RAIL WAS BUILT AND DARK. ``_chain_attr_scalar`` still consumes ``b["pending_growth"]``
and ``re_embedder.grow_linguistic_cue_candidates`` still registers ``attribute_noun`` in its
``_CARVED`` map — but the V6 rewrite deleted every PRODUCER of that key, so between V6 and this
commit NO attribute-noun candidate could ever be proposed (grep: one consumer, zero producers).
That is the state CLAUDE.md §"Dark, live, and rewritten lanes" warns is indistinguishable in the
data from a lane that was never written.

FAIL-ON-OLD (measured, changes stashed, HEAD 23832d41, real GLiNER2-typed Doc):
  * "my petrisk's fenshill is ivory."  -> [(user, owns, petrisk), (fenshill, related_to, petrisk)]
  * "my kaldrip's belvarn is crimson." -> [(user, owns, kaldrip), (belvarn, related_to, kaldrip)]
  * "my car's engine is diesel."       -> [(user, owns, car), (engine, part_of, car)]  ("diesel" logged
                                          as uncovered residue)
  * "snerrow's favorite color is teal."-> [(favorite color, related_to, snerrow),
                                           (color, has_state, teal)], growth_out == []

NOTE ON THE INPUT PATH: these pins pass a raw ``str``, so spaCy parses with ``disable=["ner"]`` and
GLiNER2 types nothing. That understates capture in general, but every assertion here is decided by
DEPENDENCY + POS, which the untyped parse carries identically — verified by running the same
sentences through ``main._build_typed_doc`` (GLiNER2-typed Doc, the production shape) and getting
the same edges.
"""
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction import linguistics  # noqa: E402
from src.extraction.linguistics import (  # noqa: E402
    derive_sentence_facts, linguistics_available,
)

requires_model = pytest.mark.skipif(
    not linguistics_available(), reason="en_core_web_sm not installed in test env")


def _facts(sentence, growth_out=None):
    out = derive_sentence_facts(sentence, reference=None, growth_out=growth_out)
    return [((f.subject or "").lower(), (f.rel_type or "").lower(),
             (f.object or "").lower(), f.scalar_datatype) for f in out]


def _rels(sentence, growth_out=None):
    return {(s, r, o) for s, r, o, _ in _facts(sentence, growth_out)}


def _scalars(sentence, growth_out=None):
    return {(s, r, o) for s, r, o, dt in _facts(sentence, growth_out) if dt == "string"}


# ─────────────────────────────────────────────────────────────────────────────
# THE DEFAULTS. Pinned SEPARATELY from behaviour, because every behavioural test below
# monkeypatches the module constant — and monkeypatching a constant proves nothing about the value
# it holds when nobody sets the env var. If a future edit flips a default to OFF, the behavioural
# tests keep passing (they set the value themselves) and ONLY these two go red.
# ─────────────────────────────────────────────────────────────────────────────

def test_bare_nominal_lane_defaults_on_when_the_env_var_is_unset():
    assert "ATTR_SCALAR_BARE_NOMINAL" not in os.environ, (
        "this pin is only meaningful with the env var unset")
    assert linguistics.ATTR_SCALAR_BARE_NOMINAL is True


def test_growth_containment_defaults_on_when_the_env_var_is_unset():
    assert "ATTR_SCALAR_GROWTH_CONTAINMENT" not in os.environ, (
        "this pin is only meaningful with the env var unset")
    assert linguistics.ATTR_SCALAR_GROWTH_CONTAINMENT is True


# ─────────────────────────────────────────────────────────────────────────────
# TERM 1 — the bare NOMINAL predicate is the attribute's SCALAR VALUE (``attr`` half)
# ─────────────────────────────────────────────────────────────────────────────

@requires_model
def test_a_bare_noun_value_on_an_invented_attribute_lands_as_a_scalar():
    """run-104 case 5. The whole point of the lane: NEITHER noun has ever been seen, and the
    CONSTRUCTION alone is enough."""
    assert ("petrisk", "fenshill", "ivory") in _scalars("my petrisk's fenshill is ivory.")


@requires_model
def test_the_annihilating_junk_edge_is_gone():
    """The value's loss and the attribute-noun-as-entity mint are ONE defect, not two: the junk
    ``related_to`` is emitted only because a binding miss disarms the possessive chain's guard."""
    rels = _rels("my petrisk's fenshill is ivory.")
    assert ("fenshill", "related_to", "petrisk") not in rels, rels
    assert not [t for t in rels if t[1] == "related_to"], rels


@requires_model
def test_the_second_run_104_bare_noun_case_also_lands():
    assert ("kaldrip", "belvarn", "crimson") in _scalars("my kaldrip's belvarn is crimson.")


# ─────────────────────────────────────────────────────────────────────────────
# TERM 2 — the ``acomp`` half of the coin flip (the complement-selector admission)
# ─────────────────────────────────────────────────────────────────────────────

@requires_model
def test_the_parser_really_does_split_this_construction_across_two_labels():
    """The premise of TERM 2, asserted against the live parser rather than from memory. If this
    ever goes green-by-agreement (both labels equal), the categorial widening is no longer load
    bearing and can be re-narrowed — but re-MEASURE before doing it."""
    doc_a = linguistics._parse("my petrisk's fenshill is ivory.")
    doc_b = linguistics._parse("my car's engine is diesel.")
    dep_a = {t.text: t.dep_ for t in doc_a}["ivory"]
    dep_b = {t.text: t.dep_ for t in doc_b}["diesel"]
    assert dep_a == "attr" and dep_b == "acomp", (dep_a, dep_b)


@requires_model
def test_an_acomp_labelled_bare_noun_value_lands_too():
    """ABLATION TARGET for the ``acomp``+NOUN admission in the complement selector: remove it and
    ONLY this test goes red (the ``attr`` cases above stay green), which is what proves the
    selector widening is a separate term from the bare-nominal predicate."""
    assert ("car", "engine", "diesel") in _scalars("my car's engine is diesel.")


@requires_model
def test_a_real_word_attribute_in_the_same_frame_lands(monkeypatch):
    """Subject-agnostic: nothing about this lane is keyed to invented vocabulary."""
    monkeypatch.setattr(linguistics, "_attribute_nouns", frozenset, raising=False)
    assert ("brother", "job", "teacher") in _scalars("my brother's job is teacher.")


# ─────────────────────────────────────────────────────────────────────────────
# TERM 1/2 ABLATION — the flag OFF restores the annihilation, byte for byte
# ─────────────────────────────────────────────────────────────────────────────

@requires_model
def test_with_the_bare_nominal_flag_off_the_value_is_annihilated_again(monkeypatch):
    monkeypatch.setattr(linguistics, "ATTR_SCALAR_BARE_NOMINAL", False, raising=False)
    rels = _rels("my petrisk's fenshill is ivory.")
    assert ("fenshill", "related_to", "petrisk") in rels, rels
    assert not [t for t in rels if t[1] == "fenshill"], rels


@requires_model
def test_with_the_bare_nominal_flag_off_the_acomp_half_is_annihilated_again(monkeypatch):
    monkeypatch.setattr(linguistics, "ATTR_SCALAR_BARE_NOMINAL", False, raising=False)
    assert not [t for t in _rels("my car's engine is diesel.") if t[1] == "engine"]


# ─────────────────────────────────────────────────────────────────────────────
# THE DISCRIMINATOR IS THE DETERMINER — an ARTICLE-introduced complement is NOT this lane
# ─────────────────────────────────────────────────────────────────────────────

@requires_model
def test_a_determined_complement_is_not_admitted_by_this_lane(monkeypatch):
    """Roy 2004 §2.1: bare = predicational, article = identificational. A determined complement has
    >=2 content tokens in the predicate nominal's subtree, so it can never reach the bare branch —
    which is why the bare test IS ``len(_content) == 1`` and not a separate ``det`` probe. Pinned
    both ways: the flag makes NO difference to this sentence."""
    monkeypatch.setattr(linguistics, "ATTR_SCALAR_BARE_NOMINAL", True, raising=False)
    on = _rels("my petrisk's fenshill is a barn.")
    monkeypatch.setattr(linguistics, "ATTR_SCALAR_BARE_NOMINAL", False, raising=False)
    off = _rels("my petrisk's fenshill is a barn.")
    assert on == off, (on, off)


# ─────────────────────────────────────────────────────────────────────────────
# TERM 3 — CONTAIN-AND-PROPOSE on a deferral, and the growth rail it feeds
# ─────────────────────────────────────────────────────────────────────────────

@requires_model
def test_a_third_party_genitive_preference_is_contained_not_annihilated(monkeypatch):
    """Bar 1.4's contract: an honest miss MAY capture nothing, but it must NOT destroy the value and
    must NOT mint the attribute noun as an entity."""
    monkeypatch.setattr(linguistics, "_attribute_nouns", frozenset, raising=False)
    rels = _rels("snerrow's favorite color is teal.")
    assert ("favorite color", "related_to", "snerrow") not in rels, rels
    assert ("color", "has_state", "teal") not in rels, rels
    assert rels == set(), rels


@requires_model
def test_the_contained_construction_proposes_its_attribute_noun_for_growth(monkeypatch):
    """The other half of bar 1.4: it MUST register the candidate. This is the ONLY producer of
    ``pending_growth`` in the tree — the consumer and the re_embedder's ``attribute_noun``
    registration both predate it and were unreachable."""
    monkeypatch.setattr(linguistics, "_attribute_nouns", frozenset, raising=False)
    growth: list = []
    _rels("snerrow's favorite color is teal.", growth_out=growth)
    assert ("color", "attribute_noun") in growth, growth


@requires_model
def test_growth_is_proposed_once_per_attribute_not_once_per_mention(monkeypatch):
    monkeypatch.setattr(linguistics, "_attribute_nouns", frozenset, raising=False)
    growth: list = []
    _rels("snerrow's favorite color is teal.", growth_out=growth)
    _rels("plimwick's favorite color is amber.", growth_out=growth)
    assert growth.count(("color", "attribute_noun")) == 1, growth


@requires_model
def test_with_the_containment_flag_off_the_junk_mint_returns(monkeypatch):
    """ABLATION TARGET for TERM 3."""
    monkeypatch.setattr(linguistics, "ATTR_SCALAR_GROWTH_CONTAINMENT", False, raising=False)
    monkeypatch.setattr(linguistics, "_attribute_nouns", frozenset, raising=False)
    growth: list = []
    rels = _rels("snerrow's favorite color is teal.", growth_out=growth)
    assert ("favorite color", "related_to", "snerrow") in rels, rels
    assert growth == [], growth


# ─────────────────────────────────────────────────────────────────────────────
# NO-REGRESSION CONTROLS — the seams this lane must not reach into
# ─────────────────────────────────────────────────────────────────────────────

@requires_model
def test_a_first_person_preference_still_belongs_to_the_affect_seam(monkeypatch):
    """THE ONE THAT BITES. Containment is GENITIVE-ONLY; first person keeps today's output."""
    monkeypatch.setattr(linguistics, "_attribute_nouns", frozenset, raising=False)
    growth: list = []
    rels = _rels("my favourite colour is blue.", growth_out=growth)
    assert ("colour", "has_state", "blue") in rels, rels
    assert ("user", "owns", "favourite colour") in rels, rels
    assert growth == [], growth


@requires_model
def test_a_world_statement_with_a_definite_subject_is_untouched():
    """No possessive marker => not this lane. "the sky is blue" must not become a user scalar."""
    assert ("sky", "has_state", "blue") in _rels("the sky is blue.")


@requires_model
def test_a_kinship_head_still_reads_as_an_age_not_an_attribute_scalar():
    assert not [t for t in _rels("my daughter is 28.") if t[1] == "daughter"]


@requires_model
def test_a_named_person_complement_still_belongs_to_the_naming_chain():
    """PROPN is deliberately excluded from the bare-nominal term (THE HARD LINE: a name is a name)."""
    assert not [t for t in _scalars("my mother's name is Priya.") if t[1] == "name"]


@requires_model
def test_a_measured_copula_still_belongs_to_the_copula_measure_chain():
    assert not [t for t in _rels("my son is 62 years old.") if t[1] == "son"]


@requires_model
def test_a_multi_token_nominal_value_still_captures():
    assert ("user", "address", "123 main street") in _scalars("my address is 123 Main Street.")


@requires_model
def test_a_negated_attribute_copula_is_still_deferred():
    assert not [t for t in _scalars("my petrisk's fenshill is not ivory.") if t[1] == "fenshill"]


@requires_model
def test_an_interrogative_is_still_not_a_statement():
    assert not _scalars("what is my petrisk's fenshill?")


@requires_model
def test_the_growth_loop_actually_closes_on_second_exposure(monkeypatch):
    """THE RAIL END TO END, and the reason containment is not merely a proposal nobody reads.

    Exposure 1 CONTAINS and proposes; the growth rail activates the cue (in production the
    freq-gated re_embedder ``grow_linguistic_cue_candidates``, whose ``_CARVED`` map already
    registers ``attribute_noun``); exposure 2 CAPTURES. Without the second-exposure branch the
    selector defers FOREVER — the selector never goes away — so the proposal would grow a cue that
    nothing on this path ever reads, which is the "built and dark" failure this lane exists to end.
    """
    _active: set = set()
    monkeypatch.setattr(linguistics, "_attribute_nouns", lambda: frozenset(_active), raising=False)

    growth: list = []
    first = _facts("snerrow's favorite color is teal.", growth_out=growth)
    assert not [t for t in first if t[3] == "string"], first
    assert growth == [("color", "attribute_noun")], growth

    _active.update(cue for cue, _cat in growth)      # the activation the growth rail performs

    assert ("snerrow", "favorite_color", "teal") in _scalars("snerrow's favorite color is teal.")


@requires_model
def test_an_oov_name_mistagged_as_a_common_noun_is_not_a_scalar_value():
    """⚠️ ``pos_ != PROPN`` DOES NOT EXCLUDE A NAME. MEASURED on en_core_web_sm: "Diane" in this slot
    tags **NOUN NN** while "Priya" in the byte-identical frame tags **PROPN NNP** — the mis-tag
    tracks the name TOKEN, not the frame. Without the orthographic term this chain emitted
    ``(mother, name, "diane")`` beside the naming chain's correct ``(diane, parent_of, user)``,
    filing a PERSON'S NAME as an attribute VALUE (a HARD LINE violation) and reddening
    ``tests/test_linguistics.py::test_genitive_name_binding_first_person``.

    ABLATION TARGET for the orthographic term: drop ``not (comp.is_alpha and comp.is_title and not
    comp.is_sent_start)`` and this goes red — as does that test in test_linguistics.py."""
    t = _rels("My mother's name is Diane.")
    assert ("diane", "parent_of", "user") in t, t
    assert not any(s == "name" or r == "name" or o == "name" for s, r, o in t), t


@requires_model
def test_the_parser_really_does_mistag_that_name_as_a_common_noun():
    """The premise of the guard above, asserted against the live parser rather than from memory."""
    tags = {t.text: (t.pos_, t.dep_) for t in linguistics._parse("My mother's name is Diane.")}
    assert tags["Diane"] == ("NOUN", "attr"), tags
    tags2 = {t.text: (t.pos_, t.dep_) for t in linguistics._parse("my mother's name is Priya.")}
    assert tags2["Priya"] == ("PROPN", "attr"), tags2


@requires_model
def test_a_lower_case_bare_nominal_is_still_captured_after_the_orthographic_guard():
    """The guard must not swallow the lane it protects: the run-104 values are lower-case."""
    assert ("petrisk", "fenshill", "ivory") in _scalars("my petrisk's fenshill is ivory.")
