"""THE HARD LINE — a MEMORY must never be pushed through the L4 TYPE-classification lane.

Pins `src.extraction.linguistics.type_term_shape`, the TERM-ADMISSIBILITY predicate that guards
the three `ingest_miss_pushback` writers (two on the ingest path in `src/api/main.py`, one in the
async climb in `src/re_embedder/embedder.py`).

THE BUG THIS PINS (measured on a live deployment). The ingest concept-grounding
queue admitted ANY un-laddered relational OBJECT as a "novel concept" needing an is-a ladder. It
asked "does this object have a place yet?" and never "is this the KIND OF THING that gets a place?"
So phrases lifted verbatim out of the owner's messages were minted as concepts and driven through
repeated LLM "what is X?" classification:

    config file at ~/.config/example/settings.json   (surfaced by `owns`)
    alpha+beta                                        (surfaced by `embed`)
    .md file extension                                (surfaced by `use`)
    notes.md                                        (surfaced by `instance_of`)
    need for review                                   (surfaced by `increase`)

Those are things the user SAID — memories. A name/value/specific instance never becomes a place.

THE MECHANISM IS NAMED AND CITED — see the `type_term_shape` docstring: TERM ADMISSIBILITY, the
terminology-science distinction between a DESIGNATION (a term naming a concept) and running text
that describes one (ISO 704, "Terminology work — Principles and methods"; §7.4 linguistic economy).

⚠️ THE MEASUREMENT THAT SHAPED THIS GATE — DO NOT "IMPROVE" IT WITH AN OPEN-CLASS POS RULE.
The obvious implementation is "the head must be a NOUN". It was measured against spaCy
`en_core_web_sm` FIRST and it would have destroyed the engine's own legitimately-grown type nodes:

    file_extension  -> NUM      data_structure -> X       digital_content -> NUM
    wireguard       -> ADJ      notes.md     -> NUM

Open-class POS is unreliable on short OOV / snake_case surfaces. Only CLOSED-CLASS tags
(ADP/DET/PRON/AUX/CCONJ/SCONJ/PART) are trustworthy, because they are a finite high-frequency
inventory. If `test_engine_grown_type_names_are_never_rejected` goes red, an open-class rule was
added — RE-MEASURE before changing the expectation.

A frequency gate was also considered and rejected on measurement: 526 of 540 miss-pushback concepts
on the production seat are hapax (occurrence_count = 1), so a freq >= 3 rule would have disabled
concept grounding entirely rather than fixed it.
"""

import pytest

from src.extraction.linguistics import type_term_shape


# The exact surfaces observed on the production seat, with the rel_type that surfaced each.
PRODUCTION_FRAGMENTS = [
    ("config file at ~/.config/example/settings.json", "owns"),
    ("alpha+beta", "embed"),
    (".md file extension", "use"),
    ("notes.md", "owns"),
    ("need for review", "increase"),
]

# Engine-grown type nodes that share `climb_state` / `ontology_evaluations` with the fragments
# above. These are legitimate L4 PLACES and MUST survive the gate untouched.
ENGINE_GROWN_TYPES = [
    "file_extension", "data_structure", "information_artifact", "condition", "web_page",
    "digital_content", "file_metadata", "file_attribute", "occurrence", "requirement",
    "geographical_feature", "academic_discipline", "operating_system", "markup_language",
]

# Ordinary user-surfaced types (multi-word, no snake_case). Also legitimate places.
USER_SURFACED_TYPES = [
    "corn snake", "morkie dog", "dog", "pet", "snake", "cat", "animal",
    "control plane", "agentic harness", "wireguard", "boundary", "card", "parts",
]


@pytest.mark.parametrize("surface,rel", PRODUCTION_FRAGMENTS)
def test_production_user_content_fragments_are_refused(surface, rel):
    """The five surfaces that were actually minted as concepts on prod must be refused."""
    ok, reason = type_term_shape(surface)
    assert ok is False, (
        f"{surface!r} (surfaced by {rel!r}) is user CONTENT, not a type designation — "
        f"it must never be queued for is-a grounding. Got ok={ok} reason={reason!r}."
    )
    assert reason and reason != "term"


@pytest.mark.parametrize("surface", ENGINE_GROWN_TYPES)
def test_engine_grown_type_names_are_never_rejected(surface):
    """Engine-minted snake_case type nodes are L4 PLACES and must pass.

    If this goes red, an OPEN-CLASS POS rule was added to `type_term_shape`. spaCy tags several of
    these as NUM/X/ADJ — see the module docstring. Re-measure before changing this expectation."""
    ok, reason = type_term_shape(surface)
    assert ok is True, (
        f"{surface!r} is a legitimate engine-grown TYPE node and must remain admissible; "
        f"got ok={ok} reason={reason!r}."
    )


@pytest.mark.parametrize("surface", USER_SURFACED_TYPES)
def test_ordinary_types_are_never_rejected(surface):
    ok, reason = type_term_shape(surface)
    assert ok is True, f"{surface!r} must remain admissible; got ok={ok} reason={reason!r}."


def test_paths_and_filenames_are_refused_without_a_parser():
    """Rule (1) is ORTHOGRAPHIC and parser-free, so it holds even when spaCy is unavailable."""
    for surface in ("notes.md", "example-app.json", "~/.config", "alpha+beta", "a/b/c"):
        ok, reason = type_term_shape(surface)
        assert ok is False, f"{surface!r} should be refused; got {reason!r}"
        assert reason == "non_lexical_orthography", (
            f"{surface!r} should be refused by the parser-free orthographic rule so the gate "
            f"still holds with no spaCy model; got {reason!r}"
        )


def test_underscore_and_digits_stay_admissible():
    """The engine's own joiners must not be mistaken for non-lexical orthography."""
    for surface in ("file_extension", "web_page", "ipv6", "http2"):
        ok, reason = type_term_shape(surface)
        assert ok is True, f"{surface!r} must stay admissible; got {reason!r}"


def test_bare_symbols_and_years_carry_no_lexical_content():
    """A single letter or a bare year is a VALUE, never a type designation.

    `f` and `2026` were both live rows in the production seat's climb/what-is queues (`2026` with
    occurrence_count 23 — a date fragment repeatedly re-classified as a concept)."""
    for surface in ("f", "2026", "42", "-"):
        ok, reason = type_term_shape(surface)
        assert ok is False, f"{surface!r} should be refused; got {reason!r}"


def test_adposition_makes_it_a_description_not_a_designation():
    """A PP postmodifier means the surface DESCRIBES a concept rather than NAMING one."""
    ok, reason = type_term_shape("need for review")
    assert ok is False
    assert reason.startswith("function_word:ADP"), reason


def test_gate_off_is_byte_for_byte_legacy():
    """`CONCEPT_TERM_SHAPE_GATE=0` must restore today's behavior exactly (kill-switch)."""
    import src.extraction.linguistics as ling

    original = ling.CONCEPT_TERM_SHAPE_GATE
    try:
        ling.CONCEPT_TERM_SHAPE_GATE = False
        for surface, _rel in PRODUCTION_FRAGMENTS:
            ok, reason = type_term_shape(surface)
            assert ok is True and reason == "gate_off", (
                f"with the gate OFF every surface must pass unchanged; {surface!r} -> {reason!r}"
            )
    finally:
        ling.CONCEPT_TERM_SHAPE_GATE = original


def test_empty_and_none_are_refused_safely():
    for surface in ("", "   ", None):
        ok, reason = type_term_shape(surface)
        assert ok is False and reason == "empty"
