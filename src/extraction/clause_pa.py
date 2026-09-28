"""Deterministic clause-typed predicate–argument (PA) core — Phase 2, increment 1.

WHAT THIS IS
============
A GENERIC, deterministic predicate–argument extractor over the spaCy dependency parse. It reads
EVERY clause in a sentence, identifies the clause's TYPE from a **closed 5-way typology**, and emits
a normalized ``Proposition`` per clause off that type's fixed argument grammar. It is the intended
"generic capture floor" that Phase 2 will eventually place UNDERNEATH the ~38 hand-written chains in
``derive_sentence_facts`` (see the internal design record): today the spine
captures a construction *iff someone wrote a chain for it*; this module captures by GRAMMAR, so a
construction with no bespoke chain still yields a correct generic proposition instead of silently
under-capturing.

THIS INCREMENT DOES NOT WIRE INTO INGEST. It is the extractor + a coverage-comparison harness only.
The live capture path (``derive_sentence_facts``) is UNCHANGED and UNREAD by ingest here. Wiring is a
later, benchmark-gated increment (``SPINE_PA_CORE`` gates the *future* call site; the module + harness
stand alone and are always callable for measurement/tests).

DETERMINISM / FIREWALL (hard constraints)
=========================================
- **Structure decision is 100% deterministic grammar** — spaCy Universal-Dependencies parse ONLY.
  NO neural model, NO LLM, NO GLiNER2, NO cosine anywhere in this module. Typing stays the deriver's
  job downstream; the PA core decides STRUCTURE, never entity type or rel_type.
- **Subject-agnostic** — no domain vocabulary, no rel_type literals, no entity-name lists in the
  grammar. The only closed sets consulted are UNIVERSAL grammatical primitives (dependency labels,
  the copula lemma "be", coordinating punctuation).
- **Fail-safe / fail-loud, never fabricate** — a clause the typology cannot type emits NOTHING for
  that clause and is recorded in ``PAResult.uncovered`` (this feeds the future verb-inclusive
  positive coverage contract, assessment Q2). We never invent an argument to force a type.

UD SUBSTRATE NOTE (load-bearing)
================================
The SOTA refinement (the internal design record Q3) says to express the ClausIE
5-way typology in **Universal Dependencies** terms rather than Stanford/ClearNLP labels. spaCy's
English pipelines (``en_core_web_sm/md/lg``) actually emit **ClearNLP-style** labels by default
(``dobj``/``dative``/``pobj``-under-``prep``/``attr``/``acomp``/``oprd``/``nsubjpass``), NOT the pure
UD inventory (``obj``/``iobj``/``obl``/``cop``/``xcomp``). We therefore NORMALIZE spaCy's labels to
UD roles at the parse boundary (``_UD_ROLE`` / ``_norm_role``) and express the whole typology and the
emitted ``Argument.role`` values in UD terms (``nsubj``/``obj``/``iobj``/``obl``/``cop_comp``/
``xcomp``/``nmod``/``appos``). This inherits UD's multilingual substrate (an i18n future-proofing
win) and keeps the module's contract parser-scheme-agnostic (a genuinely UD-labelled model drops in
unchanged).

CLOSED 5-WAY CLAUSE TYPOLOGY (ClausIE, expressed in UD)
=======================================================
Every content clause is exactly one of (Del Corro & Gemulla, WWW 2013 — English valency, Quirk et al.):
  (a) intransitive         SV        — nsubj, no obj / no copula complement.
  (b) copular              S cop C   — "be"/become/seem + predicate nominal (``attr``) or adjectival
                                        (``acomp``); complement role = ``cop_comp``.
  (c) monotransitive       SVO       — nsubj + obj, no iobj, no object-complement.
  (d) ditransitive         SVOiOd    — nsubj + obj + iobj (spaCy ``dative``) OR obj + a ``to``/``for``
                                        dative-alternation PP whose pobj is the recipient (→ iobj).
  (e) complex-transitive   SVOC      — nsubj + obj + object-complement (``oprd``, or a small-clause
                                        ``ccomp`` head that carries its OWN nsubj = the object).

MinIE-STYLE MINIMIZATION (Gashteovski et al., EMNLP 2017)
=========================================================
Over-specific argument phrases are FACTORED, not swallowed into one giant tuple: a ``nmod`` PP on an
argument head ("the malfunction **of the stand mixer**") and an appositive ("my brother**, a
doctor**") are each emitted as a SEPARATE, attached ``nominal_modifier`` proposition, and argument
surfaces are MINIMIZED to head + essential modifiers (compound/amod/nummod/poss), dropping determiners
and detached subtrees. This is the deterministic "trim overly-specific phrases" idea from MinIE, and
it is precisely where the generic core out-captures the chains on nominalizations.

COORDINATION & CLAUSAL COMPLEMENTS
==================================
Conjoined clauses (``conj`` verbs, subject inherited when elided), conjoined arguments (obj/subject
``conj`` chains → one proposition per conjunct, bounded cartesian expansion), and clausal complements
(``xcomp`` control → subject inherited; ``ccomp`` with its own subject → recursed as an independent
clause) are all decomposed, so meaning that spans a clause boundary is not lost to a flat single tuple.

EVIDENCE / SOURCES
==================
- Del Corro & Gemulla, *ClausIE: Clause-Based Open Information Extraction*, WWW 2013 —
  https://resources.mpi-inf.mpg.de/d5/clausie/clausie-www13.pdf  (closed 5-way clause typology;
  dependency-parse → propositions; domain-independent, no training data).
- Gashteovski, Gemulla & Del Corro, *MinIE: Minimizing Facts in Open IE*, EMNLP 2017 —
  https://aclanthology.org/D17-1278/  (minimization of overly-specific relation/argument phrases).
- de Marneffe, Manning, Nivre & Zeman, *Universal Dependencies*, Comp. Linguistics 2021 —
  https://universaldependencies.org/ ; UD "Simple Clauses" —
  https://universaldependencies.org/u/overview/simple-syntax.html  (closed clause inventory:
  predicate + core args nsubj/obj/iobj + obliques obl/nmod; copula ``cop``; open complement ``xcomp``).
- Palmer, Gildea & Kingsbury, *The Proposition Bank*, Comp. Linguistics 2005 —
  https://aclanthology.org/J05-1004.pdf  (predicate-argument completeness = the future coverage
  contract this module's ``uncovered`` list feeds).
- Ma et al., *Efficient Knowledge Graph Construction and Retrieval from Unstructured Text*,
  arXiv 2507.03226 (2025) — https://arxiv.org/html/2507.03226v2  (spaCy dependency-parse KG retains
  ~94% of an LLM pipeline's quality at ~10× lower cost, CPU-friendly — the cost/determinism
  justification for a deterministic dependency PA core on FaultLine's CPU-only host).

See also the internal design record for the design record and the staged-migration plan.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

try:  # structlog is present in the app; degrade to a no-op logger for standalone/harness use.
    import structlog  # type: ignore

    _log = structlog.get_logger(__name__)
except Exception:  # noqa: BLE001 — the module must import with zero app deps for the harness.
    class _NoOpLog:
        def __getattr__(self, _name):
            def _noop(*_a, **_k):
                return None
            return _noop

    _log = _NoOpLog()


# ── FLAG (guards the FUTURE ingest call site only; the module itself is always callable) ──────────
# This increment does NOT wire into ingest, so nothing reads this at runtime yet. It exists so the
# later benchmark-gated wiring increment has one env switch, matching the SENTENCE_PIPELINE discipline
# (default OFF; a public deployment behaves exactly as today until it is deliberately flipped).
def pa_core_enabled() -> bool:
    """True iff ``SPINE_PA_CORE`` is set truthy. Default OFF. (Not consulted in increment 1.)"""
    return os.environ.get("SPINE_PA_CORE", "").strip().lower() in ("1", "true", "yes", "on")


def _coord_pp_descent_enabled() -> bool:
    """True iff ``SPINE_COORD_PP_DESCENT`` is truthy — call-time env read, DEFAULT ON.

    Gates the COORDINATED-PP-OBJECT / pseudo-partitive descent so BOTH conjuncts of a coordinated
    prepositional object are captured as their own typed argument. Two Universal-Dependencies-grounded
    completions, both additive and purely structural (no domain/measure word list):

      • ``conj`` COMPLETION on a prepositional-object pobj. spaCy chains a coordinated nominal list off
        the FIRST conjunct via the ``conj`` relation ("with orange AND lemon" → orange →conj lemon;
        "slices of orange, lemon AND lime" → orange →conj lemon →conj lime). We emit one oblique / nmod
        argument PER conjunct instead of only the head. This is UD DISTRIBUTIVE COORDINATION — the
        coordinator distributes the governing preposition over every conjunct ("served with orange
        [slices] and [with] lemon [slices]"). Cf. de Marneffe & Manning, *Stanford typed dependencies*
        (``conj``/``cc``); Universal Dependencies v2 coordination (first conjunct = head, later
        conjuncts attach via ``conj``). A NON-coordinate pobj yields exactly ``[pobj]`` → NO split
        (the over-split guard: nothing fires without a real ``conj`` edge).

      • MinIE nmod FACTORING extended to OBLIQUE argument heads (previously only core subj/obj/iobj). A
        pseudo-partitive / measure NP inside an oblique ("with slices **of orange and lemon**") carries
        its semantic content in the ``of`` complement, which the subject/object-only factoring never
        reached, so those content nouns dropped whole. Factoring EVERY argument head's PP ``nmod`` into
        its own attached proposition is the SAME MinIE minimization this module already cites (Del Corro
        & Gemulla, *ClausIE*, WWW 2013; Gashteovski et al., *MinIE*, EMNLP 2017) — applied symmetrically.

    OFF → byte-identical to the prior obl/nmod behaviour."""
    return os.environ.get(
        "SPINE_COORD_PP_DESCENT", "true").strip().lower() in ("1", "true", "yes", "on")


# ── UD ROLE NORMALIZATION (spaCy ClearNLP labels → UD role names) ─────────────────────────────────
# The typology + emitted roles are expressed in UD terms; spaCy en_core_web_sm emits ClearNLP labels.
# We map at the boundary so a genuinely UD-labelled parse drops in unchanged (both keys are handled).
_UD_ROLE: dict[str, str] = {
    # core subject
    "nsubj": "nsubj",
    "nsubjpass": "nsubj",   # passive patient-subject; passive flagged separately
    "csubj": "nsubj",
    "csubjpass": "nsubj",
    # core objects
    "dobj": "obj",          # ClearNLP direct object
    "obj": "obj",           # UD direct object
    "dative": "iobj",       # ClearNLP indirect object
    "iobj": "iobj",         # UD indirect object
    # copular / object complements
    "attr": "cop_comp",     # ClearNLP predicate nominal ("is a teacher")
    "acomp": "cop_comp",    # ClearNLP predicate adjectival ("is happy")
    "oprd": "xcomp",        # ClearNLP object predicate ("painted it red")
}

_COPULA_LEMMAS = frozenset({"be", "become", "seem", "remain", "stay", "turn", "grow", "get", "appear"})
_DATIVE_MARKERS = frozenset({"to", "for"})  # the ONLY closed lexical set — the dative alternation
# NP-internal modifiers kept when MINIMIZING an argument surface (head + these). Determiners and any
# detached subtree (relcl/acl/appos/prep/cc/conj) are deliberately EXCLUDED (MinIE minimization).
_NP_KEEP_DEPS = frozenset({"compound", "amod", "nummod", "poss", "nmod:poss", "quantmod", "nmod"})
# Dependency labels that head a distinct clause (recurse / iterate as its own predicate).
_CLAUSAL_DEPS = frozenset({"ROOT", "conj", "ccomp", "xcomp", "advcl", "relcl", "acl", "parataxis"})

# Bound coordination expansion so a pathological conjunction chain cannot explode the output.
_MAX_PROPOSITIONS_PER_CLAUSE = 32
# Bound the CHAINED nmod factoring so a deep partitive/possessive PP chain cannot recurse unboundedly
# ("the X of the Y of the Z …"). 2 = handles "pitcher with slices of orange" (obl-head → with → of).
_MAX_FACTOR_DEPTH = 2


def _norm_role(tok) -> str | None:
    """UD role for a token given its spaCy dep, or ``None`` if it is not a PA role we consume."""
    return _UD_ROLE.get(tok.dep_)


# ── DATACLASSES ──────────────────────────────────────────────────────────────────────────────────
@dataclass
class Argument:
    """One filled argument slot of a proposition, UD-role-grounded and deterministically minimized."""
    role: str                    # UD role: nsubj|obj|iobj|obl|cop_comp|xcomp|nmod|appos
    text: str                    # MINIMIZED argument surface (head + essential mods), lowercased
    head_i: int                  # token index of the argument head in the source Doc
    case: str | None = None      # preposition/marker for obl/nmod args ("at"/"for"/"of"), else None


@dataclass
class Proposition:
    """One clause-typed predicate–argument proposition read off a single clause of the parse."""
    clause_type: str             # intransitive|copular|monotransitive|ditransitive|
    #                              complex_transitive|nominal_modifier
    subject: str | None          # minimized subject surface (lowercased), or None (passive w/o agent)
    predicate: str               # verb lemma[+particle], "be" for copular, or the case/appos relation
    args: list[Argument] = field(default_factory=list)
    negated: bool = False        # spaCy ``neg`` on the predicate (ConText/NegEx assertion polarity)
    passive: bool = False        # clause was passive-voiced (nsubjpass); subject may be the agent
    source_span: str = ""        # the clause's surface span (for audit / residue matching)
    predicate_i: int = -1        # token index of the predicate head
    subject_i: int = -1          # token index of the SUBJECT head (agent after passive-normalization),
    #                              or -1 (agentless passive / no subject). Additive (increment 2 wiring):
    #                              lets the ingest bridge apply the SHARED refinement helpers
    #                              (first-person I/my→user, it/they coref) on the SAME token the chains
    #                              use — never a pronoun surface string. No structural role in the PA
    #                              extractor itself; default -1 keeps every increment-1 test unchanged.


@dataclass
class PAResult:
    """The full PA read of one sentence: typed propositions + the clauses we could not type."""
    propositions: list[Proposition] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)   # untypable clause spans (fail-safe record)


# ── SURFACE HELPERS ──────────────────────────────────────────────────────────────────────────────
def _minimal_np(head) -> str:
    """MinIE-minimized surface for an argument head: head + directly-attached essential NP modifiers
    (compound/amod/nummod/poss), determiners and detached subtrees dropped. Token order preserved."""
    try:
        toks = [head]
        for c in head.children:
            if c.dep_ in _NP_KEEP_DEPS and c.pos_ != "PUNCT":
                # A nmod child that is itself a PP ("of the mixer") is FACTORED separately, not kept
                # inline — only a bare nominal modifier (rare in ClearNLP) is folded here.
                if c.dep_ == "nmod" and any(gc.dep_ in ("prep", "case") for gc in c.children):
                    continue
                toks.append(c)
        toks = sorted(set(toks), key=lambda t: t.i)
        return " ".join(t.text for t in toks).strip().lower()
    except Exception:  # noqa: BLE001 — fail-safe to the bare head surface
        return (head.text or "").strip().lower()


def _predicate_string(verb) -> str:
    """Verb lemma plus a load-bearing phrasal particle (``prt``), snake-joined ("give_up"). The
    predicate is the user's own verb — NO rel_type decision is made here (that is downstream)."""
    try:
        lemma = (verb.lemma_ or verb.text or "").strip().lower()
        prt = next((c.text.strip().lower() for c in verb.children if c.dep_ == "prt"), None)
        return f"{lemma}_{prt}" if prt else lemma
    except Exception:  # noqa: BLE001
        return (getattr(verb, "text", "") or "").strip().lower()


def _conjuncts(head) -> list:
    """[head] followed by its ``conj`` chain — the coordinated siblings of an argument/subject."""
    out = [head]
    try:
        for c in head.children:
            if c.dep_ == "conj":
                out.extend(_conjuncts(c))
    except Exception:  # noqa: BLE001
        pass
    return out


def _has_neg(*toks) -> bool:
    """True if any of the given predicate/aux tokens carries a spaCy ``neg`` dependency child."""
    for t in toks:
        if t is None:
            continue
        try:
            if any(c.dep_ == "neg" for c in t.children):
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def _clause_span(head) -> str:
    """Surface span of the clause rooted at ``head`` (its subtree), for audit/residue."""
    try:
        toks = sorted(head.subtree, key=lambda t: t.i)
        return "".join(t.text_with_ws for t in toks).strip()
    except Exception:  # noqa: BLE001
        return (getattr(head, "text", "") or "").strip()


# ── ARGUMENT COLLECTION ──────────────────────────────────────────────────────────────────────────
def _collect_obliques(verb) -> list[Argument]:
    """Every prepositional oblique on the verb → an ``obl`` Argument with ``case`` = the preposition.
    ("worked **at Acme** **for five years**" → obl[at]=Acme, obl[for]=five years.)"""
    out: list[Argument] = []
    _descend = _coord_pp_descent_enabled()
    try:
        for prep in verb.children:
            if prep.dep_ not in ("prep", "agent"):
                continue
            for pobj in prep.children:
                if pobj.dep_ in ("pobj", "obj") and pobj.pos_ not in ("PUNCT",):
                    # UD coordination completion: a coordinated PP-object ("with orange AND lemon")
                    # hangs its tail conjuncts off the FIRST pobj via ``conj`` — emit one oblique per
                    # conjunct so BOTH are captured (distributive coordination). A non-coordinate pobj
                    # → ``[pobj]`` (no split). Bounded so a pathological chain cannot explode.
                    _pheads = _conjuncts(pobj) if _descend else [pobj]
                    for _ph in _pheads[:_MAX_PROPOSITIONS_PER_CLAUSE]:
                        if _ph.pos_ in ("PUNCT",):
                            continue
                        out.append(Argument(role="obl", text=_minimal_np(_ph),
                                            head_i=_ph.i, case=(prep.text or "").strip().lower()))
    except Exception:  # noqa: BLE001
        pass
    return out


def _factor_modifiers(arg_head, doc, _depth: int = 0, _seen: set | None = None) -> list[Proposition]:
    """MinIE factoring: emit a separate ``nominal_modifier`` proposition for each PP ``nmod``/``of``
    modifier and each appositive on an argument head, instead of swallowing it into the argument.

    With ``SPINE_COORD_PP_DESCENT`` ON (default), the PP-complement is COORDINATION-complete and the
    factoring is CHAINED (bounded): a coordinated complement ("slices of orange AND lemon") yields one
    proposition per ``conj`` conjunct, and a NESTED partitive/possessive chain ("… a pitcher with
    slices of orange and lemon" → pitcher→with→slices→of→orange/lemon) is descended so the deep content
    nouns are reached — the same MinIE minimization applied transitively down the PP-modifier chain.
    OFF → single-level NOUN/PROPN factoring, byte-identical to before."""
    props: list[Proposition] = []
    _descend = _coord_pp_descent_enabled()
    if _seen is None:
        _seen = set()
    if arg_head.i in _seen:
        return props            # cycle / re-visit guard (a token is factored at most once)
    _seen.add(arg_head.i)
    # A mistagged coordinated/partitive complement head — spaCy tags a colour/OOV homograph noun as
    # ``ADJ`` ("of ORANGE, lemon and lime" → orange=ADJ) — is still the nominal content of the ``of``
    # complement. Admit ADJ ONLY on the descent path (structural: it heads a ``pobj``), never OFF.
    _pobj_pos = ("NOUN", "PROPN", "ADJ") if _descend else ("NOUN", "PROPN")
    try:
        for c in arg_head.children:
            # PP nominal modifier on a noun: "malfunction **of the stand mixer**" → (malfunction, of, stand mixer)
            if c.dep_ == "prep":
                for pobj in c.children:
                    if pobj.dep_ in ("pobj", "obj") and pobj.pos_ in _pobj_pos:
                        # UD conj completion on the PP-complement ("slices of orange AND lemon" →
                        # orange →conj lemon): one nmod proposition per conjunct so BOTH content nouns
                        # of a coordinated pseudo-partitive are captured. Non-coordinate → ``[pobj]``.
                        _pheads = _conjuncts(pobj) if _descend else [pobj]
                        for _ph in _pheads[:_MAX_PROPOSITIONS_PER_CLAUSE]:
                            if _ph.pos_ not in _pobj_pos:
                                continue
                            props.append(Proposition(
                                clause_type="nominal_modifier",
                                subject=_minimal_np(arg_head),
                                predicate=(c.text or "").strip().lower(),
                                args=[Argument(role="nmod", text=_minimal_np(_ph),
                                               head_i=_ph.i, case=(c.text or "").strip().lower())],
                                source_span=_clause_span(arg_head),
                                predicate_i=c.i, subject_i=arg_head.i))
                            # CHAINED descent: factor the complement's OWN nested PP modifiers so a
                            # multi-level partitive ("pitcher with slices of orange and lemon") reaches
                            # the deep content nouns. Bounded by depth + the per-clause proposition cap.
                            if (_descend and _depth < _MAX_FACTOR_DEPTH
                                    and len(props) < _MAX_PROPOSITIONS_PER_CLAUSE):
                                props.extend(_factor_modifiers(_ph, doc, _depth + 1, _seen))
            # Appositive: "my brother**, a doctor**" → (brother, appos, doctor)
            elif c.dep_ == "appos" and c.pos_ in ("NOUN", "PROPN"):
                props.append(Proposition(
                    clause_type="nominal_modifier",
                    subject=_minimal_np(arg_head),
                    predicate="appos",
                    args=[Argument(role="appos", text=_minimal_np(c), head_i=c.i)],
                    source_span=_clause_span(arg_head),
                    predicate_i=c.i, subject_i=arg_head.i))
    except Exception:  # noqa: BLE001
        pass
    return props


def _resolve_subject(verb):
    """The subject token of ``verb``: its own nsubj, or (subject-elided coordination / xcomp control)
    the subject of the governing head. Returns a token or ``None``."""
    try:
        s = next((c for c in verb.children if c.dep_ in ("nsubj", "nsubjpass", "csubj")), None)
        if s is not None:
            return s
        # Elided subject in coordination ("I sang and danced") or xcomp control ("want to leave"):
        # climb to the controlling head and borrow ITS subject.
        if verb.dep_ in ("conj", "xcomp", "advcl") and verb.head is not verb:
            return _resolve_subject(verb.head)
    except Exception:  # noqa: BLE001
        pass
    return None


# ── CLAUSE CLASSIFICATION (the closed 5-way typology) ─────────────────────────────────────────────
def _build_clause(verb, doc) -> tuple[list[Proposition], str | None]:
    """Classify the clause headed by ``verb`` and build its proposition(s).

    Returns ``(propositions, uncovered_span_or_None)``. An untypable clause returns ``([], span)`` so
    the caller records it — we NEVER fabricate an argument to force a type."""
    subj_tok = _resolve_subject(verb)

    # Core dependents, normalized to UD roles.
    obj_tok = iobj_tok = cop_tok = objcomp_tok = None
    for c in verb.children:
        role = _norm_role(c)
        if role == "obj" and obj_tok is None:
            obj_tok = c
        elif role == "iobj" and iobj_tok is None:
            iobj_tok = c
        elif role == "cop_comp" and cop_tok is None:
            cop_tok = c
        elif role == "xcomp" and objcomp_tok is None and c.pos_ in ("NOUN", "PROPN", "ADJ"):
            objcomp_tok = c   # oprd object-predicate

    # Small-clause complex-transitive: verb →ccomp(head noun/adj) whose OWN nsubj is the object.
    # ("consider **him** **a friend**", "made **her** **the president**".)
    sc_obj = sc_comp = None
    if obj_tok is None:
        for c in verb.children:
            if c.dep_ == "ccomp" and c.pos_ in ("NOUN", "PROPN", "ADJ"):
                inner_subj = next((g for g in c.children if g.dep_ in ("nsubj", "nsubjpass")), None)
                if inner_subj is not None:
                    sc_obj, sc_comp = inner_subj, c
                    break

    negated = _has_neg(verb, *[c for c in verb.children if c.dep_ in ("aux", "auxpass")])
    passive = any(c.dep_ in ("nsubjpass", "auxpass") for c in verb.children)
    obliques = _collect_obliques(verb)
    pred = _predicate_string(verb)
    span = _clause_span(verb)

    # Dative alternation: obj + a to/for PP whose pobj is the recipient → promote that obl to iobj.
    if obj_tok is not None and iobj_tok is None:
        for a in list(obliques):
            if a.case in _DATIVE_MARKERS:
                iobj_tok = doc[a.head_i]
                obliques.remove(a)
                break

    is_copular = (verb.lemma_ or "").strip().lower() in _COPULA_LEMMAS and cop_tok is not None

    # ── classify ──
    base: Proposition | None = None
    if is_copular:
        # (b) COPULAR  S cop C
        if subj_tok is None:
            return [], span
        base = Proposition(clause_type="copular", subject=None, predicate="be", negated=negated,
                            passive=passive, source_span=span, predicate_i=verb.i,
                            args=[Argument(role="cop_comp", text=_minimal_np(cop_tok), head_i=cop_tok.i)])
    elif (objcomp_tok is not None and obj_tok is not None) or sc_obj is not None:
        # (e) COMPLEX-TRANSITIVE  SVOC
        o = obj_tok if obj_tok is not None else sc_obj
        c = objcomp_tok if objcomp_tok is not None else sc_comp
        if subj_tok is None:
            return [], span
        base = Proposition(clause_type="complex_transitive", subject=None, predicate=pred,
                           negated=negated, passive=passive, source_span=span, predicate_i=verb.i,
                           args=[Argument(role="obj", text=_minimal_np(o), head_i=o.i),
                                 Argument(role="xcomp", text=_minimal_np(c), head_i=c.i)])
    elif obj_tok is not None and iobj_tok is not None:
        # (d) DITRANSITIVE  SVOiOd
        if subj_tok is None:
            return [], span
        base = Proposition(clause_type="ditransitive", subject=None, predicate=pred, negated=negated,
                           passive=passive, source_span=span, predicate_i=verb.i,
                           args=[Argument(role="obj", text=_minimal_np(obj_tok), head_i=obj_tok.i),
                                 Argument(role="iobj", text=_minimal_np(iobj_tok), head_i=iobj_tok.i)])
    elif obj_tok is not None:
        # (c) MONOTRANSITIVE  SVO
        if subj_tok is None and not passive:
            return [], span
        base = Proposition(clause_type="monotransitive", subject=None, predicate=pred, negated=negated,
                           passive=passive, source_span=span, predicate_i=verb.i,
                           args=[Argument(role="obj", text=_minimal_np(obj_tok), head_i=obj_tok.i)])
    elif subj_tok is not None:
        # (a) INTRANSITIVE  SV  (an obligue-only clause is still an intransitive predication)
        base = Proposition(clause_type="intransitive", subject=None, predicate=pred, negated=negated,
                           passive=passive, source_span=span, predicate_i=verb.i, args=[])
    else:
        # No subject and no object we can anchor to → untypable. Record, never fabricate.
        return [], span

    base.args.extend(obliques)

    # PASSIVE NORMALIZATION (ClausIE): "the mixer was broken **by the surge**" → agent = subject,
    # patient (nsubjpass) = object. The agent rides as an ``agent`` prep/obl above; promote it.
    if passive:
        agent = next((a for a in base.args if a.case == "by"), None)
        patient = subj_tok
        if agent is not None:
            base.subject = agent.text
            base.subject_i = agent.head_i
            base.args = [a for a in base.args if a is not agent]
            if patient is not None:
                base.args.insert(0, Argument(role="obj", text=_minimal_np(patient), head_i=patient.i))
                # Passive normalized to active with an agent + patient reads as a transitive predication.
                if base.clause_type == "intransitive":
                    base.clause_type = "monotransitive"
        else:
            base.subject = _minimal_np(patient) if patient is not None else None
            base.subject_i = patient.i if patient is not None else -1
    else:
        base.subject = _minimal_np(subj_tok) if subj_tok is not None else None
        base.subject_i = subj_tok.i if subj_tok is not None else -1

    # ── COORDINATION EXPANSION: split conjoined subject and object heads into distinct propositions ──
    props = _expand_coordination(base, subj_tok, obj_tok, doc)

    # ── MinIE factoring on the argument heads (nmod PP / appositive → separate propositions) ──
    # Core args always; OBLIQUE heads too when the coordinated-PP / pseudo-partitive descent is on, so
    # a measure/partitive NP inside an oblique ("with slices **of orange and lemon**") has its ``of``
    # complement factored — the content the subject/object-only pass never reached. Deduped below.
    _factor_heads = [subj_tok, obj_tok, iobj_tok]
    if _coord_pp_descent_enabled():
        for _a in obliques:
            _oh = doc[_a.head_i] if (isinstance(_a.head_i, int) and 0 <= _a.head_i < len(doc)) else None
            if _oh is not None:
                _factor_heads.append(_oh)
    factored: list[Proposition] = []
    _seen_factor_heads: set = set()
    for h in _factor_heads:
        if h is not None and h.i not in _seen_factor_heads:
            _seen_factor_heads.add(h.i)
            factored.extend(_factor_modifiers(h, doc))
    # de-dup factored by (subject, predicate, arg text)
    seen: set = set()
    uniq_factored = []
    for p in factored:
        k = (p.subject, p.predicate, tuple((a.role, a.text) for a in p.args))
        if k not in seen:
            seen.add(k)
            uniq_factored.append(p)

    return props + uniq_factored, None


def _expand_coordination(base: Proposition, subj_tok, obj_tok, doc) -> list[Proposition]:
    """Redistribute a coordinated subject and/or object into one proposition per conjunct (bounded
    cartesian). "I like apples and oranges" → 2 props; "Sam and Kate left" → 2 props. Passive/None
    subjects and non-obj clause types pass through with only the subject expanded."""
    subj_conj = _conjuncts(subj_tok) if subj_tok is not None and not base.passive else [None]
    obj_arg_idx = next((i for i, a in enumerate(base.args) if a.role == "obj"), None)
    obj_conj = _conjuncts(obj_tok) if (obj_tok is not None and obj_arg_idx is not None) else [None]

    if subj_conj == [None] and obj_conj == [None]:
        return [base]

    out: list[Proposition] = []
    for s in (subj_conj or [None]):
        for o in (obj_conj or [None]):
            p = Proposition(clause_type=base.clause_type,
                            subject=(_minimal_np(s) if s is not None else base.subject),
                            predicate=base.predicate, negated=base.negated, passive=base.passive,
                            source_span=base.source_span, predicate_i=base.predicate_i,
                            subject_i=(s.i if s is not None else base.subject_i),
                            args=[Argument(a.role, a.text, a.head_i, a.case) for a in base.args])
            if o is not None and obj_arg_idx is not None:
                p.args[obj_arg_idx] = Argument("obj", _minimal_np(o), o.i)
            out.append(p)
            if len(out) >= _MAX_PROPOSITIONS_PER_CLAUSE:
                return out
    return out


# ── PUBLIC ENTRYPOINT ────────────────────────────────────────────────────────────────────────────
def _load_nlp():
    """Reuse the deriver's parser-only spaCy singleton (SAME ``SPACY_MODEL``); fall back to a local
    load so the module is standalone-importable for the harness/tests. Returns an nlp or ``None``."""
    try:
        from src.extraction.linguistics import _get_nlp  # type: ignore

        nlp = _get_nlp()
        if nlp is not None:
            return nlp
    except Exception:  # noqa: BLE001 — standalone fallback below
        pass
    try:
        import spacy  # local import keeps the module dep-light until first parse

        model = (os.environ.get("SPACY_MODEL") or os.environ.get("LINGUISTIC_SPACY_MODEL")
                 or "en_core_web_sm")
        return spacy.load(model, disable=["ner"])
    except Exception as e:  # noqa: BLE001
        _log.warning("clause_pa.model_load_failed", error=str(e)[:160])
        return None


def extract_propositions(sentence, nlp=None) -> PAResult:
    """Extract clause-typed predicate–argument propositions from ONE sentence.

    ``sentence`` may be a ``str`` (parsed internally) OR an already-parsed spaCy ``Doc``. Deterministic,
    subject-agnostic, grammar-only. Fail-safe: unavailable parser / empty input / any failure → an
    empty ``PAResult`` (never raises to the caller). An untypable clause contributes to
    ``PAResult.uncovered`` and emits no proposition (never fabricated)."""
    result = PAResult()
    if sentence is None:
        return result
    doc = None
    try:
        if isinstance(sentence, str):
            if not sentence.strip():
                return result
            nlp = nlp or _load_nlp()
            if nlp is None:
                return result
            doc = nlp(sentence)
        else:
            doc = sentence  # already a parsed Doc
    except Exception as e:  # noqa: BLE001 — fail-safe
        _log.warning("clause_pa.parse_failed", error=str(e)[:160])
        return result
    if doc is None:
        return result

    # Enumerate every clause head: a VERB/AUX that heads a clause (ROOT / conj / ccomp / xcomp / advcl
    # / relcl / acl / parataxis). Copular clauses are headed by the "be" AUX in spaCy, so AUX roots
    # with a cop complement are included. Deterministic order = token order.
    try:
        heads = []
        for t in doc:
            if t.pos_ in ("VERB", "AUX") and (t.dep_ in _CLAUSAL_DEPS or t.dep_ == "aux" and t.head is t):
                # skip a bare auxiliary that is not itself the clause head
                if t.dep_ == "aux":
                    continue
                heads.append(t)
        # An AUX ROOT copula ("is"/"was") has dep_ ROOT and pos_ AUX — already included above.
        for verb in heads:
            props, uncovered = _build_clause(verb, doc)
            if props:
                result.propositions.extend(props)
            if uncovered:
                result.uncovered.append(uncovered)
                _log.info("clause_pa.uncovered_clause", span=uncovered[:120])
    except Exception as e:  # noqa: BLE001 — fail-safe: partial results already collected are kept
        _log.warning("clause_pa.extract_failed", error=str(e)[:160])

    return result
