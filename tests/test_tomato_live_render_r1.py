"""TOMATO LIVE RENDER (issue #31 R1) — pins.

The deploy-#16 live wound (fresh seat 843d39dc, aurora): "My tomato plants were planted
on May 15th, 2026 and each one is roughly 90 centimeters tall." ingested the #30 row
correctly at the DERIVER, but the how-tall render answered "tomato plants used to be
related to related measure / You own tomato plants" — no 90 centimeters.

ADJUDICATION (measured, live + in-process): the LIVE atomizer's atoms are BYTE-IDENTICAL
to the x30 stub (captured verbatim from the deployed container's REFRAME cache):

    ["My tomato plants were planted on May 15th, 2026.",
     "Each one is roughly 90 centimeters tall."]

The differential was NEVER the atom shape — it was SPINE_PENDING_GROUNDING (production
sets true; the x30 pin env ran the code default OFF). Under it, the HALF-2 grow wire
(main.py::_plan_pending_scalar_grounding) treated the #30/#29-W2 ENGINE-CARRIER flat
scalar (tomato plants, related_measure, '90 centimeters') as a "user novel-attribute
orphan" (novel on a FRESH tenant, numeric measure, unit HIT, generic datatype) and
RE-HOMED it: dropped the flat row, minted a junk entity from the REL NAME ('related
measure', typed Person), filed (tomato plants, related_to, junk) — the "used to be
related to related measure" render row (temporal past) — and hung height='90 centimeters'
one hop OFF the subject where the measure-interrogative walk (which reads the ANCHOR'S
OWN attributes) can never reach it. Order-dependent: a tenant that had already minted
related_measure hid the wound via the collision guard — only the FIRST emission on a
fresh tenant exposed it, which is why the x30 in-process proof was green.

THE FIX (one seam, structural): `related_measure` — the deterministic writer's
generic-measure carrier — joins _PENDING_GROUND_SKIP_RELS (the set that exists for
exactly this category error: "canonical scalars own their own construction; re-homing
them would be a category error"). An attribute/carrier name is never a referent (#23
family).

Pins:
  E2E  — the LIVE atom shape under PRODUCTION PARITY (SPINE_PENDING_GROUNDING=true):
         exactly ONE related_measure row on tomato plants, no 'related measure' entity
         or related_to twin anywhere in the tenant, and the how-tall renders the
         magnitude + unit.
  UNIT — the planner keeps the engine-carrier flat scalar untouched AND still re-homes
         a GENUINE user orphan (the wire's intended behavior is not over-reached away).

Deterministic per PD3 (the atomizer is stubbed at the MODULE seam — src.extraction.reframe,
never a main.py attribute). SAAS_MODE popped for the whole module (conftest pins it 1; the
open-core harvest is fail-closed without a bound tenant brain — the x30 discipline).
"""
import os
import time
import uuid

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("SENTENCE_PIPELINE", "true")
# PRODUCTION PARITY — the deploy-#16 differential. Must be set BEFORE main imports
# (the flag is read at module load).
os.environ.setdefault("SPINE_PENDING_GROUNDING", "true")

TURN = ("My tomato plants were planted on May 15th, 2026 and each one is roughly "
        "90 centimeters tall.")
ATOMS = [
    "My tomato plants were planted on May 15th, 2026.",
    "Each one is roughly 90 centimeters tall.",
]
Q = "How tall are my tomato plants?"

_DSN = os.environ.get("POSTGRES_DSN", "")
_E2E = bool(_DSN) and "test" in _DSN


# ── UNIT: the planner leaves the engine-carrier flat scalar alone (the fix) ────────────────────

def _planner_edges():
    import src.api.main as M
    return [
        {"subject": "tomato plants", "rel_type": "related_measure",
         "object": "90 centimeters", "object_datatype": "string"},
    ]


def test_planner_keeps_engine_carrier_flat_scalar():
    import src.api.main as M
    plan = M._plan_pending_scalar_grounding(
        _planner_edges(), seeded_meta={}, unit_map={"centimeter": "height"},
        existing_rel_surfaces=set(), bare_family_surfaces=set())
    assert not plan["changed"], plan
    assert plan["concepts"] == [] and plan["unit_misses"] == [], plan
    rows = [(e.get("subject"), e.get("rel_type"), e.get("object")) for e in plan["edges"]]
    assert ("tomato plants", "related_measure", "90 centimeters") in rows, rows


def test_planner_still_rehomes_a_genuine_user_orphan():
    # control: the HALF-2 wire's INTENDED prey — a user-surface novel attribute with a
    # numeric measure — is still re-homed. The fix names OUR carrier, never user language.
    import src.api.main as M
    plan = M._plan_pending_scalar_grounding(
        [{"subject": "user", "rel_type": "commute", "object": "45 minutes",
          "object_datatype": "string"}],
        seeded_meta={}, unit_map={"minute": "duration"},
        existing_rel_surfaces=set(), bare_family_surfaces=set())
    assert plan["changed"], plan
    rows = {(e.get("subject"), e.get("rel_type"), e.get("object"),
             e.get("object_datatype")) for e in plan["edges"]}
    assert ("user", "related_to", "commute", None) in rows, rows
    assert ("commute", "duration", "45 minutes", "quantity") in rows, rows
    assert ("user", "commute", "45 minutes", "string") not in rows, rows
    assert "commute" in plan["concepts"], plan


def test_planner_novel_rel_hit_is_irrelevant_for_carrier():
    # the carrier is skipped even when every OTHER novelty signal fires (absent overlay,
    # absent live rel_types — the FRESH-tenant condition that exposed the live wound)
    import src.api.main as M
    plan = M._plan_pending_scalar_grounding(
        _planner_edges(), seeded_meta={}, unit_map={"centimeter": "height"},
        existing_rel_surfaces=set(), bare_family_surfaces=frozenset({"quantity"}))
    assert not plan["changed"], plan


# ── E2E: the LIVE atom shape at production parity ─────────────────────────────────────────────

@pytest.mark.skipif(not _E2E, reason="needs POSTGRES_DSN pointing at a throwaway test DB")
class TestE2E:
    @pytest.fixture(autouse=True, scope="class")
    def _saas_off(self):
        # conftest pins SAAS_MODE=1; the open-core harvest is FAIL-CLOSED without a bound
        # tenant brain — pop it for this class only (x29/x30 precedent). ALSO pop
        # WGM_LLM_MODEL: no brain box in the pin env — the deterministic spine under
        # test runs alone.
        _had = "SAAS_MODE" in os.environ
        _val = os.environ.pop("SAAS_MODE", None)
        _had_m = "WGM_LLM_MODEL" in os.environ
        _val_m = os.environ.pop("WGM_LLM_MODEL", None)
        yield
        if _had:
            os.environ["SAAS_MODE"] = _val
        if _had_m:
            os.environ["WGM_LLM_MODEL"] = _val_m

    @pytest.fixture(scope="class")
    def stack(self):
        import psycopg2
        import src.api.main as M
        from starlette.testclient import TestClient

        M._INGEST_ENABLED = True

        class _D:
            def __init__(self): self.d = {}
            def get(self, k): return self.d.get(k)
            def setex(self, k, t, v): self.d[k] = v; return True
            def ping(self): return True
        class _Idem:
            redis_url = "dict://test"; ttl = 3600; client = _D()
        M._idempotency_mgr = _Idem()

        import src.extraction.reframe as _rm

        class _Atom:
            def __init__(self, text): self.text = text
        class _Reframe:
            def __init__(self, atoms): self.atoms = [_Atom(a) for a in atoms]; self.used_llm = True
        _orig = _rm.reframe_to_atomic

        async def _stub(text, user_id, messages=None, **kw):
            if " ".join((text or "").lower().split()) == " ".join(TURN.lower().split()):
                return _Reframe(ATOMS)
            return await _orig(text, user_id, messages, **kw)
        _rm.reframe_to_atomic = _stub   # MODULE seam (the x30 lesson)

        with TestClient(M.app) as client:
            from src.extraction import linguistics as _L
            t0 = time.time()
            while (_L._nlp is None or _L._get_nlp_ner() is None) and time.time() - t0 < 180:
                time.sleep(0.5)
            assert _L._nlp is not None
            yield M, client

    def _seat(self, M):
        import psycopg2
        from src.provisioning.schema_manager import (
            create_user_schema, derive_user_slug_from_uuid)
        user_id = str(uuid.uuid4())
        slug = derive_user_slug_from_uuid(user_id)
        conn = psycopg2.connect(_DSN)
        schema, status = create_user_schema(user_id, slug, conn)
        assert status == "ready", status
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO public.users (user_id, email, display_name, slug) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (user_id, f"{slug}@example.invalid", slug, slug))
            cur.execute(
                "INSERT INTO public.user_provisioning (user_id, schema_name, status, ready_at) "
                "VALUES (%s,%s,'ready',now()) ON CONFLICT DO NOTHING", (user_id, schema))
        conn.commit(); conn.close()
        return user_id, schema

    def _ingest_turn(self, stack):
        M, client = stack
        user_id, schema = self._seat(M)
        h = client.post("/harvest-spans", json={"text": TURN, "user_id": user_id})
        assert h.status_code == 200, h.text[:300]
        edges = h.json().get("edges") or []
        # the flat engine-carrier scalar must SURVIVE the grow wire in the harvest output
        assert any(e.get("subject") == "tomato plants" and e.get("rel_type") == "related_measure"
                   and e.get("object") == "90 centimeters" for e in edges), edges
        assert not any(e.get("rel_type") == "related_to"
                       and (e.get("object") or "").strip().lower() == "related measure"
                       for e in edges), edges
        r = client.post("/ingest", json={"text": TURN, "user_id": user_id,
                                         "edges": edges, "source": "mcp"})
        assert r.status_code == 200, r.text[:300]
        return user_id, schema

    def _ask(self, stack, user_id, q):
        _M, client = stack
        resp = client.post("/query", json={"text": q, "user_id": user_id})
        j = resp.json()
        return (j.get("response") or "") + " " + " ".join(
            str(f.get("definition") or "") for f in (j.get("facts") or []))

    def test_live_atoms_one_related_measure_no_twin_renders(self, stack):
        import psycopg2
        user_id, schema = self._ingest_turn(stack)
        c = psycopg2.connect(_DSN)
        try:
            with c.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}")
                # exactly ONE related_measure row ON the referent — no superseded twin
                cur.execute(
                    "SELECT ea.attribute, ea.value_text, ea.superseded_at FROM entity_attributes ea "
                    "JOIN entity_aliases al ON al.entity_id = ea.entity_id "
                    "WHERE al.alias = 'tomato plants' AND ea.attribute = 'related_measure'")
                rows_ = cur.fetchall()
                assert len(rows_) == 1 and rows_[0][1] == "90 centimeters" \
                    and rows_[0][2] is None, rows_
                # NO entity minted from the rel name anywhere in the tenant
                cur.execute("SELECT alias FROM entity_aliases")
                aliases = [a for a, in cur.fetchall()]
                assert "related measure" not in aliases, aliases
                # NO junk link row (the 'used to be related to related measure' render row)
                cur.execute(
                    "SELECT f.rel_type FROM facts f "
                    "JOIN entity_aliases s ON s.entity_id = f.subject_id "
                    "WHERE s.alias = 'tomato plants' AND f.rel_type = 'related_to'")
                assert cur.fetchall() == []
                # the planted date still lands (x30 sibling)
                cur.execute(
                    "SELECT ea.value_text FROM entity_attributes ea "
                    "JOIN entity_aliases al ON al.entity_id = ea.entity_id "
                    "WHERE al.alias = 'tomato plants' AND ea.attribute = 'planted'")
                assert any(str(v or "").startswith("2026-05-15") for v, in cur.fetchall())
        finally:
            c.close()
        a = self._ask(stack, user_id, Q)
        assert "90" in a and "centimeter" in a.lower(), a
        assert "used to be" not in a, a

    def test_how_tall_render_is_the_live_row(self, stack):
        # bar 1 (frozen): the how-tall renders 90 centimeters — the LIVE row, not a twin
        user_id, _schema = self._ingest_turn(stack)
        a = self._ask(stack, user_id, Q)
        assert "90 centimeters" in a, a
