"""Regression: STRANDED relative-clause ACQUISITION reification → orderable dated operand.

CLUSTER (LME temporal-reasoning "which happened first, A or B"): an operand event whose ONLY
date sits in a relative-clause acquisition — "<thing> that I got/bought <relative-date>" — is
silently DROPPED when the antecedent is governed by a matrix gerund/verb, because spaCy
mis-attaches the acquisition verb as a ``ccomp`` of that governing verb instead of a ``relcl`` of
the noun. The acquisition then never reifies with its date, so the "which happened first" ordering
has NO orderable date for that operand and cannot compare it (LME gpt4_b4a80587: the arrival of the
new prime lens — "mastering my 50mm lens that I got a month ago").

FIX (src/extraction/linguistics.py::analyze_acquisition): recover the stranded relative clause from
the unambiguous grammatical signal — the acquisition verb carries a GAPPED RELATIVIZER object
("that"/"which", tag WDT / PronType=Rel) + a 1st-person subject, so its true object is a
head-adjacent ANTECEDENT, not a concrete possession. Bind that antecedent as the acquired thing so
the existing lane emits ``(user, owns, <thing>)`` carrying the peeled acquisition date. Subject-
agnostic, deterministic (dep + tag/morph + adjacency), no word-list.

Technique / citation: telic-achievement event reification (a completed acquisition is a bounded
POINT event) makes the operand orderable via Allen's interval algebra before/after over the real
event_date column (min = first). James F. Allen, "Maintaining Knowledge about Temporal Intervals"
(1983); telic/achievement event semantics (Vendler; Filip, telicity).

FAIL-ON-OLD: before the fix analyze_acquisition returns None for the ccomp-stranded shape and
_detect_event_states emits nothing dated for the lens, so the ordering (test_ordering_*) has one
dated operand and cannot pick the lens as first. PASS-ON-FIX: the lens reifies dated → ordering
picks it first. NO-REGRESSION cases pin the canonical dobj / relcl acquisitions, the deliberately-
undated "recently", and the abstention when an operand is genuinely absent.

Run: SPACY_MODEL=en_core_web_sm python3 tools/fltest.py --bug LME-b4a80587 \
        --test tests/test_stranded_relcl_acquisition_ordering.py
"""
import os

import pytest

os.environ.pop("POSTGRES_DSN", None)  # overlays fall back to seed/code defaults — pure deterministic
os.environ.setdefault("LINGUISTIC_LAYER", "true")
os.environ.setdefault("OCCURRENCE_REIFY", "true")
os.environ.setdefault("TEMPORAL_DATE_LAYER", "true")

import src.api.main as m  # noqa: E402


def _model_available() -> bool:
    try:
        from src.extraction.linguistics import linguistics_available
        return linguistics_available()
    except Exception:  # noqa: BLE001
        return False


requires_model = pytest.mark.skipif(
    not _model_available(), reason="en_core_web_sm not installed in test env"
)


def _ref(datestr: str):
    """Session reference datetime from a harness [Date:] marker (same door ingest uses)."""
    ref, _ = m._compute_ingest_reference_and_text(f"[Date: {datestr}] x")
    return ref


# ───────────────────────── deriver-level: stranded relcl acquisition captured ─────────────────

@requires_model
@pytest.mark.parametrize("text,expect_sub", [
    # LME gpt4_b4a80587 exemplar — the acquisition verb "got" is a ccomp of the gerund "mastering".
    ("I'm especially interested in mastering my 50mm lens that I got a month ago.", "50 mm lens"),
    # Same structural defect under a different matrix verb ("used to") — cluster generalization.
    ("I'm still getting used to my new camera that I bought last month.", "new camera"),
])
def test_stranded_relcl_acquisition_is_captured(text, expect_sub):
    """FAIL-ON-OLD: analyze_acquisition returned None for the ccomp-stranded shape → nothing here."""
    from src.extraction.linguistics import analyze_acquisition
    acq = analyze_acquisition(text)
    assert acq is not None, f"stranded relcl acquisition not detected: {text!r}"
    assert expect_sub in (acq.device or ""), f"device={acq.device!r} missing {expect_sub!r}"


@requires_model
def test_stranded_relcl_acquisition_reifies_dated():
    """The stranded acquisition now reifies as a DATED (user, owns, <thing>) edge — an orderable
    operand — with the RELATIVE date ('a month ago') peeled and attached."""
    ref = _ref("2023/05/30 (Tue) 00:00")
    edges = m._detect_event_states(
        "I'm especially interested in mastering my 50mm lens that I got a month ago.", ref)
    owns = [e for e in edges if e.get("rel_type") == "owns" and e.get("event_date")]
    assert owns, f"no dated owns edge reified: {edges!r}"
    assert owns[0]["event_date"][:7] == "2023-04", f"expected ~Apr 2023, got {owns[0]['event_date']}"


@requires_model
def test_stranded_relcl_yields_dated_acquisition_EVENT_not_just_owns():
    """SYMMETRY with the matrix-verb case: a plain acquisition "I bought a camera two months ago"
    is lifted by the spine deriver's SVO as a dated verb EVENT (user, buy, camera) — an EVENT-class
    edge the ordering render treats as an occurrence. A relative-clause acquisition buries the
    acquisition verb, so the deriver lifts the MATRIX verb and mints NO acquisition event; the lane
    must supply the SAME verb-lemma event edge so _apply_get_first_last has an EVENT operand — not
    just the stative ``owns`` (which the temporal-event render excludes).

    FAIL-ON-OLD: the lane emitted ONLY owns@date for the relcl case → no dated non-owns event edge.
    """
    ref = _ref("2023/05/30 (Tue) 00:00")
    edges = m._detect_event_states(
        "I'm especially interested in mastering my 50mm lens that I got a month ago.", ref)
    # A DATED acquisition EVENT edge = a dated edge on the device whose rel is the acquisition VERB
    # (not the stative 'owns'). This is the exact shape the deriver yields for the matrix 'buy' case.
    evt = [e for e in edges
           if e.get("rel_type") not in ("owns", "also_known_as", "located_in")
           and e.get("event_date") and "lens" in (e.get("object") or "")]
    assert evt, f"no dated acquisition EVENT edge (only owns?): {edges!r}"
    assert evt[0]["rel_type"] == "get", f"event rel should be the acquisition verb lemma: {evt[0]!r}"
    assert evt[0]["event_date"][:7] == "2023-04", f"event not dated ~Apr 2023: {evt[0]!r}"
    assert evt[0].get("fact_provenance") == "user_stated", \
        f"acquisition event should match the deriver's user_stated SVO provenance: {evt[0]!r}"


@requires_model
def test_matrix_dobj_acquisition_not_double_emitted_as_lane_event():
    """A PLAIN matrix acquisition ("I bought a camera two months ago") already gets its dated verb
    EVENT from the spine deriver's SVO; the lane must NOT double-emit a second verb event for it
    (via_relcl is False), so the lane's contribution stays owns-only — byte-for-byte the pre-change
    matrix behaviour."""
    ref = _ref("2023/05/30 (Tue) 00:00")
    edges = m._detect_event_states("I bought a camera two months ago.", ref)
    lane_events = [e for e in edges
                   if e.get("rel_type") not in ("owns", "also_known_as", "located_in")]
    assert not lane_events, f"matrix acquisition double-emitted a lane verb event: {lane_events!r}"


# ───────────────────────── end-to-end ordering: earlier operand wins ─────────────────────────

@requires_model
def test_ordering_picks_earlier_dated_acquisition_operand():
    """"Which happened first, the road trip or the arrival of the lens?" — lens (owns @ Apr 30)
    < road trip (participated_in @ May 23) → the ordering operator returns the LENS first.
    FAIL-ON-OLD: the lens carried no event_date, so only the road trip was dated and the operator
    could never surface the lens as first."""
    LENS, TRIP, USER = "lens-uuid", "trip-uuid", "user-uuid"
    facts = [
        {"subject": USER, "_subject_id": USER, "rel_type": "participated_in",
         "object": TRIP, "_object_id": TRIP, "event_date": "2023-05-23T00:00:00+00:00"},
        {"subject": USER, "_subject_id": USER, "rel_type": "owns",
         "object": LENS, "_object_id": LENS, "event_date": "2023-04-30T00:00:00+00:00"},
    ]
    out = m._apply_get_first_last(list(facts), "first", operand_ids=[LENS, TRIP])
    assert out, "operator returned nothing for a decidable 2-operand comparison"
    assert str(out[0].get("_object_id")) == LENS, \
        f"expected the earlier lens operand first, got {out[0]!r}"


# ───────────────────────── no-regression guards ──────────────────────────────────────────────

@requires_model
@pytest.mark.parametrize("text,expect", [
    ("I got a 50mm lens on April 2nd.", "50 mm lens"),          # canonical dobj acquisition
    ("I love the guitar that I picked up last week.", "guitar"),  # canonical (correctly-parsed) relcl
])
def test_canonical_acquisitions_still_capture(text, expect):
    from src.extraction.linguistics import analyze_acquisition
    acq = analyze_acquisition(text)
    assert acq is not None and expect in (acq.device or ""), f"regressed canonical acquisition: {text!r}"


@requires_model
def test_recently_stays_undated_no_regression():
    """"recently" is a deliberately-undated vague window (pinned elsewhere): the acquisition still
    exposes the owns linkage but carries NO fabricated event_date."""
    ref = _ref("2023/05/30 (Tue) 00:00")
    edges = m._detect_event_states("I recently got a new prime lens.", ref)
    owns = [e for e in edges if e.get("rel_type") == "owns"]
    assert owns, "recently-acquisition owns linkage dropped"
    assert not owns[0].get("event_date"), "‘recently’ must not fabricate a date"


@requires_model
@pytest.mark.parametrize("text", [
    "I forgot that I have a meeting.",     # 'that'-complementizer, non-acquisition verb → no owns
    "I think I bought milk.",              # ccomp with a concrete dobj → NOT the stranded shape
])
def test_no_false_stranded_capture(text):
    """The stranded branch must not fire on a plain complementizer 'that' or a ccomp that already
    has a concrete direct object (that path stays the canonical dobj capture, never the antecedent)."""
    from src.extraction.linguistics import analyze_acquisition
    acq = analyze_acquisition(text)
    # 'forgot…have' → no acquisition at all; 'bought milk' → device is the concrete dobj 'milk',
    # never an antecedent noun pulled from a (non-existent) relativizer.
    if acq is not None:
        assert "milk" in (acq.device or ""), f"false stranded antecedent capture: {acq.device!r}"
