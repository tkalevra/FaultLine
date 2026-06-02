<p align="center">
  <img src="./faultline_logo.svg" alt="FaultLine" width="420"/>
</p>

<h1 align="center">FaultLine</h1>
<p align="center"><strong>Your AI actually remembers you.</strong></p>

<p align="center">
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License"/></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+"/>
  <img src="https://img.shields.io/badge/OpenWebUI-0.9.5%2B-green" alt="OpenWebUI"/>
  <img src="https://img.shields.io/badge/MCP-Claude%20Desktop-purple" alt="MCP"/>
</p>

---

FaultLine gives your local AI a real memory. It runs quietly in the background — watching your conversations, learning who you are and what matters to you, and making sure your AI already knows the important stuff the next time you chat.

**Everything stays on your machine. No cloud. No subscriptions.**

---

## What changes

Without FaultLine, your AI starts fresh every single conversation. It doesn't know your name, your family, your preferences, or anything you've told it before. With FaultLine, it does.

```
You:  "My name is Chris and I'm 42. I have a daughter named Gabby."

Next week, fresh conversation:
You:  "What do you know about me?"
AI:   "Your name is Chris, you're 42 years old, and you have a daughter named Gabby."
```

```
You:  "My home server AlphaNode has ip 10.44.5.20"

Later:
You:  "What's AlphaNode's IP?"
AI:   "AlphaNode has IP address 10.44.5.20."
```

```
You:  /expand networking

Later, after mentioning your router:
AI:   "Your router is a networking device at 192.168.1.1 — I remember that from earlier."
```

**It remembers names. Ages. Relationships. Preferences. Corrections. Server IPs. Birthdays. Pets.** Anything you tell it, in plain English.

---

## What it does behind the scenes

You don't need to know any of this to use it — but if you're curious:

- Every message gets checked for facts worth keeping ("my dog is called Spot", "my router IP is 192.168.1.1")
- Facts go through a validation step before being stored, so hallucinated details don't pollute your memory
- Relevant facts are quietly injected into the conversation before the AI responds — it just *knows*
- Memories strengthen over time. Things you mention once are held lightly. Things you confirm repeatedly become rock-solid
- Corrections work. Tell it "actually Des is 13, not 12" and it updates — the old fact is archived, not deleted

---

## `/expand` — teach it a topic

Type `/expand <topic>` to teach FaultLine how a subject is structured. After that, your personal facts about that topic are understood in context.

```
/expand networking
/expand kubernetes
/expand my home lab
/expand tls online
/expand kubernetes online https://kubernetes.io/docs/concepts/
```

The `online` variants fetch real web content and ground the learning in it. Everything runs in the background — you get an immediate response and can keep chatting.

---

## Comparison

| | FaultLine | ChatGPT Memory | MemGPT / Letta | Mem0 | OpenWebUI RAG |
|---|---|---|---|---|---|
| Self-hosted, fully private | ✅ | ❌ Cloud only | ✅ | ✅ | ✅ |
| Works with any local LLM | ✅ | ❌ OpenAI only | ✅ | ✅ | ✅ |
| MCP server (Claude Desktop) | ✅ | ❌ | ❌ | Partial | ❌ |
| Remembers relationships & people | ✅ | ✅ | Partial | Partial | ❌ |
| Remembers server IPs, MACs, emails | ✅ | ❌ | ❌ | ❌ | ❌ |
| Corrections update memory cleanly | ✅ | ❌ Overwrites | ❌ | ❌ | ❌ |
| `/expand` topic learning | ✅ | ❌ | ❌ | ❌ | ❌ |
| Web-grounded learning | ✅ | ❌ | ❌ | ❌ | ❌ |
| Short → long-term promotion | ✅ | ❌ | Partial | ❌ | ❌ |
| Dead-naming / preferred name support | ✅ | ❌ | ❌ | ❌ | ❌ |
| Per-user private memory | ✅ | ✅ Account | ✅ | ✅ | ❌ Shared |
| Validated writes (no hallucination storage) | ✅ | ❌ | ❌ | ❌ | ❌ |
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

The first start downloads the AI model used for extraction (~500 MB). Takes 3–5 minutes.

### Connect it to OpenWebUI

1. Go to **Workspace → Functions → +** in OpenWebUI
2. Paste the contents of `openwebui/faultline_function.py`
3. Open **Valves** and set `FAULTLINE_URL` to `http://faultline:8000` (or `http://localhost:8000` if not using Docker networking)
4. Enable the filter

That's it. Start a conversation and FaultLine begins learning straight away.

---

## Claude Desktop (MCP)

FaultLine includes a built-in MCP server so Claude Desktop can store and recall memories too.

Add this to your Claude Desktop config:

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

Claude will then have access to four tools: recalling memories, storing facts, retracting facts, and learning topic hierarchies — all backed by your local FaultLine instance.

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
| `src/mcp/server.py` | The MCP tool server for Claude Desktop |
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
