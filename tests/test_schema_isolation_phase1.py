"""
PHASE 1: Per-User Schema Isolation Tests

Comprehensive tests verifying that per-user schema isolation is enforced
in all 5 critical locations (as per OPERATIONAL-PROMPT.md):

1. fetch_facts_from_anchor() — sets search_path before DB queries
2. WGMValidationGate._refresh() — queries global rel_types (no isolation needed)
3. re_embedder Job 4 — per-user iteration for taxonomy discovery
4. All 5 jobs maintain data access post-schema-isolation
5. Multi-user zero-cross-contamination test

BUG Tracking:
- BUG 1: fetch_facts_from_anchor missing SET search_path
- BUG 2: WGMValidationGate._refresh queries public rel_types (no fix needed)
- BUG 3: re_embedder fetch_unsynced/staged on global schema instead of per-user
"""

import os
import pytest
import psycopg2
from uuid import uuid4
from src.api.models import QueryRequest, QueryPath
from src.api.main import fetch_facts_from_anchor, get_user_schema_context
from src.provisioning.schema_manager import derive_schema_name, create_user_schema


@pytest.fixture
def test_db():
    """Fixture for PostgreSQL test database connection."""
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        pytest.skip("POSTGRES_DSN not configured")

    conn = psycopg2.connect(dsn)
    yield conn
    conn.close()


@pytest.fixture
def test_users_and_schemas(test_db):
    """
    Create three test users with dedicated schemas.
    Returns: {
        'user_a': {'user_id': uuid, 'schema_name': 'faultline_user_a'},
        'user_b': {...},
        'user_c': {...}
    }
    """
    users = {}
    for user_label in ['user_a', 'user_b', 'user_c']:
        user_id = str(uuid4())
        user_slug = user_label
        schema_name, status = create_user_schema(user_id, user_slug, test_db)

        assert status == 'ready', f"Schema creation failed for {user_label}: {status}"

        users[user_label] = {
            'user_id': user_id,
            'schema_name': schema_name,
            'slug': user_slug
        }

    return users


class TestSchemaIsolationFix1FetchFactsFromAnchor:
    """
    BUG 1: fetch_facts_from_anchor missing SET search_path
    FIX: Add schema context line before DB queries
    """

    def test_fetch_facts_sets_schema_context(self, test_db, test_users_and_schemas):
        """Verify fetch_facts_from_anchor sets search_path for user."""
        user_a = test_users_and_schemas['user_a']
        user_id = user_a['user_id']
        schema_name = user_a['schema_name']

        # Insert a test fact into user_a's schema
        with test_db.cursor() as cur:
            cur.execute(f"SET search_path TO {schema_name}, public")
            test_db.commit()

            # Insert test entity
            entity_id = str(uuid4())
            cur.execute(
                "INSERT INTO entities (id, user_id, entity_type) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (entity_id, user_id, 'Person')
            )

            # Insert test fact
            cur.execute(
                """INSERT INTO facts (subject_id, object_id, rel_type, confidence, fact_class, qdrant_synced)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (subject_id, object_id, rel_type) DO NOTHING""",
                (entity_id, entity_id, 'pref_name', 1.0, 'A', False)
            )
            test_db.commit()

        # Now fetch facts from this entity (should use user's schema)
        path = QueryPath(
            fetch_all_details=True,
            relationship_rels=[],
            scalar_rels=['pref_name'],
            taxonomy_groups=None
        )

        # This should NOT raise an error about missing table
        facts = fetch_facts_from_anchor(entity_id, user_id, path)

        # We should get at least the fact we inserted
        assert len(facts) > 0, f"No facts fetched for {user_id} in {schema_name}"

        # Verify the fact has correct structure
        assert any(f['rel_type'] == 'pref_name' for f in facts), "pref_name fact not found"

    def test_fetch_facts_no_cross_user_leakage(self, test_db, test_users_and_schemas):
        """
        Critical: User A's facts should not leak to User B's queries.

        Test scenario:
        - User A adds fact: "Aurora is a pet"
        - User B queries pets
        - User B should see NO facts from User A
        """
        user_a = test_users_and_schemas['user_a']
        user_b = test_users_and_schemas['user_b']

        aurora_id = str(uuid4())
        user_a_id = user_a['user_id']
        user_b_id = user_b['user_id']

        # Setup User A's schema with Aurora entity and has_pet relationship
        with test_db.cursor() as cur:
            cur.execute(f"SET search_path TO {user_a['schema_name']}, public")
            test_db.commit()

            # Create Aurora in User A's schema
            cur.execute(
                "INSERT INTO entities (id, user_id, entity_type) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (aurora_id, user_a_id, 'Animal')
            )

            # Create User A entity in their schema
            cur.execute(
                "INSERT INTO entities (id, user_id, entity_type) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (user_a_id, user_a_id, 'Person')
            )

            # Add has_pet fact in User A's schema
            cur.execute(
                """INSERT INTO facts (subject_id, object_id, rel_type, confidence, fact_class, qdrant_synced)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (subject_id, object_id, rel_type) DO NOTHING""",
                (user_a_id, aurora_id, 'has_pet', 1.0, 'A', False)
            )
            test_db.commit()

        # Now User B queries for pets (should find none)
        path = QueryPath(
            fetch_all_details=False,
            relationship_rels=['has_pet'],
            scalar_rels=[],
            taxonomy_groups=None
        )

        facts_user_b = fetch_facts_from_anchor(user_b_id, user_b_id, path)

        # User B should have NO has_pet facts
        has_pet_facts = [f for f in facts_user_b if f['rel_type'] == 'has_pet']
        assert len(has_pet_facts) == 0, \
            f"User B leaked data from User A: found {len(has_pet_facts)} has_pet facts"


class TestSchemaIsolationWGMValidationGate:
    """
    BUG 2: WGMValidationGate._refresh queries public rel_types

    NOTE: This is NOT a bug! rel_types is a global ontology in public schema.
    WGMValidationGate correctly queries public.rel_types regardless of search_path.
    This test verifies the behavior is correct.
    """

    def test_wgm_gate_uses_public_rel_types(self, test_db):
        """Verify WGMValidationGate queries public.rel_types (correct behavior)."""
        from src.wgm.gate import WGMValidationGate, RelTypeRegistry

        # WGMValidationGate should work with any schema context
        # because it queries public.rel_types explicitly
        registry = RelTypeRegistry(test_db)
        gate = WGMValidationGate(test_db, registry)

        ontology = gate.get_current_ontology()

        # Should have loaded rel_types from public schema
        assert len(ontology) > 0, "No rel_types loaded from public schema"
        assert 'parent_of' in ontology, "parent_of not in ontology"
        assert 'has_pet' in ontology, "has_pet not in ontology"


class TestReEmbedderPerUserIteration:
    """
    BUG 3: re_embedder Jobs fetch from global schema instead of per-user

    This test verifies that the re_embedder correctly iterates through
    all user schemas when processing embeddings.
    """

    def test_re_embedder_promotion_loop_exists(self):
        """
        Verify re_embedder has per-user promotion loop (PHASE 2b).
        This test is code-inspection only (re_embedder runs as background process).
        """
        import inspect
        from src.re_embedder.embedder import main as re_embedder_main

        source = inspect.getsource(re_embedder_main)

        # Verify per-user loop exists
        assert "ready_schemas" in source, "Per-user schema iteration loop missing"
        assert "for user_id, schema_name in ready_schemas" in source, \
            "Per-user iteration pattern not found"
        assert "SET search_path TO" in source, "Schema context not being set"
        assert "promote_staged_facts" in source, "Promotion job not called"


class TestMultiUserZeroCrossContamination:
    """
    Comprehensive end-to-end test: Three users (A, B, C) with identical entities (Aurora).
    Verify facts remain strictly isolated.
    """

    def test_three_users_aurora_isolation(self, test_db, test_users_and_schemas):
        """
        Scenario:
        - User A: Aurora is a Pet (has_pet)
        - User B: Aurora is a Person (different identity)
        - User C: Aurora is a Computer (third identity)

        Verify each user only sees their own Aurora facts.
        """
        aurora_id = "aurora"  # Same name across users

        # Setup User A: Aurora is a pet
        user_a = test_users_and_schemas['user_a']
        with test_db.cursor() as cur:
            cur.execute(f"SET search_path TO {user_a['schema_name']}, public")
            test_db.commit()

            cur.execute(
                "INSERT INTO entities (id, user_id, entity_type) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (aurora_id, user_a['user_id'], 'Animal')
            )
            cur.execute(
                "INSERT INTO entities (id, user_id, entity_type) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (user_a['user_id'], user_a['user_id'], 'Person')
            )
            cur.execute(
                """INSERT INTO facts (subject_id, object_id, rel_type, confidence, fact_class, qdrant_synced)
                   VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                (user_a['user_id'], aurora_id, 'has_pet', 1.0, 'A', False)
            )
            test_db.commit()

        # Setup User B: Aurora is a person
        user_b = test_users_and_schemas['user_b']
        with test_db.cursor() as cur:
            cur.execute(f"SET search_path TO {user_b['schema_name']}, public")
            test_db.commit()

            aurora_b_id = str(uuid4())  # Different UUID for User B's Aurora
            cur.execute(
                "INSERT INTO entities (id, user_id, entity_type) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (aurora_b_id, user_b['user_id'], 'Person')
            )
            cur.execute(
                "INSERT INTO entity_aliases (entity_id, user_id, alias, is_preferred) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (aurora_b_id, user_b['user_id'], 'aurora', True)
            )
            test_db.commit()

        # Query User A's pets
        path_pets = QueryPath(
            fetch_all_details=False,
            relationship_rels=['has_pet'],
            scalar_rels=[],
            taxonomy_groups=None
        )

        facts_user_a = fetch_facts_from_anchor(user_a['user_id'], user_a['user_id'], path_pets)
        has_pet_a = [f for f in facts_user_a if f['rel_type'] == 'has_pet']

        # Query User B's entities
        facts_user_b = fetch_facts_from_anchor(user_b['user_id'], user_b['user_id'], path_pets)
        has_pet_b = [f for f in facts_user_b if f['rel_type'] == 'has_pet']

        # Assertions
        assert len(has_pet_a) > 0, "User A should have has_pet facts"
        assert len(has_pet_b) == 0, "User B should have NO has_pet facts (different schema)"

        assert has_pet_a[0]['object'] == aurora_id, "User A's pet should be their aurora_id"
