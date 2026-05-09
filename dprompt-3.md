# Deepseek Implementation Prompt: Fix UUID Leaking in Memory Injection

**Scope:** Add display name resolution to the Filter before building memory blocks. UUIDs are leaking into the memory injection — user sees `54214459-...` instead of `mars`.

**Why:** CLAUDE.md hard constraint: "Display names are stored in `entity_aliases.alias`, never in `*_id` columns." The Filter should convert UUIDs back to human-readable names before injecting memory.

---

## The problem

Facts come back from `/query` with UUIDs as subject/object:
```json
{
  "subject": "54214459-3d2e-5ff5-8c6c-a541667d93aa",  // UUID, not display name
  "object": "7e4bff75-706e-5feb-b8b5-f4ca1247fd3b",   // UUID, not display name
  "rel_type": "has_pet"
}
```

These get injected as-is into memory, so the LLM sees:
```
54214459-... has has_pet 7e4bff75-...
```

Should be:
```
mars has_pet fraggle
```

---

## The fix

In `openwebui/faultline_tool.py`, add a **display name resolver** before `_build_memory_block()`:

### Step 1: Add helper function
```python
def _resolve_display_names(facts: list[dict], preferred_names: dict, identity: Optional[str]) -> list[dict]:
    """
    Convert UUID subject/object to display names using preferred_names dict.
    
    preferred_names comes from /query response: {uuid: "display_name"}
    identity is the canonical user identity (usually "user")
    
    Returns facts with subject/object replaced by display names where available.
    """
    resolved = []
    for fact in facts:
        resolved_fact = fact.copy()
        subject = fact.get("subject", "")
        object_ = fact.get("object", "")
        
        # Resolve subject UUID to display name, fallback to identity if it's the user UUID
        if subject in preferred_names:
            resolved_fact["subject"] = preferred_names[subject]
        elif subject == identity:
            resolved_fact["subject"] = "user"
        
        # Resolve object UUID to display name, fallback to identity
        if object_ in preferred_names:
            resolved_fact["object"] = preferred_names[object_]
        elif object_ == identity:
            resolved_fact["object"] = "user"
        
        resolved.append(resolved_fact)
    
    return resolved
```

### Step 2: Call resolver before `_build_memory_block()`
In the `inlet()` function where memory is built (around line 1265), add:
```python
if will_query and (facts or preferred_names or canonical_identity):
    # ... existing code ...
    
    # BEFORE building memory block, resolve UUIDs to display names
    facts = _resolve_display_names(facts, preferred_names, canonical_identity)
    
    memory_block = self._build_memory_block(
        facts, preferred_names, canonical_identity, entity_attributes, ...
    )
```

---

## Test the fix

1. Run Case 1 from dprompt-2: "Mars has a pet dog named Fraggle"
2. In new chat, ask "Do I have pets?"
3. Check the memory injection in logs or the model response
4. Verify: fact shows as `mars has_pet fraggle`, NOT `54214459-... has_pet 7e4bff75-...`

---

## CONSTRAINTS (same as before)

- ✅ CAN: modify `openwebui/faultline_tool.py` only
- ❌ CANNOT: restart docker, change database, delete files
- ❌ CANNOT: add new dependencies
- ✅ CAN: curl test, check logs

---

## Done when

- ✅ Display name resolver added
- ✅ Called before `_build_memory_block()`
- ✅ Test Case 1 passes with display names (not UUIDs)
- ✅ Logs show resolved facts
- ✅ No regressions on other cases

Ship it.
