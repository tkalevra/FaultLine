"""PROPN NAMED-OCCURRENCE capture — the ingest fix for proper-named events said as the DIRECT
OBJECT of an attendance/participation support verb.

THE CLUSTER (LongMemEval temporal-reasoning misses): a turn like "I attended the annual Holiday
Market at the local mall a week before Black Friday" or "I attended the Coachella Festival" states a
NAMED event whose head is tagged PROPN by spaCy ("Market"/"Festival"/"Fair"). The old
``_collect_eventive_heads`` branch (a) admitted a direct-object eventive head ONLY when its POS was
NOUN — a PROPN dobj was rejected as "possession" — so the named event was either dropped entirely OR
(when a locative PP was present) the circumstantial venue ("at the local mall") was POACHED as the
occurrence. The reified date-diff / ordering math downstream then had NO clean dated operand and the
question missed. Root: src/extraction/linguistics.py ``_collect_eventive_heads`` branch (a).

THE FIX (subject-agnostic, deterministic, POS-only — NO name/event/domain word-list): a PROPN
direct object of an EVENT-FRAMED support verb is admitted as a NAMED occurrence under the SAME
event-framing gate the NOUN head uses (the acquisition lane diverts get/buy upstream; the
present-simple stative gate drops "I have an iPhone"). The admitted PROPN is marked a NAMED head so
the bare locative adjunct is suppressed — the proper-named EVENT, not the venue, becomes the reified
occurrence carrying the clause's date.

FAIL-ON-OLD: on the pre-fix code ``analyze_events("I attended the annual Holiday Market at the local
mall", dated=True)`` returns ``[('local mall', None)]`` — no "holiday market" event AND the poached
"local mall" present; both assertions below fail. PASS-ON-FIX: the named market is reified and the
mall is gone.

PURE tests — no DB, no network, no GLiNER2, no LLM. Skip cleanly if en_core_web_sm is absent.
Run: SPACY_MODEL=en_core_web_sm python3 tools/fltest.py --bug LME-c8090214 \
       --test tests/test_event_propn_named_occurrence.py --note "PROPN named-event dobj capture"
"""
import os
from datetime import datetime, timezone

import pytest

os.environ.pop("POSTGRES_DSN", None)
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


def _events(text, dated=True):
    from src.extraction.linguistics import analyze_events
    return [(e.event or "").strip().lower() for e in analyze_events(text, dated=dated)]


# ── EXEMPLAR 2 (qid=c8090214): "attended the annual Holiday Market at the local mall" ──────────
@requires_model
def test_propn_named_event_dobj_reifies_and_suppresses_locative():
    """The proper-named Holiday Market (PROPN dobj) is captured as an occurrence; the locative
    venue "local mall" is NOT poached as the occurrence. Fails on old code (returned only
    'local mall')."""
    evs = _events("I attended the annual Holiday Market at the local mall.")
    assert any("holiday market" in e for e in evs), \
        f"named event 'holiday market' not reified (old NOUN-only gate dropped the PROPN dobj): {evs}"
    assert not any("mall" in e for e in evs), \
        f"circumstantial locative 'local mall' was poached as the occurrence: {evs}"


# ── GENERALIZATION: the whole class of attend + PROPN-named-event direct objects ───────────────
@requires_model
@pytest.mark.parametrize("text, want, locative", [
    ("I attended the Coachella Festival at the desert grounds.", "festival", "grounds"),
    ("I attended the Renaissance Fair at the fairgrounds.", "fair", "fairground"),
    ("I attended the annual Holiday Market at the local mall.", "market", "mall"),
])
def test_propn_named_event_class_generalizes(text, want, locative):
    """Multiple exemplars of the same structural defect (PROPN dobj of an attendance verb) are all
    reified as the named event, and the locative adjunct is suppressed in each. Subject-agnostic —
    POS-only, no name/event list."""
    evs = _events(text)
    assert any(want in e for e in evs), f"expected a '{want}' occurrence, got {evs}"
    assert not any(locative in e for e in evs), f"locative '{locative}' poached: {evs}"


# ── END-TO-END on the LIVE reified seam: the named event carries the clause's event_date ───────
@requires_model
def test_reified_named_event_carries_event_date():
    """The exact EX2 gold turn on the live occurrence seam reifies (user, participated_in,
    occurrence: <holiday market>) carrying the "a week before Black Friday" offset date
    (Black Friday 2023-11-24 minus 7 = 2023-11-17) — the clean dated operand the day-diff math needs."""
    ref = datetime(2023, 12, 1, tzinfo=timezone.utc)
    turn = ("I attended the annual Holiday Market at the local mall a week before Black Friday, "
            "and I saw some unique handmade jewelry there.")
    edges = m._detect_event_states_reified(turn, ref)
    part = [e for e in edges
            if (e.get("rel_type") == "participated_in" and "holiday market" in (e.get("object") or ""))]
    assert part, f"no dated Holiday Market participation reified: {edges}"
    assert any((e.get("event_date") or "").startswith("2023-11-17") for e in part), \
        f"Holiday Market occurrence did not carry the offset event_date 2023-11-17: {part}"
    # the venue must not survive as its own dated occurrence
    assert not any(e.get("rel_type") == "participated_in" and "mall" in (e.get("object") or "")
                   for e in edges), f"locative 'mall' survived as an occurrence: {edges}"


# ── POSSESSION SAFETY (no regression): a present-simple PROPN possession is NOT an event ───────
@requires_model
def test_present_simple_propn_possession_not_eventified():
    """"I have an iPhone" (present simple, no date → dated=False) stays possession — the PROPN dobj
    is dropped by the present-simple stative gate, NOT minted as (user, participated_in, iphone).
    Proves the fix did not widen possession into events."""
    assert _events("I have an iPhone.", dated=False) == [], \
        "present-simple PROPN possession was wrongly eventified"
