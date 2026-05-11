# dprompt-48: Third-Party Preference Detection Fix

**Status:** Specification  
**Date:** 2026-05-12  
**Goal:** Fix auto-synthesis of user pref_name when another entity's preference is mentioned in text

## The Problem

When user says: **"Desmonde prefers Des"**

Current behavior:
1. LLM correctly extracts `(desmonde, pref_name, des)`
2. Auto-synthesis also fires on same text: `(user, pref_name, des)` ← WRONG
3. Both edges hit ingest
4. entity_aliases ON CONFLICT overwrites: "des" → user (latest write wins)
5. Desmonde's preferred name becomes unavailable

**Root cause:** Text-based pref_name synthesis (`_extract_preferred_name()`) doesn't check if another entity's name appears **before** the preference signal in the text.

**Pattern that breaks:** `[Named Entity] [prefers/goes by/known as] [Name]`
- "Desmonde prefers Des" → auto-synthesizes for user (WRONG)
- "my son prefers Des" → auto-synthesizes for user (WRONG)
- "she goes by Des" → blocked by pronoun guard ✓ (correct)

## Solution Approach

**Third-party detection:** Before auto-synthesizing user pref_name, check if text contains another entity's name before the preference signal.

If text matches pattern `[Named Entity (capital letter)] ... [pref signal]`, skip user auto-synthesis. Let LLM handle it explicitly.

## Implementation Strategy

**Option A (Recommended):** Pre-check in `_extract_preferred_name()`
```python
def _extract_preferred_name(text: str) -> list[dict]:
    """Extract user pref_name patterns, but guard against third-party mentions."""
    
    # Detect if another entity name appears before pref signal
    # Pattern: [Capital] ... [pref_signal]
    third_party_pattern = r'([A-Z][a-z]+)\s+.{0,50}(prefers?|goes\s+by|known\s+as|prefer[s]?\s+to\s+be\s+called)'
    if re.search(third_party_pattern, text):
        # Another entity mentioned before preference signal
        # Skip user auto-synthesis (let LLM extract explicitly)
        return []
    
    # Proceed with normal user pref_name extraction
    # ... existing code ...
```

**Option B:** LLM-controlled via prompt
- Add to Filter prompt: "If text mentions another entity's preference, extract only that entity's fact. Do NOT infer a preference for the user."
- Simpler but relies on LLM consistency

**Decision:** Option A (safer, structural guard)

## Files to Modify

- `openwebui/faultline_tool.py` — `_extract_preferred_name()` function
  - Add third-party detection before auto-synthesis
  - Return empty list if third-party preference detected
  - Log detection in debug mode

## Test Cases

1. "Desmonde prefers Des" → LLM extracts (desmonde, pref_name, des), NO user synthesis
2. "my son prefers Des" → LLM extracts (son, pref_name, des), NO user synthesis
3. "I prefer Des" → user auto-synthesis fires ✓ (correct)
4. "call me Des" → user auto-synthesis fires ✓ (correct)
5. "she prefers Des" → pronoun guard blocks ✓ (correct)

## Success Criteria

- ✓ No user pref_name synthesized when third-party entity mentioned
- ✓ LLM extracts third-party preferences correctly
- ✓ User self-identification patterns still work
- ✓ Filter tests still pass
- ✓ Live test: "Desmonde prefers Des" → Desmonde's pref_name is "Des", not user's

## Deployment

- Modify Filter only (openwebui/faultline_tool.py)
- No ingest changes
- No database changes
- Deploy to OpenWebUI on pre-prod
- Test with family preference scenario
