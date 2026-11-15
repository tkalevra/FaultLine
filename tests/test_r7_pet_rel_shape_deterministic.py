"""Fog-family R7 (lane C, critic R5-6): the pet REL must be shape-DETERMINISTIC.

THE WOUND (measured live, bench seat): "We have an owl named Pobble." filed
(user, owns, <owl>) — committed=1, NO has_pet — while "Myrtle the tortoise" filed
owns+has_pet+instance_of. The pet arm admitted ONLY heads in the grown per-tenant
pet_noun class; that class SELF-FEEDS (it grows from what the pet arm already
captured), so a head the class never saw — "owl", GLiNER-declared bird on that
seat — was permanently outside the arm, and the generic possession overlay then
resolved owns FROM THE ENTITY TYPE (Animal -> has_pet, bird -> owns): the pet REL
flipped with the GLiNER declaration and pets queries (rels=[has_pet]) went blind.

THE FIX: the arm admits the grown class UNION the invariant seed floor (widened to
the commonly-kept + story-famous animal heads, owl/pegasus named in the finding),
and pins the rel has_pet unconditionally once admitted. The declared type keeps
gating the ENTITY (instance_of owl) — NEVER the rel. Typing may no longer decide
owns-vs-has_pet for any shape the arm admits.
"""
from src.extraction.linguistics import (_PET_NOUN_SEED, _pet_nouns,
                                        derive_sentence_facts)


def _edges(sentence):
    return [e if isinstance(e, dict) else dict(getattr(e, "__dict__", {}))
            for e in (derive_sentence_facts(sentence, None) or [])]


def test_seed_floor_carries_the_wound_heads():
    # the invariant admission minimum carries the critic-named heads
    assert "owl" in _PET_NOUN_SEED and "pegasus" in _PET_NOUN_SEED


def test_admission_is_class_union_floor():
    # the grown class alone shadows the floor (the R5-6 starvation); the arm reads the
    # union, so the floor heads admit EVEN when a grown class omits them
    assert "owl" in _pet_nouns() | _PET_NOUN_SEED


def test_owl_named_possession_files_has_pet():
    edges = _edges("We have an owl named Pobble.")
    assert ("user", "has_pet", "pobble") in [
        (e.get("subject"), e.get("rel_type"), e.get("object")) for e in edges]
    assert ("pobble", "instance_of", "owl") in [
        (e.get("subject"), e.get("rel_type"), e.get("object")) for e in edges]


def test_pegasus_named_possession_files_has_pet():
    edges = _edges("We have a pegasus named Skydancer.")
    rels = [(e.get("subject"), e.get("rel_type"), e.get("object")) for e in edges]
    assert ("user", "has_pet", "skydancer") in rels


def test_typing_never_downgrades_the_pet_rel():
    # THE R5-6 CONTRACT: for an admitted pet head the rel is has_pet ALWAYS — owns is
    # never minted beside it, whatever the entity typing resolves to
    for s in ("We have an owl named Pobble.", "We have a pegasus named Skydancer.",
               "We have a cat named Turnip."):
        assert not any(e.get("rel_type") == "owns" for e in _edges(s))


def test_non_pet_head_never_files_has_pet():
    # the widened floor must not over-admit: a server named Atlas is NOT a pet
    edges = _edges("I have a server named Atlas.")
    assert not any(e.get("rel_type") == "has_pet" for e in edges)
