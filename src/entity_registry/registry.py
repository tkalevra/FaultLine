import uuid
import psycopg2
import psycopg2.errorcodes
import structlog
from fastapi import HTTPException

log = structlog.get_logger()

# Maximum length for entity names and aliases stored in entity_aliases.alias.
# 256 chars eliminates injection payload viability (no coherent multi-sentence directive fits
# within 256 chars) while preserving all real-world personal data: full legal names with
# titles, addresses, employer names.  Truncation is used (not rejection) because legitimate
# long names are possible edge cases.  Mitigates TM-01.
_ENTITY_NAME_MAX_LEN = 256


# Stable namespace UUID for deriving surrogates when user_id is not a valid UUID
_FAULTLINE_NAMESPACE = uuid.UUID('6ba7b810-9dad-11d1-80b4-00c04fd430c8')


def _make_surrogate(user_id: str, name: str) -> str:
    """Generate deterministic UUID v5 surrogate for an entity.

    Uses user_id directly as the namespace if it is a valid UUID.
    Falls back to a UUID v5 derived from a stable namespace + user_id
    when user_id is not a valid UUID (e.g., 'anonymous').
    """
    try:
        namespace = uuid.UUID(user_id)
    except (ValueError, AttributeError):
        namespace = uuid.uuid5(_FAULTLINE_NAMESPACE, user_id)
    return str(uuid.uuid5(namespace, name.lower().strip())).lower()

class EntityRegistry:
    """
    Canonical entity store. All relationship facts must reference
    canonical entity IDs from this registry.

    Responsibilities:
    - Resolve any name/alias to its canonical entity ID
    - Register new entities
    - Store aliases and preferred names
    - Never allow aliases to appear as subject_id/object_id in facts
    """

    def __init__(self, db_conn, auto_commit=True, schema_name=None):
        self.db_conn = db_conn
        self.auto_commit = auto_commit
        self.schema_name = schema_name

    @staticmethod
    def _is_valid_uuid(value: str) -> bool:
        """Check if a string is a valid UUID."""
        try:
            uuid.UUID(value)
            return True
        except (ValueError, AttributeError):
            return False

    def resolve(self, user_id: str, name: str) -> str:
        """
        Resolve a name or alias to its canonical entity ID (UUID surrogate).
        If name is a known alias, returns the canonical ID.
        If name is already a UUID, returns it unchanged.
        If name is unknown, generates a UUID v5 surrogate and registers it.
        """
        original_name = name
        name = name.lower().strip()
        if len(name) > _ENTITY_NAME_MAX_LEN:
            log.warning(
                "entity_registry.name_truncated",
                name_length=len(name),
                user_id=user_id,
            )
            name = name[:_ENTITY_NAME_MAX_LEN]
        log.info("entity_registry.resolve_start", original_name=original_name, normalized_name=name, user_id=user_id)
        if not name:
            raise ValueError("Entity name cannot be empty")

        # Special case: 'user' resolves to the canonical user entity ID.
        # If user_id is a valid UUID, use it directly.
        # If not (e.g., test user strings), derive a deterministic UUID surrogate.
        if name == "user":
            entity_id = user_id if self._is_valid_uuid(user_id) else _make_surrogate(user_id, user_id)
            log.info("entity_registry.resolve_user_special_case", entity_id=entity_id)
            # Ensure the user entity exists (per-user schema, no user_id column needed)
            with self.db_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO entities (id, entity_type) "
                    "VALUES (%s, 'Person') "
                    "ON CONFLICT (id) DO NOTHING",
                    (entity_id,),
                )
            self.db_conn.commit()
            log.info("entity_registry.resolve_returning_user", return_value=entity_id)
            return entity_id

        with self.db_conn.cursor() as cur:
            # Check if it's a known alias (but only if it points to a valid UUID)
            cur.execute(
                "SELECT entity_id FROM entity_aliases "
                "WHERE alias = %s "
                "ORDER BY CASE WHEN entity_id = %s THEN 0 ELSE 1 END, is_preferred DESC",
                (name, user_id),
            )
            row = cur.fetchone()
            log.info("entity_registry.alias_query_executed", name=name, user_id=user_id, found=row is not None)
            if row:
                entity_id = row[0]
                log.info("entity_registry.alias_query_result", name=name, user_id=user_id, entity_id=entity_id, uuid_count=entity_id.count('-') if entity_id else 0)
                # Validate that entity_id is a UUID (not a corrupted string)
                # Corrupted entries should be skipped and treated as unknown
                if entity_id and (entity_id.count('-') == 4 or entity_id == 'user'):
                    log.info("entity_registry.resolve_alias_found", alias=name, entity_id=entity_id)
                    log.info("entity_registry.resolve_returning_alias", name=name, return_value=entity_id)
                    return entity_id
                # If entity_id is a string (corrupted), fall through to generate a proper UUID

            # Check if it's already a canonical UUID (exact match)
            # Per-user schema isolation: entities table has no user_id column; schema isolation is sufficient
            cur.execute(
                "SELECT id FROM entities WHERE id = %s",
                (name,),
            )
            row = cur.fetchone()
            log.info("entity_registry.uuid_query_executed", name=name, user_id=user_id, found=row is not None)
            if row:
                entity_id = row[0]
                # Validate that entity_id is actually a UUID (not corrupted string)
                # Corrupted entries (display name strings in id column) must be skipped
                if entity_id and (entity_id.count('-') == 4 or entity_id == 'user'):
                    log.info("entity_registry.resolve_uuid_found", name=name)
                    log.info("entity_registry.resolve_returning_uuid", name=name, return_value=entity_id)
                    return entity_id
                else:
                    # Corrupted: entity_id is a string, not a UUID - fall through to generate proper UUID
                    log.warning("entity_registry.corrupted_string_entity_id_in_entities",
                               entity_id=entity_id, name=name, user_id=user_id)

            # HARD CONSTRAINT: Reject rel_type names from being registered as entities
            # Check if this name is a known rel_type (prevents parent_of, instance_of, etc. from becoming entities)
            cur.execute(
                "SELECT rel_type FROM rel_types WHERE LOWER(rel_type) = %s",
                (name,),
            )
            rel_type_row = cur.fetchone()
            if rel_type_row:
                log.error("entity_registry.rel_type_as_entity_rejected",
                         name=name, rel_type=rel_type_row[0], user_id=user_id,
                         message="HARD CONSTRAINT: rel_type names cannot be registered as entities")
                raise ValueError(f"Cannot register rel_type '{name}' as an entity (HARD CONSTRAINT)")

            # Unknown — generate UUID v5 surrogate and register (per-user schema, no user_id column)
            surrogate = _make_surrogate(user_id, name)
            log.info("entity_registry.resolve_generating_surrogate", name=name, surrogate=surrogate, surrogate_has_dashes=surrogate.count('-'))
            try:
                # Probe for aborted transaction before attempting registration
                try:
                    cur.execute("SELECT 1")
                except Exception:
                    self.db_conn.rollback()
                    if self.schema_name:
                        cur.execute(f"SET search_path TO {self.schema_name}, public")
                cur.execute(
                    "INSERT INTO entities (id, entity_type) "
                    "VALUES (%s, 'unknown') "
                    "ON CONFLICT (id) DO NOTHING",
                    (surrogate,),
                )
                cur.execute(
                    "INSERT INTO entity_aliases (entity_id, alias, is_preferred) "
                    "VALUES (%s, %s, true) "
                    "ON CONFLICT (entity_id, alias) DO UPDATE SET is_preferred = EXCLUDED.is_preferred",
                    (surrogate, name),
                )
                self.db_conn.commit()
                log.info("entity_registry.registered", surrogate=surrogate, alias=name, user_id=user_id)
                log.info("entity_registry.resolve_returning", name=name, return_value=surrogate, is_uuid=surrogate.count('-') == 4)
                return surrogate
            except Exception as e:
                log.error("entity_registry.resolve_registration_failed", name=name, error=str(e))
                raise

    def register_alias(
        self,
        canonical: str,
        alias: str,
        is_preferred: bool = False,
        entity_type: str = 'unknown',
    ) -> None:
        """
        Register an alias for a canonical entity (UUID).
        canonical is a UUID string (already lowercase).
        alias is a display name.
        If is_preferred=True, clears other preferred aliases for this entity.
        entity_type: Entity type (e.g., "Person", "Animal", "Organization")
                    Default 'unknown' for backward compatibility

        User-authoritative: if the alias already exists pointing to a corrupted
        (string) entity_id, delete it first so the correct UUID registration wins.

        Per-user schema isolation: no user_id parameter needed (schema itself provides isolation).
        """
        canonical = canonical.strip()
        alias = alias.lower().strip()
        if len(alias) > _ENTITY_NAME_MAX_LEN:
            log.warning(
                "entity_registry.alias_truncated",
                alias_length=len(alias),
                alias_prefix=alias[:20],
            )
            alias = alias[:_ENTITY_NAME_MAX_LEN]

        # Validate entity_type against known types
        valid_types = self._get_valid_entity_types()
        if entity_type not in valid_types and entity_type != 'unknown':
            log.warning("entity_type_not_recognized",
                       entity_type=entity_type,
                       available_types=valid_types)
            # Use 'unknown' as fallback, but log the issue for re-embedder awareness
            actual_type = 'unknown'
        else:
            actual_type = entity_type

        try:
            with self.db_conn.cursor() as cur:
                # Ensure canonical entity exists with validated type (per-user schema, no user_id column)
                cur.execute(
                    "INSERT INTO entities (id, entity_type) "
                    "VALUES (%s, %s) ON CONFLICT (id) DO UPDATE "
                    "SET entity_type = EXCLUDED.entity_type WHERE entities.entity_type = 'unknown'",
                    (canonical, actual_type),
                )

                # ──────────────────────────────────────────────────────────────
                # dprompt-121: Collision Detection & Staging
                # Check if this alias is already preferred for a DIFFERENT entity
                # ──────────────────────────────────────────────────────────────
                alias_lower = alias.lower()
                collision_entity = None

                if is_preferred:
                    # Only check for collisions if we're trying to set as preferred
                    cur.execute(
                        "SELECT entity_id FROM entity_aliases "
                        "WHERE alias = %s AND is_preferred = true "
                        "AND entity_id != %s LIMIT 1",
                        (alias_lower, canonical),
                    )
                    collision_row = cur.fetchone()
                    if collision_row:
                        collision_entity = collision_row[0]

                if collision_entity:
                    # ──────────────────────────────────────────────────────────────
                    # COLLISION DETECTED: Two entities claim same preferred name
                    # Stage for LLM resolution instead of silently overwriting
                    # ──────────────────────────────────────────────────────────────
                    try:
                        # Get entity names for logging/context
                        cur.execute(
                            "SELECT alias FROM entity_aliases "
                            "WHERE entity_id = %s AND is_preferred = true LIMIT 1",
                            (collision_entity,),
                        )
                        collision_name_row = cur.fetchone()
                        collision_name = collision_name_row[0] if collision_name_row else collision_entity[:8]

                        canonical_name_row = None
                        if canonical != collision_entity:
                            cur.execute(
                                "SELECT alias FROM entity_aliases "
                                "WHERE entity_id = %s AND is_preferred = true LIMIT 1",
                                (canonical,),
                            )
                            canonical_name_row = cur.fetchone()
                        canonical_name = canonical_name_row[0] if canonical_name_row else canonical[:8]

                        # Stage collision for LLM resolution
                        # Migration 051 per-user schema: entity_id_a, entity_id_b, alias
                        cur.execute(
                            "INSERT INTO entity_name_conflicts "
                            "(entity_id_a, entity_id_b, alias, status, created_at) "
                            "VALUES (%s, %s, %s, 'pending', NOW()) "
                            "ON CONFLICT (alias) DO NOTHING",
                            (collision_entity, canonical, alias_lower),
                        )
                        self.db_conn.commit()

                        log.warning(
                            "entity_registry.name_collision_detected",
                            alias=alias,
                            entity_1_id=collision_entity[:8],
                            entity_1_name=collision_name,
                            entity_2_id=canonical[:8],
                            entity_2_name=canonical_name,
                            status="staged_for_llm_resolution"
                        )

                        # Register new entity's alias as NON-preferred (fallback)
                        # This prevents silent overwrite while collision is pending resolution
                        is_pref = False
                    except Exception as e:
                        log.error(
                            "entity_registry.collision_staging_failed",
                            alias=alias,
                            canonical=canonical,
                            collision_entity=collision_entity,
                            error=str(e)
                        )
                        # Rollback collision staging, reapply search_path (SET is rolled back too)
                        self.db_conn.rollback()
                        if self.schema_name:
                            try:
                                with self.db_conn.cursor() as _r:
                                    _r.execute(f"SET search_path TO {self.schema_name}, public")
                            except Exception:
                                pass
                        is_pref = is_preferred
                else:
                    # No collision: use requested preference
                    is_pref = is_preferred

                if is_pref:
                    # Clear other preferred aliases for this entity
                    cur.execute(
                        "UPDATE entity_aliases SET is_preferred = false "
                        "WHERE entity_id = %s AND alias != %s",
                        (canonical, alias),
                    )

                # Insert/update with proper constraint (per-user schema: unique on entity_id, alias)
                cur.execute(
                    "INSERT INTO entity_aliases (entity_id, alias, is_preferred) "
                    "VALUES (%s, %s, %s) "
                    "ON CONFLICT (entity_id, alias) DO UPDATE SET "
                    "is_preferred = EXCLUDED.is_preferred",
                    (canonical, alias, is_pref),
                )
                log.info("entity_registry.alias_registered",
                         canonical=canonical, alias=alias, preferred=is_pref)
        except psycopg2.IntegrityError as err:
            # Always rollback on error — aborted transactions must be cleared regardless of auto_commit
            try:
                self.db_conn.rollback()
                if self.schema_name:
                    with self.db_conn.cursor() as _r:
                        _r.execute(f"SET search_path TO {self.schema_name}, public")
            except Exception:
                pass
            log.error("entity_registry.alias_constraint_violation",
                     error=str(err),
                     canonical=canonical,
                     alias=alias)
            raise
        except Exception as err:
            # Always rollback on error — aborted transactions must be cleared regardless of auto_commit
            try:
                self.db_conn.rollback()
                if self.schema_name:
                    with self.db_conn.cursor() as _r:
                        _r.execute(f"SET search_path TO {self.schema_name}, public")
            except Exception:
                pass
            log.error("entity_registry.alias_registration_failed",
                     error=str(err),
                     canonical=canonical,
                     alias=alias)
            raise

    def _get_valid_entity_types(self) -> set:
        """Query all valid entity types from database (metadata-driven).

        Returns a set of valid entity types from the entities table.
        Always includes 'unknown' as a valid fallback.
        Falls back to minimal set if DB query fails.
        """
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("SELECT DISTINCT entity_type FROM entities WHERE entity_type IS NOT NULL")
                rows = cur.fetchall()
                types = {row[0] for row in rows} if rows else set()
                # Always include 'unknown' as valid fallback
                types.add('unknown')
                return types
        except Exception as err:
            # Rollback to clear any aborted transaction state before returning fallback
            try:
                self.db_conn.rollback()
            except Exception:
                pass
            log.error("failed_to_load_entity_types", error=str(err))
            return {'unknown', 'Person', 'Animal', 'Organization', 'Location', 'Concept'}

    def get_preferred_name(self, canonical: str) -> str:
        """Return preferred display name for entity, or canonical if none set.

        DUMB EXTRACT LAYER: Returns whatever is in the database without validation.
        The /query layer (_populate_preferred_names in main.py) is responsible for
        filtering bad data. Do not add validation here — validate on READ, not WRITE.

        Per-user schema isolation: user_id parameter removed (schema itself provides isolation).
        """
        with self.db_conn.cursor() as cur:
            cur.execute(
                "SELECT alias FROM entity_aliases "
                "WHERE entity_id = %s AND is_preferred = true "
                "LIMIT 1",
                (canonical,),
            )
            row = cur.fetchone()
            return row[0] if row else canonical

    def get_any_alias(self, entity_id: str) -> str | None:
        """Return first available alias for an entity (preferred or not).

        Used as fallback when get_preferred_name returns a UUID — ensures
        the entity has at least one human-readable name for display resolution.
        Returns None if no alias exists.

        Per-user schema isolation: user_id parameter removed (schema itself provides isolation).
        """
        with self.db_conn.cursor() as cur:
            cur.execute(
                "SELECT alias FROM entity_aliases "
                "WHERE entity_id = %s "
                "ORDER BY is_preferred DESC LIMIT 1",
                (entity_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def get_all_aliases(self, entity_id: str) -> list[str]:
        """Return all display name aliases for a surrogate entity_id.

        Per-user schema isolation: user_id parameter removed (schema itself provides isolation).
        """
        with self.db_conn.cursor() as cur:
            cur.execute(
                "SELECT alias FROM entity_aliases "
                "WHERE entity_id = %s",
                (entity_id,),
            )
            return [row[0] for row in cur.fetchall()]

    def get_surrogate_for_user(self, user_id: str) -> str:
        """Return the surrogate UUID for the user entity.
        If user_id is a valid UUID, returns it directly.
        Otherwise derives a deterministic UUID v5 surrogate.
        """
        if self._is_valid_uuid(user_id):
            return user_id
        return _make_surrogate(user_id, user_id)

    def get_canonical_for_user(self, user_id: str) -> str:
        """
        Return the canonical entity ID for this user.
        If user_id is a valid UUID, returns it directly.
        Otherwise derives a deterministic UUID v5 surrogate.
        """
        if self._is_valid_uuid(user_id):
            return user_id
        return _make_surrogate(user_id, user_id)