"""Unit tests for the VERB-governed PP-value fold in ``derive_sentence_facts``.

The sibling of the already-shipped ``_nominal_pp_complement`` (object-head PP) fold. A prep
governed by the VERB — "I redeemed a coupon **at Target**", "I bought a laptop **from Amazon**" —
is dropped by ``_svo_predicate_token`` as a circumstantial adjunct when the verb already governs a
real object, so its NAMED value would vanish (the ``uncovered=['Target']`` residue). We fold that
value INTO the object phrase so it rides THIS clause's main, in-scope, baseline-reachable relation
(a separate ``located_at``/``related_to`` edge is NOT walk-reachable — see ``_verb_pp_value``).

PURE tests — no DB, no network, no GLiNER2, no LLM. Pure spaCy dependency parse over the deriver's
own ``en_core_web_sm`` (parser-only) pipeline.

RECALL-REACHABILITY (why fold into the object, not a separate edge): the deterministic walk
(``fetch_facts_from_anchor`` + ``_collect_descendant_layered_facts``) descends ``part_of``/
``member_of`` ONLY and never collects a 1-hop object's own outbound edges, and an active query
scope projects by ``path.allowed_rels`` — so the value only recalls if it rides the clause's own
main relation. Folding it into the object phrase achieves exactly that (same shape the shipped
nominal sibling verified e2e).

Run: python3 -m pytest tests/test_spine_verb_pp_value.py -q   (tests/ is gitignored → git add -f)
      or: python3 tools/fltest.py --bug VERBPP --test tests/test_spine_verb_pp_value.py
"""
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction import linguistics as L  # noqa: E402


def _objects(sentence):
    """The lowercased object surfaces derived from one clean sentence."""
    facts = L.derive_sentence_facts(sentence, reference=None)
    return [(f.subject, f.rel_type, f.object) for f in facts]


def _has_object_containing(sentence, needle):
    return any(needle in obj for (_s, _r, obj) in _objects(sentence))


# ── CAPTURE — a NAMED (PROPN) verb-governed PP value folds into the object (cross-domain) ────────

@pytest.mark.parametrize("sentence, needle", [
    # retail / commerce
    ("I redeemed a coupon at Target.", "target"),
    ("I bought a laptop from Amazon.", "amazon"),
    # education
    ("I earned a degree at Stanford.", "stanford"),
    # professional / org
    ("She filed the report with Deloitte.", "deloitte"),
    # tech / tooling
    ("I stored the files on GitHub.", "github"),
])
def test_named_verb_pp_value_folds_into_object(sentence, needle):
    assert _has_object_containing(sentence, needle), _objects(sentence)


def test_folded_value_rides_the_clause_main_relation():
    # The value must land ON the clause's own object (recall-reachable), not on a side rel.
    facts = L.derive_sentence_facts("I redeemed a coupon at Target.", reference=None)
    assert ("user", "redeem", "coupon at target") in [
        (f.subject, f.rel_type, f.object) for f in facts
    ], [(f.subject, f.rel_type, f.object) for f in facts]


# ── MANNER / INSTRUMENT firewall — a common-noun adjunct pobj is NEVER folded (the crux) ─────────

@pytest.mark.parametrize("sentence, adjunct", [
    ("I redeemed a coupon with enthusiasm.", "enthusiasm"),  # manner
    ("I broke the vase by accident.", "accident"),           # manner
    ("I signed the form in a hurry.", "hurry"),              # manner (has determiner)
    ("I painted the fence with a brush.", "brush"),          # instrument
])
def test_manner_instrument_adjunct_never_folded(sentence, adjunct):
    assert not _has_object_containing(sentence, adjunct), _objects(sentence)


# ── TEMPORAL firewall — a named date pobj stays on the temporal lane, never folded ──────────────

def test_temporal_pobj_not_folded():
    assert not _has_object_containing("I earned a certificate on Tuesday.", "tuesday")


def test_named_value_folds_while_temporal_pobj_dropped():
    # "at Target on Friday" — Target folds, Friday does NOT enter the object value.
    objs = _objects("I redeemed a coupon at Target on Friday.")
    joined = " ".join(o for (_s, _r, o) in objs)
    assert "target" in joined, objs
    assert "friday" not in joined, objs


# ── PRONOUN pobj excluded (PROPN-only gate) ─────────────────────────────────────────────────────

def test_pronoun_pobj_not_folded():
    # "at it" — pronoun pobj (not PROPN) is never folded as a value.
    assert not _has_object_containing("I redeemed a coupon at it.", " it")


# ── SAFE UNDER-CAPTURE — a common-noun value is left out (indistinguishable from manner) ─────────

@pytest.mark.parametrize("sentence, commonnoun", [
    ("I earned a certificate in welding.", "welding"),
    ("I fixed the bug at work.", "work"),
    ("I parked the car in the garage.", "garage"),
])
def test_common_noun_value_under_captured(sentence, commonnoun):
    # Deliberate, safe under-capture: a common-noun pobj is structurally indistinguishable from a
    # manner adjunct without a word/preposition list, so we do NOT fold it (the residue log keeps
    # it for the growth path). This pins the intended boundary — do not "fix" it by folding.
    assert not _has_object_containing(sentence, commonnoun), _objects(sentence)


# ── NO DOUBLE-FOLD — a pronoun-object clause uses predicate-particle absorption, one edge ────────

def test_pronoun_object_absorption_single_edge_no_double_fold():
    # "I met her at the conference" — the pronoun dobj is dropped, so "at" is load-bearing and the
    # pobj becomes the object via predicate-particle absorption (meet_at). The verb-PP fold must NOT
    # also fire (it gates on a real NOUN direct object), so exactly ONE edge results.
    facts = L.derive_sentence_facts("I met her at the conference.", reference=None)
    edges = [(f.subject, f.rel_type, f.object) for f in facts]
    assert ("user", "meet_at", "conference") in edges, edges
    # no duplicate / doubled object like "conference at conference"
    assert all("at conference" not in o for (_s, _r, o) in edges), edges


# ── FAIL-SAFE — the helper never raises on odd input ────────────────────────────────────────────

def test_verb_pp_value_fail_safe_none_inputs():
    assert L._verb_pp_value(None, None) == ("", [])
