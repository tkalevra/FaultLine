# dprompt-19 — Pet Extraction/Ingest Failure Investigation

## Problem

User reported pet information to the system. Extraction or ingest failed (unclear which layer).

## Investigation

**Test scenario:** Provide pet information in a statement. Examples:
- "I have a cat named Whiskers"
- "My dog is named Max, he's a golden retriever"
- "We have 2 birds, a parrot named Polly and a budgie named Tweety"

**Check these layers:**

1. **Compound extractor (`src/extraction/compound.py`):**
   - Does `_CHILDREN_CLAUSE` pattern (lines 94-97) also need to match pet clauses?
   - Current: `"(?:we\s+have|have)\s+(?:\d+\s+)?(?:children|kids)"`
   - Pets use similar pattern: "we have a cat", "we have 2 birds"
   - Check: Does compound.py extract `has_pet` edges at all?
   - Look for pet-specific patterns in compound.py

2. **Ingest validation (`src/api/main.py`):**
   - If compound extracts `has_pet(user, cat)`, does ingest process it?
   - Check: Is `has_pet` in the ontology (rel_types table)?
   - Check: Are pet entities being created with correct types (Animal)?
   - Check: Is `object_id` being resolved to UUID or stored as string?

3. **Filter extraction (`openwebui/faultline_tool.py`):**
   - Does the LLM prompt allow `has_pet` as a rel_type?
   - Does filter extract pet facts at all, or does it rely on compound?

4. **Entity type classification:**
   - Per CLAUDE.md, has_pet → object MUST be Animal type
   - Check: Are pet entities being classified as Animal?
   - Check: Descriptor extraction for species, breed, color (lines 507-540 in main.py)

## Root cause candidates

- Compound extractor doesn't handle pet patterns (only children)
- `has_pet` not in ontology
- Pet object entities aren't being classified as Animal → type constraint violation
- Descriptor extraction failing (species extraction from "golden retriever")

## Fix

1. Add pet pattern detection to compound extractor (similar to children clause)
2. Ensure `has_pet` exists in rel_types with head_type=Person, tail_type=Animal
3. Verify pet entity type classification (Animal) before ingest
4. Test end-to-end: "I have a cat named Whiskers" → `has_pet(user, whiskers)` with whiskers:Animal

## Test

New chat: "I have a cat named Whiskers"
Expected: 
- `has_pet(user, whiskers)` fact created
- `whiskers` entity created with type=Animal
- When queried "tell me about my pets", should return Whiskers

Report:
- Where did extraction/ingest fail?
- What was the error?
- What needs to be fixed?
