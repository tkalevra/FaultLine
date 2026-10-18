"""FOSS-005 es pin: a sentential-negation statement never reaches the retraction lane.

On es 761cf914, remember_facts("No uso el puerto 9004 en ningún servidor.") classified
RETRACTION (GLiNER2-native zero-shot on Spanish text — every upstream cue guard is
English/lexical and the neg-scope ensemble only engages STATEMENT/QUERY leans), the
/retract/correct lane failed honestly ("old fact not found") and STILL minted the
affirmative edge (user, usar, puerto) — recall then asserted "You use Puerto".

The es classification side fixed here: a destructive GLiNER2 lean (gate-PASSED or
SUB-GATE escalation-decided) whose ONLY negation is SENTENTIAL (verbal predicate — not
the nominal/two-pole structural denial the ensemble/CORRECTION lane own) and which
carries NO explicit removal/repair cue is vetoed to STATEMENT with polarity preserved —
en's classification outcome for the same shape. The veto is hoisted ABOVE gate/escalation
routing because the LIVE es stack scored the bug sentence SUB-GATE (CORRECTION@0.52-0.63
< gate 0.70) and the INTENT_ADJUDICATION escalation routed it "exactly as if GLiNER2
emitted it confidently" — a gate-passed-only veto never saw that turn. The decision logic is PURE (`_sentential_negation_blocks_destructive` in
src/api/main.py — no parsing), so the decision pins run without a model; the endpoint
pins drive the REAL /classify-intent with a fake GLiNER2 model and hand-built grammar
facts (simulating the es-md parse). The failed-lane atomicity guard (a failed
/retract/correct must not write) is owned by another lane — this file pins the
CLASSIFICATION side only: the sentence must never ENTER the retraction lane.
"""
from __future__ import annotations

import asyncio
import types

import pytest

import src.api.main as main
import src.extraction.linguistics as ling_mod
from src.api.main import _sentential_negation_blocks_destructive
from src.extraction.linguistics import (
    DirectiveAnalysis,
    NegationScope,
    analyze_directive,
    analyze_negation_scopes,
    linguistics_available,
)


def _es_model_ready() -> bool:
    """True only when a SHIPPED ES model is the loaded spaCy layer (SPACY_MODEL=
    es_core_news_md — the FOSS-002 measured default — or es_core_news_sm)."""
    try:
        return bool(linguistics_available()) and (ling_mod._SPACY_MODEL or "").startswith("es_core")
    except Exception:  # noqa: BLE001
        return False


# WORK-ORDER (round 3, item 2): the REAL-parse es pins run ONLY under the shipped es
# model — the same skip-if-unavailable convention as every other model pin. The
# hand-built-doc pins further down carry the same proofs on the MEASURED sm parse
# shape, so the suite is green (and red-checkable) in a no-es-model env too.
_es_real = pytest.mark.skipif(
    not _es_model_ready(),
    reason="needs SPACY_MODEL=es_core_news_md/sm loaded (no es model downloadable here)",
)

# ── fixtures: the grammar facts the es-md parse yields for the bug sentence ───

_BUG_SENTENCE = "No uso el puerto 9004 en ningún servidor."
_EN_TWIN = "I do not use port 9004 on any server."


def _sentential_scope(text=_BUG_SENTENCE):
    """NegationScope over a VERBAL predicate — "no" → "uso": sentential negation."""
    return NegationScope(
        cue_text="No",
        head_text="uso",
        predicate_text="uso",
        scope_text=text,
        predicates_nominal=False,
        two_referential_poles=False,
        clause_terminated=False,
    )


def _plain_statement_directive():
    """DirectiveAnalysis for "No uso ...": a 1sg verb root, no removal/repair cue."""
    return DirectiveAnalysis(
        imperative_root_lemma=None,
        imperative_root_negated=False,
        root_verb_lemma="usar",
        root_subject_is_self=False,
        clause_initial_markers=frozenset(),
        has_contrastive_negation=False,
        cessation_advmod_lemmas=frozenset(),
        cessative_root_lemma=None,
        cessative_complement_shape=None,
    )


def _structural_denial_scope():
    """NegationScope over a NOMINAL complement with two referential poles —
    "mi gata no es Luna": the ensemble/CORRECTION lane's shape, never vetoed."""
    return NegationScope(
        cue_text="no",
        head_text="es",
        predicate_text="es",
        scope_text="mi gata no es Luna",
        predicates_nominal=True,
        two_referential_poles=True,
        clause_terminated=False,
    )


# ── the pure decision (no model needed) ─────────────────────────────────────────


def test_bug_sentence_shape_vetoes_the_destructive_lean():
    """"No uso el puerto 9004 ..." — sentential negation, no removal cue → veto."""
    assert _sentential_negation_blocks_destructive(
        [_sentential_scope()], _plain_statement_directive()) is True


def test_structural_denial_shape_is_never_vetoed():
    """A nominal/two-pole negation is the ensemble's CORRECTION shape — keep the lane."""
    assert _sentential_negation_blocks_destructive(
        [_structural_denial_scope()], _plain_statement_directive()) is False


def test_no_negation_scope_never_vetoes():
    """An affirmative turn ("Uso el puerto 9004") has no scope → nothing to veto."""
    assert _sentential_negation_blocks_destructive(
        [], _plain_statement_directive()) is False
    assert _sentential_negation_blocks_destructive(
        None, _plain_statement_directive()) is False


def test_missing_directive_fails_safe_to_no_veto():
    """Layer unavailable → no grammar facts → today's route stands (never veto blind)."""
    assert _sentential_negation_blocks_destructive(
        [_sentential_scope()], None) is False


def test_imperative_delete_cue_is_never_vetoed():
    """An imperative delete-verb cue ("forget/delete/erase") routes upstream."""
    d = DirectiveAnalysis(
        imperative_root_lemma="forget",
        imperative_root_negated=False,
        root_verb_lemma="forget",
        root_subject_is_self=False,
        clause_initial_markers=frozenset(),
        has_contrastive_negation=False,
        cessation_advmod_lemmas=frozenset(),
        cessative_root_lemma=None,
        cessative_complement_shape=None,
    )
    assert _sentential_negation_blocks_destructive(
        [_sentential_scope()], d) is False


def test_cessation_advmod_cue_is_never_vetoed():
    """"Rex is no longer my pet"-shaped cessation — a genuine removal shape."""
    d = DirectiveAnalysis(
        imperative_root_lemma=None,
        imperative_root_negated=False,
        root_verb_lemma="have",
        root_subject_is_self=True,
        clause_initial_markers=frozenset(),
        has_contrastive_negation=False,
        cessation_advmod_lemmas=frozenset({"long"}),
        cessative_root_lemma=None,
        cessative_complement_shape=None,
    )
    assert _sentential_negation_blocks_destructive(
        [_sentential_scope()], d) is False


def test_contrastive_negation_is_never_vetoed():
    """"mi gata tiene 3 años, no 4"-shaped contrast IS a correction — keep the lane."""
    d = DirectiveAnalysis(
        imperative_root_lemma=None,
        imperative_root_negated=False,
        root_verb_lemma="tener",
        root_subject_is_self=False,
        clause_initial_markers=frozenset(),
        has_contrastive_negation=True,
        cessation_advmod_lemmas=frozenset(),
        cessative_root_lemma=None,
        cessative_complement_shape=None,
    )
    assert _sentential_negation_blocks_destructive(
        [_sentential_scope()], d) is False


def test_repair_marker_and_meant_cues_are_never_vetoed():
    """A repair marker ("actually ...") or a 1st-person "I meant" routes upstream."""
    d_marker = DirectiveAnalysis(
        imperative_root_lemma=None,
        imperative_root_negated=False,
        root_verb_lemma="tener",
        root_subject_is_self=False,
        clause_initial_markers=frozenset({"actually"}),
        has_contrastive_negation=False,
        cessation_advmod_lemmas=frozenset(),
        cessative_root_lemma=None,
        cessative_complement_shape=None,
    )
    assert _sentential_negation_blocks_destructive(
        [_sentential_scope()], d_marker) is False
    d_meant = DirectiveAnalysis(
        imperative_root_lemma=None,
        imperative_root_negated=False,
        root_verb_lemma="mean",
        root_subject_is_self=True,
        clause_initial_markers=frozenset(),
        has_contrastive_negation=False,
        cessation_advmod_lemmas=frozenset(),
        cessative_root_lemma=None,
        cessative_complement_shape=None,
    )
    assert _sentential_negation_blocks_destructive(
        [_sentential_scope()], d_meant) is False

# ── the en twin under a REAL parse (skips without a loadable model) ────────────

_ling = pytest.mark.skipif(
    not linguistics_available(),
    reason="spaCy linguistic layer unavailable (SPACY_MODEL unset) — no real parse",
)

# FOSS-012 critic round 2: the EN-twin real-parse pins need an EN model — under an
# es-model process parsing English they go RED, not skipped. Skip-with-reason.
_en_real = pytest.mark.skipif(
    not (linguistics_available() and (ling_mod._SPACY_MODEL or "").startswith("en_core")),
    reason=("needs an en_core model loaded — an es-model process parsing English "
            "does not carry the Penn negation arcs this pin reads (skip-with-reason)"),
)


@_en_real
def test_en_twin_sentence_real_parse_carries_the_sentential_shape():
    """en scores "I do not use port 9004 on any server." STATEMENT — the target outcome.
    The REAL parser must agree the shape is sentential (no nominal-pole scope)."""
    scopes = analyze_negation_scopes(_EN_TWIN)
    d = analyze_directive(_EN_TWIN)
    assert scopes, f"no negation scope parsed from the en twin: {scopes}"
    assert _sentential_negation_blocks_destructive(scopes, d) is True


# ── the REAL /classify-intent endpoint with a fake GLiNER2 model ──────────────

_RETRACTION_DESC = "deleting or forgetting information"
_STATEMENT_DESC = "stating a fact, preference, or habit"
_CORRECTION_DESC = "replacing a previously stated value with a different one"


async def _async_noop(*a, **k):
    return None


def _fake_gliner(label_desc, confidence):
    return types.SimpleNamespace(
        classify_text=lambda text, schema, include_confidence=False: {
            "intent": {"label": label_desc, "confidence": confidence},
        }
    )


@pytest.fixture()
def _routed(monkeypatch):
    """classify_intent with every DB/queue dependency stubbed to the bug-scene facts."""
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.setattr(main, "_ensure_tenant_ready", _async_noop)
    monkeypatch.setattr(main, "_spacy_cue_route", lambda text, user_id=None: (None, None))
    # The es-md parse facts are delivered by the REAL seams the veto consults.
    from src.extraction import linguistics as ling
    monkeypatch.setattr(ling, "linguistics_available", lambda: True)
    monkeypatch.setattr(ling, "is_interrogative_clause", lambda text: False)
    return monkeypatch


def _classify(text, label_desc, confidence, scopes, directive, monkeypatch):
    from src.extraction import linguistics as ling
    monkeypatch.setattr(ling, "analyze_negation_scopes", lambda t: scopes)
    monkeypatch.setattr(ling, "analyze_directive", lambda t: directive)
    return asyncio.run(main.classify_intent(
        {"text": text}, user_id="test-user-0001", model=_fake_gliner(label_desc, confidence)))


def test_es_bug_sentence_is_routed_statement_not_retraction(_routed, monkeypatch):
    """THE bug: GLiNER2 RETRACTION@0.93 on the negated sentence must come back STATEMENT.
    Red on 761cf914: the endpoint returned RETRACTION and the MCP drove /retract/correct."""
    resp = _classify(_BUG_SENTENCE, _RETRACTION_DESC, 0.93,
                     [_sentential_scope()], _plain_statement_directive(), monkeypatch)
    assert resp == {"intent": "STATEMENT", "confidence": 0.95}, (
        f"the negated statement was routed to the retraction lane: {resp}")
    assert main._INTENT_LAYER_STATS.get("test-use", {}).get("negated_statement_veto", 0) >= 1


def test_es_bug_sentence_is_routed_statement_over_correction_too(_routed, monkeypatch):
    """The same shape must not slip through on a CORRECTION lean either."""
    resp = _classify(_BUG_SENTENCE, _CORRECTION_DESC, 0.9,
                     [_sentential_scope()], _plain_statement_directive(), monkeypatch)
    assert resp["intent"] == "STATEMENT", f"routed destructive on a negated statement: {resp}"


def test_affirmative_control_still_reaches_ingest_as_statement(_routed, monkeypatch):
    """Work-order pin (iii): "Uso el puerto 9004" (no negation) is unaffected."""
    resp = _classify("Uso el puerto 9004.", _STATEMENT_DESC, 0.9,
                     [], _plain_statement_directive(), monkeypatch)
    assert resp == {"intent": "STATEMENT", "confidence": 0.9}


def test_structural_denial_lean_still_reaches_the_correction_lane(_routed, monkeypatch):
    """A nominal/two-pole denial keeps today's route — the veto only owns sentential."""
    resp = _classify("Mi gata no es Luna.", _RETRACTION_DESC, 0.9,
                     [_structural_denial_scope()], _plain_statement_directive(), monkeypatch)
    assert resp["intent"] == "RETRACTION", resp


def test_veto_flag_off_restores_the_legacy_route(_routed, monkeypatch):
    """SENTENTIAL_NEGATION_VETO=false is the documented escape hatch → legacy RETRACTION."""
    monkeypatch.setattr(main, "SENTENTIAL_NEGATION_VETO", False)
    resp = _classify(_BUG_SENTENCE, _RETRACTION_DESC, 0.93,
                     [_sentential_scope()], _plain_statement_directive(), monkeypatch)
    assert resp == {"intent": "RETRACTION", "confidence": 0.93}

# ── RE-CRITIC DEFECT 2: the veto must bind the SUB-GATE escalation route too ────


def _escalation_recorder(monkeypatch, route="CORRECTION"):
    """Replace the INTENT_ADJUDICATION LLM with a recorder that answers like the live
    es stack did (escalation_resolved adjudicated_route=CORRECTION). LLMModels.get
    fails LOUD (raise) when no model env var is set — and that raise happens while
    EVALUATING the call arguments, before the LLM function is ever consulted — so the
    pin must resolve the model name first or the escalation fail-opens before the
    recorder can observe anything."""
    import src.api.llm_calls as llm_calls
    monkeypatch.setenv("LLM_MODEL_INTENT_ADJUDICATION", "test-adjudicator")
    calls = []

    async def _recorder(**kwargs):
        calls.append(kwargs)
        return {"route": route}

    monkeypatch.setattr(llm_calls, "call_llm_with_retry_async", _recorder)
    return calls


@pytest.mark.parametrize("label_desc,conf", [
    (_CORRECTION_DESC, 0.63),   # the live-measured band: CORRECTION@0.524-0.630
    (_RETRACTION_DESC, 0.60),   # sub-gate destructive lean, other label
])
def test_subgate_bug_sentence_is_vetoed_statement_before_escalation(
        _routed, monkeypatch, label_desc, conf):
    """THE live contradiction: sub-gate (conf < 0.70) + sentential-only negation must
    come back STATEMENT WITHOUT reaching the INTENT_ADJUDICATION escalation — a veto
    wired only inside the gate-passed branch can never see this path."""
    calls = _escalation_recorder(monkeypatch)
    resp = _classify(_BUG_SENTENCE, label_desc, conf,
                     [_sentential_scope()], _plain_statement_directive(), monkeypatch)
    assert resp == {"intent": "STATEMENT", "confidence": 0.95}, (
        f"the sub-gate negated statement was routed destructive via escalation: {resp}"
    )
    assert calls == [], (
        f"the veto must bind BEFORE the escalation LLM is paid for: {len(calls)} call(s)"
    )
    assert main._INTENT_LAYER_STATS.get("test-use", {}).get("negated_statement_veto", 0) >= 1
    assert main._INTENT_LAYER_STATS.get("test-use", {}).get("llm_adjudication") is None


def test_subgate_structural_denial_still_escalates_to_the_correction_lane(
        _routed, monkeypatch):
    """The hoist must not cannibalize the escalation lane: a sub-gate destructive lean
    whose negation is the NOMINAL/two-pole denial shape (not the veto's) still routes
    through INTENT_ADJUDICATION exactly as before."""
    calls = _escalation_recorder(monkeypatch)
    resp = _classify("Mi gata no es Luna.", _CORRECTION_DESC, 0.63,
                     [_structural_denial_scope()], _plain_statement_directive(), monkeypatch)
    assert resp["intent"] == "CORRECTION", (
        f"the escalation lane changed for non-veto shapes: {resp}"
    )
    assert len(calls) == 1, f"escalation must still be consulted exactly once: {calls}"
    assert main._INTENT_LAYER_STATS.get("test-use", {}).get("llm_adjudication", 0) >= 1


def test_subgate_affirmative_correction_lean_declines_foss012(_routed, monkeypatch):
    """⚠️ REVERSED BY FOSS-012 (was: test_subgate_affirmative_correction_lean_still_escalates,
    which froze the escalation owning this band "until FOSS-012 lands"). The evidence-free
    first-person present-tense affirmative shape now DEMOTES to STATEMENT below the
    escalation — the shape facts arrive from the analyzer stub, the negation screens are
    empty (no scope), and the INTENT_ADJUDICATION LLM is never paid for. A sub-gate
    NON-affirmative destructive lean still escalates (pinned above)."""
    from src.extraction.linguistics import PersonTenseShape
    _shape = PersonTenseShape(
        root_verb_lemma="usar", root_is_finite_verb=True, root_person_first=True,
        root_tense_present=True, subject_is_first_person=False)
    from src.extraction import linguistics as ling
    monkeypatch.setattr(ling, "analyze_first_person_present_statement", lambda t: _shape)
    calls = _escalation_recorder(monkeypatch)
    resp = _classify("Uso el puerto 9004.", _CORRECTION_DESC, 0.63,
                     [], _plain_statement_directive(), monkeypatch)
    assert resp == {"intent": "STATEMENT", "confidence": 0.95}, resp
    assert calls == [], f"the FOSS-012 decline binds BEFORE the escalation: {calls}"
    assert main._INTENT_LAYER_STATS.get("test-use", {}).get(
        "affirmative_statement_decline", 0) >= 1




# ── ROUND 3 (re-critic): the grammar layer itself — REAL es parses, no fake scopes ──
# The round-2 reds could never catch the live-inert veto: every pin faked the scopes
# (unit) or the GLiNER2 model (endpoint), and the only real-parse pin was the EN twin.
# Measured live on es_core_news_sm (critic, image dev-ref): analyze_negation_scopes
# ("No uso el puerto 9004 en ningun servidor.") returned scopes=[] because the cue-
# acceptance was Penn-neg-only while the shipped es models attach the negator as
# ADV/advmod DIRECTLY on the verb. The UD sentential-negator arm in
# analyze_negation_scopes closes that; these pins bind it.

_BUG_SURFACES = [
    "No uso el puerto 9004 en ningun servidor.",   # the unaccented live surface
    "No uso el puerto 9004 en ningún servidor.",   # the accented work-order surface
]


def _hand_es_bug_doc():
    """The MEASURED es_core_news_sm UD parse of the bug sentence (round-3 critic), hand-
    built on a blank es vocab (the test_es_inc1 technique — no model file needed):
    no/ADV/advmod→uso, uso/VERB/ROOT, puerto/obj, servidor/obl. On sm this exact shape
    yielded scopes=[] pre-fix; analyze_directive on the same parse also reads
    imperative_root_lemma='uso' (negated), clause_initial_markers={'no'} and
    cessation_advmod={'no'} — NONE of which is a removal/repair cue (the veto decision
    intersects cessation/marker lemmas with the bounded ENGLISH cue sets), so the veto
    must fire on it. md (the FOSS-002 default) parses the same ADV/advmod-on-verb shape."""
    import spacy
    from spacy.tokens import Doc
    vocab = spacy.blank("es").vocab
    words = ["No", "uso", "el", "puerto", "9004", "en", "ningun", "servidor", "."]
    pos = ["ADV", "VERB", "DET", "NOUN", "NUM", "ADP", "DET", "NOUN", "PUNCT"]
    deps = ["advmod", "ROOT", "det", "obj", "nummod", "case", "det", "obl", "punct"]
    heads = [1, 1, 3, 1, 3, 7, 7, 1, 1]
    doc = Doc(vocab, words=words, pos=pos)
    doc[0].is_sent_start = True
    for i, (d, h) in enumerate(zip(deps, heads)):
        doc[i].dep_ = d
        doc[i].head = doc[h]
    return doc


@pytest.fixture()
def _transport_only(monkeypatch):
    """Transport/queue stubs ONLY — the grammar layer runs FOR REAL (real parse, or the
    hand-built measured parse via the _nlp seam). No grammar fact is faked."""
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.setattr(main, "_ensure_tenant_ready", _async_noop)
    # TIER1/TIER2 cue preroute: an English cue-lemma preroute cannot fire on this Spanish
    # sentence (the closed sets hold no es negators); stubbing avoids a redundant parse.
    monkeypatch.setattr(main, "_spacy_cue_route", lambda text, user_id=None: (None, None))
    return monkeypatch


# ---- (a) REAL-PARSE scope pins: under SPACY_MODEL=es_core_news_md (FOSS-002 default) ----


@_es_real
@pytest.mark.parametrize("surface", _BUG_SURFACES)
def test_es_real_parse_yields_a_sentential_negation_scope(surface):
    """THE round-3 defect, pinned at the grammar layer: analyze_negation_scopes on the bug
    sentence must be NON-empty and mark VERBAL SENTENTIAL scope (no nominal complement,
    no two referential poles — the veto's exact precondition), with the negator "no" as
    the cue and the object inside the scope (verb-phrase extent). scopes=[] here means
    the veto is live-inert again."""
    scopes = analyze_negation_scopes(surface)
    assert scopes, (
        "the es grammar layer emitted NO negation scope for " + repr(surface)
        + " — the veto is live-inert (round-3 defect)")
    s = scopes[0]
    assert (s.cue_text or "").lower() == "no", "cue: " + repr(s.cue_text)
    assert s.predicates_nominal is False and s.two_referential_poles is False, (
        "not the sentential shape: " + repr(s))
    assert "puerto" in (s.scope_text or "").lower(), (
        "object outside the scope: " + repr(s.scope_text))


@_es_real
def test_es_real_parse_subgate_bug_sentence_routes_statement_via_veto(
        _transport_only, monkeypatch):
    """WORK-ORDER PIN (round 3, item 2b), END-TO-END on the REAL es parse: real
    classify_intent (no grammar monkeypatch), fake GLiNER2 at the measured sub-gate
    CORRECTION@0.63 → STATEMENT@0.95 through the veto, the INTENT_ADJUDICATION LLM never
    consulted, /retract/correct never reached. Runs on md (the FOSS-002 default); the
    documented sm delta (mis-read imperative_root_lemma on a 1sg finite,
    cessation_advmod={'no'}, clause_initial_markers={'no'}) is decision-neutral: none
    intersects a removal/repair cue set."""
    calls = _escalation_recorder(monkeypatch)
    resp = asyncio.run(main.classify_intent(
        {"text": _BUG_SENTENCE}, user_id="test-user-0001",
        model=_fake_gliner(_CORRECTION_DESC, 0.63)))
    assert resp == {"intent": "STATEMENT", "confidence": 0.95}, (
        "the real-parse bug sentence was routed destructive: " + repr(resp))
    assert calls == [], "escalation must not be consulted: " + str(len(calls)) + " call(s)"
    assert main._INTENT_LAYER_STATS.get("test-use", {}).get("negated_statement_veto", 0) >= 1


@_es_real
def test_es_real_parse_affirmative_control_routes_statement_via_decline(
        _transport_only, monkeypatch):
    """⚠️ REVERSED BY FOSS-012 (was: test_es_real_parse_affirmative_control_still_escalates,
    round 3, item 2c — which froze the escalation owning this band on the REAL parse).
    On the real es parse the affirmative control is the evidence-free first-person
    present-tense shape: the FOSS-012 decline demotes it to STATEMENT before the gate,
    and the escalation is never consulted. The NEGATED twin keeps the veto's lane
    (pinned above); the GENUINE correction keeps its own (FOSS-012 file)."""
    calls = _escalation_recorder(monkeypatch)
    resp = asyncio.run(main.classify_intent(
        {"text": "Uso el puerto 9004 en el servidor principal."}, user_id="test-user-0001",
        model=_fake_gliner(_CORRECTION_DESC, 0.63)))
    assert resp == {"intent": "STATEMENT", "confidence": 0.95}, (
        "the affirmative control changed route: " + repr(resp))
    assert calls == [], "the FOSS-012 decline binds BEFORE the escalation: " + repr(calls)


# ---- the same proofs on the MEASURED sm parse shape, hand-built (green/red-checkable
# ---- in a no-es-model env; the REAL analyzer + REAL decision + REAL endpoint run) ----


def test_hand_es_measured_parse_yields_a_sentential_negation_scope(monkeypatch):
    """The measured sm UD shape through the REAL analyzer: exactly one scope, cue "no",
    predicate "uso", full verb-phrase extent, SENTENTIAL (never the structural-denial
    shape). Red pre-fix: scopes=[]."""
    monkeypatch.setattr(ling_mod, "_SPACY_MODEL", "es_core_news_sm")
    monkeypatch.setattr(ling_mod, "_nlp", lambda text: _hand_es_bug_doc())
    scopes = analyze_negation_scopes(_BUG_SENTENCE)
    assert scopes and len(scopes) == 1, "scopes=" + repr(scopes)
    s = scopes[0]
    assert (s.cue_text or "").lower() == "no" and s.predicate_text.lower() == "uso", repr(s)
    assert s.predicates_nominal is False and s.two_referential_poles is False, repr(s)
    assert "puerto" in (s.scope_text or "").lower(), (
        "object outside the scope: " + repr(s.scope_text))


def test_hand_es_measured_parse_veto_decision_is_true(monkeypatch):
    """The veto DECISION on the measured parse's REAL grammar facts: scopes + directive
    both from the real analyzers (the sm artifacts — imperative_root_lemma on the 1sg
    finite, cessation_advmod={'no'}, clause_initial_markers={'no'} — must NOT block: none
    intersects a removal/repair cue set)."""
    monkeypatch.setattr(ling_mod, "_SPACY_MODEL", "es_core_news_sm")
    monkeypatch.setattr(ling_mod, "_nlp", lambda text: _hand_es_bug_doc())
    scopes = analyze_negation_scopes(_BUG_SENTENCE)
    directive = analyze_directive(_BUG_SENTENCE)
    assert _sentential_negation_blocks_destructive(scopes, directive) is True


def test_hand_es_measured_parse_subgate_routes_statement_via_veto(
        _transport_only, monkeypatch):
    """END-TO-END on the measured parse shape: sub-gate CORRECTION@0.63 → STATEMENT@0.95
    via the veto, escalation never consulted. Red pre-fix: the sentence fell through the
    empty-scope fail-safe into the sub-gate escalation (the live contradiction)."""
    monkeypatch.setattr(ling_mod, "_SPACY_MODEL", "es_core_news_sm")
    monkeypatch.setattr(ling_mod, "_nlp", lambda text: _hand_es_bug_doc())
    calls = _escalation_recorder(monkeypatch)
    resp = asyncio.run(main.classify_intent(
        {"text": _BUG_SENTENCE}, user_id="test-user-0001",
        model=_fake_gliner(_CORRECTION_DESC, 0.63)))
    assert resp == {"intent": "STATEMENT", "confidence": 0.95}, (
        "the measured-parse bug sentence was routed destructive: " + repr(resp))
    assert calls == [], "escalation must not be consulted: " + str(len(calls)) + " call(s)"


def test_en_twin_real_parse_scope_is_byte_identical_across_the_ud_arm():
    """The UD arm must not move the EN twin: "not" carries the Penn neg arc (fires in the
    unchanged arm); no token of the twin parse satisfies the UD arm (advmod/obl + closed-
    class negator lemma + verb governor), so the twin's scope facts are the pre-arm facts.
    Skips without a model AND under an es-model process — the twin's no-model
    coverage lives in the decision pins."""
    if not (linguistics_available() and (ling_mod._SPACY_MODEL or "").startswith("en_core")):
        pytest.skip("needs an en_core model — en twin covered by the no-model pins")
    scopes = analyze_negation_scopes(_EN_TWIN)
    assert scopes, "the en twin lost its Penn-neg scope"
    assert all(s.cue_text.lower() == "not" for s in scopes), repr(scopes)
    assert all(not (s.predicates_nominal or s.two_referential_poles) for s in scopes)



