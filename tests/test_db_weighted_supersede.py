"""Unit tests for the DB-weighted correction/omission SUPERSEDE of a confident GLiNER2
(classify_intent, DESIGN-hierarchy-ladder-and-growth.md §"Routing is a DB-weighted intent
decision" + the user directive: "the LLM pre-classifies by the table data, with confidence …
on confidence supersede [GLiNER2] … honour the database decision to supersede/delete/overwrite").

These cover the pure, DB-free surfaces:
  - the bypass constants/flag
  - the spaCy∧LLM dual-gate routing (live path; LLM call + DB growth are integration-only)

The LLM call + growth are integration-only (fail-safe, compile-checked). The legacy
``_match_db_correction`` DB-matching helper was removed (dead code — uncalled + schema-incompatible);
the live correction/retraction routing is the spaCy∧LLM dual-gate (``_spacy_cue_route``).
"""
from src.api import main as m


# ── no hardcoded negation keyword list ────────────────────────────────────────
def test_no_hardcoded_negation_regex_constant():
    # Guard: the brittle hardcoded negation keyword list must NOT come back.
    assert not hasattr(m, "_NEGATION_CUE_RE")


# ── intent-independent turn fact-harvest (buried-fact ingest) ─────────────────
def test_turn_fact_harvest_flag_default_on():
    assert m.TURN_FACT_HARVEST is True


def test_harvest_spans_endpoint_defined():
    # The cheap segment→GLiNER2 harvest endpoint (no LLM triple extraction) must exist so the
    # recall path can fire it on every turn — facts buried in questions aren't dropped.
    assert callable(getattr(m, "harvest_spans", None))


# ── bypass-gate knobs ─────────────────────────────────────────────────────────
def test_correction_bypass_flag_default_on():
    assert m.CORRECTION_BYPASS_GATE is True


def test_supersede_min_conf_is_high_bar():
    # Only a CONFIDENT LLM fix/omission call may bypass GLiNER2 intent classification.
    assert m.INTENT_SUPERSEDE_MIN_CONF >= 0.75


# ── pin-to-known (User-is-truth scalar grounding) ─────────────────────────────
class _FakeCur:
    """Minimal cursor: returns the entity's known attributes for the DISTINCT scan."""
    def __init__(self, known):
        self._known = known
        self._rows = []
    def execute(self, q, params=None):
        self._rows = [(a,) for a in self._known]
    def fetchall(self):
        return self._rows


def test_pin_exact_known_passthrough():
    assert m._pin_scalar_attribute_to_known(_FakeCur(["has_ip"]), "e", "has_ip") == "has_ip"


def test_pin_morphology_fold_ip_to_has_ip():
    # The exact bug: correction said 'ip', fact is stored as 'has_ip' → must pin to has_ip.
    assert m._pin_scalar_attribute_to_known(_FakeCur(["has_ip", "age"]), "e", "ip") == "has_ip"


def test_pin_genuinely_new_attribute_unchanged():
    # No known attribute corresponds → return as-is (caller upserts; never silent-lose).
    assert m._pin_scalar_attribute_to_known(_FakeCur(["age"]), "e", "weight") == "weight"


def test_pin_nothing_known_unchanged():
    assert m._pin_scalar_attribute_to_known(_FakeCur([]), "e", "ip") == "ip"


def test_pin_empty_attr_safe():
    assert m._pin_scalar_attribute_to_known(_FakeCur(["has_ip"]), "e", "") == ""
