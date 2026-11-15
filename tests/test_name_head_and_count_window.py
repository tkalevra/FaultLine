"""Regression — NAME-HEAD-NOUN INSTANCE TYPING + COUNT DATE-WINDOW SCOPING (LME multi-session
count cluster, qid 80ec1f4f: "How many different museums or galleries did I visit in the month of
February?", gold=2).

TWO residual gaps this file pins (the appositive fix already handles the "The Art Cube, a gallery"
operand; the museum operand + the February scoping remained):

PART 1 — NAME-HEAD-NOUN TYPING (INGEST). A captured PROPER-NAMED PLACE whose name ENDS IN a common
COUNTABLE type-noun — "the Natural History Museum" — IS-A that type, but the venue lane filed it only
as `(user, located_in, <place>)` + `entity_type=Location`; nothing typed it `instance_of museum`, and
WordNet has NO `natural history museum -> museum` hyponym, so the count FALSE-MISSED it. The fix
(`wordnet_ladder.name_head_type` + `_venue_place_edges` emit, flag NAME_HEAD_INSTANCE_TYPING default
ON) files `(natural history museum, instance_of, museum)` via the endocentric RIGHT-HAND HEAD RULE
(Williams 1981; UD `compound`) with WordNet CLASS/INSTANCE + proper-noun-lemma guards (Miller &
Hristea 2006). THE HARD LINE: the full name is the naming layer, the head common-noun is the L4 type.
FAIL-ON-OLD: `name_head_type` did not exist / no `instance_of` edge emitted.

PART 2 — COUNT DATE-WINDOW SCOPING (QUERY). "in the month of February" holds MORE typed places than
gold=2 (a January place is a distractor). The count must scope to instances whose DATED visit
companion falls INSIDE the window. `_detect_count_intent` captures the window phrase; a dateparser
resolver (REQUIRE_PARTS month — NO month word-list) yields [start,end]; `_apply_instance_count`
filters companion `event_date`s (a PLACE is the OBJECT of its `(user, located_in, place)@date` edge,
so both sides are matched). FAIL-ON-OLD: `_apply_instance_count` had no `window` kwarg (TypeError) and
counted every typed place regardless of date (3, not 2).

Both deterministic + offline + subject-agnostic. Model-backed parts skip cleanly without
en_core_web_sm; the helper/count parts need only WordNet + dateparser.
"""

import datetime
import os

import pytest


# ── PART 1a: the WordNet name-head helper (pure, offline) ────────────────────────────────────────
def test_name_head_type_positives():
    """A proper-name run ending in a common countable type-noun → that head type-noun."""
    from src.api.wordnet_ladder import name_head_type
    assert name_head_type("natural history museum") == "museum"
    assert name_head_type("modern art museum") == "museum"
    assert name_head_type("luigi's restaurant") == "restaurant"
    assert name_head_type("central library") == "library"


def test_name_head_type_negatives_proper_tail():
    """THE REQUIRED GUARD: a proper name whose tail is NOT a countable common type-noun must NOT be
    typed. "New York" -> york, "Lake Tahoe" -> tahoe, "Colorado Springs" -> spring are all rejected
    (WordNet full-surface instance guard + head proper-noun-lemma / instance guards); a bare common
    noun ("museum") is a TYPE not a named instance; "the Getty" has no common head noun."""
    from src.api.wordnet_ladder import name_head_type
    assert name_head_type("new york") is None
    assert name_head_type("lake tahoe") is None
    assert name_head_type("colorado springs") is None
    assert name_head_type("the getty") is None
    assert name_head_type("museum") is None          # bare type, single token
    assert name_head_type("") is None


# ── PART 1b: the venue seam emits instance_of through the REAL ingest deriver ─────────────────────
def _detect_event_states_or_skip():
    try:
        import spacy  # noqa: F401
        from src.extraction.linguistics import linguistics_available
        if not linguistics_available():
            pytest.skip("en_core_web_sm not available")
    except Exception:  # noqa: BLE001
        pytest.skip("spaCy/en_core_web_sm not available")
    from src.api.main import _detect_event_states
    return _detect_event_states


def test_venue_emits_instance_of_museum():
    """The gold turn through the real event-states seam types the museum instance_of museum."""
    _detect_event_states = _detect_event_states_or_skip()
    ref = datetime.datetime(2026, 3, 3, tzinfo=datetime.timezone.utc)
    edges = _detect_event_states(
        "I took my niece to the Natural History Museum on February 8th.", reference=ref)
    triples = {(e["subject"], e["rel_type"], e["object"]) for e in edges}
    # PASS-ON-FIX: the named place is typed instance_of its head type-noun.
    assert ("natural history museum", "instance_of", "museum") in triples, triples
    # and the dated venue companion still rides (the count's date scope needs it).
    assert any(e["rel_type"] == "located_in" and e["object"] == "natural history museum"
               and e.get("event_date") for e in edges), edges


def test_venue_no_instance_of_for_proper_tail():
    """FAIL-SAFE NEGATIVE through the real seam: a venue that is a proper name with a PROPER tail
    ("Lake Tahoe") is captured as a place but is NEVER typed instance_of a spurious head."""
    _detect_event_states = _detect_event_states_or_skip()
    ref = datetime.datetime(2026, 3, 3, tzinfo=datetime.timezone.utc)
    edges = _detect_event_states(
        "I took my niece to Lake Tahoe on February 8th.", reference=ref)
    inst = [e for e in edges if e["rel_type"] == "instance_of"]
    assert all(e["object"] not in ("tahoe", "lake") for e in inst), inst


def test_name_head_typing_flag_off_byte_identical():
    """Flag OFF → no instance_of typing edge (byte-for-byte legacy venue behavior)."""
    import importlib
    import src.api.main as main
    os.environ["NAME_HEAD_INSTANCE_TYPING"] = "0"
    try:
        importlib.reload(main)
        try:
            from src.extraction.linguistics import linguistics_available
            if not linguistics_available():
                pytest.skip("en_core_web_sm not available")
        except Exception:  # noqa: BLE001
            pytest.skip("spaCy not available")
        ref = datetime.datetime(2026, 3, 3, tzinfo=datetime.timezone.utc)
        edges = main._detect_event_states(
            "I took my niece to the Natural History Museum on February 8th.", reference=ref)
        assert not any(e["rel_type"] == "instance_of" for e in edges), edges
    finally:
        os.environ.pop("NAME_HEAD_INSTANCE_TYPING", None)
        importlib.reload(main)


# ── PART 2: count date-window parse + filter ─────────────────────────────────────────────────────
def test_count_window_parse_and_resolve():
    from src.api.main import _detect_count_intent, _resolve_count_window
    ci = _detect_count_intent(
        "How many different museums or galleries did I visit in the month of February?")
    assert ci.get("type_phrase") == "different museums or galleries", ci
    assert ci.get("window") == "february", ci
    # session reference from the leading [Date:] marker anchors the year.
    win = _resolve_count_window(ci["window"], "[Date: 2023/03/03 (Fri) 20:37]\nHow many ...?")
    assert win == ("2023-02-01", "2023-02-28"), win
    # a non-temporal PP must NOT resolve to a window (no month word-list — dateparser decides).
    assert _resolve_count_window("my area", "How many museums are in my area?") is None


def test_apply_instance_count_window_drops_distractor():
    """FAIL-ON-OLD: old `_apply_instance_count` had no `window` kwarg (TypeError) and counted every
    typed place (3). PASS-ON-FIX: the February window drops the January place → 2 (gold)."""
    from src.api.main import _apply_instance_count
    irels = {"instance_of", "is_a"}
    MUS, GAL = "u-museum", "u-gallery"
    NHM, ART, MAM = "u-nhm", "u-artcube", "u-mam"
    facts = [
        {"rel_type": "located_in", "_subject_id": "user", "_object_id": NHM,
         "event_date": "2026-02-08"},
        {"rel_type": "participated_in", "_subject_id": "user", "_object_id": ART,
         "event_date": "2026-02-15"},
        {"rel_type": "located_in", "_subject_id": "user", "_object_id": MAM,
         "event_date": "2026-01-20"},          # January distractor
        {"rel_type": "instance_of", "_subject_id": NHM, "_object_id": MUS},
        {"rel_type": "instance_of", "_subject_id": ART, "_object_id": GAL},
        {"rel_type": "instance_of", "_subject_id": MAM, "_object_id": MUS},
    ]
    unscoped = _apply_instance_count(facts, {MUS, GAL}, irels)
    assert unscoped["count"] == 3, unscoped
    feb = _apply_instance_count(facts, {MUS, GAL}, irels, window=("2026-02-01", "2026-02-28"))
    assert feb["count"] == 2 and set(feb["instance_ids"]) == {NHM, ART}, feb
    assert feb["held"] is False, feb
