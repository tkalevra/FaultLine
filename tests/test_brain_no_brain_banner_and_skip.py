"""NOBRAIN — "no endpoint connected" is a SETUP state, not a failure, and skipping is not dropping.

MEASURED MOTIVATION (prod, 2026-08-01): 15 accounts, 6 rows in ``account_llm_config`` — 9
accounts have NO brain bound. Their LLM lane fails closed correctly (zero spend), and that
refusal was teed into the health stream as an OUTCOME_FAIL, so all nine read as an endpoint
FAILURE and were told to "check that <host> is reachable" about a host that does not exist.

WHAT THIS PINS, and why each one is a bug that has actually bitten or would:

1. NOT-CONFIGURED ≠ FAILING. Different state, different copy, different help page. The
   discriminator is the probe ``reason``, which the fail-closed short-circuit already stamps —
   not a heuristic.
2. NO_BRAIN OUTRANKS FLAPPING. Removing a bound brain produces ok/fail/ok/fail, which reads as
   "intermittent" and would send someone to the firewall page over a config change.
3. THE GOVERNED SEAT IS NEVER POINTED AT A CONTROL IT CANNOT USE. A ``tenant_context='real'``
   seat without ``can_control_endpoint`` has the LLM Brain tab HIDDEN (``user.js``
   ``applyBrainGate``); a button opening it would name a control that is not on screen.
4. IT NEVER BLOCKS. Every state carries ``blocking: False`` and a healthy seat gets ``None`` —
   a banner that is always present is a banner nobody reads.
5. NO CREDENTIAL EVER REACHES THE BANNER, including a provider that carries the key in the URL.
6. SAAS OFF IS BYTE-FOR-BYTE UNCHANGED: the new ``assess`` arguments default to "caller did not
   say", and the skip gate never skips.
7. THE BANNER IS NOT AN OUTAGE RISK: every entry point swallows its own errors.

PURE except where noted: ``banner``, ``classify_probe_state``, ``worst_state``, ``setup_action``
and ``seat_is_governed`` take no database. ``awaiting_brain`` is exercised only for its
fail-safe path (no schema resolvable → zeros), which needs no DB either.
"""

import importlib
import os

import pytest

# Same import discipline as tests/test_brain_probe_alarm_stream.py: SAAS_MODE is set ONLY across
# the import and restored exactly as found. A bare setdefault here leaks process-wide and
# produces false reds in every SaaS-sensitive file collected afterwards.
_prev_saas_mode = os.environ.get("SAAS_MODE")
os.environ["SAAS_MODE"] = _prev_saas_mode or "1"
try:
    bh = importlib.import_module("saas.brain_health")
    sb = importlib.import_module("saas.seat_brain")
finally:
    if _prev_saas_mode is None:
        os.environ.pop("SAAS_MODE", None)
    else:
        os.environ["SAAS_MODE"] = _prev_saas_mode

OK, FAIL = bh.OUTCOME_OK, bh.OUTCOME_FAIL


# ── 1: not-configured is its own state, decided by the reason the LLM stack stamped ───

def test_no_brain_reason_produces_its_own_state_not_fail():
    """A fail whose reason is the fail-closed refusal is NO_BRAIN, not FAIL."""
    assert bh.classify_probe_state([OK, FAIL], FAIL, bh.REASON_NO_BRAIN) == bh.STATE_NO_BRAIN
    # A fail with any other reason stays FAIL — this must not swallow genuine failures.
    assert bh.classify_probe_state([OK, FAIL], FAIL, "timeout") == bh.STATE_FAIL
    assert bh.classify_probe_state([OK, FAIL], FAIL, None) == bh.STATE_FAIL


def test_reason_is_the_wire_value_llm_calls_actually_stamps():
    """⚠️ The whole state hinges on this string matching `llm_calls._health_note`'s outcome
    table key. If that mapping is renamed and this constant is not, the state silently
    disappears and every unbound seat quietly reads FAIL again."""
    assert bh.REASON_NO_BRAIN == "no_brain"
    assert bh.M_NO_BRAIN == "no_brain"


def test_a_success_still_clears_a_no_brain_state():
    """Binding a brain must clear the state with no TTL and no sweep — the ok side of the
    last-write-wins stream is what makes that work."""
    assert bh.classify_probe_state([FAIL, FAIL, OK], OK, bh.REASON_NO_BRAIN) == bh.STATE_OK


# ── 2: precedence ─────────────────────────────────────────────────────────────────────

def test_no_brain_outranks_flapping():
    """Unbinding a brain looks exactly like a flap (ok/fail/ok/fail). Reporting "intermittent"
    would send somebody to the rate-limit/firewall page over a configuration change."""
    seq = [OK, FAIL, OK, FAIL, FAIL]
    assert bh.classify_probe_state(seq, FAIL) == bh.STATE_FLAPPING          # without the reason
    assert bh.classify_probe_state(seq, FAIL, bh.REASON_NO_BRAIN) == bh.STATE_NO_BRAIN


def test_worst_state_ranks_no_brain_above_everything():
    """Every other lane's failure is DOWNSTREAM of having no endpoint at all."""
    assert bh.worst_state({
        "REFRAME": {"state": bh.STATE_NO_BRAIN},
        "TURN": {"state": bh.STATE_FLAPPING},
        "EXTRACT": {"state": bh.STATE_FAIL},
    }) == bh.STATE_NO_BRAIN
    # ...and it does not disturb the existing order when it is absent.
    assert bh.worst_state({"a": {"state": bh.STATE_FLAPPING},
                           "b": {"state": bh.STATE_FAIL}}) == bh.STATE_FLAPPING
    assert bh.worst_state({"a": {"state": bh.STATE_OK}}) == bh.STATE_OK


def test_classify_probe_state_default_reason_is_byte_for_byte_legacy():
    """Every pre-existing caller passes two arguments. Their behaviour must not move."""
    for seq, last, expect in [
        ([OK], OK, bh.STATE_OK),
        ([FAIL, FAIL], FAIL, bh.STATE_FAIL),
        ([OK, FAIL, OK, FAIL], FAIL, bh.STATE_FLAPPING),
        ([], None, bh.STATE_UNKNOWN),
    ]:
        assert bh.classify_probe_state(seq, last) == expect
        assert bh.classify_probe_state(seq, last, None) == expect


# ── 3: the governed seat is pointed at a HUMAN, not at a hidden tab ───────────────────

def test_governed_seat_is_never_sent_to_the_brain_tab():
    """⚠️ THE TRAP THIS EXISTS FOR. `applyBrainGate` HIDES the LLM Brain tab for a governed
    seat. A CTA opening it would name a control that is not on their screen and cannot be made
    to appear, which reads as a broken product rather than a governance decision."""
    b = bh.banner(configured=False, tenant_context="real", can_control_endpoint=False)
    assert b["kind"] == "no_brain_governed"
    assert b["cta_target"] == bh.CTA_HELP
    assert b["cta_target"] != bh.CTA_BRAIN_TAB
    assert "administrator" in b["action"]
    # ...and it links the wiki section that explains the governance rule, not a fix-it page.
    assert b["help"].endswith(bh.HELP_GOVERNED_SEAT)


def test_managed_seat_names_a_resolved_addressee_when_there_is_one():
    """Two product TIERS, both first-class. The managed one is not a locked door: it says what
    is happening, whose setting it is, and WHO to ask — resolved from the account's own admin
    login, never invented."""
    b = bh.banner(configured=False, tenant_context="real", can_control_endpoint=False,
                  admin_contact="ops@acme.com")
    assert "ops@acme.com" in b["action"]
    assert b["admin_contact"] == "ops@acme.com"
    assert "tenant console" in b["action"]


def test_managed_seat_without_a_derivable_admin_names_the_DESTINATION_not_a_dead_end():
    """⚠️ A "contact your administrator" with nobody on the other end is worse than an honest
    statement. With no addressee the line still has a definite home — the organization's tenant
    console — which is where the fix actually happens."""
    b = bh.banner(configured=False, tenant_context="real", can_control_endpoint=False)
    assert b["admin_contact"] is None
    assert "tenant console" in b["action"]
    assert "()" not in b["action"]              # no empty placeholder
    assert "None" not in b["action"]


def test_managed_seat_copy_does_not_read_as_a_fault_of_theirs_or_ours():
    """TONE is on the record and load-bearing: never a value judgement, and a governance choice
    must not read as a broken product."""
    b = bh.banner(configured=False, tenant_context="real", can_control_endpoint=False)
    blob = (b["title"] + " " + b["body"]).lower()
    assert "nothing is wrong with your seat" in blob
    for forbidden in ("error", "failed", "unsupported", "not allowed", "denied", "restricted"):
        assert forbidden not in blob, forbidden


def test_admin_action_lands_on_the_control_that_actually_binds_the_brain():
    """The admin console is THE RESOLUTION POINT, so its instruction must name the real control
    rather than a generic settings page."""
    act = bh.setup_action(admin=True)
    assert "LLM Brain" in act
    assert "Test & calibrate" in act


def test_a_granted_real_seat_and_every_self_owned_seat_gets_the_control():
    """Only `real` WITHOUT the grant is governed — mirroring `_require_endpoint_control` and
    `user.js canControlEndpoint()` exactly."""
    for ctx, grant in [("real", True), ("house", False), ("none", False), (None, False)]:
        b = bh.banner(configured=False, tenant_context=ctx, can_control_endpoint=grant)
        assert b["kind"] == "no_brain", (ctx, grant)
        assert b["cta_target"] == bh.CTA_BRAIN_TAB, (ctx, grant)
    assert bh.seat_is_governed("real", False) is True
    assert bh.seat_is_governed("real", True) is False
    assert bh.seat_is_governed("house", False) is False
    assert bh.seat_is_governed(None, None) is False


# ── 4: prominent, but never blocking; and silent when there is nothing to say ─────────

def test_healthy_configured_seat_gets_no_banner_at_all():
    """Silence is the correct output. A banner that is always present stops being read."""
    assert bh.banner(configured=True, state=bh.STATE_OK) is None
    assert bh.banner(configured=True, state=bh.STATE_UNKNOWN) is None
    assert bh.banner(configured=True, state=None) is None


@pytest.mark.parametrize("kwargs", [
    dict(configured=False, tenant_context="house", can_control_endpoint=True),
    dict(configured=False, tenant_context="real", can_control_endpoint=False),
    dict(configured=True, state=bh.STATE_FAIL),
    dict(configured=True, state=bh.STATE_FLAPPING),
])
def test_no_state_ever_blocks_and_every_state_is_actionable(kwargs):
    b = bh.banner(**kwargs)
    assert b is not None
    assert b["blocking"] is False          # informational, never a lockout
    assert b["title"] and b["body"] and b["action"]
    assert b["help"].startswith(bh.HELP_BASE)
    assert b["severity"] in (bh.BANNER_SETUP, bh.BANNER_WARN)


def test_setup_states_are_not_coloured_as_faults():
    """A setup step rendered in the fault colour both misdescribes it and desensitises the
    colour that means something IS wrong."""
    for ctx, grant in [("house", True), ("real", False)]:
        assert bh.banner(configured=False, tenant_context=ctx,
                         can_control_endpoint=grant)["severity"] == bh.BANNER_SETUP
    for st in (bh.STATE_FAIL, bh.STATE_FLAPPING):
        assert bh.banner(configured=True, state=st)["severity"] == bh.BANNER_WARN


def test_setup_copy_never_tells_anyone_to_check_a_host_they_never_entered():
    """The FAIL remedy is reachability/firewall. Applying it to a seat with no endpoint sends
    them to troubleshoot a thing that does not exist — the exact defect this closes."""
    for ctx, grant in [("house", True), ("real", False)]:
        b = bh.banner(configured=False, tenant_context=ctx, can_control_endpoint=grant)
        blob = (b["title"] + " " + b["body"] + " " + b["action"]).lower()
        for forbidden in ("firewall", "reachable", "not answering", "unreachable"):
            assert forbidden not in blob, forbidden
        # ...and it says the two reassuring things that are actually true.
        assert "recall works normally" in blob
        assert "storing" in blob


# ── 5: no credential can reach the banner ────────────────────────────────────────────

def test_a_key_carried_in_the_endpoint_url_never_reaches_the_banner():
    """Some providers put the key in the query string or a path segment. Only HOST[:PORT] is
    ever rendered."""
    url = "https://u:p@api.example.net:8443/v1/deployments/x?api-key=SUPERSECRET"
    for st in (bh.STATE_FAIL, bh.STATE_FLAPPING):
        b = bh.banner(configured=True, state=st, base_url=url)
        blob = " ".join(str(v) for v in b.values())
        assert "SUPERSECRET" not in blob
        assert "api-key" not in blob
        assert "u:p" not in blob
        assert "api.example.net:8443" in blob


# ── 6: what the skip cost — countable, and honest about what it promises ─────────────

def test_awaiting_counts_are_surfaced_and_read_naturally():
    b = bh.banner(configured=False, tenant_context="house", can_control_endpoint=True,
                  awaiting={"turns": 47, "documents": 2})
    assert b["awaiting"]["turns"] == 47 and b["awaiting"]["documents"] == 2
    assert "47 conversation turns and 2 documents" in b["body"]
    assert "re-read automatically" in b["body"]
    # Singular/plural, because "1 turns are held" reads as a bug in the product.
    one = bh.banner(configured=False, tenant_context="house", can_control_endpoint=True,
                    awaiting={"turns": 1, "documents": 0})
    assert "1 conversation turn is held" in one["body"]


def test_the_promise_is_re_read_not_restored_to_the_same_tier():
    """⚠️ DELIBERATE OMISSION. Re-mined edges ingest with source="reextract" → llm_inferred, so
    a fact the user STATED comes back Class B/C, not A. The copy therefore says "re-read",
    which is exactly what happens, and claims nothing about the tier. If someone strengthens
    this to "restored"/"recovered in full", this test is the objection."""
    b = bh.banner(configured=False, tenant_context="house", can_control_endpoint=True,
                  awaiting={"turns": 5, "documents": 0})
    body = b["body"].lower()
    assert "re-read" in body
    for overclaim in ("restored", "recovered in full", "exactly as", "unchanged"):
        assert overclaim not in body


def test_no_backlog_means_no_backlog_sentence():
    b = bh.banner(configured=False, tenant_context="house", can_control_endpoint=True,
                  awaiting={"turns": 0, "documents": 0})
    assert "held verbatim" not in b["body"]
    assert b["awaiting"]["turns"] == 0


# ── 7: the SETUP band on the health card fires from CONFIG, not from traffic ──────────

def test_setup_band_reaches_a_brand_new_seat_with_no_telemetry_at_all():
    """⚠️ THE POPULATION THIS IS FOR HAS NO METRICS. The pre-existing "no endpoint bound" red
    band requires `no_brain > 0`, i.e. the seat must already have been refused at least once.
    A brand-new account got "No health data yet" — true, useless, and hiding the one thing
    they have to do."""
    v = bh.assess({"available": False}, None, 6.0, brain_configured=False)
    assert v["band"] == "setup"
    assert "No model endpoint is connected yet." == v["headline"]
    assert "LLM Brain tab" in v["action"]


def test_setup_band_addresses_the_right_audience():
    governed = bh.assess({"available": False}, None, 6.0, brain_configured=False, governed=True)
    assert "administrator" in governed["action"]
    assert "LLM Brain tab" not in governed["action"]
    admin = bh.assess({"available": False}, None, 6.0, brain_configured=False, admin=True)
    assert "this seat" in admin["action"]


def test_assess_without_the_new_arguments_is_byte_for_byte_legacy():
    """`brain_configured=None` means "the caller did not tell us" → the setup branch is skipped
    entirely and every pre-existing call site behaves exactly as before."""
    metrics = {"available": False}
    assert bh.assess(metrics, None, 6.0)["band"] == "unknown"
    assert bh.assess(metrics, None, 6.0, brain_configured=None)["band"] == "unknown"
    green = {"available": True, "window_hours": 24, "llm_calls": 100, "llm_call_failures": 0,
             "llm_failure_rate": 0.0, "breaker_open_events": 0, "no_brain_events": 0,
             "reframe_invocations": 50, "reframe_fallbacks": 0, "atomizer_fallback_rate": 0.0,
             "sacred_token_drops": 0, "sacred_token_drop_rate": 0.0,
             "latency_by_operation": {}, "active_signals": []}
    assert bh.assess(green, "hosted-fast", 6.0)["band"] == "green"
    # A CONFIGURED seat is never diverted into the setup band, whatever else is true.
    assert bh.assess(green, "hosted-fast", 6.0, brain_configured=True)["band"] == "green"


# ── 8: the model-visible notice says the setup thing, not the firewall thing ──────────

def test_degradation_notice_help_target_is_setup_for_no_brain():
    assert bh.help_for_state(bh.STATE_NO_BRAIN) == bh.help_link(bh.HELP_NO_BRAIN)
    assert bh.help_for_state(bh.STATE_FAIL) == bh.help_link(bh.HELP_ENDPOINT_UNREACHABLE)
    assert bh.help_for_state(bh.STATE_OK) is None


def test_help_targets_exist_in_the_shipped_wiki():
    """A broken help link on a message somebody is reading mid-problem is salt in the wound.
    Asserted against the real file, exactly as the sibling suite does for the other states."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "saas", "dashboard", "help", "index.html")
    if not os.path.exists(path):
        pytest.skip("wiki not present in this checkout")
    html = open(path, encoding="utf-8").read()
    for target in (bh.HELP_NO_BRAIN, bh.HELP_GOVERNED_SEAT):
        page, _, anchor = target.partition(":")
        assert f'id="{page}"' in html, page
        if anchor:
            assert f'id="{anchor}"' in html, anchor


# ── 9: the skip gate — fail direction and the SAAS-off contract ───────────────────────

def test_skip_gate_never_skips_when_saas_is_off(monkeypatch):
    """SAAS off has no notion of a per-tenant brain. Skipping there would silence the whole
    open-core deployment."""
    monkeypatch.setattr(sb, "saas_mode_enabled", lambda: False)
    assert sb.llm_lane_unavailable() is False
    assert sb.brain_bound("some-user", "some-account") is True


def test_skip_gate_fails_towards_NOT_skipping(monkeypatch):
    """⚠️ FAIL DIRECTION. Skipping is the action that STOPS work, so an unreadable control
    plane must never be able to silence a healthy tenant's background drain. The guard against
    the loss case is layered and does not depend on this read succeeding."""
    monkeypatch.setattr(sb, "saas_mode_enabled", lambda: True)

    def _boom():
        raise RuntimeError("control plane down")
    import saas.llm_context as lc
    monkeypatch.setattr(lc, "current_llm_config", _boom)
    assert sb.llm_lane_unavailable() is False


def test_skip_gate_recognises_the_sentinel_and_a_lost_context(monkeypatch):
    monkeypatch.setattr(sb, "saas_mode_enabled", lambda: True)
    import saas.llm_context as lc
    monkeypatch.setattr(lc, "current_llm_config", lambda: {"no_brain": True, "account_id": "a"})
    assert sb.llm_lane_unavailable() is True
    monkeypatch.setattr(lc, "current_llm_config", lambda: None)
    assert sb.llm_lane_unavailable() is True          # tenant context lost → also skip
    monkeypatch.setattr(lc, "current_llm_config",
                        lambda: {"account_id": "a", "base_url": "http://x", "model": "m"})
    assert sb.llm_lane_unavailable() is False


def test_brain_bound_says_nothing_rather_than_accusing_a_working_seat(monkeypatch):
    """A resolver error must not light "you have no endpoint" on a seat whose endpoint is fine."""
    monkeypatch.setattr(sb, "saas_mode_enabled", lambda: True)

    def _boom(_u, _a):
        raise RuntimeError("db down")
    monkeypatch.setattr(sb, "resolve_brain", _boom)
    assert sb.brain_bound("u", "a") is True


# ── 10: telemetry must never become an outage ─────────────────────────────────────────

def test_banner_swallows_its_own_errors():
    """A monitoring bug must not break the console it lives on."""
    class Exploding(dict):
        def get(self, *_a, **_k):
            raise RuntimeError("boom")
    assert bh.banner(configured=False, awaiting=Exploding()) is None


def test_awaiting_brain_is_fail_safe_without_a_resolvable_schema():
    out = bh.awaiting_brain("")
    assert out["turns"] == 0 and out["documents"] == 0 and out["available"] is False


# ── 11: the ADMIN's view — the only principal who can act on a managed seat ───────────

def test_admin_setup_action_is_distinct_from_both_seat_tiers():
    """The same state renders by WHO CAN ACT. Three renderings, three destinations, and no two
    of them may collapse into each other."""
    self_owned = bh.setup_action()
    managed = bh.setup_action(governed=True)
    admin = bh.setup_action(admin=True)
    assert len({self_owned, managed, admin}) == 3
    # ...and each names the control/principal that is real FOR THAT VIEWER.
    assert "LLM Brain tab" in self_owned          # theirs, in this session
    assert "tenant console" in managed            # not theirs — the admin's, with a home
    assert "your console" in admin                # the resolution point


def test_admin_precedence_over_governed_when_both_flags_are_passed():
    """An admin reading a managed seat's card is still an ADMIN. If `governed` won, the console
    that FIXES this would tell its own operator to go ask somebody else."""
    assert bh.setup_action(governed=True, admin=True) == bh.setup_action(admin=True)


def test_no_copy_anywhere_promises_per_seat_failure_isolation():
    """⚠️ CONFIRMED AND DELIBERATE. The circuit-breaker grain is per-ACCOUNT today while
    `seat_brain.resolve_brain` is already per-SEAT for house accounts, so one seat's dead
    endpoint CAN open the breaker for a co-tenant whose own endpoint is healthy. That is
    flagged for the owner, not built — so nothing here may claim otherwise."""
    blobs = []
    for kw in (dict(configured=False, tenant_context="house", can_control_endpoint=True),
               dict(configured=False, tenant_context="real", can_control_endpoint=False),
               dict(configured=True, state=bh.STATE_FAIL),
               dict(configured=True, state=bh.STATE_FLAPPING)):
        b = bh.banner(**kw)
        blobs.append(" ".join(str(v) for v in b.values()).lower())
    blobs.append(bh.setup_action().lower())
    blobs.append(bh.setup_action(governed=True).lower())
    blobs.append(bh.setup_action(admin=True).lower())
    for blob in blobs:
        for overclaim in ("only your seat", "only this seat", "other seats are unaffected",
                          "does not affect other", "isolated to your seat"):
            assert overclaim not in blob, overclaim
