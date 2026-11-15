"""IS1/IS2 — the BYO-endpoint egress guard is a CALL-TIME property (resolve → vet → PIN).

CodeQL py/partial-ssrf on ``saas/llm_config.py::_http_get_json`` asked the right question: the
guard ran when a config was SAVED, and a hostname whose DNS answer changed afterwards (DNS
rebinding, or an allowlist change) reached the request. Even the inference-path pre-check
(``llm_calls._tenant_egress_blocked``) resolved ONCE for the vet and let httpx resolve AGAIN
for the connect — a 0-TTL rebinder answers the vet with a public address and the connect with
a private one.

Now every BYO request rides ``saas.llm_config.GuardedTransport`` / ``GuardedAsyncTransport``:
the host is resolved inside the transport, EVERY A/AAAA answer is checked against
``SAAS_LLM_ALLOWED_EGRESS`` + the private/link-local/loopback/metadata set (the SAME
``_vet_host`` the save path uses), the URL is rewritten to the vetted IP so the socket dials
that address and nothing else, ``Host`` and SNI keep the original name, and redirects are
never followed. Evidence: OWASP SSRF Prevention Cheat Sheet ("DNS pinning" — retrieve all
A+AAAA records and verify each; disable redirect following), CodeQL py/partial-ssrf
recommendation ("saving the verified IP address of each domain, then utilizing a custom HTTP
adapter to ensure that future requests to that domain use the verified IP address"),
python-httpx.org/advanced/transports (custom transport seam), encode.io/httpcore/extensions
(``sni_hostname``).

NO NETWORK, NO DNS: ``socket.getaddrinfo`` is monkeypatched inside ``saas.llm_config`` (the one
resolver every vet uses) and the REAL transport's ``handle_request`` is replaced by a recorder
that returns a canned response — so the assertion is about which address the transport was
asked to dial, never about a live host. No real hostnames: everything is ``*.example.test``.

Callers exercised (constraint 13 — the audit table lives in
DEV/review/gauntlet/codeql-input-surface/round_1/REPORT.md):
  * ``saas.llm_config._http_get_json`` / ``list_remote_models``  (model discovery)
  * ``src.api.llm_calls.call_llm_with_retry_sync``               (every sync engine call)
  * ``src.api.llm_calls.call_llm_with_retry_async``              (every async engine call)
  * ``src.api.llm_calls.call_llm_no_retry_sync``                 (the no-retry lane)
  * ``src.api.llm_client.call_llm``                              (the raw helper)
  * ``saas.llm_admin._probe_extraction`` / ``probe_reframe_latency`` (brain_health probes —
    ``POST /me/llm-endpoint/test``, recalibrate, tenant recalibrate all funnel here)
  * ``saas.bench_selftest`` copies the brain config and runs the ENGINE — it has no request
    of its own; covered by the llm_calls seam.

Run: FLTEST_LEDGER=... python3 tools/fltest.py --bug IS5 --test tests/test_egress_guard_call_time.py
(tests/ is gitignored → ``git add -f``).
"""

import asyncio
import os
import re
import socket
from pathlib import Path

import httpx
import pytest

from saas import llm_config as C
from saas import llm_context as X
from src.api import llm_calls as L
from src.api import llm_client as LC

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_V4 = "8.8.8.8"          # globally routable (TEST-NET ranges are is_global=False)
PUBLIC_V6 = "2606:4700:4700::1111"
LAN_BRAIN = "192.168.40.20"    # inside the pre-prod allowlist 192.168.40.0/25
BRAIN = "brain.example.test"
CFG = {"account_id": "acct-egress-test", "backend_type": "openai",
       "base_url": f"https://{BRAIN}/v1", "model": "m", "api_key": "k"}
CANNED = {"choices": [{"message": {"content": '{"ok": 1}'}}]}


# ── fixtures: fake resolver + recording transport ─────────────────────────────────

def _gai_entries(ips):
    return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "",
             (ip, 0)) for ip in ips]


@pytest.fixture
def resolver(monkeypatch):
    """Deterministic resolver. ``resolver.table[host] = [ip, ...]``; ``resolver.rebind(host,
    first, then)`` answers ``first`` on the first lookup and ``then`` afterwards (0-TTL
    rebinding). Unknown host → NXDOMAIN."""
    state = {"table": {}, "rebind": {}, "calls": {}}

    def fake(host, *a, **k):
        state["calls"][host] = state["calls"].get(host, 0) + 1
        rb = state["rebind"].get(host)
        if rb is not None:
            first, then = rb
            return _gai_entries(first if state["calls"][host] == 1 else then)
        ips = state["table"].get(host)
        if ips is None:
            raise socket.gaierror(-2, "Name or service not known")
        return _gai_entries(ips)

    class R:
        table = state["table"]
        calls = state["calls"]

        @staticmethod
        def rebind(host, first, then):
            state["rebind"][host] = (list(first), list(then))

        @staticmethod
        def set(host, ips):
            """Stable answer from now on (clears any rebind schedule for ``host``)."""
            state["rebind"].pop(host, None)
            state["table"][host] = list(ips)

    monkeypatch.setattr(C.socket, "getaddrinfo", fake)
    return R


@pytest.fixture
def wire(monkeypatch):
    """Replace the REAL transports' send with a recorder. Records ``request.url.host`` (what
    would be dialled), the Host header and the SNI extension; answers 200 + CANNED, or a 302
    to the metadata host when the path is ``/redir``."""
    seen = []

    def _answer(request):
        seen.append({"host": request.url.host, "url": str(request.url),
                     "hdr_host": request.headers.get("host"),
                     "sni": request.extensions.get("sni_hostname"),
                     "path": request.url.path})
        if request.url.path.endswith("/redir"):
            return httpx.Response(302, headers={"location": "http://meta.example.test/latest/"},
                                  request=request)
        body = {"data": [{"id": "m1"}]} if request.url.path.endswith("/models") else CANNED
        # ByteStream: readable by a plain .json() AND iterable by a streaming caller
        # (client.stream(...) → aiter_raw), which a pre-read Response(json=...) is not.
        import json as _json
        return httpx.Response(200, stream=httpx.ByteStream(_json.dumps(body).encode()),
                              headers={"content-type": "application/json"}, request=request)

    def sync_handle(self, request):
        return _answer(request)

    async def async_handle(self, request):
        return _answer(request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", sync_handle)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_handle)
    # getattr: the revert-red probe below must import on the PRE-fix tree too.
    getattr(C, "reset_shared_guarded_clients", lambda: None)()
    yield seen
    getattr(C, "reset_shared_guarded_clients", lambda: None)()


@pytest.fixture
def saas_env(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.delenv("SAAS_LLM_ALLOWED_EGRESS", raising=False)
    monkeypatch.delenv("LLM_SOURCE_IPS", raising=False)
    # The context-window probe is ANOTHER BYO caller (src/api/context_window.py) — see the
    # xfail below. Off here so the recorder only sees the call under test.
    monkeypatch.setenv("LLM_CONTEXT_PROBE", "false")


@pytest.fixture
def bound_cfg(saas_env):
    token = X.bind_explicit_llm_config(dict(CFG))
    yield CFG
    X.reset_llm_config(token)


def _saved_earlier(resolver, host=BRAIN):
    """Simulate the row having been SAVED while the host was public: the save-time guard
    must ACCEPT it — that is the precondition that makes a later refusal a call-time one."""
    resolver.table[host] = [PUBLIC_V4]
    assert C.validate_egress_url(f"https://{host}/v1")["public"] is True


# ── IS1: the transport itself ─────────────────────────────────────────────────────

@pytest.mark.parametrize("rebound_to", ["127.0.0.1", "169.254.169.254", "10.0.0.5",
                                        "::1", "fd00::5", "::ffff:10.0.0.5"])
def test_saved_then_rebound_host_is_refused_at_request_time(resolver, wire, saas_env, rebound_to):
    _saved_earlier(resolver)
    resolver.table[BRAIN] = [rebound_to]               # DNS changed after the save
    with C.guarded_client(timeout=1.0) as c:
        with pytest.raises(C.EgressBlocked) as ei:
            c.get(f"https://{BRAIN}/v1/models")
    assert "blocked address" in str(ei.value) and BRAIN in str(ei.value)
    assert wire == [], "a refused host must never reach the transport"


def test_one_private_answer_among_public_refuses_the_whole_host(resolver, wire, saas_env):
    resolver.table[BRAIN] = [PUBLIC_V4, "10.0.0.5"]
    with C.guarded_client(timeout=1.0) as c, pytest.raises(C.EgressBlocked):
        c.get(f"https://{BRAIN}/v1/models")
    assert wire == []


def test_unresolvable_host_fails_closed(resolver, wire, saas_env):
    with C.guarded_client(timeout=1.0) as c, pytest.raises(C.EgressBlocked) as ei:
        c.get("https://nx.example.test/v1/models")
    assert "cannot resolve" in str(ei.value)
    assert wire == []


def test_public_host_is_pinned_to_the_vetted_ip_with_name_kept_for_host_and_sni(resolver, wire, saas_env):
    resolver.table[BRAIN] = [PUBLIC_V4]
    with C.guarded_client(timeout=1.0) as c:
        r = c.get(f"https://{BRAIN}:8443/v1/models")
    assert r.status_code == 200
    hit = wire[-1]
    assert hit["host"] == PUBLIC_V4, "the socket must dial the VETTED address, not the name"
    assert hit["url"].startswith(f"https://{PUBLIC_V4}:8443/")
    assert hit["hdr_host"] == f"{BRAIN}:8443", "Host header keeps the name (+ explicit port)"
    assert hit["sni"] == BRAIN, "TLS presents/verifies the NAME via sni_hostname"


def test_ipv6_answer_is_pinned_bracketed(resolver, wire, saas_env):
    resolver.table["v6.example.test"] = [PUBLIC_V6]
    with C.guarded_client(timeout=1.0) as c:
        assert c.get("https://v6.example.test/v1/models").status_code == 200
    assert wire[-1]["url"].startswith(f"https://[{PUBLIC_V6}]/")


def test_operator_allowlisted_lan_brain_still_passes(resolver, wire, saas_env, monkeypatch):
    """The pre-prod LAN brain (192.168.40.0/25 via SAAS_LLM_ALLOWED_EGRESS) must keep working —
    the allowlist is read from the ONE existing place (``_allowed_networks``)."""
    monkeypatch.setenv("SAAS_LLM_ALLOWED_EGRESS", "192.168.40.0/25")
    resolver.table["lan.example.test"] = [LAN_BRAIN]
    with C.guarded_client(timeout=1.0) as c:
        assert c.get("http://lan.example.test:8080/v1/models").status_code == 200
    assert wire[-1]["host"] == LAN_BRAIN
    # ...and the allowlist is SCOPED: a neighbour outside the /25 is still refused.
    resolver.table["lan2.example.test"] = ["192.168.40.200"]
    with C.guarded_client(timeout=1.0) as c, pytest.raises(C.EgressBlocked):
        c.get("http://lan2.example.test/v1/models")


def test_ip_literal_targets_are_vetted_too(resolver, wire, saas_env):
    with C.guarded_client(timeout=1.0) as c:
        assert c.get(f"http://{PUBLIC_V4}/v1/models").status_code == 200
        for lit in ("127.0.0.1", "169.254.169.254", "10.0.0.5", "[::1]"):
            with pytest.raises(C.EgressBlocked):
                c.get(f"http://{lit}/v1/models")


def test_non_http_scheme_refused_before_any_resolution(resolver, wire, saas_env):
    with C.guarded_client(timeout=1.0) as c, pytest.raises(C.EgressBlocked):
        c.get(f"ftp://{BRAIN}/x")
    assert resolver.calls == {} and wire == []


def test_refusal_is_logged_loud(resolver, wire, saas_env, monkeypatch):
    lines = []
    monkeypatch.setattr(C, "_log_crit", lambda event, **kw: lines.append((event, kw)))
    resolver.table[BRAIN] = ["10.0.0.5"]
    with C.guarded_client(timeout=1.0) as c, pytest.raises(C.EgressBlocked):
        c.get(f"https://{BRAIN}/v1/models?api_key=SECRET-IN-QUERY")
    assert lines and lines[-1][0] == "llm_egress.blocked_at_call"
    kw = lines[-1][1]
    assert kw["host"] == BRAIN and "blocked address" in kw["reason"]
    assert "SECRET-IN-QUERY" not in repr(kw), "the log must carry host + reason, never the URL"


# ── IS2: no redirect escape ───────────────────────────────────────────────────────

def test_302_to_internal_is_not_followed(resolver, wire, saas_env):
    resolver.table[BRAIN] = [PUBLIC_V4]
    resolver.table["meta.example.test"] = ["169.254.169.254"]
    with C.guarded_client(timeout=1.0) as c:
        r = c.get(f"http://{BRAIN}/redir")
    assert r.status_code == 302 and r.headers["location"].startswith("http://meta.example.test")
    assert [h["host"] for h in wire] == [PUBLIC_V4], "exactly one dial, to the vetted origin"
    with pytest.raises(httpx.HTTPStatusError):
        r.raise_for_status()     # every caller raise_for_status()es → a 3xx is an error, not data


def test_even_a_forced_follow_is_vetted_by_the_transport(resolver, wire, saas_env):
    """Belt AND braces: the guard IS the transport, so a redirect somebody does follow
    re-enters handle_request and the internal target is refused there."""
    resolver.table[BRAIN] = [PUBLIC_V4]
    resolver.table["meta.example.test"] = ["169.254.169.254"]
    with C.guarded_client(timeout=1.0) as c, pytest.raises(C.EgressBlocked):
        c.send(c.build_request("GET", f"http://{BRAIN}/redir"), follow_redirects=True)
    assert [h["host"] for h in wire] == [PUBLIC_V4]


def test_every_client_construction_in_llm_config_pins_follow_redirects_false():
    """Pin it in EVERY client construction, not one (IS2). Source-level: any ``httpx.Client(``
    / ``AsyncClient(`` in saas/llm_config.py carries ``follow_redirects=False``."""
    src = open(C.__file__, encoding="utf-8").read()
    ctors = [m for m in re.finditer(r"_?httpx\.(?:Async)?Client\((.*?)\)\n", src, re.S)]
    assert ctors, "expected at least the guarded factories"
    for m in ctors:
        assert "follow_redirects=False" in m.group(1), m.group(0)
    # ...and ``guarded_client`` does not even accept the knob.
    import inspect
    assert "follow_redirects" not in inspect.signature(C.guarded_client).parameters
    assert "follow_redirects" not in inspect.signature(C.guarded_async_client).parameters


# ── every caller (constraint 13) ──────────────────────────────────────────────────

def test_caller_http_get_json_refuses_rebound_host(resolver, wire, saas_env):
    _saved_earlier(resolver)
    resolver.table[BRAIN] = ["127.0.0.1"]
    with pytest.raises(C.EgressBlocked):
        C._http_get_json(f"https://{BRAIN}/v1/models", {}, 1.0)
    assert wire == []


def test_caller_list_remote_models_rebound_between_vet_and_request(resolver, wire, saas_env):
    """list_remote_models vets FIRST (public) — the transport must still refuse when the
    answer flips before the GET (0-TTL rebind)."""
    resolver.rebind(BRAIN, [PUBLIC_V4], ["10.0.0.5"])
    out = C.list_remote_models("openai", f"https://{BRAIN}", api_key=None, timeout=1.0)
    assert "error" in out and "rejected" in out["error"], out
    assert wire == []
    # and the healthy case still lists models through the pinned client
    resolver.set(BRAIN, [PUBLIC_V4])
    out = C.list_remote_models("openai", f"https://{BRAIN}", api_key=None, timeout=1.0)
    assert out == {"models": ["m1"]}, out
    assert wire[-1]["host"] == PUBLIC_V4


def _msgs():
    return [{"role": "user", "content": "hello"}]


def test_caller_call_llm_with_retry_sync(resolver, wire, bound_cfg):
    resolver.rebind(BRAIN, [PUBLIC_V4], ["127.0.0.1"])    # pre-check sees public; connect sees loopback
    out = L.call_llm_with_retry_sync(messages=_msgs(), model="m", user_id="u",
                                     operation="EXTRACTION", max_retries=2, timeout=2)
    assert out == {}, "a refusal is a loud non-answer, never a success shape"
    assert wire == [], "no POST reached the transport"
    # healthy: pinned dial
    resolver.set(BRAIN, [PUBLIC_V4])
    out = L.call_llm_with_retry_sync(messages=_msgs(), model="m", user_id="u",
                                     operation="EXTRACTION", max_retries=1, timeout=2)
    assert out == {"ok": 1}
    assert wire[-1]["host"] == PUBLIC_V4 and wire[-1]["sni"] == BRAIN


def test_caller_call_llm_with_retry_sync_raise_on_unavailable(resolver, wire, bound_cfg):
    resolver.rebind(BRAIN, [PUBLIC_V4], ["169.254.169.254"])
    with pytest.raises(L.LLMUnavailable):
        L.call_llm_with_retry_sync(messages=_msgs(), model="m", user_id="u",
                                   operation="EXTRACTION", max_retries=1, timeout=2,
                                   raise_on_unavailable=True)
    assert wire == []


def test_caller_call_llm_with_retry_async(resolver, wire, bound_cfg):
    resolver.rebind(BRAIN, [PUBLIC_V4], ["10.0.0.5"])

    async def go():
        return await L.call_llm_with_retry_async(messages=_msgs(), model="m", user_id="u",
                                                 operation="EXTRACTION", max_retries=2,
                                                 timeout=2)
    assert asyncio.run(go()) == {}
    assert wire == []
    resolver.set(BRAIN, [PUBLIC_V4])
    assert asyncio.run(go()) == {"ok": 1}
    assert wire[-1]["host"] == PUBLIC_V4


def test_caller_call_llm_no_retry_sync(resolver, wire, bound_cfg):
    resolver.rebind(BRAIN, [PUBLIC_V4], ["::1"])
    out = L.call_llm_no_retry_sync(messages=_msgs(), model="m", user_id="u",
                                   operation="EXTRACTION", timeout=2)
    assert out is None and wire == []
    resolver.set(BRAIN, [PUBLIC_V4])
    out = L.call_llm_no_retry_sync(messages=_msgs(), model="m", user_id="u",
                                   operation="EXTRACTION", timeout=2)
    assert out == {"ok": 1} and wire[-1]["host"] == PUBLIC_V4


def test_caller_llm_client_call_llm(resolver, wire, bound_cfg):
    resolver.table[BRAIN] = ["10.0.0.5"]
    with pytest.raises(C.EgressBlocked):
        LC.call_llm(f"https://{BRAIN}/v1/chat/completions", {"model": "m"}, timeout=2)
    assert wire == []
    resolver.table[BRAIN] = [PUBLIC_V4]
    assert LC.call_llm(f"https://{BRAIN}/v1/chat/completions", {"model": "m"}, timeout=2) == CANNED
    assert wire[-1]["host"] == PUBLIC_V4


def test_caller_brain_health_probe_extraction(resolver, wire, saas_env):
    """``POST /me/llm-endpoint/test`` → ``_run_test_and_calibrate`` → ``_probe_extraction`` binds
    the SAVED config explicitly and calls the engine seam. Rebound after the save → refused."""
    from saas.llm_admin import _probe_extraction
    _saved_earlier(resolver)
    resolver.rebind(BRAIN, [PUBLIC_V4], ["169.254.169.254"])
    resolver.calls.clear()
    out = _probe_extraction(CFG["account_id"], dict(CFG))
    assert out.get("ok") is False, out
    assert wire == []


def test_caller_brain_health_probe_reframe_latency(resolver, wire, saas_env):
    from saas.llm_admin import probe_reframe_latency
    resolver.table[BRAIN] = ["10.0.0.5"]
    out = probe_reframe_latency(CFG["account_id"], dict(CFG), samples=2, total_budget_s=5)
    assert out["ok"] is False and out["samples"] == 0, out
    assert wire == []


def test_no_tenant_brain_means_legacy_path_untouched(resolver, wire, monkeypatch):
    """SaaS off / no tenant cfg: the operator's env brain may legitimately be a LAN host and is
    NOT egress-checked (byte-for-byte legacy). The guard is scoped to tenant-supplied URLs."""
    monkeypatch.setenv("SAAS_MODE", "0")
    monkeypatch.delenv("LLM_SOURCE_IPS", raising=False)
    monkeypatch.setenv("LLM_CONTEXT_PROBE", "false")
    assert L._tenant_byo_cfg() is None
    resolver.table["ops-brain.example.test"] = ["10.0.0.9"]
    r = L._post_llm_sync(L._llm_http_client, "http://ops-brain.example.test/v1/chat/completions",
                         json={}, headers={}, timeout=1)
    assert r.status_code == 200 and wire[-1]["host"] == "ops-brain.example.test"


# ── REVERT-RED probe (version-agnostic: uses NO symbol the fix introduced) ───────────

def test_revert_red_probe_rebound_host_never_dialled_by_name(resolver, wire, bound_cfg):
    """On the pre-fix tree this test is RED for the right reason: ``_tenant_egress_blocked``
    resolved ONCE (public), then httpx dialled the NAME, so the recorder saw
    host == 'brain.example.test' — i.e. the connect would have resolved to loopback.
    On the fixed tree the transport resolves again, sees loopback, and refuses.
    Uses only symbols that exist on both trees."""
    resolver.rebind(BRAIN, [PUBLIC_V4], ["127.0.0.1"])
    L.call_llm_with_retry_sync(messages=_msgs(), model="m", user_id="u",
                               operation="EXTRACTION", max_retries=1, timeout=2)
    dialled = [h["host"] for h in wire]
    assert BRAIN not in dialled, f"request left by NAME (unpinned): {dialled}"
    assert dialled == [], f"a rebound host must not be dialled at all: {dialled}"


# ── the callers routed in the round-1 addendum (were xfail-pinned as unrouted) ─────────

def test_context_window_probe_rides_the_guard(resolver, wire, bound_cfg, monkeypatch):
    """src/api/context_window.py::_probe_sync GETs the tenant base (/props, /v1/models) on
    EVERY tenant call — it must be refused on a rebound host and pinned on a public one."""
    monkeypatch.setenv("LLM_CONTEXT_PROBE", "true")
    from src.api import context_window as CW
    monkeypatch.setattr(CW, "_base_url", lambda: f"https://{BRAIN}")
    resolver.table[BRAIN] = ["10.0.0.5"]
    CW._probe_sync(CW._WindowState())
    assert wire == [], f"probe reached the transport by name: {[h['host'] for h in wire]}"
    resolver.set(BRAIN, [PUBLIC_V4])
    CW._probe_sync(CW._WindowState())
    assert wire and all(h["host"] == PUBLIC_V4 for h in wire), wire


def test_detect_cap_direct_calls_ride_the_guard(resolver, wire, saas_env):
    from saas import detect_cap as DC
    cfg = dict(CFG)
    resolver.table[BRAIN] = ["169.254.169.254"]
    out = DC.direct_call(cfg, timeout_s=1.0)
    assert out["ok"] is False and "EgressBlocked" in out["detail"], out
    out = DC.direct_call_stream(cfg, timeout_s=1.0)
    assert out["ok"] is False and wire == [], out
    resolver.set(BRAIN, [PUBLIC_V4])
    out = DC.direct_call(cfg, timeout_s=1.0)
    assert out["status"] == 200 and wire[-1]["host"] == PUBLIC_V4, out


def test_embedder_embed_text_rides_the_guard_for_a_tenant_embedding_url(resolver, wire, bound_cfg):
    from src.re_embedder import embedder as E
    resolver.table["emb.example.test"] = ["10.0.0.5"]
    out = E.embed_text("hello", "", timeout=1.0, fallback=False,
                       embedding_url="https://emb.example.test/v1/embeddings")
    assert out is None and wire == []
    resolver.set("emb.example.test", [PUBLIC_V4])
    E.embed_text("hello", "", timeout=1.0, fallback=False,
                 embedding_url="https://emb.example.test/v1/embeddings")
    assert wire and wire[-1]["host"] == PUBLIC_V4


def test_output_validator_embed_rides_the_guard(resolver, wire, bound_cfg, monkeypatch):
    from src.api import llm_output_validator as V
    monkeypatch.setenv("EMBEDDING_API_URL", "https://emb.example.test/v1/embeddings")
    resolver.table["emb.example.test"] = ["127.0.0.1"]
    obj = V.LLMOutputValidator.__new__(V.LLMOutputValidator)
    obj.llm_endpoint = f"https://{BRAIN}/v1/chat/completions"

    async def go():
        return await obj._embed_text("hello")
    assert asyncio.run(go()) is None and wire == []
    resolver.set("emb.example.test", [PUBLIC_V4])
    asyncio.run(go())
    assert wire and wire[-1]["host"] == PUBLIC_V4


def test_tenant_console_and_detect_cap_probes_construct_only_guarded_clients():
    """The two tenant-console chat-LLM probes and both detect_cap probes need a seat/DB to
    drive end-to-end; pin at source level that no bare ``httpx.Client(`` survives in them and
    that each constructs ``guarded_client(`` (imported from saas.llm_config, never re-declared)."""
    import inspect
    from saas import detect_cap as DC, tenant_console as TC
    for fn in (TC.tenant_test_agent_chat_llm, TC.tenant_agent_preview_chat,
               DC.direct_call, DC.direct_call_stream):
        src = inspect.getsource(fn)
        assert "httpx.Client(" not in src, fn.__name__
        assert "guarded_client(" in src and "from .llm_config import guarded_client" in src, fn.__name__


# ── ROUND 2: the AGENT LANE (saas/agent_proxy, agent_autoscope, agent_gauntlet) ──────────
#
# Round-1's recorder was never pointed here: POST /v1/agent/chat (stream + non-stream),
# agent_autoscope.draft_scope and agent_gauntlet's three brain calls sent the seat's BYO brain
# through plain httpx.AsyncClients BY NAME, and run_round GETted a tenant-supplied
# reference_url with follow_redirects=True after a by-name vet. All now ride
# guarded_async_client; the reference fetch follows redirects THROUGH the guard (every hop
# re-vetted at the transport) instead of by name.

AGENT_BRAIN = {"backend_type": "openai", "base_url": f"https://{BRAIN}", "model": "m",
               "api_key": "SECRET-BRAIN-KEY", "extra_headers": {}}


@pytest.fixture
def agent_app(monkeypatch, saas_env):
    """The /v1/agent/chat router on a bare app, auth/seat/entitlement seams stubbed, brain
    resolved to AGENT_BRAIN — the REAL httpx client + transport (recorded by ``wire``)."""
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.testclient import TestClient
    from saas import agent_proxy, agent_entitlement as _ae
    monkeypatch.setattr("saas.config.saas_mode_enabled", lambda: True)
    monkeypatch.setattr(_ae, "serve_allowed", lambda aid, uid: (True, None))
    monkeypatch.setattr("saas.seat_brain.resolve_brain", lambda uid, aid: dict(AGENT_BRAIN))

    # EG6 (issue #6): ``wire`` patches httpx's transports CLASS-GLOBALLY, so inside the test
    # window it records EVERY request the process makes — including the MCP→backend brain lane
    # (FAULTLINE_API_URL, default localhost:8000) that ``recall_memory_tool`` →
    # ``_classify_and_gate`` fires on the FIRST /v1/agent/chat call of the process
    # (``recall_cache`` is module state: the first call is always cache-cold, later ones warm).
    # Those calls are operator infra, NOT tenant-brain egress. Measured at 289df547: the
    # refusal probe's ``assert wire == []`` therefore failed on WHICHEVER refusal probe ran
    # first (6 × localhost/classify-intent recorded) — the failing param fails ALONE, and the
    # red moves to [10.0.0.5] when it is made the first param. Never "cross-test recorder
    # bleed". Isolate BOTH module-state seams so ``wire`` sees ONLY what the pin is about —
    # tenant-brain traffic — and every probe starts in the same state:
    #   * ``S._http_client`` → the same inert backend double the /expand test below uses
    #     (no real httpx client is ever built for the backend lane; ``_client()`` honours an
    #     assigned stub as-is by contract);
    #   * ``recall_cache.reset()`` around the test (fresh-process semantics per test).
    # The guarantee is UNCHANGED, not filtered: ``wire`` itself is untouched, so ANY
    # tenant-brain dial — guarded or bypassed, to any host — is still recorded and still
    # fails these probes.
    from src.mcp import server as _server
    from saas import recall_cache as _recall_cache

    class _BackendStub:
        async def get(self, url, **k):
            return httpx.Response(200, json={"ingest_enabled": True}, request=httpx.Request("GET", url))

        async def post(self, url, **k):
            return httpx.Response(200, json={"status": "ok"}, request=httpx.Request("POST", url))

        async def aclose(self):
            pass

    monkeypatch.setattr(_server, "_http_client", _BackendStub(), raising=False)
    _recall_cache.reset()

    def _auth(request: Request):
        if not request.headers.get("authorization"):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return "principal"
    logs = []
    app = FastAPI()
    app.include_router(agent_proxy.build_agent_proxy_router(
        require_auth=_auth, resolve_user_id=lambda r, b, p: "10d7d879-63cd-4f31-92ce-f2c9edb760ab",
        account_for=lambda p, u: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", log=logs.append))
    yield TestClient(app), logs
    _recall_cache.reset()


def _agent_chat(client, stream):
    return client.post("/v1/agent/chat", headers={"Authorization": "Bearer flu_x"},
                       json={"messages": [{"role": "user", "content": "ping"}], "stream": stream})


@pytest.mark.parametrize("rebound_to", ["127.0.0.1", "10.0.0.5", "169.254.169.254"])
def test_agent_chat_non_stream_refuses_rebound_brain(resolver, wire, agent_app, rebound_to):
    client, logs = agent_app
    _saved_earlier(resolver)
    resolver.table[BRAIN] = [rebound_to]
    r = _agent_chat(client, stream=False)
    assert r.status_code == 502 and r.json()["error"]["code"] == "brain_egress_blocked", r.text
    assert "SECRET-BRAIN-KEY" not in r.text
    assert wire == [] and any("EGRESS BLOCKED" in ln for ln in logs)


def test_agent_chat_non_stream_dials_the_vetted_ip(resolver, wire, agent_app):
    client, _ = agent_app
    resolver.table[BRAIN] = [PUBLIC_V4]
    r = _agent_chat(client, stream=False)
    assert r.status_code == 200, r.text
    assert wire[-1]["host"] == PUBLIC_V4 and wire[-1]["sni"] == BRAIN and wire[-1]["hdr_host"] == BRAIN


def test_agent_chat_stream_refuses_rebound_brain(resolver, wire, agent_app):
    client, logs = agent_app
    _saved_earlier(resolver)
    resolver.table[BRAIN] = ["10.0.0.5"]
    r = _agent_chat(client, stream=True)
    assert r.status_code == 200 and "text/event-stream" in r.headers["content-type"]
    body = r.text
    assert "not a permitted egress target" in body and "SECRET-BRAIN-KEY" not in body, body
    assert wire == [] and any("EGRESS BLOCKED" in ln for ln in logs)


def test_agent_chat_stream_dials_the_vetted_ip(resolver, wire, agent_app, monkeypatch):
    client, _ = agent_app
    resolver.table[BRAIN] = [PUBLIC_V4]
    r = _agent_chat(client, stream=True)
    assert r.status_code == 200
    assert wire and wire[-1]["host"] == PUBLIC_V4 and wire[-1]["sni"] == BRAIN


@pytest.fixture
def agent_seat(monkeypatch, saas_env):
    """Autoscope / gauntlet preconditions: a resolved seat brain, a grounded recall."""
    monkeypatch.setattr("saas.seat_brain.resolve_brain", lambda uid, aid: dict(AGENT_BRAIN))
    from src.mcp import server as S

    async def _recall(query, user_id, **k):
        return {"memory": "The business sells widgets and repairs them on site. " * 8}
    monkeypatch.setattr(S, "recall_memory_tool", _recall)
    monkeypatch.setattr(S, "_http_client", object(), raising=False)


def test_autoscope_draft_scope_refuses_rebound_brain_then_dials_vetted_ip(resolver, wire, agent_seat):
    from saas import agent_autoscope as AS
    resolver.table[BRAIN] = ["10.0.0.5"]
    out = asyncio.run(AS.draft_scope("acct", "seat"))
    assert out["status"] == "no_grounding" and wire == [], out
    resolver.set(BRAIN, [PUBLIC_V4])
    asyncio.run(AS.draft_scope("acct", "seat"))
    assert wire and wire[-1]["host"] == PUBLIC_V4 and wire[-1]["sni"] == BRAIN


def test_gauntlet_brain_calls_refuse_rebound_then_dial_vetted_ip(resolver, wire, agent_seat):
    from saas import agent_gauntlet as G
    resolver.table[BRAIN] = ["169.254.169.254"]
    assert asyncio.run(G.judge_answer("acct", "seat", "who wins?")) == ""
    assert asyncio.run(G.compose_website_reference("acct", "seat", "q", "https://site.example.test", "hi")) == ""
    text, usage, hiccups = asyncio.run(G.chat_reply_for_question("acct", "seat", "what do you sell?"))
    assert "brain" in hiccups and wire == [], hiccups
    resolver.set(BRAIN, [PUBLIC_V4])
    asyncio.run(G.judge_answer("acct", "seat", "who wins?"))
    asyncio.run(G.compose_website_reference("acct", "seat", "q", "https://site.example.test", "hi"))
    asyncio.run(G.chat_reply_for_question("acct", "seat", "what do you sell?"))
    assert len(wire) == 3 and all(h["host"] == PUBLIC_V4 for h in wire), wire


def _run_round_env(monkeypatch, ref_url):
    from saas import agent_gauntlet as G, accounts as A
    monkeypatch.setattr(A, "get_agent_gauntlet_config", lambda a, s: {"reference_url": ref_url})
    monkeypatch.setattr(G, "pick_question", lambda g: "what do you sell?")
    monkeypatch.setattr(G, "gold_answer_for", lambda g, q: "")
    monkeypatch.setattr(G, "next_round_no", lambda a, s: 1)
    recorded = []
    monkeypatch.setattr(G, "record_round", lambda *a, **k: recorded.append(k))

    async def reply(a, s, q):
        return ("we sell widgets", {}, {})

    async def judge(a, s, p):
        return "WINNER: A"
    return G, recorded, reply, judge


def test_gauntlet_reference_url_redirect_to_internal_is_refused_through_the_guard(resolver, wire, agent_seat, monkeypatch):
    """The tenant reference site 302s to the metadata host: the first hop is dialled by its
    vetted IP, the redirect is followed THROUGH the transport, the hop is refused, and the
    round records no reference (never a fetch of 169.254.169.254)."""
    resolver.table["site.example.test"] = [PUBLIC_V4]
    resolver.table["meta.example.test"] = ["169.254.169.254"]
    G, recorded, reply, judge = _run_round_env(monkeypatch, "http://site.example.test/redir")
    out = asyncio.run(G.run_round("acct", "seat", agent_reply_fn=reply, judge_fn=judge))
    assert out["reason"] == "no_question_or_reference", out
    assert [(h["host"], h["path"]) for h in wire] == [(PUBLIC_V4, "/redir")], wire


def test_gauntlet_reference_url_public_is_fetched_by_vetted_ip(resolver, wire, agent_seat, monkeypatch):
    resolver.table["site.example.test"] = [PUBLIC_V4]
    resolver.table[BRAIN] = [PUBLIC_V4]
    G, recorded, reply, judge = _run_round_env(monkeypatch, "https://site.example.test/about")
    out = asyncio.run(G.run_round("acct", "seat", agent_reply_fn=reply, judge_fn=judge))
    assert out.get("reason") != "no_question_or_reference", out
    assert wire[0]["host"] == PUBLIC_V4 and wire[0]["path"] == "/about" and wire[0]["sni"] == "site.example.test"


def test_gauntlet_reference_url_rebound_host_refused(resolver, wire, agent_seat, monkeypatch):
    resolver.table["site.example.test"] = ["10.0.0.5"]
    G, recorded, reply, judge = _run_round_env(monkeypatch, "https://site.example.test/about")
    out = asyncio.run(G.run_round("acct", "seat", agent_reply_fn=reply, judge_fn=judge))
    assert out["reason"] == "no_question_or_reference" and wire == []


# ── ROUND 3: the three hand-rolled LLM sites in src/api/main.py ─────────────────────────
#
# ``learn_topic`` (POST /learn — reached by the ADVERTISED MCP tool learn_facts and the
# agent-proxy /expand), ``_seed_domain_operational_ontology`` and
# ``_llm_discover_taxonomy_from_facts`` POSTed ``_get_endpoint_list()`` (= the TENANT brain under
# SaaS) through ``llm_calls._llm_http_client`` — the plain module client — by name, key attached.
# They now go through ``_post_llm_sync``. The recorder here sits at the httpcore CONNECTION POOL
# (below httpx's transport), so a guarded and a plain client cannot be told apart by monkeypatch
# position: only the address the pool is asked to dial is observed. WGM_LLM_MODEL is set as on
# production — without it LLMModels.get() bails before the POST and an empty pool reads green.

@pytest.fixture
def pool_wire(monkeypatch):
    """Recorder at httpcore.(Async)ConnectionPool.handle_(async_)request — the socket layer."""
    import httpcore
    seen = []
    body = json_dumps({"choices": [{"message": {"content": '{"taxonomy_name": null}'}}]})

    def handle(self, request):
        seen.append({"host": request.url.host.decode(), "port": request.url.port,
                     "auth": next((v[:22] for k, v in request.headers
                                   if k.lower() == b"authorization"), None)})
        return httpcore.Response(200, headers=[(b"content-type", b"application/json")],
                                 content=body)

    async def ahandle(self, request):
        return handle(self, request)
    monkeypatch.setattr(httpcore.ConnectionPool, "handle_request", handle)
    monkeypatch.setattr(httpcore.AsyncConnectionPool, "handle_async_request", ahandle)
    getattr(C, "reset_shared_guarded_clients", lambda: None)()
    yield seen
    getattr(C, "reset_shared_guarded_clients", lambda: None)()


def json_dumps(obj):
    import json
    return json.dumps(obj).encode()


@pytest.fixture
def main_brain(monkeypatch, saas_env):
    monkeypatch.setenv("WGM_LLM_MODEL", "qwen/qwen3.5-9b")
    # A FRESH account per test: the sites feed their per-account breaker on every refusal
    # (a refusal is counted like any failure there), so a shared account would open the
    # breaker across tests and a later call would skip the POST for the wrong reason.
    import uuid
    cfg = dict(CFG, api_key="SECRET-BRAIN-KEY-1234", account_id=f"acct-r3-{uuid.uuid4()}")
    token = X.bind_explicit_llm_config(cfg)
    from src.api import llm_calls as _L
    _L.reset_circuit_breaker()
    from src.api import main as M
    yield M
    X.reset_llm_config(token)


class _FakeCur:
    def __init__(self, first):
        self._first, self._n = first, 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        self._n += 1

    def fetchall(self):
        return self._first if self._n == 1 else []

    def fetchone(self):
        return None


class _FakeConn:
    def __init__(self, first):
        self._first = first

    def cursor(self, *a, **k):
        return _FakeCur(self._first)


def _drive_discover(M):
    conn = _FakeConn([("networking", "network things", ["connects_to", "part_of"])])
    return M._llm_discover_taxonomy_from_facts(
        conn, "u", [{"rel_type": "connects_to", "subject": "a", "object": "b"}] * 4)


def _drive_seed(M, monkeypatch):
    # DSN is only READ before the POST (the seed writes after a parseable reply; the canned
    # reply is not one). The local gauntlet test DB, never production.
    monkeypatch.setenv("POSTGRES_DSN", os.environ.get(
        "POSTGRES_DSN", "postgresql://faultline:faultline@172.20.0.3:5432/gauntlet_ie_test"))
    return M._seed_domain_operational_ontology("networking", user_id="u")


def _drive_learn(M):
    from src.api.models import LearnTopicRequest
    return asyncio.run(M.learn_topic(LearnTopicRequest(topic="networking", user_id="u")))


@pytest.mark.parametrize("rebound_to", ["10.0.0.5", "169.254.169.254", "127.0.0.1"])
def test_main_hand_rolled_sites_refuse_rebound_brain_at_the_pool(resolver, pool_wire, main_brain,
                                                               monkeypatch, rebound_to):
    M = main_brain
    _saved_earlier(resolver)
    resolver.table[BRAIN] = [rebound_to]
    assert _drive_discover(M) is None
    assert pool_wire == [], f"taxonomy discovery dialled: {pool_wire}"
    _drive_seed(M, monkeypatch)
    assert pool_wire == [], f"operational seed dialled: {pool_wire}"
    out = _drive_learn(M)
    assert out["status"] == "error" and "blocked address" in out["detail"], out
    assert pool_wire == [], f"learn_topic dialled: {pool_wire}"


def test_main_hand_rolled_sites_dial_the_vetted_ip_at_the_pool(resolver, pool_wire, main_brain,
                                                              monkeypatch):
    M = main_brain
    resolver.table[BRAIN] = [PUBLIC_V4]
    _drive_discover(M)
    _drive_seed(M, monkeypatch)
    _drive_learn(M)
    assert len(pool_wire) == 3, pool_wire
    for hit in pool_wire:
        assert hit["host"] == PUBLIC_V4, hit           # the ADDRESS, never the name
        assert hit["auth"].startswith(b"Bearer SECRET-BRAIN-KE"), hit   # the tenant key still rides


def test_main_hand_rolled_sites_allowlisted_lan_brain_dials_lan_ip(resolver, pool_wire, main_brain,
                                                                  monkeypatch):
    monkeypatch.setenv("SAAS_LLM_ALLOWED_EGRESS", "192.168.40.0/25")
    resolver.table[BRAIN] = [LAN_BRAIN]
    _drive_learn(main_brain)
    assert pool_wire and pool_wire[-1]["host"] == LAN_BRAIN


def test_main_has_no_other_direct_llm_client_post():
    """Every ``_llm_http_client`` use in main.py is inside a ``_post_llm_sync(`` call; every other
    pooled-client ``.post(`` targets Qdrant or the backend (checked by the consumer census)."""
    src = (REPO_ROOT / "src" / "api" / "main.py").read_text(encoding="utf-8")
    assert "_llm_http_client.post(" not in src
    assert src.count("_post_llm_sync(") == 3


def test_mcp_expand_online_source_url_is_fetched_through_the_guard(resolver, wire, saas_env, monkeypatch):
    """``/expand <topic> online <url>`` (the advertised learn_facts tool) fetched a USER-supplied
    URL by name with follow_redirects=True — found by the round-3 consumer census. Now: a host
    resolving internal is refused (the learn continues LLM-only), a public one is dialled by its
    vetted IP, and a 302 to the metadata host is refused at the hop."""
    from src.mcp import server as S
    monkeypatch.setattr(S, "_serve_mode_blocks_divert", lambda uid, tool: False)

    class _Backend:      # the MCP→backend client: ingest-route probe + /learn POST (operator infra)
        async def get(self, url, **k):
            return httpx.Response(200, json={"ingest_enabled": True}, request=httpx.Request("GET", url))

        async def post(self, url, **k):
            return httpx.Response(200, json={"status": "ok"}, request=httpx.Request("POST", url))

        async def aclose(self):
            pass
    monkeypatch.setattr(S, "_http_client", _Backend(), raising=False)
    monkeypatch.setattr(S, "_log", lambda *a, **k: None, raising=False)

    async def run(url):
        await S._learn_via_llm("widgets", "10d7d879-63cd-4f31-92ce-f2c9edb760ab",
                               source_url=url, online=True)
        await asyncio.sleep(0.3)         # let the background task run

    resolver.table["site.example.test"] = ["10.0.0.5"]
    asyncio.run(run("https://site.example.test/page"))
    assert [h for h in wire if h["path"] == "/page"] == [], wire
    resolver.set("site.example.test", [PUBLIC_V4])
    resolver.table["meta.example.test"] = ["169.254.169.254"]
    asyncio.run(run("http://site.example.test/redir"))
    hits = [(h["host"], h["path"]) for h in wire]
    assert (PUBLIC_V4, "/redir") in hits and not any("meta" in h["host"] or h["host"] == "169.254.169.254" for h in wire), hits
