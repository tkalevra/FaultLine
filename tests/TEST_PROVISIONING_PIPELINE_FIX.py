#!/usr/bin/env python
"""
TEST PROVISIONING PIPELINE FIX — Comprehensive end-to-end verification

This test validates that the provisioning pipeline:
1. Creates user provisioning records correctly
2. Processes them via the background queue
3. Creates schemas in PostgreSQL
4. Bootstraps metadata (rel_types, taxonomies, patterns)
5. Marks provisioning as ready only after successful completion

Run this after deploying commit a2350cf to verify the fix works.

Usage:
    pytest tests/TEST_PROVISIONING_PIPELINE_FIX.py -v -s
"""

import os
import uuid
import psycopg2
import pytest
from pathlib import Path
from src.provisioning.provisioning_status import check_provisioning_status, ensure_user_provisioned
from src.provisioning.schema_manager import (
    create_user_schema,
    derive_schema_name,
    derive_user_slug_from_uuid,
    get_postgres_connection,
)
from src.provisioning.provisioning_job import process_provisioning_queue


@pytest.fixture
def postgres_dsn():
    """Get PostgreSQL DSN from environment."""
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        pytest.skip("POSTGRES_DSN not set")
    return dsn


@pytest.fixture
def test_user_id():
    """Generate a test user ID."""
    return str(uuid.uuid4())


def test_derive_schema_name_from_uuid():
    """Test that UUID slug becomes proper schema name."""
    user_id = "7e4b1d02-8a35-4c67-9d24-51e0b3a97f16"
    slug = derive_user_slug_from_uuid(user_id)
    schema_name = derive_schema_name(slug)
    assert schema_name == "faultline_7e4b1d02_8a35_4c67_9d24_51e0b3a97f16"
    assert schema_name.startswith("faultline_")


def test_ensure_user_provisioned_creates_record(postgres_dsn, test_user_id):
    """Test that ensure_user_provisioned creates provisioning record."""
    user_slug = derive_user_slug_from_uuid(test_user_id)

    # Should return False (not yet ready)
    is_ready = ensure_user_provisioned(
        test_user_id,
        user_slug=user_slug,
        user_name="Test User"
    )
    assert not is_ready, "User should not be ready immediately"

    # Check status in database
    status = check_provisioning_status(test_user_id)
    assert status["status"] in ["provisioning", "ready"], f"Unexpected status: {status}"
    assert status["user_id"] == test_user_id


def test_check_provisioning_status_not_found():
    """Test that unknown user returns not_found status."""
    fake_id = str(uuid.uuid4())
    status = check_provisioning_status(fake_id)
    assert status["status"] in ["not_found", "error"]


def test_create_user_schema_idempotent(postgres_dsn, test_user_id):
    """Test that create_user_schema is idempotent."""
    user_slug = derive_user_slug_from_uuid(test_user_id)
    schema_name = derive_schema_name(user_slug)

    # First call
    name1, status1 = create_user_schema(test_user_id, user_slug)
    assert status1 == "ready", f"First creation failed: {status1}"
    assert name1 == schema_name

    # Second call (should also succeed)
    name2, status2 = create_user_schema(test_user_id, user_slug)
    assert status2 == "ready", f"Second creation failed: {status2}"
    assert name2 == schema_name


def test_schema_exists_after_creation(postgres_dsn, test_user_id):
    """Test that schema actually exists in PostgreSQL."""
    user_slug = derive_user_slug_from_uuid(test_user_id)
    schema_name = derive_schema_name(user_slug)

    # Create schema
    name, status = create_user_schema(test_user_id, user_slug)
    assert status == "ready", f"Schema creation failed: {status}"

    # Verify it exists
    db = get_postgres_connection()
    try:
        with db.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM information_schema.schemata
                WHERE schema_name = %s
            """, (schema_name,))
            exists = cur.fetchone()
            assert exists, f"Schema {schema_name} not found after creation!"
    finally:
        db.close()


def test_bootstrap_tables_exist(postgres_dsn, test_user_id):
    """Test that bootstrap tables (facts, staged_facts, etc.) exist."""
    user_slug = derive_user_slug_from_uuid(test_user_id)
    schema_name = derive_schema_name(user_slug)

    # Create schema
    name, status = create_user_schema(test_user_id, user_slug)
    assert status == "ready"

    # Verify tables exist
    db = get_postgres_connection()
    try:
        with db.cursor() as cur:
            cur.execute(f"SET search_path TO {schema_name}, public")

            # Check critical tables
            tables_to_check = [
                "facts",
                "staged_facts",
                "entities",
                "entity_aliases",
                "entity_attributes",
                "rel_types",
                "entity_taxonomies",
                "negation_patterns",
            ]

            for table_name in tables_to_check:
                cur.execute("""
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = %s AND table_name = %s
                """, (schema_name, table_name))
                exists = cur.fetchone()
                assert exists, f"Table {table_name} not found in {schema_name}"
    finally:
        db.close()


def test_rel_types_bootstrap_data(postgres_dsn, test_user_id):
    """Test that rel_types table is populated with bootstrap data."""
    user_slug = derive_user_slug_from_uuid(test_user_id)
    schema_name = derive_schema_name(user_slug)

    # Create schema
    name, status = create_user_schema(test_user_id, user_slug)
    assert status == "ready"

    # Verify rel_types bootstrap
    db = get_postgres_connection()
    try:
        with db.cursor() as cur:
            cur.execute(f"SET search_path TO {schema_name}, public")
            cur.execute("SELECT COUNT(*) FROM rel_types")
            count = cur.fetchone()[0]
            assert count >= 35, f"Expected at least 35 rel_types, got {count}"

            # Check specific critical rel_types
            critical_types = [
                "parent_of",
                "child_of",
                "spouse",
                "instance_of",
                "subclass_of",
            ]

            for rel_type in critical_types:
                cur.execute("SELECT 1 FROM rel_types WHERE rel_type = %s", (rel_type,))
                exists = cur.fetchone()
                assert exists, f"Critical rel_type '{rel_type}' not bootstrapped"
    finally:
        db.close()


def test_provisioning_queue_processing(postgres_dsn):
    """Test that process_provisioning_queue successfully processes pending users."""
    # Create a pending provisioning record
    test_user_id = str(uuid.uuid4())
    user_slug = derive_user_slug_from_uuid(test_user_id)

    # Ensure provisioning record is created
    is_ready = ensure_user_provisioned(
        test_user_id,
        user_slug=user_slug,
        user_name="Queue Test User"
    )
    assert not is_ready, "Should not be ready immediately"

    # Process queue
    result = process_provisioning_queue(batch_size=10)

    # Check that provisioning succeeded
    assert result["provisioned"] >= 0, "Provisioning count should be non-negative"
    assert result["failed"] >= 0, "Failed count should be non-negative"

    # Verify status is now ready
    status = check_provisioning_status(test_user_id)
    assert status["status"] == "ready", f"Status should be ready, got {status['status']}"


def test_provisioning_marks_status_only_after_success(postgres_dsn, test_user_id):
    """Test that status is only marked ready after all steps succeed."""
    user_slug = derive_user_slug_from_uuid(test_user_id)

    # Ensure provisioning record exists
    is_ready = ensure_user_provisioned(test_user_id, user_slug=user_slug)
    assert not is_ready

    # Process queue
    result = process_provisioning_queue(batch_size=10)

    # Get status
    status = check_provisioning_status(test_user_id)

    if status["status"] == "ready":
        # Verify schema actually exists
        schema_name = derive_schema_name(user_slug)
        db = get_postgres_connection()
        try:
            with db.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM information_schema.schemata
                    WHERE schema_name = %s
                """, (schema_name,))
                exists = cur.fetchone()
                assert exists, "Status is ready but schema doesn't exist!"
        finally:
            db.close()
    else:
        # If not ready, verify error message is present
        if status["status"] == "error":
            assert "error_message" in status or status.get("error_message")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
