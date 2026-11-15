"""Host-based surface gating on the MCP HTTP transport (src/mcp/http_server.py).

One MCP instance serves two single-purpose PUBLIC faces behind a reverse proxy:

  * ADMIN / operator face   (SAAS_ADMIN_HOSTS)  → operator console + /admin + /operator;
                                                  the tenant surface + MCP 404.
  * TENANT face             (SAAS_TENANT_HOSTS) → tenant console + /tenant + MCP client;
                                                  all of god-mode 404s.
  * VOLENTI face            (SAAS_VOLENTI_HOSTS) → the corporate parent site ONLY (its
                                                  page + the always-allow set); EVERY
                                                  product/console/API path 404s. Checked
                                                  FIRST — the most restricted face wins.
  * Any OTHER host          (internal)          → NO gating (today's behavior EXACTLY).

The host lists are read into frozensets at http_server IMPORT time, so they are configured
per test through the ``saas_host_faces`` fixture (tests/conftest.py) rather than by writing
``os.environ`` at module import — module-level writes are process-wide and used to let
whichever sibling module was imported first decide this file's host config.
We drive the face via ``X-Forwarded-Host`` (what NPM sets) and, for one case, the bare
``Host`` header (fallback). Requests are GETs against POST-only routes where we only need
to prove "reachable" — a 405 proves the route exists (NOT a gate 404) without executing
tool/DB logic. tests/ is gitignored → add with ``git add -f``.

Run: python3 -m pytest tests/test_saas_host_surface_gate.py -q

DB GATE (honest skip): the SAAS offline/maintenance middleware is the OUTERMOST
middleware and FAILS CLOSED — with SAAS_MODE on and no reachable control-plane
database, every non-operator-realm path answers 503 before routing. Tests that
probe such paths therefore REQUIRE a reachable control-plane DB to be meaningful;
without one they SKIP (naming the missing resource), never fail. Tests that only
touch offline-exempt paths (/health, /admin, /operator, /dashboard operator
assets, admin "/") or no ASGI at all stay live DB-less. Pattern: _dsn_or_skip
(tests/test_db_read_no_idle_in_transaction.py shape incl. the non-test-db-name
refusal, as in tests/test_aspect_term_grounding.py).
"""

import os

import psycopg2
import pytest

# Control-plane singletons (the append-only ACTIVE contract chain, the platform settings row,
# the global operator realm) are shared by every suite in one database: the suites that
# write or depend on them run on one xdist worker (under --dist loadgroup, see tests/conftest.py).
pytestmark = pytest.mark.xdist_group("control_plane")
from fastapi.testclient import TestClient

import src.mcp.http_server as h


def _dsn_or_skip():
    """Skip honestly unless a THROWAWAY control-plane database is reachable.

    The offline gate's fail-closed 503 (maintenance page) is DESIGNED behavior for
    an unreachable control plane — so a DB-less run must skip these pins, not fail
    them: the red would say nothing about the host gate.
    """
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        pytest.skip("POSTGRES_DSN not set — this pin crosses the SAAS offline gate "
                    "(fail-closed 503 without a reachable control-plane DB); it needs "
                    "a migrated throwaway per-tenant database")
    dbname = dsn.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in dbname or dbname == "faultline":
        pytest.skip(f"refusing to run against database {dbname!r} "
                    "(name must contain 'test', never 'faultline')")
    try:
        psycopg2.connect(dsn, connect_timeout=3).close()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"POSTGRES_DSN unreachable: {type(e).__name__}: {str(e)[:120]}")
    return dsn

ADMIN_HOSTS = "fcontrol.example.test, ADMIN2.example.test:9000"
TENANT_HOSTS = "app.example.test"

ADMIN_HOST = "fcontrol.example.test"
TENANT_HOST = "app.example.test"
OTHER_HOST = "box.lan"

# ── (d) VOLENTI face (SAAS_VOLENTI_HOSTS) ────────────────────────────────────
# The corporate parent face: a single-purpose site, the MOST restricted surface,
# checked FIRST in _classify_face (http_server.py) so a host misconfigured into
# several lists lands on the safest face. A host OUTSIDE the tenant base domain
# is used so the fallthrough case is genuinely "internal", not a captured
# *.example.test subdomain (see _derive_tenant_base_domains).
VOL_HOSTS = "volenticorp.test"
VOL_HOST = "volenticorp.test"
# Marker VERIFIED unique to saas/landing/volenti.html (grep: 1 file across
# saas/landing/ + saas/dashboard/; neither console page carries it) — the
# negative console assertions reuse the exact strings the admin/tenant tests
# assert POSITIVE ("Operator Console" / "Tenant Console").
VOLENTI_MARKER = "By your leave"

# 404-from-gate looks EXACTLY like a genuine missing route.
GATE_404 = {"detail": "Not Found"}


@pytest.fixture(autouse=True)
def _faces(saas_host_faces):
    """THIS module's host config, per test and scoped — never process-wide."""
    return saas_host_faces(admin=ADMIN_HOSTS, tenant=TENANT_HOSTS)


@pytest.fixture
def client():
    # No context manager → lifespan is not run (no DB / http_client needed for these
    # routing-only assertions). Follow redirects off so root serving is observable.
    return TestClient(h.app, follow_redirects=False)


def _xfh(host):
    return {"X-Forwarded-Host": host}


def _is_gate_404(resp):
    return resp.status_code == 404 and resp.json() == GATE_404


def _reachable(resp):
    """Route exists (not a gate 404). 405/401/422/… all prove the route was hit."""
    return not (resp.status_code == 404 and resp.json() == GATE_404)


# ── (a) TENANT face ───────────────────────────────────────────────────────────

def test_tenant_face_blocks_god_mode(client):
    # god-mode routers + operator console → 404 (indistinguishable from missing route)
    assert _is_gate_404(client.get("/admin/accounts", headers=_xfh(TENANT_HOST)))
    assert _is_gate_404(client.get("/operator/me", headers=_xfh(TENANT_HOST)))
    assert _is_gate_404(client.get("/dashboard", headers=_xfh(TENANT_HOST)))
    assert _is_gate_404(client.get("/dashboard/app.js", headers=_xfh(TENANT_HOST)))
    # operator index must not leak via StaticFiles(html=True) alternates
    assert _is_gate_404(client.get("/dashboard/index.html", headers=_xfh(TENANT_HOST)))


def test_tenant_face_allows_tenant_and_mcp(client):
    # POST-only routes: probe with POST so we get a real full match (401/422 = reachable).
    # (A GET would now be absorbed by the marketing catch-all mount as a 404, so the old
    # GET→405 reachability probe no longer holds — POST is the honest probe.)
    _dsn_or_skip()  # /tenant + /mcp + tenant.html cross the offline gate
    assert _reachable(client.post("/tenant/login", headers=_xfh(TENANT_HOST), json={}))
    assert _reachable(client.post("/mcp", headers=_xfh(TENANT_HOST), json={}))
    assert _reachable(client.post("/recall_memory", headers=_xfh(TENANT_HOST), json={}))
    assert _reachable(client.post("/remember_facts", headers=_xfh(TENANT_HOST), json={}))
    # tenant console assets allowed
    assert client.get("/dashboard/tenant.html", headers=_xfh(TENANT_HOST)).status_code == 200
    assert client.get("/dashboard/tenant.js", headers=_xfh(TENANT_HOST)).status_code == 200


def test_tenant_face_shared_surface(client):
    assert client.get("/health", headers=_xfh(TENANT_HOST)).status_code == 200
    assert client.get("/dashboard/style.css", headers=_xfh(TENANT_HOST)).status_code == 200
    assert client.get("/dashboard/favicon.svg", headers=_xfh(TENANT_HOST)).status_code == 200


def test_tenant_face_root_serves_marketing_splash(client):
    # Customers now enter through the marketing front door (splash), NOT straight into
    # the tenant console. Root on the tenant face serves the splash.
    # ("Pick your" was the pre-78c465a4 hero copy; the splash's footer tagline is the
    # stable marker — unique to saas/landing/index.html, in neither console.)
    _dsn_or_skip()  # "/" on a NON-admin face is NOT offline-exempt → 503s DB-less
    r = client.get("/", headers=_xfh(TENANT_HOST))
    assert r.status_code == 200
    assert "MEMORY YOU CAN PROVE" in r.text
    assert "Tenant Console" not in r.text
    assert "Operator Console" not in r.text


def test_tenant_face_allows_marketing_and_access(client):
    # The public marketing site + the "Customer access" chooser are the tenant-face front
    # door; every landing page + the individual "My Memory" page route are reachable.
    _dsn_or_skip()  # /access.html etc. cross the offline gate
    assert client.get("/access.html", headers=_xfh(TENANT_HOST)).status_code == 200
    assert client.get("/tour.html", headers=_xfh(TENANT_HOST)).status_code == 200
    assert client.get("/research.html", headers=_xfh(TENANT_HOST)).status_code == 200
    assert client.get("/landing.css", headers=_xfh(TENANT_HOST)).status_code == 200
    # /me in a browser (Accept: text/html) serves the individual console page.
    r = client.get("/me", headers={**_xfh(TENANT_HOST), "Accept": "text/html"})
    assert r.status_code == 200 and "text/html" in r.headers.get("content-type", "")
    # /me for an API client (application/json) falls through to the JSON handler (401 no session).
    assert client.get("/me", headers={**_xfh(TENANT_HOST), "Accept": "application/json"}).status_code == 401


# ── (b) ADMIN / operator face ─────────────────────────────────────────────────

def test_admin_face_blocks_tenant_and_mcp(client):
    _dsn_or_skip()  # the BLOCKED side of these probes (/tenant, /mcp) 503s DB-less
    assert _is_gate_404(client.get("/tenant/login", headers=_xfh(ADMIN_HOST)))
    assert _is_gate_404(client.get("/mcp", headers=_xfh(ADMIN_HOST)))
    assert _is_gate_404(client.get("/recall_memory", headers=_xfh(ADMIN_HOST)))
    assert _is_gate_404(client.get("/remember_facts", headers=_xfh(ADMIN_HOST)))
    assert _is_gate_404(client.get("/dashboard/tenant.html", headers=_xfh(ADMIN_HOST)))
    assert _is_gate_404(client.get("/dashboard/tenant.js", headers=_xfh(ADMIN_HOST)))


def test_admin_face_allows_god_mode(client):
    assert _reachable(client.get("/operator/me", headers=_xfh(ADMIN_HOST)))
    # /operator/login is POST-only → probe with POST (a GET is now absorbed by the catch-all).
    assert _reachable(client.post("/operator/login", headers=_xfh(ADMIN_HOST), json={}))
    assert _reachable(client.get("/admin/operator-users", headers=_xfh(ADMIN_HOST)))
    # operator console assets allowed
    assert client.get("/dashboard/app.js", headers=_xfh(ADMIN_HOST)).status_code == 200
    assert client.get("/dashboard", headers=_xfh(ADMIN_HOST)).status_code == 200


def test_admin_face_blocks_marketing_and_me(client):
    # The public marketing site, the "Customer access" chooser, and the individual
    # "My Memory" realm are operator-face-hidden (404) — admin stays operator-only.
    _dsn_or_skip()  # the BLOCKED side of these probes (/access.html, /me) 503s DB-less
    assert _is_gate_404(client.get("/access.html", headers=_xfh(ADMIN_HOST)))
    assert _is_gate_404(client.get("/tour.html", headers=_xfh(ADMIN_HOST)))
    assert _is_gate_404(client.get("/research.html", headers=_xfh(ADMIN_HOST)))
    assert _is_gate_404(client.get("/landing.css", headers=_xfh(ADMIN_HOST)))
    assert _is_gate_404(client.get("/me", headers={**_xfh(ADMIN_HOST), "Accept": "text/html"}))
    assert _is_gate_404(client.get("/me/plan", headers=_xfh(ADMIN_HOST)))
    # …but the operator console root is still served (marketing does NOT shadow it).
    r = client.get("/", headers=_xfh(ADMIN_HOST))
    assert r.status_code == 200 and "Operator Console" in r.text


def test_admin_face_root_serves_operator_console(client):
    r = client.get("/", headers=_xfh(ADMIN_HOST))
    assert r.status_code == 200
    assert "Operator Console" in r.text
    assert "Tenant Console" not in r.text


def test_admin_face_shared_surface(client):
    assert client.get("/health", headers=_xfh(ADMIN_HOST)).status_code == 200
    assert client.get("/dashboard/style.css", headers=_xfh(ADMIN_HOST)).status_code == 200


# ── (c) unlisted / internal host → NO gate ────────────────────────────────────

def test_internal_host_no_gate(client):
    # Everything reachable on an unlisted host — no face gating.
    _dsn_or_skip()  # probes /tenant + /mcp + tenant.html → crosses the offline gate
    assert _reachable(client.get("/admin/operator-users", headers=_xfh(OTHER_HOST)))
    assert _reachable(client.get("/operator/me", headers=_xfh(OTHER_HOST)))
    assert _reachable(client.post("/tenant/login", headers=_xfh(OTHER_HOST), json={}))
    assert _reachable(client.post("/mcp", headers=_xfh(OTHER_HOST), json={}))
    assert client.get("/dashboard", headers=_xfh(OTHER_HOST)).status_code == 200
    assert client.get("/dashboard/tenant.html", headers=_xfh(OTHER_HOST)).status_code == 200
    # the marketing site + access chooser are reachable on internal too
    assert client.get("/access.html", headers=_xfh(OTHER_HOST)).status_code == 200
    # internal root → the marketing splash (the public front door), same as the tenant face
    # ("Pick your" was the pre-78c465a4 hero copy; footer tagline is the stable marker).
    r = client.get("/", headers=_xfh(OTHER_HOST))
    assert r.status_code == 200
    assert "MEMORY YOU CAN PROVE" in r.text


# ── resolver details: Host fallback, port strip, X-Forwarded-Host precedence ──

def test_host_header_fallback_gates(client):
    # No X-Forwarded-Host → bare Host header classifies the face.
    _dsn_or_skip()  # /mcp probes cross the offline gate
    assert _is_gate_404(client.get("/mcp", headers={"Host": ADMIN_HOST}))
    assert _reachable(client.post("/mcp", headers={"Host": TENANT_HOST}, json={}))


def test_forwarded_host_takes_precedence_over_host(client):
    # NPM sets X-Forwarded-Host; it wins over a spoofable internal Host.
    _dsn_or_skip()  # /mcp probes cross the offline gate
    r = client.get("/mcp", headers={"X-Forwarded-Host": ADMIN_HOST, "Host": TENANT_HOST})
    assert _is_gate_404(r)


def test_port_stripped_on_classify(client):
    # ADMIN2.example.test:9000 was configured WITH a port → stored port-stripped +
    # lowercased; a forwarded host with any :port still matches.
    _dsn_or_skip()  # /mcp probes cross the offline gate
    assert _is_gate_404(client.get("/mcp", headers=_xfh("admin2.example.test:8443")))


def test_gate_helpers_pure():
    # Pure-function proofs of the gate matrix (no ASGI).
    assert h._gate_blocks("tenant", "/admin/x") is True
    assert h._gate_blocks("tenant", "/operator") is True
    assert h._gate_blocks("tenant", "/dashboard") is True
    assert h._gate_blocks("tenant", "/tenant/login") is False
    assert h._gate_blocks("tenant", "/mcp") is False
    assert h._gate_blocks("admin", "/tenant/seats") is True
    assert h._gate_blocks("admin", "/mcp") is True
    assert h._gate_blocks("admin", "/dashboard/tenant.js") is True
    assert h._gate_blocks("admin", "/admin/x") is False
    assert h._gate_blocks("admin", "/operator/me") is False
    # /administrator must NOT match the /admin router prefix
    assert h._gate_blocks("tenant", "/administrator") is False
    # internal never blocks; shared surface never blocks
    assert h._gate_blocks("internal", "/admin/x") is False
    assert h._gate_blocks("tenant", "/health") is False
    assert h._gate_blocks("admin", "/openapi.json") is False
    # marketing site + "Customer access" + individual /me: allowed on tenant, 404 on admin.
    assert h._gate_blocks("tenant", "/access.html") is False
    assert h._gate_blocks("tenant", "/tour.html") is False
    assert h._gate_blocks("tenant", "/me") is False
    assert h._gate_blocks("tenant", "/me/plan") is False
    assert h._gate_blocks("admin", "/access.html") is True
    assert h._gate_blocks("admin", "/tour.html") is True
    assert h._gate_blocks("admin", "/landing.css") is True
    assert h._gate_blocks("admin", "/diagrams/how-it-works.mmd") is True
    assert h._gate_blocks("admin", "/me") is True
    assert h._gate_blocks("admin", "/me/plan") is True
    # "/" itself is NEVER gate-blocked (root is face-resolved in _root_by_face).
    assert h._gate_blocks("admin", "/") is False
    assert h._gate_blocks("tenant", "/") is False
    # internal never blocks any of the new surfaces
    assert h._gate_blocks("internal", "/access.html") is False
    assert h._gate_blocks("internal", "/me") is False


# ── (d) VOLENTI face (SAAS_VOLENTI_HOSTS) — corporate parent, checked FIRST ───
# Issue #7: this is the one host-face boundary that had ZERO coverage. The face
# is DESIGNED behavior; these pins hold it in place, they change nothing.

def _apply_volenti(saas_host_faces, volenti=VOL_HOSTS):
    """THIS module's host config plus the volenti list, via the SAME fixture shape.

    The autouse ``_faces`` fixture has already applied admin+tenant; re-applying
    through the same cached fixture instance overrides the volenti half only (same
    monkeypatch, torn down once). Passing ``volenti=""`` is the UNSET (no-op) config.
    """
    return saas_host_faces(admin=ADMIN_HOSTS, tenant=TENANT_HOSTS, volenti=volenti)


def test_gate_helpers_volenti_pure():
    # Pure-function proofs of the corporate allow-list (no ASGI). The volenti face
    # serves ONLY its own site (+ the shared always-allow set); everything else 404s.
    assert h._gate_blocks("volenti", "/") is False
    assert h._gate_blocks("volenti", "/volenti.html") is False
    assert h._gate_blocks("volenti", "/legal.html") is False
    # always-allow set stays shared on the corporate face
    assert h._gate_blocks("volenti", "/health") is False
    assert h._gate_blocks("volenti", "/openapi.json") is False
    # every product/console/API surface is blocked
    assert h._gate_blocks("volenti", "/mcp") is True
    assert h._gate_blocks("volenti", "/admin/x") is True
    assert h._gate_blocks("volenti", "/operator/me") is True
    assert h._gate_blocks("volenti", "/tenant/login") is True
    assert h._gate_blocks("volenti", "/dashboard/app.js") is True
    assert h._gate_blocks("volenti", "/dashboard/tenant.html") is True
    assert h._gate_blocks("volenti", "/access.html") is True
    assert h._gate_blocks("volenti", "/me") is True
    assert h._gate_blocks("volenti", "/v1/agent/chat") is True
    assert h._gate_blocks("volenti", "/customer_chat") is True
    # /volenti.html is LANDING-face (in _LANDING_EXACT): blocked on the ADMIN face —
    # it is the corporate site's page, not an operator-console asset.
    assert h._gate_blocks("admin", "/volenti.html") is True
    # internal never blocks (the no-op face)
    assert h._gate_blocks("internal", "/mcp") is False
    assert h._gate_blocks("internal", "/volenti.html") is False


def test_volenti_face_blocks_product_and_console(client, saas_host_faces):
    # Exposure boundary, DB-less half: every probed path here is offline-EXEMPT
    # (/admin, /operator, /dashboard operator assets), so the gate 404s are
    # observable without a control-plane DB. The corporate face serves ONLY the
    # Volenti site — god-mode and product assets 404, indistinguishable from a
    # missing route.
    _apply_volenti(saas_host_faces)
    assert _is_gate_404(client.get("/admin/accounts", headers=_xfh(VOL_HOST)))
    assert _is_gate_404(client.get("/operator/me", headers=_xfh(VOL_HOST)))
    assert _is_gate_404(client.get("/dashboard", headers=_xfh(VOL_HOST)))
    assert _is_gate_404(client.get("/dashboard/app.js", headers=_xfh(VOL_HOST)))
    # the one genuinely-shared surface stays shared on every face
    assert client.get("/health", headers=_xfh(VOL_HOST)).status_code == 200


def test_volenti_face_root_serves_corporate_site_not_consoles(client, saas_host_faces):
    # Exposure boundary, root + non-exempt paths. Root on the corporate face serves
    # the Volenti corporate page — NEVER a console: the exact marker strings the
    # admin/tenant root tests assert POSITIVE are asserted ABSENT here.
    _apply_volenti(saas_host_faces)
    _dsn_or_skip()  # "/" + /volenti.html + /mcp on a NON-admin face cross the offline gate
    r = client.get("/", headers=_xfh(VOL_HOST))
    assert r.status_code == 200
    assert VOLENTI_MARKER in r.text
    assert "Operator Console" not in r.text
    assert "Tenant Console" not in r.text
    # the corporate page is served by the face allow-list at its own path too
    r2 = client.get("/volenti.html", headers=_xfh(VOL_HOST))
    assert r2.status_code == 200 and VOLENTI_MARKER in r2.text
    # product/console/API surfaces gate-404 (the non-exempt probes complement the
    # DB-less half in test_volenti_face_blocks_product_and_console)
    assert _is_gate_404(client.get("/mcp", headers=_xfh(VOL_HOST)))
    assert _is_gate_404(client.post("/tenant/login", headers=_xfh(VOL_HOST), json={}))
    assert _is_gate_404(client.get("/access.html", headers=_xfh(VOL_HOST)))
    assert _is_gate_404(client.get("/dashboard/tenant.html", headers=_xfh(VOL_HOST)))


def test_volenti_face_wins_over_admin_and_tenant_lists(client, saas_host_faces):
    # Check-order priority: a host present in SEVERAL lists resolves to the MOST
    # restricted face — volenti is checked FIRST in _classify_face, before the
    # tenant and admin membership tests. Root content + reachability prove which
    # classification actually fired (not just that "a gate" ran).
    _apply_volenti(saas_host_faces, volenti=f"{ADMIN_HOST}, {TENANT_HOST}")
    _dsn_or_skip()  # "/" + /mcp cross the offline gate on a non-admin face
    # The ADMIN-listed host must land on the corporate face — NOT the operator
    # console (test_admin_face_root_serves_operator_console asserts the same root
    # carries "Operator Console" when the admin face wins).
    r = client.get("/", headers=_xfh(ADMIN_HOST))
    assert r.status_code == 200
    assert VOLENTI_MARKER in r.text
    assert "Operator Console" not in r.text
    # god-mode is REACHABLE on the admin face (test_admin_face_allows_god_mode) —
    # so a reachable /operator/me here would prove the admin face won instead.
    assert _is_gate_404(client.get("/operator/me", headers=_xfh(ADMIN_HOST)))
    # Same primacy over the TENANT list: /mcp is reachable on the tenant face
    # (test_tenant_face_allows_tenant_and_mcp) — a gate 404 proves volenti won.
    assert _is_gate_404(client.get("/mcp", headers=_xfh(TENANT_HOST)))


def test_volenti_unset_is_pure_noop(client, saas_host_faces):
    # No-op contract: with the env UNSET the frozenset is EMPTY and NO host
    # classifies as volenti — the "volenti" answer is unreachable from
    # classification — while every other face keeps its own matrix, and the
    # corporate page stays reachable as a public landing asset.
    _apply_volenti(saas_host_faces, volenti="")
    assert not h.SAAS_VOLENTI_HOSTS  # the precondition the no-op contract rests on
    # a would-be volenti host behaves as INTERNAL: no gating at all
    assert _reachable(client.get("/operator/me", headers=_xfh(VOL_HOST)))
    assert client.get("/dashboard/app.js", headers=_xfh(VOL_HOST)).status_code == 200
    # the tenant face still classifies to its own face
    assert _is_gate_404(client.get("/dashboard/app.js", headers=_xfh(TENANT_HOST)))
    # the corporate page remains a PUBLIC landing asset on the internal face, and
    # its host's root is the marketing splash — NOT the volenti page
    _dsn_or_skip()  # /volenti.html + "/" cross the offline gate on a non-admin face
    r = client.get("/volenti.html", headers=_xfh(VOL_HOST))
    assert r.status_code == 200 and VOLENTI_MARKER in r.text
    root = client.get("/", headers=_xfh(VOL_HOST))
    assert root.status_code == 200
    assert "MEMORY YOU CAN PROVE" in root.text
    assert VOLENTI_MARKER not in root.text
