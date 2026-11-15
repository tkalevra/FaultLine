"""KNOWLEDGE-UPDATE RECENCY-SCALAR cluster — a STATED CARDINAL COUNT is a SCALAR VALUE, and it
recalls coherently (recency-overwrite + no contradiction).

Root cause (INGEST, gap a): a stated count of a possessed / been-at common-noun set — "I have 600
followers", "I'm at 600 followers", "I've been to 15 countries" — IS a scalar value (the count), but
the SVO/possessive backbone MINTS ``(user, have, followers)`` and DROPS the numeral, and the copula-PP
form is dropped ENTIRELY. So "how many <noun> do I currently have" (qid 1cea1afa Instagram followers ->
600) never lands.

Fix:
  • deriver ``_chain_count_scalar``: a bare-cardinal ``nummod`` on a COMMON NOUN under the two closed-
    class light verbs of possession/copular state (``have`` dobj / ``be`` prep-pobj) → SCALAR
    ``(subject, <noun-NP>, <cardinal>)`` on a STABLE key. The UNIQUE(entity, attribute) row IS the
    recency overwrite ("current" = latest count of the SAME noun wins). The SVO twin is stepped aside
    PER-OBJECT (``_count_suppress``) so "I have 3 cats" no longer emits the "you have a pet that is Cats"
    contradiction, while a SIBLING object ("… and a dog named Rex") survives.
  • query ``_count_scalar_answer``: a "how many <X>" whose distinct-instance walk finds 0 is reconciled
    from the count scalar (HEAD-noun match, deterministic) so recall answers "You have N X" instead of a
    false "I don't have any X on record".

Subject-agnostic, deterministic, no seeding, no rel/type/domain literal.

fail-on-old: before the fix the numeral is dropped (no bare-integer SCALAR edge) and no reconciliation
helper exists.
"""
import datetime
import importlib
import os

import pytest

import src.extraction.linguistics as ling


def _reload(**env):
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return importlib.reload(ling)


def _model_available() -> bool:
    m = _reload(LINGUISTIC_LAYER="true", SPACY_MODEL="en_core_web_sm")
    return m.linguistics_available()


_HAS_MODEL = _model_available()
requires_model = pytest.mark.skipif(not _HAS_MODEL, reason="en_core_web_sm not installed in test env")

_REF = datetime.datetime(2023, 5, 30)


def _facts(text):
    m = _reload(LINGUISTIC_LAYER="true", SPACY_MODEL="en_core_web_sm")
    return list(m.derive_sentence_facts(text, _REF))


def _scalar(facts, attr, value):
    return any(f.subject == "user" and f.rel_type == attr
               and (f.object or "").strip() == value and f.scalar_datatype is not None
               for f in facts)


# ── DERIVER: count scalarized on a STABLE key across update surfaces (recency-overwrite substrate) ─

@requires_model
def test_have_count_scalarized():
    assert _scalar(_facts("I now have 600 followers."), "followers", "600")


@requires_model
def test_copula_at_count_scalarized():
    # the copula-PP form the SVO backbone dropped ENTIRELY (fail-on-old: no "600" anywhere)
    assert _scalar(_facts("I'm now at 600 followers."), "followers", "600")


@requires_model
def test_update_surfaces_share_one_stable_key():
    # RECENCY substrate: 400 -> 600 -> 800 across "have"/"now have"/"Now I have" ALL key on the SAME
    # attribute "followers" (so entity_attributes UNIQUE overwrites to the latest). Also with the
    # "Instagram" modifier the key is stably "instagram_followers".
    assert _scalar(_facts("I have 400 followers."), "followers", "400")
    assert _scalar(_facts("I now have 600 followers."), "followers", "600")
    assert _scalar(_facts("Now I have 800 followers."), "followers", "800")
    assert _scalar(_facts("Now I have 800 Instagram followers."), "instagram_followers", "800")


# ── DERIVER: twin suppression (no contradictory pet/instance edge) + sibling survival ─────────────

@requires_model
def test_count_suppresses_pet_twin():
    facts = _facts("I have 3 cats.")
    # the count scalar exists
    assert _scalar(facts, "cats", "3"), facts
    # and NO relational/instance twin ("(user, have, cats)" / "(cats, instance_of, cat)") that would
    # recall as the contradictory "you have a pet that is Cats"
    assert not any(f.rel_type in ("have", "has_pet", "owns") and (f.object or "") == "cats"
                   for f in facts), facts
    assert not any(f.rel_type in ("instance_of", "is_a") and f.subject == "cats" for f in facts), facts


@requires_model
def test_count_suppression_is_per_object_sibling_survives():
    # "I have 3 cats and a dog named Rex" — the count twin is suppressed PER-OBJECT (not whole-verb),
    # so the SIBLING named object (the dog + its name Rex) MUST survive. The exact edge shape varies by
    # the naming-chain mode (SVO (user, have, dog) vs named-instance (user, owns, rex)/(dog, aka, rex)),
    # so assert mode-robustly that BOTH "dog" and "rex" appear SOMEWHERE (subject or object) — never
    # collateral-dropped by the count suppression.
    facts = _facts("I have 3 cats and a dog named Rex.")
    _surfaces = {(f.subject or "") for f in facts} | {(f.object or "") for f in facts}
    assert "dog" in _surfaces, facts   # the sibling type survived the count suppression
    assert "rex" in _surfaces, facts   # its name survived


# ── FIREWALL: a unit-measure ("5 years") is NOT hijacked by the bare-count lane ───────────────────

@requires_model
def test_unit_measure_not_hijacked():
    facts = _facts("I have 5 years of experience.")
    assert not _scalar(facts, "years", "5"), facts


# ── QUERY: _count_scalar_answer reconciles a "how many X" to the stored count scalar ─────────────

class _FakeCur:
    def __init__(self, rows):
        self._rows = rows
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def execute(self, *a, **k):
        pass
    def fetchall(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows
    def cursor(self):
        return _FakeCur(self._rows)


def _dt(y, mo, d):
    return datetime.datetime(y, mo, d)


def test_count_scalar_answer_head_match():
    import src.api.main as main
    # rows: (attribute, value_int, value_text, updated_at)
    db = _FakeDB([("instagram_followers", 600, None, _dt(2023, 5, 1)),
                  ("cats", 3, None, _dt(2023, 5, 2))])
    # question "instagram followers" -> matches the modifier-rich key
    r = main._count_scalar_answer(db, "u1", "instagram followers")
    assert r and r["count"] == 600 and r["attribute"] == "instagram_followers", r
    # bare-head question "followers" still resolves via the HEAD noun
    r2 = main._count_scalar_answer(db, "u1", "followers")
    assert r2 and r2["count"] == 600, r2
    # "cats"
    r3 = main._count_scalar_answer(db, "u1", "cats")
    assert r3 and r3["count"] == 3, r3


def test_count_scalar_answer_prefers_recency_on_head_tie():
    import src.api.main as main
    # two competing "followers" heads with NO modifier overlap → recency (updated_at) breaks the tie
    db = _FakeDB([("followers", 600, None, _dt(2023, 5, 1)),
                  ("followers", 800, None, _dt(2023, 6, 1))])
    r = main._count_scalar_answer(db, "u1", "followers")
    assert r and r["count"] == 800, r  # latest wins


def test_count_scalar_answer_numeric_text_value():
    import src.api.main as main
    db = _FakeDB([("followers", None, "600", _dt(2023, 5, 1))])
    r = main._count_scalar_answer(db, "u1", "followers")
    assert r and r["count"] == 600, r


def test_count_scalar_answer_none_when_no_numeric_head():
    import src.api.main as main
    db = _FakeDB([("frequent_flyer_status", None, "premier silver", _dt(2023, 5, 1))])
    assert main._count_scalar_answer(db, "u1", "followers") is None
    # non-numeric value → not a count even on a head match
    assert main._count_scalar_answer(db, "u1", "status") is None
