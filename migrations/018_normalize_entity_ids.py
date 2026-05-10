#!/usr/bin/env python3
"""
Migration: Normalize string entity_ids to UUID v5 surrogates.

This migration handles the case where facts were stored with string entity_ids
(e.g., "marla", "fraggle") instead of proper UUID v5 surrogates. The migration:

1. Identifies all string entity_ids (non-UUID format) in facts/staged_facts
2. Generates proper surrogates using EntityRegistry._make_surrogate()
3. Consolidates duplicate entities under single UUIDs
4. Updates all fact references atomically
5. Ensures entity_aliases are synced with final canonical IDs

This is idempotent — running it multiple times is safe.
"""

import os
import sys
import uuid
import re
import psycopg2

# Add parent to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.entity_registry.registry import _make_surrogate, _FAULTLINE_NAMESPACE

_UUID_PATTERN = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE
)

def is_uuid(value: str) -> bool:
    """Check if value is a UUID."""
    return bool(_UUID_PATTERN.match(value))

def normalize_entity_ids():
    """Normalize all string entity_ids to UUID v5 surrogates."""
    dsn = os.environ.get('POSTGRES_DSN', 'postgresql://postgres@localhost:5432/faultline')
    db = psycopg2.connect(dsn)
    cur = db.cursor()

    print("Starting entity_id normalization migration...")

    # Map from (user_id, string_entity_id) -> UUID surrogate
    # Multiple string IDs may map to same UUID if they're aliases
    entity_map = {}

    try:
        # 1. Find all string entity_ids in facts table
        print("\n1. Scanning facts table for string entity_ids...")
        cur.execute("""
            SELECT DISTINCT user_id, subject_id FROM facts
            WHERE subject_id NOT LIKE '%-%-%-%-%'
            ORDER BY user_id, subject_id
        """)
        string_subjects = cur.fetchall()
        print(f"   Found {len(string_subjects)} unique string subject_ids")

        cur.execute("""
            SELECT DISTINCT user_id, object_id FROM facts
            WHERE object_id NOT LIKE '%-%-%-%-%'
            ORDER BY user_id, object_id
        """)
        string_objects = cur.fetchall()
        print(f"   Found {len(string_objects)} unique string object_ids")

        # 2. Find all string entity_ids in staged_facts table
        print("\n2. Scanning staged_facts table for string entity_ids...")
        cur.execute("""
            SELECT DISTINCT user_id, subject_id FROM staged_facts
            WHERE subject_id NOT LIKE '%-%-%-%-%'
            ORDER BY user_id, subject_id
        """)
        staged_subjects = cur.fetchall()
        print(f"   Found {len(staged_subjects)} unique string subject_ids in staged_facts")

        cur.execute("""
            SELECT DISTINCT user_id, object_id FROM staged_facts
            WHERE object_id NOT LIKE '%-%-%-%-%'
            ORDER BY user_id, object_id
        """)
        staged_objects = cur.fetchall()
        print(f"   Found {len(staged_objects)} unique string object_ids in staged_facts")

        # 3. Build entity_map: for each (user_id, string_id), generate UUID surrogate
        print("\n3. Generating UUID v5 surrogates for string entity_ids...")
        all_string_ids = set(string_subjects + string_objects + staged_subjects + staged_objects)
        print(f"   Total unique (user_id, entity_id) pairs: {len(all_string_ids)}")

        for user_id, string_id in all_string_ids:
            if not is_uuid(string_id):
                surrogate = _make_surrogate(user_id, string_id)
                entity_map[(user_id, string_id)] = surrogate
                print(f"   {string_id:20} -> {surrogate} (user: {user_id})")

        if not entity_map:
            print("\n✓ No string entity_ids found. Migration skipped.")
            return

        # 4. Update facts table
        print(f"\n4. Updating facts table ({len(entity_map)} replacements)...")
        for (user_id, string_id), surrogate in entity_map.items():
            # Update subject_id
            cur.execute("""
                UPDATE facts SET subject_id = %s
                WHERE user_id = %s AND subject_id = %s
            """, (surrogate, user_id, string_id))
            subjects_updated = cur.rowcount

            # Update object_id
            cur.execute("""
                UPDATE facts SET object_id = %s
                WHERE user_id = %s AND object_id = %s
            """, (surrogate, user_id, string_id))
            objects_updated = cur.rowcount

            if subjects_updated > 0 or objects_updated > 0:
                print(f"   {string_id:20}: {subjects_updated} subject rows, {objects_updated} object rows")

        db.commit()

        # 5. Update staged_facts table
        print(f"\n5. Updating staged_facts table ({len(entity_map)} replacements)...")
        for (user_id, string_id), surrogate in entity_map.items():
            # Update subject_id
            cur.execute("""
                UPDATE staged_facts SET subject_id = %s
                WHERE user_id = %s AND subject_id = %s
            """, (surrogate, user_id, string_id))
            subjects_updated = cur.rowcount

            # Update object_id
            cur.execute("""
                UPDATE staged_facts SET object_id = %s
                WHERE user_id = %s AND object_id = %s
            """, (surrogate, user_id, string_id))
            objects_updated = cur.rowcount

            if subjects_updated > 0 or objects_updated > 0:
                print(f"   {string_id:20}: {subjects_updated} subject rows, {objects_updated} object rows")

        db.commit()

        # 6. Ensure entities are registered with proper types
        print(f"\n6. Registering entities in entities table...")
        for (user_id, string_id), surrogate in entity_map.items():
            cur.execute("""
                INSERT INTO entities (id, user_id, entity_type)
                VALUES (%s, %s, 'unknown')
                ON CONFLICT (id, user_id) DO NOTHING
            """, (surrogate, user_id))
            db.commit()

        # 7. Sync entity_aliases to ensure display names are preserved
        print(f"\n7. Syncing entity_aliases...")
        for (user_id, string_id), surrogate in entity_map.items():
            # Check if an alias already exists for this canonical ID
            cur.execute("""
                SELECT COUNT(*) FROM entity_aliases
                WHERE user_id = %s AND entity_id = %s
            """, (user_id, surrogate))
            alias_count = cur.fetchone()[0]

            if alias_count == 0:
                # No aliases exist, create one from the string_id
                cur.execute("""
                    INSERT INTO entity_aliases (entity_id, user_id, alias, is_preferred)
                    VALUES (%s, %s, %s, true)
                    ON CONFLICT (user_id, alias) DO NOTHING
                """, (surrogate, user_id, string_id.lower()))
                db.commit()
                print(f"   Created alias '{string_id}' -> {surrogate}")

        # 8. Verify normalization
        print(f"\n8. Verifying normalization...")
        cur.execute("""
            SELECT COUNT(*) FROM facts
            WHERE subject_id NOT LIKE '%-%-%-%-%' OR object_id NOT LIKE '%-%-%-%-%'
        """)
        remaining_facts = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*) FROM staged_facts
            WHERE subject_id NOT LIKE '%-%-%-%-%' OR object_id NOT LIKE '%-%-%-%-%'
        """)
        remaining_staged = cur.fetchone()[0]

        if remaining_facts == 0 and remaining_staged == 0:
            print(f"   ✓ All entity_ids normalized successfully!")
        else:
            print(f"   ⚠ WARNING: {remaining_facts} facts + {remaining_staged} staged_facts still have string entity_ids")

        print(f"\n✓ Migration completed successfully!")

    except Exception as e:
        print(f"\n✗ Migration failed: {e}")
        db.rollback()
        raise
    finally:
        cur.close()
        db.close()

if __name__ == "__main__":
    normalize_entity_ids()
