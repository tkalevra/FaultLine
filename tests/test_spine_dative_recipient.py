"""Unit tests for the VERB DATIVE RECIPIENT fold in ``derive_sentence_facts``.

The third sibling of the shipped ``_nominal_pp_complement`` (object-head PP) and ``_verb_pp_value``
(verb-governed PROPN PP) folds. The clause's INDIRECT object — the recipient, "I gave the book **to
Sarah**", "I sent an invoice **to Acme**", "I emailed the report **to my manager**" — is dropped by
the two shipped folds via two distinct parse shapes:
  • ``verb →dative(to) →pobj Acme`` — spaCy labels the recipient PP ``dative``; ``_verb_pp_value``
    iterates ``prep`` deps ONLY, so it never sees a ``dative`` "to".
  • ``verb →prep(to) →pobj manager`` — spaCy under-labels the same recipient as ``prep`` and its pobj
    is a COMMON NOUN; ``_verb_pp_value`` is ``PROPN``-only (its manner/instrument firewall).
spaCy is INCONSISTENT about ``dative`` vs ``prep`` for the very same "to"-recipient across verbs, so
both dep labels are handled for the one dative marker "to".

We fold the recipient INTO the object phrase so it rides THIS clause's main, in-scope,
baseline-reachable relation (a separate ``related_to``/``recipient`` edge is NOT walk-reachable — see
``_verb_dative_recipient``). Same shape the two shipped siblings verified e2e.

PURE tests — no DB, no network, no GLiNER2, no LLM. Pure spaCy dependency parse over the deriver's
own ``en_core_web_sm`` (parser-only) pipeline.

Run: python3 -m pytest tests/test_spine_dative_recipient.py -q   (tests/ is gitignored → git add -f)
      or: python3 tools/fltest.py --bug DATIVE --test tests/test_spine_dative_recipient.py
"""
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction import linguistics as L  # noqa: E402


def _facts(sentence):
    return [(f.subject, f.rel_type, f.object)
            for f in L.derive_sentence_facts(sentence, reference=None)]


def _objects(sentence):
    return [obj for (_s, _r, obj) in _facts(sentence)]


def _has_object_containing(sentence, needle):
    return any(needle in obj for obj in _objects(sentence))


# ── CAPTURE — the dative recipient folds into the object across domains ──────────────────────────

@pytest.mark.parametrize("sentence, needle", [
    # dative-DEP lane (spaCy labels "to" as ``dative``) — PROPN recipient
    ("I sent an invoice to Acme.", "acme"),
    # prep-"to" lane, COMMON-NOUN recipient (the gap _verb_pp_value's PROPN-only firewall leaves)
    ("I emailed the report to my manager.", "manager"),
    # cross-domain: charity / giving, 3rd-person subject, common-noun recipient
    ("She donated the funds to the shelter.", "shelter"),
    # cross-domain: legal / contracting, 3rd-person subject, PROPN recipient
    ("The company awarded the contract to Siemens.", "siemens"),
    # cross-domain: healthcare — a report to a department
    ("I forwarded the results to the clinic.", "clinic"),
])
def test_dative_recipient_folds_into_object(sentence, needle):
    assert _has_object_containing(sentence, needle), _facts(sentence)


def test_folded_recipient_rides_the_clause_main_relation():
    # The recipient lands ON the clause's own object value (recall-reachable), not on a side edge.
    facts = _facts("I sent an invoice to Acme.")
    # exactly one main relation, object carries the recipient
    mains = [(s, r, o) for (s, r, o) in facts if "acme" in o]
    assert mains, facts
    subj, rel, obj = mains[0]
    assert subj == "user"
    assert obj.startswith("invoice") and "acme" in obj, facts
    # NOT emitted as a bare (x, related_to/recipient, acme) side edge
    assert not any(r in ("related_to", "recipient") and "acme" in o for (_s, r, o) in facts), facts


# ── NO DOUBLE-FOLD — a prep+PROPN "to" is _verb_pp_value's; the dative helper must not re-fold it ─

@pytest.mark.parametrize("sentence, needle", [
    ("I gave the book to Sarah.", "sarah"),      # spaCy: prep "to" + PROPN Sarah → _verb_pp_value
    ("I drove the car to Toronto.", "toronto"),  # directional prep+PROPN → _verb_pp_value
])
def test_no_double_fold_with_verb_pp_value(sentence, needle):
    objs = _objects(sentence)
    assert any(needle in o for o in objs), _facts(sentence)
    # the recipient must appear at most ONCE — never "book to sarah to sarah"
    for o in objs:
        assert o.count(" to ") <= 1, _facts(sentence)
        assert o.count(needle) <= 1, _facts(sentence)


# ── PRONOUN FIREWALL — a pronoun recipient is NOT a mergeable entity, never folded ───────────────

@pytest.mark.parametrize("sentence", [
    "I sent the report to it.",
    "I mailed the package to them.",
])
def test_pronoun_recipient_excluded(sentence):
    for o in _objects(sentence):
        assert "it" != o and "them" not in o.split()
        # the object stays the bare dobj (no "to <pronoun>" fold)
        assert " to " not in o, _facts(sentence)


# ── TEMPORAL FIREWALL — a date recipient/time PP is never folded as an object value ──────────────

def test_temporal_pp_not_folded_as_recipient():
    # "on Friday" is a temporal PP, not a recipient — it must not ride the object as "to friday"/"friday"
    objs = _objects("I sent an invoice to Acme on Friday.")
    assert any("acme" in o for o in objs), _facts("I sent an invoice to Acme on Friday.")
    for o in objs:
        assert "friday" not in o, _facts("I sent an invoice to Acme on Friday.")


# ── DOCUMENTED UNDER-CAPTURE — double-object dative (no surface "to") is left to the growth path ──

def test_double_object_dative_is_safe_undercapture():
    # "I gave Sarah the book" → verb →dative Sarah (no "to", no pobj). We do NOT synthesise a "to";
    # the recipient is left on the residue/growth path (the safe under-capture). Assert we at least
    # still capture the direct object cleanly and never crash / never mis-fold.
    facts = _facts("I gave Sarah the book.")
    assert any(r == "give" and "book" in o for (_s, r, o) in facts), facts
    # no fabricated "to sarah" fold from a synthesised preposition
    assert not any("to sarah" in o for (_s, _r, o) in facts), facts


# ── SUBJECT-AGNOSTIC — an arbitrary (non-user) subject folds identically ─────────────────────────

def test_subject_agnostic_third_person():
    facts = _facts("Carol sent the parcel to Acme.")
    assert any("acme" in o for (_s, _r, o) in facts), facts
