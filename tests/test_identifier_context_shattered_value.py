"""Regression gate for the SHATTERED-IDENTIFIER value capture on the DB-gated
identifier-context path — same value-truncation defect CLASS as the "O negative"
→ "O" drop (gated in test_spine_allergy_relational_predicate.py), but for
reference/code identifiers whose spaCy shards carry no recognised modifier label.

THE DEFECT: an alphanumeric identifier that carries internal whitespace (a code
like "X9Y 8Z7") is parsed by spaCy (en_core_web_sm) into multiple tokens attached
across arbitrary dependency labels (punct/conj/acomp on a sibling or the root) —
OUTSIDE the complement's subtree. The identifier-context capture
(``_identifier_context_binding`` inside ``derive_sentence_facts``) sliced
``comp.subtree`` for the value, so it kept only the head shard and the dropped
shard leaked to residue. The engine's ``identifier_noun`` cue class (seeded by
migration 186 with ticket/case/docket/code/...; per-tenant, DB-grown) had ALREADY
established the noun signals a reference/code — so detection is the engine's job;
the rebuild is a structural strength applied only on that DB-gated path.

THE FIX (subject-agnostic, Rule-2 compliant — detection owned by the growth
engine, NOT an in-code heuristic): on the identifier-context path, extend the
value slice across the contiguous alnum fragment run (``_alnum_ident_run_tokens``)
and CLAIM every shard so the residue guard never re-reads a dropped one. Genuine
word values ("dark blue") never reach this path (their nouns are not identifier
cues), and the generic complement-value path stays pure-grammar. See
src/extraction/linguistics.py::_identifier_context_binding + _chain_identifier_context.

NOTE: all fixture values here are synthetic placeholders of a structural shape —
they are NOT any user's real data.
"""
import warnings

import pytest

from src.extraction.linguistics import (
    _alnum_ident_run_span,
    _alnum_ident_run_tokens,
    _ident_fragment_admissible,
    _is_ident_fragment,
    derive_sentence_facts,
    linguistics_available,
)

warnings.filterwarnings("ignore")  # spaCy/torch CUDA warnings are noise in the test env

pytestmark = pytest.mark.skipif(
    not linguistics_available(),
    reason="spaCy linguistic layer unavailable (SPACY_MODEL unset) — spine seams no-op",
)


def _facts(text):
    """Derive facts for a single statement; return list of (subject, rel_type, object)."""
    return [(f.subject, f.rel_type, f.object) for f in derive_sentence_facts(text, "user")]


# ── THE DEFECT: the full identifier value is captured, no shard dropped ────────────────

@pytest.mark.parametrize("text,full_value", [
    ("my code is X9Y 8Z7", "x9y 8z7"),             # whitespace-shattered across punct/attr
    ("my code is X9Y8Z7", "x9y8z7"),                # single token
    ("my confirmation code is AB 451", "ab 451"),   # strong cue head
    ("my number is QR 1234 S", "qr 1234 s"),        # suffix head + strong compound
    ("my ticket number is 1234567", "1234567"),     # bare numeric id (no shape-atomic claim)
    ("my case number is T-99-X", "t-99-x"),         # hyphenated separators
])
def test_identifier_value_captured_whole(text, full_value):
    facts = _facts(text)
    # the FULL value lands on the user as a reference/code scalar (rel may be the generic
    # has_reference_id or a cue-grown specific rel like confirmation_code — assert by value).
    assert ("user", None, full_value) in [ (s, None, o) for (s, _r, o) in facts ], \
        f"full value {full_value!r} not captured on user for {text!r}; got {facts}"
    # AND no truncated shard survived as a captured value — ON ANY SUBJECT.
    #
    # ⚠️ THIS ASSERTION WAS SUBJECT-SCOPED (`o == shard AND s == "user"`) AND IT LIED. A shard that
    # leaked onto a DIFFERENT subject passed green: this file's own fixture "my code is X9Y 8Z7"
    # derived the good scalar PLUS ``(code, also_known_as, 'x9y')`` — the truncated head shard
    # re-read by a naming chain and bound as an alias of the cue noun. The control proves it was
    # shatter-specific (unshattered "X9Y8Z7" emits no such alias). A negative assertion about a
    # dropped fragment must assert the fragment appears on NO subject at all.
    for _shard in ({p for p in full_value.split()} - {full_value}):
        assert not any(o == _shard for (_s, _r, o) in facts), \
            f"truncated shard {_shard!r} still captured for {text!r}; got {facts}"


def test_postal_shape_shards_leak_to_no_subject():
    """RESIDUAL 2 (token-shard variant). The value is a verbatim CHAR span ("X9G 8Z7") but spaCy
    tokenises it as ``X9`` | ``G`` | ``8Z7`` — so the whitespace split of the VALUE does not name
    the pieces a sibling chain actually re-files. Measured before the fix, this ONE sentence leaked
    TWO artifacts on two different subjects: ``(code, has_state, 'x9')`` and
    ``(postal code, also_known_as, 'g')``. Both are halves of a value already captured whole."""
    facts = _facts("my postal code is X9G 8Z7")
    assert ("user", "has_reference_id", "x9g 8z7") in facts, f"value lost; got {facts}"
    for _shard in ("x9", "g", "8z7", "x9g"):
        assert not any(o == _shard for (_s, _r, o) in facts), \
            f"identifier shard {_shard!r} leaked to a sibling chain; got {facts}"


def test_unshattered_control_emits_no_alias():
    """CONTROL for the leak above: the SAME identifier written without internal whitespace mints no
    alias at all, which is what proves the leak is shatter-specific rather than chain-normal."""
    facts = _facts("my code is X9Y8Z7")
    assert ("user", "has_reference_id", "x9y8z7") in facts, f"value lost; got {facts}"
    assert not any(r == "also_known_as" for (_s, r, _o) in facts), f"got {facts}"


# ── RESIDUAL 1: a parse artifact must never be stored as the user's identifier VALUE ───

@pytest.mark.parametrize("text,value", [
    # a short capitalised token ADJACENT to a real identifier is NOT a shard of it. "I" here is the
    # nsubj of its own clause ("I think"); absorbing it stored `12 i` as a user-stated reference id.
    ("my case number is 12 I think", "12"),
    ("my ticket number is 4471 I guess", "4471"),
    ("my case number is 88 It seems", "88"),
])
def test_neighbouring_clause_token_not_absorbed_into_value(text, value):
    facts = _facts(text)
    ids = [o for (s, r, o) in facts if s == "user" and r == "has_reference_id"]
    assert ids, f"identifier not captured at all for {text!r}; got {facts}"
    for _got in ids:
        assert _got == value, \
            f"stored a parse artifact as the identifier value: {_got!r} (want {value!r}); got {facts}"


def test_shape_predicate_carries_no_signal_only_grammar_does():
    """THE MEASUREMENT THAT SETTLES THE DESIGN — do not re-tune the shape rule.

    ``G`` in "X9G 8Z7" MUST join; ``I`` in "12 I think" MUST NOT. They are ORTHOGRAPHICALLY
    IDENTICAL — both single alnum uppercase characters — so ``_is_ident_fragment`` returns True for
    BOTH and no tightening of a length/case/shape rule can separate them. Only the grammar can:
    ``G`` is PROPN/``attr`` under the copula, ``I`` is PRON/``nsubj`` of its own verb."""
    import spacy
    nlp = spacy.load("en_core_web_sm")
    g = next(t for t in nlp("my postal code is X9G 8Z7") if t.text == "G")
    i_tok = next(t for t in nlp("my case number is 12 I think") if t.text == "I")

    # identical to the SHAPE predicate …
    assert _is_ident_fragment(g) is True
    assert _is_ident_fragment(i_tok) is True
    assert (len(g.text), g.text.isupper()) == (len(i_tok.text), i_tok.text.isupper())
    # … and cleanly separated by the GRAMMAR
    assert (g.pos_, i_tok.pos_) == ("PROPN", "PRON")
    assert _ident_fragment_admissible(g) is True, "interior letter shard must still join"
    assert _ident_fragment_admissible(i_tok) is False, "a token subjecting its own clause must not"


def test_no_digit_requirement_the_interior_shard_has_none():
    """The obvious fix — "require a digit in every shard" — would destroy the very case the rebuild
    exists for: spaCy splits "X9G" into ``X9`` + ``G`` (its SI-unit suffix rule), so the interior
    shard carries no digit at all. The whole run must still rebuild."""
    import spacy
    nlp = spacy.load("en_core_web_sm")
    doc = nlp("my postal code is X9G 8Z7")
    g = next(t for t in doc if t.text == "G")
    assert not any(c.isdigit() for c in g.text)
    assert [t.text for t in _alnum_ident_run_tokens(g)] == ["X9", "G", "8Z7"]
    twelve = next(t for t in nlp("my case number is 12 I think") if t.text == "12")
    assert _alnum_ident_run_tokens(twelve) == []


def test_matrix_verb_to_the_LEFT_does_not_truncate():
    """The clause test is DIRECTIONAL and must stay so. "I THINK my postal code is X9G 8Z7" embeds
    the copula under a matrix verb, so ``G`` has a VERB ancestor — but it opens BEFORE the
    identifier, not after it. A blanket "no VERB ancestor" rule would truncate this ordinary
    sentence; only a verb to the RIGHT signals a clause the shard could belong to instead."""
    facts = _facts("I think my postal code is X9G 8Z7")
    assert ("user", "has_reference_id", "x9g 8z7") in facts, f"value truncated; got {facts}"


def test_join_width_is_zero_or_one_space():
    """Contiguity condition: a zero-width join undoes spaCy's own suffix split (it cannot cross a
    word boundary) and one space is the shatter we are repairing. A wider gap is layout."""
    import spacy
    nlp = spacy.load("en_core_web_sm")
    tight = nlp("my code is X9Y 8Z7")
    assert [t.text for t in _alnum_ident_run_tokens(
        next(t for t in tight if t.text == "X9Y"))] == ["X9Y", "8Z7"]
    wide = nlp("my code is X9Y  8Z7")          # two spaces — not one identifier
    assert _alnum_ident_run_tokens(next(t for t in wide if t.text == "X9Y")) == []


def test_admissibility_falls_back_to_shape_without_grammar():
    """Fail-safe: a token exposing no grammatical attributes keeps the pure-shape contract."""
    assert _ident_fragment_admissible(_StubTok("G")) is True
    assert _ident_fragment_admissible(_StubTok("8Z7")) is True
    assert _ident_fragment_admissible(_StubTok("blue")) is False


# ── REGRESSION: word values stay on the generic path, never routed as identifiers ─────

@pytest.mark.parametrize("text", [
    "my favorite color is dark blue",   # not an identifier noun → generic complement path
    "my favorite color is blue",
])
def test_word_value_not_routed_as_identifier(text):
    facts = _facts(text)
    # a color value must NEVER be captured as a reference/code identifier
    assert not any(r == "has_reference_id" for (_s, r, _o) in facts), \
        f"word value routed to has_reference_id for {text!r}; got {facts}"


# ── ADDRESS SAFETY: a real-word span is never merged into an identifier run ────────────

def test_address_span_not_collapsed_into_ident_run():
    """The alnum-fragment rebuild is gated on identifier-noun context, so a street
    span (not an identifier noun) is never rewritten into an identifier value."""
    facts = _facts("my address is 12 example street")
    # captured fine (as an address attribute), never as a reference id
    assert not any(r == "has_reference_id" for (_s, r, _o) in facts)
    assert any("12 example street" == o or "example street" in o for (_s, _r, o) in facts)


# ── UNIT: the structural fragment classifier + run tokeniser (subject-agnostic) ────────
#
# These helpers read ONLY tok.text, so a lightweight stub is a faithful isolated test.

class _StubTok:
    __slots__ = ("text",)

    def __init__(self, text):
        self.text = text


@pytest.mark.parametrize("word,is_frag", [
    # digit-bearing identifier fragments
    ("X9Y", True), ("8Z7", True), ("1234", True), ("90210", True),
    # short non-lowercase identifier fragments
    ("AB", True), ("QR", True), ("RX", True), ("S", True), ("G", True),
    # real words — never fragments
    ("negative", False), ("blue", False), ("example", False), ("street", False),
    ("avenue", False), ("dark", False),
    ("UniFi", False),   # len 5 → not a fragment (structured-atomic routing gate stays safe)
])
def test_is_ident_fragment_structural(word, is_frag):
    assert _is_ident_fragment(_StubTok(word)) is is_frag


def test_run_span_and_tokens_agree():
    """The span helper and the token helper describe the same run."""
    import spacy
    nlp = spacy.load("en_core_web_sm")
    doc = nlp("the code is X9Y 8Z7 today")
    # find a digit-bearing fragment token
    tok = next(t for t in doc if t.text == "X9Y")
    toks = _alnum_ident_run_tokens(tok)
    span = _alnum_ident_run_span(tok)
    assert toks and [t.text for t in toks] == ["X9Y", "8Z7"]
    assert span == "x9y 8z7"
