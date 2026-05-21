# dBug-016: OpenWebUI Filter Integration Failure — Validated Analysis

**Status:** ROOT CAUSE FOUND  
**Severity:** HIGH — blocks all LLM-based fact extraction  
**Date:** 2026-05-15  
**Validated by:** deepseek (pre-prod log analysis + code trace)

## Summary

The Filter's `rewrite_to_triples()` calls OpenWebUI's `/api/chat/completions` endpoint without a `chat_id` in the request body. OpenWebUI v0.9.5 builds `metadata['chat_id'] = None`, and downstream code crashes with `'NoneType' object has no attribute 'startswith'` when it calls `.startswith()` on the None value. This has been broken since container start but was MASKED by the old `_IS_PURE_QUESTION` regex which skipped extraction for all question-form messages. dprompt-75b's semantic classifier correctly identifies "what did I do to my back?" as personal context requiring extraction, which exposes the pre-existing crash.

## Confirmed Issue

LLM-based fact extraction via `rewrite_to_triples()` has never worked in this deployment. All 12 extraction attempts in the logs returned 400. Regex fallback (`_extract_basic_facts`) has been the sole working extraction path, handling identity/preference patterns but incapable of extracting medical, temporal, or other semantic facts.

## Evidence

### Timeline
| Timestamp | Event |
|-----------|-------|
| 2026-05-12 14:28:14 | Container started |
| 2026-05-12 14:29:42 | First crash (88 seconds after start) |
| Every call since | All `rewrite_to_triples` calls fail (12/12) |

### Log Pattern (every crash identical)
```
[FaultLine Filter] POST http://192.168.1.10:8001/extract "HTTP/1.1 200 OK"
ERROR open_webui.main:process_chat:2013 - 'NoneType' object has no attribute 'startswith'
POST /api/chat/completions HTTP/1.1" 400
[FaultLine] rewrite_to_triples HTTP error: 400
[FaultLine] rewrite_to_triples response body: {"detail":"'NoneType' object has no attribute 'startswith'"}
[FaultLine Filter] raw_triples=[]
```

### Successful vs Failing Endpoint Calls
- **Successful** `/api/chat/completions` (200): all from `192.168.1.10:0` (internal OpenWebUI routing, WITH chat_id)
- **Failing** `/api/chat/completions` (400): all from `172.16.9.1` (external/direct calls, WITHOUT chat_id)

### Zero Successful LLM Extractions
- `raw_triples=[]` in all 12 logged attempts
- Zero non-empty `raw_triples` in entire container log history

## Root Cause

### Primary: OpenWebUI cannot handle `/api/chat/completions` without `chat_id`

**Code trace:**

1. Filter calls `rewrite_to_triples()` → sends POST to `http://host.docker.internal:3000/api/chat/completions` (OpenWebUI's own endpoint, default when `LLM_URL` valve is empty)

2. OpenWebUI `chat_completion()` handler (main.py line 1764):
   ```python
   metadata = {
       'chat_id': form_data.pop('chat_id', None),  # ← None when not in request
       ...
   }
   ```

3. Downstream code in `process_chat_payload` / `background_tasks_handler` (middleware.py lines 3063–3064):
   ```python
   if (
       'chat_id' in metadata                    # ← True (key exists, value is None)
       and not metadata['chat_id'].startswith('local:')   # ← CRASH: NoneType
       and not metadata['chat_id'].startswith('channel:')
   ):
   ```
   The `'chat_id' in metadata` check passes because the key EXISTS (value is None). Then `.startswith()` is called on None → `AttributeError`.

4. Exception caught by `process_chat` except handler (main.py line 2010–2013):
   ```python
   except Exception as e:
       error_detail = str(e)  # "'NoneType' object has no attribute 'startswith'"
       log.error('Error processing chat payload: %s', error_detail)
       # Falls to else branch (no chat_id/message_id) → raises HTTPException 400
   ```

### Contributing: dprompt-75b exposed the pre-existing bug

- **Old behavior:** `_IS_PURE_QUESTION` regex matched ALL question-form messages (starting with what/who/where/when/how/why/is/are/do/does/can/could) → `_skip_rewrite = True` → `rewrite_to_triples` never called → crash never triggered for questions
- **New behavior (dprompt-75b):** `_should_skip_extraction()` correctly returns `False` for first-person questions ("what did I do to my back?" contains "I" and "my") → extraction proceeds → `rewrite_to_triples` called → crash triggered
- **dprompt-75b is CORRECT** — the semantic classifier works as aliceigned. It exposed a latent OpenWebUI bug.

### Non-question messages also affected

Both old and new Filter code call `rewrite_to_triples` for non-question messages. These have been silently failing with 400 the entire time. The regex fallback (`_extract_basic_facts`) masked the failure for messages containing explicit identity patterns ("my name is X", "I am X", etc.) but cannot extract semantic facts (medical, temporal, relational).

## Affected Components

| Component | Impact |
|-----------|--------|
| `openwebui/faultline_tool.py` — `rewrite_to_triples()` | Calls OpenWebUI endpoint, gets 400, returns [] |
| `open_webui/main.py` line 1764 | Builds metadata with `chat_id: None` |
| `open_webui/utils/middleware.py` lines 3063–3064 | Crashes on `None.startswith()` |
| All LLM extraction | Broken since container start (regex fallback only) |
| Medical/personal-context facts | Cannot be extracted (regex can't handle semantic rel_types) |

## Impact Scope

- **All users** — any message requiring LLM extraction gets regex-only fallback
- **All rel_types beyond identity** — medical, temporal, hierarchical, behavioral facts all lost
- **Silent degradation** — system appears functional (regex handles basic patterns) but semantic extraction is completely broken
- **Not a dprompt-75b regression** — bug predates the semantic classifier change

## Fix Options

### Option A — Set LLM_URL valve to a direct LLM endpoint (RECOMMENDED)

Configure `LLM_URL` to point directly to Ollama or another LLM backend instead of defaulting to OpenWebUI's own endpoint. This bypasses OpenWebUI's `process_chat` entirely.

- Example: `LLM_URL=http://ollama:11434/api/chat` (if Ollama is network-accessible)
- *Trade-off:* Requires Ollama to be reachable from the OpenWebUI container
- *Risk:* Low — one valve change, no code modification
- *Restores:* Full LLM extraction for all message types

### Option B — Guard against OpenWebUI recursion in the Filter

If `valves.LLM_URL` is empty (defaults to OpenWebUI), skip `rewrite_to_triples` entirely and rely on regex fallback only. Log a clear warning.

```python
# In inlet(), before calling rewrite_to_triples:
if not self.valves.LLM_URL:
    if self.valves.ENABLE_DEBUG:
        print("[FaultLine Filter] LLM_URL not set — skipping LLM extraction, using regex fallback only")
    raw_triples = []
else:
    raw_triples = await rewrite_to_triples(...)
```

- *Trade-off:* Loses all LLM extraction when no external LLM is configured. Medical/personal-context facts won't be extracted. But prevents the 400 crash and makes the limitation explicit.
- *Risk:* Low — additive guard, no behavior change for current (already broken) state

### Option C — Inject synthetic `chat_id` in the Filter's LLM request

Add `"chat_id": "faultline-extraction"` to the request body sent to `/api/chat/completions`.

- *Trade-off:* Hacky. Could cause DB pollution (fake chat sessions in OpenWebUI). Other OpenWebUI code paths might still crash on the fake chat_id.
- *Risk:* Medium — unknown side effects in OpenWebUI chat/DB management

### Option D — Upgrade OpenWebUI

Check if OpenWebUI v0.9.6+ fixes the `None.startswith()` crash when `chat_id` is missing.

- *Trade-off:* Requires full container rebuild. The bug may not be fixed upstream (the `'chat_id' in metadata` pattern is a subtle logic error).
- *Risk:* Medium — new OpenWebUI version could introduce other regressions

## Recommended Fix

**Option A** (set `LLM_URL` to a direct Ollama endpoint) is the intended aliceign — the `LLM_URL` valve exists specifically to route extraction LLM calls. The default fallback to OpenWebUI's own endpoint was a convenience that has never worked in this deployment.

If Ollama is not directly accessible from the OpenWebUI container, **Option B** (guard + skip) is the safe fallback — it prevents the crash and documents the limitation explicitly rather than silently failing with 400 on every extraction attempt.

## Validation Commands (for user)

```bash
# Check if Ollama is accessible from OpenWebUI container
ssh docker-host -x "sudo docker exec open-webui curl -s http://ollama:11434/api/tags 2>&1 | head -5"

# Or try host.docker.internal
ssh docker-host -x "sudo docker exec open-webui curl -s http://host.docker.internal:11434/api/tags 2>&1 | head -5"

# Check current LLM_URL valve setting (look in OpenWebUI UI or env)
ssh docker-host -x "sudo docker exec open-webui env | grep -i llm"

# Verify the crash with a direct test call
ssh docker-host -x "curl -s -X POST http://localhost:3000/api/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{\"model\":\"faultline-wgm-test-10\",\"messages\":[{\"role\":\"user\",\"content\":\"test\"}]}' 2>&1"
# Expected: 400 with 'NoneType' error (confirming the bug)
```

## Pre-Prod Reference

- **Instance:** docker-host.helpalicekpro.ca (docker-host)
- **Container:** open-webui (ghcr.io/open-webui/open-webui:v0.9.5)
- **Started:** 2026-05-12T14:28:14Z
- **FaultLine API:** http://192.168.1.10:8001
