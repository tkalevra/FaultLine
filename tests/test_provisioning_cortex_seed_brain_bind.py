"""Provisioning cortex seed MUST bind the seat's OWN brain before seeding (BYOM audit GAP-1).

THE BUG this pins: the provisioning cortex step chain (``schema_manager.create_cortex_schema``
-> ``cortex.seed_usage_patterns`` -> ``_embed_cortex_text`` -> ``llm_client.get_llm_chat_url()``)
ran with NO brain bound anywhere in the provisioning context. Under SAAS every new seat's seed
embedding was refused (``saas.global_brain_refused account_id=(unresolved)``, the observed ~60s
CRIT cadence) and even a PAID tenant's cortex seed landed keyword-only.

THE FIX: ``create_cortex_schema`` binds the provisioning seat's own brain via the SAME seam the
backend request path uses (``main.py::_saas_bind_tenant_brain`` -> ``saas.llm_context
.bind_request_brain``; user_id is in hand at this call site — chosen over
``bind_schema_brain_absolute`` because the CORTEX schema ``flagent_<uuid>`` is NOT a
``user_provisioning.schema_name``, so the schema-keyed seam would resolve nothing here), and
clears it in a ``finally`` (the ``_reembedder_clear_tenant_brain`` discipline). Contract:
  1. bind BEFORE the seed, clear AFTER — including when the seed raises;
  2. a bind failure warns LOUDLY (``provisioning.tenant_brain_seed_bind_failed``) and the seed
     still completes keyword-only — fail-safe, never silent-downgrade, never a raise;
  3. the exact saas-absent OSS miss stays SILENT; any other ModuleNotFoundError (transitive
     dep / submodule) is loud;
  4. a brainless resolution (no routable brain / no owning account) warns and seeds
     keyword-only — the no-global-brain contract is never bypassed;
  5. SAAS off is the byte-for-byte no-op.

Fake-conn style of the sibling suites (tests/test_saas_reembedder_brain_bind.py,
tests/test_doc_lane_brain_bind_contract.py): only the control-plane SQL (``saas.db.connection``)
and the cortex/Qdrant seams are faked; the REAL ``bind_request_brain`` -> ``resolve_brain``
chain runs. No live DB, no network.

Run: SPACY_MODEL=en_core_web_sm PYTHONPATH=. python3 -m pytest tests/test_provisioning_cortex_seed_brain_bind.py -q
"""

import sys

import pytest

from saas import crypto as saas_crypto
from saas import db as saas_db
from saas import llm_config as saas_llm_config
from saas import llm_context as saas_llm_context
from saas import seat_brain as saas_seat_brain

from src.api import llm_client
from src.api import cortex as cortex_mod
from src.provisioning import schema_manager


# A fresh seat being provisioned, its owning corporate account, and the account BYO brain.
SEAT = "d4e5f6a7-0000-4000-8000-00000000aaa1"
ACCT = "cccccccc-0000-4000-8000-00000000bbb2"
TENANT_BASE_URL = "https://corp-cortex.example/v1"
TENANT_CFG = {
    "backend_type": "openai",
    "base_url": TENANT_BASE_URL,
    "model": "corp-model",
    "api_key_ciphertext": "CORP_SECRET",
    "embedding_backend": "internal",
    "configured": True,
}


@pytest.fixture
def cp(monkeypatch):
    """SAAS on + a fake control plane so the REAL bind_request_brain -> resolve_brain chain runs.

    Same shape as tests/test_saas_seat_brain_isolation.py::cp: an in-memory SQL router over
    ``saas.db.connection``, egress guard neutralised, decryption identity. Caches cleared per
    test (the bind seams cache account lookups)."""
    monkeypatch.setenv("SAAS_MODE", "1")
    store = {"account_users": {SEAT: ACCT}, "account_kind": {ACCT: "tenant"},
             "acct_cfg": {ACCT: TENANT_CFG}, "seat_cfg": {}}

    class _FakeCur:
        def __init__(self):
            self._one = None

        def execute(self, sql, params=()):
            q = " ".join(str(sql).split())
            self._one = None
            if "FROM public.account_users" in q:
                acct = store["account_users"].get(str(params[0]))
                self._one = (acct,) if acct else None
            elif "FROM public.accounts" in q and "account_kind" in q:
                self._one = (store["account_kind"].get(str(params[0])),)
            elif "FROM public.individual_users" in q:
                self._one = None  # no /me row -> governed -> the org brain (account scope)
            elif "FROM public.seat_llm_config" in q:
                data = store["seat_cfg"].get((str(params[0]), str(params[1])))
                if data is not None:
                    spec = saas_llm_config._REDACT_COLS if "updated_at" in q \
                        else saas_llm_config._RESOLVE_COLS
                    self._one = tuple(data.get(c) for c in _cols(spec))
            elif "FROM public.account_llm_config" in q:
                data = store["acct_cfg"].get(str(params[0]))
                if data is not None:
                    spec = saas_llm_config._REDACT_COLS if "updated_at" in q \
                        else saas_llm_config._RESOLVE_COLS
                    self._one = tuple(data.get(c) for c in _cols(spec))
            else:
                raise AssertionError(f"unrouted control-plane query: {q[:160]}")

        def fetchone(self):
            return self._one

        def fetchall(self):
            return [self._one] if self._one else []

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FakeConn:
        def cursor(self):
            return _FakeCur()

        def close(self):
            pass

    import contextlib

    @contextlib.contextmanager
    def _connection():
        yield _FakeConn()

    monkeypatch.setattr(saas_db, "connection", _connection)
    monkeypatch.setattr(saas_llm_config, "validate_egress_url", lambda url: None)
    monkeypatch.setattr(saas_crypto, "decrypt", lambda ct: ("PLAIN:" + str(ct)) if ct else None)
    yield store


@pytest.fixture(autouse=True)
def _clean_context():
    saas_llm_config.invalidate_config_cache()
    saas_seat_brain.invalidate_kind_cache()
    saas_llm_context.invalidate_schema_account_cache()
    saas_llm_context._user_acct_cache.clear()
    if hasattr(saas_seat_brain, "_grant_cache"):
        saas_seat_brain._grant_cache.clear()
    saas_llm_context._current_llm_config.set(None)
    yield
    saas_llm_config.invalidate_config_cache()
    saas_seat_brain.invalidate_kind_cache()
    saas_llm_context.invalidate_schema_account_cache()
    saas_llm_context._user_acct_cache.clear()
    if hasattr(saas_seat_brain, "_grant_cache"):
        saas_seat_brain._grant_cache.clear()
    saas_llm_context._current_llm_config.set(None)


class _FakeCur:
    """Scripted cursor for the provisioning DDL: records, returns nothing."""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, *a, **k):
        self._conn.executed.append(" ".join(str(sql).split())[:120])


class _FakeProvConn:
    """Minimal psycopg2 connection for create_cortex_schema (context manager + txn no-ops)."""

    def __init__(self):
        self.executed = []

    def cursor(self, *a, **k):
        return _FakeCur(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass



def _cols(spec: str) -> list:
    """Split a llm_config column SPEC STRING (e.g. ``_RESOLVE_COLS``) into column names.

    ⚠️ These constants are comma-joined STRINGS, not tuples — iterating one directly yields
    CHARACTERS, and a fixture that feeds the resolver a mis-aligned row is worse than none.
    Mirrors tests/test_saas_seat_brain_isolation.py::_cols."""
    return [c.strip() for c in spec.replace("\n", " ").split(",") if c.strip()]


def _run_provisioning(monkeypatch, seed_spy=None):
    """Drive create_cortex_schema with the cortex/Qdrant seams faked; returns (schema, status)."""
    monkeypatch.setattr(cortex_mod, "seed_usage_patterns",
                        seed_spy or (lambda user_id: 3))
    monkeypatch.setattr(cortex_mod, "ensure_cortex_collection_for", lambda user_id: True)
    return schema_manager.create_cortex_schema(SEAT, db=_FakeProvConn())


# =====================================================================
# 1 — behavioural: the seed embed hop runs on the SEAT OWN brain,
#      and the bind never outlives the seed step
# =====================================================================

def test_paid_tenant_seed_embeds_through_the_seats_own_brain(cp, monkeypatch):
    seen = {}

    def _seed(user_id):
        # AT SEED TIME the brain is bound and ROUTABLE — the embed hop resolves to the
        # tenant own endpoint, never the refused global.
        cfg = saas_llm_context.current_llm_config()
        assert cfg is not None, "seed ran with NO brain bound (the GAP-1 wound)"
        assert cfg.get("base_url") == TENANT_BASE_URL
        assert not cfg.get("no_brain")
        assert "corp-cortex.example" in (llm_client.get_llm_chat_url() or ""), \
            "seed embed hop did not resolve the seat own brain"
        assert llm_client.saas_global_brain_blocked() is False
        seen["seeded_as"] = user_id
        return 3

    schema_name, status = _run_provisioning(monkeypatch, seed_spy=_seed)

    assert status == "ready"
    assert seen.get("seeded_as") == SEAT
    assert schema_name.startswith("flagent_")
    # THE CLEAR HALF: nothing outlives the seat it was bound for.
    assert saas_llm_context._current_llm_config.get() is None
    assert llm_client.get_llm_chat_url() == ""  # SAAS on: cleared = fail-closed, not global


def test_bind_precedes_seed_and_clear_follows_even_when_seed_raises(cp, monkeypatch):
    events = []
    real_bind = saas_llm_context.bind_request_brain
    real_reset = saas_llm_context.reset_llm_config

    def _spy_bind(uid):
        events.append("bind")
        return real_bind(uid)

    def _spy_reset(tok):
        events.append("reset")
        return real_reset(tok)

    def _exploding_seed(user_id):
        events.append("seed")
        raise RuntimeError("qdrant down mid-seed")

    monkeypatch.setattr(saas_llm_context, "bind_request_brain", _spy_bind)
    monkeypatch.setattr(saas_llm_context, "reset_llm_config", _spy_reset)

    _schema, status = _run_provisioning(monkeypatch, seed_spy=_exploding_seed)

    # Seed failure stays nonfatal (existing contract) AND the clear still ran (finally).
    assert status == "ready"
    assert events == ["bind", "seed", "reset"]
    assert saas_llm_context._current_llm_config.get() is None


# =====================================================================
# 2 — loudness: a bind failure warns LOUDLY, seed still completes
# =====================================================================

def test_bind_failure_warns_loudly_and_seed_still_completes_keyword_only(cp, monkeypatch):
    def _boom(uid):
        raise RuntimeError("saas control plane unreachable")

    monkeypatch.setattr(saas_llm_context, "bind_request_brain", _boom)
    calls = []

    from structlog.testing import capture_logs as _capture
    with _capture() as logs:
        _schema, status = _run_provisioning(monkeypatch,
                                            seed_spy=lambda uid: calls.append(uid) or 3)

    # Fail-safe kept (never raise into provisioning) — but never silent again: the event,
    # the exception class AND the message are on the wire, with the keyword-only note.
    warns = [e for e in logs if e.get("event") == "provisioning.tenant_brain_seed_bind_failed"]
    assert status == "ready"
    assert calls == [SEAT], "seed must still run after a failed bind"
    assert warns, "a bind failure must warn LOUDLY, never pass silently"
    assert warns[0].get("error") == "RuntimeError"
    assert "saas control plane unreachable" in str(warns[0].get("detail"))
    assert "keyword-only" in str(warns[0].get("note"))


def test_saas_absent_oss_miss_stays_silent(cp, monkeypatch):
    """THE OSS NO-OP STATE (adversarial-review ruling, mirrored from embedder): the public core
    has no saas package at all — that ONE miss is silent, byte-for-byte. Simulates the OSS
    distribution by blocking saas* at the meta_path after evicting it from sys.modules."""
    import sys

    # The test process has ALREADY imported saas* — the import machinery consults meta_path
    # finders only on a fresh import, so a blocker without this eviction never fires and the
    # assertion below passes VACUOUSLY (the dead-mock trap). Monkeypatch-integrated exactly
    # like the reference suite (test_doc_lane_brain_bind_contract.py
    # ::test_oss_no_saas_package_stays_silent): undo is automatic, no leak window.
    saas_keys = [k for k in list(sys.modules) if k == "saas" or k.startswith("saas.")]
    for k in saas_keys:
        monkeypatch.delitem(sys.modules, k, raising=False)

    class _SaasBlocker:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "saas" or fullname.startswith("saas."):
                raise ModuleNotFoundError("No module named 'saas'", name="saas")
            return None

    monkeypatch.setattr(sys, "meta_path", [_SaasBlocker()] + list(sys.meta_path))
    calls = []
    from structlog.testing import capture_logs as _capture
    with _capture() as logs:
        _schema, status = _run_provisioning(
            monkeypatch, seed_spy=lambda uid: calls.append(uid) or 3)

    assert status == "ready"
    assert calls == [SEAT], "OSS seed must proceed keyword-only exactly as today"
    assert not [e for e in logs if "tenant_brain_seed_bind" in str(e.get("event"))], \
        "the saas-absent miss must stay SILENT (un-actionable on every OSS rig)"


def test_saas_present_but_transitive_dependency_missing_is_LOUD(cp, monkeypatch):
    """The silent branch is NARROW: only the exact package-absent message. A saas PRESENT with a
    missing transitive dependency renders a DIFFERENT ModuleNotFoundError and must warn."""
    import sys

    saas_keys = [k for k in list(sys.modules) if k == "saas" or k.startswith("saas.")]
    for k in saas_keys:
        monkeypatch.delitem(sys.modules, k, raising=False)  # force a fresh import (see above)

    class _TransitiveBlocker:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "saas" or fullname.startswith("saas."):
                raise ModuleNotFoundError("No module named 'redis'", name="redis")
            return None

    monkeypatch.setattr(sys, "meta_path", [_TransitiveBlocker()] + list(sys.meta_path))
    calls = []
    from structlog.testing import capture_logs as _capture
    with _capture() as logs:
        _schema, status = _run_provisioning(
            monkeypatch, seed_spy=lambda uid: calls.append(uid) or 3)

    warns = [e for e in logs if e.get("event") == "provisioning.tenant_brain_seed_bind_failed"]
    assert status == "ready" and calls == [SEAT]
    assert warns and warns[0].get("error") == "ModuleNotFoundError", \
        "a saas-present transitive miss is a REAL breakage and must be loud"


# =====================================================================
# 3 — brainless resolutions: loud, keyword-only, NEVER a global brain
# =====================================================================

def test_brainless_account_warns_no_routable_brain_and_seeds_keyword_only(cp, monkeypatch):
    cp["acct_cfg"].clear()  # account resolves, but NO brain configured
    seen = {}

    def _seed(user_id):
        cfg = saas_llm_context.current_llm_config()
        # The no-brain SENTINEL is bound (fail-closed, zero spend) — not a global fallback.
        assert cfg is not None and cfg.get("no_brain") is True
        seen["blocked"] = llm_client.saas_global_brain_blocked()
        return 3

    from structlog.testing import capture_logs as _capture
    with _capture() as logs:
        _schema, status = _run_provisioning(monkeypatch, seed_spy=_seed)

    warns = [e for e in logs
             if e.get("event") == "provisioning.tenant_brain_seed_bind_no_routable_brain"]
    assert status == "ready" and seen.get("blocked") is True
    assert warns, "a brainless bind must warn loudly (seed proceeds keyword-only)"
    assert "keyword-only" in str(warns[0].get("note"))
    assert saas_llm_context._current_llm_config.get() is None


def test_no_owning_account_warns_unresolved_and_seeds_keyword_only(cp, monkeypatch):
    cp["account_users"].clear()  # no ACTIVE owning account for this seat
    calls = []

    from structlog.testing import capture_logs as _capture
    with _capture() as logs:
        _schema, status = _run_provisioning(
            monkeypatch, seed_spy=lambda uid: calls.append(uid) or 3)

    warns = [e for e in logs
             if e.get("event") == "provisioning.tenant_brain_seed_bind_unresolved"]
    assert status == "ready" and calls == [SEAT]
    assert warns, "no owning ACTIVE account resolved is an anomaly and must warn loudly"
    assert saas_llm_context._current_llm_config.get() is None


# =====================================================================
# 4 — SAAS off: the byte-for-byte no-op
# =====================================================================

def test_saas_off_is_a_silent_noop_with_zero_control_plane_access(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)

    def _boom():
        raise AssertionError("SAAS off must not touch the control plane")

    monkeypatch.setattr(saas_db, "connection", lambda: _boom())
    saas_llm_context._user_acct_cache.clear()
    calls = []

    from structlog.testing import capture_logs as _capture
    with _capture() as logs:
        _schema, status = _run_provisioning(
            monkeypatch, seed_spy=lambda uid: calls.append(uid) or 3)

    assert status == "ready" and calls == [SEAT]
    assert not [e for e in logs if "tenant_brain_seed_bind" in str(e.get("event"))], \
        "SAAS off must be the byte-for-byte silent no-op"
    assert saas_llm_context._current_llm_config.get() is None


# =====================================================================
# 5 — structural: the discipline cannot silently rot
# =====================================================================

def test_structural_bind_precedes_seed_and_finally_clears():
    """Source-level pin (the gate is structural, not "we remembered"): bind BEFORE the seed call
    and a finally-clear AFTER it."""
    src = open(schema_manager.__file__, encoding="utf-8").read()
    fn = src.index("def create_cortex_schema(")
    body = src[fn:src.index("def _execute_bootstrap_queries(", fn)]
    bind_at = body.index("_bind_cortex_seed_brain(user_id)")
    seed_at = body.index("seed_usage_patterns(user_id)")
    assert bind_at < seed_at, "the brain must be bound BEFORE seed_usage_patterns runs"
    finally_at = body.index("finally:", seed_at)
    assert "_clear_cortex_seed_brain(" in body[finally_at:], \
        "the seed brain bind must be cleared in a finally"


def test_oss_silent_branch_is_keyed_on_the_exact_package_absent_message():
    src = open(schema_manager.__file__, encoding="utf-8").read()
    helper = src[src.index("def _bind_cortex_seed_brain("):
                 src.index("def _clear_cortex_seed_brain(")]
    assert "No module named 'saas'" in helper, \
        "the silent branch must match the exact package-absent miss"
    assert "provisioning.tenant_brain_seed_bind_failed" in helper, \
        "every OTHER bind failure must carry the loud event"