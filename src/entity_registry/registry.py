import uuid
import psycopg2
import structlog

log = structlog.get_logger()


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

    def __init__(self, db_conn):
        self.db_conn = db_conn

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
        name = name.lower().strip()
        if not name:
            raise ValueError("Entity name cannot be empty")

        # Special case: 'user' resolves to the canonical user entity ID.
        # If user_id is a valid UUID, use it directly.
        # If not (e.g., test user strings), derive a deterministic UUID surrogate.
        if name == "user":
            entity_id = user_id if self._is_valid_uuid(user_id) else _make_surrogate(user_id, user_id)
            # Ensure the user entity exists
            with self.db_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO entities (id, user_id, entity_type) "
                    "VALUES (%s, %s, 'Person') "
                    "ON CONFLICT (id, user_id) DO NOTHING",
                    (entity_id, user_id),
                )
            self.db_conn.commit()
            return entity_id

        with self.db_conn.cursor() as cur:
            # Check if it's a known alias (but only if it points to a valid UUID)
            cur.execute(
                "SELECT entity_id FROM entity_aliases "
                "WHERE user_id = %s AND alias = %s",
                (user_id, name),
            )
            row = cur.fetchone()
            if row:
                entity_id = row[0]
                # Validate that entity_id is a UUID (not a corrupted string)
                # Corrupted entries should be skipped and treated as unknown
                if entity_id and (entity_id.count('-') == 4 or entity_id == 'user'):
                    log.info("entity_registry.resolve_alias_found", alias=name, entity_id=entity_id)
                    return entity_id
                # If entity_id is a string (corrupted), fall through to generate a proper UUID

            # Check if it's already a canonical UUID (exact match)
            cur.execute(
                "SELECT id FROM entities WHERE user_id = %s AND id = %s",
                (user_id, name),
            )
            row = cur.fetchone()
            if row:
                log.info("entity_registry.resolve_uuid_found", name=name)
                return row[0]

            # Unknown — generate UUID v5 surrogate and register
            surrogate = _make_surrogate(user_id, name)
            log.info("entity_registry.resolve_generating_surrogate", name=name, surrogate=surrogate)
            try:
                cur.execute(
                    "INSERT INTO entities (id, user_id, entity_type) "
                    "VALUES (%s, %s, 'unknown') "
                    "ON CONFLICT (id, user_id) DO NOTHING",
                    (surrogate, user_id),
                )
                cur.execute(
                    "INSERT INTO entity_aliases (entity_id, user_id, alias, is_preferred) "
                    "VALUES (%s, %s, %s, true) "
                    "ON CONFLICT (user_id, alias) DO UPDATE SET entity_id = EXCLUDED.entity_id, is_preferred = EXCLUDED.is_preferred",
                    (surrogate, user_id, name),
                )
                self.db_conn.commit()
                log.info("entity_registry.registered", surrogate=surrogate, alias=name, user_id=user_id)
                return surrogate
            except Exception as e:
                log.error("entity_registry.resolve_registration_failed", name=name, error=str(e))
                raise

    def register_alias(
        self,
        user_id: str,
        canonical: str,
        alias: str,
        is_preferred: bool = False,
    ) -> None:
        """
        Register an alias for a canonical entity (UUID).
        canonical is a UUID string (already lowercase).
        alias is a display name.
        If is_preferred=True, clears other preferred aliases for this entity.

        User-authoritative: if the alias already exists pointing to a corrupted
        (string) entity_id, delete it first so the correct UUID registration wins.
        """
        canonical = canonical.strip()
        alias = alias.lower().strip()

        with self.db_conn.cursor() as cur:
            # Ensure canonical entity exists
            cur.execute(
                "INSERT INTO entities (id, user_id, entity_type) "
                "VALUES (%s, %s, 'unknown') ON CONFLICT (id, user_id) DO NOTHING",
                (canonical, user_id),
            )

            if is_preferred:
                # Clear other preferred aliases for this entity
                cur.execute(
                    "UPDATE entity_aliases SET is_preferred = false "
                    "WHERE user_id = %s AND entity_id = %s AND alias != %s",
                    (user_id, canonical, alias),
                )

            cur.execute(
                "INSERT INTO entity_aliases (entity_id, user_id, alias, is_preferred) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (user_id, alias) DO UPDATE SET "
                "entity_id = EXCLUDED.entity_id, "
                "is_preferred = EXCLUDED.is_preferred",
                (canonical, user_id, alias, is_preferred),
            )
        self.db_conn.commit()
        log.info("entity_registry.alias_registered",
                 canonical=canonical, alias=alias, preferred=is_preferred)

    def get_preferred_name(self, user_id: str, canonical: str) -> str:
        """Return preferred display name for entity, or canonical if none set.

        Skips:
        - Self-referential aliases where alias == entity_id (useless entries)
        - UUID aliases (never valid display names, must be human-readable strings)
        """
        import re
        with self.db_conn.cursor() as cur:
            cur.execute(
                "SELECT alias FROM entity_aliases "
                "WHERE user_id = %s AND entity_id = %s AND is_preferred = true "
                "ORDER BY alias DESC "
                "LIMIT 10",
                (user_id, canonical),
            )
            _UUID_PATTERN = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
            for row in cur.fetchall():
                alias = row[0]
                # Skip self-referential and UUID aliases
                if alias != canonical and not _UUID_PATTERN.match(alias):
                    return alias
            # Fallback to canonical if no valid alias found.
            # Callers must handle UUID fallback (e.g., _clean_preferred_names).
            return canonical

    def get_all_aliases(self, user_id: str, entity_id: str) -> list[str]:
        """Return all display name aliases for a surrogate entity_id."""
        with self.db_conn.cursor() as cur:
            cur.execute(
                "SELECT alias FROM entity_aliases "
                "WHERE user_id = %s AND entity_id = %s",
                (user_id, entity_id),
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
