"""Row-level pins for NEGATION POLARITY DIRECTION in the spine deriver.

WHY THIS FILE EXISTS: this lane shipped once with ZERO tests while `tests/test_linguistics.py` was
byte-identically 331/331 at both arms of a change to a polarity discriminator. A suite that cannot
move across such a change is not reassurance — it is proof it pins nothing. Blind critics then found
regression classes by hand, twice. Every one of them is pinned here.

THE INVARIANT:

    For a clause the engine judges NEGATED, the acceptable outcomes are a NEGATED row or NO ROW.
    An AFFIRMED row FOR THAT PREDICATION is never acceptable — it asserts the opposite of what the
    person said. And for a construction that only LOOKS negated, a NEGATED row is equally
    unacceptable.

    ⚠️ SCOPE: THE INVARIANT IS PER-PREDICATION, NOT PER-SENTENCE. In a MULTI-CLAUSE sentence a
    subordinate clause is a SEPARATE assertion and may legitimately be affirmed — "I no longer drink
    coffee, though I like it more than I like tea" must store the cancellation NEGATED and
    (user, like, tea) AFFIRMED. Use `_assert_no_affirmed_row` only on SINGLE-CLAUSE sentences;
    multi-clause cases use `_assert_no_affirmed_twin`, which forbids the affirmed twin of the denied
    triple without touching other predications.

    (An earlier revision of this file stated the per-sentence form here and a previous report
    claimed this header had been corrected. It had not — only one inline pin adopted the narrower
    form, leaving the per-sentence helper as a landmine for the next multi-clause pin. Corrected
    now, and verified by diffing the header rather than by asserting it.)

Silence is a visible gap; a stored denial-of-a-denial is a confident lie. Several pins therefore
assert "no affirmed row" or "no denial" rather than demanding a specific row: they permit the
fail-safe and forbid the lie, so a future fix converting silence into capture still passes.

TWO HARD-WON CORRECTIONS RECORDED IN THE PINS THEMSELVES:

 1. NEGATION SCOPES OPPOSITELY ACROSS THE PHASE RAIL. Negating an ingressive/continuative negates
    the complement; negating a TERMINATIVE affirms it — "I did not stop drinking coffee" means the
    person STILL DRINKS COFFEE. Only the presupposition projects, never the assertion. An earlier
    version of THIS FILE asserted the opposite and was itself the falsehood.
 2. A PIN THAT FILTERS ROWS BY PREDICATE PREFIX CAN BE VACUOUS. The previous helper matched rows by
    `startswith(verb)`; on 4 of 5 parameters the parse emitted the matrix verb instead, so nothing
    matched and the assertion could not fail — and it was blind to affirmed rows on other predicates
    in the same sentence, which is exactly how an affirmed governed-PP place edge slipped through.
    The helper below filters NOTHING.

SUBJECT-AGNOSTIC: parametrized over invented fillers, so a lexical shortcut cannot pass.
"""

import os

import pytest

import src.extraction.linguistics as ling

_MODEL = os.environ.get("SPACY_MODEL", "en_core_web_sm")
_OBJECTS = ["quannup", "zantrils", "morbex", "vellisk", "drennitch"]


@pytest.fixture(scope="module", autouse=True)
def _require_linguistics():
    os.environ.setdefault("SPACY_MODEL", _MODEL)
    if not ling.linguistics_available():
        pytest.skip(f"spaCy model {_MODEL!r} not loadable — linguistics layer unavailable")


def _rows(sentence):
    return [(e.subject, e.rel_type, e.object, bool(e.negated))
            for e in ling.derive_sentence_facts(sentence, reference="user")]


def _assert_no_affirmed_row(sentence):
    """SINGLE-CLAUSE ONLY: no row from a denied clause may be AFFIRMED.

    ⚠️ Do NOT use this on a multi-clause sentence — a subordinate clause is a separate assertion and
    its affirmed row is CORRECT, so this helper would fail against right behaviour. Use
    `_assert_no_affirmed_twin` there. (See the SCOPE note in the module header.)"""
    rows = _rows(sentence)
    affirmed = [r for r in rows if not r[3]]
    assert not affirmed, f"affirmed row survived a denial: {sentence!r} -> {affirmed} (all: {rows})"


def _assert_no_affirmed_twin(sentence, subject, rel, obj):
    """MULTI-CLAUSE SAFE: the denied triple must be NEGATED and must have no AFFIRMED twin.

    Other predications in the same sentence are left alone — they may legitimately be affirmed.
    The affirmed twin of the denied triple is the thing that would actually be a lie."""
    rows = _rows(sentence)
    assert (subject, rel, obj, True) in rows, f"denial not captured: {sentence!r} -> {rows}"
    assert (subject, rel, obj, False) not in rows, (
        f"affirmed twin of the denied triple survived: {sentence!r} -> {rows}")


def _assert_no_denial(sentence):
    """No row may be NEGATED — for constructions that only LOOK negated."""
    rows = _rows(sentence)
    denials = [r for r in rows if r[3]]
    assert not denials, f"stored a denial for a non-denial: {sentence!r} -> {denials} (all: {rows})"


# ── 1. TERMINATIVE phase verbs — negation AFFIRMS the complement (GAP 1) ──────────────────────

@pytest.mark.parametrize("obj", _OBJECTS)
def test_negated_terminative_never_stores_a_denial(obj):
    """"I did not stop drinking X." asserts the person STILL DOES IT. Storing a denial here is a
    confident falsehood about ongoing behaviour — worse than the silence it replaced. The engine
    reads the phase LABEL off the per-tenant cue row (migration 113 writes "Terminative phase
    verb …" into `description`) and marks the complement only for phases whose negation scopes
    down. Silence is the accepted outcome; a denial is not."""
    for sentence in (f"I did not stop drinking {obj}.",
                     f"I never stopped playing {obj}.",
                     f"I have not finished writing {obj}."):
        _assert_no_denial(sentence)


def test_terminative_phase_label_is_read_from_the_cue_row_not_hardcoded():
    """The phase distinction must come from the cue data, because the class is per-tenant GROWABLE
    — a grown terminative would otherwise silently inherit down-scoping. Asserted on the resolver
    contract rather than on any verb list in code."""
    from src.api import linguistic_cue_overlay as lco
    phases = lco.resolve_aspectual_control_phases("") or {}
    assert phases, "phase label map must never resolve empty (bootstrap floor)"
    assert "terminative" in (phases.get("stop") or "").lower()
    assert "terminative" in (phases.get("finish") or "").lower()
    assert "continuative" in (phases.get("continue") or "").lower()
    assert "ingressive" in (phases.get("start") or "").lower()


def test_negative_implicatives_are_distinguishable_by_label_too():
    """Same asymmetry one rail over: a POSITIVE implicative scopes down, a NEGATIVE one inverts.
    The seed excludes fail/forget/neglect BY COMMENT ONLY, with no mechanism, and the class grows —
    so the consumer must read the polarity LABEL rather than trust the membership.

    ⚠️ THIS PIN PREVIOUSLY ASSERTED THE BUG. It demanded that EVERY floor member be labelled
    positive, which was true only because the floor blanket-labelled them all — the very defect
    that made DB-down more permissive than the seed. A member the seed does not label positive must
    read as not-positive here too; `test_implicative_floor_matches_the_seed_labels` pins which."""
    from src.api import linguistic_cue_overlay as lco
    pol = lco.resolve_implicative_polarities("") or {}
    assert pol, "implicative polarity map must never resolve empty (bootstrap floor)"
    assert any("positive" in (v or "").lower() for v in pol.values()), pol
    assert not all("positive" in (v or "").lower() for v in pol.values()), (
        "a blanket-positive floor is the environment-divergence defect; mirror the seed", pol)


# ── 2. INGRESSIVE / CONTINUATIVE descent — negation DOES scope down ───────────────────────────

@pytest.mark.parametrize("obj", _OBJECTS)
def test_negated_continuative_is_never_affirmed(obj):
    _assert_no_affirmed_row(f"I do not continue brewing {obj}.")


@pytest.mark.parametrize("obj", _OBJECTS)
def test_negated_continuative_is_captured_negated(obj):
    """Polarity is the invariant; the DECOMPOSITION varies with the parse (some fillers descend to
    (user, brew, X), others keep the matrix as (user, continue, "brewing X")). Both are legitimate
    and both must be negated — pinning a fixed predicate would pin the parse wobble instead."""
    rows = _rows(f"I do not continue brewing {obj}.")
    assert rows, "the denial must be captured, not dropped"
    assert any(r[3] for r in rows) and not any(not r[3] for r in rows), rows


# ── 3. GOVERNED-PP PLACE EDGE — every emit in the block, not just the one fixed (GAP 2) ───────

def test_governed_pp_place_edge_carries_the_clause_polarity():
    """The fall-through has several `_emit` calls and only some passed the verb the central flip
    keys on. The governed-PP place edge passed none, so it emitted AFFIRMED beside a correctly
    negated object edge — an affirmed row inside a denial, where the parent had dropped the whole
    clause. Audited by call site."""
    s = "I do not buy the Samsung Galaxy S22 from the Best Buy store."
    rows = _rows(s)
    assert rows, "clause must be captured"
    assert any(r[1].startswith("buy_") for r in rows), f"place edge missing: {rows}"
    _assert_no_affirmed_row(s)


def test_affirmative_control_keeps_the_place_edge_affirmed():
    rows = _rows("I buy the Samsung Galaxy S22 from the Best Buy store.")
    assert any(r[1].startswith("buy_") and not r[3] for r in rows), rows
    assert not any(r[3] for r in rows), rows


# ── 4. Non-member comparatives: rejected by MEMBERSHIP, for a reason unrelated to `than` ──────
# ⚠️ These two sentences used to sit under a pin named for the standard-of-comparison guard. They
# never tested it: "further" lemmatises to `far`, which is NOT in the closed continuative set, so
# membership rejects them before the standard is ever consulted. They are kept as comparative-scope
# regression cases, correctly labelled; the guard's real family is pinned in section 16.

@pytest.mark.parametrize("sentence", [
    "I go no further than the gate.",
    "I read no further than chapter three.",
])
def test_non_member_comparative_with_a_standard_is_not_a_denial(sentence):
    _assert_no_denial(sentence)


def test_correlative_than_clause_is_not_predicate_negation():
    """"No sooner did I buy the quannup than it broke." asserts the buying DID happen.

    ⚠️ THIS PIN REPLACES ONE THAT GUARDED AN OVER-BROAD FIX. The construction was previously
    excluded by testing for SUBJECT-AUX INVERSION, and the old pin locked that in. Inversion cannot
    discriminate: English NEGATIVE INVERSION is triggered by the whole CLASS of fronted negative
    adverbials, and "no longer"/"no more" are canonical members alongside "no sooner" — so the
    inversion test rejected the very cancellations this lane exists to capture. What identifies the
    correlative is its `than`-CLAUSE (a `mark`, attached to the subordinate verb), which is why the
    fronted pins directly below must pass at the same time as this one."""
    _assert_no_denial("No sooner did I buy the quannup than it broke.")


@pytest.mark.parametrize("sentence, subject, rel, obj", [
    ("No longer will I work at Morbex.", "user", "works_for", "morbex"),
    ("No longer did I drink coffee.", "user", "drink", "coffee"),
    ("No more do I drink coffee.", "user", "drink", "coffee"),
])
def test_fronted_negative_adverbial_still_cancels(sentence, subject, rel, obj):
    """FRONTED cancellations — the population the inversion guard destroyed. "No longer will I work
    at Morbex." was captured CORRECTLY as a denial at the parent and the guard stored its exact
    opposite; the other two went from silence to affirmed falsehoods. The file previously covered
    only mid-position and trailing, which is why nothing caught it."""
    rows = _rows(sentence)
    assert (subject, rel, obj, True) in rows, rows
    _assert_no_affirmed_row(sentence)


# ⚠️ THE FAMILY, NOT THE EXAMPLE. Four consecutive guards shipped a falsehood here, and the reason
# the pin file never caught one is that it only ever exercised the case each guard was DESIGNED
# from. The mandatory pin for an exclusion is the population that SATISFIES its trigger and must
# still be admitted.
#
# ⚠️ ACCURACY CORRECTION, because a previous version of this comment overstated it: every sentence
# below is a genuine cancellation that also contains a `than` TOKEN in the predicate's subtree, but
# that is NOT the same as the subtree-walk guard's actual trigger, which required `dep_ == "mark"`.
# Measured: only the relative-clause and concessive cases produce a `mark`; the causal case parses
# `than` as `prep` and the quantmod case as a quantifier, so those two were VACUOUS against the
# guard they were written for — which is exactly why only 2 of the 4 went red at that commit.
# They are kept because they remain useful REGRESSION pins against any broader future guard (a
# token-level or NP-level `than` test would fire on all four), but they must not be described as
# proving coverage of the mark-level trigger.
@pytest.mark.parametrize("sentence, subject, rel, obj", [
    # trailing relative clause carrying an unrelated comparison
    ("I no longer work at Morbex, which pays less than Trantor does.", "user", "works_for", "morbex"),
    # concessive adverbial clause carrying an unrelated comparison
    ("I no longer drink coffee, though I like it more than I like tea.", "user", "drink", "coffee"),
    # causal adverbial clause carrying an unrelated comparison
    ("I no longer visit Morbex, because it costs more than Trantor.", "user", "visit", "morbex"),
    # NP-internal comparative (quantmod) — the only member the old pin covered
    ("I no longer eat more than three meals.", "user", "eat", "meals"),
])
def test_cancellation_survives_an_unrelated_comparative_in_its_subtree(sentence, subject, rel, obj):
    """A cancellation must NOT be vetoed because some other clause in the sentence happens to
    contain a comparison. The subtree-walk guard failed exactly this family: a predicate's subtree
    holds every relative, adverbial and complement clause, so any `than` below the root vetoed the
    cancellation and stored its opposite."""
    _assert_no_affirmed_twin(sentence, subject, rel, obj)


# ── 5. TRAILING "no longer" — Wiktionary's entry carries a clause-final example ────────────────

@pytest.mark.parametrize("obj", _OBJECTS)
def test_trailing_no_longer_is_never_affirmed(obj):
    _assert_no_affirmed_row(f"I drink the {obj} no longer.")


@pytest.mark.parametrize("obj", _OBJECTS)
def test_trailing_no_longer_is_captured_negated(obj):
    assert ("user", "drink", obj, True) in _rows(f"I drink the {obj} no longer.")


# ── 6. COPULAR "no longer", incl. non-attr complements ────────────────────────────────────────

@pytest.mark.parametrize("sentence", [
    "I am no longer at Morbex.",       # prep complement
    "He is no longer here.",           # advmod complement
    "She is no longer my sister.",     # attr complement
])
def test_copular_no_longer_is_detected_as_negated(sentence):
    """Pinned at the PREDICATE, which is the seam the guard owns.

    ⚠️ STATED EXCEPTION, not smoothed over: for "She is no longer my sister." the predicate is
    correctly judged negated but the KINSHIP chain emits (sister, sibling_of, user) AFFIRMED,
    because that emit passes a verb_tok the polarity index does not cover — the same index/emit
    mismatch as the governed-PP case, in a third chain. It is PRE-EXISTING (identical at every arm
    measured) and is NOT fixed here, so this file does not claim the whole-sentence invariant for
    that one sentence. `test_copular_attr_kinship_is_a_known_uncovered_chain` pins the gap so it
    cannot be forgotten."""
    doc = ling._parse(sentence)
    assert doc is not None
    root = next((t for t in doc if t.dep_ == "ROOT"), None)
    assert root is not None
    assert ling._predicate_negated(root) is True, f"not detected as negated: {sentence!r}"


def test_copular_attr_kinship_now_carries_polarity():
    """✅ CLOSED — this pin was a KNOWN-GAP alarm and it fired, so it is tightened exactly as its own
    former docstring instructed ("the assertion should then become `no affirmed row`").
    The kinship chain emits from a copular clause and passes no verb token, so the central adverbial
    polarity flip had no key to match on and a retired kin relation was re-asserted. The flip now
    also admits a predicate complement governed by an already-negated token."""
    rows = _rows("She is no longer my sister.")
    assert rows, "expected at least one row for this parse"
    assert all(r[3] for r in rows), ("kinship cancellation is storing an AFFIRMED row again", rows)


# ── 7. "no doubt" — EMPHATIC AFFIRMATION, must never negate ───────────────────────────────────

@pytest.mark.parametrize("obj", _OBJECTS)
def test_no_doubt_is_an_affirmation_not_a_denial(obj):
    _assert_no_denial(f"I no doubt brew {obj}.")


@pytest.mark.parametrize("sentence", [
    "I no doubt brew quannup.",
    "We ship no matter the cost.",
    "I will go no matter what.",
])
def test_non_comparative_no_adverbial_is_not_predicate_negation(sentence):
    doc = ling._parse(sentence)
    root = next((t for t in doc if t.dep_ == "ROOT"), None)
    assert ling._predicate_negated(root) is False, f"must not read as negation: {sentence!r}"


# ── 8. THE TAGGER COIN FLIP — identical construction must behave identically ──────────────────

@pytest.mark.parametrize("obj", ["coffee", "bread", "chess", "films", "novels",
                                 "tobacco", "magazines", "vitamins"])
def test_no_longer_is_stable_across_the_tagger_wobble(obj):
    """`Degree=Cmp` on "longer" is 5/10 across sentences differing only in the object noun."""
    assert ("user", "drink", obj, True) in _rows(f"I no longer drink {obj}.")


# ── 9. AFFIRMATIVE CONTROLS ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("obj", _OBJECTS)
def test_plain_affirmative_is_untouched(obj):
    rows = _rows(f"I drink {obj}.")
    assert ("user", "drink", obj, False) in rows
    assert not any(r[3] for r in rows), rows


@pytest.mark.parametrize("obj", _OBJECTS)
def test_plain_not_is_captured_negated(obj):
    _assert_no_affirmed_row(f"I do not drink {obj}.")
    assert ("user", "drink", obj, True) in _rows(f"I do not drink {obj}.")


# ── 10. LABEL READING MUST FAIL CLOSED (exclusion beats admission) ────────────────────────────

def test_excluding_label_term_vetoes_an_admitting_one():
    """`description` is FREE PROSE, so an unordered word-set intersection has no precedence and
    FAILS OPEN: a row labelled "Terminative phase verb ending a continuative activity" contains an
    admitting word. That is precisely the grown-row inheritance the fail-safe exists to prevent, so
    an excluding term must VETO regardless of what else the prose mentions."""
    from unittest import mock

    from src.api import linguistic_cue_overlay as lco

    class _Tok:
        lemma_ = "somegrownverb"
        text = "somegrownverb"

    with mock.patch.object(lco, "resolve_aspectual_control_phases",
                           lambda _d: {"somegrownverb":
                                       "Terminative phase verb ending a continuative activity"}):
        assert ling._negation_scopes_into_complement(_Tok()) is False

    with mock.patch.object(lco, "resolve_aspectual_control_phases",
                           lambda _d: {"somegrownverb": "Continuative phase verb"}):
        assert ling._negation_scopes_into_complement(_Tok()) is True


@pytest.mark.parametrize("label", [None, "", "   ", "Unrecognised phase verb", "phase-verb"])
def test_unlabelled_or_unknown_rows_fail_closed(label):
    """A GROWN row with no recognisable label must NOT inherit down-scoping."""
    from unittest import mock

    from src.api import linguistic_cue_overlay as lco

    class _Tok:
        lemma_ = "somegrownverb"
        text = "somegrownverb"

    with mock.patch.object(lco, "resolve_aspectual_control_phases",
                           lambda _d: {"somegrownverb": label}):
        assert ling._negation_scopes_into_complement(_Tok()) is False


# ── 11. THE DB-DOWN FLOOR MUST NOT BE MORE PERMISSIVE THAN THE SEED ───────────────────────────

def test_implicative_floor_matches_the_seed_labels():
    """`_resolve_keyed_map` REPLACES the floor with tenant rows rather than merging it, so a floor
    that is more permissive than the seed makes behaviour differ BY ENVIRONMENT — DB-up declining
    to mark a member that DB-down marks. Migration 196 labels `have` a modal-necessity matrix and
    `manage` without the positive term; neither may scope down from the floor."""
    from src.api import linguistic_cue_overlay as lco
    pol = lco.resolve_implicative_polarities("") or {}
    assert pol, "floor must never be empty"
    assert "positive" not in (pol.get("have") or "").lower(), pol.get("have")
    assert "positive" not in (pol.get("manage") or "").lower(), pol.get("manage")
    assert "positive" in (pol.get("get") or "").lower(), pol.get("get")


def test_aspectual_floor_marks_terminatives_as_terminative():
    from src.api import linguistic_cue_overlay as lco
    phases = lco.resolve_aspectual_control_phases("") or {}
    assert "terminative" in (phases.get("stop") or "").lower()
    assert "terminative" in (phases.get("finish") or "").lower()


# ── 12. KNOWN, PRE-EXISTING: a denied EVENT storing a denial of the KINSHIP ────────────────────

def test_denied_meeting_stores_a_kinship_denial_known_gap():
    """PINNED AS A KNOWN GAP, DELIBERATELY NOT FIXED HERE.

    "I did not meet my sisters Carol and Sarah on May 3rd." denies a MEETING, but the kinship edge
    (sisters, sibling_of, user) is stored NEGATED — the denial of the event has been applied to an
    IDENTITY/KINSHIP binding, which is not the kind of claim the sentence denies. It is the same
    class already handled for `also_known_as` (suppressed under negation), one lane over.

    It is PRE-EXISTING at every arm measured, so it is not a regression from this work, and it sits
    in a chain this change does not otherwise touch. Fixing it under time pressure is exactly how
    the last three over-reaches happened, so it is pinned instead of patched: this test records the
    CURRENT behaviour so the gap stays visible and cannot be quietly forgotten. When it is fixed,
    this pin flips — that is the intended alarm, and the assertion should become
    `_assert_no_denial`-on-the-kinship-edge, not a deletion of the test."""
    rows = _rows("I did not meet my sisters Carol and Sarah on May 3rd.")
    kinship = [r for r in rows if r[1] in ("sibling_of", "sister_of")]
    if not kinship:
        pytest.skip("kinship edge not produced by this parse — nothing to pin here")
    assert any(r[3] for r in kinship), (
        "kinship edge no longer carries the event's denial — GOOD: tighten this pin", rows)


# ── 13. THE CORRELATIVE DISCRIMINATOR IS A CUE CLASS, NOT A PARSE TEST ────────────────────────

@pytest.mark.parametrize("sentence", [
    "No sooner had I bought the quannup than it broke.",   # `than` parses as a clause `mark`
    "No sooner do I sit down than the phone rings.",        # `than` parses as `prep` — same meaning
])
def test_correlative_is_rejected_regardless_of_how_than_parses(sentence):
    """Keying on the ADVERB rather than on `than` also removes a tagger coin flip: the same
    correlative parses `mark` in one sentence and `prep` in another, so every `than`-based guard
    fired on only half the family."""
    _assert_no_denial(sentence)


def test_believe_that_comparative_is_a_known_pre_existing_miss():
    """PINNED AS KNOWN, NOT FIXED. "I no longer believe that coffee is better than tea." stores an
    affirmed edge — and note the edge is not even about the negated predicate; the ccomp is
    mis-parsed into an unrelated state. It measures identically at 6578544e and 2c80231b, so it is
    PRE-EXISTING and independent of the correlative work. Recorded so it stays visible."""
    rows = _rows("I no longer believe that coffee is better than tea.")
    assert not any(r[3] for r in rows), (
        "this construction now carries a denial — GOOD: tighten this pin", rows)


# ── 14. EMPHATIC / EQUATIVE COMPARATIVES — assertions wearing a cancellation's syntax ─────────
# COMPARATIVE FORM DOES NOT ENTAIL COMPARATIVE MEANING. In these the negator scopes over the
# COMPARISON, not the predicate, so the clause ASSERTS its event. They parse token-for-token like
# the cancelling "no longer" (`no`/neg -> ADV advmod Degree=Cmp -> VERB), which is why no parse
# test can separate them and why the discriminator is a cue class.
#
# HOW THIS CLASS WAS MISSED, pinned as much as the behaviour: the earlier round closed the
# NON-comparative half ("no doubt", rejected because lemma == surface) and then enumerated that ONE
# member instead of sweeping the surface. The rule was applied to the member that had burned us,
# not to the class.

@pytest.mark.parametrize("sentence", [
    "I no less enjoy the quannup.",
    "I no less value the zantrils.",
    "She no less respects the vellisk.",
    # BOTH tag-frames of the surface "worse" — they lemmatise DIFFERENTLY (RBR -> `worse`,
    # JJR -> `bad`) and only the JJR half was seeded at first, so the RBR half kept storing a
    # denial. Pinning one frame per surface is not enough when lemmatisation is tag-dependent.
    "I no worse enjoy the quannup.",
    "She no worse respects the vellisk.",
    "The quannup performs no worse.",
    "I no better enjoy the quannup.",
    "The quannup performs no better.",
])
def test_emphatic_comparative_is_an_assertion_not_a_denial(sentence):
    """Measured before the class existed: "no less" stored a DENIAL 3/3 where the parent was
    SILENT — a confident denial of something the speaker asserted."""
    _assert_no_denial(sentence)


@pytest.mark.parametrize("sentence, subject, rel, obj", [
    ("I no longer enjoy the quannup.", "user", "enjoy", "quannup"),
    ("She no longer drinks coffee.", "she", "drink", "coffee"),
    ("I no longer value the zantrils.", "user", "value", "zantrils"),
])
def test_the_emphatic_class_does_not_suppress_real_cancellations(sentence, subject, rel, obj):
    """THE FAMILY THAT SATISFIES THE NEW GUARD'S TRIGGER and must still be admitted — the pin that
    every previous round omitted. These share the emphatic construction's surface exactly."""
    assert (subject, rel, obj, True) in _rows(sentence), _rows(sentence)


def test_no_more_cancellation_is_a_known_pre_existing_miss():
    """PINNED AS KNOWN, NOT FIXED. "I no more drink coffee." is an (archaic) cancellation stored
    AFFIRMED. Measured identically at 6578544e, 2c80231b and c4857e6b, so it is PRE-EXISTING and
    independent of the emphatic class — the parse does not route it through the negated-advmod path
    at all. Recorded so it stays visible rather than being mistaken for fallout from this round."""
    rows = _rows("I no more drink coffee.")
    assert not any(r[3] for r in rows), (
        "this construction now carries a denial — GOOD: tighten this pin", rows)



# ── 15. THE ARCHITECTURAL INVERSION: admit the CLOSED set, reject the OPEN one ────────────────
# The division is CONTINUATIVE/CANCELLING ("no longer", "no more" — closed, two members) versus
# COMPARATIVE-SCOPE (every other negated comparative — open and PRODUCTIVE, the event is ASSERTED).
# Five rounds enumerated the OPEN side and each shipped a falsehood, because a productive class
# cannot be finished. These pins hold the inversion from both directions.

@pytest.mark.parametrize("sentence", [
    # the rows a critic measured as WORSE than the pre-lane parent
    "The train arrived no earlier.",
    "He works no harder.",
    "The engine runs no faster.",
    "She no earlier attends the meetings.",
    "I no fewer enjoy the quannup.",
    # the productive remainder no enumeration would have reached
    "The quannup performs no higher.",
    "The quannup sells no cheaper.",
    "He travels no farther.",
    "The engine runs no louder.",
    "The quannup grows no smaller.",
    "The engine runs no slower.",
    # the former "emphatic" members — same rule now covers them, no class needed
    "I no less enjoy the quannup.",
    "I no worse enjoy the quannup.",
    "The quannup performs no worse.",
    "I no better enjoy the quannup.",
])
def test_comparative_scope_asserts_its_event(sentence):
    """OPEN SIDE: the negator scopes over the COMPARISON, so the event happened. Storing a denial
    here asserts the opposite of what was said. No member list is required — anything outside the
    closed continuative set lands here by construction."""
    _assert_no_denial(sentence)


@pytest.mark.parametrize("sentence, subject, rel, obj", [
    ("I no longer drink coffee.", "user", "drink", "coffee"),
    ("I no longer enjoy the quannup.", "user", "enjoy", "quannup"),
    ("She no longer respects the vellisk.", "she", "respect", "vellisk"),
    ("No longer will I work at Morbex.", "user", "works_for", "morbex"),
    ("No longer did I drink coffee.", "user", "drink", "coffee"),
    ("I drink the coffee no longer.", "user", "drink", "coffee"),
    ("I no longer eat more than three meals.", "user", "eat", "meals"),
])
def test_closed_set_cancellations_survive_the_inversion(sentence, subject, rel, obj):
    """CLOSED SIDE, AND THE WHOLE RISK OF THE INVERSION. Every cancellation form fixed across five
    rounds — mid, fronted, trailing, third-person, NP-internal `than` — must still be negated."""
    assert (subject, rel, obj, True) in _rows(sentence), _rows(sentence)


@pytest.mark.parametrize("sentence, subject, rel, obj", [
    ("I no longer work at Morbex, which pays less than Trantor does.", "user", "works_for", "morbex"),
    ("I no longer drink coffee, though I like it more than I like tea.", "user", "drink", "coffee"),
    ("I no longer visit Morbex, because it costs more than Trantor.", "user", "visit", "morbex"),
])
def test_multiclause_cancellations_survive_the_inversion(sentence, subject, rel, obj):
    _assert_no_affirmed_twin(sentence, subject, rel, obj)


def test_continuative_class_resolves_and_is_registered():
    """Registration in the bootstrap category map is LOAD-BEARING: an unregistered category
    silently resolves the NAMING-VERB floor. Here that would be catastrophic in a new way — the
    class ADMITS, so a wrong floor decides which sentences count as cancellations at all."""
    from src.api import linguistic_cue_overlay as lco
    members = lco.resolve_continuative_adverbs("")
    assert members, "continuative class must never resolve empty (bootstrap floor)"
    # LEMMAS measured per tag: "longer" -> long (RB and RBR); "more" -> more (JJR and RBR)
    assert {"long", "more"} <= set(members), members
    naming = lco.resolve_cues("", None, lco.NAMING_VERB_CATEGORY)
    assert not (set(members) & set(naming)), (
        "continuative_adverb resolved the NAMING-VERB floor — category not registered in "
        "_BOOTSTRAP_BY_CATEGORY", set(members) & set(naming))


def test_continuative_membership_matches_what_the_lemmatiser_emits():
    """Seed what the lemmatiser ACTUALLY emits, per tag — the lesson from "worse", whose lemma is
    tag-dependent. A member that matches nothing reads as coverage while the surface keeps storing
    the wrong polarity."""
    from src.api import linguistic_cue_overlay as lco
    members = set(lco.resolve_continuative_adverbs(""))
    missing = []
    for surface, sentence in [("longer", "I no longer drink coffee."),
                              ("longer", "I no longer enjoy the quannup."),
                              ("longer", "No longer will I work at Morbex."),
                              ("more", "No more do I drink coffee.")]:
        doc = ling._parse(sentence)
        tok = next((t for t in doc if t.text == surface), None)
        assert tok is not None, sentence
        if (tok.lemma_ or "").strip().lower() not in members:
            missing.append((surface, tok.lemma_, tok.tag_, sentence))
    assert not missing, ("lemma(s) emitted in the cancellation construction are NOT members — "
                         "those cancellations will read as affirmed", missing)



def test_superseded_enumeration_classes_are_gone_not_dead():
    """The correlative and emphatic classes were enumerations of the OPEN side and are now
    subsumed. Dead classes are deleted rather than kept: a resolver nobody calls is the
    'built and dark' state this repo already has a rule about."""
    from src.api import linguistic_cue_overlay as lco
    assert not hasattr(lco, "resolve_correlative_adverbs")
    assert not hasattr(lco, "resolve_emphatic_adverbs")
    assert not hasattr(ling, "_correlative_adverbs")
    assert not hasattr(ling, "_emphatic_adverbs")


# ── 16. THE SET IS CLOSED OVER LEMMAS BUT OPEN OVER FRAMES ────────────────────────────────────
# `no longer` in a SCALAR-LIMIT frame takes an explicit STANDARD OF COMPARISON and ASSERTS its
# event — "The meeting lasted no longer than an hour" means the meeting DID last. Its adverbial
# still lemmatises to the admitted member, so MEMBERSHIP ALONE admits it as a cancellation and
# stores a denial of something asserted. Lexical closure is not construction closure.
#
# ⚠️ THESE PINS EXIST BECAUSE THE GUARD WAS DELETED ON AN ASSERTED SUBSUMPTION. The commit claimed
# the membership test "SUBSUMES and replaces the `than`-child scalar-limit guard". MEASURED, driving
# the old guard's own cases against the membership rule alone scored 4/10. Subsumption is a
# MEASURABLE claim and must never be asserted from reading.
#
# ⚠️ AND THE OLD PIN NAMED AFTER THAT GUARD WAS VACUOUS AGAINST IT: it drove only "no further
# than …", whose adverbial lemmatises to `far` — NOT a member — so both cases were rejected by
# MEMBERSHIP for a reason unrelated to `than`. The class the guard actually protects is
# MEMBER-LEMMA × STANDARD-OF-COMPARISON, and it had no pin at all. That is pinned here.

@pytest.mark.parametrize("sentence", [
    # PHRASAL standard: `than` is a `prep` child of the adverbial
    "The meeting lasted no longer than an hour.",
    "She stayed no longer than necessary.",
    "He waited no longer than ten minutes.",
    "I slept no longer than six hours.",
    "The delay lasted no longer than a day.",
    # CLAUSAL standard: an `advcl` child of the adverbial, marked by `than` (reduced comparative)
    "The call ran no longer than expected.",
    "It took no longer than anticipated.",
])
def test_member_lemma_with_a_standard_of_comparison_is_a_limit_not_a_denial(sentence):
    """MEMBER LEMMA × STANDARD OF COMPARISON — the family the deleted guard protected, driven for
    the first time. Both attachment shapes are covered because keying only on the phrasal one left
    the reduced comparative clause storing a denial (measured 9/10 before the clausal shape)."""
    _assert_no_denial(sentence)


@pytest.mark.parametrize("sentence, subject, rel, obj", [
    # the standard hangs off the OBJECT's "more", not off the adverbial -> genuine cancellation
    ("I no longer eat more than three meals.", "user", "eat", "meals"),
])
def test_a_standard_elsewhere_in_the_clause_does_not_block_the_cancellation(sentence, subject, rel, obj):
    """The guard is scoped to the ADVERBIAL'S OWN CHILD, never a subtree walk — that was the
    round-4 failure, where any comparative anywhere below the root vetoed the cancellation."""
    assert (subject, rel, obj, True) in _rows(sentence), _rows(sentence)


# ── 17. THE REACHABLE OPERATOR ERROR IS PARTIAL DEACTIVATION, AND IT MUST BE LOUD ─────────────

def test_partial_deactivation_keeps_cancellations_and_is_logged_loudly():
    """⚠️ THE DEGRADATION PRODUCTION CAN ACTUALLY REACH.

    An earlier revision guarded the EMPTY class. That state is UNREACHABLE: `resolve_cues` already
    substitutes the bootstrap floor when a tenant category resolves empty, so the resolver never
    returns empty in production and the branch could only fire under monkeypatch — which is exactly
    what its own pin did. A fail-safe for an impossible state is decoration, and it hid this one.

    PARTIAL deactivation is reachable by a routine edit: with one member active the resolution is
    non-empty, no floor substitution happens, and every cancellation using the missing member
    silently stores an AFFIRMATION — re-asserting the habit the user just cancelled, with no error
    and no log.

    This pin asserts BOTH halves — the cancellation survives, AND the fault is announced via
    `log_crit`. The previous pin's name promised "loudly" and asserted nothing about it."""
    from unittest import mock

    from src.api import linguistic_cue_overlay as lco
    from src.api import logging_config

    calls = []

    def _spy(_logger, msg, **kw):
        calls.append((msg, kw))

    with mock.patch.object(lco, "resolve_continuative_adverbs", lambda _d: frozenset({"more"})), \
            mock.patch.object(logging_config, "log_crit", _spy):
        rows = _rows("I no longer drink tea.")

    assert ("user", "drink", "tea", True) in rows, (
        "partial deactivation silently turned a cancellation into an affirmation", rows)
    assert any("continuative_adverb_class_incomplete" in m for m, _ in calls), (
        "the fault must be announced via log_crit, not swallowed", calls)
    missing = [kw.get("missing") for m, kw in calls if "incomplete" in m]
    assert missing and "long" in (missing[0] or []), (
        "the log must name the missing member so an operator can act on it", calls)


# ── 18. PRE-EXISTING DEFECTS, PINNED AS KNOWN (found by review, NOT fixed in this round) ──────
# Each measures identically at every arm, so none is fallout from the negation work. They are
# pinned so they stay visible; when one is fixed its pin flips, which is the intended alarm.

def test_cancelled_identity_no_longer_writes_an_affirmed_l4_place_rung():
    """✅ CLOSED — THIS PIN WAS A KNOWN-GAP ALARM AND IT FIRED, so it is now tightened as its own
    former docstring instructed. "She is no longer the manager." used to store
    (she, instance_of, manager) AFFIRMED — not merely a wrong state edge but an L4 PLACE rung that
    the ladder WALKS, asserting the very identity the speaker cancelled (THE HARD LINE-adjacent, and
    the most serious of the three known gaps recorded here).

    The cause was never a missed negation: `_predicate_negated` returns True on the copula. The
    copular/identity chain passes NO verb token, so the central adverbial polarity flip — which keys
    on a verb index — had nothing to match on, and the signal was DROPPED AT THE EMITTER. The flip
    now also admits a predicate complement (`attr`/`acomp`) governed by an already-negated token.
    The assertion is inverted per the original instruction: NO affirmed ladder rung may survive."""
    rows = _rows("She is no longer the manager.")
    inst = [r for r in rows if r[1] in ("instance_of", "subclass_of")]
    assert inst, ("expected a classification edge for this parse", rows)
    assert all(r[3] for r in inst), (
        "a cancelled identity is writing an AFFIRMED L4 ladder rung again", rows)


def test_known_gap_parenthetical_defeats_the_punctuation_scope_guard():
    """"I no longer, as you know, drink coffee." is stored AFFIRMED at every arm. A comma PAIR
    between cue and verb defeats the BIOSCOPE punctuation-scope test — and it fails in the UNSAFE
    direction, re-affirming a cancelled habit rather than going silent."""
    rows = _rows("I no longer, as you know, drink coffee.")
    assert not any(r[3] for r in rows), (
        "the parenthetical no longer defeats the scope guard — GOOD: tighten this pin", rows)


def test_cue_lemma_is_never_minted_as_an_entity():
    """FIXED — this pin is INVERTED from the known-gap form it had last round, deliberately.

    It previously asserted that "He is no longer here." mints the CUE LEMMA as an entity
    (he, has_state, 'long'), pinned as a pre-existing defect. The root fix for the copular-limit
    regression resolves it as a consequence: the object there IS the negation cue's host adverbial,
    and the cue-token guard now refuses that emission before any polarity is assigned. Rather than
    leaving a pin asserting a defect that no longer exists, it now pins the correct behaviour."""
    for sentence in ("He is no longer here.",
                     "She is no longer available.",
                     "The wait was no longer than an hour."):
        rows = _rows(sentence)
        assert not any(r[2] in ("long", "longer", "more") for r in rows), (
            f"the negation cue's own lemma was minted as a memory object: {sentence!r} -> {rows}")


# ── 19. COPULAR LIMITS — the head matters, and the object was the cue itself ──────────────────

@pytest.mark.parametrize("sentence", [
    "The wait was no longer than an hour.",
    "The queue was no longer than usual.",
    "The delay was no longer than expected.",
    "The talk was no longer than scheduled.",
])
def test_copular_limit_emits_no_cue_token_object(sentence):
    """⚠️ FIXED AT THE ROOT, NOT AT THE POLARITY.

    A guard keyed on the adverbial's CHILDREN never asks what its HEAD is. Under a lexical verb
    there is a real event to assert, so exempting a standard-of-comparison is right. Under a COPULA
    there is no separate event — the copular chain emits THE ADVERBIAL'S OWN LEMMA as the object —
    so the exemption instead asserted the gradable property the speaker was explicitly capping.

    But the object was `long`: the negation cue's own lemma, minted as a memory node. That row is
    JUNK IN BOTH POLARITIES, so choosing a sign for it is treating the symptom. The fix suppresses
    the emission at the `_emit` chokepoint, ahead of every polarity flip. Silence, not a fabricated
    sign — which also matters because an mcp turn is user_stated provenance and lands Class A at
    confidence 1.0, DURABLE: a fabricated affirmation is recalled later as the user's own words."""
    rows = _rows(sentence)
    assert not any(r[2] in ("long", "longer", "more") for r in rows), (
        "the negation cue's own lemma was minted as a memory object", rows)


def test_verb_headed_limit_still_asserts_its_real_event():
    """The other side of the same coin: under a lexical verb there IS an event, and the
    standard-of-comparison exemption must still let it land affirmed."""
    rows = _rows("The meeting lasted no longer than an hour.")
    assert ("meeting", "has_state", "last", False) in rows, rows


# ── 20. STRANDED-`to` STANDARDS — the shape a "both shapes" claim missed ──────────────────────

@pytest.mark.parametrize("sentence", [
    "I stayed no longer than I had to.",
    "The rope stretched no longer than it had to.",
    "He waited no longer than he had to.",
    "The meeting lasted no longer than it needed to.",   # ccomp shape — already worked
])
def test_stranded_to_standard_is_a_limit_not_a_denial(sentence):
    """`than` heads an ELIDED clause on a stranded infinitival `to` hanging off the SAME predicate
    as the adverbial, so it is not a child of the adverbial at all and both child-shapes see
    nothing. Covered by ONE sibling hop — bounded, and still not a subtree walk."""
    _assert_no_denial(sentence)


def test_the_sibling_hop_does_not_veto_multiclause_cancellations():
    """THE RISK OF THE SIBLING HOP, pinned. Widening from children to siblings is exactly how the
    round-4 subtree walk started eating cancellations, so the multi-clause cancellations that carry
    an unrelated comparative in a sibling clause are driven explicitly.

    ⚠️ THE FIRST THREE CASES ARE STRUCTURALLY INERT FOR THIS HOP AND ARE KEPT ONLY AS CONTROLS.
    Their comparative sits in a SUBORDINATE clause (`which pays less…`, `though I like it more…`),
    so it is not a child of the cancelling adverbial's host at all and the hop can never fire on
    them — they passed for a reason unrelated to the guard they name, which is the same vacuous-pin
    trap this file has hit before. The SAME-HOST cases below are the ones that actually exercise it:
    there the comparative IS a clause-mate competing for the `than` standard, which is precisely the
    ambiguity the ownership check must resolve. Before this round the hop attached the standard by
    shared host and STOLE it from the competing degree head, re-asserting a cancelled habit."""
    for sentence, subject, rel, obj in [
        # CONTROLS — comparative in a subordinate clause; the hop is unreachable here.
        ("I no longer work at Morbex, which pays less than Trantor does.", "user", "works_for", "morbex"),
        ("I no longer drink coffee, though I like it more than I like tea.", "user", "drink", "coffee"),
        ("I no longer visit Morbex, because it costs more than Trantor.", "user", "visit", "morbex"),
        # ⚠️ THE SAME-HOST CASES THAT LIVED HERE HAVE MOVED, AND THE MOVE IS THE POINT.
        # Round 10 added "I no longer worry more than I need to." here expecting a DENIAL. That
        # expectation was a falsehood — the sentence cancels the EXCESS, not the worrying — and it
        # is now pinned, affirmed, by test_a_cancelled_comparative_excess_is_not_a_denial_of_the_
        # predicate. What remains here is what this pin was always named for: a comparative in a
        # SUBORDINATE clause must not touch the main-clause cancellation.
    ]:
        assert (subject, rel, obj, True) in _rows(sentence), (sentence, _rows(sentence))


def test_stranded_to_scalar_limits_stay_asserted():
    """THE TOKEN-IDENTITY REGRESSION GUARD. The competitor check that resolves the same-host
    ambiguity above was first written as `_o is not c`. spaCy mints a FRESH Token wrapper per
    access, so that is True even for `c` ITSELF — the adverbial became its own competitor, the hop
    never fired, and these asserted scalar limits flipped to stored DENIALS of events the speaker
    stated. Identity for a spaCy token is `.i`, never `is`. If this goes red, check for an `is`
    comparison on tokens before touching anything else."""
    for sentence, subject, rel, obj in [
        ("I stayed no longer than I had to.", "user", "has_state", "stay"),
        ("The rope stretched no longer than it had to.", "rope", "has_state", "stretch"),
    ]:
        assert (subject, rel, obj, False) in _rows(sentence), (sentence, _rows(sentence))


def test_copular_comparative_complement_emits_no_cue_token_object():
    """The `attr`/`acomp` sibling of the already-pinned copular limits. "The wait was no more than
    an hour." heads `more` as an ADJ `attr`, NOT an `advmod`, so the cue-token-object index never
    saw it and the degree word was minted as a memory node — junk in BOTH polarities (affirmed it
    asserts the very property the speaker capped; negated it denies one nobody named).
    The forbidden set is DERIVED FROM THE PARSE, never a word list: any token bearing a `neg` child
    is the cue host and its form must never be an emitted object."""
    for sentence in [
        "The wait was no more than an hour.",       # PHRASAL standard: `than` a prep child
        "The delay was no more than expected.",     # CLAUSAL standard: `than` a mark GRANDchild
    ]:
        doc = ling._parse(sentence)
        hosts = set()
        for t in doc:
            if any(g.dep_ == "neg" for g in t.children):
                hosts.add((t.lemma_ or "").strip().lower())
                hosts.add((t.text or "").strip().lower())
        rows = _rows(sentence)
        junk = [r for r in rows if (r[2] or "").strip().lower() in hosts]
        assert not junk, (sentence, junk, rows)


def test_copular_plain_negation_is_captured_not_suppressed():
    """⚠️ THE LINE A SUPPRESSION FIX WOULD HAVE CROSSED, pinned so it cannot be crossed later.
    "The situation is no different." shares a SYMPTOM with the case above (the object is the token
    bearing the `neg`) but is a DIFFERENT construction: `different` is JJ (positive degree) with no
    standard of comparison — the predicate the speaker actually asserted, negatively. Suppressing it
    to satisfy a "no emitted object may be the cue's host" rule DELETES A TRUE USER FACT. The
    discriminators are comparative DEGREE (JJR/RBR -> comparative-scope, event asserted) and the
    `than` STANDARD (-> scalar limit). Positive degree, no standard -> ordinary negation, EMIT it."""
    for sentence, subject, rel, obj in [
        ("The situation is no different.", "situation", "has_state", "different"),
        ("The result is no different.", "result", "has_state", "different"),
    ]:
        assert (subject, rel, obj, True) in _rows(sentence), (sentence, _rows(sentence))


def test_bare_comparative_complement_stays_asserted():
    """The regression the `than`-only discriminator caused, pinned. A BARE comparative complement
    with no standard ("grows no smaller") is still comparative-scope — the negator scopes over the
    COMPARISON and the event is ASSERTED. Keying the copular branch on the standard alone turned
    this into a stored denial; the comparative TAG is the primary discriminator."""
    rows = _rows("The quannup grows no smaller.")
    assert ("quannup", "has_state", "grow", False) in rows, rows


def test_partial_deactivation_alert_is_rate_limited():
    """The guard is correct but sits in a PER-CALL resolver: one misconfiguration emitted ~12
    CRITICAL lines for a 3-sentence turn, indefinitely. This system has a documented production
    event-loop stall caused by its own logging, so an unbounded CRITICAL on a hot path is a hazard.
    The condition is a persistent misconfiguration, not an event stream — one line per distinct
    missing-set per interval is what an operator needs."""
    from unittest import mock

    from src.api import linguistic_cue_overlay as lco
    from src.api import logging_config

    calls = []
    ling._CONTINUATIVE_ALERT_SEEN.clear()

    with mock.patch.object(lco, "resolve_continuative_adverbs", lambda _d: frozenset({"more"})), \
            mock.patch.object(logging_config, "log_crit",
                              lambda _l, m, **kw: calls.append(m)):
        for _ in range(8):
            ling._continuative_adverbs()

    emitted = [c for c in calls if "continuative_adverb_class_incomplete" in c]
    assert len(emitted) == 1, f"8 resolutions must emit ONE alert, got {len(emitted)}"


def test_differential_comparatives_emit_no_positive_degree_row():
    """⚠️ THIS PIN REPLACES ONE THAT ASSERTED A FALSEHOOD, AND THE REVERSAL IS THE LESSON.
    Its predecessor (`test_lexical_comparative_complements_are_not_deleted`) demanded a ROW for
    "The exam was no harder than the practice test.", on the reading that suppressing it deleted
    user content. Measured, the row it demanded is a stored falsehood: the negated sentence and its
    un-negated twin produced BYTE-IDENTICAL rows, polarity flag included — `no` annihilated — and
    with a marked antonym it asserts the opposite of the speaker ("no younger than" -> `young`).

    In `X is no ADJ-er than Y`, `no` is not sentential negation but a DIFFERENTIAL (measure-phrase)
    degree quantifier, "by no margin" — Makri (2018), *Aspects of Comparative Constructions*:
    "the negation in the comparative is not the same as sentential negation but a negative degree
    quantifier" ("Sarah is no taller (than ...)"). (An earlier version of this docstring cited
    "Kennedy & Rett"; that attribution was wrong — the paper of that name is Alrenga & Kennedy
    2014, NLS 22:1-53 — and it was asserted from memory rather than checked.) Entailing only
    diff(X,Y) <= 0. It does not license the positive-degree predication, so no sign is correct for
    that row and the honest output is none — which is what the advmod arm always did.

    TWO-SIDED so it cannot pass on blanket silence: the negated form must emit no positive-degree
    row AND the un-negated twin must still emit one."""
    for negated_form, plain_form, subject in [
        ("The exam was no harder than the practice test.", "The exam was harder than the practice test.", "exam"),
        ("The applicant is no younger than the retiree.", "The applicant is younger than the retiree.", "applicant"),
        ("The tariff is no lower than last quarter.", "The tariff is lower than last quarter.", "tariff"),
        ("The wait was no less than an hour.", "The wait was less than an hour.", "wait"),
    ]:
        neg_rows, pos_rows = _rows(negated_form), _rows(plain_form)
        assert not [r for r in neg_rows if r[0] == subject and r[1] == "has_state"], \
            (negated_form, neg_rows)
        assert any(r[0] == subject for r in pos_rows), \
            ("un-negated twin went silent too — this pin has gone vacuous", plain_form, pos_rows)


def test_a_cancelled_comparative_excess_is_not_a_denial_of_the_predicate():
    """⚠️ THIS PIN REPLACES ONE THAT ASSERTED A FALSEHOOD — the second time this file has had to.
    Its predecessor demanded a NEGATED row for "I no longer worry more than I need to.", on the
    reading that the habit was cancelled. That is the scope fallacy: NEG(P AND Q) does not entail
    NEG(P). The sentence cancels the EXCESS, not the worrying — the speaker still worries, just not
    more than they need to — so the denial asserts something they did not say. Measured, four of
    five such sentences emitted the BARE predicate and were storing exactly that falsehood, having
    replaced a row that was merely true-but-incomplete.

    The rule is now: DENY ONLY IF THE SCOPED MATERIAL SURVIVED INTO THE ROW. Its paired test below
    covers the case where it does, so this cannot be satisfied by never denying anything."""
    for sentence, subject, rel, obj in [
        ("I no longer worry more than I need to.", "user", "has_state", "worry"),
        ("I no longer sleep more than I need to.", "user", "has_state", "sleep"),
        ("I no longer work harder than I have to.", "user", "has_state", "work"),
        ("I no longer travel more often than I have to.", "user", "has_state", "travel"),
        ("I no longer worry more frequently than I need to.", "user", "has_state", "worry"),
    ]:
        assert (subject, rel, obj, False) in _rows(sentence), (sentence, _rows(sentence))


def test_a_surviving_comparative_is_still_denied():
    """The other side of the rule: when the comparative material SURVIVES into the emitted triple,
    the denial is correct — "no longer work MORE HOURS" truly denies working more hours."""
    rows = _rows("I no longer work more hours than I have to.")
    assert ("user", "work", "more hours", True) in rows, rows


def test_a_comparative_in_a_subordinate_clause_does_not_cancel_the_denial():
    """The scope rule must not reach into a SUBORDINATE clause. In "I no longer drink coffee, though
    I like it more than I like tea." the comparative belongs to the concessive clause and scopes
    over nothing in the main clause; standing the denial down there re-asserted the very habit the
    speaker retired. Descent is limited to a phrase-level gradable head (the periphrastic
    "more frequently" shape), never into a clause."""
    rows = _rows("I no longer drink coffee, though I like it more than I like tea.")
    assert ("user", "drink", "coffee", True) in rows, rows


def test_adjective_selected_than_is_not_a_comparative_standard():
    """`different` SELECTS a than/from complement without being comparative, so testing for a
    `than` standard on a POSITIVE-degree complement turned "no different than before" into an
    affirmation of the difference the speaker denied. The `from` variant staying correct is what
    exposed it as a selection property of the adjective, not a comparative frame."""
    for sentence, subject in [
        ("The situation is no different than before.", "situation"),
        ("The policy is no different from the old one.", "policy"),
    ]:
        assert (subject, "has_state", "different", True) in _rows(sentence), (sentence, _rows(sentence))


def test_coordinated_comparatives_share_one_standard():
    """The stranded standard hangs off the COORDINATE, not the host, so a host-children-only scan
    found no standard at all and stored a DENIAL of an event the speaker asserted."""
    rows = _rows("The rope stretched no longer and no tighter than it had to.")
    assert ("rope", "has_state", "stretch", False) in rows, rows


def test_copular_nominal_cessation_is_negated_not_reasserted():
    """A stale role RE-ASSERTED at the exact moment the speaker retired it. The possessive and
    instance_of chains emit from a copular clause and pass NO verb token, so the central adverbial
    polarity flip — which keys on a verb index — had nothing to match on. The negation was never
    missed: `_predicate_negated` returns True on the copula for every one of these. It was computed
    correctly and DROPPED AT THE EMITTER for want of a key. Scoped to predicate complements
    (attr/acomp) so it cannot reach a dobj, and therefore cannot touch the aspectual/implicative
    descent where negation does not scope uniformly."""
    for sentence, subject, rel, obj in [
        ("She is no longer my manager.", "user", "owns", "manager"),
        ("She is no longer my doctor.", "user", "owns", "doctor"),
        ("He is no longer a student.", "he", "instance_of", "student"),
        ("He is no longer the captain.", "he", "instance_of", "captain"),
    ]:
        assert (subject, rel, obj, True) in _rows(sentence), (sentence, _rows(sentence))


def test_copular_nominal_affirmatives_are_not_over_negated():
    """The paired positive assertion: the flip above must not blanket-negate the construction."""
    for sentence, subject, rel, obj in [
        ("She is my manager.", "user", "owns", "manager"),
        ("He is a student.", "he", "instance_of", "student"),
    ]:
        assert (subject, rel, obj, False) in _rows(sentence), (sentence, _rows(sentence))


def test_comparative_detection_does_not_depend_on_the_tagger_alone():
    """Two fragilities that each sat one decision away from a case that already worked.

    TAG: spaCy tags `later` in "I no longer stay later than I have to." as plain RB with EMPTY
    morph, while `more`/`longer` in the same frame come back RBR with Degree=Cmp. UD's *Comparative
    Constructions* confirms the tag and the feature are the SAME signal — every PTB JJR carries
    Degree=Cmp — so they miss together. The independent signal is structural: a token that OWNS a
    `than` standard is a comparative head whatever the tagger called it (a comparative subclause is
    licensed only by a comparative; Bacskai-Atkari, *On the Nature of Comparative Subclauses*).

    DESCENT: the scope guard descended only into an ADV/ADJ child, which skipped the NP-INTERNAL
    comparative (`longer` as `amod` of the noun `hours`) and stored a denial that the speaker works.
    The boundary that matters is CLAUSAL, not part-of-speech.

    Expectations are MIXED on purpose — the same rule must deny one and affirm the others, so a
    blanket answer in either direction fails this test."""
    for sentence, subject, rel, obj, want_negated in [
        ("I no longer stay later than I have to.", "user", "has_state", "stay", False),
        ("I no longer work longer hours than I have to.", "user", "has_state", "work", False),
        ("I no longer work more hours than I have to.", "user", "work", "more hours", True),
    ]:
        assert (subject, rel, obj, want_negated) in _rows(sentence), (sentence, _rows(sentence))


def test_finite_comparative_clause_on_the_degree_head_is_not_a_denial():
    """The standard hangs off the DEGREE HEAD, never off the verb: in "I no longer stay later than
    the janitor does." `does` is an `advcl` child of `later`. The scalar-limit test scanned the
    VERB's children only, found no standard, and stored a DENIAL that the speaker stays — one
    clause-shape away from the already-pinned elided variant ("...than I have to."), which was
    correct all along.

    The instructive part: the shared helper DID detect the standard on `later`, while a stale INLINE
    DUPLICATE of that test three lines away still asked the verb's siblings — so the right predicate
    existed and was wired to the wrong question. A docstring claiming two sites "share this helper
    so they cannot drift apart" proves nothing until the call sites are grepped."""
    for sentence, subject, rel, obj in [
        ("I no longer stay later than the janitor does.", "user", "has_state", "stay"),
        ("I no longer proofread more carefully than the deadline allows.", "user", "has_state", "proofread"),
        ("I no longer rehearse more slowly than the score demands.", "user", "has_state", "rehearse"),
        ("I no longer bid more aggressively than the budget permits.", "user", "has_state", "bid"),
        ("I no longer stay later than I have to, rather than leaving early.", "user", "has_state", "stay"),
    ]:
        assert (subject, rel, obj, False) in _rows(sentence), (sentence, _rows(sentence))


def test_periphrastic_copular_differential_fails_neither_way():
    """spaCy attaches `no` inconsistently across two instances of ONE construction, and the two
    attachments were failing in OPPOSITE directions: "no more reliable" put `no` on `more` and
    ANNIHILATED the negation (byte-identical to the un-negated sentence), while "no more generous"
    put it on the adjective and stored a DENIAL. Same words, same shape, opposite falsehoods —
    decided by a parser coin-flip. Both are differential comparatives and neither licenses the
    positive-degree predication, so both must emit no such row. Two-sided so blanket silence fails."""
    for negated_form, plain_form, subject in [
        ("The prototype is no more reliable than the old one.", "The prototype is more reliable than the old one.", "prototype"),
        ("The grant was no more generous than last year.", "The grant was more generous than last year.", "grant"),
    ]:
        nrows, prows = _rows(negated_form), _rows(plain_form)
        assert not [r for r in nrows if r[0] == subject and r[1] == "has_state"], (negated_form, nrows)
        assert any(r[0] == subject for r in prows), ("twin went silent — pin is vacuous", plain_form, prows)


def test_prenominal_comparatives_keep_their_type_row():
    """⚠️ REPLACES A PIN THAT WAS BOTH VACUOUS AND GUARDING HARM, and the pairing is the lesson.
    Its predecessor asserted only the ABSENCE of an instance_of row, on two sentences that emitted
    NOTHING at the parent either — so it could not fail whatever the code did. Behind that green
    pin, the guard it was written for was DELETING USER CONTENT in both polarities.

    The guard assumed whichever token owns the `than` standard is the comparative. That holds in UD
    and NOT in the parser this runs on: measured on UD's own example ("A more difficult problem than
    you thought."), UD hangs the than-clause off `difficult` while spaCy hangs it off `problem` —
    the head NOUN, which is the legitimate type. So the guard ate "Nora is a faster runner than
    Devon." and, worse, the correct DENIAL in "He is no more a plumber than I am."

    Every case here DEMANDS a row and the last demands a NEGATED one, so neither silence nor a
    blanket polarity can satisfy this test."""
    for sentence, subject, rel, obj, want_negated in [
        ("Nora is a faster runner than Devon.", "nora", "instance_of", "faster runner", False),
        ("Sarah is a better cook than her sister.", "sarah", "instance_of", "better cook", False),
        ("Rex is a bigger dog than Max.", "rex", "instance_of", "bigger dog", False),
        ("He is no more a plumber than I am.", "he", "instance_of", "plumber", True),
    ]:
        assert (subject, rel, obj, want_negated) in _rows(sentence), (sentence, _rows(sentence))
