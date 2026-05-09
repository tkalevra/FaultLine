# Deepseek Fix Prompt: User ID Leakage to LLM (CRITICAL)

**Scope:** Fix critical privacy leak where user_id UUIDs appear in LLM responses.

**Issue:** Live chat exposure: User asked "tell me about my family please" and received response containing raw user_id UUID `3f8e6836-72e3-43d4-bbc5-71fc8668b070` in LLM output. This reveals internal system identifiers to the user.

**Why:** User ID is leaking into the context visible to the LLM, allowing it to reference the UUID in responses. This violates CLAUDE.md hard constraint: "Display names stored in aliases, never in *_id columns. Filter converts UUIDs to display names before memory injection."

**CLAUDE.md Constraint (Hard):**
> No internal system identifiers (user_id, canonical_identity UUIDs) shall be visible to the LLM or end user. Only display names and "user" keyword permitted in user-facing context.

---

## Root Cause Analysis

Three possible leak sources:

### 1. Facts returned from `/query` include user_id field

Check `/query` response JSON — if facts have `{"user_id": "...", "subject": "...", ...}`, the field should be stripped before returning to Filter.

**Fix location:** `src/api/main.py`, `/query` endpoint, before returning `merged_facts` to Filter. Strip `user_id` from each fact dict.

### 2. Memory block construction includes canonical_identity UUID

In Filter `_build_memory_block()`, the `canonical_identity` parameter (which is the user's UUID) may be written into the memory block text if it's used as a fallback display name anywhere.

**Fix location:** `openwebui/faultline_tool.py`, `_build_memory_block()`. Audit all places where `identity` or `canonical_identity` is written to `lines`. Replace with "user" keyword only. Never write raw UUID to memory block.

### 3. Debug output or system context leaking

If `ENABLE_DEBUG=True`, debug prints may include user_id and be visible to LLM. Or OpenWebUI system context may be passing user_id to the LLM.

**Fix location:** `openwebui/faultline_tool.py`, Filter. Redact user_id from all debug output. Ensure no debug content reaches the final message body.

---

## The Fix

### Step 1: Strip user_id from /query response

In `src/api/main.py`, `/query` endpoint (around line 2500+):

```python
# Before returning merged_facts to Filter
for fact in merged_facts:
    fact.pop("user_id", None)  # Remove internal field
    fact.pop("qdrant_synced", None)  # Remove internal metadata
    fact.pop("superseded_at", None)  # Remove internal metadata

return {"facts": merged_facts, "preferred_names": preferred_names, ...}
```

**Rationale:** `user_id` is for database tracking only. Filter doesn't need it, and LLM absolutely must not see it.

### Step 2: Audit memory block for canonical_identity leaks

In `openwebui/faultline_tool.py`, `_build_memory_block()`:

**Search for these patterns and replace:**
- Line ~989: `if identity:` → Always use `"user"` or entity display name, never write `identity` (which is UUID) to output
- Line ~1113: `if identity and subj in _user_anchors:` → OK (internal logic), but output uses `rel` and `obj`, not `identity`
- Line ~1129: `if identity and identity in entity_attributes:` → OK (key lookup), but output uses `attr_parts`, not `identity`

**Verify:** Search file for all string literals being added to `lines` that might contain `{identity}` or f-strings with identity variable. Replace any leaked UUIDs with "user".

### Step 3: Redact debug output

In Filter inlet logic:

```python
# BEFORE: print(f"[FaultLine Filter] user_id={user_id} text='{text[:80]}'")
# AFTER: 
if self.valves.ENABLE_DEBUG:
    print(f"[FaultLine Filter] [redacted user] text='{text[:80]}'")
```

Replace all debug output that includes `user_id` with `[redacted user]` placeholder.

---

## Validation Checklist

- ✅ `/query` response has no `user_id` field in facts
- ✅ Memory block never writes raw UUID — only display names or "user" keyword
- ✅ Debug prints redact user_id (show `[redacted user]` instead)
- ✅ All internal fields stripped: `qdrant_synced`, `superseded_at`, `fact_class`, `promoted_at`
- ✅ Test: "tell me about my family" returns no UUID in response
- ✅ Test: `ENABLE_DEBUG=True` doesn't leak user_id to LLM
- ✅ Test: facts with user_anchors converted to "user" display correctly

---

## Done When

- ✅ `/query` strips user_id + internal metadata from facts
- ✅ Memory block audited: no raw UUIDs in output
- ✅ Debug output redacted: user_id replaced with placeholder
- ✅ Live validation: "tell me about my family" response contains zero UUID patterns
- ✅ No regression: existing memory injection still works, just cleaner

Ship it.

---

## Critical Notes

- **User privacy:** User ID is authentication material. Leaking it is a security issue.
- **Scope:** This is a Filter-facing leak, not a database corruption issue.
- **Test coverage:** Manual live chat test with "tell me about X" query. Grep response for UUID pattern `[0-9a-f]{8}-[0-9a-f]{4}-` — should find 0 matches.
