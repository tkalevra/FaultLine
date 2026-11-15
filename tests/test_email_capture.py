"""EMAIL CAPTURE GATE — every existing email caller routes through capture in test mode; the
kill switch short-circuits BEFORE capture. CLOSED-SOURCE.

What this pins:
  * an existing transactional email (the reset email) routes through the capture lane when
    capture is active — a row lands in public.email_outbox and _open_session is NEVER called
    (the "no real SMTP in test mode" guarantee applies to every caller, not just the new
    retention-email path);
  * the KILL SWITCH short-circuits BEFORE the capture lane — order matters (kill wins over
    capture). With the kill switch on, no outbox row is written and no SMTP session is opened.

Pure unit tests — DB-backed capture is mocked via a fake-connection context manager. Run:

  python3 tools/fltest.py --bug RETENTION-EMAILS --test tests/test_email_capture.py
"""

import threading
import uuid
from datetime import datetime, timezone

import pytest

from saas import email_outbox as outbox
from saas import platform_settings as plat
from saas import smtp_sender as smtp


# ══════════════════════════════════════════════════════════════════════════════════
# FAKES — same shape as tests/test_retention_emails.py (kept duplicated so each file stands alone)
# ══════════════════════════════════════════════════════════════════════════════════

import contextlib
import json


class _FakeCursor:
    def __init__(self, state):
        self.state = state
        self.rowcount = 0
        self._last = None

    def execute(self, sql, params=None):
        s = (sql or "").strip()
        u = s.upper()
        if "EMAIL_OUTBOX" in u and u.startswith("INSERT"):
            self.state["outbox_id"] += 1
            new_id = self.state["outbox_id"]
            self.state["outbox"].append({
                "id": new_id,
                "captured_at": datetime.now(timezone.utc),
                "transport": "capture",
                "to_address": params[0],
                "from_address": params[1],
                "subject": params[2],
                "text_body": params[3],
                "html_body": params[4],
                "reason": params[5],
                "related_user_id": params[6],
                "metadata": json.loads(params[7]) if params[7] else {},
            })
            self.rowcount = 1
            self._last = (new_id,)
            return
        self.rowcount = 0
        self._last = None

    def fetchone(self):
        return self._last

    def fetchall(self):
        return self._last or []

    def close(self):
        pass

    # Cursor is used as a context manager (`with conn.cursor() as cur`) — fake the protocol.
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, state):
        self.state = state

    def cursor(self):
        return _FakeCursor(self.state)

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def fake_outbox(monkeypatch):
    state = {"outbox": [], "outbox_id": 0, "lock": threading.Lock()}

    @contextlib.contextmanager
    def _conn():
        yield _FakeConn(state)

    monkeypatch.setattr(outbox, "db", type("D", (), {"connection": staticmethod(_conn)})())
    # Also reset the cached payment-mode read so each test sees its own env.
    smtp._payment_mode_cache["value"] = None
    smtp._payment_mode_cache["at"] = 0.0
    return state


@pytest.fixture(autouse=True)
def _clear_platform_cache():
    plat._cache = None
    plat._cache_at = 0.0
    yield
    plat._cache = None
    plat._cache_at = 0.0


@pytest.fixture(autouse=True)
def _no_smtp_session(monkeypatch):
    """FAIL-LOUD guard: _open_session must NEVER be called when capture is active. If it is,
    the test fails immediately."""
    def _boom(*a, **kw):
        raise AssertionError("_open_session was called while capture mode is active — "
                             "a sandbox send would have escaped to a real relay")
    monkeypatch.setattr(smtp, "_open_session", _boom)


# ══════════════════════════════════════════════════════════════════════════════════
# (1) existing callers (send_reset_email) route through capture in test mode
# ══════════════════════════════════════════════════════════════════════════════════

def test_existing_callers_route_through_capture(monkeypatch, fake_outbox):
    """send_reset_email in test mode → capture row written; _open_session NEVER called
    (the autouse _no_smtp_session fixture turns any call into an AssertionError)."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_EMAIL_CAPTURE_ONLY", "1")  # force capture regardless of payment cfg
    monkeypatch.setenv("SAAS_EMAIL_KILL_SWITCH", "0")

    res = smtp.send_reset_email("user@example.com", "https://reset.example/x")

    assert res.get("ok") is True
    assert res.get("captured") is True
    assert len(fake_outbox["outbox"]) == 1
    row = fake_outbox["outbox"][0]
    assert row["to_address"] == "user@example.com"
    assert row["reason"] == "transactional"  # reset email doesn't pass a reason → default
    assert "https://reset.example/x" in row["text_body"]


# ══════════════════════════════════════════════════════════════════════════════════
# (2) KILL SWITCH short-circuits BEFORE capture (order matters)
# ══════════════════════════════════════════════════════════════════════════════════

def test_kill_switch_short_circuits_before_capture(monkeypatch, fake_outbox):
    """Both kill switch AND capture are active. Kill wins: NO outbox row, NO SMTP session,
    returns killed:True."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_EMAIL_CAPTURE_ONLY", "1")    # capture would catch it...
    monkeypatch.setenv("SAAS_EMAIL_KILL_SWITCH", "1")     # ...but the kill switch fires FIRST

    res = smtp.send_reset_email("user@example.com", "https://reset.example/x")

    assert res.get("ok") is False
    assert res.get("killed") is True
    # no capture row, no SMTP session
    assert len(fake_outbox["outbox"]) == 0
