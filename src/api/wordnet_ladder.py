"""Deterministic WordNet hypernym-ladder source for SYNC L4 ingest growth.

Implements **Option (b)** of the internal design record (owner-approved):
a *deterministic, offline* lexical hypernymy source consulted synchronously at ingest so the
``subclass_of`` is-a ladder (dermatologist→doctor→…, handbag→bag→container…) is PRESENT AT QUERY
TIME, letting the existing COUNT/SUM walk aggregate over hypernyms with ZERO query-side change.

WHY THIS IS LEGITIMATE (evidence-grounded, not a hunch)
───────────────────────────────────────────────────────
WordNet is the canonical machine-readable lexical taxonomy: its **hypernym/hyponym** pointers
encode exactly the "X is-a-kind-of Y" relation between synsets, rooted at ``{entity}`` which
subsumes ``{physical_entity}``, ``{abstraction}``, ``{thing}`` (Princeton WordNet documentation,
``wngloss(7WN)`` — the "@" pointer is *hypernym*). Nouns are organized into a strict is-a
hierarchy, which is precisely the L4 classification axis FaultLine walks. Using it as a knowledge
SOURCE (like dateparser for time, GLiNER2 for typing) fits "LLM detects, deterministic structures":
the hypernym is a lexical FACT, not a model guess.

  - Princeton WordNet — hypernymy/is-a hierarchy, "@" pointer, root {entity}:
      https://wordnet.princeton.edu/documentation/wngloss7wn
  - Global WordNet / hypernym semantics: https://globalwordnet.github.io/gwadoc/
  - NLTK WordNet reader (``hypernym_paths``, ``closure``): https://www.nltk.org/api/nltk.corpus.reader.wordnet.html

DETERMINISTIC DISAMBIGUATION RULE (the #1 correctness risk — "bank" has river/finance synsets)
──────────────────────────────────────────────────────────────────────────────────────────────
ONE fixed, documented cascade. No cosine, no LLM — same input → same ladder, every run:

  1. **CONTEXT closure-containment (strongest — GAP A).** When a co-occurring CATEGORY hypernym is
     known (a Hearst umbrella "citrus fruits such as lemon, LIME, orange", or a query anchor),
     pick the member's FIRST noun synset (WordNet sense order) whose transitive hypernym-*closure*
     CONTAINS the category's synset. This lands member + category on the SAME is-a chain whenever
     WordNet supports it — deterministically resolving "lime" to ``lime.n.06`` (the *fruit*, under
     ``citrus``/``edible_fruit``) instead of ``calcium_hydroxide.n.01`` (the chemical, the MFS).
     If no member sense reaches the category, we do NOT force it — fall through (honest capture).
  2. **RANKED lexname preference.** If the entity's GLiNER2 top-type is known, among the noun
     synsets whose WordNet *lexicographer file* (``lexname``, e.g. ``noun.person``/``noun.food``)
     is one of the type's preferred buckets, pick the one whose bucket ranks EARLIEST in the
     preference order (tie-break: WordNet sense order). Ranking by *preference* (not by raw synset
     order) is what steers an Object-typed edible ("lime") to its ``noun.food`` sense above the
     ``noun.substance`` chemical sense — the documented "prefer the food/plant synset for a fruit"
     behaviour. Deterministic: fixed prefs + fixed WordNet order.
  3. **MFS / first sense.** Else the FIRST noun synset. WordNet orders senses by decreasing corpus
     frequency (SemCor), so the first sense is the **Most-Frequent-Sense (MFS)** — the standard,
     documented first-sense WSD baseline (Jurafsky & Martin SLP3 App. C; McCarthy et al.).
  4. **MODIFIER-SENSE VETO / ABSTENTION** (flag ``L4_WORDNET_MODIFIER_WSD``, default OFF). Rules
     1-3 pick a sense for a compound's HEAD *with the modifier thrown away*, so "kiln firing"
     head-reduced to ``firing`` and took its MFS ``fire.n.02`` = *the act of firing weapons at an
     enemy*, minting ``firing → fire → attack → operation`` and rendering "Firing is a subclass of
     attack". The modifier is exactly the restricting element (Levi 1978; Downing 1977), so when it
     CONTRADICTS the picked sense we **place nothing** rather than guess: see
     ``_modifier_contradicts_sense`` for the dual-signal rule (WordNet ``;c`` topic-domain mismatch
     ∧ Adapted-Lesk gloss-overlap disagreement) and its sources. It is a VETO, never a
     re-selection — the compound→head bridge survives, the wrong-domain chain does not.

PATH SELECTION: ``hypernym_paths()`` returns MULTIPLE paths when a synset has multiple hypernyms
(dog → {domestic_animal, canine}) and their ORDER is NOT stable across processes — so ``paths[0]``
would flicker (verified: poodle alternated domestic_animal↔canine). We pick ``min(paths,
key=(len, name-tuple))`` — the SHORTEST, most-direct is-a chain, lexicographic tie-break. Pure,
fixed, reproducible → same input yields the same ladder every run.

MULTIWORD TYPE WordNet LACKS → HEAD-NOUN REDUCTION (GAP B)
─────────────────────────────────────────────────────────
Many real types are noun-noun compounds WordNet has no synset for ("movie festival", "charity golf
tournament"). Universal-Dependencies / theoretical-morphology says the syntactic head of an
endocentric nominal compound is its RIGHTMOST noun, and the compound is a HYPONYM of that head — a
*movie festival* IS-A *festival*, a *charity golf tournament* IS-A *tournament* (right-hand head
rule; Williams 1981, "On the Notion 'Lexically Related' and 'Head of a Word'", Linguistic Inquiry
12(2); UD ``compound`` relation, https://universaldependencies.org/u/dep/compound.html). So when
the full compound misses, we reduce to the head noun, emit ``(compound, subclass_of, head)``, and
ladder the HEAD's WordNet chain. GUARD (keeps THE HARD LINE): only a CLEAN endocentric compound
reduces — every token alphabetic, NO closed-class connector ("of/for/the/and/…"). A phrase with a
connector ("Run for the Cure") is NOT a nominal compound → returns ``[]`` (never head-reduced).

SURFACE FORM: WordNet lemmas use underscores (``edible_fruit``, ``domestic_animal``); the rest of
FaultLine (entity_aliases, the count candidates, ``_singularize_phrase``, Hearst) uses SPACES. We
emit every node with SPACES so a laddered node ("edible fruit") is the SAME entity the query
resolves — an underscore node would be an unreachable orphan. WordNet lookup still uses the
underscore key.

BOUNDED: cap depth (default 3 rungs) and stop before a generic WordNet root (``entity``,
``physical_entity``, ``artifact``, ``organism`` …) so the ladder terminates at/just past a real
category (and, for seeded domains, at the seeded backbone node — poodle→dog→domestic_animal→
**animal**, then STOP; ``organism`` is generic). Never ladders to ``entity``/``abstraction`` (noise).

THE HARD LINE (enforced by the CALLER, reinforced here): only a common-noun **TYPE** node goes
through WordNet. A NAME/instance (Nobu, "Run for the Cure") must never get a hypernym — the caller
skips any object that is the subject of a naming edge (``also_known_as``/``pref_name``). As a second
line of defence, a multiword proper name is not a clean endocentric compound (or has no head
synset), and most proper names have no WordNet noun synset → this returns ``[]`` for them.

FALLBACK CHAIN for a WordNet MISS (novel/technical types with no head synset): this returns ``[]``;
the caller falls through to the EXISTING async LLM climb (``_query_llm_what_is`` /
``_query_llm_full_chain`` in the re_embedder) via the unchanged ``_queue_concept_for_grounding``
queue. No new online/runtime dependency is introduced.

PURE / OFFLINE: the corpus loads once (lazy module singleton); every call is a local DB lookup —
no network, CPU-cloud-safe. Never raises: any failure yields ``[]`` (fail-safe → today's behavior).
"""

from __future__ import annotations

import os
import re
import threading
from functools import lru_cache

import structlog

log = structlog.get_logger()

# ── Corpus singleton (lazy; loaded once, never per-call) ────────────────────────────────────────
_WN = None
_WN_TRIED = False
_WN_LOCK = threading.Lock()


def _wn():
    """Return the NLTK WordNet corpus reader, loading it ONCE. None if unavailable (fail-safe)."""
    global _WN, _WN_TRIED
    # [it branch] Princeton WordNet is an ENGLISH lexicon. On the Italian install the spine hands
    # it Italian lemmas, and English homographs would mint wrong is-a ladders ("cane" = dog →
    # WordNet cane.n = walking stick → stick; "camera" = room → camera.n = photographic device).
    # No Italian wordnet ships (OMW 'ita' is not in the image), so the source is INERT there —
    # the documented corpus-unavailable path (async LLM climb handles hypernymy).
    from src.extraction.install_language import english_grammar_available
    if not english_grammar_available():
        return None
    if _WN is not None or _WN_TRIED:
        return _WN
    with _WN_LOCK:
        if _WN is not None or _WN_TRIED:
            return _WN
        _WN_TRIED = True
        try:
            from nltk.corpus import wordnet as wn  # lazy — no import cost when the flag is off
            # Force the lazy corpus to actually resolve its data files now, so a missing
            # corpus fails HERE (→ None → graceful async fallback), not mid-ladder.
            wn.ensure_loaded()
            _WN = wn
            log.info("wordnet_ladder.corpus_loaded", note="offline WordNet hypernym source ready")
        except Exception as e:  # noqa: BLE001 — corpus absent/broken → deterministic no-op
            log.warning("wordnet_ladder.corpus_unavailable", error=str(e)[:160],
                        note="WordNet miss → async LLM climb handles hypernymy (fail-safe)")
            _WN = None
    return _WN


# GLiNER2 top-type → preferred WordNet lexicographer files (``lexname``), in preference order.
# NOT a domain word-zoo: these are WordNet's OWN top-level lexname buckets (structural, documented),
# keyed by the FIXED GLiNER2 label set — the type-alignment half of the deterministic rule.
# ORDER IS SIGNIFICANT: rule #2 ranks a candidate synset by the INDEX of its lexname here (earlier =
# preferred), so ``noun.food`` outranking ``noun.substance`` for an Object steers an edible name to
# its fruit sense rather than a chemical homonym.
_GLINER_LEXNAME: dict[str, tuple[str, ...]] = {
    "person": ("noun.person",),
    "animal": ("noun.animal",),
    "object": ("noun.artifact", "noun.object", "noun.food", "noun.substance", "noun.plant", "noun.body"),
    "location": ("noun.location", "noun.artifact"),
    "organization": ("noun.group",),
    "event": ("noun.event", "noun.act", "noun.process"),
    "concept": ("noun.cognition", "noun.attribute", "noun.state", "noun.feeling"),
}

# Generic WordNet upper-ontology roots — TERMINATE BEFORE these (stored UNDERSCORE-form, as WordNet
# lemmas are; checked against the raw lemma before space-normalisation). They carry no classification
# signal (WordNet's is-a tree tops out at {entity}→{physical_entity, abstraction, thing}). This is
# WordNet's OWN documented upper ontology + the pervasive mid-level catch-alls, NOT a subject
# enumeration. Mirrors the re_embedder ``_validate_bridge_placement`` upper-root guard.
_GENERIC_ROOTS: frozenset = frozenset({
    "entity", "physical_entity", "physical_object", "abstraction", "abstract_entity",
    "thing", "object", "whole", "unit", "part", "living_thing", "organism",
    "causal_agent", "matter", "substance", "material", "psychological_feature",
    "cognition", "attribute", "state", "group", "grouping", "collection",
    "artifact", "artefact", "instrumentality", "instrumentation", "relation",
    "person", "adult",
})

# Closed function-word class of compound CONNECTORS. A multiword surface containing any of these is
# NOT an endocentric noun-noun compound (it is a phrase / proper-name title) → NEVER head-reduced.
# A legitimate CLOSED grammatical class (like the Hearst markers), NOT a domain word-list.
_CONNECTORS: frozenset = frozenset({
    "of", "for", "the", "a", "an", "and", "or", "to", "in", "on", "with",
    "at", "by", "from", "as", "per", "via", "&",
})

# Default depth cap (rungs from the input term). Bounded per the options doc (~3-4). Env-overridable.
_DEPTH_DEFAULT = 3

# Bound on how many WordNet synset SYNONYM lemmas a laddered node registers as aliases (see
# ``synset_synonyms``). A synset is a small closed set of synonymous lemmas — cap keeps a rare
# large synset from spraying aliases; deterministic (WordNet's fixed lemma order).
_SYNONYM_CAP = 8


def _depth_cap() -> int:
    try:
        n = int(os.getenv("L4_WORDNET_LADDER_DEPTH", str(_DEPTH_DEFAULT)) or _DEPTH_DEFAULT)
    except Exception:  # noqa: BLE001
        return _DEPTH_DEFAULT
    return max(1, min(6, n))


def _clean_common_noun(term: str) -> str | None:
    """Return the WordNet-lookup key for a CLEAN common-noun type, else None.

    Rejects empty / scalar-looking / obviously-non-lexical surfaces (digits, punctuation such as
    IPs/emails/dates) so a leaked scalar can never ladder. Spaces/hyphens → underscores (WordNet
    lemma form) for the LOOKUP KEY only — emitted node surfaces use spaces (see module docstring).
    """
    t = (term or "").strip().lower()
    if not t:
        return None
    # Scalar / literal guard (same spirit as _validate_bridge_placement): a value is never a type.
    if any(c.isdigit() for c in t):
        return None
    if any(ch in t for ch in "@/:."):
        return None
    return t.replace(" ", "_").replace("-", "_")


def _surface(term: str) -> str:
    """Canonical SPACE-form node surface (matches entity_aliases / count candidates / Hearst)."""
    return (term or "").strip().lower().replace("_", " ")


def _synsets_for(term_key: str):
    """The noun synsets for a WordNet lookup key, or [] (fail-safe). Every returned synset has
    ``term_key`` as one of its lemmas (WordNet's ``synsets`` contract)."""
    wn = _wn()
    if wn is None:
        return []
    try:
        return wn.synsets(term_key, pos="n")
    except Exception:  # noqa: BLE001
        return []


def _pick_by_lexname_or_mfs(synsets, gliner_type: str | None):
    """Rule #2 (RANKED lexname preference) → rule #3 (MFS). Deterministic."""
    prefs = _GLINER_LEXNAME.get((gliner_type or "").strip().lower(), ())
    if prefs:
        ranked = []
        for si, s in enumerate(synsets):
            try:
                lx = s.lexname()
            except Exception:  # noqa: BLE001
                continue
            if lx in prefs:
                ranked.append((prefs.index(lx), si, s))
        if ranked:
            ranked.sort(key=lambda x: (x[0], x[1]))
            return ranked[0][2]
    # MFS / first-sense baseline (WordNet's frequency-ordered first synset).
    return synsets[0] if synsets else None


def _category_synset(context_hypernym: str | None, gliner_type: str | None):
    """Resolve the co-occurring CATEGORY surface to ONE WordNet synset for the closure test.

    Uses the lexname/MFS pick (NOT the context rule — no recursion) and, if the category is itself
    a multiword compound WordNet lacks, its head noun. Returns None when nothing resolves."""
    if not context_hypernym:
        return None
    ck = _clean_common_noun(context_hypernym)
    if ck is None:
        return None
    cs = _synsets_for(ck)
    if cs:
        return _pick_by_lexname_or_mfs(cs, gliner_type)
    head = _head_noun(context_hypernym)
    if head:
        hs = _synsets_for(head)
        if hs:
            return _pick_by_lexname_or_mfs(hs, gliner_type)
    return None


def _select_synset(term_key: str, gliner_type: str | None, context_hypernym: str | None = None):
    """Deterministic synset selection: context closure-containment → ranked lexname → MFS."""
    synsets = _synsets_for(term_key)
    if not synsets:
        return None
    # RULE 1 (strongest): CONTEXT closure-containment — the member sense whose is-a closure CONTAINS
    # the queried category's synset (lands member + category on the SAME chain). GAP A.
    cat = _category_synset(context_hypernym, gliner_type)
    if cat is not None:
        for s in synsets:
            try:
                if s == cat or cat in set(s.closure(lambda x: x.hypernyms())):
                    return s
            except Exception:  # noqa: BLE001
                continue
        # No member sense reaches the category → do NOT force it (honest under-capture); fall through.
    # RULE 2 (ranked lexname) → RULE 3 (MFS).
    return _pick_by_lexname_or_mfs(synsets, gliner_type)


def has_common_noun_sense(term: str) -> bool | None:
    """True iff `term` names a COMMON-noun TYPE — a noun synset whose matching lemma is
    LOWERCASE as stored (WordNet's convention: proper-noun lemmas are CAPITALIZED — Princeton
    WordNet, `wngloss(7WN)`; Miller et al. *Five Papers on WordNet`, section 3).

    THE USE (the ladder HARD-LINE guard's morphology arm, `src/api/hardline_guard.py` R6): a
    user-ASSERTED object surface that resolves ONLY to capitalized/proper lemmas (`halifax` →
    `Halifax`) or to nothing at all (`krellin`, `zzcanaryglyph`) is a NAME/coinage — a memory,
    never an L4 type. A surface with a lowercase lemma (`dog`, `poodle`, `blue` as a kind of
    colour) IS a common noun and still ladders. Morphological variants count: `wn.synsets`
    applies `morphy`, so `dogs` resolves to `dog`'s synsets and the lemma-equality test below
    accepts the variant. A multiword surface is head-reduced (`wireless mouse` → `mouse`) by the
    same endocentric-compound head rule the ladder itself uses.

    Returns None when the lexical DB is unavailable (the CALLER treats None as NOT-a-common-type,
    fail-closed). Deterministic, offline, subject-agnostic — a lexical FACT, no word list."""
    wn = _wn()
    if wn is None:
        return None
    key = _clean_common_noun(term or "")
    if key is None:
        return False  # empty/scalar-looking surface (digits, IPs, emails…) — never a type
    # A multiword compound: judge by its head noun (same endocentric rule as the ladder).
    if " " in term.strip() or "-" in term.strip():
        head = _head_noun(term)
        if head:
            key = _clean_common_noun(head) or key
    try:
        variants = {key}
        try:
            _mph = wn.morphy(key, "n")  # NLTK returns a str (or list of str in newer versions)
            if isinstance(_mph, str):
                variants.add(_mph)
            elif _mph:
                variants.update(f for f in _mph if f)
        except Exception:  # noqa: BLE001 — morphy is best-effort; the exact key still tested
            pass
        syns = _synsets_for(key)
        for s in syns:
            for lemma in s.lemmas():
                # EXACT lemma-name membership (stored form): 'Halifax' ≠ 'halifax' → proper;
                # 'dog' == 'dog' (and ∈ variants for 'dogs') → common.
                if lemma.name() in variants:
                    return True
        return False
    except Exception:  # noqa: BLE001
        return False


def synset_synonyms(surface: str, gliner_type: str | None = None) -> list[str]:
    """The SPACE-form WordNet SYNONYM lemmas of the synset a laddered NODE surface names — its
    co-synset lemmas EXCLUDING the surface itself (e.g. ``citrus`` → ``['citrus fruit',
    'citrous fruit']``). ``[]`` on any miss/error. Deterministic + OFFLINE, NEVER raises.

    WHY (the cross-synonym query-surface gap): the WordNet ladder emits a hypernym node under the
    synset's PRIMARY lemma (``_rungs_from_synset`` appends ``lemmas()[0]``), so members ladder under
    ``citrus`` — but a user querying "how many **citrus fruits**" grounds the surface "citrus fruit",
    a DISTINCT entity. They share the WordNet SYNSET but not a graph node, so the count walk misses
    the members. Registering the node's synset synonyms as ``also_known_as`` aliases collapses both
    surfaces onto the SAME node UUID. WordNet synonymy IS synset membership: "a synset is a set of
    synonyms" (Princeton WordNet, ``wngloss(7WN)``; Fellbaum 1998) — an offline lexical FACT, no
    cosine/LLM. HARD LINE safe: these are TYPE-node synonyms (common-noun lemmas), never NAMES.

    DETERMINISTIC synset pick: prefer the synset whose PRIMARY lemma equals ``surface`` (the exact
    form ``_rungs_from_synset`` emits for a node — so we recover the synset that was laddered), else
    the same ranked-lexname/MFS pick used everywhere else. This is CONSERVATIVE by design: when a
    surface has several senses (``bag`` = bag.n.01 sack vs bag.n.04 handbag), we take the primary
    (MFS) synset's synonyms only — honest under-capture (the module's own fall-through rule), never a
    wrong/over-broad alias. Bounded by ``_SYNONYM_CAP``. Only clean common-noun surfaces resolve
    (``_clean_common_noun`` rejects digits/punctuation), generic upper-ontology roots are dropped,
    and non-lexical lemma forms (digits / ``dr.``-style abbreviations) are filtered, so a scalar,
    root, or punctuation token can never spray aliases.
    """
    key = _clean_common_noun(surface)
    if key is None:
        return []
    synsets = _synsets_for(key)
    if not synsets:
        return []
    surf = _surface(surface)
    chosen = None
    for s in synsets:
        try:
            prim = s.lemmas()[0].name().strip().lower().replace("_", " ")
        except Exception:  # noqa: BLE001
            continue
        if prim == surf:
            chosen = s
            break
    if chosen is None:
        chosen = _pick_by_lexname_or_mfs(synsets, gliner_type)
    if chosen is None:
        return []
    out: list[str] = []
    seen: set = {surf}
    try:
        lemmas = chosen.lemmas()
    except Exception:  # noqa: BLE001
        return []
    for lem in lemmas:
        try:
            ls = lem.name().strip().lower().replace("_", " ")
        except Exception:  # noqa: BLE001
            continue
        if not ls or ls in seen:
            continue
        if ls.replace(" ", "_") in _GENERIC_ROOTS:  # never alias a node to a generic root
            continue
        # Non-lexical lemma guard (mirrors _clean_common_noun): drop digit-bearing or
        # punctuation-bearing forms ("dr.", abbreviations) — an alias must be a real word surface.
        if any(c.isdigit() for c in ls) or any(ch in ls for ch in "@/:."):
            continue
        seen.add(ls)
        out.append(ls)
        if len(out) >= _SYNONYM_CAP:
            break
    return out


def is_person_noun(term: str) -> bool | None:
    """Is a common-noun TYPE term a PERSON role (WordNet lexname ``noun.person``)? Tri-state.

    Returns ``True`` when the term's MOST-FREQUENT-SENSE noun synset is filed under WordNet's
    ``noun.person`` lexicographer file (curator/manager/doctor/friend/real estate agent — a role a
    PERSON holds), ``False`` when it resolves to a non-person noun class (gallery/museum/restaurant/
    company — a class a THING is an ``instance_of``), and ``None`` when WordNet is unavailable or the
    term has no noun synset (caller decides the safe default).

    THE USE (Hearst "NP, a(n) NP" appositive-hyponymy routing): the appositive ``<Name>, a/an <type>``
    is is-a hyponymy in general (Hearst 1992), but FaultLine files a PERSON's occupation as ``has_role``
    and a non-person's class as ``instance_of``. The person-vs-thing split is exactly WordNet's
    ``noun.person`` lexname partition — a documented lexical FACT (Miller 1995 / Fellbaum 1998), the
    SAME offline signal ``_pick_by_lexname_or_mfs`` already ranks on. Subject-agnostic, deterministic,
    OFFLINE — NO cosine, NO LLM, NO domain word-list.

    MFS (first synset) is used deliberately: a term whose DOMINANT sense is a person ("curator",
    "manager") is a person role; a secondary person sense on a dominantly-non-person term ("agent" →
    chemical agent MFS) does not make the head a person. Fail-safe → ``None`` (never raises).
    """
    key = _clean_common_noun(term)
    if key is None:
        return None
    synsets = _synsets_for(key)
    if not synsets:
        return None
    try:
        return synsets[0].lexname() == "noun.person"
    except Exception:  # noqa: BLE001 — corpus quirk → unknown, caller picks the safe default
        return None


# ── ALIENABILITY (possessive-construction disambiguation) ───────────────────────────────────────
# WordNet supersenses whose DOMINANT (most-frequent) sense marks the head noun as INALIENABLY
# possessed — i.e. "my X" is a constitutive/relational reading, NEVER an ownership transfer.
#
# EVIDENCE. English marks alienable and inalienable possession with the SAME possessive
# construction ("Mary's brother" [inalienable] vs "Mary's squirrel" [alienable]), so the
# disambiguator must be the SEMANTIC CLASS OF THE HEAD NOUN, not the syntax. The cross-
# linguistically stable inalienable core is **body parts, kinship terms, part-whole and
# relational nouns** (Nichols 1988; Chappell & McGregor 1996; Heine 1997) — see
# https://en.wikipedia.org/wiki/Inalienable_possession and Haspelmath, "Alienable vs. inalienable
# possessive constructions" (MPI-EVA):
# https://www.eva.mpg.de/lingua/conference/08_springschool/pdf/course_materials/Haspelmath_Possessives.pdf
#
# THE MAPPING onto WordNet's lexicographer files (``lexnames(5WN)``, the documented 26-file noun
# partition — "08 noun.body: nouns denoting body parts", "24 noun.relation: nouns denoting
# relations between people or things or ideas";
# https://manpages.ubuntu.com/manpages/bionic/man5/lexnames.5WN.html) is DIRECT for two of the
# four inalienable classes. The KINSHIP class is deliberately ABSENT here: it is already routed
# upstream by the ``kinship_noun`` cue class (which emits the SPECIFIC kin relation), and vetoing
# on ``noun.person`` would wrongly demote real artifacts whose most-frequent sense is an agent
# noun ("printer" → MFS ``noun.person``, "one who prints"). PART-WHOLE beyond ``noun.relation``
# is likewise left to the ``relational_noun`` cue class.
#
# DOMINANT-SENSE (MFS) rather than any-sense: the MFS/first-sense baseline is WordNet's own
# frequency ordering (SemCor) and is the standard first-sense WSD baseline (Jurafsky & Martin
# SLP3 App. C) — the SAME rule ``is_person_noun`` above already applies, for the same reason: a
# term whose DOMINANT sense is a body part IS a body part; a secondary body sense on a dominantly
# artifactual term does not make it inalienable.
_INALIENABLE_DOMINANT: frozenset = frozenset({"noun.body", "noun.relation"})


def _synsets_for_term(term: str):
    """Noun synsets for a free-text term: the raw lookup key UNIONed with WordNet's ``morphy``
    base form, in a stable order (raw first, then morphy-only additions).

    Why the union: the raw surface may be an inflected form whose plural happens to have its OWN
    synset with a different supersense (``devices`` → ``devices.n.01`` ``noun.cognition``, "plans
    or schemes", while ``device`` → ``noun.artifact``). Taking BOTH is the fail-safe direction for
    every caller here — more senses can only make a type-compatibility test MORE permissive, never
    more destructive. Returns [] when the corpus is unavailable / the term is non-lexical."""
    key = _clean_common_noun(term)
    if key is None:
        return []
    out = list(_synsets_for(key))
    wn = _wn()
    if wn is None:
        return out
    try:
        base = wn.morphy(key, "n")
    except Exception:  # noqa: BLE001
        base = None
    if base and base != key:
        seen = {s.name() for s in out}
        for s in _synsets_for(base):
            if s.name() not in seen:
                out.append(s)
    return out


def is_inalienable_dominant(term: str) -> bool | None:
    """Is ``term``'s DOMINANT (most-frequent) WordNet sense an INALIENABLE noun class? Tri-state.

    ``True``  — the MFS lexicographer file is ``noun.body`` or ``noun.relation`` ("my eye", "my
                part"): the possessive is a constitutive/relational reading, never ownership.
    ``False`` — the term has noun senses and the dominant one is not an inalienable class.
    ``None``  — WordNet unavailable / no noun synset / non-lexical surface. The CALLER picks the
                safe default (which for a suppression decision must be "keep the edge").

    Named by its established term: the **alienable/inalienable possession** distinction. See
    ``_INALIENABLE_DOMINANT`` above for the sources. Deterministic + OFFLINE — no cosine, no LLM,
    no domain word-list (WordNet's own documented supersense partition). Never raises."""
    synsets = _synsets_for_term(term)
    if not synsets:
        return None
    try:
        return synsets[0].lexname() in _INALIENABLE_DOMINANT
    except Exception:  # noqa: BLE001 — corpus quirk → unknown, caller picks the safe default
        return None


def supersense_type_match(term: str, entity_types) -> bool | None:
    """Does ``term`` have ANY noun sense whose WordNet **supersense** (lexicographer file) falls in
    the bucket(s) of ANY declared entity type in ``entity_types``? Tri-state.

    ``True``  — at least one sense is type-compatible (or a declared type is the ``ANY``
                catch-all, or the declared types map to no bucket we can test).
    ``False`` — the term HAS noun senses and NONE of them lands in the declared buckets.
    ``None``  — undecidable: WordNet unavailable, no noun synset, or a non-lexical surface.

    THE USE. ``rel_types.tail_types`` is this engine's ``rdfs:range``; checked at write time under
    a closed-world reading it is the SHACL ``sh:class`` value-type constraint
    (https://www.w3.org/TR/shacl/#ClassConstraintComponent). When the object entity has no GLiNER2
    type, WordNet's supersense partition is a deterministic OFFLINE type oracle for the same
    check — supersenses are exactly the "coarse level of generalization for essential contextual
    distinctions — artifact vs. person" the supersense-tagging literature identifies (Ciaramita &
    Johnson 2003, *Supersense Tagging of Unknown Nouns in WordNet*,
    https://aclanthology.org/W03-1022.pdf; Ciaramita & Altun 2006,
    https://home.ttic.edu/~altun/pubs/CiaAlt_EMNLP06.pdf).

    ANY-SENSE, not MFS, is deliberate: the question is not "which sense is meant" (WSD) but "does
    this noun have a type-compatible reading AT ALL" — an existential compatibility test whose
    error direction is UNDER-suppression. MFS here would wrongly reject real artifacts whose
    dominant sense is elsewhere ("ring" → MFS ``noun.attribute`` (a sound), "shoes" → MFS
    ``noun.state``, "printer" → MFS ``noun.person``).

    Buckets come from ``_GLINER_LEXNAME`` — WordNet's OWN top-level supersense partition keyed by
    the FIXED GLiNER2 label set, already used by the ladder's rule #2. NOT a domain word-list.
    Deterministic + OFFLINE. Never raises."""
    buckets: set = set()
    try:
        for t in (entity_types or ()):
            tt = (t or "").strip().lower()
            if not tt:
                continue
            if tt == "any":
                return True  # unconstrained range — nothing to check
            buckets |= set(_GLINER_LEXNAME.get(tt, ()))
    except Exception:  # noqa: BLE001
        return None
    if not buckets:
        return None  # declared types map to no testable bucket → undecidable, caller stays safe
    synsets = _synsets_for_term(term)
    if not synsets:
        return None
    try:
        return any(s.lexname() in buckets for s in synsets)
    except Exception:  # noqa: BLE001 — corpus quirk → undecidable
        return None


def name_head_type(surface: str) -> str | None:
    """The HEAD common TYPE-NOUN of a PROPER-NAME run whose tail IS a countable common noun
    (``"natural history museum"`` → ``"museum"``, ``"luigi's restaurant"`` → ``"restaurant"``),
    or ``None`` when the tail is NOT a common type-noun (``"new york"``/``"lake tahoe"``/
    ``"the getty"`` → ``None``). ``None`` on any miss/error. Deterministic + OFFLINE, NEVER raises.

    THE USE (name-head-noun instance-typing — THE HARD LINE): a captured PROPER-NAMED PLACE
    whose name ENDS IN a common type-noun IS-A that type — "the Natural History Museum" is a
    museum, "Luigi's Restaurant" is a restaurant. The caller files ``(full-name, instance_of,
    head-type)`` so a "how many `<type>`" count can ground the type node and count the instance.
    The FULL proper-name run is the NAMING layer (the ``instance_of`` SUBJECT — an entity/alias,
    NEVER classified into L4); the head common-noun is the L4 TYPE. This is the endocentric
    RIGHT-HAND HEAD RULE (Williams 1981, *On the Notion 'Lexically Related' and 'Head of a
    Word'*, Linguistic Inquiry 12(2); UD ``compound``): the syntactic head of "<Mod…> Museum"
    is "Museum", and the compound is a hyponym/instance of that head. Subject-agnostic — NO
    place/type/domain word-list, purely the head's WordNet common-noun status.

    PRINCIPLED GUARDS (reject a proper name whose tail merely LOOKS like a common noun — the
    required "New York"→NOT ``york``, "Lake Tahoe"→NOT ``tahoe`` negatives):
      (A) **FULL-SURFACE INSTANCE guard** — if the WHOLE surface is a KNOWN WordNet proper-name
          INSTANCE (a synset carrying ``instance_hypernyms``: ``new_york``, ``lake_tahoe``,
          ``colorado_springs``), it is a recognized named place, NOT a "<Mod> <type>" compound
          → ``None``. WordNet's documented CLASS-vs-INSTANCE split (Miller & Hristea 2006,
          *WordNet Nouns: Classes and Instances*; the ``@i`` instance-hypernym pointer).
      (B) **HEAD PROPER-NOUN guard** — the head's MFS noun synset's matching lemma must be a
          COMMON noun, i.e. LOWERCASE. WordNet stores PROPER-noun lemmas CAPITALIZED (Princeton
          WordNet convention), so "york" → lemma ``York`` (proper) → ``None``, while "museum" →
          ``museum`` and "restaurant" → ``restaurant`` (common) are kept.
      (C) **HEAD INSTANCE / PERSON / GENERIC guards** — the head MFS synset must be a CLASS
          (no ``instance_hypernyms`` — rejects ``michigan``), NOT a ``noun.person`` role
          (``is_person_noun`` — rejects a surname head), and not a generic upper-ontology root.
      **Multi-token requirement** — a BARE common noun ("museum") is a TYPE, not a named
      INSTANCE, so ≥2 tokens are required (the CALLER vouches the surface is a proper name;
      here we only certify the head is a real common type-noun). Fail-safe → ``None``.
    """
    wn = _wn()
    if wn is None:
        return None
    s = (surface or "").strip().lower()
    toks = [t for t in re.split(r"[\s_]+", s) if t]
    if len(toks) < 2:
        return None
    # (A) FULL-SURFACE known-instance guard (New York / Lake Tahoe / Colorado Springs).
    full_key = _clean_common_noun(s)
    if full_key is not None:
        try:
            _fs = wn.synsets(full_key, pos="n")
            if _fs and _fs[0].instance_hypernyms():
                return None
        except Exception:  # noqa: BLE001 — fail-safe: no full-surface signal → proceed to head
            pass
    head = (toks[-1] or "").strip(" '\"")
    key = _clean_common_noun(head)
    if key is None or key in _GENERIC_ROOTS:
        return None
    synsets = _synsets_for(key)
    if not synsets:
        return None  # tahoe / getty / luigi — no common-noun synset
    m = synsets[0]
    try:
        if m.instance_hypernyms():  # (C) INSTANCE synset (michigan) — not a class type
            return None
        # (B) PROPER-NOUN lemma guard: the lemma matching the head must be LOWERCASE (common).
        _matched = None
        for lem in m.lemmas():
            if lem.name().lower().replace("_", " ") == head:
                _matched = lem.name()
                break
        if _matched is None:
            _matched = m.lemmas()[0].name()
        if _matched != _matched.lower():  # capitalized → proper noun (York, Michigan)
            return None
    except Exception:  # noqa: BLE001 — corpus quirk → uncertain → safe default (no typing)
        return None
    if is_person_noun(head) is True:  # (C) a person-role head (a surname) is never a place type
        return None
    return _surface(head)


def category_hyponym_lemmas(category_surface: str,
                            gliner_type: str | None = None) -> set[str]:
    """SPACE-form WordNet HYPONYM lemma surfaces subsumed by a common-noun CATEGORY.

    ``"citrus fruit"`` → ``{"orange", "lemon", "lime", "grapefruit", "citron", …}``;
    ``"fruit"`` → the full edible-fruit closure. ``set()`` on any miss/error.

    WHY (SUBSUMPTION-count over a taxonomy — the sanctioned hypernym-membership option):
    counting "how many <category>" is the cardinality of the class under hyponymy —
    ``|{x : x is-a* category}|`` (subsumption counting; Baader et al., *DL Handbook*).
    When the members are lexicalized MORE SPECIFICALLY than the query term (a user says
    "orange bitters" / "lime juice", never "citrus fruit"), the count walk resolves the
    category to its WordNet hyponym set and counts the DISTINCT members present in the
    user's captured surfaces. Hyponymy is WordNet's ``~`` pointer — the "X is-a-kind-of Y"
    relation between synsets (Miller 1995, *WordNet: An Electronic Lexical Database*;
    Fellbaum 1998) — the SAME is-a axis L4 walks; an offline lexical FACT, no cosine/LLM.

    DETERMINISTIC + OFFLINE: the category resolves to ONE synset via the module's fixed
    ranked-lexname/MFS cascade (``_select_synset``), with GAP-B head-reduction for a
    WordNet-less compound; the transitive hyponym closure is a fixed WordNet walk. Returned
    as a SET (membership is order-independent, so the non-stable closure order is immaterial).

    GUARD: a GENERIC upper-ontology category (``thing``/``object``/``entity``/… in
    ``_GENERIC_ROOTS``) returns ``set()`` — it carries no classification signal and its
    closure is the whole lexicon, which would over-count. Digit/punctuation-bearing and
    generic-root lemmas are dropped. NEVER raises (any failure → ``set()`` = today's behavior).
    """
    key = _clean_common_noun(category_surface)
    if key is None:
        return set()
    # Never enumerate a generic upper-ontology category (thing/object/entity/…): no signal,
    # and its closure is effectively the whole noun lexicon → guaranteed over-count.
    if key in _GENERIC_ROOTS:
        return set()
    syn = _select_synset(key, gliner_type)
    if syn is None:
        head = _head_noun(category_surface)
        if head and head not in _GENERIC_ROOTS:
            syn = _select_synset(head, gliner_type)
    if syn is None:
        return set()
    out: set[str] = set()
    try:
        for h in syn.closure(lambda x: x.hyponyms()):
            try:
                lemmas = h.lemmas()
            except Exception:  # noqa: BLE001
                continue
            for lem in lemmas:
                try:
                    ls = lem.name().strip().lower().replace("_", " ")
                except Exception:  # noqa: BLE001
                    continue
                if not ls or ls.replace(" ", "_") in _GENERIC_ROOTS:
                    continue
                # Non-lexical guard (mirrors _clean_common_noun): a member surface is a real word.
                if any(c.isdigit() for c in ls) or any(ch in ls for ch in "@/:."):
                    continue
                out.add(ls)
    except Exception:  # noqa: BLE001 — fail-safe: no members, never a fabricated set
        return set()
    return out


def _head_noun(term: str) -> str | None:
    """The HEAD noun of a CLEAN endocentric nominal compound (rightmost token, UD ``compound`` /
    right-hand head rule), else None. Guards THE HARD LINE: only a >=2-token, all-alphabetic surface
    with NO closed-class connector reduces — a connector ("Run FOR THE Cure") means it is a phrase /
    proper-name title, never a noun-noun compound. Deterministic, subject-agnostic (structural)."""
    toks = [t for t in re.split(r"[\s_\-]+", (term or "").strip().lower()) if t]
    if len(toks) < 2:
        return None
    if any(not t.isalpha() for t in toks):
        return None
    if any(t in _CONNECTORS for t in toks):
        return None
    return toks[-1]


def _modifier_tokens(term: str) -> list[str]:
    """The MODIFIER tokens of a clean endocentric nominal compound — every token LEFT of the head
    ("kiln firing" → ``["kiln"]``; "charity golf tournament" → ``["charity", "golf"]``). ``[]`` when
    the surface is not a clean compound (mirrors ``_head_noun``'s guards exactly).

    The modifier is the disambiguating signal this module used to THROW AWAY: in an endocentric
    N-N compound the modifier's function is precisely to RESTRICT the head's denotation (Levi 1978,
    *The Syntax and Semantics of Complex Nominals*; Downing 1977). "kiln firing" is a firing-of-a-
    kiln, not a firing-at-an-enemy."""
    toks = [t for t in re.split(r"[\s_\-]+", (term or "").strip().lower()) if t]
    if len(toks) < 2 or any(not t.isalpha() for t in toks) or any(t in _CONNECTORS for t in toks):
        return []
    return toks[:-1]


# ── MODIFIER-SENSE VETO (compound WSD abstention) ───────────────────────────────────────────────
# Flag: L4_WORDNET_MODIFIER_WSD (default OFF → this whole lane is a no-op and the ladder is
# byte-identical to today). See ``_modifier_contradicts_sense`` for the rule + sources.
_LESK_MIN_TOKEN = 3      # content tokens shorter than this carry no gloss signal
_MODIFIER_CAP = 3        # bound the modifier tokens consulted (a compound head is right-branching)


def _flag_modifier_wsd() -> bool:
    return (os.getenv("L4_WORDNET_MODIFIER_WSD", "false") or "false").strip().lower() in (
        "1", "true", "yes", "on")


@lru_cache(maxsize=8192)
def _is_content_word(tok: str) -> bool:
    """Is ``tok`` an OPEN-CLASS (content) word? Deterministic, WordNet-native, NO stoplist.

    Lesk requires content-word overlap; function words ("and", "for", "with", "than") otherwise
    dominate the score. WordNet deliberately covers ONLY the four open classes — "WordNet contains
    only nouns, verbs, adjectives and adverbs … the function words of English are not included"
    (Princeton WordNet, ``wngloss(7WN)``). So "has ANY WordNet synset" IS the open-class test, and
    we need no hand-curated stopword list (which would be exactly the forbidden word-zoo shape)."""
    wn = _wn()
    if wn is None:
        return False
    try:
        return bool(wn.synsets(tok))
    except Exception:  # noqa: BLE001
        return False


def _gloss_tokens(blob: str) -> frozenset:
    """Lemmatised open-class content tokens of a gloss/example string (Lesk's 'gloss bag')."""
    wn = _wn()
    if wn is None:
        return frozenset()
    out: set = set()
    for w in re.findall(r"[a-z]+", (blob or "").lower()):
        if len(w) < _LESK_MIN_TOKEN:
            continue
        try:
            base = wn.morphy(w) or w
        except Exception:  # noqa: BLE001
            base = w
        if _is_content_word(base):
            out.add(base)
    return frozenset(out)


@lru_cache(maxsize=4096)
def _sense_bag(synset_name: str) -> frozenset:
    """The ADAPTED-LESK gloss bag of ONE sense: its own gloss + examples + lemma names, UNIONed with
    the SAME for its DIRECT hypernyms.

    Adapted/Extended Lesk (Banerjee & Pedersen 2002/2003) expands each candidate sense with the
    glosses of WordNet-RELATED synsets rather than the gloss alone. We deliberately expand ONLY one
    rung UP (direct hypernyms) and NOT down the hyponym closure: measured on the real bench corpus,
    the full closure makes the score a proxy for SENSE POPULARITY (a sense with many hyponyms wins
    regardless of context), which inverted the decision on real compounds. The tight bag keeps the
    overlap attributable to the sense's own definition."""
    wn = _wn()
    if wn is None:
        return frozenset()
    try:
        syn = wn.synset(synset_name)
        related = [syn] + list(syn.hypernyms())
    except Exception:  # noqa: BLE001
        return frozenset()
    out: set = set()
    for x in related:
        try:
            out |= _gloss_tokens((x.definition() or "") + " " + " ".join(x.examples() or ()))
            for lem in x.lemmas():
                out |= _gloss_tokens(lem.name().replace("_", " "))
        except Exception:  # noqa: BLE001
            continue
    return frozenset(out)


@lru_cache(maxsize=4096)
def _topic_domains(synset_name: str) -> frozenset:
    """WordNet TOPIC-domain synsets of a sense and its DIRECT hypernyms.

    The ``;c`` "Domain of synset - TOPIC" pointer marks a sense as belonging to a SPECIALIST subject
    field (``tank.n.01`` → ``military``, ``light.n.01`` → ``physics``, ``sitting.n.01`` →
    ``photography``). Princeton WordNet ``wninput(5WN)`` / ``wngloss(7WN)`` document the domain
    pointers; the domain (a.k.a. "subject field") annotation is the standard knowledge-based WSD
    signal for domain-driven disambiguation (Magnini & Cavaglià 2000, *Integrating Subject Field
    Codes into WordNet*, LREC; Navigli 2009, *Word Sense Disambiguation: A Survey*, ACM Comput.
    Surv. 41(2), §"Domain-driven approaches"). One rung up is included because the domain is
    frequently annotated on the parent (``fire.n.02`` is undomained but its hypernym
    ``attack.n.01`` is ``;c military``)."""
    wn = _wn()
    if wn is None:
        return frozenset()
    out: set = set()
    try:
        syn = wn.synset(synset_name)
        for x in [syn] + list(syn.hypernyms()):
            try:
                out |= {y.name() for y in x.topic_domains()}
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return frozenset()
    return frozenset(out)


def _first_parent_name(synset) -> str | None:
    """The synset name of the rung-1 PARENT this module would MINT for a sense (same shortest-path
    rule ``_rungs_from_synset`` uses), or None."""
    try:
        paths = synset.hypernym_paths()
    except Exception:  # noqa: BLE001
        return None
    if not paths:
        return None
    p = min(paths, key=lambda x: (len(x), tuple(s.name() for s in x)))
    return p[-2].name() if len(p) >= 2 else None


def _modifier_contradicts_sense(head_key: str, modifiers: tuple, chosen) -> bool:
    """Does the compound MODIFIER contradict the sense this module picked for the compound's HEAD?

    Returns True ⇒ the caller must ABSTAIN from laddering the head (place NO hypernym chain). This
    is a **veto**, never a re-selection: we refuse a placement we cannot justify, we never guess a
    different sense (measured: gloss-overlap RANKING switched ~46 real compounds at roughly chance
    accuracy, while the veto below fired 4/4 correctly — so ranking is not shipped).

    THE FAILURE IT CLOSES. "My kiln firing is 9 hours" head-reduced to the bare noun ``firing``,
    whose Most-Frequent-Sense is ``fire.n.02`` — *the act of firing weapons at an enemy* — and MINTED
    ``firing → fire → attack → operation`` into L4, which recall then rendered as "Firing is a
    subclass of attack". The disambiguating evidence was in the input all along: in an endocentric
    N-N compound the MODIFIER exists precisely to RESTRICT the head's denotation (Levi 1978, *The
    Syntax and Semantics of Complex Nominals*; Downing 1977, "On the Creation and Use of English
    Compound Nouns", Language 53(4); right-hand head rule — Williams 1981). ``kiln`` restricts
    ``firing`` to a heating sense; ``rifle`` would not.

    THE RULE — a DUAL-SIGNAL AGREEMENT gate (the same shape as this engine's negation dual-gate:
    act only when two INDEPENDENT deterministic signals agree). ALL must hold:

      1. **MATERIAL divergence** — the head is polysemous AND its candidate senses would mint
         DIFFERENT rung-1 parents. If every sense ladders to the same parent the choice cannot
         poison the index, so we never interfere.
      2. **SPECIALIST-DOMAIN mismatch (signal A, structural)** — the chosen sense carries a WordNet
         ``;c`` TOPIC domain (it is a subject-field-restricted sense: military / physics /
         photography …) that NONE of the modifiers' own senses carry. Domain-driven WSD: Magnini &
         Cavaglià 2000; Navigli 2009 §domain-driven.
      3. **LESK disagreement (signal B, lexical)** — Adapted-Lesk gloss overlap (Lesk 1986;
         Banerjee & Pedersen 2002/2003) between the modifiers' gloss bag and each candidate sense's
         gloss bag does NOT rank the chosen sense first. If the modifier's own glosses back the
         chosen sense ("rifle firing" → ``fire.n.02``), we keep it.
      4. **Modifier not itself in that domain** — the modifiers share no gloss overlap with the
         specialist DOMAIN synset either (a "military drill" modifier must not be vetoed out of a
         military sense).

    MARGIN / ABSTENTION RULE, stated plainly: there is no numeric confidence threshold to tune,
    because a tuned Lesk threshold was measured to be unreliable on this corpus. The margin is
    STRUCTURAL — two independent signals must AGREE that the pick is unsupported, and the response
    to that agreement is to place NOTHING rather than to place something else. A missing rung is
    recoverable (the async grounding queue is the existing fallback); a wrong rung poisons the
    place-index the deterministic walk depends on.

    Deterministic + OFFLINE (WordNet glosses/relations only) — NO embeddings, NO cosine, NO LLM, NO
    hand-curated sense/domain word list (every class consulted is WordNet's OWN annotation).
    Never raises: any failure → False (no veto = today's behaviour)."""
    wn = _wn()
    if wn is None or chosen is None:
        return False
    try:
        cands = _synsets_for(head_key)
        if len(cands) < 2:
            return False
        mods = tuple(m for m in modifiers if m)[:_MODIFIER_CAP]
        if not mods:
            return False
        # (2) SPECIALIST-DOMAIN presence. The CHEAPEST discriminating signal (one cached pointer
        #     lookup), so it is evaluated FIRST: on the real 197-compound bench set ~185 exit here
        #     without any hypernym-path walk or Lesk work. All the conditions are ANDed, so the
        #     ordering is a pure cost optimisation and never changes the decision.
        dom = _topic_domains(chosen.name())
        if not dom:
            return False
        # (1) MATERIAL divergence — would the sense choice actually change the minted rung-1 parent?
        parents = {_first_parent_name(c) for c in cands}
        if len(parents) < 2:
            return False
        mod_dom: set = set()
        mod_bag: set = set()
        for m in mods:
            mod_bag |= _gloss_tokens(m)
            for ms in _synsets_for(m):
                mod_dom |= _topic_domains(ms.name())
                mod_bag |= _sense_bag(ms.name())
        if dom & mod_dom:
            return False  # the modifier lives in the SAME specialist field → the sense is apt
        if not mod_bag:
            return False  # no lexical evidence at all → keep today's pick (never abstain blind)
        # (3) LESK disagreement — does the modifier's gloss bag rank the chosen sense first?
        excl = {head_key.replace("_", " "), head_key}
        try:
            excl.add(wn.morphy(head_key, "n") or head_key)
        except Exception:  # noqa: BLE001
            pass
        ctx = {t for t in mod_bag if t not in excl}
        if not ctx:
            return False
        scores = {c.name(): len({t for t in _sense_bag(c.name()) if t not in excl} & ctx)
                  for c in cands}
        if scores.get(chosen.name(), 0) >= max(scores.values()):
            return False  # the modifier's own glosses BACK the chosen sense → keep it
        # (4) the modifier must not itself be lexically part of the specialist domain
        for dn in dom:
            if {t for t in _sense_bag(dn) if t not in excl} & ctx:
                return False
        log.info(
            "wordnet_ladder.modifier_sense_veto",
            head=head_key[:40], modifiers=",".join(mods)[:40], sense=chosen.name(),
            domain=",".join(sorted(dom))[:60],
            note="compound modifier contradicts the head's specialist-domain sense — ABSTAIN "
                 "(no hypernym chain minted; the compound→head bridge is kept)",
        )
        return True
    except Exception:  # noqa: BLE001 — WSD is best-effort; a failure NEVER changes today's ladder
        return False


def _rungs_from_synset(term: str, synset, max_rungs: int | None) -> list[tuple[str, str]]:
    """Walk a selected synset → root into ``[(child, parent), …]`` ``subclass_of`` rungs (SPACE-form
    nodes), bounded + generic-root-terminated. The leaf is the input surface; a synonym bridge
    (input → the synset's canonical lemma, e.g. ``physician → doctor``) is emitted when they differ,
    so a synonymously-named type files UNDER the canonical hypernym the walk aggregates on."""
    try:
        paths = synset.hypernym_paths()
    except Exception:  # noqa: BLE001
        return []
    if not paths:
        return []
    # DETERMINISTIC path selection (critical): shortest, lexicographic tie-break (see docstring).
    path = min(paths, key=lambda p: (len(p), tuple(s.name() for s in p)))
    seq: list[str] = [_surface(term)]
    # Walk synset → root, appending each synset's PRIMARY lemma (space-form); stop at a generic root.
    for syn in reversed(path):
        try:
            lemma_raw = syn.lemmas()[0].name().strip().lower()
        except Exception:  # noqa: BLE001
            break
        if not lemma_raw or lemma_raw in _GENERIC_ROOTS:
            break
        lemma = lemma_raw.replace("_", " ")
        if lemma != seq[-1]:
            seq.append(lemma)
    cap = max_rungs if (max_rungs and max_rungs > 0) else _depth_cap()
    rungs = [(seq[i], seq[i + 1]) for i in range(len(seq) - 1)][:cap]
    # Defensive: drop any self-rung or generic-parent rung that slipped through.
    return [(c, p) for (c, p) in rungs
            if c and p and c != p and p.replace(" ", "_") not in _GENERIC_ROOTS]


def _multiword_head_rungs(term: str, gliner_type: str | None, max_rungs: int | None,
                          context_hypernym: str | None) -> list[tuple[str, str]]:
    """GAP B — the full compound has no synset: reduce to its HEAD noun and ladder the head, emitting
    the ``(compound, subclass_of, head)`` bridge first. Returns [] if not a clean endocentric
    compound or the head has no synset (→ caller's async fallback)."""
    head = _head_noun(term)
    if not head:
        return []
    head_syn = _select_synset(head, gliner_type, context_hypernym)
    if head_syn is None:
        return []
    compound = _surface(term)
    head_surface = _surface(head)
    if not compound or compound == head_surface:
        return []
    # MODIFIER-SENSE VETO (flag L4_WORDNET_MODIFIER_WSD, default OFF → no-op, byte-identical).
    # When the compound's MODIFIER contradicts the sense picked for its HEAD ("kiln firing" →
    # firing-AT-AN-ENEMY), ABSTAIN from the head's hypernym CHAIN — but still emit the
    # ``(compound, head)`` bridge, which is SENSE-INDEPENDENT: a kiln firing IS a firing under the
    # right-hand head rule whichever sense of "firing" is meant. That keeps the user's content
    # walkable (we never lose the term) while refusing the wrong-domain rungs that poison L4.
    if _flag_modifier_wsd() and _modifier_contradicts_sense(
            head, tuple(_modifier_tokens(term)), head_syn):
        return [(compound, head_surface)]
    head_rungs = _rungs_from_synset(head, head_syn, max_rungs)
    out = [(compound, head_surface)] + head_rungs
    cap = max_rungs if (max_rungs and max_rungs > 0) else _depth_cap()
    seen: set = set()
    clean: list[tuple[str, str]] = []
    for c, p in out:
        if not c or not p or c == p or p.replace(" ", "_") in _GENERIC_ROOTS:
            continue
        if (c, p) in seen:
            continue
        seen.add((c, p))
        clean.append((c, p))
    # Bounded: the compound→head bridge + up to `cap` head rungs.
    return clean[:cap + 1]


def hypernym_rungs(term: str, gliner_type: str | None = None,
                   max_rungs: int | None = None,
                   context_hypernym: str | None = None) -> list[tuple[str, str]]:
    """Return deterministic ``[(child, parent), …]`` ``subclass_of`` rungs for a common-noun TYPE.

    Deterministic (fixed disambiguation cascade + WordNet's stable shortest ``hypernym_paths``),
    bounded (``max_rungs``, default from ``L4_WORDNET_LADDER_DEPTH``), terminated before a generic
    root. Returns ``[]`` on any miss/error (WordNet lacks the term AND its head, corpus unavailable,
    non-lexical surface) → caller falls back to the async LLM climb. NEVER raises. Pure/offline.

    Args:
      term            : the captured type surface (space-form; the leaf node of the ladder).
      gliner_type     : the entity's GLiNER2 top-type (Person/Object/Animal/…) — steers rule #2.
      max_rungs       : depth cap override (else ``L4_WORDNET_LADDER_DEPTH``).
      context_hypernym: an OPTIONAL co-occurring CATEGORY surface (a Hearst umbrella or query
                        anchor). When given, rule #1 disambiguates ``term``'s sense so its is-a
                        chain passes through the category's synset (GAP A). Default None → today's
                        context-free lexname/MFS selection, byte-for-byte.

    Nodes are emitted SPACE-form so they are the SAME entities the count walk resolves. When the
    full ``term`` has no synset but is a clean nominal compound, its HEAD noun is laddered with a
    ``(term, head)`` bridge (GAP B).
    """
    key = _clean_common_noun(term)
    if key is None:
        return []
    synset = _select_synset(key, gliner_type, context_hypernym)
    if synset is None:
        # GAP B: no synset for the full type → head-noun reduction (clean compounds only).
        return _multiword_head_rungs(term, gliner_type, max_rungs, context_hypernym)
    return _rungs_from_synset(term, synset, max_rungs)
