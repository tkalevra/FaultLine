"""The console's endpoint preview must agree with the resolver, byte-for-byte.

``saas/dashboard/endpoint_preview.js`` shows a tenant the URL FaultLine will actually call.
That is only worth anything if it is TRUE — a preview that drifts from
``src/api/llm_client.resolve_endpoint`` is worse than no preview, because it would confirm a
config that then 404s, which is exactly the failure mode this whole change exists to kill.

So the mirror is not trusted: this runs the REAL JavaScript file under node against the REAL
Python resolver over a shared case table, and fails on any divergence.

⚠️ This test needs ``node`` on PATH. It is NOT ``skipif``-guarded on purpose: a silent skip
here means the parity claim is unverified while the suite still reads green, and this project
has been burned by exactly that. If node is genuinely unavailable in some environment, that is
an environment gap to fix, not a test to soften.

Run: python3 tools/fltest.py --bug ENDPOINTS --test tests/test_endpoint_preview_parity.py
     (tests/ is gitignored → git add -f)
"""

import json
import pathlib
import shutil
import subprocess

import pytest

from src.api import llm_client

_REPO = pathlib.Path(__file__).resolve().parents[1]
_JS = _REPO / "saas" / "dashboard" / "endpoint_preview.js"

# (backend_type, base_url) — every shape that matters, including the ones that were broken.
CASES = [
    ("openai", "https://api.z.ai/api/paas/v4"),
    ("openai", "https://api.z.ai/api/paas/v4/"),
    ("openai", "https://api.z.ai/api/paas/v4/chat/completions"),
    ("raw", "https://api.z.ai/api/paas/v4/chat/completions"),
    ("openai", "https://dashscope-intl.aliyuncs.com/compatible-mode/"),
    ("openai", "https://dashscope-intl.aliyuncs.com/compatible-mode"),
    ("openai", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"),
    ("openai", "https://api.cerebras.ai"),
    ("openai", "https://api.openai.com/v1"),
    ("openai", "https://api.openai.com/v1/chat/completions"),
    ("anthropic", "https://api.anthropic.com"),
    ("anthropic", "https://api.anthropic.com/v1"),
    ("anthropic", "https://api.anthropic.com/v1/messages"),
    ("groq", "https://api.groq.com"),
    ("groq", "https://api.groq.com/v1"),
    ("groq", "https://api.groq.com/openai/v1"),
    ("openwebui", "http://open-webui:8080"),
    ("openwebui", "http://open-webui:8080/api/chat/completions"),
    ("openwebui", "http://gw.example/v1"),
    ("ollama", "http://localhost:11434"),
    ("lm_studio", "http://192.168.1.20:1234"),
    ("lm_studio", "192.168.1.20:1234"),
    ("localai", "http://localai:8080"),
    ("openai", "https://generativelanguage.googleapis.com/v1beta"),
    ("openai", "https://host.example/api/v1beta1"),
    ("openai", "https://host.example/v2ray"),
    ("openai", "https://host.example/v"),
    ("openai", "https://v4.example.com"),
    ("openai", "https://x.openai.azure.com/openai?api-version=2024-02-01"),
    ("not-a-real-backend", "https://host.example"),
    ("raw", "https://host.example/whatever/it/is"),
]

_HARNESS = r"""
const api = require(process.argv[1]);
const cases = JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify(
  cases.map(([bt, base]) => api.resolveChat(bt, base))));
"""


def _brain_backend_select(page: str) -> str:
    """Just the ENGINE-BRAIN ``#llm-backend`` <select>.

    Scoped deliberately: ``tenant.html`` carries a SECOND, unrelated provider dropdown
    (``#ca-chatllm-prov``, the per-agent CHAT model people talk to — different vocabulary,
    different backend, ``saas/agent_proxy.py``). It is out of scope here and must not be
    dragged in by a whole-file grep.
    """
    html = (_REPO / "saas" / "dashboard" / page).read_text(encoding="utf-8")
    start = html.index('<select id="llm-backend">')
    return html[start:html.index("</select>", start)]


def _node_results():
    node = shutil.which("node")
    assert node, ("node is required to verify the console preview mirrors the Python "
                  "resolver; without it the parity claim is unverified")
    out = subprocess.run(
        [node, "-e", _HARNESS, str(_JS), json.dumps(CASES)],
        capture_output=True, text=True, timeout=60, cwd=str(_REPO))
    assert out.returncode == 0, f"node failed: {out.stderr[:2000]}"
    return json.loads(out.stdout)


def test_endpoint_preview_js_file_exists():
    assert _JS.is_file(), f"missing {_JS}"


def test_js_preview_matches_python_resolver_for_every_shape(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.setattr(llm_client, "_tenant_brain", lambda: None)

    js = _node_results()
    assert len(js) == len(CASES)

    mismatches = []
    for (backend_type, base_url), js_url in zip(CASES, js):
        py_url = llm_client.resolve_chat_endpoint(backend_type, base_url)
        if py_url != js_url:
            mismatches.append(f"{backend_type} {base_url!r}: py={py_url!r} js={js_url!r}")
    assert not mismatches, "console preview has DRIFTED from the resolver:\n" + "\n".join(
        mismatches)


def test_js_backend_path_table_matches_python():
    """The mirrored ``_BACKEND_PATHS`` table itself, not just its outputs — so a backend type
    added on one side and forgotten on the other is caught immediately."""
    node = shutil.which("node")
    assert node, "node is required (see module docstring)"
    out = subprocess.run(
        [node, "-e", "process.stdout.write(JSON.stringify(require(process.argv[1])"
                     ".BACKEND_PATHS));", str(_JS)],
        capture_output=True, text=True, timeout=60, cwd=str(_REPO))
    assert out.returncode == 0, out.stderr[:2000]
    assert json.loads(out.stdout) == dict(llm_client._BACKEND_PATHS)


def test_dashboard_js_has_no_inline_script_or_handlers():
    """CSP is ``script-src 'self'`` with no ``'unsafe-inline'``. Both pages that load the
    preview must stay free of inline script and ``on*=`` handlers."""
    for page in ("tenant.html", "user.html"):
        html = (_REPO / "saas" / "dashboard" / page).read_text(encoding="utf-8")
        assert "endpoint_preview.js" in html, f"{page} does not load the preview"
        for marker in (" onclick=", " onchange=", " oninput=", " onload=", " onsubmit="):
            assert marker not in html.lower(), f"{page} carries an inline handler {marker!r}"


@pytest.mark.parametrize("page", ["tenant.html", "user.html"])
def test_saas_consoles_no_longer_offer_local_only_backends(page):
    select = _brain_backend_select(page)
    for local in ("ollama", "lm_studio", "openwebui", "localai"):
        assert f'value="{local}"' not in select, (
            f"{page} still offers the local-only backend {local!r}")


@pytest.mark.parametrize("page", ["tenant.html", "user.html"])
def test_saas_consoles_offer_the_escape_hatch(page):
    """The whole point: a provider we did not anticipate must be reachable from the UI."""
    assert 'value="raw"' in _brain_backend_select(page)


@pytest.mark.parametrize("page", ["tenant.html", "user.html"])
def test_saas_console_options_match_the_server_accept_list(page):
    """The dropdown is guidance and the accept-list is enforcement — if they disagree, a
    tenant picks something the save door then 400s. They are asserted equal."""
    import re
    from saas.llm_config import SAAS_OFFERED_BACKENDS
    offered = {v for v in re.findall(r'value="([^"]*)"', _brain_backend_select(page)) if v}
    assert offered == set(SAAS_OFFERED_BACKENDS)
