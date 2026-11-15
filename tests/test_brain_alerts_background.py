"""Background endpoint-health alerting — saas/brain_alerts.py (migration 209).

WHAT THESE TESTS ARE FOR, stated because a green suite is not evidence:
the defect being closed is "health was only evaluated while a human was signed in and looking".
So the load-bearing assertions are about what happens with NO SESSION and NO REQUEST, and about
the SEND actually being reached — not about the ledger write. `tests/test_brain_alerts_live.py`
carries the real-Postgres + real-render half; this file pins the decision logic and the
SAAS-off byte-identity.
"""

import importlib
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

# STRUCTLOG: real module first, functional stub only as a FALLBACK.
#
# This used to be `sys.modules.setdefault("structlog", types.ModuleType("structlog"))` —
# an EMPTY module installed unconditionally at collection time. Collected alphabetically
# first (as any whole-directory run does), it replaced structlog BEFORE the real one was
# ever imported, and the corruption was process-permanent: three tests in
# test_brain_event_classification.py then died importing src.api.llm_client with
# `AttributeError: module 'structlog' has no attribute 'get_logger'` — order-dependent
# reds that pass in isolation, which is exactly the shape nobody notices. The stub also
# broke brain_alerts' OWN runtime logging (it calls structlog.get_logger inside _log).
# The fallback below keeps the file's original promise (importable with no structlog
# installed) while providing the one function the saas modules actually call.
try:
    import structlog  # noqa: F401
except ImportError:  # pragma: no cover — structlog is a core dep; fallback only
    _stub = types.ModuleType("structlog")

    class _StubLogger:
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def warn(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

        def debug(self, *a, **k):
            pass

    def _get_logger(*a, **k):  # noqa: E301
        return _StubLogger()

    _stub.get_logger = _get_logger
    sys.modules.setdefault("structlog", _stub)

import saas.brain_alerts as BA  # noqa: E402
import saas.brain_health as BH  # noqa: E402


# ── a fake ledger, so the decision logic is testable without a database ──────────────────

class _Ledger:
    """In-memory stand-in for public.brain_alert_state."""

    def __init__(self, initial=None):
        self.rows = dict(initial or {})
        self.writes = []

    def read(self, user_id):
        base = {"user_id": str(user_id), "account_id": "acct-1", "alerts_enabled": True,
                "last_state": None, "evaluated_at": None, "alerted_state": None,
                "alerted_at": None, "alerted_to": None, "fault_alert_at": None,
                "alerts_sent": 0, "alerts_suppressed": 0, "last_send_error": None}
        base.update(self.rows.get(str(user_id), {}))
        return base

    def write(self, user_id, account_id, **fields):
        self.writes.append((str(user_id), dict(fields)))
        self.rows.setdefault(str(user_id), {}).update(fields)
        return True


UID = "aaaaaaaa-1111-4aaa-8aaa-aaaaaaaaaaaa"


def _probe(state, lane="EXTRACT", history=None, host="brain.example.com:8080", fault=None):
    """A brain_probe_state read shaped exactly as brain_health.read_probe_state returns it.

    ``fault`` is the derived cause of the most recent failure (migration 265). It is the field
    that decides which remedy the email carries, so a test that leaves it None is testing the
    we-do-not-know branch — which is a real case, and not the only one.
    """
    return {lane: {"lane": lane, "outcome": "fail" if state != "ok" else "ok",
                   "observed_at": datetime.now(timezone.utc) - timedelta(minutes=4),
                   "elapsed_ms": 6000, "budget_ms": 6000, "endpoint_host": host,
                   "reason": None, "detail": None, "fault": fault,
                   "outcome_history": history or (["fail"] if state != "ok" else ["ok"]),
                   "state": state, "transitions": 0}}


UNREACHABLE = {"kind": "unreachable", "error_type": "ConnectError",
               "transport_detail": "[Errno 111] Connection refused"}
REFUSED_402 = {"kind": "refused", "status": 402, "status_phrase": "Payment Required",
               "status_class": 4,
               "provider_message": "Payment required to access this resource. "
                                   "Visit your billing tab."}


@pytest.fixture
def rig(monkeypatch):
    """Wire brain_alerts to fakes: SaaS on, ledger in memory, send captured, brain bound."""
    monkeypatch.setenv("SAAS_MODE", "true")
    monkeypatch.delenv("BRAIN_ALERTS_ENABLED", raising=False)

    ledger = _Ledger()
    sent = []

    monkeypatch.setattr(BA, "read_state", ledger.read)
    monkeypatch.setattr(BA, "_write_state", ledger.write)
    monkeypatch.setattr(BA, "_brain_is_bound", lambda uid, acct: True)
    monkeypatch.setattr(BA, "_lookup_individual", lambda uid: {
        "user_id": uid, "account_id": "acct-1", "email": "seat@example.com",
        "display_name": "Seat One", "tenant_context": "house", "can_control_endpoint": True})
    monkeypatch.setattr(BA, "_console_url", lambda: "https://app.example.com/dashboard/user.html")

    def _deliver(to, **kw):
        sent.append(dict(to=to, **kw))
        return {"ok": True}

    monkeypatch.setattr(BA, "_deliver", _deliver)
    return types.SimpleNamespace(ledger=ledger, sent=sent, monkeypatch=monkeypatch)


def _set_probe(rig, state, **kw):
    rig.monkeypatch.setattr(BH, "read_probe_state", lambda uid: _probe(state, **kw))


# ══════════════════════════════════════════════════════════════════════════════════════
# THE HEADLINE: it evaluates with NO SESSION and NO REQUEST
# ══════════════════════════════════════════════════════════════════════════════════════

def test_evaluates_and_alerts_with_no_session_anywhere(rig):
    """The whole point. No FastAPI app, no cookie, no request object is constructed here —
    if this passes, the verdict is reachable from a background loop."""
    _set_probe(rig, "fail")
    out = BA.evaluate_seat(UID)
    assert out["state"] == "fail"
    assert out["action"] == "alerted"
    assert len(rig.sent) == 1
    assert rig.sent[0]["to"] == "seat@example.com"
    assert rig.sent[0]["state"] == "fail"
    # the verdict is STORED, so a console can later read it rather than cause it
    assert rig.ledger.read(UID)["last_state"] == "fail"
    assert rig.ledger.read(UID)["alerted_state"] == "fail"


def test_a_stored_verdict_is_what_the_console_reads(rig):
    """After the background pass, the state is on the row — the console renders, never probes."""
    _set_probe(rig, "flapping")
    BA.evaluate_seat(UID)
    row = rig.ledger.read(UID)
    assert row["last_state"] == "flapping"
    assert row["alerted_to"] == "seat@example.com"
    assert row["alerts_sent"] == 1


# ══════════════════════════════════════════════════════════════════════════════════════
# COST: a quiet / opted-out / brain-less seat must not be probed at all
# ══════════════════════════════════════════════════════════════════════════════════════

def test_no_brain_bound_is_never_probed_and_never_emailed(rig):
    """'Not set up yet' is not 'broken'. An unbound endpoint must not read the probe stream and
    must not generate mail telling somebody to check a host that does not exist."""
    rig.monkeypatch.setattr(BA, "_brain_is_bound", lambda uid, acct: False)

    def _boom(uid):
        raise AssertionError("read_probe_state must not be reached for a brain-less seat")

    rig.monkeypatch.setattr(BH, "read_probe_state", _boom)
    out = BA.evaluate_seat(UID)
    assert out["action"] == "skipped_no_brain"
    assert rig.sent == []


def test_opted_out_seat_costs_nothing(rig):
    rig.ledger.rows[UID] = {"alerts_enabled": False}

    def _boom(uid):
        raise AssertionError("an opted-out seat must not be probed")

    rig.monkeypatch.setattr(BH, "read_probe_state", _boom)
    rig.monkeypatch.setattr(BA, "_lookup_individual",
                            lambda uid: pytest.fail("no control-plane lookup for an opted-out seat"))
    assert BA.evaluate_seat(UID)["action"] == "skipped_opted_out"
    assert rig.sent == []


def test_unbinding_a_brain_clears_an_open_fault_without_claiming_recovery(rig):
    """Removing an endpoint is a configuration change, not a fix. It must close the alert
    silently — and must NOT send a 'recovered' notice that never happened."""
    rig.ledger.rows[UID] = {"alerted_state": "fail"}
    rig.monkeypatch.setattr(BA, "_brain_is_bound", lambda uid, acct: False)
    BA.evaluate_seat(UID)
    assert rig.sent == []
    assert rig.ledger.read(UID)["alerted_state"] is None


def test_the_lane_makes_no_llm_call(rig):
    """Stated as a fact in the module docstring; pinned here so it stays one."""
    import saas.llm_config as _lc
    rig.monkeypatch.setattr(_lc, "resolve_account_llm_config",
                            lambda *a, **k: pytest.fail("brain_alerts must never resolve a brain "
                                                        "to CALL it"))
    _set_probe(rig, "fail")
    BA.evaluate_seat(UID)
    assert len(rig.sent) == 1


# ══════════════════════════════════════════════════════════════════════════════════════
# ALERT POLICY: state changes only, deduped, rate-limited, self-closing
# ══════════════════════════════════════════════════════════════════════════════════════

def test_same_state_twice_sends_exactly_one_email(rig):
    """A seat down for a week is ONE email, not one per cycle. This is the anti-spam invariant:
    mail an operator per incident and they filter the folder, and then the one that mattered is
    invisible too."""
    _set_probe(rig, "fail")
    for _ in range(10):
        BA.evaluate_seat(UID)
    assert len(rig.sent) == 1
    assert rig.ledger.read(UID)["last_state"] == "fail"   # still evaluated every time


def test_flapping_produces_one_alert_not_one_per_flap(rig):
    """FLAPPING oscillates by definition. The CLASSIFIER absorbs the oscillation into a single
    stable state before the mailer ever sees it — which is why FLAPPING had to be a first-class
    state rather than 'N failures recently'."""
    hist = ["ok", "fail", "ok", "fail", "ok"]
    assert BH.classify_probe_state(hist) == BH.STATE_FLAPPING   # the premise, asserted
    _set_probe(rig, "flapping", history=hist)
    for _ in range(6):
        BA.evaluate_seat(UID)
    assert len(rig.sent) == 1
    assert rig.sent[0]["state"] == "flapping"


def test_recovery_notice_closes_its_own_alert(rig):
    _set_probe(rig, "fail")
    BA.evaluate_seat(UID)
    _set_probe(rig, "ok")
    out = BA.evaluate_seat(UID)
    assert out["action"] == "recovered"
    assert [s["state"] for s in rig.sent] == ["fail", "recovered"]
    assert rig.ledger.read(UID)["alerted_state"] == "ok"


def test_an_ok_seat_that_was_never_alerted_stays_silent(rig):
    _set_probe(rig, "ok")
    for _ in range(3):
        BA.evaluate_seat(UID)
    assert rig.sent == []


def test_recovery_is_exempt_from_the_fault_rate_limit(rig):
    """Gating the recovery on the same floor is how an alert gets left lit after the problem is
    gone. The floor exists to stop fault spam, not to hide good news."""
    rig.monkeypatch.setenv("BRAIN_ALERT_MIN_INTERVAL_S", "86400")
    _set_probe(rig, "fail")
    BA.evaluate_seat(UID)
    _set_probe(rig, "ok")
    out = BA.evaluate_seat(UID)
    assert out["action"] == "recovered"
    assert len(rig.sent) == 2


def test_rate_limit_defers_a_fault_it_does_not_swallow_it(rig):
    """A suppressed alert must NOT advance alerted_state, or the transition is lost forever."""
    rig.monkeypatch.setenv("BRAIN_ALERT_MIN_INTERVAL_S", "3600")
    now = datetime.now(timezone.utc)
    rig.ledger.rows[UID] = {"alerted_state": "ok", "fault_alert_at": now - timedelta(minutes=5)}
    _set_probe(rig, "fail")
    out = BA.evaluate_seat(UID)
    assert out["action"] == "suppressed"
    assert rig.sent == []
    assert rig.ledger.read(UID)["alerted_state"] == "ok"      # NOT advanced → fires later
    assert rig.ledger.read(UID)["alerts_suppressed"] == 1

    # ...and once the floor has passed, the very same state DOES alert.
    rig.ledger.rows[UID]["fault_alert_at"] = now - timedelta(hours=2)
    assert BA.evaluate_seat(UID)["action"] == "alerted"
    assert len(rig.sent) == 1


def test_a_failed_send_is_retried_not_believed(rig):
    """alerted_state must not advance on a send failure, or one dead relay silently eats the
    only notice anyone was going to get."""
    rig.monkeypatch.setenv("BRAIN_ALERT_MIN_INTERVAL_S", "0")
    rig.monkeypatch.setattr(BA, "_deliver", lambda to, **kw: {"ok": False, "error": "relay down"})
    _set_probe(rig, "fail")
    out = BA.evaluate_seat(UID)
    assert out["action"] == "send_failed"
    assert rig.ledger.read(UID)["alerted_state"] is None
    assert "relay" in (rig.ledger.read(UID)["last_send_error"] or "")


def test_no_addressee_sends_nothing_and_keeps_the_alert_pending(rig):
    rig.monkeypatch.setattr(BA, "_lookup_individual", lambda uid: None)
    _set_probe(rig, "fail")
    out = BA.evaluate_seat(UID)
    assert out["action"] == "no_addressee"
    assert rig.sent == []
    assert rig.ledger.read(UID)["alerted_state"] is None


# ══════════════════════════════════════════════════════════════════════════════════════
# GOVERNANCE: the message goes to whoever holds the control it names
# ══════════════════════════════════════════════════════════════════════════════════════

def test_a_governed_seat_alerts_its_administrator_not_the_seat_holder(rig):
    """A tenant-governed seat has the Brain tab HIDDEN. Mailing that person 'check your endpoint'
    names a control that is not on their screen and cannot be made to appear."""
    rig.monkeypatch.setattr(BA, "_lookup_individual", lambda uid: {
        "user_id": uid, "account_id": "acct-1", "email": "seat@example.com",
        "display_name": "Seat One", "tenant_context": "real", "can_control_endpoint": False})
    import saas.user_console as UC
    rig.monkeypatch.setattr(UC, "_seat_admin_contact", lambda ind: "admin@corp.example")
    _set_probe(rig, "fail")
    out = BA.evaluate_seat(UID)
    assert out["action"] == "alerted"
    assert rig.sent[0]["to"] == "admin@corp.example"
    assert out["to_governed_admin"] is True


def test_a_self_owned_seat_alerts_the_seat_holder(rig):
    _set_probe(rig, "fail")
    BA.evaluate_seat(UID)
    assert rig.sent[0]["to"] == "seat@example.com"


def test_addressee_reuses_the_existing_seat_admin_contact_resolver(rig):
    """Pinned so a future edit cannot fork a second, differently-behaved resolver — the console
    banner and this email must never name different people."""
    import inspect
    src = inspect.getsource(BA.resolve_addressee)
    assert "_seat_admin_contact" in src


# ══════════════════════════════════════════════════════════════════════════════════════
# CONTENT: measured, actionable, no value judgement, no credential
# ══════════════════════════════════════════════════════════════════════════════════════

def test_fail_and_flapping_get_different_remedies(rig):
    """A firewall rule is binary — the FAIL advice is actively wrong for an intermittent
    endpoint, so the two states must not share a remedy line.

    ⚠️ THIS TEST USED TO PIN THE BUG. It drove a FAIL with NO recorded cause and asserted that
    the EMAIL said "firewall" — the 2026-08-17 defect with postage on it, held in place by a
    green assertion. The remedy is now derived from the cause, so the FAIL side of this
    comparison is an actual unreachable endpoint, which is the one that earns the sentence.
    """
    _set_probe(rig, "fail", fault=UNREACHABLE)
    BA.evaluate_seat(UID)
    rig.ledger.rows[UID] = {}
    _set_probe(rig, "flapping", history=["ok", "fail", "ok", "fail", "ok"])
    BA.evaluate_seat(UID)
    fail_action, flap_action = rig.sent[0]["action"], rig.sent[1]["action"]
    assert fail_action != flap_action
    assert "firewall" in fail_action
    assert "firewall" not in flap_action


def test_the_email_remedy_matches_the_fault_it_is_about(rig):
    """A tenant whose account is unpaid must not be emailed about their network.

    The email is the surface that LEAVES THE BUILDING, so a wrong remedy here is the most
    expensive copy of the defect: it arrives in an inbox, is read once, and is acted on.
    """
    _set_probe(rig, "fail", fault=REFUSED_402)
    BA.evaluate_seat(UID)
    action = rig.sent[0]["action"].lower()
    assert "firewall" not in action, "the email sent an unpaid account to their firewall"
    assert "reachable from faultline" not in action


def test_the_email_offers_no_remedy_when_no_cause_was_recorded(rig):
    """No evidence, no instruction — only an offer to go and find out."""
    _set_probe(rig, "fail", fault=None)
    BA.evaluate_seat(UID)
    action = rig.sent[0]["action"].lower()
    assert "firewall" not in action
    assert "test & calibrate" in action


# ══════════════════════════════════════════════════════════════════════════════════════
# THE RENDERED EMAIL — past the _deliver seam, which is where the lie was still living
# ══════════════════════════════════════════════════════════════════════════════════════
#
# ⚠️ EVERY OTHER TEST IN THIS FILE STOPS AT `_deliver` AND ASSERTS ON `sent[0]["action"]`.
# That seam is BEFORE smtp_sender, so the subject, the heading and the body prose were never
# exercised by anything — and they were still rendered from `state == "fail"` alone. A 402
# answered in 84ms produced, in an email: "FaultLine cannot reach your model endpoint …
# those calls are not currently getting an answer … it ran for 0.084s against an 8s budget
# before FaultLine stopped waiting." Four false clauses, on the one surface that leaves the
# building, with the action line correct above them. These tests render the real thing.

def _render(state, fault, facts=("Endpoint: api.cerebras.ai.",), host="api.cerebras.ai"):
    """Subject AND body, both through the REAL public seam.

    ⚠️ AN EARLIER VERSION OF THIS HELPER COMPUTED THE SUBJECT ITSELF, by calling
    ``_brain_alert_key`` in the test and looking the subject up here. That made the subject
    correct no matter what ``_brain_alert_bodies`` did with its argument — so deleting the
    re-key from the body builder left every assertion green while the BODY reverted to the
    "cannot reach your endpoint" story. A helper that reimplements the thing under test is
    a mock that short-circuits the statement it is meant to prove.

    ``send_brain_alert_email`` is stubbed at ``send_email`` — the last function before SMTP —
    so subject and body are exactly what would have gone on the wire.
    """
    from saas import smtp_sender
    captured = {}

    def _capture(to, subject, text, html, require_enabled=False):
        captured.update(to=to, subject=subject, text=text, html=html)
        return {"ok": True}

    real = smtp_sender.send_email
    smtp_sender.send_email = _capture
    try:
        smtp_sender.send_brain_alert_email("seat@example.com", state=state, action="",
                                           facts=list(facts), host=host, fault=fault)
    finally:
        smtp_sender.send_email = real
    return captured["subject"], captured["text"]


def test_the_email_about_a_refusal_does_not_claim_the_endpoint_was_unreachable():
    subject, text = _render("fail", REFUSED_402)
    for surface, blob in (("subject", subject.lower()), ("body", text.lower())):
        assert "cannot reach" not in blob, f"the {surface} said unreachable about an endpoint that answered"
        assert "not currently getting an answer" not in blob, surface
        assert "stopped waiting" not in blob, surface
    assert "refusing" in subject.lower()
    assert "refusing" in text.lower(), "the BODY still told the unreachable story"


def test_the_email_about_a_real_unreachable_endpoint_still_says_so():
    subject, text = _render("fail", UNREACHABLE)
    assert "cannot reach" in subject.lower()
    assert "cannot reach" in text.lower()


def test_the_email_with_no_recorded_cause_names_no_cause():
    subject, text = _render("fail", None)
    assert "did not record why" in text.lower()
    for blob in (subject.lower(), text.lower()):
        assert "cannot reach your model endpoint" not in blob
        assert "firewall" not in blob


def test_a_new_state_does_not_fall_through_to_the_unreachable_story():
    """`egress_blocked` was added to ALERTABLE with an action branch and NO subject/body branch,
    so it fell straight through to "FaultLine cannot reach your model endpoint … those calls
    are not currently getting an answer" — for a call that was never made. The same fallback
    shape had already bitten the help-page lookup in this module."""
    subject, text = _render("egress_blocked", None)
    assert "address" in subject.lower()
    assert "address" in text.lower()
    for blob in (subject.lower(), text.lower()):
        assert "cannot reach your model endpoint" not in blob
        assert "not currently getting an answer" not in blob


def test_the_timeout_email_body_is_a_speed_story_not_a_reachability_one():
    subject, text = _render("fail", {"kind": "timeout", "error_type": "ReadTimeout"})
    assert "too slowly" in subject.lower()
    assert "speed problem" in text.lower() or "does not arrive inside" in text.lower()
    assert "cannot reach" not in text.lower()


def test_our_own_saturation_is_never_billed_to_the_tenants_provider():
    subject, text = _render("fail", {"kind": "local_limit", "error_type": "PoolTimeout"})
    assert "our capacity" in subject.lower()
    assert "on our side" in text.lower()
    assert "nothing is required from you" in text.lower()
    assert "cannot reach" not in text.lower()


def test_every_alertable_state_has_its_own_subject():
    from saas import smtp_sender
    from saas import brain_alerts as BAX
    seen = {}
    for state in BAX.ALERTABLE:
        key = smtp_sender._brain_alert_key(state, None)
        subj = smtp_sender._BRAIN_ALERT_SUBJECTS.get(key)
        assert subj, f"{state} has no subject of its own and falls back to the FAIL story"
        seen[state] = subj
    assert len(set(seen.values())) == len(seen), f"two states share a subject: {seen}"


def test_the_measured_facts_do_not_manufacture_a_timeout():
    """0.084s against an 8s budget is not "before FaultLine stopped waiting" — it is an
    endpoint answering promptly, and the budget was never approached."""
    from datetime import datetime, timezone
    from saas import brain_alerts as BAX
    probe = {"REFRAME": {"lane": "REFRAME", "outcome": "fail", "state": "fail",
                         "elapsed_ms": 84, "budget_ms": 8000,
                         "endpoint_host": "api.cerebras.ai", "reason": "llm_call_failed",
                         "detail": None, "fault": REFUSED_402, "outcome_history": ["fail"],
                         "observed_at": datetime.now(timezone.utc)}}
    facts, host = BAX._facts_for(probe, "fail")
    blob = " ".join(facts)
    assert "stopped waiting" not in blob
    assert "nothing timed out" in blob.lower()
    # ...and the provider's own sentence reaches the one thing a customer definitely reads.
    assert "Visit your billing tab" in blob


def test_a_connection_that_was_never_made_is_not_described_as_a_wait():
    """A DNS failure in five milliseconds is not "before FaultLine stopped waiting".

    The first correction here carved out `refused` and left the two transport kinds inventing
    the same timeout — so the line rendered four lines below the email's own "the requests are
    never sent". Nothing was waited for because nothing was ever asked.
    """
    from datetime import datetime, timezone
    from saas import brain_alerts as BAX
    probe = {"REFRAME": {"lane": "REFRAME", "outcome": "fail", "state": "fail",
                         "elapsed_ms": 5, "budget_ms": 8000,
                         "endpoint_host": "api.cerebras.ai", "reason": "llm_call_failed",
                         "detail": None, "fault": UNREACHABLE, "outcome_history": ["fail"],
                         "observed_at": datetime.now(timezone.utc)}}
    blob = " ".join(BAX._facts_for(probe, "fail")[0])
    assert "stopped waiting" not in blob, "a connection never made was reported as a timeout"
    assert "no answer to wait for" in blob
    assert "8s budget" not in blob


def test_the_email_never_passes_judgement_on_the_endpoint(rig):
    _set_probe(rig, "fail", fault=UNREACHABLE)
    BA.evaluate_seat(UID)
    blob = " ".join([rig.sent[0]["action"]] + list(rig.sent[0]["facts"])).lower()
    for banned in ("too slow", "bad", "poor", "wrong model", "misconfigur", "should have"):
        assert banned not in blob


def test_a_credential_in_the_base_url_never_reaches_the_email(rig):
    """endpoint_host() strips user:pass@ and the path. Some providers carry the key IN the URL,
    and this text leaves the building."""
    _set_probe(rig, "fail", host=BH.endpoint_host("https://user:s3cr3t@brain.example.com/v1?api-key=AKIA"))
    BA.evaluate_seat(UID)
    blob = str(rig.sent[0])
    assert "s3cr3t" not in blob and "AKIA" not in blob
    assert "brain.example.com" in blob


def test_measured_numbers_are_carried(rig):
    _set_probe(rig, "fail")
    BA.evaluate_seat(UID)
    facts = " ".join(rig.sent[0]["facts"])
    assert "6s" in facts                       # elapsed vs budget, both rendered
    assert "brain.example.com" in facts


# ══════════════════════════════════════════════════════════════════════════════════════
# SAAS OFF ⇒ BYTE-FOR-BYTE UNCHANGED
# ══════════════════════════════════════════════════════════════════════════════════════

def test_saas_off_is_completely_inert(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    assert BA.enabled() is False
    monkeypatch.setattr(BH, "read_probe_state",
                        lambda uid: pytest.fail("nothing may run with SAAS off"))
    assert BA.evaluate_seat(UID)["action"] == "skipped_disabled"
    assert BA.run_sweep([UID]) == {"seen": 0, "due": 0, "alerted": 0, "recovered": 0,
                                   "suppressed": 0, "no_change": 0, "skipped": 0, "errors": 0}


def test_the_lane_can_be_switched_off_independently_of_saas(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "true")
    monkeypatch.setenv("BRAIN_ALERTS_ENABLED", "false")
    assert BA.enabled() is False


def test_embedder_hook_is_import_guarded(monkeypatch):
    """The public core has no saas/ directory. The hook must be a guarded lazy import."""
    import pathlib
    src = pathlib.Path("src/re_embedder/embedder.py").read_text()
    i = src.index("PHASE 2z")
    block = src[i:i + 3400]
    assert "from saas import brain_alerts" in block
    assert "except Exception" in block


# ══════════════════════════════════════════════════════════════════════════════════════
# THE SWEEP: the ledger gate is what makes a quiet seat free
# ══════════════════════════════════════════════════════════════════════════════════════

def test_sweep_skips_seats_the_ledger_says_are_clean(rig):
    _set_probe(rig, "fail")
    seats = [(UID, "faultline_a"), ("bbbbbbbb-2222-4bbb-8bbb-bbbbbbbbbbbb", "faultline_b")]
    ran = []
    stats = BA.run_sweep(seats, is_due=lambda u: u == UID, on_ran=ran.append)
    assert stats["seen"] == 2 and stats["due"] == 1 and stats["skipped"] == 1
    assert ran == [UID]


def test_sweep_records_the_run_so_the_ledger_can_skip_next_cycle(rig):
    _set_probe(rig, "ok")
    ran = []
    BA.run_sweep([UID], is_due=lambda u: True, on_ran=ran.append)
    assert ran == [UID]


def test_sweep_has_a_blast_radius_cap(rig, monkeypatch):
    monkeypatch.setenv("BRAIN_ALERT_MAX_SEATS_PER_CYCLE", "2")
    _set_probe(rig, "ok")
    seats = [f"cccccccc-{i:04d}-4ccc-8ccc-cccccccccccc" for i in range(10)]
    stats = BA.run_sweep(seats, is_due=lambda u: True)
    assert stats["due"] == 2


def test_sweep_survives_a_seat_that_explodes(rig):
    """A monitoring bug must not become an outage — one bad seat cannot end the pass."""
    calls = {"n": 0}

    def _flaky(uid, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return {"action": "no_change", "state": "ok"}

    rig.monkeypatch.setattr(BA, "evaluate_seat", _flaky)
    stats = BA.run_sweep([UID, "dddddddd-4444-4ddd-8ddd-dddddddddddd"], is_due=lambda u: True)
    assert stats["due"] == 2


def test_subsystem_name_matches_the_ledger_registry():
    """claim() returns True for an UNKNOWN subsystem, so a typo here would silently disable the
    skip gate and evaluate every seat every cycle — the exact cost defect being avoided."""
    from src.re_embedder import sweep_ledger
    assert BA.SUBSYSTEM in sweep_ledger.SUBSYSTEMS
    assert sweep_ledger.SUBSYSTEMS[BA.SUBSYSTEM]["llm"] is False
    assert "brain_probe_state" in sweep_ledger.SUBSYSTEMS[BA.SUBSYSTEM]["tables"]
    assert BA.SUBSYSTEM in sweep_ledger.subsystems_for_tables("brain_probe_state")


def test_the_probe_flush_marks_the_subsystem_dirty():
    """The writer half. Without this mark the ledger never goes dirty and every seat falls back
    to the hourly MAX-RUN bound — it would still work, but late, for no reason."""
    import inspect
    src = inspect.getsource(BH._flush_last_events)
    assert "mark_dirty_subsystems_conn" in src
    assert "brain_alerts" in src


# ══════════════════════════════════════════════════════════════════════════════════════
# THE 401 FIX
# ══════════════════════════════════════════════════════════════════════════════════════

def test_alert_strip_stops_polling_on_401():
    """41 x 401 in 20 minutes: the poller only stopped on an explicit sign-out, so an EXPIRED
    session in an open tab hammered the endpoint forever."""
    import pathlib
    js = pathlib.Path("saas/dashboard/alerts.js").read_text()
    assert "e.status === 401" in js
    assert "teardown(); return;" in js


def test_alert_strip_has_no_inline_handlers():
    """Dashboard CSP is script-src 'self' — an inline-script build renders BLANK."""
    import pathlib
    import re
    html = pathlib.Path("saas/dashboard/user.html").read_text()
    i = html.index("brain-alert-box")
    block = html[i - 400:i + 900]
    assert "<script" not in block
    # No inline event attribute of ANY name anywhere in the new markup.
    assert not re.search(r"\son[a-z]+\s*=", block), "inline handler in the alert-toggle markup"
    # ...and the handler is bound from external JS instead.
    js = pathlib.Path("saas/dashboard/user.js").read_text()
    assert "$('brain-alert-toggle'); if (b) b.onchange = saveBrainAlertPref" in js


def test_only_401_stops_the_poll():
    """A 500 or a network blip recovers on its own; tearing down on those would silently stop
    the strip for the rest of the session."""
    import pathlib
    js = pathlib.Path("saas/dashboard/alerts.js").read_text()
    i = js.index("async function refresh()")
    body = js[i:i + 1800]
    assert body.count("teardown(); return;") == 1


# ══════════════════════════════════════════════════════════════════════════════════════
# THE TOGGLE
# ══════════════════════════════════════════════════════════════════════════════════════

def test_toggle_is_hidden_for_a_seat_that_cannot_control_its_endpoint():
    """Offering a switch that would 403 reads as a broken product, not as governance."""
    import pathlib
    js = pathlib.Path("saas/dashboard/user.js").read_text()
    i = js.index("async function loadBrainAlertPref")
    body = js[i:i + 1400]
    assert "can_configure" in body
    assert "classList.add('hidden')" in body


def test_toggle_write_is_governance_gated():
    import inspect
    import saas.user_console as UC
    assert "_require_endpoint_control" in inspect.getsource(UC.me_set_brain_alerts)


def test_toggle_read_returns_the_destination():
    """A notification setting that does not say where it sends is one people distrust."""
    import inspect
    import saas.user_console as UC
    src = inspect.getsource(UC.me_get_brain_alerts)
    assert '"destination"' in src and '"destination_kind"' in src
