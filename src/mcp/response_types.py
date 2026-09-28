"""Recall VOICE presets — the wording of the confidence-as-voice bands.

This module owns ONLY the WORDING of the framing text that wraps recalled facts on
their way back to the consuming LLM (the assert / held / soft-context / chronological-event
bands emitted by ``recall_memory_tool`` in ``server.py``). It is a pure, DB-free registry +
one deterministic ``render`` function.

THE EPISTEMIC FIREWALL IS LOCKED HERE — read before editing:
  * This module NEVER decides which band a fact lands in. The class→band assignment
    (Class C → held; A/B + scalars → asserted; store_context → held/soft) is made in
    ``recall_memory_tool`` and is NOT a knob here. A preset only re-words the bands.
  * EVERY preset's HELD band MUST keep lower-confidence / tentative / do-not-state-as-fact
    semantics — including the de-self'd ``agent`` preset ("treat as unconfirmed"). A preset
    that let a held fact read as asserted would breach the firewall and is forbidden.
  * The scrub (`_clean_for_mcp`) and the no-fabrication stance are applied by the caller and
    are independent of the preset. Presets add no facts and drop no bands.

DEFAULT VOICE: the recall tool passes None, which ``render`` maps to ``personal`` — the safe
default voice.
``personal`` is the best-practice-aligned default recall voice (positive-framed per current
prompt-engineering guidance — "tell the model what to do", not what to avoid); it replaced the
older negative-heavy wording in a deliberate tightening pass (see git history). The assembly
SHAPE is unchanged — only the band WORDING was improved.

The ``agent`` preset is a MODE, not merely a tone: it turns OFF FaultLine's "I/me = the user"
self-framing. Facts are surfaced as facts-in-context, never anchored to a "you"/"user".
"""

import re as _re
from typing import Optional

# Canonical preset ids. Curated, closed set (a VOICE registry, not user content — this is
# framing wording, not domain data, so enumerating the presets is correct, not a hardcode of
# subject/domain vocabulary).
PERSONAL = "personal"
PROFESSIONAL = "professional"
CLINICAL = "clinical"
CONCISE = "concise"
AGENT = "agent"

VALID_RESPONSE_TYPES = frozenset({PERSONAL, PROFESSIONAL, CLINICAL, CONCISE, AGENT})

def normalize(response_type: Optional[str]) -> str:
    """Map any input to a valid preset id; unknown / None → ``personal`` (safe default)."""
    rt = (response_type or "").strip().lower()
    return rt if rt in VALID_RESPONSE_TYPES else PERSONAL


# ── The band-wording registry ──────────────────────────────────────────────────────
# Each preset supplies two SHORT framing strings:
#   event_intro — the CHRONOLOGICAL-events framing (precedes the pre-sorted, timestamp-prefixed
#                 event lines). Ends with ":\n" so the joined lines follow directly.
#   hold_intro  — the HELD-band cue (precedes the genuinely-uncertain lines). A MINIMAL lead,
#                 not a paragraph — but it MUST preserve lower-confidence / tentative semantics
#                 in EVERY preset (the epistemic firewall). Ends with a space so the joined
#                 lines follow inline.
#
# NO PREAMBLE: the generic "here is what you know about the user, speak as 'you', weave into
# prose, it describes the user not you" how-to is IDENTICAL every turn — pure repeated bloat in
# the response. It now lives ONCE up-chain in the recall TOOL DESCRIPTION (http_server.py /
# tools.py), where the model reads it when it connects (memory-teaches-the-model reactive
# baseline). The RESPONSE returns just the facts as clean flowing prose + this minimal held cue.
# `personal` is the default voice.
_BANDS: dict[str, dict[str, str]] = {
    PERSONAL: {
        "event_intro": (
            "These events are listed in the exact order they happened (earliest "
            "first). Keep them in this order and rely on it exactly as given:\n"
        ),
        "hold_intro": "Less certain (mention only if relevant, and tentatively): ",
    },
    PROFESSIONAL: {
        "event_intro": (
            "The following events are recorded in chronological order (earliest "
            "first). Rely on this order exactly as given:\n"
        ),
        "hold_intro": "Unconfirmed (reference tentatively, if relevant): ",
    },
    CLINICAL: {
        "event_intro": (
            "The following is noted in the order it occurred (earliest first). "
            "Keep this order exactly as given:\n"
        ),
        "hold_intro": "Less certain (hold lightly, raise only if it helps): ",
    },
    CONCISE: {
        "event_intro": (
            "Events in chronological order (earliest first). Keep this order as "
            "given:\n"
        ),
        "hold_intro": "Unconfirmed (lower confidence): ",
    },
    # DE-SELF MODE: facts are facts-in-context, NOT anchored to a "user"/"you". No
    # first-person-possessive self-framing. The held cue stays firewall-compliant with
    # de-self'd tentative wording ("lower-confidence"), never "you are less certain".
    AGENT: {
        "event_intro": (
            "The following events are ordered chronologically (earliest first). "
            "Preserve this order exactly as given:\n"
        ),
        "hold_intro": "Lower-confidence (unconfirmed): ",
    },
}


# ── De-self transform (AGENT mode only) ─────────────────────────────────────────────
# The backend's convert_to_prose renders the querying user's OWN slots in the second person
# ("You own a bike", "your dog"). That "I/me = the user" self-framing is exactly what the
# agent MODE turns off. This maps the CLOSED grammatical class of English second-person
# perspective tokens to neutral third-person, so a recalled fact reads as a standalone fact
# about a subject — no "you"/"your"/user anchor — while its MEANING (subject + predicate) is
# preserved. It is a closed FUNCTION-WORD class (not domain vocabulary and not a self-reference
# DETECTION list at ingest), applied ONLY to line CONTENT, ONLY in the de-self voice, and never
# to the authored framing. Deterministic, no LLM/cosine.
_DESELF_MAP = {
    "you're": "they're", "you've": "they've", "you'll": "they'll", "you'd": "they'd",
    "yourselves": "themselves", "yourself": "themselves",
    "yours": "theirs", "your": "their", "you": "they",
}
# Longest / most-specific alternatives first so "your" never pre-empts "yourself", etc.
_DESELF_RE = _re.compile(
    r"\b(you're|you've|you'll|you'd|yourselves|yourself|yours|your|you)\b",
    _re.IGNORECASE,
)


def _match_case(original: str, replacement: str) -> str:
    """Carry the original token's leading capitalization onto the replacement."""
    if original[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def deself_line(text: str) -> str:
    """Neutralize second-person self-anchoring in one prose line (de-self voice only)."""
    return _DESELF_RE.sub(
        lambda m: _match_case(m.group(0), _DESELF_MAP[m.group(0).lower()]), text
    )


# Presets whose fact LINES are de-self'd (perspective neutralized), not only their framing.
_DESELF_PRESETS = frozenset({AGENT})


# THE HARD LINE made visible: a background drain/status line is NOT a recalled memory. It
# is fenced under its own explicit label, always OUTSIDE the fact bands, so a model can never
# read "still importing N documents" as recallable user content.
_LABEL_STATUS = "=== STATUS (not memory) ==="


def render(
    response_type: Optional[str],
    *,
    event_lines: list[str],
    assert_lines: list[str],
    hold_lines: list[str],
    status_note: Optional[str] = None,
    abstention: Optional[str] = None,
) -> str:
    """Assemble the final recall ``memory`` string for the resolved ``response_type``.

    Band membership is decided by the CALLER; this only picks the framing wording. The
    assembly shape is IDENTICAL across presets: the present sections (events, then asserted,
    then held) joined by blank lines. There is NO generic preamble — that how-to now lives
    up-chain in the recall tool description, so the response is just the facts as clean prose
    plus a minimal held-band cue. For ``response_type in {None, "personal"}`` the output is
    the default voice.

    ``status_note`` (THE HARD LINE) is an OPTIONAL out-of-band operational STATUS — e.g. a
    background document-import banner. It is NEVER a memory: it is fenced under its own
    ``STATUS (not memory)`` label, always OUTSIDE the fact bands,
    so a model cannot read it as recalled user content. Appended last; None/empty → nothing.
    """
    preset = normalize(response_type)
    bands = _BANDS[preset]

    # De-self voice (agent): neutralize the querying-user perspective in the fact LINES so
    # they read as standalone facts. Band MEMBERSHIP is untouched — a Class-C line stays in
    # hold_lines; only its wording loses the "you" anchor. Non-de-self presets pass lines
    # through verbatim (so `personal` remains byte-for-byte the pre-feature output).
    _line = deself_line if preset in _DESELF_PRESETS else (lambda s: s)

    sections: list[str] = []
    if event_lines:
        sections.append(bands["event_intro"] + "\n".join(_line(x) for x in event_lines))
    if assert_lines:
        # Asserted band carries no intro of its own — it reads as plain, clean prose.
        sections.append("\n".join(_line(x) for x in assert_lines))
    if hold_lines:
        sections.append(bands["hold_intro"] + "\n".join(_line(x) for x in hold_lines))

    # No preamble: the response is just the fact sections (the generic framing moved up-chain
    # into the recall tool description). Joined by blank lines exactly as before.
    body = "\n\n".join(sections)

    _status = (status_note or "").strip()

    # ``abstention`` is the answer ABOUT MEMORY when the walk found nothing — "you have not
    # told me this". It belongs in the fact body, because that is the question the caller
    # asked; a status note answers a different question (why the engine is talking about
    # itself) and must not stand in for it. Only used when there are genuinely no fact lines,
    # so it can never displace a real memory.
    if not body and (abstention or "").strip():
        body = (abstention or "").strip()

    if _status:
        # THE HARD LINE: fence the background status under its own label, OUTSIDE the
        # fact body — never woven into the asserted-memory prose.
        return f"{body}\n\n{_LABEL_STATUS}\n{_status}" if body else f"{_LABEL_STATUS}\n{_status}"
    return body
