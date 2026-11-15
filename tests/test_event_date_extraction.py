"""Unit tests for src/extraction/linguistics.py::extract_event_date — the deterministic
spaCy-DATE + numeric-regex → dateparser event-date layer (KEYSTONE temporal ingest fix).

Replicates the eval that proved the approach: with reference 2023-04-10, real conversation
dates resolve against the SESSION (not 2026 wall-clock), and non-dates resolve to None
(never fabricated). Deterministic (dateparser is a rule engine — no ML/embeddings).

  - "2023/04/10 (Mon) 17:50" → 2023-04-10   (year-first numeric, regex net)
  - "March 15th"            → 2023-03-15    (spaCy DATE span)
  - "3/22"                  → 2023-03-22    (numeric regex net — spaCy misses this)
  - "February 10th"        → 2023-02-10
  - "last month"           → 2023-03-10    (relative, RELATIVE_BASE=ref)
  - "three weeks ago"      → 2023-03-20
  - "poke" / "the GPS system" → None       (non-date → dropped, never fabricated)

Skips the model-backed cases cleanly if en_core_web_sm OR dateparser is not installed.

Run: python3 -m pytest tests/test_event_date_extraction.py -q   (tests/ is gitignored → git add -f)
"""
import importlib
import os
from datetime import datetime, timezone

import pytest

import src.extraction.linguistics as ling


def _reload(**env):
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return importlib.reload(ling)


def _layers_available() -> bool:
    """True iff a flag-ON reload can load spaCy NER AND import dateparser."""
    m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    try:
        import dateparser  # noqa: F401
    except Exception:
        return False
    return m._get_nlp_ner() is not None


_HAS_LAYERS = _layers_available()
requires_layers = pytest.mark.skipif(
    not _HAS_LAYERS, reason="en_core_web_sm and/or dateparser not installed in test env"
)

REF = datetime(2023, 4, 10, tzinfo=timezone.utc)


def _iso_date(s):
    """Return the YYYY-MM-DD prefix of an ISO datetime, or None."""
    return s[:10] if s else None


# ─────────────────────────── REAL DATES RESOLVE TO THE SESSION ───────────────────────────

@requires_layers
@pytest.mark.parametrize("text,expected", [
    ("2023/04/10 (Mon) 17:50", "2023-04-10"),   # year-first numeric (regex net)
    ("March 15th", "2023-03-15"),                # spaCy DATE span
    ("3/22", "2023-03-22"),                      # numeric regex net (spaCy misses)
    ("February 10th", "2023-02-10"),
    ("last month", "2023-03-10"),                # relative → RELATIVE_BASE=ref
    ("three weeks ago", "2023-03-20"),           # relative
])
def test_real_dates_resolve_against_reference(text, expected):
    m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    iso, gran = m.extract_event_date(text, REF)
    assert _iso_date(iso) == expected, f"{text!r} → {iso!r}, expected {expected}"
    assert gran in ("day", "month", "year")


@requires_layers
def test_date_embedded_in_a_sentence():
    m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    iso, _ = m.extract_event_date("I bought the car on March 15th at the dealership", REF)
    assert _iso_date(iso) == "2023-03-15"


# ─────────────────────────── NON-DATES → None (NEVER FABRICATED) ───────────────────────────

@requires_layers
@pytest.mark.parametrize("text", ["poke", "the GPS system", "", "   ", "hello there"])
def test_non_dates_return_none(text):
    m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    iso, gran = m.extract_event_date(text, REF)
    assert iso is None, f"{text!r} fabricated a date {iso!r}"
    assert gran is None


# ─────────────────────────── FAIL-SAFE / KILL-SWITCH ───────────────────────────

def test_kill_switch_off_is_noop():
    m = _reload(TEMPORAL_DATE_LAYER="0", LINGUISTIC_LAYER="true")
    assert m.extract_event_date("March 15th", REF) == (None, None)


def test_none_reference_is_noop():
    m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    assert m.extract_event_date("March 15th", None) == (None, None)


def test_determinism_same_input_same_output():
    m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    a = m.extract_event_date("three weeks ago", REF)
    b = m.extract_event_date("three weeks ago", REF)
    assert a == b


# ── MODEL-YEAR DISCRIMINATOR (structural; UD dep + POS) ─────────────────────────────────────────
# A bare 4-digit YEAR premodifying a non-temporal NOUN/PROPN ("2018 Ford Mustang GT", "a 2018 model",
# "my 2015 ThinkPad") is a NUMERIC CLASSIFIER (model/spec year), NOT a TIMEX — it must NOT resolve to
# a frankenstein date (year 2018 + the session reference month/day). A year as pobj of a preposition
# ("in 2018", "born in 1990") or any non-bare-year date ("June 14th") IS a real date and is KEPT.
# Structural discriminator only (UD nummod/nmod/compound/amod dep of NOUN/PROPN head) — no word-list.
@requires_layers
@pytest.mark.parametrize("text", [
    "I have a 2018 Ford Mustang GT",   # nummod → PROPN (product)
    "a 2018 model",                    # nummod → NOUN  (product)
    "my 2015 ThinkPad",                # nmod   → PROPN (product)
    "the 2015 ThinkPad",               # nummod → PROPN (product)
])
def test_year_as_noun_modifier_is_rejected(text):
    m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    iso, gran = m.extract_event_date(text, REF)
    assert iso is None, f"model-year should NOT resolve to a date: {text!r} → {iso}"
    # residue keeps the year token in place (it was never a date span)
    _iso, _gran, residue = m.extract_event_date_and_residue(text, REF)
    assert _iso is None and text.split()[-1] in residue


@requires_layers
@pytest.mark.parametrize("text,expected", [
    ("I bought it in 2018", "2018-01-01"),   # pobj → ADP (real year-date, kept)
    ("born in 1990", "1990-01-01"),          # pobj → ADP (real year-date, kept)
])
def test_year_as_prep_object_is_kept(text, expected):
    m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    iso, gran = m.extract_event_date(text, REF)
    assert iso is not None, f"prepositional year MUST resolve: {text!r}"
    assert _iso_date(iso)[:4] == expected[:4], f"{text!r} → {iso}"


@requires_layers
def test_non_bare_year_date_unaffected_by_discriminator():
    # "June 14th" is not a bare year → the model-year filter must never touch month/day dates.
    m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    iso, gran = m.extract_event_date("I participated on June 14th", REF)
    assert iso is not None and _iso_date(iso)[5:] == "06-14", f"June 14th must be kept → {iso}"


# ── SESSION-REFERENCE (the [Date: …] marker) is the dateparser RELATIVE_BASE, not now() ──
# REGRESSION GUARD (temporal-correction reference bug): a turn prefixed with the LongMemEval
# session marker "[Date: 2023/06/28]\n…" must resolve a partial/relative date in the text against
# the SESSION year, NOT the 2026 wall-clock. The ingest STATEMENT path already threaded this; the
# CORRECTION path (correct_fact: corrected-date, old-date match, temporal_status recompute) was
# still defaulting the reference to now() — so "June 14th" stamped 2026-06-14 and "last Tuesday"
# anchored to this week. The fix routes those through _compute_ingest_reference_and_text(req.text),
# the same helper tested here: it parses the marker into the reference AND strips it from the text.

def test_ingest_reference_helper_parses_marker_and_strips_text():
    """_compute_ingest_reference_and_text: the [Date: …] marker → session reference + stripped text."""
    import src.api.main as m
    ref, text = m._compute_ingest_reference_and_text(
        "[Date: 2023/06/28]\nActually it was on June 14th")
    assert ref.year == 2023, f"marker must set the SESSION year as reference, got {ref.year}"
    assert "[Date:" not in text, f"marker must be stripped from the text, got {text!r}"
    assert "June 14th" in text


def test_ingest_reference_helper_defaults_to_now_without_marker():
    """No marker → reference defaults to wall-clock now() and the text is returned unchanged (fail-safe)."""
    import src.api.main as m
    ref, text = m._compute_ingest_reference_and_text("Actually it was on June 14th")
    assert ref.year == datetime.now(timezone.utc).year
    assert text == "Actually it was on June 14th"


@requires_layers
def test_correction_partial_date_anchors_to_session_year():
    """A bare month-day correction resolved against the marker reference lands in the SESSION year,
    NOT 2026 — the exact event_date=2026-06-14 bug. (Mirrors the correct_fact resolution path.)"""
    import src.api.main as m
    ling_m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    ref, _stripped = m._compute_ingest_reference_and_text(
        "[Date: 2023/06/28]\nactually the conference was on June 14th")
    iso, gran = ling_m.extract_event_date("June 14th", ref)
    assert iso is not None and _iso_date(iso) == "2023-06-14", (
        f"corrected date must anchor to the session year 2023, got {iso}")


@requires_layers
def test_correction_relative_date_anchors_to_session_date():
    """A relative correction ("last Tuesday") resolves against the marker reference, not this week."""
    import src.api.main as m
    ling_m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    ref, _stripped = m._compute_ingest_reference_and_text(
        "[Date: 2023/03/10]\nno, I adopted the dog last Tuesday")  # 2023-03-10 is a Friday
    iso, gran = ling_m.extract_event_date("last Tuesday", ref)
    assert iso is not None and _iso_date(iso) == "2023-03-07", (
        f"'last Tuesday' must resolve to the session-relative 2023-03-07, got {iso}")


# ── RELATIVE-CUE RECALL NET (spaCy DATE-NER context-miss recovery) ────────────────────
# spaCy's CNN DATE NER drops a trailing deictic ("today") when it sits after a long ORG run it
# grabbed first ("…at the Metropolitan Museum of Art today"). The growable relative-cue overlay
# recovers it — gated by spaCy's OWN isolated DATE judgment so bare modifier cues ("now", "this",
# "week") never become a spurious event date. (LongMemEval gpt4_59149c77 temporal-reasoning.)
@requires_layers
@pytest.mark.parametrize("text,expected", [
    ("I attended the 'Ancient Civilizations' exhibit at the Metropolitan Museum of Art today",
     "2023-04-10"),
    ("I just visited the Museum of Modern Art in New York City today", "2023-04-10"),
    ("I saw the show at the Metropolitan Opera House yesterday", "2023-04-09"),
])
def test_deictic_recovered_after_long_org_run(text, expected):
    """A deictic date trailing a long ORG span is recovered against the session reference."""
    ling_m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    iso, gran = ling_m.extract_event_date(text, REF)
    assert iso is not None and _iso_date(iso) == expected, (
        f"deictic must resolve to the session date {expected}, got {iso!r} for {text!r}")


@requires_layers
@pytest.mark.parametrize("text", [
    "right now I am learning python",
    "I want to research this model further",
    "I work forty hours a week",
    "I have a current project at work",
    "the latest version is better",
])
def test_bare_modifier_cue_does_not_fabricate_a_date(text):
    """A bare relative MODIFIER cue ("now"/"this"/"week"/"current"/"latest") — a date only in
    composition — must NOT each become a standalone event date (the over-propose guard)."""
    ling_m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    iso, gran = ling_m.extract_event_date(text, REF)
    assert iso is None, f"bare modifier cue fabricated a date {iso!r} for {text!r}"


# ── RECENCY-DEIXIS ANCHOR (bare immediate "just" — no DATE ent → the session day) ─────────────
# A narrated event whose ONLY temporal cue is the bare immediate-recency deictic "just" ("I just
# helped my cousin ...", "I just got back ...") has NO spaCy DATE entity and no dateparser
# resolution, so it stored UNDATED and could not be ordered against a sibling event carrying an
# explicit "today" (LongMemEval gpt4_f49edff3 temporal-reasoning root cause). "just" (= "a moment
# ago") denotes the utterance instant → day-granular it IS the session reference day. The
# FOCUS/quantity "just" ("I just want coffee" — present verb; "it's just a scratch" — nominal head)
# is excluded morphologically; the VAGUE "recently" is deliberately left undated (see below).
@requires_layers
@pytest.mark.parametrize("text,expected", [
    ("I just helped my cousin pick out some stuff for her baby shower", "2023-04-10"),
    ("I just ordered a customized phone case for my friend's birthday", "2023-04-10"),
    ("I just got back from the tour", "2023-04-10"),
    ("I just moved to Toronto", "2023-04-10"),
])
def test_recency_deixis_anchors_to_session_day(text, expected):
    """The bare immediate-recency deictic 'just' on a PAST verb anchors to the session day."""
    ling_m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    iso, gran = ling_m.extract_event_date(text, REF)
    assert iso is not None and _iso_date(iso) == expected, (
        f"recency deictic must resolve to the session date {expected}, got {iso!r} for {text!r}")


@requires_layers
@pytest.mark.parametrize("text", [
    "I just want coffee",           # focus 'just' on a PRESENT verb — not a recent event
    "it is just a scratch",         # nominal 'just' — non-verb head
    "I can just about see it",      # degree 'just' — no past eventive head
    "I changed jobs recently",      # VAGUE 'recently' — unbounded window, deliberately undated
    "a while ago I moved to Calgary",  # VAGUE 'a while ago' — deliberately undated
])
def test_non_immediate_recency_does_not_fabricate_a_date(text):
    """The focus/degree 'just' AND the VAGUE relatives ('recently'/'a while ago') must NOT anchor a
    date — only the IMMEDIATE 'just' on a PAST verb does (the immediate-recency boundary)."""
    ling_m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    iso, gran = ling_m.extract_event_date(text, REF)
    assert iso is None, f"non-immediate recency fabricated a date {iso!r} for {text!r}"


def teardown_module(_module):
    """Restore default env so other test modules see the flags ON."""
    _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
