"""
One-shot data migration: rewrite all display-name entity IDs to UUID v5 surrogates.
Run once against the live DB, then delete this script.

Usage:
    python scripts/data_migration_surrogate.py
"""
import os
import uuid
import psycopg2

DSN = os.environ.get("POSTGRES_DSN", "postgresql://faultline:faultline@192.168.40.10:5432/faultline_test")
USER_ID = "3f8e6836-72e3-43d4-bbc5-71fc8668b070"

def make_surrogate(user_id: str, name: str) -> str:
    return str(uuid.uuid5(uuid.UUID(user_id), name.lower().strip()))

# Garbage facts to hard-delete before migration
# (self-referential pref_name rows and reversed family facts)
HARD_DELETE_CONDITIONS = [
    # self-referential: subject == object
    "subject_id = object_id",
    # backwards parent_of (user is child, not parent, of daniel/david)
    "(subject_id = 'user' AND object_id IN ('daniel', 'david') AND rel_type = 'parent_of')",
]

def main():
    conn = psycopg2.connect(DSN)
    conn.autocommit = False

    try:
        with conn.cursor() as cur:

            # ── Step 0: Hard-delete garbage facts ──────────────────────────
            for condition in HARD_DELETE_CONDITIONS:
                cur.execute(
                    f"DELETE FROM facts WHERE user_id = %s AND ({condition})",
                    (USER_ID,)
                )
                print(f"Deleted garbage facts matching: {condition}, rows={cur.rowcount}")

            # ── Step 0.5: Ensure user surrogate exists in entities ────────────
            cur.execute(
                "INSERT INTO entities (id, user_id, entity_type) "
                "VALUES (%s, %s, 'person') ON CONFLICT (id, user_id) DO NOTHING",
                (USER_ID, USER_ID)
            )
            print(f"Ensured user surrogate exists in entities: {USER_ID}")

            # ── Step 1: Build surrogate map for all display-name entities ──
            cur.execute(
                "SELECT id FROM entities WHERE user_id = %s",
                (USER_ID,)
            )
            all_entity_ids = [row[0] for row in cur.fetchall()]

            # Special cases: 'user' maps to the OWUI UUID itself
            surrogate_map = {}
            for eid in all_entity_ids:
                if eid == USER_ID:
                    # Already a UUID surrogate — skip
                    surrogate_map[eid] = eid
                elif eid == "user":
                    surrogate_map[eid] = USER_ID
                else:
                    try:
                        # Check if already a UUID
                        uuid.UUID(eid)
                        surrogate_map[eid] = eid  # already surrogate
                    except ValueError:
                        surrogate_map[eid] = make_surrogate(USER_ID, eid)

            print(f"\nSurrogate map ({len(surrogate_map)} entities):")
            for k, v in sorted(surrogate_map.items()):
                print(f"  {k!r:40s} -> {v}")

            # ── Step 2: Rewrite facts.subject_id ──────────────────────────
            for old_id, new_id in surrogate_map.items():
                if old_id == new_id:
                    continue
                cur.execute(
                    "UPDATE facts SET subject_id = %s "
                    "WHERE user_id = %s AND subject_id = %s",
                    (new_id, USER_ID, old_id)
                )
                if cur.rowcount:
                    print(f"facts.subject_id: {old_id!r} -> {new_id} ({cur.rowcount} rows)")

            # ── Step 3: Rewrite facts.object_id ───────────────────────────
            for old_id, new_id in surrogate_map.items():
                if old_id == new_id:
                    continue
                cur.execute(
                    "UPDATE facts SET object_id = %s "
                    "WHERE user_id = %s AND object_id = %s",
                    (new_id, USER_ID, old_id)
                )
                if cur.rowcount:
                    print(f"facts.object_id: {old_id!r} -> {new_id} ({cur.rowcount} rows)")

            # ── Step 4: Insert all new surrogate rows into entities ─────────
            for old_id, new_id in surrogate_map.items():
                if old_id == new_id:
                    continue
                # Get entity_type from old row
                cur.execute(
                    "SELECT entity_type FROM entities WHERE user_id = %s AND id = %s",
                    (USER_ID, old_id)
                )
                row = cur.fetchone()
                entity_type = row[0] if row else "unknown"

                cur.execute(
                    "INSERT INTO entities (id, user_id, entity_type) "
                    "VALUES (%s, %s, %s) ON CONFLICT (id, user_id) DO NOTHING",
                    (new_id, USER_ID, entity_type)
                )

            # ── Step 5: Rewrite entity_aliases.entity_id ──────────────────
            for old_id, new_id in surrogate_map.items():
                if old_id == new_id:
                    continue
                cur.execute(
                    "UPDATE entity_aliases SET entity_id = %s "
                    "WHERE user_id = %s AND entity_id = %s",
                    (new_id, USER_ID, old_id)
                )
                if cur.rowcount:
                    print(f"entity_aliases.entity_id: {old_id!r} -> {new_id} ({cur.rowcount} rows)")

            # ── Step 6: Register display names as aliases in entity_aliases ─
            for old_id, new_id in surrogate_map.items():
                if old_id == new_id:
                    continue
                # Register the display name as an alias if not already present
                cur.execute(
                    "INSERT INTO entity_aliases (entity_id, user_id, alias, is_preferred) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (user_id, alias) DO NOTHING",
                    (new_id, USER_ID, old_id.lower().strip(), True)
                )

            # ── Step 7: Delete old display-name entity rows ────────────────
            for old_id, new_id in surrogate_map.items():
                if old_id == new_id:
                    continue
                cur.execute(
                    "DELETE FROM entities WHERE user_id = %s AND id = %s",
                    (USER_ID, old_id)
                )
                if cur.rowcount:
                    print(f"entities: deleted old row {old_id!r}")

            # ── Step 8: Mark all facts for re-embedding ────────────────────
            cur.execute(
                "UPDATE facts SET qdrant_synced = false WHERE user_id = %s",
                (USER_ID,)
            )
            print(f"\nMarked all facts for re-embedding: {cur.rowcount} rows")

        conn.commit()
        print("\n✅ Migration complete. Verify with /query before proceeding.")

    except Exception as e:
        conn.rollback()
        print(f"\n❌ Migration failed, rolled back: {e}")
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    main()
