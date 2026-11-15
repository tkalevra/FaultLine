"""Unit tests for PART 1 — INGEST SPINE (DEV/DESIGN-ingest-spine-and-temporal-recall.md).

PURE tests — no DB, no network, no GLiNER2, no LLM. They exercise the deterministic seams:
  * the relation-fit guardrail (junk fix: gets_my/has_my rejected BY CONSTRUCTION),
  * the swappable decomposer (buried/triggerless aside surfaces; subject-agnostic),
  * the held-blob removal (residue that can't ground is dropped — no store_context fallback).

The DB-backed RUNG 2/3 of resolve_canonical is unreachable here (no POSTGRES_DSN); resolution
falls to RUNG 1 morphology only, which is exactly the path that distinguishes a clean novel
growth predicate from junk. We assert on that deterministic core.
"""
import importlib
import os

import pytest

# Ensure no live DSN leaks in from the environment so RUNG 2/3 is a clean miss (RUNG-1 only).
os.environ.pop("POSTGRES_DSN", None)

from src.extraction import relation_fit
from src.extraction import decompose


@pytest.fixture(scope="module", autouse=True)
def _rung1_only_caches():
    """RUNG 2/3 must be a clean miss for this module — not just "no DSN now", but no DB-loaded
    canonical/alias cache either. src.ontology.canonical caches per SCHEMA (not per DSN), so
    whichever earlier module resolved with a DSN left the public alias map cached and
    is_relation_fit('has') read True here (whole-suite red, green alone - #44)."""
    from src.ontology import canonical
    canonical.reset_caches()
    yield
    canonical.reset_caches()


# ── Relation-fit guardrail (Part 1, item 3) ────────────────────────────────────

@pytest.mark.parametrize("predicate", ["gets_my", "has_my", "gives_their", "had_my", "be", "is", "has"])
def test_junk_predicates_rejected_by_construction(predicate):
    """gets_my/has_my (light verb + bare possessive) and bare copulas/auxiliaries are junk:
    no content object → rejected BY CONSTRUCTION (the LongMemEval Q1 junk-relation fix)."""
    assert relation_fit.is_relation_fit(predicate) is False


@pytest.mark.parametrize("predicate", ["fix", "repair", "lives_in", "attended_workshop", "fixed"])
def test_clean_novel_predicates_pass(predicate):
    """A clean, content-bearing verb predicate (the verb-lift growth shape) passes relation-fit —
    it is eligible to GROW via the source=mcp/user_stated /ingest path. lives_in keeps its
    load-bearing preposition (not residue)."""
    assert relation_fit.is_relation_fit(predicate) is True


def test_empty_predicate_rejected():
    assert relation_fit.is_relation_fit("") is False
    assert relation_fit.is_relation_fit("   ") is False


def test_filter_edges_drops_junk_from_any_extractor():
    """The guardrail enforces on ALL candidates regardless of which extractor proposed them —
    including a junk predicate that came from /extract/rewrite's LLM output."""
    edges = [
        {"subject": "user", "rel_type": "gets_my", "object": "discount"},      # LLM junk
        {"subject": "user", "rel_type": "has_my", "object": "car"},            # LLM junk
        {"subject": "user", "rel_type": "fix", "object": "fence"},             # clean growth
        {"subject": "user", "rel_type": "lives_in", "object": "elmstead"},       # clean known
    ]
    kept = relation_fit.filter_edges(edges)
    kept_rels = {e["rel_type"] for e in kept}
    assert "gets_my" not in kept_rels
    assert "has_my" not in kept_rels
    assert "fix" in kept_rels
    assert "lives_in" in kept_rels


def test_guardrail_is_subject_agnostic():
    """No word-list / domain matching: the SAME junk shape is rejected for any domain noun, and
    the SAME clean shape passes for any domain verb. Drive off morphology, not a dictionary."""
    # Junk shape across unrelated domains:
    for obj_pron in ("gets_my", "has_my", "gives_their"):
        assert relation_fit.is_relation_fit(obj_pron) is False
    # Clean shape across unrelated domains (networking, biology, identity, activity):
    for verb in ("administers", "feeds", "renamed", "vaccinated"):
        assert relation_fit.is_relation_fit(verb) is True


def test_guardrail_fail_open_kill_switch(monkeypatch):
    """Kill-switch (default ON): when RELATION_FIT_GUARDRAIL is off, the guardrail is a
    pass-through (today's no-filter behavior) — a single flip, fail-safe."""
    monkeypatch.setattr(relation_fit, "RELATION_FIT_GUARDRAIL", False)
    assert relation_fit.is_relation_fit("gets_my") is True
    edges = [{"subject": "user", "rel_type": "gets_my", "object": "x"}]
    assert relation_fit.filter_edges(edges) == edges


# ── Decomposer (Part 1, item 1) ─────────────────────────────────────────────────

def test_buried_triggerless_aside_surfaces():
    """The LongMemEval Q1 buried clause carries no trigger signal but MUST still surface as its
    own clause (sentence-split fallback) so the guardrailed builder sees it."""
    turn = ("Can you help me plan a road trip this weekend? "
            "By the way, I had an issue with my car's GPS system.")
    clauses = decompose.decompose(turn)
    assert any("gps" in c.lower() for c in clauses), clauses
    # And the buried clause is ISOLATED from the question (clause-level input for GLiNER).
    assert any("gps" in c.lower() and "?" not in c for c in clauses), clauses


def test_buried_clause_forms_clean_predicate_shape():
    """The buried GPS clause's verb phrase ("had an issue with") normalizes to a clean,
    relation-fit-passing predicate (has_issue) — it builds a triple, it is not residue."""
    from src.ontology.canonical import normalize_rel
    rel = normalize_rel("had an issue with")
    assert rel and rel != ""
    assert relation_fit.is_relation_fit(rel) is True  # has_issue is content-bearing, passes


def test_decompose_intent_independent():
    """Decomposition is INTENT-independent: a QUERY-shaped turn (a question) still yields its
    fact-bearing clause. The decomposer never inspects intent — it splits and unions."""
    turn = "Where should I eat tonight? Also, I just adopted a dog named Biscuit."
    clauses = decompose.decompose(turn)
    assert any("biscuit" in c.lower() for c in clauses), clauses


def test_decompose_subject_agnostic_no_wordlist():
    """No keyword/word-list: an arbitrary, never-seen domain sentence is still split into its
    sentences. The buried clause surfaces regardless of vocabulary."""
    turn = ("What is the capital of France? "
            "Incidentally, my quokka enjoys eucalyptus on Tuesdays.")
    clauses = decompose.decompose(turn)
    assert any("quokka" in c.lower() for c in clauses), clauses


def test_decompose_fail_safe_whole_turn():
    """Fail-safe: an unknown strategy degrades to the whole turn as one clause (builder always
    gets input) — it never raises or returns empty for non-empty text."""
    clauses = decompose.decompose("I own three goats.", strategy="does-not-exist")
    assert clauses == ["I own three goats."]


def test_register_decomposer_seam():
    """The seam is swappable: a registered strategy is selectable by name (the DEFERRED
    chain-back/anchor-sort optimizations plug in here later with no call-site change)."""
    decompose.register_decomposer("upper_test", lambda t: [t.upper()])
    out = decompose.decompose("hello world", strategy="upper_test")
    assert out == ["HELLO WORLD"]


def test_empty_turn_yields_no_clauses():
    assert decompose.decompose("") == []
    assert decompose.decompose("   ") == []


# ── Held-blob removal (Part 1, item 4) ──────────────────────────────────────────

def test_store_context_residue_fallback_removed():
    """The Class-C store_context residue fallback is REMOVED from remember_facts_tool — residue
    that can't build a valid triple is DROPPED (no un-walkable held-blob island). Assert the
    source no longer routes the no-edges path to store_context_tool."""
    import inspect
    from src.mcp import server
    # NOTE: the public `remember_facts_tool` is now a thin wrapper (it attaches the experience
    # push lane at ONE seam covering every terminal return); the pipeline itself lives in
    # `_remember_facts_tool_impl`. Inspect the IMPL — inspecting the wrapper would make this
    # test pass vacuously, which is worse than failing.
    src = inspect.getsource(server._remember_facts_tool_impl)
    # The no-edges tail must NOT call the store_context fallback any more.
    assert "store_context_tool(text=text" not in src, "held-blob fallback still present"
    # And it must explicitly drop residue.
    assert "residue_dropped" in src
    assert 'status": "no_ingest"' in src or "'status': 'no_ingest'" in src


# ── Relation-fit RUNG 1: function-word predicate rejection (junk re-grow fix) ────

def test_relation_fit_rejects_function_word_predicates():
    """Live Q1 cleaned-run regression: function-word predicates (conjunctions/subordinators/
    bare prepositions/degree-adverbs) re-grew as junk rels because RUNG 1 only blocked
    possessive/auxiliary segments. They are grammar, not relations — reject standalone."""
    from src.extraction.relation_fit import is_relation_fit
    for junk in ("but", "when", "while", "near", "along", "too", "especially",
                 "through", "and", "or", "because", "then", "also"):
        assert is_relation_fit(junk) is False, f"{junk!r} should be rejected as a function word"


def test_relation_fit_keeps_real_predicates_and_verbs():
    """Must NOT over-filter: real multi-token rels, novel verbs, and possessive-suffixed
    content predicates still pass (verbs like earn/fix are NOT function words)."""
    from src.extraction.relation_fit import is_relation_fit
    for ok in ("has_issue", "lives_in", "feature_of", "earn", "fix", "subclass_of", "born_on"):
        assert is_relation_fit(ok) is True, f"{ok!r} should pass"
    # possessive-object junk still rejected (existing behavior preserved)
    assert is_relation_fit("has_my") is False
    assert is_relation_fit("gets_my") is False
