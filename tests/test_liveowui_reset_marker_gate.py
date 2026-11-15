# -*- coding: utf-8 -*-
"""THE GLOBAL-RESET MARKER VOCABULARY + EXECUTOR (LIVE-OPENWEBUI FIX 1, 2026-11-13).

THE MEASURED WOUND (DEV/BUGS/family-smoke-20261111/LIVE-OPENWEBUI-DIAGNOSIS.md, the owner's
prod seat): "forget all previous family information and replace with: Christopher Thompson
(Chris), spouse Mara MacPhee (Mars), …" scored GLiNER2 CORRECTION @0.58 but
``classify_intent.correction_marker_gate`` REFUSED it — the note verbatim: "the turn
carries NO explicit old-value/repair marker — a prescription/usage statement is not a
repair; routes STATEMENT (ingest)" — so the whole 16:47–17:16 reset session ingested as
ADDS with ZERO supersession and zero retraction outcomes.

THE FIX (three deterministic pieces, one source of truth):
  1. ``_turn_carries_correction_marker`` and ``_turn_strong_correction_marker`` union the
     closed global-reset shapes ("forget all", "replace with", "start over", "wipe",
     "clear all", "start fresh") from src/extraction/reset_shapes.py — the gate passes →
     the classifier-born CORRECTION routes (and the deterministic-first tier-1 routes it
     without an LLM). Flag-gated by the gate's OWN flag: OFF → the union is invisible
     (byte-parity).
  2. ``detect_global_reset`` — the executor detector: a reset marker AND a replacement
     carrier AND a RECOGNIZED scope noun. Anything else → None (never a guessed
     destructive reset).
  3. ``reset_payload`` — the replacement data after the carrier (the MCP statement
     harvest ingests THIS span, never the imperative header).

No database: POSTGRES_DSN is cleared (the same harness as tests/test_correction_marker_gate.py).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

os.environ.pop("POSTGRES_DSN", None)

import src.api.main as M  # noqa: E402
from src.extraction.reset_shapes import (  # noqa: E402
    detect_global_reset,
    reset_payload,
)

# THE MEASURED PROD SENTENCE (the diagnosis §2, 16:47:46 turn — byte-shaped).
RESET_TURN = (
    "forget all previous family information and replace with: "
    "Christopher Thompson (Chris), spouse Mara MacPhee (Mars), sons Leo 20 and "
    "Marco \"Dev\" 13, daughter Chiara \"Chi\" 10"
)


def test_measured_reset_turn_passes_the_marker_floor():
    """The measured reset sentence clears the classifier-born CORRECTION evidence floor."""
    assert M._turn_carries_correction_marker(RESET_TURN)


def test_measured_reset_turn_is_a_tier1_strong_marker():
    """The deterministic-first router routes the reset turn CORRECTION with no LLM."""
    assert M._turn_strong_correction_marker(RESET_TURN)


@pytest.mark.parametrize("turn", [
    "start over with a blank family profile",
    "clear all my old family information",
    "wipe all my previous pets and replace with: Goose the cat",
    "Let's start fresh with the family information",
])
def test_every_reset_shape_is_a_marker(turn):
    assert M._turn_carries_correction_marker(turn)


def test_marker_floor_unchanged_without_reset_shapes():
    """The pre-existing inventory verdicts are untouched by the union."""
    assert M._turn_carries_correction_marker("Correction: the port changed to 8010")
    assert M._turn_carries_correction_marker("Actually we moved to 12 Birch Lane")
    assert not M._turn_carries_correction_marker("I like pizza on fridays")
    # a PLAIN bounded deletion ("forget my dog") carries the reset inventory's
    # "forget my" member — a genuine removal intent clears the CORRECTION floor.
    assert M._turn_carries_correction_marker("forget my dog") is True


def test_gate_off_is_byte_parity(monkeypatch):
    """CORRECTION_MARKER_GATE OFF → the helper admits every turn (pre-gate behavior)."""
    monkeypatch.setattr(M, "CORRECTION_MARKER_GATE", False)
    assert M._turn_carries_correction_marker("anything at all") is True


def test_strong_marker_gate_off_excludes_reset_union(monkeypatch):
    """Tier-1 OFF → the reset union is invisible (byte-parity): only the base inventory
    (minus 'actually') routes."""
    monkeypatch.setattr(M, "CORRECTION_MARKER_GATE", False)
    assert M._turn_strong_correction_marker(RESET_TURN) is False
    assert M._turn_strong_correction_marker("Correction: the port changed to 8010") is True


# ── the executor detector ────────────────────────────────────────────────────────

def test_detect_global_reset_measured_turn():
    r = detect_global_reset(RESET_TURN)
    assert r is not None
    assert r["scope_category"] == "family"
    assert "Christopher Thompson" in r["payload"]


def test_reset_payload_splits_after_the_carrier():
    p = reset_payload(RESET_TURN)
    assert p.startswith("Christopher Thompson")
    assert "forget all" not in p and "replace with" not in p


def test_detect_declines_plain_deletion_without_carrier():
    """'forget all my pets' is a bounded retraction — never a global reset."""
    assert detect_global_reset("forget all my pets") is None


def test_detect_declines_unrecognized_scope():
    """An unrecognized scope noun never fires the destructive reset."""
    assert detect_global_reset(
        "forget everything and replace with: the new plan") is None


def test_detect_declines_non_reset_carrier_sentence():
    assert detect_global_reset("wipe the camera lens and replace with: a new one") is None


def test_detect_declines_plain_statement():
    assert detect_global_reset("My wife is Mara MacPhee and she prefers Mars") is None
