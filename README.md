<p align="center">
  <img src="./faultline_logo.svg" alt="FaultLine" width="420"/>
</p>

<h1 align="center">FaultLine</h1>
<p align="center"><strong>Validated, private, shareable memory for your AI.</strong></p>

<p align="center">
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-AGPL%20v3-blue.svg" alt="License: AGPL v3"/></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+"/>
  <img src="https://img.shields.io/badge/OpenWebUI-0.6%2B%20(0.10.x%20tested)-green" alt="OpenWebUI"/>
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
| Open source | ✅ AGPLv3 | ❌ | ✅ MIT | Partial | ✅ MIT |

---

## Multi-tenant isolation

Every user gets a **physically separate PostgreSQL schema** (`faultline_<user_id>`) and their own Qdrant collection. Each request binds to the caller's schema via `SET search_path` **without `public`**, so one tenant's queries cannot even *name* another tenant's tables. There is no shared `user_id` column to filter on and forget — the boundary **is** the schema, enforced by the database.

**Verified on a live instance.** Three fresh tenants, each told one distinct thing, then inspected:

| Tenant | Told | Stored in *its* schema | Any other tenant's data? |
|---|---|---|---|
| A | a cat named Mittens | `cat → feline → mammal → animal` | **none** |
| B | a red Tesla Model 3 | `tesla → vehicle → transportation_device` | **none** |
| C | lives in Berlin, Germany | `berlin → city`, `germany → country` | **none** |

Each schema contained **only** its own entities and hierarchy — zero cross-tenant tokens. A recall issued as one tenant can only ever read from that tenant's schema, so it is structurally impossible for one user's memory to surface in another's. (The per-tenant `±6` hierarchy also grew independently in each — isolation holds through the growth engine, not just at rest.)

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
```

**Guided setup (recommended).** An interactive wizard first asks **Language / Lingua**, then picks your LLM backend, **tests the connection and lists your available models to choose from**, generates a secret `MCP_API_KEY`, sorts out your tenant id, and writes a ready-to-use `.env` — then prints exactly what to paste into your client:

```bash
./setup.sh            # Linux / macOS
setup.bat             # Windows
#  or, on any platform:  python3 quickstart.py
```

> **🌍 Language.** The wizard opens with a language choice. **English** continues on `main`. **Italiano** (experimental) switches to the `it` branch — an unofficial, work-in-progress Italian instance (Italian setup + `LEGGIMI-it.md`); extraction there rides the LLM path. Not production-ready — use at your own risk; for the stable version stay on English/`main`.

**Or configure manually:**

```bash
cp .env.example .env
# Set LLM_BACKEND_TYPE + LLM_BASE_URL to point at the LLM you already run
# (Ollama, LM Studio, OpenWebUI, OpenAI, Anthropic, ...)

docker compose up -d

curl http://localhost:8000/health
# {"status": "ok", ...}
```

Re-run the connectivity check anytime with `python3 quickstart.py --validate`.

FaultLine hooks into an LLM you already run — it doesn't host one. (If you *don't* have a model handy, `docker compose --profile ollama up -d` starts a bundled Ollama alongside the stack.)

The first start downloads the GLiNER2 extraction model (~500 MB, CPU-only — no GPU or CUDA required). Takes 3–5 minutes.

### Connecting a client

Two independent choices — the wizard handles both, or set them by hand.

**1. Which LLM FaultLine talks to** (`LLM_BACKEND_TYPE` + `LLM_BASE_URL` — host+port only, no path):

| Backend | `LLM_BACKEND_TYPE` | `LLM_BASE_URL` (example) |
|---|---|---|
| LM Studio | `lm_studio` | `http://host.docker.internal:1234` |
| Ollama | `ollama` | `http://host.docker.internal:11434` |
| OpenWebUI | `openwebui` | `http://open-webui:8080` |
| OpenAI / Anthropic / Groq | `openai` / `anthropic` / `groq` | provider API base URL |

**2. How your chat client calls FaultLine's memory tools** — all through the **MCP server on `:8002`** (Bearer `MCP_API_KEY`). Any number of clients can share one store at once:

- **OpenWebUI** → Settings → Tools → `+` (OpenAPI), or Admin Settings → External Tools (native MCP, 0.6.31+) *(below)*
- **Claude Desktop** → the `.mcpb` extension *(below)*
- **Cursor / other MCP clients** → `http://<host>:8002/mcp` with header `Authorization: Bearer <MCP_API_KEY>`

### Connect to OpenWebUI

*Verified against OpenWebUI v0.10.x (latest); the paths below apply from v0.6.31+. OpenWebUI's menu labels shift between releases — look for **Tools** (OpenAPI) or **External Tools** (native MCP) if yours differ.*

FaultLine's server on `:8002` speaks **both** OpenAPI and native MCP, so OpenWebUI can reach it two ways. **Use Option A** — it's the only path that reliably forwards the per-user identity header FaultLine scopes on.

**Option A — OpenAPI tool server (recommended):**

1. **Settings → Tools → `+`** (Manage Tool Servers). For an instance-wide server, use **Admin Settings → Tools** instead.
2. **URL:** `http://faultline-mcp:8002` (same Docker network) or `http://<host>:8002`. The modal may pre-fill `https://` — change it to `http://` for a local server, then hit **refresh** to test.
3. **Auth:** `Bearer` → your `MCP_API_KEY`.
4. Set **`ENABLE_FORWARD_USER_INFO_HEADERS=true`** in OpenWebUI's environment so each user's memory is scoped to them. It forwards the `X-OpenWebUI-User-Id` header FaultLine keys on (also `-User-Name`/`-Email`/`-Role`) — without it, per-user memory won't work.

OpenWebUI reads `/openapi.json` and surfaces `recall_memory`, `remember_facts`, `retract_fact` as tools in every conversation.

**Option B — native MCP (OpenWebUI 0.6.31+):**

1. **Admin Settings → External Tools → `+` (Add Server)** — native MCP is **admin-only**.
2. **Type:** `MCP (Streamable HTTP)` (the only transport OpenWebUI's native MCP supports).
3. **URL:** `http://<host>:8002/mcp` · **Auth:** `Bearer` → your `MCP_API_KEY`.

> ⚠️ **Native MCP is the newer, less-tested path with FaultLine** (its `/mcp` is a stateless JSON-RPC endpoint) — if the server or tools don't appear, use **Option A**. And per-user scoping is weaker here: as of v0.10.x, `ENABLE_FORWARD_USER_INFO_HEADERS` does **not** forward `X-OpenWebUI-User-*` headers over native MCP, and custom-header template tokens (`{{USER_ID}}`) may not resolve there yet ([open-webui#21134](https://github.com/open-webui/open-webui/issues/21134)). For reliable per-user memory use **Option A**; if you run native MCP, pin a single tenant with `FAULTLINE_USER_ID`.

**Tool firing (weak models).** As of v0.10.0, OpenWebUI defaults every model to **Native** function calling (the old "Default" mode is renamed **"Legacy"** and is unsupported). Native works for most 2024+ models; if a smaller model (Qwen, Llama, Mistral, Phi) won't reliably call the tools, switch **Chat Controls → Advanced Params → Function Calling → Legacy** (or set it per-model / globally under **Model Parameters**).

> **Legacy alternative:** the inlet/outlet Filter (`openwebui/faultline_function.py`, Workspace → Functions) still exists for automatic injection, but the tool-server paths above are the supported ones.

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

### System prompt (important)

FaultLine works best when the model is explicitly told to use the MCP tools for all memory operations. Without a system prompt, some clients (notably Claude Desktop) will use their own built-in memory instead of FaultLine.

Ready-to-paste prompts are in the [`prompts/`](prompts/) folder:

| File | When to use |
|---|---|
| [`strong-model-prompt.md`](prompts/strong-model-prompt.md) | Claude, GPT-4o, Gemini Pro — models with reliable tool-calling |
| [`weak-model-prompt.md`](prompts/weak-model-prompt.md) | Qwen, Llama, Mistral, Phi — smaller models that need explicit step-by-step instructions |

Pick the one that matches your model, copy the prompt block, and paste it into your client's system prompt field.

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
| `quickstart.py` · `setup.sh` · `setup.bat` | Guided setup wizard — writes `.env`, tests connectivity, lists your models |
| `src/mcp/server.py` | The MCP tool server — the live integration path |
| `src/api/main.py` | The backend API |
| `openwebui/faultline_function.py` | Legacy OpenWebUI Filter (the MCP tool server above is preferred) |
| `migrations/` | Database schema — runs automatically on first start |

---

## Built with

[PostgreSQL](https://www.postgresql.org/) · [Qdrant](https://qdrant.tech/) · [Redis](https://redis.io/) · [FastAPI](https://fastapi.tiangolo.com/) · [GLiNER2](https://github.com/fastino-ai/GLiNER2) · [nomic-embed-text-v1.5](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5) · [OpenWebUI](https://openwebui.com/)

---

## Licensing

FaultLine is **open core**, licensed under the **[GNU AGPLv3](./LICENSE)**.

**If you are a user, this changes nothing for you.** Run it, modify it, self-host it — for yourself
or for your company — forever, for free. That is what it is for.

The AGPL asks for one thing back: **if you modify FaultLine and offer it to others over a network,
your users get your modified source.** Improvements to the commons stay in the commons. Don't modify
it, and you owe nothing but the licence notice.

If that doesn't suit you — you want to build a closed-source product on FaultLine, or embed it in a
proprietary offering — **a commercial licence is available.** Open an issue or get in touch.

> **Licence history:** FaultLine was Apache-2.0 through commit `433daf1`. That grant is perpetual and
> is not revoked — copies obtained under it stay Apache-2.0. Everything from the relicensing commit
> onward is AGPLv3. See [NOTICE](./NOTICE).

## Contributing

Contributions are welcome, and there is **one gate: a signed [CLA](./CLA.md)**. It exists so the
project can keep dual-licensing, which is what funds the open engine — the reasoning is laid out
honestly in **[CONTRIBUTING.md](./CONTRIBUTING.md)**. You keep the copyright in your work.

House rules, in short:

- New relationship types belong in the `rel_types` database table, not in code
- GLiNER2 zero-shot labels are never modified to carry extraction patterns or descriptions
- No UUIDs in anything a user sees
- Strong ingest, lean query — if recall is wrong, fix it at ingest
- All tests pass: `pytest tests/`
