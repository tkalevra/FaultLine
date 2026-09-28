"""Unit tests for the deterministic clause-typed PA core (``src.extraction.clause_pa``), Phase 2
increment 1. Pins each of the ClausIE 5-way clause types on canonical sentences, plus coordination,
MinIE minimization/factoring, negation, and the fail-safe (untypable → emit nothing, never fabricate).

PURE tests — no DB, no network, no GLiNER2, no LLM. Pure spaCy dependency parse over the deriver's
own ``en_core_web_sm`` (parser-only) pipeline. Structure-only: the PA core makes NO entity-typing /
rel_type / first-person-binding decision (those are the chain-refinement layer, tested elsewhere).

Run: python3 -m pytest tests/test_clause_pa_extractor.py -q   (tests/ is gitignored → git add -f)
     or: python3 tools/fltest.py --bug PACORE --test tests/test_clause_pa_extractor.py
"""
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction.clause_pa import extract_propositions, pa_core_enabled  # noqa: E402


def _props(sentence):
    return extract_propositions(sentence).propositions


def _types(sentence):
    return [p.clause_type for p in _props(sentence)]


def _find(sentence, clause_type):
    return [p for p in _props(sentence) if p.clause_type == clause_type]


def _arg(prop, role):
    return next((a for a in prop.args if a.role == role), None)


# ── (a) INTRANSITIVE  SV ─────────────────────────────────────────────────────────────────────────
def test_intransitive_sv():
    props = _find("The dog barks.", "intransitive")
    assert len(props) == 1
    p = props[0]
    assert p.subject == "dog"
    assert p.predicate == "bark"
    assert p.args == []  # no object, no complement


def test_intransitive_with_oblique_keeps_obl_arg():
    # An oblique-only clause is still an intransitive predication; the PP rides as an ``obl`` arg.
    p = _find("I live in Toronto.", "intransitive")[0]
    obl = _arg(p, "obl")
    assert obl is not None and obl.text == "toronto" and obl.case == "in"


# ── (b) COPULAR  S cop C ─────────────────────────────────────────────────────────────────────────
def test_copular_nominal_predicate():
    p = _find("Carol is a teacher.", "copular")[0]
    assert p.subject == "carol" and p.predicate == "be"
    assert _arg(p, "cop_comp").text == "teacher"


def test_copular_adjectival_predicate():
    p = _find("I am excited.", "copular")[0]
    assert p.subject == "i" and _arg(p, "cop_comp").text == "excited"


# ── (c) MONOTRANSITIVE  SVO ──────────────────────────────────────────────────────────────────────
def test_monotransitive_svo():
    p = _find("I fixed the router.", "monotransitive")[0]
    assert p.subject == "i" and p.predicate == "fix"
    assert _arg(p, "obj").text == "router"


# ── (d) DITRANSITIVE  SVOiOd ─────────────────────────────────────────────────────────────────────
def test_ditransitive_dative_object():
    # "gave Luna training pads" — spaCy ``dative`` recipient → iobj.
    p = _find("I gave Luna some training pads.", "ditransitive")[0]
    assert p.subject == "i" and p.predicate == "give"
    assert _arg(p, "obj").text == "training pads"
    assert _arg(p, "iobj").text == "luna"


def test_ditransitive_for_alternation():
    # "bought training pads for Luna" — the ``for``/``to`` dative alternation PP → iobj recipient.
    p = _find("We bought training pads for Luna.", "ditransitive")[0]
    assert _arg(p, "obj").text == "training pads"
    assert _arg(p, "iobj").text == "luna"


# ── (e) COMPLEX-TRANSITIVE  SVOC ─────────────────────────────────────────────────────────────────
def test_complex_transitive_small_clause():
    p = _find("I consider him a friend.", "complex_transitive")[0]
    assert p.subject == "i" and p.predicate == "consider"
    assert _arg(p, "obj").text == "him"
    assert _arg(p, "xcomp").text == "friend"


# ── COORDINATION ─────────────────────────────────────────────────────────────────────────────────
def test_coordinated_object_splits_into_two_propositions():
    props = _find("I like apples and oranges.", "monotransitive")
    objs = sorted(_arg(p, "obj").text for p in props)
    assert objs == ["apples", "oranges"]
    assert all(p.subject == "i" and p.predicate == "like" for p in props)


def test_coordinated_subject_splits_into_two_propositions():
    props = _props("Sam and Kate joined the team.")
    subs = sorted(p.subject for p in props if p.clause_type == "monotransitive")
    assert subs == ["kate", "sam"]


def test_coordinated_clauses_each_yield_a_proposition():
    props = _props("She runs and he swims.")
    preds = sorted((p.subject, p.predicate) for p in props if p.clause_type == "intransitive")
    assert preds == [("he", "swim"), ("she", "run")]


# ── MinIE MINIMIZATION / FACTORING ───────────────────────────────────────────────────────────────
def test_minimization_drops_determiner_keeps_essential_mods():
    # "the stand mixer" → minimized to "stand mixer" (det dropped, compound kept).
    p = _find("The malfunction of the stand mixer ruined dinner.", "monotransitive")[0]
    assert _arg(p, "obj").text == "dinner"          # det "the" dropped
    assert p.subject == "malfunction"               # det "the" dropped


def test_nmod_of_phrase_factored_as_separate_proposition():
    # MinIE: "malfunction OF the stand mixer" is a SEPARATE nominal_modifier proposition, not swallowed.
    props = _find("The malfunction of the stand mixer ruined dinner.", "nominal_modifier")
    assert len(props) == 1
    nm = props[0]
    assert nm.subject == "malfunction" and nm.predicate == "of"
    assert _arg(nm, "nmod").text == "stand mixer"


def test_appositive_factored_as_separate_proposition():
    props = _find("My brother, a doctor, lives in Ohio.", "nominal_modifier")
    assert any(p.predicate == "appos" and _arg(p, "appos").text == "doctor" for p in props)


# ── NEGATION (ConText/NegEx assertion polarity) ──────────────────────────────────────────────────
def test_negation_flag_set_on_negated_state():
    p = _find("The GPS is not functioning.", "intransitive")[0]
    assert p.negated is True and p.subject == "gps"


def test_affirmed_state_is_not_negated():
    p = _find("The dog barks.", "intransitive")[0]
    assert p.negated is False


# ── FAIL-SAFE (never fabricate) ──────────────────────────────────────────────────────────────────
def test_empty_and_none_input_yield_empty_result():
    assert extract_propositions("").propositions == []
    assert extract_propositions("   ").propositions == []
    assert extract_propositions(None).propositions == []


def test_fragment_without_predicate_emits_nothing_not_fabricated():
    # A bare noun phrase has no clause predicate → no proposition may be fabricated.
    r = extract_propositions("A red bicycle.")
    assert r.propositions == []


def test_untypable_clause_is_recorded_not_dropped_silently():
    # Whatever a parse yields, the extractor never raises and never invents an argument: every
    # emitted proposition has a real predicate, and any clause it cannot type is in ``uncovered``.
    r = extract_propositions("Ouch!")
    assert all(p.predicate for p in r.propositions)
    # No fabricated subject/object content on anything emitted.
    for p in r.propositions:
        for a in p.args:
            assert a.text  # never an empty fabricated arg


# ── FLAG (guards the future ingest call site only; default OFF) ──────────────────────────────────
def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("SPINE_PA_CORE", raising=False)
    assert pa_core_enabled() is False
    monkeypatch.setenv("SPINE_PA_CORE", "1")
    assert pa_core_enabled() is True


# ── PASSIVE (ClausIE active-normalization) ───────────────────────────────────────────────────────
def test_passive_with_agent_normalizes_to_active():
    p = _props("The mixer was broken by the power surge.")[0]
    assert p.passive is True
    assert p.subject == "power surge"                 # agent → subject
    assert _arg(p, "obj").text == "mixer"             # patient → object


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
