"""EMBED-SEAM CONNECT/READ SPLIT - critic follow-up to BUG-brain-connect-timeout-burns-full-budget-20261026.

55d31515 split the connect phase at every CHAT POST seam but missed the EMBEDDINGS POST
family: embedder.embed_text's external-fallback post (3 client branches; the drain passes
a bare 30.0, several callers a bare 10.0) and llm_output_validator._embed_text (bare 10.0).
A bare number sets httpx's CONNECT phase to the FULL budget, so a SYN-blackholed
embeddings host burned it at dial - and the embed path has NO breaker to bound the storm.
The fix normalizes with the same split_connect_timeout() rule as the chat seams: connect =
LLM_CONNECT_TIMEOUT_S, read budget untouched, a tighter caller connect never widened, the
local fastembed path (the common case) fully untouched.
"""

import asyncio
import os
import sys
from unittest.mock import MagicMock

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fake_embed_response():
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    resp.json = lambda: {"data": [{"embedding": [0.1, 0.2, 0.3]}]}
    return resp


def test_the_embedder_external_fallback_post_gets_the_split(monkeypatch):
    """THE drain's shape: bare timeout=30.0 on the external embeddings post."""
    from src.re_embedder import embedder as E
    monkeypatch.setattr(E, "_get_local_embedder", lambda: None)  # force the external path
    captured = {}

    class _Fake:
        def post(self, url, **kw):
            captured.update(kw)
            return _fake_embed_response()

    monkeypatch.setattr(E, "_http_client_sync", _Fake())
    monkeypatch.setenv("LLM_CONNECT_TIMEOUT_S", "5.0")
    vec = E.embed_text("hello", "http://brain.invalid", timeout=30.0,
                       embedding_url="http://embed.invalid/v1/embeddings")
    assert vec == [0.1, 0.2, 0.3]
    t = captured["timeout"]
    assert isinstance(t, httpx.Timeout), "the seam must normalize, not forward the bare number"
    assert t.read == 30.0, "connected-but-slow keeps the full read budget"
    assert t.connect == 5.0, "a dead embeddings host must be refused at the knob, not 30s"


def test_the_embedder_never_widens_a_tighter_caller_connect(monkeypatch):
    from src.re_embedder import embedder as E
    monkeypatch.setattr(E, "_get_local_embedder", lambda: None)
    captured = {}

    class _Fake:
        def post(self, url, **kw):
            captured.update(kw)
            return _fake_embed_response()

    monkeypatch.setattr(E, "_http_client_sync", _Fake())
    E.embed_text("hello", "http://brain.invalid",
                 timeout=httpx.Timeout(30.0, connect=2.0),
                 embedding_url="http://embed.invalid/v1/embeddings")
    assert captured["timeout"].connect == 2.0, "a tighter caller connect is never widened"


def test_the_validator_embed_post_gets_the_split(monkeypatch):
    """llm_output_validator._embed_text posts bare 10.0 - same rule applies."""
    import src.api.llm_client as LC
    import src.api.llm_output_validator as V
    from src.api import llm_calls as L
    monkeypatch.setenv("EMBEDDING_API_URL", "http://embed.invalid/v1/embeddings")
    monkeypatch.setattr(LC, "saas_global_brain_blocked", lambda: False)
    # order-proof: no suite pollution may redirect the door to the tenant branch
    monkeypatch.setattr(L, "_tenant_byo_cfg", lambda: None)
    captured = {}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            captured.update(kw)
            return _fake_embed_response()

    monkeypatch.setattr(V.httpx, "AsyncClient", lambda **kw: _FakeClient())
    v = V.LLMOutputValidator(db_conn=None, llm_endpoint="http://brain.invalid")
    vec = asyncio.run(v._embed_text("hello"))
    assert vec == [0.1, 0.2, 0.3]
    t = captured["timeout"]
    assert isinstance(t, httpx.Timeout)
    assert t.read == 10.0, "the validator's read budget is untouched"
    assert t.connect == 5.0, "a dead embeddings host must be refused at the knob, not 10s"
