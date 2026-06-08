<p align="center">
  <img src="./faultline_logo.svg" alt="FaultLine" width="420"/>
</p>

<h1 align="center">FaultLine</h1>
<p align="center"><strong>Validated, private, shareable memory for your AI.</strong></p>

<p align="center">
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License"/></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+"/>
  <img src="https://img.shields.io/badge/OpenWebUI-0.9.5%2B-green" alt="OpenWebUI"/>
  <img src="https://img.shields.io/badge/MCP-2025--03--26-purple" alt="MCP"/>
</p>

---

FaultLine adds persistent, validated memory to your local AI. Facts are stored in a structured knowledge graph — not summarised into text blobs, not guessed from documents. When you correct something, it updates. When you ask about it later, the answer is right.

---

## Why FaultLine

### 1. Private by design

Everything runs on your machine. Your conversations, your memories, your data — none of it leaves. No cloud sync, no accounts, no subscriptions.

### 2. Share memory across any AI

FaultLine exposes an MCP server. That means the same memory store is available to OpenWebUI, Claude Desktop, or any other MCP-capable endpoint. Change your server IP in one conversation, every model sees the update.

### 3. Write-validated facts — not RAG guesswork

RAG retrieves documents and asks the LLM to interpret them. The answer depends on wording, context, and luck. FaultLine validates facts before storing them and rejects hallucinations at the gate. Your AI knows "AlphaNode's IP is 192.0.2.20" — not "a document mentioned something about a server."

### 4. Correctable, relational data

Facts are stored as typed relationships — person, age, occupation, network device, IP address, MAC, hostname. Corrections update the record cleanly; the old value is archived, not lost. Memory strengthens over time: things mentioned once are held lightly, confirmed facts become authoritative.

### 5. Teach it any domain — on your terms

`/expand networking` teaches FaultLine how networking works before you mention your first device. `/expand kubernetes online https://kubernetes.io/docs/concepts/` reads the actual docs and builds the ontology from them. You point it at the source, it learns the structure, and every fact you mention in that domain from then on is stored and retrieved in context — not as an isolated string. And everything it learns is still correctable by you.

---

## What it looks like in practice

```
You:  "I'm a sysadmin. My main workstation is called DevBox, IP 10.0.1.5."

Next session:
You:  "What do you know about DevBox?"
AI:   "DevBox is your workstation, IP address 10.0.1.5."
```

```
You:  "Actually DevBox moved to 10.0.1.10 after the network change."

Next session:
You:  "What's DevBox's IP?"
AI:   "DevBox has IP address 10.0.1.10."
```

```
You:  /expand networking

Later, after mentioning your firewall:
AI:   "Your firewall OPNsense is a networking device — I have that from earlier."
```

**People, preferences, server IPs, MAC addresses, hostnames, relationships, corrections.** Anything you tell it, stored and ready.

---

## How it works (briefly)

- Every message is scanned for facts worth keeping
- Facts go through a validation gate before storage — hallucinated details are rejected
- Relevant facts are injected into the conversation before the AI responds
- Memories strengthen with confirmation; corrections archive the old value cleanly
- `/expand <topic>` teaches FaultLine how a domain is structured so facts about it land correctly

---

## `/expand` — on-demand domain intelligence

Most memory systems store facts. FaultLine can learn how a *domain* works — the structure, the relationships, the vocabulary — so that facts about it land correctly and mean something.

Without a domain expansion, "my firewall is OPNsense" is stored as a flat string. With `/expand networking`, FaultLine knows that a firewall is a type of network device, that it has IP addresses, that it sits between your LAN and WAN, and how it relates to switches, routers, and hosts. Every networking fact you mention after that is stored and retrieved in context.

**The difference in practice:**

```
Without /expand:

You:  "OPNsense is at 10.0.0.1, the switch is at 10.0.0.2"
AI:   "OPNsense has IP 10.0.0.1 and the switch has IP 10.0.0.2."
      (two isolated facts, no relationship, no structure)

With /expand networking:

You:  "OPNsense is at 10.0.0.1, the switch is at 10.0.0.2"
AI:   "Your firewall OPNsense is at 10.0.0.1. Your switch is at 10.0.0.2,
       downstream from it." (stored relationally — device types, roles, topology)
```

### You control the source

Point it at the actual documentation and it grounds the expansion in that material — not in what the LLM guesses the domain looks like:

```
/expand kubernetes online https://kubernetes.io/docs/concepts/
/expand tls online https://www.rfc-editor.org/rfc/rfc8446
/expand networking
/expand home lab
/expand kubernetes
```

Without a URL, it reasons from its training knowledge. With one, it reads the source and builds the ontology from it. You decide how authoritative you need it to be.

### Still correctable

Everything `/expand` builds is subject to the same correction rules as any other fact. If it got the domain structure wrong, tell it — your correction wins and is stored as authoritative. The expansion is a starting point, not a constraint.

### It compounds

Expand once and every future conversation in that domain benefits automatically. New facts slot into the right relationships. Queries about that domain return structured, contextual answers instead of isolated strings. The more you use it, the more useful it gets.

---

## Comparison

| | FaultLine | ChatGPT Memory | MemGPT / Letta | Mem0 | OpenWebUI RAG |
|---|---|---|---|---|---|
| Self-hosted, fully private | ✅ | ❌ Cloud only | ✅ | ✅ | ✅ |
| Works with any local LLM | ✅ | ❌ OpenAI only | ✅ | ✅ | ✅ |
| MCP server — share across models | ✅ | ❌ | ❌ | Partial | ❌ |
| Write-validated (no hallucination storage) | ✅ | ❌ | ❌ | ❌ | ❌ |
| Correctable — updates cleanly, archives old | ✅ | ❌ Overwrites | ❌ | ❌ | ❌ |
| Relational facts (not document chunks) | ✅ | ❌ | Partial | Partial | ❌ |
| Remembers server IPs, MACs, hostnames | ✅ | ❌ | ❌ | ❌ | ❌ |
| Remembers relationships & people | ✅ | ✅ | Partial | Partial | ❌ |
| Short → long-term promotion | ✅ | ❌ | Partial | ❌ | ❌ |
| `/expand` topic learning | ✅ | ❌ | ❌ | ❌ | ❌ |
| Web-grounded learning | ✅ | ❌ | ❌ | ❌ | ❌ |
| Dead-naming / preferred name support | ✅ | ❌ | ❌ | ❌ | ❌ |
| Per-user private memory | ✅ | ✅ Account | ✅ | ✅ | ❌ Shared |
| Open source | ✅ Apache 2.0 | ❌ | ✅ MIT | Partial | ✅ MIT |

---

## Requirements

- Docker and Docker Compose
- An LLM backend — [Ollama](https://ollama.ai/), [LM Studio](https://lmstudio.ai/), [OpenWebUI](https://openwebui.com/), or a hosted API (OpenAI, Anthropic, Groq)
- 8 GB RAM minimum, 16 GB recommended

---

## Getting started

```bash
git clone https://github.com/tkalevra/FaultLine.git
cd FaultLine

cp .env.example .env
# Set LLM_BACKEND_TYPE + LLM_BASE_URL to point at the LLM you already run
# (Ollama, LM Studio, OpenWebUI, OpenAI, Anthropic, ...)

docker compose up -d

curl http://localhost:8000/health
# {"status": "ok", ...}
```

FaultLine hooks into an LLM you already run — it doesn't host one. (If you *don't* have a model handy, `docker compose --profile ollama up -d` starts a bundled Ollama alongside the stack.)

The first start downloads the GLiNER2 extraction model (~500 MB, CPU-only — no GPU or CUDA required). Takes 3–5 minutes.

### Connecting a client

The backend is the same for every client — only a few env values change. Point `LLM_BACKEND_TYPE` / `LLM_BASE_URL` at whatever you run, and connect over HTTP (the OpenWebUI filter on `:8000`) or MCP (`:8002`). Any number of clients can share the one store at once.

| Client | `LLM_BACKEND_TYPE` | `LLM_BASE_URL` (example) | How it connects |
|---|---|---|---|
| OpenWebUI | `openwebui` | `http://open-webui:8080` | Filter function → `:8000` |
| LM Studio | `lm_studio` | `http://host.docker.internal:1234` | Filter function → `:8000` |
| Ollama (direct) | `ollama` | `http://host.docker.internal:11434` | Filter function → `:8000` |
| OpenAI / Anthropic | `openai` / `anthropic` | provider API base URL | Filter function → `:8000` |
| Claude Desktop | *(n/a — Claude is the client)* | — | MCP → `:8002` |

### Connect to OpenWebUI

1. Go to **Workspace → Functions → +** in OpenWebUI
2. Paste the contents of `openwebui/faultline_function.py`
3. Open **Valves** and set `FAULTLINE_URL` to `http://faultline:8000` (or `http://localhost:8000` if not using Docker networking)
4. Enable the filter

Start a conversation — FaultLine begins learning immediately.

---

## Claude Desktop (MCP)

FaultLine ships a `.mcpb` extension for one-click installation in Claude Desktop.

### Install the extension

1. Build the extension (requires Python):
   ```bash
   cd tools/claude-desktop
   python build_mcpb.py
   # → produces faultline.mcpb
   ```

2. In Claude Desktop: **Settings → Extensions → Advanced settings → Install Extension** → select `faultline.mcpb`

3. Claude Desktop prompts for three values:

   | Field | What it is | How to get it |
   |---|---|---|
   | **FaultLine MCP URL** | HTTP endpoint for the MCP server | Default: `http://localhost:8002`. Change the host if FaultLine runs on another machine. |
   | **User ID** | UUID that isolates your memory store | Generate one: `python -c "import uuid; print(uuid.uuid4())"`. If you also use OpenWebUI, use the same UUID from **OpenWebUI → Settings → Account** so both clients share one memory store. |
   | **MCP API Key** | Bearer token for authentication | Must match `MCP_API_KEY` in your `.env`. Generate one: `python -c "import secrets; print(secrets.token_hex(32))"` |

4. Make sure your Docker stack is running (`docker compose up -d`) — the extension connects to the MCP server at port 8002.

### How it works

The extension is a thin stdio-to-HTTP proxy. Claude Desktop spawns it as a local process; it forwards JSON-RPC messages to your Docker MCP server. No SDK dependencies — just Python stdlib.

```
Claude Desktop (stdio) → faultline_proxy.py → HTTP → localhost:8002/mcp → Docker
```

### Alternative: direct HTTP (Streamable HTTP clients)

MCP clients that support HTTP transport directly (no stdio needed) can connect without the extension:

```json
{
  "mcpServers": {
    "faultline": {
      "url": "http://YOUR-HOST:8002/mcp",
      "headers": { "Authorization": "Bearer YOUR_MCP_API_KEY" }
    }
  }
}
```

### Tools available

| Tool | Description |
|---|---|
| `recall_memory` | Query the knowledge graph — retrieves facts relevant to the conversation |
| `remember_facts` | Extract and store facts from conversation text |
| `learn_facts` | Ingest structured fact triples directly |
| `retract_fact` | Remove a fact from the knowledge graph |

All four tools are backed by the same store your OpenWebUI conversations write to.

---

## Environment variables

```env
# The LLM hook — which model server FaultLine talks to.
# LLM_BACKEND_TYPE selects the protocol; the API path is appended automatically.
LLM_BACKEND_TYPE=ollama                         # openwebui | ollama | lm_studio | openai | anthropic | groq | localai | raw
LLM_BASE_URL=http://host.docker.internal:11434  # host + port only, no path
LLM_API_KEY=                                    # blank for local servers; token for hosted APIs

# Storage
POSTGRES_DSN=postgresql://faultline:faultline@postgres:5432/faultline
QDRANT_URL=http://qdrant:6333

# MCP server
MCP_API_KEY=          # leave blank for no auth, or set a secret token
FAULTLINE_USER_ID=    # optional — pins the MCP server to one user
```

See [`.env.example`](.env.example) for the full list with descriptions.

---

## Key files

| File | What it is |
|---|---|
| `openwebui/faultline_function.py` | The OpenWebUI filter — drop this in and you're running |
| `src/api/main.py` | The backend API |
| `src/mcp/server.py` | The MCP tool server |
| `migrations/` | Database schema — runs automatically on first start |

---

## Built with

[PostgreSQL](https://www.postgresql.org/) · [Qdrant](https://qdrant.tech/) · [Redis](https://redis.io/) · [FastAPI](https://fastapi.tiangolo.com/) · [GLiNER2](https://github.com/fastino-ai/GLiNER2) · [nomic-embed-text-v1.5](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5) · [OpenWebUI](https://openwebui.com/)

---

## License

Apache 2.0 — see [LICENSE](./LICENSE).

## Contributing

- New relationship types belong in the `rel_types` database table, not in code
- GLiNER2 zero-shot descriptions should never be modified to include extraction patterns
- No UUIDs in anything a user sees
- All tests pass: `pytest tests/`
