"""A tenant admin's brain change must reach the seats it governs.

THE DEFECT. `seat_brain.resolve_brain` gives a GRANTED corporate seat the org's row only
"until it saves one" — after that the seat row wins permanently and the account lane is never
consulted again. So a tenant admin could rebind the organisation's brain in the console, see it
saved and tested, and every seat that had ever saved its own row kept running the OLD endpoint,
silently, with nothing in the console to show it or fix it.

Measured: the owner rebound a tenant to NVIDIA NIM; the bound seat still resolved the previous
provider and stayed parked behind that provider's rate limit.

THE GUARD MATTERS AS MUCH AS THE FIX. On a house/individual account the seats are unrelated
STRANGERS who each own their own brain and credential — the account lane is deliberately
suppressed there. Propagating on that account kind would destroy other people's stored keys, so
it is restricted to the shared-brain kinds, and an unknown/unreadable kind clears nothing.
"""
import pytest

from saas import llm_config
from saas.seat_brain import SHARED_BRAIN_KINDS


class _Cur:
    """A cursor that records DELETEs and reports a rowcount."""

    def __init__(self, rowcount=2):
        self.executed = []
        self.rowcount = rowcount

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))


def _run(monkeypatch, kind, rowcount=2, apply_to=llm_config.APPLY_ALL_SEATS):
    monkeypatch.setattr("saas.seat_brain.account_kind", lambda _a: kind)
    cur = _Cur(rowcount)
    n = llm_config._propagate_account_brain_to_seats(cur, "acct-1", apply_to)
    deletes = [e for e in cur.executed if e[0].upper().startswith("DELETE")]
    return n, deletes


@pytest.mark.parametrize("kind", sorted(SHARED_BRAIN_KINDS))
def test_a_shared_brain_account_clears_stale_seat_overrides(monkeypatch, kind):
    """tenant / team / clinical: one org owns the credential, so its seats must follow it."""
    n, deletes = _run(monkeypatch, kind)
    assert n == 2, f"{kind}: propagation reported {n} cleared"
    assert len(deletes) == 1, f"{kind}: expected exactly one DELETE, got {deletes}"
    sql, params = deletes[0]
    assert "public.seat_llm_config" in sql and "account_id = %s" in sql, sql
    assert params == ("acct-1",), params


def test_a_house_account_never_touches_its_seats_credentials(monkeypatch):
    """THE SAFETY GUARD. Those seats are strangers who each own their brain; the account lane is
    suppressed for them. Clearing here would delete other people's stored keys."""
    n, deletes = _run(monkeypatch, "individual_house")
    assert n == 0
    assert deletes == [], f"a house account deleted seat rows: {deletes}"


@pytest.mark.parametrize("kind", [None, "", "some_new_kind_added_later"])
def test_an_unknown_or_unreadable_kind_clears_nothing(monkeypatch, kind):
    """Fail direction is 'change nothing'. A control plane that cannot be read must not be able
    to trigger a destructive propagation."""
    n, deletes = _run(monkeypatch, kind)
    assert n == 0 and deletes == [], f"kind={kind!r} propagated anyway: {deletes}"


def test_the_default_leaves_a_granted_seats_own_brain_alone(monkeypatch):
    """THE DEFAULT IS CONSERVATIVE. A granted seat's endpoint is a deliberate act by somebody
    the admin chose to trust; deleting it — and its stored credential — on every unrelated org
    save would be a worse defect than the one this fixes. Governed seats need no propagation at
    all: they resolve the account row directly, so writing it IS their update."""
    n, deletes = _run(monkeypatch, "tenant", apply_to=llm_config.APPLY_GOVERNED_ONLY)
    assert n == 0 and deletes == [], f"the default cleared seat overrides: {deletes}"


def test_the_admin_is_told_how_many_seats_hold_their_own_brain():
    """So the console can ask 'apply to everyone?' rather than the backend guessing."""
    cur = _Cur(0)
    cur.fetchone = lambda: (3,)
    assert llm_config.count_seat_overrides(cur, "acct-1") == 3
    sql = " ".join(cur.executed[0][0].split()).upper()
    assert sql.startswith("SELECT COUNT(*)") and "SEAT_LLM_CONFIG" in sql, cur.executed


def test_propagation_failure_never_fails_the_orgs_own_save(monkeypatch):
    """A seat left on a stale brain is a bug; an admin who cannot save their endpoint at all is
    worse. The propagation swallows its own failure and the next save retries."""
    monkeypatch.setattr("saas.seat_brain.account_kind", lambda _a: "tenant")

    class _Boom:
        rowcount = 0

        def execute(self, *_a, **_k):
            raise RuntimeError("control plane gone")

    assert llm_config._propagate_account_brain_to_seats(_Boom(), "acct-1") == 0


def test_the_account_write_actually_calls_the_propagation():
    """Guard the wiring, not just the helper — an unwired fix is the failure mode this whole
    line of work keeps finding."""
    import inspect
    src = inspect.getsource(llm_config.upsert_config)
    assert "_propagate_account_brain_to_seats(" in src, (
        "upsert_config no longer propagates the org brain to its seats")
    assert "apply_to_seats" in src, "upsert_config no longer honours the admin's choice"
    assert "count_seat_overrides(" in src, (
        "upsert_config no longer reports how many seats hold their own brain, so the console "
        "cannot ask the admin whether to move them")
