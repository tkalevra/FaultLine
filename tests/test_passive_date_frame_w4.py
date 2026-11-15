"""W4 — passive-date-frame lane (issue #19 wound 4, deploy-#10 net4/str/tfsa).

THREE defects, one lane:
  (a) "My firewall rules were last updated on January 15th, 2026." → the deploy
      minted `owns firewall rules @2026-01-15` (request-level date smear onto the
      possession twin) and the passive predication filed at the bare LEMMA "rule"
      (a junk island the walk never reaches) → "When were my firewall rules last
      updated?" rendered EMPTY. Fixes: `_chain_passive_event` branch (d) resolves
      the COMPOUND NP surface (`_np_phrase`); a batch edge carrying its own
      per-edge `event_date` HOSTS the date (`_batch_has_event_host`) so the
      request-level smear cannot date the undated `owns`/`instance_of` twins; the
      RECENCY-ADVERB "last" ("when were X last <V-ed>") is masked out of the
      ordinal matcher (it advmod-modifies the verb under a when-fronting — not a
      chronological ordinal over dated events).
  (b) "my Stratocaster was built in 1998" → @1998-09-19, a FABRICATED day
      dateparser produced by inheriting the reference's month/day for a YEAR-only
      span (the YMD snap). Fix: `_resolve_first_valid_date` normalizes a
      year/month-granular span to YYYY-01-01 / YYYY-MM-01; the render slices the
      date at the STORED event_date_granularity ("1998" / "2023-10"); the inverse
      fetch lane now CARRIES the event columns so a when-ask answered through it
      can render the date at all.
  (c) "My TFSA contribution room for 2026 is 7000 dollars" → `instance_of
      expense @<today>` — the request-level smear stamped a year-in-NP snap onto
      a CLASSIFICATION edge. Fixed by the same host-selection change as (a)
      (pinned via the apollo control, which reproduced the class at HEAD); the
      TFSA turn itself now lands as the W1 possessive-quantity scalar with no
      dated classification row.

Unit pins run everywhere; E2E pins skip without a throwaway test-named
POSTGRES_DSN — disclosed, never a silent pass.
"""
import os
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("SENTENCE_PIPELINE", "true")
# PD3 (r1 GAPS): environment-independence — the battery must never depend on a live
# brain. Deterministic segmentation is PRIMARY for these pins; the LLM atomizer is
# escalated only for parse-residual units (and keyless brains then degrade those
# escalations harmlessly). setdefault: an explicit runner value still wins.
os.environ.setdefault("SPINE_DETERMINISTIC_SEGMENTATION", "true")

_DSN = os.environ.get("POSTGRES_DSN", "")

REF = datetime(2026, 9, 20, tzinfo=timezone.utc)

FW = "My firewall rules were last updated on January 15th, 2026."
FW_Q = "When were my firewall rules last updated?"
STRAT = "My Stratocaster was built in 1998."
STRAT_Q = "When was my Stratocaster built?"
TFSA = "My TFSA contribution room for 2026 is 7000 dollars."
TFSA_Q = "What is my TFSA contribution room?"
TREATY = "The treaty was signed on March 1st, 1994."
TREATY_Q = "When was the treaty signed?"
MOVED = "We moved in October 2023."
MOVED_Q = "When did we move?"
APOLLO = "My server Apollo was provisioned on March 1, 2023."


# ─────────────────────────────────────────────────────────────────────────────
# (b) UNIT — the resolver never fabricates a day the user did not state
# ─────────────────────────────────────────────────────────────────────────────

def test_year_only_span_normalizes_to_jan1_year_gran():
    from src.extraction.linguistics import extract_event_date
    iso, gran = extract_event_date(STRAT, REF)
    assert iso is not None and iso.startswith("1998-01-01"), iso
    assert gran == "year", gran


def test_month_only_span_normalizes_to_month_first_day_gran_month():
    from src.extraction.linguistics import extract_event_date
    iso, gran = extract_event_date(MOVED, REF)
    assert iso is not None and iso.startswith("2023-10-01"), iso
    assert gran == "month", gran


def test_year_in_np_for_2026_is_year_gran_jan1():
    """The TFSA year-in-NP shape: 'for 2026' is a YEAR expression — never the
    reference's month/day (the deploy-#10 @<today> snap source)."""
    from src.extraction.linguistics import extract_event_date
    iso, gran = extract_event_date(TFSA, REF)
    assert iso is not None and iso.startswith("2026-01-01"), iso
    assert gran == "year", gran


def test_day_exact_spans_unchanged():
    from src.extraction.linguistics import extract_event_date
    iso, gran = extract_event_date(TREATY, REF)
    assert iso is not None and iso.startswith("1994-03-01"), iso
    assert gran == "day", gran
    iso2, gran2 = extract_event_date(FW, REF)
    assert iso2 is not None and iso2.startswith("2026-01-15"), iso2
    assert gran2 == "day", gran2


def test_date_granularity_accepts_leading_temporal_prep():
    """'for 2026' (a year NP with a temporal-governing prep) labels YEAR — the
    closed prep class, not a domain list."""
    from src.extraction.linguistics import _date_granularity
    assert _date_granularity("for 2026", None) == "year"
    assert _date_granularity("during 2024", None) == "year"
    assert _date_granularity("in 1998", None) == "year"
    assert _date_granularity("1998", None) == "year"
    assert _date_granularity("January 15th, 2026", None) == "day"
    assert _date_granularity("October 2023", None) == "month"


def test_durative_inception_arithmetic_not_snapped():
    """The durative inception (reference − duration, ARITHMETIC) keeps its exact
    day — the snap gate normalizes SPAN parses only, never the derived inception
    (pinned by test_duration_since_now_cluster: 2022-06-11, not 2022-06-01)."""
    from src.extraction.linguistics import durative_inception_from_text
    iso, gran = durative_inception_from_text(
        "I have had my cat Luna for 9 months now.",
        datetime(2023, 3, 11, tzinfo=timezone.utc))
    assert iso is not None and str(iso).startswith("2022-06-11"), iso


# ─────────────────────────────────────────────────────────────────────────────
# (a) UNIT — the passive predication files at the COMPOUND subject, undated twin
# ─────────────────────────────────────────────────────────────────────────────

def _fw_edges():
    from src.extraction.linguistics import derive_sentence_facts
    return list(derive_sentence_facts(FW, REF))


def test_passive_event_files_at_compound_subject():
    edges = _fw_edges()
    upd = [e for e in edges if e.rel_type == "updated"]
    assert upd, edges
    assert (upd[0].subject or "").lower() == "firewall rules", upd[0].subject
    assert str(upd[0].object or "").startswith("2026-01-15"), upd[0].object
    assert str(getattr(upd[0], "event_date", "") or "").startswith("2026-01-15"), upd[0].event_date
    # no bare-lemma island twin
    assert not [e for e in edges if (e.subject or "").lower() == "rule"], edges


def test_passive_possession_twin_carries_no_date():
    edges = _fw_edges()
    owns = [e for e in edges if e.rel_type == "owns"
            and "firewall" in (e.object or "")]
    assert owns, edges
    assert not getattr(owns[0], "event_date", None), owns[0].event_date


def test_passive_treaty_control_subject_agnostic():
    from src.extraction.linguistics import derive_sentence_facts
    edges = list(derive_sentence_facts(TREATY, REF))
    signed = [e for e in edges if e.rel_type == "signed"]
    assert signed, edges
    assert (signed[0].subject or "").lower() == "treaty", signed[0].subject
    assert str(signed[0].object or "").startswith("1994-03-01"), signed[0].object


def test_passive_strat_year_scalar():
    from src.extraction.linguistics import derive_sentence_facts
    edges = list(derive_sentence_facts(STRAT, REF))
    built = [e for e in edges if e.rel_type == "built"]
    assert built, edges
    assert (built[0].subject or "").lower() == "stratocaster", built[0].subject
    # year-granular value: the user-given precision, never a fabricated day
    assert str(built[0].object or "") == "1998", built[0].object
    assert str(getattr(built[0], "event_date", "") or "").startswith("1998-01-01")
    assert (getattr(built[0], "event_date_granularity", "") or "") == "year"


# ─────────────────────────────────────────────────────────────────────────────
# (a) UNIT — the RECENCY-ADVERB "last" is not a chronological ordinal
# ─────────────────────────────────────────────────────────────────────────────

def test_recency_adverb_last_masked_under_when_fronting():
    from src.api.main import _detect_temporal_query_intent as dt
    intent = dt(FW_Q)
    assert intent.get("when_event") is True
    assert "ordinal" not in intent, intent
    assert "order" not in intent, intent


def test_recency_adverb_mask_generalizes():
    from src.api.main import _detect_temporal_query_intent as dt
    for q in ("When did I last see her?",
              "When was the firmware last changed?",
              "When were the rules last modified?"):
        intent = dt(q)
        assert "ordinal" not in intent, (q, intent)


def test_genuine_ordinals_keep_their_ordinal():
    from src.api.main import _detect_temporal_query_intent as dt
    # the chronological-ordinal ask ("which ONE in the sequence") — not when-fronted
    assert dt("Which task did I complete last?").get("ordinal") == "last"
    assert dt("What was the last movie I watched?").get("ordinal") == "last"
    # when-fronted but "last" attaches to a NOUN (amod), not the verb → ordinal kept
    assert dt("When did I complete my last task?").get("ordinal") == "last"


# ─────────────────────────────────────────────────────────────────────────────
# (r2 A) UNIT — the ONE passive-clause detector the connect consults: a passive
# auxiliary is never a possessive-attribute copula (the W4 chain owns the date)
# ─────────────────────────────────────────────────────────────────────────────

def test_passive_clause_detector_shapes():
    from src.extraction.linguistics import clause_is_passive_predication as is_passive
    # the passive-event predications the W4 chain owns (objectless, value in-clause)
    assert is_passive(FW, "January 15th, 2026")
    assert is_passive(TREATY, "March 1st, 1994")
    assert is_passive(STRAT, "1998")
    # a genuine possessive-attribute COPULA is not a passive — the connect still binds
    assert not is_passive("My email address is jordan.smith@example.com.",
                          "jordan.smith@example.com")
    assert not is_passive("My phone number is 519-555-0123.", "519-555-0123")
    # a by-agent passive is NOT the objectless W4 shape (the agent chain owns it)
    assert not is_passive("My email was reset by the admin on January 5, 2026.",
                          "January 5, 2026")


# ─────────────────────────────────────────────────────────────────────────────
# (r2 B) UNIT — a date-shaped atomic value is the temporal DEFAULT, never an
# aspect term: no PLACE is written for the slot name
# ─────────────────────────────────────────────────────────────────────────────

def test_atomic_date_shaped_value_gate():
    from src.api.main import _atomic_value_is_date_shaped as is_date
    assert is_date("January 15th, 2026")
    assert is_date("1990-05-15")
    # identifier values never parse as dates — their terms still ground
    assert not is_date("519-555-0123")
    assert not is_date("10.0.0.1")
    assert not is_date("jordan.smith@example.com")


# ─────────────────────────────────────────────────────────────────────────────
# (b) UNIT — the render slices at the stored granularity
# ─────────────────────────────────────────────────────────────────────────────

def _mock_db():
    db = MagicMock()
    cursor = MagicMock()
    db.cursor.return_value.__enter__.return_value = cursor
    return db, cursor


def _render_dated(rel, event_date, gran):
    """Render ONE non-historical dated fact through the REAL convert_to_prose
    Fix-C block (mock db: no templates → the label fallback path)."""
    from src.api.main import convert_to_prose
    u = str(uuid.uuid4())
    fact = {"subject_id": u, "rel_type": rel, "object_id": None,
            "object": "1", "fact_class": "A", "confidence": 1.0,
            "event_date": event_date, "event_date_granularity": gran,
            "rel_type_definition": None, "category": "state"}
    db, cur = _mock_db()
    cur.fetchall.return_value = []
    with patch("src.api.main.resolve_display_name", return_value="x"), \
            patch("src.api.main.rel_type_overlay.resolve_current", return_value={}):
        out = convert_to_prose([fact], db)
    return " ".join(out)


def test_render_slices_year_granularity():
    prose = _render_dated("built", "1998-01-01T00:00:00+00:00", "year")
    assert "(on 1998)" in prose, prose
    assert "1998-01" not in prose, prose


def test_render_slices_month_granularity():
    prose = _render_dated("relocate", "2023-10-01T00:00:00+00:00", "month")
    assert "(on 2023-10)" in prose, prose


def test_render_day_granularity_byte_identical_legacy():
    prose = _render_dated("complete", "2026-01-15T00:00:00+00:00", "day")
    assert "(on 2026-01-15)" in prose, prose
    prose_none = _render_dated("complete", "2026-01-15T00:00:00+00:00", None)
    assert "(on 2026-01-15)" in prose_none, prose_none


# ─────────────────────────────────────────────────────────────────────────────
# E2E — fresh seat: sentences in → rows + renders out (skips without DSN)
# ─────────────────────────────────────────────────────────────────────────────

pytestmark_e2e = pytest.mark.skipif(
    "test" not in _DSN.lower(),
    reason="no throwaway test-named POSTGRES_DSN")


@pytest.fixture()
def w4_tenant(monkeypatch):
    if not _DSN:
        pytest.skip("no POSTGRES_DSN")
    monkeypatch.delenv("SAAS_MODE", raising=False)
    import psycopg2
    from src.provisioning.schema_manager import (
        create_user_schema, derive_user_slug_from_uuid)
    # PD3 (r1 GAPS, round 2): create the wound's PRECONDITION ourselves — the scalar_atomic
    # human-date row(s) the atomic detector reads on its UNBOUND connection (public
    # template). The r1 pins passed in an env whose public lacked them, so the poisoning
    # chain never fired and the battery guarded nothing. Idempotent: insert only when the
    # date rows are absent. THROWAWAY DB ONLY (fixture refuses any non-test DSN above).
    _seed_conn = psycopg2.connect(_DSN)
    try:
        with _seed_conn.cursor() as _scur:
            _scur.execute(
                "SELECT 1 FROM public.extraction_patterns "
                "WHERE category = 'scalar_atomic' AND rel_type = 'born_on' LIMIT 1")
            if not _scur.fetchone():
                _scur.execute(
                    "INSERT INTO public.extraction_patterns "
                    "(pattern_regex, rel_type, description, example_text, category, source,"
                    " global_confidence) VALUES (%s,%s,%s,%s,%s,%s,%s), (%s,%s,%s,%s,%s,%s,%s)",
                    (r'\b\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\b',
                     'born_on', 'ISO 8601 date YYYY-MM-DD', '1990-05-15',
                     'scalar_atomic', 'bootstrap', 0.94,
                     r'\b(?:\d{1,2}\s+)?(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|'
                     r'May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|'
                     r'Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b',
                     'born_on', 'Human-readable date with month name', 'May 15, 1990',
                     'scalar_atomic', 'bootstrap', 0.90))
        _seed_conn.commit()
    finally:
        _seed_conn.close()
    user_id = str(uuid.uuid4())
    slug = derive_user_slug_from_uuid(user_id)
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


@pytest.fixture()
def w4_client(monkeypatch):
    import src.api.main as M
    from starlette.testclient import TestClient
    monkeypatch.setattr(M, "_resolve_atomic_subjects_via_llm",
                        lambda text, unresolved, user_id: [])
    M._INGEST_ENABLED = True

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
        t0 = time.time()
        while _L._nlp is None and time.time() - t0 < 120:
            time.sleep(0.5)
        assert _L._nlp is not None
        yield client


def _rows(schema, sql, params=()):
    import psycopg2
    c = psycopg2.connect(_DSN)
    try:
        with c.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        c.close()


def _ingest(client, user_id, text):
    h = client.post("/harvest-spans", json={"text": text, "user_id": user_id})
    assert h.status_code == 200, h.text[:400]
    edges = h.json().get("edges") or []
    r = client.post("/ingest", json={"text": text, "user_id": user_id,
                                     "edges": edges, "source": "mcp"})
    assert r.status_code == 200, r.text[:400]
    return edges, r.json()


def _query(client, user_id, text):
    resp = client.post("/query", json={"text": text, "user_id": user_id})
    assert resp.status_code == 200, resp.text[:400]
    return resp.json()


@pytestmark_e2e
def test_firewall_passive_event_edge_and_render(w4_tenant, w4_client):
    user_id, schema = w4_tenant
    edges, ingest = _ingest(w4_client, user_id, FW)
    # the passive predication edge carries the date; the possession twin does NOT
    upd = [e for e in edges if e.get("rel_type") == "updated"]
    assert upd, edges
    assert (upd[0].get("subject") or "").lower() == "firewall rules", upd[0]
    assert str(upd[0].get("event_date") or "").startswith("2026-01-15"), upd[0]
    owns = [e for e in edges if e.get("rel_type") == "owns"
            and "firewall" in (e.get("object") or "")]
    assert owns and not owns[0].get("event_date"), owns
    # r2 seam A: the passive clause owns the date — NO date-pattern default twin
    assert not [e for e in edges if e.get("rel_type") == "born_on"], edges
    # DB: the attr hangs on the SAME entity the owns edge links to (no island)
    attr_rows = _rows(schema,
                      "SELECT ea.entity_id, ea.value_text FROM entity_attributes ea "
                      "WHERE ea.attribute = 'updated'")
    assert attr_rows and attr_rows[0][1] == "2026-01-15", attr_rows
    fw_id = attr_rows[0][0]
    own_rows = _rows(schema,
                     "SELECT f.object_id FROM facts f "
                     "WHERE f.rel_type = 'owns' AND f.object_id = %s",
                     (fw_id,))
    assert own_rows, f"the dated predication is an ISLAND: owns does not link {fw_id}"
    dated = _rows(schema,
                  "SELECT rel_type, event_date FROM facts WHERE event_date IS NOT NULL")
    assert not dated, f"date bled onto a facts row: {dated}"
    # r2 seam B: NO aspect-PLACE poisoning — the user's slot term is never grounded as a
    # rel_type_aliases PLACE off the temporal default, and no staged rung demotes the
    # user's OWN thing to an L4 place (the rung is what place-blocked the anchor in r1).
    poisoned = _rows(schema,
                     "SELECT alias FROM rel_type_aliases "
                     "WHERE source = 'engine' AND alias LIKE 'firewall%%'")
    assert not poisoned, f"aspect-PLACE poisoning: {poisoned}"
    rungs = _rows(schema,
                  "SELECT rel_type FROM staged_facts WHERE subject_id = %s"
                  " AND rel_type = 'subclass_of'", (fw_id,))
    assert not rungs, f"the user's thing was demoted to an L4 place: {rungs}"
    # the when-ask renders the date (was EMPTY on deploy #10 / r1 GAPS)
    j = _query(w4_client, user_id, FW_Q)
    defs = " ".join(str(f.get("definition") or "") for f in (j.get("facts") or []))
    assert "2026-01-15" in defs, (defs, j.get("facts"))
    # r2 seam D negative: reach ≠ render — the SCOPED when-ask must never RENDER the
    # off-scope owns edge, even though the walk may reach THROUGH it.
    assert "own" not in defs.lower(), defs


@pytestmark_e2e
def test_stratocaster_year_granularity_and_render(w4_tenant, w4_client):
    user_id, schema = w4_tenant
    edges, ingest = _ingest(w4_client, user_id, STRAT)
    built = [e for e in edges if e.get("rel_type") == "built"]
    assert built, edges
    assert (built[0].get("event_date_granularity") or "") == "year", built[0]
    assert str(built[0].get("event_date") or "").startswith("1998-01-01"), built[0]
    assert str(built[0].get("object") or "") == "1998", built[0]
    # the DB row: value_date 1998-01-01 (never the reference month/day)
    rows = _rows(schema,
                 "SELECT value_text, value_date FROM entity_attributes "
                 "WHERE attribute = 'built'")
    assert rows and rows[0][0] == "1998", rows
    assert rows[0][1] is not None and str(rows[0][1]).startswith("1998-01-01"), rows
    # render: the YEAR, never a fabricated day
    j = _query(w4_client, user_id, STRAT_Q)
    defs = " ".join(str(f.get("definition") or "") for f in (j.get("facts") or []))
    assert "1998" in defs, (defs, j.get("facts"))
    assert "1998-09" not in defs and "1998-01-01" not in defs, defs


@pytestmark_e2e
def test_tfsa_year_in_np_no_classification_date_bleed(w4_tenant, w4_client):
    user_id, schema = w4_tenant
    edges, ingest = _ingest(w4_client, user_id, TFSA)
    # the W1 possessive-quantity scalar owns the frame — no classification twin
    assert not [e for e in edges if e.get("rel_type") in ("instance_of", "is_a")], edges
    # NOTHING in the turn carries a request-smeared event_date
    assert not [e for e in edges if e.get("event_date")], edges
    dated = _rows(schema, "SELECT rel_type, event_date FROM facts "
                          "WHERE event_date IS NOT NULL")
    assert not dated, f"date bled onto a row: {dated}"
    # the scalar itself still answers
    j = _query(w4_client, user_id, TFSA_Q)
    defs = " ".join(str(f.get("definition") or "") for f in (j.get("facts") or []))
    assert "7000" in defs, (defs, j.get("facts"))


@pytestmark_e2e
def test_apollo_classification_edge_undated(w4_tenant, w4_client):
    """The (c) class reproduced at HEAD: a passive turn's instance_of row must
    carry NO event_date (the date hosts on the provisioned predication). The
    instance_of mint is ATOMIZER-DEPENDENT (the deterministic fallback may emit
    fewer edges), so the pin is conditional: any classification row that DID
    land carries no date; the dated predication itself always survives."""
    user_id, schema = w4_tenant
    edges, ingest = _ingest(w4_client, user_id, APOLLO)
    rows = _rows(schema, "SELECT event_date FROM facts WHERE rel_type = 'instance_of'")
    assert all(r[0] is None for r in rows), rows
    owns = _rows(schema, "SELECT event_date FROM facts WHERE rel_type = 'owns'")
    assert all(r[0] is None for r in owns), owns
    # the dated predication itself survives (attr-scalar with the day-exact date)
    prov = _rows(schema, "SELECT value_text FROM entity_attributes "
                         "WHERE attribute = 'provisioned'")
    assert prov and str(prov[0][0]).startswith("2023-03-01"), prov


@pytestmark_e2e
def test_moved_month_only_consistent(w4_tenant, w4_client):
    user_id, schema = w4_tenant
    edges, ingest = _ingest(w4_client, user_id, MOVED)
    rows = _rows(schema, "SELECT event_date, event_date_granularity FROM facts "
                         "WHERE event_date IS NOT NULL")
    assert rows, "the moved occurrence lost its date entirely"
    ed, gran = rows[0][0], rows[0][1]
    assert str(ed).startswith("2023-10-01"), ed      # month-first, never the ref day
    assert gran == "month", gran
    j = _query(w4_client, user_id, MOVED_Q)
    defs = " ".join(str(f.get("definition") or "") for f in (j.get("facts") or []))
    assert "2023-10" in defs, (defs, j.get("facts"))


@pytestmark_e2e
def test_treaty_passive_non_tech_control(w4_tenant, w4_client):
    user_id, schema = w4_tenant
    edges, ingest = _ingest(w4_client, user_id, TREATY)
    signed = [e for e in edges if e.get("rel_type") == "signed"]
    assert signed, edges
    assert (signed[0].get("subject") or "").lower() == "treaty", signed[0]
    assert str(signed[0].get("event_date") or "").startswith("1994-03-01"), signed[0]
    j = _query(w4_client, user_id, TREATY_Q)
    defs = " ".join(str(f.get("definition") or "") for f in (j.get("facts") or []))
    assert "1994-03-01" in defs, (defs, j.get("facts"))


# ─────────────────────────────────────────────────────────────────────────────
# (r2 C) E2E — the anchor reader follows the writer's key: a multiword possessed
# NP that missed on its RAW surface resolves through normalize_rel
# ─────────────────────────────────────────────────────────────────────────────

@pytestmark_e2e
def test_anchor_reader_follows_writer_normalized_key(w4_tenant):
    """An entity keyed ONLY under the normalized form ('firewall_rule', the key
    `canonical.record_alias` / the aspect-term writer uses) must still anchor a
    possessive ask phrased with the raw surface ('my firewall rules') — the
    equivalence is decided on the normalized forms of BOTH operands."""
    import psycopg2
    from src.api.main import resolve_anchor
    user_id, schema = w4_tenant
    ent = str(uuid.uuid4())
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute("INSERT INTO entities (id, entity_type) VALUES (%s, 'Object')",
                        (ent,))
            cur.execute("INSERT INTO entity_aliases (entity_id, alias, is_preferred) "
                        "VALUES (%s, 'firewall_rule', true)", (ent,))
        conn.commit()
    finally:
        conn.close()
    from unittest.mock import MagicMock
    db = MagicMock()
    cur = MagicMock()
    db.cursor.return_value.__enter__.return_value = cur
    _hits = {"firewall rules": None, "firewall_rule": (ent,)}

    def _execute(sql, params=()):
        cur.fetchone.return_value = _hits.get(params[0] if params else None)

    cur.execute.side_effect = _execute
    cur.fetchall.return_value = []
    anchor = resolve_anchor(FW_Q, [], user_id, db)
    assert anchor == ent, anchor


# ─────────────────────────────────────────────────────────────────────────────
# (r2 D) UNIT — reach ≠ render: the scoped fetch collects PRE-projection 1-hop
# neighbours into the out-set but never renders the off-scope linking edge
# ─────────────────────────────────────────────────────────────────────────────

@pytestmark_e2e
def test_scoped_fetch_reach_without_render(w4_tenant, w4_client):
    user_id, schema = w4_tenant
    edges, ingest = _ingest(w4_client, user_id, FW)
    attr_rows = _rows(schema,
                      "SELECT entity_id FROM entity_attributes WHERE attribute = 'updated'")
    assert attr_rows
    fw_id = attr_rows[0][0]
    from src.api.models import QueryPath
    from src.api.main import fetch_facts_from_anchor
    path = QueryPath()
    path.relationship_rels = ["updated"]
    path.scalar_rels = []
    path.scope_active = True
    reach: set = set()
    facts = fetch_facts_from_anchor(
        user_id, user_id, path, reach_uuids=reach)
    # RENDER: the owns edge is out of concept scope — never rendered
    assert not [f for f in facts if f.get("rel_type") == "owns"], facts
    # REACH: the neighbour uuid IS collected pre-projection (the chicken-and-egg closed)
    assert fw_id in reach, reach
