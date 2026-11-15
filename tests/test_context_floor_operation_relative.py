"""TRUTHLIE defect 2 — nothing is refused that had room to succeed.

WHY THIS FILE EXISTS
────────────────────
The context guard's soft-overrun branch clamps a call's completion budget to the room
left in the window, but only when that room clears a GLOBAL minimum-completion floor
(64 tokens). Three real operations declare budgets SMALLER than that floor —
INTENT_ADJUDICATION 16, INTENT_PRECLASSIFY 32, OCCURRENCE_CLASSIFY 48 — and reasoning
headroom (+1024) is added to the budget BEFORE the window check, so near the edge a
16-token classification arrived asking for 1040 with only ~63 tokens of room: the soft
branch fired, 63 failed the 64 floor, and the call was REFUSED with a CRIT even though
the operation needed 16 and had several times that. Unpadded, it fit and would have
succeeded.

The fix caps the floor by the operation's OWN declared budget
(_operation_completion_floor), resolved from the single source of truth
(llm_calls.LLMMaxTokens) so a future small operation is covered the day its budget
entry lands. These tests pin: the defect itself, the intact truncation guard for large
operations, the fail-safes, and the genuinely-too-small refusal.
"""
import pytest

from src.api import context_window as cw


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("LLM_CONTEXT_GUARD", "LLM_CONTEXT_WINDOW", "LLM_CONTEXT_WINDOW_DEFAULT",
              "LLM_CONTEXT_PROBE", "LLM_CONTEXT_MIN_COMPLETION_TOKENS",
              "LLM_MAX_TOKENS_INTENT_ADJUDICATION", "LLM_MAX_TOKENS_OCCURRENCE_CLASSIFY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_CONTEXT_PROBE", "false")   # never touch the network in tests
    cw.reset_state()
    cw.reset_counters()
    yield
    cw.reset_state()
    cw.reset_counters()


def _at_edge(monkeypatch, window, room, max_tokens, operation):
    """A plan whose prompt leaves EXACTLY the given room (the estimator is pinned
    aside — the branch logic under test is plan_call's, not the estimator's)."""
    monkeypatch.setenv("LLM_CONTEXT_WINDOW", str(window))
    monkeypatch.setattr(cw, "estimate_tokens", lambda msgs: window - room)
    return cw.plan_call([{"role": "user", "content": "x"}], max_tokens=max_tokens,
                        operation=operation)


# ── THE DEFECT: a small padded operation near the edge ────────────────────────

def test_small_padded_operation_with_room_beyond_its_budget_fits(monkeypatch):
    """THE LIE. INTENT_ADJUDICATION needs 16; reasoning headroom pads it to 1040; 63
    tokens of room remain. 63 < 1040 trips the soft branch, and 63 once failed the
    global 64 floor — a hard CRIT refusal of a call that had four times what it needed.
    Now the floor is capped at the operation's own budget: it must CLAMP to the room
    and send."""
    plan = _at_edge(monkeypatch, window=8192, room=63, max_tokens=1040,
                    operation="INTENT_ADJUDICATION")
    assert plan.fits is True, (
        "an operation whose declared budget (16) fits in the room (63) must not be "
        "refused — the floor may never exceed the operation's entire budget")
    assert plan.clamped is True
    assert plan.effective_max_tokens == 63
    assert plan.soft_overrun is True


def test_occurrence_classify_the_largest_tiny_op_also_fits(monkeypatch):
    """48-token budget padded to 1072 with 63 room — same defect, next size up."""
    plan = _at_edge(monkeypatch, window=8192, room=63, max_tokens=1072,
                    operation="OCCURRENCE_CLASSIFY")
    assert plan.fits is True and plan.clamped is True


def test_unpadded_small_operation_that_genuinely_cannot_fit_is_refused(monkeypatch):
    """The refusal that is CORRECT: room below the operation's own declared budget
    cannot hold its answer. 16-token op with 10 tokens of room must not be sent."""
    plan = _at_edge(monkeypatch, window=8192, room=10, max_tokens=16,
                    operation="INTENT_ADJUDICATION")
    assert plan.fits is False and plan.soft_overrun is True


# ── THE TRUNCATION GUARD IS INTACT for large operations ───────────────────────

def test_large_operation_still_refused_when_room_is_below_the_global_floor(monkeypatch):
    """EXTRACT (2048, padded 3072) with 63 room: a 63-token extraction JSON is
    unparseable — exactly the silent-degrade the guard exists to kill. The cap must not
    have weakened it: the floor for a large op stays the global 64."""
    plan = _at_edge(monkeypatch, window=8192, room=63, max_tokens=3072,
                    operation="EXTRACT")
    assert plan.fits is False, "a truncated dense-extraction JSON is not worth sending"
    assert plan.soft_overrun is True


def test_large_operation_clamps_when_room_clears_the_global_floor(monkeypatch):
    plan = _at_edge(monkeypatch, window=8192, room=200, max_tokens=3072,
                    operation="EXTRACT")
    assert plan.fits is True and plan.clamped is True and plan.effective_max_tokens == 200


# ── The floor resolver itself ─────────────────────────────────────────────────

def test_floor_is_capped_by_each_operations_declared_budget():
    assert cw._operation_completion_floor("INTENT_ADJUDICATION") == 16
    assert cw._operation_completion_floor("INTENT_PRECLASSIFY") == 32
    assert cw._operation_completion_floor("OCCURRENCE_CLASSIFY") == 48


def test_floor_is_the_global_minimum_for_large_operations():
    assert cw._operation_completion_floor("EXTRACT") == 64
    assert cw._operation_completion_floor("DEFAULT") == 64


def test_floor_respects_env_overrides(monkeypatch):
    """A raised budget lets the global floor govern again — the cap follows the
    OPERATOR's declared need, not a hardcoded list of small operation names."""
    monkeypatch.setenv("LLM_MAX_TOKENS_INTENT_ADJUDICATION", "200")
    assert cw._operation_completion_floor("INTENT_ADJUDICATION") == 64
    monkeypatch.setenv("LLM_MAX_TOKENS_INTENT_ADJUDICATION", "8")
    assert cw._operation_completion_floor("INTENT_ADJUDICATION") == 8


def test_floor_falls_back_to_global_when_the_budget_table_is_unreadable(monkeypatch):
    """Fail-safe: if the single-source budget table cannot be imported, the global
    floor stands — exactly today's behaviour, never a zero floor."""
    import builtins
    real_import = builtins.__import__

    def _broken(name, *a, **k):
        if name == "src.api.llm_calls":
            raise ImportError("simulated unreadable budget table")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _broken)
    assert cw._operation_completion_floor("INTENT_ADJUDICATION") == 64


def test_future_small_operation_is_covered_by_its_budget_entry(monkeypatch):
    """OPERATIONS THAT DO NOT EXIST YET: an env-declared budget below the floor covers
    a brand-new operation name with no code change here — the resolver reads the table,
    it does not enumerate small operations."""
    monkeypatch.setenv("LLM_MAX_TOKENS_SOME_FUTURE_TINY_OP", "12")
    assert cw._operation_completion_floor("SOME_FUTURE_TINY_OP") == 12


# ── The clamp happens on the wire seam llm_calls actually calls ────────────────

def test_the_preflight_seam_returns_the_clamped_budget(monkeypatch):
    """The consumer-side half, through the same entry llm_calls._context_preflight
    uses: a fitted-but-clamped plan reports the ROOM as the effective budget, so the
    call goes out sized to what the window can hold."""
    plan = _at_edge(monkeypatch, window=8192, room=63, max_tokens=1040,
                    operation="INTENT_ADJUDICATION")
    assert plan.fits is True
    assert plan.effective_max_tokens == 63
    assert plan.detail.get("completion_floor") == 16
