"""FIX B — HEARST EXEMPLIFICATION: the one construction whose job is to announce `instance_of`.

THE MEASURED LOSS (2026-07-30, offline `derive_sentence_facts`, ref 2022-03-05):

    "I've been attending various workshops and lectures, like the workshop on 'Effective Time
     Management' at the local community center last Saturday."
        -> (user, attend, various workshops) @2022-02-26
           (user, attend, lectures)          @2022-02-26
        …the NAMED event vanished entirely, with a CRIT `derive_residue_uncovered` listing
        ['workshop','Effective','Time','Management','community','center'].

    Two markers were worse than lossy — they were DESTRUCTIVE, folding the marker INTO the object:
    "I have several pets, such as a dog named Fraggle."  -> (user, have, "several pets AS dog")
    "I visited many museums including the Louvre last April."
                                             -> (user, visit, "many museums INCLUDING louvre")
                                                + the resolved 2022-04-05 orphaned

These are Hearst's canonical lexico-syntactic hyponymy patterns (Hearst 1992, "Automatic Acquisition
of Hyponyms from Large Text Corpora", COLING-92, §2: "NP such as NP", "NP including NP", "NP, like
NP"). Losing them is losing the only construction class in English whose entire job is to assert the
`instance_of` edge — the L4 PLACE this engine is built around.

THE NEGATIVE HALF IS LOAD-BEARING. "like" is polysemous: exemplification vs MANNER/similarity. A
manner adjunct must mint nothing — "I felt like a fraud", "I ate lunch like a king", "I treat my
dogs like children", "I ran like the wind". Two independent gates enforce that: the marker's
`comma_required` mode (nonrestrictive apposition) and the NP0-is-plural class-term test.

Pure/offline: spaCy only (no DB, no network, no LLM); the cue class falls safe to the in-code seed.
"""
import datetime

import pytest

REF = datetime.datetime(2022, 3, 5)

WORKSHOP = ("I've been attending various workshops and lectures, like the workshop on "
            "'Effective Time Management' at the local community center last Saturday.")


def _facts(sentence):
    from src.extraction.linguistics import derive_sentence_facts
    return derive_sentence_facts(sentence, REF) or []


def _triples(facts):
    return {(f.subject, f.rel_type, f.object) for f in facts}


def _date_of(facts, triple):
    for f in facts:
        if (f.subject, f.rel_type, f.object) == triple:
            return str(getattr(f, "event_date", None) or "")[:10]
    return None


# Four UNRELATED sentences, three UNRELATED markers, four UNRELATED domains, two different subjects
# (first-person and third-person). Shape (1) = the predicate replayed onto the exemplar; shape (2) =
# the Hearst hyponymy edge.
@pytest.mark.parametrize("sentence,replay,hyponymy", [
    (WORKSHOP,
     ("user", "attend", "workshop on effective time management"),
     ("workshop on effective time management", "instance_of", "workshop")),
    ("I have several pets, such as a dog named Fraggle.",
     ("user", "have", "dog"), ("dog", "instance_of", "pet")),
    ("I visited many museums including the Louvre last April.",
     ("user", "visit", "louvre"), ("louvre", "instance_of", "museum")),
    ("I take several medications such as metformin.",
     ("user", "take", "metformin"), ("metformin", "instance_of", "medication")),
    ("She reviewed multiple vendors including Acme Corp last March.",
     ("she", "review", "acme corp"), ("acme corp", "instance_of", "vendor")),
])
def test_exemplar_is_captured_and_typed(sentence, replay, hyponymy):
    got = _triples(_facts(sentence))
    assert replay in got, (sentence, got)
    assert hyponymy in got, (sentence, got)


@pytest.mark.parametrize("sentence,general,exemplar", [
    (WORKSHOP, ("user", "attend", "various workshops"),
     ("user", "attend", "workshop on effective time management")),
    ("I visited many museums including the Louvre last April.",
     ("user", "visit", "many museums"), ("user", "visit", "louvre")),
    ("I have visited many countries, like Japan, on 5/2.",
     ("user", "visit", "many countries"), ("user", "visit", "japan")),
])
def test_the_clause_date_rides_onto_the_exemplar(sentence, general, exemplar):
    """The exemplar is an occurrence of the SAME dated event as the general NP — the replay must
    carry the clause's event_date, exactly as the conjunct/dash-list distribution already does."""
    facts = _facts(sentence)
    d_general, d_exemplar = _date_of(facts, general), _date_of(facts, exemplar)
    assert d_general, (sentence, _triples(facts))
    assert d_exemplar == d_general, (sentence, d_general, d_exemplar)


@pytest.mark.parametrize("sentence,forbidden_fragment", [
    ("I have several pets, such as a dog named Fraggle.", " as "),
    ("I visited many museums including the Louvre last April.", "including"),
])
def test_the_marker_never_folds_into_an_object_surface(sentence, forbidden_fragment):
    """The DESTRUCTIVE half: "several pets as dog" / "many museums including louvre" were real
    emitted objects. No emitted subject or object may contain the marker any more."""
    for subj, _rel, obj in _triples(_facts(sentence)):
        assert forbidden_fragment not in f" {subj} ", (sentence, subj)
        assert forbidden_fragment not in f" {obj} ", (sentence, obj)


# ───────────────────────── THE POLYSEMY FIREWALL (load-bearing) ─────────────────────────
@pytest.mark.parametrize("sentence,ghost", [
    ("I felt like a fraud.", "fraud"),          # copular complement — there is no general NP at all
    ("I ate lunch like a king.", "king"),       # manner adjunct, singular NP0
    ("I treat my dogs like children.", "children"),   # manner adjunct, no appositive comma
    ("I ran like the wind.", "wind"),           # manner adjunct, no object at all
])
def test_non_exemplifying_like_mints_nothing(sentence, ghost):
    got = _triples(_facts(sentence))
    assert not any(ghost in subj or ghost in obj for subj, _r, obj in got), (sentence, got)
    assert not any(rel == "instance_of" for _s, rel, _o in got), (sentence, got)


def test_a_manner_like_is_unchanged_by_the_fix(monkeypatch):
    """Belt-and-braces: the manner readings must be IDENTICAL with the chain off — proving the
    firewall, not luck, is what keeps them clean."""
    import src.extraction.linguistics as L
    on = {s: _triples(_facts(s)) for s in
          ("I felt like a fraud.", "I ate lunch like a king.", "I ran like the wind.")}
    monkeypatch.setattr(L, "SPINE_EXEMPLIFICATION", False)
    off = {s: _triples(_facts(s)) for s in on}
    assert on == off, {k: on[k] ^ off[k] for k in on if on[k] != off[k]}


# NOTE ON HOW OFF⇒LEGACY IS PINNED. An earlier version of the test below asserted the exact measured
# pre-fix triples ("user, have, several pets AS dog"). It passed standalone and FAILED in the full
# suite: the deriver's legacy output for that sentence is not stable across the suite (other tests
# mutate module-level deriver state, and the named-instance lane then renders it differently). Pinning
# a legacy surface that other tests can move is a test bug, not a product signal. What IS invariant,
# and what the flag actually promises, is that NONE of this chain's edges exist when it is off — plus
# the in-process ON-vs-OFF identity check above for sentences the chain must not touch at all.
@pytest.mark.parametrize("sentence,mine", [
    ("I have several pets, such as a dog named Fraggle.",
     [("user", "have", "dog"), ("dog", "instance_of", "pet")]),
    ("I visited many museums including the Louvre last April.",
     [("user", "visit", "louvre"), ("louvre", "instance_of", "museum")]),
    (WORKSHOP, [("user", "attend", "workshop on effective time management"),
                ("workshop on effective time management", "instance_of", "workshop")]),
])
def test_flag_off_emits_none_of_this_chains_edges(sentence, mine, monkeypatch):
    """SPINE_EXEMPLIFICATION=off → the chain returns immediately and contributes nothing."""
    import src.extraction.linguistics as L
    on = _triples(_facts(sentence))
    for triple in mine:
        assert triple in on, ("flag ON must produce it", sentence, triple, on)
    monkeypatch.setattr(L, "SPINE_EXEMPLIFICATION", False)
    off = _triples(_facts(sentence))
    for triple in mine:
        assert triple not in off, ("flag OFF must not produce it", sentence, triple, off)
