"""
Intent-label regression gate (GLiNER2 classify_text).

Guards the Pitfall-11 failure mode that shipped as a live bug (commit 07a5484):
verbose, example-laden intent labels collapse GLiNER2 zero-shot accuracy and make
editing/usage statements ("My favorite editor is vim") mis-score as CORRECTION ->
routed to retraction, never ingested. Concise "D1" labels fixed it — benchmarked on
fastino/gliner2-base-v1 (39 cases): 48.7% -> 87.2% accuracy, editor/usage
false-CORRECTION 8/10 -> 0/10.

This test pulls the ACTUAL shipped labels from src.api.main
(_build_intent_descriptions_for_gliner2) and runs them through the SAME model and the
SAME call shape main.py uses (model.classify_text(text, {"intent": [labels]},
include_confidence=True) -> single-label softmax). So any future regression of the
labels — back to verbose, or any change that re-breaks the editor/usage boundary —
fails here instead of silently in production.

GLiNER2 stays FROZEN; only the label *strings* are under test. A label change is a
benchmark-gated whole-label replacement, never an accretion (Pitfall 11 / 11a).

Skips cleanly when gliner2 or the model weights are unavailable.
See DEV/DESIGN-intent-classification-gliner2-label-safety.md.
"""
import pytest

pytest.importorskip("gliner2")  # skip whole module if GLiNER2 isn't installed

INTENTS = ["QUERY", "STATEMENT", "RETRACTION", "CORRECTION"]
MODEL_ID = "fastino/gliner2-base-v1"  # matches src/api/main.py:4293

# D1 benchmarked at 87.2%; conservative floor that still catches a real regression
# (the verbose labels scored 48.7%) without flaking on model/version variance.
ACCURACY_BAR = 0.80

# (text, gold_intent, is_editor_usage_edge) — the benchmarked eval set.
CASES = [
    # live repro cases (the bug)
    ("My favorite editor is vim", "STATEMENT", True),
    ("I edit code with nano", "STATEMENT", True),
    ("vim is great", "STATEMENT", True),
    ("My favorite color is blue", "STATEMENT", False),
    # more editor/usage / preference edges
    ("I use emacs for everything", "STATEMENT", True),
    ("My favorite editor is neovim", "STATEMENT", True),
    ("I edit my configs in vi", "STATEMENT", True),
    ("My preferred shell is zsh", "STATEMENT", True),
    ("I write Python in VSCode", "STATEMENT", True),
    ("My go-to text editor is sublime", "STATEMENT", True),
    ("I update my dotfiles with vim", "STATEMENT", True),
    ("My favorite language is Rust", "STATEMENT", False),
    # true STATEMENT (identity / facts / usage)
    ("My son is named Theo and he's 12 years old", "STATEMENT", False),
    ("I have a dog named Spot who is brown", "STATEMENT", False),
    ("I work as a software engineer at TechCorp", "STATEMENT", False),
    ("I like to play tennis on weekends", "STATEMENT", False),
    ("My wife's name is Marla", "STATEMENT", False),
    ("I graduated from MIT", "STATEMENT", False),
    ("I'm 38 years old", "STATEMENT", False),
    ("I live in Kitchener", "STATEMENT", False),
    # true CORRECTION (replace prior value)
    ("My age is 45, not 42", "CORRECTION", False),
    ("Actually I'm 42 not 35", "CORRECTION", False),
    ("No it's vim not emacs", "CORRECTION", False),
    ("I work at Acme now, not TechCorp", "CORRECTION", False),
    ("My spouse is Jordan, not Casey", "CORRECTION", False),
    ("I live in NYC, not LA", "CORRECTION", False),
    ("My name is Alex, not Alexis", "CORRECTION", False),
    # true RETRACTION (forget/delete)
    ("Forget about my age", "RETRACTION", False),
    ("Forget that I mentioned vim", "RETRACTION", False),
    ("Delete my work information", "RETRACTION", False),
    ("Remove that fact about me", "RETRACTION", False),
    ("I'm no longer married", "RETRACTION", False),
    ("Erase my birth date", "RETRACTION", False),
    # true QUERY
    ("What editor do I use", "QUERY", False),
    ("What is my age?", "QUERY", False),
    ("Tell me about my pets", "QUERY", False),
    ("Do you know my work history?", "QUERY", False),
    ("What are my hobbies?", "QUERY", False),
    ("Which shell do I prefer?", "QUERY", False),
]


@pytest.fixture(scope="session")
def gliner2_model():
    from gliner2 import GLiNER2
    try:
        return GLiNER2.from_pretrained(MODEL_ID)
    except Exception as exc:  # weights not cached / no network in CI
        pytest.skip(f"GLiNER2 model {MODEL_ID} unavailable: {exc}")


@pytest.fixture(scope="session")
def shipped_labels():
    """The labels CURRENTLY shipped in the backend — the thing under regression test."""
    from src.api.main import _build_intent_descriptions_for_gliner2
    labels = _build_intent_descriptions_for_gliner2()
    assert set(labels) == set(INTENTS), f"unexpected intent set: {sorted(labels)}"
    return labels


def _classify(model, text, labels_map):
    """Mirror src/api/main.py:6388 — descriptions as labels, single-label softmax."""
    desc_to_intent = {labels_map[name]: name for name in INTENTS}
    labels = [labels_map[name] for name in INTENTS]
    res = model.classify_text(text, {"intent": labels}, include_confidence=True)
    ir = res.get("intent", {}) if isinstance(res, dict) else {}
    label = ir.get("label") if isinstance(ir, dict) else ir
    return desc_to_intent.get(label, "?")


def _predict_all(model, labels_map):
    return [(text, _classify(model, text, labels_map), gold, edge)
            for text, gold, edge in CASES]


def test_editor_usage_never_misroutes_to_correction(gliner2_model, shipped_labels):
    """The exact shipped bug: editor/usage statements must NOT score CORRECTION.

    This is the hard gate — 0 tolerance. If it fails, the live editor-misroute
    bug (statements routed to retraction, never ingested) has returned.
    """
    offenders = [
        (text, pred)
        for text, pred, gold, edge in _predict_all(gliner2_model, shipped_labels)
        if edge and pred == "CORRECTION" and gold != "CORRECTION"
    ]
    assert offenders == [], (
        "editor/usage statements misrouted to CORRECTION (Pitfall-11 label regression): "
        f"{offenders}"
    )


def test_intent_label_accuracy_meets_bar(gliner2_model, shipped_labels):
    """Overall accuracy must clear the floor; verbose labels scored 48.7%."""
    preds = _predict_all(gliner2_model, shipped_labels)
    correct = sum(1 for _text, pred, gold, _edge in preds if pred == gold)
    acc = correct / len(preds)
    assert acc >= ACCURACY_BAR, (
        f"intent-label accuracy {acc:.1%} < {ACCURACY_BAR:.0%} floor "
        f"({correct}/{len(preds)}) — labels regressed"
    )


@pytest.mark.parametrize("text", [
    "My favorite editor is vim",
    "I edit code with nano",
    "vim is my editor",
    "I use vim",
])
def test_live_repro_cases_classify_statement(gliner2_model, shipped_labels, text):
    """The precise utterances from the live repro must classify STATEMENT, not CORRECTION."""
    assert _classify(gliner2_model, text, shipped_labels) == "STATEMENT"
