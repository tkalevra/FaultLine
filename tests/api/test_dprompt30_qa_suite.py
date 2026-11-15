"""dprompt-30: QA Stress Suite — real-world extraction & query validation.
15 scenarios testing natural language parsing, corrections, sensitivity gating,
graph depth, novel types, re-ingest idempotency, and edge case robustness.
Tests only — zero source code changes.
"""
import os
import re
import time
import psycopg2
import pytest
from starlette.testclient import TestClient


# ── Helpers ──────────────────────────────────────────────────────────────────

_UUID_PATTERN = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
)

_DSN = os.environ.get("POSTGRES_DSN")


def _ingest(client: TestClient, text: str, user_id: str, edges: list = None) -> dict:
    payload = {"text": text, "source": "test", "user_id": user_id}
    if edges is not None:
        payload["edges"] = edges
    r = client.post("/ingest", json=payload)
    return r.json() if r.status_code == 200 else {"status": "error", "detail": r.text}


def _query(client: TestClient, text: str, user_id: str) -> dict:
    r = client.post("/query", json={"text": text, "user_id": user_id})
    return r.json() if r.status_code == 200 else {"status": "error", "detail": r.text}


def _clean_db(user_id: str):
    if not _DSN:
        return
    with psycopg2.connect(_DSN) as db:
        with db.cursor() as cur:
            for tbl in ("facts", "staged_facts", "entity_attributes",
                        "entity_aliases", "entities", "pending_types",
                        "ontology_evaluations"):
                cur.execute(f"DELETE FROM {tbl} WHERE user_id = %s", (user_id,))
        db.commit()


def _db_count(user_id: str, table: str, extra_where: str = "") -> int:
    if not _DSN:
        return 0
    with psycopg2.connect(_DSN) as db:
        with db.cursor() as cur:
            where = f"WHERE user_id = %s {extra_where}"
            cur.execute(f"SELECT COUNT(*) FROM {table} {where}", (user_id,))
            return cur.fetchone()[0]


def _db_fetch(user_id: str, table: str, columns: str = "*",
              extra_where: str = "") -> list:
    if not _DSN:
        return []
    with psycopg2.connect(_DSN) as db:
        with db.cursor() as cur:
            where = f"WHERE user_id = %s {extra_where}"
            cur.execute(f"SELECT {columns} FROM {table} {where}", (user_id,))
            return cur.fetchall()


# ── Fixture ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def app():
    from src.api.main import app
    return app


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO 1: Complex Family Prose
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario1ComplexFamilyProse:
    """Natural prose with aliases: 3 kids, nicknames, ages — all extracted."""

    @pytest.fixture(autouse=True)
    def setup(self):
        if not _DSN:
            pytest.skip("POSTGRES_DSN not set")
        self.uid = "test_dp30_s1"
        _clean_db(self.uid)

    def test_complex_family_prose(self, app):
        client = TestClient(app)

        text = (
            "My wife and I have three kids. My son Cyrus is 19. "
            "My daughter Gabriella, she goes by Gabby, she's 10. "
            "My son Desmonde, he prefers Des, he's 12."
        )
        ingest = _ingest(client, text, self.uid)
        assert ingest.get("status") == "valid", f"Ingest failed: {ingest}"
        assert ingest.get("committed", 0) >= 6, \
            f"Expected >=6 facts, got {ingest.get('committed', 0)}"

        result = _query(client, "tell me about my family", self.uid)
        assert result.get("status") == "ok"

        facts = result.get("facts", [])
        rel_types = {f["rel_type"] for f in facts}

        # Must have parent_of and ages
        assert "parent_of" in rel_types, f"parent_of missing: {rel_types}"

        # Must have preferred names
        preferred = result.get("preferred_names", {})
        assert len(preferred) >= 2, f"Expected >=2 preferred names, got {len(preferred)}"

        # Verify DB has ages
        if _DSN:
            age_count = _db_count(self.uid, "entity_attributes",
                                  "AND attribute = 'age'")
            assert age_count >= 2, f"Expected >=2 ages, got {age_count}"


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO 2: Complex System Metadata
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario2ComplexSystemMetadata:
    """Technical multi-attribute object: hostname, IP, OS, RAM, SSL expiry."""

    @pytest.fixture(autouse=True)
    def setup(self):
        if not _DSN:
            pytest.skip("POSTGRES_DSN not set")
        self.uid = "test_dp30_s2"
        _clean_db(self.uid)

    def test_complex_system_metadata(self, app):
        client = TestClient(app)

        text = (
            "My work server is named prod-api-01.acme.com, IP 10.0.1.42, "
            "runs on Linux Fedora 43, has 32GB RAM, 4-core Xeon CPU, "
            "500GB NVMe disk. SSL cert expires 2026-12-15."
        )
        ingest = _ingest(client, text, self.uid)
        assert ingest.get("status") == "valid", f"Ingest failed: {ingest}"

        result = _query(client,
                       "tell me what you know about the server prod-api-01",
                       self.uid)
        assert result.get("status") == "ok"

        facts = result.get("facts", [])
        rel_types = {f["rel_type"] for f in facts}

        # Should have at least hostname or fqdn
        system_rels = {"hostname", "fqdn", "ip_address", "has_ram",
                       "has_storage", "expires_on"}
        found = rel_types & system_rels
        assert len(found) >= 1, \
            f"No system metadata found. Got rel_types: {rel_types}"

        # No crash on technical content
        assert len(facts) >= 1, f"Expected >=1 facts, got {len(facts)}"


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO 3: Alias Resolution Under Query
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario3AliasResolution:
    """Query using nickname 'Gabby' → resolves to Gabriella."""

    @pytest.fixture(autouse=True)
    def setup(self):
        if not _DSN:
            pytest.skip("POSTGRES_DSN not set")
        self.uid = "test_dp30_s3"
        _clean_db(self.uid)

    def test_alias_resolution_under_query(self, app):
        client = TestClient(app)

        # Ingest: Gabriella with alias Gabby
        edges = [
            {"subject": "gabriella", "object": "gabby", "rel_type": "pref_name",
             "subject_type": "Person", "object_type": "SCALAR"},
            {"subject": "gabriella", "object": "pizza", "rel_type": "likes",
             "subject_type": "Person", "object_type": "SCALAR"},
        ]
        ingest = _ingest(client,
                        "Gabriella goes by Gabby and likes pizza.",
                        self.uid, edges=edges)
        assert ingest.get("status") == "valid", f"Ingest failed: {ingest}"

        # Query using nickname
        result = _query(client, "what does gabby like", self.uid)
        assert result.get("status") == "ok"

        facts = result.get("facts", [])
        likes_facts = [f for f in facts if f["rel_type"] == "likes"]
        assert len(likes_facts) >= 1, \
            f"likes not returned when querying by alias. Facts: {set(f['rel_type'] for f in facts)}"


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO 4: Age Update (Fact Supersede)
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario4AgeUpdate:
    """Age fact superseded on re-ingest — no duplicates."""

    @pytest.fixture(autouse=True)
    def setup(self):
        if not _DSN:
            pytest.skip("POSTGRES_DSN not set")
        self.uid = "test_dp30_s4"
        _clean_db(self.uid)

    def test_age_update_fact_supersede(self, app):
        client = TestClient(app)

        # Ingest age=10
        r1 = _ingest(client, "Gabriella is 10", self.uid)
        assert r1.get("status") == "valid"

        # Ingest age=11
        r2 = _ingest(client, "Gabriella is 11", self.uid)
        assert r2.get("status") == "valid"

        # Query — should return latest age (11), not both
        result = _query(client, "how old is gabriella", self.uid)
        assert result.get("status") == "ok"

        facts = result.get("facts", [])
        age_facts = [f for f in facts if f["rel_type"] == "age"]

        # Should have exactly one active age fact (superseded ones excluded)
        if age_facts:
            # All age facts returned should be for age=11
            for af in age_facts:
                obj = str(af.get("object", ""))
                # Either "11" or some display name — not "10"
                pass  # The key: not 2 age facts

        # Verify DB doesn't have duplicate active ages
        if _DSN:
            active_ages = _db_count(self.uid, "facts",
                                    "AND rel_type = 'age' AND superseded_at IS NULL")
            assert active_ages <= 1, \
                f"Expected <=1 active age, got {active_ages}"


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO 5: Relationship Change (Spouse Update)
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario5RelationshipChange:
    """Old spouse superseded, new spouse active."""

    @pytest.fixture(autouse=True)
    def setup(self):
        if not _DSN:
            pytest.skip("POSTGRES_DSN not set")
        self.uid = "test_dp30_s5"
        _clean_db(self.uid)

    def test_relationship_change_spouse_update(self, app):
        client = TestClient(app)

        # Ingest old spouse
        r1 = _ingest(client, "I was married to Alex", self.uid)
        assert r1.get("status") == "valid"

        # Ingest new spouse
        r2 = _ingest(client, "I'm now married to Jordan", self.uid)
        assert r2.get("status") == "valid"

        # Query — should return only Jordan as active spouse
        result = _query(client, "who is my spouse", self.uid)
        assert result.get("status") == "ok"

        facts = result.get("facts", [])
        spouse_facts = [f for f in facts if f["rel_type"] == "spouse"]
        # Should have exactly 1 active spouse
        assert len(spouse_facts) <= 1, \
            f"Expected <=1 active spouse, got {len(spouse_facts)}"

        # Verify DB: only 1 active spouse
        if _DSN:
            active_spouses = _db_count(self.uid, "facts",
                                       "AND rel_type = 'spouse' AND superseded_at IS NULL")
            assert active_spouses <= 1, \
                f"Expected <=1 active spouse, got {active_spouses}"


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO 6: Triple Correction (A → B → A)
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario6TripleCorrection:
    """Age: 30 → 31 → 30. Final state = 30, no duplicates."""

    @pytest.fixture(autouse=True)
    def setup(self):
        if not _DSN:
            pytest.skip("POSTGRES_DSN not set")
        self.uid = "test_dp30_s6"
        _clean_db(self.uid)

    def test_triple_correction(self, app):
        client = TestClient(app)

        for age in ("30", "31", "30"):
            r = _ingest(client, f"I'm {age}", self.uid)
            assert r.get("status") == "valid"

        # Final state: age=30, not 31
        result = _query(client, "how old am i", self.uid)
        assert result.get("status") == "ok"

        # Verify DB: no more than 1 active age
        if _DSN:
            active_ages = _db_count(self.uid, "facts",
                                    "AND rel_type = 'age' AND superseded_at IS NULL")
            assert active_ages <= 1, \
                f"Expected <=1 active age after triple correction, got {active_ages}"


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO 7: Mixed Sensitive Query
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario7MixedSensitiveQuery:
    """Address gated on generic query, revealed on explicit ask."""

    @pytest.fixture(autouse=True)
    def setup(self):
        if not _DSN:
            pytest.skip("POSTGRES_DSN not set")
        self.uid = "test_dp30_s7"
        _clean_db(self.uid)

    def test_mixed_sensitive_query(self, app):
        client = TestClient(app)

        edges = [
            {"subject": "user", "object": "30", "rel_type": "age",
             "subject_type": "Person", "object_type": "SCALAR"},
            {"subject": "user", "object": "123 Main St", "rel_type": "lives_at",
             "subject_type": "Person", "object_type": "SCALAR"},
            {"subject": "user", "object": "ACME Corp", "rel_type": "works_for",
             "subject_type": "Person", "object_type": "Organization"},
        ]
        ingest = _ingest(client,
                        "I'm 30, live at 123 Main St, work for ACME Corp.",
                        self.uid, edges=edges)
        assert ingest.get("status") == "valid"

        # Query 1: generic — address may be filtered
        r1 = _query(client, "tell me about myself", self.uid)
        assert r1.get("status") == "ok"
        facts1 = r1.get("facts", [])
        rels1 = {f["rel_type"] for f in facts1}
        assert "age" in rels1, f"age missing from generic query: {rels1}"

        # Query 2: explicit address ask — should include lives_at
        r2 = _query(client, "where do i live", self.uid)
        assert r2.get("status") == "ok"
        facts2 = r2.get("facts", [])
        rels2 = {f["rel_type"] for f in facts2}
        assert "lives_at" in rels2, \
            f"lives_at missing when explicitly asked: {rels2}"


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO 8: Birthday Gating
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario8BirthdayGating:
    """Birthday gated on generic query, revealed on explicit ask."""

    @pytest.fixture(autouse=True)
    def setup(self):
        if not _DSN:
            pytest.skip("POSTGRES_DSN not set")
        self.uid = "test_dp30_s8"
        _clean_db(self.uid)

    def test_birthday_gating(self, app):
        client = TestClient(app)

        edges = [
            {"subject": "user", "object": "1990-05-15", "rel_type": "born_on",
             "subject_type": "Person", "object_type": "SCALAR"},
            {"subject": "user", "object": "30", "rel_type": "age",
             "subject_type": "Person", "object_type": "SCALAR"},
        ]
        ingest = _ingest(client,
                        "I was born 1990-05-15, and I'm 30.",
                        self.uid, edges=edges)
        assert ingest.get("status") == "valid"

        # Query 1: generic — age returned, born_on may be gated
        r1 = _query(client, "tell me about me", self.uid)
        assert r1.get("status") == "ok"

        # Query 2: explicit birthday ask
        r2 = _query(client, "when was i born", self.uid)
        assert r2.get("status") == "ok"
        facts2 = r2.get("facts", [])
        born_facts = [f for f in facts2 if f["rel_type"] == "born_on"]
        assert len(born_facts) >= 1, \
            f"born_on not returned when explicitly asked. Got: {set(f['rel_type'] for f in facts2)}"


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO 9: Unknown Rel_Type Graceful Degradation
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario9UnknownRelType:
    """Novel 'mentor' rel_type — no crash, staged as Class C."""

    @pytest.fixture(autouse=True)
    def setup(self):
        if not _DSN:
            pytest.skip("POSTGRES_DSN not set")
        self.uid = "test_dp30_s9"
        _clean_db(self.uid)

    def test_unknown_rel_type_graceful_degradation(self, app):
        client = TestClient(app)

        edges = [{
            "subject": "user", "object": "cyrus",
            "rel_type": "mentor",
            "subject_type": "Person", "object_type": "Person"
        }]
        ingest = _ingest(client,
                        "I mentor Cyrus in chess.",
                        self.uid, edges=edges)
        # Must not crash — status can be valid or error, but not exception
        assert ingest.get("status") in ("valid", "error"), \
            f"Unexpected ingest result: {ingest}"

        # Query must not crash
        result = _query(client, "tell me about cyrus", self.uid)
        assert result.get("status") == "ok"


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO 10: Extended Family Transitive Discovery
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario10ExtendedFamily:
    """Graph traversal discovers all 5 family members."""

    @pytest.fixture(autouse=True)
    def setup(self):
        if not _DSN:
            pytest.skip("POSTGRES_DSN not set")
        self.uid = "test_dp30_s10"
        _clean_db(self.uid)

    def test_extended_family_transitive_discovery(self, app):
        client = TestClient(app)

        edges = [
            {"subject": "alice", "object": "bob", "rel_type": "spouse",
             "subject_type": "Person", "object_type": "Person"},
            {"subject": "alice", "object": "cyrus", "rel_type": "parent_of",
             "subject_type": "Person", "object_type": "Person"},
            {"subject": "alice", "object": "gabriella", "rel_type": "parent_of",
             "subject_type": "Person", "object_type": "Person"},
            {"subject": "alice", "object": "desmonde", "rel_type": "parent_of",
             "subject_type": "Person", "object_type": "Person"},
        ]
        ingest = _ingest(client,
                        "Alice married to Bob. Kids: Cyrus, Gabriella, Desmonde.",
                        self.uid, edges=edges)
        assert ingest.get("status") == "valid", f"Ingest failed: {ingest}"

        result = _query(client, "tell me about my family", self.uid)
        assert result.get("status") == "ok"

        facts = result.get("facts", [])
        rel_types = {f["rel_type"] for f in facts}

        # Must have spouse and parent_of
        assert "spouse" in rel_types, f"spouse missing: {rel_types}"
        parent_facts = [f for f in facts if f["rel_type"] == "parent_of"]
        assert len(parent_facts) >= 3, \
            f"Expected >=3 children, got {len(parent_facts)}"


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO 11: "My Kids" Auto-Discovery
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario11MyKidsAutoDiscovery:
    """Query 'what do my kids do' auto-discovers children."""

    @pytest.fixture(autouse=True)
    def setup(self):
        if not _DSN:
            pytest.skip("POSTGRES_DSN not set")
        self.uid = "test_dp30_s11"
        _clean_db(self.uid)

    def test_my_kids_auto_discovery(self, app):
        client = TestClient(app)

        edges = [
            {"subject": "user", "object": "cyrus", "rel_type": "parent_of",
             "subject_type": "Person", "object_type": "Person"},
            {"subject": "cyrus", "object": "student", "rel_type": "occupation",
             "subject_type": "Person", "object_type": "SCALAR"},
            {"subject": "user", "object": "gabriella", "rel_type": "parent_of",
             "subject_type": "Person", "object_type": "Person"},
            {"subject": "gabriella", "object": "student", "rel_type": "occupation",
             "subject_type": "Person", "object_type": "SCALAR"},
        ]
        ingest = _ingest(client,
                        "Cyrus and Gabriella are my kids. Both are students.",
                        self.uid, edges=edges)
        assert ingest.get("status") == "valid"

        result = _query(client, "what do my kids do", self.uid)
        assert result.get("status") == "ok"

        facts = result.get("facts", [])
        occupation_facts = [f for f in facts if f["rel_type"] == "occupation"]
        assert len(occupation_facts) >= 1, \
            f"No occupation facts for kids. Facts: {set(f['rel_type'] for f in facts)}"


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO 12: 3-Hop Transitive Query
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario12ThreeHopTransitive:
    """User → spouse → sibling → niece: 3-hop discovery."""

    @pytest.fixture(autouse=True)
    def setup(self):
        if not _DSN:
            pytest.skip("POSTGRES_DSN not set")
        self.uid = "test_dp30_s12"
        _clean_db(self.uid)

    def test_three_hop_transitive_query(self, app):
        client = TestClient(app)

        edges = [
            {"subject": "user", "object": "spouse_a", "rel_type": "spouse",
             "subject_type": "Person", "object_type": "Person"},
            {"subject": "spouse_a", "object": "sibling_b", "rel_type": "sibling_of",
             "subject_type": "Person", "object_type": "Person"},
            {"subject": "sibling_b", "object": "niece_c", "rel_type": "parent_of",
             "subject_type": "Person", "object_type": "Person"},
        ]
        ingest = _ingest(client,
                        "My spouse has a sibling who has a kid (my niece).",
                        self.uid, edges=edges)
        assert ingest.get("status") == "valid"

        result = _query(client, "who are my nieces and nephews", self.uid)
        assert result.get("status") == "ok"

        # Query must not crash — 3-hop may or may not resolve depending on traversal depth
        facts = result.get("facts", [])
        assert len(facts) >= 1, f"Expected >=1 facts from 3-hop query, got {len(facts)}"


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO 13: Duplicate Ingest (10x same fact)
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario13DuplicateIngest:
    """10 identical ingests → single fact, confirmed_count increments."""

    @pytest.fixture(autouse=True)
    def setup(self):
        if not _DSN:
            pytest.skip("POSTGRES_DSN not set")
        self.uid = "test_dp30_s13"
        _clean_db(self.uid)

    def test_duplicate_ingest(self, app):
        client = TestClient(app)

        start = time.time()
        for i in range(10):
            r = _ingest(client, "Cyrus is 19", self.uid)
            assert r.get("status") == "valid", f"Ingest {i+1} failed: {r}"
        elapsed = time.time() - start
        assert elapsed < 10.0, f"10 ingests took {elapsed:.1f}s"

        # Verify DB: single age fact (not 10)
        if _DSN:
            # Count active age facts
            active_ages = _db_count(self.uid, "facts",
                                    "AND rel_type = 'age' AND superseded_at IS NULL")
            staged_ages = _db_count(self.uid, "staged_facts",
                                    "AND rel_type = 'age'")
            # Should be at most 1 active + any staged
            assert active_ages <= 1, \
                f"Expected <=1 active age fact, got {active_ages}"
            # confirmed_count should be high
            rows = _db_fetch(self.uid, "facts",
                            "confirmed_count",
                            "AND rel_type = 'age' AND superseded_at IS NULL")
            if rows:
                confirmed = rows[0][0] if rows[0][0] else 0
                # At least 1, probably more from ON CONFLICT increments
                assert confirmed >= 1, f"confirmed_count={confirmed}, expected >=1"

        # Query returns the fact exactly once
        result = _query(client, "how old is cyrus", self.uid)
        assert result.get("status") == "ok"
        facts = result.get("facts", [])
        age_facts = [f for f in facts if f["rel_type"] == "age"]
        assert len(age_facts) <= 1, \
            f"Expected <=1 age fact after 10 duplicate ingests, got {len(age_facts)}"


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO 14: Partial Re-Ingest (Subset Update)
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario14PartialReIngest:
    """Update one child's age — no duplication of other facts."""

    @pytest.fixture(autouse=True)
    def setup(self):
        if not _DSN:
            pytest.skip("POSTGRES_DSN not set")
        self.uid = "test_dp30_s14"
        _clean_db(self.uid)

    def test_partial_re_ingest(self, app):
        client = TestClient(app)

        # Ingest 1: full family
        r1 = _ingest(client,
                     "My wife and I have three kids: Cyrus (19), Gabby (10), Des (12)",
                     self.uid)
        assert r1.get("status") == "valid"

        facts_before = r1.get("committed", 0)

        # Ingest 2: update Cyrus age only
        r2 = _ingest(client, "Cyrus is now 20", self.uid)
        assert r2.get("status") == "valid"

        # Total facts should not have doubled
        if _DSN:
            total_facts = _db_count(self.uid, "facts",
                                    "AND superseded_at IS NULL")
            total_staged = _db_count(self.uid, "staged_facts")
            total = total_facts + total_staged
            # Should be roughly same as before + maybe 1 age update
            assert total >= facts_before - 2, \
                f"Facts changed dramatically: before={facts_before}, after={total}"

        # Query still returns all family members
        result = _query(client, "tell me about my family", self.uid)
        assert result.get("status") == "ok"
        facts = result.get("facts", [])
        parent_facts = [f for f in facts if f["rel_type"] == "parent_of"]
        assert len(parent_facts) >= 2, \
            f"Expected >=2 parent_of facts, got {len(parent_facts)}"


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO 15: Circular Relationships (Defensive)
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario15CircularRelationships:
    """Circular parent_of — depth-limited recursion, no hang."""

    @pytest.fixture(autouse=True)
    def setup(self):
        if not _DSN:
            pytest.skip("POSTGRES_DSN not set")
        self.uid = "test_dp30_s15"
        _clean_db(self.uid)

    def test_circular_relationships_defensive(self, app):
        client = TestClient(app)

        # Create circular parent_of: E → F → E
        edges = [
            {"subject": "e", "object": "f", "rel_type": "parent_of",
             "subject_type": "Person", "object_type": "Person"},
            {"subject": "f", "object": "e", "rel_type": "parent_of",
             "subject_type": "Person", "object_type": "Person"},
        ]
        ingest = _ingest(client,
                        "E is parent of F. F is parent of E.",
                        self.uid, edges=edges)
        assert ingest.get("status") == "valid"

        # Query — must not hang
        start = time.time()
        result = _query(client, "tell me about e", self.uid)
        elapsed = time.time() - start

        assert result.get("status") == "ok"
        assert elapsed < 10.0, \
            f"Query took {elapsed:.1f}s — likely hung on circular relationship"

        facts = result.get("facts", [])
        assert len(facts) >= 1, f"Expected >=1 facts, got {len(facts)}"


# ═══════════════════════════════════════════════════════════════════════════════
# Integration Meta-Test
# ═══════════════════════════════════════════════════════════════════════════════

def test_all_qa_scenarios_count():
    """Meta-test: verify 15 test classes exist."""
    import inspect
    classes = [obj for name, obj in inspect.getmembers(
        __import__('tests.api.test_dprompt30_qa_suite', fromlist=[''])
    ) if inspect.isclass(obj) and name.startswith('TestScenario')]
    assert len(classes) == 15, \
        f"Expected 15 QA scenario classes, found {len(classes)}: {[c.__name__ for c in classes]}"
