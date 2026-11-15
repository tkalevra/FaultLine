"""The clamp path of the context guard must SURVIVE being taken.

`_context_preflight` has two non-trivial outcomes. The BLOCK outcome (prompt does not
fit) was exercised by tests. The CLAMP outcome -- prompt fits, completion budget does
not, so the budget is trimmed and the call proceeds -- is the GRACEFUL half, and it
crashed:

    TypeError: meth() got multiple values for keyword argument 'effective_max_tokens'

`plan.as_log()` carries `effective_max_tokens`, and the log call ALSO passed it
explicitly, so structlog's bound method received it twice. Every clamped call raised
instead of degrading. It surfaced as a brain-probe failure on a real tenant.

WHY THIS WAS NOT CAUGHT: no test ever drove the clamp branch with a REAL logger. A
test that mocks the logger cannot see this class of defect at all -- the duplicate
keyword is resolved by Python at the call site, so the crash only exists when the
actual bound method is invoked. These tests therefore deliberately use the module's
own live `log` object and assert on the RETURN, never on a recorded call.
"""
import pytest

from src.api import llm_calls
from src.api import context_window


def _clamping_plan(window=1000, est=900, requested=500, effective=100):
    """A plan in exactly the state the clamp branch handles: prompt fits, budget trimmed."""
    plan = context_window.ContextPlan(
        window=window, source="test", prompt_tokens_est=est,
        max_tokens=requested, effective_max_tokens=effective,
    )
    plan.fits = True
    plan.clamped = True
    plan.soft_overrun = True
    return plan


def test_the_clamp_path_does_not_raise_when_it_is_actually_taken(monkeypatch):
    """The regression itself: taking the clamp branch must return, not raise."""
    plan = _clamping_plan()
    monkeypatch.setattr(context_window, "guard_enabled", lambda: True)
    monkeypatch.setattr(context_window, "plan_call", lambda *a, **k: plan)

    effective, block = llm_calls._context_preflight(
        [{"role": "user", "content": "x"}], 500, "INTENT_ADJUDICATION", "user-1234", "sync")

    assert block is None, "a clamped call is sendable — it must not be blocked"
    assert effective == 100, (
        "the clamp branch must return the TRIMMED budget so the answer is not truncated; "
        f"got {effective!r}")


def test_no_key_is_spelled_both_explicitly_and_through_the_as_log_splat():
    """Pins the CONTRACT, not the one key that happened to break.

    The defect was a name appearing both as an explicit keyword and inside the
    `**plan.as_log()` splat. Any future key added to as_log() re-creates the crash the
    moment it is also passed explicitly, so this reads the real source and compares the
    two sets rather than asserting on 'effective_max_tokens' by name.
    """
    import inspect
    import re

    src = inspect.getsource(llm_calls._context_preflight)
    as_log_keys = set(context_window.ContextPlan(window=1, source="s", prompt_tokens_est=1,
                                                 max_tokens=1, effective_max_tokens=1).as_log())

    # Every explicit `name=` keyword used in a call that also splats as_log().
    for call in re.findall(r"log[_a-z]*\((?:[^()]|\([^()]*\))*?\*\*plan\.as_log\(\)", src):
        explicit = set(re.findall(r"(?:^|[\s(])([a-z_]+)=", call))
        clash = explicit & as_log_keys
        assert not clash, (
            f"{sorted(clash)} passed BOTH explicitly and via **plan.as_log() — structlog "
            f"raises TypeError('got multiple values for keyword argument') and the call "
            f"crashes instead of logging. Spell each as_log key exactly once.")


@pytest.mark.parametrize("operation", ["INTENT_ADJUDICATION", "EXTRACT", "DEFAULT"])
def test_the_clamp_path_survives_for_every_operation_size(monkeypatch, operation):
    """The crash was operation-independent; so is the guard against it."""
    monkeypatch.setattr(context_window, "guard_enabled", lambda: True)
    monkeypatch.setattr(context_window, "plan_call", lambda *a, **k: _clamping_plan())

    effective, block = llm_calls._context_preflight(
        [{"role": "user", "content": "x"}], 500, operation, "user-1234", "async")

    assert block is None and effective == 100
