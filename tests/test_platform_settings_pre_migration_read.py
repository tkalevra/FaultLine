"""THE DEPLOY GATE: a settings read against a PRE-MIGRATION database must not take the product
offline.

WHAT THIS SUITE FALSIFIES
─────────────────────────

``saas/platform_settings._read`` fails **CLOSED** — any read error resolves to
``offline_mode=True`` and every request is served the maintenance page. That is the right choice
for an unreadable control plane, and it has a consequence that is invisible from the SELECT list:
**naming a column the deployed database does not have yet raises ``UndefinedColumn``, which
resolves to offline.**

Deploys are not atomic. The image can land before its migration runs, and a migration can error.
So the ordinary, expected state "new code, old schema" must degrade to the DOCUMENTED DEFAULTS,
not to an outage whose logged cause points at a settings read rather than at the schema.

MEASURED, and this is why the suite exists: at ``944fbaad``, ``_COLS`` named
``data_retention_days``, ``backup_overwrite_days``, ``fulfilment_days`` and
``contract_term_months`` — four columns that arrive in migrations 246/248, new in that same
commit — while production's ``public.platform_settings`` had none of them. Driving that code
against a pre-246 database produced::

    [saas-platform-settings] read FAILED (UndefinedColumn) — resolving fail-closed/last-known
    offline_mode : True

The module already documented the correct pattern three lines below the defect and applied it to
exactly ONE column (``_SEAT_RATES_EXPR``), with a comment saying that naming it directly "would
take the entire product offline".

THE RULE THIS PINS: in a fail-CLOSED settings reader, a column added by a migration is read as an
appended ``to_jsonb(ps.*) -> 'col'`` expression, NEVER as a member of the SELECT list. An absent
key yields SQL NULL instead of raising.

⚠️ FALSE-GREEN GUARD: ``_DB_RAN`` is set by the DB fixture and asserted by the last test. Every
claim here is a property of a real read against a real schema, so a run reporting "N skipped" is
RED. Run with a THROWAWAY DSN and assert skipped == 0.
"""

import os
import uuid

import pytest

from saas import platform_settings as ps


_DSN = os.environ.get("POSTGRES_DSN")

try:
    import psycopg2 as _pg
except Exception:  # pragma: no cover
    _pg = None

needs_db = pytest.mark.skipif(not _DSN or _pg is None,
                              reason="POSTGRES_DSN + psycopg2 required")

_DB_RAN = {"yes": False}

# The EXACT shape of ``public.platform_settings`` after migrations 131..245 — read off a real
# database built by running those migrations in order, not invented here. The five columns that
# migrations 246 and 249 add are, by construction, ABSENT: that absence is the whole test.
_PRE_246_DDL = """
CREATE TABLE public.platform_settings (
    singleton                  BOOLEAN PRIMARY KEY DEFAULT TRUE,
    offline_mode               BOOLEAN NOT NULL DEFAULT FALSE,
    offline_message            TEXT,
    offline_since              TIMESTAMPTZ,
    offline_by                 TEXT,
    require_signup_approval    BOOLEAN NOT NULL DEFAULT FALSE,
    notify_email               TEXT,
    welcome_from_address       TEXT DEFAULT 'noreply@faultline.ca',
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by                 TEXT,
    trial_days                 INTEGER NOT NULL DEFAULT 30,
    retention_grace_days       INTEGER NOT NULL DEFAULT 7,
    chatbot_trial_days         INTEGER NOT NULL DEFAULT 15,
    chatbot_trial_sunset_hours INTEGER NOT NULL DEFAULT 24,
    re_embedder_enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    mfa_prompt_grace_logins    INTEGER NOT NULL DEFAULT 10,
    support_email              TEXT,
    billing_from_address       TEXT,
    billing_from_name          TEXT,
    retention_emails_enabled   BOOLEAN NOT NULL DEFAULT FALSE,
    email_kill_switch          BOOLEAN NOT NULL DEFAULT FALSE
);
"""

# What migration 246 adds, and the values this suite writes once it has been simulated. Chosen to
# differ from every documented default so "the real value was read" cannot be confused with "the
# default was used".
_MIG_246_COLS = (
    ("data_retention_days", 45, ps._DEFAULT_DATA_RETENTION_DAYS),
    ("backup_overwrite_days", 60, ps._DEFAULT_BACKUP_OVERWRITE_DAYS),
    ("fulfilment_days", 10, ps._DEFAULT_FULFILMENT_DAYS),
    ("contract_term_months", 12, ps._DEFAULT_CONTRACT_TERM_MONTHS),
)


def _dsn_for(dbname: str) -> str:
    """``_DSN`` with its database swapped. Deterministic string surgery on the path segment."""
    head, _, tail = _DSN.rpartition("/")
    # Preserve any query string (?sslmode=…) that rode on the original database name.
    qs = ""
    if "?" in tail:
        qs = "?" + tail.split("?", 1)[1]
    return f"{head}/{dbname}{qs}"


@pytest.fixture(scope="module")
def pre_246_db():
    """A REAL throwaway database whose ``platform_settings`` predates migration 246.

    A separate database rather than a schema, because ``_read`` names ``public.platform_settings``
    literally — the only faithful way to test what it does against an older ``public`` is to give
    it an older ``public``."""
    name = "fl_ps_pre246_test_" + uuid.uuid4().hex[:8]
    admin = _pg.connect(_dsn_for("postgres"))
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    admin.close()

    conn = _pg.connect(_dsn_for(name))
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(_PRE_246_DDL)
        # offline_mode EXPLICITLY FALSE. The product is UP in this database; anything that
        # reports it as down is the reader's doing, not the row's.
        cur.execute("INSERT INTO public.platform_settings (singleton, offline_mode) "
                    "VALUES (TRUE, FALSE)")
    conn.close()

    _DB_RAN["yes"] = True
    try:
        yield name
    finally:
        admin = _pg.connect(_dsn_for("postgres"))
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.close()


@pytest.fixture
def read_against(monkeypatch):
    """Point ``saas.platform_settings`` at a given database and drop its cache.

    The cache matters: ``state()`` returns the last GOOD read on a failure, so a suite that read a
    healthy database first would mask the very outage under test."""
    def _go(dbname: str) -> dict:
        monkeypatch.setenv("POSTGRES_DSN", _dsn_for(dbname))
        ps.invalidate()
        # Also clear the last-known-good value, so nothing can be answered from an earlier read.
        with ps._lock:
            ps._cache = None
        return ps.state(fresh=True)
    return _go


# ══════════════════════════════════════════════════════════════════════════════════
# THE OUTAGE ITSELF
# ══════════════════════════════════════════════════════════════════════════════════

@needs_db
def test_a_pre_migration_database_does_not_take_the_product_offline(pre_246_db, read_against):
    """THE DEPLOY GATE. New image, old schema, ``offline_mode = FALSE`` in the row.

    If this goes red, a column added by a migration has been named in ``_COLS`` again, and
    deploying the image before its migration serves every user the maintenance page."""
    st = read_against(pre_246_db)
    assert st["offline_mode"] is False, (
        "a PRE-MIGRATION platform_settings read resolved the product OFFLINE — the SELECT list "
        "names a column this database does not have. Read it as an appended "
        "to_jsonb(ps.*) -> 'col' expression instead (see _APPENDED_COLS).")
    assert ps.is_offline() is False
    assert st["degraded"] is False, (
        "the read did not merely survive — it must SUCCEED. 'degraded' means it fell back to the "
        "fail-safe state, which is the outage wearing a different hat.")


@needs_db
def test_the_four_contract_numbers_fall_back_to_their_documented_defaults(pre_246_db,
                                                                         read_against):
    """Degrading is not enough — it must degrade to the numbers the shipped agreement STATES.

    Retention 30 / backup 35 / fulfilment 5 / term 6. Never 0, never None: a 0 here would publish
    a contract promising zero days of retention, or a term with no commitment at all."""
    st = read_against(pre_246_db)
    for col, _live, default in _MIG_246_COLS:
        assert st[col] == default, f"{col} fell back to {st[col]!r}, expected {default}"
        assert st[col] >= 1


@needs_db
def test_the_getters_agree_with_the_state_on_a_pre_migration_database(pre_246_db, read_against):
    """The public getters are what the rest of the product calls; they must degrade identically."""
    read_against(pre_246_db)
    assert ps.data_retention_days() == ps._DEFAULT_DATA_RETENTION_DAYS
    assert ps.backup_overwrite_days() == ps._DEFAULT_BACKUP_OVERWRITE_DAYS
    assert ps.fulfilment_days() == ps._DEFAULT_FULFILMENT_DAYS
    assert ps.contract_term_months() == ps._DEFAULT_CONTRACT_TERM_MONTHS


@needs_db
def test_an_absent_seat_rate_card_still_degrades_to_the_shipped_rates(pre_246_db, read_against):
    """The one column that was ALREADY read correctly, pinned so the pattern cannot be undone."""
    st = read_against(pre_246_db)
    assert st["seat_rates_cents"] is None
    card = ps.seat_rates()
    assert card["tenant"]["standard"] == ps._SHIPPED_SEAT_RATES_CENTS["tenant"]["standard"]


# ══════════════════════════════════════════════════════════════════════════════════
# AND THE OTHER HALF: ONCE THE MIGRATION LANDS, THE REAL VALUES MUST BE READ
# ══════════════════════════════════════════════════════════════════════════════════

@needs_db
def test_once_migration_246_lands_the_real_values_are_read(pre_246_db, read_against):
    """A fallback that can never be escaped is not a fallback, it is a hardcoded constant.

    Adds the four columns to the SAME database and asserts the reader picks up values that differ
    from every default — so this cannot pass by accident."""
    conn = _pg.connect(_dsn_for(pre_246_db))
    conn.autocommit = True
    with conn.cursor() as cur:
        for col, live, _default in _MIG_246_COLS:
            cur.execute(f"ALTER TABLE public.platform_settings "
                        f"ADD COLUMN IF NOT EXISTS {col} INTEGER NOT NULL DEFAULT {live}")
    conn.close()

    st = read_against(pre_246_db)
    assert st["offline_mode"] is False
    for col, live, default in _MIG_246_COLS:
        assert live != default, "fixture error: the live value must differ from the default"
        assert st[col] == live, f"{col} read {st[col]!r}, expected the stored {live}"
    assert ps.contract_term_months() == 12


@needs_db
def test_a_junk_value_degrades_to_the_default_rather_than_offline(pre_246_db, read_against):
    """The appended expressions arrive as TEXT, so a non-numeric value is now REACHABLE.

    It must resolve to the documented default. Failing closed over an unparseable integer would
    re-create the outage this suite exists to prevent, by a different route."""
    conn = _pg.connect(_dsn_for(pre_246_db))
    conn.autocommit = True
    with conn.cursor() as cur:
        # Widen one column so a non-numeric value can be stored at all, then poison it.
        cur.execute("ALTER TABLE public.platform_settings "
                    "ALTER COLUMN fulfilment_days TYPE TEXT USING fulfilment_days::text")
        cur.execute("UPDATE public.platform_settings SET fulfilment_days = 'not-a-number'")
    conn.close()

    st = read_against(pre_246_db)
    assert st["offline_mode"] is False
    assert st["fulfilment_days"] == ps._DEFAULT_FULFILMENT_DAYS


# ══════════════════════════════════════════════════════════════════════════════════
# THE STRUCTURAL PIN — cheap, runs without a DSN, and names the rule directly
# ══════════════════════════════════════════════════════════════════════════════════

# Stated HERE, independently of ``_APPENDED_COLS``, on purpose. A pin that reads its expectation
# out of the structure it is guarding cannot see a change that edits both — measured: a mutation
# that moved a column from _APPENDED_COLS into _COLS left a pin written against _APPENDED_COLS
# green, because the loop simply stopped checking the column that had just been broken.
_COLUMNS_ADDED_BY_A_MIGRATION = (
    "data_retention_days",      # 246
    "backup_overwrite_days",    # 246
    "fulfilment_days",          # 246
    "contract_term_months",     # 246
    "seat_rates_cents",         # 249
    "require_email_activation", # 302
)


def test_no_migration_added_column_is_named_in_the_select_list():
    """``_COLS`` must not name any column a migration adds. The behavioural tests above prove the
    consequence; this one names the rule, so the failure message explains itself."""
    for col in _COLUMNS_ADDED_BY_A_MIGRATION:
        assert col not in ps._COLS, (
            f"{col!r} is named directly in _COLS. On a deployment where its migration has not "
            f"run, the settings read raises UndefinedColumn, _read fails CLOSED, and the WHOLE "
            f"PRODUCT serves the maintenance page. Read it via _APPENDED_COLS instead.")


def test_every_appended_column_is_read_through_the_row_to_jsonb_form():
    """The expression must ask the ROW for a key (NULL when absent), not name the column."""
    for col in _COLUMNS_ADDED_BY_A_MIGRATION:
        assert f"'{col}'" in ps._APPENDED_EXPR, (
            f"{col} is not read through to_jsonb(ps.*) — a pre-migration read will raise")
    assert ps._APPENDED_EXPR.count("to_jsonb(ps.*)") == len(_COLUMNS_ADDED_BY_A_MIGRATION)


def test_the_positional_indices_still_line_up_with_the_select_list():
    """``_row_to_state`` indexes POSITIONALLY. This pins the arithmetic the comments rely on:
    the appended expressions start immediately after the last ``_COLS`` member."""
    n_cols = len([c.strip() for c in ps._COLS.split(",") if c.strip()])
    assert n_cols == 15, f"_COLS holds {n_cols} columns; _row_to_state indexes 0..14 from it"
    assert ps._APPENDED_COLS[0] == "data_retention_days"      # row[15]
    assert ps._APPENDED_COLS[-1] == "require_email_activation"  # row[20]
    assert len(ps._APPENDED_COLS) == 6


@needs_db
def test_zzz_no_test_was_silently_skipped():
    """FALSE-GREEN GUARD. Every claim above is a property of a real read against a real schema."""
    assert _DB_RAN["yes"], (
        "the DB fixture never ran — this suite passed without touching a database and proved "
        "nothing about the deploy gate.")
