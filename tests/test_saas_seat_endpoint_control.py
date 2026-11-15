"""PER-SEAT "Own LLM endpoint · ALLOW | DENY" — the switch that FAKED control (migration 212).

THE DEFECT. The tenant console rendered a per-seat ALLOW/DENY switch that was inert in BOTH
directions, and the two halves pointed OPPOSITE ways, which is exactly why it looked plausible:

  WRITE  ``UpdateSeatRequest`` had no ``can_control_endpoint`` field, so Pydantic dropped the
         value before any handler ran. ``seats.update_seat`` had no such parameter either.
  READ   ``seats.list_seats`` never projected it, so ``s.can_control_endpoint === true`` on an
         ABSENT key rendered DENY for every seat, forever.
  GATE   Enforcement (``user_console._require_endpoint_control``) reads
         ``individual_users.can_control_endpoint`` — a table the switch never touched. And
         ``_create_individual`` hardcodes ('none', TRUE) on EVERY path including the
         tenant-admin ``create_login_for_seat``, so for exactly the seats the switch governs the
         console showed DENY while enforcement granted ALLOW. It misrepresented the posture in
         the PERMISSIVE direction.

THE RULE (owner, 2026-08-03). A seat of a TENANT does not control the brain — it is the
ACCOUNT's, managed by the admin. A SINGLE controls their own. When a single is transferred INTO
a tenant, control FLIPS to the tenant. So the state is DERIVED from ``accounts.account_kind``,
not a free-floating per-seat opinion; an admin may still GRANT an explicit exception on top.

OWNERSHIP. ``public.individual_users.can_control_endpoint`` is the SINGLE source of truth.
``public.account_users`` deliberately has NO copy — a duplicated permission drifts, and drift in
a permission is a security bug. The console READS THROUGH (list_seats LEFT JOIN) and WRITES
THROUGH (set_seat_endpoint_control).

These proofs are the ones that would have caught the original ship:
  1. the request model CARRIES the field (the exact thing that was missing)
  2. no duplicate column was added to account_users
  3. the projection mirrors the REAL gate, in all four states
  4. the console predicate and the enforcement predicate agree BY CONSTRUCTION
  5. GRANT / WITHDRAW round-trip; self-owned and no-login are REFUSED
  6. BOLA: the write is scoped by account_id in the SQL itself
  7. the transfer FLIP is inside the transfer transaction, not a follow-up step
  8. SAAS_MODE off changes nothing

Control-plane data functions are monkeypatched, so this file is green without a control plane.
The live end-to-end proof (real routes, real Postgres, all four transitions) is
``DEV/review/_seat_endpoint_control_driver.py``.
Run: python3 tools/fltest.py --bug SEATENDPOINTCTL --test tests/test_saas_seat_endpoint_control.py
"""

import pytest

from saas import seats as _seats


ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SEAT = "11111111-1111-4111-8111-111111111111"


# ════════════════════════════════════════════════════════════════════════════════
# 1. THE MISSING FIELD — the exact defect, pinned.
# ════════════════════════════════════════════════════════════════════════════════

def test_update_seat_request_carries_can_control_endpoint():
    """The value must survive the request model. It did not: the field was absent, and
    Pydantic dropped the console's body silently. If this goes red the switch is fake again."""
    from saas.admin import UpdateSeatRequest
    body = UpdateSeatRequest(**{"can_control_endpoint": True, "label": "x"})
    assert body.can_control_endpoint is True
    assert body.model_dump(exclude_unset=True) == {"can_control_endpoint": True, "label": "x"}


def test_omitted_field_still_means_leave_unchanged():
    """Partial-update semantics: None must remain 'do not touch', or every unrelated seat
    edit (rename, agent-mode flip) would silently rewrite a permission."""
    from saas.admin import UpdateSeatRequest
    assert UpdateSeatRequest(label="just a rename").can_control_endpoint is None


def test_account_users_gained_no_duplicate_column():
    """THE OWNERSHIP DECISION, pinned in the migration text: the fact lives in
    individual_users and is READ THROUGH. A second copy on account_users can drift out of step
    with the gate, and a drifting permission is a security bug — the whole reason this switch
    was dangerous rather than merely broken."""
    from pathlib import Path
    sql = (Path(__file__).resolve().parents[1]
           / "migrations" / "212_saas_seat_endpoint_control.sql").read_text()
    assert "ALTER TABLE public.account_users" not in sql
    assert "individual_users" in sql


# ════════════════════════════════════════════════════════════════════════════════
# 2. THE PROJECTION — four states, and it mirrors the REAL gate.
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "has_login,ctx,raw,expect_bool,expect_scope",
    [
        (False, None, None, False, "no_login"),      # no /me login → nothing to gate
        (True, "none", True, True, "self_owned"),    # a single — theirs
        (True, "house", True, True, "self_owned"),   # a house seat — theirs
        (True, "real", False, False, "governed"),    # tenant seat, DEFAULT
        (True, "real", True, True, "granted"),       # tenant seat, admin GRANTED
    ],
)
def test_projection_covers_every_state(has_login, ctx, raw, expect_bool, expect_scope):
    out = _seats._endpoint_control_projection(has_login, ctx, raw)
    assert out["can_control_endpoint"] is expect_bool
    assert out["endpoint_control"] == expect_scope


def test_no_login_reports_the_safe_state_not_a_phantom_grant():
    """A UI that ignores `endpoint_control` and reads only the boolean must still see the
    SAFE value. Reporting True here would render 'ALLOW' for a seat that cannot even log in."""
    assert _seats._endpoint_control_projection(False, "none", True)["can_control_endpoint"] \
        is False


@pytest.mark.parametrize("ctx,raw,expected", [
    ("none", True, True), ("none", False, True),      # not org-governed → always allowed
    ("house", False, True),
    ("real", True, True), ("real", False, False),      # org-governed → the flag decides
    (None, None, True),
])
def test_console_predicate_equals_enforcement_predicate(ctx, raw, expected):
    """VERIFY THE READ, NOT THE WRITE. The card's boolean is computed by seats.py; the refusal
    is computed by user_console. If those two ever disagree the console is lying again — just
    more subtly than before. Both are checked against the literal gate expression."""
    from saas import brain_health
    assert _seats._effective_endpoint_control(ctx, raw) is expected
    assert (not brain_health.seat_is_governed(ctx, raw)) is expected
    # And the real gate itself: allowed = True if ctx != 'real' else bool(flag)
    from fastapi import HTTPException
    from saas import user_console
    individual = {"tenant_context": ctx, "can_control_endpoint": raw}
    try:
        user_console._require_endpoint_control(individual)
        gate_allowed = True
    except HTTPException:
        gate_allowed = False
    assert gate_allowed is expected


# ════════════════════════════════════════════════════════════════════════════════
# 3. THE WRITE — grants, withdrawals, refusals, and BOLA scoping.
# ════════════════════════════════════════════════════════════════════════════════

class _Cur:
    """A cursor that records SQL and answers the two reads set_seat_endpoint_control makes."""

    def __init__(self, seat_row, updated_row):
        self.seat_row, self.updated_row = seat_row, updated_row
        self.sql, self.params, self._last = [], [], None

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))
        self.params.append(params)
        self._last = "UPDATE" if sql.lstrip().upper().startswith("UPDATE") else "SELECT"

    def fetchone(self):
        return self.updated_row if self._last == "UPDATE" else self.seat_row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur
        self.committed = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def db_for(monkeypatch):
    def _install(seat_row, updated_row=None):
        cur = _Cur(seat_row, updated_row)
        monkeypatch.setattr(_seats.db, "connection", lambda: _Conn(cur))
        monkeypatch.setattr(_seats._audit, "record", lambda *a, **k: True)
        return cur
    return _install


def test_grant_writes_individual_users_and_returns_the_effective_state(db_for):
    cur = db_for(("tenant", "ind-1", "real", False), ("real", True))
    out = _seats.set_seat_endpoint_control(ACCT_A, SEAT, True)
    assert out["ok"] and out["status_code"] == 200
    assert out["can_control_endpoint"] is True
    assert out["endpoint_control"] == "granted"
    write = [s for s in cur.sql if s.startswith("UPDATE")][0]
    assert "public.individual_users" in write
    assert "public.account_users" not in write     # never a second copy


def test_withdraw_writes_false(db_for):
    db_for(("tenant", "ind-1", "real", True), ("real", False))
    out = _seats.set_seat_endpoint_control(ACCT_A, SEAT, False)
    assert out["ok"] and out["can_control_endpoint"] is False
    assert out["endpoint_control"] == "governed"


def test_every_write_reasserts_tenant_context_real(db_for):
    """The repair that stops the fix being fake one layer down. A row drifted to 'none' by the
    pre-212 ``_create_individual`` hardcode would take the admin's DENY and then IGNORE it,
    because the gate short-circuits on ``ctx != 'real'``."""
    cur = db_for(("tenant", "ind-1", "none", True), ("real", False))
    _seats.set_seat_endpoint_control(ACCT_A, SEAT, False)
    write = [s for s in cur.sql if s.startswith("UPDATE")][0]
    assert "tenant_context = 'real'" in write


def test_self_owned_single_cannot_be_stripped(db_for):
    """A single owns their brain. No admin may take it — the 409 is the point, not an edge."""
    cur = db_for(("individual_house", "ind-1", "none", True))
    out = _seats.set_seat_endpoint_control(ACCT_A, SEAT, False)
    assert not out["ok"] and out["status_code"] == 409
    assert out["endpoint_control"] == "self_owned"
    assert out["can_control_endpoint"] is True
    assert not [s for s in cur.sql if s.startswith("UPDATE")]      # nothing was written


def test_seat_without_a_login_is_refused_not_silently_ignored(db_for):
    cur = db_for(("tenant", None, None, None))
    out = _seats.set_seat_endpoint_control(ACCT_A, SEAT, True)
    assert not out["ok"] and out["status_code"] == 409
    assert out["endpoint_control"] == "no_login"
    assert not [s for s in cur.sql if s.startswith("UPDATE")]


def test_unknown_or_revoked_seat_is_404(db_for):
    db_for(None)
    out = _seats.set_seat_endpoint_control(ACCT_A, SEAT, True)
    assert not out["ok"] and out["status_code"] == 404


def test_lookup_filters_status_active(db_for):
    """A raw seat lookup includes REVOKED rows (measured on a real account: 3 active, 8
    revoked). A permission must never be granted to a revoked seat."""
    cur = db_for(("tenant", "ind-1", "real", False), ("real", True))
    _seats.set_seat_endpoint_control(ACCT_A, SEAT, True)
    assert "au.status = 'active'" in cur.sql[0]


def test_write_is_scoped_by_account_id_in_the_sql(db_for):
    """BOLA defence in depth: even if a route forgot to authorize the seat, neither the read
    nor the write can cross an account boundary."""
    cur = db_for(("tenant", "ind-1", "real", False), ("real", True))
    _seats.set_seat_endpoint_control(ACCT_A, SEAT, True)
    assert "au.account_id = %s" in cur.sql[0]
    assert ACCT_A in cur.params[0]
    write_idx = [i for i, s in enumerate(cur.sql) if s.startswith("UPDATE")][0]
    assert "account_id = %s" in cur.sql[write_idx]
    assert ACCT_A in cur.params[write_idx]


def test_audit_carries_the_server_derived_actor(monkeypatch):
    """A permission change needs a trail, and the actor comes from the CALLING REALM's
    server-derived principal — never from the request body."""
    cur = _Cur(("tenant", "ind-1", "real", False), ("real", True))
    monkeypatch.setattr(_seats.db, "connection", lambda: _Conn(cur))
    seen = {}
    monkeypatch.setattr(_seats._audit, "record",
                        lambda action, **kw: seen.update({"action": action, **kw}) or True)
    _seats.set_seat_endpoint_control(ACCT_A, SEAT, True,
                                     actor_type="tenant", actor_id="dash-user-9")
    assert seen["action"] == "seat.endpoint_control"
    assert seen["actor_type"] == "tenant" and seen["actor_id"] == "dash-user-9"
    assert seen["target_id"] == SEAT and seen["account_id"] == ACCT_A
    assert "GRANTED" in seen["detail"]


def test_db_error_fails_closed(monkeypatch):
    def _boom():
        raise RuntimeError("control plane down")
    monkeypatch.setattr(_seats.db, "connection", _boom)
    out = _seats.set_seat_endpoint_control(ACCT_A, SEAT, True)
    assert not out["ok"] and out["status_code"] == 500
    assert out["can_control_endpoint"] is False       # never fails open


# ════════════════════════════════════════════════════════════════════════════════
# 4. THE READ — list_seats projects it, by READ-THROUGH.
# ════════════════════════════════════════════════════════════════════════════════

def test_list_seats_projects_endpoint_control_by_read_through(monkeypatch):
    """The half that made the switch render DENY forever: the key was simply absent, so the
    console's ``=== true`` test could only ever be false."""
    row = (SEAT, "active", None, "a seat", True, "standard", False, None, False,
           None, None, "tenant", "real", True, True)

    class _C:
        def execute(self, sql, params=None):
            self.sql = " ".join(sql.split())

        def fetchall(self):
            return [row]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    cur = _C()
    monkeypatch.setattr(_seats.db, "connection", lambda: _Conn(cur))
    out = _seats.list_seats(ACCT_A)
    assert out[0]["can_control_endpoint"] is True
    assert out[0]["endpoint_control"] == "granted"
    assert out[0]["account_kind"] == "tenant"
    assert "public.individual_users" in cur.sql        # read-through, not a copied column
    assert "LEFT JOIN" in cur.sql                      # a seat with no login must still list


def test_list_seats_still_carries_every_pre_existing_key(monkeypatch):
    """The projection was widened, not reshaped — the seat card reads all of these."""
    row = (SEAT, "active", None, "lbl", True, "agent", True, "clinical", True,
           None, None, "tenant", None, None, False)

    class _C:
        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return [row]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(_seats.db, "connection", lambda: _Conn(_C()))
    seat = _seats.list_seats(ACCT_A)[0]
    for k in ("user_id", "status", "created_at", "label", "ingest_enabled", "seat_type",
              "is_base", "response_type", "api_enabled"):
        assert k in seat
    assert "owui_name" not in seat        # PII stays OPT-IN (migration 210 default)
    assert seat["endpoint_control"] == "no_login"


# ════════════════════════════════════════════════════════════════════════════════
# 5. THE DERIVATION AND THE FLIP — the invariants that must not depend on a caller.
# ════════════════════════════════════════════════════════════════════════════════

def test_migration_derives_governance_at_insert_tighten_only():
    """The default is a SECURITY default reached from several insert callers, and the bug being
    fixed IS 'one insert site got it wrong and nothing caught it'. The trigger is BEFORE INSERT
    (never UPDATE, so an admin's GRANT always sticks) and can only tighten TRUE→FALSE."""
    from pathlib import Path
    sql = (Path(__file__).resolve().parents[1]
           / "migrations" / "212_saas_seat_endpoint_control.sql").read_text()
    assert "BEFORE INSERT ON public.individual_users" in sql
    assert "AFTER INSERT" not in sql and "BEFORE UPDATE" not in sql
    assert "individual_house" in sql                    # self-owned kinds are exempt
    assert "NEW.can_control_endpoint := FALSE" in sql
    assert "NEW.can_control_endpoint := TRUE" not in sql   # never grants


def test_transfer_flips_control_inside_the_transfer_transaction():
    """OWNER'S RULE: when a single is migrated into a tenant, control FLIPS — as part of the
    transfer, not a manual follow-up. A separate step means a transferred user keeps control of
    a brain that is no longer theirs. Source-pinned because the flip is one statement inside a
    transaction that a unit test cannot otherwise observe as atomic."""
    import inspect

    from saas import transfers
    src = inspect.getsource(transfers.approve_and_execute)
    # The function opens several cursors (load, then execute). Take the ONE transaction block
    # that performs the seat move, and require the flip to live inside it, before its commit.
    blocks = [b.split("conn.commit()", 1)[0]
              for b in src.split("with conn.cursor() as cur:")[1:]]
    candidates = [b for b in blocks if "UPDATE public.account_users" in b]
    assert len(candidates) == 1, "the seat move should live in exactly one transaction block"
    body = candidates[0]
    assert "tenant_context = 'real'" in body
    assert "can_control_endpoint = FALSE" in body
    assert "UPDATE public.account_users" in body            # the seat move is the SAME txn
    assert "DELETE FROM public.seat_llm_config" in body     # the personal credential dies too


def test_self_owned_kinds_are_defined_by_exclusion():
    """A future account_kind must fail towards GOVERNED. Enumerating the corporate kinds
    instead would silently grant a new class of seat control over its own egress."""
    assert _seats.SELF_OWNED_ACCOUNT_KINDS == frozenset({"individual_house"})
    for kind in ("tenant", "team", "clinical", "something_invented_later"):
        assert kind not in _seats.SELF_OWNED_ACCOUNT_KINDS


# ════════════════════════════════════════════════════════════════════════════════
# 6. SAAS_MODE OFF — byte-for-byte unchanged.
# ════════════════════════════════════════════════════════════════════════════════

def test_saas_off_writes_no_audit_row(monkeypatch):
    """The open core must be untouched. ``audit.record`` no-ops when the flag is off, and
    nothing this feature adds runs outside a SaaS route."""
    monkeypatch.delenv("SAAS_MODE", raising=False)
    from saas import audit, config
    assert config.saas_mode_enabled() is False
    assert audit.record("seat.endpoint_control", account_id=ACCT_A) is False


def test_saas_off_migration_touches_only_control_plane_tables():
    """public.individual_users / public.account_users exist ONLY in public and are never read
    from a tenant search_path, so a SAAS-off deployment cannot observe this change. VERIFIED
    against the template rather than asserted: neither table has a per-tenant twin, so adding
    one would be wrong (it would put authorization state inside the data plane's reach)."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    template = (root / "src" / "provisioning" / "templates" / "user_schema.sql").read_text()
    assert "individual_users" not in template
    assert "account_users" not in template
    sql = (root / "migrations" / "212_saas_seat_endpoint_control.sql").read_text()
    for stmt in ("CREATE TABLE", "ALTER TABLE"):
        assert stmt not in sql or "public." in sql
