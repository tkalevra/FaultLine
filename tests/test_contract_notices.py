"""The CONTRACTUAL NOTICE SENDER — saas/contract_notices.py + migration 260.

WHAT THIS PINS, and why each one is here rather than being obvious:

  * A signature gets its copy delivered, to the address recorded WITH the signature, carrying the
    document the customer actually signed. Consumer Protection Act, 2002 (Ontario) s.39(1) with
    O. Reg. 17/05 s.33(1) — a written copy within 15 days. Nothing did this before.
  * A REPLAY SENDS NOTHING. Sweeps re-run and prompt paths race the sweep; a duplicated "we are
    changing your agreement" reads as a second change. The arbiter is a Postgres row, so this is
    driven twice through the REAL function against the REAL table, not asserted about a flag.
  * A MAIL FAILURE NEVER LOSES OR ROLLS BACK A SIGNATURE, including when the sender RAISES. Proven
    by re-reading the contract row after the failure and asserting the delivery is still retryable
    — a failed notice must be at-most-once-per-attempt, never zero-of-one.
  * THE TONE. The owner's ruling was "ensure it's not abrasive or nasty". That is a property of
    rendered text, so the tests render the real bodies and assert on the actual words: no summons
    register, and the reassurance that there is nothing to do is present. A test that never renders
    the copy proves nothing about how it reads.
  * The material-change notice fires for a MATERIAL supersession, names what changed in the
    author's own words, and stays silent — loudly — when it cannot.

⚠️ FALSE-GREEN GUARD. Every DB-backed test here SKIPS without a control plane, and "PASS | 0
passed, N skipped" has lied in this repo before. ``_DB_RAN`` is set by the DB fixture and the LAST
test asserts it.

Run (never raw pytest — owner rule):
  PYTHONPATH="src:/home/chris/Documents/013-GIT/FaultLine-dev/.venv/lib64/python3.14/site-packages" \\
  POSTGRES_DSN=postgresql://faultline:faultline@172.20.0.3:5432/faultline_notice_test \\
  python3 tools/fltest.py --bug NOTICE --test tests/test_contract_notices.py
"""

import html as _html
import os
import re
import uuid
from pathlib import Path

import pytest

from saas import contract_notices as notices
from saas import contracts as saas_contracts
from saas import email_outbox as saas_outbox
from saas import platform_settings as saas_platform
from saas import smtp_sender as saas_smtp
from saas import supplier_disclosure as supplier

REPO = Path(__file__).resolve().parent.parent

# Set by the DB fixture. The final test asserts it — see the module docstring.
_DB_RAN = {"value": False}


def _dsn() -> str:
    return (os.environ.get("POSTGRES_DSN") or "").strip()


requires_db = pytest.mark.skipif(not _dsn(), reason="POSTGRES_DSN not set — control plane needed")


# ══════════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _capture_transport(monkeypatch):
    """Force the CAPTURE transport for every test in this file.

    Nothing here may reach a real relay, and capture is also the surface that makes the copy
    inspectable — the captured row holds the same subject/bodies a live send would carry. The
    payment-mode cache is cleared on both sides so an ordering change cannot decide the result."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_EMAIL_CAPTURE_ONLY", "1")
    monkeypatch.delenv("SAAS_EMAIL_KILL_SWITCH", raising=False)
    saas_smtp._payment_mode_cache["value"] = None
    saas_smtp._payment_mode_cache["at"] = 0.0
    saas_platform._cache = None
    saas_platform._cache_at = 0.0
    yield
    saas_smtp._payment_mode_cache["value"] = None
    saas_smtp._payment_mode_cache["at"] = 0.0
    saas_platform._cache = None
    saas_platform._cache_at = 0.0


def _sql(statement: str, params=(), fetch: bool = False):
    from saas import db
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(statement, params)
            rows = cur.fetchall() if fetch else None
        conn.commit()
    return rows


@pytest.fixture
def signed_contract():
    """A REAL accepted contract against the REAL active template, on a real control plane.

    Zero signed contracts exist in production, so the signature path cannot be exercised by
    waiting for one — it is driven end to end through ``record_acceptance``, the same writer the
    subscribe route uses. Every signer email is uuid-suffixed so runs never collide, and nothing is
    cleaned up: ``public.signed_contracts`` is append-only by design and a DELETE would be refused
    by migration 243's trigger."""
    _DB_RAN["value"] = True
    terms = saas_contracts.get_current_terms()
    assert terms, "the control plane has no active contract template — apply migrations >= 243"
    email = f"notice-{uuid.uuid4().hex[:12]}@example.com"
    cid = saas_contracts.record_acceptance(
        template_id=int(terms["id"]),
        signer_email=email,
        signer_name="Dana Whitfield",
        signer_ip="203.0.113.7",
        signer_user_agent="pytest",
        tier="team",
        agreed_amount_cents=75000,
        agreed_currency="CAD",
        agreed_interval="month",
    )
    assert cid, "record_acceptance did not write a contract row"
    yield {"id": int(cid), "email": email, "version": terms["version"]}


def _captures(reason: str, to: str) -> list:
    return [r for r in saas_outbox.list_recent(limit=1000, reason=reason)
            if r.get("to_address") == to]


def _notice_rows(contract_id: int) -> list:
    return notices.list_notices(limit=100, contract_id=contract_id)


# ══════════════════════════════════════════════════════════════════════════════════
# (a) THE STATUTORY DELIVERY — CPA 2002 s.39 / O. Reg. 17/05 s.33
# ══════════════════════════════════════════════════════════════════════════════════

@requires_db
def test_a_signature_gets_its_copy_delivered_to_the_address_that_signed(signed_contract):
    """One signature → one email, to signer_email, carrying the signed document as an attachment."""
    res = notices.deliver_agreement_copy(signed_contract["id"])
    assert res["ok"] is True and res["status"] == "sent", res

    rows = _captures("contract_copy", signed_contract["email"])
    assert len(rows) == 1, f"expected exactly one captured copy, got {len(rows)}"
    row = rows[0]
    assert str(signed_contract["id"]) in row["subject"]

    # The ATTACHMENT is the delivered copy. The capture table records proof of carriage
    # (filename + length + digest) rather than a second copy of a legal document.
    atts = (row.get("metadata") or {}).get("attachments") or []
    assert len(atts) == 1, f"the statutory copy must be attached, got {atts}"
    assert atts[0]["filename"] == f"FaultLine-Subscription-Agreement-{signed_contract['id']}.txt"
    assert atts[0]["content_type"] == "text/plain"
    assert atts[0]["bytes"] > 500, "an agreement copy of a few hundred bytes is not an agreement"


@requires_db
def test_the_delivered_copy_carries_everything_the_regulation_requires(signed_contract):
    """O. Reg. 17/05 s.33(2): the copy must include the s.32 information, the consumer's NAME and
    the DATE the agreement was entered into — plus, from s.32 ¶2, the supplier's telephone number
    and business address, which the agreement body does not carry."""
    contract = saas_contracts.get_signed_contract(signed_contract["id"])
    copy = notices.render_agreement_copy(contract)
    assert copy

    assert "Dana Whitfield" in copy, "s.33(2)2 — the name of the consumer"
    assert re.search(r"Date entered into: \d{1,2} \w+ \d{4}", copy), "s.33(2)3 — the date"
    assert supplier.TELEPHONE in copy, "s.32 ¶2 — the telephone number of the supplier"
    for line in supplier.postal_address_lines():
        assert line in copy, f"s.32 ¶2 — the business address line {line!r}"
    assert signed_contract["email"] in copy
    assert str(contract["contract_version"]) in copy

    # ⭐ AND THE SIGNED TEXT IS VERBATIM BELOW THE COVER BLOCK. ESIGN 15 U.S.C. § 7001(d)(1)(A)
    # wants a record that accurately reflects the contract; contract_snapshot already IS that
    # record, so anything the renderer did to those bytes would make the delivered copy a
    # RENDERING of the agreement rather than the agreement.
    assert copy.endswith(contract["contract_snapshot"]), \
        "the snapshot must be delivered byte-for-byte, as the final part of the copy"


@requires_db
def test_a_replay_sends_nothing(signed_contract):
    """THE IDEMPOTENCY PROOF, driven through the real function against the real table.

    Three deliveries: the first sends, the second and third must not. A second copy of a signed
    agreement is a real harm, not a cosmetic one."""
    first = notices.deliver_agreement_copy(signed_contract["id"])
    assert first["status"] == "sent"

    for _ in range(2):
        again = notices.deliver_agreement_copy(signed_contract["id"])
        assert again["status"] == "already_sent", again
        assert again.get("skipped") is True

    assert len(_captures("contract_copy", signed_contract["email"])) == 1, \
        "a replay produced a second email — the exactly-once claim is not holding"

    sent = [r for r in _notice_rows(signed_contract["id"])
            if r["notice_kind"] == "agreement_copy"]
    assert len(sent) == 1 and sent[0]["outcome"] == "sent"


@requires_db
def test_the_sweep_finds_an_undelivered_signature_and_then_stops(signed_contract):
    """The DURABLE path. A signature with no delivered copy is picked up by the sweep; the very
    next sweep sends nothing. This is what makes a lost prompt-path thread cost promptness only."""
    due = notices._due_agreement_copies(500)
    assert signed_contract["id"] in due, "a fresh signature must be due for its statutory copy"

    first = notices.sweep()
    assert first["copies_sent"] >= 1
    assert signed_contract["id"] not in notices._due_agreement_copies(500)

    second = notices.sweep()
    assert second["copies_sent"] == 0, "the sweep re-sent a copy it had already delivered"
    assert len(_captures("contract_copy", signed_contract["email"])) == 1


@requires_db
def test_the_prompt_path_delivers_without_the_caller_waiting(signed_contract):
    """The detached delivery actually delivers, and returns whether a THREAD started — never
    whether mail was sent. A caller that treated the return value as delivery would be reporting
    success for an email that had not been attempted yet."""
    import threading

    before = {t.name for t in threading.enumerate()}
    assert notices.deliver_on_signature_async(signed_contract["id"]) is True
    for t in threading.enumerate():
        if t.name not in before and t.name.startswith("fl-contract-copy-"):
            t.join(timeout=20)
    assert len(_captures("contract_copy", signed_contract["email"])) == 1


def test_the_signature_path_uses_the_non_blocking_hook():
    """``begin_subscription`` must call the DETACHED entry point. Calling
    ``deliver_agreement_copy`` there instead would put an SMTP round trip between a customer's
    signature and their payment page — the exact thing the async wrapper exists to prevent, and a
    regression that no functional test would notice because the mail still goes out."""
    src = (REPO / "saas" / "tenant_checkout.py").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert "deliver_on_signature_async(" in code, \
        "the signature path no longer starts a statutory-copy delivery"
    assert "deliver_agreement_copy(" not in code, \
        "the signature path calls the BLOCKING sender — an SMTP round trip is now on the "\
        "customer's checkout request"


# ══════════════════════════════════════════════════════════════════════════════════
# THE FAIL-SAFE — a mail failure must never lose or roll back a signature
# ══════════════════════════════════════════════════════════════════════════════════

@requires_db
@pytest.mark.parametrize("mode", ["returns_failure", "raises"])
def test_a_mail_failure_leaves_the_signature_intact_and_the_delivery_retryable(
        signed_contract, monkeypatch, mode):
    """THE HARD CONSTRAINT. Two failure shapes, because "never raises" is a promise made in a
    different file that a future edit can break: a sender that RETURNS a failure and a sender that
    RAISES must both leave (1) the contract untouched, (2) an honest non-ok answer, and (3) a
    delivery that a later sweep still performs. Zero-of-one is the outcome that must be impossible.
    """
    before = saas_contracts.get_signed_contract(signed_contract["id"])

    def _boom(*a, **kw):
        if mode == "raises":
            raise RuntimeError("relay exploded")
        return {"ok": False, "error": "SMTP send failed (TimeoutError)"}

    monkeypatch.setattr(saas_smtp, "send_contract_copy_email", _boom)
    res = notices.deliver_agreement_copy(signed_contract["id"])
    assert res["ok"] is False and res["status"] == "send_failed", res

    # 1. THE SIGNATURE IS UNTOUCHED — every acceptance fact identical, nothing rolled back.
    after = saas_contracts.get_signed_contract(signed_contract["id"])
    assert after == before, "a mail failure changed the signed contract"

    # 2. THE ATTEMPT IS RECORDED (a failed row is evidence, and is NOT deleted).
    rows = [r for r in _notice_rows(signed_contract["id"]) if r["notice_kind"] == "agreement_copy"]
    assert len(rows) == 1 and rows[0]["outcome"] == "failed"
    assert rows[0]["error"], "a failed attempt must record why"

    # 3. AND IT IS STILL RETRYABLE — the failure did not consume the statutory delivery.
    assert signed_contract["id"] in notices._due_agreement_copies(500)
    monkeypatch.undo()
    saas_smtp._payment_mode_cache["value"] = None
    retry = notices.deliver_agreement_copy(signed_contract["id"])
    assert retry["status"] == "sent", "the retry did not deliver — this is zero-of-one"
    assert len(_captures("contract_copy", signed_contract["email"])) == 1

    rows = [r for r in _notice_rows(signed_contract["id"]) if r["notice_kind"] == "agreement_copy"]
    assert len(rows) == 1 and rows[0]["outcome"] == "sent" and rows[0]["attempts"] == 2


@requires_db
def test_the_kill_switch_suppresses_without_consuming_the_delivery(signed_contract, monkeypatch):
    """A killed send is NOT a send. It must not claim the exactly-once slot, or the master
    shut-off would permanently destroy a customer's statutory copy."""
    monkeypatch.setenv("SAAS_EMAIL_KILL_SWITCH", "1")
    saas_platform._cache = None
    saas_platform._cache_at = 0.0

    res = notices.deliver_agreement_copy(signed_contract["id"])
    assert res["ok"] is False and res["status"] == "killed"
    assert _notice_rows(signed_contract["id"]) == [], \
        "the kill switch consumed a delivery slot for an email that never went out"

    monkeypatch.undo()
    saas_platform._cache = None
    saas_platform._cache_at = 0.0
    saas_smtp._payment_mode_cache["value"] = None
    assert notices.deliver_agreement_copy(signed_contract["id"])["status"] == "sent"


@requires_db
def test_an_empty_snapshot_is_refused_rather_than_delivered(signed_contract, monkeypatch):
    """We do not mail a blank agreement. Same family as the retention lane's alert-not-send: when
    the thing being promised is unreadable, refuse and be loud rather than send something empty."""
    real = saas_contracts.get_signed_contract

    def _hollow(cid):
        row = dict(real(cid))
        row["contract_snapshot"] = ""
        return row

    monkeypatch.setattr(saas_contracts, "get_signed_contract", _hollow)
    res = notices.deliver_agreement_copy(signed_contract["id"])
    assert res["status"] == "no_snapshot" and res["ok"] is False
    assert _captures("contract_copy", signed_contract["email"]) == []
    assert _notice_rows(signed_contract["id"]) == [], "a refusal must not consume the slot"


# ══════════════════════════════════════════════════════════════════════════════════
# (b) SECTION 8 — the material-version notice
# ══════════════════════════════════════════════════════════════════════════════════

@requires_db
def test_a_material_new_version_notifies_existing_signatories_exactly_once(signed_contract):
    """A live signature on an older version is owed section 8's notice — once per version."""
    pending = notices.pending_material_version()
    assert pending, "the active template is not a material supersession; this control plane cannot "\
                    "exercise the section 8 lane"

    # Sign against the CURRENT version, then publish a NEW material one on top of it, so this
    # signature is genuinely being superseded.
    new_version = f"test-{uuid.uuid4().hex[:8]}-v"
    published = saas_contracts.publish_terms_version(
        version=new_version,
        title="FaultLine Subscription Agreement",
        body_markdown="## FaultLine Subscription Agreement\n\nA new version for the notice test.",
        created_by="pytest",
        change_summary="We changed how notice periods are described in section 8.",
        change_is_material=True,
    )
    assert published.get("ok"), published

    pending = notices.pending_material_version()
    assert pending and pending["version"] == new_version

    first = notices.notify_version_change(signed_contract["id"], pending)
    assert first["status"] == "sent", first
    again = notices.notify_version_change(signed_contract["id"], pending)
    assert again["status"] == "already_sent", "a replay sent a second change notice"

    rows = _captures("contract_change", signed_contract["email"])
    assert len(rows) == 1
    body = rows[0]["text_body"]
    assert "We changed how notice periods are described in section 8." in body, \
        "the notice must say WHAT changed — revised terms without that are not sufficient notice"
    assert new_version in body and signed_contract["version"] in body

    logged = [r for r in _notice_rows(signed_contract["id"]) if r["notice_kind"] == "version_notice"]
    assert len(logged) == 1 and logged[0]["notice_ref"] == new_version


@requires_db
def test_a_signatory_already_on_the_new_version_is_not_notified(signed_contract):
    """Nobody is told their own version supersedes them. ``IS DISTINCT FROM`` keeps a NULL-version
    row in the set (it IS on an older version) while excluding an exact match."""
    due = notices._due_version_notices(signed_contract["version"], 500)
    assert signed_contract["id"] not in due


def test_a_material_version_with_no_summary_sends_nothing(monkeypatch):
    """THE HONEST NO-OP. "Providing revised terms without indicating what changed was not
    sufficient notice" — so a notice that cannot say what changed discharges nothing while spending
    the customer's attention. It is refused, not softened."""
    monkeypatch.setattr(saas_contracts, "get_current_terms", lambda: {
        "version": "x-v9", "supersedes_version": "x-v8",
        "change_is_material": True, "change_summary": "   ",
    })
    assert notices.pending_material_version() is None


def test_a_non_material_version_notifies_nobody(monkeypatch):
    """Section 8 promises notice for a MATERIAL change. Emailing every customer about a typo fix is
    how a real notice gets ignored."""
    monkeypatch.setattr(saas_contracts, "get_current_terms", lambda: {
        "version": "x-v9", "supersedes_version": "x-v8",
        "change_is_material": False, "change_summary": "Fixed a typo in section 10.",
    })
    assert notices.pending_material_version() is None


def test_the_first_version_ever_published_notifies_nobody(monkeypatch):
    """A first version is not a change and there is nobody to notify."""
    monkeypatch.setattr(saas_contracts, "get_current_terms", lambda: {
        "version": "x-v1", "supersedes_version": None,
        "change_is_material": None, "change_summary": "First version.",
    })
    assert notices.pending_material_version() is None


# ══════════════════════════════════════════════════════════════════════════════════
# (d) THE OPERATOR RESEND
# ══════════════════════════════════════════════════════════════════════════════════

@requires_db
def test_the_operator_resend_always_sends_and_is_always_recorded(signed_contract):
    """A resend is a deliberate act. It must NOT be suppressed by the statutory delivery that came
    before it — a support button that silently does nothing is worse than no button — and every one
    must leave a row naming who asked."""
    assert notices.deliver_agreement_copy(signed_contract["id"])["status"] == "sent"

    for _ in range(2):
        res = notices.deliver_agreement_copy(signed_contract["id"], resend=True,
                                             requested_by="operator:ada")
        assert res["status"] == "sent", res

    assert len(_captures("contract_copy_resend", signed_contract["email"])) == 2
    resends = [r for r in _notice_rows(signed_contract["id"])
               if r["notice_kind"] == "agreement_copy_resend"]
    assert len(resends) == 2
    assert all(r["requested_by"] == "operator:ada" for r in resends)
    assert len({r["notice_ref"] for r in resends}) == 2, "each resend needs its own ref"

    # ...and it did NOT disturb the statutory delivery record.
    statutory = [r for r in _notice_rows(signed_contract["id"])
                 if r["notice_kind"] == "agreement_copy"]
    assert len(statutory) == 1 and statutory[0]["attempts"] == 1


def test_the_resend_route_cannot_be_pointed_at_another_address():
    """THE SECURITY PROPERTY OF THE RESEND. The recipient is read from the immutable
    ``signer_email``, never taken from the request — otherwise a support convenience becomes a way
    to mail a customer's executed agreement (name, IP, price) to any address an operator typed."""
    from saas import email_controls_routes as routes
    fields = set(routes.ContractCopyResend.model_fields)
    assert fields == {"contract_id"}, f"the resend body must carry ONLY a contract id, got {fields}"


# ══════════════════════════════════════════════════════════════════════════════════
# TONE — the owner's ruling, asserted on the REAL rendered text
# ══════════════════════════════════════════════════════════════════════════════════

# Register that must never appear. Each is a way of sounding like a summons rather than a receipt.
_FORBIDDEN = [
    "hereby", "pursuant to", "failure to", "you must immediately", "immediate action",
    "act now", "final notice", "urgent", "terminate your access", "legal action",
    "no later than", "shall be deemed", "void", "penalt",
]


def _rendered_copy() -> str:
    text, _ = saas_smtp._contract_copy_bodies(
        signer_name="Dana Whitfield", contract_number="#1042", accepted_on="13 August 2026",
        facts=["Plan: Team", "Agreed price: $750.00 CAD per month"],
        supplier_sentence=supplier.disclosure_sentence(), resend=False)
    return text


def _rendered_change() -> str:
    text, _ = saas_smtp._contract_change_bodies(
        signer_name="Dana Whitfield", contract_number="#1042",
        signed_version="2026-08-13-v3", new_version="2026-08-13-v4",
        change_summary="We removed a promise about backups that we could not keep.",
        applies_from="12 September 2026", supplier_sentence=supplier.disclosure_sentence(),
        notice_days=30)
    return text


@pytest.mark.parametrize("render", [_rendered_copy, _rendered_change])
def test_neither_notice_reads_like_a_summons(render):
    body = render().lower()
    hits = [w for w in _FORBIDDEN if w in body]
    assert not hits, f"abrasive register in a contractual notice: {hits}"
    # No shouting. The brand wordmark in the footer is the one legitimate all-caps token, so the
    # check is on WORDS in the copy rather than on any capital letters at all.
    shouty = [w for w in re.findall(r"\b[A-Z]{4,}\b", render()) if w not in {"FAULTLINE", "SHA"}]
    assert not shouty, f"ALL-CAPS in a contractual notice: {shouty}"


def test_the_copy_leads_with_there_being_nothing_to_do():
    """For the signature copy the customer genuinely does not need to act, and the message says so
    before it says anything else — that is the whole difference between a receipt and a demand."""
    body = _rendered_copy()
    assert "You don't need to do anything with this" in body
    assert "yours to keep" in body


def test_the_change_notice_offers_the_do_nothing_option_first():
    """A customer reading it should feel informed, not cornered: what changed, what still governs
    them today, then the do-nothing option BEFORE the way out."""
    body = _rendered_change()
    assert "still governs your subscription until it renews" in body
    nothing_to_do = body.index("there's nothing to do")
    way_out = body.index("stop your subscription renewing")
    assert nothing_to_do < way_out, "the way out must not be presented as the default"
    assert "30 days' notice" in body


@pytest.mark.parametrize("render", [_rendered_copy, _rendered_change])
def test_both_notices_carry_a_real_human_contact_and_the_postal_address(render):
    """O. Reg. 17/05 s.32 ¶2, and also simple decency: a legal notice with no way to reach a person
    is the thing that makes people feel cornered."""
    body = render()
    assert supplier.LEGAL_EMAIL in body
    assert supplier.TELEPHONE in body
    assert supplier.postal_address_inline() in body


# ══════════════════════════════════════════════════════════════════════════════════
# THE SUPPLIER DISCLOSURE — one source, guarded against the page that publishes it
# ══════════════════════════════════════════════════════════════════════════════════

def test_the_disclosure_matches_the_page_that_publishes_it():
    """DUPLICATION YOU CANNOT REMOVE MUST BE DUPLICATION A TEST GUARDS.

    An HTML page cannot be imported by Python and a Python module cannot be read by a static site,
    so the statutory identity necessarily exists twice. This is the seam that stops one of them
    going stale — and a stale telephone number on a legal notice is the exact defect the disclosure
    was added to fix."""
    page = (REPO / "saas" / "landing" / "legal.html").read_text(encoding="utf-8")
    # &nbsp; and line wrapping are presentation; compare on collapsed, unescaped text.
    flat = re.sub(r"\s+", " ", _html.unescape(page))

    assert supplier.LEGAL_NAME in flat
    assert supplier.TELEPHONE in flat
    assert supplier.LEGAL_EMAIL in flat
    assert supplier.BILLING_EMAIL in flat
    for line in supplier.postal_address_lines():
        assert re.sub(r"\s+", " ", line) in flat, \
            f"address line {line!r} is not on the published legal page"


def test_the_disclosure_never_resolves_to_nothing(monkeypatch):
    """A blank statutory disclosure that fails silently is worse than a stale one that is visibly
    wrong, so an empty override falls back to the published default rather than emptying."""
    monkeypatch.setenv("SAAS_SUPPLIER_ADDR", "   |  | ")
    assert supplier.postal_address_lines() == supplier._DEFAULT_ADDRESS
    monkeypatch.setenv("SAAS_SUPPLIER_ADDR", "1 Elm St|Toronto, Ontario M1M 1M1|Canada")
    assert supplier.postal_address_lines() == ("1 Elm St", "Toronto, Ontario M1M 1M1", "Canada")


# ══════════════════════════════════════════════════════════════════════════════════
# WRITE-PATH AUDIT — structural, not a promise in a comment
# ══════════════════════════════════════════════════════════════════════════════════

def test_the_notice_sender_never_writes_to_the_financial_record():
    """THE STRUCTURAL HALF OF "a mail failure never loses a signature".

    The behavioural tests above prove one failure did no damage. This proves no failure CAN: the
    module contains no write statement naming ``signed_contracts`` or ``contract_templates`` at
    all. A future edit that adds one turns this red, which is the point — the guarantee should not
    depend on remembering."""
    src = (REPO / "saas" / "contract_notices.py").read_text(encoding="utf-8")
    # Strip ``#`` comments so the module's own prose about NOT writing cannot be mistaken for a
    # write — a file that DOCUMENTS a rule must not look identical to one that breaks it.
    #
    # ⚠️ DOCSTRINGS ARE DELIBERATELY *NOT* STRIPPED, and the first cut of this test was wrong for
    # exactly that reason: every SQL statement in this module is itself a triple-quoted string, so
    # a docstring stripper deletes the code under audit and the test passes by looking at nothing.
    # Prose cannot produce a false positive here anyway — the patterns require a literal
    # ``INSERT INTO``/``UPDATE``/``DELETE FROM`` immediately before the table name.
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    for table in ("signed_contracts", "contract_templates"):
        for verb in ("INSERT INTO", "UPDATE", "DELETE FROM"):
            pattern = rf"{verb}\s+(public\.)?{table}\b"
            assert not re.search(pattern, code, re.IGNORECASE), \
                f"saas/contract_notices.py contains a {verb} against {table}"
    # The ONE table it may write.
    assert re.search(r"INSERT INTO\s+public\.contract_notice_log", code)


def test_the_delivery_record_never_stores_the_agreement_text():
    """The signed text lives exactly once, in ``signed_contracts.contract_snapshot``. A second
    stored copy of a legal document is a second document that can drift, so the record points at
    the contract by id + version + fingerprint and holds no body column."""
    ddl = (REPO / "migrations" / "260_contract_notice_log.sql").read_text(encoding="utf-8")
    create = ddl[ddl.index("CREATE TABLE"):ddl.index(");")]
    # ⚠️ STRIP THE ``--`` COMMENTS FIRST. The column comments inside this CREATE TABLE explain that
    # the table must never hold a body, and a bare substring check matched that explanation and
    # failed on the very sentence promising the rule it was checking. Recurring class in this repo:
    # never assert a substring against text that also DISCUSSES the substring.
    columns = "\n".join(re.sub(r"--.*$", "", ln) for ln in create.splitlines()).lower()
    for banned in ("snapshot", "body", "markdown"):
        assert banned not in columns, \
            f"public.contract_notice_log declares a {banned!r} column — it must not hold the text"


def test_zzz_no_test_was_silently_skipped():
    """FALSE-GREEN GUARD (must run LAST). Without it, a run with no POSTGRES_DSN reports a
    comfortable "PASS | N skipped" while every DB-backed assertion above did nothing.

    ⚠️ IT MUST NOT CARRY ``@requires_db`` — AND THE FIRST CUT OF IT DID, WHICH IS THE JOKE. A guard
    that skips under exactly the condition it exists to detect is not a guard: measured, a run with
    no DSN reported "15 passed, 13 skipped" in green while every statutory-delivery assertion in
    this file had done nothing at all. The guard has to be the ONE test that always executes."""
    assert _DB_RAN["value"], (
        "No DB-backed test ran. Pass POSTGRES_DSN pointing at a THROWAWAY control plane with "
        "migrations >= 131 applied; a fully-skipped run proves nothing.")
