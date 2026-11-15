# FaultLine Attachment Bridge — OpenWebUI install & verification runbook

**Audience:** the OpenWebUI admin. **Time:** ~5 minutes. **Rollback:** toggle one valve.

## What this is

`faultline_attachments.py` is an OpenWebUI **function filter** that POSTs every
freshly-attached file straight to FaultLine's server-side door
(`POST /ingest/attachment`) **at attach time**. FaultLine does the parsing server-side
(binary artefact retention, page geometry, figure-caption binding, stub facts) and the
model is told, in the message context:

> FaultLine is already ingesting this document (id 42, ETA ~410s); check progress with
> document_status — never re-transmit the document's contents.

The model's job shrinks to a status check. It never re-transmits a document again.

**Scope guard:** this filter is an ADDITION for attachments only. It does NOT re-enable
the legacy `faultline_function.py` inlet behavior (memory extraction / recall injection),
which stays **disabled by design** — the MCP tool server remains the live FaultLine
integration. This filter performs no memory reads or writes of its own.

## Prerequisites

1. A working FaultLine tool-server connection in OpenWebUI (Admin → Settings → Tools).
   You need its **base URL** and its **bearer key** — the bridge reuses the SAME
   credential (the TOOL-SERVER key: `MCP_API_KEY` in the open core, the account key
   under SaaS). It is middleware-trusted server-to-server auth; it is **not** a per-seat
   `flu_` token.
2. The target FaultLine build includes the `/ingest/attachment` door (saas branch,
   `lane/attachment-bridge`).

## Install

1. OpenWebUI **Admin Panel → Functions** → **`+` (New Function)**.
2. Paste the full contents of `openwebui/faultline_attachments.py` (or upload the file),
   name it `FaultLine Attachment Bridge`, **Save**.
3. Toggle the function **ON** (it is active for all models once enabled; it is inert
   while its valves are empty).
4. Open the function's **Valves** and set:

| Valve | Set to |
|---|---|
| `FAULTLINE_MCP_URL` | Base URL of the FaultLine MCP server (port **8002**), e.g. `http://faultline-mcp:8002` (same Docker network) or your public edge `https://mcp.faultline.ca`. |
| `FAULTLINE_API_KEY` | The tool-server connection's bearer key (same value as the Tools connection). |
| `OWUI_FILES_BASE_URL` | Origin for resolving `/api/v1/files/...` URLs. Inside Docker: `http://open-webui:8080`. Default `http://localhost:8080` is correct when the filter runs inside the OWUI backend. |
| `ENABLED` | `true` (master switch; set `false` to disable the bridge without removing it). |
| `TIMEOUT_SECS` | `30` — bridge POST budget. The server-side ingest continues regardless; a timeout never loses the file *if* the POST was received. |
| `MAX_FILES_PER_MESSAGE` | `5` — flood guard. |
| `DEDUP_TTL_SECS` | `21600` (6 h) — regenerate/re-fire of the same message does not re-POST. |

5. **Save valves.** No restart needed.

## How it behaves

- Bytes are resolved per file, in order: inline `data:` URI → OpenWebUI's own files API
  (`/api/v1/files/{id}/file`, authenticated with the signed-in user's token) → local
  upload path → **text fallback**: if only OWUI's extracted text is available, that text
  goes to the existing `/ingest_document` door. Either way the parsing happens
  server-side and the local AI never parses or re-transmits anything.
- The seat identity rides `X-OpenWebUI-User-Id` (the signed-in OWUI user), so the
  document lands in **that user's** FaultLine tenant.
- Failures (bridge down, 401, 429 rate limit) are logged to the OWUI container log with
  the `[FaultLine Attachment Bridge]` prefix and the chat proceeds unchanged — the model
  can still ingest a document the old way if the user asks.

## Verify (2 minutes)

1. Attach a small PDF to a chat with a FaultLine-connected model and send any message.
2. Check `docker logs open-webui` for:
   `[FaultLine Attachment Bridge] bridged 1 attachment(s) for <user-id8>...`
3. Ask the model: *"what happened to the file I attached?"* — it should answer from the
   annotation (document id + document_status), WITHOUT dumping document text.
4. On the FaultLine MCP: `document_status` for that id → `pending` → `ready`.

## Reproduce the bridge with curl (no OWUI required)

```bash
# TOOL-SERVER credential + the seat header — exactly what the filter sends.
FAULTLINE="http://faultline-mcp:8002"          # or https://mcp.faultline.ca
TOOLSERVER_KEY="<the tool-server bearer>"      # MCP_API_KEY (open core) / account key (SaaS)
SEAT="1a2b3c4d-aa71-bb82-4c93-6d5e7f809a2b"    # the OWUI user id (seat)

base64 -w0 ./EPA_Wind_Turbines_Fact_Sheet.pdf > /tmp/doc.b64
python3 -c "import json;print(json.dumps({'filename':'EPA_Wind_Turbines_Fact_Sheet.pdf','data_b64':open('/tmp/doc.b64').read()}))" > /tmp/body.json

# Auth failure shape (pin): missing/wrong bearer → 401
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$FAULTLINE/ingest/attachment" \
  -H "Content-Type: application/json" -H "X-OpenWebUI-User-Id: $SEAT" \
  --data-binary @/tmp/body.json          # → 401

# Correct tool-server bearer + seat → the ingest receipt
curl -s -X POST "$FAULTLINE/ingest/attachment" \
  -H "Authorization: Bearer $TOOLSERVER_KEY" \
  -H "X-OpenWebUI-User-Id: $SEAT" \
  -H "Content-Type: application/json" \
  --data-binary @/tmp/body.json | python3 -m json.tool
# → {"status":"pending", ..., "artefact":{...}, "document":{"document_id":N,...},
#    "bridge":{"document_id":N,"status_tool":"document_status","message":"FaultLine is
#    already ingesting this document ..."}}
```

## Failure modes & where to look

| Symptom | Look at |
|---|---|
| `bridged 0`, nothing in FaultLine | Valves empty/wrong URL; function disabled; `ENABLED=false`. |
| Log: `bridge auth failed (401)` | `FAULTLINE_API_KEY` ≠ tool-server connection key. |
| Log: `bridge rate-limited (429)` | More than 12 posts/60 s for one seat (tunable server-side via `FL_ATTACHMENT_RATE_LIMIT`). |
| Log: `could not be auto-ingested (rejected)` | File type not in the binary door's accepted set (PDF/images only), or a pathological container — expected refusals, reported honestly. |
| Annotation missing from the prompt | The message had no user turn to annotate (system-only) — the file-context annotation is still present. |
