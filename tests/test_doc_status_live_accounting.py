"""BUG-04 - document_status / retry_document accounting mismatch: the pins.

THE STORED SNAPSHOTS ARE THE LIE; THE LEDGER IS THE TRUTH. 'facts_staged' /
'chunks_failed' are written ONCE at finalize ('embedder.py::drain_pending_documents'),
so during a drain or a re-mine they freeze for hours while the per-chunk ledger
('documents.chunk_state') moves every DOC_CHUNK_STATE_FLUSH results. Measured on prod:
docs 15/16 reported owed=44/52, kept=0 with 649/961 staged facts (frozen ledger), while
doc 13 was healthy (owed 1 / kept 94).

Pinned here:

  - LIVE accounting  - kept/owed/failed computed from the ledger AT READ TIME;
  - classification   - 'live' / 'frozen_signature' (empty ledger BUT staged facts:
    counters frozen/legacy, NOT genuinely never retained) / 'never_retained';
  - MEASURED ETA     - chunks/min from the ledger completion stamps ("t", stamped
    by '_chunk_terminal_entry' on both drain lanes) replaces the fixed per-chunk
    constant; no stamps -> the historical constant, byte-for-byte;
  - SURFACE HONESTY  - status carries kept/owed/accounting; retry annotates the
    frozen signature and the MCP tool says so in plain words.

House style: DB-less, monkeypatched, no live stack.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

import src.api.main as main  # noqa: E402
import src.re_embedder.embedder as embedder  # noqa: E402


# ---------------------------------------------------------------------------
# A - the pure accounting helper (live-computation + frozen signature)
# ---------------------------------------------------------------------------

def test_accounting_live_class_doc13_healthy_shape():
    """Doc-13-healthy: 95 chunks, 94 done, 0 failed -> owed 1, class live."""
    ledger = {str(i): {"s": "done", "c": 2, "g": 9} for i in range(94)}
    acct = main._doc_chunk_accounting(95, ledger, facts_staged=1682)
    assert acct["kept"] == 94
    assert acct["failed"] == 0
    assert acct["owed"] == 1
    assert acct["accounting"] == "live"


def test_accounting_frozen_signature_docs_15_16_shape():
    """THE recurrence: owed==total AND kept==0 while staged facts EXIST - the ledger
    is frozen/legacy, NOT a genuinely never-retained document."""
    acct15 = main._doc_chunk_accounting(44, {}, facts_staged=649)
    assert acct15["owed"] == 44 and acct15["kept"] == 0
    assert acct15["accounting"] == "frozen_signature"
    acct16 = main._doc_chunk_accounting(52, {}, facts_staged=961)
    assert acct16["accounting"] == "frozen_signature"


def test_accounting_never_retained_is_the_honest_empty():
    """Empty ledger AND no staged facts (doc-22 shape): genuinely nothing extracted."""
    acct = main._doc_chunk_accounting(4, {}, facts_staged=0)
    assert acct["owed"] == 4 and acct["kept"] == 0
    assert acct["accounting"] == "never_retained"


def test_accounting_partial_ledger_is_live_not_frozen():
    """ANY terminal entry makes the ledger trustworthy (the mid-drain class). `owed`
    is the RETRY semantics — total minus kept, because a retry re-reads failed chunks
    too; `unread` is the drain-progress remainder."""
    ledger = {"0": {"s": "done", "c": 1, "g": 5}, "1": {"s": "failed", "a": 2, "e": "t/o"}}
    acct = main._doc_chunk_accounting(6, ledger, facts_staged=5)
    assert acct["kept"] == 1 and acct["failed"] == 1
    assert acct["owed"] == 5, "retry would re-read failed chunks too (total - kept)"
    assert acct["unread"] == 4, "drain progress counts only never-reached chunks"
    assert acct["accounting"] == "live"


def test_status_and_retry_owed_semantics_agree():
    """The same row must produce the SAME `owed` on both surfaces (critic round 1, F2):
    status owed == retry owed == total - kept, on every shape."""
    shapes = [
        (44, {}, 649),                                            # docs-15 frozen
        (95, {str(i): {"s": "done", "c": 2, "g": 9} for i in range(94)}, 1682),  # doc-13
        (10, {"0": {"s": "done"}, "1": {"s": "failed", "e": "x"},
              "2": {"s": "failed", "e": "y"}}, 7),                # mixed terminal
        (6, {"0": {"s": "done", "c": 1, "g": 5},
             "1": {"s": "failed", "a": 2, "e": "t/o"}}, 5),      # mid-drain
    ]
    for total, ledger, staged in shapes:
        acct = main._doc_chunk_accounting(total, ledger, staged)
        # the retry endpoint computes: owed = len(chunks) - len(done entries)
        _kept = sum(1 for v in ledger.values()
                    if isinstance(v, dict) and v.get("s") == "done")
        retry_owed = total - _kept
        assert acct["owed"] == retry_owed, (total, staged, acct["owed"], retry_owed)


def test_accounting_survives_string_ledger_and_garbage():
    """A JSON-string ledger parses (psycopg2 may hand back a string); garbage never
    raises - it degrades to the empty-ledger class, decided by the staged witness."""
    ok = json.dumps({"0": {"s": "done", "c": 1, "g": 1}})
    assert main._doc_chunk_accounting(2, ok, 0)["accounting"] == "live"
    assert main._doc_chunk_accounting(2, "not-json{", 9)["accounting"] == "frozen_signature"
    assert main._doc_chunk_accounting(2, "not-json{", 0)["accounting"] == "never_retained"
    assert main._doc_chunk_accounting(2, ["not", "a", "dict"], 9)["accounting"] == \
        "frozen_signature"


def test_accounting_zero_chunk_doc_is_trivially_live():
    acct = main._doc_chunk_accounting(0, {}, facts_staged=99)
    assert acct["owed"] == 0 and acct["accounting"] == "live"


# ---------------------------------------------------------------------------
# B - GET /documents/status surfaces the live accounting
# ---------------------------------------------------------------------------

class _Cur:
    def __init__(self, owner):
        self._owner = owner
        self._pending = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, q, params=None):
        self._pending = list(self._owner.rows)

    def fetchall(self):
        return self._pending

    def fetchone(self):
        return None


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cur(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _status_row(did, total, ledger, stored_failed, staged):
    return (did, "t", "src", "partial", total, stored_failed, 0, staged,
            ledger, None, None, None)


def _status(monkeypatch, rows):
    conn = _Conn(rows)
    monkeypatch.setattr(main.psycopg2, "connect", lambda dsn: conn)
    monkeypatch.setattr(main, "_derive_tenant_schema", lambda uid: "faultline_x")
    return main.documents_status("u" * 36)


def test_status_surfaces_live_kept_owed_and_live_failed(monkeypatch):
    """A populated ledger IS the truth: 'chunks_failed' reports the LIVE count and the
    stale stored snapshot stays visible as 'chunks_failed_stored' (doc 13: column said
    6 from an older episode; the ledger says 94 kept / 1 owed / 0 failed)."""
    ledger = {str(i): {"s": "done", "c": 2, "g": 9} for i in range(94)}
    doc = _status(monkeypatch, [_status_row(13, 95, ledger, 6, 1682)])["documents"][0]
    assert doc["chunks_kept"] == 94
    assert doc["chunks_owed"] == 1
    assert doc["chunks_failed"] == 0, "live ledger failed count, not the frozen column"
    assert doc["chunks_failed_stored"] == 6, "the stored snapshot stays visible"
    assert doc["accounting"] == "live"


def test_status_marks_frozen_signature_not_genuine_empty(monkeypatch):
    """Docs 15/16 on the status surface: owed=all, kept=0, staged facts present -
    must read as 'frozen_signature', never as a clean never-retained doc."""
    doc = _status(monkeypatch, [_status_row(15, 44, {}, 0, 649)])["documents"][0]
    assert doc["chunks_owed"] == 44 and doc["chunks_kept"] == 0
    assert doc["accounting"] == "frozen_signature"
    assert doc["retriable"] is True
    # a FROZEN ledger never downgrades the stored status: owed=all there is untrusted
    assert doc["status_effective"] == doc["status"]


def test_status_effective_downgrades_zombie_ready_on_ledger_evidence(monkeypatch):
    """The docs 10-13 zombie class (critic round 1, F4): a `ready` stamp while a
    TRUSTED ledger says chunks failed or never finished - `status_effective` must
    say `partial` while the stored `status` stays untouched."""
    ledger = {str(i): {"s": "done", "c": 2, "g": 9} for i in range(94)}
    row = (13, "t", "src", "ready", 95, 6, 0, 1682, ledger, "quarantine text",
           None, "2026-11-02T21:48:00")
    doc = _status(monkeypatch, [row])["documents"][0]
    assert doc["status"] == "ready", "stored status is never mutated"
    assert doc["status_effective"] == "partial", "ledger evidence says work remains"
    assert doc["chunks_owed"] == 1 and doc["chunks_unread"] == 1
    row_full = (14, "t", "src", "ready", 95, 0, 0, 1682,
                {str(i): {"s": "done", "c": 2, "g": 9} for i in range(95)},
                None, None, "2026-11-02T21:48:00")
    doc_full = _status(monkeypatch, [row_full])["documents"][0]
    assert doc_full["status_effective"] == "ready"
    assert doc_full["chunks_owed"] == 0 and doc_full["chunks_unread"] == 0


def test_status_carries_counters_as_of_staleness_marker(monkeypatch):
    """critic round 1, F5: facts_staged/facts_committed are FINALIZE-ONLY snapshots;
    `counters_as_of` names the finalize instant they are stamped at (None = never
    finalized - the counters are enqueue-time zeros)."""
    doc = _status(monkeypatch, [_status_row(16, 52, {}, 0, 961)])["documents"][0]
    assert doc["counters_as_of"] is None, "never finalized: no snapshot timestamp"
    row = _status_row(17, 52, {}, 0, 961)[:11] + ("2026-11-03T08:15:00",)
    doc2 = _status(monkeypatch, [row])["documents"][0]
    assert doc2["counters_as_of"] == "2026-11-03T08:15:00"


def test_status_never_retained_and_status_shape_additive(monkeypatch):
    """The empty-and-nothing-extracted doc keeps an honest 'never_retained' class and
    every pre-existing status key keeps its meaning (additive fields only)."""
    doc = _status(monkeypatch, [_status_row(22, 4, {}, 4, 0)])["documents"][0]
    assert doc["accounting"] == "never_retained"
    assert doc["chunks_owed"] == 4 and doc["chunks_kept"] == 0
    assert doc["chunks_failed"] == 4 and doc["chunks_failed_stored"] == 4, \
        "empty ledger: the stored column is all there is, and it is surfaced"
    for key in ("id", "title", "source_ref", "status", "chunk_count", "failed_chunks",
                "skipped_chunks", "facts_committed", "facts_staged", "error",
                "retriable", "created_at", "ready_at"):
        assert key in doc, "status key must not regress: " + key


def test_status_failed_chunks_list_still_names_the_reason(monkeypatch):
    """A populated ledger still renders per-chunk failure evidence exactly as before
    (the R1-F3 skipped-chunk surface rides the same read)."""
    ledger = {"2": {"s": "failed", "a": 1, "e": "boom"},
              "1": {"s": "done", "c": 0, "g": 0, "cls": "cite_dense_reference_block"}}
    doc = _status(monkeypatch, [_status_row(23, 3, ledger, 0, 0)])["documents"][0]
    assert doc["accounting"] == "live"
    assert doc["failed_chunks"][0]["chunk"] == 2
    assert doc["skipped_chunks"] == [{"chunk": 1, "class": "cite_dense_reference_block"}]


# ---------------------------------------------------------------------------
# C - POST /documents/retry: frozen annotation + measured ETA
# ---------------------------------------------------------------------------

class _RetryCursor:
    def __init__(self, owner):
        self._owner = owner
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._owner.statements.append((" ".join(sql.split()), params))
        if sql.strip().startswith("UPDATE documents"):
            self._owner.flips += 1
            self.rowcount = 1

    def fetchall(self):
        return list(self._owner.candidate_rows)

    def fetchone(self):
        return None


class _RetryConn:
    def __init__(self, candidate_rows):
        self.candidate_rows = candidate_rows
        self.statements = []
        self.flips = 0

    def cursor(self):
        return _RetryCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _retry(monkeypatch, candidate_rows):
    # candidate rows are (id, chunks, ledger, facts_staged[, facts_committed]) - the
    # 5th element arrived with the committed-facts witness (critic round 2, F1).
    rows = [tuple(r) + (0,) * (5 - len(r)) for r in candidate_rows]
    conn = _RetryConn(rows)
    monkeypatch.setattr(main.psycopg2, "connect", lambda dsn: conn)
    monkeypatch.setattr(main, "_derive_tenant_schema", lambda uid: "faultline_x")
    out = main.documents_retry(main.DocumentRetryRequest(
        user_id="00000000-0000-4000-8000-0000000000aa", document_id=candidate_rows[0][0]))
    return out, conn


def test_retry_response_carries_frozen_annotation(monkeypatch):
    """The docs-15 shape at retry time: an empty ledger on a doc holding staged facts
    re-ows EVERY chunk AND the response says the ledger is frozen - the retry is the
    repair re-read, never presented as evidence that nothing was retained."""
    out, conn = _retry(monkeypatch, [(15, ["c"] * 44, {}, 649)])
    assert out["status"] == "retry_queued" and out["retried"] == 1
    doc = out["documents"][0]
    assert doc["chunks_owed"] == 44 and doc["chunks_kept"] == 0
    assert doc["accounting"] == "frozen_signature"
    assert out["frozen_signature_docs"] == [15]
    assert "frozen" in out["message"] and "15" in out["message"]
    assert "eta_seconds" in out
    assert conn.flips == 1


def test_retry_populated_ledger_is_live_without_note(monkeypatch):
    """A healthy partial doc (done kept, failed re-owed) reports class live and carries
    NO frozen annotation."""
    ledger = {"0": {"s": "done", "c": 2, "g": 0}, "1": {"s": "failed", "a": 3, "e": "x"}}
    out, _ = _retry(monkeypatch, [(3, ["c"] * 4, ledger, 2)])
    assert out["status"] == "retry_queued"
    assert out["documents"][0]["chunks_kept"] == 1
    assert out["documents"][0]["chunks_owed"] == 3
    assert out["documents"][0]["accounting"] == "live"
    assert "frozen_signature_docs" not in out


def _stamped_ledger(n_done, gap_s):
    now = time.time()
    return {str(i): {"s": "done", "c": 1, "g": 1, "t": now - (n_done - 1 - i) * gap_s}
            for i in range(n_done)}


def test_retry_eta_uses_measured_rate_when_stamps_exist(monkeypatch):
    """MEASURED ETA: the doc own stamped completions set the per-chunk cost. Ten
    completions 3s apart -> 20 chunks/min -> 3s/chunk; the ETA must DROP below the
    constant estimate. No stamps -> the historical constant, byte-for-byte."""
    ledger = _stamped_ledger(10, 3.0)
    out, _ = _retry(monkeypatch, [(7, ["c"] * 44, ledger, 10)])
    owed = out["documents"][0]["chunks_owed"]          # 44 - 10 kept = 34
    assert owed == 34
    expected = (main._DOC_ETA_POLL_INTERVAL + main._DOC_ETA_STARTUP_MARGIN
                + owed * (60.0 / 20.0))
    expected = int(((expected + 4) // 5) * 5)
    assert out["eta_seconds"] == expected
    constant = (main._DOC_ETA_POLL_INTERVAL + main._DOC_ETA_STARTUP_MARGIN
                + owed * main._DOC_ETA_PER_CHUNK_SECONDS)
    constant = int(((constant + 4) // 5) * 5)
    assert out["eta_seconds"] < constant, "a measured fast rate must beat the constant"


def test_retry_eta_falls_back_to_constant_without_stamps(monkeypatch):
    """The pre-stamp fleet (docs 13-16 as they exist today): no "t" anywhere -> the
    constant estimate, exactly as before the fix (no worse, no fiction upgrade)."""
    ledger = {str(i): {"s": "done", "c": 2, "g": 9} for i in range(94)}
    out, _ = _retry(monkeypatch, [(13, ["c"] * 95, ledger, 1682)])
    owed = out["documents"][0]["chunks_owed"]  # 1
    expected = (main._DOC_ETA_POLL_INTERVAL + main._DOC_ETA_STARTUP_MARGIN
                + owed * main._DOC_ETA_PER_CHUNK_SECONDS)
    expected = int(((expected + 4) // 5) * 5)
    assert out["eta_seconds"] == expected


# ---------------------------------------------------------------------------
# D - the measured-rate helpers (window, staleness, guards)
# ---------------------------------------------------------------------------

def test_ledger_rate_cpm_measures_last_n_completions():
    ledger = _stamped_ledger(10, 6.0)  # 9 completions over 54s -> 10 cpm
    rate = main._doc_ledger_rate_cpm(ledger)
    assert rate is not None and abs(rate - 10.0) < 1.0


def test_ledger_rate_cpm_none_without_stamps_or_single_stamp():
    assert main._doc_ledger_rate_cpm({}) is None
    assert main._doc_ledger_rate_cpm({"0": {"s": "done", "c": 1, "g": 1}}) is None, \
        "pre-stamp fleet: honestly not measurable"
    assert main._doc_ledger_rate_cpm({"0": {"s": "done", "t": time.time()}}) is None


def test_ledger_rate_cpm_none_on_degenerate_span():
    """critic round 4: identical stamps are NOT a rate. The 1s span floor used to mint
    a fictitious 60 cpm from 2 identical stamps (4 stamps -> 180) — the docstring
    promised None for a degenerate span and the code now honors it."""
    now = time.time()
    same = {"0": {"s": "done", "c": 1, "g": 1, "t": now},
            "1": {"s": "done", "c": 1, "g": 1, "t": now}}
    assert main._doc_ledger_rate_cpm(same) is None
    four = {str(i): {"s": "done", "c": 1, "g": 1, "t": now} for i in range(4)}
    assert main._doc_ledger_rate_cpm(four) is None
    tight = {"0": {"s": "done", "c": 1, "g": 1, "t": now},
             "1": {"s": "done", "c": 1, "g": 1, "t": now + 0.5}}
    assert main._doc_ledger_rate_cpm(tight) == 60.0, \
        "sub-second spans keep the 1s floor: 1 interval / (1s) = 60 cpm"


def test_ledger_rate_cpm_drops_stale_stamps():
    """A drain that PAUSED must not extrapolate its old rate: stamps older than the
    window (relative to the newest) drop out; <2 remaining -> None -> constant."""
    now = time.time()
    w = main._DOC_ETA_RATE_WINDOW_S
    ledger = {
        "0": {"s": "done", "t": now - (w + 600)},   # stale burst - must be ignored
        "1": {"s": "done", "t": now - (w + 540)},
        "2": {"s": "done", "t": now - 30},
    }
    assert main._doc_ledger_rate_cpm(ledger) is None
    fresh = {"0": {"s": "done", "t": now - 60}, "1": {"s": "done", "t": now - 30}}
    rate = main._doc_ledger_rate_cpm(fresh)
    assert rate is not None and rate > 0


def test_seat_rate_cpm_conservative_slowest_and_guards():
    rows = [
        (datetime(2026, 11, 3, 10, 0, 0), datetime(2026, 11, 3, 11, 0, 0), 12),  # 0.2 cpm
        (datetime(2026, 11, 3, 12, 0, 0), datetime(2026, 11, 3, 12, 1, 0), 10),  # 10 cpm
    ]
    rate = main._doc_seat_rate_cpm(rows)
    assert rate is not None and abs(rate - 0.2) < 1e-6, "the SLOWEST recent doc sets it"
    assert main._doc_seat_rate_cpm([]) is None
    assert main._doc_seat_rate_cpm([(None, None, 5)]) is None
    assert main._doc_seat_rate_cpm([
        (datetime(2026, 11, 3, 10, 0, 0), datetime(2026, 11, 3, 10, 0, 0), 5),  # zero span
        rows[0],
    ]) is not None, "a malformed row is skipped, never fatal"


def test_seat_rate_cpm_skips_stall_spans():
    """critic round 1, F3: a finalize span beyond the cap is a PAUSE (brain deferral /
    backlog stall), not a rate - extrapolating it would trade the constant fiction for
    a pause-extrapolation fiction. Skipped; if nothing measurable remains -> None."""
    stall = (datetime(2026, 10, 31, 10, 0, 0), datetime(2026, 11, 3, 10, 0, 0), 44)
    assert (stall[1] - stall[0]).total_seconds() > main._DOC_SEAT_RATE_MAX_SPAN_S
    assert main._doc_seat_rate_cpm([stall]) is None, "a weekend stall must not set the rate"
    healthy = (datetime(2026, 11, 3, 10, 0, 0), datetime(2026, 11, 3, 10, 30, 0), 12)
    rate = main._doc_seat_rate_cpm([stall, healthy])
    assert rate is not None and abs(rate - 0.4) < 1e-6, "only the in-cap doc measures"


def test_eta_measured_rate_and_fallback_are_sound():
    """The ETA contract: measured rate replaces the constant; falsy rate keeps the
    historical estimate byte-for-byte; the poll-interval floor + 5s rounding hold."""
    n = 44
    fallback = main._doc_eta_seconds(n)
    assert fallback == main._doc_eta_seconds(n, rate_cpm=None)
    assert fallback == main._doc_eta_seconds(n, rate_cpm=0)
    fast = main._doc_eta_seconds(n, rate_cpm=30.0)   # 2 s/chunk
    slow = main._doc_eta_seconds(n, rate_cpm=3.0)    # 20 s/chunk
    assert fast < fallback < slow
    for eta in (fallback, fast, slow):
        assert eta >= main._DOC_ETA_POLL_INTERVAL, "never under-shoot the pickup delay"
        assert eta % 5 == 0
    assert main._doc_eta_seconds(0) >= main._DOC_ETA_POLL_INTERVAL


# ---------------------------------------------------------------------------
# E - GET /documents/pending: the seat-level measured rate rides the probe
# ---------------------------------------------------------------------------

class _ProbeCursor:
    def __init__(self, owner):
        self._owner = owner
        self._sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._sql = " ".join(sql.split())

    def fetchone(self):
        if "count(*) FILTER" in self._sql:
            return (2, 20, 0, 0)   # 2 pending docs, 20 chunks remaining, 0 failed
        return None

    def fetchall(self):
        if "started_at, ready_at" in self._sql:
            return self._owner.rate_rows
        return []


class _ProbeConn:
    def __init__(self, rate_rows):
        self.rate_rows = rate_rows

    def cursor(self):
        return _ProbeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def test_pending_probe_eta_uses_measured_seat_rate(monkeypatch):
    """Seat rate = slowest recent finalize (12 chunks / 60 min = 0.2 cpm -> 300 s/chunk):
    the backlog ETA reflects reality instead of the 6 s/chunk fiction."""
    rows = [(datetime(2026, 11, 3, 10, 0, 0), datetime(2026, 11, 3, 11, 0, 0), 12)]
    conn = _ProbeConn(rows)
    monkeypatch.setattr(main.psycopg2, "connect", lambda dsn: conn)
    monkeypatch.setattr(main, "_derive_tenant_schema", lambda uid: "faultline_x")
    out = main.documents_pending("u" * 36)
    expected = (main._DOC_ETA_POLL_INTERVAL + main._DOC_ETA_STARTUP_MARGIN
                + 20 * (60.0 / 0.2))
    expected = int(((expected + 4) // 5) * 5)
    assert out["eta_seconds"] == expected
    assert out["pending"] is True and out["count"] == 2


def test_pending_probe_eta_falls_back_when_rate_not_measurable(monkeypatch):
    conn = _ProbeConn([])   # no finalize evidence yet (fresh seat)
    monkeypatch.setattr(main.psycopg2, "connect", lambda dsn: conn)
    monkeypatch.setattr(main, "_derive_tenant_schema", lambda uid: "faultline_x")
    out = main.documents_pending("u" * 36)
    expected = (main._DOC_ETA_POLL_INTERVAL + main._DOC_ETA_STARTUP_MARGIN
                + 20 * main._DOC_ETA_PER_CHUNK_SECONDS)
    expected = int(((expected + 4) // 5) * 5)
    assert out["eta_seconds"] == expected


class _FooterCursor:
    """Dispatches the probe's THREE reads: the aggregate (fetchone), the live failed-doc
    rows (critic round 1, F1), and the seat-rate finalize evidence."""

    def __init__(self, owner):
        self._owner = owner
        self._sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._sql = " ".join(sql.split())

    def fetchone(self):
        if "count(*) FILTER" in self._sql:
            return (1, 8, 2, 6)   # 1 pending doc; 2 failed docs; stored column says 6
        return None

    def fetchall(self):
        if "COALESCE(chunk_count, 0)" in self._sql:
            return self._owner.failed_rows
        if "started_at, ready_at" in self._sql:
            return []
        return []


class _FooterConn:
    def __init__(self, failed_rows):
        self.failed_rows = failed_rows

    def cursor(self):
        return _FooterCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def test_footer_failed_accounting_agrees_with_documents_status(monkeypatch):
    """critic round 1, F1 (THE MAJOR): the recall footer must not sum the finalize-only
    column while documents_status reports LIVE - that disagreement was the bug's lie
    surviving on the most user-visible surface. Doc 13 shape (stored column 6, ledger
    94 done / 0 failed): footer must say 0 and classify the doc `live`, owed 1; a
    frozen empty-ledger doc falls back to its stored column."""
    ledger13 = {str(i): {"s": "done", "c": 2, "g": 9} for i in range(94)}
    rows = [
        (13, "doc thirteen", "src13", 95, 6, 1682, 0, ledger13),   # live: real failed = 0
        (15, "doc fifteen", "src15", 44, 0, 649, 0, {}),           # frozen: fallback
    ]
    conn = _FooterConn(rows)
    monkeypatch.setattr(main.psycopg2, "connect", lambda dsn: conn)
    monkeypatch.setattr(main, "_derive_tenant_schema", lambda uid: "faultline_x")
    out = main.documents_pending("u" * 36)
    assert out["failed"] == 2                       # doc COUNT stays status-based
    assert out["failed_chunks"] == 0, "stored column said 6; the ledger says 0"
    by_id = {d["id"]: d for d in out["failed_docs"]}
    assert by_id[13]["chunks_failed"] == 0 and by_id[13]["accounting"] == "live"
    assert by_id[13]["chunks_owed"] == 1
    assert by_id[15]["chunks_failed"] == 0
    assert by_id[15]["accounting"] == "frozen_signature"
    assert by_id[15]["chunks_owed"] == 44


def test_footer_failure_keeps_aggregate_values(monkeypatch):
    """The live footer is an upgrade, never a dependency: if the per-doc read fails,
    the aggregate column values rule (old behavior byte-for-byte)."""

    class _BoomCursor(_FooterCursor):
        def execute(self, sql, params=None):
            if "COALESCE(chunk_count, 0)" in " ".join(sql.split()):
                raise RuntimeError("db gone")
            return super().execute(sql, params=params)

    class _BoomConn(_FooterConn):
        def cursor(self):
            return _BoomCursor(self)

    conn = _BoomConn([(13, "t", "s", 95, 6, 1682, 0, {})])   # 8-col: +facts_committed
    monkeypatch.setattr(main.psycopg2, "connect", lambda dsn: conn)
    monkeypatch.setattr(main, "_derive_tenant_schema", lambda uid: "faultline_x")
    out = main.documents_pending("u" * 36)
    assert out["failed"] == 2 and out["failed_chunks"] == 6, "aggregate fallback"
    assert out["failed_docs"] == []


# ---------------------------------------------------------------------------
# F - the write side: terminal entries carry completion stamps
# ---------------------------------------------------------------------------

def test_chunk_terminal_entry_stamps_completion_time():
    done = embedder._chunk_terminal_entry("done", c=3, g=69)
    assert done["s"] == "done" and done["c"] == 3 and done["g"] == 69
    assert isinstance(done["t"], float) and done["t"] > 0
    with_cls = embedder._chunk_terminal_entry("done", c=0, g=0,
                                              cls="cite_dense_reference_block")
    assert with_cls["cls"] == "cite_dense_reference_block"
    failed = embedder._chunk_terminal_entry("failed", reason="boom", attempts=4)
    assert failed["s"] == "failed" and failed["a"] == 4 and failed["e"] == "boom"
    assert isinstance(failed["t"], float) and failed["t"] > 0


def test_chunk_terminal_entry_legacy_shape_compat():
    """The legacy pool never recorded attempts - passing attempts=None keeps the old
    failed shape (no phantom "a" key), so existing readers/pins stay exact."""
    failed = embedder._chunk_terminal_entry("failed", reason="x" * 500)
    assert "a" not in failed
    assert len(failed["e"]) == 200, "reason is truncated exactly as the lanes did"


def test_both_drain_lanes_mint_entries_through_the_stamping_builder():
    """Wiring pin: the queue lane ok/dead/nohandler outcomes AND the legacy pool all
    mint terminal entries through '_chunk_terminal_entry' - a lane that bypasses the
    builder would resurrect the un-stamped (unmeasurable) ledger."""
    src = open(embedder.__file__, encoding="utf-8").read()
    calls = src.count("_chunk_terminal_entry(")
    assert calls >= 5, "expected the def + 4 mint sites, found %d" % calls
    assert src.count("state[idx] = {") == 0, "a raw dict literal mints an un-stamped entry"
    assert src.count("_legacy_state[str(_i)] = {") == 0


# ---------------------------------------------------------------------------
# G - the MCP retry surface says the honest thing
# ---------------------------------------------------------------------------

class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Cli:
    def __init__(self, payload):
        self._payload = payload

    async def post(self, *a, **k):
        return _Resp(self._payload)


@pytest.mark.asyncio
async def test_mcp_retry_tool_says_frozen_counters_in_plain_words(monkeypatch):
    from src.mcp import server as mcp_server

    payload = {"status": "retry_queued", "retried": 1,
               "documents": [{"id": 15, "chunks_owed": 44, "chunks_kept": 0,
                              "accounting": "frozen_signature"}],
               "eta_seconds": 300,
               "frozen_signature_docs": [15]}
    monkeypatch.setattr(mcp_server, "_client", lambda: _Cli(payload))
    out = await mcp_server.retry_document_tool("u" * 36, document_id=15)
    assert out["status"] == "retry_queued" and out["eta_seconds"] == 300
    assert "frozen" in out["message"], "the surface must not call owed=all a clean re-read"
    assert "15" in out["message"]
    assert out["documents"][0]["accounting"] == "frozen_signature"
    # structured passthrough (critic round 1, F8): machine-readable, not prose-only
    assert out["frozen_signature_docs"] == [15]


@pytest.mark.asyncio
async def test_mcp_retry_tool_without_frozen_has_no_structured_key(monkeypatch):
    from src.mcp import server as mcp_server

    payload = {"status": "retry_queued", "retried": 1,
               "documents": [{"id": 3, "chunks_owed": 3, "chunks_kept": 1,
                              "accounting": "live"}],
               "eta_seconds": 100}
    monkeypatch.setattr(mcp_server, "_client", lambda: _Cli(payload))
    out = await mcp_server.retry_document_tool("u" * 36, document_id=3)
    assert "frozen_signature_docs" not in out
    assert "frozen" not in out["message"]


@pytest.mark.asyncio
async def test_mcp_retry_tool_healthy_ledger_gets_no_frozen_note(monkeypatch):
    from src.mcp import server as mcp_server

    payload = {"status": "retry_queued", "retried": 1,
               "documents": [{"id": 3, "chunks_owed": 3, "chunks_kept": 1,
                              "accounting": "live"}],
               "eta_seconds": 100}
    monkeypatch.setattr(mcp_server, "_client", lambda: _Cli(payload))
    out = await mcp_server.retry_document_tool("u" * 36, document_id=3)
    assert "frozen" not in out["message"]


# ---------------------------------------------------------------------------
# H - critic round 2 pins (F1 committed-witness, F2 malformed ledger, F7 done-only rate)
# ---------------------------------------------------------------------------

def test_committed_facts_are_also_a_frozen_witness():
    """critic round 2, F1 (the mirror-image lie): staged=0 + COMMITTED=120 + empty
    ledger must NOT read as never_retained — 'honestly nothing extracted' would be
    the same fiction the other way round. The witness is staged OR committed."""
    acct = main._doc_chunk_accounting(44, {}, facts_staged=0, facts_committed=120)
    assert acct["accounting"] == "frozen_signature", acct
    assert acct["owed"] == 44 and acct["kept"] == 0
    assert main._doc_chunk_accounting(4, {}, 0, 0)["accounting"] == "never_retained", \
        "staged=0 AND committed=0 stays the honest empty"


def test_status_surfaces_committed_witness_and_malformed_ledger_is_quarantined(monkeypatch):
    """critic round 2, F1+F2 on the status surface: (a) a committed-facts doc reads
    frozen_signature, never_retained; (b) ONE malformed ledger row (a JSON list) is
    quarantined by the shared coercion instead of soft-failing the WHOLE seat's
    payload — the healthy doc's accounting must survive it."""
    row = (41, "committed doc", "src41", "partial", 44, 0, 120, 0, {},
           None, None, None)
    bad = (42, "bad ledger", "src42", "partial", 3, 0, 0, 0, "[1,2]",
           None, None, None)
    good = _status_row(43, 3, {"0": {"s": "done", "c": 1, "g": 1}}, 0, 5)
    docs = {d["id"]: d for d in _status(monkeypatch, [row, bad, good])["documents"]}
    assert docs[41]["accounting"] == "frozen_signature"
    assert docs[41]["chunks_owed"] == 44
    assert docs[42]["accounting"] in ("frozen_signature", "never_retained"), \
        "malformed ledger reads as the empty-ledger class, like the helper pins it"
    assert docs[43]["accounting"] == "live", "the healthy doc must survive the bad row"


def test_ledger_rate_counts_done_completions_only():
    """critic round 2, F7: failed stamps are NOT rate progress. A wall of instant
    failures must not shrink the ETA of a retry that re-reads exactly those chunks:
    failed-only -> None -> the conservative constant; mixed ledger -> the rate is
    computed from the done completions alone."""
    now = time.time()
    failed_only = {str(i): {"s": "failed", "e": "x", "t": now - i * 3.0}
                   for i in range(10)}
    assert main._doc_ledger_rate_cpm(failed_only) is None
    mixed = dict(failed_only)
    mixed["d0"] = {"s": "done", "c": 1, "g": 1, "t": now - 30.0}
    mixed["d1"] = {"s": "done", "c": 1, "g": 1, "t": now - 0.0}
    rate = main._doc_ledger_rate_cpm(mixed)
    assert rate is not None and rate > 0
    # 1 interval over 30s = 2 cpm — the done-only rate, not inflated by the failures
    assert abs(rate - 2.0) < 0.5
