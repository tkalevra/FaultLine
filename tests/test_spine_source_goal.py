"""Unit tests for the SOURCE→GOAL "to"-complement handling in ``derive_sentence_facts`` (G7).

Source→goal transfer/change verbs — "switched **from** Python **to Rust**", "moved **from** London
**to Tokyo**", "upgraded **from** v1 **to v2**", "changed jobs **from** Acme **to Globex**" — carry
their knowledge-update answer in the GOAL (the "to" pobj: what did I switch/upgrade TO). Before this
fix the deriver grabbed the *source* ("from X") as the object and dropped the goal as residue, or
concatenated source+goal into one object phrase. The fix (all pure grammar, subject-agnostic) makes
the GOAL win and NEVER concatenates source+goal:

  • objectless verb ("switched from Python to Rust") → the goal "to" is the load-bearing prep, so the
    SVO predicate/object select the goal (``switch_to`` / ``rust``), not the source (``switch_from`` /
    ``python``). The source is left on the residue/growth path.
  • verb with a direct object ("changed jobs from Acme to Globex") → the goal folds onto the object
    ("jobs to globex") via ``_verb_pp_value``; the source "from Acme" (which hangs off the object
    noun) is FIREWALLED out of ``_nominal_pp_complement`` → no "jobs from acme to globex" concat.

Detection is the grammatical from+to PAIRING (dep/POS + the closed-class directional-goal preposition
set) — NO verb/domain word list, the prepositions are NEVER mapped to a rel_type. A plain single-PP
clause ("went to a concert", "bought a laptop from Amazon") is byte-identical.

PURE tests — no DB, no network, no GLiNER2, no LLM. Pure spaCy dependency parse over the deriver's own
``en_core_web_sm`` (parser-only) pipeline.

Run: python3 -m pytest tests/test_spine_source_goal.py -q   (tests/ is gitignored → git add -f)
      or: python3 tools/fltest.py --bug G7 --test tests/test_spine_source_goal.py
"""
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction import linguistics as L  # noqa: E402


def _edges(sentence):
    facts = L.derive_sentence_facts(sentence, reference=None)
    return [(f.subject, f.rel_type, f.object) for f in facts]


def _joined_objects(sentence):
    return " ".join(obj for (_s, _r, obj) in _edges(sentence))


# ── GOAL WINS OVER SOURCE — objectless change verb picks the "to" goal, not the "from" source ───

@pytest.mark.parametrize("sentence, verb, goal, source", [
    ("I switched from Python to Rust.", "switch", "rust", "python"),
    ("I upgraded from v1 to v2.", "upgrade", "v2", "v1"),
    ("I flew from NYC to LA.", "fly", "la", "nyc"),
    ("I migrated from MySQL to Postgres.", "migrate", "postgres", "mysql"),
])
def test_objectless_change_picks_goal_not_source(sentence, verb, goal, source):
    edges = _edges(sentence)
    # the GOAL is the object, on a <verb>_to predicate (the goal prep is the load-bearing one)
    assert ("user", f"{verb}_to", goal) in edges, edges
    # the SOURCE is NEVER the object, and NEVER folded as a <verb>_from edge
    assert all(obj != source for (_s, _r, obj) in edges), edges
    assert all(r != f"{verb}_from" for (_s, r, _o) in edges), edges


# ── NO CONCATENATION — the source is never glued onto the goal in one object phrase ─────────────

def test_no_source_goal_concatenation_objectless():
    joined = _joined_objects("I switched from Python to Rust.")
    assert "rust" in joined, joined
    assert "python" not in joined, joined  # source excluded, not concatenated


def test_dobj_change_folds_goal_not_source():
    # "changed jobs from Acme to Globex" — the goal "to globex" rides the object; the source "from
    # Acme" (hanging off "jobs") is firewalled → object is "jobs to globex", NEVER "jobs from acme …".
    joined = _joined_objects("I changed jobs from Acme to Globex.")
    assert "globex" in joined, joined
    assert "acme" not in joined, joined
    assert "from" not in joined, joined


# ── RELOCATION CASE — a residence relocation still lands as lives_in(goal), goal wins ────────────

def test_relocation_from_to_lands_lives_in_goal():
    edges = _edges("I moved from London to Tokyo.")
    assert ("user", "lives_in", "tokyo") in edges, edges
    # the source London is not the residence, and no stray move_from/move_to edge
    assert all(obj != "london" for (_s, _r, obj) in edges), edges
    assert all(r not in ("move_from", "move_to") for (_s, r, _o) in edges), edges


# ── SINGLE-PP CLAUSES BYTE-IDENTICAL — no from+to pairing → unchanged behavior ───────────────────

def test_single_to_clause_unchanged():
    # only a "to" goal, no "from" source → the ordinary load-bearing "to" path (go_to concert).
    assert ("user", "go_to", "concert") in _edges("I went to a concert."), _edges("I went to a concert.")


def test_single_from_clause_unchanged():
    # only a "from" source, no "to" goal → the existing _verb_pp_value source fold is preserved.
    joined = _joined_objects("I bought a laptop from Amazon.")
    assert "laptop" in joined and "amazon" in joined, joined


# ── TEMPORAL FIREWALL — a date on the change clause never becomes the goal object ────────────────

def test_temporal_not_taken_as_goal():
    # "on Friday" is a temporal adjunct; the goal is still Rust, Friday never enters the object.
    joined = _joined_objects("I switched from Python to Rust on Friday.")
    assert "rust" in joined, joined
    assert "friday" not in joined, joined


def test_temporal_only_to_is_not_a_goal_change():
    # "postponed the launch from Monday to Friday" — BOTH pobjs are dates. The temporal firewall
    # means neither "from Monday" nor "to Friday" qualifies as a source/goal, so no date is folded
    # as the object value (dates ride the temporal lane, never a relationship object).
    joined = _joined_objects("I postponed the launch from Monday to Friday.")
    assert "monday" not in joined, joined
    assert "friday" not in joined, joined


# ── PRONOUN FIREWALL — a pronoun goal/source is left to the coref lanes, never folded ────────────

def test_pronoun_goal_not_folded():
    # "to it" — pronoun goal pobj (not a content NOUN/PROPN) → not selected as the goal object.
    joined = _joined_objects("I switched from Python to it.")
    assert " it" not in (" " + joined), joined


# ── HARD LINE — the goal is a value/object, never routed to instance_of/subclass_of ─────────────

def test_goal_never_hierarchy_typed():
    edges = _edges("I switched from Python to Rust.")
    assert all(r not in ("instance_of", "subclass_of") or o != "rust"
               for (_s, r, o) in edges), edges


# ── LOCKSTEP HELPERS — predicate + object selection agree on the goal prep ───────────────────────

def test_svo_helpers_agree_on_goal():
    doc = L._parse("I switched from Python to Rust.")
    verb = next(t for t in doc if t.pos_ == "VERB")
    goal = L._source_goal_change_goal_prep(verb)
    assert goal is not None and goal.text.lower() == "to"
    assert L._svo_predicate_token(verb) == "switch_to"
    assert L._svo_object_head(verb).text.lower() == "rust"


# ── FAIL-SAFE — the helpers never raise on odd input ────────────────────────────────────────────

def test_source_goal_helpers_fail_safe():
    assert L._source_goal_change_goal_prep(None) is None
    assert L._directional_goal_prep(None) is None
    assert L._clause_has_source_from(None) is False
    assert L._prep_content_pobj(None) is None
