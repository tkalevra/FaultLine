# dprompt-18 — Birthday Relevance: Add Age-Related Terms to Sensitivity Gate

## Problem

User provides birthday ("I was born on May 3"), then asks "how old am I?" in a new chat. System says "I don't know."

**Root cause:** `born_on` facts are filtered by relevance scoring due to sensitivity penalty.

**Why:** `born_on` is in `_SENSITIVE_RELS` (birthplace, address, weight, etc.), which applies a -0.5 confidence penalty unless the query contains an explicit term from `_SENSITIVE_TERMS`.

Current `_SENSITIVE_TERMS`: `{"born", "birth", "live", "address", "height", "weight", "birthplace", "tall", "how tall", "heavy", "how heavy"}`

User's query: `"how old am I?"` → No match in `_SENSITIVE_TERMS` → penalty applies → score drops below 0.4 → fact filtered out.

## Fix

**File:** `src/api/main.py`, `/query` endpoint (around line 1698)

**Change:**
```python
# BEFORE
_SENSITIVE_TERMS = {"born", "birth", "live", "address", "height", "weight", "birthplace", "tall", "how tall", "heavy", "how heavy"}

# AFTER
_SENSITIVE_TERMS = {"born", "birth", "live", "address", "height", "weight", "birthplace", "tall", "how tall", "heavy", "how heavy", "old", "age", "how old"}
```

Add three terms: `"old"`, `"age"`, `"how old"` so age-related queries don't trigger the sensitivity penalty on `born_on` facts.

## Test

1. New chat: "I was born on May 3, 1990"
2. New chat: "How old am I?"
3. Expected: System returns birthday fact, calculates age correctly (not "I don't know")

## Rationale

`born_on` sensitivity exists to prevent unsolicited sharing of birth dates in casual conversation. But when the user explicitly asks about age/how old, that's a clear signal they want the birth date retrieved. The penalty should not apply.

Similar pattern: `lives_at` (sensitive) shouldn't apply penalty when query contains "where do I live" or "my address".

This fix aligns sensitivity gate behavior with user intent.
