# Deepseek Implementation Prompt: Integration Testing (Phase 7 + Validation)

**Scope:** Validate temporal events (Phase 7) and earlier phases work end-to-end. Write tests covering extraction → ingest → query → memory injection.

**Why:** Phases 1-7 have scattered test cases. Need unified validation: do date facts actually flow through the system? Do pronouns resolve correctly? Do temporal events inject naturally into memory?

---

## Test Scope

### Part 1: Temporal Events (Phase 7) — 10 tests

**Setup:** Fresh user session, clean events table.

1. **Extract birthday:**
   - Input: "I was born on May 3rd"
   - POST `/ingest` → should INSERT events(user, born_on, "may 3rd", recurrence="yearly")
   - POST `/query` → should return fact with source="events_table"
   - Verify: fact appears in memory block as "⭐ user's born_on: may 3rd (annually)"

2. **Extract anniversary:**
   - Input: "Our anniversary is June 20th"
   - Expected: events(user, anniversary_on, "june 20th", recurrence="yearly")
   - Verify: memory injection shows "⭐ user's anniversary_on: june 20th (annually)"

3. **Extract appointment:**
   - Input: "I have an appointment July 16, 2026"
   - Expected: events(user, appointment_on, "july 16, 2026", recurrence="once")
   - Verify: memory injection shows "📅 user appointment_on: july 16, 2026"

4. **Extract other's birthday:**
   - Input: "Des was born on March 15, 1990"
   - Expected: events(des_uuid, born_on, "march 15, 1990", recurrence="yearly")
   - Verify: `/query` resolves des → display name → memory shows "⭐ des's born_on: march 15, 1990 (annually)"

5. **Compound age + date:**
   - Input: "I'm 25, born on May 3rd"
   - Expected: facts(user, age, "25") AND events(user, born_on, "may 3rd")
   - Verify: both age and temporal event inject

6. **Correction:**
   - Input (turn 1): "I was born May 3rd"
   - Input (turn 2): "Actually born June 3, not May 3"
   - Expected: events table UPDATE occurs_on FROM "may 3rd" → "june 3"
   - Verify: `/query` returns only "june 3", not both dates

7. **Fuzzy date:**
   - Input: "born sometime in 1990"
   - Expected: events(user, born_on, "1990", low_confidence, recurrence="yearly")
   - Verify: event injects but marked low-confidence in memory

8. **Day-only pattern:**
   - Input: "My birthday is the 15th"
   - Expected: events(user, born_on, "15th", recurrence="yearly")
   - Verify: injects as "⭐ user's born_on: 15th (annually)"

9. **No relative dates:**
   - Input: "I'm going on vacation next week"
   - Expected: no event extracted (relative dates dropped per prompt)
   - Verify: `/query` returns nothing for temporal match

10. **Multiple events for same entity:**
    - Input (turn 1): "I was born May 3rd"
    - Input (turn 2): "Our anniversary is June 20th"
    - Expected: events(user, born_on, "may 3rd") AND events(user, anniversary_on, "june 20th")
    - Verify: `/query` returns both, memory shows both

---

### Part 2: Conversation State (Phase 5) — 5 tests

**Setup:** Multi-turn conversation, no cache resets.

1. **Pronoun resolution across turns:**
   - Turn 1: "My wife is Marla"
   - Turn 2: "What does she do?"
   - Expected: Turn 2 resolves "she" → marla_uuid → `/query` returns marla facts
   - Verify: LLM can answer "what does she do?" with marla's facts

2. **Entity mention tracking:**
   - Turn 1: "I have a dog named Fraggle"
   - Turn 2: "How old is it?"
   - Expected: Turn 2 resolves "it" → fraggle_uuid → `/query` returns fraggle's age
   - Verify: LLM answers "Fraggle is X years old"

3. **Context pruning (10 entity limit):**
   - Turn 1-12: Mention 12 different entities (wife, dog, son, daughter, coworker, etc.)
   - Turn 13: Ask about entity #2
   - Expected: Context pruned to last 10 entities, entity #2 may be lost
   - Verify: Pronoun resolution fails gracefully for pruned entities (falls back to Tier 2)

4. **Multiple pronouns in one turn:**
   - Turn 1: "Marla is my wife. Fraggle is our dog."
   - Turn 2: "How are she and it doing?"
   - Expected: "she" → marla, "it" → fraggle
   - Verify: Both facts inject for both entities

5. **Pronoun without prior mention:**
   - Turn 1: "What is she doing?"
   - Expected: No pronoun resolution (no prior mention), falls back to Tier 2 identity
   - Verify: Query returns generic user facts, not a specific entity

---

### Part 3: Relational Resolution (Phase 4) — 5 tests

**Setup:** User has facts: spouse=marla, has_pet=fraggle, parent_of=des.

1. **"My wife" resolution:**
   - Input: "How's my wife?"
   - Expected: `/query` resolves "wife" → spouse rel_type → marla_uuid → returns marla facts
   - Verify: Memory shows spouse facts (lives_at, works_for, etc.), not generic facts

2. **"My pet" resolution:**
   - Input: "Tell me about my pet"
   - Expected: `/query` resolves "pet" → has_pet rel_type → fraggle_uuid
   - Verify: Memory shows fraggle facts (species, age, etc.)

3. **"My son" resolution:**
   - Input: "How old is my son?"
   - Expected: `/query` resolves "son" → parent_of rel_type → des_uuid → returns age fact
   - Verify: LLM answers "Des is X years old"

4. **Fallback when relation doesn't exist:**
   - Input: "How's my boss?" (no boss fact exists)
   - Expected: Tier 1 resolution fails, falls back to Tier 2/3
   - Verify: Query returns generic user facts, graceful fallback

5. **Dynamic domain-agnostic resolution:**
   - Setup: user has fact (user, manages, team_uuid)
   - Input: "How's my team?"
   - Expected: `/query` dynamically resolves "team" → manages rel_type → team_uuid
   - Verify: Memory shows team facts (members, projects, etc.)

---

### Part 4: UUID Display Name Resolution (Phase 3) — 3 tests

1. **UUID → display name in facts:**
   - Setup: fact has subject_id=UUID, object_id=UUID in `/query` response
   - Expected: Filter calls `_resolve_display_names()` before memory block
   - Verify: Memory block shows "marla has_pet fraggle", NOT UUIDs

2. **Canonical identity UUID → "user":**
   - Setup: fact has subject_id=canonical_identity_UUID
   - Expected: Display name resolver converts to "user"
   - Verify: Memory shows "(user has_pet fraggle)", not UUID

3. **Missing display name fallback:**
   - Setup: fact references entity with no preferred alias
   - Expected: Falls back to UUID or raw entity_id
   - Verify: Memory injects gracefully (UUID or string, no crash)

---

### Part 5: End-to-End Flow — 3 integration tests

**Narrative tests: story-driven validation**

**Test E1: Birthday + Query + Memory**
```
Turn 1: "I was born on May 3rd, 1990"
  → `/ingest` extracts: (user, born_on, "may 3rd, 1990")
  → events table: INSERT (user_id, born_on, "may 3rd, 1990", yearly)
  
Turn 2: "When was I born?"
  → `/query` merges facts + events
  → Filter injects: "⭐ user's born_on: may 3rd, 1990 (annually)"
  → LLM answers: "You were born on May 3rd, 1990"
  
Verify: All 3 steps work (extraction, storage, recall)
```

**Test E2: Spouse + Temporal Facts + Pronouns**
```
Turn 1: "My wife Marla was born June 15, 1992"
  → `/ingest`: (user, spouse, marla) + (marla, born_on, "june 15, 1992")
  → facts table: spouse fact; events table: marla's birthday
  → context track: marla_uuid stored for pronouns
  
Turn 2: "When is her birthday?"
  → Pronoun "her" → marla_uuid via context
  → `/query` returns marla facts + marla's temporal event
  → Filter injects spouse fact + birthday event
  → LLM answers: "Marla was born on June 15, 1992"
  
Verify: Spouse resolution + temporal extraction + pronoun tracking + memory injection all work
```

**Test E3: Appointment + Relevance Scoring**
```
Turn 1: "I have a dentist appointment on July 16, 2026 at 2pm"
  → `/ingest`: (user, appointment_on, "july 16, 2026")
  → events table: appointment (recurrence="once")
  
Turn 2: "What do I have coming up?"
  → `/query` detects "coming up" signal
  → Merges facts + events
  → Scores events: future appointments high, past events low
  → Filter injects: "📅 user appointment_on: july 16, 2026"
  → LLM answers: "You have a dentist appointment on July 16, 2026"
  
Verify: Temporal event ranking by relevance signal (future > past)
```

---

## Test Infrastructure

### Command to Run

```bash
pytest tests/integration/test_temporal_events.py -v
pytest tests/integration/test_conversation_state.py -v
pytest tests/integration/test_relational_resolution.py -v
pytest tests/integration/test_display_names.py -v
pytest tests/integration/test_e2e.py -v
```

### Fixtures

```python
@pytest.fixture
def user_id():
    return "test_user_123"

@pytest.fixture
def clean_events_table(db):
    """Clear events table before each test"""
    cursor = db.cursor()
    cursor.execute("DELETE FROM events WHERE user_id = %s", (user_id,))
    db.commit()
    yield
    cursor.execute("DELETE FROM events WHERE user_id = %s", (user_id,))
    db.commit()

@pytest.fixture
def clean_facts_table(db):
    """Clear facts table before each test"""
    cursor = db.cursor()
    cursor.execute("DELETE FROM facts WHERE user_id = %s", (user_id,))
    db.commit()
    yield
    cursor.execute("DELETE FROM facts WHERE user_id = %s", (user_id,))
    db.commit()
```

### Test Template

```python
def test_extract_birthday(client, user_id, clean_events_table, clean_facts_table):
    # 1. Ingest
    resp = client.post("/ingest", json={"text": "I was born on May 3rd", "user_id": user_id})
    assert resp.status_code == 200
    
    # 2. Query
    resp = client.get(f"/query?user_id={user_id}&query=When%20was%20I%20born")
    assert resp.status_code == 200
    data = resp.json()
    events = [f for f in data.get("facts", []) if f.get("source") == "events_table"]
    assert len(events) == 1
    assert events[0]["event_type"] == "born_on"
    assert events[0]["object"] == "may 3rd"
    assert events[0]["recurrence"] == "yearly"
    
    # 3. Filter memory injection (mock or live)
    memory = filter.build_memory_block(data["facts"], ...)
    assert "⭐" in memory or "user's born_on" in memory
```

---

## Done When

- ✅ All 23 tests pass (10 temporal + 5 state + 5 relational + 3 display + 3 e2e)
- ✅ Tests cover happy path + fallback paths
- ✅ No regressions on existing functionality
- ✅ Manual OpenWebUI validation: birthdays, pronouns, appointments all work
- ✅ Edge cases documented (fuzzy dates, corrections, multi-turn context)

Ship it.

---

## Notes

- Tests assume clean DB state before each run
- E2E tests may require Docker + live API
- Manual OpenWebUI validation is separate but critical (human-in-loop)
- Temporal relevance scoring (future > past) is deferred to Phase 8

