"""CONTRAST-FREE CORRECTION ASSIST (issue #227) — the four-outcome pins.

MEASURED WOUND (prod 2026-10-08, the #224 residual — see
``_designation_correction_frame_assist``'s docstring for the one provenance quote):
a contrast-free correction turn (no ", not <old>" clause) died at the completeness
gate ("Incomplete correction — relationship type not identified") because the
correction LLM named NO relationship — so the #224 same-value 'unchanged' no-op was
never reached. The assist parses the possessive-designation frame deterministically
and matches the slot noun against the SUBJECT'S OWN stored attribute slots.

What these pins freeze (the issue's four bars + the safety rails):

  1. CONTRAST-FREE SAME VALUE  → status='unchanged', ZERO writes (4-table counts).
  2. CONTRAST-FREE NEW VALUE   → status='corrected', live value updated, history row,
                                 provenance user_corrected (normal authority path).
  3. NO STORED SLOT            → today's honest 'failed' (the assist never invents a
                                 slot), ZERO writes.
  4. CONTRASTIVE/COMPLETE EXTRACTIONS UNCHANGED — a complete extraction (confidence
                                 present, ≥ floor) is never overridden by the frame;
                                 the #224 suite (tests/test_everything_correctable.py)
                                 stays green (run separately).
  5. SUB-THRESHOLD arm — a complete-but-shaky extraction (< 0.5) IS replaced by the
     deterministic frame (the issue's second trigger).
  6. FLAG OFF (CORRECTION_DESIGNATION_ASSIST=false) → the legacy 'failed' path,
     byte-for-byte.
  7-9. DECLINE rails (zero writes, honest 'failed'): third-party possessor ("her X's
     slot"), slot-echo disagreement, subject not in the alias registry.
  10. RETIRED SLOT re-assertion → NOT 'unchanged' (the retired value is not "on
      record"); the normal arm un-retires the slot (SS2).

Scratch fixtures only, FRESH subject (marina: 'catamaran'/'pen' — deliberately not
vlan/networking/family). Same harness contract as tests/test_everything_correctable.py:
fresh throwaway tenant on POSTGRES_DSN; the ONLY substitution vs the live path is the
correction brain (stubbed to the measured gap / sub-threshold shapes).
"""

import os
import sys
import uuid

import pytest

# THE SPINE FLAG IS AN ENV-DEPLOY VALUE, NOT A CODE DEFAULT (CLAUDE.md Critical
# Truth #1): set before src.api.main imports so any harvest-side helper behaves
# as deployed.
os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

_DSN = os.environ.get("POSTGRES_DSN", "")
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

sys.path.insert(0, REPO)

import src.api.main as M  # noqa: E402

# WARM THE SPACY MODEL BEFORE ANY REQUEST: each _correct() call spins a fresh
# TestClient lifespan whose un-awaited _warm_linguistics() task sets
# _load_attempted=True the moment it STARTS loading (~16s), and linguistics'
# fail-safe treats ANOTHER THREAD'S IN-FLIGHT LOAD as "unavailable" — a request
# landing inside that window would see the assist decline with
# reason=linguistics_unavailable (a harness race, not a product behavior; prod
# warms at startup and never races). Loading here, synchronously on THIS thread,
# pins the model in module state before the first lifespan exists.
from src.extraction import linguistics as _L  # noqa: E402
assert _L.linguistics_available(), (
    "spaCy model unavailable — the designation-assist pins require the deriver "
    "(SPACY_MODEL must resolve; see CLAUDE.md Test Observability)")


# ── throwaway tenant (same contract as tests/test_everything_correctable.py) ────────────

@pytest.fixture(scope="module")
def tenant():
    if not _DSN:
        pytest.skip("no POSTGRES_DSN")
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
            cur.execute(f'DROP SCHEMA IF EXISTS "flagent_{user_id.replace("-", "_")}" CASCADE')
            cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM public.users WHERE user_id = %s", (user_id,))
    finally:
        conn.close()


# ── seeding helpers (scratch fixtures, fresh marina subject) ────────────────────────────

def _conn_schema(schema):
    import psycopg2
    conn = psycopg2.connect(_DSN)
    cur = conn.cursor()
    cur.execute(f'SET search_path TO "{schema}"')
    return conn, cur


def _seed_subject(schema, name):
    """A stored SUBJECT entity + its preferred alias (lookup-only resolution)."""
    entity_id = str(uuid.uuid4())
    conn, cur = _conn_schema(schema)
    try:
        cur.execute("INSERT INTO entities (id, entity_type) VALUES (%s, 'Object') "
                    "ON CONFLICT (id) DO NOTHING", (entity_id,))
        cur.execute(
            "INSERT INTO entity_aliases (entity_id, alias, is_preferred, preference_source) "
            "VALUES (%s, %s, true, 'user_stated') "
            "ON CONFLICT (entity_id, alias) DO UPDATE SET is_preferred = true",
            (entity_id, name))
        conn.commit()
    finally:
        conn.close()
    return entity_id


def _seed_attr(schema, user_id, entity_id, attribute, value_text, retired=False):
    from datetime import datetime
    conn, cur = _conn_schema(schema)
    try:
        cur.execute(
            "INSERT INTO entity_attributes (user_id, entity_id, attribute, value_text, "
            "provenance, superseded_at) VALUES (%s, %s, %s, %s, 'user_stated', %s) "
            "ON CONFLICT (entity_id, attribute) DO UPDATE SET value_text = EXCLUDED.value_text, "
            "superseded_at = EXCLUDED.superseded_at, provenance = 'user_stated'",
            (user_id, entity_id, attribute, value_text,
             datetime.now() if retired else None))
        conn.commit()
    finally:
        conn.close()


def _rows(schema, sql, params=()):
    conn, cur = _conn_schema(schema)
    try:
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        conn.close()


def _counts(schema):
    return {
        "facts": _rows(schema, "SELECT count(*) FROM facts")[0][0],
        "attrs": _rows(schema, "SELECT count(*) FROM entity_attributes")[0][0],
        "hist": _rows(schema, "SELECT count(*) FROM entity_attributes_history")[0][0],
        "aliases": _rows(schema, "SELECT count(*) FROM entity_aliases")[0][0],
    }


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


def _correct(user_id, monkeypatch, text, extraction):
    """POST /retract/correct with the correction brain stubbed to `extraction`
    (same substitution contract as tests/test_everything_correctable.py — the live
    brain is unavailable in-process; the deterministic assist is NOT stubbed)."""
    from starlette.testclient import TestClient
    M._INGEST_ENABLED = True
    M._TENANT_READY_CACHE.add(user_id)
    M._idempotency_mgr = _Idem()
    monkeypatch.setattr(M, "_unified_correction_extraction_llm",
                        lambda **k: dict(extraction))
    with TestClient(M.app) as client:
        r = client.post("/retract/correct", json={"text": text, "user_id": user_id,
                                                  "attested": True})
    assert r.status_code == 200, r.text[:300]
    return r.json()


_GAP = {}  # the measured wound shape: the brain names NOTHING (no relationship at all)


# ── PIN 1 — contrast-free SAME value → 'unchanged', ZERO writes ─────────────────────────

def test_pin1_contrast_free_same_value_is_unchanged_noop(tenant, monkeypatch):
    user_id, schema = tenant
    ent = _seed_subject(schema, "catamaran")
    _seed_attr(schema, user_id, ent, "pen", "12")
    before = _counts(schema)

    out = _correct(user_id, monkeypatch,
                   "Correct my catamaran's pen: it is on pen 12.",
                   _GAP)

    assert out["status"] == "unchanged", (out.get("status"), out.get("message"))
    msg = (out.get("message") or "").lower()
    assert "already recorded" in msg and "respond normally" in msg
    assert out.get("old_rel_type") == "pen" and out.get("new_value") == "12"
    # ZERO writes: every store row count is byte-identical.
    assert _counts(schema) == before, "contrast-free same-value correction wrote rows"


# ── PIN 2 — contrast-free NEW value → normal supersession WITH history ─────────────────

def test_pin2_contrast_free_new_value_supersedes_with_history(tenant, monkeypatch):
    user_id, schema = tenant
    ent = _seed_subject(schema, "trimaran")
    _seed_attr(schema, user_id, ent, "pen", "12")

    out = _correct(user_id, monkeypatch,
                   "Correct my trimaran's pen: it is on pen 15.",
                   _GAP)

    assert out["status"] == "corrected", (out.get("status"), out.get("message"))
    live = _rows(schema, "SELECT value_text, provenance, superseded_at FROM "
                         "entity_attributes WHERE entity_id = %s AND attribute = 'pen'",
                 (ent,))
    assert live and live[0][0] == "15", live
    assert live[0][1] == "user_corrected" and live[0][2] is None, live
    hist = _rows(schema, "SELECT prior_value_text, new_value_text, action, cause "
                         "FROM entity_attributes_history WHERE attribute = 'pen'")
    assert hist and hist[0][0] == "12" and hist[0][1] == "15", hist
    assert hist[0][2] == "superseded" and hist[0][3] == "correct_fact", hist


# ── PIN 3 — noun with NO stored slot → today's honest 'failed', ZERO writes ─────────────

def test_pin3_no_stored_slot_is_honest_failed(tenant, monkeypatch):
    user_id, schema = tenant
    ent = _seed_subject(schema, "outrigger")
    _seed_attr(schema, user_id, ent, "pen", "12")
    before = _counts(schema)

    out = _correct(user_id, monkeypatch,
                   "Correct my outrigger's marina: it is on marina 4.",
                   _GAP)

    assert out["status"] == "failed", (out.get("status"), out.get("message"))
    assert "relationship type not identified" in (out.get("message") or "").lower()
    # the assist never invents a slot to write
    assert _counts(schema) == before, "no-slot correction wrote rows"
    live = _rows(schema, "SELECT attribute FROM entity_attributes WHERE entity_id = %s",
                 (ent,))
    assert [r[0] for r in live] == ["pen"], live


# ── PIN 4 — a COMPLETE extraction is never overridden by the frame ──────────────────────

def test_pin4_complete_extraction_never_overridden(tenant, monkeypatch):
    # The frame would parse value 12 == stored → 'unchanged'; the LLM's complete
    # extraction (conf 0.85) says 12 → 15. The assist must NOT fire on a complete,
    # confidently-keyed extraction: the outcome must be the LLM's 'corrected'.
    user_id, schema = tenant
    ent = _seed_subject(schema, "skiff")
    _seed_attr(schema, user_id, ent, "pen", "12")

    out = _correct(user_id, monkeypatch,
                   "Correct my skiff's pen: it is on pen 12.",
                   {"subject_name": "skiff", "subject": "skiff",
                    "old_rel_type": "pen", "old_value": "12",
                    "new_rel_type": "pen", "new_value": "15",
                    "dimension": "SCALAR", "confidence": 0.85})

    assert out["status"] == "corrected", (out.get("status"), out.get("message"))
    live = _rows(schema, "SELECT value_text FROM entity_attributes "
                         "WHERE entity_id = %s AND attribute = 'pen'", (ent,))
    assert live and live[0][0] == "15", live


# ── PIN 5 — sub-threshold arm: complete-but-shaky extraction IS replaced ────────────────

def test_pin5_subthreshold_extraction_replaced_by_frame(tenant, monkeypatch):
    # conf 0.3 with a WRONG rel — the issue's second trigger. The frame supplies
    # the row the words actually name (pen 12 → 15).
    user_id, schema = tenant
    ent = _seed_subject(schema, "punt")
    _seed_attr(schema, user_id, ent, "pen", "12")

    out = _correct(user_id, monkeypatch,
                   "Correct my punt's pen: it is on pen 15.",
                   {"subject_name": "punt", "subject": "punt",
                    "old_rel_type": "mooring", "old_value": None,
                    "new_rel_type": "mooring", "new_value": "15",
                    "dimension": "RELATIONAL", "confidence": 0.3})

    assert out["status"] == "corrected", (out.get("status"), out.get("message"))
    live = _rows(schema, "SELECT value_text FROM entity_attributes "
                         "WHERE entity_id = %s AND attribute = 'pen'", (ent,))
    assert live and live[0][0] == "15", live
    # the 'mooring' guess never forked a new slot
    attrs = _rows(schema, "SELECT attribute FROM entity_attributes WHERE entity_id = %s",
                  (ent,))
    assert [r[0] for r in attrs] == ["pen"], attrs


# ── PIN 6 — FLAG OFF = today's path, byte-for-byte ──────────────────────────────────────

def test_pin6_flag_off_is_legacy_failed(tenant, monkeypatch):
    user_id, schema = tenant
    ent = _seed_subject(schema, "yawl")
    _seed_attr(schema, user_id, ent, "pen", "12")
    before = _counts(schema)
    monkeypatch.setenv("CORRECTION_DESIGNATION_ASSIST", "false")

    out = _correct(user_id, monkeypatch,
                   "Correct my yawl's pen: it is on pen 15.",
                   _GAP)

    assert out["status"] == "failed", (out.get("status"), out.get("message"))
    assert "relationship type not identified" in (out.get("message") or "").lower()
    assert _counts(schema) == before


# ── PINS 7-9 — decline rails (honest failed, zero writes) ───────────────────────────────

def test_pin7_third_party_possessor_declines(tenant, monkeypatch):
    # "her X's slot" is not the speaker's designation — the referent is never guessed.
    user_id, schema = tenant
    _seed_subject(schema, "canoe")
    _seed_attr(schema, user_id, _rows(
        schema, "SELECT entity_id FROM entity_aliases WHERE alias = 'catamaran'")[0][0],
        "pen", "12")
    before = _counts(schema)

    out = _correct(user_id, monkeypatch,
                   "Correct her catamaran's pen: it is on pen 15.",
                   _GAP)

    assert out["status"] == "failed", (out.get("status"), out.get("message"))
    assert _counts(schema) == before


def test_pin8_slot_echo_disagreement_declines(tenant, monkeypatch):
    # the value predicate's noun must AGREE with the designation slot — a disagreeing
    # noun names a different designation, not this frame.
    user_id, schema = tenant
    ent = _seed_subject(schema, "dinghy")
    _seed_attr(schema, user_id, ent, "pen", "12")
    before = _counts(schema)

    out = _correct(user_id, monkeypatch,
                   "Correct my dinghy's pen: it is on berth 15.",
                   _GAP)

    assert out["status"] == "failed", (out.get("status"), out.get("message"))
    assert _counts(schema) == before


def test_pin9_unknown_subject_declines(tenant, monkeypatch):
    # lookup-only: a correction never mints — an unregistered subject declines.
    user_id, schema = tenant
    before = _counts(schema)

    out = _correct(user_id, monkeypatch,
                   "Correct my phantom dinghy's pen: it is on pen 3.",
                   _GAP)

    assert out["status"] == "failed", (out.get("status"), out.get("message"))
    assert _counts(schema) == before


# ── PIN 10 — a RETIRED slot re-asserted contrast-free UN-RETIRES, never 'unchanged' ─────

def test_pin10_retired_slot_reassertion_unretires(tenant, monkeypatch):
    user_id, schema = tenant
    ent = _seed_subject(schema, "coracle")
    _seed_attr(schema, user_id, ent, "pen", "12", retired=True)

    out = _correct(user_id, monkeypatch,
                   "Correct my coracle's pen: it is on pen 12.",
                   _GAP)

    # the retired value is NOT "on record" — this is a re-assertion, not a no-op
    assert out["status"] != "unchanged", (out.get("status"), out.get("message"))
    assert out["status"] == "corrected", (out.get("status"), out.get("message"))
    live = _rows(schema, "SELECT value_text, superseded_at FROM entity_attributes "
                         "WHERE entity_id = %s AND attribute = 'pen'", (ent,))
    assert live and live[0][0] == "12" and live[0][1] is None, live
