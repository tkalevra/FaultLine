"""A SAVED endpoint whose stored key will not decrypt must be SURFACED, not just logged.

THE DEFECT. `llm_config._build_config` refuses when a non-empty `api_key_ciphertext` fails to
decrypt under the deployed SAAS_LLM_CRED_KEY, and has always logged
`account_llm_config.key_undecryptable` with `remediation='check SAAS_LLM_CRED_KEY'`. That log
line had ZERO consumers for its entire life. The resolve returned None, the request lane bound
the generic no-brain sentinel, and the seat was told "No model endpoint is connected to
FaultLine yet. Open the LLM Brain tab, set your Base URL and Model." They had. It was saved.
It was valid.

MEASURED on pre-prod 2026-08-17: three accounts in exactly that state, including the
operator's own, every one emitting the CRIT on every call for days. A critical that fires
continuously into a container log nobody reads is operationally identical to no alarm at all.

WHAT THESE TESTS PIN
  1. the condition is its OWN state, and it outranks the generic one;
  2. every surface says the RIGHT thing — and specifically never the setup line or the
     reachability line, which are the two ways to send someone to fix a thing that is not
     broken;
  3. every surface carries the REMEDIATION. A notification that says "your brain is broken"
     without saying "re-save your API key" has failed the fail-loud test as surely as silence
     did, so the remedy is asserted, not just the fault;
  4. it EMAILS — unlike no_brain, which is a setup state and deliberately never emails;
  5. IT DOES NOT MAIL-BOMB. The CRIT fires on every call, so the debounce is the difference
     between an alert and an outage of its own.

No DB, no network, no SMTP.
"""
import types
from datetime import datetime, timedelta, timezone

import pytest

from saas import brain_alerts as BA
from saas import brain_health as BH
from saas import smtp_sender as SM

UID = "aaaaaaaa-1111-4aaa-8aaa-aaaaaaaaaaaa"


# ── 1. its own state, ranked above the generic one ───────────────────────────────────

def test_it_is_its_own_state_not_no_brain():
    assert BH.classify_probe_state(["fail"], "fail", BH.REASON_KEY_UNDECRYPTABLE) == \
        BH.STATE_KEY_UNDECRYPTABLE
    assert BH.classify_probe_state(["fail"], "fail", BH.REASON_NO_BRAIN) == BH.STATE_NO_BRAIN


def test_it_outranks_no_brain():
    """Both bind the same fail-closed sentinel. The more specific reason is the truer
    statement, and it is the only one of the two the tenant can act on."""
    worst = BH.worst_state({
        "A": {"state": BH.STATE_NO_BRAIN},
        "B": {"state": BH.STATE_KEY_UNDECRYPTABLE},
    })
    assert worst == BH.STATE_KEY_UNDECRYPTABLE


def test_an_unrelated_reason_is_untouched():
    """The new branch must not swallow arbitrary failure reasons."""
    assert BH.classify_probe_state(["ok", "fail"], "fail", "timeout") == BH.STATE_FAIL
    assert BH.classify_probe_state(["ok", "fail"], "fail", None) == BH.STATE_FAIL


def test_the_wire_value_matches_what_llm_calls_stamps():
    """The reason is derived from the METRIC NAME by `_health_note`, so the two constants
    must be the same string or the whole state silently reverts to the generic one."""
    from src.api import llm_calls as LC
    assert BH.REASON_KEY_UNDECRYPTABLE == BH.M_KEY_UNDECRYPTABLE == "key_undecryptable"
    assert BH.M_KEY_UNDECRYPTABLE in BH.METRICS, "record() drops a metric not in METRICS"
    assert BH.M_KEY_UNDECRYPTABLE in BH.DEGRADATION_METRICS
    assert callable(LC.no_brain_metric)


def test_the_metric_picks_itself_from_the_sentinel(monkeypatch):
    from src.api import llm_calls as LC
    import saas.llm_context as LX
    monkeypatch.setattr(LX, "current_llm_config",
                        lambda: {"no_brain": True, "no_brain_reason": "key_undecryptable"})
    assert LC.no_brain_metric() == "key_undecryptable"
    monkeypatch.setattr(LX, "current_llm_config", lambda: {"no_brain": True})
    assert LC.no_brain_metric() == "no_brain", "no reason recorded ⇒ today's behaviour"


# ── 2 & 3. every surface: right words, and the remedy ────────────────────────────────

_SETUP_WORDS = ("set your base url", "set a base url", "connected yet", "not connected")
_REACHABILITY_WORDS = ("firewall", "reachable", "unreachable", "not answering")


def _banner_self():
    return BH.banner(configured=True, state=BH.STATE_KEY_UNDECRYPTABLE,
                     tenant_context="house", can_control_endpoint=True,
                     base_url="https://openrouter.ai/api/")


def test_the_banner_never_tells_them_to_configure_what_is_configured():
    b = _banner_self()
    assert b is not None
    blob = f"{b['title']} {b['body']} {b['action']}".lower()
    for w in _SETUP_WORDS:
        assert w not in blob, f"setup wording {w!r} on a CONFIGURED endpoint"
    for w in _REACHABILITY_WORDS:
        assert w not in blob, f"reachability wording {w!r} — the endpoint is fine, the key is not"


def test_every_surface_carries_the_remediation():
    """The one action that clears it: re-enter the key so it re-encrypts under the current
    master key. Asserted on all three surfaces, because the fault without the fix is what
    the silent CRIT already was."""
    b = _banner_self()
    blob = f"{b['title']} {b['body']} {b['action']}".lower()
    assert "re-enter" in blob and "key" in blob
    assert "save" in blob

    action = BH.rekey_action()
    assert "re-enter" in action.lower() and "key" in action.lower()

    _, html = SM._brain_alert_bodies(
        state="key_undecryptable", action=action, facts=[], seat_label="Seat One",
        host=None, help_url="https://x/help", console_url="https://x/console",
        alerted_at_phrase=None)
    assert "re-enter" in html.lower()


def test_the_governed_seat_is_told_who_can_act_and_not_to_do_it_itself():
    # `real` is the ONLY governed tenant_context (seat_is_governed); "tenant" reads as
    # self-owned and silently exercises the wrong branch. Written wrong first, and the test
    # caught it — kept as a note because the token is easy to guess incorrectly.
    b = BH.banner(configured=True, state=BH.STATE_KEY_UNDECRYPTABLE,
                  tenant_context="real", can_control_endpoint=False,
                  base_url="https://openrouter.ai/api/", admin_contact="admin@example.com")
    assert b is not None
    blob = f"{b['title']} {b['body']} {b['action']}".lower()
    assert "administrator" in blob
    assert b["blocking"] is False, "a seat that cannot act must not be hard-blocked"


def test_the_banner_is_wellformed_like_every_other_state():
    b = _banner_self()
    for k in ("kind", "severity", "title", "body", "action", "help", "blocking"):
        assert b.get(k) not in (None, ""), f"banner missing {k}"
    assert b["severity"] in (BH.BANNER_SETUP, BH.BANNER_WARN)
    assert b["help"].startswith(BH.HELP_BASE)


def test_no_credential_from_the_base_url_reaches_the_banner():
    b = BH.banner(configured=True, state=BH.STATE_KEY_UNDECRYPTABLE,
                  tenant_context="house", can_control_endpoint=True,
                  base_url="https://u:SUPERSECRET@brain.example.com/v1?api_key=SUPERSECRET")
    assert "SUPERSECRET" not in f"{b['title']} {b['body']} {b['action']} {b['help']}"


def test_the_model_visible_notice_says_the_right_thing(monkeypatch):
    monkeypatch.setattr(BH, "read_probe_state", lambda uid: {
        "EXTRACT": {"lane": "EXTRACT", "outcome": "fail",
                    "observed_at": datetime.now(timezone.utc) - timedelta(minutes=2),
                    "elapsed_ms": None, "budget_ms": None, "endpoint_host": None,
                    "reason": BH.REASON_KEY_UNDECRYPTABLE, "detail": None,
                    "outcome_history": ["fail"], "state": BH.STATE_KEY_UNDECRYPTABLE,
                    "transitions": 0}})
    notice = BH.degradation_notice(UID)
    assert notice, "a broken key must not be silent on the recall surface"
    low = notice.lower()
    assert "re-enter" in low and "api key" in low
    for w in _REACHABILITY_WORDS:
        assert w not in low


def test_the_email_subject_is_not_a_reachability_subject():
    """An unknown state falls back to "cannot reach your model endpoint" — the exact wrong
    message, and the one this would have inherited without its own key."""
    subj = SM._BRAIN_ALERT_SUBJECTS.get("key_undecryptable")
    assert subj, "no subject ⇒ silently sends the FAIL subject"
    assert "reach" not in subj.lower()
    assert "key" in subj.lower()


# ── 4 & 5. it emails, exactly once, and keeps emailing nobody while it persists ──────

class _Ledger:
    def __init__(self):
        self.rows: dict = {}

    def read(self, user_id):
        return dict(self.rows.get(str(user_id)) or {})

    def write(self, user_id, account_id, **fields):
        self.rows.setdefault(str(user_id), {}).update(fields)
        return True


@pytest.fixture
def rig(monkeypatch):
    """brain_alerts wired to fakes, with the brain UNBOUND for the key_undecryptable reason.

    ⚠️ `read_probe_state` RAISES here on purpose. The gate this exercises returns before the
    probe stream is touched, and an existing test pins that an unbound seat is never probed —
    reading it would be the tenant connection this whole lane exists to avoid.
    """
    monkeypatch.setenv("SAAS_MODE", "true")
    monkeypatch.delenv("BRAIN_ALERTS_ENABLED", raising=False)
    ledger, sent = _Ledger(), []

    monkeypatch.setattr(BA, "read_state", ledger.read)
    monkeypatch.setattr(BA, "_write_state", ledger.write)
    monkeypatch.setattr(BA, "_brain_is_bound", lambda uid, acct: False)
    monkeypatch.setattr(BA, "_refusal_reason", lambda uid, acct: "key_undecryptable")
    monkeypatch.setattr(BA, "_lookup_individual", lambda uid: {
        "user_id": uid, "account_id": "acct-1", "email": "seat@example.com",
        "display_name": "Seat One", "tenant_context": "house", "can_control_endpoint": True})
    monkeypatch.setattr(BA, "_console_url", lambda: "https://app.example.com/dashboard/user.html")

    def _boom(uid):
        raise AssertionError("the probe stream must not be read for an unbound seat")
    monkeypatch.setattr(BH, "read_probe_state", _boom)

    def _deliver(to, **kw):
        sent.append(dict(to=to, **kw))
        return {"ok": True}
    monkeypatch.setattr(BA, "_deliver", _deliver)
    return types.SimpleNamespace(ledger=ledger, sent=sent, monkeypatch=monkeypatch)


def test_it_emails_where_no_brain_would_have_stayed_silent(rig):
    out = BA.evaluate_seat(UID)
    assert out["state"] == "key_undecryptable"
    assert out["action"] not in ("skipped_no_brain",), \
        "this is a fault, not a setup state — it must not be swallowed by the no-brain gate"
    assert len(rig.sent) == 1, "the whole point: somebody is told"
    assert rig.sent[0]["state"] == "key_undecryptable"
    assert "re-enter" in (rig.sent[0].get("action") or "").lower()


def test_it_does_not_mail_bomb_while_the_condition_persists(rig):
    """THE CONSTRAINT. The CRIT fires on EVERY call, so a naive hook is a mail bomb. The
    dedup is on `alerted_state` — the state last actually SENT about — so a seat broken for
    a week produces ONE email, not one per sweep."""
    BA.evaluate_seat(UID)
    assert len(rig.sent) == 1
    for _ in range(25):
        out = BA.evaluate_seat(UID)
        assert out["action"] in ("no_change", "suppressed")
    assert len(rig.sent) == 1, f"re-sent while the condition persisted: {len(rig.sent)} emails"


def test_a_plain_unbound_seat_still_never_emails(rig):
    """The setup state must stay silent — the new branch must not have widened the gate."""
    rig.monkeypatch.setattr(BA, "_refusal_reason", lambda uid, acct: None)
    out = BA.evaluate_seat(UID)
    assert out["action"] == "skipped_no_brain"
    assert rig.sent == []


def test_unbinding_still_clears_an_open_fault_without_claiming_recovery(rig):
    """Regression guard on the existing contract: with no refusal reason recorded, an
    already-alerted seat that loses its brain is cleared silently, not reported as recovered."""
    rig.monkeypatch.setattr(BA, "_refusal_reason", lambda uid, acct: None)
    rig.ledger.rows[UID] = {"alerted_state": "fail"}
    out = BA.evaluate_seat(UID)
    assert rig.sent == []
    assert out["action"] == "skipped_no_brain"
    assert rig.ledger.rows[UID].get("alerted_state") is None
