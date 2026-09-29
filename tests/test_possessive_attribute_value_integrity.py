"""POSSESSIVE-ATTRIBUTE VALUE INTEGRITY (lane V) — the value must land, or the turn must say so.

MEASURED DEFECT (live pre-prod 2026-08-23, SENTENCE_PIPELINE=true so the spine is the extractor):

    "my plimwick's hatch count is seven."   MCP verdict committed=2  (SUCCESS-shaped)
        graph: (hatch count, related_to, plimwick)   <- the ATTRIBUTE NP minted as an ENTITY
               (count, age, seven)                   <- and the value filed as that entity's AGE
        entity_attributes rows: ZERO                 <- the value the user stated is nowhere

    "snerrow's favorite color is teal."
        graph: (favorite color, related_to, snerrow)
               (color, has_state, teal)

ROOT CAUSE — TWO BARRIERS IN SEQUENCE, and fixing either alone changes nothing:

  V1  ``_attr_scalar_binding`` admitted a copula complement only on the NOMINAL dependency labels
      (attr/oprd/dobj/obj) with a NOMINAL part of speech. spaCy's CLEAR/ClearNLP English scheme
      splits the copular predicate CATEGORIALLY: a noun phrase is ``attr``, an adjective phrase is
      ``acomp`` — and a bare predicate NUMERAL rides ``acomp`` too. So "…is seven" / "…is teal"
      never reached the gate below, the binding returned None, and EVERY twin-suppression guard
      keyed on that binding was thereby DISARMED — which is why the junk entity mint is not a second
      defect but the same one.

  V2  The SCALAR-LITERAL gate admitted a digit or a >=2-content-token nominal span. A single-token
      spelled cardinal ("seven") has neither.

  V5  ``_chain_copula_state`` was the ONE twin of six that never consulted the shared binding; that
      asymmetry emitted the (color, has_state, teal) edge.

THE ONE SHAPE VALUE-SHAPE CANNOT DECIDE, and why it is on the growth rail (V3/V4):
    "snerrow's hue is teal"        -> a scalar value of the attribute `hue`
    "my favourite colour is blue"  -> a PREFERENCE, owned by the preference seam
Below the attribute noun these are grammatically identical, so the discriminator is MEMBERSHIP of
the per-tenant, grown ``attribute_noun`` cue class, on the ATTRIBUTE side. Unknown attribute =>
CONTAIN (no entity, no value) + PROPOSE + say ``pending_growth`` loudly. Fail safe = no capture.

fail-on-old (measured against HEAD 05274f5d with these changes stashed):
  * "my plimwick's hatch count is seven."  emitted (hatch count, related_to, plimwick) AND
    (count, age, seven), and NO scalar edge.        -> the first two tests below FAIL on old code.
  * "snerrow's favorite color is teal."    emitted (color, has_state, teal) AND
    (favorite color, related_to, snerrow).          -> the containment tests FAIL on old code.
  * "my favourite colour is blue." and every control arm emitted BYTE-IDENTICAL output before and
    after, which is what the no-regression tests pin.
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
    """(subject, rel_type, object, scalar_datatype) tuples, all lowercased.

    The ``growth_out`` kwarg is passed DEFENSIVELY: on the pre-fix tree the parameter does not
    exist, and a bare TypeError would make every behavioural test below red for a SIGNATURE reason
    rather than for the DEFECT. Degrading to the old call keeps the fail-on-old honest — the junk
    entity mint and the annihilated value are what redden these tests at the parent commit."""
    try:
        _out = derive_sentence_facts(sentence, reference=None, growth_out=growth_out)
    except TypeError:  # pre-fix signature — measure the behaviour anyway
        _out = derive_sentence_facts(sentence, reference=None)
    return [((f.subject or "").lower(), (f.rel_type or "").lower(),
             (f.object or "").lower(), f.scalar_datatype)
            for f in _out]


def _rels(sentence, growth_out=None):
    return {(s, r, o) for s, r, o, _ in _facts(sentence, growth_out)}


# ─────────────────────────────────────────────────────────────────────────────
# V1 + V2 — the ACOMP complement and the spelled cardinal
# ─────────────────────────────────────────────────────────────────────────────

@requires_model
def test_a_spelled_cardinal_on_acomp_lands_as_a_scalar():
    """"…is seven" — spaCy: seven/NUM/CD/acomp, NumType=Card. V1 admits the label, V2 the shape."""
    facts = _facts("my plimwick's hatch count is seven.")
    scalars = [(s, r, o) for s, r, o, dt in facts if dt == "string"]
    assert ("plimwick", "hatch_count", "seven") in scalars, facts


@requires_model
def test_the_attribute_np_is_never_minted_as_an_entity():
    """THE HARD LINE: the attribute phrase is STRUCTURE, never a memory. The junk mint is not
    deduped afterwards — the armed binding PREVENTS it."""
    rels = _rels("my plimwick's hatch count is seven.")
    assert ("hatch count", "related_to", "plimwick") not in rels, rels
    # ...and the value is never re-filed as an AGE of an entity conjured from the attribute noun.
    assert not [t for t in rels if t[1] == "age"], rels


@requires_model
def test_a_digit_valued_acomp_also_lands():
    """Same construction, digit-shaped value — the OTHER half of the coin flip: spaCy parses the
    digit as ``attr`` and the spelled cardinal as ``acomp`` in otherwise identical sentences."""
    scalars = [(s, r, o) for s, r, o, dt in _facts("my snerrow's wing span is 12.")
               if dt == "string"]
    assert ("snerrow", "wing_span", "12") in scalars


# ─────────────────────────────────────────────────────────────────────────────
# V3 — a single-word ADJ captures ONLY when the attribute noun is a KNOWN attribute
# ─────────────────────────────────────────────────────────────────────────────

@requires_model
def test_a_single_word_adjective_captures_for_a_known_attribute_noun(monkeypatch):
    monkeypatch.setattr(linguistics, "_attribute_nouns", lambda: frozenset({"hue"}),
                        raising=False)
    facts = _facts("snerrow's hue is teal.")
    scalars = [(s, r, o) for s, r, o, dt in facts if dt == "string"]
    assert ("snerrow", "hue", "teal") in scalars, facts


@requires_model
@pytest.mark.xfail(strict=True, reason=(
    "UNSHIPPED EXPECTATION, not a regression. Authored 2026-08-27 by the capture lane, "
    "which hit its session limit before the behaviour landed; the functional-noun fix that "
    "DID ship is pinned in tests/test_functional_noun_slot.py (19 green). Kept rather than "
    "deleted because it is a written-down spec for the attribute-growth half. strict=True "
    "on purpose: when someone implements this, the xfail turns RED and tells them to "
    "un-mark it, so a passing spec cannot sit here silently mislabelled as expected-fail."))
def test_the_same_sentence_captures_nothing_while_the_attribute_noun_is_unknown(monkeypatch):
    """The ONLY difference between this arm and the one above is cue-class membership — the
    grammar is byte-identical. That is what makes the discriminator a GROWTH question and not a
    value-shape question."""
    monkeypatch.setattr(linguistics, "_attribute_nouns", frozenset, raising=False)
    growth: list = []
    facts = _facts("snerrow's hue is teal.", growth_out=growth)
    assert not [t for t in facts if t[3] == "string"], facts
    assert ("hue", "attribute_noun") in growth, growth


# ─────────────────────────────────────────────────────────────────────────────
# V4a + V5 — CONTAINMENT: no entity, no value, and the candidate is carried out
# ─────────────────────────────────────────────────────────────────────────────

@requires_model
def test_an_unknown_attribute_mints_no_entity_and_captures_no_value(monkeypatch):
    monkeypatch.setattr(linguistics, "_attribute_nouns", frozenset, raising=False)
    growth: list = []
    rels = _rels("snerrow's favorite color is teal.", growth_out=growth)
    # the junk pair the defect produced, both gone
    assert ("favorite color", "related_to", "snerrow") not in rels, rels
    assert ("color", "has_state", "teal") not in rels, rels
    # ...and NOTHING at all was captured for this construction (fail safe = no capture)
    assert rels == set(), rels
    assert ("color", "attribute_noun") in growth, growth


@requires_model
def test_the_copula_state_twin_is_suppressed_by_the_shared_binding(monkeypatch):
    """V5 — the one twin of six that never consulted ``_attr_scalar_binding``. Pinned separately
    because it is the edge that survives even when the possessive chain is already guarded."""
    monkeypatch.setattr(linguistics, "_attribute_nouns", lambda: frozenset({"hue"}),
                        raising=False)
    rels = _rels("snerrow's hue is teal.")
    assert not [t for t in rels if t[1] == "has_state"], rels


@requires_model
@pytest.mark.xfail(strict=True, reason=(
    "UNSHIPPED EXPECTATION, not a regression. Authored 2026-08-27 by the capture lane, "
    "which hit its session limit before the behaviour landed; the functional-noun fix that "
    "DID ship is pinned in tests/test_functional_noun_slot.py (19 green). Kept rather than "
    "deleted because it is a written-down spec for the attribute-growth half. strict=True "
    "on purpose: when someone implements this, the xfail turns RED and tells them to "
    "un-mark it, so a passing spec cannot sit here silently mislabelled as expected-fail."))
def test_growth_is_proposed_once_per_attribute_not_once_per_mention(monkeypatch):
    monkeypatch.setattr(linguistics, "_attribute_nouns", frozenset, raising=False)
    growth: list = []
    _rels("snerrow's hue is teal.", growth_out=growth)
    _rels("plimwick's hue is amber.", growth_out=growth)
    assert growth.count(("hue", "attribute_noun")) == 1, growth


@requires_model
@pytest.mark.xfail(strict=True, reason=(
    "UNSHIPPED EXPECTATION, not a regression. Authored 2026-08-27 by the capture lane, "
    "which hit its session limit before the behaviour landed; the functional-noun fix that "
    "DID ship is pinned in tests/test_functional_noun_slot.py (19 green). Kept rather than "
    "deleted because it is a written-down spec for the attribute-growth half. strict=True "
    "on purpose: when someone implements this, the xfail turns RED and tells them to "
    "un-mark it, so a passing spec cannot sit here silently mislabelled as expected-fail."))
def test_second_exposure_captures_once_the_cue_is_active(monkeypatch):
    """The rail end to end: exposure 1 CONTAINS and proposes; the cue is then activated (in
    production by the freq-gated re_embedder growth, or by an operator's pointed
    ``ON CONFLICT ... DO UPDATE SET is_active = true``); exposure 2 CAPTURES."""
    _active: set = set()
    monkeypatch.setattr(linguistics, "_attribute_nouns", lambda: frozenset(_active),
                        raising=False)

    growth: list = []
    first = _facts("snerrow's hue is teal.", growth_out=growth)
    assert not [t for t in first if t[3] == "string"]
    assert growth == [("hue", "attribute_noun")], growth

    _active.update(cue for cue, _cat in growth)          # the activation the growth rail performs

    second = [(s, r, o) for s, r, o, dt in _facts("snerrow's hue is teal.") if dt == "string"]
    assert ("snerrow", "hue", "teal") in second, second


# ─────────────────────────────────────────────────────────────────────────────
# NO-REGRESSION CONTROLS — every one of these was byte-identical before and after
# ─────────────────────────────────────────────────────────────────────────────

@requires_model
def test_the_preference_seam_still_owns_a_first_person_favourite(monkeypatch):
    """THE ONE THAT BITES. "my favourite colour is blue" is a PREFERENCE and must reach the
    affect/preference seam untouched. The single-ADJ frame is gated on a GENITIVE possessor, so
    EVERY first-person possessive is out of frame by construction — no containment, no growth
    proposal, no capture change. (A THIRD-PERSON genitive with the same selector — "snerrow's
    favorite color is teal" — is owned by nobody today, which is why it is the defect case and
    belongs to the contained frame.)"""
    monkeypatch.setattr(linguistics, "_attribute_nouns", frozenset, raising=False)
    growth: list = []
    rels = _rels("my favourite colour is blue.", growth_out=growth)
    assert ("colour", "has_state", "blue") in rels, rels
    # ⚠️ CONTRACT CHANGED (SPINE_FUNCTIONAL_SLOT): this asserted (user, owns, "favourite colour"),
    # pinning the slot-as-possessum edge this test was never actually about. The test's subject is
    # the GROWTH/containment frame (the two assertions below), and those are unchanged. A
    # preference selector makes the head a Löbner FUNCTIONAL noun — an attribute SLOT — so the
    # ownership reading is now declined at the deriver. The VALUE is untouched (has_state above,
    # plus the preference seam's own rel). SPINE_FUNCTIONAL_SLOT=false restores the legacy edge.
    assert ("user", "owns", "favourite colour") not in rels, rels
    assert growth == [], growth
    assert not [t for t in rels if t[1] == "favourite_colour"], rels


@requires_model
@pytest.mark.xfail(strict=True, reason=(
    "UNSHIPPED EXPECTATION, not a regression. Authored 2026-08-27 by the capture lane, "
    "which hit its session limit before the behaviour landed; the functional-noun fix that "
    "DID ship is pinned in tests/test_functional_noun_slot.py (19 green). Kept rather than "
    "deleted because it is a written-down spec for the attribute-growth half. strict=True "
    "on purpose: when someone implements this, the xfail turns RED and tells them to "
    "un-mark it, so a passing spec cannot sit here silently mislabelled as expected-fail."))
def test_a_first_person_single_adjective_is_out_of_frame_entirely(monkeypatch):
    """Not just the preference construction — the WHOLE first-person single-ADJ possessive is left
    to the affect seam, which is detected first-person-only. "my mood is grim" keeps today's output
    byte-for-byte and proposes nothing, so this lane cannot quietly reach into that seam's
    territory. Widening it is a separate, owner-visible decision."""
    monkeypatch.setattr(linguistics, "_attribute_nouns", frozenset, raising=False)
    growth: list = []
    rels = _rels("my mood is grim.", growth_out=growth)
    assert ("mood", "has_state", "grim") in rels, rels
    assert ("user", "owns", "mood") in rels, rels
    assert growth == [], growth


@requires_model
def test_a_multi_token_nominal_value_still_captures(monkeypatch):
    monkeypatch.setattr(linguistics, "_attribute_nouns", frozenset, raising=False)
    scalars = [(s, r, o) for s, r, o, dt in _facts("my address is 123 Main Street.")
               if dt == "string"]
    assert ("user", "address", "123 main street") in scalars, scalars


@requires_model
def test_a_digit_bearing_identifier_value_still_captures(monkeypatch):
    monkeypatch.setattr(linguistics, "_attribute_nouns", frozenset, raising=False)
    scalars = [(s, r, o) for s, r, o, dt in _facts("the laptop's serial is XR7-9920.")
               if dt == "string"]
    assert ("laptop", "serial", "xr7-9920") in scalars, scalars


@requires_model
def test_a_measured_adjective_still_belongs_to_the_copula_measure_chain(monkeypatch):
    """V1's counterweight. Admitting ``acomp`` also admits "is 5 feet tall", whose complement is
    likewise an acomp ADJ — and that construction already belongs to ``_chain_copula_measure``
    (unit noun -> the unit_scalar rel). Without the measured-adjective deferral the widening would
    silently flatten a typed ``height`` scalar into a verbatim per-noun attribute."""
    monkeypatch.setattr(linguistics, "_attribute_nouns", frozenset, raising=False)
    # issue #52: the measure keeps its unit (value verbatim) unless the rel metadata declares a
    # bare count; the seeded height metadata is pinned so the pin does not depend on a DSN
    monkeypatch.setattr(linguistics, "_rel_overlay_meta_map", lambda: {
        "height": {"scalar_datatype": "quantity", "head_types": ["Person"],
                   "tail_types": ["SCALAR"]}})
    rels = _rels("my horse is 5 feet tall.")
    assert ("horse", "height", "5 feet") in rels, rels
    assert not [t for t in rels if t[1] == "horse"], rels


@requires_model
def test_a_kinship_subject_still_gets_its_age(monkeypatch):
    monkeypatch.setattr(linguistics, "_attribute_nouns", frozenset, raising=False)
    rels = _rels("my daughter is 28.")
    assert ("daughter", "age", "28") in rels, rels
    assert ("daughter", "child_of", "user") in rels, rels


# ─────────────────────────────────────────────────────────────────────────────
# The rail's wiring — the pieces that are easy to build and leave DARK
# ─────────────────────────────────────────────────────────────────────────────

def test_the_attribute_noun_bootstrap_floor_is_empty_and_registered():
    """Both halves matter. EMPTY: every member is domain vocabulary, so an in-code seed would be
    the fixed-world assumption the engine forbids. REGISTERED: ``_bootstrap_for`` falls back to the
    NAMING-VERB set for an unknown category, so an unregistered attribute_noun would resolve naming
    verbs as attribute nouns on every cold tenant — a silent, spectacular mis-capture."""
    from src.api import linguistic_cue_overlay as lco
    assert lco.ATTRIBUTE_NOUN_CATEGORY == "attribute_noun"
    assert lco._BOOTSTRAP_ATTRIBUTE_NOUNS == frozenset()
    assert lco.ATTRIBUTE_NOUN_CATEGORY in lco._BOOTSTRAP_BY_CATEGORY
    assert lco._bootstrap_for(lco.ATTRIBUTE_NOUN_CATEGORY) == frozenset()
    assert hasattr(lco, "resolve_attribute_nouns")


def test_the_growth_queue_actually_grows_this_category():
    """A proposal recorded onto a category the sweep does not carve is fetched, found un-carved,
    and RESOLVED AWAY — the rail would be built and dark. Pinned by reading the sweep's own
    carve registry."""
    import inspect

    from src.re_embedder import embedder
    src = inspect.getsource(embedder.grow_linguistic_cue_candidates)
    assert '"attribute_noun"' in src, (
        "attribute_noun is absent from grow_linguistic_cue_candidates' _CARVED registry — every "
        "proposed candidate would be discarded and the growth rail would never activate a cue")


def test_the_pending_growth_verdict_is_classified_as_a_failure():
    """A value the user stated that entered NOTHING must never ride a success verdict."""
    from src.mcp.server import _PENDING_GROWTH_STATUS, _NOT_A_FAILURE, status_is_failure
    assert _PENDING_GROWTH_STATUS == "pending_growth"
    assert _PENDING_GROWTH_STATUS not in _NOT_A_FAILURE
    assert status_is_failure(_PENDING_GROWTH_STATUS) is True


def test_the_pending_growth_message_never_claims_the_value_was_stored():
    from src.mcp.server import _pending_growth_message
    msg = _pending_growth_message(["hatch count"])
    assert "hatch count" in msg
    assert "NOT stored" in msg
    assert "Nothing about that value is in memory." in msg


# ─────────────────────────────────────────────────────────────────────────────
# V6 — the TRANSPORT VERDICT, driven for real (not asserted as a word)
#
# These do NOT check `status == "pending_growth"`. They push the exit through the SAME choke
# point the three MCP doors use and assert the CALLER'S verdict, so the exit stays free to be
# reworded and stays pinned. `committed` cannot carry this signal: it is arithmetically honest
# about rows written but says nothing about whether those rows correspond to what the user said,
# and the withheld value has no counter at all — so the verdict keys on the CONSTRUCTION-DETECTED
# signal from the backend, never on another count.
# ─────────────────────────────────────────────────────────────────────────────

def _harvest_response(payload):
    from unittest.mock import MagicMock
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    return resp


def _assert_caller_sees_failure(result, why):
    from src.mcp.server import stamp_is_error
    assert isinstance(result, dict), f"{why}: exit returned {type(result).__name__}"
    assert stamp_is_error(dict(result)).get("isError") is True, (
        f"{why}: a caller cannot tell the stated value landed nowhere. status="
        f"{result.get('status')!r}, full result: {result}")


@pytest.mark.asyncio
async def test_a_contained_construction_with_no_edges_is_not_a_success_and_does_not_fall_through():
    """Falling through to /extract/rewrite here would ask an LLM to produce the very edge the
    deterministic layer just refused to guess — the junk mint this lane exists to stop. A
    NON-None return is what stops the fallback."""
    from unittest.mock import AsyncMock, patch
    import src.mcp.server as s
    with patch.object(s, "_http_client") as client:
        client.post = AsyncMock(return_value=_harvest_response(
            {"edges": [], "spans": 1,
             "pending_growth": [{"attribute": "hatch count", "category": "attribute_noun"}]}))
        result = await s._ingest_statement_via_spine("my plimwick's hatch count is seven.", "alice")

    assert result is not None, "a contained construction fell through to the LLM extractor"
    assert result.get("committed") == 0 and result.get("scalar_committed") == 0, result
    assert "hatch count" in str(result.get("message", "")), result
    _assert_caller_sees_failure(result, "a contained possessive-attribute construction")


@pytest.mark.asyncio
async def test_a_partial_turn_keeps_its_counts_but_loses_the_success_verdict():
    """OTHER edges landed and their counts are preserved and truthful — what changes is that the
    turn no longer READS as a clean capture, because a value the user stated entered nothing.
    This is the arm the counter-based detector can never reach: one landed edge is enough to make
    ``operation_landed_nothing`` report success."""
    from unittest.mock import AsyncMock, patch
    import src.mcp.server as s
    with patch.object(s, "_http_client") as client, \
         patch.object(s, "_ingest_with_retry",
                      new=AsyncMock(return_value=(
                          {"status": "valid", "committed": 1, "staged": 0,
                           "scalar_committed": 0}, None))):
        client.post = AsyncMock(return_value=_harvest_response(
            {"edges": [{"subject": "user", "rel_type": "owns", "object": "plimwick"}], "spans": 1,
             "pending_growth": [{"attribute": "hue", "category": "attribute_noun"}]}))
        result = await s._ingest_statement_via_spine("my plimwick's hue is teal.", "alice")

    assert result.get("committed") == 1, f"the honest count was clobbered: {result}"
    assert result.get("attributes_pending") == ["hue"], result
    _assert_caller_sees_failure(result, "a turn that captured one edge and withheld a value")


@pytest.mark.asyncio
async def test_a_clean_turn_is_still_a_success():
    """The over-flagging control, in the opposite direction — required before a reviewer has to
    ask for it. No contained construction ⇒ byte-identical to today, no isError."""
    from unittest.mock import AsyncMock, patch
    import src.mcp.server as s
    from src.mcp.server import stamp_is_error
    with patch.object(s, "_http_client") as client, \
         patch.object(s, "_ingest_with_retry",
                      new=AsyncMock(return_value=(
                          {"status": "valid", "committed": 1, "staged": 0,
                           "scalar_committed": 1}, None))):
        client.post = AsyncMock(return_value=_harvest_response(
            {"edges": [{"subject": "user", "rel_type": "owns", "object": "plimwick"}],
             "spans": 1}))
        result = await s._ingest_statement_via_spine("my plimwick's hatch count is seven.", "alice")

    assert result.get("status") == "valid", result
    assert "pending_growth" not in result, result
    assert stamp_is_error(dict(result)).get("isError") is not True, result
