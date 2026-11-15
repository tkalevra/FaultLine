"""SERVER-SIDE ATTACHMENT BRIDGE (lane/attachment-bridge, 2026-10-22).

The EPA wind-turbines wound: OWUI never gives the model the bytes, so "remember this
doc" cost a 59K-char think + a 16K-char hand-stitched ingest_document argument. The fix:
an OWUI filter POSTs each attachment AT ATTACH TIME to the server door
``POST /ingest/attachment``, which delegates to the SAME ingest_file pipeline. The
model's job shrinks to a status check — never re-transmission.

Pins (gauntlet brief, DEV/RESEARCH/epa-ingest-validation-20261022.md rec #1):

  (1) BRIDGE AUTH — missing bearer → 401; wrong bearer → 401; correct TOOL-SERVER
      bearer (MCP_API_KEY — middleware-trusted server-to-server auth, DISTINCT from
      seat flu_ tokens) + X-OpenWebUI-User-Id → the seat identity is folded in and the
      ingest runs.
  (2) SAME-PATH DELEGATION — the bridge drives the REAL ingest_file_tool →
      ingest_file_core (parser seam mocked, backend HTTP mocked): artefact retention +
      stub facts + parser stamp all present in the receipt, and the bridge envelope
      (document_id + status_tool) rides ON TOP of the unchanged ingest_file shape.
  (3) OVERSIZED ATTACHMENT → the loud tool-door cap (invalid_request envelope naming
      the cap), and the NEXT ingest is unaffected.
  (4) RATE LIMIT — per-seat sliding window: over-budget seat → 429 + Retry-After;
      another seat unaffected; unexpected handler exception → fail-safe tool envelope,
      never a raw 500.
"""

import base64
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import src.mcp.server as _mcp
from src.mcp.http_server import app

TEST_USER_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
TEST_USER_2 = "3f2b6a91-0c44-4a7e-8b21-9d5e1f6a8b02"   # second well-formed seat

# A valid PNG signature is unnecessary — the PDF signature is what resolve_media_type
# sniffs, and the parser seam below is faked, so the bytes only need the magic prefix.
_PDF_BYTES = b"%PDF-1.4\n%fake-attachment-bytes-for-transport\n"


def _resp(json_obj, status=200):
    r = MagicMock()
    r.status_code = status
    r.json = lambda: json_obj
    r.raise_for_status = lambda: None
    return r


@pytest.fixture(autouse=True)
def reset_bridge_state():
    """No cross-test leakage: provisioning cache, rate buckets, cap constant, http client."""
    original_provisioned = _mcp._provisioned_users.copy()
    original_client = _mcp._http_client
    original_detected = _mcp._FAULTLINE_URL_DETECTED
    original_rate_buckets = dict(_mcp._attachment_rate_hits)
    original_limit = _mcp._INGEST_ATTACHMENT_RATE_LIMIT
    original_cap = _mcp._INGEST_FILE_B64_MAX_BYTES
    _mcp._FAULTLINE_URL_DETECTED = True   # no URL probing from unit tests
    _mcp._attachment_rate_hits.clear()
    _mcp._provisioned_users |= {TEST_USER_ID, TEST_USER_2}
    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=Exception("no real provisioning in tests"))
    mock_client.aclose = AsyncMock()      # lifespan shutdown awaits this
    _mcp._http_client = mock_client
    yield
    _mcp._provisioned_users = original_provisioned
    _mcp._http_client = original_client
    _mcp._FAULTLINE_URL_DETECTED = original_detected
    _mcp._attachment_rate_hits.clear()
    _mcp._attachment_rate_hits.update(original_rate_buckets)
    _mcp._INGEST_ATTACHMENT_RATE_LIMIT = original_limit
    _mcp._INGEST_FILE_B64_MAX_BYTES = original_cap


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _fake_pdf_extract(monkeypatch, text="# Introduction\nColonial soap making process.\n\n"
                                         "# Methods\nThe boiling process uses lye and heat."):
    """Fake the NAIVE parser half (the R5 seam pattern from test_parser_seam_r5)."""
    from src.ingest.binary_intake import ExtractedDocument, ExtractedPage
    import src.ingest.binary_intake as bi

    def _fake_extract_pdf(data, max_pages=None):
        return ExtractedDocument(
            media_type="application/pdf",
            pages=(ExtractedPage(page_index=0, text=text,
                                 char_count=len(text), image_count=0),),
        )

    monkeypatch.setattr(bi, "extract_pdf", _fake_extract_pdf)


def _fake_backend():
    """Mock the backend HTTP seam; returns the captured (url, body) calls.

    Called IN the test body (not a fixture): the transport lifespan assigns its own
    client at TestClient startup, so the mock must be (re)installed after that."""
    calls = []

    async def fake_post(url, json=None, timeout=None, headers=None):
        calls.append((url, json))
        if url.endswith("/documents/enqueue"):
            return _resp({"status": "pending", "document_id": 21,
                          "chunk_count": json["chunk_count"], "eta_seconds": 410})
        if url.endswith("/artefacts/retain"):
            return _resp({"status": "retained", "artefact_id": 18, "duplicate": False,
                          "sha256": "f" * 64, "media_type": json["media_type"]})
        if url.endswith("/ingest"):
            return _resp({"committed": 0, "staged": len(json["edges"])})
        raise AssertionError(f"unexpected backend call: {url}")

    _mcp._http_client = MagicMock(post=AsyncMock(side_effect=fake_post),
                                  get=AsyncMock(side_effect=Exception("no GET in tests")),
                                  aclose=AsyncMock())
    return calls


def _envelope(resp):
    """The REST tool door returns the tool result dict itself (isError-stamped)."""
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── pin (1): BRIDGE AUTH ──────────────────────────────────────────────────────────────────

def test_bridge_missing_bearer_401(client):
    with patch("src.mcp.http_server.MCP_API_KEY", "tool-server-secret"):
        resp = client.post("/ingest/attachment",
                           json={"filename": "a.pdf", "data_b64": "AAAA"})
    assert resp.status_code == 401
    # No challenge header (port of foss 595d084c) — flip pinned 2026-11-04.
    assert "WWW-Authenticate" not in resp.headers


def test_bridge_wrong_bearer_401(client):
    with patch("src.mcp.http_server.MCP_API_KEY", "tool-server-secret"):
        resp = client.post("/ingest/attachment",
                           json={"filename": "a.pdf", "data_b64": "AAAA"},
                           headers={"Authorization": "Bearer not-the-tool-server-key"})
    assert resp.status_code == 401


def test_bridge_correct_toolserver_bearer_ingests_with_folded_seat(client, monkeypatch):
    captured = {}

    async def fake_bridge(**kwargs):
        captured.update(kwargs)
        return {"status": "pending", "bridge": {"document_id": 21,
                                                "status_tool": "document_status"}}

    monkeypatch.setattr(_mcp, "ingest_attachment_bridge", AsyncMock(side_effect=fake_bridge))
    with patch("src.mcp.http_server.MCP_API_KEY", "tool-server-secret"):
        resp = client.post(
            "/ingest/attachment",
            json={"filename": "EPA_Wind_Turbines.pdf", "data_b64": "AAAA",
                  "source_ref": "/uploads/EPA_Wind_Turbines.pdf"},
            headers={"Authorization": "Bearer tool-server-secret",
                     "X-OpenWebUI-User-Id": TEST_USER_ID},
        )
    body = _envelope(resp)
    # The seat identity came from the HEADER (the OWUI platform names the seat it acts
    # for), folded into the delegate call — not from an untrusted body claim.
    assert captured["user_id"] == TEST_USER_ID
    assert captured["filename"] == "EPA_Wind_Turbines.pdf"
    assert body["status"] == "pending"
    assert body["bridge"]["document_id"] == 21
    assert body["bridge"]["status_tool"] == "document_status"


def test_bridge_404_when_binary_intake_disabled(client, monkeypatch):
    import src.ingest.binary_intake as bi
    monkeypatch.setattr(bi, "binary_intake_enabled", lambda: False)
    with patch("src.mcp.http_server.MCP_API_KEY", "tool-server-secret"):
        resp = client.post("/ingest/attachment",
                           json={"filename": "a.pdf", "data_b64": "AAAA"},
                           headers={"Authorization": "Bearer tool-server-secret"})
    assert resp.status_code == 404


# ── pin (2): SAME-PATH DELEGATION — real ingest_file pipeline, receipt intact ────────────

async def test_bridge_drives_real_ingest_file_pipeline(monkeypatch):
    """The bridge must NOT fork the pipeline: real ingest_file_tool → ingest_file_core,
    parser seam + backend HTTP mocked. Artefact + stub facts + parser stamp all present,
    and the bridge envelope is purely ADDITIVE."""
    _fake_pdf_extract(monkeypatch)
    calls = _fake_backend()

    result = await _mcp.ingest_attachment_bridge(
        user_id=TEST_USER_ID, filename="EPA_Wind_Turbines.pdf",
        data_b64=base64.b64encode(_PDF_BYTES).decode(),
        source_ref="/uploads/EPA_Wind_Turbines.pdf")

    # (a) The SAME document lane ran: pending receipt with document id + eta.
    assert result["status"] == "pending"
    assert result["document"]["document_id"] == 21
    assert result["document"]["eta_seconds"] == 410
    # (b) ARTEFACT RETENTION happened on the same path (original bytes retained).
    assert result["artefact"]["status"] == "retained"
    assert result["artefact"]["artefact_id"] == 18
    # (c) STUB FACTS minted pre-drain (the enqueue-window /ingest stub write: document
    #     entity + filename alias + the two ATX headings).
    assert result["document"].get("stub_facts_minted", 0) >= 4
    # (d) PARSER STAMP rode the enqueue (naive_v1 — DOC_PARSER unset).
    enqueue_bodies = [b for u, b in calls if u.endswith("/documents/enqueue")]
    assert enqueue_bodies and enqueue_bodies[0].get("parser_version") == "naive_v1"
    stub_bodies = [b for u, b in calls
                   if u.endswith("/ingest") and b.get("source") == "document"]
    assert stub_bodies, "the stub-mint /ingest write never fired"
    # (e) The bridge envelope rides ON TOP — document id + status pointer for the model.
    assert result["bridge"]["ingested_by"] == "attachment_bridge"
    assert result["bridge"]["document_id"] == 21
    assert result["bridge"]["status_tool"] == "document_status"
    assert "document_status" in result["bridge"]["message"]
    assert "never re-transmit" in result["bridge"]["message"]


async def test_bridge_keeps_ingest_file_response_shape_on_refusals(monkeypatch):
    """Unsupported media / invalid b64 keep the ingest_file refusal shape + envelope."""
    # Not valid base64 → invalid_request from the SAME door logic.
    bad = await _mcp.ingest_attachment_bridge(
        user_id=TEST_USER_ID, filename="x.pdf", data_b64="!!!not-base64!!!")
    assert bad["status"] == "invalid_request" and bad.get("isError") is True
    # Valid base64, unrecognised signature → rejected (unsupported media type).
    junk = base64.b64encode(b"not-a-real-file-signature-at-all").decode()
    refused = await _mcp.ingest_attachment_bridge(
        user_id=TEST_USER_ID, filename="x.bin", data_b64=junk)
    assert refused["status"] == "rejected"


# ── pin (3): OVERSIZED ATTACHMENT → loud cap, ingest unaffected ──────────────────────────

def test_bridge_oversized_loud_cap_then_ingest_unaffected(client, monkeypatch):
    _fake_pdf_extract(monkeypatch)
    _fake_backend()
    # Shrink the tool cap BETWEEN the two payloads: big (205 B) is over, small (46 B) is under.
    monkeypatch.setattr(_mcp, "_INGEST_FILE_B64_MAX_BYTES", 128)

    with patch("src.mcp.http_server.MCP_API_KEY", "tool-server-secret"):
        # Oversized → LOUD cap behavior: tool envelope, status invalid_request, cap named.
        resp_big = client.post(
            "/ingest/attachment",
            json={"filename": "big.pdf",
                  "data_b64": base64.b64encode(b"%PDF-" + b"x" * 200).decode()},
            headers={"Authorization": "Bearer tool-server-secret",
                     "X-OpenWebUI-User-Id": TEST_USER_ID})
        big = _envelope(resp_big)
        assert big["status"] == "invalid_request"
        assert "caps at 128" in big["message"]
        assert "/ingest_file" in big["message"]

        # The NEXT attachment on the same door ingests normally — the cap did not wedge
        # the bridge or the pipeline behind it.
        resp_ok = client.post(
            "/ingest/attachment",
            json={"filename": "small.pdf",
                  "data_b64": base64.b64encode(_PDF_BYTES).decode()},
            headers={"Authorization": "Bearer tool-server-secret",
                     "X-OpenWebUI-User-Id": TEST_USER_ID})
        ok = _envelope(resp_ok)
        assert ok["status"] == "pending"
        assert ok["document"]["document_id"] == 21
        assert ok["bridge"]["document_id"] == 21


# ── pin (4): RATE LIMIT + FAIL-SAFE SHAPES ────────────────────────────────────────────────

def test_bridge_rate_limit_per_seat(client, monkeypatch):
    monkeypatch.setattr(_mcp, "_INGEST_ATTACHMENT_RATE_LIMIT", 2)
    monkeypatch.setattr(_mcp, "ingest_attachment_bridge",
                        AsyncMock(return_value={"status": "pending", "bridge": {}}))
    hdrs = {"Authorization": "Bearer tool-server-secret"}
    with patch("src.mcp.http_server.MCP_API_KEY", "tool-server-secret"):
        for _ in range(2):
            r = client.post("/ingest/attachment", json={"data_b64": "AAAA"},
                            headers={**hdrs, "X-OpenWebUI-User-Id": TEST_USER_ID})
            assert r.status_code == 200
        r3 = client.post("/ingest/attachment", json={"data_b64": "AAAA"},
                         headers={**hdrs, "X-OpenWebUI-User-Id": TEST_USER_ID})
        assert r3.status_code == 429
        assert "Retry-After" in r3.headers
        # ANOTHER seat is unaffected — the window is per-seat, not global.
        r_other = client.post("/ingest/attachment", json={"data_b64": "AAAA"},
                              headers={**hdrs, "X-OpenWebUI-User-Id": TEST_USER_2})
        assert r_other.status_code == 200


def test_bridge_unexpected_exception_is_failsafe_envelope_not_500(client, monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("simulated pipeline crash")

    monkeypatch.setattr(_mcp, "ingest_attachment_bridge", AsyncMock(side_effect=boom))
    with patch("src.mcp.http_server.MCP_API_KEY", "tool-server-secret"):
        resp = client.post("/ingest/attachment", json={"data_b64": "AAAA"},
                           headers={"Authorization": "Bearer tool-server-secret",
                                    "X-OpenWebUI-User-Id": TEST_USER_ID})
    body = _envelope(resp)   # _envelope asserts 200 — a raw 500 fails here
    assert body["status"] == "error"
    assert body.get("isError") is True
    assert "simulated pipeline crash" not in body["message"]   # no raw exception text
