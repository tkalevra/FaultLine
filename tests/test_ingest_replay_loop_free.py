"""ID2 / ID3 / ID4 pins — `/ingest` can never hold the API's event loop hostage.

THE WOUND (gauntlet ingest-replay-loop-deadlock, 2026-09-15, round_1/repro.txt): after
`docker unpause faultline-api`, the MCP write seam's byte-identical `/ingest` replays froze the
API event loop until `docker restart` — /health dead for the whole window, pg_stat_activity
showing ONE `idle in transaction` holder (request A, suspended at `await asyncio.to_thread(
registry.resolve, ...)` with its transaction and row locks still open, its continuation queued
on a loop that never runs) and ONE `active` waiter (request B, a synchronous
`INSERT ... ON CONFLICT DO UPDATE` on the loop thread, `Lock/transactionid` blocked_by A),
`lock_timeout=0` so nothing ever broke the cycle.

Why each pin observes what it observes (evidence-grounded, primary sources):

* "While a Task is running in the event loop, no other Tasks can run in the same thread" and
  blocking code "should not be called directly ... all concurrent asyncio Tasks and IO operations
  would be delayed" — Python docs, asyncio-dev.html ("Running Blocking Code",
  "Concurrency and Multithreading"). So a /health probe task IS the instrument: if the loop is
  held, /health cannot answer. ID2 measures that under a replay burst.
* psycopg2 without a wait callback issues "the regular blocking" libpq calls
  (psycopg.org/docs/advanced.html#support-for-coroutine-libraries) and "By default, the first
  time a command is sent to the database ... a new transaction is created ... The connection is
  responsible for terminating its transaction, calling either the commit() or rollback() method"
  (psycopg.org/docs/usage.html#transactions-control). Hence the ID3 census: an `await` while a
  connection is STATUS_IN_TRANSACTION *and that transaction was opened on the loop thread* is a
  transaction whose completion depends on the loop — the exact hazard. The transactional body
  now runs in one worker thread (`main._ingest_impl::_ingest_txn_body`); a transaction opened and
  finished on a worker thread while the coroutine awaits the thread's completion is the SAFE
  shape and is deliberately not counted.
* PostgreSQL `lock_timeout`: "Abort any statement that waits longer than the specified amount of
  time while attempting to acquire a lock on a table, index, row, or other database object ...
  A value of zero (the default) disables the timeout" (runtime-config-client.html); error
  condition 55P03 `lock_not_available` (errcodes-appendix.html) →
  `psycopg2.errors.LockNotAvailable` (psycopg.org/docs/errors.html). ID4 forces a row lock from an
  external session and expects the bounded 503 + CRIT, never a hang.

DISCIPLINE. A real local Postgres (POSTGRES_DSN, database name must contain "test" — never the
owner's inventory), a real provisioned tenant schema, the real FastAPI app over an in-process
ASGI transport (the /health probe shares the loop with the ingests, which is the point). The
only fakes are the brain seams (no LLM in a determinism pin) and the idempotency cache backend
when the local Redis is unreachable (a dict-backed twin of the two methods the handler calls).
No personal data, no real seat ids: every user id is a fresh uuid4.

WATCHDOG. Under the pre-fix handler these tests would not go red — they would HANG, because a
frozen loop cannot even fire `asyncio.wait_for`. An out-of-loop `threading.Timer` therefore
cancels every tracked PostgreSQL backend (`connection.cancel()` uses libpq PQcancel on a
separate socket, so it works from another thread while the loop is blocked) after a bound,
which unblocks the loop and lets the assertion fail LOUD with the elapsed time (revert-red
evidence: round_1/revert_red.txt).
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import json
import os
import statistics
import threading
import time
import uuid

import httpx
import psycopg2
import psycopg2.extensions as pg_ext
import pytest

main = importlib.import_module("src.api.main")
idem_mod = importlib.import_module("src.api.idempotency")

_real_connect = psycopg2.connect  # captured BEFORE any monkeypatching (the patch is module-global)

HEALTH_BOUND_S = 1.0          # ID2: /health answers under load in < 1s
PROBE_PERIOD_S = 0.05
CALLER_WALL_S = 30.0          # the MCP write seam's per-attempt timeout (server.py::_ingest_with_retry)
WATCHDOG_S = 45.0             # revert-red bound: a frozen loop is cut here, not left hanging
_WARMED: list = []            # one-shot warm-up marker (see app_env)


# ─────────────────────────────────────────────────────────────────────────────
# environment
# ─────────────────────────────────────────────────────────────────────────────

def _dsn_or_skip(dsn: str | None) -> str:
    """`dsn` comes from conftest's session `postgres_dsn` fixture, captured BEFORE any test module
    is imported — extraction-layer modules `os.environ.pop("POSTGRES_DSN")` at import (see
    tests/conftest.py), so inside a batch the env value is gone by the time a fixture runs."""
    if not dsn:
        pytest.skip("POSTGRES_DSN not set/unreachable — these pins need a live throwaway Postgres")
    dbname = dsn.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in dbname:
        pytest.skip(f"refusing to run destructive pins against database {dbname!r} (name must contain 'test')")
    try:
        c = _real_connect(dsn, connect_timeout=3)
        c.close()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"POSTGRES_DSN unreachable: {type(e).__name__}: {str(e)[:120]}")
    return dsn


class _DictRedis:
    """Dict-backed twin of the two Redis calls IdempotencyManager makes on the /ingest path."""

    def __init__(self):
        self._d: dict = {}

    def get(self, k):
        return self._d.get(k)

    def setex(self, k, ttl, v):
        self._d[k] = v
        return True

    def ping(self):
        return True


def _idempotency_manager() -> tuple[object, str]:
    """Real Redis when the local stack's container answers, else the dict twin. Says which."""
    url = os.environ.get("REDIS_URL")
    candidates = [url] if url else []
    candidates += ["redis://127.0.0.1:6379/0"]
    try:  # the docker bridge IP, when docker is here
        import subprocess
        ip = subprocess.run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                             "faultline-redis"], capture_output=True, text=True, timeout=5).stdout.strip()
        if ip:
            candidates.append(f"redis://{ip}:6379/0")
    except Exception:
        pass
    for cand in candidates:
        try:
            mgr = idem_mod.IdempotencyManager(redis_url=cand)
            if mgr.client is not None:
                mgr.client.ping()
                return mgr, f"redis:{cand}"
        except Exception:
            continue
    mgr = idem_mod.IdempotencyManager.__new__(idem_mod.IdempotencyManager)
    mgr.redis_url = "dict://twin"
    mgr.ttl = 3600
    mgr.client = _DictRedis()
    return mgr, "dict-twin (no reachable Redis)"


@pytest.fixture(scope="module")
def tenant(postgres_dsn):
    """A REAL provisioned tenant schema in the throwaway database (template + seeds)."""
    dsn = _dsn_or_skip(postgres_dsn)
    from src.provisioning.schema_manager import (create_user_schema, derive_user_slug_from_uuid,
                                                 derive_schema_name)
    user_id = str(uuid.uuid4())
    slug = derive_user_slug_from_uuid(user_id)
    # create_user_schema reads os.environ["POSTGRES_DSN"] itself; pin it for the provisioning
    # call (module scope has no monkeypatch — restore by hand).
    prior = os.environ.get("POSTGRES_DSN")
    os.environ["POSTGRES_DSN"] = dsn
    db = _real_connect(dsn)
    try:
        schema, status = create_user_schema(user_id, slug, db)
    finally:
        db.close()
        if prior is None:
            os.environ.pop("POSTGRES_DSN", None)
        else:
            os.environ["POSTGRES_DSN"] = prior
    assert status == "ready", status
    assert schema == derive_schema_name(slug)
    yield {"user_id": user_id, "schema": schema, "dsn": dsn}
    c = _real_connect(dsn)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            cur.execute(f'DROP SCHEMA IF EXISTS "flagent_{user_id.replace("-", "_")}" CASCADE')
            cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM public.users WHERE user_id = %s", (user_id,))
    finally:
        c.close()


@pytest.fixture
def app_env(monkeypatch, tenant):
    """Wire the app for a hermetic, brain-free ingest against the provisioned tenant."""
    monkeypatch.setenv("POSTGRES_DSN", tenant["dsn"])
    monkeypatch.setenv("SPACY_MODEL", os.environ.get("SPACY_MODEL", "en_core_web_sm"))
    monkeypatch.setattr(main, "_INGEST_ENABLED", True, raising=False)
    main._TENANT_READY_CACHE.add(tenant["user_id"])
    mgr, backend = _idempotency_manager()
    monkeypatch.setattr(main, "_idempotency_mgr", mgr, raising=False)
    # Brain seams: a determinism pin never talks to an LLM. Each stub is the seam's documented
    # fail-safe value (empty), so the deterministic lanes run exactly as on a brain-less box.
    async def _no_residue(*a, **k):
        return []
    monkeypatch.setattr(main, "_classify_occurrence_residue", _no_residue, raising=False)
    monkeypatch.setattr(main, "_resolve_atomic_subjects_via_llm", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(main, "generate_rel_type_phrasing", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(main, "generate_rel_type_phrasing_batch", lambda *a, **k: {}, raising=False)
    monkeypatch.setattr(main, "paused_for_background", lambda: False, raising=False)
    crits: list[dict] = []
    real_log_crit = main.log_crit

    def _rec_log_crit(logger, msg, **kw):
        crits.append({"event": msg, **kw})
        return real_log_crit(logger, msg, **kw)
    monkeypatch.setattr(main, "log_crit", _rec_log_crit)
    env = {**tenant, "idem": mgr, "idem_backend": backend, "crits": crits}
    # ONE warm-up ingest per process: the first-ever ingest pays lazy module imports (spaCy /
    # linguistics) on the loop — a boot-time cost the deployed process pays before it serves
    # (main.py::lifespan warms the models). The pins measure the SERVING state.
    if not _WARMED:
        async def _warm():
            async with _client() as client:
                w = f"wrm{uuid.uuid4().hex[:8]}"
                r = await client.post("/ingest", json=_body(env["user_id"], f"My sibling {w} lives in Fergus and owns a tin cup.",
                                                            _edges(w, "fergus", f"tin cup {w}")))
                assert r.status_code == 200, r.text[:300]
        _run(_warm())
        _WARMED.append(True)
    return env


def _body(user_id: str, text: str, edges: list[dict]) -> dict:
    return {"text": text, "user_id": user_id, "source": "mcp", "edges": edges}


def _edges(subject: str, place: str, thing: str) -> list[dict]:
    """Seeded rel_types only (lives_in / owns / sibling_of) so no growth lane needs a brain."""
    return [
        {"subject": subject, "rel_type": "sibling_of", "object": "user",
         "subject_type": "Person", "object_type": "Person", "fact_provenance": "user_stated"},
        {"subject": subject, "rel_type": "lives_in", "object": place,
         "subject_type": "Person", "object_type": "Location", "fact_provenance": "user_stated"},
        {"subject": subject, "rel_type": "owns", "object": thing,
         "subject_type": "Person", "object_type": "Object", "fact_provenance": "user_stated"},
    ]


def _cache_key(mgr, body: dict) -> str:
    """The handler's own key derivation (pydantic EdgeInput objects, not the wire dicts)."""
    from src.api.models import IngestRequest
    req = IngestRequest(**body)
    return mgr.generate_key(req.text, req.user_id, "/ingest", memory_facts=req.memory_facts,
                            is_correction=req.is_correction, edges=req.edges)


def _fact_rows(dsn: str, schema: str, subject_alias: str) -> list[tuple]:
    c = _real_connect(dsn)
    try:
        with c.cursor() as cur:
            # the subject may land as OBJECT: a symmetric rel whose object is the user is swapped
            # to (user, rel, subject) by enforce_directionality — count the entity on either side
            cur.execute(
                f"SELECT f.rel_type, f.subject_id, f.object_id, f.fact_class FROM \"{schema}\".facts f "
                f"JOIN \"{schema}\".entity_aliases a ON a.entity_id IN (f.subject_id, f.object_id) "
                f"WHERE a.alias = %s AND f.superseded_at IS NULL AND f.deleted_at IS NULL "
                f"ORDER BY f.rel_type, f.subject_id, f.object_id", (subject_alias,))
            return cur.fetchall()
    finally:
        c.close()


# ─────────────────────────────────────────────────────────────────────────────
# instruments: loop-availability probe, out-of-loop watchdog, await-in-txn census
# ─────────────────────────────────────────────────────────────────────────────

async def _health_probe(client: httpx.AsyncClient, stop: asyncio.Event) -> list[float]:
    """Poll /health from the SAME loop the ingests run on; latency = loop availability.

    In-process there is no Qdrant/LLM, so /health honestly answers 503 "degraded" — the STATUS
    is not the instrument, the fact that it ANSWERED (and how fast) is. A frozen loop answers
    nothing; that is what the latency bound catches."""
    lat: list[float] = []
    await client.get("/health")  # warm the once-per-boot cold path (cached after)
    while not stop.is_set():
        t0 = time.perf_counter()
        r = await client.get("/health")
        lat.append(time.perf_counter() - t0)
        assert r.status_code in (200, 503), r.status_code
        try:
            await asyncio.wait_for(stop.wait(), timeout=PROBE_PERIOD_S)
        except asyncio.TimeoutError:
            pass
    return lat


def _p99(xs: list[float]) -> float:
    if not xs:
        return 0.0
    if len(xs) < 100:
        return max(xs)
    return statistics.quantiles(xs, n=100)[-1]


class _Tracked:
    """Every psycopg2 connection opened while a pin runs, for the watchdog + census.

    WEAK references only: much of the ingest path closes its short-lived connections by
    refcount (`with psycopg2.connect() as c:` commits but does not close), so a strong list here
    would keep every one of them open and exhaust max_connections under the load leg — an
    instrument that changes what it measures. A weakref lets the code's own lifetime rule run."""

    def __init__(self):
        self._refs: list = []
        self.lock = threading.Lock()
        self.loop_thread = threading.get_ident()
        self.watchdog_fired = False
        self.done = False

    def add(self, c):
        import weakref
        with self.lock:
            self._refs.append(weakref.ref(c))

    def live(self) -> list:
        with self.lock:
            out = []
            keep = []
            for r in self._refs:
                c = r()
                if c is not None:
                    keep.append(r)
                    out.append(c)
            self._refs = keep
        return out

    def cancel_all(self):
        """Watchdog body. ONE cancel is not enough against the pre-fix handler: its per-edge
        fail-safe swallows the QueryCanceled, rolls back to the savepoint and re-blocks on the
        next lock wait (measured: a single-shot watchdog left the revert-red run hung for the
        harness's full 800s). So keep cancelling every second until the drive marks itself done."""
        self.watchdog_fired = True
        deadline = time.monotonic() + WATCHDOG_S * 3
        while not self.done and time.monotonic() < deadline:
            for c in self.live():
                try:
                    if not c.closed:
                        c.cancel()  # libpq PQcancel: its own socket, safe from another thread
                except Exception:
                    pass
            time.sleep(1.0)


def _install_connect_tracker(monkeypatch, tracked: _Tracked, *, census: bool):
    """Patch psycopg2.connect (module-global) so every connection is tracked; with census=True
    each cursor also records the THREAD that opened the connection's current transaction."""

    class _CensusCursorMixin:
        def execute(self, query, vars=None):
            conn = self.connection
            if conn.status == pg_ext.STATUS_READY:
                conn._fl_txn_thread = threading.get_ident()
            return super().execute(query, vars)

    cursor_classes: dict = {}

    def _cursor_cls(base):
        base = base or pg_ext.cursor
        if isinstance(base, type) and issubclass(base, _CensusCursorMixin):
            return base
        cls = cursor_classes.get(base)
        if cls is None:
            cls = type(f"Census{base.__name__}", (_CensusCursorMixin, base), {})
            cursor_classes[base] = cls
        return cls

    conn_classes: dict = {}

    def _conn_cls(base):
        base = base or pg_ext.connection
        cls = conn_classes.get(base)
        if cls is None:
            class _CensusConn(base):
                _fl_txn_thread = None

                def cursor(self, *args, **kwargs):
                    if census:
                        if len(args) >= 2:
                            args = (args[0], _cursor_cls(args[1]), *args[2:])
                        else:
                            kwargs["cursor_factory"] = _cursor_cls(kwargs.get("cursor_factory") or self.cursor_factory)
                    return super().cursor(*args, **kwargs)
            _CensusConn.__name__ = f"Census{base.__name__}"
            conn_classes[base] = _CensusConn
            cls = _CensusConn
        return cls

    def tracking_connect(*args, **kwargs):
        kwargs["connection_factory"] = _conn_cls(kwargs.get("connection_factory"))
        c = _real_connect(*args, **kwargs)
        tracked.add(c)
        return c

    monkeypatch.setattr(psycopg2, "connect", tracking_connect)
    return tracked


class _AwaitCensus:
    """Drive a coroutine step by step; at EVERY suspension to the loop, count tracked
    connections that are STATUS_IN_TRANSACTION with the transaction opened on the loop thread.

    This is exact, not sampled: a coroutine proxy sees each `yield` the inner coroutine makes
    to the event loop, which is precisely "an await that suspended"."""

    def __init__(self, coro, tracked: _Tracked):
        self.coro = coro
        self.tracked = tracked
        self.violations: list[dict] = []
        self.suspensions = 0

    def _check(self):
        for c in self.tracked.live():
            try:
                if c.closed:
                    continue
                if c.status == pg_ext.STATUS_IN_TRANSACTION and \
                        getattr(c, "_fl_txn_thread", None) == self.tracked.loop_thread:
                    self.violations.append({"suspension": self.suspensions, "conn": id(c)})
            except Exception:
                pass

    def __await__(self):
        it = self.coro.__await__()
        try:
            value = it.send(None)
        except StopIteration as s:
            return s.value
        while True:
            self.suspensions += 1
            self._check()
            try:
                sent = yield value
            except BaseException as exc:  # noqa: BLE001 — forward cancellation/errors inward
                try:
                    value = it.throw(exc)
                except StopIteration as s:
                    return s.value
            else:
                try:
                    value = it.send(sent)
                except StopIteration as s:
                    return s.value


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://ingest-pin",
                             timeout=httpx.Timeout(CALLER_WALL_S * 2))


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────────────
# ID2 — two byte-identical replays + a /health probe on the same loop
# ─────────────────────────────────────────────────────────────────────────────

def test_two_identical_replays_leave_the_loop_free_and_write_one_row_set(app_env, monkeypatch):
    tracked = _install_connect_tracker(monkeypatch, _Tracked(), census=False)
    subject = f"sib{uuid.uuid4().hex[:8]}"
    body = _body(app_env["user_id"], f"My sibling {subject} lives in Waterloo and owns a red canoe {uuid.uuid4().hex[:6]}.",
                 _edges(subject, "waterloo", f"red canoe {uuid.uuid4().hex[:6]}"))
    watchdog = threading.Timer(WATCHDOG_S, tracked.cancel_all)
    watchdog.daemon = True

    async def drive():
        async with _client() as client:
            stop = asyncio.Event()
            probe = asyncio.create_task(_health_probe(client, stop))
            t0 = time.perf_counter()
            watchdog.start()
            try:
                r1, r2 = await asyncio.gather(client.post("/ingest", json=body),
                                              client.post("/ingest", json=body))
            finally:
                watchdog.cancel()
                tracked.done = True
                stop.set()
            wall = time.perf_counter() - t0
            lat = await probe
            return r1, r2, wall, lat

    r1, r2, wall, lat = _run(drive())
    assert not tracked.watchdog_fired, f"loop frozen: watchdog cut the pinned statements after {WATCHDOG_S}s"
    assert r1.status_code == 200 and r2.status_code == 200, (r1.status_code, r1.text[:300], r2.status_code, r2.text[:300])
    j1, j2 = r1.json(), r2.json()
    assert j1["status"] == "valid" and j1["committed"] == 3, j1
    # both return the SAME committed shape (the duplicate is answered from the cache, not re-written)
    same = ("status", "committed", "staged", "scalar_committed")
    assert {k: j1[k] for k in same} == {k: j2[k] for k in same}, (j1, j2)
    assert sorted((f["rel_type"], f["object"]) for f in j1["facts"]) == \
        sorted((f["rel_type"], f["object"]) for f in j2["facts"])
    # exactly ONE row set: 3 live facts for the subject, no duplicates
    rows = _fact_rows(app_env["dsn"], app_env["schema"], subject)
    assert len(rows) == 3 and len({(r[0], r[1], r[2]) for r in rows}) == 3, rows
    # the cache is authoritative afterwards (a third replay never touches a row)
    key = _cache_key(app_env["idem"], body)
    assert app_env["idem"].get_cached_response(key) is not None, app_env["idem_backend"]
    # the loop stayed free the whole time
    assert lat and max(lat) < HEALTH_BOUND_S, f"/health max {max(lat):.3f}s p99 {_p99(lat):.3f}s n={len(lat)} wall={wall:.1f}s"
    assert wall < CALLER_WALL_S, wall


# ─────────────────────────────────────────────────────────────────────────────
# ID2 — load leg: 50 identical + 5 distinct concurrent → /health p99 < 1s
# ─────────────────────────────────────────────────────────────────────────────

def test_fifty_identical_plus_five_distinct_keep_health_p99_under_one_second(app_env, monkeypatch):
    tracked = _install_connect_tracker(monkeypatch, _Tracked(), census=False)
    tag = uuid.uuid4().hex[:8]
    same_subject = f"rep{tag}"
    identical = _body(app_env["user_id"], f"My sibling {same_subject} lives in Waterloo and owns a blue kayak {tag}.",
                      _edges(same_subject, "waterloo", f"blue kayak {tag}"))
    distinct = [
        _body(app_env["user_id"], f"My sibling dst{tag}{i} lives in Cambridge and owns a green tent {tag}{i}.",
              _edges(f"dst{tag}{i}", "cambridge", f"green tent {tag}{i}"))
        for i in range(5)
    ]
    watchdog = threading.Timer(WATCHDOG_S * 2, tracked.cancel_all)
    watchdog.daemon = True

    async def drive():
        async with _client() as client:
            stop = asyncio.Event()
            probe = asyncio.create_task(_health_probe(client, stop))
            walls: dict[int, float] = {}

            async def one(i, b):
                t0 = time.perf_counter()
                r = await client.post("/ingest", json=b)
                walls[i] = time.perf_counter() - t0
                return r
            watchdog.start()
            t0 = time.perf_counter()
            try:
                results = await asyncio.gather(*[one(i, identical) for i in range(50)],
                                               *[one(50 + i, distinct[i]) for i in range(5)])
            finally:
                watchdog.cancel()
                tracked.done = True
                stop.set()
            total = time.perf_counter() - t0
            lat = await probe
            return results, walls, total, lat

    results, walls, total, lat = _run(drive())
    assert not tracked.watchdog_fired, f"loop frozen under load: watchdog fired after {WATCHDOG_S * 2}s"
    codes = [r.status_code for r in results]
    assert codes == [200] * 55, codes
    ident = [r.json() for r in results[:50]]
    assert all(j["committed"] == 3 and j["status"] == "valid" for j in ident), ident[0]
    assert len({json.dumps({k: j[k] for k in ("committed", "staged", "scalar_committed")}, sort_keys=True) for j in ident}) == 1
    for i in range(5):
        j = results[50 + i].json()
        assert j["committed"] == 3, j
        assert len(_fact_rows(app_env["dsn"], app_env["schema"], f"dst{tag}{i}")) == 3
    assert len(_fact_rows(app_env["dsn"], app_env["schema"], same_subject)) == 3
    p99 = _p99(lat)
    print(f"\nID2 load leg: /health n={len(lat)} p50={statistics.median(lat):.4f}s p99={p99:.4f}s max={max(lat):.4f}s | "
          f"ingest walls max={max(walls.values()):.2f}s total={total:.2f}s | cache={app_env['idem_backend']}")
    assert p99 < HEALTH_BOUND_S, f"/health p99 {p99:.3f}s (n={len(lat)}, max {max(lat):.3f}s)"
    assert max(walls.values()) < CALLER_WALL_S, f"a request exceeded the caller's wall: {max(walls.values()):.1f}s"


# ─────────────────────────────────────────────────────────────────────────────
# ID3 — no await inside an open transaction on the ingest path (runtime + static)
# ─────────────────────────────────────────────────────────────────────────────

def test_ingest_never_awaits_inside_a_loop_thread_transaction(app_env, monkeypatch):
    tracked = _install_connect_tracker(monkeypatch, _Tracked(), census=True)
    from src.api.models import IngestRequest
    from starlette.requests import Request
    subject = f"cen{uuid.uuid4().hex[:8]}"
    body = _body(app_env["user_id"], f"My sibling {subject} lives in Kitchener and owns a grey bike.",
                 _edges(subject, "kitchener", f"grey bike {uuid.uuid4().hex[:6]}"))
    req = IngestRequest(**body)
    scope = {"type": "http", "method": "POST", "path": "/ingest", "headers": [], "query_string": b""}
    request = Request(scope)
    census = _AwaitCensus(main.ingest(req, request, main.get_gliner_model()), tracked)
    watchdog = threading.Timer(WATCHDOG_S, tracked.cancel_all)
    watchdog.daemon = True

    async def drive():
        watchdog.start()
        try:
            return await census
        finally:
            watchdog.cancel()
            tracked.done = True

    resp = _run(drive())
    assert not tracked.watchdog_fired
    committed = resp["committed"] if isinstance(resp, dict) else resp.committed
    assert committed == 3, resp
    assert census.suspensions > 0, "the handler never suspended — the census observed nothing"
    assert census.violations == [], (
        f"{len(census.violations)} await(s) while a loop-thread transaction was open "
        f"(of {census.suspensions} suspensions): {census.violations[:5]}")
    # a genuine committed write happened on a worker thread (so the census had a transaction to watch)
    assert len(_fact_rows(app_env["dsn"], app_env["schema"], subject)) == 3


def test_static_census_transactional_body_has_no_await_and_runs_off_loop():
    """Pinned in the source too, so a NEW `await` inside the body goes red without a database."""
    src = inspect.getsource(main)
    tree = ast.parse(src)
    impl = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_ingest_impl")
    body_fn = next(n for n in ast.walk(impl) if isinstance(n, ast.FunctionDef) and n.name == "_ingest_txn_body")
    awaits = [n.lineno for n in ast.walk(body_fn) if isinstance(n, ast.Await)]
    assert awaits == [], f"await inside the transactional body at lines {awaits}"
    withs = [n for n in ast.walk(body_fn) if isinstance(n, ast.With)]
    assert any(ast.unparse(w.items[0].context_expr).startswith("_ingest_connect(") for w in withs), \
        "the transactional connection must be opened by _ingest_connect (bounded lock_timeout)"
    # the body is driven through asyncio.to_thread from the coroutine — off the loop thread
    calls = [ast.unparse(n) for n in ast.walk(impl) if isinstance(n, ast.Await)]
    assert any("asyncio.to_thread(_ingest_txn_body)" in c for c in calls), calls[:5]
    # no `await asyncio.to_thread` survived inside the body under its old spelling
    assert "_sync_in_txn_thread(" in ast.unparse(body_fn)


def test_retract_correct_is_a_sync_endpoint_so_the_same_shape_cannot_exist():
    """`/retract/correct` (`main.correct_fact`) is a plain `def`: FastAPI runs it in a worker
    thread, so it holds no event-loop transaction and can contain no `await` at all. Stated so
    nobody goes looking for the ingest defect there; if it is ever made `async def`, this pin
    turns red and the census above must be extended to it."""
    assert not inspect.iscoroutinefunction(main.correct_fact), \
        "/retract/correct became async — extend the await-in-transaction census to it"
    src = inspect.getsource(main.correct_fact)
    assert "await " not in src


# ─────────────────────────────────────────────────────────────────────────────
# ID4 — forced contention: an external session holds the row lock → bounded 503 + CRIT
# ─────────────────────────────────────────────────────────────────────────────

def test_forced_row_lock_contention_returns_503_and_crit_within_lock_timeout(app_env, monkeypatch):
    lock_wait = 1.0
    monkeypatch.setenv("INGEST_TIMEOUT_LOCK_WAIT", str(lock_wait))
    assert main.IngestTimeouts.get("LOCK_WAIT") == lock_wait
    tracked = _install_connect_tracker(monkeypatch, _Tracked(), census=False)
    subject = f"lck{uuid.uuid4().hex[:8]}"
    thing = f"copper kettle {uuid.uuid4().hex[:6]}"
    first = _body(app_env["user_id"], f"My sibling {subject} lives in Elora and owns a {thing}.",
                  _edges(subject, "elora", thing))
    # same edges, different text → different idempotency key, same target rows
    restated = _body(app_env["user_id"], f"As I said, my sibling {subject} lives in Elora and owns a {thing}.",
                     _edges(subject, "elora", thing))

    async def seed():
        async with _client() as client:
            r = await client.post("/ingest", json=first)
            assert r.status_code == 200 and r.json()["committed"] == 3, r.text[:300]
    _run(seed())
    rows = _fact_rows(app_env["dsn"], app_env["schema"], subject)
    assert len(rows) == 3

    # external session: hold row locks on the subject's facts + entity rows and DO NOT commit
    holder = _real_connect(app_env["dsn"])
    hcur = holder.cursor()
    hcur.execute(f'SET search_path TO "{app_env["schema"]}"')
    hcur.execute("SELECT entity_id FROM entity_aliases WHERE alias = %s", (subject,))
    subject_id = hcur.fetchone()[0]
    hcur.execute("SELECT id FROM facts WHERE subject_id = %s FOR UPDATE", (subject_id,))
    hcur.execute("SELECT id FROM entities WHERE id = %s FOR UPDATE", (subject_id,))
    hcur.execute("SELECT id FROM entity_aliases WHERE entity_id = %s FOR UPDATE", (subject_id,))
    assert holder.status == pg_ext.STATUS_IN_TRANSACTION
    watchdog = threading.Timer(WATCHDOG_S, tracked.cancel_all)
    watchdog.daemon = True
    try:
        async def drive():
            async with _client() as client:
                stop = asyncio.Event()
                probe = asyncio.create_task(_health_probe(client, stop))
                watchdog.start()
                t0 = time.perf_counter()
                try:
                    r = await client.post("/ingest", json=restated)
                finally:
                    watchdog.cancel()
                    tracked.done = True
                    stop.set()
                return r, time.perf_counter() - t0, await probe
        r, elapsed, lat = _run(drive())
    finally:
        holder.rollback()
        holder.close()
    assert not tracked.watchdog_fired, "the lock wait was NOT bounded — watchdog had to cut it"
    assert r.status_code == 503, (r.status_code, r.text[:400])
    detail = r.json()["detail"]
    assert detail["pgcode"] == "55P03" and detail["retryable"] is True, detail
    assert r.headers.get("retry-after"), "a retryable 503 must carry Retry-After"
    # bounded: the whole request finished within a few lock waits of the configured bound
    # (the body may hit the lock more than once — each attempt is capped by lock_timeout)
    assert elapsed < lock_wait * 6 + 5.0, f"took {elapsed:.1f}s for lock_wait={lock_wait}s"
    # LOUD: log_crit fired with the contention event and the SQLSTATE
    crit = [c for c in app_env["crits"] if c["event"] == "ingest.lock_timeout_contention"]
    assert crit, [c["event"] for c in app_env["crits"]]
    assert crit[0]["pgcode"] == "55P03"
    # and the loop was free while the worker thread waited on the lock
    assert lat and max(lat) < HEALTH_BOUND_S, f"/health max {max(lat):.3f}s during the lock wait"
    # nothing was cached for the failed attempt, so a retry re-runs (and, unlocked, lands)
    key = _cache_key(app_env["idem"], restated)
    assert app_env["idem"].get_cached_response(key) is None

    async def retry():
        async with _client() as client:
            return await client.post("/ingest", json=restated)
    r2 = _run(retry())
    assert r2.status_code == 200, r2.text[:300]
    assert len(_fact_rows(app_env["dsn"], app_env["schema"], subject)) == 3  # still one row set


# ─────────────────────────────────────────────────────────────────────────────
# config discipline — the waits come from config with floor + ceiling, never a literal
# ─────────────────────────────────────────────────────────────────────────────

def test_ingest_timeouts_are_config_driven_with_floor_and_ceiling(monkeypatch, postgres_dsn):
    T = main.IngestTimeouts
    monkeypatch.delenv("INGEST_TIMEOUT_LOCK_WAIT", raising=False)
    assert T.get("LOCK_WAIT") == T._DEFAULTS["LOCK_WAIT"] > 0
    monkeypatch.setenv("INGEST_TIMEOUT_LOCK_WAIT", "0")            # "wait forever" is refused
    assert T.get("LOCK_WAIT") == T._FLOORS["LOCK_WAIT"]
    monkeypatch.setenv("INGEST_TIMEOUT_LOCK_WAIT", "99999")
    assert T.get("LOCK_WAIT") == T._CEILINGS["LOCK_WAIT"]
    monkeypatch.setenv("INGEST_TIMEOUT_LOCK_WAIT", "not-a-number")
    assert T.get("LOCK_WAIT") == T._DEFAULTS["LOCK_WAIT"]
    monkeypatch.setenv("INGEST_TIMEOUT_INFLIGHT_WAIT", "2.5")
    assert T.get("INFLIGHT_WAIT") == 2.5
    # the request-path connection actually carries the bound (session default, survives rollback)
    dsn = _dsn_or_skip(postgres_dsn)
    monkeypatch.setenv("INGEST_TIMEOUT_LOCK_WAIT", "1.5")
    c = main._ingest_connect(dsn)
    try:
        with c.cursor() as cur:
            cur.execute("SHOW lock_timeout")
            assert cur.fetchone()[0] == "1500ms"
            cur.execute("SELECT 1")
        c.rollback()  # a rollback must not discard it — it is a startup parameter, not a SET in a txn
        with c.cursor() as cur:
            cur.execute("SHOW lock_timeout")
            assert cur.fetchone()[0] == "1500ms"
    finally:
        c.close()


def test_inflight_duplicate_waits_on_the_in_process_lock_not_postgres():
    """Two coroutines with the same key serialise on the asyncio lock; a bounded wait expires
    LOUD (503) instead of a second write. No database involved — this is the pre-row-lock door."""
    async def drive():
        order = []
        release = asyncio.Event()

        async def holder():
            async with main._ingest_inflight_guard("k1"):
                order.append("A-in")
                await release.wait()
                order.append("A-out")

        async def follower():
            await asyncio.sleep(0.01)
            async with main._ingest_inflight_guard("k1"):
                order.append("B-in")
        t1, t2 = asyncio.create_task(holder()), asyncio.create_task(follower())
        await asyncio.sleep(0.05)
        assert order == ["A-in"]           # B is parked on the asyncio lock, loop free
        release.set()
        await asyncio.gather(t1, t2)
        assert order == ["A-in", "A-out", "B-in"]
        assert "k1" not in main._INGEST_INFLIGHT_LOCKS  # entry dropped with its last holder

        os.environ["INGEST_TIMEOUT_INFLIGHT_WAIT"] = "1.0"
        try:
            async with main._ingest_inflight_guard("k2"):
                from fastapi import HTTPException
                with pytest.raises(HTTPException) as ei:
                    async with main._ingest_inflight_guard("k2"):
                        pass
                assert ei.value.status_code == 503
                assert ei.value.detail["retryable"] is True
        finally:
            os.environ.pop("INGEST_TIMEOUT_INFLIGHT_WAIT", None)
        assert "k2" not in main._INGEST_INFLIGHT_LOCKS
    _run(drive())


def test_inflight_guard_key_needs_no_redis(monkeypatch):
    """The replay door must close even with NO idempotency manager / Redis: the guard key is a
    pure hash of the request (critic residual, round 1). With the manager absent the door must
    still take the per-key lock — observed by the lock entry existing while the impl runs."""
    from src.api.models import IngestRequest
    from starlette.requests import Request
    monkeypatch.setattr(main, "_idempotency_mgr", None, raising=False)
    seen = {}

    async def fake_impl(req, request, model):
        seen["locks"] = dict(main._INGEST_INFLIGHT_LOCKS)
        return {"status": "valid", "committed": 0}
    monkeypatch.setattr(main, "_ingest_impl", fake_impl)
    req = IngestRequest(text="no redis here", user_id=str(uuid.uuid4()), edges=[
        {"subject": "user", "rel_type": "owns", "object": "a lamp"}])
    request = Request({"type": "http", "method": "POST", "path": "/ingest", "headers": [], "query_string": b""})
    expected = idem_mod.IdempotencyManager.generate_key(None, req.text, req.user_id, "/ingest",
                                                        memory_facts=req.memory_facts,
                                                        is_correction=req.is_correction, edges=req.edges)
    _run(main.ingest(req, request, None))
    assert expected in seen["locks"], "the door ran without taking the per-key in-flight lock"
    assert expected not in main._INGEST_INFLIGHT_LOCKS  # released and dropped afterwards
