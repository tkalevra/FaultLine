# dBug-026: Class C Routing Verification Incomplete — Rejection Logging Working, Staging Unclear

**Severity:** Medium — logging works, but Class C routing to staged_facts needs verification

**Status:** INVESTIGATION (Testing DEEPSEEK-27A results)

**Date:** 2026-05-15

**User Context:** User, test user <user_uuid>

## Summary

**DEEPSEEK-27A implementation complete** (dprompt-88 edge logging + Class C routing).

**Testing via docker-host.<domain> (OpenWebUI Filter):**

✅ **Rejection logging working:**
```
entity_registry.resolve_rejected name=called reason='failed entity name validation'
entity_registry.resolve_rejected name=computer reason='failed entity name validation'
entity_registry.resolve_rejected name=it reason='failed entity name validation'
entity_registry.resolve_rejected name=very reason='failed entity name validation'
```

✅ **Code in place** — `_commit_rejected_edge_to_qdrant()` exists, line 2038+, called at lines 2448, 2465, 3067

⚠️ **Class C routing verification incomplete:**
- Database check: No Class C (confidence=0.4, fact_class='C') facts in staged_facts for test user
- Query: `SELECT ... FROM staged_facts WHERE user_id='10d7d879-...' AND fact_class='C'` returns 0 rows
- Other users have Class C facts (confidence=0.4) in staged_facts, so table is working

❌ **Direct `/ingest` endpoint broken:**
- `curl POST http://localhost:8001/ingest` returns "Internal Server Error"
- Logs don't show detailed error trace
- Possible issue with recent changes or missing request parameter

## Questions

1. **Are rejected edges actually being committed to staged_facts?**
   - Logs show rejections, but database shows no Class C entries for this user
   - Possible: edges rejected but not routed, function not called, or transaction rolled back

2. **Is direct `/ingest` endpoint supposed to work?**
   - Filter uses it (via openwebui/faultline_tool.py)
   - Direct testing fails with 500 error

## Test Evidence

**Pre-prod database query (2026-05-15 23:00 UTC):**
```sql
SELECT COUNT(*) FROM staged_facts 
WHERE user_id='<user_uuid>' 
AND fact_class='C';
-- Result: 0 rows
```

**Test message sent:**
```
"My son is named Charlie and my computer is called Nexus. It is very fast."
```

**Expected behavior:**
- Subject "it" → rejected (pronoun) → logged ✓
- Object "computer" → rejected (type label) → logged ✓
- Edges routed to staged_facts with confidence=0.4 → **NOT VERIFIED**

## Next Steps

1. **Verify Class C routing:**
   - Add explicit logging to `_commit_rejected_edge_to_qdrant()` to confirm it's being called
   - Check if database transaction is committing
   - Check if there's a filtering issue (data stored but not visible)

2. **Fix `/ingest` endpoint:**
   - Check error logs with full traceback
   - Determine if it's a deepseek implementation issue or pre-existing

3. **Database cleanup (Phase 3):**
   - Can't proceed until Class C routing verified
   - Need to confirm rejected edges are actually being stored before running cleanup + fresh validation
