"""HTTP method semantics on the MCP transport — 404 vs 405 is load-bearing for real clients.

THE BUG THIS PINS. opencode could not connect to production. It reported:

    HTTP 405: Invalid OAuth error response: [{ "expected": "string", "code": "invalid_type",
      "path": ["error"], "message": "expected string, received undefined" }]
    Raw body: {"detail":"Method Not Allowed"}

Cause: the marketing site is `StaticFiles` mounted at "/" as a catch-all. Starlette's
StaticFiles serves ONLY GET/HEAD and raises HTTPException(405) otherwise — and at "/" that
covers every otherwise-unmatched path. So a POST to a route this server has never heard of
answered 405 ("this path exists, wrong verb") instead of 404. A client probing for OAuth
endpoints (RFC 7591 `/register`, RFC 8414 `/.well-known/oauth-authorization-server`) read the
405 as "an OAuth server lives here", then tried to parse `{"detail": ...}` as an OAuth error
object — which requires a string `error` member. It never fell back to the bearer token it
already had.

We serve STATIC bearer tokens, not OAuth. An honest 404 lets discovery conclude "no OAuth
here" and use the token.
"""
import pytest

from fastapi.testclient import TestClient

from src.mcp.http_server import app


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


@pytest.fixture(scope="module")
def saas_client(client):
    """The catch-all only exists under SAAS_MODE, which needs a reachable control plane.

    ⚠️ DO NOT set SAAS_MODE at module import to force it. The rest of tests/mcp runs with NO
    database (225 passing), and flipping SAAS_MODE on without a DSN makes every route answer
    503 — which is how the first cut of this file "failed": not a real defect, an environment
    I imposed on a suite that deliberately has none. Skip honestly instead.

    Gate on the mount ACTUALLY BEING THERE (GET / == 200), not on any single failure code —
    there are two distinct ways it is absent and they look nothing alike: SAAS_MODE off means
    the site was never mounted (404), while SAAS_MODE on without a DSN means the control plane
    is down (503). Both mean "the catch-all is not under test here".
    """
    if client.get("/").status_code != 200:
        pytest.skip("marketing catch-all not mounted (needs SAAS_MODE=true + a reachable "
                    "POSTGRES_DSN) — these assertions would pass trivially and prove nothing")
    return client


# ── the OAuth-discovery paths that derailed opencode ──────────────────────────────────
# These do not exist under ANY verb. 404 is both correct and the answer that lets a client
# conclude "no OAuth here" instead of "wrong method on a real OAuth endpoint".
_DISCOVERY_PATHS = [
    "/register",                                    # RFC 7591 dynamic client registration
    "/.well-known/oauth-authorization-server",      # RFC 8414 AS metadata
    "/.well-known/oauth-protected-resource",        # RFC 9728 protected-resource metadata
    "/oauth/token",
    "/some/path/that/does/not/exist",
]


@pytest.mark.parametrize("path", _DISCOVERY_PATHS)
def test_unknown_path_post_is_404_not_405(saas_client, path):
    """A POST to a path we do not serve must be 404. 405 claims the path EXISTS."""
    r = saas_client.post(path, json={})
    assert r.status_code == 404, (
        f"POST {path} returned {r.status_code}. If this is 405 the marketing catch-all is "
        f"swallowing unknown paths again — that is the opencode connection bug."
    )


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_other_verbs_on_unknown_paths_are_also_404(saas_client, method):
    assert getattr(saas_client, method)("/definitely/not/a/route").status_code == 404


# ── MCP Streamable HTTP spec compliance ───────────────────────────────────────────────
def test_get_on_the_mcp_endpoint_is_405_not_404(client):
    """MCP spec 2025-06-18, "Listening for Messages from the Server" §3, verbatim:

        The server MUST either return `Content-Type: text/event-stream` in response to this
        HTTP GET, or else return HTTP 405 Method Not Allowed, indicating that the server does
        not offer an SSE stream at this endpoint.

    We are stateless and offer no SSE stream, so 405 is the required answer. It previously
    fell through to the marketing catch-all and 404'd — a spec violation, and a materially
    different signal: per the backwards-compatibility section, a 4xx on the endpoint invites
    the client to go hunting for the DEPRECATED 2024-11-05 HTTP+SSE transport.
    """
    r = client.get("/mcp")
    assert r.status_code == 405
    assert "POST" in (r.headers.get("allow") or ""), "405 must advertise the allowed method"


def test_delete_on_the_mcp_endpoint_is_405_not_404(client):
    """MCP spec 2025-06-18 "Session Management" §5: the server MAY answer a session-terminating
    DELETE with 405. We are stateless and issue no `Mcp-Session-Id`, so 405 is honest.

    ⚠️ 404 IS NOT A HARMLESS ALTERNATIVE HERE. §3/§4 of the same section make 404 mean "your
    session was terminated — start a new one with a fresh InitializeRequest", so a client
    trying to hang up would be told to re-initialize instead. It 404'd before this route
    existed, because the "/" Mount fully matches /mcp and beats the partial-match 405.
    """
    r = client.delete("/mcp")
    assert r.status_code == 405
    assert "POST" in (r.headers.get("allow") or "")


def test_the_405_body_explains_itself(client):
    """A human debugging a client should learn where to send JSON-RPC."""
    assert "POST /mcp" in client.get("/mcp").json()["detail"]


def test_the_new_get_route_did_not_hijack_post(client):
    """The GET route is deliberately unauthenticated (it carries nothing). It must not have
    shadowed or loosened the POST lane, which is the actual data path.

    Asserts on ROUTING, not on a specific auth verdict: with MCP_API_KEY unset the server is
    deliberately open (documented dev default), so pinning 401 here would encode the test
    environment rather than the contract. What matters is that POST still REACHES the handler
    — never 404 (route lost) and never 405 (method shadowed by the GET registration).
    """
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code not in (404, 405), (
        f"POST /mcp returned {r.status_code} — adding GET /mcp broke the POST lane")


# ── the marketing site must still work — this is the regression risk of the fix ────────
def test_marketing_site_still_served_on_get(saas_client):
    """The catch-all's REASON for existing. GET/HEAD behaviour is untouched by the fix."""
    assert saas_client.get("/").status_code == 200


def test_marketing_head_still_works(saas_client):
    assert saas_client.head("/").status_code == 200


def test_openapi_door_still_lists_the_tools(saas_client):
    """The door OpenWebUI actually reads. A routing change must not empty it."""
    r = saas_client.get("/openapi.json")
    assert r.status_code == 200
    assert len(r.json().get("paths", {})) >= 8
