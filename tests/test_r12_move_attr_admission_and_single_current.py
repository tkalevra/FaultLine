"""R12 - move-side attr admission + address single-current (critic RG-G-M4 + RG-Z-2).

Two wounds from the R11 re-gate:

  (1) RG-G-M4 move attr STILL silent: the critic's turn "We moved from Spurrier's Lane
  in Halter's Mark to Glover's Hold." committed two lives_in rows and dropped the past
  source (history-kept, correct) - but scalar_committed=0, because a POSSESSED
  house-name destination ("Glover's Hold": a PROPN possessor over a place noun, no
  digits, no nested locative PP) filed lives_in and never produced the whole-value
  live_at @now edge the attr gate consumes. A possessed named destination is an
  ADDRESS (files live_at @now); a bare city ("Berlin") stays lives_in.

  (2) RG-Z-2 address half: the battery_c rep re-planted the old address ~5s AFTER the
  move and single-current did not reconcile - TWO live residences. Root cause: the R10
  same-instant guard's 8s default window blocked the legitimate cross-turn supersede
  (the rows were 5s apart). The window narrows to 2s (same-request reprocessing and
  instant flap flushes stay guarded - the R10 wound rows were created in ONE instant),
  and the end-state reconciliation now enforces EXACTLY ONE live residence row:
  multiple live rows keep the newest statement and archive the older live rows.

The attr-gate trio downstream of admission (tombstone-free probe, routed provenance,
F6 supersede) is pinned in test_r9/test_r10 and composes with the edge unchanged.
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


# ---------- (1) the destination IS an address ----------

def test_r12_possessed_house_name_destination_files_live_at_now():
    """THE CONTRACT: 'Glover's Hold' (possessed house name, no digits, no nested PP)
    files whole-value live_at stamped NOW - the edge the attr gate consumes to write
    the lives_at attr and satisfy scalar_committed>=1."""
    rows = _edges(CRITIC_MOVE)
    dest = [o for r, o, t in rows if r == "live_at" and t == "now"]
    assert dest == ["glover's hold"], rows  # whole value, never the bare head 'hold'


def test_r12_move_attr_edge_is_admission_ready():
    """Admission shape: rel_type live_at (mutable residence identity), object whole,
    temporal now, affirmed - exactly the input the F4/F6 supersede trio requires."""
    rows = _edges(CRITIC_MOVE)
    edge = next(((r, o, t) for r, o, t in rows
                 if r == "live_at" and t == "now"), None)
    assert edge is not None, rows
    assert edge[1].count("'s") >= 1 and "hold" in edge[1], edge  # possessed place whole


def test_r12_past_source_still_history():
    """The moved-from source files PAST (history) - the attr gate's history-kept drop
    for it is correct and unchanged."""
    rows = _edges(CRITIC_MOVE)
    assert any(r == "live_at" and t == "past" for r, _, t in rows), rows
    assert not any(r == "live_at" and t == "now" and "spurrier" in (o or "")
                   for r, o, t in rows), rows


def test_r12_bare_city_destination_stays_lives_in():
    """A bare city (no possessor, no nested PP, no digits) stays lives_in - the R11
    discrimination is not over-broadened."""
    rows = _edges("I moved to Berlin.")
    assert any(r == "lives_in" and o == "berlin" for r, o, _ in rows), rows
    assert not any(r == "live_at" for r, _, _ in rows), rows


def test_r11_flether_shape_still_whole():
    """The R11 nested-PP destination keeps its whole @now value (regression guard)."""
    rows = _edges("We moved from Flether's Lane in Webber's Down "
                  "to Lorer's Croft in Plomer's Rise.")
    assert any(r == "live_at" and o == "lorer's croft in plomer's rise" and t == "now"
               for r, o, t in rows), rows


# ---------- (2) single-current reconciliation ----------

def test_r12_guard_window_default_is_two_seconds():
    """The R10 same-instant guard's default window is 2s: the R11 battery proved an 8s
    window silently blocked a legitimate cross-turn re-plant 5s after a move (two live
    residences). 2s still guards same-request reprocessing and instant flap flushes."""
    api_main = None
    from src.api import main as api_main_mod
    api_main = api_main_mod
    import inspect
    src = inspect.getsource(api_main._soft_supersede_conflicting_fact)
    assert '"INGEST_SELF_SUPERSEDE_MIN_AGE_SEC", "2") or "2"' in src
    assert "_sis_win = 2.0" in src  # the fallback too


def test_r12_endstate_guard_reconciles_multiple_live_rows():
    """RG-Z-2 address half: MULTIPLE live residence rows keep the NEWEST statement and
    archive the older live rows - the end state is EXACTLY ONE live row."""
    from src.api import main as api_main

    class _Cur:
        def __init__(self):
            self.executed = []

        def execute(self, sql, params=None):
            self.executed.append((sql, params))
            if "SELECT id FROM facts" in sql:
                self._rows = [(101,), (99,)]  # 101 newest by created_at DESC
            elif sql.startswith("SELECT 1 FROM entity_attributes"):
                self._rows = None
            elif sql.startswith("UPDATE facts SET superseded_at = now()"):
                self._rows = None

        def fetchall(self):
            return getattr(self, "_rows", None) or []

        def fetchone(self):
            return None

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def __init__(self):
            self.cursor_obj = _Cur()

        def cursor(self):
            return self.cursor_obj

    conn = _Conn()
    api_main._ensure_live_residence_end_state(conn, "u1")
    ups = [e for e in conn.cursor_obj.executed
           if e[0].startswith("UPDATE facts SET superseded_at = now()")]
    assert ups, conn.cursor_obj.executed
    sql, params = ups[0]
    assert "AND id <> %s" in sql
    assert params[2] == 101  # the newest live row is kept


def test_r12_endstate_guard_single_live_is_noop():
    """Exactly one live row -> no reconciliation UPDATE is issued."""
    from src.api import main as api_main

    class _Cur:
        def __init__(self):
            self.executed = []

        def execute(self, sql, params=None):
            self.executed.append((sql, params))
            if "SELECT id FROM facts" in sql:
                self._rows = [(101,)]
            else:
                self._rows = None

        def fetchall(self):
            return getattr(self, "_rows", None) or []

        def fetchone(self):
            return None

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def __init__(self):
            self.cursor_obj = _Cur()

        def cursor(self):
            return self.cursor_obj

    conn = _Conn()
    api_main._ensure_live_residence_end_state(conn, "u1")
    assert not [e for e in conn.cursor_obj.executed
                if e[0].startswith("UPDATE")], conn.cursor_obj.executed
