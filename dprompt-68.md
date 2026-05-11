# dprompt-68: MCP Server Wrapper for FaultLine

**Date:** 2026-05-14  
**Severity:** Feature  
**Status:** Specification complete

## Problem

FaultLine currently exposes FastAPI endpoints (`/ingest`, `/query`, `/retract`, `/extract`, `/store_context`). To integrate with Claude (and other Claude-based applications via MCP protocol), we need an MCP server wrapper that exposes these endpoints as standardized MCP tools.

## Solution

Create an MCP server that:
1. Wraps existing FastAPI endpoints as MCP tool definitions
2. Handles user_id context management across tool invocations
3. Implements proper input/output schemas matching MCP spec
4. Maintains per-user isolation (user_id passed in tool input)

## Scope

### Files to Create

**`src/mcp/server.py`** — MCP server implementation
- FastAPI client to connect to local FaultLine instance
- Tool definitions: `extract`, `ingest`, `query`, `retract`, `store_context`
- User context handling
- Error handling + graceful degradation

**`src/mcp/tools.py`** — Tool schemas and handlers
```python
TOOLS = [
  {
    "name": "extract",
    "description": "Preflight entity extraction using GLiNER2",
    "inputSchema": {
      "type": "object",
      "properties": {
        "text": {"type": "string", "description": "Input text"},
        "user_id": {"type": "string", "description": "User UUID"}
      },
      "required": ["text", "user_id"]
    }
  },
  {
    "name": "ingest",
    "description": "Ingest facts into the knowledge graph",
    "inputSchema": {
      "type": "object",
      "properties": {
        "text": {"type": "string"},
        "user_id": {"type": "string"},
        "edges": {"type": "array", "items": {"type": "object"}},
        "source": {"type": "string", "default": "mcp"}
      },
      "required": ["text", "user_id", "edges"]
    }
  },
  {
    "name": "query",
    "description": "Query knowledge graph for memory recall",
    "inputSchema": {
      "type": "object",
      "properties": {
        "text": {"type": "string", "description": "Query text"},
        "user_id": {"type": "string", "description": "User UUID"}
      },
      "required": ["text", "user_id"]
    }
  },
  {
    "name": "retract",
    "description": "Retract or delete facts",
    "inputSchema": {
      "type": "object",
      "properties": {
        "user_id": {"type": "string"},
        "subject": {"type": "string"},
        "rel_type": {"type": "string"},
        "old_value": {"type": "string"},
        "behavior": {"type": "string", "enum": ["supersede", "hard_delete", "immutable"]}
      },
      "required": ["user_id", "subject"]
    }
  },
  {
    "name": "store_context",
    "description": "Store raw text context directly (no fact extraction)",
    "inputSchema": {
      "type": "object",
      "properties": {
        "text": {"type": "string"},
        "user_id": {"type": "string"}
      },
      "required": ["text", "user_id"]
    }
  }
]
```

**`mcp_server.py`** — Entry point (root of repo)
- Launches MCP server on stdio (Claude integration standard)
- Uses Claude SDK or mcp library (Python)
- Configurable via env (FAULTLINE_API_URL default to http://localhost:8001)

**Tests:** `tests/mcp/test_server.py`
- Mock FaultLine endpoints (httpx mocking)
- Verify tool schemas valid per MCP spec
- Test user_id isolation (different users → different results)
- Error handling (timeout, 500, invalid user_id)

### What NOT to Do

- Do NOT modify existing FastAPI endpoints
- Do NOT commit to production (dev only)
- Do NOT create breaking changes to /extract, /ingest, /query, /retract, /store_context
- Do NOT assume FaultLine is running on localhost:8001 in production — use env var

### Key Constraints

1. **Dev branch only** — create new files, no changes to existing ones
2. **No commits yet** — work in progress, await user approval before any git commit
3. **MCP spec compliance** — follow mcp.json spec for tool schemas
4. **Per-user isolation** — user_id always required in tool input, no global state
5. **Error handling** — graceful fallback if FaultLine API unavailable (don't crash MCP server)
6. **Tests passing** — new tests must pass, no regressions in existing tests

## Success Criteria

✅ MCP server created and launchable (`python mcp_server.py`)
✅ All 5 tools callable from Claude with proper schemas
✅ User_id isolation tested (different users get different results)
✅ Error handling tested (API timeout, 500, invalid input)
✅ Tests pass (new tests + existing tests unaffected)
✅ No commits made (dev branch only, awaiting approval)

## Deliverables

- `src/mcp/server.py` — MCP server implementation
- `src/mcp/tools.py` — Tool definitions and schemas
- `src/mcp/__init__.py` — Module init
- `mcp_server.py` — Entry point (root)
- `tests/mcp/test_server.py` — Test suite
- Updated `scratch.md` with completion status

## Note

This is a feature addition, not a bug fix. No production deployment until user approval and testing complete.

---

**Reference:** dprompt-68.md (specification)  
**Next:** dprompt-68b.md (execution template for deepseek)
