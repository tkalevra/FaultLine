"""DOCPARSE — the document-lane spaCy PARSE-MODEL split (DOC_SPACY_MODEL).

The contract under test, END TO END (flag exists is not feature works — each pin below
covers one link of the actual chain):

  1. WIRE   — embedder._document_chunk_edges puts "parse_model" on the /harvest-spans
     request body ONLY while DOC_SPACY_MODEL is enabled; absent env → the field is ABSENT
     (byte-identical legacy body) and the /extract/rewrite fallback body NEVER carries it.
  2. MODEL  — RewriteRequest accepts the field, default None (every conversational caller
     is a no-op).
  3. SEAM   — ONLY _harvest_spans_impl reads it; entering it ACTIVATES the request-scoped
     override and exiting RESETS it (proved functionally, not by grep alone).
  4. PARSE  — _parse routes to the doc-lane pipeline under an active override, falls back
     to the default pipeline when the second model cannot load, and stays byte-identical
     legacy with no override.
  5. CACHE  — the second pipeline lazy-loads EXACTLY once (success and failure).
  6. GUARD  — the Dockerfile bakes trf ADDITIVELY (sm untouched) and fails the build
     LOUDLY when the wheel or the load check fails; the runtime env default stays unset.
  7. CROSS  — the pin SURVIVES the background-lane process boundary: run_cpu dispatches
     doc-lane parses through the forked worker pool by default, ContextVars do not cross
     fork(), and _proc_entry must re-bind the pin inside the worker exactly like the
     tenant schema — else every pinned parse silently runs the DEFAULT pipeline.

Fast, DB-free: stub pipelines and captured HTTP bodies, per
tests/test_document_spine_shape.py.
"""
import asyncio
import re

import pytest

from src.api import models as api_models
from src.extraction import linguistics as ling
from src.re_embedder import embedder

DOC_MODEL = "en_core_web_trf"
DEF_MODEL = "en_core_web_sm"


class _StubDoc:
    def __init__(self, text):
        self.text = text


class _StubNlp:
    """Marks WHICH pipeline parsed, and counts loads/calls."""

    def __init__(self, name):
        self.name = name
        self.calls = 0

    def __call__(self, text):
        self.calls += 1
        return _StubDoc(text)


@pytest.fixture(autouse=True)
def _no_override_leak():
    """No test may leak the request-scoped override into another test."""
    yield
    assert ling._PARSE_MODEL_OVERRIDE.get() is None, \
        "a test left the parse-model override SET — reset discipline broken"


# ── 1. WIRE — the doc-lane caller sends the field only when enabled ──────────────────


class _StubResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


CHUNK = "[Date: 2023/05/22 (Mon) 18:21]\nWalkmeter is an excellent app for tracking walks."


def _capture_bodies(monkeypatch):
    bodies = {}

    def _post(url, json=None, timeout=None, headers=None):  # noqa: A002 - mirrors httpx
        bodies[url.rsplit("/", 1)[-1]] = json
        return _StubResponse({"status": "success", "edges": []})

    monkeypatch.setattr(embedder.httpx, "post", _post)
    return bodies


def test_embedder_knob_defaults_to_disabled():
    """Code default: DOC_SPACY_MODEL unset/empty in the ambient test env — capability OFF."""
    assert embedder.DOC_SPACY_MODEL == ""


def test_enabled_lane_sends_parse_model_on_harvest_body(monkeypatch):
    monkeypatch.setattr(embedder, "DOC_CHUNK_SKIP_CONTENTLESS", True)
    monkeypatch.setattr(embedder, "DOC_SPACY_MODEL", DOC_MODEL)
    monkeypatch.setattr(embedder, "DOC_L1_DETERMINISTIC_FIRST", False)
    monkeypatch.setattr(embedder, "_brain_lane_slow", lambda uid: None)
    bodies = _capture_bodies(monkeypatch)
    embedder._document_chunk_edges(CHUNK, "user-1", "http://backend", "rewrite")
    assert bodies["harvest-spans"]["parse_model"] == DOC_MODEL


def test_disabled_lane_sends_no_parse_model_field(monkeypatch):
    """BYTE-IDENTICAL LEGACY: absent env → the field is absent from the body entirely."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_SKIP_CONTENTLESS", True)
    monkeypatch.setattr(embedder, "DOC_SPACY_MODEL", "")
    bodies = _capture_bodies(monkeypatch)
    embedder._document_chunk_edges(CHUNK, "user-1", "http://backend", "rewrite")
    assert "parse_model" not in bodies["harvest-spans"]


def test_parse_model_never_leaks_onto_the_rewrite_call(monkeypatch):
    """/extract/rewrite has no such concept — its body stays exactly as it was."""
    monkeypatch.setattr(embedder, "DOC_CHUNK_SKIP_CONTENTLESS", True)
    monkeypatch.setattr(embedder, "DOC_SPACY_MODEL", DOC_MODEL)
    bodies = _capture_bodies(monkeypatch)
    embedder._document_chunk_edges(CHUNK, "user-1", "http://backend", "rewrite")
    assert set(bodies["rewrite"]) == {"text", "user_id", "force_relation_extraction"}


# ── 2. MODEL — the request field ─────────────────────────────────────────────────────


def test_request_model_parse_field_defaults_to_none():
    assert api_models.RewriteRequest(text="hi").parse_model is None
    assert (api_models.RewriteRequest(text="hi", parse_model=DOC_MODEL).parse_model
            == DOC_MODEL)


# ── resolver — the env is the capability switch ──────────────────────────────────────


def test_resolver_field_absent_is_none(monkeypatch):
    monkeypatch.setattr(ling, "_DOC_SPACY_MODEL", DOC_MODEL)
    assert ling.resolve_parse_model_request(None) is None
    assert ling.resolve_parse_model_request("") is None
    assert ling.resolve_parse_model_request("   ") is None


def test_resolver_capability_off_ignores_any_name(monkeypatch):
    """DOC_SPACY_MODEL unset = the capability is OFF: no request can turn it on."""
    monkeypatch.setattr(ling, "_DOC_SPACY_MODEL", "")
    assert ling.resolve_parse_model_request(DOC_MODEL) is None


def test_resolver_names_default_is_none_not_a_second_load(monkeypatch):
    """Pinning SPACY_MODEL itself is the legacy route; it must not double-load."""
    monkeypatch.setattr(ling, "_SPACY_MODEL", DEF_MODEL)
    monkeypatch.setattr(ling, "_DOC_SPACY_MODEL", DOC_MODEL)
    assert ling.resolve_parse_model_request(DEF_MODEL) is None


def test_resolver_honors_only_the_enabled_model(monkeypatch):
    monkeypatch.setattr(ling, "_SPACY_MODEL", DEF_MODEL)
    monkeypatch.setattr(ling, "_DOC_SPACY_MODEL", DOC_MODEL)
    assert ling.resolve_parse_model_request(DOC_MODEL) == DOC_MODEL
    # ANY other name — including a path — is refused: the process never spacy.loads an
    # arbitrary request-supplied name.
    for bogus in ("en_core_web_md", "/tmp/evil/model", "en_core_web_sm; rm -rf /"):
        assert ling.resolve_parse_model_request(bogus) is None


# ── 3. SEAM — only the harvest impl activates it, and resets it ──────────────────────


def test_only_the_harvest_impl_reads_the_field():
    """CENSUS: the sole getattr(req, "parse_model") read lives inside
    _harvest_spans_impl (before the _harvest_spans_body handoff) — no other endpoint
    (not /extract/rewrite, not /ingest, not recall) may honor the field."""
    src = open("src/api/main.py", encoding="utf-8").read()
    reads = [m.start() for m in re.finditer(r'getattr\(req,\s*"parse_model"', src)]
    assert len(reads) == 1, f"parse_model must be read at exactly ONE site, got {len(reads)}"
    impl = src.index("async def _harvest_spans_impl")
    body = src.index("async def _harvest_spans_body")
    assert impl < reads[0] < body, \
        "the parse_model read must sit inside _harvest_spans_impl, before the body handoff"
    # nothing BEFORE the harvest impl (i.e. every other endpoint defined earlier) reads it
    assert not re.findall(r'"parse_model"', src[:impl]), \
        "an endpoint earlier than /harvest-spans reads parse_model"


def _fake_body_capture(monkeypatch, sink):
    import src.api.main as main

    async def _fake_body(req):
        sink.append(ling._PARSE_MODEL_OVERRIDE.get())
        return {"edges": []}

    monkeypatch.setattr(main, "_harvest_spans_body", _fake_body)


def test_harvest_impl_activates_the_override_functionally(monkeypatch):
    """THE END-TO-END SEAM: the wrapper activates the ContextVar the deriver reads, and
    resets it on exit. _harvest_spans_body is stubbed to CAPTURE the active override —
    this proves the EFFECT (an active pin during the harvest), not merely the wiring."""
    import src.api.main as main

    monkeypatch.setattr(ling, "_SPACY_MODEL", DEF_MODEL)
    monkeypatch.setattr(ling, "_DOC_SPACY_MODEL", DOC_MODEL)
    seen = []
    _fake_body_capture(monkeypatch, seen)

    req = api_models.RewriteRequest(text="dense prose", user_id="u1", parse_model=DOC_MODEL)
    out = asyncio.run(main._harvest_spans_impl(req))
    assert out == {"edges": []}
    assert seen == [DOC_MODEL], "the override must be ACTIVE inside the harvest body"
    assert ling._PARSE_MODEL_OVERRIDE.get() is None, "the override must RESET after"


def test_harvest_impl_field_absent_activates_nothing(monkeypatch):
    import src.api.main as main

    monkeypatch.setattr(ling, "_SPACY_MODEL", DEF_MODEL)
    monkeypatch.setattr(ling, "_DOC_SPACY_MODEL", DOC_MODEL)
    seen = []
    _fake_body_capture(monkeypatch, seen)
    asyncio.run(main._harvest_spans_impl(
        api_models.RewriteRequest(text="chat turn", user_id="u1")))
    assert seen == [None], "absent field = byte-identical legacy route"


def test_harvest_impl_unknown_model_activates_nothing(monkeypatch):
    import src.api.main as main

    monkeypatch.setattr(ling, "_SPACY_MODEL", DEF_MODEL)
    monkeypatch.setattr(ling, "_DOC_SPACY_MODEL", DOC_MODEL)
    seen = []
    _fake_body_capture(monkeypatch, seen)
    asyncio.run(main._harvest_spans_impl(
        api_models.RewriteRequest(text="x", user_id="u1", parse_model="not-a-model")))
    assert seen == [None], "a non-enabled name must never activate"
    assert ling._PARSE_MODEL_OVERRIDE.get() is None


def test_harvest_impl_broken_seam_degrades_to_legacy(monkeypatch):
    """The pin must never break the harvest: a resolver crash degrades to no override."""
    import src.api.main as main

    def _boom(*a, **k):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(ling, "resolve_parse_model_request", _boom)
    seen = []
    _fake_body_capture(monkeypatch, seen)
    out = asyncio.run(main._harvest_spans_impl(
        api_models.RewriteRequest(text="x", user_id="u1", parse_model=DOC_MODEL)))
    assert out == {"edges": []}
    assert seen == [None]


# ── 4. PARSE — the routing ───────────────────────────────────────────────────────────


def test_parse_routes_to_doc_pipeline_under_override(monkeypatch):
    sm, trf = _StubNlp("sm"), _StubNlp("trf")
    monkeypatch.setattr(ling, "_get_nlp", lambda: sm)
    monkeypatch.setattr(ling, "_get_doc_nlp", lambda: trf)
    monkeypatch.setattr(ling, "_SPACY_MODEL", DEF_MODEL)
    monkeypatch.setattr(ling, "_DOC_SPACY_MODEL", DOC_MODEL)

    d0 = ling._parse("legacy turn")
    assert d0.text == "legacy turn" and sm.calls == 1 and trf.calls == 0

    with ling.parse_model_for_request(DOC_MODEL):
        d1 = ling._parse("document chunk")
        assert d1.text == "document chunk" and sm.calls == 1 and trf.calls == 1
    # after exit: legacy route restored
    ling._parse("back to legacy")
    assert sm.calls == 2 and trf.calls == 1


def test_parse_override_with_capability_off_degrades_to_legacy(monkeypatch):
    """A stray override (env disabled) still parses — on the DEFAULT pipeline."""
    sm = _StubNlp("sm")
    monkeypatch.setattr(ling, "_get_nlp", lambda: sm)
    monkeypatch.setattr(ling, "_get_doc_nlp", lambda: None)  # second model unavailable
    monkeypatch.setattr(ling, "_SPACY_MODEL", DEF_MODEL)
    monkeypatch.setattr(ling, "_DOC_SPACY_MODEL", "")
    with ling.parse_model_for_request(DOC_MODEL):
        assert ling._parse("x").text == "x"
    assert sm.calls == 1


def test_parse_unloadable_doc_model_degrades_to_legacy(monkeypatch):
    """The split is a CAPABILITY, never a hard dep: load failure → default pipeline."""
    sm = _StubNlp("sm")
    monkeypatch.setattr(ling, "_get_nlp", lambda: sm)
    monkeypatch.setattr(ling, "_get_doc_nlp", lambda: None)
    monkeypatch.setattr(ling, "_SPACY_MODEL", DEF_MODEL)
    monkeypatch.setattr(ling, "_DOC_SPACY_MODEL", DOC_MODEL)
    with ling.parse_model_for_request(DOC_MODEL):
        assert ling._parse("x").text == "x"
    assert sm.calls == 1


def test_parse_pins_the_same_default_model_without_a_second_load(monkeypatch):
    """An override naming SPACY_MODEL itself routes LEGACY (no second pipeline consulted)."""
    sm = _StubNlp("sm")
    monkeypatch.setattr(ling, "_get_nlp", lambda: sm)

    def _no():
        raise AssertionError("must not consult the doc pipeline")

    monkeypatch.setattr(ling, "_get_doc_nlp", _no)
    monkeypatch.setattr(ling, "_SPACY_MODEL", DEF_MODEL)
    with ling.parse_model_for_request(DEF_MODEL):
        ling._parse("x")
    assert sm.calls == 1


# ── 5. CACHE — lazy, once, failure cached ────────────────────────────────────────────


@pytest.fixture()
def _fresh_doc_cache():
    saved = (ling._doc_nlp, ling._doc_nlp_load_attempted)
    ling._doc_nlp = None
    ling._doc_nlp_load_attempted = False
    yield
    ling._doc_nlp, ling._doc_nlp_load_attempted = saved


def test_doc_pipeline_loads_exactly_once(monkeypatch, _fresh_doc_cache):
    import spacy
    loads = []
    fake = _StubNlp("fake-trf")

    def _fake_load(name, disable=None):
        loads.append(name)
        return fake

    monkeypatch.setattr(spacy, "load", _fake_load)
    monkeypatch.setattr(ling, "_DOC_SPACY_MODEL", DOC_MODEL)
    a = ling._get_doc_nlp()
    b = ling._get_doc_nlp()
    assert a is b is fake
    assert loads == [DOC_MODEL], "the second pipeline must lazy-load EXACTLY once"


def test_doc_pipeline_failure_is_cached_never_retried(monkeypatch, _fresh_doc_cache):
    import spacy
    loads = []

    def _fake_load(name, disable=None):
        loads.append(name)
        raise OSError("model not baked")

    monkeypatch.setattr(spacy, "load", _fake_load)
    monkeypatch.setattr(ling, "_DOC_SPACY_MODEL", DOC_MODEL)
    assert ling._get_doc_nlp() is None
    assert ling._get_doc_nlp() is None  # second call must NOT retry
    assert ling._get_doc_nlp() is None
    assert loads == [DOC_MODEL]


def test_doc_pipeline_env_absent_never_touches_spacy(monkeypatch, _fresh_doc_cache):
    import spacy

    def _no(*a, **k):
        raise AssertionError("spacy.load must not run with DOC_SPACY_MODEL unset")

    monkeypatch.setattr(spacy, "load", _no)
    monkeypatch.setattr(ling, "_DOC_SPACY_MODEL", "")
    assert ling._get_doc_nlp() is None


# ── 6. GUARD — the Dockerfile bake is additive and loud ──────────────────────────────


def test_dockerfile_sm_bake_untouched_and_default():
    """Vehicle update (foss-es uplift port): the conversational default bake on THIS branch is
    the es multilingual form — `es_core_news_sm` (the branch IS the language; the English
    model over Spanish text is silent corruption, and linguistics.py's language guard
    refuses the mismatch). The pin's mechanism is unchanged: the default sm wheel stays
    baked, parametrized, and additive with the doc-lane trf wheel."""
    src = open("Dockerfile", encoding="utf-8").read()
    assert "ARG SPACY_MODEL=es_core_news_sm" in src, \
        "the conversational default bake must stay es_core_news_sm (the branch IS the language)"
    assert "releases/download/${SPACY_MODEL}-${SPACY_MODEL_VERSION}/" in src, \
        "the original sm wheel install must remain"


def test_dockerfile_trf_bake_present_additive_and_loud():
    src = open("Dockerfile", encoding="utf-8").read()
    assert "en_core_web_trf" in src, "the doc-lane trf wheel must be baked"
    # the whole doc-lane bake block, from the bake ARG to the load check
    start = src.index("ARG DOC_SPACY_MODEL_BAKE")
    block = src[start:src.index("RUN python -m nltk.downloader", start)]
    wheel = "releases/download/" + chr(36) + "{DOC_SPACY_MODEL_BAKE}-"
    assert wheel in block, "the trf wheel install (GitHub release URL) must be in the bake block"
    assert '|| { echo "FATAL:' in block and "exit 1; }" in block, \
        ("the trf install RUN must carry the loud-fail guard (|| { echo FATAL; exit 1; }) — "
         "a failed bake must fail the build, never fall back silently")
    # the model must be proven LOADABLE at build (a wheel that installs but cannot
    # build its transformer pipeline must stop the build too)
    assert ("RUN DOC_MODEL_NAME=" in block and "spacy.load" in block
            and "spacy_transformers" in block), \
        "a build-time load check importing the trf stack is required"


def test_dockerfile_doc_model_is_not_a_runtime_default():
    """ENABLEMENT IS AN OPERATOR CHOICE: the image must NOT export DOC_SPACY_MODEL as a
    runtime ENV — the code default (unset = byte-identical legacy) stays in charge."""
    src = open("Dockerfile", encoding="utf-8").read()
    assert not re.search(r"^ENV DOC_SPACY_MODEL=", src, re.M), \
        "baking the wheel must not silently enable the split on rebuild"



# ── 7. PROCESS-LANE CROSSING — the pin survives the background CPU lane ────────────────────
# The doc-lane drain rides x-fl-llm-lane: background, and run_cpu on the background lane
# dispatches through the FORKED WORKER POOL by default (DOC_LANE_CPU_PROCESSES defaults
# to 2). ContextVars cannot cross fork(): without shipping the pin in the process
# payload, every pinned parse would silently run the DEFAULT pipeline inside the worker
# — exactly the no-op this whole split exists to prevent. The tenant schema already
# crossed that boundary; the parse-model pin crosses the same way.


def _pin_sensor():
    """Module-level (picklable) probe: what pin does the WORKER see?"""
    return ling._PARSE_MODEL_OVERRIDE.get()


def test_parse_model_pin_crosses_the_process_lane_payload(monkeypatch):
    """THE BOUNDARY PIN: _dispatch_process must capture the request's parse-model pin
    and _proc_entry must re-bind it inside the worker (the schema's own ship-across
    discipline). Fails — the sensor sees None — if the pin is dropped at the fork
    boundary and the parse silently runs the default pipeline in the worker."""
    import concurrent.futures

    from src.api import cpu_lane

    monkeypatch.setattr(ling, "_SPACY_MODEL", DEF_MODEL)
    monkeypatch.setattr(ling, "_DOC_SPACY_MODEL", DOC_MODEL)
    monkeypatch.setattr(cpu_lane, "_PROC_N", 1)
    _shim_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(cpu_lane, "_proc_pool", lambda: _shim_pool)

    async def _run():
        return await cpu_lane._dispatch_process(_pin_sensor, (), {})

    with ling.parse_model_for_request(DOC_MODEL):
        out = asyncio.run(_run())
    _shim_pool.shutdown(wait=True)
    assert out == DOC_MODEL, (
        "the worker must see the request's parse-model pin — None here means the pin "
        "was dropped at the process boundary and the pinned parse silently ran the "
        "DEFAULT pipeline (the silent-no-op this test exists to catch)")
    assert ling._PARSE_MODEL_OVERRIDE.get() is None, "the caller's context is untouched"


def test_process_lane_entry_resets_the_pin_after_the_call(monkeypatch):
    """Worker discipline: after a pinned call the worker's binding returns to None — a
    pooled worker must never leak one request's pin into the next call (the same rule
    the tenant schema obeys). Pinned by running a SECOND, unpinned call on the same
    worker thread and proving it reads None."""
    import concurrent.futures

    from src.api import cpu_lane

    monkeypatch.setattr(ling, "_SPACY_MODEL", DEF_MODEL)
    monkeypatch.setattr(ling, "_DOC_SPACY_MODEL", DOC_MODEL)
    monkeypatch.setattr(cpu_lane, "_PROC_N", 1)
    _shim_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(cpu_lane, "_proc_pool", lambda: _shim_pool)

    async def _run():
        return await cpu_lane._dispatch_process(_pin_sensor, (), {})

    with ling.parse_model_for_request(DOC_MODEL):
        first = asyncio.run(_run())
    second = asyncio.run(_run())  # NO pin active in the caller now
    _shim_pool.shutdown(wait=True)
    assert first == DOC_MODEL, "the pinned call must ship the pin"
    assert second is None, (
        "the worker leaked the pin into the next call (second=%r) — the finally-reset "
        "in _proc_entry is broken" % (second,))


def test_process_lane_worker_ignores_a_stale_inherited_pin(monkeypatch):
    """THE STALE-FORK PIN: a worker process is forked from whatever thread submitted
    first — if that thread was mid-request with a pin set, the child's copied thread
    state INHERITS it, and a token reset would faithfully restore the stale value.
    The worker must therefore WRITE the default (None) at entry and after every call:
    a call that ships NO pin must parse unpinned even in a stale-pinned worker.
    Pinned by calling _proc_entry directly with no shipped pin while a pin is set."""
    from src.api import cpu_lane

    monkeypatch.setattr(ling, "_SPACY_MODEL", DEF_MODEL)
    monkeypatch.setattr(ling, "_DOC_SPACY_MODEL", DOC_MODEL)

    with ling.parse_model_for_request(DOC_MODEL):  # the stale inherited state
        out = cpu_lane._proc_entry(_pin_sensor, (), {}, None, None)
    assert out == ("ok", None), (
        "a no-pin call inside a stale-pinned worker must parse UNPINNED (sensor=%r "
        "— the worker wrote the shipped state, not its fork-inherited leftover)" % (out,))
    assert ling._PARSE_MODEL_OVERRIDE.get() is None, "the caller context is restored"
