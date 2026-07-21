"""'how long is my <NP>' with a matching user DURATION scalar must anchor the USER,
not a barren owned/fragment entity — LME 118b2229 ("How long is my daily commute to
work?" → "45 minutes each way").

ROOT CAUSE (isolated live, in-process): the value is captured clean as the user scalar
``daily_commute="45 minutes each way"`` and surfaces for "what is my daily commute". But
"how long is my daily commute TO WORK" mis-resolved the ANCHOR: resolve_anchor's Rule 3
n-gram (longest-match-wins) grabbed a barren owned entity aliased "daily commute to work"
(zero outgoing facts/attrs — the trailing goal PP "to work" got entity-ised), and the
walk from that dead-end anchor is empty → "No relevant facts found". The calc lane was a
RED HERRING: _detect_temporal_calc_intent returns {} for the copula "is" (rule 6 excludes
is/are), so it never fired.

FIX (query-side, capture proven clean): resolve_anchor Rule 2.5 — a first-person
possessive query naming one of the USER's OWN scalar aspects (the attribute HEAD token,
Williams 1981 Right-hand Head Rule, appears in the query) anchors the USER so the
ATTRSCOPE scalar lane surfaces it, BEFORE the n-gram grabs the shadowing fragment. Gated
(possessive + not dative + no apostrophe chain + head-in-query) so a generic modifier
("my daily mood"), a "my X's Y" chain ("my brother's height"), or a dative never trips it.

Two proof layers:
 (1) PURE helper _possessive_self_scalar_anchor — fire / overscope-decline / chain-decline.
 (2) resolve_anchor INTEGRATION (fail-on-old): the gold query anchors the USER via the
     new self_scalar method. On the pre-fix code the n-gram returns the barren fragment
     (method "alias", anchor != user) → FAILS; on the fix it returns the user → PASSES.
"""
import re

import pytest

import src.api.main as m

USER = "00000000-0000-0000-0000-000000000001"
FRAGMENT = "6c1fe6ff-0000-0000-0000-000000000002"  # barren "daily commute to work" entity


# ── (1) PURE HELPER ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("query,attrs,is_dative,expected", [
    # FIRE — first-person possessive naming the user's own scalar aspect (head match).
    ("how long is my daily commute to work", ["daily_commute", "occupation"], False, True),
    ("how long is my daily commute", ["daily_commute"], False, True),      # sibling parity
    ("what is my daily commute", ["daily_commute"], False, True),
    ("what speed is my new internet plan", ["new_internet_plan"], False, True),
    ("what is my occupation", ["occupation"], False, True),
    # DECLINE — a lone generic MODIFIER is never the head (ATTRSCOPE overscope boundary).
    ("what is my daily mood", ["daily_commute"], False, False),
    ("what is my new job title", ["new_internet_plan"], False, False),
    # DECLINE — apostrophe-possessive CHAIN: the aspect may belong to another entity.
    ("what is my brother's height", ["height"], False, False),
    # DECLINE — dative "tell me about …" is not a self-reference.
    ("tell me about my daily commute", ["daily_commute"], True, False),
    # DECLINE — no user scalar head appears in the query.
    ("how old is my mother", ["daily_commute", "age"], False, False),
])
def test_possessive_self_scalar_helper(query, attrs, is_dative, expected):
    assert m._possessive_self_scalar_anchor(query, attrs, is_dative) is expected


def test_attr_head_token_right_hand_head_rule():
    assert m._attr_head_token("daily_commute") == "commute"
    assert m._attr_head_token("new_internet_plan") == "plan"
    assert m._attr_head_token("take_on") == "take"      # functor half dropped


# ── (2) resolve_anchor INTEGRATION — fail-on-old ─────────────────────────────────────
class _FakeCur:
    """Pattern-matching cursor: user scalars for the entity_attributes read; the barren
    fragment for the 'daily commute to work' alias; empty for everything else."""

    def __init__(self, user_attrs):
        self._user_attrs = user_attrs
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = re.sub(r"\s+", " ", sql).strip().lower()
        p = params or ()
        if "from entity_attributes" in s and "entity_id" in s:
            self._rows = [(a,) for a in self._user_attrs]
        elif "from entity_aliases" in s and "alias = %s" in s:
            alias = (p[0] if p else "").lower()
            self._rows = [(FRAGMENT,)] if alias == "daily commute to work" else []
        else:
            # rel_types / entity_taxonomies / synonym probes → nothing
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, user_attrs):
        self._user_attrs = user_attrs

    def cursor(self):
        return _FakeCur(self._user_attrs)


@pytest.fixture(autouse=True)
def _neutralize_side_lanes(monkeypatch):
    # Keep the synonym + possessive-rel lanes inert so the fake DB only has to model the
    # two reads the decision depends on (user scalars + the fragment alias).
    monkeypatch.setattr(m, "resolve_entity_synonym", lambda *a, **k: None)
    monkeypatch.setattr(m, "_resolve_possessive_rel_target", lambda *a, **k: None)


def test_gold_query_anchors_user_not_barren_fragment():
    """FAIL-ON-OLD: pre-fix, Rule 3 n-gram returns the barren 'daily commute to work'
    fragment (method 'alias', anchor != user); the fix anchors the USER via 'self_scalar'
    so the ATTRSCOPE scalar lane can surface daily_commute='45 minutes each way'."""
    db = _FakeDB(["daily_commute", "occupation", "also_known_as", "pref_name"])
    out = {}
    anchor = m.resolve_anchor("how long is my daily commute to work", [], USER, db,
                              resolution_out=out)
    assert anchor == USER, (anchor, out)
    assert out.get("method") == "self_scalar", out


def test_apostrophe_chain_does_not_self_scalar():
    """'my brother's height' must NOT be swallowed by the self-scalar rule (the aspect may
    belong to another entity) even though the user has a 'height' scalar."""
    db = _FakeDB(["height", "daily_commute"])
    out = {}
    m.resolve_anchor("what is my brother's height", [], USER, db, resolution_out=out)
    assert out.get("method") != "self_scalar", out


def test_generic_modifier_does_not_self_scalar():
    """Overscope boundary: 'my daily mood' shares only the generic modifier 'daily' with
    daily_commute (never its head 'commute') → the self-scalar rule declines."""
    db = _FakeDB(["daily_commute"])
    out = {}
    m.resolve_anchor("what is my daily mood", [], USER, db, resolution_out=out)
    assert out.get("method") != "self_scalar", out


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
