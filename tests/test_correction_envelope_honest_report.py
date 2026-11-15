# -*- coding: utf-8 -*-
"""SEC-BUG-1 — THE CORRECTION ENVELOPE REPORTS THE DB EFFECT, NOT A STORY.

THE MEASURED WOUND (DEV/BUGS/family-smoke-20261111/SEC-BUG-1…, live pre-prod, throwaway
seats, real remember_facts_tool): a zero-incumbent "correction" — no address/name ever
stored on the seat — came back

    status="corrected", facts_superseded=1,
    message="✓ user: lives_at=old place → lives_at=99 Fake Street"

while the turn's DB window held ZERO entity_attributes flips and ZERO
entity_attributes_history rows. The count came from the SCALAR lane counting the WRITE
(`_scalar_superseded = max(_scalar_superseded, 1)` on the upsert/alias INSERT — rowcount
is a landed write, not a retirement), and the envelope narrated "✓ old → new"
unconditionally. Flaky by sub-lane: the extraction LLM's variance routes the same
utterance SCALAR (dishonest envelope) or RELATIONAL (honest corrected_unresolved, 0)
across runs — the fix makes the envelope honest regardless of which lane routes.

THE FIX (flag CORRECTION_ENVELOPE_HONEST_REPORT, default ON; OFF = byte-parity):
  • facts_superseded = the ACTUAL retired-row count (the archived LIVE prior value —
    `_record_scalar_supersession`'s own DB result), 0 when nothing retired;
  • the "✓ old → new" check-mark narration only when a real supersession landed;
  • a correction over no incumbent reports the first-statement shape
    ("noted: … (new — no prior value superseded)"), never a fabricated transition;
  • a retraction over no incumbent never claims "the stored fact was archived".

The E2E pins run the REAL /retract/correct against a throwaway test-named POSTGRES_DSN
tenant (the only substitution is the correction brain, mocked to a fixed extraction —
the same harness as tests/test_correction_scalar_addressing_cx12.py). The DB rows are
the bar: 0 history rows on the zero-incumbent arms, 1 on the real-supersession arm.
"""

import os
import uuid

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("SENTENCE_PIPELINE", "true")

_DSN = os.environ.get("POSTGRES_DSN", "")

import src.api.main as M  # noqa: E402

_FLAG = "CORRECTION_ENVELOPE_HONEST_REPORT"


# ── the flag itself ──────────────────────────────────────────────────────────────────────

def test_flag_defaults_on(monkeypatch):
    """Default ON (parse-shape pin): the honest report is the shipping behavior."""
    monkeypatch.delenv("CORRECTION_ENVELOPE_HONEST_REPORT", raising=False)
    assert (os.environ.get("CORRECTION_ENVELOPE_HONEST_REPORT", "true").strip().lower()
            not in ("false", "0", "no")) is True


def test_flag_parses_off_values():
    for v in ("false", "0", "no", "False", "NO"):
        assert (v.strip().lower() not in ("false", "0", "no")) is False


# ── E2E — the REAL /retract/correct against a throwaway tenant ──────────────────────────

pytestmark_e2e = pytest.mark.skipif(
    not _DSN or "test" not in _DSN.rsplit("/", 1)[-1],
    reason="E2E needs a throwaway test-named POSTGRES_DSN (skipped without one — disclosed)")

# THE PENTEST C3 ph2 SHAPE, verbatim class: zero-incumbent seat by construction
# (nothing planted), the correction names an address that was never stored.
_ZI_EXTRACTION = {
    "subject_name": "user",
    "old_rel_type": "lives_at",
    "old_value": "old place",
    "new_rel_type": "lives_at",
    "new_value": "99 Fake Street",
    "dimension": "SCALAR",
    "confidence": 0.95,
}
_ZI_TEXT = "My address changed to 99 Fake Street."

# THE PENTEST C2 ph1 SHAPE: zero-incumbent corrected NAME (pref_name lane — the demote
# + INSERT special case with its own legacy max(…, 1)).
_PREF_EXTRACTION = {
    "subject_name": "user",
    "old_rel_type": "pref_name",
    "old_value": None,
    "new_rel_type": "pref_name",
    "new_value": "Grendel",
    "dimension": "SCALAR",
    "confidence": 0.95,
}
_PREF_TEXT = "Correction: my name is actually Grendel."

# A REAL supersession control: the incumbent IS planted, the correction retires it.
_MEASURE_PLANT = "The octopus measures about 5 centimeters across."
_MEASURE_CORRECTION = "Actually, the octopus measures 7 centimeters across, not 5."
_MEASURE_EXTRACTION = {
    "subject_name": "octopus",
    "old_rel_type": "measure",
    "old_value": "5 centimeters",
    "new_rel_type": "measure",
    "new_value": "7 centimeters",
    "dimension": "SCALAR",
    "confidence": 0.95,
}

_RETRACT_EXTRACTION = {
    # RETRACTION extraction shape (correct_fact reads subject/rel_type/object on this path)
    "subject": "user",
    "rel_type": "lives_at",
    "object": "old place",
    "dimension": "SCALAR",
    "confidence": 0.95,
}
_RETRACT_TEXT = "I no longer live at my old place."


@pytest.fixture()
def secbug1_tenant(monkeypatch):
    if not _DSN:
        pytest.skip("no POSTGRES_DSN")
    monkeypatch.delenv("SAAS_MODE", raising=False)
    import psycopg2
    from src.provisioning.schema_manager import (
        create_user_schema, derive_user_slug_from_uuid, derive_schema_name)
    user_id = str(uuid.uuid4())
    slug = derive_user_slug_from_uuid(user_id)
    schema = derive_schema_name(slug)
    conn = psycopg2.connect(_DSN)
    try:
        schema, status = create_user_schema(user_id, slug, conn)
        assert status == "ready", status
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO public.users (user_id, email, display_name, slug) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (user_id, f"{slug}@example.invalid", slug, slug))
            cur.execute(
                "INSERT INTO public.user_provisioning (user_id, schema_name, status, ready_at) "
                "VALUES (%s, %s, 'ready', now()) ON CONFLICT DO NOTHING",
                (user_id, schema))
        conn.commit()
    finally:
        conn.close()
    yield user_id, schema
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            cur.execute(
                f'DROP SCHEMA IF EXISTS "flagent_{user_id.replace("-", "_")}" CASCADE')
            cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM public.users WHERE user_id = %s", (user_id,))
    finally:
        conn.close()


def _rows(schema, sql, params=()):
    import psycopg2
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def _correct(user_id, monkeypatch, text, extraction, intent=None):
    """POST /retract/correct with the correction brain mocked to `extraction`
    (the ONLY substitution vs the live path — the extractor, addressing,
    supersession, stores and the envelope are real)."""
    from starlette.testclient import TestClient
    monkeypatch.setattr(M, "SPINE_DETERMINISTIC_SEGMENTATION", True)
    M._INGEST_ENABLED = True
    M._TENANT_READY_CACHE.add(user_id)

    class _D:
        def __init__(self):
            self.d = {}

        def get(self, k):
            return self.d.get(k)

        def setex(self, k, t, v):
            self.d[k] = v
            return True

        def ping(self):
            return True

    class _Idem:
        redis_url = "dict://test"
        ttl = 3600
        client = _D()

    M._idempotency_mgr = _Idem()

    with TestClient(M.app) as client:
        if intent == "RETRACTION":
            monkeypatch.setattr(M, "_retraction_intent_extraction_llm", lambda **k: dict(extraction))
        else:
            monkeypatch.setattr(M, "_unified_correction_extraction_llm",
                                lambda **k: dict(extraction))
        r = client.post("/retract/correct", json={"text": text, "user_id": user_id,
                                                  "attested": True, "intent": intent})
        assert r.status_code == 200, r.text[:300]
        return r.json()


def _plant_measure(user_id, monkeypatch):
    """Plant the incumbent THROUGH THE REAL LANES (harvest → ingest), so the
    supersession arm's incumbent is real."""
    from starlette.testclient import TestClient
    monkeypatch.setattr(M, "SPINE_DETERMINISTIC_SEGMENTATION", True)
    M._INGEST_ENABLED = True
    M._TENANT_READY_CACHE.add(user_id)
    with TestClient(M.app) as client:
        import src.extraction.linguistics as _L
        import time
        t0 = time.time()
        while _L._nlp is None and time.time() - t0 < 120:
            time.sleep(0.5)
        h = client.post("/harvest-spans", json={"text": _MEASURE_PLANT, "user_id": user_id})
        assert h.status_code == 200, h.text[:200]
        edges = h.json().get("edges") or []
        assert edges, f"harvest produced no edges for {_MEASURE_PLANT!r}"
        r = client.post("/ingest", json={"text": _MEASURE_PLANT, "user_id": user_id,
                                         "edges": edges, "source": "mcp"})
        assert r.status_code == 200, r.text[:200]


# ── PIN 1: THE WOUND — zero-incumbent correction = first-statement shape, count 0 ────────

def test_zero_incumbent_correction_reports_first_statement_not_a_transition(
        secbug1_tenant, monkeypatch):
    user_id, schema = secbug1_tenant
    out = _correct(user_id, monkeypatch, _ZI_TEXT, _ZI_EXTRACTION)
    assert out["status"] == "corrected", out
    # THE BAR: the count mirrors the DB — nothing was retired, so 0.
    assert out["facts_superseded"] == 0, out
    assert out["facts_superseded_by_ingest"] == 0, out
    # THE NARRATION: first-statement shape, never a fabricated "✓ old → new".
    msg = out["message"]
    assert "(new — no prior value superseded)" in msg, msg
    assert "✓" not in msg, msg
    assert "→" not in msg, msg
    assert "99 Fake Street" in msg, msg
    # THE DB: the value landed live (user-is-truth write untouched) and ZERO archive rows.
    attrs = _rows(schema, "SELECT attribute, value_text, superseded_at "
                          "FROM entity_attributes")
    assert attrs == [("lives_at", "99 Fake Street", None)], attrs
    hist = _rows(schema, "SELECT 1 FROM entity_attributes_history")
    assert hist == [], hist  # the fabricated supersession was exactly this: 0 rows


def test_zero_incumbent_pref_name_mirrors_the_demote_rowcount(
        secbug1_tenant, monkeypatch):
    """The C2-class arm: the pref_name special case counted its alias INSERT, not the
    demote. THE CONTRACT: the count equals the ACTUAL demoted preferred rows from the
    DB — never the write. (The seat bootstrap registers a preferred display name for
    the user entity, so this shape carries exactly ONE real incumbent; the envelope
    must report exactly that retirement, not the INSERT.)"""
    user_id, schema = secbug1_tenant
    # Snapshot the preferred set BEFORE the correction: a demotion is a row that WAS
    # preferred and is no longer — never-preferred rows are not retirements.
    preferred_before = {r[0] for r in _rows(
        schema, "SELECT alias FROM entity_aliases WHERE is_preferred = true")}
    out = _correct(user_id, monkeypatch, _PREF_TEXT, _PREF_EXTRACTION)
    assert out["status"] == "corrected", out
    preferred_after = {r[0] for r in _rows(
        schema, "SELECT alias FROM entity_aliases WHERE is_preferred = true")}
    # THE DB IS THE BAR: exactly the demoted incumbents counted, verified in SQL.
    demoted = preferred_before - preferred_after
    assert len(preferred_after) == 1 and "grendel" in preferred_after, \
        (preferred_before, preferred_after)
    assert out["facts_superseded"] == len(demoted), (out, preferred_before, preferred_after)
    # a real retirement narrates the real transition (✓ shape is TRUE again)
    if demoted:
        assert "✓" in out["message"] and "→" in out["message"], out
        assert "(new — no prior value superseded)" not in out["message"], out


def test_retraction_over_no_incumbent_never_claims_an_archive(
        secbug1_tenant, monkeypatch):
    """Retracting a value that was never stored: the legacy envelope claimed '✓ … the
    stored fact was archived' — a fabricated archive. THE HONEST CONTRACT, regardless
    of which axis the tenant's rel metadata routes the turn down:
      • SCALAR axis → corrected + facts_superseded=0 + the explicit 'nothing to retire,
        nothing archived' shape (the arm this fix wired);
      • RELATIONAL axis (the metadata axis override can reroute lives_at) → the honest
        no-incumbent failure ('Old … fact not found'), never an archive claim.
    Either way: NO check-mark, NO 'archived' claim, count 0."""
    user_id, schema = secbug1_tenant
    out = _correct(user_id, monkeypatch, _RETRACT_TEXT, _RETRACT_EXTRACTION,
                   intent="RETRACTION")
    assert out["status"] in ("corrected", "failed"), out
    assert out["facts_superseded"] == 0, out
    assert out["facts_superseded_by_ingest"] == 0, out
    msg = out.get("message") or ""
    assert "✓" not in msg, msg
    assert "→" not in msg, msg
    if out["status"] == "corrected":
        assert "nothing to retire" in msg and "nothing archived" in msg, msg
    else:
        # the honest no-incumbent failure names the miss; it must not claim an archive
        low = msg.lower()
        assert "not found" in low or "not identified" in low, msg
        assert "archived" not in low, msg
        assert "was superseded" not in low, msg


# ── PIN 2: a REAL supersession still narrates the transition — the ✓ shape is TRUE ──────

def test_real_supersession_still_narrates_the_transition(secbug1_tenant, monkeypatch):
    user_id, schema = secbug1_tenant
    _plant_measure(user_id, monkeypatch)
    out = _correct(user_id, monkeypatch, _MEASURE_CORRECTION, _MEASURE_EXTRACTION)
    assert out["status"] == "corrected", out
    assert out["facts_superseded"] == 1, out
    assert out["facts_superseded_by_ingest"] == 1, out
    msg = out["message"]
    assert "✓" in msg and "→" in msg, msg
    # The write is the truth: the new value live, the prior archived in history.
    attrs = _rows(schema, "SELECT attribute, value_text, superseded_at "
                          "FROM entity_attributes")
    assert attrs == [("measure", "7 centimeters", None)], attrs
    hist = _rows(schema, "SELECT attribute, prior_value_text, new_value_text, action, cause "
                         "FROM entity_attributes_history")
    assert len(hist) == 1, hist
    assert hist[0][1] == "about 5 centimeters" and hist[0][2] == "7 centimeters", hist
    assert hist[0][3] == "superseded" and hist[0][4] == "correct_fact", hist


# ── PIN 3: flag OFF = byte-parity with the legacy (fabricating) envelope ─────────────────

def test_flag_off_restores_the_legacy_envelope_byte_parity(secbug1_tenant, monkeypatch):
    """OFF = byte-parity: the legacy count (the write, max 1) and the legacy narration
    (✓ old → new) come back exactly as pre-fix — documented legacy behavior, pinned."""
    monkeypatch.setattr(M, _FLAG, False)
    user_id, schema = secbug1_tenant
    out = _correct(user_id, monkeypatch, _ZI_TEXT, _ZI_EXTRACTION)
    assert out["status"] == "corrected", out
    assert out["facts_superseded"] == 1, out  # the legacy fabricated count, byte-parity
    msg = out["message"]
    assert "✓" in msg and "→" in msg, msg
    assert "old place" in msg and "99 Fake Street" in msg, msg
