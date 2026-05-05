import uuid
import psycopg2
import structlog

log = structlog.get_logger()


def _make_surrogate(user_id: str, name: str) -> str:
    """Generate immutable UUID v5 surrogate for an entity."""
    return str(uuid.uuid5(uuid.UUID(user_id), name.lower().strip())).lower()

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

        # Special case: 'user' resolves directly to the user_id (OWUI UUID)
        if name == "user":
            return user_id

        with self.db_conn.cursor() as cur:
            # Check if it's a known alias
            cur.execute(
                "SELECT entity_id FROM entity_aliases "
                "WHERE user_id = %s AND alias = %s",
                (user_id, name),
            )
            row = cur.fetchone()
            if row:
                return row[0]

            # Check if it's already a canonical UUID (exact match)
            cur.execute(
                "SELECT id FROM entities WHERE user_id = %s AND id = %s",
                (user_id, name),
            )
            row = cur.fetchone()
            if row:
                return row[0]

            # Unknown — generate UUID v5 surrogate and register
            surrogate = _make_surrogate(user_id, name)
            cur.execute(
                "INSERT INTO entities (id, user_id, entity_type) "
                "VALUES (%s, %s, 'unknown') "
                "ON CONFLICT (id, user_id) DO NOTHING",
                (surrogate, user_id),
            )
            cur.execute(
                "INSERT INTO entity_aliases (entity_id, user_id, alias, is_preferred) "
                "VALUES (%s, %s, %s, true) "
                "ON CONFLICT (user_id, alias) DO NOTHING",
                (surrogate, user_id, name),
            )
            self.db_conn.commit()
            log.info("entity_registry.registered", surrogate=surrogate, alias=name, user_id=user_id)
            return surrogate

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
                "is_preferred = EXCLUDED.is_preferred",
                (canonical, user_id, alias, is_preferred),
            )
        self.db_conn.commit()
        log.info("entity_registry.alias_registered",
                 canonical=canonical, alias=alias, preferred=is_preferred)

    def get_preferred_name(self, user_id: str, canonical: str) -> str:
        """Return preferred display name for entity, or canonical if none set."""
        with self.db_conn.cursor() as cur:
            cur.execute(
                "SELECT alias FROM entity_aliases "
                "WHERE user_id = %s AND entity_id = %s AND is_preferred = true "
                "LIMIT 1",
                (user_id, canonical),
            )
            row = cur.fetchone()
            return row[0] if row else canonical

    def get_canonical_for_user(self, user_id: str) -> str:
        """
        Return the canonical entity ID for this user.
        The OWUI user UUID is the surrogate for the user entity.
        """
        return user_id
