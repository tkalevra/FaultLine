"""THE ENFORCED CONTRACT TERM, THE POINT-OF-SIGNATURE SUMMARY, AND THE NOTICE RECORD.

FOUR THINGS THIS SUITE FALSIFIES
────────────────────────────────

A. **A NUMBER THE SYSTEM ENFORCES MUST COME FROM THE SAME PLACE THE CUSTOMER READ IT.**
   Migration 256 froze the four contract numbers onto published templates so an operator editing
   ``platform_settings`` could no longer move the words of a live agreement. Correct for the three
   RETENTION windows — nothing enforces those. WRONG, on its own, for ``CONTRACT_TERM_MONTHS``,
   which the system DOES enforce: freezing only the words split the number in two. Measured at
   ``944fbaad`` with the setting at 24, against the Enterprise tier::

       AGREEMENT BODY (section 3) : 6-month term        (frozen; id/version/sha unchanged)
       tick-box the customer signs: 24-month subscription … total commitment of CA$36,000.00
       accounts.term_months       : 24   →  term acceleration invoices 24 months of arrears

   THE GENERAL RULE: **before freezing a number into a document, ask whether any code ENFORCES
   it.** Freezing a DESCRIPTIVE value removes drift; freezing an ENFORCED value CREATES a
   divergence. The fix derives the enforced term from the active template's own frozen value, so
   body / summary / provisioning / acceleration agree BY CONSTRUCTION rather than by an alarm
   (``_reconcile_against_signed_contract``) that fires after the money has moved.

B. **A VERSION BUMP MUST SWEEP THE UI COPY THAT RESTATES THE CLAUSE IT CHANGED.** Terms v2 exists
   to say "**Seats are not part of your term commitment.**" The summary block directly above the
   tick-box still rendered the v1 sentence "A seat you add is committed for the rest of the term
   in the same way" — a misrepresentation at the point of signature, contradicting the binding
   document on the same screen.

C. **AN ALARM SEVERITY KEYED ON ONE HARDCODED ACTOR STRING DECAYS THE MOMENT A NEW SEED ARRIVES.**
   ``_substitute_for_display`` chose ``_log`` vs ``_log_crit`` on ``created_by == 'system-seed'``.
   Migration 243's v1 matched; migration 253's v2 — the agreement IN FORCE — stamped
   ``'migration-251'`` and did not, so every read emitted a CRITICAL. Exactly the alarm-fatigue
   failure that code's own comment reasons about, aimed at the one row read most often.

D. **THE CHANGELOG IS THE NOTICE RECORD, SO A FALSE ONE IS A MISLEADING NOTICE.** Migration 256's
   backfilled summary for v2 ended "Nothing else in the agreement changed" — beside a NEW clause
   accelerating the remaining base fee into a single immediately-due invoice. Courts have held
   that "providing revised terms without indicating what changed was not sufficient notice"
   (Ironclad, *6 Components of Clickwrap Enforceability*); an affirmative denial is worse than
   silence.

⚠️ ``total_commitment_cents`` — the figure inside the authorization the customer ticks — had ZERO
test references anywhere in the repo before this file. The entire point-of-signature summary was
an uncovered path.

⚠️ FALSE-GREEN GUARD: ``_DB_RAN`` is set by the DB fixture and asserted by the last test. Run with
a THROWAWAY DSN (migrations >= 131 applied) and assert skipped == 0.
"""

import os
import re
from pathlib import Path

import pytest

# Control-plane singletons (the append-only ACTIVE contract chain, the platform settings row,
# the global operator realm) are shared by every suite in one database: the suites that
# write or depend on them run on one xdist worker (under --dist loadgroup, see tests/conftest.py).
pytestmark = pytest.mark.xdist_group("control_plane")

from saas import contracts as _contracts
from saas import platform_settings as _ps
from saas import tenant_checkout as _checkout
from saas import tenant_provisioning as _prov


REPO = Path(__file__).resolve().parent.parent
SUBSCRIBE_JS = REPO / "saas" / "dashboard" / "subscribe.js"
SUBSCRIBE_HTML = REPO / "saas" / "dashboard" / "subscribe.html"
MIG_253 = REPO / "migrations" / "253_terms_v2_elastic_seats.sql"
MIG_256 = REPO / "migrations" / "256_contract_changelog_and_frozen_terms.sql"

_DSN = os.environ.get("POSTGRES_DSN")

try:
    import psycopg2 as _pg
except Exception:  # pragma: no cover
    _pg = None

needs_db = pytest.mark.skipif(not _DSN or _pg is None,
                              reason="POSTGRES_DSN + psycopg2 (migrations >= 131) required")

_DB_RAN = {"yes": False}

V2 = "2026-08-12-v2"

# The term this suite PUBLISHES into its own agreement. Deliberately not 6 (the shipped
# default) and not 24 (the value the tests move the live setting to), so neither a stale default
# nor the setting under test can make an assertion pass by coincidence.
_PUBLISHED_TERM = 8


# ══════════════════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════════════════

def _strip_js_comments(src: str) -> str:
    """Executable JS only.

    ⚠️ LOAD-BEARING, NOT TIDINESS. The fix for finding B DOCUMENTS the v1 sentence it removed, in
    a comment, so that the next person understands why the copy reads as it does. A naive
    ``assert "committed for the rest of the term" not in js`` would then match the explanation and
    fail — a file that DOCUMENTS a rule must not be indistinguishable from one that BREAKS it.
    (This has bitten this codebase at least three times; strip first, assert second.)"""
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"(?m)^\s*//.*$", " ", src)
    return src


def _js_prose(src: str) -> str:
    """Comment-free JS with string-CONCATENATION SEAMS closed up.

    ⚠️ WITHOUT THIS, EVERY COPY ASSERTION IS A COIN FLIP. The customer-facing sentences are built
    as ``'…with no ' + 'part-month charge…'`` to stay inside the line limit, so the phrase the
    USER reads ("no part-month charge") does not exist as a substring of the SOURCE. A test that
    greps the raw file is testing where the author happened to press Enter, not what is rendered.
    Closing ``' + '`` seams reconstructs the delivered prose."""
    src = _strip_js_comments(src)
    return re.sub(r"'\s*\+\s*'", "", src)


def _strip_html_comments(src: str) -> str:
    return re.sub(r"<!--.*?-->", " ", src, flags=re.S)


@pytest.fixture(scope="module")
def db():
    conn = _pg.connect(_DSN)
    conn.autocommit = True
    _DB_RAN["yes"] = True
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="module")
def own_agreement(db):
    """Publish an agreement THIS SUITE owns, and assert against that.

    ⚠️ WHY, AND IT COST A GREEN RUN TO LEARN: these tests originally asserted against the
    SQL-seeded ``2026-08-12-v2``. ``contract_templates`` permits exactly ONE non-retired row
    (``idx_contract_templates_one_active``), so ANY sibling suite that publishes a template makes
    ITS row "the active agreement" — and several do. Measured in a full sweep: the active version
    had become ``ret-eb184ca5a3-term-v2``, frozen at 24 by a suite that did not restore it, and
    three tests here failed on the leftovers rather than on the code.

    A test about "the ACTIVE agreement" must therefore CREATE the active agreement, not assume
    one. Published through the real ``publish_terms_version`` seam so the frozen value is written
    the way production writes it (substituted once at publish), with a body carrying the token so
    the RENDERED term can be read back out of the prose the customer would see.

    No cleanup: the table is append-only (migration 243), and a uuid-suffixed version means reruns
    never collide."""
    import uuid as _uuid
    ver = "term-authority-" + _uuid.uuid4().hex[:10]
    # Publish while the platform setting is at the KNOWN value, so the frozen number is known.
    with db.cursor() as cur:
        cur.execute("SELECT contract_term_months FROM public.platform_settings WHERE singleton")
        before = cur.fetchone()[0]
    _set_term_setting(db, _PUBLISHED_TERM)
    try:
        res = _contracts.publish_terms_version(
            version=ver,
            title="Term Authority Fixture",
            body_markdown=(
                "### 3. What you pay\n\n"
                "- **Your commitment.** This is a **{{CONTRACT_TERM_MONTHS}}-month term**, "
                "starting on the day you subscribe.\n"
                "- **Renewal.** Renews for a further {{CONTRACT_TERM_MONTHS}}-month term.\n"
            ),
            created_by="test-term-authority",
            change_summary="Fixture version published by tests/test_contract_term_authority.py "
                           "to own the active agreement for the duration of this suite.",
            change_is_material=False,
        )
    finally:
        with db.cursor() as cur:
            cur.execute("UPDATE public.platform_settings SET contract_term_months = %s "
                        "WHERE singleton", (before,))
        _ps.invalidate()
    assert res.get("ok"), f"could not publish the fixture agreement: {res}"
    return ver


@pytest.fixture
def settings_restored(db):
    """Snapshot and restore the platform singleton.

    A shared row: a probe that writes it and does not put it back poisons every later suite in the
    same database (measured once — 13 spurious failures across 4 suites)."""
    with db.cursor() as cur:
        cur.execute("SELECT contract_term_months FROM public.platform_settings WHERE singleton")
        before = cur.fetchone()[0]
    _ps.invalidate()
    try:
        yield
    finally:
        with db.cursor() as cur:
            cur.execute("UPDATE public.platform_settings SET contract_term_months = %s "
                        "WHERE singleton", (before,))
        _ps.invalidate()


def _set_term_setting(db, months: int):
    with db.cursor() as cur:
        cur.execute("UPDATE public.platform_settings SET contract_term_months = %s "
                    "WHERE singleton", (int(months),))
    _ps.invalidate()


def _body_term(body: str):
    """The term the AGREEMENT BODY states, read out of the rendered markdown."""
    m = re.search(r"\*\*(\d+)-month term\*\*", body or "")
    return int(m.group(1)) if m else None


# ══════════════════════════════════════════════════════════════════════════════════
# A — THE ENFORCED TERM
# ══════════════════════════════════════════════════════════════════════════════════

@needs_db
def test_the_enforced_term_comes_from_the_active_agreement_not_the_live_setting(
        db, own_agreement, settings_restored):
    """THE HEADLINE. Move the operator setting; the agreement in force must not move, and neither
    must anything that enforces it."""
    _set_term_setting(db, 24)

    frozen = _contracts.active_contract_term_months()
    assert frozen == _PUBLISHED_TERM, (
        f"the active agreement states {frozen!r}, expected the {_PUBLISHED_TERM} this suite "
        f"published — a sibling suite's template is active (see the own_agreement fixture)")
    assert _ps.contract_term_months() == 24, "the operator setting did not take"
    assert _prov.tenant_term_months() == frozen, (
        f"tenant_term_months() returned the LIVE SETTING ({_prov.tenant_term_months()}) instead "
        f"of the term the active agreement states ({frozen}). The customer would sign one number "
        f"and be provisioned another.")
    assert _prov.tenant_term_months() != 24


@needs_db
def test_body_summary_and_provisioning_all_state_the_same_term(db, own_agreement, settings_restored):
    """The four consumers must AGREE — that is the deliverable, not merely 'the alarm fires'."""
    _set_term_setting(db, 24)

    terms = _contracts.get_current_terms()
    tier = _checkout.get_tier("enterprise")

    body = _body_term(terms["body_markdown"])
    assert body is not None, "could not read the term out of the rendered agreement body"
    assert body == tier["term_months"] == _prov.tenant_term_months(), (
        f"DIVERGENCE — agreement body says {body}, the tick-box says {tier['term_months']}, "
        f"provisioning would write {_prov.tenant_term_months()}")


@needs_db
def test_the_total_commitment_the_customer_ticks_matches_the_agreement(
        db, own_agreement, settings_restored):
    """``total_commitment_cents`` is the figure inside the authorization. It had NO test.

    It is the monthly amount times the term, and the term must be the AGREEMENT'S term — a wrong
    multiplier here is the difference between authorizing CA$9,000 and CA$36,000."""
    _set_term_setting(db, 24)

    terms = _contracts.get_current_terms()
    body = _body_term(terms["body_markdown"])
    for tier_key in ("team", "enterprise"):
        tier = _checkout.get_tier(tier_key)
        assert tier["term_months"] == body, f"{tier_key}: term {tier['term_months']} != body {body}"
        assert tier["total_commitment_cents"] == tier["amount_cents"] * body, (
            f"{tier_key}: the authorization would state "
            f"{tier['total_commitment_cents']} cents for a {body}-month agreement")


@needs_db
def test_every_catalog_tier_carries_a_usable_term_and_total(db, own_agreement):
    """No tier may reach the signature page with a missing or zero commitment figure."""
    cat = _checkout.tier_catalog()
    assert cat, "the tier catalog is empty"
    for tier in cat:
        assert isinstance(tier["term_months"], int) and tier["term_months"] >= 1
        assert isinstance(tier["total_commitment_cents"], int)
        assert tier["total_commitment_cents"] > 0


@needs_db
def test_a_template_with_no_frozen_term_falls_back_to_the_configured_setting(
        db, own_agreement, settings_restored):
    """The fallback is REAL, not decorative: a pre-246 control plane, or a version published
    before the column existed, must still resolve a term — and the configured one is right there.

    Driven by actually clearing the column on the active row (``retention_values`` is not in the
    immutable list), not by a monkeypatch, so it proves the SQL path too."""
    with db.cursor() as cur:
        cur.execute("SELECT id, retention_values FROM public.contract_templates "
                    " WHERE retired_at IS NULL ORDER BY effective_at DESC LIMIT 1")
        tid, before = cur.fetchone()
    try:
        with db.cursor() as cur:
            cur.execute("UPDATE public.contract_templates SET retention_values = NULL "
                        " WHERE id = %s", (tid,))
        _set_term_setting(db, 9)
        assert _contracts.active_contract_term_months() is None
        assert _prov.tenant_term_months() == 9, (
            "with no frozen term the configured setting must govern — otherwise an operator's "
            "CONTRACT setting is decorative again")
    finally:
        import json
        with db.cursor() as cur:
            cur.execute("UPDATE public.contract_templates SET retention_values = %s::jsonb "
                        " WHERE id = %s", (json.dumps(before) if before is not None else None, tid))


@needs_db
def test_a_zero_or_negative_frozen_term_is_refused_not_enforced(
        db, own_agreement, settings_restored, monkeypatch):
    """A published agreement stating a zero-month commitment cannot be enforced; the resolver must
    decline it and fall back rather than provision "no commitment at all"."""
    monkeypatch.setattr(_contracts, "active_contract_term_months", lambda: None)
    _set_term_setting(db, 7)
    assert _prov.tenant_term_months() == 7
    # And the real resolver refuses a junk frozen value outright.
    with db.cursor() as cur:
        cur.execute("SELECT id, retention_values FROM public.contract_templates "
                    " WHERE retired_at IS NULL ORDER BY effective_at DESC LIMIT 1")
        tid, before = cur.fetchone()
    import json
    try:
        for junk in (0, -3, "abc", None):
            payload = dict(before or {})
            payload["CONTRACT_TERM_MONTHS"] = junk
            with db.cursor() as cur:
                cur.execute("UPDATE public.contract_templates SET retention_values = %s::jsonb "
                            " WHERE id = %s", (json.dumps(payload), tid))
            monkeypatch.undo()
            assert _contracts.active_contract_term_months() is None, (
                f"CONTRACT_TERM_MONTHS={junk!r} was accepted as an enforceable term")
            monkeypatch.setattr(_contracts, "active_contract_term_months", lambda: None)
    finally:
        with db.cursor() as cur:
            cur.execute("UPDATE public.contract_templates SET retention_values = %s::jsonb "
                        " WHERE id = %s", (json.dumps(before) if before is not None else None, tid))


def test_the_resolution_order_is_stated_where_the_decision_is_made():
    """BRAIN NOT TRANSPORT: exactly ONE function decides the term, and the consumers read it."""
    src = (REPO / "saas" / "tenant_provisioning.py").read_text()
    body = src.split("def tenant_term_months()", 1)[1].split("\ndef ", 1)[0]
    # ⚠️ STRIP THE DOCSTRING AND THE COMMENTS FIRST. Measured: a mutation that DELETED the
    # resolution branch left this test green, because the docstring above it still named the
    # function. Asserting on a file that documents the rule is not asserting on the code.
    body = re.sub(r'"""[\s\S]*?"""', " ", body)
    body = re.sub(r"(?m)#.*$", " ", body)
    assert "active_contract_term_months" in body, (
        "tenant_term_months no longer consults the active agreement — the enforced term has been "
        "re-pointed at the live setting and the document/machine divergence is back")
    # The catalog and the provisioning path must both go through that one function.
    assert "tenant_term_months()" in (REPO / "saas" / "tenant_checkout.py").read_text()


# ══════════════════════════════════════════════════════════════════════════════════
# B — THE POINT-OF-SIGNATURE COPY
# ══════════════════════════════════════════════════════════════════════════════════

def test_the_summary_no_longer_contradicts_terms_v2_on_seats():
    """The v1 sentence must be GONE from the executable copy (comments explaining it are fine)."""
    js = _strip_js_comments(SUBSCRIBE_JS.read_text())
    assert "committed for the rest of the term" not in js, (
        "subscribe.js still renders the v1 seat sentence. Terms v2 §2 says the opposite — "
        "'Seats are not part of your term commitment' — and the summary sits on the same screen, "
        "directly above the tick-box.")


def test_the_summary_states_the_v2_seat_rule():
    js = _js_prose(SUBSCRIBE_JS.read_text())
    assert "Seats are not part of your term commitment" in js
    assert "no part-month charge" in js and "no part-month refund" in js


def test_the_summary_and_the_tickbox_disclose_the_acceleration_clause():
    """v2 §3 adds a clause making the remaining base fee due as ONE invoice. A summary that omits
    the only clause accelerating the customer's liability is not a summary of this agreement."""
    js = _js_prose(SUBSCRIBE_JS.read_text())
    assert "single final invoice" in js
    assert "resume" in js.lower(), "the agreement gives two options; the summary states one"
    # The LAST assignment is the real authorization; an earlier one is the "pick a plan" stub.
    agree = js.rsplit("agree.textContent", 1)[1]
    assert "single invoice" in agree or "single final invoice" in agree, (
        "the authorization the customer ticks does not mention that the remaining base fee "
        "becomes due at once")
    assert "base fee" in agree


def test_the_cancellation_copy_does_not_promise_no_further_charge():
    """subscribe.html told the customer "you are not charged again" on cancellation. Under v2 §3
    the base fee for the rest of the term becomes due as a final invoice — the page promised the
    opposite of the binding document."""
    html = _strip_html_comments(SUBSCRIBE_HTML.read_text())
    assert "you are not charged again" not in html
    assert "Your term does not end when your subscription does" in html


def test_the_subscribe_page_stays_csp_clean():
    """dashboard CSP is ``script-src 'self'`` with no ``'unsafe-inline'`` — an inline-script build
    renders BLANK."""
    html = SUBSCRIBE_HTML.read_text()
    for tag in re.findall(r"<script\b[^>]*>", html):
        assert "src=" in tag, f"inline <script> found: {tag}"
    stripped = _strip_html_comments(html)
    assert not re.search(r"\son[a-z]+\s*=", stripped), "inline on*= handler found"


# ══════════════════════════════════════════════════════════════════════════════════
# C — THE ALARM
# ══════════════════════════════════════════════════════════════════════════════════

def test_a_migration_is_recognised_as_a_sanctioned_seed_writer():
    """STRUCTURAL, not a second literal. A migration cannot call ``publish_terms_version`` — that
    is precisely why its rows carry raw tokens — so it is a sanctioned seed writer by
    construction, whatever number it happens to have."""
    for actor in ("system-seed", "SYSTEM-SEED", " system-seed ", "migration-243", "migration-253",
                  "migration-251", "migration-9999"):
        assert _contracts.is_sanctioned_seed_writer(actor) is True, actor


def test_anything_that_is_not_a_seed_still_screams():
    """The alarm must keep its teeth: an unsanctioned writer is the case it exists for."""
    for actor in (None, "", "operator", "alice@example.com", "migration", "migration-",
                  "migration-251-ish", "x migration-9", "seed"):
        assert _contracts.is_sanctioned_seed_writer(actor) is False, actor


def test_migration_253_stamps_its_own_number():
    """It stamped ``migration-251`` — a stale filename from before it was renumbered. Harmless as
    a label, load-bearing as an actor string."""
    sql = MIG_253.read_text()
    assert "'migration-253'" in sql
    body = sql.split("-- ⚠️ THIS ACTOR STRING", 1)[0] if "-- ⚠️ THIS ACTOR STRING" in sql else sql
    assert "'migration-251'" not in body, "the stale actor string is still the value being INSERTed"


@needs_db
def test_reading_the_live_agreement_emits_no_critical(db, capsys):
    """END TO END, against the real row: the agreement in force must not cry wolf on every read.

    ``_log_crit`` is the signal "something wrote to contract_templates outside the sanctioned
    path". Firing it on every ``GET /tenant/plans`` is how an operator learns to ignore it."""
    terms = _contracts.get_current_terms()
    assert terms is not None
    captured = capsys.readouterr()
    crit = [ln for ln in captured.err.splitlines()
            if "saas-contracts-CRIT" in ln and "outside the sanctioned path" in ln]
    assert not crit, (
        "reading the ACTIVE agreement emitted a CRITICAL alarm:\n  " + "\n  ".join(crit))


# ══════════════════════════════════════════════════════════════════════════════════
# D — THE NOTICE RECORD
# ══════════════════════════════════════════════════════════════════════════════════


def _read_v2(db):
    with db.cursor() as cur:
        cur.execute("SELECT change_summary, change_is_material, supersedes_version "
                    "  FROM public.contract_templates WHERE version = %s", (V2,))
        return cur.fetchone()


@needs_db
def test_the_v2_changelog_does_not_claim_nothing_else_changed(db):
    """THE FALSE SENTENCE. It sat beside a brand-new acceleration of liability."""
    summary, _material, _sup = _read_v2(db)
    assert summary, "v2 has no change_summary"
    low = summary.lower()
    assert "nothing else in the agreement changed" not in low, (
        "the changelog still affirmatively denies changes that the body diff shows it made")


@needs_db
def test_the_v2_changelog_enumerates_every_substantive_change(db):
    """Derived from a real diff of the two stored bodies, not from a migration header.

    Each probe below is a phrase from a passage v2 ADDS or REWRITES; the summary is the NOTICE
    RECORD, and an omission here is an unnoticed clause."""
    summary, _material, _sup = _read_v2(db)
    low = summary.lower()
    required = {
        "§2 seats leave the term commitment":        "part of your term commitment",
        "§2 no part-month charge/refund":            "part-month",
        "§3 the committed thing is the base fee":    "base fee",
        "§3 ACCELERATION — one immediate invoice":   "single final invoice",
        "§3 acceleration: settle or resume":         "resuming your subscription",
        "§3 acceleration: 30-day grace":             "30 days",
        "§3 one invoice for base + seats + add-ons": "billed together on one invoice",
        "§3 suspension does not end the term":       "does not end your term",
        "§3 price change never applies mid-term":    "never applies to a term you are already in",
        "§8 version recorded with the signature":    "version number alongside your signature",
        "§8 grandfathering / renewal only":          "only on renewal",
    }
    missing = [name for name, probe in required.items() if probe.lower() not in low]
    assert not missing, "the v2 changelog omits:\n  - " + "\n  - ".join(missing)


@needs_db
def test_the_v2_changelog_still_says_what_did_not_change(db):
    """Accuracy runs both ways — the diff confirms these are genuinely untouched."""
    summary, _m, _s = _read_v2(db)
    low = summary.lower()
    assert "unchanged" in low
    for kept in ("base fee amount", "length of the term", "liability", "governing law"):
        assert kept in low, f"the summary no longer states that the {kept} is unchanged"


@needs_db
def test_v2_is_recorded_as_a_material_change(db):
    """FAIL-SAFE DIRECTION. Acceleration does not increase what is owed; it makes it immediately
    due, which increases the obligation IN KIND — and section 8 gives a material change a 30-day
    notice and a right to cancel.

    The module's own principle decides the tie: *wrongly-material costs an email;
    wrongly-non-material breaks a written promise.*

    ⚠️ FLAGGED FOR THE OWNER, NOT SETTLED POLICY. If he deliberately downgrades this to FALSE,
    update this test WITH his decision recorded — do not just flip it to make a suite green."""
    _summary, material, supersedes = _read_v2(db)
    assert material is True, (
        "v2 is recorded as a NON-material change. It introduces an acceleration clause; section 8 "
        "promises 30 days' notice and a right to cancel for a material change, and we cannot "
        "un-publish a change we promised to pre-announce.")
    assert supersedes == "2026-08-12-v1"


def test_the_migration_records_the_material_judgement_and_flags_it_for_the_owner():
    """A judgement call with no trace is a default nobody examined."""
    sql = MIG_256.read_text()
    assert "change_is_material = TRUE" in sql
    assert "FLAGGED FOR THE OWNER" in sql
    assert "wrongly-material costs" in sql


@needs_db
def test_zzz_no_test_was_silently_skipped():
    """FALSE-GREEN GUARD."""
    assert _DB_RAN["yes"], (
        "the DB fixture never ran — the term-authority and changelog claims were never checked "
        "against a database.")
