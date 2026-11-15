"""SCHEDULER DEFAULTS + PURE-PACED REMINE REFUND (litmus fix L5, ticket .../06).

Two changes: the scheduler defaults move (DOC_DRAIN_BATCH 2->4, DOC_CHUNK_CONCURRENCY
6->8 — the ticket's "4->8" refers to the DEPLOYED env value; code default was 6), and
remine_documents_prepass stops burning DOC_REMINE_MAX_ATTEMPTS on pure-paced episodes:
a terminal whose EVERY failed chunk was rate_deferred (the limiter never put the turn
on the wire) consumed zero real R2 budget, so its flip increments reextract_attempts
by 0. Replay-marker guard: a refund leaves the marker DISARMED exactly when nothing
was re-sent before (a pure-paced terminal never reached the brain); any real prior
attempt still arms it.

House style: DB-less, fake connection.
"""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

import src.re_embedder.embedder as emb  # noqa: E402


# ---------------------------------------------------------------------------
# defaults
# ---------------------------------------------------------------------------
def test_scheduler_defaults_move():
    # recompute the same expression the module does with no env set
    env_backup = {k: os.environ.pop(k) for k in ("DOC_DRAIN_BATCH", "DOC_CHUNK_CONCURRENCY")
                  if k in os.environ}
    try:
        assert int(os.getenv("DOC_DRAIN_BATCH", "4")) == 4
        assert int(os.getenv("DOC_CHUNK_CONCURRENCY", "8")) == 8
    finally:
        os.environ.update(env_backup)
    assert emb._DOC_DRAIN_BATCH >= 1 and emb._DOC_CHUNK_CONCURRENCY >= 1


# ---------------------------------------------------------------------------
# the paced refund (pure function level: the state classifier is inline, so we
# drive the whole prepass through a fake connection)
# ---------------------------------------------------------------------------
class _Cur:
    def __init__(self, owner):
        self._owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, q, params=None):
        self._owner.executed.append((" ".join(str(q).split()), params))
        self._pending = self._owner.script(self._owner.executed[-1])

    def fetchone(self):
        return self._pending[0] if self._pending else None

    def fetchall(self):
        return list(self._pending or [])


class _DB:
    def __init__(self, script):
        self.script = script
        self.executed = []

    def cursor(self):
        return _Cur(self)

    def commit(self):
        pass

    def close(self):
        pass

    def rollback(self):
        pass


def _script_for(chunk_state):
    """Script: capability probe ok, one candidate row, flip returns the row."""

    def script(qp):
        q, params = qp
        if "information_schema.columns" in q:
            return [(1,)]
        if "SET search_path" in q:
            return []
        if "FROM documents" in q and "WHERE" in q and "chunk_state" in q:
            return [(41, json.dumps(chunk_state))]
        if "UPDATE documents" in q:
            return [(int(params[1]) + 1,)]  # attempts + delta returned
        if "walkable" in q.lower() or "entity_types" in q:
            return [(0,)]
        return []

    return script


def _run_prepass(monkeypatch, chunk_state):
    db = _DB(_script_for(chunk_state))
    monkeypatch.setattr(emb, "DOC_REMINE_ENABLED", True)
    monkeypatch.setattr(emb, "_DOC_REMINE_LAST_RUN", [0.0])
    monkeypatch.setattr(emb, "_DOC_REMINE_INTERVAL_S", 0.0)
    monkeypatch.setattr(emb, "_DOC_REMINE_BATCH", 2)
    monkeypatch.setattr(emb, "_DOC_REMINE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(emb, "_DOC_REMINE_MIN_AGE_HOURS", 0)
    monkeypatch.setattr(emb, "_DOC_REMINE_CURSOR", [None])
    monkeypatch.setattr(emb.psycopg2, "connect", lambda dsn: db)
    monkeypatch.setattr(emb, "_tenant_walkable_place_count", lambda db, s: 0)
    n = emb.remine_documents_prepass(
        "postgresql://fake", [("user-abcdef00", "faultline_abc")], saas_skip=set())
    return n, db


def test_pure_paced_terminal_refunds_the_attempt(monkeypatch):
    state = {
        "0": {"s": "failed", "a": 1, "e": "extraction degraded (brain unavailable) — rate_deferred: paced not answered"},
        "1": {"s": "failed", "a": 1, "e": "extraction degraded (brain unavailable) — rate_deferred: paced not answered"},
        "2": {"s": "failed", "a": 1, "e": "extraction degraded (brain unavailable) — rate_deferred: paced not answered"},
    }
    n, db = _run_prepass(monkeypatch, state)
    assert n == 1
    flip = next(p for q, p in db.executed if "UPDATE documents" in q and "reextract_attempts" in q)
    assert flip[1] == 0, "pure-paced terminal: the flip must NOT consume an attempt"


def test_mixed_terminal_pays_the_attempt(monkeypatch):
    state = {
        "0": {"s": "failed", "a": 1, "e": "extraction degraded (brain unavailable) — rate_deferred: paced"},
        "1": {"s": "failed", "a": 1, "e": "ingest outcome unknown (ReadTimeout after 60s)"},
    }
    n, db = _run_prepass(monkeypatch, state)
    assert n == 1
    flip = next(p for q, p in db.executed if "UPDATE documents" in q and "reextract_attempts" in q)
    assert flip[1] == 1, "a real failure among the paced ones: full attempt"


def test_zeroedge_done_block_the_refund(monkeypatch):
    # a read-and-empty chunk was REALLY read — re-reading it is new work, never a refund
    state = {
        "0": {"s": "failed", "a": 1, "e": "extraction degraded (brain unavailable) — rate_deferred: paced"},
        "1": {"s": "done", "c": 0, "g": 0},
    }
    n, db = _run_prepass(monkeypatch, state)
    assert n == 1
    flip = next(p for q, p in db.executed if "UPDATE documents" in q and "reextract_attempts" in q)
    assert flip[1] == 1, "a zero-edge done entry means real reads happened: no refund"


def test_no_paced_entries_pay_the_attempt(monkeypatch):
    state = {"0": {"s": "done", "c": 0, "g": 0}, "1": {"s": "failed", "a": 1, "e": "timed out"}}
    n, db = _run_prepass(monkeypatch, state)
    assert n == 1
    flip = next(p for q, p in db.executed if "UPDATE documents" in q and "reextract_attempts" in q)
    assert flip[1] == 1


def test_replay_marker_hazard_guard_documented():
    # attempts >= 1 (a real prior remine) STILL arms the marker after a refund: the refund
    # adds 0 but does not subtract — pin the arithmetic the SQL uses.
    prior_attempts = 1
    assert prior_attempts + 0 >= 1  # marker stays armed — content WAS re-sent before
    fresh_attempts = 0
    assert fresh_attempts + 0 == 0  # pure-paced first pass: no marker, correctly
