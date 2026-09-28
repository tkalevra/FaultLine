"""Focused unit tests for src/extraction/possessive_head.py (LongMemEval Q1 unlock).

The bug: "I had an issue with my car's GPS system" extracts as (i, has_issue, car) — object
is the POSSESSOR. The resolver rewrites it to the POSSESSIVE HEAD (gps system).

Run: python3 -m pytest tests/test_possessive_head.py -q   (tests/ is gitignored → git add -f)
"""
import importlib
import os

import pytest

import src.extraction.possessive_head as ph


def _reload(**env):
    """Reload the module under a fresh env so the kill-switch constant re-reads."""
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return importlib.reload(ph)


def _edge(obj, **extra):
    e = {"subject": "i", "rel_type": "has_issue", "object": obj}
    e.update(extra)
    return e


# ── THE GOLD CLAUSE (the Q1 unlock) ──────────────────────────────────────────
def test_gold_clause_car_to_gps_system():
    m = _reload()
    text = "I had an issue with my car's GPS system, it was not functioning correctly"
    out = m.resolve_object_heads([_edge("car")], text)
    assert out[0]["object"] == "gps system", out
    # The WRONG possessor must NOT survive.
    assert out[0]["object"] != "car"


def test_gold_clause_via_extract_edge_shape():
    """The (i, has_issue, car) → (i, has_issue, gps system) transformation, edge dict in/out."""
    m = _reload()
    text = "by the way, I had an issue with my car's GPS system"
    before = _edge("car", object_type="OBJECT", confidence=0.8, fact_provenance="user_stated")
    after = m.resolve_object_heads([before], text)[0]
    assert (after["subject"], after["rel_type"], after["object"]) == ("i", "has_issue", "gps system")
    # rel_type + subject + provenance preserved; stale object_type cleared.
    assert after["fact_provenance"] == "user_stated"
    assert after["object_type"] is None


# ── SIMPLE + NESTED POSSESSIVES ──────────────────────────────────────────────
def test_simple_possessive_joels_bike():
    m = _reload()
    out = m.resolve_object_heads([_edge("joel")], "joel's bike is red")
    assert out[0]["object"] == "bike"


def test_nested_possessive_takes_rightmost_head():
    m = _reload()
    text = "joel's bike's tire is flat"
    out = m.resolve_object_heads([_edge("joel")], text)
    assert out[0]["object"] == "tire", out


def test_three_word_head_company_server():
    m = _reload()
    out = m.resolve_object_heads([_edge("company")], "the company's database server crashed")
    assert out[0]["object"] == "database server"


def test_curly_apostrophe():
    m = _reload()
    out = m.resolve_object_heads([_edge("car")], "my car’s GPS system failed")
    assert out[0]["object"] == "gps system"


# ── NON-REGRESSION: NON-POSSESSIVE OBJECTS PASS THROUGH UNCHANGED ─────────────
def test_non_possessive_issue_with_server_unchanged():
    m = _reload()
    out = m.resolve_object_heads([_edge("server")], "I had an issue with the server")
    assert out[0]["object"] == "server"


def test_owns_a_car_unchanged():
    m = _reload()
    out = m.resolve_object_heads([{"subject": "i", "rel_type": "owns", "object": "car"}],
                                 "I own a car")
    assert out[0]["object"] == "car"


def test_possessor_only_my_car_unchanged():
    """No possessive `'s` after "car" → object stays "car" (don't strip too much)."""
    m = _reload()
    out = m.resolve_object_heads([_edge("car")], "I had an issue with my car")
    assert out[0]["object"] == "car"


def test_word_boundary_no_match_inside_other_word():
    """"car" must not match inside "cart's"."""
    m = _reload()
    out = m.resolve_object_heads([_edge("car")], "the cart's wheel broke")
    assert out[0]["object"] == "car"  # no "car's" in text → unchanged


def test_trailing_possessive_no_head_unchanged():
    """Possessive at end of clause with no head noun → don't strip to empty."""
    m = _reload()
    out = m.resolve_object_heads([_edge("car")], "that car's, anyway")
    assert out[0]["object"] == "car"


def test_head_terminated_by_preposition():
    """"my house's roof in the rain" → head stops at "in"."""
    m = _reload()
    out = m.resolve_object_heads([_edge("house")], "my house's roof in the rain")
    assert out[0]["object"] == "roof"


# ── FAIL-SAFE + KILL-SWITCH ──────────────────────────────────────────────────
def test_empty_text_passthrough():
    m = _reload()
    edges = [_edge("car")]
    assert m.resolve_object_heads(edges, "") == edges


def test_empty_edges_passthrough():
    m = _reload()
    assert m.resolve_object_heads([], "my car's gps") == []


def test_bad_edge_failsafe():
    """A structurally bad edge (object not a str) is left untouched, never crashes."""
    m = _reload()
    bad = {"subject": "i", "rel_type": "x", "object": None}
    out = m.resolve_object_heads([bad], "my car's gps system")
    assert out[0] is bad


def test_kill_switch_off_passthrough():
    m = _reload(POSSESSIVE_HEAD_RESOLVE="false")
    text = "I had an issue with my car's GPS system"
    out = m.resolve_object_heads([_edge("car")], text)
    assert out[0]["object"] == "car"  # disabled → no rewrite
    _reload(POSSESSIVE_HEAD_RESOLVE=None)  # restore default for other tests


# ── LINGUISTIC-LAYER FOLD (spaCy poss primary, bespoke walk fallback) ─────────
def test_spacy_primary_is_exercised_when_available(monkeypatch):
    """When the linguistic layer is available, the spaCy poss→head resolver is the PRIMARY: it
    resolves the gold clause WITHOUT falling back to the bespoke '\\''s'-walk. Guards that the fold
    actually wires spaCy in (not silently always-fallback)."""
    import src.extraction.linguistics as L
    if not L.linguistics_available():
        pytest.skip("en_core_web_sm not installed in test env")
    m = _reload()
    calls = {"bespoke": 0}
    orig = m._lift_head_after_possessive

    def _spy(*a, **k):
        calls["bespoke"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(m, "_lift_head_after_possessive", _spy)
    out = m.resolve_object_heads([_edge("car")], "I had an issue with my car's GPS system")
    assert out[0]["object"] == "gps system"
    assert calls["bespoke"] == 0   # spaCy resolved → bespoke walk never reached


def test_bespoke_fallback_when_layer_off(monkeypatch):
    """LINGUISTIC_LAYER=0 → spaCy primary is skipped; the bespoke '\\''s'-walk still resolves the
    gold clause (today's behavior before the fold). Same result, different path."""
    import src.extraction.linguistics as L
    m = _reload()
    # Force the layer unavailable for this module's import-time check.
    monkeypatch.setattr(L, "linguistics_available", lambda: False)
    out = m.resolve_object_heads([_edge("car")], "I had an issue with my car's GPS system")
    assert out[0]["object"] == "gps system"   # bespoke fallback path


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
