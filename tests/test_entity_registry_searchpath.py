"""registry-searchpath-fallthrough — an EntityRegistry read or write can NEVER land in `public`.

THE WOUND (pre-prod, 2026-09-14): `/ingest` answered 500 `UndefinedColumn: column "display_form"
does not exist` from `SELECT entity_id, display_form FROM entity_aliases …`
(src/entity_registry/registry.py::resolve) on a tenant whose OWN `entity_aliases` has that column.
The only `entity_aliases` without it is the LEGACY seed-template `public.entity_aliases`, so the
unqualified read ran on a connection whose search_path had lost the tenant binding.

THE MECHANISM, cited (constraint 11 — evidence-ground):
  * PostgreSQL, SET (https://www.postgresql.org/docs/current/sql-set.html):
      "If SET (or equivalently SET SESSION) is issued within a transaction that is later aborted,
       the effects of the SET command disappear when the transaction is rolled back."
  * PostgreSQL, DDL — Schemas §5.10.3 (https://www.postgresql.org/docs/current/ddl-schemas.html):
      "The first matching table in the search path is taken to be the one wanted." — default
      `"$user", public`; "If no such schema exists, the entry is ignored."
  `/ingest` (main.py::ingest) binds the tenant with a plain session-level SET inside its open,
  autocommit-OFF transaction and builds the registry with auto_commit=False. One rollback on that
  connection discards the SET; `"$user"` is skipped; every later unqualified read resolves in
  `public`. HEAD's registry re-asserted the SET before six WRITE sites and never before the
  resolve() READ — and main.py:14038 built `EntityRegistry(db)` with no schema_name at all.

THE FIX (one seam): `EntityRegistry._bind_tenant_schema()` on entry of every public DB-touching
method (mirrors src/wgm/gate.py::_reapply_search_path); refuses LOUD when unbound under
per-tenant operation (SAAS_MODE / ENTITY_REGISTRY_REQUIRE_SCHEMA); legacy single-tenant path
(no schema_name, no SAAS_MODE) proceeds on the connection's own search_path with one warning.

Gated on POSTGRES_DSN (like tests/test_tenant_checkout_module.py). ⚠️ A skipped DB test is NOT
evidence: with a DSN this file must run with `skipped == 0`. It DROPS/CREATES tables in `public`
of the database the DSN names, so it REFUSES to run against a database called `faultline` (the
owner's real inventory) — point it at a THROWAWAY database.
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from src.entity_registry import registry as R  # noqa: E402
from src.entity_registry.registry import EntityRegistry  # noqa: E402

# Resolved lazily so a REVERT-RED run against HEAD's registry (which has no such class) collects
# and fails ON THE BARS (RS1 UndefinedColumn, RS3 no refusal) instead of at import.
RegistrySearchPathError = getattr(R, "RegistrySearchPathError", type("_Missing", (Exception,), {}))

REPO = Path(__file__).resolve().parents[1]
_DSN = os.environ.get("POSTGRES_DSN")

needs_db = pytest.mark.skipif(not _DSN, reason="POSTGRES_DSN required for the DB-backed lane")

# The LEGACY public.entity_aliases shape measured on production 2026-09-14 — NO display_form.
_LEGACY_PUBLIC_DDL = """
CREATE TABLE public.entity_aliases (
    id SERIAL PRIMARY KEY, entity_id TEXT NOT NULL, user_id TEXT NOT NULL, alias TEXT NOT NULL,
    is_preferred BOOLEAN DEFAULT false, created_at TIMESTAMPTZ DEFAULT now(),
    valid_from TIMESTAMPTZ, valid_until TIMESTAMPTZ, preference_source TEXT DEFAULT 'unspecified',
    UNIQUE (user_id, alias));
"""

# The per-tenant shape (src/provisioning/templates/user_schema.sql — the columns the registry
# reads/writes: display_form present, UNIQUE (entity_id, alias), no user_id).
_TENANT_DDL = """
CREATE SCHEMA {sch};
CREATE TABLE {sch}.entities (id TEXT PRIMARY KEY, entity_type TEXT DEFAULT 'unknown', node_role TEXT);
CREATE TABLE {sch}.entity_aliases (
    id SERIAL PRIMARY KEY, entity_id TEXT NOT NULL, alias TEXT NOT NULL, display_form TEXT,
    is_preferred BOOLEAN DEFAULT false, preference_source TEXT DEFAULT 'unspecified',
    created_at TIMESTAMPTZ DEFAULT now(), valid_from TIMESTAMPTZ, valid_until TIMESTAMPTZ,
    UNIQUE (entity_id, alias));
CREATE TABLE {sch}.rel_types (rel_type TEXT PRIMARY KEY, wikidata_pid TEXT);
CREATE TABLE {sch}.entity_name_conflicts (
    id SERIAL PRIMARY KEY, alias TEXT UNIQUE, entity_id_a TEXT, entity_id_b TEXT,
    status TEXT DEFAULT 'pending', created_at TIMESTAMPTZ DEFAULT now());
"""


def _conn():
    conn = psycopg2.connect(_DSN)
    conn.autocommit = False
    if conn.get_dsn_parameters().get("dbname") == "faultline":
        conn.close()
        pytest.fail("REFUSING: POSTGRES_DSN names the owner's real `faultline` database; "
                    "this suite creates/drops public.entity_aliases — use a THROWAWAY db")
    return conn


def _show(conn) -> str:
    with conn.cursor() as c:
        c.execute("SHOW search_path")
        return c.fetchone()[0]


def _count(conn, table: str, alias: str) -> int:
    with conn.cursor() as c:
        c.execute(f"SELECT count(*) FROM {table} WHERE alias = %s", (alias,))  # noqa: S608 — test-owned identifiers
        return c.fetchone()[0]


def _lose_the_binding(conn, sch: str) -> None:
    """Exactly /ingest's shape, then the wound: session-level SET inside the open transaction,
    a failing statement, rollback → the SET is discarded (SET docs) and the path is the default.

    First restores the SESSION default (an earlier resolve() in the same test commits, and a
    committed SET "will persist until the end of the session" — SET docs), so the arm below
    measures the uncommitted-SET-then-rollback case and nothing else."""
    with conn.cursor() as c:
        c.execute("RESET search_path")
    conn.commit()
    with conn.cursor() as c:
        c.execute(f"SET search_path TO {sch}")
    assert _show(conn) == sch
    with pytest.raises(psycopg2.Error):
        with conn.cursor() as c:
            c.execute("SELECT 1/0")
    conn.rollback()
    assert sch not in _show(conn), "precondition: the rollback must have discarded the tenant bind"


@pytest.fixture()
def tenant(monkeypatch):
    """A throwaway tenant schema + the legacy public.entity_aliases, on one autocommit-OFF conn."""
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.delenv("ENTITY_REGISTRY_REQUIRE_SCHEMA", raising=False)
    monkeypatch.setenv("ALIAS_WELD_GUARD", "off")  # the weld guard's probes are not under test
    conn = _conn()
    sch = "faultline_rs_" + uuid.uuid4().hex[:8]
    created_public = False
    with conn.cursor() as c:
        c.execute("SELECT to_regclass('public.entity_aliases') IS NOT NULL")
        if not c.fetchone()[0]:
            c.execute(_LEGACY_PUBLIC_DDL)
            created_public = True
        else:
            c.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
                      "AND table_name='entity_aliases' AND column_name='display_form'")
            if c.fetchone():
                conn.rollback(); conn.close()
                pytest.fail("public.entity_aliases already exists WITH display_form — this is not "
                            "the legacy template shape; refusing to reshape a non-throwaway database")
        c.execute(_TENANT_DDL.format(sch=sch))
    conn.commit()
    try:
        yield conn, sch
    finally:
        conn.rollback()
        with conn.cursor() as c:
            c.execute(f"DROP SCHEMA {sch} CASCADE")
            if created_public:
                c.execute("DROP TABLE public.entity_aliases")
        conn.commit()
        conn.close()


class _CritRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, logger, msg, **kw):
        self.calls.append((msg, kw))


# ══════════════════════════════════════════════════════════════════════════════════════════════
# RS1 / RS2 — rollback-then-resolve lands in the TENANT schema (this is the pre-prod wound)
# ══════════════════════════════════════════════════════════════════════════════════════════════

@needs_db
def test_rollback_then_resolve_lands_in_tenant_schema(tenant):
    """The exact pre-prod sequence. On HEAD's registry this raised
    `UndefinedColumn: column "display_form" does not exist` from public.entity_aliases."""
    conn, sch = tenant
    user = str(uuid.uuid4())
    _lose_the_binding(conn, sch)
    reg = EntityRegistry(conn, auto_commit=False, schema_name=sch)  # main.py:28855's shape
    rid = reg.resolve(user, "amara")
    assert uuid.UUID(rid)
    assert _show(conn) == sch, "the registry must leave the connection bound to the tenant"
    assert _count(conn, f"{sch}.entity_aliases", "amara") == 1
    assert _count(conn, "public.entity_aliases", "amara") == 0


@needs_db
def test_resolve_on_an_aborted_transaction_clears_it_and_binds(tenant):
    """No rollback by the caller at all — the connection is still INSIDE the aborted transaction
    when resolve() is entered (the shape after a failed statement nobody cleared). The seam
    rolls back (logged), re-binds, and the read lands in the tenant."""
    conn, sch = tenant
    user = str(uuid.uuid4())
    with conn.cursor() as c:
        c.execute(f"SET search_path TO {sch}")
    with pytest.raises(psycopg2.Error):
        with conn.cursor() as c:
            c.execute("SELECT 1/0")
    reg = EntityRegistry(conn, auto_commit=False, schema_name=sch)
    rid = reg.resolve(user, "cambridge")
    assert uuid.UUID(rid)
    assert _show(conn) == sch
    assert _count(conn, f"{sch}.entity_aliases", "cambridge") == 1


# ══════════════════════════════════════════════════════════════════════════════════════════════
# POISON — an unqualified entity_aliases read/write can never reach public, on EVERY public method
# ══════════════════════════════════════════════════════════════════════════════════════════════

@needs_db
@pytest.mark.parametrize("method", ["resolve", "register_alias", "get_preferred_name",
                                    "get_any_alias", "get_all_aliases"])
def test_every_public_method_binds_before_its_first_statement(tenant, method):
    """POISON: public.entity_aliases carries a decoy row for the alias under test. After the
    binding is lost, each public method must (a) run with the tenant bound, (b) never see the
    decoy, (c) leave public untouched. On HEAD every method but the write sites trusted the
    connection; `resolve` raised UndefinedColumn on the decoy's table."""
    conn, sch = tenant
    user = str(uuid.uuid4())
    decoy_entity = "poison-" + uuid.uuid4().hex
    with conn.cursor() as c:
        c.execute("INSERT INTO public.entity_aliases (entity_id, user_id, alias, is_preferred) "
                  "VALUES (%s, %s, 'sister', true)", (decoy_entity, user))
    conn.commit()
    # A real tenant row for the read methods to find.
    with conn.cursor() as c:
        c.execute(f"SET search_path TO {sch}")
    bound = EntityRegistry(conn, auto_commit=False, schema_name=sch)
    real_entity = bound.resolve(user, "sister")
    conn.commit()

    _lose_the_binding(conn, sch)
    reg = EntityRegistry(conn, auto_commit=False, schema_name=sch)
    if method == "resolve":
        out = reg.resolve(user, "sister")
        assert out == real_entity and out != decoy_entity
    elif method == "register_alias":
        res = reg.register_alias(real_entity, "sis", is_preferred=False, preference_source="user_stated")
        assert res["written"] is True
        assert _count(conn, f"{sch}.entity_aliases", "sis") == 1
        assert _count(conn, "public.entity_aliases", "sis") == 0
    elif method == "get_preferred_name":
        assert reg.get_preferred_name(real_entity) == "sister"
        assert reg.get_preferred_name(decoy_entity) == decoy_entity  # not found in the tenant
    elif method == "get_any_alias":
        assert reg.get_any_alias(real_entity) == "sister"
        assert reg.get_any_alias(decoy_entity) is None
    elif method == "get_all_aliases":
        assert reg.get_all_aliases(real_entity) == ["sister"]
        assert reg.get_all_aliases(decoy_entity) == []
    assert _show(conn) == sch, f"{method} left the connection unbound"
    with conn.cursor() as c:
        c.execute("SELECT count(*) FROM public.entity_aliases WHERE alias='sister'")
        assert c.fetchone()[0] == 1, "public row count changed — a write reached public"


# ══════════════════════════════════════════════════════════════════════════════════════════════
# RS3 — fail loud: an unbound registry refuses under per-tenant operation, names its birthplace
# ══════════════════════════════════════════════════════════════════════════════════════════════

@needs_db
@pytest.mark.parametrize("env", ["SAAS_MODE", "ENTITY_REGISTRY_REQUIRE_SCHEMA"])
def test_unbound_registry_refuses_under_per_tenant_operation(tenant, monkeypatch, env):
    """`EntityRegistry(conn)` — main.py:14038's old shape — cannot re-bind anything. Under
    SAAS_MODE (production sets it) it must REFUSE with a CRIT naming the construction site,
    never read whatever schema the connection drifted to. Nothing is written anywhere."""
    conn, sch = tenant
    monkeypatch.setenv(env, "1")
    rec = _CritRecorder()
    monkeypatch.setattr(R, "log_crit", rec)
    _lose_the_binding(conn, sch)
    reg = EntityRegistry(conn, auto_commit=False)  # ← the construction-site defect
    this_line = _line_of(__file__, "reg = EntityRegistry(conn, auto_commit=False)  # ← the construction-site defect")
    with pytest.raises(RegistrySearchPathError) as ei:
        reg.resolve(str(uuid.uuid4()), "amara")
    assert "unbound_registry_refused" in str(ei.value)
    assert rec.calls, "refusal must be CRIT-logged, not just raised"
    msg, kw = rec.calls[0]
    site = kw.get("construction_site", "")
    assert site == f"{Path(__file__).name}:{this_line}::{test_unbound_registry_refuses_under_per_tenant_operation.__name__}", site
    assert kw.get("op") == "resolve"
    assert _count(conn, "public.entity_aliases", "amara") == 0
    assert _count(conn, f"{sch}.entity_aliases", "amara") == 0
    # every public DB-touching method refuses the same way
    for call in (lambda: reg.register_alias("x", "y"), lambda: reg.get_preferred_name("x"),
                 lambda: reg.get_any_alias("x"), lambda: reg.get_all_aliases("x")):
        with pytest.raises(RegistrySearchPathError):
            call()


def _line_of(path: str, needle: str) -> int:
    for i, line in enumerate(Path(path).read_text().splitlines(), 1):
        if needle in line:
            return i
    raise AssertionError(f"needle not found: {needle!r}")


def test_legacy_single_tenant_path_is_preserved_and_warned_once(monkeypatch):
    """No SAAS_MODE, no schema_name: the documented legacy path — the registry proceeds on the
    connection's OWN search_path (a single-tenant box / a unit test with a fake connection) and
    warns ONCE per instance, naming the construction site. Never silent, never refusing."""
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.delenv("ENTITY_REGISTRY_REQUIRE_SCHEMA", raising=False)
    warnings = []
    monkeypatch.setattr(R.log, "warning", lambda evt, **kw: warnings.append((evt, kw)))
    rec = _CritRecorder()
    monkeypatch.setattr(R, "log_crit", rec)

    class _Cur:
        executed = []

        def execute(self, sql, params=None):
            self.executed.append(sql)

        def fetchone(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

    user = str(uuid.uuid4())
    reg = EntityRegistry(_Conn())
    assert reg.resolve(user, "me") == user           # the pronoun branch: INSERT on the conn's path
    assert reg.get_preferred_name("abc") == "abc"    # a read: runs, finds nothing
    legacy = [w for w in warnings if w[0] == "entity_registry.unbound_legacy_path"]
    assert len(legacy) == 1, "exactly ONE legacy warning per instance"
    assert legacy[0][1]["construction_site"].startswith(Path(__file__).name + ":")
    assert not rec.calls, "the legacy path is a warning, never a CRIT"
    assert not any(sql.startswith("SET search_path") for sql in _Cur.executed), \
        "unbound → nothing to SET; the connection's own search_path is used as documented"


def test_malformed_schema_name_is_refused_at_construction():
    """The schema name is interpolated into SET — the same ^[a-z0-9_]+$ rule as the gate applies
    at construction, so a malformed name can never reach the SET."""
    class _Conn:
        pass
    with pytest.raises(ValueError):
        EntityRegistry(_Conn(), schema_name="faultline_x; DROP SCHEMA public")
    with pytest.raises(ValueError):
        EntityRegistry(_Conn(), schema_name="Faultline_Upper")
    EntityRegistry(_Conn(), schema_name="faultline_ok_1")  # well-formed: accepted


# ══════════════════════════════════════════════════════════════════════════════════════════════
# RS2 census — one bind seam in registry.py; every construction site in src/ passes schema_name
# ══════════════════════════════════════════════════════════════════════════════════════════════

def test_registry_has_exactly_one_set_search_path_statement():
    """The six inline asserts collapsed into `_bind_tenant_schema`. A second executable
    `SET search_path` in this module is a per-site copy-paste creeping back."""
    src = (REPO / "src/entity_registry/registry.py").read_text()
    executable = [ln for ln in src.splitlines() if 'execute(f"SET search_path' in ln]
    assert len(executable) == 1, executable


def test_every_public_db_method_enters_through_the_bind_seam():
    """Source census: each public DB-touching method of EntityRegistry calls the seam BEFORE its
    first cursor. (get_surrogate_for_user / get_canonical_for_user touch no DB.)"""
    src = (REPO / "src/entity_registry/registry.py").read_text()
    for meth in ("resolve", "register_alias", "get_preferred_name", "get_any_alias",
                 "get_all_aliases", "_get_valid_entity_types"):
        m = re.search(rf"\n    def {meth}\(.*?(?=\n    def |\Z)", src, re.S)
        assert m, meth
        body = m.group(0)
        bind = body.find("self._bind_tenant_schema(")
        first_cursor = body.find("self.db_conn.cursor()")
        assert bind != -1, f"{meth} never binds"
        assert first_cursor == -1 or bind < first_cursor, f"{meth} opens a cursor before binding"


_KNOWN_FOSSIL = {
    # main.py::ingest prerequisite-staging: `EntityRegistry(dsn=...)` — a kwarg the constructor
    # has never accepted (TypeError), followed by `resolve(..., type_hint=)` which does not exist
    # either. Dead by construction, swallowed by its enclosing except. Reported for deletion in
    # DEV/review/gauntlet/registry-searchpath-fallthrough/round_1/REPORT.md — NOT a live path.
    'EntityRegistry(dsn=os.environ.get("POSTGRES_DSN", ""))',
}


def test_every_construction_site_in_src_passes_schema_name():
    """Constraint 13 — audit every path. A bare `EntityRegistry(conn)` in src/ or saas/ is the
    main.py:14038 defect coming back: under SAAS_MODE it refuses every call."""
    offenders = []
    for root in ("src", "saas"):
        for p in (REPO / root).rglob("*.py"):
            if p.name == "registry.py" or "schema_oracle" in p.parts:
                continue  # the class itself / a different EntityRegistry (registry_data=)
            for i, line in enumerate(p.read_text().splitlines(), 1):
                if "EntityRegistry(" not in line or line.lstrip().startswith("#"):
                    continue
                if any(f in line for f in _KNOWN_FOSSIL):
                    continue
                # a site may spread over two lines — join the next one. Accepts the kwarg form
                # (`schema_name=`) and the dict-splat form main.py:14038 uses when the schema is
                # only derivable from a UUID user_id (`{"schema_name": …}`).
                lines = p.read_text().splitlines()
                window = line + (lines[i] if i < len(lines) else "")
                if not re.search(r'schema_name\s*=|"schema_name"\s*:', window):
                    offenders.append(f"{p.relative_to(REPO)}:{i}: {line.strip()}")
    assert not offenders, "construction sites without schema_name:\n" + "\n".join(offenders)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# ROUND 2 — the registry's OWN rollback paths re-bind for the caller (`_rebind_after_rollback`)
# ══════════════════════════════════════════════════════════════════════════════════════════════

def _seed_collision(conn, sch):
    """Entity A holds 'sam' as a user_stated PREFERRED alias; a second entity B will claim it at
    EQUAL rank → the collision-staging branch of register_alias. The tenant's
    entity_name_conflicts table is DROPPED so the staging INSERT fails deterministically, which
    drives register_alias's own `rollback()` (the collision except path) mid-write."""
    a, b = "ent-a-" + uuid.uuid4().hex[:8], "ent-b-" + uuid.uuid4().hex[:8]
    with conn.cursor() as c:
        c.execute(f"INSERT INTO {sch}.entities (id) VALUES (%s), (%s)", (a, b))
        c.execute(f"INSERT INTO {sch}.entity_aliases (entity_id, alias, is_preferred, preference_source) "
                  "VALUES (%s, 'sam', true, 'user_stated')", (a,))
        c.execute(f"DROP TABLE {sch}.entity_name_conflicts")
    conn.commit()
    return a, b


@needs_db
def test_registry_own_rollback_rebinds_so_the_next_unqualified_read_lands_in_tenant(tenant):
    """register_alias hits its collision except path → `self.db_conn.rollback()` — which
    discards the ENTRY bind (uncommitted SET) — then `_rebind_after_rollback` re-binds. The
    write that follows inside register_alias, and the CALLER's next unqualified read on the same
    connection, must both land in the tenant. Under a no-op `_rebind_after_rollback` the
    post-rollback alias INSERT runs on public (UndefinedColumn display_form) — see the mutant
    pin directly below."""
    conn, sch = tenant
    a, b = _seed_collision(conn, sch)
    _lose_the_binding(conn, sch)
    reg = EntityRegistry(conn, auto_commit=False, schema_name=sch)
    res = reg.register_alias(b, "sam", is_preferred=True, preference_source="user_stated")
    assert res["written"] is True
    # The CALLER's next unqualified read (this is what /ingest does after the registry returns).
    with conn.cursor() as c:
        c.execute("SELECT entity_id FROM entity_aliases WHERE alias = 'sam' ORDER BY entity_id")
        holders = [r[0] for r in c.fetchall()]
    assert holders == sorted([a, b]), holders
    assert _show(conn) == sch
    assert _count(conn, "public.entity_aliases", "sam") == 0


@needs_db
def test_rebind_after_rollback_is_load_bearing_mutant_pin(tenant, monkeypatch):
    """MUTATION PIN: with `_rebind_after_rollback` replaced by a no-op, the identical scenario
    must NOT succeed — the rollback inside register_alias strips the bind and the following
    alias INSERT reaches public.entity_aliases (no display_form) and raises. If this test ever
    goes green with the mutant in place, the seam has stopped being the thing that protects
    the post-rollback write."""
    conn, sch = tenant
    a, b = _seed_collision(conn, sch)
    _lose_the_binding(conn, sch)
    monkeypatch.setattr(EntityRegistry, "_rebind_after_rollback", lambda self, op: None)
    reg = EntityRegistry(conn, auto_commit=False, schema_name=sch)
    with pytest.raises(psycopg2.Error) as ei:
        reg.register_alias(b, "sam", is_preferred=True, preference_source="user_stated")
    # Measured shape of the mutant failure: the first post-rollback statement lands on
    # public.entity_aliases and fails (UndefinedColumn display_form); a SAVEPOINT-guarded probe
    # swallows that first error, so the surfaced exception is the follow-on
    # InFailedSqlTransaction. Either way: psycopg2.Error, and B's alias never lands anywhere.
    assert isinstance(ei.value, (psycopg2.errors.UndefinedColumn, psycopg2.errors.InFailedSqlTransaction)), \
        type(ei.value).__name__
    conn.rollback()
    assert _count(conn, f"{sch}.entity_aliases", "sam") == 1   # only A's row; B's never landed
    assert _count(conn, "public.entity_aliases", "sam") == 0


# ══════════════════════════════════════════════════════════════════════════════════════════════
# ROUND 2 — the UPSTREAM defect: /ingest's own bind was never committed, and the txn barrier
# `db_read.release_read_transaction` (main.py::ingest, "ingest_retype_subject_type_probe")
# rolls back a pure-read transaction — discarding an UNCOMMITTED bind.
# ══════════════════════════════════════════════════════════════════════════════════════════════

@needs_db
def test_release_read_transaction_discards_an_uncommitted_bind_but_keeps_a_committed_one(tenant):
    """`release_read_transaction`'s docstring assumes "the shape every per-tenant caller uses:
    SET search_path TO <schema> then commit()". /ingest did NOT commit — so the barrier's
    INTRANS-without-xid rollback reverted the path to `"$user", public` in front of the object
    resolution. Committed (the round-2 main.py::ingest fix), the bind survives the barrier."""
    from src.api.db_read import release_read_transaction
    conn, sch = tenant
    with conn.cursor() as c:
        c.execute("RESET search_path")
    conn.commit()
    # (1) /ingest at HEAD: SET inside the open transaction, no commit, a pure read, then the barrier
    with conn.cursor() as c:
        c.execute(f"SET search_path TO {sch}")
        c.execute("SELECT 1")
    assert release_read_transaction(conn, context="test_uncommitted") is True
    assert sch not in _show(conn), "an uncommitted bind is discarded by the barrier's rollback"
    # (2) /ingest after the fix: SET then commit, a pure read, then the barrier
    with conn.cursor() as c:
        c.execute(f"SET search_path TO {sch}")
    conn.commit()
    with conn.cursor() as c:
        c.execute("SELECT 1")
    assert release_read_transaction(conn, context="test_committed") is True
    assert _show(conn) == sch, "a committed bind survives the barrier's rollback"


def _code_lines(src: str) -> str:
    return "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))


def test_ingest_commits_its_tenant_bind_on_both_branches():
    """Source pin on main.py::ingest (comments stripped — a code-shape pin, not a comment pin):
    each `SET search_path TO {schema_name}` in the bind block is followed by `db.commit()`
    before the branch's log line."""
    src = (REPO / "src/api/main.py").read_text()
    start = src.index("ingest.schema_search_path_set") - 4000
    block = _code_lines(src[start: src.index("ingest.schema_context_fallback_set") + 200])
    assert re.search(r'SET search_path TO \{schema_name\}"\)\s*\n\s*db\.commit\(\)\s*\n\s*'
                     r'log\.info\("ingest\.schema_search_path_set"', block), "primary bind not committed"
    assert re.search(r'SET search_path TO \{schema_name\}"\)\s*\n\s*db\.commit\(\)\s*\n\s*'
                     r'log\.info\("ingest\.schema_context_fallback_set"', block), "fallback bind not committed"


def test_query_impl_commits_its_tenant_bind():
    """Same code-shape pin for main.py::_query_impl: the round-2 critic found its bind at the
    `query.schema_context_set` site was an UNCOMMITTED session SET with a conditional
    `db.rollback()` (Class-C hit-increment failure path) and unqualified reads after it — the
    /ingest shape one handler over. The bind must be followed by `db.commit()`."""
    src = (REPO / "src" / "api" / "main.py").read_text(encoding="utf-8")
    start = src.index("query.schema_context_set") - 1500
    block = _code_lines(src[start: src.index("query.schema_context_set") + 200])
    assert re.search(r'SET search_path TO \{schema_name\}"\)\s*\n\s*db\.commit\(\)\s*\n\s*'
                     r'log\.info\("query\.schema_context_set"', block), "_query_impl bind not committed"
