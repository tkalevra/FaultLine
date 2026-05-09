# Deepseek Fix Prompt: Definitive User ID Leak Elimination (CRITICAL + CLAUDE.md Hard Constraints)

**Scope:** Eliminate user_id UUID leakage definitively. Enforce CLAUDE.md hard constraints.

**Issue:** Live exposure continues. User query "Tell me about my family" receives LLM response: "I know your unique identifier (3f8e6836-72e3-43d4-bbc5-71fc8668b070)". User_id UUID is still visible to LLM despite dprompt-13 fix.

**CLAUDE.md Hard Constraints (LOAD-BEARING):**

1. **Display names stored in aliases, never in *_id columns**
   - Entity display names live in `entity_aliases.alias`
   - Never in `subject_id`, `object_id`, or any `*_id` field
   - Filter converts UUIDs to display names before memory injection

2. **User IDs are internal identifiers — never visible to LLM or user**
   - `user_id` (the canonical_identity UUID) is for backend tracking only
   - LLM must never see, reference, or generate text about user_id
   - User-facing context uses only display names from `entity_aliases.alias` or keyword "user"

3. **Filter converts UUIDs to display names before memory injection**
   - All fact subjects/objects that are UUIDs → resolved to display names via `registry.get_preferred_name()`
   - All references to canonical_identity UUID → replaced with "user" keyword
   - No raw UUIDs in memory block text

4. **Memory injection gate: facts scored, identity rels always pass**
   - Facts scoring below 0.4 excluded
   - Identity rels (also_known_as, pref_name, same_as) always pass
   - Only facts that survive gate inject
   - No fallback leak: `_filter_relevant_facts()` returns `scored` only

---

## Root Cause Analysis

User_id leak persists despite dprompt-13 fix. Three possible sources:

### Source 1: `/query` response still contains user_id field

**Check:**
```bash
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{"query": "tell me about myself", "user_id": "test_user"}' \
  | jq '.facts[0] | keys' | grep user_id
```

If `user_id` appears in output, the stripping isn't working.

**Fix location:** `src/api/main.py`, `/query` endpoint. Verify the metadata stripping block:
```python
_INTERNAL_KEYS = ("user_id", "qdrant_synced", "superseded_at", "fact_class", "promoted_at", "confirmed_count")
for _f in merged_facts:
    for _k in _INTERNAL_KEYS:
        _f.pop(_k, None)
```

**Verification:** After stripping, print `merged_facts[0]` to logs. Confirm user_id is gone.

### Source 2: Filter is not resolving canonical_identity UUID

**Check:** In Filter `_build_memory_block()`, the `identity` parameter is the canonical_identity UUID. If written to memory block text anywhere, it leaks.

**Audit:**
- Line ~989: `if identity:` should use display name from `preferred_names.get(identity, "user")`, never write raw `identity`
- All f-strings with `{identity}` should be replaced with display-safe version
- Debug output should show `identity=[redacted]`

**CLAUDE.md violation:** "Filter converts UUIDs to display names before memory injection" — if `identity` UUID appears in memory block, this is violated.

### Source 3: OpenWebUI system context passing user_id to LLM

**Most likely:** OpenWebUI's system prompt or user context includes the user_id UUID before FaultLine ever touches the body.

**Fix location:** Filter inlet, very start of processing:

```python
# NUCLEAR OPTION: Strip ALL UUID patterns from body before any LLM call
def _strip_all_uuids_from_body(body: dict) -> dict:
    """Remove all UUID patterns from body to prevent LLM exposure."""
    _UUID_PATTERN = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
    
    # Recursively strip UUIDs from messages
    if "messages" in body:
        for msg in body["messages"]:
            if "content" in msg and isinstance(msg["content"], str):
                msg["content"] = re.sub(_UUID_PATTERN, "[REDACTED_UUID]", msg["content"])
    
    # Strip from user object if present
    if "user" in body and isinstance(body["user"], dict):
        user = body["user"]
        if "id" in user:
            user["id"] = "[REDACTED_USER_ID]"
    
    return body
```

Apply this BEFORE any LLM interaction. This is belt-and-suspenders: catches UUIDs from any source.

---

## The Definitive Fix

### Step 1: Verify /query metadata stripping works

In `src/api/main.py`, `/query` endpoint, add debug before return:

```python
# DEBUG: Confirm user_id stripped
if merged_facts and "user_id" in merged_facts[0]:
    log.warning("query.user_id_not_stripped", fact_keys=list(merged_facts[0].keys()))
else:
    log.info("query.user_id_stripped_ok", fact_count=len(merged_facts))

return {"status": "ok", "facts": merged_facts, ...}
```

If warning appears in logs, the stripping code isn't executing. Investigate why.

### Step 2: Enforce CLAUDE.md constraint in Filter

In Filter `_build_memory_block()`, add guard at top:

```python
# CLAUDE.md hard constraint: no UUIDs in memory block
# Replace canonical_identity UUID with "user" keyword
_identity_display = preferred_names.get(canonical_identity, "user") if canonical_identity else "user"
if canonical_identity and canonical_identity != _identity_display:
    # UUID found, need to replace with display name
    log.info("memory_block.identity_resolved", uuid=canonical_identity[:12], display=_identity_display)
```

Then use `_identity_display` everywhere in the function instead of raw `canonical_identity`.

### Step 3: Nuclear option — strip all UUIDs from body

In Filter inlet (very top, before any processing):

```python
# Pre-filter: remove all UUID patterns from body to prevent LLM exposure
# CLAUDE.md: user IDs are internal identifiers, never visible to LLM
_UUID_PATTERN = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
if "messages" in body:
    for msg in body["messages"]:
        if "content" in msg and isinstance(msg["content"], str):
            msg["content"] = re.sub(_UUID_PATTERN, "[SYSTEM_ID]", msg["content"])

if "user" in body and isinstance(body["user"], dict):
    if "id" in body["user"]:
        # Replace user_id with placeholder that won't leak
        user_id_original = body["user"]["id"]
        body["user"]["id"] = "system_user"  # Generic, not revealing
```

This catches any UUID that OpenWebUI or upstream sources inject.

---

## Validation Checklist (CLAUDE.md Compliance)

- ✅ `/query` response has no `user_id` field (verified via curl + jq)
- ✅ `/query` logs show "user_id_stripped_ok" (not "user_id_not_stripped")
- ✅ Memory block uses `_identity_display` (display name or "user"), never raw `canonical_identity`
- ✅ Filter strips all UUID patterns from body before LLM call
- ✅ Debug output redacted: user_id → `[redacted]`, canonical_identity → `[redacted]`
- ✅ Live test: "Tell me about my family" returns ZERO UUID patterns (grep response for `[0-9a-f]{8}-`)
- ✅ Live test: "What do you know from memory" returns ZERO UUID patterns
- ✅ **CLAUDE.md compliance verified:** LLM never sees user_id; only display names and "user" keyword visible

---

## Done When

- ✅ `/query` metadata stripping verified working (logs confirm)
- ✅ Memory block uses display names/`"user"`, never raw UUIDs
- ✅ Filter strips all UUID patterns from body
- ✅ Live validation: "Tell me about my family" + "What do you know" both return zero UUIDs
- ✅ CLAUDE.md hard constraints enforced:
  - Display names stored in aliases only
  - User IDs internal, never visible
  - Filter converts UUIDs before injection
  - No leaks to LLM or user
- ✅ Code clean, tests pass

Ship fix with full compliance audit.

---

## CLAUDE.md Hard Constraint Violation if This Fails

**Current state:** VIOLATED
- User_id UUID exposed to LLM
- LLM generating text about internal identifiers
- Filter not converting UUIDs to display names

**Must enforce:**
> "Display names stored in `entity_aliases.alias`, never in `*_id` columns. Filter converts UUIDs to display names before memory injection."

> "User IDs are internal system identifiers — never visible to LLM or user."

This is a security + design constraint violation. Fix is non-negotiable.
