"""Named recurring-event resolution (deterministic rule-driven, NO ML/LLM).

THE PRINCIPLE (CLAUDE.md temporal hinge, fix #3): a named recurring event ("Black Friday",
"Christmas", "New Year's Day") resolves to a deterministic DATE via a RULE (holiday
arithmetic / seeded named-date) + the YEAR chosen by the MINUS — the MOST-RECENT-PAST
occurrence relative to the NOW reference. Loose-but-real: a named-event miss yields ``None``
and the caller keeps NULL (NEVER NULL-on-name when the rule KNOWS it; NEVER LLM-guesses).

The rule set is a CLOSED FORMAL CALENDAR class (fixed-date holidays + a small set of
nth-weekday / relative-holiday rules) — calendar grammar, the same character as the 12
month names already in the date layer, NOT an open-ended domain word-list. A "her birthday"
form is resolved against a SEEDED named-date the user stated (a per-entity birthday scalar)
+ the same minus — that lookup is the caller's (it has the entity); this module owns the
calendar-rule events and exposes the year-minus helper the birthday path reuses.

Subject-agnostic, deterministic, fail-safe (any miss / error → ``None``).
"""

from __future__ import annotations

import os
import re
from datetime import date, timedelta

import structlog

log = structlog.get_logger()

# ── OFFSET-FROM-NAMED-EVENT nearest-occurrence anchoring (default ON) ──────────────────────────────
# A signed offset off a recurring named event ("a week before Black Friday") must anchor to the
# occurrence whose OFFSET RESULT sits NEAREST the reference — the TimeML/TIMEX3 rule that a
# relative-indefinite expression (RI-TIMEX) resolves against the anchor in the window closest to the
# Document Creation Time (candidate dates are ranked by proximity to the DCT; clinical RI-TIMEX
# normalization, PMC4986666). Resolving the EVENT's own most-recent-PAST year FIRST and THEN
# offsetting mis-fires when the event is still upcoming at the reference but the offset pulls the
# result into the past: "a week before Black Friday" said 2023-11-20 → Black Friday 2023 is Nov-24
# (future), so most-recent-past picked 2022 → 2022-11-18, a YEAR off and a broken date-diff operand.
# Choosing the candidate-year whose OFFSET-date is the LATEST on-or-before the reference yields
# 2023-11-17 (correct). Deterministic pure calendar arithmetic. OFF → legacy most-recent-past-of-event
# (a non-upcoming event is byte-identical either way, so this only changes the upcoming-event case).
OFFSET_NEAREST_YEAR: bool = os.environ.get(
    "TEMPORAL_OFFSET_NEAREST_YEAR", "true"
).strip().lower() not in ("0", "false", "no")


# ── nth-weekday-of-month helper (Thanksgiving, etc.) ──────────────────────────────────
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The ``n``-th ``weekday`` (Mon=0…Sun=6) of ``month``/``year``. n>=1."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + (n - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The LAST ``weekday`` of ``month``/``year``."""
    # day count in month
    if month == 12:
        last = date(year, 12, 31)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset)


# Calendar RULES, keyed by canonical name → a function (year) → date. CLOSED formal class.
def _us_thanksgiving(year: int) -> date:
    # 4th Thursday of November
    return _nth_weekday(year, 11, 3, 4)


def _black_friday(year: int) -> date:
    # The Friday AFTER US Thanksgiving (Thanksgiving + 1 day)
    return _us_thanksgiving(year) + timedelta(days=1)


def _cyber_monday(year: int) -> date:
    # The Monday after Thanksgiving (Thanksgiving + 4 days)
    return _us_thanksgiving(year) + timedelta(days=4)


_NAMED_RULES = {
    "new year's day": lambda y: date(y, 1, 1),
    "new years day": lambda y: date(y, 1, 1),
    "new year": lambda y: date(y, 1, 1),
    "valentine's day": lambda y: date(y, 2, 14),
    "valentines day": lambda y: date(y, 2, 14),
    "st patrick's day": lambda y: date(y, 3, 17),
    "st. patrick's day": lambda y: date(y, 3, 17),
    "halloween": lambda y: date(y, 10, 31),
    "christmas eve": lambda y: date(y, 12, 24),
    "christmas": lambda y: date(y, 12, 25),
    "christmas day": lambda y: date(y, 12, 25),
    "new year's eve": lambda y: date(y, 12, 31),
    "new years eve": lambda y: date(y, 12, 31),
    "independence day": lambda y: date(y, 7, 4),
    "fourth of july": lambda y: date(y, 7, 4),
    "july 4th": lambda y: date(y, 7, 4),
    "thanksgiving": _us_thanksgiving,
    "black friday": _black_friday,
    "cyber monday": _cyber_monday,
    "boxing day": lambda y: date(y, 12, 26),
}

# Aliases / surface variants normalize to the canonical keys above.
_NAME_NORMALIZE = re.compile(r"[^a-z0-9'\.\s]")


def _canon_name(text: str) -> str | None:
    """Normalize a candidate phrase to a canonical named-event key, or None if not one."""
    if not text:
        return None
    s = _NAME_NORMALIZE.sub(" ", text.strip().lower())
    s = re.sub(r"\s+", " ", s).strip()
    if s in _NAMED_RULES:
        return s
    # tolerate a trailing/leading article and the possessive variants already keyed
    s2 = re.sub(r"^(the|this|last|next|on)\s+", "", s).strip()
    if s2 in _NAMED_RULES:
        return s2
    return None


def is_named_event(text: str) -> bool:
    """True iff ``text`` (or a span within it) names a known calendar rule event."""
    if not text:
        return False
    try:
        if _canon_name(text) is not None:
            return True
        # substring scan for a known name embedded in a longer span
        low = text.lower()
        return any(name in low for name in _NAMED_RULES)
    except Exception:  # noqa: BLE001
        return False


def most_recent_past_year(rule, reference) -> int:
    """The YEAR (via the MINUS) whose rule-date is the MOST RECENT occurrence on/before the
    reference. Tries reference.year; if that lands strictly AFTER the reference, steps back a
    year. Pure arithmetic, deterministic."""
    try:
        ref_d = reference.date()
    except Exception:  # noqa: BLE001
        ref_d = reference
    y = ref_d.year
    try:
        if rule(y) <= ref_d:
            return y
        return y - 1
    except Exception:  # noqa: BLE001 — fail-safe: reference year
        return y


# ── OFFSET-FROM-NAMED-EVENT ("a week before Black Friday") ────────────────────────────
# Calendar grammar (the SAME closed formal class as the month names / holiday rules above —
# NOT a domain word-list): a small unit→days table and the two relational prepositions that
# carry a SIGNED calendar offset. "a"/"an" = a single unit (1). spaCy/dateparser cannot resolve
# this compound ("a week before <holiday>") — dateparser drops the holiday and guesses off
# "week"; the named-event resolver alone drops the offset. This resolves the named event date
# THEN applies the signed offset, deterministically.
_OFFSET_UNIT_DAYS = {
    "day": 1, "days": 1,
    "week": 7, "weeks": 7,
    "fortnight": 14, "fortnights": 14,
}
_OFFSET_UNIT_MONTHS = {"month": 1, "months": 1}
_OFFSET_UNIT_YEARS = {"year": 1, "years": 1}
# "<count> <unit> before|after <... named event ...>". count = a/an or an integer.
_OFFSET_NAMED_RE = re.compile(
    r"\b(?P<count>a|an|\d+)\s+(?P<unit>[a-z]+)\s+(?P<dir>before|after|prior\s+to)\s+(?P<rest>.+)$",
    re.IGNORECASE,
)

# ── ANCHORED "IN ADVANCE" OFFSET ("three months in advance", "a week ahead of the trip") ──────────
# A PRE-EVENT relative duration: the count+unit measures BACKWARD from an ANCHOR event the action
# PRECEDES ("book three months in advance [of the trip]"). This is a NEGATIVE offset from that anchor
# event — NOT "N units ago" from the utterance. spaCy DATE-NER tags the bare "three months" and
# dateparser then mis-resolves it to (reference − N) as though it read "three months ago", poisoning
# the event date (the idx19 CHAINED-RELATIVE gap). TimeML/TIMEX3: an anchored DURATION (a
# relative-indefinite RI-TIMEX) resolves against a DISCOURSE anchor event, not the Document Creation
# Time (Pustejovsky et al., TimeML 2003; RI-TIMEX normalization anchors to the nearest event,
# PMC4986666). This resolver only PARSES the count+unit+advance-marker grammatically; the CALLER
# supplies the anchor date (the nearest prior dated event) and applies the signed offset. Calendar
# grammar (the SAME closed class as the unit tables above), NOT a domain word-list. The number words
# are a closed cardinal class (number grammar), NOT domain vocabulary.
_ADVANCE_NUM_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
# "<count> <unit> in advance | beforehand | ahead of <…>". The marker is the closed set of adverbials
# that denote PRECEDENCE-before-an-anchor. "ahead of" REQUIRES the "of" (bare "ahead" is ambiguous
# with a future sense and is deliberately NOT matched).
_ADVANCE_OFFSET_RE = re.compile(
    r"\b(?P<count>a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d+)\s+"
    r"(?P<unit>days?|weeks?|fortnights?|months?|years?)\s+"
    r"(?P<marker>in\s+advance|beforehand|ahead\s+of)\b",
    re.IGNORECASE,
)


def parse_advance_offset(text: str):
    """Parse a PRE-EVENT "in advance" offset out of ``text`` → ``(count:int, unit:str)`` or ``None``.

    "had to book three months in advance" → ``(3, "months")``. Grammatical, deterministic, a closed
    calendar/number-grammar class (NO domain word-list). Returns ``None`` when no such construction is
    present. The SIGN is fixed NEGATIVE (the action precedes the anchor); the caller applies it to an
    anchor date via :func:`apply_advance_offset`. Fail-safe: any error → ``None`` (never fabricates)."""
    if not text:
        return None
    try:
        m = _ADVANCE_OFFSET_RE.search(text)
        if not m:
            return None
        raw = (m.group("count") or "").lower()
        count = _ADVANCE_NUM_WORDS.get(raw)
        if count is None:
            count = int(raw) if raw.isdigit() else None
        if not count or count <= 0:
            return None
        unit = (m.group("unit") or "").lower()
        if (unit not in _OFFSET_UNIT_DAYS and unit not in _OFFSET_UNIT_MONTHS
                and unit not in _OFFSET_UNIT_YEARS):
            return None
        return (count, unit)
    except Exception as e:  # noqa: BLE001 — fail-safe: never fabricate
        log.warning("temporal.parse_advance_offset_failed",
                    text=(text or "")[:48], error=str(e)[:120])
        return None


def apply_advance_offset(anchor: date, count: int, unit: str) -> date:
    """``anchor − count*unit`` (a pre-event offset). Reuses the signed-offset calendar arithmetic
    (day/week/fortnight via timedelta, month/year via calendar math). Deterministic, pure."""
    return _apply_signed_offset(anchor, -1, count, unit)


def _named_rule_for_text(text: str):
    """The ``(canonical_name, rule)`` for the named event in ``text`` (canon or longest embedded
    surface), or ``None``. Shares the SAME name-resolution as ``resolve_named_event`` but returns the
    calendar RULE so the caller can evaluate it across candidate years (offset nearest-occurrence)."""
    if not text:
        return None
    name = _canon_name(text)
    if name is None:
        low = text.lower()
        cands = sorted((n for n in _NAMED_RULES if n in low), key=len, reverse=True)
        name = cands[0] if cands else None
    if name is None:
        return None
    rule = _NAMED_RULES.get(name)
    if rule is None:
        return None
    return (name, rule)


def _apply_signed_offset(base: date, sign: int, count: int, unit: str) -> date:
    """Apply a SIGNED calendar offset (``sign*count`` ``unit``) to ``base`` — day/week/fortnight via
    ``timedelta``, month/year via calendar arithmetic. Pure, deterministic."""
    if unit in _OFFSET_UNIT_DAYS:
        return base + timedelta(days=sign * count * _OFFSET_UNIT_DAYS[unit])
    if unit in _OFFSET_UNIT_MONTHS:
        return _shift_months(base, sign * count * _OFFSET_UNIT_MONTHS[unit])
    # years
    try:
        return base.replace(year=base.year + sign * count)
    except ValueError:  # Feb-29 → Feb-28 fail-safe
        return base.replace(year=base.year + sign * count, day=28)


def resolve_offset_named_event_span(text: str, reference):
    """Like ``resolve_offset_named_event`` but ALSO returns the matched SPAN so the peel can excise
    the whole compound. Returns ``(date, "day", start, span_text)`` or ``None``.

    The span runs from the count token through the named-event tail the rule recognized (so the peel
    drops "a week before Black Friday" whole, leaving no dangling "before Black Friday" residue)."""
    if not text or reference is None:
        return None
    try:
        m = _OFFSET_NAMED_RE.search(text.strip())
        if not m:
            return None
        unit = (m.group("unit") or "").lower()
        if (unit not in _OFFSET_UNIT_DAYS and unit not in _OFFSET_UNIT_MONTHS
                and unit not in _OFFSET_UNIT_YEARS):
            return None
        raw = (m.group("count") or "").lower()
        count = 1 if raw in ("a", "an") else int(raw)
        sign = 1 if (m.group("dir") or "").lower() == "after" else -1
        rest = m.group("rest")
        d = None
        if OFFSET_NEAREST_YEAR:
            # NEAREST-OCCURRENCE: evaluate the event RULE across candidate years, apply the offset to
            # each, and pick the OFFSET-date that is the latest on-or-before the reference (else the
            # nearest future one). This anchors "a week before Black Friday" to the season nearest the
            # utterance, not the event's own most-recent-past year. Deterministic calendar arithmetic.
            nr = _named_rule_for_text(rest)
            if nr is None:
                return None
            _name, rule = nr
            try:
                ref_d = reference.date()
            except Exception:  # noqa: BLE001 — reference is already a date
                ref_d = reference
            cands = []
            for y in (ref_d.year + 1, ref_d.year, ref_d.year - 1):
                try:
                    cands.append(_apply_signed_offset(rule(y), sign, count, unit))
                except Exception:  # noqa: BLE001 — a bad year → skip that candidate
                    continue
            if not cands:
                return None
            past = [c for c in cands if c <= ref_d]
            d = max(past) if past else min(cands)
        else:
            anchor = resolve_named_event(rest, reference)
            if anchor is None:
                return None
            base, _g = anchor
            d = _apply_signed_offset(base, sign, count, unit)
        # SPAN: from the count group through the recognized named-event surface inside ``rest``.
        # Map back onto the ORIGINAL ``text`` (the regex ran on a stripped copy — re-locate the count).
        stripped = text.strip()
        lead = len(text) - len(text.lstrip())
        nm = _canon_or_embedded_surface(rest)
        if nm:
            # end = position (within the original text) just past the named-event surface
            rel_rest_start = m.start("rest")
            idx_in_rest = rest.lower().find(nm)
            end_in_stripped = rel_rest_start + idx_in_rest + len(nm) if idx_in_rest >= 0 else m.end()
        else:
            end_in_stripped = m.end()
        start = lead + m.start("count")
        end = lead + end_in_stripped
        span_text = text[start:end]
        return (d, "day", start, span_text)
    except Exception as e:  # noqa: BLE001 — fail-safe: never fabricate
        log.warning("temporal.resolve_offset_named_event_span_failed",
                    text=(text or "")[:48], error=str(e)[:120])
        return None


def _canon_or_embedded_surface(text: str) -> str | None:
    """The lowercase SURFACE of the known named event inside ``text`` (longest match), or None."""
    if not text:
        return None
    low = _NAME_NORMALIZE.sub(" ", text.lower())
    low = re.sub(r"\s+", " ", low)
    cands = sorted((n for n in _NAMED_RULES if n in low), key=len, reverse=True)
    if not cands:
        return None
    # return the surface as it appears in the ORIGINAL (lower) text for index mapping
    name = cands[0]
    raw_low = text.lower()
    return name if name in raw_low else None


def resolve_offset_named_event(text: str, reference):
    """Resolve "<N> <unit> before|after <named-event>" to ``(date, "day")`` or ``None``.

    "a week before Black Friday" (said 2023-05) → Black Friday 2022-11-25 MINUS 7 days =
    2022-11-18. Deterministic: parse the count+unit+direction grammatically, resolve the named
    event via the existing rule, apply the SIGNED calendar offset. ``before``/``prior to`` =
    minus, ``after`` = plus. Month/year units use calendar arithmetic. Returns ``None`` when the
    pattern doesn't match, the tail names no known event, or on any error (never fabricates)."""
    res = resolve_offset_named_event_span(text, reference)
    if res is None:
        return None
    d, g, _s, _sp = res
    return (d, g)


def _shift_months(d: date, months: int) -> date:
    """Shift ``d`` by ``months`` calendar months (clamping the day to month length)."""
    m0 = d.month - 1 + months
    year = d.year + m0 // 12
    month = m0 % 12 + 1
    # clamp day to the target month's length
    if month == 12:
        last = 31
    else:
        last = (date(year, month + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(d.day, last))


def resolve_named_event(text: str, reference):
    """Resolve a named recurring event to ``(date, "day")`` via its calendar rule + the
    most-recent-past year from the NOW reference, or ``None`` when ``text`` names no known
    rule event / ``reference`` is missing / on any error.

    "Black Friday" said 2023-05 → 2022-11-25 (the most-recent-past Black Friday).
    Deterministic, rule-driven, NEVER LLM-guessed, NEVER NULL-on-name when the rule knows it."""
    if not text or reference is None:
        return None
    try:
        name = _canon_name(text)
        if name is None:
            # try an embedded name (longest match wins for specificity)
            low = text.lower()
            cands = sorted((n for n in _NAMED_RULES if n in low), key=len, reverse=True)
            name = cands[0] if cands else None
        if name is None:
            return None
        rule = _NAMED_RULES.get(name)
        if rule is None:
            return None
        y = most_recent_past_year(rule, reference)
        d = rule(y)
        return (d, "day")
    except Exception as e:  # noqa: BLE001 — fail-safe: never fabricate
        log.warning("temporal.resolve_named_event_failed", text=(text or "")[:48], error=str(e)[:120])
        return None


def resolve_named_event_span(text: str, reference):
    """Like ``resolve_named_event`` but ALSO returns the matched SPAN so a date PEEL can excise the
    bare named-event phrase. Returns ``(date, "day", start, span_text)`` or ``None``.

    This is the BARE-named-event sibling of ``resolve_offset_named_event_span`` ("a week before
    Black Friday"): for the non-offset case ("… on Black Friday", "… on Christmas") the point
    resolver already knows the date; this adds the surface LOCATION so the peel/residue path can
    drop the named-event phrase out of the clause (parity with the offset span resolver and the
    engine's own date-span excision). Without a span the peel could resolve the date but leave the
    holiday name folding into the residue relation. ``start`` is the index (in the ORIGINAL text) of
    the longest known named-event surface present verbatim; ``span_text`` is that surface. The whole
    rule set is the SAME closed formal calendar class as the holiday rules above — calendar grammar,
    NOT a domain word-list. Deterministic, fail-safe: any miss/error → ``None`` (never fabricates)."""
    if not text or reference is None:
        return None
    try:
        anchor = resolve_named_event(text, reference)
        if anchor is None:
            return None
        d, g = anchor
        low = text.lower()
        # Longest known surface present verbatim wins (specificity — "christmas eve" over "christmas").
        cands = sorted((n for n in _NAMED_RULES if n in low), key=len, reverse=True)
        if not cands:
            return None
        surface = cands[0]
        start = low.find(surface)
        if start < 0:
            return None
        return (d, g or "day", start, text[start:start + len(surface)])
    except Exception as e:  # noqa: BLE001 — fail-safe: never fabricate
        log.warning("temporal.resolve_named_event_span_failed",
                    text=(text or "")[:48], error=str(e)[:120])
        return None
