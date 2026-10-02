"""tests/mcp runs against the OPEN CORE surface — auth off, no database.

``MCP_API_KEY`` is snapshotted into ``http_server.MCP_API_KEY`` at import, so the env var alone
would not help; the module attribute is patched to the unset value. These tests assert the
UNAUTHENTICATED surface, and the ones that want auth patch this same attribute themselves —
their patch runs after this fixture and wins. ``POSTGRES_DSN`` is cleared so the dashboard
credential probes (seat tokens / rotated keys) never reach a real database from a test.

The FOSS seat gate (``http_server._seat_cap_refusal``) FAILS CLOSED without a seat store
(#122), which is the right production behaviour and the wrong environment for these
transport tests. It is stubbed to "admit" here. The gate itself is pinned against a real
throwaway database in tests/test_foss_seat_cap.py and tests/test_foss_seatcap_critic.py,
outside this package, so this stub cannot hide a seat-gate regression.

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
    monkeypatch.setattr(h, "_seat_cap_refusal", lambda user_id, principal: None)
