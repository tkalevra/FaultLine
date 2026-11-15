"""FAMILY-MINT SUPPRESSION (issue #21) — the registry/mint seam pins.

THE WOUND (deploy #10/#11, alias rows verified twice on live tenants): after
"I take lisinopril 10 milligrams daily for high blood pressure." the tenant holds an
entity + alias 'lisinopril dosage' though the stored quantity row is correct — and
"What is my lisinopril dosage?" then anchors the phrase-entity. The W1 harvest
suppression filters EDGES beside a landed family scalar; the live mint lanes (GLiNER2
candidate surfaces, LLM relation-fill, correction re-extraction, atomizer reframes of
turns that do contain the family noun) hand the '<substance> <family-noun>' surface
DIRECTLY to EntityRegistry.resolve() — no edge filter can see them.

THE FIX (the issue's option (a)): ``EntityRegistry._measurement_family_compound_redirect``
at the MINT seam (registry.py) — a compound whose TRAILING token is a measurement-family
member (dosage_noun ∪ measure_noun cue rails + canonicals, floor ∪ tenant) and whose
STRIPPED PREFIX resolves to a KNOWN entity (the substance) redirects to the substance's
id and never registers the phrase alias. Structural, no substance list; the prefix-known
requirement is the content-loss guard (a surface like 'High Level' the city, or a song
titled 'Perfect Dosage', mints as today — first exposure of both tokens is never
suppressed). Genuine compound PROPNs ('Unifi Dream Machine', 'blue-ringed octopus')
are unaffected by construction: 'machine'/'octopus' are not family members.

Deterministic per PD3: the fixture owns its env (spine route pinned, SaaS popped for the
open-core seam per the dz14/df18/W1 precedent, the #14 take-frame seeded through the REAL
harvest → /ingest so lisinopril + quantity exist exactly as the battery leaves them).
Requires POSTGRES_DSN pointing ONLY at a throwaway test DB.
"""
import json
import os
import re
import time
import uuid

import pytest

# The LIVE STATEMENT route (the spine) — the same env the deployed composes set.
os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("SENTENCE_PIPELINE", "true")

TURN = "I take lisinopril 10 milligrams daily for high blood pressure."
PHRASE = "lisinopril dosage"
PHRASE_Q = "What is my lisinopril dosage?"

_DSN = os.environ.get("POSTGRES_DSN", "")
_E2E = bool(_DSN) and "test" in _DSN


_E2E_STATE: dict = {}


@pytest.mark.skipif(not _E2E, reason="POSTGRES_DSN not pointed at a throwaway test DB")
class TestFamilyMintSuppression:
    @pytest.fixture(autouse=True, scope="class")
    def _driver(self, request):
        import psycopg2
        import src.api.main as M
        from starlette.testclient import TestClient
        from src.provisioning.schema_manager import (
            create_user_schema, derive_user_slug_from_uuid)

        # conftest's baseline turns SAAS_MODE ON for the whole session; under SaaS the
        # harvest is FAIL-CLOSED without a bound tenant brain (BYO-LLM) — the dz14/df18/W1
        # precedent: this lane pins the OPEN-CORE engine seam, so SaaS is off for this
        # class's scope only (manual save/restore because the fixture is CLASS-scoped and
        # monkeypatch is function-scoped).
        _had_saas = "SAAS_MODE" in os.environ
        _saas_val = os.environ.pop("SAAS_MODE", None)

        pytest.main_mod = M
        M._INGEST_ENABLED = True
        # The dz14/df18/W1 precedent: pin the DETERMINISTIC decomposer so the pin is a
        # deterministic replica of the deployed clean-atom condition (the local box has no LLM
        # brain, so the atomizer-down fallback would exercise a different segmentation than any
        # deploy). The mint seam under test is downstream of segmentation — unaffected by which
        # decomposer feeds it.
        M.SPINE_DETERMINISTIC_SEGMENTATION = True

        class _D:
            def __init__(self): self.d = {}
            def get(self, k): return self.d.get(k)
            def setex(self, k, t, v): self.d[k] = v; return True
            def ping(self): return True

        class _Idem:
            redis_url = "dict://test"; ttl = 3600; client = _D()
        M._idempotency_mgr = _Idem()

        user_id = str(uuid.uuid4())
        slug = derive_user_slug_from_uuid(user_id)
        conn = psycopg2.connect(_DSN)
        schema, status = create_user_schema(user_id, slug, conn)
        assert status == "ready", status
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO public.users (user_id, email, display_name, slug) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (user_id, f"{slug}@example.invalid", slug, slug))
            cur.execute(
                "INSERT INTO public.user_provisioning (user_id, schema_name, status, ready_at) "
                "VALUES (%s, %s, 'ready', now()) ON CONFLICT DO NOTHING",
                (user_id, schema))
        conn.commit()
        conn.close()
        _E2E_STATE["schema"] = schema
        _E2E_STATE["user_id"] = user_id

        # Every accessor runs ONE literal SELECT at the execute site with params bound — no
        # composed queries anywhere in the fixture. The tenant schema is bound at CONNECT time
        # via libpq options (search_path), with the schema name validated against the same
        # shape rule the production registry uses — never interpolated into SQL text.
        _SLUG_RE = re.compile(r"^[a-z0-9_]+$")

        def _connect():
            import psycopg2
            assert _SLUG_RE.match(schema), f"unexpected schema shape: {schema!r}"
            return psycopg2.connect(_DSN, options=f"-csearch_path={schema}")

        def _fetch(exec_fn):
            c = _connect()
            try:
                with c.cursor() as cur:
                    return exec_fn(cur)
            finally:
                c.close()

        def rows_alias(entity_alias):
            return _fetch(lambda cur: (
                cur.execute("SELECT entity_id FROM entity_aliases "
                            "WHERE alias = %s ORDER BY is_preferred DESC LIMIT 1",
                            (entity_alias,)) or cur.fetchall()))

        _E2E_STATE["_fetch"] = _fetch
        _E2E_STATE["rows_alias"] = rows_alias
        try:
            with TestClient(M.app) as client:
                from src.extraction import linguistics as _L
                t0 = time.time()
                while _L._nlp is None and time.time() - t0 < 180:
                    time.sleep(0.5)
                assert _L._nlp is not None
                _E2E_STATE["client"] = client
                # Seed the #14 take-frame through the REAL harvest → /ingest, exactly as the
                # battery leaves the tenant: (user, take, lisinopril) Class A + the
                # (lisinopril, quantity, '10 milligrams') scalar.
                h = client.post("/harvest-spans", json={"text": TURN, "user_id": user_id})
                assert h.status_code == 200, h.text[:300]
                edges = h.json().get("edges") or []
                assert any(e.get("rel_type") == "quantity" for e in edges), \
                    "fixture precondition: the #14 take-frame must harvest the quantity scalar"
                r = client.post("/ingest", json={
                    "text": TURN, "user_id": user_id, "edges": edges, "source": "mcp"})
                assert r.status_code == 200, r.text[:300]
                yield
        finally:
            if _had_saas:
                os.environ["SAAS_MODE"] = _saas_val

    # ── accessors ──────────────────────────────────────────────────────────────────────────

    def _aliases(self):
        return _E2E_STATE["_fetch"](lambda cur: (
            cur.execute("SELECT alias FROM entity_aliases") or
            {r0 for (r0,) in cur.fetchall()}))

    def _phrase_rows(self):
        return _E2E_STATE["_fetch"](lambda cur: (
            cur.execute(
                "SELECT ea.alias, e.entity_type FROM entity_aliases ea "
                "JOIN entities e ON e.id = ea.entity_id WHERE ea.alias = %s",
                (PHRASE,)) or cur.fetchall()))

    def _facts_rows(self):
        return _E2E_STATE["_fetch"](lambda cur: (
            cur.execute(
                "SELECT COALESCE((SELECT alias FROM entity_aliases ea WHERE ea.entity_id = "
                "f.subject_id ORDER BY ea.is_preferred DESC, ea.id LIMIT 1), f.subject_id::text), "
                "f.rel_type, COALESCE((SELECT alias FROM entity_aliases ea WHERE ea.entity_id = "
                "f.object_id ORDER BY ea.is_preferred DESC, ea.id LIMIT 1), f.object_id::text) "
                "FROM facts f") or cur.fetchall()))

    def _lisinopril_attrs(self):
        return _E2E_STATE["_fetch"](lambda cur: (
            cur.execute(
                "SELECT a.attribute, a.value_text FROM entity_attributes a "
                "JOIN entity_aliases ea ON ea.entity_id = a.entity_id "
                "WHERE ea.alias = 'lisinopril'") or cur.fetchall()))

    def _registry(self):
        import psycopg2
        from src.entity_registry.registry import EntityRegistry
        c = psycopg2.connect(_DSN)
        c.autocommit = True
        return c, EntityRegistry(c, auto_commit=True, schema_name=_E2E_STATE["schema"])

    # ── THE SEAM PINS ──────────────────────────────────────────────────────────────────────

    def test_ingest_phrase_edge_mints_nothing(self):
        """An edge whose object surface is '<substance> <family-noun>' (the payload every live
        mint lane delivers — GLiNER2 relation candidate, LLM relation-fill, correction
        re-extract) mints NO entity and NO alias for the phrase."""
        client = _E2E_STATE["client"]
        user_id = _E2E_STATE["user_id"]
        r = client.post("/ingest", json={
            "text": TURN, "user_id": user_id,
            "edges": [{"subject": "user", "rel_type": "related_to",
                       "object": PHRASE, "object_type": "Concept"}],
            "source": "mcp"})
        assert r.status_code == 200, r.text[:300]
        assert not self._phrase_rows(), \
            "the 'lisinopril dosage' phrase-entity minted at the /ingest object seam"
        # the edge is KEPT, redirected onto the substance (fail-toward-not-losing)
        assert any(s == "user" and o == "lisinopril"
                   for s, _r, o in self._facts_rows()), \
            "the phrase edge must land on the substance referent, not be dropped"

    def test_gliner2_candidate_surface_no_mint(self):
        """The GLiNER2-candidate path (a typed span for the phrase riding an edge) mints
        nothing — the same registry seam, driven with the GLiNER2 payload shape."""
        client = _E2E_STATE["client"]
        user_id = _E2E_STATE["user_id"]
        r = client.post("/ingest", json={
            "text": TURN, "user_id": user_id,
            "edges": [{"subject": PHRASE, "rel_type": "related_to",
                       "object": "high blood pressure", "subject_type": "Concept",
                       "object_type": "Concept"}],
            "source": "mcp"})
        assert r.status_code == 200, r.text[:300]
        assert not self._phrase_rows(), \
            "the phrase-entity minted from a GLiNER2-typed SUBJECT surface"

    def test_substance_keeps_quantity_attr(self):
        """The stored quantity row is untouched by the suppression — the #14/#18 frame stays
        byte-identical."""
        attrs = self._lisinopril_attrs()
        assert any(a == "quantity" and "10" in (v or "")
                   for a, v in attrs), f"quantity attr missing: {attrs}"

    def test_registry_seam_direct_and_underscore_variant(self):
        """EntityRegistry.resolve() is the seam EVERY lane funnels through (the spine's ±6
        backbone attach resolves edge objects there BEFORE /ingest): the phrase — space or
        underscore joined — resolves to the SUBSTANCE's id, and no alias is registered."""
        c, reg = self._registry()
        try:
            lis_id = _E2E_STATE["rows_alias"]("lisinopril")[0][0]
            for surface in (PHRASE, "lisinopril_dosage", "Lisinopril Dosage"):
                got = reg.resolve(_E2E_STATE["user_id"], surface)
                assert got == lis_id, f"{surface!r} did not redirect to the substance: {got}"
            assert PHRASE not in self._aliases()
            assert "lisinopril_dosage" not in self._aliases()
        finally:
            c.close()

    def test_multiword_substance_prefix_redirects(self):
        """A MULTI-token substance prefix ('bench press record' → 'bench press') redirects too —
        the W1 measure_noun family at the registry seam ('record' is a seeded floor member)."""
        c, reg = self._registry()
        try:
            uid = _E2E_STATE["user_id"]
            bench_id = reg.resolve(uid, "bench press")
            got = reg.resolve(uid, "bench press record")
            assert got == bench_id, f"multi-token prefix did not redirect: {got}"
            assert "bench press record" not in self._aliases()
        finally:
            c.close()

    # ── CONTROLS (the frozen bar: genuine compounds unaffected) ───────────────────────────

    def test_control_genuine_compound_propns_still_mint(self):
        """'Unifi Dream Machine' and 'blue-ringed octopus' — trailing token NOT a family
        member — mint exactly as before (the #11 compound-type ruling untouched)."""
        c, reg = self._registry()
        try:
            uid = _E2E_STATE["user_id"]
            udm = reg.resolve(uid, "Unifi Dream Machine")
            bro = reg.resolve(uid, "blue-ringed octopus")
            aliases = self._aliases()
            assert "unifi dream machine" in aliases
            assert "blue-ringed octopus" in aliases
            # and they did NOT weld onto anything — fresh surrogates of their own
            assert udm and bro and udm != bro
        finally:
            c.close()

    def test_control_family_trailing_token_unknown_prefix_still_mints(self):
        """'High Level' (a city; 'level' IS a family member but 'high' is not a known entity)
        mints as today — the prefix-known requirement is the content-loss guard."""
        c, reg = self._registry()
        try:
            reg.resolve(_E2E_STATE["user_id"], "High Level")
            assert "high level" in self._aliases(), \
                "an unknown-prefix family-trailing surface was suppressed (content loss)"
        finally:
            c.close()

    # ── THE QUERY (frozen bar 1: renders the milligrams) ───────────────────────────────────

    def test_dosage_query_renders_the_milligrams(self):
        client = _E2E_STATE["client"]
        resp = client.post("/query", json={
            "text": PHRASE_Q, "user_id": _E2E_STATE["user_id"]})
        assert resp.status_code == 200, resp.text[:300]
        facts = resp.json().get("facts") or []
        blobs = json.dumps(facts)
        assert "10" in blobs and "lisinopril" in blobs.lower(), \
            f"the dosage query did not render the milligrams: {blobs[:400]}"
        assert "dosage" not in {a for a in self._aliases()}, \
            "the query must never anchor a phrase-entity"

    # ── FLAG-OFF (byte-for-byte legacy) ────────────────────────────────────────────────────

    def test_flag_off_mints_as_before(self, monkeypatch):
        """REGISTRY_FAMILY_COMPOUND_REDIRECT off → every surface mints byte-for-byte as the
        pre-fix registry (the rollback lever)."""
        import src.entity_registry.registry as R
        c, reg = self._registry()
        try:
            monkeypatch.setattr(R, "REGISTRY_FAMILY_COMPOUND_REDIRECT", False)
            got = reg.resolve(_E2E_STATE["user_id"], PHRASE)
            assert got and got != _E2E_STATE["rows_alias"]("lisinopril")[0][0]
            assert PHRASE in self._aliases(), \
                "flag OFF must restore the legacy mint byte-for-byte"
        finally:
            c.close()
