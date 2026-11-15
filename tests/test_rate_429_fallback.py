"""LAYER 3 — the 429 fallback table, driven against the REAL limiter.

Every test here primes an actual ``_TokenBucket`` through ``apply_rate_limit_sync`` and then
feeds a real ``_observe_rate_headers`` call, because the bug this layer fixes was invisible to
a mocked pacer: on production the learner was *installed and running* and had simply never been
handed anything to learn from.

NO DSN, NO REDIS, NO NETWORK — these must never skip. A DSN-gated suite that prints
"0 passed, N skipped" is a false green this project has been burned by repeatedly.
"""

import os
import re

import pytest

from src.api import llm_calls


# ──────────────────────────────────────────────────────────────────────────────
# Harness
# ──────────────────────────────────────────────────────────────────────────────

class FakeResp:
    """Minimal stand-in for an httpx.Response as the observer actually reads it."""

    def __init__(self, status_code=429, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


HOMELAB = "http://198.51.100.20:1234/v1"      # unknown host — no table entry
CEREBRAS = "https://api.cerebras.ai/v1"        # table entry WITH a published rpm (5, Free Trial)
ZAI = "https://api.z.ai/api/paas/v4"           # table entry, rpm=None + non-rate 429 codes

ZAI_BALANCE_429 = (
    '{"error":{"code":"1113","message":"Insufficient balance or no resource package. '
    'Please recharge."}}'
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Deterministic, tenant-free limiter state for every test."""
    llm_calls.reset_rate_limiter()
    monkeypatch.setenv("LLM_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("LLM_RATE_HEADER_LEARNING", "1")
    monkeypatch.setenv("LLM_RATE_429_FALLBACK", "1")
    monkeypatch.setenv("LLM_MAX_RPM", "480")          # 8 rps configured
    monkeypatch.setenv("LLM_RATE_HEADROOM", "0.9")
    monkeypatch.setenv("LLM_RATE_429_DECAY", "0.5")
    monkeypatch.setenv("LLM_RATE_429_MIN_RPM", "1")
    monkeypatch.delenv("WGM_LLM_MODEL", raising=False)
    # SAAS off → no tenant cfg, no brain scope, no Redis publish. The pacer is exercised on
    # its global-env path, which is byte-for-byte the FOSS path.
    monkeypatch.setattr(llm_calls, "_current_llm_config_or_none", lambda: None)
    monkeypatch.setattr(llm_calls, "_resolve_brain_scope", lambda: None)
    yield
    llm_calls.reset_rate_limiter()


def prime(monkeypatch, base_url):
    """Create the live bucket for ``base_url`` exactly as a real pre-flight acquire would."""
    monkeypatch.setenv("LLM_BASE_URL", base_url)
    assert llm_calls.apply_rate_limit_sync("TEST") is True
    key = llm_calls._resolve_rate_limit_key()
    assert key is not None
    return key, llm_calls._rate_buckets[key]


def header_429(remaining=100, reset="60s"):
    """A 429 that DOES carry a recognised request-budget family (OpenAI/Cerebras shape)."""
    return FakeResp(429, {
        "x-ratelimit-limit-requests": "1000",
        "x-ratelimit-remaining-requests": str(remaining),
        "x-ratelimit-reset-requests": reset,
    }, text="rate limit exceeded")


# ──────────────────────────────────────────────────────────────────────────────
# THE BAR
# ──────────────────────────────────────────────────────────────────────────────

def test_a_429_with_headers_learns_from_headers_and_never_consults_the_table(monkeypatch):
    """Bar #1. Cerebras is IN the table at 5 RPM. A header-bearing 429 must ignore that
    entirely and pace off the provider's own numbers."""
    key, bucket = prime(monkeypatch, CEREBRAS)
    llm_calls._observe_rate_headers(header_429(remaining=100, reset="60s"), "TEST")

    assert bucket.floor_provenance == llm_calls.FLOOR_MEASURED
    # remaining/reset * 60 * headroom = 100/60 * 60 * 0.9 = 90 rpm = 1.5 rps
    assert bucket.learner_floor_rps == pytest.approx(1.5, rel=1e-3)
    # The table's figure (5 rpm * 0.9 / 60 = 0.075 rps) must NOT be what we adopted.
    table_rps = (llm_calls._RATE_429_FALLBACK["api.cerebras.ai"]["rpm"] * 0.9) / 60.0
    assert bucket.learner_floor_rps != pytest.approx(table_rps, rel=1e-3)
    assert bucket.floor_note == ""          # no disclaimer: nothing was assumed


def test_a_headerless_429_on_a_table_known_provider_adopts_the_table_and_says_assumed(monkeypatch):
    """Bar #2."""
    key, bucket = prime(monkeypatch, CEREBRAS)
    before = bucket.rate
    llm_calls._observe_rate_headers(FakeResp(429, {}, text=""), "TEST")

    assert bucket.floor_provenance == llm_calls.FLOOR_TABLE
    assert bucket.learner_floor_rps == pytest.approx((5 * 0.9) / 60.0, rel=1e-3)
    assert bucket.rate < before
    # Labelled as an assumption, naming its source and date.
    assert "NOT a measurement" in bucket.floor_note
    assert "inference-docs.cerebras.ai" in bucket.floor_note
    assert "Free Trial" in bucket.floor_note
    assert "2026-08-13" in bucket.floor_note


def test_a_headerless_429_on_an_unknown_provider_still_backs_off(monkeypatch):
    """Bar #3 — the common case. No table entry is not a licence to keep hammering."""
    key, bucket = prime(monkeypatch, HOMELAB)
    before = bucket.rate
    assert llm_calls._fallback_entry_for(HOMELAB) is None

    llm_calls._observe_rate_headers(FakeResp(429, {}, text=""), "TEST")

    assert bucket.floor_provenance == llm_calls.FLOOR_BACKOFF
    assert bucket.rate < before                       # it actually slowed down
    assert bucket.learner_floor_rps == pytest.approx(before * 0.5, rel=1e-3)
    assert "no published limits on file" in bucket.floor_note


def test_a_real_header_replaces_the_assumption_and_flips_the_label_to_measured(monkeypatch):
    """Bar #4."""
    key, bucket = prime(monkeypatch, CEREBRAS)
    llm_calls._observe_rate_headers(FakeResp(429, {}, text=""), "TEST")
    assert bucket.floor_provenance == llm_calls.FLOOR_TABLE
    assumed = bucket.learner_floor_rps

    llm_calls._observe_rate_headers(header_429(remaining=100, reset="60s"), "TEST")

    assert bucket.floor_provenance == llm_calls.FLOOR_MEASURED
    assert bucket.learner_floor_rps == pytest.approx(1.5, rel=1e-3)
    assert bucket.learner_floor_rps != pytest.approx(assumed, rel=1e-3)
    assert bucket.floor_note == ""          # the disclaimer is withdrawn with the assumption


def test_the_assumed_ceiling_never_leaks_outside_its_brain_scope(monkeypatch):
    """Bar #5. One brain 429s; a second brain must be untouched."""
    key_a, bucket_a = prime(monkeypatch, CEREBRAS)
    key_b, bucket_b = prime(monkeypatch, HOMELAB)
    assert key_a != key_b
    b_rate_before, b_floor_before = bucket_b.rate, bucket_b.learner_floor_rps

    # Re-bind to A and take the 429 there.
    monkeypatch.setenv("LLM_BASE_URL", CEREBRAS)
    llm_calls._observe_rate_headers(FakeResp(429, {}, text=""), "TEST")

    assert bucket_a.floor_provenance == llm_calls.FLOOR_TABLE
    assert bucket_b.floor_provenance == llm_calls.FLOOR_NONE
    assert bucket_b.rate == b_rate_before
    assert bucket_b.learner_floor_rps == b_floor_before


# ──────────────────────────────────────────────────────────────────────────────
# PRECEDENCE — the property that must not be invertible
# ──────────────────────────────────────────────────────────────────────────────

def test_an_assumption_can_never_overwrite_a_measurement(monkeypatch):
    key, bucket = prime(monkeypatch, CEREBRAS)
    llm_calls._observe_rate_headers(header_429(remaining=100, reset="60s"), "TEST")
    measured = bucket.learner_floor_rps
    assert bucket.floor_provenance == llm_calls.FLOOR_MEASURED

    # A later headerless 429 must NOT degrade the measured ceiling into a guess.
    llm_calls._observe_rate_headers(FakeResp(429, {}, text=""), "TEST")

    assert bucket.floor_provenance == llm_calls.FLOOR_MEASURED
    assert bucket.learner_floor_rps == pytest.approx(measured, rel=1e-9)


def test_set_learner_floor_refuses_a_lower_rank_write_directly():
    """The precedence rule at its own seam, independent of the observer."""
    b = llm_calls._TokenBucket(8.0, 8.0)
    assert b.set_learner_floor(2.0, "e", provenance=llm_calls.FLOOR_MEASURED) is True
    assert b.set_learner_floor(0.1, "e", provenance=llm_calls.FLOOR_TABLE) is False
    assert b.set_learner_floor(0.1, "e", provenance=llm_calls.FLOOR_BACKOFF) is False
    assert b.learner_floor_rps == pytest.approx(2.0)
    # A CLEAR is unconditional — an endpoint switch must be able to drop a measured floor.
    assert b.set_learner_floor(0.0, "") is True
    assert b.floor_provenance == llm_calls.FLOOR_NONE


def test_the_two_assumed_kinds_share_a_rank_so_a_proven_wrong_table_value_can_decay(monkeypatch):
    """A table figure that still 429s must not freeze the pace at a number already refuted."""
    key, bucket = prime(monkeypatch, CEREBRAS)
    llm_calls._observe_rate_headers(FakeResp(429, {}, text=""), "TEST")
    first = bucket.learner_floor_rps
    assert bucket.floor_provenance == llm_calls.FLOOR_TABLE

    llm_calls._observe_rate_headers(FakeResp(429, {}, text=""), "TEST")

    assert bucket.learner_floor_rps < first
    assert bucket.floor_provenance == llm_calls.FLOOR_BACKOFF   # no longer the published figure
    assert "no published limits on file" in bucket.floor_note


def test_the_blind_backoff_cannot_decay_to_a_stopped_brain(monkeypatch):
    key, bucket = prime(monkeypatch, HOMELAB)
    for _ in range(40):
        llm_calls._observe_rate_headers(FakeResp(429, {}, text=""), "TEST")
    assert bucket.learner_floor_rps >= (1.0 / 60.0) - 1e-9
    assert bucket.rate > 0


def test_a_configured_ceiling_below_the_table_still_wins(monkeypatch):
    """'An explicitly configured value beats the table' — the floor only ever TIGHTENS."""
    monkeypatch.setenv("LLM_MAX_RPM", "2")            # operator asked for 2 rpm
    key, bucket = prime(monkeypatch, CEREBRAS)        # table would allow 5 rpm
    llm_calls._observe_rate_headers(FakeResp(429, {}, text=""), "TEST")
    assert bucket.rate <= (2 / 60.0) + 1e-9


# ──────────────────────────────────────────────────────────────────────────────
# A 429 THAT IS NOT ABOUT RATE
# ──────────────────────────────────────────────────────────────────────────────

def test_a_balance_429_does_not_become_a_rate_assumption(monkeypatch):
    """Measured on production: 100% of z.ai's 429 bodies were non-rate verdicts. Throttling
    an out-of-credit account makes it slower AND still broken."""
    key, bucket = prime(monkeypatch, ZAI)
    before = bucket.rate
    llm_calls._observe_rate_headers(FakeResp(429, {}, text=ZAI_BALANCE_429), "TEST")

    assert bucket.floor_provenance == llm_calls.FLOOR_NONE
    assert bucket.learner_floor_rps == 0.0
    assert bucket.rate == before


def test_a_genuine_rate_code_from_the_same_provider_is_still_honoured(monkeypatch):
    """1302 is z.ai's documented rate-limit code and is deliberately NOT in non_rate_429."""
    key, bucket = prime(monkeypatch, ZAI)
    before = bucket.rate
    body = '{"error":{"code":"1302","message":"Rate limit reached for requests"}}'
    llm_calls._observe_rate_headers(FakeResp(429, {}, text=body), "TEST")

    # z.ai publishes no RPM, so this is the blind back-off lane — but it DOES back off.
    assert bucket.floor_provenance == llm_calls.FLOOR_BACKOFF
    assert bucket.rate < before


def test_an_unreadable_body_fails_safe_toward_backing_off(monkeypatch):
    key, bucket = prime(monkeypatch, HOMELAB)
    before = bucket.rate
    ok, reason = llm_calls._429_is_rate_evidence(FakeResp(429, {}, text=""), None)
    assert ok is True and reason == "body_unreadable"
    llm_calls._observe_rate_headers(FakeResp(429, {}, text=""), "TEST")
    assert bucket.rate < before


# ──────────────────────────────────────────────────────────────────────────────
# THE TABLE IS DATA
# ──────────────────────────────────────────────────────────────────────────────

_AXES = ("rpm", "rps", "tpm", "rpd", "concurrency")


def test_every_table_entry_names_a_tier_a_source_and_a_date():
    """Published limits go stale; the date and source are what let a human check it later,
    so they are not optional. An entry without them is a bug."""
    assert llm_calls._RATE_429_FALLBACK, "the table must not be empty"
    for host, entry in llm_calls._RATE_429_FALLBACK.items():
        assert entry.get("tier"), f"{host}: no tier named"
        assert str(entry.get("source", "")).startswith("http"), f"{host}: no source url"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(entry.get("recorded", ""))), \
            f"{host}: recorded date missing or not ISO"
        assert isinstance(entry.get("sends_rate_headers"), bool), \
            f"{host}: must record whether the vendor sends rate headers"


def test_every_axis_is_recorded_explicitly_so_an_absence_is_visible():
    """Record what the vendor ACTUALLY publishes. Every axis is present with an explicit
    None where the vendor publishes nothing, so a missing figure cannot be mistaken for an
    oversight — and no axis is silently coerced into another."""
    for host, entry in llm_calls._RATE_429_FALLBACK.items():
        for axis in _AXES:
            assert axis in entry, f"{host}: axis {axis} not recorded (use None if unpublished)"
            v = entry[axis]
            assert v is None or (isinstance(v, (int, float)) and v > 0), \
                f"{host}: {axis}={v!r} is neither None nor a positive number"
        assert any(entry[a] is not None for a in _AXES), \
            f"{host}: publishes nothing on any axis — it belongs in _NO_PUBLISHED_LIMIT"


def test_a_provider_publishing_nothing_has_no_entry_and_is_recorded_as_such():
    """An empty cell is an honest answer; a guess is not. The negative result is recorded so
    it is not silently re-researched, but it must NOT appear in the limits table."""
    assert llm_calls._NO_PUBLISHED_LIMIT, "the recorded negatives must not be empty"
    for host, entry in llm_calls._NO_PUBLISHED_LIMIT.items():
        assert host not in llm_calls._RATE_429_FALLBACK, \
            f"{host} is in BOTH the limits table and the no-published-limit list"
        assert entry.get("reason"), f"{host}: no reason recorded"
        assert str(entry.get("source", "")).startswith("http"), f"{host}: no source url"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(entry.get("recorded", ""))), \
            f"{host}: recorded date missing or not ISO"


def test_the_recorded_negatives_are_documentation_and_never_reach_the_decision_path(monkeypatch):
    """_NO_PUBLISHED_LIMIT must not be consulted — a host listed there has to fall through to
    the conservative back-off exactly like any unknown host."""
    for host in llm_calls._NO_PUBLISHED_LIMIT:
        assert llm_calls._fallback_entry_for(f"https://{host}/v1") is None

    key, bucket = prime(monkeypatch, "https://api.openai.com/v1")
    before = bucket.rate
    llm_calls._observe_rate_headers(FakeResp(429, {}, text=""), "TEST")
    assert bucket.floor_provenance == llm_calls.FLOOR_BACKOFF
    assert bucket.rate < before


def test_only_exactly_convertible_units_become_a_pace():
    """rpm direct, rps x60 (arithmetic). TPM / RPD / concurrency are NOT convertible and must
    never be turned into a request rate."""
    assert llm_calls._pacing_rpm_from({"rpm": 5}) == 5.0
    assert llm_calls._pacing_rpm_from({"rpm": None, "rps": 150}) == 9000.0
    assert llm_calls._pacing_rpm_from({"rpm": None, "rps": None, "tpm": 5_000_000}) is None
    assert llm_calls._pacing_rpm_from({"rpm": None, "rps": None, "concurrency": 500}) is None
    assert llm_calls._pacing_rpm_from({"rpm": None, "rps": None, "rpd": 50}) is None
    assert llm_calls._pacing_rpm_from(None) is None


def test_a_concurrency_only_provider_is_not_given_an_invented_request_rate(monkeypatch):
    """DeepSeek publishes concurrency and no RPM/TPM. Concurrency->RPM needs per-request
    latency we do not have, so it must fall through to the back-off, not to a fabrication."""
    entry = llm_calls._fallback_entry_for("https://api.deepseek.com/v1")
    assert entry is not None
    assert entry["concurrency"] == 500 and entry["rpm"] is None and entry["rps"] is None
    assert llm_calls._pacing_rpm_from(entry) is None

    key, bucket = prime(monkeypatch, "https://api.deepseek.com/v1")
    before = bucket.rate
    llm_calls._observe_rate_headers(FakeResp(429, {}, text=""), "TEST")
    assert bucket.floor_provenance == llm_calls.FLOOR_BACKOFF
    assert bucket.rate < before


def test_a_published_figure_that_would_not_tighten_still_backs_off(monkeypatch):
    """xAI Tier 0 is 150 RPS (9,000 RPM) — far above any pace we run. Adopting it would
    change nothing and leave us hammering, so a 429 must fall through to the back-off."""
    entry = llm_calls._fallback_entry_for("https://api.x.ai/v1")
    assert llm_calls._pacing_rpm_from(entry) == 9000.0

    key, bucket = prime(monkeypatch, "https://api.x.ai/v1")
    before = bucket.rate                      # 8 rps from LLM_MAX_RPM=480
    llm_calls._observe_rate_headers(FakeResp(429, {}, text=""), "TEST")

    assert bucket.floor_provenance == llm_calls.FLOOR_BACKOFF
    assert bucket.rate < before
    assert "no published limits on file" in bucket.floor_note


def test_a_loose_published_ceiling_on_a_header_sending_provider_also_backs_off(monkeypatch):
    """Anthropic Start is 1,000 RPM — looser than our configured pace, so the same rule
    applies. (In practice Anthropic sends headers and the measured lane wins first.)"""
    key, bucket = prime(monkeypatch, "https://api.anthropic.com/v1")
    before = bucket.rate
    llm_calls._observe_rate_headers(FakeResp(429, {}, text=""), "TEST")
    assert bucket.floor_provenance == llm_calls.FLOOR_BACKOFF
    assert bucket.rate < before


def test_adding_a_provider_is_a_data_edit_not_a_code_edit(monkeypatch):
    """Proves the decision path is table-driven: a host the code has never heard of is
    adopted purely by appearing in the dict."""
    fake = dict(llm_calls._RATE_429_FALLBACK)
    fake["api.invented-provider.example"] = {
        "rpm": 12, "tier": "Fictional", "source": "https://example.invalid/limits",
        "recorded": "2026-08-13",
    }
    monkeypatch.setattr(llm_calls, "_RATE_429_FALLBACK", fake)
    key, bucket = prime(monkeypatch, "https://api.invented-provider.example/v1")
    llm_calls._observe_rate_headers(FakeResp(429, {}, text=""), "TEST")

    assert bucket.floor_provenance == llm_calls.FLOOR_TABLE
    assert bucket.learner_floor_rps == pytest.approx((12 * 0.9) / 60.0, rel=1e-3)
    assert "Fictional" in bucket.floor_note


def test_the_decision_path_contains_no_provider_name_branching():
    """No `if "z.ai" in url` anywhere in the functions that decide. Provider strings may
    appear only as table VALUES and in comments."""
    import inspect
    for fn in (llm_calls._lookup_by_host,
               llm_calls._fallback_entry_for,
               llm_calls._non_rate_codes_for,
               llm_calls._pacing_rpm_from,
               llm_calls._429_is_rate_evidence,
               llm_calls._adopt_rate_fallback_on_429,
               llm_calls.rate_assumption_disclaimer):
        src = inspect.getsource(fn)
        code = "\n".join(
            line for line in src.splitlines() if not line.strip().startswith("#")
        )
        for host in (set(llm_calls._RATE_429_FALLBACK)
                     | set(llm_calls._NO_PUBLISHED_LIMIT)
                     | set(llm_calls._NON_RATE_429_CODES)):
            assert host not in code, f"{fn.__name__} branches on the literal {host!r}"


def test_the_live_provider_publishes_nothing_so_it_has_no_row_at_all(monkeypatch):
    """z.ai is the case that prompted this work. It publishes no numeric limit of any kind,
    so per the rule it gets NO entry — but its documented 429 error-code semantics DO exist
    and live in a separate table, because those are error codes, not published limits."""
    assert "api.z.ai" not in llm_calls._RATE_429_FALLBACK
    assert "api.z.ai" in llm_calls._NO_PUBLISHED_LIMIT
    assert llm_calls._fallback_entry_for(ZAI) is None
    assert llm_calls._non_rate_codes_for(ZAI)          # codes are known even though limits are not

    key, bucket = prime(monkeypatch, ZAI)
    llm_calls._observe_rate_headers(
        FakeResp(429, {}, text='{"error":{"code":"1302"}}'), "TEST")
    assert bucket.floor_provenance == llm_calls.FLOOR_BACKOFF


# ──────────────────────────────────────────────────────────────────────────────
# THE DISCLAIMER
# ──────────────────────────────────────────────────────────────────────────────

def test_the_table_disclaimer_names_the_source_the_tier_and_the_date():
    entry = llm_calls._RATE_429_FALLBACK["api.cerebras.ai"]
    text = llm_calls.rate_assumption_disclaimer(5, llm_calls.FLOOR_TABLE, entry)
    assert "NOT a measurement" in text
    assert "Free Trial" in text
    assert "https://inference-docs.cerebras.ai/support/rate-limits" in text
    assert "2026-08-13" in text
    assert "replace" in text.lower()


def test_the_blind_backoff_disclaimer_admits_it_has_no_source():
    text = llm_calls.rate_assumption_disclaimer(4, llm_calls.FLOOR_BACKOFF, None)
    assert "NOT a measurement" in text
    assert "no published limits on file" in text
    assert "replace" in text.lower()
    assert "http" not in text          # it must not imply a source it does not have


def test_the_refusal_disclaimer_points_at_the_real_cause():
    text = llm_calls.rate_refusal_disclaimer("non_rate_code:1113")
    assert "did NOT change your rate limit" in text
    assert "balance" in text


# ──────────────────────────────────────────────────────────────────────────────
# ROLLBACK
# ──────────────────────────────────────────────────────────────────────────────

def test_the_flag_off_is_the_legacy_discard(monkeypatch):
    monkeypatch.setenv("LLM_RATE_429_FALLBACK", "0")
    key, bucket = prime(monkeypatch, CEREBRAS)
    before = bucket.rate
    llm_calls._observe_rate_headers(FakeResp(429, {}, text=""), "TEST")
    assert bucket.floor_provenance == llm_calls.FLOOR_NONE
    assert bucket.rate == before


def test_a_non_429_with_no_headers_is_still_a_no_op(monkeypatch):
    key, bucket = prime(monkeypatch, CEREBRAS)
    before = bucket.rate
    llm_calls._observe_rate_headers(FakeResp(200, {}, text="fine"), "TEST")
    assert bucket.floor_provenance == llm_calls.FLOOR_NONE
    assert bucket.rate == before


# ──────────────────────────────────────────────────────────────────────────────
# THE RATCHET — recovery from an ASSUMED floor
#
# A blind back-off only ever multiplies the pace DOWN. For a provider that SENDS rate
# headers that is fine: the next response installs a MEASURED floor and the assumption is
# gone. For a provider that 429s BARE nothing ever measures it, so nothing ever replaces
# it, and the floor ratchets to LLM_RATE_429_MIN_RPM and stays there for the life of the
# process. Measured against NVIDIA NIM: 480 -> 240 -> ... -> 1.0 rpm, and 500 consecutive
# clean 200s left it at 1.0.
#
# This is NOT a NIM property. It is the property of EVERY provider that publishes no limit
# and sends no headers — every host in _NO_PUBLISHED_LIMIT and every unknown host. These
# tests are therefore parametrized across that whole class ON PURPOSE: a fix keyed to one
# vendor would pass a single-host test and leave the rest ratcheted.
# ──────────────────────────────────────────────────────────────────────────────

# Every host we have RECORDED as publishing nothing, plus a genuinely unknown host. The
# list is derived from the table rather than written out, so a provider added to
# _NO_PUBLISHED_LIMIT tomorrow is covered by these pins without anyone remembering to.
_UNPUBLISHED_HOSTS = [f"https://{h}/v1" for h in llm_calls._NO_PUBLISHED_LIMIT] + [HOMELAB]


def touch_bucket(base_url):
    """Re-run the acquire-time bucket refresh WITHOUT taking a token.

    A real acquire would BLOCK here: at a 1-rpm floor the bucket is empty and it sleeps up to
    LLM_RATE_MAX_WAIT_S per call. The seam under test is the refresh in ``_get_rate_bucket``,
    which is exactly what the real acquire calls first, so drive that directly and keep these
    pins instant and sleep-free.
    """
    base, model = llm_calls._resolve_endpoint_and_model()
    rate, limits = llm_calls._effective_rate_per_sec(base, model)
    return llm_calls._get_rate_bucket(
        llm_calls._resolve_rate_limit_key(), rate, limits,
        llm_calls._rate_endpoint_tag(base))


def _ratchet_to_the_floor(monkeypatch, base_url):
    """Drive a bare, headerless 429 until the blind back-off bottoms out. Returns the bucket."""
    key, bucket = prime(monkeypatch, base_url)
    for _ in range(12):
        llm_calls._observe_rate_headers(FakeResp(429, {}, text=""), "TEST")
    assert bucket.floor_provenance == llm_calls.FLOOR_BACKOFF
    assert bucket.learner_floor_rps == pytest.approx(1.0 / 60.0, rel=1e-6), \
        "precondition: the blind back-off must have bottomed out at LLM_RATE_429_MIN_RPM"
    return key, bucket


@pytest.mark.parametrize("base_url", _UNPUBLISHED_HOSTS)
def test_a_bare_429_provider_recovers_instead_of_ratcheting_to_one_rpm(monkeypatch, base_url):
    """THE DEFECT. Every provider that neither publishes a limit nor sends headers used to
    pin at 1 rpm permanently. After a quiet interval the ASSUMED floor must ease back up."""
    monkeypatch.setenv("LLM_RATE_429_RECOVERY_S", "120")
    monkeypatch.setenv("LLM_RATE_429_RECOVERY_FACTOR", "1.5")
    key, bucket = _ratchet_to_the_floor(monkeypatch, base_url)

    # Nothing has changed yet — the quiet interval has not elapsed.
    touch_bucket(base_url)
    assert bucket.learner_floor_rps == pytest.approx(1.0 / 60.0, rel=1e-6)

    # Age the floor past the quiet interval (monotonic stamp, no sleeping in a unit test).
    bucket.floor_set_at -= 121.0
    touch_bucket(base_url)

    assert bucket.learner_floor_rps == pytest.approx(1.5 / 60.0, rel=1e-6), \
        f"{base_url}: an assumed floor must recover, not ratchet"
    assert bucket.floor_provenance == llm_calls.FLOOR_BACKOFF, \
        "recovery must not launder an assumption into a measurement"


@pytest.mark.parametrize("base_url", _UNPUBLISHED_HOSTS)
def test_recovery_stops_at_the_configured_rate_and_never_exceeds_it(monkeypatch, base_url):
    """The failure mode being fixed is OVER-throttling; over-shooting the other way gets the
    account rate-limited or billed. Recovery is bounded by the ALREADY-CLAMPED effective
    rate, so it can only ever undo an assumption we invented."""
    monkeypatch.setenv("LLM_RATE_429_RECOVERY_S", "1")
    key, bucket = _ratchet_to_the_floor(monkeypatch, base_url)

    for _ in range(60):
        bucket.floor_set_at -= 5.0
        touch_bucket(base_url)

    configured_rps = 480 / 60.0
    assert bucket.rate <= configured_rps + 1e-9, \
        f"{base_url}: recovery must never pace above the configured effective rate"
    # Fully recovered → the assumption is DROPPED, not pinned at the ceiling, so the
    # tenant-facing disclaimer stops claiming a ceiling that is no longer in force.
    assert bucket.learner_floor_rps == 0.0
    assert bucket.floor_provenance == llm_calls.FLOOR_NONE


@pytest.mark.parametrize("base_url", _UNPUBLISHED_HOSTS)
def test_a_429_during_recovery_re_tightens_immediately(monkeypatch, base_url):
    """FAIL-CLOSED. Recovery is a probe, not a decision: one refusal must undo it at once,
    and the increase (1.5x) is deliberately smaller than the decrease (0.5x) so sustained
    pressure still converges DOWN."""
    monkeypatch.setenv("LLM_RATE_429_RECOVERY_S", "10")
    key, bucket = _ratchet_to_the_floor(monkeypatch, base_url)

    for _ in range(4):
        bucket.floor_set_at -= 11.0
        touch_bucket(base_url)
    recovered = bucket.learner_floor_rps
    assert recovered > 1.0 / 60.0

    llm_calls._observe_rate_headers(FakeResp(429, {}, text=""), "TEST")
    assert bucket.learner_floor_rps == pytest.approx(recovered * 0.5, rel=1e-6), \
        f"{base_url}: a 429 must re-tighten a recovering floor immediately"


def test_recovery_never_touches_a_measured_floor(monkeypatch):
    """PRECEDENCE IS NOT INVERTED. A MEASURED floor is a FACT the provider stated. Only an
    ASSUMPTION may be walked back."""
    monkeypatch.setenv("LLM_RATE_429_RECOVERY_S", "1")
    key, bucket = prime(monkeypatch, CEREBRAS)
    llm_calls._observe_rate_headers(header_429(remaining=100, reset="60s"), "TEST")
    assert bucket.floor_provenance == llm_calls.FLOOR_MEASURED
    measured = bucket.learner_floor_rps

    for _ in range(10):
        bucket.floor_set_at -= 5.0
        touch_bucket(CEREBRAS)

    assert bucket.learner_floor_rps == pytest.approx(measured, rel=1e-9), \
        "a measured floor must never be relaxed by the recovery lane"
    assert bucket.floor_provenance == llm_calls.FLOOR_MEASURED


def test_recovery_disabled_is_byte_for_byte_the_old_ratchet(monkeypatch):
    """ROLLBACK LEVER. LLM_RATE_429_RECOVERY_S=0 restores the pre-fix behaviour exactly."""
    monkeypatch.setenv("LLM_RATE_429_RECOVERY_S", "0")
    key, bucket = _ratchet_to_the_floor(monkeypatch, HOMELAB)
    for _ in range(20):
        bucket.floor_set_at -= 600.0
        touch_bucket(HOMELAB)
    assert bucket.learner_floor_rps == pytest.approx(1.0 / 60.0, rel=1e-6)


def test_every_recorded_negative_is_covered_by_the_recovery_pins():
    """The owner's bar: this is for ALL unpublished providers, not the one that burned us.
    If a provider is added to _NO_PUBLISHED_LIMIT, the parametrized pins above must already
    be driving it — this asserts the derivation, so nobody has to remember."""
    for host in llm_calls._NO_PUBLISHED_LIMIT:
        assert f"https://{host}/v1" in _UNPUBLISHED_HOSTS, \
            f"{host} is recorded as publishing nothing but is not driven by the recovery pins"


# ──────────────────────────────────────────────────────────────────────────────
# THE MISSING ROW — a headerless provider needs a ceiling from SOMEWHERE
# ──────────────────────────────────────────────────────────────────────────────

NIM = "https://integrate.api.nvidia.com/v1"


def test_a_bare_429_from_nim_adopts_the_recorded_ceiling_not_a_blind_decay(monkeypatch):
    """THE ROW THAT WAS MISSING. With no entry, NIM's headerless 429 fell straight through to
    the blind back-off and halved from the configured rate — nine refusals to reach 1 rpm.
    With the row, the FIRST refusal prices it at the recorded tier x headroom."""
    key, bucket = prime(monkeypatch, NIM)
    llm_calls._observe_rate_headers(FakeResp(429, {}, text=""), "TEST")

    assert bucket.floor_provenance == llm_calls.FLOOR_TABLE, \
        "a recorded ceiling must be adopted as a TABLE assumption, not a blind decay"
    assert bucket.learner_floor_rps == pytest.approx((40 * 0.9) / 60.0, rel=1e-6), \
        "the recorded tier must be paced with the standard headroom, never at exactly the cap"


def test_the_nim_ceiling_is_budgeted_ACCOUNT_WIDE_not_per_model(monkeypatch):
    """CORRECTNESS, NOT TUNING. The cap is reported account-wide. If the pacer's bucket were
    keyed per-model, two models on one key would each get the full 40 and overshoot by 2x.
    The key must not vary with the model."""
    monkeypatch.setenv("LLM_BASE_URL", NIM)
    monkeypatch.setenv("WGM_LLM_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")
    key_a = llm_calls._resolve_rate_limit_key()
    monkeypatch.setenv("WGM_LLM_MODEL", "meta/llama-3.3-70b-instruct")
    key_b = llm_calls._resolve_rate_limit_key()
    assert key_a == key_b, "the rate bucket must not be keyed by model — the cap is account-wide"


def test_the_recorded_ceiling_still_decays_if_the_provider_refuses_anyway(monkeypatch):
    """The recorded figure is the free-tier DEFAULT, not a universal constant, and NVIDIA says
    limits vary. An account whose real cap is lower must still converge DOWN — the table is a
    starting estimate, never a floor the pacer refuses to go below."""
    key, bucket = prime(monkeypatch, NIM)
    llm_calls._observe_rate_headers(FakeResp(429, {}, text=""), "TEST")
    table_rps = bucket.learner_floor_rps
    llm_calls._observe_rate_headers(FakeResp(429, {}, text=""), "TEST")
    assert bucket.learner_floor_rps < table_rps, \
        "a 429 at the recorded pace proves the recorded pace wrong for this key — decay"
    assert bucket.floor_provenance == llm_calls.FLOOR_BACKOFF, \
        "once decayed past it, the disclaimer must stop claiming the recorded source"
