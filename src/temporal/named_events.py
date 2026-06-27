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

import re
from datetime import date, timedelta

import structlog

log = structlog.get_logger()


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
        anchor = resolve_named_event(rest, reference)
        if anchor is None:
            return None
        base, _g = anchor
        if unit in _OFFSET_UNIT_DAYS:
            d = base + timedelta(days=sign * count * _OFFSET_UNIT_DAYS[unit])
        elif unit in _OFFSET_UNIT_MONTHS:
            d = _shift_months(base, sign * count * _OFFSET_UNIT_MONTHS[unit])
        else:  # years
            try:
                d = base.replace(year=base.year + sign * count)
            except ValueError:  # Feb-29 → Feb-28 fail-safe
                d = base.replace(year=base.year + sign * count, day=28)
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
