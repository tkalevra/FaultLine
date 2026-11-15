"""Cortex pair — OpenAPI-door parity pins (lane/cortex-product).

The cortex pair lives on TWO doors (see CLAUDE.md "Integration Architecture"):
  * MCP-native tools/list — schemas from src/mcp/tools.py::TOOLS
  * OpenAPI REST shorthand — hand-built spec from src/mcp/http_server.py::_TOOL_OPS +
    the Pydantic request models + the rest_* handler forwarding.

Descriptions are already parity-pinned (tests/mcp/test_premise_and_tool_parity.py —
test_openapi_serves_the_canonical_tool_descriptions_not_a_stripped_copy). What that file does
NOT pin is the PARAMETER surface: before this lane, the OpenAPI door advertised a TRIMMED prop
set for cortex_remember — client_source/evidence accepted-but-unadvertised, and the schema-uplift
provenance trio (model/type/project, migration 301) neither accepted nor advertised. An
OpenWebUI-connected model (the OpenAPI door is the door OWUI builds function specs from) could
not self-tag its model identity AT ALL — the uplift only worked for MCP-native clients.

These pins keep the two doors offering the same affordance, and keep the REST handler actually
forwarding what the door advertises (an advertised-but-dropped field is the same bug wearing a
costume).
"""
import pytest

import src.mcp.http_server as hs
import src.mcp.server as mcp_server
from src.mcp.tools import TOOLS

_CORTEX_REMEMBER_UPLIFT_PROPS = ("client_source", "evidence", "model", "type", "project")


def _openapi_props(op_name: str) -> dict:
    spec = hs.app.openapi()
    op = spec["paths"][f"/{op_name}"]["post"]
    schema = (op.get("requestBody", {}).get("content", {})
              .get("application/json", {}).get("schema", {}) or {})
    return schema.get("properties", {}) or {}


def test_openapi_door_advertises_the_uplift_provenance_props():
    """model / type / project (+ client_source, evidence) must be advertised on the OpenAPI
    door for cortex_remember — door parity with the MCP-native schema in tools.py."""
    props = _openapi_props("cortex_remember")
    native = {t["name"]: t for t in TOOLS}["cortex_remember"]["inputSchema"]["properties"]
    missing = [p for p in _CORTEX_REMEMBER_UPLIFT_PROPS if p not in props]
    assert not missing, f"OpenAPI door drops props the MCP-native door advertises: {missing}"
    for p in _CORTEX_REMEMBER_UPLIFT_PROPS:
        assert p in native, f"MCP-native door lost prop {p} — the parity source flipped"


def test_openapi_door_advertises_client_source_on_recall():
    """cortex_recall: client_source self-scopes to the caller's notes ('mine' lane). The REST
    handler derives scope from its presence; a model must be able to SEE the knob to use it."""
    props = _openapi_props("cortex_recall")
    assert "client_source" in props, "OpenAPI door drops client_source (the mine-lane knob)"


@pytest.mark.asyncio
async def test_rest_cortex_remember_forwards_uplift_provenance(monkeypatch):
    """The handler must FORWARD what the door advertises — model/type/project (and the
    accepted-but-only-now-advertised evidence/client_source) ride the cortex_remember_tool
    call; the auto-stamp CORTEX_AGENT_MODEL / CORTEX_PROJECT defaults are applied INSIDE the
    tool, so None here is correct only when the caller omitted it.
    (Monkeypatching the module attribute works because http_server reaches the tool via
    `_mcp.<attr>` module access, not a from-import — the green run pins that seam holds.)"""
    captured: dict = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return {"status": "noted"}

    async def _gate_ok(_uid: str):
        return True

    monkeypatch.setattr(mcp_server, "cortex_remember_tool", _capture)
    monkeypatch.setattr(mcp_server, "_ensure_provisioned", _gate_ok)
    monkeypatch.setattr(hs, "_resolve_rest_user_id", lambda *a, **k: "11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(hs, "_saas_serve_block", lambda *a, **k: None)

    body = hs.CortexRememberRequest(
        text="When the OpenAPI door advertises a param, the handler must forward it.",
        model="glm-5.3", type="code", project="faultline",
        client_source="opencode",
        evidence={"sha": "5b17107e", "command": "fltest"},
    )
    resp = await hs.rest_cortex_remember(request=object(), body=body, _principal=None)
    assert resp is not None
    assert captured["model"] == "glm-5.3", "model dropped on the REST door"
    assert captured["type"] == "code", "type dropped on the REST door"
    assert captured["project"] == "faultline", "project dropped on the REST door"
    assert captured["client_source"] == "opencode", "client_source dropped on the REST door"
    assert captured["evidence"] == {"sha": "5b17107e", "command": "fltest"}, \
        "evidence dropped on the REST door"


@pytest.mark.asyncio
async def test_rest_cortex_recall_forwards_client_source_as_mine_scope(monkeypatch):
    """The newly advertised client_source on /cortex_recall must reach the tool AND drive the
    mine-lane (scope='mine') — an advertised-but-dropped or advertised-but-inert knob is the
    same bug wearing a costume. This forwarding pre-dates the advertisement; the pin keeps it
    true now that models can see the knob."""
    captured: dict = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return {"memory": "I have no operational notes on that yet."}

    async def _gate_ok(_uid: str):
        return True

    monkeypatch.setattr(mcp_server, "cortex_recall_tool", _capture)
    monkeypatch.setattr(mcp_server, "_ensure_provisioned", _gate_ok)
    monkeypatch.setattr(hs, "_resolve_rest_user_id", lambda *a, **k: "11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(hs, "_saas_serve_block", lambda *a, **k: None)

    body = hs.CortexRecallRequest(query="fltest dsn", client_source="opencode")
    await hs.rest_cortex_recall(request=object(), body=body, _principal=None)
    assert captured.get("client_source") == "opencode", "client_source dropped on the REST recall door"
    assert captured.get("scope") == "mine", "client_source must self-scope the recall to mine"

    captured.clear()
    body = hs.CortexRecallRequest(query="fltest dsn")
    await hs.rest_cortex_recall(request=object(), body=body, _principal=None)
    assert captured.get("scope") == "all", "no client_source must keep today's all-scope recall"


@pytest.mark.asyncio
async def test_rest_cortex_remember_auto_stamps_client_source(monkeypatch):
    """The advertised auto-stamp ("Auto-stamped from the connection when omitted") is REAL on
    this door — pinned end-to-end through the REST handler with the REAL tool impl: the edge's
    captured client identity (mcp-name header / User-Agent on the HTTP door) rides the
    /cortex/note payload when the caller omits client_source, and an explicit value wins.
    (Advertised behavior must be pinned behavior — copy that lies is the exact drift class
    this repo has paid for twice.)"""
    from unittest.mock import AsyncMock, MagicMock

    captured: dict = {}

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"written": True})
    mock_http = MagicMock()
    mock_http.post = AsyncMock(return_value=mock_response)

    async def _gate_ok(_uid: str):
        return True

    async def _no_experience(_uid, _text, result):
        return result

    monkeypatch.setattr(mcp_server, "_http_client", mock_http)
    monkeypatch.setattr(mcp_server, "_client_class", MagicMock(**{
        "current_client_name": MagicMock(return_value="opencode-rest")}))
    monkeypatch.setattr(mcp_server, "_ensure_provisioned", _gate_ok)
    monkeypatch.setattr(mcp_server, "_with_experience", _no_experience)
    monkeypatch.setattr(hs, "_resolve_rest_user_id", lambda *a, **k: "11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(hs, "_saas_serve_block", lambda *a, **k: None)

    await hs.rest_cortex_remember(
        request=object(),
        body=hs.CortexRememberRequest(text="The connection identity stamps what the caller omitted."),
        _principal=None)
    _, kwargs = mock_http.post.call_args
    assert kwargs["json"].get("client_source") == "opencode-rest", \
        "the advertised auto-stamp does not fire on the REST door"

    mock_http.post.reset_mock()
    await hs.rest_cortex_remember(
        request=object(),
        body=hs.CortexRememberRequest(text="Explicit identity always beats the auto-stamp.",
                                      client_source="explicit-wins"),
        _principal=None)
    _, kwargs = mock_http.post.call_args
    assert kwargs["json"].get("client_source") == "explicit-wins"


def test_uplift_prop_descriptions_do_not_reintroduce_the_verbatim_mandate():
    """The door-parity text is new model-facing copy — it must obey the same cortex-pathway
    rule the original descriptions were fixed for (own-words lane, never 'verbatim')."""
    for op in ("cortex_remember", "cortex_recall"):
        blob = str(_openapi_props(op)).lower()
        assert "verbatim" not in blob, f"{op} OpenAPI prop copy carries the verbatim mandate"
