"""tests/mcp runs against the OPEN CORE surface — auth off, no database.

``MCP_API_KEY`` is snapshotted into ``http_server.MCP_API_KEY`` at import, so the env var alone
would not help; the module attribute is patched to the unset value. These tests assert the
UNAUTHENTICATED surface, and the ones that want auth patch this same attribute themselves —
their patch runs after this fixture and wins. ``POSTGRES_DSN`` is cleared so the dashboard
credential probes (seat tokens / rotated keys) never reach a real database from a test.

Function-scoped monkeypatches: nothing leaks back out to other suites.
"""

import pytest


@pytest.fixture(autouse=True)
def _open_core_env(monkeypatch):
    """Auth off + no DSN for every test in this package — the environment they were written for."""
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    import src.mcp.http_server as h
    monkeypatch.setattr(h, "MCP_API_KEY", "")
