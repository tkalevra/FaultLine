"""HIER1 — a promoted staged fact must carry its is_hierarchy_rel into `facts`.

Latent correctness gap: `promote_staged_facts` / `promote_class_c_hits` in the
re-embedder INSERTed the promoted row into `facts` WITHOUT the `is_hierarchy_rel`
column, so a promoted hierarchy rung (subclass_of/part_of/…) landed with the column
DEFAULT false. Harmless today (readers resolve hierarchy-ness from the rel_types
overlay), but the promoted row was simply wrong and it is a latent landmine for any
reader that trusts the per-row column.

Authoritative source (mirrors ingest): store.py writes `facts.is_hierarchy_rel` from
the value carried in the connection tuple (resolved from rel_types metadata), and
`store_class_c_facts` writes the SAME value onto the staged row. So the promotion
faithfully PASSES THROUGH the staged row's own is_hierarchy_rel — no new mechanism.

Fast, DB-free: a faked psycopg2-style connection/cursor answers the SELECT with a
hierarchy row and a relational row, and captures the INSERT-INTO-facts params. No
live Postgres, no Qdrant, no embedding model.
"""

import re

import pytest

from src.re_embedder import embedder


# ── Fake DB plumbing ─────────────────────────────────────────────────────────────────────

def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip().lower())


class _State:
    """Shared state across the per-`cursor()` FakeCursor instances."""

    def __init__(self, candidates):
        self.candidates = candidates          # rows the staged-fact SELECT returns
        self.facts_inserts = []               # (sql, params) captured for INSERT INTO facts
        self.insert_columns = None            # parsed column list of the facts INSERT


class FakeCursor:
    def __init__(self, state: "_State"):
        self._s = state
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def rowcount(self):
        return 0

    def execute(self, sql, params=()):
        s = _norm(sql)
        self._result = None
        if s.startswith("select") and "from staged_facts" in s and "fact_class = 'b'" in s:
            # the B→facts promotion candidate SELECT
            self._result = list(self._s.candidates)
        elif s.startswith("insert into facts"):
            self._s.facts_inserts.append((sql, params))
            # parse the parenthesised column list right after "insert into facts"
            m = re.search(r"insert into facts\s*\(([^)]*)\)", s)
            if m:
                self._s.insert_columns = [c.strip() for c in m.group(1).split(",")]
        # everything else (SET search_path, C→B UPDATE, UPDATE promoted_at) → no-op

    def fetchall(self):
        return self._result or []

    def fetchone(self):
        return None


class FakeConn:
    def __init__(self, state: "_State"):
        self._s = state

    def cursor(self):
        return FakeCursor(self._s)

    def commit(self):
        pass

    def rollback(self):
        pass


# ── The staged rows the SELECT yields ──────────────────────────────────────────────────────
# Column order MUST match the SELECT in promote_staged_facts:
#   id, subject_id, object_id, rel_type, provenance, fact_provenance, confidence,
#   temporal_status, event_date, event_date_granularity, source_ref, is_hierarchy_rel
_HIER_ROW = (
    "sid-hier", "subj-1", "obj-1", "subclass_of", "prov", "user_stated", 0.8,
    "now", None, None, None, True,        # <- is_hierarchy_rel = True (a hierarchy rung)
)
_REL_ROW = (
    "sid-rel", "subj-2", "obj-2", "owns", "prov", "user_stated", 0.8,
    "now", None, None, None, False,       # <- is_hierarchy_rel = False (relational)
)


def _run_promote(monkeypatch, rows):
    # best-effort Qdrant cleanup does a network call after commit — stub it out
    monkeypatch.setattr(embedder, "_qdrant_delete_fact_point", lambda *a, **k: None)
    state = _State(rows)
    conn = FakeConn(state)
    embedder.promote_staged_facts(conn, qdrant_url="http://qdrant:6333", user_id="u")
    return state


def _hier_value(sql: str, params) -> bool:
    """The value bound to the is_hierarchy_rel column of a facts INSERT.

    The INSERT has two LITERAL-valued columns (fact_class 'B', qdrant_synced false),
    so column index != param index — column-index arithmetic into params is wrong.
    is_hierarchy_rel is the LAST column AND the LAST bound %s / last param, so read
    it off the tail (and assert the column really is present + last).
    """
    s = _norm(sql)
    cols = [c.strip() for c in re.search(r"insert into facts\s*\(([^)]*)\)", s).group(1).split(",")]
    assert cols[-1] == "is_hierarchy_rel", f"is_hierarchy_rel not last INSERT col: {cols}"
    return params[-1]


def test_promoted_hierarchy_rel_carries_true(monkeypatch):
    state = _run_promote(monkeypatch, [_HIER_ROW])
    assert len(state.facts_inserts) == 1
    sql, params = state.facts_inserts[0]
    assert "is_hierarchy_rel" in state.insert_columns
    assert _hier_value(sql, params) is True


def test_promoted_relational_rel_carries_false(monkeypatch):
    state = _run_promote(monkeypatch, [_REL_ROW])
    assert len(state.facts_inserts) == 1
    sql, params = state.facts_inserts[0]
    assert _hier_value(sql, params) is False


def test_insert_placeholder_and_param_arity(monkeypatch):
    # guard the %s/param arity: a mismatched INSERT would raise at execute time in prod.
    # The column list has 2 literal-valued columns ('B', false), so n_cols = n_%s + 2.
    state = _run_promote(monkeypatch, [_HIER_ROW, _REL_ROW])
    assert len(state.facts_inserts) == 2
    for sql, params in state.facts_inserts:
        s = _norm(sql)
        n_cols = len(re.search(r"insert into facts\s*\(([^)]*)\)", s).group(1).split(","))
        n_vals = s.count("%s", 0, s.index("on conflict"))
        assert n_vals == len(params), f"placeholder/param mismatch vals={n_vals} params={len(params)}"
        assert n_cols == n_vals + 2, f"literal-column count drifted: cols={n_cols} vals={n_vals}"
