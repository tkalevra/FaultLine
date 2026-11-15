"""GAUNTLET ROUND-1 REMEDIATION PINS (ticket 06 critic round 1 findings).

Each test pins a defect the round-1 critic found, at the level it can be tested
without a live stack:

  F1 (blocker) — the NL_FILL background submit site wired a name that did not
      exist; source-level wiring pin + the defined-function existence check.
  F2 (major)   — the citation gate false-positived on bracketed-number worker
      logs (reproduced by the critic); log-shape veto pins: timestamps, ISO
      dates, and level tokens never qualify a leading-bracket line, while the
      measured populations (inline survey clutter, reference lists) still do.
  F3 (major)   — the honest zero-yield class was write-only; /documents/status
      must render done-with-cls entries as skipped_chunks.
  F4 (major)   — L1 deterministic-first captures were structurally inert for R2
      remine (kept excluded done-with-edges); the ledger now carries
      cls=l1_deterministic_first and the prepass re-ows exactly those.
  F5 (major)   — honesty amendment only: the pure-paced refund is DEFENSIVE and
      unreachable via today's lanes; the docstring pins that contract.

House style: DB-less, monkeypatched, no live stack.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

import src.api.main as main  # noqa: E402
import src.ingest.document_structure as ds  # noqa: E402
import src.re_embedder.embedder as emb  # noqa: E402
from src.re_embedder import embedder as _emb  # noqa: E402


# ---------------------------------------------------------------------------
# R9 hermeticity fixture — every density/seam knob that citation_density_verdict
# and the seam read LIVE from the environment is pinned to its default for the
# whole module, so an ambient runner env (DOC_CITE_MIN_MARKS=100 / RATIO=5 /
# AFFIL_FRONT_MATTER_MAX=10 — all three critic-measured to redden pins) can
# never spuriously fail the battery. The three per-test affil delenvs below are
# subsumed; they stay as local documentation of the R7/R8 fixes.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _pin_cite_env_defaults(monkeypatch):
    monkeypatch.delenv("DOC_CITE_MIN_MARKS", raising=False)
    monkeypatch.delenv("DOC_CITE_DENSITY_RATIO", raising=False)
    monkeypatch.delenv("DOC_CITE_AFFIL_FRONT_MATTER_MAX", raising=False)


# ---------------------------------------------------------------------------
# F1 — the background lane must submit the REAL function name
# ---------------------------------------------------------------------------
def test_f1_nl_fill_background_wiring_pins_the_real_function():
    src = Path(main.__file__).read_text()
    assert "_fill_nl_background" not in src, (
        "the phantom name is back — the submit would raise NameError, swallowed "
        "by the except, and the cache would never fill")
    m = re.search(r"_NL_FILL_EXECUTOR\.submit\(\s*(\w+)", src)
    assert m is not None, "submit site vanished"
    assert m.group(1) == "_nl_fill_background"
    assert callable(main._nl_fill_background)
    m2 = re.search(r"nl_fill_background_fired[\s\S]{0,220}?novel_rels=len\((\w+)\)", src)
    assert m2 is not None and m2.group(1) == "_misses", (
        "the fired log line must count _misses (the real variable)")


# ---------------------------------------------------------------------------
# F2 — the cite gate must not read bracketed worker logs as references
# ---------------------------------------------------------------------------
def _log_chunk(fmt):
    return chr(10).join(fmt(i) for i in range(8))


def test_f2_worker_log_with_clock_is_not_cite_dense():
    chunk = _log_chunk(lambda i: "[%d] 10:31:0%d worker: connection refused to upstream" % (400 + i, i))
    v = ds.citation_density_verdict(chunk)
    assert v["cite_dense"] is False, "the critic's reproduced false positive"


def test_f2_dated_log_is_not_cite_dense():
    chunk = _log_chunk(lambda i: "[%d] 2026-03-01T10:31:0%d worker: connection refused" % (400 + i, i))
    assert ds.citation_density_verdict(chunk)["cite_dense"] is False


def test_f2_level_token_log_is_not_cite_dense():
    chunk = _log_chunk(lambda i: "[%d] ERROR worker: connection refused" % (400 + i))
    assert ds.citation_density_verdict(chunk)["cite_dense"] is False


def test_f2_inline_survey_clutter_still_dense():
    chunk = _log_chunk(lambda i: "Method X [%d] first defines three types of paths and "
                                  "utilizes rules to retrieve each of these chain types here." % (70 + i))
    v = ds.citation_density_verdict(chunk)
    assert v["cite_dense"] is True  # the measured 175/534 population


def test_f2_reference_list_still_dense():
    refs = chr(10).join([
        "References",
        "[1] M. Okafor et al., Proceedings of the 2026 Conference on Systems, vol. 12, pp. 101-118.",
        "[2] S. Lindqvist, arXiv preprint arXiv:2601.04512 (2026).",
        "[3] T. Nakamura, Journal of Network Operations, vol. 8, pp. 55-72.",
        "[4] A. Brennan, Proceedings of the Annual Forum, 2026, pp. 9-24.",
        "[5] K. Duarte et al., IEEE Transactions on Reliability, vol. 71, no. 4, pp. 1880-1895.",
        "[6] P. Whitfield, ACM Symposium on Cloud Networking, 2025, pp. 301-315.",
    ])
    assert ds.citation_density_verdict(refs)["cite_dense"] is True


# ---------------------------------------------------------------------------
# F3 — /documents/status renders done-with-cls entries as skipped_chunks
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
        # canned rows: the status endpoint runs two SELECTs (probe + fetch) and the
        # fetch is the only fetchall consumer
        self._pending = list(self._owner.rows)

    def fetchall(self):
        return self._pending


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


_DOC_ROW = (41, "litmus doc", "src", "ready", 3, 0, 5, 0,
            {"0": {"s": "done", "c": 2, "g": 1},
             "1": {"s": "done", "c": 0, "g": 0, "cls": "cite_dense_reference_block"},
             "2": {"s": "failed", "a": 1, "e": "boom"}},
            None, None, None)


def test_f3_status_renders_skipped_chunks(monkeypatch):
    conn = _Conn([_DOC_ROW])
    monkeypatch.setattr(main.psycopg2, "connect", lambda dsn: conn)
    monkeypatch.setattr(main, "_derive_tenant_schema", lambda uid: "faultline_x")
    out = main.documents_status("u" * 36, document_id=41)
    doc = out["documents"][0]
    assert doc["skipped_chunks"] == [
        {"chunk": 1, "class": "cite_dense_reference_block"}], (
        "a 100%-cite-dense doc must NOT read byte-identical to a clean read-and-empty")
    assert doc["failed_chunks"][0]["chunk"] == 2


def test_f3_clean_done_chunks_never_appear_as_skipped(monkeypatch):
    row = _DOC_ROW[:8] + ({"0": {"s": "done", "c": 3, "g": 1}},) + _DOC_ROW[9:]
    conn = _Conn([row])
    monkeypatch.setattr(main.psycopg2, "connect", lambda dsn: conn)
    monkeypatch.setattr(main, "_derive_tenant_schema", lambda uid: "faultline_x")
    out = main.documents_status("u" * 36, document_id=41)
    assert out["documents"][0]["skipped_chunks"] == []


# ---------------------------------------------------------------------------
# F4 — the remine prepass re-ows l1_deterministic_first captures
# ---------------------------------------------------------------------------
class _RmineCur:
    def __init__(self, owner):
        self._owner = owner
        self._pending = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, q, params=None):
        self._owner.executed.append((" ".join(str(q).split()), params))
        qn = self._owner.executed[-1][0]
        if "information_schema.columns" in qn:
            self._pending = [(1,)]
        elif "SET search_path" in qn:
            self._pending = []
        elif "FROM documents" in qn and "chunk_state" in qn:
            self._pending = [(41, json.dumps(self._owner.state))]
        elif "UPDATE documents" in qn:
            delta = params[1]
            self._owner.doc_attempts += delta
            self._pending = [(self._owner.doc_attempts,)]
        else:
            self._pending = []

    def fetchone(self):
        return self._pending[0] if self._pending else None


class _RmineDB:
    def __init__(self, state):
        self.state = state
        self.executed = []
        self.doc_attempts = 1

    def cursor(self):
        return _RmineCur(self)

    def commit(self):
        pass

    def close(self):
        pass

    def rollback(self):
        pass


def _run_remine(monkeypatch, state):
    db = _RmineDB(state)
    monkeypatch.setattr(emb, "DOC_REMINE_ENABLED", True)
    monkeypatch.setattr(emb, "_DOC_REMINE_LAST_RUN", [0.0])
    monkeypatch.setattr(emb, "_DOC_REMINE_INTERVAL_S", 0.0)
    monkeypatch.setattr(emb, "_DOC_REMINE_BATCH", 2)
    monkeypatch.setattr(emb, "_DOC_REMINE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(emb, "_DOC_REMINE_MIN_AGE_HOURS", 0)
    monkeypatch.setattr(emb, "_DOC_REMINE_CURSOR", [None])
    monkeypatch.setattr(emb.psycopg2, "connect", lambda dsn: db)
    monkeypatch.setattr(emb, "_tenant_walkable_place_count", lambda db2, s: 0)
    n = emb.remine_documents_prepass(
        "postgresql://fake", [("user-abcdef00", "faultline_abc")], saas_skip=set())
    flip = next((p for q, p in db.executed
                 if "UPDATE documents" in q and "reextract_attempts" in q), None)
    kept_json = flip[2] if flip else "{}"
    return n, db, json.loads(kept_json)


def test_f4_remine_re_ows_l1_tagged_chunks(monkeypatch):
    state = {
        "0": {"s": "done", "c": 4, "g": 2, "cls": "l1_deterministic_first"},
        "1": {"s": "done", "c": 3, "g": 1},  # a brain capture — KEEP
    }
    n, db, kept = _run_remine(monkeypatch, state)
    assert n == 1
    assert "0" not in kept, "the l1 capture must be RE-OWED (deferred LLM read)"
    assert kept == {"1": state["1"]}, "a plain brain capture is kept (no double-count)"


def test_f4_remine_keeps_plain_done_chunks(monkeypatch):
    state = {"0": {"s": "done", "c": 2, "g": 1}}
    n, db, kept = _run_remine(monkeypatch, state)
    assert n == 1
    assert kept == state


def test_f4_l1_seam_annotates_the_meta_dict(monkeypatch):
    # the seam sets _meta["l1"]=True ONLY on the deterministic-first arm
    captured = {}

    monkeypatch.setattr(emb, "DOC_L1_DETERMINISTIC_FIRST", True)
    monkeypatch.setattr(emb, "_brain_lane_slow", lambda uid: "p95:99999ms")
    monkeypatch.setattr(emb, "_deterministic_chunk_edges", lambda ch: [
        {"subject": "a", "rel_type": "uses", "object": "b"}])
    out = emb._document_chunk_edges("some chunk text", "u" * 8, "http://x", "spine",
                                    _meta=captured)
    assert out and captured.get("l1") is True


def test_f4_l1_seam_leaves_meta_clean_on_the_ladder(monkeypatch):
    captured = {}
    monkeypatch.setattr(emb, "DOC_L1_DETERMINISTIC_FIRST", True)
    monkeypatch.setattr(emb, "_brain_lane_slow", lambda uid: None)  # fast brain

    def _no_ladder_post(url, json=None, headers=None, timeout=None, **kw):
        # R8 MINOR-2: house style is DB-less/monkeypatched — no live dial, even to
        # a dead loopback port; any failure raises into the swallowed fallback
        raise AssertionError("no network in this pin")

    monkeypatch.setattr(emb.httpx, "post", _no_ladder_post)
    # the ladder below needs httpx; a transport error is fine — the ladder runs
    try:
        emb._document_chunk_edges("some chunk text", "u" * 8, "http://127.0.0.1:1",
                                  "spine", _meta=captured)
    except Exception:
        pass
    assert "l1" not in captured


# ---------------------------------------------------------------------------
# R2 FINDING 1 — timestamp-LEADING logs (mid-line brackets) are not references
# ---------------------------------------------------------------------------
def test_r2_canonical_production_log_is_not_cite_dense():
    # the R2 critic shape: a timestamp LEADS, the bracket id sits MID-LINE; the
    # bracket-position veto never saw it and every line counted as a reference
    chunk = _log_chunk(lambda i: "2026-11-03 10:31:02,123 [%d] worker: connection "
                       "refused to upstream svc" % (400 + i))
    v = ds.citation_density_verdict(chunk)
    assert v["cite_dense"] is False
    assert v["ref_line_ratio"] == 0.0


def test_r2_level_leading_log_is_not_cite_dense():
    chunk = _log_chunk(lambda i: "INFO [%d] worker: connection refused" % (400 + i))
    assert ds.citation_density_verdict(chunk)["cite_dense"] is False


def test_r2_year_led_reference_lines_still_dense():
    # a wrapped reference line can legitimately LEAD with a bare year — ISO needs
    # the month part and a clock needs colons, so this must stay a reference
    chunk = _log_chunk(lambda i: "2020. Smith, J. Title of paper %d. "
                       "Journal of Things, pp. 1-9." % i)
    assert ds.citation_density_verdict(chunk)["cite_dense"] is True


# ---------------------------------------------------------------------------
# R2 FINDING 2 — the salvage lane commits PER ROW (rollback cannot eat salvaged rows)
# ---------------------------------------------------------------------------
class _SalvageCur:
    doc = ("Faithful psycopg2 implicit-transaction model: executes accrue, commit"
           " finalizes the accrued set, rollback discards it.")

    def __init__(self, owner):
        self._owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, q, params=None):
        name = params[0]
        if name in self._owner.poisoned:
            raise RuntimeError("poisoned row: " + name)
        self._owner.txn.append(name)

    def fetchone(self):
        return None


class _SalvageDB:
    def __init__(self, poisoned):
        self.poisoned = poisoned   # group names that fail
        self.txn = []              # uncommitted rows
        self.committed = []        # rows that actually landed

    def cursor(self):
        return _SalvageCur(self)

    def commit(self):
        self.committed.extend(self.txn)
        self.txn = []

    def rollback(self):
        self.txn = []

    def close(self):
        pass


def test_r2_salvage_poisoned_middle_row_lands_the_others(monkeypatch):
    # 3 pending groups, the MIDDLE one poisoned: rows before AND after must land
    # (commit-per-row), and minted must count exactly what committed — the R2 bug
    # landed ZERO groups while reporting two.
    pending = {
        "g_a": {"rels": {"uses"}, "types": {"svc"}, "members": 2},
        "g_bad": {"rels": {"uses"}, "types": {"svc"}, "members": 2},
        "g_c": {"rels": {"uses"}, "types": {"svc"}, "members": 2},
    }
    db = _SalvageDB(poisoned={"g_bad"})
    minted = main._mint_groupings_salvage(db, pending)
    assert sorted(db.committed) == ["g_a", "g_c"], db.committed
    assert minted == 2


def test_r2_salvage_all_poisoned_lands_nothing_and_counts_honestly(monkeypatch):
    pending = {
        "g_x": {"rels": {"uses"}, "types": {"svc"}, "members": 2},
        "g_y": {"rels": {"uses"}, "types": {"svc"}, "members": 2},
    }
    db = _SalvageDB(poisoned={"g_x", "g_y"})
    minted = main._mint_groupings_salvage(db, pending)
    assert db.committed == [] and minted == 0


# ---------------------------------------------------------------------------
# R3 FINDINGS 1+2 — the affil lane honours the lead veto; the lead set covers
# the syslog/slash-date/epoch/date-only log family (all critic-measured shapes)
# ---------------------------------------------------------------------------
def test_r3_affil_lane_honours_the_lead_veto():
    # R3-F1: an ISO-timestamp-led AUTH log carries an affiliation token per line;
    # the ungated affil lane re-landed the R2 population in the permanent skip
    chunk = _log_chunk(lambda i: "2026-11-03 10:31:02,123 [%d] auth: login failed "
                       "for bob@corp.com" % (400 + i))
    v = ds.citation_density_verdict(chunk)
    assert v["cite_dense"] is False and v["affil_lines"] == 0, v


def test_r3_syslog_rfc3164_is_not_cite_dense():
    chunk = _log_chunk(lambda i: "Nov  3 10:31:02 web-01 sshd[%d]: Failed password "
                       "for root from 10.0.0.5" % (400 + i))
    assert ds.citation_density_verdict(chunk)["cite_dense"] is False


def test_r3_slash_and_epoch_and_dateonly_logs_are_not_cite_dense():
    assert ds.citation_density_verdict(_log_chunk(
        lambda i: "2026/11/03 10:31:02 [%d] worker: connection refused" % (400 + i)
    ))["cite_dense"] is False
    assert ds.citation_density_verdict(_log_chunk(
        lambda i: "1762165862 [%d] worker: connection refused" % (400 + i)
    ))["cite_dense"] is False
    assert ds.citation_density_verdict(_log_chunk(
        lambda i: "2026-11-03 [%d] worker: connection refused" % (400 + i)
    ))["cite_dense"] is False


def test_r3_bare_hhmm_and_yearled_and_notice_refs_still_dense():
    # the FN guards hold: bare HH:MM verse refs, year-led refs, and level-word-
    # initial references (up to the >50% escape) are NOT vetoed
    # the FN guard is on the VETO itself: a bare HH:MM lead (two colon groups) is
    # never matched by the lead regex — verse-shaped lines stay body-prose by the
    # gate's own counts (no [n]/venue/arxid), never a false skip via the log class
    assert not ds._CITE_LOG_LEAD_RE.match("10:12 A verse-shaped reference 3, pp. 1-3.")
    year = _log_chunk(lambda i: "2020. Smith, J. Title of paper %d. "
                      "Journal of Things, pp. 1-9." % i)
    assert ds.citation_density_verdict(year)["cite_dense"] is True
    # R5 F-2 semantics: a log-vetoed line's venue token is NOT a citation mark —
    # 4 vetoed NOTICE lines + 4 bracket refs = marks 4 < 6, so the mixed block
    # escapes (throughput FN, direction-safe); a GENUINE block (>=6 real marks)
    # still flags at the same ratio.
    notice_lines = [("NOTICE of Appeal filed 2019, case %d, "
                     "Journal of Appeals, pp. 3-9." % i) for i in range(4)]
    bracket_lines = [("[%d] Smith, J. More findings on appeal law, "
                      "pp. 11-19." % (70 + i)) for i in range(4)]
    notice = chr(10).join(notice_lines + bracket_lines)
    vn = ds.citation_density_verdict(notice)
    assert vn["cite_dense"] is False and vn["marks"] == 4, vn
    real = chr(10).join(notice_lines[:2] + [("[%d] Smith, J. More findings on appeal "
                                             "law, pp. 11-19." % (70 + i)) for i in range(6)])
    vr = ds.citation_density_verdict(real)
    assert vr["cite_dense"] is True and vr["marks"] == 6, vr


# ---------------------------------------------------------------------------
# R4 FINDINGS 1-5 — epoch family, bracket-first parity, prefix-defeat, affil
# contact signature, syslog-no-seconds (all critic-measured shapes)
# ---------------------------------------------------------------------------
def test_r4_epoch_family_is_not_cite_dense():
    assert ds.citation_density_verdict(_log_chunk(
        lambda i: "1762165862123 [%d] worker: connection refused" % (400 + i)
    ))["cite_dense"] is False  # R4-F1: ms epoch
    assert ds.citation_density_verdict(_log_chunk(
        lambda i: "1762165862.123 [%d] worker: connection refused" % (400 + i)
    ))["cite_dense"] is False  # R4-F1: fractional epoch
    v = ds.citation_density_verdict(_log_chunk(
        lambda i: "1762165862 [%d] auth: login failed for bob@corp.com" % (400 + i)
    ))
    assert v["cite_dense"] is False and v["affil_lines"] == 0, v  # R3-F1 side door stays shut


def test_r4_bracket_first_shape_parity():
    # R4-F2: the bracket-FIRST veto got the same month + epoch alternatives
    assert ds.citation_density_verdict(_log_chunk(
        lambda i: "[%d] Nov  3 10:31:02 sshd: Failed password for root" % (400 + i)
    ))["cite_dense"] is False
    assert ds.citation_density_verdict(_log_chunk(
        lambda i: "[%d] 1762165862 worker: connection refused" % (400 + i)
    ))["cite_dense"] is False
    assert ds.citation_density_verdict(_log_chunk(
        lambda i: "2026-11-03[%d] worker: connection refused" % (400 + i)
    ))["cite_dense"] is False  # date-only with NO gap before the bracket


def test_r4_prefix_defeat_is_closed():
    # R4-F3: bullet / numbered / quote / transcript prefixes no longer defeat the
    # veto while the mid-line [pid] still counted as citation marks
    assert ds.citation_density_verdict(_log_chunk(
        lambda i: "- 2026-11-03 10:31:02,123 [%d] worker: connection refused" % (400 + i)
    ))["cite_dense"] is False
    assert ds.citation_density_verdict(_log_chunk(
        lambda i: "1. 2026-11-03 10:31:02,123 [%d] worker: connection refused" % (400 + i)
    ))["cite_dense"] is False
    assert ds.citation_density_verdict(_log_chunk(
        lambda i: "> Nov  3 10:31:02 sshd[%d]: Failed password for root" % (400 + i)
    ))["cite_dense"] is False
    # the FACT-BEARING transcript variant (critic's strict-major shape)
    assert ds.citation_density_verdict(_log_chunk(
        lambda i: "user: my log shows 2026-11-03 10:31:02,123 [%d] auth failed" % (400 + i)
    ))["cite_dense"] is False


def test_r4_deanchored_is_timestamp_only_and_window_bounded():
    # the de-anchored set carries TIMESTAMP alternatives ONLY — a mid-line level
    # word with a citation never vetoes prose ("Error rates in [12]...")
    # (mid-line level words never veto — the level token is ANCHORED-only; a line
    # LEADING with a level word is the R3-F3 accepted residual and still vetoes)
    assert not ds._cite_line_is_log("The error rates in [12] were significant across sites.")
    assert ds._cite_line_is_log("NOTICE of Appeal filed 2019, case 4, pp. 3-9.")
    # a non-numeric bracket plus a clock is prose (the gate needs the NUMS mark)
    assert not ds._cite_line_is_log("resumed at 10:31:02 [see appendix] with the full timeline")
    # beyond the 48-char window a timestamp signature no longer vetoes
    far = "x" * 60 + " 2026-11-03 10:31:02 [402] worker log"
    assert not ds._cite_line_is_log(far)


def test_r4_syslog_without_seconds_is_not_cite_dense():
    assert ds.citation_density_verdict(_log_chunk(
        lambda i: "Nov  3 10:31 web-01 sshd[%d]: Failed password for root" % (400 + i)
    ))["cite_dense"] is False  # R4-F5


def test_r4_affil_run_needs_the_contact_signature():
    # R4-F4: fact-bearing org-roster prose (no emails) is NO longer permanently
    # skipped; a contact block with emails still is
    roster = _log_chunk(lambda i: "Dr. Person %d is with the Department of Physics, "
                                  "University of Somewhere." % i)
    v = ds.citation_density_verdict(roster)
    assert v["cite_dense"] is False and v["reason"] == "body_prose", v
    contact = _log_chunk(lambda i: "Jane Doe is with the Department of Computer Science, "
                                   "University of Somewhere (jdoe%d@somewhere.edu)." % i)
    v2 = ds.citation_density_verdict(contact)
    assert v2["cite_dense"] is True and v2["reason"] == "affil_block", v2


# ---------------------------------------------------------------------------
# R5 FINDINGS 1-4 — contact signature is position-gated at the seam; marks
# exclude log-vetoed lines; residuals documented; TLD whitelist widened
# ---------------------------------------------------------------------------
def test_r5_affil_verdict_is_front_matter_gated_at_the_seam(monkeypatch):
    # R8 MINOR-1: hermetic — the seam reads the env LIVE, so an ambient
    # DOC_CITE_AFFIL_FRONT_MATTER_MAX would spuriously redden this pin
    monkeypatch.delenv("DOC_CITE_AFFIL_FRONT_MATTER_MAX", raising=False)
    # R5-F1: no chunk-granularity signal separates an authors-contact block from
    # a staff roster WITH emails, so the SEAM honours affil_block only in the
    # front matter (measured corpus TP = chunk pos 3); deeper rosters run the
    # ladder (throughput FN, direction-safe). The verdict itself stays pure.
    roster = _log_chunk(lambda i: "Dr. Person %d is with the Department of Physics, "
                                  "University of Somewhere (p%d@somewhere.edu)." % (i, i))
    assert ds.citation_density_verdict(roster)["cite_dense"] is True  # verdict unchanged
    from src.re_embedder import embedder as _emb
    assert _emb._cite_env_int("DOC_CITE_AFFIL_FRONT_MATTER_MAX", "5") == 5


def test_r5_marks_exclude_log_vetoed_lines():
    # R5-F2 (critic-measured compound): 4 bracket-pid log lines + 4 timestamped
    # FACT lines measured ratio 0.5 marks 8 -> ref_run -> FACT lines permanently
    # skipped. Marks now count only non-log lines: the PIDs are not citations.
    mixed = chr(10).join(
        ["[4%02d] process 2199 restarted by watchdog" % (10 + i) for i in range(4)]
        + ["As of 03/15/2026 10:31, patient 4 was prescribed [%d] mg daily." % (4 + i)
           for i in range(4)])
    v = ds.citation_density_verdict(mixed)
    assert v["cite_dense"] is False and v["marks"] == 4, v


def test_r5_international_tlds_satisfy_the_contact_signature():
    # R5-F4: x@y.io (and the widened whitelist) now count toward the >=2-email
    # signature; the email-subset-of-affil invariant holds by construction
    contact = _log_chunk(lambda i: "Jane Doe is with the Department of Physics, "
                                  "University of Somewhere (jdoe%d@somewhere.io)." % i)
    v = ds.citation_density_verdict(contact)
    assert v["cite_dense"] is True and v["reason"] == "affil_block", v


def test_r5_residual_d_is_documented():
    # R5-F3: the clocked-access/PMID/ISBN13 FN class is a DOCUMENTED residual
    src = Path(ds.__file__).read_text()
    assert "PMID 123456789" in src and "9780132350884" in src, (
        "residual (d) must name the measured FN shapes")


# ---------------------------------------------------------------------------
# R6 FINDINGS — the runtime blocker (kwargs-style logging), the marks veto-family
# parity, the seam test driving the REAL function, suffix-safety, honesty pins
# ---------------------------------------------------------------------------
def test_r6_zero_yield_log_emits_without_typeerror(monkeypatch):
    """R6-F1 RUNTIME BLOCKER pin: the skip's log call used structlog-style kwargs
    on the module's STDLIB logger — TypeError on the first cite-dense chunk of any
    stock drain (INFO default), requeue, re-crash, doc 'partial'. The emission is
    driven through the REAL _process_document_chunk with a counting handler."""
    import logging as _logging
    roster = _log_chunk(lambda i: "Dr. Person %d is with the Department of Physics, "
                                  "University of Somewhere (jdoe%d@somewhere.edu)." % (i, i))
    posts = []

    class _NoNet:
        # R7 F-2: the stub RECORDS — a lambda returning the list made
        # `assert posts == []` tautological (it could never have failed)
        def post(self, url, json=None, headers=None, timeout=None, **kw):
            posts.append(url)
            raise AssertionError("the ladder must not run on a zero-yield skip")

    monkeypatch.setattr(_emb.httpx, "post",
                        lambda url, json=None, headers=None, timeout=None, **kw:
                        _NoNet().post(url, json=json, headers=headers, timeout=timeout, **kw))
    monkeypatch.setattr(_emb, "DOC_CITE_DENSE_SKIP", True)
    monkeypatch.delenv("DOC_CITE_AFFIL_FRONT_MATTER_MAX", raising=False)  # R7 F-3: the seam reads the env LIVE
    records = []

    class _Count(_logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    h = _Count(level=_logging.DEBUG)
    old_level = _emb.log.level
    _emb.log.addHandler(h)
    _emb.log.setLevel(_logging.INFO)
    try:
        res = _emb._process_document_chunk(
            roster, idx=2, doc_id=41, user_id="u" * 32,
            backend_url="http://backend", source_ref=None,
            statement_route="rewrite", replay=False)
    finally:
        _emb.log.removeHandler(h)
        _emb.log.setLevel(old_level)
    assert res == (0, 0, 0, None, "cite_dense_reference_block"), res
    assert any("document_chunk_zero_yield" in m for m in records), records
    # the EPISODIC-RETAIN step legitimately POSTs before the gate; the guard is
    # that no EXTRACTION (/ingest) POST happens on a zero-yield skip
    assert not any(str(u).endswith("/ingest") for u in posts), posts


def test_r6_seam_gate_runs_the_ladder_for_deep_rosters(monkeypatch):
    """R5-F1/R6-F2 pin: the SAME roster at idx 7 (deep) must NOT produce the
    zero-yield 5-tuple — it runs the ladder (throughput FN, direction-safe)."""
    roster = _log_chunk(lambda i: "Dr. Person %d is with the Department of Physics, "
                                  "University of Somewhere (jdoe%d@somewhere.edu)." % (i, i))
    calls = {"edges": 0}

    def fake_edges(chunk, user_id, backend_url, route, *a, **kw):
        calls["edges"] += 1
        return [{"subject": "s", "rel_type": "r", "object": "o"}]

    class _R:
        # R7 F-1: the real success path calls raise_for_status() OUTSIDE the try —
        # a stub without it silently turned this very pin into a FAILED chunk
        def json(self):
            return {"committed": 1, "staged": 0}

        def raise_for_status(self):
            return None

    monkeypatch.setattr(_emb.httpx, "post",
                        lambda url, json=None, headers=None, timeout=None, **kw: _R())
    monkeypatch.setattr(_emb, "_document_chunk_edges", fake_edges)
    monkeypatch.setattr(_emb, "DOC_CITE_DENSE_SKIP", True)
    # R8 MINOR-1: hermetic — the seam reads DOC_CITE_AFFIL_FRONT_MATTER_MAX live;
    # an ambient value >= 7 would legitimately skip idx 7 and redden this pin
    monkeypatch.delenv("DOC_CITE_AFFIL_FRONT_MATTER_MAX", raising=False)
    res = _emb._process_document_chunk(
        roster, idx=7, doc_id=41, user_id="u" * 32,
        backend_url="http://backend", source_ref=None,
        statement_route="rewrite", replay=False)
    assert not (isinstance(res, tuple) and len(res) == 5
                and res[4] == "cite_dense_reference_block"), res
    assert calls["edges"] >= 1, "the ladder must have run"
    assert res[2] == 0, f"the chunk must NOT be a failed chunk: {res}"


def test_r6_marks_exclude_the_whole_veto_family():
    # R6-F3: bracket-first LEVEL log lines ([402] INFO worker: ...) are vetoed by
    # the R1 shape lane — their brackets must not donate marks either
    comp = chr(10).join(
        ["[%d] INFO worker: heartbeat ok" % (402 + i) for i in range(2)]
        + ["Method X [%d] first defines three types of paths here." % (70 + i) for i in range(5)]
        + ["Plain fact sentence number %d states a real relation." % i for i in range(3)])
    v = ds.citation_density_verdict(comp)
    assert v["cite_dense"] is False and v["marks"] == 5, v


def test_r6_email_tld_suffix_semantics_pinned():
    # R6-F7: the negative suffix lookahead stops MID-HOST whitelisted labels
    assert not ds._CITE_EMAIL_RE.search("user@y.edu.foo.bar")
    # INHERENT RESIDUAL (documented): a host whose REAL final label is whitelisted
    # matches — user@y.edu.evil.com ends in .com; no regex knows DNS truth
    assert ds._CITE_EMAIL_RE.search("user@y.edu.evil.com")
    assert ds._CITE_EMAIL_RE.search("user@y.ca.uk")
    # the in-code freeze ruling is present
    assert "ALTERNATIVE-SET FREEZE" in Path(ds.__file__).read_text()


# ---------------------------------------------------------------------------
# R7 F-5 — the OTHER three kwargs->f-string conversions get the same counting-
# handler emission pins (the unpinned-flag-gated shape is what masked R6-F1)
# ---------------------------------------------------------------------------
def test_r7_l1_emissions_pinned(monkeypatch):
    import logging as _logging

    class _Count(_logging.Handler):
        def __init__(self):
            super().__init__(level=_logging.DEBUG)
            self.messages = []

        def emit(self, record):
            self.messages.append(record.getMessage())

    h = _Count()
    old_level = _emb.log.level
    _emb.log.addHandler(h)
    _emb.log.setLevel(_logging.INFO)

    class _NoLadder:
        def post(self, url, json=None, headers=None, timeout=None, **kw):
            raise AssertionError("no network in this pin")

    monkeypatch.setattr(_emb.httpx, "post",
                        lambda url, json=None, headers=None, timeout=None, **kw:
                        _NoLadder().post(url, json=json, headers=headers,
                                         timeout=timeout, **kw))
    try:
        monkeypatch.setattr(_emb, "DOC_L1_DETERMINISTIC_FIRST", True)
        monkeypatch.setattr(_emb, "_brain_lane_slow", lambda uid: "p95:99999ms")
        edges = [{"subject": "a", "rel_type": "uses", "object": "b"}]
        monkeypatch.setattr(_emb, "_deterministic_chunk_edges", lambda ch: list(edges))
        meta = {}
        out = _emb._document_chunk_edges("some chunk text", "u" * 8, "http://x",
                                         "spine", _meta=meta)
        assert out and meta.get("l1") is True
        assert any("document_chunk_l1_deterministic_first" in m for m in h.messages), h.messages

        h.messages.clear()
        monkeypatch.setattr(_emb, "_deterministic_chunk_edges", lambda ch: [])
        try:
            # the L1-zero arm logs and FALLS THROUGH to the brain ladder, which the
            # _NoLadder stub stops (the extractor wraps ANY failure into
            # DocumentChunkExtractionFailed) — the emission is what this pin asserts
            _emb._document_chunk_edges("some chunk text", "u" * 8, "http://x",
                                       "spine", _meta={})
        except Exception:  # noqa: BLE001 — the fall-through failure is expected
            pass
        assert any("document_chunk_l1_deterministic_zero" in m for m in h.messages), h.messages
    finally:
        _emb.log.removeHandler(h)
        _emb.log.setLevel(old_level)


def test_r7_hollow_declined_emission_pinned(monkeypatch):
    """Drives the REAL _growth_work_reattempt taxonomy branch: novel rel + a hollow
    discovery -> the declined warning must emit (R7 F-5: this conversion had NO
    emission pin — the unpinned-flag-gated masking shape that hid R6-F1)."""
    import logging as _logging

    class _Count(_logging.Handler):
        def __init__(self):
            super().__init__(level=_logging.DEBUG)
            self.messages = []

        def emit(self, record):
            self.messages.append(record.getMessage())

    h = _Count()
    old_level = _emb.log.level
    _emb.log.addHandler(h)
    _emb.log.setLevel(_logging.WARNING)
    try:
        class _Cur:
            def __init__(self):
                self.rows = [("uses",)]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, q, params=None):
                pass

            def fetchall(self):
                return self.rows

        class _Conn:
            def cursor(self):
                return _Cur()

            def commit(self):
                pass

        monkeypatch.setattr(_emb, "release_read_transaction", lambda *a, **k: None)
        import src.api.main as _main
        monkeypatch.setattr(_main, "_llm_discover_taxonomy_from_facts",
                            lambda conn, user, facts: {"taxonomy_name": "hollow thing",
                                                       "rel_types_defining_group": []})
        ok = _emb._growth_work_reattempt(
            "growth", "taxonomy_discovery", _Conn(), "system", "schema_x",
            "http://qwen", "postgresql://pg", "http://qdrant")
        assert ok is True
        assert any("taxonomy_hollow_declined" in m for m in h.messages), h.messages
        assert "rel_type=uses" in next(m for m in h.messages
                                       if "taxonomy_hollow_declined" in m)
    finally:
        _emb.log.removeHandler(h)
        _emb.log.setLevel(old_level)


# F5 — the refund's honest contract
# ---------------------------------------------------------------------------
def test_f5_refund_is_documented_as_defensive():
    src = Path(emb.__file__).read_text()
    assert "DEFENSIVE, currently unreachable" in src, (
        "the refund comment must keep the honesty amendment: today's lanes never "
        "write rate_deferred failed ledger entries (they re-pend whole documents)")


def test_f5_pure_paced_classifier_still_refunds_when_reachable(monkeypatch):
    # the defensive path itself: IF a lane ever writes paced-failed entries, the
    # flip must not consume an attempt
    state = {
        "0": {"s": "failed", "a": 1, "e": "extraction degraded — rate_deferred: paced"},
        "1": {"s": "failed", "a": 1, "e": "extraction degraded — rate_deferred: paced"},
    }
    n, db, kept = _run_remine(monkeypatch, state)
    assert n == 1
    flip = next(p for q, p in db.executed
                if "UPDATE documents" in q and "reextract_attempts" in q)
    assert flip[1] == 0, "pure-paced terminal: delta 0"
    assert db.doc_attempts == 1  # unchanged
