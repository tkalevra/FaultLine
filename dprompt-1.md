# Deepseek Implementation Prompt: Three-Tier Entity-Centric Retrieval

**Scope:** Refactor `_filter_relevant_facts()` in `openwebui/faultline_tool.py` to use relational graph weighting instead of keyword-based scoring.

**Why:** The graph itself IS the relevance signal. `/query` already ranks facts by relational proximity (2-hop traversal, entity degree). The Filter was second-guessing that ranking. Stop doing that.

---

## What to build

Replace the current single-scoring path in `_filter_relevant_facts()` with **three tiers, evaluated in order:**

### Tier 1: Entity Match
- Extract entity names from the query (token match against `preferred_names` dict, case-insensitive)
- Return ALL facts where the extracted entity is subject or object
- If multiple entities found, return facts for all of them
- No scoring. No filtering. Just: does this fact involve an entity mentioned in the query?

### Tier 2: Identity Fallback
- If no entities extracted (generic query like "hey", "what's new")
- Return only identity facts: `{also_known_as, pref_name, same_as, spouse, parent_of, child_of, sibling_of}`
- This is the baseline — enough for the LLM to know who it's talking to, without dumping all Class B facts

### Tier 3: Keyword Scoring
- If Tier 1 and 2 return nothing
- Fall back to the current `calculate_relevance_score()` logic
- This preserves "weather" → location, "birthday" → temporal behavior for topic-based queries

---

## Implementation checklist

1. **Add helper function `_extract_query_entities(query: str, preferred_names: dict) -> set[str]`**
   - Split query into tokens
   - Match each token (lowercased) against preferred_names keys (lowercased)
   - Return set of matched entity display names
   - Handle punctuation naturally: "mars?" and "mars," both match "mars"

2. **Rewrite `_filter_relevant_facts()` logic**
   ```
   def _filter_relevant_facts(...):
       # Remove garbage facts first (already exists)
       cleaned = [f for f in facts if not _garbage(...)]
       
       # TIER 1: Entity match
       entities = _extract_query_entities(query, preferred_names)
       if entities:
           tier1 = [f for f in cleaned 
                    if f.get("subject") in entities or f.get("object") in entities]
           apply_confidence_gate(tier1) and return
       
       # TIER 2: Identity fallback
       identity_rels = {also_known_as, pref_name, same_as, spouse, parent_of, child_of, sibling_of}
       if not query or len(entities) == 0:
           tier2 = [f for f in cleaned if f.get("rel_type") in identity_rels]
           apply_confidence_gate(tier2) and return
       
       # TIER 3: Keyword scoring (existing logic)
       return current_calculate_relevance_score behavior
   ```

3. **Remove these code blocks (they're redundant now):**
   - Family query override (lines ~580–602)
   - Attribute query override (lines ~604–624)
   - Pet query detection (if you added it — don't)
   - Keep the comment explaining why they're gone

4. **Do NOT:**
   - Add relation resolution ("my wife" → spouse lookup). That's a future enhancement.
   - Add transitive path walking ("my spouse's pet").
   - Add fuzzy entity matching or regex aliases.
   - Optimize the entity extraction (linear token match is fine).
   - Change `/query` endpoint.
   - Add new confidence gates or thresholds.

5. **Test manually:**
   - Query: "hey" → should return identity facts only
   - Query: "How's Mars?" → should return all facts about Mars
   - Query: "Where do I live?" → should return facts about user's location
   - Query: "what's my birthday?" → falls to Tier 3, keyword match, returns temporal facts

---

## Locking in the decision

**This is simpler than it looks because the graph densifies with use.** You don't need perfect relation resolution or edge-case handling. The system improves naturally as new edges get created. Focus on the foundation: entity extraction + filtering + trust the graph weighting.

Don't add:
- "my wife" pattern matching (Tier 1 catches the spouse's name if it's in facts)
- Bidirectional rel_index (linear scan is fine, facts list is bounded ~20–50 items)
- Multi-hop relation walking (2-hop is already in `/query`)
- Novel type approval caching or other "improvements"

**This is the scope. Ship it. Move on.**

---

## Files to modify

- `openwebui/faultline_tool.py` — `_filter_relevant_facts()` and new helper `_extract_query_entities()`

That's it. No new files. No new endpoints. No database changes.

---

## Done when

- Tier 1, 2, 3 logic in place and commented
- Family/attribute overrides removed
- Manual tests pass
- Code review ready
