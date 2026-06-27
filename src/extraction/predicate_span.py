"""Deterministic predicate verb-lift — RUNG-1 extractor for novel activity rels.

This is the "Option B" verb-lift from ``DEV/fix-reports/RC-route-growth-ingest.md`` and the
open mechanism in ``DEV/DESIGN-trigger-span-gliner2-extraction.md`` (lines 50-55): given a
fact-bearing span and a GLiNER2-found entity PAIR (subject, object), lift the user's own
connecting verb phrase between the pair verbatim, then normalize it deterministically through
``src/ontology/canonical.py::normalize_rel`` (RUNG 1 morphology — "fixed" → "fix"). The
result is a candidate ``rel_type`` token whose NAME is the user's own verb.

HARD CONSTRAINTS (see CLAUDE.md + the RC report):
- "USER IS TRUTH": the predicate is the user's verbatim verb (deterministically normalized
  only). NO LLM names the relation. GLiNER2 is NOT involved here — it stays an entity-pair
  finder + typer upstream (Pitfall 11; GLiNER2 untouched).
- PURE module: NO FastAPI, NO GLiNER2, NO ``import main``, NO DB, NO LLM. Stdlib + the pure
  ``canonical.normalize_rel`` only. Schema-agnostic (no tenant read here — canonical
  RESOLUTION of the lifted token happens at the call site with the tenant schema).
- Scalar-tail caveat (RC §5): NEVER fold a trailing date/number/scalar into the verb
  ("fixed ... three weeks ago" must NOT become ``fixed_three_weeks_ago``). The lift takes
  ONLY the predicate between the entity pair; trailing temporal/numeric tails are scalars,
  handled by the existing scalar lane. We lift the connecting span, not the whole sentence.
- Fail-safe: any miss (entity not found in span, empty verb, no content) returns ``None`` and
  the caller falls back to today's label-scorer edges.
"""

from __future__ import annotations

import re
from typing import Optional

from src.ontology.canonical import (
    normalize_rel,
    _AUXILIARIES,
    _LEADING_DROP,
    _LIGHT_VERBS,
    _KEEP_PREPOSITIONS,
    _lemmatize,
)

_WS = re.compile(r"\s+")

# Common adverbs that sit between subject and verb ("I JUST fixed", "I FINALLY repaired").
# Dropped so the lift starts at the real verb, not the adverb.
_LEADING_ADVERBS: frozenset[str] = frozenset({
    "just", "finally", "already", "recently", "also", "then", "once", "even",
    "really", "actually", "still", "now", "today", "yesterday",
})

# Number words — a connecting region whose head is a number ("three weeks ago") is a SCALAR
# tail, not a predicate. Reject so we never bake a scalar into a rel (RC §5 scalar-tail caveat).
_NUMBER_WORDS: frozenset[str] = frozenset({
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "dozen", "hundred", "thousand", "million",
    "first", "second", "third", "couple", "few", "several", "many", "some",
})
_DIGIT = re.compile(r"^\d")

# Tokens that, if they are ALL that sits between the pair, mean there is no real verb to lift
# (the pair is adjacent / connected only by a copula or determiner). We refuse to mint from
# these so we never grow a junk rel like ``the`` or a bare copula ``be``.
_EMPTY_CONNECTORS: frozenset[str] = frozenset({
    "the", "a", "an", "of", "and", "or", "to", "with", "for", "in", "on", "at",
    "is", "are", "was", "were", "be", "been", "being", "am", "'s", "s",
})

# Naming / dubbing verbs: the predicative naming construction ("a dog NAMED Rex", "a server
# CALLED Atlas") is NOT an activity predicate — it assigns a PROPER NAME to the head noun and is
# OWNED by the deterministic naming seam (``main._detect_naming_states`` → ``also_known_as``). The
# verb-lift must SKIP it so it never mints the over-stripped junk rel ``nam`` (the rung-1 stemmer
# turns "named" → "nam" — see canonical._suffix_lemma). We match the SURFACE inflected forms of the
# two naming verbs directly (the over-strip makes the lemma "nam" unreliable to key on), a bounded
# grammatical naming class / language primitive — NOT a domain word-list. Mirrors
# ``linguistics._NAMING_VERB_LEMMAS`` ({"name","call"}); the surface forms are their inflections.
_NAMING_VERB_SURFACE: frozenset[str] = frozenset({
    "name", "names", "named", "naming",
    "call", "calls", "called", "calling",
})

# Catenative / mental-state verbs: when followed by "to" they introduce a complement verb
# (desire/intent), NOT an activity — "like to graze", "want to buy", "plan to visit". Lemmas.
_CATENATIVE: frozenset[str] = frozenset({
    "like", "love", "hate", "want", "wish", "hope", "plan", "try", "need", "intend",
    "prefer", "enjoy", "expect", "decide", "choose", "mean", "aim", "agree", "offer",
    "promise", "refuse", "tend", "happen", "manage", "fail", "seem", "appear",
})

# Mental-state / cognition / desire verbs that take a "of"/"about" complement to express an
# UNREALIZED intention or opinion, not a stated fact — "thinking OF getting a cow", "dreaming
# OF a farm", "consider buying", "hope FOR rain". These are a GRAMMATICAL class (cognition verbs
# governing a gerund/NP complement), bounded as a language primitive; NOT a domain/world list and
# NOT grown by domain. "USER IS TRUTH": an intention/opinion is not a thing the user did.
_MENTAL_STATE: frozenset[str] = frozenset({
    "think", "consider", "dream", "hope", "wish", "plan", "imagine", "contemplate",
    "ponder", "fancy", "intend", "want", "wonder",
})
# Complementizers that, after a mental-state head, introduce the (unrealized) complement.
_INTENT_COMPLEMENTIZERS: frozenset[str] = frozenset({"to", "of", "about", "for"})


def _find_span_position(span_lower: str, needle: str) -> Optional[tuple[int, int]]:
    """Return (start, end) char offsets of ``needle`` in ``span_lower`` (word-boundary aware),
    or None. Both inputs are already lowercased. Prefers a whole-word match; falls back to a
    plain substring so multi-word / punctuation-adjacent entities still locate."""
    needle = (needle or "").strip().lower()
    if not needle:
        return None
    # Word-boundary match first (avoids "i" matching inside "fixed").
    try:
        m = re.search(r"\b" + re.escape(needle) + r"\b", span_lower)
        if m:
            return (m.start(), m.end())
    except re.error:
        pass
    idx = span_lower.find(needle)
    if idx >= 0:
        return (idx, idx + len(needle))
    return None


def lift_predicate(span: str, subject: str, object_: str) -> Optional[str]:
    """Lift the user's connecting verb between (subject, object) in ``span`` → normalized rel token.

    Deterministic + pure. Steps:
      1. Locate subject and object char-spans inside ``span`` (lowercased, word-boundary aware).
      2. Take the text BETWEEN the inner edges of the pair (subject-end → object-start). If the
         object precedes the subject (e.g. "the fence I fixed"), take subject-start → object-end
         the other way and still lift the connecting span between them.
      3. ``normalize_rel`` that connecting span (RUNG-1 morphology: strips leading
         pronouns/determiners/auxiliaries, lemmatizes the head verb, keeps load-bearing
         prepositions). "I fixed the" → "fix"; "had an issue with" → "has_issue".
      4. Reject empty / pure-connector results (returns None — caller keeps today's behavior).

    Returns the snake_cased normalized rel_type token, or None on any miss (fail-safe).

    NOTE: this lifts ONLY the connecting predicate between the pair. Trailing scalars
    ("three weeks ago") fall OUTSIDE the pair and are never folded in (scalar-tail caveat).
    """
    if not span or not subject or not object_:
        return None

    span_lower = span.lower()
    s_pos = _find_span_position(span_lower, subject)
    o_pos = _find_span_position(span_lower, object_)
    if not s_pos or not o_pos:
        return None

    s_start, s_end = s_pos
    o_start, o_end = o_pos

    # Determine the connecting region between the inner edges of the two mentions, in surface
    # order. Whichever mention comes first, the predicate sits between its end and the other's
    # start. Overlapping mentions (same span) → no connector → bail.
    if s_end <= o_start:
        between = span[s_end:o_start]
    elif o_end <= s_start:
        between = span[o_end:s_start]
    else:
        return None

    between = between.strip()
    if not between:
        return None

    # Reduce the connecting region to the MINIMAL predicate head so we never drag object nouns,
    # adjectives, or scalar tails ("three weeks ago") into the rel token (RC §5 scalar-tail
    # caveat). Tokenize the connecting span and walk it:
    bare = [t for t in _WS.split(re.sub(r"[^a-z0-9'\s]+", " ", between.lower())) if t]
    if not bare:
        return None

    # 1. Drop leading pronouns/determiners and adverbs (NOT auxiliaries yet — a leading light
    #    verb "had"/"has" must survive as the head).
    i = 0
    while i < len(bare) and (bare[i] in _LEADING_DROP or bare[i] in _LEADING_ADVERBS):
        i += 1
    rest = bare[i:]
    if not rest:
        return None

    # 2. Strip leading auxiliaries (tense helpers) — but STOP at a light verb (its object
    #    carries the meaning). Mirrors canonical.normalize_rel's auxiliary handling.
    while len(rest) > 1 and rest[0] in _AUXILIARIES and _lemmatize(rest[0]) not in _LIGHT_VERBS:
        rest = rest[1:]
    if not rest:
        return None

    head = rest[0]

    # Scalar-tail guard: a head that is a number/digit ("three weeks ago") is a SCALAR tail
    # between the pair, NOT an activity verb. Reject so a scalar never becomes a rel (RC §5).
    if head in _NUMBER_WORDS or _DIGIT.match(head):
        return None

    head_lemma = _lemmatize(head)

    # Naming/dubbing verb ("named"/"called") → the naming seam owns this construction (it mints
    # the valid ``(head-noun, also_known_as, ProperName)`` edge). Skip so the verb-lift never
    # produces the over-stripped junk rel ``nam`` for a span the naming seam already handles.
    # Keyed on the SURFACE head (the rung-1 stemmer mangles "named"→"nam", so the lemma is an
    # unreliable key here). "named AFTER X" (a different relation, prep tail) is NOT collapsed —
    # the bare naming verb is the skip target; a following load-bearing prep keeps it distinct.
    if head in _NAMING_VERB_SURFACE and not (
        len(rest) > 1 and rest[1] in _KEEP_PREPOSITIONS
    ):
        return None

    # Catenative / mental-state verb + "to" introduces a COMPLEMENT verb, not an activity, and
    # expresses DESIRE/INTENT ("like TO graze", "want TO buy", "plan TO visit") — not a stated
    # fact. Reject so we never mint a junk modal rel (the live "like_to" junk) nor store an
    # unrealized intention as if it happened (USER IS TRUTH: only what the user actually did).
    if head_lemma in _CATENATIVE and len(rest) > 1 and rest[1] == "to":
        return None

    # Mental-state / cognition verb governing a gerund/NP complement via "of"/"about"/"to"/"for"
    # ("thinking OF getting a cow", "dreaming OF a farm", "considering TO buy"). This is an
    # UNREALIZED intention or opinion, not a stated fact — reject so we never mint a junk modal
    # rel like the live "think_of | brown swiss" (USER IS TRUTH: only what the user actually did).
    # Bounded GRAMMATICAL class (cognition verb + complementizer); not grown by domain.
    if head_lemma in _MENTAL_STATE and len(rest) > 1 and rest[1] in _INTENT_COMPLEMENTIZERS:
        return None

    # If the verb head is pure connector/copula/auxiliary with nothing meaningful, bail — there
    # is no real activity predicate to mint (avoids growing a bare "be"/"the" rel).
    if head in _EMPTY_CONNECTORS and head_lemma not in _LIGHT_VERBS:
        return None

    # 3. Build the MINIMAL predicate phrase to hand to normalize_rel:
    #    - light verb ("had an issue with") → head + following object noun + load-bearing prep
    #      (normalize_rel folds this into has_issue); take up to the first content noun.
    #    - regular verb → head ALONE, plus an immediately-adjacent load-bearing preposition
    #      ("lives in" → lives_in). Stop before object nouns / adjectives / scalars.
    if head_lemma in _LIGHT_VERBS:
        # Hand the light verb + the rest up to (and including) the first content noun so
        # normalize_rel's light-verb fold runs; trailing scalars after the noun are ignored
        # because normalize_rel folds only the FIRST content noun.
        phrase_tokens = [head]
        for tok in rest[1:]:
            phrase_tokens.append(tok)
            if tok not in _AUXILIARIES and tok not in _KEEP_PREPOSITIONS \
                    and tok not in _LEADING_DROP:
                break  # first content noun reached — stop (don't drag scalar tail)
        phrase = " ".join(phrase_tokens)
    else:
        phrase = head
        # Keep ONE immediately-following load-bearing preposition (lives_in ≠ lives_at).
        if len(rest) > 1 and rest[1] in _KEEP_PREPOSITIONS:
            phrase = f"{head} {rest[1]}"

    token = normalize_rel(phrase)
    if not token:
        return None
    if token in _EMPTY_CONNECTORS:
        return None
    return token


def lift_edges_from_entities(
    span: str,
    entities_by_type: dict,
    *,
    max_pairs: int = 12,
) -> list[dict]:
    """Build verb-lifted candidate edges from a span + GLiNER2 ``extract_entities`` output.

    ``entities_by_type`` is the GLiNER2 ``{ENTITY_TYPE: [name, ...]}`` map (the ``"entities"``
    sub-dict of ``extract_entities``). For every ordered entity pair found in the span, lift the
    connecting verb (``lift_predicate``) and, on success, emit a candidate edge:

        {"subject", "object", "rel_type", "subject_type", "object_type",
         "confidence": 0.8, "fact_provenance": "user_stated"}

    PURE: no DB, no GLiNER2, no LLM. The rel_type here is the RUNG-1 normalized USER VERB; the
    CALLER must still run it through ``canonical.resolve_canonical(schema=<tenant>)`` to collapse
    a known synonym (RUNG 2/3) before deciding it is novel. Provenance ``user_stated`` /
    confidence 0.8 mirror the ``source="mcp"`` ingest floor so the edge flows the growth path.

    GOVERNANCE — "USER IS TRUTH" (NOT full pairwise): a verb is bound ONLY to the entities it
    ADJACENTLY governs in surface order — each consecutive (subject, object) pair, in the order
    they appear in the span. Full pairwise would FABRICATE facts the user never stated: from
    "I fixed the fence … where my goats graze" it would emit both (i, fix, fence) AND
    (i, fix, goats) — but the user fixed the FENCE, not the goats; "goats" is reached only by
    leaping over the intervening "fence". A verb cannot govern past an intervening entity, so we
    bind consecutive mentions only. We PREFER A MISS OVER A WRONG FACT (fronted-object / ditran-
    sitive constructions may be missed; they are never fabricated). Reframe/atomize upstream
    further reduces multi-entity spans so this stays high-recall.

    Fail-safe: returns [] on any structural problem; bad pairs are skipped, never raised.
    """
    if not span or not entities_by_type:
        return []

    span_lower = span.lower()
    # Flatten to (position, name, TYPE), keeping ONLY entities locatable in the span, then sort
    # by surface position so adjacency = governance order.
    located: list[tuple[int, str, str]] = []
    try:
        for etype, names in entities_by_type.items():
            if not names or not isinstance(names, list):
                continue
            for nm in names:
                nm = (nm or "").strip()
                if not nm:
                    continue
                pos = _find_span_position(span_lower, nm)
                if pos:
                    located.append((pos[0], nm, (etype or "").upper()))
    except Exception:
        return []

    if len(located) < 2:
        return []

    located.sort(key=lambda x: x[0])  # surface order

    edges: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    # Bind each verb to span-ADJACENT (consecutive) entities only — subject precedes object in
    # surface order. No leaping over an intervening entity (that is how false facts are minted).
    for k in range(len(located) - 1):
        if len(edges) >= max_pairs:
            break
        subj, subj_t = located[k][1], located[k][2]
        obj, obj_t = located[k + 1][1], located[k + 1][2]
        if subj.lower() == obj.lower():
            continue
        try:
            rel = lift_predicate(span, subj, obj)
        except Exception:
            rel = None
        if not rel:
            continue
        key = (subj.lower(), rel, obj.lower())
        if key in seen:
            continue
        seen.add(key)
        edges.append({
            "subject": subj.lower(),
            "object": obj.lower(),
            "rel_type": rel,
            "subject_type": subj_t or None,
            "object_type": obj_t or None,
            "confidence": 0.8,
            "fact_provenance": "user_stated",
        })
    return edges
