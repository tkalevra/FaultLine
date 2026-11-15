# Security Policy

## Reporting Security Vulnerabilities

If you discover a security vulnerability in FaultLine, please email **security@faultline.local** with:

1. **Description** of the vulnerability
2. **Affected component(s)** (e.g., `/ingest` endpoint, re-embedder, filter)
3. **Steps to reproduce** (if applicable)
4. **Potential impact** (severity assessment)
5. **Suggested fix** (if you have one)

**Please do not open public GitHub issues for security vulnerabilities.** We will acknowledge receipt within 48 hours and work toward a fix in coordination with you.

## Security Considerations

### Data Protection

- **PostgreSQL:** Use strong credentials and enable SSL/TLS for production. Restrict network access to database via firewall rules. Holds authoritative Class A/B memory.
- **Qdrant:** Vector database serves ONLY the agent cortex (the per-seat `flagent-<uuid>` collection) — it no longer holds any user memory. User memory (Class A/B/C, including the short-term Class-C tier) lives entirely in PostgreSQL (`facts`/`staged_facts`/`episodic_log`); the user-memory vector tier is retired (`USER_MEMORY_VECTOR_LANE=false` in production, implemented + validated on pre-prod). Protect Qdrant with authentication and network isolation.
- **Redis:** Used for rate limiting and event queues. Configure password authentication and disable SAVE/BGSAVE in production if using ephemeral queues.
- **Entity UUIDs:** UUIDs are v5 surrogates derived from display names, not cryptographic identifiers. Do not rely on UUID uniqueness for security purposes.

### LLM Integration

- **OpenWebUI Credentials:** Protect API tokens and bearer credentials. Use environment variables or secrets management, never hardcode in source.
- **LLM Endpoint:** All LLM calls flow through a centralized client (`src/api/llm_calls.py` + `src/api/llm_client.py`); the endpoint resolves from `LLM_BACKEND_TYPE` + `LLM_BASE_URL` (host+port only). Verify endpoint configuration in production. Per-operation timeouts and token budgets are env-overridable via `LLM_TIMEOUT_<OP>` / `LLM_MAX_TOKENS_<OP>` — never hardcode.
- **Prompt Injection:** LLM prompts are populated from database metadata (rel_types table descriptions, entity names). Validate and sanitize all user input before persisting to database. A pre-flight injection-signal detector gates `remember_facts` (`src/mcp/server.py`).

### Authentication & Authorization

- **MCP transport auth:** The public core requires `MCP_API_KEY` as a Bearer token on every `POST /mcp` and REST-shorthand call (`src/mcp/http_server.py:555,652`). Unauthenticated mode (key unset) is for dev/localhost ONLY. (The managed SaaS offering layers its own account/seat auth on top of this transport; that model is documented with the SaaS offering, not in this public security doc.)
- **Per-user identity:** The MCP transport resolves caller identity ONCE via `bind_tenant` from `X-OpenWebUI-User-Id` (OpenWebUI header) or `arguments.user_id`, validates it as a well-formed UUID, and binds `SET search_path TO faultline_{user_slug}` **without `public`** for every backend call. A malformed or unauthorized claim fails loud (400/403), never silently routes to a shared pool.
- **Per-User Isolation:** User memory is isolated per PostgreSQL schema (`faultline_{user_slug}`) via `SET search_path` **without `public`** — that schema boundary IS the tenant isolation (no shared `user_id` column to filter on and forget). Under `USER_MEMORY_VECTOR_LANE=false` there are no per-user Qdrant collections for user memory; the cortex collections (`flagent-<uuid>`) are per-seat and structurally firewalled from user memory.
- **Rate Limiting:** Per-user_id tracking, 100 req/min default, enforced at `/query` and `/ingest`.

### Transport Hardening

- **Security response headers middleware:** every MCP response carries safe-default headers (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Strict-Transport-Security`, `Cross-Origin-Resource-Policy`, and a strict `Content-Security-Policy`) — `src/mcp/http_server.py:84-154`. Additive + reversible via `MCP_SECURITY_HEADERS=0`. This is a public-core control (SOC 2 CC6.6/CC6.7 boundary protection), not SaaS-specific.

### Known Issues & Workarounds

**dBug-016 (OpenWebUI NoneType Crash on Missing chat_id)**
- **Status:** Upstream issue in OpenWebUI ([openwebui/open-webui#24550](https://github.com/open-webui/open-webui/issues/24550))
- **Workaround:** Coerce `chat_id` to empty string instead of None in OpenWebUI's socket/main.py (lines 902, 920)
- **Mitigation:** FaultLine works around this at the filter level; facts still ingest correctly despite upstream crash

### Database Safety

- **Entity ID Normalization:** All entity IDs must be UUIDs or `"user"` anchor. String IDs are converted to UUID v5 surrogates at startup.
- **Cascade Deletes:** Fact deletion uses soft-delete (set `superseded_at` timestamp). Hard deletes only on explicit user retraction for identity rel_types (pref_name, also_known_as).
- **Connection Pooling:** All database connections use a pooled `psycopg2` connection via connection manager with proper cleanup in finally blocks.

### Validation & Sanitization

- **Triple Validation:** All (subject, rel_type, object) triples are validated by WGMValidationGate before commit:
  - Type constraints enforced (head_types/tail_types from rel_types table)
  - Bidirectional relationships validated (no orphaned child_of without parent_of)
  - Semantic conflicts detected (type entities cannot own or be owned)
- **Lowercase Normalization:** rel_type and entity display names are lowercased on write. All string comparisons use pre-lowercased values (guards against injection).

### Dependency Management

- **Lock Files:** Use `pip freeze` or `poetry.lock` to pin exact dependency versions in production.
- **Regular Updates:** Monitor security advisories for:
  - **psycopg2** (PostgreSQL driver)
  - **fastapi** / **uvicorn** (web framework)
  - **gliner2** / **huggingface_hub** (ML packages)
  - **httpx** (HTTP client)
- **Vulnerable Packages:** Check regularly via `pip-audit` or `safety`.

### OpenSSF Scorecard Recommendations

FaultLine implements best practices for open-source security:

- ✅ **Dangerous Workflow:** GitHub Actions use pinned versions (no `latest` tags)
- ✅ **Branch Protection:** Main branch protected (require PR reviews before merge)
- ✅ **Dependency Pinning:** All Python dependencies have version constraints in pyproject.toml
- ✅ **SECURITY.md:** This file provides clear vulnerability reporting guidelines
- ⏳ **Signed Commits:** Future: Enable commit signing for all contributors
- ⏳ **Token Permissions:** Actions use `permissions: read-all` by default; override only where write access needed

### Logging & Monitoring

- **Structured Logging:** All operations logged via `structlog` with context (user_id, endpoint, operation)
- **No Sensitive Data in Logs:** Entity display names are logged, but API tokens, database passwords, and LLM endpoints are scrubbed
- **Audit Trail:** Fact creation timestamps, user_id, and provenance recorded in PostgreSQL for historical queries

## Compliance

- **Data Retention:** Facts are retained indefinitely unless explicitly soft-deleted. Implement user data deletion requests per GDPR/CCPA if required.
- **PII Handling:** Personal identifying information (names, addresses, birth dates) is stored with sensitivity metadata. `/query` applies sensitivity penalty to reduce PII leakage in facts returned to LLM.

## Security Roadmap

- [ ] Implement commit signing for all contributors
- [ ] Add SBOM (Software Bill of Materials) generation
- [ ] OpenSSF Best Practices Badge application
- [ ] Annual security audit (planned for 2026 Q3)
- [ ] Fuzzing harness for input validation

## Contacts

**Security:** security@faultline.local  
**Issue Reports:** [GitHub Issues](https://github.com/tkalevra/FaultLine-dev/issues)
