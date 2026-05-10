# dprompt-36b — Age Validation: Entity-Type-Aware Fix [PROMPT]

## #deepseek NEXT: dprompt-36b — Age Validation (Entity-Type-Aware) — 2026-05-10

### Task:

Replace the overly-simplistic age > 150 hard reject (dprompt-35b) with entity-type-aware validation: Person ages 0–150 only; non-Person entities (Planet, Mountain, etc.) accept any non-negative age. Also improve date-to-age extraction for "born on [date]" patterns.

### Context:

dprompt-35b added age validation: reject < 0 or > 150. But that breaks Planet Earth (4.5 billion years), geological entities (millions of years), historical entities (> 150 years old). The real bug is Filter LLM extracting age=192 from "born on January 15, 1990" — that's an extraction/calculation bug, not a validation problem.

**Solution:** Be smart about what ages are valid for what entity types. Person ages have strict limits (0–150). Planet, Mountain, Ocean ages can be billions. Non-person entities don't have age constraints.

### Constraints (CRITICAL):

- **Wrong: Hard reject all ages > 150 (breaks legitimate data)**
- **Right: Entity-type-aware validation (Person ≠ Planet)**
- **MUST: For Person entities ONLY: reject age < 0 or > 150**
- **MUST: For non-Person entities: accept age ≥ 0 (no upper limit)**
- **MUST: For "born on [date]" patterns: parse date, calculate age, override LLM value if implausible**
- **MUST: Log rejected ages for observability (not error, just info)**
- **MUST: No data loss — ages still stored, just with proper validation per entity type**

### Sequence (DO NOT skip or reorder):

1. Read dprompt-36.md spec carefully (all approaches, edge cases)

2. **Find current age validation:** Search `src/api/main.py` for line 1771 or "scalar_rejected_implausible_age"

3. **Modify validation logic:**
   - Get entity_type for subject_id (query `entities` table if not already fetched)
   - If entity_type = "Person" AND rel_type = "age":
     - Reject if age < 0 or > 150
     - Log: `ingest.person_age_rejected_out_of_range` (info level)
   - Else (non-Person entity):
     - Reject if age < 0 only
     - Accept any age ≥ 0 (no upper limit)

4. **Improve "born on [date]" extraction (if time allows):**
   - Find date extraction in Filter LLM or ingest path
   - If pattern matches "born on [Month] [Day], [Year]":
     - Parse year directly
     - Calculate age as `2026 - year`
     - If LLM value differs significantly (> 10 years off), log warning and use calculated value
   - Example: "born on January 15, 1990" → year=1990 → age=36 (not 192)

5. **Test cases:**
   - Person age=36: accept ✓
   - Person age=192: reject, log warning ✓
   - Planet age=4500000000: accept ✓
   - Mountain age=50000000: accept ✓
   - Any entity age=-5: reject ✓

6. **Syntax check:** `python -m py_compile src/api/main.py` must pass

7. **Update scratch** with findings (template below)

### Deliverable:

- **`src/api/main.py`** — entity-type-aware age validation (replace line 1771 logic)
- **Improved date extraction** (if implemented) — parse "born on [date]" and calculate age correctly

### Files to Modify:

- `src/api/main.py` — age validation logic

### Success Criteria:

- Person ages: 0–150 validated ✓
- Non-Person ages: any non-negative accepted ✓
- "Born on January 15, 1990" → age=36, not 192 ✓
- Planet Earth age acceptable ✓
- Geological ages acceptable ✓
- Rejected ages logged (not silent failures) ✓
- Syntax clean ✓

### Upon Completion:

**If fix implemented successfully:**

Update scratch.md with this entry (COPY EXACTLY):
```
## ✓ DONE: dprompt-36b (Age Validation — Entity-Type-Aware) — 2026-05-10

**Problem:** dprompt-35b hard-rejected age > 150, breaking geological/astronomical entities
**Solution:** Entity-type-aware validation

**Implementation:**
- For Person entities: strict validation (age 0–150)
- For non-Person entities: accept any non-negative age
- Implausible ages logged for observability

**Results:**
- Person age=36: ✓ accepted
- Person age=192: ✓ rejected + logged
- Planet age=4.5B: ✓ accepted
- Mountain age=50M: ✓ accepted
- Date parsing: "born on January 15, 1990" → age=36 ✓ (if implemented)

**Next:** Rebuild docker image on truenas + re-test via pre-prod API (scenarios 2+4)
```

**If issues encountered:**

Update scratch.md with problem description and stop.

### CRITICAL NOTES:

- **Entity type matters.** Don't reject Planet Earth.
- **Person ages are special.** Only strict validation for Person entities.
- **Logging is important.** Rejected ages should be observable (info log, not silent skip).
- **Date parsing is nice-to-have.** If you find the LLM extraction bug (age=192 from year=1990), fix it. But entity-type-aware validation is the priority.

### Motivation:

Hard reject breaks legitimate data. Smart validation respects entity types. Person who's 36 years old? Accept. Planet that's billions of years old? Accept. Person who claims to be 192? Reject and log. That's production-ready validation.

