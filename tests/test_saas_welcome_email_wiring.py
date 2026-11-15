"""WELCOME-EMAIL wiring — the self-serve signup path must actually SEND it.

Regression guarded: `send_welcome_email` existed and was correct, but was reachable from ONE
caller only — the operator APPROVE endpoint (saas/admin.py). With the approval valve OFF (the
production default, platform_settings.require_signup_approval = false) `me_signup` returns
{"status":"authenticated"} directly and never went near it, so every self-serve signup got NO
welcome email. Prod logs showed `individual signup OK` with no email line at all.

That is a WIRING bug, not a sender bug — so these tests assert the CALL, not the SMTP mechanics.
The last test is the important one: it fails if someone deletes the call from the signup path
while leaving the (perfectly good) sender in place, which is exactly how this shipped broken.

Pure unit tests — no DB, no TestClient, no network. SAAS_MODE + the test secrets are the
suite-wide baseline set once in tests/conftest.py, not written into os.environ here (a
module-level write is process-wide and made import order decide other modules' configuration).
Run: python3 tools/fltest.py --bug WELCOME-WIRE --test tests/test_saas_welcome_email_wiring.py
"""

import inspect

from saas import admin as ad
from saas import user_console as uc


class _Sender:
    """Stand-in for saas.smtp_sender.send_welcome_email — records the call."""

    def __init__(self, result=None, boom=False):
        self.calls = []
        self.result = result if result is not None else {"ok": True}
        self.boom = boom

    def send_welcome_email(self, to, **kw):
        self.calls.append((to, kw))
        if self.boom:
            raise RuntimeError("relay exploded")
        return self.result


def _patch(monkeypatch, sender, *, base_url="https://app.faultline.ca/dashboard/user.html"):
    monkeypatch.setattr(uc, "_smtp_sender", sender)
    monkeypatch.setattr(uc, "_smtp_config",
                        type("C", (), {"resolve_config": staticmethod(
                            lambda: {"reset_link_base_url": base_url})})())
    monkeypatch.setattr(uc, "_platform_settings",
                        type("P", (), {"welcome_from_address": staticmethod(
                            lambda: "noreply@faultline.ca")})())


# ── the shared sender behaves ────────────────────────────────────────────────────────

def test_welcome_is_sent_with_console_link_and_from_address(monkeypatch):
    s = _Sender()
    _patch(monkeypatch, s)
    out = uc.send_welcome_to_individual({"email": "a@b.com", "display_name": "Ada"})
    assert out["delivery"] == "emailed"
    (to, kw), = s.calls
    assert to == "a@b.com"
    assert kw["display_name"] == "Ada"
    assert kw["from_address"] == "noreply@faultline.ca"
    # console_url is derived from the configured reset-link origin, deep-linked to the console.
    assert kw["console_url"] == "https://app.faultline.ca/dashboard/user.html"


def test_no_address_is_reported_not_sent(monkeypatch):
    s = _Sender()
    _patch(monkeypatch, s)
    assert uc.send_welcome_to_individual({"email": "  "})["delivery"] == "no_address"
    assert s.calls == []


def test_unconfigured_relay_reports_failure_without_raising(monkeypatch):
    # Exactly today's PROD state: SMTP row present but enabled=false → sender returns ok:false.
    s = _Sender(result={"ok": False, "error": "SMTP is configured but not enabled"})
    _patch(monkeypatch, s)
    out = uc.send_welcome_to_individual({"email": "a@b.com"})
    assert out["delivery"] == "email_failed"
    assert "not enabled" in out["delivery_error"]


def test_sender_exception_never_escapes(monkeypatch):
    # A courtesy email must never fail a signup that has already created the account.
    s = _Sender(boom=True)
    _patch(monkeypatch, s)
    assert uc.send_welcome_to_individual({"email": "a@b.com"})["delivery"] == "email_failed"


def test_missing_base_url_still_sends_without_a_link(monkeypatch):
    s = _Sender()
    _patch(monkeypatch, s, base_url="")
    assert uc.send_welcome_to_individual({"email": "a@b.com"})["delivery"] == "emailed"
    (_to, kw), = s.calls
    assert kw["console_url"] is None


# ── ONE definition, TWO doors — approve and self-serve must not drift ────────────────

def test_admin_approve_delegates_to_the_shared_sender(monkeypatch):
    seen = {}

    def _shared(target):
        seen["target"] = target
        return {"delivery": "emailed"}

    monkeypatch.setattr(uc, "send_welcome_to_individual", _shared)
    out = ad._send_welcome({"email": "a@b.com", "display_name": "Ada"})
    assert out == {"delivery": "emailed"}
    assert seen["target"]["email"] == "a@b.com"


# ── THE regression guard: the self-serve path must actually invoke it ────────────────

def test_self_serve_signup_path_invokes_the_welcome_sender():
    """The bug: sender present, caller absent. Assert me_signup names the shared sender.

    Source inspection is deliberate — driving the real handler needs a live DB, and a mocked
    run would prove the mock, not the wiring. This fails loudly if the call is ever removed.
    """
    src = inspect.getsource(uc.me_signup)
    assert "send_welcome_to_individual" in src, (
        "me_signup no longer sends the welcome email — self-serve signups would go out silent "
        "again (the approval-OFF path is the ONLY out-of-band contact the user gets)"
    )
