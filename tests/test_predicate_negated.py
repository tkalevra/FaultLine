"""Deriver-level pins for lane N's negation predicate — `_predicate_negated`.

WHY THIS FILE EXISTS. The perfect-flow negation/cessative lane shipped ~535 lines and,
unlike its two sibling lanes, arrived with no dedicated deriver-level test — the clearest
single coverage gap in the integrated change (recorded in the gauntlet ledger). Its load-
bearing new symbol is `_predicate_negated(tok)`: the grammar-only replacement for the old
single-hop ``any(c.dep_ == "neg" for c in tok.children)`` idiom, which now also admits a
GRANDCHILD ``neg`` under a comparative "no longer"/"no more" advmod or an aux/auxpass — the
hop that turns a cessation ("the group no longer meets") into a negated predicate.

Every dependency shape asserted here was VERIFIED empirically against en_core_web_sm before
the assertion was written (asserting a guessed parse is the trap this repo warns about); the
probe output is reproduced inline beside each case.

SCOPE HONESTY. This pins the PREDICATE PREDICATE (does this token carry sentential negation),
which is the unit lane N changed. It does NOT pin the end-to-end row outcome — the honest
partial that a MULTIWORD cancellation ("give up", "call off") is headed by spaCy on the light
verb and currently stores nothing rather than a negated capture is a deriver+DB behavior and
belongs in an integration test, not here. See the gauntlet ledger VERIFY-005 / INTEGRATION-004.
"""

import importlib
import os

import pytest

import src.extraction.linguistics as ling

# Skip cleanly (never FAIL) when the spaCy model isn't loadable — mirror the gate the
# production resolver uses, not a weaker "is spacy importable" proxy.
_MODEL = os.environ.get("SPACY_MODEL", "en_core_web_sm")


@pytest.fixture(scope="module")
def nlp():
    spacy = pytest.importorskip("spacy")
    try:
        return spacy.load(_MODEL)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"spaCy model {_MODEL!r} not loadable: {e}")


def _root(doc):
    for t in doc:
        if t.dep_ == "ROOT":
            return t
    return doc[0]


# ─────────────────────────── DIRECT neg (the pre-lane baseline) ──────────────────────────

def test_direct_neg_particle_is_negated(nlp):
    # "I do not run." -> ROOT run/VERB, child ('not','neg') — direct hop.
    assert ling._predicate_negated(_root(nlp("I do not run."))) is True


def test_direct_neg_on_passive_auxpass_is_negated(nlp):
    # "The meeting was not cancelled." -> ROOT cancelled/VERB, direct child ('not','neg').
    # (The predicate carries negation; that "not cancelled" is semantically affirmative of the
    # meeting is a downstream concern — the predicate-negation signal itself is correct.)
    assert ling._predicate_negated(_root(nlp("The meeting was not cancelled."))) is True


def test_plain_affirmative_is_not_negated(nlp):
    # "I run every day." -> ROOT run/VERB, no neg anywhere.
    assert ling._predicate_negated(_root(nlp("I run every day."))) is False


# ─────────────────────── GRANDCHILD neg — the cessation hop lane N added ──────────────────

def test_no_longer_verbal_cessation_is_negated(nlp):
    # "The group no longer meets on Tuesdays." -> ROOT meets/VERB;
    #   child ('longer','advmod',ADV,Degree=Cmp) -> grandchild ('no','neg'). THE cessation case.
    assert ling._predicate_negated(_root(nlp("The group no longer meets on Tuesdays."))) is True


def test_no_longer_copular_cessation_is_negated(nlp):
    # "She is no longer my sister." -> ROOT is/AUX; child ('longer','advmod',Cmp) -> ('no','neg').
    assert ling._predicate_negated(_root(nlp("She is no longer my sister."))) is True


# ─────────────────────── FLAG GATE: grandchild hop is opt-outable ─────────────────────────

def test_grandchild_hop_is_gated_by_the_flag(nlp, monkeypatch):
    """With SPINE_GRANDCHILD_NEG off, the grandchild "no longer" hop stops firing, but a
    DIRECT neg still reports True — the flag narrows to legacy single-hop, never disables."""
    root_cessation = _root(nlp("The group no longer meets on Tuesdays."))
    root_direct = _root(nlp("I do not run."))

    monkeypatch.setattr(ling, "SPINE_GRANDCHILD_NEG", False)
    assert ling._predicate_negated(root_cessation) is False  # grandchild hop withdrawn
    assert ling._predicate_negated(root_direct) is True      # direct hop unaffected


# ─────────────────────── FAIL-SAFE: never crash the linguistic layer ──────────────────────

def test_fail_safe_returns_false_on_a_raising_token():
    """The function is wrapped so a malformed token can never crash the spine — it returns
    False (fail toward not-negated / normal ingest), the safe default."""
    class _Boom:
        @property
        def children(self):
            raise RuntimeError("synthetic parse failure")

    assert ling._predicate_negated(_Boom()) is False
