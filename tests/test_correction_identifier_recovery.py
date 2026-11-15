"""Regression gate for the correction-path identifier-value recovery
(``_recover_alnum_identifier_in_text``) + the postal_code SCALAR rel_type typing
(migration 215).

THE DEFECT: the correction LLM (``_unified_correction_extraction_llm``, used by
/retract/correct) truncates out-of-distribution alphanumeric identifiers the user
re-states ("N2G 3M1" → "n2"), and the engine-grown ``postal_code`` rel_type was
typed tail_types=['ANY'] (relational) so the value became a Location ENTITY
(aliased to the truncated fragment) instead of a scalar string — recall then had
nothing coherent to surface, and each correction made it worse.

THE FIX (subject-agnostic, structural): (1) ``_recover_alnum_identifier_in_text``
recovers the full identifier from the source statement when the LLM returned a
left-anchored fragment of a digit-bearing alnum run; (2) migration 215 types
``postal_code`` as SCALAR so values route to entity_attributes.

NOTE: all fixture values here are synthetic placeholders of a structural shape.
"""
import warnings

import pytest

from src.api.main import _recover_alnum_identifier_in_text

warnings.filterwarnings("ignore")

# ── THE DEFECT: a truncated fragment recovers to the full identifier ──────────────────

@pytest.mark.parametrize("source,fragment,recovered", [
    ("my postal code is X9Y 8Z7", "x9y", "x9y 8z7"),        # LLM returns first token
    ("my postal code is X9Y 8Z7", "x", "x9y 8z7"),           # LLM returns a prefix shard
    ("my confirmation code is AB 451", "ab", "ab 451"),      # non-digit shard + digit run
    ("my serial is QR 1234 S", "qr", "qr 1234 s"),           # trailing single-letter shard
    ("my code is N2G 3M1", "n2", "n2g 3m1"),                 # the canonical truncation shape
    ("my code is N2G 3M1", "n2g", "n2g 3m1"),                # whole first token
])
def test_truncated_fragment_recovers_whole(source, fragment, recovered):
    assert _recover_alnum_identifier_in_text(source, fragment) == recovered


# ── SAFETY: clean values and words are never altered ─────────────────────────────────

@pytest.mark.parametrize("source,fragment", [
    ("my favorite color is dark blue", "blue"),              # no digit → untouched
    ("my blood type is O negative", "negative"),             # word → untouched
    ("my zip is 90210", "90210"),                            # single token → unchanged
    ("the value is 42", "42"),                               # bare number → unchanged
    ("I have a 3 year old dog", "a"),                        # article must not extend into a number
    ("my favorite color is dark blue", "is"),                # plain word must not extend
    ("my router is a UniFi at 192.168.1.1", "unifi"),        # non-fragment word → untouched
])
def test_clean_value_untouched(source, fragment):
    assert _recover_alnum_identifier_in_text(source, fragment) == fragment


# ── ROBUSTNESS: missing/odd inputs are safe (return fragment unchanged) ───────────────

@pytest.mark.parametrize("source,fragment", [
    ("", "n2g"),                  # empty source
    ("my code is X9Y 8Z7", ""),   # empty fragment
    (None, "n2g"),                # None source
    ("my code is X9Y 8Z7", None), # None fragment
    ("nothing here", "n2g"),      # fragment absent from source
    ("my value is blue-red", "blue-red"),  # fragment not purely alnum (hyphen)
])
def test_odd_inputs_safe(source, fragment):
    fallback = fragment if fragment is not None else ""
    assert _recover_alnum_identifier_in_text(source, fragment) in (fragment, fallback)
