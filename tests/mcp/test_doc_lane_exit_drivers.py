"""Exit-coverage drivers for the doc-lane tools (tests/mcp/ is the tree
tests/mcp/test_exit_coverage.py's coverage child scans — a driver outside it is
invisible to the inventory)."""

import pytest

import src.mcp.server as mcp_server


# ── exit-coverage driver: document_status_tool::ok must be EXECUTED, not just
#    reachable (tests/mcp/test_exit_coverage.py demands a driver for every status
#    exit — an unexercised exit is where a reworded success hides).


class _StatusResp:
    status_code = 200

    def json(self):
        return {"documents": [{"id": 3, "status": "partial", "retriable": True,
                               "failed_chunks": [{"chunk": 1,
                                                  "reason": "empty_llm_response"}]}]}


class _StatusCli:
    async def get(self, *a, **k):
        return _StatusResp()


async def test_document_status_ok_exit_is_executed(monkeypatch):
    cli = _StatusCli()
    monkeypatch.setattr(mcp_server, "_http_client", cli)
    out = await mcp_server.document_status_tool(
        user_id="00000000-0000-4000-8000-0000000000bb")
    assert out["status"] == "ok"
    assert out["documents"][0]["failed_chunks"][0]["reason"] == "empty_llm_response"
