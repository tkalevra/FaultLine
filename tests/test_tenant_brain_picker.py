"""Session-authed /tenant/llm-config/saved* — the tenant brain-picker (migration 306).

Spec: DEV/DESIGN-tenant-brain-picker.md @ 9d54da7b (the executor brief pins THIS revision:
save-config applies a selection; NO new buttons; dirty-selection voiding). Modeled on
tests/test_saas_tenant_control.py (TestClient + tc.router, session fixtures, ACCT_A/ACCT_B,
no DB). TWO proof layers:

  ROUTES  store functions monkeypatched with recording fakes -> session-scope, BOLA, CSRF,
          status-code/detail contracts, cap + active-guard + replacement wiring.
  STORE   the REAL saas.llm_config saved-brain functions driven over a scripted fake cursor
          (real code, scripted SQL answers) -> the DOUBLE-ENCRYPTION trap is pinned by
          asserting the executed SQL copies api_key_ciphertext column-to-column and that
          _encode_fields is never CALLED on the capture/apply path, that a failing
          replacement leaves the target NOT deleted (one transaction), and that is_active is
          computed by value-match, never stored.

tests/ is gitignored -> git add -f (the tenant-control docstring says the same).
"""

import datetime
import inspect
import json

import pytest

from saas import dashboard_auth as auth
from saas import llm_config as lc
from saas import tenant_console as tc


ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
USER_A = "11111111-1111-4111-8111-111111111111"
SAVED_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
SAVED_ID_2 = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
_DT = datetime.datetime(2026, 11, 3, 10, 0, 0)


@pytest.fixture
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", "unit-test-signing-secret-0123456789")
    yield


@pytest.fixture
def client(saas_on):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(tc.router)
    return TestClient(app)


def _session_a():
    return auth.issue_session(dashboard_user_id=USER_A, account_id=ACCT_A, username="alice")


def _csrf_a():
    return auth.issue_csrf(dashboard_user_id=USER_A)


# ---------------------------------------------------------------- scripted fake db -----
class FakeCursor:
    """Answers fetches from a scripted FIFO; records every executed SQL (normalized)."""

    def __init__(self, conn):
        self.conn = conn
        self.executed = []
        self._results = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((" ".join(str(sql).split()), params))
        if self.conn.script:
            self._results.append(self.conn.script.pop(0))

    def fetchone(self):
        return self._results.pop(0) if self._results else None

    def fetchall(self):
        r = self._results.pop(0) if self._results else []
        return r if isinstance(r, list) else []

    @property
    def rowcount(self):
        return self.conn.rowcounts.pop(0) if self.conn.rowcounts else 0


class FakeConn:
    def __init__(self):
        self.script = []
        self.rowcounts = []
        self.cursors = []
        self.commits = 0
        self.rolled_back = False

    def cursor(self):
        c = FakeCursor(self)
        self.cursors.append(c)
        return c

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *a):
        if exc_type:
            self.rolled_back = True
        return False

    def commit(self):
        self.commits += 1


class _ScriptedDB:
    """Factory standing in for saas.db.connection: hands back the scripted conns in order."""

    def __init__(self):
        self.queue = []

    def append(self, conn):
        self.queue.append(conn)
        return conn

    def __call__(self):
        return self.queue.pop(0) if self.queue else FakeConn()


@pytest.fixture
def scripted_db(monkeypatch):
    """Every db.connection() inside saas.llm_config yields the scripted FakeConns in order."""
    sdb = _ScriptedDB()
    monkeypatch.setattr(lc.db, "connection", sdb)
    return sdb


def _redacted_row(backend_type="openai", base_url="https://api.example.com/v1",
                  model="gpt-x", api_ct="gAAAAA_fake_ciphertext_blob"):
    """A 20-column account_llm_config row in _REDACT_COLS order (see _redacted_from_row)."""
    return (backend_type, base_url, model, api_ct, {}, {},
            "internal", None, None, None, None,
            _DT, None, None, None, None, None, None, None, None)


def _snapshot_row(saved_id=SAVED_ID, name="prod brain", backend_type="openai",
                  base_url="https://api.example.com/v1", model="gpt-x",
                  api_ct="gAAAAA_snapshot_ciphertext"):
    """A 14-column account_llm_config_saved row (list_saved_configs SELECT order)."""
    return (saved_id, name, backend_type, base_url, model, api_ct,
            {}, {}, "internal", None, None, _DT, _DT)


def _snapshot_apply_row(name="prod brain", backend_type="openai",
                        base_url="https://api.example.com/v1", model="gpt-x",
                        api_ct="gAAAAA_snapshot_ciphertext"):
    """A 12-column saved row in the apply SELECT order (name + the 11 snapshot cols)."""
    return (name, backend_type, base_url, model, api_ct, {}, {},
            "internal", None, None, None, None)


# =========================================================== STORE: redaction (R1) =====
def test_r1_payload_keys_are_exactly_the_spec_set():
    p = lc._saved_projection(_snapshot_row(), True)
    assert set(p.keys()) == set(lc._SAVED_PROJECTION_KEYS)


@pytest.mark.parametrize("api_ct", ["gAAAAA_secret_ciphertext", None, "", "   "])
def test_r1_never_carries_key_material(api_ct):
    p = lc._saved_projection(_snapshot_row(api_ct=api_ct), False)
    blob = json.dumps(p)
    assert "gAAAAA" not in blob and "ciphertext" not in blob
    assert p["api_key_set"] is bool(api_ct and str(api_ct).strip())
    assert p["embedding_key_set"] is False


# =========================================================== STORE: is_active =========
def test_is_active_matches_the_live_triple():
    live = lc._redacted_from_row(ACCT_A, _redacted_row(), scope="account")
    assert live["configured"] is True
    assert lc._saved_is_active("openai", "https://api.example.com/v1", "gpt-x", live) is True


def test_is_active_value_match_normalizes_trailing_slash_and_case():
    live = lc._redacted_from_row(ACCT_A, _redacted_row(), scope="account")
    assert lc._saved_is_active("OpenAI", "https://api.example.com/v1/", "GPT-X", live) is True


def test_is_active_false_on_any_triple_difference():
    live = lc._redacted_from_row(ACCT_A, _redacted_row(), scope="account")
    assert lc._saved_is_active("ollama", "https://api.example.com/v1", "gpt-x", live) is False
    assert lc._saved_is_active("openai", "https://other.example.com/v1", "gpt-x", live) is False
    assert lc._saved_is_active("openai", "https://api.example.com/v1", "gpt-4o", live) is False


def test_is_active_never_true_when_live_brain_unconfigured():
    live = lc._redacted_from_row(ACCT_A, _redacted_row(base_url=None, model=None), scope="account")
    assert live["configured"] is False
    assert lc._saved_is_active("openai", "https://api.example.com/v1", "gpt-x", live) is False
    assert lc._saved_is_active("openai", "https://api.example.com/v1", "gpt-x", None) is False


def test_is_active_never_compares_ciphertexts():
    """Fernet is non-deterministic; the computation reads the triple only (grep the source)."""
    src = inspect.getsource(lc._saved_is_active)
    # pin the CODE, not the prose: the docstring may (and should) explain that Fernet
    # ciphertexts are non-deterministic; the body must never REFERENCE a ciphertext column.
    assert "api_key_ciphertext" not in src
    assert "embedding_key_ciphertext" not in src


# =========================================================== STORE: capture (R2) ======
def test_capture_refuses_when_no_live_brain(scripted_db):
    c = FakeConn()
    scripted_db.append(c)
    c.script = [None]                                        # get_config_redacted -> no row
    with pytest.raises(lc.NoActiveBrainToSave) as e:
        lc.capture_saved_config(ACCT_A, "prod brain")
    assert str(e.value) == ("no active brain to save — configure base_url + model first"
                            " (▸ save config)")


def test_capture_copies_ciphertext_as_is_via_insert_select(scripted_db):
    c0 = FakeConn()
    scripted_db.append(c0)
    c0.script = [_redacted_row()]                            # live read: configured
    c1 = FakeConn()
    scripted_db.append(c1)
    c1.script = [(0,), (SAVED_ID, True)]                     # count, INSERT..SELECT (fresh row: inserted)
    out = lc.capture_saved_config(ACCT_A, "prod brain")
    assert out == {"saved_id": SAVED_ID, "name": "prod brain", "overwritten": False,
                   "account_id": ACCT_A}
    insert_sql, params = c1.cursors[0].executed[-1]
    assert "INSERT INTO public.account_llm_config_saved" in insert_sql
    assert "SELECT %s, %s, backend_type, base_url, model, api_key_ciphertext" in insert_sql
    assert "FROM public.account_llm_config" in insert_sql
    assert "ON CONFLICT (account_id, name) DO UPDATE" in insert_sql
    assert "api_key_ciphertext = EXCLUDED.api_key_ciphertext" in insert_sql
    assert params == (ACCT_A, "prod brain", ACCT_A)          # no key material crosses the wire
    assert c1.commits == 1


def test_capture_overwrite_at_cap_is_allowed(scripted_db):
    c0 = FakeConn()
    scripted_db.append(c0)
    c0.script = [_redacted_row()]
    c1 = FakeConn()
    scripted_db.append(c1)
    c1.script = [(lc.SAVED_BRAIN_CAP,), (1,), (SAVED_ID, False)]   # count=20, name EXISTS, INSERT
    out = lc.capture_saved_config(ACCT_A, "prod brain")
    assert out["overwritten"] is True


def test_capture_cap_409_when_name_is_new(scripted_db):
    c0 = FakeConn()
    scripted_db.append(c0)
    c0.script = [_redacted_row()]
    c1 = FakeConn()
    scripted_db.append(c1)
    c1.script = [(lc.SAVED_BRAIN_CAP,), None]                # count=20, name NOT found
    with pytest.raises(lc.SavedBrainCapReached) as e:
        lc.capture_saved_config(ACCT_A, "one over the cap")
    assert str(e.value) == "saved-brain limit reached (20) — delete one first"


@pytest.mark.parametrize("bad_name", ["", "   ", "x" * 81])
def test_capture_name_validation(bad_name):
    with pytest.raises(ValueError):
        lc.capture_saved_config(ACCT_A, bad_name)


# =========================================================== STORE: apply (R3) ========
def _wire_apply_ok(monkeypatch, scripted_db):
    """Script: snapshot read, INSERT..SELECT. Returns spies + the transaction conn.

    STORE-level only (no route): apply_saved_config opens exactly ONE connection."""
    calls = {"propagate": [], "invalidate": 0, "validate": []}
    c = FakeConn()
    scripted_db.append(c)
    c.script = [_snapshot_apply_row()]
    monkeypatch.setattr(lc, "validate_egress_url",
                        lambda url: calls["validate"].append(url) or {"host": "api.example.com"})
    monkeypatch.setattr(lc, "_propagate_account_brain_to_seats",
                        lambda cur, acct, scope: calls["propagate"].append((acct, scope)) or 0)
    real_invalidate = lc.invalidate_config_cache

    def _invalidate():
        calls["invalidate"] += 1
        real_invalidate()

    monkeypatch.setattr(lc, "invalidate_config_cache", _invalidate)
    return calls, c


def test_apply_copies_ciphertext_columns_untouched(scripted_db, monkeypatch):
    calls, c = _wire_apply_ok(monkeypatch, scripted_db)
    out = lc.apply_saved_config(ACCT_A, SAVED_ID)
    assert out["name"] == "prod brain" and out["saved_id"] == SAVED_ID
    insert_sql, params = c.cursors[0].executed[-1]
    assert "INSERT INTO public.account_llm_config" in insert_sql
    assert "SELECT %s, backend_type, base_url, model, api_key_ciphertext" in insert_sql
    assert "FROM public.account_llm_config_saved" in insert_sql
    assert "ON CONFLICT (account_id) DO UPDATE" in insert_sql
    assert "api_key_ciphertext = EXCLUDED.api_key_ciphertext" in insert_sql
    assert "embedding_key_ciphertext = EXCLUDED.embedding_key_ciphertext" in insert_sql
    assert params == (ACCT_A, SAVED_ID, ACCT_A)
    assert c.commits == 1


def test_apply_fields_projection_carries_no_ciphertext(scripted_db, monkeypatch):
    calls, _ = _wire_apply_ok(monkeypatch, scripted_db)
    f = lc.apply_saved_config(ACCT_A, SAVED_ID)["fields"]
    assert "gAAAAA" not in json.dumps(f)
    assert f["api_key"] is None                  # membership marker: snapshot CARRIED a key
    assert "embedding_key" not in f              # absent when the snapshot had none
    assert f["base_url"] == "https://api.example.com/v1" and f["model"] == "gpt-x"


def test_apply_runs_put_parity_post_write_steps(scripted_db, monkeypatch):
    calls, _ = _wire_apply_ok(monkeypatch, scripted_db)
    lc.apply_saved_config(ACCT_A, SAVED_ID)
    assert calls["propagate"] == [(ACCT_A, lc.APPLY_GOVERNED_ONLY)]
    assert calls["invalidate"] == 1


def test_apply_ssrf_revalidates_snapshot_urls(scripted_db, monkeypatch):
    calls, _ = _wire_apply_ok(monkeypatch, scripted_db)
    lc.apply_saved_config(ACCT_A, SAVED_ID)
    assert calls["validate"] == ["https://api.example.com/v1"]


def test_apply_egress_blocked_aborts_before_any_write(scripted_db, monkeypatch):
    c = FakeConn()
    scripted_db.append(c)
    c.script = [_snapshot_apply_row(api_ct=None)]

    def _blocked(url):
        raise lc.EgressBlocked("host resolved to a private address")

    monkeypatch.setattr(lc, "validate_egress_url", _blocked)
    with pytest.raises(lc.EgressBlocked):
        lc.apply_saved_config(ACCT_A, SAVED_ID)
    executed = [sql for sql, _ in c.cursors[0].executed]
    assert any("SELECT name" in sql for sql in executed)        # snapshot WAS read
    assert not any("INSERT INTO" in sql for sql in executed)    # nothing was written
    assert c.commits == 0


def test_apply_unknown_or_foreign_id_is_lookup_error(scripted_db):
    c = FakeConn()
    scripted_db.append(c)
    c.script = [None]                                          # WHERE misses
    with pytest.raises(LookupError):
        lc.apply_saved_config(ACCT_A, SAVED_ID)
    assert c.commits == 0


@pytest.mark.parametrize("bad", ["not-a-uuid", ""])
def test_apply_malformed_id_is_404_not_500(bad):
    with pytest.raises(LookupError):
        lc.apply_saved_config(ACCT_A, bad)


def test_apply_source_never_calls_encode_fields():
    """THE DOUBLE-ENCRYPTION TRAP (spec 9.1), pinned on the final source: the apply path must
    copy ciphertext columns as-is and must never CALL _encode_fields (the docstrings warn
    about it by name, so the pin is the CALL SYNTAX, not the word)."""
    for fn in (lc.apply_saved_config, lc._apply_snapshot_cur, lc.capture_saved_config,
               lc.delete_saved_config):
        src = inspect.getsource(fn)
        assert "_encode_fields(" not in src, fn.__name__ + " calls _encode_fields"
        assert "build_llm_config_fields(" not in src, fn.__name__


# =========================================================== STORE: delete (R4) =======
def _wire_delete(scripted_db, *, target_triple, live_row=_redacted_row(),
                 replacement_row=None):
    """Script a delete. Path with NO replacement: target read, live redacted read, DELETE.
    Path WITH a replacement: target read, replacement snapshot read, INSERT..SELECT, DELETE."""
    c = FakeConn()
    scripted_db.append(c)
    c.script = [("prod brain",) + tuple(target_triple)]       # target read (name+triple)
    if replacement_row is None:
        # The ACTIVE guard only runs when no replacement was named — script the live redacted
        # read on that path only (with a replacement, _apply_snapshot_cur reads next).
        c.script.append(live_row if live_row is not None else None)
    else:
        c.script.append(replacement_row)                      # replacement snapshot read
    return c


def test_delete_active_without_replacement_is_blocked(scripted_db):
    c = _wire_delete(scripted_db, target_triple=("openai", "https://api.example.com/v1", "gpt-x"))
    with pytest.raises(lc.ActiveSavedDeleteBlocked) as e:
        lc.delete_saved_config(ACCT_A, SAVED_ID)
    assert e.value.saved_id == SAVED_ID
    assert e.value.name == "prod brain"
    assert "saved copy" in str(e.value) and "replacement" in str(e.value)
    executed = [sql for sql, _ in c.cursors[0].executed]
    assert not any("DELETE FROM" in sql for sql in executed)
    assert c.commits == 0


def test_delete_non_active_succeeds(scripted_db, monkeypatch):
    c = _wire_delete(scripted_db, target_triple=("ollama", "https://other.example.com", "m"))
    c.rowcounts = [1]
    calls = {"invalidate": 0}

    def _inv():
        calls["invalidate"] += 1

    monkeypatch.setattr(lc, "invalidate_config_cache", _inv)
    out = lc.delete_saved_config(ACCT_A, SAVED_ID)
    assert out == {"saved_id": SAVED_ID, "name": "prod brain", "replacement_applied": False}
    executed = [sql for sql, _ in c.cursors[0].executed]
    assert any("DELETE FROM public.account_llm_config_saved" in sql for sql in executed)
    assert calls["invalidate"] == 0      # a pure snapshot delete does not touch the live row


def test_delete_with_replacement_applies_then_deletes_one_transaction(scripted_db, monkeypatch):
    c = _wire_delete(scripted_db, target_triple=("openai", "https://api.example.com/v1", "gpt-x"),
                     replacement_row=_snapshot_apply_row(name="bench brain", backend_type="ollama",
                                                         base_url="https://b.example.com",
                                                         model="m2"))
    c.rowcounts = [1]                                        # the final DELETE rowcount
    monkeypatch.setattr(lc, "validate_egress_url", lambda url: {"host": "b.example.com"})
    propagate = []
    monkeypatch.setattr(lc, "_propagate_account_brain_to_seats",
                        lambda cur, acct, scope: propagate.append(acct) or 0)
    out = lc.delete_saved_config(ACCT_A, SAVED_ID, replacement_saved_id=SAVED_ID_2)
    assert out["replacement_applied"] is True
    executed = [sql for sql, _ in c.cursors[0].executed]
    insert_idx = next(i for i, sql in enumerate(executed)
                      if "INSERT INTO public.account_llm_config " in sql)
    delete_idx = next(i for i, sql in enumerate(executed)
                      if "DELETE FROM public.account_llm_config_saved" in sql)
    assert insert_idx < delete_idx                            # applied THEN deleted
    assert propagate == [ACCT_A]
    assert c.commits == 1                                     # ONE commit, one transaction


def test_delete_failing_replacement_leaves_target_not_deleted(scripted_db, monkeypatch):
    c = _wire_delete(scripted_db, target_triple=("openai", "https://api.example.com/v1", "gpt-x"),
                     replacement_row=_snapshot_apply_row(name="bench brain",
                                                         base_url="https://b.example.com",
                                                         model="m2", api_ct=None))

    def _blocked(url):
        raise lc.EgressBlocked("host resolved to a private address")

    monkeypatch.setattr(lc, "validate_egress_url", _blocked)
    with pytest.raises(lc.EgressBlocked):
        lc.delete_saved_config(ACCT_A, SAVED_ID, replacement_saved_id=SAVED_ID_2)
    executed = [sql for sql, _ in c.cursors[0].executed]
    assert any("SELECT name" in sql for sql in executed)       # replacement WAS read
    assert not any("INSERT INTO" in sql for sql in executed)   # replacement NOT applied
    assert not any("DELETE FROM" in sql for sql in executed)   # target NOT deleted
    assert c.commits == 0                                      # nothing committed


def test_delete_unknown_id_is_lookup_error(scripted_db):
    c = FakeConn()
    scripted_db.append(c)
    c.script = [None]                                          # target WHERE misses
    with pytest.raises(LookupError):
        lc.delete_saved_config(ACCT_A, SAVED_ID)
    assert c.commits == 0


def test_delete_replacement_must_differ_from_target(scripted_db):
    with pytest.raises(ValueError):
        lc.delete_saved_config(ACCT_A, SAVED_ID, replacement_saved_id=SAVED_ID)


def test_list_returns_payload_with_computed_is_active(scripted_db):
    c0 = FakeConn()
    scripted_db.append(c0)
    c0.script = [
        [_snapshot_row(SAVED_ID, "prod"),
         _snapshot_row(SAVED_ID_2, "bench", backend_type="ollama",
                       base_url="https://other.example.com", model="m2")],
    ]
    c1 = FakeConn()
    scripted_db.append(c1)
    c1.script = [_redacted_row()]                              # live redacted read
    out = lc.list_saved_configs(ACCT_A)
    assert out["account_id"] == ACCT_A and out["count"] == 2
    assert [s["is_active"] for s in out["saved"]] == [True, False]
    assert out["saved"][0]["api_key_set"] is True
    assert "gAAAAA" not in json.dumps(out)


def test_list_empty_account_is_count_zero_never_404(scripted_db):
    c0 = FakeConn()
    scripted_db.append(c0)
    c0.script = [[]]
    c1 = FakeConn()
    scripted_db.append(c1)
    c1.script = [None]
    out = lc.list_saved_configs(ACCT_A)
    assert out == {"account_id": ACCT_A, "count": 0, "saved": []}


# ================================================================ ROUTES ==============
# From here the store is faked with recording functions; the route contracts (session scope,
# BOLA mapping, CSRF, status/detail mapping) are what is under test.
@pytest.fixture
def fake_store(monkeypatch):
    calls = {"list": [], "capture": [], "apply": [], "delete": []}

    def _list(account_id):
        calls["list"].append(account_id)
        return {"account_id": account_id, "count": 1,
                "saved": [dict(lc._saved_projection(_snapshot_row(), True))]}

    def _capture(account_id, name):
        calls["capture"].append((account_id, name))
        if name == "over the cap":
            raise lc.SavedBrainCapReached("saved-brain limit reached (20) — delete one first")
        if name == "no brain":
            raise lc.NoActiveBrainToSave("no active brain to save — configure base_url + model"
                                         " first (▸ save config)")
        return {"saved_id": SAVED_ID, "name": name, "overwritten": False,
                "account_id": account_id}

    def _apply(account_id, saved_id):
        calls["apply"].append((account_id, saved_id))
        if saved_id != SAVED_ID:
            raise LookupError("no such saved brain config for this account")
        return {"saved_id": SAVED_ID, "name": "prod brain",
                "fields": {"model": "gpt-x", "base_url": "https://api.example.com/v1",
                           "api_key": None}}

    def _delete(account_id, saved_id, replacement_saved_id=None):
        calls["delete"].append((account_id, saved_id, replacement_saved_id))
        if saved_id != SAVED_ID:
            raise LookupError("no such saved brain config for this account")
        if replacement_saved_id is None:
            raise lc.ActiveSavedDeleteBlocked("prod brain", SAVED_ID)
        return {"saved_id": SAVED_ID, "name": "prod brain", "replacement_applied": True}

    monkeypatch.setattr(tc._llm_config, "list_saved_configs", _list)
    monkeypatch.setattr(tc._llm_config, "capture_saved_config", _capture)
    monkeypatch.setattr(tc._llm_config, "apply_saved_config", _apply)
    monkeypatch.setattr(tc._llm_config, "delete_saved_config", _delete)
    return calls


@pytest.fixture
def authed_client(client):
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    return client


def test_list_route_session_scope_and_payload(authed_client, fake_store):
    r = authed_client.get("/tenant/llm-config/saved")
    assert r.status_code == 200
    assert fake_store["list"] == [ACCT_A]
    body = r.json()
    assert body["account_id"] == ACCT_A and body["count"] == 1
    assert "gAAAAA" not in json.dumps(body)


def test_capture_route_ignores_client_supplied_account(authed_client, fake_store):
    r = authed_client.post("/tenant/llm-config/saved", headers={"x-csrf-token": _csrf_a()},
                           json={"name": "prod brain", "account_id": ACCT_B})
    assert r.status_code == 200
    assert fake_store["capture"] == [(ACCT_A, "prod brain")]   # SESSION account, not B
    assert r.json()["status"] == "saved"


@pytest.mark.parametrize("bad_name", ["", "   ", "x" * 81])
def test_capture_route_rejects_bad_names(authed_client, fake_store, bad_name):
    r = authed_client.post("/tenant/llm-config/saved", headers={"x-csrf-token": _csrf_a()},
                           json={"name": bad_name})
    assert r.status_code == 400
    assert fake_store["capture"] == []


def test_capture_route_surfaces_cap_409(authed_client, fake_store):
    r = authed_client.post("/tenant/llm-config/saved", headers={"x-csrf-token": _csrf_a()},
                           json={"name": "over the cap"})
    assert r.status_code == 409
    assert r.json()["detail"] == "saved-brain limit reached (20) — delete one first"


def test_capture_route_surfaces_no_brain_409(authed_client, fake_store):
    r = authed_client.post("/tenant/llm-config/saved", headers={"x-csrf-token": _csrf_a()},
                           json={"name": "no brain"})
    assert r.status_code == 409
    assert "no active brain to save" in r.json()["detail"]


def test_apply_route_success_mirrors_put_shape(authed_client, fake_store, monkeypatch):
    cal = []
    import saas.brain_autocalibrate as autocal
    monkeypatch.setattr(autocal, "schedule_after_save",
                        lambda **kw: cal.append(kw) or "replace")
    r = authed_client.post("/tenant/llm-config/saved/" + SAVED_ID + "/apply",
                           headers={"x-csrf-token": _csrf_a()})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "applied"
    assert body["saved_id"] == SAVED_ID and body["account_id"] == ACCT_A
    assert body["fields"] == sorted(body["fields"])            # sorted, per spec
    assert body["calibration"] == "scheduled"
    assert cal[0]["scope"] == "account" and cal[0]["account_id"] == ACCT_A
    assert cal[0]["fields"]["base_url"] == "https://api.example.com/v1"
    assert fake_store["apply"] == [(ACCT_A, SAVED_ID)]


def test_apply_route_prior_redacted_read_before_apply(authed_client, fake_store, monkeypatch):
    order = []
    import saas.brain_autocalibrate as autocal
    monkeypatch.setattr(tc._llm_config, "get_config_redacted",
                        lambda acct: order.append("prior") or {"configured": True})
    monkeypatch.setattr(autocal, "schedule_after_save",
                        lambda **kw: order.append(("cal", kw.get("prior_redacted"))) or None)
    r = authed_client.post("/tenant/llm-config/saved/" + SAVED_ID + "/apply",
                           headers={"x-csrf-token": _csrf_a()})
    assert r.status_code == 200
    assert order[0] == "prior"                                 # pre-apply read, then cal
    assert order[1][1] == {"configured": True}


def test_apply_route_egress_blocked_is_400_with_put_identical_prefix(authed_client, fake_store, monkeypatch):
    import saas.brain_autocalibrate as autocal
    monkeypatch.setattr(autocal, "schedule_after_save", lambda **kw: None)

    def _apply(account_id, saved_id):
        raise lc.EgressBlocked("host resolved to a private address")

    monkeypatch.setattr(tc._llm_config, "apply_saved_config", _apply)
    r = authed_client.post("/tenant/llm-config/saved/" + SAVED_ID + "/apply",
                           headers={"x-csrf-token": _csrf_a()})
    assert r.status_code == 400
    assert r.json()["detail"].startswith("endpoint URL rejected: ")


def test_apply_route_bola_foreign_id_is_404(authed_client, fake_store):
    r = authed_client.post("/tenant/llm-config/saved/" + SAVED_ID_2 + "/apply",
                           headers={"x-csrf-token": _csrf_a()})
    assert r.status_code == 404
    assert r.json()["detail"] == "no such saved brain config for this account"
    assert fake_store["apply"] == [(ACCT_A, SAVED_ID_2)]       # observed with A, never B


def test_delete_route_active_guard_409_carries_active_saved_id(authed_client, fake_store):
    r = authed_client.request("DELETE", "/tenant/llm-config/saved/" + SAVED_ID,
                              headers={"x-csrf-token": _csrf_a()})
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["active_saved_id"] == SAVED_ID
    assert "ACTIVE brain" in body["detail"]


def test_delete_route_with_replacement_succeeds(authed_client, fake_store):
    r = authed_client.request("DELETE", "/tenant/llm-config/saved/" + SAVED_ID,
                              headers={"x-csrf-token": _csrf_a()},
                              json={"replacement_saved_id": SAVED_ID_2})
    assert r.status_code == 200
    assert r.json() == {"status": "deleted", "saved_id": SAVED_ID, "replacement_applied": True}
    assert fake_store["delete"] == [(ACCT_A, SAVED_ID, SAVED_ID_2)]


def test_delete_route_bola_foreign_id_is_404(authed_client, fake_store):
    r = authed_client.request("DELETE", "/tenant/llm-config/saved/" + SAVED_ID_2,
                              headers={"x-csrf-token": _csrf_a()},
                              json={"replacement_saved_id": SAVED_ID})
    assert r.status_code == 404


def test_unsafe_routes_require_csrf(authed_client, fake_store):
    assert authed_client.post("/tenant/llm-config/saved", json={"name": "n"}).status_code == 403
    r = authed_client.post("/tenant/llm-config/saved/" + SAVED_ID + "/apply")
    assert r.status_code == 403
    assert authed_client.delete("/tenant/llm-config/saved/" + SAVED_ID).status_code == 403
    assert fake_store["capture"] == [] and fake_store["apply"] == [] and fake_store["delete"] == []


def test_list_route_requires_session(client, fake_store):
    assert client.get("/tenant/llm-config/saved").status_code == 401
    assert fake_store["list"] == []


def test_storage_failure_maps_to_503(authed_client, monkeypatch):
    def _list(account_id):
        raise lc.ConfigStorageError("list_saved_configs failed to persist the endpoint (X)")

    monkeypatch.setattr(tc._llm_config, "list_saved_configs", _list)
    r = authed_client.get("/tenant/llm-config/saved")
    assert r.status_code == 503
    assert "fault on our side" in r.json()["detail"]

# ====================================================== STORE: list-path arity (R1) ====
# REGRESSION PIN (found live 2026-11-04, pre-prod acct 819e2ae3): the list SELECT must
# fetch EXACTLY the columns _saved_projection unpacks. _SAVED_SNAPSHOT_COLS carries the
# extra_headers_json/extra_params_json pair for the copy-AS-IS lanes; selecting it here
# made every list with >=1 saved row raise ValueError (too many values to unpack) —
# invisible while the table was empty, fatal the first time a tenant saved a brain.
def test_list_select_columns_match_projection_arity():
    n = 2 + len(lc._SAVED_LIST_COLS) + 2  # saved_id, name ... created_at, updated_at
    assert n == 13, "list SELECT arity drifted from the _saved_projection unpack"


def test_list_select_omits_copy_only_columns():
    sql_cols = set(lc._SAVED_LIST_COLS)
    assert "extra_headers_json" not in sql_cols
    assert "extra_params_json" not in sql_cols


def test_list_projection_survives_a_real_saved_row(scripted_db, monkeypatch):
    """End-to-end through list_saved_configs: one saved row -> redacted payload, no 500."""
    row = (SAVED_ID, "prod brain", "openai", "https://api.example.com/v1", "gpt-x",
           "gAAAAA_snapshot_ciphertext", "internal", None, None, None, None,
           _DT, _DT)  # saved_id, name + _SAVED_LIST_COLS order + created_at, updated_at
    conn = scripted_db.append(FakeConn())
    conn.script.append([row])                      # the list SELECT
    conn2 = scripted_db.append(FakeConn())
    conn2.script.append(_redacted_row())           # get_config_redacted live read (fetchone)
    out = lc.list_saved_configs(ACCT_A)
    assert out["count"] == 1
    saved = out["saved"][0]
    assert saved["name"] == "prod brain"
    assert saved["api_key_set"] is True
    assert "gAAAAA" not in json.dumps(out)

