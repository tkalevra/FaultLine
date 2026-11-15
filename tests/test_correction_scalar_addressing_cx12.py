"""CX12 — CORRECTION SCALAR ADDRESSING on a compound type node (issue #12).

THE WOUND (measured live on deploy #6, image 9e9446e9, and reproduced in-process):
chain (a) compound kind-statement → (b) verb-measure scalar → (c) correction failed with
"Old relational fact not found: measure" while `entity_attributes` visibly held
measure='about 5 centimeters'. Root mechanism (repro: DEV/review/gauntlet/
correction-scalar-addressing/round_1/repro.txt):

  1. The verb-measure INGEST narrowed the compound subject to its HEAD NOUN — the scalar
     row lives on the `octopus` head type node, NOT on the `blue-ringed octopus` compound
     node the correction names.
  2. `_address_scalar_target` rung 1/2 (subject-scoped) therefore miss; rung 3
     (tenant-wide UNIQUE holder) finds exactly one holder — but its exact-string value
     gate compared the correction's old_value '5 centimeters' against the stored
     'about 5 centimeters' and DECLINED: the ingest spine files the approximator inside
     value_text, so the gate refused the very row the user was correcting.
  3. via=None → dimension stays RELATIONAL → the facts-table lookup finds nothing →
     the correction dies and the OLD value stays live (user-is-truth violated).

THE FIX (one root change, at the addressing seam): rung 3's value gate admits a stored
text that differs from the extracted old value by a NON-NUMERIC PREFIX only — compared
through `_scalar_value_numeric_key` (first number token + trailing unit words). Digits
and token tails are grammar, not vocabulary: NO approximator word list. Genuinely
different numbers/units keep declining; digit-less pairs keep the strict-compare decline
byte-for-byte (the SS4 pin `test_address_rung3_value_check_when_old_value_extracted`
stays green).

Pins here: the numeric key, the rung-3 gate both arms, the FULL chain E2E on a fresh
in-process seat (kind-statement → measure → correction → rows: measure='7 centimeters'
live + history row for 'about 5 centimeters'), and the non-compound regression shape
(deploy #5: row on the SAME entity the correction names → rung 1 exact → green).

The E2E runs the REAL harvest → /ingest → /retract/correct against the throwaway
POSTGRES_DSN tenant. The ONLY substitution vs the live path is the correction brain
(unavailable in-process): `_unified_correction_extraction_llm` is mocked to the shape the
live deploy returned (dimension=RELATIONAL, old_rel_type='measure') — evidenced by the
live failure message itself. The extractor, addressing, supersession and stores are real.
"""

import os
import uuid

import pytest

# THE SPINE FLAG IS AN ENV-DEPLOY VALUE, NOT A CODE DEFAULT (CLAUDE.md Critical Truth #1):
# production sets SENTENCE_PIPELINE=true; set it before src.api.main imports so the E2E
# runs the deployed spine route, not the legacy harvest.
os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("SENTENCE_PIPELINE", "true")

_DSN = os.environ.get("POSTGRES_DSN", "")

import src.api.main as M  # noqa: E402
from src.api.main import (  # noqa: E402
    _address_scalar_target,
    _scalar_value_numeric_key,
)

from unittest.mock import MagicMock  # noqa: E402


# ── the numeric key (deterministic, no word list) ────────────────────────────────────────

def test_numeric_key_extracts_number_and_unit():
    assert _scalar_value_numeric_key("5 centimeters") == ("5", "centimeters")
    assert _scalar_value_numeric_key("about 5 centimeters") == ("5", "centimeters")
    assert _scalar_value_numeric_key("ABOUT  5  centimeters") == ("5", "centimeters")


def test_numeric_key_handles_decimal_and_separators():
    assert _scalar_value_numeric_key("3.5 weeks")[0] == "3.5"
    assert _scalar_value_numeric_key("~40 kilometers")[0] == "40"
    # ONLY '.' is a decimal separator — a comma is a token boundary, which can only make
    # the key CONSERVATIVE (an admission path beside the exact compare, never a refusal).
    assert _scalar_value_numeric_key("1,000 meters") == ("1", "000 meters")


def test_numeric_key_none_without_digit():
    assert _scalar_value_numeric_key("twelve") is None
    assert _scalar_value_numeric_key("") is None
    assert _scalar_value_numeric_key(None) is None


# ── SS4 rung 3 — the approximator-prefix admission, both arms ────────────────────────────

def _addr_db(script):
    """Same harness as tests/test_scalar_supersession.py::_addr_db — cur.execute collects
    SQL; fetchone/fetchall pop from the script in call order."""
    db = MagicMock()
    cur = db.cursor.return_value.__enter__.return_value
    state = {"calls": []}
    cur.execute.side_effect = lambda sql, params=None: state["calls"].append(
        " ".join(sql.split()))
    _one, _all = [], []
    for kind, res in script:
        (_one if kind == "one" else _all).append(res)
    _oi, _ai = iter(_one), iter(_all)
    cur.fetchone.side_effect = lambda: next(_oi, None)
    cur.fetchall.side_effect = lambda: next(_ai, [])
    return cur, state


def test_rung3_admits_approximator_prefixed_stored_value():
    # THE WOUND: the row lives on the head-noun entity (the compound correction subject
    # holds nothing); exactly one tenant holder stores 'about 5 centimeters'; the
    # correction's old value is the same measurement without the approximator.
    cur, _st = _addr_db([
        ("one", None),                      # rung 1: subject holds nothing
        ("all", []),                        # rung 2: no known scalar attrs to pin
        ("all", []),                        # rung 2.5: no OTHER stored name to re-pin
        ("all", [("octopus_entity", "about 5 centimeters")]),  # unique holder
    ])
    subj, attr, via = _address_scalar_target(
        cur, "compound_entity", "measure", "5 centimeters", "u1")
    assert (subj, attr, via) == ("octopus_entity", "measure", "unique_holder")


def test_rung3_admits_bare_number_old_value():
    # The extractor may keep only the magnitude ("not 5" → old_value '5'); the stored
    # row still carries the approximator. Same measurement → same row.
    cur, _st = _addr_db([
        ("one", None),
        ("all", []),
        ("all", []),
        ("all", [("octopus_entity", "about 5 centimeters")]),
    ])
    subj, attr, via = _address_scalar_target(
        cur, "compound_entity", "measure", "5", "u1")
    assert (subj, attr, via) == ("octopus_entity", "measure", "unique_holder")


def test_rung3_still_declines_on_genuinely_different_number():
    # A correction naming 7 must NEVER pin the row that says 5 — the value gate is what
    # stops a correction retiring a fact the user did not name.
    cur, _st = _addr_db([
        ("one", None),
        ("all", []),
        ("all", []),
        ("all", [("octopus_entity", "about 5 centimeters")]),
    ])
    subj, attr, via = _address_scalar_target(
        cur, "compound_entity", "measure", "7 centimeters", "u1")
    assert via is None


def test_rung3_still_declines_on_different_unit():
    cur, _st = _addr_db([
        ("one", None),
        ("all", []),
        ("all", []),
        ("all", [("octopus_entity", "about 5 centimeters")]),
    ])
    subj, attr, via = _address_scalar_target(
        cur, "compound_entity", "measure", "5 meters", "u1")
    assert via is None


def test_rung3_digitless_pair_keeps_strict_decline():
    # Parity with the SS4 pin: word-numerals on both sides → both keys None → the
    # strict-compare decline stands byte-for-byte (no numeric admission invented).
    cur, _st = _addr_db([
        ("one", None),
        ("all", []),
        ("all", []),
        ("all", [("24c2ea53", "eleven")]),
    ])
    subj, attr, via = _address_scalar_target(
        cur, "6719a70e", "member_of", "twelve", "u1")
    assert via is None


def test_rung1_exact_still_short_circuits_before_any_value_gate():
    # The deploy-#5 shape: subject HOLDS the attribute → rung 1 exact, value never gated.
    cur, _st = _addr_db([("one", (1,))])
    subj, attr, via = _address_scalar_target(
        cur, "e1", "measure", "5 centimeters", "u1")
    assert (subj, attr, via) == ("e1", "measure", "exact")


# ── E2E — the full chain on a fresh in-process seat (rows are the bar) ──────────────────

pytestmark_e2e = pytest.mark.skipif(
    not _DSN or "test" not in _DSN.rsplit("/", 1)[-1],
    reason="E2E needs a throwaway test-named POSTGRES_DSN (skipped without one — disclosed)")

_KIND = "The blue-ringed octopus is a cephalopod."
_MEASURE = "An adult blue-ringed octopus measures about 5 centimeters across."
_CORRECTION = "Actually, the blue-ringed octopus measures 7 centimeters across, not 5."
# deploy-#5 regression shape: NON-compound subject — the scalar row lives on the SAME
# entity the correction names, so rung 1 (exact) must keep firing.
_MEASURE_PLAIN = "The octopus measures about 5 centimeters across."
_CORRECTION_PLAIN = "Actually, the octopus measures 7 centimeters across, not 5."

_EXTRACTION = {
    "subject_name": "blue-ringed octopus",
    "old_rel_type": "measure",
    "old_value": "5 centimeters",
    "new_rel_type": "measure",
    "new_value": "7 centimeters",
    "dimension": "RELATIONAL",
    "confidence": 0.95,
}
_EXTRACTION_PLAIN = dict(_EXTRACTION, subject_name="octopus")


@pytest.fixture()
def cx12_tenant(monkeypatch):
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


def _drive_chain(user_id, monkeypatch, kind_text, measure_text,
                 correction_text, extraction):
    """Real harvest → /ingest for the two statement turns, then /retract/correct with the
    brain mocked to the live deploy's extraction shape. Returns the correction response."""
    import src.api.main as M
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
        from src.extraction import linguistics as _L
        import time
        t0 = time.time()
        while _L._nlp is None and time.time() - t0 < 120:
            time.sleep(0.5)
        assert _L._nlp is not None, "spaCy layer never loaded — the spine would no-op"

        for text in (kind_text, measure_text):
            h = client.post("/harvest-spans", json={"text": text, "user_id": user_id})
            assert h.status_code == 200, h.text[:200]
            edges = h.json().get("edges") or []
            assert edges, f"harvest produced no edges for {text!r}"
            r = client.post("/ingest", json={"text": text, "user_id": user_id,
                                             "edges": edges, "source": "mcp"})
            assert r.status_code == 200, r.text[:200]

        monkeypatch.setattr(M, "_unified_correction_extraction_llm",
                            lambda **k: dict(extraction))
        r = client.post("/retract/correct", json={"text": correction_text,
                                                  "user_id": user_id, "attested": True})
        assert r.status_code == 200, r.text[:200]
        return r.json()


def test_e2e_compound_chain_correction_lands_and_retains(cx12_tenant, monkeypatch):
    user_id, schema = cx12_tenant
    out = _drive_chain(user_id, monkeypatch, _KIND, _MEASURE, _CORRECTION,
                       _EXTRACTION)

    # FROZEN BAR 1: the correction SUCCEEDS on the SCALAR axis.
    assert out["status"] == "corrected", (out.get("status"), out.get("message"))
    assert out["dimension"] == "SCALAR", out
    assert out["facts_superseded"] >= 1, out

    # The row reads measure=7 centimeters LIVE — retire + retain, never annihilate.
    attrs = _rows(schema, "SELECT entity_id, attribute, value_text, provenance, "
                          "superseded_at FROM entity_attributes")
    assert len(attrs) == 1, attrs
    ent, attr, val, prov, sup = attrs[0]
    assert (attr, val) == ("measure", "7 centimeters"), attrs
    assert prov == "user_corrected", attrs
    assert sup is None, attrs

    # The prior value is RETAINED in the audit history — one superseded action row.
    import psycopg2
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(
                "SELECT attribute, prior_value_text, new_value_text, prior_provenance, "
                "new_provenance, action, cause FROM entity_attributes_history")
            hist = cur.fetchall()
    finally:
        conn.close()
    assert len(hist) == 1, hist
    h_attr, h_prior, h_new, h_pprov, h_nprov, h_action, h_cause = hist[0]
    assert (h_attr, h_prior, h_new) == ("measure", "about 5 centimeters",
                                        "7 centimeters"), hist
    assert h_action == "superseded" and h_cause == "correct_fact", hist

    # The compound type structure from turn (a) is untouched by the correction.
    aliases = _rows(schema, "SELECT alias FROM entity_aliases")
    assert ("blue-ringed octopus",) in aliases, aliases
    facts = _rows(schema, "SELECT rel_type FROM facts WHERE deleted_at IS NULL "
                          "AND superseded_at IS NULL")
    assert ("subclass_of",) in facts, facts


def test_e2e_noncompound_regression_deploy5_shape(cx12_tenant, monkeypatch):
    # Deploy #5: scalar on the SAME entity the correction names → rung 1 exact → green
    # (this shape was NEVER broken; the pin keeps it that way).
    user_id, schema = cx12_tenant
    out = _drive_chain(user_id, monkeypatch, _MEASURE_PLAIN, _MEASURE_PLAIN,
                       _CORRECTION_PLAIN, _EXTRACTION_PLAIN)
    assert out["status"] == "corrected", (out.get("status"), out.get("message"))
    attrs = _rows(schema, "SELECT attribute, value_text, superseded_at "
                          "FROM entity_attributes")
    assert attrs == [("measure", "7 centimeters", None)], attrs
