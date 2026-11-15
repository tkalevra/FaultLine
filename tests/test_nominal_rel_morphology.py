"""NOMINAL rel morphology — the possessed NOUN PHRASE must not be lemmatized as a VERB.

THE BUG THIS PINS (observed live on pre-prod seat1, 2026-07-28): "My kiln firing is 9 hours."
minted the rel `kiln_fir`. With the HALF-2 grow wire on (`SPINE_PENDING_GROUNDING`) that flat
novel-attribute scalar is re-homed onto a CONCEPT ENTITY named after the rel and queued for
whatis is-a grounding — so a non-word ("kiln fir") was registered as an entity and pushed into
L4, alongside the correctly-surfaced "kiln firing" the deriver's Option-A chain produced. One
attribute, two rival concepts, one of them a mangle.

ROOT CAUSE: `canonical.normalize_rel` implements VERB morphology (it strips "-ing"/"-ed"), and
two growth seams applied it to a possessed NOUN PHRASE:
  * `main._detect_preference_states`   (harvest seam)
  * `linguistics._chain_favorite_copula` (deriver chain)

THE DISTINCTION (established terms, primary sources):
  * INFLECTIONAL vs DERIVATIONAL morphology — lemmatization removes INFLECTION (the noun plural
    "-s") and must never undo DERIVATION, which creates a new lexeme. Bauer (1983), *English
    Word-Formation*, CUP; Haspelmath & Sims (2010), *Understanding Morphology*, 2nd ed., ch. 5.
  * The DERIVED NOMINAL — English "-ing" on a nominal forms a deverbal noun ("a firing",
    "a meeting"). Chomsky (1970), "Remarks on Nominalization", in Jacobs & Rosenbaum (eds.),
    *Readings in English Transformational Grammar*, 184-221.
  * Penn Treebank tags such a compound head NN, not VBG — Marcus, Santorini & Marcinkiewicz
    (1993), "Building a Large Annotated Corpus of English: The Penn Treebank", *Computational
    Linguistics* 19(2). The callers' dependency parse has already established NOUN before
    routing here, which is why the reading is a CALLER decision, not a guess in the pure module.

Flag: GROWTH_NOMINAL_REL_MORPHOLOGY (default OFF → byte-identical to today).
"""
import importlib
import os

import pytest

from src.ontology import canonical as C


# ── 1. the nominal reading keeps the derivational suffix ──────────────────────
@pytest.mark.parametrize(("phrase", "expected"), [
    ("kiln firing", "kiln_firing"),
    ("favorite running shoes", "favorite_running_shoe"),
    ("my morning meeting", "morning_meeting"),
    ("daily commute", "daily_commute"),
    ("internet plan", "internet_plan"),
])
def test_nominal_keeps_derivational_ing(phrase, expected):
    assert C.normalize_nominal_rel(phrase) == expected


def test_nominal_strips_only_the_plural_inflection():
    # inflection (plural) IS removed — a lemma, not a surface dump
    assert C.normalize_nominal_rel("my favorite colors") == "favorite_color"
    assert C.normalize_nominal_rel("boxes") == "box"          # sibilant stem -> strip "es"
    assert C.normalize_nominal_rel("glasses") == "glass"
    assert C.normalize_nominal_rel("stories") == "story"
    assert C.normalize_nominal_rel("shoes") == "shoe"         # bare -s on an "-e" base
    assert C.normalize_nominal_rel("hours") == "hour"


def test_nominal_does_not_consult_the_irregular_VERB_table():
    """A DERIVED NOMINAL listed in the irregular-VERB table must not be verb-lemmatized.

    `_IRREGULAR_LEMMA` maps the gerund/participle to its verb base ("building"->build,
    "meeting"->meet). Those surfaces are also perfectly ordinary NOUNS ("my building",
    "my morning meeting"), and in a possessed-noun-phrase slot the nominal reading is the
    right one — Chomsky's derived nominal, not a verb form."""
    assert C.normalize_rel("building") == "build"             # verb reading (unchanged)
    assert C.normalize_nominal_rel("building") == "building"  # nominal reading: the structure
    # both tokens are mangled by the verb reading ("morning"->morn via the regular -ing strip,
    # "meeting"->meet via the irregular table); the nominal reading keeps the real lexemes.
    assert C.normalize_rel("morning meeting") == "morn_meet"
    assert C.normalize_nominal_rel("morning meeting") == "morning_meeting"


# ── 2. the VERB reading is untouched (regression guard on the shared pipeline) ─
def test_verb_reading_is_byte_identical():
    """`normalize_rel` was refactored onto a shared pipeline; its output must not move."""
    assert C.normalize_rel("had an issue with") == "has_issue"     # light-verb fold
    assert C.normalize_rel("had_an_issue_with") == C.normalize_rel("had an issue with")
    assert C.normalize_rel("lives in") == "live_in"                # kept preposition
    assert C.normalize_rel("OWNS") == "own"
    assert C.normalize_rel("the owner of") == "owner_of"
    assert C.normalize_rel("my pet") == "pet"
    assert C.normalize_rel("his job") == "job"
    assert C.normalize_rel("") == "" and C.normalize_rel("!!!") == ""
    # the mangle is still what the VERB reading produces — the fix is the CALLER's reading,
    # not a change to verb morphology.
    assert C.normalize_rel("kiln firing") == "kiln_fir"


def test_nominal_is_fail_safe_on_garbage():
    assert C.normalize_nominal_rel("") == ""
    assert C.normalize_nominal_rel("   ") == ""
    assert C.normalize_nominal_rel("!!!") == ""


# ── 3. THE LOCKSTEP INVARIANT — the two growth seams must agree ───────────────
_LOCKSTEP_PHRASES = ["kiln firing", "favorite running shoes", "favorite color",
                     "daily commute", "internet plan", "morning meeting"]


@pytest.mark.parametrize("phrase", _LOCKSTEP_PHRASES)
def test_both_growth_seams_select_the_same_normalizer(phrase, monkeypatch):
    """`_detect_preference_states` (main) and `_chain_favorite_copula` (linguistics) dedup by
    producing the IDENTICAL token for one phrase. They read the same flag and must therefore
    resolve to the same function in BOTH flag states — if they diverge, one possessed attribute
    mints two rival concept entities (the live `kiln firing` / `kiln fir` split)."""
    for state, expected_fn in (("false", C.normalize_rel),
                               ("true", C.normalize_nominal_rel)):
        monkeypatch.setenv("GROWTH_NOMINAL_REL_MORPHOLOGY", state)
        # main.py seam selection
        import src.api.main as M
        main_fn = (C.normalize_nominal_rel
                   if M._flag_on("GROWTH_NOMINAL_REL_MORPHOLOGY", default="false")
                   else C.normalize_rel)
        # linguistics.py seam selection (same predicate, read inline)
        ling_on = os.getenv("GROWTH_NOMINAL_REL_MORPHOLOGY", "false").strip().lower() \
            not in ("0", "false", "no", "off")
        ling_fn = C.normalize_nominal_rel if ling_on else C.normalize_rel
        assert main_fn is expected_fn
        assert ling_fn is expected_fn
        assert main_fn(phrase) == ling_fn(phrase)


# ── 4. flag OFF is byte-identical at the live seam ────────────────────────────
def _spacy_ready() -> bool:
    try:
        from src.extraction.linguistics import linguistics_available
        return bool(linguistics_available())
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _spacy_ready(), reason="spaCy model unavailable")
def test_preference_seam_flag_off_is_todays_behavior(monkeypatch):
    import src.api.main as M
    monkeypatch.setenv("GROWTH_NOMINAL_REL_MORPHOLOGY", "false")
    out = M._detect_preference_states("My kiln firing is 9 hours.")
    assert out and out[0]["rel_type"] == "kiln_fir"      # the DEFECT, preserved when OFF


@pytest.mark.skipif(not _spacy_ready(), reason="spaCy model unavailable")
def test_preference_seam_flag_on_mints_the_real_lexeme(monkeypatch):
    import src.api.main as M
    monkeypatch.setenv("GROWTH_NOMINAL_REL_MORPHOLOGY", "true")
    out = M._detect_preference_states("My kiln firing is 9 hours.")
    assert out, "possessive predication should still be captured"
    assert out[0]["rel_type"] == "kiln_firing"
    assert out[0]["object"] == "9 hours"
    # value is never lost, and never resolved here (strong ingest, lean query)
    assert out[0]["fact_provenance"] == "user_stated"


@pytest.mark.skipif(not _spacy_ready(), reason="spaCy model unavailable")
def test_preference_seam_flagship_case_unchanged_by_the_flag(monkeypatch):
    """"my favorite color is blue" normalizes identically under both readings — the shipped
    flagship preference behavior must not move when the flag flips on."""
    import src.api.main as M
    seen = set()
    for state in ("false", "true"):
        monkeypatch.setenv("GROWTH_NOMINAL_REL_MORPHOLOGY", state)
        out = M._detect_preference_states("My favorite color is blue.")
        assert out
        seen.add((out[0]["rel_type"], out[0]["object"]))
    assert seen == {("favorite_color", "blue")}
