"""Deterministic LINGUISTIC LAYER — a single spaCy dependency parse, many grammar rules.

WHAT THIS IS (and is NOT)
=========================
This is a NEW, purely ADDITIVE deterministic helper that AUGMENTS extraction/grounding with
real grammar (POS + dependency labels) instead of the hand-rolled regexes those seams faked.
It is the *construction* layer the feeling/temporal spec keeps reaching for (see
``DEV/EVAL-spacy-linguistic-layer.md``, ``DEV/DESIGN-feeling-and-temporal-capture.md``).

It uses **spaCy ``en_core_web_sm``** — a 15 MB CNN tagger/parser, NO torch, NO GPU, loaded
ONCE per process (module-level lazy singleton), ~0.5 s load, ~3–8 ms/parse on CPU. spaCy is a
SEPARATE, task-specific model: it does NOT reuse and is NOT GLiNER2 or the qwen LLM, and it is
NEVER fed into GLiNER2's labels. Grammar ≠ entity-typing → Pitfall 11 is untouched.

HARD CONSTRAINTS (this module exists to obey them)
==================================================
- **Subject-agnostic.** Every rule is driven off POS tags / dependency labels — language
  primitives. There is NO domain/keyword word-list and NO regex zoo here. The only closed set
  this module ever consults is the universal POS function-word tag set
  (``{ADP, DET, PART, AUX, CCONJ, SCONJ}``) — grammar, not ontology.
- **Deterministic.** A parse of the same text always yields the same analysis. No LLM, no DB,
  no network, no randomness.
- **Kill-switch, default ON** (``LINGUISTIC_LAYER``). When the flag is OFF, OR spaCy is not
  importable, OR the model is not installed, OR any parse raises — every public function returns
  a benign "no analysis" result so the CALLER falls back to its existing bespoke path. Today's
  behavior is therefore EXACTLY preserved on a missing bake / flag flip / parse failure.
- **Fail-safe, never crash ingest.** No public function in this module raises on bad input.
- **Additive.** This module makes NO decisions about intent routing, negation/correction
  classification, or entity typing — those tiers are sacred and untouched. It only hands the
  caller a deterministic grammatical first-cut for EXTRACTION / GROUNDING.

PUBLIC API (one parse, many rules)
==================================
- ``linguistics_available()``                  → bool (flag ON and model loadable)
- ``analyze_copula(text)``                      → CopulaAnalysis | None  (self-predication first-cut)
- ``analyze_naming(text)``                      → NamingAnalysis | None  ("<noun> named/called <Name>")
- ``analyze_svo_relations(text)``               → list[SVORelation]      (the MERGE-brain SVO backbone)
- ``is_naming_predicate(predicate)``            → bool | None            (naming-verb class test)
- ``possessive_head(text, possessor)``          → str | None             (poss dep → head noun)
- ``is_function_word_predicate(predicate)``     → bool | None            (POS function-word test)
- ``segment_clauses(text)``                     → list[str]              (doc.sents / clause heads)
- ``is_interrogative_clause(text)``             → bool | None            (question-vs-statement, per clause)
- ``extract_event_date(text, reference)``       → (iso|None, gran|None)  (deterministic event-date)
- ``extract_event_date_and_residue(text, ref)`` → (iso|None, gran|None, residue)  (peel date OUT, return residue)

The bespoke implementations these generalize (``possessive_head.py``, ``relation_fit.py`` RUNG 1,
the ``_SELF_PREDICATION_COPULA`` / ``_ground_self_predication`` regex in ``main.py``) remain in
place as the flag-OFF / spaCy-unavailable fallback path. This module never removes them.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from datetime import timedelta as _timedelta

import structlog

log = structlog.get_logger(__name__)

# ── Kill-switch (default ON) ───────────────────────────────────────────────────────
# A single env flip (``LINGUISTIC_LAYER=0``) reverts EVERY caller to its bespoke path with no
# per-call-site wiring change. Read once at import; the caller's fallback is today's behavior.
LINGUISTIC_LAYER: bool = os.environ.get(
    "LINGUISTIC_LAYER", "true"
).strip().lower() not in ("0", "false", "no")

# Model name is PURE CONFIG — no hardcoded model literal in code. Resolved from the env
# (``SPACY_MODEL``, same shape as ``WGM_LLM_MODEL``; legacy ``LINGUISTIC_SPACY_MODEL`` honored as a
# fallback). The shipped default lives in ``.env.example`` (``en_core_web_md`` — CPU/torch-free
# upgrade from sm), NOT in code. If unset/empty here, the model is simply un-resolvable and the
# layer NO-OPS (``_get_nlp`` returns ``None``) — same fail-safe as a missing bake, never a crash.
_SPACY_MODEL = (
    os.environ.get("SPACY_MODEL")
    or os.environ.get("LINGUISTIC_SPACY_MODEL")
    or ""
).strip()

# FAIL LOUD, not silent. The spaCy layer is LOAD-BEARING (the whole spine deriver rides it). If the
# layer is enabled but no model resolves (SPACY_MODEL unset/empty — e.g. a multi-stage Dockerfile
# that drops the ENV before runtime), silently no-opping it COLLAPSES capture (a 9/10→2/10
# regression). Scream at import so a config/plumbing break is obvious, never a quiet bad-data
# continuation. The layer still degrades to no-op (no crash); the CRIT makes it visible.
if LINGUISTIC_LAYER and not _SPACY_MODEL:
    log.critical(
        "linguistic_layer.model_unresolved",
        note="LINGUISTIC_LAYER is ON but SPACY_MODEL is unset/empty — the spine deriver will NO-OP "
             "and capture will collapse. Set SPACY_MODEL (config layer: .env / compose / Dockerfile "
             "ARG), or set LINGUISTIC_LAYER=0 to intentionally disable the layer.",
    )

# ── Temporal date-extraction kill-switch (default ON, independent of LINGUISTIC_LAYER) ──
# Guards ONLY the deterministic event-date path (``extract_event_date``). OFF (or any failure)
# → the caller's existing strict-ISO + hand-rolled relative-date fallback (today's behavior).
TEMPORAL_DATE_LAYER: bool = os.environ.get(
    "TEMPORAL_DATE_LAYER", "true"
).strip().lower() not in ("0", "false", "no")

# ── Spine naming-chain kill-switch (default OFF — DORMANT until measured) ─────────────────────────
# Gates BOTH halves of the role↔name unification on the deterministic spine deriver:
#   • Part 1 (caller seam): the named-instance / naming-verb chain ("X named/called Y") — wired on the
#     spine by ``_harvest_via_sentence_pipeline``, NOT here (the multi-edge instance_of/subclass_of/
#     possession shape lives at the caller, mirroring _detect_named_instance_states).
#   • Part 2 (this module): the copula-appositive / genitive ROLE↔NAME collapse chains
#     (``_chain_copula_name``; the role-alias leg of ``_chain_genitive_name``) — so "My sister is
#     Sarah" / "My mother's name is Carol" bind the kin rel to the NAMED person and register the ROLE
#     (sister/mother) as an alias/role-slot of that person, never a parallel role entity.
# DEFAULT OFF so the commit is dormant and the temporal first-10 path is byte-for-byte unchanged
# until validated ON. Fail-safe: flag OFF or any failure → today's behavior exactly.
SPINE_NAMING_CHAIN: bool = os.environ.get(
    "SPINE_NAMING_CHAIN", "false"
).strip().lower() in ("1", "true", "yes")

# COLLECTIVE MEMBER-LIST reconciliation (default ON). A "<subj> <verb> [<count>] <HEAD>: M1, M2, …"
# enumeration ("we have three kids: Mia, Theo, Leo", "my team has three engineers: Sarah, Tom,
# Priya", "we run three servers: Apollo, Vault, Echo") was mangled by the generic chains: the collective
# HEAD became a type the members were ``instance_of`` / the user ``owns``, only the FIRST member got
# typed, and members lost their proper membership/kinship edge. This post-chain pass reconciles the
# construction deterministically: route by the HEAD noun's resolved cue/type (kinship head → the kin
# rel + direction + intrinsic gender; non-kin group head → instance_of the SINGULAR type + the
# membership/activity relation to the governor), distributing to EVERY member and dropping the junk.
# OFF → byte-identical to today's chain output. Subject-agnostic, cue/morphology/parse-driven.
COLLECTIVE_MEMBER_LIST: bool = os.environ.get(
    "COLLECTIVE_MEMBER_LIST", "true"
).strip().lower() in ("1", "true", "yes")

# DETERMINISTIC ENUMERATION PRE-SPLIT (the atomizer reliability net). The LLM atomizer's split of a
# colon-introduced named list ("My team has three engineers: Sarah who is 35, Tom who is a backend
# dev, and a designer named Priya") VARIES run-to-run — sometimes it folds "a designer named Priya"
# back into the membership line (so the downstream collective walk, which only routes BARE PROPN
# conjuncts, silently drops Priya's member_of), sometimes it drops a per-member attribute, and on a
# REFRAME timeout the whole dense sentence flows un-split (cross-member smear). ``split_enumeration``
# normalizes the structurally-obvious colon list DETERMINISTICALLY so the spine sees the SAME clean
# atom set every time, regardless of LLM variance. Subject-agnostic, structural (colon + comma/"and"
# split + the relative-pronoun grammatical class + the structural "<np> <acl-verb> <PROPN>" naming
# shape); fabrication-safe (every emitted atom's content tokens ⊆ source). OFF → today's behavior.
ENUM_PRESPLIT: bool = os.environ.get(
    "ENUM_PRESPLIT", "true"
).strip().lower() in ("1", "true", "yes")

# Universal POS tags that mark a token as a FUNCTION word (grammar, not a relation). This is the
# spaCy/Universal-Dependencies POS scheme — a closed LANGUAGE PRIMITIVE set, NOT a domain list.
#   ADP   adposition / preposition (in, at, with)      PART  particle (to, 's, not)
#   DET   determiner (the, a, this)                     AUX   auxiliary / copula (is, be, has)
#   CCONJ coordinating conjunction (and, or, but)        SCONJ subordinating conj (when, because)
_FUNCTION_POS: frozenset[str] = frozenset({"ADP", "DET", "PART", "AUX", "CCONJ", "SCONJ"})

# ── Lazy singleton loader (load ONCE per process) ──────────────────────────────────
_nlp = None                      # the loaded spaCy Language, or None
_load_attempted = False          # so a failed load is not retried on every call (fail-safe, cheap)
_load_lock = threading.Lock()    # guard concurrent first-load (FastAPI is multi-threaded)

# IDENTIFIER-shaped token: a letter+digit alphanumeric mix joined by an internal ``-_.:/@`` separator
# (CVE-2024-9999, v1.2.3, x86-64, bug-4471, log4j@2.17, COVID-19). Requires BOTH a letter AND a digit
# so normal hyphenated words ("well-known") and pure-numeric/IP spans (no letter) are NEVER caught.
# Shape-only, subject-agnostic — NO domain/token list.
_IDENTIFIER_TOKEN_RE = re.compile(
    r"^(?=[A-Za-z0-9._:/@-]*[A-Za-z])(?=[A-Za-z0-9._:/@-]*[0-9])"
    r"[A-Za-z0-9]+(?:[-_.:/@][A-Za-z0-9]+)+$"
)


def _install_identifier_tokenizer(nlp) -> None:
    """Keep IDENTIFIER-shaped tokens WHOLE through tokenization (subject-agnostic, shape-based).

    GROUNDING: spaCy Tokenizer `token_match` — "matches strings that should never be split, overriding
    infix rules" (spaCy Tokenizer API). See DEV/DESIGN-ingest-hardening-grounding.md.

    spaCy's statistical parser DERAILS on out-of-vocabulary identifier tokens its tokenizer SPLITS on
    internal separators — "CVE-2024-9999" → ["CVE-2024","-","9999"], so the bare number becomes the
    subject and the real verb demotes to a noun (the parse ROOT collapses). We extend the tokenizer's
    ``token_match`` (the SAME hook spaCy uses to keep URLs whole) so any ``_IDENTIFIER_TOKEN_RE`` span
    stays a SINGLE token → parses as one PROPN → the identifier survives WHOLE as the entity and the
    clause structure holds. Idempotent, fail-safe: any error leaves the tokenizer untouched."""
    try:
        _base = nlp.tokenizer.token_match

        def _match(text, _base=_base):
            if _IDENTIFIER_TOKEN_RE.match(text):
                return True
            return _base(text) if _base else None

        nlp.tokenizer.token_match = _match
    except Exception as e:  # noqa: BLE001 — never break the parse over a tokenizer tweak
        log.warning("linguistics.identifier_tokenizer_install_failed", error=str(e)[:120])


def _get_nlp():
    """Return the loaded spaCy pipeline, or ``None`` if unavailable (flag off / no spaCy / no model).

    Loads once, thread-safely, and CACHES the failure: a missing bake degrades to ``None`` on the
    first attempt and never re-tries (no per-turn import cost, no log spam). Disables the heavy NER
    component we do not need — only the tagger/parser are required for POS + dependency labels.
    """
    global _nlp, _load_attempted
    if not LINGUISTIC_LAYER:
        return None
    if not _SPACY_MODEL:
        # No model configured (env unset/empty) → no-op layer, same fail-safe as a missing bake.
        return None
    if _nlp is not None:
        return _nlp
    if _load_attempted:
        return None
    with _load_lock:
        if _nlp is not None:
            return _nlp
        if _load_attempted:
            return None
        _load_attempted = True
        try:
            import spacy  # deferred: a missing spaCy is a graceful no-op, not an import error
        except Exception as e:  # noqa: BLE001 — fail-safe: spaCy absent → no-op layer
            log.warning("linguistics.spacy_import_failed", error=str(e)[:160])
            return None
        try:
            # NER is GLiNER2's job and the slowest component — disable it (faster parse, smaller
            # footprint). We only need tagger + parser for POS + dependency labels.
            _nlp = spacy.load(_SPACY_MODEL, disable=["ner"])
            _install_identifier_tokenizer(_nlp)  # keep CVE ids / version strings / x86-64 whole
            log.info("linguistics.model_loaded", model=_SPACY_MODEL)
        except Exception as e:  # noqa: BLE001 — model not baked into the image → no-op layer
            log.warning("linguistics.model_load_failed", model=_SPACY_MODEL, error=str(e)[:160])
            _nlp = None
    return _nlp


def linguistics_available() -> bool:
    """True iff the kill-switch is ON and the spaCy model is loadable. Cheap after first call."""
    return _get_nlp() is not None


def _parse(text: str):
    """Parse ``text`` once. Returns the spaCy ``Doc`` or ``None`` on any failure (fail-safe)."""
    if not text or not text.strip():
        return None
    nlp = _get_nlp()
    if nlp is None:
        return None
    try:
        return nlp(text)
    except Exception as e:  # noqa: BLE001 — a parse failure must never crash ingest
        log.warning("linguistics.parse_failed", error=str(e)[:160])
        return None


# ── gap-1 PHASE 2: the minted-rel carrier (``Doc._.rel``) ──────────────────────────
# spaCy's idiomatic transport for an external relation classifier (the official
# ``rel_component`` representation): a custom ``Doc`` extension keyed by the (subject
# token index, object token index) PAIR → the authoritative rel_type the GLiNER2
# relation pass minted for that span. The deriver reads it in ``_emit`` and CONVERGES —
# GLiNER2's minted rel WINS, the SVO verb-lemma fills only the gap where it has nothing
# (mirrors the gap-1 ``token.ent_type_`` type convergence, for the PREDICATE this time).
#
# Authority lives in the DB-grown ontology; this extension is only the per-turn transport
# carrying the assignment onto the Doc the deriver parses (SPEC §9, §2.1). Registered ONCE
# (guarded against double-registration — FastAPI re-imports / test re-imports are safe).
_REL_EXTENSION_NAME = "rel"

# The canonical, subject-agnostic STATE predicate an intransitive "something happened to a
# thing" clause routes through (DESIGN-state-typing.md owner decision). It is a RELATIONAL rel
# (tail_types={Concept}, storage_target='facts'), re-minted by migration 111 as the structural
# TWIN of ``feels`` (migration 091/093): the state value ("break") is a TYPED, REUSABLE
# hierarchy NODE that resolves to a UUID and self-builds an is-a ladder via the async grounder,
# NOT a freeform scalar string. A GPS, a server and a leg all converge to the ONE ``break`` node
# by convergence-by-identity (the spaCy lemma is normalized byte-identically at emit). The THING
# (the subject) still grounds normally via its own SVO/possessive/genitive chains. One named
# module pointer at a canonical ontology-defined predicate — mirrors main._CLASSIFICATION_RETYPE_REL;
# NOT a hardcoded dispatch over verb surfaces. The predicate name lives in the rel_types table;
# this is the single in-code reference to it in the linguistic layer.
_STATE_REL = "has_state"

# SOCIAL co-participant TYPE labels — the entity categories a comitative "with" introduces (a person
# you meet / an organization you call). These are UNIVERSAL ONTOLOGY/NER PRIMITIVES (the GLiNER2
# zero-shot type names ∪ the spaCy NER labels), NOT a domain word-list — the same primitives the code
# already tests when it checks ``_ent.label_ == "PERSON"``. Used by the device-issue degrade gate to
# tell a comitative co-participant ("had lunch WITH <person/org>") from an affected THING ("had an
# issue WITH <object>"): a problem is ABOUT an inanimate thing, an activity is WITH a social party.
# Lowercased compare. Anything typed and NOT in this set is treated as a (non-social) THING.
_SOCIAL_AFFECTED_LABELS: frozenset[str] = frozenset({
    "person", "org", "organization", "norp", "group", "company", "gpe", "fac",
})


def _ensure_rel_extension() -> bool:
    """Register the ``Doc._.rel`` extension once (default ``None``). Returns True iff the
    extension is available after the call. Fail-safe: spaCy absent / registration error →
    False (caller skips minting; the deriver falls to pure SVO — today's behavior)."""
    try:
        from spacy.tokens import Doc  # deferred: spaCy absent → no extension, pure SVO
    except Exception:  # noqa: BLE001
        return False
    try:
        if not Doc.has_extension(_REL_EXTENSION_NAME):
            Doc.set_extension(_REL_EXTENSION_NAME, default=None)
        return True
    except Exception as e:  # noqa: BLE001 — registration must never crash the layer
        log.warning("linguistics.rel_extension_register_failed", error=str(e)[:160])
        return False


def set_minted_rel(doc, subj_token_i: int, obj_token_i: int, rel_type: str) -> bool:
    """Record an AUTHORITATIVE (GLiNER2-minted) rel_type for the (subject token, object
    token) pair on ``doc._.rel`` (a dict keyed by the integer token-index pair). The
    deriver's ``_emit`` reads this and converges (minted WINS over the SVO verb lemma).

    Deterministic: the key is the exact integer index pair, the value the concise rel_type
    name. Fail-safe: extension unavailable / bad indices / write error → no-op, returns
    False (the Doc simply carries no minted rel for the pair → SVO gap-fill stands)."""
    rt = (rel_type or "").strip().lower()
    if not rt or doc is None or subj_token_i is None or obj_token_i is None:
        return False
    if not _ensure_rel_extension():
        return False
    try:
        store = doc._.get(_REL_EXTENSION_NAME)
        if store is None:
            store = {}
            doc._.set(_REL_EXTENSION_NAME, store)
        # First writer wins for a pair (convergence-on-overlap, §10.7): never overwrite an
        # already-minted authoritative rel with a later, possibly-weaker one.
        store.setdefault((int(subj_token_i), int(obj_token_i)), rt)
        return True
    except Exception as e:  # noqa: BLE001 — minting must never crash the layer
        log.warning("linguistics.set_minted_rel_failed", error=str(e)[:160])
        return False


def _minted_rel_for_pair(doc, subj_tok, obj_tok) -> str | None:
    """The authoritative GLiNER2-minted rel_type for this (subject token, object token)
    pair, or ``None`` when none was minted (→ the deriver's SVO predicate fills the gap).
    Deterministic exact integer-index-pair lookup; never fuzzy. Fail-safe → None."""
    if doc is None or subj_tok is None or obj_tok is None:
        return None
    try:
        if not doc.has_extension(_REL_EXTENSION_NAME):
            return None
        store = doc._.get(_REL_EXTENSION_NAME)
        if not store:
            return None
        return store.get((int(subj_tok.i), int(obj_tok.i)))
    except Exception:  # noqa: BLE001 — lookup must never crash the deriver
        return None


# ── COPULA ANALYSIS — the self-predication first-cut ───────────────────────────────
# Complement-POS → relation first-cut. This is the FREE win the eval names: the copula
# complement's POS disambiguates self-predication deterministically, retiring both the
# ``\bi'm (\w+)`` name regex AND the per-turn ``INTENT_PRECLASSIFY`` LLM call.
#   ADJ   → feeling/affective state ("worried", "anxious")          → rel "feels"
#   NOUN  → role / occupation        ("teacher", "engineer")        → rel "occupation"
#   PROPN → name / proper noun        ("Alex", "Ace")              → rel "also_known_as"
# DETERMINER OVERRIDE: a complement with a ``det`` child (article "a"/"an"/"the") is a common-noun
# role regardless of POS — "I am a Systems Analyst" → occupation even when the sm model title-case-
# tags "Analyst" as PROPN. This is a grammatical primitive (the det dependency), casing-robust.
# A VERB complement (participle: "I am exhausted" → "exhaust"/VERB) is grammatically ambiguous as
# a first-cut (state vs action) so we report it as a copula WITHOUT a relation guess and let the
# caller's residue path (the LLM grounding router) decide — additive, not a forced bad guess.
_COMPLEMENT_POS_TO_REL: dict[str, str] = {
    "ADJ": "feels",
    "NOUN": "occupation",
    "PROPN": "also_known_as",
}


@dataclass(frozen=True)
class CopulaAnalysis:
    """A deterministic reading of a copular self-predication ("I am X" / "I'm X" / "my name is X").

    - ``subject``    : the nominal subject lemma/text ("i", "name").
    - ``subject_is_self`` : True ONLY when the nsubj is a genuine 1st-person *personal* pronoun
                            ("I"/"we" — Person=1 ∧ PronType=Prs ∧ no Poss). A possessive subject
                            ("my favorite color …") is NOT self (Poss=Yes) → preference residue.
    - ``complement`` : the copula complement, lowercased ("worried", "teacher", "alex").
    - ``complement_pos`` : the complement's universal POS ("ADJ"/"NOUN"/"PROPN"/"VERB").
    - ``relation``   : the first-cut rel_type from complement POS, or ``None`` when ambiguous
                       (VERB participle) → caller routes the residue (e.g. LLM grounding).
    - ``negated``    : True when a ``neg`` dependency hangs off the copula head ("I am not worried").
    """
    subject: str
    subject_is_self: bool
    complement: str
    complement_pos: str
    relation: str | None
    negated: bool


def _is_first_person_personal_pronoun(tok) -> bool:
    """True iff ``tok`` is a GENUINE 1st-person *personal* pronoun ("I"/"we") — the self-referent.

    Decided from morphology, NOT a token/lemma word-list (subject-agnostic, language-general):
      - ``Person == ["1"]``                     — 1st person
      - ``"Prs" in PronType``                   — a personal pronoun (not demonstrative/relative)
      - ``"Yes" not in Poss``                   — NOT possessive

    The Poss exclusion is load-bearing: possessives ("my"/"our") carry ``Poss=Yes`` precisely so
    they are NOT the self-referent — "my favorite color is blue" predicates about *color*, not the
    speaker. Only "I"/"we" (Person=1, PronType=Prs, no Poss) are the self subject.
    """
    try:
        morph = tok.morph
        return (
            morph.get("Person") == ["1"]
            and "Prs" in morph.get("PronType")
            and "Yes" not in morph.get("Poss")
        )
    except Exception:  # noqa: BLE001 — morphology probe must never crash extraction
        return False


def _is_third_person_pronoun(tok) -> bool:
    """True iff ``tok`` is a 3rd-person *personal/possessive* pronoun (he/she/it/they/his/her/its/their/him/them).

    Decided from morphology, NOT a token/lemma word-list (subject-agnostic, language-general):
      - ``Person == ["3"]``    — 3rd person
      - ``"Prs" in PronType``  — a personal pronoun (covers possessive ``Poss=Yes`` and plain)

    A 3rd-person pronoun is a referring expression with no name of its own — in a correction
    ("Actually *his* name is Max, not Rex") it MUST be coref-resolved to its antecedent entity,
    never matched literally as if "his" were an entity surface.
    """
    try:
        morph = tok.morph
        return morph.get("Person") == ["3"] and "Prs" in morph.get("PronType")
    except Exception:  # noqa: BLE001 — morphology probe must never crash extraction
        return False


def is_third_person_pronoun(text: str) -> bool:
    """True iff ``text`` is exactly a single 3rd-person personal/possessive pronoun.

    Grammatical (spaCy morphology), not a word-list. Used by the correction path to detect a
    pronoun subject ("his"/"its"/"their"/"her"…) that the extractor could not name and that
    must be coref-resolved to an antecedent entity. Fail-safe: any parse miss / multi-token
    input / failure → False (the caller then keeps its normal resolution ladder).
    """
    if not text or not text.strip():
        return False
    doc = _parse(text.strip())
    if doc is None:
        return False
    toks = [t for t in doc if not t.is_space]
    if len(toks) != 1:
        return False
    return _is_third_person_pronoun(toks[0])


def _adj_has_numeric_measure(adj_tok) -> bool:
    """True when an ADJ predicate is measured by a NUMBER — the age/height idiom ("34 years old",
    "6 feet tall"), owned by the copula-measure chain, NOT a feeling. The number sits on a unit
    noun modifying the ADJ (npadvmod/nmod ← nummod NUM) or modifies the ADJ directly. A bare
    feeling ("I am sad") has no such child; a number that measures NOTHING on the adjective ("I am
    45, sad" — the 45 is a separate ``attr`` of the copula, not a modifier of "sad") does NOT trip
    it, so a real feeling co-occurring with a number is preserved. Grammar-driven, subject-agnostic,
    NO unit/emotion word list. Fail-safe → False."""
    try:
        if adj_tok is None or adj_tok.pos_ != "ADJ":
            return False
        for ch in adj_tok.children:
            # a unit noun / adverbial measure modifying the ADJ, itself carrying a NUMBER (or a bare
            # numeric modifier directly on the ADJ).
            if ch.dep_ in ("npadvmod", "nmod", "obl", "advmod", "quantmod", "nummod"):
                if ch.pos_ == "NUM" or ch.like_num:
                    return True
                for g in ch.children:
                    if g.dep_ == "nummod" or g.pos_ == "NUM" or g.like_num:
                        return True
    except Exception:  # noqa: BLE001 — fail-safe
        return False
    return False


# Topic-marking prepositions that follow an AFFECTIVE predicate adjective — "worried/excited/nervous
# ABOUT X" names the emotion's TOPIC, not a relational complement. A closed-class function-word set (a
# grammatical primitive like the copula "be"), NOT a domain/emotion word list. Kept minimal: "about"
# is the dominant topic-of-state marker; genuine relational complements use "to"/"of"/"from"/…
_ADJ_TOPIC_PREPS: frozenset[str] = frozenset({"about"})


def _adj_prep_objects(adj_tok) -> list:
    """The prepositional-object token(s) an ADJ predicate governs — "allergic TO penicillin",
    "afraid OF spiders", "married TO Sam" → the ``pobj`` head(s) under the ADJ's ``prep`` child(ren),
    with the preposition surface. Returns a list of ``(prep_surface, pobj_tok)`` (usually one; more
    for a coordinated object "allergic to penicillin and sulfa").

    THE CLASS RULE (subject-agnostic, dependency-driven — NO domain/adjective word list): a predicate
    adjective that takes a prepositional COMPLEMENT ("<adj> <prep> <object>") is a RELATION carrying an
    object, NOT a bare affective state — so the feeling/affect seam must NOT claim it; the object is
    captured on the ``<adj>_<prep>`` relation (grown via the ontology growth engine). Grammatical
    primitive only (the ``prep``→``pobj`` dependency chain), so it holds in any domain.

    THE ONE EXCLUSION — the TOPIC-marking preposition "about" (a closed-class function word, the same
    kind of grammatical primitive as the copula "be"): "I am worried ABOUT the migration" / "excited
    ABOUT the trip" is a FEELING whose "about"-phrase is the emotion's TOPIC, NOT a relational
    complement — so it stays with the affect seam ("feels worried"). "about" is the canonical
    topic-of-state marker in English; a genuine relational complement uses "to"/"of"/"from"/… ("allergic
    TO", "afraid OF"). Excluding this one topic preposition is grammatical, not a domain word list.
    Fail-safe → []."""
    out: list = []
    try:
        if adj_tok is None or adj_tok.pos_ != "ADJ":
            return out
        for ch in adj_tok.children:
            if ch.dep_ != "prep" or ch.pos_ != "ADP":
                continue
            prep_surf = (ch.text or "").strip().lower()
            if prep_surf in _ADJ_TOPIC_PREPS:
                continue  # "worried/excited about X" → feeling's TOPIC, not a relational object
            for g in ch.children:
                if g.dep_ == "pobj" and g.pos_ in ("NOUN", "PROPN", "PRON"):
                    # a wh/interrogative pobj ("allergic to what?") is not a value — skip.
                    try:
                        if "Int" in g.morph.get("PronType") or g.tag_ in ("WP", "WP$", "WDT", "WRB"):
                            continue
                    except Exception:  # noqa: BLE001
                        pass
                    out.append((prep_surf, g))
                    # coordinated objects ("penicillin and sulfa") — the conj siblings of the pobj.
                    for gg in g.children:
                        if gg.dep_ == "conj" and gg.pos_ in ("NOUN", "PROPN"):
                            out.append((prep_surf, gg))
    except Exception:  # noqa: BLE001 — fail-safe
        return out
    return out


def _adj_has_prep_object(adj_tok) -> bool:
    """True when an ADJ predicate governs a prepositional object ("allergic to X", "afraid of X").
    See ``_adj_prep_objects`` — such a clause is a RELATION with an object, NOT a bare feeling, so the
    affect/feeling seam must decline it. Grammar-driven, subject-agnostic. Fail-safe → False."""
    return bool(_adj_prep_objects(adj_tok))


def analyze_copula(text: str):
    r"""Deterministic first-cut for a copular self-predication. Returns ``CopulaAnalysis`` or ``None``.

    THE RULE (subject-agnostic, dependency-driven — replaces the ``\bi'm (\w+)`` regex):
      Find a copular clause: a subject token (``nsubj``/``nsubjpass``) whose head is the copula
      ``be`` (AUX, lemma "be"), with a complement attached as ``attr`` (nominal: NOUN/PROPN) or
      ``acomp`` (adjectival: ADJ). spaCy structures "I'm worried" / "I am a teacher" exactly this
      way. The complement POS yields the relation first-cut
      (ADJ→feels, NOUN→occupation, PROPN→also_known_as); a VERB participle ("I am exhausted")
      is reported WITHOUT a relation (ambiguous → caller's residue path decides).

      ``subject_is_self`` is set ONLY for a genuine 1st-person personal-pronoun subject ("I"/"we");
      a possessive subject ("my favorite color is blue") is reported with ``subject_is_self=False``
      so the caller routes it to the preference seam, NOT the self-predication path.

    Negation is read deterministically from a ``neg`` dependency on the copula head — no regex
    window. Returns ``None`` when there is no copular self-predication, or on any failure.

    This makes NO intent-routing or entity-typing decision. It hands the caller a grammatical
    first-cut; grounding / what-is taxonomy placement stays the caller's (LLM) residue job.
    """
    doc = _parse(text)
    if doc is None:
        return None
    try:
        for tok in doc:
            # Subject of a clause whose head is the copula "be".
            if tok.dep_ not in ("nsubj", "nsubjpass"):
                continue
            head = tok.head
            # The copula clause head is "be" (AUX). Two shapes spaCy produces:
            #   "I am worried"  → head is the AUX "am" (ROOT, lemma be); complement is its child.
            #   "I am exhausted"→ head is the participle VERB (ROOT); "am" is auxpass; the
            #                     complement IS the head (participle). Handle both.
            comp = None
            if head.lemma_ == "be" and head.pos_ == "AUX":
                # complement is attr/acomp child of the copula
                for child in head.children:
                    if child.dep_ in ("attr", "acomp"):
                        comp = child
                        break
            elif any(c.dep_ in ("cop", "aux", "auxpass") and c.lemma_ == "be" for c in head.children):
                # participle/predicate-adjective head with a "be" copula/aux child → head is comp
                comp = head
            if comp is None:
                continue

            subject = (tok.lemma_ or tok.text or "").strip().lower()
            # SELF-DETECTION IS GRAMMATICAL, NOT A WORD-LIST. ``subject_is_self`` is True ONLY when
            # the nsubj token is itself a genuine 1st-person PERSONAL pronoun ("I"/"we") — decided
            # from morphology (Person=1 ∧ PronType=Prs ∧ no Poss). A possessive-1st-person subject
            # ("my favorite color is blue") is DELIBERATELY NOT self: "my" carries Poss=Yes so the
            # clause predicates about the possessed noun ("color"), not the speaker — that residue
            # is the preference seam's job (ingest), not a self-predication.
            subj_self = _is_first_person_personal_pronoun(tok)

            negated = any(c.dep_ == "neg" for c in head.children) or any(
                c.dep_ == "neg" for c in comp.children
            )
            complement = (comp.text or "").strip().lower()
            # FULL-NP NOMINAL COMPLEMENT (premodifier fix): "I am a principal network architect"
            # parses architect(NOUN, attr) ← amod(principal) + compound(network); taking only
            # ``comp.text`` truncated the occupation to "architect". Build the complement from the
            # head's NP span (``_np_phrase``: head + left compound/amod modifiers, det/poss
            # excluded — the SAME NP-construction rule the deriver chains use). SCOPED to NOMINAL
            # complements (NOUN/PROPN) ONLY: an ADJ/VERB complement stays the bare head so the
            # copula-feeling capture ("I am excited" → feels excited) is byte-identical. The UD
            # copula analysis treats the NONVERBAL PREDICATE (the full predicate-nominal NP) as
            # the clause's predicate — the modifiers are part of it, not discardable
            # (universaldependencies.org/u/dep/cop.html; spaCy attr = predicate "attribute").
            if comp.pos_ in ("NOUN", "PROPN"):
                try:
                    _np = (_np_phrase(comp) or "").strip().lower()
                    if _np:
                        complement = _np
                except Exception:  # noqa: BLE001 — fail-safe: NP-build miss → bare head (today's value)
                    pass
            if not complement:
                continue
            # A QUESTION ("what am I?", "who are you?") is not a value statement — skip an
            # interrogative complement (grammatical: PronType=Int / wh-tags, not a word list).
            if "Int" in comp.morph.get("PronType") or comp.tag_ in ("WP", "WP$", "WDT", "WRB"):
                continue
            # MEASURED-ADJECTIVE GUARD: "I am 34 years old" / "I am 6 feet tall" predicates a
            # MEASUREMENT (age/height), not a feeling — the ADJ is modified by a numeric measure.
            # Decline the self-predication so the copula-measure chain owns it as a scalar. Fires
            # ONLY when the number measures the adjective itself; a bare feeling ("I am sad") or a
            # separate co-occurring number ("I am 45, sad") is untouched. Subject-agnostic, grammar.
            if comp.pos_ == "ADJ" and _adj_has_numeric_measure(comp):
                continue
            # RELATIONAL-PREDICATE GUARD (data-loss fix): "I am allergic TO penicillin" / "I am afraid
            # OF spiders" is a predicate adjective carrying a PREPOSITIONAL OBJECT — a RELATION with an
            # object, NOT a bare feeling. Reading it as ``feels`` both mis-types it AND drops the object
            # (penicillin). DECLINE the self-predication here so the copula-feeling capture never claims
            # it; the object is captured on the ``<adj>_<prep>`` relation by the relational-predicate
            # seam (analyze_copula_relational_predicate). Grammar-driven (prep→pobj), subject-agnostic,
            # NO adjective/domain word list — mirrors the measured-adjective guard directly above.
            if comp.pos_ == "ADJ" and _adj_has_prep_object(comp):
                continue
            rel = _COMPLEMENT_POS_TO_REL.get(comp.pos_)  # None for VERB / other → ambiguous residue
            # DETERMINER OVERRIDE (casing-robust): a complement introduced by an article
            # ("a"/"an"/"the" — a ``det`` child) is a COMMON NOUN describing a role, NEVER a proper
            # name — "I am a Systems Analyst" is an occupation even though the sm model tags the
            # title-cased head as PROPN. The ``det`` dependency is a grammatical primitive (not a
            # casing/word-list rule): if present, route the nominal complement to "occupation".
            if comp.pos_ in ("NOUN", "PROPN") and any(c.dep_ == "det" for c in comp.children):
                rel = "occupation"
            return CopulaAnalysis(
                subject=subject,
                subject_is_self=subj_self,
                complement=complement,
                complement_pos=comp.pos_,
                relation=rel,
                negated=negated,
            )
    except Exception as e:  # noqa: BLE001 — fail-safe
        log.warning("linguistics.analyze_copula_failed", error=str(e)[:160])
        return None
    return None


def analyze_copula_affect_complements(text: str) -> list[str]:
    r"""Enumerate ALL coordinated affective complements of a 1st-person copula.

    "I was busy, overwhelmed, underappreciated, and exhausted." predicates FOUR affective states of
    the speaker, but ``analyze_copula`` returns only the FIRST complement ("busy"), so the tail
    states drop. This walks the COORDINATION off the head complement and returns every coordinated
    complement surface, lowercased, in source order — one ``feels`` edge each at the caller.

    THE RULE (subject-agnostic, dependency-driven — NO emotion word list):
      • The clause is a copula ``be`` with a 1st-person personal-pronoun subject (``subject_is_self``)
        and the HEAD complement is an ``acomp`` ADJECTIVE (the affective first-cut → ``feels``).
      • Collect the head complement + its coordinated ``conj`` descendants whose POS is ADJ or whose
        tag is a PAST PARTICIPLE (VBN) — a predicate participle ("overwhelmed"/"exhausted") is an
        affective complement coordinated with the ADJ head, NOT an action. A present participle (VBG,
        ongoing action) and any other POS are excluded.
      • A conjunct carrying its OWN ``neg`` ("…but not exhausted") is skipped (negation deferred).
    Returns ``[]`` when there is no first-person affective copula (the caller then has nothing to add).
    Fail-safe → ``[]``."""
    doc = _parse(text)
    if doc is None:
        return []
    try:
        for tok in doc:
            if tok.dep_ not in ("nsubj", "nsubjpass"):
                continue
            if not _is_first_person_personal_pronoun(tok):
                continue
            head = tok.head
            comp = None
            if head.lemma_ == "be" and head.pos_ == "AUX":
                for child in head.children:
                    if child.dep_ == "acomp" and child.pos_ == "ADJ":
                        comp = child
                        break
            if comp is None:
                continue
            if any(c.dep_ == "neg" for c in head.children):
                continue  # whole-clause negation → deferred
            # Walk the conj coordination off the ADJ head, collecting ADJ + VBN predicate complements.
            out: list[str] = []
            seen: set = set()
            frontier = [comp]
            visited = {comp.i}
            members = [comp]
            while frontier:
                nxt = []
                for t in frontier:
                    for c in t.children:
                        if c.dep_ == "conj" and c.i not in visited and (
                                c.pos_ == "ADJ" or c.tag_ == "VBN"):
                            visited.add(c.i)
                            members.append(c)
                            nxt.append(c)
                frontier = nxt
            for m in sorted(members, key=lambda t: t.i):
                if any(ch.dep_ == "neg" for ch in m.children):
                    continue  # this conjunct is negated → skip
                if m.pos_ == "ADJ" and _adj_has_numeric_measure(m):
                    continue  # "34 years old" → age/measurement (copula-measure chain), not a feeling
                if m.pos_ == "ADJ" and _adj_has_prep_object(m):
                    continue  # "allergic to X" / "afraid of X" → a RELATION w/ an object, not a feeling
                surf = (m.text or m.lemma_ or "").strip().lower()
                if surf and surf not in seen:
                    seen.add(surf)
                    out.append(surf)
            return out
    except Exception as e:  # noqa: BLE001 — fail-safe
        log.warning("linguistics.analyze_copula_affect_complements_failed", error=str(e)[:160])
        return []
    return []


def analyze_copula_relational_predicate(text: str) -> list[dict]:
    r"""Capture a 1st-person copular predicate ADJECTIVE that carries a PREPOSITIONAL OBJECT as a
    RELATION with an object — the construction the feeling/affect seam MUST NOT claim.

    "I am allergic to penicillin"  → [{subject:'user', rel_type:'allergic_to', object:'penicillin'}]
    "I am afraid of spiders"       → [{subject:'user', rel_type:'afraid_of',  object:'spiders'}]
    "I'm allergic to penicillin and sulfa" → two edges (one per coordinated object).

    THE RULE (subject-agnostic, dependency-driven — NO adjective/medical/domain word list):
      • A copula ``be`` clause with a genuine 1st-person PERSONAL-pronoun subject ("I"/"we") — the
        SAME grammatical self-binding the affect seam uses (``_is_first_person_personal_pronoun``).
      • The complement is an ``acomp`` ADJECTIVE that governs a ``prep``→``pobj`` (see
        ``_adj_prep_objects``). A bare adjective ("I am excited" — no prep object) is NOT this lane
        (it stays a feeling); a MEASURED adjective ("I am 34 years old") is excluded by the numeric
        guard so an age is never mis-read here.
      • The RELATION is the user's OWN words: ``<adjective-lemma>_<preposition>`` ("allergic_to",
        "afraid_of") — a NOVEL rel_type that the ontology growth engine grounds/approves (miss→grow),
        never a hardcoded rel constant. The OBJECT is the pobj NP (head + left compound/amod mods).
      • A ``neg`` on the copula head or the adjective ("I am not allergic to penicillin") → the edge
        carries ``negated=True`` (negation/absence modeling is deferred downstream; the caller may
        drop it, parity with the other affect seams). Interrogatives are already excluded.

    Returns growth-ready edges ``[{subject:'user', rel_type, object, negated}]`` (empty when no such
    clause exists). Fail-safe: any error → ``[]``. Never resolves the object (strong ingest, lean
    query). The complementary half of the ``analyze_copula`` / ``analyze_copula_affect_complements``
    guards, which now DECLINE this construction so it is captured here instead of dropped as a feeling."""
    if not text or not text.strip():
        return []
    doc = _parse(text)
    if doc is None:
        return []
    out: list[dict] = []
    seen: set = set()
    try:
        for tok in doc:
            if tok.dep_ not in ("nsubj", "nsubjpass"):
                continue
            if not _is_first_person_personal_pronoun(tok):
                continue
            head = tok.head
            if head is None or not (head.lemma_ == "be" and head.pos_ == "AUX"):
                continue
            comp = None
            for child in head.children:
                if child.dep_ == "acomp" and child.pos_ == "ADJ":
                    comp = child
                    break
            if comp is None:
                continue
            # a MEASURED adjective ("34 years old") is a scalar, never a relational predicate.
            if _adj_has_numeric_measure(comp):
                continue
            prep_objs = _adj_prep_objects(comp)
            if not prep_objs:
                continue
            adj_lemma = (comp.lemma_ or comp.text or "").strip().lower()
            if not adj_lemma:
                continue
            negated = any(c.dep_ == "neg" for c in head.children) or any(
                c.dep_ == "neg" for c in comp.children)
            for prep_surf, pobj in prep_objs:
                if not prep_surf:
                    continue
                obj = _np_phrase(pobj)
                if not obj:
                    continue
                rel = f"{adj_lemma}_{prep_surf}".strip("_")
                key = (rel, obj)
                if not rel or key in seen:
                    continue
                seen.add(key)
                out.append({"subject": "user", "rel_type": rel, "object": obj,
                            "negated": bool(negated)})
    except Exception as e:  # noqa: BLE001 — fail-safe: never break ingest
        log.warning("linguistics.analyze_copula_relational_predicate_failed", error=str(e)[:160])
        return []
    return out


@dataclass(frozen=True)
class PossessivePredication:
    """A deterministic reading of a 1st-person possessive predication ("my favorite color is blue").

    - ``possessed`` : the possessed-noun phrase, lowercased ("favorite color") — the nsubj NOUN
                      plus its left ``compound``/``amod`` modifiers. This is the ATTRIBUTE.
    - ``value``     : the copula complement, lowercased ("blue") — the VALUE.
    - ``value_pos`` : the complement's universal POS ("ADJ"/"NOUN"/"PROPN").
    - ``negated``   : True when a ``neg`` dependency hangs off the copula head/complement.
    """
    possessed: str
    value: str
    value_pos: str
    negated: bool


def analyze_possessive_predication(text: str):
    r"""Deterministic first-cut for a 1st-person POSSESSIVE predication. Returns
    ``PossessivePredication`` or ``None``.

    THE RULE (subject-agnostic, dependency-driven — NO keyword/attribute word-list):
      Find a copular clause whose subject (``nsubj``/``nsubjpass``) is a NOUN carrying a ``poss``
      child that is a genuine 1st-person POSSESSIVE determiner ("my"/"our" — Person=1 ∧ Poss=Yes),
      with a complement attached as ``attr`` (NOUN/PROPN) or ``acomp`` (ADJ). spaCy structures
      "my favorite color is blue" / "my dog's name is Ace" this way. The possessed-noun phrase is
      the ATTRIBUTE ("favorite color") and the complement is the VALUE ("blue").

    This is the counterpart to ``analyze_copula``: that one fires only on a genuine 1st-person
    *personal* pronoun subject ("I"/"we"); THIS one fires only on a 1st-person *possessive*
    subject. They are mutually exclusive by morphology (Poss=Yes ⊕ no-Poss), so "my favorite
    color is blue" lands HERE (preference), never the self-predication path. Makes NO rel-type or
    taxonomy decision — the caller maps possessed→rel via the metadata-driven growth path.

    Negation is read from a ``neg`` dependency. Returns ``None`` when no such clause exists, when
    the possessor is not 1st-person possessive, or on any failure (fail-safe)."""
    doc = _parse(text)
    if doc is None:
        return None
    try:
        for tok in doc:
            if tok.dep_ not in ("nsubj", "nsubjpass"):
                continue
            # The subject must be a NOUN possessed by a 1st-person possessive determiner.
            if tok.pos_ not in ("NOUN", "PROPN"):
                continue
            poss_first_person = any(
                c.dep_ == "poss"
                and c.morph.get("Person") == ["1"]
                and "Yes" in c.morph.get("Poss")
                for c in tok.children
            )
            if not poss_first_person:
                continue

            head = tok.head
            comp = None
            if head.lemma_ == "be" and head.pos_ == "AUX":
                for child in head.children:
                    if child.dep_ in ("attr", "acomp"):
                        comp = child
                        break
            elif any(c.dep_ in ("cop", "aux", "auxpass") and c.lemma_ == "be" for c in head.children):
                comp = head
            if comp is None:
                continue

            # MEASURED-ADJECTIVE GUARD (parallel to analyze_copula's guard, linguistics.py:545).
            # "my daughter is 10 years old", "my house is 100 years old" predicate a MEASUREMENT
            # (age/duration), NEVER a preference. The ADJ complement is modified by a numeric measure
            # (a unit noun npadvmod ← nummod NUM — "10 years old"). Reading it as a preference mints
            # junk (user, <possessed>, "old"); worse, the possessed noun then canonicalizes to a
            # rel_type (a kinship noun → parent_of/child_of) and the bare adjective "old" registers as
            # a PHANTOM entity — the live (user, parent_of, old) leak. The copula-measure chain in
            # derive_sentence_facts OWNS this idiom as a scalar (age lands on the entity). So DECLINE
            # here. Fires ONLY when the number measures the adjective ITSELF; a plain single-word
            # preference ("my favorite colour is blue") has no numeric measure on the ADJ and is
            # untouched. Grammar-driven (_adj_has_numeric_measure), subject-agnostic, NO word list.
            if comp.pos_ == "ADJ" and _adj_has_numeric_measure(comp):
                return None

            # KINSHIP-POSSESSIVE GUARD (metadata-driven, kinship_noun cue class). "my daughter is …",
            # "my mother is …" possess a PERSON (a kin ROLE), never a preference AXIS. The deriver's
            # kinship + copula-measure/state chains OWN the kin construction ((daughter, child_of,
            # user) + age/state). Read as a preference it mints (user, <kin-noun>, value), and the kin
            # noun canonicalizes to a kin rel_type binding the value as a phantom entity — the same
            # (user, parent_of, X) leak class. So DECLINE when the possessed HEAD NOUN is a kinship
            # role. Subject-agnostic (per-tenant kinship_noun cue class via _kinship_nouns), fail-safe
            # (empty/unbound set → no decline; strictly no worse than today — the measured-ADJ guard
            # above still covers the reported "N years old" case regardless).
            _head_lemma = (tok.lemma_ or tok.text or "").strip().lower()
            if _head_lemma and _head_lemma in _kinship_nouns():
                return None

            # PASSIVE-PARTICIPLE PREDICATE GUARD (subject-agnostic, morphological). "My wife was born
            # on …", "my server was provisioned in …", "my company was founded in …" — here the copula/
            # auxpass branch makes the PARTICIPLE VERB the complement, so this seam would mis-capture the
            # passive EVENT as a preference VALUE ((user, wife, born) / (user, server, provisioned)). A
            # preference/attribute value is a predicative ADJ/NOUN ("my favorite color is BLUE"), NEVER a
            # verbal predicate. So REJECT a VERB complement (a passive participle is a predicate, not a
            # value) — the dated passive-event chain owns that construction. No lemma/word list.
            if comp.pos_ == "VERB":
                continue

            # TYPED-DEVICE + STRUCTURED-ATOMIC GUARD (ingest-hardening). "My router is a UniFi at
            # 192.168.1.1" is NOT a preference — it is a CLASSIFICATION ("router is a UniFi", an
            # indefinite-article NOUN/PROPN complement) carrying a trailing STRUCTURED SCALAR (the IP).
            # Reading it as a preference mints rel_type == the possessed noun ("router"), which then
            # COLLIDES with the router ENTITY the atomic detector needs to host (router, has_ip, <IP>)
            # → the IP is dropped (rel_type-as-entity rejection). So DEFER here: the deriver's
            # possessed-typed-atomic chain owns this construction (owns + instance_of, freeing the noun
            # as an entity), and the /ingest atomic detector binds the scalar. Gated on BOTH signals
            # (indefinite-article classification complement AND a structured-atomic pobj in the clause)
            # so a plain preference ("my favorite colour is blue") is untouched. Subject-agnostic,
            # grammatical (article + POS) + format-grammar (the atomic shape), no domain vocabulary.
            _comp_is_class = comp.pos_ in ("NOUN", "PROPN") and any(
                c.dep_ == "det" and (c.text or "").strip().lower() in ("a", "an")
                for c in comp.children
            )
            if _comp_is_class and any(
                    d.dep_ == "pobj" and _is_structured_atomic_value(d.text) for d in doc):
                return None

            # A QUESTION ("what is my favorite colour?") is not a statement of value — its
            # interrogative complement ("what"/"which"/"who") must NOT be captured as a fact.
            # Grammatical + subject-agnostic: wh-words carry PronType=Int / WP|WP$|WDT|WRB tags.
            # (Closed grammatical class read from morphology — NOT a hardcoded word list.)
            if "Int" in comp.morph.get("PronType") or comp.tag_ in ("WP", "WP$", "WDT", "WRB"):
                continue

            negated = any(c.dep_ == "neg" for c in head.children) or any(
                c.dep_ == "neg" for c in comp.children
            )
            # FULL multi-token value ("O negative", "dark blue"), not just the complement head —
            # see _complement_value_phrase (the "blood type is O negative" → "O" drop fix).
            value = _complement_value_phrase(comp)
            if not value:
                continue
            # Possessed-noun PHRASE = the nsubj NOUN plus its left compound/amod modifiers,
            # EXCLUDING the possessive determiner itself ("favorite color", not "my favorite color").
            mods = [
                c for c in tok.children
                if c.dep_ in ("compound", "amod") and c.i < tok.i
            ]
            parts = [m.text for m in sorted(mods, key=lambda m: m.i)] + [tok.text]
            possessed = " ".join(p.strip() for p in parts if p and p.strip()).lower()
            if not possessed:
                continue
            return PossessivePredication(
                possessed=possessed,
                value=value,
                value_pos=comp.pos_,
                negated=negated,
            )
    except Exception as e:  # noqa: BLE001 — fail-safe
        log.warning("linguistics.analyze_possessive_predication_failed", error=str(e)[:160])
        return None
    return None


# ── NAMING ANALYSIS — the "<noun> named/called <ProperName>" construction ──────────
# THE WHY (RC2): "I have a dog named Rex" / "a server called Apollo" / "my dog is named
# Rex" must mint a VALID naming edge binding the PROPER NAME to the HEAD NOUN being named
# (dog/server), NOT to the user. On the live harvest path the verb-lift over-strips "named" →
# "nam" and mints junk ``(noun, nam, ProperName)``; this seam OWNS the naming construction so the
# name lands as ``(head-noun, also_known_as, ProperName)`` and the junk is suppressed at the lift.
#
# Subject-agnostic + dependency-driven (NO noun/pronoun/keyword word-list). The NAMING-VERB LEMMA
# inventory — the English verbs that form the predicative naming/dubbing construction ("X named /
# called / titled / dubbed Y") — is a grammatical (lexical-aspect) class, bounded as a language
# primitive (like the copula "be" in ``analyze_copula``), NOT a domain list.
#
# DB-HELD + per-tenant + GROWABLE (migration 105 / linguistic_cue_overlay). The frozenset below is
# the RETIRED-as-authority in-code list, KEPT as the DB-DOWN CODE-FALLBACK seed: `_naming_verbs()`
# resolves the live set from `<tenant>.linguistic_cues` (category='naming_verb', seed-copied ∪ grown)
# via the per-tenant overlay (the SAME rail temporal_patterns uses), and falls back to THIS frozenset
# only when the overlay is unavailable/unbound (fail-safe — never lose naming detection). Mirrors the
# temporal `_relative_cues()` ↔ `_BOOTSTRAP_RELATIVE_CUES` contract exactly. Membership checks below
# call `_naming_verbs()`, NOT this frozenset directly.
_NAMING_VERB_LEMMAS: frozenset[str] = frozenset(
    {"name", "call", "title", "dub", "entitle", "christen",
     "designate", "term", "label", "nickname"}
)


def _naming_verbs() -> frozenset[str]:
    """Resolve the per-tenant ACTIVE naming-verb lemma set via the overlay (ContextVar-bound to the
    request's tenant schema, the SAME binding the rel_type/taxonomy/temporal overlays use). Returns a
    frozenset of lowercased verb lemmas. Fail-safe: any import/read failure / unbound schema → the
    in-code ``_NAMING_VERB_LEMMAS`` code-fallback seed so a DB-down / pre-migration / unwarmed-overlay
    turn still detects the naming construction instead of silently dropping it. Never empty."""
    try:
        from src.api import linguistic_cue_overlay  # deferred: avoid import cycle / hard dep
        dsn = os.environ.get("POSTGRES_DSN", "")
        cues = linguistic_cue_overlay.resolve_naming_verbs(dsn)
        if cues:
            return cues
        return _NAMING_VERB_LEMMAS  # empty resolution → code-fallback (never lose detection)
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the linguistic layer
        log.warning("linguistics.naming_verbs_resolve_failed", error=str(e)[:160])
        return _NAMING_VERB_LEMMAS


# ── EMPLOYMENT / ROLE-PREDICATION verb class — DB-DOWN CODE-FALLBACK SEED ────────────
# The bounded lexical class of EMPLOYMENT / role-predication verbs read by ``derive_sentence_facts``'s
# ``_chain_employment`` (the "<subject> <verb> as <role> [at|for <org>]" construction). Authority lives
# in ``<tenant>.linguistic_cues`` (category='employment_verb', seed-copied ∪ grown) resolved via the
# per-tenant overlay; this frozenset is the DB-DOWN / unbound-overlay fail-safe ONLY. The cue class is
# the SAFETY GATE — a verb NOT in it ("dress"/"know") never yields an occupation. Membership checks
# call ``_employment_verbs()``, NOT this frozenset directly. Mirrors ``_naming_verbs`` exactly.
_EMPLOYMENT_VERB_LEMMAS: frozenset[str] = frozenset(
    {"work", "serve", "act", "function", "employ", "hire", "appoint", "contract"}
)


def _employment_verbs() -> frozenset[str]:
    """Resolve the per-tenant ACTIVE employment-verb lemma set via the overlay (ContextVar-bound to the
    request's tenant schema — the SAME binding the naming/svo/kinship overlays use). Returns a frozenset
    of lowercased verb lemmas. Fail-safe: any import/read failure / unbound schema / empty resolution →
    the in-code ``_EMPLOYMENT_VERB_LEMMAS`` code-fallback seed so a DB-down / pre-migration / unwarmed-
    overlay turn still recognizes the employment construction instead of silently dropping it. Never
    empty. Mirrors ``_naming_verbs()`` exactly."""
    try:
        from src.api import linguistic_cue_overlay  # deferred: avoid import cycle / hard dep
        dsn = os.environ.get("POSTGRES_DSN", "")
        cues = linguistic_cue_overlay.resolve_employment_verbs(dsn)
        if cues:
            return cues
        return _EMPLOYMENT_VERB_LEMMAS  # empty resolution → code-fallback (never lose detection)
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the linguistic layer
        log.warning("linguistics.employment_verbs_resolve_failed", error=str(e)[:160])
        return _EMPLOYMENT_VERB_LEMMAS


# ── SHELL-NOUN class — DB-HELD + per-tenant + GROWABLE (migration 126 / linguistic_cue_overlay,
# category='shell_noun'). The GENERIC ABSTRACT/SHELL anaphoric heads ("the flaw"/"the ruling"/"the
# condition") a later sentence uses to re-refer to the turn's topic. Authority lives in
# ``<tenant>.linguistic_cues`` (seed-copied ∪ grown) resolved via the per-tenant overlay; this
# frozenset is the DB-DOWN / unbound-overlay fail-safe ONLY. Membership checks call ``_shell_nouns()``,
# NOT this frozenset directly. Mirrors ``_employment_verbs`` exactly.
_SHELL_NOUN_LEMMAS: frozenset[str] = frozenset({
    "flaw", "issue", "problem", "matter", "condition", "situation", "case",
    "finding", "defect", "fault", "entity", "item", "thing", "ruling",
    "decision", "incident",
})


def _shell_nouns() -> frozenset[str]:
    """Resolve the per-tenant ACTIVE shell-noun lemma set via the overlay (ContextVar-bound to the
    request's tenant schema — the SAME binding the naming/kinship overlays use).

    GROUNDING: "shell nouns" are an established open class of abstract nouns whose primary discourse use
    is anaphoric reference (Schmid 2000; ACL D13-1030). See DEV/DESIGN-ingest-hardening-grounding.md.
    Returns a frozenset of
    lowercased noun lemmas. Fail-safe: any import/read failure / unbound schema / empty resolution → the
    in-code ``_SHELL_NOUN_LEMMAS`` code-fallback seed so a DB-down / pre-migration / unwarmed-overlay
    turn still recognizes the shell-noun co-referent instead of islanding it. Never empty. Mirrors
    ``_employment_verbs()`` exactly."""
    try:
        from src.api import linguistic_cue_overlay  # deferred: avoid import cycle / hard dep
        dsn = os.environ.get("POSTGRES_DSN", "")
        cues = linguistic_cue_overlay.resolve_shell_nouns(dsn)
        if cues:
            return cues
        return _SHELL_NOUN_LEMMAS  # empty resolution → code-fallback (never lose detection)
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the linguistic layer
        log.warning("linguistics.shell_nouns_resolve_failed", error=str(e)[:160])
        return _SHELL_NOUN_LEMMAS


@dataclass(frozen=True)
class NamingAnalysis:
    """A deterministic reading of a naming/dubbing construction ("a dog named Rex").

    - ``named``       : the HEAD NOUN being named, lowercased ("dog", "server", "cat") — its
                        head plus left ``compound``/``amod`` modifiers ("file server"). NEVER the
                        speaker; the name binds to the THING named.
    - ``proper_name`` : the proper name assigned, surface form ("Rex", "Apollo").
    - ``negated``     : True when a ``neg`` dependency hangs off the naming verb ("not named X").
    """
    named: str
    proper_name: str
    negated: bool


def _np_phrase(tok) -> str:
    """Head noun + its left compound/amod modifiers, lowercased ("file server", "favorite dog").

    Subject-agnostic, structural only — the same NP-construction rule used elsewhere in this
    module. Excludes determiners/possessives. Returns the bare head text if no modifiers.
    """
    mods = [c for c in tok.children if c.dep_ in ("compound", "amod") and c.i < tok.i]
    parts = [m.text for m in sorted(mods, key=lambda m: m.i)] + [tok.text]
    return " ".join(p.strip() for p in parts if p and p.strip()).lower()


def _complement_value_phrase(comp) -> str:
    """The FULL copula-complement VALUE phrase, lowercased — the head plus its qualifying modifier
    children, in source order ("O negative", "dark blue", "type 2"), not just the head token.

    THE WHY (multi-token value truncation): "my blood type is O negative" parses the complement as the
    ADJ head "negative" with "O" as a left ``advmod`` — taking only ``comp.text`` dropped the "O" and
    read the value as "negative". This collects the complement's contiguous qualifying children
    (``compound``/``amod``/``advmod``/``nummod``/``nmod``/``npadvmod``/``quantmod``/``det`` when it is a
    numeric/letter grade, NOT an article) and rebuilds the span in index order. Determiners that are
    ARTICLES ("a"/"an"/"the") and punctuation/negation/prepositional children are excluded so a plain
    value ("blue") is unchanged and an article-classification complement is untouched. Structural,
    subject-agnostic, NO value word list. Fail-safe → ``comp.text`` lowercased."""
    try:
        _MOD = {"compound", "amod", "advmod", "nummod", "nmod", "npadvmod", "quantmod"}
        _ARTICLES = {"a", "an", "the"}
        mods = []
        for c in comp.children:
            if c.is_punct or c.dep_ == "neg":
                continue
            if c.dep_ in _MOD:
                mods.append(c)
            elif c.dep_ == "det" and (c.text or "").strip().lower() not in _ARTICLES:
                # a non-article determiner used as a grade token ("O" in "O negative" tags DT/det) —
                # keep it (it is part of the value), but never fold in an article.
                mods.append(c)
        toks = sorted(mods + [comp], key=lambda t: t.i)
        phrase = " ".join((t.text or "").strip() for t in toks if (t.text or "").strip()).lower()
        return phrase or (comp.text or "").strip().lower()
    except Exception:  # noqa: BLE001 — fail-safe
        return (comp.text or "").strip().lower()


# ── STRUCTURED-ATOMIC VALUE SHAPE (format-grammar routing gate — NOT the rel_type mapping) ──────────
# A ROUTING shape-check for a structured atomic literal (an IP / MAC / email / URL / CIDR) that spaCy
# keeps as a single token. It exists ONLY to let the deriver DECIDE ROUTING for the possessed-typed
# construction ("my router is a UniFi at 192.168.1.1") — so the trailing "at <IP>" is treated as a
# device scalar on the entity, NOT swallowed into the copula VALUE (the attr-scalar/preference twin).
# The AUTHORITATIVE rel_type (has_ip/has_mac/…) is still assigned downstream by the /ingest atomic
# detector (``_detect_atomic_values``) — this is the same universal format-grammar it harnesses, used
# here only as a boolean discriminator. Subject-agnostic, no domain vocabulary. A plain-word locative
# ("in the closet") / a bare time ("at 3pm") never matches (needs an internal ``.``/``:``/``@``).
_STRUCTURED_ATOMIC_RE = re.compile(
    r'^(?:'
    r'(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)(?:/\d{1,2})?'  # IPv4 / CIDR
    r'|(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}'                     # IPv6 (loose)
    r'|[0-9a-fA-F]{2}(?:[:\-][0-9a-fA-F]{2}){5}'                       # MAC (colon/dash)
    r'|[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}'              # email
    r'|https?://\S+'                                                   # URL
    r')$'
)


def _is_structured_atomic_value(text: str) -> bool:
    """True when ``text`` is a single-token structured atomic literal (IP/MAC/email/URL/CIDR)."""
    try:
        return bool(_STRUCTURED_ATOMIC_RE.match((text or "").strip()))
    except Exception:  # noqa: BLE001 — fail-safe: shape miss → not structured
        return False


def _object_value_phrase(tok) -> str:
    """Object/value NP phrase for a VALUE-bearing object slot (SVO/locative object).

    Head + its left ``compound``/``amod`` modifiers (exactly like ``_np_phrase``), PLUS the head's
    left ``nummod``/``quantmod`` — but ONLY when the head is a MULTI-TOKEN PROPER NAME. This is the
    grammatical distinction between a NUMBER-THAT-IS-PART-OF-A-NAME and a NUMBER-THAT-IS-A-COUNT:

      • "I live at 156 Cedar St. S"  → head "S" is PROPN with PROPN compounds ("Cedar", "St.") →
        a NAMED value → keep the leading number → "156 cedar st. s" (the house number is the value).
      • "I have 3 cats" / "I work for 3 companies" → head is a bare common NOUN → a COUNT → the
        quantifier is NOT folded in → "cats" / "companies" (unchanged — no relational regression).

    "Named" is decided STRUCTURALLY (subject-agnostic, no word list): the head is itself ``PROPN``,
    or it carries a left ``PROPN`` ``compound`` child (the multi-token-proper-name signature). This
    keeps the truncation fix scoped to values whose leading modifier genuinely belongs to the value
    (addresses, product/model names, serials-as-names) and NEVER absorbs a count into a relational
    object. Lowercased (matching ``_np_phrase`` / ``_emit``). Fail-safe → ``_np_phrase(tok)``.
    """
    try:
        head_is_named = tok.pos_ == "PROPN" or any(
            c.dep_ == "compound" and c.pos_ == "PROPN" and c.i < tok.i for c in tok.children
        )
        deps = ("compound", "amod", "nummod", "quantmod") if head_is_named else ("compound", "amod")
        mods = [c for c in tok.children if c.dep_ in deps and c.i < tok.i]
        parts = [m.text for m in sorted(mods, key=lambda m: m.i)] + [tok.text]
        phrase = " ".join(p.strip() for p in parts if p and p.strip()).lower()
        return phrase or _np_phrase(tok)
    except Exception:  # noqa: BLE001 — fail-safe: never break capture on a span build
        return _np_phrase(tok)


def _np_conjuncts(head_tok) -> list:
    """Return ``head_tok`` plus its COORDINATED noun siblings ("tomatoes, peppers, and cucumbers").

    spaCy chains a coordinated list off the FIRST conjunct's ``conj`` dependents: in "tomatoes,
    peppers, and cucumbers" the parse is tomatoes →conj peppers →conj cucumbers (or all three hang
    off the head). We walk the ``conj`` subtree from the head and collect every coordinated NOUN/PROPN
    token. Returns the token list in source order (head first). A non-coordinated head returns just
    ``[head_tok]``. Structural only — NO list/word enumeration.

    PROPER-NOUN LIST QUIRK (appos chaining): spaCy is INCONSISTENT about how it chains a
    comma-separated list of BARE PROPER NAMES. "Mia, Theo, and Leo" parses as a clean conj
    chain (Mia →conj Theo →conj Leo), but "Apollo, Vault, and Echo" parses the middle
    member as ``appos`` (Apollo →appos Vault →conj Echo) — so a pure-conj walk DROPS the tail
    ("Vault"/"Echo"). We therefore ALSO follow an ``appos`` edge, but ONLY when BOTH endpoints are
    PROPN (the proper-noun-list signature): an apposition RENAME ("my friend Sam", "the president, a
    leader") has a common-NOUN endpoint and is left untouched, so no rename is mis-distributed.
    Subject-agnostic, structural (PROPN↔PROPN appos), NO list/word enumeration."""
    out = [head_tok]
    seen = {head_tok.i}
    frontier = [head_tok]
    while frontier:
        nxt = []
        for t in frontier:
            for c in t.children:
                if c.i in seen:
                    continue
                _is_nominal = c.pos_ in ("NOUN", "PROPN")
                # conj is always a coordinated sibling. appos is a coordinated list member ONLY for the
                # PROPN↔PROPN proper-noun-list quirk above (never a common-noun apposition rename).
                #
                # OOV-CONJUNCT RECOVERY: the statistical tagger frequently MIS-POS-tags an
                # out-of-vocabulary coordinated member — "iOS"→NUM, "android"→ADJ, a novel product
                # name→X — so a bare ``pos_ in {NOUN,PROPN}`` filter SILENTLY DROPS it (and, when the
                # tail of the list hangs off that dropped token, the members after it too). The ``conj``
                # DEPENDENCY is the reliable structural signal in a nominal coordination; the POS is what
                # the tagger gets wrong on an unknown token. So a ``conj`` sibling is a member when it is
                # nominal OR a CONTENT token the tagger merely mis-typed (has an alphabetic char, and is
                # not a function-word/number/punctuation POS) — never a genuine number/punct/conjunction.
                # "we don't forget": a mis-tagged member is captured, not dropped. Structural, subject-
                # agnostic, NO name/type list.
                _conj_member = c.dep_ == "conj" and (
                    _is_nominal or (
                        c.pos_ not in ("PUNCT", "CCONJ", "SCONJ", "DET", "ADP", "PART",
                                       "AUX", "PRON", "SPACE", "SYM", "NUM")
                        and any(ch.isalpha() for ch in (c.text or ""))) or (
                        # NUM only when it carries an alphabetic char (the OOV mis-tag "iOS"→NUM),
                        # never a bare numeral ("42") — a numeral conjunct is a scalar, not an entity.
                        c.pos_ == "NUM" and any(ch.isalpha() for ch in (c.text or ""))))
                _appos_member = (
                    c.dep_ == "appos" and c.pos_ == "PROPN" and t.pos_ == "PROPN")
                if _conj_member or _appos_member:
                    seen.add(c.i)
                    out.append(c)
                    nxt.append(c)
        frontier = nxt
    return sorted(out, key=lambda t: t.i)


def _is_relative_pronoun(tok) -> bool:
    """True for a RELATIVE pronoun heading a relative clause ("the brother WHO lives…", "the car
    THAT runs…", "WHOSE"). Grammatical/morphological: the wh-pronoun tags WP / WP$ / WDT whose
    syntactic head is a ``relcl``/``acl`` clause. A DEMONSTRATIVE "that"/"this" is tagged DT (not
    WDT) so it is excluded; an INTERROGATIVE wh-word ("who runs?") heads no relcl so it is excluded
    here (and the question is already dropped upstream). Subject-agnostic, NO word list. Fail-safe →
    False."""
    try:
        if tok is None or tok.tag_ not in ("WP", "WP$", "WDT"):
            return False
        return tok.head is not None and tok.head.dep_ in ("relcl", "acl")
    except Exception:  # noqa: BLE001
        return False


def _relative_pronoun_antecedent(tok) -> str | None:
    """Resolve a relative pronoun to the surface of the NOUN it stands for — its antecedent. spaCy
    parses "<antecedent> who/that <relcl-verb> …" as wh →head→ relcl-verb →head→ antecedent-noun, so
    the antecedent is the relcl verb's head. Returns the lowercased antecedent surface ("user" for a
    first-person antecedent), or ``None`` when no antecedent resolves (the caller then DROPS the edge
    rather than bind the pronoun as an entity — THE HARD LINE: a function-word is never a memory).
    Structural, subject-agnostic, fail-safe → None."""
    try:
        if not _is_relative_pronoun(tok):
            return None
        ante = tok.head.head  # relcl-verb → its head noun = the antecedent
        if ante is None or ante is tok:
            return None
        if _is_first_person_personal_pronoun(ante):
            return "user"
        if ante.pos_ not in ("NOUN", "PROPN", "PRON"):
            return None
        surf = (ante.text or ante.lemma_ or "").strip().lower()
        return surf or None
    except Exception:  # noqa: BLE001
        return None


def list_conjuncts(text: str, head_phrase: str) -> list:
    r"""Surface each coordinated head noun in a CONJOINED LIST as its own NP phrase (Part B item 2).

    THE GAP (Agent 3 traced drop): "I planted tomatoes, peppers, and cucumbers in mid-February"
    collapses to a single generic object ("seeds" / "tomatoes") so the OTHER list members are lost —
    and each should be its OWN entity so the shared event_date can host on each. Given the source
    ``text`` and the resolved ``head_phrase`` (the object/event noun a seam already recovered), this
    LOCATES that head token in the parse and returns each coordinated conjunct as its own
    ``_np_phrase`` (lowercased, head + compound/amod modifiers).

    Returns a list of >=2 phrases ONLY when the head is genuinely coordinated ("tomatoes",
    "peppers", "cucumbers"); a NON-coordinated head returns ``[]`` (the caller keeps its single
    object — no behavior change). Subject-agnostic, structural (the spaCy ``conj`` dependency), NO
    word/list enumeration; GLiNER2-pure (this is SEGMENTATION, never a label/pattern). Fail-safe:
    layer unavailable / head not found / any error → ``[]``."""
    if not text or not text.strip() or not head_phrase or not head_phrase.strip():
        return []
    try:
        doc = _parse(text)
        if doc is None:
            return []
        hp = head_phrase.strip().lower()
        # The head token is the LAST word of the resolved head phrase (the noun; modifiers precede).
        head_word = hp.split()[-1]
        head_tok = None
        for tok in doc:
            if tok.pos_ in ("NOUN", "PROPN") and (tok.text or "").strip().lower() == head_word:
                # Prefer a token that actually heads a coordination (has a conj child).
                if any(c.dep_ == "conj" for c in tok.children):
                    head_tok = tok
                    break
                if head_tok is None:
                    head_tok = tok
        if head_tok is None:
            return []
        conj_toks = _np_conjuncts(head_tok)
        if len(conj_toks) < 2:
            return []
        out: list[str] = []
        seen: set[str] = set()
        for t in conj_toks:
            ph = _np_phrase(t)
            if ph and len(ph) >= 2 and ph not in seen:
                seen.add(ph)
                out.append(ph)
        return out if len(out) >= 2 else []
    except Exception as e:  # noqa: BLE001 — fail-safe: never break the harvest
        log.warning("linguistics.list_conjuncts_failed", error=str(e)[:160])
        return []


# Closed grammatical class of English RELATIVE pronouns (a LANGUAGE PRIMITIVE, not a domain word
# list — the same closed class the function-word guardrail already recognizes). Used ONLY to locate
# the relative-clause boundary inside a single enumeration ITEM ("Sarah who is 35" → name="Sarah",
# predicate="is 35"); spaCy tags the in-clause relative pronoun inconsistently (nsubj vs relcl-head),
# so a robust split keys on the relative-pronoun token itself, corroborated by the surrounding parse.
_REL_PRONOUNS: frozenset[str] = frozenset({"who", "whom", "which", "that", "whose"})


def _enum_content_tokens(s: str) -> set[str]:
    """Lowercased alphanumeric tokens of ``s`` for the enumeration fabrication guard (no deps)."""
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def split_enumeration(sentence: str):
    r"""Deterministically split a COLON-INTRODUCED named-entity enumeration into a clean atom set.

    THE RELIABILITY NET for the LLM atomizer (see the ``ENUM_PRESPLIT`` flag note). Given a sentence
    of the shape ``<prefix>: <item>, <item>, and <item>`` whose items NAME entities, returns:

        [ "<prefix>: <Name1>, <Name2>, and <Name3>."           # membership (BARE proper names only)
        , "<Name> is <attribute>." , ... ]                      # one per item that carries an attribute

    so the downstream collective walk routes EVERY member (it only distributes membership/kinship over
    BARE PROPN conjuncts) and each per-member attribute lands on its own clean copula clause. Returns
    ``None`` (fail-safe → caller keeps the LLM atom / raw sentence) when the sentence is not such an
    enumeration, when any item yields no proper NAME, or when the fabrication guard trips.

    Item shapes recognized (subject-agnostic, structural — NO domain/role/type word list):
      • RELATIVE clause  "Sarah who is 35"          → name="Sarah",  atom="Sarah is 35."
                         "Tom who is a backend dev" → name="Tom",    atom="Tom is a backend dev."
      • NAMING apposite  "a designer named Priya"   → name="Priya",  atom="Priya is a designer."
        (structural: a ``acl`` VERB with a PROPN ``oprd``/``attr``/``dobj`` child — the reduced
         "named/called <Name>" relative — NOT a verb word list)
      • BARE proper name "Leo"                    → name="Leo",  (no attribute atom)

    FABRICATION-SAFE ("USER IS TRUTH"): every emitted atom's content tokens must be a subset of the
    SOURCE sentence's content tokens (the copula "is" is the only inserted function word) — any
    introduced token aborts the whole split (return ``None``). The split only RE-SEGMENTS text that is
    actually present; it never invents a name, number, or type. Deterministic; spaCy-only; no LLM.
    """
    if not ENUM_PRESPLIT:
        return None
    s = (sentence or "").strip().rstrip(".").strip()
    if ":" not in s:
        return None
    prefix, rest = s.split(":", 1)
    prefix = prefix.strip()
    rest = rest.strip()
    if not prefix or not rest:
        return None
    # Split the post-colon region into items on commas, strip a leading coordinator ("and"/"or"),
    # then split a remaining no-comma two-item "X and Y". Structural punctuation only.
    items = [re.sub(r"^(?:and|or)\s+", "", x.strip(), flags=re.I).strip()
             for x in re.split(r",\s*", rest) if x.strip()]
    items = [it for it in items if it]
    if len(items) == 1:
        parts = re.split(r"\s+(?:and|or)\s+", items[0], flags=re.I)
        if len(parts) > 1:
            items = [p.strip() for p in parts if p.strip()]
    if len(items) < 2:
        return None
    try:
        names: list[str] = []
        per_item: list[str] = []
        for it in items:
            d = _parse(it)
            if d is None:
                return None
            name = None
            atom = None
            # (1) NAMING apposite: a reduced "named/called <PROPN>" relative — structural ``acl`` VERB
            #     with a PROPN oprd/attr/dobj child. The modified noun phrase (everything before the
            #     acl verb) is the TYPE; the PROPN is the NAME.
            nv = next((t for t in d
                       if t.dep_ == "acl" and t.pos_ == "VERB"
                       and any(c.dep_ in ("oprd", "attr", "dobj") and c.pos_ == "PROPN"
                               for c in t.children)), None)
            if nv is not None:
                pr = next(c for c in nv.children
                          if c.dep_ in ("oprd", "attr", "dobj") and c.pos_ == "PROPN")
                name = pr.text
                typ = " ".join(w.text for w in d if w.i < nv.i).strip()
                if typ:
                    atom = f"{name} is {typ}."
            # (2) RELATIVE clause: a relative pronoun splits NAME (before) from PREDICATE (after).
            if name is None:
                rp = next((t for t in d if t.text.lower() in _REL_PRONOUNS), None)
                if rp is not None and rp.i > 0:
                    _nm = " ".join(w.text for w in d if w.i < rp.i).strip()
                    _pred = " ".join(w.text for w in d if w.i > rp.i).strip()
                    if _nm and _pred:
                        name = _nm
                        atom = f"{_nm} {_pred}."
            # (3) BARE proper name (no attribute).
            if name is None:
                props = [t for t in d if t.pos_ == "PROPN"]
                if props:
                    name = " ".join(t.text for t in props)
            if not name:
                return None  # an item with no proper NAME → not a named-entity enumeration → bail
            names.append(name.strip())
            if atom:
                per_item.append(atom)
        if len(names) < 2:
            return None
        if len(names) == 2:
            member_list = f"{names[0]} and {names[1]}"
        else:
            member_list = ", ".join(names[:-1]) + ", and " + names[-1]
        atoms = [f"{prefix}: {member_list}."] + per_item
        # FABRICATION GUARD: every emitted atom's content tokens ⊆ source (copula "is" excepted).
        src = _enum_content_tokens(sentence) | {"is"}
        for a in atoms:
            if not _enum_content_tokens(a) <= src:
                log.warning("linguistics.split_enumeration_fabrication_guard",
                            atom_preview=a[:80])
                return None
        return atoms
    except Exception as e:  # noqa: BLE001 — fail-safe: any failure → no pre-split (caller keeps LLM)
        log.warning("linguistics.split_enumeration_failed", error=str(e)[:160])
        return None


def analyze_naming_all(text: str) -> list:
    r"""Deterministic reading of EVERY naming/dubbing construction in ``text``. Returns a list of
    ``NamingAnalysis`` (possibly empty) — the multi-construction sibling of ``analyze_naming``.

    A comma-and enumeration ("I have a dog named Rex, a snake named Sophia, and a cat named
    Goose") contains ONE naming verb ("named"/"called") per conjunct, each modifying its OWN head
    noun. The single-result ``analyze_naming`` returned only the FIRST, dropping Sophia/Goose. This
    walks the SAME per-verb grammar (identical recovery rules), collecting one (head-noun, proper-
    name) pair for every naming verb that yields a valid pair. Subject-agnostic — the KIND is whatever
    common noun the verb modifies; this function makes NO entity-typing or rel-type decision.

    Deterministic, fail-safe: parse miss / any failure → ``[]``. Order is source order (first verb
    first), so ``analyze_naming`` == ``analyze_naming_all()[0]`` when any construction is present."""
    doc = _parse(text)
    if doc is None:
        return []
    out: list = []
    try:
        _naming = _naming_verbs()  # per-tenant grown set (overlay) ∪ code-fallback; resolved once
        _seen_pairs: set = set()
        for tok in doc:
            res = _analyze_naming_at(tok, _naming)
            if res is None:
                continue
            _pair = (res.named, res.proper_name.lower())
            if _pair in _seen_pairs:
                continue
            _seen_pairs.add(_pair)
            out.append(res)
    except Exception as e:  # noqa: BLE001 — fail-safe
        log.warning("linguistics.analyze_naming_all_failed", error=str(e)[:160])
        return []
    return out


def _analyze_naming_at(tok, _naming):
    r"""Recover a single naming construction headed by the naming verb ``tok`` (or ``None``).

    This is the per-verb body lifted verbatim out of ``analyze_naming`` so BOTH the single-result
    (``analyze_naming``) and the multi-result (``analyze_naming_all``) callers share ONE grammar —
    no behavioral drift. ``_naming`` is the resolved per-tenant naming-verb set. Subject-agnostic;
    makes no typing/rel decision; returns a (head-noun, proper-name) ``NamingAnalysis`` or ``None``."""
    if (tok.lemma_ or "").strip().lower() not in _naming:
        return None
    if tok.pos_ not in ("VERB", "AUX"):
        return None

    # ── Recover the HEAD NOUN being named ──────────────────────────────────────────
    # Reduced relative ("a dog named Rex"): the verb is an ``acl``/``relcl`` whose
    # HEAD is the noun. Passive copular ("my dog is named Rex"): the verb is ROOT
    # with an ``nsubjpass`` noun. Subject-agnostic: ANY noun head qualifies.
    head_noun = None
    if tok.dep_ in ("acl", "relcl", "vfin") and tok.head.pos_ in ("NOUN", "PROPN"):
        head_noun = tok.head
    if head_noun is None:
        for c in tok.children:
            if c.dep_ in ("nsubjpass", "nsubj") and c.pos_ in ("NOUN", "PROPN"):
                head_noun = c
                break
    if head_noun is None:
        return None

    # ── Recover the PROPER NAME assigned ──────────────────────────────────────────
    # The name is the verb's nominal complement (``oprd``/``attr``/``dobj``) that is a
    # proper noun. ``oprd`` (object predicate) is spaCy's tag for the naming complement;
    # ``attr`` covers the passive ("is named X"); ``dobj`` is a fallback. PROPN only so
    # we never bind a common-noun complement as a name.
    proper = None
    for c in tok.children:
        if c.dep_ in ("oprd", "attr", "dobj", "obj") and c.pos_ == "PROPN":
            proper = c
            break
    if proper is None:
        return None

    # A naming construction whose "head noun" is itself the proper name (e.g. parse
    # quirks) is not a useful (thing, name) pair — require them distinct.
    named = _np_phrase(head_noun)
    proper_name = (proper.text or "").strip()
    if not named or not proper_name or named == proper_name.lower():
        return None

    negated = any(c.dep_ == "neg" for c in tok.children)
    return NamingAnalysis(named=named, proper_name=proper_name, negated=negated)


def analyze_naming(text: str):
    r"""Deterministic first-cut for a naming/dubbing construction. Returns ``NamingAnalysis`` | None.

    THE RULE (subject-agnostic, dependency-driven — NO noun/keyword word-list):
      Find a naming verb (lemma "name"/"call" — the predicative naming class) and bind the PROPER
      NAME it assigns to the HEAD NOUN it modifies. spaCy structures the three target phrasings as:

        "I have a dog named Rex"  → "named" is an ``acl``/``vfin`` modifying the NOUN "dog";
                                        "Rex" is its ``oprd``/``attr``/``dobj`` child (PROPN).
        "a server called Apollo"      → same reduced-relative shape on "server".
        "my dog is named Rex"     → "named" is the ROOT (passive: ``nsubjpass`` "dog",
                                        ``auxpass`` "is"); "Rex" is the ``oprd``/``attr``.

      In every shape the HEAD NOUN is recovered structurally (the verb's nominal head, or its
      passive subject) and the PROPER NAME is the verb's nominal complement that is a PROPN. The
      name binds to the HEAD NOUN, never to the speaker.

    Negation is read from a ``neg`` dependency on the naming verb. Returns ``None`` when there is
    no naming construction, when no PROPN complement is found, or on any failure (fail-safe).

    Makes NO entity-typing or rel-type decision (GLiNER2/ontology own those) — it hands the
    caller a grammatical (head-noun, proper-name) pair for the ``also_known_as`` naming edge."""
    doc = _parse(text)
    if doc is None:
        return None
    try:
        _naming = _naming_verbs()  # per-tenant grown set (overlay) ∪ code-fallback; resolved once
        for tok in doc:
            res = _analyze_naming_at(tok, _naming)
            if res is not None:
                return res  # FIRST construction — byte-identical to the pre-refactor behavior
    except Exception as e:  # noqa: BLE001 — fail-safe
        log.warning("linguistics.analyze_naming_failed", error=str(e)[:160])
        return None
    return None


# ── NAMED-INSTANCE COPULA+APPOSITIVE — "My dog Rex is a poodle." ────────────────
# THE WHY (the named-instance capture wall): "My dog Rex is a poodle." is the appositive
# naming + copula type-predication construction. spaCy parses it deterministically as:
#     My   poss     -> dog        (Poss=Yes, Person=1)
#     dog  nsubj     -> is         (the possessed HEAD NOUN — the TYPE/kind)
#     Rex appos  -> dog        (the PROPER NAME of the specific instance)
#     is   ROOT (AUX, lemma be)
#     poodle attr    -> is         (the copula complement — the more-specific TYPE)
# The live LLM relation extractor (/extract/rewrite) returns [] for this; the GLiNER2 lane only
# harvests "my dog" → (user, owns, dog), DROPPING the named instance (Rex), its type (poodle),
# and the poodle→dog type rung. This analyzer recovers the four grammatical roles so the caller's
# seam can mint, per THE HARD LINE:
#   • the NAME ("Rex") goes in the NAMING layer (also_known_as) — it is NEVER classified into L4;
#   • the named instance is instance_of its complement TYPE ("poodle");
#   • the complement TYPE subclass_of the head-noun KIND ("poodle" subclass_of "dog") — the L4 rung;
#   • the pet/ownership relation points at the NAMED instance, never a bare type noun.
#
# Subject-agnostic + dependency/morphology-driven (NO noun/name/breed/keyword word-list). The ONLY
# closed sets consulted are universal grammatical primitives already used in this module: the copula
# lemma "be", the appositive ``appos`` dependency, the ``poss`` possessive dependency, the
# determiner ``det`` (to confirm the complement is a common-noun TYPE), and the wh-interrogative
# morphology gate. Casing-robust: the appositive name may tag PROPN ("Rex"/"Betsy") or NOUN
# ("Mittens" lemmatized to "whisker") — both are accepted as the instance NAME because the
# ``appos`` dependency, not the POS, identifies it as the renaming of the head.


@dataclass(frozen=True)
class NamedInstanceAnalysis:
    """A deterministic reading of the named-instance copula+appositive construction
    ("My dog Rex is a poodle." / "My cat Mittens is a tabby." / "My car Betsy is a Subaru.").

    - ``kind``         : the possessed HEAD NOUN being typed/named, lowercased ("dog", "cat",
                         "car") — its head plus left ``compound``/``amod`` modifiers. The broad KIND.
    - ``name``         : the appositive PROPER NAME of the specific instance, surface form
                         ("Rex", "Mittens", "Betsy"). Goes in the NAMING layer — never L4.
    - ``instance_type``: the copula complement common-noun TYPE, lowercased ("poodle", "tabby",
                         "subaru") — the more-specific type the named instance IS an instance_of.
    - ``possessor_is_self`` : True when the head noun carries a genuine 1st-person possessive
                         determiner ("my"/"our" — Person=1 ∧ Poss=Yes) → the pet/ownership relation
                         binds to the user. False when there is no 1st-person possessive (the caller
                         then mints no ownership edge; the type/naming structure still stands).
    - ``negated``      : True when a ``neg`` dependency hangs off the copula head/complement.
    """
    kind: str
    name: str
    instance_type: str
    possessor_is_self: bool
    negated: bool


def analyze_named_instance(text: str):
    r"""Deterministic first-cut for the named-instance copula+appositive construction.
    Returns ``NamedInstanceAnalysis`` or ``None``.

    THE RULE (subject-agnostic, dependency/morphology-driven — NO noun/name/breed word-list):
      Find a copular clause ("be" AUX) whose nominal subject (``nsubj``/``nsubjpass``) is a NOUN
      (the broad KIND) that carries BOTH
        (a) an ``appos`` child (the PROPER NAME of the specific instance — the rename of the head),
        (b) a copula complement (``attr``/``acomp`` NOUN/PROPN) introduced by a determiner
            ("a"/"an"/"the" — a ``det`` child) so it is a COMMON-NOUN TYPE, not a second name.
      The possessor flag is read from a 1st-person ``poss`` determiner on the subject NOUN.

    The complement MUST be determiner-introduced common-noun TYPE: this is what separates
      "My dog Rex is a poodle." (capture: instance_of poodle, poodle subclass_of dog)
    from
      "Rex is happy."        (ADJ acomp, no determiner → NOT this construction → None; the
                                  feeling/state seam owns it — we mint no bogus type), and
      "My dog Rex is Max."   (a bare PROPN complement with no determiner → a second NAME, not a
                                  type → None; we never classify a name into L4).

    A clause with NO appositive ("My dog is a poodle.") returns ``None`` HERE — that bare
    type-predication has no named instance, so the caller's other lanes (the kind gets typed by the
    ordinary owns + instance_of placement) handle it; this analyzer only fires when a NAME is
    present, so it can never over-capture the nameless case.

    Negation read from a ``neg`` dependency. Makes NO entity-typing/rel-type decision (the caller's
    seam maps roles → also_known_as / instance_of / subclass_of via the ontology). Returns ``None``
    when the construction is absent or on any failure (fail-safe)."""
    doc = _parse(text)
    if doc is None:
        return None
    try:
        for tok in doc:
            if tok.dep_ not in ("nsubj", "nsubjpass"):
                continue
            if tok.pos_ not in ("NOUN", "PROPN"):
                continue
            head = tok.head
            # The clause head must be the copula "be" (AUX) — "X is a Y" / "X is a Y" passive.
            comp = None
            if head.lemma_ == "be" and head.pos_ == "AUX":
                for child in head.children:
                    if child.dep_ in ("attr", "acomp"):
                        comp = child
                        break
            elif any(c.dep_ in ("cop", "aux", "auxpass") and c.lemma_ == "be" for c in head.children):
                comp = head
            if comp is None:
                continue

            # (a) the appositive NAME of the specific instance — the rename of the subject head.
            # ``appos`` identifies a renaming nominal regardless of its POS tag (PROPN "Rex"
            # /"Betsy", or NOUN "Mittens"); a determiner-introduced appositive ("a poodle") is a
            # TYPE apposition, not a name, so require NO ``det`` child on the appositive.
            appos = None
            for c in tok.children:
                if c.dep_ != "appos":
                    continue
                if c.pos_ not in ("PROPN", "NOUN"):
                    continue
                if any(gc.dep_ == "det" for gc in c.children):
                    continue  # "the poodle" apposition is a type, not a name
                appos = c
                break
            if appos is None:
                continue

            # (b) the complement must be a DETERMINER-INTRODUCED COMMON-NOUN TYPE ("a poodle"),
            # never a bare proper name ("Max") and never an adjective ("happy"). The ``det`` child
            # is the grammatical primitive that marks a common-noun type — casing-robust (the sm
            # model may title-case-tag "Subaru" as PROPN, but "a Subaru" still carries the det).
            if comp.pos_ not in ("NOUN", "PROPN"):
                continue
            if not any(c.dep_ == "det" for c in comp.children):
                continue
            # A QUESTION complement ("what is my dog Rex?") is not a statement of type.
            if "Int" in comp.morph.get("PronType") or comp.tag_ in ("WP", "WP$", "WDT", "WRB"):
                continue

            # 1st-person possessive on the subject head → the ownership/pet relation binds to user.
            possessor_is_self = any(
                c.dep_ == "poss"
                and c.morph.get("Person") == ["1"]
                and "Yes" in c.morph.get("Poss")
                for c in tok.children
            )

            kind = _np_phrase(tok)                       # "dog" (head + compound/amod, no det/poss)
            name = (appos.text or "").strip()            # "Rex" — surface form, NAMING layer
            instance_type = (comp.text or "").strip().lower()  # "poodle" — the specific TYPE
            if not kind or not name or not instance_type:
                continue
            # Distinctness: the name, the kind and the type must be three different tokens — guards
            # against parse quirks where the appositive or complement echoes the head.
            if name.lower() in (kind, instance_type) or kind == instance_type:
                continue

            negated = any(c.dep_ == "neg" for c in head.children) or any(
                c.dep_ == "neg" for c in comp.children
            )
            return NamedInstanceAnalysis(
                kind=kind,
                name=name,
                instance_type=instance_type,
                possessor_is_self=possessor_is_self,
                negated=negated,
            )
    except Exception as e:  # noqa: BLE001 — fail-safe
        log.warning("linguistics.analyze_named_instance_failed", error=str(e)[:160])
        return None
    return None


# ── UNIFIED NAME↔TYPE BINDING — the ONE connector-agnostic named-instance detector ────────────────
# THE WHY (the THIRD named-instance wall): "a dog named Rex", "a son Alex 19", "my friend Sam",
# "a server Apollo", "my friend is Sam" are ONE thing — a PROPER NAME is introduced and classified
# as a common-noun TYPE. ONLY the CONNECTOR varies (the naming verb "named", bare apposition, the
# copula "is"). Extraction had grown CHAIN-PER-CONSTRUCTION: ``analyze_naming`` keyed on the verb
# "named"; the appositive/copula forms fell through to garbage (an enumeration "a son Alex, a daughter
# Robin" yielded the bare role nouns ``son``/``daughter`` as entities, ZERO child_of, ZERO ages,
# the proper names never created). This detector keys on the (ProperName ↔ common-noun Type) BINDING
# ITSELF, connector-agnostic, so ALL three forms bind through ONE path — and the DOMAIN (pet vs kid vs
# server) lives ENTIRELY in METADATA (the kinship cue class + the possession-by-type overlay), never in
# code. To teach a new connector you ADD a structural shape here; to teach a new domain you GROW a cue
# row — never a code branch.
#
# THE HARD LINE: the PROPER NAME is the INSTANCE (its own entity, filed via also_known_as, NEVER
# classified into L4); the common noun is its TYPE/classification (instance_of). The relation the
# instance plays (child_of for a kid, has_pet for a pet, owns for an object, friend/knows for a person)
# is resolved FROM THE TYPE'S CATEGORY via metadata — the ONLY place domains differ.
#
# Subject-agnostic + dependency/morphology-driven. The ONLY closed sets consulted are universal
# grammatical primitives already used here (the naming-verb cue class for the "named" connector, the
# ``appos``/``acl``/copula dependencies, the determiner ``det``, the wh-interrogative morphology) plus
# the DB-grown kinship/possession metadata. It makes NO entity-typing decision (GLiNER2 owns that) and
# NO final rel choice for non-kin domains (the caller/deriver resolves possession-by-type) — it hands
# back the grammatical (name, type, connector, age, nickname) roles.


@dataclass(frozen=True)
class NameTypeBinding:
    """A deterministic reading of ONE (ProperName ↔ common-noun Type) binding, connector-agnostic.

    - ``name``      : the PROPER NAME introduced (surface form, e.g. "Rex", "Alex", "Sam").
                      The INSTANCE — filed via also_known_as, NEVER classified into L4 (THE HARD LINE).
    - ``type_noun`` : the common-noun TYPE the name is classified as, lowercased ("dog", "son",
                      "friend", "server") — its head plus left ``compound``/``amod`` modifiers.
    - ``connector`` : which grammatical shape bound them — "named" (naming verb), "appos" (bare
                      apposition), or "copula" ("my friend is Sam"). Diagnostic only; the emitted
                      edges are identical across connectors.
    - ``age``       : the bare cardinal NUM in the binding's span, as a STRING ("19"), or ``None``.
                      Routes to the ``age`` scalar (the unit_scalar bare-number person default).
    - ``nickname``  : a "<who> goes by <Nick>" nickname run bound to THIS name, surface form
                      ("Jay"), or ``None``. Registers as a second also_known_as of the instance.
    - ``possessor_is_self`` : True when the TYPE noun carries a 1st-person possessive ("my son",
                      "my friend") OR is the object of a 1st-person ``have`` clause ("I have a son
                      Alex", "we have a daughter Robin") → the relation binds to the user.
    - ``negated``   : True when a ``neg`` hangs off the binding's clause.
    """
    name: str
    type_noun: str
    connector: str
    age: str | None
    nickname: str | None
    possessor_is_self: bool
    negated: bool


def _binding_own_relcl(name_tok, type_tok):
    r"""The relative clause that belongs to THIS binding's NAME ("Jamie who goes by Jay"), or None.

    CRITICAL SCOPING (the sibling-bleed bug): in an enumeration "a son Alex 19, a son Jamie who
    goes by Jay 12" spaCy nests the 2nd member ("son Jamie …") as an ``appos`` UNDER the 1st
    member's type head, so the whole subtree of Alex's "son" ALSO contains Jamie's relcl. Scanning
    that subtree mis-attached Jamie's nickname/age to Alex. The relcl belongs to a binding ONLY
    when its ``relcl``/``acl`` head IS this binding's own ``type_tok`` (the head the NAME is the
    appositive of). Returns the relcl verb token whose head == type_tok, else None. Structural."""
    try:
        for c in type_tok.children:
            if c.dep_ in ("relcl", "acl") and c.pos_ == "VERB":
                return c
    except Exception:  # noqa: BLE001
        pass
    return None


def _binding_age_string(name_tok, type_tok):
    r"""The bare cardinal NUM that scopes THIS named-instance binding ("a son Alex 19" → "19"), or
    None. SCOPED to the binding's own region (never a sibling's number — see ``_binding_own_relcl``):
    the age is a ``nummod`` whose head is the NAME ("Alex 19"), or — in the nickname construction
    ("Jamie who goes by Jay 12") — a ``nummod`` inside THIS binding's OWN relcl (head=type_tok),
    typically on the nickname pobj. A NUM that quantifies the TYPE head itself ("three children") is a
    count, not an age, and is excluded. Structural, no number zoo. Fail-safe → None."""
    try:
        # (a) direct: a cardinal nummod of the NAME token ("Alex 19").
        for d in name_tok.children:
            if d.pos_ == "NUM" and d.dep_ == "nummod":
                txt = (d.text or "").strip()
                if txt:
                    return txt
        # (b) nickname case: a cardinal nummod inside THIS binding's own relcl (head=type_tok),
        #     e.g. "Jay 12" where 12 is nummod of the nickname pobj — scoped to the OWN relcl only.
        relcl = _binding_own_relcl(name_tok, type_tok)
        if relcl is not None:
            for d in relcl.subtree:
                if d.pos_ == "NUM" and d.dep_ == "nummod" and (
                        d.head is None or d.head.i != type_tok.i):
                    txt = (d.text or "").strip()
                    if txt:
                        return txt
        return None
    except Exception:  # noqa: BLE001
        return None


def _binding_nickname(name_tok, type_tok):
    r"""A "<who> goes by <Nick>" nickname bound to THIS named instance ("Jamie who goes by Jay"), or
    None. SCOPED to the binding's OWN relcl (head=type_tok — see ``_binding_own_relcl``) so a sibling's
    nickname never bleeds onto this name. spaCy parses the relative clause's ``go`` verb with head =
    the TYPE noun the NAME is the appositive of; the nickname is the ``pobj`` of the ``by`` prep. We
    accept it when THIS binding's own relcl is a ``go``-lemma verb governing a ``by`` prep with a
    PROPN/NOUN pobj that is NOT the bound name. Structural (dependency + the surface preposition "by"),
    NO nickname word-list. Fail-safe → None."""
    try:
        relcl = _binding_own_relcl(name_tok, type_tok)
        if relcl is None or (relcl.lemma_ or "").strip().lower() != "go":
            return None
        for c in relcl.children:
            if c.dep_ == "prep" and (c.text or "").strip().lower() == "by":
                pobj = next((g for g in c.children
                             if g.dep_ == "pobj" and g.pos_ in ("PROPN", "NOUN")), None)
                if pobj is not None:
                    nick = (pobj.text or "").strip()
                    if nick and nick.lower() != (name_tok.text or "").strip().lower():
                        return nick
        return None
    except Exception:  # noqa: BLE001
        return None


def _bound_name_for_type(type_tok, _naming):
    r"""Find the PROPER NAME bound to a common-noun TYPE head via ANY connector. Returns
    ``(name_tok, connector)`` or ``(None, None)``. THE ONE connector-agnostic binding rule:

      (1) APPOSITION   — a ``appos`` child that is a PROPN (or a NOUN the sm model mis-tagged) with NO
                         determiner of its own ("a son Alex", "a dog Rex", "my friend Sam").
                         A determiner-introduced appositive ("a son, a doctor") is a TYPE apposition,
                         not a name → rejected.
      (2) NAMING VERB  — an ``acl``/``relcl`` naming-verb child ("dog named Rex") whose
                         ``oprd``/``attr``/``dobj`` is a PROPN ("Rex").
      (3) COPULA       — the type is the ``nsubj`` of a copula ``be`` whose ``attr``/``oprd`` is a
                         PROPN with no determiner ("my friend is Sam").

    A determiner-introduced common-noun complement is never a name (it's a type); the wh-interrogative
    is excluded. Structural + the naming-verb cue class only; subject-agnostic, no name word-list."""
    # (1) apposition
    for c in type_tok.children:
        if c.dep_ != "appos" or c.pos_ not in ("PROPN", "NOUN"):
            continue
        if any(g.dep_ == "det" for g in c.children):
            continue  # "a son, a doctor" — type apposition, not a name
        try:
            if "Int" in c.morph.get("PronType") or c.tag_ in ("WP", "WP$", "WDT", "WRB"):
                continue
        except Exception:  # noqa: BLE001
            pass
        return c, "appos"
    # (2) naming verb (reduced relative "dog named Rex" / "server named apollo"). The object-predicate
    #     (``oprd``) complement of a naming verb is DEFINITIONALLY the assigned name — a complex-
    #     transitive naming construction "name/call/dub <X> <Name>" whose object complement spaCy tags
    #     ``oprd`` ("object predicate"; see spaCy glossary — https://github.com/explosion/spaCy glossary.py).
    #     CASING-ROBUST, mirroring the copula branch (3) below: en_core_web_sm tags a LOWERCASE name
    #     ("apollo"/"rex"/"mittens") as NOUN, not PROPN, so a PROPN-ONLY match silently dropped every
    #     lowercase named instance on the possessed form ("I have a server named apollo") — the diagnosed
    #     drop. Accept a PROPN complement always, and a NOUN complement too: the naming construction leaves
    #     no ambiguity — the post-naming-verb object-predicate complement IS the name regardless of its POS
    #     tag. Exclude a determiner-introduced complement ("named a successor" — a TYPE, not a name) and the
    #     wh-interrogative. Structural + the naming_verb cue class only; subject-agnostic, no name word-list.
    for c in type_tok.children:
        if c.dep_ not in ("acl", "relcl", "vfin"):
            continue
        if (c.lemma_ or "").strip().lower() not in _naming or c.pos_ not in ("VERB", "AUX"):
            continue
        for g in c.children:
            if g.dep_ not in ("oprd", "attr", "dobj", "obj") or g.pos_ not in ("PROPN", "NOUN"):
                continue
            if any(d.dep_ == "det" for d in g.children):
                continue  # "named a successor" — det-introduced type, not a name
            try:
                if "Int" in g.morph.get("PronType") or g.tag_ in ("WP", "WP$", "WDT", "WRB"):
                    continue
            except Exception:  # noqa: BLE001
                pass
            return g, "named"
    # (3) copula "my friend is Sam" — the type is the nsubj of a copula whose complement is a NAME.
    #     Casing-robust: the sm model may tag a person name as NOUN ("Sam" → NOUN); we accept a
    #     NOUN complement as a NAME *only* when (a) the subject role is 1st-person POSSESSED ("my
    #     friend"/"my sister" — a user-anchored role, the same gate _chain_copula_name uses) AND (b)
    #     the complement is NOT determiner-introduced (a det → "is a poodle" is a TYPE, owned
    #     elsewhere). A bare PROPN complement is always accepted. This never over-captures "the printer
    #     is Apollo"-style non-possessed subjects (no 1st-person poss → NOUN complement rejected).
    if type_tok.dep_ in ("nsubj", "nsubjpass"):
        head = type_tok.head
        if head is not None and head.lemma_ == "be" and head.pos_ == "AUX":
            _self_poss = any(
                c.dep_ == "poss" and c.morph.get("Person") == ["1"] and "Yes" in c.morph.get("Poss")
                for c in type_tok.children
            )
            for c in head.children:
                if c.dep_ not in ("attr", "oprd", "dobj", "obj"):
                    continue
                if c.pos_ == "PROPN":
                    pass
                elif c.pos_ == "NOUN" and _self_poss:
                    pass  # casing-robust: a possessed-role copula's NOUN complement is the name
                else:
                    continue
                if any(g.dep_ == "det" for g in c.children):
                    continue  # "is a poodle" — det-introduced type, not a name
                try:
                    if "Int" in c.morph.get("PronType") or c.tag_ in ("WP", "WP$", "WDT", "WRB"):
                        continue
                except Exception:  # noqa: BLE001
                    pass
                return c, "copula"
    return None, None


def _type_is_self_possessed(type_tok):
    r"""Does the TYPE noun belong to the speaker — "my son" / "I have a son …" / "we have a daughter"?

    True when (a) the type noun carries a 1st-person possessive determiner ("my"/"our" — Person=1 ∧
    Poss=Yes), OR (b) it is governed (dobj / appos-chain / npadvmod) by a 1st-person POSSESSION clause
    ("I have a son Alex", "I own a motorcycle named Bolt", "I acquired a server named Atlas", "we have a
    daughter Robin" — the enumerated members all hang off the possession verb's object). Grammatical
    (morphology + a 1st-person personal-pronoun subject), with the POSSESSION-VERB decision delegated to
    GROWABLE cue-class metadata, NOT an in-code verb literal: the STATIVE-possession class
    (``_possession_verbs()`` — have/own/possess/keep/hold; ``have`` lives there too) UNIONED with the
    transfer-of-possession ACQUISITION class (``_acquisition_verbs()`` — got/bought/acquired/received).
    Both denote the speaker possessing the object, which is exactly the question this gate asks; each
    class grows on its own rail and the gate reads both. The clause does the GRAMMAR (the head-climb);
    metadata decides what the verb MEANS. Fail-safe → False (no possession edge minted; the type/name
    structure still stands)."""
    try:
        for c in type_tok.children:
            if (c.dep_ == "poss" and c.morph.get("Person") == ["1"]
                    and "Yes" in c.morph.get("Poss")):
                return True
        # climb to a governing POSSESSION verb whose subject is a 1st-person personal pronoun. The
        # possession-verb membership is GROWABLE METADATA (the possession_verb cue class ∪ the
        # acquisition_verb transfer class), NOT a hardcoded verb literal — grammar identifies WHICH verb
        # governs the clause; the cue classes decide whether that verb means "possesses/comes-to-possess".
        _poss_verbs = _possession_verbs() | _acquisition_verbs()
        cur = type_tok
        hops = 0
        while cur is not None and hops < 8:
            h = cur.head
            if h is None or h.i == cur.i:
                break
            if (h.lemma_ or "").strip().lower() in _poss_verbs and h.pos_ in ("VERB", "AUX"):
                subj = next((s for s in h.children if s.dep_ in ("nsubj", "nsubjpass")), None)
                if subj is not None and _is_first_person_personal_pronoun(subj):
                    return True
                break
            cur = h
            hops += 1
    except Exception:  # noqa: BLE001
        return False
    return False


def analyze_name_type_bindings(text):
    r"""Deterministic reading of EVERY (ProperName ↔ common-noun Type) binding in ``text``, across ALL
    connectors (naming verb / apposition / copula). Returns ``list[NameTypeBinding]`` (possibly empty).

    THE ONE connector-agnostic detector (replaces the chain-per-construction sprawl): for each common-
    noun TYPE head, ``_bound_name_for_type`` recovers the PROPER NAME bound to it by ANY connector; then
    the age (a cardinal in the binding's span), the nickname ("goes by Jay"), the possessor (self via
    "my"/"I have"), and negation are read structurally. A comma-and enumeration ("a son Alex 19, a
    daughter Robin 10, a dog named Rex, a friend Sam") yields ONE binding per member — every
    construction binds, never just the first.

    ``text`` may be a ``str`` OR an already-parsed (and possibly GLiNER2-typed) spaCy ``Doc`` — the
    deriver passes its built Doc so the detector reads the SAME parse (and ``token.ent_type_``); a
    ``str`` is parsed internally. Subject-agnostic, GLiNER2-pure, metadata-driven. Makes NO entity-
    typing or final rel choice for non-kin domains; the caller maps roles → instance_of / also_known_as
    / kin-or-possession / scalar. Deterministic, fail-safe: parse miss / any failure → ``[]``."""
    if text is None:
        return []
    if isinstance(text, str):
        doc = _parse(text)
    else:
        doc = text  # already a parsed (typed) Doc
    if doc is None:
        return []
    out: list = []
    try:
        _naming = _naming_verbs()
        _seen: set = set()
        for type_tok in doc:
            if type_tok.pos_ != "NOUN":
                continue  # the TYPE is a common noun (son/dog/friend); a PROPN head is itself a name
            name_tok, connector = _bound_name_for_type(type_tok, _naming)
            if name_tok is None:
                continue
            type_noun = _np_phrase(type_tok)
            # FULL PROPER-NAME SPAN (truncation fix): a multi-token name ("David Chen", "John Smith")
            # parses as name_tok = the head PROPN ("Chen"/"Smith") with the given name as a left PROPN
            # ``compound`` child ("David"/"John"). Taking only ``name_tok.text`` dropped the given name
            # ("my son David Chen" → "Chen"). Rebuild the contiguous proper-name span = left PROPN
            # compound modifiers + the head, PRESERVING surface case (the registry lowercases at
            # ingest). A single-token name ("Rex") is unchanged. Structural (PROPN compound),
            # subject-agnostic, NO name word list. Fail-safe → the bare head surface.
            try:
                _name_mods = [c for c in name_tok.children
                              if c.dep_ == "compound" and c.pos_ == "PROPN" and c.i < name_tok.i]
                name = " ".join(
                    [m.text for m in sorted(_name_mods, key=lambda m: m.i)]
                    + [(name_tok.text or "")]).strip()
            except Exception:  # noqa: BLE001 — fail-safe
                name = (name_tok.text or "").strip()
            if not type_noun or not name or name.lower() == type_noun:
                continue
            _key = (name.lower(), type_noun)
            if _key in _seen:
                continue
            _seen.add(_key)
            negated = (any(c.dep_ == "neg" for c in type_tok.children)
                       or (type_tok.head is not None
                           and any(c.dep_ == "neg" for c in type_tok.head.children)))
            out.append(NameTypeBinding(
                name=name,
                type_noun=type_noun,
                connector=connector,
                age=_binding_age_string(name_tok, type_tok),
                nickname=_binding_nickname(name_tok, type_tok),
                possessor_is_self=_type_is_self_possessed(type_tok),
                negated=bool(negated),
            ))
    except Exception as e:  # noqa: BLE001 — fail-safe
        log.warning("linguistics.analyze_name_type_bindings_failed", error=str(e)[:160])
        return []
    return out


# ── SVO MERGE GRAMMAR — the assembly-line backbone (GLiNER2 entity + spaCy verb + self-ref) ──
# THE WHY (the diagnosed capture wall, 1/10): the three good components run SEPARATELY and never
# MERGE. GLiNER2 cleanly TYPES the object entity ("Samsung Galaxy S22"); spaCy cleanly parses the
# verb+subject ("I got …"); the temporal lane cleanly parses the date ("on Feb 20, 2023"). But the
# verb-lift (``predicate_span.lift_edges_from_entities``) needs a GLiNER2 entity *PAIR* — and the
# Samsung turn has only ONE entity plus a first-person subject ("I"), so it bails (``len<2``) and the
# clean components are thrown away. This grammar core supplies the SUBJECT + PREDICATE + OBJECT-HEAD
# spaCy can see, so the caller's MERGE can host the GLiNER2-typed entity as the OBJECT (never a
# verb-lift-invented noun) and the spaCy DATE as the per-edge event_date. The subject is the
# first-person speaker → the user (the self-ref binding), or a resolved nominal entity.
#
# Subject-agnostic + dependency-driven (NO verb word-list, NO noun word-list). The ONLY closed sets
# consulted are universal grammatical primitives already used elsewhere in this module: the function-
# word POS set (``_FUNCTION_POS``) and the copula lemma "be" (so a copula self-predication is left to
# ``analyze_copula``, not double-captured here). This returns PURE GRAMMAR FACTS — it makes NO
# rel-type, entity-typing, or routing decision; the predicate is the user's own verb (lemma +
# load-bearing particle/preposition only) and the caller composes/gates/disposes the merged edge.


@dataclass(frozen=True)
class SVORelation:
    """A deterministic Subject–Verb–Object reading for the merge brain.

    - ``subject_text``    : the surface subject token/phrase, lowercased ("i", "we", "ada").
    - ``subject_is_self`` : True ONLY for a genuine 1st-person personal pronoun ("I"/"we" — the
                            self-ref binding to the user). A nominal/proper subject → False (the
                            caller resolves it as an entity, or hosts a GLiNER2 entity in its place).
    - ``predicate``       : the relation candidate built from the user's OWN verb — the verb lemma
                            plus an immediately-following load-bearing particle/preposition
                            ("got"→"get", "went to"→"go_to", "moved to"→"move_to"). snake_cased.
                            NEVER folds the object noun or a scalar tail into the token.
    - ``object_text``     : the surface object head NOUN/PROPN phrase, lowercased ("samsung galaxy
                            s22", "concert"). The caller PREFERS a GLiNER2-typed entity overlapping
                            this span as the edge object; this is the grammar's fallback object.
    - ``object_char_start``/``object_char_end`` : char offsets of the object phrase in the SOURCE
                            text, so the caller can match it to a GLiNER2 entity span (positional
                            overlap), not by fragile string compare.
    - ``negated``         : True when a ``neg`` dependency hangs off the verb ("I didn't buy …") —
                            the caller SKIPS a negated SVO (absence modeling deferred).
    """
    subject_text: str
    subject_is_self: bool
    predicate: str
    object_text: str
    object_char_start: int
    object_char_end: int
    negated: bool


# Particles/prepositions that are LOAD-BEARING on a verb (change the relation: "go" vs "go to",
# "move" vs "move to", "work" vs "work for"). Kept on the predicate token; everything else after the
# verb is the object/scalar tail and is NOT folded in. Closed grammatical class (ADP/PART surface
# forms), aligned with predicate_span._KEEP_PREPOSITIONS — a language primitive, NOT a domain list.
#
# DB-HELD + per-tenant + GROWABLE (migration 108 / linguistic_cue_overlay, category='svo_particle').
# The frozenset below is the RETIRED-as-authority in-code list, KEPT as the DB-DOWN CODE-FALLBACK seed:
# `_svo_keep_particles()` resolves the live set from `<tenant>.linguistic_cues` (seed-copied ∪ grown)
# via the SAME per-tenant overlay the naming/LVC/temporal layers use, falling back to THIS frozenset
# only when the overlay is unavailable/unbound (fail-safe). Membership checks below call
# `_svo_keep_particles()`, NOT this frozenset directly.
_SVO_KEEP_PARTICLES: frozenset[str] = frozenset(
    {"to", "for", "with", "in", "on", "at", "from", "into", "about", "of"}
)


def _svo_keep_particles() -> frozenset[str]:
    """Resolve the per-tenant ACTIVE load-bearing SVO-particle set via the overlay (ContextVar-bound to
    the request's tenant schema — the SAME binding the naming/LVC/rel_type/temporal overlays use).
    Returns a frozenset of lowercased particle/preposition surface forms. Fail-safe: any import/read
    failure / unbound schema / empty resolution → the in-code ``_SVO_KEEP_PARTICLES`` code-fallback seed
    so a DB-down / pre-migration / unwarmed-overlay turn keeps load-bearing particles on the predicate
    instead of dropping the relation distinction. Never empty. Mirrors ``_naming_verbs()`` exactly."""
    try:
        from src.api import linguistic_cue_overlay  # deferred: avoid import cycle / hard dep
        dsn = os.environ.get("POSTGRES_DSN", "")
        cues = linguistic_cue_overlay.resolve_svo_particles(dsn)
        if cues:
            return cues
        return _SVO_KEEP_PARTICLES  # empty resolution → code-fallback (never lose detection)
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the linguistic layer
        log.warning("linguistics.svo_keep_particles_resolve_failed", error=str(e)[:160])
        return _SVO_KEEP_PARTICLES


def _svo_predicate_token(verb_tok, exclude_idx=None, include_agent=False) -> str | None:
    """Build the predicate token from a verb token: the verb LEMMA + one load-bearing particle/prep.

    "got" → "get"; "went to" (go + prt/prep "to") → "go_to"; "moved into" → "move_into". The object
    noun and any scalar tail are NEVER folded in (strong ingest: the relation is the user's verb, the
    object is the GLiNER2 entity). Returns the snake_cased token or ``None`` when the verb is a bare
    copula/auxiliary/function word (no relational content). Deterministic; no word-list beyond the
    universal function-word POS set + the load-bearing-particle grammatical class.

    A ``prep`` is LOAD-BEARING (kept on the predicate) ONLY when the verb has NO direct object — i.e.
    the prepositional object IS the verb's object ("went TO a concert", "looked FOR it" → ``go_to`` /
    ``look_for``). When the verb already governs a dobj/attr/oprd ("got a phone FROM Best Buy"), the
    prep introduces a CIRCUMSTANTIAL ADJUNCT (source / locative / temporal) that is NOT part of the
    relation and must NOT fold — that is the compositional peel-out principle in grammar form: the
    adjunct PP is a separate component, never glued into the verb. A phrasal PARTICLE (``prt``: "give
    UP", "pick UP") is ALWAYS load-bearing (it is the verb, not an adjunct), regardless of a dobj.

    ``exclude_idx`` (optional set of token ``.i``): tokens the caller PEELED as a date component
    (the compositional peel in token form — PART 1). A prep whose head OR whose pobj falls in this set
    is a TEMPORAL adjunct ("see ON march 1st") and is NEVER folded, regardless of how the atomizer
    reworded the clause; a date-span token is likewise not counted toward ``has_direct_object``.
    Default ``None`` → byte-identical to today (every existing caller unchanged)."""
    try:
        lemma = (verb_tok.lemma_ or verb_tok.text or "").strip().lower()
        if not lemma:
            return None
        # A bare copula "be" is a self-predication (analyze_copula owns it) — not an SVO relation.
        if lemma == "be":
            return None
        _excl = exclude_idx or ()

        def _in_date(_t):  # a token peeled as part of a date span → not relational content
            return _t is not None and _t.i in _excl

        _particles = _svo_keep_particles()  # per-tenant grown set (overlay) ∪ code-fallback; once
        parts = [lemma]
        # Does the verb already govern a DIRECT object (dobj/obj) or a linking complement (attr/oprd)?
        # If so, any prepositional child is a circumstantial ADJUNCT, not load-bearing → never folded.
        # A date-span token (exclude_idx) is NOT a real object — never count it as the direct object.
        has_direct_object = any(
            c.dep_ in ("dobj", "obj", "attr", "oprd") and c.pos_ in ("NOUN", "PROPN")
            and not _in_date(c)
            for c in verb_tok.children
        )
        # One immediately-following load-bearing particle/preposition that the verb governs.
        #   • prt  (phrasal particle: "go up", "pick up") → ALWAYS load-bearing (part of the verb).
        #   • prep (prepositional complement head)        → load-bearing ONLY when there is no direct
        #                                                   object (the pobj IS the object: "go to X").
        # Take the FIRST qualifying child only.
        for c in verb_tok.children:
            surf = (c.text or "").strip().lower()
            if surf not in _particles:
                continue
            # PEELED-DATE GUARD (PART 1): a prep that introduces a date span ("on" governing "march 1st",
            # or a prep token that IS inside the date span) is a temporal adjunct — never fold it onto
            # the predicate, so the atomizer's ``see_on`` never survives.
            if _in_date(c) or any(gc.dep_ == "pobj" and _in_date(gc) for gc in c.children):
                continue
            if c.dep_ == "prt":
                parts.append((c.lemma_ or c.text or "").strip().lower())
                break
            if c.dep_ == "prep" and not has_direct_object:
                parts.append((c.lemma_ or c.text or "").strip().lower())
                break
        # PASSIVE AGENT BY-PHRASE (``include_agent``, subordinate-predicate path only): "was cited BY
        # X" folds "_by" so the demoted-subject agent becomes the relation's object ("cited_by"). The
        # agent by-phrase carries dep_=="agent" (NOT prep), so it is invisible to the particle loop; we
        # fold it ONLY when no particle already folded AND the verb governs no direct object (the agent
        # IS the object). Grammatical (dep_), subject-agnostic, gated OFF by default → every existing
        # caller byte-identical. A date-span "by" is never an agent (excluded).
        if include_agent and len(parts) == 1 and not has_direct_object:
            for c in verb_tok.children:
                if c.dep_ == "agent" and not _in_date(c):
                    parts.append("by")
                    break
        token = "_".join(p for p in parts if p)
        # Reject a pure function-word predicate (e.g. the verb tagged as an aux only).
        if not token or token in _particles:
            return None
        return token
    except Exception:  # noqa: BLE001 — fail-safe
        return None


def _load_bearing_prep_of(verb_tok, exclude_idx=None) -> str | None:
    """The lowercased LOAD-BEARING preposition a verb governs, or ``None``.

    The preposition is what disambiguates two distinct relations sharing one verb LEMMA: "work
    WITH X" (collaboration) ≠ "work FOR X" (employment) ≠ "work AT X". This returns the SAME
    load-bearing prep ``_svo_predicate_token`` folds onto the predicate, so the two stay in lock-
    step: a ``prt`` phrasal particle (always part of the verb) OR a ``prep`` whose pobj IS the
    verb's object (no separate direct object → the prep is load-bearing, not a circumstantial
    adjunct). Returns ``None`` when the verb governs no load-bearing prep (bare transitive /
    direct-object verb). Decided purely from spaCy ``dep_``/``pos_`` + the grammatical particle
    class — NO verb/rel word-list. Subject-agnostic, fail-safe (undecidable → None).

    ``exclude_idx`` (optional set of token ``.i``) — PEELED date tokens (PART 1): a prep that
    introduces a date span is a temporal adjunct, never the load-bearing prep. Kept in lock-step
    with ``_svo_predicate_token``. Default ``None`` → today's behavior, every existing caller
    unchanged."""
    if verb_tok is None:
        return None
    try:
        _excl = exclude_idx or ()

        def _in_date(_t):
            return _t is not None and _t.i in _excl

        _particles = _svo_keep_particles()
        has_direct_object = any(
            c.dep_ in ("dobj", "obj", "attr", "oprd") and c.pos_ in ("NOUN", "PROPN")
            and not _in_date(c)
            for c in verb_tok.children
        )
        for c in verb_tok.children:
            surf = (c.text or "").strip().lower()
            if surf not in _particles:
                continue
            if _in_date(c) or any(gc.dep_ == "pobj" and _in_date(gc) for gc in c.children):
                continue  # temporal-adjunct prep — not the disambiguating load-bearing prep
            if c.dep_ == "prt":
                return (c.lemma_ or c.text or "").strip().lower() or None
            if c.dep_ == "prep" and not has_direct_object:
                return (c.lemma_ or c.text or "").strip().lower() or None
    except Exception:  # noqa: BLE001 — fail-safe: undecidable → no governing prep
        return None
    return None


def _norm_rel_identity(surface: str) -> str:
    """Normalize a rel/predicate surface to its RUNG-1 canonical identity key using the SAME
    morphology the codebase's canonical resolver uses (``ontology.canonical.normalize_rel`` — PURE,
    no DB/LLM/cosine): lemmatize the head verb + keep load-bearing preps ("lives_in"→"live_in",
    "works_for"→"work_for", "manages"→"manage"). Deferred import (avoid a cycle); fail-safe:
    any import/normalize failure → the lowered surface unchanged."""
    try:
        from src.ontology.canonical import normalize_rel as _normalize_rel  # deferred: avoid cycle
        return _normalize_rel(surface or "") or (surface or "").strip().lower()
    except Exception:  # noqa: BLE001 — fail-safe
        return (surface or "").strip().lower()


# ── RESIDENCE-PREDICATE IDENTITIES (composite-address bridge, DEV/DESIGN-address-composite.md) ──
# The normalized-identity set of LOCATION-category, MUTABLE residence rel_types (lives_in / lives_at /
# located_in / located_at) — the predicates a residence/address clause folds. Resolved PRIMARY from the
# per-tenant rel_types overlay (a tenant-grown location rel is picked up for free) and UNIONed with a
# canonical code-fallback so a DB-down / unwarmed-overlay / str-input turn still bridges. ``born_in`` is
# category='location' but correction_behavior='immutable' (a birthplace never MOVES) → EXCLUDED here, so
# "I was born at X in Riverton" is never mistaken for a residence. Metadata-driven, subject-agnostic;
# mirrors ``_svo_keep_particles()`` (DB-resolve ∪ code-fallback, never empty). These are RELATION
# IDENTITIES (like ``_STATE_REL``), not a domain word zoo — the residence VERBS (live/reside/dwell) are a
# small closed English class; the fuller build grows a ``residence_verb`` cue class on the same rail.
_RESIDENCE_REL_FALLBACK = frozenset({
    "live_in", "live_at", "reside_in", "reside_at", "dwell_in", "dwell_at", "locate_at",
})


def _residence_predicate_identities() -> frozenset:
    """LOCATION-category, MUTABLE residence-predicate identities (tenant overlay ∪ code-fallback).

    Fail-safe: any import/read failure / unbound schema → the code-fallback seed (never empty), so a
    residence clause still bridges when the overlay is cold. ``born_in`` (immutable) is excluded."""
    try:
        from src.api import rel_type_overlay  # deferred: avoid import cycle / hard dep
        dsn = os.environ.get("POSTGRES_DSN", "")
        meta = rel_type_overlay.resolve_current(dsn) if dsn else {}
        ids: set = set()
        for _rt, _m in (meta or {}).items():
            try:
                if (_m.get("category") == "location"
                        and (_m.get("correction_behavior") or "supersede") != "immutable"):
                    ids.add(_norm_rel_identity(_rt))
            except Exception:  # noqa: BLE001
                continue
        return frozenset(ids | set(_RESIDENCE_REL_FALLBACK))
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the linguistic layer
        log.warning("linguistics.residence_rels_resolve_failed", error=str(e)[:160])
        return _RESIDENCE_REL_FALLBACK


def _predicate_is_novel_to_ontology(predicate: str) -> bool:
    """Is ``predicate`` (an SVO verb-lemma predicate like ``affect`` / ``live_in`` / ``marry``) NOVEL to
    the per-tenant ontology — i.e. it resolves to NO seeded/known rel_type (RUNG 2 exact ∪ RUNG 3 alias ∪
    RUNG 3 seeded-morphology fold)? This is the SAME known-vs-novel test the verb-lift/growth path uses
    (``ontology.canonical.resolve_canonical`` + ``resolve_seeded_by_morphology``), so a verdict here is
    consistent with where the novel verb subsequently flows.

    Why this is the right discriminator: GLiNER2's relation scorer mints from the SEEDED menu. When the
    user's verb IS in the ontology (directly or by morphology — ``live_in``≡``lives_in``, ``meet``≡seeded
    ``met``), a different seeded mint is a legitimate ontology-level choice → let the mint WIN. When the
    verb is genuinely ABSENT from the ontology (``affect``), GLiNER2 was FORCED to substitute a wrong
    seeded rel (``manages``) → the deterministic content verb must win and flow to growth.

    Deterministic, NO cosine. Reads the ContextVar-bound tenant schema (the SAME binding the overlays
    use). FAIL-SAFE — returns ``False`` (→ "mint WINS", today's behavior) whenever novelty CANNOT be
    POSITIVELY confirmed: the ontology isn't loaded (DB down / empty rel_types), the predicate resolves
    to a known rel, or any error. So a novel verb only wins when the ontology is present AND lacks it."""
    try:
        from src.ontology import canonical as _canon  # deferred: avoid import cycle
        try:
            from src.api.rel_type_overlay import get_current_schema as _get_schema
            _schema = _get_schema()
        except Exception:  # noqa: BLE001
            _schema = None
        _dsn = os.environ.get("POSTGRES_DSN", "")
        # Ontology must be LOADED before a "novel" verdict is trustworthy — an empty set means the
        # DB/seed is unavailable, NOT that every verb is novel. Fail-safe → mint wins.
        if not _canon._load_reltypes(_dsn or None, _schema):
            return False
        if _canon.resolve_canonical(predicate, _dsn or None, _schema).get("canonical") is not None:
            return False  # RUNG 2/3 known rel — the mint is an ontology-level choice, let it win
        if _canon.resolve_seeded_by_morphology(predicate, _dsn or None, _schema) is not None:
            return False  # folds onto a seeded canonical (live_in→lives_in, meet→met) — known
        return True  # ontology present, predicate absent from it → genuinely novel
    except Exception:  # noqa: BLE001 — fail-safe → not-novel → mint wins (unchanged behavior)
        return False


def _content_verb_beats_mint(verb_tok, rel: str, minted: str) -> bool:
    """CONVERGENCE CARVE-OUT (sibling of the preposition guard): should the deriver's SVO CONTENT-VERB
    predicate WIN over a GLiNER2-minted rel for this pair (i.e. is the mint a FUZZY guess clobbering
    the user's own DETERMINISTIC verb)?

    Returns True — DROP the mint, keep the SVO predicate — ONLY when ALL hold (grammar/ontology-driven,
    subject-agnostic, NO rel/verb/domain literal):
      (a) there is a real verb token and it is a genuine CONTENT verb (``pos_ == "VERB"``, lemma ≠ "be"
          — an AUX/copula/verb-less chain has no user-verb to protect);
      (b) the verb is NOT a light/support verb (``_lvc_support_verbs`` — "have"/"take"/"get"/…): a light
          verb carries NO relational content and EXISTS to be replaced by a better seeded rel, so its
          mint ("I have a dog" → ``has_pet``) MUST still win — a KEY no-regression discriminator;
      (c) the emitted ``rel`` genuinely DERIVES from this verb — its normalized HEAD equals the verb
          lemma's ("affect"/"live_in"/"work_at" head == the verb). This limits the carve-out to a raw
          SVO verb-lemma gap-fill and leaves any CANONICAL rel a chain deliberately chose (state
          ``has_state``, possessive ``owns``, …) to converge normally;
      (d) the SVO predicate is NOVEL to the ontology (``_predicate_is_novel_to_ontology``). A predicate
          that resolves to a known/seeded rel — an inflection/canonical variant ("live_in"≡``lives_in``,
          "work_for"≡``works_for``) OR a different seeded rel the verb is a known member of ("meet"≡seeded
          ``met``) — lets the mint WIN (return False). Only a genuinely-novel verb ("affect") whose forced
          fuzzy mint ("manages") is a fabrication loses to the deterministic content verb.

    Fail-safe: any miss / undecidable → False (today's "minted WINS" behavior is unchanged)."""
    try:
        if verb_tok is None:
            return False
        if verb_tok.pos_ != "VERB":
            return False
        _lemma = (verb_tok.lemma_ or verb_tok.text or "").strip().lower()
        if not _lemma or _lemma == "be":
            return False
        if _lemma in _lvc_support_verbs():  # light/support verb → let the seeded mint replace it
            return False
        _rel_norm = _norm_rel_identity(rel)
        _verb_head = _norm_rel_identity(_lemma).split("_", 1)[0]
        if _rel_norm.split("_", 1)[0] != _verb_head:
            return False  # emitted rel is NOT this verb's own SVO predicate — do not interfere
        # Known/seeded predicate → the mint is an ontology-level choice, let it win; genuinely novel
        # verb → its forced fuzzy mint is a fabrication, the deterministic content verb wins + grows.
        return _predicate_is_novel_to_ontology(rel)
    except Exception:  # noqa: BLE001 — fail-safe → mint wins (unchanged behavior)
        return False


def _object_candidate_is_temporal(tok) -> bool:
    """True if an object candidate token is a DATE/TIME entity — the temporal lane owns it, not the graph.

    A date/relative-time is NEVER a relationship object (CLAUDE.md: it is an ``event_date`` scalar). Two
    object-selection branches leak a BARE weekday/month as a relational object because the deriver's own
    date-span peel (``_collect_date_spans`` → ``_date_token_idx``) enrolls only a prep-/numeric-anchored
    span and MISSES a bare weekday, so ``exclude_idx`` never covers it:
      • the capitalized-advmod object recovery ("crashed Monday" → "Monday" as ``advmod``/``npadvmod``);
      • the prepositional-object branch ("released on Tuesday" → "Tuesday" as ``pobj`` of "on", which
        would otherwise fold "on" into a ``release_on`` predicate and bind the weekday as the object).
    We gate BOTH on the SAME spaCy DATE NER the temporal layer uses (``_get_nlp_ner``) — deterministic,
    subject-agnostic, NO weekday/month literal list. The parser-only deriver Doc carries no ``ent_type_``,
    so we consult the shared NER singleton on the token's sentence and test whether the token falls inside
    a DATE/TIME span. GROUNDING: spaCy EntityRecognizer ``ent.label_ == "DATE"`` (spaCy NER / OntoNotes
    label scheme). Fail-safe → False (unavailable NER → keep the capture; a rare date-as-object is a
    lesser evil than dropping a real OOV object — "we don't forget")."""
    try:
        if (getattr(tok, "ent_type_", "") or "").upper() in ("DATE", "TIME"):
            return True
        _ner = _get_nlp_ner()
        if _ner is None:
            return False
        _doc = _ner(tok.doc.text)
        for _e in _doc.ents:
            if _e.label_ in ("DATE", "TIME") and _e.start_char <= tok.idx < _e.end_char:
                return True
    except Exception:  # noqa: BLE001 — fail-safe: an NER hiccup never drops the capture
        return False
    return False


def _svo_object_head(verb_tok, exclude_idx=None, include_agent=False):
    """Return the OBJECT head noun token a verb governs, or ``None``.

    Direct object ("got a phone" → dobj "phone"), or the prepositional object of a load-bearing
    preposition the verb governs ("went to a concert" → pobj "concert" under prep "to"). PROPN or
    NOUN only — a pronoun/clause object is not a mergeable entity. Subject-agnostic, structural.

    ``exclude_idx`` (optional set of token ``.i``) — PEELED date tokens (PART 1): a token inside a
    resolved date span is NEVER returned as the object, so the atomizer's date-as-dobj ("see march
    1st") never yields "march 1st"/"march" as the relationship object. Default ``None`` → today's
    behavior, every existing caller unchanged."""
    try:
        _excl = exclude_idx or ()

        def _ok(_t):  # a candidate object token that is NOT part of a peeled date span
            return _t is not None and _t.i not in _excl

        _particles = _svo_keep_particles()  # per-tenant grown set (overlay) ∪ code-fallback
        # Direct object first.
        for c in verb_tok.children:
            if c.dep_ in ("dobj", "obj") and c.pos_ in ("NOUN", "PROPN") and _ok(c):
                return c
        # Prepositional object of a load-bearing preposition (the prep itself must not be a date span).
        for c in verb_tok.children:
            if c.dep_ == "prep" and (c.text or "").strip().lower() in _particles and _ok(c):
                for gc in c.children:
                    if gc.dep_ == "pobj" and gc.pos_ in ("NOUN", "PROPN") and _ok(gc) \
                            and not _object_candidate_is_temporal(gc):
                        return gc
        # Attribute complement of a non-copula linking verb ("became a manager").
        for c in verb_tok.children:
            if c.dep_ in ("attr", "oprd") and c.pos_ in ("NOUN", "PROPN") and _ok(c):
                return c
        # PASSIVE AGENT (``include_agent``): the pobj of a passive "by"-phrase (dep_=="agent") IS the
        # relation's object ("was cited BY three later cases" → "cases"). Gated OFF by default so every
        # existing caller is byte-identical. Grammatical (dep_), subject-agnostic.
        if include_agent:
            for c in verb_tok.children:
                if c.dep_ == "agent" and _ok(c):
                    for gc in c.children:
                        if gc.dep_ == "pobj" and gc.pos_ in ("NOUN", "PROPN") and _ok(gc):
                            return gc
        # OOV PROPER-NOUN OBJECT MIS-PARSE (last resort — reached ONLY when no genuine object matched
        # above). On a SHORT clause with an out-of-vocabulary proper-noun object the parser routinely
        # DEMOTES the object to a non-object dependency and mis-POS-tags it: "The vulnerability affects
        # Android." parses "Android" as ``advmod`` (not ``dobj``); "affects iOS" parses "iOS" as a PRON
        # ``advmod``. There is no genuine adverb in object position — a CAPITALISED (proper-noun
        # orthography) post-verbal token the parser could not attach as the object IS the direct object.
        # We recover it by SHAPE (an uppercase letter = proper-noun form) so an OOV name is never dropped
        # and the objectless-state chain (which gates on this function returning None) does not mis-fire a
        # junk ``has_state`` on it. Grammar/orthography-driven, subject-agnostic, NO name list: a genuine
        # adverb ("crashed yesterday") is lowercase → untouched, and a date span is excluded via
        # ``exclude_idx`` before we get here. GROUNDING: spaCy ``token.dep_``/``pos_`` (UD/ClearNLP
        # labels, spaCy glossary) — capture no longer depends on GLiNER2 typing the span.
        for c in verb_tok.children:
            if c.dep_ in ("advmod", "npadvmod", "nmod", "dep") and _ok(c) \
                    and c.pos_ not in ("ADV", "PART", "ADP", "SCONJ", "CCONJ", "DET",
                                       "PUNCT", "AUX", "SPACE", "SYM") \
                    and (c.pos_ == "PROPN" or any(ch.isupper() for ch in (c.text or ""))) \
                    and not _object_candidate_is_temporal(c):
                return c
    except Exception:  # noqa: BLE001 — fail-safe
        return None
    return None


def _svo_object_pronoun(verb_tok, exclude_idx=None):
    """The 3rd-person PERSONAL-PRONOUN object a verb governs (``dobj``/``obj``, or the ``pobj`` of a
    load-bearing preposition), or ``None`` — the pronoun COMPANION to ``_svo_object_head``.

    ``_svo_object_head`` deliberately returns only NOUN/PROPN objects ("a pronoun/clause object is not
    a mergeable entity"), which DROPS an object-pronoun clause entirely: "I started working with HER on
    2/15" yields no object → no edge → the date on that clause has nothing to bind to. This surfaces the
    object PRONOUN so the caller can resolve it by COREFERENCE to a named person (nearest preceding
    PROPN / prior-atom NP) BEFORE emitting — grounding "work_with her" to (user, work_with, rachel).

    Grammar/morphology only (``_is_third_person_pronoun`` — Person=3, PronType=Prs), subject-agnostic,
    NO token list. ``exclude_idx`` skips peeled date-span tokens (parity with ``_svo_object_head``).
    Fail-safe: any miss/parse error → ``None`` (the caller keeps today's drop, never a guessed name)."""
    try:
        _excl = exclude_idx or ()

        def _ok(_t):
            return _t is not None and _t.i not in _excl and _is_third_person_pronoun(_t)

        # Direct object pronoun ("she helped HER") first.
        for c in verb_tok.children:
            if c.dep_ in ("dobj", "obj") and _ok(c):
                return c
        # Prepositional object pronoun of a load-bearing preposition ("working WITH her").
        _particles = _svo_keep_particles()
        for c in verb_tok.children:
            if c.dep_ == "prep" and (c.text or "").strip().lower() in _particles:
                for gc in c.children:
                    if gc.dep_ == "pobj" and _ok(gc):
                        return gc
    except Exception:  # noqa: BLE001 — fail-safe
        return None
    return None


def _carried_subject_token(verb_tok):
    r"""The COREFERENCE-CARRIED subject token for a SUBORDINATE / COORDINATED predicate that lacks its
    OWN grammatical subject — the deterministic key to DENSE multi-predicate decomposition.

    GROUNDING: walks spaCy dependency labels (acl/advcl/conj/relcl per the ClearNLP/UD scheme in spaCy
    glossary.py). See DEV/DESIGN-ingest-hardening-grounding.md.

    A dense sentence packs several predicates about ONE subject into subordinate/coordinated clauses
    that spaCy leaves subject-LESS (their subject is shared by coordination or supplied by the noun
    they modify):

        "CVE… is a vulnerability …, attributed to X, targeting Y, patched on <date>."
            attributed(acl of "vulnerability") · targeting(conj) · patched(conj) — none has an nsubj.
        "The patient … presented with pain, was diagnosed …, and was prescribed <drug> on <date>."
            presented(acl of "patient") · prescribed(conj of the ROOT) — subjectless.
        "Smith v. Jones, decided …, overruled Baker, established …, and was cited by …."
            overruled(advcl) · established(conj) · cited(conj) — subjectless.

    The SVO / intransitive chains require a direct ``nsubj``/``nsubjpass`` child, so today they SKIP
    every one of these and the sentence UNDER-DECOMPOSES. This resolver supplies the carried subject
    so those SAME chains (and their verb-lift / date / object machinery) fire per subordinate predicate
    — no parallel extractor.

    Returns the subject TOKEN, or ``None``. Resolution (grammar/dep_ only, subject-agnostic, NO
    literal):
      • ONLY for a verb that has NO ``nsubj``/``nsubjpass`` of its own AND whose dep_ is a
        subordinate/coordinated predicate arc (``conj``/``acl``/``advcl``/``acl:relcl``/``relcl``);
      • REJECT an infinitival / irrealis form (``xcomp``, or a ``to`` ``aux``/``mark`` child, or
        ``VerbForm=Inf``) — a purpose/control clause ("went to the store TO buy milk") is not an
        asserted predicate about the subject;
      • climb the head chain: the FIRST ancestor verb carrying an explicit ``nsubj``/``nsubjpass``
        supplies it (coordination — the shared subject); a modified NOUN reached via ``acl``/``advcl``
        IS the subject, unless that noun is a copula complement (``attr``/``oprd`` of ``be``), in which
        case the copula's own subject is used (so "… is a vulnerability, attributed to X" carries the
        CVE, not "vulnerability").
    Fail-safe: any miss / parse error → ``None`` (the caller keeps today's skip)."""
    try:
        if verb_tok is None or verb_tok.pos_ != "VERB":
            return None
        if any(c.dep_ in ("nsubj", "nsubjpass") for c in verb_tok.children):
            return None  # has its own subject — the normal chain handles it
        if verb_tok.dep_ not in ("conj", "acl", "advcl", "acl:relcl", "relcl"):
            return None
        # Irrealis / infinitival guard — a purpose/control clause is not an asserted predicate.
        try:
            if "Inf" in verb_tok.morph.get("VerbForm"):
                return None
        except Exception:  # noqa: BLE001
            pass
        if any(c.dep_ in ("aux", "mark") and (c.lemma_ or c.text or "").strip().lower() == "to"
               for c in verb_tok.children):
            return None
        cur = verb_tok
        hops = 0
        while cur is not None and hops < 8:
            parent = cur.head
            if parent is None or parent.i == cur.i:
                break
            # (A) a coordinated / clausal-head verb carrying an explicit subject → the shared subject.
            for c in parent.children:
                if c.dep_ in ("nsubj", "nsubjpass"):
                    return c
            # (B) a NOUN reached via acl/advcl/relcl IS the subject — unless it is a copula complement,
            #     in which case the copula's subject is the real subject.
            if cur.dep_ in ("acl", "advcl", "acl:relcl", "relcl") and parent.pos_ in ("NOUN", "PROPN"):
                if parent.dep_ in ("attr", "oprd") and parent.head is not None \
                        and (parent.head.lemma_ or "").strip().lower() == "be":
                    for c in parent.head.children:
                        if c.dep_ in ("nsubj", "nsubjpass"):
                            return c
                return parent
            cur = parent
            hops += 1
    except Exception:  # noqa: BLE001 — fail-safe: undecidable → no carried subject
        return None
    return None


def _verb_has_clausal_complement(verb_tok) -> bool:
    """True when a verb governs a CLAUSAL complement — the structural signature of a CONTROL /
    MENTAL / RAISING verb ("want TO go", "think THAT…", "try TO…", "wonder…"), NOT a change-of-state.

    Decided purely from spaCy dependency labels — NO verb list:
      • a ``ccomp``/``xcomp`` child (finite or infinitival clausal complement), or
      • an infinitival ``to`` marker hanging off the verb (``aux``/``mark`` whose lemma is "to").
    Such a verb's "objectlessness" is illusory — its content lives in the embedded clause, so it is
    NEVER a self-contained state of the subject. Subject-agnostic, structural, fail-safe."""
    try:
        for c in verb_tok.children:
            if c.dep_ in ("ccomp", "xcomp"):
                return True
            # an infinitival "to" attached directly to this verb ("(I) want to") — the embedded
            # clause is the content, the matrix verb is a control verb.
            if c.dep_ in ("aux", "mark") and (c.lemma_ or c.text or "").strip().lower() == "to":
                return True
    except Exception:  # noqa: BLE001 — fail-safe: undecidable → not a clausal-complement verb
        return False
    return False


def _verb_realizes_resultant_state(verb_tok) -> bool:
    """True when an objectless verb realizes an EVENTIVE CHANGE-OF-STATE / resultant state — the
    structural diagnostic for "something HAPPENED to the thing" ("crashed", "broke", "(is) broken"),
    as opposed to an ongoing present-tense ACTIVITY ("(I) work", "(I) run").

    Resultative diagnostic, from spaCy features already used in this module — NO verb list:
      • a PAST-TENSE / PERFECTIVE / PAST-PARTICIPLE realization
        (``tag_ ∈ {VBN, VBD}`` OR ``morph`` Tense=Past OR Aspect=Perf) — "crashed", "broke",
        "(is) broken". A change-of-state is canonically realized perfectively/as a participle.
    A present-tense activity/mental verb ("work", "think", "run", "get") matches NONE and is
    therefore NOT a confirmed change-of-state (it routes to the short-term tier, captured-not-
    dropped). The copula-style ADJ resultant state ("the printer is idle") is owned by the dedicated
    ``_chain_copula_state`` chain, NOT folded here — a light verb + ``acomp`` ("get tired") would
    otherwise mis-identify the LIGHT VERB ("get") as the state node, which is junk. Subject-agnostic,
    structural, fail-safe (undecidable → False → short-term tier, never dropped)."""
    try:
        if (verb_tok.tag_ or "") in ("VBN", "VBD"):
            return True
        _morph = verb_tok.morph
        if "Past" in _morph.get("Tense") or "Perf" in _morph.get("Aspect"):
            return True
    except Exception:  # noqa: BLE001 — fail-safe: undecidable → not provably a resultant state
        return False
    return False


def _verb_is_present_simple(verb_tok) -> bool:
    """True when a verb is realized in the PRESENT SIMPLE (finite, non-perfective, non-progressive)
    — the STATIVE-possession frame of a light verb ("I HAVE a car", "I ATTEND a class"), as opposed
    to a realized PAST/perfective occurrence ("I HAD a meeting", "I ATTENDED a webinar") or an
    ongoing progressive ("I have been ATTENDING …").

    Grammar/morphology ONLY (spaCy ``tag_``/``morph`` — NO verb list): present-simple finite
    realization is ``tag_ ∈ {VBP, VBZ}``, or ``Tense=Pres`` with ``VerbForm=Fin`` and NO
    ``Aspect=Perf/Prog``. A present-simple LIGHT verb governing a concrete direct object is
    overwhelmingly STATIVE POSSESSION, not an eventive occurrence — the discriminator the LVC
    direct-object lane uses to avoid minting a bogus ``(user, participated_in, car)`` on "have a car"
    while keeping every DATED or realized-PAST occurrence. GROUNDING: spaCy ``tag_`` (Penn Treebank
    VBP/VBZ = non-3rd/3rd-person present) + Universal-Features Tense/Aspect/VerbForm (spaCy
    glossary/UD). Subject-agnostic, fail-safe (undecidable → False → today's behavior: the occurrence
    is KEPT — never drop a genuine occurrence on a parse miss)."""
    try:
        if (verb_tok.tag_ or "") in ("VBP", "VBZ"):
            return True
        _m = verb_tok.morph
        if ("Pres" in _m.get("Tense") and "Fin" in _m.get("VerbForm")
                and "Perf" not in _m.get("Aspect") and "Prog" not in _m.get("Aspect")):
            return True
    except Exception:  # noqa: BLE001 — fail-safe: undecidable → not present-simple (keep occurrence)
        return False
    return False


def _aspectual_activity_xcomp(verb_tok):
    r"""Return the PROGRESSIVE ``-ing`` ``xcomp`` VERB to DESCEND into for an aspectual subject-control
    matrix ("I STARTED working with Rachel", "I KEPT emailing Tom"), or ``None``.

    THE GAP THIS CLOSES (SVO subject/object split across an aspectual aux): an ASPECTUAL / phase verb
    ("start"/"begin"/"continue"/"keep"/"resume") RAISES the subject ("I") to the matrix and leaves the
    activity verb ("working"/"emailing") as an ``xcomp`` that carries the OBJECT ("Rachel"/"Tom"). The
    flat SVO lanes require subject AND object on the SAME verb, so the matrix has a subject but no
    object and the xcomp has the object but no subject → the engine derives ``work_with`` on the xcomp
    and then DISCARDS it for lack of a co-located subject. The fix: when this shape holds, descend into
    the xcomp and run the SAME SVO recovery there, using the MATRIX subject as the xcomp's subject — so
    "I started working with Rachel" minds (user, work_with, rachel) via the identical machinery that
    already nails "I work with Rachel".

    THE GATE (deterministic grammar; NO new hardcoded verb list — reuses EXISTING discriminators):
      • the matrix lemma is in the bounded, DB-grown ASPECTUAL / phase class
        ``_aspectual_control_verbs()`` (the ingressive + continuative + terminative phase rail —
        start/begin/continue/keep/resume/finish/stop). This is DELIBERATELY DISTINCT from
        ``_inchoative_verbs()`` (which feeds ``analyze_inchoative``'s NOUN-object occurrence and must
        NOT gain keep/continue, else "I kept the receipt" mis-mints an occurrence); AND
      • the matrix governs an ``xcomp`` whose pos_ is VERB and whose realization is PROGRESSIVE ``-ing``
        (``tag_ == 'VBG'`` OR morph ``Aspect=Prog``/``VerbForm=Part``) — the structural signature of a
        REALIZED progressive activity; AND
      • that xcomp is NOT infinitival (no ``to``/``TO`` ``aux``/``mark`` marker on it) — an infinitival
        complement ("started TO think", "want TO buy", "plan TO visit") is UNREALIZED INTENT, never an
        activity-on-a-thing, so it is rejected here (and the matrix would in any case carry an
        infinitival ``to`` → ``_verb_has_clausal_complement``); AND
      • the matrix lemma is NOT a CATENATIVE / MENTAL-STATE verb (``predicate_span._CATENATIVE`` /
        ``_MENTAL_STATE``) — intent/opinion/cognition verbs ("I considered hiring Sarah", "I like
        working with Rachel") take an ``-ing`` complement too but predicate an UNREALIZED desire /
        habitual preference, NOT a stated occurrence; reusing those existing closed sets is the
        belt-and-suspenders INTENT firewall ("user is truth": an intention is not a thing the user did).

    The caller (the SVO lanes) takes the returned xcomp as the new SVO head and the MATRIX nsubj as its
    subject; ``_svo_predicate_token`` / ``_svo_object_head`` then run unchanged on the xcomp. A negated
    aspectual ("I didn't keep emailing Tom") is left to the caller's own ``neg`` skip. Subject-agnostic,
    structural, fail-safe → ``None`` (no descent → the lanes keep today's behaviour, never a crash)."""
    try:
        matrix_lemma = (verb_tok.lemma_ or verb_tok.text or "").strip().lower()
        if not matrix_lemma:
            return None
        # POSITIVE ADMIT: the matrix must be an aspectual/phase verb (DB-grown ingressive + continuative
        # + terminative rail). Rides the SAME cue-rail machinery as the inchoative/LVC/naming sets — a
        # distinct aspectual category, NOT a fresh hardcoded list and NOT the inchoative set (kept apart
        # so adding keep/continue here never broadens ``analyze_inchoative``'s NOUN-object occurrence).
        if matrix_lemma not in _aspectual_control_verbs():
            return None
        # INTENT FIREWALL: reject catenative/mental-state matrices (they also take an -ing complement,
        # but predicate an unrealized desire/opinion, not an occurrence). Reuse the EXISTING closed sets.
        try:
            from src.extraction.predicate_span import _CATENATIVE, _MENTAL_STATE
            if matrix_lemma in _CATENATIVE or matrix_lemma in _MENTAL_STATE:
                return None
        except Exception:  # noqa: BLE001 — fail-safe: discriminator unavailable → fall through to the
            pass          #                  structural -ing/no-infinitival test below (still safe).
        for c in verb_tok.children:
            if c.dep_ != "xcomp" or c.pos_ != "VERB":
                continue
            # An infinitival complement ("to think") is UNREALIZED INTENT → never an activity. Reject
            # any xcomp carrying an infinitival ``to`` marker (aux/mark), regardless of its -ing tag.
            if any(
                g.dep_ in ("aux", "mark") and (g.lemma_ or g.text or "").strip().lower() == "to"
                for g in c.children
            ):
                continue
            # REALIZED PROGRESSIVE activity: -ing (VBG / Aspect=Prog / VerbForm=Part). Structural, no
            # verb list — the SAME morphology test ``_verb_realizes_resultant_state`` reads elsewhere.
            is_progressive = (c.tag_ or "") == "VBG"
            if not is_progressive:
                try:
                    _m = c.morph
                    is_progressive = ("Prog" in _m.get("Aspect")) or ("Part" in _m.get("VerbForm"))
                except Exception:  # noqa: BLE001 — fail-safe
                    is_progressive = False
            if is_progressive:
                return c
    except Exception:  # noqa: BLE001 — fail-safe: undecidable → no descent
        return None
    return None


def analyze_svo_relations(text: str) -> list:
    r"""Deterministic SVO grammar first-cut for the MERGE brain. Returns ``list[SVORelation]``.

    THE RULE (subject-agnostic, dependency-driven — NO verb/noun word-list): for each VERB that is
    NOT a copula/light-verb-event/naming construction (those have their OWN seams), recover its
    SUBJECT (``nsubj``/``nsubjpass``), its PREDICATE (verb lemma + one load-bearing particle/prep),
    and its OBJECT head noun (``dobj``/prepositional-``pobj``/``attr``). This is exactly the
    (subject, verb, object) backbone the verb-lift cannot reach when the subject is the first-person
    speaker ("I got a Samsung Galaxy S22") — there is only ONE nominal entity, so the entity-PAIR
    verb-lift bails. Here the subject can be the self-ref ("I"/"we" → user) and the object is the
    single GLiNER2-typed entity, so the caller can MERGE them into one grounded edge.

    Char offsets of the object phrase are returned so the caller can bind a GLiNER2 entity by
    POSITIONAL OVERLAP (the GLiNER2 entity that sits on this object span becomes the edge object,
    carrying its type), never by a fragile string compare.

    Makes NO rel-type/typing/routing decision; the predicate is the user's own verb only. Returns
    ``[]`` when the layer is unavailable, no SVO clause exists, or on any failure (fail-safe → the
    caller keeps today's lanes). Skips negated clauses (absence modeling deferred)."""
    doc = _parse(text)
    if doc is None:
        return []
    out: list = []
    seen: set = set()
    try:
        # Naming verbs are owned by analyze_naming; resolve the per-tenant set once so an SVO with a
        # naming verb ("a dog named Rex") is NOT double-captured here.
        try:
            _naming = _naming_verbs()
        except Exception:  # noqa: BLE001
            _naming = _NAMING_VERB_LEMMAS
        # Light/support verbs (LVC) are owned by analyze_event; resolve the per-tenant set once so the
        # with-PP redirect below stays on the SAME class membership the event seam uses.
        _lvc = _lvc_support_verbs()
        for tok in doc:
            if tok.pos_ != "VERB":
                continue
            lemma = (tok.lemma_ or tok.text or "").strip().lower()
            if not lemma or lemma == "be":
                continue
            # Naming ("named"/"called") has its own deterministic seam — never a flat SVO.
            if lemma in _naming:
                continue
            # SUBJECT — the verb's nominal subject (self-ref or entity).
            subj_tok = None
            for c in tok.children:
                if c.dep_ in ("nsubj", "nsubjpass"):
                    subj_tok = c
                    break
            if subj_tok is None:
                continue
            subj_is_self = _is_first_person_personal_pronoun(subj_tok)
            subject_text = (subj_tok.text or subj_tok.lemma_ or "").strip().lower()
            if not subject_text:
                continue
            # ASPECTUAL SUBJECT-CONTROL DESCENT (split SVO): for "I STARTED working with Rachel" the
            # matrix ("started") carries the SUBJECT and the activity verb ("working") — its ``xcomp``
            # — carries the OBJECT. The matrix has no object of its own, so recover the OBJECT (and the
            # predicate) from the xcomp while KEEPING the matrix subject. Gated to REALIZED-activity
            # aspectual matrices only (no unrealized intent — see ``_aspectual_activity_xcomp``). When
            # it fires, the xcomp becomes the SVO head and the SAME recovery machinery runs on it. The
            # naming guard is re-checked against the xcomp's lemma so "I started naming the dog …" stays
            # owned by analyze_naming.
            svo_head = tok
            _xc = _aspectual_activity_xcomp(tok)
            if _xc is not None:
                _xc_lemma = (_xc.lemma_ or _xc.text or "").strip().lower()
                if _xc_lemma and _xc_lemma != "be" and _xc_lemma not in _naming:
                    svo_head = _xc
            lemma = (svo_head.lemma_ or svo_head.text or "").strip().lower()
            # OBJECT head noun the (matrix or descended) verb governs.
            obj_tok = _svo_object_head(svo_head)
            if obj_tok is None:
                continue
            # LVC-EVENT BOUNDARY (do not double-capture, but do NOT starve the acquisition case):
            # a light/support verb ("have"/"get"/"go"/"attend"/"take"/"do"/"make"/"participate")
            # governing a bare COMMON-noun event ("had a visit", "attended a workshop") is the EVENT
            # seam's domain → skip it here. BUT the SAME verbs in an ACQUISITION/possession reading
            # over a CONCRETE named entity ("I GOT a Samsung Galaxy S22", "I bought a Trek bike")
            # have a PROPN object (or a GLiNER2-typed thing) that analyze_event explicitly excludes —
            # that is the exact gap the merge exists to close, so KEEP it. The discriminator is the
            # object's POS: a PROPN object → acquisition (keep); a common NOUN object → event seam.
            #
            # COVERAGE HOLE (do NOT blanket-skip — preserve the SPECIFIC entity over the bland head):
            # "I had an issue WITH my car's GPS system" is an LVC over the bland common-noun head
            # "issue", which would be skipped here — but the SPECIFIC entity ("GPS system") sits in a
            # ``with``-PP off that object and must not be lost. The EVENT lane (analyze_event) now
            # carries it as ``concerns``; to also keep the merge lane from dropping the entity (and so
            # the GLiNER2-typed "GPS system" can host on an edge here too), REDIRECT the SVO object to
            # that ``with``-PP entity instead of skipping. Only redirect when a genuine ``with``-PP
            # NOUN/PROPN object exists; otherwise keep the original event-seam skip. The predicate
            # stays the user's own verb (the construction is "had-issue-with X" → the occurrence is
            # about X). Genuine phrasals + date-peel are untouched (this fires ONLY on a with-PP NP).
            if lemma in _lvc and obj_tok.pos_ != "PROPN":
                _with_obj = None
                try:
                    for _c in obj_tok.children:
                        if _c.dep_ == "prep" and (_c.text or "").strip().lower() == "with":
                            for _gc in _c.children:
                                if _gc.dep_ == "pobj" and _gc.pos_ in ("NOUN", "PROPN"):
                                    _with_obj = _gc
                                    break
                        if _with_obj is not None:
                            break
                except Exception:  # noqa: BLE001 — fail-safe: no rescue → fall to the skip
                    _with_obj = None
                if _with_obj is None:
                    continue
                obj_tok = _with_obj  # surface the SPECIFIC PP entity as the SVO object
            # PREDICATE — user's verb lemma (+ load-bearing particle/prep). None → no content rel.
            predicate = _svo_predicate_token(svo_head)
            if not predicate:
                continue
            # Object phrase = head + its left compound/amod modifiers (NP), with char offsets so the
            # caller can overlap-match a GLiNER2 entity onto this span.
            # Grammar's fallback object surface: keep a NAMED multi-token value's leading number
            # ("156 Cedar St. S") — the caller PREFERS a GLiNER2 entity overlapping the span, so this
            # only affects the scalar-value fallback; a bare count ("3 cats") is never absorbed.
            object_text = _object_value_phrase(obj_tok)
            if not object_text or len(object_text) < 2:
                continue
            try:
                sub = list(obj_tok.subtree)
                _mods = [t for t in sub if t.dep_ in ("compound", "amod") and t.i < obj_tok.i]
                _span_toks = sorted(_mods + [obj_tok], key=lambda t: t.i)
                obj_start = min(t.idx for t in _span_toks)
                obj_end = max(t.idx + len(t.text) for t in _span_toks)
            except Exception:  # noqa: BLE001 — offsets are best-effort
                obj_start, obj_end = obj_tok.idx, obj_tok.idx + len(obj_tok.text)
            # Negation on EITHER the matrix ("I didn't start working …") or the descended activity verb.
            negated = any(c.dep_ == "neg" for c in tok.children) or (
                svo_head is not tok and any(c.dep_ == "neg" for c in svo_head.children)
            )
            key = (subject_text, predicate, object_text)
            if key in seen:
                continue
            seen.add(key)
            out.append(SVORelation(
                subject_text=subject_text,
                subject_is_self=subj_is_self,
                predicate=predicate,
                object_text=object_text,
                object_char_start=obj_start,
                object_char_end=obj_end,
                negated=negated,
            ))
    except Exception as e:  # noqa: BLE001 — fail-safe: a grammar miss is never a crash
        log.warning("linguistics.analyze_svo_relations_failed", error=str(e)[:160])
        return []
    return out


# ── EVENT ANALYSIS — the light-verb + eventive-noun (LVC) construction ─────────────
# THE WHY (event-capture seam): "I had a dentist visit on January 15, 2020" stores NOTHING.
# It is a LIGHT-VERB CONSTRUCTION (LVC): the eventive noun ("visit") is the semantic head and
# "had" is an empty support verb carrying no content. The verb-lift needs a GLiNER2 entity PAIR
# (user + a 2nd entity) to mint an edge, but "dentist visit" is ONE noun phrase → no pair → no
# edge → the parsed event_date has nothing to attach to. This is the SAME structural gap that
# `feels` already solved (an affective complement GLiNER2 never surfaces). The fix mirrors
# `analyze_naming`: recover the (user, <eventive-noun-phrase>) pair grammatically so an
# occurrence edge (user, participated_in, <eventive noun>) can be minted and the date can ride it.
# Spec: DEV/DESIGN-feeling-and-temporal-capture.md (events = reified occurrences).
#
# Subject-agnostic + dependency-driven (NO event-noun word-list, NO event-verb word-list). The
# only closed set is the LVC SUPPORT-VERB lemma set — the small grammatical class of English
# "light"/support verbs that form a light-verb construction by governing an eventive direct
# object ("have/had a meeting", "go to / went to a concert", "attend(ed) a workshop", "take/took
# a trip", "do/did an interview", "make a visit"). That is a grammatical (lexical-aspect) class,
# bounded as a language primitive (like the copula "be" in ``analyze_copula`` and the naming
# verbs in ``analyze_naming``) — NOT a domain event list. It is intentionally aligned with
# canonical.py ``_LIGHT_VERBS`` (have/get/make/take/do/give) plus the motion/attendance support
# verbs go/attend that complete an event without a second entity.
#
# EVENT-PARTICIPATION SUB-CLASS (added): the eventive noun is not always a direct object — with a
# participation/attendance verb it is the prepositional object of the verb's GOVERNED preposition
# ("participate IN a webinar"). ``participate`` is the canonical member of this closed grammatical
# (light/participation-verb) class; it carries no content of its own — the eventive noun
# (webinar/conference/session) is the semantic head, exactly like the LVC light verbs. This is the
# light/participation-verb PRIMITIVE, NOT a domain event list: membership is verified downstream by
# the parse (the verb must govern a prep whose pobj is the eventive head noun), so the preferred
# discriminator stays grammatical (verb + governed-prep pobj that types as an Event); this lemma is
# only the bootstrap seed for the very common ``participate`` surface the sm parser attaches as a
# bare ``prep``/``pobj`` rather than a recoverable dobj.
# DB-HELD + per-tenant + GROWABLE (migration 108 / linguistic_cue_overlay, category='lvc_support_verb').
# The frozenset below is the RETIRED-as-authority in-code list, KEPT as the DB-DOWN CODE-FALLBACK seed:
# `_lvc_support_verbs()` resolves the live set from `<tenant>.linguistic_cues` (seed-copied ∪ grown) via
# the SAME per-tenant overlay the naming-verb / temporal layers use, and falls back to THIS frozenset
# only when the overlay is unavailable/unbound (fail-safe — never lose LVC detection). Membership checks
# below call `_lvc_support_verbs()`, NOT this frozenset directly.
_LVC_SUPPORT_VERB_LEMMAS: frozenset[str] = frozenset(
    {"have", "go", "attend", "take", "do", "make", "get", "participate"}
)


def _lvc_support_verbs() -> frozenset[str]:
    """Resolve the per-tenant ACTIVE light/support-verb (LVC) lemma set via the overlay (ContextVar-
    bound to the request's tenant schema — the SAME binding the naming/rel_type/taxonomy/temporal
    overlays use). Returns a frozenset of lowercased verb lemmas. Fail-safe: any import/read failure /
    unbound schema / empty resolution → the in-code ``_LVC_SUPPORT_VERB_LEMMAS`` code-fallback seed so a
    DB-down / pre-migration / unwarmed-overlay turn still detects the light-verb construction instead of
    silently dropping the occurrence. Never empty. Mirrors ``_naming_verbs()`` exactly."""
    try:
        from src.api import linguistic_cue_overlay  # deferred: avoid import cycle / hard dep
        dsn = os.environ.get("POSTGRES_DSN", "")
        cues = linguistic_cue_overlay.resolve_lvc_support_verbs(dsn)
        if cues:
            return cues
        return _LVC_SUPPORT_VERB_LEMMAS  # empty resolution → code-fallback (never lose detection)
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the linguistic layer
        log.warning("linguistics.lvc_support_verbs_resolve_failed", error=str(e)[:160])
        return _LVC_SUPPORT_VERB_LEMMAS


# CARVED CLASS (lean-seed): problem_noun is DOMAIN-FLAVORED (issue/glitch/bug vary by domain), so it is
# NO LONGER SEEDED and the in-code fallback is EMPTY — the discriminator is GROWN PER-TENANT, never
# enumerated in code. `_problem_nouns()` resolves the live set from `<tenant>.linguistic_cues`
# (category='problem_noun', GROWN — empty on a cold tenant) via the per-tenant overlay; an empty set
# means the "had a <bland-head> with X" construction DEGRADES safely (the affected entity stays walkable
# via its other chains; the bland head's participated_in lands Class-C — captured-not-dropped) while the
# head is queued for growth. Kept (empty) only so the fail-safe path has a stable name.
_PROBLEM_NOUN_LEMMAS: frozenset[str] = frozenset()


def _problem_nouns() -> frozenset[str]:
    """Resolve the per-tenant ACTIVE PROBLEM-NOUN (bland eventive head) lemma set via the overlay
    (ContextVar-bound to the request's tenant schema — the SAME binding the LVC/naming/temporal
    overlays use). Returns a frozenset of lowercased noun lemmas. Fail-safe: any import/read failure /
    unbound schema / empty resolution → the in-code ``_PROBLEM_NOUN_LEMMAS`` code-fallback seed so a
    DB-down / pre-migration / unwarmed-overlay turn still recognizes the problem head. Never empty.
    Mirrors ``_lvc_support_verbs()`` exactly. THE DISCRIMINATOR LIVES HERE (the DB cue class), never a
    noun literal in the deriver logic."""
    try:
        from src.api import linguistic_cue_overlay  # deferred: avoid import cycle / hard dep
        dsn = os.environ.get("POSTGRES_DSN", "")
        cues = linguistic_cue_overlay.resolve_problem_nouns(dsn)
        if cues:
            return cues
        return _PROBLEM_NOUN_LEMMAS  # empty resolution → code-fallback (never lose detection)
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the linguistic layer
        log.warning("linguistics.problem_nouns_resolve_failed", error=str(e)[:160])
        return _PROBLEM_NOUN_LEMMAS


def _record_cue_candidate(cue: str, category: str) -> None:
    """Record a CARVED-CLASS growth candidate (cue lemma → category) for the current request via the
    overlay's request-scoped accumulator. The ingest/harvest seam drains it once and writes it to the
    per-tenant growth queue; the re_embedder freq-gates (≥3) and grows it into ``linguistic_cues``.
    Fail-safe: any import/record failure is swallowed — a missed growth signal NEVER breaks the
    deriver (the construction already degraded to a generic walkable rel; growth is best-effort)."""
    try:
        from src.api import linguistic_cue_overlay  # deferred: avoid import cycle / hard dep
        linguistic_cue_overlay.record_cue_candidate(cue, category)
    except Exception:  # noqa: BLE001 — fail-safe
        return


# DB-HELD + per-tenant + GROWABLE (linguistic_cue_overlay, category='inchoative_verb'). The frozenset
# below is the in-code DB-DOWN CODE-FALLBACK seed: `_inchoative_verbs()` resolves the live set from
# `<tenant>.linguistic_cues` (seed-copied ∪ grown) via the SAME per-tenant overlay the LVC/naming/
# temporal layers use, and falls back to THIS frozenset only when the overlay is unavailable/unbound
# (fail-safe). Membership checks call `_inchoative_verbs()`, NOT this frozenset directly. This is a
# LEXICAL-ASPECT (ingressive) verb class — corroborated downstream by the parse (a concrete dated
# direct object), NOT a domain/event word-list.
_INCHOATIVE_VERB_LEMMAS: frozenset[str] = frozenset(
    {"start", "begin", "commence", "launch", "initiate", "undertake"}
)


_ASPECTUAL_CONTROL_VERB_LEMMAS: frozenset[str] = frozenset(
    {"start", "begin", "continue", "keep", "resume", "commence", "finish", "stop"}
)


def _aspectual_control_verbs() -> frozenset[str]:
    """Resolve the per-tenant ACTIVE ASPECTUAL / phase SUBJECT-CONTROL verb lemma set via the overlay
    (ContextVar-bound to the request's tenant schema — the SAME binding the inchoative/LVC/naming/
    temporal overlays use). Used by ``_aspectual_activity_xcomp`` to license descending into a
    progressive ``-ing`` activity xcomp. DELIBERATELY DISTINCT from ``_inchoative_verbs()`` (which
    feeds ``analyze_inchoative``'s NOUN-object occurrence). Returns lowercased verb lemmas. Fail-safe:
    any import/read failure / unbound schema / empty resolution → the in-code code-fallback seed.
    Never empty. Mirrors ``_inchoative_verbs()`` exactly."""
    try:
        from src.api import linguistic_cue_overlay  # deferred: avoid import cycle / hard dep
        dsn = os.environ.get("POSTGRES_DSN", "")
        cues = linguistic_cue_overlay.resolve_aspectual_control_verbs(dsn)
        if cues:
            return cues
        return _ASPECTUAL_CONTROL_VERB_LEMMAS
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the linguistic layer
        log.warning("linguistics.aspectual_control_verbs_resolve_failed", error=str(e)[:160])
        return _ASPECTUAL_CONTROL_VERB_LEMMAS


# DB-HELD + per-tenant + GROWABLE (linguistic_cue_overlay, category='acquisition_verb'). ⚠️ FLAGGED
# BOUNDED LEXICAL CLASS — the one verb class the Q4 fix had to add. Unlike the fully-structural seams,
# the acquisition (transfer-of-possession) signal cannot be made purely structural: "got a phone"
# (coming-to-possess) and "had a meeting" (light-verb occurrence) share the SAME verb→dobj dep shape;
# only the verb's lexical semantics distinguishes them. So a small bounded verb class is unavoidable —
# EXACTLY as for the naming / LVC / inchoative / aspectual classes. It is firewalled downstream by the
# parse (1st-person subject + a CONCRETE possession object; a verb-complement xcomp / eventive-noun
# dobj is excluded). The frozenset below is the DB-DOWN CODE-FALLBACK seed only; membership checks call
# `_acquisition_verbs()`, NOT this frozenset directly.
_ACQUISITION_VERB_LEMMAS: frozenset[str] = frozenset(
    {"get", "buy", "purchase", "acquire", "obtain", "receive", "grab", "pick"}
)


def _acquisition_verbs() -> frozenset[str]:
    """Resolve the per-tenant ACTIVE ACQUISITION / transfer-of-possession verb lemma set via the
    overlay (ContextVar-bound to the request's tenant schema — the SAME binding the LVC/naming/
    inchoative/temporal overlays use). Returns a frozenset of lowercased verb lemmas. ⚠️ FLAGGED
    bounded lexical class (see ``_ACQUISITION_VERB_LEMMAS``). Fail-safe: any import/read failure /
    unbound schema / empty resolution → the in-code ``_ACQUISITION_VERB_LEMMAS`` code-fallback seed.
    Never empty. Mirrors ``_inchoative_verbs()`` exactly."""
    try:
        from src.api import linguistic_cue_overlay  # deferred: avoid import cycle / hard dep
        dsn = os.environ.get("POSTGRES_DSN", "")
        cues = linguistic_cue_overlay.resolve_acquisition_verbs(dsn)
        if cues:
            return cues
        return _ACQUISITION_VERB_LEMMAS
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the linguistic layer
        log.warning("linguistics.acquisition_verbs_resolve_failed", error=str(e)[:160])
        return _ACQUISITION_VERB_LEMMAS


# DB-HELD + per-tenant + GROWABLE (linguistic_cue_overlay, category='possession_verb'). ⚠️ FLAGGED
# BOUNDED LEXICAL CLASS — the STATIVE possession verb class used by the named-instance self-possession
# gate (`_type_is_self_possessed`, clause (b)) to decide whether a named instance's TYPE belongs to the
# speaker before the (user, owns/has_pet, <name>) edge is minted. This REPLACES the retired single
# hardcoded `== "have"` verb literal: a `=="have"` box only fit have-shaped sentences and silently
# dropped "I OWN a motorcycle named Bolt" / "I POSSESS a painting named Dawn" / "I KEEP a hamster named
# Nibbles". DISTINCT from `_ACQUISITION_VERB_LEMMAS` (stative CURRENTLY-possessing — have/own/possess/
# keep/hold — vs transfer COMING-to-possess — got/bought/acquired). Like acquisition, the possession
# signal cannot be made purely structural ("I have a dog" vs "I have a meeting" share verb→dobj); only
# the verb's lexical semantics distinguishes it, so a small bounded verb class is unavoidable — EXACTLY
# as for the naming / LVC / inchoative / aspectual / acquisition classes. It is firewalled by the gate's
# grammar (1st-person-personal-pronoun subject + a ProperName↔Type binding under the governing verb).
# `have` is INCLUDED so the existing family/pet self-possession path keeps working — now AS METADATA,
# not an in-code literal. The frozenset below is the DB-DOWN CODE-FALLBACK seed only; membership checks
# call `_possession_verbs()`, NOT this frozenset directly.
_POSSESSION_VERB_LEMMAS: frozenset[str] = frozenset(
    {"have", "own", "possess", "keep", "hold"}
)


def _possession_verbs() -> frozenset[str]:
    """Resolve the per-tenant ACTIVE STATIVE-POSSESSION verb lemma set via the overlay (ContextVar-bound
    to the request's tenant schema — the SAME binding the acquisition/naming/inchoative overlays use).
    Returns a frozenset of lowercased verb lemmas. ⚠️ FLAGGED bounded lexical class (see
    ``_POSSESSION_VERB_LEMMAS``). Fail-safe: any import/read failure / unbound schema / empty resolution
    → the in-code ``_POSSESSION_VERB_LEMMAS`` code-fallback seed. Never empty. Mirrors
    ``_acquisition_verbs()`` exactly."""
    try:
        from src.api import linguistic_cue_overlay  # deferred: avoid import cycle / hard dep
        dsn = os.environ.get("POSTGRES_DSN", "")
        cues = linguistic_cue_overlay.resolve_possession_verbs(dsn)
        if cues:
            return cues
        return _POSSESSION_VERB_LEMMAS
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the linguistic layer
        log.warning("linguistics.possession_verbs_resolve_failed", error=str(e)[:160])
        return _POSSESSION_VERB_LEMMAS


def _inchoative_verbs() -> frozenset[str]:
    """Resolve the per-tenant ACTIVE inchoative / ingressive START-verb lemma set via the overlay
    (ContextVar-bound to the request's tenant schema — the SAME binding the LVC/naming/temporal
    overlays use). Returns a frozenset of lowercased verb lemmas. Fail-safe: any import/read failure /
    unbound schema / empty resolution → the in-code ``_INCHOATIVE_VERB_LEMMAS`` code-fallback seed.
    Never empty. Mirrors ``_lvc_support_verbs()`` exactly."""
    try:
        from src.api import linguistic_cue_overlay  # deferred: avoid import cycle / hard dep
        dsn = os.environ.get("POSTGRES_DSN", "")
        cues = linguistic_cue_overlay.resolve_inchoative_verbs(dsn)
        if cues:
            return cues
        return _INCHOATIVE_VERB_LEMMAS
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the linguistic layer
        log.warning("linguistics.inchoative_verbs_resolve_failed", error=str(e)[:160])
        return _INCHOATIVE_VERB_LEMMAS


# ── DETERMINISTIC ENGLISH NUMBER MORPHOLOGY (singular ↔ plural surface variants) ────────────────
# WHY: an inchoative occurrence's crop ("marigold seeds") is captured in the SINGULAR surface the
# user wrote ("marigold"), but a later comparison asks in the PLURAL ("…the tomatoes or the
# marigolds?"). The query narrows operands by TOKEN-BOUNDARY CONTAINMENT against entity_aliases, so a
# stored singular alias is NOT reachable from the plural operand (and vice-versa). We register BOTH
# surface forms as aliases of the SAME crop entity so the occurrence is reachable from either form.
# This is GENERAL English NUMBER morphology (a grammatical primitive — the same regular-noun rules
# spaCy's lemmatizer encodes), NOT a domain/crop word-list. Irregulars fall back to the identity form
# (no harm: the user-written surface is always registered too, so at worst the OTHER form is missed,
# never a crash and never a wrong entity).
def _pluralize_en(word: str) -> str:
    """Best-effort REGULAR English plural of a lowercased noun surface. Rules only (no word-list):
    -s/-x/-z/-ch/-sh → +es; consonant+y → -y+ies; consonant+o → +es; else → +s. Fail-safe → input."""
    try:
        w = (word or "").strip().lower()
        if not w:
            return word
        if re.search(r"(s|x|z|ch|sh)$", w):
            return w + "es"
        if re.search(r"[^aeiou]y$", w):
            return w[:-1] + "ies"
        if re.search(r"[^aeiou]o$", w):
            return w + "es"
        return w + "s"
    except Exception:  # noqa: BLE001 — morphology must never crash the linguistic layer
        return word


def _morph_variants(word: str) -> tuple[str, str]:
    """Return ``(canonical, alias_variant)`` singular/plural surface pair for a crop anchor.

    ``canonical`` is the SINGULAR form (the regular-noun base); ``alias_variant`` is its REGULAR
    plural. If ``word`` already looks plural, ``canonical`` is its singularization and the original
    plural is the variant. Both lowercased. When the two collapse to the same string (irregular /
    uncountable / morphology miss) the variant equals the canonical (caller de-dups). Grammar-only,
    NO word-list, fail-safe → ``(word, word)``."""
    try:
        w = (word or "").strip().lower()
        if not w:
            return (word, word)
        # Detect a likely-plural surface and recover its singular (regular rules, mirror of plural).
        sing = w
        if w.endswith("ies") and len(w) > 3:
            sing = w[:-3] + "y"
        elif w.endswith("oes") and len(w) > 3:
            sing = w[:-2]
        elif re.search(r"(s|x|z|ch|sh)es$", w):
            sing = w[:-2]
        elif w.endswith("s") and not w.endswith("ss") and len(w) > 1:
            sing = w[:-1]
        plur = _pluralize_en(sing)
        return (sing, plur)
    except Exception:  # noqa: BLE001 — fail-safe
        return (word, word)


def _has_compound_noun_child(tok) -> bool:
    """True when ``tok`` heads a noun-noun compound (has a NOUN/PROPN ``compound`` dependent), i.e.
    it is an INTERMEDIATE/GENERIC head ("seeds" in "tomato seeds") rather than the leaf-most specific
    modifier — so we never anchor to it. Structural only."""
    try:
        return any(c.dep_ == "compound" and c.pos_ in ("NOUN", "PROPN") for c in tok.children)
    except Exception:  # noqa: BLE001
        return False


def _inchoative_crop_anchors(verb_tok, item_tok, text: str) -> tuple[tuple[str, str], ...]:
    r"""Surface the SPECIFIC crop/item anchors of an inchoative start so its dated occurrence is
    reachable from the specific operand a comparison asks about, not just the GENERIC head.

    THE GAP (Q8 LongMemEval, live-traced): "I started marigold seeds … March 3rd" and "I've been
    starting seeds indoors — tomatoes, peppers, cucumbers" both bind the dated start to the bare
    generic item ("seeds"), so "which seeds were started first, the tomatoes or the marigolds?"
    cannot reach either event from its crop operand. The crop names sit in (a) a NOUN-NOUN COMPOUND
    modifier of the generic head ("marigold" / "tomato" in "X seeds"), or (b) a COORDINATION /
    APPOSITION list ("tomatoes, peppers, cucumbers"). Both are recovered here, deterministically.

    THE RULE (subject-agnostic, dependency-driven — NO crop/domain word-list; the SAME grammatical
    primitives the rest of this module uses):
      (a) COMPOUND-MODIFIER crop — a NOUN/PROPN with ``dep_=='compound'`` whose head is a noun AND
          which is itself LEAF-MOST (heads no further noun-noun compound). In "tomato seeds indoors"
          the chain is tomato→seeds→indoors; only ``tomato`` (leaf) is the crop, ``seeds`` (heads the
          tomato-compound) is the generic container and is skipped. Also rescues the frequent
          mis-parse where the crop attaches to the inchoative VERB as an ``xcomp``/``amod``/``nmod``
          NOUN sitting immediately LEFT of the item head ("marigold seeds" → ``marigold`` is the
          verb's xcomp, not seeds' compound).
      (b) COORDINATION / APPOSITION list members — every ``conj``/``appos`` NOUN/PROPN in the verb's
          clause (each is its own crop), plus the list HEAD when it heads such a list and is not the
          generic item itself.

    THE HARD LINE: each returned anchor is a crop TYPE/entity; the dated start is the instance/event
    ABOUT it. We NEVER fold the generic head, an adverbial ("indoors"), or a DATE fragment in (date
    spans are excluded via the SAME ``_collect_date_spans`` detector the temporal lane uses).

    Returns a tuple of ``(canonical, alias_variant)`` singular/plural pairs (deterministic morphology)
    so the occurrence is reachable from either surface form of the operand. Empty tuple when no
    SPECIFIC crop is present (a bare "I started a garden") — caller keeps the single generic item.
    Fail-safe: any error / missing tokens → ``()``."""
    if verb_tok is None or item_tok is None:
        return ()
    try:
        try:
            _date_spans = _collect_date_spans(text)  # [(start_char, span_text), …]
        except Exception:  # noqa: BLE001 — no date probe → no date rejection
            _date_spans = []

        def _overlaps_date(tok) -> bool:
            try:
                t_lo = tok.idx
                t_hi = tok.idx + len(tok.text)
                for (_s, _span) in (_date_spans or []):
                    if _s < t_hi and (_s + len(_span)) > t_lo:
                        return True
            except Exception:  # noqa: BLE001
                return False
            return False

        ordered: list[str] = []
        seen: set[str] = set()

        def _add(tok) -> None:
            try:
                if _overlaps_date(tok):
                    return
                surf = (tok.text or "").strip().lower()
                if not surf or len(surf) < 2:
                    return
                if surf in seen:
                    return
                seen.add(surf)
                ordered.append(surf)
            except Exception:  # noqa: BLE001
                return

        # The item head and anything coordinated WITH it ("seeds", "seeds and bulbs") are the only
        # nouns a compound crop may modify. Scoping rule (a) to these is what the docstring always
        # claimed; without it, ANY compound noun in the verb's subtree was admitted as a "crop" —
        # so "under GROW lights" minted a crop called `grow`. An instrument adjunct is not a crop.
        _item_heads = {item_tok}
        try:
            _item_heads |= set(getattr(item_tok, "conjuncts", ()) or ())
        except Exception:  # noqa: BLE001
            pass

        def _compound_chain_head(t):
            """Climb the noun-COMPOUND chain and return where it lands.

            spaCy can chain a crop several compounds deep ("tomato → seeds → indoors"), so the
            crop's immediate head is not always the item. We follow `compound` links only — never
            through a preposition — so "grow → lights" lands on `lights` (a pobj of "under"),
            NOT on the item. Instrument adjuncts are thereby excluded by STRUCTURE, not a stoplist.
            """
            cur, guard = t, 0
            while (cur.dep_ == "compound" and cur.head is not None
                   and cur.head.pos_ in ("NOUN", "PROPN") and guard < 6):
                cur = cur.head
                guard += 1
            return cur

        # (a) compound-modifier crops OF THE ITEM HEAD ("marigold seeds" → marigold), including
        #     multi-deep chains. The chain must TERMINATE at the item head to count.
        for t in verb_tok.subtree:
            if (t.pos_ in ("NOUN", "PROPN") and t.dep_ == "compound"
                    and t.head is not None and t.head.pos_ in ("NOUN", "PROPN")
                    and _compound_chain_head(t) in _item_heads
                    and not _has_compound_noun_child(t)):
                _add(t)
        # (a2) verb-attached xcomp/amod/nmod NOUN immediately LEFT of the item head — the "marigold
        #      seeds" mis-parse where the crop becomes a sibling of the item, not its compound child.
        for c in verb_tok.children:
            if (c.pos_ in ("NOUN", "PROPN") and c.dep_ in ("xcomp", "amod", "nmod")
                    and c.i == item_tok.i - 1 and not _has_compound_noun_child(c)):
                _add(c)
        # (b) coordination / apposition list members (each its own crop) + the LIST HEAD.
        #
        # The list head used to be admitted only if its OWN dep_ was in a whitelist
        # (conj/appos/dobj/obj). That is a category error: what makes a token the head of an
        # enumeration is that it HEADS a conj/appos chain — not the grammatical role the chain
        # happens to fill in the wider clause. In "…since February 20th - tomatoes, peppers, and
        # cucumbers", the dash makes `tomatoes` a `pobj`, so the whitelist silently discarded the
        # FIRST member of the list while keeping its children. Half a list is worse than none: the
        # dropped operand then has no dated fact, and the comparison walk answers with whichever
        # operand survived. Structure decides membership, not the dep label.
        for t in verb_tok.subtree:
            if t.pos_ in ("NOUN", "PROPN") and t.dep_ in ("conj", "appos"):
                _add(t)
                h = t.head
                if (h is not None and h is not item_tok and h.pos_ in ("NOUN", "PROPN")
                        and any(ch.dep_ in ("conj", "appos") for ch in h.children)):
                    _add(h)

        # Map each surface crop to its (canonical, plural) morphological pair; de-dup by canonical.
        out: list[tuple[str, str]] = []
        seen_canon: set[str] = set()
        for surf in ordered:
            canon, variant = _morph_variants(surf)
            if not canon or canon in seen_canon:
                continue
            seen_canon.add(canon)
            out.append((canon, variant))
        return tuple(out)
    except Exception as e:  # noqa: BLE001 — fail-safe: a crop-anchor miss is a bare occurrence
        log.warning("linguistics.inchoative_crop_anchors_failed", error=str(e)[:160])
        return ()


@dataclass(frozen=True)
class EventAnalysis:
    """A deterministic reading of a light-verb + eventive-noun construction ("I had a visit").

    - ``event``   : the eventive direct-object noun PHRASE, lowercased ("dentist visit",
                    "concert", "team meeting") — the verb's nominal complement plus its left
                    ``compound``/``amod`` modifiers. This is the EVENT TYPE (a place in L4).
    - ``title``   : the NAMED identity of THIS occurrence, surface form, when the eventive noun
                    phrase carries a title — a QUOTED name ("Data Analysis using Python") or a
                    proper-noun appositive/PP ("on Effective Time Management"). ``None`` when the
                    event is an un-named common occurrence. THE HARD LINE: the bare type
                    ("webinar") is the PLACE; this title is the NAME (the dog/Rex split) —
                    the caller rides it on ``also_known_as``, never as the occurrence object.
    - ``concerns``: the SUBJECT-MATTER the occurrence is ABOUT, lowercased NP, when the eventive
                    noun carries a ``with``-PP ("I had an issue WITH my car's GPS system" →
                    "gps system"). This is the SPECIFIC entity that would otherwise be lost to the
                    bland eventive head ("issue"/"problem"/"trouble"). ``None`` when there is no
                    ``with``-PP. THE HARD LINE: this is the entity the event CONCERNS — the caller
                    hosts it on the occurrence (subject-matter), NOT as a date and NOT as the event
                    object; it preserves the specific name (full NP, possessive chain intact).
    - ``negated`` : True when a ``neg`` dependency hangs off the support verb ("I didn't have a
                    meeting") — a NEGATED occurrence is NOT an event; the caller must skip it.
    - ``anchors`` : CROP/ITEM ANCHORS for an inchoative occurrence whose dated start would otherwise
                    bind only to a GENERIC head ("seeds") and be unreachable from the specific item
                    operand a comparison asks about ("tomatoes"/"marigolds"). Each element is a tuple
                    ``(canonical, alias_variant)`` of the same real entity in singular/plural surface
                    forms (deterministic morphology) so the occurrence is reachable from EITHER form
                    of the operand under the query's token-boundary containment match. Populated ONLY
                    by ``analyze_inchoative`` (the LVC ``analyze_event`` path leaves it empty). THE
                    HARD LINE: each anchor is the crop TYPE/entity ("marigold"); the dated start is
                    the instance/event ABOUT it — the caller emits one dated occurrence per anchor and
                    registers the variant as the entity's alias, never mangling the crop into a date or
                    the event. Empty tuple → no specific crop surfaced; caller keeps the bare item.
    """
    event: str
    title: str | None
    concerns: str | None
    negated: bool
    anchors: tuple[tuple[str, str], ...] = ()
    # ``problem_head`` : True when the eventive head is a SEMANTICALLY-EMPTY PROBLEM noun (lemma in the
    #                    DB-grown ``problem_noun`` cue class — "issue"/"problem"/"trouble"/…) AND a
    #                    with-PP affected entity (``concerns``) is present. This is the STATE-LANE
    #                    signal: the caller emits a competing ``(<concerns>, has_state, <problem-state>)``
    #                    candidate (the structural twin of ``feels``) alongside the participated_in
    #                    candidate, and Stage-2 arbitration picks the strong state reading over the
    #                    cratered participation. The discriminator is the cue class ∧ the with-PP — a
    #                    non-problem head ("had a meeting WITH Sarah") leaves this False (untouched).
    problem_head: bool = False
    # ``problem_candidate`` : the bland eventive head LEMMA when this is the problem_noun GROWTH signal —
    #                    a "had a <bland-head> with <NON-person thing>" construction whose head is NOT
    #                    (yet) in the grown ``problem_noun`` class. CARVE-OUT: problem_noun is grown
    #                    per-tenant, so on a cold tenant ``problem_head`` is False but this carries the
    #                    candidate head so the ingest seam queues it for freq-gated growth. The PERSON
    #                    gate (the with-PP affected entity is NOT a PERSON) excludes neutral occurrences
    #                    ("had a meeting WITH Sarah" → Sarah is a person → no candidate). None otherwise.
    problem_candidate: str | None = None
    # ``problem_affected_thing`` : True when the with-PP affected entity (``concerns``) is TYPED as a
    #                    NON-SOCIAL THING (an Object/Concept/Product/Location/… — anything that is NOT a
    #                    Person/Organization/social co-participant) by the path's own entity typer
    #                    (GLiNER2 ents on the spine doc, or the spaCy NER singleton on the rewrite path).
    #                    This is the COLD-TENANT DEGRADE signal: problem_noun is grown per-tenant and is
    #                    EMPTY on a fresh tenant (so ``problem_head`` never fires there), but a "had a
    #                    <bland-head> WITH <typed THING>" construction is structurally a device-issue —
    #                    the comitative "with" of an activity ("lunch/meeting/call WITH <person/org>")
    #                    introduces a SOCIAL co-participant, never an inanimate thing. So when the
    #                    affected entity is a typed THING the caller may fire the ``has_state`` competitor
    #                    WITHOUT waiting for the grown class (the binding "<thing> has_state <problem>"
    #                    lands NOW). Grammar+type driven, ZERO noun/word-list. UNKNOWN (no type signal)
    #                    leaves this False — degrade-fire only on a POSITIVE thing-typing, so a NER-missed
    #                    person/org co-participant can never be mis-bound; the head still queues for growth.
    problem_affected_thing: bool = False


# Quoted-title net: a TITLE is most reliably marked by quotes ("Data Analysis using Python",
# 'Effective Time Management'). spaCy keeps the quote tokens but does not span the title; this
# regex lifts the quoted run from the source text. Closed, grammar-agnostic — DETECTION only (the
# quoted run is the verbatim title; we never reword it). Straight + curly quotes.
#
# T2 FIX — a bare ASCII apostrophe is BOTH a quote and a contraction mark ("I've", "don't",
# "user's"). The old single class let the contraction apostrophe in "I've" OPEN a quoted run that
# closed at the genuine opening quote of the next title ("…I've been attending …, like the workshop
# on 'Effective…" → spuriously matched "ve been attending …, like the workshop on "). We require an
# OPENING straight ``'`` to sit at a TOKEN BOUNDARY (string start or preceded by whitespace / an
# opening bracket) so a mid-word contraction apostrophe can never open a run. Double quotes and the
# curly quotes are unambiguous openers (never contractions) and keep their unrestricted boundary.
# Language-primitive boundary test (``\A``/whitespace), NOT a word-list. The run still closes on any
# closing quote variant. ``MULTILINE`` is irrelevant; the lookbehind anchors to string start/space.
_QUOTED_TITLE_RE = re.compile(
    r"""(?:
          [\"“‘]                 # a double/curly opener — unambiguous, any position
        | (?<![^\s(\[{])'        # OR a straight ' only at a token boundary (start / space / bracket)
       )
       ([^\"”’']{2,})            # the title run (≥2 chars, no embedded quote)
       [\"”’']                   # any closing quote variant
    """,
    re.VERBOSE,
)


def _event_title(event_noun, text: str) -> str | None:
    r"""Recover the NAMED identity (title) of an eventive occurrence, or ``None`` when un-named.

    THE HARD LINE (dog/Rex): the eventive noun ("webinar") is the PLACE/type; a TITLE is the
    NAME of THIS specific occurrence and must ride ``also_known_as``, never the occurrence object.
    Two grammatical title shapes, both deterministic and verbatim (no reword, no invention):

      (a) QUOTED title — a quoted run bound to THIS eventive NP's own subtree
          ("a webinar on 'Data Analysis using Python'"). The quotes mark the title; the run
          between them is copied VERBATIM. Highest-precision signal, so it is preferred.
      (b) PROPER-NOUN appositive / PP object — a PROPN appos/PP-object hanging off the eventive
          noun ("the workshop on Effective Time Management" → "Effective Time Management";
          "the Apollo conference" handled by the modifier path). The title is the contiguous
          proper-noun span (PROPN run) under that dependent.

    TITLE↔HEAD BINDING (fix 5 — the title is its OWN eventive head's name, not the main verb's
    dobj's): a quoted title is decoupled from whatever the main verb's object was. We bind a quoted
    run to the eventive head it STRUCTURALLY hangs under (the noun governing the quote via its
    appos/PP subtree), and accept it for THIS ``event_noun`` only when the quote sits inside this
    noun's own subtree. So in "I went to a concert, like the workshop on 'Effective Time
    Management'" the quoted title binds to ``workshop`` (its own appositive head), NOT to the main
    verb's dobj ``concert`` — the titled eventive NP is its own occurrence, the caller analyzes it
    separately. The greedy full-text fallback is gated on a single unambiguous quoted run so it
    only rescues the single-eventive single-quote-attachment case, never a competing head's title.

    Subject-agnostic + structural — NO title word-list. Returns the surface-form title string, or
    ``None`` when no title is present (a bare common occurrence). Fail-safe: any error → ``None``."""
    try:
        # (a) QUOTED title — preferred (a quoted run is an unambiguous, VERBATIM title signal),
        # bound to THIS eventive head. Scan the eventive NP subtree's character span (tightest
        # scope) FIRST: a quote inside this noun's own subtree is unambiguously its title.
        #
        # EXCLUDE NESTED EXEMPLAR subtrees: a GENERIC lead ("several events … such as the conference
        # on 'Cloud Security'") has the nested exemplar's OWN title deep in its subtree — that title
        # belongs to the EXEMPLAR head, not the generic lead. We drop the subtree of any child
        # ``prep`` whose ``pobj`` is a determiner-bearing common NOUN (the SAME exemplar discriminator
        # the head-collector uses) so the lead's title scan cannot reach into the nested occurrence.
        # Structural (dep_/pos_/det), NO word-list. The head's OWN trailing title PP ("on 'X'", whose
        # pobj is quoted/PROPN/determinerless) is NOT excluded — only a full exemplar NP is.
        _sub_lo = _sub_hi = None
        try:
            _exclude: set = set()
            for _ch in event_noun.children:
                if _ch.dep_ != "prep":
                    continue
                for _gc in _ch.children:
                    if (_gc.dep_ == "pobj" and _gc.pos_ == "NOUN"
                            and any(_d.dep_ == "det" for _d in _gc.children)):
                        # whole PP (the prep + the exemplar subtree) belongs to the nested occurrence
                        _exclude.update(t.i for t in _ch.subtree)
            sub = [t for t in event_noun.subtree if t.i not in _exclude]
            if sub:
                _sub_lo = min(t.idx for t in sub)
                _sub_hi = max(t.idx + len(t.text) for t in sub)
                # scan only contiguous text up to the first excluded (nested-exemplar) token so a
                # quote beyond the exemplar boundary cannot be picked up across the gap.
                if _exclude:
                    _ex_idxs = [t.idx for t in event_noun.subtree if t.i in _exclude]
                    if _ex_idxs:
                        _ex_lo = min(_ex_idxs)
                        if _ex_lo > _sub_lo:
                            _sub_hi = min(_sub_hi, _ex_lo)
                m = _QUOTED_TITLE_RE.search(text[_sub_lo:_sub_hi])
                if m:
                    title = m.group(1).strip()
                    if len(title) >= 2:
                        return title
        except Exception:  # noqa: BLE001 — subtree/quote probe must not crash; fall through
            _sub_lo = _sub_hi = None
        # Full-text fallback ONLY for the spaCy single-quote-attachment gap (the CLOSING quote drops
        # out of the subtree so the tight scan misses) — and ONLY when the quoted title is bound to
        # THIS event_noun, not a competing eventive head in an appositive. We require BOTH:
        #   • exactly ONE quoted run in the whole sentence (no ambiguity about which head owns it),
        #   • that run's OPENING quote sits INSIDE this noun's subtree span ``[_sub_lo, _sub_hi)``.
        #     The opening quote is reliably IN the subtree (only the CLOSING quote drops out — the
        #     gap we are rescuing); requiring the opener INSIDE the subtree (not merely at/after its
        #     start) keeps a GENERIC LEAD head ("various workshops", whose subtree ENDS before the
        #     quote) from stealing the appositive head's title (T1/T3 multi-head fix). A title in a
        #     SEPARATE appositive head's subtree fails this and is left to that head's own analysis.
        try:
            _all = _QUOTED_TITLE_RE.findall(text)
            if len(_all) == 1:
                m = _QUOTED_TITLE_RE.search(text)
                if m and (_sub_lo is None or (_sub_lo <= m.start() < _sub_hi)):
                    title = m.group(1).strip()
                    if len(title) >= 2:
                        return title
        except Exception:  # noqa: BLE001 — fallback probe must not crash; fall to (b)
            pass
        # (b) PROPER-NOUN appositive / PP object under the eventive noun. Walk the noun's
        # dependents for an appositive (``appos``) PROPN or a preposition (``prep``) whose object
        # (``pobj``) is a PROPN ("workshop on Effective Time Management"). The title is the
        # contiguous PROPN run rooted at that dependent (head + adjacent PROPN/compound tokens).
        # DATE spans in the source (NER ∪ numeric regex). A date is NEVER a title/name (THE HARD
        # LINE: the date is a SCALAR leaf owned by the temporal lane, never the event's
        # also_known_as). "I had a dentist visit on Jan 15 2020" parses "Jan" as a PROPN pobj of
        # "on" → without this it would leak as title="Jan". We reject any PROPN run that OVERLAPS a
        # detected date span. Reuses the SAME date detector the temporal lane uses (deterministic,
        # fail-safe → [] on any failure, so a real title is never lost on a date-probe error).
        try:
            _date_spans = _collect_date_spans(text)  # [(start_char, span_text), …]
        except Exception:  # noqa: BLE001 — fail-safe: no date probe → no date rejection
            _date_spans = []

        def _overlaps_date(tok) -> bool:
            try:
                t_lo = tok.idx
                t_hi = tok.idx + len(tok.text)
                for (_s, _span) in (_date_spans or []):
                    if _s < t_hi and (_s + len(_span)) > t_lo:
                        return True
            except Exception:  # noqa: BLE001
                return False
            return False

        def _propn_run(tok) -> str | None:
            if tok.pos_ != "PROPN":
                return None
            # Reject a PROPN that is part of a date span ("Jan"/"March"/"2020") — a date fragment is
            # never an event name.
            if _overlaps_date(tok):
                return None
            # contiguous proper-noun phrase: the token plus its compound/PROPN children, in order
            toks = [tok] + [c for c in tok.children if c.pos_ == "PROPN" or c.dep_ == "compound"]
            toks = sorted({t.i: t for t in toks}.values(), key=lambda t: t.i)
            phrase = " ".join(t.text for t in toks if t.text and t.text.strip()).strip()
            return phrase or None

        # ── span helper: collect a verb's nominal-complement title span (oprd/attr/dobj/obj) ──
        # The naming verb's complement is the title. Prefer its full subtree run (so multi-word
        # titles like "Effective Time Management" survive) over a single PROPN; reject a complement
        # that overlaps a date span. VERBATIM (no reword). Returns the surface span or None.
        def _complement_title(verb) -> str | None:
            for gc in verb.children:
                if gc.dep_ not in ("oprd", "attr", "dobj", "obj"):
                    continue
                # full contiguous subtree span of the complement (head + its own modifiers),
                # ordered, date-overlap rejected token-wise.
                sub = sorted({t.i: t for t in gc.subtree}.values(), key=lambda t: t.i)
                toks = [t for t in sub if not _overlaps_date(t)]
                if not toks:
                    continue
                phrase = " ".join(t.text for t in toks if t.text and t.text.strip()).strip()
                if phrase and len(phrase) >= 2:
                    return phrase
            return None

        # (b1) ACL/RELCL NAMING PARTICIPLE — "the workshop called/named/titled <Title>". The naming
        # verb is an ``acl``/``relcl`` child of the eventive noun; its nominal complement is the
        # title. analyze_event DEFERS this shape to the naming seam, but for an EVENT noun the title
        # is exactly what we want on also_known_as — capture it here. Naming-verb VOCABULARY is the
        # DB-grown per-tenant set (overlay) ∪ code-fallback; the dependency relations stay in code.
        try:
            _naming = _naming_verbs()
            for c in event_noun.children:
                if c.dep_ not in ("acl", "relcl"):
                    continue
                if (c.lemma_ or "").strip().lower() not in _naming:
                    continue
                run = _complement_title(c)
                if run:
                    return run
        except Exception:  # noqa: BLE001 — naming-participle probe must not crash; fall through
            pass

        # (b2) COMPOUND PROPER-NOUN PREMODIFIER — "the Apollo webinar". The title is the head noun's
        # left ``compound``/PROPN run. GATED on a Title-Case / proper-name span: a descriptive
        # non-Title-Case multi-word premod ("the data analysis using python webinar") is ambiguous →
        # left to the gap-fill (we do NOT force it). The event TYPE string (_np_phrase) excludes this
        # premodifier via the matching gate in analyze_event, so "webinar" stays the bare place.
        try:
            _premods = [
                c for c in event_noun.children
                if c.dep_ in ("compound", "nmod") and c.i < event_noun.i
                and c.pos_ == "PROPN" and not _overlaps_date(c)
            ]
            _premods = sorted(_premods, key=lambda t: t.i)
            # Title-Case gate: every premod token must be Title-Case (or all-caps acronym). PROPN
            # tagging alone is not enough on the sm model — require the casing signal so a lowercase
            # descriptive premod is never lifted as a title.
            if _premods and all(
                (t.text[:1].isupper() and (t.text.istitle() or t.text.isupper()))
                for t in _premods if t.text
            ):
                phrase = " ".join(t.text for t in _premods if t.text and t.text.strip()).strip()
                if phrase and len(phrase) >= 2:
                    return phrase
        except Exception:  # noqa: BLE001 — premod probe must not crash; fall through
            pass

        for c in event_noun.children:
            if c.dep_ == "appos":
                run = _propn_run(c)
                if run:
                    return run
            if c.dep_ == "prep":
                for gc in c.children:
                    if gc.dep_ == "pobj":
                        run = _propn_run(gc)
                        if run:
                            return run
        return None
    except Exception as e:  # noqa: BLE001 — fail-safe: a title-miss is a bare occurrence, not a crash
        log.warning("linguistics.event_title_failed", error=str(e)[:160])
        return None


def _with_pp_subject_matter(event_noun) -> str | None:
    r"""Recover the SUBJECT-MATTER an eventive noun is ABOUT from a ``with``-PP, or ``None``.

    THE GAP (capture loses the specific entity to the bland head): "I had an issue WITH my car's
    GPS system" reifies the bare eventive head "issue" while the SPECIFIC entity ("GPS system")
    sits in the ``with``-PP and is dropped. spaCy attaches that PP to the eventive NOUN:
    issue →prep(with) →pobj(system), with "system" carrying its ``compound`` ("GPS") and a ``poss``
    chain ("car's"). We surface the pobj NP so the occurrence is ABOUT the specific thing.

    Subject-agnostic + structural — the ``with`` adposition is a closed grammatical primitive (an
    ADP surface form, like the load-bearing particles), NOT a domain word-list. We take the FIRST
    ``with``-prep child of the eventive noun whose ``pobj`` is a NOUN/PROPN, and return that pobj's
    NP phrase (head + left ``compound``/``amod`` modifiers, lowercased) so the SPECIFIC name is
    preserved verbatim. Returns ``None`` when there is no ``with``-PP object, or on any failure.

    A date pobj ("with January") is rejected via the shared date detector — a date is a temporal
    scalar (the temporal lane owns it), never the subject-matter."""
    try:
        try:
            _date_spans = _collect_date_spans(event_noun.doc.text)
        except Exception:  # noqa: BLE001 — fail-safe: no date probe → no date rejection
            _date_spans = []

        def _overlaps_date(tok) -> bool:
            try:
                t_lo, t_hi = tok.idx, tok.idx + len(tok.text)
                for (_s, _span) in (_date_spans or []):
                    if _s < t_hi and (_s + len(_span)) > t_lo:
                        return True
            except Exception:  # noqa: BLE001
                return False
            return False

        for c in event_noun.children:
            if c.dep_ != "prep" or (c.text or "").strip().lower() != "with":
                continue
            for gc in c.children:
                if gc.dep_ == "pobj" and gc.pos_ in ("NOUN", "PROPN") and not _overlaps_date(gc):
                    matter = _np_phrase(gc)
                    if matter and len(matter) >= 2:
                        return matter
        return None
    except Exception as e:  # noqa: BLE001 — fail-safe: a subject-matter miss is a bare occurrence
        log.warning("linguistics.with_pp_subject_matter_failed", error=str(e)[:160])
        return None


def _with_pp_pobj_token(event_noun):
    r"""Return the TOKEN of the first ``with``-PP ``pobj`` (NOUN/PROPN, non-date) off ``event_noun``,
    or ``None``. The token twin of ``_with_pp_subject_matter`` (which returns the surface phrase) —
    the caller needs the live token to read its subtree morphology (1st-person possessive) and its
    head lemma (a social-role/kinship cue). Same closed ``with``-adposition primitive, same date-skip,
    fail-safe to ``None``."""
    try:
        try:
            _date_spans = _collect_date_spans(event_noun.doc.text)
        except Exception:  # noqa: BLE001
            _date_spans = []

        def _overlaps_date(tok) -> bool:
            try:
                t_lo, t_hi = tok.idx, tok.idx + len(tok.text)
                for (_s, _span) in (_date_spans or []):
                    if _s < t_hi and (_s + len(_span)) > t_lo:
                        return True
            except Exception:  # noqa: BLE001
                return False
            return False

        for c in event_noun.children:
            if c.dep_ != "prep" or (c.text or "").strip().lower() != "with":
                continue
            for gc in c.children:
                if gc.dep_ == "pobj" and gc.pos_ in ("NOUN", "PROPN") and not _overlaps_date(gc):
                    return gc
        return None
    except Exception:  # noqa: BLE001 — fail-safe
        return None


def _affected_is_social(pobj_tok, concerns: str) -> bool:
    r"""True when the ``with``-PP affected entity is a SOCIAL co-participant (a person / organization),
    so an LVC "had a <problem> with <X>" is INTERPERSONAL ("a problem with my coworker Sam") rather
    than a device-issue ("an issue with my car's GPS system"). Two deterministic signals, NO word zoo:
      (1) spaCy NER on the affected span labels it PERSON/ORG/NORP/GPE (a proper-name party — "Sam",
          "Apple") — the existing NER singleton, never GLiNER2;
      (2) the affected HEAD LEMMA is a person role in the grown cue classes — kinship_noun
          ("brother"/"mother") ∪ social_role ("coworker"/"manager"/"boss") — covering common-noun
          parties NER does not tag.
    Fail-safe: any miss → False (not provably social) so the caller's POSSESSION gate still guards."""
    try:
        # (2) lexical person-role cue (cheap, no NER) — head lemma in kinship ∪ social_role.
        try:
            _head = (pobj_tok.lemma_ or "").strip().lower() if pobj_tok is not None else ""
            if _head and (_head in _kinship_nouns() or _head in (_social_role_map() or {})):
                return True
        except Exception:  # noqa: BLE001
            pass
        # (1) proper-name PERSON/ORG via the NER singleton. Run NER over the FULL sentence and test
        # whether any social entity overlaps the pobj's SUBTREE char span — so an APPOSITIVE name not
        # carried in the bare ``concerns`` phrase is still caught ("my coworker Sam" → the pobj subtree
        # spans "my coworker Sam", NER tags "Sam" PERSON → social). Fall back to NER on the bare span
        # when the token/doc is unavailable.
        _ner = _get_nlp_ner()
        if _ner is not None:
            _lo = _hi = None
            _full = None
            try:
                if pobj_tok is not None:
                    _sub = list(pobj_tok.subtree)
                    if _sub:
                        _lo = min(t.idx for t in _sub)
                        _hi = max(t.idx + len(t.text) for t in _sub)
                        _full = pobj_tok.doc.text
            except Exception:  # noqa: BLE001
                _lo = _hi = _full = None
            if _full is not None and _lo is not None:
                for _ent in (_ner(_full).ents or []):
                    if (_ent.label_ or "").strip().lower() not in _SOCIAL_AFFECTED_LABELS:
                        continue
                    # overlap test: the social entity sits within / touches the pobj subtree span.
                    if _ent.start_char < _hi and _ent.end_char > _lo:
                        return True
            else:
                _span = (concerns or "").strip()
                if _span:
                    for _ent in (_ner(_span).ents or []):
                        if (_ent.label_ or "").strip().lower() in _SOCIAL_AFFECTED_LABELS:
                            return True
    except Exception:  # noqa: BLE001 — fail-safe: NER/cue unavailable → not provably social
        return False
    return False


def _is_first_person_possessed(pobj_tok) -> bool:
    r"""True when the affected NP is POSSESSED BY THE SPEAKER — any token in the pobj subtree is a
    1st-person POSSESSIVE determiner (Person=1 ∧ Poss=Yes: "my"/"our", possibly NESTED as in "my
    car's GPS system" where "my" possesses "car" which possesses "system"). This is the CONCRETENESS
    + ownership signal that separates a device-issue ("an issue with MY car's GPS") from an abstract
    topic ("a problem with the traffic / the math" — no 1st-person possessive). Subtree walk so a
    nested possessive is caught; morphology only, NO word-list. Fail-safe: any error → False."""
    try:
        if pobj_tok is None:
            return False
        for t in pobj_tok.subtree:
            try:
                if t.morph.get("Person") == ["1"] and "Yes" in (t.morph.get("Poss") or []):
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False
    except Exception:  # noqa: BLE001
        return False


def _collect_eventive_heads(tok, text: str, dated: bool = False) -> list:
    r"""Enumerate EVERY eventive head NP reachable from a support verb ``tok`` (T1 PRIMARY fix).

    THE GAP (Q2): the old single-head selection returned the FIRST support-verb object and dropped
    every sibling eventive NP, so an appositive titled occurrence ("…attending various workshops,
    like the workshop on 'Effective Time Management'") was lost — only "various workshops" survived,
    titleless. We now collect ALL eventive heads under the verb and let the caller analyze + reify
    each, so a titled sibling occurrence is no longer dropped.

    THE SET (grammatical, dep_/pos_ only — NO preposition or word-list; "like"/"such as"/"including"
    all surface as a ``prep`` with a ``pobj`` and are walked STRUCTURALLY, never matched by surface):
      • {the ``dobj``/``obj`` NOUN}                                       — transitive support object
      • {every ``pobj`` NOUN/PROPN under the verb's (or its prt/advmod's) ``prep`` children}
        — covers governed-prep, motion/source, AND the appositive-PP "like/such as/including X"
      • {NOUN ``conj`` siblings of any of the above}                      — coordinated occurrences

    POS DISCIPLINE preserved from the single-head path:
      • a DIRECT-OBJECT head is NOUN-only (a PROPN dobj of have/get is possession, not an event);
      • a governed-prep PROPN head is admitted ONLY when QUOTED (an unambiguous NAMED occurrence —
        "'Rack Fest'"); a bare unquoted PROPN pobj ("went to Paris") is a LOCATION the locative/
        naming seams own and is NOT poached.
      • a DATE pobj is skipped (a date is a temporal scalar, never the eventive head).

    GENERIC-LEAD GUARD (T3): when a quoted title is present and the FIRST head is a determinerless/
    quantified GENERIC lead ("various workshops"/"several events": a NOUN carrying an ``amod``/``det``
    quantifier) while a more-specific sibling eventive head exists, the specific head is ORDERED FIRST
    so the title binds to it ('Cloud Security' → conference, not 'several events'). Grammatical
    (``amod``/``det`` quantifier morphology), NO word-list. Returns heads in a stable, deduped order
    (head token identity); empty when none. Fail-safe: any error → whatever was collected so far."""

    def _is_quoted_propn(_g) -> bool:
        if _g is None or _g.pos_ != "PROPN":
            return False
        try:
            sub = list(_g.subtree)
            if not sub:
                return False
            _lo = max(0, min(t.idx for t in sub) - 1)
            _hi = min(len(text), max(t.idx + len(t.text) for t in sub) + 1)
            return _QUOTED_TITLE_RE.search(text[_lo:_hi]) is not None
        except Exception:  # noqa: BLE001 — fail-safe: no quote probe → not admitted
            return False

    # DATE-SKIP — reuse the shared DATE detector; a pobj whose subtree overlaps a date span is a
    # temporal scalar, never the eventive head (dep_/NER only, NO word-list).
    try:
        _ev_date_spans = _collect_date_spans(tok.doc.text)
    except Exception:  # noqa: BLE001 — fail-safe: no date probe → no skip
        _ev_date_spans = []

    def _is_date_tok(_g) -> bool:
        if not _ev_date_spans:
            return False
        try:
            sub = list(_g.subtree)
            if not sub:
                return False
            g_lo = min(t.idx for t in sub)
            g_hi = max(t.idx + len(t.text) for t in sub)
            for (_s, _span) in _ev_date_spans:
                if _s < g_hi and (_s + len(_span)) > g_lo:
                    return True
        except Exception:  # noqa: BLE001 — fail-safe → not a date → today's behavior
            return False
        return False

    heads: list = []
    seen: set = set()

    def _is_quantity_unit_head(_g) -> bool:
        # A "<num> <unit> of <substance>" partitive ("500 milligrams of metformin") is a QUANTITY,
        # NOT an eventive occurrence — the UNIT noun bearing a NUM nummod (DIGIT-gated) + an "of"-PP
        # naming the substance is owned by the deriver's quantity-of scalar chain (which grounds the
        # SUBSTANCE relationally). Emitting participated_in(user, <unit>) here re-introduces the exact
        # mangle the quantity chain fixes. Structural (nummod digit + "of" prep with a NOUN/PROPN
        # pobj), subject-agnostic, NO unit/domain word-list. Fail-safe → not a quantity.
        try:
            if _g is None or _g.pos_ not in ("NOUN", "PROPN"):
                return False
            if not any(c.dep_ == "nummod" and c.pos_ == "NUM"
                       and any(ch.isdigit() for ch in (c.text or "")) for c in _g.children):
                return False
            for c in _g.children:
                if c.dep_ == "prep" and (c.text or "").strip().lower() == "of" \
                        and any(gc.dep_ == "pobj" and gc.pos_ in ("NOUN", "PROPN")
                                for gc in c.children):
                    return True
        except Exception:  # noqa: BLE001 — fail-safe
            return False
        return False

    def _add(_g):
        if _g is None or _g.i in seen:
            return
        if _is_quantity_unit_head(_g):
            return  # a "<num> <unit> of <substance>" quantity — the deriver's scalar chain owns it
        seen.add(_g.i)
        heads.append(_g)

    # A head OWNS A TITLE when a quoted run, an ``appos`` PROPN, or a PROPN pobj of an ``on``-style
    # PP hangs inside its OWN subtree — grammatical (subtree char-span ∩ quote / PROPN dependent),
    # NO word-list. Used both to ORDER a specific head ahead of a generic lead (T3) and to keep a
    # bare locative adjunct from being suppressed when it carries its own title.
    def _owns_title(_g) -> bool:
        try:
            sub = list(_g.subtree)
            if not sub:
                return False
            _lo = min(t.idx for t in sub)
            _hi = max(t.idx + len(t.text) for t in sub)
            if _QUOTED_TITLE_RE.search(text[_lo:_hi]) is not None:
                return True
            # an appos PROPN or a PROPN pobj of a child prep is a non-quoted title (the (b) path of
            # _event_title) — a head with one is a NAMED occurrence, not a bare adjunct.
            for ch in _g.children:
                if ch.dep_ == "appos" and ch.pos_ == "PROPN":
                    return True
                if ch.dep_ == "prep":
                    for gc in ch.children:
                        if gc.dep_ == "pobj" and gc.pos_ == "PROPN" and not _is_date_tok(gc):
                            return True
            return False
        except Exception:  # noqa: BLE001
            return False

    # PROVENANCE for adjunct suppression: a bare common-noun head reached only via a GOVERNED PREP
    # directly on the verb (not a dobj, not an appositive under another head, not titled) is a
    # circumstantial adjunct ("got back from 'Rack Fest' IN nearby city" → "city"). When a stronger
    # eventive head exists (a quoted PROPN named occurrence, or a titled head) it is dropped.
    _verb_prep_bare: set = set()   # head.i reached via a governed prep on the verb, untitled NOUN
    _has_named_head = False        # any quoted-PROPN / titled head present

    try:
        # (a) direct object — NOUN only (PROPN dobj of have/get is possession, not an event).
        for c in tok.children:
            if c.dep_ in ("dobj", "obj") and c.pos_ == "NOUN" and not _is_date_tok(c):
                # OVER-CAPTURE GATE (present-simple STATIVE POSSESSION): a light verb in the PRESENT
                # SIMPLE governing a concrete direct object is stative possession ("I HAVE a car"),
                # NOT an eventive occurrence — minting (user, participated_in, car) is junk. Admit the
                # direct-object occurrence only when it is EVENT-FRAMED: the clause carries a real date
                # (``dated`` — the occurrence seam's raison d'être: a dated event) OR the verb is a
                # realized past/perfective/progressive occurrence ("HAD a meeting", "ATTENDED a
                # webinar", "been ATTENDING"). Grammar-only (tense/aspect morphology via
                # _verb_is_present_simple), subject-agnostic, NO noun/verb list; fail-safe (undecidable
                # tense → kept). A genuine dated/past occurrence is untouched — only a present-simple
                # UNDATED possession clause is dropped here. The prep/pobj/conj branches below are the
                # inherently-directional/eventive constructions ("went TO a concert") and are NOT gated.
                if not dated and _verb_is_present_simple(tok):
                    continue
                _add(c)
        # (b) governed-prep objects — every NOUN pobj, plus a QUOTED PROPN pobj (named occurrence).
        # Governed preps hang DIRECTLY off the verb ("went TO a concert") OR off its prt/advmod in a
        # phrasal ("got BACK from the Rack Fest"); walk both (one level). dep_/pos_ only — NO
        # preposition word-list.
        #
        # FIX A — REJECT FRONTED ADJUNCT/DISCOURSE PPs as eventive heads. A prep that attaches to the
        # support VERB but linearly PRECEDES the verb's own subject is a sentence-initial discourse /
        # circumstantial adjunct ("By the way, I had …" → By(prep→had) precedes the subject "I"), NOT
        # a core argument of the verb. A genuine governed COMPLEMENT prep ("participate IN a webinar",
        # "go TO a concert", "… like the workshop") always FOLLOWS the subject. The discriminator is
        # pure dep-arc shape + linear order (prep index < the verb's nsubj index), NEVER a preposition
        # word-list ("by"/"way" are never named) — so it is subject-agnostic and grows for free. Its
        # pobj ("way") is an ADJUNCT, never an occurrence, so we skip the WHOLE fronted-adjunct prep.
        # Only applies to preps governed by the VERB host (an adjunct hangs off the predicate); a prep
        # off a prt/advmod phrasal particle keeps today's behavior.
        def _verb_subj_index() -> int | None:
            try:
                for _c in tok.children:
                    if _c.dep_ in ("nsubj", "nsubjpass"):
                        return _c.i
            except Exception:  # noqa: BLE001
                return None
            return None

        _subj_i = _verb_subj_index()

        def _is_fronted_adjunct_prep(_prep, _host) -> bool:
            # Only a VERB-host prep that precedes the verb's subject is a fronted adjunct. A genuine
            # complement prep follows the subject; a phrasal-particle-hosted prep is exempt.
            try:
                if _host is not tok or _subj_i is None:
                    return False
                return _prep.i < _subj_i
            except Exception:  # noqa: BLE001 — fail-safe: not adjunct → today's behavior
                return False

        _prep_hosts = [tok] + [
            c for c in tok.children
            if c.dep_ in ("prt", "advmod") and c.pos_ in ("ADP", "ADV", "PART")
        ]
        for _host in _prep_hosts:
            for c in _host.children:
                if c.dep_ != "prep":
                    continue
                if _is_fronted_adjunct_prep(c, _host):
                    continue  # FIX A: fronted discourse/circumstantial adjunct → not an occurrence
                for gc in c.children:
                    if gc.dep_ != "pobj" or _is_date_tok(gc):
                        continue
                    if gc.pos_ == "NOUN":
                        _add(gc)
                        if not _owns_title(gc):
                            _verb_prep_bare.add(gc.i)
                    elif gc.pos_ == "PROPN" and _is_quoted_propn(gc):
                        _add(gc)
                        _has_named_head = True
        # (c) NESTED APPOSITIVE / EXEMPLAR pobjs — the "such as the conference", "like the workshop
        # on 'X'" shape: a ``prep`` hanging off a HEAD NOUN (not the verb) whose ``pobj`` is a fuller
        # eventive NP. Walked structurally from each collected head — "like / such as / including"
        # all surface as prep→pobj, NO preposition word-list. This is where the SPECIFIC sibling of a
        # GENERIC lead ("various workshops … like the workshop on 'X'", "several events … such as the
        # conference on 'Y'") is recovered.
        #
        # GUARD (do NOT mistake a TITLE PP for a sub-occurrence): "a webinar ON 'Data Analysis using
        # Python'" / "the workshop ON Effective Time Management" hang the TITLE under the head via a PP
        # too — that pobj is the NAME, not its own occurrence. The structural discriminator (no word-
        # list): a genuine exemplar is a COMMON NOUN carrying its OWN determiner (a full NP — "THE
        # conference"); a title pobj is QUOTED, a PROPN, or a determinerless topic — all skipped here
        # and left to _event_title to bind as the head's name.
        for _h in list(heads):
            for c in _h.children:
                if c.dep_ != "prep":
                    continue
                for gc in c.children:
                    if gc.dep_ != "pobj" or _is_date_tok(gc):
                        continue
                    if gc.pos_ != "NOUN":
                        continue  # PROPN / proper-noun title pobj → the head's NAME, not an occurrence
                    if not any(ch.dep_ == "det" for ch in gc.children):
                        # determinerless / quoted title pobj ("on time management", "on 'X'") → the
                        # head's NAME, not an exemplar. A genuine exemplar is a full NP ("THE
                        # conference") with its own determiner — that NP may itself carry a title.
                        continue
                    _add(gc)
        # (d) coordinated conj siblings: a conj of the VERB ("attended a workshop … and a webinar" —
        # the second NP hangs off the verb as conj) AND a conj of any collected head ("a meeting, a
        # call, and a review"). NOUN only (a bare PROPN conj is a name the naming seam owns).
        for _src in [tok] + list(heads):
            for _cj in _np_conjuncts(_src) if _src in heads else [
                c for c in _src.children if c.dep_ == "conj"
            ]:
                if _cj.pos_ == "NOUN" and not _is_date_tok(_cj):
                    _add(_cj)
        # (d2) DEEP COORDINATED NP — without a comma, spaCy chains the second event off the FIRST
        # event's TITLE pobj rather than the verb/head ("a workshop on 'Intro to Rust' and a webinar
        # on 'Data Pipelines'" → webinar is conj of "Intro", a title token). Recover any ``conj``
        # NOUN anywhere in the VERB's subtree that is a full coordinated NP (carries its OWN
        # determiner) so the second titled occurrence is not lost. det-bearing → a real NP, not a
        # title fragment; date-skip preserved. Structural (dep_/pos_/det), NO word-list.
        try:
            for _t in tok.subtree:
                if (_t.dep_ == "conj" and _t.pos_ == "NOUN" and _t.i not in seen
                        and not _is_date_tok(_t)
                        and any(_d.dep_ == "det" for _d in _t.children)):
                    _add(_t)
        except Exception:  # noqa: BLE001 — fail-safe: subtree walk must not crash collection
            pass
        if any(_owns_title(_h) for _h in heads):
            _has_named_head = True

        # ADJUNCT SUPPRESSION (T-pres): when a NAMED occurrence head exists, drop a bare untitled
        # common-noun head reached ONLY via a governed prep on the verb (a circumstantial locative —
        # "in nearby city") so it is not minted as a spurious second occurrence. Preserves the old
        # single-head PROPN-preference behavior ("got back from 'Rack Fest' in nearby city" → just
        # the Rack Fest). A bare governed-prep head with NO named sibling is kept (today's behavior).
        #
        # EXEMPT A LEAD: a head that GOVERNS another collected head in its subtree (the generic lead
        # of an exemplar — "several events … such as the conference") is NOT a leaf adjunct; only a
        # true LEAF bare governed-prep head ("in nearby city", governing no other occurrence) is a
        # circumstantial adjunct and may be dropped. Structural (subtree containment), NO word-list.
        if _has_named_head and _verb_prep_bare:
            _head_idxs = {h.i for h in heads}

            def _governs_other_head(_h) -> bool:
                try:
                    return any(t.i in _head_idxs and t.i != _h.i for t in _h.subtree)
                except Exception:  # noqa: BLE001
                    return False

            heads = [
                h for h in heads
                if h.i not in _verb_prep_bare or _owns_title(h) or _governs_other_head(h)
            ]

        # GENERIC-LEAD GUARD (T3): a quoted title in the clause + a generic/quantified LEAD head with
        # a more-specific TITLED sibling → order the SPECIFIC head first so the title binds to it. A
        # GENERIC head carries an ``amod``/quantifier ``det`` and governs no title subtree of its own.
        if len(heads) >= 2 and _QUOTED_TITLE_RE.search(text):
            def _is_generic_lead(_g) -> bool:
                try:
                    if not any(ch.dep_ in ("amod", "det") and ch.pos_ in ("ADJ", "DET")
                               for ch in _g.children):
                        return False
                    return not _owns_title(_g)
                except Exception:  # noqa: BLE001 — fail-safe → not generic → no reorder
                    return False

            if _is_generic_lead(heads[0]):
                _specific = [h for h in heads[1:] if _owns_title(h)]
                if _specific:
                    _rest = [h for h in heads if h not in _specific]
                    heads = _specific + _rest
        return heads
    except Exception as e:  # noqa: BLE001 — fail-safe: return whatever was collected
        log.warning("linguistics.collect_eventive_heads_failed", error=str(e)[:160])
        return heads


def _build_event_analysis(event_noun, tok, text: str):
    r"""Build ONE ``EventAnalysis`` for an already-selected eventive head ``event_noun`` governed by
    support verb ``tok`` (the per-head body factored out of the old single-head ``analyze_event``).

    Recovers the event PHRASE, negation (from a ``neg`` on the support verb), the TITLE (the
    dog/Rex split — quoted/appos/PP/acl name bound to THIS head's own subtree), and the
    ``with``-PP SUBJECT-MATTER, applying the same distinctness guards, naming-seam defer, and
    Title-Case-premod strip as the original. Returns ``EventAnalysis`` | ``None`` (fail-safe)."""
    try:
        event = _np_phrase(event_noun)
        if not event:
            return None
        negated = any(c.dep_ == "neg" for c in tok.children)
        # TITLE (the dog/Rex split): a quoted name, PROPN appositive/PP-object, an
        # acl/relcl naming-participle ("workshop called X"), or a Title-Case compound premod
        # ("the Apollo webinar") on the eventive noun is the NAME of THIS occurrence — it rides
        # ``also_known_as``, never the occurrence object (which stays the bare type). ``None`` for
        # a bare common occurrence. _event_title binds a quoted/appos/PP title to the HEAD it hangs
        # under, scoped to THIS head's own subtree — so per-head analysis gives each occurrence its
        # OWN title (the appositive "the workshop on 'Effective Time Management'" titles workshop).
        title = _event_title(event_noun, text)
        # Guard: a title that IS the event surface (parse quirk) is no useful (type, name) pair.
        if title and title.strip().lower() == event:
            title = None

        # SUBJECT-MATTER (preserve the specific entity over the bland head): "I had an issue
        # WITH my car's GPS system" reifies the bland eventive head "issue" while the SPECIFIC
        # entity ("gps system") sits in a ``with``-PP off the eventive noun and would be lost.
        # Surface it so the occurrence is ABOUT the specific thing, not the generic head. THE
        # HARD LINE: this is the entity the event CONCERNS — the caller hosts it on the
        # occurrence (subject-matter), never as a date and never as the event object.
        concerns = _with_pp_subject_matter(event_noun)
        # Guard: a subject-matter identical to the event surface or the title is no useful pair.
        if concerns:
            _cl = concerns.strip().lower()
            if _cl == event or (title and _cl == title.strip().lower()):
                concerns = None

        # DEFER TO THE NAMING SEAM (grammatical, not a word-list): "I have a dog named Rex"
        # parses as have→dobj(dog) with a NAMING-verb modifier ("named") on the object — that is a
        # POSSESSION/naming construction the naming seam (_detect_naming_states/analyze_naming)
        # OWNS, binding (dog, also_known_as, Rex); emitting (user, participated_in, dog) here
        # would be bogus. BUT "I attended a workshop called X" is a genuine OCCURRENCE whose
        # acl-naming participle is the event's TITLE (captured above), and should ride
        # also_known_as on the event TYPE.
        #
        # The discriminator is the SUPPORT VERB's lexical aspect, both bounded grammatical
        # classes already in _LVC_SUPPORT_VERB_LEMMAS — NOT a domain list:
        #   • POSSESSION support ("have"/"get") + a noun carrying a naming participle → the
        #     possession/naming reading; DEFER to the naming seam (drop the occurrence).
        #   • OCCURRENCE support ("attend"/"go"/"take"/"do"/"make") + a naming participle → the
        #     participle TITLES the event; KEEP the occurrence and ride the title.
        # Naming-verb VOCABULARY is the DB-grown per-tenant set ∪ code-fallback.
        _naming = _naming_verbs()
        _has_naming_child = any(
            (gc.lemma_ or "").strip().lower() in _naming
            for gc in event_noun.children
        )
        _support_lemma = (tok.lemma_ or "").strip().lower()
        if _has_naming_child and _support_lemma in ("have", "get"):
            return None

        # EVENT-TYPE EXCLUDES the lifted Title-Case compound premodifier (the "Apollo" in "the
        # Apollo webinar"): when the title was lifted from a leading Title-Case PROPN compound
        # that is embedded in the bare-type phrase, strip it so the occurrence object stays the
        # bare place ("webinar"), not "apollo webinar". Structural surface strip only — if the
        # title did not come from a premod (quoted/appos/acl), event is already the bare type.
        if title:
            _tl = title.strip().lower()
            if event.startswith(_tl + " "):
                _stripped = event[len(_tl):].strip()
                if _stripped:
                    event = _stripped
        # STATE-LANE SIGNAL (Stage 3): the eventive head is a SEMANTICALLY-EMPTY PROBLEM noun (lemma in
        # the problem_noun cue class — grown per-tenant + the cold-tenant floor) AND a with-PP affected
        # entity is present (``concerns``). This is the "I had an issue WITH my car's GPS system" shape:
        # the meaning lives in the affected entity, not the empty head. The deriver runs on a GRAMMAR-ONLY
        # doc (no GLiNER2 types, and spaCy NER does NOT tag "gps system"), so the affected-thing test is
        # GRAMMATICAL, never type-table-dependent:
        #   • problem_head            — DOBJ lemma ∈ problem_noun (so "had a MEETING/LUNCH/CONVERSATION
        #                               with X" — eventive but not a problem — never fires).
        #   • _affected_is_social     — the with-PP pobj is a PERSON/ORG (spaCy NER) or a person-role cue
        #                               (kinship_noun ∪ social_role): "a problem with my COWORKER Sam"
        #                               is interpersonal → NOT a device-issue → excluded.
        #   • _is_first_person_possessed — the pobj NP is possessed by the speaker ("MY car's GPS", "MY
        #                               router"): the concreteness/ownership signal that excludes an
        #                               ABSTRACT topic ("a problem with the traffic / the math").
        # ``problem_affected_thing`` = NON-social ∧ 1st-person-possessed → the caller fires the competing
        # ``(<affected>, has_state, <problem>)`` candidate (the structural twin of ``feels``). NO word
        # zoo; fail-safe: any miss → flags stay False → the flat reading stands. ``problem_candidate``
        # (the growth signal) is queued whenever the affected entity is NON-social (even if not possessed).
        problem_head = False
        problem_candidate = None
        problem_affected_thing = False
        if concerns:
            try:
                _head_lemma = (event_noun.lemma_ or "").strip().lower()
                if _head_lemma and _head_lemma in _problem_nouns():
                    problem_head = True
                if _head_lemma and event_noun.pos_ == "NOUN":
                    _pobj = _with_pp_pobj_token(event_noun)
                    _social = _affected_is_social(_pobj, concerns)
                    if not _social:
                        # Queue the head for freq-gated problem_noun growth (the carve-out growth signal):
                        # a "had a <bland-head> with <non-social X>" the cue class hasn't learned yet.
                        problem_candidate = _head_lemma
                        if _is_first_person_possessed(_pobj):
                            # NON-social ∧ speaker-possessed concrete thing → device-issue. Fire-eligible.
                            problem_affected_thing = True
            except Exception:  # noqa: BLE001 — fail-safe: lemma/overlay miss → not a problem head
                problem_head = False
                problem_candidate = None
                problem_affected_thing = False
        return EventAnalysis(event=event, title=title, concerns=concerns,
                             negated=negated, problem_head=problem_head,
                             problem_candidate=problem_candidate,
                             problem_affected_thing=problem_affected_thing)
    except Exception as e:  # noqa: BLE001 — fail-safe
        log.warning("linguistics.build_event_analysis_failed", error=str(e)[:160])
        return None


def analyze_events(text: str, dated: bool = False) -> list:
    r"""Deterministic capture of ALL light-verb + eventive-noun occurrences in ``text`` (T1 PRIMARY).
    Returns a ``list[EventAnalysis]`` (possibly empty) — one entry per distinct eventive head.

    ``dated`` (default False) — the CALLER's signal that this clause carried a real, peeled
    ``event_date``. It relaxes the present-simple stative-possession gate on the LVC direct-object
    lane (a DATED present-tense event — "I have a meeting on Jan 15" — is a genuine occurrence),
    while a present-simple UNDATED direct-object clause ("I have a car") is dropped as possession.
    The date is peeled UPSTREAM (out of ``text``), so the gate cannot see it here — the caller,
    which HAS the peeled date, passes ``dated=bool(iso)``.

    THE RULE (subject-agnostic, dependency-driven — NO event word-list):
      Find a SUPPORT verb (lemma in the bounded LVC support-verb class) whose subject
      (``nsubj``/``nsubjpass``) is a genuine 1st-person personal pronoun ("I"/"we"), then enumerate
      EVERY eventive head it governs via ``_collect_eventive_heads`` (dobj NOUN ∪ governed-prep
      NOUN/PROPN pobj ∪ NOUN conj siblings — covering "like/such as/including X" structurally, no
      preposition list) and analyze EACH via ``_build_event_analysis``. spaCy structures the targets:

        "I had a dentist visit"  → "had" (lemma have) is ROOT; "visit" is its ``dobj``/``obj``.
        "I went to a concert"    → "went" (lemma go) is ROOT; "concert" is the ``pobj`` of "to".
        "I attended a workshop"  → "attended" (lemma attend) is ROOT; "workshop" is ``dobj``.

      THE Q2 GAP THIS CLOSES: "I've been attending various workshops, like the workshop on 'Effective
      Time Management'." now yields the GENERIC head ("various workshops") AND the appositive titled
      occurrence ("workshop", title 'Effective Time Management') — the old single-head path dropped
      the second. "I attended a workshop on 'X', and a webinar on 'Y'." yields TWO titled occurrences.

    Each ``EventAnalysis`` is the (type, title, concerns, negated) tuple for its head — NO rel-type or
    taxonomy decision (strong ingest, lean query). De-dup is by the analyzed event PHRASE so the same
    bare type captured twice (e.g. dobj + a redundant conj) is not double-counted; a distinct title
    on the same bare type is kept (it is a distinct occurrence). Returns ``[]`` when there is no LVC,
    no eventive head, or on any failure (fail-safe). Negated occurrences ARE returned (with
    ``negated=True``) — the caller skips them, matching the single-head behavior.

    Subject-agnostic + grammatical: the only closed sets are the SAME LVC support-verb / naming-verb
    grammatical classes the single-head path used; quotes are a language primitive; date-skip reuses
    the shared detector. NO preposition list, NO event/domain word zoo."""
    doc = _parse(text)
    if doc is None:
        return []
    out: list = []
    seen_keys: set = set()
    try:
        _lvc = _lvc_support_verbs()  # per-tenant grown set (overlay) ∪ code-fallback; resolved once
        for tok in doc:
            if (tok.lemma_ or "").strip().lower() not in _lvc:
                continue
            if tok.pos_ not in ("VERB", "AUX"):
                continue
            # Subject must be a genuine 1st-person personal pronoun ("I"/"we") — the participant.
            subj_self = any(
                c.dep_ in ("nsubj", "nsubjpass") and _is_first_person_personal_pronoun(c)
                for c in tok.children
            )
            if not subj_self:
                continue
            for _head in _collect_eventive_heads(tok, text, dated=dated):
                _ea = _build_event_analysis(_head, tok, text)
                if _ea is None:
                    continue
                # Dedup by (event-phrase, title) so a redundant duplicate head does not double up,
                # while a distinct title on the same bare type stays a distinct occurrence.
                _k = ((_ea.event or "").strip().lower(), (_ea.title or "").strip().lower())
                if _k in seen_keys:
                    continue
                seen_keys.add(_k)
                out.append(_ea)
    except Exception as e:  # noqa: BLE001 — fail-safe
        log.warning("linguistics.analyze_events_failed", error=str(e)[:160])
        return out
    return out


def analyze_event(text: str):
    r"""BACK-COMPAT SHIM — first-element view of ``analyze_events`` (returns ``EventAnalysis`` | None).

    The original contract: a deterministic first-cut for a light-verb + eventive-noun construction.
    Now backed by the multi-occurrence ``analyze_events`` so the FIRST eventive head is returned
    EXACTLY as before (single-event behavior preserved bit-for-bit: one head → that one occurrence).
    Callers that need every occurrence in a clause (the event-state seams) use ``analyze_events``.
    Returns ``None`` when there is no LVC / no eventive head / on any failure (fail-safe)."""
    _all = analyze_events(text)
    return _all[0] if _all else None


def analyze_inchoative(text: str):
    r"""Deterministic first-cut for an INCHOATIVE / ingressive construction — "I started <item>",
    "I began <item>". Returns ``EventAnalysis`` | ``None``.

    THE GAP THIS CLOSES (q7 LongMemEval): "I started some marigold seeds … on March 3rd" / "I have
    been starting seeds … since February 20th" carries a real ``event_date`` but produces NO host
    edge — ``analyze_event`` only fires on the LIGHT-VERB class (a support verb governing an EVENTIVE
    NOUN, "had a visit"), whereas here the EVENT meaning is carried by the VERB itself (ingressive
    aspect — "start"/"begin" marks the BEGINNING of an activity) and the direct object is the ITEM
    being started. So the date had nothing to attach to and the dated start was dropped, making
    "which seeds were started first?" un-answerable.

    THE RULE (subject-agnostic, dependency-driven — NO event/item word-list):
      Find an INCHOATIVE verb (lemma in the bounded, DB-grown ``_inchoative_verbs()`` aspectual class)
      whose subject (``nsubj``/``nsubjpass``) is a genuine 1st-person personal pronoun ("I"/"we"),
      directly governing a CONCRETE DIRECT-OBJECT NOUN (the thing being started). The item NP is
      recovered structurally via ``_np_phrase`` (head + left compound/amod) so "marigold seeds" keeps
      its modifier. The caller emits ``(user, participated_in, <item>)`` + ``event_date`` — the SAME
      action-occurrence backbone the residue classifier emits for "washed my car" (an ACTION on a
      thing), but DETERMINISTICALLY (no LLM).

    OVER-CAPTURE GUARDS (all grammatical — pos_/dep_, NO word-list):
      • The object must be a NOUN. "I started crying" / "I started to think" parse the complement as
        an ``xcomp`` VERB → REJECTED (no dated activity-on-a-thing). spaCy sometimes labels the
        nominal complement of a progressive ingressive ("been starting seeds") as ``xcomp`` rather
        than ``dobj``; we accept ``dobj``/``obj``/``xcomp`` ONLY when that token's pos_ is NOUN, so the
        verb-complement (crying/think) is still excluded.
      • Negation is read deterministically (a ``neg`` dep on the verb); the caller skips a negated
        start (absence modeling deferred). This NEVER touches the negation/correction gate — a negated
        inchoative simply returns ``negated=True`` and is dropped here.

    The caller is responsible for the DATE (peeled upstream, exactly as for ``analyze_event``); a
    dateless inchoative still returns the item but the caller's residue/date gate decides whether to
    keep it. Makes NO rel-type/taxonomy decision (strong ingest, lean query). Fail-safe → ``None``."""
    doc = _parse(text)
    if doc is None:
        return None
    try:
        _inch = _inchoative_verbs()  # per-tenant grown aspectual set (overlay) ∪ code-fallback
        for tok in doc:
            if (tok.lemma_ or "").strip().lower() not in _inch:
                continue
            if tok.pos_ != "VERB":
                continue
            # Subject must be a genuine 1st-person personal pronoun ("I"/"we") — the doer.
            subj_self = any(
                c.dep_ in ("nsubj", "nsubjpass") and _is_first_person_personal_pronoun(c)
                for c in tok.children
            )
            if not subj_self:
                continue
            # Direct object NOUN (the item). Accept dobj/obj, and an xcomp ONLY when it is a NOUN
            # (the progressive-ingressive "been starting seeds" parse). A VERB complement
            # ("crying"/"think") is excluded by the pos_ == NOUN guard → no dated activity-on-a-thing.
            item_tok = None
            for c in tok.children:
                if c.dep_ in ("dobj", "obj") and c.pos_ == "NOUN":
                    item_tok = c
                    break
            if item_tok is None:
                for c in tok.children:
                    if c.dep_ == "xcomp" and c.pos_ == "NOUN":
                        item_tok = c
                        break
            if item_tok is None:
                continue
            item = _np_phrase(item_tok)
            if not item or len(item) < 3:
                continue
            negated = any(c.dep_ == "neg" for c in tok.children)
            # CROP/ITEM ANCHORS (Q8): surface the SPECIFIC crop(s) the start is about so the dated
            # occurrence is reachable from the crop operand a comparison asks about ("tomatoes" /
            # "marigolds"), not just the generic head ("seeds"). THE HARD LINE: each anchor is the
            # crop TYPE; the dated start is the event ABOUT it. Empty when no specific crop is present.
            anchors = _inchoative_crop_anchors(tok, item_tok, text)
            return EventAnalysis(event=item, title=None, concerns=None,
                                 negated=negated, anchors=anchors)
    except Exception as e:  # noqa: BLE001 — fail-safe
        log.warning("linguistics.analyze_inchoative_failed", error=str(e)[:160])
        return None
    return None


# ── ACQUISITION / TRANSFER-OF-POSSESSION — "I got a Samsung Galaxy S22 at the mall on Feb 20." ────
# THE WHY (Q4 capture wall): an acquisition is a CROSS-SECTION (who got / what / where / when). The
# PRIMARY binding is the user COMING TO POSSESS the device, but the light-verb occurrence seam
# (``analyze_events``) treats "got" as an LVC support verb, REJECTS the device dobj as "possession not
# an event" (the PROPN/possession POS rule), and PROMOTES the locative prep-pobj ("mall"/"store") to a
# ``participated_in`` occurrence — so the user→device ownership linkage is never EXPOSED as a dated,
# comparable fact and the locative steals the event slot. This deriver recovers the acquisition as the
# transfer-of-possession it is and lets the caller EXPOSE the inferred ``(user, owns, <device>)`` edge
# (Class B, dated), the device's clean alias, and the locative kept as a co-bound LOCATION — never
# suppressed, never promoted. It mirrors ``analyze_inchoative`` (a verb-class lane that the caller
# turns into a backbone edge), but the rel is OWNS (transfer of possession), not participated_in.


@dataclass(frozen=True)
class AcquisitionAnalysis:
    """A deterministic reading of a transfer-of-possession (acquisition) construction
    ("I got a Samsung Galaxy S22 at the mall.", "I bought a Dell XPS 13.").

    - ``device``     : the acquired-object NP, lowercased and CLEANED of a leading generic ``amod``
                       ADJ determiner-cruft ("new") so the alias is the bare product name
                       ("samsung galaxy s22") — the OBJECT of the inferred owns linkage (a place in
                       L4 once typed). THE HARD LINE: this is the memory's object, never a date.
    - ``alias``      : an appositive PROPER-NAME run on the acquired object ("the Dell XPS 13"
                       appositive), surface-cased, when present — registered as an ``also_known_as`` of
                       the device so the comparison operand resolves by either surface. ``None`` when
                       there is no appositive.
    - ``location``   : a co-bound LOCATION the acquisition happened at — the ``pobj`` of an ``at``/
                       ``from``/``in`` prep on the verb ("at the mall" → "mall"), lowercased NP.
                       ``None`` when absent. THE HARD LINE: kept as WHERE, never promoted to the event
                       and never the owns object.
    - ``negated``    : True when a ``neg`` dependency hangs off the verb ("I didn't get a phone") —
                       the caller skips a negated acquisition (absence modeling deferred).
    """
    device: str
    alias: str | None
    location: str | None
    negated: bool


def _acquisition_object_phrase(tok, exclude_subtree_of=None) -> str:
    """The acquired-object NP for a device dobj ``tok``, CLEANED of determiner / leading evaluative-ADJ
    cruft.

    ``exclude_subtree_of`` (a token or ``None``): when set, tokens that fall under THAT token's
    subtree are pruned from the product-name walk. Used by the RELATIVE-CLAUSE acquisition branch:
    the device is the ANTECEDENT, so its subtree CONTAINS the whole relative clause (the acquisition
    verb, subject and the relative-time modifier) — without this prune the date's NUM/PROPN tokens
    ("3 weeks ago", "Feb 20") would leak into the device name. Excluding the acquisition verb's
    subtree restores canonical parity (date is a sibling of the dobj, never inside it). Default
    ``None`` → byte-identical to the canonical dobj path.

    A product/device name is a multi-token compound of brand/model PROPN/NUM tokens that spaCy may
    chain on EITHER side of the head ("Samsung →compound Galaxy →compound S22[dobj]" — a LEFT nested
    compound chain; "Dell →compound XPS[dobj] 13←nummod" — a RIGHT nummod). The bare ``_np_phrase``
    only keeps the head's DIRECT-left compound/amod children, so it loses the nested brand ("Samsung")
    and a right-side model number ("13"). We instead walk the head's SUBTREE and keep the contiguous
    PRODUCT-NAME tokens — those whose dep is a NAME-forming dependency (``compound``/``nummod``/
    ``flat``/``nmod``) OR whose POS is PROPN/NUM — dropping determiners and a leading evaluative
    ``amod`` ADJ ("new"/"old") that is determiner-cruft a comparison operand must not carry.

    When the head is a PLAIN common noun with NO PROPN/NUM name tokens ("a red bike"), we fall back to
    ``_np_phrase`` so descriptive amods are preserved (no product-name strip applies). Tokens are
    emitted in source order, lowercased. Fail-safe → ``_np_phrase``."""
    try:
        _excluded: set = set()
        if exclude_subtree_of is not None:
            try:
                _excluded = {t.i for t in exclude_subtree_of.subtree}
            except Exception:  # noqa: BLE001 — fail-open (no prune)
                _excluded = set()
        kept = [tok]
        head_named = tok.pos_ in ("PROPN", "NUM")
        for d in tok.subtree:
            if d.i == tok.i:
                continue
            if d.i in _excluded:
                continue
            if d.pos_ == "DET" or d.dep_ == "det":
                continue
            # a NAME-forming dependency token, or any PROPN/NUM in the head's subtree = part of the
            # product name (brand / model / number). Evaluative ADJ amods and the locative are excluded.
            if d.dep_ in ("compound", "nummod", "flat", "nmod") or d.pos_ in ("PROPN", "NUM"):
                kept.append(d)
                if d.pos_ in ("PROPN", "NUM"):
                    head_named = True
        if not head_named:
            return _np_phrase(tok)  # ordinary common-noun possession — keep descriptive amods
        # contiguous source-order span (dedup by token index)
        seen_i: set = set()
        ordered = []
        for m in sorted(kept, key=lambda m: m.i):
            if m.i in seen_i:
                continue
            seen_i.add(m.i)
            ordered.append(m.text)
        out = " ".join(p.strip() for p in ordered if p and p.strip()).lower()
        return out or _np_phrase(tok)
    except Exception:  # noqa: BLE001 — fail-safe
        return _np_phrase(tok)


def analyze_acquisition(text: str):
    r"""Deterministic first-cut for a TRANSFER-OF-POSSESSION (acquisition) construction.
    Returns ``AcquisitionAnalysis`` | ``None``.

    THE RULE (subject-agnostic, dependency-driven — the ONLY closed set is the FLAGGED, DB-grown
    ``_acquisition_verbs()`` lexical class; everything else is grammar):
      Find an ACQUISITION verb (a transfer-of-possession lemma) whose subject (``nsubj``/``nsubjpass``)
      is a genuine 1st-person personal pronoun ("I"/"we"), directly governing a CONCRETE DIRECT-OBJECT
      NOUN/PROPN (the thing acquired → the subject's new possession). The caller emits the inferred
      ``(user, owns, <device>)`` linkage + the acquisition ``event_date`` — the SAME ownership backbone
      the possessive seam emits for "my phone", but for a DATED acquisition EVENT, and EXPOSED as an
      explicit Class-B fact (never silent).

    WHY THE OBJECT POS IS WIDER HERE THAN IN ``_collect_eventive_heads`` (the deliberate divergence the
    Q4 fix turns on): the occurrence seam rejects a PROPN dobj of get/have as "possession, not an
    event" — CORRECT for an OCCURRENCE, but the possession IS exactly what an acquisition wants. So
    this lane ADMITS the PROPN/typed-entity possession dobj (the device the occurrence seam discards),
    and the caller suppresses the occurrence seam's locative-as-event promotion for this clause.

    OVER-CAPTURE GUARDS (all grammatical — pos_/dep_, NO word-list):
      • The object must be a NOUN/PROPN. "I got to leave" / "I got tired" parse the complement as an
        ``xcomp`` VERB / an ``acomp`` ADJ → REJECTED (no possession).
      • An eventive-noun dobj of get ("I got a haircut"/"a massage") is a SERVICE occurrence, not a
        durable possession. We do NOT special-case event nouns here (no event word-list); the caller's
        occurrence seam still captures those, and the ownership of an abstract service edge is harmless
        + low-confidence (Class B). The device case (a typed PROPN/compound product) is the target.
      • Negation read from a ``neg`` dependency; the caller skips a negated acquisition.

    The DATE is peeled by the caller (exactly as for ``analyze_event``/``analyze_inchoative``); a
    dateless acquisition still exposes the owns linkage (undated). Makes NO rel-type/taxonomy decision
    (strong ingest, lean query). Fail-safe → ``None``."""
    doc = _parse(text)
    if doc is None:
        return None
    try:
        _acq = _acquisition_verbs()  # FLAGGED per-tenant grown transfer-verb class ∪ code-fallback
        for tok in doc:
            if (tok.lemma_ or "").strip().lower() not in _acq:
                continue
            if tok.pos_ not in ("VERB", "AUX"):
                continue
            subj_self = any(
                c.dep_ in ("nsubj", "nsubjpass") and _is_first_person_personal_pronoun(c)
                for c in tok.children
            )
            if not subj_self:
                continue
            # Direct object that becomes the possession — NOUN or PROPN (the device). A VERB xcomp
            # ("got to leave") or an ADJ acomp ("got tired") is excluded by the pos_ guard.
            obj_tok = None
            for c in tok.children:
                if c.dep_ in ("dobj", "obj") and c.pos_ in ("NOUN", "PROPN"):
                    obj_tok = c
                    break
            # RELATIVE-CLAUSE ACQUISITION (parity with the canonical "I got <X> <when>"). In
            # "<X> that I got a month ago" / "<X> I bought last week" the acquisition verb is
            # STRANDED inside a relative clause modifying its antecedent: the verb's dep is
            # ``relcl``/``acl`` and its ``.head`` IS the acquired NOUN/PROPN (the lens / camera —
            # the relative pronoun, if any, is the gapped dobj, never a concrete possession). The
            # ANTECEDENT is therefore the device, and the relative-time modifier ("a month ago")
            # sits INSIDE this relcl on the verb → the caller's whole-clause date peel already
            # carries it, so parity with the canonical form is just binding the antecedent here.
            # Grammatical (dep-arc shape only, NO word-list); subject already gated to 1st-person.
            _relcl_verb = None  # set when the device is a relcl antecedent → prune its subtree
            if obj_tok is None and tok.dep_ in ("relcl", "acl"):
                _ante = tok.head
                if _ante is not None and _ante.pos_ in ("NOUN", "PROPN") and _ante.i != tok.i:
                    obj_tok = _ante
                    _relcl_verb = tok
            if obj_tok is None:
                continue
            # Prune the relative-clause verb's subtree (date / subject / gapped pronoun) so only the
            # antecedent's OWN product-name tokens survive; canonical dobj path passes None (no prune).
            device = _acquisition_object_phrase(obj_tok, exclude_subtree_of=_relcl_verb)
            if not device or len(device) < 2:
                continue
            # APPOSITIVE ALIAS: "I got a phone, the Samsung Galaxy S22" / a PROPN appositive run on the
            # device dobj → the product's proper name, registered as an alias of the device entity.
            alias = None
            for c in obj_tok.children:
                if c.dep_ == "appos" and c.pos_ in ("PROPN", "NOUN"):
                    _run = _np_phrase(c)
                    if _run and _run != device:
                        alias = (c.text or "").strip() or None
                        # prefer the full appositive surface NP when multi-token (e.g. "Dell XPS 13")
                        _full = " ".join(t.text for t in sorted(c.subtree, key=lambda t: t.i)
                                         if t.dep_ != "det").strip()
                        if _full:
                            alias = _full
                    break
            # LOCATION (co-bound WHERE — never the event, never the owns object): the pobj of an
            # at/from/in prep on the verb. Date pobjs are excluded via the shared date detector so
            # "on Feb 20" can never masquerade as a location. dep_/pos_ only — the prep SURFACE is read
            # only to scope to spatial prepositions (a closed grammatical primitive, like the SVO
            # particles), NOT a domain word-list.
            location = None
            try:
                _date_spans = _collect_date_spans(text)
            except Exception:  # noqa: BLE001
                _date_spans = []

            def _is_date_pobj(_g) -> bool:
                if not _date_spans:
                    return False
                try:
                    sub = list(_g.subtree)
                    g_lo = min(t.idx for t in sub)
                    g_hi = max(t.idx + len(t.text) for t in sub)
                    return any(_s < g_hi and (_s + len(_sp)) > g_lo for (_s, _sp) in _date_spans)
                except Exception:  # noqa: BLE001
                    return False

            for c in tok.children:
                if c.dep_ != "prep":
                    continue
                if (c.text or "").strip().lower() not in ("at", "from", "in"):
                    continue
                for gc in c.children:
                    if (gc.dep_ == "pobj" and gc.pos_ in ("NOUN", "PROPN")
                            and not _is_date_pobj(gc)):
                        _loc = _np_phrase(gc)
                        if _loc and _loc != device:
                            location = _loc
                            break
                if location:
                    break

            negated = any(c.dep_ == "neg" for c in tok.children)
            return AcquisitionAnalysis(device=device, alias=alias,
                                       location=location, negated=negated)
    except Exception as e:  # noqa: BLE001 — fail-safe
        log.warning("linguistics.analyze_acquisition_failed", error=str(e)[:160])
        return None
    return None


# ── CLEAN-SENTENCE DEPENDENCY DERIVER (ClausIE-lite) — "feed it the clean sentence" ─────────────
# THE WHY (root cause this seam fixes): the legacy harvest feeds spaCy the LLM's LEFTOVERS — the
# turn is run through ``_llm_detect_factbearing_spans`` → joined → ``_peel_dates_at_entry`` (date
# stripped) → THEN the spaCy lanes. spaCy never sees a clean sentence and the date is gone before it
# can bind to its governing verb. This deriver replaces that tower: given ONE clean sentence (the
# caller does the sentence split off the marker-bearing turn, no LLM detect, no reframe, no entry-peel
# first), it returns the FULL structured fact set for that sentence in a single dependency parse —
# SVO + possessive (relational/sortal split) + date→governing-verb (TempEval) + the four local rules.
#
# Subject-agnostic + dependency-driven. The only closed sets consulted are the SAME grammatical
# primitives the rest of this module already uses (the function-word POS set, the load-bearing SVO
# particle class, the LVC support-verb class, the naming-verb class) PLUS two thin DB-overlay cue
# classes (relational_noun for the genitive split, discourse_marker for the marker drop) — both on
# the SAME per-tenant linguistic_cue rail with an in-code fallback seed, NOT a hardcoded literal in
# logic. It makes NO entity-typing decision (GLiNER2 supplies the type; the deriver just tags the
# slot) and NO rel-type/routing decision the WGM gate doesn't get to dispose. ClausIE / LazyGraphRAG
# / TempEval-validated; the live spaCy parse shapes below were checked against the acceptance turns.

# Discourse markers that must NEVER seed a fact ("by the way" must not yield (i, have, way)). A small
# CLOSED grammatical class of pragmatic/discourse adverbials — a language primitive, NOT a domain list.
# DB-HELD + per-tenant + GROWABLE on the SAME rail (category='discourse_marker'); this frozenset is the
# DB-DOWN code-fallback seed. ``_discourse_markers()`` resolves the live set; membership is checked as a
# leading-substring of the sentence (the marker is sentence-initial: "by the way, …", "anyway, …").
_DISCOURSE_MARKERS: frozenset[str] = frozenset({
    "by the way", "anyway", "anyways", "actually", "honestly", "frankly",
    "to be honest", "in any case", "incidentally", "for what it's worth",
    "as it happens", "speaking of which", "that said", "on another note",
})


def _discourse_markers() -> frozenset[str]:
    """Resolve the per-tenant ACTIVE discourse-marker set via the overlay (ContextVar-bound to the
    request's tenant schema — the SAME binding the naming/LVC/relational-noun overlays use). Returns a
    frozenset of lowercased marker surface forms. Fail-safe: any import/read failure / unbound schema /
    empty resolution → the in-code ``_DISCOURSE_MARKERS`` code-fallback seed so a DB-down / pre-migration
    turn still drops the marker instead of confabulating a fact from it. Never empty."""
    try:
        from src.api import linguistic_cue_overlay  # deferred: avoid import cycle / hard dep
        dsn = os.environ.get("POSTGRES_DSN", "")
        cues = linguistic_cue_overlay.resolve_cues(
            dsn,
            linguistic_cue_overlay.rel_type_overlay.get_current_schema(),
            getattr(linguistic_cue_overlay, "DISCOURSE_MARKER_CATEGORY", "discourse_marker"),
        )
        if cues:
            return cues
        return _DISCOURSE_MARKERS
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the linguistic layer
        log.warning("linguistics.discourse_markers_resolve_failed", error=str(e)[:160])
        return _DISCOURSE_MARKERS


def _relational_nouns() -> frozenset[str]:
    """Resolve the per-tenant ACTIVE RELATIONAL-noun set via the overlay (ContextVar-bound to the
    request's tenant schema — the SAME binding the naming/LVC/svo overlays use). Returns a frozenset of
    lowercased relational/component/kinship noun lemmas. Fail-safe: any import/read failure / unbound
    schema / empty resolution → the in-code ``_BOOTSTRAP_RELATIONAL_NOUNS`` code-fallback seed (held in
    linguistic_cue_overlay) so the genitive split still works DB-down. Never empty. Mirrors
    ``_naming_verbs()`` exactly."""
    try:
        from src.api import linguistic_cue_overlay  # deferred: avoid import cycle / hard dep
        dsn = os.environ.get("POSTGRES_DSN", "")
        cues = linguistic_cue_overlay.resolve_relational_nouns(dsn)
        if cues:
            return cues
        return linguistic_cue_overlay._BOOTSTRAP_RELATIONAL_NOUNS
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the linguistic layer
        log.warning("linguistics.relational_nouns_resolve_failed", error=str(e)[:160])
        try:
            from src.api import linguistic_cue_overlay
            return linguistic_cue_overlay._BOOTSTRAP_RELATIONAL_NOUNS
        except Exception:  # noqa: BLE001
            return frozenset()


# Kinship relational nouns map to a kinship rel_type; component/part nouns map to part_of. The kinship
# membership that splits WHICH inherent relation a relational noun carries (kinship vs mereology) is now
# DB-HELD + per-tenant + GROWABLE on the SAME rail (category='kinship_noun'), resolved via the overlay
# below. The in-code seed lives in linguistic_cue_overlay._BOOTSTRAP_KINSHIP_NOUNS as the DB-DOWN
# code-fallback ONLY (it supersets nothing — it is the verbatim retired in-code list). A relational
# noun NOT in the kinship class defaults to ``part_of`` (component reading); a noun OUTSIDE the
# relational_noun class never reaches here (it gets the generic ``related_to`` sortal reading).


def _kinship_nouns() -> frozenset[str]:
    """Resolve the per-tenant ACTIVE KINSHIP-noun set via the overlay (ContextVar-bound to the
    request's tenant schema — the SAME binding the naming/LVC/relational-noun overlays use). Returns a
    frozenset of lowercased kinship noun lemmas. Fail-safe: any import/read failure / unbound schema /
    empty resolution → the in-code ``_BOOTSTRAP_KINSHIP_NOUNS`` code-fallback seed (held in
    linguistic_cue_overlay) so the genitive kinship/mereology split still works DB-down. Never empty.
    Mirrors ``_relational_nouns()`` exactly."""
    try:
        from src.api import linguistic_cue_overlay  # deferred: avoid import cycle / hard dep
        dsn = os.environ.get("POSTGRES_DSN", "")
        cues = linguistic_cue_overlay.resolve_kinship_nouns(dsn)
        if cues:
            return cues
        return linguistic_cue_overlay._BOOTSTRAP_KINSHIP_NOUNS
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the linguistic layer
        log.warning("linguistics.kinship_nouns_resolve_failed", error=str(e)[:160])
        try:
            from src.api import linguistic_cue_overlay
            return linguistic_cue_overlay._BOOTSTRAP_KINSHIP_NOUNS
        except Exception:  # noqa: BLE001
            return frozenset()


def _kinship_rel_map() -> dict:
    """Resolve the per-tenant kinship-noun → rel_type MAP via the overlay (the kinship_noun rows'
    ``description`` column: {noun: rel_type}). The SPECIFIC kin relation the HEAD noun plays toward the
    POSSESSOR ("my mother" → mother is the PARENT of me → parent_of; "my son" → child_of; "my wife" →
    spouse; grandparent/uncle/aunt/cousin → generic related_to). Metadata-driven, NOT an in-code
    literal. Fail-safe: any failure / empty → the ``_BOOTSTRAP_KINSHIP_REL_MAP`` code-fallback seed.
    Mirrors ``_thin_type_map()``."""
    try:
        from src.api import linguistic_cue_overlay  # deferred: avoid import cycle / hard dep
        dsn = os.environ.get("POSTGRES_DSN", "")
        m = linguistic_cue_overlay.resolve_kinship_rel_map(dsn)
        if m:
            return m
        return dict(linguistic_cue_overlay._BOOTSTRAP_KINSHIP_REL_MAP)
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the linguistic layer
        log.warning("linguistics.kinship_rel_map_resolve_failed", error=str(e)[:160])
        try:
            from src.api import linguistic_cue_overlay
            return dict(linguistic_cue_overlay._BOOTSTRAP_KINSHIP_REL_MAP)
        except Exception:  # noqa: BLE001
            return {}


def _unit_scalar_map() -> dict:
    """Resolve the per-tenant measurement-unit → scalar rel_type MAP via the overlay (the unit_scalar
    rows: {unit: rel_type}). Used by the copula measurement chain ("she is 62 years old" → unit
    'year' → age). Metadata-driven; a unit OUTSIDE the map mints no scalar (never a guessed
    measurement). Fail-safe: any failure / empty → the ``_BOOTSTRAP_UNIT_SCALAR_MAP`` code-fallback
    seed. Mirrors ``_thin_type_map()``."""
    try:
        from src.api import linguistic_cue_overlay  # deferred: avoid import cycle / hard dep
        dsn = os.environ.get("POSTGRES_DSN", "")
        m = linguistic_cue_overlay.resolve_unit_scalar_map(dsn)
        if m:
            return m
        return dict(linguistic_cue_overlay._BOOTSTRAP_UNIT_SCALAR_MAP)
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the linguistic layer
        log.warning("linguistics.unit_scalar_map_resolve_failed", error=str(e)[:160])
        try:
            from src.api import linguistic_cue_overlay
            return dict(linguistic_cue_overlay._BOOTSTRAP_UNIT_SCALAR_MAP)
        except Exception:  # noqa: BLE001
            return {}


def _kinship_gender_map() -> dict:
    """Resolve the per-tenant kinship-noun → gender MAP via the overlay (the kinship_gender rows'
    ``description`` column: {noun: gender}). The gender a kin role INTRINSICALLY carries ("son" →
    male, "daughter" → female); a GENDER-NEUTRAL role (child/parent/sibling/spouse/partner/cousin) is
    ABSENT so no gender is fabricated. Metadata-driven, NOT an in-code literal. Used by the unified
    named-instance binding chain to mint the SCALAR ``has_gender`` edge. Fail-safe: any failure /
    empty → the ``_BOOTSTRAP_KINSHIP_GENDER_MAP`` code-fallback seed. Mirrors ``_unit_scalar_map()``."""
    try:
        from src.api import linguistic_cue_overlay  # deferred: avoid import cycle / hard dep
        dsn = os.environ.get("POSTGRES_DSN", "")
        m = linguistic_cue_overlay.resolve_kinship_gender_map(dsn)
        if m:
            return m
        return dict(linguistic_cue_overlay._BOOTSTRAP_KINSHIP_GENDER_MAP)
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the linguistic layer
        log.warning("linguistics.kinship_gender_map_resolve_failed", error=str(e)[:160])
        try:
            from src.api import linguistic_cue_overlay
            return dict(linguistic_cue_overlay._BOOTSTRAP_KINSHIP_GENDER_MAP)
        except Exception:  # noqa: BLE001
            return {}


def _social_role_map() -> dict:
    """Resolve the per-tenant social-role-noun → rel_type MAP via the overlay (the social_role rows'
    ``description``: {noun: rel_type}). A PERSON social role (friend → friend_of, colleague → knows)
    the named-instance binding chain uses so a person introduced by a social role binds to a SOCIAL
    rel, never ``owns`` (a person is not owned) nor a bare ``has_role``. Metadata-driven, NOT an
    in-code literal. Fail-safe: any failure / empty → the ``_BOOTSTRAP_SOCIAL_ROLE_MAP`` code-fallback
    seed. Mirrors ``_kinship_gender_map()``."""
    try:
        from src.api import linguistic_cue_overlay  # deferred: avoid import cycle / hard dep
        dsn = os.environ.get("POSTGRES_DSN", "")
        m = linguistic_cue_overlay.resolve_social_role_map(dsn)
        if m:
            return m
        return dict(linguistic_cue_overlay._BOOTSTRAP_SOCIAL_ROLE_MAP)
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the linguistic layer
        log.warning("linguistics.social_role_map_resolve_failed", error=str(e)[:160])
        try:
            from src.api import linguistic_cue_overlay
            return dict(linguistic_cue_overlay._BOOTSTRAP_SOCIAL_ROLE_MAP)
        except Exception:  # noqa: BLE001
            return {}


def _role_noun_map() -> dict:
    """Resolve the per-tenant role-noun → rel_type MAP via the overlay (the role_noun rows'
    ``description``: {noun: rel_type}). CONVENTION (distinct from the kinship/social_role maps,
    which run FILLER→user): the value is the rel_type from the POSSESSOR (the user) TO the FILLER
    entity — ``employer → works_for`` reads "<Filler> is my employer" as (user, works_for, <Filler>).
    Used by the copula predicate-nominal role chain so "Globex Industries is my employer" binds the
    SUBJECT NP as the entity instead of minting (user, owns, "employer"). Metadata-driven (seeded
    migration 142, grown per-tenant), NOT an in-code literal. Fail-safe: any failure / empty → the
    ``_BOOTSTRAP_ROLE_NOUN_MAP`` code-fallback seed. Mirrors ``_social_role_map()``."""
    try:
        from src.api import linguistic_cue_overlay  # deferred: avoid import cycle / hard dep
        dsn = os.environ.get("POSTGRES_DSN", "")
        m = linguistic_cue_overlay.resolve_role_noun_map(dsn)
        if m:
            return m
        return dict(linguistic_cue_overlay._BOOTSTRAP_ROLE_NOUN_MAP)
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the linguistic layer
        log.warning("linguistics.role_noun_map_resolve_failed", error=str(e)[:160])
        try:
            from src.api import linguistic_cue_overlay
            return dict(linguistic_cue_overlay._BOOTSTRAP_ROLE_NOUN_MAP)
        except Exception:  # noqa: BLE001
            return {}


def _alias_predicate_map() -> dict:
    """Resolve the per-tenant phrasal-alias-predicate → licensing-particle MAP via the overlay (the
    alias_predicate rows' ``description``: {verb_lemma: particle}). Used by the third-party nickname/
    alias deriver chain so "she goes by Dee" (go→'by') / "he is known as Sammy" (know→'as') bind the
    proper name as an ``also_known_as`` alias of the coref'd person. The value is the licensing
    preposition the verb must govern (with a PROPN pobj) for the alias reading — the disambiguator that
    keeps a non-naming same-verb use ("go to work") out. Metadata-driven (seeded migration 146, grown
    per-tenant), NOT an in-code verb literal. Fail-safe: any failure / empty → the
    ``_BOOTSTRAP_ALIAS_PREDICATE_MAP`` code-fallback seed. Mirrors ``_role_noun_map()``."""
    try:
        from src.api import linguistic_cue_overlay  # deferred: avoid import cycle / hard dep
        dsn = os.environ.get("POSTGRES_DSN", "")
        m = linguistic_cue_overlay.resolve_alias_predicate_map(dsn)
        if m:
            return m
        return dict(linguistic_cue_overlay._BOOTSTRAP_ALIAS_PREDICATE_MAP)
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the linguistic layer
        log.warning("linguistics.alias_predicate_map_resolve_failed", error=str(e)[:160])
        try:
            from src.api import linguistic_cue_overlay
            return dict(linguistic_cue_overlay._BOOTSTRAP_ALIAS_PREDICATE_MAP)
        except Exception:  # noqa: BLE001
            return {}


def _possession_rel_for_type(type_lemma: str | None, instance_type_tag: str | None = None) -> str:
    """Resolve the POSSESSION rel_type that fits a common-noun TYPE, metadata-driven — the deriver-side
    twin of ``main._possession_rel_for_head_type`` (the SAME selection-by-metadata-specificity, reading
    the SAME rel_types overlay), kept here so the deriver stays self-contained (it cannot import main).

    "a dog named Rex" → the TYPE "dog" is an ANIMAL → the pet relation (``has_pet``); "a server
    Apollo" → "server" is an OBJECT → generic ownership (``owns``). We do NOT hardcode ``if
    type=='Animal'``: we scan the rel_types overlay for an ownership-CLASS rel (head admits Person/ANY)
    whose ``tail_types`` ADMIT this type, and pick the MOST SPECIFIC (a concrete tail match beats an
    ANY catch-all; among concretes the narrowest tail_types wins, so ``has_pet={Animal}`` beats
    ``owns={Animal,Object,…}`` for an animal). The TYPE's entity-class is recovered WITHOUT a GLiNER2
    injection: ``instance_type_tag`` (the NER label the binding's PROPER NAME carried — a named Animal
    instance ⟹ its kind is an animal kind), else the thin-type slot tag. Default → ``owns`` (the
    generic possession the WGM gate itself upgrades to has_pet if the object grounds Animal). Fail-safe:
    unknown type / metadata unreadable / any failure → ``owns``. Subject-agnostic, NO type list."""
    et = (instance_type_tag or "").strip().upper()
    if not et and type_lemma:
        # the thin-type slot tag for the bare type noun (gps system→device); a weak fallback only.
        et = (_thin_type_map().get((type_lemma or "").strip().lower()) or "").strip().upper()
    if not et:
        return "owns"
    try:
        from src.api import rel_type_overlay
        dsn = os.environ.get("POSTGRES_DSN", "")
        if not dsn:
            return "owns"
        meta = rel_type_overlay.resolve_current(dsn)
        if not isinstance(meta, dict):
            return "owns"
        # POSSESSION-CLASS ANCHOR (class identification by canonical membership — NOT domain logic):
        # we choose AMONG possession rels, so the class is named by its canonical seed members. This
        # keeps affective/preference rels whose tail merely overlaps out of the running.
        _POSSESSION_CLASS = ("owns", "has_pet")
        _best = None
        _best_specific = False
        _best_tail_width = None
        for _rt, _row in meta.items():
            if not isinstance(_row, dict) or _rt not in _POSSESSION_CLASS:
                continue
            if _row.get("is_hierarchy_rel"):
                continue
            _ht = [(_h or "").strip().upper() for _h in (_row.get("head_types") or [])]
            _tt = [(_t or "").strip().upper() for _t in (_row.get("tail_types") or [])]
            if not _tt:
                continue
            if not ((not _ht) or "ANY" in _ht or "PERSON" in _ht):
                continue
            _tail_specific = et in _tt
            _tail_any = "ANY" in _tt
            if not (_tail_specific or _tail_any):
                continue
            _tail_width = len([_t for _t in _tt if _t != "ANY"]) or len(_tt)
            if _tail_specific:
                if (not _best_specific) or (_best_tail_width is None or _tail_width < _best_tail_width):
                    _best, _best_specific, _best_tail_width = _rt, True, _tail_width
            elif _best is None:
                _best, _best_tail_width = _rt, _tail_width
        if _best:
            return _best
    except Exception as e:  # noqa: BLE001 — fail-safe: never break the deriver
        log.warning("linguistics.possession_rel_for_type_failed", error=str(e)[:160])
    return "owns"


def _inherent_relation_for_noun(noun_lemma: str) -> str:
    """Pick the inherent relation a RELATIONAL noun carries: kinship → the SPECIFIC kin rel_type from
    the per-tenant ``kinship_noun`` cue-class METADATA (the row's ``description``: mother→parent_of,
    son→child_of, wife→spouse, …; a kin with no exact 1-hop rel → ``related_to``); component/part/body
    → ``part_of``. Caller has ALREADY confirmed the noun is in the relational_noun overlay; this only
    splits kinship vs mereology and, for kinship, names the specific relation metadata-driven (NOT an
    in-code literal). Returns a rel_type token the WGM gate disposes."""
    n = (noun_lemma or "").strip().lower()
    if n in _kinship_nouns():
        # The SPECIFIC kin rel from the kinship_noun row's mapped rel_type (metadata-driven). A kin in
        # the set but missing a mapping (DB-down / mis-seeded) falls to generic related_to so the walk
        # still resolves it — never a wrong fabricated rel.
        return _kinship_rel_map().get(n, "related_to")
    return "part_of"          # component / part / body-part mereology


def _rel_head_types(rel_type: str) -> tuple[str, ...]:
    """Resolve a rel_type's declared ``head_types`` from the rel_types overlay (per-tenant seed∪grown,
    ContextVar-bound schema). Returns an UPPERCASED tuple of admitted head-entity types, or ``()`` when
    the rel is unknown / metadata is unreadable. Metadata-driven (same overlay the WGM gate validates
    against), NOT an in-code constraint table — so a grown/edited rel's head_types are honored. Fail-safe:
    any failure → ``()`` (caller reads that as 'unknown → do not constrain', today's behavior)."""
    rt = (rel_type or "").strip().lower()
    if not rt:
        return ()
    try:
        from src.api import linguistic_cue_overlay  # deferred: avoid import cycle / hard dep
        dsn = os.environ.get("POSTGRES_DSN", "")
        if not dsn:
            return ()
        meta = linguistic_cue_overlay.rel_type_overlay.resolve_current(dsn)
        row = meta.get(rt) if isinstance(meta, dict) else None
        if not row:
            return ()
        hts = row.get("head_types") or []
        return tuple((h or "").strip().upper() for h in hts if (h or "").strip())
    except Exception as e:  # noqa: BLE001 — fail-safe: never break the linguistic layer
        log.warning("linguistics.rel_head_types_resolve_failed", rel_type=rt, error=str(e)[:160])
        return ()


def _scalar_rel_admits_subject(rel_type: str, subj_ent_type: str) -> bool:
    """Does a SCALAR measurement rel (age/height/weight) ADMIT this subject's GLiNER2 type as its head?

    The copula measurement chain mints a Person-scoped scalar (``height`` head_types={Person}, ``age``
    {Person}, …). On a NON-admitted subject ("the tomatoes are 2-3 inches tall" → subject typed OBJECT)
    the WGM gate would QUARANTINE that scalar to Class C (head_type_inconsistent) — a dead edge that
    ALSO displaces the viable relational ``has_state`` capture (Fix 3 over-reach: the crop loses its
    only walkable identity). So the chain must STEP ASIDE when the subject's type is incompatible.

    Returns True (emit the scalar, today's behavior) when:
      • the subject carries NO GLiNER2 type (empty ent_type — e.g. a bare pronoun "she"/"he" or an
        un-typed raw-str parse: the tests' Person cases), OR
      • the rel's head_types are unknown / unconstrained (``()`` / contains 'ANY'), OR
      • the subject's type IS in the rel's head_types.
    Returns False (step aside → let ``has_state`` capture the entity relationally) ONLY when the subject
    has a CONCRETE type that the CONCRETE head_types set does not admit. Grammatical + metadata-driven
    (GLiNER2 type ∧ overlay head_types), subject-agnostic, NO entity-type word list."""
    et = (subj_ent_type or "").strip().upper()
    if not et:
        return True  # untyped subject → unchanged (the person-pronoun/raw-str path the tests cover)
    hts = _rel_head_types(rel_type)
    if not hts or "ANY" in hts:
        return True  # unconstrained rel → unchanged
    return et in hts


@dataclass(frozen=True)
class DiscourseTopic:
    """The salient PRIMARY entity of an ingest turn — established from its FIRST sentence and used to
    resolve cross-sentence anaphora in the LATER sentences of the SAME turn (deterministic, subject-
    agnostic). It is NOT a memory (never emitted as a fact); it is the discourse-structure anchor a
    later sentence's subject pronoun / definite type-NP co-refers to so all the sentences consolidate
    onto ONE entity instead of islanding on the wrong local subject.

    - ``surface``     : the topic entity surface, lowercased (a NOUN/PROPN NP, or ``"user"`` for a
                        1st-person sentence-1 subject). This is what a resolved anaphor rebinds to.
    - ``gliner_type`` : the topic's COARSE GLiNER2 type (upper: ``PERSON``/``ORGANIZATION``/… ) or
                        ``None``. The type-compatibility GATE: an anaphor binds only when it agrees
                        with this type (``it``/``this`` ⇎ a PERSON topic; ``he``/``she`` ⇒ a PERSON
                        topic; a definite type-NP head must share this coarse type or be the topic's
                        type noun). This is what stops "the lesion" binding to a PERSON patient.
    - ``type_nouns`` : the topic's TYPE-noun surfaces (from its ``instance_of``/``subclass_of`` /
                        copula complement — "vulnerability", "request-forgery vulnerability"), plus
                        each phrase's head word. A definite NP whose head is in this set is the topic's
                        own type restated ("the vulnerability") → a strong co-reference signal.

    Built by ``discourse_topic_from_doc``. Absent / ambiguous (coordinated competing subjects) → the
    caller passes ``None`` and NO cross-sentence rebinding happens (today's per-sentence behavior)."""
    surface: str
    gliner_type: str | None = None
    type_nouns: frozenset = frozenset()


@dataclass(frozen=True)
class SentenceFact:
    """One structured fact derived from a single clean sentence by ``derive_sentence_facts``.

    - ``subject``               : subject surface, lowercased; a 1st-person self-ref → ``"user"``.
    - ``rel_type``              : the relation token (user's own verb lemma[+particle], or the
                                  possessive/appositive inherent relation). Snake_cased; the WGM
                                  gate disposes it (known → canonical, novel → grow).
    - ``object``                : object surface, lowercased (the thing / value / role / name).
    - ``event_date``            : ISO date bound to THIS fact's governing verb, or ``None``.
    - ``event_date_granularity``: "day"/"month"/"year" when dated, else ``None``.
    - ``thin_type``             : the object's IMMEDIATE kind ONE step up (gps system→device), or
                                  ``None`` — a slot tag only (GLiNER2 supplies real typing).
    - ``provenance``            : always "user_stated" (these are the user's own statements).
    - ``tentative``             : True when the deriver captured this fact but is NOT structurally
                                  confident it is a DURABLE fact — currently only an UNCERTAIN
                                  intransitive state (a present-tense activity/mental verb that
                                  passed the weak object-test but lacks the change-of-state cues).
                                  A tentative fact is CAPTURED-NOT-DROPPED but the consumer MUST
                                  route it to the short-term Class-C lane (store_context), NEVER a
                                  durable Class-B edge. It is promoted C→B only if it later grounds /
                                  converges as a state-type via the existing re_embedder machinery.
                                  Default False (every structurally-confident fact).
    - ``negated``               : True when this fact captures a NEGATED genuine STATE
                                  ("the GPS is not functioning", "the server is not down").
                                  This is the ASSERTION POLARITY (ConText/NegEx) of the state —
                                  NOT a correction/retraction (those are routed BEFORE the deriver
                                  by the intent gate and never reach here). A negated state is a
                                  DEFINITE, durable non-functional fact: it must read back NEGATED,
                                  never as its positive opposite. The consumer threads it onto the
                                  edge's ``polarity`` ('negated' vs the 'affirmed' default), exactly
                                  like ``event_date``/``temporal_status`` ride their own columns.
                                  Default False (every affirmed fact). Read deterministically from
                                  the spaCy ``neg`` dependency already computed at the state lanes —
                                  no word lists, no LLM.
    """
    subject: str
    rel_type: str
    object: str
    event_date: str | None
    event_date_granularity: str | None
    thin_type: str | None
    provenance: str
    tentative: bool = False
    negated: bool = False
    # SCALAR-ATTRIBUTE discipline: when set (e.g. "string"), this fact's OBJECT is a literal SCALAR
    # VALUE (an address / serial / employee-id span captured VERBATIM), NOT an entity to resolve. The
    # harvest threads this onto the edge's ``object_datatype`` so /ingest routes the value to
    # entity_attributes (the SCALAR storage path), never resolving it to a UUID. Default None (every
    # relational/hierarchical fact). Mirrors how event_date/negated ride their own fields.
    scalar_datatype: str | None = None
    # OBJECT-PRONOUN PROVENANCE (2c63a862 date-reattach). When this fact's OBJECT was coref-resolved
    # from a 3rd-person object PRONOUN ("…working with HER" → object "rachel"), this carries the
    # ORIGINAL pronoun surface ("her"). The harvest threads it onto the edge's ``object_pronoun`` so
    # the entry-peel date reattach (``_reattach_clause_dates``) can bind the peeled date to THIS edge:
    # its resolved object ("rachel") is NOT in the clause residue (which still says "her"), but the
    # PRONOUN is — matching by the pronoun is precise (only a pronoun-object edge, only to the clause
    # containing that pronoun; no over-bind on a named edge). Default None (object was a real surface).
    object_pronoun: str | None = None


# ── NAME↔TYPE BINDER vs ATTR-SCALAR PRECEDENCE ────────────────────────────────────────────────────
# The unified name↔type binding detector (analyze_name_type_bindings / analyze_named_instance and the
# deriver's binding chains) fires on the possessive-attribute copula ("my address is 123 Main Street,
# Riverton, Ontario") when LIVE GLiNER2 types the value-span head ("Street") as a NAMED INSTANCE of
# the attribute noun ("address") read as a TYPE. It then mints a cluster of junk TWINS — e.g.
#   (street, also_known_as|instance_of|has_role, address) + (user, owns, street) + (street, age, 123)
# alongside the attr-scalar chain's authoritative VERBATIM scalar edge (user, address, "123 main
# street, …" carrying object_datatype). The attr-scalar chain OWNS this construction; the binder twins
# compete with it. There is no in-binder precedence for this construction, so we suppress the twins at
# the union level: deterministic whole-word token membership against the claimed scalar VALUE.
#
# located_in is DELIBERATELY EXCLUDED from the twin-rel set: the geo-containment chain emits
# (123 main street, located_in, riverton) / (riverton, located_in, ontario) whose subjects/objects
# ARE whole-word fragments of the same value — but those edges are DESIRED and must survive. Only the
# binder's own twin rels are eligible to drop.
_NAME_TYPE_BINDER_TWIN_RELS = frozenset({
    "instance_of", "also_known_as", "has_role", "owns", "age", "has_state",
})

_BINDER_VALUE_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def _value_word_tokens(value) -> set:
    """Whole-word, lowercased token set of a scalar value string (split on any non-alphanumeric run).
    "123 Main Street, Riverton, Ontario" → {"123","main","street","riverton","ontario"}."""
    return {t for t in _BINDER_VALUE_TOKEN_RE.split((value or "").lower()) if t}


def suppress_name_type_binder_vs_attr_scalar(edges):
    """Union-level, deterministic, subject-agnostic suppression of name↔type-binder TWIN edges that
    collide with an attr-scalar claim over the SAME possessive-attribute copula.

    For every attr-scalar edge in ``edges`` (an edge carrying ``object_datatype`` — the verbatim
    scalar the deriver's attr-scalar chain captured), collect the whole-word token set of its claimed
    VALUE. Then DROP any other edge whose rel_type is a known binder twin
    (``_NAME_TYPE_BINDER_TWIN_RELS``) and whose SUBJECT or OBJECT shares a whole-word token with that
    claimed value. ``located_in`` (the geo-containment chain) is not in the twin set, so the geo edges
    — which legitimately share value words like "riverton"/"ontario" — are always preserved.

    FAIL-SAFE: no attr-scalar claim in the batch → returns the edge list unchanged. The attr-scalar
    edge itself (it carries ``object_datatype``) is never dropped. Pure function, no I/O, no fuzzy
    scoring — exact normalized-token membership only."""
    try:
        claimed_tokens: set = set()
        for e in edges:
            if e.get("object_datatype"):
                claimed_tokens |= _value_word_tokens(e.get("object"))
        if not claimed_tokens:
            return list(edges)  # no scalar claim → no change
        kept = []
        for e in edges:
            rel = (e.get("rel_type") or "").strip().lower()
            # Never drop the scalar claim itself; only the binder's twin rels are eligible.
            if rel in _NAME_TYPE_BINDER_TWIN_RELS and not e.get("object_datatype"):
                if ((_value_word_tokens(e.get("subject")) & claimed_tokens)
                        or (_value_word_tokens(e.get("object")) & claimed_tokens)):
                    continue
            kept.append(e)
        return kept
    except Exception:  # noqa: BLE001 — fail-safe: a suppression miss never drops real edges
        return list(edges)


def _strip_leading_discourse_marker(sentence: str) -> str:
    """Drop a sentence-INITIAL discourse marker ("by the way, …", "anyway …") so no fact is minted
    from it. Returns the residue (the marker + its trailing comma/space removed), or the sentence
    unchanged when it carries no leading marker. Surface, deterministic, fail-safe."""
    try:
        s = (sentence or "").strip()
        if not s:
            return sentence
        low = s.lower()
        best = None
        for mk in _discourse_markers():
            mk = (mk or "").strip().lower()
            if not mk:
                continue
            if low.startswith(mk):
                # only treat as a marker when followed by a boundary (comma / space / end), so a
                # marker that is a genuine prefix of a content word ("actually" vs "actual") is safe.
                tail = s[len(mk):]
                if tail == "" or tail[:1] in (",", " ", ";", ":", "."):
                    if best is None or len(mk) > len(best):
                        best = mk
        if best is None:
            return s
        residue = s[len(best):].lstrip(" ,;:.").strip()
        return residue if residue else s
    except Exception as e:  # noqa: BLE001 — fail-safe: never break the deriver
        log.warning("linguistics.strip_discourse_marker_failed", error=str(e)[:160])
        return sentence


def _thin_type_map() -> dict:
    """Resolve the per-tenant ACTIVE thin-type (surface→coarse-type) MAP via the overlay (ContextVar-
    bound to the request's tenant schema — the SAME binding the naming/LVC/relational-noun overlays
    use). Returns a {surface_lemma: type} dict. Fail-safe: any import/read failure / unbound schema /
    empty resolution → the in-code ``_BOOTSTRAP_THIN_TYPE_MAP`` code-fallback (held in
    linguistic_cue_overlay) so the slot tag still applies DB-down. Mirrors ``_relational_nouns()``."""
    try:
        from src.api import linguistic_cue_overlay  # deferred: avoid import cycle / hard dep
        dsn = os.environ.get("POSTGRES_DSN", "")
        m = linguistic_cue_overlay.resolve_thin_type(dsn)
        if m:
            return m
        return dict(linguistic_cue_overlay._BOOTSTRAP_THIN_TYPE_MAP)
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the linguistic layer
        log.warning("linguistics.thin_type_map_resolve_failed", error=str(e)[:160])
        try:
            from src.api import linguistic_cue_overlay
            return dict(linguistic_cue_overlay._BOOTSTRAP_THIN_TYPE_MAP)
        except Exception:  # noqa: BLE001
            return {}


def _thin_type_for_token(tok) -> str | None:
    """ONE-STEP immediate kind tag for an object head token (NO upward ladder). The deriver only
    LABELS the slot — GLiNER2 supplies real typing and WINS over this. We map a few high-signal
    device/system compound heads to their immediate kind ("gps system"/"system" → "device") so the
    acceptance shape carries a thin type; everything else returns ``None`` (the caller / GLiNER2
    fills it). The surface→type MAP is DB-HELD + per-tenant + GROWABLE (category='thin_type', resolved
    via ``_thin_type_map``); the in-code fallback is the overlay's bootstrap. Deterministic, exact
    lemma lookup (no fuzzy), surface-only, fail-safe. Intentionally MINIMAL — a slot tag, not a
    taxonomy."""
    try:
        head = (tok.lemma_ or tok.text or "").strip().lower()
        return _thin_type_map().get(head)
    except Exception:  # noqa: BLE001
        return None


def derive_sentence_facts(sentence, reference, prior_nps=None, dash_specifier_only=False,
                          named_role_only=False, discourse_topic=None, turn_persons=None):
    r"""Derive the FULL structured fact set for ONE clean sentence (ClausIE-lite, deterministic).

    Returns ``list[SentenceFact]``. This is the clean-sentence deriver the harvest's
    ``SENTENCE_PIPELINE`` wires to: the caller hands ONE clean sentence (already split off the
    marker-bearing turn — no LLM detect, no reframe, no entry-peel-first) plus the session
    ``reference`` (for relative/year-less date anchoring) and the ``prior_nps`` seen earlier IN THE
    SAME TURN (for intra-turn pronoun coref).

    ``sentence`` may be EITHER a ``str`` OR an already-parsed-and-GLiNER2-TYPED spaCy ``Doc``
    (gap-1 native seeding): the harvest builds ONE Doc (grammar parse + GLiNER2 spans written via
    ``set_ents``) and passes it here, so the deriver reads the authoritative GLiNER2 type straight
    off ``token.ent_type_`` — **GLiNER2 type WINS; empty ent_type_ → the one-step thin slot tag
    fills the gap** (the whole convergence rule, no post-hoc surface-match helper). A ``str`` is
    still accepted (parsed internally as today → no GLiNER2 types → pure thin-tag gap-fill) so any
    other caller/test keeps working. It runs a SINGLE dependency parse and composes:

      • SVO            — each content verb → (nsubj subject, verb lemma[+load-bearing particle], obj
                         NP). First-person subject → ``user``. (Reuses ``_svo_predicate_token`` /
                         ``_svo_object_head`` so the predicate/particle logic is identical to the
                         merge brain.)
      • POSSESSIVE     — ``my X`` (poss pronoun) → ``(user, owns, X)`` Class-B default;
                         ``X's Y`` (genitive) → relational/sortal split: a RELATIONAL/component/
                         kinship Y (overlay class) → its inherent relation (part_of / kinship-
                         related_to); a SORTAL Y → generic ``related_to`` (the walk resolves it).
      • DATE→VERB      — spaCy DATE NER ∪ numeric-date regex → dateparser(reference) → attach as
                         ``event_date`` to the fact whose VERB GOVERNS the date's PP (TempEval), never
                         an object. Multi-date → each binds to its nearest governing verb. Miss →
                         NULL, never wall-clock, never fabricated.
      • THIN TYPE      — object → its immediate kind ONE step (slot tag; GLiNER2 supplies real typing).
      • THE FOUR LOCAL RULES (single-turn-local, deterministic): intra-turn pronoun coref, conjunct/
        dash-list distribution (verb + date across each conjunct), appositive → ``has_role``, and the
        discourse-marker drop (handled at entry).

    Subject-agnostic, GLiNER2-pure, metadata-driven. ``provenance`` is always "user_stated". Returns
    ``[]`` when the layer is unavailable / the sentence is empty / on any failure (fail-safe → the
    caller keeps today's path)."""
    # ACCEPT a pre-built TYPED Doc (gap-1 native seeding) OR a raw str. A Doc already carries the
    # grammar parse AND GLiNER2's spans on ``token.ent_type_``; a str is parsed internally (no
    # GLiNER2 types → thin-tag gap-fill). Detect a Doc by its ``.text`` attr (avoids importing spacy).
    if sentence is None:
        return []
    if isinstance(sentence, str):
        if not sentence.strip():
            return []
        # (Rule 4) DISCOURSE-MARKER DROP — strip a leading marker BEFORE the parse so "by the way, I
        # had an issue" never yields a fact from "by the way" (no (i, have, way)).
        sentence = _strip_leading_discourse_marker(sentence)
        doc = _parse(sentence)
    else:
        # Already a typed Doc — the caller stripped markers + ran GLiNER2/set_ents before parsing.
        doc = sentence
    if doc is None:
        return []
    sentence = doc.text  # the deriver's date/offset logic operates on the parsed text
    if not sentence or not sentence.strip():
        return []
    out: list = []
    seen: set = set()

    # ── DATE SPANS for this sentence (positions in THIS sentence's text), each → its governing verb ──
    # We resolve each candidate span to a (verb_token, iso, gran) binding so a fact built on that verb
    # gets the date. spaCy attaches a temporal PP under the verb it modifies; we find the date span's
    # governing verb by walking the head chain of the token at the span's char offset.
    _date_by_verb: dict = {}   # verb_token.i → (iso, gran)
    _first_iso = _first_gran = None  # fallback: single-verb sentence with one date
    # ── DATE-SPAN TOKEN INDICES (PART 1 — atomizer-wobble robustness) ────────────────────────────
    # Every token whose char range OVERLAPS a RESOLVED date span. THE COMPOSITIONAL CAPTURE PRINCIPLE
    # in token form: a date is a PEELED component, so its tokens must NEVER be read as the verb's
    # load-bearing particle/prep NOR as the verb's object. This makes the SVO build robust to however
    # the LLM atomizer reworded the clause: whether it produced "I saw a house on 3/1" (date is a clean
    # PP adjunct) or "I see_on march 1st" (the atomizer absorbed "on" and lifted the date as the dobj),
    # the date tokens are EXCLUDED from predicate-folding and object-selection, so BOTH land
    # (user, see, house)@event_date — the date never folds into a ``see_on`` particle and "march 1st"
    # never becomes the relationship object. Subject-agnostic (no verb/noun literals): the EXCLUSION is
    # driven solely by which tokens the deterministic ``extract_event_date`` machinery resolved as a
    # date span. Indices are token ``.i`` on THIS doc; passed to ``_svo_*`` helpers as ``exclude_idx``.
    _date_token_idx: set = set()
    try:
        import dateparser as _dp  # noqa: F401 — presence probe; the resolver does the parsing
    except Exception:  # noqa: BLE001 — no dateparser → no dates (fail-safe)
        _dp = None
    if _dp is not None and reference is not None:
        try:
            for _start, _span in (_collect_date_spans(sentence) or []):
                # Resolve THIS span via the shared first-valid resolver semantics on the span alone,
                # so vague-month / weekday-relative / absolute-year anchoring all apply identically.
                _iso, _gran, _s2, _sp2 = _resolve_first_valid_date(_span, reference)
                if not _iso:
                    continue
                # Record EVERY token whose char range overlaps this date span — the whole "march 1st",
                # not just its first token — so the SVO peel excludes the entire date phrase.
                _span_end = _start + len(_span)
                for _t in doc:
                    _t_end = _t.idx + len(_t.text)
                    if _t.idx < _span_end and _t_end > _start:
                        _date_token_idx.add(_t.i)
                # Find the token at this span's start char, then climb to its governing VERB.
                _gov = None
                for _t in doc:
                    if _t.idx <= _start < _t.idx + len(_t.text):
                        _cur = _t
                        _hops = 0
                        while _cur is not None and _hops < 12:
                            if _cur.pos_ == "VERB":
                                _gov = _cur
                                break
                            if _cur.head is _cur:
                                break
                            _cur = _cur.head
                            _hops += 1
                        break
                if _gov is not None:
                    _date_by_verb.setdefault(_gov.i, (_iso, _gran or "day"))
                if _first_iso is None:
                    _first_iso, _first_gran = _iso, (_gran or "day")
        except Exception as e:  # noqa: BLE001 — date binding is best-effort; facts still emit undated
            log.warning("linguistics.derive_date_bind_failed", error=str(e)[:160])

    # ── DATE-VALUED ATTRIBUTE STATE (populated by the pre-pass below; consumed at ``_emit`` + the
    #    ``_chain_date_attribute`` chain). ``_date_attr_suppress`` holds the token indices of the
    #    ATTRIBUTE noun + the DATE complement of a "<owner>'s <noun> is <date>" / "<owner> has a <noun>
    #    of <date>" construction, so the other chains never mis-read the month-name PROPN / day-NUM /
    #    attribute noun as an entity/age (the date-value chain OWNS them). Empty unless the construction
    #    is present → zero behaviour change for every other sentence. ``_date_attr_binds`` carries the
    #    raw (owner_tok, attr_noun_tok, iso, gran) so the chain can resolve the owner via coref (defined
    #    later) and emit the normalized dated scalar. Kept minimal + fail-safe.
    _date_attr_suppress: set = set()
    _date_attr_suppress_surf: set = set()
    _date_attr_binds: list = []

    def _date_for_verb(verb_tok):
        """The (iso, gran) bound to this verb, or the sentence's sole date as a single-verb fallback.

        SCOPING FIX (SPEC §10.4): a date binds ONLY to the fact whose predicate IS the date span's
        governing verb. Callers that emit a verb-LESS fact (``verb_tok=None`` — the possessive
        ``owns``, the genitive ``part_of``/``related_to``, the appositive ``has_role``) carry no
        predicate the date could govern, so they NEVER receive a date — not even via the single-verb
        fallback. Otherwise "My car's GPS broke last week" wrongly stamps ``(user, owns, car)`` and
        ``(gps, part_of, car)`` with last-week's date. The fallback now also requires a verb_tok."""
        try:
            if verb_tok is None:
                return (None, None)
            if verb_tok.i in _date_by_verb:
                return _date_by_verb[verb_tok.i]
            # Single-verb sentence with one detected date and no precise PP attachment → bind it,
            # but ONLY to a verb-bearing fact whose verb IS that sole verb (governing-verb scope).
            if _first_iso is not None and len(_date_by_verb) == 0:
                _verbs = [t for t in doc if t.pos_ == "VERB"]
                if len(_verbs) == 1 and _verbs[0].i == verb_tok.i:
                    return (_first_iso, _first_gran)
        except Exception:  # noqa: BLE001
            pass
        return (None, None)

    # ── COVERED-TOKEN SET (gap-2 §10.3) ─────────────────────────────────────────────────────────
    # "Consume what you claim": every token a chain folds into an emitted fact is recorded here.
    # After all chains run, any CONTENT-bearing token (a real noun/proper-noun/content-verb) that is
    # neither covered nor accounted-for is the failure-residue — it MUST flow on to growth/C, never be
    # silently dropped. The deriver itself only EMITS facts; the residue check below is the fail-loud
    # guard that proves nothing content-bearing vanished. Indices are token .i values on THIS doc.
    _covered: set = set()

    def _claim(*toks):
        """Record the token(s) (and their compound/amod NP modifiers) a chain consumed."""
        for t in toks:
            if t is None:
                continue
            try:
                _covered.add(t.i)
                # the NP a chain folds in also covers its left compound/amod modifiers
                for c in t.children:
                    if c.dep_ in ("compound", "amod") and c.i < t.i:
                        _covered.add(c.i)
            except Exception:  # noqa: BLE001 — fail-safe: claim-tracking never breaks capture
                pass

    def _emit(subject, rel, obj, verb_tok=None, obj_tok=None, subj_tok=None, tentative=False,
              negated=False, scalar_datatype=None, distribute=True, object_pronoun=None):
        subj = (subject or "").strip().lower()
        rel = (rel or "").strip().lower()
        obj = (obj or "").strip().lower()
        # DATE-VALUED-ATTRIBUTE SUPPRESSION (single chokepoint). When a "<owner>'s <noun> is <date>" /
        # "<owner> has a <noun> of <date>" construction is present, its ATTRIBUTE noun (birthday /
        # "provision date") and its DATE complement (the month-name PROPN + day-NUM + year) are OWNED by
        # the date-value chain — no OTHER chain may bind those tokens as a standalone entity/age/name
        # (else "March"→instance_of/age, "date"→owns/related_to junk). The date-value chain itself emits
        # the owner (not in this set) + the normalized value, so it is never self-suppressed. Empty for
        # every sentence without the construction → zero behaviour change. Grammar-driven, fail-safe.
        for _tk in (subj_tok, obj_tok):
            if _tk is not None and _tk.i in _date_attr_suppress:
                return
        # SURFACE fallback: many chains (named-instance, measure, possessive) resolve a SURFACE and do
        # not pass the exact token, so the index guard alone misses "March"→instance_of/age /
        # "date"→owns junk. Drop a NON-scalar emit whose subject/object surface is a suppressed token
        # surface (the month/day/attribute words the date-value chain owns). Scoped to NON-scalar edges
        # so the date-value chain's OWN normalized scalar (which carries ``scalar_datatype``) survives.
        if _date_attr_suppress_surf and scalar_datatype is None:
            if subj in _date_attr_suppress_surf or obj in _date_attr_suppress_surf:
                return
        # RELATIVE-PRONOUN GUARD (THE HARD LINE — a function word is never a memory). When a chain's
        # subject/object token is a relative pronoun ("the brother WHO lives…", "the car THAT runs…")
        # the deriver would otherwise bind "who"/"that" as a standalone entity. Resolve it to its
        # antecedent NOUN; if the antecedent cannot be resolved, DROP the edge rather than bind the
        # pronoun. Single chokepoint (every chain emits here), grammar/morphology-driven, fail-safe.
        for _tk, _is_subj in ((subj_tok, True), (obj_tok, False)):
            if _tk is not None and _is_relative_pronoun(_tk):
                _ante = _relative_pronoun_antecedent(_tk)
                if not _ante:
                    return  # unresolved relative pronoun → never bind as an entity
                if _is_subj:
                    subj = _ante
                else:
                    obj = _ante
        # CROSS-SENTENCE DISCOURSE-TOPIC REBIND (single chokepoint) — rebind the resolved SUBJECT to the
        # turn's topic when it is a topic-compatible anaphor: (A) a subject pronoun ("it has a CVSS score
        # of 9.8" → the CVE), else (B) a definite type-NP co-referent ("the flaw has been exploited" →
        # the CVE). Type/agreement-gated + no-closer-antecedent inside the helpers (see their defs); a
        # non-anaphoric subject returns None → unchanged. This OVERRIDES a weaker in-chain ``_coref``
        # prior-NP guess for a topic-compatible pronoun (the whole point — a subject pronoun co-refers
        # with the salient topic, not a random recent object), but NOT when incompatible ("it" ⇎ a
        # PERSON topic keeps the chain's local resolution → "I have a dog. It is brown" stays the dog).
        if _topic is not None and subj_tok is not None:
            _tb = _topic_pronoun_bind(subj_tok)
            if _tb:
                subj = _tb
            else:
                _td = _topic_definite_subject(subj_tok, subj)
                if _td:
                    subj = _td
        # PREDICATE CONVERGENCE (gap-1 phase 2, §2.1): a GLiNER2-minted rel on ``doc._.rel``
        # for THIS (subject token, object token) pair is AUTHORITATIVE and WINS over the
        # deriver's SVO verb-lemma predicate; an absent minted rel → the SVO predicate
        # stands (the gap-fill). MIRRORS the object-type convergence below (``ent_type_``
        # WINS, else thin tag fills) — same shape, for the predicate. No surface match;
        # exact integer token-index pair lookup, deterministic, fail-safe → SVO.
        #
        # PREPOSITION GUARD (subject-agnostic, grammar-driven — NO rel-name list): GLiNER2's
        # relation scorer is PREPOSITION-BLIND — it scores the verb against the candidate set
        # without consulting the governing preposition, so "work WITH X" (collaboration) and
        # "work FOR X" (employment) both score onto the SAME seeded rel. But the SVO predicate
        # token ALREADY folds the load-bearing prep ("work_with" vs "work_for"), and that prep
        # is what distinguishes the two relations. So a minted rel may OVERRIDE the SVO predicate
        # ONLY when it does NOT clash with the verb's load-bearing prep: if the verb governs a
        # load-bearing prep (``_load_bearing_prep_of``) that the SVO predicate carries but the
        # minted rel does NOT honor, the prep-bearing SVO predicate is authoritative and the
        # prep-blind minted rel is dropped for this pair. Keyed on the actual prep token
        # (``dep_``/``pos_``), never a verb-lemma→rel lookup. Fail-safe: no governing prep, or the
        # minted rel honors it → today's "minted WINS" behavior is unchanged.
        _minted = _minted_rel_for_pair(doc, subj_tok, obj_tok)
        if _minted and _minted != rel:
            _prep = _load_bearing_prep_of(verb_tok, exclude_idx=_date_token_idx)
            # The SVO predicate carries the prep iff its token ends in "_<prep>" (the fold
            # _svo_predicate_token performs); the minted rel honors it iff it contains the same
            # prep segment. A prep-blind minted rel that omits the disambiguating prep loses.
            if _prep and rel.endswith(f"_{_prep}") and (
                f"_{_prep}" not in _minted and not _minted.endswith(_prep)
            ):
                pass  # prep-bearing SVO predicate wins; prep-blind minted rel dropped for this pair
            # CONTENT-VERB GUARD (sibling carve-out): GLiNER2's relation scorer picks the best-fitting
            # rel from the seeded menu even when the sentence's verb expresses a relation NOT in that
            # menu — so "CVE affects Exchange" mints a fuzzy ``manages`` that would CLOBBER the user's
            # own content verb ``affect``. A genuine, non-light CONTENT verb whose lemma is a DIFFERENT
            # relation from the mint (by inflectional identity — ``affect`` ≠ ``manage``, but ``live`` ==
            # ``lives_in``) keeps ITS OWN predicate; the fuzzy mint is dropped and the novel verb flows on
            # to verb-lift/growth. Light-verb mints ("have" → ``has_pet``) and same-verb canonicalizations
            # ("live" → ``lives_in``, "work" → ``works_for``) are NOT affected (they return False → mint
            # WINS). Structural/identity-driven, subject-agnostic; fail-safe → today's "minted WINS".
            elif _content_verb_beats_mint(verb_tok, rel, _minted):
                pass  # deterministic content verb wins; fuzzy prep-blind mint dropped for this pair
            else:
                rel = _minted
        if not (subj and rel and obj) or subj == obj:
            return
        # NAMING-NOUN-IS-NEVER-A-TYPE GUARD (genitive-name fix — THE HARD LINE). "My wife's name
        # is Ada" must bind Ada as the SPOUSE'S name (spouse + also_known_as), never classify
        # Ada as a thing of type "name". The atomizer sometimes reshapes the genitive so a generic
        # copula/SVO seam fires (Ada, instance_of, name) instead of the genitive chain — and "name"
        # is the NAMING-CONSTRUCTION anchor (the same lemma the genitive/possessive-naming seams are
        # built on), NOT an L4 type. So DROP any classification edge whose object IS that naming
        # noun: classifying anything INTO "name" is a category error across the memory/place line.
        # Structural (the naming-construction head), subject-agnostic, no domain literal.
        if rel in ("instance_of", "subclass_of") and obj == "name":
            return
        # CLAIM the spans this candidate touches REGARDLESS of dedup outcome — a span that two chains
        # both match (§10.7 overlap) is still covered by whichever fact lands; the loser converges
        # away (dedup below) but the tokens are accounted for, never re-flagged as residue.
        _claim(verb_tok, obj_tok, subj_tok)
        key = (subj, rel, obj)
        if key in seen:
            # CONVERGENCE-ON-OVERLAP (§2.1, §10.7): an identical (subj, rel, obj) already emitted by
            # another chain — do NOT double-write. The first writer's GLiNER2-typed object stands;
            # we never emit a competing second fact for the same span.
            return
        seen.add(key)
        _iso, _gran = _date_for_verb(verb_tok)
        # OBJECT TYPE — convergence (gap-1): the GLiNER2 type seeded onto the Doc (``ent_type_``)
        # WINS; an empty ent_type_ falls to the deriver's one-step thin slot tag. No surface match.
        thin = None
        if obj_tok is not None:
            try:
                _et = (obj_tok.ent_type_ or "").strip()
            except Exception:  # noqa: BLE001
                _et = ""
            thin = _et.upper() if _et else _thin_type_for_token(obj_tok)
        out.append(SentenceFact(
            subject=subj, rel_type=rel, object=obj,
            event_date=_iso, event_date_granularity=_gran,
            thin_type=thin, provenance="user_stated",
            tentative=bool(tentative), negated=bool(negated),
            scalar_datatype=(scalar_datatype or None),
            object_pronoun=(object_pronoun or None),
        ))

        # ── GENERAL COORDINATED-CONJUNCT DISTRIBUTION (rel-agnostic) ────────────────────────────
        # When a predicate's SUBJECT or OBJECT is a COORDINATED list ("... Leo, Theo, and Mia",
        # "affects Apache, Nginx, and OpenSSL", "I use Python, Rust, and Go"), a capture chain binds only
        # the FIRST conjunct and the coordinated rest are silently dropped. This step distributes the
        # SAME resolved (subject, rel, object) over EVERY coordinated sibling of the bound head — one
        # edge per member — REGARDLESS of the rel_type. It is the general engine; the rel itself is
        # whatever the chain already resolved (kinship child_of from the kinship_noun cue class, an SVO
        # verb, a social tie, …). We only distribute a side whose emitted surface is EXACTLY the head
        # token's surface (a single-token, correctly-aligned argument — never a merged multi-token span
        # or a mislabeled token), and only over ``_np_conjuncts`` members (the shared conj/PROPN-appos
        # collector — NOT arbitrary siblings). Each coordinated member is its own MEMORY entity (bound
        # by its own token, so object-type/thin convergence re-resolves per member); a NAME is never a
        # type (THE HARD LINE is unchanged — we replicate the same edge, we do not classify the name).
        # Recursion-guarded (``distribute=False`` on the replicated calls); dedup via ``seen`` makes it
        # idempotent when a chain already distributed (e.g. SVO's own dobj loop). Subject-agnostic,
        # structural, fail-safe. OFF-path parity: a non-coordinated head has a single-member conjunct
        # list → no replication → byte-identical to before.
        if distribute:
            try:
                if subj_tok is not None and subj_tok.pos_ in ("PROPN", "NOUN") \
                        and subj == (subj_tok.text or "").strip().lower():
                    _sibs = _np_conjuncts(subj_tok)
                    if len(_sibs) > 1:
                        for _sib in _sibs:
                            if _sib.i == subj_tok.i:
                                continue
                            _emit((_sib.text or "").strip().lower(), rel, obj,
                                  verb_tok=verb_tok, obj_tok=obj_tok, subj_tok=_sib,
                                  tentative=tentative, negated=negated,
                                  scalar_datatype=scalar_datatype, distribute=False)
                if obj_tok is not None and obj_tok.pos_ in ("PROPN", "NOUN") \
                        and obj == (obj_tok.text or "").strip().lower():
                    _sibs = _np_conjuncts(obj_tok)
                    if len(_sibs) > 1:
                        for _sib in _sibs:
                            if _sib.i == obj_tok.i:
                                continue
                            _emit(subj, rel, (_sib.text or "").strip().lower(),
                                  verb_tok=verb_tok, obj_tok=_sib, subj_tok=subj_tok,
                                  tentative=tentative, negated=negated,
                                  scalar_datatype=scalar_datatype, distribute=False)
            except Exception as _de:  # noqa: BLE001 — distribution never sinks the primary capture
                log.debug("linguistics.conj_distribution_failed", error=str(_de)[:160])

    # ── INTRA-TURN PRONOUN COREF (shared by every chain) ─────────────────────────────────────────
    # Resolve a 3rd-person pronoun (it/they/them) with no in-sentence antecedent to the most-recent
    # compatible prior-turn NP. Fail-safe: no antecedent → leave the pronoun. (Was "Rule 1".)
    _prior = [p for p in (prior_nps or []) if p and str(p).strip()]
    # TURN-LEVEL PERSON ANTECEDENT POOL (atom-order-INDEPENDENT coref). The atomizer split of a turn
    # is non-deterministic: the ``prior_nps`` accumulator (built from EARLIER-ATOM edge OBJECTS) only
    # carries a name like "Rachel" when the earlier atom happened to emit an edge with that object AND
    # the atomizer kept that atom BEFORE the pronoun's atom. When it doesn't, a 3rd-person object
    # pronoun ("…started working with HER") has no antecedent and the dated edge is dropped on SOME
    # runs (the 2c63a862 flake). ``turn_persons`` is the turn's PERSON proper-noun mentions computed
    # ONCE from the WHOLE turn (spaCy PERSON NER, upstream in the harvest) — the SAME pool regardless of
    # how the atomizer splits/reorders/loses atoms. A pronoun with no closer antecedent resolves to the
    # turn's UNAMBIGUOUS person (exactly one distinct PERSON in the turn). Type-agreement is inherent
    # (a PERSON-NER pool + a 3rd-person PERSONAL pronoun). Multiple persons → ambiguous → NOT used here
    # (fall through to prior_nps / defer — never guess). Lowercased, deduped, order-preserved.
    _turn_persons: list[str] = []
    try:
        _seen_tp: set[str] = set()
        for _p in (turn_persons or []):
            _pl = str(_p or "").strip().lower()
            if _pl and _pl not in _seen_tp:
                _seen_tp.add(_pl)
                _turn_persons.append(_pl)
    except Exception:  # noqa: BLE001 — fail-safe: bad pool → no turn-person coref
        _turn_persons = []

    def _coref(tok):
        try:
            low = (tok.text or "").strip().lower()
            if low not in ("it", "they", "them"):
                return None
            for cand in reversed(_prior):  # turn-order list → last = most recent
                c = str(cand).strip().lower()
                if c and c not in ("it", "they", "them"):
                    return c
        except Exception:  # noqa: BLE001
            return None
        return None

    # ── CROSS-SENTENCE DISCOURSE-TOPIC COREF (subject-agnostic, deterministic) ────────────────────
    # The turn's salient primary entity (established from sentence 1 by ``discourse_topic_from_doc`` and
    # passed in) is the antecedent a LATER sentence's subject anaphor resolves to — so a description
    # spread across several sentences CONSOLIDATES onto ONE entity (the CVE / the patient / the ruling)
    # instead of islanding on a random local subject. TWO anaphor shapes rebind the SUBJECT to the topic:
    #   (A) a 3rd-person / demonstrative SUBJECT PRONOUN ("it"/"they"/"this"/"he"/"she") with no closer
    #       antecedent, AND type/agreement-compatible with the topic; and
    #   (B) a DEFINITE type-NP subject ("the flaw"/"the vulnerability"/"the ruling") whose head is the
    #       topic's TYPE noun OR shares the topic's coarse GLiNER2 type (a generic co-referent).
    # ANTI-OVER-EAGER GUARDS (a wrong bind is worse than none): only ONE unambiguous topic (else the
    # caller passed None); NO bind when a closer in-sentence antecedent exists; TYPE-COMPATIBILITY gates
    # every bind ("the lesion"/Object ⇎ patient/Person; "the attacker"/Person ⇎ cve/Concept; "it" ⇎ a
    # Person topic so "I have a dog. It is brown" stays on the dog). Applied at the single ``_emit``
    # chokepoint on the resolved SUBJECT token — every chain routes through it. Fail-safe → no rebind.
    _topic = discourse_topic if (
        discourse_topic is not None and getattr(discourse_topic, "surface", None)) else None

    def _preceding_content_noun(tok):
        # A NOUN/PROPN (non-date) that LINEARLY PRECEDES ``tok`` in THIS sentence — a closer in-sentence
        # antecedent that must win over the cross-sentence topic. Grammar-only, subject-agnostic.
        try:
            for _t in doc:
                if _t.i >= tok.i:
                    break
                if _t.dep_ == "case" or _t.pos_ not in ("NOUN", "PROPN"):
                    continue
                if (_t.ent_type_ or "").upper() in ("DATE", "TIME"):
                    continue
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _topic_pronoun_bind(tok):
        # (A) A SUBJECT pronoun → the topic, when agreement-compatible + no closer antecedent.
        if _topic is None or tok is None:
            return None
        try:
            if tok.dep_ not in ("nsubj", "nsubjpass"):
                return None
            low = (tok.text or "").strip().lower()
            _is_dem = False
            try:
                _is_dem = tok.pos_ == "PRON" and "Dem" in tok.morph.get("PronType")
            except Exception:  # noqa: BLE001
                _is_dem = False
            if not (_is_third_person_pronoun(tok) or _is_dem):
                return None
            ttype = (_topic.gliner_type or "").upper()
            # AGREEMENT: inanimate "it"/"this"/"that" ⇎ a PERSON topic; animate "he"/"she" ⇒ a PERSON
            # (or as-yet-untyped) topic. "they"/"them" (plural) agree with any type. This is what keeps
            # "it" off a person topic (the dog case) and "he" off a non-person topic.
            if low in ("it", "this", "that"):
                if ttype == "PERSON":
                    return None
            elif low in ("he", "she", "him", "her"):
                if ttype and ttype != "PERSON":
                    return None
            if _preceding_content_noun(tok):
                return None
            return _topic.surface
        except Exception:  # noqa: BLE001
            return None

    def _topic_definite_subject(tok, subj):
        # (B) A DEFINITE type-NP subject co-referent with the topic → the topic. Type-compatibility gated.
        if _topic is None or tok is None:
            return None
        try:
            if tok.pos_ != "NOUN" or tok.dep_ not in ("nsubj", "nsubjpass"):
                return None  # a PROPN is its own named entity; a pronoun is handled by (A)
            if subj == _topic.surface:
                return None
            _det = next((c for c in tok.children if c.dep_ == "det"), None)
            if _det is None:
                return None  # bare (no determiner) → not a definite anaphor
            _dl = (_det.lemma_ or _det.text or "").strip().lower()
            if _dl in ("a", "an"):
                return None  # INDEFINITE → introduces a NEW entity, never a co-referent
            if _dl not in ("the", "this", "that", "these", "those"):
                try:
                    _pt = _det.morph.get("PronType")
                    if "Dem" not in _pt and "Art" not in _pt:
                        return None
                except Exception:  # noqa: BLE001
                    return None
            if _preceding_content_noun(tok):
                return None
            head = (tok.lemma_ or tok.text or "").strip().lower()
            if not head:
                return None
            # TYPE-COMPATIBILITY: (a) head IS the topic's type noun ("the vulnerability"), OR
            #                     (b) head shares the topic's coarse GLiNER2 type ("the flaw"/Concept), OR
            #                     (c) head is a GENERIC SHELL NOUN co-referent ("the flaw"/"the ruling").
            if head in (_topic.type_nouns or frozenset()):
                return _topic.surface
            # (c) SHELL/ABSTRACT co-referent (gap-1 shell-noun anaphora). A DEFINITE generic shell noun
            #     ("the flaw"/"the ruling"/"the condition") re-refers to the salient topic — these are
            #     the domain-agnostic anaphoric shells English uses to pick a prior entity back up, and
            #     GLiNER2 does NOT coarse-match them to the topic's exact type noun (flaw ≉ vulnerability,
            #     ruling ≉ case), so (a)/(b) miss them and the sentence's facts island. Shell nouns are
            #     TYPE-AGNOSTIC by nature (a shell can re-refer to a Person topic — "the condition
            #     worsened" → the patient), so this is NOT gated on type match; it rides the SAME
            #     definiteness + single-topic + no-closer-antecedent guards already applied above (an
            #     INDEFINITE "a flaw" was rejected earlier → a newly-introduced shell never binds). The
            #     shell-noun class is DB-grown per-tenant via the overlay (NOT an in-code list).
            try:
                if head in _shell_nouns():
                    return _topic.surface
            except Exception:  # noqa: BLE001 — fail-safe → fall through to coarse-type / no bind
                pass
            try:
                _ht = (tok.ent_type_ or "").strip().upper()
            except Exception:  # noqa: BLE001
                _ht = ""
            _tt = (_topic.gliner_type or "").upper()
            if _ht and _tt and _ht == _tt:
                return _topic.surface
            return None
        except Exception:  # noqa: BLE001
            return None

    # ── NAMED-INSTANCE SUPPRESSION SET (the unified binding chain OWNS these spans) ───────────────
    # When the unified name↔type binding chain owns a clause ("a son Alex 19", "a dog named Rex"),
    # the bare TYPE noun (son/dog/friend), the enclosing COLLECTIVE governing it ("children" in "we
    # have three children …"), and the nickname relative-clause ("who goes by Jay") are all CONSUMED by
    # that chain — they must NOT also be read by the SVO / appositive / intransitive chains as their own
    # facts (else "(user, have, children)", "(son, has_role, daughter)", "(who, has_state, go)" junk).
    # We compute the suppressed token-index set ONCE here (flag-gated, fail-safe → empty) so every other
    # chain can skip a token whose ``.i`` is in it. Structural + the bindings; subject-agnostic.
    _ni_suppress: set = set()
    if SPINE_NAMING_CHAIN:
        try:
            _binds = analyze_name_type_bindings(doc)
            if _binds:
                _bound_type_heads = {(_b.type_noun.split()[-1] if _b.type_noun else "")
                                     for _b in _binds if _b and not _b.negated}
                _bound_type_heads.discard("")
                for _t in doc:
                    _tl = (_t.text or "").strip().lower()
                    # the bare TYPE noun (each occurrence in an enumeration)
                    if _t.pos_ == "NOUN" and _tl in _bound_type_heads:
                        _ni_suppress.add(_t.i)
                    # a 1st-person ``have`` clause's verb + its collective dobj that heads the members
                    if (_t.lemma_ or "").strip().lower() == "have" and _t.pos_ in ("VERB", "AUX"):
                        _sj = next((s for s in _t.children
                                    if s.dep_ in ("nsubj", "nsubjpass")), None)
                        if _sj is not None and _is_first_person_personal_pronoun(_sj):
                            _ni_suppress.add(_t.i)  # the "have" verb (SVO would mint user-have-X)
                            for _c in _t.children:
                                if _c.dep_ in ("dobj", "obj") and _c.pos_ == "NOUN" and (
                                        any(_g.dep_ in ("appos", "conj") for _g in _c.children)):
                                    _ni_suppress.add(_c.i)
                    # the nickname relative clause "who goes by Theo" (the ``go`` verb + its subtree)
                    if (_t.lemma_ or "").strip().lower() == "go" and _t.pos_ == "VERB" and \
                            _t.dep_ in ("relcl", "acl"):
                        if any(_c.dep_ == "prep" and (_c.text or "").strip().lower() == "by"
                               for _c in _t.children):
                            for _d in _t.subtree:
                                _ni_suppress.add(_d.i)
        except Exception:  # noqa: BLE001 — fail-safe: suppression is best-effort, never blocks capture
            _ni_suppress = set()

    # ── EMPLOYMENT CONSTRUCTION PRE-PASS (role-predication + affiliation) ─────────────────────────
    # "<subject> <employment verb> as <role> [at|for <org>]" — "I work as a Systems Analyst III at the
    # University of Springfield's Computing Services", "Sarah works as a nurse at the clinic", "I serve
    # as a board member of the co-op", "employed as an engineer at Globex". WITHOUT this chain the
    # employment clause falls to the LLM relation-fill / rewrite, which drops the role and mislabels
    # "work at <university>" as ``educated_at`` (a university object biases the weak model to education)
    # and leaks genitive ``related_to`` junk. THE FIX (deterministic, grammar + a DB-grown verb cue
    # class — NO per-subject/employer/role literals):
    #   • the VERB LEMMA must be in the ``employment_verb`` cue class (``_employment_verbs()``, overlay
    #     seed∪tenant + bootstrap floor). This class IS the safety gate: "I DRESSED as a pirate" /
    #     "known as X" — ``dress``/``know`` are not employment verbs → NEVER read as an occupation.
    #   • the SUBJECT is resolved GRAMMATICALLY — a 1st-person personal pronoun ("I"/"we") → ``user``,
    #     or a NAMED 3rd-person subject ("Sarah works …") → that name. Not first-person-only.
    #   • "as <NP>"        → occupation(subject, <full role NP incl. trailing "III">) — TYPE-agnostic.
    #   • "at|for <ORG>"   → works_for(subject, <org NP>) — the EMPLOYMENT VERB (not the object's type)
    #     determines the relation, so a university/school object is NEVER flipped to ``educated_at``.
    #     The affiliation PP may hang directly off the verb ("work at Acme Corp") OR nest under the
    #     role NP ("work as a <role> at <org>" — spaCy attaches "at" to the role head), so we look on
    #     BOTH the verb and the role head. A temporal/duration "for" pobj (a resolved date span or a
    #     DATE/TIME entity — "work for 3 years") is EXCLUDED, never bound as an employer.
    # This pre-pass computes the bindings ONCE (so the SVO/intransitive/possessive chains SUPPRESS the
    # verb + the role/org subtrees they would otherwise mis-capture) and ``_chain_employment`` emits
    # off it. Subject-agnostic, deterministic, fail-safe (any miss → today's path). ``study``/``graduate``
    # are NOT employment verbs → "I studied at the University" still routes to ``educated_at`` untouched.
    _emp_binds: list = []
    _emp_suppress: set = set()
    try:
        _empverbs = _employment_verbs()
        for _v in doc:
            if _v.pos_ != "VERB" or _v.i in _ni_suppress:
                continue
            _vlem = (_v.lemma_ or _v.text or "").strip().lower()
            if _vlem not in _empverbs:
                continue
            _subj = next((c for c in _v.children if c.dep_ in ("nsubj", "nsubjpass")), None)
            if _subj is None:
                continue
            # NEGATED employment clause ("I don't work for them") → skip (absence deferred, SVO parity).
            if any(c.dep_ == "neg" for c in _v.children):
                continue
            # ROLE: "as <NP>" — the prep "as" the verb governs, its NOUN/PROPN pobj is the role head.
            _role = None
            _as = next((c for c in _v.children if c.dep_ == "prep"
                        and (c.text or "").strip().lower() == "as"), None)
            if _as is not None:
                _role = next((g for g in _as.children
                              if g.dep_ == "pobj" and g.pos_ in ("NOUN", "PROPN")), None)
            # ORG: "at|for <PROPN/NOUN>" governed by the verb OR nested under the role head. A pobj that
            # is a resolved date span / DATE-TIME entity (duration "for 3 years") is not an employer.
            _org = None
            for _h in ([_v, _role] if _role is not None else [_v]):
                for _c in _h.children:
                    if _c.dep_ != "prep" or (_c.text or "").strip().lower() not in ("at", "for"):
                        continue
                    _po = next((g for g in _c.children
                                if g.dep_ == "pobj" and g.pos_ in ("NOUN", "PROPN")
                                and g.i not in _date_token_idx
                                and (g.ent_type_ or "").upper() not in ("DATE", "TIME")), None)
                    if _po is not None:
                        _org = _po
                        break
                if _org is not None:
                    break
            if _role is None and _org is None:
                continue  # not an employment frame this verb owns → leave to SVO/other chains
            _emp_binds.append((_v, _subj, _role, _org))
            # SUPPRESS the spans this construction OWNS so the SVO/intransitive/possessive chains never
            # re-capture them: the verb (kills "(user, has_state, work)" + a duplicate SVO "work_at"),
            # the role subtree (which — since "at <org>" nests under it — also covers the org PP and its
            # genitive junk "(computing services, related_to, springfield)"), and the org subtree.
            _emp_suppress.add(_v.i)
            for _o in (_role, _org):
                if _o is not None:
                    for _d in _o.subtree:
                        _emp_suppress.add(_d.i)
    except Exception:  # noqa: BLE001 — fail-safe: employment detection never blocks capture
        _emp_binds = []
        _emp_suppress = set()

    # ── DATED PASSIVE-EVENT PRE-PASS (bind the DATE + predicate to the NAMED entity; participle is
    #    NEVER an object) ─────────────────────────────────────────────────────────────────────────
    # "My wife Ada was born on July 8, 1985." / "My company Acme was founded in 1998." / "My server
    # Apollo was provisioned on March 1, 2023." / "The product was released in 2019." Today a DATED
    # passive-participle clause mints junk: the objectless-state chain reads the participle as a STATE
    # OBJECT and strands the date on the role/possessed noun — (wife, has_state, bear)@1985-07-08,
    # (server, has_state, provision)@2023-03-01 — and sibling chains can read it as a relationship
    # object (spouse(user, born) / owns(x, founded)). So "When was Ada born?" / "When was Apollo
    # provisioned?" finds nothing (the date is on the wrong node, tagged with a junk state).
    #
    # THE FIX — ONE fully ENTITY-AGNOSTIC + PREDICATE-AGNOSTIC grammatical rule (NO lemma / role /
    # kinship / domain literals): for ANY "<entity> was <PASSIVE-PARTICIPLE> [prep <date>]" the
    # PARTICIPLE is the PREDICATE (never an object) and the ``event_date`` + predicate bind to the
    # entity the clause is ABOUT — resolved by the SAME appositive/naming + possessive + discourse-
    # coref the deriver already uses (a person, a dog, a server, a product all take this path). The
    # participle is detected MORPHOLOGICALLY — spaCy ``tag_ == "VBN"`` (VerbForm=Part) governed by an
    # ``auxpass`` 'be' aux (the "was/were/is/are/been <Xed>" passive) — NOT any surface/lemma. We only
    # bind (and only suppress the state twin) when the DATE layer actually resolves a date for this
    # verb, so a DATELESS passive state ("the server was decommissioned") is left to today's chains
    # untouched (no regression). Fail-safe: any miss → today's path.
    _passive_binds: list = []
    _passive_suppress: set = set()
    try:
        for _v in doc:
            # PASSIVE PARTICIPLE, purely morphological: a past-participle verb (VBN / VerbForm=Part).
            if _v.tag_ != "VBN" and "Part" not in str(_v.morph):
                continue
            _has_auxpass = any(c.dep_ == "auxpass" and (c.lemma_ or "").strip().lower() == "be"
                               for c in _v.children)
            # TWO subject shapes reach this lane:
            #   (i)  FINITE passive ("was/were <Xed>") — a 'be' ``auxpass`` child + an nsubjpass subject.
            #   (ii) REDUCED-RELATIVE / COORDINATED participle ("a dog named Rex, BORN in 2020") — the
            #        participle is an ``acl``/``relcl`` modifying a NOUN/PROPN with NO auxpass and NO
            #        nsubjpass; its logical subject IS the modified head noun (Rex). spaCy attaches the
            #        second, coordinated participle ("born") as an ``acl`` of the appositive NAME, so the
            #        finite-passive branch above never sees it and the intransitive chain mis-reads it as
            #        (rex, has_state, bear). Admit the reduced participle so its date binds to the head.
            # Purely morphological/structural (VBN + acl/relcl + nominal head), subject- & predicate-
            # agnostic (no lemma/role list); the objectless + date guards below still gate BOTH shapes.
            _is_reduced_participle = (
                not _has_auxpass and _v.dep_ in ("acl", "relcl")
                and _v.head is not None and _v.head.pos_ in ("NOUN", "PROPN")
            )
            if not _has_auxpass and not _is_reduced_participle:
                continue
            if _has_auxpass:
                _psubj = next((c for c in _v.children if c.dep_ in ("nsubjpass", "nsubj")), None)
            else:
                # The reduced participle predicates its head noun (the modified instance). PREFER the
                # bound PROPER NAME (THE HARD LINE — the event files at the NAME, not the bare type):
                # the head may be a PROPN itself ("dog named Rex, BORN" → head IS Rex), carry an appos
                # PROPN, or be named by a sibling naming-verb acl ("server called Apollo, PROVISIONED"
                # → head "server" has a ``called`` acl whose PROPN object is Apollo). Resolve the name
                # so the date lands on Apollo, not "server". Structural, subject-agnostic, no lemma list.
                _head = _v.head
                if _head is not None and _head.pos_ == "PROPN":
                    _psubj = _head
                else:
                    _nm = next((g for g in _head.children
                                if g.dep_ == "appos" and g.pos_ == "PROPN"), None) if _head else None
                    if _nm is None and _head is not None:
                        for _sib in _head.children:
                            if _sib.dep_ in ("acl", "relcl") and _sib.pos_ == "VERB":
                                _op = next((g for g in _sib.children
                                            if g.dep_ in ("oprd", "dobj", "attr") and g.pos_ == "PROPN"),
                                           None)
                                if _op is not None:
                                    _nm = _op
                                    break
                    _psubj = _nm or _head
            if _psubj is None:
                continue
            # AGENT / OBJECT guard — our lane is the OBJECTLESS dated passive (born / hired /
            # provisioned / released). A by-agent passive ("founded BY Ada"), a direct object, or a
            # prepositional/oblique complement ("diagnosed WITH diabetes", "cited BY the court") is a
            # real subject↔object relation the agent/SVO chain captures WITH its own date — leave it to
            # that chain so we never mint a redundant date-only twin. This is the SAME objectless test
            # the intransitive chain uses (``_svo_object_head`` sees NOUN/PROPN objects incl. the by-
            # agent; a date-only pobj is excluded via ``_date_token_idx`` so it stays "objectless").
            if any(c.dep_ in ("agent", "dobj", "obj", "iobj", "dative", "obl") for c in _v.children):
                continue
            if _svo_object_head(_v, exclude_idx=_date_token_idx, include_agent=True) is not None:
                continue
            # DATE bound to THIS participle's governing verb (the shared temporal machinery). A dateless
            # passive clause is NOT ours — leave it to the existing chains (no fabricated date, no
            # regression). NEGATED passive ("was not born here") also stays with the existing chains.
            _piso, _pgran = _date_for_verb(_v)
            if not _piso or any(c.dep_ == "neg" for c in _v.children):
                continue
            _passive_binds.append((_v, _psubj, _piso, _pgran))
            _passive_suppress.add(_v.i)  # the intransitive chain must never mint (x, has_state, <part>)
    except Exception:  # noqa: BLE001 — fail-safe: passive-event detection never blocks capture
        _passive_binds, _passive_suppress = [], set()

    # ── ALIAS-PREDICATE VERB SUPPRESS SET ────────────────────────────────────────────────────────
    # ``_chain_alias_predicate`` (the third-party nickname/alias chain) OWNS the verb of an alias
    # construction ("she GOES by Dee", "he is KNOWN as Sammy"). It records that verb's index here so
    # the intransitive / copula-state / SVO chains never ALSO mint a junk (she, has_state, go) /
    # (she, has_state, know) twin for the SAME verb. Populated by the alias chain (which runs before
    # those chains in the ``_chains`` tuple); read by them via ``if tok.i in _alias_suppress``.
    _alias_suppress: set = set()

    # ── DATED-OCCURRENCE SUPPRESS SET ─────────────────────────────────────────────────────────────
    # ``_chain_dated_occurrence`` (below) OWNS a date-postmodified eventive NP ("my team meeting on
    # the 17th"): it mints the (user, participated_in, <NP>) dated-event edge and records the NP's
    # scattered modifier tokens here so ``_chain_possessive`` never ALSO mints the mis-scoped
    # (user, owns, "upcoming team") twin off the same possessed noun. Populated by the occurrence
    # chain (which runs BEFORE the possessive chain in the ``_chains`` tuple); read via ``in``.
    _occ_suppress: set = set()

    # ── POSSESSED-TYPED + STRUCTURED-ATOMIC PRE-PASS (device-classification + scalar routing) ─────
    # "My router is a UniFi at 192.168.1.1." — a possessed NOUN classified with an indefinite-article
    # TYPE ("a UniFi") that carries a trailing STRUCTURED-ATOMIC literal (the IP). Today the attr-scalar
    # / preference twins read the WHOLE copula complement as one scalar VALUE keyed by the possessed
    # noun → rel_type == "router", which (a) buries the IP inside the value string and (b) BLOCKS the
    # router ENTITY the /ingest atomic detector needs to host (router, has_ip, <IP>) — the has_ip edge
    # is dropped as a rel_type-as-entity collision. THE FIX (deterministic, subject-agnostic): read the
    # construction as a CLASSIFICATION — (possessor, owns, <noun>) + (<noun>, instance_of, <Type>) —
    # so the noun grounds as a first-class ENTITY, and LEAVE the trailing atomic to the /ingest atomic
    # seam (which now binds the scalar to the freed entity). Detected purely by grammar (possessed noun
    # + copula + indefinite-article NOUN/PROPN type) + the format-grammar atomic shape; NO rel_type is
    # decided here (that stays with the atomic detector). The possessed noun + type head are suppressed
    # from the attr-scalar / copula-state twins (own-the-construction / suppress-twins). Gated on the
    # structured-atomic signal so a plain "my router is a UniFi" (no scalar) is UNTOUCHED (still a
    # preference/attr scalar). Kinship heads are excluded (their age/kin readings are unaffected).
    _typed_atomic_binds: list = []
    _typed_atomic_suppress: set = set()
    try:
        _kin = _kinship_nouns()
        for _tok in doc:
            if _tok.dep_ not in ("nsubj", "nsubjpass") or _tok.pos_ != "NOUN":
                continue
            _hd = _tok.head
            if _hd is None or not (_hd.lemma_ == "be" and _hd.pos_ == "AUX"):
                continue
            # POSSESSOR: 1st-person poss determiner → user; genitive NOUN/PROPN possessor → that noun.
            _pos = None
            for _c in _tok.children:
                if _c.dep_ != "poss":
                    continue
                try:
                    if _c.morph.get("Person") == ["1"] and "Yes" in _c.morph.get("Poss"):
                        _pos = "user"
                        break
                except Exception:  # noqa: BLE001
                    pass
                if _c.pos_ in ("NOUN", "PROPN"):
                    _pos = (_c.text or _c.lemma_ or "").strip().lower()
                    break
            if not _pos:
                continue
            if (_tok.lemma_ or _tok.text or "").strip().lower() in _kin:
                continue
            # NEGATED copula → defer (absence modeling, parity with the scalar chains).
            if any(_c.dep_ == "neg" for _c in _hd.children):
                continue
            # A trailing STRUCTURED-ATOMIC pobj must be present in the clause (the routing signal).
            if not any(_d.dep_ == "pobj" and _is_structured_atomic_value(_d.text) for _d in doc):
                continue
            # TYPE = an indefinite-article NOUN/PROPN complement of the copula ("a UniFi") — OPTIONAL
            # ("my router is at 192.168.1.1" has no type; the owns edge alone still frees the entity).
            _type_tok = None
            for _c in _hd.children:
                if _c.dep_ in ("attr", "oprd") and _c.pos_ in ("NOUN", "PROPN") and any(
                        _g.dep_ == "det" and (_g.text or "").strip().lower() in ("a", "an")
                        for _g in _c.children):
                    _type_tok = _c
                    break
            _typed_atomic_binds.append((_tok, _pos, _type_tok))
            _typed_atomic_suppress.add(_tok.i)
            if _type_tok is not None:
                _typed_atomic_suppress.add(_type_tok.i)
    except Exception:  # noqa: BLE001 — fail-safe: never blocks capture
        _typed_atomic_binds, _typed_atomic_suppress = [], set()

    # ── DATE-VALUED ATTRIBUTE PRE-PASS (construction-agnostic date-value binding) ────────────────
    # The date-valued sibling of the numeric copula-measure layer. A DATE is a VALUE: when a clause
    # PREDICATES a date OF an entity via a possessive/copula ("<owner>'s <noun> is <date>", "my <noun>
    # is <date>") or a have-construction ("<owner> has a <noun> of <date>"), bind it as a dated SCALAR
    # named by the possessed/predicate NOUN, on the entity — so "Rex was born in 2020" and
    # "Rex's birthday is March 3, 2020" both land a date value on Rex. This pre-pass does the
    # GRAMMAR + DATE-LAYER detection only (owner resolution + emit happen in ``_chain_date_attribute``,
    # which can use the coref helpers defined later). It records (owner_tok, attr_noun_tok, iso, gran)
    # and populates ``_date_attr_suppress`` (the attribute noun + date complement tokens) so no OTHER
    # chain mis-reads the month-name/day-num/attribute-noun as an entity/age. ZERO word/attribute
    # literals: DATE-ness is decided ONLY by the date layer; the attribute NAME is the possessed noun;
    # the OWNER is genitive/possessive/subject grammar. A bare "X's birthday" with NO date → nothing
    # (no fabrication); a PLACE ("born in Toronto") is a participle/location construction, never here.
    def _da_date_of(_text):
        try:
            for _st, _sp in (_collect_date_spans(_text) or []):
                _di, _dg, _, _ = _resolve_first_valid_date(_sp, reference)
                if _di:
                    return _di, (_dg or "day")
        except Exception:  # noqa: BLE001
            pass
        return None, None

    def _da_subtree_text(_t):
        _ts = sorted(_t.subtree, key=lambda x: x.idx)
        if not _ts:
            return ""
        return doc.text[_ts[0].idx:(_ts[-1].idx + len(_ts[-1].text))]

    def _da_suppress_subtree(_t):
        try:
            for _st in _t.subtree:
                _date_attr_suppress.add(_st.i)
        except Exception:  # noqa: BLE001
            pass

    try:
        for _tok in doc:
            # Pattern B: HAVE + <noun> of <date>.
            if _tok.pos_ == "VERB" and (_tok.lemma_ or "").strip().lower() == "have":
                _hs = next((c for c in _tok.children if c.dep_ in ("nsubj", "nsubjpass")), None)
                _hd = next((c for c in _tok.children
                            if c.dep_ in ("dobj", "obj") and c.pos_ == "NOUN"), None)
                if _hs is None or _hd is None:
                    continue
                _hof = next((c for c in _hd.children if c.dep_ == "prep"
                             and (c.text or "").strip().lower() == "of"), None)
                if _hof is None:
                    continue
                _hp = next((g for g in _hof.children if g.dep_ == "pobj"), None)
                if _hp is None:
                    continue
                _di, _dg = _da_date_of(_da_subtree_text(_hp))
                if not _di:
                    continue
                _date_attr_binds.append((_hs, _hd, _di, _dg))
                # suppress the attribute noun + the "of <date>" complement (never a standalone entity)
                _date_attr_suppress.add(_hd.i)
                for _m in _hd.children:
                    if _m.dep_ in ("compound", "amod") and _m.i < _hd.i:
                        _date_attr_suppress.add(_m.i)
                _da_suppress_subtree(_hp)
                continue
            # Pattern A: COPULA be with a DATE complement + a possessed attribute NOUN.
            if not ((_tok.lemma_ or "").strip().lower() == "be" and _tok.pos_ == "AUX"):
                continue
            _cmp = next((c for c in _tok.children
                         if c.dep_ in ("attr", "acomp", "oprd", "npadvmod")), None)
            if _cmp is None:
                continue
            _di, _dg = _da_date_of(_da_subtree_text(_cmp))
            if not _di:
                continue
            _acands = []
            for c in _tok.children:
                if c.dep_ in ("nsubj", "nsubjpass") and c.pos_ in ("NOUN", "PROPN"):
                    _acands.append(c)
                    for g in c.children:
                        if g.dep_ == "appos" and g.pos_ in ("NOUN", "PROPN"):
                            _acands.append(g)
            _an = _ao = None
            for c in _acands:  # PREFER a genitive PROPN owner ("Apollo's date") over a determiner
                _pp = next((x for x in c.children if x.dep_ == "poss" and x.pos_ == "PROPN"), None)
                if _pp is not None:
                    _an, _ao = c, _pp
                    break
            if _an is None:
                for c in _acands:
                    _pp = next((x for x in c.children if x.dep_ == "poss"), None)
                    if _pp is not None:
                        _an, _ao = c, _pp
                        break
            if _an is None or _ao is None:
                continue
            _date_attr_binds.append((_ao, _an, _di, _dg))
            # suppress the attribute noun (+ its compound/amod) and the whole DATE complement subtree
            _date_attr_suppress.add(_an.i)
            for _m in _an.children:
                if _m.dep_ in ("compound", "amod") and _m.i < _an.i:
                    _date_attr_suppress.add(_m.i)
            _da_suppress_subtree(_cmp)
        # SURFACE index for the ``_emit`` fallback (chains that resolve a surface, not the token).
        for _si in _date_attr_suppress:
            try:
                _date_attr_suppress_surf.add((doc[_si].text or "").strip().lower())
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001 — fail-safe: date-attribute detection never blocks capture
        _date_attr_binds, _date_attr_suppress, _date_attr_suppress_surf = [], set(), set()

    # ── KINSHIP-COLLECTIVE PRE-PASS (the family-list case) ───────────────────────────────────────
    # "We have three kids: Mia, Theo, and Leo." — a COLLECTIVE kinship head ("kids"/"children"/
    # "sons") governing a named member list. The bare SVO would mint a degenerate (user, have, kids)
    # owning the collective TYPE, and the dash chain would distribute the verb (user, have, mia).
    # Neither uses the KINSHIP relation the head metadata carries. Here we detect such a head ONCE so:
    #   • the SVO chain SUPPRESSES the (user, have, <collective>) edge (the collective is a class, not a
    #     thing the user owns), and
    #   • the dash-specifier chain re-routes each named member to the head's kinship rel + direction
    #     ((mia, child_of, user)) + the head's intrinsic gender, if any.
    # Detection is grammatical + metadata-driven (the DB-grown kinship cue maps via the head's LEMMA —
    # NO kinship word list in code) and fail-safe (any miss → today's behavior). ``_kin_collective``
    # maps the collective head token index → (kin_rel, gender_or_None); the members are resolved by the
    # dash chain. Gated to a 1st-person/coref possessor + a possession verb governing the head as dobj.
    _kin_collective: dict = {}
    try:
        _kin_map = _kinship_rel_map()
        _kin_gender = _kinship_gender_map()
        for _v in doc:
            if _v.pos_ not in ("VERB", "AUX"):
                continue
            _vl = (_v.lemma_ or _v.text or "").strip().lower()
            if _vl not in _possession_verbs() and _vl != "have":
                continue
            _sj = next((c for c in _v.children if c.dep_ in ("nsubj", "nsubjpass")), None)
            if _sj is None:
                continue
            for _h in _v.children:
                if _h.dep_ not in ("dobj", "obj") or _h.pos_ != "NOUN":
                    continue
                _hl = (_h.lemma_ or _h.text or "").strip().lower()
                _krel = _kin_map.get(_hl)
                if not _krel:
                    continue
                # the head must INTRODUCE a named member list (a PROPN appos/conj member, or a
                # dash/colon-separated PROPN following it) — else it is a bare "I have a son" the
                # named-instance chain already owns. Structural, subject-agnostic.
                _has_named_members = any(
                    g.pos_ == "PROPN" and g.dep_ in ("appos", "conj") for g in _h.children)
                if not _has_named_members:
                    for _t in doc[_h.i + 1:]:
                        if _t.is_space:
                            continue
                        if _t.pos_ == "PUNCT" and (_t.text or "").strip() in ("-", "–", "—", ":"):
                            continue
                        _has_named_members = _t.pos_ == "PROPN"
                        break
                if _has_named_members:
                    _kin_collective[_h.i] = (_krel, _kin_gender.get(_hl))
    except Exception:  # noqa: BLE001 — fail-safe: collective routing is best-effort
        _kin_collective = {}

    # ── HAS-A-MEASURE PRE-PASS (SCALAR "has/have a <measure> of <value>") ─────────────────────────
    # "It has a CVSS base score of 9.8", "the patient has a temperature of 39", "the case has a term of
    # 20 years" — a possession verb ("have") whose dobj is a MEASURE noun carrying an "of"-PP with a
    # DIGIT-bearing value is a SCALAR MEASUREMENT of the subject (attribute = the dobj head-noun phrase,
    # value = the of-object), NOT a relationship object. This is the companion to ``_chain_measure_pp``
    # (which owns the PP form "with a score of 9.8"). The DIGIT gate + the "of"-PP shape keep it narrow:
    # "I have a dog" / "I have three children" (no of-PP) never match. Computed ONCE here so the SVO
    # chain SUPPRESSES the (subject, have, <measure>) junk twin; ``_chain_has_measure`` emits the scalar
    # off these binds. Subject-agnostic, grammar + value-shape (NO attribute/unit word list), fail-safe.
    _has_measure_binds: list = []
    _has_measure_suppress: set = set()
    try:
        for _v in doc:
            if (_v.lemma_ or "").strip().lower() != "have" or _v.pos_ not in ("VERB", "AUX"):
                continue
            if _v.i in _ni_suppress:
                continue  # a named-instance "have" collective the binding chain owns
            _dobj = next((c for c in _v.children
                          if c.dep_ in ("dobj", "obj") and c.pos_ in ("NOUN", "PROPN")), None)
            if _dobj is None:
                continue
            try:
                if (_dobj.ent_type_ or "").upper() in ("DATE", "TIME"):
                    continue
            except Exception:  # noqa: BLE001
                pass
            # VALUE: an "of"-PP under the measure dobj whose pobj IS (or carries a nummod) a DIGIT.
            _val_root = None
            for _c in _dobj.children:
                if _c.dep_ == "prep" and (_c.text or "").strip().lower() == "of":
                    for _gc in _c.children:
                        if _gc.dep_ != "pobj":
                            continue
                        if any(ch.isdigit() for ch in (_gc.text or "")):
                            _val_root = _gc
                            break
                        if any(k.dep_ == "nummod" and any(ch.isdigit() for ch in (k.text or ""))
                               for k in _gc.children):
                            _val_root = _gc
                            break
                if _val_root is not None:
                    break
            if _val_root is None:
                continue
            _sj = next((c for c in _v.children if c.dep_ in ("nsubj", "nsubjpass")), None)
            if _sj is None:
                _sj = _carried_subject_token(_v)
            if _sj is None:
                continue
            _has_measure_binds.append({"verb": _v, "subj": _sj, "dobj": _dobj, "val": _val_root})
            _has_measure_suppress.add(_v.i)
    except Exception:  # noqa: BLE001 — fail-safe: has-measure detection is best-effort
        _has_measure_binds, _has_measure_suppress = [], set()

    # ── QUANTITY-OF-SUBSTANCE PRE-PASS (SCALAR "<num> <unit> [of <substance>]") ────────────────────
    # "I take 500 milligrams of metformin", "my NAS has 40 terabytes of storage", "humans have 23
    # pairs of chromosomes", "the server has 64 gigabytes of RAM", "lisinopril 10 milligrams" — a
    # UNIT/COUNT noun carrying a NUM nummod (DIGIT-gated) is NEVER a relationship object: the
    # number+unit is a SCALAR VALUE and the of-noun (or the appositive head) names the SUBSTANCE /
    # attribute. Without this the SVO / appositive chains bind the UNIT as the object and DROP BOTH the
    # number and the substance (live DB: ``take(user, milligrams)`` — 500 + metformin gone;
    # ``owns(humans, pairs)`` — 23 + chromosomes gone). Companion to the has-measure pre-pass, which
    # owns the INVERSE shape "has a score OF 9.8" (the DIGIT is the of-object); HERE the digit is a
    # nummod ON the unit noun and the of-object is the (non-digit) substance — the two never collide.
    # THREE grammatical readings, routed by the governing verb (grammar, not a domain word list):
    #   • POSSESSION ("have" dobj / "with" pobj + "of <noun>") → the of-noun NAMES the attribute; the
    #     SCALAR "<num> <unit>" attaches to the SUBJECT ("server has 64 gigabytes of RAM" → ram=
    #     "64 gigabytes"; "humans have 23 pairs of chromosomes" → chromosomes="23 pairs").
    #   • CONTENT verb ("take"/"contains" + "of <substance>") → the of-substance is the REAL OBJECT
    #     (emitted relationally so it grounds as its own L4 entity: take(user, metformin)) and the
    #     SCALAR "<num> <unit>" attaches to the SUBSTANCE. The attribute name is the unit's unit_scalar
    #     cue-map value if the unit is known there, else a generic "quantity" (never dropped).
    #   • APPOSITIVE ("<substance> <num> <unit>": "lisinopril 10 milligrams") → SCALAR on the head noun.
    # DIGIT-gated (never fires without a numeral). Subject-agnostic — spaCy dependency shape + the DB
    # unit_scalar map, NO drug/unit/domain literal. The suppress sets step the SVO/appositive twins
    # aside (else the mangled UNIT-as-object edge is re-emitted). Fail-safe → no binds, legacy path.
    _quantity_binds: list = []
    _quantity_verb_suppress: set = set()
    _quantity_appos_suppress: set = set()
    try:
        _POSSESSION_LIGHT = {"have"}  # grammatical stative-possession light verb (NOT a domain word)
        for _u in doc:
            if _u.pos_ not in ("NOUN", "PROPN"):
                continue
            # DIGIT-GATED unit/count noun: a NUM nummod child carrying an actual numeral.
            _num = next((c for c in _u.children
                         if c.dep_ == "nummod" and c.pos_ == "NUM"
                         and any(ch.isdigit() for ch in (c.text or ""))), None)
            if _num is None:
                continue
            # the of-PP substance/attribute, if any: "<unit> of <noun>".
            _ofnoun = None
            for _c in _u.children:
                if _c.dep_ == "prep" and (_c.text or "").strip().lower() == "of":
                    _ofnoun = next((g for g in _c.children
                                    if g.dep_ == "pobj" and g.pos_ in ("NOUN", "PROPN")), None)
                    if _ofnoun is not None:
                        break
            # VALUE span = "<num> … <unit>" sliced verbatim (keeps an intervening amod: "40 usable TB").
            try:
                _qval = (sentence[_num.idx:_u.idx + len(_u.text)] or "").strip()
            except Exception:  # noqa: BLE001
                _qval = f"{(_num.text or '').strip()} {(_u.text or '').strip()}".strip()
            if not _qval:
                continue
            _dep = _u.dep_
            # (A) UNIT as the dobj/obj of a verb → possession (have) vs content (any other verb).
            if _dep in ("dobj", "obj") and _u.head is not None and _u.head.pos_ in ("VERB", "AUX"):
                _v = _u.head
                if _v.i in _ni_suppress or _v.i in _has_measure_suppress:
                    continue
                if _ofnoun is None:
                    continue  # a bare count ("3 cats", "23 chromosomes") — SVO/possessive own it
                _vlemma = (_v.lemma_ or "").strip().lower()
                _sj = next((c for c in _v.children if c.dep_ in ("nsubj", "nsubjpass")), None) \
                    or _carried_subject_token(_v)
                if _sj is None:
                    continue
                if _vlemma in _POSSESSION_LIGHT:
                    _quantity_binds.append({"mode": "possession", "subj": _sj, "attr_tok": _ofnoun,
                                            "value": _qval, "unit": _u, "num": _num, "verb": _v})
                else:
                    _quantity_binds.append({"mode": "content", "subj": _sj, "substance": _ofnoun,
                                            "value": _qval, "unit": _u, "num": _num, "verb": _v})
                _quantity_verb_suppress.add(_v.i)
            # (B) UNIT as the pobj of a prep ("comes WITH 40 terabytes of storage") → possession.
            elif _dep == "pobj" and _u.head is not None and _u.head.dep_ == "prep":
                if _ofnoun is None:
                    continue
                _gov = _u.head.head
                _hops = 0
                while _gov is not None and _gov.pos_ not in ("VERB", "AUX") and _hops < 6:
                    if _gov.head is _gov:
                        break
                    _gov = _gov.head
                    _hops += 1
                if _gov is None or _gov.pos_ not in ("VERB", "AUX") or _gov.i in _ni_suppress:
                    continue
                _sj = next((c for c in _gov.children if c.dep_ in ("nsubj", "nsubjpass")), None) \
                    or _carried_subject_token(_gov)
                if _sj is None:
                    continue
                _quantity_binds.append({"mode": "possession", "subj": _sj, "attr_tok": _ofnoun,
                                        "value": _qval, "unit": _u, "num": _num, "verb": _gov})
                _quantity_verb_suppress.add(_gov.i)
            # (C) UNIT as an appositive of a substance noun ("lisinopril 10 milligrams") → scalar on it.
            elif _dep == "appos" and _u.head is not None and _u.head.pos_ in ("NOUN", "PROPN"):
                _quantity_binds.append({"mode": "appos", "owner_tok": _u.head,
                                        "value": _qval, "unit": _u, "num": _num, "verb": None})
                _quantity_appos_suppress.add(_u.i)
    except Exception:  # noqa: BLE001 — fail-safe: quantity detection is best-effort
        _quantity_binds, _quantity_verb_suppress, _quantity_appos_suppress = [], set(), set()

    # ── CAPTURE CHAINS (gap-2 §10.1) ─────────────────────────────────────────────────────────────
    # The deriver is NOT a fixed sequence of capture rules. Each capture chain is a self-contained
    # match-condition + builder: it walks the parse and, wherever its GRAMMATICAL SHAPE (and/or a
    # DB-grown-ontology match — relational-noun / naming-verb cue classes, resolved via the overlays)
    # holds, it drops in and emits candidate fact(s) through the shared ``_emit``. The chains are
    # collected into ``_chains`` and run in a single pass over the parse; there is NO hardcoded
    # PRIORITY among them — reconciliation is by CONVERGENCE inside ``_emit`` (dedup on
    # (subj, rel, obj); GLiNER2's object type wins; identical span → first writer stands, never a
    # competing second fact — §2.1/§10.7), so the OUTCOME is independent of the iteration order. To
    # teach the deriver a new shape you ADD a chain / GROW the cue ontology, never reorder a list.
    #
    # A chain receives the typed ``doc`` and shares ``_emit`` / ``_claim`` / ``_coref`` by closure.

    def _attr_scalar_binding(nsubj_tok):
        # POSSESSIVE-ATTRIBUTE SCALAR construction detector (Defect 1, subject-agnostic, grammar +
        # value-shape — NO attribute word-list). Recognizes "<possessor> <attribute-noun> is <literal
        # value span>" where the value is a SCALAR LITERAL (an address / serial / employee-id, etc.):
        #     "my address is 123 Main Street, Riverton, Ontario"  → (user,    address, "<verbatim>")
        #     "the laptop's serial is XR7-9920"                    → (laptop,  serial,  "XR7-9920")
        # The ENTIRE post-copula span is the SCALAR VALUE, kept VERBATIM and routed to entity_attributes
        # (the deriver tags it via scalar_datatype="string"); it is NEVER decomposed into a relationship
        # to a sub-entity. Returns a dict {possessor, attribute, value, comp} or None.
        #
        # The construction is detected ONLY structurally:
        #   • nsubj_tok is a NOUN that is the subject of a copula ``be`` AUX, and
        #   • it is POSSESSED — either a 1st-person possessive determiner ("my"/"our", Person=1∧Poss=Yes)
        #     → possessor "user"; OR a genitive possessor noun ("the laptop's …") → that noun, and
        #   • the attribute head-noun is NOT a kinship role (kinship_noun cue class) — "my daughter is 28"
        #     is an AGE, owned by the copula-measure chain, never an attribute scalar, and
        #   • the complement is a NOUN/PROPN/NUM value (not wh/interrogative), not negated, and
        #   • the value span is a SCALAR LITERAL: it contains a DIGIT, OR it is a multi-token nominal
        #     span (>=2 content tokens with a nominal head — a literal like "main street", not a bare
        #     single-word ADJ preference "blue" which the preference seam still owns).
        # Deterministic, fail-safe → None. Shared by the attr-scalar chain AND the possessive /
        # copula-measure suppression guards (so OUTCOME is order-independent — the chain owns the
        # construction and the twins step aside, the established own-the-construction / suppress-twin
        # pattern).
        try:
            if nsubj_tok is None or nsubj_tok.pos_ != "NOUN":
                return None
            if nsubj_tok.dep_ not in ("nsubj", "nsubjpass"):
                return None
            # POSSESSED-TYPED-ATOMIC DEFERRAL: the device-classification pre-pass OWNS the
            # "my <noun> is a <Type> at <structured-atomic>" construction (owns + instance_of, freeing
            # the noun as an entity so the atomic detector can host the scalar). Step aside so the whole
            # copula complement is NOT swallowed as one scalar keyed by the possessed noun.
            if nsubj_tok.i in _typed_atomic_suppress:
                return None
            head = nsubj_tok.head
            if head is None or not (head.lemma_ == "be" and head.pos_ == "AUX"):
                return None
            # POSSESSOR: 1st-person poss determiner → user; genitive NOUN/PROPN poss → that possessor.
            possessor = None
            for c in nsubj_tok.children:
                if c.dep_ != "poss":
                    continue
                try:
                    if c.morph.get("Person") == ["1"] and "Yes" in c.morph.get("Poss"):
                        possessor = "user"
                        break
                except Exception:  # noqa: BLE001
                    pass
                if c.pos_ in ("NOUN", "PROPN"):
                    possessor = (c.text or c.lemma_ or "").strip().lower()
                    break
            if not possessor:
                return None
            # KINSHIP head → an age/person reading, NOT an attribute scalar (let copula-measure own it).
            _hl = (nsubj_tok.lemma_ or nsubj_tok.text or "").strip().lower()
            if _hl in _kinship_nouns():
                return None
            # COMPLEMENT: a NOUN/PROPN/NUM value of the copula; skip wh/interrogative.
            comp = None
            for c in head.children:
                if c.dep_ in ("attr", "oprd", "dobj", "obj") and c.pos_ in ("NOUN", "PROPN", "NUM"):
                    try:
                        if "Int" in c.morph.get("PronType") or c.tag_ in ("WP", "WP$", "WDT", "WRB"):
                            continue
                    except Exception:  # noqa: BLE001
                        pass
                    comp = c
                    break
            if comp is None:
                return None
            # NEGATION ("my address is not X") → absence; defer (parity with the other chains).
            if any(c.dep_ == "neg" for c in head.children) or any(
                    c.dep_ == "neg" for c in comp.children):
                return None
            # VERBATIM value span = the complement's full subtree (covers "123 Main Street, Riverton,
            # Ontario" / "XR7-9920" / "a Tesla Model 3"), sliced from the sentence text so commas/
            # appositions/numbers survive. A LEADING DETERMINER ("a"/"an"/"the") is a function word,
            # NOT part of the value (THE HARD LINE — a function word is never a memory), so it is
            # dropped from the left edge → "Tesla Model 3", not "a Tesla Model 3".
            try:
                _sub = sorted(comp.subtree, key=lambda t: t.i)
                while _sub and _sub[0].pos_ == "DET":
                    _sub = _sub[1:]
                if not _sub:
                    return None
                _start = min(t.idx for t in _sub)
                _end = max(t.idx + len(t.text) for t in _sub)
                value = (sentence[_start:_end] or "").strip()
            except Exception:  # noqa: BLE001
                value = (comp.text or "").strip()
            if not value:
                return None
            # SCALAR-LITERAL gate: a digit anywhere, OR a multi-token nominal span (>=2 content tokens
            # with a nominal head). A bare single-word ADJ/NOUN value ("blue") is NOT scalar-literal —
            # it stays with the preference seam (unchanged). Value-shape, deterministic, no word list.
            _has_digit = any(ch.isdigit() for ch in value)
            try:
                _content = [t for t in comp.subtree if not t.is_punct and not t.is_space]
            except Exception:  # noqa: BLE001
                _content = []
            _head_nominal = comp.pos_ in ("NOUN", "PROPN", "NUM")
            if not (_has_digit or (len(_content) >= 2 and _head_nominal)):
                return None
            attribute = _np_phrase(nsubj_tok)
            if not attribute:
                return None
            return {"possessor": possessor, "attribute": attribute, "value": value, "comp": comp}
        except Exception as e:  # noqa: BLE001 — fail-safe: never break the deriver
            log.warning("linguistics.attr_scalar_binding_failed", error=str(e)[:160])
            return None

    def _chain_possessed_typed_atomic(doc):
        # POSSESSED-TYPED + STRUCTURED-ATOMIC (device classification). Emits the CLASSIFICATION read of
        # "my <noun> is a <Type> at <structured-atomic>" (pre-pass ``_typed_atomic_binds``):
        #   (possessor, owns, <noun>)         — grounds the noun as a first-class ENTITY, and
        #   (<noun>, instance_of, <Type>)     — the device type, when an indefinite-article Type is present.
        # The trailing structured-atomic literal (the IP/MAC/…) is deliberately NOT emitted here — the
        # /ingest atomic detector binds it as the typed scalar to the now-freed entity. Subject-agnostic;
        # no rel_type/domain literals (owns/instance_of are seeded hierarchy rels). Claims the subject +
        # type head so the attr-scalar / copula-state twins never re-read the swallowed value.
        for _tok, _pos, _type_tok in _typed_atomic_binds:
            _noun = _np_phrase(_tok) or (_tok.text or "").strip().lower()
            if not _noun:
                continue
            _emit(_pos, "owns", _noun, subj_tok=None, obj_tok=_tok)
            if _type_tok is not None:
                _tval = _np_phrase(_type_tok) or (_type_tok.text or "").strip().lower()
                if _tval and _tval != _noun:
                    _emit(_noun, "instance_of", _tval, subj_tok=_tok, obj_tok=_type_tok)
            try:
                _claim(_tok)
                if _type_tok is not None:
                    _claim(_type_tok)
            except Exception:  # noqa: BLE001
                pass

    def _chain_attr_scalar(doc):
        # ATTRIBUTE-SCALAR (Defect 1) — owns the possessive-attribute copula "my <attr> is <literal>"
        # / "<X>'s <attr> is <literal>" and emits the SCALAR value VERBATIM, keyed by the attribute
        # head-noun, on the possessor. tagged scalar_datatype="string" so the harvest routes it to
        # entity_attributes (the SCALAR path), never resolving the value to a UUID. The possessive /
        # copula-measure deriver twins step aside via the shared _attr_scalar_binding guard; the
        # preference seam defers (the harvest drops a preference edge that collides with a scalar edge).
        for tok in doc:
            if tok.dep_ not in ("nsubj", "nsubjpass"):
                continue
            b = _attr_scalar_binding(tok)
            if not b:
                continue
            rel = (b["attribute"] or "").strip().lower().replace(" ", "_")
            value = b["value"]
            if not rel or not value:
                continue
            _emit(b["possessor"], rel, value, subj_tok=tok, obj_tok=None, scalar_datatype="string")
            # CLAIM the whole construction so the residue guard never re-flags it. The SUBJECT side
            # (the attribute noun + its possessor / appositive fragments — "laptop", a tokenized-apart
            # "id") is a CLASSIFICATION/possessor, not a standalone entity; the VALUE span is a SCALAR
            # leaf, not a set of entities. Geo Locations inside the value span are ADDITIONALLY
            # hierarchized by the geo-containment chain (the dual-reading division).
            try:
                for _d in tok.subtree:
                    _claim(_d)
                for _d in b["comp"].subtree:
                    _claim(_d)
            except Exception:  # noqa: BLE001
                _claim(tok)

    def _chain_quoted_value(doc):
        # QUOTED-VALUE SCALAR — "my <attr> is '<verbatim quoted text>'" / "<X>'s <attr> is \"…\"".
        # A quotation-mark-delimited span after a possessive-attribute copula is a VERBATIM scalar
        # VALUE (a quote, a motto, a passphrase, a title) — the ENTIRE quoted text is the memory,
        # kept intact and routed to entity_attributes (scalar_datatype="string"), NEVER decomposed
        # into the clause spaCy parses INSIDE it. Detected purely structurally (subject-agnostic, NO
        # word list): a copula ``be`` AUX whose nsubj is POSSESSED (1st-person poss determiner → user,
        # or a genitive NOUN/PROPN possessor) + a balanced pair of QUOTE punct tokens after the copula.
        # Quotation marks are a closed grammatical/punctuation primitive. Fail-safe; _emit dedups.
        _QUOTES = {'"', "'", "‘", "’", "“", "”", "`"}
        for tok in doc:
            if tok.dep_ not in ("nsubj", "nsubjpass"):
                continue
            head = tok.head
            if head is None or not (head.lemma_ == "be" and head.pos_ == "AUX"):
                continue
            # POSSESSOR — 1st-person poss determiner → user; genitive NOUN/PROPN poss → that possessor.
            possessor = None
            for c in tok.children:
                if c.dep_ != "poss":
                    continue
                try:
                    if c.morph.get("Person") == ["1"] and "Yes" in c.morph.get("Poss"):
                        possessor = "user"
                        break
                except Exception:  # noqa: BLE001
                    pass
                if c.pos_ in ("NOUN", "PROPN"):
                    possessor = (c.text or c.lemma_ or "").strip().lower()
                    break
            if not possessor:
                continue
            if any(c.dep_ == "neg" for c in head.children):
                continue  # negated copula — absence deferred (parity with the other scalar chains)
            # Balanced QUOTE pair AFTER the copula head → the inner span is the verbatim value.
            q = [t for t in doc if t.i > head.i and (t.text or "").strip() in _QUOTES]
            if len(q) < 2:
                continue
            first, last = q[0], q[-1]
            if last.idx <= first.idx + len(first.text):
                continue
            value = (sentence[first.idx + len(first.text):last.idx] or "").strip()
            if not value or len(value) < 2:
                continue
            attribute = _np_phrase(tok)
            if not attribute:
                continue
            rel = attribute.replace(" ", "_")
            _emit(possessor, rel, value, subj_tok=tok, obj_tok=None, scalar_datatype="string")
            # CLAIM the subject subtree AND every token between the quotes so the residue guard never
            # re-flags the inner-clause tokens ("constant", "change") as uncovered content.
            try:
                for _d in tok.subtree:
                    _claim(_d)
                for _t in doc:
                    if first.i <= _t.i <= last.i:
                        _claim(_t)
            except Exception:  # noqa: BLE001
                _claim(tok)

    def _loc_obj_phrase(tok):
        # A LOCATION/container object phrase: head + left compound/amod modifiers AND right nummod
        # ("rack 4", "gps system") — but NOT appos/conj (those are separate containment members the
        # geo-list chain decomposes). Lowercased, structural, subject-agnostic.
        try:
            left = [c for c in tok.children if c.dep_ in ("compound", "amod") and c.i < tok.i]
            right = [c for c in tok.children if c.dep_ == "nummod" and c.i > tok.i]
            parts = ([m.text for m in sorted(left, key=lambda m: m.i)] + [tok.text]
                     + [m.text for m in sorted(right, key=lambda m: m.i)])
            return " ".join(p.strip() for p in parts if p and p.strip()).lower()
        except Exception:  # noqa: BLE001
            return (tok.text or "").strip().lower()

    def _chain_classification_containment(doc):
        # CLASSIFICATION + CONTAINMENT (Defect 2) — "X is a <type> [in|within|inside <Location>]":
        #   "Riverton is a city in Ontario" → instance_of(riverton, city) + located_in(riverton, ontario)
        #   "Paris is a city in France"      → instance_of(paris, city)     + located_in(paris, france)
        #   "the server is in rack 4"        →                                located_in(server, "rack 4")
        # Lays down the L4 founding anchors for the geographic (and any containment) domain at ingest;
        # the async ±6 backbone climb grows Ontario→Canada→… once these anchors exist. Subject-agnostic,
        # structural (copula + determiner-introduced TYPE complement; containment preposition + nominal
        # pobj) — NO city/province/country word zoo. instance_of files the named instance AT its type
        # (THE HARD LINE); located_in is the containment hierarchy edge (child located_in parent).
        _CONTAINMENT_PREPS = ("in", "within", "inside")  # closed containment-preposition primitive
        for tok in doc:
            if tok.dep_ not in ("nsubj", "nsubjpass"):
                continue
            head = tok.head
            if head is None or not (head.lemma_ == "be" and head.pos_ == "AUX"):
                continue
            if _is_first_person_personal_pronoun(tok):
                continue  # "I am ..." is the self/feeling/identity lane, never a geo classification
            if any(c.dep_ == "neg" for c in head.children):
                continue  # negated copula — absence deferred (parity)
            # An attribute-SCALAR construction ("my address is 123 …") is owned by _chain_attr_scalar.
            if _attr_scalar_binding(tok) is not None:
                continue
            subject = _np_phrase(tok)
            if not subject:
                continue
            # (A) TYPE complement → instance_of. A determiner-introduced common NOUN ("a city"/"the
            #     city") is a TYPE the subject is an instance of; a PROPN complement is a name (owned
            #     by the naming chains), a bare NOUN with no determiner is a state/role (other chains).
            type_comp = None
            for c in head.children:
                if c.dep_ in ("attr", "oprd") and c.pos_ == "NOUN" and \
                        any(g.dep_ == "det" for g in c.children):
                    try:
                        if "Int" in c.morph.get("PronType") or c.tag_ in ("WP", "WP$", "WDT", "WRB"):
                            continue
                    except Exception:  # noqa: BLE001
                        pass
                    type_comp = c
                    break
            if type_comp is not None:
                _type = _np_phrase(type_comp)
                if _type and _type != subject:
                    _emit(subject, "instance_of", _type, subj_tok=tok, obj_tok=type_comp)
            # (B) CONTAINMENT preposition (in/within/inside) reachable from the copula head (it may
            #     hang off the copula, the type complement, or an intervening "located" participle) +
            #     a nominal pobj → located_in(subject, pobj). A temporal pobj ("in an hour") is dropped.
            for prep in doc:
                if prep.dep_ != "prep":
                    continue
                if (prep.text or "").strip().lower() not in _CONTAINMENT_PREPS:
                    continue
                _cur = prep.head
                _hops = 0
                _reach = False
                while _cur is not None and _hops < 5:
                    if _cur.i == head.i:
                        _reach = True
                        break
                    if _cur.head is _cur:
                        break
                    _cur = _cur.head
                    _hops += 1
                if not _reach:
                    continue
                for pobj in prep.children:
                    if pobj.dep_ != "pobj" or pobj.pos_ not in ("NOUN", "PROPN"):
                        continue
                    try:
                        if (pobj.ent_type_ or "").upper() in ("DATE", "TIME"):
                            continue
                    except Exception:  # noqa: BLE001
                        pass
                    _loc = _loc_obj_phrase(pobj)
                    if _loc and _loc != subject:
                        _emit(subject, "located_in", _loc, subj_tok=tok, obj_tok=pobj)

    def _chain_geo_containment_list(doc):
        # GEOGRAPHIC COMMA-LIST CONTAINMENT (Defect 2) — "…, Riverton, Ontario" / "Riverton, Ontario"
        # → located_in(riverton, ontario): in a comma-separated run of LOCATION entities the TRAILING
        # element CONTAINS the leading one, so each adjacent pair (child, parent) is a containment edge
        # ("Riverton, Ontario, Canada" → located_in(riverton, ontario), located_in(ontario, canada)).
        # Subject-agnostic, NO place word zoo: members are identified by GLiNER2 Location typing
        # (token-aligned ent labels on the typed Doc); a pair is admitted only when the tokens BETWEEN
        # the two ents are purely a comma / "and" / whitespace (an enumerated containment list). When no
        # GLiNER2 types are present (raw-str parse), fall back to a PROPN appos/conj chain rooted at the
        # pobj of a locative preposition ("I live in Riverton, Ontario") — a grammatically-locative
        # context only, so a non-geo PROPN list is never swept in.
        def _emit_pair(child_txt, parent_txt, child_tok=None, parent_tok=None):
            c = (child_txt or "").strip().lower()
            p = (parent_txt or "").strip().lower()
            if c and p and c != p:
                _emit(c, "located_in", p, subj_tok=child_tok, obj_tok=parent_tok)

        # PRIMARY: GLiNER2 Location ents.
        _loc_ents = []
        try:
            for _e in (getattr(doc, "ents", []) or []):
                if (_e.label_ or "").strip().upper() in ("LOCATION", "GPE", "LOC"):
                    _loc_ents.append(_e)
        except Exception:  # noqa: BLE001
            _loc_ents = []
        if _loc_ents:
            _loc_ents = sorted(_loc_ents, key=lambda e: e.start)
            for _a, _b in zip(_loc_ents, _loc_ents[1:]):
                try:
                    _between = [t for t in doc[_a.end:_b.start] if not t.is_space]
                    if all(t.is_punct or t.pos_ == "CCONJ"
                           or (t.lemma_ or "").strip().lower() == "and" for t in _between):
                        _emit_pair(_a.text, _b.text)
                        for _t in list(_a) + list(_b):  # claim the spans (no residue false-alarm)
                            _claim(_t)
                except Exception:  # noqa: BLE001
                    continue
            return
        # FALLBACK (no GLiNER2 types): a PROPN appos/conj chain rooted at a locative-prep pobj.
        _LOC_PREPS = ("in", "within", "inside", "from", "at")
        for prep in doc:
            if prep.dep_ != "prep" or (prep.text or "").strip().lower() not in _LOC_PREPS:
                continue
            pobj = next((c for c in prep.children
                         if c.dep_ == "pobj" and c.pos_ == "PROPN"), None)
            if pobj is None:
                continue
            _chain = [pobj] + [c for c in pobj.children
                               if c.dep_ in ("appos", "conj") and c.pos_ == "PROPN"]
            _chain = sorted(_chain, key=lambda t: t.i)
            for _a, _b in zip(_chain, _chain[1:]):
                _emit_pair(_a.text, _b.text, child_tok=_a, parent_tok=_b)

    def _chain_residence_geo_bridge(doc):
        # RESIDENCE→CITY BRIDGE (composite address) — DEV/DESIGN-address-composite.md §5 first slice.
        # "I live at <street> in <city>[, <prov>]" folds ONLY the first prep (at→street SCALAR); the
        # one-prep break in _svo_predicate_token (~1911) DROPS the 2nd locative PP "in <city>", so the
        # city floats — _chain_geo_containment_list builds located_in(city, prov) rooted at the CITY,
        # never linked to the user (the ORPHAN). This bridge ADOPTS the orphan: it emits the RELATIONAL
        # residence edge lives_in(<subject>, <city>) ALONGSIDE the verbatim residence scalar, so the walk
        # from the subject reaches the whole nested place (subject →lives_in→ city →located_in→ prov).
        #
        # THE HARD LINE: the verbatim address stays the SCALAR (the memory — emitted by _chain_svo /
        # _chain_attr_scalar); the city/province are L4 PLACES (located_in, geo-list); this chain adds
        # ONLY the relational lives_in bridge — NO street decomposition, NO place word zoo, NO parser
        # library. The city is GLiNER2 Location-typed (LOCATION/GPE/LOC ents) — the SAME typing the
        # geo-containment chain uses.
        #
        # Fires ONLY for a RESIDENCE construction, discriminated by METADATA (NOT a verb list):
        #   • PATH A (residence verb): a content verb whose folded SVO predicate is a location-category,
        #     MUTABLE residence rel (_residence_predicate_identities — born_in EXCLUDED as immutable),
        #     with a PERSON subject (first-person→user, or a PROPN name — lives_in head_types=Person).
        #     An employment/acquisition clause ("work at Google in Mountain View", "bought a house in
        #     Riverton") folds a NON-residence predicate → never bridged. The single-city case ("I live
        #     in Toronto") is already the SVO object → skipped here (no duplicate lives_in).
        #   • PATH B (address scalar): a possessed attribute-scalar copula ("my address is <street>,
        #     <city>, <prov>", _attr_scalar_binding) whose VALUE span carries a city that HEADS a
        #     located_in chain (>=2 nested Location ents) → adopt the leading (most-specific) city for
        #     the possessor. ("my favorite city is Toronto" is not even an attr-scalar — the scalar-
        #     literal gate drops a bare single-word value — so it never bridges.)
        # Subject-agnostic, deterministic, fail-safe.
        _loc_ents = []
        try:
            for _e in (getattr(doc, "ents", []) or []):
                if (_e.label_ or "").strip().upper() in ("LOCATION", "GPE", "LOC"):
                    _loc_ents.append(_e)
        except Exception:  # noqa: BLE001
            _loc_ents = []
        if not _loc_ents:
            return
        _loc_ents = sorted(_loc_ents, key=lambda e: e.start)

        _LOC_PREPS = ("in", "within", "inside", "at")  # closed locative-preposition primitive

        def _under_locative_prep(_tok):
            # True when _tok is the pobj of a locative preposition (a genuine "in <city>" PP).
            try:
                _h = _tok.head
                return (_h is not None and _h.dep_ == "prep" and _tok.dep_ == "pobj"
                        and (_h.text or "").strip().lower() in _LOC_PREPS)
            except Exception:  # noqa: BLE001
                return False

        # PATH A — residence VERB with a dropped locative city PP.
        _res_ids = _residence_predicate_identities()
        for tok in doc:
            if tok.pos_ != "VERB":
                continue
            _lemma = (tok.lemma_ or tok.text or "").strip().lower()
            if not _lemma or _lemma == "be" or _lemma in _naming_verbs():
                continue
            _pred = _svo_predicate_token(tok, exclude_idx=_date_token_idx)
            if not _pred or _norm_rel_identity(_pred) not in _res_ids:
                continue  # not a residence clause (work/meet/buy/born → never bridged)
            _subj_tok = next((c for c in tok.children if c.dep_ in ("nsubj", "nsubjpass")), None) \
                or _carried_subject_token(tok)
            if _subj_tok is None:
                continue
            # PERSON subject only (lives_in head_types=Person): first-person→user, or a PROPN name.
            if _is_first_person_personal_pronoun(_subj_tok):
                subject = "user"
            elif _subj_tok.pos_ == "PROPN":
                subject = (_subj_tok.text or _subj_tok.lemma_ or "").strip().lower()
                _cr = _coref(_subj_tok)
                if _cr:
                    subject = _cr
            else:
                continue  # a non-person subject (server/office/company) is not a residence
            if not subject:
                continue
            _svo_obj = _svo_object_head(tok, exclude_idx=_date_token_idx)
            _subtree_idx = {t.i for t in tok.subtree}
            _city_ent = None
            for _e in _loc_ents:
                if not any(t.i in _subtree_idx for t in _e):
                    continue  # a Location outside this residence clause's subtree
                _root = _e.root
                if _svo_obj is not None and _root.i == _svo_obj.i:
                    continue  # the city IS already the SVO object (single-city "live in Toronto")
                if not _under_locative_prep(_root):
                    continue  # only a genuine dropped locative PP "in <city>"
                _city_ent = _e
                break
            if _city_ent is None:
                continue
            _city = (_city_ent.text or "").strip().lower()
            if not _city or _city == subject:
                continue
            # distribute=False: adopt ONLY the leading city — the province (an appos/conj of the city)
            # is reached via located_in(city, province), NOT a second lives_in(user, province).
            _emit(subject, "lives_in", _city, subj_tok=_subj_tok, obj_tok=_city_ent.root,
                  distribute=False)
            for _t in _city_ent:  # claim the adopted city span (geo-list already did — idempotent)
                _claim(_t)
            return  # one residence bridge per clean sentence

        # PATH B — "my address is <street>, <city>, <province>" attribute-scalar: adopt the leading city.
        for tok in doc:
            if tok.dep_ not in ("nsubj", "nsubjpass"):
                continue
            _b = _attr_scalar_binding(tok)
            if not _b:
                continue
            _comp = _b.get("comp")
            _possessor = _b.get("possessor")
            if _comp is None or not _possessor:
                continue
            try:
                _comp_idx = {t.i for t in _comp.subtree}
            except Exception:  # noqa: BLE001
                continue
            _cities = [_e for _e in _loc_ents if any(t.i in _comp_idx for t in _e)]
            if len(_cities) < 2:
                continue  # require a genuine city⊂container composite (not a lone place value)
            _cities = sorted(_cities, key=lambda e: e.start)
            # Adopt the leading typed place that is NOT the STREET/address line. GLiNER2 often types the
            # whole "156 Cedar Street South" as a Location too — but a street/address line carries a
            # house/unit NUMBER, so we skip a leading Location bearing a numeric token and take the first
            # numberless place = the CITY. A digit is a closed grammatical primitive (no street-suffix
            # word zoo); deterministic, subject-agnostic. Fail-safe → the leading place if all carry
            # digits (still adopts a real place; the located_in chain reaches the rest).
            _city_ent = next((_e for _e in _cities
                              if not any(ch.isdigit() for ch in (_e.text or ""))), _cities[0])
            _city = (_city_ent.text or "").strip().lower()
            if not _city or _city == _possessor:
                continue
            _emit(_possessor, "lives_in", _city, subj_tok=tok, obj_tok=_city_ent.root,
                  distribute=False)
            return

    def _chain_employment(doc):
        # EMPLOYMENT construction (role-predication + affiliation). Consumes the bindings the
        # ``_emp_binds`` pre-pass computed (verb in the ``employment_verb`` cue class + a grammatical
        # subject + an "as <role>" and/or "at|for <org>" frame) and emits:
        #   • occupation(<subject>, <full role NP>)  — the whole title incl. a trailing "III"/"Sr."
        #   • works_for(<subject>, <org NP>)          — the EMPLOYMENT VERB decides the relation, so a
        #     university/school object is never flipped to ``educated_at``.
        # Subject-agnostic (1st-person → ``user``; a named 3rd-person subject → that name). The role/org
        # objects are TYPE-agnostic. We DO NOT pass ``subj_tok`` to ``_emit`` (mirroring the kinship
        # chain): the employment construction is an AUTHORITATIVE deterministic reading, so a prep-blind
        # GLiNER2-minted rel for the pair (which would otherwise wrongly re-assert ``educated_at``)
        # cannot override it — ``_minted_rel_for_pair`` needs BOTH tokens, so omitting the subject token
        # disables the override while ``obj_tok`` still supplies GLiNER2 object typing. Fail-safe.
        def _role_phrase(head):
            # The FULL role title span (subject-agnostic, no literals): the head noun + its ENTIRE
            # left-modifier run via spaCy ``left_edge`` — so a multi-level compound ("Systems Analyst
            # III", where "Systems" is a compound of "Analyst" which is a compound of the head "III")
            # is captured WHOLE, not just the head's direct child ("analyst iii"). Mirrors the shipped
            # full-value span logic. A leading determiner ("a"/"the") is stripped. The trailing "at
            # <org>" PP nests to the RIGHT of the head, so left_edge..head never includes it. Fail-safe
            # → ``_np_phrase``.
            try:
                _lo = head.left_edge.i
                _toks = [doc[_k] for _k in range(_lo, head.i + 1)]
                while _toks and (_toks[0].pos_ == "DET" or _toks[0].dep_ == "det"
                                 or _toks[0].is_punct or _toks[0].is_space):
                    _toks.pop(0)
                _ph = " ".join(t.text.strip() for t in _toks if t.text and t.text.strip()).lower()
                return _ph or _np_phrase(head)
            except Exception:  # noqa: BLE001 — never break capture on a span build
                return _np_phrase(head)

        for (_v, _subj, _role, _org) in _emp_binds:
            if _is_first_person_personal_pronoun(_subj):
                subject = "user"
            else:
                subject = _np_phrase(_subj) or (_subj.text or _subj.lemma_ or "").strip().lower()
                _cr = _coref(_subj)
                if _cr:
                    subject = _cr
            if not subject:
                continue
            if _role is not None:
                role_phrase = _role_phrase(_role)  # full title → "systems analyst iii"
                if role_phrase and len(role_phrase) >= 2:
                    _emit(subject, "occupation", role_phrase, obj_tok=_role)
            if _org is not None:
                org_phrase = _object_value_phrase(_org)
                if org_phrase and len(org_phrase) >= 2:
                    _emit(subject, "works_for", org_phrase, verb_tok=_v, obj_tok=_org)

    def _chain_svo(doc):
        # SVO backbone (+ governing-verb date + conjunct distribution). Each non-copula content verb
        # with a subject and an object → (subject, verb-lemma[+particle], object). First-person subject
        # → "user". Naming verbs are owned by analyze_naming (caller seam) → skipped here.
        _naming = _naming_verbs()
        for tok in doc:
            if tok.pos_ != "VERB":
                continue
            if tok.i in _ni_suppress:
                continue  # the named-instance chain owns this clause's verb ("we have a son …")
            if tok.i in _emp_suppress:
                continue  # the employment chain owns this verb ("work as … at …") — no SVO dup
            if tok.i in _has_measure_suppress:
                continue  # the has-measure chain owns this verb ("has a score of 9.8") — scalar, no SVO
            if tok.i in _quantity_verb_suppress:
                continue  # the quantity-of chain owns this verb ("take 500 mg of metformin") — scalar
            if tok.i in _alias_suppress:
                continue  # the alias chain owns this verb ("goes by Dee") — no (she, go_by, dee)
            lemma = (tok.lemma_ or tok.text or "").strip().lower()
            if not lemma or lemma == "be":
                continue
            if lemma in _naming:
                continue  # naming construction → analyze_naming owns it (caller runs that seam)
            subj_tok = next((c for c in tok.children if c.dep_ in ("nsubj", "nsubjpass")), None)
            if subj_tok is None:
                # DENSE DECOMPOSITION: a subordinate/coordinated predicate ("…, attributed to X",
                # "and was prescribed Y", "overruled Baker") has no subject of its own — carry it by
                # coreference from the clause it shares/modifies so this verb still yields its fact.
                subj_tok = _carried_subject_token(tok)
            if subj_tok is None:
                continue
            if _is_first_person_personal_pronoun(subj_tok):
                subject = "user"
            else:
                subject = (subj_tok.text or subj_tok.lemma_ or "").strip().lower()
                _cr = _coref(subj_tok)
                if _cr:
                    subject = _cr
            if not subject:
                continue
            # ASPECTUAL SUBJECT-CONTROL DESCENT (split SVO) — parity with analyze_svo_relations: for
            # "I started working with Rachel" the matrix carries the subject, the xcomp activity verb
            # carries the object. Descend into a REALIZED-activity aspectual xcomp and run recovery on
            # it, keeping the matrix subject. Gated to realized activity only (no unrealized intent —
            # see ``_aspectual_activity_xcomp``). The naming guard is re-checked against the xcomp lemma.
            svo_head = tok
            _xc = _aspectual_activity_xcomp(tok)
            if _xc is not None:
                _xc_lemma = (_xc.lemma_ or _xc.text or "").strip().lower()
                if _xc_lemma and _xc_lemma != "be" and _xc_lemma not in _naming:
                    svo_head = _xc
            # Negation on EITHER the matrix or the descended activity verb → skip (absence deferred).
            if any(c.dep_ == "neg" for c in tok.children) or (
                svo_head is not tok and any(c.dep_ == "neg" for c in svo_head.children)
            ):
                continue  # negated clause — absence modeling deferred (parity with analyze_svo)
            # PART 1: exclude PEELED date tokens so the atomizer's "see_on march 1st" wobble never
            # folds "on" into the predicate nor lifts the date phrase as the object — both
            # atomizations land (user, see, house)@event_date. ``_date_token_idx`` is in closure.
            predicate = _svo_predicate_token(svo_head, exclude_idx=_date_token_idx, include_agent=True)
            if not predicate:
                continue
            obj_tok = _svo_object_head(svo_head, exclude_idx=_date_token_idx, include_agent=True)
            if obj_tok is None:
                # OBJECT-PRONOUN COREF RESCUE: a 3rd-person personal pronoun in object position
                # ("I started working with HER on 2/15") is not a mergeable entity, so
                # _svo_object_head returns None and the clause — WITH ITS DATE — is dropped
                # (the "her" reading of the Rachel-start event, the LongMemEval 2c63a862 miss).
                # Resolve the pronoun to the nearest preceding named person (intra-doc PROPN, else
                # the prior-atom NP) via the SAME _person_coref the copula-measure / passive-event
                # chains already use, and emit the edge with the RESOLVED NAME so it grounds to
                # (user, work_with, rachel) @ event_date. Grammar-only (PronType=Prs, Person=3
                # inside _person_coref); fail-safe: no antecedent → no edge (today's drop, never a
                # guessed name). obj_tok is the pronoun token so the date still binds to this clause.
                _pron = _svo_object_pronoun(svo_head, exclude_idx=_date_token_idx)
                if _pron is not None:
                    _res = _person_coref(_pron)
                    if _res:
                        # Tag the ORIGINAL pronoun surface so the entry-peel date reattach can bind
                        # this clause's date to the resolved-name edge (the residue still says "her",
                        # not "rachel"). Whole-token pronoun, morphology already gated by _person_coref.
                        _emit(subject, predicate, _res, verb_tok=svo_head,
                              obj_tok=_pron, subj_tok=subj_tok,
                              object_pronoun=(_pron.text or "").strip().lower() or None)
                continue
            for _ct in _np_conjuncts(obj_tok):  # conjunct/dash-list distribution
                if _ct.i in _ni_suppress:
                    continue  # a named-instance collective/type the binding chain owns
                if _ct.i in _kin_collective:
                    continue  # kinship COLLECTIVE ("kids") — members route via the kinship rel (dash chain)
                # VALUE-SPAN build: a NAMED multi-token object ("156 Cedar St. S") keeps its leading
                # number (the value is the whole name); a bare count ("3 cats") does NOT — so a scalar
                # value is captured in FULL without absorbing a quantifier into a relational object.
                obj_phrase = _object_value_phrase(_ct)
                if not obj_phrase or len(obj_phrase) < 2:
                    continue
                if obj_phrase in ("it", "they", "them"):
                    _cr = _coref(_ct)
                    if _cr:
                        obj_phrase = _cr
                # Bind the date to the SVO head (the activity verb when descended — the temporal PP
                # "on 2/15" attaches to "working", not the aspectual matrix).
                _emit(subject, predicate, obj_phrase, verb_tok=svo_head, obj_tok=_ct, subj_tok=subj_tok)

    def _chain_intransitive(doc):
        # INTRANSITIVE STATE/EVENT (gap-2 §10.4; DESIGN-state-typing.md owner decision) — a content
        # verb with a SUBJECT and NO object ("my car's GPS broke last week", "the server crashed
        # yesterday"). Today such a clause would emit nothing, so a date on its governing verb has
        # nothing to bind to. THE OWNER'S DECISION (supersedes the earlier SCALAR routing):
        #   • the SUBJECT (gps / server / car) is the THING — grounded into L4 normally by its own
        #     SVO/possessive/genitive chains; this chain does NOT touch its grounding.
        #   • the STATE ("broke") is a TYPED, REUSABLE hierarchy NODE — the same ``break`` node a
        #     GPS, a server and a leg all point to — emitted RELATIONALLY through ``_STATE_REL``
        #     (``has_state``, re-minted RELATIONAL → tail_types={Concept}, storage_target='facts'),
        #     EXACTLY like ``feels`` for a feeling. This is the structural TWIN of the feeling case:
        #     an associative edge to a typed, SELF-BUILDING hierarchy node, never a scalar leaf.
        #     We therefore pass ``obj_tok=tok`` (restoring the span so GLiNER2 + the thin-type
        #     machinery can TYPE it and the backbone-attach / async grounder can PLACE it) and keep
        #     ``verb_tok=tok`` so ``_date_for_verb`` still binds the date to THIS state fact only.
        # The state NODE IDENTITY is the spaCy LEMMA of the state token, lowercased — DETERMINISTIC
        # and byte-identical across "broke"/"breaks"/"broke" → ONE shared node (convergence-by-
        # identity via EntityRegistry UUID v5; no cosine, no hardcoded broke→broken map, no verb
        # list). Detected STRUCTURALLY (verb has nsubj, no dobj/obj/attr/oprd/pobj object), NOT from
        # a verb list. The rel is the single canonical ontology-defined state predicate, resolved via
        # the overlay, NOT the user's open-class verb lemma. Subject-agnostic.
        for tok in doc:
            if tok.pos_ != "VERB":
                continue
            if tok.i in _ni_suppress:
                continue  # the named-instance nickname relcl ("who goes by Theo") — not a state
            if tok.i in _emp_suppress:
                continue  # the employment chain owns this verb — never "(user, has_state, work)"
            if tok.i in _quantity_verb_suppress:
                continue  # the quantity-of chain owns this verb ("NAS has 40 TB of storage") — scalar
            if tok.i in _passive_suppress:
                continue  # the dated passive-event chain owns "was <Xed>" — never (x, has_state, <part>)
            if tok.i in _alias_suppress:
                continue  # the alias chain owns this verb ("goes by"/"known as") — no has_state twin
            lemma = (tok.lemma_ or tok.text or "").strip().lower()
            if not lemma or lemma == "be":
                continue
            if lemma in _naming_verbs():
                continue
            subj_tok = next((c for c in tok.children if c.dep_ in ("nsubj", "nsubjpass")), None)
            if subj_tok is None:
                # DENSE DECOMPOSITION: an objectless subordinate/coordinated predicate ("…, decided in
                # 2019", "and was cited by …") shares/modifies a subject — carry it by coreference so
                # the state/event (with its date) still lands. Fail-safe → skip (today's behavior).
                subj_tok = _carried_subject_token(tok)
            if subj_tok is None:
                continue
            # ASSERTION POLARITY (Q1 negated-state capture): a ``neg`` dependency on THIS state
            # verb is NOT a drop — it is a NEGATED genuine STATE ("it is not functioning", "the
            # printer is not working"). We CAPTURE it (carry ``_neg``) so it reads back NEGATED,
            # never as its positive opposite. This is the ConText/NegEx assertion polarity of an
            # intransitive STATE only; a TRANSITIVE negated clause ("I did not break it") has a
            # dobj → it falls out at the object guards below (not this lane), and a
            # correction/retraction ("forget X", "it's Luna not Bella") was routed away by the
            # intent gate BEFORE the deriver ran — neither is touched here. Read deterministically
            # from the spaCy ``neg`` dep already at hand; no word list, no LLM.
            _neg = any(c.dep_ == "neg" for c in tok.children)
            # STRUCTURAL intransitivity: the verb governs NO object of any kind. ``_svo_object_head``
            # only sees NOUN/PROPN objects (the SVO chain's mergeable-entity objects); a verb with a
            # PRONOUN/clausal object ("she helped ME", "I told THEM") is still TRANSITIVE — not an
            # intransitive state — so we ALSO reject any direct/indirect object child of ANY pos.
            if _svo_object_head(tok, exclude_idx=_date_token_idx, include_agent=True) is not None:
                continue  # SVO owns a NOUN/PROPN-object verb (a date-only pobj is NOT an object —
                #           "patched on <date>" is an objectless dated STATE this chain must capture)
            if any(c.dep_ in ("dobj", "obj", "iobj", "dative", "obl") for c in tok.children):
                continue  # transitive (incl. pronoun/oblique object) → not an objectless state
            # ── STRUCTURAL STATE PRE-FILTER (over-capture firewall) ──────────────────────────────
            # The bare object-test above is TOO WEAK on its own: it admits control/mental verbs whose
            # content lives in a clausal complement ("I want TO go", "I think THAT…"), the EMBEDDED
            # clause verb itself ("I think I will WAIT" — "wait" is a ccomp), and ongoing present-
            # tense ACTIVITIES ("I work", "I run") — none of which is a CHANGE-OF-STATE of the
            # subject. Emitting ``has_state work/think/want/wait`` is junk (live DB: ``think`` even
            # promoted to Class B). We discriminate STRUCTURALLY (dep_/tag_/morph only, NO verb list):
            #   (1a) REJECT a verb that IS ITSELF an embedded clausal complement — it predicates
            #        about the matrix clause, not a top-level state of this subject ("…I will WAIT").
            #        CARVE-OUT — RESULTATIVE PAST-PARTICIPLE SMALL CLAUSE (subject-agnostic, structural):
            #        the periphrastic-causative / resultative "I got my bike REPAIRED [in mid-February]",
            #        "I had my car SERVICED last week" parse the participle as a ccomp of the light verb
            #        ("got"/"had") whose OWN nsubj is the PATIENT ("bike"/"car"). That participle IS a
            #        genuine dated STATE/event ABOUT the patient (the bike underwent repair — the exact
            #        twin of "the gps BROKE"), NOT a control/mental complement ("I will WAIT"/"I think
            #        X"). Admit it ONLY when the complement is a PAST PARTICIPLE (VerbForm=Part ∧
            #        Past/Perfective) carrying its OWN CONCRETE NOUN/PROPN nsubj — so the state files on
            #        a resolvable entity (a bare-pronoun patient "I want IT done" has no unambiguous
            #        antecedent → DEFER, never guess). Every infinitival/finite control complement
            #        ("wait"/"think") lacks Part → still falls through to the reject. spaCy morph + dep
            #        only, no verb/lemma list; the date binds via the shared governing-verb map (the PP
            #        "in mid-February" attaches to the participle). Fail-safe → today's reject.
            if tok.dep_ in ("ccomp", "xcomp"):
                _resultative_small_clause = False
                try:
                    _own_subj = next(
                        (c for c in tok.children
                         if c.dep_ in ("nsubj", "nsubjpass") and c.pos_ in ("NOUN", "PROPN")), None)
                    if _own_subj is not None and "Part" in tok.morph.get("VerbForm") and (
                            "Past" in tok.morph.get("Tense") or "Perf" in tok.morph.get("Aspect")):
                        _resultative_small_clause = True
                except Exception:  # noqa: BLE001 — undecidable → keep the reject
                    _resultative_small_clause = False
                if not _resultative_small_clause:
                    continue
            #   (1b) REJECT a control/mental/raising verb outright — its objectlessness is illusory;
            #        the content is in the embedded clause, not a state of the subject.
            if _verb_has_clausal_complement(tok):
                continue
            #   (2) CONFIRMED change-of-state (past/perfective/participle, or copula-ADJ resultant)
            #       → a DURABLE ``has_state`` (Class B downstream; the typed-node ladder self-builds).
            #       UNCERTAIN (present-tense activity that slipped the object-test) → TENTATIVE: we
            #       still CAPTURE it (never drop), but the consumer routes it to the short-term
            #       Class-C lane (store_context) so it only promotes C→B if it later grounds/converges
            #       as a state-type via the EXISTING re_embedder convergence machinery — "broke"~
            #       "crashed"~"failed" converge to one state node and promote; "work"/"run" never
            #       ground as a state and decay (30-day C). No durable B for an uncertain state.
            _state_tentative = not _verb_realizes_resultant_state(tok)
            # A NEGATED present-tense activity ("it does not work") is FORCED durable: a negated
            # activity IS a definite non-functional STATE, not an uncertain ongoing activity, so it
            # must not be siphoned to the tentative Class-C lane (where the positive opposite would
            # be the surviving reading on decay). Negation makes the state CONFIRMED.
            if _neg:
                _state_tentative = False
            if _is_first_person_personal_pronoun(subj_tok):
                subject = "user"
            else:
                subject = (subj_tok.text or subj_tok.lemma_ or "").strip().lower()
                _cr = _coref(subj_tok)
                if _cr:
                    subject = _cr
            if not subject:
                continue
            # The state NODE: the spaCy LEMMA of the state verb, lowercased — the byte-identical
            # normalized identity that makes "broke"/"breaks" converge to ONE reusable node by
            # identity (EntityRegistry UUID v5). NOT the inflected surface (which would split nodes),
            # NOT a hand-rolled suffix/irregular map. We emit it RELATIONALLY with ``obj_tok=tok`` so
            # the span is typeable (GLiNER2 + thin-type) and resolves to a UUID node the backbone-
            # attach / async grounder places into a self-building state hierarchy (mirrors ``feels``).
            state = (tok.lemma_ or tok.text or "").strip().lower()
            if not state:
                continue
            _emit(subject, _STATE_REL, state, verb_tok=tok, obj_tok=tok, subj_tok=subj_tok,
                  tentative=_state_tentative, negated=_neg)

    def _chain_passive_event(doc):
        # DATED PASSIVE EVENT → the NAMED ENTITY (pre-pass ``_passive_binds``). For ANY
        # "<entity> was <PASSIVE-PARTICIPLE> [prep <date>]" emit ONE dated fact whose PREDICATE is the
        # participle (never an object) and whose value carries the ``event_date`` — filed on the entity
        # the clause is ABOUT. Fully entity-agnostic (a person / dog / server / product) and predicate-
        # agnostic (born / founded / hired / provisioned / released — the participle SURFACE is the rel,
        # no lemma list): the entity is resolved by the SAME appositive/naming + possessive + discourse-
        # coref the deriver already uses, and the date binds via the shared governing-verb date map. The
        # value is emitted as a SCALAR (``scalar_datatype`` set) so the date is a scalar LEAF, not a
        # relationship graph object — THE HARD LINE (the participle is the place/relation, the date the
        # value). A miss → NULL (the pre-pass only enrolled dated passives). No role/person/kinship gate.
        for _v, _psubj, _iso, _gran in _passive_binds:
            _entity = None
            _etok = None
            # PREFER THE PROPER NAME (subject-agnostic, ONE grammatical rule for ALL possessed-noun+name
            # shapes). "my <possessed-noun> <Name> was <participle>" reaches the deriver in two spaCy
            # parse shapes: (i) the name is an ``appos`` of the common-noun subject ("my wife ADA",
            # "my server APOLLO"); (ii) the common noun AND the name are BOTH attached as ``nsubjpass``
            # siblings of the participle ("my cat cat/MITTENS", "my dog dog/REX"). In BOTH the
            # date-bearing entity is the trailing PROPER NAME — never the possessed common noun (which
            # keeps its own owns/has_pet/kinship edge). Resolve the NAME first, regardless of whether
            # the possessed noun is kinship/animal/thing (no role/type literal): a PROPN among the
            # participle's subject candidates, else a PROPN ``appos`` of one. Only when there is NO name
            # do we fall to first-person → user, 3rd-person coref, or the possessed noun itself.
            _subj_cands = [c for c in _v.children if c.dep_ in ("nsubjpass", "nsubj")]
            _name_tok = next((c for c in _subj_cands if c.pos_ == "PROPN"), None)
            if _name_tok is None:
                for _c in _subj_cands:
                    _ap = next((g for g in _c.children
                                if g.dep_ == "appos" and g.pos_ == "PROPN"), None)
                    if _ap is not None:
                        _name_tok = _ap
                        break
            if _name_tok is not None:
                # (a) the trailing PROPER NAME is the named instance the event files at (THE HARD LINE).
                _entity = (_name_tok.text or "").strip().lower()
                _etok = _name_tok
            elif _is_first_person_personal_pronoun(_psubj):
                # (b) first person ("I was hired in 2020") → the user.
                _entity = "user"
            elif _psubj.pos_ == "PRON":
                # (c) 3rd-person pronoun ("she/he/it/they was …") → discourse/near-antecedent coref.
                _cr = _coref(_psubj) or _person_coref(_psubj)
                if _cr:
                    _entity = _cr
                else:
                    _entity = (_psubj.text or "").strip().lower()
                    _etok = _psubj
            else:
                # (d) an un-named possessed/definite common-noun subject ("my server was provisioned",
                #     "the product was released") — the thing itself is the entity. No person gate.
                _entity = (_psubj.lemma_ or _psubj.text or "").strip().lower()
                _etok = _psubj
            if not _entity:
                continue
            # PREDICATE = the participle SURFACE (lowercased) — the same word the user would ask with
            # ("when was X born / founded / hired"). NOT the lemma (which would split "born"↔"bear").
            _pred = (_v.text or _v.lemma_ or "").strip().lower()
            if not _pred:
                continue
            # VALUE by GRANULARITY — never surface a fabricated day for a year/month-granular date. A
            # full DAY stores the ISO date (datatype "date" → value_date); coarser granularities store
            # the granularity-trimmed string the user gave ("1958", "1985-07") as an untyped-string
            # scalar (the strict date validator wants full YYYY-MM-DD). ``scalar_datatype`` FORCES the
            # scalar route so the date is a leaf value, not a resolved entity/graph object.
            _iso10 = _iso[:10]
            if _gran == "year":
                _val, _dt = _iso10[:4], "string"
            elif _gran == "month":
                _val, _dt = _iso10[:7], "string"
            else:
                _val, _dt = _iso10, "date"
            _emit(_entity, _pred, _val, verb_tok=_v, obj_tok=None,
                  subj_tok=(_etok if _etok is not None else _psubj),
                  scalar_datatype=_dt, distribute=False)

    def _chain_dated_occurrence(doc):
        # DATED OCCURRENCE / EVENT NP → (user, participated_in, <NP>) @ event_date. A NOMINAL that is
        # directly postmodified by a date PP ("<NP> on/at <DATE>") is a SCHEDULED OCCURRENCE, not an
        # owned thing — an object does not carry an "on <date>"; an EVENT does. This is the eventive-
        # noun TWIN of the dated SVO capture ("I attended a <workshop> on <date>"): when the eventive
        # noun heads its own clause ("my team meeting on the 17th", "I have a meeting on the 17th",
        # "…in my upcoming team meeting on the 17th") spaCy mis-tags it as a gerund VERB with no
        # object, so neither the SVO chain (no verb-borne object) nor the possessive chain (binds the
        # date-less possessed noun) attaches the WHEN — the occurrence's date is silently dropped and a
        # duration walk that needs two event dates comes up one short. The host token DOUBLES as the
        # date's governing verb in ``_date_by_verb``, so emitting with ``verb_tok=host`` binds the
        # resolved ``event_date`` via the SAME ``_date_for_verb`` path the workshop's dated SVO uses.
        #
        # Subject-agnostic + grammar-driven — NO event-noun word list. The trigger is purely: (1) the
        # host of a RESOLVED date PP is a NOMINAL occurrence host (a NOUN, or a gerund-noun spaCy
        # mis-tagged VERB whose only nominal dependents are compound/nsubj NOUN modifiers) with NO
        # object and NO personal/proper subject — so a genuine dated action clause ("I attended … on
        # <date>", governed by a real content verb with a dobj/subject) is EXCLUDED and never
        # double-minted; and (2) 1st-person framing (a "my/our" possessive in the host's NP, or a
        # governing clause whose subject is "I"/"we") so the occurrence is the user's — an arbitrary
        # third-party dated noun stays residue for the growth path rather than being force-anchored.
        for _gov_i, _dv in list(_date_by_verb.items()):
            try:
                _host = doc[_gov_i]
                _iso_o, _gran_o = _dv
            except Exception:  # noqa: BLE001 — per-host fail-safe
                continue
            if not _iso_o:
                continue
            _kids = list(_host.children)
            # (1a) a real object or a personal/proper SUBJECT ⇒ genuine action clause, not a bare
            #      occurrence NP → leave the date to the SVO/passive chains (no double-mint).
            if any(_c.dep_ in ("dobj", "dative", "attr", "oprd", "obj", "iobj") for _c in _kids):
                continue
            if any(_c.dep_ in ("nsubj", "nsubjpass") and _c.pos_ in ("PRON", "PROPN")
                   for _c in _kids):
                continue
            # (1b) the host must be a NOMINAL occurrence head: a NOUN outright, or a gerund-noun
            #      mis-tagged VERB. The gerund test is morphological and load-bearing: a mis-tagged
            #      eventive noun ("meeting", "gathering") is a NON-FINITE gerund/participle
            #      (VerbForm=Ger|Part, tag VBG) whereas a genuine finite clause verb ("car BROKE last
            #      week") is VerbForm=Fin — so a real dated action clause is EXCLUDED and never minted
            #      as a bogus (user, participated_in, "car broke"). We also require a compound/nsubj
            #      NOUN modifier (the scattered NP, e.g. "team") and NO auxiliary ("was meeting …").
            if _host.pos_ == "NOUN":
                pass
            elif _host.pos_ == "VERB":
                try:
                    _vf = _host.morph.get("VerbForm")
                except Exception:  # noqa: BLE001
                    _vf = []
                if not ("Ger" in _vf or "Part" in _vf):
                    continue
                if any(_c.dep_ in ("aux", "auxpass") for _c in _kids):
                    continue
                if not any(_c.dep_ in ("compound", "nsubj") and _c.pos_ == "NOUN"
                           for _c in _kids):
                    continue
            else:
                continue
            # (2) 1st-person framing: a possessive in the host's subtree, or a governing "I"/"we" clause.
            _first_person = False
            try:
                for _d in _host.subtree:
                    _mp = _d.morph
                    if _mp.get("Person") == ["1"] and "Yes" in _mp.get("Poss"):
                        _first_person = True
                        break
            except Exception:  # noqa: BLE001
                pass
            if not _first_person:
                _cur = _host
                for _ in range(6):
                    if _cur is None or _cur.head is _cur:
                        break
                    _cur = _cur.head
                    if any(_c.dep_ in ("nsubj", "nsubjpass")
                           and _is_first_person_personal_pronoun(_c) for _c in _cur.children):
                        _first_person = True
                        break
            if not _first_person:
                continue
            # (3) Reconstruct the occurrence NP: the host surface + its left compound/nsubj NOUN
            #     modifiers (spaCy scattered "team" as an nsubj/compound of the mis-tagged "meeting").
            #     Descriptive ADJ modifiers (e.g. the temporal "upcoming") are left OUT so the event
            #     name is its canonical noun compound ("team meeting"), not "upcoming team meeting".
            _mods = sorted(
                (_c for _c in _kids
                 if _c.dep_ in ("compound", "nsubj") and _c.pos_ == "NOUN" and _c.i < _host.i),
                key=lambda t: t.i)
            _np = " ".join([(_m.text or "").strip() for _m in _mods]
                           + [(_host.text or _host.lemma_ or "").strip()]).strip().lower()
            if not _np:
                continue
            _emit("user", "participated_in", _np, verb_tok=_host, obj_tok=_host)
            # Cover the scattered NP-modifier nouns folded into the occurrence object so the residue
            # guard does not false-alarm on them (they ARE captured, as part of "team meeting").
            _claim(*_mods)
            # Suppress the possessive/SVO junk for the scattered NP-modifier nouns (e.g. the
            # mis-scoped (user, owns, "upcoming team") from _chain_possessive off the "team" nsubj).
            _occ_suppress.add(_host.i)
            for _m in _mods:
                _occ_suppress.add(_m.i)

    def _chain_possessive(doc):
        # POSSESSIVE (relational-vs-sortal split). ``my X`` (1st-person poss pronoun) → (user, owns, X).
        # ``X's Y`` genitive → relational/sortal split via the relational_noun cue class (overlay):
        # a RELATIONAL/component/kinship Y → its inherent relation; a SORTAL Y → generic related_to.
        _relnouns = _relational_nouns()
        for tok in doc:
            if tok.dep_ != "poss":
                continue
            head = tok.head
            if head is None or head.pos_ not in ("NOUN", "PROPN"):
                continue
            # DATED-OCCURRENCE GUARD: the possessed noun is a scattered modifier of a date-postmodified
            # eventive NP already claimed by ``_chain_dated_occurrence`` ("my … team meeting on the
            # 17th" → the (user, participated_in, "team meeting") @date edge). Skip so the mis-scoped
            # (user, owns, "upcoming team") twin is never minted alongside it.
            if head.i in _occ_suppress or tok.i in _occ_suppress:
                continue
            # NAMED-INSTANCE GUARD: "My dog Rex is a poodle" — "dog" is a bound TYPE (Rex is its
            # appositive name) owned by the named-instance chain, NOT an ``owns`` of a bare type. Skip a
            # possessed head whose token the named-instance chain suppressed (else "(user, owns, dog)").
            if head.i in _ni_suppress:
                continue
            # EMPLOYMENT GUARD: the org affiliation PP ("… at the University of Springfield's Computing
            # Services") is owned by the employment chain — skip a possessed/possessor token inside that
            # span so we never leak the genitive "(computing services, related_to, springfield)" junk.
            if head.i in _emp_suppress or tok.i in _emp_suppress:
                continue
            # ATTRIBUTE-SCALAR GUARD (Defect 1): "my address is 123 …" / "the laptop's serial is X"
            # is owned by _chain_attr_scalar (the value is a VERBATIM SCALAR, not an ownership of the
            # bare attribute noun). Skip so we never mint (user, owns, address) / (serial, related_to,
            # laptop) for the construction the attr-scalar chain captures. Shared detector, structural.
            if _attr_scalar_binding(head) is not None:
                continue
            # INTERPLAY GUARD (Fix 2): the "X's name is Y" naming construction is owned by the
            # GENITIVE-NAME chain (it binds Y as the person + attaches the kin relation there). The
            # possessive chain must stay OUT of it in BOTH directions:
            #   (a) the possessor leg ("my mother's …"): the head role-noun ("mother") is itself a
            #       ``poss`` of a "name" nsubj-of-copula → emitting (mother, parent_of, user) here would
            #       leave "mother" as a standalone entity (Fix 2 collapses it into the named person).
            #   (b) the naming leg ("…'s name is Carol"): the possessed head IS the naming noun "name"
            #       (nsubj of a copula) → emitting (name, related_to, mother) here is the spurious
            #       "name"-as-entity leak. Skip it; the genitive-name chain mints the real edges.
            # Detected grammatically (lemma "name" + nsubj-of-be), NO word list.
            def _is_name_copula_nsubj(_n):
                return (_n is not None and (_n.lemma_ or "").strip().lower() == "name"
                        and _n.dep_ in ("nsubj", "nsubjpass")
                        and _n.head is not None
                        and _n.head.lemma_ == "be" and _n.head.pos_ == "AUX")
            if head.dep_ == "poss" and _is_name_copula_nsubj(head.head):
                continue   # (a) possessor leg of "my mother's name is Carol"
            # (a2) APOSTROPHE-STRIPPED possessor leg (Failure 1). When the atomizer drops the genitive
            # apostrophe ("my wife's name" → "my wifes name") the role noun attaches to "name" as a
            # ``compound``/``nmod`` (not ``poss``) — so guard (a) misses it and this chain would mint
            # the PHANTOM (wifes, spouse, user) off the surface. Step aside for the SAME frame the
            # genitive-name chain now recovers: a compound/nmod role of a "name"-copula nsubj whose
            # lemma is a kinship/relational cue. Cue-gated so an ordinary compound ("my user name is
            # Bob") keeps its reading. Grammar + cue-class driven, subject-agnostic, no noun literal.
            if head.dep_ in ("compound", "nmod") and _is_name_copula_nsubj(head.head) \
                    and (head.lemma_ or head.text or "").strip().lower() in (
                        _kinship_nouns() | _relational_nouns()):
                continue
            if _is_name_copula_nsubj(head):
                continue   # (b) naming leg — head IS "name"; never (name, related_to, role)
            # INTERPLAY GUARD (Fix B, Part 2 — flag-gated): "My sister is Sarah" is owned by the
            # COPULA-NAME chain (it binds the kin rel to the named person + registers the role as an
            # alias). The possessive chain must stay OUT of it, else it would mint (sister, sibling_of,
            # user) — the very parallel role entity Part 2 collapses. Detected grammatically: the
            # possessed head IS the nsubj of a copula ``be`` whose attr complement is a PROPER NAME.
            # Only active when the flag is ON, so flag-OFF behavior is byte-identical.
            if SPINE_NAMING_CHAIN and head.dep_ in ("nsubj", "nsubjpass") and head.pos_ == "NOUN" \
                    and head.head is not None and head.head.lemma_ == "be" \
                    and head.head.pos_ == "AUX":
                if any(_c.dep_ in ("attr", "oprd", "dobj", "obj") and _c.pos_ == "PROPN"
                       and not any(_g.dep_ == "det" for _g in _c.children)
                       for _c in head.head.children):
                    continue   # the copula-name chain owns "my sister is Sarah"
            # INTERPLAY GUARD (predicate-nominal role): "<Filler NP> is my <role-noun>" ("Globex
            # Industries is my employer") is owned by ``_chain_copula_role_predicate`` — the
            # possessed head is the copula's attr/oprd COMPLEMENT (the nonverbal predicate) and the
            # nsubj is the FILLER NP the role binds to. Step aside ONLY when that chain will
            # actually fire (1st-person possessive + role_noun map hit + a nominal non-pronoun
            # subject), so the (user, owns, "employer") twin is never minted; any other shape keeps
            # today's reading (no capture is ever lost). Same guard style as the genitive-name /
            # copula-name interplay guards above. Grammatical + metadata-driven, no noun literal.
            if head.dep_ in ("attr", "oprd") and head.head is not None \
                    and head.head.lemma_ == "be" and head.head.pos_ == "AUX":
                try:
                    _g_first = (tok.morph.get("Person") == ["1"]
                                and "Yes" in tok.morph.get("Poss"))
                except Exception:  # noqa: BLE001
                    _g_first = False
                if _g_first and (head.lemma_ or head.text or "").strip().lower() in _role_noun_map():
                    if any(_s.dep_ in ("nsubj", "nsubjpass") and _s.pos_ in ("PROPN", "NOUN")
                           and not _is_first_person_personal_pronoun(_s)
                           for _s in head.head.children):
                        continue   # the copula-role chain owns "Globex Industries is my employer"
            head_phrase = _np_phrase(head)
            if not head_phrase or len(head_phrase) < 2:
                continue
            is_first_poss = False
            try:
                is_first_poss = (
                    tok.morph.get("Person") == ["1"] and "Yes" in tok.morph.get("Poss")
                )
            except Exception:  # noqa: BLE001
                is_first_poss = False
            if is_first_poss:
                # KINSHIP-AWARE (Fix 1): "my mother" is NOT ownership — the head noun is a kinship
                # role. Test the head noun's lemma against the kinship_noun cue class; if it is
                # kinship, emit the SPECIFIC kin relation from the cue-class metadata (mother→parent_of,
                # son→child_of, …) with the HEAD as subject and the USER as object — "my mother" →
                # (mother, parent_of, user), i.e. mother is the parent of me. Metadata-driven, NOT an
                # in-code ``if noun=="mother"`` literal. The user entity is the existing grammatical
                # self-ref (Person=1, via is_first_poss above), not a token check. A NON-kinship head
                # keeps today's ownership reading.
                _hl = (head.lemma_ or head.text or "").strip().lower()
                if _hl in _kinship_nouns():
                    _kin = _inherent_relation_for_noun(_hl)
                    _emit(head_phrase, _kin, "user", obj_tok=head)
                else:
                    _emit("user", "owns", head_phrase, obj_tok=head)
                continue
            if tok.pos_ in ("NOUN", "PROPN"):
                possessor = (tok.text or tok.lemma_ or "").strip().lower()
                if not possessor or possessor == head_phrase:
                    continue
                head_lemma = (head.lemma_ or head.text or "").strip().lower()
                if head_lemma in _relnouns or head_phrase.split()[-1] in _relnouns:
                    # "X's gps" → (gps, part_of, X); "X's mother" → (mother, related_to, X).
                    rel = _inherent_relation_for_noun(head_phrase.split()[-1])
                    _emit(head_phrase, rel, possessor, obj_tok=head, subj_tok=tok)
                else:
                    # SORTAL Y → generic related_to (let the walk resolve), not an ownership claim.
                    _emit(head_phrase, "related_to", possessor, obj_tok=head, subj_tok=tok)

    def _chain_copula_state(doc):
        # COPULA RESULTANT STATE ("the printer is idle", "the server is down") — a copular clause
        # whose ADJ/ADV complement predicates a non-functional / resultant STATE of a NON-self
        # subject. ``_chain_intransitive`` skips the copula ``be`` (line ~1971), so without this chain
        # "X is down/idle" produces nothing. This is the structural TWIN of the eventive intransitive
        # state: it emits the SAME canonical RELATIONAL ``_STATE_REL`` to a typed, reusable state node
        # (node identity = the complement's spaCy LEMMA, byte-identical so "idle"/"down" converge),
        # CONFIRMED (an adjectival/predicative state IS a resultant state — no tentative tier here).
        #
        # SELF-subjects are DELIBERATELY EXCLUDED: "I am worried"/"I feel sad" is a FEELING and is
        # owned by the copula/feeling seam (``analyze_copula`` → ``feels``), not a device-style state.
        # Detection is grammatical (1st-person personal-pronoun subject), NOT a word list. A negated
        # copula is skipped here (P2 negated-state assessment is handled in ``_chain_intransitive``'s
        # design note; the negation/correction gate is never touched). Subject-agnostic, structural.
        for tok in doc:
            if tok.dep_ not in ("nsubj", "nsubjpass"):
                continue
            head = tok.head
            if head is None or not (head.lemma_ == "be" and head.pos_ == "AUX"):
                continue
            # SELF subject → feeling seam owns it; never a copula STATE here.
            if _is_first_person_personal_pronoun(tok):
                continue
            # ASSERTION POLARITY (Q1): a ``neg`` on the copula + ADJ/ADV complement is a NEGATED
            # genuine STATE ("the server is not down", "the printer is not idle") — CAPTURE it,
            # never drop. It must read back negated. A NOMINAL complement ("that is not my dog")
            # is excluded by the ADJ/ADV-only complement test below — that is an identity negation
            # the correction gate already owns, not a state. The intent gate ran first; this never
            # touches it. Read from the spaCy ``neg`` dep on the copula head; no word list.
            _neg = any(c.dep_ == "neg" for c in head.children)
            # the predicative complement: an ADJ (acomp/attr) or a stative ADV particle (advmod ADV,
            # e.g. "is down"). A NOMINAL complement ("is a teacher") is a role/identity, NOT a state —
            # excluded so we never re-route occupation/naming clauses through the state predicate.
            comp = None
            for c in head.children:
                if c.dep_ in ("acomp", "attr") and c.pos_ == "ADJ":
                    comp = c
                    break
                if c.dep_ == "advmod" and c.pos_ == "ADV":
                    comp = c
                    break
            if comp is None:
                continue
            # MEASUREMENT GUARD (Fix 3 interplay): "she is 62 years old" / "he is 6 feet tall" parse the
            # measurement adjective ("old"/"tall") as the acomp complement, with a NUM-bearing UNIT noun
            # ("years"/"feet") in its subtree. That is a SCALAR measurement owned by
            # ``_chain_copula_measure`` (→ age/height), NOT a resultant STATE. Skip it here so we never
            # mint a junk ``(she, has_state, old)`` alongside the real ``(she, age, 62)``. Detected
            # grammatically: a NUM nummod under a NOUN in the complement's subtree — no word list.
            #
            # ADDITIVE GUARD (Q8 over-reach fix): suppress the state ONLY when the measurement chain will
            # ACTUALLY land its scalar — i.e. the would-be scalar rel (the UNIT noun's unit_scalar
            # mapping) ADMITS this subject's GLiNER2 type as its head. On a NON-admitted subject ("the
            # tomatoes are 2-3 inches tall" → OBJECT, ``height`` head_types={Person}) the measure chain
            # STEPS ASIDE (it would only mint a quarantined dead-C scalar), so this state chain must NOT
            # suppress — it captures ``(tomatoes, has_state, tall)`` (head_types={ANY}, survives) and the
            # crop stays walkable. Untyped subject (raw-str / bare pronoun) → admitted → unchanged
            # (the person tests). Mirrors ``_chain_copula_measure``; metadata-driven, subject-agnostic.
            _measure_unit_tok = None
            try:
                for _d in comp.subtree:
                    if _d.pos_ == "NOUN" and any(
                            _c.pos_ == "NUM" and _c.dep_ == "nummod" for _c in _d.children):
                        _measure_unit_tok = _d
                        break
            except Exception:  # noqa: BLE001 — fail-safe
                _measure_unit_tok = None
            if _measure_unit_tok is not None:
                # the would-be scalar rel: the unit noun's unit_scalar mapping (year→age, inch→height);
                # an unmapped unit → no scalar would be minted → no suppression (let the state through).
                _units = _unit_scalar_map()
                _ul = (_measure_unit_tok.lemma_ or _measure_unit_tok.text or "").strip().lower()
                _ut = (_measure_unit_tok.text or "").strip().lower()
                _would_rel = _units.get(_ul) or _units.get(_ut)
                if _would_rel:
                    try:
                        _subj_et = (tok.ent_type_ or "").strip()
                    except Exception:  # noqa: BLE001
                        _subj_et = ""
                    if _scalar_rel_admits_subject(_would_rel, _subj_et):
                        continue  # the measure chain lands its scalar → this state would be junk; skip
            # a QUESTION complement ("what is it?") is not a value — skip wh / interrogative.
            try:
                if "Int" in comp.morph.get("PronType") or comp.tag_ in ("WP", "WP$", "WDT", "WRB"):
                    continue
            except Exception:  # noqa: BLE001 — fail-safe
                pass
            subject = (tok.text or tok.lemma_ or "").strip().lower()
            _cr = _coref(tok)
            if _cr:
                subject = _cr
            if not subject:
                continue
            state = (comp.lemma_ or comp.text or "").strip().lower()
            if not state:
                continue
            # CONFIRMED (durable B): a predicative adjectival/stative state IS a resultant state.
            # ``verb_tok=head`` so a date on the copula governing verb still binds; ``obj_tok=comp`` so
            # the state span is typeable / resolves to a reusable node (mirrors the intransitive emit).
            _emit(subject, _STATE_REL, state, verb_tok=head, obj_tok=comp, subj_tok=tok,
                  tentative=False, negated=_neg)

    def _chain_copula_name(doc):
        # COPULA-APPOSITIVE ROLE↔NAME (Fix B, Part 2). "My sister is Sarah" / "My colleague is Bob" /
        # "My server is Apollo" — a copular clause whose nsubj is a 1st-person-POSSESSED ROLE noun and
        # whose attr complement is a PROPER NAME. spaCy parses it as:
        #     My      poss   -> sister     (Person=1, Poss=Yes)
        #     sister  nsubj  -> is         (the ROLE noun)
        #     is      ROOT (AUX, lemma be)
        #     Sarah   attr   -> is         (the PROPER NAME)
        # THE HARD LINE: the NAMED person (Sarah) is the entity; the ROLE (sister) is a slot/alias on
        # it — NEVER a parallel "sister" entity. We bind the kin/role relation to the NAMED person and
        # register the role surface as an ``also_known_as`` alias of that person, so a later atom
        # ("My sister is 28") resolves sister→sarah and the scalar lands on the named person.
        #   • KINSHIP role  → (name, <kin>, user)        e.g. (sarah, sibling_of, user)
        #   • NON-kin role  → (name, has_role, role)     e.g. (bob, has_role, colleague) — a generic
        #                      role-slot the walk resolves; still binds the name as the entity.
        # PLUS the role-alias leg: (name, also_known_as, role) so the role resolves to the person.
        # Gated behind SPINE_NAMING_CHAIN; subject-agnostic, kin metadata-driven (kinship_noun cue
        # rail), grammatical (poss morphology + copula). Fail-safe: flag off / any miss → no emit.
        if not SPINE_NAMING_CHAIN:
            return
        for tok in doc:
            if tok.dep_ not in ("nsubj", "nsubjpass"):
                continue
            if tok.pos_ != "NOUN":
                continue  # the ROLE is a common noun (sister/mother/colleague); a PROPN nsubj is a name
            head = tok.head
            if head is None or not (head.lemma_ == "be" and head.pos_ == "AUX"):
                continue
            # SELF subject is the feeling/identity seam, never a role-name binding.
            if _is_first_person_personal_pronoun(tok):
                continue
            # the role noun must carry a 1st-person possessive determiner ("my"/"our") — that is what
            # makes "sister" a USER-anchored role rather than a free-standing subject. Grammatical
            # (Person=1 ∧ Poss=Yes), NOT a token list. Without it we never fire (no over-capture of
            # "the printer is Apollo"-style clauses where the subject is not user-possessed).
            poss_self = False
            for c in tok.children:
                try:
                    if (c.dep_ == "poss" and c.morph.get("Person") == ["1"]
                            and "Yes" in c.morph.get("Poss")):
                        poss_self = True
                        break
                except Exception:  # noqa: BLE001
                    continue
            if not poss_self:
                continue
            # the PROPER NAME — the copula's attr/oprd complement that is a PROPN (a NOUN the parser
            # mis-tagged is excluded here: a common-noun complement is a type/role, owned elsewhere).
            proper = None
            for c in head.children:
                if c.dep_ in ("attr", "oprd", "dobj", "obj") and c.pos_ == "PROPN":
                    try:
                        if "Int" in c.morph.get("PronType") or c.tag_ in ("WP", "WP$", "WDT", "WRB"):
                            continue
                    except Exception:  # noqa: BLE001
                        pass
                    proper = c
                    break
            if proper is None:
                continue
            # A determiner-introduced complement ("a poodle") is a TYPE, not a name — never reached
            # here (PROPN guard) but defensive: skip if the complement has a det child.
            if any(gc.dep_ == "det" for gc in proper.children):
                continue
            proper_name = (proper.text or "").strip().lower()
            role_lemma = (tok.lemma_ or tok.text or "").strip().lower()
            if not proper_name or not role_lemma or proper_name == role_lemma:
                continue
            # NEGATION ("my sister is not Sarah") → absence; skip (parity with the other chains).
            if any(c.dep_ == "neg" for c in head.children):
                continue
            # BIND the kin/role relation to the NAMED person. Kinship role → its specific kin rel
            # (metadata-driven via the kinship_noun rail); a non-kin role → generic has_role with the
            # role as the object slot (still anchors the name as the entity).
            if role_lemma in _kinship_nouns():
                _kin = _inherent_relation_for_noun(role_lemma)
                _emit(proper_name, _kin, "user", obj_tok=None, subj_tok=proper)
            else:
                _emit(proper_name, "has_role", role_lemma, obj_tok=None, subj_tok=proper)
            # ROLE-ALIAS leg: register the ROLE surface as an alias of the NAMED person so a later
            # atom ("My sister is 28") resolves sister→sarah and the scalar lands on the named person.
            # THE HARD LINE: the role is a slot/alias ON the named instance, not a second entity.
            _emit(proper_name, "also_known_as", role_lemma, obj_tok=None, subj_tok=proper)
            # COLLAPSE the role noun: claim it so the residue guard never flags it as a dropped entity.
            _claim(tok)

    def _chain_copula_role_predicate(doc):
        # COPULA PREDICATE-NOMINAL ROLE ("<Filler NP> is my <role-noun>"). "Globex Industries is my
        # employer" is a textbook copular clause whose NONVERBAL PREDICATE is the first-person-
        # possessed role noun — live spaCy parse: Industries(nsubj, PROPN, compound Globex) ← is
        # (AUX, lemma be, ROOT) → employer(attr, NOUN) carrying poss "my" (Person=1|Poss=Yes|
        # PronType=Prs). Grounding: UD's copula analysis makes the predicate NOMINAL the clause's
        # nonverbal predicate (universaldependencies.org/u/dep/cop.html); spaCy's ``attr`` is the
        # copular predicate "attribute" label (github.com/explosion/spacy/blob/master/spacy/
        # glossary.py). This construction previously had NO owning chain: ``_chain_possessive``
        # read only "my employer" → (user, owns, "employer"), the SUBJECT NP was never bound, and
        # the lone role noun got GLiNER2-mistyped downstream (Animal → has_pet junk).
        #
        # THE FIX mirrors the kinship/copula-name precedent: the ROLE is a slot resolved via a DB
        # cue map (role_noun cue class, ``_role_noun_map``: {noun: rel_type}, seeded migration 142,
        # grown per-tenant); the FILLER (the copula SUBJECT NP) is the entity. Map CONVENTION:
        # the rel runs FROM the user TO the filler — employer→works_for ⇒ (user, works_for,
        # "globex industries"). A role noun OUTSIDE the map → NO emit (honest no-op; never
        # fabricate a relation), and the possessive chain's interplay guard also steps aside ONLY
        # on a map hit, so unmapped constructions keep today's behavior byte-identical.
        # Grammatical (Poss=Yes ∧ Person=1 morphology — never a token list), metadata-driven,
        # subject-agnostic, deterministic. NO GLiNER2 mistype fix needed: binding the subject NP
        # removes the lone role-noun node that seeded the mistype.
        _rmap = _role_noun_map()
        if not _rmap:
            return
        for tok in doc:
            if tok.dep_ not in ("nsubj", "nsubjpass"):
                continue
            head = tok.head
            if head is None or not (head.lemma_ == "be" and head.pos_ == "AUX"):
                continue
            # The FILLER is a nominal subject NP (PROPN "Globex Industries", or a lowercase-robust
            # common-noun NP); a pronoun / 1st-person subject is NOT this construction.
            if tok.pos_ not in ("PROPN", "NOUN") or _is_first_person_personal_pronoun(tok):
                continue
            # the ROLE noun: the copula's attr/oprd COMMON-NOUN complement. (A PROPN complement is
            # a NAME — the copula-name/naming seams own that; never this lane.)
            comp = None
            for c in head.children:
                if c.dep_ in ("attr", "oprd") and c.pos_ == "NOUN":
                    comp = c
                    break
            if comp is None:
                continue
            # the role noun must be FIRST-PERSON-POSSESSED ("my"/"our" — Person=1 ∧ Poss=Yes read
            # from morphology, never a token list): that is what anchors the role to the user.
            _poss_self = False
            for c in comp.children:
                try:
                    if (c.dep_ == "poss" and c.morph.get("Person") == ["1"]
                            and "Yes" in c.morph.get("Poss")):
                        _poss_self = True
                        break
                except Exception:  # noqa: BLE001
                    continue
            if not _poss_self:
                continue
            role_lemma = (comp.lemma_ or comp.text or "").strip().lower()
            rel = _rmap.get(role_lemma)
            if not rel:
                continue  # unmapped role → honest no-op (the possessive chain keeps its reading)
            # NEGATION ("Globex is not my employer") → absence; skip (parity with sibling chains).
            if any(c.dep_ == "neg" for c in head.children):
                continue
            filler = _np_phrase(tok)
            if not filler or filler == role_lemma:
                continue
            # (user, <mapped_rel>, <filler NP>). obj_tok=tok so GLiNER2's live type on the subject
            # NP (Organization/Person) rides the edge; verb_tok=head so a clause date could bind.
            _emit("user", rel, filler, verb_tok=head, obj_tok=tok)
            # COLLAPSE the role noun: claim it so no other chain / the residue guard reads the lone
            # "employer" as a standalone entity (the seed of the GLiNER2 Animal mistype).
            _claim(comp)

    def _chain_alias_predicate(doc):
        r"""THIRD-PARTY / NAMED-SUBJECT ALIAS PREDICATE (Failure 2). Capture a NON-first-person
        subject's nickname/alias and file it via ``also_known_as`` on that person:
            "She prefers to be called Liv"   → (olivia, also_known_as, liv)   [she → Olivia by coref]
            "She goes by Dee"                → (dana,  also_known_as, dee)
            "He is known as Sammy"           → (sam,   also_known_as, sammy)
            "Dana goes by Dee"               → (dana,  also_known_as, dee)    [named subject]

        THE HARD LINE: a NAME is FILED via the alias registry (``also_known_as``), NEVER classified
        into L4. First-person self-naming ("I prefer to be called Max") is OWNED by the affect/
        preference seam (analyze_naming / _detect_preference_states on the UNION path) and is SKIPPED
        here to avoid a double capture.

        SUBJECT RESOLUTION is grammatical, NOT a token list. A 3rd-person PERSONAL pronoun subject
        (she/he/they — UD ``PronType=Prs`` ∧ ``Person=3``; see UD feature spec
        https://universaldependencies.org/u/feat/Person.html and
        https://universaldependencies.org/u/feat/PronType.html, exposed on spaCy ``Token.morph`` per
        https://spacy.io/api/morphologizer) is resolved by ``_person_coref`` to the NEAREST PRECEDING
        named person — the classic recency/salience heuristic of pronominal anaphora resolution
        ("the most recent antecedent that agrees in gender and number"; Jurafsky & Martin, *Speech and
        Language Processing* 3rd ed., ch. Coreference Resolution; Hobbs 1978, "Resolving Pronoun
        References"). A PROPN subject is used directly; a non-name common-noun subject → no-op (never
        guess).

        TWO grammatical shapes, both deterministic + metadata-driven (NO name/verb literal):
          (A) NAMING-VERB complement — a verb whose lemma is in the ``naming_verb`` cue class
              (call/name/title/…) governing the assigned name as an ``oprd``/``attr``/``dobj`` PROPN.
              Covers the passive xcomp "prefers to be called <Name>" (subject on the matrix verb) and
              the direct "is called <Name>" (subject = the verb's own nsubjpass).
          (B) ALIAS-PP idiom — a verb whose lemma is in the ``alias_predicate`` cue MAP
              ({verb: particle}; go→by, know→as, refer→as) governing a ``prep`` whose surface == the
              mapped particle with a PROPN ``pobj``. Covers "goes by <Name>", "is known as <Name>",
              "referred to as <Name>". Mirrors the codebase's existing go-by nickname idiom
              (``_nickname_run``); the phrasal alias vocabulary is DB-held/growable, never in code.
        """
        _naming = _naming_verbs()
        _alias_pp = _alias_predicate_map()

        def _alias_subject(verb):
            # the alias construction's SUBJECT token: the verb's own nsubj/nsubjpass, else — when the
            # verb is a clausal complement ("prefers TO BE CALLED X") — the matrix head's nsubj.
            st = next((c for c in verb.children if c.dep_ in ("nsubj", "nsubjpass")), None)
            if st is None and verb.dep_ in ("xcomp", "ccomp", "acl", "relcl", "advcl") \
                    and verb.head is not None:
                st = next((c for c in verb.head.children
                           if c.dep_ in ("nsubj", "nsubjpass")), None)
            return st

        def _resolve_alias_subject(st):
            # → (subject_surface | None, is_first_person). First-person self is flagged so the caller
            # defers to the preference/naming seam. A 3rd-person pronoun resolves via _person_coref;
            # a PROPN is used directly; anything else → None (never guess a non-name subject).
            if st is None:
                return None, False
            if _is_first_person_personal_pronoun(st):
                return None, True
            if st.pos_ == "PROPN":
                return (st.text or "").strip().lower(), False
            _cr = _person_coref(st)
            if _cr:
                return _cr, False
            return None, False

        def _own_alias_verb(verb, name_tok, st):
            # The verb + name complement ARE an alias construction. ALWAYS own the verb (suppress the
            # intransitive/copula-state/SVO has_state twin + claim) so "goes by Dee" never degrades to
            # (she, has_state, go) — even when the subject cannot be resolved. Then EMIT the alias edge
            # only when the subject resolved to a concrete person and is NOT first-person (the self case
            # is owned by the preference/naming seam). A negated construction is skipped (absence).
            _alias_suppress.add(verb.i)
            _claim(verb, name_tok)
            if any(c.dep_ == "neg" for c in verb.children):
                return
            subj_surface, is_first = _resolve_alias_subject(st)
            if is_first or not subj_surface:
                return
            name = (name_tok.text or "").strip().lower()
            if not name or name == subj_surface:
                return
            _emit(subj_surface, "also_known_as", name, obj_tok=None, subj_tok=st)

        for tok in doc:
            if tok.pos_ not in ("VERB", "AUX"):
                continue
            lemma = (tok.lemma_ or tok.text or "").strip().lower()
            if not lemma:
                continue
            st = _alias_subject(tok)

            # (A) NAMING-VERB complement — "called/named <PROPN>".
            if lemma in _naming:
                name_tok = next(
                    (c for c in tok.children
                     if c.dep_ in ("oprd", "attr", "dobj", "obj") and c.pos_ == "PROPN"), None)
                if name_tok is not None:
                    _own_alias_verb(tok, name_tok, st)
                continue

            # (B) ALIAS-PP idiom — "<verb> <particle> <PROPN>" (go→by, know→as, refer→as).
            _particle = _alias_pp.get(lemma)
            if _particle:
                name_tok = None
                for c in tok.children:
                    if c.dep_ != "prep" or (c.text or "").strip().lower() != _particle:
                        continue
                    name_tok = next((g for g in c.children
                                     if g.dep_ == "pobj" and g.pos_ == "PROPN"), None)
                    if name_tok is not None:
                        break
                if name_tok is not None:
                    _own_alias_verb(tok, name_tok, st)

    def _chain_genitive_name(doc):
        # GENITIVE NAME-BINDING (Fix 2). "[poss] <relational-noun>'s name is <PROPN>"
        #   "my mother's name is Carol"     → (carol, parent_of, user)   [Carol is the named entity]
        #   "John's mother's name is Susan" → (susan, parent_of, john)   [Susan is the named entity]
        # spaCy parses this as: a copula ``be`` whose nsubj is the NOUN "name" (lemma 'name'); that
        # "name" carries a ``poss`` role-noun child (mother/son/wife); the role-noun carries its OWN
        # ``poss`` possessor (a 1st-person poss pronoun → user, or a PROPN → that person); the copula's
        # ``attr`` is the PROPER NAME assigned. We BIND the proper name as the PERSON and attach the
        # kinship role to THAT PERSON, COLLAPSING the role-noun (it never becomes a standalone entity).
        # Subject-agnostic (works for "my mother" and "John's mother"); the kin rel is metadata-driven
        # (kinship_noun cue class); the naming-noun anchor is the lemma 'name' (the existing naming seam
        # already treats 'name' as the naming construction — see analyze_possessive_predication /
        # _IDENTITY_PATTERNS). Reuses _kinship_nouns + the kin-rel map; no in-code noun literal.
        for tok in doc:
            # copula be with an nsubj whose lemma is the naming noun "name"
            if tok.dep_ not in ("nsubj", "nsubjpass"):
                continue
            head = tok.head
            if head is None or not (head.lemma_ == "be" and head.pos_ == "AUX"):
                continue
            if (tok.lemma_ or "").strip().lower() != "name":
                continue
            # the role-noun: a poss child of "name" (mother/son/wife/…). The genitive apostrophe
            # (``'s``, spaCy PART dep=case) makes the role a ``poss`` dependent — unambiguous.
            role = next((c for c in tok.children
                         if c.dep_ == "poss" and c.pos_ in ("NOUN", "PROPN")), None)
            # APOSTROPHE-STRIPPED GENITIVE ROBUSTNESS (Failure 1 root cause). The LLM atomizer /
            # normalization sometimes DROPS the genitive apostrophe ("my wife's name" → "my wifes
            # name"). spaCy then re-parses the role noun as a ``compound``/``nmod`` dependent of
            # "name" (tag NNS, but the LEMMA is still the singular kin noun — "wifes"→lemma "wife"),
            # and the 1st-person possessive ("my") attaches to the ROLE noun itself, not to "name".
            # Without this the genitive-name frame never matches → the role is not collapsed and
            # ``_chain_possessive`` mis-mints (wifes, spouse, user) off the surface "wifes" (a
            # PHANTOM entity) while a copula seam reads "wifes name" as a type. We accept the compound
            # frame ONLY when the role lemma is a KINSHIP/relational cue — so an ordinary noun-noun
            # compound ("my user name is Bob", "the code name is X") is NEVER mistaken for a collapsed
            # genitive. The possessor ("my"/"John's") may attach to EITHER the role noun ("my wifes
            # name" → my→wifes) OR the "name" nsubj directly ("my sisters name" → my→name); the
            # possessor lookup below checks both, and a frame with NO possessor is dropped there.
            # Grammar (dep/pos/lemma) + cue-class gated, subject-agnostic, NO noun literal. The kin/
            # relational vocab lives in the DB cue classes (kinship_noun / relational_noun), overlay.
            if role is None:
                _kin_rel_nouns = _kinship_nouns() | _relational_nouns()
                role = next(
                    (c for c in tok.children
                     if c.dep_ in ("compound", "nmod") and c.pos_ in ("NOUN", "PROPN")
                     and (c.lemma_ or c.text or "").strip().lower() in _kin_rel_nouns),
                    None)
            if role is None:
                continue
            role_lemma = (role.lemma_ or role.text or "").strip().lower()
            # the PROPER NAME assigned — the copula's attr/attr-like complement (PROPN or a NOUN the
            # parser mis-tagged, e.g. "Carol" → NOUN). Exclude a wh/interrogative complement.
            proper = None
            for c in head.children:
                if c.dep_ in ("attr", "oprd", "dobj", "obj") and c.pos_ in ("PROPN", "NOUN"):
                    try:
                        if "Int" in c.morph.get("PronType") or c.tag_ in ("WP", "WP$", "WDT", "WRB"):
                            continue
                    except Exception:  # noqa: BLE001 — fail-safe
                        pass
                    proper = c
                    break
            if proper is None:
                continue
            proper_name = (proper.text or "").strip().lower()
            if not proper_name or proper_name == role_lemma:
                continue
            # the POSSESSOR of the role-noun: 1st-person poss pronoun → user; a PROPN → that person.
            # It attaches to the role noun ("my wifes name" → my→wifes) OR, in the apostrophe-stripped
            # compound frame, to the "name" nsubj itself ("my sisters name" → my→name) — check both.
            possessor = None
            poss_tok = next((c for c in role.children if c.dep_ == "poss"), None)
            if poss_tok is None:
                poss_tok = next((c for c in tok.children
                                 if c.dep_ == "poss" and c is not role), None)
            if poss_tok is not None:
                try:
                    _is_first = (poss_tok.morph.get("Person") == ["1"]
                                 and "Yes" in poss_tok.morph.get("Poss"))
                except Exception:  # noqa: BLE001
                    _is_first = False
                if _is_first:
                    possessor = "user"
                elif poss_tok.pos_ in ("NOUN", "PROPN"):
                    possessor = (poss_tok.text or poss_tok.lemma_ or "").strip().lower()
            if not possessor:
                continue
            # BIND the name (also_known_as) AND attach the kin role to the NAMED PERSON, collapsing the
            # role-noun. If the role-noun is kinship → its specific kin rel; otherwise generic
            # related_to (still attaches the person to the possessor, the walk resolves it).
            if role_lemma in _kinship_nouns():
                _kin = _inherent_relation_for_noun(role_lemma)
            else:
                _kin = "related_to"
            # (person, kin, possessor) — e.g. (carol, parent_of, user). subj_tok=proper so the named
            # person is the entity; verb_tok=None (no date on a name/role edge). The proper name BECOMES
            # the entity's surface here (the deriver works lowercased, so the alias is the subject
            # surface itself) — the EntityRegistry registers "carol" as the entity's also_known_as alias
            # when it grounds this edge at ingest. A separate (carol, also_known_as, carol) self-edge
            # would be degenerate (subj==obj, rejected by _emit) and is unnecessary: the NAME is filed
            # via the subject surface, never classified into L4 (THE HARD LINE preserved).
            _emit(proper_name, _kin, possessor, obj_tok=None, subj_tok=proper)
            # ROLE-ALIAS leg (Fix B, Part 2 — flag-gated): register the ROLE surface (mother/son/wife)
            # as an ``also_known_as`` alias of the NAMED person so a later SPLIT atom ("My mother is
            # 62" — the reframe Root-1 split) resolves mother→carol and the scalar lands on the named
            # person, not a parallel "mother" role entity. THE HARD LINE: the role is a slot/alias ON
            # the named instance. Only meaningful for a kinship/relational role (a generic possessor
            # like "John" is not a role-slot); we gate it on the role being in the relational/kinship
            # cue class so we never alias an arbitrary possessed noun. Subject-agnostic, metadata-driven.
            if SPINE_NAMING_CHAIN and (
                    role_lemma in _kinship_nouns() or role_lemma in _relational_nouns()):
                _emit(proper_name, "also_known_as", role_lemma, obj_tok=None, subj_tok=proper)
            # COLLAPSE the role-noun + the "name" anchor: claim them so the residue guard never flags
            # them as a dropped standalone entity (mother/son/wife is the RELATION, not a thing).
            _claim(role, tok)

    def _person_coref(tok):
        # Resolve a 3rd-person personal pronoun subject (she/he/they) with no in-clause antecedent to
        # the NEAREST PRECEDING proper-noun person in THIS doc, else the most-recent prior-turn NP.
        # Grammatical (PronType=Prs, Person=3), NOT a token list beyond the universal pronoun surface
        # set the deriver already uses for coref. Fail-safe: no antecedent → None (the caller skips).
        try:
            low = (tok.text or "").strip().lower()
            if low not in ("she", "he", "they", "her", "him", "them"):
                return None
            # nearest preceding named person in the doc: a PROPN, OR a NOUN bound as a name by a
            # "X's name is <Name>" copula (the attr complement — spaCy sometimes tags "Carol" NOUN).
            best = None
            for _t in doc:
                if _t.i >= tok.i:
                    break
                if _t.dep_ == "case":
                    continue
                if _t.pos_ == "PROPN":
                    best = (_t.text or "").strip().lower()
                    continue
                # a NOUN that is the attr of a naming copula ("name is Carol") is the bound person name
                if _t.pos_ == "NOUN" and _t.dep_ in ("attr", "oprd"):
                    _h = _t.head
                    if (_h is not None and _h.lemma_ == "be" and _h.pos_ == "AUX"
                            and any((_c.lemma_ or "").strip().lower() == "name"
                                    and _c.dep_ in ("nsubj", "nsubjpass") for _c in _h.children)):
                        best = (_t.text or "").strip().lower()
            if best:
                return best
            # TURN-LEVEL PERSON (atom-order-INDEPENDENT): no closer antecedent in THIS atom → resolve to
            # the turn's UNAMBIGUOUS person (exactly one distinct PERSON across the WHOLE turn, computed
            # upstream by PERSON NER and threaded in). This binds "…started working with HER" → "Rachel"
            # (introduced in a SIBLING atom) on EVERY run regardless of how the atomizer split / reordered
            # / lost atoms — the deterministic resolve of the 2c63a862 flake (the ``_prior`` accumulator
            # only carried "Rachel" when an earlier atom happened to emit it as an edge object first).
            # Type-agreeing by construction (a PERSON-NER pool + a 3rd-person PERSONAL pronoun) and
            # preferred over the untyped ``_prior`` NP fallback (which could bind a non-person NP). Two or
            # more distinct persons → ambiguous → fall through (never guess); zero → fall through.
            if len(_turn_persons) == 1:
                return _turn_persons[0]
            # else fall back to the most-recent cross-sentence antecedent (a bound name from a prior atom)
            for cand in reversed(_prior):
                c = str(cand).strip().lower()
                if c and c not in ("it", "they", "them", "she", "he"):
                    return c
        except Exception:  # noqa: BLE001
            return None
        return None

    def _chain_copula_measure(doc):
        # COPULA MEASUREMENT / SCALAR (Fix 3). "she is 62 years old", "Sarah is 28", "he is 6 feet
        # tall" → a SCALAR edge (subject, <scalar_rel>, value). The rel_type is metadata-driven via the
        # unit→rel_type map (unit_scalar cue class: year→age, foot→height, …); a BARE NUM with no unit
        # ("Sarah is 28") resolves to ``age`` (the default bare-number person scalar). The value is the
        # NUM surface as a STRING → routes to entity_attributes downstream (these rels carry
        # tail_types={SCALAR}). Subject-agnostic: any "X is N <unit>"; NO age token list, NO number
        # zoo — the NUM is detected grammatically (pos NUM / nummod) and the unit via the cue map.
        # Subject via the existing intra-turn coref ("she"→nearest named person; "Sarah"→the PROPN).
        _units = _unit_scalar_map()
        for tok in doc:
            if tok.dep_ not in ("nsubj", "nsubjpass"):
                continue
            head = tok.head
            if head is None or not (head.lemma_ == "be" and head.pos_ == "AUX"):
                continue
            # SUBJECT: a 1st-person self ("I am 34 years old") is the USER — resolve to "user" and let
            # the measurement detection below decide. (Previously this chain SKIPPED all 1st-person and
            # punted to the "self path", but that path reads the ADJ "old" as a FEELING and DROPPED the
            # age — first-person age fell through the crack.) A NON-measured 1st-person copula ("I am
            # happy") finds no NUM below and emits NOTHING, so the feeling/self path still owns it; the
            # feeling seam itself now steps aside for a MEASURED adjective (analyze_copula's
            # measured-adjective guard). Grammar-gated, subject-agnostic.
            _first_person_self = _is_first_person_personal_pronoun(tok)
            # ATTRIBUTE-SCALAR GUARD (Defect 1): a possessed-attribute literal ("my employee id is
            # 4471", "the laptop's serial is XR7-9920") is NOT a measured person — step aside so the
            # bare NUM is captured VERBATIM by _chain_attr_scalar, never mis-read as ``age``. Kinship
            # subjects ("my daughter is 28") are EXCLUDED from the binding, so their age still lands.
            if _attr_scalar_binding(tok) is not None:
                continue
            if _first_person_self:
                subject = "user"
            else:
                subject = (tok.text or tok.lemma_ or "").strip().lower()
                _cr = _person_coref(tok)
                if _cr:
                    subject = _cr
            if not subject:
                continue
            # Find a measurement: a NUM with a governing UNIT noun (year/foot/pound) anywhere under the
            # copula complement, OR a bare NUM complement (attr/acomp) with no unit → age.
            rel = None
            value = None
            num_tok = None
            # (a) UNIT-bearing: a unit noun (in the unit_scalar map) carrying a NUM nummod child.
            for t in doc:
                if t.pos_ != "NOUN":
                    continue
                _ul = (t.lemma_ or t.text or "").strip().lower()
                _ut = (t.text or "").strip().lower()
                _mapped = _units.get(_ul) or _units.get(_ut)
                if not _mapped:
                    continue
                # the unit noun must be grammatically tied to THIS copula's OWN clause. Climb to the
                # FIRST governing copula ``be`` AUX and require it to BE this head — so a conjoined
                # clause's unit ("…and she is 62 years old") binds to ITS copula, never the prior
                # clause's copula it merely conj-attaches up to. NOTE: spaCy Token identity must be
                # compared by ``.i`` — ``token is token`` is False across attribute accesses.
                _anc = t
                _hops = 0
                _under_be = False
                while _anc is not None and _hops < 8:
                    if _anc.i == head.i:
                        _under_be = True
                        break
                    # stop at the FIRST copula be we climb into that is NOT our head — the unit belongs
                    # to that other clause, not ours.
                    if (_anc.lemma_ or "").strip().lower() == "be" and _anc.pos_ == "AUX" \
                            and _anc.i != t.i:
                        break
                    if _anc.head.i == _anc.i:
                        break
                    _anc = _anc.head
                    _hops += 1
                if not _under_be:
                    continue
                _num = next((c for c in t.children if c.pos_ == "NUM" and c.dep_ == "nummod"), None)
                if _num is None:
                    continue
                rel = _mapped
                value = (_num.text or "").strip()
                num_tok = _num
                break
            # (b) BARE NUM person-scalar: a NUM directly as the copula complement (attr/acomp/attr-num)
            #     with no unit → age. "Sarah is 28".
            if rel is None:
                for c in head.children:
                    if c.pos_ == "NUM" and c.dep_ in ("attr", "acomp", "dobj", "obj"):
                        # PLAIN-CARDINAL SHAPE GATE (IP-as-age fix). POS alone CANNOT separate a
                        # bare age ("Sarah is 28") from a STRUCTURED numeric literal: UD tags
                        # formatted numerics (dates "11/11/1918", times "11:00" — and a dotted-quad
                        # IP "172.16.5.9", live-parsed NUM/CD dep=attr) as NUM exactly like a plain
                        # cardinal (UD NUM: universaldependencies.org/u/pos/NUM.html; spaCy glossary:
                        # attr = the copular predicate "attribute", NUM/CD = numeral/cardinal —
                        # github.com/explosion/spacy/blob/master/spacy/glossary.py). A bare-NUM *age*
                        # is a PLAIN INTEGER CARDINAL; any internal structure ('.', ':', '-', '/')
                        # marks a formatted literal (IP / version / MAC / range) owned by the
                        # atomic-scalar detector (has_ip/has_mac/… format-grammar) — NEVER an
                        # age/measure, in any domain. Deterministic value-shape check on the token
                        # surface, subject-agnostic; keep scanning for a genuine plain cardinal.
                        _num_surf = (c.text or "").strip()
                        if any(_ch in _num_surf for _ch in (".", ":", "-", "/")):
                            continue  # structured literal → the atomic lane owns it, never a scalar age
                        # YEAR-NOT-AGE GUARD (data-loss fix): a BARE 4-digit cardinal in calendar-year
                        # range ("diagnosed in 2019" reframed to a copula → "diagnosis is 2019") is a
                        # DATE/year owned by the temporal event_date lane, NEVER a person's age (a human
                        # age is at most 3 digits — 0–150). Reading 2019 as ``age`` mints a junk scalar
                        # and buries the year. Decline so the temporal layer captures it as event_date.
                        # Deterministic value-shape, subject-agnostic, no word list; a genuine 2-/3-digit
                        # age ("Sarah is 28", "the tree is 120") is untouched — keep scanning.
                        if len(_num_surf) == 4 and _num_surf.isdigit() and 1000 <= int(_num_surf) <= 2999:
                            continue
                        rel = "age"
                        value = _num_surf
                        num_tok = c
                        break
            if rel is None or not value:
                continue
            # ADDITIVE GUARD (Q8 over-reach fix): a measurement scalar (age/height/weight) is
            # Person-scoped (head_types={Person}). On a NON-admitted subject ("the tomatoes are 2-3
            # inches tall" → GLiNER2 type OBJECT) the WGM gate would QUARANTINE this scalar to Class C
            # (head_type_inconsistent) — a DEAD edge that also STEALS the crop's only walkable identity,
            # because claiming the subject + the copula-state measurement guard then suppress the viable
            # ``(tomatoes, has_state, tall)`` relational capture. So STEP ASIDE when the subject's
            # GLiNER2 type is not admitted by the scalar rel's head_types: emit NOTHING here and do NOT
            # claim the subject, leaving ``_chain_copula_state`` to capture the entity relationally
            # (it survives the gate, head_types={ANY}). The Person cases the family fix added (she/he/
            # Sarah — untyped pronoun or Person PROPN) are ADMITTED → unchanged. Metadata-driven
            # (overlay head_types), grammatical (GLiNER2 ent_type), subject-agnostic, NO word list.
            try:
                _subj_et = (tok.ent_type_ or "").strip()
            except Exception:  # noqa: BLE001 — fail-safe
                _subj_et = ""
            # PROPER-NOUN OVERRIDE (live-only bug, was silently dropping per-member ages): a PROPER
            # NOUN subject ("Mia is 10", "Theo is 12") is a NAMED entity whose bare-cardinal
            # copula complement is ITS OWN measurement. GLiNER2's type on a one-name micro-sentence is
            # NOISE — it mis-types "Mia"→Animal and "Theo"→Organization, so the additive guard
            # below would VETO their age (head_types={Person}) while admitting "Leo"→Person. The
            # guard's real job is the COMMON-noun over-reach ("the tomatoes are 2-3 inches tall" → leave
            # to has_state). A PROPN named subject is never that case, so trust the grammar, not the
            # noisy NER. Grammatical (PROPN), subject-agnostic, NO word list.
            if tok.pos_ != "PROPN" and not _scalar_rel_admits_subject(rel, _subj_et):
                continue  # common-noun, type-incompatible → let has_state capture it relationally
            # SCALAR emit: object is the STRING value; verb_tok=head so a date could bind (rare);
            # obj_tok=num_tok claims the number span. The rel carries tail_types={SCALAR} downstream so
            # the value lands in entity_attributes, never resolved to a UUID.
            _emit(subject, rel, value, verb_tok=head, obj_tok=num_tok, subj_tok=tok)
            _claim(tok)

    def _chain_date_attribute(doc):
        # DATE-VALUED SCALAR ATTRIBUTE — the emit half of the pre-pass ``_date_attr_binds``. A DATE is a
        # VALUE: for each detected "<owner>'s <noun> is <date>" / "my <noun> is <date>" / "<owner> has a
        # <noun> of <date>" construction, emit ONE dated SCALAR (owner, <possessed-noun>, date-value) —
        # so "Rex was born in 2020" and "Rex's birthday is March 3, 2020" both land a date value
        # on Rex, retrievably. The attribute/date tokens are already SUPPRESSED from every other
        # chain (``_date_attr_suppress`` at ``_emit``), so no month-name/day-num/attribute-noun junk. The
        # OWNER is resolved here (coref helpers are in scope now): first-person→user, PROPN→name,
        # 3rd-person→coref, else the possessed/subject noun. Subject-agnostic, ZERO word/attribute
        # literals, no fabricated dates (the pre-pass only enrolled date-layer-resolved complements).
        def _fmt(iso, gran):
            _i10 = iso[:10]
            if gran == "year":
                return _i10[:4], "string"
            if gran == "month":
                return _i10[:7], "string"
            return _i10, "date"

        def _owner_resolve(_tk):
            # the genitive/possessive/subject owner → (entity surface, token|None). ``my/our`` (Poss,
            # Person=1) and a 1st-person subject → user; a PROPN → the name; a 3rd-person pronoun →
            # coref; else the noun's own lemma/surface (a possessed common noun IS the entity).
            if _tk is None:
                return None, None
            try:
                _p1 = (_tk.morph.get("Person") == ["1"] and "Yes" in _tk.morph.get("Poss"))
            except Exception:  # noqa: BLE001
                _p1 = False
            if _is_first_person_personal_pronoun(_tk) or _p1:
                return "user", None
            if _tk.pos_ == "PROPN":
                return (_tk.text or "").strip().lower(), _tk
            if _tk.pos_ == "PRON":
                _cr = _coref(_tk) or _person_coref(_tk)
                if _cr:
                    return _cr, None
            return (_tk.lemma_ or _tk.text or "").strip().lower(), _tk

        def _attr_phrase(noun):
            _mods = [c for c in noun.children if c.dep_ in ("compound", "amod") and c.i < noun.i]
            _parts = [m.text for m in sorted(_mods, key=lambda m: m.i)] + [noun.text]
            return " ".join(p.strip() for p in _parts if p and p.strip()).lower()

        for _owner_tok, _attr_tok, _iso, _gran in _date_attr_binds:
            _owner, _otok = _owner_resolve(_owner_tok)
            _attr = _attr_phrase(_attr_tok)
            if not _owner or not _attr:
                continue
            _val, _dt = _fmt(_iso, _gran)
            # subj_tok=_otok only when it is NOT the suppressed attribute region (the owner never is);
            # obj_tok=None (the value is a scalar leaf, not an entity). distribute=False.
            _emit(_owner, _attr, _val, verb_tok=None, obj_tok=None,
                  subj_tok=_otok, scalar_datatype=_dt, distribute=False)

    def _chain_dash_specifier(doc):
        # DASH/COLON-INTRODUCED SPECIFYING LIST (gap-class: loose apposition that ENUMERATES a prior
        # generic event object). "I have been starting seeds … since Feb 20 - tomatoes, peppers, and
        # cucumbers …", "I bought tools — a hammer, a saw and a drill", "I'm learning languages: Python
        # and Rust". The dash/colon introduces a noun(-list) that SPECIFIES the generic object ("seeds"
        # / "tools" / "languages") of the preceding clause's event. spaCy mis-parses the dash boundary
        # (the list attaches as the subject of a trailing filler clause — "… - tomatoes … are doing
        # well"), so the seed-starting EVENT loses its real objects and the crops land on a useless
        # state ("tomatoes are doing"). We read the SURFACE specification structure instead of trusting
        # the cross-dash parse: distribute the preceding event over each specifier so the date-stamped
        # event lands on the actual things named. Subject-agnostic, structural — NO crop/produce/word
        # list; the dash + a coordinated NP list is a universal grammatical shape.
        _DASH_SURF = {"-", "–", "—", ":"}  # hyphen, en-dash, em-dash, colon (punctuation)
        for sep in doc:
            # A GENUINE clause/phrase-boundary separator is a PUNCT-POS token surrounded by whitespace.
            # An intra-word hyphen ("mid-February") has ``is_punct`` True at the LEXEME level but is
            # tagged NOUN/dep=pobj and carries NO surrounding whitespace — it must NOT open a specifier
            # list. Both signals (PUNCT pos + whitespace on both sides) are structural, subject-agnostic.
            if sep.pos_ != "PUNCT" or (sep.text or "").strip() not in _DASH_SURF:
                continue
            # The list must FOLLOW the separator with whitespace ("… basement - tomatoes",
            # "languages: Python"). An intra-word hyphen ("mid-February") is glued on BOTH sides
            # (no trailing whitespace), so this single check excludes it while admitting the colon
            # (which legitimately has no leading space). Structural, subject-agnostic.
            if sep.whitespace_ == "":
                continue  # glued separator (hyphenated compound) — not a boundary
            # SPECIFIER LIST: the first NOUN/PROPN content head AFTER the separator (its conjuncts form
            # the enumerated list). It must START the post-separator span (loose apposition opens with
            # the named thing), so we take the nearest following nominal head, skipping determiners.
            spec_head = None
            for t in doc[sep.i + 1:]:
                if t.is_space or t.is_punct:
                    continue
                if t.pos_ in ("NOUN", "PROPN"):
                    spec_head = t
                    break
                if t.pos_ in ("DET", "ADJ", "NUM"):
                    continue  # a leading "a"/"some"/"three" still opens the named list
                break  # a non-nominal opener (verb/adverb/…) → not a specifying list
            if spec_head is None:
                continue
            # PRECEDING EVENT: the nearest content verb BEFORE the separator that carries a subject and
            # a generic NOUN object (the thing the list specifies). We reuse the SVO machinery so the
            # subject/predicate/aspectual logic is identical to the backbone chain — the dash-specifier
            # only RE-TARGETS that event's object onto the named members.
            event_verb = subj_tok = generic_obj = None
            for v in reversed([t for t in doc[: sep.i] if t.pos_ == "VERB"]):
                lemma = (v.lemma_ or v.text or "").strip().lower()
                if not lemma or lemma == "be" or lemma in _naming_verbs():
                    continue
                _sj = next((c for c in v.children if c.dep_ in ("nsubj", "nsubjpass")), None)
                if _sj is None:
                    continue
                # the event must have a GENERIC NOUN object the list elaborates (e.g. "seeds"); a verb
                # with no nominal object, or whose object is the specifier itself, is not specified here.
                _ov = _svo_object_head(v)
                _xc = _aspectual_activity_xcomp(v)
                if _ov is None and _xc is not None:
                    _ov = _svo_object_head(_xc)  # "started growing seeds" → object on the xcomp
                if _ov is None:
                    # spaCy sometimes attaches the bare generic object as an ``xcomp`` NOUN rather than
                    # a dobj ("started seeds" → seeds is xcomp of started). Accept a direct nominal
                    # child the verb governs as the elaborated object. Structural, not a word list.
                    _ov = next(
                        (c for c in v.children
                         if c.pos_ in ("NOUN", "PROPN")
                         and c.dep_ in ("xcomp", "dobj", "obj", "attr", "oprd", "ccomp")),
                        None,
                    )
                if _ov is None:
                    # PAST-SIMPLE POS MIS-TAG: spaCy mis-parses the generic object of a past-simple
                    # event verb as a VERB ``xcomp`` ("I started seeds …" → ``seeds`` tagged
                    # pos=VERB, dep=xcomp of ``started``), so the NOUN/PROPN fallback above rejects it
                    # and the dash list never links. The progressive form ("I have been starting
                    # seeds") tags ``seeds`` NOUN/dobj and is unaffected. Recover STRUCTURALLY: the
                    # IMMEDIATE post-event-verb ``xcomp`` child is the (nominally-functioning)
                    # elaborated object even when mis-tagged VERB — gate it to a token that directly
                    # follows the event verb (the nominal-mis-tag signature) and is NOT itself the
                    # specifier head. No POS/word-list dependence; the progressive path (already
                    # resolved above) never reaches here, so it stays byte-identical.
                    _ov = next(
                        (c for c in v.children
                         if c.dep_ == "xcomp" and c.i > v.i and c.i < spec_head.i),
                        None,
                    )
                if _ov is None or _ov.i == spec_head.i:
                    continue
                event_verb, subj_tok = v, _sj
                generic_obj = _ov
                break
            if event_verb is None or subj_tok is None:
                continue
            # Descend an aspectual activity xcomp for the predicate (parity with _chain_svo).
            svo_head = event_verb
            _xc = _aspectual_activity_xcomp(event_verb)
            if _xc is not None:
                _xl = (_xc.lemma_ or _xc.text or "").strip().lower()
                if _xl and _xl != "be" and _xl not in _naming_verbs():
                    svo_head = _xc
            if any(c.dep_ == "neg" for c in event_verb.children) or (
                svo_head is not event_verb and any(c.dep_ == "neg" for c in svo_head.children)
            ):
                continue  # negated event — absence deferred (parity with _chain_svo)
            predicate = _svo_predicate_token(svo_head)
            if not predicate:
                continue
            if _is_first_person_personal_pronoun(subj_tok):
                subject = "user"
            else:
                subject = (subj_tok.text or subj_tok.lemma_ or "").strip().lower()
                _cr = _coref(subj_tok)
                if _cr:
                    subject = _cr
            if not subject:
                continue
            # DATE RE-TARGET: spaCy attaches the event's temporal PP ("since Feb 20") to the trailing
            # filler clause's verb (it crosses the dash with the list), so the date is bound to a verb
            # that is NOT our event verb. The dash-specification means that date belongs to THE EVENT.
            # If our event verb has no date bound but some OTHER verb at/after the separator does, lift
            # that date onto the event verb so _emit stamps the specifiers with it. Deterministic, reads
            # the same _date_by_verb the date layer already populated; never fabricates.
            if event_verb.i not in _date_by_verb and svo_head.i not in _date_by_verb:
                _lift = next(
                    ((vi, d) for vi, d in _date_by_verb.items() if vi >= sep.i), None
                ) or next(iter(_date_by_verb.items()), None)
                if _lift is not None:
                    _date_by_verb.setdefault(svo_head.i, _lift[1])
            # SPECIFIER NP builder: the head noun + its left compound/amod modifiers, EXCLUDING a date
            # token that spaCy mis-attached across the dash ("February 20th - tomatoes" leaves "20th"
            # as a compound of "tomatoes"). A modifier typed DATE/TIME, or a bare ordinal/number, is the
            # stray date boundary — never part of the named crop. Subject-agnostic, structural.
            def _spec_phrase(tok):
                mods = []
                for c in tok.children:
                    if c.dep_ not in ("compound", "amod") or c.i >= tok.i:
                        continue
                    try:
                        if (c.ent_type_ or "").upper() in ("DATE", "TIME"):
                            continue
                    except Exception:  # noqa: BLE001
                        pass
                    if c.like_num or (c.text or "").strip().lower().rstrip("stndrh").isdigit():
                        continue  # "20th"/"3rd"/numbers are date residue, not crop modifiers
                    mods.append(c)
                parts = [m.text for m in sorted(mods, key=lambda m: m.i)] + [tok.text]
                return " ".join(p.strip() for p in parts if p and p.strip()).lower()

            # INCHOATIVE DASH EVENT — emit so the harvest seam REIFIES it as a Class-A occurrence.
            # Q8 fix: a "started <generic> … - <crop list>" enumeration is an INGRESSIVE start, so each
            # crop is intrinsically a PER-OCCURRENCE event (the SAME shape the reified lane gives the
            # sibling "I started marigold seeds" turn → (user, participated_in, occurrence: marigold
            # seeds) Class A). The raw-verb (user, start, tomato) edge the seam would build is Class B,
            # so in the 122-turn haystack the Class-A operand (marigold) out-prioritizes the Class-B
            # operand (tomato) and the comparison recall drops "tomato". To put BOTH operands on the
            # SAME tier we hand the harvest seam an object string that COMBINES the crop with the
            # generic head ("tomato seeds") — a multi-token NAMED occurrence that reifies via
            # _occurrence_handle exactly like the marigold head — and keep the inchoative verb as the
            # rel_type so the seam recognizes the inchoative class (DB-grown _inchoative_verbs) and
            # reifies. A NON-inchoative dash event (buy/learn) is NOT combined and keeps its raw verb
            # (unchanged). Subject-agnostic: gated on the DB-grown inchoative cue class + the structural
            # dash boundary — NO crop/produce vocabulary. The combine uses the generic event object the
            # specifier elaborates; absent that, the bare member stands.
            _pred_lemma = (svo_head.lemma_ or svo_head.text or "").strip().lower()
            _is_inchoative_event = _pred_lemma in _inchoative_verbs()
            _generic_surface = ""
            if generic_obj is not None:
                _generic_surface = (generic_obj.lemma_ or generic_obj.text or "").strip().lower()
            # KINSHIP COLLECTIVE re-route: when the generic head is a kinship collective the pre-pass
            # flagged ("kids"/"children"/"sons" → child_of), each named member is bound to the head's
            # KINSHIP rel toward the possessor — (mia, child_of, user) — NOT (user, have, mia)
            # and NOT (user, owns, kids). Direction: the kin map gives the rel the HEAD plays toward the
            # possessor, so the member is the SUBJECT and the possessor the OBJECT. The head's intrinsic
            # gender (if any — neutral kin roles carry none) rides as a scalar on each member. Metadata-
            # driven (kinship cue maps), grammar-gated; the inchoative/event path below is skipped here.
            _kin_route = _kin_collective.get(generic_obj.i) if generic_obj is not None else None
            if _kin_route:
                _kin_rel, _kin_gender_val = _kin_route
                _claim(generic_obj)  # the collective head is consumed by the kinship route (not residue)
                for member in _np_conjuncts(spec_head):
                    _mp = _spec_phrase(member)
                    if not _mp or len(_mp) < 2 or _mp in ("it", "they", "them"):
                        continue
                    _emit(_mp, _kin_rel, subject,
                          verb_tok=svo_head, obj_tok=subj_tok, subj_tok=member)
                    if _kin_gender_val:
                        _emit(_mp, "has_gender", _kin_gender_val,
                              verb_tok=None, obj_tok=None, subj_tok=member)
                continue
            # DISTRIBUTE the event over each named specifier (its coordinated conjuncts).
            for member in _np_conjuncts(spec_head):
                obj_phrase = _spec_phrase(member)
                if not obj_phrase or len(obj_phrase) < 2:
                    continue
                if obj_phrase in ("it", "they", "them"):
                    _cr = _coref(member)
                    if _cr:
                        obj_phrase = _cr
                # For an INCHOATIVE start, combine the singular crop with the generic event object
                # ("tomatoes"+"seeds" → "tomato seeds") so the harvest seam reifies it as a NAMED
                # multi-token occurrence (matching the sibling marigold reification). The plural alias
                # rides via the morphological variant the seam re-derives from the singular. A bland
                # generic that already IS the member, or an empty generic, leaves obj_phrase bare.
                if _is_inchoative_event and _generic_surface:
                    _canon, _ = _morph_variants(obj_phrase)
                    _member_sing = _canon or obj_phrase
                    if _member_sing and _member_sing != _generic_surface \
                            and _generic_surface not in _member_sing.split():
                        obj_phrase = f"{_member_sing} {_generic_surface}"
                _emit(subject, predicate, obj_phrase,
                      verb_tok=svo_head, obj_tok=member, subj_tok=subj_tok)

    def _chain_named_instance(doc):
        # UNIFIED NAME↔TYPE BINDING (the ONE connector-agnostic named-instance chain). For EACH
        # (ProperName ↔ common-noun Type) binding the detector finds — across ALL connectors (naming
        # verb / apposition / copula) — emit the SAME canonical edge set, with the RELATION resolved
        # FROM THE TYPE'S CATEGORY via METADATA (the ONLY place domains differ):
        #   1. (name, instance_of, type)            — the name IS-AN-INSTANCE of its type (THE HARD LINE
        #                                              files the name AT the type place; never the
        #                                              reverse, never the name into L4).
        #   2. (name, also_known_as, <Name>)        — the proper name in the NAMING layer (user memory).
        #   3a. KINSHIP type → (name, <kin>, user)  — the specific kin rel from the kinship_noun cue map
        #       + (name, has_gender, <gender>)         + the gender from the kinship_gender cue map
        #                                              (son→child_of+male, daughter→child_of+female).
        #   3b. NON-kin self-possessed → (user, <poss>, name) — the possession rel that fits the type
        #                                              (has_pet for an animal, owns for an object) via
        #                                              the rel_types overlay (_possession_rel_for_type).
        #   3c. NON-kin, NOT self-possessed person/role → (name, has_role, type) — a generic role slot.
        #   4. age scalar (name, age, "19")         — the bare cardinal in the binding's span.
        #   5. nickname (name, also_known_as, Theo)  — a "goes by Theo" run.
        #   6. SUPPRESS the bare TYPE noun (son/daughter/dog/children) from becoming a standalone entity
        #      (claim it) once the named instance binds — the role is a CLASSIFICATION, not a thing.
        # Gated behind SPINE_NAMING_CHAIN (parity with the sibling naming chains); subject-agnostic,
        # kin/gender/possession all metadata-driven, grammatical. The proper name BECOMES the subject
        # surface (lowercased) so every edge hangs off the ONE instance entity the registry grounds.
        if not SPINE_NAMING_CHAIN:
            return
        try:
            _bindings = analyze_name_type_bindings(doc)
        except Exception:  # noqa: BLE001 — fail-safe
            _bindings = []
        _kin_set = _kinship_nouns()
        _gender_map = _kinship_gender_map()
        _social_map = _social_role_map()
        # Every PROPER NAME that is INDEPENDENTLY bound to its OWN type (the mixed "a son Alex, a
        # daughter Robin" enumeration gives each name its own binding) — used below to keep the
        # shared-role conj-distribution ROLE-BLEED-SAFE (a coordinated sibling that is another
        # binding's name keeps its own role; it is NEVER swept into this binding's role).
        _all_bound = {(x.name or "").strip().lower()
                      for x in (_bindings or []) if x and not x.negated}
        for b in (_bindings or []):
            if b is None or b.negated:
                continue
            name_key = (b.name or "").strip().lower()
            type_noun = (b.type_noun or "").strip().lower()
            type_head_raw = (type_noun.split()[-1] if type_noun else "")
            if not name_key or not type_noun or name_key == type_noun:
                continue
            # SINGULARIZE the type head for the SINGULAR-KEYED cue maps (kinship/gender/social) and for
            # the instance_of place: a PLURAL collective role ("My kids are …", "children named …") must
            # still resolve its metadata and file each name at the SINGULAR type ("kid"/"child"), never
            # the collective. Use the spaCy LEMMA of the type token (handles irregulars: children→child),
            # falling back to the surface. Morphological, NO plural/irregular word list.
            _ttok = next((t for t in doc if t.pos_ == "NOUN"
                          and (t.text or "").strip().lower() == type_head_raw), None)
            type_head = ((_ttok.lemma_ if _ttok is not None else "") or type_head_raw).strip().lower()
            _type_obj = type_head if (type_noun == type_head_raw and type_head) else type_noun

            # SHARED-ROLE COORDINATED NAMES (general conj-distribution, role-bleed-safe). "children named
            # Leo, Theo, and Mia" / "My kids are Leo, Theo, and Mia" bind ONE role over a
            # COORDINATED name list, but the detector returns a single binding (the first name). Walk the
            # coordination (``_np_conjuncts`` — the shared conj/PROPN-appos collector) and distribute the
            # SAME role to every coordinated sibling that is NOT independently bound to its own type — so
            # the mixed "a son Alex, a daughter Robin" case (each name has its own binding) never bleeds a
            # role across siblings. Each name is its OWN MEMORY entity; a NAME is never classified as a
            # type (THE HARD LINE — we replicate the role, we do not make Leo a type). Fail-safe.
            _emit_names = [name_key]
            try:
                _ptok = next((t for t in doc if t.pos_ == "PROPN"
                              and (t.text or "").strip().lower() == name_key), None)
                if _ptok is not None:
                    for _sib in _np_conjuncts(_ptok):
                        _ss = (_sib.text or "").strip().lower()
                        if _sib.i == _ptok.i or not _ss or _ss == type_noun:
                            continue
                        if _ss in _all_bound or _ss in _emit_names:
                            continue  # a sibling with its OWN binding keeps its own role (no bleed)
                        _emit_names.append(_ss)
            except Exception:  # noqa: BLE001 — fail-safe: distribution never sinks the primary bind
                _emit_names = [name_key]

            def _name_ner_for(nk):
                # Object-type tag for the instance: the GLiNER2 type seeded onto the proper-name token
                # (native), else None — passed to the possession resolver (NO GLiNER2 injection).
                try:
                    for _ent in getattr(doc, "ents", []) or []:
                        if (_ent.text or "").strip().lower() == nk and _ent.label_:
                            return _ent.label_.upper()
                except Exception:  # noqa: BLE001
                    pass
                return None

            def _type_ner_for():
                # GLiNER2 type on the TYPE-noun head (the AUTHORITATIVE L4 place), read from doc.ents —
                # NO GLiNER2 injection, just the native label already seeded. THE HARD LINE: the instance's
                # KIND is authoritatively its TYPE (server→Object, dog→Animal), NOT the name's surface-based
                # NER guess — GLiNER2 reads a "server named apollo" name as a Person, but the instance is an
                # OBJECT because its TYPE is "server". Keying the relation on the TYPE (the place) keeps a
                # non-animal object out of the person carve-out. Matches the ent whose text is the type head
                # or that contains the type-head token (surface or lemma). None if the type is untyped.
                try:
                    for _ent in getattr(doc, "ents", []) or []:
                        if not _ent.label_:
                            continue
                        if (_ent.text or "").strip().lower() in (type_head, type_head_raw):
                            return _ent.label_.upper()
                        for _tk in _ent:
                            _tkl = (_tk.lemma_ or "").strip().lower()
                            _tks = (_tk.text or "").strip().lower()
                            if _tkl == type_head or _tks == type_head_raw:
                                return _ent.label_.upper()
                except Exception:  # noqa: BLE001
                    pass
                return None

            def _emit_category(nk, nk_ner):
                # 3. the relation from the TYPE'S CATEGORY (the only place domains differ) — metadata.
                #    Resolution order (each layer is DB-grown metadata, NO domain literal in code):
                #      (a) KINSHIP type → the specific kin rel + the intrinsic gender (son→child_of+male).
                #      (b) SOCIAL person-role type → the social rel (friend→friend_of) — never ``owns``.
                #      (c) self-possessed NON-person type → the possession rel that fits the type
                #          (animal→has_pet, object→owns) via the rel_types overlay.
                #      (d) else → a self-subject activity verb (user, <verb>, name), if any.
                # The INSTANCE's authoritative entity-class is its TYPE (the place), not the name's
                # surface-based NER: prefer the TYPE-noun's GLiNER2 label, and only fall back to the
                # name's NER when the TYPE is untyped (an ungrown role like "my colleague Sam" where
                # GLiNER2 typed only the person name). This keeps a concretely-typed OBJECT ("server
                # named apollo") OUT of the person carve-out below even when the name reads as a Person.
                _inst_tag = _type_ner_for() or nk_ner
                _social_rel = _social_map.get(type_head)
                if type_head in _kin_set:
                    _kin = _inherent_relation_for_noun(type_head)
                    _emit(nk, _kin, "user", subj_tok=None, obj_tok=None)
                    _gender = _gender_map.get(type_head)
                    if _gender:
                        # has_gender carries tail_types={SCALAR} → routes to entity_attributes (STRING).
                        _emit(nk, "has_gender", _gender, subj_tok=None, obj_tok=None)
                elif _social_rel:
                    # a PERSON social role → the social tie to the speaker (friend_of / knows).
                    _emit(nk, _social_rel, "user", subj_tok=None, obj_tok=None)
                elif _inst_tag == "PERSON":
                    # CARVE-OUT FAIL-SAFE DEGRADE: a PERSON-typed named instance introduced by a common-
                    # noun role that is NEITHER kinship NOR an already-grown social role ("my colleague
                    # Sam"). Gated on the TYPE's class (or the name's NER only when the type is untyped),
                    # so a non-person object never lands here. The social_role class is GROWN per-tenant
                    # and is empty/missing here, so we DEGRADE to the generic walkable
                    # ``related_to(name, user)`` (a PERSON is NEVER ``owns``) — captured, NOT dropped —
                    # and QUEUE the role noun for freq-gated growth.
                    _emit(nk, "related_to", "user", subj_tok=None, obj_tok=None)
                    _record_cue_candidate(type_head, "social_role")
                elif b.possessor_is_self:
                    # self-possessed NON-person type → the possession rel that FITS THE TYPE (animal→
                    # has_pet, object→owns) via the rel_types overlay, keyed on the TYPE-authoritative
                    # instance class (falls back to owns).
                    _poss = _possession_rel_for_type(type_head, instance_type_tag=_inst_tag)
                    _emit("user", _poss, nk, subj_tok=None, obj_tok=None)
                else:
                    # NO stated possessor, but the named instance may be the OBJECT of a SELF-subject
                    # ACTIVITY verb — "We run a web server named Apollo" → (user, run, apollo). Locate the
                    # TYPE-noun token, climb to its governing content VERB; if that verb has a 1st-person
                    # subject and is not be/naming/possession-generic, mint (user, <verb-lemma>, name).
                    # Subject-agnostic, grammatical (the user's OWN verb), NO verb word list.
                    try:
                        _tn_tok = next(
                            (t for t in doc if t.pos_ in ("NOUN", "PROPN")
                             and (t.text or "").strip().lower() == type_head), None)
                        _gv = None
                        _cur = _tn_tok
                        _hops = 0
                        while _cur is not None and _hops < 6:
                            if _cur.pos_ == "VERB":
                                _gv = _cur
                                break
                            if _cur.head is None or _cur.head.i == _cur.i:
                                break
                            _cur = _cur.head
                            _hops += 1
                        if _gv is not None:
                            _gl = (_gv.lemma_ or _gv.text or "").strip().lower()
                            _gsubj = next((c for c in _gv.children
                                           if c.dep_ in ("nsubj", "nsubjpass")), None)
                            if (_gl and _gl != "be" and _gl not in _naming_verbs()
                                    and _gl not in _possession_verbs()
                                    and _gsubj is not None
                                    and _is_first_person_personal_pronoun(_gsubj)):
                                _emit("user", _gl, nk, subj_tok=None, obj_tok=None)
                    except Exception:  # noqa: BLE001 — fail-safe: activity membership is best-effort
                        pass

            # 1 + 3 for EVERY coordinated name: the name IS-AN-INSTANCE of its (singular) type place
            # (THE HARD LINE files the name AT the type place, never the name into L4) + the category rel.
            for nk in _emit_names:
                _emit(nk, "instance_of", _type_obj, subj_tok=None, obj_tok=None)
                _emit_category(nk, _name_ner_for(nk))
            # 2. the proper name in the naming layer (also_known_as) — PRIMARY name only. A self-edge
            #    (subj==obj) is rejected by _emit; the SURFACE-cased name is filed as the alias at ingest.
            if b.name and b.name.strip().lower() != name_key:
                _emit(name_key, "also_known_as", b.name.strip(), subj_tok=None, obj_tok=None)
            # 4. age scalar (the bare cardinal in the binding's span) — PRIMARY name only.
            if b.age:
                _emit(name_key, "age", b.age, subj_tok=None, obj_tok=None)
            # 5. nickname → a second also_known_as of the instance ("Jamie … goes by Jay") — PRIMARY only.
            if b.nickname and b.nickname.strip().lower() != name_key:
                _emit(name_key, "also_known_as", b.nickname.strip(), subj_tok=None, obj_tok=None)
            # 6. SUPPRESS the bare TYPE noun + CLAIM the PROPER NAME so neither leaks/false-flags. The
            #    type role is a CLASSIFICATION the named instance is filed at, not a thing (claim the
            #    type head token[s] by surface match — an enumeration repeats "son", claiming all is
            #    correct). The PROPER NAME tokens are CONSUMED by this binding (instance_of/alias/rel)
            #    so the residue guard must not log_crit them as uncovered content. The enclosing
            #    collective ("children") is claimed by _claim_named_instance_collectives.
            try:
                for _t in doc:
                    _tl = (_t.text or "").strip().lower()
                    # claim the type head by SURFACE or LEMMA (a plural collective "children" has
                    # surface != singular lemma "child") so the role never leaks as a bare entity.
                    if _t.pos_ == "NOUN" and _tl in (type_head, type_head_raw):
                        _claim(_t)
                    # claim EVERY bound name (primary + coordinated riders) so no name is residue.
                    if _t.pos_ in ("PROPN", "NOUN") and _tl in _emit_names:
                        _claim(_t)
                    # the nickname proper noun (also consumed by the alias edge)
                    if b.nickname and _t.pos_ in ("PROPN", "NOUN") and \
                            _tl == b.nickname.strip().lower():
                        _claim(_t)
            except Exception:  # noqa: BLE001
                pass

    def _claim_named_instance_collectives(doc):
        # Companion to _chain_named_instance: once at least one named instance has bound, SUPPRESS a
        # bare COLLECTIVE head ("children" in "We have three children together, a son …") that is the
        # dobj of a 1st-person ``have`` and whose appositive members are the bound named instances — so
        # the collective never leaks as a standalone entity. Structural (have + dobj + appos members),
        # NO collective-noun word-list. Only runs when SPINE_NAMING_CHAIN and a binding exists.
        if not SPINE_NAMING_CHAIN:
            return
        try:
            if not analyze_name_type_bindings(doc):
                return
        except Exception:  # noqa: BLE001
            return
        for tok in doc:
            if (tok.lemma_ or "").strip().lower() != "have" or tok.pos_ not in ("VERB", "AUX"):
                continue
            subj = next((s for s in tok.children if s.dep_ in ("nsubj", "nsubjpass")), None)
            if subj is None or not _is_first_person_personal_pronoun(subj):
                continue
            for c in tok.children:
                if c.dep_ in ("dobj", "obj") and c.pos_ == "NOUN":
                    # the collective is suppressed only if it heads an appositive named instance
                    # (its members are the bound names) — otherwise "I have a car" stays a real object.
                    if any(g.dep_ == "appos" and g.pos_ in ("PROPN", "NOUN") for g in c.children) or \
                       any(g.dep_ in ("appos", "conj") for g in c.children):
                        _claim(c)

    def _chain_appositive(doc):
        # APPOSITIVE → has_role. "Rachel, a real estate agent" → (rachel, has_role, real estate agent).
        # COMMON-noun role only (NOUN appos head); a PROPN appositive is an alias, not a role.
        # NAMED-INSTANCE GUARD (flag-gated): when the unified named-instance chain OWNS this clause (a
        # ProperName↔Type binding is present), the appositive role-noun ("son"/"daughter") is the TYPE
        # the named instance is filed at — NOT a standalone "X has_role son" fact. Skip an appos whose
        # HEAD is the bound PROPER NAME (the named-instance chain already minted instance_of/kin), so we
        # never double-capture the role as a generic has_role. Structural, subject-agnostic.
        _owned_name_keys = set()
        if SPINE_NAMING_CHAIN:
            try:
                for _b in (analyze_name_type_bindings(doc) or []):
                    if _b and not _b.negated and _b.name:
                        _owned_name_keys.add(_b.name.strip().lower())
            except Exception:  # noqa: BLE001
                _owned_name_keys = set()
        for tok in doc:
            if tok.dep_ != "appos":
                continue
            head = tok.head
            if head is None:
                continue
            if tok.pos_ != "NOUN":
                continue
            # the named-instance chain owns the bound TYPE nouns (son/daughter/dog) — an appos whose
            # HEAD or whose own token is a suppressed bound type is NOT a generic has_role fact
            # ("a son Alex, a daughter …" parses daughter as appos of son: both are bound types).
            if tok.i in _ni_suppress or head.i in _ni_suppress:
                continue
            if tok.i in _quantity_appos_suppress:
                continue  # "lisinopril 10 milligrams" — the quantity-of chain owns this measure appos
            role = _np_phrase(tok)
            named = (head.text or head.lemma_ or "").strip().lower()
            if not role or not named or role == named or len(role) < 2:
                continue
            # the named-instance chain owns "a son Alex" (Alex is the appos PROPN of son); here we
            # only reach a NOUN appos. If the HEAD is a bound proper name, the role is its type — skip.
            if named in _owned_name_keys:
                continue
            _emit(named, "has_role", role, obj_tok=tok, subj_tok=head)

    def _reconcile_collective_member_list(doc):
        # See COLLECTIVE_MEMBER_LIST. Reconcile a "<subj> <verb> [<count>] <HEAD>: M1, M2, …" named
        # enumeration AFTER the generic chains have run. The generic chains (SVO / classification /
        # named-instance) mangle this construction: they make the collective HEAD a type each member is
        # ``instance_of`` and the user ``owns``, only type the FIRST member, and never route the members
        # to their proper membership/kinship edge. Here we detect the construction structurally, route
        # by the HEAD noun's resolved cue/type, distribute to EVERY member, and drop the junk. The HEAD
        # is the dobj/obj of a non-copula verb; the members are the appos PROPN + its coordinated PROPN
        # conjuncts. Kinship HEAD (cue map) → (member, kin_rel, possessor) + intrinsic gender, NO type;
        # non-kin group HEAD → (member, instance_of, SINGULAR head) + the membership/activity relation
        # (a non-self group governor → member_of the governor; a self governor → its own verb to each
        # member, e.g. "we run servers" → (user, run, <server>)). Subject-agnostic, cue/morphology/
        # parse-driven, NO collective-noun word list. Fail-safe: any error → today's chain output.
        if not COLLECTIVE_MEMBER_LIST:
            return
        try:
            _kin_map = _kinship_rel_map()
            _gen_map = _kinship_gender_map()
        except Exception:  # noqa: BLE001 — fail-safe
            _kin_map, _gen_map = {}, {}
        try:
            _poss_verbs = _possession_verbs()
        except Exception:  # noqa: BLE001 — fail-safe
            _poss_verbs = frozenset()
        for _v in doc:
            if _v.pos_ not in ("VERB", "AUX"):
                continue
            _vl = (_v.lemma_ or _v.text or "").strip().lower()
            if not _vl or _vl == "be":
                continue
            _subj = next((c for c in _v.children if c.dep_ in ("nsubj", "nsubjpass")), None)
            if _subj is None:
                continue
            for _head in _v.children:
                if _head.dep_ not in ("dobj", "obj") or _head.pos_ != "NOUN":
                    continue
                # the HEAD must INTRODUCE a NAMED LIST: an appos PROPN member, and the HEAD is a genuine
                # collective (plural morphology, a numeric modifier, OR more than one named member).
                _m0 = next((g for g in _head.children
                            if g.dep_ == "appos" and g.pos_ == "PROPN"), None)
                if _m0 is None:
                    continue
                _members_toks = [t for t in _np_conjuncts(_m0) if t.pos_ == "PROPN"]
                _members: list = []
                for _mt in _members_toks:
                    _ms = (_mt.text or "").strip().lower()
                    if _ms and _ms not in _members:
                        _members.append(_ms)
                try:
                    _is_plural = "Plur" in _head.morph.get("Number")
                except Exception:  # noqa: BLE001
                    _is_plural = False
                _has_count = any(c.pos_ == "NUM" and c.dep_ == "nummod" for c in _head.children)
                if not _members or not (_is_plural or _has_count or len(_members) > 1):
                    continue
                _head_lemma = (_head.lemma_ or _head.text or "").strip().lower()
                _head_surface = (_head.text or "").strip().lower()
                try:
                    _head_sing, _ = _morph_variants(_head_surface)
                except Exception:  # noqa: BLE001
                    _head_sing = _head_lemma
                _head_sing = _head_sing or _head_lemma
                _head_variants = {x for x in (_head_lemma, _head_surface, _head_sing) if x}
                _kin_rel = _kin_map.get(_head_lemma) or _kin_map.get(_head_sing)
                _gender = _gen_map.get(_head_lemma) or _gen_map.get(_head_sing)
                _subj_is_self = _is_first_person_personal_pronoun(_subj)
                _subj_surface = "user" if _subj_is_self else \
                    (_subj.text or _subj.lemma_ or "").strip().lower()
                _member_set = set(_members)
                # The FULL enumerated region: every NOUN/PROPN in the appos/conj subtree under the head
                # / first member, INCLUDING common-noun specifiers ("a designer named Priya" contributes
                # "designer"). The dash-specifier RECOVERY pass distributes the raw colon list over ALL
                # of these, minting (governor, owns/have, <specifier>) junk we must drop alongside the
                # member possession junk. Structural walk, NO word list.
                _region_surfaces = set(_member_set) | _head_variants
                try:
                    _frontier = [_m0, _head]
                    _seen_i = {_m0.i, _head.i}
                    while _frontier:
                        _nx = []
                        for _t in _frontier:
                            for _c in _t.children:
                                if _c.i in _seen_i or _c.pos_ not in ("NOUN", "PROPN"):
                                    continue
                                if _c.dep_ in ("appos", "conj", "compound", "nmod"):
                                    _seen_i.add(_c.i)
                                    _cs = (_c.text or "").strip().lower()
                                    if _cs:
                                        _region_surfaces.add(_cs)
                                    _nx.append(_c)
                        _frontier = _nx
                except Exception:  # noqa: BLE001 — fail-safe: fall back to members+head only
                    _region_surfaces = set(_member_set) | _head_variants
                _governors = {_subj_surface, "user"}

                # ── DROP the junk the generic chains / dash-recovery emitted for THIS construction ──
                def _is_junk(_f, _hv=_head_variants, _ms_set=_member_set, _vlemma=_vl,
                             _region=_region_surfaces, _govs=_governors):
                    _s = (_f.subject or "").strip().lower()
                    _r = (_f.rel_type or "").strip().lower()
                    _o = (_f.object or "").strip().lower()
                    # a member is ``instance_of`` the COLLECTIVE head (plural class) — never its real type
                    if _r == "instance_of" and _o in _hv and _s in _ms_set:
                        return True
                    # subject owns/has a MEMBER (possession junk: "user owns mia"/"team have sarah")
                    if _r in ("owns", "own", "have", "has") and _o in _ms_set:
                        return True
                    # the GOVERNOR (the construction's subject, or the user) "owns/has" anything in the
                    # enumerated region — the group does not OWN its listed members/specifiers; they are
                    # member_of it ("team owns designer"/"team have sarah" → dropped; "user owns team",
                    # subject=user object NOT in region, is KEPT).
                    if _r in ("owns", "own", "have", "has") and _s in _govs and _o in _region:
                        return True
                    # subject → (possession/verb) → the collective HEAD itself ("user have kids")
                    if _r in ("owns", "own", "have", "has", _vlemma) and _o in _hv:
                        return True
                    # the GOVERNING GROUP itself is not "owned": once a non-self collective ("my team")
                    # governs a routed member list, the possessive's (user, owns, team) edge is
                    # redundant noise — the members are ``member_of`` the group, not the group a
                    # possession. Drop a (governor, owns/has, <the governing group>) edge. Subject-
                    # agnostic; only fires for a NON-self governing collective inside this chain.
                    if (_r in ("owns", "own", "have", "has")
                            and not _subj_is_self and _subj_surface
                            and _subj_surface not in ("user", "")
                            and _o == _subj_surface):
                        return True
                    # MEMBER ↔ MEMBER spurious edge: the members of an enumeration are distinct PEERS,
                    # so any edge BETWEEN two of them is a mis-link — e.g. the geo-containment chain
                    # reads "three cities: Paris, Tokyo, Cairo" as Paris located_in Tokyo. Drop it.
                    if _s in _ms_set and _o in _ms_set:
                        return True
                    return False
                _kept = [_f for _f in out if not _is_junk(_f)]
                if len(_kept) != len(out):
                    for _f in out:
                        if _is_junk(_f):
                            seen.discard((_f.subject, _f.rel_type, (_f.object or "").strip().lower()))
                    out[:] = _kept

                # members that ALREADY carry a self→member edge (the named-instance chain links the
                # FIRST member's possession, e.g. (user, has_pet, rex)) — do NOT re-emit a conflicting
                # bland possession for them; only the tail members need the distributed edge.
                _self_keys = {"user"} if _subj_is_self else {_subj_surface}
                _already_linked = {
                    (_f.object or "").strip().lower() for _f in out
                    if (_f.subject or "").strip().lower() in _self_keys
                    and (_f.object or "").strip().lower() in _member_set
                }
                # ── ADD the correct member edges (dedup via _emit/seen) ──
                for _m in _members:
                    if _kin_rel:
                        # KINSHIP collective: member kin-relates to the possessor; NO collective type.
                        _emit(_m, _kin_rel, ("user" if _subj_is_self else _subj_surface))
                        if _gender:
                            _emit(_m, "has_gender", _gender)
                    else:
                        # GROUP collective: type each member by the SINGULAR head + a membership edge.
                        if _head_sing and _head_sing != _m:
                            _emit(_m, "instance_of", _head_sing)
                        if _m in _already_linked:
                            # the named-instance chain already linked this member to the governor
                            # (e.g. (user, has_pet, rex)) — don't re-emit a conflicting bland edge.
                            continue
                        if not _subj_is_self and _subj_surface and _subj_surface != _m:
                            # a non-self group governs the members → (member, member_of, group)
                            _emit(_m, "member_of", _subj_surface)
                        elif _subj_is_self:
                            # self governs the members. A POSSESSION verb ("we have two dogs: Rex,
                            # Bella") routes to the TYPE-appropriate possession rel (has_pet for an
                            # animal head, owns for an object) — metadata-driven, NOT the bland "have".
                            # An ACTIVITY verb ("we run servers", "I grow plants") keeps the user's OWN
                            # verb. Subject-agnostic, NO verb/type word list.
                            if _vl == "have" or _vl in _poss_verbs:
                                try:
                                    _het = (_head.ent_type_ or "").strip() or None
                                except Exception:  # noqa: BLE001
                                    _het = None
                                _prel = _possession_rel_for_type(_head_sing, instance_type_tag=_het)
                                _emit("user", _prel, _m)
                            else:
                                _emit("user", _vl, _m)
                try:
                    _claim(_head, _m0, *_members_toks)
                except Exception:  # noqa: BLE001
                    pass

    def _reconcile_named_role_collective(doc):
        # COMMA-ROLE COLLECTIVE (GAP-2): "<group-subj> <have> a <role> named <Name>, a <role> named
        # <Name>, and a <role> named <Name>". Unlike the colon form ("… has these members: a violinist
        # named Mira, …") this has NO "members:" head and NO shared appos head, so split_enumeration
        # returns None and the whole sentence reaches the deriver. The named members get
        # instance_of <role> (analyze_naming) but NO membership edge — so the group floats via owns and
        # the grouping auto-mint (keyed on member_of) never fires → the group is not walkable.
        #
        # Here we emit (member, member_of, <group>) for each NAMED member when a NON-SELF COMMON-NOUN
        # subject governs a LIST (>=2) of "<role> named <PROPN>" bindings — exact PARITY with
        # _reconcile_collective_member_list's GROUP branch (which owns the colon/"members:" form). The
        # group is the governing subject's surface (the head noun "band"). Members are located
        # STRUCTURALLY: a naming-verb (DB-grown naming_verb cue ∪ code-fallback) attached as ``acl`` to a
        # role NOUN that sits in the verb's OBJECT region (never the subject), binding a PROPN name.
        #
        # GATES (deterministic, NO role/group/domain word list):
        #   • subject is a COMMON NOUN (a collective that can HAVE members — "my band"/"the team"); a
        #     1st-person / PROPN / pronoun subject is excluded (a person does not have "members").
        #   • >=2 named members → a genuine membership LIST (a single "a dog named Rex" is not a roster;
        #     this also bounds the possession/pet false-positive the colon form shares for a non-self
        #     possessor — an honest, parity-level residual, NOT a new error class).
        # _emit dedups via ``seen`` so a colon form that ALSO matches here lands the identical edges.
        # Fail-safe: any error → today's chain output (no member_of), never lose other capture.
        if not COLLECTIVE_MEMBER_LIST:
            return
        try:
            _naming = _naming_verbs()
        except Exception:  # noqa: BLE001 — fail-safe
            _naming = _NAMING_VERB_LEMMAS

        def _role_in_object_region(role_tok, verb_tok, subj_tok):
            # True iff role_tok reaches verb_tok by climbing heads WITHOUT crossing the subject token
            # (i.e. it lives in the verb's object/predicate region, not the subject NP). Cycle/hop-bounded.
            _cur = role_tok
            _hops = 0
            while _cur is not None and _hops < 12:
                if _cur.i == subj_tok.i:
                    return False
                if _cur.i == verb_tok.i:
                    return True
                if _cur.head is None or _cur.head.i == _cur.i:
                    return False
                _cur = _cur.head
                _hops += 1
            return False

        for _v in doc:
            if _v.pos_ not in ("VERB", "AUX"):
                continue
            _vl = (_v.lemma_ or _v.text or "").strip().lower()
            if not _vl or _vl == "be" or _vl in _naming:
                continue
            _subj = next((c for c in _v.children if c.dep_ in ("nsubj", "nsubjpass")), None)
            if _subj is None:
                continue
            # a collective that HAS members is a COMMON NOUN; self / proper-name / pronoun subjects are not.
            if _is_first_person_personal_pronoun(_subj) or _subj.pos_ != "NOUN":
                continue
            _group = (_subj.text or _subj.lemma_ or "").strip().lower()
            if not _group:
                continue
            _members: list = []
            for _nv in doc:
                if _nv.dep_ != "acl":
                    continue
                if (_nv.lemma_ or _nv.text or "").strip().lower() not in _naming:
                    continue
                _role = _nv.head
                if _role is None or _role.pos_ != "NOUN":
                    continue
                if not _role_in_object_region(_role, _v, _subj):
                    continue
                _name = next((c for c in _nv.children
                              if c.dep_ in ("oprd", "dobj", "obj", "attr") and c.pos_ == "PROPN"), None)
                if _name is None:
                    continue
                _ms = (_name.text or "").strip().lower()
                if _ms and _ms != _group and _ms not in _members:
                    _members.append(_ms)
            if len(_members) < 2:
                continue  # not a roster (single named instance is not a membership list)
            for _m in _members:
                _emit(_m, "member_of", _group)

    def _measure_pp_subject_tok(prep):
        # The SUBJECT entity a measure/adjunct PP predicates about: climb the PP's head chain to the
        # governing clause and return its subject token (carrying it by coreference for a subordinate
        # governing verb). A copula-attr NOUN host resolves to the copula's subject. Fail-safe → None.
        try:
            cur = prep.head
            hops = 0
            while cur is not None and hops < 8:
                if cur.pos_ in ("VERB", "AUX"):
                    _s = next((c for c in cur.children if c.dep_ in ("nsubj", "nsubjpass")), None)
                    if _s is not None:
                        return _s
                    if cur.pos_ == "VERB":
                        _cs = _carried_subject_token(cur)
                        if _cs is not None:
                            return _cs
                if cur.pos_ in ("NOUN", "PROPN"):
                    if cur.dep_ in ("attr", "oprd") and cur.head is not None \
                            and (cur.head.lemma_ or "").strip().lower() == "be":
                        _s = next((c for c in cur.head.children
                                   if c.dep_ in ("nsubj", "nsubjpass")), None)
                        if _s is not None:
                            return _s
                    elif cur.dep_ in ("nsubj", "nsubjpass", "appos"):
                        return cur.head if cur.dep_ == "appos" else cur
                nxt = cur.head
                if nxt is None or nxt.i == cur.i:
                    break
                cur = nxt
                hops += 1
        except Exception:  # noqa: BLE001 — fail-safe
            return None
        return None

    def _chain_has_measure(doc):
        # HAS-A-MEASURE SCALAR — emits off the ``_has_measure_binds`` pre-pass ("has/have a <measure> of
        # <value>"). attribute = the measure dobj head-noun phrase ("cvss base score"/"temperature"),
        # value = the of-object value span (digit-gated), routed to the SCALAR path (scalar_datatype=
        # "string") — NEVER a relationship object. Subject via the shared coref (a pronoun subject "it"
        # → the discourse topic through _emit's rebind). Subject-agnostic, grammar + value-shape.
        for _b in _has_measure_binds:
            subj_tok = _b["subj"]
            dobj = _b["dobj"]
            val_root = _b["val"]
            if _is_first_person_personal_pronoun(subj_tok):
                subject = "user"
            else:
                subject = (subj_tok.text or subj_tok.lemma_ or "").strip().lower()
                _cr = _coref(subj_tok)
                if _cr:
                    subject = _cr
            if not subject:
                continue
            attribute = _np_phrase(dobj)
            if not attribute:
                continue
            try:
                _sub = sorted(val_root.subtree, key=lambda t: t.i)
                _sub = [t for t in _sub if not t.is_punct or t.i == val_root.i]
                value = (sentence[min(t.idx for t in _sub):
                                  max(t.idx + len(t.text) for t in _sub)] or "").strip()
            except Exception:  # noqa: BLE001
                value = (val_root.text or "").strip()
            if not value:
                continue
            rel = attribute.replace(" ", "_")
            _emit(subject, rel, value, subj_tok=subj_tok, obj_tok=None, scalar_datatype="string")
            try:
                for _d in dobj.subtree:
                    _claim(_d)
                _claim(_b["verb"])
            except Exception:  # noqa: BLE001
                _claim(dobj)

    def _chain_measure_pp(doc):
        # SCALAR MEASURE PP (dense decomposition) — "<clause> with a CVSS score of 9.8", "at a
        # temperature of 39C", "for a term of 20 years". A prepositional phrase whose pobj NOUN carries
        # an "of"-PP (or nummod) with a DIGIT-bearing value is a SCALAR MEASUREMENT of the governing
        # clause's SUBJECT: the attribute name is the pobj head-noun phrase ("cvss score"/"temperature"/
        # "term"), the value is the numeric object ("9.8"/"39C"/"20 years"), routed to the SCALAR path
        # (scalar_datatype="string") — NEVER a relationship object. Subject-agnostic, grammar +
        # value-shape (a digit gates it), NO attribute/unit word list. The subject is carried by
        # coreference to the governing clause (a subordinate governing verb → the shared subject).
        for prep in doc:
            if prep.dep_ != "prep" or prep.pos_ != "ADP":
                continue
            pobj = next((c for c in prep.children
                         if c.dep_ == "pobj" and c.pos_ in ("NOUN", "PROPN")), None)
            if pobj is None:
                continue
            # a temporal pobj ("in June") is not a measurement — leave it to the temporal lane.
            try:
                if (pobj.ent_type_ or "").upper() in ("DATE", "TIME"):
                    continue
            except Exception:  # noqa: BLE001
                pass
            # VALUE: an "of"/quantity PP under the pobj whose object is (or carries a nummod) a DIGIT.
            val_root = None
            for c in pobj.children:
                if c.dep_ == "prep":
                    for gc in c.children:
                        if gc.dep_ != "pobj":
                            continue
                        if any(ch.isdigit() for ch in (gc.text or "")):
                            val_root = gc
                            break
                        if any(k.dep_ == "nummod" and any(ch.isdigit() for ch in (k.text or ""))
                               for k in gc.children):
                            val_root = gc
                            break
                if val_root is not None:
                    break
            if val_root is None:
                continue
            subj_tok = _measure_pp_subject_tok(prep)
            if subj_tok is None:
                continue
            if _is_first_person_personal_pronoun(subj_tok):
                subject = "user"
            else:
                subject = (subj_tok.text or subj_tok.lemma_ or "").strip().lower()
                _cr = _coref(subj_tok)
                if _cr:
                    subject = _cr
            if not subject:
                continue
            attribute = _np_phrase(pobj)
            if not attribute:
                continue
            # VALUE span = the value root's subtree, sliced verbatim ("9.8", "20 years").
            try:
                _sub = sorted(val_root.subtree, key=lambda t: t.i)
                _sub = [t for t in _sub if not t.is_punct or t.i == val_root.i]
                value = (sentence[min(t.idx for t in _sub):
                                  max(t.idx + len(t.text) for t in _sub)] or "").strip()
            except Exception:  # noqa: BLE001
                value = (val_root.text or "").strip()
            if not value:
                continue
            rel = attribute.replace(" ", "_")
            _emit(subject, rel, value, subj_tok=subj_tok, obj_tok=None, scalar_datatype="string")
            try:
                for _d in pobj.subtree:
                    _claim(_d)
            except Exception:  # noqa: BLE001
                _claim(pobj)

    def _chain_attributive_measure(doc):
        # ATTRIBUTIVE MEASURE (dense decomposition) — "a 62-year-old smoker", "a 6-foot-tall man": an
        # amod ADJ whose npadvmod is a UNIT noun (unit_scalar cue map) carrying a NUM nummod → a SCALAR
        # (unit→rel_type, value=NUM) on the entity the ADJ modifies. The described noun is an appositive
        # of the real subject ("a 62-year-old smoker" appos of "patient") → attach to that subject.
        # Metadata-driven (unit_scalar map: year→age, foot→height), grammatical, subject-agnostic, NO
        # age/unit word list. Reuses the SCALAR emit path (routes to entity_attributes).
        # ATTRIBUTIVE position only (amod/attr/nummod modifier of a noun); the PREDICATIVE "62 years
        # old" (acomp) is owned by _chain_copula_measure. The identifier tokenizer merges the phrase to
        # one token typed ADJ ("62-year-old") or NUM ("6-foot-tall"), so admit both POS.
        _units = _unit_scalar_map()
        for adj in doc:
            if adj.pos_ not in ("ADJ", "NUM") or adj.dep_ not in ("amod", "attr", "nummod"):
                continue
            mapped = None
            value = None
            num = None
            # (a) SPLIT form: "6 feet tall" → an npadvmod UNIT noun with a NUM nummod ("year"/"foot").
            unit = next((c for c in adj.children
                         if c.dep_ == "npadvmod" and c.pos_ == "NOUN"), None)
            if unit is not None:
                _ul = (unit.lemma_ or unit.text or "").strip().lower()
                _ut = (unit.text or "").strip().lower()
                mapped = _units.get(_ul) or _units.get(_ut)
                if mapped:
                    num = next((c for c in unit.children
                                if c.pos_ == "NUM" and c.dep_ == "nummod"), None)
                    if num is not None:
                        value = (num.text or "").strip()
            # (b) MERGED form: the identifier tokenizer keeps "62-year-old" / "6-foot-tall" WHOLE
            #     (one ADJ token). Parse the surface "<NUM><sep><unit>…" — the UNIT-map gate keeps it
            #     to genuine measurements (an id/version ADJ never matches). Subject-agnostic, no word
            #     list beyond the metadata unit map.
            if mapped is None:
                _m = re.match(r"^(\d+(?:\.\d+)?)[-\s]?([a-zA-Z]+)", (adj.text or "").strip())
                if _m:
                    _uw = _m.group(2).strip().lower()
                    _mapped2 = _units.get(_uw)
                    if _mapped2:
                        mapped = _mapped2
                        value = _m.group(1).strip()
            if not mapped or not value:
                continue
            described = adj.head
            if described is None or described.pos_ not in ("NOUN", "PROPN"):
                continue
            # the subject entity: an appositive describes its head; else the described noun (1st-person
            # → user). A copula complement resolves to the copula's subject.
            if described.dep_ == "appos" and described.head is not None:
                subj_tok = described.head
            elif described.dep_ in ("attr", "oprd") and described.head is not None \
                    and (described.head.lemma_ or "").strip().lower() == "be":
                subj_tok = next((c for c in described.head.children
                                 if c.dep_ in ("nsubj", "nsubjpass")), described)
            else:
                subj_tok = described
            if _is_first_person_personal_pronoun(subj_tok):
                subject = "user"
            else:
                subject = (subj_tok.text or subj_tok.lemma_ or "").strip().lower()
                _cr = _coref(subj_tok)
                if _cr:
                    subject = _cr
            if not subject:
                continue
            _emit(subject, mapped, value, subj_tok=subj_tok, obj_tok=num, scalar_datatype="string")
            _claim(adj, unit, num, described)

    def _chain_quantity_of(doc):
        # QUANTITY-OF-SUBSTANCE SCALAR — emits off the ``_quantity_binds`` pre-pass. The "<num> <unit>"
        # is a SCALAR VALUE (scalar_datatype="string" → entity_attributes), NEVER a relationship object;
        # in CONTENT mode the of-substance is ALSO grounded RELATIONALLY so it becomes its own L4 entity
        # (typed by GLiNER2, queued for the what-is/climb classification). Subject-agnostic, digit-gated.
        _units = _unit_scalar_map()

        def _resolve_subject(_tok):
            if _is_first_person_personal_pronoun(_tok):
                return "user"
            _s = (_tok.text or _tok.lemma_ or "").strip().lower()
            _cr = _coref(_tok)
            return _cr or _s

        def _attr_for_unit(_unit_tok):
            # the unit's unit_scalar cue-map value (grown per-tenant) if known, else generic "quantity".
            _ul = (_unit_tok.lemma_ or _unit_tok.text or "").strip().lower()
            _ut = (_unit_tok.text or "").strip().lower()
            return _units.get(_ul) or _units.get(_ut) or "quantity"

        for _b in _quantity_binds:
            _mode = _b["mode"]
            _value = _b["value"]
            if _mode == "possession":
                _subj_tok = _b["subj"]
                _subject = _resolve_subject(_subj_tok)
                _attr = _np_phrase(_b["attr_tok"])
                if not _subject or not _attr:
                    continue
                _emit(_subject, _attr.replace(" ", "_"), _value,
                      subj_tok=_subj_tok, obj_tok=None, scalar_datatype="string")
                _claim(_b["attr_tok"], _b["unit"], _b["num"], _b["verb"])
            elif _mode == "content":
                _subj_tok = _b["subj"]
                _subject = _resolve_subject(_subj_tok)
                _subst_tok = _b["substance"]
                _substance = _np_phrase(_subst_tok)
                if not _subject or not _substance:
                    continue
                # the SUBSTANCE is the REAL object of the verb — grounded as its own entity.
                _pred = (_b["verb"].lemma_ or _b["verb"].text or "").strip().lower()
                if _pred:
                    _emit(_subject, _pred, _substance, verb_tok=_b["verb"],
                          obj_tok=_subst_tok, subj_tok=_subj_tok)
                # the "<num> <unit>" dose/amount is a SCALAR on the SUBSTANCE.
                _emit(_substance, _attr_for_unit(_b["unit"]), _value,
                      subj_tok=_subst_tok, obj_tok=None, scalar_datatype="string")
                _claim(_b["unit"], _b["num"])
            elif _mode == "appos":
                _owner_tok = _b["owner_tok"]
                _owner = _resolve_subject(_owner_tok)
                if not _owner:
                    continue
                _emit(_owner, _attr_for_unit(_b["unit"]), _value,
                      subj_tok=_owner_tok, obj_tok=None, scalar_datatype="string")
                _claim(_b["unit"], _b["num"])

    # The chain COLLECTION — a data-driven set the loop iterates; NOT a priority ladder. Convergence
    # in ``_emit`` makes the result order-independent (see comment above), so this list expresses
    # "all the shapes the deriver knows", not "the order to try them in". Add a shape → add a chain.
    # ``dash_specifier_only`` (gap-class fix, cross-atom recovery): the live harvest atomizes a turn
    # BEFORE the deriver, which SEVERS a dash-introduced specifying list from its event clause ("…
    # starting seeds since Feb 20 - tomatoes, peppers, cucumbers" → atom 1 keeps only "seeds", the
    # crops become subjects of separate "… are doing well" atoms). The seed-starting EVENT then never
    # links to the named crops. So the harvest runs ONE extra deriver pass on the RAW un-atomized turn
    # with this flag → ONLY ``_chain_dash_specifier`` fires (no cross-clause smear from the other
    # chains — that is exactly why atomization exists), recovering the distributed event edges. Dedup
    # at the harvest seam drops any overlap with the atomized edges. Subject-agnostic, deterministic.
    _chains = (
        () if named_role_only else
        (_chain_dash_specifier,) if dash_specifier_only else
        (_chain_employment,
         _chain_alias_predicate,
         _chain_svo, _chain_intransitive, _chain_passive_event, _chain_copula_state,
         _chain_dated_occurrence,
         _chain_possessive, _chain_genitive_name, _chain_copula_name,
         _chain_copula_role_predicate,
         _chain_copula_measure, _chain_date_attribute, _chain_dash_specifier,
         _chain_possessed_typed_atomic,
         _chain_attr_scalar, _chain_quoted_value, _chain_classification_containment,
         _chain_has_measure, _chain_measure_pp, _chain_attributive_measure,
         _chain_quantity_of,
         _chain_geo_containment_list, _chain_residence_geo_bridge,
         _chain_named_instance, _claim_named_instance_collectives, _chain_appositive)
    )

    try:
        for _chain in _chains:
            try:
                _chain(doc)
            except Exception as _ce:  # noqa: BLE001 — one chain failing never sinks the others
                log.warning("linguistics.derive_chain_failed",
                            chain=getattr(_chain, "__name__", "?"), error=str(_ce)[:160])

        # COLLECTIVE MEMBER-LIST reconciliation (post-chain): a "<subj> <verb> <HEAD>: M1, M2, …" named
        # enumeration is fixed up here (drop collective-as-type/owns junk; distribute the right kin/
        # membership edge to EVERY member). Runs in BOTH modes: the dash_specifier_only RECOVERY pass
        # fires on the RAW un-atomized turn, so a named-member collective ("my team has three engineers:
        # Sarah, Tom, and a designer named Priya") would otherwise leak the un-reconciled
        # (team, owns, <member>) / collective-as-type junk from the dash chain. The reconciliation only
        # acts on a HEAD with appos PROPER-NOUN members, so the crop-seed recovery case (common-noun
        # specifiers — tomatoes/peppers) it is meant for is never touched. Fail-safe → chain output.
        if not named_role_only:
            try:
                _reconcile_collective_member_list(doc)
            except Exception as _rce:  # noqa: BLE001 — reconciliation never sinks capture
                log.warning("linguistics.collective_reconcile_failed", error=str(_rce)[:160])

        # COMMA-ROLE COLLECTIVE (gap-2): "<group> has a <role> named <Name>, …, and a <role> named
        # <Name>" — no colon "members:" head, so split_enumeration leaves it whole and the members get
        # instance_of but no membership edge. Distribute (member, member_of, <group>) for a non-self
        # common-noun group governing a >=2 named-role roster. _emit dedups. The live harvest LLM-
        # ATOMIZES a turn BEFORE the deriver, severing the roster into per-member atoms (each <2
        # members → no roster), so the harvest runs ONE extra pass on the RAW un-atomized turn with
        # ``named_role_only`` → ONLY this reconcile fires (no cross-clause smear; identical pattern to
        # ``dash_specifier_only``). Skipped under the dash-recovery pass. Fail-safe → no member_of.
        if not dash_specifier_only:
            try:
                _reconcile_named_role_collective(doc)
            except Exception as _nrce:  # noqa: BLE001 — reconciliation never sinks capture
                log.warning("linguistics.named_role_reconcile_failed", error=str(_nrce)[:160])

        # named_role_only RECOVERY pass: like dash_specifier_only, this fires ONE reconcile on the raw
        # turn to recover membership only — the rest of the turn's content is captured by the atomized
        # passes, so the residue guard below would false-alarm. Return the member_of edges now.
        if named_role_only:
            return out

        # ── RESIDUE GUARD (gap-2 §10.3) — fail loud on a silently-dropped content span ───────────
        # Every content-bearing NOUN/PROPN that no chain claimed is failure-residue. It is NOT dropped
        # here — the harvest returns the EMITTED edges to /ingest (which grows the ontology / async ±6
        # so the concept becomes typeable and lands in Postgres A/B), and only a span that genuinely
        # cannot be typed even after growth ends up in Class C via the existing store_context path.
        # The deriver's job is to make sure nothing content-bearing vanishes WITHOUT a trace: an
        # uncovered content head is log_crit'd (silent drop = bug, per the contract), surfacing it for
        # the growth path / investigation. Pronouns, determiners, copulas, pure function words and
        # dates are NOT content residue. Fail-safe: the guard never raises.
        # SKIP in dash_specifier_only mode: this pass intentionally fires ONE chain on the raw turn to
        # RECOVER the dash-specification only — the rest of the turn's content is captured by the
        # atomized passes, so flagging it here would be a false residue alarm.
        if dash_specifier_only:
            return out
        try:
            _uncovered = []
            for _t in doc:
                if _t.i in _covered:
                    continue
                if _t.pos_ not in ("NOUN", "PROPN"):
                    continue  # only nominal content heads are "things"; verbs ride their clause
                # a date token already routed to the temporal lane (event_date) is not lost content
                if _t.i in _date_token_idx:
                    continue  # PART 1: peeled date span — consumed by the temporal lane, not residue
                try:
                    if (_t.ent_type_ or "").upper() in ("DATE", "TIME"):
                        continue
                except Exception:  # noqa: BLE001
                    pass
                # a head that is only a compound/amod modifier of a covered head is covered-by-proxy
                if _t.dep_ in ("compound", "amod") and _t.head is not None and _t.head.i in _covered:
                    continue
                _uncovered.append((_t.i, _t.text))
            if _uncovered:
                # NOT a drop — these flow on as growth cues via the harvest→/ingest path; we log_crit
                # so a genuine silent loss (neither emitted, grown, nor held in C) is never invisible.
                from src.api.logging_config import log_crit  # deferred: leaf module, avoid cycle
                log_crit(
                    log,
                    "linguistics.derive_residue_uncovered",
                    sentence=(sentence or "")[:200],
                    uncovered=[w for _, w in _uncovered][:20],
                    emitted=len(out),
                    note=("content-bearing span(s) not claimed by any capture chain — flow to the "
                          "growth path (/ingest → grow ontology / async ±6); a span that cannot be "
                          "typed even after growth is held in Class C (store_context). Logged so a "
                          "genuine silent drop is never invisible (gap-2 §10.3)."),
                )
        except Exception as _rge:  # noqa: BLE001 — residue accounting never breaks capture
            log.debug("linguistics.derive_residue_guard_failed", error=str(_rge)[:160])
    except Exception as e:  # noqa: BLE001 — fail-safe: a derive miss is never a crash
        log.warning("linguistics.derive_sentence_facts_failed", error=str(e)[:160])
        return out
    return out


def discourse_topic_from_doc(doc, facts=None):
    """Establish the DISCOURSE TOPIC of a turn from its FIRST sentence (deterministic, subject-agnostic).

    GROUNDING: a reduced Centering Theory model (Grosz/Joshi/Weinstein 1995) — most-salient entity by
    grammatical role (subject) as antecedent; locality guard per Hobbs (1978). Shell-noun anaphora per
    Schmid (2000). See DEV/DESIGN-ingest-hardening-grounding.md.

    The topic is the salient primary entity introduced early — operationalized as the FIRST sentence's
    ROOT-clause grammatical SUBJECT (a NAMED/definite NP, or ``user`` for a 1st-person subject). This
    anchor is threaded into ``derive_sentence_facts`` for the LATER sentences of the same turn so a
    subject pronoun ("it"/"they"/"he") or a definite type-NP ("the flaw"/"the vulnerability") that has
    no closer antecedent resolves BACK to it — consolidating the whole description onto one entity.

    ``doc`` is the sentence-1 spaCy ``Doc`` (a TYPED Doc carries GLiNER2 ``ent_type_`` → the topic's
    coarse type; a str is parsed with no types). ``facts`` are sentence-1's derived ``SentenceFact``s
    (source of the topic's ``instance_of``/``subclass_of`` TYPE nouns).

    ANTI-OVER-EAGER: returns ``None`` (→ NO cross-sentence rebinding) when there is no single clear
    subject, when the subject is COORDINATED (multiple competing topics → ambiguous), or the subject is
    itself a pronoun/undecidable. Subject-agnostic (grammar + GLiNER2 type only); fail-safe → ``None``."""
    try:
        if doc is None:
            return None
        if isinstance(doc, str):
            if not doc.strip():
                return None
            doc = _parse(doc)
            if doc is None:
                return None
        # ROOT-clause subject (a copula "X is a Y" attaches the subject under the "be" AUX / the attr).
        subj = None
        root = next((t for t in doc if t.dep_ == "ROOT"), None)
        if root is not None:
            subj = next((c for c in root.children if c.dep_ in ("nsubj", "nsubjpass")), None)
        if subj is None:
            subj = next((t for t in doc if t.dep_ in ("nsubj", "nsubjpass")), None)
        if subj is None:
            return None
        # AMBIGUITY GUARD: a coordinated subject ("the server and the database …") is MULTIPLE competing
        # topics — never pick one. Also bail if a DISTINCT second top-level clause subject exists.
        if any(c.dep_ == "conj" for c in subj.children):
            return None
        _subj_surface_of = lambda t: (_np_phrase(t) or (t.text or "").strip().lower())  # noqa: E731
        _distinct_subjects = {
            _subj_surface_of(t) for t in doc
            if t.dep_ in ("nsubj", "nsubjpass") and t.pos_ in ("NOUN", "PROPN")
        }
        # SURFACE + COARSE TYPE. A bare PRONOUN subject in sentence 1 is not a stable anchor → None.
        # Otherwise accept the subject surface as the topic — including an OOV IDENTIFIER the tokenizer
        # kept whole but mis-tagged (a CVE id / version / hostname surfaces as PUNCT/X/NUM, not PROPN);
        # excluding those would drop exactly the technical primary entity the topic is meant to be.
        _is_dem_pron = False
        try:
            _is_dem_pron = subj.pos_ == "PRON" and "Dem" in subj.morph.get("PronType")
        except Exception:  # noqa: BLE001
            _is_dem_pron = False
        if _is_first_person_personal_pronoun(subj):
            surface, gtype = "user", "PERSON"
        elif subj.pos_ == "PRON" or _is_third_person_pronoun(subj) or _is_dem_pron:
            return None
        else:
            surface = _subj_surface_of(subj)
            if not surface or not any(ch.isalnum() for ch in surface):
                return None  # punctuation-only / empty subject → not a real anchor
            try:
                gtype = (subj.ent_type_ or "").strip().upper() or None
            except Exception:  # noqa: BLE001
                gtype = None
        if not surface:
            return None
        if surface != "user" and len(_distinct_subjects) > 1 and surface in _distinct_subjects:
            # more than one distinct NAMED clause subject in sentence 1 → topic is ambiguous.
            return None
        # TYPE NOUNS — from sentence-1's classification facts + the copula complement off the parse.
        type_nouns: set = set()
        for f in (facts or []):
            try:
                if (f.subject or "").strip().lower() == surface and \
                        (f.rel_type or "").strip().lower() in ("instance_of", "subclass_of"):
                    o = (f.object or "").strip().lower()
                    if o:
                        type_nouns.add(o)
                        type_nouns.add(o.split()[-1])
            except Exception:  # noqa: BLE001
                continue
        try:
            for t in doc:
                if not ((t.lemma_ or "").strip().lower() == "be" and t.pos_ == "AUX"):
                    continue
                _s = next((c for c in t.children if c.dep_ in ("nsubj", "nsubjpass")), None)
                if _s is None or _subj_surface_of(_s) != surface:
                    continue
                _comp = next((c for c in t.children
                              if c.dep_ in ("attr", "oprd") and c.pos_ in ("NOUN", "PROPN")), None)
                if _comp is not None:
                    _cp = _np_phrase(_comp)
                    if _cp:
                        type_nouns.add(_cp)
                        type_nouns.add(_cp.split()[-1])
        except Exception:  # noqa: BLE001
            pass
        return DiscourseTopic(surface=surface, gliner_type=gtype,
                              type_nouns=frozenset(type_nouns))
    except Exception as e:  # noqa: BLE001 — fail-safe: undecidable → no topic (no rebinding)
        log.warning("linguistics.discourse_topic_failed", error=str(e)[:160])
        return None


def is_naming_predicate(predicate: str):
    """Return True iff ``predicate`` is the NAMING/dubbing verb class ("named"/"called").

    Used by the verb-lift to SKIP the naming connector (the naming seam owns it) so the
    over-stripped ``"nam"`` junk is never minted. Decided by lemma membership in the bounded
    naming-verb class — a grammatical primitive, NOT a domain word-list. Returns ``None`` when
    the layer is unavailable so the caller can fall back to its bespoke check (do NOT treat
    ``None`` as a verdict); empty predicate → False (nothing to skip)."""
    nlp = _get_nlp()
    if nlp is None:
        return None
    p = (predicate or "").replace("_", " ").strip()
    if not p:
        return False
    try:
        doc = nlp(p)
        content = [t for t in doc if not t.is_punct and not t.is_space]
        if not content:
            return False
        # The naming verb is the lone content head of a pure naming connector ("named",
        # "called", "is named"). True iff EVERY content token is either the naming verb or a
        # function word (aux/copula) around it — so "named"/"is named" match, but "named after"
        # (a different relation) does not collapse to the naming seam. The naming-verb set is the
        # per-tenant grown overlay set ∪ code-fallback (resolved once).
        _naming = _naming_verbs()
        for t in content:
            if (t.lemma_ or "").strip().lower() in _naming:
                continue
            if t.pos_ in _FUNCTION_POS:
                continue
            return False
        return any((t.lemma_ or "").strip().lower() in _naming for t in content)
    except Exception as e:  # noqa: BLE001 — fail-safe → caller's bespoke path
        log.warning("linguistics.is_naming_predicate_failed", error=str(e)[:160])
        return None


# ── POSSESSIVE HEAD — generalizes possessive_head.py ───────────────────────────────
def possessive_head(text: str, possessor: str):
    """Return the HEAD NOUN of the possessive phrase whose possessor token is ``possessor``.

    THE RULE (``poss`` dependency → head noun): when ``possessor`` appears in ``text`` as a token
    with dependency ``poss``, its syntactic HEAD is the thing possessed — the head noun phrase.
    "my car's GPS system" → the ``poss`` token "car" heads "system" → return "gps system"
    (the head plus its left ``compound`` modifiers, lowercased). This is the deterministic,
    parse-driven generalization of the bespoke string walk in ``possessive_head.py``.

    Returns the head noun-phrase string, or ``None`` when ``possessor`` is not a possessive in
    ``text``, when no distinct head survives, or on any failure (caller leaves the object as-is).
    """
    doc = _parse(text)
    if doc is None:
        return None
    target = (possessor or "").strip().lower()
    if not target:
        return None
    try:
        for tok in doc:
            if tok.dep_ != "poss":
                continue
            if (tok.lemma_ or "").strip().lower() != target and (tok.text or "").strip().lower() != target:
                continue
            head = tok.head
            # NESTED POSSESSIVE CHAIN: when the head is ITSELF a possessor ("joel's bike's tire"
            # → joel poss→ bike poss→ tire), the thing being described is the RIGHTMOST head, not
            # the immediate one. Chase the ``poss`` chain to its terminal head (bounded, structural;
            # matches the bespoke walk's rightmost-head rule). The visited-guard prevents a cycle.
            seen = {head.i}
            while head.dep_ == "poss" and head.head.i not in seen:
                head = head.head
                seen.add(head.i)
            # Build the head noun phrase: the head plus its left-side compound modifiers
            # ("GPS" compound→ "system" gives "gps system"). Subject-agnostic; structural only.
            parts = [c.text for c in head.lefts if c.dep_ == "compound"]
            parts.append(head.text)
            phrase = " ".join(p.strip().lower() for p in parts if p and p.strip())
            phrase = phrase.strip()
            if phrase and phrase != target:
                return phrase
    except Exception as e:  # noqa: BLE001 — fail-safe
        log.warning("linguistics.possessive_head_failed", error=str(e)[:160])
        return None
    return None


# ── FUNCTION-WORD PREDICATE TEST — generalizes relation_fit.py RUNG 1 ───────────────
def is_function_word_predicate(predicate: str):
    """Return True iff ``predicate`` is (entirely) GRAMMAR — a function word, not a relation.

    THE RULE (POS test): parse the predicate; if EVERY content-bearing token is a function-word
    POS (``{ADP, DET, PART, AUX, CCONJ, SCONJ}``), it carries no relational content ("but",
    "when", "near", "be", "the") → True (reject). A predicate containing any VERB/NOUN/ADJ/etc.
    is content → False (keep). This is the parse-driven generalization of the bespoke
    function-word string set in ``relation_fit.py`` RUNG 1.

    Returns ``None`` when the layer is unavailable so the caller can fall back to its bespoke set
    (do NOT treat ``None`` as a verdict). Empty/whitespace predicate → True (no content). A token
    underscore ("lives_in") is normalized to spaces so the multi-word predicate parses naturally.
    """
    nlp = _get_nlp()
    if nlp is None:
        return None  # layer unavailable → caller uses its bespoke function-word set
    p = (predicate or "").replace("_", " ").strip()
    if not p:
        return True  # empty predicate carries no content
    try:
        doc = nlp(p)
        content = [t for t in doc if not t.is_punct and not t.is_space]
        if not content:
            return True
        # True iff there is NO content-bearing (non-function-word) token.
        return all(t.pos_ in _FUNCTION_POS for t in content)
    except Exception as e:  # noqa: BLE001 — fail-safe → caller's bespoke path
        log.warning("linguistics.function_word_test_failed", error=str(e)[:160])
        return None


# ── CLAUSE / SENTENCE SEGMENTATION ─────────────────────────────────────────────────
def segment_clauses(text: str):
    """Return the sentence spans of ``text`` (``doc.sents``), each stripped, as a list of strings.

    The dependency-parser's sentence boundaries are more robust than a punctuation split for the
    ingest spine's clause decomposition. Returns ``[]`` when the layer is unavailable or on any
    failure → caller falls back to its existing sentence-split. Single-sentence input returns a
    one-element list.
    """
    doc = _parse(text)
    if doc is None:
        return []
    try:
        return [s.text.strip() for s in doc.sents if s.text and s.text.strip()]
    except Exception as e:  # noqa: BLE001 — fail-safe
        log.warning("linguistics.segment_clauses_failed", error=str(e)[:160])
        return []


def is_interrogative_clause(text: str):
    r"""Deterministic question-vs-statement test for ONE clause. Returns ``True`` (interrogative),
    ``False`` (declarative), or ``None`` (layer unavailable — caller keeps today's behavior).

    WHY THIS EXISTS (query-path pollution fix): the buried-fact harvest fires on EVERY recall
    turn (by design — to catch a real statement buried in a question, "…by the way I got a dog").
    But it OVER-extracted the question's OWN interrogative clause as if it were a buried statement
    — ``recall_memory("what did I attend after my graduation")`` wrote ``(user, attended,
    graduation)`` etc. Recall must stay READ-ONLY for the question's own content. This function
    lets the harvest SKIP a clause that is itself a question, while still harvesting a genuinely
    buried DECLARATIVE clause in the same turn.

    GRAMMATICAL (subject-agnostic, NO keyword/word-list) — three independent question signals,
    each a language primitive (the SAME wh-grammar the copula/possessive seams already guard on):
      1. **wh-question word** — a token with ``PronType=Int`` or a wh-tag (WP/WP$/WDT/WRB) that is
         NOT buried inside a relative clause (``relcl``/``acl``) of a declarative main clause. A
         relative "the place where I live" is declarative; a main-clause "where do I live" is not.
      2. **subject–auxiliary inversion** — a yes/no question ("did I attend…", "is it…"): the
         clause ROOT is an AUX (or has an AUX child) that PRECEDES its nominal subject.
      3. **trailing question mark** — the last non-space token of the clause is ``?``.

    Any one signal → interrogative (``True``). None present → declarative (``False``). A parse
    failure on an OTHERWISE-loadable layer biases to ``True`` (skip → do NOT pollute the query
    path), per the fix's fail-safe direction; only a fully-unavailable layer returns ``None`` so
    the caller can fall back to its existing harvest-all behavior (global kill-switch contract).
    """
    if not text or not text.strip():
        return False
    nlp = _get_nlp()
    if nlp is None:
        # Layer entirely unavailable (flag off / no spaCy / no model): caller decides. Returning
        # None preserves the existing harvest-all fallback rather than silently dropping facts.
        return None
    try:
        doc = nlp(text)
    except Exception as e:  # noqa: BLE001 — a clause parse failure must never crash the harvest
        log.warning("linguistics.is_interrogative_parse_failed", error=str(e)[:160])
        # Bias to NOT-polluting the query path: an unparseable clause on the recall path is
        # treated as a question and skipped, never confabulated into facts.
        return True
    try:
        # Signal 3 — trailing '?' (cheap, casing/structure-independent).
        for tok in reversed(doc):
            if tok.is_space:
                continue
            if tok.text == "?":
                return True
            break

        for tok in doc:
            # Signal 1 — a MAIN-CLAUSE wh question word. A wh token inside a relative clause is a
            # declarative modifier ("the city where I was born"), so require the wh token (or its
            # head chain) to NOT sit under a relcl/acl. Grammatical: PronType=Int / wh-tags.
            morph = tok.morph
            is_wh = ("Int" in morph.get("PronType")) or tok.tag_ in ("WP", "WP$", "WDT", "WRB")
            if is_wh:
                in_relative = False
                cur = tok
                hops = 0
                while cur is not None and cur.head is not cur and hops < 12:
                    # A wh under ANY embedded/subordinate clause is a declarative modifier, not a
                    # main-clause question: relative ("the school where I graduated" → relcl/acl/
                    # advcl, depending on spaCy's parse) and complement ("I know what I want" →
                    # ccomp/xcomp/pcomp/csubj). Only a wh in the MAIN clause is interrogative.
                    if cur.dep_ in ("relcl", "acl", "advcl", "ccomp", "xcomp",
                                    "pcomp", "csubj", "csubjpass"):
                        in_relative = True
                        break
                    cur = cur.head
                    hops += 1
                if not in_relative:
                    return True

            # Signal 2 — subject–auxiliary inversion (yes/no question): an AUX that PRECEDES its
            # own nominal subject. "did I attend" → AUX "did" (i) is left of nsubj "I". A
            # declarative "I did attend" has the subject to the LEFT of the aux → not inverted.
            if tok.pos_ == "AUX":
                subj = next(
                    (c for c in tok.children if c.dep_ in ("nsubj", "nsubjpass")),
                    None,
                )
                if subj is not None and tok.i < subj.i:
                    return True
                # AUX as a child of a ROOT verb ("did I attend" — "did" is aux of ROOT "attend"):
                # the inversion is the aux preceding the ROOT's subject.
                if tok.dep_ in ("aux", "auxpass"):
                    head = tok.head
                    hsubj = next(
                        (c for c in head.children if c.dep_ in ("nsubj", "nsubjpass")),
                        None,
                    )
                    if hsubj is not None and tok.i < hsubj.i:
                        return True

        return False
    except Exception as e:  # noqa: BLE001 — fail-safe: bias to skip on the recall path
        log.warning("linguistics.is_interrogative_failed", error=str(e)[:160])
        return True


def count_declarative_assertions(text: str):
    r"""Count the DECLARATIVE FACTUAL ASSERTIONS in ``text``. Returns an int, or ``None``
    when the grammar layer is unavailable (caller keeps its existing behaviour).

    WHY THIS EXISTS (intent-misroute fix): a multi-sentence first-person STATEMENT that opens
    with a courtesy/announcement clause ("I wanted to tell you about my family. My wife's name
    is Ada. We have three kids …") is mis-scored as QUERY by GLiNER2 — the "tell" verb drags
    it to the recall route and the whole turn is dropped at the QUERY gate. The grammar is
    unambiguous: the turn CONTAINS asserted facts. This counts them so the caller can route a
    fact-bearing declarative to STATEMENT/ingest BEFORE GLiNER2 (kept pure — no label edits).

    An ASSERTION sentence (subject-agnostic, NO keyword/word-list — pure dependency structure):
      • is NOT interrogative (``is_interrogative_clause`` is False), and
      • has an EXPLICIT nominal subject (nsubj/nsubjpass) — this excludes IMPERATIVES like
        "Tell me about my family" / "Show me my pets" (no overt subject), which is exactly the
        recall surface we must NOT capture, and
      • PREDICATES content one of two grammatical ways:
          (a) COPULA assignment — a ``be`` root (or a ``cop`` dependency) with a nominal/numeric/
              adjectival complement (attr/acomp/oprd): "my wife's name IS Ada", "she IS 28".
          (b) STATIVE/POSSESSIVE transitive — a content-verb root with a direct object (dobj/obj/
              dative/attr): "we HAVE three kids", "I OWN a dog".
      A retrieval-desire clause ("I want to know about my family", "I'd like to hear about X")
      does NOT qualify: its predicate is the desire verb with an xcomp/prep, never a copula-attr
      or a concrete dobj — so it scores 0 and stays for GLiNER2 (a real recall is preserved).

    Caller contract: route to STATEMENT when the return is >= 2 (a clear, multi-fact declarative
    that GLiNER2 would otherwise misroute) — single-assertion turns are left to GLiNER2 / the
    affect+copula seams so this never widens the STATEMENT surface for ordinary one-liners.
    Fail-safe: layer unavailable → None; any parse error → 0 (fall through to GLiNER2).
    """
    if not text or not text.strip():
        return 0
    nlp = _get_nlp()
    if nlp is None:
        return None
    try:
        doc = nlp(text)
    except Exception as e:  # noqa: BLE001 — never crash the intent path
        log.warning("linguistics.count_declarative_assertions_parse_failed", error=str(e)[:160])
        return 0
    try:
        _COP_COMPL = {"attr", "acomp", "oprd"}
        _OBJ_DEPS = {"dobj", "obj", "dative", "attr"}
        n = 0
        for sent in doc.sents:
            stext = sent.text.strip()
            if not stext:
                continue
            # A question clause is never an assertion (a real recall stays for GLiNER2).
            if is_interrogative_clause(stext) is True:
                continue
            # Find a finite ROOT (or any clause head) with an EXPLICIT nominal subject.
            asserted = False
            for tok in sent:
                if tok.dep_ not in ("nsubj", "nsubjpass"):
                    continue
                head = tok.head
                if head is None:
                    continue
                children = list(head.children)
                # (a) COPULA assignment: head is `be` with a nominal/adj complement, OR the head
                #     itself is the predicate nominal carrying a `cop` child (spaCy attaches the
                #     copula as a dependent of the complement in some parses).
                is_be_root = (head.lemma_ == "be") and head.pos_ in ("AUX", "VERB")
                has_cop_child = any(c.dep_ == "cop" for c in children)
                if is_be_root or has_cop_child:
                    if is_be_root:
                        if any(c.dep_ in _COP_COMPL for c in children):
                            asserted = True
                    else:  # head IS the complement (noun/adj) with a cop child
                        if head.pos_ in ("NOUN", "PROPN", "NUM", "ADJ"):
                            asserted = True
                # (b) STATIVE/POSSESSIVE transitive: a content-verb head with a direct object.
                if not asserted and head.pos_ == "VERB":
                    if any(c.dep_ in _OBJ_DEPS for c in children):
                        asserted = True
                if asserted:
                    break
            if asserted:
                n += 1
        return n
    except Exception as e:  # noqa: BLE001 — fail-safe: fall through to GLiNER2
        log.warning("linguistics.count_declarative_assertions_failed", error=str(e)[:160])
        return 0


# ── DIRECTIVE / CORRECTION GRAMMAR FIRST-CUT (negation is NOT GLiNER2's job) ────────
# GLiNER2 deliberately does NOT judge negation/correction; that decision is made on the
# dependency parse (NegEx tradition: cues + syntactic scope, not substring windows). This
# function returns PURE GRAMMAR FACTS about a possible directive (imperative / contrastive /
# discourse-marker structure). It makes NO routing decision and consults NO cue word-list —
# the CALLER (main.py intent gate) owns the curated cue inventories and the CORRECTION/
# RETRACTION verdict. That keeps this module's "grammar, not classification/ontology" contract:
# we hand the caller the structure; the caller's closed cue sets + the model corroborate it.
@dataclass(frozen=True)
class DirectiveAnalysis:
    """Deterministic grammatical structure of a possible correction/retraction directive.

    - ``imperative_root_lemma`` : lemma of a ROOT VERB used imperatively (no overt subject, or
      a 2nd-person "you" subject) — e.g. "forget"/"delete". ``None`` when the ROOT is not an
      imperative verb. (Caller checks membership in its retraction-cue set.)
    - ``imperative_root_negated`` : True when that imperative carries a ``neg`` child ("don't
      forget") — a NEGATED imperative is NOT a retraction; the caller must skip it.
    - ``root_verb_lemma`` / ``root_subject_is_self`` : the main ROOT verb lemma and whether its
      subject is the 1st-person speaker — for "I meant"/"I correct" repair detection.
    - ``clause_initial_markers`` : lemmas of sentence-initial discourse adverbials/interjections
      (ADV/INTJ at the sentence head) — e.g. {"actually"}. (Caller intersects its marker set.)
    - ``has_contrastive_negation`` : True when a ``neg`` attaches to an ``appos``/``conj`` token
      ("…Luna, not Bella") — a CONTRAST, distinct from plain predicate negation ("is not a
      problem", where ``neg`` hangs off the ROOT predicate → False).
    - ``cessation_advmod_lemmas`` : lemmas of TEMPORAL-CESSATION adverbials (``advmod``) hanging off
      the ROOT predicate WHILE the clause carries negation polarity — the grammatical shape of "no
      longer X"/"not X anymore"/"X no more" (a negated copular/possessive/verbal predicate scoped by
      a cessation adverbial). Pure dependency facts ({lemma of each ROOT-``advmod`` ADV when the ROOT
      predicate is negated}); the caller intersects its own bounded cessation-cue set to decide
      RETRACTION. Empty when there is no negated-predicate-with-adverbial shape.
    """
    imperative_root_lemma: str | None
    imperative_root_negated: bool
    root_verb_lemma: str | None
    root_subject_is_self: bool
    clause_initial_markers: frozenset[str]
    has_contrastive_negation: bool
    cessation_advmod_lemmas: frozenset[str]


def analyze_directive(text: str):
    """Deterministic grammar first-cut for correction/retraction routing. ``DirectiveAnalysis`` | None.

    Reads the dependency parse only — no cue word-list, no routing decision (the caller owns
    both). Returns ``None`` when the layer is unavailable or on any failure (caller fails SAFE
    to STATEMENT — a parse miss must never route a fact to the destructive supersede path).
    """
    doc = _parse(text)
    if doc is None:
        return None
    try:
        imperative_root_lemma = None
        imperative_root_negated = False
        root_verb_lemma = None
        root_subject_is_self = False
        clause_initial_markers: set[str] = set()
        has_contrastive_negation = False
        cessation_advmod_lemmas: set[str] = set()

        # CONTRASTIVE negation: a ``neg`` that negates a NOMINAL constituent — an apposition/
        # conjunct ("…Luna, not Bella") or a noun/proper-noun/number it directly heads ("…14,
        # not 12"). Plain PREDICATE negation hangs ``neg`` off the clause's AUX/VERB ("this is
        # not a problem", "I am not worried") → head POS is AUX/VERB → NOT contrastive. The
        # distinction is the head's grammatical class, not a word-list.
        for tok in doc:
            if tok.dep_ != "neg":
                continue
            h = tok.head
            # (a) NOMINAL contrast: ``neg`` over an apposition/conjunct, or a noun/propn/num it
            #     directly heads ("…Luna, not Bella", "…14, not 12").
            if h.dep_ in ("appos", "conj") or h.pos_ in ("NOUN", "PROPN", "NUM"):
                has_contrastive_negation = True
                break
            # (b) COMPLEMENT contrast: ``neg`` over a predicate complement (acomp/attr) that stands
            #     OPPOSITE an ASSERTED sibling complement of the same predicate ("is red, not BLUE").
            #     The asserted sibling is exactly what separates a CONTRAST (two complements, one
            #     negated) from plain predicate negation ("I am not worried" — a single, negated
            #     complement → head POS is ADJ/AUX with no asserted sibling → NOT contrastive). The
            #     distinction is the grammatical shape (a competing non-negated complement), not a
            #     word-list.
            if h.dep_ in ("acomp", "attr"):
                pred = h.head
                sibling_asserted = any(
                    c is not h
                    and c.dep_ in ("acomp", "attr", "conj")
                    and not any(g.dep_ == "neg" for g in c.children)
                    for c in pred.children
                )
                if sibling_asserted:
                    has_contrastive_negation = True
                    break

        # TEMPORAL-CESSATION shape: a ROOT predicate (copular AUX / verb) that is NEGATED and
        # scoped by an adverbial ("no longer", "anymore", "no more"). Grammar only — we report the
        # lemma of every ``advmod`` ADV hanging off the (or a) negated ROOT predicate; the caller's
        # bounded cessation-cue set decides whether it is a removal. Negation polarity is read from a
        # ``neg`` anywhere in the predicate's subtree (covers "no longer" — where ``neg`` "no" hangs
        # off the "longer" advmod — and "not … anymore" / "do not … anymore" — where ``neg`` hangs
        # off the ROOT/aux). Distinct from contrastive negation (nominal ``appos``/``conj``) and from
        # plain predicate negation with NO cessation adverbial ("is not a problem" — no advmod → empty).
        for tok in doc:
            if tok.dep_ != "ROOT" or tok.pos_ not in ("VERB", "AUX"):
                continue
            _advmods = [c for c in tok.children if c.dep_ == "advmod" and c.pos_ == "ADV"]
            if not _advmods:
                break
            # Predicate-scoped negation: a ``neg`` on the ROOT itself, or on any token the ROOT
            # heads ("no longer" → neg "no" under advmod "longer"; "not … anymore" → neg on ROOT).
            _negated = any(c.dep_ == "neg" for c in tok.children) or any(
                gc.dep_ == "neg" for c in tok.children for gc in c.children
            )
            if _negated:
                for am in _advmods:
                    lem = (am.lemma_ or am.text or "").strip().lower()
                    if lem:
                        cessation_advmod_lemmas.add(lem)
            break  # only the main ROOT clause

        # Sentence-initial discourse markers: the first content token of each sentence when it is
        # an adverbial/interjection ("Actually, …", "Rather, …"). Grammar only — caller filters.
        for sent in doc.sents:
            for t in sent:
                if t.is_punct or t.is_space:
                    continue
                if t.pos_ in ("ADV", "INTJ") or t.dep_ in ("advmod", "intj"):
                    lem = (t.lemma_ or t.text or "").strip().lower()
                    if lem:
                        clause_initial_markers.add(lem)
                break  # only the sentence-initial token

        # Main ROOT verb: imperative (no subject / "you") vs. 1st-person repair ("I meant").
        for tok in doc:
            if tok.dep_ != "ROOT" or tok.pos_ not in ("VERB", "AUX"):
                continue
            lemma = (tok.lemma_ or tok.text or "").strip().lower()
            neg = any(c.dep_ == "neg" for c in tok.children)
            subs = [c for c in tok.children if c.dep_ in ("nsubj", "nsubjpass")]
            if tok.pos_ == "VERB":
                root_verb_lemma = lemma
                if subs:
                    s0 = (subs[0].lemma_ or subs[0].text or "").strip().lower()
                    # Grammatical self-detection (NO word-list): a genuine 1st-person personal
                    # pronoun subject ("I meant" / "we said") — Person=1 ∧ PronType=Prs ∧ no Poss.
                    root_subject_is_self = _is_first_person_personal_pronoun(subs[0])
                    if s0 == "you":  # 2nd-person subject ~ imperative addressing the system
                        imperative_root_lemma = lemma
                        imperative_root_negated = neg
                else:  # no overt subject → imperative
                    imperative_root_lemma = lemma
                    imperative_root_negated = neg
            break  # only the main ROOT clause

        return DirectiveAnalysis(
            imperative_root_lemma=imperative_root_lemma,
            imperative_root_negated=imperative_root_negated,
            root_verb_lemma=root_verb_lemma,
            root_subject_is_self=root_subject_is_self,
            clause_initial_markers=frozenset(clause_initial_markers),
            has_contrastive_negation=has_contrastive_negation,
            cessation_advmod_lemmas=frozenset(cessation_advmod_lemmas),
        )
    except Exception as e:  # noqa: BLE001 — fail-safe
        log.warning("linguistics.analyze_directive_failed", error=str(e)[:160])
        return None


# ── DETERMINISTIC EVENT-DATE EXTRACTION (spaCy DATE spans + numeric regex → dateparser) ──
# THE WHY: real conversation dates ("March 15th", "3/22", "2023/04/10", "three weeks ago") never
# reached ``facts.event_date`` because the bespoke ingest path only did a strict ISO ``strptime``
# plus a fragile hand-rolled relative-date regex. This is the ROBUST, STILL-DETERMINISTIC
# replacement: spaCy's DATE NER ∪ a small numeric-date regex propose candidate spans;
# ``dateparser`` (a rule-based, reference-anchored parser — NO ML, NO embeddings) NORMALIZES and
# VALIDATES each candidate. A non-date span normalizes to ``None`` and is dropped → we never
# fabricate a date. First valid date wins (matches the existing "first date wins" rule).
#
# Determinism: dateparser is a deterministic rule engine; same (text, reference) → same date.
# Fail-safe: flag OFF / no spaCy model / no dateparser / any exception → (None, None) so the
# caller falls back to its existing strict-ISO + hand-rolled relative path. A miss → NULL
# event_date, NEVER today's wall-clock.
#
# NER NOTE: the shared ``_get_nlp()`` singleton loads with ``disable=["ner"]`` (the grammar layer
# needs only tagger/parser). The DATE path DOES need entity recognition, so it uses a SEPARATE
# lazy singleton (``_get_nlp_ner``) that keeps NER enabled. This is still spaCy ``en_core_web_sm``
# (the baked CNN) — NOT GLiNER2, NOT the LLM; it labels DATE spans only, never feeds GLiNER2.

# Numeric-date recall net: spaCy's CNN NER reliably DATE-labels worded dates ("March 15th",
# "last month") but MISSES bare numeric forms like "3/22". These two patterns catch the slash
# numeric forms; dateparser then validates each (a non-date numeric → None → dropped). Closed,
# grammar-agnostic regexes for SPAN DETECTION only — normalization is dateparser's job.
_NUMERIC_DATE_PATTERNS: tuple = (
    re.compile(r"\b\d{4}/\d{1,2}/\d{1,2}\b"),          # 2023/04/10  (year-first)
    re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),   # 3/22  or  03/22/2023
)

# dateparser settings: anchor relatives to the SESSION reference, prefer PAST resolutions
# (conversation dates are historical), and read ambiguous numeric dates as Month/Day/Year.
_DATEPARSER_SETTINGS: dict = {
    "PREFER_DATES_FROM": "past",
    "DATE_ORDER": "MDY",
    # Be strict-ish: require an explicit date-bearing token so bare words don't drift to "now".
    "STRICT_PARSING": False,
}

# dateparser language restriction — the single biggest perf win on the hot ingest path:
# unrestricted parse() runs multi-locale detection (~400ms/parse, degrading to ~3s in
# long-running processes — dateparser issue #457); pinning languages=["en"] is ~1600x faster
# (~0.25ms) with NO determinism change (English-only here anyway). Passed as a parse() KWARG,
# NOT a settings key. [dateparser issue #457 / usage docs]
_DATEPARSER_LANGUAGES: list = ["en"]

_nlp_ner = None                 # spaCy Language WITH ner enabled (DATE path only), or None
_ner_load_attempted = False
_ner_load_lock = threading.Lock()

# DATE-NER INPUT-WINDOW SAFETY (the over-token half, spaCy side). The shared GLiNER2 typing path
# segments-then-unions; spaCy's CNN NER does NOT truncate the same way, but a very long whole-turn
# doc is still wasteful and the segment→union discipline keeps the seam consistent. We run DATE
# NER PER SENTENCE via nlp.pipe — BUT we keep each candidate DATE span on its WHOLE SENTENCE/clause
# (we NEVER split below the clause): relative dates ("three weeks ago", "last Tuesday") need their
# anchor context to normalize, so sub-clause splitting would corrupt them. Char offsets are mapped
# back to the original text so first-date-by-position ordering is preserved across sentences.
# Env-overridable sentence count above which we segment (cheap; default 1 = always segment when >1).
try:
    _DATE_NER_MAX_SENTENCES_SINGLE = int(os.environ.get("DATE_NER_MAX_SENTENCES_SINGLE", "1"))
except (TypeError, ValueError):
    _DATE_NER_MAX_SENTENCES_SINGLE = 1

# Clause-safe sentence splitter (terminal punctuation + newlines). Conservative — never splits a
# clause/relative-time phrase; only at sentence boundaries. Returns (offset, sentence) pairs so
# DATE-span char positions remain anchored to the ORIGINAL text.
_DATE_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def _sentence_spans_with_offsets(text: str) -> list:
    """Split ``text`` into ``[(start_char, sentence_text), …]`` at SENTENCE boundaries only.

    Offsets index into the ORIGINAL ``text`` so downstream DATE-span positions stay correct for the
    first-date-by-position ordering. NEVER splits below a sentence/clause (relative-date anchor
    safety). Fail-safe: on any error, returns the whole text as a single (0, text) span.
    """
    if not text:
        return []
    try:
        spans: list = []
        pos = 0
        # iterate over the gaps the splitter would cut at, tracking original offsets
        last = 0
        for m in _DATE_SENT_SPLIT_RE.finditer(text):
            sent = text[last:m.start()]
            if sent.strip():
                # leading-whitespace-aware start offset
                lead = len(sent) - len(sent.lstrip())
                spans.append((last + lead, sent.strip()))
            last = m.end()
        tail = text[last:]
        if tail.strip():
            lead = len(tail) - len(tail.lstrip())
            spans.append((last + lead, tail.strip()))
        return spans if spans else [(0, text)]
    except Exception:  # noqa: BLE001 — fail-safe: one whole-text span
        return [(0, text)]


def _get_nlp_ner():
    """Return a spaCy pipeline WITH NER enabled (for DATE ents), or ``None`` if unavailable.

    Separate from ``_get_nlp()`` (which disables NER for the grammar layer). Same baked CNN
    (model named by the ``SPACY_MODEL`` env — pure config, no code literal). Loads once, caches
    failure (no per-turn retry). Gated by BOTH the temporal kill-switch and the general
    linguistic-layer flag.
    """
    global _nlp_ner, _ner_load_attempted
    if not (LINGUISTIC_LAYER and TEMPORAL_DATE_LAYER):
        return None
    if not _SPACY_MODEL:
        # No model configured (env unset/empty) → no-op, same fail-safe as a missing bake.
        return None
    if _nlp_ner is not None:
        return _nlp_ner
    if _ner_load_attempted:
        return None
    with _ner_load_lock:
        if _nlp_ner is not None:
            return _nlp_ner
        if _ner_load_attempted:
            return None
        _ner_load_attempted = True
        try:
            import spacy  # deferred: a missing spaCy degrades to no-op
        except Exception as e:  # noqa: BLE001 — fail-safe
            log.warning("linguistics.spacy_import_failed_ner", error=str(e)[:160])
            return None
        try:
            # Keep NER (we need DATE ents); drop only the parser+tagger we don't use here to
            # keep the pipe lean. ``ner`` requires ``tok2vec``, so disable nothing it depends on.
            _nlp_ner = spacy.load(_SPACY_MODEL, disable=["parser", "tagger", "attribute_ruler", "lemmatizer"])
            log.info("linguistics.ner_model_loaded", model=_SPACY_MODEL)
        except Exception as e:  # noqa: BLE001 — model not baked → no-op
            log.warning("linguistics.ner_model_load_failed", model=_SPACY_MODEL, error=str(e)[:160])
            _nlp_ner = None
    return _nlp_ner


def _date_granularity(span_text: str, parsed_dt) -> str:
    """Coarse day/month/year granularity for a normalized date, from the SPAN's surface form.

    Aligns with ``main._event_date_granularity`` semantics (which today only emits "day"). A span
    with an explicit day number → "day"; a month name with no day → "month"; a bare 4-digit year
    → "year"; default "day" (the common case + matches the existing parsers). Heuristic on the
    span surface only — never changes the parsed DATE, just labels its precision. Fail-safe "day".
    """
    try:
        s = (span_text or "").strip().lower()
        # bare year: "2023" or "in 2025"
        if re.fullmatch(r"(?:in\s+|on\s+)?\d{4}", s):
            return "year"
        # has an explicit day number? (digit 1-2 chars not part of a 4-digit year token)
        if re.search(r"\b\d{1,2}(?:st|nd|rd|th)?\b", s) or re.search(r"/\d{1,2}\b", s):
            return "day"
        # month name present but no day → month granularity
        _MONTHS = ("january", "february", "march", "april", "may", "june", "july",
                   "august", "september", "october", "november", "december",
                   "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept",
                   "oct", "nov", "dec")
        if any(m in s for m in _MONTHS):
            return "month"
    except Exception:  # noqa: BLE001
        pass
    return "day"


# ── Relative-vs-absolute span classification + closest-to-reference year anchoring ──
# WHY (LongMemEval temporal-reasoning root cause): dateparser's PREFER_DATES_FROM=past is CORRECT
# for relative expressions ("three weeks ago") but WRECKS an ABSOLUTE month-day with no year.
# "January 17th" @RELATIVE_BASE 2023-01-13 → prefer-past jumps a FULL YEAR back to 2022-01-17
# (Jan 17 is days-future vs Jan 13). So we split the two cases deterministically per span:
#   • RELATIVE span  → keep PREFER_DATES_FROM=past + RELATIVE_BASE (unchanged, correct).
#   • ABSOLUTE month-day, NO year → parse WITHOUT prefer-past, then anchor the year to the
#     candidate (ref.year-1 / ref.year / ref.year+1) whose date is CLOSEST to the reference.
#   • ABSOLUTE WITH an explicit year → use verbatim (no anchoring).
# Deterministic surface checks only — NO LLM, NO ML.

# RELATIVE-CUE recognition is DB-HELD + per-tenant + GROWABLE (migration 103 / temporal_pattern_overlay).
# The frozen in-code `_RELATIVE_DATE_CUES` word-list is RETIRED: a relative cue ("ago", "last",
# "yesterday", a grown tail cue) is now a row in `<tenant>.temporal_patterns` (anchor_type='relative'),
# resolved via the per-tenant overlay (seed-copied-at-provisioning ∪ freq-gated grown). The seed is
# ONLINE-EVIDENCED (dateparser English locale data + HeidelTime reThisNextLast — see migration 103),
# NOT a port of the old list. The closed FORMAL ABSOLUTE class STAYS in code below (genuinely closed):
# a 4-digit year token (`_EXPLICIT_YEAR_RE`); month-name / numeric M-D detection lives in the span
# collectors + the absolute branch in extract_event_date. Determinism is preserved (regex match on a
# DB-held but fixed-per-request cue set); fail-safe to the evidenced BOOTSTRAP set when the DB is down.
_EXPLICIT_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")  # a 4-digit year token in the span


def _span_has_explicit_year(span: str) -> bool:
    """True iff the span text carries an explicit 4-digit year (→ use the parse verbatim)."""
    return bool(_EXPLICIT_YEAR_RE.search(span or ""))


def _relative_cues():
    """Resolve the per-tenant ACTIVE relative-cue regex list via the overlay (ContextVar-bound to
    the request's tenant schema, the SAME binding the rel_type/taxonomy overlays use). Returns a
    list of compiled patterns. Fail-safe: any import/read failure → the evidenced BOOTSTRAP set so a
    DB-down / pre-migration tenant still anchors relatives instead of mis-treating them as absolute.
    """
    try:
        from src.api import temporal_pattern_overlay  # deferred: avoid import cycle / hard dep
        dsn = os.environ.get("POSTGRES_DSN", "")
        cues = temporal_pattern_overlay.resolve_current(dsn)
        if cues:
            return [c for (_pat, c) in cues]
        # Empty resolution (no dsn / empty table) → bootstrap so we never silently lose relatives.
        return [c for (_pat, c) in temporal_pattern_overlay._bootstrap_cues()]
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the temporal layer
        log.warning("linguistics.relative_cues_resolve_failed", error=str(e)[:160])
        try:
            from src.api import temporal_pattern_overlay
            return [c for (_pat, c) in temporal_pattern_overlay._bootstrap_cues()]
        except Exception:
            return []


def _date_cue_present(text: str) -> bool:
    """THE LATENCY GATE precheck. True iff `text` matches ANY active per-tenant temporal_patterns
    row (relative cue OR formal-absolute surface form) via the combined overlay matcher — a single
    .search() resolved against the request-bound tenant schema (warm cache = DB-free). False → the
    turn has no date cue → the caller SKIPS the entire date pipeline (no spaCy NER, no dateparser).

    Fail-safe: any import/resolution error → True (run the pipeline) so a real date is NEVER lost.
    """
    try:
        from src.api import temporal_pattern_overlay  # deferred: avoid import cycle / hard dep
        dsn = os.environ.get("POSTGRES_DSN", "")
        return temporal_pattern_overlay.text_has_date_cue(text, dsn)
    except Exception as e:  # noqa: BLE001 — fail-safe: never block the date pipeline on a gate error
        log.warning("linguistics.date_cue_gate_failed", error=str(e)[:160])
        return True


def _classify_span_anchor(span: str) -> str:
    """Classify a date SPAN's anchoring for dateparser: 'explicit_year' | 'absolute_no_year' |
    'relative'. RELATIVE-cue recognition is DB-held (per-tenant, growable) via the overlay; the
    closed FORMAL absolute checks (a 4-digit year token) stay in code.

      • 'explicit_year'     — the span carries a 4-digit year → parse verbatim (no anchoring).
      • 'relative'          — the span matches an ACTIVE relative-cue row → resolve against the
                              reference, keep prefer-past ("three weeks ago", "last Tuesday").
      • 'absolute_no_year'  — neither → an absolute month-day with no year ("January 17th",
                              "3/22") → parse without prefer-past + closest-to-reference YEAR anchor.

    Surface-only + deterministic for a given request (the cue set is fixed per request). Fail-safe:
    an empty span → 'absolute_no_year' (the non-relative default, preserving today's anchoring).
    """
    s = (span or "").lower()
    if not s:
        return "absolute_no_year"
    if _span_has_explicit_year(s):
        return "explicit_year"
    for c in _relative_cues():
        try:
            if c.search(s):
                return "relative"
        except Exception:  # noqa: BLE001 — a bad compiled row must not crash classification
            continue
    return "absolute_no_year"


def _span_is_relative(span: str) -> bool:
    """True iff the span is a RELATIVE expression (resolve against reference, keep prefer-past).

    Thin compatibility wrapper over the DB-resolved `_classify_span_anchor`. A span with an
    explicit year is NOT relative (handled verbatim). A bare numeric "3/22" with no year is
    ABSOLUTE month-day (anchored), not relative.
    """
    return _classify_span_anchor(span) == "relative"


def _anchor_absolute_year(parsed, reference):
    """Anchor an ABSOLUTE month-day (parsed without prefer-past) to the CLOSEST-to-reference year.

    dateparser, run without PREFER_DATES_FROM, returns a date in some year (typically ref.year);
    we re-pin only the YEAR to whichever of {ref.year-1, ref.year, ref.year+1} yields the date
    nearest the reference (min |date − ref|), preserving month/day. Deterministic.

    e.g. "January 17th" @ref 2023-01-13 → 2023-01-17 (4 days), NOT 2022-01-17 (~1yr).
         "December 20th" @ref 2023-01-13 → 2022-12-20 (24 days), NOT 2023-12-20 (~1yr).
    Fail-safe: any error → the original parsed datetime unchanged.
    """
    try:
        ref_year = reference.year
        best = None
        best_dist = None
        for y in (ref_year - 1, ref_year, ref_year + 1):
            try:
                cand = parsed.replace(year=y)
            except ValueError:
                # Feb 29 on a non-leap candidate year — skip that year.
                continue
            # Distance in absolute seconds; both are midnight-normalized later, compare tz-aware.
            dist = abs((cand - reference).total_seconds())
            if best_dist is None or dist < best_dist:
                best, best_dist = cand, dist
        return best if best is not None else parsed
    except Exception as e:  # noqa: BLE001 — fail-safe: keep the original parse
        log.warning("linguistics.year_anchor_failed", error=str(e)[:160])
        return parsed


# ── WEEKDAY-RELATIVE TRANSLATE GATE ("last Tuesday" / "next Monday" / "this Friday") ──
# WHY (live-isolated gap, LongMemEval temporal-reasoning): this install's ``dateparser`` returns
# None for the weekday-relative construction ("last Tuesday", "next Monday") even with RELATIVE_BASE
# set — it resolves "three weeks ago"/"yesterday" fine, but cannot compute "<dir> <weekday>". The
# span IS detected by spaCy DATE NER and IS classified 'relative' by ``_classify_span_anchor``; the
# ONLY failure is dateparser's date math. We close that one gap DETERMINISTICALLY (no LLM, no ML)
# by computing the date from the session reference with pure calendar arithmetic.
#
# SEMANTICS (the deictic-weekday rule — HeidelTime/SUTime/TIMEX3 normalize this against a reference;
# online-verified against dateutil's documented weekday operator, which is NOT directional on its own):
#   • "last <weekday>"  → the most recent <weekday> STRICTLY BEFORE the reference.
#   • "next <weekday>"  → the first <weekday> STRICTLY AFTER the reference.
#   • "this <weekday>"  → the <weekday> within the reference's CURRENT ISO week (Monday-start).
# The STRICT exclusion of the reference day is the documented off-by-one fix: dateutil's
# ``relativedelta(weekday=TU(-1))`` returns TODAY when today IS Tuesday (the sign is "Nth occurrence",
# not future/past), so the dateutil idiom for STRICT is ``days=∓1`` first
# (``base + relativedelta(days=+1, weekday=WD(+1))`` for strictly-next;
# ``base + relativedelta(days=-1, weekday=WD(-1))`` for strictly-prev) — and the pure-datetime path
# below enforces the same strictness via modular arithmetic. "this <weekday>" when the reference IS
# that weekday returns the reference day itself (in-week, no strict exclusion).
#
# CLOSED FORMAL CLASS: the 7 weekday names (+ common 3-letter abbreviations) are a closed grammatical
# set — legitimate in code exactly like the 12 month names in ``_date_granularity``. This is NOT an
# open-ended word-list. The DIRECTION cues (last/past/previous/next/this) already exist as relative-cue
# rows (migration 103, HeidelTime reThisNextLast); we recognize the SAME surface cues here as the
# direction modifier — no redundant cue list, just the direction sign each cue carries.
#
# Python's ``date.weekday()``: Monday=0 … Sunday=6 (we key the names to that index directly).
_WEEKDAY_INDEX: dict = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "weds": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
# Direction modifiers → strict direction. These mirror the migration-103 relative cues
# (HeidelTime reThisNextLast); a closed formal direction class, not a domain word-list.
_WD_DIR_BEFORE = ("last", "past", "previous", "prev")     # strictly before the reference
_WD_DIR_AFTER = ("next", "coming", "upcoming", "following")  # strictly after the reference
_WD_DIR_THIS = ("this", "current")                         # within the reference's current ISO week
_WEEKDAY_RELATIVE_RE = re.compile(
    r"\b(last|past|previous|prev|next|coming|upcoming|following|this|current)\s+"
    r"(monday|mon|tuesday|tues|tue|wednesday|weds|wed|thursday|thurs|thur|thu|friday|fri|"
    r"saturday|sat|sunday|sun)\b",
    re.IGNORECASE,
)
# A standalone weekday token (no direction modifier in front). Used ONLY to DE-PRIORITIZE a bare
# weekday in the span-winner selection (see ``_date_span_preference_rank``): "Wednesday" lifted
# from the proper noun "Ash Wednesday" resolves to the reference week and would otherwise beat a
# real absolute month-day by position. Closed formal class (the same 7 weekday names + abbrevs).
_WEEKDAY_BARE_RE = re.compile(
    r"\b(monday|mon|tuesday|tues|tue|wednesday|weds|wed|thursday|thurs|thur|thu|friday|fri|"
    r"saturday|sat|sunday|sun)\b",
    re.IGNORECASE,
)


def _is_bare_weekday_without_cue(span: str) -> bool:
    """True iff ``span`` is a STANDALONE weekday name with NO direction cue ("Wednesday", "Sunday").

    A bare weekday with no direction modifier ("last/next/this Wednesday") carries no real anchor —
    dateparser resolves it to the reference week, so it must NOT outrank a concrete absolute
    month-day (the "Ash Wednesday … February 1st" corruption). A weekday WITH a direction cue
    ("last Tuesday") is a legitimate relative date and is NOT bare. Surface-deterministic, no ML.

    Returns True only when: a weekday token is present, NO direction-cued weekday is present, AND
    the span has no explicit 4-digit year (a yearful span is never "bare"). Fail-safe: any error →
    False (do not de-prioritize on uncertainty)."""
    s = (span or "").lower()
    if not s:
        return False
    try:
        if _span_has_explicit_year(s):
            return False
        if not _WEEKDAY_BARE_RE.search(s):
            return False
        # A direction-cued weekday ("last Tuesday") is a real relative date, not bare.
        if _WEEKDAY_RELATIVE_RE.search(s):
            return False
        return True
    except Exception as e:  # noqa: BLE001 — fail-safe: never de-prioritize on uncertainty
        log.warning("linguistics.bare_weekday_test_failed", span=(span or "")[:64], error=str(e)[:160])
        return False


# ── BARE VAGUE-RELATIVE PERIOD GATE (Option A — see main._parse_relative_date "DELIBERATELY EXCLUDED") ──
# A bare period phrase — "<last|next|this> <month|week|year>" with NO concrete anchor — is ambiguous
# about WHICH calendar date it pins, so Option A DELIBERATELY does NOT ground it: it returns NULL
# event_date → the caller (_detect_temporal) reports ("now", None, None). The legacy fallback parser
# (_parse_relative_date) already excludes these; this gate restores the SAME exclusion in the PRIMARY
# spaCy-DATE+dateparser layer (which would otherwise resolve "next month" to a fabricated day-precise
# date). NEVER fabricate precision we don't have. A SPECIFIC date is NOT bare and still grounds:
#   • a weekday-relative ("last Tuesday") — weekday, not a bare period word.
#   • a quantified relative ("in two weeks", "three weeks ago") — carries a number / "ago" / "in".
#   • a period phrase with a concrete day ("next month on the 5th") — carries a day number.
#   • an explicit 4-digit year ("next year 2027") — yearful spans are never bare.
# Closed formal class (3 period words × 3 direction cues); deterministic surface check, NO ML.
# Ungrounds only FORWARD/present bare relative periods ("next month", "this week") — a vague
# future intention must not fabricate a date. PAST bare relatives ("last month") DO ground
# (established contract: extract_event_date("last month") → ref−1mo), so the backward cues
# (last/previous/prev/past) are deliberately NOT gated here. (Grounding past relatives at COARSE
# month/week/year granularity instead of day-precise is a separate deferred enhancement.)
_BARE_VAGUE_PERIOD_RE = re.compile(
    r"^\s*(?:next|this|coming|upcoming|following|current)\s+"
    r"(?:month|week|year)\s*$",
    re.IGNORECASE,
)


def _is_bare_vague_relative(span: str) -> bool:
    """True iff ``span`` is a BARE vague-relative PERIOD phrase ("next month", "last week",
    "this year") with no concrete date anchor → Option A does NOT ground it (→ NULL event_date).

    Returns True ONLY when the WHOLE span is a direction-cue + period-word and nothing else: it
    carries no explicit 4-digit year, no day number, and is not a weekday-relative. Those concrete
    forms ("next month on the 5th", "last Tuesday", "next year 2027") are NOT bare and still ground.
    Surface-deterministic, no ML. Fail-safe: any error → False (do not drop on uncertainty)."""
    s = (span or "").strip().lower()
    if not s:
        return False
    try:
        if _span_has_explicit_year(s):
            return False                       # yearful span is never bare
        if re.search(r"\b\d{1,2}(?:st|nd|rd|th)?\b", s):
            return False                       # a concrete day number → not bare
        if _WEEKDAY_RELATIVE_RE.search(s) or _WEEKDAY_BARE_RE.search(s):
            return False                       # weekday-relative is a real anchor, not bare
        return bool(_BARE_VAGUE_PERIOD_RE.match(s))
    except Exception as e:  # noqa: BLE001 — fail-safe: never drop a span on uncertainty
        log.warning("linguistics.bare_vague_relative_test_failed",
                    span=(span or "")[:64], error=str(e)[:160])
        return False


def _date_span_preference_rank(span: str) -> int:
    """Winner-preference rank for a candidate date span — LOWER rank wins (ties → by position).

    Replaces the strict left-to-right "first span by position wins" with a PREFERENCE ORDER so a
    real absolute month-day beats a bare weekday lifted from a proper noun:
      0  explicit-year ("February 1st, 2023") / absolute month-day ("February 1st", "3/22")
      1  relative WITH a direction cue ("last Tuesday", "three weeks ago")
      2  bare weekday with NO direction cue ("Wednesday", "Sunday")  ← only wins if SOLE candidate
    A bare weekday still resolves when it is the only candidate (rank-2 vs nothing); it just never
    outranks a concrete date present in the same text. Surface-deterministic, no LLM/ML."""
    if _is_bare_weekday_without_cue(span):
        return 2
    if _classify_span_anchor(span) == "relative":
        return 1
    return 0


def _resolve_weekday_relative(span: str, reference):
    """Deterministically resolve a "<direction> <weekday>" span to a date against ``reference``.

    Closes the live gap where this install's ``dateparser`` returns None for "last Tuesday" /
    "next Monday" / "this Friday" (it handles "three weeks ago"/"yesterday" fine). PURE calendar
    arithmetic — NO LLM, NO ML, NO dateparser. The reference day is STRICTLY excluded for
    last/next (deictic-weekday rule, online-verified); included for "this" (same-week).

    Returns a tz-aware ``datetime`` at midnight in ``reference``'s tzinfo, or ``None`` when the span
    does not cleanly match the "<direction> <weekday>" shape (fail-loud → caller falls through →
    ultimately NULL event_date, NEVER a fabricated date).
    """
    if not span or reference is None:
        return None
    m = _WEEKDAY_RELATIVE_RE.search(span)
    if m is None:
        return None
    try:
        direction = m.group(1).strip().lower()
        weekday_name = m.group(2).strip().lower()
        target = _WEEKDAY_INDEX.get(weekday_name)
        if target is None:
            return None  # abbreviation not in the closed set → fall through (fail-safe)

        ref_wd = reference.weekday()  # Monday=0 … Sunday=6

        if direction in _WD_DIR_BEFORE:
            # Most recent <weekday> STRICTLY before the reference. delta ∈ [1..7]:
            # if today IS the weekday, go a full week back (strict exclusion of today).
            delta = (ref_wd - target) % 7
            if delta == 0:
                delta = 7
            result = reference - _timedelta(days=delta)
        elif direction in _WD_DIR_AFTER:
            # First <weekday> STRICTLY after the reference. delta ∈ [1..7]:
            # if today IS the weekday, go a full week forward (strict exclusion of today).
            delta = (target - ref_wd) % 7
            if delta == 0:
                delta = 7
            result = reference + _timedelta(days=delta)
        elif direction in _WD_DIR_THIS:
            # The <weekday> of the reference's CURRENT ISO week (Monday-start). Reference day
            # included when it IS the weekday. Offset = target − weekday_of_reference (signed).
            result = reference + _timedelta(days=(target - ref_wd))
        else:
            return None  # unrecognized direction → fail-safe fall-through

        # Day-granular, midnight in the reference tz (consistent with extract_event_date).
        return result.replace(hour=0, minute=0, second=0, microsecond=0)
    except Exception as e:  # noqa: BLE001 — fail-safe: any miscompute → fall through to None
        log.warning("linguistics.weekday_relative_failed", span=(span or "")[:64], error=str(e)[:160])
        return None


# ── VAGUE-MONTH NORMALIZATION ("mid-February", "early March", "late January") ─────────────────
# WHY (Agent 3 traced drop — the bike answer): a vague-within-month phrase ("mid-February",
# "early March", "late January") IS a real, datable signal but this install's ``dateparser``
# returns None for it (the leading vague qualifier defeats the month parse), and spaCy tags it a
# DATE span. Without this it falls through to NULL event_date and the gold event is lost. We
# normalize it DETERMINISTICALLY to a concrete day within the named month, anchored to the SAME
# session reference + closest-year rule the absolute month-day path uses:
#   • early  → the 5th    • mid → the 15th    • late → the 25th
# Granularity is "month" (the day is a deterministic representative WITHIN the month, not a
# user-stated day) so recall never over-claims day precision we don't have. CLOSED formal class:
# the 3 vague qualifiers × the 12 month names (the same closed month set as ``_date_granularity``)
# — a language primitive, NOT a domain word-list. Surface-deterministic, NO LLM, NO ML.
_VAGUE_MONTH_DAY: dict = {
    "early": 5,
    "mid": 15,
    "middle": 15,
    "late": 25,
    "end": 25,
    "beginning": 5,
    "start": 5,
}
_MONTH_INDEX: dict = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
# "<qualifier>[- ]<month>" or "<qualifier> of <month>" / "the <qualifier> of <month>". The
# qualifier may be hyphen- or space-joined ("mid-February" / "mid February") or possessive
# ("the beginning of March"). An explicit day number ("mid-February 12th") or 4-digit year is
# handled by the normal absolute/explicit branch and NOT routed here (this gate only fires when
# there is no concrete day in the span).
_VAGUE_MONTH_RE = re.compile(
    r"\b(early|mid|middle|late|end|beginning|start)\b"
    r"(?:\s+of)?[-\s]+"
    r"(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|"
    r"august|aug|september|sept|sep|october|oct|november|nov|december|dec)\b",
    re.IGNORECASE,
)


def _resolve_vague_month(span: str, reference):
    """Resolve a VAGUE-within-month span ("mid-February", "early March") to a concrete date.

    Returns ``(datetime_at_midnight_ref_tz, "month")`` for the representative day within the named
    month, year anchored CLOSEST to ``reference`` (the same closest-year rule the absolute month-day
    path uses), or ``(None, None)`` when the span is not a bare vague-month phrase / on any failure.
    The representative day (early=5, mid=15, late=25) is OUR deterministic placement WITHIN the
    month — granularity stays "month" so recall never claims a user-stated day. PURE calendar
    arithmetic, NO LLM/ML/dateparser. Skips a span carrying a concrete day number or an explicit
    year (those are real absolute/explicit dates handled verbatim by the normal branch)."""
    if not span or reference is None:
        return (None, None)
    try:
        s = span.strip().lower()
        # An explicit day or 4-digit year means it is NOT a bare vague-month phrase — let the
        # normal absolute/explicit-year branch own it (never downgrade a concrete date to month).
        if _span_has_explicit_year(s) or re.search(r"\b\d{1,2}(?:st|nd|rd|th)?\b", s):
            return (None, None)
        m = _VAGUE_MONTH_RE.search(s)
        if m is None:
            return (None, None)
        qualifier = m.group(1).strip().lower()
        month_name = m.group(2).strip().lower()
        day = _VAGUE_MONTH_DAY.get(qualifier)
        month = _MONTH_INDEX.get(month_name)
        if day is None or month is None:
            return (None, None)
        from datetime import datetime as _dt_vm
        ref_tz = getattr(reference, "tzinfo", None)
        # Build the month-day at the reference's year, then anchor the YEAR closest to reference
        # (Jan stated near a December reference should land in the right calendar year).
        try:
            candidate = _dt_vm(reference.year, month, day, tzinfo=ref_tz)
        except ValueError:
            return (None, None)
        candidate = _anchor_absolute_year(candidate, reference)
        candidate = candidate.replace(hour=0, minute=0, second=0, microsecond=0)
        return (candidate, "month")
    except Exception as e:  # noqa: BLE001 — fail-safe: a miss → NULL, never fabricate a date
        log.warning("linguistics.vague_month_failed", span=(span or "")[:64], error=str(e)[:160])
        return (None, None)


# ── MODEL-YEAR DISCRIMINATOR (structural; UD dep relations + POS only) ──────────────────────────
# WHY (root cause): the DATE-NER pipeline (``_get_nlp_ner``) runs with the PARSER DISABLED, so a bare
# 4-digit year that PRE-MODIFIES a product noun ("2018 Ford Mustang GT", "a 2018 model", "my 2015
# ThinkPad") is tagged DATE with no dependency context to tell it apart from a real temporal year, and
# dateparser then back-fills it into a frankenstein date (year 2018 + the session reference month/day).
# A premodifying year of a product noun is a NUMERIC CLASSIFIER (a model/spec year), NOT a TIMEX —
# TIMEX3 explicitly excludes numeric premodifiers that classify a noun ("the 2018 model" is a kind of
# model, not a point in time). The Universal-Dependencies parse exposes the discriminator STRUCTURALLY:
#   • year token's ``dep_`` ∈ {nummod, nmod, compound, amod}  AND
#   • the year's ``head.pos_`` ∈ {NOUN, PROPN}  AND
#   • that head is NON-temporal (not itself part of / adjacent to a DATE span)
#       → REJECT (a model-year / product spec, not a date).
#   • a year that is ``pobj`` of a preposition ("in 2018", "born in 1990"), or whose head IS temporal
#     ("the 2018 fiscal year"), or any non-bare-year date ("June 14th") → KEEP.
# The dep_/head come from the PARSER-enabled ``_get_nlp()`` (the DATE-NER pipeline has the parser off);
# the temporal-head test reuses the DATE-NER pipeline (head token inside a DATE ent). This is a closed
# set of LANGUAGE PRIMITIVES (UD dep labels + UD POS tags) — there is NO product/noun/year word-list.
# Refs: Universal Dependencies (de Marneffe et al., 2021) nummod/nmod/compound/amod relations; ISO-
# TimeML/TIMEX3 — a year premodifying a product noun is a numeric classifier, not a temporal TIMEX.
_YEAR_MODIFIER_DEPS: frozenset[str] = frozenset({"nummod", "nmod", "compound", "amod"})
_NOMINAL_HEAD_POS: frozenset[str] = frozenset({"NOUN", "PROPN"})


def _span_is_bare_year(span: str) -> bool:
    """True iff the span is JUST a bare 4-digit year (optionally a leading 'in'/'on'). Only bare-year
    spans are in scope for the model-year reject; "June 14th 2018" / "June 14th" are out of scope."""
    s = (span or "").strip().lower()
    return bool(re.fullmatch(r"(?:in\s+|on\s+)?(?:19|20)\d{2}", s))


def _head_token_is_temporal(head_tok, ner_doc) -> bool:
    """STRUCTURAL temporal-head test (no noun list): True iff the year's grammatical head is itself
    part of a DATE span ("the 2018 fiscal year" — 'year' is inside the DATE ent). Aligns the parser
    head (``_get_nlp``) to the DATE-NER doc (``_get_nlp_ner``) BY CHARACTER OFFSET, since they are two
    separate pipelines. Fail-safe: any miss → False (treat head as non-temporal → the reject can fire),
    matching the conservative "a product head is not temporal" default."""
    if head_tok is None or ner_doc is None:
        return False
    try:
        h_start = head_tok.idx
        h_end = h_start + len(head_tok.text)
        for ent in getattr(ner_doc, "ents", ()):
            if ent.label_ != "DATE":
                continue
            # head char-span overlaps a DATE ent char-span → head is part of a temporal phrase.
            if h_start < ent.end_char and h_end > ent.start_char:
                return True
    except Exception:  # noqa: BLE001 — fail-safe: undecidable → non-temporal
        return False
    return False


def _year_span_is_noun_modifier(text: str, span_start: int, span: str) -> bool:
    """STRUCTURAL discriminator: True iff this candidate is a bare 4-digit YEAR that grammatically
    PRE-MODIFIES a non-temporal NOUN/PROPN (a model-year / product spec) and must be REJECTED as a
    DATE. Uses the PARSER-enabled ``_get_nlp()`` (the DATE-NER pipeline has the parser disabled).

    Returns True ONLY for the reject case; False for KEEP and for EVERY undecidable / fail-safe case
    (parser unavailable, parse error, year token not locatable, not a bare year, head is a preposition
    / temporal noun / non-nominal) — so a real date is NEVER dropped. Scope: bare 4-digit YEAR spans
    only; month/day/relative spans are out of scope (``_span_is_bare_year`` gates first).
    """
    try:
        if not _span_is_bare_year(span):
            return False  # only bare-year spans are model-year candidates
        nlp = _get_nlp()
        if nlp is None:
            return False  # parser unavailable → fail-safe KEEP (today's behavior)
        doc = nlp(text)
        if doc is None:
            return False
        # Locate the YEAR token. Prefer the one at the span's char offset (exact); else the first
        # 4-digit-year token whose surface matches the span's year digits. Fail-safe: not found → KEEP.
        m = _EXPLICIT_YEAR_RE.search(span or "")
        if m is None:
            return False
        year_digits = m.group(0)
        year_tok = None
        for tok in doc:
            if tok.text != year_digits:
                continue
            # exact-offset match wins; remember a digit-match as the fallback
            if span_start is not None and span_start >= 0 and tok.idx == span_start:
                year_tok = tok
                break
            if span_start is not None and span_start >= 0:
                # the span may carry a leading "in "/"on " — the year token sits a few chars in
                if tok.idx >= span_start and tok.idx <= span_start + len(span):
                    year_tok = year_tok or tok
            else:
                year_tok = year_tok or tok
        if year_tok is None:
            return False  # cannot locate the year in the parse → fail-safe KEEP
        head = year_tok.head
        # KEEP a year that is the pobj of a preposition ("in 2018", "born in 1990") or otherwise not a
        # nominal premodifier — only nummod/nmod/compound/amod of a NOUN/PROPN is a model-year shape.
        if year_tok.dep_ not in _YEAR_MODIFIER_DEPS:
            return False
        if head is None or head.pos_ not in _NOMINAL_HEAD_POS:
            return False
        # NON-temporal head guard: a temporal head ("the 2018 fiscal year") is a real date → KEEP.
        ner_doc = None
        try:
            ner_nlp = _get_nlp_ner()
            ner_doc = ner_nlp(text) if ner_nlp is not None else None
        except Exception:  # noqa: BLE001 — NER hiccup → treat head as non-temporal (reject can fire)
            ner_doc = None
        if _head_token_is_temporal(head, ner_doc):
            return False  # head is part of a DATE span → temporal noun → KEEP
        return True  # bare year premodifying a non-temporal NOUN/PROPN → model-year → REJECT
    except Exception as e:  # noqa: BLE001 — fail-safe: ANY error → KEEP the span (never lose a date)
        log.warning("linguistics.model_year_discriminator_failed", span=(span or "")[:64], error=str(e)[:160])
        return False


def _collect_date_spans(text: str) -> list:
    """Collect candidate date SPANS (spaCy DATE ents ∪ numeric-date regex), ordered by position.

    Shared span-detection for ``extract_event_date`` (normalize+validate each) and
    ``has_date_residue`` (does ANY date span exist?). Returns ``[(start_char, span_text), …]`` or
    ``[]`` when the layer is unavailable / on any failure. Detection ONLY — no dateparser here.
    """
    if not (LINGUISTIC_LAYER and TEMPORAL_DATE_LAYER):
        return []
    if not text or not text.strip():
        return []
    # ── LATENCY GATE (cheap DB-cue precheck) ──────────────────────────────────────────
    # Before the EXPENSIVE detectors (spaCy DATE NER + the numeric regexes that feed dateparser),
    # ask the combined per-tenant temporal_patterns matcher: does this turn carry ANY date cue
    # (relative cue OR a formal-absolute surface form: month name / numeric shape / 4-digit year)?
    # No cue → no possible date span → return [] WITHOUT loading spaCy NER. This is what keeps a
    # plain statement ("my name is Alexander") off the date pipeline. ONE combined .search()
    # (warm cache = DB-free). Fail-safe inside the overlay: any gate error → True → pipeline runs,
    # so a real date is never silently lost. Tenant cues resolve via the request-bound ContextVar
    # (same binding the cue/rel_type/taxonomy overlays use — set at the ingest temporal block).
    if not _date_cue_present(text):
        return []
    candidates: list = []
    seen_spans: set = set()
    try:
        nlp = _get_nlp_ner()
        if nlp is not None:
            # PER-SENTENCE DATE NER (segment→union discipline). We split at SENTENCE boundaries
            # only and keep each DATE span on its WHOLE sentence/clause — relative dates need the
            # anchor context, so we NEVER split below the clause. Char offsets are remapped to the
            # ORIGINAL text so first-date-by-position ordering still holds. nlp.pipe batches the
            # sentences for one efficient pass.
            sent_spans = _sentence_spans_with_offsets(text)
            if len(sent_spans) <= _DATE_NER_MAX_SENTENCES_SINGLE:
                # Short turn — single whole-text pass (already in-window; avoid pipe overhead).
                docs = [(0, nlp(text))]
            else:
                _texts = [s for (_off, s) in sent_spans]
                docs = list(zip((off for (off, _s) in sent_spans), nlp.pipe(_texts)))
            for base_off, doc in docs:
                for ent in getattr(doc, "ents", ()):  # DATE ents only
                    if ent.label_ == "DATE":
                        span = (ent.text or "").strip()
                        if span and span.lower() not in seen_spans:
                            candidates.append((base_off + ent.start_char, span))
                            seen_spans.add(span.lower())
    except Exception as e:  # noqa: BLE001 — spaCy miss must not crash; regex net still runs
        log.warning("linguistics.date_ner_failed", error=str(e)[:160])
    try:
        for pat in _NUMERIC_DATE_PATTERNS:
            for m in pat.finditer(text):
                span = m.group(0).strip()
                if span and span.lower() not in seen_spans:
                    candidates.append((m.start(), span))
                    seen_spans.add(span.lower())
    except Exception as e:  # noqa: BLE001 — fail-safe
        log.warning("linguistics.date_regex_failed", error=str(e)[:160])
    # ── MODEL-YEAR REJECT (structural; UD dep + POS) ──────────────────────────────────
    # Drop a bare 4-digit-YEAR candidate that grammatically PRE-MODIFIES a non-temporal NOUN/PROPN
    # (a model/spec year — "2018 Ford Mustang GT", "a 2018 model", "my 2015 ThinkPad"). The DATE-NER
    # pipeline that proposed it has the parser DISABLED, so we re-parse the WHOLE text ONCE here with
    # the parser-enabled ``_get_nlp()`` to read the year token's dep_/head. KEEP everything else
    # (prepositional years "in 2018", temporal-head years "the 2018 fiscal year", and ALL non-bare-year
    # dates). Fail-safe: parser unavailable / parse error / year not locatable → KEEP (a real date is
    # NEVER dropped). Every caller of extract_event_date(_and_residue) inherits the fix through here.
    try:
        candidates = [
            (start, span) for (start, span) in candidates
            if not _year_span_is_noun_modifier(text, start, span)
        ]
    except Exception as e:  # noqa: BLE001 — fail-safe: discriminator error → keep all candidates
        log.warning("linguistics.model_year_filter_failed", error=str(e)[:160])
    candidates.sort(key=lambda c: c[0])  # first date (by position) wins
    return candidates


def has_date_residue(text: str, reference) -> bool:
    """True iff ``text`` contains a span that NORMALIZES to a real date (deterministic, fail-safe).

    This is the DETERMINISTIC RESIDUE SIGNAL for the occurrence/event-capture seam: a sentence
    that carries a parseable date/relative-time ("on March 3, 2021", "last Tuesday", "3/22") but
    whose structured lanes (GLiNER2 relations, verb-lift, the deterministic LVC seam) produced NO
    host edge — so the parsed ``event_date`` has nothing to attach to. The caller escalates JUST
    that residue to a targeted occurrence classification. It reuses the EXACT span detection +
    ``dateparser`` validation as ``extract_event_date`` (a non-date span normalizes to None →
    not residue), so the two never disagree about "is there a date here?".

    Returns ``False`` when the layer is unavailable / no date span resolves / on any failure
    (fail-safe: no residue → no escalation → today's behavior). NEVER fabricates a date.
    """
    if not (LINGUISTIC_LAYER and TEMPORAL_DATE_LAYER):
        return False
    if not text or not text.strip() or reference is None:
        return False
    try:
        import dateparser  # deferred: missing dep → no residue (fail-safe)
    except Exception as e:  # noqa: BLE001 — fail-safe
        log.warning("linguistics.dateparser_import_failed", error=str(e)[:160])
        return False
    candidates = _collect_date_spans(text)
    if not candidates:
        return False
    settings = dict(_DATEPARSER_SETTINGS)
    settings["RELATIVE_BASE"] = reference
    for _start, span in candidates:
        try:
            if dateparser.parse(span, languages=_DATEPARSER_LANGUAGES, settings=settings) is not None:
                return True  # at least one span is a REAL date → residue present
        except Exception:  # noqa: BLE001 — one bad span → try the next
            continue
    return False


def _resolve_first_valid_date(text: str, reference):
    """Resolve the FIRST (preference-ordered) candidate date span in ``text`` against ``reference``.

    The shared core of ``extract_event_date`` (returns just the date) and
    ``extract_event_date_and_residue`` (also needs WHICH span won, so it can be peeled out of the
    clause). Returns a 4-tuple ``(iso_date, granularity, span_start, span_text)`` for the winning
    span, or ``(None, None, None, None)`` when no span resolves / the layer is unavailable / on any
    failure (fail-safe — NEVER fabricates a date).

    ``span_start``/``span_text`` are the ORIGINAL-text char offset + surface of the span that
    resolved, so a caller can strip exactly those characters. They are reported for EVERY resolution
    path that yields a date, including the vague-month and weekday-relative gates.

    DETERMINISTIC (dateparser is a rule engine — no ML, no embeddings). ``reference`` threads the
    SESSION date so relatives ("three weeks ago") resolve against the conversation, not wall-clock.
    """
    if not (LINGUISTIC_LAYER and TEMPORAL_DATE_LAYER):
        return (None, None, None, None)
    if not text or not text.strip() or reference is None:
        return (None, None, None, None)
    try:
        import dateparser  # deferred: missing dep → no-op fallback
    except Exception as e:  # noqa: BLE001 — fail-safe
        log.warning("linguistics.dateparser_import_failed", error=str(e)[:160])
        return (None, None, None, None)

    # ── 1. Collect candidate spans (ordered by position) — spaCy DATE ents ∪ numeric regex ──
    candidates = _collect_date_spans(text)  # shared with has_date_residue (single detector)
    if not candidates:
        return (None, None, None, None)

    # ── 1b. Re-order by PREFERENCE (lower rank wins; ties keep position order) ──
    # Strict left-to-right would let a bare weekday lifted from a proper noun ("Wednesday" in
    # "Ash Wednesday") beat a real absolute month-day ("February 1st") that appears later. The
    # preference rank fixes that: explicit/absolute > relative-with-cue > bare-weekday-without-cue.
    # A bare weekday still wins when it is the SOLE candidate (preserves "last Tuesday" too: that
    # carries a direction cue so it ranks ABOVE bare and is unaffected). Stable sort → position is
    # the tie-break within a rank, preserving every existing single-anchor behavior.
    candidates = [c for _r, c in sorted(
        ((_date_span_preference_rank(span), (start, span)) for start, span in candidates),
        key=lambda rc: (rc[0], rc[1][0]),
    )]

    # ── 2. Normalize + VALIDATE each span with dateparser; first valid wins ──
    # Per span, branch on RELATIVE vs ABSOLUTE-month-day (the LongMemEval year-corruption lever):
    #   • RELATIVE / explicit-year → PREFER_DATES_FROM=past + RELATIVE_BASE (unchanged, correct).
    #   • ABSOLUTE month-day, NO year → parse WITHOUT prefer-past, then anchor year CLOSEST to ref.
    base_settings = dict(_DATEPARSER_SETTINGS)            # carries PREFER_DATES_FROM=past
    base_settings["RELATIVE_BASE"] = reference
    abs_settings = dict(_DATEPARSER_SETTINGS)             # absolute month-day: DROP prefer-past
    abs_settings["RELATIVE_BASE"] = reference
    abs_settings.pop("PREFER_DATES_FROM", None)
    ref_tz = getattr(reference, "tzinfo", None)
    for _start, span in candidates:
        # Deterministic surface classification (NO LLM, NO ML). RELATIVE-cue recognition is
        # DB-held per-tenant (temporal_patterns via the overlay); the closed formal absolute
        # checks (4-digit year) stay in code. 'absolute_no_year' → anchor the YEAR closest to ref.
        # OPTION A: a BARE vague-relative period ("next month", "last week", "this year") carries no
        # concrete date — do NOT fabricate a day-precise date for it. Skip the span (a concrete day /
        # weekday-relative / explicit-year span in the same text still wins). See _is_bare_vague_relative.
        if _is_bare_vague_relative(span):
            continue
        # VAGUE-MONTH GATE (Agent 3 drop — the bike answer): "mid-February" / "early March" /
        # "late January" is a real datable signal this install's dateparser returns None for. Resolve
        # it DETERMINISTICALLY to a representative day within the named month (early=5/mid=15/late=25),
        # year-anchored to the reference, granularity "month" (we don't claim a user-stated day). A
        # span with a concrete day/explicit year is NOT routed here (returns (None, None)) → falls
        # through to the normal absolute/explicit branch below, unchanged.
        _vm_dt, _vm_gran = _resolve_vague_month(span, reference)
        if _vm_dt is not None:
            try:
                return (_vm_dt.isoformat(), _vm_gran or "month", _start, span)
            except Exception as e:  # noqa: BLE001 — normalize hiccup → try the normal branch
                log.warning("linguistics.vague_month_iso_failed", span=span[:64], error=str(e)[:160])
        span_anchor = _classify_span_anchor(span)
        anchor_absolute = span_anchor == "absolute_no_year"
        settings = abs_settings if anchor_absolute else base_settings
        try:
            parsed = dateparser.parse(span, languages=_DATEPARSER_LANGUAGES, settings=settings)
        except Exception as e:  # noqa: BLE001 — a parse failure on one span → try the next
            log.warning("linguistics.dateparser_parse_failed", span=span[:64], error=str(e)[:160])
            parsed = None
        # WEEKDAY-RELATIVE TRANSLATE GATE: this install's dateparser returns None for
        # "<direction> <weekday>" (last Tuesday / next Monday / this Friday) though the span IS a
        # valid RELATIVE expression. When the span classified 'relative' and dateparser gave nothing,
        # compute it deterministically against the reference (strict deictic-weekday rule). A clean
        # match wins; a non-match returns None → fall through → still NULL (never fabricated).
        if parsed is None and span_anchor == "relative":
            wd_parsed = _resolve_weekday_relative(span, reference)
            if wd_parsed is not None:
                parsed = wd_parsed
        if parsed is None:
            continue  # non-date span (e.g. "the GPS system", "poke") → dropped, never fabricated
        try:
            # Normalize to the reference tz FIRST (anchor distance must be tz-consistent).
            if ref_tz is not None and parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ref_tz)
            if anchor_absolute:
                # Re-pin only the YEAR to whichever of ref.year∓1/ref.year is nearest the reference,
                # so an absolute month-day ("January 17th") lands in the conversation's year, not a
                # prefer-past year-jump. Month/day preserved.
                parsed = _anchor_absolute_year(parsed, reference)
            # Day-granular: event_date column is TIMESTAMPTZ; store at midnight in the reference tz.
            parsed = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
            iso = parsed.isoformat()
        except Exception as e:  # noqa: BLE001 — normalization hiccup → try next span
            log.warning("linguistics.date_normalize_failed", span=span[:64], error=str(e)[:160])
            continue
        gran = _date_granularity(span, parsed)
        return (iso, gran, _start, span)

    return (None, None, None, None)


def extract_event_date(text: str, reference):
    """Deterministic event-date extraction for the temporal ingest layer.

    Detects candidate date SPANS (spaCy DATE ents ∪ numeric-date regex), then NORMALIZES and
    VALIDATES each with ``dateparser`` anchored to ``reference`` (a tz-aware datetime). Returns
    ``(iso_date, granularity)`` for the FIRST span that resolves to a real date, or ``(None, None)``
    when no span resolves / the layer is unavailable / anything fails.

    DETERMINISTIC (dateparser is a rule engine — no ML, no embeddings). FAIL-SAFE: a miss yields
    ``(None, None)`` and the caller keeps NULL event_date — it NEVER fabricates "today". ``reference``
    threads the SESSION date so relative expressions ("three weeks ago") resolve against the
    conversation, not 2026 wall-clock.

    The ISO date returned is the parsed date at MIDNIGHT in ``reference``'s tzinfo (event_date is
    day-granular; the column is TIMESTAMPTZ) so it is consistent with the existing parsers.
    """
    iso, gran, _start, _span = _resolve_first_valid_date(text, reference)
    return (iso, gran)


# ── PEEL-AND-DROP-OUT — extract the date, REMOVE its span, return the date-free residue ─────────
# THE COMPOSITIONAL CAPTURE PRINCIPLE (Alexander's directive): "on a component existing → extract
# it, DROP IT OUT of the clause, then build the relation from the residue." For the DATE component
# this PREVENTS the circumstantial prep from FOLDING into the predicate: with "I got X on February
# 20th" the SVO grammar would otherwise attach the prepositional date ("on …") to the verb → a
# spurious ``get_on`` predicate (and a date masquerading as the prepositional object), while
# event_date came back NULL. Peeling the date span out FIRST means "on February 20th" is simply GONE
# from the residue and CANNOT fold — the SVO sees the bare verb ("got"→"get") and the real object.
#
# This is NOT "patch the predicate to avoid folding the date" — it is the same drop-out the rest of
# the system uses: peel each component, build the relation from what remains. The date is the one
# component peeled here (a genuine phrasal — "went TO a concert", "looked FOR it" — has no DATE pobj,
# so nothing is peeled and the phrasal survives). Structured so a future circumstantial (source /
# locative) could peel the same way; ONLY the date peel is implemented now.
def extract_event_date_and_residue(text: str, reference):
    """Peel the FIRST resolvable date span out of ``text`` and return the date + the date-free residue.

    Returns ``(iso_date, granularity, residue_text)``:
      • When a date resolves → its ``(iso, gran)`` plus ``residue_text`` = ``text`` with the winning
        date SPAN removed (and the now-dangling leading preposition that governed it — "on"/"in"/"at"
        directly preceding the span — dropped too, so "got a phone on Feb 20" → "got a phone", not
        "got a phone on "). Whitespace is collapsed; the residue is never empty (fail-safe: if removal
        would empty it, the ORIGINAL text is returned with the date still bound).
      • When NO date resolves / the layer is unavailable / on any failure → ``(None, None, text)``
        (the ORIGINAL text unchanged), so the caller's SVO build runs on today's input exactly.

    DETERMINISTIC, fail-safe, NO ML. The date is an event_date SCALAR — it is peeled OUT, never folded
    into the relation or treated as a relationship object (CLAUDE.md Temporal hard rule)."""
    if not text or not text.strip():
        return (None, None, text)
    # OFFSET-FROM-NAMED-EVENT pre-check ("a week before Black Friday"): dateparser would peel only
    # the bare "a week" → a bogus date AND leave "before Black Friday" in the residue. The named-event
    # offset resolver computes the real compound date and returns the WHOLE span to excise, so the
    # second comparison operand dates correctly and no dangling tail survives. Gated to the precise
    # grammatical pattern → behavior-preserving for every other span (a non-match falls straight
    # through to the engine). Deterministic, fail-safe.
    try:
        from src.temporal.named_events import resolve_offset_named_event_span as _off_span
        _off = _off_span(text, reference)
    except Exception:  # noqa: BLE001 — fail-open: no offset → engine path
        _off = None
    if _off is not None:
        try:
            from datetime import datetime, time
            o_d, o_gran, o_start, o_span = _off
            ref_tz = getattr(reference, "tzinfo", None)
            o_iso = datetime.combine(o_d, time(0, 0)).replace(tzinfo=ref_tz).isoformat()
            iso, gran, start, span = o_iso, o_gran, o_start, o_span
            # fall through to the SHARED excision below with this span
            return _peel_excise_span(text, iso, gran, start, span)
        except Exception as e:  # noqa: BLE001 — fail-safe → engine path
            log.warning("linguistics.peel_offset_failed", error=str(e)[:160])
    try:
        iso, gran, start, span = _resolve_first_valid_date(text, reference)
    except Exception as e:  # noqa: BLE001 — fail-safe: never break the caller's SVO build
        log.warning("linguistics.peel_resolve_failed", error=str(e)[:160])
        return (None, None, text)
    return _peel_excise_span(text, iso, gran, start, span)


def _peel_excise_span(text: str, iso, gran, start, span):
    """Shared date-span excision for the peel: remove ``span`` (at ``start``) + a dangling leading
    temporal preposition, return ``(iso, gran, residue)``. Used by BOTH the engine path and the
    offset-named-event path. Fail-safe: any problem → keep ORIGINAL text, still bind the date."""
    if not iso or start is None or not span:
        # No date (or offset unavailable) → return the original text, no date bound.
        return (iso, gran, text)
    try:
        end = start + len(span)
        if start < 0 or end > len(text):
            return (iso, gran, text)        # offset sanity failed → keep text, still bind the date
        # Drop a dangling LEADING preposition that governed the date ("on"/"in"/"at"/"by"/"from"/"of"
        # /"around"/"since"/"until"/"before"/"after") immediately before the span, plus its whitespace,
        # so we don't leave "got a phone on ". Closed grammatical class (temporal-governing ADPs) — a
        # language primitive, NOT a domain list. We only strip ONE such preposition adjacent to the span.
        head = text[:start]
        _m = _LEADING_TEMPORAL_PREP_RE.search(head)
        cut_start = _m.start() if _m is not None else start
        residue = (text[:cut_start] + " " + text[end:])
        # Collapse the whitespace seam left by the excision; trim leftover punctuation orphans.
        residue = re.sub(r"\s+", " ", residue).strip()
        residue = re.sub(r"\s+([,.;:!?])", r"\1", residue)      # "phone ," → "phone,"
        residue = residue.strip(" ,;:")
        if not residue or len(residue) < 2:
            # Excision emptied the clause → keep the ORIGINAL text but still bind the date (fail-safe).
            return (iso, gran, text)
        return (iso, gran, residue)
    except Exception as e:  # noqa: BLE001 — fail-safe: any excision error → original text + date
        log.warning("linguistics.peel_excise_failed", span=(span or "")[:64], error=str(e)[:160])
        return (iso, gran, text)


# Temporal-governing prepositions that DANGLE when the date span they govern is peeled out. A closed
# grammatical class (the ADPs that introduce a temporal adjunct), aligned with _SVO_KEEP_PARTICLES'
# temporal members — a language primitive, NOT a domain word-list. Matched ONLY immediately before
# the peeled span (optionally with a trailing "the"/"a", e.g. "in the" / "on the") so we excise the
# whole dangling temporal PP head, never an "on" that belongs to a real phrasal earlier in the clause.
_LEADING_TEMPORAL_PREP_RE = re.compile(
    r"\b(?:on|in|at|by|from|of|around|since|until|before|after|during)"
    r"(?:\s+the|\s+a|\s+an)?\s*$",
    re.IGNORECASE,
)
