# dBug-028: Family Query Returns Generic Response — Facts Not Injected

**Status:** OPEN  
**Severity:** CRITICAL  
**Date Reported:** 2026-05-15 23:45 UTC  
**User:** family-debug / faultline-test  

---

## Broken Behavior

**Query:** "Tell me about my family"

**Current (Broken):**
```
I can't provide information about your family. As an AI assistant, I don't have 
access to your personal relationships or know anyone outside of our conversation.
```

**Expected:**
```
Your spouse is emma. You have three children: charlie (age 19), bob, and alice. 
You have a computer named ${ENTITY}. You live at 156 Cedar Street S in ${LOCATION}.
```

---

## Facts Are In The System

- PostgreSQL: 29 active facts exist for this user
- `/query` endpoint: Returns 40 facts → 10 final hits (working correctly)
- Qdrant: Re_embedder reconciling successfully
- Database: Family structure complete (spouse, children, location, pet)

But: **LLM receives no facts in context**

---

## Expectations For DEEPSEEK

**Investigate & Report:**

1. **Is Filter loading?**
   - Check `openwebui/faultline_tool.py` — is inlet filter executing?
   - Add debug logging to confirm Filter runs on each message

2. **Is Filter calling `/query`?**
   - Add logging: fact retrieval from backend
   - Verify endpoint URL is correct
   - Check if `/query` request succeeds

3. **Is Filter injecting facts into system message?**
   - Add logging: facts returned by `/query`
   - Add logging: system message construction
   - Verify facts appear in the prompt sent to LLM

4. **What's the actual failure point?**
   - Logs from `openwebui/faultline_tool.py` 
   - Query logs showing Filter calls (or lack thereof)
   - Identify: retrieval failing, injection failing, or Filter not running

---

## Files to Check

- `openwebui/faultline_tool.py` — Filter code
- OpenWebUI container logs: `sudo docker logs open-webui --tail 100`
- FaultLine API logs: already visible (shows `/query` working)

---

## Definition of Fixed

This bug is FIXED when:
- User asks "tell me about my family"
- System returns facts about spouse emma, children charlie/bob/alice, location, and ${ENTITY}
- Facts injected into system message BEFORE LLM generates response
- Log shows Filter → `/query` call → facts returned → injected into prompt
