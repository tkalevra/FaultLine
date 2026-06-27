"""Deterministic synonym-term normalizer — the precision keystone (IMPL-2 §1).

ONE function, ONE module, imported by BOTH capture (IMPL-2) and resolution
(IMPL-3). It produces the exact lexical key `entity_synonyms.term` is matched
on. If the two callers ever diverge, every table match silently fails — so this
is the single source of truth: same import, both sides.

Contract (stdlib-only, no DB, no LLM, pure ``str -> str``):

  * Deterministic and **idempotent**: ``normalize(normalize(x)) == normalize(x)``.
  * Collapses *how a word is written* (FORMATTING) and PRESERVES *what a word
    means* (meaning-bearing modifiers).

The MERGE / PRESERVE split is the whole point. See IMPL-2 §1.2.

THE WIFE/EX-WIFE TRAP (why the PRESERVE list exists)
----------------------------------------------------
``ex wife`` and ``wife`` are DIFFERENT referents — the ex-wife is not the wife.
If we stripped ``ex`` as noise we would silently fold two distinct people into
one synonym key and mislink. So ``ex / former / late / step / half / in-law``
are never deleted; they stay as whole tokens in the term string. We only
canonicalize their SPELLING (``x -> ex``, ``in-law -> in law``). The litmus the
whole design hangs on::

    normalize("ex wife") != normalize("wife")
    normalize("x-wife") == normalize("x wife") == normalize("xwife") == "ex wife"

NO RECURSIVE MATCHING (Pitfall 4)
---------------------------------
Every transform is WHOLE-TOKEN on a lowercased, whitespace-split term. We never
do naive substring replacement, so ``x -> ex`` cannot fire inside ``xavier``
(it would corrupt it to ``exavier``) and ``box`` is never mangled into ``bo``.
"""

# M4 abbreviation expansion — the ONLY abbreviation map. Whole-token only.
# Do NOT grow this into a general abbreviation engine (IMPL-2 §1.2 M4).
_ABBREV = {"x": "ex"}

# Glued abbreviation forms: a single token that fuses the abbreviation onto a
# real word, e.g. "xwife" -> "ex wife". We CANNOT split "x" + arbitrary-suffix
# generally — that corrupts names ("xavier" -> "ex avier"), the exact Pitfall-4
# recursive-matching trap. So the glued split is an EXPLICIT, closed allow-list
# of documented glued forms only. The spec (IMPL-2 §1.2 M4) names "xwife"; we
# keep a small kinship set in the same spirit. Anything not listed is left as a
# whole token and never split.
_GLUED_FORMS = {
    "xwife": "ex wife",
    "xhusband": "ex husband",
    "xgirlfriend": "ex girlfriend",
    "xboyfriend": "ex boyfriend",
    "xspouse": "ex spouse",
    "xpartner": "ex partner",
}

# Meaning-bearing modifiers we canonicalize-but-PRESERVE. These change the
# referent and must never be stripped or folded into the bare term. This set is
# documentation/guard only — the normalizer never deletes a meaning-bearing token;
# reviewers check this list stays intact. ("in-law" arrives here as "in law" post-M3.)
_SEMANTIC_PRESERVE = frozenset(
    {"ex", "former", "late", "step", "half", "in", "law"}
)

# M6 — leading-determiner strip. The resolution side removes {the, a, an} as
# stop-words before it looks up entity_synonyms.term, but capture stored the raw
# normalized term — so "the box" was stored verbatim while resolution only ever
# queried "box" → exact-match miss → the synonym never resolved. Determiners are
# NOT referent-bearing ("the box" == "box"), so stripping a LEADING determiner here
# makes BOTH sides produce the same key. Leading-position ONLY, and never if it is
# the sole token (a bare "the" → "" is not a synonym). Does not touch
# _SEMANTIC_PRESERVE tokens.
_LEADING_DETERMINERS = frozenset({"the", "a", "an"})

# Edge punctuation stripped per-token (M5). The apostrophe is handled separately
# so we can detect and remove the possessive "'s" before this strip runs.
_EDGE_PUNCT = ".,!?\"'"


def _expand_token(tok: str) -> str:
    """Whole-token M4 expansion, plus the documented glued ``xwife`` case.

    Whole-token: ``x`` -> ``ex``. Glued: ONLY an explicit, closed allow-list of
    documented glued forms (``xwife`` -> ``ex wife``) is split. We deliberately
    do NOT generically split ``x`` + arbitrary-suffix: that would corrupt
    ``xavier`` into ``ex avier`` — the Pitfall-4 recursive-matching trap. A name
    that merely starts with ``x`` is left untouched; only ``x`` itself, or a
    listed glued form, expands.

    Returns a possibly multi-token string (space-joined) for the glued case.
    """
    # Whole-token abbreviation: exact match wins first (e.g. "x" -> "ex").
    if tok in _ABBREV:
        return _ABBREV[tok]
    # Glued abbreviation: explicit closed allow-list only (no generic split).
    if tok in _GLUED_FORMS:
        return _GLUED_FORMS[tok]
    return tok


def normalize_synonym_term(raw: str) -> str:
    """Normalize a raw synonym term to its canonical lexical key.

    MERGE (formatting-only, collapsed): M1 lowercase, M2 trim+collapse internal
    whitespace, M3 hyphen/underscore -> space, M4 ``x``-style abbrev -> ``ex``
    (whole-token), M5 strip possessive ``'s`` and edge punctuation.

    PRESERVE (meaning-bearing, kept as whole tokens): ``ex / former / late /
    step / half / in-law`` (and ``x`` once expanded to ``ex``).

    Idempotent and deterministic. Empty / whitespace-only input -> ``""``.
    """
    if not raw:
        return ""

    # M1 + M2 (outer trim) + M3 (hyphen/underscore -> space).
    s = raw.strip().lower().replace("-", " ").replace("_", " ")

    tokens = []
    # split() also collapses any run of internal whitespace (M2).
    for tok in s.split():
        # M5: strip possessive "'s" first (grammatical case on the qualifier,
        # not part of the term), then strip residual edge punctuation.
        if tok.endswith("'s"):
            tok = tok[:-2]
        tok = tok.strip(_EDGE_PUNCT)
        # NOTE: plain plural-s is intentionally NOT stripped in the pilot
        # ("boxes" != "box"); aggressive stemming would mislink (IMPL-2 §1.3).
        if not tok:
            continue
        # M4: whole-token (+ documented glued) abbreviation expansion. This may
        # yield a multi-token string for the glued "xwife" case.
        expanded = _expand_token(tok)
        tokens.extend(expanded.split())

    # M6: strip a LEADING determiner so capture matches the resolution side
    # (which removes the/a/an as stop-words before lookup). Leading-position only,
    # and never the sole token.
    if len(tokens) > 1 and tokens[0] in _LEADING_DETERMINERS:
        tokens = tokens[1:]

    return " ".join(tokens)
