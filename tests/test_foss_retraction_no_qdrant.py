"""#218 pin: the retraction fallback never consults Qdrant unless the legacy lane is ON.

correct_fact's _retraction_semantic_fallback searched the user's Qdrant collection (top 8,
>= 0.35) to propose candidate facts, with no USER_MEMORY_VECTOR_LANE gate. User memory lives
in Postgres, so with the lane off the candidates now come from a Postgres search over
facts / staged_facts, keyed by the tenant's own grown entity_aliases.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def main_mod():
    import src.api.main as M
    return M


# ── #218: the retraction fallback with the lane off ──────────────────────────

class _Cur:
    def __init__(self, conn):
        self.c = conn
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.c.sql.append(sql)
        s = " ".join(sql.split())
        if s.startswith("SELECT DISTINCT entity_id FROM entity_aliases"):
            spans = set(params[0])
            self._rows = [(eid,) for alias, eid in self.c.aliases.items() if alias in spans]
        elif "FROM facts" in s and "UNION ALL" in s:
            ids = set(params[0])
            self._rows = [r for r in self.c.facts if r[0] in ids or r[2] in ids]
        elif "is_preferred = true" in s and "ANY" in s:
            ids = set(params[0])
            self._rows = [(eid, a) for a, eid in self.c.aliases.items() if eid in ids]
        else:
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _DB:
    def __init__(self, aliases, facts):
        self.aliases, self.facts, self.sql = aliases, facts, []

    def cursor(self):
        return _Cur(self)

    def rollback(self):
        pass


class _Log:
    def __getattr__(self, _n):
        return lambda *a, **k: None


def _no_network(M, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError(f"network call with the lane off: {a[:1]}")
    monkeypatch.setattr(M.httpx, "post", _boom)
    monkeypatch.setattr(M, "embed_text", _boom)


_U = ["11111111-1111-1111-1111-11111111111" + str(i) for i in range(6)]


@pytest.mark.parametrize("text,aliases,facts,expect", [
    ("Actually I don't own a kayak anymore",
     {"kayak": _U[0], "me": _U[1]},
     [(_U[1], "owns", _U[0], "B", 0.8)], "me owns kayak"),
    # fresh domain: nothing about boats; the tenant's own aliases decide what is a mention
    ("That's wrong, the cello lessons were with Ilse",
     {"cello lessons": _U[2], "ilse": _U[3], "me": _U[1]},
     [(_U[1], "takes", _U[2], "C", 0.4), (_U[2], "taught_by", _U[4], "B", 0.7)],
     "me takes cello lessons"),
])
def test_retraction_fallback_uses_postgres_when_lane_off(main_mod, monkeypatch,
                                                         text, aliases, facts, expect):
    M = main_mod
    monkeypatch.setattr(M, "USER_MEMORY_VECTOR_LANE", False)
    monkeypatch.setenv("QDRANT_URL", "http://vectors.example:6333")  # even when configured
    _no_network(M, monkeypatch)
    db = _DB(aliases, facts)
    out = M._retraction_semantic_fallback(text, _U[5], db, _Log())
    assert out and expect in out, out


def test_retraction_fallback_no_candidates_returns_none(main_mod, monkeypatch):
    M = main_mod
    monkeypatch.setattr(M, "USER_MEMORY_VECTOR_LANE", False)
    monkeypatch.delenv("QDRANT_URL", raising=False)
    _no_network(M, monkeypatch)
    assert M._retraction_semantic_fallback("never mind", _U[5], _DB({}, []), _Log()) is None


def test_retraction_fallback_control_lane_on_consults_qdrant(main_mod, monkeypatch):
    """Control: the legacy lane, explicitly on and configured, is still the one that searches."""
    M = main_mod
    monkeypatch.setattr(M, "USER_MEMORY_VECTOR_LANE", True)
    monkeypatch.setenv("QDRANT_URL", "http://vectors.example:6333")
    seen = []
    monkeypatch.setattr(M, "embed_text", lambda *a, **k: [0.0] * 768)

    class _R:
        status_code = 200

        def json(self):
            return {"result": {"points": []}}
    monkeypatch.setattr(M.httpx, "post", lambda url, **k: seen.append(url) or _R())
    M._retraction_semantic_fallback("not a kayak", _U[5], _DB({}, []), _Log())
    assert seen and seen[0].startswith("http://vectors.example:6333/collections/")


