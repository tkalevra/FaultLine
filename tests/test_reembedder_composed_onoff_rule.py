"""Composed on/off rule for the re-embedder back-end (migrations 223 + 224).

Covers the two lazy helpers wired into the main loop by 534dca40:

    _saas_re_embedder_global_enabled()      GLOBAL  switch (platform_settings)
    _saas_re_embedder_tenant_enabled(uid)   TENANT  switch (accounts)

The contract under test — a tenant is processed iff BOTH are True:
  * GLOBAL False -> the whole loop idles this cycle.
  * TENANT False -> only that tenant is skipped.
  * SAAS off / ``saas`` package absent -> True, so the public FOSS core is unchanged.
  * A legacy / non-seat tenant (no owning account) -> True, never mass-skipped.
  * Fail-CLOSED lives INSIDE the resolvers; the helpers' outer ``except`` is the FOSS
    path only. These tests pin that a resolver verdict of False is honoured and is NOT
    silently converted to True.

DB-free: the helpers import ``saas.*`` lazily inside the function body, so injecting
stub modules into ``sys.modules`` exercises the real control flow with no database.
"""

import sys
import types

import pytest

from src.re_embedder import embedder


# ── stub plumbing ────────────────────────────────────────────────────────────────────

def _install(monkeypatch, *, saas_on=True, global_enabled=True,
             account_id="acct-1", tenant_enabled=True,
             global_raises=False, tenant_raises=False):
    """Inject the minimal saas surface the two helpers import."""
    cfg = types.ModuleType("saas.config")
    cfg.saas_mode_enabled = lambda: saas_on

    ps = types.ModuleType("saas.platform_settings")

    def _global():
        if global_raises:
            raise RuntimeError("control plane unreadable")
        return global_enabled
    ps.re_embedder_enabled = _global

    ctx = types.ModuleType("saas.llm_context")
    ctx._account_for_user = lambda uid: account_id

    accts = types.ModuleType("saas.accounts")

    def _tenant(acct, **kw):
        if tenant_raises:
            raise RuntimeError("control plane unreadable")
        return tenant_enabled
    accts.re_embedder_enabled = _tenant

    pkg = types.ModuleType("saas")
    for name, mod in (("saas", pkg), ("saas.config", cfg),
                      ("saas.platform_settings", ps), ("saas.llm_context", ctx),
                      ("saas.accounts", accts)):
        monkeypatch.setitem(sys.modules, name, mod)


def _blank_saas(monkeypatch):
    """Simulate the FOSS core: importing anything under ``saas`` fails."""
    for name in ("saas", "saas.config", "saas.platform_settings",
                 "saas.llm_context", "saas.accounts"):
        monkeypatch.setitem(sys.modules, name, None)  # import -> ImportError


# ── GLOBAL switch ────────────────────────────────────────────────────────────────────

def test_global_on_processes(monkeypatch):
    _install(monkeypatch, global_enabled=True)
    assert embedder._saas_re_embedder_global_enabled() is True


def test_global_off_stops(monkeypatch):
    """The operator kill-switch must actually reach the loop."""
    _install(monkeypatch, global_enabled=False)
    assert embedder._saas_re_embedder_global_enabled() is False


def test_global_saas_off_is_true(monkeypatch):
    """SAAS off -> public core behaviour, unchanged."""
    _install(monkeypatch, saas_on=False, global_enabled=False)
    assert embedder._saas_re_embedder_global_enabled() is True


def test_global_foss_no_saas_package_is_true(monkeypatch):
    _blank_saas(monkeypatch)
    assert embedder._saas_re_embedder_global_enabled() is True


# ── TENANT switch ────────────────────────────────────────────────────────────────────

def test_tenant_on_processes(monkeypatch):
    _install(monkeypatch, tenant_enabled=True)
    assert embedder._saas_re_embedder_tenant_enabled("u-1") is True


def test_tenant_off_skips(monkeypatch):
    _install(monkeypatch, tenant_enabled=False)
    assert embedder._saas_re_embedder_tenant_enabled("u-1") is False


def test_tenant_saas_off_is_true(monkeypatch):
    _install(monkeypatch, saas_on=False, tenant_enabled=False)
    assert embedder._saas_re_embedder_tenant_enabled("u-1") is True


def test_legacy_tenant_without_account_is_never_skipped(monkeypatch):
    """No owning account (pre-SaaS tenant) -> process; mirrors _saas_suspended_user_ids."""
    _install(monkeypatch, account_id=None, tenant_enabled=False)
    assert embedder._saas_re_embedder_tenant_enabled("u-legacy") is True


def test_tenant_foss_no_saas_package_is_true(monkeypatch):
    _blank_saas(monkeypatch)
    assert embedder._saas_re_embedder_tenant_enabled("u-1") is True


# ── composition + independence ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "g,t,expected",
    [(True, True, True), (True, False, False), (False, True, False), (False, False, False)],
)
def test_composed_and_rule(monkeypatch, g, t, expected):
    """Processed iff BOTH switches are ON."""
    _install(monkeypatch, global_enabled=g, tenant_enabled=t)
    processed = (embedder._saas_re_embedder_global_enabled()
                 and embedder._saas_re_embedder_tenant_enabled("u-1"))
    assert processed is expected


def test_one_tenant_off_does_not_affect_another(monkeypatch):
    """Per-tenant OFF must skip only that tenant."""
    seen = {"a": True, "b": False}
    _install(monkeypatch)
    monkeypatch.setitem(sys.modules, "saas.llm_context",
                        types.SimpleNamespace(_account_for_user=lambda uid: uid))
    accts = types.ModuleType("saas.accounts")
    accts.re_embedder_enabled = lambda acct, **kw: seen[acct]
    monkeypatch.setitem(sys.modules, "saas.accounts", accts)

    assert embedder._saas_re_embedder_tenant_enabled("a") is True
    assert embedder._saas_re_embedder_tenant_enabled("b") is False


# ── the helpers must never raise into the loop ───────────────────────────────────────

def test_helpers_never_raise(monkeypatch):
    """A resolver blowing up must not kill the poll cycle."""
    _install(monkeypatch, global_raises=True, tenant_raises=True)
    assert embedder._saas_re_embedder_global_enabled() is True
    assert embedder._saas_re_embedder_tenant_enabled("u-1") is True


# ── FOSS-clean structural pin ────────────────────────────────────────────────────────

def test_embedder_has_no_module_level_saas_import():
    """embedder.py ships in the public FOSS core, which has no saas/ package."""
    import inspect
    for i, line in enumerate(inspect.getsource(embedder).splitlines(), 1):
        assert not (line.startswith("from saas") or line.startswith("import saas")), (
            f"module-level saas import at line {i}: {line!r} — breaks the FOSS core"
        )
