"""DOCUMENT-lane tenant-brain bind contract (regression: silent brainless drain).

THE REPRO this pins (pre-prod, 2026-10-21 02:40 UTC, seat c591f22b, account 0b1a09e6 WITH a
bound BYO brain): a document was drained to ``committed=0 staged=6`` and the operator could
not tell from the bind site whether the lane had a brain at all, because

  1. ``embedder._reembedder_bind_tenant_brain`` swallowed EVERY bind failure in a bare
     ``except Exception: pass`` -- and even an exception-free bind could resolve to NOTHING
     (``bind_schema_brain_absolute`` never raises; it resolves internally to None/no-brain on
     any lookup failure), so a brainless lane was structurally silent; and
  2. the fast doc-drain prepass (``fast_drain_pending_documents_prepass`` -- the standalone
     doc-drain daemon's WHOLE life under ``DOC_DRAIN_STANDALONE``) bound the tenant brain but
     NEVER cleared it, so the last drained tenant's brain stayed resident in the daemon
     thread's context forever -- the latent cross-tenant leak the in-order walk (clears at
     end of cycle) and the supervisor's ``_run_tenant_work_cycle`` (clears in its own
     ``finally``) both already close.

Verified live before writing this: the resolution chain (``user_provisioning`` ->
``account_users`` active -> ``accounts`` active/tenant -> ``account_llm_config``) is HEALTHY
for this seat, so the contract tests below wire the SAME seams the suite already uses
(``tests/test_saas_reembedder_brain_bind.py`` ``_wire_account`` pattern) and assert the DOC
lane's contract:

  A. behavioural -- the prepass binds the tenant brain (and the seat identity) BEFORE
     ``drain_pending_documents`` runs, the bound endpoint RESOLVES (never an ``(unset)``
     endpoint) at drain time, and the brain + identity are CLEARED after -- including when
     the drain RAISES.
  B. loudness -- a bind failure logs ``tenant_brain_bind_failed`` with the schema AND the
     exception class instead of passing silently; a bind that resolves nothing logs
     ``tenant_brain_bind_unresolved`` / ``tenant_brain_bind_no_routable_brain``. Fail-safe
     is kept (never raises into a background loop); silence is not.
  C. structural -- every doc-drain entry point (fast prepass, in-order walk, supervisor)
     binds before the drain, and the prepass + supervisor clear in a ``finally``.

Pure unit/seam tests: DB + network seams monkeypatched, no live services.

Run: python3 tools/fltest.py --test tests/test_doc_lane_brain_bind_contract.py
"""

import logging
import types
from pathlib import Path

import pytest
import psycopg2

from saas import llm_config as saas_llm_config
from saas import llm_context as saas_llm_context

from src.api import llm_client
from src.re_embedder import embedder
from src.re_embedder import supervisor as embedder_supervisor


# The LIVE repro seat -- a real UUID-shaped schema, so `_seat_user_id_for_schema` derives the
# seat identity from it exactly as production does.
SEAT = "c591f22b-4f2a-4c3b-9d1e-c11cba7f975f"
SCHEMA = "faultline_c591f22b_4f2a_4c3b_9d1e_c11cba7f975f"
ACCT = "0b1a09e6-eb67-40ac-9194-16b98ac60610"
TENANT_BASE_URL = "http://192.168.40.20:8080/v1"
TENANT_CFG = {
    "account_id": ACCT,
    "backend_type": "openai",
    "base_url": TENANT_BASE_URL,
    "model": "Qwen3.6-35B-A3B-Q4_K_M",
    "api_key": "byo-secret",
}


@pytest.fixture(autouse=True)
def _clean_context():
    saas_llm_config.invalidate_config_cache()
    saas_llm_context.invalidate_schema_account_cache()
    saas_llm_context._current_llm_config.set(None)
    embedder._current_seat_user_id = None
    yield
    saas_llm_config.invalidate_config_cache()
    saas_llm_context.invalidate_schema_account_cache()
    saas_llm_context._current_llm_config.set(None)
    embedder._current_seat_user_id = None


def _wire_bound_account(monkeypatch):
    """Wire the schema->seat->account->config seams for an account WITH a bound brain.

    Same three live seams the sibling suite patches (see
    tests/test_saas_reembedder_brain_bind.py `_wire_account` and its DEAD-MOCK REPAIR note):
    ``_seat_for_schema`` (the seam bind_schema_brain_absolute actually reads since migration
    202), the account resolver, and ``seat_scoped`` (which would otherwise do its OWN
    control-plane read and fail towards seat-scoped). The account here is CORPORATE
    (``seat_scoped`` False), so the shared account row is the lane under test.
    """
    monkeypatch.setenv("SAAS_MODE", "true")
    monkeypatch.setenv("LLM_BACKEND_TYPE", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "http://open-webui:8080")
    monkeypatch.setattr(
        saas_llm_context, "_seat_for_schema",
        lambda schema: (SEAT, ACCT) if schema == SCHEMA else (None, None))
    monkeypatch.setattr(
        saas_llm_config, "resolve_account_llm_config", lambda account_id: TENANT_CFG)
    from saas import seat_brain as saas_seat_brain
    monkeypatch.setattr(saas_seat_brain, "seat_scoped", lambda account_id: False)


class _FakeCursor:
    """Scripted cursor: answers the prepass pending-probe SELECT with one row."""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, *a, **k):
        self._conn.last_sql = sql

    def fetchone(self):
        # The prepass probe (BUG-005 widened it to the reclaimable status pair):
        # SELECT 1 FROM documents WHERE status IN ('pending', 'processing') LIMIT 1
        if "status IN ('pending', 'processing')" in (self._conn.last_sql or ""):
            return (1,)
        return None

    def fetchall(self):
        return []


class _FakeConn:
    """Minimal psycopg2 connection: context manager + cursor + txn no-ops."""

    def __init__(self):
        self.last_sql = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self, *a, **k):
        return _FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _fake_psycopg2(monkeypatch):
    """Replace the psycopg2 module INSIDE embedder only (prepass connect + Error)."""
    fake = types.SimpleNamespace(Error=psycopg2.Error,
                                 connect=lambda dsn: _FakeConn())
    monkeypatch.setattr(embedder, "psycopg2", fake)
    return fake


# =====================================================================
# A -- behavioural: bind BEFORE the first chunk harvest, endpoint RESOLVED,
#      clear AFTER (including on a raising drain)
# =====================================================================

def test_doc_prepass_binds_before_drain_endpoint_resolved_and_clears_after(monkeypatch):
    _wire_bound_account(monkeypatch)
    _fake_psycopg2(monkeypatch)
    seen = {}

    def _fake_drain(db_conn, backend_url, user_id, schema_name=None,
                    statement_route="rewrite", batch_size=None):
        # AT HARVEST TIME the brain must be bound and routable -- never "(unset)".
        cfg = saas_llm_context.current_llm_config()
        assert cfg is not None, "doc drain ran with NO brain bound"
        assert cfg.get("base_url") == TENANT_BASE_URL
        assert not cfg.get("no_brain")
        assert llm_client.get_backend_endpoint() == \
            TENANT_BASE_URL.rstrip("/") + "/chat/completions", \
            "doc-lane bind did not resolve a routable endpoint"
        assert embedder._reembedder_llm_user_id() == SEAT, \
            "doc drain ran without the seat identity set"
        seen["drained"] = True
        return 1

    monkeypatch.setattr(embedder, "drain_pending_documents", _fake_drain)

    n = embedder.fast_drain_pending_documents_prepass(
        "postgres://unused", [(SEAT, SCHEMA)], set(), "http://backend:8000",
        statement_route="rewrite")

    assert n == 1 and seen.get("drained")
    # THE CLEAR HALF: nothing may outlive the tenant it was bound for.
    assert embedder._current_seat_user_id is None
    assert embedder._reembedder_llm_user_id() == embedder._NO_SEAT_USER_ID
    assert llm_client.get_backend_endpoint() is None  # SAAS on: cleared, not global


def test_doc_prepass_clears_brain_even_when_drain_raises(monkeypatch):
    _wire_bound_account(monkeypatch)
    _fake_psycopg2(monkeypatch)

    def _exploding_drain(*a, **k):
        raise RuntimeError("backend unreachable")

    monkeypatch.setattr(embedder, "drain_pending_documents", _exploding_drain)

    # Per-tenant fail-safe: the prepass logs and moves on (returns 0), never raises.
    n = embedder.fast_drain_pending_documents_prepass(
        "postgres://unused", [(SEAT, SCHEMA)], set(), "http://backend:8000")

    assert n == 0
    # And the bind did NOT outlive the failed drain -- no leak into the next tenant.
    assert embedder._current_seat_user_id is None
    assert llm_client.get_backend_endpoint() is None


# =====================================================================
# B -- loudness: a bind failure / brainless resolution is logged, never silent
# =====================================================================

def test_bind_failure_logs_loudly_with_schema_and_exception_class(monkeypatch, caplog):
    _wire_bound_account(monkeypatch)

    def _boom(schema_name):
        raise RuntimeError("saas control plane unreachable")

    monkeypatch.setattr(saas_llm_context, "bind_schema_brain_absolute", _boom)

    with caplog.at_level(logging.WARNING):
        # Must NOT raise into the background loop (fail-safe kept)...
        embedder._reembedder_bind_tenant_brain(SCHEMA)

    # ...but it must NEVER be silent again: schema + exception class on the wire.
    assert "tenant_brain_bind_failed" in caplog.text
    assert SCHEMA in caplog.text
    assert "RuntimeError" in caplog.text
    assert "saas control plane unreachable" in caplog.text


def test_brainless_resolution_logs_loudly_no_routable_brain(monkeypatch, caplog):
    # Account resolves but has NO brain configured -> the no-brain sentinel path.
    monkeypatch.setenv("SAAS_MODE", "true")
    monkeypatch.setenv("LLM_BACKEND_TYPE", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "http://open-webui:8080")
    monkeypatch.setattr(saas_llm_context, "_seat_for_schema",
                        lambda schema: (SEAT, ACCT) if schema == SCHEMA else (None, None))
    monkeypatch.setattr(saas_llm_config, "resolve_account_llm_config",
                        lambda account_id: None)
    from saas import seat_brain as saas_seat_brain
    monkeypatch.setattr(saas_seat_brain, "seat_scoped", lambda account_id: False)

    with caplog.at_level(logging.WARNING):
        embedder._reembedder_bind_tenant_brain(SCHEMA)

    assert "tenant_brain_bind_no_routable_brain" in caplog.text
    assert SCHEMA in caplog.text
    # The sentinel is still bound (fail-closed contract unchanged) -- the log is additive.
    cfg = saas_llm_context.current_llm_config()
    assert cfg is not None and cfg.get("no_brain") is True


def test_unresolved_chain_logs_loudly_when_no_account_resolves(monkeypatch, caplog):
    # The chain itself breaks (no owning ACTIVE account) -> nothing bound at all.
    monkeypatch.setenv("SAAS_MODE", "true")
    monkeypatch.setattr(saas_llm_context, "_seat_for_schema", lambda schema: (None, None))

    with caplog.at_level(logging.WARNING):
        embedder._reembedder_bind_tenant_brain(SCHEMA)

    assert "tenant_brain_bind_unresolved" in caplog.text
    assert SCHEMA in caplog.text


def test_oss_no_saas_package_stays_silent(monkeypatch, caplog):
    """The PUBLIC CORE has no saas package at all — that is this helper's documented no-op
    state, and it must stay byte-for-byte SILENT there (adversarial-review defect 1: warning
    on the import miss would stream un-actionable lines per tenant per cycle on every OSS
    rig). Only the exact 'saas' miss is silent; a missing transitive dep is a real failure
    and the next test pins that it stays loud. Simulates the OSS distribution by blocking
    saas* at the meta_path after evicting it from sys.modules."""
    import sys

    saas_keys = [k for k in list(sys.modules) if k == "saas" or k.startswith("saas.")]
    for k in saas_keys:
        monkeypatch.delitem(sys.modules, k, raising=False)

    class _SaasBlocker:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "saas" or fullname.startswith("saas."):
                raise ModuleNotFoundError("No module named 'saas'", name="saas")
            return None

    monkeypatch.setattr(sys, "meta_path", [_SaasBlocker()] + list(sys.meta_path))

    with caplog.at_level(logging.WARNING):
        embedder._reembedder_bind_tenant_brain(SCHEMA)

    assert "tenant_brain_bind_failed" not in caplog.text
    assert "tenant_brain_bind_unresolved" not in caplog.text
    assert "tenant_brain_bind_no_routable_brain" not in caplog.text
    # The identity is set FIRST and unconditionally — even on the OSS no-op path.
    assert embedder._current_seat_user_id == SEAT


def test_non_saas_import_breakage_stays_loud(monkeypatch, caplog):
    """An import breakage that is NOT the OSS saas-miss is a REAL failure: loud, not silent.
    Simulated at the same import statement (module set to None -> ImportError on import);
    only the exact 'No module named 'saas'' miss takes the silent OSS branch."""
    import sys

    monkeypatch.setitem(sys.modules, "saas.llm_context", None)
    _wire_bound_account(monkeypatch)

    with caplog.at_level(logging.WARNING):
        embedder._reembedder_bind_tenant_brain(SCHEMA)

    assert "tenant_brain_bind_failed" in caplog.text
    assert SCHEMA in caplog.text
    # ModuleNotFoundError (an ImportError subclass) — its OWN class name in the log, and it
    # must NOT have taken the silent OSS branch (the message is not the saas miss).
    assert "ModuleNotFoundError" in caplog.text
    assert "No module named 'saas'" not in caplog.text


def test_saas_off_bind_stays_quiet_noop(monkeypatch, caplog):
    """SAAS off: the bind is a byte-for-byte no-op and the loudness layer must NOT fire."""
    _wire_bound_account(monkeypatch)
    monkeypatch.delenv("SAAS_MODE", raising=False)

    with caplog.at_level(logging.WARNING):
        embedder._reembedder_bind_tenant_brain(SCHEMA)

    assert "tenant_brain_bind_failed" not in caplog.text
    assert "tenant_brain_bind_no_routable_brain" not in caplog.text
    assert "tenant_brain_bind_unresolved" not in caplog.text


# =====================================================================
# C -- structural: every doc-drain entry point binds, and the lifetimes close
# =====================================================================

def _embedder_lines():
    return Path(embedder.__file__).read_text().splitlines()


def _function_region(lines, def_signature):
    start = next(i for i, ln in enumerate(lines) if ln.startswith(def_signature))
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("def ")), len(lines))
    return start, end


def _enclosing_ready_schemas_loop(lines, call_idx):
    for i in range(call_idx, -1, -1):
        if "for " in lines[i] and "ready_schemas" in lines[i]:
            return i
    return None


def test_structural_prepass_binds_before_drain_and_clears_in_finally():
    lines = _embedder_lines()
    start, end = _function_region(lines, "def fast_drain_pending_documents_prepass(")
    region = lines[start:end]
    bind = next(i for i, ln in enumerate(region) if "_reembedder_bind_tenant_brain(" in ln)
    drain = next(i for i, ln in enumerate(region) if "drain_pending_documents(" in ln)
    assert bind < drain, "prepass must bind the tenant brain BEFORE the drain (first harvest)"
    # The bind lifetime closes in the SAME per-tenant try: a finally-clear, not a
    # best-effort clear on the success path only.
    finally_idx = next(i for i, ln in enumerate(region) if ln.strip() == "finally:")
    clear = next(i for i, ln in enumerate(region) if "_reembedder_clear_tenant_brain()" in ln)
    assert drain < finally_idx < clear, \
        "prepass must clear the brain in a finally AFTER the drain"


def test_structural_in_order_walk_binds_before_doc_drain():
    lines = _embedder_lines()
    # The in-order walk drain call: `n_docs = drain_pending_documents(` inside main().
    drain_idx = next(i for i, ln in enumerate(lines)
                     if ("n_docs = drain_pending_documents(" in ln)
                     and not ln.lstrip().startswith("#"))
    loop_start = _enclosing_ready_schemas_loop(lines, drain_idx)
    assert loop_start is not None, "walk drain call must sit inside a ready_schemas loop"
    region = "\n".join(lines[loop_start:drain_idx])
    assert "_reembedder_bind_tenant_brain(" in region, \
        "in-order walk must bind the tenant brain before its document drain"


def test_structural_supervisor_binds_and_clears_in_finally():
    src = Path(embedder_supervisor.__file__).read_text()
    start = src.index("def _run_tenant_work_cycle(")
    region = src[start:]
    bind = region.index("embedder._reembedder_bind_tenant_brain(schema_name)")
    phases = region.index("_run_phases_on_conn(")
    clear = region.index("embedder._reembedder_clear_tenant_brain()")
    assert bind < phases, "supervisor must bind before running the phases (incl. drain)"
    finally_idx = region.index("finally:")
    assert bind < finally_idx < clear, \
        "supervisor must clear the brain in a finally after the phases"


# ======================================================================
# D -- end-to-end through the REAL resolution chain (anti-dead-mock):
#      only saas.db.connection is faked; the REAL _seat_for_schema SQL,
#      the REAL bind_schema_brain_absolute, and the REAL prepass all run.
#      (The sibling suite's DEAD-MOCK REPAIR note is the precedent: a pin
#      above a seam the code no longer reads proves nothing.)
# ======================================================================

class _ChainCursor:
    """Answers the REAL _seat_for_schema SQL with the live-repro row shape."""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, *a, **k):
        self._conn.last_sql = sql
        self._conn.last_params = a[0] if a else None

    def fetchone(self):
        sql = self._conn.last_sql or ""
        if "FROM public.user_provisioning" in sql and "account_users" in sql:
            assert self._conn.last_params == (SCHEMA,), \
                "the chain must key on the schema name"
            return (SEAT, ACCT)
        if "account_kind" in sql:
            # accounts.account_kind = 'tenant' — a SHARED_BRAIN_KIND, so seat_brain resolves
            # ACCOUNT scope (the live-repro governance shape; fail direction is seat-scope,
            # so this row is what keeps the corporate lane under test).
            return ("tenant",)
        if "status IN ('pending', 'processing')" in sql:
            return (1,)
        return None

    def fetchall(self):
        return []


class _ChainConn(_FakeConn):
    def cursor(self, *a, **k):
        return _ChainCursor(self)


def test_end_to_end_real_chain_binds_and_doc_drain_sees_it(monkeypatch):
    """REAL _seat_for_schema SQL + REAL bind_schema_brain_absolute + REAL prepass:
    only the saas.db connection seam is faked."""
    monkeypatch.setenv("SAAS_MODE", "true")
    monkeypatch.setenv("LLM_BACKEND_TYPE", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "http://open-webui:8080")
    conn = _ChainConn()
    fake = types.SimpleNamespace(Error=psycopg2.Error,
                                 connect=lambda dsn: conn)
    monkeypatch.setattr(embedder, "psycopg2", fake)

    from saas import db as saas_db
    import contextlib

    @contextlib.contextmanager
    def _fake_db_connection():
        yield conn

    monkeypatch.setattr(saas_db, "connection", _fake_db_connection)
    # REAL so far: _seat_for_schema runs its actual SQL against the scripted cursor and
    # bind_schema_brain_absolute runs unmodified. Patched at its MODULE boundary (one level,
    # deliberately): resolve_account_llm_config -- its SSRF guard + Fernet decrypt internals
    # are their own layer with their own suite; the chain under test here is
    # schema -> seat -> account -> bind -> drain. seat_brain still runs for real and its
    # individual_users governance read scripts to "no row" -> governed -> ACCOUNT scope, the
    # live-repro shape.
    monkeypatch.setattr(saas_llm_config, "resolve_account_llm_config",
                        lambda account_id: TENANT_CFG)

    seen = {}

    def _fake_drain(db_conn, backend_url, user_id, schema_name=None,
                    statement_route="rewrite", batch_size=None):
        cfg = saas_llm_context.current_llm_config()
        assert cfg is not None and cfg.get("base_url") == TENANT_BASE_URL, \
            "REAL chain did not bind the tenant brain before the doc drain"
        assert not cfg.get("no_brain")
        seen["ok"] = True
        return 1

    monkeypatch.setattr(embedder, "drain_pending_documents", _fake_drain)

    n = embedder.fast_drain_pending_documents_prepass(
        "postgres://unused", [(SEAT, SCHEMA)], set(), "http://backend:8000")

    assert n == 1 and seen.get("ok")
    assert saas_llm_context.current_llm_config() is None  # cleared after
