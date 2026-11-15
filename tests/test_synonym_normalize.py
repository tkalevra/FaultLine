"""Unit tests for the shared synonym-term normalizer (IMPL-2 §1).

Pure stdlib, no model / DB deps. Covers the litmus, the formatting-merge
equivalences, PRESERVE-token survival, idempotency, case/whitespace/possessive
handling, and empty input.
"""

import pytest

from src.extraction.synonym_normalize import normalize_synonym_term as N


# --------------------------------------------------------------------------- #
# THE LITMUS — the precision contract the whole design hangs on.
# --------------------------------------------------------------------------- #

def test_litmus_ex_wife_is_not_wife():
    # Meaning-bearing modifier must keep the two terms distinct.
    assert N("ex wife") != N("wife")


def test_litmus_x_forms_collapse_to_ex_wife():
    # All formatting variants of the ex-wife abbreviation collapse to one key.
    assert N("X-Wife") == "ex wife"
    assert N("ex wife") == "ex wife"
    assert N("x wife") == "ex wife"
    assert N("x-wife") == "ex wife"
    assert N("xwife") == "ex wife"
    assert N("ex-wife") == "ex wife"
    # ...and they are all equal to each other.
    forms = {N("X-Wife"), N("ex wife"), N("x wife"),
             N("x-wife"), N("xwife"), N("ex-wife")}
    assert forms == {"ex wife"}


# --------------------------------------------------------------------------- #
# MERGE (M1–M5) — formatting-only collapses.
# --------------------------------------------------------------------------- #

def test_m1_lowercase():
    assert N("Wife") == "wife"
    assert N("TrueNAS") == "truenas"


def test_m2_trim_and_collapse_whitespace():
    # Use a non-determiner multi-token term so this tests ONLY whitespace
    # collapse (M2), not the leading-determiner strip (M6, tested below).
    assert N("  home   server ") == "home server"
    assert N("\tboss\n") == "boss"


def test_m6_strip_leading_determiner():
    # Leading the/a/an is stripped so capture matches the resolution side
    # (which removes determiners as stop-words). Referent is unchanged.
    assert N("the box") == "box"
    assert N("a server") == "server"
    assert N("an apple") == "apple"
    assert N("The Box") == "box"
    # Leading-position ONLY: a determiner that isn't first stays put.
    assert N("box of the cat") == "box of the cat"
    # A bare determiner is the sole token → preserved (not a synonym anyway).
    assert N("the") == "the"
    # Does not strip the meaning-bearing preserve tokens after the determiner.
    assert N("the ex wife") == "ex wife"


def test_m3_hyphen_and_underscore_to_space():
    assert N("shoe-box") == "shoe box"
    assert N("shoe_box") == "shoe box"


def test_m4_abbrev_is_whole_token_only_not_substring():
    # x -> ex only as a whole token; must NOT corrupt a word that starts with x.
    assert N("xavier") == "xavier"
    assert N("box") == "box"          # the classic "bo"+"ex" corruption guard
    assert N("x") == "ex"


def test_m5_possessive_and_edge_punctuation():
    assert N("wife's") == "wife"
    assert N('"box"') == "box"
    assert N("boss!") == "boss"


def test_plural_s_is_not_stripped_in_pilot():
    # Aggressive stemming is forbidden; boxes != box unless registered.
    assert N("boxes") == "boxes"
    assert N("boxes") != N("box")


# --------------------------------------------------------------------------- #
# PRESERVE — each meaning-bearing modifier survives and stays distinct.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("modifier", ["ex", "former", "late", "step", "half"])
def test_preserve_modifier_survives_and_stays_distinct(modifier):
    out = N(f"{modifier} wife")
    assert modifier in out.split()          # token survived
    assert out != N("wife")                 # distinct referent preserved


def test_preserve_in_law_canonicalized_and_kept():
    out = N("mother-in-law")
    assert out == "mother in law"
    assert out != N("mother")


# --------------------------------------------------------------------------- #
# IDEMPOTENCY — normalize(normalize(x)) == normalize(x).
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw", [
    "X-Wife", "ex wife", "xwife", "  the   Wife's ", "mother-in-law",
    "TrueNAS", "shoe_box", '"box"', "former boss", "x", "boxes",
])
def test_idempotent(raw):
    once = N(raw)
    assert N(once) == once


# --------------------------------------------------------------------------- #
# EMPTY / WHITESPACE input.
# --------------------------------------------------------------------------- #

def test_empty_and_whitespace_input():
    assert N("") == ""
    assert N("   ") == ""
    assert N("\t\n") == ""
    assert N(None) == ""  # falsy guard
