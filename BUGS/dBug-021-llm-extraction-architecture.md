# dBug-021: LLM Extraction Architecture — Hardcoded Regex Bypassing Ontology Validation

**Status:** PENDING DEEPSEEK REVIEW  
**Severity:** HIGH — Architectural violation, data quality degradation  
**Date Found:** 2026-05-14  
**Reporter:** John  

---

## Summary

The Filter (openwebui/faultline_tool.py) contains ~100 lines of hardcoded regex patterns that extract facts and OVERRIDE the LLM output, completely bypassing the WGMValidationGate and ontology system. This violates the core principle: **"Filter is dumb, backend is smart."** The system should extract facts through LLM, validate through ontology, not extract through regex patterns.

**The core violation:** Hardcoded regex patterns for identity/preference/relationship facts that:
1. Inject edges directly without WGM validation
2. Take priority over LLM extraction (regex augmentation)
3. Prevent ontology metadata from governing rel_type validity
4. Accumulate unvalidated facts instead of librarian-matched facts

---

## Root Cause Chain

### Layer 1: LLM Confusion (typed_entities Injection)

**Location:** `openwebui/faultline_tool.py:273-287` (rewrite_to_triples function)

```python
if typed_entities:
    entity_lines = "\n".join(
        f"- {e.get('subject')} (type: {e.get('subject_type', 'unknown')})"
        f" {e.get('rel_type')} {e.get('object')} (type: {e.get('object_type', 'unknown')})"
        for e in typed_entities
        if e.get("subject") and e.get("object")
    )
    user_content = (
        f"{text}\n\n"
        f"GLiNER2 has pre-classified these entities from the text:\n{entity_lines}\n"
        f"Use these entity types to guide relationship selection..."
    )
```

**Problem:** 
- typed_entities are injected as TEXT containing pre-classified edges
- Example: User says "I prefer to be called ${USER}"
  - GLiNER2 extracts: john (Person), ${USER} (Person)
  - Filter injects as: "john (Person) [rel_type] ${USER} (Person)"
  - LLM reads this as a REFERENCE edge already extracted
  - LLM gets confused: "Is this what I should match against? Should I invert it? Is this the subject or object?"
- Result: LLM returns wrong rel_types (e.g., "met" instead of "pref_name"), bac${LOCATION}ards relationships

### Layer 2: Regex Augmentation Workaround

**Location:** `openwebui/faultline_tool.py:1435-1471` (Filter inlet flow)

Instead of fixing the LLM confusion, a regex augmentation was added:

```python
_augment_edges = [
    e for e in basic_edges
    if e.get("is_correction") or e.get("rel_type") == "pref_name"
]
# Merge: existing triples + augment edges
for aug in _augment_edges:
    _key = (aug["subject"], aug["object"], aug["rel_type"])
    if _key not in _existing_keys:
        raw_triples.append(aug)
```

**The comment admits it:**
> "The LLM may fail to extract pref_name correctly (e.g., "call me X" → met). Regex pref_name extraction is explicit and unambiguous — take priority."

**This is a band-aid, not a fix.** It:
1. Doesn't solve the LLM confusion
2. Creates two parallel extraction systems (LLM + regex)
3. Hardcoalice specific rel_types (pref_name, correction)
4. Won't scale to other rel_types that have extraction issues

### Layer 3: Hardcoded Fact Patterns

**Location:** `openwebui/faultline_tool.py:330-433` (_extract_basic_facts function)

~100 lines of hardcoded regex patterns:

```python
_ID_PATTERNS = [
    (re.compile(r"\bmy\s+name\s+is\s+([a-zA-Z]+)", re.IGNORECASE), "also_known_as"),
    (re.compile(r"\bcall\s+me\s+([a-zA-Z]+)", re.IGNORECASE), "pref_name"),
    ...
]

_RELATIONSHIP_PATTERNS = [
    (re.compile(r"\bmy\s+wife'?s?\s+(?:name\s+)?is\s+([a-zA-Z]+)", re.IGNORECASE), "spouse"),
    (re.compile(r"\bmy\s+child'?s?\s+(?:name\s+)?is\s+([a-zA-Z]+)", re.IGNORECASE), "child_of"),
    ...
]

_PREF_PATTERNS = [
    re.compile(r"...\bprefers?\s+to\s+be\s+called\s+([a-zA-Z]+)", re.IGNORECASE),
    re.compile(r"...\bgoes\s+by\s+([a-zA-Z]+)", re.IGNORECASE),
    ...
]
```

**The problem:**
- These are SUPPOSED to be "lightweight fallback when the LLM is unavailable"
- But they're actually used as OVERRIDES (regex augmentation takes priority)
- Each pattern hardcoalice a specific rel_type mapping (e.g., "my wife" → spouse)
- New patterns needed for every linguistic variation ("prefer to be called", "goes by", "known as")
- No ontology validation: regex says it's a pref_name, no metadata check on whether this is valid

---

## Why This Violates Architecture

### CLAUDE.md Principle: "Filter is dumb, backend is smart"

**What should happen:**
```
Filter (dumb): Extract via LLM → Pass to backend
Backend (smart): Validate against ontology → Apply type constraints → Classify A/B/C
Result: Facts validated through metadata-driven WGM gate
```

**What's happening:**
```
Filter (smart): LLM extraction → Regex augmentation → Hardcoded rel_type → Direct inject
Backend: Receives pre-classified "edges" from Filter → WGM gate just validates JSON shape
Result: Facts bypass semantic validation, ontology metadata unused
```

### Missing Librarian Approach

The user said: **"Extract be a librarian, match on each layer to provide validly to the user."**

Current approach:
- Extract via pattern matching (regex) or LLM (confused)
- Merge results ad-hoc
- Pass to backend as-is

Librarian approach:
- Extract candidate edges via LLM (clean, consistent)
- Check each edge against ontology layers:
  1. Is this rel_type valid? (check `rel_types` table)
  2. Is subject entity type compatible? (check `rel_types.head_types`)
  3. Is object entity type compatible? (check `rel_types.tail_types`)
  4. Does this fit the hierarchy? (walk `_REL_TYPE_HIERARCHY`)
- If validation fails: LLM is called to expand/clarify
- Only commit facts that match the knowledge graph schema

---

## Impact: Data Quality Degradation

### Example: "I prefer to be called ${USER}"

**Current flow:**
1. User: "My name is John, I prefer to be called ${USER}"
2. GLiNER2 extracts: entities (john, ${USER})
3. LLM receives typed_entities context: "john (Person) [?] ${USER} (Person)"
4. LLM gets confused, returns rel_type="met" (wrong)
5. Regex sees "prefer to be called" pattern, extracts pref_name="${USER}"
6. Regex result OVERRIDES LLM, submitted to backend
7. Backend validates: pref_name object must be STRING ✓ (${USER} is lowercase)
8. Fact committed: (user, pref_name, ${USER}) ✓ — Correct, but only by accident

**Problem:** System worked, but only because regex caught it. LLM was broken the whole time.

---

## What Needs to Happen

### Phase 1: Remove Regex Garbage (Deepseek Review)

1. **Analyze** the `_TRIPLE_SYSTEM_PROMPT` (lines 106-192): Is it comprehensive for all rel_types?
2. **Identify** why LLM is confused: Is typed_entities injection the root cause?
3. **Test** LLM extraction WITHOUT typed_entities context injection
4. **Document** findings: Which rel_types are still problematic? Does LLM perform better without context?

### Phase 2: Fix LLM Context (Code Change)

1. **Don't inject typed_entities as text** — pass them separately or use for backend validation only
2. **Let LLM extract independently** without pre-classified edge context
3. **Backend validates the extracted edges** against ontology (not Filter)

### Phase 3: Ontology-Based Validation (Backend Enhancement)

Currently in src/api/main.py `/ingest` endpoint:
1. Receive edges from Filter
2. Query `rel_types` metadata for each edge
3. Validate subject/object types against `head_types` / `tail_types`
4. Walk hierarchy to ensure edge fits schema
5. **If validation fails:** Call LLM to expand/reinterpret (not hardcoded fallback)
6. Only commit if validated

### Phase 4: Remove Hardcoded Regex (Cleanup)

Once LLM + ontology validation is working:
- Remove `_extract_basic_facts()` function entirely
- Remove regex augmentation logic (lines 1435-1471)
- Filter becomes truly dumb: just calls LLM, passes output to backend

---

## Expected Flow (Post-Fix)

```
User message: "My name is John, I prefer to be called ${USER}"
↓
Filter inlet:
  1. Call LLM (clean prompt, NO typed_entities as text)
  2. LLM extracts: [(user, also_known_as, john), (user, pref_name, ${USER})]
  3. Pass to /ingest
  4. NO regex augmentation
  5. NO hardcoded patterns
  6. Just clean LLM output

Backend /ingest:
  1. Validate edge 1: (user, also_known_as, john)
     - Check rel_types: also_known_as exists ✓
     - Check head_types: SCALAR rel_type ✓
     - Check object: STRING "john" ✓
     - Commit: Class A
  2. Validate edge 2: (user, pref_name, ${USER})
     - Check rel_types: pref_name exists ✓
     - Check head_types: SCALAR rel_type ✓
     - Check object: STRING "${USER}" ✓
     - Commit: Class A
  3. WGM gate returns VALID ✓
  4. Both facts stored
  5. No garbage, no regex workarounds

Filter outlet:
  1. Retrieve facts: preferred_names = {user: "${USER}"}
  2. Inject memory: "I know you prefer to be called ${USER}"
  3. Clean, validated knowledge graph
```

---

## Files Involved

| File | Issue | Action |
|------|-------|--------|
| `openwebui/faultline_tool.py:273-287` | typed_entities injected as text | Remove/restructure |
| `openwebui/faultline_tool.py:330-433` | _extract_basic_facts() hardcoded regex | Remove entirely |
| `openwebui/faultline_tool.py:1435-1471` | Regex augmentation workaround | Remove entirely |
| `src/api/main.py:/ingest` | WGM validation gate | Enhance with LLM fallback |
| `_TRIPLE_SYSTEM_PROMPT` | LLM extraction instruction | Review/improve |

---

## Deepseek Investigation Scope

**STOP before any code changes.** Validate the approach through systematic investigation:

---

## Investigation 1: What Does GLiNER2 Actually Extract?

**Objective:** Understand what typed_entities contains before it's injected into LLM prompt.

**Command:** SSH into pre-prod and inspect GLiNER2 extraction logs
```bash
ssh docker-host -x "sudo docker logs faultline-wgm 2>&1 | grep -i 'gliNER2\|typed_entities\|extract_json' | tail -50"
```

**What to look for:**
- What entities does GLiNER2 extract from "I prefer to be called ${USER}"?
- Does it return (john, Person), (${USER}, Person) separately?
- Or does it try to extract a relationship (john → ${USER})?
- Sample log output: `GLiNER2 extracted: {...}` or similar

**Report back:**
- Raw GLiNER2 output for the test phrase
- Entity types assigned (Person/Concept/etc)
- Whether relationships are pre-extracted or just entities

---

## Investigation 2: What Does LLM Return WITH typed_entities Context?

**Objective:** Verify that LLM extraction is currently broken (returns "met" instead of "pref_name").

**Setup:** Use the pre-prod bearer token and make a direct extraction call

**Command:** Test current extraction with typed_entities context
```bash
BEARER="sk-1cf72f713e884a06b3dab80a8a003669"
curl -X POST http://localhost:8001/extract \
  -H "Authorization: Bearer $BEARER" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "My name is John, I prefer to be called ${USER}",
    "source": "test_investigation",
    "user_id": "test-user"
  }' | jq '.edges[] | {subject, object, rel_type}'
```

**Expected (current broken):**
- Some edges might have rel_type="met" or other wrong types for pref_name
- Or subject/object reversed

**Report back:**
- Full extraction output (all edges returned)
- Which edges are wrong? Which rel_types incorrect?
- Confirm: does pref_name extraction fail with current code?

---

## Investigation 3: What Does LLM Return WITHOUT typed_entities Context?

**Objective:** Test the hypothesis that removing typed_entities injection fixes LLM extraction.

**Method:** Patch the Filter locally to test

**Step 1:** SSH into pre-prod and create a test script
```bash
ssh docker-host -x "cat > /tmp/test_llm_extraction.py << 'EOF'
# Simplified test of rewrite_to_triples without typed_entities
# Uses the actual LLM endpoint from pre-prod

import httpx
import json

# LLM endpoint (from pre-prod config)
llm_url = 'http://localhost:11434/v1/chat/completions'  # or OpenWebUI internal

# Simplified prompt (just use the system prompt, no entity context)
messages = [
    {
        'role': 'system',
        'content': '''You are a relationship fact extractor for a personal knowledge graph.
Output ONLY a raw JSON array. No markdown, no explanation, no code fences.

REL_TYPE REFERENCE:
- pref_name: explicitly preferred name ("goes by", "prefers to be called", "preferred name is")
- also_known_as: nickname or alternate name

Output: [{"subject":"...","object":"...","rel_type":"..."}]
If nothing to extract: []'''
    },
    {
        'role': 'user',
        'content': 'My name is John, I prefer to be called ${USER}'
    }
]

response = httpx.post(llm_url, json={
    'model': 'qwen/qwen3.5-9b',
    'messages': messages,
    'temperature': 0.0,
    'max_tokens': 400
})

result = response.json()['choices'][0]['message']['content'].strip()
print('LLM output without typed_entities context:')
print(result)
try:
    edges = json.loads(result)
    for e in edges:
        print(f"  - {e.get('subject')} -> {e.get('rel_type')} -> {e.get('object')}")
except:
    print('Failed to parse JSON')
EOF
python /tmp/test_llm_extraction.py
"
```

**Report back:**
- Does LLM return pref_name (correct) or met/other (wrong)?
- Are both also_known_as AND pref_name returned, or just one?
- Subject/object in correct order?
- Compare with Investigation 2: is extraction better without typed_entities?

---

## Investigation 4: Where Do typed_entities Come From in Code Flow?

**Objective:** Trace how typed_entities gets populated and injected.

**Command:** Grep for typed_entities in the codebase
```bash
cd /home/${USER}/Documents/013-GIT/FaultLine-dev
grep -n "typed_entities" openwebui/faultline_tool.py | head -20
```

**Look for:**
- Line 1417: `typed_entities = await self._fetch_entities(...)`
- What does `_fetch_entities()` return? (calls `/extract` endpoint)
- Lines 1428: passed to `rewrite_to_triples(..., typed_entities=typed_entities)`
- Lines 274-287: injected as text context

**Report back:**
- Flow chain: where typed_entities comes from → what it contains → how it's used
- Does _fetch_entities() call FaultLine's /extract endpoint? Or does it use GLiNER2 directly?
- Are GLiNER2's raw entity extractions being formatted as edges with rel_types?

---

## Investigation 5: WGM Gate Current Capabilities

**Objective:** Understand what validation the backend ALREADY does vs what needs enhancement.

**Command:** Inspect the WGMValidationGate
```bash
cd /home/${USER}/Documents/013-GIT/FaultLine-dev
grep -A 50 "class WGMValidationGate" src/wgm/gate.py | head -60
grep -n "validate\|rel_type\|head_types\|tail_types" src/api/main.py | grep -A 3 -B 3 "ingest"
```

**Look for:**
- What validation already exists in WGMValidationGate?
- Does it check `rel_types` table metadata?
- Does it validate head_types/tail_types?
- Where would LLM fallback need to be inserted?

**Report back:**
- Current validation checks (list them)
- What metadata is available for validation (rel_types columns?)
- Feasibility of adding LLM clarification on validation failures

---

## Investigation 6: LLM Prompt Completeness

**Objective:** Verify the `_TRIPLE_SYSTEM_PROMPT` is comprehensive for all rel_types.

**Command:** Check what rel_types the prompt mentions
```bash
cd /home/${USER}/Documents/013-GIT/FaultLine-dev
sed -n '106,192p' openwebui/faultline_tool.py | grep -i 'rel_type\|pref_name\|also_known_as'
```

**Look for:**
- Is pref_name mentioned explicitly? How is it alicecribed?
- Are all major rel_types from CLAUDE.md mentioned?
- Any guidance on entity types (Person vs Animal vs Concept)?
- Are hierarchy relationships (instance_of, subclass_of) mentioned?

**Report back:**
- List rel_types mentioned in prompt
- Which rel_types from ontology are NOT mentioned?
- Gaps in the instruction set?

---

## Summary Report for Scratch

When complete, update scratch.md with:

**Investigation Results:**
1. GLiNER2 extraction output (what entities/types extracted?)
2. LLM performance WITH typed_entities (current extraction quality)
3. LLM performance WITHOUT typed_entities (hypothesis validation)
4. typed_entities code flow (where does it come from, what is it)
5. WGM gate current capabilities (what validation exists?)
6. LLM prompt gaps (what rel_types are missing?)

**Findings Summary:**
- Is typed_entities injection the root cause? (Y/N, based on test 3 vs test 2)
- Can WGM gate be enhanced for LLM fallback? (Y/N, based on investigation 5)
- What's the minimum scope of change? (lines to remove? what to enhance?)

**Recommendation:**
- Proceed with Phase 2 (remove typed_entities context)? Y/N
- Proceed with Phase 3 (enhance WGM validation)? Y/N
- Any blockers or unforeseen dependencies?

---

## References

- CLAUDE.md: "Filter is dumb, backend is smart"
- CLAUDE.md: Metadata-driven validation via rel_types table
- scratch.md: Established workflow (analyze → report → review → prompt → execute)
- Memory: extraction-robustness-analysis.md (prior UUID compliance analysis)
