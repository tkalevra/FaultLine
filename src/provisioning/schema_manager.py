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
from src.api import errors as _errors  # THE ONE ERROR SEAM — user_provisioning.error_message is rendered by /provisioning/status
from src.api.db_read import release_read_transaction

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
        user_id: UUID in standard format, e.g., "550e8400-e29b-41d4-a716-446655440000"

    Returns:
        URL-safe slug, e.g., "550e8400_e29b_41d4_a716_446655440000"

    Raises:
        ValueError: If user_id is empty or None

    Examples:
        >>> derive_user_slug_from_uuid("550e8400-e29b-41d4-a716-446655440000")
        "550e8400_e29b_41d4_a716_446655440000"

        >>> derive_user_slug_from_uuid("550e8400-e29b-41d4-a716-446655440000")
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




def execute_psql_file(file_path: Path, schema_name: str, dsn_components: Dict[str, str], timeout: int = None) -> Tuple[bool, str]:
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

    # PROVISIONING_PSQL_TIMEOUT_S (interactive-latency): the user-schema template is a
    # ~1100-line DDL+seed file. On a loaded box it legitimately takes >30s, and the old
    # hard default made every such attempt a psql_timeout FAILURE — the queue then retried
    # the whole schema creation six times (measured 145s to provision a fresh seat on
    # pre-prod, with each attempt's DDL freezing the event loop before the worker offload).
    # 120s default: first-try completion under load; env-tunable without a rebuild.
    if timeout is None:
        timeout = int(os.environ.get("PROVISIONING_PSQL_TIMEOUT_S", "120"))

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
            "-c", f"SET search_path TO {schema_name}, public",  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query — schema_name validated by derive_user_slug_from_uuid UUID regex + derive_schema_name sanitize
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
        msg = "psql execution failed: " + _errors.public_detail(e, where="schema.psql", what=type(e).__name__)
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

        >>> derive_schema_name("alma")
        'faultline_katya'
    """
    # Sanitize slug: lowercase, alphanumeric + underscore only
    safe_slug = "".join(c if c.isalnum() or c == "_" else "_" for c in user_slug.lower())
    prefix = settings.SCHEMA_NAME_PREFIX
    return f"{prefix}_{safe_slug}"


# ── Agent Cortex (Tier-1 episodic self-healing) — firewalled per-seat schema ──────────
# The cortex is the serving-agent's OWN memory of operating this tenant's memory. It
# lives in its OWN schema `flagent_<uuid_slug>`, DISTINCT from the user-fact schema
# `faultline_<uuid_slug>`. That distinct string IS the truth firewall: the user-fact
# walk derives ONLY the `faultline_` name, so it can NEVER reference the cortex table.
# Prefix is INTENTIONALLY NOT `settings.SCHEMA_NAME_PREFIX` — it must never collide with
# the user prefix even if that setting is overridden.
CORTEX_SCHEMA_PREFIX = "flagent"


def derive_cortex_schema_name(user_id: str) -> str:
    """Derive the firewalled cortex schema name from a user UUID (never hardcode).

    Mirrors ``derive_user_slug_from_uuid`` + ``derive_schema_name`` but emits the
    ``flagent_`` prefix so the name is DISJOINT from the user-fact schema
    (``faultline_``). Raises on a non-UUID id (a bad id can never interpolate a
    schema name).

    Example:
        derive_cortex_schema_name("550e8400-e29b-41d4-a716-446655440000")
        -> 'flagent_550e8400_e29b_41d4_a716_446655440000'
        (never equal to the user schema 'faultline_550e8400_...').
    """
    slug = derive_user_slug_from_uuid(user_id)  # validates UUID; raises on bad id
    return f"{CORTEX_SCHEMA_PREFIX}_{slug}"


def _bind_cortex_seed_brain(user_id: str):
    """Bind the provisioning seat's OWN BYO engine-brain for the cortex seed (BYOM audit GAP-1).

    The seed's semantic lane (``cortex.seed_usage_patterns`` -> ``_embed_cortex_text`` ->
    ``llm_client.get_llm_chat_url()``) used to run with NO brain bound in the provisioning
    context: under SAAS every seed embed was refused (``saas.global_brain_refused
    account_id=(unresolved)``) and even a PAID tenant's cortex seed landed keyword-only.
    ``user_id`` is in hand here, so bind THAT user's brain — the provisioning twin of the
    backend request seam (``main.py::_saas_bind_tenant_brain`` -> ``bind_request_brain``).

    Discipline mirrors ``src/re_embedder/embedder.py::_reembedder_bind_tenant_brain``
    (post-BUG-12): fail-safe (never raise into provisioning) but never silent — the exact
    saas-absent OSS miss stays silent, every other bind failure and every brainless resolution
    warns LOUDLY with a note that the seed proceeds keyword-only. Returns the ContextVar token
    to clear (None = nothing to clear).
    """
    try:
        from saas.llm_context import bind_request_brain
        _token = bind_request_brain(user_id)
    except ModuleNotFoundError as _missing:
        # THE OSS NO-OP STATE, NOT A FAILURE (same ruling as embedder): the public core has no
        # saas package at all. Exactly that one miss stays silent; a None-in-sys.modules halt or
        # a missing TRANSITIVE dependency renders a DIFFERENT message and falls through loud.
        if str(_missing) == "No module named 'saas'":
            return None
        log.warning("provisioning.tenant_brain_seed_bind_failed",
                    user_id=user_id[:8], error=type(_missing).__name__,
                    detail=str(_missing)[:120],
                    note="cortex seed proceeds keyword-only (embed refused fail-closed)")
        return None
    except Exception as _bind_err:  # noqa: BLE001 — fail-safe (seed never blocks), but LOUD
        log.warning("provisioning.tenant_brain_seed_bind_failed",
                    user_id=user_id[:8], error=type(_bind_err).__name__,
                    detail=str(_bind_err)[:120],
                    note="cortex seed proceeds keyword-only (embed refused fail-closed)")
        return None
    # Read the bind back the way an LLM call would see it and say so: bind_request_brain binds
    # NOTHING for a user with no owning ACTIVE account, and the no-brain sentinel for an account
    # without a routable brain — in both states the seed stays keyword-only. SAAS off = the
    # byte-for-byte OSS no-op (nothing was bound, nothing to say).
    try:
        from saas.config import saas_mode_enabled
        if saas_mode_enabled():
            from saas.llm_context import current_llm_config
            _cfg = current_llm_config()
            if _cfg is None:
                log.warning("provisioning.tenant_brain_seed_bind_unresolved",
                            user_id=user_id[:8],
                            note="no owning ACTIVE account resolved - seed proceeds keyword-only")
            elif _cfg.get("no_brain") or not _cfg.get("base_url"):
                log.warning("provisioning.tenant_brain_seed_bind_no_routable_brain",
                            user_id=user_id[:8], account=_cfg.get("account_id") or "-",
                            reason=_cfg.get("no_brain_reason") or "no_base_url",
                            note="seed proceeds keyword-only (embed fail-closed, never a global)")
    except Exception:  # noqa: BLE001 — an observer must never break the bind it reports on
        pass
    return _token


def _clear_cortex_seed_brain(token) -> None:
    """Teardown twin of ``_bind_cortex_seed_brain`` (mirrors
    ``src/re_embedder/embedder.py::_reembedder_clear_tenant_brain``): the seed's brain must
    never outlive the seat it was bound for. No-op on a None token (OSS / SAAS off / failed
    bind — in each, nothing was bound, so nothing may be cleared).

    ⚠️ SAME-CONTEXT discipline: bind (token set) and this reset MUST stay in ONE contextvars
    context — the provisioning worker runs the whole batch in one copied context and the lazy
    in-request caller (``cortex._ensure_cortex_schema`` -> ``create_cortex_schema``) is one
    request task, so the token reset restores the PRIOR value (unset, or the request's own
    bind) exactly. Moving this clear across a thread/task boundary makes ``ContextVar.reset``
    raise ValueError (swallowed below) and STRANDS the bound brain — the cross-seat leak the
    finally exists to prevent."""
    if token is None:
        return
    try:
        from saas.llm_context import reset_llm_config
        reset_llm_config(token)
    except Exception:  # noqa: BLE001 — teardown must never raise into provisioning
        pass


def create_cortex_schema(user_id: str, db: Optional[psycopg2.extensions.connection] = None) -> Tuple[str, str]:
    """Create the per-seat cortex schema ``flagent_<uuid>`` and its one table.

    Idempotent (``CREATE SCHEMA/TABLE IF NOT EXISTS``). Uses its OWN template
    ``templates/cortex_schema.sql`` (NOT the user template). Best-effort: a failure
    here NEVER blocks user provisioning — cortex writes are fail-safe until the schema
    exists (they simply no-op-log until then).

    Returns (schema_name, status) where status is 'ready' or an error description.
    """
    schema_name = derive_cortex_schema_name(user_id)  # raises on non-UUID
    close_conn = False
    try:
        if not db:
            db = get_postgres_connection()
            close_conn = True

        template_path = Path(__file__).parent / "templates" / "cortex_schema.sql"
        if not template_path.exists():
            return schema_name, f"Cortex template not found: {template_path}"
        template_sql = template_path.read_text().replace("{schema_name}", schema_name)

        with db.cursor() as cur:
            # Create the FIREWALLED schema, then apply the template DDL under it.
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema_name}")  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.formatted-sql-query.formatted-sql-query schema_name derived from UUID via derive_cortex_schema_name which calls derive_user_slug_from_uuid (UUID regex validated)
            # Bind the cortex schema WITHOUT public — the DDL is fully schema-qualified,
            # but binding keeps any unqualified object creation isolated too.
            cur.execute(f"SET search_path TO {schema_name}")  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.formatted-sql-query.formatted-sql-query validated cortex schema name from UUID source
            cur.execute(template_sql)
        db.commit()
        log.info("created_cortex_schema", schema=schema_name, user_id=user_id[:8])
        # Seed the HOW-YOU-OPERATE usage-pattern set into THIS tenant's cortex schema
        # (per-tenant seed rail — `public` is a read-only template, never read at runtime).
        # Idempotent + best-effort: a seed failure NEVER blocks provisioning (opens its own
        # flagent_<uuid> connection; the table is already committed above).
        # BYOM GAP-1: bind the provisioning seat's OWN brain for the seed (its semantic lane
        # embeds via llm_client.get_llm_chat_url, which refuses under SAAS with no brain bound),
        # then clear it — the brain must never outlive this seat's seed step.
        _seed_brain_token = _bind_cortex_seed_brain(user_id)
        try:
            from src.api.cortex import seed_usage_patterns
            _seeded = seed_usage_patterns(user_id)
            log.info("seeded_cortex_usage_patterns", user_id=user_id[:8], inserted=_seeded)
        except Exception as _seed_e:
            log.warning("seed_cortex_usage_patterns_failed_nonfatal",
                        user_id=user_id[:8], error=str(_seed_e)[:120])
        finally:
            _clear_cortex_seed_brain(_seed_brain_token)
        # Per-seat cortex Qdrant collection (`flagent-<uuid>`, DISTINCT from the user's
        # `faultline-<uuid>`) — the semantic recall lane's HNSW index. Best-effort +
        # fail-safe: a Qdrant outage NEVER blocks provisioning; the cortex write path
        # re-ensures the collection lazily on first /iremember.
        try:
            from src.api.cortex import ensure_cortex_collection_for
            _cx_coll_ok = ensure_cortex_collection_for(user_id)
            log.info("created_cortex_collection", user_id=user_id[:8], ok=bool(_cx_coll_ok))
        except Exception as _cx_e:
            log.warning("create_cortex_collection_failed_nonfatal",
                        user_id=user_id[:8], error=str(_cx_e)[:120])
        return schema_name, "ready"
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        log.error("create_cortex_schema_failed", schema=schema_name,
                  user_id=user_id[:8], error=str(e))
        return schema_name, "Cortex schema creation failed: " + _errors.public_detail(e, where="schema.cortex", what=type(e).__name__)
    finally:
        if close_conn and db:
            try:
                db.close()
            except Exception:
                pass


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
            # Bootstrap rel_types: copy from public schema (not hard-coded) to ensure natural_language templates are identical.
            # DO UPDATE propagates critical metadata columns so per-user schemas stay in sync
            # with public schema constraint fixes (e.g., migration 075 head_types/tail_types).
            # Columns NOT overwritten: source, engine_generated — those may differ per-schema
            # for user-created rel_types.
            cur.execute("""
                INSERT INTO rel_types
                SELECT * FROM public.rel_types
                ON CONFLICT (rel_type) DO UPDATE SET
                    head_types = EXCLUDED.head_types,
                    tail_types = EXCLUDED.tail_types,
                    is_symmetric = EXCLUDED.is_symmetric,
                    inverse_rel_type = EXCLUDED.inverse_rel_type,
                    is_hierarchy_rel = EXCLUDED.is_hierarchy_rel,
                    is_leaf_only = EXCLUDED.is_leaf_only,
                    allows_leaf_rels = EXCLUDED.allows_leaf_rels,
                    category = EXCLUDED.category,
                    fact_class = EXCLUDED.fact_class,
                    natural_language = EXCLUDED.natural_language,
                    natural_language_2p = EXCLUDED.natural_language_2p,
                    correction_behavior = EXCLUDED.correction_behavior,
                    label = EXCLUDED.label,
                    wikidata_pid = EXCLUDED.wikidata_pid,
                    storage_target = EXCLUDED.storage_target,
                    temporal_class = EXCLUDED.temporal_class
            """)
            log.info("bootstrapped_rel_types", schema=schema_name, note="copied from public schema")

            # Bootstrap rel_type_aliases: copy from public TEMPLATE into the tenant schema.
            # Maps relationship variations AND role-nouns (mother→parent_of, boss→works_for)
            # to canonical rel_types. Read UNQUALIFIED at runtime on the tenant search_path
            # (no public) by possessive-relationship anchor resolution. public is the SEED
            # SOURCE only. CREATE TABLE IF NOT EXISTS defends older tenant schemas whose
            # template predates this table; explicit columns keep the copy aligned with
            # migrations 030 + 031. ON CONFLICT (alias) DO NOTHING — idempotent/re-runnable.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rel_type_aliases (
                    id SERIAL PRIMARY KEY,
                    canonical_rel_type VARCHAR(255) NOT NULL,
                    alias VARCHAR(255) NOT NULL UNIQUE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    source VARCHAR(50) DEFAULT 'ontology',
                    confidence FLOAT DEFAULT 1.0,
                    requires_inversion BOOLEAN DEFAULT FALSE,
                    is_symmetric BOOLEAN DEFAULT FALSE,
                    inverse_alias VARCHAR(255),
                    FOREIGN KEY (canonical_rel_type) REFERENCES rel_types(rel_type) ON DELETE CASCADE
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_rel_type_aliases_alias ON rel_type_aliases(alias)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_rel_type_aliases_canonical "
                "ON rel_type_aliases(canonical_rel_type)"
            )
            cur.execute("""
                INSERT INTO rel_type_aliases
                    (canonical_rel_type, alias, source, confidence,
                     requires_inversion, is_symmetric, inverse_alias)
                SELECT canonical_rel_type, alias, source, confidence,
                       requires_inversion, is_symmetric, inverse_alias
                FROM public.rel_type_aliases
                WHERE canonical_rel_type IN (SELECT rel_type FROM rel_types)
                ON CONFLICT (alias) DO NOTHING
            """)
            log.info("bootstrapped_rel_type_aliases", schema=schema_name, note="copied from public template")

            # Bootstrap the SENSE LAYER (migration 323, LANGUAGE-STANDARDS-SPEC.md §2):
            # copy the seed senses + the citable VerbNet class inventory + the
            # sense-scoped VerbNet bindings from the public TEMPLATE into the tenant
            # schema. public is the SEED SOURCE only, never read at runtime (the
            # rel_type_aliases discipline). The tables themselves come from the template
            # (which MUST precede rel_types — its sense_id carries the inline FK); the
            # to_regclass guards skip an older template rather than fail provisioning,
            # mirroring migration 320's fan. Serial ids are per-schema: the bindings map
            # through the NATURAL keys (sense_key / class_id), never the ids.
            cur.execute(
                "SELECT to_regclass('public.senses') IS NOT NULL "
                "AND to_regclass('public.verbnet_classes') IS NOT NULL "
                "AND to_regclass('public.sense_verbnet_bindings') IS NOT NULL "
                "AND to_regclass(%s) IS NOT NULL "
                "AND to_regclass(%s) IS NOT NULL "
                "AND to_regclass(%s) IS NOT NULL",
                (f"{schema_name}.senses", f"{schema_name}.verbnet_classes",
                 f"{schema_name}.sense_verbnet_bindings"),
            )
            if cur.fetchone()[0]:
                cur.execute("""
                    INSERT INTO senses
                        (sense_key, lemma, pos, sense_class, definition, standard_ref,
                         discriminators, default_sense, source)
                    SELECT sense_key, lemma, pos, sense_class, definition, standard_ref,
                           discriminators, default_sense, source
                    FROM public.senses
                    WHERE source = 'seed'
                    ON CONFLICT (sense_key) DO NOTHING
                """)
                cur.execute("""
                    INSERT INTO verbnet_classes
                        (class_id, label, members, lexicon_ref, source)
                    SELECT class_id, label, members, lexicon_ref, source
                    FROM public.verbnet_classes
                    WHERE source = 'seed'
                    ON CONFLICT (class_id) DO NOTHING
                """)
                cur.execute("""
                    INSERT INTO sense_verbnet_bindings
                        (sense_id, verbnet_class_id, source)
                    SELECT ls.id, lv.id, b.source
                    FROM public.sense_verbnet_bindings b
                    JOIN public.senses s ON s.id = b.sense_id
                    JOIN public.verbnet_classes v ON v.id = b.verbnet_class_id
                    JOIN senses ls ON ls.sense_key = s.sense_key
                    JOIN verbnet_classes lv ON lv.class_id = v.class_id
                    ON CONFLICT (sense_id, verbnet_class_id) DO NOTHING
                """)
                log.info("bootstrapped_sense_layer", schema=schema_name,
                         note="copied from public template (migration 323 seeds)")

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

            # Bootstrap correction_patterns (pre-GLiNER2 intent detection for negation-correction phrases)
            # These patterns fire BEFORE GLiNER2 to catch sentences GLiNER2 misclassifies as QUERY.
            # Patterns are subject-agnostic regex — they detect sentence structure, not entity names.
            cur.execute("""
                INSERT INTO correction_patterns (pattern_text, confidence)
                VALUES
                    ('is .+, not (a |an )?', 0.9),
                    ('is (actually|really) .+ not', 0.9),
                    ('does not have', 0.85),
                    ('is not (a |an )', 0.9)
                ON CONFLICT (pattern_text) DO NOTHING
            """)
            log.info("bootstrapped_correction_patterns", schema=schema_name, count=4)

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

            # Bootstrap deterministic extraction/intent/preference/correction layer.
            # These 5 data-bearing tables are read UNQUALIFIED on the request-scoped
            # connection (search_path = {schema} WITHOUT public, per 31580f6), so they
            # MUST be seeded INTO the tenant schema. public is the seed source only —
            # never read at runtime. Explicit column lists (NOT SELECT *) so a future
            # public column add can't silently mis-align the copy. ON CONFLICT DO NOTHING
            # keeps this idempotent and re-runnable on existing schemas.

            # extraction_patterns (~53 rows): metadata-driven regex extraction patterns
            cur.execute("""
                INSERT INTO extraction_patterns
                    (pattern_regex, rel_type, frequency, confirmed_count, rejected_count,
                     correction_count, global_confidence, description, example_text,
                     category, source, is_active, archived_at, last_matched_at)
                SELECT pattern_regex, rel_type, frequency, confirmed_count, rejected_count,
                       correction_count, global_confidence, description, example_text,
                       category, source, is_active, archived_at, last_matched_at
                FROM public.extraction_patterns
                ON CONFLICT (pattern_regex, rel_type) DO NOTHING
            """)

            # temporal_patterns (~50 rows): metadata-driven GROWABLE date-cue inventory — relative
            # cues (migration 103) + the FORMAL-ABSOLUTE class (migration 104: month names / numeric
            # shapes / 4-digit year). Read via temporal_pattern_overlay by linguistics
            # _classify_span_anchor (relative classify) AND text_has_date_cue (the latency GATE that
            # skips the whole date pipeline on a no-cue turn). Blanket SELECT copies BOTH classes.
            cur.execute("""
                INSERT INTO temporal_patterns
                    (pattern_regex, anchor_type, frequency, confirmed_count, rejected_count,
                     correction_count, global_confidence, description, example_text,
                     category, source, is_active, archived_at, last_matched_at)
                SELECT pattern_regex, anchor_type, frequency, confirmed_count, rejected_count,
                       correction_count, global_confidence, description, example_text,
                       category, source, is_active, archived_at, last_matched_at
                FROM public.temporal_patterns
                ON CONFLICT (pattern_regex, anchor_type) DO NOTHING
            """)

            # linguistic_cues (~28 rows): metadata-driven GROWABLE linguistic verb/particle cue
            # inventory read via linguistic_cue_overlay. Categories: 'naming_verb' (migration 105,
            # analyze_naming / _event_title / is_naming_predicate), 'lvc_support_verb' (migration 108,
            # analyze_event / analyze_svo_relations — have/go/attend/…), 'svo_particle' (migration
            # 108, _svo_predicate_token / _svo_object_head — to/for/with/…), 'inchoative_verb'
            # (migration 112, analyze_inchoative — start/begin/… ingressive START verbs), and
            # 'aspectual_control_verb' (migration 113, _aspectual_activity_xcomp — start/begin/keep/
            # continue/resume/finish/stop phase verbs licensing the split-SVO xcomp descent), and
            # 'employment_verb' (migration 125, derive_sentence_facts._chain_employment — work/serve/
            # act/employ/hire/… gating the "<subj> <verb> as <role> [at|for <org>]" construction), and
            # 'relocation_verb' (migration 167, derive_sentence_facts._chain_relocation — move/relocate/
            # resettle/emigrate/… gating "<person> <verb> to <place>" → lives_in state change). The
            # relations stay in code; only the verb-LEMMA / particle-SURFACE VOCABULARY is DB data that grows
            # (freq-gated, tenant-only). Copies the seeded GRAMMAR/UNIT/KINSHIP categories.
            #
            # CARVE-OUT (lean-seed): the DOMAIN-FLAVORED classes problem_noun / thin_type are NOT seeded —
            # they are GROWN PER-TENANT from observed constructions (re_embedder
            # grow_linguistic_cue_candidates). The WHERE excludes them as defense-in-depth so a NEW tenant
            # never inherits them even if a stray seed row reappears in public (migration 123 also clears
            # public). KINSHIP (kinship_noun/kinship_gender) + grammar/unit classes REMAIN seeded.
            #
            # ⚠️ AMENDED 2026-08-27 (migration 272): `social_role` is NO LONGER carved out. It measured
            # n=0 on a fresh seat, which left the possessed-person-role rail INERT on every virgin
            # tenant — "My colleague works late." emitted (user, owns, colleague), a PERSON filed as an
            # OWNED OBJECT. It is not domain-flavored: colleague/coworker/teammate/roommate is
            # CLOSED-CLASS English structure, the same category as the already-seeded kinship_noun, and
            # the inventory is WordNet-derived (DEV/derivations/social_role_from_wordnet.py). Per the
            # owner's ruling, engine scaffolding is seeded and grown, never gated behind confirmations.
            # This carve-out is defense-in-depth ONLY — leaving social_role in the list would silently
            # starve every NEW tenant of the class migration 272 seeds into public.
            cur.execute("""
                INSERT INTO linguistic_cues
                    (cue, category, frequency, confirmed_count, rejected_count,
                     correction_count, global_confidence, description, example_text,
                     source, is_active, archived_at, last_matched_at)
                SELECT cue, category, frequency, confirmed_count, rejected_count,
                       correction_count, global_confidence, description, example_text,
                       source, is_active, archived_at, last_matched_at
                FROM public.linguistic_cues
                WHERE category NOT IN ('problem_noun', 'thin_type')
                ON CONFLICT (cue, category) DO NOTHING
            """)

            # intent_classes (4 rows): GLiNER2 zero-shot intent label descriptions
            cur.execute("""
                INSERT INTO intent_classes
                    (intent_name, description, priority, version, is_active, refined_by)
                SELECT intent_name, description, priority, version, is_active, refined_by
                FROM public.intent_classes
                ON CONFLICT (intent_name) DO NOTHING
            """)

            # preference_patterns (8 rows): Layer 2c preference signal patterns
            cur.execute("""
                INSERT INTO preference_patterns
                    (pattern_text, signal_type, intent_name, base_confidence, is_active, created_by)
                SELECT pattern_text, signal_type, intent_name, base_confidence, is_active, created_by
                FROM public.preference_patterns
                ON CONFLICT (pattern_text) DO NOTHING
            """)

            # correction_signals (~12 rows): implicit correction-signal patterns
            cur.execute("""
                INSERT INTO correction_signals
                    (pattern, pattern_type, applicable_rel_types, priority, confidence,
                     category, example_usage, notes, user_id, success_count, last_applied_at,
                     extraction_hints, seed_confidence, semantics, occurrence_count)
                SELECT pattern, pattern_type, applicable_rel_types, priority, confidence,
                       category, example_usage, notes, user_id, success_count, last_applied_at,
                       extraction_hints, seed_confidence, semantics, occurrence_count
                FROM public.correction_signals
                ON CONFLICT (pattern) DO NOTHING
            """)

            # intent_pattern_cache (~15 rows): Layer 2a TTL intent-pattern cache (global seeds)
            cur.execute("""
                INSERT INTO intent_pattern_cache
                    (user_id, pattern_text, intent_type, negation_type, confidence,
                     confirmed_count, contradicted_count, last_fired_at, expires_at,
                     is_permanent, min_context_chars, requires_replacement_clause, learned_from)
                SELECT user_id, pattern_text, intent_type, negation_type, confidence,
                       confirmed_count, contradicted_count, last_fired_at, expires_at,
                       is_permanent, min_context_chars, requires_replacement_clause, learned_from
                FROM public.intent_pattern_cache
                ON CONFLICT (user_id, pattern_text, intent_type) DO NOTHING
            """)
            log.info("bootstrapped_deterministic_layer", schema=schema_name,
                     tables="extraction_patterns,temporal_patterns,linguistic_cues,intent_classes,preference_patterns,correction_signals,intent_pattern_cache")

            # Seed user identity anchor — allows first-person references from GLiNER2
            # (subject="user") to normalize to the user UUID immediately on first ingest.
            # Overwritable: when user later says "my name is Chris", pref_name updates the alias.
            cur.execute("""
                INSERT INTO entities (id, entity_type)
                VALUES (%s, 'Person')
                ON CONFLICT (id) DO NOTHING
            """, (user_id,))
            # PLACEHOLDER PROVENANCE (identity-honesty, ALIAS-PROVENANCE-DESIGN provisioned):
            # the 'user' alias is a PROVISIONING PLACEHOLDER nobody ever chose, so it must be
            # stamped preference_source='provisioned' (rank 1) — NOT left at the column default
            # 'unspecified' (rank 0). Measured on the owner's demo seat: the unstamped row was
            # accepted by every read-time guard that checks preference_source != 'provisioned'
            # and by _is_surfaceable_user_name ('user' is not a stopword), so recall rendered the
            # literal placeholder FOREVER — and a user-stated name (rank 5) that did land had to
            # fight a phantom unspecified incumbent instead of an honestly-labelled placeholder.
            # Stamping at the SOURCE keeps every consumer's existing provisioned-refusal logic
            # (query display gate, fallback SQL, re_embedder suspect flag) finally reachable.
            # The ON CONFLICT arm deliberately does NOT clobber an existing preference_source —
            # a re-run of bootstrap must never demote a name the user already stated.
            cur.execute("""
                INSERT INTO entity_aliases (entity_id, alias, is_preferred, preference_source)
                VALUES (%s, 'user', TRUE, 'provisioned')
                ON CONFLICT (entity_id, alias) DO UPDATE
                SET is_preferred = TRUE,
                    preference_source = CASE
                        WHEN entity_aliases.preference_source = 'unspecified'
                        THEN 'provisioned'
                        ELSE entity_aliases.preference_source END
            """, (user_id,))
            log.info("bootstrapped_user_anchor", schema=schema_name, user_id=user_id)

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
            cur.execute(f"SET search_path TO {schema_name}, public")  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.formatted-sql-query.formatted-sql-query schema_name from derive_schema_name(UUID) validated
            # Copy taxonomies from public schema to per-user schema
            # ON CONFLICT ensures idempotency (can be retried safely)
            # Phase 2 (2C): carry `source` so seeded vs user-corrected scope rows
            # are distinguishable in the tenant schema (Phase 3 corrections rely
            # on it). The tenant `source` column is guaranteed present by the
            # template DDL (ADD COLUMN IF NOT EXISTS).
            # member_taxonomies (migration 087): nesting refs (family ⊃ pets ⊃ animal).
            # Copied from public so the animal/pets demo groups and any nested rows land
            # in the tenant. The tenant column is guaranteed present by the template DDL
            # (ADD COLUMN IF NOT EXISTS).
            cur.execute("""
                INSERT INTO entity_taxonomies
                (taxonomy_name, description, member_entity_types, rel_types_defining_group,
                 has_transitivity, transitive_rel_types, is_hierarchical, parent_rel_type,
                 member_taxonomies, severed_taxonomies, source, created_at)
                SELECT taxonomy_name, description, member_entity_types, rel_types_defining_group,
                       has_transitivity, transitive_rel_types, is_hierarchical, parent_rel_type,
                       COALESCE(member_taxonomies, '{}'),
                       COALESCE(severed_taxonomies, '{}'),
                       COALESCE(source, 'seeded'), NOW()
                FROM public.entity_taxonomies
                ON CONFLICT (taxonomy_name) DO NOTHING
            """)

            # Guarded family nesting (migration 087): make family hierarchical and nest
            # pets under it, without clobbering a user-corrected row. The INSERT above is
            # ON CONFLICT DO NOTHING, so a pre-existing flat `family` would not pick up the
            # nesting from the public copy — wire it explicitly here.
            cur.execute("""
                UPDATE entity_taxonomies
                   SET member_taxonomies = ARRAY['pets']::TEXT[],
                       is_hierarchical   = true
                 WHERE taxonomy_name = 'family'
                   AND COALESCE(source, 'seeded') <> 'user_corrected'
                   AND (member_taxonomies IS NULL OR member_taxonomies = '{}')
            """)
            db.commit()

            # Verify seeding succeeded by counting rows
            cur.execute(f"SET search_path TO {schema_name}, public")  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.formatted-sql-query.formatted-sql-query validated schema name from UUID source
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
        'entity_name_conflicts', 'retraction_outcomes', 'entity_synonyms'
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
        # ``errors`` becomes the row's error_message (validation-failure arm) → rendered.
        errors.append("Validation query failed: " + _errors.public_detail(e, where="schema.validate", what=type(e).__name__))
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


def _flush_user_idempotency_cache(user_id: str) -> None:
    """Flush Redis idempotency keys that reference this user_id.

    Called once during schema provisioning — NOT on every request.
    Idempotency keys encode user_id in the hash input, so we can't
    selectively delete by user.  SCAN for the `idempotent:` prefix
    and delete all; the 1-hour TTL means the set is small.
    Best-effort: Redis unavailability must not block provisioning.
    """
    try:
        redis_url = os.environ.get("REDIS_URL", "redis://faultline-redis:6379/0")
        import redis as _redis
        r = _redis.from_url(redis_url, decode_responses=True)
        r.ping()

        deleted = 0
        for prefix in ("idempotent:", "lock:"):
            cursor = 0
            while True:
                cursor, keys = r.scan(cursor, match=f"{prefix}*", count=100)
                if keys:
                    r.delete(*keys)
                    deleted += len(keys)
                if cursor == 0:
                    break

        log.info("provisioning.redis_idempotency_flushed",
                 user_id=user_id[:8], keys_deleted=deleted)
    except Exception as e:
        log.warning("provisioning.redis_flush_failed",
                    user_id=user_id[:8], error=str(e))


def seed_tenant_metadata(db: psycopg2.extensions.connection, schema_name: str, user_id: str,
                         user_slug: str, *, after_stage=None) -> Optional[str]:
    """Seed EVERYTHING a tenant schema needs to be a usable seat, from the public template —
    the single seeding source. Returns ``None`` on success or the provisioning failure message.

    Three stages, exactly the ones ``create_user_schema`` has always run in this order:
      1. ``_execute_bootstrap_queries`` — rel_types (upsert), rel_type_aliases, negation/correction
         patterns, retraction signals, extraction/temporal patterns, linguistic_cues, intent
         classes, preference patterns, correction_signals, intent_pattern_cache, the seat's own
         ``entities`` row + preferred ``'user'`` alias (all ON CONFLICT — idempotent).
      2. ``_seed_entity_taxonomies`` — the groupings, with the guarded family ⊃ pets nesting.
      3. the seat's slug as a NON-preferred technical alias.

    ``after_stage`` (optional) runs after each successful stage — provisioning passes its
    heartbeat. Callers other than provisioning (the bench sandbox reset, which TRUNCATEs the
    seat's tables between claims) call this so the seat comes out of a reset carrying the SAME
    rows a freshly provisioned tenant carries, by construction: a table added to the seed here
    is re-seeded there without anyone having to remember it. The connection's search_path is
    bound to ``<schema>, public`` for the duration (the copies read ``public.*`` qualified and
    write the tenant's tables unqualified, as they always have).
    """
    with db.cursor() as cur:
        # Apply bootstrap metadata directly (no longer via migration 052)
        try:
            cur.execute(f"SET search_path TO {schema_name}, public")  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.formatted-sql-query.formatted-sql-query validated schema name from UUID source
            if not _execute_bootstrap_queries(db, schema_name, user_id):
                return "Bootstrap metadata queries failed (see logs)"
            log.info("bootstrapped_metadata", schema=schema_name, user_id=user_id[:8])
            if after_stage:
                after_stage()
        except Exception as e:
            log.error("bootstrap_exception", schema=schema_name, user_id=user_id[:8], error=str(e))
            return "Bootstrap failed: " + _errors.public_detail(e, where="schema.bootstrap", what=type(e).__name__)

        # Seed entity_taxonomies from public schema (Phase 3.5)
        # Copies 5 core taxonomies to enable taxonomy-aware query filtering
        if not _seed_entity_taxonomies(user_id, schema_name, db):
            return "Entity taxonomy seeding failed (see logs)"
        log.info("seeded_entity_taxonomies", schema=schema_name, user_id=user_id[:8])
        if after_stage:
            after_stage()

        # Register user identity in entity_aliases (Phase 3.6)
        # User UUID must have a preferred display name for /query resolution.
        # Without this, /query can't resolve user_id to display_name,
        # Filter injects UUID to LLM, LLM can't ground user identity.
        try:
            cur.execute(f"SET search_path TO {schema_name}, public")  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.formatted-sql-query.formatted-sql-query validated schema name from UUID source
            # Insert user entity (idempotent)
            cur.execute("""
                INSERT INTO entities (id, entity_type)
                VALUES (%s, 'Person')
                ON CONFLICT (id) DO NOTHING
            """, (user_id,))

            # Register user slug in entity_aliases as a NON-preferred technical alias.
            # The slug is a UUID-derived identifier (e.g., 550e8400_e29b_41d4_...)
            # used for schema naming only — it is NOT a human display name.
            # is_preferred=false ensures the query layer never surfaces it as the
            # user's name. Human names (set via ingest pref_name/also_known_as)
            # will be registered as is_preferred=true by entity_registry.register_alias().
            cur.execute("""
                INSERT INTO entity_aliases (entity_id, alias, is_preferred)
                VALUES (%s, %s, false)
                ON CONFLICT (entity_id, alias) DO NOTHING
            """, (user_id, user_slug))
            db.commit()
            log.info("registered_user_identity", schema=schema_name, user_id=user_id[:8], display_name=user_slug)
        except Exception as e:
            db.rollback()
            log.error("user_identity_registration_failed", schema=schema_name, user_id=user_id[:8], error=str(e))
            return "User identity registration failed: " + _errors.public_detail(e, where="schema.identity", what=type(e).__name__)
    return None


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
        ...     user_id="550e8400-e29b-41d4-a716-446655440000",
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
            return schema_name, "Error parsing POSTGRES_DSN: " + _errors.public_detail(e, where="schema.dsn", what=type(e).__name__)

        with db.cursor() as cur:
            # Create schema (idempotent)
            try:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema_name}")  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.formatted-sql-query.formatted-sql-query schema_name from derive_schema_name which sanitizes UUID-derived slug
                db.commit()
                log.info("created_schema", schema=schema_name, user_id=user_id[:8])
                # Update heartbeat after schema creation
                _update_provisioning_heartbeat(user_id, db)
            except Exception as e:
                db.rollback()
                return schema_name, "Failed to create schema: " + _errors.public_detail(e, where="schema.create", what=type(e).__name__)

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
                return schema_name, "Failed to verify schema existence: " + _errors.public_detail(e, where="schema.verify_exists", what=type(e).__name__)

            # The catalog check above opened a read transaction on this connection. The
            # template apply below runs a psql SUBPROCESS that takes tens of seconds; held
            # open, this transaction sat idle-in-transaction across the whole subprocess —
            # measured live 2026-08-22 on the gauntlet rig: 12.3s (the 2026-08-01 incident
            # class — the original pre-prod probe saw the same schemata fingerprint at
            # 5-11s x3). Release the pure-read transaction BEFORE the subprocess; the
            # connection is re-bound (SET search_path) before its next statement anyway.
            try:
                release_read_transaction(db, context="create_user_schema_schemata")
            except Exception:
                pass  # fail-safe: the release protects lock retention, never provisioning

            # Apply template schema via psql (handles dollar-quoted strings correctly)
            template_path = Path(__file__).parent / "templates" / "user_schema.sql"
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

            # SEED the tenant's metadata — the ONE function every "make this schema a usable seat"
            # caller runs (provisioning here; the bench sandbox reset after its TRUNCATE), so a
            # table the provisioner seeds can never be missed by a re-seeder that keeps its own
            # hand list (gauntlet kinship-name-cold-seat r2: the bench re-seeded 4 of 6 and every
            # reset seat came out with correction_patterns/correction_signals at 0).
            _seed_failure = seed_tenant_metadata(
                db, schema_name, user_id, user_slug,
                after_stage=lambda: _update_provisioning_heartbeat(user_id, db))
            if _seed_failure:
                return schema_name, _seed_failure

            # Verify critical tables exist before marking ready (FIX #2: prevent schema gaps)
            # False assumption: schema exists in schemata ≠ tables exist in schema
            # Ref: COMPREHENSIVE-FIX-PROMPT.md — migration errors cause partial table creation
            try:
                cur.execute(f"SET search_path TO {schema_name}, public")  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.formatted-sql-query.formatted-sql-query validated schema name from UUID source
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
                    'correction_patterns',  # pre-GLiNER2 negation-correction intent detection
                    'entity_synonyms',  # referential-term → entity/relationship linguistic layer (IMPL-1)
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
                return schema_name, "Schema verification failed: " + _errors.public_detail(e, where="schema.verify", what=type(e).__name__)

            # Flush stale Redis idempotency keys for this user.
            # On fresh schema creation, cached ingest/extract responses from a
            # prior schema are stale — the data they reference no longer exists.
            # One-shot: only fires during provisioning, never on normal requests.
            #
            # The tables-verified read above opened a READ transaction on this
            # connection; the flush performs NETWORK I/O (redis ping + SCAN) that
            # blocks on an unreachable DNS name for seconds — measured live on the
            # gauntlet stack 2026-08-22: 7.9s idle-in-transaction on BOTH provisioning
            # backends (schemata class), last query the information_schema verify.
            # Release before the blocking wait (the 9aa355c6 class).
            try:
                release_read_transaction(db, context="create_user_schema_redis_flush")
            except Exception:
                pass  # fail-safe: release protects lock retention, never provisioning
            _flush_user_idempotency_cache(user_id)

            # ── READY MEANS READY: everything below runs BEFORE the ready flag ─────────────
            # THE WOUND (first-touch-cold-path gauntlet, measured on pre-prod 2026-09-14): the
            # `status='ready'` UPDATE used to be committed HERE, and then this thread went on to
            # (a) stamp the migration ledger and (b) create the cortex schema + seed it (10
            # fastembed embeddings, an in-process ONNX model init on a cold process, Qdrant
            # PUTs). The MCP polls `status`, saw `ready`, and fired the seat's first
            # /classify-intent INTO that tail — measured locally: GLiNER2 inference 4.7s vs
            # 0.95s on the next turn, the whole request 8.4s vs ~1s, exactly co-timed with the
            # embedding PUTs; on pre-prod the same overlap was 22s, past the MCP's timeouts,
            # and the transport then substituted its own route. `asyncio.to_thread` keeps this
            # job off the event loop but NOT off the CPU — a `ready` written before the CPU-
            # heavy tail is a lie the first request pays for.
            #
            # Order now: heartbeat → ledger stamp → cortex (bounded, own connection) → READY.
            # The MCP's readiness wait (`_ensure_provisioned`) and the backend's
            # `_ensure_tenant_ready` both key on this flag, so both now include the whole job.
            _update_provisioning_heartbeat(user_id, db)  # the reaper must not mistake the tail for a crash

            # ── Record this brand-new schema as already carrying every on-disk migration ──
            # Provisioning does NOT run migrations; it applies the template and seeds from
            # `public`. Measured 2026-08-27: sweeping all 275 migrations over a freshly minted
            # schema changed NOTHING once the template's one missing column
            # (entity_aliases.coreference_warrant) was added — the corpus is redundant against
            # the template.
            #
            # This stamp is what keeps the boot gate honest. Without it a single new tenant has
            # no ledger rows, every fan-out migration becomes "needed" again, and the next boot
            # re-runs all 95 of them across the WHOLE fleet — steamrolling every existing tenant
            # to catch up one new one, which is exactly the behaviour the gate exists to stop.
            #
            # Fail-safe and non-blocking: if the stamp cannot be written the mint still succeeds
            # and the tenant simply gets the migrations applied on the next boot (correct, just
            # noisier). A ledger problem must never fail a provisioning. (It is now ONE multi-row
            # INSERT — `boot_migrations._stamp_rows` — not one autocommit transaction per file.)
            try:
                from src.provisioning.boot_migrations import stamp_schema_as_current
                stamped = stamp_schema_as_current(schema_name)
                log.info("stamped_schema_migration_current",
                         schema=schema_name, user_id=user_id[:8], migrations=stamped)
            except Exception as _stamp_e:
                log.warning("stamp_schema_migration_current_failed",
                            schema=schema_name, user_id=user_id[:8],
                            error=str(_stamp_e)[:200])

            # ── Agent cortex (firewalled `flagent_<uuid>` schema + seed + collection) ──────
            # Best-effort — a cortex failure NEVER fails user provisioning (cortex writes are
            # fail-safe until it exists, and the re-embedder's `sweep_lifecycle_for_user`
            # back-fills any seed row whose vector never landed). BOUNDED so that a Qdrant
            # black-hole cannot hold `ready` hostage past the transports' wait budgets: the
            # work runs on its own connection in a helper thread and we wait at most
            # PROVISIONING_CORTEX_BUDGET_SEC (default 30s); past the budget the flag is set
            # anyway and the cortex step finishes in the background (outage case only —
            # on a healthy box it completes in a few seconds and `ready` waits for it).
            _cortex_status = _provision_cortex_bounded(user_id)
            log.info("provisioned_cortex_schema", user_id=user_id[:8],
                     schema=derive_cortex_schema_name(user_id), status=_cortex_status)

            # Update user_provisioning status to ready (in public schema) — the LAST write.
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
                return schema_name, "Failed to mark provisioning as ready: " + _errors.public_detail(e, where="schema.mark_ready", what=type(e).__name__)

            return schema_name, "ready"

    except Exception as e:
        log.error("provisioning_failed", schema=schema_name, user_id=user_id, error=str(e))
        # ONE public shape for the row AND the return (one correlation id, one CRIT line). The
        # row is rendered VERBATIM by GET /provisioning/status via check_provisioning_status —
        # the round-2 critic read a DSN password back out of it (src/api/errors.py, ASVS V7.4.1).
        _public = _errors.public_detail(e, where="schema.create_user_schema", what="provisioning failed")

        # Try to update provisioning status with error
        try:
            with db.cursor() as cur:
                cur.execute("SET search_path TO public")
                cur.execute("""
                    UPDATE public.user_provisioning
                    SET status = 'error', error_message = %s
                    WHERE user_id = %s
                """, (_public, user_id))
                db.commit()
        except Exception as e2:
            log.error("failed_to_update_error_status", error=str(e2))

        return schema_name, "Error: " + _public

    finally:
        if close_conn and db:
            db.close()


def _provision_cortex_bounded(user_id: str) -> str:
    """Run `create_cortex_schema(user_id)` on its OWN connection, waiting at most
    PROVISIONING_CORTEX_BUDGET_SEC (default 30s) for it.

    Returns the cortex status ('ready' / error text / 'budget_exceeded'). NEVER raises and
    NEVER shares the caller's connection (a thread that outlives the budget must not touch a
    connection the caller is about to use for the ready UPDATE — psycopg2 connections are not
    safe for concurrent use). Past the budget the thread keeps running to completion in the
    background (best-effort semantics preserved); the reconcile sweep covers anything that
    did not land. The budget is env-driven, never a literal at the call site."""
    import concurrent.futures as _cf
    try:
        budget = float(os.environ.get("PROVISIONING_CORTEX_BUDGET_SEC", "30") or 30)
        if budget <= 0:
            budget = 30.0
    except ValueError:
        budget = 30.0
    pool = _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="cortex-provision")
    try:
        fut = pool.submit(create_cortex_schema, user_id, None)
        try:
            _schema, status = fut.result(timeout=budget)
            return status
        except _cf.TimeoutError:
            log.warning("provisioning_cortex_budget_exceeded", user_id=user_id[:8],
                        budget_sec=budget,
                        note="ready flag set anyway; cortex completes in background, "
                             "reconcile sweep back-fills")
            return "budget_exceeded"
        except Exception as e:  # noqa: BLE001 — best-effort by contract
            log.warning("provisioning_cortex_exception_nonfatal", user_id=user_id[:8],
                        error=str(e)[:200])
            return "error: " + _errors.public_detail(e, where="schema.cortex_bounded", what=type(e).__name__)
    finally:
        pool.shutdown(wait=False)


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
        ...     user_id="550e8400-e29b-41d4-a716-446655440000",
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
            cur.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE")  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.formatted-sql-query.formatted-sql-query schema_name from derive_schema_name(UUID) with sanitize, validated by UUID regex gate at derive_user_slug_from_uuid
            db.commit()
            log.info(f"deleted_schema", schema=schema_name, user_id=user_id)

            # Delete provisioning record
            cur.execute("SET search_path TO public")
            cur.execute("""
                DELETE FROM user_provisioning WHERE user_id = %s
            """, (user_id,))
            db.commit()

        # Flush the Redis idempotency cache on tenant wipe. The cache is keyed by an
        # opaque request hash with no per-tenant prefix, so a wiped tenant's stale
        # "already stored" entries would otherwise survive (TTL 3600s) and phantom-block a
        # legitimate re-ingest. Best-effort + fail-safe — a Redis error never fails the wipe.
        try:
            from src.api.idempotency import flush_idempotency_keys
            flushed = flush_idempotency_keys()
            log.info("schema_deletion.idempotency_flushed",
                     schema=schema_name, keys=flushed)
        except Exception as _flush_err:
            log.warning("schema_deletion.idempotency_flush_failed",
                        schema=schema_name, error=str(_flush_err))

        return True

    except Exception as e:
        log.error(f"schema_deletion_failed", schema=schema_name, user_id=user_id, error=str(e))
        return False

    finally:
        if close_conn and db:
            db.close()
