"""Regression pins for the retract/correction-lane idle-transaction seams (gauntlet
fix/correction-idle-txn, 2026-08-22).

STEP ZERO re-verification on the gauntlet rig (labhost, throwaway fl-g3-* containers,
brain stubbed slow at 10s via DEV/probes/stub_brain.py) measured, AT PARENT 007356c5:

  * the /retract/correct request connection was CLEAN across the LLM call (9aa355c6 held)
    for an already-provisioned user, but a turn that had to WAIT FOR PROVISIONING held
    FOUR other backends idle-in-transaction:
      - the sync provisioning poll  (17.7s)  — _ensure_tenant_ready_sync
      - the async provisioning poll (same)   — wait_for_schema_ready
      - the provisioning QUEUE connection (25.2s and 97.5s across a batch) —
        process_provisioning_queue's queue-fetch transaction never ended before the
        per-user loop (each user takes tens of seconds on its OWN connection)
      - information_schema.schemata (12.3s)  — create_user_schema's catalog check held
        across the psql template subprocess
  * AND a correctness rider: correct_fact's "SET search_path" rode the same implicit
    transaction, so the builders' release_read_transaction() (9aa355c6) ROLLED IT BACK —
    post-LLM queries ran against PUBLIC (measured: 'column "display_form" does not exist'
    from public.entity_aliases, then InFailedSqlTransaction -> HTTP 500; retractions
    silently missed tenant facts because public.facts exists but is empty).

THE FIX (connection-release only — the /retract/correct anti-scope forbids adding retry
to this lane; no behaviour change other than ending pure-read transactions before a
blocking wait and committing the tenant binding):

  1. _ensure_tenant_ready_sync      releases the poll transaction before each backoff sleep
  2. wait_for_schema_ready          same, before its await asyncio.sleep
  3. process_provisioning_queue     releases the queue-fetch transaction before the batch loop
  4. create_user_schema             releases the schemata-check transaction before the psql
                                    template subprocess
  5. correct_fact                   COMMITS the tenant search_path binding right after SET
                                    (a committed binding survives every later rollback —
                                    the contract src/api/db_read.py documents)

ROUND 3 of the gauntlet deployed the fix to a RUNNING stack and the live probe found
TWO more seams of the same class that a stale tree could not show (the rig's DNS fails
fast; a REAL resolver holds the connection for the full lookup):

  6. create_user_schema              releases the tables-verify read transaction before
                                    the Redis idempotency flush — the flush's ping/SCAN
                                    blocked ~8s on an unresolvable DNS name with the
                                    verify txn still open (measured live: 7.88s x2,
                                    last query the information_schema verify)
  7. correct_fact                    releases the lookup-cascade read transaction before
                                    the retraction semantic fallback — the fallback's
                                    embed + Qdrant network calls blocked with the cascade
                                    txn still open (measured live: 8.03s, last query the
                                    staged_facts subject-side lookup)

Plus two ARM-INDEPENDENT pins (green on both arms by design): the S4 mechanism
pin (committed vs uncommitted SET search_path on live PG) and the S8
FIXTURE-INTEGRITY pin — _seed_user collision-proof against a leftover
constant-slug row + the session-start sweep self-heals a wedged battery (the
round-5 critic gap: a battery interrupted mid-pin left slug='pin' rows that
redded every later battery at both correct_fact pins; demonstrated live at
round 4).

Each pin observes the connection's REAL transaction status / current_schema() from INSIDE
the blocking wait or the real subprocess (the same discipline as the 9aa355c6 pins in
test_db_read_no_idle_in_transaction.py), so the assertion can not pass by reading the
fix's own bookkeeping. The S3 pin runs the REAL template apply (execute_psql_file is
wrapped, not faked) so the observation point is a real provisioning.
"""

import asyncio
import importlib
import os
import uuid

import psycopg2
import psycopg2.extensions as pg_ext
import pytest

main = importlib.import_module("src.api.main")
schema_manager = importlib.import_module("src.provisioning.schema_manager")
provisioning_job = importlib.import_module("src.provisioning.provisioning_job")
provisioning_status_mod = importlib.import_module("src.provisioning.provisioning_status")

DSN = os.environ.get("POSTGRES_DSN")
_real_connect = psycopg2.connect  # saved BEFORE any monkeypatching (see S4 note)


def _dsn_or_skip():
    if not DSN:
        pytest.skip("POSTGRES_DSN not set — this pin requires a live Postgres")
    try:
        c = _real_connect(DSN, connect_timeout=3)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"POSTGRES_DSN unreachable: {type(e).__name__}: {str(e)[:120]}")
    c.close()
    return DSN


def _cleanup(dsn, *, schema=None, user_id=None):
    """Cleanup must use the REAL connect — monkeypatch may still be active (patching
    main.psycopg2.connect patches the psycopg2 MODULE, i.e. globally)."""
    c = _real_connect(dsn)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            if schema:
                cur.execute('DROP SCHEMA IF EXISTS %s CASCADE' % schema)
            if user_id:
                cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s",
                            (user_id,))
                cur.execute("DELETE FROM public.users WHERE user_id = %s", (user_id,))
    finally:
        c.close()


def _seed_user(dsn, user_id, schema_name, status="provisioning"):
    """public.users row (user_provisioning.user_id has an FK to it) + the
    provisioning row itself.

    ROUND-5 CRITIC GAP (fixture fragility): the slug was the CONSTANT "pin" with
    dedup on user_id only — public.users has a UNIQUE users_slug_key, so a battery
    interrupted mid-pin (cleanup never ran) left a slug='pin' row that made every
    LATER battery red at both correct_fact pins with UniqueViolation (demonstrated
    live: round 4). The slug is now UNIQUE PER CALL; the leftover class itself is
    cleared by the session-start sweep below (_sweep_pin_leftovers), pinned by
    test_pin_fixture_sweep_self_heals_a_wedged_battery.
    """
    c = _real_connect(dsn)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute("INSERT INTO public.users (user_id, email, display_name, slug) "
                        "VALUES (%s, %s, %s, %s) ON CONFLICT (user_id) DO NOTHING",
                        (user_id, f"{user_id}@pin", "pin",
                         f"pin-{uuid.uuid4().hex[:12]}"))
            cur.execute(
                "INSERT INTO public.user_provisioning (user_id, schema_name, status) "
                "VALUES (%s, %s, %s) ON CONFLICT (user_id) DO NOTHING",
                (user_id, schema_name, status))
    finally:
        c.close()


def _sweep_pin_leftovers(dsn):
    """Session-start self-heal: a battery interrupted mid-pin leaves fixture rows
    behind (public.users with the @pin email marker / constant-slug 'pin' rows from
    pre-hardening batteries, user_provisioning rows, faultline_pin* tenant
    schemas). Left in place they wedge every later battery (unique slug collision,
    orphan queue rows re-provisioned by the S2 batch). This namespace is EXCLUSIVE
    to this file (verified: no other test creates faultline_pin* schemas or @pin
    emails), so a pattern sweep is safe. Runs once per session before the first
    pin; failure is non-fatal (the sweep is a self-heal, never a gate)."""
    c = _real_connect(dsn)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name LIKE %s", ("faultline_pin%",))
            for (schema,) in cur.fetchall():
                cur.execute('DROP SCHEMA IF EXISTS %s CASCADE' % schema)
            cur.execute(
                "DELETE FROM public.user_provisioning WHERE schema_name LIKE %s",
                ("faultline_pin%",))
            cur.execute(
                "DELETE FROM public.users WHERE email LIKE %s OR slug = %s "
                "OR slug LIKE %s",
                ("%@pin", "pin", "pin-%"))
    finally:
        c.close()


@pytest.fixture(scope="session", autouse=True)
def _pin_fixture_sweep():
    """Battery self-heal: clear leftovers from an interrupted previous battery
    BEFORE the first pin runs (the critic's wedge scenario)."""
    if DSN:
        try:
            _sweep_pin_leftovers(DSN)
        except Exception:
            pass  # non-fatal by contract — the unique-slug seed is the hard guarantee


class _NoCloseConn:
    """Delegate everything to a real psycopg2 connection except close() — lets a test
    hand the REAL connection to code that believes it owns (and closes) it."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass  # the test owns the lifecycle


# ── S1a: the SYNC provisioning poll must not hold its read transaction across the
#    backoff sleep — measured 17.7s idle-in-transaction at parent on the rig.

def test_sync_provisioning_poll_holds_no_transaction_across_the_sleep(monkeypatch):
    dsn = _dsn_or_skip()
    conn = _real_connect(dsn)  # NOT autocommit: reproduce the production shape
    wrapper = _NoCloseConn(conn)
    seen = []

    def fake_check(user_id, db=None):
        target = db if db is not None else wrapper
        # Entry status = what the backend looked like WHILE THE LOOP SLEPT before this
        # poll. Then reproduce the parent's poll shape: a real SELECT opening the
        # implicit read transaction on the poll connection.
        seen.append(target.info.transaction_status)
        cur = target.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.close()
        return {"status": "provisioning"}  # never ready -> exercises the sleep path

    monkeypatch.setenv("PROVISIONING_TIMEOUT_SEC", "1")
    monkeypatch.setattr(main.psycopg2, "connect", lambda *a, **k: wrapper)
    monkeypatch.setattr(provisioning_status_mod, "check_provisioning_status", fake_check)
    monkeypatch.setattr(provisioning_status_mod, "ensure_user_provisioned",
                        lambda *a, **k: False)

    try:
        with pytest.raises(Exception):
            main._ensure_tenant_ready_sync(str(uuid.uuid4()), "pin")
    finally:
        conn.rollback()
        conn.close()

    assert len(seen) >= 2, "the poll never slept — pin did not exercise the seam"
    later = seen[1:]
    held = [s for s in later if s == pg_ext.TRANSACTION_STATUS_INTRANS]
    assert not held, (
        f"the sync provisioning poll held its read transaction across the backoff "
        f"sleep (INTRANS at entry of {len(held)}/{len(later)} later polls) — release "
        f"it before the sleep; measured 17.7s idle-in-transaction live at parent"
    )


# ── S1b: the ASYNC provisioning poll (wait_for_schema_ready) — same seam, own loop.

def test_async_provisioning_poll_holds_no_transaction_across_the_sleep(monkeypatch):
    dsn = _dsn_or_skip()
    conn = _real_connect(dsn)
    seen = []

    user_id = str(uuid.uuid4())
    schema_name = f"faultline_pin_{user_id.replace('-', '_')}"
    _seed_user(dsn, user_id, schema_name)

    class _Recorder:
        """Cursor proxy that records the connection status at each poll SELECT."""
        def __init__(self, real_cur):
            self._c = real_cur

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return self._c.__exit__(*a)

        def execute(self, sql, params=None):
            if "user_provisioning" in str(sql):
                seen.append(conn.info.transaction_status)
            return self._c.execute(sql, params)

        def fetchone(self):
            return (None,)  # ready_at NULL forever -> the loop must sleep and re-poll

        def __getattr__(self, name):
            return getattr(self._c, name)

    class _ConnShim:
        def __init__(self, real):
            self._real = real

        def cursor(self):
            return _Recorder(self._real.cursor())

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setenv("PROVISIONING_TIMEOUT_SEC", "1")
    try:
        asyncio.run(main.wait_for_schema_ready(
            user_id=user_id, user_slug="pin", db=_ConnShim(conn), timeout_sec=1))
    finally:
        conn.rollback()
        conn.close()
        _cleanup(dsn, user_id=user_id)

    assert len(seen) >= 2, "the poll never slept — pin did not exercise the seam"
    later = seen[1:]
    held = [s for s in later if s == pg_ext.TRANSACTION_STATUS_INTRANS]
    assert not held, (
        f"wait_for_schema_ready held its read transaction across the backoff sleep "
        f"(INTRANS at entry of {len(held)}/{len(later)} later polls) — release it "
        f"before the sleep"
    )


# ── S2: the provisioning QUEUE connection — its queue-fetch transaction must not
#    span the per-user provisioning loop (measured 25.2s / 97.5s at parent).

def test_provisioning_queue_holds_no_transaction_across_the_batch(monkeypatch):
    dsn = _dsn_or_skip()
    conn = _real_connect(dsn)
    user_id = str(uuid.uuid4())
    schema_name = f"faultline_pin_{user_id.replace('-', '_')}"
    _seed_user(dsn, user_id, schema_name)

    seen = {}

    def fake_create_user_schema(uid, slug, db=None):
        # The per-user lane runs on its own connection; with the queue's
        # get_postgres_connection patched to the same real conn, this observes exactly
        # what the queue-fetch transaction did before the loop hands over.
        seen["status"] = (db if db is not None else conn).info.transaction_status
        return schema_name, "ready"

    monkeypatch.setattr(provisioning_job, "get_postgres_connection",
                        lambda: _NoCloseConn(conn))
    monkeypatch.setattr(provisioning_job, "create_user_schema", fake_create_user_schema)
    # No create_cortex_schema patch: the cortex step moved INSIDE create_user_schema (8ad556ad,
    # "READY MEANS READY"), so provisioning_job no longer binds that name and the faked
    # create_user_schema above already covers it.

    try:
        results = provisioning_job.process_provisioning_queue(batch_size=5)
        # Under a FULL-SUITE run the shared queue table carries other tests' leftover
        # 'provisioning' rows (measured: count=5 in suite run 2, count=1 isolated), so
        # the sanity bound is >= 1 — the SEAM observation below is the actual pin and
        # is identical for every iteration (same real connection).
        assert results["provisioned"] >= 1
    finally:
        conn.rollback()
        conn.close()
        _cleanup(dsn, user_id=user_id)

    assert seen.get("status") == pg_ext.TRANSACTION_STATUS_IDLE, (
        f"the provisioning QUEUE connection was still inside its queue-fetch read "
        f"transaction while provisioning users (transaction_status={seen.get('status')}) "
        f"— release it before the per-user loop; measured 97.5s idle-in-transaction "
        f"live at parent"
    )


# ── S3: create_user_schema's schemata catalog check must not span the psql template
#    subprocess (measured 12.3s at parent; the 2026-08-21 pre-prod probe saw the same
#    fingerprint at 5-11s x3). The REAL template apply runs — execute_psql_file is
#    wrapped for observation, not faked — so the status is read inside a real
#    provisioning, from the connection that will next bind the tenant search_path.

def test_create_user_schema_holds_no_transaction_across_the_psql_subprocess(monkeypatch):
    dsn = _dsn_or_skip()
    conn = _real_connect(dsn)
    user_id = str(uuid.uuid4())
    slug = f"pin{user_id.replace('-', '')[:12]}"  # unique -> derive_schema_name unique
    seen = {}
    real_psql = schema_manager.execute_psql_file

    def probe_psql(path, schema, dsn_components):
        seen["status"] = conn.info.transaction_status
        seen["schema"] = schema
        return real_psql(path, schema, dsn_components)

    monkeypatch.setenv("POSTGRES_DSN", dsn)
    monkeypatch.setattr(schema_manager, "execute_psql_file", probe_psql)

    try:
        name, status = schema_manager.create_user_schema(user_id, slug, conn)
    finally:
        conn.rollback()
        conn.close()
        _cleanup(dsn, schema=seen.get("schema"), user_id=user_id)

    assert "status" in seen, "the template apply never ran — pin did not exercise the seam"
    assert seen["status"] == pg_ext.TRANSACTION_STATUS_IDLE, (
        f"the schemata catalog-check transaction was still open inside the REAL psql "
        f"template subprocess (transaction_status={seen['status']}) — release it "
        f"before the subprocess; measured 12.3s idle-in-transaction live at parent. "
        f"(provisioning terminal status was {status!r}, informational)"
    )


# ── S4: correct_fact must COMMIT the tenant search_path binding — an uncommitted SET
#    is rolled back by the builders' release_read_transaction() (9aa355c6), silently
#    re-scoping every post-LLM query on the request connection to PUBLIC.

def _drive_correct_fact_for_binding(dsn, monkeypatch):
    """Run the REAL correct_fact with a REAL Postgres connection. Only the provisioning
    guards and the extraction builder are faked; the fake builder reproduces the REAL
    builders' seam-relevant shape exactly (menu SELECT -> release_read_transaction ->
    LLM call), so what the pin observes from inside the "brain call" is the true
    request-connection state, not the fix's own bookkeeping."""
    from src.api.db_read import release_read_transaction
    from src.api.models import FactCorrectionRequest

    conn = _real_connect(dsn)
    user_id = str(uuid.uuid4())
    schema_name = f"faultline_pin_{user_id.replace('-', '_')}"

    seed = _real_connect(dsn)
    seed.autocommit = True
    with seed.cursor() as cur:
        cur.execute("CREATE SCHEMA " + schema_name)
    seed.close()
    _seed_user(dsn, user_id, schema_name, status="ready")
    ready = _real_connect(dsn)
    ready.autocommit = True
    with ready.cursor() as cur:
        cur.execute("UPDATE public.user_provisioning SET ready_at = now() "
                    "WHERE user_id = %s", (user_id,))
    ready.close()

    llm_seen = {}

    def fake_builder(**kwargs):
        db = kwargs.get("db")
        with db.cursor() as cur:          # the menu read (opens the implicit txn)
            cur.execute("SELECT 1")
            cur.fetchone()
        release_read_transaction(db, context="pin_builder_menu")  # the 9aa355c6 shape
        probe = db.cursor()               # inside the brain call, observe the scope
        probe.execute("SELECT current_schema()")
        llm_seen["schema_at_llm"] = probe.fetchone()[0]
        probe.close()
        return {
            "subject_name": "user",
            "old_rel_type": "has_pet",
            "old_value": "pin-old",
            "new_rel_type": "has_pet",
            "new_value": "pin-new",
            "dimension": "RELATIONAL",
            "confidence": 0.95,
        }

    monkeypatch.setattr(main, "_ensure_tenant_ready_sync", lambda *a, **k: None)
    monkeypatch.setattr(main, "_require_resolvable_user_id", lambda uid, *a, **k: uid)
    monkeypatch.setattr(main, "_INGEST_ENABLED", True, raising=False)
    monkeypatch.setattr(main, "STRUCTURAL_CORRECTION", False, raising=False)
    monkeypatch.setattr(main, "_idempotency_mgr", None, raising=False)
    monkeypatch.setattr(main, "_unified_correction_extraction_llm", fake_builder)

    # correct_fact's own psycopg2.connect must hand it the instrumented connection, but
    # patching main.psycopg2.connect patches psycopg2 GLOBALLY (same module object), so
    # route only the FIRST connect (the request connection) to the wrapper and let every
    # later internal connect (overlays etc.) get a real connection of their own.
    calls = {"n": 0}

    def selective_connect(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _NoCloseConn(conn)
        return _real_connect(*a, **k)

    monkeypatch.setattr(main.psycopg2, "connect", selective_connect)

    try:
        req = FactCorrectionRequest(
            user_id=user_id, text="the pin value is pin-new, not pin-old",
            intent="CORRECTION", context_facts=[])
        main.correct_fact(req)
    except Exception:
        # The post-LLM lane may fail on the bare pin schema (no tenant tables); the
        # BINDING is observed from inside the brain call, before any of that runs.
        pass
    finally:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        _cleanup(dsn, schema=schema_name, user_id=user_id)

    return llm_seen, schema_name


def test_correct_fact_tenant_binding_survives_the_builder_release(monkeypatch):
    """RED AT PARENT: with no commit after SET search_path, the builders'
    release_read_transaction() rolls the binding back, and by the time the brain call
    runs the request connection is scoped to PUBLIC (measured live: post-LLM queries
    hit public.entity_aliases and the correction 500'd). The pin observes
    current_schema() from INSIDE the brain call."""
    dsn = _dsn_or_skip()
    llm_seen, schema_name = _drive_correct_fact_for_binding(dsn, monkeypatch)

    assert llm_seen.get("schema_at_llm") is not None, (
        "the brain call never ran — the pin did not exercise the seam"
    )
    assert llm_seen["schema_at_llm"] == schema_name, (
        f"at the brain call the request connection was scoped to "
        f"{llm_seen['schema_at_llm']!r}, not the tenant schema {schema_name!r} — the "
        f"SET search_path binding was rolled back by the builders' "
        f"release_read_transaction() (an uncommitted SET reverts) and every post-LLM "
        f"query on the request connection will read/write PUBLIC"
    )


def test_uncommitted_set_search_path_does_not_survive_a_read_release():
    """MECHANISM pin (both arms, live PG): proves WHY the fix is a commit — a SET issued
    inside a transaction that is then rolled back reverts, while a committed binding
    survives. This is the contract src/api/db_read.py documents; pinned so a future
    'simplify the commit away' refactor fails here first."""
    dsn = _dsn_or_skip()
    schema_name = f"faultline_pin_mech_{uuid.uuid4().hex[:8]}"
    from src.api.db_read import release_read_transaction

    conn = _real_connect(dsn)
    try:
        admin = _real_connect(dsn)
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute("CREATE SCHEMA " + schema_name)
        admin.close()

        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema_name}")
        release_read_transaction(conn, context="pin_uncommitted")
        with conn.cursor() as cur:
            cur.execute("SELECT current_schema()")
            assert cur.fetchone()[0] != schema_name, (
                "an UNCOMMITTED SET search_path survived a rollback — PG semantics "
                "changed; re-examine the correct_fact binding commit"
            )

        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema_name}")
        conn.commit()
        release_read_transaction(conn, context="pin_committed")
        with conn.cursor() as cur:
            cur.execute("SELECT current_schema()")
            assert cur.fetchone()[0] == schema_name, (
                "a COMMITTED SET search_path did not survive a read release — the "
                "correct_fact binding commit no longer protects the tenant scope"
            )
    finally:
        conn.rollback()
        conn.close()
        _cleanup(dsn, schema=schema_name)


# ── S6: create_user_schema's tables-verify read transaction must not span the
#    Redis idempotency flush (measured live on the RUNNING gauntlet stack, round 3:
#    7.88s idle-in-transaction x2, last query the information_schema verify; the
#    flush's redis ping blocked ~8s on an unresolvable DNS name). The pin observes
#    the connection's REAL transaction status at ENTRY to the flush — the blocking
#    wait the seam guards — with the real provisioning running to that point.

def test_create_user_schema_holds_no_transaction_across_the_redis_flush(monkeypatch):
    dsn = _dsn_or_skip()
    conn = _real_connect(dsn)
    user_id = str(uuid.uuid4())
    slug = f"pin{user_id.replace('-', '')[:12]}"  # unique -> unique schema name
    seen = {}

    def probe_flush(uid):
        # Observation point: entry into the blocking network wait. The real flush
        # (redis ping + SCAN) is NOT invoked — the wait's duration is irrelevant to
        # the seam; the connection state AT ENTRY is what the release protects.
        seen["status"] = conn.info.transaction_status

    monkeypatch.setenv("POSTGRES_DSN", dsn)
    monkeypatch.setattr(schema_manager, "_flush_user_idempotency_cache", probe_flush)

    name = None
    try:
        name, status = schema_manager.create_user_schema(user_id, slug, conn)
    finally:
        conn.rollback()
        conn.close()
        _cleanup(dsn, schema=name, user_id=user_id)

    assert "status" in seen, "the redis flush never ran — pin did not exercise the seam"
    assert seen["status"] == pg_ext.TRANSACTION_STATUS_IDLE, (
        f"the tables-verify read transaction was still open at ENTRY to the Redis "
        f"idempotency flush (transaction_status={seen['status']}) — release it before "
        f"the flush; measured 7.88s idle-in-transaction live on the running stack "
        f"(redis DNS timeout). (provisioning terminal status was {status!r}, "
        f"informational)"
    )


# ── S7: correct_fact's lookup-cascade read transaction must not span the retraction
#    semantic fallback (measured live on the RUNNING gauntlet stack, round 3: 8.03s
#    idle-in-transaction on the REQUEST backend, last query the staged_facts
#    subject-side lookup; the fallback's embed call + Qdrant query blocked on
#    unreachable services). The pin drives the REAL correct_fact on a REAL provisioned
#    (empty) tenant schema so every cascade step runs and misses, and observes the
#    REQUEST connection's REAL transaction status at ENTRY to the fallback.

def test_correct_fact_holds_no_transaction_across_the_retraction_fallback(monkeypatch):
    from src.api.db_read import release_read_transaction
    from src.api.models import FactCorrectionRequest

    dsn = _dsn_or_skip()
    user_id = str(uuid.uuid4())
    slug = f"pin{user_id.replace('-', '')[:12]}"

    # Seed users + user_provisioning first (create_user_schema's mark-ready is an
    # UPDATE — without the row it no-ops and get_user_schema_context() then rejects
    # the tenant as not provisioned before extraction ever runs).
    _seed_user(dsn, user_id, f"faultline_{slug}", status="provisioning")

    # REAL provisioning: the cascade queries facts/staged_facts/entities in the
    # tenant schema — they must exist for every lookup step to run and MISS.
    schema_name, prov_status = schema_manager.create_user_schema(user_id, slug)
    assert prov_status == "ready", f"pin provisioning failed: {prov_status!r}"

    conn = _real_connect(dsn)
    llm_seen = {}
    fallback_seen = {}

    def fake_retraction_extraction(**kwargs):
        db = kwargs.get("db")
        with db.cursor() as cur:          # the menu read (opens the implicit txn)
            cur.execute("SELECT 1")
            cur.fetchone()
        release_read_transaction(db, context="pin_retraction_menu")  # 9aa355c6 shape
        llm_seen["ran"] = True
        return {
            "subject": "user",
            "rel_type": "has_pet",
            "object": "pin-ghost-pet",
            "action": "remove",
            "dimension": "RELATIONAL",
            "confidence": 0.95,
        }

    def probe_fallback(text, user_id_arg, db, log):
        fallback_seen["status"] = db.info.transaction_status
        return None  # "Qdrant unavailable" contract — caller falls to not-found

    monkeypatch.setattr(main, "_ensure_tenant_ready_sync", lambda *a, **k: None)
    monkeypatch.setattr(main, "_require_resolvable_user_id", lambda uid, *a, **k: uid)
    monkeypatch.setattr(main, "_INGEST_ENABLED", True, raising=False)
    monkeypatch.setattr(main, "STRUCTURAL_CORRECTION", False, raising=False)
    monkeypatch.setattr(main, "_idempotency_mgr", None, raising=False)
    monkeypatch.setattr(main, "_retraction_intent_extraction_llm",
                        fake_retraction_extraction)
    monkeypatch.setattr(main, "_retraction_semantic_fallback", probe_fallback)

    calls = {"n": 0}

    def selective_connect(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _NoCloseConn(conn)
        return _real_connect(*a, **k)

    monkeypatch.setattr(main.psycopg2, "connect", selective_connect)

    try:
        req = FactCorrectionRequest(
            user_id=user_id, text="forget the pin ghost pet",
            intent="RETRACTION", context_facts=[])
        main.correct_fact(req)
    except Exception:
        pass  # the not-found return path is not the seam; observation already made
    finally:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        _cleanup(dsn, schema=schema_name, user_id=user_id)

    assert llm_seen.get("ran"), "the retraction extraction never ran — pin broken"
    assert "status" in fallback_seen, (
        "the semantic fallback never ran — every cascade step must miss on an empty "
        "tenant schema; if this fires, the cascade short-circuited earlier"
    )
    assert fallback_seen["status"] == pg_ext.TRANSACTION_STATUS_IDLE, (
        f"the lookup-cascade read transaction was still open at ENTRY to the "
        f"retraction semantic fallback on the REQUEST connection "
        f"(transaction_status={fallback_seen['status']}) — release it before the "
        f"fallback's embed + Qdrant network calls; measured 8.03s idle-in-transaction "
        f"live on the running stack"
    )


# ── S8 (FIXTURE INTEGRITY — the round-5 critic gap, arm-independent like the
#    mechanism pin): _seed_user must be collision-proof against a leftover
#    constant-slug row AND the session sweep must self-heal a wedged battery.
#    Pre-fix this file demonstrated the wedge live: round-4 battery RED,
#    UniqueViolation users_slug_key slug='pin' at BOTH correct_fact pins.

def test_pin_fixture_sweep_self_heals_a_wedged_battery():
    dsn = _dsn_or_skip()

    def _plant_wedge():
        """Reproduce the interrupted-battery leftover EXACTLY: a dead user's row
        with the CONSTANT slug (what the pre-hardening _seed_user wrote), an
        orphan provisioning row, and an orphan tenant schema."""
        stale = str(uuid.uuid4())
        c = _real_connect(dsn)
        c.autocommit = True
        try:
            with c.cursor() as cur:
                cur.execute("INSERT INTO public.users (user_id, email, display_name, slug) "
                            "VALUES (%s, %s, 'pin', 'pin')",
                            (stale, f"{stale}@pin"))
                cur.execute("INSERT INTO public.user_provisioning "
                            "(user_id, schema_name, status) "
                            "VALUES (%s, 'faultline_pin_stalewedge', 'provisioning')",
                            (stale,))
                cur.execute("CREATE SCHEMA IF NOT EXISTS faultline_pin_stalewedge")
        finally:
            c.close()
        return stale

    # Phase A — the sweep clears the whole leftover class (schema + rows).
    stale = _plant_wedge()
    _sweep_pin_leftovers(dsn)
    c = _real_connect(dsn)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute("SELECT 1 FROM information_schema.schemata "
                        "WHERE schema_name = 'faultline_pin_stalewedge'")
            assert not cur.fetchone(), "sweep left the orphan tenant schema behind"
            cur.execute("SELECT 1 FROM public.user_provisioning WHERE user_id = %s",
                        (stale,))
            assert not cur.fetchone(), "sweep left the orphan provisioning row behind"
            cur.execute("SELECT 1 FROM public.users WHERE user_id = %s", (stale,))
            assert not cur.fetchone(), "sweep left the stale users row behind"
    finally:
        c.close()

    # Phase B — the seed is collision-proof WITHOUT the sweep: a constant-slug
    # row re-appearing mid-session (a wedge planted after battery start) must not
    # red the battery; fresh seeds get unique slugs.
    _plant_wedge()
    fresh1, fresh2 = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        _seed_user(dsn, fresh1, f"faultline_pin_{fresh1.replace('-', '_')}")
        _seed_user(dsn, fresh2, f"faultline_pin_{fresh2.replace('-', '_')}")
    except Exception as e:
        # surface the exact pre-fix failure shape in the assertion message
        raise AssertionError(
            f"_seed_user collided with a leftover constant-slug row — the round-5 "
            f"critic wedge reproduced: {type(e).__name__}: {str(e)[:160]}")
    c = _real_connect(dsn)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute("SELECT slug FROM public.users WHERE user_id IN (%s, %s)",
                        (fresh1, fresh2))
            slugs = sorted(r[0] for r in cur.fetchall())
    finally:
        c.close()
        _cleanup(dsn, user_id=fresh1)
        _cleanup(dsn, user_id=fresh2)
        _sweep_pin_leftovers(dsn)  # clears the re-planted wedge rows too

    assert len(slugs) == 2 and slugs[0] != slugs[1], (
        f"fresh seeds did not get unique slugs: {slugs!r}"
    )
    assert all(s != "pin" and s.startswith("pin-") for s in slugs), (
        f"fresh seeds must carry unique pin-<hex> slugs, got {slugs!r}"
    )
