r"""NOW reference derivation — the single ingest temporal anchor (fix #1b).

THE PRINCIPLE: DERIVE NOW (the ingest reference) ONCE, then resolve EVERY temporal anchor
against it. The reference is:

  • the leading harness ``[Date: ...]`` marker when present (benchmark session reference),
  • else the ingest SYSTEM TIME (wall-clock ``now()``) — the prod default when the REST
    ``/remember_facts`` path passes none.

NEVER wall-clock an UN-anchored fact (that is the event_date resolver's job — a miss → NULL);
this is only the REFERENCE the relatives resolve against. Deterministic, fail-safe.

This module is the single source of truth; ``main._compute_ingest_reference_and_text``
delegates here so the marker semantics live in ONE place (the temporal module).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import structlog

log = structlog.get_logger()

_MARKER_RE = re.compile(r"\s*\[Date:\s*([^\]]+)\]")
_MARKER_STRIP_RE = re.compile(r"^\s*\[Date:[^\]]*\]\s*")


def derive_now(req_text: str | None = None):
    """Resolve the ingest NOW reference + the marker-stripped text for date extraction.

    Returns ``(reference: tz-aware datetime, text_for_temporal: str)``:
      • ``reference`` — the ``[Date: ...]`` marker instant when ``req_text`` carries one,
        else the ingest system time (``datetime.now(utc)``). Always tz-aware.
      • ``text_for_temporal`` — ``req_text`` with the leading marker stripped (the marker is
        the REFERENCE only, never an event date), or ``""`` when ``req_text`` is empty.

    Fail-safe: any error → ``(now_utc, req_text)``. NO ML/LLM (dateparser is a rule engine)."""
    reference = datetime.now(timezone.utc)
    text_for_temporal = req_text or ""
    if not req_text:
        return (reference, text_for_temporal)
    try:
        m = _MARKER_RE.match(req_text)
        if m:
            import dateparser  # deferred: rule engine, no ML
            dt = dateparser.parse(
                m.group(1).strip(),
                settings={"PREFER_DATES_FROM": "past", "DATE_ORDER": "MDY"},
            )
            if dt is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                reference = dt
    except Exception as e:  # noqa: BLE001 — fail-safe: keep system-time now
        log.warning("temporal.derive_now_marker_failed", error=str(e)[:120])
    try:
        text_for_temporal = _MARKER_STRIP_RE.sub("", req_text, count=1)
    except Exception:  # noqa: BLE001
        text_for_temporal = req_text
    return (reference, text_for_temporal)


def now_utc():
    """The ingest system-time reference (tz-aware UTC). The prod default when no marker /
    no explicit reference is supplied — fix #1b: relatives resolve against THIS, never NULL."""
    return datetime.now(timezone.utc)
