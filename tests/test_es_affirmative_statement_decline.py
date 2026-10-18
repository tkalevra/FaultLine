"""FOSS-012 es pin: a plain first-person present-tense AFFIRMATIVE statement never
enters the correction lane.

On the es release head (and base-identical on the engine branch), remember_facts("Uso el
puerto 9004 en el servidor principal.") classified CORRECTION@0.845 — GLiNER2-native,
gate-PASSED (0.845 > 0.70), layer=gliner2_native — so the MCP drove /retract/correct,
the failed-correction lane answered "Incomplete correction — relationship type not
identified" (isError=true) and captured NOTHING in-turn. Re-measured end-to-end live on
the es eval stack @ dev-ref (2026-10-18, seat see-dev-bug-docs): the turn answers as a FAILED
correction, is retained verbatim in episodic_log (source=mcp, intent=CORRECTION) with
ZERO queryable rows (facts=0, staged_facts=0), and recall of the port is empty for at
least the 10-minute measured window — the only recovery path is the hourly
reextract_episodic re-mine (gated created_at < now() - INTERVAL '1 hour').

The classification side fixed here: a gate-passed CORRECTION lean whose turn is the
FIRST-PERSON PRESENT-TENSE AFFIRMATIVE DECLARATIVE shape (Person=1 by verbal pro-drop
agreement or explicit subject + Tense=Pres, non-interrogative) and which carries ZERO
correction-shape evidence — no removal/repair cue, no contrastive old-vs-new negation,
no repair frame, no tenant-grown correction-cue literal, no cessative-verb removal,
and NO negation scope at all (the FOSS-005 veto owns the negated twin) — is DEMOTED to
STATEMENT, exactly en's classification outcome for the same shape. The decision is
PURE (`_affirmative_statement_declines_correction` in src/api/main.py — no parsing, no
DB), so the decision pins run without a model; the analyzer pins run the REAL grammar
layer on the MEASURED es parse shape (hand-built Doc — the test_es_inc1 technique),
and the endpoint pins drive the REAL /classify-intent with a fake GLiNER2 model. The
decline sits in classify_intent ABOVE gate + escalation routing (the veto's hoist
discipline) so both routes see it, and it is bound to CORRECTION only — a RETRACTION
lean and every marked/contrastive/negated/removal shape keep today's route.

Red-check: at dev-ref this file fails at import (the decision, the analyzer, the
flag and the layer name do not exist).
"""
from __future__ import annotations

import asyncio
import types

import pytest

import src.api.main as main
import src.extraction.linguistics as ling_mod
from src.api.main import _affirmative_statement_declines_correction
from src.extraction.linguistics import (
    DirectiveAnalysis,
    PersonTenseShape,
    analyze_directive,
    analyze_first_person_present_statement,
    analyze_negation_scopes,
    linguistics_available,
)


def _es_model_ready() -> bool:
    """True only when a SHIPPED ES model is the loaded spaCy layer."""
    try:
        return bool(linguistics_available()) and (ling_mod._SPACY_MODEL or "").startswith("es_core")
    except Exception:  # noqa: BLE001
        return False


_es_real = pytest.mark.skipif(
    not _es_model_ready(),
    reason="needs SPACY_MODEL=es_core_news_md/sm loaded (no es model downloadable here)",
)

_ling = pytest.mark.skipif(
    not linguistics_available(),
    reason="spaCy linguistic layer unavailable (SPACY_MODEL unset) — no real parse",
)

_BUG_SENTENCE = "Uso el puerto 9004 en el servidor principal."
_EN_TWIN = "I use port 9004 on the main server."
_ES_CORRECTION = "En realidad la ip es 10.0.0.5."


# ── grammar facts ───────────────────────────────────────────────────────────────


def _person1_pres_shape(lemma="usar"):
    """The es pro-drop shape: Person=1 + Tense=Pres on the VERB itself, no subject."""
    return PersonTenseShape(
        root_verb_lemma=lemma,
        root_is_finite_verb=True,
        root_person_first=True,
        root_tense_present=True,
        subject_is_first_person=False,
    )


def _en_shape():
    """The en twin shape: explicit 1st-person subject; English verbs carry no Person."""
    return PersonTenseShape(
        root_verb_lemma="use",
        root_is_finite_verb=True,
        root_person_first=False,
        root_tense_present=True,
        subject_is_first_person=True,
    )


def _third_person_copula_shape():
    """En realidad LA IP ES X — the correction's predicate agrees 3rd person."""
    return PersonTenseShape(
        root_verb_lemma="ser",
        root_is_finite_verb=True,
        root_person_first=False,
        root_tense_present=True,
        subject_is_first_person=False,
    )


def _clean_directive(lemma="usar"):
    """DirectiveAnalysis with NO correction-shape evidence at all."""
    return DirectiveAnalysis(
        imperative_root_lemma=None,
        imperative_root_negated=False,
        root_verb_lemma=lemma,
        root_subject_is_self=False,
        clause_initial_markers=frozenset(),
        has_contrastive_negation=False,
        cessation_advmod_lemmas=frozenset(),
        cessative_root_lemma=None,
        cessative_complement_shape=None,
    )


# ── the pure decision (no model, no DB) ────────────────────────────────────────


def test_bug_sentence_facts_decline_the_correction_lean():
    """THE fix: zero evidence + Person=1 Present declarative → the CORRECTION lean
    demotes to STATEMENT. Red at dev-ref (the decision does not exist)."""
    assert _affirmative_statement_declines_correction(
        _clean_directive(), _person1_pres_shape(), False, False, False) is True


def test_en_twin_shape_declines_too():
    """Parity: the same shape read on the en twin (explicit "I") demotes as well —
    the guard is morphological, never per-language. (en classifies this STATEMENT
    natively; the decline is the same safety net on both.)"""
    assert _affirmative_statement_declines_correction(
        _clean_directive("use"), _en_shape(), False, False, False) is True


def test_third_person_correction_never_declines():
    """THE genuine-correction pin: "En realidad la ip es 10.0.0.5." — the predicate
    agrees 3rd person; there is no first-person assertion to demote. The correction
    lane keeps the turn."""
    assert _affirmative_statement_declines_correction(
        _clean_directive("ser"), _third_person_copula_shape(), False, False, False) is False


def test_past_tense_affirmative_never_declines():
    """Tense=Pres is load-bearing: a past-tense assertion is not the shape."""
    shape = PersonTenseShape(
        root_verb_lemma="usar", root_is_finite_verb=True, root_person_first=True,
        root_tense_present=False, subject_is_first_person=False)
    assert _affirmative_statement_declines_correction(
        _clean_directive(), shape, False, False, False) is False


def test_missing_grammar_facts_fail_safe():
    """Layer unavailable / parse miss → today's route stands (never demote blind)."""
    assert _affirmative_statement_declines_correction(
        None, _person1_pres_shape(), False, False, False) is False
    assert _affirmative_statement_declines_correction(
        _clean_directive(), None, False, False, False) is False


def test_imperative_removal_cue_blocks_the_decline():
    d = DirectiveAnalysis(
        imperative_root_lemma="forget", imperative_root_negated=False,
        root_verb_lemma="forget", root_subject_is_self=False,
        clause_initial_markers=frozenset(), has_contrastive_negation=False,
        cessation_advmod_lemmas=frozenset(), cessative_root_lemma=None,
        cessative_complement_shape=None)
    assert _affirmative_statement_declines_correction(
        d, _person1_pres_shape("forget"), False, False, False) is False


def test_cessation_advmod_cue_blocks_the_decline():
    d = DirectiveAnalysis(
        imperative_root_lemma=None, imperative_root_negated=False,
        root_verb_lemma="tener", root_subject_is_self=True,
        clause_initial_markers=frozenset(), has_contrastive_negation=False,
        cessation_advmod_lemmas=frozenset({"long"}), cessative_root_lemma=None,
        cessative_complement_shape=None)
    assert _affirmative_statement_declines_correction(
        d, _person1_pres_shape("tener"), False, False, False) is False


def test_contrastive_old_vs_new_blocks_the_decline():
    """The anzi-shape / "14, no 12" old-vs-new contrast IS a correction."""
    d = DirectiveAnalysis(
        imperative_root_lemma=None, imperative_root_negated=False,
        root_verb_lemma="tener", root_subject_is_self=False,
        clause_initial_markers=frozenset(), has_contrastive_negation=True,
        cessation_advmod_lemmas=frozenset(), cessative_root_lemma=None,
        cessative_complement_shape=None)
    assert _affirmative_statement_declines_correction(
        d, _person1_pres_shape("tener"), False, False, False) is False


def test_repair_marker_blocks_the_decline():
    d = DirectiveAnalysis(
        imperative_root_lemma=None, imperative_root_negated=False,
        root_verb_lemma="usar", root_subject_is_self=False,
        clause_initial_markers=frozenset({"actually"}), has_contrastive_negation=False,
        cessation_advmod_lemmas=frozenset(), cessative_root_lemma=None,
        cessative_complement_shape=None)
    assert _affirmative_statement_declines_correction(
        d, _person1_pres_shape(), False, False, False) is False


def test_meant_repair_verb_blocks_the_decline():
    d = DirectiveAnalysis(
        imperative_root_lemma=None, imperative_root_negated=False,
        root_verb_lemma="mean", root_subject_is_self=True,
        clause_initial_markers=frozenset(), has_contrastive_negation=False,
        cessation_advmod_lemmas=frozenset(), cessative_root_lemma=None,
        cessative_complement_shape=None)
    assert _affirmative_statement_declines_correction(
        d, _person1_pres_shape("mean"), False, False, False) is False


def test_repair_frame_blocks_the_decline():
    """"Correction: uso el puerto 9004" — the frame, not the clause, decides."""
    assert _affirmative_statement_declines_correction(
        _clean_directive(), _person1_pres_shape(), False, True, False) is False


def test_grown_correction_cue_blocks_the_decline():
    """DB-grown evidence beats the shape prior: a tenant correction_signals literal
    matching the turn keeps the destructive lane."""
    assert _affirmative_statement_declines_correction(
        _clean_directive(), _person1_pres_shape(), True, False, False) is False


def test_admitted_cessative_removal_blocks_the_decline():
    """"Dejo de usar el puerto 9004" — a lexically-expressed removal admitted by the
    grown cessative_verb cue class is a RETRACTION shape, never demoted."""
    assert _affirmative_statement_declines_correction(
        _clean_directive(), _person1_pres_shape(), False, False, True) is False


# ── the grammar analyzer on the MEASURED es parse shape (hand-built Doc) ───────


def _hand_es_affirmative_doc():
    """The measured es_core_news_md/sm UD parse of the bug sentence, hand-built on a
    blank es vocab (the test_es_inc1 technique — no model file needed): uso/VERB/ROOT
    with Mood=Ind|Number=Sing|Person=1|Tense=Pres|VerbForm=Fin (the pro-drop agreement
    the es chains read), puerto/obj, servidor/obl. No subject token — Spanish drops it."""
    import spacy
    from spacy.tokens import Doc
    vocab = spacy.blank("es").vocab
    words = ["Uso", "el", "puerto", "9004", "en", "el", "servidor", "principal", "."]
    pos = ["VERB", "DET", "NOUN", "NUM", "ADP", "DET", "NOUN", "ADJ", "PUNCT"]
    deps = ["ROOT", "det", "obj", "nummod", "case", "det", "obl", "amod", "punct"]
    heads = [1, 3, 1, 3, 8, 8, 1, 8, 1]
    doc = Doc(vocab, words=words, pos=pos)
    doc[0].is_sent_start = True
    for i, (d, h) in enumerate(zip(deps, heads)):
        doc[i].dep_ = d
        doc[i].head = doc[h]
    doc[0].set_morph("Mood=Ind|Number=Sing|Person=1|Tense=Pres|VerbForm=Fin")
    return doc


def _hand_es_third_person_doc():
    """The measured UD shape of "En realidad la ip es 10.0.0.5.": es/AUX/ROOT agreeing
    3rd person, ip/nsubj, 10.0.0.5 the copula's complement, realidad an obl adjunct."""
    import spacy
    from spacy.tokens import Doc
    vocab = spacy.blank("es").vocab
    words = ["En", "realidad", "la", "ip", "es", "10.0.0.5", "."]
    pos = ["ADP", "NOUN", "DET", "NOUN", "AUX", "NUM", "PUNCT"]
    deps = ["case", "obl", "det", "nsubj", "ROOT", "attr", "punct"]
    heads = [2, 5, 4, 5, 5, 5, 5]
    doc = Doc(vocab, words=words, pos=pos)
    doc[0].is_sent_start = True
    for i, (d, h) in enumerate(zip(deps, heads)):
        doc[i].dep_ = d
        doc[i].head = doc[h]
    doc[4].set_morph("Mood=Ind|Number=Sing|Person=3|Tense=Pres|VerbForm=Fin")
    return doc


def test_hand_es_parse_reads_the_first_person_present_shape(monkeypatch):
    """The REAL analyzer on the MEASURED es parse: pro-drop Person=1 + Tense=Pres, no
    subject token. This is the shape the decline demotes on."""
    monkeypatch.setattr(ling_mod, "_SPACY_MODEL", "es_core_news_sm")
    monkeypatch.setattr(ling_mod, "_nlp", lambda text: _hand_es_affirmative_doc())
    shape = analyze_first_person_present_statement(_BUG_SENTENCE)
    assert shape is not None
    assert shape.root_person_first is True, repr(shape)
    assert shape.root_tense_present is True, repr(shape)
    assert shape.subject_is_first_person is False, repr(shape)  # pro-drop: no subject


def test_hand_es_parse_third_person_yields_no_first_person(monkeypatch):
    """En realidad la ip ES X — the copula agrees 3rd person; the shape reader reports
    person_first=False and no 1st-person subject. The person screen keeps the lane."""
    monkeypatch.setattr(ling_mod, "_SPACY_MODEL", "es_core_news_sm")
    monkeypatch.setattr(ling_mod, "_nlp", lambda text: _hand_es_third_person_doc())
    shape = analyze_first_person_present_statement(_ES_CORRECTION)
    assert shape is not None
    assert shape.root_person_first is False, repr(shape)
    assert shape.subject_is_first_person is False, repr(shape)


def test_hand_es_parse_full_decline_decision_is_true(monkeypatch):
    """The REAL decision on the REAL analyzers over the measured parse: directive +
    shape both from the grammar layer (the sm artifact — analyze_directive reads the
    subjectless 1sg finite as an imperative root, lemma 'uso' — is decision-neutral:
    'uso' is in no removal/repair cue set), all DB screens empty → decline fires."""
    monkeypatch.setattr(ling_mod, "_SPACY_MODEL", "es_core_news_sm")
    monkeypatch.setattr(ling_mod, "_nlp", lambda text: _hand_es_affirmative_doc())
    d = analyze_directive(_BUG_SENTENCE)
    shape = analyze_first_person_present_statement(_BUG_SENTENCE)
    assert _affirmative_statement_declines_correction(
        d, shape, False, False, False) is True


def test_hand_es_parse_nonverbal_frame_yields_none(monkeypatch):
    """A sentence-initial nominal frame ("Correction: ...") has no finite predicate as
    ROOT and no cop child → the analyzer returns None → fail-safe, no decline."""
    import spacy
    from spacy.tokens import Doc
    vocab = spacy.blank("es").vocab
    words = ["Correction", "uso", "el", "puerto", "."]
    pos = ["NOUN", "VERB", "DET", "NOUN", "PUNCT"]
    deps = ["ROOT", "acl", "det", "obj", "punct"]
    heads = [1, 1, 4, 2, 1]
    doc = Doc(vocab, words=words, pos=pos)
    doc[0].is_sent_start = True
    for i, (d, h) in enumerate(zip(deps, heads)):
        doc[i].dep_ = d
        doc[i].head = doc[h]
    monkeypatch.setattr(ling_mod, "_SPACY_MODEL", "es_core_news_sm")
    monkeypatch.setattr(ling_mod, "_nlp", lambda text: doc)
    assert analyze_first_person_present_statement("Correction: uso el puerto.") is None


_en_real = pytest.mark.skipif(
    not (linguistics_available() and (ling_mod._SPACY_MODEL or "").startswith("en_core")),
    reason=("needs an en_core model loaded — an es-model process parsing English "
            "does not carry the Person=1 PronType=Prs subject morphology this pin "
            "reads (skip-with-reason, never red)"))


@_en_real
def test_en_twin_real_parse_carries_the_subject_shape():
    """The en twin through the REAL parser: explicit "I" subject (Person=1 PronType=Prs)
    + Tense=Pres root. Skips without a model AND under an es-model process — the
    twin's no-model coverage is the hand-built _en_shape decision pin."""
    shape = analyze_first_person_present_statement(_EN_TWIN)
    assert shape is not None
    assert shape.subject_is_first_person is True, repr(shape)
    assert shape.root_tense_present is True, repr(shape)


@_es_real
def test_es_real_parse_reads_the_pro_drop_shape():
    """THE live proof (runs where an es model ships): the real es parse of the bug
    sentence reads Person=1 + Tense=Pres on the verb with NO subject token."""
    shape = analyze_first_person_present_statement(_BUG_SENTENCE)
    assert shape is not None, "the es parse yielded no finite-predicate shape"
    assert shape.root_person_first is True, repr(shape)
    assert shape.root_tense_present is True, repr(shape)


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
    # The es-md parse facts are delivered by the REAL seams the decline consults.
    from src.extraction import linguistics as ling
    monkeypatch.setattr(ling, "linguistics_available", lambda: True)
    monkeypatch.setattr(ling, "is_interrogative_clause", lambda text: False)
    return monkeypatch


def _classify(text, label_desc, confidence, scopes, directive, monkeypatch,
              shape=None, grown_hit=False, repair_frame=False, cessative=False):
    from src.extraction import linguistics as ling
    monkeypatch.setattr(ling, "analyze_negation_scopes", lambda t: scopes)
    monkeypatch.setattr(ling, "analyze_directive", lambda t: directive)
    # UNCONDITIONAL: every decision pin pins the shape the decision sees — including
    # shape=None. Conditional stubbing made test_no_shape_facts_* vacuous wherever a
    # real es model loads (the REAL analyzer found a shape and the decline fired in
    # the "no shape" pin). The real-parse pins below call classify_intent directly.
    monkeypatch.setattr(ling, "analyze_first_person_present_statement", lambda t: shape)
    monkeypatch.setattr(main, "_turn_matches_grown_correction_cue",
                        lambda text, user_id=None: grown_hit)
    monkeypatch.setattr(main, "_repair_frame", lambda text, user_id=None: repair_frame)
    monkeypatch.setattr(main, "_cessative_shape_admitted", lambda d: cessative)
    # dedicated uid: _INTENT_LAYER_STATS is process-global per-uid — never let this
    # file's counters collide with test_es_negated_statement_intent's ("test-use").
    return asyncio.run(main.classify_intent(
        {"text": text}, user_id="foss012-user-01", model=_fake_gliner(label_desc, confidence)))


def test_gatepassed_bug_sentence_declines_to_statement(_routed, monkeypatch):
    """THE bug, end-to-end at the classification seam: GLiNER2 CORRECTION@0.845
    (gate-passed, the live-measured score) on the affirmative statement comes back
    STATEMENT@0.95 — ingest gets the fact, /retract/correct never sees it. Red at
    dev-ref: the endpoint returned CORRECTION."""
    resp = _classify(_BUG_SENTENCE, _CORRECTION_DESC, 0.845,
                     [], _clean_directive(), monkeypatch, shape=_person1_pres_shape())
    assert resp == {"intent": "STATEMENT", "confidence": 0.95}, (
        "the affirmative statement was routed to the correction lane: " + repr(resp))
    assert main._INTENT_LAYER_STATS.get("foss012-", {}).get(
        "affirmative_statement_decline", 0) >= 1


def test_subgate_bug_sentence_declines_before_escalation(_routed, monkeypatch):
    """The decline binds the SUB-GATE route too (the veto's hoist discipline): a
    sub-gate CORRECTION lean on the evidence-free affirmative shape never reaches the
    INTENT_ADJUDICATION escalation. (FOSS-012 reverses the contract
    test_es_negated_statement_intent froze around this shape.)"""
    import src.api.llm_calls as llm_calls
    monkeypatch.setenv("LLM_MODEL_INTENT_ADJUDICATION", "test-adjudicator")
    calls = []

    async def _recorder(**kwargs):
        calls.append(kwargs)
        return {"route": "CORRECTION"}

    monkeypatch.setattr(llm_calls, "call_llm_with_retry_async", _recorder)
    resp = _classify(_BUG_SENTENCE, _CORRECTION_DESC, 0.63,
                     [], _clean_directive(), monkeypatch, shape=_person1_pres_shape())
    assert resp == {"intent": "STATEMENT", "confidence": 0.95}, repr(resp)
    assert calls == [], ("the decline must bind BEFORE the escalation LLM is paid for: "
                         + str(len(calls)) + " call(s)")


def test_retraction_lean_is_never_declined(_routed, monkeypatch):
    """The decline is bound to CORRECTION only: a RETRACTION lean keeps today's route
    (the FOSS-005 veto owns the negated shapes; an affirmative RETRACTION lean is NOT
    this fix's scope)."""
    resp = _classify(_BUG_SENTENCE, _RETRACTION_DESC, 0.9,
                     [], _clean_directive(), monkeypatch, shape=_person1_pres_shape())
    assert resp == {"intent": "RETRACTION", "confidence": 0.9}, repr(resp)


def test_third_person_correction_lean_still_routes_correction(_routed, monkeypatch):
    """WORK-ORDER PIN: the GENUINE es correction ("En realidad la ip es 10.0.0.5.")
    still routes CORRECTION — the predicate agrees 3rd person, the decline never fires."""
    resp = _classify(_ES_CORRECTION, _CORRECTION_DESC, 0.9,
                     [], _clean_directive("ser"), monkeypatch,
                     shape=_third_person_copula_shape())
    assert resp == {"intent": "CORRECTION", "confidence": 0.9}, repr(resp)


def test_marked_correction_still_routes_correction(_routed, monkeypatch):
    """A repair marker ("Actualmente/Actually ..." — clause-initial marker lemma) is
    correction-shape evidence: the decline never fires over it."""
    d = DirectiveAnalysis(
        imperative_root_lemma=None, imperative_root_negated=False,
        root_verb_lemma="usar", root_subject_is_self=False,
        clause_initial_markers=frozenset({"actually"}), has_contrastive_negation=False,
        cessation_advmod_lemmas=frozenset(), cessative_root_lemma=None,
        cessative_complement_shape=None)
    resp = _classify(_BUG_SENTENCE, _CORRECTION_DESC, 0.9,
                     [], d, monkeypatch, shape=_person1_pres_shape())
    assert resp["intent"] == "CORRECTION", repr(resp)


def test_grown_cue_keeps_the_correction_lane(_routed, monkeypatch):
    """A tenant-grown correction_signals literal matching the turn is DB evidence:
    the decline stands down and the lane routes as before (sub-gate → escalation)."""
    import src.api.llm_calls as llm_calls
    monkeypatch.setenv("LLM_MODEL_INTENT_ADJUDICATION", "test-adjudicator")
    calls = []

    async def _recorder(**kwargs):
        calls.append(kwargs)
        return {"route": "CORRECTION"}

    monkeypatch.setattr(llm_calls, "call_llm_with_retry_async", _recorder)
    resp = _classify(_BUG_SENTENCE, _CORRECTION_DESC, 0.63,
                     [], _clean_directive(), monkeypatch, shape=_person1_pres_shape(),
                     grown_hit=True)
    assert resp["intent"] == "CORRECTION", repr(resp)
    assert len(calls) == 1, repr(calls)


def test_admitted_cessative_keeps_the_removal_lane(_routed, monkeypatch):
    """"Dejo de usar el puerto 9004" — the DB-admitted cessation keeps today's route.
    (The es seed rows migration 293 adds — dejar/abandonar/cesar — are exactly what
    makes this screen live for Spanish.)"""
    resp = _classify("Dejo de usar el puerto 9004.", _CORRECTION_DESC, 0.9,
                     [], _clean_directive("dejar"), monkeypatch,
                     shape=_person1_pres_shape("dejar"), cessative=True)
    assert resp["intent"] == "CORRECTION", repr(resp)


def test_negated_twin_stays_the_vetos_lane(_routed, monkeypatch):
    """Disjointness: the NEGATED twin has a negation scope → the decline's facts
    unit returns nothing (scopes screen) and the FOSS-005 veto owns the turn. Here:
    scopes present → the veto fires FIRST and the decline is never consulted."""
    from src.extraction.linguistics import NegationScope
    scope = NegationScope(
        cue_text="No", head_text="uso", predicate_text="uso", scope_text=_BUG_SENTENCE,
        predicates_nominal=False, two_referential_poles=False, clause_terminated=False)
    main._INTENT_LAYER_STATS.pop("foss012-", None)  # fresh counters: order-independent
    resp = _classify("No uso el puerto 9004 en el servidor principal.",
                     _CORRECTION_DESC, 0.9,
                     [scope], _clean_directive(), monkeypatch, shape=_person1_pres_shape())
    assert resp["intent"] == "STATEMENT", repr(resp)  # via the VETO, not the decline
    stats = main._INTENT_LAYER_STATS.get("foss012-", {})
    assert stats.get("negated_statement_veto", 0) >= 1
    assert stats.get("affirmative_statement_decline", 0) == 0


def test_decline_flag_off_restores_the_legacy_route(_routed, monkeypatch):
    """AFFIRMATIVE_STATEMENT_DECLINE=false is the documented escape hatch → the legacy
    sub-gate escalation route."""
    import src.api.llm_calls as llm_calls
    monkeypatch.setenv("LLM_MODEL_INTENT_ADJUDICATION", "test-adjudicator")
    monkeypatch.setattr(main, "AFFIRMATIVE_STATEMENT_DECLINE", False)
    calls = []

    async def _recorder(**kwargs):
        calls.append(kwargs)
        return {"route": "CORRECTION"}

    monkeypatch.setattr(llm_calls, "call_llm_with_retry_async", _recorder)
    resp = _classify(_BUG_SENTENCE, _CORRECTION_DESC, 0.63,
                     [], _clean_directive(), monkeypatch, shape=_person1_pres_shape())
    assert resp["intent"] == "CORRECTION", repr(resp)
    assert len(calls) == 1, repr(calls)


def test_no_shape_facts_falls_through_to_the_legacy_route(_routed, monkeypatch):
    """Fail-safe: the grammar layer yields NO shape (parse miss / layer unavailable) →
    the label stands (the legacy route), never a blind demote."""
    import src.api.llm_calls as llm_calls
    monkeypatch.setenv("LLM_MODEL_INTENT_ADJUDICATION", "test-adjudicator")
    calls = []

    async def _recorder(**kwargs):
        calls.append(kwargs)
        return {"route": "CORRECTION"}

    monkeypatch.setattr(llm_calls, "call_llm_with_retry_async", _recorder)
    resp = _classify(_BUG_SENTENCE, _CORRECTION_DESC, 0.63,
                     [], _clean_directive(), monkeypatch, shape=None)
    assert resp["intent"] == "CORRECTION", repr(resp)
    assert len(calls) == 1, repr(calls)


# ── REAL-parse end-to-end (runs only where an es model ships) ─────────────────


@pytest.fixture()
def _transport_only(monkeypatch):
    """Transport/queue stubs ONLY — the grammar layer runs FOR REAL. No grammar fact
    is faked."""
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.setattr(main, "_ensure_tenant_ready", _async_noop)
    monkeypatch.setattr(main, "_spacy_cue_route", lambda text, user_id=None: (None, None))
    return monkeypatch


@_es_real
def test_es_real_parse_bug_sentence_routes_statement_via_decline(
        _transport_only, monkeypatch):
    """END-TO-END on the REAL es parse, no grammar monkeypatch: gate-passed
    CORRECTION@0.845 (the live-measured score) → STATEMENT@0.95 through the decline,
    the INTENT_ADJUDICATION LLM never consulted."""
    import src.api.llm_calls as llm_calls
    monkeypatch.setenv("LLM_MODEL_INTENT_ADJUDICATION", "test-adjudicator")
    calls = []

    async def _recorder(**kwargs):
        calls.append(kwargs)
        return {"route": "CORRECTION"}

    monkeypatch.setattr(llm_calls, "call_llm_with_retry_async", _recorder)
    resp = asyncio.run(main.classify_intent(
        {"text": _BUG_SENTENCE}, user_id="foss012-user-01",
        model=_fake_gliner(_CORRECTION_DESC, 0.845)))
    assert resp == {"intent": "STATEMENT", "confidence": 0.95}, (
        "the real-parse bug sentence was routed destructive: " + repr(resp))
    assert calls == [], "escalation must not be consulted: " + str(len(calls)) + " call(s)"


@_es_real
def test_es_real_parse_third_person_correction_stays_correction(
        _transport_only, monkeypatch):
    """END-TO-END on the REAL es parse: the genuine correction keeps its lane."""
    resp = asyncio.run(main.classify_intent(
        {"text": _ES_CORRECTION}, user_id="foss012-user-01",
        model=_fake_gliner(_CORRECTION_DESC, 0.9)))
    assert resp["intent"] == "CORRECTION", repr(resp)


@_es_real
def test_es_real_parse_negated_twin_still_vetoes_not_declines(
        _transport_only, monkeypatch):
    """On the real es parse the NEGATED twin keeps the FOSS-005 lane assignment (veto,
    not decline) — the two fixes stay disjoint on real parses too."""
    resp = asyncio.run(main.classify_intent(
        {"text": "No uso el puerto 9004 en ningún servidor."}, user_id="foss012-user-01",
        model=_fake_gliner(_CORRECTION_DESC, 0.9)))
    main._INTENT_LAYER_STATS.pop("foss012-", None)  # reset BEFORE the call below
    resp2 = asyncio.run(main.classify_intent(
        {"text": "No uso el puerto 9004 en ningún servidor."}, user_id="foss012-user-01",
        model=_fake_gliner(_CORRECTION_DESC, 0.9)))
    assert resp == {"intent": "STATEMENT", "confidence": 0.95}, repr(resp)
    # the classifier writes the layer counter under user_id[:8] — "foss012-" here.
    stats = main._INTENT_LAYER_STATS.get("foss012-", {})
    assert stats.get("negated_statement_veto", 0) >= 1
    assert stats.get("affirmative_statement_decline", 0) == 0





def test_hand_es_parse_initial_propon_artifact_rescues(monkeypatch):
    """THE MEASURED sm ARTIFACT (the es eval stack, SPACY_MODEL=es_core_news_sm): the battery
    sentence roots on Uso/PROPN — no Person/Tense. When _reads_as_first_person_verb
    re-lemmatizes the ROOT (the same rescue the subject-agreement repair uses), the
    shape facts are that 1sg-present paradigm. Stubbed here (no model in this env);
    the real-parse twin below carries the live proof where an es model ships."""
    import spacy
    from spacy.tokens import Doc
    vocab = spacy.blank("es").vocab
    words = ["Uso", "el", "puerto", "9004", "en", "el", "servidor", "principal", "."]
    pos = ["PROPN", "DET", "NOUN", "NUM", "ADP", "DET", "NOUN", "ADJ", "PUNCT"]
    deps = ["ROOT", "det", "obj", "nummod", "case", "det", "obl", "amod", "punct"]
    heads = [1, 3, 1, 3, 8, 8, 1, 8, 1]
    doc = Doc(vocab, words=words, pos=pos)
    doc[0].is_sent_start = True
    for i, (d, h) in enumerate(zip(deps, heads)):
        doc[i].dep_ = d
        doc[i].head = doc[h]
    monkeypatch.setattr(ling_mod, "_SPACY_MODEL", "es_core_news_sm")
    monkeypatch.setattr(ling_mod, "_nlp", lambda text: doc)
    monkeypatch.setattr(ling_mod, "_reads_as_first_person_verb", lambda tok: True)
    shape = analyze_first_person_present_statement(
        "Uso el puerto 9004 en el servidor principal.")
    assert shape is not None, "the PROPN artifact lost the rescue arm"
    assert shape.root_person_first is True, repr(shape)
    assert shape.root_tense_present is True, repr(shape)


def test_hand_es_parse_genuine_noun_is_not_rescued(monkeypatch):
    """Fail-safe leg of the rescue: _reads_as_first_person_verb False ("el uso del
    puerto" — a genuine noun) → the analyzer returns None → no decline."""
    import spacy
    from spacy.tokens import Doc
    vocab = spacy.blank("es").vocab
    words = ["Uso", "el", "puerto", "9004", "en", "el", "servidor", "principal", "."]
    pos = ["PROPN", "DET", "NOUN", "NUM", "ADP", "DET", "NOUN", "ADJ", "PUNCT"]
    deps = ["ROOT", "det", "obj", "nummod", "case", "det", "obl", "amod", "punct"]
    heads = [1, 3, 1, 3, 8, 8, 1, 8, 1]
    doc = Doc(vocab, words=words, pos=pos)
    doc[0].is_sent_start = True
    for i, (d, h) in enumerate(zip(deps, heads)):
        doc[i].dep_ = d
        doc[i].head = doc[h]
    monkeypatch.setattr(ling_mod, "_SPACY_MODEL", "es_core_news_sm")
    monkeypatch.setattr(ling_mod, "_nlp", lambda text: doc)
    monkeypatch.setattr(ling_mod, "_reads_as_first_person_verb", lambda tok: False)
    assert analyze_first_person_present_statement(
        "Uso el puerto 9004 en el servidor principal.") is None


@_es_real
def test_es_real_parse_initial_propon_artifact_rescues():
    """LIVE PROOF TWIN of the stubbed rescue pin: on a real es_core parse the
    battery sentence roots PROPN and _reads_as_first_person_verb rescues it —
    the analyzer returns the 1sg-present shape."""
    doc_probe = analyze_first_person_present_statement(
        "Uso el puerto 9004 en el servidor principal.")
    assert doc_probe is not None, (
        "no shape: the rescue arm is broken (or the es model now tags the initial "
        "1sg VERB — then the VERB arm above must carry it)")
    assert doc_probe.root_person_first is True, repr(doc_probe)
    assert doc_probe.root_tense_present is True, repr(doc_probe)


