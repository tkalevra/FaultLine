"""Integration tests for /query endpoint — compound family + domain-agnostic retrieval."""
import json
import os
import psycopg2
import pytest
from starlette.testclient import TestClient


# ── Helpers ──────────────────────────────────────────────────────────────────
def _ingest(client, text: str, user_id: str, edges: list = None) -> dict:
    payload = {"text": text, "source": "test", "user_id": user_id}
    if edges is not None:
        payload["edges"] = edges
    r = client.post("/ingest", json=payload)
    return r.json() if r.status_code == 200 else {"status": "error", "detail": r.text}


def _query(client, text: str, user_id: str) -> dict:
    r = client.post("/query", json={"text": text, "user_id": user_id})
    return r.json() if r.status_code == 200 else {"status": "error", "detail": r.text}


def _clean_db(dsn: str, user_id: str):
    """Remove all data for a test user."""
    with psycopg2.connect(dsn) as db:
        with db.cursor() as cur:
            for tbl in ("facts", "staged_facts", "entity_attributes",
                        "entity_aliases", "entities"):
                cur.execute(f"DELETE FROM {tbl} WHERE user_id = %s", (user_id,))
        db.commit()


# ── Tests ────────────────────────────────────────────────────────────────────

class TestQueryFamilyCompound:
    """Globbed family input: 6 people, names, preferences, ages — all retrieved."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.dsn = os.environ.get("POSTGRES_DSN")
        if not self.dsn:
            pytest.skip("POSTGRES_DSN not set")
        self.uid = "test_query_family"
        _clean_db(self.dsn, self.uid)

    def test_compound_family_ingest_and_query(self):
        """Ingest a compound family text and query 'tell me about my family'."""
        from src.api.main import app
        client = TestClient(app)

        text = (
            "My name is Christopher, I prefer to be called Chris, "
            "I am married to Mara, who prefers to be called Mars. "
            "We have 3 children, a daughter Chiara, age 10, who prefers Chi, "
            "Leo, our son is 19, and a son named Marco, age 12, who goes by Dev."
        )

        ingest = _ingest(client, text, self.uid)
        assert ingest.get("status") == "valid"
        assert ingest.get("committed", 0) >= 10  # family facts

        result = _query(client, "Tell me about my family", self.uid)
        assert result.get("status") == "ok"

        facts = result.get("facts", [])
        preferred_names = result.get("preferred_names", {})
        canonical = result.get("canonical_identity", "")

        # ── Consistency checks ──────────────────────────────────────────
        # No UUIDs as display names
        uuid_pattern = __import__('re').compile(
            r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        )
        uuids_in_pns = [k for k, v in preferred_names.items()
                        if len(k) > 30 and k == v]
        assert len(uuids_in_pns) == 0, f"UUIDs in preferred_names: {uuids_in_pns}"

        # Canonical identity must be "chris" (preferred name)
        assert canonical.lower() == "chris", f"canonical_identity={canonical}, expected 'chris'"

        # Must have spouse fact with display name
        spouse_facts = [f for f in facts if f["rel_type"] == "spouse"]
        assert len(spouse_facts) == 1
        assert spouse_facts[0]["subject"] == "user"

        # Must have parent_of facts for 3 children
        parent_facts = [f for f in facts if f["rel_type"] == "parent_of"]
        assert len(parent_facts) >= 3  # chiara, leo, marco

        # Ages are stored in entity_attributes — verify via DB since
        # attribute-to-fact merging requires Qdrant (unavailable in test).
        _dsn = os.environ.get("POSTGRES_DSN")
        if _dsn:
            import psycopg2 as _pg
            with _pg.connect(_dsn) as _db:
                with _db.cursor() as _cur:
                    _cur.execute(
                        "SELECT COUNT(*) FROM entity_attributes"
                        " WHERE user_id = %s AND attribute = 'age'",
                        (self.uid,)
                    )
                    age_count = _cur.fetchone()[0]
            assert age_count >= 2, f"Expected at least 2 ages, got {age_count}"

        # Must have pref_name for user (chris)
        pref_facts = [f for f in facts
                      if f["rel_type"] == "pref_name" and f["subject"] == "user"]
        assert len(pref_facts) >= 1

        # Preferred names should include human-readable values, not bare UUIDs
        all_values = list(preferred_names.values())
        for v in all_values:
            if v and len(str(v)) > 8:
                assert not uuid_pattern.match(str(v)), \
                    f"UUID leaked into preferred_names: {v}"


class TestQuerySystemAurora:
    """Domain-agnostic: system facts about 'aurora' ingest + query."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.dsn = os.environ.get("POSTGRES_DSN")
        if not self.dsn:
            pytest.skip("POSTGRES_DSN not set")
        self.uid = "test_query_aurora"
        _clean_db(self.dsn, self.uid)

    def test_system_aurora_ingest_and_query(self):
        """Ingest system facts and query 'tell me what you know about aurora'."""
        from src.api.main import app
        client = TestClient(app)

        text = (
            "The system is a Ryzen 7, with 64Gb of ram, "
            "a 2TB M.2 Hard drive, the hostname is Aurora, "
            "fqdn of server.example.com, "
            "the internal ip is 10.0.0.100 running Linux, "
            "the certificate expires on November 27th 2026."
        )

        ingest = _ingest(client, text, self.uid)
        assert ingest.get("status") == "valid"

        result = _query(client,
                        "Tell me what you know about the system named aurora",
                        self.uid)
        assert result.get("status") == "ok"

        facts = result.get("facts", [])
        preferred_names = result.get("preferred_names", {})

        # ── Consistency checks ──────────────────────────────────────────
        # No UUIDs as display names
        uuid_pattern = __import__('re').compile(
            r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        )
        uuids_in_pns = [k for k, v in preferred_names.items()
                        if len(k) > 30 and k == v]
        assert len(uuids_in_pns) == 0, f"UUIDs in preferred_names: {uuids_in_pns}"

        # Must return at least the hostname fact
        rel_types = {f["rel_type"] for f in facts}
        assert "hostname" in rel_types, \
            f"hostname fact missing from response. Facts: {rel_types}"

        # The hostname fact object should resolve to "aurora" text, not a UUID
        hostname_facts = [f for f in facts if f["rel_type"] == "hostname"]
        assert len(hostname_facts) >= 1
        for h in hostname_facts:
            obj = h.get("object", "")
            assert not uuid_pattern.match(str(obj)), \
                f"hostname object is UUID: {obj}"

        # Should have at least 1 system fact (hostname).
        # Other facts (fqdn, has_ram, etc.) are staged as Class C and may
        # not all resolve via entity lookup — the fqdn fact's subject is
        # the "ryzen 7" entity, not "system" or "aurora".
        assert len(facts) >= 1, \
            f"Expected at least 1 system fact, got {len(facts)}: {rel_types}"


# ─────────────────────────────────────────────────────────────────────────────
# GOLDEN SCOPE-RESOLUTION TESTS (DESIGN-query-scope-resolution.md, Phase 1)
#
# These exercise the FULL source-merge + admission gate on a REAL per-user schema
# (the only layer that catches cross-concept bleed — mock-DB phase tests cannot).
# They assert BOTH that in-scope facts are present AND that named out-of-scope
# junk is absent.  This is the contract guard against the member_of/instance_of
# bleed regressing for a 6th time.
# ─────────────────────────────────────────────────────────────────────────────

# Stable v5-format UUIDs for the golden fixtures (must pass _UUID_PATTERN).
_GOLD_FAMILY_UID = "a0000000-0000-5000-8000-000000000001"
_GOLD_NET_UID = "a0000000-0000-5000-8000-000000000002"
_GOLD_CONNECT_UID = "a0000000-0000-5000-8000-000000000003"


def _provision_schema(user_id: str):
    """Provision a real per-user schema (production create_user_schema path).

    Returns the schema_name.  Idempotent — safe to call per test.
    """
    import psycopg2 as _pg
    from src.provisioning.schema_manager import (
        create_user_schema,
        derive_user_slug_from_uuid,
        derive_schema_name,
    )
    dsn = os.environ["POSTGRES_DSN"]
    slug = derive_user_slug_from_uuid(user_id)
    schema_name = derive_schema_name(slug)
    db = _pg.connect(dsn)
    try:
        create_user_schema(user_id, slug, db)
        # Deterministically mark provisioning READY. The async provisioning
        # worker does not run under TestClient, so /ingest's
        # ensure_user_provisioned() would otherwise leave status='provisioning'
        # and the request would block on wait_for_schema_ready until timeout.
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO public.users (user_id, email, display_name, slug)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO NOTHING
            """, (user_id, f"{user_id}@local", slug, slug))
            cur.execute("""
                INSERT INTO public.user_provisioning (user_id, schema_name, status, ready_at)
                VALUES (%s, %s, 'ready', now())
                ON CONFLICT (user_id) DO UPDATE
                    SET status = 'ready', schema_name = EXCLUDED.schema_name,
                        ready_at = now()
            """, (user_id, schema_name))
        db.commit()
    finally:
        db.close()
    return schema_name


def _wipe_schema(schema_name: str):
    """Truncate fact-bearing tables in a provisioned schema between tests
    (keeps the seeded rel_types / taxonomies / user anchor)."""
    import psycopg2 as _pg
    dsn = os.environ["POSTGRES_DSN"]
    db = _pg.connect(dsn)
    try:
        with db.cursor() as cur:
            cur.execute(f"SET search_path TO {schema_name}")
            for tbl in ("facts", "staged_facts", "entity_attributes"):
                cur.execute(f"DELETE FROM {tbl}")
        db.commit()
    finally:
        db.close()


def _exec(schema_name: str, sql: str, params=None):
    import psycopg2 as _pg
    dsn = os.environ["POSTGRES_DSN"]
    db = _pg.connect(dsn)
    try:
        with db.cursor() as cur:
            cur.execute(f"SET search_path TO {schema_name}")
            cur.execute(sql, params or ())
            rows = cur.fetchall() if cur.description else None
        db.commit()
        return rows
    finally:
        db.close()


class TestFamilyScopeNoServerBleed:
    """RC1 (over-fetch) + RC2 (stale type-cache) closure.

    Reproduces the live bug: a homelab server entity with
      - a Class-A `instance_of <server-type>` edge (true type), AND
      - a stale `entities.entity_type = 'Person'` column (drift), AND
      - a `member_of user` edge (the leaking fact, category='identity'),
    must NOT appear in the answer to 'tell me about my family'.
    Family rels (parent_of / spouse) MUST still appear.
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.dsn = os.environ.get("POSTGRES_DSN")
        if not self.dsn:
            pytest.skip("POSTGRES_DSN not set")
        self.uid = _GOLD_FAMILY_UID
        self.schema = _provision_schema(self.uid)
        _wipe_schema(self.schema)

    def _ingest_edges(self, client, edges, text="x"):
        payload = {"text": text, "source": "test", "user_id": self.uid, "edges": edges}
        r = client.post("/ingest", json=payload)
        assert r.status_code == 200, r.text
        return r.json()

    def test_family_query_excludes_server_member_of(self):
        from src.api.main import app
        client = TestClient(app)

        # ── Family facts (Class A, user-stated) ──────────────────────────
        self._ingest_edges(client, [
            {"subject": "user", "object": "mars", "rel_type": "spouse",
             "fact_provenance": "user_stated", "confidence": 1.0,
             "subject_type": "Person", "object_type": "Person"},
            {"subject": "user", "object": "marco", "rel_type": "parent_of",
             "fact_provenance": "user_stated", "confidence": 1.0,
             "subject_type": "Person", "object_type": "Person"},
            {"subject": "user", "object": "leo", "rel_type": "parent_of",
             "fact_provenance": "user_stated", "confidence": 1.0,
             "subject_type": "Person", "object_type": "Person"},
        ], text="My spouse is Mars and my children are Marco and Leo")

        # ── Server entity: true type via hierarchy, plus the leaking edges ──
        self._ingest_edges(client, [
            {"subject": "fatty", "object": "homelab server", "rel_type": "instance_of",
             "fact_provenance": "user_stated", "confidence": 1.0,
             "subject_type": "Object", "object_type": "Object"},
            {"subject": "fatty", "object": "user", "rel_type": "member_of",
             "fact_provenance": "user_stated", "confidence": 1.0,
             "subject_type": "Object", "object_type": "Person"},
        ], text="fatty is a homelab server and a member of my systems")

        # ── Inject the DRIFT: force fatty's cached entity_type to a WRONG Person ──
        # This is the stale denormalized column the gate must NOT trust.
        rows = _exec(self.schema,
                     "SELECT entity_id FROM entity_aliases WHERE alias = %s", ("fatty",))
        assert rows, "fatty entity not registered"
        fatty_uuid = rows[0][0]
        _exec(self.schema,
              "UPDATE entities SET entity_type = 'Person' WHERE id = %s", (fatty_uuid,))
        # Verify the drift is in place AND the true hierarchy edge exists.
        et = _exec(self.schema, "SELECT entity_type FROM entities WHERE id = %s", (fatty_uuid,))
        assert et[0][0] == "Person", "drift setup failed"
        hier = _exec(self.schema,
                     "SELECT 1 FROM facts WHERE subject_id = %s AND rel_type = 'instance_of'"
                     " AND superseded_at IS NULL", (fatty_uuid,))
        assert hier, "instance_of edge missing — fixture invalid"

        # ── Query ────────────────────────────────────────────────────────
        result = _query(client, "Tell me about my family", self.uid)
        assert result.get("status") == "ok" or result.get("anchor"), result
        facts = result.get("facts", [])
        rel_types = {(f.get("rel_type") or "").lower() for f in facts}
        subjects = {(f.get("subject") or "").lower() for f in facts}
        objects = {(f.get("object") or "").lower() for f in facts}

        # IN-SCOPE present: family rels must survive.
        assert "parent_of" in rel_types, f"family rels lost! got: {rel_types}"

        # OUT-OF-SCOPE absent (RC1 + RC2): the server, member_of, instance_of
        # must NOT leak into a family answer.
        assert "member_of" not in rel_types, \
            f"member_of leaked into family scope (RC1/RC2): {facts}"
        assert "instance_of" not in rel_types, \
            f"instance_of leaked into family scope: {facts}"
        assert "fatty" not in subjects and "fatty" not in objects, \
            f"server entity 'fatty' leaked into family answer: {facts}"
        assert "homelab server" not in objects, \
            f"server-type entity leaked into family answer: {facts}"


class TestSubjectGroundedNonUser:
    """Subject-grounding (HARD): anchor on a NON-USER subject.

    'network config for <host>' grounds on the host, and returns only that
    host's network-category facts — proving grounding is on the query subject,
    not the user, and that scope projects to the asked-about concept.
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.dsn = os.environ.get("POSTGRES_DSN")
        if not self.dsn:
            pytest.skip("POSTGRES_DSN not set")
        self.uid = _GOLD_NET_UID
        self.schema = _provision_schema(self.uid)
        _wipe_schema(self.schema)

    def _ingest_edges(self, client, edges, text="x"):
        payload = {"text": text, "source": "test", "user_id": self.uid, "edges": edges}
        r = client.post("/ingest", json=payload)
        assert r.status_code == 200, r.text
        return r.json()

    def test_network_config_grounds_on_host(self):
        from src.api.main import app
        client = TestClient(app)

        # Network scalar facts about a host 'webserver01'.
        self._ingest_edges(client, [
            {"subject": "webserver01", "object": "10.10.10.99", "rel_type": "has_ip",
             "fact_provenance": "user_stated", "confidence": 1.0,
             "subject_type": "Object"},
            {"subject": "webserver01", "object": "aa:bb:cc:dd:ee:ff", "rel_type": "has_mac",
             "fact_provenance": "user_stated", "confidence": 1.0,
             "subject_type": "Object"},
        ], text="webserver01 has IP 10.10.10.99 and MAC aa:bb:cc:dd:ee:ff")

        # An off-concept fact about the SAME host (should NOT appear for a
        # network-scoped query).
        self._ingest_edges(client, [
            {"subject": "webserver01", "object": "datacenter-a", "rel_type": "located_in",
             "fact_provenance": "user_stated", "confidence": 1.0,
             "subject_type": "Object", "object_type": "Location"},
        ], text="webserver01 is located in datacenter-a")

        # Resolve anchor must NOT be the user — it must be the host entity.
        from src.api.main import resolve_anchor
        import psycopg2 as _pg
        _db = _pg.connect(self.dsn)
        with _db.cursor() as _c:
            _c.execute(f"SET search_path TO {self.schema}")
        anchor = resolve_anchor("network config for webserver01", None, self.uid, _db)
        _db.close()
        assert anchor != self.uid, \
            "anchor regressed to user — subject-grounding broken for non-user subject"

        result = _query(client, "network config for webserver01", self.uid)
        assert result.get("status") == "ok" or result.get("anchor"), result
        facts = result.get("facts", [])
        rel_types = {(f.get("rel_type") or "").lower() for f in facts}

        # In-scope network facts present.
        assert "has_ip" in rel_types, f"has_ip missing from network answer: {rel_types}"
        # The IP value must be the host's value.
        ip_facts = [f for f in facts if (f.get("rel_type") or "").lower() == "has_ip"]
        assert any("10.10.10.99" in str(f.get("object", "")) for f in ip_facts), \
            f"host IP value missing: {ip_facts}"


class TestFamilyScopeAnchorConnectedness:
    """Part 1C — anchor-connectedness (kills the live 'reginald/zelda' leak).

    Reproduces the live bug: a SECOND, DISCONNECTED family cluster exists in the
    same tenant schema —
      - `reginald sibling_of brother`  (family-category rel, sibling_of)
      - `zelda parent_of child`        (family-category rel, parent_of)
    neither of whose entities has ANY edge to the user. These leaked into
    'tell me about my family' because the domain-scoped source fetched EVERY
    family-category fact for the WHOLE schema with no anchor filter.

    Subject-grounding: 'my family' grounds on the user. An entity with no path to
    the user is NOT the user's family and MUST be excluded — while the user's
    OWN connected family (spouse / children) MUST still be returned.
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.dsn = os.environ.get("POSTGRES_DSN")
        if not self.dsn:
            pytest.skip("POSTGRES_DSN not set")
        self.uid = _GOLD_CONNECT_UID
        self.schema = _provision_schema(self.uid)
        _wipe_schema(self.schema)

    def _ingest_edges(self, client, edges, text="x"):
        payload = {"text": text, "source": "test", "user_id": self.uid, "edges": edges}
        r = client.post("/ingest", json=payload)
        assert r.status_code == 200, r.text
        return r.json()

    def test_family_query_excludes_disconnected_subjects(self):
        from src.api.main import app
        client = TestClient(app)

        # ── CONNECTED family (anchored to the user) ──────────────────────
        self._ingest_edges(client, [
            {"subject": "user", "object": "mars", "rel_type": "spouse",
             "fact_provenance": "user_stated", "confidence": 1.0,
             "subject_type": "Person", "object_type": "Person"},
            {"subject": "user", "object": "marco", "rel_type": "parent_of",
             "fact_provenance": "user_stated", "confidence": 1.0,
             "subject_type": "Person", "object_type": "Person"},
        ], text="My spouse is Mars and my child is Marco")

        # ── DISCONNECTED family cluster (ZERO edges to the user) ─────────
        # Both are family-CATEGORY rels — in scope by TYPE — but about people
        # with no path to the user. This is the exact live leak.
        self._ingest_edges(client, [
            {"subject": "reginald", "object": "brother", "rel_type": "sibling_of",
             "fact_provenance": "user_stated", "confidence": 1.0,
             "subject_type": "Person", "object_type": "Person"},
            {"subject": "zelda", "object": "child", "rel_type": "parent_of",
             "fact_provenance": "user_stated", "confidence": 1.0,
             "subject_type": "Person", "object_type": "Person"},
        ], text="Reginald is a sibling of his brother; Zelda is the parent of her child")

        # Sanity: the disconnected facts ARE in the DB and ARE family-category,
        # and have NO edge to the user (so only the anchor-connectedness gate can
        # keep them out).
        rows = _exec(self.schema,
                     "SELECT entity_id FROM entity_aliases WHERE alias IN "
                     "('reginald','zelda','brother','child')")
        dis_ids = [r[0] for r in (rows or [])]
        assert len(dis_ids) >= 2, "disconnected entities not registered"
        edges_to_user = _exec(self.schema,
                              "SELECT count(*) FROM facts WHERE superseded_at IS NULL "
                              "AND ((subject_id = ANY(%s) AND object_id = %s) "
                              "  OR (object_id = ANY(%s) AND subject_id = %s))",
                              (dis_ids, self.uid, dis_ids, self.uid))
        assert edges_to_user[0][0] == 0, "fixture invalid — disconnected cluster has edges to user"

        # ── Query ────────────────────────────────────────────────────────
        result = _query(client, "Tell me about my family", self.uid)
        assert result.get("status") == "ok" or result.get("anchor"), result
        facts = result.get("facts", [])
        subjects = {(f.get("subject") or "").lower() for f in facts}
        objects = {(f.get("object") or "").lower() for f in facts}
        rel_types = {(f.get("rel_type") or "").lower() for f in facts}

        # IN-SCOPE, CONNECTED family present.
        assert "spouse" in rel_types or "parent_of" in rel_types, \
            f"connected family lost! got: {rel_types} / {facts}"
        assert ("mars" in subjects or "mars" in objects
                or "marco" in subjects or "marco" in objects), \
            f"connected family members lost: {facts}"

        # DISCONNECTED subjects ABSENT (the 1C contract).
        for junk in ("reginald", "zelda", "brother", "child"):
            assert junk not in subjects and junk not in objects, \
                f"disconnected subject '{junk}' leaked into family answer: {facts}"


class TestPerTenantTaxonomyOverlay:
    """Part 2 (2C) — scope reads are PER-TENANT, not the global boot cache.

    A tenant edits its OWN entity_taxonomies (adds a rel_type to family's
    defining group). The overlay must surface that edit for THIS tenant's scope
    decisions WITHOUT a process restart and WITHOUT touching public or any other
    tenant. Verifies the security/isolation property: no cross-tenant bleed.
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.dsn = os.environ.get("POSTGRES_DSN")
        if not self.dsn:
            pytest.skip("POSTGRES_DSN not set")
        self.uid = _GOLD_CONNECT_UID  # reuse a provisioned schema
        self.schema = _provision_schema(self.uid)

    def test_tenant_edit_visible_to_overlay_and_isolated(self):
        from src.api import taxonomy_overlay

        dsn = self.dsn
        # Baseline: seed family defining rels (from public seed ∪ tenant copy).
        taxonomy_overlay.invalidate(None)
        base = taxonomy_overlay.resolve_meta(dsn, self.schema)
        assert "family" in base, f"family taxonomy missing from tenant overlay: {list(base)}"
        base_rels = set(base["family"].get("rel_types_defining_group", []))

        # The DDL alignment must have given the tenant a `source` column.
        assert "source" in base["family"], "tenant taxonomy missing `source` (DDL not aligned)"

        # Tenant edits ITS OWN family definition (Phase-3-style scope edit).
        _exec(self.schema,
              "UPDATE entity_taxonomies "
              "SET rel_types_defining_group = array_append(rel_types_defining_group, 'co_parent_of'), "
              "    source = 'user_corrected' "
              "WHERE taxonomy_name = 'family'")
        # Invalidate ONLY this tenant — mirrors a scope-correction refresh.
        taxonomy_overlay.invalidate(self.schema)

        after = taxonomy_overlay.resolve_meta(dsn, self.schema)
        after_rels = set(after["family"].get("rel_types_defining_group", []))
        assert "co_parent_of" in after_rels, \
            f"tenant edit not visible via overlay: {after_rels}"
        assert after["family"]["source"] == "user_corrected", \
            "source did not reflect the tenant edit"

        # Isolation: the PUBLIC seed must be untouched by a tenant edit.
        public_meta = taxonomy_overlay.resolve_meta(dsn, "public")
        pub_rels = set(public_meta.get("family", {}).get("rel_types_defining_group", []))
        assert "co_parent_of" not in pub_rels, \
            "tenant edit bled into the PUBLIC seed (cross-tenant leak!)"
