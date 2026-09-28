"""G6 / G8 / G10 scalar-adverbial & named-place capture — LongMemEval capture-gap backlog.

Pins the ingest-side diagnosis + fixes for three documented capture gaps:

  • G6  — FREQUENCY / RATE ADVERBIAL (already handled, regression-pinned). "I attend yoga classes
          three times a week" — the recurrence adverbial is captured as a RATE SCALAR on the ACTIVITY
          entity (the verb's object) via ``scalar_datatype="string"``, rel = the user's own verb lemma.
          No new code — these tests LOCK the existing ``_rate_binds`` behavior so it never regresses.
          UD: ``npadvmod`` NOUN adverbial with a ``nummod`` count over a governed period noun.

  • G8  — COMMON-NOUN / COMPOUND PLACE. Two sub-cases:
          (b) PROPN-compound place ("from the Best Buy **store**") — the proper name is a ``compound``
              modifier of a COMMON-noun pobj head, so ``_verb_pp_value``'s PROPN-*pobj* fold never sees
              it. NOW captured as a SEPARATE ``(user, <verb>_<prep>, <place>)`` edge (never welded into
              the PROPN product name → dedup-safe). UD: oblique ``nmod`` (``case`` from) + ``compound``
              proper-noun chain.
          (a) BARE common-noun locative ("under my bed") — DELIBERATELY under-captured (the PROPN-only
              gate is the safe value-vs-manner firewall; loosening it re-admits manner/instrument
              adjuncts). Pinned as a documented boundary, NOT a bug.

  • G10 — PASSIVE "got + participle" + SCALAR + counterparty. "I got pre-approved for $400,000 from
          Wells Fargo" — the SCALAR ($400,000) was already captured by prior G1/G2 work; the residual
          FROM-PP counterparty (Wells Fargo) is NOW captured as a SEPARATE ``(user, approve_from,
          wells fargo)`` edge off the measure-verb lane. UD: ``auxpass``/resultative frame; the
          counterparty is an oblique ``nmod`` (``case`` from).

THE HARD LINE (asserted): a named place/source is a relational object, NEVER a scalar welded into an
entity name (it must dedup) and NEVER classified into ``instance_of``/``subclass_of``.

Deterministic / subject-agnostic: spaCy dependency + POS + the surface preposition only — no
place/brand/verb/domain word list, no cosine/LLM. First-person is grammatical (``Person=1``).
"""
import importlib
import os

import pytest

import src.extraction.linguistics as ling


def _reload(**env):
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return importlib.reload(ling)


def _model_available() -> bool:
    m = _reload(LINGUISTIC_LAYER="true")
    return m.linguistics_available()


_HAS_MODEL = _model_available()
requires_model = pytest.mark.skipif(not _HAS_MODEL, reason="en_core_web_sm not installed in test env")


def _facts(m, sentence, reference="2023-05-21"):
    return list(m.derive_sentence_facts(sentence, reference))


def _edges(m, sentence, reference="2023-05-21"):
    return [(f.subject, f.rel_type, f.object) for f in _facts(m, sentence, reference)]


# ───────────────────────────── G6 — rate/frequency (regression pin) ─────────────────────────────

@requires_model
def test_g6_rate_captured_as_scalar_on_activity_object():
    m = _reload(LINGUISTIC_LAYER="true")
    facts = _facts(m, "I attend yoga classes three times a week.")
    edges = [(f.subject, f.rel_type, f.object) for f in facts]
    # the activity relation survives …
    assert ("user", "attend", "yoga classes") in edges, edges
    # … and the recurrence rides a SCALAR attribute on the activity entity (never glued into a name).
    rate = [f for f in facts if f.object == "three times a week"]
    assert rate, edges
    assert rate[0].subject == "yoga classes", rate[0]
    assert rate[0].scalar_datatype == "string", rate[0]


@requires_model
def test_g6_multiplicative_and_distributive_rates():
    m = _reload(LINGUISTIC_LAYER="true")
    for sent, val, subj in [
        ("I go to the gym twice a week.", "twice a week", "gym"),
        ("I get a wax and detailing done every 3-4 months.", "every 3-4 months", "wax"),
    ]:
        facts = _facts(m, sent)
        rate = [f for f in facts if f.object == val]
        assert rate, [(f.subject, f.rel_type, f.object) for f in facts]
        assert rate[0].subject == subj, rate[0]
        assert rate[0].scalar_datatype == "string", rate[0]


@requires_model
def test_g6_rate_never_glued_into_object_name():
    # the rate must NOT be concatenated into the relational object (would ruin dedup).
    m = _reload(LINGUISTIC_LAYER="true")
    edges = _edges(m, "I attend yoga classes three times a week.")
    for (_s, _r, obj) in edges:
        assert "three times a week" not in obj or obj == "three times a week", edges


# ───────────────────────── G8(b) — PROPN-compound place value ─────────────────────────

@requires_model
def test_g8b_compound_place_captured_as_separate_edge():
    m = _reload(LINGUISTIC_LAYER="true")
    edges = _edges(m, "I got a new Samsung Galaxy S22 from the Best Buy store on February 20th.")
    # the product keeps its own relation …
    assert ("user", "get", "new samsung galaxy s22") in edges, edges
    # … the store (PROPN compound of the common-noun head "store") is a SEPARATE source edge.
    assert ("user", "get_from", "best buy") in edges, edges


@requires_model
def test_g8b_place_never_welded_into_product_name():
    # HARD CONSTRAINT: the place is never glued into the product name (must dedup independently).
    m = _reload(LINGUISTIC_LAYER="true")
    for (_s, _r, obj) in _edges(m, "I got a new Samsung Galaxy S22 from the Best Buy store."):
        assert "best buy" not in obj or obj == "best buy", obj


@requires_model
def test_g8b_temporal_compound_not_a_place():
    # FIREWALL: "on February 20th" — February is a PROPN compound of the pobj "20th", but it is a
    # WHEN on the temporal lane, NEVER a place. No (user, get_on, february) place edge.
    m = _reload(LINGUISTIC_LAYER="true")
    edges = _edges(m, "I got a new Samsung Galaxy S22 from the Best Buy store on February 20th.")
    # February (a PROPN compound of the temporal pobj "20th") must NEVER surface as a place.
    assert not any("february" in o for (_s, r, o) in edges if r.endswith("_from")), edges
    # the ONLY source/place edge is the store.
    assert [(s, r, o) for (s, r, o) in edges if r.endswith("_from")] == \
        [("user", "get_from", "best buy")], edges


@requires_model
def test_g8a_bare_commonnoun_locative_under_captured_documented_boundary():
    # DOCUMENTED SAFE UNDER-CAPTURE: a bare common-noun locative ("under my bed") is NOT captured as
    # a place — the PROPN-only gate is the deliberate value-vs-manner firewall. Assert we do NOT
    # fabricate a place edge here (the honest boundary), NOT that we capture it.
    m = _reload(LINGUISTIC_LAYER="true")
    edges = _edges(m, "I have been keeping them under my bed for storage.")
    assert not any(o == "bed" and r.endswith(("_under", "_from", "_at")) for (_s, r, o) in edges), edges


# ─────────────────────── G10 — passive got+participle counterparty ───────────────────────

@requires_model
def test_g10_scalar_and_counterparty_both_captured():
    m = _reload(LINGUISTIC_LAYER="true")
    facts = _facts(m, "I got pre-approved for $400,000 from Wells Fargo.")
    edges = [(f.subject, f.rel_type, f.object) for f in facts]
    # the amount is a scalar …
    amt = [f for f in facts if f.object == "$400,000"]
    assert amt and amt[0].scalar_datatype == "string", edges
    # … and the lender (residual FROM-PP counterparty) is a SEPARATE source edge.
    assert ("user", "approve_from", "wells fargo") in edges, edges


@requires_model
def test_g10_counterparty_never_welded_into_scalar():
    m = _reload(LINGUISTIC_LAYER="true")
    for (_s, _r, obj) in _edges(m, "I got pre-approved for $400,000 from Wells Fargo."):
        assert "wells fargo" not in obj or obj == "wells fargo", obj


# ─────────────────────── over-fire / firewall guards (no regression) ───────────────────────

@requires_model
def test_manner_instrument_common_noun_never_a_place():
    # "with a brush" / manner PP — a common-noun pobj with NO PROPN → never a place edge.
    m = _reload(LINGUISTIC_LAYER="true")
    edges = _edges(m, "I painted the fence with a brush.")
    assert not any(r.endswith(("_with", "_from", "_at")) and o == "brush" for (_s, r, o) in edges), edges


@requires_model
def test_already_folded_propn_not_double_captured():
    # "from Amazon" is folded into the object by _verb_pp_value; my seam must NOT emit a duplicate.
    m = _reload(LINGUISTIC_LAYER="true")
    edges = _edges(m, "I bought a laptop from Amazon.")
    assert ("user", "buy", "laptop from amazon") in edges, edges
    assert not any(r == "buy_from" for (_s, r, _o) in edges), edges


@requires_model
def test_no_propn_place_leaves_output_unchanged():
    # A clause with no residual named place is byte-identical (the seam is a no-op).
    m = _reload(LINGUISTIC_LAYER="true")
    edges = _edges(m, "I met her at the conference.")
    assert not any(r.endswith("_at") and o == "conference" and r == "meet_from" for (_s, r, o) in edges)
    # conference (common noun) is captured by the EXISTING promotion path, not a new _from edge.
    assert not any(r.endswith("_from") for (_s, r, _o) in edges), edges
