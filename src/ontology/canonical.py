"""Deterministic canonical ladder — rungs 1-3 + 7 of the resolution ladder.

This is the PURE, deterministic, DB-backed front of the rel_type resolution ladder from
``DEV/DESIGN-hierarchy-ladder-and-growth.md`` §"The deterministic resolution ladder":

    1. normalize_rel()      — RUNG 1: surface morphology only (no semantics, no DB, no LLM)
    2. resolve_canonical()  — RUNG 2 exact rel_types PK lookup, then
                              RUNG 3 rel_type_aliases.alias lookup (carries RUNG 7 inversion)
    3. record_alias()       — write a NEW rel_type_aliases row (the deterministic
                              synonym-collapse that REPLACES the re_embedder cosine-map)

HARD CONSTRAINTS (see CLAUDE.md + the design doc):
- Deterministic + DB-backed. NEVER cosine/embeddings — exact/normalized DB membership ONLY.
  Cosine is RETIRED by design (the >0.85 rewrite is replaced by curated alias rows).
- Metadata-driven: the canonical set and synonyms are READ from postgres tables, never hardcoded.
  The ONLY hardcoded content is the irregular-verb / light-verb morphology table in RUNG 1 — that
  is language MECHANISM (English morphology), the same category as snake_case, explicitly allowed
  under the design's "NOT hard-bound" rule. It is NOT ontology content.
- Per-tenant: every DB read binds ``SET search_path TO <schema>`` when a schema is given, matching
  the per-tenant isolation rule (search_path WITHOUT public for tenant tables; rel_type_aliases is
  the global seed table, read with whatever the search_path resolves — same as the existing
  ``_get_canonical_rel_type_with_directionality`` in main.py).
- PURE module: NO FastAPI, NO GLiNER2, NO ``import main``. Stdlib + psycopg2 only.

The DB read uses the same cached-loader style as ``src/extraction/compound.py`` /
``trigger_span.py`` — a process-level cache keyed by schema, cleared by ``reset_caches()``.
"""

from __future__ import annotations

import os
import re
import threading
from typing import Optional

# ──────────────────────────────────────────────────────────────────────────────
# RUNG 1 — deterministic morphology tables (language MECHANISM, not ontology)
# ──────────────────────────────────────────────────────────────────────────────

# Leading tokens stripped from a surface rel before lemmatization. Determiners/articles,
# possessive + personal pronouns. These never carry rel meaning.
_LEADING_DROP: frozenset[str] = frozenset({
    "the", "a", "an",
    "i", "you", "he", "she", "it", "we", "they",
    "me", "him", "her", "us", "them",
    "my", "your", "his", "its", "our", "their",
    "this", "that", "these", "those",
})

# Auxiliary / copular verbs. Dropped ONLY when they lead and a following content token exists
# (so a bare "is"/"has" still lemmatizes to a token rather than vanishing). "to" is included as
# the infinitival marker.
_AUXILIARIES: frozenset[str] = frozenset({
    "is", "are", "was", "were", "be", "been", "being", "am",
    "do", "does", "did",
    "have", "has", "had",
    "will", "would", "shall", "should", "can", "could", "may", "might", "must",
    "to",
})

# Irregular verb lemmas (surface form → lemma). Suffix-stripping handles the regular cases.
_IRREGULAR_LEMMA: dict[str, str] = {
    "had": "have", "has": "have", "having": "have",
    "was": "be", "were": "be", "been": "be", "being": "be", "am": "be",
    "is": "be", "are": "be",
    "did": "do", "does": "do", "doing": "do",
    "made": "make", "making": "make",
    "went": "go", "gone": "go", "going": "go",
    "got": "get", "gotten": "get", "getting": "get",
    "knew": "know", "known": "know", "knowing": "know",
    "met": "meet", "meeting": "meet",
    "owned": "own", "owning": "own",
    "lived": "live", "living": "live",
    "born": "bear",
    "ran": "run", "running": "run",
    "bought": "buy", "buying": "buy",
    "built": "build", "building": "build",
    "kept": "keep", "keeping": "keep",
    "left": "leave", "leaving": "leave",
    "told": "tell", "telling": "tell",
    "worked": "work", "working": "work",
    "studied": "study", "studying": "study",
    "preferred": "prefer", "preferring": "prefer",
    "liked": "like", "liking": "like",
    "disliked": "dislike", "disliking": "dislike",
    # "named"→"nam" would over-truncate (final "m" is not in the silent-e orthographic class —
    # restoring it generically would break "team"/"program"), so the high-frequency naming verb
    # is pinned in the irregular table, the same pattern as owned/liked above. (RC2)
    "named": "name", "naming": "name",
}

# Light verbs: when the head verb is "light", the load-bearing meaning is the OBJECT noun, so we
# fold the noun into the token ("had an issue with" → has_issue). The verb keeps its present 3sg
# surface ("has", "gets", "makes") so the token reads naturally and aligns with existing rels.
_LIGHT_VERBS: dict[str, str] = {
    "have": "has",
    "get": "gets",
    "make": "makes",
    "take": "takes",
    "do": "does",
    "give": "gives",
}

# Load-bearing prepositions kept on the tail (lives_in ≠ lives_at). Everything else among the
# trailing connectives is dropped before snake_casing.
_KEEP_PREPOSITIONS: frozenset[str] = frozenset({
    "in", "at", "on", "of", "for", "from", "by", "as", "into", "to", "with", "about",
})

# Filler determiners/articles inside a span (e.g. "issue WITH THE server" → drop "the").
_INNER_DETERMINERS: frozenset[str] = frozenset({"the", "a", "an"})

# [es branch] UNICODE-AWARE. This was `[^a-z0-9]+`, an ASCII class, so every accented character
# was treated as a SEPARATOR and split the word around it: "está"→"est"+"a", "diseñó"→"dise"+"ó",
# "años"→"a"+"os". Every Spanish surface carrying a diacritic was therefore shredded into
# fragments before it was ever snake_cased, minting junk rel_types that no alias layer could
# bridge back together. `\w` under Python 3 `re` is Unicode by default, so this keeps letters,
# digits and underscore in ANY script while still collapsing punctuation/whitespace runs.
#
# ⚠️ MIGRATION NOTE: this changes the canonical slug for accented surfaces (`est` → `está`).
# Rel_types minted by the OLD shredding behavior will not match newly-minted ones. On an install
# that already ingested Spanish, re-ingest to consolidate — the old slugs were corrupt anyway.
# `\W` alone would KEEP underscore, losing the snake_case split the callers rely on — so the
# class is `[\W_]`: every non-word char PLUS underscore, exactly the old separator set minus the
# accented letters it should never have been eating.
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)
_WS = re.compile(r"\s+")

# Contraction artifacts — clitic fragments left behind when an apostrophe is stripped during
# tokenization ("I've" → "i" + "ve", "they're" → "they" + "re"). These are NOT verbs; they are
# the tail halves of contractions. Map the meaning-bearing ones to their lemma and mark the rest
# for drop. This is bounded English morphology (a closed clitic set), not an ontology/world list.
_CONTRACTION_CLITICS: dict[str, str] = {
    "ve": "have",   # I've / we've / they've  → have
    "ll": "will",   # I'll / we'll            → will (auxiliary, dropped downstream)
    "d": "had",     # I'd                     → had/would (auxiliary, dropped downstream)
    "re": "be",     # we're / they're         → be (copula, dropped downstream)
    "m": "be",      # I'm                     → be (copula)
}
# Clitics that carry NO predicate meaning at all (pure copula/aux residue) — never mint from them.
_CONTRACTION_DROP: frozenset[str] = frozenset({"s", "t", "re", "m"})

# Silent magic-e restoration on a verb stem left by -ed/-ing strip. English drops a base verb's
# silent final "e" before "-ed"/"-ing" ("schedule"→"scheduled", "name"→"named", "vaccinate"→
# "vaccinated"). Stripping the suffix therefore OVER-truncates: "scheduled"→"schedul" (RC2 bug,
# same class as the historical "named"→"nam"). _restore_silent_e puts the "e" back, mirroring
# spaCy's en_lemma_rules ["ed","e"] vs ["ed",""] disambiguation, using a BOUNDED orthographic
# signal (NOT a word list): a stem whose final letter is one English does not legally end a verb
# base on without a following silent "e". Those final-letter classes:
#   * single "l" (not "ll"): schedule→schedul, rule→rul, file→fil, scale→scal, settle→settl.
#     "ll" is excluded (pull/fill/tell are real bare bases).
#   * "v"/"z"/"u": love→lov, serve→serv, amaze→amaz, argue→argu (English words don't end in bare
#     v/u; bare-z is vanishingly rare for verb bases).
#   * "c"/"g"/"s" preceded by a vowel: dance→danc, change→chang, use→us (a soft-c/soft-g/-se verb
#     needs the silent e). Restricted to a preceding VOWEL so we never touch a real consonant
#     cluster ("attac"... n/a; protects clusters like "-rg"/"-ng" only when the prior char is a
#     consonant — those keep the honest stem and the alias layer bridges if needed).
#   * "-at" (the productive Latinate "-ate"): vaccinat→vaccinate, automat→automate.
# Regular consonant-final bases are UNTOUCHED — "own"(n), "visit"(t), "walk"(k), "work"(k),
# "play"(y) do not match any class, so "owned"→"own", "visited"→"visit" stay correct.
# A genuine miss still degrades to the honest stem (the existing alias-bridge contract), so this
# is strengthening, never a new failure mode. Bounded English morphology, NOT a verb/world list.
_VOWELS = frozenset("aeiou")


def _restore_silent_e(stem: str) -> str:
    """Restore a dropped silent magic-e on a verb stem left by -ed/-ing strip
    ("schedul"→"schedule", "nam"→"name", "vaccinat"→"vaccinate"). Deterministic, bounded
    orthographic morphology (see the table above) — NO word list, NO ML. Conservative: only stems
    long enough to be a plausible verb, only the closed set of final-letter shapes that signal a
    dropped silent e; everything else passes through as the honest stem."""
    if len(stem) < 3:
        return stem
    last = stem[-1]
    prev = stem[-2]
    # single "l" (not doubled) — schedule/rule/file/scale/settle
    if last == "l" and prev != "l":
        return stem + "e"
    # v / z / u — love/serve/amaze/argue (bare v/u never end English words)
    if last in ("v", "z", "u"):
        return stem + "e"
    # soft-c / soft-g / -se after a VOWEL — dance/change/use
    if last in ("c", "g", "s") and prev in _VOWELS:
        return stem + "e"
    # productive Latinate "-ate" (vaccinate/automate); guard "eat" (eat→eated is N/A but cheap)
    if stem.endswith("at") and not stem.endswith("eat"):
        return stem + "e"
    return stem


def _suffix_lemma(word: str) -> str:
    """Regular English suffix strip (-ed/-ing/-s). Conservative — keeps short stems intact.

    Restores the silent magic-e dropped before -ed/-ing (``_restore_silent_e``) so "scheduled"→
    "schedule", "vaccinated"→"vaccinate" instead of the over-truncated "schedul"/"vaccinat" (RC2).
    The restoration is a bounded orthographic rule, never a word list; a genuine miss degrades to
    the honest stem (the DB alias layer bridges it). Consonant-final regular bases (own/visit/work)
    are untouched. (Irregulars are resolved by the table in ``_lemmatize`` before this runs.)
    """
    w = word
    if len(w) > 4 and w.endswith("ing"):
        stem = w[:-3]
        # doubled consonant (running→run) handled by irregular table; default keep stem.
        return _restore_silent_e(stem)
    if len(w) > 4 and w.endswith("ied"):
        # married→marry, studied→study
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("ed"):
        return _restore_silent_e(w[:-2])
    if len(w) > 3 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and (w.endswith("ches") or w.endswith("shes") or w.endswith("xes") or w.endswith("zes") or w.endswith("oes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _lemmatize(word: str) -> str:
    """Lemmatize a single token: irregular table first, then regular suffix strip."""
    w = word.lower()
    if w in _IRREGULAR_LEMMA:
        return _IRREGULAR_LEMMA[w]
    return _suffix_lemma(w)


# ── NOMINAL (noun-phrase) morphology — INFLECTIONAL ONLY ──────────────────────
# A stem ending that takes "-es" rather than a bare "-s" plural: the English sibilant
# classes (box→boxes, church→churches, dish→dishes, buzz→buzzes, glass→glasses). Any other
# stem takes a bare "-s" (shoe→shoes, colour→colours), so only the sibilant set may strip
# two characters. Bounded orthography, NOT a word list.
_SIBILANT_STEM_ENDINGS: tuple = ("s", "x", "z", "ch", "sh")


def _nominal_lemma(word: str) -> str:
    """Lemmatize a token occupying a NOUN slot: strip the PLURAL inflection ONLY.

    THE DISTINCTION, by its established name: **inflectional vs derivational morphology**
    (Bauer 1983, *English Word-Formation*, CUP; Haspelmath & Sims 2010, *Understanding
    Morphology*, 2nd ed., Hodder, ch. 5). Lemmatization is defined as the removal of
    INFLECTION — the plural "-s" on a noun — and must NEVER undo DERIVATION, which creates a
    distinct lexeme. English "-ing" on a nominal is derivational: it forms a deverbal noun
    ("a firing", "a meeting", "a building"), the classic *derived nominal* of Chomsky (1970),
    "Remarks on Nominalization", in Jacobs & Rosenbaum (eds.) *Readings in English
    Transformational Grammar*, 184-221. Stripping it does not yield a lemma — it destroys the
    lexeme ("kiln firing" → "kiln fir"; "favorite running shoes" → "favorite run sho").
    Penn Treebank tags the head of such a compound NN, not VBG (Marcus et al. 1993,
    *Computational Linguistics* 19(2)), which is exactly the signal the caller has already
    established before routing here: the possessed head is a NOUN.

    So this is the NOUN counterpart of ``_lemmatize``: no irregular-VERB table (applying it to
    a noun mis-lemmatizes real nouns — "my saw" → "see"), no "-ed"/"-ing" strip, no silent-e
    restoration (a verb-stem repair that has no nominal analogue). Plural only.

    Deterministic, offline, no word list, no ML. Conservative: short tokens and unrecognized
    endings pass through verbatim (an honest surface always beats an invented stem)."""
    w = (word or "").lower()
    if len(w) > 3 and w.endswith("ies"):
        return w[:-3] + "y"                      # stories → story
    if len(w) > 3 and w.endswith("es"):
        stem = w[:-2]
        if stem.endswith(_SIBILANT_STEM_ENDINGS):
            return stem                          # boxes → box, glasses → glass
        return w[:-1]                            # shoes → shoe (bare -s on an "-e" base)
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss") and not w.endswith("us"):
        return w[:-1]                            # colours → colour, hours → hour
    return w                                     # firing → firing, kiln → kiln


def _guard_mint(token: str) -> str:
    """Refuse to mint a rel from a clitic-residue token (the "ve"/"ll"/"re" tail left by a
    stripped contraction apostrophe).

    A miss beats a junk rel (the design line): a SINGLE-segment bare token that is exactly a
    contraction-clitic fragment is not a real predicate — return "" so the caller drops it rather
    than growing a junk rel like ``ve``. A MULTI-segment token (carries an underscore, e.g.
    "lives_in", "has_issue") is a built phrase and is kept verbatim. Legitimate short lemmas
    ("be", "go", "do", "own") are NOT clitics and pass through untouched. Bounded + deterministic;
    no ontology/world knowledge, no generic length cull.
    """
    if not token:
        return ""
    if "_" in token:
        return token
    if token in _CONTRACTION_CLITICS or token in _CONTRACTION_DROP:
        return ""
    return token


def normalize_rel(surface: str) -> str:
    """RUNG 1 — deterministic surface→token morphology. NO semantics, NO DB, NO LLM.

    Steps (all mechanical English morphology):
      - lowercase, split into word tokens (non-word chars become separators);
      - strip leading determiners/pronouns and leading auxiliaries (while content remains);
      - lemmatize the head verb (irregular table + suffix strip);
      - if the head verb is LIGHT (have/get/make/...), fold the object noun into the token and
        present the verb in 3sg ("had an issue with" → "has_issue");
      - drop inner determiners; KEEP load-bearing prepositions (lives_in ≠ lives_at);
      - snake_case the survivors.

    Returns the snake_cased token (possibly empty string for empty/garbage input).
    """
    return _normalize_surface(surface, _lemmatize)


def normalize_nominal_rel(surface: str) -> str:
    """RUNG 1-N — the NOMINAL twin of :func:`normalize_rel`, for a possessed NOUN PHRASE.

    Identical pipeline (same tokenization, determiner/auxiliary stripping, preposition keeping
    and snake_casing) with ONE substitution: tokens are lemmatized by :func:`_nominal_lemma`
    (INFLECTIONAL plural strip only) instead of :func:`_lemmatize` (verb morphology).

    WHY A SEPARATE ENTRY POINT rather than a smarter shared lemmatizer: the correct morphology
    depends on the token's SYNTACTIC CATEGORY, which the caller already knows and this pure
    module cannot see. ``normalize_rel`` is applied to a PREDICATE span, where "-ing"/"-ed" are
    inflection to strip; ``normalize_nominal_rel`` is applied to a possessed-noun phrase whose
    head the caller's dependency parse has already tagged NOUN, where "-ing" is derivational and
    stripping it destroys the lexeme (see ``_nominal_lemma``). Same string in, different correct
    answer, decided by the grammar at the call site — so the choice belongs to the caller.

        normalize_rel("kiln firing")          -> "kiln_fir"      (verb reading)
        normalize_nominal_rel("kiln firing")  -> "kiln_firing"   (nominal reading)
        normalize_nominal_rel("favorite running shoes") -> "favorite_running_shoe"

    Pure/deterministic — NO DB, NO LLM, NO embeddings, no word list."""
    return _normalize_surface(surface, _nominal_lemma)


# ----------------------------------------------------------------------------
# SPELLING-VARIANT FOLD (identity-honesty, bug 3) - single-edit morphological closeness.
#
# THE DEFECT (measured on the owner's demo): 'My favourite colour is Blue' grew the rel
# favourite_colour; 'what is my favorite colour' minted/grounded a JUNK twin because the
# surface spellings differ and nothing folds them (normalize_rel is spelling-preserving).
#
# WHY NOT A DIALECT RULE TABLE: the tempting closed rule set (-our<->-or, -ise<->-ize,
# -yse<->-yze) was BUILT and REFUTED in-session: the substring correspondence -our- -> -or-
# ('favourite'->'favorite') also folds DIFFERENT LEXEMES ('works_for'<->'works_four';
# 'for'/'four' pass each other's forward check), so any rule strong enough to catch
# Oxford variants is strong enough to corrupt real rels. A restricted table that is safe
# would have to enumerate exactly the variant words - the forbidden en-GB/en-US
# dictionary. So the mechanism shipped here is NOT about dialects at all:
#
#   a NOVEL surface folds onto a LIVE rel when the two are SINGLE-EDIT MORPHOLOGICAL
#   SIBLINGS - same token count, and exactly one token differs by a single-character
#   edit (insert/delete/substitute). favourite/favorite, organise/organize,
#   analyse/analyze, recieve/receive, colour/color are ALL single-edit pairs; distinct
#   lexemes (works_for / has_pet / age) are not. Subject-agnostic, dictionary-free,
#   deterministic; the fold TARGETS are the tenant's OWN live rels (DB metadata), and a
#   grown rel_type_aliases row (source='engine') records each fold so every later
#   occurrence resolves through the deterministic alias rail.
#
# Anti-pollution invariants, same family as resolve_seeded_by_morphology:
#   * NEVER folds onto itself; only fires when the surface does NOT resolve (caller
#     checks) and the target EXISTS in rel_types;
#   * AMBIGUOUS (two live rels are both single-edit siblings - e.g. both dialect twins
#     were already grown) -> None (a wrong fold is worse than a miss; the caller
#     mints/defers exactly as today);
#   * never cosine, never embeddings, no word lists.


def _single_edit_distance(a: str, b: str) -> bool:
    """True when two tokens differ by EXACTLY ONE character edit, Damerau-style:
    insert, delete, substitute, OR adjacent transposition (recieve/receive - the most
    common English typo class). Early-exit at distance 2. Pure string mechanism."""
    if a == b:
        return False
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diff_positions = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        if len(diff_positions) == 1:
            return True                       # one substitution
        if (len(diff_positions) == 2
                and diff_positions[1] == diff_positions[0] + 1
                and a[diff_positions[0]] == b[diff_positions[1]]
                and a[diff_positions[1]] == b[diff_positions[0]]):
            return True                       # one adjacent transposition
        return False
    if la > lb:
        a, b, la, lb = b, a, lb, la
    # a is shorter by 1: exactly one insertion in b
    i = j = 0
    skipped = False
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
        else:
            if skipped:
                return False
            skipped = True
            j += 1
    return True


def spelling_variant_key(rel: str) -> str:
    """Identity under this mechanism: the rel itself. (Kept as a named seam so callers
    and tests can ask for the fold surface without knowing the mechanism - single-edit
    closeness has no canonical image, so the key IS the surface.)"""
    return (rel or "").strip().lower()


def _spelling_fold_keeps_prepositions() -> bool:
    """Flag: refuse a single-edit fold whose ONE differing token is a load-bearing
    preposition. Default TRUE = the SAFE direction (refuse). Set
    ``SPELLING_FOLD_KEEP_PREPOSITIONS=false`` to restore the byte-for-byte legacy fold."""
    return os.environ.get(
        "SPELLING_FOLD_KEEP_PREPOSITIONS", "true").strip().lower() in ("1", "true", "yes", "on")


def spelling_variant_siblings(surface: str, rel: str) -> bool:
    """Are SURFACE and REL single-edit morphological siblings? Same token count,
    exactly one token differs, and that difference is a single character edit.

    ⛔ EXCEPT when that one differing token is a LOAD-BEARING PREPOSITION on EITHER side.
    This module already declares ``_KEEP_PREPOSITIONS`` semantically load-bearing — its
    comment is literally "lives_in ≠ lives_at" — and RUNG 1 protects those tokens from
    being dropped during normalization. This predicate then ignored that declaration and
    let a ONE-CHARACTER edit collapse the very tokens RUNG 1 preserved: ``in``↔``on``,
    ``at``↔``as``, ``of``↔``on``/``or`` are all single substitutions.

    WHY A PREPOSITION IS NOT A SPELLING VARIANT. Prepositions are a CLOSED CLASS, so a
    single-edit neighbour of a preposition is virtually always ANOTHER REAL PREPOSITION —
    a "real-word error" in the sense of Golding & Roth, *Applying Winnow to
    Context-Sensitive Spelling Correction* (arXiv:cmp-lg/9607024, 1996), whose stated task
    is "fixing spelling errors that happen to result in valid words". Their point is that
    such confusions are UNDECIDABLE from the string and need context; edit distance alone
    (Damerau, CACM 7(3):171-176, 1964) cannot separate a typo from a different word. In an
    open-class content token (``colour``/``color``, ``recieve``/``receive``) a one-edit
    neighbour is overwhelmingly a misspelling; in a closed-class function token it is a
    DIFFERENT RELATION. ``sit_in`` ≠ ``sit_on``; ``believe_on`` ≠ ``believe_in``;
    ``approve_on`` ≠ ``approve_of``.

    MEASURED (2026-08-27, read-only across all 22 production tenants, 1,058 distinct
    rel_types): 187 distinct preposition-collapsing folds were reachable in 22/22 tenants
    — e.g. ``believe_on``→``believe_in``, ``approve_on``→``approve_of``,
    ``build_in``→``build_on``, ``cave_on``→``cave_in``. The ingest seam
    (``main.py`` ``ingest.rel_spelling_variant_folded``) REWRITES ``edge.rel_type`` and
    grows a PERMANENT alias row on such a fold, so each one is a user-truth corruption
    that persists for that tenant. The 9 live-vs-live preposition pairs already present on
    production (``sit_in``/``sit_on``, ``born_in``/``born_on``, ``stay_in``/``stay_on``, …)
    were ALREADY protected by ``resolve_grown_spelling_variant``'s ambiguity/already-live
    guards; this closes the case those guards cannot see — a genuinely NOVEL surface one
    preposition-edit from a live rel.

    Both directions are refused ("on EITHER side") because the asymmetric case is no safer:
    folding a non-preposition token onto a preposition invents a particle the user never
    said. Fail-safe direction is REFUSE = leave the rel novel, which is this module's
    stated bias ("a wrong fold is worse than a miss").
    """
    s_toks = [t for t in (surface or "").lower().split("_") if t]
    r_toks = [t for t in (rel or "").lower().split("_") if t]
    if not s_toks or len(s_toks) != len(r_toks):
        return False
    if sum(1 for s, r in zip(s_toks, r_toks) if s != r) != 1:
        return False
    _keep_preps = _spelling_fold_keeps_prepositions()
    for s, r in zip(s_toks, r_toks):
        if s != r:
            if _keep_preps and (s in _KEEP_PREPOSITIONS or r in _KEEP_PREPOSITIONS):
                return False
            return _single_edit_distance(s, r)
    return False


def resolve_grown_spelling_variant(
    surface: str, dsn: Optional[str] = None, schema: Optional[str] = None
) -> Optional[str]:
    """Fold a surface rel onto a LIVE rel that is its single-edit spelling sibling.

    The metadata-driven rail this lane owns: read the tenant's OWN live rel_types (seed
    plus grown - every rel the ingest seam can mint or match) and fold the surface onto
    the unique live rel that is a single-edit sibling (favourite_colour -> the live
    favorite_colour, or vice versa). Deterministic + DB-backed, NEVER cosine. Ambiguous
    (both siblings already live) -> None. Fail-soft -> None. NEVER returns the surface."""
    norm = normalize_rel(surface)
    if not norm:
        return None
    try:
        rels = _load_reltypes(dsn, schema)
    except Exception:
        return None
    if norm in rels:
        # the surface is ITSELF a live rel - folding would shadow a real concept.
        # (the caller only folds on a MISS, but the resolver must hold the invariant
        # on its own: an exact hit is not a variant)
        return None
    hits = {r for r in rels if r != norm and spelling_variant_siblings(norm, r)}
    if len(hits) == 1:
        return next(iter(hits))
    return None

def _normalize_surface(surface: str, lemma_fn) -> str:
    """Shared RUNG-1 pipeline for :func:`normalize_rel` / :func:`normalize_nominal_rel`.

    ``lemma_fn`` is the per-token lemmatizer the caller's syntactic reading selects. Keeping ONE
    pipeline is load-bearing: the preference-copula deriver chain and the possessive-predication
    harvest seam must produce the BYTE-IDENTICAL token for the same phrase or they stop deduping
    and mint two rival concepts for one attribute."""
    if not surface:
        return ""

    s = surface.strip().lower()
    # Tokenize: collapse any run of non-word chars (incl. underscores from snake_case input)
    # into a single space, so "had_an_issue" and "had an issue" normalize identically.
    s = _NON_WORD.sub(" ", s)
    tokens = [t for t in _WS.split(s) if t]
    if not tokens:
        return ""

    # 0. Resolve contraction-clitic artifacts left by apostrophe stripping ("I've"→[i, ve],
    #    "they're"→[they, re]). A bare clitic fragment is NOT a verb — it is the tail half of a
    #    contraction. Meaning-bearing clitics map to their lemma so the auxiliary/light-verb
    #    machinery handles them ("ve"→"have"); pure copula/aux residue ("re","m","s") is dropped.
    #    Bounded English morphology (closed clitic set), never an ontology list.
    _resolved: list[str] = []
    for t in tokens:
        if t in _CONTRACTION_DROP:
            continue
        _resolved.append(_CONTRACTION_CLITICS.get(t, t))
    tokens = _resolved
    if not tokens:
        return ""

    # 1. Strip leading determiners / pronouns (while something remains after).
    i = 0
    while i < len(tokens) - 0 and tokens[i] in _LEADING_DROP and i < len(tokens) - 1:
        i += 1
    # Edge: if ALL tokens are leading-drop words, keep the last as the token.
    if all(t in _LEADING_DROP for t in tokens):
        return lemma_fn(tokens[-1])
    tokens = tokens[i:]

    # 2. Strip leading auxiliaries — but STOP at a light verb: a leading have/has/had/get/...
    #    is the head light verb (whose object carries the meaning), NOT a tense helper to drop.
    #    ("had an issue" → keep 'had' as the light head → has_issue, not strip→issue.)
    while len(tokens) > 1 and tokens[0] in _AUXILIARIES:
        if _lemmatize(tokens[0]) in _LIGHT_VERBS:
            break
        tokens = tokens[1:]
    if not tokens:
        return ""

    # 3. Lemmatize the head (verb reading: verb morphology; nominal reading: plural strip only).
    head = lemma_fn(tokens[0])
    rest = tokens[1:]

    # 4. Light-verb object fold: "have an issue with" → head=have(light), find first content noun.
    if head in _LIGHT_VERBS:
        light_surface = _LIGHT_VERBS[head]
        noun = None
        for tok in rest:
            if tok in _INNER_DETERMINERS or tok in _AUXILIARIES:
                continue
            if tok in _KEEP_PREPOSITIONS:
                continue
            noun = lemma_fn(tok)
            break
        if noun:
            return _guard_mint(f"{light_surface}_{noun}")
        # No object noun — keep the light verb's 3sg surface alone (e.g. "has").
        return light_surface

    # 5. Non-light head verb: keep head + load-bearing prepositions + lemmatized content tail.
    out_tokens = [head]
    for tok in rest:
        if tok in _INNER_DETERMINERS:
            continue
        if tok in _AUXILIARIES and tok not in _KEEP_PREPOSITIONS:
            # auxiliaries mid-span carry no rel meaning (e.g. "is born in" handled above)
            continue
        if tok in _KEEP_PREPOSITIONS:
            out_tokens.append(tok)  # keep prepositions verbatim (load-bearing)
            continue
        out_tokens.append(lemma_fn(tok))

    token = "_".join(t for t in out_tokens if t)
    # Final snake_case hygiene.
    token = _NON_WORD.sub("_", token).strip("_")
    return _guard_mint(token)


# ──────────────────────────────────────────────────────────────────────────────
# RUNG 2 / RUNG 3 / RUNG 7 — DB-backed canonical resolution (deterministic, cached)
# ──────────────────────────────────────────────────────────────────────────────

# Per-schema caches. Each is a dict keyed by schema name (or "" for default search_path):
#   _RELTYPE_CACHE[schema]  -> frozenset of canonical rel_type PKs in that tenant
#   _ALIAS_CACHE[schema]    -> dict alias -> (canonical_rel_type, requires_inversion)
#   _SEEDED_NORM_CACHE[schema] -> dict normalize_rel(seed_pk) -> seed_pk  (SEEDED rels only)
_RELTYPE_CACHE: dict[str, frozenset] = {}
_ALIAS_CACHE: dict[str, dict] = {}
_SEEDED_NORM_CACHE: dict[str, dict] = {}
_CACHE_LOCK = threading.RLock()

# Sources that mark a rel_type as a SEEDED canonical (a curated, standards-aligned or
# domain built-in rel). ONLY these are valid morphology-fold targets — a tenant-grown
# ('engine'/'user') rel is NEVER a fold target, so distinct grown rels can never be
# collapsed into one another. Matches migrations/007_rel_types_source.sql.
_SEEDED_SOURCES: frozenset[str] = frozenset({"wikidata", "builtin"})


def _dsn_default() -> str:
    return os.environ.get(
        "POSTGRES_DSN", "postgresql://faultline:faultline@localhost:5432/faultline"
    )


def reset_caches(schema: Optional[str] = None) -> None:
    """Clear the canonical/alias caches. Clears one schema or all (schema=None).

    Mirrors compound.reset_extraction_patterns_cache() — call after a rel_type or alias
    write so the next resolve sees fresh DB state.
    """
    with _CACHE_LOCK:
        if schema is None:
            _RELTYPE_CACHE.clear()
            _ALIAS_CACHE.clear()
            _SEEDED_NORM_CACHE.clear()
        else:
            key = schema or ""
            _RELTYPE_CACHE.pop(key, None)
            _ALIAS_CACHE.pop(key, None)
            _SEEDED_NORM_CACHE.pop(key, None)


def _connect(dsn: Optional[str], schema: Optional[str]):
    """Open a psycopg2 connection and bind the tenant search_path when a schema is given.

    Per-tenant rule: tenant tables (rel_types AND rel_type_aliases) are read under
    ``SET search_path TO <schema>`` with NO public. Both tables are seeded INTO the
    tenant schema at provisioning (schema_manager bootstrap copies public →
    <schema>.rel_types and <schema>.rel_type_aliases), so the tenant schema carries
    the full alias/rel seed PLUS its grown rows. public is the template/seed-source
    only — never read at runtime. Including public here would let a tenant read fall
    through to the public seed, masking missing per-tenant seeds and risking
    cross-tenant resolution.
    """
    import psycopg2

    conn = psycopg2.connect(dsn or _dsn_default())
    if schema:
        with conn.cursor() as cur:
            # Tenant schema ONLY — no public fallthrough (per-tenant isolation).
            cur.execute(f'SET search_path TO "{schema}"')  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.lang.security.audit.sqli.psycopg-sqli.psycopg-sqli — schema from UUID-derived source with validation
    return conn


def _load_reltypes(dsn: Optional[str], schema: Optional[str]) -> frozenset:
    key = schema or ""
    with _CACHE_LOCK:
        cached = _RELTYPE_CACHE.get(key)
    if cached is not None:
        return cached

    result: frozenset = frozenset()
    try:
        conn = _connect(dsn, schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT rel_type FROM rel_types")
                result = frozenset(r[0].lower() for r in cur.fetchall() if r[0])
        finally:
            conn.close()
    except Exception as e:  # fail soft — empty set means "nothing resolves at rung 2"
        print(f"[WARNING] canonical: failed to load rel_types (schema={schema}): {e}")
        result = frozenset()

    with _CACHE_LOCK:
        _RELTYPE_CACHE[key] = result
    return result


def _load_aliases(dsn: Optional[str], schema: Optional[str]) -> dict:
    key = schema or ""
    with _CACHE_LOCK:
        cached = _ALIAS_CACHE.get(key)
    if cached is not None:
        return cached

    result: dict = {}
    try:
        conn = _connect(dsn, schema)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT alias, canonical_rel_type, requires_inversion FROM rel_type_aliases"
                )
                for alias, canonical, requires_inversion in cur.fetchall():
                    if not alias or not canonical:
                        continue
                    val = (canonical.lower(), bool(requires_inversion))
                    raw = alias.lower()
                    result[raw] = val
                    # Also index the alias under its RUNG-1 normalized form so a surface like
                    # "married to" (→ marry_to) resolves a seed row stored raw as "married_to".
                    # The seed rows predate this normalizer; indexing both is deterministic and
                    # never overwrites an explicit raw row (setdefault).
                    norm = normalize_rel(alias)
                    if norm and norm != raw:
                        result.setdefault(norm, val)
        finally:
            conn.close()
    except Exception as e:  # fail soft — no aliases means "nothing resolves at rung 3"
        print(f"[WARNING] canonical: failed to load rel_type_aliases (schema={schema}): {e}")
        result = {}

    with _CACHE_LOCK:
        _ALIAS_CACHE[key] = result
    return result


def _load_seeded_norm(dsn: Optional[str], schema: Optional[str]) -> dict:
    """Cached map ``normalize_rel(seed_pk) -> seed_pk`` over the SEEDED canonical rels only
    (``rel_types.source IN ('wikidata','builtin')``). Used by the morphology-fold so a novel
    morphological variant (``live_in``) whose RUNG-1 normalized form collides with a seeded
    rel's normalized form (``lives_in`` also normalizes to ``live_in``) folds onto that seeded
    canonical — WITHOUT ever targeting a tenant-grown ('engine'/'user') rel.

    Fail-soft (empty map → nothing folds) and per-tenant (binds ``SET search_path`` via
    ``_connect``, NO public fallthrough). If two seeded rels share a normalized form the first
    seen wins (setdefault) — seeded rels are curated and not expected to collide, but this is
    deterministic regardless.
    """
    key = schema or ""
    with _CACHE_LOCK:
        cached = _SEEDED_NORM_CACHE.get(key)
    if cached is not None:
        return cached

    result: dict = {}
    try:
        conn = _connect(dsn, schema)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT rel_type FROM rel_types WHERE lower(source) IN %s",
                    (tuple(_SEEDED_SOURCES),),
                )
                for (pk,) in cur.fetchall():
                    if not pk:
                        continue
                    pk_l = pk.lower()
                    norm = normalize_rel(pk_l)
                    if norm:
                        result.setdefault(norm, pk_l)
        finally:
            conn.close()
    except Exception as e:  # fail soft — empty map means "no morphology fold target"
        print(f"[WARNING] canonical: failed to load seeded rel_types (schema={schema}): {e}")
        result = {}

    with _CACHE_LOCK:
        _SEEDED_NORM_CACHE[key] = result
    return result


def resolve_seeded_by_morphology(
    surface: str, dsn: Optional[str] = None, schema: Optional[str] = None
) -> Optional[str]:
    """Morphology-fold a NOVEL surface rel onto a SEEDED canonical rel by normalized form.

    Deterministic generalization of the existing scalar morphology-fold
    (``main.py::_pin_scalar_attribute_to_known``): compare ``normalize_rel(surface)`` against
    each SEEDED canonical rel's normalized form. Returns the seeded canonical PK iff one
    matches, else None. NEVER cosine — exact normalized-form membership only.

    Only SEEDED rels ('wikidata'/'builtin') are fold targets — a tenant-grown rel is never a
    target, so two distinct grown rels can never be collapsed. The caller is expected to have
    already confirmed ``surface`` is NOT itself a known rel (resolve_canonical miss) before
    folding. Never aliases a rel onto itself (the normalized forms differ from the PK only when
    the surface morphology genuinely differs). Fail-soft → None.
    """
    norm = normalize_rel(surface)
    if not norm:
        return None
    seeded = _load_seeded_norm(dsn, schema)
    hit = seeded.get(norm)
    # Guard: never fold a surface that already IS the seeded PK (no-op alias).
    if hit and hit != (surface or "").strip().lower():
        return hit
    return None


def resolve_canonical(
    surface: str, dsn: Optional[str] = None, schema: Optional[str] = None
) -> dict:
    """Resolve a surface rel through RUNG 1 (normalize) → RUNG 2 (exact) → RUNG 3 (alias).

    Deterministic + DB-backed. NEVER cosine. Returns:
        {
          "input":              the raw surface string,
          "normalized":         the RUNG-1 token,
          "canonical":          the canonical rel_type PK, or None if unresolved,
          "requires_inversion": bool (RUNG 7 — True iff the matched alias inverts direction),
          "matched":            "exact" | "alias" | "none",
        }

    Resolution order (first hit wins):
      RUNG 2 — normalized token is itself a rel_types PK → exact, no inversion.
      RUNG 3 — normalized token is a rel_type_aliases.alias → canonical + requires_inversion.
      else   — canonical=None, matched="none" (caller decides: mint novel, drop, escalate).
    """
    normalized = normalize_rel(surface)

    if not normalized:
        return {
            "input": surface,
            "normalized": normalized,
            "canonical": None,
            "requires_inversion": False,
            "matched": "none",
        }

    # RUNG 2 — exact canonical PK lookup.
    reltypes = _load_reltypes(dsn, schema)
    if normalized in reltypes:
        return {
            "input": surface,
            "normalized": normalized,
            "canonical": normalized,
            "requires_inversion": False,
            "matched": "exact",
        }

    # RUNG 3 — alias lookup (carries RUNG 7 inversion).
    aliases = _load_aliases(dsn, schema)
    hit = aliases.get(normalized)
    if hit is not None:
        canonical, requires_inversion = hit
        return {
            "input": surface,
            "normalized": normalized,
            "canonical": canonical,
            "requires_inversion": requires_inversion,
            "matched": "alias",
        }

    # Unresolved — exact/normalized DB membership only; NO cosine fallback by design.
    return {
        "input": surface,
        "normalized": normalized,
        "canonical": None,
        "requires_inversion": False,
        "matched": "none",
    }


# Sources allowed to mint an alias row (deterministic synonym-collapse, replaces cosine-map).
_ALLOWED_ALIAS_SOURCES: frozenset[str] = frozenset({"engine", "user_corrected"})


def record_alias(
    alias: str,
    canonical: str,
    requires_inversion: bool,
    source: str,
    dsn: Optional[str] = None,
    schema: Optional[str] = None,
) -> bool:
    """RUNG 3 write — persist a confirmed surface→canonical synonym as a NEW alias row.

    This is the deterministic synonym-collapse that REPLACES the re_embedder cosine-map: a
    synonym confirmed by rung-6 bridging or by user correction becomes a persistent DB row,
    never a runtime embedding score.

    The alias is stored NORMALIZED (RUNG 1) so resolve_canonical finds it deterministically.
    ``source`` must be 'engine' or 'user_corrected'. ON CONFLICT (alias) is a no-op (the
    existing curated row wins; do not clobber).

    Returns True if a row was inserted, False on no-op/skip. Invalidates the schema's alias
    cache on a successful insert.
    """
    if source not in _ALLOWED_ALIAS_SOURCES:
        raise ValueError(
            f"record_alias: source must be one of {sorted(_ALLOWED_ALIAS_SOURCES)}, got {source!r}"
        )

    alias_norm = normalize_rel(alias)
    canonical_norm = (canonical or "").strip().lower()
    if not alias_norm or not canonical_norm:
        return False
    # Never alias a token to itself.
    if alias_norm == canonical_norm:
        return False

    inserted = False
    try:
        conn = _connect(dsn, schema)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO rel_type_aliases
                        (alias, canonical_rel_type, requires_inversion, source)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (alias) DO NOTHING
                    """,
                    (alias_norm, canonical_norm, bool(requires_inversion), source),
                )
                inserted = cur.rowcount > 0
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[WARNING] canonical: record_alias failed (alias={alias_norm}): {e}")
        return False

    if inserted:
        reset_caches(schema)
    return inserted
