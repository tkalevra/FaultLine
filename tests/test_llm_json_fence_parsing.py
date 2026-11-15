"""A fenced JSON payload is a SUCCESS, not a parse failure.

WHY THIS FILE EXISTS. Models routinely return ```` ```json\\n[...]\\n``` ```` even when the
prompt says "return ONLY the JSON array" — DeepSeek, GPT, Claude and Qwen all do it
intermittently. Our parser did `json.loads(content)` and, on failure, searched for an OBJECT
(`{...}`). Neither can read a fenced payload, and the object-only salvage regex cannot rescue
the ARRAY shape the extraction prompt actually asks for.

The consequence was not a cosmetic log line. A chunk containing no extractable facts
legitimately returns `[]`; fenced, that was recorded as `no_json_in_response` — a hard "the
model gave us nothing" verdict — retried to exhaustion, and fed the CIRCUIT BREAKER. Measured
on pre-prod 2026-07-30 during a document-ingest run: 53 `circuit_breaker_open` events and 18
chunk timeouts in four minutes, on a 128-chunk document whose chunks were mostly fine. Once
the breaker opened, every good chunk after it was rejected — silent document loss with a
healthy-looking API.

So this guards two things:
  1. a fenced payload parses (the fence is stripped BEFORE json.loads), and
  2. a top-level ARRAY is recoverable when embedded in prose (the shape we request).
"""
import json

import pytest

from src.api.llm_calls import _unfence


@pytest.mark.parametrize("raw,expected,why", [
    # THE case that stalled document ingest.
    ("```json\n[]\n```", "[]", "fenced EMPTY extraction — a valid 'no facts here' result"),
    ('```json\n[{"subject":"user","rel_type":"owns","object":"car"}]\n```',
     '[{"subject":"user","rel_type":"owns","object":"car"}]', "fenced triple array"),
    ('```\n{"a":1}\n```', '{"a":1}', "fenced object with no language tag"),
    ("```JSON\n[]\n```", "[]", "uppercase language tag"),
    ("```json\n[]```", "[]", "no trailing newline before the closing fence"),
])
def test_fence_is_stripped(raw, expected, why):
    assert _unfence(raw) == expected, why


@pytest.mark.parametrize("raw", ["[]", '{"a":1}', "", "no json here at all", "   "])
def test_unfenced_content_is_untouched(raw):
    """Must be a no-op on anything that is not fenced — including prose and empty input."""
    assert _unfence(raw) == raw


def test_fenced_empty_array_now_parses_instead_of_failing():
    """The regression that matters: `[]` must round-trip to an empty list, not a parse error.

    Before the fix this path produced `no_json_in_response` → `unparseable_llm_response` →
    retries → circuit-breaker failure. An empty extraction is a legitimate outcome and must
    never be counted as an LLM failure.
    """
    assert json.loads(_unfence("```json\n[]\n```")) == []


def test_fenced_triple_array_survives_round_trip():
    payload = '```json\n[{"subject":"diane","rel_type":"parent_of","object":"user"}]\n```'
    parsed = json.loads(_unfence(payload))
    assert len(parsed) == 1
    assert parsed[0]["rel_type"] == "parent_of"


def test_unfence_never_raises_on_odd_input():
    """Fail-safe: parsing helpers must not become a new failure source."""
    for weird in ("```", "``````", "```json", "```json\n", None, "```json\n[[[\n```"):
        try:
            _unfence(weird)  # type: ignore[arg-type]
        except Exception as exc:  # pragma: no cover
            pytest.fail(f"_unfence raised on {weird!r}: {exc!r}")


def test_array_fallback_regex_is_present_at_every_parse_site():
    """All THREE response-parse paths need the array fallback, not just the one you tested.

    sync_retry / async_retry / no_retry each have their own copy of the salvage block; a fix
    applied to two of three leaves the bug live on the third (the write-path-audit lesson).
    """
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "api" / "llm_calls.py"
    text = src.read_text()
    assert text.count("ARRAY fallback") == 3, "expected the array fallback at all 3 parse sites"
    assert text.count("_unfence(content)") == 3, "expected unfence at all 3 parse sites"
