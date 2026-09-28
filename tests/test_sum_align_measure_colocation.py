"""Merged-measure scalar CO-LOCATES on the dated-instance surface (SUM/duration reachability).

THE GAP (SUM diagnosis): a "how many days on camping trips" Σ walk reads the numeric
``entity_attributes`` scalar carried by each INSTANCE that is ``instance_of`` the type. The dated
instance is filed by the SVO/motion backbone under its FULL composed object surface — e.g.
``(user, go_on, "3-day solo camping trip to big sur")`` @date and
``("3-day solo camping trip to big sur", instance_of, trip)``. But ``_chain_attributive_merged_measure``
built its duration scalar's subject from ``_np_phrase(head)`` ALONE ("camping trip"), which:
  1. STRANDS the duration on a type-ish node unreachable from the dated instance, and
  2. COLLIDES across two same-type trips on the ``(subject, attribute)`` unique key ("camping trip",
     "duration") → one lost.
So ``_l4_direct_scalar_sum`` finds no numeric scalar on the instances → the Σ misses (days = 0).

THE FIX (deterministic, subject-agnostic): ``_chain_svo`` records ``object_token.i → composed
surface`` in a shared map; ``_chain_attributive_merged_measure`` re-uses that EXACT surface as the
scalar subject when the measured head is an SVO/motion object — so the duration lands on the SAME
entity as the date + ``instance_of``, and two trips get DISTINCT instance surfaces (no collision).
Byte-identical to today for any head with no divergent SVO surface (fail-safe → ``_np_phrase``).

PURE deriver tests — no DB, no network, no GLiNER2, no LLM. Need the real spaCy model.

Run: python3 -m pytest tests/test_sum_align_measure_colocation.py -q   (tests/ is gitignored → git add -f)
"""
import datetime
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction.linguistics import derive_sentence_facts  # noqa: E402

_REF = datetime.date(2023, 6, 1)


def _facts(sentence, reference=_REF):
    return list(derive_sentence_facts(sentence, reference=reference))


def _instance_surface(facts, type_name):
    """The subject of the ``instance_of <type_name>`` edge — the dated-instance surface."""
    for f in facts:
        if f.rel_type == "instance_of" and (f.object or "").lower() == type_name:
            return f.subject
    return None


def _scalar_on(facts, subject, rel):
    return [f for f in facts
            if f.scalar_datatype and f.subject == subject and f.rel_type == rel]


@pytest.mark.parametrize("sentence,expect_surface", [
    ("I went on a 3-day solo camping trip to Big Sur last Saturday.",
     "3-day solo camping trip to big sur"),
    ("I took a 5-day camping trip to Yellowstone in June.",
     "5-day camping trip to yellowstone"),
])
def test_merged_measure_duration_colocates_on_dated_instance(sentence, expect_surface):
    """The merged-measure duration scalar's subject == the ``instance_of`` subject (the dated instance
    surface the SVO backbone filed), NOT the bare ``_np_phrase`` type-ish node."""
    facts = _facts(sentence)
    inst = _instance_surface(facts, "trip")
    assert inst == expect_surface, f"instance surface not the SVO composed surface: {facts!r}"
    scal = _scalar_on(facts, inst, "duration")
    assert scal, f"duration scalar not co-located on the dated instance {inst!r}: {facts!r}"
    # the stranded type-ish subject ("camping trip") must NOT carry the merged-measure scalar anymore
    assert not _scalar_on(facts, "camping trip", "duration"), \
        f"duration scalar still stranded on the type-ish node: {facts!r}"


def test_two_same_type_trips_get_distinct_instance_surfaces_no_collision():
    """Both trips' durations survive because the instance surfaces DIFFER (the ``(subject, attribute)``
    collision that lost one duration is resolved by the shared dated-instance surface)."""
    f1 = _facts("I went on a 3-day solo camping trip to Big Sur last Saturday.")
    f2 = _facts("I took a 5-day camping trip to Yellowstone in June.")
    s1, s2 = _instance_surface(f1, "trip"), _instance_surface(f2, "trip")
    assert s1 and s2 and s1 != s2, f"instance surfaces collide: {s1!r} vs {s2!r}"
    assert _scalar_on(f1, s1, "duration") and _scalar_on(f2, s2, "duration"), \
        "one of the two trip durations did not survive on its own instance"


def test_alignment_is_byte_identical_when_np_phrase_already_matches():
    """FAIL-SAFE / non-regression: when ``_np_phrase(head)`` already equals the SVO object surface
    (no nummod-excluded premod, no PP tail), the aligned subject is UNCHANGED — the merged-measure
    scalar stays on the same reachable object entity."""
    for sentence, surface, val in (
        ("I bought a 3-bedroom apartment", "3-bedroom apartment", "3-bedroom"),
        ("I installed a 500-gb drive", "500-gb drive", "500-gb"),
        ("I built a 6-foot fence", "6-foot fence", "6-foot"),
    ):
        facts = _facts(sentence, reference=None)
        scal = [f for f in facts if f.scalar_datatype == "string" and f.object == val]
        assert scal, f"merged measure {val!r} not scalarized for {sentence!r}: {facts!r}"
        assert any(s.subject == surface for s in scal), \
            f"scalar subject drifted off the reachable object {surface!r}: {facts!r}"
