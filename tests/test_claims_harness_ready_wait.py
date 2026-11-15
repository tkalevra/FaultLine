"""Gauntlet lane ``claims-harness-ready-wait`` — the laptop-side CLAIMS harness waits on
provisioning ``status='ready'`` through the ONE shared wait, never on the schema's existence, and
never continues past a timeout into a dirty-seat baseline.

THE WOUND (twin of ``bench-sandbox-ready-wait``, CLEAN r1): ``benchmarks/harness/run_claims.py::
_wait_provisioned`` polled ``information_schema.schemata`` over ``docker exec psql`` (a 6 s ssh
cadence), ``time.sleep(3)``'d once the SCHEMA existed, and on timeout printed a WARNING and
CONTINUED ("first claim ingest will provision"). ``schema_manager.create_user_schema`` commits
``CREATE SCHEMA`` first, applies the table template afterwards (tens of seconds on the pre-prod
ZFS pool) and commits ``status='ready'`` LAST — so the schema is visible seconds before its
tables, the first claim's ``reset_tenant`` TRUNCATE lands on a seat with no tables, and the
timeout branch publishes a CLAIMS baseline from a seat nobody confirmed was provisioned at all.

Pins:
  CW1  the old shape, DRIVEN: schema present + tables absent → the old probe returns True (after
       its 3 s settle) and the harness proceeds — a TRUNCATE then raises
       ``psycopg2.errors.UndefinedTable`` by identity; with the schema absent the old wait returns
       False and CONTINUES. Against a throwaway local Postgres when ``GAUNTLET_BENCH_TEST_DSN``
       names one (NEVER the db named ``faultline``), and always against a psql-seam stub.
  CW2  the new wait consumes the provisioning-status seam through the door this harness has —
       ``GET /provisioning/status`` over HTTP (the backend beside the MCP, port 8000; the
       DOC_PENDING_URL precedent) — via the ONE shared ``provisioning_status.wait_until_ready``
       (classified by the MCP's own ``_classify_probe_body``): returns ONLY on ``ready``, keeps
       polling through ``not_found`` / a 5xx / a refused connection / a non-JSON body, raises at
       once on a TERMINAL row, raises loud at the 180 s budget (no ``continue``), has no settle
       sleep, and there is NO second copy of the loop anywhere (AST census across src/, saas/,
       benchmarks/harness/, tools/): both harnesses call the same function.
  CW3  revert-red is the fltest run recorded in ``round_1/revert_red.txt`` (run_claims.py
       restored to HEAD; the shared function and this file unchanged).
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import psycopg2
import psycopg2.errors
import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
HARNESS = REPO / "benchmarks" / "harness"
CLAIMS_SRC = HARNESS / "run_claims.py"
BENCH_SRC = REPO / "saas" / "bench_selftest.py"
SHARED_SRC = REPO / "src" / "provisioning" / "provisioning_status.py"

_DSN = os.environ.get("GAUNTLET_BENCH_TEST_DSN", "")


def _import_run_claims():
    if str(HARNESS) not in sys.path:
        sys.path.insert(0, str(HARNESS))
    import run_claims as R  # noqa: E402  (sets two harmless env defaults at import; no network)
    return R


def _dsn_or_skip() -> str:
    """The DB-driven pins need a THROWAWAY database. Refuse anything that smells like a real
    inventory or a remote box — this lane is LOCAL ONLY and never the db named ``faultline``."""
    if not _DSN:
        pytest.skip("GAUNTLET_BENCH_TEST_DSN unset — export a THROWAWAY local Postgres DSN "
                    "(e.g. …/gauntlet_bench_test) to drive CW1/CW2 against a real database; "
                    "the seam-stub pins below still run")
    dbname = _DSN.rsplit("/", 1)[-1].split("?", 1)[0]
    assert dbname not in ("faultline", "faultline_test", "postgres"), \
        f"refusing to drive a destructive pin against db {dbname!r}"
    for banned in ("truenas", "faultline.ca", "198.51.100.10", "faultline-prod"):
        assert banned not in _DSN, f"refusing a non-local DSN ({banned})"
    return _DSN


# ═══════════════════════════════════════════════════════════════════════════════════════
# Fixtures — a throwaway seat row, and a local HTTP door that serves /provisioning/status
# ═══════════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def db_seat(monkeypatch):
    """A seat whose SCHEMA exists but whose TABLES do not — exactly the window the old wait
    returned in — plus the ``public.user_provisioning`` row the status seam reads. Torn down."""
    dsn = _dsn_or_skip()
    monkeypatch.setenv("POSTGRES_DSN", dsn)
    seat = str(uuid.uuid4())
    schema = "faultline_" + seat.replace("-", "_")
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(f"CREATE SCHEMA {schema}")  # the state after schema_manager's FIRST commit
    cur.execute("CREATE TABLE IF NOT EXISTS public.user_provisioning ("
                "user_id text primary key, status text, schema_name text, error_message text, "
                "ready_at timestamptz)")
    cur.execute("INSERT INTO public.user_provisioning (user_id, status, schema_name) "
                "VALUES (%s, 'provisioning', %s)", (seat, schema))
    try:
        yield SimpleNamespace(conn=conn, cur=cur, seat=seat, schema=schema, dsn=dsn)
    finally:
        cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (seat,))
        conn.close()


class _Door:
    """A local HTTP door serving ``GET /provisioning/status`` the way src/api/main.py does —
    either a SCRIPTED sequence of bodies (the seam-stub pins) or the REAL reader
    ``check_provisioning_status`` against the throwaway database (the driven pin). Records every
    request's query string so a pin can assert the harness asked for the right seat."""

    def __init__(self, answers=None, real=False):
        self.answers = list(answers or [])
        self.real = real
        self.calls = 0
        self.seen: list[dict] = []
        door = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def do_GET(self):
                u = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                door.calls += 1
                door.seen.append({"path": u.path, "query": q,
                                  "hdr_user": self.headers.get("X-OpenWebUI-User-Id")})
                if u.path != "/provisioning/status":
                    self.send_response(404); self.end_headers(); return
                if door.real:
                    from src.provisioning import provisioning_status as PS
                    body = PS.check_provisioning_status(q.get("user_id", ""))
                    self._json(200, body)
                    return
                a = door.answers[min(door.calls, len(door.answers)) - 1]
                if isinstance(a, tuple):          # (status_code, raw_bytes)
                    code, raw = a
                    self.send_response(code); self.end_headers(); self.wfile.write(raw); return
                self._json(200, dict(a, user_id=q.get("user_id", "")) if "user_id" not in a else a)

            def _json(self, code, body):
                raw = json.dumps(body, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def door(monkeypatch):
    """A scripted door; the harness is pointed at it through BENCH_API_URL (the explicit
    override) with a fast poll cadence."""
    d = _Door()
    monkeypatch.setenv("BENCH_API_URL", d.url)
    R = _import_run_claims()
    monkeypatch.setattr(R, "PROVISION_POLL_S", 0.01, raising=False)   # raising=False: the reverted
    # module lacks the knob — the revert-red must show the BEHAVIOUR, not a setup AttributeError
    monkeypatch.setattr(R, "PROVISION_PROBE_TIMEOUT", 2.0, raising=False)
    # the fixed wait bound the real time.sleep at definition time (unaffected); HEAD's wait looks
    # it up per call — shrink it so a reverted run reds on behaviour inside the same budget
    monkeypatch.setattr(time, "sleep", lambda s: _real_sleep(min(s, 0.01)))
    d.R = R
    try:
        yield d
    finally:
        d.close()


_OWUI = SimpleNamespace(MCP_RECALL_URL="http://truenas:8002/recall_memory")
_SEAT = "5f6d9dce-396f-414e-bac4-bbec109969aa"
_SCHEMA = "faultline_5f6d9dce_396f_414e_bac4_bbec109969aa"


def _capture(monkeypatch, R) -> list[str]:
    lines: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: lines.append(" ".join(str(x) for x in a)))
    return lines


_real_sleep = time.sleep


class _RlStub:
    """HEAD's psql plumbing with the catalog answering 'no such schema' — what the OLD wait sees
    on a seat whose provisioning is still running."""
    PG_CONTAINER, PG_USER, PG_DB = "faultline-postgres", "faultline", "gauntlet_bench_test"

    def __init__(self, seat):
        self.BENCH_SCHEMA = "faultline_" + seat.replace("-", "_")

    def _docker(self, args):
        return ["docker"] + args

    def _run_cmd(self, cmd, timeout=120):
        return SimpleNamespace(stdout="", stderr="", returncode=0)


def _wait(R, seat, timeout, rl=None):
    """Call the harness wait by whichever signature the module carries — the fixed
    ``(owui, seat, timeout)`` or HEAD's ``(rl, timeout)`` — so a revert reds on the BEHAVIOUR
    (the old wait RETURNS instead of raising) and not on a signature TypeError."""
    import inspect
    params = list(inspect.signature(R._wait_provisioned).parameters)
    if params and params[0] == "rl":
        return R._wait_provisioned(rl or _RlStub(seat), timeout=timeout)
    return R._wait_provisioned(_OWUI, seat, timeout=timeout)


# ═══════════════════════════════════════════════════════════════════════════════════════
# CW1 — the old shape, driven
# ═══════════════════════════════════════════════════════════════════════════════════════

def _old_wait_provisioned(rl, timeout=180):
    """The wait run_claims.py carried at HEAD^ (kept HERE, in the test, as the record of the
    wrong signal + the settle + the CONTINUE — it must not exist in the module, see CW2).
    Byte-for-byte the shipped algorithm: schema-exists over ``rl._run_cmd(rl._docker(psql …))``,
    ``time.sleep(3)`` once present, ``time.sleep(6)`` between polls, and a WARNING + ``return
    False`` at the budget that the caller ignored."""
    sql = ("SELECT 1 FROM information_schema.schemata WHERE schema_name = "
           f"'{rl.BENCH_SCHEMA}'")
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = rl._run_cmd(rl._docker(
            ["exec", rl.PG_CONTAINER, "psql", "-U", rl.PG_USER, "-d", rl.PG_DB,
             "-P", "pager=off", "-tAc", sql]))
        if (r.stdout or "").strip().startswith("1"):
            print(f"[provision] seat schema {rl.BENCH_SCHEMA} present.", flush=True)
            time.sleep(3)
            return True
        time.sleep(6)
    print(f"[provision] WARNING: schema not confirmed after {timeout}s — continuing (first "
          f"claim ingest will provision).", flush=True)
    return False


class _RlOverDb:
    """The ``run_longmemeval`` plumbing the old wait leaned on, with ``docker exec psql`` replaced
    by the SAME catalog query against the throwaway database — the real answer, not a stub."""
    PG_CONTAINER, PG_USER, PG_DB = "faultline-postgres", "faultline", "gauntlet_bench_test"

    def __init__(self, dsn, schema):
        self.dsn, self.BENCH_SCHEMA = dsn, schema
        self.sql_seen: list[str] = []

    def _docker(self, args):
        return ["docker"] + args

    def _run_cmd(self, cmd, timeout=120):
        sql = cmd[-1]
        self.sql_seen.append(sql)
        c = psycopg2.connect(self.dsn)
        try:
            with c.cursor() as k:
                k.execute(sql)
                row = k.fetchone()
        finally:
            c.close()
        return SimpleNamespace(stdout=("1\n" if row else ""), stderr="", returncode=0)


def test_cw1_db_schema_present_tables_absent_old_wait_returns_true_and_a_reset_raises_undefined_table(
        db_seat, monkeypatch):
    cur = db_seat.cur
    cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = %s",
                (db_seat.schema,))
    assert cur.fetchone()[0] == 0, "fixture: the schema must carry NO tables"
    rl = _RlOverDb(db_seat.dsn, db_seat.schema)
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    lines = _capture(monkeypatch, None)
    # (a) the OLD wait returns True in this state — the exact window the wound lives in — after
    #     its blind 3 s settle, on the strength of the catalog alone
    assert _old_wait_provisioned(rl, timeout=5) is True
    assert slept == [3], slept
    assert any("present" in l for l in lines), lines
    assert rl.sql_seen and "information_schema.schemata" in rl.sql_seen[0]
    # (b) what the harness then does first — reset_tenant's TRUNCATE — raises UndefinedTable,
    #     pinned by exception IDENTITY (SQLSTATE 42P01 → psycopg2.errors.UndefinedTable)
    with pytest.raises(psycopg2.errors.UndefinedTable):
        with psycopg2.connect(db_seat.dsn) as c2, c2.cursor() as k:
            k.execute(f"TRUNCATE {db_seat.schema}.facts RESTART IDENTITY CASCADE")
    # (c) the NEW wait, on the REAL row through the REAL reader behind a local HTTP door, does
    #     NOT return in this state: the row says 'provisioning'. (Reverted module: the old wait
    #     has no HTTP door at all — see revert_red.txt for how it reds.)
    d = _Door(real=True)
    try:
        monkeypatch.setenv("BENCH_API_URL", d.url)
        R = _import_run_claims()
        monkeypatch.setattr(R, "PROVISION_POLL_S", 0.05, raising=False)
        # (the shared wait bound the REAL time.sleep as its default at definition time — the
        # recorder patched above only reaches the old wait's call-time lookup, so a reverted
        # module returns True here in ~0 s → DID NOT RAISE, the wound itself)
        with pytest.raises(Exception) as ei:
            _wait(R, db_seat.seat, 0.3, rl=rl)
        assert type(ei.value).__name__ == "ProvisioningNotReady", repr(ei.value)
        assert ei.value.last_status == "provisioning" and "no claim scored" in str(ei.value)
        assert d.calls >= 3 and all(s["query"].get("user_id") == db_seat.seat for s in d.seen), d.seen
        # (d) the row flips to ready (the LAST write of create_user_schema) → the wait returns the
        #     row's schema; a TERMINAL row → raises at once, not after the budget
        cur.execute("UPDATE public.user_provisioning SET status='ready', ready_at=now() "
                    "WHERE user_id=%s", (db_seat.seat,))
        assert _wait(R, db_seat.seat, 5.0, rl=rl) == db_seat.schema
        cur.execute("UPDATE public.user_provisioning SET status='error', "
                    "error_message='provisioning failed' WHERE user_id=%s", (db_seat.seat,))
        t0 = time.monotonic()
        with pytest.raises(Exception) as ei:
            _wait(R, db_seat.seat, 30.0, rl=rl)
        assert type(ei.value).__name__ == "ProvisioningNotReady"
        assert time.monotonic() - t0 < 2.0 and "TERMINAL" in str(ei.value)
    finally:
        d.close()


def test_cw1_db_schema_absent_old_wait_times_out_and_continues(db_seat, monkeypatch):
    """The second half of the wound: at the budget the old wait returned False with a WARNING —
    and main() ignored the return, so the claims ran on a seat nobody had confirmed."""
    db_seat.cur.execute(f"DROP SCHEMA {db_seat.schema} CASCADE")
    rl = _RlOverDb(db_seat.dsn, db_seat.schema)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    lines = _capture(monkeypatch, None)
    assert _old_wait_provisioned(rl, timeout=0.2) is False          # no raise: the harness proceeds
    assert any("WARNING" in l and "continuing" in l for l in lines), lines


def test_cw1_stub_psql_seam_old_wait_returns_on_the_catalog_alone(monkeypatch):
    """The same shape with the psql seam stubbed: '1' from the catalog is all the old wait ever
    asked for — no table check, no status read — then a fixed settle and True."""
    rl = SimpleNamespace(BENCH_SCHEMA=_SCHEMA, PG_CONTAINER="pg", PG_USER="u", PG_DB="gauntlet_bench_test",
                         _docker=lambda args: ["docker"] + args,
                         _run_cmd=lambda cmd, timeout=120: SimpleNamespace(stdout="1\n"))
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    _capture(monkeypatch, None)
    assert _old_wait_provisioned(rl, timeout=5) is True and slept == [3]


# ═══════════════════════════════════════════════════════════════════════════════════════
# CW2 — the new wait: the shared function, through the HTTP door
# ═══════════════════════════════════════════════════════════════════════════════════════

def test_cw2_returns_only_on_ready_and_asks_the_backend_door_for_this_seat(door, monkeypatch):
    door.answers = [{"status": "provisioning", "schema_name": _SCHEMA},
                    {"status": "provisioning", "schema_name": _SCHEMA},
                    {"status": "ready", "schema_name": _SCHEMA, "ready_at": "2026-09-15T00:00:00"}]
    lines = _capture(monkeypatch, door.R)
    out = _wait(door.R, _SEAT, 5.0)
    assert out == _SCHEMA
    assert door.calls == 3, "returned before the seam said ready"
    assert all(s["path"] == "/provisioning/status" and s["query"] == {"user_id": _SEAT}
               and s["hdr_user"] == _SEAT for s in door.seen), door.seen
    assert any("ready" in l and "attempt 3" in l for l in lines), lines


def test_cw2_keeps_polling_through_not_found_5xx_4xx_refused_and_non_json(door, monkeypatch):
    """None of these is a verdict on the seat: ``not_found`` (row not visible yet), an HTTP 503,
    an HTTP 404 / 401 / 403 from the door (e.g. BENCH_API_URL mispointed at the MCP on 8002, which
    has no /provisioning/status — critic mutation M4b: a probe that reads a 4xx as ready), a
    non-JSON body, and the seam's own except-dict. Every non-2xx is UNREACHABLE: keep polling."""
    door.answers = [{"status": "not_found"},
                    (503, b"upstream busy"),
                    (404, b'{"detail":"Not Found"}'),
                    (401, b'{"detail":"Unauthorized"}'),
                    (403, b'{"detail":"Forbidden"}'),
                    (200, b"<html>not json</html>"),
                    {"status": "error", "error_message": "Failed to check status",
                     "correlation_id": "abc123def456"},
                    {"status": "ready", "schema_name": _SCHEMA}]
    _capture(monkeypatch, door.R)
    assert _wait(door.R, _SEAT, 5.0) == _SCHEMA
    assert door.calls == 8, "returned before the door answered ready"


@pytest.mark.parametrize("code", [404, 401, 403, 500])
def test_cw2_a_door_that_only_answers_non_2xx_is_never_ready_and_raises_loud_at_the_budget(
        door, monkeypatch, code):
    """A mispointed or refusing door (4xx/5xx forever) is UNREACHABLE, not ready and not terminal:
    the wait polls to the budget and raises naming the last answer — never a fabricated ready."""
    door.answers = [(code, b'{"detail":"nope"}')]
    lines = _capture(monkeypatch, door.R)
    with pytest.raises(Exception) as ei:
        _wait(door.R, _SEAT, 0.08)
    assert type(ei.value).__name__ == "ProvisioningNotReady", repr(ei.value)
    assert door.calls >= 3
    assert ei.value.last_status == "error" and "unreachable" in str(ei.value) and f"HTTP {code}" in str(ei.value)
    assert any("not ready after" in l for l in lines), lines


def test_cw2_a_refused_connection_is_unreachable_not_ready_and_not_terminal(monkeypatch):
    """A closed port (nothing listening) → the probe raises → the shared wait classifies it
    UNREACHABLE and keeps polling to the budget → loud raise; never 'ready', never a continue."""
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    monkeypatch.setenv("BENCH_API_URL", f"http://127.0.0.1:{port}")
    R = _import_run_claims()
    monkeypatch.setattr(R, "PROVISION_POLL_S", 0.01, raising=False)
    monkeypatch.setattr(time, "sleep", lambda s: _real_sleep(min(s, 0.01)))
    lines = _capture(monkeypatch, R)
    with pytest.raises(Exception) as ei:
        _wait(R, _SEAT, 0.15)
    assert type(ei.value).__name__ == "ProvisioningNotReady", repr(ei.value)
    assert ei.value.last_status == "error" and "unreachable" in str(ei.value)
    assert any("not ready after" in l for l in lines), lines


def test_cw2_terminal_row_raises_at_once_not_after_the_budget(door, monkeypatch):
    door.answers = [{"status": "error", "schema_name": _SCHEMA,
                     "error_message": "provisioning failed", "correlation_id": "c0ffee00c0de"}]
    lines = _capture(monkeypatch, door.R)
    t0 = time.monotonic()
    with pytest.raises(Exception) as ei:
        _wait(door.R, _SEAT, 30.0)   # 30 s budget; must not be waited
    assert type(ei.value).__name__ == "ProvisioningNotReady", repr(ei.value)
    assert time.monotonic() - t0 < 1.0 and door.calls == 1
    msg = str(ei.value)
    assert "TERMINAL" in msg and "'error'" in msg and "c0ffee00c0de" in msg and "no claim scored" in msg
    assert ei.value.public_reason == msg and ei.value.correlation_id == "c0ffee00c0de"
    assert any("TERMINAL" in l for l in lines), "the reason is printed, not only raised"


@pytest.mark.parametrize("status", ["failed", "wedged"])
def test_cw2_failed_and_unknown_statuses_are_terminal(door, monkeypatch, status):
    door.answers = [{"status": status, "schema_name": _SCHEMA}]
    _capture(monkeypatch, door.R)
    t0 = time.monotonic()
    with pytest.raises(Exception) as ei:
        _wait(door.R, _SEAT, 30.0)
    assert type(ei.value).__name__ == "ProvisioningNotReady" and time.monotonic() - t0 < 1.0
    assert door.calls == 1


def test_cw2_budget_exhausted_raises_loud_never_continues(door, monkeypatch):
    door.answers = [{"status": "provisioning", "schema_name": _SCHEMA}]
    lines = _capture(monkeypatch, door.R)
    t0 = time.monotonic()
    with pytest.raises(Exception) as ei:       # reverted module: returns False + WARNING → DID NOT RAISE
        _wait(door.R, _SEAT, 0.08)
    assert type(ei.value).__name__ == "ProvisioningNotReady", repr(ei.value)
    assert 0.05 <= time.monotonic() - t0 < 1.0
    assert door.calls >= 3
    msg = str(ei.value)
    assert "not ready after" in msg and "'provisioning'" in msg and "wait" in msg and "no claim scored" in msg
    assert not any("continuing" in l for l in lines), lines
    assert any("not ready after" in l for l in lines), lines


def test_cw2_backend_door_is_the_mcp_host_on_8000_unless_overridden(monkeypatch):
    R = _import_run_claims()
    monkeypatch.delenv("BENCH_API_URL", raising=False)
    assert R.backend_api_url(_OWUI) == "http://truenas:8000"
    assert R.backend_api_url(SimpleNamespace(MCP_RECALL_URL="http://localhost:8002/recall_memory")) \
        == "http://localhost:8000"
    monkeypatch.setenv("BENCH_API_URL", "http://10.0.0.5:8000/")
    assert R.backend_api_url(_OWUI) == "http://10.0.0.5:8000"


# ── the shared function itself, with an injected clock: bound, cadence, brand ─────────────

def test_cw2_shared_wait_is_bounded_by_the_budget_and_brands_the_raise():
    from src.provisioning.provisioning_status import ProvisioningNotReady, wait_until_ready
    now = [0.0]
    slept: list[float] = []

    def _sleep(s):
        slept.append(s); now[0] += s

    answers = iter([{"status": "not_found"}] * 100)
    with pytest.raises(ProvisioningNotReady) as ei:
        wait_until_ready(lambda: next(answers), seat_uuid=_SEAT, timeout=10.0, poll_s=3.0,
                         clock=lambda: now[0], sleep=_sleep)
    # probes at t=0,3,6,9 → the NEXT sleep would cross 10 → stop: never overshoots by a poll
    assert slept == [3.0, 3.0, 3.0] and now[0] == 9.0
    assert ei.value.last_status == "not_found" and "4 status probes" in str(ei.value)

    class Mine(ProvisioningNotReady):
        pass
    with pytest.raises(Mine):
        wait_until_ready(lambda: {"status": "failed", "schema_name": _SCHEMA}, seat_uuid=_SEAT,
                         timeout=10.0, poll_s=1.0, not_ready=Mine, clock=lambda: 0.0, sleep=_sleep)
    with pytest.raises(TypeError):
        wait_until_ready(lambda: {"status": "ready"}, seat_uuid=_SEAT, timeout=1.0, poll_s=1.0,
                         not_ready=RuntimeError)  # type: ignore[arg-type]
    # a probe that RAISES is unreachable (keep polling), and ready returns the body itself
    seq = iter([RuntimeError("boom"), {"status": "ready", "schema_name": _SCHEMA}])

    def _probe():
        a = next(seq)
        if isinstance(a, Exception):
            raise a
        return a
    body = wait_until_ready(_probe, seat_uuid=_SEAT, timeout=5.0, poll_s=0.0,
                            clock=lambda: 0.0, sleep=_sleep)
    assert body["schema_name"] == _SCHEMA


# ── census: no second copy, no settle, no continue, the call site is honest ─────────────

def _tree(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _func(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _non_docstring_strings(tree: ast.Module) -> list[str]:
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.body and isinstance(node.body[0], ast.Expr) \
                    and isinstance(node.body[0].value, ast.Constant) \
                    and isinstance(node.body[0].value.value, str):
                docs.add(id(node.body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]


def test_cw2_run_claims_carries_no_schema_probe_no_settle_and_no_continue():
    tree = _tree(CLAIMS_SRC)
    fn = _func(tree, "_wait_provisioned")
    text = CLAIMS_SRC.read_text(encoding="utf-8")
    # the CODE of the wait (its docstring, which records the wound, is not under census)
    src = "\n".join(ast.get_source_segment(text, st) for st in fn.body[1:])
    # no catalog probe, no psql, no sleep of any kind, no 'continuing' branch in the wait
    assert "information_schema" not in src and "psql" not in src and "_run_cmd" not in src
    sleeps = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
              and ((isinstance(n.func, ast.Attribute) and n.func.attr == "sleep")
                   or (isinstance(n.func, ast.Name) and n.func.id == "sleep"))]
    assert not sleeps, "the harness adapter must not carry its own poll/settle sleep"
    assert "continuing" not in src and "return False" not in src
    # the whole module: the old catalog SQL is gone from every non-docstring string
    leaked = [s for s in _non_docstring_strings(tree) if "information_schema.schemata" in s]
    assert not leaked, leaked
    # exactly one call into the shared wait, carrying the harness knob; the probe hits the door
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "wait_until_ready"]
    assert len(calls) == 1
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert isinstance(kw.get("poll_s"), ast.Name) and kw["poll_s"].id == "PROVISION_POLL_S", kw
    assert "_probe_provisioning_status(" in src
    probe = _func(tree, "_probe_provisioning_status")
    psrc = ast.get_source_segment(text, probe)
    assert "/provisioning/status" in psrc and "X-OpenWebUI-User-Id" in psrc


def test_cw2_call_site_keeps_the_180s_budget_and_lets_the_raise_propagate():
    tree = _tree(CLAIMS_SRC)
    fn = _func(tree, "main")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_wait_provisioned"]
    assert len(calls) == 1
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert isinstance(kw.get("timeout"), ast.Constant) and kw["timeout"].value == 180
    # the return is not consulted and the call is not wrapped in an except that would swallow it:
    # the enclosing try is main's teardown try/finally and must have NO handlers
    src_all = CLAIMS_SRC.read_text(encoding="utf-8")
    for node in ast.walk(fn):
        if isinstance(node, ast.Try) and "_wait_provisioned(" in ast.get_source_segment(src_all, node):
            assert not node.handlers, "ProvisioningNotReady must propagate — never caught into a claim loop"
    parent_stmt = [n for n in ast.walk(fn) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                   and n.value is calls[0]]
    assert parent_stmt, "the wait is called for its RAISE, its return is not a decision"


_CENSUS_ROOTS = ("src", "saas", "benchmarks/harness", "tools")
# the transport's own gate (async, turn-wall-bounded, per-turn cache) defines and consumes the
# classifier; the shared wait is the ONE harness loop; nothing else may read the classifier
_ALLOWED_CLASSIFIER_READERS = {
    ("src/mcp/server.py", "_classify_probe_body"),          # the definition
    ("src/mcp/server.py", "_ensure_provisioned"),           # the MCP readiness gate
    ("src/provisioning/provisioning_status.py", "wait_until_ready"),
}


def _enclosing_func_names(tree: ast.Module):
    """Map every node id → the name of the innermost enclosing def (module level → '<module>')."""
    owner = {}

    def walk(node, cur):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            cur = node.name
        owner[id(node)] = cur
        for child in ast.iter_child_nodes(node):
            walk(child, cur)
    walk(tree, "<module>")
    return owner


def test_cw2_one_loop_census_no_second_copy_across_the_tree():
    """Every reader of ``_classify_probe_body`` and every caller of ``wait_until_ready`` in the
    tree, by (file, function): the classifier is read in exactly three places, and BOTH harnesses
    call the shared wait — neither carries a loop of its own."""
    readers, callers = set(), set()
    for root in _CENSUS_ROOTS:
        for path in (REPO / root).rglob("*.py"):
            rel = str(path.relative_to(REPO))
            if "/tests/" in rel or rel.startswith("tests/"):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            owner = _enclosing_func_names(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "_classify_probe_body":
                    readers.add((rel, node.name))          # the definition itself
                if isinstance(node, ast.Name) and node.id == "_classify_probe_body":
                    readers.add((rel, owner[id(node)]))
                if isinstance(node, ast.ImportFrom) and any(a.name == "_classify_probe_body" for a in node.names):
                    readers.add((rel, owner[id(node)]))
                if isinstance(node, ast.Attribute) and node.attr == "_classify_probe_body":
                    readers.add((rel, owner[id(node)]))
                if isinstance(node, ast.Call):
                    f = node.func
                    if (isinstance(f, ast.Name) and f.id == "wait_until_ready") or \
                            (isinstance(f, ast.Attribute) and f.attr == "wait_until_ready"):
                        callers.add((rel, owner[id(node)]))
                    if isinstance(f, ast.Attribute) and f.attr == "to_thread" and node.args \
                            and isinstance(node.args[0], ast.Name) and node.args[0].id == "wait_until_ready":
                        callers.add((rel, owner[id(node)]))
    # `_ensure_provisioned` reads it inside a nested `_probe`; fold nested names onto their owner
    readers = {(f, "_ensure_provisioned" if (f == "src/mcp/server.py" and n == "_probe") else n)
               for f, n in readers}
    assert readers == _ALLOWED_CLASSIFIER_READERS, sorted(readers)
    assert callers == {("benchmarks/harness/run_claims.py", "_wait_provisioned"),
                       ("saas/bench_selftest.py", "_wait_provisioned")}, sorted(callers)
    # and neither harness re-implements the reader's SQL
    for path in (CLAIMS_SRC, BENCH_SRC):
        sql = [s for s in _non_docstring_strings(_tree(path))
               if "user_provisioning" in s or "information_schema" in s]
        assert not sql, f"{path.name} re-implements the provisioning read: {sql}"
