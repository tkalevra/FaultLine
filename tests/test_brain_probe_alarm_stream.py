"""The alarm stream: ok/fail/FLAPPING, last-write-wins, and the user-facing message.

WHAT THIS PINS, and why each one is a bug that has actually bitten:

1. A success CLEARS a prior failure with no TTL, decay job or reconciliation sweep. If only
   failures were recorded, the newest failure would be the newest record forever.
2. FLAPPING OUTRANKS the newest line. ok/fail/ok/fail/ok reads OK to `tail -1`, and showing a
   user green on an intermittent endpoint actively misleads them.
3. Transitions, not failures. fail/fail/fail is ONE incident (FAIL), not instability.
4. The message names WHAT timed out, HOW LONG we waited, WHICH host, WHAT to do, and carries a
   help link — and it NEVER leaks a credential from a URL.
5. Silence when healthy: a working seat produces no notice at all.

No database and no SaaS control plane: `classify_probe_state`, `count_transitions`,
`endpoint_host` and the renderers are pure. The DB-backed half is covered by the live probe in
tests/test_brain_probe_live.py.
"""

import importlib
import os

import pytest

# ⚠️ 2026-08-01: this used to be a bare `os.environ.setdefault("SAAS_MODE", "1")` at module
# scope — a PROCESS-WIDE env write that pytest never undoes. Every SaaS-sensitive test file
# collected AFTER this one then ran with SAAS_MODE on and produced FALSE REDS in multi-file
# runs (measured: test_saas_seat_brain_isolation::test_saas_off_resolve_brain_and_bind_are_noops
# and test_llm_reasoning_suppression::test_payload_carries_suppression_and_not_the_vllm_field
# both fail in a combined run and pass alone). The functions under test here are PURE (see the
# docstring) and `saas.brain_health` imports fine without the flag, so the flag is set only
# across the import and then restored exactly as it was found.
_prev_saas_mode = os.environ.get("SAAS_MODE")
os.environ["SAAS_MODE"] = _prev_saas_mode or "1"
try:
    bh = importlib.import_module("saas.brain_health")
finally:
    if _prev_saas_mode is None:
        os.environ.pop("SAAS_MODE", None)
    else:
        os.environ["SAAS_MODE"] = _prev_saas_mode

OK, FAIL = bh.OUTCOME_OK, bh.OUTCOME_FAIL


# ── 1/2/3: the state machine ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("history,expected", [
    ([OK],                              bh.STATE_OK),
    ([FAIL],                            bh.STATE_FAIL),
    ([OK, OK, OK, OK, OK],              bh.STATE_OK),
    # (1) a success after failures clears the alarm — no ageing logic involved.
    ([FAIL, FAIL, OK],                  bh.STATE_OK),
    # (3) a run of failures is ONE incident, not flapping: 1 transition.
    ([OK, FAIL, FAIL, FAIL, FAIL],      bh.STATE_FAIL),
    ([FAIL, FAIL, FAIL],                bh.STATE_FAIL),
    # (2) THE CASE THAT MATTERS: last line is `ok`, but the endpoint is alternating.
    ([OK, FAIL, OK, FAIL, OK],          bh.STATE_FLAPPING),
    ([FAIL, OK, FAIL],                  bh.STATE_FLAPPING),
    ([OK, OK, FAIL, OK],                bh.STATE_FLAPPING),
])
def test_state_machine(history, expected):
    assert bh.classify_probe_state(history) == expected


def test_flapping_outranks_the_newest_line():
    """The whole point of the third state: `tail -1` says ok, the truth is flapping."""
    history = [OK, FAIL, OK, FAIL, OK]
    assert history[-1] == OK
    assert bh.classify_probe_state(history) == bh.STATE_FLAPPING


def test_transitions_counted_not_failures():
    assert bh.count_transitions([FAIL, FAIL, FAIL, FAIL]) == 0
    assert bh.count_transitions([OK, FAIL, OK, FAIL]) == 3
    assert bh.count_transitions([OK, OK, FAIL]) == 1


def test_two_samples_cannot_declare_flapping():
    """A single transition is onset or recovery — every incident and every fix produces one."""
    assert bh.classify_probe_state([OK, FAIL]) == bh.STATE_FAIL
    assert bh.classify_probe_state([FAIL, OK]) == bh.STATE_OK


def test_worst_state_ranks_flapping_above_fail():
    probe = {"TURN": {"state": bh.STATE_FAIL}, "REFRAME": {"state": bh.STATE_FLAPPING}}
    assert bh.worst_state(probe) == bh.STATE_FLAPPING
    assert bh.worst_state({"TURN": {"state": bh.STATE_OK}}) == bh.STATE_OK


# ── 4: the endpoint must never leak a credential ──────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://llm.example.com:8080/v1/chat/completions", "llm.example.com:8080"),
    # a key in the query string must not survive into a log line or a console card
    ("https://api.vendor.ai/v1/chat?api-key=sk-SECRET-DO-NOT-LEAK", "api.vendor.ai"),
    # ...nor one in userinfo
    ("https://user:hunter2@brain.internal:11434/v1", "brain.internal:11434"),
    ("", None),
    (None, None),
])
def test_endpoint_host_is_netloc_only(url, expected):
    got = bh.endpoint_host(url)
    assert got == expected
    if got:
        assert "SECRET" not in got and "hunter2" not in got


def test_reachability_action_names_the_host_and_never_blames_the_user():
    msg = bh.reachability_action("llm.example.com:8080")
    assert "llm.example.com:8080" in msg
    assert "firewall" in msg.lower()
    for slur in ("your fault", "bad model", "poor", "wrong model", "misconfigured"):
        assert slur not in msg.lower()


def test_help_targets_resolve_in_the_real_wiki():
    """A broken help link on an error message is salt in the wound.

    This asserts against the SHIPPED wiki file, not against a copy of the constants — the exact
    class of drift that made three copies of the MCP tool descriptions diverge. If someone
    renames a section, this goes red instead of quietly shipping a link to nowhere.
    """
    import pathlib
    html = (pathlib.Path(__file__).resolve().parents[1]
            / "saas" / "dashboard" / "help" / "index.html").read_text(encoding="utf-8")
    for target in (bh.HELP_HUB, bh.HELP_ENDPOINT_UNREACHABLE, bh.HELP_ENDPOINT_FLAPPING,
                   bh.HELP_CAPTURE_DEGRADED, bh.HELP_LATENCY_PROFILES):
        page, _, anchor = target.partition(":")
        assert f'id="{page}"' in html, f"help page #{page} does not exist"
        if anchor:
            assert f'id="{anchor}"' in html, f"help anchor #{page}:{anchor} does not exist"

    # each state links to its own FIX section — the reader is already in the failure
    assert bh.help_for_state(bh.STATE_FAIL).endswith("#endpoint-fail:fail-fix")
    assert bh.help_for_state(bh.STATE_FLAPPING).endswith("#endpoint-flapping:flap-fix")
    assert bh.help_for_state(bh.STATE_OK) is None      # nothing wrong ⇒ nothing to explain


# ── 4/5: the user-facing message ──────────────────────────────────────────────────────

def _state(monkeypatch, lanes):
    monkeypatch.setattr(bh, "_enabled", lambda: True)
    monkeypatch.setattr(bh, "read_probe_state", lambda _uid: lanes)


def test_notice_is_silent_when_healthy(monkeypatch):
    """(5) A working seat produces NOTHING. A field that is always present stops being read."""
    _state(monkeypatch, {"TURN": {"lane": "TURN", "outcome": OK, "state": bh.STATE_OK,
                                  "outcome_history": [OK, OK, OK]}})
    assert bh.degradation_notice("user-0000-1111") is None


def test_fail_notice_carries_what_howlong_where_and_whattodo(monkeypatch):
    """(4) The five required parts, on the FAIL wording — for a fault that IS reachability.

    ⚠️ THIS TEST USED TO PIN THE BUG. It drove a FAIL row with NO recorded cause and then
    asserted that the notice said "firewall" and linked to the reachability page. That is
    exactly what production did to a tenant on 2026-08-17 whose endpoint had answered
    "402 Payment Required" in 90ms: a remedy for a fault they did not have, asserted by a green
    test. The five parts are still required — they were never the problem — but they are now
    required OF THE BRANCH THAT EARNED THEM: a derived ``unreachable`` fault, where no
    connection was ever established and reachability is the honest answer.
    """
    from datetime import datetime, timedelta, timezone
    _state(monkeypatch, {"REFRAME": {
        "lane": "REFRAME", "outcome": FAIL, "state": bh.STATE_FAIL,
        "outcome_history": [OK, FAIL], "elapsed_ms": 45000, "budget_ms": 6000,
        "endpoint_host": "llm.example.com:8080",
        "detail": "my mother's birthday is March 3rd",
        "fault": {"kind": "unreachable", "error_type": "ConnectError",
                  "transport_detail": "[Errno 111] Connection refused"},
        "observed_at": datetime.now(timezone.utc) - timedelta(minutes=4),
    }})
    msg = bh.degradation_notice("user-0000-1111")
    assert msg
    assert "my mother's birthday is March 3rd" in msg      # WHAT
    assert "llm.example.com:8080" in msg                    # WHICH ENDPOINT
    assert "firewall" in msg.lower()                        # WHAT TO DO
    assert "#endpoint-fail:fail-fix" in msg                 # HELP LINK
    assert "4 minutes ago" in msg                           # AGE
    assert "deleted" in msg                                 # reassurance: nothing lost
    assert "Connection refused" in msg                      # the transport's OWN words


def test_fail_notice_with_no_recorded_cause_names_no_cause_and_no_remedy(monkeypatch):
    """The same row WITHOUT a derived cause must not borrow one.

    A pre-migration-265 row, or a failure whose cause could not be derived, is the case that
    used to produce the whole false story — a timeout, a budget and a firewall, none of them
    observed. The honest output is the facts we have (which endpoint, how long, when) and an
    explicit refusal to name a cause.
    """
    from datetime import datetime, timedelta, timezone
    _state(monkeypatch, {"REFRAME": {
        "lane": "REFRAME", "outcome": FAIL, "state": bh.STATE_FAIL,
        "outcome_history": [OK, FAIL], "elapsed_ms": 45000, "budget_ms": 6000,
        "endpoint_host": "llm.example.com:8080",
        "detail": "my mother's birthday is March 3rd",
        "fault": None,
        "observed_at": datetime.now(timezone.utc) - timedelta(minutes=4),
    }})
    msg = bh.degradation_notice("user-0000-1111")
    assert msg
    # Everything we DO know is still said.
    assert "my mother's birthday is March 3rd" in msg
    assert "llm.example.com:8080" in msg
    assert "4 minutes ago" in msg
    assert "deleted" in msg
    # ...and nothing we do not.
    low = msg.lower()
    assert "firewall" not in low, "a remedy was invented for a cause that was never observed"
    assert "timed out" not in low and "timeout" not in low
    assert "budget for this step" not in low
    assert "did not record why" in low


def test_flapping_notice_is_a_different_diagnosis(monkeypatch):
    """FAIL says 'it did not answer'. FLAPPING says 'it answers sometimes' — distinct causes."""
    from datetime import datetime, timezone
    _state(monkeypatch, {"REFRAME": {
        "lane": "REFRAME", "outcome": OK, "state": bh.STATE_FLAPPING,
        "outcome_history": [OK, FAIL, OK, FAIL, OK],
        "elapsed_ms": 13000, "budget_ms": 6000,
        "endpoint_host": "llm.example.com:8080", "detail": None,
        "observed_at": datetime.now(timezone.utc),
    }})
    msg = bh.degradation_notice("user-0000-1111")
    assert msg
    assert "intermittent" in msg.lower()
    assert "13s" in msg and "6s" in msg                     # measured vs the budget
    assert "rate-limited" in msg or "busy" in msg           # names the concrete causes
    assert "load balancer" in msg
    assert "#endpoint-flapping:flap-fix" in msg
    assert msg[0].isupper()                                 # it is a sentence
    # ...and it must NOT read like the plain-failure message
    assert "did not answer" not in msg
    # A firewall rule is binary — it cannot pass a request one minute and block it the next, so
    # the FAIL remedy is the WRONG remedy here and must not be what we hand the user.
    assert "firewall" not in msg.lower()
    assert "Test & calibrate" in msg


def test_notice_never_blames_the_tenants_model(monkeypatch):
    """Tone rule. Tonight's flap was caused by OUR 6s budget, not by a bad endpoint."""
    from datetime import datetime, timezone
    for state, hist in ((bh.STATE_FAIL, [OK, FAIL]),
                        (bh.STATE_FLAPPING, [OK, FAIL, OK, FAIL])):
        _state(monkeypatch, {"TURN": {
            "lane": "TURN", "outcome": hist[-1], "state": state, "outcome_history": hist,
            "elapsed_ms": 9000, "budget_ms": 6000, "endpoint_host": "brain.local:11434",
            "detail": "x", "observed_at": datetime.now(timezone.utc)}})
        msg = (bh.degradation_notice("user-0000-1111") or "").lower()
        for slur in ("bad model", "your model is", "poor model", "weak model",
                     "you misconfigured", "your fault", "unsupported model"):
            assert slur not in msg, f"{state}: judgemental phrasing {slur!r}"


def test_notice_is_failsafe(monkeypatch):
    """An advisory must never break its host."""
    monkeypatch.setattr(bh, "_enabled", lambda: True)

    def _boom(_uid):
        raise RuntimeError("probe state unreadable")
    monkeypatch.setattr(bh, "read_probe_state", _boom)
    assert bh.degradation_notice("user-0000-1111") is None


def test_probe_event_never_raises_on_bad_input():
    for bad in (None, "", "short", "user-0000-1111"):
        bh.probe_event(bad, "TURN", "not-an-outcome")
        bh.probe_event(bad, None, OK)
