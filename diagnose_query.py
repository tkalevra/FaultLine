#!/usr/bin/env python3
"""
Standalone diagnostic to identify why spouse facts aren't appearing in /query.
Run this INSIDE the FaultLine container on TrueNAS.
"""

import os
import sys
import psycopg2
import json

def main():
    print("\n" + "=" * 80)
    print("FAULTLINE /query DIAGNOSIS")
    print("=" * 80 + "\n")

    # Get env vars
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        print("[FATAL] POSTGRES_DSN not set!")
        print("  Expected: postgresql://faultline:faultline@postgres:5432/faultline_test")
        return False

    print(f"[OK] POSTGRES_DSN is set\n")

    # Try database connection
    print("[TEST 1] Database Connection")
    print("-" * 80)
    try:
        db = psycopg2.connect(dsn)
        print("[OK] Database connection successful")
    except Exception as e:
        print(f"[FATAL] Database connection failed: {e}")
        print("  This is why /query cannot fetch facts!")
        return False

    # Check for test user facts
    user_id = "anonymous"
    print(f"\n[TEST 2] Looking for facts in database (user_id='{user_id}')")
    print("-" * 80)

    with db.cursor() as cur:
        # Count all facts
        cur.execute("SELECT COUNT(*) FROM facts WHERE user_id = %s", (user_id,))
        total_facts = cur.fetchone()[0]
        print(f"Total facts for user: {total_facts}")

        # Look for spouse facts
        cur.execute(
            "SELECT id, subject_id, object_id, rel_type, confidence FROM facts "
            "WHERE user_id = %s AND rel_type = 'spouse' LIMIT 5",
            (user_id,)
        )
        spouse_facts = cur.fetchall()
        print(f"Spouse facts: {len(spouse_facts)}")
        for row in spouse_facts:
            fact_id, subject, obj, rel_type, conf = row
            print(f"  ID={fact_id} {subject} --{rel_type}--> {obj} (conf={conf})")

        # Look for pref_name facts
        cur.execute(
            "SELECT id, subject_id, object_id, rel_type FROM facts "
            "WHERE user_id = %s AND rel_type = 'pref_name' LIMIT 5",
            (user_id,)
        )
        pref_facts = cur.fetchall()
        print(f"Pref_name facts: {len(pref_facts)}")
        for row in pref_facts:
            fact_id, subject, obj, rel_type = row
            print(f"  ID={fact_id} {subject} --{rel_type}--> {obj}")

        # Check if any facts at all
        if total_facts == 0:
            print("\n[WARNING] No facts in database at all!")
            print("  Ingest might not be working. Check /ingest logs.")
            db.close()
            return False

        # Test 3: Entity registry
        print(f"\n[TEST 3] Entity Registry (_make_surrogate)")
        print("-" * 80)

        # Try to import and test the registry
        sys.path.insert(0, '/app/src')
        try:
            from entity_registry.registry import EntityRegistry, _make_surrogate
            registry = EntityRegistry(db)

            # Test resolve for "anonymous" user
            try:
                result = registry.resolve(user_id, "test_entity")
                print(f"[OK] resolve('test_entity') = {result}")
                if result and len(result) == 36 and result.count('-') == 4:
                    print("    (This is a valid UUID v5)")
                else:
                    print(f"    [WARNING] Unexpected format: {result}")
            except Exception as e:
                print(f"[ERROR] resolve() failed: {e}")

            # Test get_preferred_name for user
            try:
                display = registry.get_preferred_name(user_id, user_id)
                print(f"[OK] get_preferred_name('{user_id}', '{user_id}') = {display}")
                if display is None:
                    print("    [ERROR] Returns None - /query graph traversal will fail!")
                elif display == user_id:
                    print(f"    (Returns user_id as fallback - OK)")
            except Exception as e:
                print(f"[ERROR] get_preferred_name() failed: {e}")

        except ImportError as e:
            print(f"[ERROR] Cannot import EntityRegistry: {e}")
            print("  Make sure FaultLine is installed in /app")

        # Test 4: Simulate /query logic
        print(f"\n[TEST 4] Simulate /query graph traversal logic")
        print("-" * 80)

        # Check if we can fetch facts anchored to user
        try:
            cur.execute(
                "SELECT subject_id, object_id, rel_type FROM facts "
                "WHERE user_id = %s AND (subject_id = %s OR object_id = %s) "
                "LIMIT 10",
                (user_id, user_id, user_id)
            )
            user_anchored = cur.fetchall()
            print(f"Facts anchored to user ('{user_id}'): {len(user_anchored)}")
            for subject, obj, rel_type in user_anchored:
                print(f"  {subject} --{rel_type}--> {obj}")

            if len(user_anchored) == 0:
                print("\n[ERROR] No facts are anchored to the user!")
                print("  This means /query graph traversal won't return any spouse facts.")
        except Exception as e:
            print(f"[ERROR] Query failed: {e}")

    db.close()

    # Final verdict
    print("\n" + "=" * 80)
    print("DIAGNOSIS SUMMARY")
    print("=" * 80)

    if total_facts > 0 and len(user_anchored) > 0:
        print("[OK] Facts are in database and anchored to user.")
        print("     /query should be returning them. Check Filter logs.")
    elif total_facts > 0:
        print("[ERROR] Facts exist but aren't anchored to the user.")
        print("        spouse facts have subject_id != user_id")
        print("        This is a data integrity issue.")
    else:
        print("[ERROR] No facts in database.")
        print("        Ingest is failing or facts aren't being persisted.")

    return True

if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n[FATAL] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
