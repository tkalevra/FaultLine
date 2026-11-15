"""CTIER increment 1 — the retained-turn tier (episodic_log) must actually DRAIN.

WHY THIS FILE EXISTS
`episodic_log` retains every turn verbatim before extraction, and it has exactly ONE
consumer: `reextract_episodic`. Nothing reads it at query time. So the drain rate IS the
tier's usefulness — a net that drains at 5 rows per ~13-minute cycle is decorative.

WHAT IS PINNED HERE
  1. FLAG-OFF IS BYTE-IDENTICAL. `REEXTRACT_BACKLOG_DRAIN` default OFF must reproduce the
     shipped SQL exactly — the literal `INTERVAL '1 hour'`, no extra params, the caller's
     batch_size verbatim, and NO backlog-count query (flag-OFF must not add a round-trip).
  2. FLAG-ON behaviour: parameterised age gate, backlog-proportional batch, hard ceiling.
  3. STORM BOUND: consecutive row failures abort the batch instead of burning the whole
     batch against a sick brain.
  4. PROVENANCE UNTOUCHED: `source="reextract"` still lands `llm_inferred` on every edge.

PURE tests — no DB, no network, no LLM. `db_conn` and the HTTP seams are fakes.
"""
import types

import pytest

import src.re_embedder.embedder as E


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _FakeCursor:
    """Records every execute() so the test can assert on the SQL actually issued."""

    def __init__(self, owner):
        self.owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.owner.executed.append((sql, params))
        self.owner.last_sql = sql

    def fetchall(self):
        return self.owner.rows

    def fetchone(self):
        return (self.owner.backlog,)

    @property
    def rowcount(self):
        return 0


class _FakeConn:
    def __init__(self, rows=None, backlog=0):
        self.rows = rows if rows is not None else []
        self.backlog = backlog
        self.executed = []
        self.last_sql = ""
        self.commits = 0

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _select_sql(conn):
    """The episodic_log SELECT (the eligibility scan), not the search_path/count calls."""
    for sql, params in conn.executed:
        if "FROM episodic_log" in sql and "SELECT id" in sql:
            return sql, params
    return None, None


def _count_sqls(conn):
    return [s for s, _ in conn.executed if "count(*)" in s and "episodic_log" in s]


def _rows(n, start=1, source=None):
    """n episodic rows: (id, raw_text, source_ref, intent, created_at, source).

    `source` (the ORIGIN LANE) defaults to None = origin unknown = NOT elevatable, which
    keeps every test in this file on the legacy "reextract" lane exactly as before
    REEXTRACT_PRESERVE_PROVENANCE existed. Provenance preservation is pinned separately
    in tests/test_restated_provenance_preserved.py.
    """
    return [(start + i, f"turn number {start + i}", None, "STATEMENT", None, source)
            for i in range(n)]


# ── 1. FLAG OFF IS BYTE-IDENTICAL ─────────────────────────────────────────────

def test_flag_defaults_off():
    """The flag must ship OFF so the baseline benchmark measures today's behaviour."""
    assert E.REEXTRACT_BACKLOG_DRAIN is False


def test_age_gate_flag_off_is_the_literal_shipped_interval(monkeypatch):
    monkeypatch.setattr(E, "REEXTRACT_BACKLOG_DRAIN", False)
    sql, params = E._reextract_age_interval_sql()
    assert sql == "created_at < now() - INTERVAL '1 hour'"
    assert params == ()


def test_effective_batch_flag_off_returns_base_without_touching_db(monkeypatch):
    """Flag OFF must not even COUNT — no added round-trip on the default path."""
    monkeypatch.setattr(E, "REEXTRACT_BACKLOG_DRAIN", False)
    conn = _FakeConn(backlog=10_000)
    assert E._reextract_effective_batch(conn, 5, "created_at < now()", ()) == 5
    assert conn.executed == []


def test_reextract_flag_off_issues_the_shipped_sql(monkeypatch):
    """End-to-end on the drain body: flag OFF → literal 1-hour gate, LIMIT = batch_size,
    exactly one bound parameter (the limit), and no backlog count."""
    monkeypatch.setattr(E, "REEXTRACT_BACKLOG_DRAIN", False)
    conn = _FakeConn(rows=[])
    E.reextract_episodic(conn, "http://backend", user_id="u" * 36, batch_size=5)

    sql, params = _select_sql(conn)
    assert sql is not None
    assert "INTERVAL '1 hour'" in sql
    assert params == (5,)
    assert _count_sqls(conn) == []


# ── 2. FLAG ON — tunable gate + proportional batch ────────────────────────────

def test_age_gate_flag_on_is_parameterised(monkeypatch):
    monkeypatch.setattr(E, "REEXTRACT_BACKLOG_DRAIN", True)
    monkeypatch.setattr(E, "_REEXTRACT_MIN_AGE_MINUTES", 15)
    sql, params = E._reextract_age_interval_sql()
    assert "INTERVAL '1 hour'" not in sql
    assert params == (15,)


def test_effective_batch_scales_with_backlog(monkeypatch):
    """358 eligible rows (the worst tenant measured on pre-prod) at fraction 0.10 → 36,
    clamped by the ceiling."""
    monkeypatch.setattr(E, "REEXTRACT_BACKLOG_DRAIN", True)
    monkeypatch.setattr(E, "_REEXTRACT_DRAIN_FRACTION", 0.10)
    monkeypatch.setattr(E, "_REEXTRACT_MAX_BATCH", 25)
    conn = _FakeConn(backlog=358)
    assert E._reextract_effective_batch(conn, 5, "created_at < now()", ()) == 25


def test_effective_batch_ceiling_is_hard(monkeypatch):
    """The ceiling is the bill — an unbounded backlog must not buy an unbounded batch."""
    monkeypatch.setattr(E, "REEXTRACT_BACKLOG_DRAIN", True)
    monkeypatch.setattr(E, "_REEXTRACT_DRAIN_FRACTION", 1.0)
    monkeypatch.setattr(E, "_REEXTRACT_MAX_BATCH", 25)
    conn = _FakeConn(backlog=1_000_000)
    assert E._reextract_effective_batch(conn, 5, "created_at < now()", ()) == 25


def test_effective_batch_caught_up_tenant_keeps_base(monkeypatch):
    """No backlog → no extra spend. The proportional budget lands only where it is needed."""
    monkeypatch.setattr(E, "REEXTRACT_BACKLOG_DRAIN", True)
    conn = _FakeConn(backlog=3)
    assert E._reextract_effective_batch(conn, 5, "created_at < now()", ()) == 5


def test_effective_batch_failsafe_never_grows_on_error(monkeypatch):
    """A failed count must degrade to the BASE batch, never to a larger one."""
    monkeypatch.setattr(E, "REEXTRACT_BACKLOG_DRAIN", True)

    class _Boom(_FakeConn):
        def cursor(self):
            raise RuntimeError("db down")

    assert E._reextract_effective_batch(_Boom(), 5, "created_at < now()", ()) == 5


# ── 3. STORM BOUND ────────────────────────────────────────────────────────────

def _all_rows_fail(monkeypatch, calls):
    def _boom(raw_text, user_id, backend_url, statement_route, preserve_source=None):
        calls.append(raw_text)
        raise RuntimeError("timed out")
    monkeypatch.setattr(E, "_reextract_row_edges", _boom)


def test_storm_abort_stops_the_batch_after_n_consecutive_failures(monkeypatch):
    """A sick brain fails SLOW (60 s timeout each). Flag ON must stop after N in a row
    instead of paying batch_size × 60 s per tenant per cycle."""
    monkeypatch.setattr(E, "REEXTRACT_BACKLOG_DRAIN", True)
    monkeypatch.setattr(E, "_REEXTRACT_FAIL_ABORT", 3)
    calls = []
    _all_rows_fail(monkeypatch, calls)

    conn = _FakeConn(rows=_rows(20), backlog=20)
    E.reextract_episodic(conn, "http://backend", user_id="u" * 36, batch_size=20)
    assert len(calls) == 3, f"expected abort after 3 consecutive failures, made {len(calls)} calls"


def test_storm_abort_is_flag_gated_off_by_default(monkeypatch):
    """Flag OFF must keep grinding the whole batch — byte-identical to today, including
    today's cost profile. The abort is NEW behaviour and must not leak into the baseline."""
    monkeypatch.setattr(E, "REEXTRACT_BACKLOG_DRAIN", False)
    calls = []
    _all_rows_fail(monkeypatch, calls)

    conn = _FakeConn(rows=_rows(8))
    E.reextract_episodic(conn, "http://backend", user_id="u" * 36, batch_size=8)
    assert len(calls) == 8


def test_a_success_resets_the_failure_run(monkeypatch):
    """Intermittent failures are NOT a storm — only a consecutive run backs off."""
    monkeypatch.setattr(E, "REEXTRACT_BACKLOG_DRAIN", True)
    monkeypatch.setattr(E, "_REEXTRACT_FAIL_ABORT", 3)
    seen = []

    def _flaky(raw_text, user_id, backend_url, statement_route, preserve_source=None):
        seen.append(raw_text)
        # fail, fail, succeed, fail, fail, succeed, ... never 3 in a row
        if len(seen) % 3 == 0:
            return []
        raise RuntimeError("timed out")

    monkeypatch.setattr(E, "_reextract_row_edges", _flaky)
    conn = _FakeConn(rows=_rows(9), backlog=9)
    E.reextract_episodic(conn, "http://backend", user_id="u" * 36, batch_size=9)
    assert len(seen) == 9, "an intermittent failure pattern must not trip the storm abort"


# ── 4. PROVENANCE IS UNTOUCHED (deliberate — do not "fix") ────────────────────

def test_reextract_edges_are_forced_llm_inferred(monkeypatch):
    """The NOT-ELEVATABLE lane: with no known user-stated origin (`preserve_source` unset),
    every edge is still FORCED to llm_inferred — a spine edge arrives stamped user_stated
    and the /ingest router preserves a carried provenance, so `source="reextract"` alone
    would not hold it down.

    ⚠️ The original docstring justified this as "a re-derivation lacks the live turn's
    context, so it is inference, not user testimony." That reasoning was OVERRULED by the
    owner (see REEXTRACT_PRESERVE_PROVENANCE) because it conflated EXTRACTION QUALITY with
    PROVENANCE. The ASSERTION below is unchanged and still load-bearing — it now guards the
    half that must never elevate: unknown / non-user-stated origins. What survives from the
    old note is the part that was always right: draining FASTER must never launder
    provenance. Rate and authority stay orthogonal.
    """
    class _Resp:
        """The REAL /extract/rewrite success shape — status="success" is the only
        success status that endpoint returns, and the re-mine gates on it."""
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "success",
                    "edges": [
                        {"subject": "a", "rel_type": "likes", "object": "b",
                         "fact_provenance": "user_stated"},
                    ]}

    monkeypatch.setattr(E.httpx, "post", lambda *a, **k: _Resp())
    edges = E._reextract_row_edges("some text", "u" * 36, "http://backend", "rewrite")
    assert edges and all(e["fact_provenance"] == "llm_inferred" for e in edges)


# ── 5. INCREMENT 2 — ontology-regrowth re-eligibility ─────────────────────────
#
# The tier's whole advantage over an embedding is that a retained turn can be RE-READ
# against a GROWN ontology. Today a zero-edge turn is stamped terminal and frozen out
# forever (measured: 102 of 542 drained turns on pre-prod). These pin the re-opening —
# and pin that it re-opens ONLY on evidence of growth, never on a timer.

def test_regrowth_flag_defaults_off():
    assert E.REEXTRACT_ONTOLOGY_REGROWTH is False


def test_regrowth_predicate_flag_off_is_the_shipped_predicate(monkeypatch):
    monkeypatch.setattr(E, "REEXTRACT_ONTOLOGY_REGROWTH", False)
    sql, params = E._regrowth_predicate_sql(500)
    assert sql == "reextracted_at IS NULL"
    assert params == ()


def test_regrowth_predicate_without_a_mark_is_the_shipped_predicate(monkeypatch):
    """No usable place count (count failed / migration 197 absent) → today's behaviour.
    Fail-safe must never manufacture a retry it has no evidence for."""
    monkeypatch.setattr(E, "REEXTRACT_ONTOLOGY_REGROWTH", True)
    sql, params = E._regrowth_predicate_sql(None)
    assert sql == "reextracted_at IS NULL"
    assert params == ()


def test_regrowth_predicate_reopens_only_zero_edge_rows(monkeypatch):
    """A turn that DID cast stays terminal — re-mining a successful cast is pure churn
    and is exactly the continuous-update degradation arXiv 2605.12978 measures."""
    monkeypatch.setattr(E, "REEXTRACT_ONTOLOGY_REGROWTH", True)
    sql, params = E._regrowth_predicate_sql(120)
    assert "extracted_fact_count = 0" in sql
    assert "reextracted_at IS NULL" in sql
    assert "reextract_attempts < %s" in sql
    assert params == (E._REEXTRACT_MAX_ATTEMPTS, 120, E._REEXTRACT_MIN_GROWTH)


def test_walkable_place_count_excludes_pending_placement(monkeypatch):
    """VERIFY THE READ. A rel with category='pending_placement' is never appended to
    entity_taxonomies.rel_types_defining_group, so the query path's rel_type allowlist
    can never admit it — a fact filed under it does not come back from a scoped recall.
    The growth mark must therefore count only WALKABLE places, or the lane would re-mine
    turns into a lane with no reader."""
    conn = _FakeConn(backlog=42)
    assert E._tenant_walkable_place_count(conn) == 42
    sql, params = conn.executed[0]
    assert "category IS DISTINCT FROM %s" in sql
    assert params == (E._CATEGORY_PENDING_RE,)
    assert "entity_taxonomies" in sql


def test_walkable_place_count_failsafe_returns_none(monkeypatch):
    class _Boom(_FakeConn):
        def cursor(self):
            raise RuntimeError("no such column")

    assert E._tenant_walkable_place_count(_Boom()) is None


def test_regrowth_off_stamps_with_the_shipped_update(monkeypatch):
    """Flag OFF must issue the ORIGINAL two-column UPDATE — no new columns touched, so
    the lane is inert on a tenant that has not run migration 197."""
    monkeypatch.setattr(E, "REEXTRACT_ONTOLOGY_REGROWTH", False)
    monkeypatch.setattr(E, "_reextract_row_edges", lambda *a, **k: [])
    conn = _FakeConn(rows=_rows(1))
    E.reextract_episodic(conn, "http://backend", user_id="u" * 36, batch_size=5)

    updates = [s for s, _ in conn.executed if s.startswith("UPDATE episodic_log")]
    assert updates, "expected a stamp"
    assert "reextract_ontology_mark" not in updates[0]
    assert "reextract_attempts" not in updates[0]


def test_regrowth_on_records_mark_and_bumps_attempts(monkeypatch):
    monkeypatch.setattr(E, "REEXTRACT_ONTOLOGY_REGROWTH", True)
    monkeypatch.setattr(E, "_tenant_walkable_place_count", lambda *a, **k: 137)
    monkeypatch.setattr(E, "_reextract_row_edges", lambda *a, **k: [])
    conn = _FakeConn(rows=_rows(1))
    E.reextract_episodic(conn, "http://backend", user_id="u" * 36, batch_size=5)

    updates = [(s, p) for s, p in conn.executed if s.startswith("UPDATE episodic_log")]
    assert updates
    sql, params = updates[0]
    assert "reextract_ontology_mark = %s" in sql
    assert "reextract_attempts = reextract_attempts + 1" in sql
    assert 137 in params


def test_regrowth_on_widens_the_eligibility_scan(monkeypatch):
    monkeypatch.setattr(E, "REEXTRACT_ONTOLOGY_REGROWTH", True)
    monkeypatch.setattr(E, "_tenant_walkable_place_count", lambda *a, **k: 137)
    conn = _FakeConn(rows=[])
    E.reextract_episodic(conn, "http://backend", user_id="u" * 36, batch_size=5)

    sql, params = _select_sql(conn)
    assert "extracted_fact_count = 0" in sql
    # Params order must be eligibility → age → limit.
    assert params[-1] == 5
    assert 137 in params


def test_increments_are_independent(monkeypatch):
    """Each increment must be measurable ALONE against a baseline. Increment 2 ON with
    increment 1 OFF must keep the shipped 1-hour gate and the caller's batch size."""
    monkeypatch.setattr(E, "REEXTRACT_BACKLOG_DRAIN", False)
    monkeypatch.setattr(E, "REEXTRACT_ONTOLOGY_REGROWTH", True)
    monkeypatch.setattr(E, "_tenant_walkable_place_count", lambda *a, **k: 137)
    conn = _FakeConn(rows=[])
    E.reextract_episodic(conn, "http://backend", user_id="u" * 36, batch_size=5)

    sql, params = _select_sql(conn)
    assert "INTERVAL '1 hour'" in sql          # increment 1 inert
    assert "extracted_fact_count = 0" in sql   # increment 2 active
    assert params[-1] == 5
    assert _count_sqls(conn) == []             # no backlog-count round-trip


# ── 6. THE PREMISE ITSELF — "capture is not the problem" must be VERIFIED ─────
#
# Every drop downstream is justified by "the raw turn is already retained". That is only
# true if the episodic write actually landed. /episodic/append is soft-fail and answers
# HTTP 200 with a status BODY, and _episodic_capture never inspected it — so a soft-failing
# tenant lost the turn AND the residue with nothing louder than an un-raised 200.
# Observability only: these pin that a non-capture is LOUD, and that capture stays
# non-fatal (a failed net must never break the caller's turn).

@pytest.mark.asyncio
async def test_episodic_capture_soft_error_is_loud(monkeypatch):
    import src.mcp.server as S

    crits = []
    monkeypatch.setattr(S, "_log_crit", lambda ev, msg: crits.append((ev, msg)))

    class _R:
        status_code = 200

        def json(self):
            return {"status": "soft_error", "id": None}

    async def _post(*a, **k):
        return _R()

    monkeypatch.setattr(S, "_http_client", types.SimpleNamespace(post=_post))
    await S._episodic_capture("some turn", "u" * 36, "STATEMENT")
    assert crits and crits[0][0] == "episodic_capture_missed"


@pytest.mark.asyncio
async def test_episodic_capture_transport_failure_is_loud_but_not_fatal(monkeypatch):
    import src.mcp.server as S

    crits = []
    monkeypatch.setattr(S, "_log_crit", lambda ev, msg: crits.append((ev, msg)))

    async def _post(*a, **k):
        raise RuntimeError("connection refused")

    warns = []
    monkeypatch.setattr(S, "_log_warn", lambda ev, msg: warns.append((ev, msg)))
    queued = []
    async def _queue(body):
        queued.append(body); return True
    monkeypatch.setattr(S, "_queue_episodic_reappend", _queue)
    monkeypatch.setattr(S, "_http_client", types.SimpleNamespace(post=_post))
    # MUST NOT raise — the safety net failing must never break the caller's turn.
    await S._episodic_capture("some turn", "u" * 36, "STATEMENT")
    # first-touch-cold-path (gap C, rounds 2→4): a transport failure is "sent, unknown". The
    # seam classifies UNVERIFIED (a WARN, never a CRITICAL) and queues an idempotent re-append
    # with the SAME turn_key on the drain — whose answer is the only thing that settles it.
    assert crits == [], crits
    assert warns and warns[0][0] == "episodic_capture_unverified", warns
    assert len(queued) == 1 and queued[0]["turn_key"] and queued[0]["raw_text"] == "some turn"


@pytest.mark.asyncio
async def test_episodic_capture_success_is_silent(monkeypatch):
    import src.mcp.server as S

    crits = []
    monkeypatch.setattr(S, "_log_crit", lambda ev, msg: crits.append((ev, msg)))

    class _R:
        status_code = 200

        def json(self):
            return {"status": "stored", "id": 7}

    async def _post(*a, **k):
        return _R()

    monkeypatch.setattr(S, "_http_client", types.SimpleNamespace(post=_post))
    await S._episodic_capture("some turn", "u" * 36, "STATEMENT")
    assert crits == []


@pytest.mark.asyncio
async def test_deliberate_freeze_does_not_cry_wolf(monkeypatch):
    """The freeze switch is operator-chosen and the caller is told explicitly. Visible,
    but not a CRITICAL on every turn — alarm fatigue defeats the point of failing loud."""
    import src.mcp.server as S

    crits = []
    monkeypatch.setattr(S, "_log_crit", lambda ev, msg: crits.append((ev, msg)))

    class _R:
        status_code = 200

        def json(self):
            return {"status": "ingest_disabled", "id": None}

    async def _post(*a, **k):
        return _R()

    monkeypatch.setattr(S, "_http_client", types.SimpleNamespace(post=_post))
    await S._episodic_capture("some turn", "u" * 36, "STATEMENT")
    assert crits == []


def test_retraction_rows_are_never_reingested(monkeypatch):
    """Unchanged contract: re-ingesting a retraction as a statement resurrects the fact."""
    monkeypatch.setattr(E, "REEXTRACT_BACKLOG_DRAIN", True)
    calls = []
    monkeypatch.setattr(
        E, "_reextract_row_edges",
        lambda *a, **k: calls.append(1) or [])
    conn = _FakeConn(rows=[(1, "forget that", None, "RETRACTION", None)], backlog=1)
    E.reextract_episodic(conn, "http://backend", user_id="u" * 36, batch_size=5)
    assert calls == []
