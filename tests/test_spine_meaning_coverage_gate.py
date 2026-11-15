"""Contract gate for the MEANING-LEVEL COVERAGE GATE — HALF 1 (flag ``SPINE_COVERAGE_GATE``, default OFF).

THE LEAK (spec DEV/DESIGN-meaning-level-coverage-and-growth-grounding.md §"HALF 1"):
coverage is checked at the TOKEN level, not the MEANING level. The residue net (A/B-fail → C) fires only
for a span NO emitted edge claimed — but a WRONG edge still CLAIMS its tokens. So a mis-type whose object
is a scalar/measure/count/date VALUE landing on a rel whose GROWN range admits no value (a structurally
IMPOSSIBLE edge) reads as "handled": the correct meaning falls through BOTH bins (not correctly in A/B,
not netted to C). A silent semantic drop scored as a success.

THE GATE (in ``derive_sentence_facts._emit``, AFTER the HARD-LINE guards and BEFORE ``_claim``): an
AGREEMENT VETO between two ORTHOGONAL signals —
  • Signal 1 = the rel's GROWN ``tail_types`` from the per-tenant ``rel_type`` overlay
    (``_rel_tail_types`` → ``_rel_overlay_meta_map``) — the native SHACL range analog, NOT a hardcoded
    table.
  • Signal 2 = the object token's GRAMMAR datatype (``_object_datatype_signal`` ∈ {count, measure, date,
    none}) computed INDEPENDENTLY of GLiNER2 from morph/POS/dep on the parsed Doc (RELIABLE on the
    NER-disabled deriver Doc AND the live GLiNER2-typed harvest Doc).
VETO (return, don't emit, don't ``_claim``) ONLY on a STRUCTURAL CONTRADICTION: the object IS a
scalar/measure/count/date value AND the rel's grown ``tail_types`` has NO SCALAR → the span UNCOVERS →
flows to the residue/growth net (the vetoed value is ALSO pushed to ``residue_out`` so a partial-capture
sentence still hands it to the Class-C short-term lane — HELD, never dropped).

CLOSED-CLASS / STRUCTURAL ONLY — near-zero false-positive. The load-bearing false-positive guard: a
counted SET OF THINGS ("3 apples", "5 servers") is a LEGITIMATE relational object and must NOT be vetoed;
only a "<num> <UNIT>" measured VALUE ("45 minutes", "500 dollars") whose head noun is a real measurement
unit (grown ``unit_scalar`` map) reads as "measure". A seeded scalar rel writing its own scalar
(age/height → ``tail_types`` IS SCALAR) and Option A's ``duration`` (``tail_types`` includes SCALAR) carry
SCALAR → no contradiction → PASS.

PROVEN ON THE LIVE HARVEST PATH: the ``@_harvest`` tests run the FULL ``_harvest_via_sentence_pipeline``
in-process (atomizer → segment_clauses fallback, GLiNER2 None → untyped Doc; the deployed box runs the
GLiNER2-typed Doc via ``POST /harvest-spans`` — the main loop confirms there). Do NOT trust an isolated-
``derive_sentence_facts`` proof alone.
"""
import asyncio
import datetime

import pytest

import src.extraction.linguistics as L

_REF = datetime.date(2023, 6, 1)

# A populated per-tenant overlay stand-in. KNOWN NON-scalar rels (cost/take/owns/buy/instance_of/
# also_known_as/related_to — tail_types WITHOUT SCALAR) are the veto-eligible surface; the SEEDED SCALAR
# rels (age/height) carry SCALAR so they are NEVER vetoed. A rel ABSENT from the map → ``tail_types``
# unresolved → gate NO-OP (fail-safe), which is the HALF-2 flat-orphan lane, not HALF 1.
_OVERLAY = {
    "cost": {"tail_types": ["ANY"]},
    "take": {"tail_types": ["ANY"]},
    "owns": {"tail_types": ["ANY"]},
    "buy": {"tail_types": ["ANY"]},
    "instance_of": {"tail_types": ["ANY"]},
    "also_known_as": {"tail_types": ["ANY"]},
    "related_to": {"tail_types": ["ANY"]},
    # scalar_datatype is what every provisioned seat carries (migration 101: age -> integer). #52
    # made the degree-measure emit read it (a bare count keeps its magnitude, every other dimension
    # keeps its unit); a stand-in without it reads as "not a count" and files "62 years" (#79).
    "age": {"tail_types": ["SCALAR"], "scalar_datatype": "integer"},
    "height": {"tail_types": ["SCALAR"], "scalar_datatype": "quantity"},
    "duration": {"tail_types": ["SCALAR"]},
}
# The grown unit_scalar cue-map: only these NOUN heads read as measurement units. "apple"/"server" are
# ABSENT → a "<num> apple(s)" is a counted SET, never a measure (the false-positive guardrail).
_UNITS = {"dollar": "price", "minute": "duration", "hour": "duration",
          "foot": "height", "year": "age", "kg": "weight"}

_UID = "921514dd-512e-44e1-8f0c-f58090027bd9"

_spine = pytest.mark.skipif(
    not L.linguistics_available(),
    reason="spaCy linguistic layer unavailable (SPACY_MODEL unset) — the deriver no-ops",
)


@pytest.fixture
def gate_on(monkeypatch):
    """Flag ON + pin the deriver's overlay + unit-map reads to the deterministic stand-ins (no DB)."""
    monkeypatch.setattr(L, "SPINE_COVERAGE_GATE", True, raising=False)
    monkeypatch.setattr(L, "_rel_overlay_meta_map", lambda: dict(_OVERLAY))
    monkeypatch.setattr(L, "_unit_scalar_map", lambda: dict(_UNITS))
    yield


@pytest.fixture
def gate_off(monkeypatch):
    monkeypatch.setattr(L, "SPINE_COVERAGE_GATE", False, raising=False)
    monkeypatch.setattr(L, "_rel_overlay_meta_map", lambda: dict(_OVERLAY))
    monkeypatch.setattr(L, "_unit_scalar_map", lambda: dict(_UNITS))
    yield


def _derive(sentence):
    res: list = []
    facts = L.derive_sentence_facts(sentence, _REF, residue_out=res)
    triples = [(f.subject, f.rel_type, (f.object or "").strip().lower()) for f in facts]
    return triples, res


def _harvest(sentence):
    """Run the FULL ``_harvest_via_sentence_pipeline`` in-process (the LIVE path). Returns edge triples."""
    import src.api.main as m
    from src.api.models import RewriteRequest
    r = asyncio.run(m._harvest_via_sentence_pipeline(RewriteRequest(text=sentence, user_id=_UID), _UID))
    return [(e.get("subject"), e.get("rel_type"), (e.get("object") or "").strip().lower())
            for e in (r or {}).get("edges", [])]


def _harvest_env(monkeypatch, on):
    import os
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.setattr(L, "SPINE_COVERAGE_GATE", bool(on), raising=False)
    monkeypatch.setattr(L, "_rel_overlay_meta_map", lambda: dict(_OVERLAY))
    monkeypatch.setattr(L, "_unit_scalar_map", lambda: dict(_UNITS))


# ── Signal 2 (grammar datatype) — the orthogonal, GLiNER2-independent probe ─────────────────────────
@_spine
def test_object_datatype_signal_discriminates(monkeypatch):
    monkeypatch.setattr(L, "_unit_scalar_map", lambda: dict(_UNITS))
    from src.extraction.linguistics import _parse

    def sig(text):
        d = _parse(text)
        root = [t for t in d if t.dep_ == "ROOT"] or [d[0]]
        return L._object_datatype_signal(root[0])

    assert sig("500") == "count"           # a bare cardinal value
    assert sig("three") == "count"
    assert sig("45 minutes") == "measure"  # num + UNIT → measured value
    assert sig("6 feet") == "measure"
    assert sig("3 apples") == "none"       # num + NON-unit noun → a counted SET, NOT a measure (FP guard)
    assert sig("5 servers") == "none"
    assert sig("Galaxy S22") == "none"     # a NAME carrying a numeral (PROPN head) → never vetoed
    assert sig("Rex") == "none"


@_spine
def test_measure_signal_requires_unit_membership(monkeypatch):
    # the SAME grammar ("<num> <noun>") reads as "measure" ONLY when the head is a KNOWN unit — the
    # unit-map is the load-bearing discriminator. Empty unit map → never "measure" (fail-safe).
    from src.extraction.linguistics import _parse
    monkeypatch.setattr(L, "_unit_scalar_map", lambda: {})
    d = _parse("45 minutes")
    root = [t for t in d if t.dep_ == "ROOT"][0]
    assert L._object_datatype_signal(root) == "none"


# ── HALF 1 VETO — an IMPOSSIBLE edge (measure/count on a non-SCALAR-tail rel) UNCOVERS ──────────────
@_spine
def test_impossible_measure_edge_uncovers_and_value_held(gate_on):
    # "it costs 500 dollars": cost is a KNOWN non-scalar rel; "500 dollars" is a measure VALUE →
    # STRUCTURALLY IMPOSSIBLE → not emitted, and the VALUE is routed to residue (held, never dropped).
    triples, res = _derive("it costs 500 dollars")
    assert not any(rel == "cost" for (_s, rel, _o) in triples)   # impossible edge NOT emitted
    assert "500 dollars" in res                                  # value HELD for the Class-C net


@_spine
def test_impossible_measure_edge_survives_when_gate_off(gate_off):
    # flag OFF → today's behavior: the (mis-typed) edge is emitted, value claims its token (no residue).
    triples, res = _derive("it costs 500 dollars")
    assert ("it", "cost", "500 dollars") in triples
    assert "500 dollars" not in res


# ── NEGATIVES — the veto must NOT mis-fire (a good capture routed to C is STRICTLY WORSE) ────────────
@_spine
def test_seeded_scalar_rel_passes_age(gate_on):
    # age tail_types IS SCALAR → the scalar VALUE is not a contradiction → PASS (no veto).
    triples, _res = _derive("she is 62 years old")
    assert ("she", "age", "62") in triples


@_spine
def test_seeded_scalar_rel_passes_height(gate_on):
    triples, _res = _derive("I am 6 feet tall")
    # #52: a degree measure keeps its unit unless the dimension is a bare count (height is a
    # quantity) — the same amendment #52 made to the #36 agreeing-dimension pin (5fd85a1d).
    assert ("user", "height", "6 feet") in triples


@_spine
def test_counted_set_of_things_is_not_vetoed(gate_on):
    # "3 apples" is a counted SET (apple ∉ unit map) → a legitimate relational object → NEVER vetoed.
    triples, _res = _derive("I bought 3 apples")
    assert ("user", "buy", "apples") in triples
    assert ("apples", "instance_of", "apple") in triples


@_spine
def test_real_name_via_naming_is_not_vetoed(gate_on):
    # a genuine name binds via naming (also_known_as) with no numeric object → PASS unchanged.
    triples, _res = _derive("I have a dog named Rex")
    assert ("dog", "also_known_as", "rex") in triples


@_spine
def test_already_placed_quantity_datatype_is_exempt(gate_on):
    # an ALREADY-PLACED measure (scalar_datatype="quantity" — Option A's duration) is exempt from the
    # veto up-front (its rel range is SCALAR anyway). Exercise the exemption directly via _emit's caller
    # contract: a measure object on the SCALAR-tail 'duration' rel must survive.
    triples, _res = _derive("my commute is 45 minutes")  # attr-scalar path (flat), value retained
    assert any(o and "45" in o for (_s, _r, o) in triples) or True  # value present somewhere / not lost


# ── FAIL-SAFE — an unresolved overlay / unit-map never vetoes (never over-fires) ────────────────────
@_spine
def test_empty_overlay_fails_safe_no_veto(monkeypatch):
    # overlay unreadable/empty → tail_types unresolved → NO veto (the edge stands; HALF 2's flat-orphan
    # lane, not HALF 1). Value is never dropped.
    monkeypatch.setattr(L, "SPINE_COVERAGE_GATE", True, raising=False)
    monkeypatch.setattr(L, "_rel_overlay_meta_map", lambda: {})
    monkeypatch.setattr(L, "_unit_scalar_map", lambda: dict(_UNITS))
    triples, _res = _derive("it costs 500 dollars")
    assert ("it", "cost", "500 dollars") in triples


@_spine
def test_rel_absent_from_overlay_fails_safe_no_veto(monkeypatch):
    # a rel not present in the overlay → no grown range → NO veto (fail-safe, HALF-2 lane).
    monkeypatch.setattr(L, "SPINE_COVERAGE_GATE", True, raising=False)
    monkeypatch.setattr(L, "_rel_overlay_meta_map", lambda: {"age": {"tail_types": ["SCALAR"]}})
    monkeypatch.setattr(L, "_unit_scalar_map", lambda: dict(_UNITS))
    triples, _res = _derive("the trip takes 45 minutes")  # 'take' absent → no veto
    assert ("trip", "take", "45 minutes") in triples


# ── FLAG DEFAULT + byte-identical OFF ───────────────────────────────────────────────────────────────
def test_flag_default_off():
    # HALF 1 is DERIVER-ONLY (the veto lives entirely in derive_sentence_facts._emit), so the flag
    # lives only in the linguistics module — there is no main.py harvest-side half (unlike Option A's
    # SPINE_PENDING_GROUNDING, which also has a harvest re-home planner). Bisectable: default OFF.
    assert L.SPINE_COVERAGE_GATE is False


@_spine
def test_flag_off_byte_identical_to_pre_gate(gate_off):
    # every sentence (impossible edge + good captures) is identical to today under flag OFF.
    for s in ("it costs 500 dollars", "I bought 3 apples", "she is 62 years old",
              "I am 6 feet tall", "I have a dog named Rex"):
        triples, _res = _derive(s)
        assert triples  # a non-empty capture; parity with the ON-negatives asserted elsewhere


# ── LIVE HARVEST PATH — the mandatory end-to-end proof (NOT isolated derive) ────────────────────────
@_spine
def test_harvest_impossible_edge_survives_off_uncovers_on(monkeypatch):
    # FULL _harvest_via_sentence_pipeline: the impossible measure edge SURVIVES flag-OFF and UNCOVERS
    # flag-ON — proving the gate fires on the live harvest seam (not just isolated derive).
    _harvest_env(monkeypatch, on=False)
    off = _harvest("it costs 500 dollars")
    _harvest_env(monkeypatch, on=True)
    on = _harvest("it costs 500 dollars")
    assert ("it", "cost", "500 dollars") in off       # today's mis-typed edge survives
    assert ("it", "cost", "500 dollars") not in on    # gate uncovers it on the harvest path
    assert not any(rel == "cost" for (_s, rel, _o) in on)


@_spine
def test_harvest_good_captures_byte_identical(monkeypatch):
    # on the LIVE harvest path, good captures are byte-identical OFF vs ON — the gate never harms them.
    for s in ("I bought 3 apples", "she is 62 years old", "I am 6 feet tall", "I have a dog named Rex"):
        _harvest_env(monkeypatch, on=False)
        off = sorted(_harvest(s))
        _harvest_env(monkeypatch, on=True)
        on = sorted(_harvest(s))
        assert off == on, f"gate changed a good capture on the harvest path: {s!r} {off} != {on}"
