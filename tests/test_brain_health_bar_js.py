"""The Brain Health BAR renders the Redis-truth read-out a tenant actually reads.

Drives the real `saas/dashboard/brain-health.js` in jsdom (via
`tests/dashboard/render_brain_health.js`) with payloads shaped exactly like
`GET /tenant/brain-health` builds them, and asserts on the rendered words:

  • the ceiling + its source render AT the number (headroom row) — an assumed ceiling is
    labelled assumed and ships the disclaimer, never a fake-live one;
  • the old two-line footer ("N/min (live)" + "recent 429s: N") is GONE (owner-ordered
    2026-08-14) — its information now lives in ONE right-justified segment ON the
    scope-note line: the wiki link, the pace/detected-max fraction, and a folded pause
    indicator. ZERO added lines in any state is the contract, asserted per state;
  • a shared hard pause renders "rate-limited … resumes in Ns" (a paused brain has no
    headroom percentage to speak of — showing one would be a fabricated measurement);
  • absent data renders "not measured", never a fake number, and the segment hides
    entirely (the old footer's hide rule).

Skips (with the install command) when node/jsdom are unavailable.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_HARNESS = _ROOT / "tests" / "dashboard" / "render_brain_health.js"

node = shutil.which("node")
has_jsdom = (_ROOT / "tests" / "dashboard" / "node_modules" / "jsdom").exists()
pytestmark = pytest.mark.skipif(
    not node or not has_jsdom,
    reason="node + jsdom required — npm install --prefix tests/dashboard",
)


def _thr(**over):
    base = {
        "ceiling_rpm": None, "ceiling_source": "unknown", "ceiling_disclaimer": None,
        "ceiling_basis": None, "available_now": None, "burst_capacity": None,
        "saturation": None, "measured": False, "paused": False, "pause_remaining_s": None,
        "rate_429": 0, "pace_rpm": None, "detected_rpm": None, "detected_source": None,
    }
    base.update(over)
    return base


def _payload(name, thr, status_state="ok"):
    return {
        "__name": name,
        "cache": {"available": True, "hits": 10, "misses": 2, "hit_rate": 10 / 12,
                  "lookups": 12},
        "throughput": thr,
        "upkeep": {"available": True, "deferred": 0, "admitted": 40, "defer_rate": 0.0},
        "window_hours": 24,
        "observed": 40,
        "scope": {"kind": "account", "shared": True, "seats_sharing": 3, "seat_id": None,
                  "configured": True, "brain_key": "abc123def456", "per_seat_account": False},
        "status": {"state": status_state, "headline": "Keeping up comfortably.",
                   "detail": "Requests are being served well inside this brain's ceiling."},
    }


def _render(payloads):
    proc = subprocess.run(
        [node, str(_HARNESS), json.dumps(payloads)],
        capture_output=True, text=True, timeout=60,
        cwd=str(_ROOT / "tests" / "dashboard"),
    )
    assert proc.returncode == 0, f"harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_a_live_measured_ceiling_is_labelled_live_at_the_number():
    out = _render([_payload("live", _thr(ceiling_rpm=30, ceiling_source="live",
                                        measured=True, saturation=0.4,
                                        pace_rpm=30.0, detected_rpm=30.0,
                                        detected_source="live",
                                        available_now=1.2, burst_capacity=2.0))])
    r = out["live"]
    h = r["has"]
    assert not h["assumed_tag"], "a measured ceiling must not render as assumed"
    assert not h["rate_limited"]
    assert not h["foot_present"], "the footer is gone by design — no state may bring it back"
    # Its replacement: the segment on the scope-note line, carrying the fraction.
    assert h["seg_text"] and h["seg_frac_any"], r["text"]
    assert h["seg_in_scope_note"], "the segment must live ON the scope note, not add a line"
    assert r["note_count"] == 2, "scope note + status detail only — zero added lines"


def test_a_shared_pause_renders_ratelimited_with_the_resume_countdown():
    out = _render([_payload("paused", _thr(ceiling_rpm=30, ceiling_source="live",
                                           measured=True, saturation=1.0,
                                           paused=True, pause_remaining_s=41.0,
                                           pace_rpm=30.0, detected_rpm=30.0,
                                           detected_source="live", rate_429=3))])
    r = out["paused"]
    h = r["has"]
    assert h["rate_limited"], r["text"]
    assert h["resumes_in"], "the admin must see WHEN the window resets"
    assert "paused" in r["text"], "the paused state must stay visible"
    assert h["seg_in_scope_note"], "the pause indicator folds INTO the segment, not a line"
    assert r["note_count"] == 2, "a pause adds ZERO lines — same count as the live state"
    assert "100% free" not in r["text"], \
        "a paused brain has no headroom to speak of — never a fabricated percentage"


def test_an_assumed_ceiling_is_labelled_at_the_number_and_carries_the_disclaimer():
    out = _render([_payload("assumed", _thr(
        ceiling_rpm=15, ceiling_source="assumed", measured=True, saturation=0.2,
        pace_rpm=15.0, detected_rpm=15.0, detected_source="assumed",
        ceiling_disclaimer="Assumed limit: 15 requests/min. This is NOT a measurement — "
                           "we will replace this with a real measured value.",
        rate_429=1))])
    r = out["assumed"]
    h = r["has"]
    assert h["assumed_tag"], r["text"]
    assert h["disclaimer"], "the disclaimer must render verbatim"
    assert h["seg_text"], "an assumed ceiling still renders the segment (it is the detected max)"
    assert not h["foot_present"]
    assert r["note_count"] == 3, "scope + disclaimer + status detail — still no footer line"


def test_absent_data_renders_not_measured_never_a_fake_number():
    out = _render([_payload("cold", _thr())])
    h = out["cold"]["has"]
    assert h["not_measured"], out["cold"]["text"]
    assert not h["seg_text"] and not h["seg_frac_any"], \
        "measured:false → the whole segment hides, exactly as the old footer did"
    assert not h["foot_present"]
