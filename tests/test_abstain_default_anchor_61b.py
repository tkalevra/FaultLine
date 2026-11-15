"""Issue #61b (prod owner seat, saas 6e14008c): "What is my favorite color?" dumped pets and document
fragments on a seat holding NO colour data, while "What is my favorite sport?" abstained on
pre-prod.

Root cause: ``main.py::_possessed_np_aspect_grounded`` treated the mere EXISTENCE of a tenant slot
for the compound (a ``favorite_color`` rel_type minted by an earlier turn whose rows were since
cleaned/superseded) as grounding. The NP was therefore never marked ``absent_np``; the anchor fell
to ``method=default``, determine_path found no scope (``unresolved_aspect_defers_to_fetch_all``),
and the fetch-all rendered the unrelated profile. A slot grounds only when the user holds a live
row on it.

The seat is planted data-rich: pets (instance_of pet), owned things, a document-derived Concept
("first-person shooter") with a superseded ``want`` row, unit-carrying user scalars, the orphaned
``favorite_color`` rel with only a SUPERSEDED row, and no live colour data. Fresh-domain control: an
orphaned ``favorite_movie`` rel. Held control: a live ``favorite_drink`` still answers.
"""
import uuid

import pytest

from tests.test_smoke_ingest_35_36_37 import (  # noqa: F401 — e2e/_module_flags are module fixtures
    L, _spine, e2e, _module_flags, _DSN,
)


def _exec(schema, sql, params=()):
    import psycopg2
    c = psycopg2.connect(_DSN)
    try:
        with c.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(sql, params)
        c.commit()
    finally:
        c.close()


def _ent(env, name, etype):
    eid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{env['schema']}/{name}"))
    _exec(env["schema"], "INSERT INTO entities (id, entity_type) VALUES (%s, %s) "
                         "ON CONFLICT DO NOTHING", (eid, etype))
    _exec(env["schema"], "INSERT INTO entity_aliases (entity_id, alias, is_preferred) "
                         "VALUES (%s, %s, true) ON CONFLICT DO NOTHING", (eid, name))
    return eid


def _fact(env, s, rel, o, superseded=False):
    _exec(env["schema"],
          "INSERT INTO facts (subject_id, object_id, rel_type, fact_class, provenance,"
          " fact_provenance, confidence, superseded_at) VALUES (%s, %s, %s, 'A', 'mcp',"
          " 'user_stated', 1.0, " + ("now()" if superseded else "NULL") + ")"
          " ON CONFLICT DO NOTHING", (s, o, rel))


def _rel(env, rel):
    _exec(env["schema"],
          "INSERT INTO rel_types (rel_type, label, head_types, tail_types, category, source,"
          " engine_generated) VALUES (%s, %s, '{ANY}', '{ANY}', 'pending_placement', 'engine',"
          " true) ON CONFLICT DO NOTHING", (rel, rel))


def _attr(env, eid, attr, value):
    _exec(env["schema"],
          "INSERT INTO entity_attributes (user_id, entity_id, attribute, value_text, provenance)"
          " VALUES (%s, %s, %s, %s, 'user_stated') ON CONFLICT DO NOTHING",
          (env["user_id"], eid, attr, value))


@pytest.fixture(scope="module")
def seat(e2e):
    u = e2e["user_id"]
    pet = _ent(e2e, "pet", "Concept")
    for name in ("bella", "goose", "luna"):
        p = _ent(e2e, name, "Animal")
        _fact(e2e, u, "has_pet", p)
        _fact(e2e, p, "instance_of", pet)
    for thing in ("sailboat", "workbench", "telescope"):
        _fact(e2e, u, "owns", _ent(e2e, thing, "Object"))
    fps = _ent(e2e, "first-person shooter", "Concept")
    _rel(e2e, "want")
    _fact(e2e, u, "want", fps, superseded=True)
    _fact(e2e, fps, "subclass_of", _ent(e2e, "video game", "Concept"))
    _attr(e2e, u, "age", "45 years")
    _attr(e2e, u, "feed", "every 12 hours")
    # the orphaned slots: rel minted by an earlier turn, only a SUPERSEDED row remains
    _rel(e2e, "favorite_color")
    _fact(e2e, u, "favorite_color", _ent(e2e, "teal", "Concept"), superseded=True)
    _rel(e2e, "favorite_movie")
    # held control
    _rel(e2e, "favorite_drink")
    _fact(e2e, u, "favorite_drink", _ent(e2e, "espresso", "Concept"))
    return e2e


def _q(env, text):
    r = env["client"].post("/query", json={"text": text, "user_id": env["user_id"]})
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    return body.get("anchor"), [(str(f.get("subject")), str(f.get("rel_type")),
                                 str(f.get("object")), str(f.get("definition") or ""))
                                for f in (body.get("facts") or [])]


@_spine
@pytest.mark.parametrize("q", [
    "What is my favorite color?",      # the prod owner-seat shape
    "What is my favorite movie?",      # fresh domain: another orphaned slot
])
def test_orphaned_slot_does_not_ground_an_absent_aspect(seat, q):
    anchor, facts = _q(seat, q)
    assert anchor == seat["user_id"], (anchor, facts)
    assert facts == [], facts


@_spine
def test_held_slot_still_answers(seat):
    _anchor, facts = _q(seat, "What is my favorite drink?")
    assert any("espresso" in (o + d).lower() for (_s, _r, o, d) in facts), facts
