# dprompt-36 — Age Validation: Entity-Type-Aware & Context-Conscious

## Purpose

Replace the overly-simplistic age > 150 hard reject with a nuanced, entity-type-aware validation that handles person ages, geological ages, and other legitimate age values without breaking legitimate data.

## The Problem with dprompt-35b Fix

**Current fix:** Reject age < 0 or > 150 unconditionally.

**Why it's wrong:**
- Planet Earth is ~4.5 billion years old
- Geological entities (mountains, oceans, rocks) have ages in millions/billions
- Historical entities (civilizations, empires) have ages > 150 years
- Hard reject breaks all of these

**Real root cause:** Filter LLM extracted age=192 from "I was born on January 15, 1990". That's a **parsing/extraction bug**, not a data validation issue. Shouldn't be storing 192 for a person at all.

## Solution: Context-Aware Validation

### Approach 1: Entity-Type-Aware Limits
```python
# For Person entities only
if entity_type == "Person" and attribute == "age":
    if age < 0 or age > 150:
        reject("implausible person age")

# For Geological/Astronomical entities
elif entity_type in ["Planet", "Star", "Galaxy", "Mountain", "Ocean"]:
    if age < 0:
        reject("age cannot be negative")
    # else: accept any positive value

# For other entities
else:
    if age < 0:
        reject("age cannot be negative")
    # else: accept any positive value
```

### Approach 2: Source-Aware Validation
If extraction came from "born on [date]", validate date parsing:
- Extract year from date string
- Calculate age as `current_year - birth_year`
- Don't trust LLM's direct numeric extraction if it's implausible

### Approach 3: Hybrid (Recommended)
1. **For Person entities:** Validate age 0–150, reject outliers
2. **For non-Person entities:** Accept any non-negative age
3. **For "born on [date]" patterns:** Re-parse date, calculate age correctly, override LLM value if implausible

## Implementation

**File:** `src/api/main.py` (scalar storage path, around line 1771)

**Logic:**
```python
if rel_type == "age":
    entity_type = get_entity_type(subject_id)  # fetch from entities table
    
    if entity_type == "Person":
        # Person: strict validation
        if object_value < 0 or object_value > 150:
            log_rejection(f"implausible person age: {object_value}")
            return  # skip this fact
    else:
        # Non-person: accept non-negative
        if object_value < 0:
            log_rejection(f"negative age not allowed: {object_value}")
            return
    
    # Age passes validation, store it
    store_scalar_fact(...)
```

## Edge Cases to Handle

1. **"Born on January 15, 1990"** → Extract year (1990) → Calculate age (36) → Override LLM value if different
2. **"Planet Earth is 4.5 billion years old"** → Entity type = Planet → Accept > 150
3. **"I'm 256 years old"** → Entity type = Person → Reject (but log for review)
4. **"The mountain is 50 million years old"** → Entity type = Mountain → Accept

## Success Criteria

- Person age validation: 0–150 years only ✓
- Non-person age: any non-negative value accepted ✓
- "Born on [date]" patterns: date parsing overrides LLM value ✓
- No legitimate geological/historical/astronomical ages rejected ✓
- Implausible person ages logged and skipped ✓
- Test: "born on January 15, 1990" → age=36, not 192 ✓

