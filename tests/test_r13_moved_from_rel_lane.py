"""R13 - moved-from REL lane (critic RG-G-M4 last piece).

R12b left one defect: the moved-from correction landed the ATTR (whole-value live_at
@now - the R12 admission fix works) but committed ZERO residence REL rows - the REL
slot kept pointing at the OLD city while the attr said the new one. Root cause: a bare
possessed destination ("Glover's Hold") has no nested district PP to split, so the city
split produced NO lives_in at all and the relational slot was never written.

Contract pinned here: a moved-from correction ends with EXACTLY ONE live residence -
the REL lane (lives_in) AND the attr lane (live_at) BOTH point at the destination; the
old city's rel is archived by the ingest supersede, backstopped by the end-state
reconciliation (multi-live keeps the newest TEMPORAL-CURRENT row - a move lands the
destination row and the source-city history row in the same txn, so created_at alone
would keep the PAST row and archive the destination).

Also pinned: the end-state tracker arms on ANY mutable-residence row reaching the
temporal decision (not only on supersedes) - a guard-window-blocked supersede must not
leave the slot dual-live with no reconciliation. The guards compose, never fight: the
supersede archives when the target predates the window; the reconciliation catches the
window-blocked remainder post-commit.
"""

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.extraction.linguistics import derive_sentence_facts  # noqa: E402


def _edges(sentence):
    out = derive_sentence_facts(sentence, datetime.now()) or []
    rows = []
    for e in out:
        d = e.__dict__ if hasattr(e, "__dict__") else {}
        rows.append((d.get("rel_type"), d.get("object"), d.get("temporal_status")))
    return rows


CRITIC_MOVE = "We moved from Spurrier's Lane in Halter's Mark to Glover's Hold."


def test_r13_rel_lane_points_at_destination():
    """THE CONTRACT: the relational residence row (lives_in) points at the destination
    itself, stamped NOW - the old city's slot is superseded at ingest."""
    rows = _edges(CRITIC_MOVE)
    assert any(r == "lives_in" and o == "glover's hold" and t == "now"
               for r, o, t in rows), rows


def test_r13_both_lanes_carry_destination_no_split():
    """No dual-lane split: attr (live_at whole) AND rel (lives_in) BOTH carry the
    destination @now."""
    rows = _edges(CRITIC_MOVE)
    assert any(r == "live_at" and o == "glover's hold" and t == "now"
               for r, o, t in rows), rows
    assert any(r == "lives_in" and o == "glover's hold" and t == "now"
               for r, o, t in rows), rows


def test_r13_old_city_never_current():
    """The moved-from city files as PAST history - never as the current residence."""
    rows = _edges(CRITIC_MOVE)
    assert not any(r == "lives_in" and "halter" in (o or "") and t == "now"
                   for r, o, t in rows), rows
    assert any(r == "lives_in" and "halter" in (o or "") and t == "past"
               for r, o, t in rows), rows


def test_r13_city_split_destination_not_duplicated():
    """When a district city exists (nested PP), the city split owns the relational row -
    NO second lives_in welding the whole street phrase in (the R11/R12 shape holds)."""
    rows = _edges("We moved from Flether's Lane in Webber's Down "
                  "to Lorer's Croft in Plomer's Rise.")
    cities = [o for r, o, t in rows if r == "lives_in" and t == "now"]
    assert cities == ["plomer's rise"], rows


def test_r13_bare_city_stays_lives_in_only():
    """A bare city destination keeps the single lives_in reading - no live_at invented."""
    rows = _edges("I moved to Berlin.")
    assert any(r == "lives_in" and o == "berlin" and t == "now"
               for r, o, t in rows), rows
    assert not any(r == "live_at" for r, _, _ in rows), rows


def test_r13_tracker_arms_on_any_residence_row_source_contract():
    """The end-state tracker arms BEFORE the F6 decision (supersede, coexist, or drop)
    and on first-residence inserts - a window-blocked supersede cannot leave the slot
    dual-live with no reconciliation. Source-contract pin (inside the monolithic txn)."""
    import inspect
    from src.api import main as api_main
    src = inspect.getsource(api_main._ingest_impl)
    marker = src.count("arm the tracker for ANY mutable-residence row")
    assert marker == 1, marker
    assert "a FIRST residence row also arms the end-state" in src


def test_r13_keeper_prefers_temporal_current_rows():
    """The multi-live keeper ranks temporal-CURRENT rows first: the move lands the
    destination row and the source-city history row in the same txn (identical
    created_at) - created_at alone would keep the PAST row and archive the
    destination. Source-contract pin on the keeper's ORDER BY."""
    import inspect
    from src.api import main as api_main
    src = inspect.getsource(api_main._ensure_live_residence_end_state)
    assert "(COALESCE(temporal_status, 'now') = 'now') DESC" in src
    assert "created_at DESC, id DESC" in src
