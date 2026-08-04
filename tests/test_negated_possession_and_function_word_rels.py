"""Regression tests for two MEASURED defects, both reproduced end-to-end before the fix.

BUG 1 — a user's negation of a possession was silently DISCARDED, and worse, counted as a
RE-CONFIRMATION. Measured on a clean local tenant:

    "Aurora is my dog."      -> facts: has_pet user -> aurora   polarity=affirmed
    "Aurora is not my dog."  -> facts: has_pet user -> aurora   polarity=affirmed  (committed:1)

Nothing superseded, nothing archived, and recall still asserted the pet in the CONFIDENT band.
Root cause: ``_chain_possessive`` reads the ``my X`` NP in isolation and never consults the clause
it sits in, so it emitted an identical ``negated=False`` fact for the affirmative and the denial.
The negated copula/state chains already handled negation correctly, and the SVO chain correctly
emits nothing under negation — only the PRESUPPOSED possessive/kinship leg was blind to it.

The fix is scoped by GRAMMAR, not a word list: the possession is marked negated ONLY when the
possessed noun is the DIRECT PREDICATIVE COMPLEMENT of a predicate carrying a ``neg`` dependency.
Everywhere else the possession is a PRESUPPOSITION that PROJECTS THROUGH the negation and stays
asserted (Karttunen 1973, Linguistic Inquiry 4(2)) — "My dog is not sick" still means I have a dog.

BUG 2 — a negation particle was being MINTED as a rel_type. Measured: POSTing an edge with
``rel_type="not"`` created a ``rel_types`` row (source='engine', engine_generated=true) and
committed the Class-A fact ``(aurora, not, dog)`` stored with ``polarity='affirmed'``. Growth now
refuses a candidate whose tokens are ALL closed-class (function) words, per the UD open/closed POS
split — and only ever for a rel_type the system does not already know.

PURE tests — no DB, no network, no GLiNER2, no LLM.

Run: python3 tools/fltest.py --bug FOSSNEG --test tests/test_negated_possession_and_function_word_rels.py
     (tests/ is gitignored -> git add -f)
"""
import os
from datetime import datetime

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction.linguistics import (  # noqa: E402
    derive_sentence_facts,
    linguistics_available,
)

pytestmark = pytest.mark.skipif(
    not linguistics_available(),
    reason="spaCy model unavailable — the deriver no-ops without it (env failure, not a product failure)",
)

REF = datetime(2026, 8, 3)


def _facts(sentence):
    return derive_sentence_facts(sentence, REF, None)


def _find(facts, rel_substr):
    """The emitted fact whose rel_type contains ``rel_substr``, or None."""
    for f in facts:
        if rel_substr in (f.rel_type or ""):
            return f
    return None


# ── BUG 1: the denial must be captured as a NEGATED edge ────────────────────────────────────────

def test_negated_predicative_possession_is_marked_negated():
    """THE REPRO. "Aurora is not my dog" must not emit the affirmative edge.

    Before the fix this returned negated=False — byte-identical to the affirmative sentence — so
    ingest re-committed the affirmed row and the user's correction had zero effect."""
    f = _find(_facts("Aurora is not my dog."), "owns")
    assert f is not None, "the possession edge must still be CAPTURED, only its polarity changes"
    assert f.negated is True


def test_affirmative_possession_stays_affirmed():
    """The control arm: the affirmative must be untouched by the fix."""
    f = _find(_facts("Aurora is my dog."), "owns")
    assert f is not None
    assert f.negated is False


def test_negated_predicative_kinship_is_marked_negated():
    """The kin leg of the same chain: "Sarah is not my sister" denies the tie."""
    f = _find(_facts("Sarah is not my sister."), "sibling_of")
    assert f is not None
    assert f.negated is True


def test_affirmative_kinship_stays_affirmed():
    f = _find(_facts("Sarah is my sister."), "sibling_of")
    assert f is not None
    assert f.negated is False


# ── BUG 1, the other half: presupposition PROJECTION must not be over-negated ───────────────────
# These are the cases a blunt "clause contains a neg -> negate everything" fix would corrupt.

def test_presupposition_projects_when_possessum_is_the_subject():
    """"My dog is not sick" — I STILL HAVE A DOG. Only the state is denied.

    The possessum is the ``nsubj``, outside the scope of the predicate negation."""
    facts = _facts("My dog is not sick.")
    owns = _find(facts, "owns")
    assert owns is not None
    assert owns.negated is False, "possession presupposition must PROJECT through the negation"
    state = _find(facts, "has_state")
    assert state is not None and state.negated is True, "the STATE is what is denied"


def test_presupposition_projects_from_a_nested_complement():
    """"my pets are not part of my family" — I still have pets AND a family.

    ``family`` is a ``pobj`` nested under the "part of" complement, not the complement head, so
    the possession is not what the clause denies."""
    facts = _facts("my pets are not part of my family")
    for f in facts:
        if "owns" in (f.rel_type or ""):
            assert f.negated is False, f"possession {f.object!r} must stay affirmed"


def test_kinship_presupposition_projects_when_role_is_the_subject():
    """"My mother is not happy" — she is still my mother."""
    facts = _facts("My mother is not happy.")
    kin = _find(facts, "parent_of")
    assert kin is not None
    assert kin.negated is False


def test_unnegated_clause_never_marks_negated():
    """Belt-and-suspenders: no ``neg`` anywhere -> nothing is ever flipped."""
    for sentence in ("I have my laptop.", "That is my car.", "My sister is happy."):
        for f in _facts(sentence):
            assert f.negated is False, f"{sentence!r} carries no negation"


# ── BUG 2: growth must refuse a closed-class relation name ──────────────────────────────────────

def test_negation_particle_is_refused_as_a_rel_type():
    """THE REPRO. ``not`` named no predicate yet became a permanent ontology entry."""
    import src.api.main as m
    assert m._rel_type_is_function_word_only("not") is True


def test_other_function_words_are_refused():
    """Any all-closed-class candidate — not a negation word list."""
    import src.api.main as m
    for candidate in ("is", "of", "and", "that", "the", "n't", "neither"):
        assert m._rel_type_is_function_word_only(candidate) is True, candidate


def test_content_bearing_rel_types_are_admitted():
    """A rel_type carrying ANY open-class token is content-bearing and must pass.

    ``has_pet``/``instance_of``/``part_of`` all contain closed-class tokens; the rule is
    ALL-function-words, never any-function-word."""
    import src.api.main as m
    for candidate in ("has_pet", "instance_of", "subclass_of", "part_of", "same_as",
                      "also_known_as", "related_to", "lives_in", "works_for", "born_on",
                      "favorite_colour", "buy_movies", "has_ip"):
        assert m._rel_type_is_function_word_only(candidate) is False, candidate


def test_an_already_known_rel_type_is_never_dropped():
    """SAFETY. The guard constrains GROWTH only — a rel the overlay resolves is never examined.

    ``is_a`` is all-closed-class ('is' AUX + 'a' PRON) AND is source='builtin' in the public seed.
    It must survive, because the filter skips any rel_type ``_rel_meta`` can resolve. If this goes
    red, the known-rel exemption was removed and seeded ontology is now droppable."""
    import src.api.main as m

    class _E:
        def __init__(self, rt):
            self.rel_type, self.subject, self.object = rt, "x", "y"

    known = {"is_a": {"rel_type": "is_a"}}
    original = m._rel_meta
    try:
        m._rel_meta = lambda rt=None: (known.get(rt) if rt is not None else known)
        kept = m._drop_function_word_rel_edges([_E("is_a"), _E("not")])
    finally:
        m._rel_meta = original
    kept_rels = [e.rel_type for e in kept]
    assert "is_a" in kept_rels, "a KNOWN all-function-word rel must be exempt"
    assert "not" not in kept_rels, "an UNKNOWN all-function-word rel must be refused"


def test_guard_is_disableable_and_fails_open():
    """Flag OFF -> byte-identical legacy admission."""
    import src.api.main as m
    original = m.REL_TYPE_FUNCTION_WORD_GUARD
    try:
        m.REL_TYPE_FUNCTION_WORD_GUARD = False
        m._rel_type_is_function_word_only.cache_clear()
        assert m._rel_type_is_function_word_only("not") is False
    finally:
        m.REL_TYPE_FUNCTION_WORD_GUARD = original
        m._rel_type_is_function_word_only.cache_clear()


# ── RENDER: a negated possession must read as English ───────────────────────────────────────────

def test_lexical_verb_negation_uses_do_support():
    """The fix above made this path reachable and it rendered "You HAVE NOT a pet".

    English negates a LEXICAL verb with do-support (Huddleston & Pullum, CGEL 2002 ch.3 §1.3)."""
    import src.api.main as m
    assert m._negate_prose("You have a pet that is Dog") == "You do not have a pet that is Dog"
    assert m._negate_prose("Diane lives in Toronto") == "Diane does not live in Toronto"
    assert m._negate_prose("You had a pet") == "You did not have a pet"


def test_auxiliary_negation_is_unchanged():
    """A genuine auxiliary/copula still takes "not" directly — the legacy path, byte-identical."""
    import src.api.main as m
    assert m._negate_prose("The gps system is functioning") == "The gps system is not functioning"
    assert m._negate_prose("You have eaten lunch") == "You have not eaten lunch"
    assert m._negate_prose("You will attend the meeting") == "You will not attend the meeting"


def test_already_negated_prose_is_not_double_negated():
    import src.api.main as m
    assert m._negate_prose("You are not sick") == "You are not sick"
