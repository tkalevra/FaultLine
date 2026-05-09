# Deepseek Fix Prompt: UUID Resolution Order (CLAUDE.md Hard Constraint)

**Scope:** Fix UUID leakage in event subjects. Align with CLAUDE.md hard constraint: "Display names stored in aliases, never in *_id columns. Filter must convert UUIDs to display names before injecting memory."

**Issue:** dprompt-9 validation found event subjects show raw UUIDs ("user", UUID, etc.) instead of display names in memory injection. This violates CLAUDE.md hard requirement.

**Why:** `/query` merges events after `_resolve_display_names()` has already run on facts. Events bypass display name resolution and inject raw UUIDs to the LLM.

---

## Root Cause

Current flow in Filter `inlet()`:

```python
# 1. Fetch facts from /query
facts = _fetch_from_query(...)

# 2. Resolve UUIDs → display names (EARLY)
facts = _resolve_display_names(facts, preferred_names, canonical_identity)

# 3. Build memory block (uses resolved facts)
memory_block = _build_memory_block(facts, ...)

# PROBLEM: Events are merged AFTER resolution
# Events still have raw UUIDs
```

But in actual code (openwebui/faultline_tool.py line ~1100), events are merged into facts **before** memory building, but after display resolution.

---

## The Fix

Two options (choose one):

### Option A: Resolve events before merge (preferred)

In `_filter_relevant_facts()` or query merge point:

```python
# After /query returns facts + events + preferred_names
facts = response.get("facts", [])
events = [f for f in facts if f.get("source") == "events_table"]
regular_facts = [f for f in facts if f.get("source") != "events_table"]

# RESOLVE events BEFORE merge
resolved_events = _resolve_display_names(
    events, 
    preferred_names, 
    canonical_identity
)

# Now merge
facts = regular_facts + resolved_events

# Build memory (all facts already resolved)
memory_block = _build_memory_block(facts, ...)
```

**Pro:** Single resolution pass, consistent behavior.
**Con:** Requires restructuring facts/events separation in Filter.

### Option B: Extend _resolve_display_names() to idempotent (safer)

Modify `_resolve_display_names()` to re-resolve if called twice:

```python
def _resolve_display_names(facts, preferred_names, identity):
    """
    Convert UUID subject/object to display names.
    Idempotent: safe to call multiple times (second call no-ops on already-resolved names).
    """
    resolved = []
    for fact in facts:
        resolved_fact = fact.copy()
        subject = fact.get("subject", "")
        object_ = fact.get("object", "")
        
        # Only resolve if UUID pattern detected
        if subject and _UUID_RE.match(subject):
            resolved_fact["subject"] = preferred_names.get(subject, subject)
        elif subject == identity:
            resolved_fact["subject"] = "user"
        
        if object_ and _UUID_RE.match(object_):
            resolved_fact["object"] = preferred_names.get(object_, object_)
        elif object_ == identity:
            resolved_fact["object"] = "user"
        
        resolved.append(resolved_fact)
    
    return resolved
```

Then call **after** events merge:

```python
# Merge events into facts
facts = regular_facts + events

# RESOLVE ALL (including events) before memory injection
facts = _resolve_display_names(facts, preferred_names, canonical_identity)

# Build memory
memory_block = _build_memory_block(facts, ...)
```

**Pro:** Minimal code change, safe idempotent operation.
**Con:** Slightly redundant (resolves facts twice), but negligible cost.

---

## CLAUDE.md Constraint Enforcement

**Hard requirement (CLAUDE.md):**
> "Display names are stored in `entity_aliases.alias`, never in `*_id` columns. Filter converts UUIDs to display names before memory injection."

**Validation:**
1. No `^[0-9a-f]{8}-` UUIDs appear in memory_block string
2. All entity subjects/objects are display names or "user"
3. `_UUID_RE` pattern match confirms no UUID leakage

```python
# Add validation before __event_emitter__ (temporary debug)
assert not _UUID_RE.search(memory_block), "UUID leaked into memory block!"
```

---

## Fix Checklist

- ✅ Choose Option A or B
- ✅ Implement in `openwebui/faultline_tool.py`
- ✅ Call `_resolve_display_names()` on events before memory building
- ✅ Verify UUID pattern regex still matches (lines 38-40)
- ✅ Test: run dprompt-9 Part C (Mixed queries)
  - Event subjects should show display names, not UUIDs
  - Memory block should NOT contain UUID patterns
- ✅ Log check: memory injection shows "⭐ user's born_on: ..." not "⭐ UUID's born_on: ..."
- ✅ SQL validation: facts table unchanged, only Filter output fixed

---

## Done When

- ✅ `_resolve_display_names()` called on events (or Option B idempotent version deployed)
- ✅ dprompt-9 Part C re-run: event subjects display as names, not UUIDs
- ✅ Memory block contains zero UUID patterns (regex check)
- ✅ CLAUDE.md hard constraint verified: no UUID leakage to LLM
- ✅ Regression: dprompt-9 Part A/B still pass

Ship it.

---

## Notes

- Option B (idempotent) is safer and minimal-change
- UUID regex: `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`
- Test memory_block with `grep -o '[0-9a-f]\{8\}-' memory_block` — should return zero matches
- CLAUDE.md constraint is load-bearing: LLM must never see UUIDs for correct context

