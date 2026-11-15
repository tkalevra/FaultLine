"""Gauntlet lane ``bench-sandbox-ready-wait`` — the self-benchmark waits on provisioning
``status='ready'``, not on the schema's mere existence.

THE WOUND (pre-prod, measured twice 2026-09-15, ``round_1/preprod_litmus_10of12.log`` lines 9–13):
``saas/bench_selftest.py::_wait_provisioned`` polled ``information_schema.schemata`` and returned
the moment the seat's SCHEMA existed. ``schema_manager.create_user_schema`` commits ``CREATE
SCHEMA`` first, applies the table template in a psql subprocess that takes tens of seconds on the
pre-prod ZFS pool, and commits ``status='ready'`` as its LAST write (round-17 first-touch fix). So
for a window of seconds the schema exists and its tables do not; the first claim's
``_reset_sandbox`` TRUNCATE raised ``UndefinedTable`` 3/3 and the claim was graded ERROR on every
run although its recall was correct ('Red', no 'blue').

Pins:
  BW1  the old shape, DRIVEN: schema present + tables absent → the old probe says "provisioned",
       the reset raises ``psycopg2.errors.UndefinedTable`` (by identity) and reports a dirty seat.
       Against a throwaway local Postgres when ``GAUNTLET_BENCH_TEST_DSN`` names one (NEVER the db
       named ``faultline``), and always against a psycopg2 seam stub raising the real class.
  BW2  the new wait consumes the provisioning-status seam (``check_provisioning_status`` — the
       reader behind ``GET /provisioning/status`` — classified by the MCP's own
       ``_classify_probe_body``): returns ONLY on ``ready``, keeps polling through ``provisioning``
       / ``not_found`` / a probe-side error, raises ``SandboxNotReady`` at once on a TERMINAL row,
       raises loud at the 180 s budget, and the run becomes an ERRORED run with that reason — no
       claim is reset or scored on an unprovisioned seat.
  BW3  census: bench_selftest carries no second copy of the status poll (no
       ``information_schema`` / ``user_provisioning`` SQL, no ``_schema_exists``, no sleep at all
       in the adapter), supplies its probe to the ONE shared ``wait_until_ready`` (which is where
       ``_classify_probe_body`` is read and the one cadence sleep lives), and keeps the 180 s
       budget. (Re-pinned by the twin lane claims-harness-ready-wait, which lifted the loop.)
       Revert-red is the fltest run recorded in ``round_1/revert_red.txt``.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import os
import pathlib
import time
import uuid
from types import SimpleNamespace

import psycopg2
import psycopg2.errors
import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
BENCH_SRC = REPO / "saas" / "bench_selftest.py"

_DSN = os.environ.get("GAUNTLET_BENCH_TEST_DSN", "")


def _dsn_or_skip() -> str:
    """The DB-driven pins need a THROWAWAY database. Refuse anything that smells like a real
    inventory or a remote box — this lane is LOCAL ONLY and never the db named ``faultline``."""
    if not _DSN:
        pytest.skip("GAUNTLET_BENCH_TEST_DSN unset — export a THROWAWAY local Postgres DSN "
                    "(e.g. …/gauntlet_bench_test) to drive BW1/BW2 against a real database; "
                    "the seam-stub pins below still run")
    dbname = _DSN.rsplit("/", 1)[-1].split("?", 1)[0]
    assert dbname not in ("faultline", "faultline_test", "postgres"), \
        f"refusing to drive a destructive pin against db {dbname!r}"
    for banned in ("truenas", "faultline.ca", "192.0.2.10", "faultline-prod"):
        assert banned not in _DSN, f"refusing a non-local DSN ({banned})"
    return _DSN


@pytest.fixture
def db_seat(monkeypatch):
    """A seat whose SCHEMA exists but whose TABLES do not — exactly the window the old wait returned
    in — plus a ``public.user_provisioning`` row the status seam reads. Torn down after."""
    dsn = _dsn_or_skip()
    monkeypatch.setenv("POSTGRES_DSN", dsn)
    from saas import bench_selftest as B
    seat = str(uuid.uuid4())
    schema = B._schema_for(seat)
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(f"CREATE SCHEMA {schema}")  # the state after schema_manager's FIRST commit
    cur.execute("CREATE TABLE IF NOT EXISTS public.rel_types ("
                "rel_type text primary key, natural_language text, natural_language_2p text)")
    cur.execute("CREATE TABLE IF NOT EXISTS public.user_provisioning ("
                "user_id text primary key, status text, schema_name text, error_message text, "
                "ready_at timestamptz)")
    cur.execute("INSERT INTO public.user_provisioning (user_id, status, schema_name) "
                "VALUES (%s, 'provisioning', %s)", (seat, schema))
    try:
        yield SimpleNamespace(conn=conn, cur=cur, seat=seat, schema=schema, B=B)
    finally:
        cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (seat,))
        conn.close()


def _capture_log(monkeypatch, B) -> list[str]:
    lines: list[str] = []
    monkeypatch.setattr(B, "_log", lambda msg: lines.append(str(msg)))
    monkeypatch.setattr(B, "httpx", None)   # the best-effort Qdrant clear is not under test
    return lines


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ═══════════════════════════════════════════════════════════════════════════════════════
# BW1 — the old shape, driven
# ═══════════════════════════════════════════════════════════════════════════════════════

def _old_probe_schema_exists(cur, schema: str) -> bool:
    """The probe the OLD ``_wait_provisioned`` returned on (kept here, in the test, as the record
    of the wrong signal — it must not exist in the module, see BW3)."""
    cur.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (schema,))
    return cur.fetchone() is not None


def test_bw1_db_schema_present_tables_absent_old_probe_says_ready_reset_raises_undefined_table(
        db_seat, monkeypatch):
    B, cur = db_seat.B, db_seat.cur
    cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = %s",
                (db_seat.schema,))
    assert cur.fetchone()[0] == 0, "fixture: the schema must carry NO tables"
    # (a) the OLD signal fires in this state — it is what let the reset run early
    assert _old_probe_schema_exists(cur, db_seat.schema) is True
    # (b) the reset's own TRUNCATE raises UndefinedTable — pinned by exception IDENTITY
    with pytest.raises(psycopg2.errors.UndefinedTable):
        with psycopg2.connect(_DSN) as c2, c2.cursor() as k:
            k.execute(f"TRUNCATE {db_seat.schema}.facts RESTART IDENTITY CASCADE")
    # (c) the shipped reset, driven: every attempt fails the same way and it reports a dirty seat
    lines = _capture_log(monkeypatch, B)
    assert B._reset_sandbox(db_seat.seat, attempts=2) is False
    assert sum("FAILED (UndefinedTable)" in l for l in lines) == 2, lines
    assert any("would run on a dirty seat" in l for l in lines), lines
    # (d) the NEW wait does NOT return in this state: the row says 'provisioning'. (On the
    # reverted module this is the wound itself: it returns True in ~0.02 s → DID NOT RAISE.)
    monkeypatch.setattr(B, "_PROVISION_POLL_S", 0.05, raising=False)
    with pytest.raises(Exception) as ei:
        _run(B._wait_provisioned(db_seat.seat, timeout=0.3))
    assert type(ei.value).__name__ == "SandboxNotReady", repr(ei.value)
    assert "'provisioning'" in str(ei.value) and "no claim scored" in str(ei.value)


def test_bw1_stub_reset_against_schema_without_tables_reports_undefined_table(monkeypatch):
    """The same shape with the psycopg2 seam stubbed to raise the REAL exception class the server
    sends for a missing relation (SQLSTATE 42P01 → ``psycopg2.errors.UndefinedTable``)."""
    from saas import bench_selftest as B
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://stub/gauntlet_bench_test")
    seat = str(uuid.uuid4())
    executed: list[str] = []

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, *a):
            executed.append(sql)
            if sql.startswith("TRUNCATE"):
                raise psycopg2.errors.UndefinedTable(
                    f'relation "{B._schema_for(seat)}.facts" does not exist')

    class _Conn:
        committed = False
        def cursor(self): return _Cur()
        def commit(self): self.committed = True
        def close(self): pass

    conns: list[_Conn] = []

    def _connect(dsn):
        conns.append(_Conn()); return conns[-1]
    monkeypatch.setattr(B.psycopg2, "connect", _connect)
    monkeypatch.setattr(B.time, "sleep", lambda s: None)   # the inter-attempt backoff, not under test
    lines = _capture_log(monkeypatch, B)

    assert B._reset_sandbox(seat, attempts=3) is False
    assert sum("FAILED (UndefinedTable)" in l for l in lines) == 3, lines
    assert any("NOT reset after 3 attempts" in l and "dirty seat" in l for l in lines), lines
    assert len(conns) == 3 and not any(c.committed for c in conns)
    assert any(s.startswith("TRUNCATE") for s in executed)


# ═══════════════════════════════════════════════════════════════════════════════════════
# BW2 — the new wait consumes the status seam
# ═══════════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def seam(monkeypatch):
    """Feed ``check_provisioning_status`` a scripted sequence of answers (the dicts the real seam
    returns — see ``src/provisioning/provisioning_status.py``) and record how often it was read."""
    from saas import bench_selftest as B
    from src.provisioning import provisioning_status as PS
    state = SimpleNamespace(answers=[], calls=0, B=B, lines=_capture_log(monkeypatch, B))
    monkeypatch.setattr(B, "_PROVISION_POLL_S", 0.01, raising=False)  # raising=False: the reverted
    # module lacks the knob, and the revert-red must show the BEHAVIOUR, not a setup AttributeError

    def _check(user_id):
        state.calls += 1
        a = state.answers[min(state.calls, len(state.answers)) - 1]
        if isinstance(a, Exception):
            raise a
        return dict(a, user_id=user_id)
    monkeypatch.setattr(PS, "check_provisioning_status", _check)
    return state


_SEAT = "5f6d9dce-396f-414e-bac4-bbec109969aa"
_SCHEMA = "faultline_5f6d9dce_396f_414e_bac4_bbec109969aa"


def test_bw2_returns_only_on_ready(seam):
    seam.answers = [{"status": "provisioning", "schema_name": _SCHEMA},
                    {"status": "provisioning", "schema_name": _SCHEMA},
                    {"status": "ready", "schema_name": _SCHEMA, "ready_at": "2026-09-15T00:00:00"}]
    out = _run(seam.B._wait_provisioned(_SEAT, timeout=5.0))
    assert out == _SCHEMA
    assert seam.calls == 3, "returned before the seam said ready"
    assert any("ready" in l and "attempt 3" in l for l in seam.lines), seam.lines


def test_bw2_keeps_polling_through_not_found_and_probe_side_error(seam):
    """``not_found`` (the row not visible yet) and ``status: error`` WITHOUT a seat row (the seam's
    own except-dict — the DB refused the probe) are not verdicts on the seat: keep polling."""
    seam.answers = [{"status": "not_found"},
                    {"status": "error", "error_message": "Failed to check status",
                     "correlation_id": "abc123def456"},
                    RuntimeError("thread blew up"),
                    {"status": "ready", "schema_name": _SCHEMA}]
    assert _run(seam.B._wait_provisioned(_SEAT, timeout=5.0)) == _SCHEMA
    assert seam.calls == 4


def test_bw2_terminal_row_raises_at_once_not_after_the_budget(seam):
    """A row in a terminal state (``error`` WITH a seat row, ``failed``, unknown) is never retried
    by the queue — waiting 180 s for it would be a fabricated 'still provisioning'."""
    seam.answers = [{"status": "error", "schema_name": _SCHEMA,
                     "error_message": "provisioning failed", "correlation_id": "c0ffee00c0de"}]
    t0 = time.monotonic()
    with pytest.raises(Exception) as ei:
        _run(seam.B._wait_provisioned(_SEAT, timeout=30.0))   # 30 s budget; must not be waited
    assert type(ei.value).__name__ == "SandboxNotReady", repr(ei.value)
    assert time.monotonic() - t0 < 1.0
    assert seam.calls == 1
    msg = str(ei.value)
    assert "TERMINAL" in msg and "'error'" in msg and "c0ffee00c0de" in msg and "no claim scored" in msg
    assert ei.value.public_reason == msg and ei.value.correlation_id == "c0ffee00c0de"
    assert ei.value.last_status == "error"
    assert any("TERMINAL" in l for l in seam.lines), "the reason is logged, not only raised"


@pytest.mark.parametrize("status", ["failed", "wedged"])
def test_bw2_failed_and_unknown_statuses_are_terminal(seam, status):
    seam.answers = [{"status": status, "schema_name": _SCHEMA}]
    t0 = time.monotonic()
    with pytest.raises(Exception) as ei:
        _run(seam.B._wait_provisioned(_SEAT, timeout=30.0))
    assert type(ei.value).__name__ == "SandboxNotReady" and time.monotonic() - t0 < 1.0
    assert seam.calls == 1


def test_bw2_budget_exhausted_raises_loud_naming_the_last_answer(seam):
    seam.answers = [{"status": "provisioning", "schema_name": _SCHEMA}]
    t0 = time.monotonic()
    with pytest.raises(Exception) as ei:       # reverted module: returns False silently → DID NOT RAISE
        _run(seam.B._wait_provisioned(_SEAT, timeout=0.08))
    assert type(ei.value).__name__ == "SandboxNotReady", repr(ei.value)
    elapsed = time.monotonic() - t0
    assert 0.05 <= elapsed < 1.0, elapsed          # bounded by the budget, not by an attempt count
    assert seam.calls >= 3
    msg = str(ei.value)
    assert "not ready after 0s" in msg or "not ready after" in msg
    assert "'provisioning'" in msg and "wait" in msg and "no claim scored" in msg
    assert ei.value.last_status == "provisioning"
    assert any("not ready after" in l for l in seam.lines)


def test_bw2_db_status_row_drives_the_wait(db_seat, monkeypatch):
    """Against the throwaway database: the REAL seam reads the REAL row. 'provisioning' → budget
    raise; 'ready' → return with the row's schema; 'error' + schema → immediate terminal raise."""
    B, cur, seat = db_seat.B, db_seat.cur, db_seat.seat
    _capture_log(monkeypatch, B)
    monkeypatch.setattr(B, "_PROVISION_POLL_S", 0.05, raising=False)
    with pytest.raises(Exception) as ei:       # reverted module: returns True at once → DID NOT RAISE
        _run(B._wait_provisioned(seat, timeout=0.25))
    assert type(ei.value).__name__ == "SandboxNotReady" and ei.value.last_status == "provisioning"

    cur.execute("UPDATE public.user_provisioning SET status='ready', ready_at=now() "
                "WHERE user_id=%s", (seat,))
    assert _run(B._wait_provisioned(seat, timeout=5.0)) == db_seat.schema

    cur.execute("UPDATE public.user_provisioning SET status='error', "
                "error_message='provisioning failed' WHERE user_id=%s", (seat,))
    t0 = time.monotonic()
    with pytest.raises(Exception) as ei:
        _run(B._wait_provisioned(seat, timeout=30.0))
    assert type(ei.value).__name__ == "SandboxNotReady"
    assert time.monotonic() - t0 < 2.0 and "TERMINAL" in str(ei.value)


def _sandbox_stubs(monkeypatch, B):
    """The control-plane rows run_selftest mints, stubbed at the narrowest seam. Records whether a
    claim was ever reset/ingested and whether teardown ran."""
    from src.provisioning import provisioning_status as PS
    from src.mcp import server as S
    rec = SimpleNamespace(reset=0, ingest=0, torn=None)
    monkeypatch.setattr(B, "saas_mode_enabled", lambda: True)
    monkeypatch.setattr(B, "_load_claim_lib", lambda: SimpleNamespace(
        CLAIMS=[{"name": "c1", "category": "x", "setup_turns": ["I like tea."],
                 "query": "what do I like?", "why": ""}],
        assess=lambda c, t: {"passed": True, "expected": "", "checks": []}))
    monkeypatch.setattr(B._accounts, "create_account",
                        lambda **kw: {"account_id": "acct-sandbox", "slug": kw.get("slug")})
    monkeypatch.setattr(B._seats, "register_seat", lambda *a, **kw: {"ok": True})
    monkeypatch.setattr(B._llm_config, "upsert_config", lambda *a, **kw: None)
    monkeypatch.setattr(PS, "ensure_user_provisioned", lambda *a, **kw: False)

    def _reset(seat_uuid, **kw):
        rec.reset += 1; return True
    monkeypatch.setattr(B, "_reset_sandbox", _reset)

    async def _remember(*a, **kw):
        rec.ingest += 1; return {"status": "ok"}
    monkeypatch.setattr(S, "remember_facts_tool", _remember)
    monkeypatch.setattr(B, "_teardown_sandbox", lambda a, s, u: setattr(rec, "torn", u) or {})
    return rec


def test_bw2_run_selftest_never_resets_or_scores_a_claim_when_the_seat_is_not_ready(seam, monkeypatch):
    B = seam.B
    rec = _sandbox_stubs(monkeypatch, B)
    seam.answers = [{"status": "provisioning", "schema_name": _SCHEMA}]
    # the call site keeps its 180 s budget; collapse it for the drive via the poll cadence + a
    # deadline the test controls
    orig = B._wait_provisioned
    monkeypatch.setattr(B, "_wait_provisioned",
                        lambda seat_uuid, timeout=180.0: orig(seat_uuid, timeout=0.05))
    with pytest.raises(B.SandboxNotReady):
        _run(B.run_selftest("acct-owner", {"backend_type": "openai",
                                           "base_url": "http://brain.example.test/v1",
                                           "model": "stub"}))
    assert rec.reset == 0 and rec.ingest == 0, "a claim ran on an unprovisioned seat"
    assert rec.torn, "the sandbox is still torn down on an errored run"


def test_bw2_start_run_marks_an_errored_run_with_the_loud_reason(seam, monkeypatch):
    """The status door (``/me/benchmark/status`` → ``get_status``) renders ``error`` — for a
    SandboxNotReady that is the authored reason; any other exception stays a bare type name."""
    B = seam.B
    from saas import seat_brain as SB, community_bench as CB
    from src.mcp import server as S
    monkeypatch.setattr(B, "saas_mode_enabled", lambda: True)
    monkeypatch.setattr(SB, "resolve_brain", lambda *a, **kw: {"backend_type": "openai",
                                                              "base_url": "http://b/v1",
                                                              "model": "m"})
    monkeypatch.setattr(CB, "record_consent", lambda *a, **kw: None)
    tasks: list = []
    monkeypatch.setattr(S, "_background", lambda coro: tasks.append(coro))
    monkeypatch.setattr(B, "_RUNS", {})
    monkeypatch.setattr(B, "_ACCOUNT_RUNS", {})

    reason = "sandbox seat be6f92c6 not ready after 180s (…) — run ERRORED, no claim scored"

    async def _not_ready(account_id, brain_cfg, progress_cb=None):
        raise B.SandboxNotReady(reason, last_status="provisioning")
    monkeypatch.setattr(B, "run_selftest", _not_ready)

    async def drive(uid):
        out = await B.start_run(uid, "acct-1")
        assert out == {"ok": True, "status_code": 202, "status": "running"}
        await tasks.pop()
        return B.get_status(uid)
    st = _run(drive("user-a"))
    assert st["status"] == "error" and st["error"] == reason, st
    assert st["result"] is None
    assert B._ACCOUNT_RUNS["acct-1"]["inflight"] == 0

    async def _leaky(account_id, brain_cfg, progress_cb=None):
        raise psycopg2.OperationalError("password=hunter2 host=db refused")
    monkeypatch.setattr(B, "run_selftest", _leaky)
    monkeypatch.setattr(B, "_RUNS", {})
    monkeypatch.setattr(B, "_ACCOUNT_RUNS", {})
    st = _run(drive("user-b"))
    assert st["status"] == "error" and st["error"] == "OperationalError", st   # no message leak


# ═══════════════════════════════════════════════════════════════════════════════════════
# BW3 — census: one seam, no second copy, no settle heuristics, 180 s kept
# ═══════════════════════════════════════════════════════════════════════════════════════

def _module_ast() -> ast.Module:
    return ast.parse(BENCH_SRC.read_text(encoding="utf-8"))


def _non_docstring_strings(tree: ast.AST) -> list[str]:
    """Every string constant that is NOT a docstring — a SQL text lives here, prose does not."""
    doc_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.body and isinstance(node.body[0], ast.Expr) \
                    and isinstance(node.body[0].value, ast.Constant) \
                    and isinstance(node.body[0].value.value, str):
                doc_ids.add(id(node.body[0].value))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in doc_ids:
            out.append(node.value)
    return out


def _func(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in saas/bench_selftest.py")


def test_bw3_no_second_copy_of_the_status_poll():
    tree = _module_ast()
    names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "_schema_exists" not in names, "the schema-exists probe is the wrong signal; delete, do not keep"
    sql = [s for s in _non_docstring_strings(tree)
           if "user_provisioning" in s or "information_schema" in s]
    assert not sql, f"bench_selftest re-implements the provisioning read: {sql}"


def test_bw3_wait_imports_both_halves_of_the_seam_and_has_no_settle_sleep():
    """INVARIANT (re-pinned when the twin lane claims-harness-ready-wait lifted the loop): the
    bench's wait owns NO loop and NO sleep — it supplies its PROBE (the in-process reader) to the
    ONE shared ``provisioning_status.wait_until_ready`` and brands the raise ``SandboxNotReady``;
    the shared function is the one place that imports the MCP's ``_classify_probe_body``, has
    exactly one poll-cadence sleep, and returns only on the classifier's ``ready``."""
    tree = _module_ast()
    fn = _func(tree, "_wait_provisioned")
    imports = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.ImportFrom):
            imports.setdefault(node.module, set()).update(a.name for a in node.names)
    got = imports.get("src.provisioning.provisioning_status", set())
    assert {"check_provisioning_status", "wait_until_ready"} <= got, imports
    # the CODE of the adapter (its docstring, which records the wound + names the seam, is not
    # under census)
    src = "\n".join(ast.get_source_segment(BENCH_SRC.read_text(encoding="utf-8"), st) for st in fn.body[1:])
    assert "time.sleep" not in src and "_schema_exists" not in src and "_classify_probe_body" not in src
    # NO sleep of any kind in the adapter: the cadence lives in the shared loop
    sleeps = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute) and n.func.attr == "sleep"]
    assert not sleeps, "the bench adapter must not carry its own poll/settle sleep"
    # exactly one call into the shared wait, carrying the bench's knob + brand
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and ((isinstance(n.func, ast.Name) and n.func.id == "wait_until_ready")
                  or (isinstance(n.func, ast.Attribute) and n.func.attr == "to_thread"
                      and n.args and isinstance(n.args[0], ast.Name) and n.args[0].id == "wait_until_ready"))]
    assert len(calls) == 1, ast.dump(fn)
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert isinstance(kw.get("poll_s"), ast.Name) and kw["poll_s"].id == "_PROVISION_POLL_S", kw
    assert isinstance(kw.get("not_ready"), ast.Name) and kw["not_ready"].id == "SandboxNotReady", kw
    # the shared loop: one sleep on the cadence, return gated on the classifier's ready
    shared_src_path = REPO / "src" / "provisioning" / "provisioning_status.py"
    shared_tree = ast.parse(shared_src_path.read_text(encoding="utf-8"))
    wfn = _func(shared_tree, "wait_until_ready")
    wimports = {}
    for node in ast.walk(wfn):
        if isinstance(node, ast.ImportFrom):
            wimports.setdefault(node.module, set()).update(a.name for a in node.names)
    assert "_classify_probe_body" in wimports.get("src.mcp.server", set()), wimports
    wsrc = ast.get_source_segment(shared_src_path.read_text(encoding="utf-8"), wfn)
    wsleeps = [n for n in ast.walk(wfn) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Name) and n.func.id == "sleep"]
    assert len(wsleeps) == 1 and isinstance(wsleeps[0].args[0], ast.Name) \
        and wsleeps[0].args[0].id == "poll_s", "one poll-cadence sleep, no settle heuristic"
    assert 'kind == "ready"' in wsrc and "raise not_ready(" in wsrc


def test_bw3_call_site_keeps_the_180s_budget_and_lets_the_raise_propagate():
    tree = _module_ast()
    fn = _func(tree, "run_selftest")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_wait_provisioned"]
    assert len(calls) == 1
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert isinstance(kw.get("timeout"), ast.Constant) and kw["timeout"].value == 180
    # the call is NOT wrapped in a try that would swallow SandboxNotReady into a dirty run
    for node in ast.walk(fn):
        if isinstance(node, ast.Try):
            for h in node.handlers:
                for inner in ast.walk(h):
                    pass
            body_src = ast.get_source_segment(BENCH_SRC.read_text(encoding="utf-8"), node)
            if "_wait_provisioned(" in body_src:
                # the enclosing try is the teardown try/finally — it must have NO except handlers
                assert not node.handlers, "SandboxNotReady must propagate — never caught into a claim loop"


def test_bw3_reset_docstring_no_longer_tolerates_a_missing_schema():
    from saas import bench_selftest as B
    doc = inspect.getdoc(B._reset_sandbox) or ""
    assert "not-yet-provisioned schema is tolerated" not in doc
    assert "status='ready'" in doc
