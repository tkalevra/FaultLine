# dprompt-16 — Identity Loss Under Sequential Preferences

## The Problem

User states: "I'm Christopher, call me Chris" then "My wife is Marla, she prefers Mars"

Expected behavior: Query returns "christopher" (or "chris"), identity preserved

Actual behavior: Query returns UUID, identity lost

## Root Cause Chain

1. **Extraction creates false positive:** "she prefers mars" generates `pref_name(user, mars)` (should be `pref_name(marla, mars)`)
2. **Sync logic demotes all competitors:** When `pref_name(user, mars)` confidence=1.0 is committed, lines 1811-1816 in `/ingest` execute:
   ```python
   if _is_pref:
       UPDATE entity_aliases SET is_preferred = false 
       WHERE entity_id = user AND alias != mars
   ```
   This demotes `christopher` and `chris` to is_preferred=false
3. **Display resolution fails:** Query does:
   ```sql
   SELECT alias FROM entity_aliases WHERE entity_id=user AND is_preferred=true
   ```
   Returns NULL (no preferred alias), fallback returns UUID

## DB State (Clean Test)

Facts created (in order):
- `also_known_as(user, christopher)` confidence=1.0, is_preferred=false ← should be true
- `pref_name(user, chris)` confidence=0.5, is_preferred=false ← should be true
- `pref_name(user, mars)` confidence=1.0, is_preferred=false ← FALSE POSITIVE (should not exist)
- `pref_name(user, mars)` confidence=1.0 (duplicate) ← FALSE POSITIVE
- `spouse(user, marla)` confidence=1.0
- `pref_name(marla, mars)` confidence=1.0, is_preferred=true ← CORRECT

Entity aliases (all marked NOT preferred):
- user: christopher (is_preferred=false), chris (is_preferred=false) ← inverted
- marla: marla (is_preferred=false), she (is_preferred=false) ← "she" is garbage
- Other junk entities: called, my, prefers, family, wifes (most marked is_preferred=true)

## The Two Bugs

### Bug 1: False-Positive Preference Extraction

**Where:** Filter-level patterns in faultline_tool.py or compound extractor

Text: "My wife's name is Marla, she prefers Mars"

Current behavior:
- Pattern finds "she prefers mars" → creates `pref_name(user, mars)` ✗
- Should only extract: `spouse(user, marla), pref_name(marla, mars)` ✓

Root cause: Preference patterns don't validate subject context. They search for "prefers X" and assume subject="user", regardless of pronouns/context.

### Bug 2: is_preferred Demotion Logic Too Aggressive

**Where:** `/ingest` lines 1811-1816

Current behavior:
- One high-confidence pref_name fact → demote all other pref_name facts for that entity
- Problem: If that one fact is wrong (false positive), it wipes out correct preferences

Better behavior:
- Don't auto-demote. Let multiple pref_name facts coexist.
- At query/display time: choose the highest-confidence, most-recent, user-stated fact
- Confidence hierarchy: user_stated (1.0) > llm_inferred (0.6) > staged/unconfirmed (0.4)

## What Needs Fixing

1. **Extract preferences with subject awareness** — Don't search for "prefers X" blindly. Validate that the subject is first-person before attributing to user:
   - First-person signals: "I prefer", "call me", "please call me", "my preference"
   - Third-person signals: "she prefers", "he goes by", "X prefers" (where X is a named entity)
   - Action: Extract subject first, then preference object, together

2. **Disable automatic demotion of competing preferences** — Multiple pref_name facts for the same entity should coexist with their original confidence values. Display resolution (not demotion logic) picks which to use.

3. **Add validation to prevent stopword-entity creation** — Stopwords like "called", "prefers", "my", "she", "wifes" should never be registered as entities. Check: Is the stopword list in compound.py being respected? Is there a second extraction path bypassing it?

## Expected Outcome

Clean test of same statements should result in:
- `also_known_as(user, christopher)` → entity_aliases: christopher is_preferred=true
- `pref_name(user, chris)` → entity_aliases: chris is_preferred=true (or marked preferred in /query logic)
- `spouse(user, marla)` → fact stored, marla entity created
- `pref_name(marla, mars)` → entity_aliases: mars is_preferred=true
- NO `pref_name(user, mars)` fact
- NO junk entities for stopwords
- Query "what's my name" returns "christopher" (or "chris"), not UUID

## For Deepseek

The librarian model is right: the graph should work if built correctly. These are extraction and storage bugs, not design bugs. The fixes are surgical:

1. **Extract preferences with subject context** (extraction layer)
2. **Remove auto-demotion logic** (ingest layer)
3. **Validate against stopword creation** (extraction + registry layers)

After these fixes, the query/display layers should Just Work™ — the graph is already sound.
