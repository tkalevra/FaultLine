"""R6 — the per-chunk PASSAGE lane ("read the chunk that answers the question").

Pins (plan DEV/BUGS/plan-100pct-addressability-20261024.md §R6):

  (a) "how efficient are wind turbines" against a doc whose chunk N carries
      "20-40% conversion efficiency" → the hit IS that chunk's TEXT (chunk_index N,
      chunk_text verbatim) — not a stitched ts_headline, not a document-level banner.
  (b) a multi-chunk doc where many chunks match → the TOP-3 chunks by per-chunk
      ts_rank, ranked (rank descending), never ALL chunks; a non-matching chunk never
      appears.
  (c) no-match / junk-only / hostile tsquery metacharacters / blank / anonymous /
      pre-299 schema / unprovisioned tenant → EMPTY, and the lane NEVER raises.
  (d) tenant isolation: a doc seeded in tenant A is invisible to tenant B (the bound
      search_path IS the boundary) — B's identical query returns B's passages only,
      never A's.
  (e) recall renders the passage: "From your document <title>: <the actual chunk
      text that matches>" — deterministic string shaping, no LLM; a title-only
      discovery row keeps the W2 "You have a document: <title>" shape.

Determinism pin: identical repeated calls return identical results (zero LLM, zero
randomness — pure SQL).

DB pins run against a dedicated throwaway database booted with the real migration
corpus (the W2 harness convention — the fanout sweep takes ACCESS EXCLUSIVE locks
and must never touch a shared DB). MCP pins mock the network.
"""

import json
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DSN = os.environ.get("POSTGRES_DSN")

from src.mcp.server import (  # noqa: E402
    _render_document_hit_lines,
    recall_memory_tool,
)


def _resp(payload, status=200):
    m = MagicMock()
    m.status_code = status
    m.json = lambda: payload
    m.raise_for_status = lambda: None
    return m


# ── pin (e), unit half: the renderer speaks the passage ─────────────────────────────────

def test_render_passage_line_is_the_chunk_text():
    """PASSAGE hit → 'From your document <title>: <chunk text> (document id N, section M+1)'."""
    lines = _render_document_hit_lines([
        {"document_id": 21, "title": "Wind Energy Handbook", "status": "ready",
         "chunk_index": 6, "chunk_text": "Modern turbines achieve 20-40% conversion "
         "efficiency at optimal wind speeds.", "chunk_count": 18, "rank": 0.09},
    ])
    assert lines == [
        "From your document Wind Energy Handbook: Modern turbines achieve 20-40% "
        "conversion efficiency at optimal wind speeds. (document id 21, section 7)."
    ]


def test_render_title_only_row_keeps_w2_discovery_shape():
    """No chunk matched (title/source only) → the W2 discovery line, honest status."""
    lines = _render_document_hit_lines([
        {"document_id": 21, "title": "Wind Energy Handbook", "status": "processing",
         "chunk_index": None, "chunk_text": None, "chunk_count": 18, "rank": None},
    ])
    assert lines == [
        "You have a document: Wind Energy Handbook (document id 21; import processing "
        "— verbatim text only, facts still extracting)."
    ]


def test_render_passage_stays_verbatim_including_uuids():
    """A UUID quoted in the user's own document is CONTENT, not a store entity id to
    redact — the passage lane quotes it verbatim (membership-gated strip skipped)."""
    lines = _render_document_hit_lines([
        {"document_id": 5, "title": "KB Article", "status": "ready", "chunk_index": 2,
         "chunk_text": "Set tenant id 3f2504e0-4f89-11d3-9a0c-0305e82c3301 then reboot.",
         "chunk_count": 9, "rank": 0.1},
    ])
    assert "3f2504e0-4f89-11d3-9a0c-0305e82c3301" in lines[0]
    # Internal annotation debris DOES still get cleaned from the passage.
    lines2 = _render_document_hit_lines([
        {"document_id": 5, "title": "KB", "status": "ready", "chunk_index": 0,
         "chunk_text": "confidence=0.9 some flagged text", "chunk_count": 9}])
    assert "confidence=" not in lines2[0] and "some flagged text" in lines2[0]


def test_render_never_raises_on_malformed_hits():
    assert _render_document_hit_lines([]) == []
    # Non-dict junk is skipped; empty/None-passage dicts render the discovery shape
    # honestly (two identical lines here — never a raise).
    assert _render_document_hit_lines(
        [None, 42, "junk", {}, {"chunk_text": None}]) == [
        "You have a document: document None (document id None).",
        "You have a document: document None (document id None)."]


# ── pin (e), e2e half: the recall surface carries the passage ───────────────────────────

async def test_recall_empty_walk_renders_the_passage(monkeypatch):
    """Empty fact walk + passage hit → the PASSAGE is the grounded answer surface."""
    import src.mcp.server as s

    async def fake_post(url, json=None, timeout=None, headers=None):
        if url.endswith("/classify-intent"):
            return _resp({"intent": "QUERY", "confidence": 0.9})
        if url.endswith("/query"):
            return _resp({"facts": [], "attributes": {}, "document_hits": [
                {"document_id": 21, "title": "Wind Energy Handbook", "status": "ready",
                 "chunk_index": 6, "chunk_text": "Modern turbines achieve 20-40% "
                 "conversion efficiency at optimal wind speeds.",
                 "chunk_count": 18, "rank": 0.09}]})
        raise AssertionError(url)

    async def fake_get(url, params=None, timeout=None):
        if "/confidence-gate/" in url:
            return _resp({"threshold": 0.70})
        if url.endswith("/documents/pending"):
            return _resp({"pending": False, "count": 0})
        raise AssertionError(url)

    monkeypatch.setattr(s, "_http_client",
                        MagicMock(post=AsyncMock(side_effect=fake_post),
                                  get=AsyncMock(side_effect=fake_get)))
    monkeypatch.setattr(s, "RECALL_INTENT_ROUTING", False, raising=False)
    out = await recall_memory_tool("how efficient are wind turbines", "u-alice")
    mem = out["memory"]
    assert "From your document Wind Energy Handbook: Modern turbines achieve 20-40% " \
           "conversion efficiency at optimal wind speeds." in mem
    assert "document id 21" in mem
    assert "No relevant facts found" not in mem


# ── DB pins (a)-(d): dedicated throwaway database, real migration corpus ────────────────

def _dedicated_database(suffix: str) -> str:
    from urllib.parse import urlsplit, urlunsplit
    import psycopg2
    p = urlsplit(_DSN)
    base = p.path.lstrip("/")
    assert base.endswith("_test"), "refusing to derive a throwaway from a non-*_test database"
    name = f"{base}_{suffix}"[:63]
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        conn.close()
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


def _drop_dedicated_database(dsn: str) -> None:
    from urllib.parse import urlsplit
    import psycopg2
    name = urlsplit(dsn).path.lstrip("/")
    assert "r6pass" in name and name != urlsplit(_DSN).path.lstrip("/"), name
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        conn.close()


WIND_CHUNKS = [
    "Wind power overview: turbines convert the kinetic energy of moving air into electricity.",
    "A brief history of windmills in the Netherlands and Persia.",
    "Turbine maintenance: gearbox lubrication intervals and blade inspection schedules.",
    "Modern turbines achieve 20-40% conversion efficiency at optimal wind speeds, "
    "with capacity factors around 35% offshore.",
    "Turbine efficiency depends heavily on wind speed; steady wind improves the "
    "conversion efficiency of the rotor and generator.",
    "Siting considerations: terrain shadowing, adjacent turbine wake losses, "
    "setback distances from residences.",
    "Grid integration of wind farms: frequency response, curtailment, storage pairing.",
    "The wind resource is intermittent; forecasting models reduce curtailment costs.",
]


@pytest.fixture(scope="module")
def r6_db():
    """Boot ONE throwaway database with the real migration corpus; hand back helpers."""
    if not _DSN:
        pytest.skip("POSTGRES_DSN not set")
    mig_dir = os.environ.get("FAULTLINE_MIGRATIONS_DIR", "migrations")
    if not os.path.isdir(mig_dir):
        pytest.skip(f"migrations dir {mig_dir!r} not found")
    import psycopg2

    dsn = _dedicated_database("r6pass" + uuid.uuid4().hex[:6])
    saved_dsn = os.environ.get("POSTGRES_DSN")
    os.environ["POSTGRES_DSN"] = dsn
    # FOSS convention: the whole migration corpus applies at boot (entrypoint
    # psql loop) — tests apply the files via psql -f per file, the SAME
    # per-statement continue-on-error semantics the entrypoint uses. The ported
    # migrations (298-301) are guarded-idempotent and must apply cleanly.
    import glob as _glob
    import subprocess as _sp
    _ported = ("298", "299", "300", "301")
    for _mf in sorted(_glob.glob(os.path.join(mig_dir, "*.sql"))):
        _proc = _sp.run(["psql", dsn, "-v", "ON_ERROR_STOP=0", "-q", "-f", _mf],
                        capture_output=True, text=True)
        if _proc.returncode != 0 and os.path.basename(_mf).split("_")[0] in _ported:
            raise AssertionError(f"{_mf} failed: {_proc.stderr[-400:]}")
    from src.provisioning.schema_manager import create_user_schema

    def seed(title, chunks, *, status="pending"):
        uid = str(uuid.uuid4())
        schema, st = create_user_schema(uid, uid.replace("-", "_"))
        assert st == "ready", st
        conn = psycopg2.connect(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}")
                cur.execute(
                    "INSERT INTO documents (user_id, source_ref, title, chunks, "
                    "chunk_count, status) VALUES (%s, %s, %s, %s::jsonb, %s, %s) "
                    "RETURNING id",
                    (uid, f"/docs/{schema}.pdf", title, json.dumps(chunks),
                     len(chunks), status))
                doc_id = cur.fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        return uid, doc_id

    yield {"dsn": dsn, "seed": seed}
    if saved_dsn is None:
        os.environ.pop("POSTGRES_DSN", None)
    else:
        os.environ["POSTGRES_DSN"] = saved_dsn
    _drop_dedicated_database(dsn)


def test_pin_a_question_returns_the_answer_chunk(r6_db):
    """PIN (a): the question's ANSWER chunk comes back BY TEXT — not a headline."""
    import src.api.main as M
    uid, doc_id = r6_db["seed"]("Wind Energy Handbook", WIND_CHUNKS)
    hits = M._document_hits_for_query(uid, "how efficient are wind turbines")
    assert hits, "the passage lane must hit on a natural-language question"
    # THE answer chunk (index 3 carries '20-40% conversion efficiency') is returned
    # with its ACTUAL TEXT — never a stitched headline, never just the doc banner.
    eff = [h for h in hits if h["chunk_index"] == 3]
    assert eff, f"answer chunk missing from {[(h['chunk_index'], h['chunk_text'][:40]) for h in hits]}"
    h = eff[0]
    assert h["document_id"] == doc_id
    assert h["chunk_text"] == WIND_CHUNKS[3]
    assert "20-40% conversion efficiency" in h["chunk_text"]
    assert h["rank"] > 0
    # The OLD shape is gone: no stitched headline, no section-count banner fields.
    assert "headline" not in h and "matching_chunks" not in h
    # chunk_text is verbatim passage text, not a '<<token>>'-marked snippet.
    assert "<<" not in h["chunk_text"]


def test_pin_b_multi_chunk_doc_top3_ranked_not_all(r6_db):
    """PIN (b): top-3 CHUNKS ranked by per-chunk relevance; never all chunks."""
    import src.api.main as M
    uid, doc_id = r6_db["seed"]("Efficiency Study", WIND_CHUNKS)
    # 5+ chunks mention turbine/wind/efficiency — the lane must bound to 3 passages.
    hits = M._document_hits_for_query(uid, "wind turbine efficiency")
    assert 1 <= len(hits) <= 3, f"expected <=3 passages, got {len(hits)}"
    assert len({h["chunk_index"] for h in hits}) == len(hits), "duplicate chunk ordinals"
    # Ranked: descending rank; determinism: identical repeat call.
    ranks = [h["rank"] for h in hits]
    assert ranks == sorted(ranks, reverse=True), ranks
    again = M._document_hits_for_query(uid, "wind turbine efficiency")
    assert again == hits
    # Every returned chunk actually carries a query content token ('simple' config:
    # exact tokens, so 'windmills'/'turbine' do NOT satisfy 'wind'/'turbines' — the
    # chunk text itself must carry a token).
    toks = {"wind", "turbine", "turbines", "efficiency"}
    for h in hits:
        words = set("".join(c if c.isalnum() else " " for c in h["chunk_text"].lower()).split())
        assert words & toks, h["chunk_text"][:60]


def test_pin_c_no_match_and_error_shapes_empty_never_raises(r6_db):
    """PIN (c): every degenerate input → [], and nothing ever raises."""
    import src.api.main as M
    import psycopg2
    from src.provisioning.schema_manager import create_user_schema
    uid, _doc = r6_db["seed"]("Wind Energy Handbook", WIND_CHUNKS)
    # No lexical match under the 'simple' config (no stemming: 'solar' is absent).
    assert M._document_hits_for_query(uid, "solar panel photovoltaic") == []
    # Junk-only / function-word-only / blank / anonymous / None-shaped.
    assert M._document_hits_for_query(uid, "the of and how are at") == []
    assert M._document_hits_for_query(uid, "?!@#") == []
    assert M._document_hits_for_query(uid, "") == []
    assert M._document_hits_for_query(uid, "   ") == []
    assert M._document_hits_for_query("anonymous", "wind") == []
    assert M._document_hits_for_query("", "wind") == []
    # Hostile tsquery metacharacters degrade to content tokens — never raise, never
    # damage the table (still queryable afterwards).
    assert M._document_hits_for_query(uid, "wind'); DROP TABLE documents; --") is not None
    assert M._document_hits_for_query(uid, "wind & efficiency | !turbine") is not None
    assert M._document_hits_for_query(uid, "zzzunmatchablezzz") == []
    # Unprovisioned tenant (valid uuid shape, no schema) → [].
    assert M._document_hits_for_query(str(uuid.uuid4()), "wind") == []
    # Pre-299 schema (stub_tsv stripped) → [] via UndefinedColumn soft-fail.
    uid_pre = str(uuid.uuid4())
    schema_pre, st = create_user_schema(uid_pre, uid_pre.replace("-", "_"))
    assert st == "ready"
    conn = psycopg2.connect(r6_db["dsn"])
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"ALTER TABLE {schema_pre}.documents DROP COLUMN stub_tsv")
    finally:
        conn.close()
    assert M._document_hits_for_query(uid_pre, "wind") == []


def test_pin_d_foreign_tenant_sees_nothing(r6_db):
    """PIN (d): tenant isolation — A's doc is invisible to B and vice versa.
    ('simple' config = exact tokens, so the probes use tokens each side really
    carries; B has nothing wind-shaped, so the wind question HONESTLY empties.)"""
    import src.api.main as M
    uid_a, doc_a = r6_db["seed"]("Wind Energy Handbook", WIND_CHUNKS)
    uid_b, doc_b = r6_db["seed"]("Aircraft Maintenance Manual", [
        "Turbine engines require borescope inspections every 800 flight hours.",
        "Hydraulic fluid efficiency checks are part of the line maintenance card.",
    ])
    # The wind question as B → EMPTY (B holds no wind content; A's handbook is
    # invisible across the schema boundary).
    assert M._document_hits_for_query(uid_b, "how efficient are wind turbines") == []
    # A query B CAN answer → only B's doc, never A's.
    hits_b = M._document_hits_for_query(uid_b, "turbine maintenance")
    assert hits_b and all(h["document_id"] == doc_b for h in hits_b), hits_b
    # The same query as A → only A's doc (chunk 2 is the turbine-maintenance chunk),
    # never B's aircraft manual. NOTE: document ids are per-schema sequences (both
    # tenants' docs are id 1 here) — cross-tenant isolation is pinned by the SCOPED
    # result set: every hit is A's own document_id AND carries A's title/text only.
    hits_a = M._document_hits_for_query(uid_a, "turbine maintenance")
    assert hits_a and all(h["document_id"] == doc_a for h in hits_a), hits_a
    assert all(h["title"] == "Wind Energy Handbook" for h in hits_a), hits_a
    assert all("Aircraft" not in (h["title"] or "") for h in hits_a)


def test_title_only_match_keeps_discovery_fallback(r6_db):
    """Non-regression: a TITLE-only match (W2's discovery pin) still surfaces the doc —
    as a fallback row ranked below every passage, with NULL passage columns."""
    import src.api.main as M
    uid, doc_id = r6_db["seed"]("Zephyr Quarterly Review", [
        "Market analysis of semiconductor supply chains for the last quarter.",
        "Editorial notes on logistics and shipping container shortages.",
    ])
    hits = M._document_hits_for_query(uid, "Zephyr Quarterly")
    assert len(hits) == 1, hits
    h = hits[0]
    assert h["document_id"] == doc_id
    assert h["chunk_text"] is None and h["chunk_index"] is None and h["rank"] is None
    # And the renderer speaks the W2 discovery line for it (pin-e complement).
    lines = _render_document_hit_lines(hits)
    assert lines == ["You have a document: Zephyr Quarterly Review "
                     "(document id %d; import pending — verbatim text only, "
                     "facts still extracting)." % doc_id]
