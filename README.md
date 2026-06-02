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

RAG retrieves documents and asks the LLM to interpret them. The answer depends on wording, context, and luck. FaultLine validates facts before storing them and rejects hallucinations at the gate. Your AI knows "AlphaNode's IP is 10.44.5.20" — not "a document mentioned something about a server."

### 4. Correctable, relational data

Facts are stored as typed relationships — person, age, occupation, network device, IP address, MAC, hostname. Corrections update the record cleanly; the old value is archived, not lost. Memory strengthens over time: things mentioned once are held lightly, confirmed facts become authoritative.

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

## `/expand` — teach it a topic

```
/expand networking
/expand kubernetes
/expand home lab
/expand tls online
/expand kubernetes online https://kubernetes.io/docs/concepts/
```

The `online` variants fetch real web content and ground the learning in it. Runs in the background — you get an immediate response and can keep chatting.

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
- [OpenWebUI](https://openwebui.com/) v0.9.5 or newer
- A local LLM via [Ollama](https://ollama.ai/) or [LM Studio](https://lmstudio.ai/) — Qwen2.5 recommended
- 8 GB RAM minimum, 16 GB recommended

---

## Getting started

```bash
git clone https://github.com/tkalevra/FaultLine.git
cd FaultLine

cp .env.example .env
# Open .env and point QWEN_API_URL at your Ollama or LM Studio endpoint

docker compose up -d

curl http://localhost:8000/health
# {"status": "ok", ...}
```

The first start downloads the model used for extraction (~500 MB). Takes 3–5 minutes.

### Connect to OpenWebUI

1. Go to **Workspace → Functions → +** in OpenWebUI
2. Paste the contents of `openwebui/faultline_function.py`
3. Open **Valves** and set `FAULTLINE_URL` to `http://faultline:8000` (or `http://localhost:8000` if not using Docker networking)
4. Enable the filter

Start a conversation — FaultLine begins learning immediately.

---

## Claude Desktop (MCP)

FaultLine's MCP server makes the same memory available to Claude Desktop or any MCP-capable client.

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

Four tools: recall memory, store facts, retract facts, learn topic hierarchies — all backed by the same local store your OpenWebUI conversations write to.

---

## Environment variables

```env
# The two you'll almost certainly need to set
POSTGRES_DSN=postgresql://faultline:faultline@postgres:5432/faultline
QWEN_API_URL=http://host.docker.internal:11434/v1/chat/completions

# Everything else has sensible defaults
QDRANT_URL=http://qdrant:6333
MCP_API_KEY=          # leave blank for no auth, or set a secret token
FAULTLINE_USER_ID=    # optional — pins the MCP server to one user
```

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
