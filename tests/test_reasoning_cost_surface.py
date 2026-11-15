"""dprompt-156: the measured reasoning cost must reach ORDINARY console output, not stop at
the API layer.

The probe (``_probe_extraction``) has returned ``reasoning_cost`` since the first commit of
this lane, but NO renderer showed it: ``testBrain`` drew triples + calibration, the tenant
``seatBrainResult`` drew the same, and the recalibrate wrapper DROPPED the key entirely. A
number that never reaches a human's eyes is exactly the "visible only to someone grepping
token counts" failure this lane exists to close.
"""
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def _js(page: str) -> str:
    return (_REPO / "saas" / "dashboard" / page).read_text(encoding="utf-8")


def test_user_console_test_brain_renders_reasoning_cost():
    js = _js("user.js")
    assert "reasoning_cost" in js, "testBrain must read the measured reasoning spend"
    assert "were reasoning" in js, "the message must name what the tokens were"
    assert "cannot turn reasoning off" in js


def test_tenant_console_seat_result_renders_reasoning_cost():
    js = _js("tenant.js")
    assert "seatBrainResult" in js
    # the seat card path reads probe.reasoning_cost and names the standing cost
    m = re.search(r"function seatBrainResult[\s\S]{0,1200}", js)
    assert m and "reasoning_cost" in m.group(0)
    assert "cannot be turned off" in m.group(0)
    # the tenant-level testLlm path renders it too
    t = re.search(r"async function testLlm\(\)[\s\S]{0,900}", js)
    assert t and "reasoning_cost" in t.group(0)


def test_recalibrate_wrapper_passes_reasoning_cost_through():
    """The /me/brain-health/recalibrate probe wrapper dropped the key (found in self-review
    round 1) — the seat card rendered everything EXCEPT the cost."""
    src = (_REPO / "saas" / "user_console.py").read_text(encoding="utf-8")
    m = re.search(r'"probe": \{[\s\S]{0,400}', src)
    assert m and "reasoning_cost" in m.group(0), (
        "the probe wrapper must forward reasoning_cost or the console cannot render it")
