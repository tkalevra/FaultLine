"""A BRAIN NON-ANSWER IS NOT A VERDICT — and a load-bearing preposition is not a typo.

Two defects of one family: a lane records a PERMANENT decision on evidence it does not have.

DEFECT 1 (src/re_embedder/embedder.py). ``call_llm_with_retry_sync`` returns the same python
shape for three different things — an answer, "answered nothing" (``{}``), and "never reached"
(``{}`` or an ``{"error": ...}`` envelope). ``converge_lifted_synonyms`` read only the verdict
VALUE, so a transient outage wrote ``re_embedder_decision='left_novel'``, which its own
skip-probe then honours with NO TTL and NO retry — the rel is excluded from convergence
forever. Its sibling ``evaluate_aspect_synonym_candidates`` had the identical bug writing
``left_unmapped``, which ``main._aspect_word_memoized_unmapped`` honours forever.

  MEASURED (read-only, all 22 production tenants, 2026-08-27):
    * ``llm_call.context_overrun_preflight operation=ENRICHMENT window=448`` — 198 events in
      72h. That path REFUSES BEFORE THE WIRE and returns a dict, so ``raise_on_unavailable``
      cannot express it.
    * 707 ``left_novel`` memos; 512 of them (9 of 22 tenants, 483 distinct rels) carry
      ``ans='' conf=0.00`` — the fingerprint of a call that never happened.

  STANDARD. RFC 9520, "Negative Caching of DNS Resolution Failures" (Kristoff, Wessels,
  Wallström, Dec 2023) separates "(2) a response indicating the requested data does not exist"
  from "(3) a non-response due to a resolution failure in which the resolver does not receive
  any useful information", rules that a type-2 answer "is not a condition for resolution
  failure", and bounds failure caching: "Resolution failures MUST NOT be cached for longer than
  5 minutes." We cached type-3 as type-2, forever.

DEFECT 2 (src/ontology/canonical.py). ``_KEEP_PREPOSITIONS`` is declared load-bearing
("lives_in ≠ lives_at") and RUNG 1 protects those tokens from being dropped — then
``spelling_variant_siblings`` let a ONE-CHARACTER edit collapse them (``in``↔``on``,
``at``↔``as``, ``of``↔``on``). Prepositions are a CLOSED class, so a single-edit neighbour is
another real preposition: a "real-word error" in the sense of Golding & Roth, *Applying Winnow
to Context-Sensitive Spelling Correction* (arXiv:cmp-lg/9607024, 1996) — "fixing spelling errors
that happen to result in valid words" — undecidable from the string, which is precisely what
edit distance (Damerau, CACM 7(3):171-176, 1964) measures.

  MEASURED (same read-only pass, 1,058 distinct rel_types): 187 distinct preposition-collapsing
  folds reachable in 22/22 tenants (``believe_on``→``believe_in``, ``approve_on``→``approve_of``,
  ``build_in``→``build_on``, ``cave_on``→``cave_in``). The ingest seam REWRITES ``edge.rel_type``
  and grows a permanent alias row on each, so every one is a persisting user-truth corruption.

BOTH ARMS. Every pin here runs against BOTH the fixed default and the legacy flag value, and
asserts the legacy arm still exhibits the defect — so a pin that would pass either way cannot
masquerade as proof.

RUN: python3 tools/fltest.py --bug MEMO --test tests/test_nonanswer_is_not_a_verdict.py
"""
import importlib
import os

import pytest

# ── The EXACT shapes llm_calls.py hands a caller. Not invented: (A) is rebuilt from the real
#    _context_preflight below; (B)/(C) are copied from the `return {}` / `return error_msg`
#    statements at llm_calls.py saas_no_brain_failclosed / empty_llm_content /
#    unparseable_llm_response / circuit_breaker_open.
_EMPTY = {}
_BREAKER = {"error": "circuit_breaker_open",
            "message": "Language model not responding. Wait 30s or restart backend."}


def _real_preflight_block():
    """Ask the REAL ``_context_preflight`` for the dict it returns when it refuses to dial.

    The pin must feed the value the pipeline actually emits, not one I wrote down. Skips if the
    guard cannot be driven here rather than asserting against a hand-made stand-in.
    """
    from src.api import llm_calls
    huge = [{"role": "user", "content": "word " * 20000}]
    try:
        _mt, block = llm_calls._context_preflight(huge, 500, "ENRICHMENT", "test", mode="sync_retry")
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"_context_preflight not drivable here: {exc}")
    if block is None:  # pragma: no cover - no window configured in this environment
        pytest.skip("no context window configured; preflight did not refuse")
    return block


@pytest.fixture(autouse=True)
def _model_env(monkeypatch):
    """LLMModels.get('ENRICHMENT') raises CRIT when unset, which would short-circuit the helper
    before the shape under test is ever reached (and make every arm look identical)."""
    monkeypatch.setenv("WGM_LLM_MODEL", "stub-model")


def _embedder(monkeypatch, memo_requires_answer: str):
    monkeypatch.setenv("ONTOLOGY_MEMO_REQUIRES_ANSWER", memo_requires_answer)
    import src.re_embedder.embedder as E
    return importlib.reload(E)


def _func_source(module, name: str) -> str:
    """Source of ONE top-level function, read from the file by AST.

    NOT ``inspect.getsource``: these modules are ``importlib.reload``-ed per arm, and after a
    reload inspect's line cache can hand back a stale byte range from a different part of the
    file — which is exactly how two of these pins first went red against a correct fix.
    """
    import ast
    import inspect as _i
    path = _i.getsourcefile(module)
    text = open(path).read()
    for node in ast.parse(text).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(text, node) or ""
    raise AssertionError(f"{name} not found in {path}")


def _canonical(monkeypatch, keep_preps: str):
    monkeypatch.setenv("SPELLING_FOLD_KEEP_PREPOSITIONS", keep_preps)
    import src.ontology.canonical as C
    return importlib.reload(C)


# ══════════════════════════════════════════════════════════════════════════════════════════
# DEFECT 1 — the synonym-convergence memo
# ══════════════════════════════════════════════════════════════════════════════════════════

_CANDIDATES = {"has_pet": {"nl": "person has pet", "is_symmetric": False}}


def _propose(E, monkeypatch, returned):
    monkeypatch.setattr(E, "call_llm_with_retry_sync", lambda **kw: returned)
    return E._llm_propose_equivalence("adopt", "", {"person"}, {"animal"}, _CANDIDATES, None)


@pytest.mark.parametrize("label,shape", [
    ("saas_no_brain / empty content / unparseable", _EMPTY),
    ("circuit_breaker_open envelope", _BREAKER),
])
def test_nonanswer_shapes_are_not_verdicts(monkeypatch, label, shape):
    """A brain that was never reached, or answered nothing, produces NO verdict to cache."""
    E = _embedder(monkeypatch, "true")
    _proposal, answered = _propose(E, monkeypatch, shape)
    assert answered is False, f"{label}: a non-answer must not be reported as an answer"


def test_context_overrun_preflight_is_not_a_verdict(monkeypatch):
    """The measured production case: the request was NOT SENT (198 events/72h, ENRICHMENT,
    window=448). ``raise_on_unavailable`` cannot cover it — the preflight returns a dict."""
    block = _real_preflight_block()
    assert block.get("error") == "context_overrun", "fixture drifted from the real preflight"
    E = _embedder(monkeypatch, "true")
    _proposal, answered = _propose(E, monkeypatch, block)
    assert answered is False, (
        "a request that was refused before the wire is not a verdict about the relation")


@pytest.mark.parametrize("verdict,conf", [(None, 0.95), ("has_pet", 0.9)])
def test_a_real_verdict_is_still_a_verdict(monkeypatch, verdict, conf):
    """The guard must NOT swallow a considered answer. ``{"equivalent_to": null}`` is the brain
    saying "not equivalent" — key PRESENT, value null — and that MUST still be memoized, or the
    fix would turn a one-time question into a per-cycle LLM bill."""
    E = _embedder(monkeypatch, "true")
    proposal, answered = _propose(E, monkeypatch,
                                  {"equivalent_to": verdict, "confidence": conf, "reason": "x"})
    assert answered is True
    assert proposal.get("equivalent_to") == verdict


def test_legacy_arm_still_memoizes_a_nonanswer(monkeypatch):
    """RED ARM. With ONTOLOGY_MEMO_REQUIRES_ANSWER=false the lane is byte-for-byte the shipped
    behaviour: every shape — including a call that never happened — reads as an answer, and the
    caller writes the permanent 'left_novel' memo. This is the defect, pinned so the two arms
    are measured rather than asserted."""
    E = _embedder(monkeypatch, "false")
    for shape in (_EMPTY, _BREAKER, _real_preflight_block()):
        _proposal, answered = _propose(E, monkeypatch, shape)
        assert answered is True, (
            "legacy arm must reproduce the defect: a non-answer indistinguishable from a verdict")


def test_llm_unavailable_is_not_a_verdict(monkeypatch):
    """breaker_open / rate_deferred / retries_exhausted now raise (raise_on_unavailable=True).
    The lane must convert that to 'no verdict', never to a cached decision."""
    from src.api.llm_lane import LLMUnavailable
    E = _embedder(monkeypatch, "true")

    def _boom(**kw):
        raise LLMUnavailable("retries_exhausted", "ENRICHMENT")

    monkeypatch.setattr(E, "call_llm_with_retry_sync", _boom)
    proposal, answered = E._llm_propose_equivalence(
        "adopt", "", {"person"}, {"animal"}, _CANDIDATES, None)
    assert (proposal, answered) == ({}, False)


def test_the_verdict_caching_kwarg_is_actually_passed(monkeypatch):
    """llm_calls.py's own docstring says raise_on_unavailable exists for callers that CACHE A
    VERDICT. Pin that this lane opts in — otherwise the breaker path silently returns an error
    dict again and only _brain_answered stands between it and a false memo."""
    E = _embedder(monkeypatch, "true")
    seen = {}

    def _spy(**kw):
        seen.update(kw)
        return {"equivalent_to": None, "confidence": 0.9}

    monkeypatch.setattr(E, "call_llm_with_retry_sync", _spy)
    E._llm_propose_equivalence("adopt", "", {"person"}, {"animal"}, _CANDIDATES, None)
    assert seen.get("raise_on_unavailable") is True


def test_unavailable_memo_value_is_not_honoured_by_the_skip_probe(monkeypatch):
    """The skip-probe matches ``re_embedder_decision = 'left_novel'`` EXACTLY. Whatever the
    lane records for a non-answer must therefore differ from that literal, or the door is shut
    just the same and the fix is cosmetic."""
    E = _embedder(monkeypatch, "true")
    assert E._MEMO_BRAIN_UNAVAILABLE != "left_novel"
    src = _func_source(E, "converge_lifted_synonyms")
    assert "re_embedder_decision = 'left_novel'" in src, "skip-probe literal moved; re-check"
    assert "_MEMO_BRAIN_UNAVAILABLE" in src


# ══════════════════════════════════════════════════════════════════════════════════════════
# DEFECT 1b — the ASPECT-synonym twin (same bug, different memo, different retry mechanism)
# ══════════════════════════════════════════════════════════════════════════════════════════

_ASPECT_CANDIDATES = [("height", "how tall someone is"), ("age", "years old")]


def _aspect(E, monkeypatch, returned):
    monkeypatch.setattr(E, "call_llm_with_retry_sync", lambda **kw: returned)
    return E._aspect_map_via_llm_async("tall", _ASPECT_CANDIDATES, None)


def test_aspect_async_twin_reports_called_ok(monkeypatch):
    """The INLINE twin main._aspect_map_via_llm has always returned a third element,
    ``called_ok``, and its caller branches on it. The ASYNC twin returned only two, so a brain
    outage wrote the permanent 'left_unmapped' memo the inline path refuses to write."""
    E = _embedder(monkeypatch, "true")
    for shape in (_EMPTY, _BREAKER, _real_preflight_block()):
        result = _aspect(E, monkeypatch, shape)
        assert len(result) == 3, "the async twin must carry called_ok, like its inline twin"
        assert result[2] is False, "a non-answer must report called_ok=False"


def test_aspect_async_twin_memoizes_a_real_none(monkeypatch):
    """Answered "none of them" is a verdict — memoize it, or the word is re-asked every sweep."""
    E = _embedder(monkeypatch, "true")
    attr, conf, called_ok = _aspect(E, monkeypatch, {"attribute": None, "confidence": 0.9})
    assert (attr, called_ok) == (None, True)
    assert conf == pytest.approx(0.9)


def test_aspect_async_twin_maps_a_real_hit(monkeypatch):
    E = _embedder(monkeypatch, "true")
    assert _aspect(E, monkeypatch,
                   {"attribute": "height", "confidence": 0.9}) == ("height", 0.9, True)


def test_aspect_legacy_arm_still_treats_a_nonanswer_as_an_answer(monkeypatch):
    """RED ARM for the aspect lane."""
    E = _embedder(monkeypatch, "false")
    for shape in (_EMPTY, _BREAKER):
        assert _aspect(E, monkeypatch, shape)[2] is True


def test_aspect_nonanswer_leaves_the_row_undecided(monkeypatch):
    """CRITICAL asymmetry with the synonym lane: this drain selects
    ``WHERE re_embedder_decision IS NULL``, so writing ANY non-NULL value — even a
    'retryable' one — would remove the row from its own queue. The non-answer branch must
    write NO decision at all."""
    import ast
    E = _embedder(monkeypatch, "true")
    tree = ast.parse(_func_source(E, "evaluate_aspect_synonym_candidates"))
    branch = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.If) and isinstance(node.test, ast.UnaryOp)
                and isinstance(node.test.op, ast.Not)
                and getattr(node.test.operand, "id", None) == "called_ok"):
            branch = node
    assert branch is not None, "the `if not called_ok:` non-answer branch is missing"
    # Structural, not textual: the branch's own EXPLANATION names the column, so a substring
    # search over the source would match a comment and fail a correct fix (it did).
    for sub in ast.walk(ast.Module(body=branch.body, type_ignores=[])):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            assert "re_embedder_decision" not in sub.value, (
                "the aspect non-answer branch must NOT write a decision — this lane's drain "
                "selects `WHERE re_embedder_decision IS NULL`, so NULL *is* its retry mechanism "
                "and any non-NULL value removes the row from its own queue")
        if isinstance(sub, ast.Call):
            fn = sub.func
            assert getattr(fn, "attr", None) != "execute", (
                "the aspect non-answer branch must issue no SQL at all")


# ══════════════════════════════════════════════════════════════════════════════════════════
# DEFECT 2 — the preposition collapse
# ══════════════════════════════════════════════════════════════════════════════════════════

# Every pair below is a real production rel or a real production near-miss (measured across the
# 22 live tenants), differing by ONE character in a token this module declares load-bearing.
_PREP_COLLAPSES = [
    ("stay_on", "stay_in"),        # the proven live hazard from the brief
    ("believe_on", "believe_in"),
    ("approve_on", "approve_of"),
    ("build_in", "build_on"),
    ("cave_on", "cave_in"),
    ("sit_on", "sit_in"),
    ("attend_as", "attend_at"),
]


@pytest.mark.parametrize("a,b", _PREP_COLLAPSES)
def test_a_preposition_is_never_a_spelling_variant(monkeypatch, a, b):
    C = _canonical(monkeypatch, "true")
    assert not C.spelling_variant_siblings(a, b), f"{a} and {b} are different relations"
    assert not C.spelling_variant_siblings(b, a), "the refusal must be symmetric"


@pytest.mark.parametrize("a,b", _PREP_COLLAPSES)
def test_legacy_arm_still_collapses_the_preposition(monkeypatch, a, b):
    """RED ARM. SPELLING_FOLD_KEEP_PREPOSITIONS=false is the shipped behaviour, and it folds
    every one of these — 187 such folds are reachable across 22/22 production tenants."""
    C = _canonical(monkeypatch, "false")
    assert C.spelling_variant_siblings(a, b), (
        "legacy arm must reproduce the defect, or this pin proves nothing")


def test_the_guarded_entry_point_refuses_the_live_hazard(monkeypatch):
    """End of the rail, not just the predicate: the ingest seam calls THIS, and on a hit it
    rewrites edge.rel_type and grows a permanent alias row."""
    live = {"stay_in", "believe_in", "approve_of", "build_on", "has_pet", "works_for"}
    for arm, expect_fold in (("true", False), ("false", True)):
        C = _canonical(monkeypatch, arm)
        C._load_reltypes = lambda dsn=None, schema=None: set(live)
        for surface, target in (("stay_on", "stay_in"), ("believe_on", "believe_in"),
                                ("approve_on", "approve_of"), ("build_in", "build_on")):
            got = C.resolve_grown_spelling_variant(surface)
            if expect_fold:
                assert got == target, f"legacy arm must reproduce the {surface} collapse"
            else:
                assert got is None, f"{surface} must not fold onto {target}"


@pytest.mark.parametrize("surface,live,target,why", [
    ("favourite_colour", {"favorite_colour", "likes"}, "favorite_colour", "Oxford colour/color"),
    ("favorite_colour", {"favourite_colour", "likes"}, "favourite_colour", "reverse dialect"),
    ("favourite_color", {"favorite_color"}, "favorite_color", "dialect, two tokens"),
    ("organise", {"organize"}, "organize", "-ise/-ize"),
    ("analyse", {"analyze"}, "analyze", "-yse/-yze"),
    ("recieve", {"receive"}, "receive", "adjacent transposition typo"),
    ("contribut_to", {"contribute_to"}, "contribute_to",
     "content token differs; the preposition is IDENTICAL on both sides — must still fold"),
])
def test_the_intended_folds_are_untouched(monkeypatch, surface, live, target, why):
    """A fix that kills the case the rail exists for is not a fix. Both arms must agree here:
    the guard fires ONLY when the ONE differing token is itself a preposition."""
    for arm in ("true", "false"):
        C = _canonical(monkeypatch, arm)
        C._load_reltypes = lambda dsn=None, schema=None: set(live)
        assert C.resolve_grown_spelling_variant(surface) == target, f"{why} (arm={arm})"


def test_oxford_pairs_from_the_shipped_suite_still_hold(monkeypatch):
    """The pins that shipped with the rail must not go red — the guard is additive."""
    C = _canonical(monkeypatch, "true")
    for a, b in [("favourite_colour", "favorite_colour"), ("favourite_color", "favorite_color"),
                 ("favourite_colour", "favourite_color"), ("organise", "organize"),
                 ("analyse", "analyze"), ("recieve", "receive")]:
        assert C.spelling_variant_siblings(a, b)
        assert C.spelling_variant_siblings(b, a)


def test_the_declared_invariant_is_the_one_enforced(monkeypatch):
    """The guard must read _KEEP_PREPOSITIONS itself, not a private copy that can drift from
    the set RUNG 1 actually protects."""
    C = _canonical(monkeypatch, "true")
    assert "_KEEP_PREPOSITIONS" in _func_source(C, "spelling_variant_siblings")
    for tok in ("in", "on", "at", "of", "to", "with"):
        assert tok in C._KEEP_PREPOSITIONS
