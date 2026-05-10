# Validation: Birthday Persistence Across Chats (2026-05-10)

**Issue:** User provided birthday in Chat 1, asked "how old am I?" in Chat 2 (new session), system said "I don't know."

**Expected:** Birthday should be retrieved from RAG/PostgreSQL and injected into Chat 2 memory.

## Validate on truenas pre-prod

### 1. Check PostgreSQL

```sql
-- Does the birthday fact exist?
SELECT user_id, subject_id, rel_type, object_id FROM facts 
WHERE rel_type IN ('born_on', 'birthday') LIMIT 5;

-- Is it in entity_attributes (scalars)?
SELECT user_id, entity_id, attribute, value_text FROM entity_attributes 
WHERE attribute IN ('born_on', 'birthday', 'age') LIMIT 5;

-- What about events table (temporal)?
SELECT user_id, entity_id, event_type, event_date FROM events 
WHERE event_type = 'birthday' LIMIT 5;
```

### 2. Check Qdrant (RAG)

- Does the collection exist for the user? 
  ```bash
  curl -s http://qdrant:6333/collections | jq '.result'
  ```

- Does it have birthday points? 
  ```bash
  curl -s http://qdrant:6333/collections/{user_collection}/points?limit=50 \
    | jq '.result.points | map(.payload)' | grep -i birth
  ```

### 3. Check `/query` response in new chat

- Does `/query` return the birthday fact in its response?
- Is the relevance score above threshold (0.4)?
- Check Filter logs: Does "⊢ FaultLine Memory" header appear? What facts are included?

### 4. Root cause analysis

- **If birthday in PostgreSQL but not returned by `/query`** → graph traversal or relevance gate issue
- **If birthday in RAG but not returned** → Qdrant query issue or relevance gate issue
- **If birthday nowhere** → `/store_context` or initial extraction issue
- **If returned by `/query` but not injected** → Filter injection logic issue

## Report findings

Where is the birthday stored? (PostgreSQL facts / entity_attributes / events / Qdrant / nowhere)

What does `/query` return for a new chat with self-referential signal?

What's the confidence/relevance score on the birthday fact?

Is the Filter receiving it but filtering it out?
