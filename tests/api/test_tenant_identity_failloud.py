"""Phase 0 — fail-loud on absent tenant identity (multi-tenant isolation).

See DEV/SECURITY-multiuser-tenant-isolation.md §RP-2 / §RP-5.

These tests assert that an identity-less tenant-data operation FAILS LOUD
(4xx + log_crit) instead of silently collapsing to the shared
"anonymous"/faultline_test pool, while the explicit ALLOW_ANONYMOUS_TENANT
opt-in (default OFF) preserves the legacy shared-pool path for dev/single-user.

No live DB or model needed — these exercise the pure guard logic.
"""

import os

import pytest
from fastapi import HTTPException

from src.api.main import (
    _require_resolvable_user_id,
    _allow_anonymous_tenant,
    get_user_schema_context,
)


@pytest.fixture(autouse=True)
def _clear_anon_optin(monkeypatch):
    """Default each test to the secure (opt-in OFF) state."""
    monkeypatch.delenv("ALLOW_ANONYMOUS_TENANT", raising=False)
    yield


# ── _require_resolvable_user_id ──────────────────────────────────────────────


def test_present_identity_passes_through():
    assert _require_resolvable_user_id("3f2a9c41-abc", "/ingest") == "3f2a9c41-abc"


@pytest.mark.parametrize("absent", ["", None, "anonymous"])
def test_absent_identity_rejects_400_by_default(absent):
    with pytest.raises(HTTPException) as exc:
        _require_resolvable_user_id(absent, "/query")
    assert exc.value.status_code == 400
    assert "user_id required" in str(exc.value.detail)


@pytest.mark.parametrize("absent", ["", "anonymous"])
def test_opt_in_returns_anonymous_shared_pool(monkeypatch, absent):
    monkeypatch.setenv("ALLOW_ANONYMOUS_TENANT", "1")
    assert _allow_anonymous_tenant() is True
    assert _require_resolvable_user_id(absent, "/ingest") == "anonymous"


def test_opt_in_does_not_affect_present_identity(monkeypatch):
    monkeypatch.setenv("ALLOW_ANONYMOUS_TENANT", "1")
    assert _require_resolvable_user_id("abc-123", "/ingest") == "abc-123"


# ── get_user_schema_context (second line of defense) ─────────────────────────


@pytest.mark.parametrize("absent", ["", None, "anonymous"])
def test_schema_context_raises_on_absent_identity_by_default(absent):
    # db unused on the reject branch — pass a sentinel
    with pytest.raises(ValueError):
        get_user_schema_context(absent, db=None)


def test_schema_context_opt_in_returns_shared_schema(monkeypatch):
    monkeypatch.setenv("ALLOW_ANONYMOUS_TENANT", "1")
    monkeypatch.setenv("QDRANT_COLLECTION", "faultline-test")
    ctx = get_user_schema_context("anonymous", db=None)
    assert ctx["schema_name"] == "faultline_test"
    assert ctx["user_id"] == "anonymous"
