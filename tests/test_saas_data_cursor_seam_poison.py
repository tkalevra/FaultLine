"""POISON TEST — the acceptance gate for the search_path pooling fix (per-account grain 1b.0).

Proves the STRUCTURAL properties the fix must hold, against a **faithful fake of PgBouncer
transaction pooling** (a live pooled DB is unavailable in the local harness; the fake models the
exact GUC lifecycle that makes the leak real). Ref: ``DEV/DESIGN-per-account-grain-1b-execution.md``
§2 and the ``src/api/data_cursor.py`` seam.

The three gate properties (brief GATE a/b/c):
  (a) an UNBOUND / lost binding does NOT silently fall through to ``public`` — it THROWS.
  (b) tenant B CANNOT see tenant A's schema across a REUSED/pooled connection (with ``SET LOCAL``).
  (c) a binding does NOT outlive its transaction (``SET LOCAL`` dies at COMMIT).

Plus faithfulness checks: the fake MUST reproduce the *bug* under session-level ``SET`` (else it
would prove nothing), and the SAAS-off seam MUST be byte-for-byte today's non-LOCAL ``SET`` with
ZERO ``saas`` import.

Run: python3 tools/fltest.py --bug GRAIN-1B --test tests/test_saas_data_cursor_seam_poison.py
"""

import builtins
import os
import uuid

import pytest

from src.api import data_cursor as dc
from src.api.data_cursor import (
    SearchPathError,
    bind_search_path,
    commit_and_rebind,
    data_plane_cursor,
    verify_bound,
)

_DEFAULT_PATH = '"$user", public'
_SCHEMA_A = "faultline_" + uuid.uuid4().hex[:12]
_SCHEMA_B = "faultline_" + uuid.uuid4().hex[:12]


# ─────────────────────────────────────────────────────────────────────────────────────
# Faithful fake of a PgBouncer transaction-pooled server connection.
#
#   • ``SET search_path TO X``        -> session GUC: SURVIVES commit AND survives handoff to
#                                        the next pooled client (THE LEAK).
#   • ``SET LOCAL search_path TO X``  -> txn GUC: cleared at COMMIT/ROLLBACK (dies with the txn),
#                                        so it can NEVER be handed to the next client (THE FIX).
#   • ``SHOW search_path``            -> local if set else session else the server default.
# ─────────────────────────────────────────────────────────────────────────────────────
class _FakeServer:
    """The physical server connection PgBouncer multiplexes between clients."""

    def __init__(self):
        self.session_path = _DEFAULT_PATH  # survives commit + handoff
        self.local_path = None             # dies at commit/rollback
        self.statements: list[str] = []

    def _end_txn(self):
        # SET LOCAL is transaction-scoped: cleared at COMMIT and ROLLBACK alike.
        self.local_path = None

    def show(self) -> str:
        return self.local_path if self.local_path is not None else self.session_path


class _FakeCursor:
    def __init__(self, server: "_FakeServer"):
        self._s = server
        self._last_show = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._s.statements.append(sql)
        u = " ".join(sql.strip().split())
        up = u.upper()
        if up.startswith("SET LOCAL SEARCH_PATH TO "):
            self._s.local_path = u[len("SET LOCAL SEARCH_PATH TO "):]
        elif up.startswith("SET SEARCH_PATH TO "):
            self._s.session_path = u[len("SET SEARCH_PATH TO "):]
        elif up.startswith("SHOW SEARCH_PATH"):
            self._last_show = self._s.show()
        elif up.startswith("SELECT SET_CONFIG"):
            # subject GUC (is_local=true) — irrelevant to search_path fidelity; accept it.
            self._last_show = None
        else:
            self._last_show = None

    def fetchone(self):
        return (self._last_show,) if self._last_show is not None else None

    def fetchall(self):
        return []

    @property
    def rowcount(self):
        return 0


class _FakeConn:
    """A client-facing connection handle bound to a (possibly reused) server."""

    def __init__(self, server: "_FakeServer"):
        self._s = server
        self.autocommit = False
        self.closed = False

    def cursor(self):
        return _FakeCursor(self._s)

    def commit(self):
        self._s._end_txn()  # txn ends -> SET LOCAL cleared, server returns to pool

    def rollback(self):
        self._s._end_txn()

    def close(self):
        self.closed = True


# ─────────────────────────────────────────────────────────────────────────────────────
# (c) SET LOCAL dies at COMMIT; (a) verify_bound THROWS on the lost binding (no silent public).
# ─────────────────────────────────────────────────────────────────────────────────────
def test_set_local_binding_dies_at_commit_and_verify_throws():
    srv = _FakeServer()
    conn = _FakeConn(srv)
    cur = conn.cursor()
    bind_search_path(cur, _SCHEMA_A, local=True)
    verify_bound(cur, _SCHEMA_A)  # bound now — passes

    conn.commit()  # SET LOCAL dies here

    # (a) the lost binding must be LOUD, not a silent fall-through to public.
    assert srv.show() == _DEFAULT_PATH  # fell back to "$user", public
    with pytest.raises(SearchPathError):
        verify_bound(cur, _SCHEMA_A)


def test_session_set_survives_commit_proves_fake_is_faithful():
    """Faithfulness: with a session-level SET (today's code) the binding SURVIVES commit — the
    fake must reproduce the real behavior, else the fix-proof below is vacuous."""
    srv = _FakeServer()
    conn = _FakeConn(srv)
    cur = conn.cursor()
    bind_search_path(cur, _SCHEMA_A, local=False)  # today's non-LOCAL path
    conn.commit()
    verify_bound(cur, _SCHEMA_A)  # still bound — no throw (this is exactly the leak vector)


# ─────────────────────────────────────────────────────────────────────────────────────
# (b) cross-tenant leak across a REUSED pooled server connection.
# ─────────────────────────────────────────────────────────────────────────────────────
def test_session_set_LEAKS_across_pooled_handoff():
    """THE BUG: session SET survives the handoff — tenant B inherits A's search_path."""
    srv = _FakeServer()  # ONE physical server connection, multiplexed

    conn_a = _FakeConn(srv)
    bind_search_path(conn_a.cursor(), _SCHEMA_A, local=False)
    conn_a.commit()  # A's txn ends; server returns to pool STILL carrying A's session path

    conn_b = _FakeConn(srv)  # B checks out the same server connection
    with pytest.raises(SearchPathError):
        # B expects its OWN schema; it must NOT already be bound to A's. The leak means SHOW==A.
        verify_bound(conn_b.cursor(), _SCHEMA_B)
    assert srv.show() == _SCHEMA_A  # concrete proof of the cross-tenant inheritance


def test_set_local_does_NOT_leak_across_pooled_handoff():
    """THE FIX: SET LOCAL is gone by the time the server returns to the pool — B sees no A."""
    srv = _FakeServer()

    conn_a = _FakeConn(srv)
    bind_search_path(conn_a.cursor(), _SCHEMA_A, local=True)
    conn_a.commit()  # A's SET LOCAL dies with the txn

    conn_b = _FakeConn(srv)
    assert _SCHEMA_A not in srv.show()          # A's schema did NOT survive to B
    assert srv.show() == _DEFAULT_PATH          # server is back to a clean default
    # And B binding its own schema is a normal, isolated bind.
    bind_search_path(conn_b.cursor(), _SCHEMA_B, local=True)
    verify_bound(conn_b.cursor(), _SCHEMA_B)


# ─────────────────────────────────────────────────────────────────────────────────────
# The mid-request-commit LANDMINE handler: commit_and_rebind keeps the binding alive.
# ─────────────────────────────────────────────────────────────────────────────────────
def test_commit_and_rebind_survives_mid_request_commit_in_local_mode():
    srv = _FakeServer()
    conn = _FakeConn(srv)
    cur = conn.cursor()
    bind_search_path(cur, _SCHEMA_A, local=True)
    # Hot paths (query / correct_fact / fetch_facts_from_anchor) commit then keep querying:
    commit_and_rebind(conn, cur, _SCHEMA_A, local=True)
    verify_bound(cur, _SCHEMA_A)  # still bound after the mid-request commit — no public fallthrough


def test_commit_and_rebind_is_plain_commit_in_session_mode():
    srv = _FakeServer()
    conn = _FakeConn(srv)
    cur = conn.cursor()
    bind_search_path(cur, _SCHEMA_A, local=False)
    commit_and_rebind(conn, cur, _SCHEMA_A, local=False)
    # session path already survives; only ONE SET was ever emitted (no redundant re-bind).
    assert sum(1 for s in srv.statements if s.upper().startswith("SET ")) == 1
    verify_bound(cur, _SCHEMA_A)


# ─────────────────────────────────────────────────────────────────────────────────────
# Fail-loud schema guard — a malformed schema can never reach SET search_path.
# ─────────────────────────────────────────────────────────────────────────────────────
def test_bind_rejects_unsafe_schema():
    srv = _FakeServer()
    cur = _FakeConn(srv).cursor()
    for bad in ("faultline; DROP", "public schema", "", "FaultLine_Up", "a-b"):
        with pytest.raises(SearchPathError):
            bind_search_path(cur, bad, local=True)
    # nothing unsafe was ever emitted
    assert all("DROP" not in s for s in srv.statements)


# ─────────────────────────────────────────────────────────────────────────────────────
# SAAS-OFF seam: byte-for-byte today's NON-LOCAL SET, and ZERO saas import.
# ─────────────────────────────────────────────────────────────────────────────────────
def test_saas_off_seam_emits_non_local_set_and_never_imports_saas(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)  # SAAS off
    srv = _FakeServer()
    conn = _FakeConn(srv)

    # Sabotage any import of the saas package — the SAAS-off path must never trip it.
    real_import = builtins.__import__

    def _guard(name, *a, **k):
        if name == "saas" or name.startswith("saas."):
            raise AssertionError(f"SAAS-off path must NOT import {name!r}")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _guard)

    with data_plane_cursor(_SCHEMA_A, conn=conn) as cur:
        assert cur is not None
    # exactly today's statement — non-LOCAL SET, no ", public" on a data bind.
    sets = [s for s in srv.statements if s.upper().startswith("SET ")]
    assert sets == [f"SET search_path TO {_SCHEMA_A}"]


def test_saas_off_seam_rejects_unsafe_schema(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    conn = _FakeConn(_FakeServer())
    with pytest.raises(SearchPathError):
        with data_plane_cursor("bad; schema", conn=conn):
            pass


# ─────────────────────────────────────────────────────────────────────────────────────
# SAAS-ON seam: unbound => FAIL LOUD (never a silent unscoped cursor); bound => SET LOCAL.
# ─────────────────────────────────────────────────────────────────────────────────────
def _saas_importable():
    try:
        import saas.subject_cursor  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _saas_importable(), reason="saas package not importable in this env")
def test_saas_on_unbound_data_site_fails_loud(monkeypatch):
    from saas.subject_cursor import UnboundSubjectError
    import saas.subject_cursor as scmod

    monkeypatch.setenv("SAAS_MODE", "1")
    scmod._bound_subject.set(None)  # explicitly unbound
    with pytest.raises(UnboundSubjectError):
        with data_plane_cursor(_SCHEMA_A):
            pytest.fail("data_plane_cursor must throw when SAAS on + unbound — never yield")


@pytest.mark.skipif(not _saas_importable(), reason="saas package not importable in this env")
def test_saas_on_bound_routes_through_subject_cursor_with_set_local(monkeypatch):
    import saas.subject_cursor as scmod
    from saas.subject_cursor import set_bound_subject, reset_bound_subject

    monkeypatch.setenv("SAAS_MODE", "1")

    srv = _FakeServer()

    # Route the factory's connection to our fake pooled server.
    class _CM:
        def __enter__(self_):
            return _FakeConn(srv)

        def __exit__(self_, *a):
            return False

    monkeypatch.setattr(scmod.db, "connection", lambda: _CM())

    tok = set_bound_subject(str(uuid.uuid4()), _SCHEMA_A, str(uuid.uuid4()))
    try:
        with data_plane_cursor("ignored-when-bound") as sc:
            # It is the structural SubjectCursor, not a raw cursor.
            assert sc.__class__.__name__ == "SubjectCursor"
    finally:
        reset_bound_subject(tok)

    # The factory bound via SET LOCAL (pool-safe), NOT a session SET.
    assert any(s.upper().startswith("SET LOCAL SEARCH_PATH TO") for s in srv.statements)
    assert not any(
        s.upper().startswith("SET SEARCH_PATH TO") and "LOCAL" not in s.upper()
        for s in srv.statements
    )
