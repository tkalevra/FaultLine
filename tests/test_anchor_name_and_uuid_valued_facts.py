"""Two owner rulings pinned (2026-08-20 closing round):

1. ADDITIVE ``anchor_name`` on every /query answer. ``QueryResponse.anchor`` and
   ``canonical_identity`` KEEP the raw entity UUID (consuming software keys on it);
   a human-readable name field is ADDED alongside, sourced from the store's alias
   tables (``entity_aliases``, preferred-first; the user-identity lane for the seat
   owner) — never an enumerated list, never an invented placeholder. Unnameable →
   None (honest absence).

2. A UUID-shaped FACT VALUE the person WANTS remembered is CONTENT, not an internal
   handle. The render pass may rewrite only identifiers that RESOLVE TO KNOWN
   ENTITIES (store membership in ``entities``); anything else is the person's data
   and must survive VERBATIM through query and recall. The discriminator is store
   membership, not shape.

Ground truth is the STORE, never the render: the expected name set for an anchor is
read from ``entity_aliases`` directly; the expected surviving value is the row we
stated on a real bench seat. Gated on LADDER_TEST_DSN + LADDER_TEST_SCAFFOLD_SEAT
(comma list) so CI without a database skips rather than lying; a green run with
skips proves nothing — check "0 skipped".
"""
import os
import re
import uuid as _uuid

import psycopg2
try:
    import pytest
except ImportError:  # pytest-free critic runner (fltools/run_suite.py pattern)
    class pytest:  # type: ignore
        class mark:
            @staticmethod
            def skipif(*a, **k):
                return lambda f: f


DSN = os.environ.get("LADDER_TEST_DSN")
SEATS = [s for s in (os.environ.get("LADDER_TEST_SCAFFOLD_SEAT") or "").split(",") if s]

_gate = pytest.mark.skipif(
    not (DSN and SEATS),
    reason="set LADDER_TEST_DSN and LADDER_TEST_SCAFFOLD_SEAT (comma list) to run these",
)

_UUID_FULL = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)

# The stated-value probe: a UUID-shaped value that is NOT an entity in any seat's
# store (asserted against the store before use). If a person states this as a fact
# value, it is data, and it must come back byte-identical.
_PROBE_ATTRIBUTE = "server_id"

_ASK_UUID_VALUE = [
    "what is my server id?",
    "tell me about my server id",
    "tell me everything about me",
    "what do you know about my server id?",
]

_ASK_ANCHOR = [
    "tell me about my family",
    "what is my mother's name?",
    "what is my dog's name?",
    "who am I?",
]


def _db(readonly=True):
    conn = psycopg2.connect(DSN)
    conn.set_session(autocommit=True, readonly=readonly)
    return conn


def _schema(uid: str) -> str:
    return "faultline_" + uid.replace("-", "_")


def _ask_response(uid, text):
    """Drive the REAL query entrypoint and return the full QueryResponse."""
    os.environ["POSTGRES_DSN"] = DSN or ""
    os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
    from src.api.models import QueryRequest
    from src.api import main as M
    return M._query_impl(QueryRequest(text=text, user_id=uid, source="test"))


def _aliases_of(schema, entity_id):
    # BOTH forms are store ground truth: the raw lowercase alias AND the retained
    # casing display_form — a name sourced from either is store-sourced.
    with _db() as c, c.cursor() as cur:
        cur.execute(
            f"SELECT alias, COALESCE(display_form, alias) FROM {schema}.entity_aliases "
            f"WHERE entity_id = %s", (entity_id,))
        out = set()
        for raw, disp in cur.fetchall():
            if raw:
                out.add(raw)
            if disp:
                out.add(disp)
        return out


def _is_store_entity(schema, value):
    with _db() as c, c.cursor() as cur:
        cur.execute(f"SELECT 1 FROM {schema}.entities WHERE id = %s", (value,))
        return cur.fetchone() is not None


def _letter_start_uuid() -> str:
    """A UUID-shaped probe GUARANTEED to start with a hex letter.

    The prose truecase pass titles leading letters; a digit-start UUID is
    invisible to the case-corruption detector (uuid4() draws a letter only
    6/16 of the time, which made the pin probabilistic — round-4 critic
    demonstrated a green truecase-revert mutation on a digit-start draw).
    Every probe in this suite is letter-start: the pin is deterministic.
    """
    return "a" + str(_uuid.uuid4())[1:]


def _state_uuid_valued_fact(seat, value):
    """STATE a UUID-valued fact on a real seat, store-side (user_stated scalar on
    the seat owner's own entity). Idempotent; returns nothing. Cleanup pairs with
    _cleanup_stated_fact."""
    schema = _schema(seat)
    with _db(readonly=False) as c, c.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema}.entities (id, entity_type) VALUES (%s, 'person') "
            f"ON CONFLICT (id) DO NOTHING", (seat,))
        cur.execute(
            f"DELETE FROM {schema}.entity_attributes "
            f"WHERE entity_id = %s AND attribute = %s", (seat, _PROBE_ATTRIBUTE))
        cur.execute(
            f"INSERT INTO {schema}.entity_attributes "
            f"(user_id, entity_id, attribute, value_text, provenance, datatype) "
            f"VALUES (%s, %s, %s, %s, 'user_stated', 'uuid')",
            (seat, seat, _PROBE_ATTRIBUTE, value))


def _cleanup_stated_fact(seat):
    schema = _schema(seat)
    with _db(readonly=False) as c, c.cursor() as cur:
        cur.execute(
            f"DELETE FROM {schema}.entity_attributes "
            f"WHERE entity_id = %s AND attribute = %s", (seat, _PROBE_ATTRIBUTE))


# ── ruling 2: a stated UUID-valued fact survives byte-identical ────────────────

@_gate
def test_stated_uuid_valued_fact_returns_untouched():
    """A UUID the person WANTS remembered is content. State a UUID-valued fact on a
    real seat, drive the real query entrypoint until it surfaces, and assert the
    value returns VERBATIM in the fact's slots (byte-identical, not blanked, not
    rewritten). The discriminator is store membership: the probe UUID is proven
    NOT to be an entity before use."""
    seat = SEATS[0]
    probe = _letter_start_uuid()
    schema = _schema(seat)
    # the probe must be exactly what the ruling separates: UUID-SHAPED but NOT a
    # store entity. (If it somehow collides, pick another.)
    while _is_store_entity(schema, probe):
        probe = _letter_start_uuid()
    _state_uuid_valued_fact(seat, probe)
    try:
        surfaced = False
        for q in _ASK_UUID_VALUE:
            resp = _ask_response(seat, q)
            for f in (resp.facts or []):
                for k, v in f.items():
                    if isinstance(v, str) and v.strip().lower() == probe:
                        surfaced = True
                        assert v == probe, (
                            f"stated UUID value was MUTATED on return: slot={k} "
                            f"got={v!r} want={probe!r} q={q!r}")
                    elif isinstance(v, str) and probe[:13] in v:
                        # embedded (e.g. the definition sentence) — must still carry
                        # the FULL value verbatim inside it
                        surfaced = True
                        assert probe in v, (
                            f"stated UUID value was CORRUPTED on return: slot={k} "
                            f"got={v!r} want={probe!r} q={q!r}")
                    elif isinstance(v, str) and probe[:13].lower() in v.lower():
                        # case-corruption detector: the value is present (case-
                        # insensitively) but NOT verbatim — e.g. a truecasing pass
                        # titled its first hex digit ("a906…" → "A906…"). Fires only
                        # because every probe is LETTER-start (digits cannot be
                        # re-cased by title()); the previous form compared an
                        # UPPERCASED probe against a lowercased value, which could
                        # never match — dead code the round-4 M5 mutant slipped past.
                        raise AssertionError(
                            f"stated UUID value was CASE-CORRUPTED on return: slot={k} "
                            f"got={v!r} want={probe!r} q={q!r}")
        assert surfaced, (
            "the stated UUID-valued fact never surfaced — the pin is vacuous; "
            "check the seat list and the surfacing phrasings")
    finally:
        _cleanup_stated_fact(seat)


@_gate
def test_render_membership_discriminator_is_store_membership_not_shape():
    """Unit pin on the render pass with a REAL store cursor: a UUID that IS a
    store entity with no name in the map → honest absence (""); a UUID-shaped
    value that is NOT a store entity → VERBATIM (person data); a mapped entity →
    its label."""
    from src.api.main import _render_no_raw_entity_ids as r
    seat = SEATS[0]
    schema = _schema(seat)
    # a REAL store entity WITH an alias, and one WITHOUT any alias
    with _db() as c, c.cursor() as cur:
        cur.execute(
            f"SELECT e.id FROM {schema}.entities e "
            f"JOIN {schema}.entity_aliases a ON a.entity_id = e.id "
            f"WHERE e.id != %s LIMIT 1", (seat,))
        named = cur.fetchone()
        cur.execute(
            f"SELECT e.id FROM {schema}.entities e "
            f"LEFT JOIN {schema}.entity_aliases a ON a.entity_id = e.id "
            f"WHERE a.id IS NULL AND e.id != %s LIMIT 1", (seat,))
        unnamed = cur.fetchone()
    assert named, "no aliased non-user entity on the seat; fixture drifted"
    named_id = named[0]
    person_data = _letter_start_uuid()
    while _is_store_entity(schema, person_data):
        person_data = _letter_start_uuid()
    conn = psycopg2.connect(DSN)
    conn.set_session(autocommit=True, readonly=False)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            facts = [
                {"subject": named_id, "object": "x", "rel_type": "r"},
                {"subject": person_data, "object": "y", "rel_type": "r"},
            ]
            r(facts, {}, db=conn)
        # NOT a store entity → the person's data: VERBATIM
        assert facts[1]["subject"] == person_data, (
            f"non-entity UUID value was not kept verbatim: {facts[1]['subject']!r}")
        # IS a store entity, unnameable from the map → honest absence, never the id
        if unnamed:
            facts2 = [{"subject": unnamed[0], "object": "y", "rel_type": "r"}]
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}")
                r(facts2, {}, db=conn)
            assert facts2[0]["subject"] == "", (
                f"unnamed store entity must render as absence, got "
                f"{facts2[0]['subject']!r}")
        # IS a store entity, name in the map → the label
        facts3 = [{"subject": named_id, "object": "x", "rel_type": "r"}]
        r(facts3, {named_id: "label"}, db=None)  # map hit needs no membership probe
        assert facts3[0]["subject"] == "label"
    finally:
        conn.close()


# ── ruling 2, MCP door: the recall shaping layer keeps person data too ────────

@_gate
def test_mcp_clean_keeps_stated_uuid_strips_only_store_entities():
    """The MCP recall shaping layer (_clean_for_mcp) has the same sharp edge the
    backend render pass had: a shape-based UUID strip. Pinned with the STORE as
    oracle: a UUID-shaped value that is NOT a store entity survives verbatim; a
    UUID that IS a store entity is stripped; no user_id → legacy strip."""
    seat = SEATS[0]
    schema = _schema(seat)
    # a REAL store entity id to embed (must be stripped)
    with _db() as c, c.cursor() as cur:
        cur.execute(
            f"SELECT e.id FROM {schema}.entities e "
            f"JOIN {schema}.entity_aliases a ON a.entity_id = e.id "
            f"WHERE e.id != %s LIMIT 1", (seat,))
        named = cur.fetchone()
    assert named, "no aliased non-user entity on the seat; fixture drifted"
    entity_id = named[0]
    person_data = _letter_start_uuid()
    while _is_store_entity(schema, person_data):
        person_data = _letter_start_uuid()
    from src.mcp.server import _clean_for_mcp
    text_person = f"you server id {person_data}"
    text_entity = f"the node {entity_id} here"
    out_person = _clean_for_mcp(text_person, user_id=seat)
    out_entity = _clean_for_mcp(text_entity, user_id=seat)
    assert person_data in out_person, (
        f"MCP shaping destroyed a stated UUID value: {out_person!r}")
    assert entity_id not in out_entity, (
        f"MCP shaping passed a store entity id through: {out_entity!r}")
    # no seat context → legacy shape strip still applies (cortex-note callers)
    assert person_data not in _clean_for_mcp(text_person)


# ── ruling 1: anchor_name populated from the store, UUID unchanged ─────────────

@_gate
def test_anchor_name_store_sourced_and_uuid_field_unchanged():
    """Across bench seats and battery questions: `anchor`/`canonical_identity`
    still carry the raw UUID (or user_id) — unchanged; `anchor_name`, when the
    anchor is a known entity, is one of that entity's STORED display aliases
    (ground truth from entity_aliases), and when the anchor has no stored alias
    it renders as an honest absence (None) — never a UUID, never an invented
    placeholder."""
    checked_named = 0
    checked_absent = 0
    for seat in SEATS:
        schema = _schema(seat)
        for q in _ASK_ANCHOR:
            resp = _ask_response(seat, q)
            a = resp.anchor
            assert a == resp.canonical_identity, (
                f"anchor/canonical_identity diverged: {a!r} vs "
                f"{resp.canonical_identity!r}")
            if not a:
                assert resp.anchor_name is None, (
                    f"empty anchor must render anchor_name=None, got "
                    f"{resp.anchor_name!r}")
                continue
            # THE UUID FIELD IS UNCHANGED: still the raw identifier the consumer
            # keys on — never the display name.
            assert a == seat or _UUID_FULL.match(a), (
                f"anchor is no longer a raw identifier/user id: {a!r}")
            if resp.anchor_name is not None:
                assert resp.anchor_name != a, "anchor_name must be a NAME, not the id"
            names = _aliases_of(schema, a)
            is_entity = a == seat or _is_store_entity(schema, a)
            if names:
                # known, named anchor → the name must come FROM THE STORE
                assert resp.anchor_name in names, (
                    f"anchor_name {resp.anchor_name!r} is not any stored alias of "
                    f"anchor {a[:8]}… (store has {sorted(names)}) — the field must "
                    f"be store-sourced, never invented")
                assert not _UUID_FULL.match(resp.anchor_name), (
                    f"anchor_name is a raw UUID: {resp.anchor_name!r}")
                checked_named += 1
            elif is_entity:
                # known entity with NO alias → honest absence, never a guess
                assert resp.anchor_name is None, (
                    f"anchor {a[:8]}… has no stored alias but anchor_name="
                    f"{resp.anchor_name!r} — unnameable must render as absence")
                checked_absent += 1
    assert checked_named >= 3, (
        f"only {checked_named} named-anchor answers checked — the pin must "
        f"exercise the named path (check 0 skipped and the seat list)")
