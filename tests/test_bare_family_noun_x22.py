"""BARE FAMILY-NOUN MINT (issue #22) — the registry/mint seam pins.

THE WOUND (deploy #12 smoke, live rows verified): #21's redirect killed the
'<substance> dosage' phrase-entity, but the dosage query then rendered
'Lisinopril is related to Quantity / Quantity's Quantity is 10 milligrams' — a
BARE 'quantity' entity (alias pref=false) minted from the family-noun CANONICAL
itself. The #21 gate needs a known-entity PREFIX; a bare single-token surface has
an empty prefix, so the redirect declined and the registry minted.

MEASURED MINT WINDOWS (probe, fresh tenants): (a) the HARVEST-time backbone
attach — the canonical's own rel_type only registers at /ingest, so a first-turn
harvested edge carrying the bare noun resolves BEFORE any rel_type-name guard
exists; (b) /ingest when the bare-noun edge precedes the rel-minting edge in the
batch. Once the rel IS registered, /ingest's rel_type_name_collision pre-check and
the registry's own rel-type HARD CONSTRAINT already refuse the surface.

THE FIX: ``EntityRegistry._measurement_family_bare_surface_refused`` at the SAME
mint seam — and (issue #23) DEFAULT-DENY: a single-token surface that IS a cue-rail
family member raises ``MeasurementFamilySurfaceError`` (a ValueError subclass) at
the surrogate-registration block for EVERY caller that does not declare
``name_intent=True`` — /ingest's existing ValueError handler drops the edge LOUDLY
(recorded, turn retained in episodic_log); growth lanes skip the rung via their
existing except-Exception. The #22 opt-in (edge kwargs at 5 adjudicated sites)
missed the LIVE sixth mint path (deploy-13: the harvest's pending-scalar-grounding
concept queue minted the alias BEFORE /ingest ran; the alias probe's early return
then bypassed every guarded site) — the guard now holds by default and the AST
census (tests/test_mint_guard_default_deny_x23.py) classifies every resolve() site.

THE GUARD (frozen bar 2 — genuine names still mint): full-surface-registered-first
(the alias probe returns EARLY before the refusal); naming rels are SCALAR-tailed
(pref_name/also_known_as — migration 039's own class) so a naming edge's object is
string-kept at /ingest and never reaches the flagged sites; the genitive-name frame
binds the NAME as a RELATIONAL edge's subject, which passes no scalar gate.

Deterministic per PD3 (the x21 fixture pattern): the fixture owns its env (spine
route pinned, SaaS popped for the open-core seam, the #14 take-frame seeded through
the REAL harvest → /ingest). Requires POSTGRES_DSN pointing ONLY at a throwaway
test DB.
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
BARE = "quantity"
DOSAGE_Q = "What is my lisinopril dosage?"
MANGLED = [
    # the deploy-#12 wound rows as edges: the relational island-carrier…
    {"subject": "lisinopril", "rel_type": "related_to",
     "object": BARE, "object_type": "Concept"},
    # …and the mangled scalar (subject = the bare canonical itself)
    {"subject": BARE, "rel_type": BARE,
     "object": "10 milligrams", "object_datatype": "text"},
]

_DSN = os.environ.get("POSTGRES_DSN", "")
_E2E = bool(_DSN) and "test" in _DSN

_E2E_STATE: dict = {}


@pytest.mark.skipif(not _E2E, reason="POSTGRES_DSN not pointed at a throwaway test DB")
class TestBareFamilyNounMint:
    @pytest.fixture(autouse=True, scope="class")
    def _driver(self, request):
        import psycopg2
        import src.api.main as M
        from starlette.testclient import TestClient
        from src.provisioning.schema_manager import (
            create_user_schema, derive_user_slug_from_uuid)

        # conftest's baseline turns SAAS_MODE ON for the whole session; under SaaS the
        # harvest is FAIL-CLOSED without a bound tenant brain (BYO-LLM) — the dz14/df18/
        # W1/x21 precedent: this lane pins the OPEN-CORE engine seam, so SaaS is off for
        # this class's scope only (manual save/restore — the fixture is CLASS-scoped).
        _had_saas = "SAAS_MODE" in os.environ
        _saas_val = os.environ.pop("SAAS_MODE", None)

        pytest.main_mod = M
        M._INGEST_ENABLED = True
        # The x21 precedent: pin the DETERMINISTIC decomposer — the mint seam under test
        # is downstream of segmentation.
        M.SPINE_DETERMINISTIC_SEGMENTATION = True

        class _D:
            def __init__(self): self.d = {}
            def get(self, k): return self.d.get(k)
            def setex(self, k, t, v): self.d[k] = v; return True
            def ping(self): return True

        class _Idem:
            redis_url = "dict://test"; ttl = 3600; client = _D()
        M._idempotency_mgr = _Idem()

        def _provision():
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
                    "INSERT INTO public.user_provisioning (user_id, schema_name, status, "
                    "ready_at) VALUES (%s, %s, 'ready', now()) ON CONFLICT DO NOTHING",
                    (user_id, schema))
            conn.commit()
            conn.close()
            return user_id, schema

        # ONE fresh seat per PHASE (the mangled-edge window is pre-rel-registration, so
        # the wound seats must NOT carry the seeded take-frame).
        _E2E_STATE["wound_seat"] = _provision()      # mangled edges FIRST (window b)
        _E2E_STATE["battery_seat"] = _provision()    # take-frame, then mangled, then query
        _E2E_STATE["name_seat"] = _provision()       # bar-2 genuine-name frames
        _E2E_STATE["ctx_seat"] = _provision()        # registry-context contract probes

        _SLUG_RE = re.compile(r"^[a-z0-9_]+$")

        def _fetch(schema, exec_fn):
            import psycopg2
            c = psycopg2.connect(_DSN, options=f"-csearch_path={schema}")
            try:
                with c.cursor() as cur:
                    return exec_fn(cur)
            finally:
                c.close()

        def _aliases_of(schema):
            return _fetch(schema, lambda cur: (
                cur.execute("SELECT alias FROM entity_aliases") or
                {r0 for (r0,) in cur.fetchall()}))

        _E2E_STATE["_fetch"] = _fetch
        _E2E_STATE["_aliases_of"] = _aliases_of
        try:
            with TestClient(M.app) as client:
                from src.extraction import linguistics as _L
                t0 = time.time()
                while _L._nlp is None and time.time() - t0 < 180:
                    time.sleep(0.5)
                assert _L._nlp is not None
                _E2E_STATE["client"] = client

                # Seed the battery seat's #14 take-frame through the REAL harvest →
                # /ingest (exactly as the battery leaves the tenant).
                bu, bs = _E2E_STATE["battery_seat"]
                h = client.post("/harvest-spans", json={"text": TURN, "user_id": bu})
                assert h.status_code == 200, h.text[:300]
                edges = h.json().get("edges") or []
                assert any(e.get("rel_type") == BARE for e in edges), \
                    "fixture precondition: the #14 take-frame must harvest the quantity scalar"
                r = client.post("/ingest", json={
                    "text": TURN, "user_id": bu, "edges": edges, "source": "mcp"})
                assert r.status_code == 200, r.text[:300]
                yield
        finally:
            if _had_saas:
                os.environ["SAAS_MODE"] = _saas_val

    # ── accessors ──────────────────────────────────────────────────────────────────────────

    def _facts_rows(self, schema):
        return self._fetch(schema, lambda cur: (
            cur.execute(
                "SELECT COALESCE((SELECT alias FROM entity_aliases ea WHERE ea.entity_id = "
                "f.subject_id ORDER BY ea.is_preferred DESC, ea.id LIMIT 1), f.subject_id::text), "
                "f.rel_type, COALESCE((SELECT alias FROM entity_aliases ea WHERE ea.entity_id = "
                "f.object_id ORDER BY ea.is_preferred DESC, ea.id LIMIT 1), f.object_id::text) "
                "FROM facts f") or cur.fetchall()))

    def _attrs(self, schema):
        return self._fetch(schema, lambda cur: (
            cur.execute(
                "SELECT COALESCE((SELECT alias FROM entity_aliases ea WHERE ea.entity_id = "
                "a.entity_id ORDER BY ea.is_preferred DESC, ea.id LIMIT 1), a.entity_id::text), "
                "a.attribute, a.value_text FROM entity_attributes a") or cur.fetchall()))

    @property
    def _fetch(self):
        return _E2E_STATE["_fetch"]

    @property
    def _aliases(self):
        return _E2E_STATE["_aliases_of"]

    # ── THE WOUND (frozen bar 1: no bare entity, no junk rows, loud drop) ─────────────────

    def test_bare_noun_edges_mint_nothing_and_drop_loudly(self):
        """The mangled edges on a FRESH seat (the pre-rel-registration window): NO bare
        'quantity' entity/alias, NO (X, related_to, quantity) row, and BOTH edges dropped
        LOUDLY with the named reason (stage + the issue-#22 message)."""
        client = _E2E_STATE["client"]
        wu, ws = _E2E_STATE["wound_seat"]
        r = client.post("/ingest", json={
            "text": TURN, "user_id": wu, "edges": MANGLED, "source": "mcp"})
        assert r.status_code == 200, r.text[:300]
        j = r.json()
        dropped = j.get("dropped") or []
        stages = {d.get("stage") for d in dropped}
        assert "object_entity_resolution" in stages and "subject_entity_resolution" in stages, \
            f"the mangled edges must drop at the two resolution stages: {stages}"
        assert any("#22" in str(d.get("reason", "")) or "#23" in str(d.get("reason", ""))
                   for d in dropped), \
            f"the drop reason must NAME the refusal: {dropped}"
        assert BARE not in self._aliases(ws), "the bare 'quantity' entity minted"
        assert not any(o == BARE for _s, _r, o in self._facts_rows(ws)), \
            "a facts row carries the bare canonical as an object"

    def test_dosage_query_renders_clean(self):
        """Frozen bar 1 (the battery shape): after the take-frame + the mangled edges, the
        dosage query renders (lisinopril, quantity, '10 milligrams') and NO bare Quantity
        entity render ('related to Quantity' / 'Quantity's Quantity')."""
        client = _E2E_STATE["client"]
        bu, bs = _E2E_STATE["battery_seat"]
        r = client.post("/ingest", json={
            "text": TURN, "user_id": bu, "edges": MANGLED, "source": "mcp"})
        assert r.status_code == 200, r.text[:300]
        assert BARE not in self._aliases(bs), "the bare 'quantity' entity minted (battery seat)"
        q = client.post("/query", json={"text": DOSAGE_Q, "user_id": bu})
        assert q.status_code == 200, q.text[:300]
        blob = json.dumps(q.json().get("facts") or [])
        assert "10" in blob and "lisinopril" in blob.lower(), \
            f"the dosage query did not render the milligrams: {blob[:300]}"
        low = blob.lower()
        assert "related to quantity" not in low and "quantity's" not in low, \
            f"the bare Quantity entity render survived: {blob[:300]}"

    def test_substance_keeps_quantity_attr(self):
        """The stored quantity row is untouched by the suppression — the #14/#18 frame stays
        byte-identical."""
        _bu, bs = _E2E_STATE["battery_seat"]
        attrs = self._attrs(bs)
        assert any(x == "lisinopril" and a == BARE and "10" in (v or "")
                   for x, a, v in attrs), f"quantity attr missing: {attrs}"

    # ── THE GUARD (frozen bar 2: genuine names still mint) ────────────────────────────────

    def test_naming_edge_still_mints_the_family_word_name(self):
        """'My cat is named Quantity.' — the naming edge as the LANE delivers it
        ((cat, pref_name, 'quantity'); pref_name is SCALAR-tailed so the object is
        string-kept and files through the alias lane) still mints the name."""
        client = _E2E_STATE["client"]
        nu, ns = _E2E_STATE["name_seat"]
        r = client.post("/ingest", json={
            "text": "My cat is named Quantity.", "user_id": nu,
            "edges": [
                {"subject": "user", "rel_type": "owns", "object": "cat",
                 "object_type": "ANIMAL"},
                {"subject": "cat", "rel_type": "pref_name", "object": "Quantity",
                 "object_datatype": "string"},
            ], "source": "mcp"})
        assert r.status_code == 200, r.text[:300]
        dropped = r.json().get("dropped") or []
        assert not any(d.get("object") == BARE or d.get("subject") == BARE
                       for d in dropped), f"a naming edge was refused: {dropped}"
        assert BARE in self._aliases(ns), \
            "the genuine family-word NAME no longer mints (frozen bar 2 violated)"

    def test_genitive_name_genuine_frame_still_mints(self):
        """'My cat's name is Level.' — the genitive-name frame binds the NAME as the
        subject of its edge (a relational/naming lane, never the scalar-subject gate);
        'level' IS a measure_noun family member and must still mint."""
        client = _E2E_STATE["client"]
        nu, ns = _E2E_STATE["name_seat"]
        h = client.post("/harvest-spans", json={
            "text": "My cat's name is Level.", "user_id": nu})
        assert h.status_code == 200, h.text[:300]
        edges = h.json().get("edges") or []
        r = client.post("/ingest", json={
            "text": "My cat's name is Level.", "user_id": nu, "edges": edges,
            "source": "mcp"})
        assert r.status_code == 200, r.text[:300]
        assert "level" in self._aliases(ns), \
            "the genitive-name genuine frame ('my cat's name is Level') no longer mints"

    # ── THE REGISTRY CONTRACT (both directions of the edge-context thread) ────────────────

    def test_registry_seam_refusal_and_context_freedom(self):
        """EntityRegistry.resolve() is the ONE owner — DEFAULT-DENY (issue #23): a bare
        family canonical REFUSES the mint for every caller with no opt-out; a declared
        NAMING FRAME (name_intent=True) mints; the refusal is a ValueError (the /ingest
        drop contract); a NON-family single token mints untouched (the family membership
        alone drives the refusal, never a blanket single-token gate)."""
        import psycopg2
        from src.entity_registry.registry import (
            EntityRegistry, MeasurementFamilySurfaceError)
        cu, cs = _E2E_STATE["ctx_seat"]
        c = psycopg2.connect(_DSN)
        c.autocommit = True
        reg = EntityRegistry(c, auto_commit=True, schema_name=cs)
        try:
            # DEFAULT-DENY: a context-free resolve (the sixth-site class) now REFUSES —
            # the #22 contract ("no context → mint") is deliberately INVERTED by #23.
            with pytest.raises(MeasurementFamilySurfaceError):
                reg.resolve(cu, BARE)
            with pytest.raises(ValueError):  # the /ingest contract: a ValueError subclass
                reg.resolve(cu, "dosage")
            # the naming opt-out: a declared naming frame mints (frozen bar 2's carrier)
            got_name = reg.resolve(cu, "level", name_intent=True)
            assert got_name
            # a NON-family single token with NO opt-out mints — the family membership
            # alone never refuses (the flag alone is not a gate)
            got_aspirin = reg.resolve(cu, "aspirin")
            assert got_aspirin
        finally:
            c.close()

    def test_full_surface_registered_first_precedence(self):
        """Once the genuine name IS registered (the name seat minted 'quantity' for the
        cat), the alias probe returns EARLY — the refusal can never un-register or shadow
        an existing name, even from a default-deny caller (no opt-out needed once
        registered)."""
        import psycopg2
        from src.entity_registry.registry import EntityRegistry
        nu, ns = _E2E_STATE["name_seat"]
        c = psycopg2.connect(_DSN)
        c.autocommit = True
        reg = EntityRegistry(c, auto_commit=True, schema_name=ns)
        try:
            # the registered 'quantity' alias (the cat's NAME) resolves EARLY even under
            # the #23 default-deny (no name_intent) — and resolves to the SAME single
            # entity, no shadow surrogate minted beside it.
            q_rows = self._fetch(ns, lambda cur: (
                cur.execute("SELECT entity_id FROM entity_aliases WHERE alias = %s",
                            (BARE,)) or cur.fetchall()))
            assert len(q_rows) == 1, \
                f"a shadow surrogate was minted beside the registered name: {q_rows}"
            got = reg.resolve(nu, BARE)
            assert got == q_rows[0][0]
        finally:
            c.close()

    # ── CONTROLS ──────────────────────────────────────────────────────────────────────────

    def test_control_genuine_compound_propns_still_mint(self):
        """'Unifi Dream Machine' / 'High Level' — the #21 controls, unchanged by the bare
        extension (multi-token surfaces never reach the bare refusal)."""
        import psycopg2
        from src.entity_registry.registry import EntityRegistry
        cu, cs = _E2E_STATE["ctx_seat"]
        c = psycopg2.connect(_DSN)
        c.autocommit = True
        reg = EntityRegistry(c, auto_commit=True, schema_name=cs)
        try:
            reg.resolve(cu, "Unifi Dream Machine")
            reg.resolve(cu, "High Level")
            aliases = self._aliases(cs)
            assert "unifi dream machine" in aliases
            assert "high level" in aliases
        finally:
            c.close()

    def test_flag_off_mints_as_before(self, monkeypatch):
        """REGISTRY_FAMILY_COMPOUND_REDIRECT off → the bare refusal never runs and the
        surface mints byte-for-byte as the pre-fix registry (the ONE rollback lever for
        the whole #21+#22 mint-suppression seam)."""
        import psycopg2
        import src.entity_registry.registry as R
        from src.entity_registry.registry import EntityRegistry
        cu, cs = _E2E_STATE["ctx_seat"]
        c = psycopg2.connect(_DSN)
        c.autocommit = True
        reg = EntityRegistry(c, auto_commit=True, schema_name=cs)
        try:
            monkeypatch.setattr(R, "REGISTRY_FAMILY_COMPOUND_REDIRECT", False)
            got = reg.resolve(cu, "dosage")
            assert got, "flag OFF must restore the legacy mint byte-for-byte"
            assert "dosage" in self._aliases(cs)
        finally:
            c.close()
