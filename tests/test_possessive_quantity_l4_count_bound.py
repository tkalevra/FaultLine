"""POSSESSED-QUANTITY L4 - THE COUNT BOUND (critic round 1, issue #19 W1 lane).

The critic measured 9/12 count frames flipping at 0bdc1916: the UNMAPPED-UNIT ADMISSION
(shape-valid dobj/obj + nummod NUM, no further bound) admitted COUNT constructions -
"my dog has 4 legs" is a count, not a measurement - minting duplicate
(<thing>, related_measure, "<N> <noun>") rows, proposing the count nouns (leg, bedroom,
page, tire, player, bathroom, strap, kilometer) as unit_scalar candidates (junk growth:
a count noun would GROW into the tenant map at 3 mentions - the exact thing owner doctrine
forbids), and swapping the possession backbone owns->related_to. The odometer frame's
twin-drop annihilated the (bike, have, '13000 kilometers') have-edge the count lane
(6a0191a8/01716476 value_bears_unit consult) reads.

THE BOUND (per the orchestrator ruling, option (a) - the of-PP PARTITIVE): the unmapped
admission requires the unit noun to head a PARTITIVE of-PP ("800 grams OF FISH" - the
of-phrase names the measured substance). Mapped units bypass it (the unit_scalar map IS
the measurement cue class). Documented residual (GLM.md): an unmapped BARE unit ("13
kilometers", no of-PP) stays declined until a measure-unit cue class exists to admit it -
option (b) as a floor class could keep such frames admissible but cannot admit an
unknown unit's FIRST sighting (membership is circular for a first sighting) and would
re-open the odometer frame.

RED AT BASELINE: every pin below is red at 0bdc1916 by construction (measured: the
flag-on rows carried the related_measure duplicates + junk candidates there, and the
flag-on/off byte-identity asserted here failed on 8 of the 12 frames).
"""
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("SENTENCE_PIPELINE", "true")

from src.extraction import linguistics as L  # noqa: E402

COUNT_FRAMES = [
    ("My dog has 4 legs.", "dog", "legs"),
    ("My house has 3 bedrooms.", "house", "bedrooms"),
    ("The contract has 12 pages.", "contract", "pages"),
    ("My car has 4 tires.", "car", "tires"),
    ("My team has 11 players.", "team", "players"),
    ("My book has 300 pages.", "book", "pages"),
    ("My apartment has 2 bathrooms.", "apartment", "bathrooms"),
    ("My watch has 2 straps.", "watch", "straps"),
    ("My bike has 13000 kilometers on it.", "bike", "kilometers"),
    ("My commute runs 13 kilometers.", "commute", "kilometers"),
    ("My car has 50000 kilometers on the odometer.", "car", "kilometers"),
    ("The shelf holds 40 books.", "shelf", "books"),
]

MEASURE_FRAMES = [
    ("My bouillabaisse recipe uses 800 grams of fish.", "bouillabaisse recipe", "quantity"),
    ("My daily commute takes 45 minutes each way.", "daily commute", "duration"),
    ("My bouillabaisse recipe uses 800 zorphs of fish.", "bouillabaisse recipe", "related_measure"),
    ("My sourdough recipe uses 3 cups of flour.", "sourdough recipe", "related_measure"),
]


def _rows_flagged(text, flag):
    saved = L.SPINE_POSSESSED_QUANTITY_L4
    L.SPINE_POSSESSED_QUANTITY_L4 = flag
    try:
        growth = []
        facts = L.derive_sentence_facts(text, None, growth_out=growth)
        rows = sorted((f.subject, f.rel_type, f.object, f.scalar_datatype or "")
                      for f in facts)
        return rows, sorted(growth)
    finally:
        L.SPINE_POSSESSED_QUANTITY_L4 = saved


@pytest.mark.parametrize("sentence,thing,unit", COUNT_FRAMES)
def test_count_frame_is_flag_invariant_and_candidate_free(sentence, thing, unit):
    """Flag ON must produce the flag-OFF rows byte-identical (the admission never fires
    on a count frame), with ZERO unit_scalar candidates (a count noun never grows into
    the tenant map) and ZERO related_measure rows / quantity rows carrying the count
    unit (the count stays a count-attribute row on the thing, never a measurement)."""
    rows_on, growth_on = _rows_flagged(sentence, True)
    rows_off, growth_off = _rows_flagged(sentence, False)
    assert rows_on == rows_off, (
        f"the flag changed the rows for the count frame {sentence!r}: "
        f"on={rows_on} off={rows_off}")
    assert growth_on == [] and growth_off == [], (
        f"a count frame proposed a growth candidate: {growth_on} / {growth_off} "
        f"for {sentence!r}")
    assert not any(r == "related_measure" for _s, r, _o, _dt in rows_on), (
        f"a count frame filed a related_measure row: {rows_on}")
    assert not any(unit in (o or "") and dt == "quantity"
                   for _s, _r, o, dt in rows_on), (
        f"a count frame filed a quantity row carrying the count unit: {rows_on}")


@pytest.mark.parametrize("sentence,thing,unit",
                         [f for f in COUNT_FRAMES if f[0].startswith(("My ", "The "))])
def test_possession_backbone_unchanged_flag_on(sentence, thing, unit):
    """For a speaker-possessed thing tail the owns backbone stands under flag ON - the
    L4 arm never swaps the possession verb (owns is gate-legal for Animal/Object tails;
    the related_to swap was an unrequested behaviour change, reverted by the bound)."""
    rows_on, _g = _rows_flagged(sentence, True)
    rows_off, _g2 = _rows_flagged(sentence, False)
    owns_off = [r for r in rows_off if r[0] == "user" and r[1] == "owns" and r[2] == thing]
    owns_on = [r for r in rows_on if r[0] == "user" and r[1] == "owns" and r[2] == thing]
    assert owns_on == owns_off, (
        f"the possession backbone moved under flag ON for {sentence!r}: on={owns_on} "
        f"off={owns_off} rows_on={rows_on}")
    assert not any(r[0] == "user" and r[1] == "related_to" and r[2] == thing
                   for r in rows_on), (
        f"the arm swapped owns for related_to on a count frame: {rows_on}")


def test_odometer_have_edge_survives_flag_on():
    """'My bike has 13000 kilometers on it.' - the (bike, have, '13000 kilometers') scalar
    is the count lane food (the value_bears_unit consult); the L4 twin-drop must never
    touch it (the frame is a count: no of-PP -> no admission -> no fire -> no drop)."""
    rows_on, growth_on = _rows_flagged("My bike has 13000 kilometers on it.", True)
    assert ("bike", "have", "13000 kilometers", "string") in rows_on, rows_on
    assert ("user", "owns", "bike", "") in rows_on, rows_on
    assert growth_on == [], growth_on
    assert not any(r == "related_measure" for _s, r, _o, _dt in rows_on), rows_on


@pytest.mark.parametrize("sentence,concept,scalar_rel", MEASURE_FRAMES)
def test_measurement_frames_still_fire_the_l4_shape(sentence, concept, scalar_rel):
    """The bound narrows the ADMISSION, never the arm: mapped frames (c1, commute) and
    unmapped PARTITIVE frames (zorph, cups) keep the concept-level L4 emit."""
    rows_on, growth_on = _rows_flagged(sentence, True)
    assert any(s == "user" and r == "related_to" and o == concept
               for s, r, o, _dt in rows_on), rows_on
    assert any(s == concept and r == scalar_rel and dt == "quantity"
               for s, r, _o, dt in rows_on), rows_on
    assert not any(s == "user" and r == concept.replace(" ", "_")
                   for s, r, _o, _dt in rows_on), (
        f"the flat user weld returned beside the L4 shape: {rows_on}")
    if scalar_rel == "related_measure":
        # the unmapped frames still propose the unit (the growth half rides the fire)
        assert growth_on != [], "the unmapped partitive frame must still propose the unit"

# ── ROUND 2: THE LEAK-SIM — the mapped arm under a GROWN map (critic round 2, owner ruling) ───
# The mapped arm bypasses the partitive bound BY DESIGN (the unit_scalar map IS the
# measurement cue class), so once a PARTITIVE frame grows a unit ('13 kilometers of forest'
# x3 -> kilometer grown), the mapped binding re-admits BARE count-flavored frames and the
# wound returns: measured pre-guard at HEAD, the odometer frame fired the arm under a grown
# map — the twin-drop ate the (bike, have, '13000 kilometers') have-edge the count lane's
# value_bears_unit consult reads, and the backbone swapped owns->related_to. THE GUARD: on
# possession-verb frames (have/own/possess lemmas — the count-lane backbone verbs) the mapped
# binding declines the re-model entirely (flag-gated; the unmapped partitive admission is
# exempt — the partitive bound governs it); on measure-verb frames (use/take/run class + the
# copula) the mapped arm admits growth-provisioned units as today.


def _with_grown_map():
    """Context helper: a kilometer-provisioned unit_scalar map (the state AFTER the partitive
    frame '13 kilometers of forest' grew kilometer at 3 mentions)."""
    saved = L._unit_scalar_map
    floor = dict(saved())
    floor.update({"kilometer": "related_measure", "kilometers": "related_measure"})
    L._unit_scalar_map = lambda: dict(floor)
    return saved


_GROWN_FRAMES = [
    # (tag, sentence) — possession frames the count lane owns even post-growth
    ("odo", "My bike has 13000 kilometers on it."),
    ("tank", "My tank has 20 liters."),
]


def test_odometer_have_edge_survives_grown_map():
    """THE LEAK-SIM PIN (the ruling's headline): the odometer frame with a GROWN kilometer
    map must return EXACTLY the pre-growth rows — (bike, have, '13000 kilometers', string)
    + (user, owns, bike) — have-edge intact, count lane reads it, NO measure row from the
    mapped arm, NO related_to swap, NO candidates. Measured pre-guard: related_measure
    ('13000 kilometers on it') + related_to — the wound, byte-for-byte."""
    saved = _with_grown_map()
    try:
        rows_on, growth_on = _rows_flagged("My bike has 13000 kilometers on it.", True)
    finally:
        L._unit_scalar_map = saved
    assert rows_on == [
        ("bike", "have", "13000 kilometers", "string"),
        ("user", "owns", "bike", ""),
    ], rows_on
    assert growth_on == [], growth_on


def test_possession_frames_stay_count_lane_under_grown_map():
    """The tank frame (have + a mapped unit) — the same leak class: the mapped arm must not
    re-model a possession frame even when the unit is in the map. The count lane keeps its
    verb edge and the owns backbone; no measure row, no related_to swap."""
    saved = _with_grown_map()
    try:
        rows_on, growth_on = _rows_flagged("My tank has 20 liters.", True)
        rows_off, _g2 = _rows_flagged("My tank has 20 liters.", False)
    finally:
        L._unit_scalar_map = saved
    assert not any(r == "related_measure" for _s, r, _o, _dt in rows_on), rows_on
    assert not any(r == "related_to" and o == "tank" for _s, r, o, _dt in rows_on), rows_on
    assert ("user", "owns", "tank", "") in rows_on, rows_on
    # the flag-off weld is the mapped decline's standard fallback — the guard must never
    # touch flag-off (the weld row survives flag-off unchanged, byte-for-byte pre/post-guard)
    assert ("user", "tank", "20 liters", "string") in rows_off, rows_off


def test_flag_off_byte_identical_under_grown_map():
    """Flag-honesty under the grown map: flag OFF keeps today's mapped-only decline
    byte-for-byte (the binding's mapped gate is ungated — the weld + the count lane's verb
    edge, no arm, no drop). The guard is flag-gated and never acts flag-off."""
    saved = _with_grown_map()
    try:
        rows_off, growth_off = _rows_flagged("My bike has 13000 kilometers on it.", False)
    finally:
        L._unit_scalar_map = saved
    assert rows_off == [
        ("bike", "have", "13000 kilometers", "string"),
        ("user", "bike", "13000 kilometers on it", "string"),
    ], rows_off
    assert growth_off == [], growth_off


def test_copula_measure_persists_under_grown_map():
    """'My run is 13 kilometers.' — the copula-measure capture must persist under the grown
    map (the possession gate keys the VERB LEMMA: have/own/possess — never the copula; a
    measure-verb/copula frame admits growth-provisioned units as today). Flag-invariant."""
    saved = _with_grown_map()
    try:
        rows_on, _g = _rows_flagged("My run is 13 kilometers.", True)
        rows_off, _g2 = _rows_flagged("My run is 13 kilometers.", False)
    finally:
        L._unit_scalar_map = saved
    assert rows_on == rows_off == [("user", "run", "13 kilometers", "string")], (
        rows_on, rows_off)


def test_measurement_frames_unaffected_by_possession_guard():
    """The guard is possession-verb-scoped: the measure-verb class (use/take) admits
    growth-provisioned units exactly as before — re-fire the 4 measurement frames under the
    grown map and demand the same L4 shapes (this is the same-state regression the ruling
    orders with the guard)."""
    saved = _with_grown_map()
    try:
        c1_rows, _a = _rows_flagged("My bouillabaisse recipe uses 800 grams of fish.", True)
        comm_rows, _b = _rows_flagged("My daily commute takes 45 minutes each way.", True)
        zorph_rows, zorph_g = _rows_flagged(
            "My bouillabaisse recipe uses 800 zorphs of fish.", True)
        cups_rows, cups_g = _rows_flagged("My sourdough recipe uses 3 cups of flour.", True)
    finally:
        L._unit_scalar_map = saved
    assert ("bouillabaisse recipe", "quantity", "800 grams of fish", "quantity") in c1_rows
    assert ("daily commute", "duration", "45 minutes each way", "quantity") in comm_rows
    assert ("bouillabaisse recipe", "related_measure",
            "800 zorphs of fish", "quantity") in zorph_rows
    assert ("sourdough recipe", "related_measure", "3 cups of flour", "quantity") in cups_rows
    assert ("zorph", "unit_scalar") in zorph_g and ("cup", "unit_scalar") in cups_g
