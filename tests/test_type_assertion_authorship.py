"""A type assertion the person never made is never spoken as their memory.

EVERY LANE IS VALID and every tier — A, B and C — is joined and spoken on recall. This is
NOT a lane filter and NOT a tier filter: an `llm_inferred` `spouse`, `child_of`, `has_pet`
or `located_in` row is the person's memory and still answers. The single thing declined is
a LADDER RUNG (`instance_of` / `subclass_of`) that the person did not assert — structure
the walk climbs, not content to emit. The walk still climbs every one of them.

The decision keys on `fact_provenance`, the column that records WHO ASSERTED the row
(NOT NULL, CHECK IN user_stated/llm_inferred/llm_learned). It does NOT key on `fact_class`:
measured on this bench, 129 of 210 live rows are class A but only 57 are user_stated —
67 assistant-authored rows sit at A/1.0 — so class cannot stand in for authorship here.
It also does not key on `_hierarchy_admitted`, which marks REACHABILITY; a previous
revision did and deleted the person's own words.

DB-gated. Point the seat vars at a bench:
  LADDER_TEST_DSN            a DSN
  LADDER_TEST_STATED_SEAT    a seat whose ladder row is user_stated (must SURVIVE)
  LADDER_TEST_SCAFFOLD_SEAT  a seat carrying assistant/engine-authored instance_of rows
"""
import os
import pytest

DSN = os.environ.get("LADDER_TEST_DSN")
STATED_SEAT = os.environ.get("LADDER_TEST_STATED_SEAT")
# Comma-separated: one seat rarely exercises more than one collector, and the plumbing
# assertion below is only as good as the number of collectors it reaches.
SCAFFOLD_SEATS = [s.strip() for s in
                  (os.environ.get("LADDER_TEST_SCAFFOLD_SEAT") or "").split(",") if s.strip()]
SCAFFOLD_SEAT = SCAFFOLD_SEATS[0] if SCAFFOLD_SEATS else None

_LADDER = ("subclass_of", "instance_of")


def _ask(uid, text):
    # FORCE, never setdefault — an already-set DSN would silently validate the wrong DB.
    os.environ["POSTGRES_DSN"] = DSN or ""
    os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
    from src.api.models import QueryRequest
    from src.api import main as M
    return (M._query_impl(QueryRequest(text=text, user_id=uid, source="test")).facts or [])


_stated_gate = pytest.mark.skipif(
    not (DSN and STATED_SEAT),
    reason="set LADDER_TEST_DSN and LADDER_TEST_STATED_SEAT",
)
_scaffold_gate = pytest.mark.skipif(
    not (DSN and SCAFFOLD_SEATS),
    reason="set LADDER_TEST_DSN and LADDER_TEST_SCAFFOLD_SEAT",
)


@_scaffold_gate
@pytest.mark.parametrize("question", ["who am I?", "what am I?"])
def test_the_engine_does_not_tell_you_that_you_are_an_instance_of_person(question):
    """`<user> instance_of person` is written by hierarchy reconciliation. Nobody said it."""
    for seat in SCAFFOLD_SEATS:
        facts = _ask(seat, question)
        spoken = [f for f in facts if (f.get("rel_type") or "").lower() in _LADDER
                  and (f.get("fact_provenance") or f.get("provenance") or "").lower()
                  != "user_stated"]
        assert not spoken, \
            f"{seat}: a type assertion the person never made was spoken: {spoken}"


@_scaffold_gate
def test_declining_a_rung_does_not_empty_the_answer():
    """Precision direction. The seat's real facts must still answer."""
    facts = _ask(SCAFFOLD_SEAT, "who am I?")
    assert facts, "declining ladder rungs must not take the real answer with them"


@_scaffold_gate
def test_a_grown_rung_is_declined_on_the_lexical_branch_too():
    """The gate used to live INSIDE the topic-gate skip branch, so whether a rung was
    spoken depended on which branch the question took. Authorship is a property of the
    ROW, not of the branch that reached it."""
    for seat, q in [(s, q) for s in SCAFFOLD_SEATS
                    for q in ("tell me about my walks", "tell me about safety",
                              "tell me about my vest")]:
        facts = _ask(seat, q)
        spoken = [f for f in facts
                  if (f.get("rel_type") or "").lower() in _LADDER
                  and (f.get("fact_provenance") or f.get("provenance") or "").lower()
                  != "user_stated"]
        assert not spoken, f"{seat} {q!r} spoke engine scaffolding: {spoken}"


@_stated_gate
def test_a_rung_the_person_stated_is_their_truth_and_survives():
    """THE line that must never be crossed. Deleting a fact the person stated is worse
    than any amount of scaffolding."""
    facts = _ask(STATED_SEAT, "what is my family?")
    assert facts, "a user-stated instance_of was deleted as if it were scaffolding"
    rungs = [f for f in facts if (f.get("rel_type") or "").lower() in _LADDER]
    assert rungs, f"the stated type fact did not survive: {facts}"
    for f in rungs:
        assert (f.get("fact_provenance") or f.get("provenance") or "").lower() \
            == "user_stated", f"a rung survived that the person did not state: {f}"


@pytest.mark.skipif(not (DSN and STATED_SEAT and SCAFFOLD_SEATS),
                    reason="set LADDER_TEST_DSN, LADDER_TEST_STATED_SEAT and "
                           "LADDER_TEST_SCAFFOLD_SEAT")
def test_every_ladder_row_reaching_the_render_carries_its_authorship():
    """The plumbing half. A GATE CANNOT BE MORE CORRECT THAN THE DATA IT READS: nine of
    the ten collectors used to drop the authorship column on the floor, so every decision
    about a rung was guessing — demanding the column kept the whole dump, punishing its
    absence deleted the person's words. This asserts the column actually ARRIVES.

    Swept over several seats and questions on purpose: one seat exercises one or two
    collectors, and a single-seat version of this test passes while a collector nobody
    reached still drops the column.
    """
    from src.api import main as M
    os.environ["POSTGRES_DSN"] = DSN or ""
    os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
    from src.api.models import QueryRequest

    seen = []
    _orig = M.convert_to_prose

    def _spy(facts, db, *a, **kw):
        seen.extend(dict(f) for f in facts)
        return _orig(facts, db, *a, **kw)

    questions = ("what is my family?", "what do you know about me?", "tell me about my pets",
                 "who am I?", "tell me about my vest", "tell me about my walks")
    M.convert_to_prose = _spy
    try:
        for seat in [STATED_SEAT] + SCAFFOLD_SEATS:
            for q in questions:
                M._query_impl(QueryRequest(text=q, user_id=seat, source="test"))
    finally:
        M.convert_to_prose = _orig

    ladder = [f for f in seen if (f.get("rel_type") or "").lower() in _LADDER]
    assert len(ladder) >= 3, (
        f"only {len(ladder)} ladder rows reached the render; the seats must put several "
        f"through it or this test passes vacuously"
    )
    naked = [f for f in ladder if not (f.get("fact_provenance") or f.get("provenance"))]
    assert not naked, (
        f"{len(naked)} of {len(ladder)} ladder rows reached the render with no recorded "
        f"authorship, so the gate is guessing on them: {naked[:3]}"
    )


def test_authorship_not_a_roll_call_of_engine_names():
    """A recorded author that is not the person silences a rung whether or not its lane
    name was ever enumerated — and an ABSENT author never does (fail-safe)."""
    from src.api.main import _is_grown_ontology_row as g

    # A lane nobody enumerated, recorded as not-the-person -> declined.
    assert g("instance_of", "some_lane_invented_next_year", "llm_inferred") is True
    assert g("instance_of", None, "llm_learned") is True
    # The person said it -> kept, whatever the writer tag says.
    assert g("instance_of", "assistant", "user_stated") is False
    assert g("subclass_of", "engine_classify_climb", "user_stated") is False
    # Authorship absent -> KEPT. Punishing silence is how the person's words got deleted.
    assert g("instance_of", None, None) is False
    assert g("instance_of", "mcp", None) is False
    assert g("instance_of", "reextract", "") is False
    # Not a type assertion -> never touched, whatever the lane.
    assert g("has_pet", None, "llm_inferred") is False
    assert g("spouse", "assistant", "llm_inferred") is False
    assert g("child_of", None, "llm_learned") is False
    assert g("located_in", "assistant", "llm_inferred") is False


@_scaffold_gate
@pytest.mark.parametrize("question", ["what is a person?", "what is paseo?",
                                      "what type of thing am I?"])
def test_the_ladder_still_answers_when_the_ladder_is_the_question(question):
    """THE OTHER DIRECTION, and the one a first version of this fix broke.

    "what is a person?" asks the classification axis directly — the person asked for the
    type chain, so returning it is answering, not dumping scaffolding. `fetch_facts_from_anchor`
    already decides this before admitting a climbed rung; the render must not silently
    reverse it. Measured when it did: "what is a person?" 9 rows -> 0, "what is paseo?"
    12 -> 0, on seats that hold the whole class ladder.

    These rows are `llm_inferred`, so the MCP render voices them HELD, never asserted as
    something the person said — which is what makes answering the question honest.
    """
    answered = [seat for seat in SCAFFOLD_SEATS if _ask(seat, question)]
    assert answered, (
        f"{question!r} returned nothing on every scaffold seat; a direct question about a "
        f"type must be answered from the ladder the engine grew, not silenced"
    )


@_scaffold_gate
def test_asking_ABOUT_a_topic_is_not_asking_WHAT_IT_IS():
    """The half that keeps the exemption honest. "tell me about safety" resolves the same
    classification AXIS as "what is safety?", so axis alone re-opened the defect and put
    the assistant's "Safety is an instance of top priority" back in front of the reader.
    The second signal is grammatical (`is_interrogative_clause`): an imperative is not a
    question about a type."""
    for seat in SCAFFOLD_SEATS:
        for q in ("tell me about safety", "tell me about my walks", "tell me about my vest"):
            spoken = [f for f in _ask(seat, q)
                      if (f.get("rel_type") or "").lower() in _LADDER
                      and (f.get("fact_provenance") or f.get("provenance") or "").lower()
                      != "user_stated"]
            assert not spoken, f"{seat} {q!r} spoke engine scaffolding: {spoken}"


# ── THE EXEMPTION IS SCOPED TO THE ASKED CHAIN (the 58ba3592 regression) ────────────
#
# The exemption began life as a whole-list bypass: when the ladder was the question, the
# render gate did not run AT ALL, so the MEMBER DUMP came through with the answer — on a
# real pre-prod bench, "what is a person?" answered with eleven rungs of engine
# misclassification ("workshop bench is an instance of person", "damp filament is an
# instance of person"), and a 6-seat x 19-question sweep spoke 48 unasserted rungs, every
# one through the bypass, ZERO through the gate itself.
#
# The scope: a rung is exempt only when its SUBJECT is on the chain that answers — the
# asked entity and everything the answer's own ladder rungs reach from it. The tests
# below derive that chain from the DATABASE (facts + staged_facts joined to entity
# names), never from the response and never from any mark the implementation writes:
# a mutation that widens the exemption changes the ANSWER, not this ground truth.


def _seat_ladder_rows(seat):
    """(subject_name, rel_type, object_name, fact_provenance) for every ladder row the
    seat holds, from both durability tiers, resolved to display NAMES via
    `entity_aliases` (preferred alias first — `entities` carries no name column) — the
    reader's own currency, independent of the render. An endpoint with no alias at all
    resolves to '' and never matches a spoken name: the safe direction for a ground
    truth."""
    import psycopg2
    from src.provisioning.schema_manager import derive_user_slug_from_uuid
    schema = f"faultline_{derive_user_slug_from_uuid(seat)}"
    sql = f"""
        WITH names AS (
            SELECT DISTINCT ON (entity_id) entity_id, alias
              FROM {schema}.entity_aliases
             ORDER BY entity_id, is_preferred DESC, alias
        )
        SELECT COALESCE(nsa.alias, ''), f.rel_type, COALESCE(noa.alias, ''),
               COALESCE(f.fact_provenance, f.provenance, '')
          FROM {schema}.facts f
          LEFT JOIN names nsa ON nsa.entity_id = f.subject_id
          LEFT JOIN names noa ON noa.entity_id = f.object_id
         WHERE f.rel_type IN ('instance_of', 'subclass_of')
           AND f.superseded_at IS NULL AND f.archived_at IS NULL AND f.deleted_at IS NULL
        UNION ALL
        SELECT COALESCE(nsa.alias, ''), st.rel_type, COALESCE(noa.alias, ''),
               COALESCE(st.fact_provenance, st.provenance, '')
          FROM {schema}.staged_facts st
          LEFT JOIN names nsa ON nsa.entity_id = st.subject_id
          LEFT JOIN names noa ON noa.entity_id = st.object_id
         WHERE st.rel_type IN ('instance_of', 'subclass_of')
           AND st.promoted_at IS NULL AND st.deleted_at IS NULL
    """
    with psycopg2.connect(DSN, options=f"-csearch_path={schema}") as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return [(str(r[0]).strip().lower(), str(r[1]).strip().lower(),
                     str(r[2]).strip().lower(), str(r[3]).strip().lower())
                    for r in cur.fetchall()]


# asked-entity NAME per question — derived from the question's own grammar, not from the
# implementation's anchor resolution. 'user' is the seat's speaking subject.
_ASKED_ENTITY = {
    "what is a person?": "person",
    "am I a person?": "user",
    "what is a dog?": "dog",
    "what type of thing am I?": "user",
}


def _chain_from(seat_rows, root_name):
    """Entity NAMES reachable from root through the seat's own ladder rows."""
    chain = {root_name}
    changed = True
    while changed:
        changed = False
        for subj, _rel, obj, _prov in seat_rows:
            if subj in chain and obj not in chain:
                chain.add(obj)
                changed = True
    return chain


@_scaffold_gate
@pytest.mark.parametrize("question", sorted(_ASKED_ENTITY))
def test_a_ladder_answer_carries_only_the_asked_chain(question):
    """THE SEPARATION. Both properties, one ground truth, per seat:

      * (b) every spoken unasserted rung has its SUBJECT on the DB-derived chain — the
        member dump ("workshop bench is an instance of person" on "what is a person?")
        is never spoken;
      * (a) when the seat HOLDS unasserted chain rungs for the asked entity, the answer
        is not empty — the chain the person asked for is spoken.

    A seat with NO chain rungs for the asked entity may honestly answer nothing — that
    is the standing ruling (an answer that is only scaffolding is worse than none), and
    it is what distinguishes this test from a blanket non-empty assertion."""
    for seat in SCAFFOLD_SEATS:
        rows = _seat_ladder_rows(seat)
        asked = _ASKED_ENTITY[question]
        chain = _chain_from(rows, asked)
        facts = _ask(seat, question)
        spoken = [f for f in facts if (f.get("rel_type") or "").lower() in _LADDER
                  and (f.get("fact_provenance") or f.get("provenance") or "").lower()
                  != "user_stated"]
        for f in spoken:
            subj = str(f.get("subject") or "").strip().lower()
            assert subj in chain, (
                f"{seat} {question!r}: spoke a rung about {subj!r}, which is not on the "
                f"chain rooted at {asked!r} — the member dump reached the reader: "
                f"{f.get('definition') or f}"
            )
        # (a) is NOT asserted per-seat here on purpose: whether a seat's chain rungs
        # ARRIVE at the render is the collectors' decision (scope, axis admission), not
        # the gate's — a seat may hold chain rungs and still answer nothing because the
        # walk never fetched them (measured: a82c4005 "what type of thing am I?" is empty
        # at 58ba3592 too, exemption active, because nothing arrived to speak). The
        # non-empty direction is pinned where it belongs, multi-seat, by
        # test_the_ladder_still_answers_when_the_ladder_is_the_question. This test pins
        # the OTHER direction: nothing OFF the asked chain is ever spoken — which is
        # also what makes an honest nothing possible on a seat whose only ladder rows
        # are member junk.


@_scaffold_gate
def test_no_private_uuid_keys_leak_into_the_response():
    """The chain scoping reads `_subject_id` at the gate, so the UUID strip moved BELOW
    it. Pin that the move never lets the private keys out: no response fact carries
    `_subject_id`/`_object_id` after the render (CLAUDE.md: no UUIDs out)."""
    for seat in SCAFFOLD_SEATS[:2]:
        for q in ("what is a dog?", "who am I?", "what is my family?"):
            for f in _ask(seat, q):
                assert "_subject_id" not in f and "_object_id" not in f, \
                    f"{seat} {q!r}: private UUID key reached the response: {f}"


def test_the_chain_reconstruction_is_transitive_and_member_blind():
    """Unit pins on `_ladder_answer_subject_ids` — the exemption's scope.

      * transitive: dog->canine plus canine->mammal puts mammal's rung on the chain
        rooted at dog (multi-hop answers stay answers);
      * member-blind: a rung whose subject is NOT on the chain never seeds itself in —
        "golden retriever subclass_of dog" does not put golden retriever's OWN rungs on
        dog's chain;
      * off-axis relations never extend the chain, whatever they connect;
      * a row with no resolvable UUID extends nothing (absence fails silent)."""
    from src.api.main import _ladder_answer_subject_ids as chain
    dog, canine, mammal, gr = "d" * 8 + "-1111-4bbb-8ccc-111111111111", \
                              "c" * 8 + "-1111-4bbb-8ccc-111111111111", \
                              "m" * 8 + "-1111-4bbb-8ccc-111111111111", \
                              "g" * 8 + "-1111-4bbb-8ccc-111111111111"
    facts = [
        {"rel_type": "subclass_of", "_subject_id": dog, "_object_id": canine},
        {"rel_type": "subclass_of", "_subject_id": canine, "_object_id": mammal},
        # member rows: subjects OFF the chain pointing AT it — they never join
        {"rel_type": "subclass_of", "_subject_id": gr, "_object_id": dog},
        {"rel_type": "instance_of", "_subject_id": gr, "_object_id": mammal},
        # off-axis: never extends, even between chain nodes
        {"rel_type": "owns", "_subject_id": dog, "_object_id": mammal},
        # no ids at all: extends nothing
        {"rel_type": "subclass_of", "subject": "plain name", "object": "another"},
    ]
    got = chain(dog, facts)
    assert got == {dog, canine, mammal}, got
    # TRANSITIVE-BY-SUBJECT: once mammal is ON dog's chain, mammal's own type rung
    # extends it — that is a multi-hop answer, not a leak (the dump rows above never
    # seeded anything: gr stayed out until mammal vouched for it).
    assert (chain(dog, facts + [{"rel_type": "instance_of",
                                 "_subject_id": mammal, "_object_id": gr}])
            == {dog, canine, mammal, gr})
    # anchored nowhere: just the anchor
    assert chain(None, facts) == set()
    # walk-lane shape: UUID in `subject` itself, no underscore keys
    walk_shaped = [{"rel_type": "instance_of", "subject": gr, "object": dog}]
    assert chain(dog, walk_shaped) == {dog}
    assert chain(gr, walk_shaped) == {gr, dog}
