"""Round-2 census — every HTTP client construction in saas/ src/ tools/ is classified.

The round-1 recorder found the engine seams; the critic's recorder found the AGENT LANE
(agent_proxy / agent_autoscope / agent_gauntlet) sending the seat's BYO brain by name. The
lesson is that the audit must be a CENSUS, not a memory: this module enumerates every
``httpx.Client(`` / ``httpx.AsyncClient(`` / ``aiohttp`` / ``requests.`` / ``urllib.request``
construction in shipped code and requires each one to be in the table below with a
classification —

  * ``operator``  — the URL is operator infrastructure (Qdrant, Redis, the FaultLine backend/
                    MCP itself, Stripe/SMTP/Portainer, an env brain outside SaaS). Never a
                    tenant-configured URL. A plain client is correct.
  * ``fallback``  — the SAME line constructs the plain client ONLY when no tenant BYO cfg is
                    bound (``(_guarded or httpx.Client(...))`` / ``(_client or ...)``); the
                    tenant branch rides ``guarded_client`` / ``guarded_async_client``.
  * ``guard``     — the guarded factories themselves (``saas/llm_config.py``).

An UNLISTED construction fails ("classify it"), and a construction in a file that handles a
tenant-configured URL may only be ``fallback`` or ``guard`` — a bare ``httpx.Client(`` in one
of those files is a regression. Tenant-URL files are the ones ``tests/test_egress_guard_call_time.py``
drives; add to BOTH when a new one appears.

Run: FLTEST_LEDGER=... python3 tools/fltest.py --bug IS5 --test tests/test_http_client_census.py
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCOPES = ["saas", "src", "tools"]
CTOR_RE = re.compile(
    r"httpx\.(?:Async)?Client\(|aiohttp\.|requests\.(?:get|post|put|delete|Session|request)\("
    r"|urllib\.request\.(?:urlopen|Request)\(")

#: (file, substring that identifies the construction line, classification, what it talks to)
TABLE = [
    # ── guard ───────────────────────────────────────────────────────────────────
    ("saas/llm_config.py", "_httpx.Client(transport=GuardedTransport(", "guard", "tenant BYO — pinned sync"),
    ("saas/llm_config.py", "_httpx.AsyncClient(transport=GuardedAsyncTransport(", "guard", "tenant BYO — pinned async"),
    # ── fallback (tenant branch is guarded on the same statement) ───────────────
    ("src/api/context_window.py", "(_client or httpx.Client(timeout=_probe_timeout()))", "fallback", "env brain /props,/v1/models when no tenant cfg"),
    ("src/api/llm_client.py", "(_guarded or httpx.Client(timeout=timeout))", "fallback", "call_llm without a tenant brain"),
    ("src/api/llm_output_validator.py", "(_client or httpx.AsyncClient())", "fallback", "embedding when no tenant cfg"),
    # ── operator infrastructure ─────────────────────────────────────────────────
    ("src/api/llm_calls.py", "_llm_http_client: httpx.Client = httpx.Client(", "operator", "env brain (non-SaaS) — tenant BYO is routed to shared_guarded_client by _post_llm_sync"),
    ("src/api/llm_source_ip.py", "return httpx.Client(", "operator", "source-IP-bound twin of the env-brain client (LLM_SOURCE_IPS)"),
    ("src/api/llm_source_ip.py", "return httpx.AsyncClient(", "operator", "source-IP-bound twin (async)"),
    ("src/api/main.py", "_http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0)", "operator", "backend pooled client — Qdrant / env brain; tenant BYO routed via _post_llm_async"),
    ("src/api/main.py", "_http_client_sync = httpx.Client(timeout=httpx.Timeout(30.0)", "operator", "backend pooled sync client (Qdrant)"),
    ("src/mcp/http_server.py", "_mcp._http_client = _mcp.TurnBoundedClient(httpx.AsyncClient(timeout=30.0))", "operator", "MCP → FaultLine backend (HTTP transport lifespan, wall-bounded)"),
    ("src/mcp/server.py", "async with httpx.AsyncClient(timeout=3.0) as probe:", "operator", "backend provisioning probe"),
    ("src/mcp/server.py", "async with httpx.AsyncClient(timeout=30.0) as fresh:", "operator", "backend (fresh client after loop change) ×2"),
    ("src/mcp/server.py", "(_guarded_fetcher or httpx.AsyncClient(timeout=15.0))", "fallback", "USER-supplied /expand online <url> — guarded (redirects through the guard) when saas/ is present; plain on the open core (round 3)"),
    ("src/mcp/server.py", "async with httpx.AsyncClient(timeout=120.0) as fresh:", "operator", "backend long-call (fresh)"),
    ("src/mcp/server.py", "async with httpx.AsyncClient(timeout=90.0) as client:", "operator", "backend /expand"),
    ("src/mcp/server.py", "_http_client = TurnBoundedClient(httpx.AsyncClient(timeout=30.0))", "operator", "backend shared client (stdio transport lifespan, wall-bounded)"),
    ("src/mcp/server.py", "c = TurnBoundedClient(httpx.AsyncClient(timeout=30.0))", "operator", "backend shared client — the `_client()` accessor's LAZY creation for in-process consumers (gauntlet inprocess-mcp-client; closed by close_lazy_client at API shutdown)"),
    ("src/re_embedder/embedder.py", "_http_client = httpx.Client(timeout=30.0, limits=httpx.Limits(max_connections=10))", "operator", "Qdrant / backend; tenant embedding_url routed to shared_guarded_client in embed_text"),
    ("src/re_embedder/embedder.py", "_http_client_sync = httpx.Client(timeout=httpx.Timeout(30.0)", "operator", "Qdrant / backend pooled"),
    ("src/wgm/rel_type_inference.py", "async with httpx.AsyncClient(timeout=timeout) as client:", "operator", "llm_url argument — NO in-repo caller (dead lane); env brain if ever wired"),
    ("tools/capture_failure.py", "urllib.request.Request(url", "operator", "cortex MCP (operator URL)"),
    ("tools/capture_failure.py", "urllib.request.urlopen(req", "operator", "same (CORTEX_RECALL_URL env)"),
    ("tools/fltest.py", "urllib.request.urlopen(req", "operator", "cortex / dashboard (env URLs)"),
    ("tools/preprod_deploy.py", "urllib.request.urlopen(req", "operator", "Portainer API (operator config)"),
    ("tools/preprod_deploy.py", "urllib.request.urlopen(url", "operator", "pre-prod verification GETs (operator env)"),
    ("tools/fl_brain_switch.py", "urllib.request.Request(base.rstrip", "operator", "WGM_LLM_BASE_URL env brain (CLI)"),
    ("tools/fl_brain_switch.py", "urllib.request.urlopen(req", "operator", "same"),
    ("tools/fltest.py", "urllib.request.Request(", "operator", "dashboard publish / cortex (operator URLs)"),
    ("tools/preprod_deploy.py", "urllib.request.Request(url + path", "operator", "Portainer API (operator URL)"),
    # BUG-FAMILY-9 (2026-11-13): the ON-image door-litmus driver — drives the pre-prod
    # MCP door (operator infrastructure, a test-harness CLI, never tenant-configured).
    ("tools/bf9_litmus.py", "urllib.request.Request(", "operator", "the BF9 door-litmus driver — the pre-prod MCP door"),
    ("tools/bf9_litmus.py", "urllib.request.urlopen(req, timeout=timeout)", "operator", "the same driver's urlopen"),
]

#: Files that handle a TENANT-configured URL somewhere: any construction here must be
#: ``fallback`` or ``guard`` — never a bare client.
TENANT_URL_FILES = {
    "saas/llm_config.py", "saas/agent_proxy.py", "saas/agent_autoscope.py",
    "saas/agent_gauntlet.py", "saas/detect_cap.py", "saas/tenant_console.py",
    "saas/llm_admin.py", "saas/bench_selftest.py", "saas/user_console.py",
    "src/api/llm_calls.py", "src/api/llm_client.py", "src/api/context_window.py",
    "src/api/llm_output_validator.py", "src/re_embedder/embedder.py",
}
#: The only bare constructions tolerated in a tenant-URL file: module clients whose tenant
#: traffic is diverted BEFORE the client is used (llm_calls._post_llm_*, embedder.embed_text)
#: and the MCP→backend clients — each named in TABLE with the diversion in its note.
TOLERATED_IN_TENANT_FILES = {
    ("src/api/llm_calls.py", "_llm_http_client: httpx.Client = httpx.Client("),
    ("src/re_embedder/embedder.py", "_http_client = httpx.Client(timeout=30.0, limits=httpx.Limits(max_connections=10))"),
    ("src/re_embedder/embedder.py", "_http_client_sync = httpx.Client(timeout=httpx.Timeout(30.0)"),
}


def _constructions():
    out = []
    for scope in SCOPES:
        for f in sorted((ROOT / scope).rglob("*.py")):
            posix = f.as_posix()
            if "/tests/" in posix or "/.codeql/" in posix or "/FaultLine/" in posix:
                continue
            rel = f.relative_to(ROOT).as_posix()
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
                if CTOR_RE.search(code):
                    out.append((rel, i, line.strip()))
    return out


def _classify(rel, line):
    for f, needle, cls, _ in TABLE:
        if f == rel and needle in line:
            return cls, needle
    return None, None


@pytest.mark.parametrize("entry", _constructions(), ids=lambda e: f"{e[0]}:{e[1]}")
def test_every_http_client_construction_is_classified(entry):
    rel, n, line = entry
    cls, needle = _classify(rel, line)
    assert cls, f"{rel}:{n}: UNCLASSIFIED HTTP client construction — add it to TABLE with " \
                f"operator|fallback|guard (and route it if the URL is tenant-configured):\n  {line}"
    if rel in TENANT_URL_FILES and cls == "operator":
        assert (rel, needle) in TOLERATED_IN_TENANT_FILES, \
            f"{rel}:{n}: a bare client in a tenant-URL file must be guarded or a fallback:\n  {line}"


def test_table_rows_all_exist():
    """A stale row (construction removed/renamed) is noise — keep the table honest."""
    found = _constructions()
    for f, needle, cls, _ in TABLE:
        assert any(rel == f and needle in line for rel, _, line in found), (f, needle)


def test_tenant_url_files_construct_the_guard():
    """Every file that sends a tenant-configured URL imports/uses the guarded factories."""
    for rel in sorted(TENANT_URL_FILES - {"saas/llm_config.py", "saas/bench_selftest.py",
                                          "saas/user_console.py", "saas/llm_admin.py"}):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert re.search(r"guarded_(?:async_)?client\(|shared_guarded_(?:async_)?client\(", src), rel
