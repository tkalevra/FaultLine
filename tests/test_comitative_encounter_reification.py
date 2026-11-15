"""Regression — COMITATIVE SOCIAL-ENCOUNTER reification (person-event ordering cluster).

CLUSTER (LME temporal-reasoning): "who did I meet first, A or B" needs a DATED edge keyed to EACH
person operand so the min/max ordering operator (Allen point-event, _apply_get_first_last) can
compare them. The salient person is expressed OBLIQUELY as the comitative ``with``-PP co-participant
of a light-verb social activity ("had a conversation WITH a jam maker", "had lunch WITH Boris", "a
meeting WITH Sarah"). Before the fix the occurrence reified at its bland TYPE ("lunch"/"conversation")
and the co-participant landed only on an UNDATED companion (also_known_as title / related_to) — so the
comparison scoped to the person found NO dated edge and abstained/missed.

THE FIX (deterministic, subject-agnostic): the deriver (_build_event_analysis) surfaces the comitative
``with``-PP co-participant on EventAnalysis.companions (scanning BOTH the eventive noun and the light
verb — attachment varies), and the event seams emit (user, met, <person>)@<event_date>, MIRRORING the
clean-SVO (user, met, <person>)@date that "I met a tourist" already produces. Gated OUT of the
device-issue reading (a possessed inanimate "an issue WITH my car's GPS" carries no companion).

FAIL-ON-OLD: old code emits NO ``met`` edge for these constructions (the person is undated) → the
``assert any(... rel==met ... event_date)`` fails. PASS-ON-FIX: each person yields a dated met edge.

PURE — spaCy only (cue overlays degrade gracefully with no DB), no network/LLM/GLiNER2.
Run: SPACY_MODEL=en_core_web_sm python3 -m pytest tests/test_comitative_encounter_reification.py -q
(tests/ is gitignored → git add -f)
"""
import datetime as dt
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("OCCURRENCE_REIFY", "1")

import src.api.main as m  # noqa: E402

_ling = pytest.importorskip("src.extraction.linguistics")

pytestmark = pytest.mark.skipif(
    not _ling.linguistics_available(), reason="spaCy model not installed"
)

_REF = dt.date(2023, 6, 1)


def _met_edges(text):
    edges = m._detect_event_states_reified(text, _REF)
    return [e for e in edges if (e.get("rel_type") or "").lower() == "met"]


def _dated_met_object(text):
    """Return {object: event_date} for every dated met edge the seam emits."""
    return {
        (e.get("object") or "").lower(): e.get("event_date")
        for e in _met_edges(text)
        if e.get("event_date")
    }


# ── The cluster: an obliquely-expressed person co-participant → a DATED (user, met, X) edge ──────

@pytest.mark.parametrize(
    "text, person, iso_prefix",
    [
        # EX2 exemplar shape — with-PP attached to the eventive NOUN, common-noun agent person.
        ("I had a lovely conversation with a jam maker two weeks ago.", "jam maker", "2023-05-18"),
        # with-PP attached to the LIGHT VERB (attachment variance) — proper-name person.
        ("I had lunch with Boris on January 5th 2023.", "boris", "2023-01-05"),
        # with-PP attached to the eventive NOUN — proper-name person (also grabbed as title).
        ("I had coffee with Dmitri on March 10th 2023.", "dmitri", "2023-03-10"),
    ],
)
def test_comitative_person_reifies_as_dated_met(text, person, iso_prefix):
    dated = _dated_met_object(text)
    assert person in dated, f"no DATED met edge for {person!r}: got {dated}"
    assert str(dated[person]).startswith(iso_prefix), (
        f"{person}: expected date {iso_prefix}, got {dated[person]}"
    )


def test_two_operands_both_reify_dated_and_order():
    """The ordering keystone: TWO social encounters both reify as dated met edges, and the min-date
    operand is the earlier one — exactly what 'who did I meet first, Boris or Dmitri' needs."""
    boris = _dated_met_object("I had lunch with Boris on January 5th 2023.")
    dmitri = _dated_met_object("I had coffee with Dmitri on March 10th 2023.")
    assert boris.get("boris") and dmitri.get("dmitri")
    # Allen point-event "before": start(Boris) < start(Dmitri) → Boris is met first.
    assert min(boris["boris"], dmitri["dmitri"]) == boris["boris"]


# ── Close the query chain: the ordering operator picks the EARLIER operand ───────────────────────

def test_ordering_operator_picks_earlier_met_operand():
    """END-TO-END (in-process): the two dated met edges the seam emits, once fetched + operand-scoped
    exactly as the /query comparison path does, drive _apply_get_first_last('first') to the EARLIER
    person — 'who did I meet first, Boris or Dmitri' → Boris. Uses UUID surrogates on the object side
    (dedup/scoping is UUID-authoritative), mirroring fetch_facts_from_anchor's comparison lane."""
    boris_id, dmitri_id, user_id = "uuid-boris", "uuid-dmitri", "uuid-user"
    fetched = [
        {"subject": user_id, "object": boris_id, "rel_type": "met",
         "event_date": "2023-01-05T00:00:00"},
        {"subject": user_id, "object": dmitri_id, "rel_type": "met",
         "event_date": "2023-03-10T00:00:00"},
    ]
    scoped = m._operand_scope_facts(fetched, [boris_id, dmitri_id])
    assert len(scoped) == 2  # both operands carry a dated edge → decidable comparison
    ordered = m._apply_get_first_last(scoped, "first", operand_ids=[boris_id, dmitri_id])
    assert ordered, "operator returned no winner"
    assert ordered[0]["object"] == boris_id  # Boris (Jan) met before Dmitri (Mar)


# ── The guard: a possessed inanimate device-issue is NEVER read as a person you met ─────────────

def test_device_issue_emits_no_met_edge():
    """'an issue WITH my car's GPS system' is the has_state affected-thing lane, NOT a co-participant
    — it must emit NO (user, met, gps) edge (would fabricate a social encounter with a device)."""
    assert _met_edges("I had an issue with my car's GPS system on May 3rd.") == []


def test_plain_dated_occurrence_no_spurious_met():
    """A dated occurrence with NO with-PP co-participant ('I attended a workshop on Jan 15') emits no
    met edge — the lane fires only on a genuine comitative co-participant."""
    assert _met_edges("I attended a workshop on January 15th 2023.") == []
