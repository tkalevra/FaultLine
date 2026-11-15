"""Issues #61 / #50 / #49 — anchor precedence: a THING the user named outranks a relation alias and a
user scalar of the same word; a degree word never answers a different dimension.

Measured on a fresh throwaway seat (production flag values, the real harvest → /ingest → /query) at
saas 452b177c before the fix:

  #61 promotion  "My kayak is 14 feet long." on a seat whose rel ``kayak`` was already minted by the
                 pre-#36 junk fact ``(user, kayak, "feet long 14")``: the deriver's Option-A pair
                 ``(user, has, kayak) + (kayak, length, "14 feet")`` was REVERTED by
                 ``main.py::_revert_pending_ground_collisions`` (log
                 ``sentence_pipeline.pending_ground_collision_reverted``) to the flat
                 ``(user, kayak, "14 feet", object_datatype='string')`` → a USER scalar
                 ``kayak = 14 feet``; the later "16 feet" restatement was filed there too and the
                 kayak's own length never moved.
  #61 anchor     "How long is my kayak?" → ``resolve_anchor.possessive_self_scalar`` → the USER →
                 ``[(user, kayak, 'feet long 14'), (user, kayak, '14 feet')]``.
  #50            Rule 2 probed the alias ``kayak?`` (question mark kept) and, for a word that is ALSO
                 a rel_type name, skipped the alias probe entirely.
  #49            "How wide is my kayak?" (only a length stored) → the length.
  #61 (it)       an entity literally named ``figlia`` loses to the kinship alias figlia → child_of.
  #61 (prod)     "What is my favorite color?" anchored the junk has_state object ``color`` by a bare
                 alias match and rendered honest-empty over two live favorite_color rows.

Fresh-domain controls use a different owned object (telescope) and a different junk scalar name, a
different kinship alias (sorella → sibling_of) and a different possessed attribute (favorite food).
Genuine user scalars ("What is my name?", "How old am I?") still anchor on the user.
"""
import uuid

import pytest

from tests.test_smoke_ingest_35_36_37 import (  # noqa: F401 — e2e/_module_flags are module fixtures
    L, _spine, e2e, _module_flags, _state, _rows, _DSN,
)


# ── UNITS: the promotion lane (pure) ────────────────────────────────────────────────────────────

def _option_a(concept, rel, value, possessor="user"):
    return [{"subject": possessor, "rel_type": "has", "object": concept},
            {"subject": concept, "rel_type": rel, "object": value, "object_datatype": "quantity"}]


@pytest.mark.parametrize("concept,value", [("kayak", "14 feet"), ("telescope", "2 meters")])
def test_revert_never_promotes_an_existing_entity(concept, value):
    from src.api.main import _revert_pending_ground_collisions
    edges = _option_a(concept, "length", value)
    out, n = _revert_pending_ground_collisions(
        edges, existing_rel_surfaces={concept}, existing_entity_surfaces={concept})
    assert n == 0 and out == edges, out
    assert not any(e["subject"] == "user" and e["rel_type"] == concept for e in out), out


def test_revert_still_reverts_a_rel_surface_that_names_no_entity():
    """Control: the hard-line value-loss guard it was written for is unchanged."""
    from src.api.main import _revert_pending_ground_collisions
    out, n = _revert_pending_ground_collisions(
        _option_a("commute", "duration", "45 minutes"), {"commute"})
    assert n == 1, out
    assert out == [{"subject": "user", "rel_type": "commute", "object": "45 minutes",
                    "object_datatype": "string"}], out


@pytest.mark.parametrize("concept,value,unit_rel", [
    ("kayak", "14 feet", "height"), ("telescope", "2 meters", "height")])
def test_flat_scalar_naming_an_entity_is_rehomed_onto_it(concept, value, unit_rel):
    from src.api.main import _rehome_flat_scalars_naming_entities
    flat = [{"subject": "user", "rel_type": concept, "object": value, "object_datatype": "string"}]
    out, n = _rehome_flat_scalars_naming_entities(
        flat, {concept}, {"feet": unit_rel, "meter": unit_rel})
    assert n == 1, out
    assert out == [{"subject": concept, "rel_type": unit_rel, "object": value,
                    "object_datatype": "quantity"}], out


def test_flat_scalar_rehome_controls():
    from src.api.main import _rehome_flat_scalars_naming_entities
    edges = [
        {"subject": "user", "rel_type": "daily_commute", "object": "45 minutes",
         "object_datatype": "string"},                          # names no entity → kept
        {"subject": "user", "rel_type": "kayak", "object": "blue"},   # not a measure → kept
        {"subject": "core-1", "rel_type": "has_ip", "object": "10.0.0.1",
         "object_datatype": "ipv4"},                            # structured atomic → kept
    ]
    out, n = _rehome_flat_scalars_naming_entities(edges, {"kayak", "core 1"}, {"minute": "duration"})
    assert n == 0 and out == edges, out


# ── UNITS: the degree word never answers another dimension (#49) ─────────────────────────────────

@pytest.mark.parametrize("adj,attr,other", [
    ("wide", "length", True), ("deep", "length", True), ("tall", "length", True),
    ("heavy", "length", True),
    ("long", "width", True), ("tall", "width", True),        # fresh pairs
    ("big", "length", False),       # a general magnitude word admits its specific dimensions
    ("large", "height", False),
    ("long", "measure", False),     # generic carrier (rs13 octopus row)
    ("wide", "related_measure", False),                      # engine carrier: unknown dimension
    ("long", "length", False), ("tall", "height", False),    # same dimension
])
def test_degree_asks_other_dimension(adj, attr, other):
    from src.api.wordnet_ladder import degree_asks_other_dimension, _wn
    if _wn() is None:
        pytest.skip("WordNet unavailable")
    assert degree_asks_other_dimension(adj, attr) is other


# ── E2E: one planted seat carrying every junk shape, the real /query ────────────────────────────

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


def _ent(env, name, etype, aliases=()):
    eid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{env['schema']}/{name}"))
    _exec(env["schema"], "INSERT INTO entities (id, entity_type) VALUES (%s, %s) "
                         "ON CONFLICT DO NOTHING", (eid, etype))
    for i, a in enumerate((name,) + tuple(aliases)):
        _exec(env["schema"], "INSERT INTO entity_aliases (entity_id, alias, is_preferred) "
                             "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (eid, a, i == 0))
    return eid


def _fact(env, s, rel, o):
    _exec(env["schema"],
          "INSERT INTO facts (subject_id, object_id, rel_type, fact_class, provenance,"
          " fact_provenance, confidence) VALUES (%s, %s, %s, 'A', 'mcp', 'user_stated', 1.0)"
          " ON CONFLICT DO NOTHING", (s, o, rel))


def _attr(env, eid, attr, value):
    _exec(env["schema"],
          "INSERT INTO entity_attributes (user_id, entity_id, attribute, value_text, provenance)"
          " VALUES (%s, %s, %s, %s, 'user_stated') ON CONFLICT (entity_id, attribute)"
          " DO UPDATE SET value_text = EXCLUDED.value_text, superseded_at = NULL",
          (env["user_id"], eid, attr, value))


def _rel(env, rel, tail="{ANY}", category="pending_placement"):
    _exec(env["schema"],
          "INSERT INTO rel_types (rel_type, label, head_types, tail_types, category, source,"
          " engine_generated) VALUES (%s, %s, '{ANY}', %s, %s, 'engine', true)"
          " ON CONFLICT DO NOTHING", (rel, rel, tail, category))


def _alias(env, alias, canonical):
    _exec(env["schema"],
          "INSERT INTO rel_type_aliases (canonical_rel_type, alias, source) VALUES (%s, %s, 'seed')"
          " ON CONFLICT DO NOTHING", (canonical, alias))


@pytest.fixture(scope="module")
def seat(e2e):
    u = e2e["user_id"]
    # the owned kayak (named Otter) with its live length; the prior 14 feet is only history
    kayak = _ent(e2e, "kayak", "Object", ("otter",))
    _fact(e2e, u, "owns", kayak)
    _attr(e2e, kayak, "length", "16 feet")
    # JUNK (#61): the pre-#36 fact that minted the rel `kayak`, and the promoted user scalar
    _rel(e2e, "kayak")
    _fact(e2e, u, "kayak", _ent(e2e, "feet long 14", "Concept"))
    _attr(e2e, u, "kayak", "14 feet")
    # fresh domain: a different owned object + a different junk scalar name
    tele = _ent(e2e, "telescope", "Object")
    _fact(e2e, u, "owns", tele)
    _attr(e2e, tele, "length", "2 meters")
    _rel(e2e, "telescope")
    _attr(e2e, u, "telescope", "5 feet")
    # genuine user scalars (some unit-carrying — the measure family's bait)
    _attr(e2e, u, "age", "45 years")
    _attr(e2e, u, "feed", "every 12 hours")
    _attr(e2e, u, "favorite_fruit", "papaya")
    _attr(e2e, u, "shoe_size", "28 centimeters")
    # #77 regression shapes: a band the user only LIKES named after a seeded scalar rel, and an
    # unowned junk node named after another seeded scalar rel
    _fact(e2e, u, "likes", _ent(e2e, "weight", "Organization"))
    _fact(e2e, _ent(e2e, "ladder", "Object"), "has_state", _ent(e2e, "height", "Concept"))
    # #61 Italian-release shape: an entity literally named like a kinship alias
    _rel(e2e, "chiamare")
    _alias(e2e, "figlia", "child_of")
    _fact(e2e, _ent(e2e, "figlia", "Person"), "chiamare", _ent(e2e, "anna", "Person"))
    _alias(e2e, "sorella", "sibling_of")                                   # fresh control
    _fact(e2e, _ent(e2e, "sorella", "Person"), "chiamare", _ent(e2e, "lucia", "Person"))
    # #61 production shape: two live favorite_color rows + the pre-#35 junk node `color`
    _rel(e2e, "favorite_color")
    for c in ("teal", "burgundy"):
        _fact(e2e, u, "favorite_color", _ent(e2e, c, "Concept"))
    _fact(e2e, _ent(e2e, "color", "Concept"), "has_state", _ent(e2e, "teal", "Concept"))
    # fresh controls, same shape (two live values + a junk node named after the head noun):
    # a different possessed attribute each time
    for rel, head, vals in (("favorite_food", "food", ("lasagna", "sushi")),
                            ("favorite_song", "song", ("yesterday", "vienna")),
                            ("lucky_number", "number", ("seven", "eleven"))):
        _rel(e2e, rel)
        for val in vals:
            _fact(e2e, u, rel, _ent(e2e, val, "Concept"))
        _fact(e2e, _ent(e2e, head, "Concept"), "has_state", _ent(e2e, vals[0], "Concept"))
    return e2e


def _q(env, text):
    r = env["client"].post("/query", json={"text": text, "user_id": env["user_id"]})
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    facts = [(str(f.get("subject")), str(f.get("rel_type")), str(f.get("object")),
              str(f.get("definition") or "")) for f in (body.get("facts") or [])]
    return body.get("anchor"), facts


def _objs(facts):
    return " | ".join(f"{o} {d}" for (_s, _r, o, d) in facts).lower()


def _eid(env, name):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{env['schema']}/{name}"))


@_spine
@pytest.mark.parametrize("q,thing,good,stale", [
    ("How long is my kayak?", "kayak", "16 feet", "14 feet"),
    ("How long is my telescope?", "telescope", "2 meters", "5 feet"),    # fresh domain
])
def test_e2e_owned_entity_outranks_junk_user_scalar(seat, q, thing, good, stale):
    anchor, facts = _q(seat, q)
    assert anchor == _eid(seat, thing), (anchor, facts)
    got = _objs(facts)
    assert good in got, facts
    assert stale not in got and "feet long" not in got, facts


@_spine
@pytest.mark.parametrize("q", ["What is my name?", "How old am I?"])
def test_e2e_genuine_user_scalars_still_anchor_the_user(seat, q):
    anchor, facts = _q(seat, q)
    assert anchor == seat["user_id"], (anchor, facts)
    if "old" in q:
        assert "45" in _objs(facts), facts


@_spine
@pytest.mark.parametrize("q,good", [("What about figlia?", "anna"),
                                    ("What about sorella?", "lucia")])   # fresh alias/rel
def test_e2e_entity_name_outranks_relation_alias(seat, q, good):
    anchor, facts = _q(seat, q)
    assert good in _objs(facts), (anchor, facts)


@_spine
@pytest.mark.parametrize("q,stored", [
    ("What is my favorite color?", ("teal", "burgundy")),
    ("What is my favorite food?", ("lasagna", "sushi")),                  # fresh controls
    ("What is my favorite song?", ("yesterday", "vienna")),
    ("What is my lucky number?", ("seven", "eleven")),
])
def test_e2e_user_held_rel_outranks_junk_head_noun(seat, q, stored):
    anchor, facts = _q(seat, q)
    assert anchor == seat["user_id"], (anchor, facts)
    got = _objs(facts)
    assert facts and any(s in got for s in stored), facts


@_spine
@pytest.mark.parametrize("q,forbidden", [
    ("How wide is my kayak?", ("16 feet", "14 feet")),
    ("How deep is my telescope?", ("2 meters", "5 feet")),               # fresh domain
    ("How wide is my pottery kiln?", ("14 feet", "5 feet", "16 feet")),  # the bleed shape
])
def test_e2e_absent_dimension_declines(seat, q, forbidden):
    _anchor, facts = _q(seat, q)
    got = _objs(facts)
    assert not any(f in got for f in forbidden), facts


@_spine
def test_e2e_general_magnitude_still_answers(seat):
    anchor, facts = _q(seat, "How big is my kayak?")
    assert anchor == _eid(seat, "kayak"), (anchor, facts)
    assert "16 feet" in _objs(facts), facts


@_spine
def test_e2e_zz_restatement_lands_on_the_kayak_not_the_user(seat):
    """The promotion lane through the real harvest seam: with the junk rel `kayak` minted, a fresh
    "my kayak is N feet long" must move the kayak's own length and file no user scalar."""
    text = "My kayak is 18 feet long."
    _state(seat, text, [text])
    attrs = _rows(seat["schema"], "SELECT entity_id, attribute, value_text FROM entity_attributes "
                                  "WHERE superseded_at IS NULL")
    user_kayak = [v for (e, a, v) in attrs if e == seat["user_id"] and a == "kayak"]
    assert user_kayak == ["14 feet"], attrs            # the planted junk is untouched, not refilled
    on_kayak = [v for (e, a, v) in attrs if e == _eid(seat, "kayak")]
    assert "18 feet" in on_kayak, attrs


@_spine
@pytest.mark.parametrize("q", [
    "What is my favorite sport?",          # the pre-prod 843d39dc acceptance shape
    "What is my favorite season?",         # fresh domain
    "What is my lucky charm?",             # fresh modifier + head
])
def test_e2e_absent_possessed_aspect_abstains(seat, q):
    """Owner acceptance (#61 follow-up): a modified possessed aspect the user holds nothing for
    abstains — never the measure family (unit-carrying user scalars: kayak 14 feet, feed every 12
    hours, age 45 years), never the topic-filtered fetch-all (the favorite fruit, the owned things)."""
    anchor, facts = _q(seat, q)
    assert anchor == seat["user_id"], (anchor, facts)
    assert facts == [], facts


@_spine
def test_e2e_held_scalar_aspect_still_answers(seat):
    """Control: the same shape with the aspect held (as a user scalar) answers it."""
    anchor, facts = _q(seat, "What is my favorite fruit?")
    assert anchor == seat["user_id"], (anchor, facts)
    got = _objs(facts)
    assert "papaya" in got, facts
    assert "14 feet" not in got and "12 hours" not in got, facts


# ── #77: the critic's round-2 gaps ───────────────────────────────────────────────────────────────

def _path(env, q, anchor=None):
    import psycopg2
    from src.api import rel_type_overlay
    conn = psycopg2.connect(_DSN)
    tok = rel_type_overlay.set_current_schema(env["schema"])
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {env['schema']}")
        anchor = anchor or env["user_id"]
        return env["M"].determine_path(q, conn, user_id=env["user_id"], anchor_uuid=anchor,
                                       anchor_resolved_uuid=anchor)
    finally:
        rel_type_overlay.reset_current_schema(tok)
        conn.close()


@_spine
@pytest.mark.parametrize("text,rel,val,thing", [
    ("My weight is 175 pounds.", "weight", "175 pounds", "weight"),            # liked band
    ("My height is 190 centimeters.", "height", "190 centimeters", "height"),  # unowned junk node
])
def test_e2e_seeded_scalar_is_never_rehomed_onto_a_same_named_entity(seat, text, rel, val, thing):
    """#77 item 1 (regression on d49a447c): the flat-scalar re-home and the /ingest collision
    exemption fire only on the #61 junk shape (engine-minted rel + a thing the speaker holds)."""
    _state(seat, text, [text])
    attrs = _rows(seat["schema"], "SELECT entity_id, attribute, value_text FROM entity_attributes "
                                  "WHERE superseded_at IS NULL")
    assert (seat["user_id"], rel, val) in attrs, attrs
    assert not [a for a in attrs if a[0] == _eid(seat, thing)], attrs


@_spine
def test_e2e_m13_mirror_frame_never_admits_the_unit_family(seat):
    """M13: "What is my favorite color?" is no measure question — no unit-carrying scalar joins."""
    path = _path(seat, "What is my favorite color?")
    assert not {"feed", "kayak", "telescope", "age"} & set(path.scalar_rels), path.scalar_rels


@_spine
def test_e2e_m14_junk_or_liked_namesake_never_removes_the_users_dimension(seat):
    """#77 item 3: an unowned junk node named 'height' must not drop the user's own height (the
    shoe size then rode into "How tall am I?")."""
    path = _path(seat, "How tall am I?")
    assert "height" in path.scalar_rels, path.scalar_rels
    assert "shoe_size" not in path.scalar_rels, path.scalar_rels


@_spine
def test_e2e_m14_owned_engine_minted_scalar_is_excluded(seat):
    """M14 itself: the promoted junk scalars named after owned things (kayak, telescope — rels
    engine-minted) never ride into the user's general-magnitude answer; the seeded height does."""
    path = _path(seat, "How big am I?")
    assert not {"kayak", "telescope"} & set(path.scalar_rels), path.scalar_rels
    assert "height" in path.scalar_rels, path.scalar_rels


@_spine
@pytest.mark.parametrize("stmt,q,want", [
    ("My favourite colour is green.", "What is my favorite colour?", ("green",)),
    ("I just started a new job at Acme Corp.", "Where is my new job?", None),
])
def test_e2e_held_aspect_is_not_abstained(seat, stmt, q, want):
    """#77 item 2: the abstain gate grounds on a user row whose rel HEAD is the NP head, or a row to
    an entity whose alias is the whole NP."""
    _state(seat, stmt, [stmt])
    _anchor, facts = _q(seat, q)
    assert facts, facts
    if want:
        assert any(w in _objs(facts) for w in want), facts


@_spine
def test_e2e_compound_question_abstains_only_if_every_np_is_ungrounded(seat):
    _anchor, facts = _q(seat, "What is my favorite sport and my favorite fruit?")
    assert "papaya" in _objs(facts), facts
