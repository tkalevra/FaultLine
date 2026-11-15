"""TOPIC GROUNDING — a recall must answer the question it was asked, and nothing else.

Every other stage on the recall path RANKS. None of them asks whether the thing being ranked
is on the subject of the question, so a walk anchored on the user hands back the user's facts
whether or not the query's subject is among them. Measured on pre-production seat 11111111,
whose stored content is exactly six facts, `POST /query "What car do I drive?"` returned FIVE
of them — the pet, both cities, the weight and the height — and the MCP rendered all five as
memory. There is no car in that seat. Ranking cannot fix that: the right answer is not further
down the list, it is not in the list at all.

WHAT THESE TESTS PIN, and it is two separable claims:
  1. a question whose answer IS stored comes back with that fact and WITHOUT its neighbours;
  2. a question the seat cannot answer comes back EMPTY, not with the nearest thing to hand.

NO WORD LISTS. The discriminator is WordNet — open-class membership to find the content
tokens, SemCor frequency to pick which one carries the topic, synonym/hypernym overlap to
decide whether a fact is on it. If a future change makes any of these pass by adding a
keyword table, a type table or a tuned cutoff, the test has stopped being worth anything;
that is the failure mode this module is most likely to be "fixed" with.

No DB, no network, no model load — but WordNet must be present, and these SKIP rather than
lie if it is not (a green tick from a run where the lexicon never loaded would assert the
gate works while it was structurally incapable of firing).
"""
import pytest

from src.api import main as M
from src.api import wordnet_ladder as WL

pytestmark = pytest.mark.skipif(
    WL._wn() is None,
    reason="WordNet corpus absent — the gate cannot fire, so passing would be meaningless")


# The live pre-production content of seat 11111111, transcribed from the `facts` table.
SEAT = [
    {"subject": "user", "rel_type": "has_pet", "object": "the cure"},
    {"subject": "user", "rel_type": "lives_in", "object": "Wellington"},
    {"subject": "user", "rel_type": "weight", "object": "70"},
    {"subject": "user", "rel_type": "height", "object": "180"},
    {"subject": "user", "rel_type": "lives_in", "object": "auckland"},
]


def _rels(facts):
    return [(f["rel_type"], f["object"]) for f in facts]


# ── half one: the fact that was asked for, with nothing else attached ────────────────

def test_a_single_stored_answer_comes_back_alone():
    """THE BAR, first half. One fact answers this; four others are merely nearby."""
    out = M._topic_grounded_facts("What pet do I have?", SEAT)
    assert _rels(out) == [("has_pet", "the cure")]


def test_a_scalar_attribute_does_not_drag_its_sibling():
    """height and weight sit side by side on the same anchor and are trivially confusable."""
    assert _rels(M._topic_grounded_facts("What is my height?", SEAT)) == [("height", "180")]
    assert _rels(M._topic_grounded_facts("What is my weight?", SEAT)) == [("weight", "70")]


def test_a_question_with_two_true_answers_keeps_both():
    """Precision is not "return one row". Both cities are genuinely stored `lives_in` facts —
    one current, one former — so both are asked-for and both must survive."""
    assert _rels(M._topic_grounded_facts("Where do I live?", SEAT)) == [
        ("lives_in", "Wellington"), ("lives_in", "auckland")]


def test_a_general_question_reaches_a_specific_memory():
    """The fact's own tokens generalise too, so "animal" finds a stored pet. Without the
    symmetric side the gate would only ever match a question that echoed the memory's wording."""
    assert _rels(M._topic_grounded_facts("What animal do I own?", SEAT)) == [
        ("has_pet", "the cure")]


# ── a direct hit beats an expansion hit, and that is what keeps a broad word usable ──

# The operator's OWN pre-production seat, transcribed from the rows POST /query returned for
# "Who do I work for?" before this tier existed. It answered with all NINE.
WORK_SEAT = [
    {"subject": "user", "rel_type": "participated_in", "object": "linux"},
    {"subject": "user", "rel_type": "participated_in", "object": "trip to sujo funny farm"},
    {"subject": "user", "rel_type": "occupation", "object": "systems analyst iii"},
    {"subject": "user", "rel_type": "works_for", "object": "university"},
    {"subject": "user", "rel_type": "works_for", "object": "services"},
    {"subject": "user", "rel_type": "has_state", "object": "order"},
    {"subject": "user", "rel_type": "has_state", "object": "interested"},
    {"subject": "user", "rel_type": "works_for", "object": "current economic system"},
    {"subject": "user", "rel_type": "located_in", "object": "university"},
]


def test_a_polysemous_topic_does_not_smear_across_the_seat():
    """`work` carries 34 WordNet senses, so its synonym+hypernym expansion admits almost
    anything. Measured live: this returned all nine rows. A DIRECT lemma hit is strictly
    better evidence than an expansion hit, so when any fact has one, only those are kept."""
    out = M._topic_grounded_facts("Who do I work for?", WORK_SEAT)
    assert {r for r, _ in _rels(out)} == {"works_for"}
    assert len(out) == 3


def test_the_direct_tier_does_not_generalise_the_fact():
    """The tier is only meaningful if it is genuinely direct.

    Built first with the symmetric side still on, and it let `participated_in "trip to sujo
    funny farm"` match the topic `work` — WordNet files `farm` under `workplace`, and two
    rungs is a short walk in a lexicon that dense. Pinned because the bug is invisible: the
    tier still looks like it is working, it is just quietly matching everything again."""
    out = M._topic_grounded_facts("Who do I work for?", WORK_SEAT)
    assert not any("farm" in o for _, o in _rels(out))


def test_a_sibling_relation_is_not_dragged_in_by_its_neighbour():
    assert _rels(M._topic_grounded_facts("What is my occupation?", WORK_SEAT)) == [
        ("occupation", "systems analyst iii")]


def test_the_expansion_still_works_when_there_is_no_direct_hit():
    """Direct-first must not disable generalisation — "animal" appears nowhere in the seat,
    so the only way to reach the stored pet is through the hypernym side."""
    assert _rels(M._topic_grounded_facts("What animal do I own?", SEAT)) == [
        ("has_pet", "the cure")]


# ── THE RECALL FLOOR: precision must never be bought by shedding true rows ───────────
#
# ⚠️ READ THIS BEFORE "IMPROVING" ANY NUMBER IN THIS FILE. Returning fewer facts is the
# cheapest possible way to fake precision, and the honest-nothing tests above CANNOT catch it:
# they only guard questions the seat cannot answer, and say nothing about dropping a TRUE fact
# from a question it can. Every tightening below is therefore paired with a floor naming the
# rows that MUST survive. A precision test with no recall floor is half a test.

_WORK_TRUE = ("works_for", "university")        # the actual employer
_LOC_TRUE = [("lives_in", "156 cedar st s"), ("lives_in", "kitchener"), ("lives_in", "on")]

OWNER_LOCATION = [
    {"subject": "user", "rel_type": "lives_in", "object": "on"},
    {"subject": "user", "rel_type": "lives_in", "object": "156 cedar st s"},
    {"subject": "user", "rel_type": "lives_in", "object": "kitchener"},
    # The row the gate drops. Same place ("on" = Ontario) reached by a weaker relation; there
    # is no `located_in` row in `facts` at all on that seat, so this is the Class-C/staged
    # duplicate of the Class-A `lives_in` above, not an independent answer.
    {"subject": "user", "rel_type": "located_in", "object": "on"},
]


def test_the_true_employer_survives_the_tightening():
    """9 -> 3 is only an improvement if the answer is still in the 3."""
    assert _WORK_TRUE in _rels(M._topic_grounded_facts("Who do I work for?", WORK_SEAT))


def test_every_component_of_the_address_survives():
    """4 -> 3 must not have cost a real location. Street, city and province all stay."""
    got = _rels(M._topic_grounded_facts("Where do I live?", OWNER_LOCATION))
    for expected in _LOC_TRUE:
        assert expected in got, f"lost a true location fact: {expected}"


def test_the_dropped_location_row_was_a_duplicate_not_an_answer():
    """Named explicitly, because "it was noise" is an assertion and this is the evidence:
    the province is STILL returned, via the relation that actually answers the question."""
    got = _rels(M._topic_grounded_facts("Where do I live?", OWNER_LOCATION))
    assert ("located_in", "on") not in got
    assert ("lives_in", "on") in got, "the province itself must not have been lost with it"


@pytest.mark.parametrize("dropped,why", [
    (("participated_in", "occurrence: enterprise experience linux"), "an event, not an employer"),
    (("participated_in", "occurrence: farm funny sujo trip"), "a trip, not an employer"),
    (("occupation", "systems analyst iii"), "the job TITLE; the question asks who, not what"),
    (("has_state", "order"), "not about work at all"),
    (("has_state", "interested"), "not about work at all"),
])
def test_each_dropped_work_row_is_named_and_justified(dropped, why):
    """Item by item, so a future reader can disagree with a specific judgement rather than
    having to take a count on trust."""
    seat = WORK_SEAT + [{"subject": "user", "rel_type": dropped[0], "object": dropped[1]}]
    got = _rels(M._topic_grounded_facts("Who do I work for?", seat))
    assert dropped not in got, why
    assert _WORK_TRUE in got, "the floor holds even with the distractor present"


# ── half two: an honest nothing, not the nearest neighbour ───────────────────────────

def test_an_unanswerable_question_returns_nothing():
    """THE BAR, second half — the exact live reproduction. This seat has no car."""
    assert M._topic_grounded_facts("What car do I drive?", SEAT) == []


@pytest.mark.parametrize("query", [
    "What is my favourite food?",
    "What is my phone number?",
    "What is my job?",
])
def test_nothing_stored_means_nothing_returned(query):
    assert M._topic_grounded_facts(query, SEAT) == []


def test_the_light_verb_is_not_the_topic():
    """The regression that makes this gate worthless if it comes back.

    "What car do I HAVE?" shares `have` with the stored `has_pet`. Matching on any content
    word — or on the DEEPEST one, which is how this was first built — grounds the question on
    that shared verb and hands back the pet with full confidence. `car` is rarer than `have`
    in SemCor by two orders of magnitude, and rarity is what identifies the topic."""
    assert M._topic_grounded_facts("What car do I have?", SEAT) == []
    assert M._topic_lemmas("What car do I have?")[0] == "car"


def test_zero_frequency_lemmas_do_not_win_the_topic():
    """SemCor annotates a sample, so an unannotated lemma scores 0 and would win an argmin.

    Live example: `favourite` is unannotated (the American spelling scores 8), so it beat
    `food` as the topic — and WordNet legitimately lists `pet` among its synonyms
    ("teacher's pet"), so a question about food came back holding the user's pet."""
    assert M._topic_lemmas("What is my favourite food?")[0] == "food"


# ── fail direction: uncertain must mean OPEN, never a silent loss of user memory ─────

def test_a_named_ask_is_left_to_the_entity_resolver():
    """"Diane" is not in WordNet and never will be. The gate must not judge that question —
    if it tried, the only lemmas left would be the functors around the name."""
    out = M._topic_grounded_facts("Who is Diane?", SEAT)
    assert out == SEAT, "a named ask must pass through untouched"


def test_no_determinable_topic_passes_everything_through():
    """Nothing in the lexicon to reason from ⇒ no opinion ⇒ hand the walk back untouched.

    (Written first as `"hmm ok"`, which FAILED and was right to: `ok` is in WordNet with a
    real SemCor count, so it IS a determinable topic and grounds nothing. Kept as a note
    because the near-miss is instructive — the branch under test is "no content tokens at
    all", not "the query looked casual".)"""
    assert M._topic_grounded_facts("zzz qqq", SEAT) == SEAT
    assert M._topic_grounded_facts("", SEAT) == SEAT


def test_empty_input_is_returned_unchanged():
    assert M._topic_grounded_facts("What car do I drive?", []) == []


def test_an_unjudgeable_fact_is_kept():
    """A row carrying no readable surface cannot be assessed, so it survives. Dropping what
    you cannot read is the silent-loss direction."""
    blank = [{"subject": "", "rel_type": "", "object": ""}]
    assert M._topic_grounded_facts("What car do I drive?", blank) == blank


def test_wordnet_absent_is_fully_open(monkeypatch):
    """The gate is the last thing between the walk and the caller. If its evidence source is
    missing it must return the walk untouched, never an empty answer."""
    monkeypatch.setattr(WL, "_wn", lambda: None)
    assert M._topic_grounded_facts("What car do I drive?", SEAT) == SEAT


def test_a_raising_lexicon_is_fully_open(monkeypatch):
    def _boom():
        raise RuntimeError("corpus exploded")
    monkeypatch.setattr(WL, "_wn", _boom)
    assert M._topic_grounded_facts("What car do I drive?", SEAT) == SEAT
