"""FRONT-DOOR E2E — the auto-posture against a STUB SLOW BRAIN (issue #229, no real brain).

The shape the issue's wound described, replayed end to end through the REAL front door:

    MCP remember_facts turn (in-process) → backend (uvicorn thread, real socket) → stub brain

  1. WARM     — stub answers fast; one turn pays the residual model loads and proves the
                healthy door (the fact lands, alias + Class-A edge).
  2. WOUND    — the stub starts answering only after 30s (the NVIDIA shape). Each degraded
                turn: REFRAME waits out its STATIC budget (6s), the harvest is DEGRADED and
                the edges are WITHHELD (the standing brain-not-transport doctrine — the turn
                is retained verbatim in episodic_log for re-extraction, never silently
                committed), and the failed attempt feeds the posture ONE latency sample.
  3. TIGHTEN  — the second degraded turn's sample reaches quorum → the posture TIGHTENS
                REFRAME to the fail-open floor for this brain: growth defers, the POSTURE
                lane row lands, and the NEXT degraded turn gives up in floor-time instead of
                budget-time — the wound's 60s+ wait-sum, bounded automatically.
  4. RECOVER  — cooldown expires (short window at demo scale); static budget restored,
                growth resumes, the POSTURE row recovers to ok — and a fresh statement
                ingests cleanly (the fact lands; nothing was lost by the tighten window).

Everything is a documented env knob at demo scale: static REFRAME 6s, MIN_SAMPLES 2,
COOLDOWN 15s; the FLOOR stays at its 4s default (the value the tight turn actually runs at).
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid

import pytest

pytest.importorskip("uvicorn", reason="E2E drives the backend over real HTTP")

E2E_FLAGS = {
    # the demo-scale posture window (documented knobs; the floor stays at its 4s default).
    # COOLDOWN 45s: comfortably longer than the measured phase-2→4 span (~30-40s incl. the
    # ~10s classify retry hiccups a cold-ish GLiNER adds) so the tighten window cannot
    # expire between the wound and the assert — and still observable inside the test.
    "LLM_TIMEOUT_REFRAME": "6",            # the wound's static budget, scaled to demo time
    "BRAIN_POSTURE_MIN_SAMPLES": "2",      # tighten after 2 over-budget samples
    "BRAIN_POSTURE_COOLDOWN_S": "45",      # recovery observable inside the test
    "LLM_CIRCUIT_BREAKER_THRESHOLD": "50", # keep the breaker CLOSED so the POSTURE is what
                                           # saves the turn (breaker short-circuit would
                                           # confound the tightened-budget measurement)
    # the spine route (the REFRAME → deterministic-fail-open path this E2E exercises)
    "SENTENCE_PIPELINE": "true",
    "SPACY_MODEL": "en_core_web_sm",
    # the stub brain is loopback — the OPERATOR egress escape hatch (the sanctioned way
    # pre-prod reaches a LAN brain; never a tenant-self-allowlist)
    "SAAS_LLM_ALLOWED_EGRESS": "127.0.0.1/8",
    "QDRANT_URL": "http://127.0.0.1:1",    # vector lane is retired; point it at nothing
    "PROVISIONING_CORTEX_BUDGET_SEC": "2",
    "MCP_TURN_WALL_S": "240",
    "MCP_PROVISIONING_WAIT_SEC": "20",
    "FAULTLINE_MIGRATIONS_DIR": "./migrations",
    # re_embedder preflight flag default ON; explicit for the growth-defer leg
    "REEMBED_LLM_BRAIN_PREFLIGHT": "true",
}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _StubBrain:
    """An OpenAI-shaped chat endpoint whose latency is a dial. NOT a real brain (issue Bar:
    stub endpoint with delayed responses). Requests are recorded AT ARRIVAL — a request the
    client abandons at its (short) timeout still sleeps server-side for the full delay, so
    recording after the response write would RACE the assert (measured: the wound turn's
    chat request was invisible until 30s after the turn ended)."""

    delay_s = 0.2
    requests: list[dict] = []

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):                      # noqa: N802 — http.server API
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                delay = outer.delay_s
                outer.requests.append({"path": self.path, "delay_s": delay,
                                       "arrived_s": round(time.monotonic(), 1),
                                       "body_head": body[:120].decode("utf-8", "replace")})
                t0 = time.monotonic()
                time.sleep(delay)
                content = json.dumps({"atoms": []})   # reframe parses [] → fail-open to raw text
                payload = {"choices": [{"message": {"content": content}}]}
                try:
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(payload).encode())
                except Exception:
                    pass                            # client timed out and hung up — expected

            def log_message(self, *a):              # keep the test output readable
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def chats(self):
        return [r for r in self.requests if "chat/completions" in r["path"]]

    def stop(self):
        self.httpd.shutdown()


@pytest.fixture(scope="module")
def e2e():
    dsn = os.environ.get("POSTGRES_DSN", "")
    if not dsn or not dsn.rsplit("/", 1)[-1].endswith("_test"):
        pytest.skip("E2E needs a throwaway *_test POSTGRES_DSN (skipped — disclosed)")

    import psycopg2
    from src.provisioning.schema_manager import create_user_schema, derive_user_slug_from_uuid

    # ── the world: one account, one seat, one (stub) brain ────────────────────────────
    account = str(uuid.uuid4())
    seat = str(uuid.uuid4())
    slug = derive_user_slug_from_uuid(seat)
    conn = psycopg2.connect(dsn)
    schema, status = create_user_schema(seat, slug, conn)
    assert status == "ready", status
    stub = _StubBrain()
    stub.start()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO public.accounts (account_id, name, slug, account_kind) "
                    "VALUES (%s,'posture-e2e',%s,'tenant')", (account, "e2e-" + account[:8]))
        cur.execute("INSERT INTO public.account_users (account_id, user_id) VALUES (%s,%s)",
                     (account, seat))
        cur.execute("INSERT INTO public.users (user_id, email, display_name, slug) VALUES "
                    "(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                     (seat, f"{slug}@example.invalid", slug, slug))
        cur.execute("INSERT INTO public.user_provisioning (user_id, schema_name, status, "
                    "ready_at) VALUES (%s,%s,'ready',now()) ON CONFLICT DO NOTHING",
                     (seat, schema))
        # The seat's brain IS the stub (keyless endpoint; loopback allowed by the operator
        # egress hatch). backend_type openai = OpenAI-shaped payloads, which the stub speaks.
        cur.execute("INSERT INTO public.account_llm_config (account_id, backend_type, "
                    "base_url, model) VALUES (%s,'openai',%s,'stub-slow-brain')",
                     (account, stub.url))
    conn.commit()
    conn.close()

    # ── env must be in place BEFORE src.api.main / src.mcp.server import ───────────────
    saved_env = {k: os.environ.get(k) for k in E2E_FLAGS}
    os.environ.update(E2E_FLAGS)
    import src.api.main as M                       # noqa: F401 — imports under the flags
    api_port = _free_port()
    os.environ["FAULTLINE_API_URL"] = f"http://127.0.0.1:{api_port}"
    import src.mcp.server as S
    import saas.brain_posture as bp

    # ⚠️ FAULTLINE_API_URL is an IMPORT-TIME constant in src.mcp/server.py — if any earlier
    # import in this pytest process already loaded the module, the env write above is dead.
    # Set the LIVE module attr too (and mark the URL probe done) so the front door points at
    # OUR backend regardless of import order.
    S.FAULTLINE_API_URL = f"http://127.0.0.1:{api_port}"
    S._FAULTLINE_URL_DETECTED = True

    # Env AND module constants (the #63 discipline): ANY module in this pytest process may
    # already have imported src.api.main / saas.brain_posture before the env update above
    # (conftest's fixtures touch the mcp/http_server import chain), and these flags are read
    # at IMPORT time. Set them on the LIVE modules so the E2E's route and posture window are
    # in force regardless of import order — and ASSERT the spine route is armed, so a silent
    # legacy-route harvest (no REFRAME, no brain, no wound) fails loudly instead of
    # pretending to pass.
    saved_flags = (M.SENTENCE_PIPELINE, getattr(M, "SPINE_DETERMINISTIC_SEGMENTATION", None),
                   bp.MIN_SAMPLES, bp.COOLDOWN_S)
    M.SENTENCE_PIPELINE = True
    M.SPINE_DETERMINISTIC_SEGMENTATION = False
    assert M.SENTENCE_PIPELINE is True
    bp.MIN_SAMPLES = int(E2E_FLAGS["BRAIN_POSTURE_MIN_SAMPLES"])
    bp.COOLDOWN_S = float(E2E_FLAGS["BRAIN_POSTURE_COOLDOWN_S"])

    # Loudness capture shared with the backend thread (module attr, process-wide).
    events: list[tuple] = []
    real_log_event = bp._log_event

    def _recording_log_event(event, level="info", **kw):
        events.append((event, dict(kw)))
        return real_log_event(event, level, **kw)

    bp._log_event = _recording_log_event
    bp.reset_for_tests()

    # ── the backend, on a real socket, in a thread ────────────────────────────────────
    import uvicorn
    config = uvicorn.Config(M.app, host="127.0.0.1", port=api_port, log_level="warning")
    server = uvicorn.Server(config)
    uv_thread = threading.Thread(target=server.run, daemon=True, name="e2e-backend")
    uv_thread.start()

    import httpx
    t0 = time.monotonic()
    up = False
    while time.monotonic() - t0 < 90:
        try:
            if httpx.get(f"http://127.0.0.1:{api_port}/docs", timeout=2).status_code == 200:
                up = True
                break
        except Exception:
            time.sleep(0.5)
    assert up, "backend did not come up on its socket"

    # Warm EVERY model the measured turns will touch, OUTSIDE any MCP brain budget: GLiNER
    # (classify ×2 — the second call also pays the NER/model follow-on loads measured to
    # eat a 10s brain budget on a cold second call) and the spine's own harvest path (one
    # direct /harvest-spans whose edges are DISCARDED — no DB write, all spaCy/GLiNER/deriver
    # state resident). Cold loads inside a measured turn would masquerade as brain latency.
    t0 = time.monotonic()
    for txt in ("hello there", "and again"):
        try:
            httpx.post(f"http://127.0.0.1:{api_port}/classify-intent",
                       json={"text": txt, "user_id": seat}, timeout=300)
        except Exception:
            pass
    try:
        httpx.post(f"http://127.0.0.1:{api_port}/harvest-spans",
                   json={"text": "My cousin's name is Vera.", "user_id": seat}, timeout=300)
    except Exception:
        pass
    timings_warmup = round(time.monotonic() - t0, 1)
    # The warmup harvest is an HONEST observation too (its reframe answered ok in ~0.25s) —
    # clear the ring AFTER it so the tighten quorum is decided by the WOUND evidence alone:
    # deterministic phasing, no luck about which sample lands second.
    bp.reset_for_tests()

    yield {"stub": stub, "seat": seat, "schema": schema, "account": account,
           "S": S, "M": M, "bp": bp, "events": events, "dsn": dsn,
           "gliner_warmup_s": timings_warmup}

    # ── teardown: restore env, stop servers, drop the world ───────────────────────────
    bp._log_event = real_log_event
    server.should_exit = True
    uv_thread.join(timeout=15)
    stub.stop()
    M.SENTENCE_PIPELINE, _spine_seg, bp.MIN_SAMPLES, bp.COOLDOWN_S = saved_flags
    if _spine_seg is not None:
        M.SPINE_DETERMINISTIC_SEGMENTATION = _spine_seg
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        cur.execute(f'DROP SCHEMA IF EXISTS "flagent_{seat.replace("-", "_")}" CASCADE')
        cur.execute("DELETE FROM public.account_llm_config WHERE account_id = %s", (account,))
        cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (seat,))
        cur.execute("DELETE FROM public.users WHERE user_id = %s", (seat,))
        cur.execute("DELETE FROM public.account_users WHERE account_id = %s", (account,))
        cur.execute("DELETE FROM public.accounts WHERE account_id = %s", (account,))
    conn.close()
    bp.reset_for_tests()


def _mcp_turn(e2e, text):
    """One REAL remember_facts turn through the in-process MCP tool."""
    import asyncio
    S = e2e["S"]
    t0 = time.monotonic()
    out = asyncio.run(S.remember_facts_tool(text, e2e["seat"]))
    return round(time.monotonic() - t0, 2), out


def _sql(e2e, sql, params=()):
    import psycopg2
    conn = psycopg2.connect(e2e["dsn"])
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {e2e['schema']}")
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []
    finally:
        conn.commit()
        conn.close()


def _bound_cfg(e2e):
    from saas import llm_context as lx, seat_brain as sb
    cfg = sb.resolve_brain(e2e["seat"], e2e["account"])
    assert cfg and not cfg.get("no_brain"), f"seat brain did not resolve: {cfg}"
    return cfg


# ── the E2E itself ─────────────────────────────────────────────────────────────────────

def test_posture_saves_the_turn_from_a_slow_brain(e2e):
    bp, stub = e2e["bp"], e2e["stub"]
    from saas import brain_health as bh
    from src.api.llm_calls import LLMTimeouts
    from src.re_embedder import embedder as E
    timings = {"gliner_warmup_s": e2e["gliner_warmup_s"]}
    cfg = _bound_cfg(e2e)

    # ── 1. WARM: stub fast; pays residual loads and proves the healthy front door ─────
    stub.delay_s = 0.2
    t_warm, warm_out = _mcp_turn(e2e, "My aunt's name is Ruth.")
    timings["warm_turn_s"] = t_warm
    assert warm_out.get("status") == "valid" and warm_out.get("committed", 0) >= 1, warm_out
    assert _sql(e2e, "SELECT 1 FROM entity_aliases WHERE alias = 'ruth' LIMIT 1"), \
        "the healthy turn's fact did not land"

    # ── 2. WOUND: the brain degrades — 200s only after 30s (the NVIDIA shape) ─────────
    # TWO degraded turns: each failed REFRAME feeds the posture ONE at-cutoff sample
    # (ReadTimeout is not retried in this lane — total_attempts=1, measured); quorum is 2.
    stub.delay_s = 30.0
    t_w1, w1 = _mcp_turn(e2e, "My mother's name is Diane.")
    t_w2, w2 = _mcp_turn(e2e, "My grandmother's name is Elsie.")
    timings["wound1_turn_s"] = t_w1
    timings["wound2_turn_s"] = t_w2
    for out in (w1, w2):
        # the standing brain-not-transport doctrine: a degraded harvest is WITHHELD, the
        # turn retained verbatim for re-extraction — loud, never a silent commit, never lost
        assert out.get("extraction_degraded") is True, out
        assert out.get("retained_for_reextraction") is True, out
    assert "REFRAME" in (w1.get("llm_unanswered_operations") or {}), w1
    # the wound waited the STATIC budget (the notice carries the value the call ran with)
    assert "the budget for this step is 6s" in str(w1.get("notice", "")), w1.get("notice")
    assert t_w1 >= float(E2E_FLAGS["LLM_TIMEOUT_REFRAME"]), timings
    retained = _sql(e2e, "SELECT count(*) FROM episodic_log WHERE raw_text LIKE %s",
                    ("%name is%",))
    assert retained and retained[0][0] >= 2, retained     # both wound turns retained
    # the stub was REALLY consulted (chat requests, recorded at arrival)
    assert len(stub.chats()) >= 2, [r["path"] for r in stub.requests]

    # ── 3. TIGHTEN fired on the second sample: budget capped, growth deferred, loud ────
    with _bind(cfg):
        effective = LLMTimeouts.get("REFRAME")
        static = LLMTimeouts.static("REFRAME")
    assert effective == bp.FLOOR_S and effective < static, (effective, static)
    timings["effective_reframe_s"] = effective
    timings["static_reframe_s"] = static
    assert [e[0] for e in e2e["events"]].count("brain_posture.tighten") == 1
    rem = bp.growth_deferred(cfg)
    assert rem and 0 < rem <= bp.COOLDOWN_S, rem
    with _bind(cfg):        # the preflight reads the BOUND brain — bind like the sweep does
        assert E._reembedder_llm_lane_open(
            e2e["seat"], "ontology_eval", e2e["schema"]) is False

    # ── 4. TIGHT turn: gives up in FLOOR-time — the wound's wait-sum, bounded ──────────
    _chats_before = len(stub.chats())
    t_tight, tight_out = _mcp_turn(e2e, "My sister's name is Anne.")
    timings["tight_turn_s"] = t_tight
    assert tight_out.get("extraction_degraded") is True, tight_out
    assert len(stub.chats()) > _chats_before, "the tight turn never asked the brain"
    # THE timing evidence, at the source: the degrade NOTICE carries the wait + the budget
    # the call actually ran with — the tight turn waited the FLOOR, not the static budget.
    # (Turn-vs-turn wall deltas are reported but not asserted: a ~10s classify retry hiccup
    # can land on either turn and swamp a hard delta — measured on both runs so far.)
    _notice = str(tight_out.get("notice", ""))
    assert "the budget for this step is 4s" in _notice, _notice
    assert "waited 4." in _notice, _notice
    timings["posture_events"] = [e[0] for e in e2e["events"]]

    # POSTURE lane row on the seat's alarm stream. NOTE: flush_now() drains the COUNTERS
    # only — the last-write-wins probe ROWS ride _flush_last_events() on the async cadence
    # (measured: the recovery row sat in _last_events with the DB still showing 'fail').
    bh._flush_last_events()
    rows = _sql(e2e, "SELECT lane, outcome FROM brain_probe_state WHERE lane = 'POSTURE'")
    assert rows and rows[0][1] == "fail", rows

    # ── 5. RECOVER: cooldown expires; static budgets; growth resumes; fact lands ──────
    stub.delay_s = 0.2
    time.sleep(bp.COOLDOWN_S + 2.0)
    with _bind(cfg):
        assert LLMTimeouts.get("REFRAME") == LLMTimeouts.static("REFRAME")
    assert bp.growth_deferred(cfg) is None
    with _bind(cfg):        # recovered brain: the growth seam is OPEN again
        assert E._reembedder_llm_lane_open(
            e2e["seat"], "ontology_eval", e2e["schema"]) is True
    t_fast, fast_out = _mcp_turn(e2e, "My brother's name is Peter.")
    timings["recovered_turn_s"] = t_fast
    assert fast_out.get("status") == "valid", fast_out
    assert _sql(e2e, "SELECT 1 FROM entity_aliases WHERE alias = 'peter' LIMIT 1"), \
        "the post-recovery turn's fact did not land"
    bh._flush_last_events()
    rows = _sql(e2e, "SELECT lane, outcome FROM brain_probe_state WHERE lane = 'POSTURE'")
    assert rows and rows[0][1] == "ok", rows            # last-write-wins: recovery cleared it
    assert "brain_posture.recover" in [e[0] for e in e2e["events"]]

    # ── evidence: the run's own numbers, written by the run ────────────────────────────
    timings["stub_chat_requests"] = len(stub.chats())
    timings["facts_committed"] = len(_sql(
        e2e, "SELECT 1 FROM facts WHERE superseded_at IS NULL"))
    timings["episodic_retained"] = retained[0][0]
    art = os.path.join("DEV", "review", "gauntlet", "brain-posture-229", "round_1")
    os.makedirs(art, exist_ok=True)
    with open(os.path.join(art, "e2e_timings.json"), "w") as f:
        json.dump(timings, f, indent=2, default=str)
    with open(os.path.join(art, "e2e_stub_requests.json"), "w") as f:
        json.dump(stub.requests, f, indent=2, default=str)
    print("\n=== POSTURE E2E TIMINGS ===", json.dumps(timings, indent=2, default=str))


class _bind:
    """Bind cfg for LLMTimeouts.get reads (the ContextVar a live request would hold)."""

    def __init__(self, cfg):
        from saas import llm_context as lx
        self._lx = lx
        self._cfg = cfg

    def __enter__(self):
        self._tok = self._lx.bind_explicit_llm_config(self._cfg)
        return self

    def __exit__(self, *a):
        self._lx.reset_llm_config(self._tok)
