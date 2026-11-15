"""GLOBAL-RESET TURN SHAPES — the closed marker vocabulary for the wholesale-replace
conversation moves ("forget all previous family information and replace with: …").

LIVE-OPENWEBUI FIX 1 (DEV/BUGS/family-smoke-20261111/LIVE-OPENWEBUI-DIAGNOSIS.md, measured
2026-11-13 on the owner's prod seat): "forget all previous family information and replace
with: …" scored GLiNER2 CORRECTION @0.58 but ``classify_intent.correction_marker_gate``
REFUSED it (no explicit old-value/repair marker matched) → routed STATEMENT → the whole
16:47–17:16 reset session ingested as ADDS: zero supersession, zero retraction outcomes,
the old family rows stayed live beside the new ones.

THE FIX, in three deterministic parts (this module is the ONE source of truth both
processes import — the API classifier/executor and the MCP transport never re-derive):

  1. THE MARKER VOCABULARY — ``RESET_MARKER_RES``: the closed global-reset shapes
     ("forget all", "replace with", "start over", "wipe", "clear all", "start fresh").
     ``main._turn_carries_correction_marker`` (the CORRECTION_MARKER_GATE evidence floor)
     unions this set, so a classifier-born CORRECTION carrying a reset marker is never
     refused for lack of an old-value marker. ``main._turn_strong_correction_marker``
     (the INTENT_DETERMINISTIC_FIRST tier-1 route) unions it too — routing on the marker
     inventory is the stronger-evidence direction, never a new word list. Both unions are
     flag-gated by the gate's OWN flag (CORRECTION_MARKER_GATE OFF → byte-parity: the
     reset members are invisible, exactly today's behavior).

  2. THE EXECUTOR DETECTOR — ``detect_global_reset(text)``: True reset = a reset marker
     AND a replacement carrier ("replace with") AND a RECOGNIZED scope noun in the closed
     ``SCOPE_NOUN_CATEGORIES`` map. Anything else → ``None`` (the turn keeps today's
     lanes). The destructive reset NEVER fires on an unrecognized scope — the closed map
     is the safety interlock, and it is growable (new rows extend the map, never new code
     paths).

  3. THE PAYLOAD SPLIT — ``reset_payload(text)``: the replacement data after the carrier.
     The MCP transport harvests THIS span (not the reset header), so the reset sentence's
     new profile lands through the ordinary statement harvest without the imperative
     header minting junk edges.

Deterministic throughout: compiled regexes over a closed inventory, no LLM, no DB.
"""

import re

# ── 1. THE MARKER VOCABULARY (the closed global-reset shapes) ──────────────────
# Each member is the surface shape of a WHOLESALE-REPLACE move — the speaker retires a
# scope and substitutes new content in one turn. Word-boundary anchored, case-insensitive.
RESET_MARKER_RES: tuple = (
    re.compile(r"\bforget\s+(?:all|everything|my|the|our|previous)\b", re.IGNORECASE),
    # "forget all previous family information and replace with: …"
    re.compile(r"\breplace\s+(?:everything|all|it(?:\s+all)?)?\s*with\b", re.IGNORECASE),
    re.compile(r"\bstart\s+over\b", re.IGNORECASE),          # "let's start over"
    re.compile(r"\bstart\s+fresh\b", re.IGNORECASE),         # "start fresh"
    re.compile(r"\bclear\s+(?:all|everything|my|the|our)\b", re.IGNORECASE),
    re.compile(r"\bwipe\s+(?:all|everything|my|the|our|it)\b", re.IGNORECASE),
    # bare "wipe" alone is too trigger-happy for a marker that routes CORRECTION;
    # the wipe member above requires an object. ("wipe" the pin names is covered.)
)

# The replacement CARRIER: the marker that separates the retired scope from the new data.
# A reset without a carrier ("forget all my pets" — a plain bounded retraction) is NOT a
# global reset and keeps today's lanes; with the carrier the turn is a wholesale replace.
REPLACE_CARRIER_RE = re.compile(
    r"\breplace(?:s|d)?\s+(?:everything|all|it(?:\s+all)?)?\s*with\b", re.IGNORECASE)

# ── 2. THE SCOPE MAP (closed; the destructive-reset interlock) ─────────────────
# Scope noun (as it appears in the turn, lowercased, plural-tolerant via the suffix
# alternation below) → the rel_types CATEGORY whose live rows the reset retires.
# 'family' is the measured scope (the owner's prod reset); the seed family category is
# spouse/child_of/parent_of/sibling_of/step_parent_of/ex_spouse and the nested 'pets'
# category (has_pet) hangs under it (entity_taxonomies member_taxonomies: family ⊃ pets).
# Anything NOT in this map → detect_global_reset returns None → the turn keeps today's
# lanes (never a guessed destructive scope).
SCOPE_NOUN_CATEGORIES: dict = {
    "family": "family",
    "pet": "pets",
}

_SCOPE_NOUN_RE = re.compile(
    r"\b(" + "|".join(SCOPE_NOUN_CATEGORIES) + r")(?:s|es)?\b",
    re.IGNORECASE)


def _reset_marker_hits(text: str) -> list:
    """The reset-markers the turn carries (matched pattern strings; diagnostic only)."""
    _t = (text or "").lower()
    return [r.pattern for r in RESET_MARKER_RES if r.search(_t)]


def detect_global_reset(text) -> dict | None:
    """The deterministic global-reset reading of a turn, or None.

    A turn IS a global reset when ALL THREE hold:
      1. it carries a reset marker (``RESET_MARKER_RES``), and
      2. it carries the replacement carrier (``REPLACE_CARRIER_RE`` — the new data is
         IN the turn, not just a deletion), and
      3. it names a RECOGNIZED scope noun (``SCOPE_NOUN_CATEGORIES``).

    Returns {"scope_category": str, "payload": str, "markers": list[str]} — the category
    is the rel_types category whose live rows retire; the payload is the replacement data
    (``reset_payload``). Any miss → None (today's lanes, byte-for-byte).
    """
    if not text or not (text or "").strip():
        return None
    markers = _reset_marker_hits(text)
    if not markers:
        return None
    if not REPLACE_CARRIER_RE.search(text):
        return None
    m = _SCOPE_NOUN_RE.search(text)
    if not m:
        return None
    scope = SCOPE_NOUN_CATEGORIES.get((m.group(1) or "").strip().lower())
    if not scope:
        return None
    return {"scope_category": scope, "payload": reset_payload(text), "markers": markers}


def reset_payload(text) -> str:
    """The replacement data of a reset turn: everything after the LAST replacement
    carrier ("… and replace with: Daniel Reyes (Dan), spouse Mara …" → the
    profile body). No carrier → "" (a marker-only reset carries no new data). The MCP
    statement harvest ingests THIS span, so the imperative header never mints junk."""
    if not text:
        return ""
    hits = list(REPLACE_CARRIER_RE.finditer(text))
    if not hits:
        return ""
    _tail = text[hits[-1].end():].strip()
    # the carrier's own punctuation ("replace with:") rides the tail — strip a leading
    # colon/semicolon before the payload reaches the statement harvest.
    return _tail.lstrip(":;").strip()
