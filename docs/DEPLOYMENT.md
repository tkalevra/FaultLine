# FaultLine Deployment Guide

## Architecture: Write-Validated Knowledge Graph Pipeline

FaultLine is the **single source of truth** (port 8001). Filter is intentionally dumb — all validation happens in the backend.

```
OpenWebUI Filter (dumb)
  ├─ Extract corrections via regex (explicit user signals)
  └─ Send {text, corrections} to FaultLine:8001

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
   docker-compose up -d faultline-wgm faultline-postgres qdrant
   ```

2. **Verify health:**
   ```bash
   curl http://localhost:8001/health
   ```
   Should return `"status": "ok"`

3. **Install the Filter in OpenWebUI:**
   - Go to OpenWebUI → Tools → Create new tool
   - Select "Filters"
   - Paste content from `openwebui/faultline_tool.py`
   - Click "Save"

4. **Configure Filter (OpenWebUI → Tools → FaultLine Filter → Valves):**

   **Step 1: Set FAULTLINE_URL (CRITICAL)**
   - **Docker setup:** Set `FAULTLINE_URL` to `http://faultline:8000` (service name)
   - **Local setup:** Set `FAULTLINE_URL` to `http://localhost:8001`
   
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
**Standard value:** `http://localhost:8001`  
**Docker value:** `http://faultline:8000` (service name in docker-compose)  
**Remote value:** `http://your-hostname:8001`  
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
**DEPRECATED** — No longer used. FaultLine reads QWEN_API_URL from environment.  
**Standard value:** **LEAVE EMPTY**  
**Note:** Kept for backwards compatibility only. Can be safely ignored.

**New approach:**
FaultLine backend now reads LLM configuration from environment variables:
- `QWEN_API_URL`: LLM endpoint (e.g., `http://localhost:11434/v1/chat/completions`)
- `WGM_LLM_MODEL`: Model name (e.g., `qwen/qwen3.5-9b`)

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
curl http://localhost:8001/health
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
- **Wrong:** `http://localhost:8001` ← OpenWebUI container cannot reach host localhost
- **Correct:** `http://faultline:8000` ← Use Docker service name

**Why:** OpenWebUI runs in a container. When it tries to reach `localhost:8001`, it's looking for a service on the container itself, not the host machine. Use the service name from docker-compose (`faultline`).

**How to fix:**
1. Go to OpenWebUI → Tools → FaultLine Filter → Valves
2. Find `FAULTLINE_URL`
3. Change to: `http://faultline:8000`
4. Save

**For non-Docker (local) setups:**
- `FAULTLINE_URL` should be `http://localhost:8001` (current host)
- Ensure FaultLine backend is running: `curl http://localhost:8001/health`

**For remote deployments:**
- Use actual hostname: `http://faultline.example.com:8001`
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

- [ ] FaultLine backend running: `curl http://localhost:8001/health`
- [ ] PostgreSQL + Qdrant running
- [ ] Filter installed in OpenWebUI
- [ ] `FAULTLINE_URL`: Set correctly (localhost:8001 or docker service name)
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
- `FAULTLINE_URL`: `http://faultline.internal.example.com:8001`
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
4. Check `/health` endpoint: `curl http://localhost:8001/health`

**Common issue:** URL misconfiguration. Ensure all URLs have `http://` or `https://` prefix.
