# dBug-023-deepseek: Entity Fragmentation Root Cause & Fix

**Status:** FIX IMPLEMENTED (dprompt-23), AWAITING VERIFICATION

**Parent:** dBug-023-extraction-entity-fragmentation.md (CLAUDE-23)

## Root Cause Analysis

### The Gap: "Filter is dumb" architecture + LLM prompt mismatch

**Architecture context (v1.3.0):**
Filter no longer performs LLM extraction. The comment at `faultline_tool.py:1580` reads:
```python
# Filter is dumb — just send text. /ingest handles LLM extraction, WGM validation, etc.
raw_triples = []
```
Filter sends raw text + empty `edges` to POST `/ingest`. All extraction happens server-side.

**The `/ingest` pipeline when edges are empty:**
1. GLiNER2 runs for entity typing (`/extract` endpoint logic)
2. `/extract/rewrite` is called internally for LLM triple extraction
3. LLM output triples are converted to `EdgeInput` objects with no pronoun normalization

**The prompt problem:**
`/extract/rewrite` (`main.py:1672`) system prompt example:
```
For "I live at <address>, <city>, <state>":
- (I, lives_at, <address>)
```

The LLM follows examples literally and outputs `subject: "i"` (lowercased).

**The resolution gap:**
`main.py:2270` — `registry.resolve(req.user_id, "i")` — "i" is not "user", not a known alias → creates UUID v5 surrogate. This is the orphan.

**Why Filter's pronoun filter didn't help:**
`faultline_tool.py:1625` has `_PRONOUNS = {"i", "me", ...}` filtering, but it applies to `raw_triples` which is always `[]` in the current Filter. Dead code — the filtering happens too late (post-extraction in Filter) after extraction was moved to `/ingest`.

### Flow trace: "I live at 156 Cedar Street S"

```
OpenWebUI user message
  → Filter inlet (faultline_tool.py:1580): raw_triples = [], sends text+empty edges to /ingest
    → /ingest (main.py:2012): no edges → calls GLiNER2 + /extract/rewrite
      → /extract/rewrite (main.py:1672): LLM prompt shows "(I, lives_at, ...)" example
        → LLM outputs: {"subject": "i", "object": "156 cedar street s", "rel_type": "lives_at"}
      ← /ingest (main.py:2080): EdgeInput(subject="i") — no pronoun check
        → registry.resolve("i") (main.py:2270): creates UUID a91f8c22 ← ORPHAN
```

## Fix Applied (dprompt-23)

### Fix 1: `/extract/rewrite` system prompt (`main.py:1672`)
- Changed example: `(I, lives_at, address)` → `(user, lives_at, address)`
- Added explicit rule: "For first-person statements, ALWAYS use 'user' as subject — never 'I', 'me', 'my', or 'we'."

### Fix 2: `/ingest` pronoun normalization guard (`main.py:2063`)
```python
_FIRST_PERSON_PRONOUNS = {"i", "me", "my", "myself", "we", "us", "our", "ourselves"}
for t in rewrite_data.get("triples", []):
    subj = (t.get("subject") or "").lower().strip()
    if subj in _FIRST_PERSON_PRONOUNS:
        t["subject"] = "user"
```
Inserted after `rewrite_data = response.json()`, before `EdgeInput` construction. Catches any pronoun that survives the prompt fix regardless of source (LLM, GLiNER2, future external extractors).

### Defense in depth:
- **Layer 1 (prompt):** LLM instructed to use "user". Corrects the statistical tendency.
- **Layer 2 (normalizer):** Pronoun→user mapping as safety net. Deterministic, zero-cost.

## Verification Plan

1. Ingest "I live at 156 Cedar Street S" for user 10d7d879
2. Query facts table: `SELECT subject_id, rel_type, object_id FROM facts WHERE user_id = '10d7d879...' AND rel_type = 'lives_at'`
3. Assert: `subject_id` = canonical user UUID (10d7d879), not a newly created orphan
4. Query "where do I live?" — assert lives_at fact is visible in /query response

## Files Changed

- `src/api/main.py` — `/extract/rewrite` system prompt + `/ingest` pronoun normalizer
