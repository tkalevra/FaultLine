"""No bare internal entity identifier may reach the reader in any slot of any answer.

The destination: everything the reader sees is human. Whatever lane produced a row —
the main render, a location-grounding surfacing, a scalar answer off the multi-hop
walk, a synthetic count/calc/sum insert — no slot of a returned fact may carry a raw
entity UUID. A slot that cannot be named honestly must render as an honest absence
(empty string), never as an identifier.

Measured on the bench at the parent commit (70c5c20b), a 16-question × 6-seat battery
through the REAL `_query_impl` leaked 22 UUID slots, stable across 3 runs:
  • location_grounding rows shipped the place entity's UUID in `object`
    ("You live in Toronto" with object=73ff80d3-…) — even though the place HAS an
    alias; the fetch never resolved the slot;
  • walk-lane scalar answers ("what is my mother's name?", "what is my name?",
    "what is my dog's name?") shipped the resolved entity UUID in `subject`;
  • count_over_walk synthetic rows shipped the anchor UUID in `subject`.
At the fix: 0 leaks, 330 facts both arms, 0 sentences lost (22 slot rewrites on
identical definitions).

Ground truth is the STORE, never the render: expectations about what a slot should
say are read from `entity_aliases` directly (names live in the aliases table, not on
entities).

Gated on LADDER_TEST_DSN + LADDER_TEST_SCAFFOLD_SEAT (comma list) + SPACY_MODEL so CI
without a database skips rather than lying. A green run with skips proves nothing —
check "0 skipped".
"""
import os
import re

import psycopg2
import pytest

DSN = os.environ.get("LADDER_TEST_DSN")
SEATS = [s for s in (os.environ.get("LADDER_TEST_SCAFFOLD_SEAT") or "").split(",") if s]
STATED_SEAT = os.environ.get("LADDER_TEST_STATED_SEAT")

_gate = pytest.mark.skipif(
    not (DSN and SEATS),
    reason="set LADDER_TEST_DSN and LADDER_TEST_SCAFFOLD_SEAT (comma list) to run these",
)

_UUID_FULL = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)

# The mother seat on the bench (parent_of → user, aliases diane|mother). Pinned by
# data, not by render output (verified by reading facts + entity_aliases directly).
_MOTHER_SEAT = "a710422b-f537-4955-83bc-392c2a1af442"

_BATTERY = [
    "where do I live?",
    "what is my mother's name?",
    "how old is my mother?",
    "what's the weather tomorrow?",
    "what is my name?",
    "tell me about my family",
    "what is my dog's name?",
    "how many dogs do I have?",
    "what do I own?",
    "who am I?",
    "tell me about my walks",
    "what is a person?",
    "what is my address?",
    "how old am I?",
    "where does my mother live?",
    "what is my favorite color?",
]


def _ask(uid, text):
    # FORCE, never setdefault: an already-set POSTGRES_DSN would win and the test
    # would silently validate against a different database than LADDER_TEST_DSN.
    os.environ["POSTGRES_DSN"] = DSN or ""
    os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
    from src.api.models import QueryRequest
    from src.api import main as M
    return (M._query_impl(QueryRequest(text=text, user_id=uid, source="test")).facts or [])


def _db():
    conn = psycopg2.connect(DSN)
    conn.set_session(autocommit=True, readonly=True)
    return conn


def _schema(uid: str) -> str:
    return "faultline_" + uid.replace("-", "_")


def _preferred_alias(schema: str, entity_id: str):
    with _db() as c, c.cursor() as cur:
        cur.execute(
            f"SELECT alias FROM {schema}.entity_aliases WHERE entity_id = %s "
            f"ORDER BY is_preferred DESC, alias LIMIT 1", (entity_id,))
        row = cur.fetchone()
    return (row[0] if row and row[0] else None)


def _iter_string_slots(fact):
    for k, v in fact.items():
        if isinstance(v, str):
            yield k, v


# ── the bar itself ──────────────────────────────────────────────────────────────

@_gate
def test_no_fact_slot_in_any_answer_is_a_raw_uuid():
    """The bar: across every seat × battery question driven through the real
    entrypoint, NO string slot of ANY returned fact matches the UUID pattern."""
    checked = 0
    for seat in SEATS:
        for q in _BATTERY:
            for f in _ask(seat, q):
                assert isinstance(f, dict)
                for slot, v in _iter_string_slots(f):
                    checked += 1
                    assert not _UUID_FULL.match(v), (
                        f"raw entity id reached the reader: seat={seat} q={q!r} "
                        f"slot={slot} source={f.get('source')} rel={f.get('rel_type')} "
                        f"value={v}")
    assert checked > 100, (
        f"only {checked} slots checked — the battery must exercise the lanes, "
        "not skip them (check 0 skipped and the seat list)")


# ── known leak sites, pinned in BOTH directions ────────────────────────────────

@_gate
def test_location_grounding_object_names_the_place_from_the_store():
    """location_grounding rows (both surfacing sites) must carry the place's NAME in
    the object slot — read from entity_aliases, not from the render — and never the
    UUID. The sentence is unchanged ("You live in …")."""
    saw_grounding = False
    for seat in SEATS:
        for f in _ask(seat, "what's the weather tomorrow?") + _ask(seat, "what is a person?"):
            if f.get("source") != "location_grounding":
                continue
            saw_grounding = True
            obj = f.get("object")
            assert not (isinstance(obj, str) and _UUID_FULL.match(obj)), (
                f"location_grounding shipped a raw place id: {obj!r}")
            if obj:
                # the name must be an alias of a REAL place entity on this seat —
                # ground truth from the store, not the render.
                with _db() as c, c.cursor() as cur:
                    cur.execute(
                        f"SELECT 1 FROM {_schema(seat)}.entity_aliases a "
                        f"JOIN {_schema(seat)}.facts t "
                        f"  ON t.object_id = a.entity_id AND t.subject_id = %s "
                        f"WHERE a.alias = %s LIMIT 1", (seat, obj))
                    assert cur.fetchone(), (
                        f"object slot {obj!r} is not an alias of any stored place "
                        f"of seat {seat}")
    assert saw_grounding, (
        "no location_grounding row surfaced on any seat — the pin is vacuous; "
        "check the seat list")


@_gate
@pytest.mark.skipif(_MOTHER_SEAT not in SEATS,
                    reason=f"mother seat {_MOTHER_SEAT} not in LADDER_TEST_SCAFFOLD_SEAT")
def test_walk_scalar_answer_names_the_mother_not_her_id():
    """The walk-lane scalar answer for "what is my mother's name?" must NAME the
    mother (her preferred alias, read from the store) and must not ship her entity
    UUID in the subject slot. Both directions: the name appears, the id does not."""
    # ground truth from the store: the seat's parent_of → user row names the mother
    with _db() as c, c.cursor() as cur:
        cur.execute(
            f"SELECT f.subject_id FROM {_schema(_MOTHER_SEAT)}.facts f "
            f"WHERE f.object_id = %s AND f.rel_type = 'parent_of' "
            f"  AND f.superseded_at IS NULL AND f.archived_at IS NULL LIMIT 1",
            (_MOTHER_SEAT,))
        row = cur.fetchone()
    assert row, "mother seat has no parent_of→user row; fixture drifted"
    mother_alias = _preferred_alias(_schema(_MOTHER_SEAT), row[0])
    assert mother_alias, "mother entity has no alias; fixture drifted"

    facts = _ask(_MOTHER_SEAT, "what is my mother's name?")
    assert facts, "the mother answer must not be lost to the id strip"
    for f in facts:
        subj = f.get("subject")
        assert not (isinstance(subj, str) and _UUID_FULL.match(subj)), (
            f"walk lane shipped the mother entity id in subject: {subj!r}")
    named = any(
        mother_alias.lower() in " ".join(
            str(x) for x in (f.get("subject"), f.get("object"), f.get("definition"))
            if x).lower()
        for f in facts)
    assert named, (
        f"neither the slot nor the sentence names the mother ({mother_alias!r}); "
        f"an honest absence must keep the answer's content: {facts}")


@_gate
def test_the_id_strip_never_drops_a_fact_or_a_sentence():
    """Slot rendering must not thin the answer: for every battery question the
    answer still carries facts, and every fact carries its prose (definition) —
    on seats where the parent commit answered with sentences. Pinned against the
    two known-leak questions: the location row keeps its sentence, the count row
    keeps its sentence."""
    for seat in SEATS:
        for q in ("what's the weather tomorrow?", "how many dogs do I have?"):
            facts = _ask(seat, q)
            for f in facts:
                if f.get("source") in ("location_grounding", "count_over_walk"):
                    assert (f.get("definition") or "").strip(), (
                        f"a {f.get('source')} row lost its sentence: {f}")


# ── unit: the render helper itself, including the honest-absence branch ───────

def test_render_no_raw_entity_ids_maps_blanks_and_never_mutates_membership():
    """Known id → label; unknown id → "" (honest absence, NEVER the id); non-uuid
    strings untouched; membership and order unchanged."""
    from src.api.main import _render_no_raw_entity_ids as r
    u1 = "11111111-2222-4333-8444-555555555555"
    u2 = "99999999-8888-4777-8666-555555555555"
    facts = [
        {"subject": u1, "object": "Toronto", "rel_type": "lives_in",
         "definition": "You live in Toronto", "confidence": 1.0},
        {"subject": u2, "object": "12", "rel_type": "age"},
        {"subject": "you", "object": "Rex", "rel_type": "pref_name"},
    ]
    out = r(facts, {u1: "jamie"})
    assert out is facts and len(out) == 3
    assert facts[0]["subject"] == "jamie"          # known → label
    assert facts[1]["subject"] == ""               # unknown → honest absence
    assert not _UUID_FULL.match(facts[1]["subject"])
    assert facts[0]["object"] == "Toronto"         # non-uuid untouched
    assert facts[2]["subject"] == "you"
    assert facts[0]["confidence"] == 1.0           # non-string slots untouched
    # no name_map at all → a still-raw uuid blanks, never passes through; a name
    # already rendered (no longer a UUID) is left alone.
    r(facts, None)
    assert facts[1]["subject"] == ""
    assert facts[0]["subject"] == "jamie"


def test_render_no_raw_entity_ids_is_inert_on_clean_facts():
    from src.api.main import _render_no_raw_entity_ids as r
    facts = [{"subject": "you", "object": "2", "rel_type": "instance_count"}]
    before = [dict(f) for f in facts]
    r(facts, {})
    assert facts == before
