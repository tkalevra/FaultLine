"""BOOT MIGRATION GATE — apply each migration ONCE per schema, then never touch it again.

THE RULE THIS IMPLEMENTS
------------------------
    A boot must never steamroll an existing tenant's data. The engine's only involvement with a
    tenant schema is at first mint (to make it usable); after that, a migration touches it once,
    when the shape genuinely needs to change — never again on every start.

THE MEASURED PROBLEM
--------------------
``docker-entrypoint.sh`` ran ``psql -f`` over EVERY ``migrations/*.sql`` on EVERY container
start, relying on each file being idempotent. There was no ledger of any kind. 95 of the 275
files FAN OUT into every tenant schema (``FOR _schema IN ... LIKE 'faultline\\_%' LOOP``) and a
dozen of those INSERT/UPDATE/DELETE inside the fan-out.

Measured on the local 9-tenant database, one boot sweep, via ``pg_stat_all_tables`` deltas::

    ins=738  upd=3106  del=134     TOTAL 3,978 row-writes into EXISTING tenants

Every one of those is redundant: the same rows, re-written on every deploy, into schemas that
were finished months ago. 134 of them are DELETEs against tenant metadata.

WHY A LEDGER IS THE FIX AND EDITING THE 95 FILES IS NOT
-------------------------------------------------------
The files self-fan: each one opens its own ``FOR _schema IN pg_namespace`` loop. So the unit the
BOOT can gate is the FILE, not the (file, schema) pair — you cannot tell an already-written
DO-block to visit only one schema without editing it. The ledger therefore keys per (schema,
migration) for PROVENANCE and per-schema retry, but the RUN DECISION is:

    run file F  iff  at least one live schema lacks an `applied` row for F at F's CURRENT checksum

In steady state (no new migration, no new tenant) that is false for every file, every file is
skipped, and the boot performs ZERO writes against tenant data. That is the rule, mechanised.

THE BACKFILL PROBLEM — AND WHY THIS DESIGN DOES NOT HAVE ONE
-------------------------------------------------------------
The obvious danger is the backfill: existing tenants have already had all 275 applied, so the
ledger must be pre-marked or the first boot either re-runs everything (no better) or skips
something genuinely needed. Every "stamp what we believe is already applied" scheme risks
freezing a REAL gap — and this kind of database can carry a live example of one: a partial tenant schema
(half its tables missing) that ~20 migrations error against on every boot.

So this design does not pre-stamp anything. **The ledger starts empty and the first boot is
byte-for-byte today's behaviour** — every file runs, exactly as it always has — and each file
stamps itself as it succeeds. The "backfill" IS the first run. From the second boot onward
everything is skipped. This makes the transition zero-regression by construction: there is no
window in which a migration is skipped on the strength of an assumption about the past.

A schema is only ever marked `applied` for a file that actually ran to completion in the same
boot. Nothing is taken on trust.

FAIL-SAFE DIRECTION — WE RUN, WE DO NOT SKIP
---------------------------------------------
If the ledger cannot be read or written (table missing, permissions, connection fault), this
module RUNS EVERY MIGRATION — today's behaviour — and says so loudly. It never treats an
unreadable ledger as "nothing to do".

The argument is asymmetry of consequence, and both sides are real:

  * OVER-application (run something already applied) is BOUNDED, OBSERVABLE and SURVIVED: it is
    literally what every boot has done for 275 migrations. Its cost is the 3,978 redundant
    writes above — bad, which is why this module exists, but a known, measured, non-corrupting
    quantity.
  * UNDER-application (skip something needed) is UNBOUNDED and SILENT. A migration that never
    applies leaves a column absent, and in this codebase an absent column does not fail where it
    is missing — it raises ``UndefinedColumn``, ABORTS THE CALLER'S TRANSACTION, and surfaces as
    a wrong answer somewhere else entirely (the ``_query_scoped_to_absent_concept`` /
    ``entity_aliases.user_id`` incident: a walk that had already read the rows rendered "no facts
    found" on a memory it had just read). That failure is unattributable at the point it appears.

A skip is only safe when the ledger is TRUSTED. An unreadable ledger is not a trusted ledger, so
it buys no skips. Fail toward the loud, bounded, already-survived failure.

WHAT IS DELIBERATELY UNCHANGED
------------------------------
  * ``psql -f`` is still the executor, one subprocess per file, same order, same environment.
    Execution semantics are IDENTICAL — this matters: psql commits statement-by-statement, so
    legacy migrations that rely on continue-past-error idempotency behave exactly as before.
    (Sending a whole file through psycopg2 would wrap it in ONE implicit transaction and roll
    back MORE than psql does. That would be a silent semantic change, so it is not done.)
  * Errors do not stop startup, and the end-of-run ERROR summary is preserved verbatim in
    spirit — a silently-failed ADD CONSTRAINT is how dBug-074 happened.

ROLLBACK LEVER
--------------
``FAULTLINE_MIGRATION_LEDGER=false`` restores the legacy sweep exactly: every file, every boot,
no ledger reads or writes. Pinned by its own test, separately from the behaviour tests.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:  # THE ONE ERROR SEAM (redacted client detail); never let a missing helper break a boot
    from src.api import errors as _errors
except Exception:  # pragma: no cover - defensive
    class _errors:  # type: ignore[no-redef]
        @staticmethod
        def public_detail(exc, where="", what=""):
            return what or type(exc).__name__

try:  # the CRIT sink main.py uses; a missing logger must never break a migration run
    import structlog as _structlog
    from src.api.logging_config import log_crit as _log_crit
    _log = _structlog.get_logger()
except Exception:  # pragma: no cover - defensive
    _log = None
    _log_crit = None

try:  # psycopg2 is a hard dependency of the app, but this module must degrade loudly, not crash
    import psycopg2
except Exception:  # pragma: no cover - defensive
    psycopg2 = None  # type: ignore


# ── configuration ─────────────────────────────────────────────────────────────────────
def _ledger_enabled() -> bool:
    """The gate's on/off switch. DEFAULT ON.

    ``FAULTLINE_MIGRATION_LEDGER=false`` reproduces the pre-ledger sweep byte-for-byte.
    """
    return os.environ.get("FAULTLINE_MIGRATION_LEDGER", "true").strip().lower() != "false"


_DEFAULT_MIGRATIONS_DIR = "/app/migrations"

# Tenant schemas the fan-out migrations target. `public` is included because many files also
# (or only) touch public; gating it with the same key keeps one code path.
# NOTE the escaped underscore: in LIKE, a bare `_` is a single-character wildcard, so
# 'faultline_%' would also match e.g. 'faultlineX...'. The fan-out migrations themselves use
# 'faultline\_%'; this MUST match their target set or the gate and the files disagree.
_TENANT_SCHEMA_LIKE = r"faultline\_%"

# A migration_id is the file basename without `.sql` — stable and unique even though the
# numeric prefix alone can collide (the entrypoint warns about duplicate numbers).
_MIGRATION_GLOB_RE = re.compile(r"^\d+_.*\.sql$")

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS public.schema_migration_status (
    schema_name   TEXT NOT NULL,
    migration_id  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'applied', 'failed')),
    checksum      TEXT,
    started_at    TIMESTAMPTZ,
    applied_at    TIMESTAMPTZ,
    error         TEXT,
    PRIMARY KEY (schema_name, migration_id)
);
"""


def _checksum(sql_bytes: bytes) -> str:
    """Content hash of a migration file. A CHANGED file gets a new checksum, so it is detected
    and re-applied rather than silently skipped under its old identity."""
    return hashlib.sha256(sql_bytes).hexdigest()


# COMMENT-ONLY REVISIONS. A file whose only change is in SQL comments gets a new checksum, and
# the run decision would then RE-RUN it against every existing tenant (several of these files
# INSERT/UPDATE inside a per-tenant fan-out) for no schema change at all.
#
# Each entry maps a PRIOR checksum to the ONE reviewed CURRENT checksum it is equivalent to.
# A schema whose ledger row carries the prior is re-stamped ONLY while the file on disk still
# hashes to that expected current value (#162). Any later change to the file (a real SQL edit,
# or another comment edit) hashes to something else and takes the normal changed-migration
# path: it re-runs. tests/test_foss_migration_comment_scrub.py checks every expected value
# against the file on disk and, when the pre-scrub commit is in the clone, re-proves the pair
# comment-only from git history. Add an entry ONLY for a reviewed comment-only edit.
#   #158 (2026-10-02): references to private design docs scrubbed from these headers.
_COMMENT_ONLY_PRIOR_CHECKSUMS: Dict[str, Dict[str, str]] = {
    "086_trigger_span_patterns": {
        "8ed7ac277a46532890e81db644afc7cd6f41b3252190cfa08fad1336491630e8":
            "c4b30f7880e0f4b29fb8bbd3dc1f5692b07626604601c1e8fcb502c5c6f3a513",
    },
    "087_entity_taxonomy_nesting": {
        "1f64240a0bddc8787c490600701af1f06589ddf83c879a77dce416f592e9c2df":
            "71dde164b868b77afd6f03e980bbd2ee46f83808939d552d100cd8ca5d96a6c3",
    },
    "088_temporal_model": {
        "db5e107578198f43ea8868963267219f666015de7c6aa4e199b6d778ab94f5bb":
            "3aed8975fd81cdc5aca3478084e7aa1140b517d71e143049bf3b095659e5a263",
    },
    "089_wire_hierarchy_flags": {
        "dac13cf5e5610d7cdd392b368ab256935cdc989b70d34b7498c876064d5f92a2":
            "851832c27a7ae08f54b8b6e3abd6113b77e62465f74a9336ce5265a7032e404e",
    },
    "091_feels_rel_type_and_emotion_taxonomy": {
        "e5d85244c117c5c9577e3717fec21fd68ce5d3e9db270fc94425c0f6a145bfb6":
            "e551db436887c37f60edfdcec466c4fb4182d7f8588606eaa8cf272ab27e89e7",
    },
    "096_temporal_class": {
        "8e69e3dc44b22584ca34c12394686f4326010a715a95e52d40b9b4ddea0cbd8d":
            "68eba25978cba38a132c94d7279be8f9c4e5f855104cf9ef1f574b4d38c822b6",
    },
    "097_tombstone": {
        "9c7e53dd3387b7132685a92b969e44bb1344d3afbd2571be8246512337f5da29":
            "ade161b5c25e98cfeb0ec3d7ec41dac70b53d0d493558c2535d3108bcca091f4",
    },
    "098_event_date_granularity": {
        "a434ec243ba2cb552aac248ded30878aeb89244a5f30f028ea350eaf5b9bd317":
            "a31595529b0364ede5661f48f848062e0dd8cfaa778ea9404dd438741bf78af5",
    },
    "100_attended_rel_type_event_capture": {
        "7aa2e15978bc51b901bf028144570e226bac1a9736d5201478e07c234a6f9444":
            "c019dda568da883d0a96b4fff10e3016b486f7726f19ec37c58cfae02d56dc67",
    },
    "111_has_state_relational_predicate": {
        "20a7cc25b8ab13d89550b4cdd6ea6efc3a4bb8813d3dcb9080d1d56a3fe64d77":
            "6f20f1729ac3dd9721eb56f190054fc5906c0d82bb11ffa64e16741ef6d6bc37",
    },
}


@dataclass
class BootSummary:
    """What one boot actually did. These counts are PRINTED — the proof that the gate works is
    a measurement, not an assertion."""

    ran: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    errored: List[Tuple[str, int]] = field(default_factory=list)
    # migration_id -> structured diagnoses (sqlstate / message / missing_fk_target) for every
    # file that produced ERROR lines. The boot log line is for humans; this is for a test.
    diagnoses: Dict[str, List[Dict[str, Optional[str]]]] = field(default_factory=dict)
    stamped_pairs: int = 0
    failed_pairs: int = 0
    ledger_active: bool = True
    ledger_error: Optional[str] = None

    @property
    def ran_count(self) -> int:
        return len(self.ran)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)


# ── ledger plumbing ───────────────────────────────────────────────────────────────────
class LedgerUnavailable(RuntimeError):
    """The ledger could not be reached. Callers MUST fall back to running everything."""


def _connect(dsn: str):
    if psycopg2 is None:  # pragma: no cover - defensive
        raise LedgerUnavailable("psycopg2 is not importable")
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        return conn
    except Exception as exc:
        raise LedgerUnavailable(f"connect failed: {type(exc).__name__}: {exc}") from exc


def _ensure_ledger(conn) -> None:
    """Create the ledger if absent. Idempotent; safe on every boot.

    Self-sufficient on purpose: no migration creates this table — the gate runs BEFORE the
    migrations, so it owns its own ledger DDL.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(_LEDGER_DDL)
    except Exception as exc:
        raise LedgerUnavailable(f"cannot create/verify ledger: {type(exc).__name__}: {exc}") from exc


def _live_schemas(conn) -> List[str]:
    """Every schema the fan-out migrations can reach, plus `public`."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT nspname FROM pg_namespace WHERE nspname LIKE %s ORDER BY nspname",
                (_TENANT_SCHEMA_LIKE,),
            )
            schemas = [r[0] for r in cur.fetchall()]
    except Exception as exc:
        raise LedgerUnavailable(f"cannot enumerate schemas: {type(exc).__name__}: {exc}") from exc
    return ["public"] + schemas


def _applied_map(conn) -> Dict[Tuple[str, str], str]:
    """(schema, migration_id) -> checksum, for rows currently marked `applied`.

    Only `applied` rows are returned: `pending`/`failed` rows deliberately do NOT suppress a
    re-run, so a transient failure (lock timeout, deadlock) is retried on the next boot exactly
    as it is today.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT schema_name, migration_id, coalesce(checksum, '') "
                "FROM public.schema_migration_status WHERE status = 'applied'"
            )
            return {(r[0], r[1]): r[2] for r in cur.fetchall()}
    except Exception as exc:
        raise LedgerUnavailable(f"cannot read ledger: {type(exc).__name__}: {exc}") from exc


def _stamp(conn, pairs: Iterable[Tuple[str, str]], checksum: str, status: str,
           error: Optional[str] = None) -> int:
    """Record an outcome for (schema, migration) pairs. Best-effort and LOUD on failure — a
    ledger write that does not land only costs a redundant re-run next boot, never data."""
    rows = list(pairs)
    if not rows:
        return 0
    # applied_at is the timestamp for a SUCCESSFUL apply only; a `failed` row carries NULL so
    # "when did this last actually land" is never confused with "when did we last try".
    applied_at_sql = "now()" if status == "applied" else "NULL"
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO public.schema_migration_status "
                "(schema_name, migration_id, status, checksum, started_at, applied_at, error) "
                f"VALUES (%s, %s, %s, %s, now(), {applied_at_sql}, %s) "
                "ON CONFLICT (schema_name, migration_id) DO UPDATE SET "
                "  status = EXCLUDED.status, checksum = EXCLUDED.checksum, "
                "  started_at = EXCLUDED.started_at, applied_at = EXCLUDED.applied_at, "
                "  error = EXCLUDED.error",
                [(s, m, status, checksum, error) for (s, m) in rows],
            )
        return len(rows)
    except Exception as exc:
        print(f"WARNING: could not write migration ledger ({type(exc).__name__}: {exc}) — "
              f"these migrations will re-run next boot", flush=True)
        return 0


# ── migration discovery ───────────────────────────────────────────────────────────────
def discover_migrations(migrations_dir: str) -> List[Tuple[str, str, str]]:
    """Return [(migration_id, path, checksum)] in the SAME order the shell glob produced.

    ``/app/migrations/*.sql`` in bash sorts bytewise by full filename, which is what
    ``sorted()`` on the basename reproduces for this corpus (all names share the directory).
    Order is preserved exactly because several migrations depend on their predecessors.
    """
    try:
        names = sorted(os.listdir(migrations_dir))
    except Exception as exc:
        raise LedgerUnavailable(f"cannot list {migrations_dir}: {exc}") from exc

    out: List[Tuple[str, str, str]] = []
    for name in names:
        if not name.endswith(".sql"):
            continue
        path = os.path.join(migrations_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            body = fh.read()
        out.append((name[: -len(".sql")], path, _checksum(body)))
    return out


# ── error attribution ─────────────────────────────────────────────────────────────────
def attribute_errors(output: str, schemas: Sequence[str]) -> Tuple[List[str], Set[str], bool]:
    """Split a psql run's ERROR lines into per-schema blame.

    Returns ``(error_lines, blamed_schemas, has_unattributed)``.

    WHY THIS EXISTS. A partial tenant schema (tables missing) makes ~20 migrations error on
    EVERY boot ("relation \"faultline_<x>.facts\" does not exist"). If one
    such error blocked stamping for the whole file, those files would never converge and the
    ledger would deliver nothing for them. The error names the schema, so the healthy schemas can
    still be stamped while the broken one is recorded `failed` and retried — which is strictly
    BETTER isolation than today, where the file simply re-runs against everyone forever.

    An error line that names NO known schema (e.g. ``column "fact_provenance" does not exist``)
    is UNATTRIBUTABLE. We then blame nothing and refuse to stamp anything for that file, so it
    re-runs next boot exactly as it does today. Conservative on purpose: we never guess that an
    unexplained failure was harmless.
    """
    error_lines = [ln for ln in output.splitlines() if "ERROR:" in ln]
    blamed: Set[str] = set()
    unattributed = False
    # Longest first so `faultline_abc_x` cannot be shadowed by `faultline_abc`.
    ordered = sorted((s for s in schemas if s != "public"), key=len, reverse=True)
    for line in error_lines:
        hit = next((s for s in ordered if s in line), None)
        if hit is None:
            unattributed = True
        else:
            blamed.add(hit)
    return error_lines, blamed, unattributed


# ── error diagnosis (seed-030-first-boot) ────────────────────────────────────────────
# `ERROR:  23503: ...` — psql verbose puts the five-character SQLSTATE right after the severity.
_SQLSTATE_RE = re.compile(r"ERROR:\s+([0-9A-Z]{5}):\s*(.*)$")
# PostgreSQL's FK-violation DETAIL (SQLSTATE 23503, class 23 integrity_constraint_violation —
# Appendix A, https://www.postgresql.org/docs/current/errcodes-appendix.html):
#   DETAIL:  Key (canonical_rel_type)=(member_of) is not present in table "rel_types".
_FK_DETAIL_RE = re.compile(
    r"DETAIL:\s+Key \(([^)]+)\)=\(([^)]*)\) is not present in table \"([^\"]+)\""
)


def diagnose_errors(output: str) -> List[Dict[str, Optional[str]]]:
    """Turn a psql run's error report into structured, greppable diagnoses.

    One dict per ``ERROR:`` line: ``sqlstate``, ``message``, ``line`` (the ``file:NNN``
    location psql prefixes), and — for an FK violation — ``missing_fk_target`` as
    ``table.column=value`` read from the DETAIL line that follows. WHY: migration 030's 75-row
    alias INSERT was rejected on every fresh database for months (``member_of`` absent from
    ``rel_types``) and the only trace was one ERROR line in a 2,000-line boot log; the file was
    recorded ``failed`` (correctly — never ``applied``), but nothing said WHICH row of WHICH
    table was missing, so the wound read as boot noise until boot #2 papered over it. Pure
    parsing; never raises; an output without ERROR lines yields ``[]``.
    """
    diagnoses: List[Dict[str, Optional[str]]] = []
    current: Optional[Dict[str, Optional[str]]] = None
    for raw in output.splitlines():
        line = raw.rstrip()
        if "ERROR:" in line:
            m = _SQLSTATE_RE.search(line)
            loc = line.split("ERROR:", 1)[0].strip().rstrip(":") or None
            if m:
                current = {"sqlstate": m.group(1), "message": m.group(2).strip(),
                           "line": loc, "missing_fk_target": None}
            else:  # legacy/non-verbose shape — keep the message, no code
                current = {"sqlstate": None,
                           "message": line.split("ERROR:", 1)[1].strip(),
                           "line": loc, "missing_fk_target": None}
            diagnoses.append(current)
            continue
        if current is not None and current.get("missing_fk_target") is None:
            fk = _FK_DETAIL_RE.search(line)
            if fk:
                current["missing_fk_target"] = f"{fk.group(3)}.{fk.group(1)}={fk.group(2)}"
    return diagnoses


def _crit_rejected_migration(migration_id: str, path: str, diagnoses: Sequence[Dict[str, Optional[str]]],
                             *, needed: Sequence[str]) -> None:
    """LOUD, per rejected statement: one CRIT record naming the migration, the SQLSTATE, the
    message and (for 23503) the missing FK target. A rejected migration is a seed/order defect,
    not boot noise — it must be greppable as ``boot_migrations.statement_rejected`` and must
    never hide behind the end-of-run summary. Best-effort: a logging failure never masks the
    boot."""
    if _log is None or _log_crit is None:
        return
    for d in diagnoses:
        try:
            _log_crit(
                _log, "boot_migrations.statement_rejected",
                migration_id=migration_id, path=path,
                sqlstate=d.get("sqlstate"), message=d.get("message"),
                location=d.get("line"), missing_fk_target=d.get("missing_fk_target"),
                schemas_needed=len(needed),
                consequence="recorded failed (never applied); re-runs next boot — fix the seed order, "
                            "a file that only applies on boot #2 is absent from every first boot",
            )
        except Exception:  # noqa: BLE001 — a log line must never mask the failure it records
            pass


# ── the run ───────────────────────────────────────────────────────────────────────────
def _run_psql(dsn: str, path: str) -> str:
    """Execute one migration EXACTLY as the entrypoint always has. Never raises.

    ``-v VERBOSITY=verbose`` is the one addition (seed-030-first-boot): psql's VERBOSITY
    variable controls the *format* of error reports only — ``verbose`` prints the SQLSTATE on
    the ERROR line (``ERROR:  23503: insert or update ...``) plus DETAIL / SCHEMA NAME /
    TABLE NAME / CONSTRAINT NAME lines, exactly what ``diagnose_errors`` reads. Execution
    semantics (statement-by-statement commit, continue past error) are untouched.
    PostgreSQL docs, psql "Variables" → VERBOSITY:
    https://www.postgresql.org/docs/current/app-psql.html#APP-PSQL-VARIABLES-VERBOSITY
    """
    try:
        proc = subprocess.run(
            ["psql", dsn, "-v", "VERBOSITY=verbose", "-f", path],
            capture_output=True, text=True, check=False,
        )
        return (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:  # pragma: no cover - defensive
        return "ERROR:  could not execute psql for " + path + ": " + _errors.public_detail(
            exc, where="boot_migrations.psql", what=type(exc).__name__)


def run_boot_migrations(
    dsn: Optional[str] = None,
    migrations_dir: Optional[str] = None,
    *,
    echo: bool = True,
) -> BootSummary:
    """Apply every migration that is not already applied to every live schema.

    Returns a :class:`BootSummary` with the applied/skipped counts. Never raises for a migration
    failure — failures are recorded and reported, and startup continues (unchanged behaviour).
    """
    dsn = dsn or os.environ.get("POSTGRES_DSN") or ""
    migrations_dir = migrations_dir or os.environ.get("FAULTLINE_MIGRATIONS_DIR") or _DEFAULT_MIGRATIONS_DIR
    summary = BootSummary()

    def say(msg: str) -> None:
        if echo:
            print(msg, flush=True)

    try:
        migrations = discover_migrations(migrations_dir)
    except LedgerUnavailable as exc:
        say(f"FATAL: {exc}")
        summary.ledger_error = _errors.public_detail(exc, where="boot_migrations.discover", what=type(exc).__name__)
        return summary

    # ── decide whether the gate is available at all ──────────────────────────────────
    conn = None
    applied: Dict[Tuple[str, str], str] = {}
    schemas: List[str] = []
    gate_on = _ledger_enabled()

    if not gate_on:
        say("Migration ledger DISABLED (FAULTLINE_MIGRATION_LEDGER=false) — "
            "running every migration, legacy behaviour.")
        summary.ledger_active = False
    else:
        try:
            conn = _connect(dsn)
            _ensure_ledger(conn)
            schemas = _live_schemas(conn)
            applied = _applied_map(conn)
        except LedgerUnavailable as exc:
            # FAIL-SAFE: run everything. See the module docstring — an unreadable ledger buys
            # no skips, because a skip is only safe when the ledger is trusted.
            say("==================================================================")
            say(f"WARNING: migration ledger UNAVAILABLE ({exc}).")
            say("Falling back to RUNNING EVERY MIGRATION (pre-ledger behaviour).")
            say("This is the deliberate fail-safe direction: over-application is bounded and")
            say("survivable; a silently skipped migration is not. Nothing is being skipped.")
            say("==================================================================")
            summary.ledger_active = False
            summary.ledger_error = _errors.public_detail(exc, where="boot_migrations.ledger", what=type(exc).__name__)
            conn = None

    say(f"Running migrations ({len(migrations)} files"
        + (f", {len(schemas)} schemas, ledger ACTIVE" if summary.ledger_active else ", ledger INACTIVE")
        + ")...")

    for migration_id, path, checksum in migrations:
        needed: List[str] = []
        if summary.ledger_active:
            prior = _COMMENT_ONLY_PRIOR_CHECKSUMS.get(migration_id, {})
            equivalent = [s for s in schemas
                          if prior.get(applied.get((s, migration_id)) or "") == checksum]
            if equivalent and conn is not None:
                # Applied under a comment-only-different revision: re-stamp, never re-run.
                summary.stamped_pairs += _stamp(
                    conn, [(s, migration_id) for s in equivalent], checksum, "applied")
            needed = [s for s in schemas
                      if applied.get((s, migration_id)) != checksum and s not in equivalent]
            if not needed:
                summary.skipped.append(migration_id)
                continue

        say(f"Applying {path}...")
        output = _run_psql(dsn, path)
        if echo and output.strip():
            print(output, flush=True)
        summary.ran.append(migration_id)

        error_lines, blamed, unattributed = attribute_errors(
            output, schemas if summary.ledger_active else []
        )
        if error_lines:
            say(f">>> {path} produced {len(error_lines)} ERROR line(s) (execution continued)")
            summary.errored.append((migration_id, len(error_lines)))
            diagnoses = diagnose_errors(output)
            summary.diagnoses[migration_id] = diagnoses
            for d in diagnoses:
                say(f">>>   sqlstate={d.get('sqlstate')} at={d.get('line')} "
                    f"missing_fk_target={d.get('missing_fk_target')} :: {d.get('message')}")
            _crit_rejected_migration(migration_id, path, diagnoses,
                                     needed=needed if summary.ledger_active else [])

        if not summary.ledger_active or conn is None:
            continue

        if unattributed:
            # Cannot tell which schema failed → stamp NOTHING; re-run next boot, as today.
            summary.failed_pairs += _stamp(
                conn, [(s, migration_id) for s in needed], checksum, "failed",
                error="unattributed error; see boot log",
            )
            continue

        ok_pairs = [(s, migration_id) for s in needed if s not in blamed]
        bad_pairs = [(s, migration_id) for s in needed if s in blamed]
        summary.stamped_pairs += _stamp(conn, ok_pairs, checksum, "applied")
        if bad_pairs:
            summary.failed_pairs += _stamp(
                conn, bad_pairs, checksum, "failed", error="schema-attributed error; see boot log"
            )

    # ── the summary. These numbers ARE the proof the gate is working. ────────────────
    if summary.errored:
        say("==================================================================")
        say("WARNING: migrations produced ERROR lines (startup continues):")
        for mid, n in summary.errored:
            say(f"  - {mid} ({n} error(s))")
        say("Some errors are expected re-run noise (e.g. duplicate_object on")
        say("unguarded ADD CONSTRAINT), but a NEW migration appearing here means")
        say("its schema change may NOT have applied. Inspect before trusting.")
        say("==================================================================")

    say(f"Migrations complete — applied={summary.ran_count} skipped={summary.skipped_count} "
        f"(ledger {'ACTIVE' if summary.ledger_active else 'INACTIVE'}; "
        f"stamped={summary.stamped_pairs} failed={summary.failed_pairs} schema-pairs)")

    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    return summary


def stamp_schema_as_current(schema_name: str, dsn: Optional[str] = None,
                            migrations_dir: Optional[str] = None) -> int:
    """Mark a FRESHLY MINTED schema as already carrying every on-disk migration.

    WHY THIS IS CORRECT, AND MEASURED. Provisioning does NOT run migrations — it applies
    ``templates/user_schema.sql`` and seeds from ``public``. So the question is whether the
    template is equivalent to template+migrations. Measured directly: a fresh mint was swept with
    all 275 migrations and the ONLY differences were one column (``entity_aliases
    .coreference_warrant``, migration 244 — since added to the template) and four rel_types rows
    that migration 271 STRIPPED. i.e. the corpus is redundant against the template.

    WHY IT MATTERS THAT WE STAMP. Without this, one newly minted tenant has no ledger rows, so
    every fan-out migration becomes "needed" again and re-runs across the WHOLE fleet — the boot
    would steamroll every existing tenant in order to catch up one new one. That is precisely the
    behaviour the rule forbids, re-entering through the back door.

    The equivalence this relies on is PINNED by a test (``test_boot_migrations.py``) that mints a
    schema, sweeps it, and asserts nothing changed. If a future migration is added without
    updating the template, that test goes red — the invisible discipline problem becomes a red
    test rather than a silently stale tenant.
    """
    dsn = dsn or os.environ.get("POSTGRES_DSN") or ""
    migrations_dir = migrations_dir or os.environ.get("FAULTLINE_MIGRATIONS_DIR") or _DEFAULT_MIGRATIONS_DIR
    if not _ledger_enabled():
        return 0
    try:
        migrations = discover_migrations(migrations_dir)
        conn = _connect(dsn)
    except LedgerUnavailable as exc:
        # Fail-safe: no stamp. The tenant simply gets the migrations applied on the next boot —
        # correct, just noisier. Never block a mint on the ledger.
        print(f"WARNING: could not stamp {schema_name} as migration-current ({exc}); "
              f"its migrations will be applied on the next boot", flush=True)
        return 0
    try:
        _ensure_ledger(conn)
        # ONE STATEMENT, ONE COMMIT (first-touch-cold-path). This used to loop `_stamp(...)`
        # once per migration; `_connect` runs AUTOCOMMIT, so N migrations were N single-row
        # transactions = N WAL fsyncs. Measured: ~1.5s on local NVMe, ~31s on a ZFS-backed
        # PostgreSQL — a fsync-bound tail that overlapped a fresh tenant's first turn. `execute_values` renders one multi-row INSERT, so the stamp
        # is one statement and one fsync regardless of the corpus size. Same ON CONFLICT
        # semantics as `_stamp`; `_stamp` itself is unchanged for the boot-sweep callers.
        return _stamp_rows(conn, [(schema_name, mid, cks) for mid, _p, cks in migrations],
                           "applied")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _stamp_rows(conn, rows: List[Tuple[str, str, str]], status: str) -> int:
    """Record (schema, migration, checksum) rows in ONE multi-row INSERT (one transaction).

    Best-effort and LOUD on failure, like `_stamp`: a ledger write that does not land only
    costs a redundant re-run next boot, never data."""
    if not rows:
        return 0
    applied_at_sql = "now()" if status == "applied" else "NULL"
    try:
        from psycopg2.extras import execute_values
        with conn.cursor() as cur:
            execute_values(
                cur,
                "INSERT INTO public.schema_migration_status "
                "(schema_name, migration_id, status, checksum, started_at, applied_at, error) "
                "VALUES %s "
                "ON CONFLICT (schema_name, migration_id) DO UPDATE SET "
                "  status = EXCLUDED.status, checksum = EXCLUDED.checksum, "
                "  started_at = EXCLUDED.started_at, applied_at = EXCLUDED.applied_at, "
                "  error = EXCLUDED.error",
                [(s, m, status, c, None) for (s, m, c) in rows],
                template=f"(%s, %s, %s, %s, now(), {applied_at_sql}, %s)",
                page_size=1000,
            )
        return len(rows)
    except Exception as exc:
        print(f"WARNING: could not write migration ledger ({type(exc).__name__}: {exc}) — "
              f"these migrations will re-run next boot", flush=True)
        return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    summary = run_boot_migrations()
    # Startup continues regardless — a migration error has never blocked boot and must not start
    # doing so here (that would be a behaviour change smuggled in with an unrelated fix).
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
