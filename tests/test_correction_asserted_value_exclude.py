"""Correction supersede vs. the intent-independent-harvest RACE (asserted-value exclusion).

ROOT CAUSE under test
---------------------
``remember_facts_tool`` fires an INTENT-INDEPENDENT HARVEST (src/mcp/server.py) that
ingests a correction's ASSERTED NEW value ("red") as a LIVE ``(subject, rel)`` fact
BEFORE the correction is routed to ``/retract/correct``. So by the time ``correct_fact``
runs its old-value lookup, the ``facts`` table already holds TWO live ``favorite_colour``
rows — the stored OLD value (blue) AND the harvest-added NEW value (red).

When the correction extractor's ``old_value`` is absent / unresolvable (observed live:
"Actually, my favorite colour is red." yields no old value), the object-anchored steps
(1/2/7) miss and the subject-only step sees >1 row and BAILS (clarification / not-found)
superseding NOTHING → the bug: ``favorite_colour = blue AND red`` both live.

THE FIX (main.py, flag ``CORRECTION_NEWVALUE_EXCLUDE`` default ON)
- resolve the asserted NEW value's UUID lookup-only (``entity_aliases``),
- EXCLUDE it from the subject-only ambiguity set (step 3) and from a new
  facts∪staged exclusion fallback (step 8),
so the single stored OLD value is isolated and superseded → exactly ONE live value.

These tests drive the REAL ``correct_fact`` endpoint function (the same seam prod runs);
only the LLM extractor + Qdrant/network collaborators are stubbed. The Postgres layer is
a SQL-routing fake cursor over an in-memory fact model, so the actual 8-step lookup +
supersede branching executes for real and we assert the resulting fact state.
"""

import os
import uuid
import importlib
from contextlib import contextmanager

import pytest

main = importlib.import_module("src.api.main")
from src.api.models import FactCorrectionRequest


USER = "11111111-1111-5111-8111-111111111111"
U_BLUE = "22222222-2222-5222-8222-222222222222"
U_RED = "33333333-3333-5333-8333-333333333333"
U_TORONTO = "44444444-4444-5444-8444-444444444444"
U_VANCOUVER = "55555555-5555-5555-8555-555555555555"


# ── in-memory fact model ──────────────────────────────────────────────────────
class _Model:
    def __init__(self):
        self.facts = []   # {id, subject_id, object_id, rel_type, superseded, archived}
        self.staged = []  # {id, subject_id, object_id, rel_type, promoted, deleted, expired}
        self.aliases = {}  # alias_lower -> uuid
        self._next = 900

    def add_fact(self, fid, s, o, rel, superseded=False):
        self.facts.append(dict(id=fid, subject_id=s, object_id=o,
                               rel_type=rel, superseded=superseded, archived=False))

    def live_facts(self, rel):
        return [f for f in self.facts
                if f["rel_type"] == rel and not f["superseded"] and not f["archived"]]


class _Cur:
    def __init__(self, model):
        self.m = model
        self._sql = ""
        self._params = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def rowcount(self):
        return 1

    def execute(self, sql, params=None):
        self._sql = " ".join(str(sql).split()).lower()
        self._params = params
        s = self._sql
        # writes take effect immediately
        if s.startswith("update facts set superseded_at"):
            fid = params[0]
            for f in self.m.facts:
                if f["id"] == fid:
                    f["superseded"] = True
        elif s.startswith("delete from staged_facts"):
            p = params[0] if params else None
            ids = list(p) if isinstance(p, (list, tuple)) else [p]
            self.m.staged = [r for r in self.m.staged if r["id"] not in ids]

    def fetchone(self):
        s = self._sql
        if "from entity_aliases where alias" in s:
            a = str(self._params[0]).lower().strip()
            u = self.m.aliases.get(a)
            return (u,) if u else None
        if "from entity_attributes" in s:
            return None  # no stored scalar attribute for this (entity, attr)
        rows = self._select(s, self._params)
        return rows[0] if rows else None

    def fetchall(self):
        return self._select(self._sql, self._params)

    def _select(self, s, params):
        params = params or ()
        # ── facts ──
        if "from facts" in s and "select id, object_id" in s:
            if "and object_id = %s" in s:  # step 1 (direct: subject, object, rel)
                subj, obj, rel = params[0], params[1], params[2]
                return [(f["id"], f["object_id"]) for f in self.m.facts
                        if f["subject_id"] == subj and f["object_id"] == obj
                        and f["rel_type"] == rel and not f["superseded"]
                        and not f["archived"]]
            subj, rel = params[0], params[1]  # step 3 / step 8 (subject, rel)
            return [(f["id"], f["object_id"]) for f in self.m.facts
                    if f["subject_id"] == subj and f["rel_type"] == rel
                    and not f["superseded"] and not f["archived"]]
        if "from facts" in s and "select id, subject_id" in s:  # step 4 (object-side)
            obj, rel = params[0], params[1]
            return [(f["id"], f["subject_id"]) for f in self.m.facts
                    if f["object_id"] == obj and f["rel_type"] == rel
                    and not f["superseded"]]
        if "from facts" in s and "select id, rel_type" in s:  # step 7 (old-value object)
            subj, obj = params[0], params[1]
            return [(f["id"], f["rel_type"]) for f in self.m.facts
                    if f["subject_id"] == subj and f["object_id"] == obj
                    and not f["superseded"] and not f["archived"]]
        # ── staged_facts ──
        if "from staged_facts" in s and "select id, object_id" in s:  # step 6 / step 8 staged
            subj, rel = params[0], params[1]
            return [(r["id"], r["object_id"]) for r in self.m.staged
                    if r["subject_id"] == subj and r["rel_type"] == rel
                    and not r["promoted"] and not r["deleted"] and not r["expired"]]
        if "from staged_facts" in s and "select id, subject_id" in s:  # step 5 (object-side)
            obj, rel = params[0], params[1]
            return [(r["id"], r["subject_id"]) for r in self.m.staged
                    if r["object_id"] == obj and r["rel_type"] == rel
                    and not r["promoted"]]
        return []


class _Conn:
    def __init__(self, model):
        self.m = model

    def cursor(self):
        return _Cur(self.m)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


# ── collaborator stubs ────────────────────────────────────────────────────────
class _FakeRegistry:
    def __init__(self, model):
        self.m = model

    def resolve(self, user_id, name, name_intent=False):
        if not name:
            return None
        return self.m.aliases.get(str(name).lower().strip())


class _FakeManager:
    """Records the re-ingest of the corrected NEW value as a live fact (ON-CONFLICT dedup)."""
    def __init__(self, model):
        self.m = model

    def commit(self, tuples, confidence=None, fact_class=None, fact_provenance=None):
        for t in tuples:
            _s, o, rel = t[1], t[2], t[3]
            exists = any(f["subject_id"] == _s and f["object_id"] == o
                         and f["rel_type"] == rel and not f["superseded"]
                         for f in self.m.facts)
            if not exists:
                self.m._next += 1
                self.m.add_fact(self.m._next, _s, o, rel)
        return [("ok",)]


class _FakeGate:
    def __init__(self, *a, **k):
        pass

    def validate_edge(self, *a, **k):
        return True


@contextmanager
def _harness(model, extraction, monkeypatch, flag_on=True):
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://stub")
    if flag_on:
        monkeypatch.delenv("CORRECTION_NEWVALUE_EXCLUDE", raising=False)
    else:
        monkeypatch.setenv("CORRECTION_NEWVALUE_EXCLUDE", "false")

    monkeypatch.setattr(main.psycopg2, "connect", lambda *a, **k: _Conn(model))
    monkeypatch.setattr(main, "get_user_schema_context",
                        lambda uid, db: {"schema_name": "faultline_test"})
    monkeypatch.setattr(main, "_ensure_tenant_ready_sync", lambda *a, **k: None)
    monkeypatch.setattr(main, "_require_resolvable_user_id", lambda uid, *a, **k: uid)
    monkeypatch.setattr(main, "_INGEST_ENABLED", True, raising=False)
    monkeypatch.setattr(main, "STRUCTURAL_CORRECTION", False, raising=False)
    monkeypatch.setattr(main, "_idempotency_mgr", None, raising=False)
    monkeypatch.setattr(main, "_unified_correction_extraction_llm",
                        lambda **k: dict(extraction))
    monkeypatch.setattr(main, "resolve_anchor", lambda **k: k.get("user_id"))
    monkeypatch.setattr(main, "EntityRegistry", lambda db, **_kw: _FakeRegistry(model))
    monkeypatch.setattr(main, "FactStoreManager", lambda db: _FakeManager(model))
    monkeypatch.setattr(main, "WGMValidationGate", lambda *a, **k: _FakeGate())

    # relational metadata for the rels under test (tail {ANY}, not hierarchy → RELATIONAL axis)
    def _fake_rel_meta(rel):
        if str(rel).lower() in ("favorite_colour", "lives_in"):
            return {"tail_types": ["ANY"], "is_hierarchy_rel": False}
        return {}
    monkeypatch.setattr(main, "_rel_meta", _fake_rel_meta)

    # neutralise Qdrant / HTTP side-effects (best-effort blocks; failures are swallowed anyway)
    monkeypatch.setattr(main, "qp_resolve_partition", lambda *a, **k: ("c", {"t": 1}))
    monkeypatch.setattr(main, "qp_require_tenant", lambda *a, **k: None)
    monkeypatch.setattr(main, "qp_build_delete_body", lambda *a, **k: {})
    monkeypatch.setattr(main, "qp_qdrant_headers", lambda *a, **k: {})
    monkeypatch.setattr(main, "qp_resolve_point_id", lambda *a, **k: 0)

    class _NoHTTP:
        def post(self, *a, **k):
            class _R:
                status_code = 200

                def json(self):
                    return {}
            return _R()
    monkeypatch.setattr(main, "_http_client_sync", _NoHTTP(), raising=False)
    yield


def _extraction(old_rel, old_val, new_rel, new_val):
    return {
        "subject_name": "user",
        "old_rel_type": old_rel,
        "old_value": old_val,
        "new_rel_type": new_rel,
        "new_value": new_val,
        "dimension": "RELATIONAL",
        "confidence": 0.98,
    }


# ── tests ─────────────────────────────────────────────────────────────────────
def test_harvest_race_value_correction_supersedes_old_leaves_one_value(monkeypatch):
    """THE BUG → FIX: harvest pre-added red; correction (no resolvable old value)
    must supersede blue and leave EXACTLY ONE live favorite_colour (red)."""
    m = _Model()
    m.aliases = {"blue": U_BLUE, "red": U_RED}
    m.add_fact(1, USER, U_BLUE, "favorite_colour")          # stored OLD value
    m.add_fact(2, USER, U_RED, "favorite_colour")           # harvest-added NEW value (the race)
    # old_value=None reproduces the live "Actually, my favorite colour is red." miss
    extraction = _extraction("favorite_colour", None, "favorite_colour", "red")

    with _harness(m, extraction, monkeypatch):
        resp = main.correct_fact(FactCorrectionRequest(user_id=USER,
                                 text="Actually, my favorite colour is red."))

    assert resp.status == "corrected", resp.message
    assert resp.facts_superseded == 1
    live = m.live_facts("favorite_colour")
    assert len(live) == 1, f"expected exactly one live value, got {live}"
    assert live[0]["object_id"] == U_RED          # red is the survivor
    assert any(f["id"] == 1 and f["superseded"] for f in m.facts)  # blue retired


def test_flag_off_reproduces_two_live_values_bug(monkeypatch):
    """With the fix DISABLED, the same input BAILS (clarification) and both values stay
    live — proving the flag gates the fix and the defect is real without it."""
    m = _Model()
    m.aliases = {"blue": U_BLUE, "red": U_RED}
    m.add_fact(1, USER, U_BLUE, "favorite_colour")
    m.add_fact(2, USER, U_RED, "favorite_colour")
    extraction = _extraction("favorite_colour", None, "favorite_colour", "red")

    with _harness(m, extraction, monkeypatch, flag_on=False):
        resp = main.correct_fact(FactCorrectionRequest(user_id=USER,
                                 text="Actually, my favorite colour is red."))

    assert resp.status == "clarification_needed"
    assert len(m.live_facts("favorite_colour")) == 2  # the bug: blue AND red both live


def test_relational_correction_still_supersedes(monkeypatch):
    """Regression guard: an explicit relational value correction (Toronto→Vancouver)
    still supersedes via the direct object-anchored match."""
    m = _Model()
    m.aliases = {"toronto": U_TORONTO, "vancouver": U_VANCOUVER}
    m.add_fact(10, USER, U_TORONTO, "lives_in")
    extraction = _extraction("lives_in", "toronto", "lives_in", "vancouver")

    with _harness(m, extraction, monkeypatch):
        resp = main.correct_fact(FactCorrectionRequest(user_id=USER,
                                 text="Actually I live in Vancouver, not Toronto."))

    assert resp.status == "corrected", resp.message
    assert resp.facts_superseded == 1
    live = m.live_facts("lives_in")
    assert len(live) == 1
    assert live[0]["object_id"] == U_VANCOUVER
    assert any(f["id"] == 10 and f["superseded"] for f in m.facts)


def test_genuine_no_target_still_fails_loud(monkeypatch):
    """User-is-truth, both arms honest (recall-fidelity lane 2026-11-06): a correction
    with no stored target for the rel commits the NEW value MARKED (status
    'corrected_unresolved', a countable retraction_outcomes marker, honest message) -
    NOT a silent success, and no longer a bare refusal that leaves the harvest's
    inserted value unsuperseded. With the flag OFF the legacy bare-refusal contract
    holds (still loud: 'failed', nothing inserted)."""
    m = _Model()
    m.aliases = {"red": U_RED}  # nothing stored for favorite_colour
    extraction = _extraction("favorite_colour", None, "favorite_colour", "red")

    with _harness(m, extraction, monkeypatch):
        resp = main.correct_fact(FactCorrectionRequest(user_id=USER,
                                 text="Actually, my favorite colour is red."))

    # the new value was committed UNSUPERSEDED and the response SAYS SO
    assert resp.status == "corrected_unresolved", resp
    assert resp.facts_superseded == 0
    assert "couldn't find" in (resp.message or "").lower(), resp.message
    _live = m.live_facts("favorite_colour")
    assert [f["object_id"] for f in _live] == [U_RED], _live

    # legacy contract under the flag OFF: bare refusal, NOTHING inserted
    m2 = _Model()
    m2.aliases = {"red": U_RED}
    real_flag_on = main._flag_on

    def _flag_off(name, default="true"):
        if name == "CORRECTION_UNRESOLVED_SUPERSESSION":
            return False
        return real_flag_on(name, default)

    with _harness(m2, extraction, monkeypatch):
        monkeypatch.setattr(main, "_flag_on", _flag_off)
        resp2 = main.correct_fact(FactCorrectionRequest(user_id=USER,
                                  text="Actually, my favorite colour is red."))

    assert resp2.status == "failed", resp2
    assert "not found" in (resp2.message or "").lower()
    assert m2.live_facts("favorite_colour") == []


def test_wrong_old_value_gates_step3_step8_supersedes(monkeypatch):
    """The task's literal 'Old relational fact not found' manifestation: the extractor's
    old_value resolves to a WRONG entity (not on any stored fact), so object_uuid is truthy
    and the subject-only step (3) is gated OFF while the object-anchored steps (1/7) miss —
    every rel-keyed step returns not-found and the harvest-red lives on (two values). Step 8
    (facts∪staged, asserted-value excluded) isolates and supersedes the stored OLD value."""
    m = _Model()
    m.aliases = {"blue": U_BLUE, "red": U_RED, "teal": "66666666-6666-5666-8666-666666666666"}
    m.add_fact(1, USER, U_BLUE, "favorite_colour")   # stored OLD value
    m.add_fact(2, USER, U_RED, "favorite_colour")    # harvest-added NEW value
    # old_value "teal" resolves to an entity that is on NO favorite_colour fact
    extraction = _extraction("favorite_colour", "teal", "favorite_colour", "red")

    with _harness(m, extraction, monkeypatch):
        resp = main.correct_fact(FactCorrectionRequest(user_id=USER,
                                 text="Actually, my favorite colour is red, not teal."))

    assert resp.status == "corrected", resp.message
    live = m.live_facts("favorite_colour")
    assert len(live) == 1 and live[0]["object_id"] == U_RED
    assert any(f["id"] == 1 and f["superseded"] for f in m.facts)


def test_harvest_race_via_staged_old_value(monkeypatch):
    """Same race, but the stored OLD value lives in staged_facts (Class B) while the
    harvest added the NEW value to facts. The step-3 asserted-value EXCLUSION drops the
    harvest-red from the facts candidate set (else it would wrongly match red as the
    'old' value); the staged OLD value is then located and superseded → one live value."""
    m = _Model()
    m.aliases = {"blue": U_BLUE, "red": U_RED}
    # OLD value staged (Class B), NEW value harvested into facts
    m.staged.append(dict(id=71, subject_id=USER, object_id=U_BLUE,
                         rel_type="favorite_colour", promoted=False,
                         deleted=False, expired=False))
    m.add_fact(2, USER, U_RED, "favorite_colour")
    extraction = _extraction("favorite_colour", None, "favorite_colour", "red")

    with _harness(m, extraction, monkeypatch):
        resp = main.correct_fact(FactCorrectionRequest(user_id=USER,
                                 text="Actually, my favorite colour is red."))

    assert resp.status == "corrected", resp.message
    live = m.live_facts("favorite_colour")
    assert len(live) == 1 and live[0]["object_id"] == U_RED
    # staged OLD value (blue) removed on supersede — no longer present/live
    assert not any(r["id"] == 71 and not r["deleted"] for r in m.staged)
