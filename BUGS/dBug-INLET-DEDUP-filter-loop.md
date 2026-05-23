# dBug-INLET-DEDUP: Filter Inlet Dedup Cache Failing — Infinite `/ingest` Loop

**Status:** 🔴 OPEN  
**Severity:** HIGH (blocks end-to-end testing, causes 45s timeouts)  
**Discovery Date:** 2026-05-20  
**Related:** dprompt-126 (unblocked by this issue, but blocks testing)

---

## Symptom

Filter inlet repeatedly calls `/ingest` with the SAME text message without respecting the dedup window. This causes:
- Infinite inlet loop (~1 call per second)
- Eventually times out waiting for LLM responses (45s timeout)
- Test fails on spouse message: `My spouse's name is Marla, she prefers emma`

**OpenWebUI Logs:**
```
[FaultLine Filter] inlet CALLED enabled=True debug=False
[FaultLine Filter] inlet CALLED enabled=True debug=True
[FaultLine Filter] user_id=[redacted] text='My spouse's name is Marla, she prefers emma'
[FaultLine Filter] /ingest: text='My spouse's name is Marla, she prefers emma'
[FaultLine Filter] user_id=[redacted] text='My spouse's name is Marla, she prefers emma'
[FaultLine Filter] /ingest: text='My spouse's name is Marla, she prefers emma'
[FaultLine Filter] user_id=[redacted] text='My spouse's name is Marla, she prefers emma'
[FaultLine Filter] /ingest: text='My spouse's name is Marla, she prefers emma'
... (repeated 20+ times in 5 seconds)
```

**Backend Logs:**
```
ingest.call_start call_count=270 has_edges=False has_text=True
ingest.call_start call_count=271 has_edges=False has_text=True
ingest.call_start call_count=272 has_edges=False has_text=True
... (call_count increments with every inlet call)
```

---

## Root Cause

The filter inlet deduplication mechanism is NOT preventing repeated processing of the same text within the 5-second window.

**Code:** `openwebui/faultline_function.py` lines 2145-2149

```python
_DEDUP_TRACKER: dict[str, tuple[int, float]] = {}
_DEDUP_WINDOW: float = 5.0  # seconds

# In inlet method:
_text_hash = hash(text)
_last = _DEDUP_TRACKER.get(user_id)
if _last and _last[0] == _text_hash and (_time.time() - _last[1]) < _DEDUP_WINDOW:
    return body  # Should skip processing
_DEDUP_TRACKER[user_id] = (_text_hash, _time.time())
```

**Expected behavior:** Same text within 5 seconds → return early (skip `/ingest`)  
**Actual behavior:** Same text → process every time (call `/ingest`)

---

## Investigation

1. **Dedup cache exists:** `_DEDUP_TRACKER` is module-level dict, persists across inlet calls ✅
2. **Time check logic:** `(_time.time() - _last[1]) < _DEDUP_WINDOW` looks correct
3. **Hash consistency:** `hash(text)` should be consistent for same text
4. **Hypothesis:** Either:
   - Hash is changing between calls (text is being modified)
   - Time check is failing (time not advancing or overflow)
   - Cache is being cleared between calls
   - Old filter version is still running (disable/re-enable didn't work)

---

## Impact

- **Blocks comprehensive family pipeline test** (times out on 2nd message)
- **Causes wasteful LLM inference** (redundant extraction calls)
- **Wastes re_embedder cycles** (processes duplicate staged facts)
- **Affects user experience** (slow response time on repeated messages)

---

## Reproduction Steps

1. Run comprehensive_family_pipeline_test.sh
2. First message passes (3-6s): "My name is John, I prefer to be called ${USER}"
3. Second message times out (45s): "My spouse's name is Marla, she prefers emma"
4. Check OpenWebUI logs: inlet is called 20+ times with identical text

---

## Workaround

None currently. Temporary fix: increase `_DEDUP_WINDOW` to 60+ seconds, but this masks the issue.

---

## Proposed Fix

1. **Add debug logging** to understand why dedup isn't working:
   - Log when dedup check fires (matched hash + within window)
   - Log when dedup check fails (new hash or time expired)
   - Log tracker state at each inlet call

2. **Verify hash consistency:**
   - Add logging to see if `hash(text)` is the same for repeated calls
   - Check if text is being modified somewhere

3. **Verify time checks:**
   - Ensure `_time.time()` is advancing correctly
   - Check for integer overflow (unlikely in Python)

4. **Test filter reload:**
   - Verify disable/re-enable actually reloads the filter code
   - Check if old filter version is persisting in OpenWebUI memory

5. **If dedup is disabled somewhere:**
   - Check if there's a valve or config that disables dedup
   - Verify dedup logic isn't being bypassed elsewhere

---

## Questions for Investigation

1. Is the text hash changing between calls? (log `_text_hash`)
2. Is the time check passing? (log `_time.time() - _last[1]` vs `_DEDUP_WINDOW`)
3. Is the cache being cleared? (log `len(_DEDUP_TRACKER)` at each inlet call)
4. Is the old filter code still running? (check if the latest filter reload worked)

---

## Related Issues

- dprompt-126 (UNRELATED — blocked by this issue for testing, but not caused by it)
- dBug-STREAMING-TIMEOUT (similar symptom, but different root cause)

---

## Next Steps

1. Add debug logging to inlet dedup check
2. Run test and capture logs
3. Analyze why dedup isn't matching
4. Fix root cause
5. Verify comprehensive test passes

---

**Reported By:** Claude Code (Haiku 4.5)  
**Date:** 2026-05-20  
**Environment:** Pre-prod (docker-host docker-host.helpalicekpro.ca)
