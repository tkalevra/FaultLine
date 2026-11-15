"""TEMPORAL-INTENT GRANULARITY (gauntlet temporal-intent-granularity, TG1-TG6).

THE WOUND: main.py::_detect_temporal_query_intent decided intent by regex over
the WHOLE lowercased query, so a temporal cue in ANY sentence — a trailing "My
old bike was red." / "I previously fixed the fence." — set history /
state_phase / filter_status for the ENTIRE ask and routed an unrelated
present-tense question into the supersession lane (fetch_superseded_facts),
serving a RETIRED value for the thing actually asked about.

THE FIX (cue REACH, never key semantics): _temporal_ask_scoped_text parses the
marker-stripped query (same spaCy the engine already runs for
_old_is_history_cue) and returns only the sentence(s) that carry the ASK —
WH asks dominate polar checks; verbless FRAGMENTS join the ask; tensed
declarative asides do not. Undecidable granularity → None → cues evaluated on
the WHOLE query = today's behaviour (TG6: never silent current-state erasure
of a genuine history ask).

These are UNIT lane tests over the real detector (real spaCy parse, no mocks
inside the scope decision). Row-level / end-to-end truth for every bar lives
in the pre-prod battery (DEV/review/gauntlet/temporal-intent-granularity/).
Run through tools/fltest.py on the rig (SPACY_MODEL=en_core_web_sm).
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.extraction.linguistics import linguistics_available  # noqa: E402
from src.api.main import (  # noqa: E402
    _detect_temporal_query_intent,
    _temporal_ask_scoped_text,
)

# A skip on the rig (spaCy baked, SPACY_MODEL set) means the battery is NOT
# evidence — a skip here is a red flag, not a pass.
_NEEDS_PARSE = pytest.mark.skipif(
    not linguistics_available(), reason="spaCy parse unavailable — granularity lane untestable")

ASK = "What is the weight of my zorble?"

# TG1 battery: every remaining cue phrase, as a sentence ABOUT SOMETHING ELSE,
# both trailing and leading. All asides are tensed declaratives or a polar
# check — none is the value ask.
ASIDES = {
    "used to": "I used to have a heavier one.",
    "previously": "I previously fixed the fence.",
    "formerly": "The shed was formerly a garage.",
    "no longer": "I no longer keep tools in the shed.",
    "back when": "Back when we lived downtown, things were simpler.",
    "did i ever": "Did I ever tell you about the shed?",
}


def _composite(cue, position):
    aside = ASIDES[cue]
    return f"{ASK} {aside}" if position == "trailing" else f"{aside} {ASK}"


# ── TG1: an unrelated-sentence cue does not hijack the ask ───────────────────

@_NEEDS_PARSE
@pytest.mark.parametrize("position", ["trailing", "leading"])
@pytest.mark.parametrize("cue", sorted(ASIDES))
def test_tg1_unrelated_sentence_cue_does_not_hijack(cue, position):
    intent = _detect_temporal_query_intent(_composite(cue, position))
    assert "history" not in intent, (cue, position, intent)
    assert intent.get("state_phase") != "past", (cue, position, intent)
    assert intent.get("filter_status") != "past", (cue, position, intent)


@_NEEDS_PARSE
@pytest.mark.parametrize("cue", sorted(ASIDES))
def test_tg1_scope_actually_located_the_ask(cue):
    """Non-vacuousness at the unit layer: the scoping DECIDED (returned ask
    text, not None) and the ask text excludes the aside's cue sentence."""
    scoped = _temporal_ask_scoped_text(_composite(cue, "trailing"))
    assert scoped is not None
    assert "zorble" in scoped
    assert cue not in scoped, (cue, scoped)


# ── TG2: genuine past-state asks still reach the supersession lane ───────────

@_NEEDS_PARSE
@pytest.mark.parametrize("query,keys", [
    ("Where did I used to live?", {"history", "state_phase"}),
    ("What was my old job?", {"history"}),
    ("Did I ever mention the quandle?", {"history", "state_phase"}),
])
def test_tg2_genuine_past_state_asks_still_fire(query, keys):
    intent = _detect_temporal_query_intent(query)
    for k in keys:
        assert k in intent, (query, k, intent)
    if "state_phase" in keys:
        assert intent["state_phase"] == "past"


# ── TG3: same-clause / same-sentence cues still fire ─────────────────────────

@_NEEDS_PARSE
def test_tg3_cue_inside_the_ask_sentence_still_fires():
    # one sentence: the cue and the ask are inseparable → whole-query (today).
    assert "history" in _detect_temporal_query_intent(
        "What was my zorble's weight previously?")
    # fragment + ask: the topicalized label carries its cue INTO the ask
    assert "history" in _detect_temporal_query_intent(
        "My previously-corrected weight for zorble — what is it?")


@_NEEDS_PARSE
def test_tg3_same_sentence_boundary_behaviour_unchanged():
    """The TG3 example is a SINGLE sentence: today it sets boundary=before and
    no history — granularity must not change either fact."""
    intent = _detect_temporal_query_intent(
        "What was the weight of my zorble before I corrected it?")
    assert "history" not in intent
    assert intent.get("boundary") == "before"


# ── TG4: state_phase and filter_status get the same treatment ────────────────

@_NEEDS_PARSE
def test_tg4_window_in_aside_no_longer_filters():
    intent = _detect_temporal_query_intent(
        "What is my zorble's weight? I fixed the fence last week.")
    assert "filter_status" not in intent, intent
    intent = _detect_temporal_query_intent(
        "Where do I live? I previously lived in Oslo.")
    assert intent.get("state_phase") != "past", intent
    assert "history" not in intent, intent


@_NEEDS_PARSE
def test_tg4_same_clause_window_still_filters():
    assert _detect_temporal_query_intent(
        "What did I fix last week?").get("filter_status") == "past"
    assert _detect_temporal_query_intent(
        "Where did I previously live?").get("state_phase") == "past"


# ── TG5: regression pins the detector must keep holding ─────────────────────

@_NEEDS_PARSE
def test_tg5_present_tense_age_still_not_history():
    assert "history" not in _detect_temporal_query_intent("How old is my plovex?")


# ── ROUND 2 (critic GAPS): quoted/third-party cue in a declarative clause,
# ask in a complement clause, ONE sentence — the sentence half alone cannot
# separate them; the CLAUSE half must. All shapes are single-sentence unless
# noted, so a sentence-count-only fix is provably insufficient.
@_NEEDS_PARSE
@pytest.mark.parametrize("query", [
    "He said 'no longer' — what is the weight of my zorble?",
    "He said 'no longer'. What is my weight?",              # 2 sentences: control
    "My neighbor says I used to have a heavier one; what does my zorble weigh?",
    "He told me I no longer keep tools, so what is the weight of my zorble?",
    "She mentioned that I formerly owned one — what is my zorble's weight?",
])
def test_r2_quoted_third_party_cue_does_not_hijack(query):
    intent = _detect_temporal_query_intent(query)
    assert "history" not in intent, (query, intent)
    assert intent.get("state_phase") != "past", (query, intent)


@_NEEDS_PARSE
def test_r2_ask_complement_keeps_cue():
    """A cue inside a COMPLEMENT of the ask verb is part of the ask itself."""
    assert "history" in _detect_temporal_query_intent(
        "What did I say I no longer do?")


@_NEEDS_PARSE
def test_r2_tg2_catenative_cues_survive_clause_scope():
    """The clause walk must not eat the cue verb in genuine past-state asks
    (catenative parses: the cue verb IS the ask's finite root)."""
    for q in ("Where did I used to live?",
              "What did my wobbly farxen use to weigh?"):
        assert "history" in _detect_temporal_query_intent(q), q


@_NEEDS_PARSE
@pytest.mark.parametrize("query", [
    "He said 'no longer' — what is the weight of my zorble?",
    "My neighbor says I used to have a heavier one; what does my zorble weigh?",
])
def test_r2_revert_red_whole_text_reach_restores_hijack(query):
    with patch("src.api.main._temporal_ask_scoped_text",
               lambda q: (q or "").lower()):
        assert _detect_temporal_query_intent(query).get("history") is True, query


# ── ROUND 3 (critic GAPS): the blanket adjunct boundary had three live failure
# modes — TG2 erasure (back-when windows), a false declared residual
# (coordinated second asks), and a revived hijack (ask-as-conj of a cue-bearing
# declarative). New rule in main.py::_clause_token_covers_ask: (a) a token whose
# chain reaches a finite INTERROGATIVE clause is covered whatever attaches above
# it; (b) an adjunct governed by an ask root is the ask's own window, not a
# boundary; (c) only a declarative adjunct NOT governed by an ask excludes.
@_NEEDS_PARSE
@pytest.mark.parametrize("query,expect", [
    # F1 — the ask's own temporal window: cue KEEPS deciding (TG2 protection)
    ("What did my wobbly farxen weigh back when we lived downtown?", "history"),
    ("What is my zorble's reading when I checked it last year?", "filter_past"),
    ("What did my zorble read after I previously recalibrated it?", "history"),
    # r2 FLIP, declared: a since-advcl of the ask root is the ask's own window
    ("What is my zorble's weight, since I previously corrected it?", "history"),
])
def test_r3_ask_window_cues_keep_deciding(query, expect):
    intent = _detect_temporal_query_intent(query)
    if expect == "history":
        assert intent.get("history") is True, (query, intent)
        # state_phase only where a state_phase CUE phrase is present — "back
        # when" is a history-cue but not in the state_phase cue set (frozen).
        if "previously" in query.lower() or "use to" in query.lower():
            assert intent.get("state_phase") == "past", (query, intent)
    else:
        assert intent.get("filter_status") == "past", (query, intent)


@_NEEDS_PARSE
def test_r3_coordinated_second_ask_keeps_cue():
    """F2 — the coordinated history ask is covered by its OWN ask clause;
    the round-1 declared residual (two-WH keeps whole-query behaviour) was
    FALSE for the coordinated case."""
    intent = _detect_temporal_query_intent(
        "What's my mottled farxen's weight and what did my wobbly farxen use to weigh?")
    assert intent.get("history") is True, intent
    assert intent.get("state_phase") == "past", intent


@_NEEDS_PARSE
@pytest.mark.parametrize("query", [
    "The tag that reads 'formerly 61 pounds' is wrong, so what is the weight of the wobbly farxen?",
    "She asked whether I formerly owned one, and what is my weight?",
])
def test_r3_ask_conj_of_cue_declarative_does_not_hijack(query):
    """F3 — the ask claims its own conj/parataxis clause; the cue in the
    declarative frame never reaches an ask root."""
    intent = _detect_temporal_query_intent(query)
    assert "history" not in intent, (query, intent)
    assert intent.get("state_phase") != "past", (query, intent)


@_NEEDS_PARSE
@pytest.mark.parametrize("query", [
    "The tag that reads 'formerly 61 pounds' is wrong, so what is the weight of the wobbly farxen?",
    "She asked whether I formerly owned one, and what is my weight?",
])
def test_r3_revert_red_whole_text_reach_restores_hijack(query):
    with patch("src.api.main._temporal_ask_scoped_text",
               lambda q: (q or "").lower()):
        assert _detect_temporal_query_intent(query).get("history") is True, query


@_NEEDS_PARSE
def test_r3_backwhen_revert_red_none_scope_is_todays_behavior():
    """The F1 family under an UNDECIDABLE scope keeps today's behaviour
    (history fires) — the TG6 direction, from the ask-window side."""
    with patch("src.api.main._temporal_ask_scoped_text", return_value=None):
        assert _detect_temporal_query_intent(
            "What did my wobbly farxen weigh back when we lived downtown?"
        ).get("history") is True

# ── ROUND 4 (critic GAPS): the detection/coverage ASYMMETRY on relcl/acl —
# detection excluded relatives from ask marking while coverage treated them as
# transparent complements, so a cue inside an NP-relative climbed to the ask
# root and erased the current value. Now symmetric: relatives (relcl/acl +
# csubj free-relative, an NP-function clause) are NEVER an ask and NEVER
# transparent (rule (d) in main.py::_clause_token_covers_ask).
@_NEEDS_PARSE
@pytest.mark.parametrize("query", [
    "What is the weight of the mottled farxen that I tracked?",          # control: no cue
    "What is the weight of the mottled farxen that I previously tracked?",  # the critic minimal pair
    "What is the weight that I no longer track?",                         # relative on the ASKED NOUN (decision declared)
    "What I formerly weighed is private, but what is it now?",            # free-relative aside (csubj)
    "What is the weight of the mottled farxen that I tracked and that I previously reported?",  # coordinated relative
])
def test_r4_relative_cue_does_not_hijack(query):
    intent = _detect_temporal_query_intent(query)
    assert "history" not in intent, (query, intent)
    assert intent.get("state_phase") != "past", (query, intent)
    assert intent.get("filter_status") != "past", (query, intent)


@_NEEDS_PARSE
@pytest.mark.parametrize("query", [
    "What is the weight of the mottled farxen that I previously tracked?",
    "What is the weight that I no longer track?",
])
def test_r4_revert_red_whole_text_reach_restores_hijack(query):
    with patch("src.api.main._temporal_ask_scoped_text",
               lambda q: (q or "").lower()):
        intent = _detect_temporal_query_intent(query)
        assert intent.get("history") is True or intent.get("state_phase") == "past", (query, intent)


@_NEEDS_PARSE
def test_r4_ambiguous_windows_pinned_to_todays_behaviour():
    """The critic's two AMBIGUOUS findings, pinned and DECLARED (docstring):
    both are advcl windows under rule (b); both fired pre-campaign; firing
    IS today's behaviour, so the fail-safe holds by symmetry."""
    assert "history" in _detect_temporal_query_intent(
        "What did my zorble weigh more than it used to?")
    intent = _detect_temporal_query_intent(
        "What is my zorble's reading when I previously miscalibrated it?")
    assert intent.get("history") is True and intent.get("state_phase") == "past", intent

# ── ROUND 5 (critic GAPS): the first-hop-vs-RESOLVER asymmetry — the wh branch
# guard checked the first clause head above the wh (xcomp passes) while
# _clause_governing_finite climbed past the non-finite xcomp and returned the
# finite RELCL head as an ask root ('that I used to track' — the #1 history cue
# is itself catenative; also 'that I said I formerly tracked' — ccomp inside the
# relative, leaked via the terminal-? climb). Both resolvers now reject
# relative/csubj/advcl heads and skip clauses governed by a relative.
@_NEEDS_PARSE
@pytest.mark.parametrize("query", [
    "What is the weight of the wobbly farxen that I used to track?",
    "What is the weight of the wobbly farxen that I said I formerly tracked?",
])
def test_r5_catenative_and_complement_relative_do_not_hijack(query):
    intent = _detect_temporal_query_intent(query)
    assert "history" not in intent, (query, intent)
    assert intent.get("state_phase") != "past", (query, intent)
    assert intent.get("filter_status") != "past", (query, intent)


@_NEEDS_PARSE
@pytest.mark.parametrize("query", [
    "What is the weight of the wobbly farxen that I used to track?",
    "What is the weight of the wobbly farxen that I said I formerly tracked?",
])
def test_r5_revert_red_whole_text_reach_restores_hijack(query):
    with patch("src.api.main._temporal_ask_scoped_text",
               lambda q: (q or "").lower()):
        intent = _detect_temporal_query_intent(query)
        assert intent.get("history") is True, (query, intent)


@_NEEDS_PARSE
def test_r5_r4_simple_verb_exclusion_still_holds():
    """The r4 asked-noun/simple-verb exclusion must survive the r5 hardening."""
    intent = _detect_temporal_query_intent(
        "What is the weight of the mottled farxen that I previously tracked?")
    assert "history" not in intent, intent

# ── ROUND 6 (critic GAPS): the EMBEDDED-INTERROGATIVE role — a REPORTED
# question (quoted/embedded question under a DECLARATIVE attribution) counted
# as our ask: _sentence_interrogative_kind gave its sentence wh-priority and
# _clause_ask_roots marked its ccomp head, so a third-party reported question
# carrying a history cue set intent for OUR present-tense ask. Discriminator
# (declared in _clause_is_interrogative): attribution DIRECTION at the clause
# level — a ccomp/xcomp question under a NON-INTERROGATIVE parent is reported;
# under an INTERROGATIVE parent it is the content of our ask (r3 keep-case).
@_NEEDS_PARSE
@pytest.mark.parametrize("query", [
    "He asked 'what did I used to weigh?' - what is the weight of the wobbly farxen?",
    "He asked 'what did I used to weigh?'. What is the weight of the wobbly farxen?",
    "He mentioned 'did I ever fix the fence?' - what is the weight of my zorble?",
])
def test_r6_reported_question_does_not_hijack(query):
    intent = _detect_temporal_query_intent(query)
    assert "history" not in intent, (query, intent)
    assert intent.get("state_phase") != "past", (query, intent)
    assert intent.get("filter_status") != "past", (query, intent)




@_NEEDS_PARSE
def test_r6_wondered_when_misattachment_pinned():
    """DECLARED LIMITATION, pinned (the leak child's parenthetical precedent).
    "She wondered when I formerly weighed it - what is my weight?" still
    reads as history: the parse attaches the when-clause as an ADVCL of OUR
    ask root, not as a complement of `wondered`, so by the r3 window rule
    (b) it is the ask's own temporal window. No structural discriminator
    separates this mis-attachment from a GENUINE window ("what did I weigh
    back when I lived downtown?") — same shape, and there the reading is
    exactly what we want. Recorded, not patched; if a parser upgrade fixes
    the attachment, this test goes red and should simply be inverted."""
    assert "history" in _detect_temporal_query_intent(
        "She wondered when I formerly weighed it - what is my weight?")

@_NEEDS_PARSE
def test_r6_interrogative_attribution_keep_case_still_fires():
    """r3-verified TG2 direction: the attribution verb IS the interrogative
    root, its complement is the content of OUR ask."""
    assert "history" in _detect_temporal_query_intent(
        "What did she say I formerly weighed?")


@_NEEDS_PARSE
def test_r6_genuine_embedded_past_ask_still_fires():
    """The leak child's documented genuine shape: inverted interrogative
    parent ('Do you know ...') keeps its embedded past ask."""
    assert "history" in _detect_temporal_query_intent(
        "Do you know how old my car was?")


@_NEEDS_PARSE
@pytest.mark.parametrize("query", [
    "He asked 'what did I used to weigh?' - what is the weight of the wobbly farxen?",
])
def test_r6_revert_red_whole_text_reach_restores_hijack(query):
    with patch("src.api.main._temporal_ask_scoped_text",
               lambda q: (q or "").lower()):
        intent = _detect_temporal_query_intent(query)
        assert intent.get("history") is True, (query, intent)


@_NEEDS_PARSE
def test_r6_reported_question_does_not_outrank_our_ask_sentence():
    """Third surface: a reported-question sentence must not take wh-priority
    over our genuine ask sentence and drop its cues."""
    intent = _detect_temporal_query_intent(
        "He asked 'what did I use to weigh?'. Did I ever mention the quandle?")
    assert intent.get("history") is True, intent  # OUR ask keeps its cue

# ── ROUND 7 (critic GAPS): the utterance-final-'?' containment INVERTS when the
# utterance ENDS with a reported question; and the interrogative-parent carve-out
# cannot split epistemic-access matrices from reporting-event matrices. Order-aware
# speaker's-ask (structural): fronted-wh matrix or 1st/2nd-person matrix subject =
# epistemic access (keep); polar matrix + 3rd-person subject = attribution (exclude);
# declarative parent = ours only when NO direct root-interrogative exists anywhere.
@_NEEDS_PARSE
@pytest.mark.parametrize("query", [
    "What is the weight of the mottled farxen? He asked, 'What did I used to weigh?'",
    "What is my weight? Didn't he ask what I used to weigh?",
    "What is the weight of the wobbly farxen, didn't he ask what I used to weigh?",
])
def test_r7_trailing_reported_question_does_not_hijack(query):
    intent = _detect_temporal_query_intent(query)
    assert "history" not in intent, (query, intent)
    assert intent.get("state_phase") != "past", (query, intent)
    assert intent.get("filter_status") != "past", (query, intent)


@_NEEDS_PARSE
def test_r7_keep_cases_still_fire():
    """Epistemic access and wh-matrix asks keep their embedded past asks."""
    assert "history" in _detect_temporal_query_intent(
        "Do you know how old my car was?")          # polar matrix + you
    assert "history" in _detect_temporal_query_intent(
        "What did she say I formerly weighed?")     # fronted-wh matrix


@_NEEDS_PARSE
@pytest.mark.parametrize("query", [
    "What is the weight of the mottled farxen? He asked, 'What did I used to weigh?'",
    "What is my weight? Didn't he ask what I used to weigh?",
])
def test_r7_revert_red_whole_text_reach_restores_hijack(query):
    with patch("src.api.main._temporal_ask_scoped_text",
               lambda q: (q or "").lower()):
        intent = _detect_temporal_query_intent(query)
        assert intent.get("history") is True, (query, intent)

# ── ROUND 8 (critic GAPS): _temporal_ask_scoped_text returned None (TG6
# whole-text fallback) whenever ask_roots was EMPTY — exactly what the r6/r7
# reported-question exclusion causes when the only interrogative content is
# reported. The positive identification was thrown away. Fix: REMOVAL-ONLY —
# tokens inside positively-identified reported clauses are removed even when
# no ask root survives; plus indirect ARGUMENT questions (non-direct wh-
# complement, wh = dobj/attr/nsubj) are reported wherever they attach, while
# wh-ADJUNCT windows (when/where) keep rule (b).
@_NEEDS_PARSE
@pytest.mark.parametrize("query", [
    "Current weight of my speckled farxen? He asked what it used to weigh.",
    "Didn't she ask what my wobbly farxen used to weigh?",
    "He asked what it used to weigh, but I want to know its current weight.",
    "He asked her, and she asked me, what it used to weigh - what is it now?",
])
def test_r8_reported_only_interrogative_does_not_hijack(query):
    intent = _detect_temporal_query_intent(query)
    assert "history" not in intent, (query, intent)
    assert intent.get("state_phase") != "past", (query, intent)
    assert intent.get("filter_status") != "past", (query, intent)


@_NEEDS_PARSE
def test_r8_keep_shapes_still_fire():
    """Direct-form questions and windows survive the removal-only path."""
    assert "history" in _detect_temporal_query_intent(
        "What did she say I formerly weighed?")      # fronted-wh matrix
    assert "history" in _detect_temporal_query_intent(
        "Do you know how old my car was?")           # epistemic access
    assert "history" in _detect_temporal_query_intent(
        "What did my zorble weigh back when we lived downtown?")  # wh-adjunct window
    assert "history" in _detect_temporal_query_intent(
        "Didn't I ask what I used to weigh?")        # 1st-person matrix keeps firing


@_NEEDS_PARSE
@pytest.mark.parametrize("query", [
    "Current weight of my speckled farxen? He asked what it used to weigh.",
    "Didn't she ask what my wobbly farxen used to weigh?",
])
def test_r8_revert_red_whole_text_reach_restores_hijack(query):
    with patch("src.api.main._temporal_ask_scoped_text",
               lambda q: (q or "").lower()):
        intent = _detect_temporal_query_intent(query)
        assert intent.get("history") is True, (query, intent)


@_NEEDS_PARSE
def test_r8_removal_is_monotone_not_erasing_genuine_ask():
    """The removal-only path cannot erase a genuine history ask: the cue
    rides in a direct-form clause that is never reported. Undecidable
    parses still return today's whole-query behaviour (TG6)."""
    with patch("src.api.main._temporal_ask_scoped_text", return_value=None):
        assert _detect_temporal_query_intent(
            "What did my zorble use to weigh?").get("history") is True

# ── TG6: fail-safe direction — undecidable → TODAY'S whole-query behaviour ───

@_NEEDS_PARSE
def test_tg6_scope_patchout_falls_back_to_whole_query():
    """Granularity undecidable (no parse) → the trailing-aside composite keeps
    today's WHOLE-QUERY behaviour (history fires), never a silent narrowing."""
    composite = _composite("used to", "trailing")
    with patch("src.extraction.linguistics._parse", return_value=None):
        intent = _detect_temporal_query_intent(composite)
    assert intent.get("history") is True, intent
    assert intent.get("state_phase") == "past", intent


@_NEEDS_PARSE
def test_tg6_parse_exception_falls_back_to_whole_query():
    composite = _composite("did i ever", "trailing")
    with patch("src.extraction.linguistics._parse", side_effect=RuntimeError("boom")):
        assert _detect_temporal_query_intent(composite).get("history") is True


# ── REVERT-RED: the battery above is non-vacuous ─────────────────────────────

@_NEEDS_PARSE
@pytest.mark.parametrize("cue", sorted(ASIDES))
def test_revert_red_whole_text_reach_restores_the_hijack(cue):
    """With cue REACH restored to the whole query (the pre-fix behaviour —
    _temporal_ask_scoped_text returning the whole text), every TG1 composite
    MUST hijack again. If this goes green while TG1 is green, TG1 is real;
    if this ever goes red, the TG1 assertions stopped testing the fix."""
    composite = _composite(cue, "trailing")
    with patch("src.api.main._temporal_ask_scoped_text",
               lambda q: (q or "").lower()):
        intent = _detect_temporal_query_intent(composite)
    assert intent.get("history") is True, (cue, intent)


@_NEEDS_PARSE
def test_revert_red_none_scope_keeps_today_behaviour():
    """Patch _temporal_ask_scoped_text to None (undecidable): identical to
    today's behaviour — the fail-safe IS the revert, proven from the other
    side."""
    composite = _composite("used to", "trailing")
    with patch("src.api.main._temporal_ask_scoped_text", return_value=None):
        assert _detect_temporal_query_intent(composite).get("history") is True
