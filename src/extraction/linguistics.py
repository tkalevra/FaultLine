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
#     Sarah" / "My mother's name is Robin" bind the kin rel to the NAMED person and register the ROLE
#     (sister/mother) as an alias/role-slot of that person, never a parallel role entity.
# DEFAULT OFF so the commit is dormant and the temporal first-10 path is byte-for-byte unchanged
# until validated ON. Fail-safe: flag OFF or any failure → today's behavior exactly.
SPINE_NAMING_CHAIN: bool = os.environ.get(
    "SPINE_NAMING_CHAIN", "false"
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
            if not complement:
                continue
            # A QUESTION ("what am I?", "who are you?") is not a value statement — skip an
            # interrogative complement (grammatical: PronType=Int / wh-tags, not a word list).
            if "Int" in comp.morph.get("PronType") or comp.tag_ in ("WP", "WP$", "WDT", "WRB"):
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

            # A QUESTION ("what is my favorite colour?") is not a statement of value — its
            # interrogative complement ("what"/"which"/"who") must NOT be captured as a fact.
            # Grammatical + subject-agnostic: wh-words carry PronType=Int / WP|WP$|WDT|WRB tags.
            # (Closed grammatical class read from morphology — NOT a hardcoded word list.)
            if "Int" in comp.morph.get("PronType") or comp.tag_ in ("WP", "WP$", "WDT", "WRB"):
                continue

            negated = any(c.dep_ == "neg" for c in head.children) or any(
                c.dep_ == "neg" for c in comp.children
            )
            value = (comp.text or "").strip().lower()
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
# THE WHY (RC2): "I have a dog named Rex" / "a server called Atlas" / "my dog is named
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


@dataclass(frozen=True)
class NamingAnalysis:
    """A deterministic reading of a naming/dubbing construction ("a dog named Rex").

    - ``named``       : the HEAD NOUN being named, lowercased ("dog", "server", "cat") — its
                        head plus left ``compound``/``amod`` modifiers ("file server"). NEVER the
                        speaker; the name binds to the THING named.
    - ``proper_name`` : the proper name assigned, surface form ("Rex", "Atlas").
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


def _np_conjuncts(head_tok) -> list:
    """Return ``head_tok`` plus its COORDINATED noun siblings ("tomatoes, peppers, and cucumbers").

    spaCy chains a coordinated list off the FIRST conjunct's ``conj`` dependents: in "tomatoes,
    peppers, and cucumbers" the parse is tomatoes →conj peppers →conj cucumbers (or all three hang
    off the head). We walk the ``conj`` subtree from the head and collect every coordinated NOUN/PROPN
    token. Returns the token list in source order (head first). A non-coordinated head returns just
    ``[head_tok]``. Structural only — NO list/word enumeration."""
    out = [head_tok]
    seen = {head_tok.i}
    frontier = [head_tok]
    while frontier:
        nxt = []
        for t in frontier:
            for c in t.children:
                if c.dep_ == "conj" and c.pos_ in ("NOUN", "PROPN") and c.i not in seen:
                    seen.add(c.i)
                    out.append(c)
                    nxt.append(c)
        frontier = nxt
    return sorted(out, key=lambda t: t.i)


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


def analyze_naming(text: str):
    r"""Deterministic first-cut for a naming/dubbing construction. Returns ``NamingAnalysis`` | None.

    THE RULE (subject-agnostic, dependency-driven — NO noun/keyword word-list):
      Find a naming verb (lemma "name"/"call" — the predicative naming class) and bind the PROPER
      NAME it assigns to the HEAD NOUN it modifies. spaCy structures the three target phrasings as:

        "I have a dog named Rex"  → "named" is an ``acl``/``vfin`` modifying the NOUN "dog";
                                        "Rex" is its ``oprd``/``attr``/``dobj`` child (PROPN).
        "a server called Atlas"      → same reduced-relative shape on "server".
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
            if (tok.lemma_ or "").strip().lower() not in _naming:
                continue
            if tok.pos_ not in ("VERB", "AUX"):
                continue

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
                continue

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
                continue

            # A naming construction whose "head noun" is itself the proper name (e.g. parse
            # quirks) is not a useful (thing, name) pair — require them distinct.
            named = _np_phrase(head_noun)
            proper_name = (proper.text or "").strip()
            if not named or not proper_name or named == proper_name.lower():
                continue

            negated = any(c.dep_ == "neg" for c in tok.children)
            return NamingAnalysis(named=named, proper_name=proper_name, negated=negated)
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
# ("Whiskers" lemmatized to "whisker") — both are accepted as the instance NAME because the
# ``appos`` dependency, not the POS, identifies it as the renaming of the head.


@dataclass(frozen=True)
class NamedInstanceAnalysis:
    """A deterministic reading of the named-instance copula+appositive construction
    ("My dog Rex is a poodle." / "My cat Whiskers is a tabby." / "My car Betsy is a Subaru.").

    - ``kind``         : the possessed HEAD NOUN being typed/named, lowercased ("dog", "cat",
                         "car") — its head plus left ``compound``/``amod`` modifiers. The broad KIND.
    - ``name``         : the appositive PROPER NAME of the specific instance, surface form
                         ("Rex", "Whiskers", "Betsy"). Goes in the NAMING layer — never L4.
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
            # /"Betsy", or NOUN "Whiskers"); a determiner-introduced appositive ("a poodle") is a
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

    - ``subject_text``    : the surface subject token/phrase, lowercased ("i", "we", "jordan").
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


def _svo_predicate_token(verb_tok, exclude_idx=None) -> str | None:
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


def _svo_object_head(verb_tok, exclude_idx=None):
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
                    if gc.dep_ == "pobj" and gc.pos_ in ("NOUN", "PROPN") and _ok(gc):
                        return gc
        # Attribute complement of a non-copula linking verb ("became a manager").
        for c in verb_tok.children:
            if c.dep_ in ("attr", "oprd") and c.pos_ in ("NOUN", "PROPN") and _ok(c):
                return c
    except Exception:  # noqa: BLE001 — fail-safe
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
            object_text = _np_phrase(obj_tok)
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


# DB-HELD + per-tenant + GROWABLE (linguistic_cue_overlay, category='problem_noun'). The frozenset below
# is the in-code DB-DOWN CODE-FALLBACK seed only: `_problem_nouns()` resolves the live set from
# `<tenant>.linguistic_cues` (seed-copied ∪ grown) via the SAME per-tenant overlay the LVC/naming/
# temporal layers use, and falls back to THIS frozenset only when the overlay is unavailable/unbound.
# Membership checks call `_problem_nouns()`, NOT this frozenset directly. This is a bounded LEXICAL
# class of SEMANTICALLY-EMPTY problem/fault HEADS — corroborated downstream by the parse (a with-PP
# affected entity must be present), NOT a domain word-list; a non-problem head never reaches the lane.
_PROBLEM_NOUN_LEMMAS: frozenset[str] = frozenset(
    {"issue", "problem", "trouble", "fault", "difficulty", "glitch", "bug", "error", "concern"}
)


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

        # (a) leaf-most compound-modifier crops anywhere in the verb's clause.
        for t in verb_tok.subtree:
            if (t.pos_ in ("NOUN", "PROPN") and t.dep_ == "compound"
                    and t.head is not None and t.head.pos_ in ("NOUN", "PROPN")
                    and not _has_compound_noun_child(t)):
                _add(t)
        # (a2) verb-attached xcomp/amod/nmod NOUN immediately LEFT of the item head — the "marigold
        #      seeds" mis-parse where the crop becomes a sibling of the item, not its compound child.
        for c in verb_tok.children:
            if (c.pos_ in ("NOUN", "PROPN") and c.dep_ in ("xcomp", "amod", "nmod")
                    and c.i == item_tok.i - 1 and not _has_compound_noun_child(c)):
                _add(c)
        # (b) coordination / apposition list members (each its own crop) + the list head.
        for t in verb_tok.subtree:
            if t.pos_ in ("NOUN", "PROPN") and t.dep_ in ("conj", "appos"):
                _add(t)
                h = t.head
                if (h is not None and h is not item_tok and h.pos_ in ("NOUN", "PROPN")
                        and h.dep_ in ("conj", "appos", "dobj", "obj")
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
          "the Atlas conference" handled by the modifier path). The title is the contiguous
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

        # (b2) COMPOUND PROPER-NOUN PREMODIFIER — "the Atlas webinar". The title is the head noun's
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


def _collect_eventive_heads(tok, text: str) -> list:
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

    def _add(_g):
        if _g is None or _g.i in seen:
            return
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
        # ("the Atlas webinar") on the eventive noun is the NAME of THIS occurrence — it rides
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

        # EVENT-TYPE EXCLUDES the lifted Title-Case compound premodifier (the "Atlas" in "the
        # Atlas webinar"): when the title was lifted from a leading Title-Case PROPN compound
        # that is embedded in the bare-type phrase, strip it so the occurrence object stays the
        # bare place ("webinar"), not "atlas webinar". Structural surface strip only — if the
        # title did not come from a premod (quoted/appos/acl), event is already the bare type.
        if title:
            _tl = title.strip().lower()
            if event.startswith(_tl + " "):
                _stripped = event[len(_tl):].strip()
                if _stripped:
                    event = _stripped
        # STATE-LANE SIGNAL (Stage 3): the eventive head is a SEMANTICALLY-EMPTY PROBLEM noun (lemma in
        # the DB-grown ``problem_noun`` cue class) AND a with-PP affected entity is present (``concerns``).
        # This is the "I had an issue WITH my car's GPS system" shape: the meaning lives in the affected
        # entity, not the empty head. Flag it so the caller emits a competing ``(<concerns>, has_state,
        # <problem-state>)`` candidate (the structural twin of ``feels``). THE DISCRIMINATOR is the cue
        # class ∧ the with-PP — both must hold, so "had a meeting WITH Sarah" (meeting ∉ problem_noun)
        # and "had an issue" with NO affected entity both leave problem_head False (untouched). The cue
        # class is DB-grown (NO noun literal in this logic); fail-safe: any resolve miss → code-fallback
        # set, never an empty gate. Read against the bare HEAD LEMMA, not the modified phrase.
        problem_head = False
        if concerns:
            try:
                _head_lemma = (event_noun.lemma_ or "").strip().lower()
                if _head_lemma and _head_lemma in _problem_nouns():
                    problem_head = True
            except Exception:  # noqa: BLE001 — fail-safe: lemma/overlay miss → not a problem head
                problem_head = False
        return EventAnalysis(event=event, title=title, concerns=concerns,
                             negated=negated, problem_head=problem_head)
    except Exception as e:  # noqa: BLE001 — fail-safe
        log.warning("linguistics.build_event_analysis_failed", error=str(e)[:160])
        return None


def analyze_events(text: str) -> list:
    r"""Deterministic capture of ALL light-verb + eventive-noun occurrences in ``text`` (T1 PRIMARY).
    Returns a ``list[EventAnalysis]`` (possibly empty) — one entry per distinct eventive head.

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
            for _head in _collect_eventive_heads(tok, text):
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


def derive_sentence_facts(sentence, reference, prior_nps=None, dash_specifier_only=False):
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
              negated=False):
        subj = (subject or "").strip().lower()
        rel = (rel or "").strip().lower()
        obj = (obj or "").strip().lower()
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
            else:
                rel = _minted
        if not (subj and rel and obj) or subj == obj:
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
        ))

    # ── INTRA-TURN PRONOUN COREF (shared by every chain) ─────────────────────────────────────────
    # Resolve a 3rd-person pronoun (it/they/them) with no in-sentence antecedent to the most-recent
    # compatible prior-turn NP. Fail-safe: no antecedent → leave the pronoun. (Was "Rule 1".)
    _prior = [p for p in (prior_nps or []) if p and str(p).strip()]

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

    def _chain_svo(doc):
        # SVO backbone (+ governing-verb date + conjunct distribution). Each non-copula content verb
        # with a subject and an object → (subject, verb-lemma[+particle], object). First-person subject
        # → "user". Naming verbs are owned by analyze_naming (caller seam) → skipped here.
        _naming = _naming_verbs()
        for tok in doc:
            if tok.pos_ != "VERB":
                continue
            lemma = (tok.lemma_ or tok.text or "").strip().lower()
            if not lemma or lemma == "be":
                continue
            if lemma in _naming:
                continue  # naming construction → analyze_naming owns it (caller runs that seam)
            subj_tok = next((c for c in tok.children if c.dep_ in ("nsubj", "nsubjpass")), None)
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
            predicate = _svo_predicate_token(svo_head, exclude_idx=_date_token_idx)
            if not predicate:
                continue
            obj_tok = _svo_object_head(svo_head, exclude_idx=_date_token_idx)
            if obj_tok is None:
                continue
            for _ct in _np_conjuncts(obj_tok):  # conjunct/dash-list distribution
                obj_phrase = _np_phrase(_ct)
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
            lemma = (tok.lemma_ or tok.text or "").strip().lower()
            if not lemma or lemma == "be":
                continue
            if lemma in _naming_verbs():
                continue
            subj_tok = next((c for c in tok.children if c.dep_ in ("nsubj", "nsubjpass")), None)
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
            if _svo_object_head(tok) is not None:
                continue  # SVO chain owns a NOUN/PROPN-object verb
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
            if tok.dep_ in ("ccomp", "xcomp"):
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
            # INTERPLAY GUARD (Fix 2): the "X's name is Y" naming construction is owned by the
            # GENITIVE-NAME chain (it binds Y as the person + attaches the kin relation there). The
            # possessive chain must stay OUT of it in BOTH directions:
            #   (a) the possessor leg ("my mother's …"): the head role-noun ("mother") is itself a
            #       ``poss`` of a "name" nsubj-of-copula → emitting (mother, parent_of, user) here would
            #       leave "mother" as a standalone entity (Fix 2 collapses it into the named person).
            #   (b) the naming leg ("…'s name is Robin"): the possessed head IS the naming noun "name"
            #       (nsubj of a copula) → emitting (name, related_to, mother) here is the spurious
            #       "name"-as-entity leak. Skip it; the genitive-name chain mints the real edges.
            # Detected grammatically (lemma "name" + nsubj-of-be), NO word list.
            def _is_name_copula_nsubj(_n):
                return (_n is not None and (_n.lemma_ or "").strip().lower() == "name"
                        and _n.dep_ in ("nsubj", "nsubjpass")
                        and _n.head is not None
                        and _n.head.lemma_ == "be" and _n.head.pos_ == "AUX")
            if head.dep_ == "poss" and _is_name_copula_nsubj(head.head):
                continue   # (a) possessor leg of "my mother's name is Robin"
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
        # "My server is Atlas" — a copular clause whose nsubj is a 1st-person-POSSESSED ROLE noun and
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
            # "the printer is Atlas"-style clauses where the subject is not user-possessed).
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

    def _chain_genitive_name(doc):
        # GENITIVE NAME-BINDING (Fix 2). "[poss] <relational-noun>'s name is <PROPN>"
        #   "my mother's name is Robin"     → (robin, parent_of, user)   [Robin is the named entity]
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
            # the role-noun: a poss child of "name" (mother/son/wife/…)
            role = next((c for c in tok.children
                         if c.dep_ == "poss" and c.pos_ in ("NOUN", "PROPN")), None)
            if role is None:
                continue
            role_lemma = (role.lemma_ or role.text or "").strip().lower()
            # the PROPER NAME assigned — the copula's attr/attr-like complement (PROPN or a NOUN the
            # parser mis-tagged, e.g. "Robin" → NOUN). Exclude a wh/interrogative complement.
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
            possessor = None
            poss_tok = next((c for c in role.children if c.dep_ == "poss"), None)
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
            # (person, kin, possessor) — e.g. (robin, parent_of, user). subj_tok=proper so the named
            # person is the entity; verb_tok=None (no date on a name/role edge). The proper name BECOMES
            # the entity's surface here (the deriver works lowercased, so the alias is the subject
            # surface itself) — the EntityRegistry registers "robin" as the entity's also_known_as alias
            # when it grounds this edge at ingest. A separate (robin, also_known_as, robin) self-edge
            # would be degenerate (subj==obj, rejected by _emit) and is unnecessary: the NAME is filed
            # via the subject surface, never classified into L4 (THE HARD LINE preserved).
            _emit(proper_name, _kin, possessor, obj_tok=None, subj_tok=proper)
            # ROLE-ALIAS leg (Fix B, Part 2 — flag-gated): register the ROLE surface (mother/son/wife)
            # as an ``also_known_as`` alias of the NAMED person so a later SPLIT atom ("My mother is
            # 62" — the reframe Root-1 split) resolves mother→robin and the scalar lands on the named
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
            # "X's name is <Name>" copula (the attr complement — spaCy sometimes tags "Robin" NOUN).
            best = None
            for _t in doc:
                if _t.i >= tok.i:
                    break
                if _t.dep_ == "case":
                    continue
                if _t.pos_ == "PROPN":
                    best = (_t.text or "").strip().lower()
                    continue
                # a NOUN that is the attr of a naming copula ("name is Robin") is the bound person name
                if _t.pos_ == "NOUN" and _t.dep_ in ("attr", "oprd"):
                    _h = _t.head
                    if (_h is not None and _h.lemma_ == "be" and _h.pos_ == "AUX"
                            and any((_c.lemma_ or "").strip().lower() == "name"
                                    and _c.dep_ in ("nsubj", "nsubjpass") for _c in _h.children)):
                        best = (_t.text or "").strip().lower()
            if best:
                return best
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
            # SUBJECT: a 1st-person self is owned by the self/feeling seam — never a 3rd-party scalar
            # here (kept parity with the copula-state chain). "I am 40" still routes via the identity/
            # self path; this chain handles a NON-self measured subject.
            if _is_first_person_personal_pronoun(tok):
                continue
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
                        rel = "age"
                        value = (c.text or "").strip()
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
            if not _scalar_rel_admits_subject(rel, _subj_et):
                continue  # leave the entity to its relational has_state capture (additive, not dead-C)
            # SCALAR emit: object is the STRING value; verb_tok=head so a date could bind (rare);
            # obj_tok=num_tok claims the number span. The rel carries tail_types={SCALAR} downstream so
            # the value lands in entity_attributes, never resolved to a UUID.
            _emit(subject, rel, value, verb_tok=head, obj_tok=num_tok, subj_tok=tok)
            _claim(tok)

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

    def _chain_appositive(doc):
        # APPOSITIVE → has_role. "Rachel, a real estate agent" → (rachel, has_role, real estate agent).
        # COMMON-noun role only (NOUN appos head); a PROPN appositive is an alias, not a role.
        for tok in doc:
            if tok.dep_ != "appos":
                continue
            head = tok.head
            if head is None:
                continue
            if tok.pos_ != "NOUN":
                continue
            role = _np_phrase(tok)
            named = (head.text or head.lemma_ or "").strip().lower()
            if not role or not named or role == named or len(role) < 2:
                continue
            _emit(named, "has_role", role, obj_tok=tok, subj_tok=head)

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
        (_chain_dash_specifier,) if dash_specifier_only else
        (_chain_svo, _chain_intransitive, _chain_copula_state,
         _chain_possessive, _chain_genitive_name, _chain_copula_name,
         _chain_copula_measure, _chain_dash_specifier, _chain_appositive)
    )

    try:
        for _chain in _chains:
            try:
                _chain(doc)
            except Exception as _ce:  # noqa: BLE001 — one chain failing never sinks the others
                log.warning("linguistics.derive_chain_failed",
                            chain=getattr(_chain, "__name__", "?"), error=str(_ce)[:160])

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
            if tok.dep_ == "neg" and (
                tok.head.dep_ in ("appos", "conj")
                or tok.head.pos_ in ("NOUN", "PROPN", "NUM")
            ):
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
