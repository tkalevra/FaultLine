"""
Seed rel_type template-preservation guard (strength-passing-recursion bug).

Root cause being defended:
  evaluate_ontology_candidates() re-APPROVED EXISTING seed rels (owns/participated_in/
  instance_of) queued into ontology_evaluations, called the LLM with an instance snippet,
  and the rel_types UPSERT UNCONDITIONALLY overwrote natural_language → the clean seed
  template "X participated in Y" got clobbered to "You participated in unknown".

Defense in depth (all three asserted here at the logic / SQL-shape level — no live DB):
  FIX #1  evaluate_ontology_candidates skips regeneration of an EXISTING curated rel.
  FIX #2a _query_llm_for_rel_type_metadata REJECTS a 3p natural_language lacking the "X"
          subject placeholder (mirror of the existing 2p "Y" check).
  FIX #2b the rel_types UPSERTs COALESCE natural_language so a NULL/blank generated value
          never nukes a good existing template.
  RESET   run_longmemeval.reset_tenant restores seed phrasing from public each run.
"""
import os
import sys
import importlib.util
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))


def _load_embedder():
    import src.re_embedder.embedder as emb
    return emb


# ────────────────────────────────────────────────────────────────────────────
# FIX #2a — 3p "X"-placeholder validation in _query_llm_for_rel_type_metadata
# ────────────────────────────────────────────────────────────────────────────

def _fake_llm(natural_language, natural_language_2p="You X-ed Y"):
    # Returns a parsed-JSON dict shaped like call_llm_with_retry_sync output.
    return {
        "natural_language": natural_language,
        "natural_language_2p": natural_language_2p,
        "is_symmetric": False,
        "inverse_rel_type": None,
        "category": "social",
        "head_types": ["Person"],
        "tail_types": ["Person"],
        "fact_class": "B",
        "confidence": 0.8,
    }


def test_3p_missing_X_placeholder_is_rejected():
    """A 3p template lacking the X subject placeholder (baked instance / 'unknown')
    must be REJECTED outright — returns {} so the caller skips the UPSERT."""
    emb = _load_embedder()
    bad = _fake_llm("You participated in unknown", natural_language_2p="You attended Y")
    with patch.object(emb, "call_llm_with_retry_sync", return_value=bad):
        out = emb._query_llm_for_rel_type_metadata(
            "participated_in", "Person", "Event", "You attended Workshop on Jan 10", "url"
        )
    assert out == {}, "placeholderless 3p must be rejected (no metadata returned)"


def test_3p_with_X_placeholder_is_accepted():
    """A well-formed 3p template (carries X) passes and returns metadata."""
    emb = _load_embedder()
    good = _fake_llm("X participated in Y", natural_language_2p="You participated in Y")
    with patch.object(emb, "call_llm_with_retry_sync", return_value=good):
        out = emb._query_llm_for_rel_type_metadata(
            "participated_in", "Person", "Event", "X attended Y", "url"
        )
    assert out, "valid 3p template should yield metadata"
    assert out["llm_natural_language"] == "X participated in Y"
    # The valid 2p is preserved too.
    assert out["llm_natural_language_2p"] == "You participated in Y"


def test_2p_validation_still_drops_malformed_2p_but_keeps_valid_3p():
    """Regression: a malformed 2p is dropped (existing behavior) while a valid 3p
    is still returned — the two validations are independent."""
    emb = _load_embedder()
    md = _fake_llm("X knows Y", natural_language_2p="X knows Y")  # 2p contains X → invalid
    with patch.object(emb, "call_llm_with_retry_sync", return_value=md):
        out = emb._query_llm_for_rel_type_metadata("knows", "Person", "Person", "X knows Y", "url")
    assert out, "valid 3p means metadata is returned"
    assert out["llm_natural_language"] == "X knows Y"
    assert out["llm_natural_language_2p"] == "", "malformed 2p dropped to empty"


# ────────────────────────────────────────────────────────────────────────────
# FIX #2b — COALESCE UPSERT shape (a blank EXCLUDED never nukes a good template)
# ────────────────────────────────────────────────────────────────────────────

def _embedder_source():
    path = os.path.join(os.path.dirname(__file__), "../../src/re_embedder/embedder.py")
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def test_natural_language_upsert_uses_coalesce_not_bare_excluded():
    """Both rel_types UPSERT sites must COALESCE natural_language so a NULL/blank
    EXCLUDED value falls back to the existing template — never a bare overwrite."""
    src = _embedder_source()
    # The bare-overwrite form must be GONE.
    assert "natural_language = EXCLUDED.natural_language," not in src, (
        "a bare `natural_language = EXCLUDED.natural_language` UPSERT survives — "
        "this is the clobber bug; it must COALESCE to the existing template"
    )
    # The protective COALESCE form must be present (at least one site, ideally both).
    assert (
        "natural_language = COALESCE(NULLIF(btrim(EXCLUDED.natural_language), ''), "
        "rel_types.natural_language)"
    ) in src


# ────────────────────────────────────────────────────────────────────────────
# FIX #1 — existing curated rel is SKIPPED from approval/regeneration
# ────────────────────────────────────────────────────────────────────────────

def test_fix1_curated_skip_present_in_source():
    """The approval loop must short-circuit (continue) for a candidate that is an
    EXISTING curated rel, gated on curated-set membership — NOT a rel-name literal."""
    src = _embedder_source()
    assert "candidate_rel in curated_rels" in src, "FIX #1 curated-skip gate missing"
    assert "ontology_existing_rel_skipped" in src, "FIX #1 skip log line missing"
    # Metadata-driven curated detection (source membership OR non-blank template),
    # never an enumerated rel-name list.
    assert "WHERE source IN ('wikidata', 'builtin', 'user', 'seed')" in src


def test_fix1_metadata_driven_no_relname_literals():
    """Subject-agnostic guard: the curated-skip must not enumerate specific rel names
    (no `candidate_rel == 'participated_in'`-style literals introduced)."""
    src = _embedder_source()
    for forbidden in ("== 'participated_in'", '== "participated_in"',
                      "== 'owns'", "in ('owns'", "in ['owns'"):
        assert forbidden not in src, f"rel-name literal {forbidden!r} leaked into the skip logic"
