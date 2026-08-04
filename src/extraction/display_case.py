"""Observed display case — RETAIN the surface casing of a name at ingest.

THE DEFECT
==========
Every entity name in every recall response renders lowercase: *"diane does not live in
toronto"*.  The cause is not one line — ``alias`` is lowercased at the registry
(``entity_registry/registry.py``), but by then the casing is ALREADY gone: the whole ingest
pipeline works on pre-lowercased strings by design (``.strip().lower()`` appears at 500+ sites
in ``extraction/linguistics.py`` and again across ``api/main.py``), because "all string
compares on pre-lowercased values" is a load-bearing repo invariant.  Measured on a live local
stack: ``entity_registry.resolve_start`` logs ``original_name=diane`` / ``original_name=ibm`` —
the registry never sees a capital letter at all.

So the casing cannot be recovered by threading a display string down the edge path; it would
have to survive several hundred independent normalisations.  It CAN be recovered from the one
thing that is still verbatim at ``POST /ingest``: ``IngestRequest.text``, the raw user turn.
This module reads that turn ONCE per request and returns a casing overlay keyed by the
lowercase form the rest of the pipeline is already using.

WHY NOT CAPITALISE AT RENDER TIME (the rejected alternative)
============================================================
Restoring case from a lowercase stream is TRUECASING (Lita, Ittycheriah, Roukos & Kambhatla,
*"tRuEcasIng"*, ACL 2003, pp. 152-159).  It is a statistical reconstruction, lossy by
construction and unable to recover ``eBay``, ``IBM``, ``McDonald`` or ``iPhone`` — it would
render ``Ebay``, ``Ibm``, ``Mcdonald``, ``Iphone`` and there is no signal left in the stored
string to tell it otherwise.  Case is a property of the character sequence (Unicode Standard
§4.2 *Case*; case mappings are explicitly non-invertible), so once folded it is gone.  It would
also put reconstruction on the QUERY path, which this codebase forbids: fix it at ingest by
RETAINING the data, never at query by rebuilding it.  This module therefore records what the
user actually typed and nothing else — it never invents a capital.

THE HARD LINE (why naive capture is wrong)
==========================================
A MEMORY is user truth (the name ``Diane``); a PLACE is an L4 type node (``dog``, ``city``)
that the engine builds and files memories AT.  A name is never a place.  Capturing the observed
surface naively breaks that: *"Dogs are great"* would record ``Dogs`` as the display form of
the type node ``dog``, and the type would start rendering like a proper name.

Two guards, both structural — no name list, no capitalisation lexicon, no domain vocabulary:

1. **Sentence-initial evidence is DISCARDED.**  In English orthography the first word of a
   sentence is capitalised obligatorily, so its capital says nothing about the word's identity.
   This is the same first-word ambiguity truecasing systems isolate as their hardest case (Lita
   et al. 2003 §2).  ``Dogs are great`` therefore yields NOTHING for ``dogs``.
2. **Only PROPN runs are observed.**  A proper noun is the grammatical category of a name; a
   common noun in mid-sentence caps is not admitted.  POS comes from the existing spaCy tagger
   (``_get_nlp()``), not from the shape of the string.

A third guard lives at the read seam (``api/main.py``), where the P31/P279 evidence actually
exists: an entity the classification ladder says is a PLACE never renders a display form, even
if one was somehow observed.  See ``_entity_is_l4_place``.

WHAT IS STORED
==============
``entity_aliases.display_form`` is a pure CASING OVERLAY of ``alias``: the invariant
``display_form.lower() == alias`` is enforced at the write seam, so this column can never
become a second, divergent name.  It is nullable, and NULL means "no casing observed" — the
renderer falls back to ``alias``, i.e. exactly today's output.  The lowercase ``alias`` column
keeps its current values and semantics untouched; it remains the matching / dedup / UUID-v5 /
``ON CONFLICT`` key.  This mirrors the SKOS split between a lexical label presented as authored
(``skos:prefLabel``) and the normalised key a system matches on (W3C SKOS Reference §5).
"""

from __future__ import annotations

import contextvars

import structlog

log = structlog.get_logger()


# Per-request casing overlay: {lowercase surface -> observed display form}.
#
# A ContextVar (not a parameter) because ``EntityRegistry`` is constructed at dozens of points
# inside a single ``/ingest`` request, several of them deep inside main.py's edge loop; a
# parameter would have to be threaded through every one and a single missed site silently
# reverts that entity to lowercase.  Same pattern, and same reasoning, as
# ``rel_type_overlay._current_schema``: ContextVars are isolated per asyncio task and survive
# across awaits, so one bind at the request boundary covers every reader.
#
# MUST be reset by the binder (token-based, in a ``finally``) — an unreset overlay would leak
# one turn's casing into the next request in the same task.
_display_forms: contextvars.ContextVar = contextvars.ContextVar(
    "faultline_display_forms", default=None
)


def set_display_forms(forms: dict | None):
    """Bind the request's casing overlay. Returns a token for ``reset_display_forms``."""
    return _display_forms.set(dict(forms) if forms else None)


def reset_display_forms(token) -> None:
    """Unbind the casing overlay (always call in a ``finally``)."""
    try:
        _display_forms.reset(token)
    except Exception:  # noqa: BLE001 — a stale token must never fail a request
        pass


def get_display_forms() -> dict:
    """The current request's casing overlay, or ``{}`` when nothing is bound."""
    return _display_forms.get() or {}


def display_form_for(alias: str) -> str | None:
    """Return the observed display form for a lowercase ``alias``, or ``None``.

    The returned value is guaranteed to case-fold back to ``alias`` — a casing overlay, never
    different content.  ``None`` (no observation, or an overlay entry that does not fold back)
    means the caller stores NULL and the renderer falls back to ``alias`` unchanged.
    """
    if not alias:
        return None
    key = alias.strip().lower()
    if not key:
        return None
    observed = get_display_forms().get(key)
    if not observed or not isinstance(observed, str):
        return None
    observed = observed.strip()
    # INVARIANT — casing overlay only.  Enforced HERE, in Python, rather than as a SQL CHECK
    # constraint: PostgreSQL's ``lower()`` is collation-dependent and can disagree with Python's
    # ``str.lower()`` on non-ASCII input (the tenant DB's collation is driven by the install
    # language).  A CHECK would turn that cosmetic disagreement into a constraint violation on
    # the alias INSERT — i.e. the user's whole sentence lost over a capital letter.  The alias
    # itself is user content and is sacred; its casing is presentation.  Fail toward keeping the
    # sentence.
    if observed.lower() != key:
        return None
    if observed == key:
        return None  # no casing information — store NULL, not a redundant copy
    return observed


def observe_display_forms(text: str) -> dict:
    """Derive ``{lowercase surface -> display form}`` from a VERBATIM user turn.

    Deterministic and subject-agnostic: spaCy POS + sentence boundaries only.  No name list, no
    capitalisation heuristic, no statistical model, no LLM, no GLiNER2.  Returns ``{}`` on any
    failure (spaCy absent, model not baked, parse error) — the layer degrades to today's
    lowercase rendering rather than guessing.

    Admitted: a maximal run of contiguous ``PROPN`` tokens whose FIRST token is not
    sentence-initial and whose surface differs from its own lowercase.  Both the whole run and
    each of its tokens are recorded, because the pipeline may bind either the multi-word name
    (``miss bee providore``) or a single token of it (``diane``) as the entity surface.

    CONFLICT ⇒ DROP.  If one lowercase key is observed with two different surfaces in the same
    turn, neither is recorded.  There is no principled winner, and inventing one would fabricate
    a name; NULL and today's lowercase is the honest answer.
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
        log.warning("display_case.parse_failed", error=str(e)[:160])
        return {}

    observations: dict[str, set] = {}

    def _record(surface: str) -> None:
        surface = (surface or "").strip()
        if not surface:
            return
        key = surface.lower()
        if key == surface:
            return  # carries no casing information
        observations.setdefault(key, set()).add(surface)

    try:
        run: list = []
        for tok in doc:
            if tok.pos_ == "PROPN":
                # Sentence-initial capitalisation is orthographically obligatory and therefore
                # evidence-free.  Discard the whole run it starts — not just that one token —
                # because "Diane lives here" gives no more evidence for the run than for its
                # first word.
                if not run and tok.is_sent_start:
                    run = []
                    continue
                run.append(tok)
                continue
            if run:
                _record("".join(t.text_with_ws for t in run))
                for t in run:
                    _record(t.text)
                run = []
        if run:
            _record("".join(t.text_with_ws for t in run))
            for t in run:
                _record(t.text)
    except Exception as e:  # noqa: BLE001
        log.warning("display_case.observe_failed", error=str(e)[:160])
        return {}

    # CONFLICT ⇒ DROP (see docstring).
    resolved = {k: next(iter(v)) for k, v in observations.items() if len(v) == 1}
    if len(resolved) != len(observations):
        log.info(
            "display_case.conflicting_surfaces_dropped",
            dropped=sorted(k for k, v in observations.items() if len(v) > 1),
        )
    return resolved
