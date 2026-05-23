# FaultLine Environment Variable Specification

## Overview
This document specifies the exact environment variables required for FaultLine deployment, with bulletproof OpenWebUI integration.

## Deployment Scenarios

### Scenario 1: Docker Compose (Local Dev)
**File:** `docker-compose.yml`  
**Use Case:** Development and testing  
**LLM Source:** QWEN_API_URL or localhost:11434

**.env file (required):**
```
QWEN_API_URL=http://host.docker.internal:11434/v1/chat/completions
POSTGRES_USER=faultline
POSTGRES_PASSWORD=faultline
POSTGRES_DB=faultline
QDRANT_COLLECTION=faultline-test
REEMBED_INTERVAL=5
DB_POOL_SIZE=15
```

### Scenario 2: Portainer Stack (Production)
**File:** `docker-compose-portainer-withoutqdrant.yml`  
**Use Case:** Production deployment with external Qdrant  
**LLM Source:** OPENWEBUI_URL (required, must be set)

**Portainer Environment Variables (required):**
```
OPENWEBUI_URL=https://${OPENWEBUI_DOMAIN}
LLM_API_KEY=${BEARER_TOKEN}
FAULTLINE_MEMORY_CHAIN_UUID=00000000-0000-0000-0000-000000000000
FAULTLINE_URL=http://${BACKEND_IP}:8001
POSTGRES_PASSWORD=faultline
WGM_LLM_MODEL=qwen/qwen3.5-9b
CATEGORY_LLM_MODEL=qwen2.5-coder
DB_POOL_SIZE=15
```

## LLM Endpoint Configuration Pattern

### Bulletproof Rule: OPENWEBUI_URL Takes Precedence

The FaultLine backend MUST follow this priority order for LLM endpoint resolution:

**Priority Chain (in src/api/main.py):**
1. **OPENWEBUI_URL** — Explicit environment variable (external/HTTP endpoint)
   - Used when explicitly set: `OPENWEBUI_URL=https://${OPENWEBUI_DOMAIN}`
   - Endpoint format: `{OPENWEBUI_URL}/api/chat/completions`
   - Required: `LLM_API_KEY` bearer token for authentication
   
2. **QWEN_API_URL** — Fallback for direct Qwen/Ollama access
   - Used when OPENWEBUI_URL is not set
   - Endpoint format: Direct connection to Qwen/Ollama API
   - Example: `http://host.docker.internal:11434/v1/chat/completions`
   
3. **Hardcoded fallback** — Last resort (should never be used in production)
   - `http://localhost:11434/v1/chat/completions`

### Implementation: No Circular Calls

**CRITICAL BUG TO FIX:**
Current code in `src/api/main.py` lines 1445-1455 has circular dependency:
```python
# WRONG - circular
def _get_llm_url() -> str:
    openwebui_url = os.environ.get("OPENWEBUI_URL")
    if openwebui_url:
        return f"{openwebui_url}/api/chat/completions"
    return _configured_llm_url()  # ← calls configured_llm_url

def _configured_llm_url() -> str:
    global _LLM_URL
    return _LLM_URL or _get_llm_url()  # ← calls get_llm_url → infinite loop
```

**CORRECT Pattern (must be implemented):**
```python
def _get_llm_url() -> str:
    """Get LLM endpoint URL with bulletproof priority chain."""
    
    # Priority 1: OPENWEBUI_URL (explicit, external)
    openwebui_url = os.environ.get("OPENWEBUI_URL")
    if openwebui_url:
        if not openwebui_url.startswith("http"):
            openwebui_url = f"https://{openwebui_url}"
        endpoint = f"{openwebui_url}/api/chat/completions"
        log.info("llm_endpoint.openwebui_detected", url=endpoint)
        return endpoint
    
    # Priority 2: QWEN_API_URL (fallback)
    qwen_url = os.environ.get("QWEN_API_URL")
    if qwen_url:
        log.info("llm_endpoint.qwen_fallback", url=qwen_url)
        return qwen_url
    
    # Priority 3: Hardcoded fallback (development only)
    fallback = "http://localhost:11434/v1/chat/completions"
    log.warning("llm_endpoint.hardcoded_fallback", url=fallback)
    return fallback
```

**Key Changes:**
- ✅ NO circular calls
- ✅ Clear priority chain (1 → 2 → 3)
- ✅ Each endpoint selection is logged
- ✅ OPENWEBUI_URL handling supports URL normalization (adds https:// if needed)

## OpenWebUI Authentication

### Required: LLM_API_KEY Bearer Token

When using OPENWEBUI_URL, the FaultLine backend MUST include the bearer token:

**In get_llm_headers():**
```python
def get_llm_headers() -> dict:
    """Return headers for LLM API calls."""
    headers = {"Content-Type": "application/json"}
    
    # Add bearer token if using OpenWebUI (OPENWEBUI_URL is set)
    if os.environ.get("OPENWEBUI_URL"):
        api_key = os.environ.get("LLM_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    
    return headers
```

## Model Name Configuration

### WGM_LLM_MODEL vs CATEGORY_LLM_MODEL

These must match model names available on the LLM backend:

**For OpenWebUI (${OPENWEBUI_DOMAIN}):**
```
WGM_LLM_MODEL=qwen/qwen3.5-9b:2
CATEGORY_LLM_MODEL=qwen/qwen3.5-9b:2
```

**For Local Qwen/Ollama:**
```
WGM_LLM_MODEL=qwen/qwen3.5-9b
CATEGORY_LLM_MODEL=qwen2.5-coder
```

**Validation at startup:**
- FaultLine /health endpoint should echo the actual model names configured
- If model name is invalid, extraction will fail with HTTP 400 Bad Request

## Complete .env Template

### For Portainer Production:
```
# OpenWebUI Integration (REQUIRED for production)
OPENWEBUI_URL=https://${OPENWEBUI_DOMAIN}
LLM_API_KEY=${BEARER_TOKEN}

# Database Configuration
POSTGRES_PASSWORD=faultline
POSTGRES_DSN=postgresql://faultline:${POSTGRES_PASSWORD}@postgres:5432/faultline_test

# FaultLine Backend
FAULTLINE_URL=http://${BACKEND_IP}:8001
FAULTLINE_MEMORY_CHAIN_UUID=00000000-0000-0000-0000-000000000000

# LLM Model Selection
WGM_LLM_MODEL=qwen/qwen3.5-9b:2
CATEGORY_LLM_MODEL=qwen/qwen3.5-9b:2

# Qdrant Vector DB (external)
QDRANT_URL=http://qdrant:6333
QDRANT_COLLECTION=faultline-test

# Redis (for distributed caching if needed)
REDIS_URL=redis://redis:6379/0

# Performance Tuning
REEMBED_INTERVAL=5
DB_POOL_SIZE=15
HTTPX_TIMEOUT=30
DB_TIMEOUT=30
QDRANT_TIMEOUT=10
RATE_LIMIT_PER_MIN=100
```

### For Docker Compose Dev:
```
# Direct LLM Backend (no OpenWebUI)
QWEN_API_URL=http://host.docker.internal:11434/v1/chat/completions

# Database Configuration
POSTGRES_USER=faultline
POSTGRES_PASSWORD=faultline
POSTGRES_DB=faultline

# Local Qdrant
QDRANT_URL=http://qdrant:6333
QDRANT_COLLECTION=faultline-test

# Performance Tuning
REEMBED_INTERVAL=5
DB_POOL_SIZE=15
```

## Validation Checklist

Before deploying, verify:

- [ ] OPENWEBUI_URL is set to the correct external URL
- [ ] LLM_API_KEY is set and matches the OpenWebUI bearer token
- [ ] WGM_LLM_MODEL matches an available model on the LLM backend
- [ ] FaultLine /health endpoint returns 200 OK
- [ ] FaultLine /extract/rewrite endpoint accepts POST requests
- [ ] Database connections are healthy (POSTGRES_DSN works)
- [ ] Qdrant is reachable at QDRANT_URL
- [ ] Sample extraction returns valid JSON (not HTTP 400)

## Known Issues

### HTTP 400 Bad Request from /api/chat/completions

**Cause:** Usually one of:
1. Model name doesn't exist on the LLM backend
2. Request payload is malformed (missing required fields)
3. Bearer token is invalid or missing

**Debug:**
1. Check WGM_LLM_MODEL against available models: `curl https://${OPENWEBUI_DOMAIN}/api/models`
2. Test endpoint directly: `curl -H "Authorization: Bearer YOUR_KEY" https://${OPENWEBUI_DOMAIN}/api/chat/completions -d '{"model":"qwen/qwen3.5-9b:2","messages":[...]}'`
3. Check FaultLine logs for the actual error: `docker logs faultline 2>&1 | grep -i "400\|error"`

### Circular LLM Endpoint Resolution

**Current Code Bug:** Lines 1445-1455 in src/api/main.py have circular dependency  
**Status:** REQUIRES FIX (see Implementation section above)

## Future: MCP Support

This specification prepares for future MCP (Model Context Protocol) integration:

When MCP support is added:
```
LLM_BACKEND=openwebui    # or "mcp", "qwen", "ollama"
OPENWEBUI_URL=https://${OPENWEBUI_DOMAIN}
MCP_SERVER_URL=...       # Future
```

The bulletproof priority chain pattern supports this evolution without breaking existing deployments.
