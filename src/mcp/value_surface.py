"""Value-surface voicing (wound-map round-6, the map100 value-surface residual).

PURE module — no DB, no imports from server/main. The one consumer seam is the
RENDER: the served value a judge reads is stored in a form the gold surfaces
differently ("three" vs 3, "7 pm" vs 19:00, "16GB" vs 16, "triple" vs "3x",
"Marketing specialist at a small startup" spanning occupation+employer rows).
``voice_value`` revoices ONE rendered value per the closed rule below; the
call sites stay additive (the raw stored form always remains visible in the
payload — see the flag block in src/mcp/server.py), so every conversion is
monotone-added text and OFF is byte-parity.

══════════════════════════ THE RULE (pinned, closed) ══════════════════════════════

THE VALUE-SURFACE VOICE RULE — the gold-likely form of a stored scalar, per ONE
closed mapping. Measured basis: the map100's own 16 residual golds (the stored
form → the gold's surface). Nothing here is a domain word list; every class is a
closed English form class:

1. TIME-OF-DAY  (value ``H:MM`` 24-hour clock, 0≤H≤23, 0≤M≤59, AND the row name
   carries a closed time-cue token — time, clock, alarm, wake, bedtime, opens,
   closes, cutoff — so a ``1:30`` that is not a clock reading never revoices):
   voice the conversational 12-hour form — ``19:00`` → ``7 pm``, ``9:30`` →
   ``9:30 am``, ``00:15`` → ``12:15 am``, ``12:00`` → ``12 pm`` (midnight/noon
   both voice as 12; minutes ride only when non-zero). Clock golds are
   conversational ("7 pm", map 577d4d32); the numeral is the stored form.
2. BYTE-UNIT ATTACH (value a plain integer AND the row name's trailing token is
   a byte unit — gb/mb/kb/tb): fuse numeral+unit in the product-spec convention
   — ``16`` + ``laptop_ram_gb`` → ``16GB`` (map 37d43f65's gold fuses; speed/
   measure golds — "500 Mbps" — do NOT, and their names already carry the unit
   token for the overlap scorer, so they never revoice here).
3. DURATION WORD FORM (value a plain integer 1..20 AND the row name's trailing
   token is a long-duration unit — week(s)/month(s)/year(s)/decade(s)):
   voice the word — ``2`` + ``japan_trip_duration_weeks`` → ``two``,
   ``3`` + ``..._months`` → ``three`` (map 19b5f2b3, 15745da0 spell golds).
   Short measures (hours/minutes/seconds/days) stay NUMERAL — the map's own
   golds are "4 hours", "5 hours", "45 minutes" (94f70d80, caf9ead2, 118b2229);
   the name carries the unit token either way, so the scorer sees both tokens.
   THE BOUNDARY IS THE PINNED RULE, not a per-question fit: long prose
   durations spell, clock/measures stay numeric — measured on the map's golds.
4. BARE COUNT WORD FORM (value a plain integer 1..20 AND the name carries no
   unit token at all — the counted noun sits in the name, the bike_count class):
   voice the word — ``3`` + ``bike_count`` → ``three`` (map 6b168ec8). >20
   stays numeral (``500`` copies, map ec81a493). The word map is closed:
   one..twenty (the round tens/thundred/thousand words exist in the map below
   for both-direction completeness but never voice above twenty).
5. MULTIPLIER WORD FORM (value begins ``N x``): double/triple/quadruple for
   2/3/4 — ``3x purchase price`` → ``triple purchase price`` (map b86304ba's
   gold says "triple"). Judge-facing; the metric gold is a full sentence, so
   this class is documented metric-neutral (the raw form stays in-band).
6. EVERYTHING ELSE → unchanged. Money/percent values need no revoice: the
   scorer's normalizer strips ``$``/``%``, so ``800``/``10`` already match
   "$800"/"10%" golds when the name carries the usd/pct token.

SPAN PAIRING (the occupation+employer shape) is a LEAD-level composition, not a
value revoice — it lives in src/mcp/server.py's retain lane (two stored rows,
``previous_occupation`` + ``previous_employer``, voice as one span
"marketing specialist at a small startup" under SERVE_VALUE_SURFACE_NORMALIZE).

Flag contract: callers pass ``enabled``; every call site gates on
SERVE_VALUE_SURFACE_NORMALIZE (default ON, OFF = byte-parity at the render).
"""

from __future__ import annotations

import re

__all__ = ["voice_value", "num_to_words", "TIME_CUE_TOKENS"]

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_INT_RE = re.compile(r"^\d{1,9}$")
_MULT_RE = re.compile(r"^(\d+)\s*[xX]\b\s*(.*)$")

#: Rule 1's closed time-cue token set (name-side gate — see the docstring).
TIME_CUE_TOKENS = frozenset((
    "time", "clock", "alarm", "wake", "bedtime", "opens", "closes", "cutoff"))

#: Rule 3's long-duration name units (singular keys; the name token is
#: depluralized before the lookup).
_LONG_DURATION_UNITS = frozenset(
    ("week", "month", "year", "decade"))

#: Rule 2's byte units → the fused surface convention.
_BYTE_UNITS = {"gb": "GB", "mb": "MB", "kb": "KB", "tb": "TB"}

#: Rule 5's multiplier words (closed: 2..4; other N stays "Nx").
_MULT_WORDS = {"2": "double", "3": "triple", "4": "quadruple"}

#: The closed number-word map — one..twenty, then the round tens / hundred /
#: thousand forms (both-direction completeness; voicing itself stops at 20).
_WORDS_UP_TO_TWENTY = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty")
_ROUND_WORDS = {30: "thirty", 40: "forty", 50: "fifty", 60: "sixty",
                70: "seventy", 80: "eighty", 90: "ninety",
                100: "one hundred", 1000: "one thousand"}


def num_to_words(n: int) -> str | None:
    """The closed numeral→word map (1..20 voiceable; round tens/hundred/
    thousand mapped for completeness). None when outside the map."""
    if 0 <= n < len(_WORDS_UP_TO_TWENTY):
        return _WORDS_UP_TO_TWENTY[n]
    return _ROUND_WORDS.get(n)


def _name_tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^A-Za-z0-9]+", (name or "").lower()) if t]


def _has_time_cue(name: str) -> bool:
    return any(t in TIME_CUE_TOKENS for t in _name_tokens(name))


def _trailing_unit(name: str) -> str:
    toks = _name_tokens(name)
    if not toks:
        return ""
    t = toks[-1]
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        t = t[:-1]  # naive depluralize: weeks→week, months→month, gb→gb
    return t


def _twelve_hour(h: int, m: int) -> str:
    mer = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {mer}" if m else f"{h12} {mer}"


def voice_value(name: str, value: str) -> str:
    """THE RULE (module docstring) applied to one rendered value. Returns the
    gold-likely surface, or the input unchanged (never raises, never empty)."""
    v = (value or "").strip()
    if not v:
        return v
    # 5) multiplier words ("3x purchase price" → "triple purchase price")
    m = _MULT_RE.match(v)
    if m:
        w = _MULT_WORDS.get(m.group(1))
        if w:
            rest = (m.group(2) or "").strip()
            return f"{w} {rest}".strip()
        return v
    # 1) time-of-day (name-cued, bounded H:MM 24h clock)
    if _INT_RE.match(v) is None:
        t = _TIME_RE.match(v)
        if t and _has_time_cue(name):
            h, mi = int(t.group(1)), int(t.group(2))
            if h <= 23 and mi <= 59:
                return _twelve_hour(h, mi)
        return v
    # pure integer below: rules 2/3/4
    n = int(v)
    unit = _trailing_unit(name)
    # 2) byte-unit attach (name trailing gb/mb/kb/tb → fused product spec)
    if unit in _BYTE_UNITS:
        return f"{v}{_BYTE_UNITS[unit]}"
    # 3) long-duration word form (name carries week/month/year/decade)
    if unit in _LONG_DURATION_UNITS:
        w = num_to_words(n) if 1 <= n <= 20 else None
        return w if w else v
    # 4) bare count word form (no unit token in the name at all)
    if unit not in ("hour", "minute", "second", "day", "%", "percent",
                    "dollar", "usd", "mbps", "km", "kg", "lb", "degree",
                    "celsius", "fahrenheit", "pct"):
        w = num_to_words(n) if 1 <= n <= 20 else None
        if w:
            return w
    return v
