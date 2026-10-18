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
SPOT = "55555555-5555-4555-8555-555555555555"
JUNK = "99999999-9999-4999-8999-999999999999"   # harvest capture-debris entity
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
            if s.startswith("select id, subject_id from facts") and \
                    "object_id = %s and rel_type = %s" in s:
                # DUTY F round-2: step 4's object-side probe reads the FACT'S SUBJECT.
                self._rows = [(f["id"], f["subject_id"]) for f in t.facts
                              if f["object_id"] == P[0] and f["rel_type"] == P[1]
                              and not f["superseded_at"]][:1]
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
            # DUTY F round-2: the staged object-side swap probe reads the STAGED FACT'S
            # SUBJECT (step 5's "SELECT id, subject_id ... WHERE object_id = %s ...").
            if s.startswith("select id, subject_id from staged_facts") and \
                    "object_id = %s and rel_type = %s" in s:
                self._rows = [
                    (sf["id"], sf["subject_id"]) for sf in t.staged
                    if not sf["deleted_at"] and not sf.get("promoted_at")
                    and sf["object_id"] == P[0] and sf["rel_type"] == P[1]][:1]
                return
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
        if s.startswith("select id from entities"):
            # FOSS-005 pre-mint census: the full entity-id SET (no params).
            self._rows = [(e,) for e in t.entities]
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
        alias = str(name).lower().strip()
        # critic round 1 minor (2) repro knob: the REAL resolve() can also succeed by
        # REDIRECTING to an existing entity through paths resolve_lookup lacks
        # (measurement-family compound redirect). t.resolve_redirect maps name -> uuid.
        _redirect = getattr(self.db.t, "resolve_redirect", None)
        if _redirect and alias in _redirect:
            return _redirect[alias]
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


# =============================================================================
# FOSS-010 - the reported old value comes from STORAGE, never from the
# correction-extractor's re-derivation.
# =============================================================================

def test_fabrication_pin_no_prior_row_reports_no_fabricated_old(monkeypatch):
    t = _default_tenant()
    extraction = {"subject_name": "DevBox", "old_rel_type": "lives_at",
                  "old_value": "10.0.1.9", "new_rel_type": "lives_at",
                  "new_value": "10.0.1.10", "dimension": "SCALAR",
                  "confidence": 0.9}
    env = Env(monkeypatch, t, extraction)
    out = env.post("La IP de DevBox ahora es 10.0.1.10.")

    assert out.get("old_value") is None, out
    assert out.get("facts_superseded") == 0, out
    assert "10.0.1.9" not in str(out), out
    assert out["status"] in ("stored", "corrected"), out
    live = [a for a in t.attributes
            if a["entity_id"] == SERVIDOR and a["attribute"] == "lives_at"]
    assert live and live[0]["value_text"] == "10.0.1.10", t.attributes
    assert not t.history, "a first-value INSERT must not write a supersede history row"


def test_stored_old_preferred_over_extractor(monkeypatch):
    t = _devbox_ip_tenant()
    extraction = {"subject_name": "DevBox", "old_rel_type": "has_ip",
                  "old_value": "10.0.1.9", "new_rel_type": "has_ip",
                  "new_value": "10.0.1.10", "dimension": "SCALAR",
                  "confidence": 0.9}
    env = Env(monkeypatch, t, extraction)
    out = env.post("La IP de DevBox cambio: ahora es 10.0.1.10.")

    assert out["status"] == "corrected", out
    assert out.get("old_value") == "10.0.1.5", out
    assert out.get("new_value") == "10.0.1.10", out
    assert out.get("facts_superseded") == 1, out
    assert "10.0.1.9" in str(out.get("message")), (
        "the disagreement must be surfaced, not swallowed")


# =============================================================================
# FOSS-008 - the subject-sentence agreement gate: a plain-language retraction may
# only retire what the USER'S OWN WORDS ground.
# =============================================================================

def _gata_age_tenant():
    t = _default_tenant()
    t.attributes.append(_attr(GATA, "age", "3"))
    t.attributes.append(_attr(SERVIDOR, "has_ip", "10.0.1.5"))
    return t


def test_agreement_pin_retract_naming_gata_never_mutates_servidor(monkeypatch):
    """The FOSS-008 wound: the extractor resolves the subject of the CAT sentence to
    the SERVER and the lane retires the server's attributes. The resolved subject must
    be grounded in the user's own words; a mismatch refuses with no_match and writes
    NOTHING. The response still names the resolved subject and the candidates."""
    t = _gata_age_tenant()
    counts = (t.entity_count(), t.live_fact_count(), t.live_attr_count())
    extraction = {"subject": "servidor", "rel_type": "has_ip",
                  "object": "10.0.1.5", "action": "remove",
                  "dimension": "SCALAR", "confidence": 0.9}
    env = Env(monkeypatch, t, extraction, intent="RETRACTION")
    out = env.post("olvida que tengo una gata que se llama Fraggle")

    assert out["status"] == "no_match", out
    assert out.get("subject_uuid") == SERVIDOR, out   # resolved subject, named either way
    assert out.get("subject_name"), out
    live_ip = [a for a in t.attributes
               if a["entity_id"] == SERVIDOR and a["attribute"] == "has_ip"
               and not a["superseded_at"]]
    assert live_ip, "the un-grounded entity must survive untouched"
    assert (t.entity_count(), t.live_fact_count(),
            t.live_attr_count()) == counts, "a refused retraction writes nothing"
    assert not t.history, t.history


def test_agreement_positive_control_devbox_ip_still_retires(monkeypatch):
    """Doc pin (iii): a retraction whose subject IS grounded still retires normally."""
    t = _gata_age_tenant()
    extraction = {"subject": "devbox", "rel_type": "has_ip",
                  "object": "10.0.1.5", "action": "remove",
                  "dimension": "SCALAR", "confidence": 0.9}
    env = Env(monkeypatch, t, extraction, intent="RETRACTION")
    out = env.post("olvida la IP de DevBox")

    assert out["status"] == "corrected", out
    live_ip = [a for a in t.attributes
               if a["entity_id"] == SERVIDOR and a["attribute"] == "has_ip"
               and not a["superseded_at"]]
    assert not live_ip, "the grounded retraction must retire the named value"
    assert any(h["attribute"] == "has_ip" and h["action"] == "retired"
               for h in t.history), t.history


def test_agreement_ambiguous_returns_candidates_retires_nothing(monkeypatch):
    """Two entities grounded by one sentence: the lane refuses to guess, returns the
    candidates, and retires nothing."""
    t = _gata_age_tenant()
    extraction = {"subject": "gata", "rel_type": "age",
                  "object": "3", "action": "remove",
                  "dimension": "SCALAR", "confidence": 0.9}
    env = Env(monkeypatch, t, extraction, intent="RETRACTION")
    out = env.post("la gata y el servidor")

    assert out["status"] == "ambiguous", out
    live_age = [a for a in t.attributes
                if a["entity_id"] == GATA and a["attribute"] == "age"
                and not a["superseded_at"]]
    assert live_age, "an ambiguous retraction must retire nothing"
    assert out.get("candidates"), out
    cands = str(out["candidates"])
    assert GATA in cands and SERVIDOR in cands, out


# =============================================================================
# FOSS-005 - ATOMICITY: a failed retraction/correction leaves entity, fact and
# attribute counts unchanged and mints NOTHING.
# =============================================================================

def test_atomicity_pin_negation_retraction_writes_nothing(monkeypatch):
    """""No uso el puerto 9004...": a negation retraction whose (user, usar, puerto)
    fact does not exist. The old object resolution MINTED 'puerto' (committed inside
    the registry) even though the supersede then failed - entity count 3->4. Now:
    lookup-only resolution, honest failed status, zero writes, zero mints."""
    t = _default_tenant()
    counts = (t.entity_count(), t.live_fact_count(), t.live_attr_count())
    extraction = {"subject": "user", "rel_type": "usar",
                  "object": "puerto", "action": "remove",
                  "dimension": "RELATIONAL", "confidence": 0.9}
    env = Env(monkeypatch, t, extraction, intent="RETRACTION")
    out = env.post("No uso el puerto 9004 para nada.")

    assert out["status"] == "failed", out
    assert (t.entity_count(), t.live_fact_count(),
            t.live_attr_count()) == counts, (
        f"counts changed on a failed retraction: "
        f"{counts} -> {(t.entity_count(), t.live_fact_count(), t.live_attr_count())}")
    assert t.mint_calls == [], t.mint_calls
    assert not t.history, t.history
    assert not t.staged, t.staged


def test_lane_mint_kept_when_correction_lands_issue23(monkeypatch):
    """Control (issue #23 preserved): a pref_name correction NAMING a subject not yet
    in the graph mints it and LANDS - the mint is kept when the correction succeeds."""
    t = _default_tenant()
    before = t.entity_count()
    extraction = {"subject": "Rex", "old_rel_type": "pref_name",
                  "new_rel_type": "pref_name", "new_value": "reggie",
                  "dimension": "SCALAR", "confidence": 0.9}
    env = Env(monkeypatch, t, extraction)
    out = env.post("Rex se llama reggie, no Rex.")

    assert out["status"] == "corrected", out
    assert t.entity_count() == before + 1, "the named-subject mint must be kept on success"
    assert "rex" in t.mint_calls, t.mint_calls


def test_object_resolution_never_mints(monkeypatch):
    """The old-value object resolve is LOOKUP-ONLY now: whatever the lane decides
    (the grounded value legitimately retires has_ip; a value nothing holds lands
    nothing), the resolve itself must never leave a minted entity behind."""
    t = _devbox_ip_tenant()
    before = t.entity_count()
    extraction = {"subject": "devbox", "rel_type": "usar",
                  "object": "10.0.1.5", "action": "remove",
                  "dimension": "RELATIONAL", "confidence": 0.9}
    env = Env(monkeypatch, t, extraction, intent="RETRACTION")
    out = env.post("olvida que DevBox usa 10.0.1.5")

    assert t.entity_count() == before, "object resolve minted"
    assert t.mint_calls == [], t.mint_calls
    live_ip = [a for a in t.attributes
               if a["entity_id"] == SERVIDOR and a["attribute"] == "has_ip"
               and not a["superseded_at"]]
    assert not live_ip, "the grounded value must still retire"

    # and a value nothing holds: nothing lands, still zero mints
    t2 = _devbox_ip_tenant()
    before2 = t2.entity_count()
    extraction2 = {"subject": "devbox", "rel_type": "usar",
                   "object": "10.0.1.99", "action": "remove",
                   "dimension": "RELATIONAL", "confidence": 0.9}
    env2 = Env(monkeypatch, t2, extraction2, intent="RETRACTION")
    out2 = env2.post("olvida que DevBox usa 10.0.1.99")
    assert t2.entity_count() == before2, t2.mint_calls
    assert t2.mint_calls == [], t2.mint_calls



# =============================================================================
# CRITIC ROUND 1 — blocking defects A + B and minors 1 + 2.
# =============================================================================

def _two_entity_seat_with_pet():
    """The live critic seat: the SPEAKER plus DevBox(10.0.1.10), and the speaker's
    live (user, has_pet, gata['fraggle']) edge."""
    t = _default_tenant()
    t.attributes.append(_attr(SERVIDOR, "has_ip", "10.0.1.10"))
    t.facts.append({"id": 3001, "subject_id": USER, "object_id": GATA,
                    "rel_type": "has_pet", "superseded_at": None})
    return t


def test_agreement_binds_retargeted_final_target(monkeypatch):
    """DEFECT A (live-measured): first-person cat sentence on a two-entity seat. The
    gate exempts the speaker, then the SS4 axis rungs renamed has_pet -> has_ip (shared
    head) and re-targeted the tenant-unique holder — retiring DevBox.has_ip for a CAT
    sentence. The agreement discipline must bind the FINAL (subject, attribute): the
    ungrounded rename/re-target refuses, and the sentence's REAL object (Fraggle, the
    speaker's actual pet edge) is what retires."""
    t = _two_entity_seat_with_pet()
    # the live shape: NO stated object (the retraction-extractor gave subject+rel only)
    extraction = {"subject": "user", "rel_type": "has_pet",
                  "object": None, "action": "remove",
                  "dimension": "RELATIONAL", "confidence": 0.9}
    env = Env(monkeypatch, t, extraction, intent="RETRACTION")
    out = env.post("Please forget that I have a cat named Fraggle.")

    live_ip = [a for a in t.attributes
               if a["entity_id"] == SERVIDOR and a["attribute"] == "has_ip"
               and not a["superseded_at"]]
    assert live_ip, f"DevBox.has_ip must survive a cat sentence: {out}"
    live_pet = [f for f in t.facts
                if f["subject_id"] == USER and f["rel_type"] == "has_pet"
                and not f["superseded_at"]]
    assert not live_pet, (
        "the sentence DOES name Fraggle: the speaker's own has_pet edge is the intent "
        "and must retire")
    assert t.mint_calls == [], t.mint_calls


def test_retarget_refusal_when_nothing_grounds(monkeypatch):
    """DEFECT A, pure refusal shape: the same rung machinery, but a sentence that names
    no stored thing at all — the re-target must refuse with no_match, writing nothing."""
    t = _two_entity_seat_with_pet()
    counts = (t.entity_count(), t.live_fact_count(), t.live_attr_count())
    extraction = {"subject": "user", "rel_type": "has_pet",
                  "object": "unicorn", "action": "remove",
                  "dimension": "RELATIONAL", "confidence": 0.9}
    env = Env(monkeypatch, t, extraction, intent="RETRACTION")
    out = env.post("Forget that my unicorn has wings.")

    assert out["status"] == "no_match", out
    assert (t.entity_count(), t.live_fact_count(),
            t.live_attr_count()) == counts, "nothing may be written"
    assert not t.history, t.history


def test_relational_object_must_ground(monkeypatch):
    """DEFECT B (live-measured): 'Forget that my unicorn has wings.' matched the live
    (user, has_pet, fraggle) edge on (subject, rel) alone — zero sentence-token
    grounding of the object — and superseded it. The relational leg must ground the
    matched object: unicorn/wings ground nothing stored -> no_match, nothing written."""
    t = _two_entity_seat_with_pet()
    counts = (t.entity_count(), t.live_fact_count(), t.live_attr_count())
    extraction = {"subject": "user", "rel_type": "has_pet",
                  "object": "unicorn", "action": "remove",
                  "dimension": "RELATIONAL", "confidence": 0.9}
    env = Env(monkeypatch, t, extraction, intent="RETRACTION")
    out = env.post("Forget that my unicorn has wings.")

    assert out["status"] == "no_match", out
    live_pet = [f for f in t.facts
                if f["subject_id"] == USER and f["rel_type"] == "has_pet"
                and not f["superseded_at"]]
    assert live_pet, "the un-named live edge must survive"
    assert (t.entity_count(), t.live_fact_count(),
            t.live_attr_count()) == counts
    assert not t.history, t.history


def test_relational_object_grounding_disambiguates(monkeypatch):
    """DEFECT B positive control: two pet edges, the sentence names ONE of them — the
    grounding filter is the disambiguation, and exactly the named edge retires."""
    t = _two_entity_seat_with_pet()
    REX = "77777777-7777-4777-8777-777777777777"
    t.add_entity(REX, "Animal", ["rex"])
    t.facts.append({"id": 3002, "subject_id": USER, "object_id": REX,
                    "rel_type": "has_pet", "superseded_at": None})
    extraction = {"subject": "user", "rel_type": "has_pet",
                  "object": "rex", "action": "remove",
                  "dimension": "RELATIONAL", "confidence": 0.9}
    env = Env(monkeypatch, t, extraction, intent="RETRACTION")
    out = env.post("olvida que tengo un gato que se llama Rex")

    assert out["status"] == "corrected", out
    live = [(f["object_id"]) for f in t.facts
            if f["subject_id"] == USER and f["rel_type"] == "has_pet"
            and not f["superseded_at"]]
    assert live == [GATA], f"only Rex retires; Fraggle survives: {live}"


def test_turn_window_boundary_14min(monkeypatch):
    """Minor (1): the attribution window is now 3 minutes. A harvest supersede 14
    minutes ago is a PREVIOUS turn — a value-identical re-correction must answer
    already_current, never claim this turn's supersede."""
    t = _devbox_ip_tenant()
    # faithful harvest model: the single (entity, attribute) row is UPDATED in place
    t.attributes[0]["value_text"] = "10.0.1.10"
    t.history.append({
        "entity_id": SERVIDOR, "attribute": "has_ip",
        "prior_value_text": "10.0.1.5", "prior_value_int": None,
        "prior_value_float": None, "prior_value_date": None,
        "new_value_text": "10.0.1.10", "new_value_int": None,
        "action": "superseded", "cause": "ingest_restate",
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=14)})
    extraction = {"subject_name": "DevBox", "old_rel_type": "has_ip",
                  "old_value": "10.0.1.5", "new_rel_type": "has_ip",
                  "new_value": "10.0.1.10", "dimension": "SCALAR",
                  "confidence": 0.9}
    env = Env(monkeypatch, t, extraction)
    out = env.post("La IP de DevBox es 10.0.1.10.")

    assert out["status"] == "already_current", out
    assert out.get("facts_superseded") == 0, out


def test_lane_mint_detection_uses_pre_mint_set(monkeypatch):
    """Minor (2): resolve() REDIRECTING to an existing entity (a path resolve_lookup
    lacks) must NOT be flagged as a lane mint — the old uuid-inequality detection
    marked a pre-existing entity lane-minted and the refusal tried to delete it. The
    pre-mint entity-id SET decides; a redirected refusal leaves the entity intact."""
    t = _default_tenant()
    t.resolve_redirect = {"mi peso": GATA}   # resolve() redirects; lookup misses
    extraction = {"subject": "mi peso", "rel_type": "tiene",
                  "object": "alas", "action": "remove",
                  "dimension": "RELATIONAL", "confidence": 0.9}
    env = Env(monkeypatch, t, extraction, intent="RETRACTION")
    # the sentence grounds gata (the redirect target), so the gate passes and the lane
    # reaches its honest not-found _refuse - which must NOT compensate a pre-existing
    # entity the lane never minted.
    out = env.post("olvida el peso de la gata")

    assert out["status"] == "failed", out
    assert GATA in t.entities, "a pre-existing entity must survive a lane refusal"
    gata_aliases = [a for a, ent in t.aliases.items() if ent == GATA]
    assert set(gata_aliases) == {"gata", "fraggle"}, t.aliases
    assert t.mint_calls == [], t.mint_calls


# =============================================================================
# Critic round 2 - DUTY F: the post-gate SUBJECT swaps bind to the user's words
# =============================================================================

def _inverted_capture_tenant(inverse_row=True):
    """GATA was captured INVERTED: (gata, pet_of, user) instead of (user, has_pet,
    gata). The gate-checked subject (user) holds ZERO forward has_pet rows."""
    t = _default_tenant()
    if inverse_row:
        t.facts.append({"id": 4001, "subject_id": GATA, "rel_type": "pet_of",
                        "object_id": USER, "superseded_at": None})
    return t


def test_inverse_swap_ungrounded_subject_refused(monkeypatch):
    """DUTY F step 2: the inverse swap moves the destructive write onto object_uuid
    AFTER the agreement gate. When the sentence never names the swapped-in subject,
    the lane refuses - nothing is written. Live shape: the user's sentence is pure
    first-person; the extraction's object surface ('gata') came from the extractor,
    not the words."""
    t = _inverted_capture_tenant()
    extraction = {"subject": "user", "rel_type": "has_pet",
                  "object": "gata", "action": "remove",
                  "dimension": "RELATIONAL", "confidence": 0.9}
    env = Env(monkeypatch, t, extraction, intent="RETRACTION")
    env.mp.setattr(env.M, "_REL_TYPE_META",
                   {"has_pet": {"inverse_rel_type": "pet_of"}})
    out = env.post("Forget that I have a pet.")

    assert out["status"] == "no_match", out
    assert t.facts[0]["superseded_at"] is None, \
        "the inverted row must never be superseded by an ungrounded swap"
    assert not t.staged, t.staged


def test_inverse_swap_grounded_subject_lands(monkeypatch):
    """DUTY F step 2 positive control: when the sentence DOES name the swapped-in
    subject, the inverse swap is exactly right and lands."""
    t = _inverted_capture_tenant()
    extraction = {"subject": "user", "rel_type": "has_pet",
                  "object": "gata", "action": "remove",
                  "dimension": "RELATIONAL", "confidence": 0.9}
    env = Env(monkeypatch, t, extraction, intent="RETRACTION")
    env.mp.setattr(env.M, "_REL_TYPE_META",
                   {"has_pet": {"inverse_rel_type": "pet_of"}})
    out = env.post("olvida que gata es mi mascota")

    assert out["status"] == "corrected", out
    assert t.facts[0]["superseded_at"] is not None, "the inverted row retires"
    assert not t.staged, t.staged


def test_object_side_swap_ungrounded_subject_refused(monkeypatch):
    """DUTY F step 4: the object-side lookup swaps the subject onto the FACT'S REAL
    SUBJECT after the gate. (gata, has_pet, spot) stored; 'Forget Spot is our pet.'
    names Spot only - the swap would put GATA's edge on the block. Refuse, write
    nothing."""
    t = _default_tenant()
    t.add_entity(SPOT, "Animal", ["spot"])
    t.facts.append({"id": 4002, "subject_id": GATA, "rel_type": "has_pet",
                    "object_id": SPOT, "superseded_at": None})
    extraction = {"subject": "spot", "rel_type": "has_pet",
                  "object": None, "action": "remove",
                  "dimension": "RELATIONAL", "confidence": 0.9}
    env = Env(monkeypatch, t, extraction, intent="RETRACTION")
    out = env.post("Forget Spot is our pet.")

    assert out["status"] == "no_match", out
    assert t.facts[0]["superseded_at"] is None, "gata's edge must survive"
    assert not t.staged, t.staged


def test_object_side_swap_speaker_lands(monkeypatch):
    """DUTY F step 4 positive control: the canonical (user, has_pet, spot) capture
    swaps the subject onto the SPEAKER - exempt by identity - and lands exactly as
    it always has."""
    t = _default_tenant()
    t.add_entity(SPOT, "Animal", ["spot"])
    t.facts.append({"id": 3001, "subject_id": USER, "rel_type": "has_pet",
                    "object_id": GATA, "superseded_at": None})
    t.facts.append({"id": 4003, "subject_id": USER, "rel_type": "has_pet",
                    "object_id": SPOT, "superseded_at": None})
    extraction = {"subject": "spot", "rel_type": "has_pet",
                  "object": None, "action": "remove",
                  "dimension": "RELATIONAL", "confidence": 0.9}
    env = Env(monkeypatch, t, extraction, intent="RETRACTION")
    out = env.post("Forget Spot is our pet.")

    assert out["status"] == "corrected", out
    live = [f["object_id"] for f in t.facts
            if f["subject_id"] == USER and f["rel_type"] == "has_pet"
            and not f["superseded_at"]]
    assert live == [GATA], f"spot's edge retires; gata's survives: {live}"


def test_staged_object_side_swap_ungrounded_refused(monkeypatch):
    """DUTY F step 5: the staged twin of the object-side swap obeys the same
    discipline."""
    t = _default_tenant()
    t.add_entity(SPOT, "Animal", ["spot"])
    t.staged.append({"id": 501, "subject_id": GATA, "rel_type": "has_pet",
                     "object_id": SPOT, "deleted_at": None,
                     "promoted_at": None, "expires_at": None})
    extraction = {"subject": "spot", "rel_type": "has_pet",
                  "object": None, "action": "remove",
                  "dimension": "RELATIONAL", "confidence": 0.9}
    env = Env(monkeypatch, t, extraction, intent="RETRACTION")
    out = env.post("Forget Spot is our pet.")

    assert out["status"] == "no_match", out
    assert t.staged and t.staged[0]["deleted_at"] is None, \
        "the staged row must survive untouched"


# =============================================================================
# Critic round 2 - the stopword-alias lockout (both ends)
# =============================================================================

def test_stopword_alias_does_not_lock_out_agreement(monkeypatch):
    """Moderate (a): a junk capture-debris entity aliased 'that' grounded in EVERY
    sentence containing the word, keeping the agreement gate in permanent ambiguous
    refusal on such a seat. The stopword screen drops it; the real subject resolves."""
    t = _devbox_ip_tenant()
    t.add_entity(JUNK, "Concept", ["that"])
    extraction = {"subject": "devbox", "rel_type": "has_ip",
                  "object": None, "action": "remove",
                  "dimension": "SCALAR", "confidence": 0.9}
    env = Env(monkeypatch, t, extraction, intent="RETRACTION")
    out = env.post("Forget that DevBox has an IP.")

    assert out["status"] == "corrected", out
    rows = [a for a in t.attributes
            if a["entity_id"] == SERVIDOR and a["attribute"] == "has_ip"
            and not a["superseded_at"]]
    assert not rows, "the has_ip attribute retires"


def test_capture_never_mints_stopword_aliases(monkeypatch):
    """Moderate (b): the REAL registry refuses to mint a pure function-word surface -
    the measured wound is a harvest debris entity aliased 'that'. Naming frames keep
    their opt-out (the #23 family shape); register_alias refuses the debris shape."""
    from src.entity_registry.registry import (
        EntityRegistry, StopwordSurfaceError, _stopword_surface_refused)

    class _RCur:
        def __init__(self):
            self.rows = []
            self.rowcount = 0

        def execute(self, s, p=None):
            self.rows.append(" ".join(str(s).split()).lower())

        def fetchone(self):
            return None

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _RDB:
        def __init__(self):
            self.cur = _RCur()

        def cursor(self):
            return self.cur

        def commit(self):
            pass

        def rollback(self):
            pass

    db = _RDB()
    reg = EntityRegistry(db, schema_name="foss_pins_tenant")
    uid = USER

    # the module screen itself (the shared closed-class list, en + es)
    assert _stopword_surface_refused("that")
    assert _stopword_surface_refused("que y")
    assert not _stopword_surface_refused("gata")
    assert not _stopword_surface_refused("after the network change"), \
        "multiword debris WITH content tokens is not the stopword class"

    # the mint seam: a bare stopword surface never mints
    with pytest.raises(StopwordSurfaceError):
        reg.resolve(uid, "that")
    writes = [s for s in db.cur.rows
              if s.startswith("insert into entities")
              or s.startswith("insert into entity_aliases")]
    assert not writes, "a stopword surface must never mint"

    # the naming-frame opt-out survives (issue #23 family shape)
    out = reg.resolve(uid, "that", name_intent=True)
    assert out, "a genuine naming frame keeps its lane"

    # register_alias: the debris shape is refused without writing (the naming-frame
    # mint above legitimately wrote, so assert no NEW alias insert follows)
    _n0 = len([s for s in db.cur.rows
               if s.startswith("insert into entity_aliases")])
    r = reg.register_alias(GATA, "that", is_preferred=False)
    assert r.get("written") is False and r.get("refused") is True, r
    _n1 = len([s for s in db.cur.rows
               if s.startswith("insert into entity_aliases")])
    assert _n1 == _n0, "the refused registration must write nothing"


# ── FOSS-016-it: the stopword screen is LANGUAGE-NATIVE ────────────────────────────

def _it_model_installed_foss016() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("it_core_news_sm") is not None
    except Exception:  # noqa: BLE001
        return False


def test_stopword_screen_covers_italian_at_mint_and_registration(monkeypatch):
    """FOSS-016-it: 'che' (and the other it closed-class words) are refused by the
    screen, at the MINT seam and at the NON-PREFERRED REGISTRATION seam - the lane-C
    round-2 wound (a harvest-minted stopword alias ambiguity-locking retractions) is
    closed for Italian. A content-bearing it surface still mints. en/es verdicts are
    byte-identical (pinned by test_capture_never_mints_stopword_aliases above)."""
    from src.entity_registry.registry import (
        EntityRegistry, StopwordSurfaceError, _stopword_surface_refused)

    # the screen: it closed-class refused, content-bearing it surface kept
    assert _stopword_surface_refused("che"), "che must be refused (floor or model)"
    assert _stopword_surface_refused("non")
    assert _stopword_surface_refused("il")
    assert not _stopword_surface_refused("gattino"), "content-bearing it surface"
    assert not _stopword_surface_refused("server principale"), "multiword with content"

    class _RCur:
        def __init__(self):
            self.rows = []
        def execute(self, s, p=None):
            self.rows.append(" ".join(str(s).split()).lower())
        def fetchone(self):
            return None
        def fetchall(self):
            return []
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _RDB:
        def __init__(self):
            self.cur = _RCur()
        def cursor(self):
            return self.cur
        def commit(self):
            pass
        def rollback(self):
            pass

    db = _RDB()
    reg = EntityRegistry(db, schema_name="foss016_it_tenant")

    # the mint seam: 'che' never mints
    with pytest.raises(StopwordSurfaceError):
        reg.resolve(USER, "che")
    writes = [s for s in db.cur.rows
              if s.startswith("insert into entities")
              or s.startswith("insert into entity_aliases")]
    assert not writes, "an it stopword surface must never mint"

    # the agreement lockout seam: 'che' cannot register as a non-preferred alias
    r = reg.register_alias(GATA, "che", is_preferred=False)
    assert r.get("written") is False and r.get("refused") is True, r

    # the naming-frame opt-out survives (#23 family shape, same as en/es)
    out = reg.resolve(USER, "che", name_intent=True)
    assert out, "a genuine it naming frame keeps its lane"


def test_stopword_primary_verdict_is_the_models_own_stop_list(monkeypatch):
    """FOSS-016-it PRIMARY SHAPE: the screen's verdict is the LOADED model's
    tok.is_stop (language-native; the model IS the language), with the hand set as the
    NO-MODEL floor only. Pinned with a word the model's stop list carries but the hand
    floor deliberately does not ('poi'): model up -> refused; model down -> not refused
    (the floor answers alone, byte-exact to the hand set)."""
    import src.extraction.linguistics as L
    from src.entity_registry.registry import _stopword_surface_refused
    if not _it_model_installed_foss016():
        pytest.skip("it_core_news_sm not installed - the model-native verdict runs only on an it stack")
    assert _stopword_surface_refused("poi"), "the model's stop list answers"
    # the no-model path falls to the floor: 'poi' is NOT in the hand set
    monkeypatch.setattr(L, "_get_nlp", lambda: None)
    assert not _stopword_surface_refused("poi"), "the floor answers alone, exactly"
    # ...but the floor still refuses the it closed-class words without a model
    assert _stopword_surface_refused("che"), "the floor keeps the it hole closed"


# =============================================================================
# Critic round 2 - the turn-window wording
# =============================================================================

def _window_history_fixture(t, minutes_ago):
    """Faithful harvest model: the single (entity, attribute) row is UPDATED in
    place and one history row records the supersede."""
    t.attributes[0]["value_text"] = "10.0.1.10"
    t.history.append({
        "entity_id": SERVIDOR, "attribute": "has_ip",
        "prior_value_text": "10.0.1.5", "prior_value_int": None,
        "prior_value_float": None, "prior_value_date": None,
        "new_value_text": "10.0.1.10", "new_value_int": None,
        "action": "superseded", "cause": "ingest_restate",
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)})


def test_recent_correction_wording_not_this_turn(monkeypatch):
    """Minor: a supersede INSIDE the 3-minute window but from the PREVIOUS turn must
    not be claimed as 'this correction turn' - it reads 'a recent correction'."""
    t = _devbox_ip_tenant()
    _window_history_fixture(t, minutes_ago=1)
    extraction = {"subject_name": "DevBox", "old_rel_type": "has_ip",
                  "old_value": "10.0.1.5", "new_rel_type": "has_ip",
                  "new_value": "10.0.1.10", "dimension": "SCALAR",
                  "confidence": 0.9}
    env = Env(monkeypatch, t, extraction)
    out = env.post("La IP de DevBox es 10.0.1.10.")

    assert out["status"] == "corrected", out
    assert "a recent correction" in out["message"], out["message"]
    assert "this correction turn" not in out["message"], out["message"]


def test_this_turn_supersede_wording_kept(monkeypatch):
    """Minor: a supersede PROVABLY from this turn keeps the precise wording."""
    t = _devbox_ip_tenant()
    _window_history_fixture(t, minutes_ago=-5)   # created AFTER the request began
    extraction = {"subject_name": "DevBox", "old_rel_type": "has_ip",
                  "old_value": "10.0.1.5", "new_rel_type": "has_ip",
                  "new_value": "10.0.1.10", "dimension": "SCALAR",
                  "confidence": 0.9}
    env = Env(monkeypatch, t, extraction)
    out = env.post("La IP de DevBox es 10.0.1.10.")

    assert out["status"] == "corrected", out
    assert "this correction turn" in out["message"], out["message"]
