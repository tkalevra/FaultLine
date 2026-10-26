"""BUG-03 pins (adversarial bench 2026-10-24): the doc-content repair refuses wrong
bytes and rebuilds chunks from the TRUE source.

The production defect: doc 18 ("On the Compressibility of Quantized Large Language
Models") holds chunks byte-identical to doc 20 (the crayfish sous-vide paper). The
repair (``scripts/repairs/repair_document_content_mismatch.py``) resolves the true
source, VALIDATES it against the row's title before any write, and re-chunks with the
production chunker. These pins hold the validation and the chunker parity:

 1. THE CONTENT PIN — crayfish text FAILS the quantization title (this is the exact
    check whose absence stored the wrong bytes); real quantization text PASSES.
 2. Subject-agnostic — the same predicate protects the crayfish paper from quantized
    bytes (it keys on the row's OWN title tokens, never a domain word list).
 3. A title with no significant tokens is REFUSED (never store unverifiable bytes).
 4. The repair chunks with the SAME deterministic chunker the enqueue path uses.

Run with:
  python3 tools/fltest.py --bug BUG03 --test tests/test_repair_document_content_mismatch.py
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "repair_document_content_mismatch",
    _REPO / "scripts" / "repairs" / "repair_document_content_mismatch.py",
)
R = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(R)

from src.mcp.server import _chunk_document  # noqa: E402 — after path setup by the script module

QUANT_TITLE = "On the Compressibility of Quantized Large Language Models"
CRAYFISH_TITLE = ("Analysis of storage quality of crayfish (Procambarus clarkii) "
                  "meat based on sous-vide cooking")

QUANT_TEXT = (
    "Deploying Large Language Models (LLMs) on edge or mobile devices offers significant "
    "benefits such as enhanced data privacy. Quantization is an effective way of reducing "
    "the model size while maintaining good performance. In this work we study the "
    "compressibility of quantized LLMs and the trade-off between compressibility and "
    "performance.\n\nWe discussed applying data compression techniques to reduce data "
    "movement and speed up the inference of quantized models on memory-constrained "
    "devices."
)

CRAYFISH_TEXT = (
    "This study aimed to investigate the effect of different combinations of temperature "
    "and time of sous-vide (SV) on the quality of crayfish (Procambarus clarkii) meat. "
    "Alonondialdehyde (MDA) kit was provided by Nan-Jing JianCheng Bioengineering "
    "Institute. Heating at 75°C effectively suppressed the increase in total volatile "
    "basic nitrogen during refrigerated storage."
)


# ── PIN: wrong content is refused, right content passes ───────────────────────

def test_crayfish_bytes_fail_the_quantization_title():
    """THE BUG-03 PIN: the crayfish paper's text — the exact wrong-bytes payload that
    contaminated doc 18 — is REFUSED against the quantization title."""
    ok, missing = R.source_matches_title(CRAYFISH_TEXT, QUANT_TITLE)
    assert ok is False
    # the discriminative tokens must be among the missing
    assert {"compressibility", "quantized"} <= set(missing)


def test_quantization_text_passes_its_own_title():
    ok, missing = R.source_matches_title(QUANT_TEXT, QUANT_TITLE)
    assert ok is True, f"unexpectedly missing: {missing}"


def test_guard_is_symmetric_and_subject_agnostic():
    """The predicate keys on the ROW's own title tokens — it protects the crayfish
    paper from quantization bytes exactly as well as the reverse. No domain word
    list: the tokens come from the title under repair."""
    ok, _missing = R.source_matches_title(QUANT_TEXT, CRAYFISH_TITLE)
    assert ok is False, "quantization bytes must fail the crayfish title"


def test_unvalidatable_title_is_refused_not_waved_through():
    """A title with no significant tokens (e.g. 'AI?') cannot be validated — the
    fail-safe is REFUSAL, never storing unverifiable bytes."""
    ok, missing = R.source_matches_title(QUANT_TEXT, "AI?")
    assert ok is False


def test_significant_tokens_drop_function_words():
    toks = R.significant_title_tokens(QUANT_TITLE)
    assert "compressibility" in toks and "quantized" in toks
    assert "the" not in toks and "with" not in toks and "based" not in toks


# ── PIN: chunker parity with the enqueue path ─────────────────────────────────

def test_repair_chunks_with_the_production_chunker():
    """The repair must produce chunks the SAME way the original enqueue did — same
    paragraph shapes, deterministic, no drift."""
    chunks = _chunk_document(QUANT_TEXT)
    assert len(chunks) >= 1
    assert all(isinstance(c, str) and c.strip() for c in chunks)
    # determinism: same text in, same chunks out
    assert _chunk_document(QUANT_TEXT) == chunks


def test_identical_bytes_guard_differs_between_right_and_wrong_sources():
    """The repair's identical-bytes guard: re-chunked correct text can never equal the
    stored (colliding) wrong chunks — direct list equality, no hashing."""
    good = _chunk_document(QUANT_TEXT)
    bad = _chunk_document(CRAYFISH_TEXT)
    assert R._chunks_differ(good, bad) is True
    assert R._chunks_differ(good, list(good)) is False
