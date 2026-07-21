"""Pure-NP scalar-attribute surfacing + overscope boundary — determine_path ATTRSCOPE.

Captured pure-NP scalar attributes on the USER anchor — ``daily_commute="45 minutes each way"``,
``new_internet_plan="500 mbps"`` — were captured clean but NOT SURFACED: a "how long is my daily
commute" / "what speed is my new internet plan" question resolved no scope, so recall returned
"No relevant facts found" (systemic captured-not-surfaced gap: schedule, commute, internet, …).

ATTRSCOPE (main.py ~33482) tokenises the anchor-attribute name and matches it against the query
aspect words. The prior tokeniser matched ANY shared token (greedy set-∩). That admitted the
pure-NP exemplars — but OVERSCOPED: an unrelated query sharing only a generic MODIFIER token
("what is my NEW job title" vs new_internet_plan; "my DAILY mood" vs daily_commute) firehosed the
wrong scalar into scope.

FIX (lean-query, ATTRSCOPE HEAD grounding — the Right-hand Head Rule for English compounds,
Williams 1981): match the query aspect words against the attribute's HEAD token — its RIGHTMOST
content constituent ("daily_commute"→commute, "new_internet_plan"→plan, "take_on"→take). The NP
head admits the pure-NP scalar; a lone generic MODIFIER (never the head) cannot. Scalar-only +
anchor-grounded (reads the anchor's OWN entity_attributes) → no relationship edge, no cross-group
leak. Deterministic word-boundary tokenisation, no cosine/LLM/word-zoo; subject-agnostic (head
comes from the query + the stored attribute only, no NP/domain literal).

Proofs:
 (a) pure-NP scalar admission + verbatim render for commute/internet.
 (b) OVERSCOPE BOUNDARY — an unrelated query sharing only a generic modifier admits nothing.
     FAILS on the old greedy set-∩ (which admitted new_internet_plan for "new job title"),
     PASSES on the head-grounded fix.
"""
import re

import pytest

from tests.test_schedule_scalar_surfacing import _FakeDB, _FakeCur

ANCHOR = "22222222-2222-2222-2222-222222222222"


def _resolve(query, attr_rows):
    from src.api.main import determine_path
    return determine_path(query, _FakeDB(attr_rows), user_id=None,
                          anchor_resolved_uuid=ANCHOR)


# ── (a) PURE-NP SCALAR ADMISSION (fixes the captured-not-surfaced miss) ───────────────────────────
def test_daily_commute_pure_np_admitted():
    # anchor holds (user, daily_commute, "45 minutes each way"); no natural_language template.
    path = _resolve("how long is my daily commute to work",
                    attr_rows=[("daily_commute", None)])
    assert "daily_commute" in [r.lower() for r in path.scalar_rels], path.scalar_rels
    assert path.scope_active is True


def test_new_internet_plan_pure_np_admitted():
    path = _resolve("what speed is my new internet plan",
                    attr_rows=[("new_internet_plan", None)])
    assert "new_internet_plan" in [r.lower() for r in path.scalar_rels], path.scalar_rels
    assert path.scope_active is True


def test_ram_upgrade_compound_admitted():
    path = _resolve("how much RAM did I upgrade my laptop to",
                    attr_rows=[("ram_upgrade", None)])
    assert "ram_upgrade" in [r.lower() for r in path.scalar_rels], path.scalar_rels
    assert path.scope_active is True


# ── (a) RENDER seam emits the verbatim pure-NP value ──────────────────────────────────────────────
def test_prose_renders_commute_value():
    from src.api.main import convert_to_prose

    class _NullDB:
        def cursor(self):
            return _FakeCur([])

    fact = {
        "subject": ANCHOR, "_subject_id": ANCHOR,
        "rel_type": "daily_commute", "object": "45 minutes each way",
        "fact_class": "A", "source": "attributes",
        "confidence": 1.0, "category": None,
    }
    prose = convert_to_prose([fact], _NullDB(), anchor=ANCHOR, user_id=ANCHOR)
    assert any("45 minutes each way" in p.lower() for p in prose), prose


# ── (b) OVERSCOPE BOUNDARY — a lone generic modifier must NOT admit the scalar ────────────────────
#     FAIL-ON-OLD: the greedy set-∩ admitted new_internet_plan (shared "new") / daily_commute
#     (shared "daily"); the head-grounded match rejects them.
def test_generic_modifier_new_does_not_admit_internet_plan():
    path = _resolve("what is my new job title",
                    attr_rows=[("new_internet_plan", None)])
    assert "new_internet_plan" not in [r.lower() for r in path.scalar_rels], path.scalar_rels


def test_generic_modifier_daily_does_not_admit_commute():
    path = _resolve("what is my daily mood",
                    attr_rows=[("daily_commute", None)])
    assert "daily_commute" not in [r.lower() for r in path.scalar_rels], path.scalar_rels


def test_unrelated_query_admits_nothing_no_overscope():
    # a query naming NONE of the anchor's attribute heads must admit nothing (fetch-all boundary).
    path = _resolve("what is my mother's birthday",
                    attr_rows=[("daily_commute", None), ("new_internet_plan", None)])
    admitted = [r.lower() for r in path.scalar_rels]
    assert "daily_commute" not in admitted, admitted
    assert "new_internet_plan" not in admitted, admitted
    assert path.scope_active is False


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
