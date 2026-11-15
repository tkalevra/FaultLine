"""Anti-fabrication SCOPED-ABSENT-CONCEPT gate (LME cluster: abstention-fabrication).

A query can carry a 1st/2nd-person reference AND/OR a temporal intent yet still be scoped
to a concrete concept the user never mentioned ("what is the name of my hamster", "how long
was I in Korea for"). Those slip past the broad-recall gate to the UNSCOPED fetch-all lane
and DUMP the user's unrelated facts as if they answered — a fabrication. The correct answer
is abstention.

`_query_scoped_to_absent_concept` is the deterministic signal that drives that abstention:
True iff the query names a concrete ENTITY concept absent from the tenant's memory + ontology
(spaCy NOUN/PROPN, grounded against entity_aliases / rel_types / attributes / taxonomies).
Subject-agnostic — no domain / rel / word literals.

These tests exercise the helper directly against a fake cursor so they need no live DB, and
cover MULTIPLE cluster exemplars (Korea + hamster) plus the must-NOT-fire safe cases.
"""
import os
import re
# The spaCy layer is load-bearing for this helper; ensure a model is configured before
# src.api.main (→ src.extraction.linguistics) is imported. fltest sets this too.
os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

import pytest

from src.api.main import _query_scoped_to_absent_concept
from src.extraction.linguistics import linguistics_available


# The tenant's KNOWN ontology/memory for these tests. entity_aliases holds the entities the
# user actually mentioned; the other tables supply the SLOT words a value-query asks about.
_ALIASES = {"japan", "luna", "sarah",
            # MULTI-WORD entity phrases: the user mentioned a "crystal chandelier" and a
            # "vintage armchair", but they were stored as the full noun phrase — the query's
            # bare head noun ("chandelier"/"armchair") NEVER equals these, so the exact
            # `alias = ANY()` check misses them and an exact-only gate wrongly abstains. The
            # word-boundary grounding pass must catch the head noun INSIDE the phrase.
            "stunning crystal chandelier from aunt", "beautiful vintage armchair"}
_REL_ROWS = [("age", "age"), ("pref_name", "preferred name"),
             ("also_known_as", "also known as"), ("favorite_color", "favorite color"),
             ("has_pet", "has pet"), ("lives_in", "lives in")]
_ATTR_ROWS = [("age",), ("height",)]
_TAX_ROWS = [("family",), ("pets",)]


class _FakeCursor:
    def __init__(self):
        self._last = None
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = sql.lower()
        if "from entity_aliases" in s and "~*" in s:
            # WORD-BOUNDARY regex grounding: the LAST bound param is a `\m(a|b|c)\M`
            # alternation. A probe form grounds iff it appears as a WHOLE WORD inside ANY
            # known alias (so the head noun "chandelier" grounds against "stunning crystal
            # chandelier from aunt").
            #
            # DEAD-MOCK FIX (2026-07-31): this read params[1], hard-coding the OLD two-param
            # shape `(user_id, pattern)`. That shape could never run — the per-tenant
            # `entity_aliases` table has NO `user_id` column, so the real statement raised
            # UndefinedColumn in production while this mock happily fed a seam the code
            # could not reach. When the column was dropped from the query the index went
            # stale and `_rx` became "" — an EMPTY regex, which matches everything, so the
            # gate read "grounded" and the absent-concept assertions flipped. Read the LAST
            # param so the fixture tracks the real call either way.
            _rx = params[-1] if params else ""
            try:
                _pat = re.compile(_rx.replace(r"\m", r"\b").replace(r"\M", r"\b"), re.I)
                self._result = [(1,)] if any(_pat.search(a) for a in _ALIASES) else []
            except re.error:
                self._result = []
        elif "from entity_aliases" in s:
            # ANY(%s) alias membership → return a row iff a queried token is a known alias.
            # LAST param (see the dead-mock note above — no `user_id` is bound any more).
            toks = set(params[-1]) if params else set()
            self._result = [(1,)] if (toks & _ALIASES) else []
        elif "from rel_types" in s:
            self._result = list(_REL_ROWS)
        elif "from entity_attributes" in s:
            self._result = list(_ATTR_ROWS)
        elif "from entity_taxonomies" in s:
            self._result = list(_TAX_ROWS)
        else:
            self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result or [])


class _FakeDB:
    def cursor(self):
        return _FakeCursor()


USER = "user-uuid-abc"

pytestmark = pytest.mark.skipif(
    not linguistics_available(),
    reason="spaCy en_core_web_sm required (set SPACY_MODEL); helper no-ops without it",
)


# ── MUST FIRE (abstain) — the cluster fabrication exemplars ─────────────────────────────
@pytest.mark.parametrize("query", [
    "How long was I in Korea for?",              # qid 19b5f2b3_abs — never mentioned Korea
    "What is the name of my hamster?",           # qid 0862e8bf_abs — has a cat, not a hamster
    "What did my dad give me for my birthday?",  # dad/birthday-gift absent-entity shape
])
def test_absent_concept_fires(query):
    assert _query_scoped_to_absent_concept(query, _FakeDB(), USER) is True


# ── MUST NOT FIRE — a named concept the user DID mention grounds ────────────────────────
@pytest.mark.parametrize("query", [
    "How long was I in Japan for?",   # Japan is a known alias → real recall, never blank
    "Tell me about Sarah",            # Sarah grounds
    "What is Luna's age?",            # Luna grounds
])
def test_grounded_concept_does_not_fire(query):
    assert _query_scoped_to_absent_concept(query, _FakeDB(), USER) is False


# ── MUST NOT FIRE — a head noun living INSIDE a MULTI-WORD stored alias grounds ─────────
# The user mentioned "a stunning crystal chandelier" / "a beautiful vintage armchair"; the
# entity is stored as the FULL phrase, so the query's bare head noun ("chandelier" /
# "armchair") never EQUALS the alias. Exact-only grounding wrongly judged it ABSENT and
# ABSTAINED — a false "you did not mention that" that also short-circuited the temporal-calc
# lane (LME 71017276: "how many weeks ago did I ... receive the crystal chandelier"). The
# word-boundary grounding pass must recognize the head noun inside the compound.
@pytest.mark.parametrize("query", [
    "How many weeks ago did I meet up with my aunt and receive the crystal chandelier?",
    "How long ago did I get my armchair?",
    "Tell me about my chandelier",
])
def test_compound_alias_head_noun_grounds(query):
    assert _query_scoped_to_absent_concept(query, _FakeDB(), USER) is False


# ── MUST NOT FIRE — pure self-recall / attribute-slot queries name no absent entity ─────
@pytest.mark.parametrize("query", [
    "Tell me about me",          # no concept noun at all
    "What do you know about me",  # no concept noun
    "Tell me everything",         # no concept noun
    "What is my age",             # 'age' is an ontology slot, not an absent entity
    "How old am I",               # no concept noun
    "Tell me about my family",    # 'family' is a known taxonomy slot
])
def test_self_recall_and_slots_do_not_fire(query):
    assert _query_scoped_to_absent_concept(query, _FakeDB(), USER) is False


# ── FAIL-SAFE — no db / empty query never blanks ────────────────────────────────────────
def test_failsafe_no_db_or_empty():
    assert _query_scoped_to_absent_concept("what is the name of my hamster", None, USER) is False
    assert _query_scoped_to_absent_concept("", _FakeDB(), USER) is False
    assert _query_scoped_to_absent_concept("How long was I in Korea for?", _FakeDB(), "") is False
