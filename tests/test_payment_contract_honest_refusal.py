"""The tenant subscribe flow must tell the truth about a PERMANENT signature.

WHAT THIS SUITE GUARDS, and why each property is here rather than being obvious:

  * THE STATUS ENUM IS CLOSED. ``begin_subscription`` used to return the payment ADAPTER's own
    status string on a step-4 failure (``checkout_failed`` / ``invalid_return_url`` /
    ``unconfigured``). None of those are in the transport's ``_STATUS_HTTP`` table, so they fell
    through its ``.get(..., 502)`` default and the customer got an HTTP code that no line of
    code had chosen. Two tests pin this from both ends: the module must emit only documented
    statuses, and every documented status must be mapped.

  * THE ONE REFUSAL THAT WROTE A ROW MUST SAY SO. The step-4 failure is the ONLY refusal in the
    flow that happens after the permanent ``signed_contracts`` row is committed. It returned a
    bare string, the page took its "outcome unknown" branch, and a customer was told we could
    not confirm whether their signature had been recorded — about a row the server could name.

  * ``signed`` IS DERIVED, NEVER ARGUED. It is true exactly when a contract_id is carried, so
    the two cannot disagree and a refusal added later inherits the right answer.

  * A COMPED ACCOUNT IS NOT OFFERED A PAID CONTRACT, and the refusal writes nothing.

  * THE SUCCESS CASE IS PINNED TOO. Five defects in this repo came from a guard written against
    an assumed response shape, so the success shape is asserted here alongside the refusals.

TWO LAYERS. Module/route tests with fakes always run. The DB-backed tests drive the REAL route
against a REAL migrated PostgreSQL (``PAYCONTRACT_TEST_DSN``, migrations >= 131) with only the
``stripe`` library substituted, because "was a row written" is a property of the DATABASE and a
mock cannot falsify it.

NO real Stripe call is made anywhere in this file.
"""

import json
import os
import re
import sys
import types
import uuid
from pathlib import Path

import pytest

from saas import tenant_checkout as tc
from saas import tenant_checkout_routes as tcr

REPO = Path(__file__).resolve().parent.parent
DSN = os.environ.get("PAYCONTRACT_TEST_DSN", "")

# FALSE-GREEN GUARD. A run with no DSN would otherwise report a comfortable "all passed" while
# every property that needs a real database was silently skipped. The last test in this file
# asserts this flipped.
_DB_RAN = {"value": False}


# ══════════════════════════════════════════════════════════════════════════════════
# 1. THE STATUS ENUM IS CLOSED — both directions
# ══════════════════════════════════════════════════════════════════════════════════

def _documented_statuses() -> set:
    """The status enum as ``begin_subscription``'s own docstring publishes it.

    Parsed from the docstring on purpose. The defect this suite exists for was a docstring that
    said "· <the adapter's own failure status>" — an enum with an open tail — while the
    transport mapped a closed set. Reading the doc here means the two can never drift again
    without a test noticing."""
    doc = tc.begin_subscription.__doc__ or ""
    m = re.search(r"``status`` values[^\n]*\n(.*?)\n\s*\n", doc, re.S)
    assert m, "begin_subscription's docstring no longer publishes its status enum"
    block = m.group(1)
    return {w for w in re.findall(r"[a-z_]{4,}", block)} - {"status", "values", "closed",
                                                            "every", "them", "mapped", "the",
                                                            "http", "transport", "one", "and",
                                                            "set", "that", "this", "with"}


def test_the_status_enum_has_no_open_tail():
    """🔴 THE HEADLINE. The docstring must not admit "the adapter's own failure status".

    That phrase was the defect in one line: it made the enum open, and everything the tail
    admitted arrived at the customer as an unchosen 502."""
    doc = tc.begin_subscription.__doc__ or ""
    head = doc.split("⚠️")[0]      # the ⚠️ block below DESCRIBES the old wording; exempt it
    assert "adapter's own failure status" not in head
    # and the words the tail used to admit are not published as this function's own statuses
    for leaked in ("provider_not_configured", "missing_price"):
        assert leaked not in head, f"{leaked!r} is the ADAPTER's vocabulary, not this enum's"


def test_every_documented_status_is_mapped_by_the_transport():
    """A status the transport cannot map becomes a 502 nobody decided on.

    This is the test that would have caught the original defect at the seam where it lived."""
    mapped = set(tcr._STATUS_HTTP) | {"checkout_created"}
    unmapped = _documented_statuses() - mapped
    assert not unmapped, (
        f"{sorted(unmapped)} are documented statuses with no HTTP mapping — each would fall "
        f"through _STATUS_HTTP.get(..., 502) and reach a customer as an accidental 502")


def test_checkout_unavailable_is_mapped_on_purpose_and_not_by_default():
    """The 502 must be a DECISION. Reading it out of the table proves it is one."""
    assert tcr._STATUS_HTTP["checkout_unavailable"] == 502


def test_a_comped_account_refusal_is_a_conflict_not_a_permission_error():
    """409, not 403. The customer is authorized; the SERVER STATE makes the purchase wrong."""
    assert tcr._STATUS_HTTP["account_is_comped"] == 409


def test_our_own_faults_are_never_blamed_on_the_customer():
    """``no_active_terms`` and ``contract_write_failed`` are OURS. 5xx, never 4xx."""
    for ours in ("no_active_terms", "contract_write_failed", "payments_unavailable",
                 "checkout_unavailable"):
        assert tcr._STATUS_HTTP[ours] >= 500, f"{ours} is our fault and must not be a 4xx"


# ══════════════════════════════════════════════════════════════════════════════════
# 2. ``signed`` IS DERIVED FROM contract_id, NOT ARGUED
# ══════════════════════════════════════════════════════════════════════════════════

def test_signed_is_true_exactly_when_a_contract_id_is_carried():
    """The two facts cannot disagree, because only one of them is an input."""
    assert tc._fail("x", "r", contract_id=41)["signed"] is True
    assert tc._fail("x", "r", contract_id=41)["contract_id"] == 41
    assert tc._fail("x", "r")["signed"] is False
    assert tc._fail("x", "r")["contract_id"] is None


def test_a_refusal_can_never_claim_a_charge():
    """No refusal this module can make follows money moving — it returns at most a session url."""
    assert tc._fail("x", "r", contract_id=41)["charged"] is False
    assert tc._fail("x", "r")["charged"] is False


def test_signed_cannot_be_passed_in_as_an_argument():
    """If ``signed`` were an argument a caller could assert a permanent record that is not there.

    Driven, not grepped: passing it must not be honoured."""
    with pytest.raises(TypeError):
        tc._fail("x", "r", signed=True)          # type: ignore[call-arg]


# ══════════════════════════════════════════════════════════════════════════════════
# 3. THE REFUSAL BODY THE TRANSPORT BUILDS
# ══════════════════════════════════════════════════════════════════════════════════

def test_a_refusal_that_wrote_a_row_names_it_and_offers_the_archive():
    out = {"status": "checkout_unavailable", "signed": True, "charged": False, "contract_id": 77}
    body = tcr._refusal_detail(out, "Stripe checkout could not be created. No charge was made.")
    assert body["signed"] is True
    assert body["charged"] is False
    assert body["contract_id"] == 77
    assert body["contracts_url"] == tcr._CONTRACT_ARCHIVE_PATH


def test_a_refusal_that_wrote_nothing_offers_no_contract_number():
    """A number quoted beside "your signature was recorded" must never be one we invented."""
    body = tcr._refusal_detail(
        {"status": "payments_unavailable", "signed": False, "charged": False,
         "contract_id": None}, "nope")
    assert body["signed"] is False
    assert "contract_id" not in body
    assert "contracts_url" not in body


def test_the_transport_never_re_derives_signed_from_the_status_code():
    """BRAIN NOT TRANSPORT. If the module says nothing was written, the body says so — even on
    a status whose HTTP code is the one that usually means a row exists."""
    body = tcr._refusal_detail(
        {"status": "checkout_unavailable", "signed": False, "charged": False,
         "contract_id": None}, "x")
    assert body["signed"] is False
    assert "contract_id" not in body


def test_the_archive_link_is_a_readable_page_not_the_json_api():
    """A customer told their contract may be unrecorded must be sent somewhere they can READ."""
    assert tcr._CONTRACT_ARCHIVE_PATH.endswith(".html")
    assert (REPO / "saas" / "dashboard" /
            tcr._CONTRACT_ARCHIVE_PATH.rsplit("/", 1)[-1]).is_file()


def test_the_stale_document_refusals_still_carry_the_terms():
    """The pre-existing 409 behaviour must survive the generalisation."""
    body = tcr._refusal_detail(
        {"status": "terms_changed", "signed": False, "charged": False, "contract_id": None,
         "terms": {"id": 3, "version": "v9", "body_markdown": "x", "body_sha256": "a"}}, "moved")
    assert body["terms"]["version"] == "v9"


# ══════════════════════════════════════════════════════════════════════════════════
# 4. THE PAGE — it must answer, without becoming optimistic
# ══════════════════════════════════════════════════════════════════════════════════

def _subscribe_js() -> str:
    """subscribe.js with comments STRIPPED.

    A file that DOCUMENTS a rule must not be indistinguishable from one that breaks it — three
    tests in this repo's history tripped on their own explanatory comments."""
    src = (REPO / "saas" / "dashboard" / "subscribe.js").read_text()
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"(?m)^\s*//.*$", " ", src)
    return src


def test_the_page_renders_a_recorded_signature_only_on_an_explicit_server_assertion():
    """It must key on ``signed === true`` from the body — never on the status code, and never
    on the mere absence of a field. That distinction is what keeps it honest rather than
    optimistic."""
    js = _subscribe_js()
    assert "d.signed === true" in js, (
        "the recorded-signature branch must trigger on an explicit server assertion")
    assert "Your signature WAS recorded" in js


def test_the_page_still_locks_when_the_outcome_is_genuinely_unknown():
    """A network fault or a 5xx carrying no assertion must keep the honest refusal AND the lock,
    so this fix can never be read as "assume it worked"."""
    js = _subscribe_js()
    assert "We could not confirm whether your signature was recorded" in js
    assert "Do NOT sign again" in js


def test_the_unknown_branch_is_no_longer_a_dead_end():
    """The charter's requirement: if the answer is not knowable, offer a way to FIND OUT.

    The two branches reach the archive differently ON PURPOSE and both are asserted: the
    recorded-signature branch uses the url the SERVER sent (so the door and the record come
    from the same read), while the genuinely-unknown branch has no server body to read and
    falls back to the page's own constant."""
    js = _subscribe_js()
    assert "d.contracts_url" in js, (
        "the recorded-signature branch must offer the archive the server named")
    assert "agreements.html" in js, (
        "the genuinely-unknown branch must still offer an archive door of its own")


def test_the_page_never_states_a_charge_it_was_not_told_about():
    js = _subscribe_js()
    assert "d.charged === true" in js


def test_the_comped_account_is_refused_the_signature_surface_on_the_page():
    js = _subscribe_js()
    assert "c.active === true" in js, "comp must be trusted only on an explicit active flag"
    assert "S.comp" in js and "refuseSignature" in js


def test_the_message_link_is_built_as_dom_not_as_markup():
    """The dashboard CSP is script-src 'self' with no unsafe-inline, and the link label carries
    server-supplied text — so it must be built with createElement/textContent.

    innerHTML is permitted for exactly one thing: CLEARING a box. Anything assigned to innerHTML
    other than an empty string is content built as markup, which is what this forbids."""
    js = _subscribe_js()
    # Scoped to msg() -- the function this work changed. The page also renders the AGREEMENT via
    # its own small markdown renderer into innerHTML, which is pre-existing, deliberate and
    # documented there; this test is not a licence to police it.
    m = re.search(r"function msg\(id, kind, text, action\) \{(.*?)\n  \}", js, re.S)
    assert m, "msg() no longer has the action parameter"
    body = m.group(1)
    bad = [x for x in re.findall(r"innerHTML\s*=\s*([^;]+);", body)
           if x.strip() not in ("''", '""')]
    assert not bad, f"msg() assigned built content to innerHTML: {bad}"
    assert "createElement('a')" in body and "a.textContent" in body


def test_the_action_link_href_is_validated_before_it_reaches_an_anchor():
    """One caller takes the href from a RESPONSE BODY. "our own server sent it" is not a reason
    to hand an arbitrary string to an anchor on the page that handles signatures."""
    js = _subscribe_js()
    m = re.search(r"test\(String\(action\.href\)\)", js)
    assert m, "msg() must validate the action href"
    assert "^\\/[A-Za-z0-9._~\\/-]*$" in js or "^/[A-Za-z0-9._~/-]*$" in js.replace("\\", "")


# ══════════════════════════════════════════════════════════════════════════════════
# 5. DB-BACKED — the REAL route, the REAL module, the REAL SQL
# ══════════════════════════════════════════════════════════════════════════════════

pytestmark_db = pytest.mark.skipif(not DSN, reason="PAYCONTRACT_TEST_DSN not set")


@pytest.fixture(scope="module")
def bench():
    """A real migrated control plane + the real route, with ONLY the stripe library replaced."""
    if not DSN:
        pytest.skip("PAYCONTRACT_TEST_DSN not set")
    import psycopg2

    # ⚠️ EVERY ENV CHANGE HERE IS RESTORED ON TEARDOWN, AND THAT IS NOT TIDINESS.
    # This fixture is module-scoped, so it cannot use monkeypatch, and a bare
    # ``os.environ[...] = ...`` leaks for the rest of the pytest PROCESS. It did: setting
    # SAAS_PUBLIC_BASE_URL gave ``payments_stripe.create_checkout`` a public origin to build a
    # default return url from, which made
    # ``test_tenant_checkout_module.py::test_adapter_REFUSES_a_non_absolute_return_url`` go red
    # on its ""/None cases — in a DIFFERENT FILE, only when this one was collected alongside it,
    # and it read exactly like a product regression. A test that changes global state must put
    # it back.
    _env_saved = {k: os.environ.get(k) for k in
                  ("POSTGRES_DSN", "SAAS_MODE", "SAAS_SESSION_SECRET",
                   "SAAS_PUBLIC_BASE_URL", "SAAS_LLM_CRED_KEY")}
    os.environ["POSTGRES_DSN"] = DSN
    os.environ["SAAS_MODE"] = "1"
    os.environ.setdefault("SAAS_SESSION_SECRET", "paycontract-suite-secret-0123456789")
    os.environ.setdefault("SAAS_PUBLIC_BASE_URL", "https://app.example.test")
    os.environ.setdefault("SAAS_LLM_CRED_KEY", _fernet_key())

    # Same problem, same rule: ``ps._stripe`` is replaced below and must be put back.
    from saas import payments_stripe as _ps_mod
    _stripe_saved = _ps_mod._stripe

    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()

    body = ("# Agreement\n\nGoverned by the laws of the Province of Ontario.\n\n1. Fees apply.\n")
    version = "paycontract-" + uuid.uuid4().hex[:8]
    cur.execute("UPDATE public.contract_templates SET retired_at = now() "
                "WHERE retired_at IS NULL")
    cur.execute("INSERT INTO public.contract_templates (version, title, body_markdown, "
                "effective_at, retired_at) VALUES (%s,%s,%s, now(), NULL) RETURNING id",
                (version, "Agreement", body))
    template_id = cur.fetchone()[0]
    cur.execute("SELECT body_sha256 FROM public.contract_templates WHERE id=%s", (template_id,))
    sha = cur.fetchone()[0]

    from saas import crypto as _crypto
    cur.execute("DELETE FROM public.payment_config WHERE singleton = TRUE")
    cur.execute(
        "INSERT INTO public.payment_config (singleton, provider, mode, api_key_ciphertext, "
        "api_secret_ciphertext, webhook_secret_ciphertext, enabled_methods_json, currency, "
        "configured) VALUES (TRUE,'stripe','test',%s,%s,%s,%s,'CAD',true)",
        (_crypto.encrypt("pk_test_x"), _crypto.encrypt("sk_test_x"),
         _crypto.encrypt("whsec_x"), json.dumps(["card"])))

    mode = {"arm": "raise", "status": None}

    class _Boom(Exception):
        pass

    def _create(**params):
        if mode["arm"] == "raise":
            raise _Boom("connection error to api.stripe.com")
        if mode["arm"] == "badstatus":
            # The adapter's OWN non-exception refusal shape (e.g. invalid_return_url).
            raise AssertionError("unused")
        sid = "cs_suite_" + uuid.uuid4().hex[:16]
        return types.SimpleNamespace(id=sid, url="https://checkout.stripe.com/c/pay/" + sid)

    fake = types.ModuleType("stripe")
    fake.checkout = types.SimpleNamespace(Session=types.SimpleNamespace(create=_create))
    fake.api_key = None
    fake.api_version = None
    from saas import payments_stripe as ps
    ps._stripe = fake

    from saas import dashboard_auth as _auth
    import saas.contract_notices as _cn
    _cn.deliver_on_signature_async = lambda cid: True
    tcr._audit.record = lambda *a, **k: None

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(tcr.router)
    client = TestClient(app, raise_server_exceptions=False)

    state = {"conn": conn, "cur": cur, "template_id": template_id, "version": version,
             "sha": sha, "mode": mode, "client": client, "auth": _auth}
    _DB_RAN["value"] = True
    yield state
    conn.close()
    ps._stripe = _stripe_saved
    for k, v in _env_saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _fernet_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def _subscribe(bench, *, comped=False, arm="raise", tier="team"):
    """Sign as a FRESH account each time (the flow refuses a second signature in flight)."""
    cur = bench["cur"]
    _auth = bench["auth"]
    bench["mode"]["arm"] = arm
    acct, admin = str(uuid.uuid4()), str(uuid.uuid4())
    email = f"o+{acct[:8]}@example.test"
    cur.execute("INSERT INTO public.accounts (account_id, name, slug, status) "
                "VALUES (%s,%s,%s,'active')", (acct, "Suite Co", "suite-" + acct[:8]))
    if comped:
        cur.execute("UPDATE public.accounts SET comped=true, comp_reason=%s WHERE account_id=%s",
                    ("suite comp", acct))
    _auth.get_user_by_id = lambda uid, _a=acct, _ad=admin, _e=email: (
        {"id": _ad, "account_id": _a, "username": _e, "password_hash": "x", "totp_secret": None,
         "mfa_enabled": False, "role": "admin", "status": "active", "display_name": None}
        if str(uid) == _ad else None)
    _auth.can_view_billing = lambda uid: True
    c = bench["client"]
    c.cookies.clear()
    c.cookies.set(_auth.SESSION_COOKIE_NAME,
                  _auth.issue_session(dashboard_user_id=admin, account_id=acct, username=email))
    cur.execute("SELECT count(*) FROM public.signed_contracts")
    before = cur.fetchone()[0]
    r = c.post("/tenant/subscribe",
               headers={"x-csrf-token": _auth.issue_csrf(dashboard_user_id=admin)},
               json={"tier": tier, "legal_name": "Christopher Probe", "accept": True,
                     "contract_template_id": bench["template_id"],
                     "terms_version": bench["version"], "terms_sha256": bench["sha"]})
    cur.execute("SELECT count(*) FROM public.signed_contracts")
    after = cur.fetchone()[0]
    return r, after - before, acct, admin


@pytestmark_db
def test_DB_the_reported_failure_now_names_the_permanent_record(bench):
    """🔴 THE REPORTED BUG. A Stripe fault after the signature landed must say the signature
    landed, and must not tell the customer we cannot confirm it."""
    r, rows, _acct, _admin = _subscribe(bench, arm="raise")
    assert r.status_code == 502
    d = r.json()["detail"]
    assert isinstance(d, dict), "a bare string is what made this unanswerable"
    assert d["status"] == "checkout_unavailable"
    assert d["signed"] is True
    assert d["charged"] is False
    assert isinstance(d["contract_id"], int)
    assert d["contracts_url"].endswith(".html")
    assert rows == 1, "the step-4 failure DOES write a permanent row — that is the whole point"


@pytestmark_db
def test_DB_the_named_contract_is_really_there_and_the_archive_can_show_it(bench):
    """Verify the READ, not just the write: the number we quote must resolve for the signer."""
    r, _rows, acct, admin = _subscribe(bench, arm="raise")
    cid = r.json()["detail"]["contract_id"]
    c, _auth = bench["client"], bench["auth"]
    listing = c.get("/tenant/contracts")
    assert listing.status_code == 200
    assert any(row["id"] == cid for row in listing.json()["contracts"]), (
        "we told the customer to check the archive — it must contain the row we named")
    one = c.get(f"/tenant/contract/{cid}")
    assert one.status_code == 200
    assert one.json()["status"] == "accepted"


@pytestmark_db
def test_DB_the_adapter_status_survives_for_the_operator(bench):
    """A Stripe outage and a misconfigured return url must stay distinguishable somewhere."""
    from saas import payments as _payments
    provider = _payments.get_payment_provider()
    out = tc.begin_subscription(
        tier="team", signer_email="x@example.test", legal_name="Christopher Probe",
        account_id=None, signer_user_id=None, signer_ip="203.0.113.1", signer_user_agent="s",
        expected_template_id=bench["template_id"], expected_version=bench["version"],
        expected_sha256=bench["sha"], success_url="https://a.test/ok",
        cancel_url="https://a.test/no")
    assert out["status"] == "checkout_unavailable"
    assert out["provider_status"] == "checkout_failed", (
        "the adapter's own word must be preserved, just not promoted to this module's enum")
    assert provider is not None


@pytestmark_db
def test_DB_the_success_case_is_unchanged_and_states_the_same_two_facts(bench):
    """Measured before the fix and pinned here: the success shape must not have moved."""
    r, rows, _a, _ad = _subscribe(bench, arm="ok")
    assert r.status_code == 200
    b = r.json()
    assert b["ok"] is True
    assert b["checkout_url"].startswith("https://checkout.stripe.com/")
    assert isinstance(b["contract_id"], int)
    assert b["tier"] == "team" and b["amount_cents"] == 75000 and b["currency"] == "CAD"
    assert rows == 1


@pytestmark_db
def test_DB_a_comped_account_is_refused_and_NOTHING_is_written(bench):
    """🔴 The owner held a comped account and signed a CA$750/mo agreement. Never again."""
    r, rows, _a, _ad = _subscribe(bench, comped=True, arm="ok")
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["status"] == "account_is_comped"
    assert d["signed"] is False
    assert "contract_id" not in d
    assert rows == 0, "a refusal for a comped account must leave no permanent row behind"
    assert "complimentary" in d["message"].lower()


@pytestmark_db
def test_DB_the_comp_refusal_says_why_rather_than_temporarily_unavailable(bench):
    """A customer we chose to give the product to deserves the real reason."""
    r, _rows, _a, _ad = _subscribe(bench, comped=True, arm="ok")
    msg = r.json()["detail"]["message"].lower()
    assert "unavailable" not in msg
    assert "free" in msg or "complimentary" in msg


@pytestmark_db
def test_DB_the_plans_page_is_told_about_the_comp_before_a_signature_is_offered(bench):
    """Learning this AFTER reading a four-figure agreement is a poor way to find out."""
    cur, _auth, c = bench["cur"], bench["auth"], bench["client"]
    acct, admin = str(uuid.uuid4()), str(uuid.uuid4())
    cur.execute("INSERT INTO public.accounts (account_id,name,slug,status,comped,comp_reason) "
                "VALUES (%s,%s,%s,'active',true,%s)",
                (acct, "Comp Co", "comp-" + acct[:8], "suite comp"))
    _auth.get_user_by_id = lambda uid: (
        {"id": admin, "account_id": acct, "username": "c@example.test", "password_hash": "x",
         "totp_secret": None, "mfa_enabled": False, "role": "admin", "status": "active",
         "display_name": None} if str(uid) == admin else None)
    _auth.can_view_billing = lambda uid: True
    c.cookies.clear()
    c.cookies.set(_auth.SESSION_COOKIE_NAME,
                  _auth.issue_session(dashboard_user_id=admin, account_id=acct,
                                      username="c@example.test"))
    r = c.get("/tenant/plans")
    assert r.status_code == 200
    assert r.json()["comp"]["active"] is True
    assert r.json()["comp"]["message"]


@pytestmark_db
def test_DB_a_non_comped_account_is_told_nothing_and_the_page_is_unchanged(bench):
    """The notice must be absent, not falsy-but-present, for the ordinary customer."""
    cur, _auth, c = bench["cur"], bench["auth"], bench["client"]
    acct, admin = str(uuid.uuid4()), str(uuid.uuid4())
    cur.execute("INSERT INTO public.accounts (account_id,name,slug,status) "
                "VALUES (%s,%s,%s,'active')", (acct, "Plain Co", "plain-" + acct[:8]))
    _auth.get_user_by_id = lambda uid: (
        {"id": admin, "account_id": acct, "username": "p@example.test", "password_hash": "x",
         "totp_secret": None, "mfa_enabled": False, "role": "admin", "status": "active",
         "display_name": None} if str(uid) == admin else None)
    _auth.can_view_billing = lambda uid: True
    c.cookies.clear()
    c.cookies.set(_auth.SESSION_COOKIE_NAME,
                  _auth.issue_session(dashboard_user_id=admin, account_id=acct,
                                      username="p@example.test"))
    assert c.get("/tenant/plans").json()["comp"] is None


@pytestmark_db
def test_DB_a_control_plane_fault_does_not_block_a_paying_customer(bench, monkeypatch):
    """The comp gate FAILS OPEN, deliberately, and the direction is argued in the code.

    Fail-closed would refuse revenue from every customer in the deployment on a transient DB
    fault; fail-open costs at most a comped account signing, which is today's behaviour and is
    recoverable by an operator."""
    from saas import entitlements as _ent

    def _boom(_aid):
        raise RuntimeError("control plane unreachable")

    monkeypatch.setattr(_ent, "comp_state", _boom)
    r, rows, _a, _ad = _subscribe(bench, arm="ok")
    assert r.status_code == 200, "a comp_state fault must not refuse a genuine paying customer"
    assert rows == 1


@pytestmark_db
def test_DB_an_expired_comp_does_not_block_a_purchase(bench):
    """``comp_state`` distinguishes ``comped`` from ``active``. Only ACTIVE blocks.

    30 days, not 1, DELIBERATELY. ``comp_state`` compares the stored date against the PROCESS's
    LOCAL date while PostgreSQL stores it against UTC, so a comp that lapsed yesterday reads as
    still active for the length of the UTC offset. That is a real pre-existing defect in
    ``entitlements.comp_state`` (see RESIDUALS.md) and it is NOT this suite's to pin — using an
    unambiguously lapsed date tests THIS gate's semantics instead of freezing that boundary."""
    cur = bench["cur"]
    acct, admin = str(uuid.uuid4()), str(uuid.uuid4())
    email = f"e+{acct[:8]}@example.test"
    cur.execute("INSERT INTO public.accounts (account_id,name,slug,status,comped,comp_reason,"
                "comp_until) VALUES (%s,%s,%s,'active',true,%s, current_date - 30)",
                (acct, "Expired Co", "exp-" + acct[:8], "lapsed comp"))
    _auth, c = bench["auth"], bench["client"]
    _auth.get_user_by_id = lambda uid: (
        {"id": admin, "account_id": acct, "username": email, "password_hash": "x",
         "totp_secret": None, "mfa_enabled": False, "role": "admin", "status": "active",
         "display_name": None} if str(uid) == admin else None)
    _auth.can_view_billing = lambda uid: True
    bench["mode"]["arm"] = "ok"
    c.cookies.clear()
    c.cookies.set(_auth.SESSION_COOKIE_NAME,
                  _auth.issue_session(dashboard_user_id=admin, account_id=acct, username=email))
    r = c.post("/tenant/subscribe",
               headers={"x-csrf-token": _auth.issue_csrf(dashboard_user_id=admin)},
               json={"tier": "team", "legal_name": "Christopher Probe", "accept": True,
                     "contract_template_id": bench["template_id"],
                     "terms_version": bench["version"], "terms_sha256": bench["sha"]})
    assert r.status_code == 200, "an EXPIRED comp is not an active one and must not block a sale"


@pytestmark_db
def test_DB_no_refusal_anywhere_in_the_flow_ever_claims_a_charge(bench):
    """Money-path invariant, driven across every reachable refusal rather than reasoned about."""
    for kwargs in ({"arm": "raise"}, {"comped": True, "arm": "ok"}):
        r, _rows, _a, _ad = _subscribe(bench, **kwargs)
        assert r.status_code >= 400
        assert r.json()["detail"]["charged"] is False


# ── LAST: the false-green guard ─────────────────────────────────────────────────────

def test_no_db_test_was_silently_skipped():
    """Turns "all passed, N skipped" into a RED. The DB tests are the ones with teeth."""
    assert _DB_RAN["value"], (
        "the DB-backed tests did not run — set PAYCONTRACT_TEST_DSN to a database with "
        "migrations >= 131 applied. A green run without them proves nothing about whether a "
        "permanent row was written.")
