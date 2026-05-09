# Deepseek Implementation Prompt: Temporal Events Architecture

**Scope:** Build the `events` table and plumb it through ingest → extraction → query. Foundation for time-aware memory.

**Why:** Temporal facts (birthdays, appointments, anniversaries) have different semantics than static facts (spouse, parent_of). They need recurrence rules, expiry logic, and time-aware ranking. A dedicated `events` table separates concerns and enables future features like "what's coming up?" or "how long ago was that?"

---

## Design: Events Table

### Schema

```sql
CREATE TABLE events (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,  -- UUID or "user"
  object_id TEXT NOT NULL,   -- UUID or entity name
  event_type TEXT NOT NULL,  -- born_on, met_on, anniversary_on, appointment_on, etc.
  occurs_on TEXT NOT NULL,   -- date string as extracted ("may 3", "june 15, 1990", "15th")
  recurrence TEXT,           -- nullable: "yearly", "monthly", "once", null → infer from event_type
  confidence FLOAT DEFAULT 0.8,
  created_at TIMESTAMP DEFAULT now(),
  UNIQUE(user_id, subject_id, event_type),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX idx_events_user_subject ON events(user_id, subject_id);
CREATE INDEX idx_events_type ON events(user_id, event_type);
```

**Key fields:**
- `event_type`: rel_type analogue. Maps to ontology (born_on, anniversary_on, etc.)
- `occurs_on`: Raw date string from extraction ("may 3", "1990", "june 15, 2020"). No normalization yet.
- `recurrence`: Semantic tag for retrieval logic (yearly → surface every May; once → expire after date; null → infer from type)
- `UNIQUE(user_id, subject_id, event_type)`: One birthday per entity, one anniversary date, etc.

### Recurrence Rules (Hardcoded, No DB Config)

```python
_EVENT_RECURRENCE_DEFAULTS = {
    "born_on": "yearly",
    "born_in": "once",
    "anniversary_on": "yearly",
    "met_on": "once",
    "married_on": "once",
    "appointment_on": "once",
    # Extend as needed
}
```

---

## Part 1: Migration

Create `migrations/015_events_table.sql`:

```sql
CREATE TABLE events (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  object_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  occurs_on TEXT NOT NULL,
  recurrence TEXT,
  confidence FLOAT DEFAULT 0.8,
  created_at TIMESTAMP DEFAULT now(),
  UNIQUE(user_id, subject_id, event_type)
);

CREATE INDEX idx_events_user_subject ON events(user_id, subject_id);
CREATE INDEX idx_events_type ON events(user_id, event_type);
```

Run during startup like the other migrations.

---

## Part 2: Ingest Classification

Update `/ingest` endpoint logic to route temporal facts to events table instead of facts table.

### Define Temporal Rel_Types

In `src/api/main.py`, near the top:

```python
_TEMPORAL_REL_TYPES = {
    "born_on", "born_in",
    "anniversary_on", "met_on", "married_on",
    "appointment_on",
    # Extend as facts mature
}
```

### Routing Logic (in `/ingest` after WGM gate)

```python
# After WGM validation, before fact commitment:

for edge in validated_edges:
    event_type = edge.rel_type.lower()
    
    if event_type in _TEMPORAL_REL_TYPES:
        # Route to events table
        subject_id = await registry.resolve(user_id, edge.subject)
        object_id = edge.object  # object is date string for events
        
        try:
            cursor.execute("""
                INSERT INTO events (user_id, subject_id, object_id, event_type, occurs_on, confidence)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, subject_id, event_type)
                DO UPDATE SET occurs_on = EXCLUDED.occurs_on, confidence = EXCLUDED.confidence
            """, (user_id, subject_id, object_id, event_type, edge.object, edge.confidence))
            
            # Set recurrence based on defaults
            recurrence = _EVENT_RECURRENCE_DEFAULTS.get(event_type)
            if recurrence:
                cursor.execute("""
                    UPDATE events SET recurrence = %s 
                    WHERE user_id = %s AND subject_id = %s AND event_type = %s
                """, (recurrence, user_id, subject_id, event_type))
        except Exception as e:
            log.error(f"ingest.event_insert_failed", event_type=event_type, error=str(e))
    else:
        # Route to facts table (existing logic)
        ...
```

---

## Part 3: Qwen Prompt Update

Update `_TRIPLE_SYSTEM_PROMPT` in `openwebui/faultline_tool.py`.

Replace the DATES AND EVENTS section with clearer, simpler rules:

```
DATES AND EVENTS:
- NEVER emit spouse, met, married relationships as dates. Emit as separate facts FIRST.
  Example: "We met on June 15, 2020 and got married August 12, 2015"
    → (user, met_on, "june 15, 2020")
    → (user, spouse, person_name)
    → (user, married_on, "august 12, 2015")
- Birthday patterns ("born on X", "my birthday is Y", "X's birthday is Z"):
  emit {"subject":"<entity>","object":"<date>","rel_type":"born_on"}.
  Date formats: "may 3", "june 10, 1990", "15th", "1988", "may" (month only).
- Anniversary patterns ("our anniversary is X", "X's birthday is Y"):
  emit {"subject":"user" or entity,"object":"<date>","rel_type":"anniversary_on"}.
- Meeting dates ("we met on X"):
  emit {"subject":"user","object":"<date>","rel_type":"met_on"}.
- One-time events (appointments, deadlines) with future/past relevance:
  emit {"subject":"user","object":"<date>","rel_type":"appointment_on"}.
- Compound date+age ("I'm 25, born on May 3"):
  emit BOTH (user, age, "25") AND (user, born_on, "may 3").
- Corrections ("Actually born June 3, not May 3"):
  emit {"subject":"<entity>","object":"<date>","rel_type":"born_on","is_correction":true}.
- Fuzzy/partial dates ("sometime in 1990", "around May"):
  emit as-is with "low_confidence":true.
- Day-only patterns ("birthday is the 3rd"):
  emit "3rd" as the date.
- NEVER emit relative dates ("next week", "last month") — drop or mark "low_confidence":true.
```

---

## Part 4: Query Integration

Update `/query` endpoint to merge events results.

### Add Events Fetcher

```python
def _fetch_user_events(db, user_id: str, subject_id: str = None, event_types: list[str] = None) -> list[dict]:
    """
    Fetch events for user. Optionally filter by subject_id or event_types.
    Returns list of dicts: {id, subject_id, object_id, event_type, occurs_on, recurrence}.
    """
    query = "SELECT id, subject_id, object_id, event_type, occurs_on, recurrence FROM events WHERE user_id = %s"
    params = [user_id]
    
    if subject_id:
        query += " AND subject_id = %s"
        params.append(subject_id)
    
    if event_types:
        placeholders = ",".join(["%s"] * len(event_types))
        query += f" AND event_type IN ({placeholders})"
        params.extend(event_types)
    
    cursor = db.cursor()
    cursor.execute(query, params)
    results = cursor.fetchall()
    
    return [
        {
            "id": row[0],
            "subject_id": row[1],
            "object_id": row[2],
            "event_type": row[3],
            "occurs_on": row[4],
            "recurrence": row[5],
        }
        for row in results
    ]
```

### Merge Events into Query Response

In `/query`, after merging facts from baseline + graph + Qdrant:

```python
# Fetch events (same subject scope as facts)
events = _fetch_user_events(db, user_id, subject_id=canonical_identity)

# Convert events to fact-like dicts for consistent return structure
events_as_facts = [
    {
        "id": e["id"],
        "subject": preferred_names.get(e["subject_id"], e["subject_id"]),
        "object": e["object_id"],  # date string
        "rel_type": e["event_type"],
        "confidence": 0.9,  # events are high-confidence once stored
        "source": "events_table",
        "recurrence": e["recurrence"],
    }
    for e in events
]

# Merge with facts
merged_facts.extend(events_as_facts)
merged_facts = list({(f["subject"], f["object"], f["rel_type"]): f for f in merged_facts}.values())

return {
    "facts": merged_facts,
    "preferred_names": preferred_names,
    "canonical_identity": canonical_identity,
    "entity_attributes": entity_attributes,
}
```

---

## Part 5: Filter Integration

Update `openwebui/faultline_tool.py` to handle events in memory injection.

Events should be formatted naturally:
```python
# In _build_memory_block(), before injecting facts:

# Separate events from regular facts
events = [f for f in facts if f.get("source") == "events_table"]
regular_facts = [f for f in facts if f.get("source") != "events_table"]

# Format events with natural language
event_lines = []
for evt in events:
    recurrence = evt.get("recurrence", "once")
    rel_type = evt.get("rel_type", "")
    
    if recurrence == "yearly":
        event_lines.append(f"⭐ {evt['subject']}'s {rel_type.replace('_', ' ')}: {evt['object']} (annually)")
    else:
        event_lines.append(f"📅 {evt['subject']} {rel_type.replace('_', ' ')}: {evt['object']}")

# Inject event block before fact block
if event_lines:
    memory_text += "\n⊢ FaultLine Temporal\n" + "\n".join(event_lines) + "\n"

memory_text += "\n⊢ FaultLine Memory\n" + fact_lines
```

---

## Test Cases

1. **Simple birthday:** "I was born on May 3rd" → INSERT events (user, born_on, "may 3rd", recurrence="yearly")
2. **Other's birthday:** "Des was born March 15, 1990" → INSERT events (des_uuid, born_on, "march 15, 1990", recurrence="yearly")
3. **Anniversary:** "Our anniversary is June 20th" → INSERT events (user, anniversary_on, "june 20th", recurrence="yearly")
4. **Appointment:** "I have an appointment July 16, 2026" → INSERT events (user, appointment_on, "july 16, 2026", recurrence="once")
5. **Compound age+date:** "I'm 25, born on May 3rd" → INSERT facts (user, age, "25") AND events (user, born_on, "may 3rd")
6. **Correction:** "Actually born June 3, not May 3" → UPDATE events WHERE user_id=user AND subject_id=user AND event_type='born_on' SET occurs_on="june 3"
7. **Fuzzy date:** "born sometime in 1990" → INSERT events (user, born_on, "1990", low_confidence, recurrence="yearly")
8. **Query recall:** User asks "When was I born?" → `/query` merges events → Filter injects "⭐ user's born_on: may 3rd (annually)"
9. **Appointment expiry:** User asks "What's coming up?" → `/query` filters events by occurs_on > today
10. **Historical event:** "We met on June 15, 2020" → INSERT events (user, met_on, "june 15, 2020", recurrence="once") → used for "how long ago" queries

---

## Done When

- ✅ `migrations/015_events_table.sql` created and runs at startup
- ✅ `/ingest` routes temporal rel_types to events table
- ✅ Qwen prompt DATES AND EVENTS section rewritten (clear, unambiguous, no spouse conflation)
- ✅ `/query` fetches events and merges into response
- ✅ Filter formats events naturally in memory block
- ✅ All 10 test cases pass (ingest, query, memory injection)
- ✅ No regressions on non-temporal facts

Ship it.

---

## Future (Not Blocked)

- Temporal relevance scoring: rank upcoming events higher, age past events
- Recurrence expansion: "May 3" + recurrence="yearly" → suggest next occurrence
- Duration queries: "how long ago" parsed from past event dates
- Holiday/recurring entity support: "every birthday", "every anniversary"

These are Phase 8+. Foundation is Phase 7.
