"""Migration 252 — BIND THE TERMS VERSION THE CUSTOMER *SAW* TO THE SIGNATURE WE RECORD.

WHAT THIS SUITE EXISTS TO FALSIFY
─────────────────────────────────
``signed_contracts`` already recorded ``contract_version`` + an immutable ``contract_snapshot``.
That proves WHAT WE WROTE. It does not prove that what we wrote is the document the customer's
browser RENDERED — ``GET /tenant/plans`` reads the terms, the POST reads them AGAIN, and a human
reading time sits between the two reads. An adversarial pass DEMONSTRATED the gap live: the page
displayed "governed by the laws of the Province of Ontario", an operator published a new version
mid-session, and the permanent immutable row recorded "governed by the laws of Delaware, USA. You
waive all class-action rights."

Two REAL exploits are reproduced here, and each must now fail:

  H1  the version-supersession exploit  — a NEW template row is published mid-session.
      Refused as ``terms_changed``.
  H2  the SILENT-REWRITE exploit        — the SAME template id and the SAME version string, but
      the served text changes underneath. Refused as ``terms_version_stale``, and ONLY the
      document fingerprint can see it.

H2 is not hypothetical and it is the reason a version string alone is not enough:
``saas/contracts.py::_substitute_for_display`` interpolates LIVE platform settings
({{RETENTION_DAYS}}, {{FULFILMENT_DAYS}}, {{CONTRACT_TERM_MONTHS}}, {{BACKUP_OVERWRITE_DAYS}}) into
the seeded template AT READ TIME. An operator changing ``data_retention_days`` between the page
load and the click changes the WORDS being signed while ``contract_templates.id`` and ``.version``
both stay byte-identical. A VERSION STRING IS A LABEL; A HASH IS THE DOCUMENT.

LEGAL GROUNDING (cited in the migration and in saas/contracts.py::body_fingerprint):
  * Ironclad, "6 Components of Clickwrap Enforceability" —
    https://ironcladapp.com/journal/contract-management/6-components-of-clickwrap-enforceability
    "Unless you can prove who accepted your agreements and which version(s) they accepted, your
     clickwrap is not likely to be enforceable, as courts have found that customers have no
     obligation to check the terms periodically for changes."
  * ESIGN 15 U.S.C. § 7001(d)(1)(A) — a retained electronic record must "accurately reflect the
    information set forth in the contract or other record".
    https://www.law.cornell.edu/uscode/text/15/7001

⚠️ FALSE-GREEN GUARD. Every claim above is a DATABASE property (a trigger, a transaction, a row
count). ``_DB_RAN`` is set by the DB fixture and asserted by the LAST test in this file, so a run
that reports "0 passed, N skipped" is RED rather than reassuring. Run with a THROWAWAY DSN and
assert skipped == 0.
"""

import hashlib
import os
import uuid
from pathlib import Path

import pytest

from saas import contracts as _contracts
from saas import tenant_checkout as tc


REPO = Path(__file__).resolve().parent.parent
MIG_243 = REPO / "migrations" / "243_contract_infrastructure.sql"
MIG_252 = REPO / "migrations" / "252_contract_document_fingerprint.sql"

_DSN = os.environ.get("POSTGRES_DSN")
_SCHEMA = "tb252_" + uuid.uuid4().hex[:8]

try:
    import psycopg2 as _pg
except Exception:  # pragma: no cover
    _pg = None

needs_db = pytest.mark.skipif(
    not _DSN or _pg is None or not MIG_243.exists() or not MIG_252.exists(),
    reason="POSTGRES_DSN + psycopg2 + migrations 243 and 252 required")

_DB_RAN = {"value": False}


# ══════════════════════════════════════════════════════════════════════════════════
# 1. THE FINGERPRINT FUNCTION — pure, no DB
# ══════════════════════════════════════════════════════════════════════════════════

def test_the_fingerprint_is_lowercase_hex_sha256_of_utf8():
    body = "## Terms\n\nGoverned by the laws of the Province of Ontario. — em dash, é, 日本語\n"
    got = _contracts.body_fingerprint(body)
    assert got == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert len(got) == 64 and got == got.lower()
    assert all(c in "0123456789abcdef" for c in got)


def test_a_one_character_change_changes_the_fingerprint():
    """The whole feature rests on this. Stated as a test so it is checked, not assumed."""
    a = _contracts.body_fingerprint("governed by the laws of the Province of Ontario")
    b = _contracts.body_fingerprint("governed by the laws of Delaware, USA")
    c = _contracts.body_fingerprint("governed by the laws of the Province of Ontario.")
    assert a != b and a != c


def test_the_fingerprint_is_total_and_never_raises():
    """Callers sit inside broad ``except`` blocks on a money path — a raising hash would be
    swallowed and read as "no fingerprint", which is silent degradation."""
    empty = hashlib.sha256(b"").hexdigest()
    assert _contracts.body_fingerprint(None) == empty
    assert _contracts.body_fingerprint("") == empty


def test_a_missing_fingerprint_is_never_a_match():
    """FAIL-CLOSED. If absence counted as agreement, a client could opt out of the binding by
    simply omitting the field — which is the defect with extra steps."""
    real = _contracts.body_fingerprint("x")
    assert _contracts.fingerprints_match(real, real) is True
    assert _contracts.fingerprints_match(real.upper(), real) is True   # hex case only
    assert _contracts.fingerprints_match(None, real) is False
    assert _contracts.fingerprints_match("", real) is False
    assert _contracts.fingerprints_match(real, None) is False
    assert _contracts.fingerprints_match(None, None) is False
    assert _contracts.fingerprints_match("  " + real + " ", real) is True


# ══════════════════════════════════════════════════════════════════════════════════
# 2. THE THROWAWAY SCHEMA — real SQL, real triggers, real PostgreSQL
# ══════════════════════════════════════════════════════════════════════════════════
#
# Both migrations are applied AS SHIPPED (retargeted from ``public.`` at the throwaway schema, the
# same substitution tests/test_tenant_checkout_module.py validated 243 with), so what runs here is
# the file that will run in production, not a copy of it.

@pytest.fixture(scope="module")
def throwaway_schema():
    if not _DSN or _pg is None:
        pytest.skip("no DSN")
    conn = _pg.connect(_DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {_SCHEMA}")
        for mig in (MIG_243, MIG_252):
            cur.execute(mig.read_text().replace("public.", f"{_SCHEMA}."))
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    conn.close()


class _CurProxy:
    """Retargets the module's literal ``public.`` prefix at the throwaway schema.

    A WRAPPER, not attribute assignment: psycopg2's ``cursor.execute`` is a read-only C attribute,
    and assigning to it fails INSIDE contracts.py's own ``except`` so every function quietly
    returns None — which reads exactly like a product bug and is not one."""

    def __init__(self, cur):
        self._cur = cur

    def execute(self, q, params=None):
        return self._cur.execute(q.replace("public.", f"{_SCHEMA}."), params)

    def __getattr__(self, name):
        return getattr(self._cur, name)

    def __enter__(self):
        self._cur.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cur.__exit__(*exc)


class _ConnProxy:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self, *a, **kw):
        return _CurProxy(self._conn.cursor(*a, **kw))

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.fixture
def db_contracts(throwaway_schema, monkeypatch):
    import contextlib

    @contextlib.contextmanager
    def _conn():
        c = _pg.connect(_DSN)
        try:
            yield _ConnProxy(c)
        finally:
            c.close()

    monkeypatch.setattr(_contracts.db, "connection", _conn)
    _DB_RAN["value"] = True
    return _contracts


def _q(conn, sql, args=None):
    with conn.cursor() as cur:
        cur.execute(sql.replace("public.", f"{_SCHEMA}."), args)
        try:
            return cur.fetchall()
        except Exception:
            return []


def _contract_count(conn) -> int:
    return int(_q(conn, "SELECT count(*) FROM public.signed_contracts")[0][0])


def _publish(db_c, body: str, *, version=None) -> dict:
    """Publish a NEW active version through the REAL operator seam. Version strings are
    uuid-suffixed because a published version is immutable and can never be re-used, and the
    partial unique index permits exactly one non-retired row."""
    ver = version or ("probe-" + uuid.uuid4().hex[:10])
    out = db_c.publish_terms_version(
        version=ver, title="FaultLine Subscription Agreement", body_markdown=body,
        created_by="test",
        # REQUIRED when superseding (migration 256) — a revision with no statement of what
        # changed is refused, so every publisher in a test must behave like a real one.
        change_summary="test fixture: superseding the incumbent")
    assert out.get("ok"), out
    return out


# ══════════════════════════════════════════════════════════════════════════════════
# 3. POSTGRES AND PYTHON MUST AGREE — measured, not assumed
# ══════════════════════════════════════════════════════════════════════════════════

@needs_db
def test_postgres_and_python_agree_on_the_fingerprint(throwaway_schema, db_contracts):
    """🔴 THE LOAD-BEARING EQUALITY. Migration 252 backfills with an SQL expression and every
    runtime write uses a Python one. If the two disagree by so much as an encoding, the binding
    refuses every legitimate signature (or, worse, accepts a stale one).

    Driven against the REAL seeded template — thousands of bytes of real markdown with em dashes
    and typographic punctuation — not a synthetic ASCII string that could not expose an encoding
    difference."""
    rows = _q(throwaway_schema,
              "SELECT id, body_markdown, body_sha256, "
              "       encode(sha256(convert_to(body_markdown,'UTF8')),'hex') "
              "FROM public.contract_templates ORDER BY id")
    assert rows, "the seeded template is missing — migration 243 did not apply"
    for tid, body, stored, sql_hash in rows:
        py_hash = _contracts.body_fingerprint(body)
        assert py_hash == sql_hash, f"template {tid}: python {py_hash} != sql {sql_hash}"
        assert stored == sql_hash, f"template {tid}: backfill wrote {stored}, expected {sql_hash}"
    assert "—" in rows[0][1], "the corpus under test has no non-ASCII bytes — it proves nothing"


@needs_db
def test_the_backfill_covered_every_pre_existing_row(throwaway_schema, db_contracts):
    n = _q(throwaway_schema,
           "SELECT count(*) FROM public.contract_templates WHERE body_sha256 IS NULL")[0][0]
    assert int(n) == 0


@needs_db
def test_a_template_inserted_WITHOUT_a_fingerprint_gets_one_anyway(throwaway_schema, db_contracts):
    """⚠️ CONSTRAINT 13 — EVERY WRITER, NOT THE ONE I CHANGED. ``public.contract_templates`` is
    also written by migration 253's '2026-08-12-v2' seed, whose INSERT names an explicit column
    list that does NOT include body_sha256 and which runs AFTER 252, so the backfill can never
    reach it. Rather than guard four writers out of five, 252 derives the fingerprint at the TABLE
    (BEFORE INSERT trigger). This test is that guarantee, driven the way a migration seed drives
    it: raw SQL, no application code."""
    body = "## Seeded by a migration\n\nNo fingerprint supplied.\n"
    ver = "rawseed-" + uuid.uuid4().hex[:8]
    _q(throwaway_schema,
       "INSERT INTO public.contract_templates (version, title, body_markdown, retired_at) "
       "VALUES (%s, %s, %s, NOW())", (ver, "t", body))
    got = _q(throwaway_schema,
             "SELECT body_sha256 FROM public.contract_templates WHERE version = %s", (ver,))
    assert got[0][0] == _contracts.body_fingerprint(body)


@needs_db
def test_a_template_inserted_with_a_LYING_fingerprint_is_refused(throwaway_schema, db_contracts):
    """A fingerprint that can be set to an arbitrary string is decoration. The column can only
    ever hold the hash of the row it sits on."""
    ver = "liar-" + uuid.uuid4().hex[:8]
    with pytest.raises(Exception) as exc:
        _q(throwaway_schema,
           "INSERT INTO public.contract_templates "
           "(version, title, body_markdown, body_sha256, retired_at) "
           "VALUES (%s, %s, %s, %s, NOW())", (ver, "t", "real body", "00" * 32))
    assert "not the SHA-256" in str(exc.value), str(exc.value)


@needs_db
def test_publish_terms_version_stores_the_fingerprint_of_what_it_published(
        throwaway_schema, db_contracts):
    body = "## Published\n\nRetention is 30 days.\n"
    out = _publish(db_contracts, body)
    assert out["body_sha256"] == _contracts.body_fingerprint(body)
    served = db_contracts.get_current_terms()
    assert served["body_sha256"] == _contracts.body_fingerprint(body)
    assert served["body_sha256"] == out["body_sha256"]


# ══════════════════════════════════════════════════════════════════════════════════
# 4. THE SERVED FINGERPRINT IS THE FINGERPRINT OF THE SERVED TEXT
# ══════════════════════════════════════════════════════════════════════════════════

@needs_db
def test_the_served_fingerprint_describes_the_SERVED_body_not_the_stored_one(
        throwaway_schema, db_contracts, monkeypatch):
    """⚠️ THE SUBTLE HALF, AND GETTING IT WRONG WOULD BREAK EVERY SIGNATURE ON THE SEEDED TERMS.

    ``contract_templates.body_sha256`` fingerprints the STORED bytes. What the customer reads —
    and what ``record_acceptance`` snapshots — is the body AFTER read-time placeholder
    substitution. For a properly published version those are byte-identical. For migration 243's
    SQL-seeded template, which legitimately still carries ``{{TOKENS}}``, they are NOT.

    The binding must fingerprint THE DOCUMENT THE HUMAN SAW. If it echoed the stored column
    instead, the seeded terms would fingerprint as one thing, snapshot as another, and every
    signature against the default agreement would be refused."""
    from saas import platform_settings as ps
    monkeypatch.setattr(ps, "data_retention_days", lambda: 30)

    raw = _q(throwaway_schema,
             "SELECT id, body_markdown, body_sha256 FROM public.contract_templates "
             "WHERE version = '2026-08-12-v1'")
    assert raw, "the migration-243 seed is missing"
    tid, stored_body, stored_sha = raw[0]
    assert "{{RETENTION_DAYS}}" in stored_body, (
        "the seeded template no longer carries placeholders — this test's premise is gone; "
        "re-target it at a template that does, or delete it deliberately")

    served = db_contracts.get_terms_by_id(tid)
    assert "{{" not in served["body_markdown"]
    assert served["body_sha256"] == _contracts.body_fingerprint(served["body_markdown"])
    assert served["body_sha256"] != stored_sha, (
        "the served fingerprint equals the STORED one even though substitution changed the "
        "body — the fingerprint is describing the wrong document")


@needs_db
def test_changing_a_platform_setting_changes_the_fingerprint_with_the_version_UNCHANGED(
        throwaway_schema, db_contracts, monkeypatch):
    """🔴 THE FINDING THAT JUSTIFIES THE HASH. Same row, same id, same version string, DIFFERENT
    words — because the agreement's retention numbers are interpolated at read time. No
    id-or-label binding can see this; the fingerprint can."""
    from saas import platform_settings as ps
    tid = _q(throwaway_schema, "SELECT id FROM public.contract_templates "
                               "WHERE version = '2026-08-12-v1'")[0][0]

    monkeypatch.setattr(ps, "data_retention_days", lambda: 30)
    before = db_contracts.get_terms_by_id(tid)
    monkeypatch.setattr(ps, "data_retention_days", lambda: 7)
    after = db_contracts.get_terms_by_id(tid)

    assert before["id"] == after["id"]
    assert before["version"] == after["version"]           # the LABEL did not move…
    assert before["body_markdown"] != after["body_markdown"]   # …but the DOCUMENT did
    assert before["body_sha256"] != after["body_sha256"]
    assert "30 days" in before["body_markdown"] and "7 days" in after["body_markdown"]


# ══════════════════════════════════════════════════════════════════════════════════
# 5. THE HEADLINE EXPLOITS — reproduced, then refused
# ══════════════════════════════════════════════════════════════════════════════════

class _FakeProvider:
    """A CONFIGURED provider. Load-bearing: with an unconfigured one, begin_subscription refuses
    at step 2 and never REACHES the binding — the probe would 'pass' for the wrong reason.
    ``calls`` proves whether Stripe was touched at all."""
    name = "fake"

    def __init__(self):
        self.calls = []

    def is_configured(self):
        return True

    def create_checkout(self, **kw):
        self.calls.append(kw)
        return {"status": "checkout_created", "checkout_url":
                "https://checkout.stripe.com/c/pay/cs_test_BIND", "session_id": "cs_test_BIND"}


@pytest.fixture
def wired_flow(db_contracts, monkeypatch):
    """``tenant_checkout`` driven against the THROWAWAY control plane with a configured provider.

    Only the payment adapter is faked. contracts.py, its SQL, both migrations' triggers and the
    real transaction boundaries are all live — the binding is a database-and-ordering property and
    a mock cannot falsify it."""
    prov = _FakeProvider()
    monkeypatch.setattr(tc._payments, "get_payment_provider", lambda: prov)
    monkeypatch.setattr(tc._contracts.db, "connection", db_contracts.db.connection)
    # ASSERT THE FIXTURE, not just the result: an unconfigured provider makes every test below
    # pass for a reason that has nothing to do with the binding.
    assert tc._payments.get_payment_provider().is_configured() is True
    return prov


def _begin(**over):
    kw = dict(tier="team", signer_email="buyer@example.com", legal_name="Christopher Thompson",
              signer_ip="203.0.113.9", signer_user_agent="Mozilla/5.0 (test)",
              account_id=str(uuid.uuid4()), signer_user_id=str(uuid.uuid4()))
    kw.update(over)
    return tc.begin_subscription(**kw)


@needs_db
def test_H0_the_happy_path_signs_and_stamps_the_fingerprint(
        throwaway_schema, db_contracts, wired_flow):
    """The binding must not break the thing it guards. Also pins that EVERY new signature carries
    its ``contract_sha256`` — the column is nullable for history's sake, so the write path is the
    only thing that guarantees it."""
    body = "## Happy\n\nGoverned by the laws of the Province of Ontario.\n"
    _publish(db_contracts, body)
    shown = db_contracts.get_current_terms()

    out = _begin(expected_template_id=shown["id"], expected_version=shown["version"],
                 expected_sha256=shown["body_sha256"])
    assert out["ok"] is True and out["status"] == "checkout_created", out
    assert len(wired_flow.calls) == 1

    row = _q(throwaway_schema,
             "SELECT contract_snapshot, contract_sha256, contract_version "
             "FROM public.signed_contracts WHERE id = %s", (out["contract_id"],))[0]
    assert row[1] is not None, "a new signature was written with NO document fingerprint"
    assert row[1] == _contracts.body_fingerprint(row[0]), (
        "contract_sha256 is not the hash of the snapshot it sits on")
    assert row[1] == shown["body_sha256"], (
        "the row's fingerprint is not the one the page was served")
    assert row[2] == shown["version"]


@needs_db
def test_H1_publishing_a_new_version_mid_session_is_REFUSED_and_writes_NOTHING(
        throwaway_schema, db_contracts, wired_flow):
    """🔴 THE ORIGINAL EXPLOIT, REPRODUCED VERBATIM.

    The page renders v1 ("Province of Ontario"). An operator publishes v2 ("Delaware, USA. You
    waive all class-action rights.") while the customer is reading. The customer clicks accept.

    BEFORE: the POST re-read ``get_current_terms()`` and wrote a permanent, undeletable row
    quoting Delaware over a signature given for Ontario.
    NOW: 409, the CURRENT terms come back so the page can re-render, and the signed_contracts row
    count is UNCHANGED — asserted by counting rows, not by reading the code."""
    ontario = ("## Agreement\n\n### 9. Governing law\n\nThis agreement is governed by the laws of "
               "the Province of Ontario.\n")
    delaware = ("## Agreement\n\n### 9. Governing law\n\nThis agreement is governed by the laws of "
                "Delaware, USA. You waive all class-action rights.\n")

    _publish(db_contracts, ontario)
    shown = db_contracts.get_current_terms()          # what the PAGE rendered
    assert "Ontario" in shown["body_markdown"]

    before = _contract_count(throwaway_schema)
    v2 = _publish(db_contracts, delaware)             # the operator, mid-session
    assert "Delaware" in db_contracts.get_current_terms()["body_markdown"]

    out = _begin(expected_template_id=shown["id"], expected_version=shown["version"],
                 expected_sha256=shown["body_sha256"])

    assert out["ok"] is False, out
    assert out["status"] == "terms_changed", out
    assert out["contract_id"] is None and out["checkout_url"] is None
    # NOTHING WAS WRITTEN — counted, not inferred.
    assert _contract_count(throwaway_schema) == before, (
        "a permanent contract row was written against terms the customer never saw")
    # …and no payment surface was created either.
    assert wired_flow.calls == []
    # The refusal carries the NEW agreement so the page can re-render it.
    assert out["terms"] and out["terms"]["id"] == v2["id"]
    assert "Delaware" in out["terms"]["body_markdown"]
    assert out["terms"]["body_sha256"] == _contracts.body_fingerprint(
        out["terms"]["body_markdown"])
    assert "nothing was signed" in out["reason"].lower()


@needs_db
def test_H2_a_SILENT_REWRITE_under_a_stable_id_and_version_is_REFUSED(
        throwaway_schema, db_contracts, wired_flow, monkeypatch):
    """🔴 THE EXPLOIT ONLY THE FINGERPRINT CAN SEE, AND IT IS LIVE IN THIS CODEBASE TODAY.

    No new version is published. The template row does not move. Its id and its version string are
    byte-identical across the whole scenario. The operator changes ONE platform setting, and the
    agreement's retention clause is re-interpolated at read time — so the words being signed change
    under a page that has no way to know.

    An id binding sees nothing here. A version-label binding sees nothing here. The document
    fingerprint refuses, with ``terms_version_stale``, and writes nothing."""
    from saas import platform_settings as ps
    monkeypatch.setattr(ps, "data_retention_days", lambda: 30)

    body = ("## Agreement\n\n### 7. Your data\n\nYour data is retained for "
            "**{{RETENTION_DAYS}} days** after termination.\n")
    pub = _publish(db_contracts, body, version="rewrite-" + uuid.uuid4().hex[:8])
    # ⚠️ publish substitutes at PUBLISH time, so the stored row is already literal. To reproduce
    # the READ-TIME rewrite the row must still carry the token — write it raw, exactly as
    # migration 243's SQL seed does.
    _q(throwaway_schema, "UPDATE public.contract_templates SET retired_at = NOW() "
                         "WHERE retired_at IS NULL")
    ver = "seedlike-" + uuid.uuid4().hex[:8]
    _q(throwaway_schema,
       "INSERT INTO public.contract_templates (version, title, body_markdown, created_by) "
       "VALUES (%s, %s, %s, 'system-seed')", (ver, "FaultLine Subscription Agreement", body))
    assert pub["id"] is not None

    shown = db_contracts.get_current_terms()          # what the PAGE rendered, at 30 days
    assert "30 days" in shown["body_markdown"] and "{{" not in shown["body_markdown"]

    before = _contract_count(throwaway_schema)

    # THE OPERATOR MOVES THE SETTING. No publish, no new row, no version change.
    monkeypatch.setattr(ps, "data_retention_days", lambda: 7)
    now = db_contracts.get_current_terms()
    assert now["id"] == shown["id"], "the row moved — this is no longer the silent-rewrite case"
    assert now["version"] == shown["version"], "the label moved — the id/label binding would catch it"
    assert "7 days" in now["body_markdown"]           # …but the WORDS changed

    out = _begin(expected_template_id=shown["id"], expected_version=shown["version"],
                 expected_sha256=shown["body_sha256"])

    assert out["ok"] is False, out
    assert out["status"] == "terms_version_stale", out
    assert _contract_count(throwaway_schema) == before, (
        "a permanent row was written recording '7 days' over a signature given for '30 days'")
    assert wired_flow.calls == []
    assert out["terms"] and "7 days" in out["terms"]["body_markdown"]
    assert out["terms"]["body_sha256"] == now["body_sha256"]


@needs_db
def test_a_forged_or_absent_fingerprint_cannot_opt_out_of_the_binding(
        throwaway_schema, db_contracts, wired_flow):
    """The client does not get to decide whether the binding applies. An empty string, a garbage
    hex string and a wrong-but-well-formed hash are all refusals."""
    _publish(db_contracts, "## Body\n\nSome terms.\n")
    shown = db_contracts.get_current_terms()
    before = _contract_count(throwaway_schema)
    for bad in ("", "   ", "deadbeef", "00" * 32, _contracts.body_fingerprint("something else")):
        out = _begin(expected_template_id=shown["id"], expected_version=shown["version"],
                     expected_sha256=bad)
        assert out["ok"] is False and out["status"] == "terms_version_stale", (bad, out)
        assert _contract_count(throwaway_schema) == before, bad
    assert wired_flow.calls == []


@needs_db
def test_a_stale_version_LABEL_alone_is_refused(throwaway_schema, db_contracts, wired_flow):
    """Both arms are checked. The label is the cheap one and it still has to be right."""
    _publish(db_contracts, "## Body\n\nSome terms.\n")
    shown = db_contracts.get_current_terms()
    before = _contract_count(throwaway_schema)
    out = _begin(expected_template_id=shown["id"], expected_version="a-version-from-last-year",
                 expected_sha256=shown["body_sha256"])
    assert out["ok"] is False and out["status"] == "terms_version_stale", out
    assert _contract_count(throwaway_schema) == before
    assert wired_flow.calls == []


@needs_db
def test_the_WRITER_refuses_a_snapshot_that_is_not_the_document_it_was_told_to_record(
        throwaway_schema, db_contracts):
    """The last gate, driven directly at ``record_acceptance_guarded``.

    NOT redundant with the arms in ``begin_subscription``: those compared what the BROWSER sent
    against a read of the terms, and the writer then does its OWN read to take the snapshot. A
    read-time settings substitution can move the served text between those two reads with the id
    and the version unchanged, so the writer asserts that the bytes it is about to freeze are the
    document it was told to freeze.

    A mismatch must REFUSE, not warn-and-continue — the row is permanent and undeletable, so a
    snapshot the signer did not see is worse than no row at all."""
    tid = _q(throwaway_schema, "SELECT id FROM public.contract_templates "
                               "WHERE retired_at IS NULL ORDER BY effective_at DESC LIMIT 1")[0][0]
    before = _contract_count(throwaway_schema)

    out = db_contracts.record_acceptance_guarded(
        template_id=tid, signer_email="mism@example.com", signer_name="Mis Match", tier="team",
        expected_sha256=_contracts.body_fingerprint("a document that was never served"))
    assert out["ok"] is False and out["status"] == "snapshot_mismatch", out
    assert out["contract_id"] is None
    assert _contract_count(throwaway_schema) == before, (
        "a permanent row was written for a snapshot the caller said was the wrong document")

    # …and the CORRECT fingerprint still writes normally, so the guard is a gate and not a wall.
    served = db_contracts.get_terms_by_id(tid)
    out2 = db_contracts.record_acceptance_guarded(
        template_id=tid, signer_email="ok@example.com", signer_name="O K", tier="team",
        expected_sha256=served["body_sha256"])
    assert out2["ok"] is True, out2
    assert _contract_count(throwaway_schema) == before + 1


@needs_db
def test_the_write_path_DEGRADES_SAFELY_when_migration_252_is_not_applied(monkeypatch):
    """A control plane running 243 but not yet 252 must still take signatures.

    Naming an absent column would raise ``UndefinedColumn``, and an UndefinedColumn ABORTS THE
    ENCLOSING TRANSACTION — here, mid-way through an advisory-locked acceptance. The fail-SAFE
    direction on a write path is to record the signature and be loud about the missing
    fingerprint, never to lose the acceptance. Driven against a schema with ONLY 243 applied."""
    if not _DSN or _pg is None:
        pytest.skip("no DSN")
    import contextlib
    schema = "tb243only_" + uuid.uuid4().hex[:8]
    conn = _pg.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
            cur.execute(MIG_243.read_text().replace("public.", f"{schema}."))
            cur.execute(f"SELECT count(*) FROM {schema}.contract_templates")
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT 1 FROM pg_attribute "
                        f"WHERE attrelid = to_regclass('{schema}.signed_contracts') "
                        "AND attname = 'contract_sha256'")
            assert cur.fetchone() is None, "this fixture is not a 243-only schema"

        class _C:
            def __init__(self, c):
                self._c = c

            def execute(self, q, p=None):
                return self._c.execute(q.replace("public.", f"{schema}."), p)

            def __getattr__(self, n):
                return getattr(self._c, n)

            def __enter__(self):
                self._c.__enter__()
                return self

            def __exit__(self, *e):
                return self._c.__exit__(*e)

        class _K:
            def __init__(self, c):
                self._c = c

            def cursor(self, *a, **kw):
                return _C(self._c.cursor(*a, **kw))

            def __getattr__(self, n):
                return getattr(self._c, n)

        @contextlib.contextmanager
        def _conn():
            c = _pg.connect(_DSN)
            try:
                yield _K(c)
            finally:
                c.close()

        monkeypatch.setattr(_contracts.db, "connection", _conn)
        _DB_RAN["value"] = True

        terms = _contracts.get_current_terms()
        assert terms and terms["body_sha256"], (
            "the SERVED fingerprint must still be computed with no stored column — it is "
            "computed in Python from the served body, not read from the table")

        cid = _contracts.record_acceptance(
            template_id=terms["id"], signer_email="old@example.com",
            signer_name="Old Schema", tier="team")
        assert cid, "the signature was LOST because the fingerprint column does not exist yet"
        with conn.cursor() as cur:
            cur.execute(f"SELECT contract_snapshot FROM {schema}.signed_contracts WHERE id = %s",
                        (cid,))
            assert cur.fetchone()[0]
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════════
# 6. THE RECORDED FINGERPRINT IS AN ACCEPTANCE FACT
# ══════════════════════════════════════════════════════════════════════════════════

@needs_db
def test_contract_sha256_is_IMMUTABLE_on_a_signed_row(throwaway_schema, db_contracts):
    """A mutable hash of an immutable document proves nothing. Asserted on the REAL error text,
    with the trigger's enabled state re-checked immediately beforehand — a probe suite that has
    disabled a trigger earlier and forgotten to restore it produces a convincing false finding."""
    enabled = _q(throwaway_schema,
                 "SELECT tgenabled FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                 "WHERE c.relname = 'signed_contracts' "
                 "AND t.tgname = 'trg_signed_contracts_immutable'")
    assert enabled and enabled[0][0] == "O", f"the trigger under test is not enabled: {enabled}"

    tid = _q(throwaway_schema, "SELECT id FROM public.contract_templates "
                               "WHERE retired_at IS NULL ORDER BY effective_at DESC LIMIT 1")[0][0]
    cid = db_contracts.record_acceptance(
        template_id=tid, signer_email="imm@example.com", signer_name="Imm Utable", tier="team")
    assert cid

    original = _q(throwaway_schema,
                  "SELECT contract_sha256 FROM public.signed_contracts WHERE id = %s", (cid,))[0][0]
    assert original

    # ⚠️ AN IMMUTABILITY PROBE MUST CHANGE THE VALUE, NOT RE-ASSERT IT — ``IS DISTINCT FROM`` is
    # false for a no-op UPDATE and the guard would look absent.
    with pytest.raises(Exception) as exc:
        _q(throwaway_schema,
           "UPDATE public.signed_contracts SET contract_sha256 = %s WHERE id = %s",
           ("11" * 32, cid))
    assert "contract_sha256 is an acceptance fact" in str(exc.value), str(exc.value)

    assert _q(throwaway_schema,
              "SELECT contract_sha256 FROM public.signed_contracts WHERE id = %s",
              (cid,))[0][0] == original


@needs_db
def test_body_sha256_is_write_once_and_can_only_hold_the_true_hash(
        throwaway_schema, db_contracts):
    """The template column's decision, as documented in migration 252: WRITE-ONCE rather than
    strictly immutable (so a NULL row can be repaired), and the ONE value a repair may take is the
    correct hash (so the repair cannot lie)."""
    ver = "wo-" + uuid.uuid4().hex[:8]
    body = "## Write once\n\nBody.\n"
    _q(throwaway_schema,
       "INSERT INTO public.contract_templates (version, title, body_markdown, retired_at) "
       "VALUES (%s, %s, %s, NOW())", (ver, "t", body))
    real = _contracts.body_fingerprint(body)

    with pytest.raises(Exception) as exc:
        _q(throwaway_schema,
           "UPDATE public.contract_templates SET body_sha256 = %s WHERE version = %s",
           ("22" * 32, ver))
    assert "write-once" in str(exc.value), str(exc.value)
    assert _q(throwaway_schema,
              "SELECT body_sha256 FROM public.contract_templates WHERE version = %s",
              (ver,))[0][0] == real


# ══════════════════════════════════════════════════════════════════════════════════
# 7. TRANSPORT — status → HTTP, and the page's own contract
# ══════════════════════════════════════════════════════════════════════════════════

def test_the_stale_statuses_map_to_409_and_the_map_is_exact():
    """BRAIN NOT TRANSPORT: the module decided; this only chooses how to say it. An exact dict
    lookup with a 502 default — our faults are 5xx, and a stale submit is the customer's browser
    holding an old page, so it is a 409 CONFLICT and not a 500."""
    from saas import tenant_checkout_routes as r
    assert r._STATUS_HTTP["terms_version_stale"] == 409
    assert r._STATUS_HTTP["terms_changed"] == 409
    assert r._STATUS_HTTP.get("no_active_terms") == 503
    assert r._STATUS_HTTP.get("contract_write_failed") == 503
    assert r._STATUS_HTTP.get("nonexistent_status_invented_later") is None  # → 502 default
    assert r._STATUS_CARRIES_TERMS == frozenset({"terms_changed", "terms_version_stale"})


def test_the_binding_tokens_are_carried_by_the_schema_and_ENFORCED_BY_THE_BRAIN():
    """WHERE "required" is enforced decides what the customer sees when it is not met.

    The two document tokens are OPTIONAL in the Pydantic model on purpose: a page that predates
    the binding cannot echo them, and a schema-level requirement answers that with a 422 naming
    fields the customer has never heard of and no way forward. Declared optional, they reach
    ``begin_subscription`` as None, and the brain — the only layer that knows what the current
    agreement IS — refuses with a 409 carrying the text needed to recover. BRAIN NOT TRANSPORT.

    If someone "tightens" these to ``Field(...)`` later, this test says why not."""
    from saas.tenant_checkout_routes import SubscribeRequest
    fields = SubscribeRequest.model_fields
    for name in ("terms_version", "terms_sha256"):
        assert name in fields, f"{name} is not on the subscribe body"
        assert not fields[name].is_required(), (
            f"{name} is schema-REQUIRED again — that turns an out-of-date page into a bare 422 "
            f"with no recovery path. Enforce presence in begin_subscription (ARM 0) instead.")
    # …and a body without them still VALIDATES, so the request reaches the brain at all.
    ok = SubscribeRequest(tier="team", legal_name="A B", accept=True, contract_template_id=1)
    assert ok.terms_version is None and ok.terms_sha256 is None
    # The template id remains schema-required: every page build that has ever posted here sends
    # it, so its absence is a malformed request, not a stale page.
    assert fields["contract_template_id"].is_required()


def test_the_brain_refuses_a_caller_that_cannot_state_what_it_rendered():
    """ARM 0, at the unit level and with no DB: absence is the STALE case, and there is no
    opt-out parameter to reintroduce the hole."""
    import inspect
    src = inspect.getsource(tc.begin_subscription)
    assert "ARM 0: THE TOKENS MUST BE PRESENT AT ALL" in src
    params = inspect.signature(tc.begin_subscription).parameters
    for name in ("expected_template_id", "expected_version", "expected_sha256"):
        assert name in params
    assert not any("skip" in p or "require_binding" in p or "enforce" in p for p in params), (
        "an opt-out parameter for the binding has appeared — an optional binding is not a "
        "binding, which is exactly how the pre-fix hole worked")


def test_get_tenant_plans_serves_the_fingerprint():
    """The page cannot echo what it was never given. Read off the route's own source so this
    cannot pass against a payload that merely happens to contain the word."""
    import inspect
    from saas import tenant_checkout_routes as r
    src = inspect.getsource(r.tenant_plans)
    assert '"body_sha256": terms.get("body_sha256")' in src
    assert '"version": terms.get("version")' in src


# ── the page ──────────────────────────────────────────────────────────────────────

_JS = REPO / "saas" / "dashboard" / "subscribe.js"
_HTML = REPO / "saas" / "dashboard" / "subscribe.html"


def test_the_page_echoes_the_version_and_the_fingerprint_it_rendered():
    js = _JS.read_text()
    assert "terms_version: String(termsVersion)" in js
    assert "terms_sha256: String(termsSha)" in js
    assert "S.terms && S.terms.body_sha256" in js
    # It must ECHO, never COMPUTE — a page that hashes its own DOM could claim a fingerprint for
    # text it never received from us. Checked against the browser APIs that could actually do it,
    # not against the substring "sha256" (which appears in this file's own explanatory comments).
    for api in ("crypto.subtle", "digest(", "SubtleCrypto", "createHash"):
        assert api not in js, f"the page appears to compute a hash itself ({api})"


def test_a_409_clears_the_tick_AND_the_typed_signature():
    """Do not silently swap the text under a still-ticked box and a still-typed name — that is the
    original defect reproduced at the client."""
    js = _JS.read_text()
    block = js[js.index("function reloadTermsAfterConflict"):js.index("function rearm()")]
    assert "$('agree').checked = false;" in block
    assert "nameEl.value = ''" in block
    assert "S.readAll = false" in block
    assert "S.selected = null" in block


def test_the_409_handler_survives_a_structured_detail():
    """FastAPI's ``detail`` is a JSON OBJECT on the stale-agreement 409s. Without ``detailText``
    the customer would be shown the literal string "[object Object]"."""
    js = _JS.read_text()
    assert "function detailText(" in js
    assert "function conflictTerms(" in js
    assert "reloadTermsAfterConflict(friendly(err), conflictTerms(err))" in js


def test_the_page_is_CSP_CLEAN_no_inline_script_no_event_handler_attributes():
    """⚠️ The dashboard CSP is ``script-src 'self'`` with NO ``'unsafe-inline'``. An inline-script
    build renders BLANK — nothing errors, the page is simply empty."""
    import re
    html = _HTML.read_text()
    inline = [m for m in re.findall(r"<script\b[^>]*>", html) if "src=" not in m]
    assert inline == [], f"inline <script> blocks would be blocked by CSP: {inline}"
    handlers = re.findall(r"\son[a-z]+\s*=", html, flags=re.I)
    assert handlers == [], f"inline event-handler attributes are CSP-blocked: {handlers}"
    assert '<script src="/dashboard/subscribe.js"></script>' in html


# ══════════════════════════════════════════════════════════════════════════════════
# 8. FALSE-GREEN TRIPWIRE — MUST BE LAST
# ══════════════════════════════════════════════════════════════════════════════════

def test_zzz_no_db_test_was_silently_skipped():
    """Fails the run if the DB-backed half never executed.

    Everything this suite is FOR — the backfill, the two triggers, the row counts on the refusal
    paths, the Postgres↔Python hash equality — is a database property. A green with the DB lane
    skipped proves none of it. Do NOT delete this to make a run green; supply POSTGRES_DSN
    pointing at a THROWAWAY database."""
    assert _DB_RAN["value"], (
        "the DB-backed half of this suite never ran (POSTGRES_DSN unset/unreachable, psycopg2 "
        f"missing, or migrations 243/252 absent) — POSTGRES_DSN={_DSN!r}. A green without it is "
        "a FALSE GREEN.")
