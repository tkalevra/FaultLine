# dprompt-39b — Filter Prompt Deployment & Re-Validation [PROMPT]

## #deepseek NEXT: dprompt-39b — Deploy Filter Prompt & Re-Test Scenarios 3 & 5 — 2026-05-10

### Task:

Deploy the enhanced Filter prompt (dprompt-38b) to the OpenWebUI container on truenas, then re-test scenarios 3 & 5 to validate system metadata and transitive relationship extraction now works.

### Context:

dprompt-38b investigation identified two root causes:
1. **Filter prompt** (openwebui/faultline_tool.py) doesn't ask LLM to extract system metadata or transitive relationships — **FIXED locally in lines 163–178** (SYSTEM METADATA + TRANSITIVE RELATIONSHIPS sections added)
2. **Ontology** was missing system property rel_types — **DEPLOYED to truenas DB** (has_ip, has_os, has_hostname added)

The Filter prompt fix is LOCAL ONLY. Pre-prod container still runs old prompt, which is why scenarios 3 & 5 still fail despite ontology fix. Deploy the new prompt, rebuild OpenWebUI container, re-test.

### Constraints (CRITICAL):

- **Wrong: Assume the prompt changes are already live. They're not.**
- **Right: Commit local changes, rebuild OpenWebUI container on truenas with new prompt.**
- **MUST: Deploy Filter prompt to container, do NOT skip the rebuild.**
- **MUST: Re-test both scenarios 3 & 5. Both must now return expected results.**
- **MUST: Database state verified after each scenario (curl /query + inspect facts table).**
- **MUST: Zero code regressions. Run full test suite after rebuild.**

### Sequence (DO NOT skip or reorder):

1. **Verify Filter prompt is ready locally:**
   - Read `openwebui/faultline_tool.py` lines 163–178
   - Confirm SYSTEM METADATA section (7 patterns) is present ✓
   - Confirm TRANSITIVE RELATIONSHIPS section (4 patterns) is present ✓

2. **Commit local changes:**
   ```bash
   git add openwebui/faultline_tool.py src/api/main.py dprompt-38.md dprompt-38b.md scratch.md
   git commit -m "feat: system metadata & transitive relationship extraction

   Enhanced Filter prompt with two new extraction sections:
   - SYSTEM METADATA: has_ip, has_os, has_hostname, fqdn, has_ram, has_storage, expires_on
   - TRANSITIVE RELATIONSHIPS: knows, friend_of, met, related_to

   Added 6 system property rel_types to ontology seed list.
   
   dprompt-38b investigation confirmed LLM understands the data but prompt
   wasn't asking for structured extraction. Filter prompt now instructs extraction.
   
   Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>"
   ```

3. **Push to origin/master:**
   ```bash
   git push origin master
   ```

4. **Connect to truenas and rebuild OpenWebUI container:**
   ```bash
   ssh truenas -x "cd /mnt/tank/docker/openwebui && git pull origin master && docker-compose up --build -d openwebui"
   ```
   - Wait for container to start (~30s)
   - Verify: `curl -s http://localhost:3000/api/models | jq '.data[] | select(.id == "faultline-wgm-test-10")'`

5. **Re-test Scenario 3 (System Metadata):**
   ```bash
   # Ingest
   curl -X POST -H "Authorization: Bearer sk-addb2220bf534bfaa8f78d96e6991989" \
     -H "Content-Type: application/json" \
     -d '{"model":"faultline-wgm-test-10","messages":[{"role":"user","content":"My laptop is named Workstation-X, IP 192.168.1.100, OS Ubuntu 22.04"}],"stream":false}' \
     https://hairbrush.helpdeskpro.ca/api/chat/completions
   
   # Wait 2s
   
   # Query
   curl -X POST -H "Authorization: Bearer sk-addb2220bf534bfaa8f78d96e6991989" \
     -H "Content-Type: application/json" \
     -d '{"model":"faultline-wgm-test-10","messages":[{"role":"user","content":"Tell me about my computer"}],"stream":false}' \
     https://hairbrush.helpdeskpro.ca/api/chat/completions
   
   # Database verify (via SSH)
   ssh truenas -x "sudo docker exec faultline-postgres psql -U faultline -d faultline_test -c 'SELECT COUNT(*) FROM facts WHERE rel_type IN (\"has_ip\", \"has_os\", \"has_hostname\");'"
   ```
   - **Expected:** Query response mentions Workstation-X (or system facts), system facts stored (≥3 rows in facts table)
   - **PASS/FAIL:** ___

6. **Re-test Scenario 5 (Transitive Relationships):**
   ```bash
   # Ingest
   curl -X POST -H "Authorization: Bearer sk-addb2220bf534bfaa8f78d96e6991989" \
     -H "Content-Type: application/json" \
     -d '{"model":"faultline-wgm-test-10","messages":[{"role":"user","content":"My friend Alice knows my sister Sarah"}],"stream":false}' \
     https://hairbrush.helpdeskpro.ca/api/chat/completions
   
   # Wait 2s
   
   # Query
   curl -X POST -H "Authorization: Bearer sk-addb2220bf534bfaa8f78d96e6991989" \
     -H "Content-Type: application/json" \
     -d '{"model":"faultline-wgm-test-10","messages":[{"role":"user","content":"Who knows my family"}],"stream":false}' \
     https://hairbrush.helpdeskpro.ca/api/chat/completions
   
   # Database verify
   ssh truenas -x "sudo docker exec faultline-postgres psql -U faultline -d faultline_test -c 'SELECT COUNT(*) FROM facts WHERE rel_type IN (\"knows\", \"friend_of\", \"met\");'"
   ```
   - **Expected:** Query response mentions Alice and/or Sarah (connected via "knows"), relationship facts stored (≥1 row in facts table)
   - **PASS/FAIL:** ___

7. **Run full test suite locally:**
   ```bash
   pytest tests/api/ --ignore=tests/evaluation -v
   ```
   - Expected: 112+ passed, 0 regressions

8. **Update scratch.md** with results (use template below)

### Deliverable:

- **Deployment verified:** Filter prompt deployed to OpenWebUI container ✓
- **Re-test results:** Both scenarios 3 & 5 documented (PASS/FAIL + database state) ✓
- **Test suite:** 112+ passed, 0 regressions ✓
- **Scratch.md updated** with completion entry ✓

### Success Criteria:

- Filter prompt deployed to OpenWebUI container ✓
- Scenario 3 (System Metadata): Query returns system facts OR gracefully missing ✓
- Scenario 5 (Transitive Relationships): Query returns relationships OR gracefully missing ✓
- No UUID leaks ✓
- Full test suite passes, 0 regressions ✓

### Upon Completion:

**If both scenarios now pass:**

Update scratch.md with:
```
## ✓ DONE: dprompt-39b (Filter Prompt Deployment & Re-Validation) — 2026-05-10

**Deployment:**
- Filter prompt (SYSTEM METADATA + TRANSITIVE RELATIONSHIPS sections) deployed to OpenWebUI container ✓
- Container rebuilt on truenas, faultline-wgm-test-10 model updated ✓

**Re-test Results:**

### Scenario 3 (System Metadata): [PASS/FAIL + findings]
- Ingest: "My laptop is named Workstation-X, IP 192.168.1.100, OS Ubuntu 22.04"
- Query: [response excerpt]
- Database: [fact count] system property facts stored
- Result: [PASS / PARTIAL / FAIL + explanation]

### Scenario 5 (Transitive Relationships): [PASS/FAIL + findings]
- Ingest: "My friend Alice knows my sister Sarah"
- Query: [response excerpt]
- Database: [fact count] transitive relationship facts stored
- Result: [PASS / PARTIAL / FAIL + explanation]

**Validations:**
- Zero UUID leaks ✓
- Full test suite: 112+ passed, 0 regressions ✓
- System is production-ready. All 5 scenarios fully pass.

Next: Commit to git and proceed to production deployment.
```

**If any scenario fails:**

Update scratch.md with:
```
## ❌ FAILED: dprompt-39b (Filter Prompt Deployment) — Scenario X still failing

**Deployment:** Filter prompt deployed ✓
**Re-test Scenario X:** Still failing

**Input:** [what was sent]
**Expected:** [what should happen]
**Actual:** [what happened]
**Database state:** [relevant queries + results]
**Assessment:** [Filter prompt issue / Model behavior / Other]

Awaiting direction.
```

### CRITICAL NOTES:

- **Filter prompt is the blocking factor.** If it's not deployed to the container, scenarios 3 & 5 will still fail.
- **Container rebuild is required.** Changing openwebui/faultline_tool.py requires docker rebuild to be effective.
- **Both scenarios must improve.** If they're identical to dprompt-37b results, the prompt isn't live yet.
- **Database state matters.** Verify system property facts are actually stored, not just conversationally acknowledged.

### Motivation:

dprompt-38b identified code issues, not model limitations. The Filter prompt wasn't asking for system metadata or transitive relationship extraction. Enhanced prompt + rebuilt container = LLM now has explicit instructions. That's the difference between conversational understanding and structured extraction.

Scenarios 3 & 5 should now pass.
