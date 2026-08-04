# FaultLine Investigation Framework

**Purpose:** Systematic debugging methodology that respects CLAUDE.md hard constraints, growth principles, and architecture decisions.

**Updated:** 2026-05-24

---

## Core Principle: The Growth Architecture

FaultLine is **self-strengthening**, not **pre-seeded**. Every architectural decision flows from this:

### The Three Growth Layers

1. **Database as Source of Truth** (rel_types, entity_taxonomies, entity_types)
   - All metadata queries go to DB first
   - Hardcoded fallbacks are EMERGENCY ONLY (DB completely unreachable)
   - No "sensible defaults" — fail loud if DB unavailable

2. **GLiNER2 as Native Extractor** (entity types + relationships)
   - GLiNER2 can extract entities, relationships, JSON structures natively
   - Don't ask LLM to do what GLiNER2 is designed for
   - LLM handles ONLY novel/complex patterns GLiNER2 can't recognize

3. **re_embedder as Growth Loop** (async learning)
   - Novel patterns staged with low confidence
   - Promoted when frequency >= 3 (statistical validation)
   - LLM evaluates, approves, stores in rel_types
   - Future instances automatically benefit

---

## Red Flags: When to Investigate

### Flag 1: Hardcoding or Seeding
**Watch for:**
- `_FALLBACK_*` constants with values (rel_types, categories, mappings, etc.)
- `INSERT INTO` statements with hardcoded data in migrations (after initial schema)
- Hardcoded lists like `["spouse", "parent_of", "child_of"]` in code (not in prompts)
- Pattern strings like regex or string matches that should be DB-driven
- "sensible default" comments suggesting fallback logic

**Why it's wrong:**
- Violates growth principle — system can't learn new patterns
- Maintenance nightmare — changes require code deploys
- Contradicts CLAUDE.md: "All rel_type resolution queries the database — NO HARDCODED MAPPINGS"

**Fix:**
- Query DB, let query fail if DB unavailable (don't catch with fallback)
- Log the failure loudly (`log.error`, not `log.warning`)
- Return empty/error to caller (let ingest/query decide next step)
- Only exception: emergency caches loaded at startup from DB (not hardcoded values)

---

### Flag 2: Wrong Tool for the Job
**Watch for:**
- Using LLM to extract something a specialized library can do natively
- Multiple extraction layers (e.g., GLiNER2 for types, then LLM for relationships)
- Fallback to LLM when specialized tool fails
- "LLM as fallback" comments

**Common mistakes:**
- ❌ GLiNER2 extracts entities → LLM extracts relationships (backwards!)
- ❌ GLiNER2 extracts types → LLM asked to use types as context
- ❌ Pattern extraction fails → LLM called as fallback
- ✅ GLiNER2 `extract_relations()` returns relationships directly
- ✅ GLiNER2 `extract_json()` returns structured data directly
- ✅ LLM ONLY for novel patterns (e.g., unseen rel_types, complex pronouns)

**Fix:**
- Check GLiNER2 capabilities: https://huggingface.co/urchade/gliner2-base
- Use `extract_relations(text, rel_type_list)` for standard relationships
- Use `extract_json(text, schema)` for structured extraction
- Use `extract_entities()` for entity types only
- LLM handles ONLY what GLiNER2 can't: novel patterns, context resolution

---

### Flag 3: Missing Database Query or Silent Failure
**Watch for:**
- Try/except blocks that catch DB errors and continue silently
- Functions that return empty/default without logging the error
- Code paths where DB query "should" run but isn't logged
- Comments like "fallback to cache" or "use hardcoded if DB fails"

**Why it's wrong:**
- Silent failures hide bugs (you don't know if feature is working or falling back)
- Code becomes unmaintainable (unclear which path is primary)
- Growth breaks (novel patterns don't get stored if DB is silently unavailable)

**Fix:**
- Log DB errors as `log.error()` with full context
- Don't catch DB exceptions silently
- Let the error propagate (caller decides fallback)
- Cache is ONLY for performance, not for correctness

---

### Flag 4: Prompt Engineering Instead of Architecture
**Watch for:**
- Very long prompts trying to compensate for missing data
- "Fallback" prompt sections that hardcode examples
- Prompt mentioning specific rel_types, categories, or patterns
- Comments like "examples loaded from DB at runtime" but code doesn't show it

**Why it's wrong:**
- Prompts should reference DB metadata, not hardcode it
- When DB schema changes, prompt becomes stale
- Violates separation of concerns (prompt shouldn't know about data)
- LLM becomes bottleneck (tight coupling between code and model behavior)

**Fix:**
- Extraction prompt queries DB for `natural_language` descriptions
- Prompt mentions rel_types dynamically (not hardcoded list)
- If DB unavailable, prompt says "error" (not "here's a default list")
- Prompt is DATA-DRIVEN, not hand-crafted

---

## Investigation Checklist

### Before Modifying Code

- [ ] **Does the feature need database queries?** (rel_types, entity_taxonomies, taxonomy_cache)
  - If yes: trace the query path
  - Is the query actually running? (check logs)
  - Does it return expected data?
  - Is there a hardcoded fallback hiding the error?

- [ ] **Does this use a specialized library?** (GLiNER2, psycopg2, etc.)
  - If yes: check library docs for native methods
  - Are we using the right method? (extract_entities vs extract_relations vs extract_json)
  - Are we using it correctly? (parameters, expected output)
  - Is there a fallback to LLM that shouldn't exist?

- [ ] **Is this in the extraction path?** (Filter, /extract/rewrite, /ingest)
  - Does it hardcode rel_types, patterns, or fallback data?
  - Does it rely on LLM when GLiNER2 could do it?
  - Is there a prompt section with hardcoded examples?
  - Should this use database-driven metadata instead?

- [ ] **Is error handling hiding real problems?**
  - Are DB failures caught and silently ignored?
  - Are there "fallback to cache" comments?
  - Are errors logged as warnings instead of errors?
  - Can you reproduce the issue with DB intentionally failing?

---

## How to Use This Framework

### When investigating a bug:

1. **Identify which layer is broken** (Filter → /extract/rewrite → /ingest → /query)
2. **Check for red flags** (hardcoding, wrong tool, silent failures, prompt tricks)
3. **Trace the data flow**:
   - Does it query the database? ✓ Is the query correct?
   - Does it use GLiNER2? ✓ Is it the right method?
   - Does it call the LLM? ✓ Is LLM actually needed?
   - Does it have error handling? ✓ Is the error being hidden?
4. **Ask: "Is this hardcoded?"**
   - If yes → violates growth principle → must be removed
   - If no but has hardcoded fallback → investigate why DB query failed
5. **Check CLAUDE.md** (lines 267-283: "Why Three Dimensions Matter")
   - Extract should be dumb (just produce triples)
   - Ingest should be smart (metadata-driven routing)
   - Query should trust DB (no hardcoded filters)

### When modifying code:

1. **Assume DB is the source of truth** — query it, don't guess
2. **Use the right tool for the job** — don't ask LLM to do GLiNER2's job
3. **Fail loud, don't fall back silently** — log errors clearly
4. **Keep prompts data-driven** — pull metadata from DB, not hardcode examples
5. **Test with DB unavailable** — confirm error is caught and logged

---

## Key Files to Reference

| File | Purpose | What to Look For |
|------|---------|-----------------|
| CLAUDE.md | Architecture | Lines 113-283: Three Dimensions Model |
| engine-growth-entities.md | Growth layers | How rel_types/taxonomies grow |
| src/api/main.py | Ingest pipeline | _build_extraction_prompt, _extract_structured_facts, /ingest endpoint |
| .venv/.../gliner2/inference/engine.py | GLiNER2 API | extract_relations, extract_json, extract_entities methods |
| migrations/005_rel_types_table.sql | Schema | rel_types table structure |
| src/wgm/gate.py | Validation | Metadata-driven validation, novel rel_type approval |
| src/re_embedder/embedder.py | Growth loop | ontology_evaluations, promotion logic |

---

## When to Use Tools

### Agent (Explore)
- Broad codebase search: "find all places where we use LLM for extraction"
- Pattern discovery: "where are hardcoded rel_types used?"
- Architecture review: "trace the extraction pipeline end-to-end"

### Agent (General-Purpose)
- Root cause analysis: "why is family extraction failing?"
- Complex debugging: "compare expected vs actual data flow"
- Architecture validation: "does this violate growth principles?"

### Bash (direct grep/inspection)
- Quick checks: `grep -n "hardcoded\|FALLBACK\|_EMERGENCY"` to find violations
- Code review: check specific function implementations
- Log inspection: examine error messages and flow

### Manual code reading
- Understanding architecture (CLAUDE.md, engine-growth-entities.md)
- Learning tool APIs (GLiNER2 methods, PostgreSQL queries)
- Tracing execution paths (function calls, error handling)

---

## Common Investigation Patterns

### Pattern: "Only 1 fact created, 45 junk entities"

**Root cause diagnosis:**
1. GLiNER2 ran → extracted 45 random entity names
2. Entities registered (no filter)
3. LLM extraction called but returned empty or malformed
4. LLM extraction likely had wrong prompt or missing context
5. Result: Entities created, but no relationships → junk data

**Trace points:**
- Does `/extract/rewrite` prompt have DB-loaded examples or hardcoded?
- Is the LLM actually being called? (check logs for HTTP success)
- Is the LLM response being parsed correctly? (check for JSON parse failures)
- Is GLiNER2 being asked to extract relationships? (check for extract_relations calls)

**Fix approach:**
- Use GLiNER2.extract_relations() instead of LLM
- Ensure LLM only called for novel patterns
- Remove hardcoded prompt fallbacks
- Log every step clearly

---

## Anti-Patterns: What NOT to Do

❌ **Don't hardcode:**
```python
_FAMILY_REL_TYPES = ["spouse", "parent_of", "child_of"]  # HARDCODED!
```

✅ **Do query DB:**
```python
cur.execute("SELECT rel_type FROM rel_types WHERE category = 'family'")
family_types = [row[0] for row in cur.fetchall()]
```

---

❌ **Don't fallback silently:**
```python
try:
    # query DB
except:
    return HARDCODED_FALLBACK  # Silent failure!
```

✅ **Do fail loud:**
```python
try:
    # query DB
except Exception as e:
    log.error("critical_db_failure", error=str(e))
    raise  # Let caller handle it
```

---

❌ **Don't ask LLM to do GLiNER2's job:**
```python
entities = gliner_model.extract_entities(text, types)
# Then:
response = await llm_call("extract relationships from: " + text)
# LLM tries to extract relationships from text without structured context
```

✅ **Do use the right tool:**
```python
entities = gliner_model.extract_entities(text, types)
relations = gliner_model.extract_relations(text, rel_type_list)
# GLiNER2 extracts relationships using its native strength
```

---

❌ **Don't embed examples in prompt:**
```python
prompt = """
Extract relationships. Examples:
- parent_of: parent is the parent of child
- spouse: person is married to spouse
"""
```

✅ **Do load examples from DB:**
```python
cur.execute("SELECT rel_type, natural_language FROM rel_types LIMIT 10")
examples = cur.fetchall()
prompt = f"Extract relationships. Examples:\n" + 
         "\n".join(f"- {rt}: {nl}" for rt, nl in examples)
```

---

## Debugging Workflow

### When feature is broken:

1. **Start at entry point** (Filter inlet for extraction features)
2. **Check logs** (Docker container logs, not just HTTP response)
3. **Verify DB is accessible** (can you query manually?)
4. **Trace metadata flow** (are rel_types being loaded?)
5. **Check for hardcoding** (are values from DB or hardcoded?)
6. **Verify tool usage** (is GLiNER2 method correct?)
7. **Test in isolation** (extract endpoint alone, without Filter)

### Questions to ask:

- **Is this supposed to work without the database?** → NO. Fail if DB unavailable.
- **Should this use a specialized library instead?** → Check capabilities first.
- **Is this hardcoded anywhere?** → Search for it, remove it.
- **Is the error being logged clearly?** → Make sure `log.error()` is called.
- **Does this violate a CLAUDE.md principle?** → Fix it.

---

## Reference: CLAUDE.md Hard Constraints

From CLAUDE.md "Key Principles (Do Not Violate)":

- **No hardcoded rel_types/entities/categories** — all from database
- **LLM never has unsupervised write access** — WGM gate validates
- **PostgreSQL is authoritative** — Qdrant is read-only
- **Write-time normalization** — consistent storage
- **No recursive matching** — use pre-lowercased values
- **Validation is metadata-driven** — rel_types table stores rules
- **Deduplication uses UUIDs** — not display names
- **Graph + hierarchy are separate** — don't conflate them
- **Backend graph-proximity is authoritative** — Filter trusts it
- **LLM injection responses are plain English** — not machine-readable tuples

**Any code violating these is a bug.**

---

## Going Forward

Every investigation should:
1. ✓ Understand the growth principle (database-driven, not hardcoded)
2. ✓ Check for red flags (hardcoding, wrong tools, silent failures)
3. ✓ Use proper tools (Agent for search, Bash for quick checks, manual for architecture)
4. ✓ Respect CLAUDE.md constraints
5. ✓ Test assumptions (especially DB availability, tool capabilities)
6. ✓ Document findings clearly (enable future investigations)

**When in doubt, ask: "Does this belong in the database?"**
**If yes and it's hardcoded: remove it.**
**If no and it's hardcoded: remove the hardcoding.**

---

*Last updated: 2026-05-24*
*Investigation: LLM Endpoint Docker Configuration + GLiNER2 Misuse*
