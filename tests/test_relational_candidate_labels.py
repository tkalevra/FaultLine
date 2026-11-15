"""Unit test for _build_relational_candidate_labels (trigger-span GLiNER2 pass).

DEV/DESIGN-trigger-span-gliner2-extraction.md, point 3: the candidate label set handed
to GLiNER2's extract_relations() from a focused trigger span must be metadata-driven,
CONCISE (Pitfall 11 — names + short natural_language only, never blobs), and CAPPED at
the relational budget. This guards that contract without any live DB / GLiNER2 model.

No DB, no network, no model load — pure dict-in / dict-out on the helper.
"""
from src.api.main import _build_relational_candidate_labels, _GLINER2_RELATIONAL_LABEL_CAP


def _meta():
    return {
        "spouse":    {"head_types": ["Person"], "category": "family",  "natural_language": "is married to"},
        "works_for": {"head_types": ["Person"], "category": "work",    "natural_language": "is employed by"},
        "has_ip":    {"head_types": [],          "category": "network", "natural_language": "has IP address"},
        "has_pet":   {"head_types": ["Person"], "category": "social",  "natural_language": "has the pet"},
    }


def test_shape_and_metadata_driven_thresholds():
    out = _build_relational_candidate_labels(_meta())
    # All rel_types present (under the cap), shaped as {rel_type: {description, threshold}}.
    assert set(out) == {"spouse", "works_for", "has_ip", "has_pet"}
    for entry in out.values():
        assert set(entry.keys()) == {"description", "threshold"}
    # Threshold is metadata-driven (_gliner2_threshold_for): human=0.5, network=0.85.
    assert out["spouse"]["threshold"] == 0.5
    assert out["works_for"]["threshold"] == 0.5
    assert out["has_ip"]["threshold"] == 0.85


def test_concise_descriptions_kept_blobs_dropped():
    meta = {
        "good":  {"head_types": ["Person"], "category": "work", "natural_language": "manages"},
        "blob":  {"head_types": ["Person"], "category": "work", "natural_language": "x" * 200},
        "multi": {"head_types": ["Person"], "category": "work", "natural_language": "a; b; c"},
        "lines": {"head_types": ["Person"], "category": "work", "natural_language": "a\nb"},
        "none":  {"head_types": ["Person"], "category": "work", "natural_language": None},
    }
    out = _build_relational_candidate_labels(meta)
    # Pitfall 11: concise natural_language survives; blob/multi-clause/newline → empty string.
    assert out["good"]["description"] == "manages"
    assert out["blob"]["description"] == ""
    assert out["multi"]["description"] == ""
    assert out["lines"]["description"] == ""
    assert out["none"]["description"] == ""


def test_cap_is_enforced_deterministically():
    # 30 rel_types, cap at default — output never exceeds the cap and is deterministic.
    big = {
        f"rel_{i:02d}": {"head_types": ["Person"], "category": "work", "natural_language": "verb"}
        for i in range(30)
    }
    out = _build_relational_candidate_labels(big)
    assert len(out) == _GLINER2_RELATIONAL_LABEL_CAP
    # Deterministic: same input → same selected keys.
    assert _build_relational_candidate_labels(big) == out
    # Explicit cap argument honored.
    assert len(_build_relational_candidate_labels(big, cap=3)) == 3


def test_empty_and_garbage_input():
    assert _build_relational_candidate_labels({}) == {}
    assert _build_relational_candidate_labels(None) == {}
    # Garbage entries are skipped, not crashed on.
    assert _build_relational_candidate_labels({"": {}, "ok": {"natural_language": "v"}}) == {
        "ok": {"description": "v", "threshold": 0.6}
    }
