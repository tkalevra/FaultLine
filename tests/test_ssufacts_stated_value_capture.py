"""STATED-VALUE user-fact capture cluster (LME ssufacts: 6b168ec8 / c8c3f81d / a06e4cfe).

The common defect: a user STATES a value/count and it DROPS at the copula/possession construction.

  6b168ec8  "I own three bikes"                         GOLD three
  c8c3f81d  "My favorite running shoes are Nike"        GOLD Nike
  a06e4cfe  "My preferred gin-to-vermouth ratio is 3:1" GOLD 3:1

FIX (all deterministic, subject-agnostic, no seeding, no rel/type/domain literal):

  EX1 — the count-scalar lane (deriver ``_chain_count_scalar``) gated ONLY on ``have``/``be`` and
  ONLY on a DIGIT-bearing cardinal, so "I OWN three bikes" dropped the count on TWO counts:
    (1) ``own`` is not ``have`` — extend the counted-object frame to the DB-grown STATIVE-POSSESSION
        cue class (``_possession_verbs()`` — have/own/possess/keep/hold + grown), metadata-driven.
    (2) "three" is a SPELLED-OUT cardinal (no digit char) — accept a nummod NUM whose morphology is
        NumType=Card (excludes ordinals; covers "three" and "3").
    The count owns the construction (twin ``(user, own, bikes)`` + companion ``(bikes, instance_of,
    bike)`` suppressed) so the render is a clean count, NOT the junk "a bike named Bikes".

    Then the QUERY count-answer (``_count_scalar_answer``) rejected the WORD "three" (``_coerce_scalar``
    stores it as value_text, value_int NULL) → a false "I don't have any bikes on record". Recognize a
    spelled-out cardinal (``_count_scalar_value`` closed-class lookup) AND carry the SURFACE so recall
    voices the user's own word — "You have three bikes on record" (gold "three" is a literal token).

  EX2 / EX3 — "my favorite/preferred <X> is/are <Y>" is captured by the EXISTING preference seam
  (``_detect_preference_states`` → (user, favorite_<x>, <value>)); these lock the multi-word ("running
  shoes" → Nike) and ratio ("3:1", colon intact) values so they never regress.

fail-on-old:
  * ``derive_sentence_facts("I own three bikes")`` emitted NO bare-count scalar (own ungated) — and
    even "I have three bikes" dropped the spelled-out cardinal.
  * ``_count_scalar_value`` did not exist; ``_count_scalar_answer`` returned None for a WORD count and
    carried no ``surface``.
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


# ── EX1 DERIVER: count on the POSSESSION backbone + SPELLED-OUT cardinal ──────────────────────────

# ⚠️ AMENDED 2026-11-15 (entity-centric filing, the wound-map doctrine lane): the THING-class
# count rows (bikes / guitars — concrete owned things, the map100 "I have 20 playlists" class)
# no longer file the flat user attr at defaults — the count chain's entity-centric arm stands
# the COLLECTION as an entity ((user, owns, bikes) + (bikes, count, three)), per the doctrine
# (things stand as entities with components as slots; nothing flattens into user attrs). The
# flat-count MECHANISM this pin guards stays green under ENTITY_CENTRIC_FILING=False
# (test_possession_count_scalarized_legacy) and keeps firing at defaults for heads the arm does
# not admit (dogs — an animal count is the pet ladder's territory, unchanged below).
@requires_model
@pytest.mark.parametrize("text,attr,value", [
    ("I keep two dogs.", "dogs", "two"),            # keep (possession) + spelled-out
])
def test_possession_count_scalarized(text, attr, value):
    assert _scalar(_facts(text), attr, value), _facts(text)


@requires_model
@pytest.mark.parametrize("text,attr,value", [
    ("I own three bikes.", "bikes", "three"),      # own (not have) + spelled-out — the live miss
    ("I have three bikes.", "bikes", "three"),      # spelled-out under have (also dropped pre-fix)
    ("I own 3 bikes.", "bikes", "3"),               # own + digit
    ("I possess five guitars.", "guitars", "five"),  # possess + spelled-out
])
def test_possession_count_scalarized_legacy(text, attr, value):
    # ⚠️ flag control rides THIS FILE's own env→reload convention (monkeypatch cannot reach
    # a module _facts() reloads per call).
    m = _reload(LINGUISTIC_LAYER="true", SPACY_MODEL="en_core_web_sm",
                ENTITY_CENTRIC_FILING="false")
    facts = list(m.derive_sentence_facts(text, _REF))
    assert _scalar(facts, attr, value), facts


@requires_model
@pytest.mark.parametrize("text,attr,collection", [
    ("I own three bikes.", "bikes", "bikes"),
    ("I possess five guitars.", "guitars", "guitars"),
])
def test_possession_count_stands_the_collection(text, attr, collection):
    """The entity-centric counterpart: the collection stands with its count slot, and the junk
    twins stay dead (no instance_of the singular, no bare SVO have-twin)."""
    facts = _facts(text)
    assert any(f.subject == "user" and f.rel_type == "owns"
               and (f.object or "") == collection for f in facts), facts
    assert any(f.subject == collection and f.rel_type == "count"
               and (f.object or "").strip() for f in facts), facts
    assert not any(f.subject == collection and f.rel_type == "instance_of" for f in facts), facts
    assert not any((f.object or "") == collection and f.rel_type in ("own", "have", "has_pet")
                   for f in facts), facts


@requires_model
def test_possession_count_suppresses_relational_twin():
    # the count OWNS "I own three bikes" → no (user, own, bikes) and no (bikes, instance_of, bike)
    # that would recall as the junk "a bike named Bikes" / "you own a Bike".
    # ⚠️ Amended 2026-11-15: run under the legacy flag state (the flat-count contract this
    # pin guards; the entity-centric shape is pinned in
    # test_possession_count_stands_the_collection — whose junk-twin refusals hold in BOTH
    # states, the instance_of refusal now enforced by the arm's own obj_tok discipline).
    # Flag control rides this file's env→reload convention (see the legacy pin above).
    m = _reload(LINGUISTIC_LAYER="true", SPACY_MODEL="en_core_web_sm",
                ENTITY_CENTRIC_FILING="false")
    facts = list(m.derive_sentence_facts("I own three bikes.", _REF))
    assert _scalar(facts, "bikes", "three"), facts
    assert not any((f.object or "") == "bikes" and f.rel_type in ("own", "owns", "have", "has_pet")
                   for f in facts), facts
    assert not any(f.subject == "bikes" and f.rel_type in ("instance_of", "is_a") for f in facts), facts


# ── EX1 REGRESSION FIREWALLS: neither fix#17 count-distinct nor named-instance may break ──────────

@requires_model
def test_count_distinct_verb_not_hijacked_as_scalar():
    # "I led 2 projects" is count-DISTINCT (2 project INSTANCES via instance_of), NOT a scalar count.
    # "lead" is not a possession verb → stays out of the count-scalar lane (fix#17 preserved).
    facts = _facts("I led 2 projects.")
    assert not _scalar(facts, "projects", "2"), facts
    assert any(f.subject == "projects" and f.rel_type == "instance_of" for f in facts), facts


@requires_model
def test_named_instance_under_possession_not_a_count():
    # "I have a cat named Luna" — no cardinal; the named-instance chain keeps (cat, also_known_as, luna).
    facts = _facts("I have a cat named Luna.")
    assert any((f.object or "") == "luna" for f in facts), facts
    assert not _scalar(facts, "cat", "luna"), facts


# ── EX1 QUERY: recognize a WORD count + voice the user's SURFACE ("three", not "3") ───────────────

def test_count_scalar_value_recognizes_word_and_keeps_surface():
    import src.api.main as main
    # spelled-out cardinal: count=3 (no false miss) AND surface="three" (voiced verbatim)
    assert main._count_scalar_value(None, "three") == (3, "three")
    # digit paths keep working; junk is not a count
    assert main._count_scalar_value(3, "3") == (3, "3")
    assert main._count_scalar_value(None, "3") == (3, "3")
    assert main._count_scalar_value(None, "premier silver") == (None, None)


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


def test_count_scalar_answer_surfaces_spelled_out_word():
    import src.api.main as main
    dt = datetime.datetime(2023, 5, 1)
    # value_text "three", value_int NULL (exactly how _coerce_scalar stores a spelled-out cardinal)
    db = _FakeDB([("bikes", None, "three", dt)])
    r = main._count_scalar_answer(db, "u1", "bikes")
    assert r and r["count"] == 3 and r["surface"] == "three", r  # fail-on-old: was None / no surface


# ── EX2 SHOES: the FULL derive path — favorite-copula owns the PROPN, naming chain must NOT steal it ─
# (Regression for the LIVE miss the coordinator caught: verifying _detect_preference_states in ISOLATION
#  hid that the full derive_sentence_facts path routed "Nike" to the NAMING chain, dropping the value.)

@requires_model
@pytest.mark.parametrize("text,head", [
    ("My favorite running shoes are Nike.", "nike"),
    ("My favourite brand is Nike.", "nike"),
    ("My favorite restaurant is Nobu.", "nobu"),
])
def test_favorite_copula_propn_is_value_not_named_instance(text, head):
    # (a) the FULL deriver emits a favorite/preference VALUE edge carrying the PROPN
    facts = _facts(text)
    assert any(f.subject == "user" and f.rel_type.startswith(("favorite", "favourite", "prefer"))
               and (f.object or "").strip().lower() == head for f in facts), facts
    # (b) fail-on-old: NO naming/instance_of/owns twin steals the PROPN as a NAME of the possessed
    #     thing — i.e. no (<possessed>, also_known_as|instance_of, <propn>) and no (user, owns, <NP>)
    #     that drops the value. The PROPN appears ONLY as the preference value's object.
    assert not any(f.rel_type in ("also_known_as", "instance_of", "is_a")
                   and (f.object or "").strip().lower() == head for f in facts), facts
    assert not any(f.rel_type in ("owns", "own") and f.subject == "user"
                   and "favorite" in (f.object or "").lower() or "favourite" in (f.object or "").lower()
                   for f in facts), facts


@requires_model
def test_plain_naming_copula_still_names_no_preference_selector():
    # THE distinguisher: "my dog is Rex" has NO preference selector → the naming chain still OWNS it
    # (Rex is the dog's NAME), unchanged by the favorite-copula guard.
    facts = _facts("My dog is Rex.")
    assert any(f.subject == "dog" and f.rel_type == "also_known_as"
               and (f.object or "") == "rex" for f in facts), facts
    assert not any(f.rel_type.startswith("favorite") for f in facts), facts


# ── EX2 / EX3 PREFERENCE lock: "my favorite/preferred <X> is/are <Y>" keeps the VALUE ─────────────

@requires_model
@pytest.mark.parametrize("text,value", [
    ("My favorite running shoes are Nike.", "nike"),        # multi-word X, plural copula, PROPN value
    ("My preferred gin-to-vermouth ratio is 3:1.", "3:1"),  # ratio value — colon NOT mangled
    ("My favorite color is blue.", "blue"),                 # the classic single-word case
])
def test_preference_seam_keeps_stated_value(text, value):
    import src.api.main as main
    edges = main._detect_preference_states(text)
    assert edges, text
    assert edges[0]["subject"] == "user"
    assert edges[0]["object"] == value, edges
    assert edges[0]["fact_provenance"] == "user_stated"
