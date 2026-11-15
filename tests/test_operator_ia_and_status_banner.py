"""OPERATOR-CONSOLE IA RESTRUCTURE + THE PINNED ACCOUNT-STATUS BANNER.

The owner's instruction, verbatim, is the specification these tests pin:

  "Upon account suspension for non-payment, actually for each Tenant, show a static banner
   at the top 'pinned' stating the account status, ie. OK 'green', suspended.... include
   verbiage on the far right corner. I want operational actions, and customer actions to be
   entirely separated, placing only tenant actions on left side, and operational on the
   right - anything not Tenant related. 'TENANT' menu. The operators account actions should
   be in the bottom, as a pop-up menu up from the bottom corner"

FOUR KINDS OF EVIDENCE, kept separate on purpose:

  PURE      ``account_status_banner.decide()`` is a pure facts→key function, so the whole
            precedence table is exercised with no database at all. A precedence change is a
            one-line diff here.

  DB        The resolver's SQL is EXECUTED against a real migrated control plane. This is
            the lesson from the billing-dashboard round: a suite built on dict fixtures
            stayed green for the entire life of a view that errored on every load, because
            no test ever ran the query. ONE test drives EVERY reader, deliberately, so a
            PARTIAL repair still fails. Skipped only when no DSN is supplied — and the
            ``_DB_RAN`` guard at the bottom turns a silent all-skip into a RED.

  RENDERED  ``tests/dashboard/render_operator_console.js`` runs the REAL index.html plus the
            real app.js / status_banner.js / tour.js in jsdom and drives the real router,
            the real popup wiring and the real banner renderer. It proves which element
            lives in which rail, what the popup does on Escape, and what the banner prints
            for each severity. jsdom does not lay out and does not apply CSS.

  STATIC    Route wiring, the per-face asset allowlist and the CSP counts, asserted against
            the real source with a real HTML parser (never a regex — a regex counts the
            word "script" inside a comment).

┌─ RUNNING IT ───────────────────────────────────────────────────────────────────────┐
│   npm install --prefix tests/dashboard        # once, for the RENDERED half         │
│   POSTGRES_DSN=postgresql://faultline:faultline@172.20.0.3:5432/faultline_opsia_test│
│   python3 tools/fltest.py --bug OPSIA --test tests/test_operator_ia_and_status_banner.py │
│ The DB is built from migrations >= 131.                                             │
└────────────────────────────────────────────────────────────────────────────────────┘
"""
import html.parser
import inspect
import itertools
import json
import os
import pathlib
import shutil
import subprocess
import uuid

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DASH = ROOT / "saas" / "dashboard"
HARNESS = ROOT / "tests" / "dashboard" / "render_operator_console.js"

INDEX = (DASH / "index.html").read_text(encoding="utf-8")
TENANT_HTML = (DASH / "tenant.html").read_text(encoding="utf-8")
APP_JS = (DASH / "app.js").read_text(encoding="utf-8")
TENANT_JS = (DASH / "tenant.js").read_text(encoding="utf-8")
BANNER_JS = (DASH / "status_banner.js").read_text(encoding="utf-8")

# ══════════════════════════════════════════════════════════════════════════════════════
# THE ACTION INVENTORY — "prove nothing became unreachable"
#
# This is a RE-ORGANISATION, not a re-scope. Every view that was reachable from the nav
# before must still be reachable after, from exactly one of the three homes. The sets below
# are the BEFORE inventory, transcribed from the pre-restructure markup, partitioned into
# the three AFTER homes. The union is asserted equal to what the live page actually offers,
# in BOTH directions — so this fails if anything is dropped AND if anything appears that
# nobody accounted for.
# ══════════════════════════════════════════════════════════════════════════════════════
LEFT_TENANT_VIEWS = {
    # "Tenants" section — an account and its lifecycle.
    "clients", "newclient", "subscribers", "moderation", "transfers", "trash",
    # "Selected tenant" section — the seven views of the tenant currently open.
    "overview", "plan", "admins", "users", "billing", "logs", "danger",
}
RIGHT_OPERATIONAL_VIEWS = {
    # Money. ⚠️ "stripe-billing" was RENAMED to "provider-config" (owner, 2026-08-17): a
    # destination named after ONE processor is a lie the moment a second one is selectable, and
    # the catalog has offered five for a while. Same destination, same panel, provider-agnostic
    # key; the visible LABEL is resolved at render time from the selected processor. Recorded as
    # a rename rather than a drop, because nothing became unreachable.
    "billing-overview", "provider-config", "payments",
    "reset-approvals", "signup-approvals", "tenant-signups",     # Approvals
    "platform", "security", "smtp", "retention",                 # Platform
    # ⚠️ "engine-health" was ALREADY on the live right rail at bf209cdb and was missing from this
    # inventory, so these two tests were RED before this branch existed (verified at bf209cdb:
    # {'unaccounted': ['engine-health']}). Accounted for now — it is an operational view of our
    # OWN internal model brains, with no tenant scope, which is exactly this rail's remit.
    "engine-health",
    "operators", "broadcasts", "bench-report",                   # Operators & reports
}
OPMENU_VIEWS = {"account"}                                       # the operator's own identity

ALL_VIEWS_BEFORE = LEFT_TENANT_VIEWS | RIGHT_OPERATIONAL_VIEWS | OPMENU_VIEWS


# ══════════════════════════════════════════════════════════════════════════════════════
# PURE — the precedence table, no database
# ══════════════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def banner_mod():
    import saas.account_status_banner as m
    return m


def _facts(status="active", *, found=True, lifecycle_error=False,
           comp_active=False, comp_flag=None, external=False,
           agent_seats=0, frozen=0, outstanding=False, present=True,
           serve_error=False, billing_error=False, commitment_error=False):
    """Build the fact bundle ``decide()`` consumes. Mirrors the reader shapes exactly."""
    return {
        "lifecycle": {"status": status, "found": found, "error": lifecycle_error,
                      "lock_origin": None, "name": "Acme", "slug": "acme"},
        "serve": {"applicable": agent_seats > 0, "agent_seats": agent_seats,
                  "frozen": frozen, "all_frozen": agent_seats > 0 and frozen == agent_seats,
                  "error": serve_error},
        "billing": {
            "comp": {"comped": comp_flag if comp_flag is not None else comp_active,
                     "active": comp_active, "reason": "internal", "by": "op",
                     "at": None, "until": None, "expired": False},
            "external": {"externally_billed": external, "active": external,
                         "processor": "stripe", "reference": None, "at": None, "source": None},
            "error": billing_error,
        },
        "commitment": {"present": present, "outstanding": outstanding, "state": "invoiced",
                       "amount_cents": 150000, "currency": "CAD", "months_remaining": 1,
                       "grace_until": "2026-09-11T00:00:00+00:00", "observed_only": False,
                       "error": commitment_error},
    }


@pytest.mark.parametrize("kwargs,expected", [
    # Lifecycle outranks everything — a suspended account that is ALSO comped, externally
    # billed, frozen and in arrears is still, first and foremost, SUSPENDED.
    (dict(status="archived", comp_active=True, external=True, outstanding=True,
          agent_seats=2, frozen=2), "archived"),
    (dict(status="locked", comp_active=True, outstanding=True), "locked"),
    (dict(status="suspended", comp_active=True, external=True, outstanding=True),
     "suspended"),
    # Below lifecycle: obligation ▸ service ▸ billing arrangement.
    (dict(outstanding=True, agent_seats=2, frozen=2, comp_active=True, external=True),
     "commitment_broken"),
    (dict(agent_seats=2, frozen=2, comp_active=True, external=True), "serve"),
    (dict(comp_active=True, external=True), "comp"),
    (dict(external=True), "external"),
    (dict(), "active"),
    # A PARTIALLY frozen fleet is NOT serve mode — some seats still capture.
    (dict(agent_seats=3, frozen=1), "active"),
    # No Agent seats at all → the train/serve axis does not exist for this account.
    (dict(agent_seats=0, frozen=0), "active"),
    # An EXPIRED comp (flag set, not honoured by billing) must not headline.
    (dict(comp_active=False, comp_flag=True), "active"),
])
def test_the_precedence_order_is_exactly_as_documented(banner_mod, kwargs, expected):
    assert banner_mod.decide(_facts(**kwargs)) == expected


@pytest.mark.parametrize("kwargs", [
    dict(lifecycle_error=True),                        # the read blew up
    dict(found=False),                                 # no such account
    dict(status="deprovisioning"),                     # an enum value we have never seen
    dict(status=None),
])
def test_an_unresolvable_account_is_never_reported_as_ok(banner_mod, kwargs):
    """🔴 THE FAILURE MODE THIS WHOLE MODULE EXISTS TO REFUSE: a green OK painted over an
    account nobody could actually read. 'unknown' outranks 'active' in the precedence."""
    key = banner_mod.decide(_facts(**kwargs))
    assert key == "unknown"
    assert banner_mod._SEVERITY[key] == "unknown"
    for audience in banner_mod.AUDIENCES:
        env = banner_mod._envelope("a", key, audience, [], degraded=True)
        assert env["severity"] != "ok"
        assert "OK" != env["label"]


# ── THE EXHAUSTIVE SWEEP ─────────────────────────────────────────────────────────────
# 🔴 WHY A SWEEP AND NOT MORE PARAMETRIZED CASES. ``decide()`` is a PURE function over four
# small axes, so the whole decision table fits in 700 combinations and costs milliseconds.
# The hand-written cases above encode what somebody THOUGHT to check; the sweep encodes a
# PROPERTY, and it is the sweep that found the defect this block exists to pin — 24 of the
# 700 returned severity 'ok' while a reader had failed, because the escalation only covered
# the LIFECYCLE reader. Reading the code did not show it. The module docstring denied it.

#: The four axes, each spanning every distinguishable shape its reader can produce —
#: including its OWN failure. Sizes: 7 × 5 × 5 × 4 = 700.
_SWEEP_LIFECYCLE = (
    dict(status="active"),
    dict(status="suspended"),
    dict(status="locked"),
    dict(status="archived"),
    dict(status="deprovisioning"),        # an enum value this module has never seen
    dict(found=False),                    # no such account
    dict(lifecycle_error=True),           # the read itself failed
)
_SWEEP_SERVE = (
    dict(agent_seats=0, frozen=0),        # axis not applicable
    dict(agent_seats=3, frozen=0),        # all capturing (train)
    dict(agent_seats=3, frozen=1),        # partial — NOT serve mode
    dict(agent_seats=3, frozen=3),        # serve mode
    dict(serve_error=True),               # the read failed
)
_SWEEP_BILLING = (
    dict(),
    dict(comp_active=True),
    dict(external=True),
    dict(comp_active=True, external=True),
    dict(billing_error=True),             # the read failed
)
_SWEEP_COMMITMENT = (
    dict(present=False),                  # surface absent on this deployment
    dict(present=True, outstanding=False),
    dict(present=True, outstanding=True),
    dict(commitment_error=True),          # the read failed
)


def _sweep_cases():
    for parts in itertools.product(_SWEEP_LIFECYCLE, _SWEEP_SERVE,
                                   _SWEEP_BILLING, _SWEEP_COMMITMENT):
        kw = {}
        for p in parts:
            kw.update(p)
        yield kw


def test_the_sweep_covers_the_whole_decision_table(banner_mod):
    """A guard on the guard: if an axis loses a shape, the sweep below silently tests less.
    700 is 7 × 5 × 5 × 4 and is asserted so a shrunken axis is a RED, not a quiet pass."""
    cases = list(_sweep_cases())
    assert len(cases) == 700, len(cases)
    keys = {banner_mod.decide(_facts(**kw)) for kw in cases}
    # Every governing key except 'external' is reachable from these axes; 'external' is
    # reachable too (billing external with no higher axis). If a key becomes unreachable the
    # precedence has been broken above it.
    assert keys <= set(banner_mod.PRECEDENCE), keys - set(banner_mod.PRECEDENCE)
    assert "active" in keys and "unknown" in keys


def test_no_ok_severity_survives_a_failed_read(banner_mod):
    """🔴 THE 700-COMBINATION SWEEP. A green ✓ / the word OK / "No lifecycle, billing or
    service restriction is in force" is an ASSERTION THAT A NEGATIVE WAS ESTABLISHED. It may
    only be made when every reader answered.

    MEASURED AT 944fbaad: 24 of 700 combinations returned severity 'ok' with a reader's
    ``error`` flag set — all of them a serve / billing / commitment failure, because
    ``decide()`` escalated to 'unknown' on a LIFECYCLE failure only. Concrete harm:
    ``entitlements.comp_state`` raising on a COMPED account produced a green
    "no billing restriction is in force" over an account whose bill is a signed write-off.

    The required count is ZERO, not "small"."""
    offenders = []
    for kw in _sweep_cases():
        facts = _facts(**kw)
        if not banner_mod._any_reader_failed(facts):
            continue
        key = banner_mod.decide(facts)
        if banner_mod._SEVERITY[key] == "ok":
            offenders.append((kw, key))
    assert offenders == [], (
        str(len(offenders)) + " of 700 fact combinations render an ok-severity banner over a "
        "FAILED read — a green tick asserting a negative that was never established. "
        "First: " + repr(offenders[:3]))


def test_a_failed_read_never_reaches_the_reassuring_verbiage(banner_mod):
    """The severity is only the COLOUR. The words are the other half, and they are the half a
    reader actually acts on. This drives the FULL envelope (both audiences) and asserts that
    no combination with a failed read ever prints the sentence that denies a restriction."""
    denials = ("no lifecycle, billing or service restriction is in force",
               "everything is working", "nothing needs your attention")
    bad = []
    for kw in _sweep_cases():
        facts = _facts(**kw)
        if not banner_mod._any_reader_failed(facts):
            continue
        key = banner_mod.decide(facts)
        for audience in banner_mod.AUDIENCES:
            blob = (banner_mod._HEADLINE[audience][key] + " "
                    + banner_mod._VERBIAGE[audience][key]).lower()
            for d in denials:
                if d in blob:
                    bad.append((audience, key, d))
    assert bad == [], bad


def test_a_partial_read_still_states_the_restriction_it_could_establish(banner_mod):
    """The gate must not over-fire. A SUSPENDED account whose billing read failed is still
    suspended — that restriction WAS established, and burying it under a grey 'unknown'
    would hide the thing the operator must act on. Only an ok-severity answer is refused."""
    facts = _facts(status="suspended", billing_error=True, commitment_error=True)
    assert banner_mod.decide(facts) == "suspended"
    assert banner_mod._SEVERITY["suspended"] == "critical"
    # …and the failures are not lost: they surface as honest 'could not be determined' chips.
    sec = banner_mod._secondary("suspended", facts, "operator")
    details = " ".join(s["detail"] for s in sec)
    assert "could not be determined" in details, sec


def test_the_ok_gate_keys_on_severity_not_on_the_literal_active(banner_mod):
    """TEETH ON THE SHAPE OF THE FIX. Keying the gate on the string 'active' would re-open
    the hole the moment a second ok-severity key is added. This adds a synthetic ok key and
    asserts the gate covers it too — i.e. the gate reads ``_SEVERITY``, not a name."""
    banner_mod._SEVERITY["_probe_ok"] = "ok"
    try:
        facts = _facts(billing_error=True)
        # Monkeypatch the precedence walk to yield the synthetic ok key.
        original = banner_mod._governing_status
        banner_mod._governing_status = lambda _f: "_probe_ok"
        try:
            assert banner_mod.decide(facts) == "unknown"
        finally:
            banner_mod._governing_status = original
    finally:
        banner_mod._SEVERITY.pop("_probe_ok", None)


def test_the_fail_loud_logger_can_actually_be_called(banner_mod):
    """🔴 A FAIL-LOUD PATH THAT ITSELF FAILS IS THE WORST KIND. This module imported
    ``src.api.main.log_crit`` — which is ``log_crit(logger, msg, **args)``, TWO required
    positional arguments — and called it with one. Every one of the ten call sites raised
    ``TypeError``, and every one of them sits in a reader's except-arm on the line BEFORE
    ``out["error"] = True``, so a control-plane read failure raised a second unrelated
    exception past ``resolve()`` and 500'd the route instead of degrading the banner.

    Invisible without a DB failure to reach the line. Pinned here by CALLING it."""
    banner_mod._log_crit("teeth: the one-argument contract is exercised, not assumed")
    sig = inspect.signature(banner_mod._log_crit)
    required = [pname for pname, prm in sig.parameters.items()
                if prm.default is inspect.Parameter.empty
                and prm.kind in (prm.POSITIONAL_ONLY, prm.POSITIONAL_OR_KEYWORD)]
    assert len(required) == 1, (
        "_log_crit takes %d required positional args; every call site in this module passes "
        "exactly one message: %s" % (len(required), sig))


def test_the_tenant_demo_mirror_quotes_the_resolvers_own_tenant_copy(banner_mod):
    """The tenant console's demo handler for GET /tenant/status-banner (added 2026-08-13 —
    before it, "load demo data" showed every prospect a grey failure banner carrying the raw
    string "404 — demo: unhandled GET /tenant/status-banner").

    A demo mirror is a SECOND COPY of copy, which is the drift this module exists to prevent,
    so the copy it prints must be byte-identical to ``_HEADLINE['tenant']`` /
    ``_VERBIAGE['tenant']``. It cannot import Python, so this test is the coupling."""
    js = (pathlib.Path(__file__).resolve().parents[1]
          / "saas" / "dashboard" / "tenant.js").read_text(encoding="utf-8")
    assert "'/tenant/status-banner'" in js, "the tenant demo mirror is missing"
    for key in ("active", "suspended", "locked", "archived", "unknown"):
        for table in (banner_mod._HEADLINE, banner_mod._VERBIAGE):
            text = table["tenant"][key]
            assert text in js, (
                "tenant demo mirror does not carry the resolver's own tenant copy for %r: %r"
                % (key, text))
    # ...and it must never claim a confident green: the demo consults no control plane.
    assert "degraded: true" in js and "control plane not consulted" in js


@pytest.mark.parametrize("reader", ["_read_lifecycle", "_read_serve_mode",
                                    "_read_billing_arrangements", "_read_commitment"])
def test_a_raising_reader_degrades_the_banner_and_the_log_line_really_fires(
        banner_mod, monkeypatch, capsys, reader):
    """🔴 THE ERROR PATH, EXECUTED END TO END — FOR EACH READER SEPARATELY.

    This is the composition that made the two defects worse than either alone:
      (a) a failed reader other than lifecycle left ``severity='ok'`` (24 of 700 combinations);
      (b) the honest-escalation path called ``_log_crit(msg)`` against a TWO-argument
          ``src.api.main.log_crit``, so it raised ``TypeError`` on the line BEFORE
          ``out["error"] = True`` — the report of "I could not determine this" CRASHED
          instead of reporting, and the exception escaped ``resolve()`` entirely.

    So asserting the RETURN VALUE is not enough: a green assertion is compatible with the log
    call raising inside a swallowed except. This test asserts BOTH — the envelope degrades
    honestly AND the CRIT line was really written to stderr. Each reader is failed on its own,
    because a partial repair (one reader's arm fixed, the next left) must still fail."""
    monkeypatch.setattr(banner_mod, reader,
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("forced")),
                        raising=True)
    capsys.readouterr()          # drop anything emitted before this point

    out = banner_mod.resolve("11111111-2222-3333-4444-555555555555", audience="operator")

    # 1. It RETURNED. Before the arity fix this raised TypeError out of resolve().
    assert isinstance(out, dict), out
    # 2. It never claims a clean bill of health.
    assert out["severity"] != "ok", (reader, out["status_key"], out["severity"])
    assert out["degraded"] is True, out
    blob = (out["headline"] + " " + out["verbiage"]).lower()
    assert "no lifecycle, billing or service restriction is in force" not in blob
    # 3. 🔴 THE LOG LINE ACTUALLY FIRED. A silent degrade is the failure mode that let the
    #    arity bug live: FAIL LOUD means something reached stderr.
    err = capsys.readouterr().err
    assert "CRIT" in err, (
        "reader %s failed and NOTHING was logged — the fail-loud path is dead again. "
        "stderr was: %r" % (reader, err[-400:]))


def test_comp_is_an_arrangement_and_never_renders_as_a_problem(banner_mod):
    """A complimentary account is a deliberate, signed write-off with a reason and an
    optional expiry. Rendering it amber or red trains an operator to read our own
    generosity as a fault — and to stop trusting the colour."""
    assert banner_mod._SEVERITY["comp"] == "info"
    assert banner_mod._SEVERITY["comp"] not in ("warn", "critical")
    for audience in banner_mod.AUDIENCES:
        env = banner_mod._envelope("a", "comp", audience, [], degraded=False)
        assert env["severity"] == "info"
        blob = (env["headline"] + " " + env["verbiage"]).lower()
        for alarming in ("overdue", "past due", "problem", "failed", "action required",
                         "must be", "resolve this"):
            assert alarming not in blob, blob
        # "delinquency" is ALLOWED here but only in the negated form the copy actually uses
        # ("not a delinquency"). A bare occurrence would mean the copy started asserting one.
        if "delinquen" in blob:
            assert "not a delinquency" in blob, blob


def test_suspended_is_unmissable(banner_mod):
    assert banner_mod._SEVERITY["suspended"] == "critical"
    for audience in banner_mod.AUDIENCES:
        env = banner_mod._envelope("a", "suspended", audience, [], degraded=False)
        assert env["label"] == "Suspended"
        assert "suspend" in env["headline"].lower()
        assert env["verbiage"].strip()


def test_every_status_key_has_a_label_severity_headline_and_verbiage_for_every_audience(banner_mod):
    """A missing copy entry would be a KeyError at render time, on the one surface whose
    whole job is to be legible."""
    for key in banner_mod.PRECEDENCE:
        assert key in banner_mod._LABELS
        assert banner_mod._SEVERITY[key] in banner_mod.SEVERITIES
        for audience in banner_mod.AUDIENCES:
            assert banner_mod._HEADLINE[audience][key].strip()
            assert banner_mod._VERBIAGE[audience][key].strip()


def test_the_two_audiences_share_the_decision_and_differ_only_in_words(banner_mod):
    """🔴 'SAME SOURCE OF TRUTH, DIFFERENT VERBIAGE' IS ONLY WORTH CLAIMING IF IT IS
    CHECKABLE. status_key / severity must be byte-identical across audiences; headline and
    verbiage must NOT be (or the tenant is being handed operator instructions like 'clear
    the lock from the Danger Zone', a surface they cannot even see)."""
    for key in banner_mod.PRECEDENCE:
        op = banner_mod._envelope("a", key, "operator", [], degraded=False)
        tn = banner_mod._envelope("a", key, "tenant", [], degraded=False)
        assert op["status_key"] == tn["status_key"]
        assert op["severity"] == tn["severity"]
        assert op["label"] == tn["label"]
        assert op["headline"] != tn["headline"], key
        assert op["verbiage"] != tn["verbiage"], key
    # And the operator's levers must never leak into customer copy.
    for key in banner_mod.PRECEDENCE:
        tn = banner_mod._envelope("a", key, "tenant", [], degraded=False)
        blob = (tn["headline"] + " " + tn["verbiage"]).lower()
        for operator_only in ("danger zone", "trash", "check the logs", "operator decision"):
            assert operator_only not in blob, (key, blob)


def test_the_lifecycle_enum_is_not_a_private_second_copy(banner_mod):
    """If accounts.py grows a fifth status and this module does not, an account in that
    state must render UNKNOWN rather than fall through to 'active'. Pinning equality here
    makes the divergence a RED instead of a silent mis-report."""
    from saas import accounts as _accounts
    assert tuple(banner_mod.LIFECYCLE_STATUSES) == tuple(_accounts.ACCOUNT_STATUSES)


def test_precedence_tuple_is_the_precedence(banner_mod):
    """decide() walks PRECEDENCE, so the tuple and the docstring cannot drift apart by
    editing one and not the other."""
    assert banner_mod.PRECEDENCE == (
        "unknown", "archived", "locked", "suspended", "commitment_broken",
        "serve", "comp", "external", "active")
    assert set(banner_mod.PRECEDENCE) == set(banner_mod._LABELS)


def test_an_absent_commitment_surface_says_absent_and_not_nothing_owed(banner_mod):
    """THE MARKED SEAM. The term-acceleration / broken-commitment surface is owned by a
    concurrent workstream. On a deployment where it has not landed the banner must say the
    surface is NOT TRACKED — inventing 'nothing owed' would be a reassurance we cannot
    back."""
    sec = banner_mod._secondary("active", _facts(present=False, outstanding=False), "operator")
    entry = [s for s in sec if s["key"] == "commitment_broken"]
    assert entry, sec
    assert "not tracked" in entry[0]["detail"]
    assert "nothing" not in entry[0]["detail"].lower()
    assert "no " not in entry[0]["detail"].lower()


def test_a_failed_subread_is_reported_as_undetermined_not_as_a_negative(banner_mod):
    for kw, key in [(dict(serve_error=True), "serve"),
                    (dict(billing_error=True), "billing"),
                    (dict(commitment_error=True), "commitment_broken")]:
        sec = banner_mod._secondary("active", _facts(**kw), "operator")
        hit = [s for s in sec if s["key"] == key]
        assert hit, (kw, sec)
        assert "could not be determined" in hit[0]["detail"]


def test_secondary_keeps_every_axis_the_headline_could_not_carry(banner_mod):
    """A one-line banner PROJECTS four orthogonal axes onto one headline. The projection is
    only honest if the losers are still visible."""
    facts = _facts(status="suspended", comp_active=True, external=True,
                   agent_seats=2, frozen=2, outstanding=True)
    sec = banner_mod._secondary("suspended", facts, "operator")
    keys = {s["key"] for s in sec}
    assert {"comp", "external", "serve", "commitment_broken"} <= keys, keys


# ══════════════════════════════════════════════════════════════════════════════════════
# DB — the SQL is EXECUTED, not merely read
# ══════════════════════════════════════════════════════════════════════════════════════

_DB_RAN = {"value": False}
DSN = os.environ.get("POSTGRES_DSN")
needs_db = pytest.mark.skipif(
    not DSN,
    reason="POSTGRES_DSN not set — pass a migrated control-plane DSN "
           "(migrations >= 131) to execute the resolver's real SQL")


@pytest.fixture()
def account(monkeypatch):
    """A real row in a real public.accounts, torn down after each test."""
    import psycopg2
    _DB_RAN["value"] = True
    aid = str(uuid.uuid4())
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.accounts (account_id, name, slug, status) "
            "VALUES (%s, %s, %s, 'active')", (aid, "OPSIA Test", "opsia-" + aid[:8]))
    yield aid, conn
    with conn.cursor() as cur:
        for t in ("public.account_ledger", "public.term_accelerations",
                  "public.account_users", "public.accounts"):
            try:
                cur.execute("DELETE FROM " + t + " WHERE account_id = %s", (aid,))
            except Exception:
                conn.rollback()   # append-only ledger refuses DELETE — expected, harmless
    conn.close()


@needs_db
def test_every_reader_runs_against_the_real_schema(account, banner_mod):
    """🔴 ONE TEST OVER ALL READERS, ON PURPOSE. A suite of dict fixtures cannot see a
    column that does not exist; a partial repair (fix one query, leave the next) must still
    fail. Every reader is called with a real cursor against the real migrated tables and
    none may raise, and none may report the `error` flag that its own except-arm sets."""
    aid, _ = account
    for fn in (banner_mod._read_lifecycle, banner_mod._read_serve_mode,
               banner_mod._read_commitment, banner_mod._read_billing_arrangements):
        out = fn(aid)
        assert isinstance(out, dict), fn.__name__
        assert out.get("error") is not True, (
            fn.__name__ + " reported a read error against a healthy migrated schema — "
            "check the log line it emitted for the failing column")
    assert banner_mod._read_lifecycle(aid)["found"] is True


@needs_db
@pytest.mark.parametrize("status,expected", [
    ("active", "active"), ("suspended", "suspended"),
    ("locked", "locked"), ("archived", "archived"),
])
def test_the_lifecycle_states_resolve_end_to_end(account, banner_mod, status, expected):
    aid, conn = account
    with conn.cursor() as cur:
        cur.execute("UPDATE public.accounts SET status = %s WHERE account_id = %s",
                    (status, aid))
    r = banner_mod.resolve(aid, audience="operator")
    assert r["status_key"] == expected
    assert r["lifecycle_status"] == status
    assert r["degraded"] is False, r["degraded_reason"]
    assert r["severity"] == ("ok" if status == "active" else "critical")


@needs_db
def test_an_account_that_does_not_exist_is_unknown_not_active(banner_mod):
    _DB_RAN["value"] = True
    r = banner_mod.resolve(str(uuid.uuid4()), audience="operator")
    assert r["status_key"] == "unknown"
    assert r["severity"] == "unknown"
    assert r["degraded"] is True


@needs_db
def test_serve_mode_reads_the_rows_the_write_gate_reads(account, banner_mod):
    """🔴 THE COLUMN THAT LOOKS RIGHT AND IS NOT. ``public.accounts.ingest_enabled`` has a
    setter and reads like the tenant-wide train/serve switch, but ``agent_mode.ingest_allowed``
    never selects it — the gate keys off ``account_users.ingest_enabled`` on Agent seats.
    Asserted in BOTH directions, because a banner that reports an unenforced freeze is
    exactly as wrong as one that misses an enforced one."""
    aid, conn = account
    with conn.cursor() as cur:
        # The account column says FROZEN. The gate does not read it.
        cur.execute("UPDATE public.accounts SET ingest_enabled = false WHERE account_id = %s",
                    (aid,))
        for i, ing in enumerate([True, True]):
            cur.execute(
                "INSERT INTO public.account_users (account_id, user_id, status, seat_type, "
                "ingest_enabled) VALUES (%s, %s, 'active', 'agent', %s)",
                (aid, str(uuid.uuid4()), ing))
    assert banner_mod.resolve(aid)["status_key"] == "active", (
        "accounts.ingest_enabled=false must NOT produce serve mode — the write gate "
        "does not read that column")

    with conn.cursor() as cur:
        # Now freeze the SEATS, and restore the account column to 'enabled'.
        cur.execute("UPDATE public.accounts SET ingest_enabled = true WHERE account_id = %s",
                    (aid,))
        cur.execute("UPDATE public.account_users SET ingest_enabled = false "
                    "WHERE account_id = %s", (aid,))
    r = banner_mod.resolve(aid)
    assert r["status_key"] == "serve", r
    assert r["severity"] == "info"   # a deliberate product mode, never amber


@needs_db
def test_a_partially_frozen_fleet_is_not_serve_mode_but_is_still_reported(account, banner_mod):
    aid, conn = account
    with conn.cursor() as cur:
        for ing in (True, False, True):
            cur.execute(
                "INSERT INTO public.account_users (account_id, user_id, status, seat_type, "
                "ingest_enabled) VALUES (%s, %s, 'active', 'agent', %s)",
                (aid, str(uuid.uuid4()), ing))
    r = banner_mod.resolve(aid)
    assert r["status_key"] == "active"
    serve = [s for s in r["secondary"] if s["key"] == "serve"]
    assert serve and "1 of 3" in serve[0]["detail"], r["secondary"]


@needs_db
def test_comp_and_external_resolve_from_the_real_columns(account, banner_mod):
    aid, conn = account
    with conn.cursor() as cur:
        cur.execute("UPDATE public.accounts SET externally_billed = true, "
                    "external_billing_processor = 'stripe' WHERE account_id = %s", (aid,))
    r = banner_mod.resolve(aid)
    assert r["status_key"] == "external" and r["severity"] == "info"

    with conn.cursor() as cur:
        cur.execute("UPDATE public.accounts SET comped = true, comp_reason = 'internal "
                    "dogfood' WHERE account_id = %s", (aid,))
    r = banner_mod.resolve(aid)
    assert r["status_key"] == "comp", "comp outranks external — it suppresses the whole bill"
    assert r["severity"] == "info"
    ext = [s for s in r["secondary"] if s["key"] == "external"]
    assert ext, "the losing axis must still be visible"


@needs_db
def test_a_broken_commitment_outranks_a_billing_arrangement(account, banner_mod):
    aid, conn = account
    with conn.cursor() as cur:
        cur.execute("UPDATE public.accounts SET comped = true, comp_reason = 'internal' "
                    "WHERE account_id = %s", (aid,))
        cur.execute(
            "INSERT INTO public.term_accelerations (account_id, base_fee_cents, "
            "months_remaining, amount_cents, status) VALUES (%s, 150000, 3, 450000, 'invoiced')",
            (aid,))
    r = banner_mod.resolve(aid)
    assert r["status_key"] == "commitment_broken"
    assert r["severity"] == "warn"
    # and it is still not a lifecycle problem — service keeps running
    assert r["lifecycle_status"] == "active"



def test_the_commitment_reader_defers_to_the_owning_module(banner_mod):
    """``saas/term_acceleration.py`` is the concurrent workstream's module and its
    ``state()`` derives the 30-day grace verdict, failing TOWARD THE CUSTOMER when the
    notice date is unknown. Re-deriving that here would be a second, divergent copy of a
    CONTRACTUAL decision — so the banner defers, and the direct SELECT survives only as the
    fallback for a deployment where the module is absent. Pinned so a later 'simplification'
    cannot quietly fork the grace clock."""
    src = inspect.getsource(banner_mod._read_commitment)
    assert "from . import term_acceleration" in src
    assert "_ta.state(account_id)" in src
    # ...and a read failure from THEIR module must surface as undetermined, never as clear.
    assert 'out["error"] = True' in src
    assert "not as 'nothing owed'" in src


@needs_db
def test_the_grace_clock_reaches_the_banner(account, banner_mod):
    """The one number a customer under a written-notice clock actually needs."""
    import datetime as dt
    aid, conn = account
    until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=17)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.term_accelerations (account_id, base_fee_cents, "
            "months_remaining, amount_cents, status, notice_sent_at, grace_until) "
            "VALUES (%s, 150000, 3, 450000, 'invoiced', NOW(), %s)", (aid, until))
    r = banner_mod.resolve(aid, audience="tenant")
    assert r["status_key"] == "commitment_broken"
    # The grace days ride the SECONDARY chip when the commitment is the headline? No — when
    # it governs, the headline carries it and secondary omits it. Force it to lose instead.
    with conn.cursor() as cur:
        cur.execute("UPDATE public.accounts SET status = 'suspended' WHERE account_id = %s",
                    (aid,))
    r = banner_mod.resolve(aid, audience="tenant")
    assert r["status_key"] == "suspended"
    chip = [s for s in r["secondary"] if s["key"] == "commitment_broken"][0]
    assert "invoiced" in chip["detail"] and "CAD 4500.00" in chip["detail"]
    assert "d left)" in chip["detail"], chip["detail"]

@needs_db
def test_a_settled_acceleration_no_longer_governs(account, banner_mod):
    aid, conn = account
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.term_accelerations (account_id, base_fee_cents, "
            "months_remaining, amount_cents, status) VALUES (%s, 150000, 3, 450000, 'settled')",
            (aid,))
    assert banner_mod.resolve(aid)["status_key"] == "active"


@needs_db
def test_the_tenant_audience_reaches_the_same_decision_over_real_data(account, banner_mod):
    aid, conn = account
    with conn.cursor() as cur:
        cur.execute("UPDATE public.accounts SET status = 'suspended' WHERE account_id = %s",
                    (aid,))
    op = banner_mod.resolve(aid, audience="operator")
    tn = banner_mod.resolve(aid, audience="tenant")
    assert op["status_key"] == tn["status_key"] == "suspended"
    assert op["severity"] == tn["severity"]
    assert [s["key"] for s in op["secondary"]] == [s["key"] for s in tn["secondary"]]
    assert op["verbiage"] != tn["verbiage"]
    # The customer needs a REMEDY, not a lever they cannot reach.
    assert "billing" in tn["verbiage"].lower() or "support" in tn["verbiage"].lower()


# ══════════════════════════════════════════════════════════════════════════════════════
# STATIC — route wiring, the per-face allowlist, CSP
# ══════════════════════════════════════════════════════════════════════════════════════

def test_the_tenant_route_cannot_be_pointed_at_another_account():
    """BOLA. The tenant endpoint must take NO account identifier of any kind — the id is
    server-derived from the signed session. Asserted on the RESOLVED dependency object, not
    by grepping for the decorator name: a grep passes on a comment."""
    from saas import tenant_console as tc
    from saas.tenant_console import require_dashboard_session
    fn = tc.tenant_status_banner
    params = inspect.signature(fn).parameters
    assert set(params) == {"account_id"}, params
    dep = params["account_id"].default
    assert getattr(dep, "dependency", None) is require_dashboard_session
    # The ROUTE PATH itself carries no id either — there is nothing to substitute.
    src = inspect.getsource(tc)
    assert '@router.get("/tenant/status-banner")' in src
    assert '/tenant/status-banner/{' not in src


def test_the_operator_route_is_account_authorized():
    from saas import admin
    from saas.admin import require_account
    fn = admin.get_account_status_banner
    params = inspect.signature(fn).parameters
    assert set(params) == {"account_id", "resolved"}
    assert getattr(params["resolved"].default, "dependency", None) is require_account
    # _authorize_account_path is the cross-account firewall on every /accounts/{id} route.
    assert "_authorize_account_path(resolved, account_id)" in inspect.getsource(fn)


def test_both_consoles_call_the_one_resolver_and_neither_re_decides():
    import re
    """The whole point of resolving server-side. If either console starts branching on the
    component facts, the two will drift and one will reassure somebody wrongly."""
    from saas import account_status_banner as m
    assert 'account_status_banner as _banner' in inspect.getsource(__import__(
        "saas.admin", fromlist=["x"]).get_account_status_banner)
    assert 'audience="operator"' in inspect.getsource(
        __import__("saas.admin", fromlist=["x"]).get_account_status_banner)
    assert 'audience="tenant"' in inspect.getsource(
        __import__("saas.tenant_console", fromlist=["x"]).tenant_status_banner)
    # NO STATUS ARITHMETIC IN EITHER CONSOLE'S BANNER LANE. Scoped to the loadStatusBanner
    # body rather than the whole file on purpose: `severity ===` legitimately appears
    # elsewhere in tenant.js (the unrelated brain-health banner), and a whole-file grep
    # would be a false positive that later gets "fixed" by weakening the rule everywhere.
    # What must hold is that the STATUS-BANNER path branches on nothing.
    for js, name in ((APP_JS, "app.js"), (TENANT_JS, "tenant.js")):
        start = js.index("async function loadStatusBanner")
        body = js[start:js.index("\n}", start)]
        # Strip comments first. The prose legitimately NAMES the fields it is forbidding
        # code from touching ("...renders an explicit status unknown in the unknown
        # severity"), and a grep over comments would ban documenting the rule. Same
        # precedent as CSS_LIVE in tests/test_operator_console_ux.py.
        body = re.sub(r"//[^\n]*", "", body)
        for forbidden in ("status_key", "severity", "comped", "suspended", "lifecycle_status"):
            assert forbidden not in body, (name, forbidden, body)
        assert "FLStatusBanner.render" in body, name
    # ...and the renderer itself only maps severity → glyph; it composes no sentence and
    # makes no decision about what a status MEANS.
    assert "status_key ===" not in BANNER_JS
    for forbidden in ("comped", "suspended", "archived", "locked", "externally_billed"):
        assert forbidden not in BANNER_JS, forbidden


def test_the_shared_asset_is_served_on_both_faces():
    """``_OPERATOR_CONSOLE_EXACT`` / ``_TENANT_CONSOLE_EXACT`` are DENY-lists for the OTHER
    face. status_banner.js is loaded by BOTH consoles, so listing it in either would 404 it
    on the console that needs it — the same treatment style.css and tour.js already get.
    This asserts the intent explicitly so nobody 'fixes' it by adding it to one."""
    import src.mcp.http_server as hs
    path = "/dashboard/status_banner.js"
    assert path not in hs._OPERATOR_CONSOLE_EXACT
    assert path not in hs._TENANT_CONSOLE_EXACT
    # The precedent: the other genuinely-shared assets are in neither list either.
    for shared in ("/dashboard/style.css", "/dashboard/feature_copy.js", "/dashboard/tour.js"):
        assert shared not in hs._OPERATOR_CONSOLE_EXACT
        assert shared not in hs._TENANT_CONSOLE_EXACT
    # And it is genuinely loaded by both.
    assert 'src="/dashboard/status_banner.js"' in INDEX
    assert 'src="/dashboard/status_banner.js"' in TENANT_HTML
    # It is not a PAGE, so it has no business in the offline page list.
    assert path not in hs._OFFLINE_BLOCKED_PAGES


class _ScriptCounter(html.parser.HTMLParser):
    """A REAL parser, not a regex. A regex counts the word 'script' inside a comment and
    reports a CSP violation that does not exist (measured — it did, on this very file)."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.inline_scripts = 0
        self.on_attrs = []
        self._in_script_without_src = False

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        for k in d:
            if k.lower().startswith("on"):
                self.on_attrs.append((tag, k))
        if tag == "script" and not d.get("src"):
            self._in_script_without_src = True

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script_without_src = False

    def handle_data(self, data):
        if self._in_script_without_src and data.strip():
            self.inline_scripts += 1


@pytest.mark.parametrize("name", ["index.html", "tenant.html"])
def test_the_dashboard_stays_csp_clean(name):
    """`script-src 'self'` with no 'unsafe-inline'. An inline-script build renders BLANK
    with no error — there is no louder failure to catch this later."""
    p = _ScriptCounter()
    p.feed((DASH / name).read_text(encoding="utf-8"))
    assert p.inline_scripts == 0, name
    assert p.on_attrs == [], (name, p.on_attrs)
    assert "javascript:" not in (DASH / name).read_text(encoding="utf-8").lower()


def test_the_banner_markup_carries_the_live_region_role_in_both_consoles():
    """role="status" must be in the MARKUP, not applied by the renderer: a live region
    injected at the same moment as its text is not reliably announced. Polite (implicit for
    role=status per MDN) rather than role="alert", which is for updates that must interrupt
    — this banner is persistent and re-announced on every tenant switch."""
    for src, name in ((INDEX, "index.html"), (TENANT_HTML, "tenant.html")):
        assert 'id="acct-status-banner"' in src, name
        i = src.index('id="acct-status-banner"')
        tag = src[src.rindex("<", 0, i):src.index(">", i) + 1]
        assert 'role="status"' in tag, (name, tag)
        assert 'role="alert"' not in tag, name
        assert " hidden" in tag, (name, "must start hidden — no status until one is resolved")
    # The renderer must not invent a role of its own.
    assert "setAttribute('role'" not in BANNER_JS


def test_the_popup_follows_the_apg_disclosure_pattern():
    """W3C WAI-ARIA APG, Disclosure Pattern: the control is a button carrying aria-expanded
    (true when shown, false when hidden) and optionally aria-controls naming the region.
    https://www.w3.org/WAI/ARIA/apg/patterns/disclosure/"""
    i = INDEX.index('id="opmenu-btn"')
    tag = INDEX[INDEX.rindex("<", 0, i):INDEX.index(">", i) + 1]
    assert tag.startswith("<button"), tag
    assert 'aria-expanded="false"' in tag
    assert 'aria-controls="opmenu-panel"' in tag
    # NOT the Menu Button pattern: no role=menu / menuitem anywhere in the popup.
    panel = INDEX[INDEX.index('id="opmenu-panel"'):INDEX.index('id="opmenu-btn"')]
    assert 'role="menu"' not in panel and 'role="menuitem"' not in panel
    # Sign out is an ACTION, not a route — a .nav-item with no data-view breaks the router
    # AND the search index (measured: undefined.replace in navFindResults).
    assert 'class="opmenu-act opmenu-signout"' in INDEX
    assert 'class="nav-item opmenu-signout"' not in INDEX


def test_colour_is_never_the_only_signal():
    """WCAG 2.2 SC 1.4.1 Use of Color. Each severity carries a DISTINCT glyph as well as a
    colour, plus the status word as visible text and again in the visually-hidden prefix."""
    import re
    glyphs = re.search(r"var GLYPH = \{(.*?)\};", BANNER_JS, re.S).group(1)
    found = dict(re.findall(r"(\w+):\s*'(.+?)'", glyphs))
    assert set(found) == {"ok", "info", "warn", "critical", "unknown"}, found
    assert len(set(found.values())) == 5, ("glyphs must be distinct in monochrome", found)
    assert "asb-sr" in BANNER_JS and "Account status: " in BANNER_JS
    css = (DASH / "style.css").read_text(encoding="utf-8")
    # The visually-hidden prefix must be CLIPPED, not display:none (which removes it from
    # the accessibility tree along with the pixels).
    block = css[css.index(".asb-sr {"):css.index("}", css.index(".asb-sr {"))]
    assert "clip" in block and "display: none" not in block


def test_the_severity_palette_does_not_follow_the_accent_picker():
    """Every other accent-coloured thing in this console correctly follows the operator's
    chosen swatch. These five must NOT: picking the red swatch would paint a healthy account
    red and a suspended one red, and the colour channel would carry no information at all.
    The owner asked for "OK 'green'" — green regardless of the picker."""
    css = (DASH / "style.css").read_text(encoding="utf-8")
    ok = css[css.index(".asb-ok"):css.index("\n", css.index(".asb-ok"))]
    assert "var(--accent)" not in ok, ok
    assert "#3FB950" in ok or "#3fb950" in ok.lower(), ok
    crit = css[css.index(".asb-critical"):css.index("\n", css.index(".asb-critical"))]
    assert "var(--danger)" in crit
    # UNKNOWN is additionally hatched, so "we could not read this" is unlike every other
    # state even in greyscale.
    unk = css[css.index(".asb-unknown"):css.index("}", css.index(".asb-unknown"))]
    assert "repeating-linear-gradient" in unk


# ══════════════════════════════════════════════════════════════════════════════════════
# RENDERED — the real page, the real router, the real popup, the real renderer
# ══════════════════════════════════════════════════════════════════════════════════════

INSTALL_HINT = "npm install --prefix tests/dashboard"


@pytest.fixture(scope="module")
def report():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed — the rendered half needs it")
    proc = subprocess.run([node, str(HARNESS)], capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        err = (proc.stderr or "")[:400]
        if "Cannot find module 'jsdom'" in err:
            pytest.skip("jsdom missing — run: " + INSTALL_HINT)
        pytest.fail("render harness exited 1 — a REAL failure, not a missing dependency:\n" + err)
    return json.loads(proc.stdout)


def test_no_operator_action_became_unreachable(report):
    """🔴 THE RE-ORGANISATION PROOF, BOTH DIRECTIONS. Every view reachable from the nav
    before the restructure is still reachable after, from exactly one of the three homes —
    and nothing appeared that the inventory does not account for."""
    m = report["markup"]
    after = set(m["nav_views"])
    assert after == ALL_VIEWS_BEFORE, {
        "lost": sorted(ALL_VIEWS_BEFORE - after),
        "unaccounted": sorted(after - ALL_VIEWS_BEFORE)}
    # Every one still has a real panel behind it, and every panel still has a way in.
    assert m["nav_views_without_panel"] == []
    assert m["panels_without_nav"] == []


def test_the_left_rail_holds_tenant_things_and_nothing_else(report):
    m = report["markup"]
    assert set(m["left_rail_views"]) == LEFT_TENANT_VIEWS, {
        "unexpected on the left": sorted(set(m["left_rail_views"]) - LEFT_TENANT_VIEWS),
        "missing from the left": sorted(LEFT_TENANT_VIEWS - set(m["left_rail_views"]))}
    assert (m["left_rail_head"] or "").strip().lower() == "tenant", m["left_rail_head"]
    # The search box stays the first thing in the tenant rail.
    assert m["search_is_first_sidenav_child"]


def test_the_right_rail_holds_operational_things_and_nothing_tenant_scoped(report):
    m = report["markup"]
    assert set(m["right_rail_views"]) == RIGHT_OPERATIONAL_VIEWS, {
        "unexpected on the right": sorted(set(m["right_rail_views"]) - RIGHT_OPERATIONAL_VIEWS),
        "missing from the right": sorted(RIGHT_OPERATIONAL_VIEWS - set(m["right_rail_views"]))}
    assert (m["right_rail_head"] or "").strip().lower() == "operations"
    # The hard rule: nothing tenant-scoped may appear on the operational side.
    client_views = set(json.loads(report["scripts"]["client_views"]))
    assert not (set(m["right_rail_views"]) & client_views)
    # The Benchmarks external link travelled with its section.
    assert m["nav_ext_count"] == 1 and m["nav_ext_is_anchor"]
    assert not m["nav_ext_is_nav_item"], "a.nav-ext must stay unbound by the router"


def test_the_operator_menu_holds_only_the_operators_own_identity(report):
    m = report["markup"]
    assert set(m["opmenu_views"]) == OPMENU_VIEWS
    assert m["opmenu_present"] and m["opmenu_btn_is_button"]
    assert m["opmenu_panel_is_navrail"], (
        "the popup must carry the ROUTER class or its items render, look clickable and "
        "do nothing")
    assert m["opmenu_panel_precedes_button"], (
        "the panel must precede the trigger in DOM order so it opens UPWARD with visual "
        "order == DOM order == tab order")
    assert m["opmenu_panel_starts_hidden"]
    assert m["opmenu_btn_aria_expanded"] == "false"
    assert m["opmenu_btn_aria_controls"] == "opmenu-panel"
    assert not m["opmenu_signout_is_nav_item"]


def test_the_operator_menu_behaves_like_a_disclosure(report):
    """Driven through the real wireOperatorMenu() the console installs at boot."""
    o = report["opmenu"]
    assert o["available"]
    assert o["closed_initially"]
    assert o["open_sets_aria_expanded"] and o["open_reveals_panel"]
    assert o["focus_moves_into_panel"], "a keyboard user must not be dropped at the document top"
    assert o["escape_closes"]
    assert o["escape_returns_focus"], "Escape must not strand focus on a hidden element"
    assert o["click_outside_closes"]
    assert o["click_inside_keeps_open"], (
        "dismissing on any inside click would eat the very click that selects an item")
    assert o["account_item_routes"] == "account"
    assert o["account_item_dismisses"]
    assert o["identity_label"] == "Jane Operator"


def test_every_severity_renders_a_distinct_colour_class_glyph_and_label(report):
    b = report["banner"]
    assert b["available"]
    rows = {r["key"]: r for r in b["rendered"]}
    assert set(rows) == {"active", "comp", "serve", "commitment_broken", "suspended",
                         "locked", "archived", "unknown"}
    expected_class = {
        "active": "asb asb-ok", "comp": "asb asb-info", "serve": "asb asb-info",
        "commitment_broken": "asb asb-warn", "suspended": "asb asb-critical",
        "locked": "asb asb-critical", "archived": "asb asb-critical",
        "unknown": "asb asb-unknown"}
    for key, want in expected_class.items():
        r = rows[key]
        assert r["cls"] == want, (key, r["cls"])
        assert r["hidden"] is False
        assert r["label_text"], key
        assert r["headline_text"], key
        assert r["verbiage_text"], key            # the far-right verbiage is always printed
        assert r["sr_text"].startswith("Account status: "), key
        assert r["glyph"], key
        assert r["chips"] == 1, key               # the secondary axes survive the projection
    # comp must not look like a problem, and suspended must not look like comp.
    assert rows["comp"]["cls"] != rows["suspended"]["cls"]
    assert rows["comp"]["cls"] != rows["commitment_broken"]["cls"]
    assert rows["active"]["cls"] != rows["unknown"]["cls"]


def test_the_renderer_escapes_and_never_degrades_to_a_comforting_blank(report):
    b = report["banner"]
    assert b["escapes_html"], "a tenant-supplied name can reach the secondary detail"
    assert b["unreadable"]["cls"] == "asb asb-unknown"
    assert b["unreadable"]["hidden"] is False, (
        "an unreadable status must be VISIBLE as unknown — a blank reads as 'fine'")
    assert b["unreadable"]["label"] == "Status unknown"
    assert b["cleared"]["hidden"] is True and b["cleared"]["html"] == "", (
        "clearing must empty as well as hide, or a stale tenant's status flashes back")


def test_the_banner_is_pinned_inside_the_content_column(report):
    m = report["markup"]
    assert m["status_banner_present"]
    assert m["status_banner_role"] == "status"
    assert m["status_banner_starts_hidden"]
    assert m["status_banner_is_inside_content"]
    css = (DASH / "style.css").read_text(encoding="utf-8")
    block = css[css.index("\n.asb {"):css.index("}", css.index("\n.asb {"))]
    assert "position: sticky" in block, "'pinned' is the whole request"
    assert "top: 0" in block
    # It must outrank the sticky tenant header (z-index 20) or it is pinned under it.
    assert "z-index: 25" in block


def test_the_router_still_reaches_every_view_after_the_move(report):
    assert report["router"]["errors"] == []
    for v, r in report["router"]["routes"].items():
        assert r["landed"] in (v, "clients"), (v, r)   # tenant-scoped views bounce w/o a tenant


def test_the_search_index_survived_the_split(report):
    """navFindViewIndex() reads the live rails. A .nav-item with no data-view used to throw
    `undefined.replace` out of it and take the whole search box down with it."""
    s = report["scripts"]
    assert s["actions_with_unknown_view"] == [], s["actions_with_unknown_view"]
    hits = report.get("search") or {}
    assert hits, "the search probe must still run"


def test_the_guided_tour_still_reaches_its_targets(report):
    """MOVING NAV ITEMS BREAKS THE TOUR SILENTLY: a step's target must live INSIDE the
    tab/panel its `reveal` opens, and a target inside a CLOSED <details> has a zero rect."""
    t = report["tour"]
    assert t["public_surface"]
    assert t["tenants_section_opened_by_tour"], "create-tenant could not reach its nav target"
    assert t["client_section_opened_by_tour"], "event-log could not reach the Audit nav target"


def test_every_operator_tour_reveal_still_resolves_in_the_new_markup():
    """Audited statically as well, because the harness only drives two of the topics. Every
    `reveal:` selector in the operator tour must match something on the page."""
    import re
    tour = (DASH / "tour.js").read_text(encoding="utf-8")
    op = tour[tour.index("operator: ["):tour.index("tenant: [")]
    reveals = set(re.findall(r"reveal:\s*'(\[data-view=\"[^\"]+\"\])'", op))
    assert reveals, "the operator tour lost its reveals"
    for sel in reveals:
        view = re.search(r'data-view="([^"]+)"', sel).group(1)
        assert 'data-view="%s"' % view in INDEX, sel
        assert view in ALL_VIEWS_BEFORE, sel


def test_no_data_feature_lost_its_copy():
    """`data-feature` is coupled to FL_FEATURES and a mismatch fails SILENTLY — an inert `?`
    and an empty popup, no error. Nothing was renamed by this restructure; this asserts it."""
    import re
    copy = (DASH / "feature_copy.js").read_text(encoding="utf-8")
    for name, src in (("index.html", INDEX), ("tenant.html", TENANT_HTML)):
        for key in sorted(set(re.findall(r'data-feature="([^"]+)"', src))):
            assert ("'%s'" % key) in copy or ('"%s"' % key) in copy, (name, key)


# ══════════════════════════════════════════════════════════════════════════════════════
# FALSE-GREEN GUARD — a run where every DB case silently skipped is a RED, not a PASS.
# ══════════════════════════════════════════════════════════════════════════════════════

def test_no_db_test_was_silently_skipped():
    """🔴 STRENGTHENED 2026-08-13 — THE OLD GUARD SKIPPED ITSELF AND SO GUARDED NOTHING.

    It caught only "DSN set but nothing connected". The failure that actually happened was
    the OTHER one: every run of this suite since it was written had ``POSTGRES_DSN`` unset,
    so **14 of 62 tests skipped every single time** and the four readers' real SQL — including
    fresh queries against ``public.term_accelerations`` and ``public.account_ledger``, both
    created by migration 254 in the same commit — had NEVER EXECUTED. A column typo there
    raises, the reader sets ``error``, and (before the OK gate above) that landed in the green
    'active' path. The report line read ``48 passed`` and looked like a pass.

    So an ABSENT DSN is now a RED, not a skip. The DB half of this suite is not optional: it
    is the only thing that executes the SQL. Run it as::

        POSTGRES_DSN=postgresql://faultline:faultline@172.20.0.3:5432/faultline_opsia_test \\
          python3 tools/fltest.py --bug <REF> --test tests/test_operator_ia_and_status_banner.py

    THE OPT-OUT IS DELIBERATELY EXPLICIT AND NOISY. ``FL_ALLOW_DB_SKIP=1`` exists for an
    environment that genuinely has no control-plane database, because a guard nobody can
    satisfy gets deleted rather than fixed. It must be a decision somebody TYPED — the whole
    defect was that the skip was the silent default."""
    if not DSN:
        if os.environ.get("FL_ALLOW_DB_SKIP") == "1":
            pytest.skip("FL_ALLOW_DB_SKIP=1 — DB half deliberately waived for this environment")
        pytest.fail(
            "POSTGRES_DSN is NOT SET, so the 14 database tests in this suite did not run and "
            "the resolver's real SQL was never executed. That is a FALSE GREEN, not a pass — "
            "it is how the four readers shipped unexecuted. Point POSTGRES_DSN at a migrated "
            "control-plane database (migrations >= 131), e.g. "
            "postgresql://faultline:faultline@172.20.0.3:5432/faultline_opsia_test . "
            "If this environment genuinely has no control plane, set FL_ALLOW_DB_SKIP=1 to "
            "waive it explicitly.")
    assert _DB_RAN["value"], (
        "POSTGRES_DSN was set but no DB test actually connected — the DB half passed "
        "vacuously, which is exactly the false green this guard exists to catch")
