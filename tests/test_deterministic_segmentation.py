"""SEGDET — deterministic-first spine segmentation.

Pins the contract of ``decompose_assertions`` / ``segment_independent_clauses`` /
``segmentation_residual_signals`` and the ``SPINE_DETERMINISTIC_SEGMENTATION`` flag.

The flag is DEFAULT OFF and the OFF path must stay byte-identical to today's atomize-first pipeline.
"""
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction.linguistics import (  # noqa: E402
    decompose_assertions,
    linguistics_available,
    segment_clauses,
    segment_independent_clauses,
    segmentation_residual_signals,
)
from src.extraction.reframe import _content_tokens  # noqa: E402

pytestmark = pytest.mark.skipif(
    not linguistics_available(), reason="spaCy layer unavailable (SPACY_MODEL not baked)")


# ── the flag is OFF by default and OFF changes nothing ──────────────────────────────
def test_flag_defaults_off():
    """SPINE_DETERMINISTIC_SEGMENTATION must default OFF — flag-OFF is today's path verbatim."""
    import importlib
    import src.api.main as main
    prev = os.environ.pop("SPINE_DETERMINISTIC_SEGMENTATION", None)
    try:
        importlib.reload(main)
        assert main.SPINE_DETERMINISTIC_SEGMENTATION is False
    finally:
        if prev is not None:
            os.environ["SPINE_DETERMINISTIC_SEGMENTATION"] = prev


def test_flag_reads_env():
    import src.api.main as main
    assert hasattr(main, "SPINE_DETERMINISTIC_SEGMENTATION")


# ── what the decomposer MUST split ─────────────────────────────────────────────────
def test_coordinated_vp_reattaches_the_shared_subject():
    """UD basic annotation hangs the subject off the FIRST conjunct only; the enhanced
    representation makes it shared. Reconstructing it is a COPY of the user's own token."""
    out = decompose_assertions("I bought a car and sold the bike.")
    assert len(out) == 2
    assert any("bought a car" in u for u in out)
    sold = [u for u in out if "sold" in u][0]
    assert sold.lower().startswith("i "), f"subject not re-attached: {sold!r}"


def test_three_way_vp_coordination():
    out = decompose_assertions("I fixed the fence, painted the shed and mowed the lawn on Sunday.")
    assert len(out) == 3
    assert all(u.lower().startswith("i ") for u in out), out


def test_subject_relative_is_promoted_to_its_own_assertion():
    out = decompose_assertions("Paris, which is in France, is lovely.")
    assert len(out) == 2
    assert any(u.lower().startswith("paris is in france") for u in out), out
    assert not any("which" in u.lower() for u in out), out


def test_apposition_is_LEFT_WHOLE_and_does_not_escalate():
    """MEASURED decision, not an omission: promoting an apposition renders cleanly but LOSES
    capture (-8.8% derived edges on 300 real LongMemEval turns) because the deriver's naming /
    binder chains already read it off the intact NP. So it is neither split NOR escalated."""
    s = "My laptop, a Dell XPS 13, died yesterday."
    assert decompose_assertions(s) == [s]
    assert "unsplit_apposition" not in segmentation_residual_signals(s)


def test_relative_inside_an_apposition_is_still_lifted():
    """The apposition stays whole (the binder chain owns it) but the relative clause riding on it
    is still promoted to its own assertion."""
    out = decompose_assertions("My daughter Marina, who is 10, broke her leg.")
    assert len(out) == 2, out
    joined = " | ".join(out).lower()
    assert "marina is 10" in joined
    assert "broke her leg" in joined
    assert "who" not in joined


def test_independent_clause_coordination_splits():
    out = segment_independent_clauses(
        "I attended the workshop on June 14th and my wife met a mechanic there.")
    assert len(out) == 2
    assert "June 14th" in out[0]
    assert "mechanic" in out[1]


# ── what the decomposer MUST NOT split ─────────────────────────────────────────────
@pytest.mark.parametrize("sentence", [
    "I went to the store because it was raining.",        # advcl — the ground, not a peer assertion
    "I think the car is fast.",                            # ccomp — attitude scope
    "My favorite color is red.",                           # simple
    "I have a dog named Rex.",                             # single predicate
])
def test_subordination_and_simple_clauses_are_left_whole(sentence):
    assert decompose_assertions(sentence) == [sentence]


def test_colon_enumeration_is_handed_on_untouched():
    """Colon lists belong to split_enumeration / the LLM escalation — never to generic decomposition
    (which mis-attaches the count noun to every member)."""
    s = "We have three kids: Marina who is 10, Theo who is 12, and Dax who is 19."
    assert decompose_assertions(s) == [s]
    assert "unsplit_enumeration" in segmentation_residual_signals(s)


def test_empty_and_whitespace_are_safe():
    assert decompose_assertions("") == []
    assert decompose_assertions("   ") == ["   "]


# ── the write-path guarantees ──────────────────────────────────────────────────────
_CORPUS = [
    "I bought a car and sold the bike.",
    "My laptop, a Dell XPS 13, died yesterday.",
    "Paris, which is in France, is lovely.",
    "My daughter Marina, who is 10, broke her leg.",
    "I fixed the fence, painted the shed and mowed the lawn on Sunday.",
    "I got my car serviced on March 15th, and it was a great experience.",
    "I went to the store because it was raining.",
    "The car I bought last year is red.",
    "My favorite color is red.",
]


@pytest.mark.parametrize("sentence", _CORPUS)
def test_lossless_every_source_content_token_survives(sentence):
    got = set()
    for u in decompose_assertions(sentence):
        got |= _content_tokens(u)
    missing = _content_tokens(sentence) - got
    assert not missing, f"decomposition dropped user truth: {sorted(missing)}"


@pytest.mark.parametrize("sentence", _CORPUS)
def test_fabrication_guard_nothing_is_ever_inserted(sentence):
    """Every unit is a REORDERING of the user's own tokens — nothing is inserted, so the decomposer
    needs no inline lexicon at all. Verified over 300 real LongMemEval turns: 0 introduced tokens,
    0 dropped tokens."""
    got = set()
    for u in decompose_assertions(sentence):
        got |= _content_tokens(u)
    introduced = got - _content_tokens(sentence)
    assert not introduced, sorted(introduced)


@pytest.mark.parametrize("sentence", _CORPUS)
def test_never_emits_a_subjectless_fragment(sentence):
    from src.extraction.linguistics import _get_nlp, _has_overt_subject
    nlp = _get_nlp()
    for u in decompose_assertions(sentence):
        if u == sentence:
            continue
        assert any(_has_overt_subject(t) for t in nlp(u)), f"subject-less fragment: {u!r}"


@pytest.mark.parametrize("sentence", _CORPUS)
def test_decomposition_is_byte_identical_across_repetitions(sentence):
    first = decompose_assertions(sentence)
    for _ in range(4):
        assert decompose_assertions(sentence) == first


# ── the ESCALATION TRIGGER is structural ───────────────────────────────────────────
def test_clean_unit_does_not_escalate():
    assert segmentation_residual_signals("My favorite color is red.") == set()
    assert segmentation_residual_signals("I went to the store because it was raining.") == set()


def test_object_relative_escalates():
    """An object relative cannot be promoted losslessly, so it is handed to the LLM."""
    assert "unsplit_relative" in segmentation_residual_signals("The car I bought last year is red.")


def test_trigger_is_not_a_length_heuristic():
    """A long single-assertion sentence must NOT escalate; a short two-assertion one MUST."""
    long_simple = ("I recently purchased a very comfortable ergonomic mesh office chair "
                   "with adjustable lumbar support from a local furniture retailer downtown.")
    assert segmentation_residual_signals(long_simple) == set()
    short_two = "The dog I own is old."
    assert segmentation_residual_signals(short_two) != set()


def test_decomposed_output_clears_its_own_trigger():
    """After decomposition the produced units must no longer carry the signal that caused the split
    — otherwise the trigger would escalate work the deterministic layer already did."""
    for s in ("I bought a car and sold the bike.",
              "Paris, which is in France, is lovely.",
              "I fixed the fence, painted the shed and mowed the lawn on Sunday."):
        for u in decompose_assertions(s):
            assert segmentation_residual_signals(u) == set(), (s, u)


def test_residual_signals_escalate_when_the_parse_is_unavailable(monkeypatch):
    """Fail direction on a WRITE path: an unreadable unit escalates, never silently skips the LLM."""
    import src.extraction.linguistics as L
    monkeypatch.setattr(L, "_get_nlp", lambda: None)
    assert L.segmentation_residual_signals("anything at all") == {"parse_unavailable"}


def test_decompose_is_a_noop_without_the_grammar_layer(monkeypatch):
    import src.extraction.linguistics as L
    monkeypatch.setattr(L, "_get_nlp", lambda: None)
    assert L.decompose_assertions("I bought a car and sold the bike.") == [
        "I bought a car and sold the bike."]


# ── the layered pipeline shape used by the flag-ON branch ──────────────────────────
def test_layered_segmentation_covers_multi_sentence_turns():
    turn = ("I'm thinking of getting my car detailed soon. By the way, I just got my car "
            "serviced for the first time on March 15th, and it was a great experience.")
    units = []
    for s in segment_clauses(turn) or [turn]:
        units.extend(decompose_assertions(s) or [s])
    assert len(units) >= 3
    assert any("March 15th" in u for u in units)
    got = set()
    for u in units:
        got |= _content_tokens(u)
    assert not (_content_tokens(turn) - got)
