# dprompt-48b: Third-Party Preference Detection Fix

**For: deepseek (V4-Pro)**

---

## Task

Fix auto-synthesis of user pref_name when another entity's preference is mentioned. Prevents "Desmonde prefers Des" from overwriting user's preferred name with "Des".

---

## Context

**Bug:** Text "Desmonde prefers Des" triggers TWO edges:
1. LLM: `(desmonde, pref_name, des)` ✓
2. Auto-synthesis: `(user, pref_name, des)` ✗ (WRONG)

entity_aliases ON CONFLICT makes des→user (latest wins). Desmonde's preferred name lost.

**Root cause:** `_extract_preferred_name()` doesn't check if another entity name appears before preference signal.

---

## Constraints

**DO:**
- Add third-party detection to `_extract_preferred_name()` in openwebui/faultline_tool.py
- Pattern: `[Capital Name] ... [pref signal]`
- Return empty list if detected (skip auto-synthesis)
- Test: Desmonde/Des, my son/Des, I/Des scenarios
- Debug logging

**DO NOT:**
- Modify ingest or /query
- Add LLM prompt changes (struct guard better)
- Change database

---

## Sequence

**1. Add pattern to `_extract_preferred_name()`**

```python
def _extract_preferred_name(text: str) -> list[dict]:
    # NEW: Third-party detection
    third_party_pattern = r'([A-Z][a-z]+)\s+.{0,50}(prefers?|goes\s+by|known\s+as|prefer[s]?\s+to\s+be\s+called)'
    if re.search(third_party_pattern, text):
        # Another entity mentioned before pref signal — skip user auto-synthesis
        return []
    
    # EXISTING: Normal user pref_name extraction
    # ... (keep all existing code)
```

**2. Test Cases**

```python
test_cases = [
    ("Desmonde prefers Des", []),  # Third-party → skip
    ("my son prefers Des", []),     # Third-party → skip
    ("I prefer Des", [...]),        # First-person → extract
    ("call me Des", [...]),         # Direct call → extract
    ("she prefers Des", []),        # Pronoun guard blocks (existing)
]
```

**3. Validate**

- Run existing Filter tests (no regressions)
- Test live: ingest "Desmonde prefers Des", verify LLM extraction, NO user synthesis

---

## Deliverable

**File:** `openwebui/faultline_tool.py`
- Modified `_extract_preferred_name()`
- Added third-party detection
- Return empty list if third-party detected

---

## Success

- ✓ "Desmonde prefers Des" → Desmonde gets pref_name, user does NOT
- ✓ User self-ID patterns still work
- ✓ Tests pass

---

## Upon Completion

```markdown
## ✓ DONE: dprompt-48 (Third-Party Preference Detection) — 2026-05-12

**Implementation:**
- Added third-party entity detection to `_extract_preferred_name()`
- Pattern: [Capital Name] + pref signal → skip user auto-synthesis
- Filter tests: all pass ✓

**Test:** "Desmonde prefers Des" → Desmonde pref_name=Des, user unaffected ✓

**Next:** Redeploy Filter + run integration test.
```
