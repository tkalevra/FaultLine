"""Regression pins for the 2026-08-01 production "idle in transaction" incident.

A ``faultline-api`` connection sat ``idle in transaction`` for 10h08m holding
``AccessShareLock`` on ``rel_types`` / ``ontology_evaluations`` with ``backend_xid`` NULL — a
pure READ that never ended its transaction. Tenant deprovision (``DROP SCHEMA … CASCADE``)
queued behind it, and once that exclusive waiter was queued EVERYTHING queued behind it:
``pg_dump`` produced a 0-byte file for 10+ minutes, the metrics collector blocked.

Two defects, both pinned here:

  1. **A read that owns a transaction.** Closing the CURSOR does not end the transaction —
     psycopg2 holds it until commit/rollback/close.
  2. **A read that owns a connection.** ``with psycopg2.connect(dsn) as conn:`` commits but
     does NOT close; the backend survives until GC, which is not a guarantee.

Every metadata overlay now reads through ``src.api.db_read.read_only_connection`` (autocommit
+ readonly + guaranteed ``close()``), so neither is expressible.

Requires a live Postgres. Skips (never silently passes) when ``POSTGRES_DSN`` is unset or
unreachable.
"""

import os

import psycopg2
import psycopg2.extensions as pg_ext
import pytest

from src.api.db_read import read_only_connection, release_read_transaction


DSN = os.environ.get("POSTGRES_DSN")


def _dsn_or_skip():
    if not DSN:
        pytest.skip("POSTGRES_DSN not set — this pin requires a live Postgres")
    try:
        c = psycopg2.connect(DSN, connect_timeout=3)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"POSTGRES_DSN unreachable: {type(e).__name__}: {str(e)[:120]}")
    c.close()
    return DSN


def _backend_state(dsn, pid):
    """Return this backend's pg_stat_activity state, or None when it is gone."""
    probe = psycopg2.connect(dsn)
    probe.autocommit = True
    try:
        with probe.cursor() as cur:
            cur.execute("SELECT state FROM pg_stat_activity WHERE pid = %s", (pid,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        probe.close()


def _my_client_backends(dsn):
    """{state: count} for every backend from THIS host, excluding the probe itself.

    Client-addr scoped so a concurrently-running app container's connections cannot make the
    assertion pass or fail spuriously.
    """
    probe = psycopg2.connect(dsn)
    probe.autocommit = True
    try:
        with probe.cursor() as cur:
            cur.execute("SELECT inet_client_addr()")
            me = cur.fetchone()[0]
            cur.execute(
                "SELECT state, count(*) FROM pg_stat_activity "
                "WHERE client_addr IS NOT DISTINCT FROM %s AND pid <> pg_backend_pid() "
                "GROUP BY 1",
                (me,),
            )
            return dict(cur.fetchall())
    finally:
        probe.close()


# ── the seam itself ───────────────────────────────────────────────────────────────────

def test_read_only_connection_is_autocommit_readonly_and_always_closed():
    dsn = _dsn_or_skip()
    with read_only_connection(dsn) as conn:
        assert conn.autocommit is True, "a read seam must not be able to open a lingering txn"
        with conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            pid = cur.fetchone()[0]
            # Statement finished → NOT in a transaction. This is the whole fix.
            assert conn.get_transaction_status() == pg_ext.TRANSACTION_STATUS_IDLE
        captured = conn
    assert captured.closed != 0, "read_only_connection must close the backend on exit"
    assert _backend_state(dsn, pid) is None, "backend survived the read seam"


def test_read_only_connection_closes_on_exception():
    dsn = _dsn_or_skip()
    pid = None
    with pytest.raises(RuntimeError):
        with read_only_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_backend_pid()")
                pid = cur.fetchone()[0]
            raise RuntimeError("boom")
    assert _backend_state(dsn, pid) is None, "backend survived an exception through the seam"


def test_read_only_connection_refuses_a_write():
    """readonly=True is a real guard, not decoration — a write through the read seam is LOUD."""
    dsn = _dsn_or_skip()
    with read_only_connection(dsn) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
                cur.execute("CREATE TEMP TABLE _fl_readonly_probe (x int)")


# ── the retrofit for caller-owned connections ─────────────────────────────────────────

def test_release_read_transaction_ends_the_production_shape():
    """cursor SELECT → cursor closed → no commit == the prod signature (backend_xid NULL)."""
    dsn = _dsn_or_skip()
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchall()
        cur.close()  # closing the CURSOR does NOT end the transaction
        with conn.cursor() as c2:
            c2.execute("SELECT pg_backend_pid()")
            pid = c2.fetchone()[0]
        assert _backend_state(dsn, pid) == "idle in transaction", (
            "precondition failed: this test must first REPRODUCE the leak it pins"
        )

        assert release_read_transaction(conn, context="test") is True
        assert _backend_state(dsn, pid) == "idle"
    finally:
        conn.rollback()
        conn.close()


def test_release_read_transaction_never_discards_a_write():
    """A transaction with an xid assigned is NOT rolled back — fail loud, never lose data."""
    dsn = _dsn_or_skip()
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE _fl_write_probe (x int)")
            cur.execute("INSERT INTO _fl_write_probe VALUES (1)")
        assert release_read_transaction(conn, context="test") is False
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM _fl_write_probe")
            assert cur.fetchone()[0] == 1, "the pending write was discarded"
    finally:
        conn.rollback()
        conn.close()


def test_the_reembedder_ontology_sweep_no_longer_leaks():
    """THE ROOT CAUSE, pinned against the REAL functions — executable specification.

    ⚠️ FLIPPED 2026-08-01, exactly as this test's own assertion message instructed
    ("if embedder.py grew the barrier, flip this pin to assert the CLEAN state"). It was
    written as a CHARACTERIZATION test asserting the leak EXISTS, while the fix was still
    pending; the barrier has now landed in embedder.py, so the same drive is now the
    REGRESSION: the sweep's read functions must leave the connection CLEAN. If this goes
    red with ``idle in transaction``, a read barrier was removed from the sweep loop — do
    NOT relax the assertion, restore the barrier.


    ``embedder.main()`` opens ONE connection per tenant (``_ont_db``, embedder.py:11287),
    binds the schema, then runs ~12 subsystems on it. Several of them SELECT and then
    ``return stats`` on an empty result WITHOUT commit/rollback:

        evaluate_ontology_candidates          embedder.py:7241  ``if not candidates: return``
        drain_pending_placement_by_morphology embedder.py:7691  ``if not pending_rels: return``
        converge_lifted_synonyms              embedder.py:8186  ``if not novel_rows: return``

    The very next subsystem then makes an **LLM call on that same connection**, so the read
    transaction — and its ``AccessShareLock`` on ``rel_types`` + ``ontology_evaluations`` — is
    held for the entire duration of that HTTP call. This test reproduces exactly the production
    signature (``idle in transaction``, ``backend_xid`` NULL, those two tables locked) and now
    pins that the shipped barrier clears it.

    MEASURED both ways on a real Postgres before this flip: without the barrier the drive
    below left the backend ``idle in transaction`` (``backend_xid`` NULL) holding 11 granted
    AccessShareLocks; with it, ``idle`` and zero locks.
    """
    dsn = _dsn_or_skip()
    pytest.importorskip("psycopg2")
    try:
        from src.re_embedder.embedder import (
            drain_pending_placement_by_morphology,
            evaluate_ontology_candidates,
        )
    except Exception as e:  # pragma: no cover
        pytest.skip(f"re_embedder import unavailable: {type(e).__name__}: {str(e)[:120]}")

    probe = psycopg2.connect(dsn)
    probe.autocommit = True
    try:
        with probe.cursor() as cur:
            cur.execute(
                "SELECT schema_name FROM public.user_provisioning "
                "WHERE status = 'ready' ORDER BY ready_at LIMIT 1"
            )
            row = cur.fetchone()
    finally:
        probe.close()
    if not row:
        pytest.skip("no provisioned tenant schema to sweep")
    schema = row[0]
    dead_llm = "http://127.0.0.1:9/v1/chat/completions"  # never reachable; no LLM is called

    def _locked_tables(pid):
        p = psycopg2.connect(dsn)
        p.autocommit = True
        try:
            with p.cursor() as cur:
                cur.execute(
                    "SELECT coalesce(string_agg(DISTINCT cl.relname, ','), '') FROM pg_locks l "
                    "JOIN pg_class cl ON cl.oid = l.relation "
                    "WHERE l.pid = %s AND cl.relkind = 'r' AND l.mode = 'AccessShareLock'",
                    (pid,),
                )
                return cur.fetchone()[0]
        finally:
            p.close()

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            pid = cur.fetchone()[0]
        conn.commit()

        evaluate_ontology_candidates(conn, dead_llm)
        drain_pending_placement_by_morphology(conn, dsn, schema)

        # THE INVARIANT: this is the state the sweep is in at the instant it would start its
        # LLM work. It must hold NO transaction and NO locks — that is the whole fix.
        state = _backend_state(dsn, pid)
        locked = _locked_tables(pid)
        assert state == "idle", (
            f"the sweep left the connection {state!r} — a read barrier was REMOVED from "
            "embedder.py (or from a fetch `finally`). Restore it; do not relax this pin. "
            "This is the 10h08m production incident's exact signature."
        )
        assert locked == "", f"the sweep is holding locks across its LLM call: {locked!r}"

        # And the barrier itself is idempotent: nothing left to release.
        assert release_read_transaction(conn, context="re_embedder.sweep") is False
    finally:
        conn.rollback()
        conn.close()


def test_release_read_transaction_is_a_noop_when_idle():
    dsn = _dsn_or_skip()
    conn = psycopg2.connect(dsn)
    try:
        assert release_read_transaction(conn, context="test") is False
    finally:
        conn.close()


# ── the real overlay seams, driven under load ─────────────────────────────────────────

def _overlay_drivers(dsn):
    from src.api import (
        linguistic_cue_overlay,
        rel_type_overlay,
        taxonomy_overlay,
        temporal_pattern_overlay,
    )

    return [
        ("rel_type", lambda: rel_type_overlay._fetch_meta(dsn, "public")),
        ("taxonomy", lambda: taxonomy_overlay._fetch_taxonomies(dsn, "public")),
        ("ling_cue", lambda: linguistic_cue_overlay._fetch_cues(dsn, "public", "naming_verb")),
        ("thin_type", lambda: linguistic_cue_overlay._fetch_thin_type_map(dsn, "public")),
        ("keyed_map", lambda: linguistic_cue_overlay._fetch_keyed_map(dsn, "public", "kinship_noun")),
        ("temporal_rel", lambda: temporal_pattern_overlay._fetch_relative_cues(dsn, "public")),
        ("temporal_gate", lambda: temporal_pattern_overlay._fetch_gate_patterns(dsn, "public")),
    ]


@pytest.mark.parametrize("name", [n for n, _ in _overlay_drivers("")])
def test_overlay_fetcher_leaves_no_backend_behind(name):
    """THE pin that fails on the old code.

    The old body was ``with psycopg2.connect(dsn) as conn:`` — which commits and leaves the
    connection OPEN. Capturing the connection the fetcher actually opened and asserting it is
    CLOSED is deterministic: it does not depend on garbage collection, which is exactly the
    thing the old code was (accidentally) relying on.
    """
    dsn = _dsn_or_skip()
    driver = dict(_overlay_drivers(dsn))[name]

    opened = []
    real_connect = psycopg2.connect

    def _spy(*a, **kw):
        conn = real_connect(*a, **kw)
        opened.append(conn)
        return conn

    psycopg2.connect = _spy
    try:
        driver()
    finally:
        psycopg2.connect = real_connect

    assert opened, f"{name}: fetcher opened no connection — driver wired wrong"
    for conn in opened:
        assert conn.closed != 0, (
            f"{name}: the fetcher LEAKED an open backend. "
            "`with psycopg2.connect(...)` commits but does not close — use "
            "src.api.db_read.read_only_connection."
        )


def test_repeated_overlay_reads_leave_zero_idle_in_transaction():
    """Drive every overlay seam under repetition; nothing may linger in ANY state.

    HERMETIC 2026-08-21: the assertion is now an IN-PROCESS connection spy, not a
    pg_stat_activity client-addr scan. The suite runs INSIDE the API container, so the
    client-addr scope counted the LIVE API's backends too — a concurrent /retract/correct
    (measured: a 60s idle-in-transaction backend on a slow brain) failed this test ~1/30
    spuriously. The API-lane defect itself is pinned deterministically by the
    correction/retraction extraction pins below; THIS pin keeps the overlay-reads intent:
    30 rounds through every real fetcher, and every connection the reads opened must be
    closed with no transaction lingering — deterministically, independent of live traffic.
    """
    dsn = _dsn_or_skip()
    drivers = _overlay_drivers(dsn)

    opened = []
    real_connect = psycopg2.connect

    def _spy(*a, **kw):
        conn = real_connect(*a, **kw)
        opened.append(conn)
        return conn

    psycopg2.connect = _spy
    try:
        for _ in range(30):
            for _name, fn in drivers:
                fn()
    finally:
        psycopg2.connect = real_connect

    assert opened, "drivers opened no connections — spy wired wrong"
    lingering = [c for c in opened if c.closed == 0]
    assert not lingering, (
        f"{len(lingering)}/{len(opened)} overlay-read connections left open after "
        f"{30 * len(drivers)} reads — a fetcher is relying on GC"
    )
    intrans = [
        c for c in opened
        if c.closed == 0 and c.info.transaction_status == pg_ext.TRANSACTION_STATUS_INTRANS
    ]
    assert not intrans, (
        f"{len(intrans)} overlay-read connections sat idle-in-transaction"
    )


# ── the API correction lane: no read transaction may ride the brain call ──────────────
#
# MEASURED LIVE 2026-08-21 (pre-prod, deployed door): ONE retract_fact request on a slow
# brain held FOUR backends idle-in-transaction — the rel_types MENU connection for 60
# seconds (this lane's builders below), plus 25s/16s/4s on provisioning/schema-check/facts
# reads. The menu backend's last query was verbatim the SELECT in these builders. That is
# the 2026-08-01 production incident's exact signature (a read whose transaction spans an
# LLM call), survived by the API lane after the re_embedder lane was fixed.
#
# Why this surfaced as an intermittent suite failure: the suite runs INSIDE the API
# container, so the client-addr scope of _my_client_backends() counts the live API's
# backends. A concurrent /retract/correct during a full-suite run made the old
# test_repeated_overlay_reads_leave_zero_idle_in_transaction fail ~1/30 — not noise: the
# test was correctly reporting this defect, gated on concurrent API traffic.


def _inside_llm_txn_pin(builder_name):
    """Drive one extraction builder with a stubbed brain and return the request
    connection's transaction status OBSERVED FROM INSIDE the brain call."""
    import src.api.main as M

    dsn = _dsn_or_skip()
    conn = psycopg2.connect(dsn)
    seen = {}

    def fake_llm(*_a, **_kw):
        seen["status"] = conn.info.transaction_status
        return {"subject": "user", "action": "correct", "confidence": 0.9}

    builder = getattr(M, builder_name)
    real_llm = M.call_llm_with_retry_sync
    M.call_llm_with_retry_sync = fake_llm
    try:
        builder(text="I am not 30 years old, I am 31", user_id="pin",
                context_facts=[], db=conn)
    finally:
        M.call_llm_with_retry_sync = real_llm
        conn.rollback()
        conn.close()
    return seen.get("status")


def test_correction_extraction_holds_no_transaction_across_the_llm_call():
    """The menu SELECT must be RELEASED before the brain call — an AccessShareLock on
    rel_types must never ride an LLM round-trip (measured: 60s on a slow brain)."""
    status = _inside_llm_txn_pin("_unified_correction_extraction_llm")
    assert status == pg_ext.TRANSACTION_STATUS_IDLE, (
        f"the correction menu transaction was still open inside the brain call "
        f"(transaction_status={status}) — release it before the LLM call"
    )


def test_retraction_extraction_holds_no_transaction_across_the_llm_call():
    """Same pin for the retraction builder (the 60s offender was measured on this lane)."""
    status = _inside_llm_txn_pin("_retraction_intent_extraction_llm")
    assert status == pg_ext.TRANSACTION_STATUS_IDLE, (
        f"the retraction menu transaction was still open inside the brain call "
        f"(transaction_status={status}) — release it before the LLM call"
    )
