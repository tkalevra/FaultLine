# dBug-report-009: Non-Factual Information (Health Status/Ephemeral Events) Not Persisting

**Date Reported:** 2026-05-15  
**Severity:** P2 (Missing feature, not data loss)  
**Status:** Open  
**Version:** v1.0.7

## Summary

Health-related and ephemeral event facts (e.g., "I pulled my back," "I visited the chiropractor," "I'm in bed resting") are not being extracted or persisted to the knowledge graph at all. User explicitly states these should be captured as **Class C facts** (ephemeral, 30-day TTL) at minimum.

## Evidence

**Pre-prod testing (2026-05-15):**

User: "I pulled my back over the weekend, it's really sore. I'm in bed after visiting the chiropractor"

FaultLine Response: Correctly injects family facts (Mars, Des, Cyrus, Gabby, Fraggle) but ignores back injury details.

User followup: "I hurt my back, we talked about it, you didn't remember"

**Database audit:**
- PostgreSQL facts table: 21 total facts
- Staged_facts table: 20 total facts
- **Zero health-related facts**: No "pulled_back", "visited_chiropractor", "in_bed", or similar
- **Zero novel rel_types** for health domain in rel_types or pending_types tables

**Conclusion:** Facts were never extracted by the LLM, never ingested, never stored.

## Root Cause

The Filter's extraction prompt (`openwebui/faultline_tool.py` lines 86–171, `_TRIPLE_SYSTEM_PROMPT`) defines:

1. **Explicit rel_types to extract:** spouse, parent_of, child_of, sibling_of, works_for, lives_at, likes, dislikes, owns, age, height, weight, born_on, instance_of, subclass_of, member_of, part_of
2. **Open-ended rule:** "Other types allowed if none fit"

However, **health status and ephemeral event facts fall outside the primary extraction scope**. The LLM either:
- Doesn't recognize "pulled my back" as a valid rel_type (it's not in the explicit list)
- Treats it as novel and gates it as Class C without explicit extraction instructions
- Or **silently rejects it** because it doesn't match the extraction patterns

The prompt has **no guidance for:**
- Health/body status facts (injury, pain, fatigue, illness)
- Ephemeral location facts (currently in bed, at chiropractor)
- Transient state changes (sick, recovering, resting)
- Short-term event facts (appointments, visits, activities happening right now)

## Expected Behavior

**As Class C facts (ephemeral, 30-day TTL):**
- "user has_injury back" (OR "user has_health_status injured")
- "user located_at chiropractor" OR "user visited chiropractor"
- "user is_currently resting" OR "user health_status recovering"

These are behavioral/contextual facts with **low confidence (0.4)** and **short relevance windows** — perfect for Class C staging.

Current: **Never extracted or staged**  
Expected: **Extracted as Class C, expire after 30 days if unconfirmed**

## Impact

- User reports facts multiple times thinking the system forgot, but actually **facts were never ingested**
- Distinction between "I forgot" vs "facts never extracted" is unclear to users
- Ephemeral information (current state, short-term activities) is lost even though it's explicitly valuable
- System appears unreliable for transient states

## Solution (Scoped)

**Option A: Expand extraction prompt**
- Add health/status rel_types to `_TRIPLE_SYSTEM_PROMPT` (lines 103–126)
- Document health_status, has_injury, currently_at, is_recovering as valid Class C rel_types
- Examples: "I'm in pain" → user has_injury back, "visiting doctor" → user located_at clinic

**Option B: Novel rel_type approval pipeline** (already exists, but not working)
- LLM attempts extraction: "user pulled_back injury"
- WGM gate flags as novel, stages as Class C
- Re-embedder evaluates: "pulled_back" too specific, map to "has_injury" or create generic
- But this requires **explicit extraction attempt**, which isn't happening now

**Option C: Conversation-aware extraction** (broader)
- Extract ephemeral/state facts explicitly when user is reporting current status
- Pattern match: "I'm [verb]ing" (in bed, resting, visiting, at X location)
- Emit as Class C with low confidence

## Testing

After fix, repeat conversation:
- Message: "I pulled my back over the weekend, visited chiropractor, in bed resting"
- Expected in staged_facts: 
  - (user, has_injury, back, C, 0.4)
  - OR (user, visited, chiropractor, C, 0.4)
  - OR (user, located_at, chiropractor, C, 0.4)
  - OR (user, health_status, recovering, C, 0.4)
- Expected /query response: Injects health context before model sees message

## Notes

- This is **not a data loss bug** — facts aren't being deleted, they're not being captured
- Requires LLM extraction improvement, not database/validation fix
- Class C staging + 30-day TTL is the correct tier for ephemeral health facts
- Novel rel_type pipeline already supports this **IF** LLM attempts extraction

---

**Assigned to:** deepseek (dprompt-TBD) — Investigation + extraction prompt expansion  
**Blocked by:** None  
**Related:** dprompt-52 (entity type metadata), dprompt-59 (semantic conflicts)
