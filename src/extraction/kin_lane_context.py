"""Kin-cue lane context at the mint — FIX 1 (flag KIN_TYPE_AT_MINT, default ON).

THE WOUND (Q4, the blended-family smoke, measured live on deploy-wm3final2 2026-11-13)
======================================================================================
"Our daughter Wren started kindergarten this September." — the zero-shot labeler types the
name span ANIMAL (a bird-named child). The ingest TYPE STAMP then persists that turn-local
concrete label onto the fresh mint, and every LATER {Person}-tailed kin edge to the same
entity refuses on it — until the wound-map round-3 admission-side repair
(KIN_TYPE_FROM_LANE_CONTEXT) re-types the endpoint when the kin evidence is live. The
admission-side repair is a DANCE, not a fix: (a) the typing is fragile at the intro — it
survives only when the intro batch happens to carry a KIN-REL edge (the F1-R14 kin-copula
display-rung fires only inside the stamp of a kin-guard rel) and the display-case overlay
happens to carry the capitalized form; and (b) the gate consumes the CALLER-PASSED type
first, so the harvest's ANIMAL object_type keeps triggering the refusal-then-repair on
every later kin turn even when the registry row already says Person.

THE MINT-TIME FIX
=================
The raw turn is the last place the kin lane context exists ("Our daughter Wren", "My son
Teo", "the mother of Wren and Teo"). This module reads that turn ONCE per request — the
same retain-at-ingest doctrine as src/extraction/display_case.py — and returns a map
{lowercase surface -> kin cue noun} for every name span that sits in a KIN-DENOTING PHRASE:
a kinship cue noun (the seat's own kinship cue class) grammatically linked to the span:

  FRAME A (compound/appositive intro)   "our daughter Wren", "my son Teo"
  FRAME B (comma appositive after)      "Wren, our daughter,"
  FRAME C (genitive complement list)    "the mother of Wren and Teo",
                                        "the parents of Wren and Teo"

A daughter/son/child/mother-of complement is a PERSON by grammar, whatever a zero-shot
labeler scored — the same evidence-mirror as KIN_TYPE_FROM_LANE_CONTEXT, but AT THE MINT:
the ingest type stamp consults this map (``kin_lane_person_override``) and stamps Person
instead of the concrete non-Person label, and the gate-passed type is adjudicated to Person
in the same last miles, so the later kin edges admit WITHOUT the admission-side repair.

Deterministic: spaCy POS + token adjacency only. No name list, no capitalisation
heuristic, no LLM, no GLiNER2. The over-capture is benign BY GRAMMAR: any
"<kin role> <Name>" phrase names a person regardless of whose kin. Fragments that are NOT
kin-denoting ("started kindergarten") are never recorded — the span must be adjacent to
the cue (Frames A/B) or in the cue's genitive complement (Frame C, bounded window, no
verb crossing, stops at a second adposition).

A ContextVar (not a parameter) because the stamp sites sit deep inside main.py's edge
loop; one bind at the request boundary covers every reader — the same reasoning as
``display_case._display_forms``. MUST be reset by a token-based ``finally`` where the
binder owns one (direct unit callers), mirroring the display-case discipline.
"""

from __future__ import annotations

import contextvars

import structlog

log = structlog.get_logger()


# Per-request kin-lane overlay: {lowercase surface -> the kin cue noun that denoted it}.
_kin_lane_contexts: contextvars.ContextVar = contextvars.ContextVar(
    "faultline_kin_lane_contexts", default=None
)


def set_kin_lane_contexts(contexts: dict | None):
    """Bind the request's kin-lane overlay. Returns a token for ``reset_kin_lane_contexts``."""
    return _kin_lane_contexts.set(dict(contexts) if contexts else None)


def reset_kin_lane_contexts(token) -> None:
    """Unbind the overlay (always call in a ``finally``)."""
    try:
        _kin_lane_contexts.reset(token)
    except Exception:  # noqa: BLE001 — a stale token must never fail a request
        pass


def get_kin_lane_contexts() -> dict:
    """The current request's kin-lane overlay, or ``{}`` when nothing is bound."""
    return _kin_lane_contexts.get() or {}


# Concrete GLiNER/zero-shot labels that a kin-cue frame OVERRIDES at the stamp. A kin
# cue never overrides Person (already right) and never fabricates a type onto 'unknown'
# (nothing to repair — unknown-only stamps fill it honestly).
_OVERRIDABLE_CONCRETE_TYPES = frozenset({
    "animal", "object", "location", "organization", "concept", "event",
})


def kin_lane_person_override(surface: str, stamp_type: str | None) -> bool:
    """Pure decision: must the mint-time stamp for ``surface`` be Person?

    True iff the surface sits in the bound kin-lane overlay AND the stamp about to be
    written is a CONCRETE non-Person label (the zero-shot ANIMAL-class wound). A Person
    stamp is already correct; an empty/unknown stamp has nothing to repair. No I/O, no
    word list beyond the overlay itself — directly unit-testable."""
    if not surface or not stamp_type:
        return False
    key = str(surface).strip().lower()
    if not key or key not in get_kin_lane_contexts():
        return False
    return str(stamp_type).strip().lower() in _OVERRIDABLE_CONCRETE_TYPES


def _kin_cue_nouns_for_request() -> frozenset:
    """The kinship cue class for observation: the seed∪overlay class the kin gates
    already key on (linguistic_cue_overlay.resolve_kinship_nouns), fallen back to the
    closed code-default set when the overlay is unavailable. Deterministic; the class is
    language-level kin vocabulary, not user content."""
    try:
        from src.api.linguistic_cue_overlay import resolve_kinship_nouns
        import os
        nouns = resolve_kinship_nouns(os.getenv("POSTGRES_DSN", "") or "")
        if nouns:
            return frozenset(str(n or "").strip().lower() for n in nouns if n)
    except Exception:  # noqa: BLE001 — overlay unavailable → the code-default class
        pass
    return frozenset({
        "mother", "father", "daughter", "son", "child", "children",
        "wife", "husband", "brother", "sister", "parent", "parents",
    })


def observe_kin_lane_contexts(text: str) -> dict:
    """Derive ``{lowercase surface -> kin cue}`` from a VERBATIM user turn.

    Deterministic and subject-agnostic: spaCy POS + adjacency, the three kin-denoting
    frames above. Returns ``{}`` on any failure (spaCy absent, model not baked, parse
    error) — the layer degrades to today's typing rather than guessing.

    Frame C's complement window is BOUNDED (cue +1 'of', then up to 6 tokens), stops at a
    VERB/AUX (clause boundary) and at a SECOND adposition (a new PP is not the complement),
    and records only PROPN runs inside it — so "the mother of Wren and Teo" records both
    conjuncts while "the mother of Wren at school" never reaches 'school'.
    """
    if not text or not isinstance(text, str):
        return {}
    try:
        from src.extraction.linguistics import _get_nlp  # deferred: absent spaCy is a no-op
    except Exception:  # noqa: BLE001
        return {}
    nlp = None
    try:
        nlp = _get_nlp()
    except Exception:  # noqa: BLE001
        nlp = None
    if nlp is None:
        return {}
    try:
        doc = nlp(text)
    except Exception as e:  # noqa: BLE001 — a parse failure must never fail an ingest
        log.warning("kin_lane_context.parse_failed", error=str(e)[:160])
        return {}

    cues = _kin_cue_nouns_for_request()
    if not cues:
        return {}

    contexts: dict[str, str] = {}

    def _record_propn_run(tokens: list, cue: str) -> None:
        """Record a PROPN run (the whole run + each token — the pipeline may bind either)."""
        run = [t for t in tokens if t.pos_ == "PROPN"]
        if not run:
            return
        surface = "".join(t.text_with_ws for t in tokens).strip()
        # The map VALUE is the cue noun (diagnostic only — the override decision reads
        # MEMBERSHIP); every surface the pipeline might bind (the whole run or one of
        # its tokens) is recorded, mirroring display_case's both-shapes doctrine.
        for s in (surface, *[t.text for t in run]):
            s = (s or "").strip()
            if s and len(s) >= 2:
                contexts[s.lower()] = cue

    toks = list(doc)
    for i, tok in enumerate(toks):
        lemma = (tok.lemma_ or "").strip().lower()
        if lemma not in cues or tok.pos_ not in ("NOUN", "PROPN"):
            continue
        # FRAME A — the cue immediately followed by a PROPN run ("our daughter Wren").
        if i + 1 < len(toks) and toks[i + 1].pos_ == "PROPN":
            run = toks[i + 1:i + 2]
            j = i + 2
            while j < len(toks) and toks[j].pos_ == "PROPN":
                run.append(toks[j])
                j += 1
            _record_propn_run(run, lemma)
            continue
        # FRAME B — a (comma) appositive AFTER the name ("Wren, our daughter, ..."):
        # walk left over the run, an optional possessive determiner ("our"/"my" — the
        # DET/PRON possessor of the cue), and the comma that closes the appositive.
        if i >= 1:
            k = i - 1
            if k >= 0 and toks[k].pos_ in ("DET", "PRON"):
                k -= 1                      # the possessor determiner ("our daughter")
            if k >= 0 and toks[k].text in (",",):
                k -= 1
                if k >= 0 and toks[k].pos_ == "PROPN":
                    run = [toks[k]]
                    m = k - 1
                    while m >= 0 and toks[m].pos_ == "PROPN":
                        run.insert(0, toks[m])
                        m -= 1
                    _record_propn_run(run, lemma)
                    continue
        # FRAME C — the genitive complement list ("the mother of Wren and Teo"):
        # cue + 'of' + the complement window.
        if i + 2 < len(toks) and (toks[i + 1].lemma_ or "").lower() == "of":
            window = toks[i + 2:i + 8]
            run: list = []
            for t in window:
                if t.pos_ in ("VERB", "AUX"):
                    break                      # clause boundary — the complement ended
                if t.pos_ == "ADP" and run:
                    break                      # a SECOND adposition opens a new PP
                if t.pos_ == "PROPN":
                    run.append(t)
                    continue
                if t.pos_ in ("CCONJ", "PUNCT") and t.text in ("and", "or", ",", "&") and run:
                    continue                   # a conjunctive list continues the complement
                if t.text == ".":
                    break
            _record_propn_run(run, lemma)
    if contexts:
        log.info("kin_lane_context.observed", surfaces=sorted(contexts.keys()))
    return contexts
