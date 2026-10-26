"""Package fixtures for the MCP server tests.

The brain-slow breaker (brain-independence lane, 2026-10-25) is cross-turn MODULE state
by design (a classify timeout opens a 60s window), so any test that fails a classify —
deliberately or as a side effect — would otherwise fast-path every LATER recall test in
the same process and silently break their sequenced mocks. One reset here covers the
whole package.
"""

import pytest


@pytest.fixture(autouse=True)
def _reset_brain_slow(monkeypatch):
    monkeypatch.delenv("MCP_BRAIN_DEGRADED", raising=False)
    import src.mcp.server as _server
    _server._brain_slow_reset("test isolation (tests/mcp conftest)")
    yield
    _server._brain_slow_reset("test isolation (tests/mcp conftest)")
