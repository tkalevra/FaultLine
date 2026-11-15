"""Shared E2E rig for the lane-B ingest pins (#64 #67 #68 #38 #53 #57 #60).

Drives the REAL /harvest-spans → /ingest (→ /query) on a throwaway ``*_test`` POSTGRES_DSN tenant with
the PRODUCTION values of the spine flags (deploy compose + the measured-on-prod list the #35-37 pins
carry). Flags are applied by a module-scoped fixture on env AND the already-imported module constants
and undone afterwards — never module-level os.environ (#63). Skips, disclosed, without a DSN.
"""
from __future__ import annotations

import os
import sys
import time
import uuid

import pytest

PROD_FLAGS = {
    "SENTENCE_PIPELINE": "true", "SPINE_COVERAGE_GATE": "true", "SPINE_IMPLICATIVE_XCOMP": "true",
    "SPINE_NAMING_CHAIN": "true", "SPINE_PA_CORE": "1", "SPINE_PENDING_GROUNDING": "true",
    "SPINE_POSSESSIVE_ALIENABILITY": "true", "QUERY_NAME_INTENT_SURFACING": "1",
    "QUERY_WALK_READS_ATTRIBUTES": "1", "INGEST_QUEUE_CONCEPT_GROUNDING": "1",
    "LINGUISTIC_LAYER": "true", "SPACY_MODEL": "en_core_web_sm",
}


def dsn() -> str:
    return os.environ.get("POSTGRES_DSN", "")


def apply_flags(mp, flags=PROD_FLAGS):
    for k, v in flags.items():
        mp.setenv(k, v)
        for modname in ("src.api.main", "src.extraction.linguistics", "src.api.turn_budget"):
            mod = sys.modules.get(modname)
            if mod is None or not hasattr(mod, k):
                continue
            cur = getattr(mod, k)
            if isinstance(cur, bool):
                new = v.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(cur, float):
                new = float(v)
            else:
                new = v
            mp.setattr(mod, k, new)
    import src.extraction.linguistics as L
    mp.setattr(L, "_SPACY_MODEL", "en_core_web_sm")
    if L._nlp is None:
        mp.setattr(L, "_load_attempted", False)


def module_flags_fixture():
    """Return a module-scoped autouse fixture applying PROD_FLAGS (deriver-level pins)."""
    @pytest.fixture(scope="module", autouse=True)
    def _module_flags():
        import src.extraction.linguistics  # noqa: F401
        mp = pytest.MonkeyPatch()
        apply_flags(mp)
        yield
        mp.undo()
    return _module_flags


def e2e_fixture():
    @pytest.fixture(scope="module")
    def e2e():
        d = dsn()
        if not d or not d.rsplit("/", 1)[-1].endswith("_test"):
            pytest.skip("E2E needs a throwaway *_test POSTGRES_DSN (skipped — disclosed)")
        mp = pytest.MonkeyPatch()
        mp.delenv("SAAS_MODE", raising=False)
        mp.setenv("FAULTLINE_MIGRATIONS_DIR", "./migrations")
        mp.setenv("QDRANT_URL", "http://127.0.0.1:1")
        mp.setenv("PROVISIONING_CORTEX_BUDGET_SEC", "2")
        import psycopg2
        from src.provisioning.schema_manager import create_user_schema, derive_user_slug_from_uuid
        import src.api.main as M
        import src.extraction.linguistics as L
        apply_flags(mp)
        user_id = str(uuid.uuid4())
        slug = derive_user_slug_from_uuid(user_id)
        conn = psycopg2.connect(d)
        schema, status = create_user_schema(user_id, slug, conn)
        assert status == "ready", status
        with conn.cursor() as cur:
            cur.execute("INSERT INTO public.users (user_id, email, display_name, slug) VALUES "
                        "(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (user_id, f"{slug}@example.invalid", slug, slug))
            cur.execute("INSERT INTO public.user_provisioning (user_id, schema_name, status, "
                        "ready_at) VALUES (%s,%s,'ready',now()) ON CONFLICT DO NOTHING",
                        (user_id, schema))
        conn.commit()
        conn.close()
        mp.setattr(M, "_INGEST_ENABLED", True)
        M._TENANT_READY_CACHE.add(user_id)

        class _D:
            def get(self, k):
                return None

            def setex(self, k, t, v):
                return True

            def ping(self):
                return True

        class _Idem:
            redis_url = "dict://test"
            ttl = 3600
            client = _D()

        mp.setattr(M, "_idempotency_mgr", _Idem())
        from starlette.testclient import TestClient
        with TestClient(M.app) as client:
            t0 = time.time()
            while L._nlp is None and time.time() - t0 < 180:
                L._get_nlp()
                time.sleep(0.5)
            yield {"client": client, "user_id": user_id, "schema": schema, "M": M, "mp": mp}
        mp.undo()
        conn = psycopg2.connect(d)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            cur.execute(f'DROP SCHEMA IF EXISTS "flagent_{user_id.replace("-", "_")}" CASCADE')
            cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM public.users WHERE user_id = %s", (user_id,))
        conn.close()
    return e2e


def rows(schema, sql, params=()):
    import psycopg2
    conn = psycopg2.connect(dsn())
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []
    finally:
        conn.commit()
        conn.close()


def state(env, text, atoms=None, source="mcp"):
    """One STATEMENT turn through the live harvest → /ingest. ``atoms`` stubs the atomizer; None =
    deterministic segmentation."""
    M = env["M"]
    import src.extraction.reframe as RF
    mp = pytest.MonkeyPatch()
    try:
        if atoms is not None:
            async def _stub(t, u, m=None):
                use = list(atoms) if t.strip() == text.strip() else [t]
                return RF.ReframeResult(atoms=[RF.Atom(text=a, source_span=a) for a in use],
                                        used_llm=True, rejected_count=0)
            mp.setattr(RF, "reframe_to_atomic", _stub)
            mp.setattr(M, "SPINE_DETERMINISTIC_SEGMENTATION", False, raising=False)
        else:
            mp.setattr(M, "SPINE_DETERMINISTIC_SEGMENTATION", True, raising=False)
        c = env["client"]
        h = c.post("/harvest-spans", json={"text": text, "user_id": env["user_id"]})
        assert h.status_code == 200, h.text[:300]
        edges = h.json().get("edges") or []
        if edges:
            r = c.post("/ingest", json={"text": text, "user_id": env["user_id"], "edges": edges,
                                        "source": source})
            assert r.status_code == 200, r.text[:300]
        return edges
    finally:
        mp.undo()


def ingest_edges(env, text, edges, source="mcp", source_ref=None):
    body = {"text": text, "user_id": env["user_id"], "edges": edges, "source": source}
    if source_ref:
        body["source_ref"] = source_ref
    r = env["client"].post("/ingest", json=body)
    assert r.status_code == 200, r.text[:300]
    return r.json()


def ask(env, q):
    r = env["client"].post("/query", json={"text": q, "user_id": env["user_id"]}).json()
    return [(f.get("subject"), f.get("rel_type"), str(f.get("object")))
            for f in (r.get("facts") or [])]


def user_aliases(env):
    return {r[0] for r in rows(env["schema"],
                               "SELECT alias FROM entity_aliases WHERE entity_id = %s",
                               (env["user_id"],))}


def fact_rows(env):
    """Every live relational row as (subject_surface, rel, object_surface) — surfaces via aliases."""
    rs = rows(env["schema"], """
        SELECT f.subject_id, f.rel_type, f.object_id FROM facts f WHERE f.superseded_at IS NULL
        UNION ALL
        SELECT s.subject_id, s.rel_type, s.object_id FROM staged_facts s WHERE s.deleted_at IS NULL
    """)
    names = {}
    for eid, alias, pref in rows(env["schema"],
                                 "SELECT entity_id, alias, is_preferred FROM entity_aliases"):
        if pref or eid not in names:
            names[eid] = alias
    out = set()
    for s, r, o in rs:
        sn = "user" if s == env["user_id"] else names.get(s, s)
        on = "user" if o == env["user_id"] else names.get(o, o)
        out.add((sn, r, on))
    return out


def attr_rows(env):
    return set(rows(env["schema"], "SELECT entity_id, attribute, value_text FROM entity_attributes "
                                   "WHERE superseded_at IS NULL"))
