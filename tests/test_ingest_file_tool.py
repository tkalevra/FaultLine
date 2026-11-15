"""THE BINARY TOOL DOOR — ingest_file (base64) reaches the shared core.

Verification 2026-08-21 found the binary lane fully wired on the WRITE half but
model-DISABLED: absent from TOOLS/_TOOL_OPS/TOOL_DISPATCH, so no connected model could
hand FaultLine a file, and every ARTEFACT_* flag defaulted OFF (dark in every
deployment). The owner ruling ("Images must be imported into the DB on best effort")
made it live product surface: this file pins the model-facing door and the flag
rollout. The tool-surface pin (tools count 11 incl. ingest_file, in
tests/mcp/test_server.py) is RED at parent c0eb3633 — the missing door IS the defect.
"""

from __future__ import annotations

import base64

import pytest

import src.mcp.server as mcp_server


def test_tool_dispatch_routes_ingest_file():
    """The JSON-RPC door routes tools/call ingest_file → the tool handler."""
    assert mcp_server.TOOL_DISPATCH.get("ingest_file") is mcp_server.ingest_file_tool


# A minimal 1x1 transparent PNG — real magic bytes, no text layer anywhere.
_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhf"
    "DwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


class _RetainResp:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"status": "retained", "artefact_id": 41, "placements": 0,
                "captions_bound": 0}


class _IngestResp:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"status": "valid", "committed": 2, "staged": 0}


class _Cli:
    """Captures BOTH backend posts: /artefacts/retain AND the upload-event /ingest.

    (headers kwarg: the /ingest seam passes the lane header — a stub without it TypeErrors
    and the mint reads as 'failed', the same fixture family as the lineage reds.)"""

    def __init__(self):
        self.posts = []

    async def post(self, url, json=None, timeout=None, headers=None):
        self.posts.append((url, json))
        if url.endswith("/artefacts/retain"):
            return _RetainResp()
        return _IngestResp()


async def test_the_file_itself_is_surfaced_on_the_walk(monkeypatch):
    """THE OWNER BAR (2026-08-21): "what use is a document that cannot be surfaced on
    query?" Retention alone is not enough — the upload event (owns + instance_of, the
    file's NAME at its type node) must land on the graph as DOCUMENT-TIER facts
    (source='document' → staged B), so 'what files have I given you?' and 'tell me about
    photo.png' are walkable. RED at parent: the door never minted anything."""
    cli = _Cli()
    monkeypatch.setattr(mcp_server, "_http_client", cli)
    out = await mcp_server.ingest_file_tool(
        data_b64=base64.b64encode(_PNG_1PX).decode("ascii"),
        user_id="00000000-0000-4000-8000-0000000000bb",
        filename="photo.png")
    assert out["status"] == "retained_no_text", out
    mints = [j for (u, j) in cli.posts if u.endswith("/ingest")]
    assert mints, "the upload-event edges were never minted — retained but NOT surfaced"
    body = mints[0]
    assert body["source"] == "document", (
        f"upload-event edges must mint under the document tier (staged B) — saw "
        f"{body['source']!r}"
    )
    rels = {(e["subject"], e["rel_type"], e["object"]) for e in body["edges"]}
    assert ("user", "owns", "photo.png") in rels, rels
    assert any(r == "instance_of" and o == "png" for (_, r, o) in rels), (
        f"the file must be filed AT its media-type node — saw {rels}"
    )
    # L4 GROUNDING, SUBJECT-AGNOSTIC (owner insistence 2026-08-21): the type node's claim
    # is Concept (the seeded instance_of tail constraint — the ordinary hierarchy
    # enrichment/taxonomy/±6 ladder owns the rest), and the subtype token comes from the
    # sniffer's media type, never a code list.
    _io = [e for e in body["edges"] if e["rel_type"] == "instance_of"]
    assert _io and _io[0]["object_type"] == "Concept", _io
    assert _io[0]["object"] == "png", "subtype derives from the sniffed media type"


async def test_bound_captions_surface_and_unbound_do_not(monkeypatch):
    """Only geometry-BOUND captions become facts (the geometry module is decline-biased;
    importing a decline would fabricate). The caption text is the STRING VALUE on a
    growth-ready rel surface minted under the document tier."""
    cli = _Cli()

    async def _post(url, json=None, timeout=None, headers=None):
        cli.posts.append((url, json))
        if url.endswith("/artefacts/retain"):
            return _RetainResp()
        return _IngestResp()

    class _C:
        post = staticmethod(_post)

    monkeypatch.setattr(mcp_server, "_http_client", _C)
    # A placement with a bound caption and one declined — fabricate via the seam the
    # core reads (monkeypatch _artefact_placements to control placements deterministically).
    monkeypatch.setattr(mcp_server, "_artefact_placements",
                        lambda extracted, chunks, doc_result: [
                            {"caption_text": "Figure 3: the rack layout",
                             "caption_confidence": 0.9},
                            {"caption_text": None, "caption_declined_reason": "ambiguous"},
                        ])
    out = await mcp_server.ingest_file_tool(
        data_b64=base64.b64encode(_PNG_1PX).decode("ascii"),
        user_id="00000000-0000-4000-8000-0000000000bb",
        filename="pic.png")
    assert out["status"] == "retained_no_text"
    mints = [j for (u, j) in cli.posts if u.endswith("/ingest")]
    assert mints and mints[0]["source"] == "document"
    captions = [e for e in mints[0]["edges"] if e["rel_type"] == "has_caption"]
    assert len(captions) == 1 and captions[0]["object"] == "Figure 3: the rack layout"


async def test_no_mint_over_failed_retention(monkeypatch):
    """The artefact_id gate (round-4 critic mutation 5): a FAILED byte retention must
    mint NOTHING — 'user owns photo.png' over bytes that were NOT kept would be a graph
    lie about a file we do not have."""
    cli = _Cli()

    class _RetainFailed:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "soft_error", "artefact_id": None,
                    "reason": "backend down"}

    async def _post(url, json=None, timeout=None, headers=None):
        cli.posts.append((url, json))
        if url.endswith("/artefacts/retain"):
            return _RetainFailed()
        return _IngestResp()

    class _C:
        post = staticmethod(_post)

    monkeypatch.setattr(mcp_server, "_http_client", _C)
    out = await mcp_server.ingest_file_tool(
        data_b64=base64.b64encode(_PNG_1PX).decode("ascii"),
        user_id="00000000-0000-4000-8000-0000000000bb",
        filename="ghost.png")
    assert out["status"] == "retained_no_text" and out.get("isError") is True
    mints = [j for (u, j) in cli.posts if u.endswith("/ingest")]
    assert not mints, (
        f"upload-event edges minted over a FAILED retention — the graph now claims a "
        f"file that was not kept: {mints}"
    )


async def test_failed_mint_is_loud_but_never_loses_the_upload(monkeypatch):
    """Best-effort surfacing: if the /ingest mint fails entirely, the upload is still
    RETAINED (status/report unchanged) and the failure is LOUD (crit)."""
    cli = _Cli()

    async def _post(url, json=None, timeout=None, headers=None):
        cli.posts.append((url, json))
        if url.endswith("/artefacts/retain"):
            return _RetainResp()
        import httpx as _hx
        raise _hx.ConnectError("backend gone")

    class _C:
        post = staticmethod(_post)

    monkeypatch.setattr(mcp_server, "_http_client", _C)
    out = await mcp_server.ingest_file_tool(
        data_b64=base64.b64encode(_PNG_1PX).decode("ascii"),
        user_id="00000000-0000-4000-8000-0000000000bb",
        filename="photo.png")
    assert out["status"] == "retained_no_text"
    assert out["artefact"]["artefact_id"] == 41, "the bytes stay retained"


async def test_an_image_is_retained_and_reported_honestly(monkeypatch):
    """Best effort, honestly reported: a text-free IMAGE is RETAINED (bytes in the
    per-tenant artefacts table) with status retained_no_text — never a fake success,
    never a drop."""
    monkeypatch.setattr(mcp_server, "_http_client", _Cli())
    out = await mcp_server.ingest_file_tool(
        data_b64=base64.b64encode(_PNG_1PX).decode("ascii"),
        user_id="00000000-0000-4000-8000-0000000000bb",
        filename="photo.png")
    assert out["status"] == "retained_no_text", out
    assert out["media_type"] == "image/png"
    assert out["artefact"]["artefact_id"] == 41, "the bytes were retained"
    assert "no readable text" in out["message"]


async def test_garbage_base64_is_an_honest_invalid_request(monkeypatch):
    out = await mcp_server.ingest_file_tool(
        data_b64="!!! not base64 !!!",
        user_id="00000000-0000-4000-8000-0000000000bb")
    assert out["status"] == "invalid_request" and out.get("isError") is True


async def test_over_cap_is_refused_with_the_rest_door_named(monkeypatch):
    monkeypatch.setattr(mcp_server, "_INGEST_FILE_B64_MAX_BYTES", 8)
    out = await mcp_server.ingest_file_tool(
        data_b64=base64.b64encode(_PNG_1PX * 4).decode("ascii"),
        user_id="00000000-0000-4000-8000-0000000000bb")
    assert out["status"] == "invalid_request"
    assert "REST /ingest_file" in out["message"]
