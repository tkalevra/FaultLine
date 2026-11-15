"""Contract gate for meaning-coverage OPTION A — deriver-level capture-modeling of a possessed
NOVEL common-noun that HAS a measure (flag ``SPINE_PENDING_GROUNDING``, default OFF).

THE GAP (DB-verified): "My commute to work is 45 minutes" was derived by the spine as a FLAT
``(user, commute, "45 minutes")`` edge — the possessed THING ``commute`` becomes a rel_type. At
/ingest that novel rel mints ``category='pending_placement'`` with NO concept node and NO
instance_of/subclass_of ladder; grounding it then hits THE HARD LINE (entity_registry: a rel_type
name cannot ALSO be a concept entity) — and the VALUE IS LOST. THE HARD LINE was correctly signalling
a MALFORMED capture.

OPTION A fixes it AT CAPTURE (``src/extraction/linguistics.py`` ``_chain_attr_scalar`` — the possessed-
attribute scalar chain). A NOVEL possessed common-noun with a MEASURE value is modeled as an ENTITY the
possessor HAS, with the measure as a SCALAR on that entity:

    "my commute to work is 45 minutes"
      →  (user, has, commute)                      # commute is an ENTITY, never a rel_type
         (commute, duration, "45 minutes")         # measure hangs off the concept (object_datatype)

So NO rel-typed ``commute`` edge is ever produced → no orphan to ground, no rel_type↔entity collision.
The /ingest concept-object path queues the un-laddered ``commute`` for the async whatis is-a ladder on
its own. THE DISCRIMINATOR is metadata-driven (the rel_type overlay), NOT a noun list: fire ONLY when
the noun-as-rel is ABSENT from a readable overlay (truly novel) AND the value is a clean MEASURE; a
SEEDED rel (age/height/owns/…) stays EXACTLY as today (a direct user scalar).

These tests pin (1) the REAL ``derive_sentence_facts`` capture shape on the REAL sentences with a
populated (mocked) overlay — NOT a synthetic edge list — and (2) the reconciled harvest re-home planner
(``main._plan_pending_scalar_grounding``): it finds NOTHING to do for Option-A output (no double-
capture), still re-homes a flat orphan from ANY OTHER path (backstop), and never registers a rel-name
as an entity on a unit-miss. The async ladder-mint + the live queue INSERT are deploy-time / main-loop
live-validated.
"""
import datetime

import pytest

import src.extraction.linguistics as L
from src.api.main import (
    _plan_pending_scalar_grounding,
    _revert_pending_ground_collisions,
    _scalar_measure_parts,
)

_REF = datetime.date(2023, 6, 1)

# A populated per-tenant overlay stand-in: SEEDED rels are PRESENT (age is a scalar rel; owns/related_to/
# has are canonical structural rels); the NOVEL attribute nouns (commute / internet_plan / address) are
# deliberately ABSENT so the discriminator reads them as truly-novel.
_OVERLAY = {
    # scalar_datatype as seeded on every seat (migration 101: age -> integer); #52 reads it to keep
    # a bare count bare, so a stand-in without it files "32 years" (#79 — stale stand-in, not a defect).
    "age": {"category": "general", "tail_types": ["SCALAR"], "scalar_datatype": "integer"},
    "owns": {"category": "general", "tail_types": ["ANY"]},
    "related_to": {"category": "general", "tail_types": ["ANY"]},
    "has": {"category": "ontology", "tail_types": ["ANY"]},
    "address": {"category": "location", "tail_types": ["Location", "SCALAR"]},
}
# The grown unit_scalar cue-map (bootstrap floor): time units → duration; year → age.
_UNITS = {"minute": "duration", "hour": "duration", "second": "duration", "year": "age"}
# The seeded overlay the PLANNER (harvest backstop) sees — same shape, key→meta.
_SEEDED = {"related_to": {"category": "general"}, "age": {"category": "general"},
           "owns": {"category": "general"}, "has": {"category": "ontology"}}

_spine = pytest.mark.skipif(
    not L.linguistics_available(),
    reason="spaCy linguistic layer unavailable (SPACY_MODEL unset) — the deriver no-ops",
)


def _triples(edges):
    return [(e.get("subject"), e.get("rel_type"), (e.get("object") or "").strip().lower())
            for e in edges]


@pytest.fixture
def option_a_on(monkeypatch):
    """Turn Option A ON and pin the deriver's overlay + unit-map reads to the deterministic stand-ins
    (no live DB needed) — so ``derive_sentence_facts`` runs its REAL capture path against a KNOWN
    seed∪tenant overlay."""
    monkeypatch.setattr(L, "SPINE_PENDING_GROUNDING", True, raising=False)
    monkeypatch.setattr(L, "_rel_overlay_meta_map", lambda: dict(_OVERLAY))
    monkeypatch.setattr(L, "_unit_scalar_map", lambda: dict(_UNITS))
    yield


def _dt(sentence):
    return {(f.subject, f.rel_type, (f.object or "").strip().lower(),
             getattr(f, "scalar_datatype", None))
            for f in L.derive_sentence_facts(sentence, _REF)}


# ── _measure_value_parts / _scalar_measure_parts: the false-positive-bounding grammar probe ─────────
def test_measure_probe_accepts_clean_measures():
    for probe in (_scalar_measure_parts, L._measure_value_parts):
        assert probe("45 minutes") == ("45", "minutes")
        assert probe("500 Mbps") == ("500", "mbps")
        assert probe("8") == ("8", None)


def test_measure_probe_rejects_names_and_multiword():
    # a bare name, a name with a glued numeral, a multi-word phrase, and an ADDRESS are NOT measures
    for probe in (_scalar_measure_parts, L._measure_value_parts):
        assert probe("Nike") is None
        assert probe("Galaxy S22") is None            # digit glued to a letter head
        assert probe("Area 51") is None               # number is not leading
        assert probe("7 eleven convenience store") is None
        assert probe("123 Main Street") is None       # an address — never a measure
        assert probe("45 minutes each way") is None   # trailing adverbial → not a bare measure
        assert probe("") is None


# ── OPTION A — the primary needle-move, PROVEN on the REAL derive_sentence_facts capture path ───────
@_spine
def test_commute_captured_as_entity_plus_scalar_not_rel_typed(option_a_on):
    out = _dt("my commute to work is 45 minutes")

    # 1. the possessed noun is an ENTITY the possessor HAS — NOT a rel_type (THE HARD LINE)
    assert ("user", "has", "commute", None) in out
    # 2. NO rel-typed (user, commute, …) edge is EVER produced — nothing to ground, no collision
    assert not any(rel == "commute" for (_s, rel, _o, _dt_) in out)
    # 3. the measure hangs off the CONCEPT as a scalar (unit minute → duration), object_datatype set
    #    → /ingest routes the verbatim value into entity_attributes ON THE CONCEPT (not flat on user)
    assert ("commute", "duration", "45 minutes", "quantity") in out
    # 4. commute NEVER appears as a rel_type surface anywhere in the capture
    assert all(rel != "commute" for (_s, rel, _o, _dt_) in out)


@_spine
def test_internet_plan_unit_miss_still_entity_plus_generic_measure(option_a_on):
    # Mbps is not in the unit map → a GENERIC measure rel + the unit recorded for cue-map growth;
    # the value is STILL on the concept (never lost), commute-style entity+scalar.
    from src.api import linguistic_cue_overlay as lco
    lco.drain_cue_candidates()  # clear request-scoped buffer
    out = _dt("my internet plan is 500 Mbps")
    assert ("user", "has", "internet plan", None) in out
    assert ("internet plan", "related_measure", "500 mbps", "quantity") in out
    assert not any(rel == "internet_plan" for (_s, rel, _o, _dt_) in out)
    # the unseen unit is queued so the unit_scalar map grows (freq≥3), re-homing later occurrences
    assert ("mbps", lco.UNIT_SCALAR_CATEGORY) in lco.drain_cue_candidates()


# ── the SEEDED cases are UNTOUCHED — the discriminator must not mis-fire ────────────────────────────
@_spine
def test_seeded_age_scalar_is_a_direct_user_scalar(option_a_on):
    # "age" is a SEEDED scalar rel (present in the overlay) → stays EXACTLY as today (user scalar),
    # NEVER re-modeled as (user, has, age).
    out = _dt("my age is 32")
    assert ("user", "age", "32", "string") in out
    assert not any(rel == "has" for (_s, rel, _o, _dt_) in out)


@_spine
def test_first_person_years_old_is_age_untouched(option_a_on):
    # "I am 32 years old" is owned by the copula-measure chain (not attr-scalar) → (user, age, 32).
    out = _dt("I am 32 years old")
    assert ("user", "age", "32", None) in out
    assert not any(rel == "has" for (_s, rel, _o, _dt_) in out)


@_spine
def test_address_multiword_value_is_not_a_measure(option_a_on):
    # "123 Main Street" is a multi-word ADDRESS, not a clean measure → Option A does NOT fire; the
    # value stays a direct scalar (here "address" is also seeded, doubly safe).
    out = _dt("my address is 123 Main Street")
    assert not any(rel == "has" and obj != "123 main street" for (_s, rel, obj, _dt_) in out)
    assert not any(rel == "related_measure" for (_s, rel, _o, _dt_) in out)


@_spine
def test_serial_code_value_is_not_a_measure(option_a_on):
    # a serial CODE ("XR7-9920") is not a leading-number measure → Option A does NOT fire.
    out = _dt("the laptop's serial is XR7-9920")
    assert not any(rel == "has" for (_s, rel, _o, _dt_) in out)


# ── FAIL-SAFE — flag OFF and unreadable overlay → byte-identical to today (flat scalar, value kept) ─
@_spine
def test_flag_off_keeps_todays_flat_scalar(monkeypatch):
    monkeypatch.setattr(L, "SPINE_PENDING_GROUNDING", False, raising=False)
    monkeypatch.setattr(L, "_rel_overlay_meta_map", lambda: dict(_OVERLAY))
    out = _dt("my commute to work is 45 minutes")
    # today's behavior exactly: the flat possessed-attribute scalar, value NEVER dropped
    assert ("user", "commute", "45 minutes", "string") in out
    assert not any(rel == "has" for (_s, rel, _o, _dt_) in out)


@_spine
def test_empty_overlay_fails_safe_to_flat_scalar(monkeypatch):
    # an UNREADABLE / empty overlay (no DB) → cannot confirm novelty → Option A NO-OPs (flat scalar).
    monkeypatch.setattr(L, "SPINE_PENDING_GROUNDING", True, raising=False)
    monkeypatch.setattr(L, "_rel_overlay_meta_map", lambda: {})
    out = _dt("my commute to work is 45 minutes")
    assert ("user", "commute", "45 minutes", "string") in out
    assert not any(rel == "has" for (_s, rel, _o, _dt_) in out)


# ── HARVEST RE-HOME PLANNER — reconciled: no double-capture, backstop intact, collision-safe miss ───
def test_planner_finds_nothing_to_do_for_option_a_output():
    # the Option-A deriver output (has-link + quantity scalar on the concept) is ALREADY placed —
    # the planner MUST NOT re-home it again (no double-capture, no second concept mint).
    optA = [{"subject": "user", "rel_type": "has", "object": "commute",
             "fact_provenance": "user_stated"},
            {"subject": "commute", "rel_type": "duration", "object": "45 minutes",
             "object_datatype": "quantity", "fact_provenance": "user_stated"}]
    plan = _plan_pending_scalar_grounding(optA, _SEEDED, _UNITS)
    assert plan["changed"] is False
    assert plan["concepts"] == []
    assert _triples(plan["edges"]) == _triples(optA)


def test_planner_backstop_rehomes_a_flat_orphan_from_another_path():
    # a FLAT orphan reaching the harvest from any OTHER path (e.g. the LLM rewrite extractor) is still
    # re-homed by the backstop: drop the flat edge, link the subject to the concept, hang the value.
    flat = [{"subject": "user", "rel_type": "commute", "object": "45 minutes",
             "object_datatype": "string", "fact_provenance": "user_stated"}]
    plan = _plan_pending_scalar_grounding(flat, _SEEDED, _UNITS)
    out = _triples(plan["edges"])
    assert ("user", "commute", "45 minutes") not in out       # flat orphan GONE
    assert ("user", "related_to", "commute") in out           # walkable link (seeded rel)
    assert ("commute", "duration", "45 minutes") in out       # value on the concept
    assert "commute" in plan["concepts"]
    assert plan["changed"] is True


def test_planner_unit_miss_keeps_flat_but_never_registers_rel_as_entity():
    # unit MISS: KEEP the flat orphan (recall intact — value never dropped) and record the unit for
    # growth, but do NOT queue the concept — the surviving flat orphan still mints the rel, so queuing
    # that SAME surface as an entity would hit THE HARD LINE (the exact bug). Collision-safe.
    miss = [{"subject": "user", "rel_type": "internet_plan", "object": "500 mbps",
             "object_datatype": "string"}]
    plan = _plan_pending_scalar_grounding(miss, _SEEDED, _UNITS)
    out = _triples(plan["edges"])
    assert ("user", "internet_plan", "500 mbps") in out       # flat orphan STAYS (nothing dropped)
    assert plan["concepts"] == []                             # concept NOT queued (no rel-as-entity)
    assert "mbps" in plan["unit_misses"]                      # unit recorded for map growth
    assert plan["changed"] is True


def test_planner_leaves_seeded_and_atomic_and_naming_untouched():
    # a seeded rel (age), a structured atomic (ipv4), an already-placed quantity, and a non-numeric
    # naming value are all left EXACTLY alone.
    edges = [{"subject": "sarah", "rel_type": "age", "object": "28"},
             {"subject": "core-1", "rel_type": "ip_address", "object": "10.0.0.1",
              "object_datatype": "ipv4"},
             {"subject": "commute", "rel_type": "duration", "object": "45 minutes",
              "object_datatype": "quantity"},
             {"subject": "user", "rel_type": "owns", "object": "favorite running shoes"}]
    plan = _plan_pending_scalar_grounding(edges, _SEEDED, _UNITS)
    assert plan["changed"] is False
    assert plan["concepts"] == []
    assert _triples(plan["edges"]) == _triples(edges)


def test_structured_and_already_placed_datatype_sets():
    import src.api.main as m
    assert "string" not in m._STRUCTURED_OBJECT_DATATYPES          # generic → re-homed by backstop
    assert "ipv4" in m._STRUCTURED_OBJECT_DATATYPES                # structured atomic → skipped
    assert "mac" in m._STRUCTURED_OBJECT_DATATYPES
    assert "quantity" in m._ALREADY_PLACED_OBJECT_DATATYPES        # already placed on a concept → skip
    assert "quantity" not in m._STRUCTURED_OBJECT_DATATYPES        # kept distinct from atomics


def test_bootstrap_unit_map_carries_time_primitive():
    # the live path merges the bootstrap floor UNDER the tenant map so canonical measurement
    # primitives always resolve (observed live: a stale tenant unit_scalar map lacked 'minute').
    from src.api.linguistic_cue_overlay import _BOOTSTRAP_UNIT_SCALAR_MAP as UM
    assert UM.get("minute") == "duration"
    assert UM.get("hour") == "duration"


def test_flag_default_off_on_branch():
    # bisectable branch default: OFF on both seams unless explicitly enabled for the LME re-run
    import src.api.main as m
    assert m.SPINE_PENDING_GROUNDING is False
    assert L.SPINE_PENDING_GROUNDING is False


# ── HARD-LINE COLLISION GUARD — a PRE-EXISTING rel_type surface must NEVER lose the value ──────────
# Live regression (492 stale pending rels): a `commute` pending_placement rel_type minted by a failed
# pre-Option-A ingest persisted across seat wipes; every retry the entity registration for `commute`
# collided (a rel_type name cannot be an entity), BOTH re-homed edges were skipped, committed=0 — VALUE
# LOST. The guard: when the surface is (or is becoming) a rel_type, keep the LEGACY FLAT SCALAR.

def test_planner_leaves_pending_placement_surface_as_flat_scalar():
    # OLD planner: the surface is a PRE-EXISTING pending_placement rel_type → do NOT re-home (that would
    # register the rel-name as an entity → collide → lose the value). Keep the flat scalar.
    seeded = {"related_to": {"category": "general"},
              "commute": {"category": "pending_placement"}}   # ← the stale rel_type
    flat = [{"subject": "user", "rel_type": "commute", "object": "45 minutes",
             "object_datatype": "string"}]
    plan = _plan_pending_scalar_grounding(flat, seeded, _UNITS)
    assert plan["changed"] is False
    assert plan["concepts"] == []
    assert ("user", "commute", "45 minutes") in _triples(plan["edges"])   # value KEPT as flat scalar


def test_planner_live_rel_surface_guard_beats_stale_overlay():
    # the 5s overlay says ABSENT, but a LIVE rel_types check (existing_rel_surfaces) says the surface is
    # a rel_type (minted seconds ago / this turn) → still keep the flat scalar, never register as entity.
    seeded_absent = {"related_to": {"category": "general"}}
    flat = [{"subject": "user", "rel_type": "commute", "object": "45 minutes",
             "object_datatype": "string"}]
    plan = _plan_pending_scalar_grounding(flat, seeded_absent, _UNITS,
                                          existing_rel_surfaces={"commute"})
    assert plan["changed"] is False
    assert ("user", "commute", "45 minutes") in _triples(plan["edges"])


def test_revert_deriver_option_a_output_when_concept_is_live_rel_type():
    # DERIVER-output belt-and-suspenders: the overlay missed a recent mint so Option A emitted the
    # entity shape, but the concept surface IS a live rel_type → REVERT to the legacy flat scalar so
    # the value is kept and no rel_type surface is registered as an entity.
    optA = [{"subject": "user", "rel_type": "has", "object": "commute",
             "fact_provenance": "user_stated"},
            {"subject": "commute", "rel_type": "duration", "object": "45 minutes",
             "object_datatype": "quantity", "fact_provenance": "user_stated"}]
    reverted, n = _revert_pending_ground_collisions(optA, existing_rel_surfaces={"commute"})
    assert n == 1
    tr = [(e["subject"], e["rel_type"], (e["object"] or "").strip().lower(),
           e.get("object_datatype")) for e in reverted]
    assert ("user", "commute", "45 minutes", "string") in tr       # legacy flat scalar KEPT
    assert all(rel != "has" for (_s, rel, _o, _d) in tr)           # NO rel_type→entity attempt
    # provenance preserved through the revert (value carried, never dropped)
    assert all(e.get("fact_provenance") == "user_stated" for e in reverted)


def test_revert_noop_when_concept_is_fresh():
    # a genuinely-fresh concept surface (NOT a rel_type) → NO revert; Option A's entity shape stands.
    optA = [{"subject": "user", "rel_type": "has", "object": "commute"},
            {"subject": "commute", "rel_type": "duration", "object": "45 minutes",
             "object_datatype": "quantity"}]
    reverted, n = _revert_pending_ground_collisions(optA, existing_rel_surfaces=set())
    assert n == 0
    assert reverted == optA


def test_revert_multiword_concept_surface_underscored():
    # the concept display surface ("internet plan") maps to the rel_type key ("internet_plan"): the
    # underscored form is what the live rel_types set carries.
    optA = [{"subject": "user", "rel_type": "has", "object": "internet plan"},
            {"subject": "internet plan", "rel_type": "related_measure", "object": "500 mbps",
             "object_datatype": "quantity"}]
    reverted, n = _revert_pending_ground_collisions(optA, existing_rel_surfaces={"internet_plan"})
    assert n == 1
    tr = [(e["subject"], e["rel_type"], (e["object"] or "").strip().lower()) for e in reverted]
    assert ("user", "internet_plan", "500 mbps") in tr
    assert all(rel != "has" for (_s, rel, _o) in tr)


# ── DERIVER STATE B1 — the surface is ALREADY a rel_type IN THE OVERLAY → Option A skips (legacy) ───
@_spine
def test_deriver_skips_option_a_when_surface_already_rel_type(monkeypatch):
    monkeypatch.setattr(L, "SPINE_PENDING_GROUNDING", True, raising=False)
    # overlay carries a PRE-EXISTING `commute` rel_type (pending_placement) → NOT novel → skip Option A
    monkeypatch.setattr(L, "_rel_overlay_meta_map",
                        lambda: {"commute": {"category": "pending_placement"},
                                 "has": {"category": "ontology"}})
    monkeypatch.setattr(L, "_unit_scalar_map", lambda: dict(_UNITS))
    out = _dt("my commute to work is 45 minutes")
    # legacy flat scalar (value kept), NO (user, has, commute) → no rel_type→entity collision
    assert ("user", "commute", "45 minutes", "string") in out
    assert not any(rel == "has" for (_s, rel, _o, _dt_) in out)
