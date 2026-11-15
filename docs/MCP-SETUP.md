# FaultLine MCP — Claude Desktop Setup

## Prerequisites

1. **FaultLine backend running** — via Docker Compose or bare metal. The backend must be reachable from the machine running Claude Desktop.
2. **`FAULTLINE_USER_ID`** — your user UUID as registered in FaultLine's PostgreSQL. To look it up on any instance:
   ```bash
   psql "$POSTGRES_DSN" -c "SELECT user_id, schema_name FROM user_provisioning WHERE provisioned_at IS NOT NULL;"
   ```
3. **`MCP_API_KEY`** — a shared secret required to reach the HTTP transport. Generate one:
   ```bash
   openssl rand -hex 32
   # example output: a3f8c2d1e4b5a6f7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1
   ```
   Set this in your Portainer stack environment variables AND in your Claude Desktop config (or system env). The same key goes in both places.

---

## Two Deployment Models

### Model 1 — Stdio (Claude Desktop, local process)

The MCP server runs as a child process of Claude Desktop on your workstation. It reaches the FaultLine backend over the network. No HTTP transport, no bearer token needed — the process is local and trusted.

**When to use**: Claude Desktop on your local machine, FaultLine on TrueNAS or local Docker.

```json
{
  "mcpServers": {
    "faultline": {
      "command": "python",
      "args": ["/path/to/FaultLine/tools/mcp_server.py"],
      "env": {
        "FAULTLINE_USER_ID": "YOUR-USER-UUID-HERE",
        "FAULTLINE_API_URL": "http://YOUR-TRUENAS-IP:8000"
      }
    }
  }
}
```

### Model 2 — HTTP Transport (Docker sidecar, for OpenWebUI or remote Claude)

The `faultline-mcp` Docker service runs alongside the backend and exposes the MCP server at port 8002. This is what OpenWebUI's native MCP integration connects to, and what Claude Desktop uses when it connects to a remote MCP server over HTTP.

**Bearer token is mandatory for this model** — port 8002 is network-accessible.

#### Step 1 — Set env vars in Portainer stack

In your Portainer stack for `docker-compose-portainer-withoutqdrant.yml`, add two environment variables:

```
FAULTLINE_USER_ID = YOUR-USER-UUID-HERE
MCP_API_KEY       = <your openssl rand -hex 32 output>
```

The `faultline-mcp` service reads both automatically on startup.

#### Step 2a — OpenWebUI integration

In OpenWebUI: **Settings → Integrations → Tools → Add MCP Server** (native MCP) **OR** Settings → Tools → `+` (OpenAPI tool server — the recommended path that reliably forwards the per-user identity header).

| Field | Value |
|-------|-------|
| URL | `http://faultline-mcp:8002/mcp` (native MCP, Docker-internal) or `http://faultline-mcp:8002` (OpenAPI tool server) |
| Bearer Token | `<your MCP_API_KEY value>` |

OpenWebUI discovers the six memory tools — `recall_memory`, `remember_facts`, `ingest_document`, `learn_facts`, `retract_fact`, `forget_fact` — plus the two agent-cortex tools (`cortex_remember`, `cortex_recall`), and surfaces them in every conversation. Tool descriptions OpenWebUI reads come from **`src/mcp/tools.py::TOOLS`** — the single source of truth. `/openapi.json` is a hand-built spec (`_build_tool_openapi`) that resolves each operation's description from `TOOLS`; the `@app.post` decorator text is never served to an OpenAPI client, so editing it changes nothing. Do NOT keep two copies in sync — that is the drift this consolidation removed. (The legacy OpenWebUI Filter exists but is intentionally disabled — the MCP tool server is the supported path.)

> ⚠️ **Do not "correct" the paragraph above back to "`http_server.py` is the source of truth".** A peer review on the pre-consolidation `review/08d0a468-mcp-redirect` line did exactly that (commit `7c15156d`) in good faith — its branch predated the fix. On this line it is backwards, and it is the specific trap that already cost real product quality: three hand-maintained copies of the tool descriptions drifted, the OpenAPI door served a stripped `recall_memory` description with the "call at the START of a turn" trigger deleted, and every OpenWebUI-hosted model read the impoverished copy. `_build_tool_openapi` now resolves descriptions FROM `TOOLS`; `_TOOL_OPS` owns only the transport-specific schema shape. Pinned by `tests/mcp/test_premise_and_tool_parity.py`.

#### Step 2b — Claude Desktop over HTTP (remote MCP server)

If you want Claude Desktop to connect to the Docker sidecar directly rather than running a local stdio process:

```json
{
  "mcpServers": {
    "faultline": {
      "url": "http://YOUR-TRUENAS-IP:8002/mcp",
      "headers": {
        "Authorization": "Bearer YOUR-MCP-API-KEY-HERE"
      }
    }
  }
}
```

Note: Claude Desktop's HTTP MCP support requires a recent version (0.10+). Check the hammer icon appears after restarting.

---

## Auth model

- **Self-hosted (this guide):** the shared `MCP_API_KEY` Bearer is transport auth. The OpenWebUI↔MCP hop is on a trusted segment and OpenWebUI stamps the logged-in user's UUID into `X-OpenWebUI-User-Id`; the MCP transport binds that UUID to `faultline_{user_slug}` via `SET search_path` (no `public`) for every backend call (`src/mcp/server.py:127`).
- **Managed SaaS offering:** the hosted FaultLine offering at https://faultline.ca adds its own account/seat auth model on top of this transport; those details are documented with the SaaS offering, not here.

---

## Config File Location

| OS | Path |
|----|------|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

Create the file if it does not exist. Restart Claude Desktop after saving changes.

---

## System Prompt Recommendation

Paste this into your Claude Desktop system prompt (Settings → Claude → System Prompt):

```
You have access to a personal knowledge graph via FaultLine MCP tools.

At the start of each turn, call recall_memory with the topic of the user's message before composing your answer.
When the user states a fact worth remembering (name, relationship, preference, correction), call remember_facts with the relevant text.
When the user says something was wrong or should be forgotten, call retract_fact with their statement.
Do not mention the tools by name in your replies — use the recalled facts naturally in your response.
Prefer specificity: query recall_memory with "family", "pets", "where I live", etc. rather than generic terms.
```

---

## Verification

1. Restart Claude Desktop after updating the config file.
2. Open a new conversation. The MCP tools indicator (hammer icon) should appear in the input bar. Click it to confirm all six tools are listed — `recall_memory`, `remember_facts`, `ingest_document`, `learn_facts`, `retract_fact`, `forget_fact`.
3. Test recall: type `what do you know about me?` — Claude should call `recall_memory` and either return known facts or a clean empty response (not an error).
4. Test ingest: type `my name is [your name]` — Claude should call `remember_facts`. Verify with:
   ```bash
   psql "$POSTGRES_DSN" -c "SELECT alias, is_preferred FROM entity_aliases ORDER BY created_at DESC LIMIT 5;"
   ```
5. Check the MCP server's stderr for diagnostics:
   ```bash
   # Claude Desktop routes MCP stderr to its own log — check:
   # macOS:  ~/Library/Logs/Claude/mcp-server-faultline.log
   # Linux:  ~/.config/Claude/logs/mcp-server-faultline.log
   ```
