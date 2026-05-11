# dprompt-51b: Graph-Proximity Relevance — Replace Keyword Scoring

**For: deepseek (V4-Pro)**

---

## Task

Replace the Filter's keyword-based relevance scoring (`_categorize_query`, Tier 3 of `_filter_relevant_facts`) with graph-proximity-based relevance. The backend already computes graph distance and taxonomy membership — the Filter should trust that structure instead of re-judging relevance with brittle keyword lists.

---

## Context

**Architecture mismatch:** The backend runs graph traversal, hierarchy expansion, and taxonomy filtering. It returns facts organized by connectivity to the user. But `_filter_relevant_facts()` throws away this structure and re-scores every fact from scratch using `_CAT_SIGNALS` — a hardcoded keyword dictionary that fails on natural language variation ("our pets" ≠ "my pets", "her family" ≠ "my family").

**What the backend already does:** Graph proximity IS the relevance signal. One-hop facts (spouse, child, pet, job) are inherently relevant. Two-hop facts (spouse's pet, child's school) are contextually relevant. The taxonomy filter narrows by entity type when query intent is detected.

**What the Filter should do:** Gate by confidence, protect sensitive facts, limit volume, format for injection. Not second-guess relevance.

---

## Constraints

**DO:**
- Remove `_categorize_query()` method — no longer needed
- Remove Tier 3 keyword scoring from `_filter_relevant_facts()` — replace with graph-proximity pass-through
- Keep `calculate_relevance_score()` but strip keyword-match component (component 1, 0.0–0.6)
- Keep confidence bonus (component 2) and sensitivity penalty (component 3) in `calculate_relevance_score`
- Keep Tier 1 entity-name matching via `_extract_query_entities()` — graph-proximity-based, not keyword-based
- Keep Tier 2 identity fallback for generic queries
- Keep `_apply_confidence_gate()` and `MIN_INJECT_CONFIDENCE`
- Keep sensitivity gating (`_SENSITIVE_RELS`, `_SENSITIVE_TERMS`)
- Keep volume limiting (`MAX_MEMORY_SENTENCES`)
- Keep `_is_realtime_query()` — realtime detection is separate from relevance
- Add debug logging for dropped facts (count, reason)

**DO NOT:**
- Modify backend `/query` or `/ingest`
- Change database schema
- Remove entity-name matching (Tier 1)
- Change the memory injection format or `_build_memory_block()`

---

## Sequence

**1. Remove `_categorize_query()` method**

Delete the entire method from the `Filter` class. Its only call site is in `inlet()` to compute `_categories` — that variable passes to `_filter_relevant_facts()` which will no longer use it.

**2. Simplify `calculate_relevance_score()`**

Remove the keyword-match component (component 1, query signal match 0.0–0.6). Keep only:
- Confidence bonus (component 2): `confidence * 0.3` — unchanged
- Sensitivity penalty (component 3): `-0.5` for PII facts not explicitly requested — unchanged

New method:
```python
def calculate_relevance_score(self, fact: dict, query: str) -> float:
    """Score a fact's relevance. Graph proximity is determined by the backend;
    the Filter only gates by confidence and sensitivity."""
    score = 0.0
    
    # Confidence bonus (0.0–0.3)
    confidence = fact.get("confidence", 0.0)
    score += confidence * 0.3
    
    # Sensitivity penalty (-0.5 for PII facts not explicitly requested)
    _SENSITIVE_RELS = {"born_on", "lives_at", "lives_in", "height", "weight", "born_in"}
    _SENSITIVE_TERMS = {"born", "birth", "live", "address", "height", "weight", 
                        "birthplace", "tall", "how tall", "heavy", "how heavy", 
                        "old", "age", "how old"}
    if fact.get("rel_type") in _SENSITIVE_RELS:
        query_lower = query.lower()
        explicitly_asked = any(term in query_lower for term in _SENSITIVE_TERMS)
        if not explicitly_asked:
            score -= 0.5
    
    return max(0.0, min(1.0, score))
```

**3. Replace Tier 3 in `_filter_relevant_facts()`**

Current Tier 3 does keyword scoring and returns only facts above `RELEVANCE_THRESHOLD` (0.4). Replace with graph-proximity pass-through that trusts the backend's ordering:

```python
# TIER 3: Graph-proximity pass-through — backend already ranked by relevance.
# Only gate by confidence. Sensitivity penalty applied per-fact.
_RELEVANCE_THRESHOLD = 0.0  # confidence-only; graph structure is the signal

def should_include_fact(fact: dict) -> bool:
    # Identity facts always pass (names, aliases)
    _IDENTITY_RELS = {"also_known_as", "pref_name", "same_as"}
    if fact.get("rel_type") in _IDENTITY_RELS:
        return True
    # Gate by confidence + sensitivity
    return self.calculate_relevance_score(fact, query) >= _RELEVANCE_THRESHOLD

scored = [f for f in cleaned if should_include_fact(f)]
return _apply_confidence_gate(scored)
```

Key change: `RELEVANCE_THRESHOLD` drops from 0.4 to 0.0. Confidence alone gates — a fact with confidence 0.4 gets score 0.12 and passes. Only facts with confidence 0 AND a sensitivity penalty (score -0.5) get dropped. This is intentional: the backend already decided these facts are graph-relevant.

**4. Update `inlet()` call sites**

Remove `_categories` computation and `is_realtime` passing to `_filter_relevant_facts`:

```python
# BEFORE:
_categories = self._categorize_query(text, facts)
_is_realtime = self._is_realtime_query(text)
if _is_realtime:
    _categories.add("location")
facts = self._filter_relevant_facts(
    facts, _categories, canonical_identity,
    preferred_names=preferred_names, query=text, is_realtime=_is_realtime
)

# AFTER:
facts = self._filter_relevant_facts(
    facts, set(), canonical_identity,
    preferred_names=preferred_names, query=text
)
```

Remove `categories` parameter from `_filter_relevant_facts()` signature. Remove `is_realtime` parameter. Remove the `has_category_intent` check in Tier 2 (it used `categories`).

**Tier 2 fallback update:** Without `categories`, Tier 2 becomes a pure identity fallback for queries that don't match any entity name:

```python
# TIER 2: Identity fallback — for generic queries with no entity match,
# return identity-defining facts (names, family structure).
# NOTE: Without category info, this runs for ALL non-entity-matched queries.
# This is correct — generic queries should get identity context.
_TIER2_IDENTITY_RELS = {"also_known_as", "pref_name", "same_as",
                        "spouse", "parent_of", "child_of", "sibling_of"}
tier2 = [f for f in cleaned if f.get("rel_type") in _TIER2_IDENTITY_RELS]
if tier2:
    return _apply_confidence_gate(tier2)
```

**5. Add debug logging**

At the end of `_filter_relevant_facts()`, log what was dropped and why:

```python
if self.valves.ENABLE_DEBUG:
    dropped = len(cleaned) - len(scored)
    if dropped > 0:
        print(f"[FaultLine Filter] relevance dropped {dropped} facts "
              f"(kept {len(scored)}/{len(cleaned)})")
```

**6. Test cases**

- "tell me about my family" → all spouse/child facts pass (confidence ≥ 0.6 → score ≥ 0.18)
- "tell me about our pets" → all has_pet facts pass (same confidence math)
- "what's my address" → lives_at passes (explicit ask, no sensitivity penalty)
- "how are you" → lives_at dropped (confidence 0.6 → score 0.18, sensitivity penalty -0.5 → -0.32, below threshold → dropped)

---

## Deliverable

**File:** `openwebui/faultline_tool.py`
- Removed `_categorize_query()` method
- Simplified `calculate_relevance_score()` — keyword component removed
- `_filter_relevant_facts()` — Tier 3 replaced with confidence-only gating
- `inlet()` — removed `_categories` computation, updated call sites

---

## Success

- ✓ "tell me about our pets" returns has_pet facts (no keyword "my pets" needed)
- ✓ "her family" returns family facts (no keyword "my family" needed)
- ✓ Sensitivity still gates PII on generic queries
- ✓ Explicit PII queries ("how old am I") still return age/birthday
- ✓ Confidence gate still active (`MIN_INJECT_CONFIDENCE`)
- ✓ Volume limiting still active (`MAX_MEMORY_SENTENCES`)
- ✓ Tests pass, no regressions

---

## Upon Completion

```markdown
## ✓ DONE: dprompt-51 (Graph-Proximity Relevance) — 2026-05-12

**Implementation:**
- Removed `_categorize_query()` — keyword-based category detection eliminated
- Simplified `calculate_relevance_score()` — keyword-match component removed, confidence + sensitivity only
- Tier 3 replaced with confidence-only gating (threshold 0.0, backend's graph structure is the signal)
- Removed `categories` and `is_realtime` parameters from `_filter_relevant_facts()`

**Result:** Filter no longer second-guesses backend relevance. Graph proximity from
/query is authoritative. "our pets", "her family", "their kids" — all work without
keyword lists.

**Next:** Deploy Filter to OpenWebUI + integration test.
```
