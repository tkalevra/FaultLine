# dBug-039: UUID Leakage to User — Symptomatic of Staged Fact Promotion Failure

**Status:** CLOSED (Fixed & Deployed 2026-05-17)  
**Severity:** Critical (CLAUDE.md constraint violation)  
**Reported:** 2026-05-16 (post-testing)  
**System:** Production (FaultLine main)  
**Affected User:** 10d7d879-63cd-4f31-92ce-f2c9edb760ab (test user)
**Deployment:** Commit 3be04a5 (prod main)

---

## Problem Statement

User receives chat responses with **raw UUIDs exposed** instead of display names:

```
User: "My son alice has a pet corn snake named Sophia"

LLM: "I have noted that your son alice (preferred name identifier: 2E0D4A79-9E76-5288-Adc9-2A0D5E9Be7F7) has a pet corn snake named Sophia."
```

**CLAUDE.md Constraint Violation:**
> "LLM injection responses MUST be plain English" — NOT raw UUIDs or machine-readable identifiers. Display names must never be UUIDs in user-facing output.

---

## Root Cause Analysis: Three Nested Failures

### Failure 1: UUID Redaction Regex is Case-Sensitive (Surface Issue)

**File:** `openwebui/faultline_function.py` line 678

```python
_UUID_ANYWHERE_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
```

**Problem:** Regex only matches lowercase hex `[0-9a-f]`, but UUIDs can be mixed/uppercase.

**Evidence:**
- Database stores UUIDs in lowercase: `2e0d4a79-9e76-5288-adc9-2a0d5e9be7f7`
- LLM output shows mixed case: `2E0D4A79-9E76-5288-Adc9-2A0D5E9Be7F7`
- Regex `[0-9a-f]` fails to match uppercase `E`, `A`, `D`, `C`, `B` → **UUID NOT REDACTED**

### Failure 2: Entity Attributes Block Falls Back to UUID

**File:** `openwebui/faultline_function.py` line 1304

```python
display_name = uuid_to_display.get(entity_id, entity_id.title())
```

**Problem:** When UUID not in `uuid_to_display` mapping, fallback displays raw UUID with `.title()` formatting.

**Impact:** Any entity without a mapped display name leaks UUID to LLM injection block.

### Failure 3: Facts Stuck in Staging, Never Promoted (Root Issue)

**Problem:** Class B facts require `confirmed_count >= 3` to be promoted from `staged_facts` to `facts` table. Facts with confidence 0.8 (LLM-inferred) were being staged indefinitely without promotion.

**Why:** Pet facts like `(alice, has_pet, sophia)` extracted with confidence 0.8 → classified as Class B → staged, not committed → blocked until 3 confirmations.

---

## Impact Assessment

| Failure | Impact | Severity |
|---------|--------|----------|
| **Case-sensitive UUID regex** | UUIDs leak to user in responses | Critical |
| **Entity attributes UUID fallback** | Unmapped entities expose raw UUID | Critical |
| **Facts stuck in staging** | User-provided pet facts invisible in /query results | High |

**User Experience:**
- User mentions pets → facts staged, not committed
- Query returns facts from memory injection with UUIDs
- LLM sees raw UUIDs → echoes them back
- UUID redaction fails → user sees identifiers

---

## Resolution (2026-05-17)

### Fixes Deployed

**1. Case-Insensitive UUID Redaction (openwebui/faultline_function.py, line 678)**
```python
_UUID_ANYWHERE_RE = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
```
- Changed `[0-9a-f]` to `[0-9a-fA-F]` to match uppercase/mixed case
- Catches all UUID variations (2E0D4A79, 2e0d4a79, 2e0d4A79, etc.)
- Defense-in-depth: redacts any UUIDs that slip through

**2. Never Display UUID in Entity Attributes (openwebui/faultline_function.py, lines 1304-1315)**
```python
# Get display name — never show raw UUID (dBug-039)
display_name = uuid_to_display.get(entity_id)
if not display_name:
    # Fall back to pref_name from attributes
    pref_name_attr = attrs.get("pref_name")
    if isinstance(pref_name_attr, dict):
        display_name = pref_name_attr.get("value")
    else:
        display_name = pref_name_attr
    # Skip entity if no display name available (never show UUID)
    if not display_name:
        continue
```
- Tries uuid_to_display mapping first
- Falls back to pref_name from entity attributes
- **Skips entity entirely if no display name** (never shows UUID)

**3. Confidence-Based Class A Routing (src/api/main.py, lines 452-458)**
```python
# If confidence >= 0.9, bypass staging → Class A immediate commit
if confidence >= 0.9:
    return ("A", current_confidence)
```
- User-provided facts (confidence 0.95) bypass staging
- Pet facts `(alice, has_pet, sophia)` → Class A immediately
- No waiting for 3 confirmations

**4. Metadata-Driven Directness Assessment (src/api/main.py)**
- Replaced hardcoded IDENTITY_STRUCTURAL patterns
- Simple check: if both subject and object in req_text → confidence 0.95
- Works for ANY rel_type, scalable with ontology growth

### Test Results
- ✅ has_pet facts: facts table (Class A), not staged_facts
- ✅ Confidence: 0.95→1.0 (user-provided signal preserved)
- ✅ UUID leakage: ZERO (no UUIDs in LLM responses)
- ✅ Display names: All entity attributes show proper names (alice, sophia, emma, etc.)
- ✅ No UUID patterns in full pipeline test

### CLAUDE.md Compliance
- ✅ "LLM injection MUST be plain English" — enforced at source
- ✅ "Entity ID vs Display Name" — semantic distinction preserved
- ✅ "Strong Ingest/Dumb Extract" — metadata-driven, no hardcoding

### Deployment Steps
1. **Dev consolidation:** Cherry-picked dBug-039 fixes onto dBug-026 entity validation gate
2. **Code changes:**
   - src/api/main.py: Confidence-based routing + metadata-driven directness
   - openwebui/faultline_function.py: UUID regex fix + entity attributes prevention
3. **Full pipeline test:** Passed (no UUID leakage, pet facts understood correctly)
4. **Production push:** Commit 3be04a5 to FaultLine main branch

### Commits
- **Dev (master):** 
  - 73d0e79: Case-insensitive UUID redaction + entity attributes fix
  - 728a212: Confidence-based Class A routing
- **Prod (main):** 3be04a5 fix: UUID leakage + entity validation gate (dBug-039, dBug-026, dprompt-100)

---

## References

- **CLAUDE.md:** 
  - Line: "LLM injection responses MUST be plain English"
  - Line: "Entity ID vs Display Name: Semantic Distinction"
- **Code Files:**
  - openwebui/faultline_function.py (lines 678, 1304-1315)
  - src/api/main.py (lines 452-458, confidence-based routing)
- **Related Bugs:**
  - dBug-026: Entity registry pollution (fixed simultaneously)
  - dBug-027: pref_name validation (fixed simultaneously)
