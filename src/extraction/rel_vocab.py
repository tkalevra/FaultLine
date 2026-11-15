"""Rel-type VOCABULARY gates for the rel_type mint chokepoint.

BUG 05 (prod seat 3f9c21ae, 2026-11-03) measured an ontology that mints junk vocabulary at
scale: negated/modal phrases as rel_types (should_not_be_given_broad_autonomy_when,
"cannot handle" WITH a space, not_inspects, never_watches, might_not_understand),
space-bearing sentence fragments ("enhances performance", "results in the loss of"), a
punctuation rel_type (a curly apostrophe), and -- bug 03's remainder -- citation surnames
minted as rel_types (beltagy, cattan, see, schmidhuber, khandelwal ...; rizwan
served as a staged rel_type 68x/24h). The schema already carries facts.polarity /
staged_facts.polarity (migration 114, CHECK-constrained): negation
must set polarity on the fact and must NEVER mint vocabulary.

ONE module, THREE gates, all structural, all subject-agnostic, all NO content word lists:

  1. vocab_normal_form     - vocabulary normal form ^[a-z]+(?:_[a-z]+)*$ plus a
                              sentence-length phrase guard (bug 05 fix-shape 1). A
                              candidate that fails is refused-and-deferred: the FACT
                              still stages (Class C under the unknown rel surface) but
                              no rel_types row is born.
  2. phrase_is_negated     - the candidate phrase, read as a clause by the SAME parser
                              the spine uses, carries sentential negation (the shared
                              _predicate_negated - "neg" dependency, direct or grandchild).
                              This is the upstream negation detector wired to the mint
                              decision. Primary signal is grammar (UD neg); the r3-7
                              critic rounds added a CLOSED-CLASS negator-lemma set
                              (function words only) for parse-shape escapes - content
                              word lists remain banned.
  3. citation_frame_lift   - every surface occurrence of the candidate string in the
                              source text sits on a CITATION-GENRE line (author-list
                              rhythm, "et al.", arXiv/ISSN serials, bracket reference
                              numbers, vol./pp., initials). Occurrence-scoped ON
                              PURPOSE: proportional_to lifted from prose that
                              merely CITES "(Min et al., 1979)" on another line must
                              survive; beltagy lifted from a reference entry must
                              not. Measured on the live corpus before shipping
                              (the CLAUDE.md precedent: a type gate was tried and
                              deleted for 60% false vetoes - RE-MEASURED here; the
                              tests pin the measured corpus verdicts).

Refusal contract (shared by every gate): return False from the mint - the ingest seam
logs loud (log_crit) and the candidate is NOT quarantined into ontology_evaluations.
That is deliberate: the re_embedder's freq>=3 approval lane inserts rel_types rows
DIRECTLY from the evaluations queue, so quarantining a refused candidate would re-mint it
behind this gate's back. The defer lives on the FACT (Class C staging), never on the
vocabulary queue.

Rollback levers (all default ON): REL_TYPE_VOCAB_NORMALIZER, REL_TYPE_NEGATION_VETO,
REL_TYPE_CITATION_VETO.
"""
import os
import re
from typing import Optional, Tuple

# (the env rollback levers live inside the *_enabled() predicates as LIVE env reads -
#  flip REL_TYPE_*_VETO=0 at runtime, no restart)

# -- GATE 1: vocabulary normal form --------------------------------------------------------
# ^[a-z]+(?:_[a-z]+)*$ - no spaces ("cannot handle"), no digits, no punctuation (a
# curly apostrophe, "y ."), no uppercase ("usa" is admitted as a SHAPE - the name class
# is gate 3's target; "USA" is not a rel_type surface), and no underscore runs or
# trailing underscores ("a__b", "trailing_" - this file's house shape, one definition).
# Every rel_type tokenizer and _-join consumer assumes
# this shape. The phrase guard is bug 05's "no sentence-length phrases":
# the measured junk tail carries 7+ token clauses
# (should_not_be_given_broad_autonomy_when); the measured healthy grown tail tops out at
# 4 underscore tokens / 29 chars (max_percentage_during_storage), so the caps sit far
# above every healthy row and far below the clause class.
# CRITIC FIX (2026-11-03): single/double underscores are not vocabulary either - the
# stricter house shape lives HERE (one definition, no drift):
# 'trailing_' and 'a__b' refuse, 'also_known_as' admits.
_VOCAB_FORM_RE = re.compile(r"^[a-z]+(?:_[a-z]+)*$")
# Measured bounds: healthy grown tail tops out at 4 underscore tokens / 29 chars;
# the clause-fragment junk class starts at 5-7 tokens. 6 = 2x headroom over healthy,
# below every measured fragment.
_VOCAB_MAX_TOKENS = 6
_VOCAB_MAX_CHARS = 64


def vocab_normal_form(rel_type: str) -> Tuple[bool, str]:
    """(ok, reason) - the structural normal-form gate. Fail-CLOSED on shape: a rel_type
    that is not vocabulary normal form names no walkable predicate, so there is nothing
    to be permissive about. (Emptiness is the caller's job - both mint seams already
    refuse it.)"""
    if not vocab_normalizer_enabled():
        return True, "lever_disabled (REL_TYPE_VOCAB_NORMALIZER=0) - pass-through"
    rt = (rel_type or "").strip()
    if not _VOCAB_FORM_RE.match(rt):
        return False, "not_vocabulary_normal_form (^[a-z]+(?:_[a-z]+)*$: space/digit/punct/case/underscore-run)"
    if len(rt) > _VOCAB_MAX_CHARS:
        return False, "sentence_length_phrase (>%d chars)" % _VOCAB_MAX_CHARS
    if rt.count("_") + 1 > _VOCAB_MAX_TOKENS:
        return False, "sentence_length_phrase (>%d tokens)" % _VOCAB_MAX_TOKENS
    return True, "ok"


# -- GATE 2: negation routing at mint -------------------------------------------------------
# A negated frame must land in facts.polarity, never in the vocabulary. Detection reuses
# the spine's own shared negation predicate - _predicate_negated (UD "neg" dependency,
# direct neg child or the grandchild idiom; the parser labels not/never/n't as "neg") -
# over the candidate phrase read as a clause. (Rounds 3-7 added a CLOSED-CLASS
# negator-lemma set below - function words only - for the parse shapes spaCy labels
# preconj/cc/punct instead of neg; content word lists remain banned.) Measured on the
# live junk family before
# shipping: 20/20 vetoed (should_not_*, not_*, never_*, cannot_*, might_not_* - including
# ROOT-fragment shapes like not_at); 42 measured healthy rel_types: 0 false vetoes
# (negatively_correlated_with, takes_into_account, randomly_masks ... all admitted).


def negation_veto_enabled() -> bool:
    # LIVE env read (see vocab_normalizer_enabled)
    return os.getenv("REL_TYPE_NEGATION_VETO", "true").strip().lower() in (
        "1", "true", "yes", "on")


# Closed-class NEGATOR lemmas (function words, not a content word list): the canonical
# set - the degraded-mode fallback in rel_type_has_negator reads THIS set, so
# the parse's one blind construction (preconj correlative) cannot re-admit the family.
# 'without' is deliberately absent - works_without_pay is a measured-healthy predicate.
_CLOSED_NEGATOR_LEMMAS = frozenset({
    "not", "never", "cannot", "cant", "dont", "doesnt", "didnt",
    "isnt", "arent", "wasnt", "werent", "wont", "wouldnt", "couldnt", "shouldnt",
    "nor", "neither",
})


def phrase_is_negated(rel_type: str) -> Tuple[bool, str]:
    """(negated, reason) - True iff the phrase parses as a clause whose predicate carries
    a "neg" dependency. Fail-open (False) on any parser unavailability, exactly like
    _rel_type_is_function_word_only - a dead parser must not widen the veto."""
    if not negation_veto_enabled():
        return False, "lever_disabled (REL_TYPE_NEGATION_VETO=0) - pass-through"
    try:
        from src.extraction.linguistics import (  # noqa: PLC0415 - lazy, mirrors main.py
            _parse as _ling_parse,
            linguistics_available as _ling_ok,
            _predicate_negated,
        )
        if not _ling_ok():
            return False, "parser_unavailable (fail-open)"
        rt = (rel_type or "").strip().lower()
        if not rt:
            return False, "empty"
        doc = _ling_parse(rt.replace("_", " "))
        if doc is None:
            return False, "no_parse (fail-open)"
        for tok in doc:
            if tok.pos_ in ("VERB", "AUX") or tok.dep_ == "ROOT":
                if _predicate_negated(tok):
                    return True, "neg_dependency_on_predicate (%s)" % tok.lemma_
        # ROUND-3 CRITIC FIX: the UD 'neg' label misses the correlative construction
        # ("not only X but also Y") - spaCy labels the negator preconj there, not neg,
        # so a folded-polarity phrase like not_only_stores_but_also_shares slipped every
        # layer. ROUND-4 CRITIC FIX: the head-constraint (VERB/AUX/ROOT) was itself a
        # blind subclass - when spaCy roots the SECOND conjunct and tags the first as
        # NOUN/nsubj (not_only_comments_but_shares_posts), the negator's head is a plain
        # noun and the veto missed again. The preconj dependency IS the discriminator:
        # UD preconj marks the correlative-initial conjunct, and a negator lemma there
        # means the whole construction carries folded clause polarity - a closed-class
        # FUNCTION-word set (the same 17 lemmas the degraded fallback carries; 'without'
        # deliberately absent). 'both'/'either' preconj tokens do not match the lemma
        # set, so healthy correlatives (both_stores_and_shares) still admit.
        for tok in doc:
            if tok.dep_ in ("preconj", "cc:preconj") and tok.lemma_.lower() in _CLOSED_NEGATOR_LEMMAS:
                return True, "neg_preconj_correlative (%s)" % tok.lemma_.lower()
        # ROUND-5 CRITIC FIX: two bare-fragment shapes slipped past the correlative rule
        # because spaCy never labels them preconj: 'nor' is cc EVERY time (nor_rain_nor_snow
        # = not rain and not snow), and bare 'not' heads a fragment as ROOT/PART (not_only).
        # Both carry folded negative polarity per English grammar; the veto fires on the
        # token's OWN dep/POS + closed-class lemma - no head constraint to go stale (the
        # round-4 lesson). Measured blast radius: zero corpus rows (223 mints + 122 @@-rows).
        # ROUND-8 DISCLOSED BOUNDARY (do NOT 'fix' without re-measuring): the spaCy
        # tokenizer exception table splits the homograph NOUNS cant (railway
        # superelevation) / wont (archaic habit) into aux + n't (lemma not, PART), so a
        # first-token cant_/wont_ vocabulary shape (cant_slope_angle) manufactures a neg
        # label and vetoes - a false-veto class with NO folded polarity. Measured: exact
        # first token only (aunt_/pennant_/quant_ safe), 0/67 seeds, 0/60 controls, 0
        # healthy anchors; fail-closed direction (fact still stages Class C). Separating
        # them needs a lexical carve-out for two nouns - a content word list, banned -
        # or a POS-disambiguation pass: re-measure first (CLAUDE.md type-gate precedent).
        for tok in doc:
            lem = tok.lemma_.lower()
            if lem not in _CLOSED_NEGATOR_LEMMAS:
                continue
            # ROUND-7 CRITIC FIX: spaCy splits contractions capriciously - in cannot/
            # wouldnt the PART negator lands as dep=punct (in cant/couldnt as neg), so
            # dep-based branches alone are a blind subclass within ONE contraction
            # family. A PART (grammatical particle) carrying a closed-negator lemma is
            # never content: veto regardless of dep_. cc/ROOT stay for bare fragments.
            if tok.dep_ == "cc" or tok.dep_ == "ROOT" or tok.pos_ == "PART":
                return True, "neg_function_word_shape (%s %s/%s)" % (lem, tok.dep_, tok.pos_)
        # ROUND-6 CRITIC FIX (the scope-adverb escape): spaCy attaches the negator to a
        # SCOPE ADVERB which itself attaches to the predicate (not_yet_done: not=neg-child
        # of yet/advmod, yet=head done/ROOT) - loop 1's predicate check and the continuative
        # grandchild hop both miss, the cc/ROOT branch misses ('yet' is advmod), and the
        # phrase minted at every seam. UD guarantees dep_=='neg' ONLY on true negators, so
        # the blanket rule has no false-positive class: a neg label ANYWHERE in the folded
        # name is the family. Measured before shipping: no_show ('no' is det, never neg),
        # works_without_pay ('without' is a preposition) and the 0/60 control corpus all
        # carry no neg-labeled token; the escape class + the whole measured family carry one.
        for tok in doc:
            if tok.dep_ == "neg":
                return True, "neg_token_in_phrase (%s -> %s)" % (tok.lemma_.lower(), tok.head.lemma_)
        return False, "ok"
    except Exception:  # noqa: BLE001 - fail-safe: never let a parse failure widen the veto
        return False, "parse_error (fail-open)"


# -- GATE 3: citation-frame veto (bug 03 remainder) ------------------------------------------
# Author surnames became rel_types because reference-entry lines flowed through the ingest
# lane as if they were world content (measured on the tenant's own stored mint evidence:
# every surname mint sits on a bibliography line). The L4 cite-dense CHUNK skip
# (src/ingest/document_structure.py) did NOT starve the class - 225 rel_types minted in
# the 24h before this fix, ~25% of them surname-shaped - so a mint-time, occurrence-scoped
# veto is justified and MEASURED.
#
# The discriminator is STRUCTURAL (bug 03's own prescription: "citation frame: et al.,
# author-list position, reference-section provenance - over a lexical one"). Each marker
# is a FORMATTING convention of the citation genre, not a content word list; weights come
# from the measured corpus (tests pin the measured verdicts):
#   - "et al." alone crosses the threshold: bug 03's named lift shape
#     ("Khandelwal et al. (2020a) build on the dense retriever") is a citation sentence.
#   - "arXiv:2110.07752" / "ISSN 0101-2061" serial identifiers exist only in citation
#     provenance.
#   - "[154]" bracket numbers, bare years, year-comma entry tails, "vol./pp." + digit,
#     venue prepositions.
#   - Author-list positional rhythm: "Chen W, Su Y, Zuo J" (capital-comma run),
#     "Hamdi A, Hammoud H A A K" (spaced-initial cluster), "T. Zhang, S. G. Patil"
#     (surname-comma-initial).
# ALL occurrences must sit on citation-genre lines - a candidate lifted from prose that
# merely CONTAINS a citation elsewhere survives (proportional_to vs a "(Min et al.,
# 1979)" parenthetical on a DIFFERENT line: measured control, must NOT veto).
_CITE_MARKERS = (
    (re.compile(r"\bet\s+al\s*[.,]", re.IGNORECASE), 2.0),
    (re.compile(r"\barXiv\s*:\s*\d{4}\.\d{4,6}", re.IGNORECASE), 2.0),
    (re.compile(r"\bISSN\b", re.IGNORECASE), 1.5),
    (re.compile(r"\b\d{4}\s*-\s*\d{3,4}\s*[Xx\d]\b"), 1.0),
    (re.compile(r"\[\d{1,4}(?:\s*[,\.\-\u2013]\s*\d{1,4})*\]"), 1.5),
    (re.compile(r"\b(?:19|20)\d{2}\b"), 1.0),
    (re.compile(r"\((?:19|20)\d{2}[a-z]?\)"), 1.0),
    (re.compile(r"(?:^|\s)\d{1,3}\.\s"), 0.5),
    (re.compile(r"\b(?:19|20)\d{2}\s*[,.]\s*(?:\)|$|[a-z])"), 0.5),
    (re.compile(r"\b(?:vol|pp|no)\.\s*\d", re.IGNORECASE), 1.0),
    (re.compile(r"\b(?:proc\.?|proceedings|conference on|journal of|advances in|trans\.|assoc\.|in)\b",
                re.IGNORECASE), 0.5),
    (re.compile(r"\b[A-Z]\s*\.\s*[A-Z]?\s*\."), 1.0),
    (re.compile(r"(?:^|[\s,;(\[])[A-Z][A-Za-z'\u2019\-]+,\s*[A-Z]\s*\."), 1.0),
    (re.compile(r"(?:\b[A-Z][A-Za-z'\u2019]*\s+[A-Z],\s*){2,}"), 1.5),
    (re.compile(r"(?:\b[A-Z]{2,},\s*){3,}"), 1.5),
    (re.compile(r"\b(?:[A-Z]\s+){2,}[A-Z]\b"), 1.0),
)
_CITE_LINE_THRESHOLD = 2.0
# GALE CRITIC FIX (2026-11-03, round 1 blocker): weight alone is NOT the veto - weak
# markers STACK on ordinary prose ("J. K. Rowling wrote in 1997 and still lives in
# Edinburgh" scored 2.5 = initials 1.0 + bare year 1.0 + venue-prep "in" 0.5, and
# "1. In 2019 the team moved to Berlin" scored exactly 2.0). The veto additionally
# requires at least one STRONG genre marker (weight >= 1.5) on the same line: et al.,
# arXiv IDs, ISSN, bracket references, the author-list positional rhythm, the ALLCAPS
# run. Every measured veto case carries one (askell: et al.; chan/hung: rhythm; issn:
# ISSN; khandelwal: et al.); every measured false-positive class lacks one.
_CITE_STRONG_WEIGHT = 1.5


def citation_veto_enabled() -> bool:
    # LIVE env read (see vocab_normalizer_enabled)
    return os.getenv("REL_TYPE_CITATION_VETO", "true").strip().lower() in (
        "1", "true", "yes", "on")


def _cite_line_score(line: str):
    """(total, max_weight) of every marker firing on this line."""
    fired = [(rx, weight) for rx, weight in _CITE_MARKERS if rx.search(line)]
    total = sum(weight for _rx, weight in fired)
    max_weight = max((weight for _rx, weight in fired), default=0.0)
    return total, max_weight


def citation_frame_lift(rel_type: str, source_text: Optional[str]) -> Tuple[bool, str]:
    """(lifted, reason) - True iff the candidate string OCCURS in the source text and
    EVERY occurrence sits on a citation-genre line: threshold reached AND at least one
    strong genre marker on the containing line. Fail-open everywhere: no text, no
    occurrence, or a scoring error all return False (never veto what cannot be judged)."""
    if not citation_veto_enabled():
        return False, "lever_disabled (REL_TYPE_CITATION_VETO=0) - pass-through"
    text = source_text or ""
    probe = (rel_type or "").strip().lower().replace("_", " ")
    if not text or not probe:
        return False, "no_source_text (fail-open)"
    try:
        # Whitespace-flexible, word-bounded, case-insensitive occurrence search: the source
        # text carries PDF line breaks mid-phrase.
        pat = r"\b" + r"\s+".join(re.escape(w) for w in probe.split()) + r"\b"
        hits = [m.start() for m in re.finditer(pat, text, re.IGNORECASE)]
        if not hits:
            return False, "no_occurrence (fail-open)"
        # Containing-line attribution.
        bounds = []
        start = 0
        for part in text.split("\n"):
            bounds.append((start, start + len(part)))
            start += len(part) + 1
        verdicts = []
        for i in hits:
            line = next(text[a:b] for a, b in bounds if a <= i < b)
            total, max_weight = _cite_line_score(line)
            verdicts.append(total >= _CITE_LINE_THRESHOLD
                            and max_weight >= _CITE_STRONG_WEIGHT)
        if all(verdicts):
            return True, ("every occurrence on a citation-genre line (strong marker "
                          "present; verdicts=%s)" % verdicts[:4])
        shaped = sum(1 for v in verdicts if v)
        return False, "%d/%d occurrences citation-shaped" % (shaped, len(verdicts))
    except Exception:  # noqa: BLE001 - fail-safe: a scoring error must never veto
        return False, "scoring_error (fail-open)"


# -- EXTRACTION-SIDE GROWTH GUARDS
# (consolidation home, 2026-11-03 orchestrator decision)
# Backward-compat export (the deleted extraction module's public name): ONE canonical
# constant, exported under the historical symbol so external readers keep resolving.
MAX_REL_TYPE_TOKENS = _VOCAB_MAX_TOKENS
# The old src/extraction/rel_vocab.py module is CONSOLIDATED here: one module now owns the
# whole mint-path vocabulary surface (normal form, token/char caps, the UD-neg parse veto
# above, the degraded-mode fallback, and the name-identity lift veto). main.py's growth
# seams (rewrite triple filter, quarantine, edge filter, the _create_rel_type_in_flow
# mint itself, the heuristic insert) consume growth_veto_reason from THIS module; there is
# no second definition to drift. The name-identity lift veto is the intruder lane's
# structural idea, ADOPTED and measured (see tests/test_rel_type_mint_vocab_gates.py
# TestAdoptedNameIdentityLift).
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")

# Closed-class tokens dropped BEFORE the name-identity subset check, so a rel like
# works_at never collides with an endpoint phrase that merely contains "at"
# ("at microsoft"). Closed-class grammar (prepositions/articles/conjunctions/auxiliaries),
# not world knowledge - the same open/closed line the chokepoint's function-word gate
# draws with UD POS. "et"/"al" are here so the citation frame itself never reads as content.
_FUNCTION_TOKENS = frozenset({
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "with", "by", "from",
    "and", "or", "but", "is", "are", "was", "were", "be", "been", "being",
    "as", "into", "about", "over", "under", "et", "al",
})


def _tokens(text) -> list:
    """Lowercase alphanumeric tokens of text (empty on None/non-str). Pure."""
    if not isinstance(text, str):
        return []
    return [t for t in _TOKEN_SPLIT.split(text.strip().lower()) if t]


def vocab_normalizer_enabled() -> bool:
    """LIVE-READ rollback lever (REL_TYPE_VOCAB_NORMALIZER): enabled iff the env value
    is in ("1", "true", "yes", "on"); default ON. Read at every call, never cached."""
    return os.environ.get("REL_TYPE_VOCAB_NORMALIZER", "1").strip().lower() in (
        "1", "true", "yes", "on")


def rel_type_vocabulary_ok(rel_type) -> bool:
    """True iff rel_type is mintable vocabulary: snake_case normal form, at most
    _VOCAB_MAX_TOKENS tokens, at most _VOCAB_MAX_CHARS chars.

    ROUND-12 CRITIC FIX: reads the REL_TYPE_VOCAB_NORMALIZER rollback lever HERE, so
    flipping it mid-process relaxes the normal-form/caps veto at EVERY seam family that
    consumes this predicate (chokepoint structural, rewrite triple filter, edge filter,
    quarantine, heuristic insert AND the two WGM seams) - previously only the WGM seams
    honored it and the documented live-read rollback was narrower than written.
    Fail-closed by default (lever absent => ON). Never raises."""
    if not vocab_normalizer_enabled():
        return True
    rt = rel_type if isinstance(rel_type, str) else ""
    rt = rt.strip().lower()
    if not rt or not _VOCAB_FORM_RE.match(rt):
        return False
    if len(rt) > _VOCAB_MAX_CHARS:
        return False
    return len(_tokens(rt)) <= _VOCAB_MAX_TOKENS


def rel_type_has_negator(rel_type) -> bool:
    """True iff the phrase reads as a clause whose PREDICATE carries negation - the
    clause polarity was folded into the predicate name. PRIMARY detector: phrase_is_negated
    above (UD 'neg' labels + the closed-class shapes the r3-7 critic rounds added).
    DEGRADED-MODE fallback ONLY (parser unavailable/crashed): the closed-class token set
    - vetoing the known family beats admitting it when the parse is down; refused
    candidates defer to pending classification, not data loss. The parse is what keeps
    genuine predicates alive: no_show (bare determiner 'no' on a noun),
    works_without_pay ('without' attaches as a preposition, not a neg). Pure; never raises."""
    rt = rel_type if isinstance(rel_type, str) else ""
    try:
        negated, why = phrase_is_negated(rt)
        if any(k in str(why) for k in ("parser_unavailable", "no_parse", "parse_error")):
            return any(t in _CLOSED_NEGATOR_LEMMAS for t in _tokens(rt))
        return bool(negated)
    except Exception:  # noqa: BLE001 - never raise from a predicate
        return any(t in _CLOSED_NEGATOR_LEMMAS for t in _tokens(rt))


def rel_type_is_name_lift(rel_type, head_surface=None, tail_surface=None) -> bool:
    """True iff rel_type is a NAME LIFT: its content tokens are a non-empty subset of its
    OWN edge head/tail surface tokens - an entity mention promoted into the predicate slot
    (beltagy minted from a sentence about Beltagy; usa from the tail surface
    "austin, tx, usa"). THE HARD LINE: a name is never a place. Closed-class tokens are
    dropped from BOTH sides first, so a genuine predicate (works_at, instance_of) never
    collides with endpoint phrases. With no surfaces the gate cannot judge and admits
    (the structural guards above still hold). Pure; never raises."""
    rt_tokens = [t for t in _tokens(rel_type) if t not in _FUNCTION_TOKENS]
    if not rt_tokens:
        return False
    surface_tokens = set()
    for surface in (head_surface, tail_surface):
        for t in _tokens(surface):
            if t not in _FUNCTION_TOKENS:
                surface_tokens.add(t)
    if not surface_tokens:
        return False
    return set(rt_tokens) <= surface_tokens


def growth_veto_reason(rel_type, head_surface=None, tail_surface=None):
    """The FIRST structural reason the candidate must not become vocabulary, or None.
    Order: normal form -> negation -> name lift (cheap checks first). Pure; never raises.
    Callers log the reason verbatim (fail loud) and route per their own contract:
    vocabulary/negation -> the EDGE is growth-ineligible; name lift -> refuse the mint
    and the quarantine, keep the fact (user truth) under the unknown rel."""
    rt = rel_type if isinstance(rel_type, str) else ""
    rt = rt.strip().lower()
    if not rt:
        return None
    if not rel_type_vocabulary_ok(rt):
        return "vocabulary_normal_form"
    if rel_type_has_negator(rt):
        return "negation_token"
    if rel_type_is_name_lift(rt, head_surface, tail_surface):
        return "name_identity_lift"
    return None
