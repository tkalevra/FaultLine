# FaultLine Deployment Guide

This is the full deployment walkthrough. For a 30-second start see the root
[`DEPLOYMENT.md`](../DEPLOYMENT.md); for the architecture see
[`ARCHITECTURE.md`](ARCHITECTURE.md).

## Architecture in one paragraph

FaultLine is a **per-tenant, write-validated, deterministic knowledge-graph
memory**. PostgreSQL is the authoritative store; a Qdrant vector index holds only
the short-term Class-C tier. The **live integration path is the MCP server**
(`recall_memory`, `remember_facts`, `learn_facts`, `retract_fact`) on port `:8002`.
The legacy OpenWebUI Filter in `openwebui/` is intentionally disabled and is **not**
the production path — do not treat its being off as a fault.

```
client (Claude Desktop / OpenWebUI MCP) ──MCP──▶ faultline-mcp :8002 ──▶ backend :8000
                                                                            │
                                          PostgreSQL :5432 (authoritative, per-tenant)
                                                  + Qdrant :6333 (Class-C short-term)
```

The backend orchestrates the complete ingest pipeline — extraction, the WGM
validation gate, conflict/directionality checks, Class A/B/C assignment, and commit
to the per-tenant PostgreSQL schema. All validation lives in the backend; nothing is
scattered into the client.

---

## Quick start

### Prerequisites
- Docker & Docker Compose v2+
- PostgreSQL 16+ and Qdrant (both come up via the compose file)
- An LLM backend you already run

### Steps

1. **Configure and start the stack:**
   ```bash
   cp .env.example .env
   # Set LLM_BACKEND_TYPE + LLM_BASE_URL, and MCP_API_KEY for any networked deploy.
   docker compose up -d --build
   ```

2. **Verify the backend:**
   ```bash
   curl http://localhost:8000/health
   # → {"status": "ok", "database": "ok", "qdrant": "ok", "llm": "ok"}
   ```

3. **Connect a client over MCP** — see [`MCP-SETUP.md`](MCP-SETUP.md) for the
   Claude Desktop and OpenWebUI MCP configuration. The MCP server listens on
   `:8002`; set `MCP_API_KEY` and use it as the bearer token.

4. **Test end to end:**
   ```
   You:  "My name is Sam, I prefer to go by Sammy."
        → the model calls remember_facts; the fact is validated and stored.
   You:  "What's my name?"
        → the model calls recall_memory; the stored fact is returned.
   ```

---

## Configuration reference

The full annotated list is in [`.env.example`](../.env.example); a summary table is
in [`ENV-REFERENCE.md`](ENV-REFERENCE.md). The essentials:

| Variable | Purpose |
|---|---|
| `POSTGRES_DSN` | PostgreSQL connection (authoritative store). |
| `LLM_BACKEND_TYPE` / `LLM_BASE_URL` | Which LLM you talk to and where. |
| `LLM_API_KEY` | Bearer token for hosted backends; blank for local. |
| `WGM_LLM_MODEL` | Model name as it appears on your backend. |
| `MCP_API_KEY` | Bearer token enforced on the MCP transport (`:8002`). **Set this for any networked deploy.** |
| `FAULTLINE_USER_ID` | Single-user fallback for MCP; omit in multi-user. |
| `QDRANT_URL` | Vector index endpoint (Class-C short-term tier only). |
| `REEMBED_INTERVAL` | Re-embedder poll interval (seconds). |

Set LLM configuration through environment variables (compose `environment:` or
Kubernetes manifests), not inside any client UI.

---

## Logging & troubleshooting

FaultLine uses `structlog` for structured output, controlled entirely by
environment variables.

### Log level

```yaml
# docker-compose.yml
services:
  faultline:
    environment:
      FAULTLINE_LOG_LEVEL: INFO   # DEBUG | INFO | WARNING | ERROR | CRITICAL
```

| Level | Use case |
|---|---|
| `DEBUG` | Development, deep troubleshooting — function entry/exit, query plans. |
| `INFO` | Production default — extraction success, ingest completion, promotion, errors. |
| `WARNING` | Only unexpected conditions (failed retries, fallbacks). |
| `ERROR` / `CRITICAL` | Failures only. |

Performance overhead: `INFO` ~2–5%, `DEBUG` ~5–15%. Use `INFO` in production,
`DEBUG` for incident investigation.

### Reading backend logs

```bash
docker logs faultline                                  # all backend logs
docker logs faultline | grep "extract_rewrite"         # extraction
docker logs faultline | grep "wgm_gate\|fact_store\|commit"   # ingest pipeline
docker logs faultline | grep "query_user_facts"        # query
docker logs faultline | grep "gliner2"                 # entity typing
docker logs faultline | grep "re_embedder"             # background loop
docker logs faultline | grep "ERROR\|CRITICAL\|Exception"
```

### Key pipeline log patterns (DEBUG)

**Extraction:** `gliner2_entity_extraction: entities_extracted=N`,
`extract_rewrite: types_enriched`.
**Validation:** `validate_triple_against_metadata: matched_rel_type`,
`_validate_hierarchy_membership: check_passed`.
**Ingest:** `fact_classification: assigned_class` (A/B/C),
`FactStoreManager.commit: fact_stored`.
**Query:** `query_user_facts: found_facts`, `_hierarchy_expand: expansion_result`.
**Re-embedder:** `re_embedder.promote_staged_facts: promoted`,
`re_embedder.evaluate_ontology_candidates: approved`.

---

## Common issues

### MCP tools don't appear / calls fail

- Confirm the backend is healthy: `curl http://localhost:8000/health`.
- Confirm the MCP server is reachable on `:8002` and the bearer token matches
  `MCP_API_KEY`.
- Confirm the per-request user id is being set (the `X-OpenWebUI-User-Id` header,
  or `FAULTLINE_USER_ID` in single-user mode). Without a resolvable user id, calls
  land in the wrong (or no) tenant schema.

### "Facts aren't being stored"

- A pure question or short chit-chat correctly does **not** ingest. Send a
  declarative statement with substance.
- Enable `FAULTLINE_LOG_LEVEL=DEBUG` and look for `entities_extracted=0` — that is
  an extraction miss, not a model under-firing.
- After wiping a tenant, restart the MCP container so its in-memory
  provisioning cache re-fires.

### Backend can't reach the LLM

- Verify `LLM_BACKEND_TYPE` + `LLM_BASE_URL` point at a running model server.
- For Docker-internal LLMs use the service name; for host LLMs use
  `host.docker.internal`.
- `WGM_LLM_MODEL` must match a model the backend actually serves.

---

## Production configuration

### Docker network

```yaml
services:
  faultline:
    container_name: faultline
    networks: [faultline-net]
  faultline-mcp:
    container_name: faultline-mcp
    networks: [faultline-net]
networks:
  faultline-net:
    driver: bridge
```

### Hardening checklist

- [ ] `curl http://localhost:8000/health` returns `ok`
- [ ] PostgreSQL + Qdrant running with external volumes
- [ ] `MCP_API_KEY` set to a strong secret (and used by clients)
- [ ] `LLM_BACKEND_TYPE` / `LLM_BASE_URL` / `WGM_LLM_MODEL` correct for your backend
- [ ] `FAULTLINE_LOG_LEVEL=INFO`
- [ ] `DB_POOL_SIZE` tuned to concurrency

---

## What gets stored

FaultLine extracts and stores, per tenant:

- **Identity:** names, aliases, preferred names, dates of birth.
- **Relationships:** family, colleagues, friends (graph edges).
- **Attributes (scalars):** age, location, occupation, IPs, MACs, hostnames.
- **Classifications:** entity types and hierarchy membership.

Authoritative facts (Class A/B) live in PostgreSQL. Class C short-term material is
mirrored to Qdrant until it is promoted or expires.
