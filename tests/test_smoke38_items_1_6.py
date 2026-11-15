"""#38 items 1 and 6 (smoke c88f04e0, seat c3d62ac4).

Item 1 — "I found my best specimen in Bancroft, Ontario." → user has_pet "best specimen in bancroft".
  (a) the NP swallowed its PP: _nominal_pp_complement / _verb_pp_value composed a PROPN PP into a
      POSSESSED object's name. A possessive determiner makes the NP a definite, identified referent;
      a PP naming another entity is its relatum, never part of its name. Indefinite value
      complements ("an Airbnb in Chicago", "a degree in Business Administration") still fold.
  (b) owns → has_pet was driven by a GLiNER2 Animal stamp (0.609) alone. With no instance_of ladder
      behind it, the upgrade (main.py) and the deriver's possession pick (_possession_rel_for_type)
      now require the object's head noun not to be contradicted by WordNet's supersense partition
      against the rel's declared range (tail_types). A named instance with a ladder ("Rex
      instance_of dog") is decided as before.
Item 6 — a refused edge re-minted by a later harvest: refused_edges (migration 286) records every
  gate refusal per (subject, rel, object); a later non-correction write of the same triple is
  withheld; a correction clears it.

Fresh-domain controls: compass/Bergen, quartz geode, goldfish, "owns sonata tempo".
"""
from __future__ import annotations

import datetime

import pytest

pytest.importorskip("spacy")

from src.extraction import linguistics as L  # noqa: E402
from tests.laneb_e2e_support import (  # noqa: E402
    e2e_fixture, fact_rows, ingest_edges, module_flags_fixture, rows,
)

_flags = module_flags_fixture()
e2e = e2e_fixture()
_REF = datetime.datetime(2026, 9, 29)


def _objs(s):
    return {(f.subject, f.rel_type, (f.object or "").lower()) for f in L.derive_sentence_facts(s, _REF)}


# ── item 1a — the NP stops at the PP on a possessed head ─────────────────────────────────────────
@pytest.mark.parametrize("text,place", [
    ("I found my best specimen in Bancroft, Ontario.", "bancroft"),
    ("I bought my favourite chisel in Leeds.", "leeds"),
    ("I lost my favourite compass in Bergen, Norway.", "bergen"),   # fresh domain
])
def test_possessed_np_does_not_swallow_its_pp(text, place):
    got = _objs(text)
    welded = [t for t in got if place in t[0].split() and t[0] != place
              or place in t[2].split() and t[2] != place]
    assert not welded, f"#38.1: PP welded into a possessed entity name: {welded}"


@pytest.mark.parametrize("text,expect", [
    ("We stayed at an Airbnb in Chicago.", "airbnb in chicago"),
    ("I have a degree in Business Administration.", "degree in business administration"),
])
def test_indefinite_value_complement_still_folds(text, expect):
    assert any(o == expect for (_s, _r, o) in _objs(text)), sorted(_objs(text))


# ── item 1b — a lone NER stamp does not drive owns → has_pet ─────────────────────────────────────
def _set_type(env, surface, etype):
    rows(env["schema"], "UPDATE entities SET entity_type = %s WHERE id IN "
                        "(SELECT entity_id FROM entity_aliases WHERE alias = %s)", (etype, surface))


@pytest.mark.parametrize("surface", ["best specimen", "quartz geode"])
def test_e2e_lone_animal_stamp_does_not_upgrade_to_has_pet(e2e, surface):
    # the smoke shape: the object entity already carries entity_type='Animal' (a GLiNER2 miss)
    ingest_edges(e2e, f"I have a {surface}.",
                 [{"subject": "user", "object": surface, "rel_type": "owns",
                   "object_type": "Animal"}])
    _set_type(e2e, surface, "Animal")
    ingest_edges(e2e, f"My {surface} is on the shelf.",
                 [{"subject": "user", "object": surface, "rel_type": "owns",
                   "object_type": "Animal"}])
    got = fact_rows(e2e)
    assert ("user", "has_pet", surface) not in got, f"#38.1: {surface} filed as a pet: {sorted(got)}"


def test_e2e_real_animal_still_upgrades(e2e):
    ingest_edges(e2e, "I have a goldfish.",
                 [{"subject": "user", "object": "goldfish", "rel_type": "owns",
                   "object_type": "Animal"}])
    _set_type(e2e, "goldfish", "Animal")
    ingest_edges(e2e, "My goldfish is orange.",
                 [{"subject": "user", "object": "goldfish", "rel_type": "owns",
                   "object_type": "Animal"}])
    got = fact_rows(e2e)
    assert ("user", "has_pet", "goldfish") in got, sorted(got)


# ── item 6 — the gate's refusal is remembered ────────────────────────────────────────────────────
@pytest.mark.parametrize("obj", ["chess rating", "sonata tempo"])      # second = fresh domain
def test_e2e_refused_edge_is_not_reminted_by_a_later_lane(e2e, obj):
    r1 = ingest_edges(e2e, f"I own my {obj}.",
                      [{"subject": "user", "object": obj, "rel_type": "owns",
                        "object_type": "Concept"}])
    assert any(x.get("rel_type") == "owns" for x in (r1.get("refused") or [])), r1
    # a later lane re-mints the SAME triple claiming Object on the edge (llm_inferred) while the
    # entity's own stamp is unchanged (still Concept) — the verdict's inputs are the same → withheld
    ingest_edges(e2e, f"The {obj} is mine.",
                 [{"subject": "user", "object": obj, "rel_type": "owns",
                   "object_type": "Object"}], source="unattested")
    got = fact_rows(e2e)
    assert ("user", "owns", obj) not in got, f"#38.6: refused edge re-minted: {sorted(got)}"


# ── #94 — a refusal is a memory of a VERDICT, not a ban ─────────────────────────────────────────
@pytest.mark.parametrize("obj", ["tide chart", "violin bow"])          # second = fresh domain
def test_e2e_94_retyped_entity_restatement_goes_back_through_the_gate(e2e, obj):
    """The gate's own remedy ("assert the entity's type … or restate the fact"): once the entity
    is retyped Object, a user_stated restatement (NOT a correction) lands, and the record is
    deleted when the gate admits the triple."""
    r1 = ingest_edges(e2e, f"I own my {obj}.",
                      [{"subject": "user", "object": obj, "rel_type": "owns",
                        "object_type": "Concept"}])
    assert any(x.get("rel_type") == "owns" for x in (r1.get("refused") or [])), r1
    _set_type(e2e, obj, "Object")
    ingest_edges(e2e, f"I own a {obj}.",
                 [{"subject": "user", "object": obj, "rel_type": "owns",
                   "object_type": "Object"}])
    assert ("user", "owns", obj) in fact_rows(e2e), f"#94: restatement withheld for {obj}"
    assert not rows(e2e["schema"], "SELECT 1 FROM refused_edges r JOIN entity_aliases a "
                                   "ON a.entity_id = r.object_id WHERE a.alias = %s", (obj,))


def test_e2e_94_house_is_big_then_i_own_a_house(e2e):
    """Organic shape: "My house is big" files (user, owns, house) with house typed LOCATION →
    refused + recorded. When the entity's type changes, "I own a house" is not withheld forever."""
    r1 = ingest_edges(e2e, "My house is big.",
                      [{"subject": "user", "object": "house", "rel_type": "owns",
                        "object_type": "Location"}])
    assert any(x.get("rel_type") == "owns" for x in (r1.get("refused") or [])), r1
    _set_type(e2e, "house", "Object")
    ingest_edges(e2e, "I own a house.",
                 [{"subject": "user", "object": "house", "rel_type": "owns",
                   "object_type": "Object"}])
    assert ("user", "owns", "house") in fact_rows(e2e), "#94: house withheld permanently"


def test_e2e_94_record_expires(e2e):
    """TTL: a record older than REFUSED_EDGE_TTL_DAYS no longer binds."""
    obj = "chart table"
    ingest_edges(e2e, f"I own my {obj}.",
                 [{"subject": "user", "object": obj, "rel_type": "owns",
                   "object_type": "Concept"}])
    rows(e2e["schema"], "UPDATE refused_edges SET last_refused_at = now() - interval '400 days'")
    M = e2e["M"]
    assert M._edge_refusal_on_record is not None
    got = rows(e2e["schema"], "SELECT r.subject_id, r.rel_type, r.object_id FROM refused_edges r "
                              "JOIN entity_aliases a ON a.entity_id = r.object_id "
                              "WHERE a.alias = %s", (obj,))
    assert got, "record planted"
    import psycopg2
    from tests.laneb_e2e_support import dsn
    conn = psycopg2.connect(dsn())
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {e2e['schema']}")
        assert M._edge_refusal_on_record(conn, *got[0]) is None, "#94: expired record still binds"
    finally:
        conn.close()


def test_94_refusal_binding_policy():
    """Unit: same type + no-higher provenance binds; a retype or a higher authority does not."""
    import src.api.main as M
    fn = getattr(M, "_refusal_still_binds", None)
    assert fn is not None, "#94: _refusal_still_binds missing (round-1 behaviour)"
    rec = {"role": "tail", "entity_type": "Concept", "provenance": "user_stated"}
    assert fn(rec, "Concept", "llm_inferred") is True
    assert fn(rec, "concept", "user_stated") is True
    assert fn(rec, "Object", "llm_inferred") is False
    low = {"role": "tail", "entity_type": "Concept", "provenance": "llm_inferred"}
    assert fn(low, "Concept", "user_stated") is False
    assert fn(None, "Concept", "user_stated") is False


def test_e2e_correction_clears_the_refusal(e2e):
    obj = "tide table"
    ingest_edges(e2e, f"I own my {obj}.",
                 [{"subject": "user", "object": obj, "rel_type": "owns",
                   "object_type": "Concept"}])
    _set_type(e2e, obj, "Object")
    ingest_edges(e2e, f"Actually I do own a {obj}.",
                 [{"subject": "user", "object": obj, "rel_type": "owns",
                   "object_type": "Object", "is_correction": True}])
    assert ("user", "owns", obj) in fact_rows(e2e)
    assert not rows(e2e["schema"], "SELECT 1 FROM refused_edges r JOIN entity_aliases a "
                                   "ON a.entity_id = r.object_id WHERE a.alias = %s", (obj,))
