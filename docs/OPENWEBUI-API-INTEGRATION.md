# OpenWebUI API Integration Guide

## Authoritative OpenWebUI /api/chat/completions Specification

**Source:** https://docs.openwebui.com/reference/api-endpoints/

### Endpoint: POST /api/chat/completions

**Description:** OpenAI API compatible chat completion endpoint for all models on Open WebUI (Ollama, OpenAI, OpenWebUI Functions).

### Authentication

**Required:** Bearer token in `Authorization` header

```
Authorization: Bearer {YOUR_API_KEY}
```

The token represents the user's API key generated in OpenWebUI settings.

### Request Body

**Required Fields:**

| Field | Type | Description | Example |
|-------|------|-------------|---------|
| `model` | string | Model identifier registered with OpenWebUI | `"qwen/qwen3.5-9b:2"` |
| `messages` | array | Array of message objects | See below |

**Message Object Format:**

```json
{
  "role": "system|user|assistant",
  "content": "Text content of the message"
}
```

**Optional Fields:**

| Field | Type | Description | Default |
|-------|------|-------------|---------|
| `chat_id` | UUID | Conversation history persistence key | None |
| `files` | array | File/collection IDs for RAG | None |
| `stream` | boolean | Enable streaming response | false |
| `temperature` | float | Sampling temperature (0.0-2.0) | 0.7 |
| `max_tokens` | integer | Maximum response tokens | Model default |

### Minimal Valid Request Example

```bash
curl -X POST https://example.com/api/chat/completions \
  -H "Authorization: Bearer sk-your-api-key-here" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen/qwen3.5-9b:2",
    "messages": [
      {
        "role": "user",
        "content": "Hello, what is your name?"
      }
    ]
  }'
```

### Success Response (HTTP 200)

```json
{
  "id": "chatcmpl-xyz123",
  "object": "chat.completion",
  "created": 1679123456,
  "model": "qwen/qwen3.5-9b:2",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "I am Claude, an AI assistant made by Anthropic."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 12,
    "completion_tokens": 25,
    "total_tokens": 37
  }
}
```

### Error Responses (HTTP 400, 401, 404, 500)

**HTTP 400 Bad Request** — Most common OpenWebUI error

| Cause | Check | Fix |
|-------|-------|-----|
| Invalid model name | `curl https://example.com/api/models -H "Authorization: Bearer YOUR_KEY"` | Use exact model name from models endpoint |
| Missing required fields | Request body missing `model` or `messages` | Include both fields |
| Invalid message format | Messages not array of {role, content} objects | Validate message structure |
| Bearer token missing | No `Authorization` header | Add header: `Authorization: Bearer token` |
| Expired/invalid token | Token doesn't exist or is revoked | Regenerate API key in OpenWebUI |

**HTTP 401 Unauthorized** — Authentication failed

| Cause | Fix |
|-------|-----|
| Missing Authorization header | Add header: `Authorization: Bearer token` |
| Invalid bearer token | Check LLM_API_KEY environment variable |
| Token has wrong format | Token should be `sk-...` not `Bearer sk-...` |

**HTTP 404 Not Found** — Endpoint doesn't exist

| Cause | Fix |
|-------|-----|
| Wrong URL path | Use `/api/chat/completions` not other variations |
| OpenWebUI not running | Verify deployment at example.com |

### How Filters Execute

**Inlet Filter:** Executes BEFORE the request reaches the LLM
- Intercepts POST /api/chat/completions calls
- Can inject context (FaultLine memory facts)
- Can modify request (system message prepending)
- Can short-circuit (retraction, early return)

**Outlet Filter:** Does NOT execute for direct API calls
- Only executes for web UI chat, not `/api/chat/completions`
- Use `/api/chat/completed` endpoint if outlet hook needed

### FaultLine Integration Pattern

**Correct flow for FaultLine extraction:**

1. **OpenWebUI Filter inlet** receives POST /api/chat/completions
2. **Filter calls** FaultLine `/query` endpoint
   - Fetches user facts from PostgreSQL
   - Returns formatted facts for injection
3. **Filter injects** facts into system message
4. **Filter forwards** augmented request to LLM
5. **LLM response** returned to user
6. **Filter calls** FaultLine `/ingest` (async)
   - Extracts facts from user message
   - Stores in PostgreSQL

**Direct backend extraction call (for testing):**

```bash
curl -X POST http://faultline:8001/extract/rewrite \
  -H "Authorization: Bearer sk-test-key" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user-uuid",
    "text": "My name is Chris and I have 3 children",
    "messages": []
  }'
```

### Troubleshooting HTTP 400 Errors

**Step 1: Verify OpenWebUI is running**
```bash
curl -X GET https://example.com/api/models \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -v
```

**Step 2: Check available models**
```bash
curl -X GET https://example.com/api/models \
  -H "Authorization: Bearer YOUR_API_KEY" \
  | jq '.data[].name'
```

**Step 3: Verify your model name is exact**
```
Expected: qwen/qwen3.5-9b:2
Not:      qwen-qwen3.5-9b
Not:      qwen3.5-9b
```

**Step 4: Test with minimal request**
```bash
curl -X POST https://example.com/api/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen/qwen3.5-9b:2",
    "messages": [{"role": "user", "content": "hi"}]
  }'
```

**Step 5: Check FaultLine logs for actual error**
```bash
ssh docker-host -x "sudo docker logs faultline 2>&1 | grep -i 'error\|400\|extract'"
```

### Environment Variables for FaultLine

**Required when using OPENWEBUI_URL:**

```bash
OPENWEBUI_URL=https://example.com
LLM_API_KEY=***REDACTED-API-KEY***
WGM_LLM_MODEL=qwen/qwen3.5-9b:2
CATEGORY_LLM_MODEL=qwen/qwen3.5-9b:2
```

**Validation before deployment:**

1. ✅ `OPENWEBUI_URL` resolves to OpenWebUI instance
2. ✅ `LLM_API_KEY` is valid bearer token (generate in OpenWebUI settings)
3. ✅ `WGM_LLM_MODEL` exists on OpenWebUI (verify with /api/models)
4. ✅ Bearer token is sent with every request (handled by get_llm_headers())
5. ✅ Model name is exact match (case-sensitive)

### Key Differences: OpenWebUI vs Direct Qwen/Ollama

| Aspect | OpenWebUI | Direct Qwen/Ollama |
|--------|-----------|-------------------|
| Endpoint | `https://example.com/api/chat/completions` | `http://localhost:11434/v1/chat/completions` |
| Authentication | Bearer token in Authorization header | None (local) |
| Model name format | `provider/model:version` | `model` |
| Filters | Inlet/outlet execute | No filters |
| Streaming | Supported | Supported |
| RAG support | Yes (via files) | No |

### FaultLine Code Implementation

**In src/api/main.py:**

```python
# LLM endpoint with bulletproof priority chain
qwen_url = _configured_llm_url()  # Priority: OPENWEBUI_URL → QWEN_API_URL → fallback

# Prepare request
response = await _http_client.post(
    qwen_url,
    json={
        "model": os.getenv("WGM_LLM_MODEL", "qwen/qwen3.5-9b"),
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 1200,
    },
    headers=get_llm_headers(),  # Adds Authorization: Bearer token
    timeout=120,
)
response.raise_for_status()  # Raise on HTTP 400/401/500
```

**In src/api/llm_client.py:**

```python
def get_llm_headers() -> dict:
    """Returns Authorization header with LLM_API_KEY if set."""
    llm_api_key = os.environ.get("LLM_API_KEY", "")
    headers = {}
    if llm_api_key:
        headers["Authorization"] = f"Bearer {llm_api_key}"
    return headers
```

### Testing Checklist

- [ ] OpenWebUI /api/models endpoint returns list with your model
- [ ] Model name from /api/models matches WGM_LLM_MODEL exactly
- [ ] Bearer token from LLM_API_KEY is valid
- [ ] FaultLine backend /health returns 200 OK
- [ ] FaultLine /extract/rewrite accepts POST with valid model
- [ ] Response is HTTP 200 with valid JSON triples (not 400)
- [ ] Database stores facts from extraction (check facts table)
- [ ] Filter injects facts correctly in OpenWebUI conversation
