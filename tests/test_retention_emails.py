"""EMAIL LIFECYCLE — retention warning + farewell emails (migrations 238/239/240). CLOSED-SOURCE.

What this pins:
  * the WARNING email is captured in test mode (no SMTP session opened) + the dedupe row is written;
  * EXACTLY-ONCE under replay (call send_warning twice → second returns skipped:already_sent);
  * EXACTLY-ONCE under restart (a pre-existing dedupe row → send_warning skips; the arbiter is
    DB-backed, not in-memory);
  * ALERT-NOT-SEND when the retention value is unreadable (platform_settings degraded=True →
    send_warning alerts, returns without sending, NO outbox/log row, log_crit fires);
  * the copy interpolates the LIVE retention value (set 14, assert "14 days" in the body, NEVER 7);
  * the FAREWELL contains both https://volenti.ca and https://faultline.ca URLs;
  * the KILL SWITCH suppresses everything (no outbox, no log, returns killed:True);
  * the daily sweep warns two days before purge-eligibility and farewells on the day, and is
    idempotent under restart (call sweep twice → no double sends).

Pure unit tests — DB is mocked via a fake-connection context manager so the suite runs without a
live Postgres (mirrors the test_saas_retention.py style). SAAS_MODE + the test secrets are the
suite-wide baseline set once in tests/conftest.py.

Run: python3 tools/fltest.py --bug RETENTION-EMAILS --test tests/test_retention_emails.py
"""

import json
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from saas import email_outbox as outbox
from saas import platform_settings as plat
from saas import retention_emails as re


# ══════════════════════════════════════════════════════════════════════════════════
# FAKES — a tiny in-memory Postgres that backs the dedupe INSERT + the capture table
# ══════════════════════════════════════════════════════════════════════════════════

class _FakeCursor:
    """Mimics enough of psycopg2's cursor for the two SQL paths under test:
      * INSERT INTO public.retention_email_log ... ON CONFLICT DO NOTHING (returns rowcount)
      * INSERT INTO public.email_outbox ... RETURNING id (returns rowcount + a row)
      * SELECT/DELETE on either table (best-effort).
    """

    def __init__(self, state):
        self.state = state
        self.rowcount = 0
        self._last = None

    def execute(self, sql, params=None):
        s = (sql or "").strip()
        u = s.upper()
        # ---- retention_email_log INSERT ON CONFLICT DO NOTHING (the exactly-once arbiter) ----
        if "RETENTION_EMAIL_LOG" in u and u.startswith("INSERT"):
            # params = (user_id, email_kind, transport)
            uid, kind, transport = params
            key = (str(uid), str(kind))
            with self.state["lock"]:
                if key in self.state["email_log"]:
                    self.rowcount = 0
                    self._last = None
                else:
                    self.state["email_log"][key] = {
                        "related_user_id": str(uid),
                        "email_kind": kind,
                        "transport": transport,
                        "sent_at": datetime.now(timezone.utc),
                    }
                    self.rowcount = 1
                    self._last = None
            return
        # ---- email_outbox INSERT RETURNING id ----
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
        # ---- SELECT 1 FROM retention_email_log (has_retention_email) ----
        if "RETENTION_EMAIL_LOG" in u and u.startswith("SELECT"):
            # only the EXISTS-style probe used by has_retention_email
            uid, kind = params
            with self.state["lock"]:
                ok = (str(uid), str(kind)) in self.state["email_log"]
            self.rowcount = 1 if ok else 0
            self._last = (1,) if ok else None
            return
        # ---- SELECT on email_outbox (list_recent) ----
        if "EMAIL_OUTBOX" in u and u.startswith("SELECT"):
            rows = list(reversed(self.state["outbox"]))  # newest first
            # limit is the last param
            try:
                limit = int(params[-1]) if params else 1000
            except Exception:
                limit = 1000
            self._last = [(
                r["id"], r["captured_at"], r["transport"], r["to_address"],
                r["from_address"], r["subject"], r["text_body"], r["html_body"],
                r["reason"], (uuid.UUID(r["related_user_id"]) if r["related_user_id"] else None),
                (r["metadata"] if isinstance(r["metadata"], dict) else json.dumps(r["metadata"])),
            ) for r in rows[:limit]]
            return
        # ---- SELECT on retention_email_log (list_retention_log) ----
        if u.startswith("SELECT") and "RETENTION_EMAIL_LOG" in u:
            rows = list(self.state["email_log"].values())
            self._last = [(r["related_user_id"], r["email_kind"], r["sent_at"], r["transport"])
                          for r in rows]
            return
        # ---- generic DELETE on outbox ----
        if u.startswith("DELETE") and "EMAIL_OUTBOX" in u:
            n = len(self.state["outbox"])
            self.state["outbox"].clear()
            self.rowcount = n
            return
        # ---- UPDATE platform_settings (no-op for these tests; the cached state drives the gates) ----
        if u.startswith("UPDATE"):
            self.rowcount = 1
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
def fake_db(monkeypatch):
    """Replace the saas.db.connection() context manager with one backed by an in-memory state."""
    state = {
        "email_log": {},   # (user_id, email_kind) → row
        "outbox": [],      # list of rows
        "outbox_id": 0,
        "lock": threading.Lock(),
    }

    import contextlib

    @contextlib.contextmanager
    def _conn():
        yield _FakeConn(state)

    # Patch the package-level connection (some modules do `from . import db` and call db.connection).
    import saas.db as saas_db
    monkeypatch.setattr(saas_db, "connection", _conn)
    # Patch outbox's `db` reference too (record_capture uses module-level `db`).
    monkeypatch.setattr(outbox, "db", type("D", (), {"connection": staticmethod(_conn)})())
    # retention_emails does `from . import db` LAZILY inside _claim_exactly_once; replace the
    # whole claim with an in-memory mirror so the test does not depend on the lazy import.
    monkeypatch.setattr(re, "_claim_exactly_once",
                        lambda uid, kind, transport: _claim_via(state, uid, kind, transport))
    # Make the alert-record path (which calls outbox.record_capture) use the same fake.
    # No-op needed: record_capture uses outbox.db which is patched above.
    return state


def _claim_via(state, user_id, email_kind, transport):
    """In-memory mirror of the real ON-CONFLICT-DO-NOTHING claim. Matches _FakeCursor's INSERT
    branch exactly so the dedupe semantics are consistent across both code paths."""
    key = (str(user_id), str(email_kind))
    with state["lock"]:
        if key in state["email_log"]:
            return False
        state["email_log"][key] = {
            "related_user_id": str(user_id),
            "email_kind": email_kind,
            "transport": transport,
            "sent_at": datetime.now(timezone.utc),
        }
    return True


# ══════════════════════════════════════════════════════════════════════════════════
# Common per-test setup
# ══════════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _baseline_env(monkeypatch):
    """Per-test baseline: SAAS on, capture mode forced (so send_email routes to outbox), payment
    mode=test, feature flag ON, kill switch OFF. Each test overrides what it needs."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_EMAIL_CAPTURE_ONLY", "1")
    monkeypatch.setenv("SAAS_EMAIL_KILL_SWITCH", "0")
    # feature flag ON via env override (the feature ships DARK otherwise)
    monkeypatch.setenv("SAAS_RETENTION_EMAILS_ENABLED", "1")


@pytest.fixture(autouse=True)
def _clear_platform_cache():
    plat._cache = None
    plat._cache_at = 0.0
    yield
    plat._cache = None
    plat._cache_at = 0.0


@pytest.fixture(autouse=True)
def _no_smtp_session(monkeypatch):
    """FAIL-LOUD guard: _open_session must NEVER be called when capture is active. If it is, the
    test fails immediately (this is the "no real SMTP in test mode" guarantee)."""
    import saas.smtp_sender as smtp

    def _boom(*a, **kw):
        raise AssertionError("_open_session was called while capture mode is active — "
                             "a sandbox send would have escaped to a real relay")
    monkeypatch.setattr(smtp, "_open_session", _boom)


def _individual(uid="usr-aaa", email="alma@example.com",
                trial_ends_at_iso=None) -> dict:
    return {"individual_id": "ind-1", "user_id": uid, "account_id": "acc-1",
            "email": email, "trial_ends_at": trial_ends_at_iso,
            "display_name": "Alma"}


# ══════════════════════════════════════════════════════════════════════════════════
# (1) WARNING — captured in test mode (no SMTP session opened)
# ══════════════════════════════════════════════════════════════════════════════════

def test_warning_send_captures_in_test_mode(monkeypatch, fake_db):
    """send_warning writes to public.email_outbox + public.retention_email_log; no SMTP session
    opened (the autouse _no_smtp_session fixture turns any _open_session call into a test failure)."""
    monkeypatch.setattr(plat, "state", lambda **k: {"retention_grace_days": 7, "degraded": False})
    monkeypatch.setattr(plat, "retention_grace_days", lambda: 7)

    res = re.send_warning(_individual())

    assert res.get("ok") is True
    assert res.get("captured") is True
    assert len(fake_db["outbox"]) == 1
    assert fake_db["outbox"][0]["reason"] == "retention_warning"
    assert fake_db["outbox"][0]["to_address"] == "alma@example.com"
    assert ("usr-aaa", "warning") in fake_db["email_log"]


# ══════════════════════════════════════════════════════════════════════════════════
# (2) EXACTLY-ONCE under replay (call send_warning twice for the same user)
# ══════════════════════════════════════════════════════════════════════════════════

def test_warning_exactly_once_under_replay(monkeypatch, fake_db):
    monkeypatch.setattr(plat, "state", lambda **k: {"retention_grace_days": 7, "degraded": False})
    monkeypatch.setattr(plat, "retention_grace_days", lambda: 7)

    first = re.send_warning(_individual())
    second = re.send_warning(_individual())

    assert first.get("ok") is True
    assert second.get("ok") is True
    assert second.get("skipped") == "already_sent"
    # EXACTLY one outbox row across the two calls
    assert len(fake_db["outbox"]) == 1
    # EXACTLY one dedupe log row
    assert len(fake_db["email_log"]) == 1


# ══════════════════════════════════════════════════════════════════════════════════
# (3) EXACTLY-ONCE under restart simulated — a pre-existing dedupe row skips the send
# ══════════════════════════════════════════════════════════════════════════════════

def test_warning_exactly_once_under_restart_simulated(monkeypatch, fake_db):
    """Pre-seed the dedupe row (simulating a previous process that already sent), then call
    send_warning. It must skip — proving the arbiter is DB-backed, not in-memory."""
    monkeypatch.setattr(plat, "state", lambda **k: {"retention_grace_days": 7, "degraded": False})
    monkeypatch.setattr(plat, "retention_grace_days", lambda: 7)
    # pre-populate the dedupe row
    fake_db["email_log"][("usr-aaa", "warning")] = {
        "related_user_id": "usr-aaa", "email_kind": "warning",
        "transport": "capture", "sent_at": datetime.now(timezone.utc) - timedelta(hours=2),
    }

    res = re.send_warning(_individual())

    assert res.get("ok") is True
    assert res.get("skipped") == "already_sent"
    # no NEW outbox row
    assert len(fake_db["outbox"]) == 0
    # the dedupe row is unchanged (still exactly one)
    assert len(fake_db["email_log"]) == 1


# ══════════════════════════════════════════════════════════════════════════════════
# (4) ALERT-NOT-SEND when retention value is unreadable
# ══════════════════════════════════════════════════════════════════════════════════

def test_warning_blocked_when_retention_unreadable(monkeypatch, fake_db):
    """platform_settings.state() degraded=True → send_warning alerts, NO send, log_crit called."""
    monkeypatch.setattr(plat, "state", lambda **k: {"retention_grace_days": 7, "degraded": True})

    crit_calls = []
    monkeypatch.setattr(re, "_log_crit", lambda msg: crit_calls.append(msg))

    res = re.send_warning(_individual())

    assert res.get("ok") is False
    assert res.get("alerted") is True
    assert res.get("reason") == "retention_value_unreadable"
    # NO outbox row from the send path (the alert record uses reason='alert', distinct from a send)
    sends = [r for r in fake_db["outbox"] if r["reason"] != "alert"]
    assert len(sends) == 0
    # NO dedupe row claimed (alert-not-send does not consume the exactly-once slot — a later,
    # healthy sweep can still send)
    assert ("usr-aaa", "warning") not in fake_db["email_log"]
    # log_crit fired (the operator-visible failure signal)
    assert any("retention value unreadable" in c for c in crit_calls)


# ══════════════════════════════════════════════════════════════════════════════════
# (5) the copy interpolates the LIVE retention value (NEVER a literal 7)
# ══════════════════════════════════════════════════════════════════════════════════

def test_warning_copy_contains_live_retention_value(monkeypatch, fake_db):
    """Set retention_grace_days=14, send the warning, assert the body contains '14 days'
    (NOT a hardcoded 7)."""
    monkeypatch.setattr(plat, "state", lambda **k: {"retention_grace_days": 14, "degraded": False})
    monkeypatch.setattr(plat, "retention_grace_days", lambda: 14)

    res = re.send_warning(_individual())
    assert res.get("ok") is True

    body = fake_db["outbox"][0]["text_body"]
    assert "your account and information will be removed after 14 days" in body, body
    # never a hardcoded 7
    assert "removed after 7 days" not in body


# ══════════════════════════════════════════════════════════════════════════════════
# (6) the farewell contains both canonical URLs
# ══════════════════════════════════════════════════════════════════════════════════

def test_farewell_contains_volenti_and_faultline_urls(monkeypatch, fake_db):
    monkeypatch.setattr(plat, "state", lambda **k: {"retention_grace_days": 7, "degraded": False})
    monkeypatch.setattr(plat, "retention_grace_days", lambda: 7)

    res = re.send_farewell(_individual())
    assert res.get("ok") is True

    body = fake_db["outbox"][0]["text_body"]
    html = fake_db["outbox"][0]["html_body"]
    assert "https://volenti.ca" in body
    assert "https://faultline.ca" in body
    assert "https://volenti.ca" in html
    assert "https://faultline.ca" in html
    # modest — no CTA button on the farewell
    assert "Open this link" not in body


# ══════════════════════════════════════════════════════════════════════════════════
# (7) KILL SWITCH suppresses everything (no capture, no log row)
# ══════════════════════════════════════════════════════════════════════════════════

def test_kill_switch_suppresses_everything(monkeypatch, fake_db):
    monkeypatch.setenv("SAAS_EMAIL_KILL_SWITCH", "1")
    monkeypatch.setattr(plat, "state", lambda **k: {"retention_grace_days": 7, "degraded": False})
    monkeypatch.setattr(plat, "retention_grace_days", lambda: 7)

    res = re.send_warning(_individual())

    assert res.get("ok") is False
    assert res.get("killed") is True
    # NO outbox row written (kill switch short-circuits BEFORE the capture lane)
    assert len(fake_db["outbox"]) == 0
    # NO dedupe row claimed — kill-switch is not a send, so the exactly-once slot survives
    assert ("usr-aaa", "warning") not in fake_db["email_log"]


# ══════════════════════════════════════════════════════════════════════════════════
# (8) SWEEP — warns two days before, farewells on the day
# ══════════════════════════════════════════════════════════════════════════════════

def test_sweep_warns_two_days_before_and_farewells_on_day(monkeypatch, fake_db):
    """Seed two candidates: one whose (trial_ends_at + grace - 2) is NOW (warn), and one whose
    (trial_ends_at + grace) is NOW (farewell). sweep() should send the on-time warning AND the
    farewell — PLUS a BACKFILLED warning for the farewell-day candidate (its 2-day-before warn
    window was 'missed' because the test seeds it straight at farewell-day, simulating a sweep
    that was down during the warn window). The backfill is the spec-correct behavior: a user
    who never received the warning gets it before the farewell fires, never silently dropped."""
    now = datetime.now(timezone.utc)
    grace = 7
    warn_iso = (now - timedelta(days=grace - 2)).isoformat()  # trial ended 5d ago → warn due
    fare_iso = (now - timedelta(days=grace)).isoformat()      # trial ended 7d ago → farewell due

    monkeypatch.setattr(plat, "state", lambda **k: {"retention_grace_days": grace, "degraded": False})
    monkeypatch.setattr(plat, "retention_grace_days", lambda: grace)

    warn_cand = _individual(uid="warn-1", email="warn@example.com", trial_ends_at_iso=warn_iso)
    fare_cand = _individual(uid="fare-1", email="fare@example.com", trial_ends_at_iso=fare_iso)
    # list_candidates is called with grace - WARNING_LEAD_DAYS = 5 → both candidates returned
    monkeypatch.setattr(re._retention, "list_candidates",
                        lambda **k: [warn_cand, fare_cand])

    summary = re.sweep()

    # warn-1 gets its on-time warning; fare-1 gets a BACKFILLED warning (its warn window passed
    # with no prior send) AND the farewell. Total: 2 warnings + 1 farewell. This is the
    # missed-warning backfill working — the OLD behavior (warn-XOR-farewell) would silently
    # drop fare-1's warning forever, violating the spec's "every expiring individual must be
    # emailed exactly once two days before expiry" when the sweep is late.
    assert summary["warned"] == 2, f"expected warn-1 + fare-1 backfill; got {summary}"
    assert summary["farewelled"] == 1
    assert summary["killed"] == 0
    assert summary["alerted_unreadable"] == 0
    # outbox: one farewell + two warnings (warn-1 + fare-1 backfill)
    reasons = sorted(r["reason"] for r in fake_db["outbox"])
    assert reasons == ["retention_farewell", "retention_warning", "retention_warning"], reasons
    # dedupe log: warn-1 warning, fare-1 warning (backfill), fare-1 farewell — all exactly-once
    assert ("warn-1", "warning") in fake_db["email_log"]
    assert ("fare-1", "warning") in fake_db["email_log"], "fare-1 backfilled warning must be logged"
    assert ("fare-1", "farewell") in fake_db["email_log"]


# ══════════════════════════════════════════════════════════════════════════════════
# (9) SWEEP is idempotent under restart (call twice → no double sends)
# ══════════════════════════════════════════════════════════════════════════════════

def test_sweep_idempotent_under_restart(monkeypatch, fake_db):
    now = datetime.now(timezone.utc)
    grace = 7
    warn_iso = (now - timedelta(days=grace - 2)).isoformat()

    monkeypatch.setattr(plat, "state", lambda **k: {"retention_grace_days": grace, "degraded": False})
    monkeypatch.setattr(plat, "retention_grace_days", lambda: grace)
    warn_cand = _individual(uid="warn-1", email="warn@example.com", trial_ends_at_iso=warn_iso)
    monkeypatch.setattr(re._retention, "list_candidates", lambda **k: [warn_cand])

    first = re.sweep()
    second = re.sweep()

    assert first["warned"] == 1
    assert second["warned"] == 0
    assert second["skipped_already"] == 1
    # exactly one outbox row across both sweeps
    assert len([r for r in fake_db["outbox"] if r["reason"] == "retention_warning"]) == 1


# ══════════════════════════════════════════════════════════════════════════════════
# (10) NO real SMTP in test mode — _open_session is never called (autouse fixture above
#      turns any call into an AssertionError). This test exists so the assertion is named.
# ══════════════════════════════════════════════════════════════════════════════════

def test_no_real_smtp_in_test_mode(monkeypatch, fake_db):
    monkeypatch.setattr(plat, "state", lambda **k: {"retention_grace_days": 7, "degraded": False})
    monkeypatch.setattr(plat, "retention_grace_days", lambda: 7)
    # If capture weren't active, _open_session would fire and the autouse _no_smtp_session
    # fixture would raise AssertionError here. Reaching the asserts proves capture won.
    res = re.send_warning(_individual())
    assert res.get("ok") or res.get("skipped") or res.get("killed") or res.get("alerted")


# ══════════════════════════════════════════════════════════════════════════════════
# (11) FAREWELL exactly-once under replay + restart-simulated (the spec says BOTH emails are
#      exactly-once; previously only the warning had these tests). The farewell uses a DIFFERENT
#      dedupe key ('farewell') + a different date predicate (on-or-after backfill) — both must
#      be exactly-once independently.
# ══════════════════════════════════════════════════════════════════════════════════

def test_farewell_exactly_once_under_replay(monkeypatch, fake_db):
    """Calling send_farewell twice for the same user sends ONCE; second returns skipped."""
    monkeypatch.setattr(plat, "state", lambda **k: {"retention_grace_days": 7, "degraded": False})
    monkeypatch.setattr(plat, "retention_grace_days", lambda: 7)
    cand = _individual()
    r1 = re.send_farewell(cand)
    r2 = re.send_farewell(cand)
    assert r1.get("ok"), f"first send should succeed: {r1}"
    assert r2.get("skipped") == "already_sent", f"second send should skip: {r2}"
    farewell_rows = [r for r in fake_db["outbox"] if r["reason"] == "retention_farewell"]
    assert len(farewell_rows) == 1, f"exactly one farewell outbox row, got {len(farewell_rows)}"


def test_farewell_exactly_once_under_restart_simulated(monkeypatch, fake_db):
    """A farewell dedupe row written 'before restart' (simulated by a direct INSERT) makes the
    next send_farewell skip — proves the dedupe is DB-backed, not in-memory."""
    monkeypatch.setattr(plat, "state", lambda **k: {"retention_grace_days": 7, "degraded": False})
    monkeypatch.setattr(plat, "retention_grace_days", lambda: 7)
    cand = _individual()
    # simulate a prior send by pre-writing the dedupe row directly
    fake_db["email_log"][(cand["user_id"], "farewell")] = {"sent_at": "prior"}
    r = re.send_farewell(cand)
    assert r.get("skipped") == "already_sent", f"pre-logged farewell should skip: {r}"
    farewell_rows = [r for r in fake_db["outbox"] if r["reason"] == "retention_farewell"]
    assert len(farewell_rows) == 0, "no outbox row when the dedupe slot was pre-claimed"


def test_farewell_kill_switch_suppresses(monkeypatch, fake_db):
    """The kill-switch suppresses the farewell send (no outbox row, no dedupe row consumed)."""
    monkeypatch.setattr(plat, "state", lambda **k: {"retention_grace_days": 7, "degraded": False})
    monkeypatch.setattr(plat, "retention_grace_days", lambda: 7)
    monkeypatch.setenv("SAAS_EMAIL_KILL_SWITCH", "1")
    cand = _individual()
    r = re.send_farewell(cand)
    assert r.get("killed") is True, f"kill-switch should suppress farewell: {r}"
    # kill-switch returns BEFORE claiming the dedupe slot, so a later (un-killed) send still fires
    farewell_rows = [r for r in fake_db["outbox"] if r["reason"] == "retention_farewell"]
    assert len(farewell_rows) == 0
    assert (cand["user_id"], "farewell") not in fake_db["email_log"]
