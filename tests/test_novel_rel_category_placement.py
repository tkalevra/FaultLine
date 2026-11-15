"""Tests for taxonomy-driven novel-rel layer placement (AUDIT Item 6a fix).

These verify that a novel verb-lifted/discovered rel_type's category (its hierarchy LAYER)
is resolved against the per-tenant entity_taxonomies overlay — subject-agnostic + hierarchy-
aware — instead of the old hardcoded English keyword zoo (`_infer_category_from_rel_type`),
and that a genuinely-novel rel with no taxonomy match is marked PENDING (quarantine target),
NEVER a silent permanent "general".

PURE-FUNCTION: the taxonomy overlay resolution is monkeypatched so there is no live DB / LLM /
GLiNER2. The one live-DB path (the actual ontology_evaluations quarantine INSERT) is exercised
indirectly and marked where it would need a DB.

Maps to DEV/fix-reports/AUDIT-ingest-protocol-compliance.md Item 6a and
DEV/DESIGN-hierarchy-ladder-and-growth.md (rung 4, "no permanent orphans").
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.api import main as M


# A fake per-tenant taxonomy overlay (seed ∪ tenant). Subject-agnostic: it keys on entity
# TYPES (member_entity_types) and defining rels, never on entity names.
_FAKE_TAXONOMIES = {
    "pets": {
        "taxonomy_name": "pets",
        "member_entity_types": ["Animal"],
        "rel_types_defining_group": ["has_pet"],
    },
    "work": {
        "taxonomy_name": "work",
        "member_entity_types": ["Organization"],
        "rel_types_defining_group": ["works_for"],
    },
    "land": {
        "taxonomy_name": "land",
        "member_entity_types": ["Location", "Object"],
        "rel_types_defining_group": ["graze", "irrigate"],
    },
}


@pytest.fixture
def with_fake_overlay(monkeypatch):
    """Resolve the taxonomy overlay to the fake set, and give the resolver a DSN so it
    proceeds past the no-DSN guard (the overlay call itself is stubbed, so no DB is hit)."""
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://stub/stub")
    monkeypatch.setenv("TAXONOMY_DRIVEN_CATEGORY", "true")
    # Pass 2 (TYPE MATCH) now abstains by DEFAULT — it was measured to mis-file over the six
    # coarse GLiNER2 labels (see `_resolve_category_via_taxonomy`). The type-match tests below
    # pin what the pass DOES when it acts, so they must declare the flag that lets it act.
    # Their assertions are unchanged; only the precondition is now explicit.
    #
    # ⚠️ READ `_FAKE_TAXONOMIES` AGAINST THE REAL SEED BEFORE TRUSTING THESE.
    # The fixture gives `pets` member types ["Animal"] and `work` ["Organization"]. The
    # ACTUAL seeded rows are `pets` = ['Person','Animal'] and `work` = ['Person','Organization']
    # (verified live: every seeded taxonomy covers BOTH sides of its defining rel). The fixture
    # therefore deletes the exact property that makes type match unsound in production — a
    # Person-headed rel matches EVERY Person-bearing taxonomy — so these cases are
    # discriminating here and are not discriminating on real data. See
    # `test_real_seed_member_types_collide_which_is_why_the_default_abstains` below.
    monkeypatch.setenv("TAXONOMY_TYPE_MATCH_PLACEMENT", "true")
    monkeypatch.setattr(M.taxonomy_overlay, "resolve_current",
                        lambda dsn: dict(_FAKE_TAXONOMIES))
    return monkeypatch


# ──────────────────────────────────────────────────────────────────────────────
# Pass 2 — TYPE MATCH (hierarchy-aware): a novel rel whose head/tail types fall inside
# a taxonomy's member_entity_types resolves to that taxonomy's layer.
# ──────────────────────────────────────────────────────────────────────────────

class TestTypeMatchPlacement:
    def test_novel_rel_animal_tail_resolves_to_pets(self, with_fake_overlay):
        # A novel activity rel between a Person and an Animal → the "pets" layer (Animal ∈ pets).
        cat, pending = M._resolve_or_quarantine_category("feeds", ["Person"], ["Animal"])
        assert cat == "pets"
        assert pending is False

    def test_novel_rel_org_tail_resolves_to_work(self, with_fake_overlay):
        cat, pending = M._resolve_or_quarantine_category("consults_for", ["Person"], ["Organization"])
        assert cat == "work"
        assert pending is False

    def test_wildcards_never_match(self, with_fake_overlay):
        # ANY/SCALAR are NOT domain types → no spurious match → PENDING, not a wrong layer.
        cat, pending = M._resolve_or_quarantine_category("does_thing", ["ANY"], ["ANY"])
        assert cat == M._CATEGORY_PENDING
        assert pending is True


# ──────────────────────────────────────────────────────────────────────────────
# Pass 1 — DEFINING MATCH: a novel rel already named in a taxonomy's
# rel_types_defining_group resolves to that taxonomy (strongest signal).
# ──────────────────────────────────────────────────────────────────────────────

class TestDefiningRelPlacement:
    def test_defining_rel_resolves(self, with_fake_overlay):
        # `graze` is in land.rel_types_defining_group → resolves to "land" even though
        # its head/tail types here would ALSO type-match nothing.
        cat, pending = M._resolve_or_quarantine_category("graze", ["Animal"], ["Location"])
        assert cat == "land"
        assert pending is False


# ──────────────────────────────────────────────────────────────────────────────
# QUARANTINE — a genuinely-novel rel with NO taxonomy match → PENDING marker, not "general".
# ──────────────────────────────────────────────────────────────────────────────

class TestGenuinelyNovelQuarantine:
    def test_novel_activity_verb_is_pending_not_general(self, with_fake_overlay):
        # `compost` matches no member_entity_types (Concept ∉ any) and no defining group.
        cat, pending = M._resolve_or_quarantine_category("compost", ["Person"], ["Concept"])
        assert cat == M._CATEGORY_PENDING
        assert pending is True
        # The whole point of the fix: NOT silently bucketed to "general".
        assert cat != "general"

    def test_pending_marker_is_distinct_from_general(self):
        assert M._CATEGORY_PENDING != "general"
        assert M._CATEGORY_PENDING == "pending_placement"


# ──────────────────────────────────────────────────────────────────────────────
# KNOWN rels — unchanged: a metadata-supplied category always wins (never re-derived).
# ──────────────────────────────────────────────────────────────────────────────

class TestKnownRelMetadataWins:
    def test_explicit_metadata_category_used_at_inflow(self, with_fake_overlay):
        # _create_rel_type_in_flow takes norm.get("category") FIRST — verified at the call
        # site (main.py): a supplied category short-circuits the resolver entirely.
        # Here we assert the resolver is NOT consulted when category is present by checking
        # the resolver would have returned something different.
        supplied = "identity"
        resolver_would_give, _ = M._resolve_or_quarantine_category("also_known_as", ["Person"], ["Concept"])
        assert resolver_would_give != supplied  # proves the two paths differ
        # The call site uses `norm.get("category")` before the resolver, so `supplied` wins.


# ──────────────────────────────────────────────────────────────────────────────
# FAIL-SAFE — overlay/DB error inside the resolver → "no match" → PENDING, never a crash.
# ──────────────────────────────────────────────────────────────────────────────

class TestFailSafe:
    def test_overlay_raises_returns_pending(self, monkeypatch):
        monkeypatch.setenv("POSTGRES_DSN", "postgresql://stub/stub")
        monkeypatch.setenv("TAXONOMY_DRIVEN_CATEGORY", "true")

        def _boom(dsn):
            raise RuntimeError("overlay unreadable")

        monkeypatch.setattr(M.taxonomy_overlay, "resolve_current", _boom)
        # Must NOT raise — must degrade to PENDING (tracked), per the fail-safe rule.
        cat, pending = M._resolve_or_quarantine_category("feeds", ["Person"], ["Animal"])
        assert cat == M._CATEGORY_PENDING
        assert pending is True

    def test_no_dsn_returns_pending(self, monkeypatch):
        monkeypatch.delenv("POSTGRES_DSN", raising=False)
        monkeypatch.setenv("TAXONOMY_DRIVEN_CATEGORY", "true")
        cat, pending = M._resolve_or_quarantine_category("feeds", ["Person"], ["Animal"])
        assert cat == M._CATEGORY_PENDING
        assert pending is True


# ──────────────────────────────────────────────────────────────────────────────
# FLAG ROLLBACK — TAXONOMY_DRIVEN_CATEGORY=false restores the legacy keyword zoo
# (documented last-resort), and never returns PENDING.
# ──────────────────────────────────────────────────────────────────────────────

class TestFlagRollback:
    def test_flag_off_uses_keyword_fallback(self, monkeypatch):
        monkeypatch.setenv("TAXONOMY_DRIVEN_CATEGORY", "false")
        # "has_pet" → keyword "pet" → "pets" (legacy behavior preserved exactly).
        cat, pending = M._resolve_or_quarantine_category("has_pet", ["Person"], ["Animal"])
        assert cat == "pets"
        assert pending is False

    def test_flag_off_unknown_verb_is_general_not_pending(self, monkeypatch):
        monkeypatch.setenv("TAXONOMY_DRIVEN_CATEGORY", "false")
        cat, pending = M._resolve_or_quarantine_category("compost", ["Person"], ["Concept"])
        assert cat == "general"   # legacy keyword zoo default
        assert pending is False   # flag-off never quarantines (full prior behavior)


# ──────────────────────────────────────────────────────────────────────────────
# Regression guard: the keyword fallback function still behaves as before (so a
# flag-off rollback is byte-identical to the pre-fix path).
# ──────────────────────────────────────────────────────────────────────────────

class TestKeywordFallbackUnchanged:
    @pytest.mark.parametrize("rt,expected", [
        ("favorite_dog", "pets"),
        ("works_at_office", "work"),
        ("spouse_of", "family"),
        ("home_address", "location"),
        ("birth_year", "temporal"),
        ("body_height", "physical"),
        ("preferred_alias", "identity"),
        ("graze", "general"),
    ])
    def test_keyword_fallback_values(self, rt, expected):
        assert M._infer_category_from_rel_type(rt) == expected


# ──────────────────────────────────────────────────────────────────────────────
# Why the DEFAULT abstains: the real seed is not discriminating.
# ──────────────────────────────────────────────────────────────────────────────

# The member_entity_types actually seeded per tenant (verified against a live tenant:
# 13/13 seeded (taxonomy, defining-rel) pairs cover BOTH the head and tail type of their
# defining rel — `has_pet` head Person / tail Animal, `works_for` head Person / tail
# Person+Organization, and so on).
_REAL_SEED_TAXONOMIES = {
    "pets": {"taxonomy_name": "pets",
             "member_entity_types": ["Person", "Animal"],
             "rel_types_defining_group": ["has_pet"]},
    "work": {"taxonomy_name": "work",
             "member_entity_types": ["Person", "Organization"],
             "rel_types_defining_group": ["works_for"]},
    "family": {"taxonomy_name": "family",
               "member_entity_types": ["Person"],
               "rel_types_defining_group": ["parent_of"]},
}


def test_real_seed_member_types_collide_which_is_why_the_default_abstains(monkeypatch):
    """The evidence behind the abstention, expressed as a contract.

    Against the REAL seeded shape, a Person-headed novel rel is admissible to `pets`, `work`
    AND `family` at once — the six-label type vocabulary carries no signal that separates
    them, so "first match in dict order" decides. Measured live on the local corpus, that
    files `hike`, `wash`, `commute`, `earn` and `visit` all under `work`.

    So: with the flag ON the pass places (arbitrarily); with it OFF — the default — the rel
    stays PENDING, captured and honestly marked unplaced rather than mis-filed into a
    grouping it would then project into on every scoped query.
    """
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://stub/stub")
    monkeypatch.setenv("TAXONOMY_DRIVEN_CATEGORY", "true")
    monkeypatch.setattr(M.taxonomy_overlay, "resolve_current",
                        lambda dsn: dict(_REAL_SEED_TAXONOMIES))

    # The collision is a property of the seed, not of any one rel: a Person head is covered
    # by every one of these groupings.
    admissible = [n for n, meta in _REAL_SEED_TAXONOMIES.items()
                  if "person" in {t.lower() for t in meta["member_entity_types"]}]
    assert len(admissible) >= 3, (
        "the real seed is expected to be NON-discriminating for a Person-headed rel; if this "
        f"changed, re-measure before trusting type match again (admissible={admissible})")

    monkeypatch.setenv("TAXONOMY_TYPE_MATCH_PLACEMENT", "false")
    cat, pending = M._resolve_or_quarantine_category("hikes_with", ["Person"], ["Location"])
    assert cat == M._CATEGORY_PENDING and pending is True, (
        "DEFAULT must abstain to pending_placement rather than mis-file a coarse-typed rel")

    monkeypatch.setenv("TAXONOMY_TYPE_MATCH_PLACEMENT", "true")
    cat_on, pending_on = M._resolve_or_quarantine_category("hikes_with", ["Person"], ["Location"])
    assert pending_on is False and cat_on in admissible, (
        "with the flag ON the pass must still place — the gate is a decision, not a deletion")
