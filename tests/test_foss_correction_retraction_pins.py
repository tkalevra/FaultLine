"""FOSS-004/005/008/010 pins - the /retract/correct correction+retraction lane.

House conventions: the LLM extraction and the DB are stubbed (no spacy/gliner2/torch/
postgres in the pin environment) - tests/test_foss_retraction_no_qdrant.py (fake _DB/_Cur
with SQL-pattern routing) and tests/test_measure_scalar_52.py (mp.setattr of the
correction-extraction LLM to the live extraction shape) are the patterns followed here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


USER = "11111111-1111-4111-8111-111111111111"
GATA = "22222222-2222-4222-8222-222222222222"
SERVIDOR = "33333333-3333-4333-8333-333333333333"
SCHEMA = "faultline_unit"


# -- the fake tenant -----------------------------------------------------------

def _attr(entity, attribute, value, superseded_at=None, provenance="mcp"):
    return {
        "entity_id": entity, "attribute": attribute, "value_text": value,
        "value_int": None, "value_float": None, "value_date": None,
        "provenance": provenance, "datatype": None,
        "superseded_at": superseded_at,
        "created_at": datetime.now(timezone.utc) - timedelta(days=1),
        "updated_at": None,
    }


class Tenant:
    def __init__(self):
        self.entities = {USER: "Person"}
        self.aliases = {}            # alias(lower) -> entity_id
        self.alias_pref = {}         # (entity_id, alias) -> bool
        self.attributes = []
        self.facts = []
        self.staged = []
        self.history = []
        self.retraction_outcomes = []
        self.commits = 0
        self.rollbacks = 0
        self.mint_calls = []         # every registry.resolve mint (name minted)
        self.lookup_calls = []       # every resolve_lookup call
        self.sql = []                # (normalized sql, params) tape

    def add_alias(self, entity, alias, preferred=True):
        self.aliases[alias.lower()] = entity
        self.alias_pref[(entity, alias.lower())] = preferred

    def add_entity(self, eid, etype, aliases):
        self.entities[eid] = etype
        for a in aliases:
            self.add_alias(eid, a)

    def entity_count(self):
        return len(self.entities)

    def live_fact_count(self):
        return len([f for f in self.facts if not f["superseded_at"]])

    def live_attr_count(self):
        return len([a for a in self.attributes if not a["superseded_at"]])

    def live_staged_count(self):
        return len([s for s in self.staged if not s["deleted_at"]])


def _norm(s):
    return " ".join(str(s or "").split()).lower().strip()


def _norm_sql(sql):
    return _norm(sql)


# -- the fake cursor/conn (SQL-pattern routing, house style) --------------------

class _Cur:
    def __init__(self, db):
        self.db = db
        self._rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        db = self.db
        t = db.t
        s = _norm_sql(sql)
        P = params
        t.sql.append((s, P))
        self._rows = []
        self.rowcount = 0

        if s.startswith("set search_path") or s == "select 1" or s.startswith("begin"):
            return

        # ---- entity_aliases reads ----
        if "from entity_aliases" in s:
            if s.startswith("select entity_id, alias from entity_aliases"):
                self._rows = [(e, a) for (e, a) in t.alias_pref.keys()]
                return
            if s.startswith("select entity_id from entity_aliases") and "where alias =" in s:
                alias = str(P[0]).lower().strip()
                eid = t.aliases.get(alias)
                if eid:
                    self._rows = [(eid, None)] if "display_form" in s else [(eid,)]
                return
            if s.startswith("select alias from entity_aliases"):
                eid = P[0]
                rows = [a for (e, a) in t.alias_pref.keys() if e == eid]
                rows.sort(key=lambda a: (not t.alias_pref.get((eid, a), False), a))
                self._rows = [(a,) for a in rows]
                return
            if "lower(alias)" in s:
                eid = P[0]
                self._rows = [(a.lower(),) for (e, a) in t.alias_pref.keys() if e == eid]
                return

        # ---- entity_attributes reads ----
        if "from entity_attributes" in s:
            if s.startswith("select distinct attribute from entity_attributes"):
                if "entity_id <> " in s:
                    names = sorted({a["attribute"] for a in t.attributes
                                    if a["entity_id"] != P[0]})
                else:
                    names = sorted({a["attribute"] for a in t.attributes
                                    if a["entity_id"] == P[0]})
                self._rows = [(n,) for n in names]
                return
            if s.startswith("select attribute, value_text from entity_attributes"):
                self._rows = [(a["attribute"], a["value_text"]) for a in t.attributes
                              if a["entity_id"] == P[0]]
                return
            if s.startswith("select entity_id, value_text from entity_attributes"):
                holders = {}
                for a in t.attributes:
                    if a["attribute"] == P[0] and a["entity_id"] != P[1]:
                        holders[a["entity_id"]] = a["value_text"]
                self._rows = [(e, v) for e, v in holders.items()]
                return
            if s.startswith("select attribute, value_text, value_int"):
                self._rows = [(a["attribute"], a["value_text"], a["value_int"],
                               a["value_float"], a["value_date"])
                              for a in t.attributes
                              if a["entity_id"] == P[0] and not a["superseded_at"]]
                return
            if s.startswith("select attribute from entity_attributes"):
                hits = sorted({a["attribute"] for a in t.attributes
                               if a["entity_id"] == P[0] and not a["superseded_at"]
                               and (a["value_text"] or "").lower() == str(P[1]).lower()})
                self._rows = [(a,) for a in hits]
                return
            if s.startswith("select value_text, value_int"):
                hits = [a for a in t.attributes
                        if a["entity_id"] == P[0] and a["attribute"] == P[1]
                        and (("superseded_at is null" not in s) or not a["superseded_at"])]
                if hits:
                    r = hits[0]
                    self._rows = [(r["value_text"], r["value_int"], r["value_float"],
                                   r["value_date"], r["provenance"], r["datatype"],
                                   r["superseded_at"], r["created_at"])]
                return
            if s.startswith("select entity_id from entity_attributes"):
                self._rows = []
                return
            if s.startswith("select 1 from entity_attributes"):
                hit = any(a["entity_id"] == P[0] and a["attribute"] == P[1]
                          for a in t.attributes)
                if hit:
                    self._rows = [(1,)]
                return

        # ---- entity_attributes_history ----
        if "from entity_attributes_history" in s:
            rows = [h for h in t.history if not P or h["entity_id"] == P[0]]
            if len(P) >= 3 and "attribute = %s" in s:
                rows = [h for h in rows if h["attribute"] == P[1]]
            since_idx = 2 if len(P) >= 3 else 1
            if (len(P) > since_idx and "created_at >=" in s
                    and P[since_idx] is not None):
                rows = [h for h in rows if h["created_at"] >= P[since_idx]]
            self._rows = [
                (h["attribute"], h["prior_value_text"], h["prior_value_int"],
                 h["prior_value_float"], h["prior_value_date"], h["action"],
                 h["cause"], h["new_value_text"], h["created_at"])
                for h in rows]
            return
        if s.startswith("insert into entity_attributes_history"):
            t.history.append({
                "entity_id": P[1], "attribute": P[2],
                "prior_value_text": P[3], "prior_value_int": P[4],
                "prior_value_float": P[5], "prior_value_date": P[6],
                "new_value_text": P[10], "new_value_int": P[11],
                "action": P[15], "cause": P[16],
                "created_at": datetime.now(timezone.utc),
            })
            self.rowcount = 1
            return

        # ---- entity_attributes writes ----
        if s.startswith("insert into entity_attributes"):
            (uid, eid, attr, vt, vi, vf, vd, prov, dt, unit, vnorm) = P[:11]
            for a in t.attributes:
                if a["entity_id"] == eid and a["attribute"] == attr:
                    a.update({"value_text": vt, "value_int": vi, "value_float": vf,
                              "value_date": vd, "provenance": prov, "datatype": dt,
                              "superseded_at": None,
                              "updated_at": datetime.now(timezone.utc)})
                    break
            else:
                t.attributes.append(_attr(eid, attr, vt, provenance=prov))
            self.rowcount = 1
            return
        if s.startswith("update entity_attributes") and "superseded_at = now()" in s:
            n = 0
            for a in t.attributes:
                if a["entity_id"] == P[0] and a["attribute"] == P[1] and not a["superseded_at"]:
                    a["superseded_at"] = datetime.now(timezone.utc)
                    n += 1
            self.rowcount = n
            return

        # ---- facts ----
        if "from facts" in s:
            if s.startswith("select distinct rel_type"):
                eid = P[0]
                rels = {f["rel_type"] for f in t.facts
                        if (f["subject_id"] == eid or f["object_id"] == eid)
                        and not f["superseded_at"]}
                self._rows = [(r,) for r in rels]
                return
            if s.startswith("select"):
                hits = []
                for f in t.facts:
                    if f["superseded_at"]:
                        continue
                    if "subject_id = %s and object_id = %s and rel_type = %s" in s:
                        if (f["subject_id"], f["object_id"], f["rel_type"]) == (P[0], P[1], P[2]):
                            hits.append(f)
                    elif "object_id = %s and rel_type = %s" in s:
                        if f["object_id"] == P[0] and f["rel_type"] == P[1]:
                            hits.append(f)
                    elif "subject_id = %s and rel_type = %s" in s and "object_id = %s" not in s:
                        if f["subject_id"] == P[0] and f["rel_type"] == P[1]:
                            hits.append(f)
                    elif "(subject_id = %s or object_id = %s)" in s:
                        if f["subject_id"] == P[0] or f["object_id"] == P[0]:
                            hits.append(f)
                if "limit 1" in s:
                    hits = hits[:1]
                self._rows = [(f["id"], f["object_id"]) for f in hits]
                return
        if s.startswith("update facts") and "superseded_at = now()" in s:
            n = 0
            for f in t.facts:
                if f["id"] == P[0] and not f["superseded_at"]:
                    f["superseded_at"] = datetime.now(timezone.utc)
                    n += 1
            self.rowcount = n
            return
        if s.startswith("insert into facts"):
            t.facts.append({"id": 1000 + len(t.facts), "subject_id": P[0],
                            "object_id": P[1], "rel_type": P[2], "superseded_at": None})
            self.rowcount = 1
            return

        # ---- staged_facts ----
        if "from staged_facts" in s:
            if s.startswith("select distinct rel_type"):
                self._rows = []
                return
            if s.startswith("select id, subject_id, object_id"):
                hits = [sf for sf in t.staged if not sf["deleted_at"]
                        and ((sf["subject_id"] == P[0] and sf["rel_type"] == P[1])
                             or (sf["object_id"] == P[0] and sf["rel_type"] == P[1]))]
                self._rows = [(sf["id"], sf["subject_id"], sf["object_id"]) for sf in hits[:1]]
                return
            if s.startswith("select"):
                hits = [sf for sf in t.staged if not sf["deleted_at"]]
                if len(P) >= 2 and "subject_id = %s and rel_type = %s" in s:
                    hits = [sf for sf in hits if sf["subject_id"] == P[0]
                            and sf["rel_type"] == P[1]]
                hits = hits[:1]
                self._rows = [(sf["id"], sf.get("object_id")) for sf in hits]
                return
        if s.startswith("insert into staged_facts"):
            t.staged.append({"id": 500 + len(t.staged), "subject_id": P[1],
                             "rel_type": P[2], "object_id": P[3], "deleted_at": None})
            self.rowcount = 1
            return
        if s.startswith("delete from staged_facts"):
            ids = set(P[0]) if P and not isinstance(P[0], str) else None
            n = 0
            for sf in t.staged:
                if not sf["deleted_at"] and (ids is None or sf["id"] in ids):
                    sf["deleted_at"] = datetime.now(timezone.utc)
                    n += 1
            self.rowcount = n
            return
        if s.startswith("update staged_facts") and "deleted_at = now()" in s:
            n = 0
            for sf in t.staged:
                if not sf["deleted_at"] and ((not P) or sf["subject_id"] == P[0] or sf["object_id"] == P[0]):
                    sf["deleted_at"] = datetime.now(timezone.utc)
                    n += 1
            self.rowcount = n
            return
        if s.startswith("update staged_facts") and "expires_at = now()" in s:
            self.rowcount = 0
            return

        # ---- entities / entity minting ----
        if s.startswith("insert into entities"):
            t.entities.setdefault(P[0], "unknown")
            self.rowcount = 1
            return
        if s.startswith("insert into entity_aliases"):
            eid, alias = P[0], str(P[1]).lower()
            t.aliases.setdefault(alias, eid)
            t.alias_pref[(eid, alias)] = True
            self.rowcount = 1
            return
        if s.startswith("delete from entity_aliases"):
            eid = P[0]
            for (e, a) in list(t.alias_pref.keys()):
                if e == eid:
                    t.alias_pref.pop((e, a))
                    t.aliases.pop(a, None)
            self.rowcount = 1
            return
        if s.startswith("delete from entities"):
            t.entities.pop(P[0], None)
            self.rowcount = 1
            return
        if s.startswith("update entity_aliases"):
            self.rowcount = 0
            return
        if "from entities" in s and s.startswith("select"):
            eid = P[0]
            if eid in t.entities:
                self._rows = [(eid, t.entities[eid])]
            return

        # ---- learning ledger / misc ----
        if s.startswith("insert into retraction_outcomes"):
            t.retraction_outcomes.append(P)
            self.rowcount = 1
            return
        if "from rel_types" in s:
            self._rows = []
            return
        if "from entity_name_conflicts" in s or "from entity_synonyms" in s:
            self._rows = []
            return
        return

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _DB:
    def __init__(self, tenant):
        self.t = tenant

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.t.commits += 1

    def rollback(self):
        self.t.rollbacks += 1

    def close(self):
        pass


# -- stub registry / manager / gate ---------------------------------------------

_SURROGATE_N = [0]


class _StubRegistry:
    def __init__(self, db, schema_name=None):
        self.db = db

    def _lookup(self, user_id, name):
        name = str(name).lower().strip()
        if name in ("user", "i", "me", "my", "myself", "mine"):
            return user_id
        return self.db.t.aliases.get(name)

    def resolve_lookup(self, user_id, name):
        self.db.t.lookup_calls.append(str(name).lower())
        return self._lookup(user_id, name)

    def resolve(self, user_id, name, name_intent=False):
        hit = self._lookup(user_id, name)
        if hit:
            return hit
        _SURROGATE_N[0] += 1
        surrogate = "99999999-9999-4999-8999-" + str(100000000000 + _SURROGATE_N[0])[-12:]
        self.db.t.entities.setdefault(surrogate, "unknown")
        alias = str(name).lower().strip()
        self.db.t.aliases.setdefault(alias, surrogate)
        self.db.t.alias_pref[(surrogate, alias)] = True
        self.db.t.mint_calls.append(alias)
        self.db.t.commits += 1        # the real resolve() commits internally
        return surrogate

    def register_alias(self, *a, **k):
        return True


class _StubManager:
    def __init__(self, db):
        self.db = db

    def commit(self, tuples, **kw):
        for (uid, subj, obj, rel, src) in tuples:
            self.db.t.facts.append({
                "id": 2000 + len(self.db.t.facts), "subject_id": subj,
                "object_id": obj, "rel_type": rel, "superseded_at": None})
        return len(tuples)


class _StubGate:
    def __init__(self, db, schema_name=None):
        self.db = db

    def validate_edge(self, **kw):
        return None


# -- the endpoint harness --------------------------------------------------------

class Env:
    """One /retract/correct call against the fake tenant. The LLM correction/retraction
    extractor is stubbed to the caller-supplied live extraction shape."""

    def __init__(self, monkeypatch, tenant, extraction, intent="CORRECTION"):
        import src.api.main as M
        self.M = M
        self.t = tenant
        self.db = _DB(tenant)
        self.intent = intent
        mp = self.mp = monkeypatch
        mp.setenv("FAULTLINE_BACKEND_SECRET", "foss-pins-secret")
        self.headers = {"X-FaultLine-Backend-Secret": "foss-pins-secret"}
        mp.setattr(M, "_INGEST_ENABLED", True)
        mp.setattr(M, "_ensure_tenant_ready_sync", lambda uid, ep: None)
        mp.setattr(M, "get_user_schema_context",
                   lambda uid, db: {"schema_name": SCHEMA})
        mp.setattr(M.psycopg2, "connect", lambda dsn=None: self.db)
        mp.setattr(M, "EntityRegistry", _StubRegistry)
        mp.setattr(M, "FactStoreManager", _StubManager)
        mp.setattr(M, "WGMValidationGate", _StubGate)
        mp.setattr(M, "STRUCTURAL_CORRECTION", False)
        mp.setattr(M, "NAME_REPAIR_CORRECTION", False)
        mp.setattr(M, "NAME_REFERENT_L4_WALK", False)
        mp.setattr(M, "NAME_CORRECTION_SPEAKER_GUARD", False)
        mp.setattr(M, "SPINE_NAMING_CHAIN", False)
        mp.setattr(M, "_idempotency_mgr", None)
        mp.setattr(M, "resolve_anchor", lambda **kw: kw.get("user_id"))
        mp.setattr(M, "_retraction_semantic_fallback", lambda *a, **k: None)
        if intent == "RETRACTION":
            mp.setattr(M, "_retraction_intent_extraction_llm",
                       lambda **kw: dict(extraction))
        else:
            mp.setattr(M, "_unified_correction_extraction_llm",
                       lambda **kw: dict(extraction))

    def post(self, text, intent=None):
        from fastapi.testclient import TestClient
        client = TestClient(self.M.app)
        r = client.post("/retract/correct", json={
            "text": text, "user_id": USER,
            "intent": intent or self.intent}, headers=self.headers)
        assert r.status_code == 200, r.text[:400]
        return r.json()


def _default_tenant():
    t = Tenant()
    t.add_entity(GATA, "Animal", ["gata", "fraggle"])
    t.add_entity(SERVIDOR, "Object", ["devbox", "servidor"])
    return t


def _devbox_ip_tenant():
    t = _default_tenant()
    t.attributes.append(_attr(SERVIDOR, "has_ip", "10.0.1.5"))
    return t


# =============================================================================
# FOSS-004 - the correction lottery
# =============================================================================

def test_correction_lottery_pin_attribute_backed_correction_reports_corrected(monkeypatch):
    """es battery, pinned: the extractor guesses located_in / old=10.0.1.9 (both wrong);
    the stored row is (devbox, has_ip, 10.0.1.5). The lane must land the correction on
    the STORED attribute and report status=corrected - never 'Old fact not found'."""
    t = _devbox_ip_tenant()
    extraction = {"subject_name": "DevBox", "old_rel_type": "located_in",
                  "old_value": "10.0.1.9", "new_rel_type": "located_in",
                  "new_value": "10.0.1.10", "dimension": "RELATIONAL",
                  "confidence": 0.9}
    env = Env(monkeypatch, t, extraction)
    out = env.post("En realidad DevBox se mudo a 10.0.1.10 despues del cambio de red.")

    assert out["status"] == "corrected", out
    assert out.get("dimension") == "SCALAR", out
    assert out.get("old_rel_type") == "has_ip", out
    assert out.get("new_value") == "10.0.1.10", out
    assert out.get("facts_superseded") == 1, out
    live = [a for a in t.attributes
            if a["entity_id"] == SERVIDOR and a["attribute"] == "has_ip"
            and not a["superseded_at"]]
    assert live and live[0]["value_text"] == "10.0.1.10", t.attributes
    assert any(h["attribute"] == "has_ip" and h["action"] == "superseded"
               and h["prior_value_text"] == "10.0.1.5" for h in t.history), t.history


def test_already_current_pin_value_identical_recorrection(monkeypatch):
    """Doc fix 2: a value-identical re-correction returns an explicit already-current
    status - never 'not found' - and writes nothing."""
    t = _devbox_ip_tenant()
    t.attributes[0]["value_text"] = "10.0.1.10"
    before = len(t.history)
    extraction = {"subject_name": "DevBox", "old_rel_type": "ip_address",
                  "old_value": "10.0.1.9", "new_rel_type": "ip_address",
                  "new_value": "10.0.1.10", "dimension": "RELATIONAL",
                  "confidence": 0.9}
    env = Env(monkeypatch, t, extraction)
    out = env.post("La IP de DevBox ahora es 10.0.1.10.")

    assert out["status"] == "already_current", out
    assert out.get("facts_superseded") == 0, out
    assert len(t.history) == before, "already-current must not write a history row"
    live = [a for a in t.attributes
            if a["entity_id"] == SERVIDOR and a["attribute"] == "has_ip"
            and not a["superseded_at"]]
    assert live and live[0]["value_text"] == "10.0.1.10"


def test_harvest_supersede_pin_reports_the_scalar_leg(monkeypatch):
    """The es wound's actual DB state: the intent-independent harvest already superseded
    has_ip (history row 10.0.1.5 -> 10.0.1.10, cause=ingest_restate) before
    /retract/correct ran, and the lane still answered 'Old relational fact not found'.
    The response must reflect the supersede that happened this turn."""
    t = _devbox_ip_tenant()
    t.attributes[0]["value_text"] = "10.0.1.10"
    t.history.append({
        "entity_id": SERVIDOR, "attribute": "has_ip",
        "prior_value_text": "10.0.1.5", "prior_value_int": None,
        "prior_value_float": None, "prior_value_date": None,
        "new_value_text": "10.0.1.10", "new_value_int": None,
        "action": "superseded", "cause": "ingest_restate",
        "created_at": datetime.now(timezone.utc) - timedelta(seconds=30),
    })
    extraction = {"subject_name": "DevBox", "old_rel_type": "located_in",
                  "old_value": "10.0.1.9", "new_rel_type": "located_in",
                  "new_value": "10.0.1.10", "dimension": "RELATIONAL",
                  "confidence": 0.9}
    env = Env(monkeypatch, t, extraction)
    out = env.post("En realidad DevBox se mudo a 10.0.1.10 despues del cambio de red.")

    assert out["status"] == "corrected", out
    assert out.get("dimension") == "SCALAR", out
    assert out.get("old_value") == "10.0.1.5", out
    assert out.get("new_value") == "10.0.1.10", out
    assert out.get("facts_superseded") == 1, out


def test_stale_supersede_is_not_reported_as_this_turn(monkeypatch):
    """A supersede from LAST WEEK does not make today's no-op correction 'corrected'."""
    t = _devbox_ip_tenant()
    t.attributes[0]["value_text"] = "10.0.1.10"
    t.history.append({
        "entity_id": SERVIDOR, "attribute": "has_ip",
        "prior_value_text": "10.0.1.5", "prior_value_int": None,
        "prior_value_float": None, "prior_value_date": None,
        "new_value_text": "10.0.1.10", "new_value_int": None,
        "action": "superseded", "cause": "correct_fact",
        "created_at": datetime.now(timezone.utc) - timedelta(days=6),
    })
    extraction = {"subject_name": "DevBox", "old_rel_type": "ip_address",
                  "old_value": "10.0.1.9", "new_rel_type": "ip_address",
                  "new_value": "10.0.1.10", "dimension": "RELATIONAL",
                  "confidence": 0.9}
    env = Env(monkeypatch, t, extraction)
    out = env.post("La IP de DevBox ahora es 10.0.1.10.")
    assert out["status"] == "already_current", out


def test_honest_fail_pin_true_no_match_writes_nothing(monkeypatch):
    """A genuinely-unmatched correction still fails honestly, and NOTHING is written."""
    t = _default_tenant()
    counts = (t.entity_count(), t.live_fact_count(), t.live_attr_count(),
              t.live_staged_count())
    extraction = {"subject_name": "DevBox", "old_rel_type": "located_in",
                  "old_value": "10.0.1.9", "new_rel_type": "located_in",
                  "new_value": "10.9.9.9", "dimension": "RELATIONAL",
                  "confidence": 0.9}
    env = Env(monkeypatch, t, extraction)
    out = env.post("En realidad DevBox se mudo a 10.9.9.9 despues del cambio de red.")

    assert out["status"] == "failed", out
    assert out.get("facts_superseded", 0) == 0, out
    assert (t.entity_count(), t.live_fact_count(), t.live_attr_count(),
            t.live_staged_count()) == counts
    assert not t.history, t.history
