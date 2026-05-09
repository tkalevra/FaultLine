# Deepseek Debug Prompt: Memory Injection Failure + User ID Leak (CRITICAL)

**Scope:** Debug why memory extraction is completely broken + user_id still leaking.

**Issues:**
1. **No facts injected** — User queries like "tell me about my family" and "tell me what you know from memory" return zero facts to the user. Memory block is empty.
2. **User ID still leaking** — Second query response includes "your ID is 3f8e6836-72e3-43d4-bbc5-71fc8668b070" despite dprompt-11 metadata stripping fix.

**Hypothesis:** dprompt-11 metadata stripping may have broken the memory injection pipeline, or `/query` is failing silently.

---

## Debug Plan

### Part 1: Verify /query Endpoint is Being Called

In Filter `openwebui/faultline_tool.py`, around line 1300+ where `/query` is called:

```python
# ADD THIS DEBUG BLOCK:
if self.valves.ENABLE_DEBUG:
    print(f"[FaultLine Filter] CALLING /query with user_id=[redacted]")
    print(f"[FaultLine Filter] query text: {text[:100]}")

# AFTER /query returns:
if self.valves.ENABLE_DEBUG:
    print(f"[FaultLine Filter] /query RESPONSE: status={response.get('status')}, facts_count={len(response.get('facts', []))}")
    if response.get('facts'):
        for fact in response.get('facts', [])[:3]:  # First 3 facts
            print(f"[FaultLine Filter]   fact: {fact}")
```

**Check logs:**
- Is `/query` being called at all?
- How many facts are returned from `/query`?
- Are facts in the response?

### Part 2: Verify Metadata Stripping Didn't Break Facts

In `src/api/main.py`, `/query` endpoint where facts are returned:

The metadata stripping code was added. **PROBLEM:** The code is duplicated at the last return statement (lines ~2560+). This is syntactically fine but indicates sloppy application.

**More critical:** Check if the facts dict is properly formed after stripping. The `pop()` operation should work, but verify:
- Facts are still dicts after stripping
- "subject" and "object" fields are preserved
- "rel_type" field is preserved
- Only internal keys are removed

**Add debug:**
```python
# Before return statement in /query
if facts_count := len(merged_facts):
    log.info(f"query.final_facts_before_return", count=facts_count, sample_fact=merged_facts[0] if merged_facts else None)
```

### Part 3: Verify Memory Block is Being Built

In Filter `_build_memory_block()`:

**Add debug:**
```python
# At start of _build_memory_block
if facts:
    print(f"[FaultLine Filter] _build_memory_block: {len(facts)} facts received")
else:
    print(f"[FaultLine Filter] _build_memory_block: ZERO FACTS received")

# At end of _build_memory_block
print(f"[FaultLine Filter] memory_block length: {len(memory_block)}")
print(f"[FaultLine Filter] memory_block first 200 chars: {memory_block[:200]}")
```

### Part 4: Verify Memory Block is Being Injected

In Filter inlet, around line 1560+ where memory is injected:

**Current code:**
```python
msgs = body["messages"]
injected = False
for i in range(len(msgs) - 1, -1, -1):
    if msgs[i].get("role") == "user":
        msgs.insert(i, {"role": "system", "content": memory_block})
        injected = True
        break
if not injected:
    msgs.append({"role": "system", "content": memory_block})
```

**Add debug:**
```python
if self.valves.ENABLE_DEBUG:
    print(f"[FaultLine Filter] memory_injection: injected={injected}, total_messages_after={len(body['messages'])}")
    if injected:
        print(f"[FaultLine Filter] injected at position, memory_block={memory_block[:150]}")
```

### Part 5: Trace User ID Leak Source

The user_id UUID is still appearing in LLM responses. This suggests:

**Option A:** OpenWebUI is passing user info to LLM outside FaultLine's control
- Check if `body["user"]` or similar field contains UUID
- Filter should strip or redact any user identity fields from body before LLM sees it

**Option B:** Facts still contain user_id despite stripping
- Add debug to print all fields of returned facts:
```python
if response.get('facts'):
    print(f"[FaultLine Filter] fact fields: {list(response['facts'][0].keys())}")
```

**Option C:** Debug output is still leaking (ENABLE_DEBUG=True and logs visible to LLM somehow)
- Verify all debug prints are actually redacted

### Part 6: Check Relevance Gate

Query `/query` manually via curl with a simple query like "tell me about my family":

```bash
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "tell me about my family",
    "user_id": "test_user_123"
  }' | jq .
```

**Verify:**
- Status is "ok"
- Facts list is not empty
- Each fact has subject, object, rel_type fields
- No internal keys (user_id, qdrant_synced, etc.) remain

---

## Critical Questions to Answer

1. **Is `/query` being called?** (Check logs for "CALLING /query")
2. **How many facts returned from `/query`?** (Check logs for fact_count)
3. **Are facts making it to memory block?** (Check logs for "_build_memory_block")
4. **Is memory being injected?** (Check logs for "memory_injection")
5. **Where is user_id still appearing?** (OpenWebUI context? Facts? Debug output?)

---

## Done When

- ✅ Logs show `/query` being called with facts returned
- ✅ Facts list not empty and properly formed (no internal keys)
- ✅ Memory block being built and injected to messages
- ✅ User ID NOT appearing in logs or responses
- ✅ "tell me about my family" returns facts in LLM response
- ✅ Manual curl test of `/query` returns clean facts

Ship debug logs + findings.

---

## Notes

- Enable `ENABLE_DEBUG=True` in Filter valves to see all debug output
- Check Docker logs: `docker logs faultline-api` and `docker logs faultline-filter` (if separate)
- This is a full-stack debug: /query endpoint → Filter memory building → message injection → LLM response
- User ID leak may be separate from memory injection failure — they might have different root causes
