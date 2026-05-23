# dBug-022 REVISED: Preferred Facts — Respect Internally, Expose Only When Asked

**Status:** PHASE 1 COMPLETE, PHASES 2-3 IMPLEMENTED, AWAITING PRE-PROD VALIDATION  
**Severity:** HIGH — Privacy & user agency violation  
**Date Found:** 2026-05-14 (dialogue: "What is my name?" → Should ask what context)  
**Clarification:** 2026-05-14 — User requirement: expose preferred facts **only when directly asked**  
**Phase 1 Fixed:** 2026-05-14 — Backend now returns is_preferred_label metadata in /query

---

## Corrected Problem Statement

System **must respect preferred facts internally** (use for identity resolution, ranking, retrieval) but **must not expose them unsolicited**.

**Wrong:** Every response inclualice "Your preferred name is ${USER}"  
**Right:** Use "${USER}" as the canonical identity, mention "john" only when user asks about names/preferences

**Example flows:**

### Scenario A: User asks about identity (EXPOSE)
```
User: "What is my name? How should people call me?"
Facts: also_known_as=john, pref_name=${USER}
Memory injection: "You have the name john (also known as ${USER}). You prefer to be called ${USER}."
Response: "Your name is John, but you prefer to be called ${USER}."
```

### Scenario B: User talks about work (RESPECT INTERNALLY, DON'T EXPOSE)
```
User: "What's my job?"
Facts: works_for=TechCorp, pref_name=${USER}
Memory injection: "Job: works_for TechCorp"  <-- NO name directive
Response: "You work at TechCorp." <-- System used '${USER}' internally for canonical identity, but didn't announce it
```

### Scenario C: User asks something unrelated (SILENT)
```
User: "What's the weather?"
Query: This is realtime, pref_name facts irrelevant
Memory injection: No preference facts at all
Response: "I don't have weather access, but..."  <-- pref_name never mentioned
```

---

## Architectural Implications

### Current (Broken)
```
Extract facts → Store all equally → Inject all into memory → LLM sees everything

Problem: Can't control what LLM sees/uses
```

### Required
```
Extract facts → Tag with "preferred" metadata → 
  Backend: Use preferred facts for canonical identity resolution
  Filter: Query-aware filtering — expose preferences only for preference queries
  Memory: Conditional injection based on query intent
  LLM: Gets both facts but clear guidance on preferred vs. alternate forms
```

---

## Required Changes

### 1. Backend Metadata: Mark Preferred Facts

**File:** `src/api/main.py:/ingest` endpoint

Currently facts have: `confidence`, `fact_class`, `rel_type`, `subject`, `object`, etc.

**Add:** `is_preferred_label` or `is_preferred` flag

```python
# Current:
fact = {
    "subject": "user",
    "rel_type": "pref_name",
    "object": "${USER}",
    "confidence": 1.0,
    "fact_class": "A"
}

# Should be:
fact = {
    "subject": "user",
    "rel_type": "pref_name",
    "object": "${USER}",
    "confidence": 1.0,
    "fact_class": "A",
    "is_preferred_label": True  # <-- New flag
}
```

**Where:** 
- `/ingest` stores facts with this flag
- `/query` returns facts with this flag
- Filter uses flag to decide: expose or silent?

### 2. Backend /query: Return Preference Metadata

**File:** `src/api/main.py:/query` endpoint

Response should indicate which facts are "preferred":

```python
# Current:
{
  "facts": [
    {"subject": "user", "rel_type": "also_known_as", "object": "john"},
    {"subject": "user", "rel_type": "pref_name", "object": "${USER}"}
  ],
  "preferred_names": {"user": "${USER}"}
}

# Should be:
{
  "facts": [
    {"subject": "user", "rel_type": "also_known_as", "object": "john", "is_preferred_label": False},
    {"subject": "user", "rel_type": "pref_name", "object": "${USER}", "is_preferred_label": True}
  ],
  "preferred_names": {"user": "${USER}"},
  "preferred_fact_types": ["pref_name", "is_preferred"]  # <-- Metadata about what's preferred
}
```

### 3. Filter: Query-Aware Preference Exposure

**File:** `openwebui/faultline_tool.py` inlet/Filter class

**Current problem:** Line 1021 skips user's pref_name entirely

**New requirement:** Expose pref_name ONLY when query asks about identity/preferences

```python
# In Filter.inlet(), detect query intent:

_IDENTITY_QUERIES = {
    "what is my name",
    "who am i",
    "how should people call me",
    "what do you call me",
    "my preferred name",
    "my alternate name",
    "what names do i have",
    "who should i introduce myself as",
}

_query_asks_about_identity = any(sig in text.lower() for sig in _IDENTITY_QUERIES)

# Then in memory injection:
if _query_asks_about_identity:
    # Expose preferred facts with explanation
    inject_preference_facts = True
else:
    # Use preferred facts internally for identity resolution, but don't expose them
    inject_preference_facts = False
```

### 4. Memory Block: Conditional Preference Injection

**File:** `openwebui/faultline_tool.py:_build_memory_block()`

```python
def _build_memory_block(
    self, 
    text, 
    facts, 
    preferred_names, 
    canonical_identity,
    entity_attributes,
    is_realtime=False,
    locations=None,
    expose_preferences=False  # <-- NEW PARAMETER
):
    lines = []
    
    # ONLY if user asked about identity/preferences:
    if expose_preferences:
        # Build preference directives
        if preferred_names.get("user"):
            pref = preferred_names.get("user")
            lines.append(f"Preferred name: {pref}")
        
        # Add preference facts from facts list
        pref_facts = [f for f in facts if f.get("rel_type") in ("pref_name", "also_known_as")]
        for f in pref_facts:
            lines.append(f"{f['rel_type']}: {f['object']}")
    
    # Rest of memory building (family, attributes, etc.) — unchanged
    # ... existing code ...
```

### 5. System Prompt: Guide LLM on Preferred Facts

**File:** `openwebui/faultline_tool.py:_TRIPLE_SYSTEM_PROMPT` (or in memory block)

Add guidance:

```python
# In system prompt or as part of memory:
"""
IMPORTANT: If the user has a preferred_name marked in facts, respect it:
- When the user asks "What is my name?", prioritize preferred_name
- When the user asks other questions, use preferred_name internally for identity but don't announce it
- Never volunteer preference information unless asked
- If user has both also_known_as and pref_name, pref_name takes priority when responding about identity
"""
```

---

## Implementation Phases

### Phase 1: Metadata & Backend (Critical) ✓ COMPLETE
- ✓ Add `is_preferred_label` flag to facts at storage time (already in DB schema)
- ✓ Modified `/query` to return `is_preferred_label` metadata in facts list
  - Updated _fetch_user_facts() to SELECT is_preferred_label from facts table
  - Added is_preferred_label to all fact dicts in response
  - Facts table: bool value | Staged facts: false (not applicable)
- Test: Facts have preference metadata ✓

### Phase 2: Filter Query Detection (Critical)
- Detect identity/preference queries
- Conditionally expose based on query intent
- Test: "What is my name?" → exposes pref_name; "What's my job?" → doesn't

### Phase 3: System Prompt Guidance (Important)
- Add instructions for LLM about preferred facts
- Guide LLM to prioritize pref_name over also_known_as when appropriate
- Test: LLM reads facts and uses correct name

### Phase 4: Comprehensive Preferred Fact Support (Future)
- Apply same logic to OTHER preferred facts (pref_pronouns, pref_title, etc.)
- Build general framework: "is_preferred" flag works for all rel_types
- Test: All preferences respected, only exposed when asked

---

## Success Criteria

| Scenario | Expected | Verify |
|----------|----------|--------|
| "What is my name?" | Mentions pref_name explicitly | LLM response inclualice preferred name |
| "What's my job?" | Uses pref_name internally, doesn't mention it | Response shows job, not names |
| "Tell me about myself" | Lists facts including preference | Both also_known_as and pref_name shown, with pref_name marked preferred |
| "Who am I?" | Emphasizes pref_name | "You're ${USER} (also known as John)" |
| General chat | No unsolicited preference announcements | User preferences never mentioned unless asked |

---

## Why This Matters

1. **Privacy:** User preferences aren't exposed to every question
2. **Consent:** User chooses when/how to share preferences
3. **Quality:** Responses are contextually relevant, not cluttered
4. **Respect:** System uses preferred identity internally (canonical) without being preachy
5. **Scalability:** Same pattern works for all "preferred" metadata

---

## Files to Modify

| File | Section | Change | Priority |
|------|---------|--------|----------|
| `src/api/main.py` | `/ingest` | Add `is_preferred_label` flag at fact storage | P1 |
| `src/api/main.py` | `/query` | Return preference metadata with facts | P1 |
| `openwebui/faultline_tool.py` | Filter.inlet | Add `_IDENTITY_QUERIES` detection | P1 |
| `openwebui/faultline_tool.py` | `_build_memory_block()` | Conditional preference injection | P1 |
| `_TRIPLE_SYSTEM_PROMPT` | Instructions | Add guidance on preferred facts | P2 |

---

## Related Issues

- **dBug-021:** GLiNER2 poison removed — extraction now works correctly
- **dBug-022 (original):** Dead-naming bug identified
- **dBug-022 (revised):** Architectural requirement — respect preferred facts, expose selectively

---

## Notes

- This is about **respecting user identity** while respecting their **privacy**
- Internal use (canonical identity resolution) vs. external communication (what to say to user)
- Pattern applies to all "preferred" metadata: pref_pronouns, pref_title, pref_introduction, etc.
- Requires coordination: backend metadata + filter query detection + system prompt guidance
