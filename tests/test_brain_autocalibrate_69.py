"""#69 — a brain's latency profile is calibrated and stored AUTOMATICALLY.

Owner's rule: setting a tenant's brain calibrates it. Validated on saas 452b177c that it did not:
neither save door calibrated, the account "Test" never ran the REFRAME probe, and a seat's own
manual profile was ignored when its brain was inherited from the account. The owner's NVIDIA NIM
brain (prod, 72 h: p50 1.5 s, p95 44.2 s, 22 timeouts) sat on ``hosted-fast`` — and the one
calibration that had run stored p95 948 ms from a quiet window.

These pins drive the REAL routes and the REAL SQL against a throwaway Postgres (the database name
must end in ``_test``); only the LLM is stubbed. Each goes red on the base (452b177c) and green on
the fix.

  python3 tools/fltest.py --bug 69 --test tests/test_brain_autocalibrate_69.py
"""

import os
import time
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.environ.get("POSTGRES_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN or not DSN.rsplit("/", 1)[-1].split("?")[0].endswith("_test"),
    reason="needs POSTGRES_DSN to a throwaway *_test database (migrated)")

CORP = str(uuid.uuid4())      # account_kind='tenant' (one org brain)
HOUSE = str(uuid.uuid4())     # account_kind='individual_house' (per-seat brains)
GOV = str(uuid.uuid4())       # corporate seat, GOVERNED (org owns the endpoint)
GRANT = str(uuid.uuid4())     # corporate seat, GRANTED endpoint control, no brain of its own
HSEAT = str(uuid.uuid4())     # house seat with its own brain
OTHER = str(uuid.uuid4())     # a second, unrelated tenant (starvation pin)

NIM_P95_MS = 44160            # measured on the owner's brain (prod, 72 h)


def _sql(q, params=()):
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(q, params)
            rows = cur.fetchall() if cur.description else None
        conn.commit()
        return rows
    finally:
        conn.close()


def _profile_row(account_id=None, user_id=None):
    if user_id:
        rows = _sql("SELECT latency_profile, latency_profile_source, latency_p95_ms, "
                    "latency_samples FROM public.seat_llm_config WHERE user_id = %s", (user_id,))
    else:
        rows = _sql("SELECT latency_profile, latency_profile_source, latency_p95_ms, "
                    "latency_samples FROM public.account_llm_config WHERE account_id = %s",
                    (account_id,))
    return rows[0] if rows else None


def _wait_for(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    return pred()


def _drain_save_lane():
    """Wait for every queued save-triggered calibration (single-thread lane) to finish."""
    from saas import brain_autocalibrate as ac
    ac.drain(timeout=30)


@pytest.fixture(scope="module", autouse=True)
def _env():
    """PRODUCTION flag values (truenas `faultline` container: SAAS_MODE=1,
    LLM_TIMEOUT_REFRAME=45; SAAS_CALIBRATION_DEADLINE_S unset → code default). Module-scoped
    MonkeyPatch, never module-level os.environ (#63)."""
    mp = pytest.MonkeyPatch()
    mp.setenv("SAAS_MODE", "1")
    mp.setenv("LLM_TIMEOUT_REFRAME", "45")
    mp.delenv("SAAS_CALIBRATION_DEADLINE_S", raising=False)
    mp.delenv("SAAS_AUTOCALIBRATE_ON_SAVE", raising=False)
    mp.delenv("SAAS_LATENCY_WATCH", raising=False)
    _sql("INSERT INTO public.accounts (account_id, name, slug, account_kind) VALUES "
         "(%s,'corp69',%s,'tenant'), (%s,'house69',%s,'individual_house'), "
         "(%s,'other69',%s,'tenant')",
         (CORP, "corp69-" + CORP[:8], HOUSE, "house69-" + HOUSE[:8],
          OTHER, "other69-" + OTHER[:8]))
    _sql("INSERT INTO public.account_users (account_id, user_id) VALUES "
         "(%s,%s),(%s,%s),(%s,%s)", (CORP, GOV, CORP, GRANT, HOUSE, HSEAT))
    _sql("INSERT INTO public.individual_users (user_id, account_id, email, password_hash, "
         "tenant_context, can_control_endpoint) VALUES "
         "(%s,%s,%s,'x','real',false), (%s,%s,%s,'x','real',true)",
         (GOV, CORP, f"gov-{GOV[:8]}@t.test", GRANT, CORP, f"grant-{GRANT[:8]}@t.test"))
    # A real seat is minted governed (the insert path forces the grant OFF); granting is a
    # separate admin act — the same UPDATE the tenant console's toggle performs.
    _sql("UPDATE public.individual_users SET can_control_endpoint = true WHERE user_id = %s",
         (GRANT,))
    assert _sql("SELECT can_control_endpoint FROM public.individual_users WHERE user_id = %s",
                (GRANT,))[0][0] is True
    yield
    for t, col in (("seat_llm_config", "account_id"), ("account_llm_config", "account_id"),
                   ("individual_users", "account_id"), ("account_users", "account_id"),
                   ("accounts", "account_id")):
        _sql(f"DELETE FROM public.{t} WHERE {col} = ANY(%s::uuid[])", ([CORP, HOUSE, OTHER],))
    mp.undo()


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    from saas import llm_config as lc
    from saas import seat_brain as sb
    # No DNS in a pin: the SSRF guard has its own suite.
    monkeypatch.setattr(lc, "validate_egress_url", lambda url: None)
    lc.invalidate_config_cache()
    sb.invalidate_kind_cache()
    try:
        from saas import brain_autocalibrate as ac
        ac.reset_for_tests()
    except ImportError:
        pass
    try:
        # (#229) the auto-posture shares this suite's sample feed (probe_event tees to both),
        # and its tightened state is in-process like autocalibrate's rings — reset it here or
        # an outage window fed to one test caps LLMTimeouts for the same brain in the next.
        from saas import brain_posture
        brain_posture.reset_for_tests()
    except ImportError:
        pass
    _sql("DELETE FROM public.seat_llm_config WHERE account_id = ANY(%s::uuid[])",
         ([CORP, HOUSE, OTHER],))
    _sql("DELETE FROM public.account_llm_config WHERE account_id = ANY(%s::uuid[])",
         ([CORP, HOUSE, OTHER],))
    yield
    lc.invalidate_config_cache()
    sb.invalidate_kind_cache()


def _stub_probe(monkeypatch, profile, p95_ms, p50_ms=1500, samples=5):
    """Stub the LLM at the calibration probe: returns a measurement, spends nothing."""
    from saas import llm_admin
    calls = []

    def _probe(account_id, cfg, *a, **k):
        calls.append((account_id, cfg.get("base_url"), cfg.get("brain_scope")))
        return {"ok": True, "model": cfg.get("model"), "samples": samples, "failures": 0,
                "p50_ms": p50_ms, "p95_ms": p95_ms, "profile": profile,
                "reframe_budget_s": 0, "detail": "", "_normalized": profile}
    monkeypatch.setattr(llm_admin, "probe_reframe_latency", _probe)
    return calls


def _save_account(account_id, **fields):
    from saas import tenant_console as tc
    from saas.llm_admin import LLMConfigRequest
    return tc.tenant_set_llm_config(LLMConfigRequest(**fields), account_id=account_id)


# ══════════════════════════════════════════════════════════════════════════════════════
# 1. SAVE → background calibration persists a profile
# ══════════════════════════════════════════════════════════════════════════════════════

def test_account_save_calibrates_in_the_background_and_persists(monkeypatch):
    calls = _stub_probe(monkeypatch, "slow", NIM_P95_MS)
    resp = _save_account(CORP, base_url="https://nim.example/v1", model="nemotron-30b")
    assert resp.get("calibration") == "scheduled", resp
    row = _wait_for(lambda: (_profile_row(CORP) or (None,))[0] and _profile_row(CORP))
    assert row[:3] == ("slow", "measured", NIM_P95_MS), row
    assert calls and calls[0][1] == "https://nim.example/v1" and calls[0][2] == "account"


def test_account_save_runs_the_real_reframe_probe_with_the_llm_stubbed(monkeypatch):
    """The whole chain, only the network stubbed: save → resolve → probe_reframe_latency (five
    REFRAME-shaped calls through call_llm_with_retry_sync) → resolve_profile_from_p95 → SQL."""
    import src.api.llm_calls as llm_calls
    seen = []

    def _llm(**kw):
        seen.append(kw.get("operation"))
        return {"atoms": [{"statement": "x", "source": "x"}]}
    monkeypatch.setattr(llm_calls, "call_llm_with_retry_sync", _llm)
    _save_account(CORP, base_url="https://fast.example/v1", model="fast-model")
    row = _wait_for(lambda: (_profile_row(CORP) or (None,))[0] and _profile_row(CORP))
    assert row[0] == "hosted-fast" and row[1] == "measured" and row[3] == 5, row
    assert seen and set(seen) == {"REFRAME"}


def test_seat_save_calibrates_the_seat_row_fresh_domain_control(monkeypatch):
    """Fresh-domain control: a different account KIND (house) and a different scope (seat) take
    the same path — nothing in the fix is keyed to the tenant it was found on."""
    from saas import user_console as uc
    from saas.llm_admin import LLMConfigRequest
    _stub_probe(monkeypatch, "local", 9000)
    ind = {"id": "i-" + HSEAT[:8], "user_id": HSEAT, "account_id": HOUSE,
           "tenant_context": "house", "can_control_endpoint": True}
    resp = uc.me_set_llm_endpoint(LLMConfigRequest(base_url="https://home.example/v1",
                                                   model="home-model"), individual=ind)
    assert resp.get("calibration") == "scheduled", resp
    row = _wait_for(lambda: (_profile_row(user_id=HSEAT) or (None,))[0]
                    and _profile_row(user_id=HSEAT))
    assert row[:3] == ("local", "measured", 9000), row
    assert _profile_row(HOUSE) is None, "a seat's measurement must never land on the house row"


# ══════════════════════════════════════════════════════════════════════════════════════
# 2. MANUAL survives; a re-save of the SAME brain never narrows
# ══════════════════════════════════════════════════════════════════════════════════════

def test_manual_profile_survives_a_save(monkeypatch):
    from saas import llm_config as lc
    _save_account(CORP, base_url="https://a.example/v1", model="m1")
    _drain_save_lane()
    assert lc.save_calibration(CORP, profile="local", source="manual")
    _stub_probe(monkeypatch, "hosted-fast", 900)
    _save_account(CORP, base_url="https://b.example/v1", model="m2")   # a NEW brain
    _drain_save_lane()
    assert _profile_row(CORP)[:2] == ("local", "manual")


def test_resaving_the_same_brain_never_narrows_but_a_new_brain_may(monkeypatch):
    """The owner's row: slow/measured/44160 after the real tail was seen. A quiet-window probe
    after re-saving the SAME endpoint must not put it back to hosted-fast; switching to a
    different endpoint legitimately may."""
    from saas import llm_config as lc
    _stub_probe(monkeypatch, "slow", NIM_P95_MS)
    _save_account(CORP, base_url="https://nim.example/v1", model="nemotron-30b")
    _drain_save_lane()
    assert _profile_row(CORP)[0] == "slow"

    _stub_probe(monkeypatch, "hosted-fast", 948)
    _save_account(CORP, base_url="https://nim.example/v1", model="nemotron-30b",
                  extra_params={"temperature": 0.0})
    _drain_save_lane()
    assert _profile_row(CORP)[:3] == ("slow", "measured", NIM_P95_MS)

    _save_account(CORP, base_url="https://other-provider.example/v1", model="other")
    _drain_save_lane()
    assert _profile_row(CORP)[:3] == ("hosted-fast", "measured", 948)
    assert lc.save_measured_profile("account", CORP, profile="slow", mode="widen",
                                    base_url="https://other-provider.example/v1",
                                    model="other") == "saved"


# ══════════════════════════════════════════════════════════════════════════════════════
# 3. The account "Test" button calibrates (parity with the seat console)
# ══════════════════════════════════════════════════════════════════════════════════════

def test_account_test_button_calibrates(monkeypatch):
    from saas import llm_admin
    from saas import tenant_console as tc
    monkeypatch.setenv("SAAS_AUTOCALIBRATE_ON_SAVE", "0")   # isolate the Test button
    _save_account(CORP, base_url="https://t.example/v1", model="t-model")
    assert _profile_row(CORP)[0] is None
    monkeypatch.setattr(llm_admin, "_probe_extraction",
                        lambda account_id, cfg: {"ok": True, "model": cfg["model"],
                                                 "triples_found": 1, "detail": ""})
    _stub_probe(monkeypatch, "local", 12000)
    out = tc.tenant_test_llm_config(account_id=CORP)
    assert out.get("calibration", {}).get("saved") is True, out
    assert out["calibration"]["profile"] == "local"
    assert _profile_row(CORP)[:3] == ("local", "measured", 12000)


def test_account_test_button_never_narrows_a_measured_profile(monkeypatch):
    from saas import llm_admin
    from saas import llm_config as lc
    from saas import tenant_console as tc
    monkeypatch.setenv("SAAS_AUTOCALIBRATE_ON_SAVE", "0")
    _save_account(CORP, base_url="https://t.example/v1", model="t-model")
    assert lc.save_calibration(CORP, profile="slow", source="measured", p95_ms=NIM_P95_MS)
    monkeypatch.setattr(llm_admin, "_probe_extraction",
                        lambda account_id, cfg: {"ok": True, "model": cfg["model"]})
    _stub_probe(monkeypatch, "hosted-fast", 948)
    out = tc.tenant_test_llm_config(account_id=CORP)
    assert out["calibration"]["outcome"] == "not_wider"
    assert _profile_row(CORP)[:3] == ("slow", "measured", NIM_P95_MS)


# ══════════════════════════════════════════════════════════════════════════════════════
# 4. PASSIVE rolling p95 — widens hosted-fast → slow on the owner's tail, never narrows
# ══════════════════════════════════════════════════════════════════════════════════════

def _nim_shaped_traffic():
    """34 ok calls (p50 1.5 s, a tail to 57.8 s) + 22 timeouts at the 30 s / 60 s cutoffs."""
    ok = [1.2, 1.3, 1.4, 1.5, 1.5, 1.5, 1.6, 1.4, 1.5, 1.3, 1.6, 1.5, 1.7, 1.4, 1.5, 1.6,
          1.5, 1.4, 2.0, 2.5, 3.1, 1.5, 1.5, 1.6, 4.8, 9.7, 12.0, 18.4, 22.9, 31.0, 38.6,
          44.2, 51.3, 57.8]
    events = [("ok", s, 60.0, None) for s in ok]
    events += [("fail", 30.0, 30.0, {"kind": "timeout"})] * 11
    events += [("fail", 60.0, 60.0, {"kind": "timeout"})] * 11
    # interleave so the window fills the way live traffic does
    return events[::2] + events[1::2]


def test_rolling_p95_widens_hosted_fast_to_slow_and_never_narrows(monkeypatch):
    from saas import brain_health
    from saas import llm_config as lc
    from saas import llm_context as lx
    from saas import seat_brain as sb
    monkeypatch.setenv("SAAS_AUTOCALIBRATE_ON_SAVE", "0")
    _save_account(CORP, base_url="https://nim.example/v1", model="nemotron-30b")
    assert lc.save_calibration(CORP, profile="hosted-fast", source="measured", p95_ms=948)

    lc.invalidate_config_cache()
    cfg = sb.resolve_brain(GOV, CORP)
    assert cfg["latency_profile"] == "hosted-fast"
    tok = lx.bind_explicit_llm_config(cfg)
    try:
        # A fast REFUSAL is not a latency sample — it must not dilute the window.
        for _ in range(30):
            brain_health.probe_event(GOV, "REFRAME", "fail", operation="REFRAME",
                                     elapsed_s=0.09, budget_s=45.0,
                                     fault={"kind": "refused", "status": 402})
        for outcome, el, bud, fault in _nim_shaped_traffic():
            brain_health.probe_event(GOV, "REFRAME", outcome, operation="REFRAME",
                                     elapsed_s=el, budget_s=bud, fault=fault)
    finally:
        lx.reset_llm_config(tok)
    row = _wait_for(lambda: (_profile_row(CORP) or (None,))[0] == "slow" and _profile_row(CORP))
    assert row and row[:2] == ("slow", "measured"), _profile_row(CORP)

    # Never narrows: a long run of fast calls on the (now slow) brain changes nothing.
    lc.invalidate_config_cache()
    cfg = sb.resolve_brain(GOV, CORP)
    tok = lx.bind_explicit_llm_config(cfg)
    try:
        for _ in range(120):
            brain_health.probe_event(GOV, "REFRAME", "ok", operation="REFRAME",
                                     elapsed_s=0.4, budget_s=90.0)
    finally:
        lx.reset_llm_config(tok)
    time.sleep(0.3)
    assert _profile_row(CORP)[0] == "slow"


def test_rolling_p95_never_overwrites_a_manual_profile(monkeypatch):
    from saas import brain_autocalibrate as ac
    from saas import llm_config as lc
    monkeypatch.setenv("SAAS_AUTOCALIBRATE_ON_SAVE", "0")
    _save_account(CORP, base_url="https://nim.example/v1", model="nemotron-30b")
    assert lc.save_calibration(CORP, profile="hosted-fast", source="manual")
    lc.invalidate_config_cache()
    cfg = lc.resolve_account_llm_config(CORP)
    for outcome, el, bud, fault in _nim_shaped_traffic():
        ac.observe(GOV, "REFRAME", outcome, elapsed_s=el, budget_s=bud, fault=fault, cfg=cfg)
    time.sleep(0.3)
    assert _profile_row(CORP)[:2] == ("hosted-fast", "manual")


# ══════════════════════════════════════════════════════════════════════════════════════
# 5. SEAT vs ACCOUNT — resolver and console agree
# ══════════════════════════════════════════════════════════════════════════════════════

def test_inherited_seat_resolves_the_account_profile_and_its_manual_choice_wins(monkeypatch):
    from saas import llm_config as lc
    from saas import seat_brain as sb
    from saas import tenant_console as tc
    from saas import user_console as uc
    monkeypatch.setenv("SAAS_AUTOCALIBRATE_ON_SAVE", "0")
    _save_account(CORP, base_url="https://nim.example/v1", model="nemotron-30b")
    assert lc.save_calibration(CORP, profile="slow", source="measured", p95_ms=NIM_P95_MS)

    # Granted seat with no brain of its own → the org brain AND the org's measured profile.
    cfg = sb.resolve_brain(GRANT, CORP)
    assert cfg["brain_scope"] == "account" and cfg["latency_profile"] == "slow"

    # A seat-row MEASURED value (a brain the seat no longer runs) does not apply.
    assert lc.save_seat_calibration(GRANT, CORP, profile="local", source="measured")
    assert sb.resolve_brain(GRANT, CORP)["latency_profile"] == "slow"

    # The seat's explicit MANUAL choice (PUT /me/llm-endpoint/profile) wins — resolver,
    # the seat's own console and the admin's view all say the same thing.
    ind = {"id": "i-" + GRANT[:8], "user_id": GRANT, "account_id": CORP,
           "tenant_context": "real", "can_control_endpoint": True}
    uc.me_set_profile(uc.LatencyProfileRequest(profile="local"), individual=ind)
    lc.invalidate_config_cache()
    assert sb.resolve_brain(GRANT, CORP)["latency_profile"] == "local"
    assert uc._brain_get_redacted(ind)["latency_profile"] == "local"
    assert uc._brain_get_redacted(ind)["latency_profile_source"] == "manual"
    assert tc._seat_brain_config_for_admin(CORP, GRANT)["latency_profile"] == "local"

    # A GOVERNED seat has no say: the org profile, whatever sits in a seat row.
    assert lc.save_seat_calibration(GOV, CORP, profile="hosted-fast", source="manual")
    lc.invalidate_config_cache()
    assert sb.resolve_brain(GOV, CORP)["latency_profile"] == "slow"
    assert tc._seat_brain_config_for_admin(CORP, GOV)["latency_profile"] == "slow"


def test_resolver_reframe_budget_matches_the_stored_profile(monkeypatch):
    from saas import llm_config as lc
    from saas import llm_context as lx
    from saas import seat_brain as sb
    from saas.brain_profile import budget_seconds
    from src.api.llm_calls import LLMTimeouts
    monkeypatch.setenv("SAAS_AUTOCALIBRATE_ON_SAVE", "0")
    _save_account(CORP, base_url="https://nim.example/v1", model="nemotron-30b")
    assert lc.save_measured_profile("account", CORP, profile="slow", mode="replace",
                                    base_url="https://nim.example/v1", model="nemotron-30b",
                                    p95_ms=NIM_P95_MS) == "saved"
    stored = _profile_row(CORP)[0]
    for seat in (GOV, GRANT):
        lc.invalidate_config_cache()
        cfg = sb.resolve_brain(seat, CORP)
        tok = lx.bind_explicit_llm_config(cfg)
        try:
            got = LLMTimeouts.get("REFRAME")
        finally:
            lx.reset_llm_config(tok)
        assert got == budget_seconds(stored, "REFRAME", 45.0) == 90.0, (seat, got)


def test_samples_timeouts_count_at_cutoff_and_refusals_do_not():
    from saas.brain_autocalibrate import rolling_p95, sample_ms
    assert sample_ms("ok", 1.5, 45, None) == 1500
    assert sample_ms("fail", 29.8, 30, {"kind": "timeout"}) == 30000
    assert sample_ms("fail", 60.0, 60, None) == 60000           # ran the whole budget
    assert sample_ms("fail", 0.09, 45, {"kind": "refused", "status": 402}) is None
    assert sample_ms("fail", None, 45, {"kind": "unreachable"}) is None
    assert rolling_p95([1000] * 19 + [50000]) == 1000
    assert rolling_p95([1000] * 18 + [50000] * 2) == 50000


# ══════════════════════════════════════════════════════════════════════════════════════
# 6. ROUND 2 (#74) — outage is not latency, the write is tied to the measured brain, the save
#    lane is not the click lane.
# ══════════════════════════════════════════════════════════════════════════════════════

def _hosted_fast_account(lc):
    _save_account(CORP, base_url="https://nim.example/v1", model="nemotron-30b")
    assert lc.save_calibration(CORP, profile="hosted-fast", source="measured", p95_ms=948)
    lc.invalidate_config_cache()
    return lc.resolve_account_llm_config(CORP)


def test_74_a_few_hung_calls_among_fast_answers_do_not_widen(monkeypatch):
    """47 ok at 0.9 s + 3 hung calls at the 45 s cutoff: the p95 lands on a hung call, but no
    answer ever took longer than 0.9 s — raising the budget buys nothing. Stays hosted-fast."""
    from saas import brain_autocalibrate as ac
    from saas import llm_config as lc
    monkeypatch.setenv("SAAS_AUTOCALIBRATE_ON_SAVE", "0")
    cfg = _hosted_fast_account(lc)
    scheduled = []
    for i in range(50):
        if i in (10, 25, 40):
            r = ac.observe(GOV, "REFRAME", "fail", elapsed_s=45.0, budget_s=45.0, cfg=cfg)
        else:
            r = ac.observe(GOV, "REFRAME", "ok", elapsed_s=0.9, budget_s=45.0, cfg=cfg)
        scheduled.append(r)
    time.sleep(0.3)
    assert not any(scheduled), scheduled
    assert _profile_row(CORP)[:2] == ("hosted-fast", "measured")


def test_74_a_pure_outage_never_widens(monkeypatch):
    """25 consecutive timeouts and not one answer: an availability fault, not a latency."""
    from saas import brain_autocalibrate as ac
    from saas import llm_config as lc
    monkeypatch.setenv("SAAS_AUTOCALIBRATE_ON_SAVE", "0")
    cfg = _hosted_fast_account(lc)
    for _ in range(25):
        ac.observe(GOV, "REFRAME", "fail", elapsed_s=45.0, budget_s=45.0,
                   fault={"kind": "timeout"}, cfg=cfg)
    time.sleep(0.3)
    assert _profile_row(CORP)[:2] == ("hosted-fast", "measured")


def test_74_unreachable_and_refused_are_never_latency_samples():
    from saas.brain_autocalibrate import sample_ms
    # ConnectTimeout that ran the full budget: brain_fault classifies it `unreachable`.
    assert sample_ms("fail", 44.0, 45.0, {"kind": "unreachable"}) is None
    assert sample_ms("fail", 45.0, 45.0, {"kind": "connection_lost"}) is None
    assert sample_ms("fail", 45.0, 45.0, {"kind": "refused", "status": 429}) is None
    # Unclassified (or classified `unknown`) and ran the budget → counted at the cutoff.
    assert sample_ms("fail", 45.0, 45.0, None) == 45000
    assert sample_ms("fail", 45.0, 45.0, {"kind": "unknown"}) == 45000


def test_74_m7_a_timeout_with_no_elapsed_counts_at_its_cutoff():
    """The no-retry lane reports a timeout with a budget but NO elapsed time
    (llm_calls.call_llm_no_retry_sync) — only the classified kind can place it. This is why the
    kind=='timeout' branch is not redundant with the 0.95×budget heuristic."""
    from saas.brain_autocalibrate import sample_ms
    assert sample_ms("fail", None, 30.0, {"kind": "timeout"}) == 30000
    assert sample_ms("fail", None, 30.0, None) is None


def test_74_the_owner_pattern_still_widens_to_slow():
    from saas import brain_autocalibrate as ac
    window = [(ac.sample_ms(o, el, bud, f), o == "ok")
              for o, el, bud, f in _nim_shaped_traffic()]
    window = [w for w in window if w[0] is not None]
    assert ac.window_target(window[-50:]) == "slow"


def test_74_a_late_measurement_never_lands_on_a_newer_brain(monkeypatch):
    """Measure Y; Z is saved while the probe is in flight; Y's result must not stamp Z's row."""
    import threading as _t
    from saas import llm_admin
    entered, release = _t.Event(), _t.Event()

    def _probe(account_id, cfg, *a, **k):
        entered.set()
        release.wait(20)
        return {"ok": True, "samples": 5, "p50_ms": 30000, "p95_ms": 60000, "profile": "slow"}
    monkeypatch.setattr(llm_admin, "probe_reframe_latency", _probe)
    try:
        _save_account(CORP, base_url="https://y.example/v1", model="y-model")
        assert entered.wait(10), "the save-triggered probe never started"
        monkeypatch.setenv("SAAS_AUTOCALIBRATE_ON_SAVE", "0")
        _save_account(CORP, base_url="https://z.example/v1", model="z-model")
    finally:
        release.set()
    _drain_save_lane()
    assert _profile_row(CORP)[0] is None, _profile_row(CORP)
    rows = _sql("SELECT base_url FROM public.account_llm_config WHERE account_id = %s", (CORP,))
    assert rows[0][0] == "https://z.example/v1"


def test_74_a_passive_widening_never_lands_on_a_newer_brain(monkeypatch):
    from saas import brain_autocalibrate as ac
    from saas import llm_config as lc
    monkeypatch.setenv("SAAS_AUTOCALIBRATE_ON_SAVE", "0")
    cfg_y = _hosted_fast_account(lc)                          # traffic observed on brain Y …
    _save_account(CORP, base_url="https://z.example/v1", model="z-model")   # … Z saved since
    for outcome, el, bud, fault in _nim_shaped_traffic():
        ac.observe(GOV, "REFRAME", outcome, elapsed_s=el, budget_s=bud, fault=fault, cfg=cfg_y)
    time.sleep(0.5)
    assert _profile_row(CORP)[:2] == ("hosted-fast", "measured")
    assert lc.save_measured_profile("account", CORP, profile="slow", mode="widen",
                                    base_url="https://nim.example/v1",
                                    model="nemotron-30b") == "brain_changed"


def test_74_a_slow_brain_still_calibrates_on_save(monkeypatch):
    """The save lane is not the click lane: a brain whose probe outlasts the click deadline
    still gets its profile (the probe keeps its own 75 s budget)."""
    from saas import llm_admin
    from saas import user_console as uc
    monkeypatch.setattr(uc, "_CALIBRATION_DEADLINE_S", 1.0)   # stand-in for 45 s

    def _slow_probe(account_id, cfg, *a, **k):
        time.sleep(2.0)                                       # stand-in for > 45 s
        return {"ok": True, "samples": 5, "p50_ms": 11000, "p95_ms": 52000, "profile": "slow"}
    monkeypatch.setattr(llm_admin, "probe_reframe_latency", _slow_probe)
    _save_account(CORP, base_url="https://slow.example/v1", model="big-local")
    _drain_save_lane()
    assert _profile_row(CORP)[:3] == ("slow", "measured", 52000)


def test_74_saves_against_a_hung_endpoint_do_not_starve_another_tenants_test(monkeypatch):
    import threading as _t
    from saas import brain_autocalibrate as ac
    from saas import llm_admin
    from saas import llm_config as lc
    from saas import user_console as uc
    monkeypatch.setattr(uc, "_CALIBRATION_DEADLINE_S", 2.0)
    release = _t.Event()
    hung = []

    def _probe(account_id, cfg, *a, **k):
        if "hang.example" in str(cfg.get("base_url")):
            hung.append(1)
            release.wait(60)
            return {"ok": False, "samples": 0}
        return {"ok": True, "samples": 5, "p50_ms": 5000, "p95_ms": 9000, "profile": "local"}
    monkeypatch.setattr(llm_admin, "probe_reframe_latency", _probe)
    try:
        for i in range(5):
            _save_account(CORP, base_url=f"https://hang.example/v{i}", model="dead")
        # Give a pool-saturating implementation every chance to fill the shared click pool.
        _wait_for(lambda: len(hung) >= 4, timeout=10)
        monkeypatch.setenv("SAAS_AUTOCALIBRATE_ON_SAVE", "0")
        _save_account(OTHER, base_url="https://other.example/v1", model="ok-model")
        lc.invalidate_config_cache()
        cfg = lc.resolve_account_llm_config(OTHER)
        t0 = time.monotonic()
        out = ac.calibrate_account_now(OTHER, cfg, {"ok": True})
        assert out.get("saved") is True, (out, len(hung))
        assert time.monotonic() - t0 < 2.0
    finally:
        release.set()
    _drain_save_lane()


def test_74_owner_row_shape_seat_measured_local_resolves_the_account_slow(monkeypatch):
    """Verified read-only on the owner's rows: account slow/measured/44160; seat
    local/measured/12068 from 2026-08-13 (a measurement, not a manual pick). The seat runs the
    account's slow profile — in the resolver, the REFRAME budget and both consoles."""
    from saas import llm_config as lc
    from saas import llm_context as lx
    from saas import seat_brain as sb
    from saas import tenant_console as tc
    from saas import user_console as uc
    from src.api.llm_calls import LLMTimeouts
    monkeypatch.setenv("SAAS_AUTOCALIBRATE_ON_SAVE", "0")
    _save_account(CORP, base_url="https://nim.example/v1", model="nemotron-30b")
    _sql("UPDATE public.account_llm_config SET latency_profile='slow', "
         "latency_profile_source='measured', latency_p95_ms=44160 WHERE account_id=%s", (CORP,))
    for seat in (GRANT, GOV):
        _sql("INSERT INTO public.seat_llm_config (user_id, account_id, latency_profile, "
             "latency_profile_source, latency_p95_ms, latency_measured_at) VALUES "
             "(%s,%s,'local','measured',12068,'2026-08-13T00:00:00Z')", (seat, CORP))
    for seat in (GRANT, GOV):
        lc.invalidate_config_cache()
        cfg = sb.resolve_brain(seat, CORP)
        assert cfg["latency_profile"] == "slow", (seat, cfg.get("latency_profile"))
        tok = lx.bind_explicit_llm_config(cfg)
        try:
            assert LLMTimeouts.get("REFRAME") == 90.0
        finally:
            lx.reset_llm_config(tok)
        assert tc._seat_brain_config_for_admin(CORP, seat)["latency_profile"] == "slow"
    ind = {"id": "i", "user_id": GRANT, "account_id": CORP, "tenant_context": "real",
           "can_control_endpoint": True}
    assert uc._brain_get_redacted(ind)["latency_profile"] == "slow"
