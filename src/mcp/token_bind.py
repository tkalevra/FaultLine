"""Question-token → stored-row binding (wound-map round-5, the map100 ranked fixes).

PURE module — no DB, no imports from server/main (both import THIS; importing either
back would be circular). Everything here is deterministic string work so every arm is
unit-pinnable and every flag toggles byte-parity at the CALL SITES.

THE MEASURED WOUND (tools/longmemeval/runs/map100/REPORT.md): all 50 pilot golds are
stored structurally (27 facts / 23 entity_attributes); the 16% score is pure
serve-side — binding/anchor 21, fetch 9, render 12. The questions NAME their topic
("handbag", "volunteer", "study abroad", "last name") while the refusal gates never
match a token to a stored row's NAME (attribute name, fact rel_type, object alias).

THE MATCHER: tokenize the question (closed stopword set), normalize + stem each
token, and match stems against the row-name's stems — equal, or one a prefix of the
other at >=4 chars (catch→caught, study→studied, bike→bikes via depluralize).
Ultra-generic question words ("many", "long", "last" …) are WEAK: a weak-only match
scores below the fire floor, so "how many" never binds a row by itself — a content
noun ("handbag", "bikes", "volunteer", "degree") must do the binding.
"""

from __future__ import annotations

import re

__all__ = [
    "normalize_token", "stem", "query_weights", "surface_stems",
    "stems_match", "score_surface", "best_row", "lead_line",
    "move_top_overlap_lead", "MIN_FIRE_SCORE",
]

_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\\-]*")
_CLITIC_RE = re.compile(r"'s$|s'$|'$", re.IGNORECASE)

# Closed grammatical stopword set for QUERY-side content tokens (mirrors the
# self-verify scorer's list — grammatical function words, never domain vocabulary).
_STOPWORDS = frozenset("""
a an the this that these those of in on at to for from with without by as is are was
were be been being am do does did doing have has had having i me my we our you your
he she it they them their and or but if then else so than not no nor too very just
also which who whom what when where why how about into onto over under again more
most some any each few other such only own same off out up down did
""".split())

# WEAK question words: grammatically common but topically empty — a match on these
# ALONE (score 0.5 < MIN_FIRE_SCORE) never binds; they only top up a real content
# match. Closed grammar class, not a domain list.
_WEAK_WORDS = frozenset((
    "many", "much", "long", "old", "new", "last", "first", "next", "time", "way",
    "use", "used", "like", "type", "kind", "get", "got", "take", "make", "say",
    "said", "tell", "told", "record", "spend", "spent", "pay", "paid",
))

MIN_FIRE_SCORE = 1.0


def normalize_token(tok: str) -> str:
    """Lowercase, strip punctuation, naive deterministic depluralize (the scorer's
    rule, inlined — batteries→battery, boxes→box, cars→car)."""
    t = (tok or "").lower().strip().strip("'\"“”‘’.,;:!?()[]{}")
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        if t.endswith("ies") and len(t) > 4:
            t = t[:-3] + "y"
        elif t.endswith("es") and t[-3] in "sxzo" or t.endswith("ches") or t.endswith("shes"):
            t = t[:-2]
        else:
            t = t[:-1]
    return t


def stem(tok: str) -> str:
    """normalize_token + closed suffix fold: -ing/-ed (and the orthographic
    -ied→-y), then trailing y→i — so study/studied→'studi', catch/caught meet via
    the prefix rule in stems_match instead of here."""
    t = normalize_token(tok)
    if len(t) > 4 and t.endswith("ing"):
        t = t[:-3]
    elif len(t) > 4 and t.endswith("ied"):
        t = t[:-3] + "y"
    elif len(t) > 4 and t.endswith("ed"):
        t = t[:-2]
    if len(t) > 3 and t.endswith("y"):
        t = t[:-1] + "i"
    if len(t) > 3 and t.endswith("e"):
        t = t[:-1]
    return t


def query_weights(query: str) -> list[tuple[str, float]]:
    """The question's bindable content tokens as (stem, weight) — clitic-stripped,
    stopword-filtered, weak-word down-weighted. Order-preserving, de-duplicated."""
    out: list[tuple[str, float]] = []
    seen: set[str] = set()
    for raw in _WORD_RE.findall(query or ""):
        bare = _CLITIC_RE.sub("", raw)
        n = normalize_token(bare if len(bare) >= 2 else raw)
        if not n or n in _STOPWORDS or not any(c.isalnum() for c in n):
            continue
        if n in seen:
            continue
        seen.add(n)
        out.append((stem(n), 0.5 if n in _WEAK_WORDS else 1.0))
    return out


def surface_stems(surface: str) -> list[str]:
    """The bindable stems of a row-name surface (attr name / rel_type / alias)."""
    return [stem(w) for w in re.split(r"[^A-Za-z0-9]+", (surface or "").lower()) if w]


def stems_match(a: str, b: str) -> bool:
    """Equal stems, or one a >=4-char prefix of the other (caught⊃catch,
    studied⊃studi). Never matches empty or sub-3 stems."""
    if not a or not b or len(a) < 3 or len(b) < 3:
        return False
    if a == b:
        return True
    if len(a) >= 4 and len(b) >= 4 and (a.startswith(b) or b.startswith(a)):
        return True
    return False


def score_surface(surface: str, qw: list[tuple[str, float]]) -> float:
    """Weighted question↔surface-name overlap (each question stem counts once)."""
    if not qw:
        return 0.0
    nstems = surface_stems(surface)
    if not nstems:
        return 0.0
    return sum(w for q, w in qw
               if any(stems_match(q, ns) for ns in nstems))


def matched_stems_len(surface: str, qw: list[tuple[str, float]]) -> int:
    """Total length of the question stems this surface NAME matched — the
    specificity tie-break ("commute" ⊐ "work" when both score 1)."""
    nstems = surface_stems(surface)
    return sum(len(q) for q, _w in qw
               if any(stems_match(q, ns) for ns in nstems))


def best_row(rows, qw, min_score: float = MIN_FIRE_SCORE):
    """The best-binding (name_surface, value) row, or None below the fire floor.

    TWO-TIER: a row whose NAME surface (attr name / rel_type) binds is a DIRECT
    answer candidate and ALWAYS outranks a value-only match — the map measured the
    value-cue trap ("how much was my designer handbag?" binding the owns_item
    topic row 'designer handbag' over handbag_price_usd=800: the value describes
    the TOPIC, the name carries the ASPECT). Within a tier: score desc, then the
    SPECIFICITY of the matched stems (commute ⊐ work at equal score — the map's
    occupation-dump class), then the RICHER name (volunteer_event_date ⊐
    volunteers_at — the answer-bearing column over the topical rel at equal
    score), then lexical determinism."""
    best = None  # (key, name, value)
    for name, value in rows:
        ns = score_surface(name, qw)
        vs = score_surface(str(value or ""), qw)
        if ns >= min_score:
            key = (0, -ns, -matched_stems_len(name, qw),
                   -len(surface_stems(name)), str(name))
        elif vs >= min_score:
            key = (1, -vs, -matched_stems_len(str(value or ""), qw),
                   -len(surface_stems(name)), str(name))
        else:
            continue
        if best is None or key < best[0]:
            best = (key, name, value)
    if best is None:
        return None
    return (best[1], best[2])


def lead_line(name: str, value) -> str:
    """The plain-answers answer-framed lead (SERVE_PLAIN_ANSWERS voice): the bound
    row's name humanized, the stored VALUE verbatim."""
    return f"You {str(name).replace('_', ' ').strip()}: {value}"


def line_score(line: str, qw: list[tuple[str, float]]) -> float:
    """Overlap of a RENDERED line (attr 'name: value' or fact definition prose)."""
    return score_surface(line, qw)


def move_top_overlap_lead(query: str, lines: list[str]) -> bool:
    """map100 fix #2 (SERVE_RENDER_TOKEN_RANK): move the top question-overlap line
    to the FRONT of the band, only when it STRICTLY beats the current lead (ties and
    weak-only scores keep today's order — byte-parity on no-clear-winner). In-place;
    returns whether a move happened."""
    qw = query_weights(query)
    if not qw or not lines:
        return False
    scores = [line_score(ln, qw) for ln in lines]
    best_i = max(range(len(lines)), key=lambda i: (scores[i], -i))
    if scores[best_i] < MIN_FIRE_SCORE or best_i == 0:
        return False
    if scores[best_i] <= scores[0]:
        return False
    lines.insert(0, lines.pop(best_i))
    return True
