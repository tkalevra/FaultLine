"""Q4 pins — the two blended-family ingest fixes (2026-11-13, branch fix/q4-kin-mint-atom-merge).

THE MEASURED WOUNDS (live on deploy-wm3final2 81c8d723, seat blfamdoor-1b796c64, 2026-11-13):

FIX 1 — KIN_TYPE_AT_MINT. "Our daughter Wren started kindergarten this September." — the
zero-shot labeler types the name span ANIMAL and the ingest TYPE STAMP persists it at mint;
the later {Person}-tailed kin edges then refuse and only the wound-map round-3 ADMISSION-side
repair (kin_type_from_lane_context) recovers them — measured: ingest.kin_copula_person_asserted
gliner_type=ANIMAL saved wren's stamp ONLY because the intro batch happened to carry a kin-rel
edge, and ingest.kin_type_from_lane_context prior_type=ANIMAL re-typed teo at the ex2 gate
(the refusal-then-repair dance the litmus wants gone). The fix: the intro turn's kin-cue lane
context (a kinship cue noun grammatically linked to the name span in the RAW turn — Frame A
"our daughter Wren", Frame B "Wren, our daughter,", Frame C "the mother of Wren and Teo")
overrides a concrete non-Person stamp AND the gate-passed type AT THE MINT, surface-keyed,
edge-order-independent. Flag KIN_TYPE_AT_MINT default ON; OFF = byte-parity.

FIX 2 — ATOM_MERGE_KIN_CONJUNCT. "Isabelle is the mother of Wren and Teo." — live harvest
envelope: (isabelle, instance_of, mother) + (isabelle, "parent_of: x is the parent of y", wren)
+ (isabelle, "parent_of: x is the parent of y", teo) + (isabelle, parent_of, teo) — the CLEAN
parent_of wren edge was SWALLOWED AT THE ATOM-MERGE. Mechanism (proven in-container):
gliner2's extract_relations returns the label DECORATED with its description
("parent_of: x is the parent of y", the pairs riding the decorated key and the bare key EMPTY);
the sentence-pipeline relation pass mints the decorated string onto doc._.rel; the deriver's
predicate convergence (minted WINS, linguistics.py) substitutes it over the clean canonical
rel for the minted (subj_tok, obj_tok) pair — the wren conjunct's clean parent_of never
existed downstream — and the ingest vocab guard then vetoed the composite
(stage vocab_guard_rel_type) → statements_dropped, committed=2, and "who is the mother of
Wren and Teo?" served the wrong-side child_of row. Two flag-gated guards: the converter
normalizes the decorated key back to the passed label (the birth is undone), and the
atom-merge read (_minted_rel_for_pair) treats a NON-MINTABLE minted rel as absent so the
deriver's clean rel stands. Flag ATOM_MERGE_KIN_CONJUNCT default ON; OFF = byte-parity —
the flag-OFF typed-doc repro below reproduces the live harvest shape exactly.

No LLM, no GLiNER2 model, no DB: the detector and the convergence are pure spaCy; the
decorated-mint repro seeds the typed Doc exactly as _build_typed_doc's GLiNER2 pass does.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO = Path(__file__).resolve().parents[1]
MAIN_SOURCE = (REPO / "src" / "api" / "main.py").read_text(encoding="utf-8")
KIN_LANE_SOURCE = (REPO / "src" / "extraction" / "kin_lane_context.py").read_text(encoding="utf-8")
LING_SOURCE = (REPO / "src" / "extraction" / "linguistics.py").read_text(encoding="utf-8")


@pytest.fixture()
def kin_lane_module():
    import src.extraction.kin_lane_context as klc
    return klc


# ── FIX 1: the kin-cue lane context detector ─────────────────────────────────────────
@pytest.mark.parametrize("sentence,want,reject", [
    ("Our daughter Wren started kindergarten this September.",
     {"wren"}, {"kindergarten", "september"}),
    ("My son Teo is 9 and he's in grade 4 at Ridgeline Elementary.",
     {"teo"}, {"ridgeline", "elementary"}),
    ("Isabelle is the mother of Wren and Teo.",
     {"wren", "teo"}, {"isabelle"}),
    ("the parents of Wren and Teo are coming.",
     {"wren", "teo"}, set()),
    ("My wife Maren was born on June 14, 1988.",
     {"maren"}, {"june"}),
    ("Wren, our daughter, started school today.",
     {"wren"}, {"school"}),
    ("Our dog Biscuit is a golden retriever.",
     set(), {"biscuit"}),
    ("The mother of Wren at school was kind.",
     {"wren"}, {"school"}),
])
def test_kin_lane_frames_record_only_the_kin_denoting_spans(sentence, want, reject, kin_lane_module):
    """Frames A/B/C record exactly the name spans in kin-denoting phrases; non-kin
    content in the same sentence (kindergarten/school/june) and non-kin names
    (biscuit) are never recorded; the frame-2nd-PP stop keeps 'school' out of
    'the mother of Wren at school'."""
    got = kin_lane_module.observe_kin_lane_contexts(sentence)
    assert want <= set(got), f"missing {want - set(got)} in {sorted(got)}"
    assert not (reject & set(got)), f"over-captured {reject & set(got)} in {sorted(got)}"


def test_kin_lane_person_override_pure_decision(kin_lane_module):
    """The override fires ONLY for an overlay surface with a CONCRETE non-Person stamp:
    ANIMAL → True; Person (already right) → False; None/unknown (nothing to repair) →
    False; a surface outside the overlay → False. Reset unbinds (no cross-turn leak)."""
    tok = kin_lane_module.set_kin_lane_contexts(
        kin_lane_module.observe_kin_lane_contexts(
            "Our daughter Wren started kindergarten this September."))
    try:
        assert kin_lane_module.kin_lane_person_override("wren", "Animal") is True
        assert kin_lane_module.kin_lane_person_override("wren", "ANIMAL") is True
        assert kin_lane_module.kin_lane_person_override("wren", "Person") is False
        assert kin_lane_module.kin_lane_person_override("wren", None) is False
        assert kin_lane_module.kin_lane_person_override("wren", "unknown") is False
        assert kin_lane_module.kin_lane_person_override("kindergarten", "Animal") is False
        assert kin_lane_module.kin_lane_person_override("biscuit", "Animal") is False
    finally:
        kin_lane_module.reset_kin_lane_contexts(tok)
    assert kin_lane_module.kin_lane_person_override("wren", "Animal") is False


def test_kin_type_at_mint_wiring_is_present():
    """Structural: the flag, the request-boundary bind, both stamp overrides, and the
    gate-feed adjudication exist in main.py (anchored to the repo this file ships in)."""
    assert "_KIN_TYPE_AT_MINT = (" in MAIN_SOURCE
    assert 'os.environ.get("KIN_TYPE_AT_MINT", "true")' in MAIN_SOURCE
    assert "observe_kin_lane_contexts as _observe_kin_lane" in MAIN_SOURCE
    assert MAIN_SOURCE.count("ingest.kin_type_at_mint") >= 2, \
        "both stamp sides (subject + object) must carry the mint-time override marker"
    assert "ingest.kin_type_at_mint_gate_feed" in MAIN_SOURCE, \
        "the gate-passed type must be adjudicated too — the gate consumes the PASSED type"
    assert "or _kinmint_subj" in MAIN_SOURCE and "or _kinmint_obj" in MAIN_SOURCE, \
        "a kinmint-adjudicated stamp must be written UNCONDITIONALLY (the row may already \
carry ANIMAL from an earlier edge of the same batch)"


def test_gate_feed_sits_at_the_last_mile():
    """R4-1b-class ORDERING pin (measured live on image 73a2d17c's first deploy): the
    classification phase re-assigns final_object_type from edge.object_type / the GLiNER
    cache / the DB row AFTER the stamps — an adjudication placed before those resets logs
    and is then silently overwritten (the gate received ANIMAL and the refusal fired
    anyway). The gate-feed block must sit AFTER the LAST of those re-assignments and AFTER
    the directionality swap's type re-sync, i.e. in the validate_edge last mile."""
    last_reset = max(
        MAIN_SOURCE.rindex("final_object_type = edge.object_type"),
        MAIN_SOURCE.rindex("final_object_type = _gliner_cache[object_key]"),
        MAIN_SOURCE.rindex("final_object_type = _row[0]"),
        MAIN_SOURCE.rindex("final_subject_type, final_object_type = final_object_type, final_subject_type"),
    )
    feed_pos = MAIN_SOURCE.index("ingest.kin_type_at_mint_gate_feed")
    assert feed_pos > last_reset, \
        "the gate-feed adjudication is starvable at its position — the classification " \
        "phase resets final_*_type after it (the R4-1b live-dark class)"
    assert "THE LAST-MILE GATE FEED" in MAIN_SOURCE
    assert "_kin_role_swap" in MAIN_SOURCE, \
        "the directionality swap trades the roles — the adjudication must be swap-aware"


# ── FIX 2: the atom-merge conjunct preservation ──────────────────────────────────────
_DECORATED = "parent_of: x is the parent of y"


def _typed_doc_with_decorated_mint():
    """The live sentence-pipeline shape: a TYPED Doc (GLiNER2 ents seeded exactly as
    _build_typed_doc does) + the DECORATED rel minted onto doc._.rel at the ent-root pair
    (the _head_token_index_for_surface keying — measured live in-container)."""
    import datetime
    import spacy
    from spacy.tokens import Span
    import src.extraction.linguistics as L

    nlp = spacy.load(os.environ.get("SPACY_MODEL", "en_core_web_sm"))
    doc = nlp("Isabelle is the mother of Wren and Teo.")
    ents = []
    for t in doc:
        if t.text == "Isabelle":
            ents.append(Span(doc, t.i, t.i + 1, label="PERSON"))
        elif t.text in ("Wren", "Teo"):
            ents.append(Span(doc, t.i, t.i + 1, label="ANIMAL"))
    doc.ents = ents
    si = [e.root.i for e in doc.ents if e.text == "Isabelle"][0]
    oi = [e.root.i for e in doc.ents if e.text == "Wren"][0]
    assert L.set_minted_rel(doc, si, oi, _DECORATED)
    return L, doc, datetime.datetime(2026, 11, 13)


def test_atom_merge_guard_preserves_both_kin_conjuncts(monkeypatch):
    """GUARD ON (default): the decorated mint is declined at the atom-merge read — the
    deriver's CLEAN canonical rels stand for BOTH conjuncts; no composite anywhere."""
    import src.extraction.linguistics as L
    monkeypatch.setenv("ATOM_MERGE_KIN_CONJUNCT", "true")
    L_mod, doc, ref = _typed_doc_with_decorated_mint()
    facts = L_mod.derive_sentence_facts(doc, ref) or []
    rels = [f.rel_type for f in facts]
    assert ("isabelle", "parent_of", "wren") in [(f.subject, f.rel_type, f.object) for f in facts], \
        f"the wren conjunct must survive with its clean rel; got {rels}"
    assert ("isabelle", "parent_of", "teo") in [(f.subject, f.rel_type, f.object) for f in facts]
    assert not any(": " in r for r in rels), \
        f"the decorated composite must never reach an edge; got {rels}"
    assert ("isabelle", "instance_of", "mother") in [(f.subject, f.rel_type, f.object) for f in facts]


def test_atom_merge_flag_off_is_byte_parity(monkeypatch):
    """FLAG OFF: byte-for-byte today's behaviour — the minted composite WINS at the
    convergence for the minted pair (wren's clean rel is swallowed) while the unminted
    teo conjunct keeps its clean rel. This is the EXACT live harvest shape measured on
    deploy-wm3final2 (the deterministic repro of the Q4 drop)."""
    import src.extraction.linguistics as L
    monkeypatch.setenv("ATOM_MERGE_KIN_CONJUNCT", "false")
    L_mod, doc, ref = _typed_doc_with_decorated_mint()
    facts = L_mod.derive_sentence_facts(doc, ref) or []
    triplets = [(f.subject, f.rel_type, f.object) for f in facts]
    assert ("isabelle", _DECORATED, "wren") in triplets, \
        f"flag OFF must reproduce today's composite substitution; got {triplets}"
    assert ("isabelle", _DECORATED, "teo") in triplets
    assert ("isabelle", "parent_of", "teo") in triplets
    assert ("isabelle", "parent_of", "wren") not in triplets, \
        "today's behaviour: the wren conjunct's clean rel is consumed by the convergence"


def test_gliner2_decorated_key_normalized_at_converter(monkeypatch):
    """The converter (flag ON) normalizes the decorated key back to the passed label —
    the birth of the composite is undone at ALL consumer lanes; (flag OFF) the decorated
    key passes through verbatim (byte-parity)."""
    import src.api.main as M
    raw = {"relation_extraction": {
        _DECORATED: [["Isabelle", "Wren"], ["Isabelle", "Teo"]],
        "parent_of": [],
        "instance_of": [["Isabelle", "mother"]],
    }}
    monkeypatch.setattr(M, "ATOM_MERGE_KIN_CONJUNCT", True)
    edges = M._convert_gliner_relations_to_edges(raw, {}) or []
    assert {e["rel_type"] for e in edges} == {"parent_of", "instance_of"}
    assert ("isabelle", "parent_of", "wren") in [(e["subject"], e["rel_type"], e["object"]) for e in edges]

    monkeypatch.setattr(M, "ATOM_MERGE_KIN_CONJUNCT", False)
    edges_off = M._convert_gliner_relations_to_edges(raw, {}) or []
    assert {e["rel_type"] for e in edges_off} == {_DECORATED, "instance_of"}, \
        "flag OFF must pass the decorated key through verbatim (today's behaviour)"


def test_atom_merge_gate_wiring_is_present():
    """Structural: the live-read gate in linguistics.py + the converter normalization in
    main.py + the flag declaration exist (anchored to the repo this file ships in)."""
    assert "_atom_merge_mint_vocabulary_gate" in LING_SOURCE
    assert 'os.environ.get("ATOM_MERGE_KIN_CONJUNCT", "true")' in LING_SOURCE
    assert "rel_type_vocabulary_ok" in LING_SOURCE
    assert "ATOM_MERGE_KIN_CONJUNCT = (" in MAIN_SOURCE
    assert "gliner2.decorated_rel_key_normalized" in MAIN_SOURCE


def test_non_mintable_vocabulary_authority_agrees():
    """The shared authority the atom-merge gate consumes must veto the measured composite
    (the same verdict the ingest vocab guard reached at stage vocab_guard_rel_type)."""
    from src.extraction.rel_vocab import rel_type_vocabulary_ok
    assert rel_type_vocabulary_ok(_DECORATED) is False
    assert rel_type_vocabulary_ok("parent_of") is True
