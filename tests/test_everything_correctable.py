"""EVERYTHING IS CORRECTABLE (issue #224, owner ruling 2026-10-07) — the constitution pins.

OWNER RULING (2026-10-07, live): "everything should be correctable though, there
shouldn't be shit marked immutable … everything should be able to be overwritten,
user.is.truth." NO immutable escape hatch anywhere in the correction lane. The
measured wound (prod seat 54064c76): "Correct my date of birth: I was born on
1978-03-18." (SAME value as stored) → Phase-6 hardcoded set hard-rejected → the
serving model told the user their birthday is immutable.

What these pins freeze, per the ruling:

  1. SAME-VALUE correction → status='unchanged', directive-style message, ZERO
     writes (row counts unchanged in facts / entity_attributes /
     entity_attributes_history / entity_aliases).
  2. NEW-VALUE correction of a rel whose correction_behavior='immutable' (scratch
     fixture seeded in THIS test's own schema — never a seed rel) → supersedes WITH
     history, never 'rejected'. Correction behavior metadata is inert: no code
     branch may refuse a correction by rel name.
  3. NEW-VALUE correction of a supersede rel → existing behavior unchanged
     (corrected + history row).
  4. AUTHORITY RANK negative control — an inferred-source write can never retire a
     user-stated value (`_scalar_gate_hold`).
  5. The correction-extraction prompt carries NO immutable auto-reject block and NO
     rel-name literals in its template (the dynamic rel menu is tenant metadata and
     is deliberately excluded from the assertion by capturing with an empty menu).
  6. THE EXECUTABLE CONSTITUTION — tools/rel_literal_census.py: a planted
     IMMUTABLE_REL_TYPES-shaped literal gate must go RED; the whole tree at HEAD
     must be GREEN.

Scratch fixtures only (subject-agnostic: 'zorp'/''vane'' codes on the seat's own
anchor entity) — no real people, no birthdays, no dependence on any seeded rel's
correction_behavior value. Same harness conventions as
tests/test_correction_scalar_addressing_cx12.py (fresh throwaway tenant on
POSTGRES_DSN; the ONLY substitution vs the live path is the correction brain).
"""

import os
import subprocess
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


# ── the same-value comparison key (deterministic, exact) ─────────────────────────────────

def test_value_key_normalizes_case_whitespace_quotes():
    assert M._correction_value_key("1978-03-18") == M._correction_value_key(" 1978-03-18 ")
    assert M._correction_value_key("Blue") == M._correction_value_key("blue")
    assert M._correction_value_key('"Z-77"') == M._correction_value_key("Z-77")
    assert M._correction_value_key(None) is None
    assert M._correction_value_key("   ") is None
    assert M._correction_value_key("Z-77") != M._correction_value_key("Z-78")


# ── throwaway tenant ─────────────────────────────────────────────────────────────────────

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


def _rows(schema, sql, params=()):
    import psycopg2
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def _seed_scratch_rel(schema, rel_type, correction_behavior):
    """Scratch rel fixture IN THE TEST'S OWN SCHEMA (constraint: never depend on a
    seed rel's correction_behavior value). Scratch names are deliberately not real
    rels — subject-agnostic, nothing about them is a person or a birthday."""
    import psycopg2
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(
                "INSERT INTO rel_types (rel_type, label, correction_behavior, source, "
                "head_types, tail_types) VALUES (%s, %s, %s, 'engine', ARRAY['ANY'], "
                "ARRAY['SCALAR']) ON CONFLICT (rel_type) DO UPDATE "
                "SET correction_behavior = EXCLUDED.correction_behavior",
                (rel_type, rel_type.replace("_", " ").title(), correction_behavior))
        conn.commit()
    finally:
        conn.close()


def _seed_scalar(schema, user_id, attribute, value_text):
    import psycopg2
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(
                "INSERT INTO entity_attributes (user_id, entity_id, attribute, "
                "value_text, provenance) VALUES (%s, %s, %s, %s, 'user_stated') "
                "ON CONFLICT (entity_id, attribute) DO UPDATE SET value_text = EXCLUDED.value_text, "
                "superseded_at = NULL, provenance = 'user_stated'",
                (user_id, user_id, attribute, value_text))
        conn.commit()
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
    (same substitution contract as cx12 — the live brain is unavailable in-process)."""
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


def _extraction(user_id, rel_type, old_value, new_value):
    return {
        "subject_uuid": user_id,
        "subject_name": "user",
        "old_rel_type": rel_type,
        "old_value": old_value,
        "new_rel_type": rel_type,
        "new_value": new_value,
        "dimension": "SCALAR",
        "confidence": 0.98,
        "reason": "explicit old→new statement (stubbed)",
    }


# ── PIN 1 — same-value correction is a no-op, never a wall, never a write ───────────────

def test_pin1_same_value_correction_is_unchanged_noop(tenant, monkeypatch):
    user_id, schema = tenant
    _seed_scratch_rel(schema, "zorp_code", "immutable")  # the wound's behavior class
    _seed_scalar(schema, user_id, "zorp_code", "Z-77")
    before = _counts(schema)

    out = _correct(user_id, monkeypatch,
                   "Correct my zorp code: it is Z-77.",
                   _extraction(user_id, "zorp_code", "Z-77", "Z-77"))

    assert out["status"] == "unchanged", (out.get("status"), out.get("message"))
    msg = (out.get("message") or "").lower()
    assert "already recorded" in msg
    # directive-style: tell the model to behave normally and NOT to claim a lock
    assert "respond normally" in msg
    assert "lock" in msg and "do not claim" in msg
    assert out.get("old_rel_type") == "zorp_code" and out.get("new_value") == "Z-77"

    # ZERO writes: every store row count is byte-identical.
    assert _counts(schema) == before, "same-value correction wrote rows"

    # and never the wound's vocabulary:
    assert "immutable" not in msg


# ── PIN 2 — new-value correction of a formerly-immutable-class rel supersedes ───────────

def test_pin2_new_value_correction_of_immutable_behavior_rel(tenant, monkeypatch):
    user_id, schema = tenant
    # Scratch fixture seeded 'immutable' IN THE TEST SCHEMA — the metadata value is
    # inert by construction; the correction must proceed anyway (owner ruling).
    _seed_scratch_rel(schema, "vane_setting", "immutable")
    _seed_scalar(schema, user_id, "vane_setting", "LOW")
    before_hist = _counts(schema)["hist"]

    out = _correct(user_id, monkeypatch,
                   "Correct my vane setting: it is HIGH, not LOW.",
                   _extraction(user_id, "vane_setting", "LOW", "HIGH"))

    assert out["status"] != "rejected", out
    assert "immutable" not in (out.get("message") or "").lower()
    assert out["status"] == "corrected", (out.get("status"), out.get("message"))

    live = _rows(schema, "SELECT value_text, provenance, superseded_at FROM "
                         "entity_attributes WHERE attribute = 'vane_setting'")
    assert live and live[0][0] == "HIGH", live
    assert live[0][1] == "user_corrected" and live[0][2] is None, live

    # … WITH history: the prior value is retained, never annihilated.
    hist = _rows(schema, "SELECT prior_value_text, new_value_text, action, cause "
                         "FROM entity_attributes_history WHERE attribute = 'vane_setting'")
    assert len(hist) == 1 and before_hist == _counts(schema)["hist"] - 1
    assert hist[0][0] == "LOW" and hist[0][1] == "HIGH", hist
    assert hist[0][2] == "superseded" and hist[0][3] == "correct_fact", hist


# ── PIN 3 — supersede rel keeps the existing supersession behavior ──────────────────────

def test_pin3_new_value_correction_of_supersede_rel(tenant, monkeypatch):
    user_id, schema = tenant
    _seed_scratch_rel(schema, "dial_mark", "supersede")
    _seed_scalar(schema, user_id, "dial_mark", "3")
    out = _correct(user_id, monkeypatch,
                   "Correct my dial mark: it is 7, not 3.",
                   _extraction(user_id, "dial_mark", "3", "7"))
    assert out["status"] == "corrected", (out.get("status"), out.get("message"))
    live = _rows(schema, "SELECT value_text FROM entity_attributes "
                         "WHERE attribute = 'dial_mark'")
    assert live and live[0][0] == "7", live
    hist = _rows(schema, "SELECT prior_value_text, new_value_text "
                         "FROM entity_attributes_history WHERE attribute = 'dial_mark'")
    assert hist and hist[0] == ("3", "7"), hist


# ── PIN 4 — authority-rank negative control (existing guard, stays green) ───────────────

def test_pin4_authority_rank_inferred_cannot_retire_user_stated():
    from src.api.main import _scalar_gate_hold, _scalar_authority_rank
    # stored user truth vs incoming inference → HOLD (the inferred write is refused)
    assert _scalar_gate_hold("user_stated", "llm_inferred") is True
    assert _scalar_gate_hold("user_stated", "llm_learned") is True
    # stored inference vs incoming user truth → proceeds (user-is-truth)
    assert _scalar_gate_hold("llm_inferred", "user_stated") is False
    assert _scalar_authority_rank("user_corrected") == _scalar_authority_rank("user_stated")


# ── PIN 5 — the correction prompt has no immutable block and no rel literals ────────────

def test_pin5_prompt_no_immutable_block_no_rel_literals(monkeypatch):
    from unittest.mock import MagicMock
    captured = {}

    def _fake_llm(messages, **k):
        captured["system"] = messages[0]["content"]
        return {}

    # Empty rel menu: the DB cursor returns no rel_types rows, so the ONLY rel
    # tokens that can appear in the captured prompt are TEMPLATE literals —
    # exactly what this pin forbids (the tenant's dynamic menu is metadata).
    db = MagicMock()
    cur = db.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = []
    cur.fetchone.return_value = None

    monkeypatch.setattr(M, "call_llm_with_retry_sync", _fake_llm)
    M._unified_correction_extraction_llm(
        text="Correct my zorp code: it is Z-77.", user_id="u-pin5",
        context_facts=[], db=db)

    prompt = captured["system"]
    assert prompt, "prompt was never captured"
    up = prompt.upper()
    assert "IMMUTABLE FACTS" not in up
    assert "AUTO-REJECT" not in up
    assert "CHECK IMMUTABLE" not in up
    for literal in ("born_on", "born_in", "nationality"):
        assert literal not in prompt, f"rel literal {literal!r} in the prompt template"


# ── PIN 6 — the executable constitution: census gate plant-RED, tree-GREEN ──────────────

def _run_census(root):
    return subprocess.run(
        [sys.executable, os.path.join(REPO, "tools", "rel_literal_census.py"),
         "--root", str(root)],
        capture_output=True, text=True, timeout=120)


def test_pin6_census_flags_planted_immutable_literal_gate(tmp_path):
    import shutil
    root = tmp_path / "plant_root"
    (root / "src" / "api").mkdir(parents=True)
    (root / "src" / "extraction").mkdir(parents=True)
    (root / "src" / "mcp").mkdir(parents=True)
    (root / "migrations").mkdir(parents=True)
    # the seed vocabulary sources the census itself uses (born_on/born_in/nationality)
    for mig in ("007_correction_behavior.sql", "007_rel_types_source.sql"):
        shutil.copy(os.path.join(REPO, "migrations", mig), root / "migrations" / mig)
    (root / "src" / "api" / "plant.py").write_text(
        "IMMUTABLE_REL_TYPES = {\"born_on\", \"born_in\", \"nationality\"}\n"
        "\n"
        "def correct_fact(old_rel_type, new_value):\n"
        "    if old_rel_type in IMMUTABLE_REL_TYPES:\n"
        "        return {\"status\": \"rejected\",\n"
        "                \"message\": \"Field is immutable and cannot be corrected.\"}\n"
        "    if old_rel_type in {\"born_on\", \"born_in\"}:\n"
        "        return {\"status\": \"rejected\", \"reason\": \"immutable fact\"}\n"
        "    return {\"status\": \"ok\"}\n",
        encoding="utf-8")

    proc = _run_census(root)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "IMMUTABLE_REL_TYPES" in proc.stdout  # the original line shape, V1
    assert "V2" in proc.stdout                    # the anonymous refusal shape


def test_pin6_census_green_on_whole_tree():
    proc = _run_census(REPO)
    assert proc.returncode == 0, (proc.returncode, proc.stdout[-2000:], proc.stderr[-2000:])
    assert "violation(s)" in proc.stdout
    assert proc.stdout.strip().splitlines()[-1].startswith("rel_literal_census: 0 violation")


# ── MCP door — 'unchanged' is not a failure status ──────────────────────────────────────

def test_unchanged_status_is_not_a_mcp_failure():
    from src.mcp.server import status_is_failure
    assert status_is_failure("unchanged") is False
    # the failure default is intact for unknown words (fail loud, never silently)
    assert status_is_failure("rejected") is True
    assert status_is_failure("immutable_fact") is True


# ── WGM gate — the third immutable seam is gone (behavior token is inert) ───────────────

def test_wgm_gate_correction_semantics_never_immutable(tenant):
    user_id, schema = tenant
    _seed_scratch_rel(schema, "kite_hitch", "immutable")  # straggler metadata, pre-migration
    import psycopg2
    from src.wgm.gate import WGMValidationGate
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
            gate = WGMValidationGate(conn, schema_name=schema)
            verdict = gate._apply_correction_semantics(
                {"is_correction": True, "subject_id": user_id}, "kite_hitch")
        assert verdict["action"] in ("accept", "supersede"), verdict
        assert verdict["action"] != "immutable"
    finally:
        conn.close()
