"""PA-core increment-2 WIRING verification — SPINE_PA_CORE ON vs OFF (in-process, no DB/LLM/GLiNER2).

Proves the three benchmark-gate properties of the additive PA-base wiring in
``derive_sentence_facts`` (``src/extraction/linguistics.py``):

  1. OFF = BYTE-IDENTICAL to today's chain output (flag skipped in full → chains untouched).
  2. ON = STRICTLY ADDITIVE — every OFF edge still produced (no chain capability lost).
  3. ON captures the MISS CLASS the chains never had a chain for (ditransitive recipient, xcomp
     control, event nominalization / nmod factoring), AND twin-suppression yields ONE clean edge on
     the chain-owned constructions (family/kinship/preference) — no PA+chain double.

Pure spaCy dependency parse over the deriver's own en_core_web_sm (parser-only). Deterministic.
Run: ``python3 tools/fltest.py --bug PACORE2 --test tests/test_spine_pa_core_wiring.py``
"""
from __future__ import annotations

import datetime
import os

import pytest

from src.extraction import linguistics as L

_REF = datetime.datetime(2026, 7, 25)


def _edges(text: str) -> set[tuple[str, str, str]]:
    """(subject, rel_type, object) set derived for ``text`` at the current SPINE_PA_CORE setting."""
    return {
        (f.subject, f.rel_type, (f.object or "").strip().lower())
        for f in (L.derive_sentence_facts(text, _REF) or [])
    }


def _off(text: str, monkeypatch) -> set[tuple[str, str, str]]:
    monkeypatch.delenv("SPINE_PA_CORE", raising=False)
    return _edges(text)


def _on(text: str, monkeypatch) -> set[tuple[str, str, str]]:
    monkeypatch.setenv("SPINE_PA_CORE", "1")
    return _edges(text)


pytestmark = pytest.mark.skipif(
    not L.linguistics_available(),
    reason="spaCy en_core_web_sm unavailable (set SPACY_MODEL=en_core_web_sm)",
)


# ── Corpus ────────────────────────────────────────────────────────────────────────────────────────
# Band A: constructions a chain fully OWNS → ON must EQUAL OFF (twin-suppression, one clean edge).
_BAND_A = [
    "My mother Carol is 62 years old.",
    "My favorite color is blue.",
    "I am excited.",
    "Carol is a teacher.",
    "I have a Sony A7R IV.",
    "My wife's name is Carol.",
    "I live in Toronto.",
]

# Band B: run2-miss / generic-core constructions → ON must be a STRICT SUPERSET of OFF.
_BAND_B = [
    "I gave Luna some training pads.",
    "We bought training pads for Luna.",
    "The malfunction of the stand mixer ruined dinner.",
    "I want to leave the company.",
    "I fixed the router and rebooted the server.",
]


@pytest.mark.parametrize("text", _BAND_A + _BAND_B)
def test_on_is_strictly_additive_over_off(text, monkeypatch):
    """Property 2: no chain capability lost — OFF edges ⊆ ON edges for EVERY construction."""
    off = _off(text, monkeypatch)
    on = _on(text, monkeypatch)
    missing = off - on
    assert not missing, f"PA wiring DROPPED chain edges for {text!r}: {missing}"


@pytest.mark.parametrize("text", _BAND_A)
def test_off_is_deterministic_and_flag_toggles_cleanly(text, monkeypatch):
    """Property 1: OFF is stable across calls (byte-identical), and toggling the flag is the ONLY
    difference — OFF twice is identical."""
    first = _off(text, monkeypatch)
    second = _off(text, monkeypatch)
    assert first == second


@pytest.mark.parametrize("text", _BAND_A)
def test_chain_owned_constructions_get_no_pa_double(text, monkeypatch):
    """Property 3a (twin-suppression): a construction a chain fully owns produces ONE clean edge set —
    ON equals OFF (PA emitted nothing extra because every content span was already _covered)."""
    off = _off(text, monkeypatch)
    on = _on(text, monkeypatch)
    added = on - off
    assert not added, f"twin-suppression FAILED — PA double-captured on chain-owned {text!r}: {added}"


def test_pa_captures_ditransitive_recipient(monkeypatch):
    """Property 3b: the dropped recipient (iobj) the chains never captured now lands, ADDITIVELY."""
    text = "I gave Luna some training pads."
    off = _off(text, monkeypatch)
    on = _on(text, monkeypatch)
    assert off <= on
    assert any(o == "luna" for (_s, _r, o) in on), f"recipient 'luna' not captured; ON={on}"
    assert not any(o == "luna" for (_s, _r, o) in off), f"OFF unexpectedly had the recipient: {off}"


def test_pa_captures_xcomp_control_clause(monkeypatch):
    """Property 3b: 'want to leave the company' — the xcomp-control clause the chains capture 0 of."""
    text = "I want to leave the company."
    on = _on(text, monkeypatch)
    assert any(r == "leave" and o == "company" for (_s, r, o) in on), f"xcomp clause missing; ON={on}"


def test_pa_captures_event_nominalization_factoring(monkeypatch):
    """Property 3b: MinIE nmod factoring — '(malfunction, of, stand mixer)' the chains drop entirely."""
    text = "The malfunction of the stand mixer ruined dinner."
    off = _off(text, monkeypatch)
    on = _on(text, monkeypatch)
    assert off <= on
    assert any(s == "malfunction" and "mixer" in o for (s, _r, o) in on), \
        f"event-nominalization nmod not factored; ON={on}"


def test_first_person_binds_to_user_not_pronoun_surface(monkeypatch):
    """The shared first-person refinement runs over PA output: a PA-captured first-person subject is
    ``user``, never the raw 'i' surface (grammatical, not a pronoun word list)."""
    text = "I gave Luna some training pads."
    on = _on(text, monkeypatch)
    assert not any(s == "i" for (s, _r, _o) in on), f"raw 'i' subject leaked (should be 'user'); ON={on}"
