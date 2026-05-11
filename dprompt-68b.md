# dprompt-68b: MCP Server Wrapper Implementation — DEEPSEEK_INSTRUCTION_TEMPLATE

**Template version:** 1.0  
**Philosophy:** Create MCP server wrapper for FaultLine. Dev-only feature branch work. No commits until user approval.

---

## CRITICAL: WORK SCOPE & CONSTRAINTS

**⚠️ YOU MUST READ THIS FIRST:**

1. **DEV BRANCH ONLY** — All new files go in `src/mcp/` and `tests/mcp/`. Do NOT modify existing files.
2. **NO GIT COMMITS** — You will create files and write code. Do NOT run `git add` or `git commit`. User will review and decide.
3. **FEATURE, NOT BUG FIX** — This is new functionality. Production deployment happens ONLY after user approval and testing.
4. **NEW FILES ONLY** — Create `src/mcp/server.py`, `src/mcp/tools.py`, `src/mcp/__init__.py`, `mcp_server.py`, `tests/mcp/test_server.py`.

If you attempt to commit or modify existing files, stop immediately and report in scratch.md what you were about to do.

---

## Task

Implement an MCP server wrapper for FaultLine that exposes existing FastAPI endpoints (`/ingest`, `/query`, `/retract`, `/extract`, `/store_context`) as standardized MCP tools. Per-user isolation maintained via user_id in tool inputs.

**Read:** dprompt-68.md for full specification. This prompt tells you HOW to execute it.

## Execution Sequence

### 1. Environment & Dependencies Check

```bash
# Check Python version
python --version  # Must be 3.11+

# Check if mcp library available or if we need to use httpx + mcp spec
pip list | grep -i mcp || echo "MCP library not installed — will use raw spec"

# Confirm httpx available (for API calls to FaultLine)
python -c "import httpx; print('httpx available')"
```

**If mcp library unavailable:** Use raw Python with stdio communication following mcp.json spec (slightly more code, fully compatible).

**Decision point:** Report findings in scratch.md, then proceed.

### 2. Create Module Structure

```bash
mkdir -p src/mcp
mkdir -p tests/mcp
touch src/mcp/__init__.py
```

### 3. Implement `src/mcp/tools.py`

**Requirements:**
- Define TOOLS list with 5 tool objects (extract, ingest, query, retract, store_context)
- Each tool has: name, description, inputSchema (with required fields)
- Schemas match dprompt-68.md specification exactly
- Input validation: text (string, non-empty), user_id (string, non-empty), edges (array of dicts for ingest)

**No API calls in this file** — pure schema definitions.

**Validation helpers:**
```python
def validate_text(text: str) -> bool:
    return isinstance(text, str) and len(text.strip()) > 0

def validate_user_id(user_id: str) -> bool:
    return isinstance(user_id, str) and len(user_id.strip()) > 0
```

### 4. Implement `src/mcp/server.py`

**Requirements:**
- Import httpx, json, sys, os
- Load FAULTLINE_API_URL from env (default: http://localhost:8001)
- Implement async function for each tool: `extract_tool()`, `ingest_tool()`, `query_tool()`, `retract_tool()`, `store_context_tool()`
- Each function POSTs to corresponding FaultLine endpoint
- Handle errors gracefully (timeout, 500, invalid response) — log error, return error dict, don't crash

**Error handling pattern:**
```python
async def query_tool(text: str, user_id: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{FAULTLINE_API_URL}/query",
                json={"text": text, "user_id": user_id}
            )
            response.raise_for_status()
            return response.json()
    except httpx.TimeoutException:
        return {"error": "FaultLine API timeout (10s)"}
    except httpx.HTTPError as e:
        return {"error": f"FaultLine API error: {e.response.status_code}"}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}
```

- Implement MCP message loop (if using mcp library, follow their docs; if raw, implement stdio protocol)

### 5. Implement `mcp_server.py` (Root Entry Point)

**Requirements:**
- Imports: `from src.mcp.server import run_mcp_server` (or equivalent)
- `if __name__ == "__main__":` block launches MCP server
- Accepts optional command-line args: `--api-url http://custom:8001`
- Logs startup message to stderr (stdout reserved for MCP protocol)

**Minimal example:**
```python
import sys
import asyncio
from src.mcp.server import run_mcp_server

if __name__ == "__main__":
    asyncio.run(run_mcp_server())
```

### 6. Implement `tests/mcp/test_server.py`

**Requirements:**
- Mock httpx.AsyncClient to simulate FaultLine API responses
- Test each tool with valid input → expected output
- Test each tool with invalid input → error dict returned
- Test user_id isolation: tool called with user_id="alice" and user_id="bob" returns different results (mock DB separation)
- Test timeout handling: AsyncClient times out → error dict with "timeout" message
- Test 500 error: AsyncClient returns 500 → error dict with status code
- Test schema compliance: each tool's inputSchema is valid JSON Schema

**Mock pattern:**
```python
from unittest.mock import AsyncMock, patch
import httpx

@pytest.mark.asyncio
async def test_query_tool_success():
    mock_response = {"facts": [...], "preferred_names": {...}}
    with patch("httpx.AsyncClient.post") as mock_post:
        mock_post.return_value.json.return_value = mock_response
        result = await query_tool("tell me about my family", "user-123")
        assert result == mock_response
        mock_post.assert_called_once_with(...)
```

### 7. Run Tests

```bash
pytest tests/mcp/test_server.py -v
pytest tests/ --ignore=tests/evaluation --ignore=tests/feature_extraction --ignore=tests/model_inference --ignore=tests/preprocessing
```

**Success:** All new tests pass, no regressions in existing tests.

### 8. Manual Test (If Possible)

If FaultLine is running locally:
```bash
# Terminal 1: Start FaultLine (if not already running)
docker compose up

# Terminal 2: Test MCP server
python mcp_server.py
```

Verify server starts without errors (MCP protocol prints to stdout, diagnostic logs to stderr).

### 9. Update `scratch.md`

Add entry under "## Current State":
```
## ✓ IN PROGRESS: dprompt-68 (MCP Server Wrapper) — 2026-05-14

**Task:** Implement MCP server wrapper for FaultLine endpoints.

**Status:** DEVELOPMENT ONLY — no commits made yet.

**Files created:**
- src/mcp/server.py — MCP server + tool handlers
- src/mcp/tools.py — Tool definitions & schemas
- src/mcp/__init__.py — Module init
- mcp_server.py — Entry point
- tests/mcp/test_server.py — Test suite

**Tests:** All passing (new + existing)

**Next:** Await user review and approval before committing.
```

### 10. STOP & REPORT

Do NOT commit. Do NOT push. Do NOT modify existing files.

Write final summary in scratch.md:

```markdown
## AWAITING REVIEW: dprompt-68 (MCP Server Wrapper)

**Status:** Development complete. No commits made. Ready for user review.

**Verification:**
- All new tests pass ✓
- No existing tests broken ✓
- No modifications to existing endpoints ✓
- Dev-only work in src/mcp/ and tests/mcp/ ✓
- No git commits attempted ✓

**User next action:** Review code, approve, then decide on commit/merge strategy.
```

Then **STOP immediately**. Do not proceed further. User will review.

---

## Success Criteria (All Required)

✅ MCP server created and launchable  
✅ 5 tools callable with proper schemas  
✅ User_id isolation verified in tests  
✅ Error handling tested (timeout, 500, invalid)  
✅ All tests passing (new + existing)  
✅ No git commits made  
✅ Dev-only work (no changes to existing files)  
✅ Summary in scratch.md  

## Do NOT

- Commit to git (any branch)
- Modify existing endpoints or handlers
- Change `src/api/main.py`, `docker-compose.yml`, or other production files
- Deploy or push to remote
- Assume FaultLine is running (tests should mock it)

## Critical Rules

**NO COMMITS.** User decides when (if) to commit.

**DEV ONLY.** All new files, no existing file modifications.

**TESTS PASS.** No regressions.

**STOP CLAUSE MANDATORY.** Report completion, then stop. Await user approval.

---

**Template version:** 1.0  
**Philosophy:** Feature-branch work. User-controlled deployment. Tests validate correctness before merge.  
**Status:** Ready for execution by deepseek
