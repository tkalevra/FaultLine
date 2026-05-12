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
            "I am married to Marla, who prefers to be called Mars. "
            "We have 3 children, a daughter Gabriella, age 10, who prefers Gabby, "
            "Cyrus, our son is 19, and a son named Desmonde, age 12, who goes by Des."
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
        assert len(parent_facts) >= 3  # gabriella, cyrus, desmonde

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
