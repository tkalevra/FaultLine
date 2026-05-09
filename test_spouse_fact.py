#!/usr/bin/env python3
"""
Comprehensive test to verify spouse facts flow end-to-end.
Checks: /ingest → database → /query → display name resolution
"""

import os
import json
import psycopg2
import requests
from pathlib import Path

def test_spouse_flow():
    # Configuration
    POSTGRES_DSN = os.environ.get(
        "POSTGRES_DSN",
        "postgresql://faultline:faultline@localhost:5432/faultline_test"
    )
    FAULTLINE_URL = os.environ.get("FAULTLINE_URL", "http://localhost:8001")
    USER_ID = "test-user-spouse"

    print("[TEST] Starting spouse fact end-to-end test")
    print(f"[TEST] POSTGRES_DSN: {POSTGRES_DSN}")
    print(f"[TEST] FAULTLINE_URL: {FAULTLINE_URL}")
    print(f"[TEST] USER_ID: {USER_ID}\n")

    # Step 1: POST /ingest with spouse fact
    print("=" * 80)
    print("STEP 1: Ingest spouse fact")
    print("=" * 80)

    spouse_fact = {
        "text": "My wife's name is Marla",
        "user_id": USER_ID,
        "source": "test_script"
    }

    try:
        resp = requests.post(f"{FAULTLINE_URL}/ingest", json=spouse_fact, timeout=10)
        print(f"[INGEST] Status: {resp.status_code}")
        print(f"[INGEST] Response: {json.dumps(resp.json(), indent=2)}")
        if resp.status_code != 200:
            print("[ERROR] Ingest failed!")
            return
    except Exception as e:
        print(f"[ERROR] Ingest request failed: {e}")
        return

    print()

    # Step 2: POST /ingest with pref_name fact
    print("=" * 80)
    print("STEP 2: Ingest pref_name fact")
    print("=" * 80)

    pref_fact = {
        "text": "She prefers to be called Mars",
        "user_id": USER_ID,
        "source": "test_script"
    }

    try:
        resp = requests.post(f"{FAULTLINE_URL}/ingest", json=pref_fact, timeout=10)
        print(f"[INGEST] Status: {resp.status_code}")
        print(f"[INGEST] Response: {json.dumps(resp.json(), indent=2)}")
    except Exception as e:
        print(f"[ERROR] Ingest failed: {e}")
        return

    print()

    # Step 3: Query database to verify facts were stored
    print("=" * 80)
    print("STEP 3: Check database for spouse and pref_name facts")
    print("=" * 80)

    try:
        db = psycopg2.connect(POSTGRES_DSN)
        with db.cursor() as cur:
            # Check for spouse facts
            cur.execute(
                "SELECT id, user_id, subject_id, object_id, rel_type, confidence FROM facts "
                "WHERE user_id = %s AND rel_type = 'spouse' "
                "ORDER BY created_at DESC LIMIT 10",
                (USER_ID,)
            )
            spouse_facts = cur.fetchall()
            print(f"[DB] Spouse facts found: {len(spouse_facts)}")
            for row in spouse_facts:
                print(f"     ID: {row[0]}, subject: {row[2]}, object: {row[3]}, confidence: {row[5]}")

            # Check for pref_name facts
            cur.execute(
                "SELECT id, user_id, subject_id, object_id, rel_type, confidence FROM facts "
                "WHERE user_id = %s AND rel_type = 'pref_name' "
                "ORDER BY created_at DESC LIMIT 10",
                (USER_ID,)
            )
            pref_facts = cur.fetchall()
            print(f"\n[DB] Pref_name facts found: {len(pref_facts)}")
            for row in pref_facts:
                print(f"     ID: {row[0]}, subject: {row[2]}, object: {row[3]}, confidence: {row[5]}")

            # Check entity_aliases to verify alias registration
            if spouse_facts:
                spouse_object_id = spouse_facts[0][3]  # Get object_id from first spouse fact
                print(f"\n[DB] Checking aliases for marla UUID: {spouse_object_id}")
                cur.execute(
                    "SELECT entity_id, alias, is_preferred FROM entity_aliases "
                    "WHERE user_id = %s AND entity_id = %s "
                    "ORDER BY alias",
                    (USER_ID, spouse_object_id)
                )
                aliases = cur.fetchall()
                print(f"[DB] Aliases found: {len(aliases)}")
                for entity_id, alias, is_preferred in aliases:
                    print(f"     alias: '{alias}', is_preferred: {is_preferred}")

        db.close()
    except Exception as e:
        print(f"[ERROR] Database query failed: {e}")
        return

    print()

    # Step 4: Call /query with family question
    print("=" * 80)
    print("STEP 4: Query with 'tell me about my family'")
    print("=" * 80)

    query_request = {
        "text": "tell me about my family",
        "user_id": USER_ID
    }

    try:
        resp = requests.post(f"{FAULTLINE_URL}/query", json=query_request, timeout=10)
        print(f"[QUERY] Status: {resp.status_code}")
        query_result = resp.json()

        facts = query_result.get("facts", [])
        print(f"[QUERY] Facts returned: {len(facts)}")

        for f in facts:
            subj = f.get("subject", "?")
            rel = f.get("rel_type", "?")
            obj = f.get("object", "?")
            print(f"     {subj} -{rel}-> {obj}")

        if not facts:
            print("[WARNING] No facts returned by /query!")
            print(f"[QUERY] Full response: {json.dumps(query_result, indent=2)}")
    except Exception as e:
        print(f"[ERROR] Query request failed: {e}")
        return

    print()

    # Step 5: Check Filter logs (if available)
    print("=" * 80)
    print("STEP 5: Summary and Diagnostics")
    print("=" * 80)

    print("[SUMMARY]")
    print("If you see 'No facts returned by /query!' above:")
    print("  - Check if /query endpoint is failing due to database connection")
    print("  - Check if canonical_identity is None (would skip graph traversal)")
    print("  - Check logs: docker logs <container_id> | grep 'query.db_init_failed'")
    print()
    print("If facts are returned but display names are wrong:")
    print("  - Check entity_aliases table - 'mars' should have is_preferred=true")
    print("  - Check get_preferred_name() logic in registry.py")
    print()
    print("If pref_name fact is missing from database:")
    print("  - Check /ingest logs for validation errors")
    print("  - Check if pref_name edges are being skipped")

if __name__ == "__main__":
    test_spouse_flow()
