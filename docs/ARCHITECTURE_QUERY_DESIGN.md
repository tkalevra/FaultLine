# Architecture: Query Design & Backend-First Extraction

**Date:** 2026-05-12  
**Version:** 1.0  
**Purpose:** Establish architectural principle for query handling, extraction ranking, and Filter responsibility

## Core Principle

**The Filter is dumb. The backend is smart.**

The Filter does NOT:
- Gate facts based on entity types or keywords
- Re-rank based on brittle keyword lists
- Reject facts because they're "concepts" or "unknown"
- Distinguish between "generic queries" and "category queries"

The Filter DOES:
- Inject facts returned by `/query` into the prompt
- Trust backend ranking (fact order reflects relevance)
- Reorder by confidence *if needed* (rare)
- Pass through all facts with confidence ≥ threshold

## The Problem We Solve

**Current bug (dBug-report-001):** "Tell me about our pets" returns identity facts, zero `has_pet` facts.

**Root cause:** Filter's Tier 2 (identity fallback) fires on empty Tier 1, treats concept-filtered query the same as generic query, returns early before Tier 3 can run.

**Real root cause:** The backend isn't telling us which facts matter and why. So the Filter tries to guess. It adds Tier 1/2/3 logic, keyword lists, Concept filters — all brittle.

**Correct solution:** Backend extraction, ontology, and hierarchy must be so strong that the Filter never needs to guess.

## Query Structure Example

**Query:** "Where should my son and I go for dinner tomorrow?"

### Semantic Decomposition

```
Where         → location_relevance (spatial constraint)
should        → intent (recommendation)
my            → possessive anchor (user identity)
son           → hierarchy reference (familial: child_of relationship)
and           → conjunction (multiple entities)
I             → user identity anchor (repeat)
go            → movement/location action
for           → purpose
dinner        → activity/meal type
tomorrow      → temporal (future date)
```

### Information Hierarchy (What Matters Most)

1. **User Identity (A-tier):** "my" and "I" are anchors. Must resolve to user entity. Identity facts (pref_name, age, location) highest priority.

2. **Hierarchy Relationships (A-tier):** "son" triggers hierarchy traversal. Must walk `child_of` chain. User → parent_of → son entity. Essential for context.

3. **Relational Data (B-tier):** "son" and "dinner" → look for facts connecting son to locations, activities, preferences. `son -> lives_in -> [location]`, `son -> likes -> [food/restaurant]`.

4. **Contextual Relationships (B-tier):** "my" → possessive. Query mentions multiple entities (user + son). Graph traversal finds connected locations, restaurants, activities. `son -> works_at -> [location]`, `son -> has_visited -> [restaurant]`.

5. **Temporal Filtering (C-tier):** "tomorrow" → future date. Rank open restaurants, upcoming events. Time-based filtering applied post-fetch, not pre-filter.

### Backend Processing (What We Actually Return)

```
/query("Where should my son and I go for dinner tomorrow?")
├─ Extract entities: [user_id, son_id]
├─ Extract signals: location_relevance=true, hierarchy=true, temporal=tomorrow
├─ Fetch baseline facts (Class A)
│  └─ user: pref_name, location(current), preferences
│  └─ son: pref_name, location(lives_in), preferences
├─ Graph traversal (Class A/B)
│  └─ user → parent_of → son
│  └─ son → lives_in → location
│  └─ son → works_at → location
│  └─ son → likes → restaurant/food
├─ Hierarchy expansion
│  └─ location → instance_of → city (type context)
│  └─ restaurant → instance_of → dining_establishment
├─ Rank by fact class (A > B > C)
├─ Include confidence for tie-breaking within class
└─ Return: [30 facts, sorted by (class, confidence)]
```

**Key:** No gating. No "is this relevant?" decision. Backend returns ALL relevant facts ranked by provenance (A > B > C) + confidence. Filter trusts that order.

## What We DON'T Do

❌ Filter decides "dinner is not a person, skip it"  
❌ Filter checks "is son a concept entity?"  
❌ Filter applies keyword lists ("family", "pets", "kids")  
❌ Filter implements Tier logic (Tier 1 → Tier 2 → Tier 3)  
❌ Backend returns `entity_types` so Filter can gate  
❌ Ingest stores concept entities in `preferred_names`  

## What We DO Do

✅ Backend extraction recognizes entity types and hierarchy chains  
✅ Backend ranking respects fact class (A > B > C)  
✅ Backend includes confidence for uncertainty  
✅ Backend graph/hierarchy traversal captures all valid connections  
✅ Filter injects facts in returned order  
✅ Filter reorders *only if needed* (e.g., by confidence within class)  

## Architectural Implications

### 1. Extraction Must Be Ontology-Aware

Ingestion should not create "concept entities" with `entity_type = unknown`. Either:
- (a) Concept entities exist but are NOT stored in entity_aliases (not available for matching)
- (b) Concepts are soft (tagged in `entities.category` but not distinct entities)
- (c) Extraction recognizes domain context and types entities correctly

**Current issue:** `member_of(pets, family)` creates entities for "pets" and "family" with `entity_type = unknown`. These leak into `preferred_names` and pollute matching.

**Fix:** Don't create "concept entities." Store `member_of` relationships between real entities only. If query mentions "pets" as a concept, treat it as a category signal, not an entity lookup.

### 2. Hierarchy Must Support Upward & Downward Traversal

Query "where should my son go" must:
- Upward: son → instance_of → person (type inference)
- Downward: family_taxonomy → member_of → [children]
- Graph: son → lives_in → location (sibling facts)

All should return in ranked order. No filtering by type at `/query` time.

### 3. Confidence Must Reflect Provenance

- **Class A facts:** confidence 0.8–1.0 (user-stated or type-inferred)
- **Class B facts:** confidence 0.6–0.8 (behavioral, staged)
- **Class C facts:** confidence 0.4–0.6 (ephemeral, unconfirmed)
- **Qdrant vector hits:** confidence 0.3–0.7 (similarity-based)

Filter can trust this range to understand what's strong vs. exploratory.

### 4. Filter Never Calls `/query` Twice

Current architecture: Filter calls `/query`, evaluates result with Tiers, if unsatisfied calls `/query` again.

Correct architecture: Filter calls `/query` once. Result is authoritative. If results are wrong, ingest/ranking was wrong, not Filter logic.

## Path Forward

### Phase 1: Fix Ingestion (Backend Owns Types)

Don't let "concept entities" leak into `preferred_names`. Either:
- Exclude Concept/unknown from entity_aliases during ingest
- Or: make entity_aliases entries require real entity types

### Phase 2: Strengthen Hierarchy Chains

Ensure `_hierarchy_expand()` walks all relevant rel_types (`instance_of`, `subclass_of`, `part_of`, `member_of`, `is_a`) bidirectionally without gating.

### Phase 3: Remove Filter Gating Logic

Delete Tier 1/2/3 from Filter. Replace with simple confidence threshold (0.4 or configurable). Inject facts in backend-returned order.

### Phase 4: Validate via Real Queries

Test "where should my son and I go for dinner tomorrow?" and similar real-world queries. Verify backend returns right facts. Verify Filter injects them unchanged.

## References

- dBug-report-001: Tier 2 Identity Fallback Blocks Tier 3 After Concept Filter
- dprompt-52b: Entity-Type-Aware Tier 1 Matching (symptom fix, not root fix)
- dprompt-47c: Hierarchy-Chain-Aware Taxonomy Filter (symptom fix, not root fix)

## Next Steps

1. Analyze ingestion pipeline: where do concept entities come from?
2. Propose extraction changes to prevent Concept/unknown from polluting `preferred_names`
3. Simplify Filter to pure confidence gating (remove Tier logic)
4. Validate with real queries
