"""Schema creation and deletion for per-user database isolation.

All schema names are derived from users.slug (never hardcoded).
All metadata is copied from base schema at provisioning time (no assumptions).
"""

import os
import re
import subprocess
import structlog
import psycopg2
import psycopg2.extensions
from pathlib import Path
from typing import Tuple, Optional, Dict, Any
from urllib.parse import urlparse
from src.config.settings import settings

log = structlog.get_logger()

# UUID format validation — accepts both UUID v4 and UUID v5 (both used in FaultLine).
# Pattern: 8-4-4-4-12 lowercase hex digits separated by hyphens.
# Applied after .lower() so input case does not matter.
# Mitigates TM-03/TM-04: prevents crafted user_id strings from routing to unexpected
# PostgreSQL schemas (e.g. "pg_catalog", path-traversal attempts).
_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
)


def parse_postgres_dsn(dsn: str = None) -> Dict[str, str]:
    """Parse PostgreSQL DSN into components for psql subprocess calls.

    Args:
        dsn: PostgreSQL connection string. Falls back to POSTGRES_DSN env var.

    Returns:
        Dict with keys: user, password, host, port, database

    Raises:
        ValueError: If DSN not provided or cannot be parsed
    """
    if not dsn:
        dsn = os.environ.get("POSTGRES_DSN")

    if not dsn:
        raise ValueError("POSTGRES_DSN env var not set and no dsn parameter provided")

    try:
        parsed = urlparse(dsn)
        return {
            "user": parsed.username or "postgres",
            "password": parsed.password or "",
            "host": parsed.hostname or "localhost",
            "port": str(parsed.port or 5432),
            "database": parsed.path.lstrip("/") if parsed.path else "postgres"
        }
    except Exception as e:
        raise ValueError(f"Failed to parse POSTGRES_DSN: {str(e)}")


def get_postgres_connection(dsn: str = None):
    """Get PostgreSQL connection using POSTGRES_DSN env var or provided DSN.

    Args:
        dsn: Optional connection string. Falls back to POSTGRES_DSN env var.

    Returns:
        psycopg2 connection object

    Raises:
        ValueError: If DSN not provided and POSTGRES_DSN env var not set
    """
    if not dsn:
        dsn = os.environ.get("POSTGRES_DSN")

    if not dsn:
        raise ValueError("POSTGRES_DSN env var not set and no dsn parameter provided")

    return psycopg2.connect(dsn)


def derive_user_slug_from_uuid(user_id: str) -> str:
    """Derive URL-safe user slug from UUID (immutable).

    Converts UUID from standard format (with hyphens) to URL-safe format
    (with underscores) for PostgreSQL schema naming.

    This ensures:
    - Schema names are collision-free (full UUID space)
    - Schema names are immutable (UUID never changes)
    - Schema names are URL-safe (compatible with psql queries)

    Args:
        user_id: UUID in standard format, e.g., "00000000-0000-0000-0000-000000000000"

    Returns:
        URL-safe slug, e.g., "550e8400_e29b_41d4_a716_446655440000"

    Raises:
        ValueError: If user_id is empty or None

    Examples:
        >>> derive_user_slug_from_uuid("00000000-0000-0000-0000-000000000000")
        "550e8400_e29b_41d4_a716_446655440000"

        >>> derive_user_slug_from_uuid("00000000-0000-0000-0000-000000000000")
        "550e8400_e29b_41d4_a716_446655440000"

    CLAUDE.md Compliance:
    - Immutable: UUID never changes, schema names persist across user updates
    - Collision-free: Full UUID space (340 undecillion), not 8-char fragment
    - Metadata-driven: No hardcoded assumptions about user_id format
    """
    if not user_id:
        raise ValueError("user_id cannot be empty or None")

    # Primary gate: UUID format validation (v4 and v5 both accepted).
    # Must match ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ (lowercase).
    # Raises immediately — callers passing non-UUID strings are already broken and must not
    # silently route to malformed schema names.  Mitigates TM-03/TM-04.
    normalized_id = str(user_id).lower()
    if not _UUID_RE.match(normalized_id):
        raise ValueError(
            f"user_id must be a valid UUID format, got: {str(user_id)!r:.40}"
        )

    # Normalize: replace hyphens with underscores for URL-safe naming
    slug = normalized_id.replace("-", "_")

    # Belt-and-suspenders: log if length is unexpected (should never fire after regex gate)
    if len(slug) != 36:
        log.warning(
            "derive_user_slug_from_uuid.unexpected_length",
            user_id=user_id,
            slug_length=len(slug),
            expected=36
        )

    return slug




def execute_psql_file(file_path: Path, schema_name: str, dsn_components: Dict[str, str], timeout: int = 30) -> Tuple[bool, str]:
    """Execute SQL file via psql subprocess with proper error handling.

    Uses psql directly for bulletproof SQL parsing (handles dollar-quoted strings,
    complex triggers, etc.). Sets search_path to new schema before executing.

    Args:
        file_path: Path to SQL file to execute
        schema_name: Schema name to set search_path to
        dsn_components: Dict with user, password, host, port, database
        timeout: Command timeout in seconds

    Returns:
        Tuple of (success: bool, message: str)
    """
    if not file_path.exists():
        return False, f"File not found: {file_path}"

    try:
        # Build psql command
        psql_cmd = [
            "psql",
            "-U", dsn_components["user"],
            "-h", dsn_components["host"],
            "-p", dsn_components["port"],
            "-d", dsn_components["database"],
            "-f", str(file_path)
        ]

        # Set up environment with password (PGPASSWORD for psql)
        env = os.environ.copy()
        if dsn_components["password"]:
            env["PGPASSWORD"] = dsn_components["password"]

        # Prepend search_path command via -c flag
        # This ensures schema is set before template file executes
        psql_cmd_with_search = [
            "psql",
            "-U", dsn_components["user"],
            "-h", dsn_components["host"],
            "-p", dsn_components["port"],
            "-d", dsn_components["database"],
            "-c", f"SET search_path TO {schema_name}, public",
            "-f", str(file_path)
        ]

        # Execute psql with captured output
        result = subprocess.run(
            psql_cmd_with_search,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout
        )

        # Analyze output
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        combined = f"{stderr} {stdout}".lower()

        # Log raw output for debugging (PGPASSWORD not visible in logs)
        log.debug(f"psql_execution_output", file=str(file_path), schema=schema_name,
                 returncode=result.returncode)

        # Check for actual ERROR messages (not NOTICE or WARNING)
        # NOTICE messages are informational (e.g., "relation already exists")
        has_error_line = "error:" in combined or "fatal:" in combined or "could not" in combined

        # Only treat as fatal if we have actual errors (not just informational notices)
        has_fatal_error = has_error_line and "notice:" not in stderr.lower()[:200]


        # Check for fatal errors REGARDLESS of returncode
        if has_fatal_error:
            error_msg = stderr[:500] if stderr else stdout[:500]
            log.error(f"psql_execution_fatal_error", file=str(file_path), schema=schema_name,
                     returncode=result.returncode, stderr=error_msg)
            return False, f"psql encountered fatal error: {error_msg}"

        if result.returncode == 0:
            # Success with returncode 0 and no actual errors
            return True, "Success"
        else:
            # Failure: returncode != 0 means psql command failed
            error_msg = stderr[:500] if stderr else stdout[:500]
            log.error(f"psql_execution_failed", file=str(file_path), schema=schema_name,
                     returncode=result.returncode, stderr=error_msg)
            return False, f"psql execution failed (returncode {result.returncode}): {error_msg}"

    except subprocess.TimeoutExpired:
        msg = f"psql execution timed out after {timeout}s"
        log.error(f"psql_timeout", file=str(file_path), schema=schema_name, timeout=timeout)
        return False, msg
    except Exception as e:
        msg = f"psql execution failed: {str(e)}"
        log.error(f"psql_exception", file=str(file_path), schema=schema_name, error=str(e))
        return False, msg


def derive_schema_name(user_slug: str) -> str:
    """Derive PostgreSQL schema name from user slug (never hardcode).

    Args:
        user_slug: Human-readable slug from users.slug (e.g., "christopher")

    Returns:
        Schema name (e.g., "faultline_christopher")

    Examples:
        >>> derive_schema_name("christopher")
        'faultline_christopher'

        >>> derive_schema_name("marla")
        'faultline_marla'
    """
    # Sanitize slug: lowercase, alphanumeric + underscore only
    safe_slug = "".join(c if c.isalnum() or c == "_" else "_" for c in user_slug.lower())
    prefix = settings.SCHEMA_NAME_PREFIX
    return f"{prefix}_{safe_slug}"


def _execute_bootstrap_queries(db: psycopg2.extensions.connection, schema_name: str, user_id: str) -> bool:
    """Execute bootstrap metadata queries for a newly created user schema.

    Inserts baseline rel_types, entity_taxonomies, and negation_patterns.
    All inserts use ON CONFLICT DO NOTHING for idempotency.

    Args:
        db: psycopg2 connection (must already have search_path set to schema_name)
        schema_name: Name of the user schema (for logging)
        user_id: User UUID (for per-user table scoping)

    Returns:
        True if successful, False on error (errors are logged)
    """
    try:
        with db.cursor() as cur:
            # Bootstrap rel_types: copy from public schema (not hard-coded) to ensure natural_language templates are identical
            cur.execute("""
                INSERT INTO rel_types
                SELECT * FROM public.rel_types
                ON CONFLICT (rel_type) DO NOTHING
            """)
            log.info("bootstrapped_rel_types", schema=schema_name, note="copied from public schema")

            # NOTE: entity_taxonomies seeded separately via _seed_entity_taxonomies()
            # (Moved to dedicated function for clarity and to copy from public schema)

            # Bootstrap negation_patterns (20 linguistic patterns for retraction detection)
            # Per-user schema: no user_id needed — schema provides isolation
            cur.execute("""
                INSERT INTO negation_patterns (pattern_text, negation_type, learned_from, confidence)
                VALUES
                    ('is not', 'retraction', 'linguistic_bootstrap', 0.95),
                    ('is not a', 'retraction', 'linguistic_bootstrap', 0.95),
                    ('is not an', 'retraction', 'linguistic_bootstrap', 0.95),
                    ('no longer', 'retraction', 'linguistic_bootstrap', 0.95),
                    ('not anymore', 'retraction', 'linguistic_bootstrap', 0.95),
                    ('never', 'retraction', 'linguistic_bootstrap', 0.90),
                    ('forget', 'retraction', 'linguistic_bootstrap', 0.92),
                    ('delete', 'retraction', 'linguistic_bootstrap', 0.92),
                    ('remove', 'retraction', 'linguistic_bootstrap', 0.90),
                    ('erase', 'retraction', 'linguistic_bootstrap', 0.90),
                    ('wrong', 'correction', 'linguistic_bootstrap', 0.88),
                    ('actually', 'correction', 'linguistic_bootstrap', 0.82),
                    ('i meant', 'correction', 'linguistic_bootstrap', 0.85),
                    ('changed my mind', 'correction', 'linguistic_bootstrap', 0.90),
                    ('mistake', 'correction', 'linguistic_bootstrap', 0.88),
                    ('incorrect', 'correction', 'linguistic_bootstrap', 0.88),
                    ('typo', 'correction', 'linguistic_bootstrap', 0.80),
                    ('not true', 'retraction', 'linguistic_bootstrap', 0.92),
                    ('that is wrong', 'correction', 'linguistic_bootstrap', 0.88),
                    ('not the case', 'retraction', 'linguistic_bootstrap', 0.90)
                ON CONFLICT (pattern_text, negation_type) DO NOTHING
            """)
            log.info("bootstrapped_negation_patterns", schema=schema_name, count=20)

            # Bootstrap retraction_signals (growth engine: learned signals for intent improvement)
            # Per-user table: no user_id column (schema isolation provides scope)
            cur.execute("""
                INSERT INTO retraction_signals (signal, signal_category, language, priority)
                VALUES
                    ('forget', 'explicit', 'en', 95),
                    ('delete', 'explicit', 'en', 95),
                    ('remove', 'explicit', 'en', 90),
                    ('erase', 'explicit', 'en', 90),
                    ('is not', 'implicit_negation', 'en', 92),
                    ('no longer', 'implicit_negation', 'en', 92),
                    ('not anymore', 'implicit_negation', 'en', 90),
                    ('wrong', 'correction', 'en', 88),
                    ('actually', 'correction', 'en', 82),
                    ('i meant', 'correction', 'en', 85)
                ON CONFLICT (signal, language) DO NOTHING
            """)
            log.info("bootstrapped_retraction_signals", schema=schema_name, count=10)

            db.commit()
            return True

    except Exception as e:
        log.error("bootstrap_queries_failed", schema=schema_name, error=str(e))
        return False


def _seed_entity_taxonomies(user_id: str, schema_name: str, db: psycopg2.extensions.connection) -> bool:
    """Seed entity_taxonomies from public schema into per-user schema.

    Migration 051 creates the entity_taxonomies table in per-user schema,
    but doesn't populate it. This function copies the 5 core taxonomies from
    the public schema into the per-user schema copy.

    Core taxonomies (from migration 019):
    1. family (Person entities: spouses, children, parents)
    2. household (Person + Animal: members of a home)
    3. work (Person + Organization: employment relationships)
    4. location (Location entities: cities, addresses)
    5. computer_system (Concept + Object: tech domain)

    Idempotent: ON CONFLICT (taxonomy_name) DO NOTHING allows safe retries.
    Metadata-driven: Copies exact definitions from public schema, prevents drift.

    Args:
        user_id: User UUID (for logging)
        schema_name: Per-user schema name (e.g., faultline_abc123)
        db: PostgreSQL connection

    Returns:
        True if seeding succeeded, False otherwise
    """
    try:
        with db.cursor() as cur:
            # Set schema path for this transaction
            cur.execute(f"SET search_path TO {schema_name}, public")

            # Copy taxonomies from public schema to per-user schema
            # ON CONFLICT ensures idempotency (can be retried safely)
            cur.execute("""
                INSERT INTO entity_taxonomies
                (taxonomy_name, description, member_entity_types, rel_types_defining_group,
                 has_transitivity, transitive_rel_types, is_hierarchical, parent_rel_type, created_at)
                SELECT taxonomy_name, description, member_entity_types, rel_types_defining_group,
                       has_transitivity, transitive_rel_types, is_hierarchical, parent_rel_type, NOW()
                FROM public.entity_taxonomies
                ON CONFLICT (taxonomy_name) DO NOTHING
            """)
            db.commit()

            # Verify seeding succeeded by counting rows
            cur.execute(f"SET search_path TO {schema_name}, public")
            cur.execute("SELECT COUNT(*) FROM entity_taxonomies")
            count = cur.fetchone()[0]

            log.info(
                "seeded_entity_taxonomies",
                schema=schema_name,
                user_id=user_id[:8],
                taxonomy_count=count
            )
            return True

    except Exception as e:
        db.rollback()
        log.error(
            "seed_entity_taxonomies_failed",
            schema=schema_name,
            user_id=user_id[:8],
            error=str(e)
        )
        return False


def _update_provisioning_heartbeat(user_id: str, db: psycopg2.extensions.connection) -> bool:
    """Update heartbeat_at timestamp for a provisioning job.

    Called periodically during schema creation to signal worker is alive.

    Args:
        user_id: UUID of user being provisioned
        db: Active database connection

    Returns:
        True if heartbeat updated, False on error
    """
    try:
        with db.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute("""
                UPDATE public.user_provisioning
                SET heartbeat_at = NOW()
                WHERE user_id = %s AND status = 'provisioning'
            """, (user_id,))
            db.commit()
            return True
    except Exception as e:
        log.warning("provisioning_heartbeat_update_failed", user_id=user_id[:8], error=str(e))
        return False


def _validate_schema_structure(schema_name: str, user_id: str, db: psycopg2.extensions.connection) -> Dict[str, Any]:
    """Validate that per-user schema has all required columns and tables (FAIL LOUD per CLAUDE.md #3).

    This runs AFTER migration 051 executes to ensure schema structure is correct.
    If validation fails, create_user_schema will NOT mark status='ready'.

    Args:
        schema_name: Schema to validate (e.g., "faultline_550e8400_...")
        user_id: User UUID (for logging)
        db: psycopg2 connection (must already be open)

    Returns:
        dict: {
            'success': bool,
            'reason': str (explanation),
            'missing_tables': list[str],
            'missing_columns': list[tuple(table, column)],
            'errors': list[str] (all error details)
        }
    """
    required_columns = {
        'facts': [
            'valid_from', 'valid_until', 'fact_class', 'fact_provenance',
            'unified_confidence', 'superseded_at', 'archived_at', 'storage_type',
            'is_hierarchy_rel', 'taxonomies', 'rel_type_definition'
        ],
        'staged_facts': [
            'fact_class', 'fact_provenance', 'unified_confidence',
            'storage_type', 'is_hierarchy_rel', 'taxonomies', 'rel_type_definition'
        ],
        'entity_attributes': ['user_id', 'entity_id', 'attribute', 'value_text'],
        'entity_aliases': ['entity_id', 'alias', 'is_preferred'],
        'entities': ['id', 'entity_type'],
        'ontology_evaluations': ['candidate_rel_type'],
        'retraction_outcomes': ['user_id', 'original_message'],
    }

    required_tables = [
        'facts', 'staged_facts', 'entities', 'entity_aliases', 'entity_attributes',
        'rel_types', 'entity_taxonomies', 'ontology_evaluations', 'negation_patterns',
        'intent_confidence_feedback', 'retraction_signals', 'pending_types',
        'entity_name_conflicts', 'retraction_outcomes'
    ]

    errors = []
    missing_tables = []
    missing_columns = []

    try:
        with db.cursor() as cur:
            # Check table existence
            for table_name in required_tables:
                cur.execute("""
                    SELECT EXISTS(
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema=%s AND table_name=%s
                    )
                """, (schema_name, table_name))
                exists = cur.fetchone()[0]

                if not exists:
                    missing_tables.append(table_name)
                    errors.append(f"Table '{table_name}' missing from schema")

            # Check column existence for critical tables
            for table_name, columns in required_columns.items():
                for column_name in columns:
                    cur.execute("""
                        SELECT EXISTS(
                            SELECT 1 FROM information_schema.columns
                            WHERE table_schema=%s AND table_name=%s AND column_name=%s
                        )
                    """, (schema_name, table_name, column_name))
                    exists = cur.fetchone()[0]

                    if not exists:
                        missing_columns.append((table_name, column_name))
                        errors.append(f"Column '{column_name}' missing from table '{table_name}'")

    except Exception as e:
        errors.append(f"Validation query failed: {str(e)}")
        log.error(
            "schema_validation_exception",
            schema=schema_name,
            user_id=user_id[:8],
            error=str(e)
        )

    if errors:
        return {
            'success': False,
            'reason': f"{len(errors)} validation error(s): {errors[0]}",
            'missing_tables': missing_tables,
            'missing_columns': missing_columns,
            'errors': errors
        }

    return {
        'success': True,
        'reason': 'All required tables and columns present',
        'missing_tables': [],
        'missing_columns': [],
        'errors': []
    }


def create_user_schema(user_id: str, user_slug: str, db: Optional[psycopg2.extensions.connection] = None) -> Tuple[str, str]:
    """Create a new user schema and bootstrap metadata.

    Idempotent: Safe to call multiple times for same user.

    Args:
        user_id: UUID of user (from users.user_id)
        user_slug: Human-readable slug (from users.slug)
        db: Optional psycopg2 connection. Creates new if not provided.

    Returns:
        Tuple of (schema_name, status) where status is 'ready' or error description

    Examples:
        >>> schema_name, status = create_user_schema(
        ...     user_id="00000000-0000-0000-0000-000000000000",
        ...     user_slug="christopher"
        ... )
        >>> assert schema_name == "faultline_christopher"
        >>> assert status == "ready"
    """
    schema_name = derive_schema_name(user_slug)
    close_conn = False

    try:
        if not db:
            db = get_postgres_connection()
            close_conn = True

        # Parse DSN for psql subprocess calls
        dsn = os.environ.get("POSTGRES_DSN")
        if not dsn:
            return schema_name, "Error: POSTGRES_DSN environment variable not set"

        try:
            dsn_components = parse_postgres_dsn(dsn)
        except ValueError as e:
            return schema_name, f"Error parsing POSTGRES_DSN: {str(e)}"

        with db.cursor() as cur:
            # Create schema (idempotent)
            try:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema_name}")
                db.commit()
                log.info("created_schema", schema=schema_name, user_id=user_id[:8])
                # Update heartbeat after schema creation
                _update_provisioning_heartbeat(user_id, db)
            except Exception as e:
                db.rollback()
                return schema_name, f"Failed to create schema: {str(e)}"

            # Verify schema actually exists before proceeding
            try:
                cur.execute("""
                    SELECT 1 FROM information_schema.schemata
                    WHERE schema_name = %s
                """, (schema_name,))
                exists = cur.fetchone()
                if not exists:
                    return schema_name, f"Schema {schema_name} not found in information_schema after CREATE"
            except Exception as e:
                return schema_name, f"Failed to verify schema existence: {str(e)}"

            # Apply template schema via psql (handles dollar-quoted strings correctly)
            template_path = Path(__file__).parent.parent.parent / "migrations" / "051_template_user_schema.sql"
            if not template_path.exists():
                return schema_name, f"Template file not found: {template_path}"

            # Substitute {schema_name} placeholder in template before execution
            template_sql = template_path.read_text()
            template_sql = template_sql.replace("{schema_name}", schema_name)

            # Write substituted SQL to temp file for psql
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.sql', delete=False) as tmp:
                tmp.write(template_sql)
                tmp_path = tmp.name

            try:
                success, msg = execute_psql_file(Path(tmp_path), schema_name, dsn_components)
            finally:
                # Clean up temp file
                import os as os_module
                try:
                    os_module.unlink(tmp_path)
                except:
                    pass
            if not success:
                log.error("template_execution_failed", schema=schema_name, user_id=user_id[:8], error=msg)
                return schema_name, f"Template execution failed: {msg}"

            log.info("applied_template_schema", schema=schema_name, user_id=user_id[:8])
            # Update heartbeat after template application
            _update_provisioning_heartbeat(user_id, db)

            # FIX #2: VALIDATE SCHEMA STRUCTURE BEFORE PROCEEDING (FAIL LOUD per CLAUDE.md #3)
            # ==================================================================================
            validation_result = _validate_schema_structure(schema_name, user_id, db)
            if not validation_result['success']:
                # FAIL LOUD: Log error details, set status='failed', DO NOT mark ready
                log.critical(
                    "schema_validation_failed_critical",
                    schema=schema_name,
                    user_id=user_id[:8],
                    missing_columns=validation_result.get('missing_columns', []),
                    missing_tables=validation_result.get('missing_tables', []),
                    errors=validation_result.get('errors', [])
                )

                # Update provisioning record with error details
                try:
                    with db.cursor() as cur:
                        cur.execute("SET search_path TO public")
                        error_msg = f"Schema validation failed: {'; '.join(validation_result.get('errors', [])[:3])}"
                        cur.execute("""
                            UPDATE public.user_provisioning
                            SET status='failed', error_message=%s
                            WHERE user_id=%s
                        """, (error_msg[:500], user_id))
                        db.commit()
                except Exception as e:
                    log.error("failed_to_update_provisioning_on_validation_failure", schema=schema_name, user_id=user_id[:8], error=str(e))

                return schema_name, f"Schema validation failed: {validation_result['reason']}"

            log.info("schema_structure_validated", schema=schema_name, user_id=user_id[:8])

            # Apply bootstrap metadata directly (no longer via migration 052)
            try:
                cur.execute(f"SET search_path TO {schema_name}, public")
                if not _execute_bootstrap_queries(db, schema_name, user_id):
                    return schema_name, "Bootstrap metadata queries failed (see logs)"
                log.info("bootstrapped_metadata", schema=schema_name, user_id=user_id[:8])
                # Update heartbeat after bootstrap
                _update_provisioning_heartbeat(user_id, db)
            except Exception as e:
                log.error("bootstrap_exception", schema=schema_name, user_id=user_id[:8], error=str(e))
                return schema_name, f"Bootstrap failed: {str(e)}"

            # Seed entity_taxonomies from public schema (Phase 3.5)
            # Copies 5 core taxonomies to enable taxonomy-aware query filtering
            if not _seed_entity_taxonomies(user_id, schema_name, db):
                return schema_name, "Entity taxonomy seeding failed (see logs)"
            log.info("seeded_entity_taxonomies", schema=schema_name, user_id=user_id[:8])
            # Update heartbeat after seeding
            _update_provisioning_heartbeat(user_id, db)

            # Register user identity in entity_aliases (Phase 3.6)
            # User UUID must have a preferred display name for /query resolution.
            # Without this, /query can't resolve user_id to display_name,
            # Filter injects UUID to LLM, LLM can't ground user identity.
            try:
                cur.execute(f"SET search_path TO {schema_name}, public")
                # Insert user entity (idempotent)
                cur.execute("""
                    INSERT INTO entities (id, entity_type)
                    VALUES (%s, 'Person')
                    ON CONFLICT (id) DO NOTHING
                """, (user_id,))

                # Register user display name (user_slug) in entity_aliases
                # Maps user_id UUID → display name (e.g., christopher, marla, etc.)
                cur.execute("""
                    INSERT INTO entity_aliases (entity_id, alias, is_preferred)
                    VALUES (%s, %s, true)
                    ON CONFLICT (entity_id, alias) DO UPDATE
                    SET is_preferred = true
                """, (user_id, user_slug))
                db.commit()
                log.info("registered_user_identity", schema=schema_name, user_id=user_id[:8], display_name=user_slug)
            except Exception as e:
                db.rollback()
                log.error("user_identity_registration_failed", schema=schema_name, user_id=user_id[:8], error=str(e))
                return schema_name, f"User identity registration failed: {str(e)}"

            # Verify critical tables exist before marking ready (FIX #2: prevent schema gaps)
            # False assumption: schema exists in schemata ≠ tables exist in schema
            # Ref: COMPREHENSIVE-FIX-PROMPT.md — migration errors cause partial table creation
            try:
                cur.execute(f"SET search_path TO {schema_name}, public")
                # FIX #2: Expanded list of 9 required tables (from prior 5)
                # All tables must exist for Layer 2 pattern matching to work without tuple errors
                required_tables = [
                    'facts',
                    'staged_facts',
                    'entity_attributes',
                    'entities',
                    'entity_aliases',
                    'negation_patterns',  # FIX #2: NEW — Layer 2a pattern matching
                    'retraction_signals',  # FIX #2: NEW — Layer 2b pattern matching
                    'intent_confidence_feedback',  # FIX #2: NEW — adaptive gate learning
                    'entity_taxonomies',  # FIX #2: NEW — query filtering
                ]
                missing_tables = []
                for table_name in required_tables:
                    cur.execute("""
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = %s AND table_name = %s
                    """, (schema_name, table_name))
                    if not cur.fetchone():
                        missing_tables.append(table_name)

                if missing_tables:
                    # FIX #2: FAIL LOUD — don't mark ready if tables missing
                    log.critical("schema_provisioning.incomplete",
                            schema=schema_name,
                            user_id=user_id[:8],
                            missing_tables=missing_tables)
                    return schema_name, f"Schema verification failed: missing tables {missing_tables}"

                log.info("schema_tables_verified", schema=schema_name, user_id=user_id[:8], tables_verified=len(required_tables))
            except Exception as e:
                log.error("schema_verification_failed", schema=schema_name, user_id=user_id[:8], error=str(e))
                return schema_name, f"Schema verification failed: {str(e)}"

            # Update user_provisioning status to ready (in public schema)
            try:
                cur.execute("SET search_path TO public")
                cur.execute("""
                    UPDATE public.user_provisioning
                    SET status = 'ready', ready_at = NOW(), heartbeat_at = NOW()
                    WHERE user_id = %s
                """, (user_id,))
                db.commit()
                log.info("marked_provisioning_ready", schema=schema_name, user_id=user_id[:8])
            except Exception as e:
                db.rollback()
                log.error("failed_to_mark_ready", schema=schema_name, user_id=user_id[:8], error=str(e))
                return schema_name, f"Failed to mark provisioning as ready: {str(e)}"

            return schema_name, "ready"

    except Exception as e:
        log.error("provisioning_failed", schema=schema_name, user_id=user_id, error=str(e))

        # Try to update provisioning status with error
        try:
            with db.cursor() as cur:
                cur.execute("SET search_path TO public")
                cur.execute("""
                    UPDATE public.user_provisioning
                    SET status = 'error', error_message = %s
                    WHERE user_id = %s
                """, (str(e)[:500], user_id))
                db.commit()
        except Exception as e2:
            log.error("failed_to_update_error_status", error=str(e2))

        return schema_name, f"Error: {str(e)}"

    finally:
        if close_conn and db:
            db.close()


def delete_user_schema(user_id: str, schema_name: str, db: Optional[psycopg2.extensions.connection] = None) -> bool:
    """Delete a user schema and all its data (non-recoverable).

    Args:
        user_id: UUID of user (for logging)
        schema_name: Schema to delete (e.g., "faultline_christopher")
        db: Optional psycopg2 connection. Creates new if not provided.

    Returns:
        True if successful, False on error

    Examples:
        >>> success = delete_user_schema(
        ...     user_id="00000000-0000-0000-0000-000000000000",
        ...     schema_name="faultline_christopher"
        ... )
        >>> assert success
    """
    close_conn = False

    try:
        if not db:
            db = get_postgres_connection()
            close_conn = True

        with db.cursor() as cur:
            # Drop schema and all objects (CASCADE)
            cur.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE")
            db.commit()
            log.info(f"deleted_schema", schema=schema_name, user_id=user_id)

            # Delete provisioning record
            cur.execute("SET search_path TO public")
            cur.execute("""
                DELETE FROM user_provisioning WHERE user_id = %s
            """, (user_id,))
            db.commit()

            return True

    except Exception as e:
        log.error(f"schema_deletion_failed", schema=schema_name, user_id=user_id, error=str(e))
        return False

    finally:
        if close_conn and db:
            db.close()
