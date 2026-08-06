"""Regression gate for the GLiNER2 PER-INSTANCE label ceiling (Pitfall 11's other half).

THE DEFECT. Pitfall 11 is grounded in GLiREL, which states it outright:

    "We limit the number of relation type labels prepended to each training instance to 25."
    (arxiv:2501.03172)

The comment above the cap constants in main.py says exactly that. But the SPLIT-BUDGET change
made the caps PER-POOL — scalar 15, relational 20 — and then MERGED both pools into ONE dict
handed to a SINGLE extract_relations() call. The count the model actually sees is their SUM: 35,
which is 40% BEYOND the training maximum, a sequence shape GLiREL never saw. Measured on the live
line: one /ingest call carried 33 labels, and GLiNER2 fired two conflicting scalar labels on one
span ({'age': [('Rowan','12')], 'has_gender': [('Rowan','12')]}).

The paper also measures the cost of merely APPROACHING the cap (F1 by candidate count m):
    m=5 → 94.20 (FewRel) / 83.28 (Wiki-ZSL)
    m=10 → 87.60         / 83.67
    m=15 → 84.48         / 73.91
so 5→15 labels costs ~10 F1. Fewer labels is measurably MORE accurate, not merely safer.

THE FIX enforces the ceiling at the one point both pools are still distinguishable, sacrificing
relational candidates first (a scalar rel is the more specific storage path) but never below a
reserved floor — because "a big scalar pool crowds relational out entirely" is the exact failure
the split budgets were introduced to fix. Truncation is always logged, never silent.
"""
import pytest

from src.api import main as m


def _pool(prefix, n):
    return {f"{prefix}{i}": {"description": "", "threshold": 0.6} for i in range(n)}


def test_the_shipped_defaults_no_longer_breach_the_training_cap():
    """15 scalar + 20 relational = 35 was being sent; the ceiling is 25."""
    scalars, relational = m._enforce_gliner2_total_label_budget(
        _pool("s", m._GLINER2_SCALAR_LABEL_CAP), _pool("r", m._GLINER2_RELATIONAL_LABEL_CAP))
    assert len(scalars) + len(relational) <= m._GLINER2_TOTAL_LABEL_CAP
    assert m._GLINER2_TOTAL_LABEL_CAP == 25, "the ceiling is GLiREL's stated training maximum"


def test_a_set_within_the_ceiling_is_untouched():
    s, r = _pool("s", 5), _pool("r", 5)
    assert m._enforce_gliner2_total_label_budget(s, r) == (s, r)


def test_exactly_at_the_ceiling_is_untouched():
    s, r = _pool("s", 10), _pool("r", 15)
    kept_s, kept_r = m._enforce_gliner2_total_label_budget(s, r)
    assert len(kept_s) + len(kept_r) == 25
    assert (kept_s, kept_r) == (s, r)


def test_relational_is_sacrificed_before_scalar():
    """A scalar rel is the more specific storage path, so it keeps its slots first."""
    kept_s, kept_r = m._enforce_gliner2_total_label_budget(_pool("s", 15), _pool("r", 20))
    assert len(kept_s) == 15, kept_s
    assert len(kept_r) == 10, kept_r


def test_relational_is_never_crowded_out_entirely():
    """The exact failure the split budgets were introduced to fix must not come back."""
    kept_s, kept_r = m._enforce_gliner2_total_label_budget(_pool("s", 40), _pool("r", 20))
    assert len(kept_r) == m._GLINER2_RELATIONAL_FLOOR, kept_r
    assert len(kept_s) + len(kept_r) == m._GLINER2_TOTAL_LABEL_CAP


def test_the_floor_never_invents_relational_labels():
    """With few relational candidates the floor shrinks to what actually exists."""
    kept_s, kept_r = m._enforce_gliner2_total_label_budget(_pool("s", 40), _pool("r", 3))
    assert len(kept_r) == 3, kept_r
    assert len(kept_s) + len(kept_r) == m._GLINER2_TOTAL_LABEL_CAP


def test_trimming_is_a_deterministic_tail_drop():
    """Pools arrive ranked (seeded backbone first), so the survivors are the highest-value head."""
    kept_s, kept_r = m._enforce_gliner2_total_label_budget(_pool("s", 15), _pool("r", 20))
    assert list(kept_s) == [f"s{i}" for i in range(15)]
    assert list(kept_r) == [f"r{i}" for i in range(10)]


def test_truncation_is_logged_never_silent(monkeypatch):
    seen = {}

    def _warn(event, **kw):
        seen[event] = kw

    monkeypatch.setattr(m.log, "warning", _warn)
    m._enforce_gliner2_total_label_budget(_pool("s", 15), _pool("r", 20))
    assert "gliner2.total_label_cap_enforced" in seen, seen
    kw = seen["gliner2.total_label_cap_enforced"]
    assert kw["before_total"] == 35 and kw["after_total"] == 25
    assert kw["dropped_relational"], "the dropped labels must be named, not just counted"


def test_empty_pools_are_safe():
    assert m._enforce_gliner2_total_label_budget({}, {}) == ({}, {})
    assert m._enforce_gliner2_total_label_budget(None, None) == ({}, {})


@pytest.mark.parametrize("scalar_n,rel_n", [(0, 40), (40, 0), (13, 13), (25, 25)])
def test_the_ceiling_holds_for_any_pool_shape(scalar_n, rel_n):
    kept_s, kept_r = m._enforce_gliner2_total_label_budget(_pool("s", scalar_n), _pool("r", rel_n))
    assert len(kept_s) + len(kept_r) <= m._GLINER2_TOTAL_LABEL_CAP
