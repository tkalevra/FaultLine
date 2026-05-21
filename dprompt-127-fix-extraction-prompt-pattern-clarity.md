# dprompt-127: Fix Extraction Prompt Pattern Clarity (dBug-062 Root Cause)

**Date:** 2026-05-21  
**Severity:** CRITICAL  
**Related:** dBug-062, dBug-art-false-children  
**Scope:** `src/api/main.py:_build_extraction_prompt()` only (no code logic changes)

---

## Problem Statement

**dBug-062 Root Cause:** LLM extraction returns rel_type **names** as entity **values** because the extraction prompt uses ambiguous placeholders that don't clearly distinguish between three distinct extraction patterns:

1. **Relationship triples** — both subject and object are entity names
2. **Scalar/attribute triples** — object is a literal value (string, number, date)
3. **Identity triples** — both are entity names, but semantic function is aliasing

Current FORMAT line (line 3027):
```
FORMAT: [{"subject":"entity","object":"value","rel_type":"rel_type","definition":"short description"}]
```

**Problem:** `"rel_type":"rel_type"` uses same word as key and value → LLM hallucinates rel_type names into object field → facts become `{"subject":"ChildB","object":"pref_name","rel_type":"pref_name"}` instead of `{"subject":"ChildB","object":"ArtMajor","rel_type":"also_known_as"}`.

---

## Root Cause: Ambiguous Pattern Distinction

The LLM needs to learn THREE **structurally different patterns**:

| Pattern | Subject | Object | Rel_Type | Example |
|---|---|---|---|---|
| **Relationship** | Entity | Entity | relationship_name | `(user, parent_of, child_b)` — user IS parent OF child_b |
| **Scalar/Attribute** | Entity | **VALUE** | attribute_name | `(child_b, age, 19)` — child_b HAS age 19 |
| **Identity** | Entity | Entity (alias) | identity_rel_type | `(child_b, also_known_as, art)` — child_b ALSO known AS art |

**Current prompt conflates all three.** The FORMAT line shows a generic placeholder that doesn't teach the LLM these distinctions.

---

## Solution: Pattern-Based Prompt Clarification

### Phase 1: Rewrite FORMAT Section (Lines 3025-3027)

**BEFORE (Ambiguous):**
```python
base_prompt = """Extract ALL relationships and facts from text. Return ONLY a JSON array. Each triple must have subject, object, rel_type, and definition.

FORMAT: [{"subject":"entity","object":"value","rel_type":"rel_type","definition":"short description"}]
```

**AFTER (Pattern-Based, Domain-Generic):**
```python
base_prompt = """Extract ALL relationships and facts from text. Return ONLY a JSON array. Each triple must have subject, object, rel_type, and definition.

CRITICAL DISTINCTION — Three extraction patterns:

1. RELATIONSHIP (entity → entity):
   - Both subject and object are ENTITY NAMES (person, organization, location, etc.)
   - rel_type describes the RELATIONSHIP (parent_of, works_for, located_in, knows, friend_of, spouse, etc.)
   - Example: {"subject":"alice","object":"bob","rel_type":"parent_of","definition":"alice is parent of bob"}

2. SCALAR/ATTRIBUTE (entity → value):
   - Subject is ENTITY NAME
   - Object is a LITERAL VALUE: number, date, or string (NOT an entity name, NOT a rel_type name)
   - rel_type describes the ATTRIBUTE (age, height, weight, occupation, born_on, nationality, etc.)
   - Example: {"subject":"alice","object":"45","rel_type":"age","definition":"alice is 45 years old"}

3. IDENTITY/ALIAS (entity → entity):
   - Subject is PRIMARY ENTITY NAME
   - Object is an ALIAS or ALTERNATE NAME for same entity (also_known_as, pref_name, same_as)
   - rel_type is ALWAYS one of: pref_name, also_known_as, same_as
   - Example: {"subject":"alice","object":"alice smith","rel_type":"pref_name","definition":"alice prefers to be called alice smith"}

⚠️  COMMON MISTAKE: If rel_type is in the list above (pref_name, also_known_as, age, parent_of), the object field should NEVER contain that rel_type name itself. Example WRONG: {"object":"pref_name"} or {"object":"age"}. The object is the ACTUAL VALUE or ENTITY, not the relationship type.

FORMAT: [{"subject":"entity_name","object":"entity_or_value","rel_type":"relationship_or_attribute_type","definition":"brief description"}]
```

### Phase 2: Update EXTRACT RULES (Lines 3036-3047)

Reorganize by pattern type to reinforce the three-way distinction:

**BEFORE:**
```python
EXTRACT RULES:
1. Identity: pref_name, also_known_as, same_as (pronouns → entities)
2. Entity types: instance_of for EVERY entity (person, location, organization, object, animal, concept, etc.)
3. Hierarchies: For locations, extract nested containment (street→city→state→country)
4. Family kinship (CRITICAL):
   - "My children are Gabby, Des" → (user, parent_of, gabby), (user, parent_of, des)
   - "My son's name is X" → (user, parent_of, x), THEN (x, pref_name, x)
   ...
```

**AFTER (Pattern-Organized):**
```python
EXTRACT RULES (organized by pattern type):

PATTERN 1 — RELATIONSHIPS (entity → entity):
  - Family: parent_of, child_of, spouse, sibling_of
    * "My son is X" → (user, parent_of, x) — X is a separate child entity
    * "My wife is X" → (user, spouse, x) — X is separate entity
    * Do NOT create inverse facts yourself; system handles directionality
  - Work: works_for, educated_at
  - Other: knows, friend_of, met, has_pet, owns, located_in, created_by, related_to
  - Hierarchies: instance_of, subclass_of, part_of, is_a, member_of

PATTERN 2 — SCALARS/ATTRIBUTES (entity → value):
  - Demographics: age, height, weight, nationality, has_gender, born_on, born_in
    * "X is 19" → (x, age, "19") — object is the NUMBER or DATE
    * "X was born in 1990" → (x, born_on, "1990-01-01") — object is the DATE STRING
  - Descriptive: occupation, title
    * "X is an ArtMajor Major" → (x, occupation, "ArtMajor Major") — object is STRING VALUE

PATTERN 3 — IDENTITY/ALIASES (entity → entity):
  - pref_name: entity's preferred display name ("X prefers to be called Y")
  - also_known_as: entity's alternative names or nicknames
  - same_as: entity identity resolution (same person across contexts)
  - CRITICAL: These describe ALIAS RELATIONSHIPS, not entities named "pref_name"

FIRST-PERSON RESOLUTION:
  - "I", "me", "my", "we" → always map to "user" entity
  - NEVER use pronouns as literal entity names

AMBIGUOUS PRONOUNS (he, she, it, they):
  - Resolve from prior context if available
  - Omit if cannot resolve (prevents hallucination)
```

### Phase 3: Metadata-Driven Examples (No Hardcoding)

Instead of hardcoded entity names in the prompt, **dynamically build pattern examples from rel_types table at runtime**:

**Location:** `src/api/main.py:_build_extraction_prompt()` after line 3053

**Code:**
```python
    if db_connection:
        try:
            with db_connection.cursor() as cur:
                # Query rel_types to build pattern examples dynamically
                cur.execute("""
                    SELECT 
                        category,
                        rel_type,
                        natural_language,
                        is_hierarchy_rel,
                        tail_types
                    FROM rel_types
                    WHERE natural_language IS NOT NULL
                    ORDER BY category, rel_type
                    LIMIT 20
                """)
                rel_type_rows = cur.fetchall()
            
            # Build dynamic examples grouped by pattern type
            relationship_examples = []
            scalar_examples = []
            identity_examples = []
            
            for row in rel_type_rows:
                category, rel_type, natural_lang, is_hierarchy, tail_types = row
                
                # Pattern 1: Relationships
                if not is_hierarchy and tail_types != '["SCALAR"]':
                    if len(relationship_examples) < 3:
                        relationship_examples.append(
                            f'  - {rel_type}: {natural_lang}'
                        )
                
                # Pattern 2: Scalars
                if tail_types == '["SCALAR"]':
                    if len(scalar_examples) < 3:
                        scalar_examples.append(
                            f'  - {rel_type}: {natural_lang}'
                        )
                
                # Pattern 3: Identity
                if rel_type in ('pref_name', 'also_known_as', 'same_as'):
                    if len(identity_examples) < 3:
                        identity_examples.append(
                            f'  - {rel_type}: {natural_lang}'
                        )
            
            # Inject examples into prompt
            if relationship_examples:
                base_prompt += "\nDOMAIN-SPECIFIC RELATIONSHIP EXAMPLES:\n"
                base_prompt += "\n".join(relationship_examples) + "\n"
            
            if scalar_examples:
                base_prompt += "\nDOMAIN-SPECIFIC ATTRIBUTE EXAMPLES:\n"
                base_prompt += "\n".join(scalar_examples) + "\n"
            
            if identity_examples:
                base_prompt += "\nIDENTITY REL_TYPE EXAMPLES:\n"
                base_prompt += "\n".join(identity_examples) + "\n"
        
        except Exception as e:
            log.warning("extract_prompt.db_query_failed", error=str(e))
            # Fallback to minimal pattern explanation (no hardcoded names)
```

---

## HARD CONSTRAINTS (NON-NEGOTIABLE)

1. **NO HARDCODED ENTITY NAMES** in the prompt
   - ❌ Do NOT use: "alice", "bob", "child_b", "art", "gabby", "des" (or any real user data)
   - ✅ Do USE: Generic pattern descriptions + dynamically loaded rel_types from DB
   - ✅ Do USE: `natural_language` field from rel_types table

2. **NO BRITTLE HARDCODED REL_TYPE LISTS**
   - ❌ Do NOT hardcode which rel_types are relationships vs scalars vs identity
   - ✅ Do USE: `tail_types`, `is_hierarchy_rel` metadata columns from DB
   - ✅ Do USE: `natural_language` descriptions, not manual examples

3. **METADATA-DRIVEN EVERYTHING**
   - ❌ Do NOT add new rel_types without updating prompt hardcoding
   - ✅ Do USE: Query rel_types table at `/extract/rewrite` startup or per-request
   - ✅ Do USE: `_REL_TYPE_META` cache for performance

4. **PATTERN CLARITY WITHOUT DATA LEAKAGE**
   - ✅ Teach the LLM three PATTERNS (relationship, scalar, identity) using STRUCTURAL DESCRIPTION
   - ✅ Let examples come from metadata, not manual prompting
   - ✅ DO NOT embed user conversation data (names, values) in system prompt

5. **ZERO REGRESSION ON OTHER DOMAINS**
   - ✅ Solution must work for: family, work, location, computer_system, pets, body_parts, arbitrary domains
   - ✅ Prompt must NOT assume domain context (no "parent_of" examples exclusive to family)
   - ✅ Metadata drives domain — not hardcoded prompt

---

## What NOT to Do

❌ **DO NOT:** Hardcode example triples with real names (ChildB, ArtMajor, Alice, Bob, etc.)
❌ **DO NOT:** Add `prompt_example` column to rel_types table and hardcode examples there
❌ **DO NOT:** Create domain-specific extraction prompts (family vs work vs location)
❌ **DO NOT:** Put user data anywhere in the system prompt
❌ **DO NOT:** Hardcode relationships in prompt that could vary by domain
❌ **DO NOT:** Break the existing fallback chain (if DB unavailable, use minimal pattern description, not examples)

---

## Implementation Requirements

### Code Changes
- **File:** `src/api/main.py:_build_extraction_prompt()`
- **Lines affected:** 3025-3127 (entire function body)
- **Approach:** 
  1. Rewrite FORMAT section to explain three patterns structurally
  2. Reorganize EXTRACT RULES by pattern type
  3. Replace hardcoded rel_type lists with dynamic DB queries using tail_types + is_hierarchy_rel metadata
  4. Use `natural_language` field for domain-agnostic descriptions
  5. Keep fallback chain (minimal pattern description if DB unavailable)

### Testing Requirements
1. ✅ Extract "My son ChildB is also known as ArtMajor, age 19" — verify NO rel_type names in object field
2. ✅ Extract across domains: family, work, location, computer_system — all produce correct patterns
3. ✅ Verify no user data in logs/prompt
4. ✅ Verify fallback works (DB unavailable → minimal pattern prompt)
5. ✅ Verify LLM output distinguishes:
   - Relationships: `(entity, rel_type, entity)`
   - Scalars: `(entity, rel_type, value_string_or_number)`
   - Identity: `(entity, identity_rel, alias)`

### No Rebuild Required
- Function-only change
- Request user to simply update the filter via UI or redeploy container

---

## Conclusion

**Root Cause:** Ambiguous FORMAT placeholder + unstructured rel_type listing confused LLM about pattern distinctions.

**Fix:** Replace hardcoded examples with **structural pattern explanations** + **metadata-driven rel_type guidance** from DB.

**Result:**
- ✅ Works for ANY domain (not just family)
- ✅ Scales automatically when new rel_types added
- ✅ No user data in code
- ✅ Robust, metadata-driven, non-brittle
- ✅ Fixes dBug-062 (rel_type hallucination) and dBug-art-false-children (false entity creation)

---

## Files Affected

| File | Change | Reason |
|---|---|---|
| `src/api/main.py:3017-3127` | Rewrite `_build_extraction_prompt()` | Fix prompt clarity + add metadata-driven examples |
| `BUGS/dBug-062-*.md` | Mark RESOLVED | Root cause fixed, solution verified |

---

## Deployment Notes

- **Timing:** Ready to implement immediately (prompt-only change)
- **Risk:** LOW (pattern clarification only, no logic changes)
- **Verification:** Test with problematic message from dBug-062 bug report
- **Rollback:** Revert to previous `_build_extraction_prompt()` if issues arise
