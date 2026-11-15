"""LME cluster A3 — TELIC ACCOMPLISHMENT over a QUOTED-TITLE work reifies as a dated occurrence.

THE INGEST GAP (fail-on-old): a completive/telic engagement with a NAMED, bounded work —
"I finished reading 'The Hate U Give'", "I started watching 'Game of Thrones'", "I've been
reading 'The Nightingale'" — carries the event meaning on a verb (finish/read/watch/start/
check-out) that is OUTSIDE the bounded LVC support-verb class, whose object is a QUOTED PROPN
rather than an eventive noun. So the two shipped occurrence lanes BOTH miss it:

  * analyze_events        → [] (no LVC head)
  * analyze_inchoative    → None (the complement is an -ing VERB xcomp, not a NOUN item)

→ the titled work never becomes a dated occurrence, so a downstream "which did I finish/start
FIRST, A or B?" ordering (gpt4_2d58bcd6 / gpt4_483dd43c) or "how long ... combined?" duration
(b9cfe692) question has NO dated operand to order/measure and recall returns nothing.

THE FIX (subject-agnostic, deterministic, NO verb word-list): a NEW deriver lane
``analyze_titled_work_occurrences`` licensed by the OBJECT, not the verb — a QUOTED-TITLE PROPN
that is the direct object of a VERB whose governing subject (resolved through subject control)
is 1st-person. The verb LEMMA (read structurally from the parse) is the occurrence's L4 activity
type; the quoted run is its name. Wired as a FALLBACK after the LVC + inchoative lanes in BOTH
``_detect_event_states`` and ``_detect_event_states_reified``.

These tests pin the parse across MULTIPLE cluster exemplars AND the full reified-edge shape, and
guard the subject-control / negation discipline. Model-backed tests skip cleanly without spaCy.

Run: python3 tools/fltest.py --bug LME-A3 --test tests/test_titled_work_occurrence_reify.py
     (tests/ is gitignored → git add -f if committing)
"""
import os

import pytest

# No live DSN — the cue overlays fall back to seed/code defaults; this lane is pure-deterministic.
os.environ.pop("POSTGRES_DSN", None)
os.environ.setdefault("LINGUISTIC_LAYER", "true")
os.environ.setdefault("OCCURRENCE_REIFY", "true")
os.environ.setdefault("TEMPORAL_DATE_LAYER", "true")


def _model_available() -> bool:
    try:
        from src.extraction.linguistics import linguistics_available
        return linguistics_available()
    except Exception:  # noqa: BLE001
        return False


requires_model = pytest.mark.skipif(
    not _model_available(), reason="en_core_web_sm not installed in test env"
)


# ── The cluster A3 exemplars (verb-agnostic; the QUOTED TITLE is the licenser) ──────────────
# (sentence, expected title surface, expected activity-verb lemma)
_CLUSTER = [
    # EXEMPLAR 1 — "finished reading 'The Hate U Give'" (complement-verb object) + the bare
    # "finished 'The Hate U Give'" (direct object) + the subject-control "able to finish 'X'".
    ("I just finished 'The Hate U Give' by Angie Thomas", "The Hate U Give", "finish"),
    ("I finished reading 'The Hate U Give'", "The Hate U Give", "read"),
    ("In my case, since I was able to finish 'The Hate U Give' just in time for my book "
     "club meeting, I think it helped", "The Hate U Give", "finish"),
    # EXEMPLAR 2 — "started watching 'Game of Thrones'" + subject-raised "meaning to check out 'X'".
    ("I started watching 'Game of Thrones' about a month ago", "Game of Thrones", "watch"),
    ("I have been meaning to check out 'Game of Thrones' for a while", "Game of Thrones", "check"),
    # EXEMPLAR 3 — "finished 'The Nightingale'" (single content-token title) + progressive "been reading".
    ("I recently finished 'The Nightingale' by Kristin Hannah", "The Nightingale", "finish"),
    ("I've been reading 'The Nightingale' for three weeks", "The Nightingale", "read"),
]


@requires_model
@pytest.mark.parametrize("sentence,title,lemma", _CLUSTER)
def test_titled_work_lane_captures_cluster_exemplar(sentence, title, lemma):
    from src.extraction.linguistics import analyze_titled_work_occurrences as atw
    evs = atw(sentence)
    assert evs, f"titled-work lane produced nothing for: {sentence!r}"
    titles = {(e.title or "").strip().lower() for e in evs}
    assert title.lower() in titles, f"expected title {title!r}, got {titles}"
    ev0 = next(e for e in evs if (e.title or "").strip().lower() == title.lower())
    assert ev0.event == lemma, f"expected activity lemma {lemma!r}, got {ev0.event!r}"
    assert ev0.negated is False


@requires_model
@pytest.mark.parametrize("sentence", [c[0] for c in _CLUSTER])
def test_old_lanes_miss_the_cluster_fail_on_old(sentence):
    # PROVES the gap the new lane fills: on the OLD pipeline BOTH shipped occurrence lanes produce
    # nothing for these clauses, so no occurrence was reified (the fail-on-old evidence).
    from src.extraction.linguistics import analyze_events, analyze_inchoative
    assert analyze_events(sentence, dated=True) == []
    assert analyze_inchoative(sentence) is None


@requires_model
def test_second_person_subject_blocks_capture():
    # "I'm glad you LIKED 'The Hate U Give'" — the titled-work verb's OWN subject is "you" (2nd
    # person). Subject control must NOT climb past it to the matrix "I": no first-person occurrence.
    from src.extraction.linguistics import analyze_titled_work_occurrences as atw
    assert atw("I'm glad you liked 'The Hate U Give'") == []


@requires_model
def test_negated_accomplishment_is_marked_negated():
    from src.extraction.linguistics import analyze_titled_work_occurrences as atw
    evs = atw("I never finished 'The Nightingale'")
    assert evs and evs[0].negated is True


@requires_model
def test_no_quoted_title_no_fire():
    # No quoted run anywhere → the lane is inert (an unquoted bare-object accomplishment is
    # deliberately NOT admitted; separating it from a stative would need a verb list).
    from src.extraction.linguistics import analyze_titled_work_occurrences as atw
    assert atw("I finished the book last week") == []
    assert atw("I have a Samsung Galaxy") == []


@requires_model
@pytest.mark.parametrize("sentence,title,lemma", _CLUSTER)
def test_reified_edges_are_a_dated_named_occurrence(sentence, title, lemma):
    # END-TO-END through the shipped reifier: each titled work becomes a participated_in occurrence
    # carrying the peeled date, filed at the activity type (instance_of), and NAMED via
    # also_known_as with the verbatim title — the shape a "which FIRST" / "how long" walk needs.
    import src.api.main as m
    from src.extraction.linguistics import analyze_titled_work_occurrences as atw
    iso = "2023-05-07"
    ev = atw(sentence)[0]
    edges = m._reify_occurrence_edges(ev.event, ev.title, iso, "day")
    assert edges, "reifier returned no edges for a titled occurrence"
    part = [e for e in edges if e.get("rel_type") == "participated_in"]
    assert part and part[0].get("event_date") == iso
    handle = part[0].get("object")
    assert handle and handle.startswith("occurrence:")
    aka = [e for e in edges
           if e.get("rel_type") == "also_known_as"
           and (e.get("object") or "").strip().lower() == title.lower()]
    assert aka, f"title {title!r} not bound via also_known_as; edges={edges}"
    inst = [e for e in edges if e.get("rel_type") == "instance_of"]
    assert inst and inst[0].get("object") == lemma
