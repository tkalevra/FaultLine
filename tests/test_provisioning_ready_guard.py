"""Focused unit tests for the backend-authoritative provisioning readiness guard.

These cover the INFRA fix that closes the fresh-tenant first-call race: every
tenant-schema endpoint must BLOCK until the schema is provisioned-and-ready, or
return a clean 503 retry signal — never run extraction against a missing schema.

Pure-logic tests: the async/sync guards are exercised via monkeypatch so no live
DB / provisioning worker is required.
"""
import asyncio
import uuid

import pytest
from fastapi import HTTPException

from src.api import main


def _fresh_uuid() -> str:
    return str(uuid.uuid4())


def test_retry_503_shape():
    exc = main._provisioning_retry_503()
    assert isinstance(exc, HTTPException)
    assert exc.status_code == 503
    assert exc.detail["status"] == "provisioning"
    # Fail-safe: signals zero committed, never a silent success.
    assert exc.detail["committed"] == 0


def test_async_guard_anonymous_is_noop(monkeypatch):
    # anonymous / empty must never touch the DB.
    def _boom(*a, **k):
        raise AssertionError("psycopg2.connect must not be called for anonymous")

    monkeypatch.setattr(main.psycopg2, "connect", _boom)
    asyncio.run(main._ensure_tenant_ready("anonymous", "/test"))
    asyncio.run(main._ensure_tenant_ready("", "/test"))


def test_async_guard_cache_fastpath(monkeypatch):
    uid = _fresh_uuid()
    main._TENANT_READY_CACHE.add(uid)
    try:
        def _boom(*a, **k):
            raise AssertionError("cached tenant must not reconnect/poll")

        monkeypatch.setattr(main.psycopg2, "connect", _boom)
        asyncio.run(main._ensure_tenant_ready(uid, "/test"))
    finally:
        main._TENANT_READY_CACHE.discard(uid)


def test_async_guard_ready_caches(monkeypatch):
    uid = _fresh_uuid()
    main._TENANT_READY_CACHE.discard(uid)

    class _DummyConn:
        def close(self):
            pass

    monkeypatch.setattr(main.psycopg2, "connect", lambda *a, **k: _DummyConn())
    monkeypatch.setattr(
        "src.provisioning.provisioning_status.ensure_user_provisioned",
        lambda *a, **k: True,
    )

    async def _ready(*a, **k):
        return True

    monkeypatch.setattr(main, "wait_for_schema_ready", _ready)
    try:
        asyncio.run(main._ensure_tenant_ready(uid, "/test"))
        assert uid in main._TENANT_READY_CACHE  # confirmed ready → cached
    finally:
        main._TENANT_READY_CACHE.discard(uid)


def test_async_guard_timeout_raises_503_and_not_cached(monkeypatch):
    uid = _fresh_uuid()
    main._TENANT_READY_CACHE.discard(uid)

    class _DummyConn:
        def close(self):
            pass

    monkeypatch.setattr(main.psycopg2, "connect", lambda *a, **k: _DummyConn())
    monkeypatch.setattr(
        "src.provisioning.provisioning_status.ensure_user_provisioned",
        lambda *a, **k: False,
    )

    async def _never(*a, **k):
        return False

    monkeypatch.setattr(main, "wait_for_schema_ready", _never)
    try:
        with pytest.raises(HTTPException) as ei:
            asyncio.run(main._ensure_tenant_ready(uid, "/test"))
        assert ei.value.status_code == 503
        assert ei.value.detail["status"] == "provisioning"
        # Not ready → MUST NOT be cached (a later call must retry, never assume ready).
        assert uid not in main._TENANT_READY_CACHE
    finally:
        main._TENANT_READY_CACHE.discard(uid)


def test_sync_guard_anonymous_and_cache(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("sync guard must not connect for anonymous/cached")

    monkeypatch.setattr(main.psycopg2, "connect", _boom)
    main._ensure_tenant_ready_sync("anonymous", "/test")
    uid = _fresh_uuid()
    main._TENANT_READY_CACHE.add(uid)
    try:
        main._ensure_tenant_ready_sync(uid, "/test")
    finally:
        main._TENANT_READY_CACHE.discard(uid)


def test_sync_guard_ready_caches(monkeypatch):
    uid = _fresh_uuid()
    main._TENANT_READY_CACHE.discard(uid)

    class _DummyConn:
        def close(self):
            pass

    monkeypatch.setattr(main.psycopg2, "connect", lambda *a, **k: _DummyConn())
    monkeypatch.setattr(
        "src.provisioning.provisioning_status.ensure_user_provisioned",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "src.provisioning.provisioning_status.check_provisioning_status",
        lambda *a, **k: {"status": "ready"},
    )
    try:
        main._ensure_tenant_ready_sync(uid, "/test")
        assert uid in main._TENANT_READY_CACHE
    finally:
        main._TENANT_READY_CACHE.discard(uid)
