"""FOSS-005 es pin: a sentential-negation statement never reaches the retraction lane.

On es 761cf914, remember_facts("No uso el puerto 9004 en ningún servidor.") classified
RETRACTION (GLiNER2-native zero-shot on Spanish text — every upstream cue guard is
English/lexical and the neg-scope ensemble only engages STATEMENT/QUERY leans), the
/retract/correct lane failed honestly ("old fact not found") and STILL minted the
affirmative edge (user, usar, puerto) — recall then asserted "You use Puerto".

The es classification side fixed here: a gate-passed destructive GLiNER2 lean whose ONLY
negation is SENTENTIAL (verbal predicate — not the nominal/two-pole structural denial
the ensemble/CORRECTION lane own) and which carries NO explicit removal/repair cue is
vetoed to STATEMENT with polarity preserved — en's classification outcome for the same
shape. The decision logic is PURE (`_sentential_negation_blocks_destructive` in
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
from src.api.main import _sentential_negation_blocks_destructive
from src.extraction.linguistics import (
    DirectiveAnalysis,
    NegationScope,
    analyze_directive,
    analyze_negation_scopes,
    linguistics_available,
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
    "mi gata no es Fraggle": the ensemble/CORRECTION lane's shape, never vetoed."""
    return NegationScope(
        cue_text="no",
        head_text="es",
        predicate_text="es",
        scope_text="mi gata no es Fraggle",
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


@_ling
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
    resp = _classify("Mi gata no es Fraggle.", _RETRACTION_DESC, 0.9,
                     [_structural_denial_scope()], _plain_statement_directive(), monkeypatch)
    assert resp["intent"] == "RETRACTION", resp


def test_veto_flag_off_restores_the_legacy_route(_routed, monkeypatch):
    """SENTENTIAL_NEGATION_VETO=false is the documented escape hatch → legacy RETRACTION."""
    monkeypatch.setattr(main, "SENTENTIAL_NEGATION_VETO", False)
    resp = _classify(_BUG_SENTENCE, _RETRACTION_DESC, 0.93,
                     [_sentential_scope()], _plain_statement_directive(), monkeypatch)
    assert resp == {"intent": "RETRACTION", "confidence": 0.93}

