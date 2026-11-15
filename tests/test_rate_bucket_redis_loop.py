"""The WHOLE rate-limit learning loop, over a REAL Redis with a REAL Lua engine.

Why this file exists: `REDIS_COORD_RATE_BUDGET` was enabled on pre-prod but INERT in the
ways that matter — the limiter learned a rate/pause in-process only, so the process that
saw the 429 was the only one paced by it, and the Brain Health bar could not show the
number because the Redis hash never carried it. The loop is only closed when EVERY link
holds:

  take → hash carries tokens/ts/rate/cap ─→ a second reader sees the pacing rate
  429 (headers)  → floor + pause PUBLISHED at learn time → hash reflects them
  429 (no header)→ table/backoff assumption lands in the SAME Redis-truth path
  any taker      → Lua CLAMPS to the shared floor, REFUSES during a shared pause
  bar            → ceiling_rpm/ceiling_source/measured/paused read from that truth

And the ISOLATION LAW, which outranks everything: the bucket is per BRAIN (owner+endpoint
+model+key-fingerprint digest). A 429 taught to brain A must leave brain B — same provider
host, different key — at full speed.

Redis-gated by necessity (the Lua scripts ARE the seam under test — a mock that
short-circuits them is not coverage). Run with a scratch Redis:
    docker run --rm -p 55433:6379 redis:7-alpine
    FLTEST_REDIS_URL=redis://127.0.0.1:55433/0 python3 tools/fltest.py --bug RATEBUCKET \\
        --test tests/test_rate_bucket_redis_loop.py
"""

import os
import threading
import time

import pytest

from src.api import llm_calls as LC
from src.api import redis_coord as RC

REDIS_URL = os.environ.get("FLTEST_REDIS_URL", "")

pytestmark = pytest.mark.skipif(
    not REDIS_URL,
    reason="FLTEST_REDIS_URL not set — this suite exercises the real Lua take/publish seam",
)

_RAN = {"live": False}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("REDIS_COORD", "true")
    monkeypatch.setenv("REDIS_COORD_RATE_BUDGET", "true")
    monkeypatch.setenv("REDIS_COORD_SOCKET_TIMEOUT", "2")
    RC.reset_client()
    if RC.client() is None:
        pytest.skip("scratch Redis not reachable")
    _RAN["live"] = True
    yield
    RC.reset_client()


def _brain_cfg(tag, key=None, base="https://api.cerebras.ai/v1", model="gpt-oss-120b",
               owner=None):
    return {
        "account_id": owner or f"acct-{tag}",
        "brain_scope": "account",
        "base_url": base,
        "model": model,
        "api_key": key or f"sk-key-{tag}",
    }


def _scope(tag="A", **kw):
    s = RC.scope_for_brain(_brain_cfg(tag, **kw))
    assert s is not None, "the test cfg must resolve a real brain scope"
    return s


def _purged(scope):
    RC.purge_scope(scope)
    return scope


# ══════════════════════════════════════════════════════════════════════════════
# THE BAR, TEST BY TEST
# ══════════════════════════════════════════════════════════════════════════════

def test_one_take_through_the_gate_leaves_the_full_hash_in_redis():
    """Bar #1: one call through the acquire gate → tokens/ts/rate/cap ALL set."""
    s = _purged(_scope("hash"))
    got = RC.take_token_state(s, 2.0, 2.0)
    assert got is not None and got["wait_s"] == 0.0 and got["paused"] is False
    k = RC.key("llmrate", s, "bucket")
    raw = RC.client().hgetall(k)
    for field in ("tokens", "ts", "rate", "cap"):
        assert raw.get(field) is not None, f"hash field {field!r} missing after a take"
    assert float(raw["rate"]) == pytest.approx(2.0)
    assert float(raw["cap"]) == pytest.approx(2.0)


def test_a_second_reader_sees_nonnone_state_at_the_pacing_rate():
    """Bar #2: brain_bucket_state immediately after → non-None, rate == pacing rate."""
    s = _purged(_scope("read"))
    assert RC.take_token_state(s, 3.0, 1.0)["wait_s"] == 0.0
    st = RC.brain_bucket_state(s)
    assert st is not None, "a second process must see the bucket, not None"
    assert st["rate_per_sec"] == pytest.approx(3.0)
    assert st["capacity"] == pytest.approx(1.0)
    assert 0.0 <= st["saturation"] <= 1.0
    # refill drift tolerance: reading later can only have ADDED tokens
    assert st["tokens"] <= st["capacity"] + 1e-6


def test_a_hard_pause_is_shared_and_spends_no_token():
    """Bar #3: 429 with remaining=0 → the hash reflects the pause; a DIFFERENT process
    (a fresh take) is throttled by it and no token is burned while paused."""
    s = _purged(_scope("pause"))
    assert RC.take_token_state(s, 50.0, 50.0)["wait_s"] == 0.0   # full-speed brain
    k = RC.key("llmrate", s, "bucket")
    tokens_before = float(RC.client().hget(k, "tokens"))

    assert RC.publish_brain_pause(s, 2.0) is True
    # …and the hash TTL must OUTLIVE the pause (an idle brain cannot forget mid-pause).
    # Teeth: the plain take TTL for this shape is exactly 60s; the pause stretch makes it
    # ceil(2)+60 = 62 — so >= 61 distinguishes the stretch from the base TTL.
    assert RC.client().ttl(k) >= 61, "hash must live past the pause it carries"

    st = RC.take_token_state(s, 50.0, 50.0)                        # ← the second process
    assert st is not None and st["paused"] is True
    assert 0.0 < st["wait_s"] <= 2.0
    assert float(RC.client().hget(k, "tokens")) == pytest.approx(tokens_before), \
        "a paused bucket must not spend tokens"
    assert (RC.brain_pause_remaining(s) or 0.0) > 0.0
    assert RC.brain_bucket_state(s)["pause_remaining_s"] > 0.0
    # the available-tokens probe AGREES with the pacer: paused → zero spendable capacity
    assert RC.brain_available_tokens(s) == 0.0

    # max()-extend semantics: a SECOND exhausted-window report cannot cut the pause short
    RC.publish_brain_pause(s, 0.2)
    assert (RC.brain_pause_remaining(s) or 0.0) > 1.5, \
        "a shorter later report must never shorten an earlier pause"

    time.sleep(2.1)                                                # window resets
    st2 = RC.take_token_state(s, 50.0, 50.0)
    assert st2 is not None and st2["paused"] is False, "pause must lift at reset"


def test_a_long_pause_survives_a_take_refreshing_the_hash_ttl():
    """A take mid-pause refreshes the hash TTL — it must not SHORTEN it below the pause
    it enforces (expire-to-60 on a 120s pause would evaporate the hash mid-pause and the
    brain would forget it was paused). No sleep needed: the TTL read is the whole proof."""
    s = _purged(_scope("longpause"))
    RC.publish_brain_pause(s, 120.0)
    st = RC.take_token_state(s, 50.0, 50.0)
    assert st is not None and st["paused"] is True
    ttl = RC.client().ttl(RC.key("llmrate", s, "bucket"))
    assert ttl > 110, f"take shortened the hash TTL to {ttl}s under a 120s pause"


def test_a_learned_floor_clamps_every_takers_rate():
    """Bar #4/#5: the floor ANY process learned binds the REFILL RATE in Redis for every
    process — even one arriving with a much higher configured rate."""
    s = _purged(_scope("floor"))
    assert RC.publish_brain_floor(s, 0.5, 2, "measured") is True
    got = RC.take_token_state(s, 10.0, 10.0)     # a fresh process, configured fast
    assert got["wait_s"] == 0.0                  # the clamped burst (1 token) grants once
    st = RC.brain_bucket_state(s)
    assert st["rate_per_sec"] == pytest.approx(0.5), "the floor must clamp the hash rate"
    assert st["capacity"] == pytest.approx(1.0), "burst must shrink with the floor"
    got2 = RC.take_token_state(s, 10.0, 10.0)    # second take → paced by the FLOOR
    assert got2["wait_s"] > 1.5, "wait must come from 1/0.5=2s, not the caller's 10 rps"
    assert RC.brain_rate_per_sec(s) == pytest.approx(0.5)


def test_floor_precedence_survives_a_cross_process_race():
    """MEASURED > TABLE > BACKOFF, enforced ATOMICALLY in Redis: an assumption arriving
    after (or racing with) a measurement must be refused, not overwrite it."""
    s = _purged(_scope("rank"))
    assert RC.publish_brain_floor(s, 0.5, 2, "measured") is True
    assert RC.publish_brain_floor(s, 0.1, 1, "assumed_table") is False, \
        "an assumption may never overwrite a measurement"
    st = RC.brain_floor_state(s)
    assert st["rank"] == 2 and st["provenance"] == "measured"
    assert st["floor_rps"] == pytest.approx(0.5)
    # same rank may tighten (a repeated 429 tightens an assumption)
    assert RC.publish_brain_floor(s, 0.4, 2, "measured") is True
    assert RC.brain_floor_state(s)["floor_rps"] == pytest.approx(0.4)
    # a CLEAR is unconditional — same semantics as _TokenBucket.set_learner_floor
    assert RC.publish_brain_floor(s, 0.0, 0, "") is True
    assert RC.brain_floor_state(s) is None


def test_isolation_a_429_taught_to_brain_a_leaves_brain_b_at_full_speed():
    """THE ISOLATION LAW. Two brains on the SAME provider host, different keys: A gets
    pause + tight floor; B's hash, rate and pace stay completely untouched."""
    a = _purged(_scope("isoA"))
    b = _purged(_scope("isoB"))
    assert a != b, "different owner+key on the same host MUST be different digests"

    # both take at full speed first
    assert RC.take_token_state(a, 20.0, 20.0)["wait_s"] == 0.0
    assert RC.take_token_state(b, 20.0, 20.0)["wait_s"] == 0.0

    # a 429 teaches brain A: hard pause + a tight 20 rpm floor, both visible in ITS hash
    assert RC.publish_brain_pause(a, 3.0) is True
    assert RC.publish_brain_floor(a, 20.0 / 60.0, 2, "measured") is True

    # brain B is untouched: not paused, still full speed
    st_b = RC.take_token_state(b, 20.0, 20.0)
    assert st_b["paused"] is False and st_b["wait_s"] == 0.0
    snap_b = RC.brain_bucket_state(b)
    assert snap_b["pause_remaining_s"] == 0.0
    assert snap_b["rate_per_sec"] == pytest.approx(20.0), "B's rate must stay unclamped"
    assert RC.brain_floor_state(b) is None

    # brain A IS throttled by what it learned
    st_a = RC.take_token_state(a, 20.0, 20.0)
    assert st_a["paused"] is True
    time.sleep(3.1)   # let A's pause lift; the floor still binds A but never B
    st_a2 = RC.take_token_state(a, 20.0, 20.0)
    assert st_a2["paused"] is False
    assert RC.brain_bucket_state(a)["rate_per_sec"] == pytest.approx(20.0 / 60.0)
    assert RC.brain_bucket_state(b)["rate_per_sec"] == pytest.approx(20.0)


def test_concurrent_takers_never_exceed_rate_window_plus_burst():
    """The race the critic will hunt: N threads taking concurrently must never exceed
    rate×window + burst. The Lua refill+take is the only thing standing between us and a
    provider ban."""
    s = _purged(_scope("race"))
    rate, cap = 3.0, 1.0
    grants = {"n": 0}
    stop_at = time.monotonic() + 1.0
    lock = threading.Lock()

    def hammer():
        while time.monotonic() < stop_at:
            st = RC.try_take_token(s, rate, cap)
            if st == 0.0:
                with lock:
                    grants["n"] += 1
            time.sleep(0.005)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # window 1.0s × 3/s + burst 1 + slack for the boundary refill
    assert grants["n"] <= int(rate * 1.0 + cap + 2), \
        f"{grants['n']} grants in 1s exceeds rate×window+burst"
    assert grants["n"] >= int(rate), "refill must actually have minted tokens"


def test_a_reader_arriving_mid_lua_never_sees_a_torn_hash():
    """Lua scripts run atomically in Redis, so a concurrent reader sees either the pre-
    or the post-state — never tokens from one take and ts from another."""
    s = _purged(_scope("torn"))
    RC.take_token_state(s, 5.0, 2.0)
    stop_at = time.monotonic() + 0.6
    errors: list[str] = []

    def writer():
        while time.monotonic() < stop_at:
            RC.take_token_state(s, 5.0, 2.0)
            time.sleep(0.002)

    t = threading.Thread(target=writer)
    t.start()
    reads = 0
    while time.monotonic() < stop_at:
        st = RC.brain_bucket_state(s)
        if st is not None:
            reads += 1
            if not (st["tokens"] <= st["capacity"] + 1e-6):
                errors.append(f"tokens {st['tokens']} > cap {st['capacity']}")
            if not (0.0 <= st["saturation"] <= 1.0):
                errors.append(f"saturation {st['saturation']} out of [0,1]")
            if st["rate_per_sec"] <= 0 or st["pause_remaining_s"] < 0:
                errors.append(f"nonsense fields: {st}")
        time.sleep(0.001)
    t.join()
    assert not errors, errors
    assert reads > 10, "the reader must actually have read mid-flight"


# ══════════════════════════════════════════════════════════════════════════════
# THE PACER (llm_calls._TokenBucket) AGAINST THE SHARED STATE
# ══════════════════════════════════════════════════════════════════════════════

def _bucket(rate, scope):
    b = LC._TokenBucket(rate, LC._rate_burst_capacity(rate))
    b.coord_scope = scope
    return b


def test_process_a_pause_throttles_process_bs_bucket():
    """The in-process pacer respects the SHARED pause: bucket B (a different process,
    same brain) is throttled because the take script refuses while paused — and ADOPTS
    the pause locally so its wait cap extends exactly as if it saw the 429."""
    s = _purged(_scope("cross"))
    a, b = _bucket(50.0, s), _bucket(50.0, s)
    a.pause_for(1.2)                                   # process A saw the 429
    assert (RC.brain_pause_remaining(s) or 0.0) > 0.0  # …and published it
    ok, wait = b._try_acquire()                        # process B tries immediately
    assert ok is False, "B must be throttled by A's pause"
    assert wait > 0.0
    assert b.pause_until > time.monotonic(), "B must ADOPT the pause locally"
    time.sleep(1.35)
    ok2, _ = b._try_acquire()
    assert ok2 is True, "B proceeds once the window resets"


def test_process_a_floor_paces_process_bs_bucket():
    """The learned ceiling binds a second process's bucket through Redis alone: B's own
    rate is 50 rps, but its takes are clamped to A's learned floor — B stays refused for
    far longer than its own rate could explain (1/50s), because the shared bucket refills
    at the floor."""
    s = _purged(_scope("crossfloor"))
    a, b = _bucket(50.0, s), _bucket(50.0, s)
    assert a.set_learner_floor(0.5, "https://api.cerebras.ai/v1",
                               provenance=LC.FLOOR_MEASURED) is True
    assert (RC.brain_floor_state(s) or {}).get("floor_rps") == pytest.approx(0.5)
    assert b._try_acquire()[0] is True          # the clamped burst token
    deadline = time.monotonic() + 0.4           # at B's own 50 rps a token mints in 20ms
    while time.monotonic() < deadline:
        ok, _ = b._try_acquire()
        assert ok is False, "B must still be paced by A's 0.5 rps floor after 0.4s"
        time.sleep(0.02)
    assert RC.brain_bucket_state(s)["rate_per_sec"] == pytest.approx(0.5), \
        "the hash must show the FLOOR as the pace every process is refilled at"


def test_the_real_observer_publishes_pause_and_floor_from_a_429():
    """Integration through the REAL `_observe_rate_headers`: a 429 whose headers say the
    budget is exhausted must publish the pause into the shared hash — and a no-header 429
    must land its table/backoff assumption in the SAME Redis-truth path (bucket hash +
    assumption key), not just the in-process bucket."""
    s = _purged(_scope("obs"))
    # The pacer derives the coord scope FROM the rate key — use the REAL digest so the
    # publishes land under exactly the scope the console would read.
    key = f"brain:{s.ident}"

    class _Resp:
        status_code = 429
        headers = {
            "x-ratelimit-limit-requests": "10",
            "x-ratelimit-remaining-requests": "0",
            "x-ratelimit-reset-requests": "1.5s",
        }
        text = ""

    mp = pytest.MonkeyPatch()
    mp.setenv("LLM_RATE_LIMIT_ENABLED", "1")
    mp.setenv("LLM_RATE_HEADER_LEARNING", "1")
    mp.setattr(LC, "_resolve_rate_limit_key", lambda: key)
    mp.setattr(LC, "_resolve_brain_scope", lambda: s)
    mp.setattr(LC, "_resolve_endpoint_and_model",
               lambda: ("https://api.cerebras.ai/v1", "gpt-oss-120b"))
    try:
        with LC._rate_buckets_lock:
            LC._rate_buckets.pop(key, None)
        bucket = LC._get_rate_bucket(key, 50.0,
                                     {"source": "api.cerebras.ai", "known": True},
                                     "https://api.cerebras.ai/v1")
        assert bucket.coord_scope == s, "the brain: key must map onto the test scope"
        LC._observe_rate_headers(_Resp(), "EXTRACTION")
        # the PAUSE is in Redis (any process throttled), not just in-process
        assert (RC.brain_pause_remaining(s) or 0.0) > 0.0

        # …and the no-header 429: the fallback assumption lands in the SAME path
        class _Bare429:
            status_code = 429
            headers = {}
            text = ""

        with LC._rate_buckets_lock:
            LC._rate_buckets.pop(key, None)
        LC._get_rate_bucket(key, 50.0, {"source": "api.cerebras.ai", "known": True},
                            "https://api.cerebras.ai/v1")
        LC._observe_rate_headers(_Bare429(), "EXTRACTION")
        fl = RC.brain_floor_state(s)
        assert fl is not None and fl["provenance"] in ("assumed_table", "assumed_backoff"), \
            "the assumed ceiling must land in the bucket hash, not only the assumption key"
        assumed = RC.read_rate_assumption(s)
        assert assumed is not None and assumed.get("state") == "assumed"
        assert assumed.get("disclaimer"), "the disclaimer must ship with the assumption"
    finally:
        mp.undo()
        with LC._rate_buckets_lock:
            LC._rate_buckets.pop(key, None)


# ══════════════════════════════════════════════════════════════════════════════
# THE BAR (Tenant Admin) — payload + isolation audit
# ══════════════════════════════════════════════════════════════════════════════

def test_the_brain_health_payload_is_driven_from_redis_truth():
    from saas import tenant_console

    s = _purged(_scope("payload"))
    RC.take_token_state(s, 2.0, 2.0)                       # one call through the gate
    thr = tenant_console._brain_meter_payload(s, {"max_rpm": 300}, 24)["throughput"]
    assert thr["measured"] is True
    assert thr["ceiling_source"] == "live"
    assert thr["ceiling_rpm"] == pytest.approx(120.0)
    assert thr["paused"] is False

    # a 429 teaches a tighter MEASURED ceiling → the next poll shows it (live)
    RC.publish_brain_floor(s, 0.5, 2, "measured")
    thr2 = tenant_console._brain_meter_payload(s, {"max_rpm": 300}, 24)["throughput"]
    assert thr2["ceiling_rpm"] == pytest.approx(30.0)
    assert thr2["ceiling_source"] == "live"

    # …and a hard pause renders as the paused state
    RC.publish_brain_pause(s, 4.0)
    thr3 = tenant_console._brain_meter_payload(s, {"max_rpm": 300}, 24)["throughput"]
    assert thr3["paused"] is True
    assert thr3["pause_remaining_s"] is not None and thr3["pause_remaining_s"] > 0


def test_an_assumed_floor_renders_as_assumed_even_without_the_assumption_key():
    """The no-header case: the assumed table figure lands in the SAME Redis-truth path
    and renders as ASSUMED — never as a fake-live measurement, even if the separate
    assumption/disclaimer key has already expired (24h TTL) while the floor persists."""
    from saas import tenant_console

    s = _purged(_scope("assumedpayload"))
    RC.take_token_state(s, 2.0, 2.0)
    RC.publish_brain_floor(s, 0.25, 1, "assumed_table")
    # NOTE: deliberately NO publish_rate_assumption here — the floor alone must not
    # masquerade as a measurement.
    thr = tenant_console._brain_meter_payload(s, {"max_rpm": 300}, 24)["throughput"]
    assert thr["ceiling_source"] == "assumed"
    assert thr["ceiling_rpm"] == pytest.approx(15.0)
    assert thr["paused"] is False


def test_the_digest_is_audited_key_by_key():
    """The isolation audit: every identity input that MUST discriminate a brain does;
    nothing host-only or shared survives."""
    mk = _brain_cfg
    # same everything → same scope (stability: a restart must not re-key the bucket)
    assert RC.scope_for_brain(mk("x", key="sk-1")) == RC.scope_for_brain(mk("x", key="sk-1"))
    # SAME host, DIFFERENT key → distinct (the free-key/paid-key law)
    assert RC.scope_for_brain(mk("x", key="sk-1")) != RC.scope_for_brain(mk("x", key="sk-2"))
    # same host+key, DIFFERENT owner → distinct
    assert RC.scope_for_brain(mk("a", key="sk-1")) != RC.scope_for_brain(mk("b", key="sk-1"))
    # different model on the same host+key+owner → distinct
    assert RC.scope_for_brain(mk("x", key="sk-1")) != \
        RC.scope_for_brain(mk("x", key="sk-1", model="other-model"))
    # a seat's OWN brain isolates from its OWN account's brain on the same endpoint
    seat_cfg = dict(mk("acct-1", key="sk-1"))
    seat_cfg.update(brain_scope="seat", seat_user_id="user-seat-1")
    assert RC.scope_for_brain(seat_cfg) != RC.scope_for_brain(mk("acct-1", key="sk-1"))
    # the digest is NOT the bare host scope — no host-keyed state anywhere
    assert RC.scope_for_brain(mk("x", key="sk-1")) != \
        RC.scope_for_endpoint("https://api.cerebras.ai/v1", "")


# ══════════════════════════════════════════════════════════════════════════════
# FLAG OFF — byte-for-byte legacy
# ══════════════════════════════════════════════════════════════════════════════

def test_flag_off_is_byte_for_byte_the_in_process_bucket(monkeypatch):
    """REDIS_COORD off: no module in this loop may TOUCH Redis, let alone change
    behavior. Proven by mining the seam — client() would explode if reached, because the
    flag short-circuit is the FIRST thing every entry point does."""
    s = _scope("flagoff")

    def _explode(*a, **kw):
        raise AssertionError("the flag-off path must not touch redis_coord's client")

    monkeypatch.setattr(RC, "client", _explode)
    monkeypatch.setenv("REDIS_COORD", "false")

    # module-level probes: no opinion, never an exception, no client reached
    assert RC.take_token_state(s, 1.0, 1.0) is None
    assert RC.try_take_token(s, 1.0, 1.0) is None
    assert RC.brain_bucket_state(s) is None
    assert RC.brain_floor_state(s) is None
    assert RC.brain_pause_remaining(s) is None
    assert RC.publish_brain_floor(s, 1.0, 2, "measured") is False
    assert RC.publish_brain_pause(s, 1.0) is False

    # the pacer: identical in-process behavior with a scope bound but the flag OFF
    b = _bucket(50.0, s)
    assert b._try_acquire() == (True, 0.0)          # local bucket, full → grant
    b.pause_for(0.05)                               # local pause only, no publish
    ok, wait = b._try_acquire()
    assert ok is False and wait > 0.0
    time.sleep(0.06)
    assert b._try_acquire()[0] is True              # local pause lifts locally
    assert b.set_learner_floor(0.5, "https://x", provenance=LC.FLOOR_MEASURED) is True
    assert b.learner_floor_rps == 0.5               # the local floor still binds locally


def test_budget_flag_off_leaves_the_assumption_lane_intact(monkeypatch):
    """REDIS_COORD on but RATE_BUDGET off: the bucket probes return no opinion, while the
    assumption/disclaimer publication (gated on `enabled()` alone, DELIBERATELY) still
    works — the tenant must see the disclaimer whether or not the budget is switched on."""
    s = _purged(_scope("budgetoff"))
    monkeypatch.setenv("REDIS_COORD_RATE_BUDGET", "false")
    assert RC.brain_bucket_state(s) is None
    assert RC.take_token_state(s, 1.0, 1.0) is None
    assert RC.publish_rate_assumption(s, {"state": "assumed", "disclaimer": "d"}) is True
    got = RC.read_rate_assumption(s)
    assert got is not None and got["state"] == "assumed"


def test_redis_down_never_loses_a_live_call_and_never_blocks(monkeypatch):
    """REDIS_COORD on but Redis UNREACHABLE: the pacer degrades to the in-process bucket
    — a call proceeds (never lost), and the bounded socket timeout means it is delayed by
    at most ~1s of failed connects on the FIRST attempt (the client cache then holds None
    for the process lifetime, so subsequent acquires pay nothing)."""
    import time as _t
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6390/0")   # nothing listens here
    RC.reset_client()
    s = _scope("down")
    b = _bucket(50.0, s)
    t0 = _t.monotonic()
    ok, _ = b._try_acquire()
    elapsed = _t.monotonic() - t0
    assert ok is True, "a dead Redis must never lose a live call"
    assert elapsed < 5.0, f"degradation took {elapsed:.1f}s — a slow Redis must not block"
    # second acquire pays nothing (client cached as unavailable)
    t1 = _t.monotonic()
    assert b._try_acquire()[0] is True
    assert _t.monotonic() - t1 < 0.1


def test_the_pacer_level_derivation_keeps_two_brains_on_one_host_apart(monkeypatch):
    """The isolation probe through the REAL in-process chain, not just the scope factory:
    cfg → _resolve_rate_limit_key → _get_rate_bucket → coord_scope → Redis. Two brain
    configs on the SAME provider host with different keys must land on two buckets, two
    Redis keys, and a pause taught to one must leave the other acquiring immediately."""
    cfg_a = _brain_cfg("pacerA")
    cfg_b = _brain_cfg("pacerB")
    monkeypatch.setattr(LC, "_current_llm_config_or_none", lambda: cfg_a)
    key_a = LC._resolve_rate_limit_key()
    bucket_a = LC._get_rate_bucket(key_a, 50.0, {"source": "x", "known": False}, "")
    monkeypatch.setattr(LC, "_current_llm_config_or_none", lambda: cfg_b)
    key_b = LC._resolve_rate_limit_key()
    bucket_b = LC._get_rate_bucket(key_b, 50.0, {"source": "x", "known": False}, "")

    assert key_a != key_b, "two brains on one host must derive two rate keys"
    assert bucket_a is not bucket_b
    assert bucket_a.coord_scope != bucket_b.coord_scope
    assert RC.key("llmrate", bucket_a.coord_scope, "bucket") != \
        RC.key("llmrate", bucket_b.coord_scope, "bucket")

    RC.purge_scope(bucket_a.coord_scope)
    RC.purge_scope(bucket_b.coord_scope)
    bucket_a.pause_for(2.0)                       # a 429 teaches brain A only
    ok, _ = bucket_b._try_acquire()               # brain B acquires immediately
    assert ok is True, "B must be at full speed while A is hard-paused"
    ok_a, _ = bucket_a._try_acquire()
    assert ok_a is False, "A must be throttled by its own learned pause"
    RC.purge_scope(bucket_a.coord_scope)
    RC.purge_scope(bucket_b.coord_scope)
    with LC._rate_buckets_lock:
        LC._rate_buckets.pop(key_a, None)
        LC._rate_buckets.pop(key_b, None)


def test_zzz_the_live_lane_actually_ran():
    """Guard against a silent all-skip: a green run of this file that exercised nothing."""
    assert _RAN["live"], "no live-Redis test executed — this file proved nothing"


# ══════════════════════════════════════════════════════════════════════════════
# THE SHARED CAP CLAMP — capacity is a WINDOW ALLOWANCE, not a function of rate
#
# These exercise the AUTHORITATIVE object. _TokenBucket._try_acquire returns on the
# shared verdict whenever it has one ("when the shared budget has an opinion it is
# AUTHORITATIVE"), so a correctly-sized in-process bucket is irrelevant if the Lua
# collapses cap. It did: `cap = min(cap, max(1, ceil(rate)))` on any bound floor, which
# is 1 for every sub-1-rps floor — i.e. every per-minute provider, under exactly the
# bare-429 condition the local capacity fix was written for.
#
# Testing the local bucket here would pass while the real path stayed broken, which is
# the precise shape of the false greens this file exists to prevent.
# ══════════════════════════════════════════════════════════════════════════════


def test_shared_cap_keeps_the_burst_learned_with_the_floor():
    """floor_cap is the provider's own window count, learned WITH the floor. A bound
    floor must clamp the RATE and leave that burst intact."""
    s = _purged(_scope("capfloor"))
    assert RC.publish_brain_floor(s, 0.0833, 2, "measured", capacity=5.0) is True
    got = RC.take_token_state(s, 4.5, 5.0)
    assert got["wait_s"] == 0.0
    st = RC.brain_bucket_state(s)
    assert st["rate_per_sec"] == pytest.approx(0.0833), "the floor must still clamp the rate"
    assert st["capacity"] == pytest.approx(5.0), (
        "the burst learned WITH the floor must survive — collapsing to ceil(rate)=1 is "
        "the metronome this fix removes")


def test_shared_cap_stays_conservative_with_no_evidence():
    """REGRESSION GUARD, and the reason the allowance is threaded explicitly rather than
    inferred from the caller's capacity: with NO floor_cap and NO allowance we know the
    rate and nothing about the window, so the original clamp must still apply. A caller
    that merely passes a big capacity has asserted nothing."""
    s = _purged(_scope("capnoev"))
    assert RC.publish_brain_floor(s, 0.5, 2, "measured") is True
    RC.take_token_state(s, 10.0, 10.0)
    st = RC.brain_bucket_state(s)
    assert st["capacity"] == pytest.approx(1.0), (
        "no evidence about the window => burst must NOT be widened")


def test_shared_cap_grants_a_real_burst_through_the_authoritative_path():
    """EFFECT level: five consecutive takes must be granted with zero wait under a bound
    per-minute floor. This is the behaviour the metronome destroyed — a conversational
    turn (REFRAME + N EXTRACT) could not proceed as a unit."""
    s = _purged(_scope("capburst"))
    assert RC.publish_brain_floor(s, 0.0833, 2, "measured", capacity=5.0) is True
    waits = [RC.take_token_state(s, 4.5, 5.0)["wait_s"] for _ in range(5)]
    assert waits == [0.0] * 5, f"a 5-per-window allowance must grant 5 instantly, got {waits}"
    sixth = RC.take_token_state(s, 4.5, 5.0)["wait_s"]
    assert sixth > 1.0, (
        f"the SIXTH take must be paced by the floor — burst is restored, the sustained "
        f"rate is NOT widened, got {sixth}")


def test_a_bare_429_backoff_floor_gets_NO_burst():
    """THE HOLE THIS SERIES OPENED AND CLOSED AGAIN. A back-off floor learned from a bare
    429 carries no window knowledge, so there is nothing to justify a burst. An earlier
    revision let a config/registry allowance supply one -- the registry publishes rpm 1000
    for Anthropic and Cerebras, so a 1.5-req/MINUTE back-off still granted 1000 instant
    permits on the AUTHORITATIVE path, inverting the guard into a hole."""
    s = _purged(_scope("backoff"))
    assert RC.publish_brain_floor(s, 0.025, 1, "assumed_backoff") is True
    first = RC.take_token_state(s, 8.0, 1000.0)
    assert first["wait_s"] == 0.0, "the first token is the one the bucket was born with"
    second = RC.take_token_state(s, 8.0, 1000.0)
    assert second["wait_s"] > 30.0, (
        f"a 1.5-req/min back-off must pace the SECOND take, not hand out a burst of 1000; "
        f"got {second['wait_s']}")
    assert RC.brain_bucket_state(s)["capacity"] == pytest.approx(1.0)


def test_floor_cap_can_tighten_but_never_widen_past_the_caller():
    """A floor may only ever TIGHTEN. A stale floor_cap larger than the caller's capacity
    (which already encodes LLM_RATE_BURST and LLM_MAX_RPM) must not raise it."""
    s = _purged(_scope("capbound"))
    assert RC.publish_brain_floor(s, 0.0833, 2, "measured", capacity=50.0) is True
    RC.take_token_state(s, 4.5, 3.0)
    st = RC.brain_bucket_state(s)
    assert st["capacity"] == pytest.approx(3.0), (
        "floor_cap=50 must not widen a caller that asked for 3 — the operator's knobs "
        "live in the caller's capacity and the shared path must not override them")


def test_operator_ceiling_bounds_the_resolved_allowance():
    """_known_window_allowance is 'the TIGHTEST allowance we actually KNOW', so it must
    include the OPERATOR's own ceiling. Omitting it meant a deployment configured to N rpm
    was handed the provider's published rpm as its burst: refill obeyed the operator while
    burst ignored him."""
    import os
    from src.api import llm_calls as LC
    prev = os.environ.get("LLM_MAX_RPM")
    os.environ["LLM_MAX_RPM"] = "60"
    try:
        allow = LC._known_window_allowance({"known": True, "rpm": 1000, "source": "reg"})
        assert allow == pytest.approx(60.0), (
            f"the operator's 60 rpm must bound the registry's 1000, got {allow}")
    finally:
        if prev is None:
            os.environ.pop("LLM_MAX_RPM", None)
        else:
            os.environ["LLM_MAX_RPM"] = prev


def test_a_tighter_floor_with_no_capacity_retires_the_old_burst():
    """floor_cap is only meaningful as the allowance learned WITH the floor it shipped with.
    The bare-429 decay path republishes a TIGHTER rate carrying no capacity, so leaving the
    old floor_cap in place let a burst outlive the floor that justified it — pacing tightened
    on every 429 while the burst never did."""
    s = _purged(_scope("staleburst"))
    assert RC.publish_brain_floor(s, 0.0833, 2, "measured", capacity=5.0) is True
    assert RC.brain_floor_state(s)["floor_cap"] == pytest.approx(5.0)
    # provider tightens; this write carries NO capacity
    assert RC.publish_brain_floor(s, 0.0166, 2, "measured") is True
    st = RC.brain_floor_state(s)
    assert not st.get("floor_cap"), (
        f"a capacity-less floor write must retire the stale burst, got {st.get('floor_cap')}")
    RC.take_token_state(s, 4.5, 5.0)
    assert RC.brain_bucket_state(s)["capacity"] == pytest.approx(1.0), (
        "with the stale burst retired the tighter floor must pace, not burst")


def test_a_take_never_shortens_a_learned_floors_lifetime():
    """publish_brain_floor sets an 86400s TTL so a learned back-off outlives an idle period.
    The take script's expire was unconditional and derived from the caller's capacity/rate
    (typically 60s), so ONE take collapsed the floor's lifetime to the taker's — silently
    discarding a ceiling the system had just learned, and the floor_cap the burst decision
    depends on."""
    s = _purged(_scope("ttlkeep"))
    assert RC.publish_brain_floor(s, 0.0833, 2, "measured", capacity=5.0) is True
    c = RC.client()
    k = RC.key(RC._NS_RATE, s, "bucket")
    ttl_after_publish = c.ttl(k)
    assert ttl_after_publish > 3600, f"floor TTL should be long-lived, got {ttl_after_publish}"
    RC.take_token_state(s, 4.5, 5.0)
    ttl_after_take = c.ttl(k)
    assert ttl_after_take > 3600, (
        f"a take must EXTEND, never shorten, the learned floor's lifetime — "
        f"{ttl_after_publish}s became {ttl_after_take}s")


def test_the_clamp_applies_when_rate_EQUALS_floor_the_state_production_is_always_in():
    """THE ARM EVERY EARLIER TEST MISSED. _get_rate_bucket sets target = min(rate, floor)
    before _shared_budget_wait forwards self.rate, so in the live path rate == floor,
    ALWAYS. The clamp used to be guarded on `rate > floor`, which is false there — so the
    whole block was dead in production while every test drove it with rate > floor and
    passed. Measured end-to-end before the fix: 480 instant permits against a learned
    4.5-req/MINUTE ceiling."""
    s = _purged(_scope("rateeqfloor"))
    assert RC.publish_brain_floor(s, 0.075, 1, "assumed_table") is True
    # rate == floor, and a caller capacity inflated by a pre-floor allowance
    waits = [RC.take_token_state(s, 0.075, 480.0)["wait_s"] for _ in range(3)]
    assert waits[0] == 0.0, "the first token is the one the bucket is born with"
    assert waits[1] > 1.0, (
        f"a bound floor must pace the SECOND take even when rate == floor; got {waits}")
    assert RC.brain_bucket_state(s)["capacity"] == pytest.approx(1.0), (
        "cap must be clamped when a floor is in force, regardless of how rate compares")
