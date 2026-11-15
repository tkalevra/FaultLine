"""ZAI404 — every LLM endpoint URL is built by THE ONE JOIN, and nothing hand-rolls it.

``src/api/llm_client``'s endpoint-resolution header states the rule in its own words: the join
"lives HERE, once, and every site calls it", because fixing only chat leaves a tenant with a
working brain whose other lanes 404. Sites kept not calling it. Twice in one day:

  * the Gemini backend path (a base whose OpenAI-compatible surface lives under
    ``/v1beta/openai/``, resolving to ``/v1/chat/completions`` and 404ing); and
  * the live z.ai failure this suite is named for —
    ``llm_call.all_retries_exhausted final_error_message="Client error '404 Not Found' for url
    'https://api.z.ai/api/paas/v4'" operation=CLASSIFY_CHAIN`` — the BARE BASE, no operation
    path at all, POSTed at a version prefix.

Three sites were hand-rolling the join and DIVERGED from the engine on half the backend types
we support. A console probe that reaches a different URL than the engine will is worse than no
probe: it reports a brain healthy that the engine cannot call. A patch alone would not stop the
third recurrence, so this pins the rule instead of the instance.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LLM_CLIENT = REPO / "src" / "api" / "llm_client.py"

# Every module that builds an outbound LLM URL must go through the join. llm_client itself IS
# the join, and llm_config's _models_discovery_url is the join's own model-list sibling.
_JOIN_OWNERS = {
    "src/api/llm_client.py",      # THE join itself
    "saas/llm_config.py",         # the join's model-list sibling (_models_suffix_for derives
                                  # its suffix from the SAME _BACKEND_PATHS table)
}

# An operation path that only the join is allowed to append.
_OPERATION_TAILS = ("/chat/completions", "/v1/messages", "/v1/models", "/v1/embeddings",
                    "/api/chat/completions", "/api/embeddings")


def _py_files():
    for base in ("src", "saas"):
        for p in sorted((REPO / base).rglob("*.py")):
            yield p


def _appended_operation_paths(path: Path):
    """Yield (lineno, literal) where an operation path is APPENDED ONTO A BASE at runtime.

    ⚠️ TWO WRONG DETECTORS CAME FIRST, and both passed green over live offenders.

    (1) Tokenize and skip COMMENT + STRING. Vacuous: the violations ARE string literals —
        ``url + ("/v1/chat/completions" if ... else "/chat/completions")`` — so skipping every
        STRING token skips exactly what the sweep exists to find. Verified against the pre-fix
        files: 0 detections over 3 real offenders.

    (2) Flag every non-docstring literal ending in an operation path. Too blunt: it flags
        complete DEFAULT URLs from the legacy env chain
        (``http://localhost:11434/v1/chat/completions``), which are configuration values, not
        joins, and it flags the recognition lists that PARSE a known tail off a resolved URL.

    The invariant is narrower and is the actual bug: a RELATIVE operation path (starts with
    "/") combined with a variable — string concatenation, an f-string, ``.format``, or a
    ``.replace`` that swaps one operation path for another. A complete literal URL is a
    configured default and is not this; a tail used in ``endswith``/``in`` is recognition.
    """
    import ast
    try:
        tree = ast.parse(path.read_text())
    except Exception:
        return

    def _is_op_path(v):
        return (isinstance(v, str) and v.startswith("/")
                and any(v == t or v.endswith(t) for t in _OPERATION_TAILS))

    for node in ast.walk(tree):
        # base + "/v1/chat/completions"   (either side)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            for side in (node.left, node.right):
                if isinstance(side, ast.Constant) and _is_op_path(side.value):
                    yield node.lineno, side.value
        # f"{base}/v1/models"
        elif isinstance(node, ast.JoinedStr):
            has_var = any(isinstance(v, ast.FormattedValue) for v in node.values)
            if has_var:
                for v in node.values:
                    if isinstance(v, ast.Constant) and _is_op_path(v.value):
                        yield node.lineno, v.value
        # url.replace("/chat/completions", "/embeddings")  ·  "{}/v1/models".format(base)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("replace", "format", "join"):
                for a in node.args:
                    if isinstance(a, ast.Constant) and _is_op_path(a.value):
                        yield node.lineno, a.value


def test_no_module_appends_an_operation_path_by_hand():
    """The sweep. A string-concatenation of an operation path outside the join is the bug.

    This is what would have caught all three offenders (saas/tenant_console.py twice,
    saas/agent_proxy.py once) the moment they were written.
    """
    offenders = []
    for path in _py_files():
        rel = str(path.relative_to(REPO))
        if rel in _JOIN_OWNERS:
            continue
        for lineno, value in _appended_operation_paths(path):
            offenders.append(f"{rel}:{lineno}: appends {value!r} onto a base by hand")
    assert not offenders, (
        "these sites build an LLM endpoint by hand instead of calling "
        "llm_client.resolve_endpoint / resolve_chat_endpoint — the join must live in ONE "
        "place or a tenant gets a brain that works in one lane and 404s in another:\n  "
        + "\n  ".join(offenders)
    )


def test_the_four_historical_offenders_all_resolve_through_the_join():
    """chat · model list · embeddings · health — the header names these four by name."""
    src = LLM_CLIENT.read_text()
    for fn in ("def get_backend_endpoint(", "def get_embedding_url(", "def get_health_check_url("):
        assert fn in src, f"{fn} vanished — the family is no longer complete"
    body = src.split("def get_backend_endpoint(", 1)[1].split("\ndef ", 1)[0]
    assert "resolve_chat_endpoint(" in body, "chat no longer resolves through the join"
    cfg = (REPO / "saas" / "llm_config.py").read_text()
    disc = cfg.split("def _models_discovery_url(", 1)[1].split("\ndef ", 1)[0]
    assert "resolve_endpoint(" in disc, "the remote model list no longer resolves through the join"


@pytest.mark.parametrize("backend_type,base_url,expected", [
    # THE LIVE BUG. A base that carries its own version segment is one of the three real-world
    # shapes the join already documents — the hand-rolled rule doubled it.
    ("openai", "https://api.z.ai/api/coding/paas/v4",
     "https://api.z.ai/api/coding/paas/v4/chat/completions"),
    # The shapes the hand-rolled builders got wrong because they ignored backend_type.
    ("anthropic", "https://api.anthropic.com", "https://api.anthropic.com/v1/messages"),
    ("gemini", "https://generativelanguage.googleapis.com",
     "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
    ("openwebui", "http://owui:8080", "http://owui:8080/api/chat/completions"),
    # The shapes they got right — pinned so the fix cannot regress them.
    ("openai", "https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1/chat/completions"),
    ("openai", "http://host:8080", "http://host:8080/v1/chat/completions"),
    ("groq", "https://api.groq.com/openai/v1", "https://api.groq.com/openai/v1/chat/completions"),
    # Shape 3: the tenant pasted the FULL endpoint. Collapses onto the root, then rejoins.
    ("openai", "https://api.z.ai/api/coding/paas/v4/chat/completions",
     "https://api.z.ai/api/coding/paas/v4/chat/completions"),
])
def test_the_join_resolves_every_real_world_shape(backend_type, base_url, expected):
    from src.api.llm_client import resolve_chat_endpoint
    assert resolve_chat_endpoint(backend_type, base_url) == expected


def test_raw_returns_the_base_verbatim_which_is_why_a_bare_base_cannot_be_raw():
    """``raw`` means "the base IS the complete URL" — it appends nothing, by contract.

    This is not a bug in the join, and the fix is not to make ``raw`` guess. It is recorded
    here because it is the CONFIGURATION half of the live 404: the stored row was
    ``backend_type='raw'`` with ``base_url='https://api.z.ai/api/coding/paas/v4'``, a bare
    base. The join honoured the contract and POSTed the version prefix. A base with no
    operation tail is not a raw endpoint, and the console should not let one be saved as one.
    """
    from src.api.llm_client import resolve_chat_endpoint
    bare = "https://api.z.ai/api/coding/paas/v4"
    assert resolve_chat_endpoint("raw", bare) == bare, (
        "raw must stay verbatim — a tenant who pasted a complete endpoint relies on it"
    )
    full = bare + "/chat/completions"
    assert resolve_chat_endpoint("raw", full) == full


def test_the_console_probe_and_the_engine_agree_on_every_backend():
    """The property that actually matters: probe URL == engine URL, for every backend type.

    The console reported a 429 (a real provider answer) while the engine got a 404 on a
    different URL for the SAME saved brain. That divergence is the defect; agreement is the
    invariant.
    """
    from src.api.llm_client import _BACKEND_PATHS, resolve_chat_endpoint
    from saas.tenant_console import _provider_backend_type
    for bt in sorted(_BACKEND_PATHS):
        for base in ("https://api.z.ai/api/coding/paas/v4", "https://openrouter.ai/api/v1",
                     "http://host:8080"):
            engine = resolve_chat_endpoint(bt, base)
            console = resolve_chat_endpoint(_provider_backend_type(bt), base)
            assert engine == console, (
                f"console probe and engine disagree for backend {bt!r} on {base!r}: "
                f"{console!r} vs {engine!r}"
            )


def test_provider_label_mapping_asks_the_join_rather_than_listing_providers():
    """The mapper must not become a second provider list that drifts from _BACKEND_PATHS."""
    from saas.tenant_console import _provider_backend_type
    assert _provider_backend_type("openai-compat") == "openai"
    assert _provider_backend_type("") == "openai"
    assert _provider_backend_type("anthropic") == "anthropic"
    assert _provider_backend_type("gemini") == "gemini"
    src = (REPO / "saas" / "tenant_console.py").read_text()
    body = src.split("def _provider_backend_type(", 1)[1].split("\ndef ", 1)[0]
    assert "_BACKEND_PATHS" in body, (
        "the mapper hardcodes provider names instead of consulting the join's own table"
    )
