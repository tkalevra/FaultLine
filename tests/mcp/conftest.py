"""tests/mcp runs against the OPEN CORE — SaaS off, auth off, no database.

That contract is stated outright in ``test_transport_method_semantics.saas_client``:

    ⚠️ DO NOT set SAAS_MODE at module import to force it. The rest of tests/mcp runs with NO
    database, and flipping SAAS_MODE on without a DSN makes every route answer 503.

It was being violated from outside: ~15 sibling modules under ``tests/`` assigned SAAS_MODE into
``os.environ`` at IMPORT time, which is process-wide, so by the time these tests RAN the SaaS
lane was live — the offline gate fail-closed 503 on an unreadable control plane, and the C3
startup guard raised ``SAAS_MODE is on but MCP_API_KEY is unset``. MEASURED on saas HEAD: 24
errors in a full-suite run, 0 when tests/mcp was run alone.

This package therefore states its own environment rather than inheriting whatever the last
importer left behind. Two halves, because the two values are read at different times:

  * SAAS_MODE is read LIVE per request by everything that would break these tests (the offline
    gate, the startup guards, the per-account lanes), so clearing the env var is enough. The
    import-time half — which routers got mounted — is decided once and owned by the root
    conftest; extra mounted routers do not disturb these assertions.
  * MCP_API_KEY is snapshotted into ``http_server.MCP_API_KEY`` at import, so the env var alone
    would not help; the module attribute is patched to the unset value. These tests assert the
    UNAUTHENTICATED open-core surface, and the ones that want auth patch this same attribute
    themselves — their patch runs after this fixture and wins.

Both are function-scoped monkeypatches: nothing leaks back out to the SaaS suites.
"""

import pytest


@pytest.fixture(autouse=True)
def _open_core_env(monkeypatch):
    """SaaS off + auth off for every test in this package — the environment they were written for.

    Also resets the brain-slow breaker (brain-independence lane, 2026-10-25): it is
    cross-turn MODULE state by design (a classify timeout opens a 60s window), so any test
    that fails a classify — deliberately or as a side effect — would otherwise fast-path
    every LATER recall test in the same process and silently break their sequenced mocks.
    One reset here covers the whole package."""
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    monkeypatch.delenv("MCP_BRAIN_DEGRADED", raising=False)
    import src.mcp.server as _server
    _server._brain_slow_reset("test isolation (tests/mcp conftest)")
    import src.mcp.http_server as h
    monkeypatch.setattr(h, "MCP_API_KEY", "")
