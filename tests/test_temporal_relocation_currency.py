"""Regression gate — RESIDENCE-CURRENCY / relocation valid-FROM (TEMPORAL_RELOCATION_CURRENT).

LIVE-OBSERVED BUG (pre-prod, feature/spine-composition): "I live in Toronto." then "I moved to
Vancouver last year." → DB coexist OK (both ``lives_in``, Vancouver ``event_date`` = last-year), BUT
  • "where do I live now?"      → BOTH Toronto and Vancouver (no current-vs-former split), and
  • "where did I used to live?" → "No relevant facts found."
Two stacked defects:
  (1) INGEST: the relocation chain emitted ``temporal_status=None`` for a PRESENT move, so the
      request-level DATE-DRIVEN detector read the PAST move-date and stamped the RESIDENCE 'past'
      (event_date is a valid-FROM of the MOVE, NOT the residence-currency — bi-temporal error).
  (2) QUERY: ``_recency_collapse_single_valued_state`` SKIPPED all dated facts, so a dated
      relocation never reconciled against an UNDATED prior residence → both stayed current.

This module pins the QUERY half (pure-Python, no DB): the collapse now admits DATED single-valued
STATE 'now' facts and marks the OLDER-asserted value FORMER, while a dated 'past' HISTORICAL mention
is never promoted to current. The INGEST half ('now' emit) is pinned by
``test_spine_relocation_residence.py``. Subject-agnostic; non-personal reference data only.
"""
import os

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.api.main import _recency_collapse_single_valued_state as _rc  # noqa: E402


def _meta(single=("lives_in",)):
    return lambda rt: {"temporal_class": "state", "is_single_valued": rt in single}


def _f(obj, status="now", event_date=None, created_at="", ingest_id=0, rel="lives_in"):
    d = {"rel_type": rel, "_subject_id": "U", "_object_id": obj,
         "temporal_status": status, "created_at": created_at, "_ingest_id": ingest_id}
    if event_date:
        d["event_date"] = event_date
    return d


def _status(facts, obj):
    return next(f["temporal_status"] for f in facts if f["_object_id"] == obj)


def test_dated_relocation_demotes_undated_prior():
    # Toronto (undated 'now', asserted FIRST) + Vancouver (dated 'now', asserted SECOND) → the
    # dated relocation is CURRENT, the older undated prior is FORMER. This is the exact live bug.
    facts = [
        _f("TOR", created_at="2026-07-25T14:31:55", ingest_id=1),
        _f("VAN", event_date="2025-07-25", created_at="2026-07-25T14:32:01", ingest_id=2),
    ]
    _rc(facts, _meta(), db=None)
    assert _status(facts, "TOR") == "past"
    assert _status(facts, "VAN") == "now"


def test_historical_past_dated_mention_not_promoted():
    # DANGER CASE: "I live in Vancouver" (undated 'now') + "I lived in Toronto in 2010" (dated,
    # temporal_status='past' — a past-stative, NOT a relocation). The 'now'-only guard must keep
    # the historical Toronto OUT of the group so it is never promoted to current.
    facts = [
        _f("VAN", created_at="2026-07-25T14:31:55", ingest_id=1),
        _f("TOR", status="past", event_date="2010-01-01",
           created_at="2026-07-25T14:32:01", ingest_id=2),
    ]
    _rc(facts, _meta(), db=None)
    assert _status(facts, "VAN") == "now"      # untouched current
    assert _status(facts, "TOR") == "past"     # stays historical (never promoted)


def test_multi_valued_state_untouched():
    # A MULTI-VALUED state rel ("speaks") keeps EVERY coexisting value current — the cardinality
    # guard must not collapse it even with the dated-inclusion change.
    facts = [
        _f("EN", created_at="2026-07-25T14:31:55", ingest_id=1, rel="speaks"),
        _f("FR", event_date="2025-07-25", created_at="2026-07-25T14:32:01",
           ingest_id=2, rel="speaks"),
    ]
    _rc(facts, _meta(single=("lives_in",)), db=None)  # speaks is NOT single-valued
    assert _status(facts, "EN") == "now"
    assert _status(facts, "FR") == "now"


def test_legacy_flag_off_skips_dated():
    # Flag OFF → legacy dated-skip: the dated Vancouver never joins the group, so the single
    # undated Toronto is a group of one → no collapse (byte-for-byte prior behavior).
    prev = os.environ.get("TEMPORAL_RELOCATION_CURRENT")
    os.environ["TEMPORAL_RELOCATION_CURRENT"] = "false"
    try:
        facts = [
            _f("TOR", created_at="2026-07-25T14:31:55", ingest_id=1),
            _f("VAN", event_date="2025-07-25", created_at="2026-07-25T14:32:01", ingest_id=2),
        ]
        _rc(facts, _meta(), db=None)
        assert _status(facts, "TOR") == "now"   # NOT demoted (legacy)
        assert _status(facts, "VAN") == "now"
    finally:
        if prev is None:
            os.environ.pop("TEMPORAL_RELOCATION_CURRENT", None)
        else:
            os.environ["TEMPORAL_RELOCATION_CURRENT"] = prev
