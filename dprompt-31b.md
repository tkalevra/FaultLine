# dprompt-31b — Live Pipeline Debugging: Gabriella Ingest Failure [PROMPT]

## #deepseek NEXT: dprompt-31b — Live Pipeline Debugging (Gabriella Missing from Query) — 2026-05-10

### Task:
Debug why Gabriella/Gabby doesn't persist through the ingest pipeline. Test against live OpenWebUI instance, capture logs, identify failure point in Filter → ingest → query path.

### Context:

dprompt-30b marked the system PRODUCTION-READY (all 15 QA scenarios passed). But live testing found a regression:

**User prompt:** "We have a third Daughter, Gabriella who's 10 and goes by Gabby"

**Filter response:** LLM acknowledges her: "Got it, Des! It sounds like your family is even closer than I realized with the addition of Gabriella, or "Gabby," who is 10 years old."

**Query result:** Missing her. Query returns only "Mars (spouse), Cyrus and Desmonde" — no Gabriella.

**Expected:** Gabriella facts should persist and be queryable like the other family members.

This is a real pipeline failure. Unit tests passed but live testing breaks. Need to debug the actual running system to identify where Gabriella is lost.

### Constraints (CRITICAL):

- **Wrong: Modify code, add features, change database, fix bugs**
- **Right: Test, debug, gather logs, identify failure point, document findings**
- **MUST: ONLY use model `faultline-wgm-test-10`. Do NOT test other models.**
- **MUST: Bearer token for hairbrush.helpdeskpro.ca: `sk-addb2220bf534bfaa8f78d96e6991989`**
- **MUST: Do NOT commit any changes. This is debugging only.**
- **MUST: Gather all logs. Paste full logs/findings in scratch upon completion.**
- **MUST: If you find the root cause, document it clearly. Do NOT attempt fixes.**
- **MUST: SSH to truenas for docker logs: `ssh truenas -x "sudo docker logs -f open-webui"` — capture while testing**

### Sequence (DO NOT skip):

1. Read dprompt-31.md carefully (all debug paths, log collection points)
2. Open new conversation in OpenWebUI (https://hairbrush.helpdeskpro.ca/?models=faultline-wgm-test-10)
3. Start docker log capture in background:
   ```bash
   ssh truenas -x "sudo docker logs -f open-webui" > gabriella_debug.log &
   ```
4. **Ingest phase:**
   - Prompt: "We have a third Daughter, Gabriella who's 10 and goes by Gabby"
   - Wait for Filter response
   - Observe what the Filter extracts (check logs)
5. **Query phase:**
   - Prompt: "tell me about my family"
   - Observe result (should include Gabriella but doesn't)
6. **Database inspection:**
   ```sql
   -- SSH to truenas, connect to faultline-postgres
   SELECT COUNT(*) FROM facts WHERE object_id ILIKE '%gabriella%';
   SELECT COUNT(*) FROM staged_facts WHERE object_id ILIKE '%gabriella%';
   SELECT * FROM entities WHERE entity_type = 'Person' ORDER BY created_at DESC LIMIT 10;
   ```
7. Stop docker logs: `kill %1`
8. Review logs and determine failure point:
   - **Filter didn't extract her?** (LLM issue)
   - **Ingest rejected her?** (WGM/classification)
   - **Query filtered her?** (retrieval/scoring)
   - **Embedding failed?** (Qdrant sync)
9. Document complete findings in scratch

### Deliverable:

- Complete docker logs (gabriella_debug.log)
- Database query results (facts count, staged_facts count, entities list)
- Clear identification of failure point:
  - Stage: Filter / Ingest / Query / Qdrant
  - Exact error or missing step
- Root cause hypothesis (not fix, just hypothesis)
- Any error messages or warnings from logs

### Files to Modify:

- **Create:** gabriella_debug.log (log capture — commit to git for reference)
- **NO code changes.** This is debugging only.

### Success Criteria:

- Docker logs fully captured ✓
- Gabriella facts checked in PostgreSQL ✓
- Failure point identified ✓
- Root cause clear from logs ✓
- Findings documented in scratch ✓

### Upon Completion:

**Update scratch.md with this entry (COPY EXACTLY):**
```
## #claude: dprompt-31b Results — Gabriella Ingest Debug

**Failure point identified:**
[Stage where Gabriella is lost: Filter / Ingest / Query / Qdrant]

**Root cause:**
[What specifically is happening: LLM not extracting, WGM rejecting, query filtering, etc.]

**Evidence:**
[Key log lines or database query results that prove it]

**Logs:**
[gabriella_debug.log captured and saved]

**Next:** [Awaiting direction on fix or further investigation]
```

Then STOP. Do not propose fixes. Wait for direction in scratch.

### CRITICAL NOTES:

- **This is live debugging, not unit testing.** You're looking at real logs from the actual running system.
- **Do NOT modify code.** Your job is to find the bug, not fix it.
- **Model lock is strict:** Only `faultline-wgm-test-10`. Do NOT switch models.
- **Logs are critical.** Full docker output is your evidence. Paste key sections in scratch findings.
- **Database state matters.** Check if Gabriella facts exist in PostgreSQL at all. That tells us if it's extraction/ingest or retrieval.

### Motivation:

The unit tests said everything is fine. Real usage says otherwise. This is a perfect example of why live testing catches what unit tests miss. Your job: be the bridge between the two. Find the gap.
