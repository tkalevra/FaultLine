# -*- coding: utf-8 -*-
'''seam-5 supersession gate - the critic B1/B2 pins (commit after 971931ec).

B1: the correction-shape inference must fire for BARE PROSE (no category) - captured
    PRE-normalize (normalize maps None to gotcha; the None check was dead code).
B2: the victim link bar counts CONTENT tokens only (stopwords + sub-4-char fragments
    stripped) - "not, the" and deploy-substring-deployment must NOT tombstone unrelated notes.
C:  _CORRECTION_SHAPE_RE raw-string form has no backspace bytes and matches the corpus
    correction shapes.
'''
import pytest

from src.api import cortex


# -- C: regex form -----------------------------------------------------------

def test_shape_regex_has_no_backspace_bytes():
    src = open(cortex.__file__, encoding="utf-8").read()
    assert src.count(chr(8)) == 0, "BACKSPACE 0x08 in cortex.py - raw-string corruption"

def test_shape_regex_matches_corpus_correction_shapes():
    hits = [
        "Actually the staging API port changed: it's 8010 now, not 8001 - we moved it.",
        "Correction on backups: they run at 03:15 these days, not 02:30.",
        "The token changed from X-Api-Token to Y-Api-Token during the rotation.",
        "Use the pooled client instead of the per-call client.",
    ]
    for t in hits:
        assert cortex._CORRECTION_SHAPE_RE.search(t), t

def test_shape_regex_ignores_plain_lessons():
    misses = [
        "Restart the worker to clear the schema cache.",
        "Deploy via the compose env-file form on truenas.",
        "The gauntlet ledger lives in DEV/review.",
    ]
    for t in misses:
        assert not cortex._CORRECTION_SHAPE_RE.search(t), t


# -- B2: content-token bar (pure functions) ----------------------------------

# -- B2: the SAME-FACT link rule (A5 corpus-scale fix) -------------------------

def _ordered(t):
    return [w for w in cortex._tokenize(t)
            if w not in cortex._STOPWORDS and len(w) >= 4]

def test_true_correction_still_links_value_restated():
    # the real corpus-A port pair: short overlap {port, staging}, but the victor
    # restates the old value 8001 — the link MUST fire
    v = "The staging API listens on port 8001, the dashboard proxies to it directly."
    w = "Actually the staging API port changed: it's 8010 now, not 8001 — we moved it."
    ok, why = cortex._supersede_link_decision(_ordered(w), set(_ordered(w)), v, w)
    assert ok and why == "value-restated", (ok, why)

def test_template_chain_still_links_coffee():
    v = "Coffee count today: 13. Do not ask about tomorrow."
    w = "Coffee count today: 12. Do not ask about tomorrow."
    ok, why = cortex._supersede_link_decision(_ordered(w), set(_ordered(w)), v, w)
    assert ok and why == "template-match", (ok, why)

def test_force_flag_note_never_linked_by_force_lease_rule():
    # THE A5 REGRESSION: the --force-with-lease house rule tombstoned the
    # --force-recreate compose note (shared content: force + never). Must refuse.
    v = ("Note to self, and I keep re-learning this: after rebuilding an image with "
         "the same tag, docker compose up -d does nothing to running containers — you "
         "need the --force-recreate flag or the stack silently keeps the old container.")
    w = ("House rule after last month's incident: force-push only with --force-with-lease, "
         "never a bare --force — the lease is what keeps the force push honest.")
    ok, why = cortex._supersede_link_decision(_ordered(w), set(_ordered(w)), v, w)
    assert not ok, "the qF1 killer link must be refused"

def test_generic_domain_pair_refused_port_graph_door():
    # corpus pair 32<-49: {port, staging} shared — "do not confuse" shape, different fact
    v = "The staging API listens on port 8001, the dashboard proxies to it directly."
    w = "Production graph door is on 8443 behind the tunnel — do not confuse it with the staging port."
    ok, why = cortex._supersede_link_decision(_ordered(w), set(_ordered(w)), v, w)
    assert not ok, (ok, why)

def test_stopword_only_overlap_refused_worker_stack():
    v = "the staging api moved to 8010, port 8001 is not in use"
    w = "restart the worker, not the whole stack"
    ok, _ = cortex._supersede_link_decision(_ordered(w), set(_ordered(w)), v, w)
    assert not ok

def test_substring_pair_refused_deploy_deployment():
    v = "update the deployment manifest for the release"
    w = "how to deploy the release, the steps changed recently"
    ok, _ = cortex._supersede_link_decision(_ordered(w), set(_ordered(w)), v, w)
    assert not ok, "deploy inside deployment must not satisfy the bar"

def test_lunch_pair_refused_despite_not_shape():
    # "Not bad honestly" fires the shape regex but the pair is not a correction
    v = "Lunch was the ramen place on Court. Not bad honestly."
    w = "Lunch was the salad bar. Not bad honestly."
    ok, _ = cortex._supersede_link_decision(_ordered(w), set(_ordered(w)), v, w)
    assert not ok, (ok,)


# -- B1: the gate fires for bare prose (fake-conn harness) -------------------

class _FakeCursor:
    def __init__(self, victim_rows):
        self.victim_rows = victim_rows
        self.executed = []
        self._last = ""

    def execute(self, sql, args=None):
        self.executed.append((sql, args))
        self._last = sql

    def fetchone(self):
        return (101, 1, "observed", [])

    def fetchall(self):
        if "FROM cortex_episodes" in self._last and "superseded_by IS NULL" in self._last:
            return self.victim_rows
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, victim_rows):
        self.cur = _FakeCursor(victim_rows)

    def cursor(self):
        return self.cur

    def commit(self):
        pass

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def gate_harness(monkeypatch):
    def _install(victim_rows):
        conn = _FakeConn(victim_rows)
        monkeypatch.setattr(cortex, "cortex_schema_for", lambda u: "flagent_x")
        monkeypatch.setattr(cortex, "_ensure_cortex_schema", lambda u, s: True)
        monkeypatch.setattr(cortex, "_cortex_conn", lambda s: conn)
        monkeypatch.setattr(cortex, "_embed_cortex_text", lambda *a, **k: None)
        monkeypatch.setattr(cortex, "_cortex_partition", lambda u: (None, None))
        return conn
    return _install


def test_bare_prose_correction_tombstones_related_note(gate_harness):
    victim = (55, "the staging api listens on port 8001 for internal traffic")
    conn = gate_harness([victim])
    ok = cortex.note("u1", "the staging api port changed: it's 8010 now, not 8001")
    assert ok is True
    updates = [sql for sql, _ in conn.cur.executed if "SET superseded_by" in sql]
    assert updates, "bare-prose correction never tombstoned (shape inference dead)"


def test_explicit_plain_gotcha_never_opens_the_gate(gate_harness):
    conn = gate_harness([(55, "the staging api listens on port 8001")])
    cortex.note("u1", "Restart the worker to clear the schema cache.", category="gotcha")
    victim_queries = [sql for sql, _ in conn.cur.executed
                      if "FROM cortex_episodes" in sql and "superseded_by IS NULL" in sql]
    assert not victim_queries, "plain gotcha opened the supersession gate"
