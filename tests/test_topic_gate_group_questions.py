"""The last-gate topic filter must not answer "tell me about my family?" with nothing.

REGRESSION UNDER TEST — introduced 2026-08-17 by `69f1def6` ("fix(recall): a degraded harvest
writes nothing, and a recall answers only what was asked"), which added `_topic_grounded_facts`
as the last gate before the /query response. Two independent defects in it, both MEASURED on the
running stack against a seat whose stored, user_stated memory is a family:

  seat aa9edf96, facts: (chen, child_of, user) user_stated
                        (son,  child_of, user) user_stated
                        (chen, instance_of, son) user_stated

  BEFORE  query.phase5.deduplication          after=9  before=18
          query.axis_projection.applied       after=9  axis=membership
          query.phase5.topic_grounding_filtered  before=9  after=0   <-- all nine dropped
          -> recall_memory returned "I don't have any information about that in your memory"

DEFECT 1 — A PREPOSITION WINS THE TOPIC ARGMIN.
`_topic_lemmas` claims WordNet membership IS the open-class test ("what/where/who fall out for
free by not being in the lexicon"). English prepositions break that: WordNet lists `about` with
an adverb + satellite-adjective reading, and SemCor almost never tags it, so it wins the
rarest-lemma argmin outright. Measured from the lexicon itself:

    "tell me about my family?"  ->  about 79  <  family 135  <  tell 560   -> topic = ABOUT
    "who is in my family?"      ->  in    22  <  family 135  <  be  16667  -> topic = IN

The expansion of adverbial `about` is {almost, approximately, around, roughly, ...}; nothing a
person ever stated overlaps it, so the gate "positively knew" the topic and dropped everything —
on the single most common recall phrasing there is.

DEFECT 2 — THE GATE CANNOT REACH A GROUP'S MEMBERS, EVER.
Even with the topic correctly resolved to `family`, all nine facts still drop: WordNet files
`son` under offspring -> relative -> person and `family` under social group, so `family`'s
77-lemma expansion contains neither `child` nor `son`, and the symmetric side does not reach
`family` from either. The relation a group question needs is MERONYMY; this gate walks only
synonymy and bounded hypernymy. No expansion budget fixes it.

THE FIX for defect 2 is not to widen the lexicon — it is to stop re-judging a walk the ONTOLOGY
already grounded. When `determine_path` bound the query's own grouping word to a taxonomy scope,
the walk was projected through that taxonomy's member rels and every surviving row is on topic by
construction. `_topic_gate_should_run` encodes exactly that, and nothing more.

PRECISION IS PART OF THE CONTRACT. The gate exists because "What car do I drive?" on a seat with
no car returned the pet, both cities, the weight and the height. That query resolves NO taxonomy
(measured: `taxonomy_groups=[] scope_active=False`), so the gate still runs for it. The tests
below pin both directions.
"""

import pytest

from src.api.main import _topic_gate_should_run


class _Path:
    """The two `QueryPath` attributes the gate decision reads. Nothing else is consulted."""

    def __init__(self, scope_active=False, taxonomy_groups=None):
        self.scope_active = scope_active
        self.taxonomy_groups = list(taxonomy_groups or [])


# ── the regression: a taxonomy-scoped walk must not be re-judged lexically ────────────────

def test_taxonomy_scoped_walk_skips_the_lexical_gate():
    """"tell me about my family?" — measured determine_path output on the live stack.

    axis=membership, taxonomy_groups=['family'], scope_active=True,
    relationship_rels=['spouse','sibling_of','child_of','parent_of'].

    The walk was projected THROUGH the family rels, so its rows are the answer. Before the fix
    the lexical gate ran anyway and took 9 -> 0.
    """
    path = _Path(scope_active=True, taxonomy_groups=["family"])
    assert _topic_gate_should_run(path) is False


def test_a_second_taxonomy_scoped_grouping_also_skips():
    """Subject-agnostic: the skip keys on "a taxonomy resolved", never on a taxonomy NAME."""
    path = _Path(scope_active=True, taxonomy_groups=["pets"])
    assert _topic_gate_should_run(path) is False


# ── precision: the defect 69f1def6 fixed must stay fixed ─────────────────────────────────

def test_unscoped_query_still_runs_the_gate():
    """"What car do I drive?" — measured: taxonomy_groups=[], scope_active=False.

    This is the query the gate was built for. It must still run, and still return nothing on a
    seat with no car. If this goes red the original defect is reopened.
    """
    path = _Path(scope_active=False, taxonomy_groups=[])
    assert _topic_gate_should_run(path) is True


def test_scope_without_a_taxonomy_still_runs_the_gate():
    """"what is my job?" — measured: scope_active=True (scalar `occupation`) but NO taxonomy.

    A scalar/rel scope is not the ontology answering "what is this question about"; only a
    resolved taxonomy grouping is. The gate keeps running here.
    """
    path = _Path(scope_active=True, taxonomy_groups=[])
    assert _topic_gate_should_run(path) is True


def test_taxonomy_without_active_scope_still_runs_the_gate():
    """A taxonomy that did NOT arm the scope grounded nothing — no reason to defer to it."""
    path = _Path(scope_active=False, taxonomy_groups=["family"])
    assert _topic_gate_should_run(path) is True


def test_fail_direction_is_open_on_a_path_missing_both_attributes():
    """Unknown shape ⇒ run the gate (unchanged behaviour), never a silent blanket skip."""

    class _Bare:
        pass

    assert _topic_gate_should_run(_Bare()) is True


# ── defect 1: the topic of a "tell me about my X" question is X, not "about" ─────────────

def _lexicon_available() -> bool:
    try:
        from src.api import wordnet_ladder as _wl
        if _wl._wn() is None:
            return False
        from src.extraction.linguistics import linguistics_available
        return bool(linguistics_available())
    except Exception:
        return False


requires_lexicon = pytest.mark.skipif(
    not _lexicon_available(),
    reason="needs the WordNet corpus AND the spaCy model (SPACY_MODEL=en_core_web_sm)",
)


@requires_lexicon
@pytest.mark.parametrize(
    "query,expected",
    [
        # The gauntlet question. `about` scores 79 in SemCor, `family` 135 — the preposition
        # won the rarest-wins argmin and the answer came back empty.
        ("tell me about my family?", "family"),
        # `in` scores 22 and beat `family` the same way.
        ("who is in my family?", "family"),
        # Untouched controls: these already picked the right topic and must keep it.
        ("What car do I drive?", "car"),
        ("how much do I weigh?", "weigh"),
        ("where do I live?", "live"),
    ],
)
def test_a_preposition_never_carries_the_topic(query, expected):
    from src.api.main import _topic_lemmas

    lemmas = _topic_lemmas(query)
    assert lemmas, f"no topic resolved for {query!r}"
    assert lemmas[0] == expected, (
        f"{query!r} resolved topic {lemmas[0]!r}; the whole ranked list was {lemmas}. "
        "A word doing grammatical work in THIS sentence must not compete for the topic."
    )


@requires_lexicon
def test_the_lexical_gate_cannot_reach_a_group_s_members_even_with_the_right_topic():
    """CHARACTERISATION — this is WHY `_topic_gate_should_run` exists, not a bug to fix here.

    With the topic correctly resolved to `family` (defect 1 already repaired), the gate still
    cannot judge a group question. MEASURED: of three real family facts it keeps exactly ONE,
    and it keeps that one for a reason that has nothing to do with family —

        'you have a Son named Chen'   KEPT   — the rendered verb "named" lemmatises to `name`,
                                               and WordNet's `name` carries the surname sense,
                                               whose closure reaches `family`
        'You are the parent of Son'   DROPPED
        'Chen is your son'            DROPPED

    So the survivor is an artefact of how the prose happened to be worded, and the two facts
    that state the relation outright are the ones thrown away. `family` and `son`/`child` are
    related by MERONYMY; this gate walks synonymy and bounded hypernymy, so on a group question
    its output is noise rather than grounding. That is what the ontology-scoped skip replaces.

    If this ever changes, the gate's reach changed — RE-MEASURE before relaxing the skip.
    """
    from src.api.main import _topic_grounded_facts

    facts = [
        {"subject": "chen", "object": "user", "rel_type": "child_of",
         "definition": "you have a Son named Chen"},
        {"subject": "son", "object": "user", "rel_type": "child_of",
         "definition": "You are the parent of Son"},
        {"subject": "chen", "object": "son", "rel_type": "instance_of",
         "definition": "Chen is your son"},
    ]
    kept = _topic_grounded_facts("tell me about my family?", facts)
    assert [f["definition"] for f in kept] == ["you have a Son named Chen"], (
        "the lexical gate's reach on a group question changed — re-measure before relying on it"
    )


# ─────────────────────────────────────────────────────────────────────────────
# REGRESSION — every counterexample a fresh-context critic used to fail round 1.
# These drive the REAL topic resolver against the REAL predicate together; a stub
# path with hand-set attributes cannot catch any of them, which is exactly how
# round 1 shipped green while returning the dog for "who is my ex-wife?".
# ─────────────────────────────────────────────────────────────────────────────

import pytest as _pytest
from src.api.main import _topic_lemmas as _tl, _topic_gate_should_run as _tgsr


class _ScopedPath:
    def __init__(self, groups):
        self.taxonomy_groups = list(groups)
        self.scope_active = bool(groups)


def _needs_models():
    """Gate on what the PRODUCTION CODE checks, not on what happens to be installed.

    An earlier version asked "can I spacy.load a model?" — true on any dev box — while
    the resolver itself gates on ``linguistics_available()``, which is false unless
    SPACY_MODEL is set. So on a runner with the model installed but the var unset these
    tests FAILED instead of skipping, which is the worst of both: a red that means
    "misconfigured", not "broken".
    """
    try:
        from nltk.corpus import wordnet as wn
        wn.synsets("family")
        from src.extraction.linguistics import linguistics_available
        return not linguistics_available()
    except Exception:
        return True


_skip = _pytest.mark.skipif(_needs_models(), reason="needs WordNet + the spaCy model")


@_skip
@_pytest.mark.parametrize("query,expected_head", [
    ("tell me about my family?", "family"),   # 'about' must not win the argmin
    ("who is in my family?", "family"),       # nor 'in'
    ("who is my ex-wife?", "wife"),           # hyphen compound must survive tokenisation
    ("tell me about my ex-wife", "wife"),     # and must beat the light verb 'tell'
    ("where do I currently live?", "live"),   # sentence adverbs must not be the topic
    ("where exactly do I live?", "live"),
    ("what car do I actually drive?", "car"),
    ("what do I still own?", "own"),
])
def test_the_topic_is_a_content_word_not_a_functor(query, expected_head):
    chain = _tl(query)
    assert chain, f"{query!r} resolved no topic at all"
    assert chain[0] == expected_head, f"{query!r} -> {chain[:3]}"


@_skip
def test_a_grouping_word_does_not_license_skipping_a_question_about_something_else():
    """THE round-1 regression: the skip keyed on the PRESENCE of a scope, so a seat with
    family facts and no car answered "what car does my family drive?" with the children."""
    for query in ("what car does my family drive?", "what is my family income?"):
        chain = _tl(query)
        assert _tgsr(_ScopedPath(["family"]), chain) is True, (
            f"{query!r} (topic {chain[:3]}) must still be judged by the gate — the taxonomy "
            f"is the QUALIFIER on the question, not the question")


@_skip
def test_the_skip_still_fires_when_the_taxonomy_is_the_question():
    for query in ("tell me about my family?", "who is in my family?"):
        assert _tgsr(_ScopedPath(["family"]), _tl(query)) is False, query


@_skip
def test_an_unscoped_query_is_always_judged_by_the_gate():
    assert _tgsr(_ScopedPath([]), _tl("what car do I drive?")) is True
