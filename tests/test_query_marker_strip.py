"""Recall/query-path harness MARKER strip (mirror the ingest marker handling).

The LongMemEval harness prepends a leading ``[Date: YYYY/MM/DD (Day) HH:MM]\\n`` session-
reference marker to EVERY recall query (executors.py). The INGEST path already strips this
marker (the marker is the REFERENCE only, never content) so fact dates anchor to the session.
The RECALL path did NOT strip it before the topic/intent parse, so the marker tokens
("[date:", "2023/04/10", "(mon)", "23:07]") survived the ``_QT_STOP`` function-word filter and
became the bogus topic-word — the real topic ("issue") was lost, ``_resolve_topic_to_entity_ids``
never fired, and a same-day tie handed the ordinal to the wrong event.

FIX (subject-agnostic, fail-safe): reuse the ingest single-door
(``_compute_ingest_reference_and_text`` → ``src.temporal.derive_now``) on the recall parse:
  • ``classify_query_template`` strips the marker before topic/relation/scope extraction.
  • ``_detect_temporal_query_intent`` runs its phrase NLU on the STRIPPED text while still
    deriving the reference instant from the marker (so year-less granules / pivots anchor to
    the QUESTION date — the harness intent).
No marker → stripped == raw AND reference == wall-clock now → byte-for-byte unchanged.
"""
import os

os.environ.setdefault("POSTGRES_DSN", "")

from src.api import main  # noqa: E402


_Q = "What was the first issue I had with my new car after its first service?"
_MARKER = "[Date: 2023/04/10 (Mon) 23:07]\n"


# ── classify_query_template: marker stripped BEFORE topic extraction ──────────────

def test_topic_word_extracted_through_marker():
    """WITH the marker, the topic must still be the real content word ('issue'),
    NOT the bogus '[date:' token the raw marker would yield."""
    r = main.classify_query_template(_MARKER + _Q)
    assert r["topic_word"] == "issue"


def test_topic_word_parity_with_and_without_marker():
    """Marker present vs absent → identical topic_word + template (marker is never content)."""
    with_m = main.classify_query_template(_MARKER + _Q)
    no_m = main.classify_query_template(_Q)
    assert with_m["topic_word"] == no_m["topic_word"] == "issue"
    assert with_m["template"] == no_m["template"]
    assert with_m["boundary"] == no_m["boundary"]


def test_raw_marker_text_would_have_broken_topic_extraction():
    """Documents the bug: feeding the RAW marker text to the topic decomposer yields the
    marker token as topic — proving the strip is load-bearing."""
    bad = main._qt_topic_relation((_MARKER + _Q).strip().lower())
    assert bad[0] == "[date:"  # the bug, pre-strip


def test_no_marker_query_unchanged():
    """No leading marker → topic extraction byte-for-byte unchanged (live/non-harness path)."""
    r = main.classify_query_template(_Q)
    assert r["topic_word"] == "issue"


# ── _detect_temporal_query_intent: stripped parse, marker-anchored reference ──────

def test_temporal_intent_parity_with_and_without_marker():
    """The temporal-intent NLU must be identical with/without the marker — the marker was
    never an intent phrase, and the reference anchoring is independent of the parse text."""
    with_m = main._detect_temporal_query_intent(_MARKER + _Q)
    no_m = main._detect_temporal_query_intent(_Q)
    assert with_m.get("ordinal") == no_m.get("ordinal")
    assert with_m.get("direction") == no_m.get("direction")
    assert with_m.get("anchor") == no_m.get("anchor")


def test_temporal_intent_reference_anchors_to_marker_year():
    """A bare-year/relative branch must anchor to the QUESTION date (2023), not wall-clock,
    when the marker is present — the reference derivation survives the parse strip."""
    # derive_now is the single-door the intent fn delegates to for the reference.
    from src.temporal import derive_now
    ref, stripped = derive_now(_MARKER + _Q)
    assert ref.year == 2023
    assert stripped == _Q  # marker stripped, content intact


def test_duration_calc_question_with_marker_unaffected():
    """A two-anchor duration calc WITH its marker resolves the same calc directive as without
    (spot-check that the strip does not disturb other temporal questions)."""
    qd = "How many days passed between my car service and my research?"
    with_m = main._detect_temporal_query_intent(_MARKER + qd)
    no_m = main._detect_temporal_query_intent(qd)
    assert with_m.get("calc") == no_m.get("calc")
    assert with_m.get("calc", {}).get("op") == "between"


# ── fail-safe: empty / marker-less inputs ─────────────────────────────────────────

def test_classify_empty_query_safe():
    assert main.classify_query_template("")["topic_word"] is None


def test_detect_temporal_empty_query_safe():
    assert main._detect_temporal_query_intent("") == {}
