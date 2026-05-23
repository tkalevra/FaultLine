# dBug-025: Entity Duplication Blocks Taxonomy Filtering — Query Returns Unfiltered Noise

**Severity:** High — taxonomy-aware queries return mixed entity types, defeating PII filtering

**Status:** CONFIRMED (validated via pre-prod database inspection 2026-05-15)

**User Context:** User, user_id=`<user_uuid>`

## Problem Summary

User query: `"Tell me about my family"`

Expected response: Only Person + Animal entities (<child_name>, <child_name>, <child_name>, <spouse>, ${ENTITY})

Actual response: Mixed entities including:
- Addresses (<address>) — Location type
- Servers (${ENTITY}.<domain>, titan.<domain>) — unknown type, no filtering possible
- Garbage entities (computer, domain_name, son, it) — type labels and pronouns mistaken for entities
- Duplicate/orphaned string-ID entities (<child_name>, <child_name>, <child_name>) — entity_type=unknown

Response feels "stalkerish" because PII leaks and infrastructure noise mixed with family relationships.

## Root Cause: Entity Duplication at Write Time

**Every real-world entity exists as TWO separate database entities:**

### Family Members (Duplicated)
```
Entity: <child_name>
├─ UUID-based: <entity_uuid>
│  └─ entity_type = Person ✓ (correct, filterable)
│  └─ facts: pref_name="<child_name>" (confidence 1.0)
│
└─ String-ID-based: "<child_name>" (display name as entity ID)
   └─ entity_type = unknown ✗ (can't filter by taxonomy)
   └─ alias in entity_aliases (is_preferred=false, orphaned)

Similar duplication for: <child_name>, <child_name>, <spouse>, ${ENTITY}
```

### Garbage Entities (Misclassified)
```
Unclassified (entity_type=unknown):
- ${ENTITY}.<domain> (should be Object, not entity)
- titan.<domain> (should be Object, not entity)
- "computer" (type label, not entity)
- "domain_name" (type label, not entity)
- "it" (pronoun, should not be entity)
- "son" (relationship/type, should not be entity)
```

### Locations (Correctly Typed, but Included)
```
Addresses: entity_type=Location ✓ (correct type, but no filtering in /query)
```

## Evidence (Pre-Prod Database)

```sql
-- Entity duplication for "<child_name>"
SELECT id, entity_type FROM entities 
WHERE user_id='<user_uuid>' 
AND (id='<entity_uuid>' OR id='<child_name>');

Result:
<entity_uuid> | Person
<child_name>                                  | unknown
```

```sql
-- Facts showing both UUID and string-ID references
SELECT subject_id, rel_type, object_id FROM facts 
WHERE user_id='<user_uuid>' 
AND rel_type='pref_name' 
AND object_id IN ('<child_name>', '<child_name>', '<child_name>', '${ENTITY}');

Result: Multiple facts with string object_ids instead of UUID consolidation
```

```sql
-- Garbage entities
SELECT id, entity_type FROM entities 
WHERE user_id='<user_uuid>' 
AND id IN ('computer', 'domain_name', 'it', 'son', '${ENTITY}.<domain>');

Result: All entity_type=unknown (can't be filtered)
```

## Why Taxonomy Filtering Fails

`_TAXONOMY_KEYWORDS` maps "family" → Person + Animal types.

`/query` should filter results to ONLY entities with entity_type IN ('Person', 'Animal').

**Current filtering doesn't work because:**

1. String-ID entities (<child_name>, <child_name>, <child_name>) have entity_type=unknown → not excluded
2. Garbage entities (computer, domain_name, ${ENTITY}.<domain>) have entity_type=unknown → not excluded
3. Address entities have entity_type=Location, which is correct type but still appears in results (no query-side filtering applied)

Result: `/query` returns UUID-based facts (Person-typed, pass filter) + String-ID facts (unknown-typed, can't filter) + unfiltered Location facts.

When Filter injects these, user sees noise alongside family relationships.

## Classification

**Write-time duplication issue:** Entity consolidation at `/ingest` is creating parallel representations instead of unified entities.

**Query-time missing filter:** Even if duplication is fixed, `/query` isn't applying taxonomy filtering to results.

## Impact

- Taxonomy-aware queries return unfiltered results
- PII leaks (addresses included alongside family relationships)
- Infrastructure noise (servers treated as family entities)
- Garbage entities pollute results (pronouns, type labels)
- System appears to "know" everything when it should scope by intent

## Recommended Investigation & Fix

### Phase 1: Understand Entity Lifecycle (Investigation Only)

**For DEEPSEEK (Investigation Mode):**

1. **Locate entity creation logic in `/ingest`:**
   - Find where `EntityRegistry.resolve()` is called
   - Find where string-ID entities (display names) are created as separate entities
   - Understand why both UUID + string representations exist

2. **Examine entity_aliases normalization:**
   - Does `_resolve_display_names()` in `/query` expect both UUID and string-ID entities?
   - Are string-ID entities intended as "fallback" representations?

3. **Trace pref_name/also_known_as ingestion:**
   - When `/ingest` creates a pref_name fact with string object (e.g., `(uuid, pref_name, "<child_name>")`), does it also create entity_id="<child_name>"?
   - Should it?

4. **Identify garbage entity creation:**
   - Where are "computer", "domain_name", "it", "son" being registered as entities?
   - Should extraction be filtered to prevent type labels + pronouns from becoming entities?

**Deliverable:** Brief analysis in scratch.md (DEEPSEEK-25A) with:
- Location of entity creation code
- Whether string-ID duplication is intentional or bug
- Where garbage entities originate
- Whether extraction should filter entities before registry

### Phase 2: Implement Fix (Pending)

Once investigation confirms:
- Entity consolidation at `/ingest` (eliminate string-ID orphans)
- Query-side taxonomy filtering in `/query` (enforce entity_type gates)
- Extraction-side garbage filtering (block pronouns + type labels from entity registry)

## Upon Completion (For DEEPSEEK-25A)

```markdown
###################################DEEPSEEK-25A##################################

## Entity Duplication Investigation — Root Cause Identified

**Code locations:**
- [File, line range] — entity creation
- [File, line range] — string-ID registration
- [File, line range] — garbage entity source

**Finding:** [Is duplication intentional or bug?]

**Next steps:** [What needs fixing in /ingest, /query, extraction filter]

####################################################################
```

## Next Steps

1. **DEEPSEEK-25A:** Investigation of entity lifecycle (above)
2. **DEEPSEEK-25B** (pending): Implementation of fix (consolidation + filtering)
3. **Validation:** Test that "Tell me about my family" returns only Person + Animal entities
