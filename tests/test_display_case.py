"""OBSERVED DISPLAY CASE (migration 214) — casing RETAINED at ingest, never reconstructed.

THE DEFECT: every entity name rendered lowercase ("diane does not live in toronto"), because
`entity_aliases` had no display column and the pipeline folds the surface long before the
registry sees it.

THE REJECTED ALTERNATIVE: capitalising at render time is TRUECASING (Lita, Ittycheriah, Roukos
& Kambhatla, "tRuEcasIng", ACL 2003) — lossy by construction, unable to recover `eBay`, `IBM`,
`iPhone`, `McDonald`, and it would put reconstruction on the query path. This suite pins the
retained-data behaviour AND pins that the pre-existing titlecase post-pass no longer destroys it.

THE HARD LINE: a MEMORY is user truth (the name `Diane`); a PLACE is an L4 type node (`dog`)
the engine builds. A place must never acquire a proper-name display form. Two ingest-side
guards (sentence-initial evidence discarded; PROPN runs only) plus a read-side guard
(`_entity_is_l4_place`) are each pinned below.
"""

from unittest.mock import MagicMock

import pytest

from src.extraction.display_case import (
    display_form_for,
    observe_display_forms,
    reset_display_forms,
    set_display_forms,
)


@pytest.fixture(autouse=True)
def _clean_overlay():
    """Every test starts with NO overlay bound (the production default)."""
    token = set_display_forms(None)
    yield
    reset_display_forms(token)


class TestObserveDisplayForms:
    """What the verbatim turn is allowed to teach us about a name's casing."""

    def test_retains_casing_truecasing_cannot_reconstruct(self):
        """The whole reason this is captured at ingest instead of guessed at render.

        A first-letter rule renders these `Ebay`, `Ibm`, `Iphone`, `Mcdonald` — all wrong, and
        unrecoverable from the folded string (Unicode Standard §4.2: case mappings are
        non-invertible). Retaining the surface is the only correct answer.
        """
        observed = observe_display_forms(
            "I bought an iPhone from eBay, and my mother works for IBM at McDonald Corporation."
        )
        assert observed.get("iphone") == "iPhone"
        assert observed.get("ebay") == "eBay"
        assert observed.get("ibm") == "IBM"
        assert observed.get("mcdonald") == "McDonald"

    def test_multi_token_name_is_recorded_whole_and_by_token(self):
        """The pipeline may bind either the whole name or one of its tokens as the entity."""
        observed = observe_display_forms("The report was written by Jenna Blum.")
        assert observed.get("jenna blum") == "Jenna Blum"
        assert observed.get("jenna") == "Jenna"

    # ── THE HARD LINE ────────────────────────────────────────────────────────────────────

    def test_sentence_initial_common_noun_never_becomes_a_name(self):
        """THE HARD LINE. "Dogs are great" must teach us NOTHING about the type node `dog`.

        In English orthography the first word of a sentence is capitalised obligatorily, so its
        capital carries no evidence about the word's identity — this is the same first-word
        ambiguity truecasing systems isolate as their hardest case (Lita et al. 2003 §2). If
        this ever goes red, a type node is about to start rendering like a proper name.
        """
        assert observe_display_forms("Dogs are great.") == {}
        assert observe_display_forms("Cities are noisy. Dogs are great.") == {}
        assert observe_display_forms("Preferences matter to me.") == {}

    def test_sentence_initial_evidence_is_discarded_even_for_a_real_name(self):
        """The guard is positional, not lexical — it cannot know `Toronto` is a real name, and
        must not pretend to. A later mid-sentence observation supplies the casing instead."""
        assert "toronto" not in observe_display_forms("Toronto is a city.")
        assert observe_display_forms("I live in Toronto.").get("toronto") == "Toronto"

    def test_mid_sentence_common_noun_is_not_admitted(self):
        """Second guard: only PROPN runs are observed, so a capitalised common noun in
        mid-sentence ("I love Dogs") does not mint a display form for the type."""
        assert observe_display_forms("I love Dogs.") == {}

    # ── determinism ──────────────────────────────────────────────────────────────────────

    def test_conflicting_surfaces_in_one_turn_are_dropped_not_arbitrated(self):
        """Two surfaces for one name have no principled winner; picking one would FABRICATE a
        name. NULL (and today's lowercase) is the honest answer."""
        assert "bob" not in observe_display_forms("I saw Bob. Then i saw BOB again.")

    def test_a_lowercase_only_turn_teaches_nothing(self):
        assert observe_display_forms("i live in toronto") == {}

    @pytest.mark.parametrize("bad", ["", None, 12345])
    def test_degrades_silently_on_bad_input(self, bad):
        """Casing is presentation. It must never be able to fail an ingest."""
        assert observe_display_forms(bad) == {}


class TestDisplayFormFor:
    """The write-seam gate: what is allowed to reach `entity_aliases.display_form`."""

    def test_unbound_overlay_yields_none_which_is_todays_behaviour(self):
        assert display_form_for("toronto") is None

    def test_returns_the_observed_casing(self):
        set_display_forms({"toronto": "Toronto"})
        assert display_form_for("toronto") == "Toronto"

    def test_rejects_a_value_that_is_not_a_casing_overlay(self):
        """INVARIANT: lower(display_form) == alias. `display_form` is a CASING overlay and can
        never become a second, divergent NAME — otherwise it would be user content stored
        outside the alias registry, invisible to matching and dedup."""
        set_display_forms({"toronto": "Montreal"})
        assert display_form_for("toronto") is None

    def test_stores_null_rather_than_a_redundant_copy(self):
        set_display_forms({"toronto": "toronto"})
        assert display_form_for("toronto") is None

    def test_alias_lookup_is_by_the_lowercase_key_the_pipeline_uses(self):
        set_display_forms({"toronto": "Toronto"})
        assert display_form_for("  TORONTO  ") == "Toronto"

    @pytest.mark.parametrize("bad", ["", None])
    def test_degrades_on_bad_alias(self, bad):
        set_display_forms({"toronto": "Toronto"})
        assert display_form_for(bad) is None


class TestTitlecasePostPassDefersToRetainedCasing:
    """`_titlecase_display_slots` IS the render-time truecasing this feature replaces.

    It was correct only while every slot arrived folded. Now that a slot can carry the casing
    the user actually typed, the guess must not overwrite the evidence — measured before the
    fix: `eBay` -> `EBay`, `iPhone` -> `IPhone`.
    """

    def test_retained_casing_survives_the_post_pass(self):
        from src.api.main import _titlecase_display_slots

        out = _titlecase_display_slots(
            "you bought an iPhone from eBay", ["iPhone", "eBay"]
        )
        assert "iPhone" in out and "eBay" in out
        assert "IPhone" not in out and "EBay" not in out

    def test_a_folded_slot_still_gets_todays_titlecase_fallback(self):
        """BYTE-IDENTICAL fallback: an entity with no observed casing is unaffected by this
        whole feature and keeps the pre-existing behaviour exactly."""
        from src.api.main import _titlecase_display_slots

        assert _titlecase_display_slots("you have a dog named rex", ["rex"]) == (
            "you have a dog named Rex"
        )


class TestLowercaseAliasKeepsItsSemantics:
    """`display_form` is ADDED ALONGSIDE `alias`; `alias` keeps every job it already had.

    `alias` remains the matching key, the dedup key, the UUID-v5 input, the
    `UNIQUE (entity_id, alias)` key and the `ON CONFLICT` target. If casing ever leaked into
    it, one entity would fragment into several — which is exactly what the feature must not do.
    """

    def test_casing_never_changes_the_surrogate_uuid(self):
        """The identity function is `name.lower().strip()`. Verified live end to end:
        "I met TORONTO friends in toronto and Toronto again." produced ONE `toronto` alias row
        on ONE entity."""
        from src.entity_registry.registry import _make_surrogate

        user = "11111111-2222-3333-4444-555555555555"
        base = _make_surrogate(user, "toronto")
        for variant in ("Toronto", "TORONTO", "  tOrOnTo  "):
            assert _make_surrogate(user, variant) == base


class TestDisplayCasedReadSeam:
    """`_display_cased` — the read-time renderer. It reads a STORED value; it never capitalises."""

    def test_null_display_form_renders_the_alias_unchanged(self):
        """The fallback that makes an unpopulated column byte-identical to today."""
        from src.api.main import _display_cased

        assert _display_cased(MagicMock(), "uuid-1", "toronto", None) == "toronto"

    def test_a_named_instance_keeps_its_casing(self, monkeypatch):
        import src.api.main as main

        monkeypatch.setattr(main, "_entity_is_l4_place", lambda db, eid: False)
        assert main._display_cased(MagicMock(), "uuid-1", "ibm", "IBM") == "IBM"

    def test_an_l4_place_never_renders_as_a_proper_name(self, monkeypatch):
        """THE HARD LINE, read side. Ingest cannot see that a token inside a multi-word name
        ("McDonald Corporation") folds to a word that is ALSO a type in this tenant; the
        P31/P279 ladder that settles it only exists at read time. Verified live: with
        `display_form='Corporation'` planted on a place, /query renders
        "IBM is an instance of corporation" — the instance keeps its casing, the place does not.
        """
        import src.api.main as main

        monkeypatch.setattr(main, "_entity_is_l4_place", lambda db, eid: True)
        assert main._display_cased(
            MagicMock(), "uuid-1", "corporation", "Corporation"
        ) == "corporation"

    def test_a_failing_place_probe_degrades_to_the_alias(self, monkeypatch):
        """Presentation must never fail a recall."""
        import src.api.main as main

        def _boom(db, eid):
            raise RuntimeError("db gone")

        monkeypatch.setattr(main, "_entity_is_l4_place", _boom)
        assert main._display_cased(MagicMock(), "uuid-1", "ibm", "IBM") == "ibm"
