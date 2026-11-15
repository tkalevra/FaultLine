"""R10 - residence whole-value compose + supersede discipline (Lane C, fog family).

Four wounds, four pins:

  FIX A (deriver, RG-G2 wound A): "We live at 9 Lattenmaker's Gate in Sporer's Field."
  shredded the city - the residual-place seam minted a doubled-prep rel ("live_at_in")
  whose object was the BARE proper-name head ("field"; the possessive "Sporer's" is a
  poss child, not a compound, so _verb_pp_named_place dropped it) while the address edge
  kept only the street. The residence-address compose folds the whole city NP into the
  address value and emits the relational lives_in bridge instead.

  FIX B (attr lane, RG-G-M4 FAIL): the pre-existence probe counted ARCHIVED tombstones as
  values, so every re-plant after an archive was refused with "overwriting it requires
  stating the turn as a correction" while NO live value existed; and the F4 gate read the
  edge's raw provenance BEFORE the router ran, so an mcp-sourced edge stamped
  "llm_inferred" silently lost the trust firewall to an older user_stated value.

  GUARD 1 (RG-G2-1, same-instant supersede): a row created within this turn's window is
  this turn's own (possibly degraded) output - re-processing reconciles, it never
  chain-supersedes its own output.

  GUARD 2 (RG-G2-2, zero-live-residence end-state): a residence chain that archived the
  last live residence row without a surviving replacement restores the last-known prior
  residence - the user lives SOMEWHERE. The attr lane counts as a residence; rows created
  inside the turn window are never resurrected.

Pins are behavioral where the seam is isolable (decider, routed provenance, guard
executors with a stub cursor, deriver output) and source-contract where the seam sits
inside the monolithic ingest transaction (the probe SQL text) - each source pin says so.
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
        rows.append((d.get("rel_type"), d.get("object")))
    return rows


# ---------- FIX A - deriver ----------

def test_r10_possessed_city_composes_whole_address_and_city():
    """RG-G2 wound A: the possessed city composes whole - never a bare-head shard,
    never a doubled-prep junk rel."""
    rows = _edges("We live at 9 Lattenmaker's Gate in Sporer's Field.")
    live_at = [o for r, o in rows if r == "live_at"]
    lives_in = [o for r, o in rows if r == "lives_in"]
    assert any(o == "9 lattenmaker's gate in sporer's field" for o in live_at), rows
    assert any(o == "sporer's field" for o in lives_in), rows
    assert not any(isinstance(r, str) and r.startswith("live_at_") for r, _ in rows), rows
    assert not any(o == "field" for _, o in rows), rows


def test_r10_residence_compose_gated_to_residence_predicates():
    """The compose's metadata rail: a non-residence verb clause keeps its own reading -
    'work at Google in Mountain View' never mints a lives_in."""
    rows = _edges("I work at Google in Mountain View.")
    assert not any(r == "lives_in" for r, _ in rows), rows
    assert any(r == "works_for" and o == "google" for r, o in rows), rows


def test_r10_residence_compose_keeps_residual_seam_for_non_addresses():
    """Non-address clauses keep the residual-place seam byte-identical (G8(b)): the
    Best Buy store still rides a verb_prep rel - no compose, no lives_in."""
    rows = _edges("I got a new Samsung Galaxy S22 from the Best Buy store.")
    assert any(r == "get_from" and o == "best buy" for r, o in rows), rows
    assert not any(r == "lives_in" for r, _ in rows), rows


def test_r10_bare_city_address_shape_unchanged():
    """The R7 orphan-shape plant ('31 Scrimgeour's Lane in Weftmoor', city PP hanging off
    the recovered ROOT noun) stays byte-identical - the compose only fires on the
    normal-attach shape that used to shard."""
    rows = _edges("We live at 31 Scrimgeour's Lane in Weftmoor.")
    assert any(r == "live_at" and o == "31 scrimgeour's lane in weftmoor"
               for r, o in rows), rows


# ---------- FIX B - attr lane gate ----------

def _api_main():
    from src.api import main as api_main
    return api_main


def test_r10_f4_gate_supersedes_equal_authority_residence_plant():
    """The exact verdict the F4 gate must reach for a plain residence re-plant at equal
    authority: supersede_existing - the same inputs the gate now sends after routing."""
    api_main = _api_main()
    verdict = api_main._decide_supersede_or_coexist(
        {"fact_provenance": "user_stated", "is_correction": False,
         "rel_type": "lives_at", "temporal_status": "now", "polarity": "affirmed"},
        {"fact_provenance": "user_stated"},
        None,
    )
    assert verdict == "supersede_existing", verdict


def test_r10_routed_provenance_gives_mcp_edges_user_stated_rank():
    """The router parity the F4 gate now applies BEFORE the decider: an mcp-sourced edge
    whose EdgeInput stamp defaulted to 'llm_inferred' ranks user_stated at the gate -
    it can never silently lose the trust firewall to an older user_stated value."""
    api_main = _api_main()
    assert api_main._routed_fact_provenance("mcp", "llm_inferred") == "user_stated"
    assert api_main._routed_fact_provenance("assistant", None) == "user_stated"
    assert api_main._routed_fact_provenance("unattested", "user_stated") == "llm_inferred"


def test_r10_lower_authority_incoming_still_drops():
    """The trust firewall itself is untouched: a genuinely lower-authority incoming still
    loses to the stored value."""
    api_main = _api_main()
    verdict = api_main._decide_supersede_or_coexist(
        {"fact_provenance": "llm_inferred", "is_correction": False,
         "rel_type": "lives_at", "temporal_status": "now", "polarity": "affirmed"},
        {"fact_provenance": "user_stated"},
        None,
    )
    assert verdict == "drop_incoming", verdict


def test_r10_tombstone_probe_only_counts_live_rows_source_contract():
    """RG-G-M4 root cause: the pre-existence probe's SQL must carry the live filter - an
    ARCHIVED tombstone is not a value, and a free slot must accept the first write.
    Source-contract pin: this probe runs inside the monolithic ingest transaction, so the
    SQL text is the isolable contract."""
    import inspect
    # the probe lives in the NESTED _ingest_txn_body closure — read it via its parent
    src = inspect.getsource(_api_main()._ingest_impl)
    assert "def _ingest_txn_body" in src
    assert "SELECT 1 FROM entity_attributes WHERE entity_id = %s " in src
    assert "AND attribute = %s AND superseded_at IS NULL LIMIT 1" in src
    assert "a TOMBSTONE is not a value" in src


# ---------- stub db for the guard executors ----------

class _StubCursor:
    def __init__(self, state):
        self._state = state
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "SELECT id FROM facts" in sql:
            # R12: the end-state guard counts live rows first (fetchall)
            self._state["last_select"] = "facts"
            self._state["rows"] = [(101,)] if self._state.get("facts_live") else []
        elif "SELECT 1 FROM facts" in sql:
            self._state["last_select"] = "facts"
            self._state["fetch"] = [("x",)] if self._state.get("facts_live") else None
        elif "SELECT 1 FROM entity_attributes" in sql:
            self._state["last_select"] = "attrs"
            self._state["fetch"] = [("x",)] if self._state.get("attrs_live") else None
        elif sql.startswith("UPDATE facts SET superseded_at = NULL"):
            self._state["restores"] = self._state.get("restores", 0) + 1
            self._state["fetch"] = None
        elif sql.startswith("UPDATE facts SET superseded_at = now()"):
            self._state["supersedes"] = self._state.get("supersedes", 0) + 1
            self._state["fetch"] = None

    def fetchall(self):
        return self._state.get("rows") or []

    def fetchone(self):
        f = self._state.get("fetch")
        return f[0] if f else None

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _StubConn:
    def __init__(self, state):
        self._state = state
        self.cursor_obj = _StubCursor(state)

    def cursor(self):
        return self.cursor_obj


# ---------- GUARD 1 - same-instant supersede ----------

def test_r10_same_instant_guard_clauses_in_both_executors():
    """Both supersede executors (rel lane + attr twin) exclude rows created inside the
    turn window - re-processing reconciles, it never chain-supersedes its own output."""
    api_main = _api_main()
    import inspect
    exec_src = inspect.getsource(api_main._soft_supersede_conflicting_fact)
    assert "AND created_at < now() - make_interval(secs => %s)" in exec_src
    assert "INGEST_SELF_SUPERSEDE_MIN_AGE_SEC" in exec_src
    txn_src = inspect.getsource(api_main._ingest_impl)  # nested _ingest_txn_body closure
    assert "AND created_at < now() - make_interval(secs => %s)" in txn_src


def test_r10_same_instant_guard_executes_windowed_update():
    """Behavior: the rel-lane executor issues its UPDATE with the window parameter."""
    api_main = _api_main()
    conn = _StubConn({"facts_live": False})
    n = api_main._soft_supersede_conflicting_fact(conn, "u1", "lives_in", "o9")
    assert n == 0  # stub reports rowcount None -> 0
    ups = [e for e in conn.cursor_obj.executed
           if e[0].startswith("UPDATE facts SET superseded_at = now()")]
    assert ups, conn.cursor_obj.executed
    sql, params = ups[0]
    assert "AND created_at < now() - make_interval(secs => %s)" in sql
    assert params[3] == 2.0  # the R12 default window (8s blocked a legit 5s-apart re-plant)


def test_r10_zero_window_disables_same_instant_guard():
    """Fail-safe escape hatch: window <= 0 -> byte-identical to the pre-R10 UPDATE."""
    api_main = _api_main()
    old = os.environ.get("INGEST_SELF_SUPERSEDE_MIN_AGE_SEC")
    os.environ["INGEST_SELF_SUPERSEDE_MIN_AGE_SEC"] = "0"
    try:
        conn = _StubConn({"facts_live": False})
        api_main._soft_supersede_conflicting_fact(conn, "u1", "lives_in", "o9")
        ups = [e for e in conn.cursor_obj.executed
               if e[0].startswith("UPDATE facts SET superseded_at = now()")]
        assert ups and "created_at" not in ups[0][0]
    finally:
        if old is None:
            os.environ.pop("INGEST_SELF_SUPERSEDE_MIN_AGE_SEC", None)
        else:
            os.environ["INGEST_SELF_SUPERSEDE_MIN_AGE_SEC"] = old


# ---------- GUARD 2 - end-state ----------

def test_r10_endstate_guard_restores_when_no_live_residence():
    """Facts zero + attrs zero + an archived prior predating the window -> the guard
    restores exactly one row (the last-known truth).""" 
    api_main = _api_main()
    conn = _StubConn({"facts_live": False, "attrs_live": False})
    api_main._ensure_live_residence_end_state(conn, "u1")
    ups = [e for e in conn.cursor_obj.executed
           if e[0].startswith("UPDATE facts SET superseded_at = NULL")]
    assert ups, conn.cursor_obj.executed
    sql, params = ups[0]
    assert "ORDER BY superseded_at DESC LIMIT 1" in sql
    assert "created_at < now() - make_interval(secs => %s)" in sql  # never this turn's rows


def test_r10_endstate_guard_holds_when_facts_residence_live():
    api_main = _api_main()
    conn = _StubConn({"facts_live": True})
    api_main._ensure_live_residence_end_state(conn, "u1")
    assert not [e for e in conn.cursor_obj.executed if e[0].startswith("UPDATE")]


def test_r10_endstate_guard_holds_when_attr_residence_live():
    """The attr lane IS a residence: a live whole-address attr means the user has a
    residence - a facts-zero state is then the normal history shape."""
    api_main = _api_main()
    conn = _StubConn({"facts_live": False, "attrs_live": True})
    api_main._ensure_live_residence_end_state(conn, "u1")
    assert not [e for e in conn.cursor_obj.executed if e[0].startswith("UPDATE")]


def test_r10_endstate_guard_no_archived_prior_is_honest_noop():
    api_main = _api_main()
    conn = _StubConn({"facts_live": False, "attrs_live": False})
    api_main._ensure_live_residence_end_state(conn, "u1")  # rowcount 0 -> must not raise
    ups = [e for e in conn.cursor_obj.executed
           if e[0].startswith("UPDATE facts SET superseded_at = NULL")]
    assert ups  # attempted, found nothing - the honest fresh state


def test_r10_endstate_guard_fail_safe_on_dead_conn():
    api_main = _api_main()
    api_main._ensure_live_residence_end_state(None, "u1")  # must not raise
    api_main._ensure_live_residence_end_state(_StubConn({}), None)
