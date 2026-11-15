"""ENTITY-CENTRIC FILING — the wound-map doctrine lane (the WORLD MODEL THESIS).

The owner's core doctrine: entities exist in MULTIPLES of L4 spaces simultaneously; things
stand as entities with components as slots; NOTHING flattens into user attrs. The pet ladder
is the correct shape (Fraggle the ENTITY: (fraggle, instance_of, cat) + (user, has_pet,
fraggle)); this battery pins its GENERALIZATION to possessions / events / collections under
ONE flag — ENTITY_CENTRIC_FILING (default ON; OFF = byte-parity, every arm skips whole):

  • THE MINT RULE — a naming frame whose possessed surface is a THING head mints the thing
    (instance_of its class) + the member edge from the possessor (the shared _ecf_mint_instance
    emitter the pet arm itself now calls — one Fraggle pattern, two admissions, never a fork).
  • COMPONENTS HANG OFF THE THING — "my handbag was 800 dollars" files the thing + its slot,
    never a compound user attr.
  • THE /EXPAND CONSULT — the L4 space map (entity_taxonomies, the binder's own drawn rows)
    is a lookup the mints consult; when the map holds the space, ITS defining rel names the
    member edge (typing assistive, made load-bearing). DB-down → the seeded floor.
  • THE MULTI-SPACE STAMP — the member edge lands at mint in the space the consult admits
    (things → possessions; event things → events; plural collections → the collections
    SUB-space, per the map's own member_taxonomies nesting).
  • THE L4-INVARIANT — a standing pure validator (l4_invariant_violations): classification
    edges land only on L4 places (class-shaped surfaces), never on scalars/dates/names.
  • RETRIEVAL RIDES FREE — one walk pin on the faked-cursor harness: the possessions space
    walk (user → owns-defining members) finds the handbag and the layered-fact descent
    collects its price scalar. The token-bind crutch stays (it catches stragglers).

PURE tests — no DB, no network, no GLiNER2, no LLM; the deriver DIRECT (cue classes resolve
via the in-code DB-DOWN floors). Run: python3 -m pytest tests/test_entity_centric_filing.py -q
"""
import datetime
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction import linguistics as L  # noqa: E402
from src.extraction.linguistics import (  # noqa: E402
    derive_sentence_facts,
    l4_invariant_violations,
    linguistics_available,
)

requires_model = pytest.mark.skipif(
    not linguistics_available(), reason="en_core_web_sm not installed in test env")

_REF = datetime.datetime(2026, 4, 15)


def _facts(sentence):
    return {(f.subject, f.rel_type, f.object)
            for f in derive_sentence_facts(sentence, reference=_REF)}


# ── 1. THE MINT RULE (the pet-ladder discipline generalized to things) ─────────────────────
@requires_model
def test_thing_naming_mints_the_thing_not_an_alias():
    """THE MAP100 PLAYLIST SHAPE, entity-centric: "…playlist … called Summer Vibes" stands the
    PLAYLIST as an entity — instance_of its class + the member edge from the possessor —
    instead of the alias-only twin ((playlist, also_known_as, summer vibes)) whose direction
    is inverted for an instance (the F5 wound's misleading row)."""
    facts = _facts("I created a playlist called Summer Vibes.")
    assert ("summer vibes", "instance_of", "playlist") in facts, facts
    assert ("user", "owns", "summer vibes") in facts, facts
    assert not any(r == "also_known_as" and s == "playlist" for s, r, _o in facts), facts


@requires_model
def test_mint_rule_off_restores_the_alias_only_shape_byte_for_byte(monkeypatch):
    """ENTITY_CENTRIC_FILING=OFF → the naming seam is today's, byte-for-byte: the alias-only
    edge stands and NO thing is minted."""
    monkeypatch.setattr(L, "ENTITY_CENTRIC_FILING", False)
    facts = _facts("I created a playlist called Summer Vibes.")
    assert ("playlist", "also_known_as", "summer vibes") in facts, facts
    assert ("summer vibes", "instance_of", "playlist") not in facts, facts
    assert ("user", "owns", "summer vibes") not in facts, facts


@requires_model
def test_pet_ladder_unchanged_under_both_flag_states():
    """THE HARD CONTRACT — this lane GENERALIZES, never forks: the pet arm's shape is identical
    with the flag ON and OFF (the shared emitter emits the identical rows in identical order)."""
    on = _facts("We have a cat named Fraggle.")
    monkey_off = pytest.MonkeyPatch()
    monkey_off.setattr(L, "ENTITY_CENTRIC_FILING", False)
    try:
        off = _facts("We have a cat named Fraggle.")
    finally:
        monkey_off.undo()
    pet_on = {f for f in on if "fraggle" in f}
    pet_off = {f for f in off if "fraggle" in f}
    assert ("user", "has_pet", "fraggle") in pet_on, on
    assert ("fraggle", "instance_of", "cat") in pet_on, on
    assert pet_on == pet_off, (pet_on, pet_off)


# ── 2. COMPONENTS HANG OFF THE THING (the map100 handbag_price flattening, closed) ─────────
@requires_model
def test_possessed_value_files_the_thing_and_its_slot():
    """THE MAP100 HANDBAG SHAPE: "My handbag was 800 dollars" files (user, owns, handbag) +
    (handbag, related_measure, "800 dollars") — the value is a SLOT ON THE THING, never the
    flat user attr ((user, handbag, "800 dollars")) that keyed the THING as a rel_type."""
    facts = _facts("My handbag was 800 dollars.")
    assert ("user", "owns", "handbag") in facts, facts
    assert ("handbag", "related_measure", "800 dollars") in facts, facts
    assert ("user", "handbag", "800 dollars") not in facts, facts


@requires_model
def test_premodified_thing_gets_its_type_place():
    """"My designer handbag was 800 dollars": the NP narrows the head → the thing files AT its
    type place ((designer handbag, instance_of, handbag) — the SVO companion convention); a
    bare head stays its own type place (no self-instance row)."""
    facts = _facts("My designer handbag was 800 dollars.")
    assert ("user", "owns", "designer handbag") in facts, facts
    assert ("designer handbag", "instance_of", "handbag") in facts, facts
    assert ("designer handbag", "related_measure", "800 dollars") in facts, facts
    bare = _facts("My handbag was 800 dollars.")
    assert ("handbag", "instance_of", "handbag") not in bare, bare


@requires_model
def test_components_off_restores_the_flat_user_attr_byte_for_byte(monkeypatch):
    """OFF → today's flat scalar emit, byte-for-byte (the flattening stays available as the
    pinned legacy shape)."""
    monkeypatch.setattr(L, "ENTITY_CENTRIC_FILING", False)
    facts = _facts("My handbag was 800 dollars.")
    assert ("user", "handbag", "800 dollars") in facts, facts
    assert ("user", "owns", "handbag") not in facts, facts
    assert ("handbag", "related_measure", "800 dollars") not in facts, facts


@requires_model
def test_genitive_slot_stays_the_established_on_thing_shape():
    """"My handbag's price was 800 dollars" was ALREADY entity-centric (the genitive slot
    lands on the thing: (handbag, price, …)) — this lane must not change it (the same
    subject-resolution the pet ladder uses, already extended)."""
    facts = _facts("My handbag's price was 800 dollars.")
    assert ("handbag", "price", "800 dollars") in facts, facts
    assert ("user", "owns", "handbag") in facts, facts


# ── 3. THE COLLECTION STANDS (the map100 spotify_playlist_count flattening, closed) ─────────
@requires_model
def test_plural_count_mints_the_collection_with_its_count_slot():
    """THE MAP100 PLAYLISTS SHAPE: "I have 20 playlists." stands the COLLECTION —
    (user, owns, playlists) + (playlists, count, 20) — never the flat user attr
    ((user, playlists, 20))."""
    facts = _facts("I have 20 playlists.")
    assert ("user", "owns", "playlists") in facts, facts
    assert ("playlists", "count", "20") in facts, facts
    assert ("user", "playlists", "20") not in facts, facts


@requires_model
def test_collections_off_restores_the_flat_count_byte_for_byte(monkeypatch):
    monkeypatch.setattr(L, "ENTITY_CENTRIC_FILING", False)
    facts = _facts("I have 20 playlists.")
    assert ("user", "playlists", "20") in facts, facts
    assert ("user", "owns", "playlists") not in facts, facts


@requires_model
@pytest.mark.parametrize("sentence,attr,value", [
    # the possession guard's count-lane territory (the F5 contract): a body-part count is
    # inalienable — never a collection, never minted.
    ("I have four legs.", "legs", "four"),
    # a person-class count stays the flat attr (followers are people, not possessions)
    ("I have 600 followers on Instagram.", "followers", "600"),
    # a location-class count stays the flat attr (visited places have their own L4)
    ("I've been to 15 countries.", "countries", "15"),
    # an ANIMAL count is the pet ladder's territory (never a collection mint)
    ("I have 3 cats.", "cats", "3"),
])
def test_negative_guards_counts_stay_flat(sentence, attr, value):
    """THE NEGATIVE GUARDS: person / location / inalienable-body / animal heads never admit —
    the count stays today's flat user attr with the flag ON."""
    facts = _facts(sentence)
    assert ("user", attr, value) in facts, facts
    assert not any(r == "owns" and o == attr for _s, r, o in facts), facts


@requires_model
def test_abstract_heads_stay_attrs():
    """THE ABSTRACTION GUARDS: "my day was long" and "my thoughts on X" are states of the
    speaker, not things — no thing is minted, the attr stands."""
    day = _facts("My day was long.")
    assert ("user", "day", "long") in day, day
    assert not any(r == "owns" and o == "day" for _s, r, o in day), day
    th = _facts("My thoughts on climate are strong.")
    assert ("user", "thoughts", "strong") in th, th
    assert not any(r == "owns" and o == "thoughts" for _s, r, o in th), th


@requires_model
def test_functional_slots_stay_attrs():
    """THE FUNCTIONAL-SLOT GUARD (Löbner): "my email address is …" names an ADDRESS — a slot
    with one value per possessor — never a thing. The identifier/attr paths keep it flat."""
    facts = _facts("My email address is 519-555-0123.")
    assert ("user", "email_address", "519-555-0123") in facts, facts
    assert not any(r == "owns" and "address" in o for _s, r, o in facts), facts


# ── 4. THE MULTI-SPACE STAMP (events; the collections sub-space; the consult) ──────────────
@requires_model
def test_event_thing_stamps_the_events_space_rel():
    """A NAMED event thing ("a concert called Electric Fields") stamps the events space's OWN
    defining rel (participated_in — migration 100's seeded event rel) at mint, not owns."""
    facts = _facts("I attended a concert called Electric Fields.")
    assert ("electric fields", "instance_of", "concert") in facts, facts
    assert ("user", "participated_in", "electric fields") in facts, facts


@requires_model
def test_participation_date_rides_the_event_edge():
    """THE MAP100 VOLUNTEER SHAPE (the reachable deriver-direct form): the participation edge
    carries the event_date AND the event thing stands typed — the walk
    user→events→fundraising dinner reaches the date through the events space's defining rel."""
    facts = _facts("I volunteered at a fundraising dinner on February 14th.")
    assert ("user", "volunteer_at", "fundraising dinner") in facts, facts
    assert ("fundraising dinner", "instance_of", "dinner") in facts, facts
    dated = [f for f in derive_sentence_facts(
        "I volunteered at a fundraising dinner on February 14th.", reference=_REF)
        if f.subject == "user" and f.object == "fundraising dinner"]
    assert dated and dated[0].event_date is not None, dated


@requires_model
def test_event_object_scan_admits_events_and_refuses_activities():
    """Arm D's admission helper — unit-pinnable: the event token is found ("volunteered at a
    fundraising dinner on D"), the date prep itself is never read as the object, and an
    activity frame ("wake up on Tuesdays") returns None."""
    d = L._parse("I volunteered at a fundraising dinner on February 14th.")
    verb = next(t for t in d if t.pos_ == "VERB" and t.lemma_ == "volunteer")
    tok = L._ecf_event_object_of(d, verb, "on")
    assert tok is not None and (tok.lemma_ or tok.text).strip().lower() == "dinner", tok
    d2 = L._parse("I wake up on Tuesdays and Thursdays.")
    verb2 = next(t for t in d2 if t.pos_ == "VERB" and t.lemma_ == "wake")
    assert L._ecf_event_object_of(d2, verb2, "on") is None


@requires_model
def test_expand_consult_wins_when_the_map_holds_the_space(monkeypatch):
    """THE /EXPAND CONSULT, load-bearing: when the tenant's drawn map holds a space for the
    head's class, THE MAP'S OWN defining rel names the member edge (here a grown `gear` space
    with keeps). The consult is a lookup, not a call — the monkeypatch stands in for the one
    entity_taxonomies read."""
    monkeypatch.setattr(L, "_l4_space_map", lambda: {
        "gear": {"member_entity_types": ["Object"],
                 "defining_rels": ["keeps"], "member_taxonomies": []},
    })
    hit = L._ecf_member_rel_for("handbag")
    assert hit == ("gear", "keeps"), hit
    facts = _facts("I bought a handbag called Hermes.")
    assert ("hermes", "instance_of", "handbag") in facts, facts
    assert ("user", "keeps", "hermes") in facts, facts


@requires_model
def test_plural_heads_stamp_the_sub_space():
    """THE SUB-SPACE RULE (metadata-only): a space listed inside another space's
    member_taxonomies is its NESTED SUB-SPACE; a PLURAL collection head stamps the sub-space
    (collections), a singular head the super-space (possessions)."""
    space_map = {
        "possessions": {"member_entity_types": ["Object"], "defining_rels": ["owns"],
                        "member_taxonomies": ["collections"]},
        "collections": {"member_entity_types": ["Object"], "defining_rels": ["owns"],
                        "member_taxonomies": []},
    }
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(L, "_l4_space_map", lambda: space_map)
    try:
        plural = L._l4_space_for_noun("record", None, plural=True)
        singular = L._l4_space_for_noun("handbag", None, plural=False)
    finally:
        monkeypatch.undo()
    assert plural == ("collections", "owns"), plural
    assert singular == ("possessions", "owns"), singular


@requires_model
def test_negative_guards_things_never_steal_persons_or_animals():
    """The space derivation refuses person/animal classes outright — the kin/social/pet
    ladders own those (this generalizes, never forks)."""
    assert L._l4_space_for_noun("sister") is None
    assert L._l4_space_for_noun("cat") is None
    assert L._ecf_thing_admitted("leg") is False        # inalienable body part
    assert L._ecf_thing_admitted("address") is False    # functional slot (Concept, no concrete)
    assert L._ecf_thing_admitted("thought") is False    # abstract
    assert L._ecf_thing_admitted("handbag") is True     # a real thing
    assert L._ecf_thing_admitted("playlist") is False   # undecidable offline → cue class admits


# ── 5. THE L4-INVARIANT (the standing world-model write-side audit) ────────────────────────
@requires_model
def test_l4_invariant_holds_over_the_map100_corpus():
    """The invariant holds over the doctrine corpus: every classification edge lands on an
    L4 place — no scalar, date, or name ever wears a type edge."""
    facts = []
    for s in ("We have a cat named Fraggle.",
              "I created a playlist called Summer Vibes.",
              "My handbag was 800 dollars.",
              "My designer handbag was 800 dollars.",
              "I have 20 playlists.",
              "I volunteered at a fundraising dinner on February 14th.",
              "My handbag's price was 800 dollars."):
        facts.extend(derive_sentence_facts(s, reference=_REF))
    assert l4_invariant_violations(facts) == []


def test_l4_invariant_flags_scalar_wearing_a_type_edge():
    class _F:
        def __init__(self, s, r, o):
            self.subject, self.rel_type, self.object = s, r, o
    viol = l4_invariant_violations([
        _F("user", "owns", "handbag"),
        _F("handbag", "instance_of", "800 dollars"),   # a scalar wearing a type edge
        _F("handbag", "subclass_of", "2026-02-14"),    # a date wearing a type edge
        _F("x", "instance_of", "Summer Vibes"),        # a NAME filed as a type (THE HARD LINE)
    ])
    assert ("handbag", "instance_of", "800 dollars") in viol, viol
    assert ("handbag", "subclass_of", "2026-02-14") in viol, viol
    assert ("x", "instance_of", "Summer Vibes") in viol, viol
    assert ("user", "owns", "handbag") not in viol     # non-classification edges never flag


# ── 6. RETRIEVAL RIDES FREE (the one walk pin — possessions → handbag → price) ─────────────
from src.api import main as m  # noqa: E402

_USER = "99999999-9999-9999-9999-999999999999"
_HANDBAG = "hhhhhhhh-hhhh-hhhh-hhhh-hhhhhhhhhhhh"


class _FakeDate:
    def __init__(self, iso):
        self._iso = iso

    def isoformat(self):
        return self._iso


class _WalkCursor:
    """Dispatches BOTH walk helpers' SQL shapes off one in-memory graph (the descend harness's
    FakeCursor shapes + the taxonomy row _collect_member_taxonomy_entities reads)."""

    def __init__(self, taxonomy_rows, graph_edges, scalars):
        self._tax = taxonomy_rows          # name -> (defining_rels, subs, transitive)
        self._edges = graph_edges          # (subject, rel, object, event_date)
        self._scalars = scalars            # entity_id -> [(attribute, value)]
        self._result = []
        self._one = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "FROM entity_taxonomies WHERE taxonomy_name" in s:
            row = self._tax.get(params[0])
            self._one = row and (row[0], row[1], True, row[2])
        elif "DISTINCT object_id FROM facts" in s:
            sid, rels = params[0], {r.lower() for r in params[1]}
            self._result = sorted({(e[2],) for e in self._edges
                                   if e[0] == sid and e[1].lower() in rels})
        elif "WITH RECURSIVE chain" in s:
            frontier, rels = set(params[0]), {r.lower() for r in params[1]}
            self._result = sorted({
                (e[0],) for e in self._edges if e[2] in frontier and e[1].lower() in rels})
        elif "object_id = ANY" in s:
            frontier = set(params[0])
            descent = {d.lower() for d in params[1]}
            self._result = sorted({
                (e[0],) for e in self._edges if e[2] in frontier and e[1].lower() in descent})
        elif "FROM facts WHERE subject_id = %s" in s or \
                "FROM staged_facts WHERE subject_id = %s" in s:
            sid = params[0]
            self._result = [
                (e[0], e[2], e[1], 0.8, "B", (_FakeDate(e[3]) if e[3] else None))
                for e in self._edges if e[0] == sid]
        elif "FROM entity_attributes WHERE entity_id = %s" in s:
            did = params[0]
            self._result = [(did, a, v, None, None, None)
                            for (a, v) in self._scalars.get(did, [])]
        else:
            self._result = []

    def fetchone(self):
        row = self._one
        self._one = None
        return row

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        row = self._one
        self._one = None
        return row

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_the_walk_user_possessions_handbag_price():
    """"HOW MUCH WAS THE HANDBAG?" — the serve walk through the NEW shape, no token-bind
    crutch: the possessions space walk (defining rel owns) finds the handbag member from the
    user anchor, and the layered-fact descent collects the handbag's own price scalar.
    The FILING rows come from the deriver under the flag; the WALK rows are the serve's
    existing descent — this pin proves the new shape rides it free."""
    # 1. the filing (flag ON): the thing + its slot exist as the deriver emits them
    facts = _facts("My handbag was 800 dollars.")
    assert ("user", "owns", "handbag") in facts
    assert ("handbag", "related_measure", "800 dollars") in facts
    # 2. the walk (the serve's existing descent over the SAME shape, faked cursor)
    cur = _WalkCursor(
        taxonomy_rows={"possessions": (["owns"], [], [])},
        graph_edges=[(_USER, "owns", _HANDBAG, None)],
        scalars={_HANDBAG: [("related_measure", "800 dollars")]},
    )
    members = m._collect_member_taxonomy_entities(cur, _USER, "possessions")
    assert members == {_HANDBAG}, members
    scalars = m._collect_descendant_layered_facts(
        cur, _USER, ["part_of", "member_of"], frozenset({"instance_of", "subclass_of"}),
        query_axis="membership", depth_cap=4, scope_rels=None, seed_entities=members,
    )
    hits = [f for f in scalars if f.get("attribute") == "related_measure"
            and f.get("entity_id") == _HANDBAG] or [
        f for f in scalars if f.get("rel_type") == "related_measure"]
    assert hits, scalars
    top = hits[0]
    assert "800" in str(top.get("object") or top.get("value")
                        or top.get("value_text") or ""), top
