"""Per-brain CONFIGURABLE rate limit (up to provider max) — resolution + clamp + fail-safe.

Extends the account-wide client-side limiter (tests/test_llm_rate_limiter.py) with the
per-brain override stored on ``account_llm_config.max_rpm`` (migration 188). A tenant may set
an outbound RPM ceiling UP TO — never above — the provider's published maximum for its endpoint.

Coverage:
  (a) a per-brain max_rpm BELOW the provider max is honored (effective == that value).
  (b) a per-brain max_rpm ABOVE the provider max is CLAMPED to the provider max — on the pacing
      READ path (_effective_rate_per_sec) AND on the setter WRITE path (set_account_max_rpm).
  (c) NULL / unset max_rpm → the existing global/registry behavior, byte-for-byte.
  (d) TIGHTEST-OF-THREE: min(per_brain, global env LLM_MAX_RPM, provider_max*headroom).
  (e) the clamp + disclaimer work for the ANTHROPIC (api.anthropic.com) endpoint too.
  (f) the transparency disclaimer is surfaced.

These drive the gate functions directly (no live LLM / network). The setter/reader DB helpers
are exercised via the pure display core (``effective_rpm_for``) + a monkeypatched endpoint so no
Postgres is required.

Run: python3 -m pytest tests/test_llm_per_brain_ratelimit.py -q
"""

import pytest

import src.api.llm_calls as rl


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(rl, "_current_llm_config_or_none", lambda: None)
    monkeypatch.setenv("LLM_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("LLM_RATE_HEADROOM", "0.9")
    rl.reset_rate_limiter()
    yield
    rl.reset_rate_limiter()


# ── (a) per-brain below provider max is HONORED ──────────────────────────────────────

def test_per_brain_below_provider_max_is_honored(monkeypatch):
    # Global env is huge; provider (dashscope 600) clamp = 9/s; per-brain 300 RPM = 5/s → wins.
    monkeypatch.setenv("LLM_MAX_RPM", "100000")
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "a", "max_rpm": 300,
                                 "base_url": "https://dashscope-intl.aliyuncs.com/v1",
                                 "model": "qwen-turbo"})
    rate, limits = rl._effective_rate_per_sec(
        "https://dashscope-intl.aliyuncs.com/v1", "qwen-turbo")
    assert rate == pytest.approx(5.0, abs=1e-6)     # 300/60, the tightest
    assert rate * 60 <= 600                          # still under provider max


# ── (b) per-brain ABOVE provider max is CLAMPED (read path) ──────────────────────────

def test_per_brain_above_provider_max_is_clamped_on_read(monkeypatch):
    # Tenant stored an absurd 5000 RPM for a 600-RPM endpoint; global env also huge.
    monkeypatch.setenv("LLM_MAX_RPM", "100000")
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "a", "max_rpm": 5000,
                                 "base_url": "https://dashscope-intl.aliyuncs.com/v1",
                                 "model": "qwen-turbo"})
    rate, limits = rl._effective_rate_per_sec(
        "https://dashscope-intl.aliyuncs.com/v1", "qwen-turbo")
    # Re-clamped to provider ceiling floor(600*0.9)/60 = 9.0 — NOT 5000/60.
    assert rate == pytest.approx(9.0, abs=1e-6)
    assert rate * 60 <= 600  # structurally impossible to exceed provider max


# ── (c) NULL / unset per-brain → existing behavior, byte-for-byte ────────────────────

def test_null_per_brain_falls_back_to_registry(monkeypatch):
    monkeypatch.setenv("LLM_MAX_RPM", "100000")
    # cfg with NO max_rpm key at all.
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "a",
                                 "base_url": "https://dashscope-intl.aliyuncs.com/v1",
                                 "model": "qwen-turbo"})
    rate, _ = rl._effective_rate_per_sec(
        "https://dashscope-intl.aliyuncs.com/v1", "qwen-turbo")
    assert rate == pytest.approx(9.0, abs=1e-6)   # provider clamp only, unchanged

    # explicit None max_rpm behaves identically
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "a", "max_rpm": None,
                                 "base_url": "https://dashscope-intl.aliyuncs.com/v1",
                                 "model": "qwen-turbo"})
    assert rl._configured_brain_max_rpm() is None
    rate2, _ = rl._effective_rate_per_sec(
        "https://dashscope-intl.aliyuncs.com/v1", "qwen-turbo")
    assert rate2 == pytest.approx(9.0, abs=1e-6)


def test_null_per_brain_unknown_host_unchanged(monkeypatch):
    # Self-hosted host + no per-brain → configured env pace, no clamp (byte-for-byte today).
    monkeypatch.setenv("LLM_MAX_RPM", "1200")  # 20/s
    monkeypatch.setattr(rl, "_current_llm_config_or_none", lambda: None)
    rate, limits = rl._effective_rate_per_sec("http://truenas:1234/v1", "qwen3.5-36b")
    assert rate == pytest.approx(20.0, abs=1e-6)
    assert limits["known"] is False


# ── (d) tightest-of-three ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("env_rpm,brain_rpm,expected_rps", [
    # min(global, brain, provider*headroom=9)  [dashscope 600]
    (600, 120, 2.0),      # brain 120/60 = 2/s is tightest
    (120, 600, 2.0),      # global 120/60 = 2/s is tightest
    (100000, 100000, 9.0),  # provider clamp 9/s is tightest
])
def test_tightest_of_three(monkeypatch, env_rpm, brain_rpm, expected_rps):
    monkeypatch.setenv("LLM_MAX_RPM", str(env_rpm))
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "a", "max_rpm": brain_rpm,
                                 "base_url": "https://dashscope-intl.aliyuncs.com/v1",
                                 "model": "qwen-turbo"})
    rate, _ = rl._effective_rate_per_sec(
        "https://dashscope-intl.aliyuncs.com/v1", "qwen-turbo")
    assert rate == pytest.approx(expected_rps, abs=1e-6)


# ── (e) ANTHROPIC / Claude endpoint: registry populated + clamp + disclaimer ─────────

# NOTE (2026-07-30): these previously pinned 50 RPM / 30k ITPM as "Claude Tier-1 published".
# That was a stale FACT, not a deliberate policy — Anthropic renamed the tiers (Tier 1..4 →
# Start/Build/Scale/Custom) and the entry (Start) tier is 1,000 RPM / 2M ITPM / 400k OTPM for
# the Opus+Sonnet+Haiku-4.5 families. Verified against
# https://platform.claude.com/docs/en/api/rate-limits (fetched 2026-07-30). The test INTENT is
# unchanged: the registry is populated, and a tenant asking for more than the published ceiling
# is clamped down to it. Only the published number moved.
_CLAUDE_START_RPM = 1_000


def test_anthropic_registry_populated():
    lim = rl.resolve_provider_limits("https://api.anthropic.com", "claude-sonnet-4")
    assert lim["known"] is True
    assert lim["rpm"] == _CLAUDE_START_RPM          # Start-tier published RPM
    lim_haiku = rl.resolve_provider_limits("https://api.anthropic.com/v1", "claude-haiku-3-5")
    assert lim_haiku["known"] is True
    assert lim_haiku["rpm"] == _CLAUDE_START_RPM
    # Haiku 3.5 carries its own LOWER input-token bucket (100k, not 2M) — the one family where
    # cache reads also count toward ITPM. Guards the substring-ordering of the registry.
    assert lim_haiku["tpm"] == 100_000


def test_anthropic_per_brain_above_max_is_clamped(monkeypatch):
    # Start tier is 1000 RPM; a tenant setting 5000 must clamp to floor(1000*0.9)/60 = 15.0/s.
    monkeypatch.setenv("LLM_MAX_RPM", "100000")
    monkeypatch.setattr(rl, "_current_llm_config_or_none",
                        lambda: {"account_id": "a", "max_rpm": 5000,
                                 "base_url": "https://api.anthropic.com",
                                 "model": "claude-opus-4"})
    rate, limits = rl._effective_rate_per_sec("https://api.anthropic.com", "claude-opus-4")
    assert limits["known"] is True
    assert rate == pytest.approx(900 / 60.0, abs=1e-6)   # floor(1000*0.9)=900 → 15.0/s
    assert rate * 60 <= _CLAUDE_START_RPM  # cannot exceed the published Start-tier max


def test_anthropic_effective_rpm_display_and_disclaimer():
    info = rl.effective_rpm_for("https://api.anthropic.com", "claude-sonnet-4", brain_max_rpm=5000)
    assert info["provider_known"] is True
    assert info["provider_max_rpm"] == _CLAUDE_START_RPM  # raw published ceiling shown to the UI
    assert info["configured_max_rpm"] == 5000            # what the tenant asked for
    assert info["effective_rpm"] <= _CLAUDE_START_RPM     # never above provider max
    assert "provider" in info["disclaimer"].lower() and "never higher" in info["disclaimer"].lower()


# ── (f) pure display core: tightest-of-three RPM + disclaimer ────────────────────────

def test_effective_rpm_for_tightest(monkeypatch):
    monkeypatch.setenv("LLM_MAX_RPM", "480")
    # dashscope 600 → provider cap floor(600*0.9)=540; brain 300 tightest.
    info = rl.effective_rpm_for("https://dashscope-intl.aliyuncs.com/v1", "qwen-turbo", 300)
    assert info["provider_max_rpm"] == 600
    assert info["global_env_rpm"] == 480
    assert info["configured_max_rpm"] == 300
    assert info["effective_rpm"] == 300


def test_effective_rpm_for_unknown_host_no_provider_max(monkeypatch):
    monkeypatch.setenv("LLM_MAX_RPM", "480")
    info = rl.effective_rpm_for("http://192.0.2.20:1234/v1", "qwen3.5-9b", 300)
    assert info["provider_known"] is False
    assert info["provider_max_rpm"] is None      # no published max for a self-hosted host
    assert info["effective_rpm"] == 300          # min(480 global, 300 brain)


def test_disclaimer_text():
    d = rl.rate_limit_disclaimer()
    assert "provider" in d.lower()
    assert "never higher" in d.lower()


# ── setter clamp-on-write (pure math via monkeypatched endpoint; no DB) ───────────────

def test_setter_clamps_on_write_known_host(monkeypatch):
    import saas.llm_config as cfg
    monkeypatch.setattr(cfg, "_account_endpoint",
                        lambda aid: ("https://dashscope-intl.aliyuncs.com/v1", "qwen-turbo"))
    writes = {}
    monkeypatch.setattr(cfg, "upsert_config", lambda aid, fields: writes.update(fields))

    # Requesting 5000 against a 600-RPM endpoint → STORED clamped to 600.
    res = cfg.set_account_max_rpm("acct", 5000)
    assert res["clamped"] is True
    assert res["stored_max_rpm"] == 600
    assert res["provider_max_rpm"] == 600
    assert writes == {"max_rpm": 600}
    assert res["effective_rpm"] <= 600


def test_setter_below_max_stored_as_is(monkeypatch):
    import saas.llm_config as cfg
    monkeypatch.setattr(cfg, "_account_endpoint",
                        lambda aid: ("https://dashscope-intl.aliyuncs.com/v1", "qwen-turbo"))
    writes = {}
    monkeypatch.setattr(cfg, "upsert_config", lambda aid, fields: writes.update(fields))
    res = cfg.set_account_max_rpm("acct", 200)
    assert res["clamped"] is False
    assert res["stored_max_rpm"] == 200
    assert writes == {"max_rpm": 200}


def test_setter_none_clears_override(monkeypatch):
    import saas.llm_config as cfg
    monkeypatch.setattr(cfg, "_account_endpoint",
                        lambda aid: ("https://dashscope-intl.aliyuncs.com/v1", "qwen-turbo"))
    writes = {}
    monkeypatch.setattr(cfg, "upsert_config", lambda aid, fields: writes.update(fields))
    res = cfg.set_account_max_rpm("acct", None)
    assert res["stored_max_rpm"] is None
    assert writes == {"max_rpm": None}


def test_setter_anthropic_clamp(monkeypatch):
    import saas.llm_config as cfg
    monkeypatch.setattr(cfg, "_account_endpoint",
                        lambda aid: ("https://api.anthropic.com", "claude-opus-4"))
    writes = {}
    monkeypatch.setattr(cfg, "upsert_config", lambda aid, fields: writes.update(fields))
    res = cfg.set_account_max_rpm("acct", 5000)
    assert res["clamped"] is True
    # Start-tier published max (was 50 — a stale Claude-3-era figure; see note above).
    assert res["stored_max_rpm"] == _CLAUDE_START_RPM
    assert writes == {"max_rpm": _CLAUDE_START_RPM}
