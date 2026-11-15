"""Fog-family R8 (lane C, re-gate RG-G): the street-MOVE path captures through the
SAME deterministic capture as the plant path.

THE WOUND (critic RG-G): the R4-8 family was ALIVE on the street-MOVE donor -
the repair-turn arm bypassed the R7 deriver repairs: lives_at stored a malformed
space-broken street string, the move welded a phrase entity + a hollow city shard
with a real located_in row, and the family walk rendered the mess. Root causes,
all structural (no word lists):
  1. the relocation destination scan took the FIRST _DEST_PREPS PP - the
     source-side "in Weftmoor" (the OLD address district) preceded the goal PP and
     was adopted as the destination (the hollow city shard weld);
  2. a house-number destination whose parser lift stole the pobj slot ("moved to 31
     | Scrimgeour's | Lane[ROOT]") failed the PROPN place gate and the destination was
     dropped to the junk possessive chain (the phrase entity + twin);
  3. the place value was built WITHOUT the nested locative PP (truncated street
     string) and without the street/city split the plant path applies.

THE FIX: source discipline (a from+to pairing makes "to" the goal), the stolen-pobj
run-climb in the destination scan, whole-value compose (nested locative PP),
street-level destinations file live_at (digit primitive) with a separate city-only
lives_in adopted from the destination NP, and the possessive place guard runs in
address-prep mode (from/to-marked genitives are place-name members).
"""
from src.extraction.linguistics import derive_sentence_facts


def _edges(sentence):
    return [e if isinstance(e, dict) else dict(getattr(e, "__dict__", {}))
            for e in (derive_sentence_facts(sentence, None) or [])]


def _triples(sentence):
    return [(e.get("subject"), e.get("rel_type"), e.get("object"))
            for e in _edges(sentence)]


def test_street_move_whole_value_lives_at_and_city_only_lives_in():
    t = _triples("We've moved to 31 Scrimgeour's Lane in Weftmoor.")
    assert ("user", "live_at", "31 scrimgeour's lane in weftmoor") in t
    assert ("user", "lives_in", "weftmoor") in t
    # no phrase entity / junk twin beside the whole value
    assert not any(r == "related_to" for _s, r, _o in t)
    # the raw move predicate never re-mints beside the residence capture
    assert not any(r.startswith("move") for _s, r, _o in t)


def test_street_move_from_to_pairs_source_discipline():
    s = "We moved from 22 Cordwainer's Stair in Weftmoor to 31 Scrimgeour's Lane in Weftmoor."
    edges = _edges(s)
    t = [(e.get("subject"), e.get("rel_type"), e.get("object")) for e in edges]
    # DESTINATION: whole value, current
    assert ("user", "live_at", "31 scrimgeour's lane in weftmoor") in t
    # CITY: city-only, current, on the DESTINATION side (never the source district as
    # a shard welded on with a located_in row)
    assert ("user", "lives_in", "weftmoor") in t
    # PRIOR ADDRESS: history preserved as PAST (never current)
    _prior = [e for e in edges if e.get("object") == "22 cordwainer's stair"]
    assert _prior and all(e.get("temporal_status") == "past" for e in _prior)
    _curr = [e for e in edges if e.get("object") == "31 scrimgeour's lane in weftmoor"]
    assert _curr and all(e.get("temporal_status") == "now" for e in _curr)
    # no junk twins on either street
    assert not any(r == "related_to" for _s, r, _o in t)


def test_street_move_junk_twin_gone_on_goal_parse():
    # the possessive place guard now runs in address-prep mode: a genitive inside a
    # to/from-marked address PP never genitive-links to its first token
    t = _triples("We moved to 31 Scrimgeour's Lane in Weftmoor.")
    assert ("scrimgeour's lane", "related_to", "scrimgeour") not in t
    assert ("scrimgeour's lane in weftmoor", "related_to", "scrimgeour") not in t


def test_city_move_stays_lives_in():
    # R12 SUPERSEDED in part (critic RG-G-M4 move contract): a POSSESSED named
    # destination ("Pilgrim's Reach" - PROPN possessor over a place noun) is now an
    # ADDRESS and files whole-value live_at @now, exactly like Glover's Hold /
    # Lorer's Croft; the R8 digit-only gate lost the attr contract. Bare cities
    # ("moved to Berlin") still keep the lives_in reading (pinned in test_r12).
    t = _triples("We moved to Pilgrim's Reach.")
    # the whole @now address value is the attr-contract edge (temporal is pinned in
    # tests/test_r12 - _triples here carries subject/rel/object only)
    assert any(r == "live_at" and o == "pilgrim's reach"
               for _s, r, o in t), t
    # R13: the RELATIONAL residence row now points at the destination itself (the
    # lives_in=destination single-live contract) - the old no-lives_in assert is
    # superseded; both lanes carry the destination, the old city's rel is archived
    # by the ingest supersede + end-state reconciliation.


def test_plant_path_unchanged_by_r8_repairs():
    # the R7 plant capture contract still holds byte-for-byte
    t = _triples("We live at 31 Scrimgeour's Lane in Weftmoor.")
    assert ("user", "live_at", "31 scrimgeour's lane in weftmoor") in t
    assert not any(r == "related_to" for _s, r, _o in t)


def test_street_move_capture_is_repeat_stable():
    # the RG-G capture is deterministic across repeats (the R5-4 family must not
    # reopen on the move path)
    s = "We've moved to 31 Scrimgeour's Lane in Weftmoor."
    sigs = set()
    for _ in range(6):
        sigs.add(tuple(sorted(_triples(s))))
    assert len(sigs) == 1
    (sig,) = sigs
    assert any(r == "live_at" and o == "31 scrimgeour's lane in weftmoor"
               for _s, r, o in sig)
