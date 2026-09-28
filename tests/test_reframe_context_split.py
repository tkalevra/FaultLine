"""CTXOVERRUN — the atomizer must SPLIT or FAIL LOUD, never degrade silently.

The failure being closed: an over-window REFRAME request came back as an ordinary error →
``reframe.llm_failed`` → ``used_llm=False`` → the caller fell back to ``segment_clauses``.
The model never saw the turn, nobody was told, and the benchmark scored it as a capture
miss indistinguishable from a genuine engine failure.
"""

import asyncio
import os

import pytest

from src.api import context_window as cw
from src.extraction import reframe


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("LLM_CONTEXT_PROBE", "false")
    # LLMModels.get() fails LOUD on an unset model var (by design — model identity is
    # config, never guessed), so the fixture must supply one or the reframe try-block
    # swallows the RuntimeError before the patched LLM is ever reached.
    monkeypatch.setenv("WGM_LLM_MODEL", "test-model")
    monkeypatch.setenv("REFRAME_ENABLED", "true")
    monkeypatch.setenv("REFRAME_CONTEXT_SPLIT", "true")
    monkeypatch.setenv("REFRAME_SACRED_COVERAGE", "false")   # guard tested elsewhere
    monkeypatch.setenv("LLM_CONTEXT_WINDOW", "8192")
    cw.reset_state()
    cw.reset_counters()
    yield
    cw.reset_state()
    cw.reset_counters()


_OVERRUN = {"error": "context_overrun",
            "context_overrun": {"stage": "preflight", "prompt_tokens_est": 9575,
                                "window": 8192, "window_source": "server_rejection"}}


def _fake_llm(seen, window=None):
    """A FAITHFUL brain simulation: refuse exactly when the WHOLE request (system prompt +
    turn) would not fit the window — which is what llama.cpp does — otherwise atomize each
    sentence. The refusal threshold is therefore NOT the same code as the split budget;
    the split has to derive a fitting size on its own."""
    async def _call(messages=None, **kwargs):
        seen.append(messages[-1]["content"])
        limit = int(window or os.getenv("LLM_CONTEXT_WINDOW", "8192"))
        if cw.estimate_tokens(messages) >= limit:
            return dict(_OVERRUN)
        content = messages[-1]["content"]
        atoms = [{"statement": s.strip() + ".", "source": s.strip()}
                 for s in content.split(".") if s.strip()]
        return {"atoms": atoms}
    return _call


def test_over_window_turn_splits_instead_of_silently_falling_back(monkeypatch):
    # A window small enough that the 7,998-char REFRAME system prompt plus a long turn no
    # longer fits. (Worth stating plainly: against a real 8,192 window that same system
    # prompt leaves ~17k characters of headroom, so only a genuinely huge turn overruns —
    # the REFRAME lane is not the common offender. The EXTRACT preamble is.)
    monkeypatch.setenv("LLM_CONTEXT_WINDOW", "3200")
    seen: list[str] = []
    monkeypatch.setattr("src.api.llm_calls.call_llm_with_retry_async", _fake_llm(seen))
    turn = " ".join(f"Fact number {i} happened in city {i}." for i in range(120))

    res = asyncio.run(reframe.reframe_to_atomic(turn, user_id="u1"))

    assert res.atoms, "an over-window turn produced NO atoms — this is the silent degrade"
    assert res.used_llm is True
    assert len(seen) > 2, "the turn was never re-sent in windows"
    budget = reframe._content_budget_chars()
    assert all(len(s) <= budget for s in seen[1:]), "a window still exceeded the budget"
    # every fact survived the split — no capture lost to the window
    joined = " ".join(a.text for a in res.atoms)
    for i in (0, 59, 119):
        assert f"Fact number {i} " in joined
    assert cw.counters().get(cw.EV_CALLER_SPLIT) == 1


def test_unsplittable_over_window_fails_loud_and_is_counted(monkeypatch):
    """One sentence that alone exceeds the window cannot be split without mutilating the
    user's words. That is a genuine dead end — it must be LOUD, not silent."""
    crits: list[tuple] = []
    monkeypatch.setenv("LLM_CONTEXT_WINDOW", "3200")
    monkeypatch.setattr("src.api.llm_calls.call_llm_with_retry_async", _fake_llm([]))
    import src.api.logging_config as lc
    monkeypatch.setattr(lc, "log_crit", lambda *a, **k: crits.append((a, k)))

    res = asyncio.run(reframe.reframe_to_atomic("A " * 4000 + "single enormous sentence",
                                                user_id="u1"))
    assert res.atoms == [] and res.used_llm is False
    assert crits, "an unrecoverable capture loss was not logged loudly"
    assert crits[0][0][1] == "reframe.context_overrun_unsplittable"
    assert crits[0][1]["window"] == 8192  # the window the OVERRUN verdict carried


def test_split_is_only_attempted_on_a_real_overrun(monkeypatch):
    """A timeout must keep today's fail-open-fast behaviour, not fan out into N more calls
    against an endpoint that is already struggling."""
    calls = []

    async def _boom(messages=None, **kwargs):
        calls.append(1)
        raise TimeoutError("read timeout")

    monkeypatch.setattr("src.api.llm_calls.call_llm_with_retry_async", _boom)
    res = asyncio.run(reframe.reframe_to_atomic("One. Two. Three. Four.", user_id="u1"))
    assert res.atoms == [] and res.used_llm is False
    assert len(calls) == 1, "a generic failure must not trigger the split fan-out"


def test_split_flag_off_reproduces_the_legacy_silent_fallback(monkeypatch):
    monkeypatch.setenv("REFRAME_CONTEXT_SPLIT", "false")
    monkeypatch.setenv("LLM_CONTEXT_WINDOW", "3200")
    seen: list[str] = []
    monkeypatch.setattr("src.api.llm_calls.call_llm_with_retry_async", _fake_llm(seen))
    turn = " ".join(f"Fact number {i} happened in city {i}." for i in range(120))
    res = asyncio.run(reframe.reframe_to_atomic(turn, user_id="u1"))
    assert res.atoms == [] and len(seen) == 1


def test_window_unknown_means_no_split_attempt(monkeypatch):
    """Nothing to split TO. Must not invent a budget."""
    monkeypatch.delenv("LLM_CONTEXT_WINDOW", raising=False)
    cw.reset_state()
    assert reframe._content_budget_chars() is None
    seen: list[str] = []
    monkeypatch.setattr("src.api.llm_calls.call_llm_with_retry_async",
                        _fake_llm(seen, window=10))
    res = asyncio.run(reframe.reframe_to_atomic("One fact. Two facts.", user_id="u1"))
    assert res.atoms == [] and len(seen) == 1


def test_window_packing_never_cuts_a_sentence():
    windows = reframe._split_into_windows("Alpha one. Beta two. Gamma three. Delta four.", 24)
    assert windows and len(windows) > 1
    rejoined = " ".join(windows)
    for s in ("Alpha one.", "Beta two.", "Gamma three.", "Delta four."):
        assert s in rejoined
    assert reframe._split_into_windows("One enormous unsplittable sentence.", 5) is None
