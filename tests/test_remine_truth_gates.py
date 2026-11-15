"""TRUTHLIE defect 1 — a retained turn must never be recorded as processed when
nothing was stored, for ANY 200-shaped failure, including shapes that do not exist yet.

WHY THIS FILE EXISTS
────────────────────
The re-mine lane (`reextract_episodic`) used to classify success by HTTP status plus a
list of NAMED 200-shaped failures. /ingest answers at least one more failure as HTTP 200
with `status="error"` in the body (provisioning/schema-context failure — nothing stored);
the lane read it as success, stamped `reextracted_at = now(), extracted_fact_count =
len(edges) > 0`, and the only predicate that ever re-opens a stamped row requires
`extracted_fact_count = 0` — so the turn was closed PERMANENTLY as "successfully
processed" with nothing stored. /extract/rewrite had two more unguarded 200-shaped
failures (`status="error"`, `status="processing"` — the latter has NO edges key and read
as "zero edges = successfully uncastable" → stamped).

The fix gates on the POSITIVE success set instead of naming failures:
  /ingest            → stamp only on status="valid"  (the only success status)
  /extract/rewrite   → edges only from status="success" bodies
  /harvest-spans     → edges only from status-less (or success-marked) bodies; an
                       unrecognized status discards the harvest and falls back to
                       /extract/rewrite, whose own gate decides the row's fate

Anything else — a known failure or a shape not yet invented — raises, the stamp never
runs, and the row is re-mined next cycle. These tests pin that contract, including the
fail-closed direction for UNKNOWN statuses.

PURE tests — no DB, no network, no LLM. `db_conn` and the HTTP seam are fakes.
"""
import pytest

import src.re_embedder.embedder as E


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _FakeCursor:
    def __init__(self, owner):
        self.owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.owner.executed.append((sql, params))

    def fetchall(self):
        return self.owner.rows

    def fetchone(self):
        return (0,)

    @property
    def rowcount(self):
        return 0


class _FakeConn:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.executed = []
        self.commits = 0

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _row(row_id=1, intent="STATEMENT"):
    """(id, raw_text, source_ref, intent, created_at, source) — the drain SELECT shape."""
    return (row_id, f"retained turn number {row_id}", None, intent, None, None)


class _Resp:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


_REWRITE_OK = {"status": "success",
               "edges": [{"subject": "a", "rel_type": "likes", "object": "b"}]}
_INGEST_VALID = {"status": "valid", "committed": 1, "staged": 0, "scalar_committed": 0}


def _stamp_sqls(conn):
    """The terminal 'processed' stamps — UPDATEs that set reextracted_at (the poison-row
    guard stamp is excluded: it is the >30d best-effort pass, a DIFFERENT terminal that
    only fires for month-old rows, which these fixtures never are)."""
    return [s for s, _ in conn.executed
            if s.startswith("UPDATE episodic_log") and "reextracted_at = now()"
            and "INTERVAL '30 days'" not in s]


def _run_row(monkeypatch, *, rewrite_body=None, ingest_body=None):
    """Run ONE retained turn through the drain with route=rewrite and return the conn.

    If rewrite_body is None the extractor is monkeypatched to return edges directly
    (the /ingest seam under test); otherwise the real _reextract_row_edges runs against
    a faked /extract/rewrite response."""
    conn = _FakeConn(rows=[_row()])
    posts = []

    if rewrite_body is None:
        monkeypatch.setattr(
            E, "_reextract_row_edges",
            lambda *a, **k: [{"subject": "a", "rel_type": "likes", "object": "b"}])

    def _post(url, **kw):
        posts.append(url)
        if url.endswith("/ingest"):
            return _Resp(ingest_body if ingest_body is not None else _INGEST_VALID)
        return _Resp(rewrite_body)

    monkeypatch.setattr(E.httpx, "post", _post)
    E.reextract_episodic(conn, "http://backend", user_id="u" * 36, batch_size=5,
                         statement_route="rewrite")
    return conn, posts


# ── 1. THE DEFECT: /ingest 200-shaped failures must NOT stamp ─────────────────

def test_ingest_200_error_body_leaves_the_row_unstamped(monkeypatch):
    """THE LIE. /ingest returns HTTP 200 with status='error' (provisioning/schema
    failure — nothing stored). The old code stamped the row terminally with
    extracted_fact_count = len(edges) > 0; the only re-open predicate requires count=0,
    so the turn was closed forever as processed-when-nothing-was-stored."""
    conn, _ = _run_row(monkeypatch, ingest_body={"status": "error",
                                                 "error": "schema context failed"})
    assert _stamp_sqls(conn) == [], (
        "a 200-shaped ingest FAILURE must never stamp the row as processed — "
        "nothing was stored and the turn must be re-mined next cycle")


def test_ingest_200_ingest_disabled_leaves_the_row_unstamped(monkeypatch):
    """The one previously-guarded failure shape — still closed by the positive gate."""
    conn, _ = _run_row(monkeypatch, ingest_body={"status": "ingest_disabled"})
    assert _stamp_sqls(conn) == []


def test_ingest_200_unknown_future_status_leaves_the_row_unstamped(monkeypatch):
    """FAIL-CLOSED FOR FAILURE SHAPES THAT DO NOT EXIST YET. A status nobody has
    written today must not be trusted as success tomorrow — the gate is the POSITIVE
    set, not a list of known-bad values."""
    conn, _ = _run_row(monkeypatch, ingest_body={"status": "quota_exceeded_future_lane",
                                                 "committed": 0})
    assert _stamp_sqls(conn) == []


def test_ingest_missing_status_leaves_the_row_unstamped(monkeypatch):
    """No status at all is not success either — the body must AFFIRM it."""
    conn, _ = _run_row(monkeypatch, ingest_body={"committed": 3})
    assert _stamp_sqls(conn) == []


def test_ingest_valid_stamps_with_the_extracted_count(monkeypatch):
    """The POSITIVE arm: status='valid' is the only /ingest success status, and it
    stamps exactly as before — the fix must not stop real work from being recorded."""
    conn, _ = _run_row(monkeypatch, ingest_body=_INGEST_VALID)
    stamps = _stamp_sqls(conn)
    assert stamps, "a confirmed-stored ingest must still stamp"
    params = [p for s, p in conn.executed if s in stamps for p in (p or [])]
    assert params and params[0] == 1, "extracted_fact_count must record len(edges)"


# ── 2. /extract/rewrite 200-shaped failures must NOT read as zero-edge success ─

def test_rewrite_200_error_body_raises_instead_of_yielding_edges(monkeypatch):
    with pytest.raises(RuntimeError, match="did not confirm success"):
        _rewrite(monkeypatch, {"status": "error", "error": "boom"})


def _rewrite(monkeypatch, body):
    monkeypatch.setattr(E.httpx, "post", lambda *a, **k: _Resp(body))
    return E._reextract_row_edges("t", "u" * 36, "http://backend", "rewrite")


def test_rewrite_200_processing_lock_body_raises(monkeypatch):
    """The sneakiest shape: the idempotency lock returns 200 with status='processing'
    and NO edges key — the old code read it as zero edges and the caller stamped the
    row 'successfully uncastable'. It must raise instead."""
    with pytest.raises(RuntimeError, match="did not confirm success"):
        _rewrite(monkeypatch, {"status": "processing",
                               "message": "Another request is processing this query"})


def test_rewrite_unknown_future_status_raises(monkeypatch):
    with pytest.raises(RuntimeError, match="did not confirm success"):
        _rewrite(monkeypatch, {"status": "future_failure_shape", "edges": []})


def test_rewrite_missing_status_raises(monkeypatch):
    with pytest.raises(RuntimeError, match="did not confirm success"):
        _rewrite(monkeypatch, {"edges": []})


def test_rewrite_success_body_yields_its_edges(monkeypatch):
    edges = _rewrite(monkeypatch, _REWRITE_OK)
    assert edges and edges[0]["rel_type"] == "likes"


def test_rewrite_degraded_body_carries_the_rate_deferred_reason(monkeypatch):
    """PACE DETECTION SURVIVES THE GATE. The poison-row guard upstream decides
    paced-vs-outage from 'rate_deferred' in the raise text; the reason-carry must
    survive the positive gate or a merely-paced day terminally stamps user turns."""
    with pytest.raises(RuntimeError, match="rate_deferred"):
        _rewrite(monkeypatch, {"status": "degraded", "error": "rate_deferred",
                               "edges": []})


# ── 3. /harvest-spans: unrecognized statuses never yield trusted edges ────────

def _spine_then_rewrite(monkeypatch, spine_body, rewrite_body):
    calls = []

    def _post(url, **kw):
        calls.append(url)
        if url.endswith("/harvest-spans"):
            return _Resp(spine_body)
        return _Resp(rewrite_body)

    monkeypatch.setattr(E.httpx, "post", _post)
    edges = E._reextract_row_edges("t", "u" * 36, "http://backend", "spine")
    return edges, calls


def test_spine_statusless_body_is_the_success_shape(monkeypatch):
    """/harvest-spans success bodies carry NO status key — that contract must keep
    working (the positive set here is 'no status', unlike rewrite's 'success')."""
    edges, calls = _spine_then_rewrite(
        monkeypatch, {"edges": [{"subject": "s", "rel_type": "r", "object": "o"}],
                      "spans": 1}, _REWRITE_OK)
    assert edges and edges[0]["subject"] == "s"
    assert calls[0].endswith("/harvest-spans")
    assert len(calls) == 1, "a successful spine harvest must not fall back to rewrite"


def test_spine_unrecognized_status_discards_edges_and_falls_back(monkeypatch):
    """An unrecognized spine status must not have its edges trusted — the harvest is
    discarded and /extract/rewrite (whose own positive gate decides) runs instead."""
    edges, calls = _spine_then_rewrite(
        monkeypatch,
        {"status": "future_failure_shape",
         "edges": [{"subject": "poison", "rel_type": "x", "object": "y"}], "spans": 1},
        _REWRITE_OK)
    assert calls[-1].endswith("/extract/rewrite"), "must fall back to rewrite"
    assert edges and all(e["subject"] != "poison" for e in edges), (
        "edges from a body that did not affirm success must be discarded")


def test_spine_degraded_raises_with_reason(monkeypatch):
    """The named no-brain guard keeps its reason-carry (paced-vs-outage detection)."""
    monkeypatch.setattr(E.httpx, "post",
                        lambda *a, **k: _Resp({"status": "degraded",
                                               "extraction_degraded": True,
                                               "error": "rate_deferred", "edges": []}))
    with pytest.raises(RuntimeError, match="rate_deferred"):
        E._reextract_row_edges("t", "u" * 36, "http://backend", "spine")


# ── 4. End-to-end: the failed row is picked up again on the NEXT cycle ─────────

def test_failed_row_is_returned_by_the_next_eligibility_scan(monkeypatch):
    """'Unstamped' must mean 're-picked'. The eligibility scan is
    `WHERE reextracted_at IS NULL ...` — this pins the loop closes: run the drain
    against a 200-error ingest, then assert the SELECT the NEXT run would issue still
    finds the row (no stamp was written for it)."""
    conn, _ = _run_row(monkeypatch, ingest_body={"status": "error", "error": "x"})
    assert _stamp_sqls(conn) == []
    # The scan the next cycle issues — the stamp columns must still be NULL, which the
    # absence of any stamp UPDATE above guarantees; and the scan itself must not have
    # been narrowed (flag OFF ⇒ the shipped predicate).
    scan = [s for s, _ in conn.executed if "FROM episodic_log" in s and "SELECT id" in s]
    assert scan and "reextracted_at IS NULL" in scan[0]
