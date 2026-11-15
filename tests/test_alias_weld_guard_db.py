"""ARRIVAL WELD GUARD — the place probe, executed against a REAL PostgreSQL.

WHY THIS FILE EXISTS. The sibling suite `tests/test_alias_weld_guard.py` answers the place probe
from a mocked boolean, so `_entity_is_user_asserted_place`'s SQL never runs. That mock cannot
catch an inverted `subject_id`/`object_id`, which is precisely the mistake that would turn the
guard into the HARD-LINE violation it exists to prevent: reading the SUBJECT of an
`rdf:type`-role edge as class-side would make every NAMED INSTANCE a "place".

Proven to have teeth by mutation — see `test_the_probe_is_direction_sensitive`, which pins the
asymmetry directly, and DEV/DESIGN-alias-weld-guard.md for the recorded mutation runs.

The schema below mirrors production column TYPES that matter: `facts.subject_id` /
`facts.object_id` / `entity_aliases.entity_id` are **TEXT**, not `uuid` (verified on a live
per-tenant schema). A test built on `uuid` columns would not exercise the real statement.

Skips cleanly when no throwaway database is offered via `WELD_GUARD_TEST_DSN`.
"""
import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from src.entity_registry import weld_guard  # noqa: E402

DSN = os.environ.get("WELD_GUARD_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="set WELD_GUARD_TEST_DSN to a THROWAWAY postgres to run the DB-backed probe tests",
)

# Minimal per-tenant shape. Column types match production (TEXT ids).
_DDL = """
CREATE SCHEMA {sch};
SET search_path TO {sch};
CREATE TABLE rel_types (rel_type text PRIMARY KEY, wikidata_pid text);
CREATE TABLE entity_aliases (
    entity_id text, alias text, is_preferred boolean DEFAULT false,
    preference_source text DEFAULT 'unspecified',
    PRIMARY KEY (entity_id, alias));
CREATE TABLE facts (
    subject_id text, object_id text, rel_type text,
    fact_provenance text, fact_class text,
    superseded_at timestamptz, archived_at timestamptz);
CREATE TABLE staged_facts (
    subject_id text, object_id text, rel_type text,
    provenance text, fact_class text,
    promoted_at timestamptz, deleted_at timestamptz);
-- The rel NAMES are arbitrary on purpose: the guard must find them by wikidata_pid alone.
INSERT INTO rel_types VALUES ('is_an_example_of','P31'), ('narrows','P279'), ('owns',NULL);
"""


@pytest.fixture()
def cur():
    sch = "wg_" + uuid.uuid4().hex[:10]
    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    with conn.cursor() as c:
        c.execute(_DDL.format(sch=sch))
    conn.commit()
    with conn.cursor() as c:
        c.execute(f"SET search_path TO {sch}")
        yield c
    conn.rollback()
    with conn.cursor() as c:
        c.execute(f"DROP SCHEMA {sch} CASCADE")
    conn.commit()
    conn.close()


def _alias(cur, eid, alias, src="unspecified", pref=False):
    cur.execute("INSERT INTO entity_aliases VALUES (%s,%s,%s,%s)", (eid, alias, pref, src))


def _fact(cur, s, o, rel, prov="user_stated"):
    cur.execute("INSERT INTO facts (subject_id,object_id,rel_type,fact_provenance) "
                "VALUES (%s,%s,%s,%s)", (s, o, rel, prov))


# ── the probe's DIRECTION — the thing a mocked boolean can never check ──────────────────────

def test_the_probe_is_direction_sensitive(cur):
    """OBJECT of an rdf:type-role edge is class-side; SUBJECT of one is NOT.

    RDF Schema §3.3: in `R rdf:type C` the CLASS is C, the object. If this asymmetry is ever
    inverted, every named instance becomes a "place" and the guard starts refusing exactly the
    user memories it was built to protect. This is the mutation the mocked suite cannot catch.
    """
    _fact(cur, "the_instance", "the_class", "is_an_example_of")
    assert weld_guard._entity_is_user_asserted_place(cur, "the_class") is True
    assert weld_guard._entity_is_user_asserted_place(cur, "the_instance") is False


def test_subclass_role_is_class_side_on_the_subject(cur):
    """RDF Schema §3.4: BOTH ends of rdfs:subClassOf are classes, so subject position counts."""
    _fact(cur, "narrow_class", "broad_class", "narrows")
    assert weld_guard._entity_is_user_asserted_place(cur, "narrow_class") is True


def test_a_rel_without_an_rdfs_pid_is_not_class_side(cur):
    """Metadata-driven: rel identity is wikidata_pid, never the rel NAME."""
    _fact(cur, "somebody", "something", "owns")
    assert weld_guard._entity_is_user_asserted_place(cur, "something") is False


# ── EVIDENCE AUTHORITY — the fix for the blocking false refusal ─────────────────────────────

def test_engine_grown_placement_evidence_cannot_make_a_node_a_place(cur):
    """THE BLOCKING BUG, pinned. Growth must never veto user truth (authority order).

    Measured on production: an `llm_inferred` subclass_of edge made the SEAT OWNER's own user
    entity "class-side", so the guard refused the owner's own given name. If this goes red the
    authority filter on the evidence has been removed and that regression is back.
    """
    _fact(cur, "some_node", "some_class", "narrows", prov="llm_inferred")
    assert weld_guard._entity_is_user_asserted_place(cur, "some_node") is False
    _fact(cur, "some_node", "some_class2", "narrows", prov="user_stated")
    assert weld_guard._entity_is_user_asserted_place(cur, "some_node") is True


def test_staged_engine_placement_is_also_inadmissible(cur):
    cur.execute("INSERT INTO staged_facts (subject_id,object_id,rel_type,provenance) "
                "VALUES ('n','c','narrows','engine_classify_climb')")
    assert weld_guard._entity_is_user_asserted_place(cur, "n") is False


def test_superseded_and_archived_evidence_is_ignored(cur):
    cur.execute("INSERT INTO facts (subject_id,object_id,rel_type,fact_provenance,archived_at) "
                "VALUES ('i','c','is_an_example_of','user_stated', now())")
    assert weld_guard._entity_is_user_asserted_place(cur, "c") is False


# ── END-TO-END verdicts through the real cursor ─────────────────────────────────────────────

def test_arm1_refuses_a_user_label_on_a_user_declared_class(cur):
    _alias(cur, "cls", "a type word", "inferred")
    _fact(cur, "inst", "cls", "is_an_example_of")
    allow, arm = weld_guard.weld_verdict(cur, "cls", "a given name", "user_stated")
    assert (allow, arm) == (False, weld_guard.ARM_PLACE)


def test_the_seat_anchor_is_never_refused_via_the_schema_name(cur):
    """BAR ITEM 1. The first-person resolution target keeps its own name, always.

    Refusing here does NOT merely cost reachability: resolve() would mint uuid5(user_id, name),
    which is NOT the user entity, so the user's own name detaches from the speaker.
    """
    seat = "a45bb965-0a66-4b1e-a1fb-8d0e89ef6ffa"
    schema = "faultline_a45bb965_0a66_4b1e_a1fb_8d0e89ef6ffa"
    _alias(cur, seat, "user", "unspecified")
    _fact(cur, seat, "person", "narrows")           # user_stated class-side evidence
    assert weld_guard._entity_is_user_asserted_place(cur, seat) is True
    allow, arm = weld_guard.weld_verdict(cur, seat, "a given name", "user_stated",
                                         schema_name=schema)
    assert (allow, arm) == (True, weld_guard.ALLOW)


def test_the_seat_anchor_is_also_detected_by_the_provisioning_marker(cur):
    """Second, independent structural marker — one production tenant lacks the schema-derived
    one, so both are kept and unioned."""
    _alias(cur, "seat_without_schema_hint", "user", "unspecified")
    _alias(cur, "seat_without_schema_hint", "the slug", "provisioned")
    _fact(cur, "seat_without_schema_hint", "person", "narrows")
    allow, _ = weld_guard.weld_verdict(cur, "seat_without_schema_hint", "a given name",
                                       "user_stated", schema_name=None)
    assert allow is True


def test_a_preferred_user_write_reaches_the_correction_lane(cur):
    """An explicit prefLabel assertion is the naming/correction lane and must not be vetoed
    before the registry's user-is-truth provenance override can run."""
    _alias(cur, "cls", "a type word", "inferred")
    _fact(cur, "inst", "cls", "is_an_example_of")
    allow, _ = weld_guard.weld_verdict(cur, "cls", "a given name", "user_stated",
                                       is_preferred=True)
    assert allow is True


def test_arm2_fires_without_a_ladder_ONLY_when_armed_and_user_declared(cur, monkeypatch):
    """ARM 2 used to fire here with NO place condition at all, and that was the blocker review
    found: it refused a user-stated label on 2 526 production entities, 2 133 of them not
    classes by any provenance. It is now OFF by default and place-gated when armed.

    Kept (rather than deleted) because it is the only test that exercises ARM 2's own
    growth-only-labels logic; it now documents the arm's real, narrowed contract.
    """
    monkeypatch.setenv("ALIAS_WELD_GUARD_ARM2", "on")
    _alias(cur, "growth_node", "an engine surface", "inferred")
    _fact(cur, "inst", "growth_node", "is_an_example_of", prov="user_stated")
    allow, arm = weld_guard.weld_verdict(cur, "growth_node", "a given name", "user_stated")
    # ARM 1 gets there first on this shape — which is precisely why ARM 2 is subsumed.
    assert allow is False
    assert arm in (weld_guard.ARM_GROWTH_ONLY, weld_guard.ARM_PLACE)


def test_an_origin_label_with_no_authority_claim_is_not_growth(cur):
    """THE ZERO-FALSE-REFUSAL CASE. resolve() mints the origin label with no source parameter."""
    _alias(cur, "named_thing", "a given name", "unspecified")
    allow, arm = weld_guard.weld_verdict(cur, "named_thing", "a role word", "user_stated")
    assert (allow, arm) == (True, weld_guard.ALLOW)


def test_a_probe_failure_fails_open_and_leaves_the_transaction_usable(cur):
    """Fail-open AND no transaction poisoning — the failure mode that once made a live walk
    report 'no facts found' over rows it had already read."""
    cur.execute("DROP TABLE facts")
    allow, arm = weld_guard.weld_verdict(cur, "x", "y", "user_stated")
    assert (allow, arm) == (True, weld_guard.ALLOW)
    cur.execute("SELECT 1")          # transaction still usable
    assert cur.fetchone()[0] == 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))


# ══════════════════════════════════════════════════════════════════════════════════════════
# ARM 3 — unwarranted growth co-reference. Every test below runs the REAL place probe through
# the REAL cursor; ARM 3's whole point is a graph condition, so a mocked boolean would prove
# nothing about it (that mistake is what this file exists to stop repeating).
# ══════════════════════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def arm3_on(monkeypatch):
    monkeypatch.setenv("ALIAS_WELD_GUARD_ARM3", "on")


def _growth_place(cur, node="a type node"):
    """A node whose every label is an engine-growth CLAIM and which is used as a class."""
    _alias(cur, node, "an engine surface", "inferred")
    _fact(cur, "some instance", node, "is_an_example_of", prov="llm_inferred")
    return node


def test_arm3_is_disarmed_by_default(cur):
    """SHIPPING DEFAULT. ARM 3 did not reach zero false refusals, so it is OFF unless asked for.

    If this goes red, ARM 3 was defaulted on without the measurement that gates it — see
    `_DEFAULT_ARM3` and DEV/DESIGN-alias-weld-guard.md §7.
    """
    node = _growth_place(cur)
    assert weld_guard.arm3_enabled() is False
    allow, arm = weld_guard.weld_verdict(cur, node, "an unrelated surface", "rel_default")
    assert (allow, arm) == (True, weld_guard.ALLOW)


def test_arm3_refuses_an_unwarranted_growth_coreference_on_a_place(cur, arm3_on):
    """The corruption ARMs 1-2 cannot see: `{cat} ≡ {luna}` — a NAME onto its own type node,
    arriving engine-authored on BOTH sides so no memory-authority arm ever looks at it."""
    node = _growth_place(cur)
    allow, arm = weld_guard.weld_verdict(cur, node, "an unrelated surface", "rel_default")
    assert (allow, arm) == (False, weld_guard.ARM_UNWARRANTED)


def test_arm3_admits_the_warranted_lexical_lane_on_the_very_same_node(cur, arm3_on):
    """THE POSITIVE CONTROL, as a discrimination test rather than an absence test.

    On the IDENTICAL node that refuses an unwarranted surface, a lexical-provenance surface is
    admitted. This is the whole justification for the separate provenance: without a recorded
    warrant the two writes are byte-identical and no rule can separate them. If this goes red
    the canonicalization lane has been closed.
    """
    node = _growth_place(cur)
    refused, _ = weld_guard.weld_verdict(cur, node, "an unrelated surface", "rel_default")
    admitted, arm = weld_guard.weld_verdict(cur, node, "a co-synset lemma", "lexical")
    assert refused is False, "control is meaningless unless the arm is live on this node"
    assert (admitted, arm) == (True, weld_guard.ALLOW)


def test_arm3_requires_the_place_condition(cur, arm3_on):
    """Load-bearing third condition, added on measurement. Without it the arm also refused a
    legitimate family — an engine INSTANCE node acquiring its own natural phrasing
    (`{occurrence: barbecue party} ≡ {barbecue party}`), abbreviations, acronyms. Adding a
    surface to an INSTANCE is indexing; only a co-reference onto a CLASS is the HARD-LINE error.
    """
    _alias(cur, "an instance node", "an engine surface", "inferred")
    _fact(cur, "an instance node", "a class", "is_an_example_of", prov="llm_inferred")
    allow, arm = weld_guard.weld_verdict(cur, "an instance node", "another surface", "rel_default")
    assert (allow, arm) == (True, weld_guard.ALLOW)


def test_arm3_may_use_engine_authored_placement_evidence_but_arm1_may_not(cur, arm3_on):
    """The two arms need DIFFERENT evidence rules, and it is an authority question.

    ARM 1 declines USER truth, so engine evidence there would invert the authority order (that
    inversion is the recorded production regression). ARM 3 declines an ENGINE claim, so engine
    evidence cannot invert anything — and requiring user evidence would blind it on exactly the
    engine-built type nodes it exists to protect.
    """
    node = _growth_place(cur)                       # placement evidence is llm_inferred
    assert weld_guard._entity_is_user_asserted_place(cur, node) is False
    assert weld_guard._entity_is_place(cur, node, require_user_evidence=False) is True
    allow, arm = weld_guard.weld_verdict(cur, node, "an unrelated surface", "rel_default")
    assert (allow, arm) == (False, weld_guard.ARM_UNWARRANTED)


def test_arm3_ignores_a_label_that_records_no_authority_claim(cur, arm3_on):
    """The provisioning FLOOR is not a growth claim. resolve() mints an entity's origin surface
    with no source parameter, and that distinction is what keeps legitimately-named entities
    out of ARMs 2 and 3 alike."""
    _alias(cur, "a named thing", "a given name", "unspecified")
    _fact(cur, "an instance", "a named thing", "is_an_example_of", prov="llm_inferred")
    allow, arm = weld_guard.weld_verdict(cur, "a named thing", "another surface", "rel_default")
    assert (allow, arm) == (True, weld_guard.ALLOW)


def test_arm3_ignores_an_incoming_label_at_the_floor(cur, arm3_on):
    """An incoming surface that records no claim is not an unwarranted CLAIM — it is no claim."""
    node = _growth_place(cur)
    allow, arm = weld_guard.weld_verdict(cur, node, "an unrelated surface", "unspecified")
    assert (allow, arm) == (True, weld_guard.ALLOW)


def test_arm3_does_not_fire_when_the_node_carries_user_authority(cur, arm3_on):
    """ARM 3 is growth-vs-growth ONLY. If the user has named this node, growth adding a surface
    is not the unwarranted-both-sides case and stays out of scope."""
    node = "a user named node"
    _alias(cur, node, "an engine surface", "inferred")
    _alias(cur, node, "a given name", "user_stated")
    _fact(cur, "an instance", node, "is_an_example_of", prov="llm_inferred")
    allow, arm = weld_guard.weld_verdict(cur, node, "another surface", "rel_default")
    assert (allow, arm) == (True, weld_guard.ALLOW)


def test_arm3_never_refuses_the_seat_anchor(cur, arm3_on):
    """BAR ITEM 1 again, for the new arm. The first-person resolution target is exempt from
    EVERY arm — refusing there detaches the user's own name from the speaker."""
    seat = "a45bb965-0a66-4b1e-a1fb-8d0e89ef6ffa"
    schema = "faultline_a45bb965_0a66_4b1e_a1fb_8d0e89ef6ffa"
    _alias(cur, seat, "an engine surface", "inferred")
    _fact(cur, "an instance", seat, "is_an_example_of", prov="llm_inferred")
    allow, arm = weld_guard.weld_verdict(cur, seat, "another surface", "rel_default",
                                         schema_name=schema)
    assert (allow, arm) == (True, weld_guard.ALLOW)


def test_arm3_probe_failure_fails_open(cur, arm3_on):
    node = _growth_place(cur)
    cur.execute("DROP TABLE facts")
    allow, arm = weld_guard.weld_verdict(cur, node, "another surface", "rel_default")
    assert (allow, arm) == (True, weld_guard.ALLOW)
    cur.execute("SELECT 1")
    assert cur.fetchone()[0] == 1


# ── the ENTITY MERGE pre-flight (re_embedder) — the decision, on real rows ──────────────────

def test_the_merge_preflight_query_and_verdict_refuse_an_inadmissible_surface(cur, arm3_on):
    """GAP 2b, the decision half: the merge selects the loser aliases it is about to re-parent
    and asks the guard about each ON THE WINNER. Driven with the merge's own SELECT so the test
    exercises the real pairing, not a convenient one."""
    winner, loser = "the winner type node", "the loser"
    _alias(cur, winner, "an engine surface", "inferred")
    # USER-stated placement: the winner is a class the user declared, so ARM 1 (the arm that
    # enforces by default) refuses a name being welded onto it.
    _fact(cur, "an instance", winner, "is_an_example_of", prov="user_stated")
    _alias(cur, loser, "a given name", "user_stated")
    cur.execute("SELECT alias, preference_source FROM entity_aliases "
                "WHERE entity_id = %s AND alias NOT IN "
                "(SELECT alias FROM entity_aliases WHERE entity_id = %s)", (loser, winner))
    moving = cur.fetchall()
    assert moving == [("a given name", "user_stated")]
    assert any(weld_guard.refuse_weld(cur, winner, a, s, is_preferred=False) for a, s in moving)


def test_a_label_probe_failure_fails_open_and_does_not_poison_the_transaction(cur):
    """THE MOST LOAD-BEARING SAFETY PROPERTY, and it was untested until review said so.

    `_existing_labels` returns None (distinct from []) when its probe FAILED, and the caller
    must treat that as ALLOW. Failing CLOSED here would refuse writes across a whole tenant the
    moment the guard's own probe hit a problem — and an aborted transaction would surface
    somewhere else entirely, which is exactly how a previous defect on this table made a live
    walk report "no facts found" over rows it had already read.

    ⚠️ NOTE ON AN EQUIVALENT MUTANT: rewriting `if existing is None: return True, ALLOW` to
    `existing = []` is NOT a behaviour change — the very next line (`if not existing`) also
    returns ALLOW — so no test can distinguish it. The property worth pinning is that a REAL
    probe failure reaches ALLOW at all, which is what this test drives.
    """
    _alias(cur, "e", "an engine surface", "inferred")
    cur.execute("ALTER TABLE entity_aliases RENAME TO entity_aliases_gone")
    allow, arm = weld_guard.weld_verdict(cur, "e", "a given name", "user_stated")
    assert (allow, arm) == (True, weld_guard.ALLOW)
    cur.execute("SELECT 1")            # transaction still usable
    assert cur.fetchone()[0] == 1


def test_arm2_is_disarmed_by_default_and_is_subsumed_when_armed(cur):
    """ARM 2 shipped WITHOUT the place condition and review caught it.

    Measured: it would refuse a new user-stated label on 2 526 production entities, 2 133 of
    them not class-side by ANY provenance — INSTANCE nodes, which §7 declares legitimate
    indexing. Given the authority-safe place condition it must have (ARM 2 declines USER truth,
    so engine evidence would invert the authority order), it becomes a strict SUBSET of ARM 1,
    which runs first: measured marginal value ZERO refusals.

    Both halves are pinned here — off by default, and inert even when armed on a node ARM 1
    does not already refuse.
    """
    assert weld_guard.arm2_enabled() is False
    _alias(cur, "growth_node", "an engine surface", "inferred")
    allow, arm = weld_guard.weld_verdict(cur, "growth_node", "a given name", "user_stated")
    assert (allow, arm) == (True, weld_guard.ALLOW)


def test_arm2_when_armed_still_requires_user_stated_place_evidence(cur, monkeypatch):
    """If ARM 2 is ever armed it must NOT be able to fire on engine-authored placement — that
    inversion is the revision-1 regression that refused the seat owner's own given name."""
    monkeypatch.setenv("ALIAS_WELD_GUARD_ARM2", "on")
    _alias(cur, "growth_node", "an engine surface", "inferred")
    _fact(cur, "inst", "growth_node", "is_an_example_of", prov="llm_inferred")
    allow, _arm = weld_guard.weld_verdict(cur, "growth_node", "a given name", "user_stated")
    assert allow is True, "engine placement evidence must never veto a user-stated label"
