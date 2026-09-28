"""Pins for `linguistics.analyze_negation_cues` — the cue classes the `neg`-arc lane cannot see.

WHY THIS FILE EXISTS. Scored on the perfect-flow fixture set (n=68) at `7a87f17b`, cue detection
was PRECISION 0.9744 / RECALL 0.5000, and the recall loss was two classes at ABSOLUTE ZERO plus one
truncated span: determiner negation (`no`/`nothing`/`none`, 0/7), correlative negation
(`neither … nor`, 0/5) and the multiword continuative (`no longer`/`no more`, 13/28 tokens — the
negator fired, the adverb never joined it). `analyze_negation_cues` adds those three lanes.

PRECISION IS THE BINDING CONSTRAINT. A cue invented here becomes a STORED DENIAL of something the
user asserted, which is worse than a miss. So every "must stay silent" pin below is written as a
TWIN: the silent arm AND a minimally-different arm that must still FIRE. An absence-only assertion
goes vacuous the moment something upstream (a missing spaCy model, a flag, an exception fail-safe)
silences the whole seam, and would then pass while pinning nothing.

`tests/` is gitignored but has tracked files — add with `git add -f`.
"""
import os

import pytest

from src.extraction import linguistics as L
from src.extraction.linguistics import analyze_negation_cues, linguistics_available

pytestmark = pytest.mark.skipif(
    not linguistics_available(),
    reason="spaCy en_core_web_sm not available — the cue seam is fail-safe to no cues.",
)


def cues(text):
    """Cue surfaces only. `None` (parse-miss fail-safe) is normalised so a pin never crashes on it,
    but it is NOT collapsed into `[]`: a fail-safe `None` returns the sentinel so an assertion that
    expected silence cannot be satisfied by the layer having died."""
    r = analyze_negation_cues(text)
    if r is None:
        return ["<SEAM-FAILSAFE-NONE>"]
    return [c for c, _ in r]


def scope_of(text, cue):
    for c, s in (analyze_negation_cues(text) or []):
        if c == cue:
            return s.split()
    return None


# ── LANE 2: DETERMINER / NEGATIVE-INDEFINITE ────────────────────────────────────────────────────
# CGEL Ch.5 §7.8 "The negative determinatives no and none": with count heads `no` indicates that
# "not one member of the set under consideration has the predication property" — the determiner
# negates the PREDICATION, so the scope is the clause. ConanDoyle-neg (Morante & Daelemans, LREC
# 2012) example (3d) annotates exactly this as a cue class, and (3e) the negative pronoun.
@pytest.mark.parametrize("text,cue", [
    ("the brindaw has no quenderal.", "no"),          # determiner on the direct object
    ("no vestrow calls at tramsett on mondays.", "no"),   # determiner on the SUBJECT NP
    ("there is no dremmage on my krellin.", "no"),    # existential-there
    ("morlisk knows nothing about the brindaw.", "nothing"),   # negative pronoun
    ("the cresmott needs none of the tibberow fittings.", "none"),  # `none of` partitive
    ("nobody leaves yevelock on sundays.", "nobody"),
])
def test_nominal_negative_operator_is_a_cue(text, cue):
    assert cue in cues(text), f"{text!r} lost its {cue!r} cue"


def test_determiner_scope_is_the_whole_clause_not_the_np():
    """CGEL Ch.5 §7.8: the determiner negates the predication. A scope confined to the NP would
    make the subject and the adjuncts unreachable, which is the failure BioScope-style scope
    annotation exists to catch."""
    assert scope_of("the flendric has no noldrite on record.", "no") == \
        ["the", "flendric", "has", "no", "noldrite", "on", "record", "."]


def test_negative_operator_needs_a_nominal_head_TWIN():
    """`no` before an ADVERB is not nominal negation — "no matter"/"no doubt"-shaped adjuncts are
    NOT this lane's. TWIN ARM: the same negator on a NOUN head still fires, so a silenced seam
    cannot make the first assertion pass vacuously."""
    assert cues("the tibberow opens on wednesdays, no matter the weather.") == []
    assert cues("the tibberow has no weather station.") == ["no"]


# ── LANE 3: CORRELATIVE `neither … nor` ─────────────────────────────────────────────────────────
# CGEL Ch.15 §2.4 "Neither and nor" (p.1308): "As a marker of coordination, neither is usually
# paired correlatively with nor." Ch.5 §7.7: `neither` "represents the lexicalisation of 'not +
# either'"; "Neither boy had a key" means "It is not the case that either boy had a key" — ONE
# clausal negation over the WHOLE coordination.
@pytest.mark.parametrize("text", [
    "neither the vellisk nor the petrisk fits the quoltrane.",       # subject coordination
    "the shalvern covers neither the ulnavere nor the pemvrack.",    # object coordination
    "the krellin feeds neither at murvale nor at velmorrow.",        # coordinated PP adjuncts
])
def test_correlative_pair_is_one_discontinuous_cue(text):
    assert cues(text) == ["neither nor"]


def test_correlative_scope_is_not_cut_at_its_own_nor_TWIN():
    """`nor` is a `cc`, and a `cc` normally TERMINATES a NegEx scope. Inside a correlative it is
    INSIDE the negation, not after it, so terminating there would amputate the second conjunct and
    the predicate. TWIN ARM: an ordinary `and` coordination must still be cut, or this pin would
    pass equally well with scope termination removed altogether."""
    assert scope_of("neither the vellisk nor the petrisk fits the quoltrane.", "neither nor") == \
        ["neither", "the", "vellisk", "nor", "the", "petrisk", "fits", "the", "quoltrane", "."]
    scopes = L.analyze_negation_scopes("the vellisk does not fit and the petrisk does.")
    assert scopes, "twin arm went silent — the termination half of this pin is vacuous"
    assert "petrisk" not in scopes[0].scope_text


def test_correlative_form_test_separates_it_from_either_or_TWIN():
    """`either … or` and `not only … but` share the preconj/cc SHAPE and are not negations. The
    FORM of the initial marker is what separates them — and, ablated, it is the only thing that
    does (requiring the `nor` correlate as well turned no pin red and cost recall; see the lane's
    own comment). TWIN ARM: the negative marker still fires."""
    assert cues("either the vellisk or the petrisk fits the quoltrane.") == []
    assert cues("the shalvern covers not only the ulnavere but also the pemvrack.") == []
    assert cues("neither the vellisk nor the petrisk fits the quoltrane.") == ["neither nor"]


def test_correlative_survives_multiple_coordination_under_one_neither():
    """CGEL Ch.15 §2.4 [48ii]: `He was [neither kind, handsome, nor rich]` — one `neither` can head
    a MULTIPLE coordination, and spaCy hands back an `and` coordinator for some of those. The cue
    is then `neither` alone: an `and` is coordination, not a second negator, so it is exempted from
    scope termination but never annotated as a cue token."""
    assert cues("she found it neither surprising and alarming.") == ["neither"]
    assert cues("neither kim, pat, and sam came.") == ["neither"]
    # SCOPE ON THIS CONSTRUCTION IS **NOT** PINNED, and that is stated rather than quietly omitted.
    # Measured: the first sentence parses `surprising` as a `ccomp`, so the matrix-clause scope
    # walk excludes the very clause the negation sits in and returns "she found ."; the second is
    # cut at the intra-coordination comma and returns "neither kim". Both are the PRE-EXISTING
    # clause-scope rules meeting a construction the fixture set does not annotate. This work
    # targets CUE COVERAGE — scope was already at bar (0.9528 gold-cue-detected) — so the scope
    # here is left measured-and-declared rather than tuned against no ground truth.


def test_clause_level_than_veto_contract():
    """Direct contract pin for `_clause_has_comparative_standard`, the det-arc backstop. It has NO
    measured firing case (see its docstring: spaCy attaches `no` as `neg`, not `det`, in every
    standard-bearing construction probed), so no corpus pin can cover it and this one is the only
    thing standing between it and silent rot. Pinned as a CONTRACT, never cited as coverage."""
    with_std = L._parse("the meeting lasted no longer than an hour.")
    without = L._parse("the bosterway no longer gathers on thursdays.")
    assert L._clause_has_comparative_standard(next(t for t in with_std if t.dep_ == "ROOT")) is True
    assert L._clause_has_comparative_standard(next(t for t in without if t.dep_ == "ROOT")) is False


# ── LANE 1: CONTINUATIVE MULTIWORD ──────────────────────────────────────────────────────────────
# ConanDoyle-neg §3.1 takes "could NO LONGER be withheld" as its own multiword-cue example.
@pytest.mark.parametrize("text,cue", [
    ("the bosterway no longer gathers on thursdays.", "no longer"),
    ("the brindaw is no longer open at sarrowvent on sundays.", "no longer"),   # copular
    ("the kaldrip is no longer kept at yevelock.", "no longer"),                # passive
    ("my vestrow's crandolet is no longer olive.", "no longer"),                # scalar
    ("the quoltrane serves the jolvane no more.", "no more"),                   # `det`-attached
])
def test_continuative_cue_spans_both_tokens(text, cue):
    assert cues(text) == [cue], "the multiword cue must be ONE two-token span, not the negator alone"


def test_continuative_scope_still_terminates_at_the_clause_comma_TWIN():
    """The scope must stop at the comma; swallowing the `though` clause would negate an AFFIRMATIVE
    proposition about a DIFFERENT entity. TWIN ARM: the same sentence without the tail keeps its
    full scope, so this cannot pass by the lane emitting nothing."""
    assert scope_of("the bosterway no longer meets at murvale, though the flendric still does.",
                    "no longer") == ["the", "bosterway", "no", "longer", "meets", "at", "murvale"]
    assert scope_of("the bosterway no longer meets at murvale.", "no longer") == \
        ["the", "bosterway", "no", "longer", "meets", "at", "murvale", "."]


def test_scalar_limit_is_not_a_cancellation_TWIN():
    """"The meeting lasted no longer THAN an hour." — the meeting DID last; the negator scopes over
    the COMPARISON. This lane delegates that call to `_predicate_negated`, which owns the measured
    guard chain. TWIN ARM: a genuine cancellation with a comparative in the OBJECT still fires
    (the `than` there hangs off the object's `more`, not off `longer`)."""
    assert cues("the meeting lasted no longer than an hour.") == []
    assert cues("i stayed no longer than i had to.") == []
    assert cues("i no longer eat more than three meals.") == ["no longer"]


def test_det_attached_negator_still_respects_a_standard_of_comparison_TWIN():
    """spaCy attaches the SAME negator as `det` post-verbally, an arc no `neg`-arc guard can reach,
    so that arm falls back to a blunt clause-level `than` veto. TWIN ARM: without a standard the
    same shape fires — otherwise the veto could be vetoing everything."""
    assert cues("the wait was no more than an hour.") == []
    assert cues("he cared no more than she did.") == []
    assert cues("the quoltrane serves the jolvane no more.") == ["no more"]


def test_bare_comparative_adverb_without_a_negator_is_silent_TWIN():
    """`longer`/`more` alone are ordinary comparatives. TWIN ARM: prefix the negator and it fires."""
    assert cues("the flendric runs longer on saturdays.") == []
    assert cues("the dorrimant matches the pemvrack more than the quoltrane does.") == []
    assert cues("we ran once more around the park.") == []
    assert cues("the flendric no longer runs on saturdays.") == ["no longer"]


def test_open_class_negated_comparatives_stay_silent_TWIN():
    """"no earlier"/"no harder" are COMPARATIVE-SCOPE: the event is asserted. The admitted
    continuative class is CLOSED (`long`, `more`) and lives on the per-tenant cue rail. TWIN ARM:
    an admitted member in the same frame fires."""
    assert cues("the train arrived no earlier than six.") == []
    assert cues("the flendric works no harder than before.") == []
    assert cues("the flendric no longer works here.") == ["no longer"]


# ── FLAG + FAIL-SAFE ────────────────────────────────────────────────────────────────────────────
def test_flag_DEFAULT_is_on_without_any_monkeypatch():
    """Pinned SEPARATELY from the behaviour pins above: those would all pass under a monkeypatched
    constant while the shipped default was OFF. This reads the module's own resolved value and the
    env it was resolved from."""
    assert os.environ.get("SPINE_NEGATION_CUE_INVENTORY") in (None, "", "true", "1"), \
        "env is forcing the flag — this pin measures the CODE default"
    assert L.SPINE_NEGATION_CUE_INVENTORY is True


def test_flag_off_returns_the_pre_fix_inventory_TWIN(monkeypatch):
    """OFF is byte-for-byte today's coverage: an EMPTY inventory, never `None` (which would read as
    a parse failure). TWIN ARM: ON, the same sentence fires."""
    monkeypatch.setattr(L, "SPINE_NEGATION_CUE_INVENTORY", False)
    assert analyze_negation_cues("the brindaw has no quenderal.") == []
    monkeypatch.setattr(L, "SPINE_NEGATION_CUE_INVENTORY", True)
    assert cues("the brindaw has no quenderal.") == ["no"]


def test_the_neg_arc_lane_is_untouched_by_this_seam():
    """`analyze_negation_scopes` feeds the intent ensemble's DESTRUCTIVE correction/retraction
    bypass. This work is span-only and additive; if the neg-arc lane's own answers move, coverage
    and ROUTING changed in one commit and can no longer be measured apart."""
    s = L.analyze_negation_scopes("the bosterway no longer gathers on thursdays.")
    assert [x.cue_text for x in s] == ["no"]
    assert s[0].scope_text == "the bosterway no longer gathers on thursdays ."
    assert L.analyze_negation_scopes("neither the vellisk nor the petrisk fits the quoltrane.") == []
    assert L.analyze_negation_scopes("the brindaw has no quenderal.") == []
