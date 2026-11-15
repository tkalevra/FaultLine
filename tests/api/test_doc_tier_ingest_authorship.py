"""DOCUMENT-TIERED AUTHORSHIP — document-extracted facts land staged B, never A.

OWNER RULING (2026-08-21): "document ingest must land in the users memories
(postgresql) as B." A document the user supplied is content-attested — the user chose
to give it to us — but the TRIPLE extracted from it is a machine reading, and by the
owner's own class contract ("A is user stated, B is learned/inferred") it tiers at
staged B: query-visible from staged_facts, promoted on confirmation
(confirmed_count >= 3), NEVER Class A — so one uploaded file can never outrank the
user's own conversational corrections (fact_provenance llm_inferred loses to
user_stated in _decide_supersede_or_coexist).

The mechanism MIRRORS the unattested ruling (owner direction 2026-08-15, pinned in
tests/api/test_unattested_ingest_authorship.py): the ROUTER owns the tiering decision
(overwriting the spine's extraction-time user_stated stamp), the class force lives at
the /ingest call site keyed on req.source — because the llm_inferred arm of
assign_class_and_confidence deliberately returns a defined-A rel at Class A (pinned
for the other lanes; that arm stays untouched) — and the four downstream seams (novel
rel staging, the unknown_staging row literal, the hierarchy C-clock floors) must not
re-demote or re-elevate.

Red at parent c0eb3633: there the doc lane posted source="mcp" (user-attested →
Class A on defined-A rels) — the writer pins and the drain-level behavioural pin below
fail there for exactly that reason.
"""

import os
import re

import pytest

import src.re_embedder.embedder as embedder

_MAIN = os.path.join(os.path.dirname(__file__), "../../src/api/main.py")
_SERVER = os.path.join(os.path.dirname(__file__), "../../src/mcp/server.py")
_EMBEDDER = os.path.join(os.path.dirname(__file__), "../../src/re_embedder/embedder.py")


# ── (1) the re-mine map reproduces the tier ──────────────────────────────────


def test_episodic_origin_map_routes_document_to_the_document_lane():
    """A re-mined document chunk must re-ingest under source="document" — the previous
    "mcp" mapping re-elevated machine-extracted content to user_stated/Class A, the
    exact defect class the unattested down-routes were added to close."""
    assert embedder._EPISODIC_ORIGIN_INGEST_SOURCE["document"] == "document"
    assert embedder._reextract_ingest_source("document") == "document"


# ── (2) the router + class force (source-regex pins — the force lives mid-handler,
#         the same proof shape the unattested ruling uses) ─────────────────────


def test_router_overwrites_document_provenance_to_llm_inferred():
    src = open(_MAIN).read()
    m = re.search(r'elif req\.source == "document":\s*\n\s*#[^\n]*\n(?:\s*#[^\n]*\n)*\s*'
                  r'edge\.fact_provenance = "llm_inferred"', src)
    assert m, ("the provenance router must overwrite source='document' edges to "
               "llm_inferred — without the overwrite the spine's extraction-time "
               "user_stated stamp rides into Class A")


def test_document_class_force_exists_and_never_yields_a():
    src = open(_MAIN).read()
    m = re.search(
        r'if req\.source == "document" and fact_class != "B":', src)
    assert m, ("the document class force must live at the /ingest call site (the "
               "llm_inferred arm alone returns defined-A rels at Class A)")
    m2 = re.search(r'ingest\.document_forced_class_b', src)
    assert m2, "the force must log loudly (document_forced_class_b)"


def test_document_never_lands_the_c_clock_in_downstream_seams():
    """Novel-rel staging + the unknown_staging row literal + both hierarchy floors must
    keep a document-origin row at staged B — never re-demote onto the 30-day clock,
    never re-elevate to A."""
    src = open(_MAIN).read()
    assert re.search(r'elif req\.source in \("unattested", "document"\):', src), \
        "novel-rel unknown_staging must route document to durable B"
    assert re.search(r'"B" if req\.source in \("unattested", "document"\) else "C"', src), \
        "the unknown_staging row literal must stage document at B, not C"
    assert len(re.findall(r'req\.source not in \("unattested", "document"\)', src)) >= 2, \
        "both hierarchy C-clock floors must exclude the document lane"


# ── (3) the writers — every document-lane /ingest poster sends source="document" ──


def test_embedder_doc_lane_posts_document_source():
    """The async registry drain (the main document lane) must post source='document'
    for BOTH speaker partitions and the legacy whole-chunk path — never 'mcp'.
    (The call carries the l1_deterministic_first ledger's ``_meta=`` out-param since
    that lane landed; this pin's subject is the SOURCE STAMP, so the literal is
    matched on the leading args.)"""
    src = open(_EMBEDDER).read()
    assert '_extract_and_ingest(_user_text, "document"' in src
    assert '_extract_and_ingest(_asst_text, "document"' in src
    assert '_extract_and_ingest(chunk, "document"' in src
    assert '_extract_and_ingest(chunk, "mcp"' not in src, (
        "the document lane must never post the conversational (user-attested) source"
    )


def test_mcp_sync_fallback_posts_document_source():
    """The SYNCHRONOUS fallback in ingest_document_tool (used when the registry is
    unavailable) must tier identically: spine route via ingest_source='document',
    rewrite route via the body literal."""
    src = open(_SERVER).read()
    m = re.search(r'_ingest_statement_via_spine\(\s*chunk, user_id, source_ref=ref or None,'
                  r'\s*ingest_source="document"', src)
    assert m, "the spine fallback must pass ingest_source='document'"
    assert re.search(r'"source": "document",\s*\n\s*"source_ref": ref or None', src), \
        "the rewrite fallback must post source='document'"


def test_conversational_lanes_still_post_mcp():
    """ZERO REGRESSION: remember_facts' attested lane keeps source='mcp' — the ruling
    is about the DOCUMENT lane only, not the conversational attestation."""
    src = open(_SERVER).read()
    assert re.search(r'_ing_source = "mcp" if (?:_)?attested', src), \
        "remember_facts' attested conversational lane must stay 'mcp' (Class A)"


# ── (4) the behavioural pin — drive the real drain, capture the /ingest body ────


class _StubResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"committed": 1, "staged": 1}


def test_drain_ingests_document_chunks_under_the_document_source(monkeypatch):
    """THE BEHAVIOURAL PIN (red at parent, which posted 'mcp'): one drained document
    chunk reaches POST /ingest with source='document' — the router-side tiering keys
    on exactly this token."""
    monkeypatch.setattr(embedder, "_backend_is_reachable", lambda _u: True)
    monkeypatch.setattr(embedder, "INGEST_ASSISTANT_TURNS", False)
    seen = []

    def _post(url, json=None, timeout=None, headers=None):  # noqa: A002 - mirrors httpx
        seen.append((url, (json or {}).get("source")))
        return _StubResponse()

    monkeypatch.setattr(embedder.httpx, "post", _post)
    monkeypatch.setattr(embedder, "_document_chunk_edges",
                        lambda *a, **k: [{"subject": "a", "rel_type": "r", "object": "b"}])

    class _Conn:
        statements = []

        def cursor(self):
            return _Cur(self)

        def commit(self):
            return None

        def rollback(self):
            return None

    class _Cur:
        def __init__(self, conn):
            self._conn = conn
            self._last = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            self._conn.statements.append((sql, params))
            self._last = (sql, params)

        def fetchall(self):
            if "SELECT id FROM documents" in (self._last or ("", None))[0]:
                return [(7,)]
            return []

        def fetchone(self):
            if "SET status = 'processing'" in (self._last or ("", None))[0]:
                return (["one chunk"], "ref", 1)
            return None

    embedder.drain_pending_documents(_Conn(), "http://backend", "u", schema_name=None)
    ingest_sources = [s for (u, s) in seen if u.endswith("/ingest")]
    assert ingest_sources, "the drain never reached /ingest — the pin is vacuous"
    assert all(s == "document" for s in ingest_sources), (
        f"document-lane /ingest posts must carry source='document' — saw {ingest_sources}"
    )
