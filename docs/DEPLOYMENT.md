# FaultLine Deployment Guide

## Architecture: Write-Validated Knowledge Graph Pipeline

FaultLine is the **single source of truth** (port 8000). Filter is intentionally dumb — all validation happens in the backend.

```
OpenWebUI Filter (dumb)
  ├─ Extract corrections via regex (explicit user signals)
  └─ Send {text, corrections} to FaultLine:8000

FaultLine /ingest orchestrates the COMPLETE pipeline:
  1. Call /extract/rewrite (LLM-based triple inference)
  2. Merge with provided corrections (user overrides LLM)
  3. Validate through WGMValidationGate (ontological mapping)
  4. Detect semantic conflicts (auto-supersede type/ownership)
  5. Validate bidirectional relationships (prevent child_of + parent_of)
  6. Classify as A/B/C (identity, behavioral, ephemeral)
  7. Commit to PostgreSQL + Qdrant

Filter also calls:
  ├─ /query (before LLM sees message, inject facts)
  └─ /retract (handle "forget", "delete", etc.)
```

**Benefits:**
- All ontological validation in one place (/ingest), not scattered
- Filter has no direct LLM dependency
- Corrections properly override LLM inference
- Follows CLAUDE.md principle: "Filter is dumb, backend is smart"
- No OpenWebUI internal API dependency (brittleness eliminated)

---

## Quick Start (5 minutes)

### Prerequisites
- Docker & Docker Compose installed
- PostgreSQL 16+
- Qdrant vector database
- OpenWebUI v0.9.5+

### Standard Setup (Recommended)

1. **Start the FaultLine backend:**
   ```bash
   docker compose up -d
   ```

2. **Verify health:**
   ```bash
   curl http://localhost:8000/health
   ```
   Should return `"status": "ok"`

3. **Install the Filter in OpenWebUI:**
   - Go to OpenWebUI → Tools → Create new tool
   - Select "Filters"
   - Paste content from `openwebui/faultline_function.py`
   - Click "Save"

4. **Configure Filter (OpenWebUI → Tools → FaultLine Filter → Valves):**

   **Step 1: Set FAULTLINE_URL (CRITICAL)**
   - **Docker setup:** Set `FAULTLINE_URL` to `http://faultline:8000` (service name)
   - **Local setup:** Set `FAULTLINE_URL` to `http://localhost:8000`
   
   **Step 2: Enable features:**
   - `ENABLED`: ✓ True
   - `INGEST_ENABLED`: ✓ True (learn facts)
   - `QUERY_ENABLED`: ✓ True (use facts in conversation)
   
   **Step 3: Leave everything else at default:**
   - **All LLM settings (LLM_URL, LLM_MODEL, LLM_API_KEY, BACKEND_LLM_URL):** LEAVE EMPTY
   - FaultLine now handles LLM selection internally (reads from environment variables)
   - All other settings: Use defaults

5. **Test it:**
   ```
   User: "My name is Christopher, I prefer Chris"
   System: [Extracts and stores facts]
   User: "What's my name?"
   System: [Uses stored fact to respond with "Christopher, but you prefer Chris"]
   ```

---

## Configuration Reference

### FAULTLINE_URL
**What it is:** The location of the FaultLine backend API  
**Standard value:** `http://localhost:8000`  
**Docker value:** `http://faultline:8000` (service name in docker-compose)  
**Remote value:** `http://your-hostname:8000`  
**When to change:** Only if FaultLine is on a different host/port

### LLM_URL
**DEPRECATED** — No longer used. FaultLine calls LLM directly, not OpenWebUI.  
**Standard value:** **LEAVE EMPTY**  
**Note:** Kept for backwards compatibility only. Can be safely ignored.

### LLM_MODEL
**DEPRECATED** — No longer used. FaultLine reads WGM_LLM_MODEL from environment.  
**Standard value:** **LEAVE EMPTY**  
**Note:** Kept for backwards compatibility only. Can be safely ignored.

### LLM_API_KEY
**DEPRECATED** — No longer used. FaultLine manages LLM authentication internally.  
**Standard value:** **LEAVE EMPTY**  
**Note:** Kept for backwards compatibility only. Can be safely ignored.

### BACKEND_LLM_URL
**DEPRECATED** — No longer used. FaultLine reads its LLM configuration from environment.  
**Standard value:** **LEAVE EMPTY**  
**Note:** Kept for backwards compatibility only. Can be safely ignored.

**New approach:**
FaultLine backend reads LLM configuration from environment variables:
- `LLM_BACKEND_TYPE`: protocol (`openwebui` / `ollama` / `lm_studio` / `openai` / `anthropic` / …)
- `LLM_BASE_URL`: host + port only (e.g. `http://host.docker.internal:11434`); the API path is appended automatically
- `LLM_API_KEY`: bearer/API key (blank for local servers)
- `WGM_LLM_MODEL`: Model name (e.g., `qwen2.5`)

Set these in docker-compose.yml or kubernetes manifests, not in OpenWebUI valves.

### ENABLE_DEBUG
**What it is:** Detailed logging for troubleshooting  
**When to enable:** If facts aren't being extracted or injected  
**Where logs appear:** `docker logs open-webui` (search for `[FaultLine]`)

### MAX_MEMORY_SENTENCES
**What it is:** Maximum facts to inject per conversation  
**Default:** 20  
**When to reduce:** If hitting token limits or slowing down responses

### MIN_INJECT_CONFIDENCE
**What it is:** Minimum quality threshold for facts (0.0–1.0)  
**Default:** 0.5 (medium confidence)  
**Increase to:** 0.7–0.9 for stricter, higher-quality facts  
**Decrease to:** 0.3–0.4 for more inclusive, lower-quality facts

---

## Logging Levels & Troubleshooting Configuration

FaultLine uses Python's standard `logging` module with `structlog` for structured output. All logging is controlled via environment variables, making it easy to adjust verbosity without code changes.

### Global Log Level

Set the `LOG_LEVEL` environment variable in docker-compose.yml or your deployment configuration:

```yaml
# docker-compose.yml
services:
  faultline:
    environment:
      LOG_LEVEL: INFO  # or DEBUG, WARNING, ERROR, CRITICAL
```

**Standard Levels (in order of verbosity):**

| Level | Use Case | What Gets Logged |
|-------|----------|-----------------|
| `DEBUG` | Development, deep troubleshooting | Everything including function entry/exit, variable inspection, query plans |
| `INFO` | Production (default) | Major milestones (extraction success, ingest completion, promotion, errors) |
| `WARNING` | Strict deployments | Only unexpected conditions (failed retries, fallbacks, missing optional data) |
| `ERROR` | Silent operation | Only critical failures (crashes, data loss, validation failures) |
| `CRITICAL` | Hardened production | Only system-breaking failures (database unavailable, essential service down) |

### Default Production Setting

```bash
LOG_LEVEL=INFO  # Balanced — logs important events without spam
```

### Development & Troubleshooting Setting

```bash
LOG_LEVEL=DEBUG  # Verbose — every function call, every query, every decision
```

### Filter (OpenWebUI) Logging

The Filter (`openwebui/faultline_function.py`) has its own logging control via the `ENABLE_DEBUG` valve in OpenWebUI:

```
OpenWebUI → Tools → FaultLine Filter → ENABLE_DEBUG = True
```

When enabled, Filter logs appear in OpenWebUI logs:
```bash
docker logs open-webui | grep "\[FaultLine\]"
```

### Backend Logging Output

**All FaultLine backend logs go to:**
```bash
docker logs faultline  # Standard output from FastAPI/uvicorn
```

**Search for specific events:**
```bash
# Extraction events
docker logs faultline | grep "extract_rewrite"

# Ingest pipeline
docker logs faultline | grep "extract_rewrite\|wgm_gate\|fact_store\|commit"

# Query operations
docker logs faultline | grep "query_user_facts"

# GLiNER2 operations
docker logs faultline | grep "gliner2"

# Re-embedder operations
docker logs faultline | grep "re_embedder"

# Type validation
docker logs faultline | grep "validate_triple\|entity_type"

# Errors only
docker logs faultline | grep "ERROR\|CRITICAL\|Exception"
```

### Key Log Patterns (Debug Mode)

When `LOG_LEVEL=DEBUG`, watch for these patterns to understand pipeline flow:

**Extraction Phase:**
```
extract_rewrite: entities_needing_types_check
  └─ entities_to_type=5, triples_count=3
gliner2_entity_extraction: entities_extracted=4
  └─ entities_needed=5, entities_extracted=4 (1 miss)
extract_rewrite: types_enriched
  └─ entity_count=4, scalar_rel_types=12
```

**Validation Phase:**
```
validate_triple_against_metadata: matched_rel_type
  └─ rel_type=has_pet, confidence=0.8
_validate_hierarchy_membership: check_passed
  └─ entity_type=Animal matches taxonomy member_entity_types
```

**Ingest Phase:**
```
fact_classification: assigned_class
  └─ class=B, confidence=0.8, rel_type=has_pet
FactStoreManager.commit: fact_stored
  └─ id=uuid, rel_type=has_pet, status=committed
```

**Query Phase:**
```
query_user_facts: found_facts
  └─ count=5, includes_staged=true
_graph_traverse: traversal_result
  └─ rel_type=spouse, hops=1, matches=1
_hierarchy_expand: expansion_result
  └─ rel_type=instance_of, direction=up, chain_length=3
```

**Re-Embedder Phase:**
```
re_embedder.promote_staged_facts: promoted
  └─ count=2, new_facts=2
re_embedder.evaluate_ontology_candidates: approved
  └─ rel_type=friend_of, frequency=4
```

### Module-Specific Debugging (Advanced)

To log ONLY specific modules (Python):

```python
# In src/api/main.py or src/re_embedder/embedder.py
import logging
logging.getLogger("src.api.main").setLevel(logging.DEBUG)
logging.getLogger("src.wgm.gate").setLevel(logging.DEBUG)
logging.getLogger("src.re_embedder.embedder").setLevel(logging.INFO)
```

But simpler to use environment variable (recommended):

```bash
LOG_LEVEL=DEBUG  # Global, easy to toggle
```

### Performance Impact

**Log Level Performance Overhead:**
- `ERROR`: <1% (production standard)
- `WARNING`: <2% (slightly more checks)
- `INFO`: ~2-5% (default balance)
- `DEBUG`: ~5-15% (verbose, slower for high-volume)

**Recommendation:**
- **Production:** `INFO` or `WARNING` (balanced)
- **Development:** `DEBUG` (full visibility)
- **Incident investigation:** `DEBUG` then `INFO` after resolution

### Health Check Logging

The `/health` endpoint is designed to NOT spam logs. It returns status without logging for common cases:

```bash
curl http://localhost:8000/health
# Returns: {"status": "ok", "database": "ok", "qdrant": "ok", ...}
# Logs nothing unless a component fails
```

This keeps logs clean even in high-volume deployments.

---

## Troubleshooting

### "Facts aren't being extracted"

**Check 1: Is the Filter enabled?**
```
OpenWebUI → Tools → FaultLine Filter → ENABLED = True
```

**Check 2: Enable debug logging**
```
OpenWebUI → Tools → FaultLine Filter → ENABLE_DEBUG = True
docker logs open-webui | grep "\[FaultLine\]"
```

Look for:
- `inlet CALLED` — Filter started
- `/query status=200` — Backend responding
- `raw_triples=[]` — LLM not extracting (problem!)
- `rewrite_to_triples configuration error` — URL misconfigured

**Check 3: Is FaultLine backend running?**
```bash
curl http://localhost:8000/health
# Should return: {"status": "ok"}
```

**Check 4: Can OpenWebUI reach FaultLine?**
```bash
# From inside OpenWebUI container:
docker exec open-webui curl http://faultline:8000/health
# If error: Check docker network, hostnames, firewall
```

### "URL missing protocol" error

**Problem:** `BACKEND_LLM_URL` set to value like `ollama:11434/v1/chat/completions` (missing `http://`)  
**Fix:** Either:
- Set `BACKEND_LLM_URL` to **EMPTY** (standard), OR
- Set it to complete URL: `http://ollama:11434/v1/chat/completions`

### "Connection refused" to FaultLine (Most Common Issue)

**Problem:** `FAULTLINE_URL` points to wrong host/port  

**For Docker setups (most common):**
- **Wrong:** `http://localhost:8000` ← OpenWebUI container cannot reach host localhost
- **Correct:** `http://faultline:8000` ← Use Docker service name

**Why:** OpenWebUI runs in a container. When it tries to reach `localhost:8000`, it's looking for a service on the container itself, not the host machine. Use the service name from docker-compose (`faultline`).

**How to fix:**
1. Go to OpenWebUI → Tools → FaultLine Filter → Valves
2. Find `FAULTLINE_URL`
3. Change to: `http://faultline:8000`
4. Save

**For non-Docker (local) setups:**
- `FAULTLINE_URL` should be `http://localhost:8000` (current host)
- Ensure FaultLine backend is running: `curl http://localhost:8000/health`

**For remote deployments:**
- Use actual hostname: `http://faultline.example.com:8000`
- Ensure firewall allows OpenWebUI → FaultLine connection

### "Facts extracted but not injected into memory"

**Problem:** Filter gets facts from `/query` but doesn't pass them to LLM  
**Cause 1:** `QUERY_ENABLED = False` — Enable it  
**Cause 2:** `MIN_INJECT_CONFIDENCE` too high — Lower to 0.3–0.5  
**Cause 3:** Facts don't meet confidence threshold — Check debug logs

### "LLM extraction timeout"

**Problem:** Fact extraction takes >10 seconds  
**Fix:** Increase `QWEN_TIMEOUT` to 15–30 seconds

---

## Standard Configuration Checklist

- [ ] FaultLine backend running: `curl http://localhost:8000/health`
- [ ] PostgreSQL + Qdrant running
- [ ] Filter installed in OpenWebUI
- [ ] `FAULTLINE_URL`: Set correctly (localhost:8000 or docker service name)
- [ ] `LLM_API_KEY`: Pasted from OpenWebUI Settings
- [ ] `LLM_URL`: **EMPTY** (for standard setup)
- [ ] `BACKEND_LLM_URL`: **EMPTY** (for standard setup)
- [ ] `ENABLED`: ✓ True
- [ ] `INGEST_ENABLED`: ✓ True
- [ ] `QUERY_ENABLED`: ✓ True
- [ ] Test extraction: Send a message with facts, check logs for `[FaultLine]` output

---

## Production Configuration

### Docker Network
```yaml
# docker-compose.yml
services:
  faultline:
    container_name: faultline
    networks:
      - faultline-net
  open-webui:
    container_name: open-webui
    networks:
      - faultline-net
networks:
  faultline-net:
    driver: bridge
```

**Then set:**
- `FAULTLINE_URL`: `http://faultline:8000` (service name)

### Remote Deployment
If FaultLine runs on a different server (e.g., `faultline.internal.example.com`):
- `FAULTLINE_URL`: `http://faultline.internal.example.com:8000`
- Ensure firewall allows OpenWebUI → FaultLine connection
- Use static hostnames (not IPs)

### High-Volume Setup
- Increase `QWEN_TIMEOUT` to 15–30 seconds
- Increase `FAULTLINE_TIMEOUT` to 45–60 seconds
- Set `MAX_MEMORY_SENTENCES` to 10–15 (reduce memory overhead)
- Monitor: `docker logs faultline | tail -100`

---

## What Gets Stored?

FaultLine extracts and stores:
- **Identity facts:** Names, aliases, pronouns, dates of birth
- **Relationships:** Family, colleagues, friends
- **Attributes:** Job, location, education, preferences
- **Behaviors:** Likes, dislikes, habits

Facts stored in PostgreSQL. Searchable via vector embeddings in Qdrant.

---

## Disabling FaultLine (Temporarily)

If you need to pause FaultLine without uninstalling:
```
OpenWebUI → Tools → FaultLine Filter → ENABLED = False
```

Conversation continues normally; facts aren't learned or recalled.

To re-enable:
```
OpenWebUI → Tools → FaultLine Filter → ENABLED = True
```

---

## Support

**For issues:**
1. Enable `ENABLE_DEBUG = True`
2. Run the test: Message user facts, then ask about them
3. Collect logs: `docker logs open-webui | grep "\[FaultLine\]"`
4. Check `/health` endpoint: `curl http://localhost:8000/health`

**Common issue:** URL misconfiguration. Ensure all URLs have `http://` or `https://` prefix.
