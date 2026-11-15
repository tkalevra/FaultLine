"""Migration 256 — FROZEN CONTRACT NUMBERS + THE CHANGELOG / NOTICE RECORD + THE ARCHIVE.

THREE THINGS THIS SUITE FALSIFIES
─────────────────────────────────

A. A PUBLISHED AGREEMENT'S WORDS MUST NOT MOVE. ``saas/contracts.py::_substitute_for_display``
   interpolates live platform settings into the seeded template AT READ TIME, because migrations
   243 and 253 seed their bodies in SQL with the raw ``{{TOKENS}}`` still in them and SQL cannot
   call the publish seam. So an operator editing ``data_retention_days`` rewrote section 7 of the
   agreement IN FORCE, with the template id and version both unchanged. Migration 252's
   fingerprint DETECTS that (the signature is refused rather than recorded wrongly); migration
   256 FIXES it by freezing each version's numbers into ``contract_templates.retention_values``
   and preferring them over live settings.

B. A NEW VERSION MUST SAY WHAT CHANGED. "Courts have ruled that providing revised terms without
   indicating what changed was not sufficient notice" — Ironclad, *6 Components of Clickwrap
   Enforceability*,
   https://ironcladapp.com/journal/contract-management/6-components-of-clickwrap-enforceability
   Section 8 of the agreement itself promises a 30-day notice and a right to cancel for a
   MATERIAL change, which cannot be honoured or audited if nobody recorded which changes were
   material. ``publish_terms_version`` now REFUSES a supersession with no summary.

C. THE CUSTOMER MUST BE ABLE TO READ WHAT THEY SIGNED. Section 8: "you can obtain a complete copy
   at any time". That sentence had ZERO consumers — the routes existed and nothing called them.

⚠️ FALSE-GREEN GUARD: ``_DB_RAN`` is set by the DB fixture and asserted by the LAST test. Every
claim in A and B is a database property (a backfill, a trigger, a transaction), so a run reporting
"0 passed, N skipped" is RED. Run with a THROWAWAY DSN and assert skipped == 0.
"""

import os
import re
import uuid
from pathlib import Path

import pytest

# Control-plane singletons (the append-only ACTIVE contract chain, the platform settings row,
# the global operator realm) are shared by every suite in one database: the suites that
# write or depend on them run on one xdist worker (under --dist loadgroup, see tests/conftest.py).
pytestmark = pytest.mark.xdist_group("control_plane")

from saas import contracts as _contracts


REPO = Path(__file__).resolve().parent.parent
MIGS = [REPO / "migrations" / n for n in (
    "243_contract_infrastructure.sql",
    "252_contract_document_fingerprint.sql",
    "256_contract_changelog_and_frozen_terms.sql",
)]
MIG_246 = REPO / "migrations" / "246_retention_settings.sql"
MIG_256 = MIGS[2]

_DSN = os.environ.get("POSTGRES_DSN")
_SCHEMA = "tc256_" + uuid.uuid4().hex[:8]

try:
    import psycopg2 as _pg
except Exception:  # pragma: no cover
    _pg = None

needs_db = pytest.mark.skipif(
    not _DSN or _pg is None or not all(m.exists() for m in MIGS),
    reason="POSTGRES_DSN + psycopg2 + migrations 243/252/256 required")

_DB_RAN = {"value": False}


# ══════════════════════════════════════════════════════════════════════════════════
# 1. THE TOKEN MAP IS STATED TWICE — prove the two copies agree
# ══════════════════════════════════════════════════════════════════════════════════

def test_the_migration_and_the_module_agree_on_the_token_map():
    """🔴 A migration cannot import Python, so migration 256's freeze restates the
    token → platform_settings-column mapping that ``_RETENTION_PLACEHOLDER_SOURCES`` owns. Two
    copies of a map that decides what number goes into a legal document WILL drift; this makes
    the drift a red test instead of a wrong contract.

    Both directions are checked: every token the module knows must be frozen by the migration,
    and the migration must not freeze a token the module has never heard of (which would be
    dead JSON the substituter silently ignores)."""
    sql = MIG_256.read_text()
    block = sql[sql.index("jsonb_build_object"):sql.index("GET DIAGNOSTICS frozen")]
    pairs = dict(re.findall(r"'([A-Z_]+)',\s*ps\.([a-z_]+)", block))
    expected = {tok: col for tok, (col, _desc)
                in _contracts._RETENTION_PLACEHOLDER_SOURCES.items()}
    assert pairs == expected, (
        f"migration 256 freezes {pairs} but the module resolves {expected} — the two copies of "
        f"the token map have drifted, so a frozen version would state the wrong number")


def test_every_frozen_column_actually_exists_on_platform_settings():
    """The migration reads ``ps.<column>``; migration 246 is what creates them. A rename there
    would make the freeze silently fail (or hard-error) with nothing else noticing."""
    ddl = MIG_246.read_text()
    for _tok, (col, _d) in _contracts._RETENTION_PLACEHOLDER_SOURCES.items():
        assert f"ADD COLUMN IF NOT EXISTS {col} " in ddl, (
            f"{col} is not a platform_settings column created by migration 246")


def test_a_junk_frozen_value_falls_back_to_the_live_setting():
    """Pure. A frozen set is JSONB, and a junk entry must not render ``None days`` or leave the
    raw token in a legal document — it falls back to the live setting."""
    body = "retained for {{RETENTION_DAYS}} days"
    for junk in ({"RETENTION_DAYS": None}, {"RETENTION_DAYS": "abc"},
                 {"RETENTION_DAYS": -1}, {"RETENTION_DAYS": ""}, {}):
        out = _contracts._substitute_for_display(
            body, where="t", created_by="system-seed", frozen_values=junk)
        assert "{{" not in out, junk
        assert " days" in out and "None" not in out, (junk, out)


def test_a_PARTIAL_frozen_set_freezes_what_it_covers_and_no_more():
    """A future token added to the map must not invalidate every already-frozen row: the frozen
    set wins per TOKEN, and anything it does not cover falls back to live."""
    body = "{{RETENTION_DAYS}} / {{FULFILMENT_DAYS}}"
    out = _contracts._substitute_for_display(
        body, where="t", created_by="system-seed", frozen_values={"RETENTION_DAYS": 99})
    assert out.startswith("99 / ")
    assert "{{" not in out


# ══════════════════════════════════════════════════════════════════════════════════
# 2. THE THROWAWAY SCHEMA
# ══════════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def throwaway_schema():
    if not _DSN or _pg is None:
        pytest.skip("no DSN")
    conn = _pg.connect(_DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {_SCHEMA}")
        # A minimal platform_settings standing in for migration 246's columns — the freeze reads
        # it, and 246 also carries a great deal of unrelated machinery. The COLUMN NAMES are
        # pinned against 246's real DDL by test_every_frozen_column_actually_exists_on_platform_settings.
        cur.execute(f"""
            CREATE TABLE {_SCHEMA}.platform_settings (
                singleton BOOLEAN PRIMARY KEY DEFAULT TRUE,
                data_retention_days   INTEGER NOT NULL DEFAULT 30,
                backup_overwrite_days INTEGER NOT NULL DEFAULT 35,
                fulfilment_days       INTEGER NOT NULL DEFAULT 5,
                contract_term_months  INTEGER NOT NULL DEFAULT 6);
            INSERT INTO {_SCHEMA}.platform_settings (singleton) VALUES (TRUE);
            ALTER TABLE {_SCHEMA}.platform_settings
                  ADD COLUMN IF NOT EXISTS retention_grace_days INTEGER NOT NULL DEFAULT 7;
        """)
        for mig in MIGS:
            # ⚠️ ORDER IS LOAD-BEARING AND THE FIRST CUT GOT IT WRONG: migration 256's freeze is
            # guarded on contract_templates.retention_values existing (migration 246 adds it, and
            # 246 < 256 in any real run). Adding the column AFTER the loop made 256 take its
            # "column absent, skip the freeze" branch, and two tests failed reporting a product
            # defect that was a fixture defect. Real apply order, or the sandbox tests nothing.
            if mig.name.startswith("256"):
                cur.execute(f"ALTER TABLE {_SCHEMA}.contract_templates "
                            f"ADD COLUMN IF NOT EXISTS retention_values JSONB")
            cur.execute(mig.read_text().replace("public.", f"{_SCHEMA}."))
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    conn.close()


class _CurProxy:
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


# ══════════════════════════════════════════════════════════════════════════════════
# 3. PART A — THE FREEZE
# ══════════════════════════════════════════════════════════════════════════════════

@needs_db
def test_the_migration_froze_the_seeded_template(throwaway_schema, db_contracts):
    """Migration 256 stamps ``retention_values`` on every template that still carries tokens."""
    rows = _q(throwaway_schema,
              "SELECT version, retention_values FROM public.contract_templates "
              "WHERE body_markdown LIKE '%{{%'")
    assert rows, "no token-bearing template — this sandbox cannot exercise the freeze"
    for ver, vals in rows:
        assert vals, f"{ver} still has NO frozen numbers — its words remain mobile"
        for tok in _contracts._RETENTION_PLACEHOLDER_SOURCES:
            assert tok in vals, (ver, tok)
        assert vals.get("frozen_by") == "migration-256"


@needs_db
def test_A_CHANGING_A_PLATFORM_SETTING_NO_LONGER_REWRITES_THE_LIVE_AGREEMENT(
        throwaway_schema, db_contracts, monkeypatch):
    """🔴 THE HEADLINE FOR PART A, AND THE EXACT SCENARIO THAT MOTIVATED IT.

    Before: an operator moving ``data_retention_days`` 30 → 7 rewrote section 7 of the agreement
    in force, with the template id AND version byte-identical, so a customer mid-read would sign
    words they never saw (caught, but only as a 409, by migration 252's fingerprint).

    After: the version's OWN frozen number governs. The live setting decides what the NEXT
    published version will say and nothing else."""
    from saas import platform_settings as ps
    tid = _q(throwaway_schema, "SELECT id FROM public.contract_templates "
                               "WHERE version = '2026-08-12-v1'")[0][0]

    monkeypatch.setattr(ps, "data_retention_days", lambda: 30)
    before = db_contracts.get_terms_by_id(tid)
    monkeypatch.setattr(ps, "data_retention_days", lambda: 7)
    after = db_contracts.get_terms_by_id(tid)

    assert before["id"] == after["id"] and before["version"] == after["version"]
    assert before["body_markdown"] == after["body_markdown"], (
        "the words of a published agreement MOVED when a platform setting changed")
    assert before["body_sha256"] == after["body_sha256"], (
        "the document fingerprint moved — a customer mid-signature would be bounced with a 409 "
        "every time an operator edits a setting")
    # …and the frozen value is the one actually rendered.
    frozen = _q(throwaway_schema, "SELECT retention_values FROM public.contract_templates "
                                  "WHERE id = %s", (tid,))[0][0]
    assert f"{int(frozen['RETENTION_DAYS'])} days" in after["body_markdown"]


@needs_db
def test_an_UNFROZEN_token_bearing_row_still_renders_and_screams(
        throwaway_schema, db_contracts, monkeypatch):
    """A row that entered after the migration with no frozen set must still render correctly —
    the read-side net is retained — but it is a standing hazard and says so loudly."""
    from saas import platform_settings as ps
    monkeypatch.setattr(ps, "data_retention_days", lambda: 21)
    ver = "unfrozen-" + uuid.uuid4().hex[:8]
    _q(throwaway_schema,
       "INSERT INTO public.contract_templates (version, title, body_markdown, created_by, "
       "retired_at) VALUES (%s, 't', 'retained for {{RETENTION_DAYS}} days', 'someone', NOW())",
       (ver,))
    tid = _q(throwaway_schema,
             "SELECT id FROM public.contract_templates WHERE version = %s", (ver,))[0][0]
    got = db_contracts.get_terms_by_id(tid)
    assert "retained for 21 days" == got["body_markdown"]
    assert "{{" not in got["body_markdown"]


# ══════════════════════════════════════════════════════════════════════════════════
# 4. PART B — THE CHANGELOG
# ══════════════════════════════════════════════════════════════════════════════════

@needs_db
def test_B_PUBLISHING_OVER_AN_INCUMBENT_WITHOUT_SAYING_WHAT_CHANGED_IS_REFUSED(
        throwaway_schema, db_contracts):
    """🔴 THE HEADLINE FOR PART B. "Providing revised terms without indicating what changed was
    not sufficient notice." The refusal must also change NOTHING — the incumbent stays active,
    which is the property this function has historically got wrong (it once left ZERO active
    versions while returning ok=True)."""
    active_before = _q(throwaway_schema,
                       "SELECT version FROM public.contract_templates WHERE retired_at IS NULL")
    n_before = _q(throwaway_schema, "SELECT count(*) FROM public.contract_templates")[0][0]

    out = db_contracts.publish_terms_version(
        version="nolog-" + uuid.uuid4().hex[:8], title="T",
        body_markdown="## New\n\nDifferent words.\n", created_by="test")
    assert out.get("ok") is False, out
    assert "what changed" in out.get("reason", "")
    assert out.get("supersedes")

    assert _q(throwaway_schema,
              "SELECT version FROM public.contract_templates "
              "WHERE retired_at IS NULL") == active_before, (
        "the refusal retired the incumbent anyway — the platform would have NO active terms")
    assert _q(throwaway_schema,
              "SELECT count(*) FROM public.contract_templates")[0][0] == n_before

    # …and a blank / whitespace summary is not a summary.
    for blank in ("", "   ", "\n"):
        out = db_contracts.publish_terms_version(
            version="blank-" + uuid.uuid4().hex[:8], title="T", body_markdown="## X\n",
            created_by="test", change_summary=blank)
        assert out.get("ok") is False, blank


@needs_db
def test_a_supersession_WITH_a_summary_publishes_and_records_the_notice_record(
        throwaway_schema, db_contracts):
    prev = db_contracts.get_current_terms()["version"]
    ver = "withlog-" + uuid.uuid4().hex[:8]
    out = db_contracts.publish_terms_version(
        version=ver, title="T", body_markdown="## New\n\nDifferent words.\n",
        created_by="test",
        change_summary="Section 3 now bills monthly in advance instead of quarterly.",
        change_is_material=True)
    assert out.get("ok") is True, out
    assert out["supersedes_version"] == prev
    assert out["change_is_material"] is True

    now = db_contracts.get_current_terms()
    assert now["version"] == ver
    assert now["change_summary"].startswith("Section 3")
    assert now["change_is_material"] is True
    assert now["supersedes_version"] == prev


@needs_db
def test_materiality_defaults_to_TRUE_when_it_is_not_stated(throwaway_schema, db_contracts):
    """The two errors are not symmetric: wrongly-material costs an unnecessary notice, wrongly
    non-material means a promised 30-day notice was never given. Default toward the promise."""
    out = db_contracts.publish_terms_version(
        version="defmat-" + uuid.uuid4().hex[:8], title="T", body_markdown="## Y\n",
        created_by="test", change_summary="Something changed.")
    assert out.get("ok") is True, out
    assert out["change_is_material"] is True


@needs_db
def test_the_changelog_is_WRITE_ONCE(throwaway_schema, db_contracts):
    """It is the record of what customers were TOLD had changed. A NULL may be documented later
    (a repair); a stated summary may never be rewritten."""
    enabled = _q(throwaway_schema,
                 "SELECT tgenabled FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                 "WHERE c.relname = 'contract_templates' "
                 "AND t.tgname = 'trg_contract_templates_immutable'")
    assert enabled and enabled[0][0] == "O", f"the trigger under test is not enabled: {enabled}"

    ver = "wolog-" + uuid.uuid4().hex[:8]
    _q(throwaway_schema,
       "INSERT INTO public.contract_templates (version, title, body_markdown, retired_at) "
       "VALUES (%s, 't', 'body', NOW())", (ver,))
    # NULL → value is the permitted REPAIR.
    _q(throwaway_schema,
       "UPDATE public.contract_templates SET change_summary = %s WHERE version = %s",
       ("documented after the fact", ver))
    # value → different value is REFUSED. (Changing it, not re-asserting it — an immutability
    # probe that writes the same value proves nothing, IS DISTINCT FROM is false for a no-op.)
    with pytest.raises(Exception) as exc:
        _q(throwaway_schema,
           "UPDATE public.contract_templates SET change_summary = %s WHERE version = %s",
           ("a different story", ver))
    assert "write-once" in str(exc.value), str(exc.value)
    assert _q(throwaway_schema,
              "SELECT change_summary FROM public.contract_templates WHERE version = %s",
              (ver,))[0][0] == "documented after the fact"


@needs_db
def test_the_shipped_versions_carry_an_honest_backfilled_changelog(throwaway_schema):
    """v1 has no predecessor, so it claims none. v2's summary must describe the elastic-seats
    change that migration 253 actually made."""
    v1 = _q(throwaway_schema,
            "SELECT change_summary, change_is_material, supersedes_version "
            "FROM public.contract_templates WHERE version = '2026-08-12-v1'")[0]
    assert "no earlier version" in v1[0]
    assert v1[1] is None, "the FIRST version is not a 'change' — materiality must not be claimed"
    assert v1[2] is None


# ══════════════════════════════════════════════════════════════════════════════════
# 5. PART C — THE ARCHIVE (the section-8 consumer that did not exist)
# ══════════════════════════════════════════════════════════════════════════════════

_HTML = REPO / "saas" / "dashboard" / "agreements.html"
_JS = REPO / "saas" / "dashboard" / "agreements.js"
_TENANT_HTML = REPO / "saas" / "dashboard" / "tenant.html"
_HTTP = REPO / "src" / "mcp" / "http_server.py"


# ⚠️ SCAN THE CODE, NOT THE PROSE. Three assertions in this file failed on their own explanatory
# comments — a comment saying *"do NOT turn this into 'we notified you'"* matched a test looking
# for the string "we notified"; a comment saying *"no inline `<script>`"* matched the inline-script
# detector. A source file that DOCUMENTS a rule must not be indistinguishable from one that BREAKS
# it, so every content assertion below runs against comment-stripped source.
def _strip_html_comments(s: str) -> str:
    return re.sub(r"<!--.*?-->", "", s, flags=re.S)


def _strip_js_comments(s: str) -> str:
    # Block comments first, then whole-line // comments. Deliberately does NOT try to handle //
    # inside a string literal: this file's only such case is a URL in a comment, and a stricter
    # parser here would be more code than the thing it guards.
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return "\n".join(ln for ln in s.splitlines() if not ln.lstrip().startswith("//"))


def test_the_archive_page_exists_and_is_reachable_from_the_console():
    assert _HTML.exists() and _JS.exists()
    assert '/dashboard/agreements.html' in _TENANT_HTML.read_text(), (
        "the archive has no entry point — section 8's 'obtain a complete copy at any time' "
        "still has no consumer")


def test_the_archive_is_registered_on_BOTH_face_gates():
    """⚠️ StaticFiles serves any file dropped into saas/dashboard/, so a new console page is
    exposed on the OPERATOR face until it is listed, and renders during maintenance while its
    /tenant/* API is default-denied. Both are easy to forget and neither fails loudly."""
    src = _HTTP.read_text()
    tenant_block = src[src.index("_TENANT_CONSOLE_EXACT"):src.index("_TENANT_CONSOLE_EXACT") + 2000]
    assert '"/dashboard/agreements.html"' in tenant_block
    assert '"/dashboard/agreements.js"' in tenant_block
    off = src[src.index("_OFFLINE_BLOCKED_PAGES"):src.index("_OFFLINE_BLOCKED_PAGES") + 600]
    assert "/dashboard/agreements.html" in off


def test_the_archive_renders_the_SNAPSHOT_and_never_the_current_template():
    """The signed copy is a DIFFERENT DOCUMENT from today's terms. ESIGN § 7001(d)(1)(A): the
    retained record must "accurately reflect the information set forth in the contract"."""
    js = _strip_js_comments(_JS.read_text())
    assert "c.contract_snapshot" in js
    assert "body_markdown" not in js, (
        "the archive reads a TEMPLATE body — it must render the signed snapshot only")


def test_the_archive_never_claims_a_notice_was_sent():
    """Nothing in this codebase emails customers about a new version. A page implying it did
    would be a second false clause layered on the one this page fixes."""
    text = (_strip_js_comments(_JS.read_text())
            + _strip_html_comments(_HTML.read_text())).lower()
    for claim in ("we emailed", "we notified", "you were notified", "notification sent",
                  "we have emailed", "we sent you"):
        assert claim not in text, f"the archive claims a notice was sent: {claim!r}"


def test_the_archive_distinguishes_NO_FINGERPRINT_from_a_MISMATCH():
    """Tri-state, and the third state matters: a pre-252 agreement has no fingerprint, which is
    NOT a verification failure. Collapsing it to False would tell a customer their oldest
    agreement failed verification — false, and alarming."""
    js = _strip_js_comments(_JS.read_text())
    assert "c.fingerprint_matches === true" in js
    assert "c.fingerprint_matches === false" in js
    assert "signed before document fingerprinting" in js
    from saas import tenant_checkout_routes as r
    import inspect
    src = inspect.getsource(r.tenant_get_contract)
    assert 'None if not contract.get("contract_sha256")' in src, (
        "the route collapses 'no fingerprint recorded' into 'mismatch'")


def test_a_failed_load_is_never_rendered_as_an_empty_archive():
    """"You have no agreements" is a statement about a legal record. It must never be produced
    by a network error."""
    js = _strip_js_comments(_JS.read_text())
    assert "NOT a statement that you have no agreements" in js


def test_the_archive_is_CSP_CLEAN():
    """script-src 'self', no 'unsafe-inline'. An inline-script build renders BLANK, silently."""
    html = _strip_html_comments(_HTML.read_text())
    inline = [m for m in re.findall(r"<script\b[^>]*>", html) if "src=" not in m]
    assert inline == [], f"inline <script> would be CSP-blocked: {inline}"
    handlers = re.findall(r"\son[a-z]+\s*=", html, flags=re.I)
    assert handlers == [], f"inline event-handler attributes are CSP-blocked: {handlers}"
    assert '<script src="/dashboard/agreements.js"></script>' in html
    # …and the console link that reaches it must be an anchor, not a button: tenant.js wires
    # tabs exclusively via `nav.tabs button`, so an anchor needs no JS edit and cannot perturb
    # the active-class toggle of a 367KB file another agent owns.
    assert '<a class="tab-link" href="/dashboard/agreements.html">' in _TENANT_HTML.read_text()


def test_the_archive_is_READ_ONLY():
    """No write on a page about permanent legal records — no cancel, no re-sign, no acknowledge.
    The only fetch verb is GET."""
    js = _strip_js_comments(_JS.read_text())
    assert "method: 'GET'" in js
    for verb in ("'POST'", "'PUT'", "'DELETE'", "'PATCH'"):
        assert verb not in js, f"the archive issues a {verb} — it must be read-only"


def test_the_archive_never_sends_an_account_id_from_the_client():
    """BOLA: account_id is derived SERVER-side from the session. A client-supplied one on a
    contract read is exactly the hole this surface must not open."""
    js = _strip_js_comments(_JS.read_text())
    assert "account_id" not in js


# ══════════════════════════════════════════════════════════════════════════════════
# 6. FALSE-GREEN TRIPWIRE — MUST BE LAST
# ══════════════════════════════════════════════════════════════════════════════════

def test_zzz_no_db_test_was_silently_skipped():
    assert _DB_RAN["value"], (
        "the DB-backed half of this suite never ran (POSTGRES_DSN unset/unreachable, psycopg2 "
        f"missing, or migrations 243/252/256 absent) — POSTGRES_DSN={_DSN!r}. Parts A and B are "
        f"entirely database properties; a green without them proves nothing.")
