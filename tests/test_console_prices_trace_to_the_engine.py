"""NO BASE-PACKAGE FIGURE MAY LIVE IN THE OPERATOR CONSOLE. Every one traces to the engine.

WHAT WENT WRONG, AND WHY A TEST THAT PINS *THE CURRENT NUMBER* WOULD NOT HAVE CAUGHT IT.

Team launched as a no-base, pure-per-seat tier. It was later given a real base package with
included Standard seats. The MONEY LAYER migrated completely — ``entitlements.team_base_price()``,
``TEAM_INCLUDED_STANDARD``, ``_team_billing_view()`` emitting a real base line,
``tenant_checkout._TIERS['team'].amount_cents``, ``/admin/billing/overview`` returning
``team.base_price``. The CONSOLE COPY did not migrate, because the console held its own copies of
the numbers as literals in HTML and JS. Six of them, in five files, for months.

The result was a console that contradicted itself INSIDE ONE FLOW: the create-tenant Package
chooser said Team was a zero-base tier, and the confirmation banner rendered seconds later — from
a DIFFERENT literal in app.js, which happened to be right — said otherwise. Neither was reading
the engine. The right-looking one was right by luck and was one price change away from being the
wrong one.

So this file does NOT assert "the console says 750". That assertion goes stale in exactly the same
way and reintroduces exactly the same defect one layer up. It asserts:

  1. THE CONSOLE STATES NO PACKAGE AMOUNT AT ALL. No currency literal for any base package
     survives in the served markup or scripts, in any spelling the engine's own numbers could
     take. Derived FROM the engine at test time, so it keeps working after a price change.
  2. THE ENGINE SERVES THE FIGURES. ``GET /admin/pricing`` carries ``account_kinds``, and every
     row agrees with ``entitlements`` for every account kind.
  3. EVERY SURFACE IN THE BAR READS THAT CATALOG. The package chooser, the creation confirmation,
     the kind badges, Plan & Capacity, the billing overview — each is wired to the catalog and
     none of them carries a fallback literal.
  4. THE FIGURES AGREE ACROSS SURFACES. The chooser, the capacity report, the billing overview
     and the invoice the engine builds state ONE base price and ONE included-seat count per tier.
  5. ⚠️ ZERO IS NEVER A PRICE. An unresolved amount renders "unknown". A ``|| 750`` style default
     is a confident wrong answer about money and is banned by name.
     ⚠️ AND ITS OPPOSITE IS PROTECTED: a COMPED account's zero is a real, deliberate, signed zero
     and must keep rendering as a zero bill. See ``tests/test_comp_and_trial_survive_pricing_ia``
     and ``tests/test_saas_billing_quantity_and_comp`` — the two rules look identical and are not.

DEFECT 2, pinned in section 6: ``capacity_report()`` back-filled a missing standard entitlement
with ``BASE_QUANTITY`` (5 — the ENTERPRISE allotment) BEFORE it knew the account's kind. A freshly
created Team account showed "Standard licensed 5 / base 5" on Plan & Capacity while
``included_standard`` in the SAME payload said 2 and ``_team_billing_view`` invoiced 2. The console
told the operator the customer had more than they were paying for, in a payload that disagreed
with itself two fields apart.

────────────────────────────────────────────────────────────────────────────────────────────
THREE KINDS OF EVIDENCE, AND WHY THE FIRST CUT OF THIS FILE ONLY HAD ONE
────────────────────────────────────────────────────────────────────────────────────────────
An adversarial review mutation-tested the first cut and got five mutations past it, every one
of which restores a real, shipped defect. All five slipped through for the same reason: the
file was almost entirely ``re`` OVER SOURCE TEXT. A grep proves a mechanism EXISTS. It cannot
prove the mechanism RUNS, and it cannot prove what the mechanism PRODUCES.

  STATIC    (sections 1–5) — the bans. "No base-package amount is written anywhere", "no
            ``|| 750`` fallback survives". A ban is the one thing a grep is genuinely the right
            tool for: it is an assertion about the TEXT, not about behaviour.

  RENDERED  (section 3R) — ``tests/dashboard/render_money_surfaces.js`` loads the real
            index.html + the real app.js in jsdom, feeds ``/admin/pricing`` a payload of
            SENTINEL figures that appear nowhere in this repo, drives the console's own
            ``ensurePricing`` / ``renderPlan`` / ``renderBillingOverview`` / ``kindBadge``, and
            reports the text that landed in the DOM. Unwiring ``renderTierCatalogCopy()`` from
            ``ensurePricing()`` leaves every grep in this file green and every catalog-driven
            surface showing its placeholder; only a render can see that. It is also where the
            ⚠️ COMP invariant is measured on the shipped code: ``CADX(0) === 'CAD $0.00'``.

  DB-BACKED (section 6B) — real accounts in a throwaway control plane, real
            ``provision_capacity`` / ``provision_bundle`` calls, and the ``base_quantity`` READ
            BACK OUT OF ``public.account_entitlements``. The write half of DEFECT 2 moves MONEY
            (``base_quantity`` is the count billing treats as already paid for), and the only
            honest way to pin money is to look at the row and then at the invoice line.

RUN IT (make your OWN throwaway database; the DB lane refuses a name without "test" in it):
  psql postgresql://faultline:faultline@172.20.0.6:5432/postgres \\
    -c "CREATE DATABASE faultline_priceia_beh_test TEMPLATE faultline_pricepanel_test;"
  npm install --prefix tests/dashboard        # jsdom, for the rendered lane
  POSTGRES_DSN=postgresql://faultline:faultline@172.20.0.6:5432/faultline_priceia_beh_test \\
    python3 tools/fltest.py --bug PRICEIA --test tests/test_console_prices_trace_to_the_engine.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DASH = ROOT / "saas" / "dashboard"
INDEX = (DASH / "index.html").read_text(encoding="utf-8")
APPJS = (DASH / "app.js").read_text(encoding="utf-8")
FEATJS = (DASH / "feature_copy.js").read_text(encoding="utf-8")
BILLJS = (DASH / "billing.js").read_text(encoding="utf-8")

# The four files the operator console is actually served as. A price literal in ANY of them is a
# price the engine cannot move — including inside the demo mock, which is a script in this console
# and which used to hold the Enterprise base package as a bare `1500`.
CONSOLE_FILES = {
    "index.html": INDEX,
    "app.js": APPJS,
    "feature_copy.js": FEATJS,
    "billing.js": BILLJS,
}

# The TENANT-facing console. The bar names the OPERATOR console, but a stale price here is the
# worse defect of the two: this one is read by the customer paying it. Held to the same ban.
TENANT_FILES = {
    "tenant.html": (DASH / "tenant.html").read_text(encoding="utf-8"),
    "tenant.js": (DASH / "tenant.js").read_text(encoding="utf-8"),
}


@pytest.fixture(scope="module")
def ent():
    from saas import entitlements
    return entitlements


@pytest.fixture(scope="module")
def catalog(ent):
    rows = ent.account_kind_catalog()
    assert rows, "entitlements.account_kind_catalog() returned nothing"
    return {r["kind"]: r for r in rows}


# ══════════════════════════════════════════════════════════════════════════════════════
# THE TWO EXECUTED LANES — their toolchains, and the bookkeeping that stops either of them
# disappearing quietly. A lane that skips in silence is a green suite over untested code.
# ══════════════════════════════════════════════════════════════════════════════════════

HARNESS = ROOT / "tests" / "dashboard" / "render_money_surfaces.js"
INSTALL_HINT = "npm install --prefix tests/dashboard"
# Node reports an unresolvable require() with this text on stderr. Matched on the MESSAGE, not
# the exit code: a harness merely missing its dependency and a harness genuinely broken by a
# regression both exit 1, and only the first of those is allowed to skip.
_MODULE_MISSING = re.compile(r"Cannot find module ['\"]([^'\"]+)['\"]")

#: Why the rendered lane did not run, if it did not. ``None`` here after a full session means
#: the lane vanished without even reaching its own toolchain check — see the guard in section 7.
_RENDER = {"ran": False, "skipped_because": None}

# The DB lane. Its own DSN var so a shared POSTGRES_DSN cannot drag this suite into somebody
# else's database, and the "test" check so a stray DSN can never have it writing into anything
# real. Same guard the sibling DB suites use (tests/test_scheduled_downgrade_applier.py).
DSN = (os.environ.get("PRICEIA_TEST_DSN") or os.environ.get("POSTGRES_DSN") or "").strip()
_DB_RAN = {"yes": False}


def _dsn_usable() -> bool:
    return bool(DSN) and "test" in DSN.rsplit("/", 1)[-1].split("?")[0].lower()


requires_db = pytest.mark.skipif(
    not _dsn_usable(),
    reason="set PRICEIA_TEST_DSN (or POSTGRES_DSN) to a THROWAWAY control plane whose database "
           "name contains 'test' — see the run recipe in this file's docstring and the tripwire "
           "in section 7")


@pytest.fixture(scope="module")
def rendered():
    """Run ``tests/dashboard/render_money_surfaces.js`` and hand back its observations.

    THREE outcomes, deliberately distinct (the same split as tests/test_operator_console_ux.py,
    and for the same reason):
      * toolchain absent (no node, no harness file) → SKIP, naming what is missing;
      * dependency absent ("Cannot find module")    → SKIP, naming the install command;
      * anything else                               → FAIL, loudly, with stderr.

    The third branch is the point. A harness broken by a real regression in app.js must not be
    able to disguise itself as somebody's missing npm install.
    """
    node = shutil.which("node")
    if not node:
        _RENDER["skipped_because"] = "node is not on PATH"
        pytest.skip("node is not on PATH — install Node.js to run the rendered money-surface "
                    "lane; the static assertions in this module still run")
    if not HARNESS.exists():
        _RENDER["skipped_because"] = f"{HARNESS.name} is missing"
        pytest.skip(f"render harness missing at {HARNESS.relative_to(ROOT)}")

    env = dict(os.environ)
    roots = [str(HARNESS.parent / "node_modules"), str(ROOT / "node_modules")]
    if env.get("NODE_PATH"):
        roots.append(env["NODE_PATH"])
    env["NODE_PATH"] = os.pathsep.join(roots)

    try:
        out = subprocess.run([node, str(HARNESS)], capture_output=True, text=True,
                             timeout=180, cwd=str(ROOT), env=env)
    except subprocess.TimeoutExpired:                         # pragma: no cover
        pytest.fail("the money-surface harness timed out after 180s — it hangs or waits on a "
                    "real network call; every endpoint it needs must be stubbed")
    except OSError as exc:                                    # pragma: no cover
        _RENDER["skipped_because"] = f"failed to launch: {exc}"
        pytest.skip(f"render harness failed to launch: {exc}")

    if out.returncode != 0:
        missing = _MODULE_MISSING.search(out.stderr or "")
        if missing:
            _RENDER["skipped_because"] = f"node module {missing.group(1)!r} is not installed"
            pytest.skip(f"node module {missing.group(1)!r} is not installed — run:  "
                        f"{INSTALL_HINT}  (declared in tests/dashboard/package.json; "
                        f"node_modules is intentionally not committed).")
        pytest.fail(f"the money-surface harness exited {out.returncode} — this is a REAL "
                    f"failure, not a missing dependency:\n{(out.stderr or '')[-3000:]}")

    try:
        report = json.loads(out.stdout)
    except json.JSONDecodeError as exc:                       # pragma: no cover
        pytest.fail(f"the money-surface harness emitted non-JSON on stdout ({exc}); "
                    f"stderr tail:\n{(out.stderr or '')[-2000:]}")
    _RENDER["ran"] = True
    return report


# ══════════════════════════════════════════════════════════════════════════════════════
# 1 — NO BASE-PACKAGE CURRENCY LITERAL SURVIVES IN THE CONSOLE
# ══════════════════════════════════════════════════════════════════════════════════════

def _money_spellings(amount):
    """Every way this console could plausibly have written one amount as money.

    Derived from the ENGINE's number rather than from a list of digits somebody typed here, so
    the ban follows a price change instead of quietly ceasing to mean anything after one.
    """
    if amount is None:
        return []
    whole = int(round(float(amount)))
    plain = str(whole)
    grouped = f"{whole:,}"
    out = set()
    for body in {plain, grouped}:
        for tail in ("", ".00", ".0"):
            for lead in ("$", "$ ", "CA$", "CAD $", "CAD$"):
                out.add(lead + body + tail)
    return sorted(out)


def test_no_base_package_amount_is_written_anywhere_in_the_console(catalog):
    """The bar, stated as a grep. Every tier's base price, in every currency spelling, must be
    absent from the served markup and scripts — because the only correct number of places for a
    price outside the billing engine is zero."""
    banned = {}
    for kind, row in catalog.items():
        for spelling in _money_spellings(row.get("base_price")):
            banned[spelling] = kind
    assert banned, "no base prices resolved — this test would be vacuously green"
    found = []
    for fname, text in CONSOLE_FILES.items():
        for spelling, kind in banned.items():
            for m in re.finditer(re.escape(spelling), text):
                line = text.count("\n", 0, m.start()) + 1
                found.append(f"{fname}:{line} states {spelling!r} (the {kind} base package)")
    assert not found, (
        "a base-package amount is written into the operator console:\n  " + "\n  ".join(found) +
        "\nEvery figure must come from GET /admin/pricing -> account_kinds. A literal here is a "
        "price the engine cannot move, and it is how the Package chooser went on calling Team a "
        "zero-base tier for months after Team was given a base package.")


def test_no_base_package_amount_is_written_into_the_TENANT_console_either(catalog):
    """SCOPE-ADJACENT AND MORE SERIOUS THAN THE BAR ASKS FOR. The operator console was the brief.
    The tenant console is read by the CUSTOMER, and it carried the clinical base fee as a literal
    in a section header the clinic paying it reads, plus the Enterprise package as a literal in
    its own demo mock. A price the engine cannot move is worse pointed outward than inward."""
    banned = {}
    for kind, row in catalog.items():
        for spelling in _money_spellings(row.get("base_price")):
            banned[spelling] = kind
    found = []
    for fname, text in TENANT_FILES.items():
        for spelling, kind in banned.items():
            for m in re.finditer(re.escape(spelling), text):
                line = text.count("\n", 0, m.start()) + 1
                found.append(f"{fname}:{line} states {spelling!r} (the {kind} base package)")
    assert not found, (
        "a base-package amount is written into the TENANT console:\n  " + "\n  ".join(found) +
        "\nThese figures reach the paying customer. They must come from the tenant endpoints "
        "(GET /tenant/clinical/plan, GET /tenant/plans), which resolve them from entitlements.")


def test_the_tenant_clinical_plan_never_renders_a_null_as_a_price():
    """``$${esc(String(p.base_price))}`` renders the four characters ``null`` when the engine
    could not resolve the fee — which is not a currency zero, but is not an honest 'unknown'
    either, and it appears on a customer's own plan page."""
    tjs = TENANT_FILES["tenant.js"]
    fn = tjs[tjs.index("async function loadClinicalPlan()"):]
    fn = fn[:fn.index("\n}")]
    assert "String(p.base_price)" not in fn, (
        "the clinical base fee is stringified without a null check — an unresolved fee renders "
        "the literal text 'null' on the customer's plan page")
    assert "'unknown'" in fn


# ══════════════════════════════════════════════════════════════════════════════════════
# 1B — MONEY WEARING NO CURRENCY SIGIL
# ══════════════════════════════════════════════════════════════════════════════════════
# ⚠️ THIS SECTION EXISTS BECAUSE THE BAN ABOVE CERTIFIED A DEFECT IT COULD NOT SEE.
# ``_money_spellings`` builds every candidate by prefixing a sigil — "$", "CA$", "CAD $".
# That is correct for COPY, which is how a price reaches a reader. It is blind to a price
# reaching a reader through CODE, where money is a bare float: ``PATIENT_BASE = 4000.0`` and
# ``{ up_to: 250, unit_price: 8.0 }`` sat inside a file the tenant ban already scanned, and
# the suite reported green over both. A pin that cannot see the regression it exists to
# prevent is worse than no pin, because it certifies.
#
# The shape banned here is deliberately narrow, for the same reason the count ban carries a
# proximity window: a bare number is not evidence of anything. What IS evidence is a number
# that an identifier has already NAMED as money (rule A), or a rate card — two or more of the
# engine's own unit prices on one line, which no unrelated code produces by coincidence
# (rule B). Structural data keeps passing: ``quantity: 5`` names no money and stands alone.

#: Identifier fragments that declare "the number after me is money". Substring match, not
#: word-boundary: the defect was ``PATIENT_BASE`` and ``unit_price``, both compounds.
_MONEY_NAME = re.compile(r"(?:price|rate|cost|fee|amount|base|unit|tier)", re.I)
#: ``NAME = 4000.0`` / ``unit_price: 8.0`` — an assignment or object key holding a numeric
#: literal. The value must be a bare number; ``base_price: PATIENT_BASE`` is a reference to
#: the engine's answer travelling through code and is not a second copy of the figure.
_NAMED_NUMBER = re.compile(r"([A-Za-z_$][\w$]*)\s*[:=]\s*(\d+(?:\.\d+)?)\b")
#: Any of the engine's amounts written as a bare number, for the rate-card rule.
#:
#: ⚠️ MONEY-SHAPED, NOT MERELY NUMERIC, AND THE FIRST CUT OF THIS WAS WRONG. Matching any
#: ``\d+(\.\d+)?`` made "2026-08-04" read as the amounts 8 and 4, and the CGNAT probe's
#: ``/^100\.(6[4-9]|...)/`` read as 6 and 4 — three false hits in files with no price in them.
#: A single- or double-digit integer carries no evidence of being money. So rule B counts a
#: number only when it is written WITH a decimal point (``8.0``, the exact form the clinical
#: rate card used) or is at least three digits (750, 1500, 4000). Rule A is unaffected: there
#: an identifier has already declared the number to be money, so its shape proves nothing more.
_BARE_NUMBER = re.compile(r"(?<![\w.$])(\d+\.\d+|\d{3,})(?![\w%])")


def _forbidden_bare_amounts(ent):
    """Every amount the ENGINE owns, as a float — base packages and per-patient unit rates.

    Resolved from the engine exactly like ``_money_spellings``, so re-pricing a tier moves the
    ban with it instead of quietly retiring it. Amounts below 1 are dropped: a sub-dollar rate
    written bare is indistinguishable from an opacity or a ratio, and banning it would fire on
    CSS-ish code while catching nothing a customer reads.
    """
    out = set()
    for kind in ("tenant", "team", "clinical", "individual_house"):
        v = ent.base_package_price_for(kind)
        if v:
            out.add(float(v))
    out.add(float(ent.patient_addon_base_price()))
    for tier in ent.patient_capacity_tiers():
        up = tier.get("unit_price")
        if up:
            out.add(float(up))
    return sorted(a for a in out if a >= 1.0)


def _sigil_less_money_hits(text, amounts):
    """``[(line, amount, evidence)]`` for money written as a bare number in a money context."""
    wanted = {float(a) for a in amounts}
    hits, seen = [], set()

    def _line_of(pos):
        return text.count("\n", 0, pos) + 1

    # Rule A — an identifier already named it money.
    for m in _NAMED_NUMBER.finditer(text):
        name, raw = m.group(1), m.group(2)
        if not _MONEY_NAME.search(name):
            continue
        try:
            val = float(raw)
        except ValueError:
            continue
        if val not in wanted:
            continue
        line = _line_of(m.start())
        if (line, val, "A") in seen:
            continue
        seen.add((line, val, "A"))
        hits.append((line, val, f"{name} = {raw}"))

    # Rule B — a rate card: two or more distinct engine amounts on one line.
    for idx, raw_line in enumerate(text.split("\n"), start=1):
        found = {float(v) for v in _BARE_NUMBER.findall(raw_line)
                 if _safe_float(v) in wanted}
        if len(found) >= 2:
            key = (idx, tuple(sorted(found)), "B")
            if key in seen:
                continue
            seen.add(key)
            hits.append((idx, sorted(found), raw_line.strip()[:120]))
    return hits


def _safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@pytest.mark.parametrize("fname", sorted({**CONSOLE_FILES, **TENANT_FILES}))
def test_no_engine_amount_is_written_as_a_bare_number_in_either_console(ent, fname):
    """Money without a currency sigil is still money, and the customer still reads it.

    The clinical demo lane held ``PATIENT_BASE = 4000.0`` and the whole per-patient rate card
    as bare floats in tenant.js — a file whose own comment records that the demo is LIVE on the
    production tenant console. A clinical prospect walking it was quoted the base fee and the
    per-patient rates from a script rather than from the billing engine, which is precisely the
    defect this suite was written to end, four thousand lines below where it was fixed.
    """
    amounts = _forbidden_bare_amounts(ent)
    assert amounts, "no engine amounts resolved — this test would be vacuously green"
    text = _without_comments({**CONSOLE_FILES, **TENANT_FILES}[fname])
    hits = _sigil_less_money_hits(text, amounts)
    assert not hits, (
        f"an engine-owned amount is written as a bare number in {fname}:\n  "
        + "\n  ".join(f"{fname}:{ln} → {amt} :: {ev}" for ln, amt, ev in hits)
        + "\nThese are prices. They must be read from the engine (GET /tenant/clinical/plan, "
          "GET /admin/pricing), not typed into a script — a sigil is a rendering detail, not "
          "what makes a number money.")


# ── the PUBLISHED price list — pinned rather than rewritten ────────────────────────
# saas/dashboard/help/index.html is the customer-facing wiki. Its pricing page is hand-authored
# prose with a worked example, and turning a documentation table into a catalog-driven widget is a
# different piece of work with its own build script (build_help.py) and PDF renderer. So the
# figures stay written there — but they STOP being able to drift silently. If somebody moves a
# price in the engine and does not update the published price list, this goes red and names the
# row. A published price that contradicts the invoice is the same defect as a console one, and it
# is the version a customer can screenshot.

_WIKI = DASH / "help" / "index.html"
_WIKI_PRINT = DASH / "help" / "_print.html"


def _money(v):
    """The wiki writes whole dollars bare and part-dollars with cents: $750, $1,500, $12.50."""
    f = float(v)
    return f"${f:,.2f}".replace(".00", "") if f == int(f) else f"${f:,.2f}"


@pytest.mark.parametrize("path", [_WIKI, _WIKI_PRINT])
def test_the_published_price_list_still_agrees_with_the_billing_engine(ent, catalog, path):
    if not path.exists():
        pytest.skip(f"{path.name} is not built in this tree")
    text = path.read_text(encoding="utf-8")
    from saas import plans as plans_mod
    from saas import platform_settings as ps

    expected = {
        "Team base package": _money(catalog["team"]["base_price"]),
        "Enterprise base package": _money(catalog["tenant"]["base_price"]),
    }
    shipped = ps.shipped_seat_rates()
    for kind in ("tenant", "team"):
        for st, cents in shipped.get(kind, {}).items():
            expected[f"{kind} {st} seat rate"] = _money(cents / 100.0)
    for st in ("standard", "advanced", "expert", "agent"):
        expected[f"individual {st} retail"] = _money(plans_mod.individual_price_for(st))

    missing = {k: v for k, v in expected.items() if v not in text}
    assert not missing, (
        f"the published price list in {path.name} no longer states what the engine charges — "
        f"these figures are absent from the page: {missing}. Either the engine moved and the "
        f"published price list was not updated (a customer is reading a price we will not "
        f"honour), or the page was reworded and this pin needs re-anchoring. Check which.")


def test_the_published_price_list_does_not_still_call_team_a_zero_base_tier():
    text = _WIKI.read_text(encoding="utf-8")
    for stale in ("Team</strong></td><td>No base", "Team = no base package", "Team ($0 base)"):
        assert stale not in text, f"the published price list still says {stale!r}"


def _blank(m):
    """Replace a comment with spaces, keeping every newline — so line numbers stay true."""
    return re.sub(r"[^\n]", " ", m.group(0))


def _without_comments(text):
    """Strip ``<!-- -->``, ``/* */`` and WHOLE-LINE ``//`` comments.

    Why the count ban reads a de-commented file and the PRICE ban does not: the code that
    stopped using a figure legitimately NAMES it in the comment explaining why. app.js carries
    ``// This block used to hold `BASE_PACKAGE_PRICE = 1500` and `INCLUDED_STANDARD = 5``` right
    above the demo mock, and index.html quotes the superseded contract wording it corrected. A
    ban that cannot tell a warning from a use forbids explaining the bug, which is how the
    explanation gets deleted instead of the defect. Comments render to nobody.

    Only WHOLE-LINE ``//`` is stripped: a trailing ``//`` is indistinguishable from the ``//``
    in a URL without parsing JavaScript, and over-stripping would blind the ban to real copy.
    """
    text = re.sub(r"<!--.*?-->", _blank, text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", _blank, text, flags=re.S)
    return re.sub(r"(?m)^[ \t]*//.*$", _blank, text)


#: How far either side of a number we look for the words that make it a PACKAGE figure. Wide
#: enough for "a hard total-seat cap of 15 seats" and "included base allowance (5 standard)";
#: narrow enough that an unrelated number in the next clause is not dragged in. Never crosses
#: a line break.
_COUNT_WINDOW = 34
#: The words that turn a bare number into a claim about what a base package INCLUDES or CAPS.
#: ``\b`` on every one of them matters: without it ``base`` matches inside ``base_quantity`` and
#: ``cap`` inside ``seat_cap``, and the ban starts firing on the demo mock's data structures —
#: which are not copy, state no package, and are read by nobody.
_ALLOTMENT_WORD = re.compile(
    r"\b(?:includ\w*|incl|allowance|allot\w*|cap|capped|ceiling|base)\b", re.I)
#: ...and the thing being counted. A count with no noun beside it is not a package figure.
_COUNTED_NOUN = re.compile(r"\b(?:standard|seats?|patients?)\b", re.I)


def _forbidden_counts(ent):
    """The COUNTS the console may not state, resolved from the engine — exactly the way the
    price ban resolves the forbidden AMOUNTS, and for the same reason: a hand-typed list stops
    meaning anything the first time somebody re-ratifies the Team allotment.

    Zero is excluded on purpose. Clinical includes no Standard seats, and banning the digit 0
    near the word "seat" would fire on every empty state in the console while catching nothing
    — a clinic's allotment written as copy is "no Standard seats included", which is words.
    """
    counts = {ent.included_standard_for(k)
              for k in ("tenant", "team", "clinical", "individual_house")}
    counts.add(ent.TEAM_SEAT_CAP)
    counts.add(ent.clinical_included_patients())
    return sorted(c for c in counts if isinstance(c, int) and c > 0)


def _package_count_hits(text, counts):
    """Every place ``text`` states one of ``counts`` inside a sentence already talking about
    seats, allotments or caps. Returns ``[(line, count, window)]``, one per line+count."""
    hits, seen = [], set()
    for n in counts:
        for m in re.finditer(r"\b%d\b" % n, text):
            # The window never crosses a newline: a number at the end of one line and the word
            # "seats" at the start of the next are not one sentence.
            ls = text.rfind("\n", 0, m.start()) + 1
            le = text.find("\n", m.end())
            le = len(text) if le < 0 else le
            win = text[max(ls, m.start() - _COUNT_WINDOW):min(le, m.end() + _COUNT_WINDOW)]
            if not (_ALLOTMENT_WORD.search(win) and _COUNTED_NOUN.search(win)):
                continue
            line = text.count("\n", 0, m.start()) + 1
            if (line, n) in seen:
                continue
            seen.add((line, n))
            hits.append((line, n, win.strip()))
    return hits


def test_no_included_seat_or_seat_cap_count_is_written_as_package_copy():
    """The COUNTS drift exactly like the prices did — "5 Standard included" was in the markup for
    every account of every kind, including Team accounts that include a different number.

    THE SHAPES THIS CONSOLE HAS ACTUALLY USED. Literal phrasings, kept because they are cheap and
    because they name the exact strings that shipped. They are NOT the ban — see the
    engine-derived one below, which exists because a mutation walked straight past every pattern
    here."""
    patterns = [
        (r"\b\d+\s+Standard\s+(?:seats?\s+)?(?:incl\b|included)",
         "an included-Standard count written as copy"),
        (r"\bup to \d+ (?:total )?seats\b", "a seat cap written as copy"),
        (r"\bbase\s+\d+\s+Standard\b", "an included-Standard count written as copy"),
        (r"\bcap\s+\d+\b", "a seat cap written as copy"),
        (r"\b\d+\s+patients\s+included\b", "an included-patient count written as copy"),
    ]
    found = []
    for fname, text in CONSOLE_FILES.items():
        for pat, why in patterns:
            for m in re.finditer(pat, text):
                line = text.count("\n", 0, m.start()) + 1
                found.append(f"{fname}:{line} {why}: {m.group(0)!r}")
    assert not found, (
        "a base-package COUNT is written into the operator console:\n  " + "\n  ".join(found) +
        "\nUse NUMX() over the served catalog / capacity report. These counts are per ACCOUNT "
        "KIND, and a literal states one kind's answer for all of them.")


#: BOTH consoles. The operator's and the CUSTOMER'S — see the parametrized test below for why
#: the tenant side is held to this and not merely to the price ban.
_COUNT_BAN_FILES = {**CONSOLE_FILES, **TENANT_FILES}


@pytest.mark.parametrize("fname", sorted(_COUNT_BAN_FILES))
def test_no_count_the_engine_owns_is_written_into_this_console_file(ent, fname):
    """⚠️ THE ENGINE-DERIVED COUNT BAN, ADDED AFTER A MUTATION WALKED STRAIGHT PAST THE ONE ABOVE.

    Inserting ``a hard total-seat cap of 15 seats`` into feature_copy.js satisfied none of those
    patterns — they look for ``cap 15``, not ``cap of 15 seats`` — and the suite stayed green
    over a re-hardcoded seat cap in the copy an operator reads before creating an account.
    Enumerating phrasings is a losing game: there is always one more way to write a number into
    a sentence. So this inverts it and enumerates the NUMBERS, resolved from
    ``included_standard_for`` / ``TEAM_SEAT_CAP`` / ``clinical_included_patients`` exactly the
    way the price ban resolves the forbidden AMOUNTS — and objects when one of them turns up in
    a sentence that is already talking about seats, allotments or caps. It follows a re-ratified
    allotment for free instead of going quietly stale.

    PRECISION IS THE WHOLE DIFFICULTY, and it is why both a package word and a counted noun are
    required, both with word boundaries. Without that this fires on
    ``<option value="5">5 seats</option>`` (an agent-seat BUCKET picker: 1 / 5 / 10, nothing to
    do with a package) and on ``{ standard: { quantity: 5, base_quantity: 5 } }`` in the demo
    mock's data — neither of which is copy, and neither of which states a package.

    ⚠️ THE TENANT CONSOLE IS IN SCOPE, AND IT IS THE HALF THAT MATTERS MOST. tenant.html /
    tenant.js are read by the CUSTOMER PAYING THE BILL, which is the worse place for a
    remembered number than the operator's own screen. They carried four of these:
    ``note: 'included base allowance (5 standard)'`` and
    ``label: 'Base package — 5 Standard (included)'`` in the demo mock, and — much worse, because
    these were LIVE code and not a mock — ``included_standard || (isTeam ? 2 : 5)`` and
    ``cap.seat_cap || 15`` twice. A ``|| <literal>`` answers confidently with a remembered number
    the instant the engine's answer arrives falsy, which is precisely when the reader most needs
    to be told "unknown". All four are fixed. THIS TEST IS WHAT KEEPS THEM FIXED: without it the
    next ``|| 15`` goes in next month and nothing turns red. The tenant side was outside this ban
    while those sites were still open, deliberately, so the suite carried exactly one intended
    red rather than five — that reason has expired and the scope is now both consoles.

    PARAMETRIZED PER FILE ON PURPOSE. When one file legitimately carries a known red, a single
    whole-console case would let it mask a NEW violation in a different file: the suite still
    reports "1 failed" and nobody looks. Per file, a second offender is a second red.
    """
    counts = _forbidden_counts(ent)
    assert counts, "no package COUNTS resolved from the engine — this ban would be vacuous"
    text = _without_comments(_COUNT_BAN_FILES[fname])
    hits = _package_count_hits(text, counts)
    assert not hits, (
        f"{fname} states a count the billing engine owns, as copy:\n  " +
        "\n  ".join(f"{fname}:{line} states {n} — {win!r}" for line, n, win in hits) +
        f"\nThese counts are per ACCOUNT KIND ({', '.join(str(c) for c in counts)} are the "
        f"engine's current answers) and a literal states one kind's answer for all of them. "
        f"Use NUMX() over the served catalog / capacity report — and if this is a ``|| <count>`` "
        f"fallback, note that a remembered number is a confident wrong answer at exactly the "
        f"moment the engine failed to give a right one.")


def test_team_is_no_longer_described_as_a_zero_base_tier():
    """DEFECT 1, by name. Six sites said Team had no base package long after it had one."""
    stale = [
        r"Team\s*=\s*no base package",
        r"Team[^.\n]{0,60}\$0 base",
        r"Team[^.\n]{0,80}starts with no base package",
        r"Team package:\s*\$0",
        r"\$0-base",
        r"zero-base per-seat tier",
    ]
    found = []
    for fname, text in CONSOLE_FILES.items():
        for pat in stale:
            for m in re.finditer(pat, text, re.I):
                line = text.count("\n", 0, m.start()) + 1
                found.append(f"{fname}:{line}: {m.group(0)!r}")
    assert not found, (
        "the console still describes Team as a zero-base tier:\n  " + "\n  ".join(found))


def test_no_defaulted_price_or_count_fallback_survives():
    """⚠️ ZERO IS NEVER A PRICE, and neither is A REMEMBERED NUMBER. ``tm.base_price || 750`` and
    ``cap.included_standard || 5`` both answer confidently with a literal the instant the engine's
    answer arrives falsy — which is precisely when the operator most needs to be told 'unknown'."""
    bad = re.findall(
        r"(?:base_price|included_standard|seat_cap|included_patients|base_package_price)"
        r"\s*(?:\|\||\?\?)\s*[\d'\"]",
        APPJS)
    assert not bad, (
        f"a base-package field is defaulted to a literal in app.js: {bad}. "
        f"Use CADX()/NUMX() — an unresolvable amount renders 'unknown', never a number we "
        f"remembered and never a currency zero.")


def test_the_console_has_the_two_formatters_and_they_mean_opposite_things():
    """CADX turns an UNRESOLVED amount into "unknown". CAD leaves a RESOLVED zero as a zero,
    because a COMPED account bills zero on purpose, with a signature and a reason, and hiding
    that would be its own defect. Both must exist, and the difference must be written down."""
    assert "const CADX = " in APPJS and "const NUMX = " in APPJS
    assert re.search(r"const CAD\s*=\s*\(n\)\s*=>", APPJS), "CAD() must still exist for real totals"
    head = APPJS[:APPJS.index("const S = {")]
    assert "comp" in head.lower(), (
        "the comment above the formatters must say WHY CAD() still exists — a comped zero is a "
        "real zero and must keep rendering as one")
    # CADX must return the string, not a zero, for a null.
    assert "'unknown'" in APPJS[APPJS.index("const CADX = "):APPJS.index("const NUMX = ")]


# ══════════════════════════════════════════════════════════════════════════════════════
# 2 — THE ENGINE SERVES THE CATALOG, AND IT AGREES WITH ITSELF
# ══════════════════════════════════════════════════════════════════════════════════════

def test_the_catalog_covers_every_kind_the_console_can_name(catalog):
    assert set(catalog) == {"tenant", "team", "clinical", "individual_house"}


def test_every_catalog_figure_comes_from_the_engines_own_resolvers(ent, catalog):
    """Not "equals 750" — equals whatever ``base_package_price_for`` says today, which is the
    same call ``_team_billing_view`` and the invoice builder resolve through."""
    for kind, row in catalog.items():
        assert row["base_price"] == ent.base_package_price_for(kind), kind
        assert row["included_standard"] == ent.included_standard_for(kind), kind
        assert row["currency"] == ent.CURRENCY, kind
        if row["base_price"] is None:
            assert row["base_price_cents"] is None, kind
        else:
            assert row["base_price_cents"] == round(row["base_price"] * 100), kind
    assert catalog["team"]["seat_cap"] == ent.TEAM_SEAT_CAP
    assert catalog["tenant"]["seat_cap"] is None
    assert catalog["clinical"]["included_patients"] == ent.clinical_included_patients()


def test_no_price_in_the_catalog_is_ever_a_zero(catalog):
    """⚠️ ZERO IS NEVER A PRICE (saas/admin.py:1862). A tier that has a base package either
    resolves a real amount or reports None — it never reports 0.0, which would render as a
    confident CA$0.00 and is the exact defect that shipped here before."""
    for kind, row in catalog.items():
        assert row["base_price"] != 0, f"{kind} reports a base price of ZERO"
        assert row["base_price_cents"] != 0, f"{kind} reports a base price of ZERO cents"


def test_no_base_package_and_unresolved_are_different_answers(catalog):
    """Singles genuinely has no tenant base package. That is a FACT, not a failure, and calling
    it "unknown" would invent a doubt about a thing we are certain of. The two states have to be
    distinguishable from the payload alone, or the console cannot tell them apart either."""
    assert catalog["individual_house"]["has_base_package"] is False
    assert catalog["individual_house"]["base_price"] is None
    for kind in ("tenant", "team", "clinical"):
        assert catalog[kind]["has_base_package"] is True, kind
        assert catalog[kind]["base_price"] is not None, kind
    assert "has_base_package" in APPJS, (
        "the console must read has_base_package — without it, 'no base package' and 'we could "
        "not work out the base package' render identically")


def test_the_stripe_tier_catalog_agrees_with_the_engine(catalog):
    """The processor is the system of record for base tiers, so the amount checkout sends and the
    amount the console shows must be the same number. They are two literals in two files today,
    and the only thing stopping them drifting is that somebody checks."""
    from saas import tenant_checkout
    tiers = {t["account_kind"]: t for t in tenant_checkout.tier_catalog()}
    for kind in ("team", "tenant"):
        assert tiers[kind]["amount_cents"] == catalog[kind]["base_price_cents"], (
            f"the checkout catalog charges {tiers[kind]['amount_cents']} cents for {kind} but the "
            f"pricing catalog states {catalog[kind]['base_price_cents']} — the customer's card "
            f"and the operator's screen disagree")
        assert tiers[kind]["seats_included"] == catalog[kind]["included_standard"], kind


def test_the_pricing_endpoint_serves_the_catalog(catalog):
    """The console can only be metadata-driven if the metadata is actually on the wire."""
    from saas import admin
    payload = admin.get_pricing(_op=True)
    assert "account_kinds" in payload, "GET /admin/pricing does not serve the tier catalog"
    assert not payload.get("account_kinds_error"), payload.get("account_kinds_error")
    served = {r["kind"]: r for r in payload["account_kinds"]}
    assert served == catalog


# ══════════════════════════════════════════════════════════════════════════════════════
# 3 — EVERY SURFACE IN THE BAR READS THAT CATALOG
# ══════════════════════════════════════════════════════════════════════════════════════

def test_the_package_chooser_options_carry_only_the_account_kind():
    """The chooser's option TEXT is built by renderTierCatalogCopy(). What ships in the markup is
    the kind and a bare tier name — no amount, no allotment, no cap."""
    m = re.search(r'<select id="ca-kind">(.*?)</select>', INDEX, re.S)
    assert m, "the create-tenant Package chooser is gone"
    opts = re.findall(r'<option value="([a-z_]+)"[^>]*>(.*?)</option>', m.group(1))
    assert [k for k, _ in opts] == ["tenant", "team", "clinical"]
    for kind, text in opts:
        assert not re.search(r"\d", text), (
            f'the "{kind}" option ships a figure in the markup: {text!r} — every amount must be '
            f"painted from the served catalog")
    assert "renderTierCatalogCopy" in APPJS
    assert re.search(r"const sel = \$\('ca-kind'\);", APPJS)


def test_the_creation_confirmation_states_the_catalogs_package_not_its_own():
    """The banner that used to carry ``'$750 base · 2 Standard incl · up to 15 seats'`` — correct
    on the day it was typed, and one price change from being the console's second wrong answer."""
    fn = APPJS[APPJS.index("async function createAccount()"):]
    fn = fn[:fn.index("\n}\n")]
    assert "tierPackageText(account_kind)" in fn, (
        "the confirmation must describe the package from the catalog")
    assert "ensurePricing()" in fn, (
        "this banner is the operator's receipt for a REAL account — it must not render a package "
        "from a catalog that was never fetched")
    assert not re.search(r"\$\d", fn)


def test_the_catalog_is_fetched_before_any_surface_needs_it():
    """A catalog fetched only when the pricing panel is opened leaves the Package chooser blank
    for anyone who goes straight to New Tenant — which is everyone."""
    after = APPJS[APPJS.index("async function afterLogin()"):]
    after = after[:after.index("\n}")]
    assert "ensurePricing()" in after, "the tier catalog must be loaded at sign-in"
    assert "loadPayCfg()" in after, (
        "the payment config must be loaded at sign-in — the rail names the selected processor")
    refresh = APPJS[APPJS.index("function refreshView(name)"):]
    refresh = refresh[:refresh.index("\n}")]
    assert "'newclient'" in refresh and "ensurePricing()" in refresh


# ══════════════════════════════════════════════════════════════════════════════════════
# 3R — ...AND IT REALLY PAINTS THEM. THE RENDERED HALF.
# ══════════════════════════════════════════════════════════════════════════════════════
# This section replaces a set of greps that read
#     assert "CADX(cap.base_package_price)" in plan
#     assert "tierPackageText(" in badge
# and were satisfied by source text alone. Every one of them stayed green under a mutation that
# deleted the single ``renderTierCatalogCopy()`` call inside ``ensurePricing()`` — after which
# the Package chooser reads "Enterprise / Team / Clinical — regulated" forever, the note under
# it reads "resolving the package catalog…" forever, and the badges quote nothing. The console
# was not wrong; it was BLANK, and a suite made of greps could not tell the difference.
#
# So: real index.html, real app.js, real ``ensurePricing`` over a stubbed ``window.fetch``, and
# SENTINEL figures chosen because they exist nowhere else in this repository. If the sentinel is
# on the surface, the surface got it from the payload. Nothing else can explain it.

def _sent_price(v):
    """The console's own spelling of an amount: ``CADX`` → ``'CAD $' + n.toFixed(2)``."""
    return "CAD $%.2f" % float(v)


def test_the_console_formatters_are_opposites_when_the_shipped_code_runs_them(rendered):
    """⚠️ THE COMP INVARIANT, MEASURED RATHER THAN GREPPED. ``test_the_console_has_the_two
    _formatters_and_they_mean_opposite_things`` proves the two functions EXIST and that the word
    "comp" appears above them. It does not evaluate either one — so widening ``CADX``'s guard
    from ``n === null`` to ``!n`` passes that test while turning every comped account's bill
    into "unknown" on the operator's screen.

    A comped zero is a REAL zero: signed, reasoned, audited, and deliberately given away. The
    operator must see ``CAD $0.00`` and know the comp is in force. "unknown" says the console
    could not work out what this customer owes, which is a different and much worse sentence.
    Its exact opposite — an amount that genuinely did not resolve — must never render as money.
    """
    f = rendered["formatters"]
    assert f["CADX_zero"] == "CAD $0.00", (
        f"CADX(0) rendered {f['CADX_zero']!r}. A COMPED account bills a real, deliberate, signed "
        f"zero and it must render as a zero bill — see tests/test_comp_and_trial_survive_"
        f"pricing_ia.py. Hiding it behind 'unknown' misreports a decision a human made and "
        f"signed for.")
    assert f["CADX_zero_string"] == "CAD $0.00", (
        "the wire carries JSON; a zero that arrives as the string '0' is still a comped zero")
    assert f["CADX_null"] == "unknown", (
        f"CADX(null) rendered {f['CADX_null']!r} — an amount the engine could not resolve is "
        f"NOT free, and printing money for it is a confident wrong answer")
    assert f["CADX_undefined"] == "unknown"
    assert f["CADX_empty_string"] == "unknown"
    assert f["CADX_nan"] == "unknown"
    assert f["CADX_750"] == "CAD $750.00", "a resolved amount must render as that amount"
    # NUMX draws the same line for COUNTS: Clinical genuinely includes 0 Standard seats.
    assert f["NUMX_zero"] == "0", (
        f"NUMX(0) rendered {f['NUMX_zero']!r} — a clinical account's included-Standard allotment "
        f"IS zero, and that is an answer, not a missing value")
    assert f["NUMX_null"] == "unknown"
    assert f["NUMX_undefined"] == "unknown"
    # ...and CAD() must keep its opposite behaviour, or somebody will "unify" them.
    assert f["CAD_null"] == "CAD $0.00", (
        "CAD() is the TOTALS formatter and must keep treating a missing value as zero — it is "
        "what renders a comped bill. If it ever starts saying 'unknown', swap the call sites, "
        "do not change the function.")


def test_every_money_surface_renders_the_served_catalog_and_nothing_of_its_own(rendered):
    """THE BAR, EXECUTED. Package chooser · package note · kind-badge tooltips · Plan & Capacity
    · billing overview — each is fed a figure that appears nowhere in this repository, and each
    must be shown to be printing THAT figure.

    This is the test that fails when ``renderTierCatalogCopy()`` is unwired from
    ``ensurePricing()``: the sentinels never reach the DOM and the placeholders survive.
    """
    S = rendered["sentinels"]
    r = rendered["with_catalog"]
    assert r["pricing_state_is_set"], "the console never stored the catalog it fetched"

    opts = {o["value"]: o["text"] for o in r["ca_kind_options"]}
    assert set(opts) == {"tenant", "team", "clinical"}, opts
    for kind in ("tenant", "team", "clinical"):
        want = _sent_price(S[kind]["base_price"])
        assert want in opts[kind], (
            f"the {kind} option in the create-tenant Package chooser reads {opts[kind]!r} and "
            f"does not contain {want} — the served catalog is not reaching the chooser. Either "
            f"renderTierCatalogCopy() is never called (check the call inside ensurePricing) or "
            f"it no longer paints the option text.")
    assert str(S["team"]["seat_cap"]) in opts["team"], (
        "the Team option must state the SERVED seat cap, not a remembered one")
    assert str(S["clinical"]["included_patients"]) in opts["clinical"]

    note = r["ca_package_note"]
    assert "resolving the package catalog" not in note, (
        "#ca-package-note is still showing its markup placeholder — nothing painted it")
    for kind in ("tenant", "team", "clinical"):
        assert _sent_price(S[kind]["base_price"]) in note, (
            f"the package note does not state the served {kind} base package: {note!r}")

    # The clinical "included patients" field hint quotes the clinical base fee.
    assert _sent_price(S["clinical"]["base_price"]) in r["ca_included_note"], r["ca_included_note"]

    # The self-signup approve blurbs — a self-signup always lands on Team.
    assert _sent_price(S["team"]["base_price"]) in (r["signups_team_package"] or "")
    assert _sent_price(S["tenant"]["base_price"]) in (r["signups_ent_package"] or "")

    # The ROSTER badge tooltips. These used to carry base prices as literals, and one of them
    # was still calling Team a zero-base tier long after Team had a base package.
    titles = r["kind_badge_titles"]
    for kind in ("tenant", "team", "clinical"):
        assert _sent_price(S[kind]["base_price"]) in (titles[kind] or ""), (
            f"the {kind} kind-badge tooltip reads {titles[kind]!r} — it is not quoting the "
            f"served catalog")
    assert "Singles" in (titles["individual_house"] or ""), (
        "the Singles badge must keep saying what it is; it has no tenant base package to quote")

    # Plan & Capacity, painted from the CAPACITY report (a different endpoint, its own figures).
    cap = S["capacity"]
    assert str(cap["included_standard"]) in (r["plan_cap_subtitle"] or ""), r["plan_cap_subtitle"]
    assert str(cap["included_standard"]) in (r["plan_included_note"] or ""), r["plan_included_note"]
    term = r["plan_term"] or ""
    assert _sent_price(cap["base_package_price"]) in term, (
        f"Plan & Capacity does not state the capacity report's base package: {term!r}")
    assert str(cap["included_standard"]) in term
    assert str(cap["seat_cap"]) in term
    assert str(cap["term_months"]) in term

    # The global Billing overview's Team block — a THIRD reading of the Team package, and the
    # line that used to carry `|| 750`, `|| 2` and `|| 15` all on its own.
    ov = S["overview"]
    bov = r["bov_summary"] or ""
    assert _sent_price(ov["base_price"]) in bov, (
        f"the billing overview does not state the served Team base price: {bov!r}")
    assert str(ov["included_standard"]) in bov
    assert str(ov["seat_cap"]) in bov


def test_the_catalog_painter_is_wired_to_the_fetch_and_repaints_on_a_new_answer(rendered):
    """The unwiring, isolated so a failure names it directly.

    Nothing in this case calls ``renderTierCatalogCopy`` — the harness sets ``S.pricing = null``,
    serves a payload with a Team base price seen nowhere else, and awaits ``ensurePricing(true)``
    and nothing more. If the chooser is not carrying the new figure afterwards, then fetching the
    catalog does not paint the console, and every "is wired to the catalog" grep in this file is
    describing a mechanism with no trigger.
    """
    r = rendered["repaint_on_ensure_pricing"]
    want = _sent_price(r["fed_team_base_price"])
    assert want in (r["team_option_text"] or ""), (
        f"after ensurePricing() fetched a catalog quoting {want}, the Team option still reads "
        f"{r['team_option_text']!r}. ensurePricing() must call renderTierCatalogCopy() — without "
        f"that one line the whole catalog-painting mechanism is dead code and every surface "
        f"keeps whatever text it was born with.")
    assert want in (r["package_note"] or "")


def test_an_unresolved_base_price_renders_as_words_and_never_as_a_currency_zero(rendered):
    """⚠️ ZERO IS NEVER A PRICE — measured on the shipped renderer rather than asserted about
    its source. ``has_base_package: true`` with ``base_price: null`` is the engine saying "this
    tier HAS a package and I could not price it". Every surface must say so."""
    r = rendered["with_unknown_price"]
    surfaces = {
        "the package note": r["ca_package_note"],
        "the clinical included-patients hint": r["ca_included_note"],
        "Plan & Capacity": r["plan_term"],
        "the billing overview": r["bov_summary"],
    }
    for opt in r["ca_kind_options"]:
        surfaces[f"the {opt['value']} option"] = opt["text"]
    for kind, title in r["kind_badge_titles"].items():
        surfaces[f"the {kind} badge tooltip"] = title
    for name, text in surfaces.items():
        assert "$0.00" not in (text or ""), (
            f"{name} renders an UNRESOLVED base package as a currency zero: {text!r}. A zero is "
            f"what a COMPED account bills; a tier we could not price is 'unknown'.")
    assert "base price unknown" in (r["ca_package_note"] or ""), r["ca_package_note"]
    assert "unknown" in (r["plan_term"] or ""), r["plan_term"]
    # ...and an unresolved COUNT must not fall through to a number either: the overview's Team
    # block carries three fields and all three arrived null.
    assert (r["bov_summary"] or "").count("unknown") >= 3, (
        f"the billing overview should read 'unknown' for the base price, the included allotment "
        f"AND the seat cap when all three are unresolved: {r['bov_summary']!r}")


def test_with_no_catalog_at_all_the_console_invents_no_figure(rendered):
    """The endpoint is unreachable. ``ensurePricing`` swallows the error and leaves ``S.pricing``
    null — which is exactly the moment a ``|| 750`` fallback would have filled the screen with a
    remembered price and the operator would never have known the fetch failed."""
    r = rendered["with_no_catalog"]
    assert r["pricing_state_is_null"], "the console kept a stale catalog after a failed fetch"
    # ``bov_summary`` is deliberately absent from this sweep: its MRR / monthly columns are
    # TOTALS rendered by CAD(), and an empty overview totalling CAD $0.00 is the correct answer
    # to "how much are we billing" — not a remembered package price. Its package fields are
    # asserted below by name instead.
    blobs = ([o["text"] for o in r["ca_kind_options"]]
             + [r["ca_package_note"], r["ca_included_note"], r["plan_term"]]
             + list(r["kind_badge_titles"].values())
             + list(r["tier_package_text"].values()))
    for text in blobs:
        assert not re.search(r"(?:CAD|CA)?\s*\$\s*[\d,]+(?:\.\d\d)?", text or ""), (
            f"with no catalog on hand the console still printed an amount: {text!r} — that is a "
            f"number this file remembered, and it is the whole defect")
    assert "unavailable" in (r["ca_package_note"] or "").lower(), (
        "an unreachable catalog must SAY so; a blank surface reads as 'this tier is free'")
    assert "unknown base" in (r["bov_summary"] or ""), (
        f"the billing overview's Team block must read 'unknown base' when the overview carries "
        f"no base price: {r['bov_summary']!r}")


# ── THE TWO LEVERS THE BAR ADDED, AT THE RENDER LAYER ────────────────────────────────────
# ⚠️ SCOPE NOTE, so nobody duplicates work or assumes somebody else has it.
# The ENGINE half of both of these is already owned, DB-backed and green in
# ``tests/test_comp_and_trial_survive_pricing_ia.py`` (16 passed): the comp overlay's contract at
# its own boundary, the comped zero being a real 0.0 with ``comped_value`` equal to the gross,
# the comp/external roll-ups never leaking into each other, and the individual trial extension
# landing in the DATABASE and re-read through the serving path. Nothing here re-asserts any of
# that — it would be duplication, and duplication of a money test is worse than none because two
# suites then disagree about who owns the rule.
#
# What NOBODY had was the RENDER half, and it is not cosmetic. ``renderBilling`` prints the
# bill's "Total due this month" through ``CADX``. A comped account's total IS ``CADX(0)``. So the
# exact mutation this file exists to catch — widening CADX's guard from ``n === null`` to ``!n``
# — does not merely mis-format a formatter: it replaces a comped customer's zero bill with the
# word "unknown" on the operator's screen, on the one account where the amount is known exactly
# because a human signed for it. The existing console harness drives ``renderBilling()`` with
# ``comped: true`` and reads back only the BUTTON LABEL, so that went unmeasured.

def test_a_comped_account_still_renders_a_zero_bill_with_its_reason_on_screen(rendered):
    """⚠️ THE COMP INVARIANT, ON THE ACTUAL BILL rather than on the formatter in isolation.

    A comp is the ONLY sanctioned zero on this console. It is signed, reasoned and audited, and
    the operator has to be able to read all three off the bill: the gross that is being given
    away, WHY and by WHOM, and a total that says zero rather than shrugging.

    Measured against the SAME renderer rendering the SAME account un-comped, so this cannot pass
    by the panel being empty or the figures never arriving."""
    C = rendered["sentinels"]["comp"]
    gross = "CAD $%.2f" % C["gross"]
    normal = rendered["normal_bill"]["panel"] or ""
    comped = rendered["comped_bill"]["panel"] or ""

    # The control: un-comped, the total is the real amount.
    assert f"Total due this month (CAD){gross}" in normal.replace(" ", "") or gross in normal, (
        f"the un-comped bill does not state its own total: {normal!r}")
    assert "billing normally" in normal, normal

    # The comp: the gross is still visible...
    assert gross in comped, (
        f"a comped bill must keep showing what the comp is WORTH — the gross is the number the "
        f"operator needs to know what is being given away: {comped!r}")
    # ...the credit line carries the reason and who waived it...
    assert C["reason"] in comped, (
        f"the comp's REASON is not on the bill. An unexplained zero is exactly what the comp "
        f"lever exists to prevent: {comped!r}")
    assert C["by"] in comped, "the bill must name who waived it"
    assert ("CAD $-%.2f" % C["gross"]) in comped, (
        "the comp must appear as ONE explicit credit line at minus the gross, never as a "
        "silently reduced subtotal")
    # ...and the total reads a real, deliberate ZERO. Not "unknown".
    assert "CAD $0.00" in comped, (
        f"a COMPED account's bill does not render a zero total: {comped!r}. This is the failure "
        f"that a widened CADX guard produces — the operator is told we could not work out what "
        f"this customer owes, on the one account where we know exactly, because somebody signed "
        f"for it. See tests/test_comp_and_trial_survive_pricing_ia.py for the engine half.")
    assert "unknown" not in comped, (
        f"something on a comped bill rendered 'unknown'. A comp is a KNOWN zero: {comped!r}")
    # ...and the posture is stated, not hidden behind the button.
    assert "comped" in comped and C["until"] in comped, (
        "the comp's posture and its expiry must be visible on the bill — a comp that lapses on "
        "its own must not be a surprise when the bill comes back")
    assert "restore billing" in (rendered["comped_bill"]["comp_button"] or "").lower()


def test_an_extended_individual_trial_still_renders_as_trialing(rendered):
    """The operator's trial-extension lever moves ``individual_users.trial_ends_at``. The
    console's ONLY report that anyone is on trial is the pill on the billing overview, painted
    from ``individuals.trialing``.

    A trial that stops being COUNTED is a trial nobody can see was extended: the individual
    still bills nothing, and the operator has no way to tell that from a billing failure. Pinned
    against the same summary rendered with NO trials, so a pill that is always present — or
    always absent — fails."""
    n = rendered["sentinels"]["comp"]["trialing"]
    with_trials = rendered["trialing_overview"]["bov_summary"] or ""
    without = rendered["no_trials_overview"]["bov_summary"] or ""
    assert f"{n} on first-month-free trial" in with_trials, (
        f"the billing overview does not report the {n} individuals on trial: {with_trials!r}. "
        f"The extension lever is pinned end-to-end in "
        f"tests/test_comp_and_trial_survive_pricing_ia.py; this is the half where the operator "
        f"actually SEES it.")
    assert "trial" not in without, (
        f"the trial pill is rendered even with nobody on trial, so its presence proves nothing: "
        f"{without!r}")


# ══════════════════════════════════════════════════════════════════════════════════════
# 4 — ONE PRICE, ONE ALLOTMENT, ACROSS EVERY SURFACE IN THE BAR
# ══════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("kind", ["team", "tenant"])
def test_one_base_price_and_one_allotment_across_every_producer(ent, catalog, kind):
    """The bar: create a tenant and follow it through, and every surface that mentions its base
    package states the same figures. Here that is asserted at the SOURCE of each surface, so it
    holds for every account of that kind rather than for the one somebody happened to click."""
    from saas import tenant_checkout
    price = catalog[kind]["base_price"]
    incl = catalog[kind]["included_standard"]
    producers = {
        "account_kind_catalog (chooser · confirmation · badges)": (price, incl),
        "base_package_price_for / included_standard_for (capacity report · Plan & Capacity)":
            (ent.base_package_price_for(kind), ent.included_standard_for(kind)),
        "tenant_checkout tier catalog (what the processor charges)": (
            tenant_checkout.get_tier("team" if kind == "team" else "enterprise")["amount_cents"] / 100.0,
            tenant_checkout.get_tier("team" if kind == "team" else "enterprise")["seats_included"]),
    }
    prices = {name: v[0] for name, v in producers.items()}
    incls = {name: v[1] for name, v in producers.items()}
    assert len(set(prices.values())) == 1, f"base price disagrees across surfaces: {prices}"
    assert len(set(incls.values())) == 1, f"included Standard disagrees across surfaces: {incls}"


def test_the_billing_overview_team_block_is_the_same_catalog(ent, catalog):
    """``/admin/billing/overview`` states the Team package in its own three fields. They must be
    the catalog's, not a second reading."""
    src = (ROOT / "saas" / "admin.py").read_text(encoding="utf-8")
    blk = src[src.index('"team": {'):]
    blk = blk[:blk.index("},") + 2]
    assert "team_base_price()" in blk, "the overview must resolve the base price from entitlements"
    assert "TEAM_INCLUDED_STANDARD" in blk
    assert "TEAM_SEAT_CAP" in blk
    assert ent.team_base_price() == catalog["team"]["base_price"]
    assert ent.TEAM_INCLUDED_STANDARD == catalog["team"]["included_standard"]
    assert ent.TEAM_SEAT_CAP == catalog["team"]["seat_cap"]


def test_the_team_invoice_bills_the_catalogs_base_and_allotment(ent, catalog, monkeypatch):
    """The last surface in the bar is the invoice the engine builds. ``_team_billing_view`` must
    charge the catalog's base and forgive the catalog's included allotment — no more, no less."""
    monkeypatch.setattr(ent, "_account_kind", lambda _a: "team")
    monkeypatch.setattr(ent, "get_entitlements", lambda _a: {})
    monkeypatch.setattr(ent, "seat_type_usage", lambda _a: {})
    monkeypatch.setattr(ent, "_contract_term", lambda _a: {"term_months": 6, "expires_at": None})
    monkeypatch.setattr(ent, "ledger_summary",
                        lambda _a: {"periods": [], "grand_total": 0.0, "paid_total": 0.0,
                                    "balance": 0.0})
    monkeypatch.setattr(ent, "_account_tax", lambda _a: {"tax_rate": 0.0, "tax_amount": 0.0})
    monkeypatch.setattr(ent, "scheduled_downgrades", lambda _a: [])
    # The COMP / external-billing overlays are left INTACT on purpose: an account with no comp
    # must come out of them unchanged, which also proves this measures the invoice the engine
    # really builds rather than a pre-overlay draft.
    view = ent._team_billing_view("acct-under-test")
    assert view is not None
    base = [ln for ln in view["lines"] if ln.get("kind") == "base"]
    assert len(base) == 1, f"expected exactly one base line, got {view['lines']}"
    assert float(base[0]["monthly"]) == float(catalog["team"]["base_price"]), (
        "the invoice's base line and the catalog the console reads disagree")
    assert view["included_standard"] == catalog["team"]["included_standard"]
    assert view["base_package_price"] == catalog["team"]["base_price"]


# ══════════════════════════════════════════════════════════════════════════════════════
# 5 — THE PROCESSOR DESTINATION ANNOUNCES WHICH PROCESSOR IT IS
# ══════════════════════════════════════════════════════════════════════════════════════

def test_the_rail_ships_the_neutral_label_and_a_provider_agnostic_view_key():
    """A destination named "Stripe billing" is a lie the moment a second processor is selectable,
    and the catalog has offered five for a while. The MARKUP ships the neutral form; the selected
    processor's own name is resolved at render time."""
    nav = re.search(r'data-remember="nav-sect-money"(.*?)</details>', INDEX, re.S)
    assert nav, "the Money section is gone"
    items = re.findall(r'data-view="([a-z-]+)">([^<]*)</button>', nav.group(1))
    keys = [k for k, _ in items]
    assert keys == ["billing-overview", "provider-config", "payments"], keys
    labels = dict(items)
    assert labels["provider-config"].strip() == "Provider config", labels
    for named in ("Stripe", "stripe", "Paddle", "PayPal", "Lemon"):
        assert named not in nav.group(1), (
            f"the Money rail names {named!r} — the destination must be named for whichever "
            f"processor is SELECTED, not for one forever")


def test_the_label_is_resolved_from_the_served_provider_catalog():
    fn = APPJS[APPJS.index("function renderProviderNav()"):]
    fn = fn[:fn.index("\n}")]
    assert "pricingProvider(null, true)" in fn, (
        "the rail's label must come from the served provider catalog, and from the SAVED "
        "selection — an operator who changes the Config dropdown and navigates away without "
        "saving must not leave the rail announcing a processor that is not collecting")
    assert "'Provider config'" in fn, "there must be a neutral fallback when none is selected"
    assert "nav-item[data-view=\"provider-config\"]" in fn
    for pid in ("'stripe'", '"stripe"', "'paddle'", "'paypal'", "'lemonsqueezy'"):
        assert pid not in fn, f"{pid} is a provider id written into the rail renderer"


def test_the_provider_health_line_never_defaults_to_one_processor():
    """``(h.provider || 'stripe')`` would have told an operator running Paddle that Stripe was
    their processor, on the very card whose job is to say who is collecting."""
    assert "|| 'stripe'" not in BILLJS and '|| "stripe"' not in BILLJS
    assert "h.selected_provider" in BILLJS


# ══════════════════════════════════════════════════════════════════════════════════════
# 6 — DEFECT 2: THE CAPACITY BACK-FILL MUST DERIVE FROM THE ACCOUNT KIND
# ══════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("kind", ["team", "tenant", "clinical", "individual_house"])
def test_a_fresh_account_is_credited_with_ITS_OWN_allotment_not_the_enterprise_one(
        ent, catalog, monkeypatch, kind):
    """``accounts.create_account()`` deliberately seeds the base entitlement for ENTERPRISE only,
    so a fresh Team/Clinical/Singles account has no standard row at all. ``capacity_report`` then
    invented one — unconditionally, with the ENTERPRISE constant, BEFORE it had looked up the
    account's kind. A brand-new Team showed licensed 5 / base 5 while ``included_standard`` two
    fields later in the same payload said 2 and the invoice charged for 2."""
    monkeypatch.setattr(ent, "get_entitlements", lambda _a: {})
    monkeypatch.setattr(ent, "seat_type_usage", lambda _a: {})
    monkeypatch.setattr(ent, "_account_kind", lambda _a: kind)
    monkeypatch.setattr(ent, "_contract_term", lambda _a: {"term_months": 6, "expires_at": None})
    rep = ent.capacity_report("acct-fresh")
    assert rep is not None
    std = next(t for t in rep["types"] if t["seat_type"] == "standard")
    want = catalog[kind]["included_standard"]
    assert std["base"] == want, (
        f"a fresh {kind} account is credited with {std['base']} base Standard seats but its "
        f"package includes {want}")
    assert std["licensed"] == want, (
        f"a fresh {kind} account is LICENSED for {std['licensed']} Standard seats but its "
        f"package includes {want}")
    # ...and the payload must not disagree with itself two fields apart.
    assert rep["included_standard"] == want
    assert rep["base_package_price"] == catalog[kind]["base_price"]


def test_the_same_defect_one_layer_down_writes_no_enterprise_allotment_to_the_database(ent):
    """THE BACK-FILL WAS THE DISPLAY HALF. The WRITE half is ``provision_capacity`` /
    ``provision_bundle``, which stamp ``base_quantity`` onto a brand-new standard row — and both
    read ``BASE_QUANTITY``, the Enterprise allotment, for every account kind.

    That one is worse than a wrong label. ``base_quantity`` is the count billing treats as ALREADY
    PAID FOR inside the base package: ``invoicing._clinical_line_items`` bills a clinic on
    ``quantity - base_quantity``, and a clinic's package includes NO Standard seats. The first
    Standard purchase on a clinical account therefore handed over five seats free.

    Enterprise is untouched by the fix — ``included_standard_for('tenant')`` IS ``BASE_QUANTITY``,
    and ``create_account`` has already seeded that row, so the production path takes the ON
    CONFLICT branch and no money moves.

    ⚠️ THIS IS A GREP AND IT IS NOT THE EVIDENCE. It catches the ORIGINAL spelling and nothing
    else: ``included_standard_for("tenant")`` satisfies both assertions below while restoring
    the whole defect. The evidence is section 6B, which creates real accounts and reads
    ``base_quantity`` back out of the database. Kept because naming the spelling that shipped is
    cheap and because it still fails loudly if somebody reverts the line verbatim."""
    src = (ROOT / "saas" / "entitlements.py").read_text(encoding="utf-8")
    for fname in ("def provision_capacity(", "def provision_bundle("):
        fn = src[src.index(fname):]
        fn = fn[:fn.index("\ndef ", 10)]
        code = "\n".join(ln for ln in fn.splitlines() if not ln.lstrip().startswith("#"))
        bad = re.findall(r"base\s*=\s*BASE_QUANTITY\s+if\s+st\s*==\s*BASE_SEAT_TYPE", code)
        assert not bad, (
            f"{fname.strip('def (')} still stamps the ENTERPRISE allotment as base_quantity for "
            f"every account kind — that writes free seats into the billing state of tiers whose "
            f"package does not include them")
        assert "included_standard_for(" in code, (
            f"{fname.strip('def (')} must derive the included allotment from the account kind")
    # ...and the resolver it must agree with is the same one every other surface reads.
    assert ent.included_standard_for("tenant") == ent.BASE_QUANTITY, (
        "Enterprise must be a no-op under this fix; if these ever diverge the change is no "
        "longer behaviour-preserving for the tier that is already in production")


def test_the_back_fill_reads_the_kind_before_it_uses_it(ent):
    """A structural pin, because the bug WAS the ordering: the back-fill ran above the line that
    worked out the account kind, so it could not have been right for anything but Enterprise."""
    src = (ROOT / "saas" / "entitlements.py").read_text(encoding="utf-8")
    fn = src[src.index("def capacity_report("):]
    fn = fn[:fn.index("\ndef ", 10)]
    # Strip comments: the block deliberately NAMES the constant it stopped using, and a grep that
    # cannot tell a warning from a use would forbid explaining the bug.
    code = "\n".join(ln for ln in fn.splitlines() if not ln.lstrip().startswith("#"))
    kind_at = code.index("kind = _account_kind(")
    assert "BASE_QUANTITY" not in code, (
        "capacity_report still USES BASE_QUANTITY — the ENTERPRISE allotment applied to "
        "every account kind is exactly the defect")
    backfill_at = code.index('if "standard" not in ent')
    assert kind_at < backfill_at, (
        "the standard back-fill still runs BEFORE the account kind is resolved, so it cannot be "
        "deriving from the kind")
    assert "included_standard_for(kind)" in code


# ══════════════════════════════════════════════════════════════════════════════════════
# 6B — ...AND THE ROW IT WROTE SAYS SO. THE DB-BACKED HALF OF DEFECT 2.
# ══════════════════════════════════════════════════════════════════════════════════════
# ⚠️ WHY THIS SECTION HAD TO BE WRITTEN. The test above is a ``re.findall`` for one old spelling
# plus ``"included_standard_for(" in code``. Both of those are satisfied by
#
#     base = included_standard_for("tenant") if st == BASE_SEAT_TYPE else 0
#
# — which is the DEFECT, spelled differently. A review mutated exactly that into both
# ``provision_capacity`` and ``provision_bundle`` and the suite stayed green over a Team account
# being handed the Enterprise allotment. The grep above is kept (it names the original spelling
# and it is free); it is no longer the evidence.
#
# THE EVIDENCE IS THE ROW. ``base_quantity`` is not a label: it is the count billing treats as
# ALREADY PAID FOR inside the base package. ``get_entitlements`` reports
# ``provisioned = quantity - base_quantity`` and ``invoicing._clinical_line_items`` invoices on
# ``provisioned``. Stamp 5 there on a clinic — whose package includes NO Standard seats — and the
# first Standard purchase becomes five free seats and an invoice line that never appears. So:
# make a real account of each kind, call the real writer, read ``public.account_entitlements``
# back, and then look at what the invoice builder does with it.

@pytest.fixture(scope="module", autouse=True)
def _priceia_db_env():
    """Point the SaaS layer at THIS suite's own throwaway control plane, then put it back.

    Module-scoped and autouse because ``saas.db`` reads ``POSTGRES_DSN`` at CONNECT time, not at
    import: a fixture that only ran for the DB tests would still be racing whatever another
    module left in ``os.environ``. Restored on the way out so nothing downstream inherits it.
    """
    old = {k: os.environ.get(k) for k in ("SAAS_MODE", "POSTGRES_DSN")}
    os.environ["SAAS_MODE"] = "1"
    if _dsn_usable():
        os.environ["POSTGRES_DSN"] = DSN
    yield
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture()
def plane():
    """A live control plane plus an account factory THAT CLEANS UP AFTER ITSELF.

    Every account this suite creates is deleted in teardown. ``public.accounts`` is the FK parent
    of ``account_entitlements``, ``account_ledger`` and ``account_users`` with ON DELETE CASCADE,
    so one DELETE takes the entitlement rows and the signed ledger lines with it. Suites in this
    repo have broken each other by leaving accounts behind — this one leaves nothing, including
    when a test fails part-way (the teardown is unconditional).
    """
    import psycopg2
    conn = psycopg2.connect(DSN)
    made = []

    class _Plane:
        def account(self, kind, *, seat_limit=1000):
            """A REAL row in the REAL accounts table — which is what makes this behavioural:
            ``provision_capacity`` resolves the kind through ``_account_kind``, i.e. by reading
            this row, exactly as it does in production."""
            aid = str(uuid.uuid4())
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.accounts (account_id, name, slug, account_kind, "
                    "seat_limit, term_months, expires_at) "
                    "VALUES (%s, %s, %s, %s, %s, 6, NOW() + INTERVAL '90 days')",
                    (aid, f"PRICEIA {kind} {aid[:8]}", f"priceia-{kind}-{aid[:8]}",
                     kind, seat_limit))
            conn.commit()
            made.append(aid)
            return aid

        def seed_entitlement(self, account_id, seat_type, quantity, base_quantity):
            """Write a capacity row DIRECTLY. Deliberately not through a provisioning function:
            what is under test downstream is how the INVOICE reads an allotment, and routing
            through a writer would couple the case to that writer's own guards."""
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.account_entitlements "
                    "(account_id, seat_type, quantity, base_quantity) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (account_id, seat_type) DO UPDATE SET "
                    "quantity = EXCLUDED.quantity, base_quantity = EXCLUDED.base_quantity",
                    (account_id, seat_type, quantity, base_quantity))
            conn.commit()

        def entitlement(self, account_id, seat_type):
            """The ROW, straight out of the database — never the writer's return value. A
            function that reports ``ok: True`` while the row says something else is the exact
            'success shape for a write that did not happen' this codebase keeps paying for."""
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT quantity, base_quantity FROM public.account_entitlements "
                    "WHERE account_id = %s AND seat_type = %s", (account_id, seat_type))
                row = cur.fetchone()
            conn.commit()
            if row is None:
                return None
            return {"quantity": int(row[0]), "base_quantity": int(row[1]),
                    "provisioned": int(row[0]) - int(row[1])}

    try:
        yield _Plane()
    finally:
        if made:
            with conn.cursor() as cur:
                # ``account_ledger`` is append-only, enforced by a trigger: the CASCADE from
                # accounts would be REFUSED and this teardown would leave every test account
                # behind. ``faultline.account_purge`` is the trigger's own sanctioned escape —
                # the transaction-local flag the operator hard-delete path sets — so the cleanup
                # goes through the same door production does rather than around it.
                cur.execute("SET LOCAL faultline.account_purge = 'on'")
                # ``::uuid[]`` because account_id is a uuid column and psycopg2 adapts a Python
                # list of str to text[]; without the cast PostgreSQL refuses the comparison and
                # the cleanup silently becomes an error at teardown — i.e. rows left behind.
                cur.execute("DELETE FROM public.accounts WHERE account_id = ANY(%s::uuid[])",
                            (made,))
            conn.commit()
        conn.close()


@pytest.fixture()
def no_stripe_push(ent, monkeypatch):
    """Silence the post-commit Stripe push. It runs OUTSIDE the capacity transaction, never
    raises and never changes the caller's result (see its docstring), so stubbing it removes a
    network round-trip from every case below without touching a single thing under test."""
    monkeypatch.setattr(ent, "_push_seat_quantities_to_stripe", lambda *a, **k: None)


_SIG = "PRICEIA Behavioural Test"


@requires_db
@pytest.mark.parametrize("kind", ["team", "clinical", "tenant"])
def test_provision_capacity_writes_THIS_KINDS_allotment_as_base_quantity(
        ent, plane, no_stripe_push, kind):
    """A real account of each kind buys Standard capacity, and the row it produced is read back.

    ``base_quantity`` must be ``included_standard_for(<this account's kind>)`` — 2 for Team,
    0 for Clinical, 5 for Enterprise — and NOT the Enterprise 5 for all three. Expected values
    are resolved from the engine, not typed here, so a re-ratified Team allotment moves this
    test with it instead of turning it into a lie.
    """
    _DB_RAN["yes"] = True
    bought = 3
    want = ent.included_standard_for(kind)
    account_id = plane.account(kind)

    r = ent.provision_capacity(account_id, "standard", bought,
                               actor="pytest", signature=_SIG)
    assert r["ok"], f"the purchase itself failed: {r}"

    row = plane.entitlement(account_id, "standard")
    assert row is not None, (
        "provision_capacity reported success and wrote no account_entitlements row at all")
    assert row["base_quantity"] == want, (
        f"a {kind} account was stamped base_quantity={row['base_quantity']} on its first "
        f"Standard purchase, but its base package includes {want} Standard seats. "
        f"base_quantity is what billing treats as ALREADY PAID FOR — {row['base_quantity'] - want} "
        f"seats of this purchase would never be invoiced. This is the WRITE half of the "
        f"per-account-kind allotment defect; provision_capacity must resolve the allotment from "
        f"the account's own kind, not from BASE_QUANTITY / 'tenant'.")
    # The proposed-tuple GREATEST() only ever floors a BRAND-NEW row at its own included
    # allotment — a row below its own base_quantity is a contradiction the CHECK forbids.
    assert row["quantity"] == max(bought, want), row


@requires_db
def test_the_clinic_is_actually_invoiced_for_the_standard_seats_it_bought(
        ent, plane, no_stripe_push):
    """THE MONEY. The paragraph above says a clinic was handed five free Standard seats; this is
    the assertion that measures it, and nothing in the suite made it before.

    A clinical package includes NO Standard seats, so every Standard seat a clinic buys is
    billable. ``invoicing._clinical_line_items`` bills the human-seat lines on
    ``provisioned = quantity - base_quantity``. Stamp the Enterprise 5 as base_quantity and
    ``provisioned`` collapses to 0: the seats exist, the clinic uses them, and the line item
    simply is not on the invoice. No error, no warning — just missing revenue.
    """
    _DB_RAN["yes"] = True
    from saas import invoicing

    bought = 3
    account_id = plane.account("clinical")
    assert ent.included_standard_for("clinical") == 0, (
        "this test's premise is that a clinical package includes no Standard seats")

    r = ent.provision_capacity(account_id, "standard", bought, actor="pytest", signature=_SIG)
    assert r["ok"], r

    row = plane.entitlement(account_id, "standard")
    assert row["quantity"] - row["base_quantity"] == bought, (
        f"the clinic bought {bought} Standard seats and the billable count "
        f"(quantity {row['quantity']} - base_quantity {row['base_quantity']}) is "
        f"{row['quantity'] - row['base_quantity']}. Every seat inside base_quantity is given "
        f"away free.")

    lines, subtotal, base_total = invoicing._clinical_line_items(account_id)
    seat_lines = [ln for ln in lines
                  if str(ln.get("description", "")).lower().startswith("standard seat")]
    assert len(seat_lines) == 1, (
        f"the clinic's invoice has no Standard-seat line at all. It bought {bought} seats. "
        f"Lines built: {[ln.get('description') for ln in lines]}")
    assert seat_lines[0]["quantity"] == bought, seat_lines[0]
    assert seat_lines[0]["amount"] > 0, (
        f"the Standard-seat line is on the invoice for {seat_lines[0]['amount']} — a seat a "
        f"clinic bought is not included in its package and is never free")
    assert subtotal == round(base_total + seat_lines[0]["amount"], 2), (
        f"the clinical subtotal {subtotal} is not the base package {base_total} plus the seats "
        f"{seat_lines[0]['amount']} — something else moved")


@requires_db
@pytest.mark.parametrize("kind", ["team", "clinical", "tenant"])
def test_provision_bundle_writes_THIS_KINDS_allotment_as_base_quantity(
        ent, plane, no_stripe_push, kind):
    """The second writer, and the one that is easy to forget: a BUNDLE is a capacity purchase.

    ``starter_standard_x5`` carries 5 Standard seats. On a Clinical account all five are
    billable (included allotment 0); on a Team, three are (allotment 2); on Enterprise, none
    are (allotment 5) — and that last one is the no-op that proves the fix did not move money
    for the tier already in production.
    """
    _DB_RAN["yes"] = True
    bundle_id = "starter_standard_x5"
    seats = sum(int(i["quantity"]) for i in ent.BUNDLES[bundle_id]["items"]
                if i["seat_type"] == "standard")
    assert seats > 0, f"{bundle_id} no longer carries Standard seats — pick another bundle"
    want = ent.included_standard_for(kind)
    account_id = plane.account(kind)

    r = ent.provision_bundle(account_id, bundle_id, actor="pytest", signature=_SIG)
    assert r["ok"], f"the bundle purchase itself failed: {r}"

    row = plane.entitlement(account_id, "standard")
    assert row is not None, "provision_bundle reported success and wrote no entitlement row"
    assert row["base_quantity"] == want, (
        f"a {kind} account that bought the {bundle_id} bundle was stamped "
        f"base_quantity={row['base_quantity']}; its package includes {want}. provision_bundle "
        f"must resolve the allotment from the account's kind for the same reason "
        f"provision_capacity must — base_quantity is money, not a label.")
    assert row["quantity"] == max(seats, want), row
    assert row["provisioned"] == max(seats, want) - want, (
        f"a {kind} bought {seats} Standard seats in a bundle and only {row['provisioned']} of "
        f"them are billable")


@requires_db
def test_enterprise_is_a_no_op_the_row_still_carries_BASE_QUANTITY(ent, plane, no_stripe_push):
    """The safety half, stated on its own so a regression names itself.

    The whole per-kind change is only landable because it is behaviour-preserving for the tier
    that is already in production: ``included_standard_for('tenant')`` IS ``BASE_QUANTITY``. If
    those two ever diverge, every existing Enterprise account's billable Standard count moves
    silently on its next purchase. Pinned here against the DATABASE, not against the constants.
    """
    _DB_RAN["yes"] = True
    assert ent.included_standard_for("tenant") == ent.BASE_QUANTITY
    account_id = plane.account("tenant")
    r = ent.provision_capacity(account_id, "standard", 1, actor="pytest", signature=_SIG)
    assert r["ok"], r
    row = plane.entitlement(account_id, "standard")
    assert row["base_quantity"] == ent.BASE_QUANTITY, (
        f"an Enterprise account's standard row now carries base_quantity "
        f"{row['base_quantity']} instead of {ent.BASE_QUANTITY} — this change was supposed to "
        f"be a no-op for Enterprise and it is not")


# ── the SAME defect class, one tier over: a GLOBAL default standing in for a PER-ACCOUNT answer ──
# This is the bar's own sentence applied to the patient lane. ``invoicing._clinical_line_items``
# forgave ``clinical_included_patients()`` — the platform-wide default — for EVERY clinic, while
# ``clinical.provision_clinical_account`` seeds the real allotment per account into
# ``account_entitlements(patient).base_quantity`` and an operator may override it at creation
# (``admin.CreateClinicalAccountRequest.included_patients``). Same shape as ``base_quantity``
# above, same consequence: a remembered platform number deciding what a specific customer is
# billed.
#
# WHY IT WAS NOT ALREADY PINNED, WHICH IS THE INTERESTING PART. ``tests/test_saas_invoicing.py
# ::test_build_invoice_clinical_itemises_base_and_overage`` monkeypatches ``clinical_plan`` to
# ``{"licensed": 300}`` — a dict with NO ``included_patients`` key — and monkeypatches
# ``clinical_included_patients`` to 250. So it exercises the global-default FALLBACK and only
# that. It is byte-identical green with the defect present and with it fixed. A stub that omits
# the field under test cannot fail on it; that is why this pair is DB-backed and reads the
# invoice the builder really produces from a row that really exists.

@requires_db
@pytest.mark.parametrize("direction", ["above_the_default", "below_the_default"])
def test_a_clinics_overage_is_forgiven_against_ITS_OWN_patient_allotment(
        ent, plane, no_stripe_push, direction):
    """A clinic is invoiced on the allotment ITS package includes, not on the platform default.

    BOTH DIRECTIONS, because they fail differently and only one of them is loud:

      ABOVE — a clinic seeded higher than the default is OVER-billed. Measured on a real
        database before the fix: seeded at 400, invoiced CA$900/mo of "overage" for 150 patients
        its own base package includes — under a base line whose own text claimed 250 were
        included. The invoice contradicted the account, in the customer's disfavour, and said so
        on its own face.

      BELOW — a clinic seeded lower than the default is UNDER-billed, and there is no line item
        at all to notice. Nothing on the invoice is wrong-looking; revenue is simply absent. That
        is the one that survives a review, so it is asserted as hard as the other.

    The allotments are DERIVED from the engine's current default and asserted to differ from it,
    so this cannot quietly become a test of the default agreeing with itself.
    """
    _DB_RAN["yes"] = True
    from saas import invoicing

    default = ent.clinical_included_patients()
    allotment = default + 150 if direction == "above_the_default" else max(1, default // 2)
    assert allotment != default, (
        "this case only means something if the account's allotment DIFFERS from the platform "
        "default — otherwise the buggy and correct readings coincide and it proves nothing")
    licensed = allotment + 50

    account_id = plane.account("clinical")
    plane.seed_entitlement(account_id, "patient", licensed, allotment)

    lines, subtotal, base_total = invoicing._clinical_line_items(account_id)

    # 1 — the BASE line must describe THIS clinic's package, not the platform's.
    base_desc = lines[0]["description"]
    assert f"{allotment} patient capacity incl." in base_desc, (
        f"the base line reads {base_desc!r} — it is quoting an allotment that is not this "
        f"account's ({allotment}). An invoice whose own text contradicts the account it bills is "
        f"the version the customer screenshots.")
    assert f"{default} patient" not in base_desc or default == allotment, base_desc

    # 2 — the OVERAGE must be measured from this clinic's allotment.
    over = [ln for ln in lines if "overage" in str(ln.get("description", "")).lower()]
    assert len(over) == 1, (
        f"expected exactly one patient-overage line for a clinic licensed {licensed} against an "
        f"allotment of {allotment} (= 50 billable). Lines built: "
        f"{[ln.get('description') for ln in lines]}. Under the defect a clinic seeded BELOW the "
        f"platform default produces NO overage line at all — silent under-billing.")
    assert over[0]["quantity"] == 50, (
        f"the clinic is billed for {over[0]['quantity']} patients of overage; it is licensed for "
        f"{licensed} against its own included {allotment}, so exactly 50 are billable. A count "
        f"derived from the platform default ({default}) would be "
        f"{max(0, licensed - default)}.")
    assert f"beyond {allotment} included" in over[0]["description"], over[0]["description"]

    # 3 — and the money, resolved from the engine's ladder rather than typed here.
    unit = ent.patient_unit_price(licensed)
    assert over[0]["amount"] == round(50 * unit, 2), over[0]
    assert subtotal == round(base_total + over[0]["amount"], 2), (
        f"the clinical subtotal {subtotal} is not the base package {base_total} plus the patient "
        f"overage {over[0]['amount']}")


@requires_db
def test_a_clinic_inside_its_allotment_is_billed_no_overage_at_all(
        ent, plane, no_stripe_push):
    """The boundary, and the one a fencepost error breaks: a clinic licensed EXACTLY up to its
    own allotment owes the base package and nothing else. Off by one here and every clinic in
    the estate gets an overage line for capacity it already paid for inside the base."""
    _DB_RAN["yes"] = True
    from saas import invoicing

    allotment = ent.clinical_included_patients() + 150
    account_id = plane.account("clinical")
    plane.seed_entitlement(account_id, "patient", allotment, allotment)

    lines, subtotal, base_total = invoicing._clinical_line_items(account_id)
    over = [ln for ln in lines if "overage" in str(ln.get("description", "")).lower()]
    assert not over, (
        f"a clinic licensed exactly to its included allotment ({allotment}) was billed overage: "
        f"{over}. Those patients are inside the base package it already pays for.")
    assert subtotal == base_total, (
        f"subtotal {subtotal} exceeds the base package {base_total} with no overage line to "
        f"explain the difference")


@requires_db
def test_a_non_standard_seat_type_is_never_stamped_with_an_included_allotment(
        ent, plane, no_stripe_push):
    """The other direction of the same bug. Only the BASE seat type has an included allotment;
    an Expert seat is à-la-carte on every tier. A base_quantity above zero here would give away
    the most expensive seats in the catalog."""
    _DB_RAN["yes"] = True
    for kind in ("team", "clinical", "tenant"):
        account_id = plane.account(kind)
        r = ent.provision_capacity(account_id, "expert", 2, actor="pytest", signature=_SIG)
        assert r["ok"], r
        row = plane.entitlement(account_id, "expert")
        assert row == {"quantity": 2, "base_quantity": 0, "provisioned": 2}, (
            f"a {kind} account's Expert entitlement came out as {row} — no package includes "
            f"Expert seats, so all of them must be billable")


# ══════════════════════════════════════════════════════════════════════════════════════
# 7 — the guard against a false green
# ══════════════════════════════════════════════════════════════════════════════════════

def test_no_test_in_this_file_was_silently_skipped(ent, catalog):
    """A suite whose evidence quietly failed to run reports a cheerful green over nothing. This
    file now has THREE lanes and each one can vanish for its own reason, so each is accounted
    for here rather than trusted.

      * THE IMPORT LANE — the static bans. A module whose fixtures failed to import, or whose
        file paths went stale, would grep four empty strings and pass every one.

      * THE DB LANE (section 6B) — a HARD RED when it did not run, deliberately, and the same
        choice ``tests/test_scheduled_downgrade_applier.py`` makes. What it pins is MONEY written
        to a table, it cannot be pinned any other way, and "all passed" over an unexecuted money
        writer is worse than a failure. Supply the DSN; the remedy is in the message.

      * THE RENDERED LANE (section 3R) — a NAMED skip, not a hard red, because node is a real
        external toolchain and a machine-dependent red gets a suite labelled broken and then
        ignored (see the box at the top of tests/test_operator_console_ux.py, which is that
        lesson written down). What is asserted here is that it either RAN or said out loud why
        it did not: a lane that disappears without a reason is the failure mode that matters.
    """
    assert hasattr(ent, "account_kind_catalog")
    assert catalog["team"]["base_price"] is not None
    assert len(CONSOLE_FILES) == 4
    assert all(len(v) > 5000 for v in CONSOLE_FILES.values()), (
        "a console file read as near-empty — the path is wrong and every grep above is vacuous")

    assert _RENDER["ran"] or _RENDER["skipped_because"], (
        "the RENDERED lane neither ran nor reported a reason — the harness fixture was never "
        "reached, so tests/dashboard/render_money_surfaces.js proved nothing this run and "
        "nothing said so. Run:  " + INSTALL_HINT)

    assert _DB_RAN["yes"], (
        "the DATABASE lane of this suite did not run — every provision_capacity / "
        "provision_bundle case was SKIPPED, and those are the only tests here that measure what "
        "is WRITTEN to public.account_entitlements. Make your own throwaway control plane and "
        "point this suite at it:\n"
        "  psql postgresql://faultline:faultline@172.20.0.6:5432/postgres -c \"CREATE DATABASE "
        "faultline_priceia_beh_test TEMPLATE faultline_pricepanel_test;\"\n"
        "  PRICEIA_TEST_DSN=postgresql://faultline:faultline@172.20.0.6:5432/"
        "faultline_priceia_beh_test python3 tools/fltest.py --bug PRICEIA --test "
        "tests/test_console_prices_trace_to_the_engine.py\n"
        "This tripwire is the intended behaviour, not a defect.")
