"""Connection POISON TEST — the acceptance gate for the per-account isolation grain.

Maps to ``DEV/DESIGN-per-account-grain-and-api-subject.md`` §2.2 (the CRUX). The guard under
test is ``saas/subject_cursor.py`` (Phase 1a foundation).

The four §2.2 gates:
  #1 Unbound throws            — GREEN now (no DB: the throw precedes any connection).
  #2 Cross-subject is empty    — SKIP (gated on Phase 1b sweep: factory wired into read paths + live DB).
  #3 Cross-account is denied   — SKIP (gated on Phase 1b: bind_tenant wiring + live DB).
  #4 Grep-audit lint           — GREEN now (structural lint; scoped to the factory module,
                                  machinery ready to expand to the whole data plane in 1b).

Run: python3 -m pytest tests/test_saas_subject_cursor_poison.py -q   (tests/ is gitignored → git add -f)
"""

import os
import re
import uuid

import pytest

from saas import subject_cursor as sc
from saas.subject_cursor import (
    BoundContext,
    SubjectCursor,
    UnboundSubjectError,
    current_bound_subject,
    reset_bound_subject,
    set_bound_subject,
    subject_cursor,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ─────────────────────────────────────────────────────────────────────────────────────
# Test fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────────────
class _FakeCursor:
    """Records the (sql, params) of the last execute — lets us prove the builder injects the
    subject predicate WITHOUT a live database."""

    def __init__(self):
        self.executed: list[tuple[str, object]] = []
        self._rows: list = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    @property
    def rowcount(self):
        return len(self._rows)


@pytest.fixture(autouse=True)
def _clean_binding():
    """Guarantee every test starts + ends with NO bound subject (no cross-test bleed)."""
    tok = None
    # Force-clear any binding leaked by a prior test.
    try:
        sc._bound_subject.set(None)
    except Exception:
        pass
    yield
    try:
        sc._bound_subject.set(None)
    except Exception:
        pass
    if tok is not None:
        reset_bound_subject(tok)


# ─────────────────────────────────────────────────────────────────────────────────────
# Bind-context unit coverage (source of truth) — GREEN now, no DB
# ─────────────────────────────────────────────────────────────────────────────────────
def test_unbound_by_default():
    assert current_bound_subject() is None


def test_set_reset_roundtrip():
    acct = str(uuid.uuid4())
    subj = str(uuid.uuid4())
    tok = set_bound_subject(acct, "faultline_acct_deadbeef", subj)
    ctx = current_bound_subject()
    assert isinstance(ctx, BoundContext)
    assert ctx.account_id == acct
    assert ctx.account_schema == "faultline_acct_deadbeef"
    assert ctx.subject_id == subj
    reset_bound_subject(tok)
    assert current_bound_subject() is None


def test_bind_rejects_unsafe_schema():
    # Fail-loud at bind time: a bad schema can never reach SET search_path.
    for bad in ("public", "faultline; DROP", "flagent_x", "", "faultline_acct_$"):
        with pytest.raises(ValueError):
            set_bound_subject(str(uuid.uuid4()), bad, str(uuid.uuid4()))


def test_bind_accepts_legacy_and_account_schemas():
    for good in ("faultline_acct_abc123", "faultline_550e8400_e29b_41d4_a716_446655440000"):
        tok = set_bound_subject(str(uuid.uuid4()), good, str(uuid.uuid4()))
        assert current_bound_subject().account_schema == good
        reset_bound_subject(tok)


def test_bind_rejects_empty_ids():
    with pytest.raises(ValueError):
        set_bound_subject("", "faultline_acct_x", str(uuid.uuid4()))
    with pytest.raises(ValueError):
        set_bound_subject(str(uuid.uuid4()), "faultline_acct_x", "")


# ─────────────────────────────────────────────────────────────────────────────────────
# POISON TEST #1 — UNBOUND THROWS (design §2.2 #1). MUST pass now (no DB needed).
# ─────────────────────────────────────────────────────────────────────────────────────
def test_poison_1_unbound_cursor_throws():
    """With no bound subject, opening a data-plane cursor is a HARD ERROR — not 'return
    everything', not 'default account'. The throw precedes any connection attempt."""
    assert current_bound_subject() is None
    with pytest.raises(UnboundSubjectError):
        with subject_cursor():
            pytest.fail("subject_cursor() must throw before yielding when unbound")


def test_poison_1_unbound_throws_before_touching_db(monkeypatch):
    """Prove the trip happens BEFORE any connection: sabotage db.connection to explode if
    called. UnboundSubjectError must still be what propagates (the guard short-circuits first)."""
    def _boom(*a, **k):
        raise AssertionError("db.connection() must NOT be reached when unbound")

    monkeypatch.setattr(sc.db, "connection", _boom)
    with pytest.raises(UnboundSubjectError):
        with subject_cursor():
            pass


# ─────────────────────────────────────────────────────────────────────────────────────
# Builder always-scoped injection — GREEN now via a fake cursor (no DB).
# Proves the SubjectCursor surface can NEVER emit a per-subject read without the filter.
# ─────────────────────────────────────────────────────────────────────────────────────
def _mk_wrapper(subject_id: str):
    fake = _FakeCursor()
    ctx = BoundContext(account_id=str(uuid.uuid4()),
                       account_schema="faultline_acct_test",
                       subject_id=subject_id)
    return SubjectCursor(fake, ctx), fake


def test_builder_injects_subject_predicate():
    subj = str(uuid.uuid4())
    wrapper, fake = _mk_wrapper(subj)
    wrapper.select("facts", ["subject_id", "rel_type"])
    sql, params = fake.executed[-1]
    assert "WHERE user_id = %(subject)s" in sql
    assert params["subject"] == subj


def test_builder_convenience_methods_scope():
    subj = str(uuid.uuid4())
    for meth in ("facts", "aliases", "attributes"):
        wrapper, fake = _mk_wrapper(subj)
        getattr(wrapper, meth)()
        sql, params = fake.executed[-1]
        assert "WHERE user_id = %(subject)s" in sql
        assert params["subject"] == subj


def test_builder_caller_cannot_override_subject():
    subj = str(uuid.uuid4())
    wrapper, fake = _mk_wrapper(subj)
    # A caller trying to widen scope by passing their own 'subject' param must LOSE.
    wrapper.select("facts", "*", extra_predicates="rel_type = %(rt)s",
                   params={"rt": "owns", "subject": "ATTACKER"})
    sql, params = fake.executed[-1]
    assert params["subject"] == subj  # bound context wins
    assert "AND (rel_type = %(rt)s)" in sql


def test_builder_rejects_unsafe_identifiers():
    wrapper, _ = _mk_wrapper(str(uuid.uuid4()))
    with pytest.raises(ValueError):
        wrapper.select("facts; DROP TABLE facts")
    with pytest.raises(ValueError):
        wrapper.select("facts", ["ok", "bad col"])


def test_escape_hatch_is_raw_and_named():
    """The escape hatch runs raw SQL verbatim (for account-metadata tables) — proving it is a
    distinct, deliberately-named path, not the scoped builder."""
    wrapper, fake = _mk_wrapper(str(uuid.uuid4()))
    wrapper.execute_account_scoped("SELECT rel_type FROM rel_types", None)
    sql, params = fake.executed[-1]
    assert sql == "SELECT rel_type FROM rel_types"
    assert "user_id" not in sql  # account-scoped, NOT subject-scoped — by design


# ─────────────────────────────────────────────────────────────────────────────────────
# POISON TEST #4 — GREP-AUDIT LINT (design §2.2 #4). GREEN now.
#
# Structural lint: no raw `.execute(` may exist on a data-plane connection outside the
# factory module / the named escape hatch. The sweep (1b) has NOT happened, so the data-plane
# module set is intentionally EMPTY here — the machinery runs and is asserted clean, ready to
# expand to main.py / server.py / embedder.py once those route through the factory.
# ─────────────────────────────────────────────────────────────────────────────────────
_EXECUTE_RE = re.compile(r"\.execute(?:many)?\s*\(")

# The factory module IS the sanctioned home for raw `.execute(` (it wraps every access).
FACTORY_MODULE = os.path.join(_REPO_ROOT, "saas", "subject_cursor.py")

# Modules whose data-plane SQL MUST route through the factory. EMPTY in Phase 1a (nothing swept
# yet). Phase 1b MUST add: src/api/main.py, src/mcp/server.py, src/re_embedder/embedder.py, …
# as each is migrated onto subject_cursor(); the lint then guards them from regressing.
DATA_PLANE_MODULES: list[str] = []


def find_raw_execute_calls(files: list[str]) -> list[tuple[str, int, str]]:
    """Lint machinery: return every `.execute(`/`.executemany(` occurrence in ``files`` as
    (path, lineno, stripped_line). Ready to expand to the whole data plane in 1b."""
    hits: list[tuple[str, int, str]] = []
    for path in files:
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            for i, line in enumerate(fh, start=1):
                if _EXECUTE_RE.search(line):
                    hits.append((path, i, line.strip()))
    return hits


def test_poison_4_grep_audit_data_plane_clean():
    """No raw `.execute(` on data-plane modules outside the factory. Green now (empty set);
    the gate that turns RED if a 1b-swept module keeps a raw execute after migration."""
    violations = find_raw_execute_calls(DATA_PLANE_MODULES)
    assert violations == [], (
        "Raw .execute( found on data-plane modules outside the factory — route through "
        f"subject_cursor(): {violations}"
    )


def test_poison_4_factory_surface_is_grep_auditable():
    """Assert-clean over the factory module ITSELF: the only PUBLIC method that runs raw SQL is
    the single named escape hatch; there is no bare `def execute(` on the wrapper. This is what
    lets the lint allowlist exactly one raw path when the sweep expands."""
    with open(FACTORY_MODULE, "r", encoding="utf-8") as fh:
        src = fh.read()
    # The named escape hatch is defined exactly once.
    assert src.count(f"def {sc.ESCAPE_HATCH_METHOD}(") == 1
    # No generic public .execute() method on the wrapper (that would defeat the guard).
    assert re.search(r"\n    def execute\s*\(", src) is None


# ─────────────────────────────────────────────────────────────────────────────────────
# POISON TESTS #2 and #3 — SKELETONS, gated on the Phase 1b sweep. Do NOT fake green.
# ─────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.skip(reason="gated on Phase 1b sweep — wire factory into read paths + bind_tenant + live DB")
def test_poison_2_cross_subject_is_empty_not_leaked():
    """§2.2 #2. Bind subject A (an account schema), insert a fact via the factory. Bind subject B
    in the SAME account schema, run EVERY read path (recall_memory_tool, fetch_facts_from_anchor,
    alias lookup, attribute fetch, the Qdrant lane) → assert ZERO rows attributable to A.

    Turns GREEN when 1b: (a) provisions a real per-account schema with the subject `user_id`
    column, (b) routes the read paths through subject_cursor(), and (c) runs against a live DB.
    Red-team variant: with RLS forced on and the app-layer filter deliberately removed, STILL
    zero rows (proves the (C) DB backstop is real)."""
    acct_schema = "faultline_acct_poisontest"
    subj_a, subj_b = str(uuid.uuid4()), str(uuid.uuid4())
    acct = str(uuid.uuid4())

    tok = set_bound_subject(acct, acct_schema, subj_a)
    try:
        with subject_cursor() as cur:
            # 1b: cur.insert_fact(...)  — write a fact owned by subject A
            ...
    finally:
        reset_bound_subject(tok)

    tok = set_bound_subject(acct, acct_schema, subj_b)
    try:
        with subject_cursor() as cur:
            rows = cur.facts().fetchall()
            assert rows == [], "subject B must NOT see subject A's rows (within-account leak)"
    finally:
        reset_bound_subject(tok)


@pytest.mark.skip(reason="gated on Phase 1b sweep — wire factory into bind_tenant + live DB")
def test_poison_3_cross_account_denied_at_the_door():
    """§2.2 #3. Bind account X's key, claim a subject that belongs to account Y → 403 at
    bind_tenant (the existing BOLA firewall, account_users UNIQUE(user_id)). The subject binding
    for a cross-account subject must never be established at all.

    Turns GREEN when 1b wires set_bound_subject into bind_tenant so the account authorization
    (server-derived principal) precedes the subject bind, against a live control plane."""
    # 1b: create accounts X and Y + a seat under Y; assert bind_tenant(X_key, Y_subject) → 403,
    # and that current_bound_subject() is None afterward (no partial bind).
    ...
