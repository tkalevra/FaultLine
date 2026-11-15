"""Recall-scope OVER-PROJECTION audit — repro scaffold.

Companion to DEV/review/recall-scope-overprojection-audit.md. The audit finds every
place the /query walk projects facts by scope and classifies each drop as BUG (a
valid in-scope fact dropped) vs CORRECT (aspect precision / cross-domain-bleed
protection, cf. milestone 398a4fb0 — must NOT change).

This file has TWO kinds of test:

  • RUNNABLE GREEN — pin the CORRECT behaviour of the two pure, DB-free projection
    predicates (`_member_fact_passes_axis`, `_qdrant_fact_is_class_c`). These are the
    load-bearing firewall surfaces; a future "fix" that loosens them to chase the
    over-projection bugs must turn THESE red (they are the guard, not the bug).

  • SKIP-MARKED REPRO — the four DB-bound over-projection BUG sites (#1–#4). They are
    inline in /query and fetch_facts_from_anchor and need a live per-tenant schema
    with grown metadata, so they are documented as executable-against-a-tenant
    scenarios for the main.py fix owner (the OWNER implements the fix; this agent does
    NOT edit main.py). Each carries the exact ingest + query + expected-vs-actual.

The owner directive: "recall shouldn't be scoped like that, whether on /expand topics
or otherwise." The bug class: allowed_rels / member_entity_types are resolved from the
SEED taxonomy shape, narrower than what INGEST GREW → grown rels + grown/unknown-typed
members fall outside the seed scope and get projected out.
"""
import pytest

from src.api import main as m
from src.api.models import QueryPath


# Closed classification set (instance_of/subclass_of/is_a) — the CLASSIFICATION axis.
CLASSIFICATION = frozenset({"instance_of", "subclass_of", "is_a"})


# ─────────────────────────────────────────────────────────────────────────────
# RUNNABLE GREEN — the CORRECT projection predicates (the firewall that must hold).
# These pin behaviour the over-projection fix must NOT regress.
# ─────────────────────────────────────────────────────────────────────────────

class TestMembershipAxisFilterIsCorrect:
    """`_member_fact_passes_axis` (main.py:3154) — the two-axes split.

    CORRECT drop: a reached member's classification chain is dropped on a MEMBERSHIP
    query (it answers a different question). This is NOT over-projection — it is the
    axis firewall. Documented here so a loosening fix can't silently take it out.
    """

    def test_membership_drops_member_classification_chain(self):
        assert not m._member_fact_passes_axis("instance_of", "membership", CLASSIFICATION)
        assert not m._member_fact_passes_axis("subclass_of", "membership", CLASSIFICATION)

    def test_membership_keeps_membership_edges_and_scalars(self):
        for rel in ("has_pet", "parent_of", "member_of", "part_of", "spouse",
                    "age", "pref_name"):
            assert m._member_fact_passes_axis(rel, "membership", CLASSIFICATION), rel

    def test_classification_axis_keeps_everything(self):
        assert m._member_fact_passes_axis("instance_of", "classification", CLASSIFICATION)
        assert m._member_fact_passes_axis("has_pet", "classification", CLASSIFICATION)

    def test_no_axis_passes_everything(self):
        # axis None → legacy behaviour, no projection.
        assert m._member_fact_passes_axis("instance_of", None, CLASSIFICATION)
        assert m._member_fact_passes_axis("anything_grown", None, CLASSIFICATION)


class TestQdrantClassCDropIsCorrect:
    """`_qdrant_fact_is_class_c` (main.py:9650) — the A/B-off-the-vector-lane firewall.

    CORRECT drop: A/B Qdrant results are dropped because A/B are Postgres-authoritative
    (VECTOR_CLASS_C_ONLY). This is NOT over-projection. Pinned so a fix can't start
    serving A/B from the vector to paper over a Postgres-walk drop (that would violate
    the tier model). See audit site #6 — this is the amplifier, not the bug.
    """

    def test_store_context_is_always_class_c(self):
        assert m._qdrant_fact_is_class_c({"rel_type": "context"}) is True

    def test_facts_table_point_is_ab_dropped(self):
        # source_table == "facts" → A/B → NOT class C → dropped from the vector lane.
        assert m._qdrant_fact_is_class_c({"source_table": "facts", "fact_id": 7}) is False

    def test_resolved_ab_key_wins_over_claimed_source(self):
        # Even if the point claims staged, a resolved (facts, id) key marks it A/B.
        f = {"source_table": "staged_facts", "fact_id": 7, "fact_class": "C"}
        assert m._qdrant_fact_is_class_c(f, {("facts", 7)}) is False

    def test_staged_class_c_is_kept(self):
        f = {"source_table": "staged_facts", "fact_id": 9, "fact_class": "C"}
        assert m._qdrant_fact_is_class_c(f, set()) is True


class TestUntypedGroupingMemberAdmission:
    """`_untyped_grouping_member_admitted` (main.py) — audit site #1 fix, pure predicate.

    The taxonomy node-gate's `not _signals` (NO resolved type) branch. Proves the fix
    admits a GROWN grouping member reached via the grouping's own defining rels
    (`_grouping_reachable`) WITHOUT widening the aspect-precise firewall: a no-type fact
    that was NOT reached via the grouping stays dropped, and the typed-MISMATCH drop
    (site #5) is a DIFFERENT branch this predicate never sees.
    """

    def test_grouping_reachable_untyped_member_is_admitted(self):
        # /expand-grown device: staged-B, llm_learned, entity_type unknown, reached from
        # the anchor via the taxonomy's defining rel → admitted by REACHABILITY.
        f = {"fact_class": "B", "provenance": "llm_learned", "_grouping_reachable": True}
        assert m._untyped_grouping_member_admitted(f) is True

    def test_user_correction_still_admitted(self):
        assert m._untyped_grouping_member_admitted(
            {"fact_class": "A", "provenance": "user_stated"}) is True
        assert m._untyped_grouping_member_admitted(
            {"fact_class": "B", "provenance": "user_stated"}) is True

    def test_untyped_NON_reachable_fact_stays_dropped(self):
        # Aspect precision: a no-type staged-B fact that was NOT reached via the grouping
        # (no `_grouping_reachable` marker) is NOT admitted — the anti-bleed floor holds.
        f = {"fact_class": "B", "provenance": "llm_learned"}
        assert m._untyped_grouping_member_admitted(f) is False
        assert m._untyped_grouping_member_admitted(
            {"fact_class": "C", "provenance": "llm_inferred"}) is False

    def test_marker_only_touches_the_no_signal_branch(self):
        # The predicate governs ONLY the no-type-signal branch; it carries no notion of a
        # type match/mismatch. The typed-MISMATCH drop (site #5 / RC2 398a4fb0) is a
        # sibling `else` that an entity WITH a signal reaches instead — never this rule.
        # So a `_grouping_reachable` marker cannot resurrect a fuzzy-taxonomy bleed.
        reachable_but_would_be_typed_mismatch = {
            "fact_class": "B", "provenance": "llm_learned", "_grouping_reachable": True,
        }
        # The predicate says "admit" — but in the gate this branch is only entered when
        # the entity has NO signal; a typed entity never gets here. Pin the predicate's
        # contract so a future edit can't smuggle a type check into it.
        assert m._untyped_grouping_member_admitted(
            reachable_but_would_be_typed_mismatch) is True


class TestGroupEdgeOutwardToUserIsCut:
    """`_group_edge_is_outward_to_user` — bounds the grown-grouping walk (over-widen fix).

    Live e2e caught the named-group all-edges fetch reaching the USER node via
    `dan has_interest_in espresso`, then the downstream /query expansions cascaded the
    user's whole personal profile (occupation/family/address) into a "tell me about
    espresso" answer. The bound: cut a group-node edge whose non-group endpoint is the
    querying user. These prove it fires for the user link and NOT for domain members.
    """

    GRP = "espresso-uuid"
    USER = "user-uuid"

    def test_user_interest_edge_is_cut(self):
        # (dan, has_interest_in, espresso) — user is the subject, espresso the group.
        assert m._group_edge_is_outward_to_user(self.USER, self.GRP, self.GRP, self.USER)
        # (espresso, some_rel, dan) — user on the object side too.
        assert m._group_edge_is_outward_to_user(self.GRP, self.USER, self.GRP, self.USER)

    def test_domain_member_edge_is_KEPT(self):
        # (espresso, subclass_of, coffee) — the grouping's OWN structure stays.
        assert not m._group_edge_is_outward_to_user(
            self.GRP, "coffee-uuid", self.GRP, self.USER)
        # (espresso, brewed_by, machine) — a domain-rel member stays.
        assert not m._group_edge_is_outward_to_user(
            self.GRP, "machine-uuid", self.GRP, self.USER)
        # (latte, subclass_of, espresso) — inbound domain edge stays.
        assert not m._group_edge_is_outward_to_user(
            "latte-uuid", self.GRP, self.GRP, self.USER)

    def test_no_user_id_never_cuts(self):
        # Empty/None user_id (single-user fallback unresolved) must not drop everything.
        assert not m._group_edge_is_outward_to_user(self.GRP, "coffee-uuid", self.GRP, "")


class TestUserAnchoredTopicClassificationGate:
    """`_is_user_anchored_topic_classification` — bounds the user-anchored classification
    path (over-widen regression: "what is espresso a type of?" dumped the user's family).

    "What is X a type of" where X is a taxonomy falls back to anchor=USER; re-admitting the
    user's own is-a chain seeds Step 3c's downward fan-out into the whole family. The gate
    suppresses that ONLY when the anchor is the user AND a topic grouping owns the
    classification — never for a non-user anchor or scopeless classification.
    """

    class _Path:
        def __init__(self, taxonomy_groups):
            self.taxonomy_groups = taxonomy_groups

    def test_user_anchor_with_topic_scope_is_gated(self):
        assert m._is_user_anchored_topic_classification(
            "user-uuid", "user-uuid", self._Path(["espresso"])) is True

    def test_non_user_anchor_not_gated(self):
        # "what is router-1" — anchor is the topic entity itself → classification is legit.
        assert m._is_user_anchored_topic_classification(
            "router-1-uuid", "user-uuid", self._Path(["networking"])) is False

    def test_user_anchor_without_topic_scope_not_gated(self):
        # "what am I" — user classification with no topic grouping → keep the user's is-a.
        assert m._is_user_anchored_topic_classification(
            "user-uuid", "user-uuid", self._Path([])) is False
        assert m._is_user_anchored_topic_classification(
            "user-uuid", "user-uuid", self._Path(None)) is False


# ─────────────────────────────────────────────────────────────────────────────
# SKIP-MARKED REPRO — the DB-bound OVER-PROJECTION BUG sites.
# Executable against a live per-tenant schema with GROWN metadata. The main.py owner
# implements the fix; when fixed, remove the skip and assert members > 0 / rel present.
# ─────────────────────────────────────────────────────────────────────────────

_NEEDS_TENANT = "DB-bound: needs a live per-tenant schema with GROWN metadata; " \
                "inline in /query — main.py fix owner runs against a tenant."


@pytest.mark.skip(reason=_NEEDS_TENANT)
class TestOverProjectionBugSites:
    """The four BUG drop sites, as runnable-against-a-tenant scenarios.

    Setup for all: provision a tenant, `/expand networking` (or ingest a coherent
    domain corpus) so members land staged-B / grown rels / unknown types, then query
    the grouping through the MCP recall path.
    """

    def test_site1_unknown_typed_grown_member_dropped_by_node_gate(self):
        """BUG #1 — main.py:37003-37016 (legacy taxonomy node-gate).

        Ingest: /expand networking; corpus mentioning core-1, rack-7 (grown devices →
                staged-B, provenance llm_learned, entity_type 'unknown', instance_of
                still in staged_facts).
        Query : "tell me about my network"
        Now   : node-gate resolves no type signal for the grown members, they are
                class B (not user_stated) → EXCLUDED → members≈0.
        Want  : graph-reachable/staged grouping members surface (admit by reachability,
                exempt unknown-typed reachable members from the type node-gate).
        Do NOT change the SIBLING drop at 37017-37026 (typed-mismatch = correct RC2).

        STATUS (query-side fix LANDED — validate live, do not re-implement):
          • The ENTITY-NAMED grouping (`/expand espresso` → "tell me about espresso")
            is fully closed at the source: the named-group branch of
            fetch_facts_from_anchor now fetches ALL edges touching the group node
            (facts ∪ staged-B/live-C, NO rel projection) stamped `_hierarchy_admitted`.
          • The USER-ANCHORED grouping ("tell me about my network") is closed via a
            `_grouping_reachable` marker on the graph-reachable lane + the node-gate
            `not _signals` admission (pure predicate `_untyped_grouping_member_admitted`,
            unit-tested above). Sibling typed-mismatch drop untouched.
        """
        raise NotImplementedError(_NEEDS_TENANT)

    def test_site2_grown_rel_projected_out_of_allowed_rels(self):
        """BUG #2 — main.py:32197 / 32294 / 32433 (allowed_rels projection).

        Ingest: "core-1 is mounted in rack-7"  (mounted_in grown, staged-B).
        Scope : networking defines {located_in, part_of, connected_to} — NOT mounted_in.
        Query : "where is core-1"
        Now   : rel_type = ANY(allowed_rels) drops the mounted_in edge (direct+staged);
                only rel_type IS NULL rough rows are exempt.
        Want  : a rel GROWN under this grouping (or connecting two in-grouping entities)
                is in-scope even if absent from the seed rel_types_defining_group.
        """
        raise NotImplementedError(_NEEDS_TENANT)

    def test_site3_descendant_grown_relationship_dropped(self):
        """BUG #3 — main.py:8509-8516 (_collect_descendant_layered_facts scope gate).

        Ingest: family + "Rex is my dog" + "Rex is groomed by Sarah" (groomed_by grown).
        Query : "tell me about my family"
        Now   : descent reaches Rex; his scalars (age) pass, has_state/feels pass, but
                groomed_by ∉ scope ∪ descent ∪ state rels → DROPPED.
        Want  : a directly-descended member's own grown relationship surfaces (the walk
                descended precisely to collect the leaf's grounded facts).
        Keep  : the axis filter one line up dropping Rex's instance_of chain (correct).
        """
        raise NotImplementedError(_NEEDS_TENANT)

    def test_site4_member_offaxis_relationship_dropped(self):
        """BORDERLINE BUG #4 — main.py:36855-36873 (axis_projection membership else-drop).

        Ingest: "my sister Diane" + "Diane lives in Toronto" (+ "my brother works for
                Google").
        Query : "tell me about my family"
        Now   : lives_in / works_for ∉ family defining rels, not classification, not
                _hierarchy_admitted → hit the else → DROPPED. Scalars (age/name) survive.
        Want  : a DIRECTLY-reached group member's own 1-hop relationship is in-scope for
                a grouping recall (owner call: member 1-hop yes, arbitrary transitive no).
        Tension: same knob as cross-domain protection — narrow the exemption to reached
                members, do not blanket-admit off-axis rels.
        """
        raise NotImplementedError(_NEEDS_TENANT)


@pytest.mark.skip(reason="CORRECT behaviour (aspect precision, milestone 398a4fb0) — "
                         "documented as a NON-bug; a fix must NOT loosen this.")
class TestCorrectlyScopedOut:
    """Guardrails: drops that are CORRECT and must survive the over-projection fix."""

    def test_typed_mismatch_foreign_entity_stays_dropped(self):
        """CORRECT #5 — main.py:37017-37026. `fatty` typed 'homelab server' vs
        family.member_types={person} → dropped. This is RC2 / 398a4fb0. Widening it
        re-opens the fuzzy-taxonomy bleed. The only legitimate miss is an UNDER-SEEDED
        member_entity_types → fix at SEED/ingest, never by loosening the query gate.
        """
        raise NotImplementedError

    def test_scopeless_autobiographical_default_stays_small(self):
        """CORRECT — main.py:32105-32121. "hello" → {pref_name, has_interest_in} only,
        NOT a firehose of every user-subject fact. Intentional anti-bleed.
        """
        raise NotImplementedError
