"""EP1-shaped LANE TESTS (gauntlet emitter-provenance-honesty) — the flip, at lane level.

For every lane fixed in this gauntlet: flip the STORED provenance of one row to
llm_inferred (and reextract) and NO copy the lane emits may carry fact_class "A" with
confidence 1.0 or an ABSENT fact_provenance; the control flip on user_stated (and the
EP3 legacy values mcp/assistant, which canonicalise to user_stated through
_SCALAR_READ_PROVENANCE_CANON) keeps serving A/1.0 exactly as before — the scalar-retract
campaign's 367/384 blast-radius promise must not regress.

Conventions follow tests/test_scalar_retract_recall_leak.py: a dispatching fake cursor
answers by SQL TEXT (never call order) so a test asserts on the SQL the lane actually
issued as well as on what it emitted. Non-vacuity: every assertion below requires
fact_provenance to be PRESENT and the tier to track the flipped provenance — the pre-fix
code paths never emitted fact_provenance and stamped a flat ("A", 1.0), so each test is
red against the reverted lane by construction; the census suite additionally proves
revert-red at source level.
"""

import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.api.main import (  # noqa: E402
    _resolve_compound_rel_terminal,
    fetch_superseded_facts,
    _collect_descendant_layered_facts,
)

ANCHOR = "11111111-2222-4333-8444-555555555555"
SEED = "66666666-7777-4888-9999-000000000000"
USER = "04c0787b-8959-48de-a07b-4f0b60d11e43"


def _assert_not_fabricated(fact, lane):
    assert not (fact.get("fact_class") == "A" and fact.get("confidence") == 1.0), (
        f"{lane}: llm_inferred row still served in the asserted band: {fact!r}")
    assert fact.get("fact_provenance"), (
        f"{lane}: fact_provenance absent — server.py::_fact_is_asserted reads absent "
        f"provenance as ASSERTED: {fact!r}")


# ── shared fake-cursor plumbing ───────────────────────────────────────────────────────

class _Cursor:
    """Dispatches fetchall/fetchone by SQL text needles."""

    def __init__(self, table):
        self._table = table
        self.seen = []

    def execute(self, sql, params=None):
        self._last = " ".join(str(sql).split())
        self.seen.append(self._last)

    def _has(self, *needles):
        return all(n in self._last for n in needles)

    def fetchall(self):
        for needles, rows in self._table.get("all", []):
            if all(n in self._last for n in needles):
                return rows
        return []

    def fetchone(self):
        for needles, row in self._table.get("one", []):
            if all(n in self._last for n in needles):
                return row
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _conn(cursor):
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.cursor.return_value.__enter__ = lambda s: cursor
    conn.cursor.return_value.__exit__ = lambda s, *a: False
    return conn


# ── _resolve_compound_rel_terminal — the resolve_typed_walk SHORT-CIRCUIT door ────────

def _compound_conn(attr_rows=(), fact_rows=()):
    cur = _Cursor({
        "all": [
            (["FROM rel_type_aliases"], []),
            (["FROM facts", "rel_type = ANY"], list(fact_rows)),
            (["FROM staged_facts", "rel_type = ANY"], []),
            (["FROM entity_attributes", "attribute = ANY"], list(attr_rows)),
        ],
        "one": [],
    })
    return _conn(cur), cur


def _compound_attr(provenance):
    # (attribute, value_text, value_int, value_float, value_date, provenance)
    return [("favorite_color", "cerulean", None, None, None, provenance)]


def test_compound_rel_scalar_flip_to_llm_inferred_is_not_asserted():
    conn, _cur = _compound_conn(attr_rows=_compound_attr("llm_inferred"))
    res = _resolve_compound_rel_terminal(conn, ANCHOR, ["favorite", "color"])
    assert res and res.get("status") == "answer", f"door did not answer: {res!r}"
    fact = res["facts"][0]
    _assert_not_fabricated(fact, "_resolve_compound_rel_terminal")
    assert fact["fact_provenance"] == "llm_inferred"
    assert fact["object"] == "cerulean"  # surfaced, retiered — not deleted


def test_compound_rel_scalar_flip_to_reextract_is_not_asserted():
    conn, _cur = _compound_conn(attr_rows=_compound_attr("reextract"))
    res = _resolve_compound_rel_terminal(conn, ANCHOR, ["favorite", "color"])
    fact = res["facts"][0]
    _assert_not_fabricated(fact, "_resolve_compound_rel_terminal(reextract)")
    assert fact["fact_provenance"] == "llm_inferred"


def test_compound_rel_control_user_stated_keeps_a_1():
    conn, _cur = _compound_conn(attr_rows=_compound_attr("user_stated"))
    fact = _resolve_compound_rel_terminal(conn, ANCHOR, ["favorite", "color"])["facts"][0]
    assert (fact["fact_class"], fact["confidence"]) == ("A", 1.0), fact
    assert fact["fact_provenance"] == "user_stated"


def test_compound_rel_ep3_legacy_mcp_assistant_keep_a_1():
    """EP3: legacy source values canonicalise through the ingest-router mapping to
    user_stated and keep A/1.0 — no blast-radius regression on this door."""
    for prov in ("mcp", "assistant", "user_corrected"):
        conn, _cur = _compound_conn(attr_rows=_compound_attr(prov))
        fact = _resolve_compound_rel_terminal(conn, ANCHOR, ["favorite", "color"])["facts"][0]
        assert (fact["fact_class"], fact["confidence"]) == ("A", 1.0), (prov, fact)
        assert fact["fact_provenance"] == "user_stated", (prov, fact)


def test_compound_rel_relational_half_carries_the_row_tier():
    """The relational half of the door must not fabricate either: an llm_inferred edge
    row (B, 0.5) is served as B/0.5, and a user_stated A row keeps A."""
    # (rel_type, object_id, confidence, fact_class, fact_provenance)
    inferred = [("favorite_color", "cerulean", 0.5, "B", "llm_inferred")]
    conn, _cur = _compound_conn(fact_rows=inferred)
    fact = _resolve_compound_rel_terminal(conn, ANCHOR, ["favorite", "color"])["facts"][0]
    assert (fact["fact_class"], fact["confidence"]) == ("B", 0.5), fact
    assert fact["fact_provenance"] == "llm_inferred"

    stated = [("favorite_color", "cerulean", 1.0, "A", "user_stated")]
    conn, _cur = _compound_conn(fact_rows=stated)
    fact = _resolve_compound_rel_terminal(conn, ANCHOR, ["favorite", "color"])["facts"][0]
    assert (fact["fact_class"], fact["confidence"]) == ("A", 1.0), fact


def test_compound_rel_sql_selects_provenance():
    """The lane must SELECT the provenance column — a derivation over a column the SQL
    never fetched silently canonicalises to llm_inferred for everyone."""
    conn, cur = _compound_conn(attr_rows=_compound_attr("user_stated"))
    _resolve_compound_rel_terminal(conn, ANCHOR, ["favorite", "color"])
    attr_sql = [s for s in cur.seen if "FROM entity_attributes" in s and "attribute = ANY" in s]
    assert attr_sql and "provenance" in attr_sql[0], attr_sql


# ── fetch_superseded_facts — the retired-scalar HISTORY lane (EP1's history intent) ───

def _history_run(superseded_attr_rows):
    now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    cur = _Cursor({
        "all": [
            (["FROM facts", "superseded_at IS NOT NULL", "subject_id = %s"], []),
            (["FROM facts", "superseded_at IS NOT NULL", "object_id = %s"], []),
            (["FROM entity_attributes", "superseded_at IS NOT NULL"],
             list(superseded_attr_rows)),
        ],
        "one": [],
    })
    conn = _conn(cur)
    with patch.dict(os.environ, {"POSTGRES_DSN": "postgresql://test"}):
        with patch("src.api.main.psycopg2.connect") as mock_connect, \
                patch("src.api.main.get_user_schema_context",
                      return_value={"schema_name": "faultline_ep_test"}):
            mock_connect.return_value.__enter__.return_value = conn
            facts = fetch_superseded_facts(ANCHOR, USER)
    return facts, cur


def _hist_row(provenance):
    # (attribute, value_text, value_int, value_float, value_date,
    #  valid_until, superseded_at, provenance)
    return ("address", "12 Former Lane", None, None, None, None,
            datetime(2026, 9, 1, tzinfo=timezone.utc), provenance)


def test_history_lane_flip_to_llm_inferred_is_not_asserted():
    facts, _cur = _history_run([_hist_row("llm_inferred")])
    hits = [f for f in facts if f.get("source") == "db_superseded_attr"]
    assert hits, "history lane dropped the retired row — retire is RETAIN, not erase"
    fact = hits[0]
    _assert_not_fabricated(fact, "fetch_superseded_facts")
    assert fact["fact_provenance"] == "llm_inferred"
    # the historical framing survives the honesty fix
    assert fact.get("historical") is True and fact.get("temporal_status") == "past"


def test_history_lane_control_user_stated_keeps_a_1():
    facts, _cur = _history_run([_hist_row("user_stated")])
    fact = [f for f in facts if f.get("source") == "db_superseded_attr"][0]
    assert (fact["fact_class"], fact["confidence"]) == ("A", 1.0), fact
    assert fact["fact_provenance"] == "user_stated"


def test_history_lane_ep3_legacy_mcp_keeps_a_1():
    facts, _cur = _history_run([_hist_row("mcp"), _hist_row("assistant")])
    hits = [f for f in facts if f.get("source") == "db_superseded_attr"]
    assert len(hits) == 2
    for fact in hits:
        assert (fact["fact_class"], fact["confidence"]) == ("A", 1.0), fact
        assert fact["fact_provenance"] == "user_stated", fact


def test_history_lane_sql_selects_provenance():
    _facts, cur = _history_run([_hist_row("user_stated")])
    attr_sql = [s for s in cur.seen if "FROM entity_attributes" in s]
    assert attr_sql and "provenance" in attr_sql[0], attr_sql


# ── _collect_own (via _collect_descendant_layered_facts) — the descendant-merge feeder ─

def _layered_run(attr_rows):
    cur = _Cursor({
        "all": [
            (["FROM facts", "object_id = ANY"], []),          # BFS descent: no descendants
            (["FROM facts", "subject_id = %s"], []),          # _collect_own relational half
            (["FROM entity_attributes", "entity_id = %s"],
             list(attr_rows)),
        ],
        "one": [],
    })
    return _collect_descendant_layered_facts(
        cur, ANCHOR, ["part_of"], frozenset({"instance_of"}), None, 1,
        seed_entities={SEED})


def _own_row(provenance):
    # (entity_id, attribute, value_text, value_int, value_float, value_date, provenance)
    return (SEED, "firmware_version", "2.1.7", None, None, None, provenance)


def test_collect_own_flip_to_llm_inferred_is_not_asserted():
    """This lane feeds deduplicate_facts, which keeps the HIGHEST-confidence copy — a
    fabricated 1.0 here OVERWRITES an honest demotion from a fixed lane."""
    out = _layered_run([_own_row("llm_inferred")])
    hits = [f for f in out if f.get("source") == "attributes"]
    assert hits, "descendant scalar dropped — retier, never delete"
    _assert_not_fabricated(hits[0], "_collect_own")
    assert hits[0]["fact_provenance"] == "llm_inferred"




# ── ROUND 2 (critic gap): the SUM-OVER-WALK aggregate + db-lane NULL-tier fallbacks ──

from src.api.main import (  # noqa: E402
    _l4_direct_scalar_sum,
    _build_sum_over_walk_fact,
)


def _sum_conn(attr_rows):
    cur = _Cursor({
        "all": [
            (["FROM facts", "object_id = ANY"], [("inst-1",)]),
            (["FROM staged_facts", "object_id = ANY"], []),
            (["FROM entity_attributes", "entity_id = ANY"], list(attr_rows)),
        ],
        "one": [],
    })
    return _conn(cur)


def _sum_row(provenance, vtext):
    # (entity_id, attribute, value_int, value_text, provenance)
    return ("e1", "cost", None, vtext, provenance)


def test_sum_aggregate_worst_of_set_is_llm_inferred():
    """WORST-OF-SET: one llm_inferred component makes the whole total an inference —
    the number the model would state is numerically contaminated by a guess."""
    res = _l4_direct_scalar_sum(_sum_conn([
        _sum_row("user_stated", "30 dollars"),
        _sum_row("llm_inferred", "45 dollars"),
    ]), {"t1"}, None)
    assert res["miss"] is False and res["sum"] == 75.0, res
    assert res["provenance"] == "llm_inferred", res


def test_sum_aggregate_all_stated_is_user_stated_and_ep3_legacy():
    """All-user-stated keeps user_stated; legacy mcp/assistant canonicalise INTO the
    stated set (EP3 — an aggregate over mcp rows is an aggregate over the user's own
    words)."""
    res = _l4_direct_scalar_sum(_sum_conn([
        _sum_row("user_stated", "30 dollars"),
        _sum_row("mcp", "45 dollars"),
        _sum_row("assistant", "25 dollars"),
    ]), {"t1"}, None)
    assert res["provenance"] == "user_stated", res


def test_sum_over_walk_fact_flips_with_constituents():
    """THE gap, end to end: the rendered aggregate carries the worst-of-set tier —
    an inferred total is NOT the asserted band, and the provenance key is present."""
    stated = _build_sum_over_walk_fact(
        {"miss": False, "sum": 75.0, "is_currency": True,
         "n_components": 2, "n_instances": 2, "provenance": "user_stated"}, "anchor")
    assert (stated["fact_class"], stated["confidence"]) == ("A", 1.0), stated
    assert stated["fact_provenance"] == "user_stated"

    inferred = _build_sum_over_walk_fact(
        {"miss": False, "sum": 75.0, "is_currency": True,
         "n_components": 2, "n_instances": 2, "provenance": "llm_inferred"}, "anchor")
    assert not (inferred["fact_class"] == "A" and inferred["confidence"] == 1.0), (
        f"inferred-total aggregate still asserts: {inferred!r}")
    assert inferred["fact_provenance"] == "llm_inferred", (
        "absent provenance + class A is the ASSERTED band via _fact_is_asserted")


def test_sum_over_walk_miss_stays_held():
    miss = _build_sum_over_walk_fact(
        {"miss": True, "sum": 0.0, "n_components": 0}, "anchor")
    assert miss["fact_class"] == "C" and miss["confidence"] == 0.5


def test_sum_input_lane_selects_provenance():
    conn = _sum_conn([_sum_row("user_stated", "30 dollars")])
    _l4_direct_scalar_sum(conn, {"t1"}, None)
    sql = [s for s in conn.cursor.return_value.seen if "FROM entity_attributes" in s]
    assert sql and "provenance" in sql[0], sql






# ── ROUND 3 (critic r2 gap): the INFORMATIVE ABSTENTION worst-of-set ──────────────────

def _abstention_run(neighbour_provs):
    """Drive _fetch_informative_abstention with a fake neighbour set. Each entry is
    the stored (fact_class, confidence, fact_provenance) of one matched neighbour."""
    from unittest.mock import patch as _p
    import src.api.main as M
    rows = [("owns", "11111111-2222-4333-8444-555555555555", fc, conf, prov)
            for (fc, conf, prov) in neighbour_provs]

    class _C:
        def execute(self, sql, params=None):
            self._last = " ".join(str(sql).split())

        def fetchall(self):
            if "COALESCE(fact_provenance" in getattr(self, "_last", ""):
                return rows
            return []

        def fetchone(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    conn = MagicMock()
    conn.cursor.return_value = _C()
    with _p.object(M, "get_gliner_model", return_value=object()), \
            _p.object(M, "_gliner_canonical_type", lambda t, g: "object"), \
            _p.object(M, "_resolve_type_signals", lambda db, oid: ({"object"}, True)), \
            _p.object(M, "convert_to_prose", staticmethod(
                lambda facts, db, anchor=None, user_id=None: ["You own a plinth novac."])), \
            _p.object(M._turn_budget, "remaining", return_value=30.0), \
            _p.object(M._turn_budget, "can_afford", return_value=True):
        return M._fetch_informative_abstention(
            "What is the name of my hamster?", {"hamster"}, conn, USER, "sch")


def test_abstention_all_stated_keeps_asserted_voice():
    fact = _abstention_run([("A", 1.0, "user_stated"), ("A", 1.0, "mcp")])
    assert fact is not None, "lane declined to answer on a grounded neighbourhood"
    assert fact["fact_provenance"] == "user_stated", fact
    assert fact["confidence"] == 1.0 and fact["fact_class"] == "A", fact


def test_abstention_flip_follows_worst_of_set():
    """THE r3 gap: one llm_inferred neighbour makes the composed abstention carry
    the inference — never a hardcoded user_stated/1.0 assertion over guesses."""
    fact = _abstention_run([("A", 1.0, "user_stated"), ("B", 0.5, "llm_inferred")])
    assert fact is not None
    assert fact["fact_provenance"] == "llm_inferred", fact
    assert not (fact["fact_class"] == "A" and fact["confidence"] == 1.0), fact
    # absent-provenance neighbours (COALESCE '') canonicalise to llm_inferred too
    fact2 = _abstention_run([("B", 0.5, "")])
    assert fact2["fact_provenance"] == "llm_inferred", fact2


def test_abstention_ep3_legacy_stays_stated():
    fact = _abstention_run([("A", 1.0, "assistant"), ("A", 1.0, "user_corrected")])
    assert fact["fact_provenance"] == "user_stated", fact
    assert fact["confidence"] == 1.0, fact

def test_db_lane_null_tier_fallbacks_are_held_shaped():
    """Source-level pin (the lanes' SQL/emission lives inside the /query handler and
    is not unit-drivable): none of the three repaired db lanes may carry a 1.0 or
    '"A"' fallback literal for a NULL stored tier."""
    with open(os.path.join(os.path.dirname(__file__), "..", "src", "api", "main.py"),
              encoding="utf-8") as fh:
        src = fh.read()
    for bad in ('float(_conf) if _conf is not None else (\n                            1.0',
                '"confidence": 1.0 if _tsrc == "facts" else 0.6',
                '"fact_class": float(row[3]) if row[3] else 1.0',
                '"confidence": float(row[3]) if row[3] else 1.0'):
        assert bad not in src, f"NULL-tier 1.0 fallback restored: {bad!r}"
    assert '"fact_class": row[4] or "A",' not in src
    assert '_fc or ("A" if _src == "facts" else "B")' not in src

def test_collect_own_control_and_ep3_keep_a_1():
    for prov in ("user_stated", "mcp", "assistant", "user_corrected"):
        out = _layered_run([_own_row(prov)])
        hit = [f for f in out if f.get("source") == "attributes"][0]
        assert (hit["fact_class"], hit["confidence"]) == ("A", 1.0), (prov, hit)
        assert hit["fact_provenance"] == "user_stated", (prov, hit)
