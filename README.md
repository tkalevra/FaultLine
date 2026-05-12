![FaultLine](docs/faultline_logo.svg)

## Design Principles (Plain English)

FaultLine is built to **play nicely with every layer it touches**. Nothing is forced, hacked, or "almost compatible".
## How FaultLine Compares

Most AI memory systems trust the LLM to write whatever it extracts. FaultLine doesn't — every fact passes a validation gate before it touches storage. It's the only system in the field that treats the model as an untrusted writer by design.

|Project|Open Source|Self-Host|Write Gate|Knowledge Graph|Short → Long Term|Per-User|Temporal|OpenWebUI|
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
|**FaultLine**|✅ MIT|✅|✅|✅|✅ A→B→C pipeline|✅|✅|✅|
|Graphiti (Zep)|✅ Apache|✅|❌|✅|❌|⚠️|✅|❌|
|Zep Cloud|❌|❌|❌|✅|❌|✅|✅|❌|
|Mem0|⚠️ open-core|⚠️|❌|⚠️ paid|❌|✅|⚠️|❌|
|Letta / MemGPT|✅ Apache|✅|❌|❌|⚠️ agent-managed|✅|❌|❌|
|Cognee|✅ Apache|✅|❌|✅|❌|⚠️|⚠️|❌|
|EverMind / EverOS|✅ Apache|✅|❌|✅|⚠️ expiry only|⚠️|✅|❌|
|LangMem|✅ MIT|✅|❌|❌|❌|⚠️|❌|❌|
|LlamaIndex Memory|✅ MIT|✅|❌|⚠️|❌|⚠️|❌|❌|
|LightRAG|✅ MIT|✅|❌|✅|❌|❌|❌|❌|
|SuperLocalMemory|✅ Apache|✅|❌|❌|❌|❌|❌|❌|
|OMEGA|✅ Apache|✅|❌|❌|❌|❌|❌|❌|
|Memary|✅ MIT|✅|❌|✅ Neo4j|❌|❌|❌|❌|
|Motorhead|✅ MIT|✅|❌|❌|❌|⚠️|❌|❌|
|CrewAI Memory|✅ MIT|✅|❌|❌|❌|❌|❌|❌|
|Hermes Agent|✅ Apache|✅|❌|⚠️|⚠️ episodic|❌|❌|❌|
|Hindsight|❌|❌|❌|⚠️|❌|✅|⚠️|❌|
|Supermemory|❌|⚠️ enterprise|❌|⚠️|❌|✅|❌|❌|
|Cloudflare Agent Memory|❌|❌|❌|❌|❌|✅|❌|❌|
|Microsoft MAF|✅ MIT|⚠️ Azure|❌|❌|❌|⚠️|❌|❌|

> ✅ Full  ·  ⚠️ Partial or paywalled  ·  ❌ Not supported

**Short → Long Term** refers to staged fact promotion: facts move from ephemeral (Class C) through behavioral (Class B) to permanent storage (Class A) based on confirmation, rather than writing everything to long-term storage immediately or never at all.

FaultLine is the only entry with a validated promotion pipeline — unconfirmed facts expire, confirmed facts persist, and nothing is written unsupervised at any stage.

---

### 1. Developer‑compliant by design

Each component is used **the way its authors intended**:

- **GLiNER2**
  - Receives schema‑typed inputs, not raw prompts
  - Outputs structured entities and relations exactly in its preferred format
  - No prompt abuse, no post‑hoc guessing

- **OpenWebUI**
  - Uses official inlet filters and function hooks
  - Context is injected using supported message roles
  - Nothing bypasses or mutates internal state unexpectedly

- **PostgreSQL**
  - Single‑transaction writes
  - Explicit uniqueness constraints prevent duplication
  - Facts are stored as data, not blobs of text

- **Qdrant**
  - Used strictly as a *derived* index
  - Never treated as source‑of‑truth memory
  - Rebuilt safely from Postgres when needed

**Result:**  
If any component is swapped, upgraded, or audited, FaultLine remains predictable and maintainable.

---

### 2. Memory is strictly separated per user

- Each user has **isolated long‑term memory**
- No facts are shared across users
- No embeddings are cross‑user
- Queries are always scoped by `user_id`

This is not just privacy‑friendly — it's **correct**.

> "The assistant remembers me" does not mean  
> "The system remembers everyone"

---

### 3. Short‑term vs Long‑term memory (human‑style)

FaultLine models memory the way people do it:

- **Short‑term memory**  
  What's being talked about right now (the current conversation)

- **Long‑term memory**  
  Verified facts that persist across conversations

- **Fast recall**  
  "This seems related" hints used to surface helpful context

Nothing automatically becomes long‑term memory.  
It must pass validation first.

---

## Why This Improves Answers (Concrete Example)

### Without FaultLine (typical AI behavior)

User:  
> "What's the weather like tomorrow?"hing automatically becomes long‑term memory.  
It must pass validation first.

---

## Why This Improves Answers (Concrete Example)

### Without FaultLine (typical AI behavior)

User:  
> “What’s the weather like tomorrow?”

Assistant:  
> “I don’t know where you are.”

Why this happens:
- Location was mentioned earlier
- It fell out of the conversation window
- It was never stored as a real fact

---
### With FaultLine

User:  
> “What’s the weather like tomorrow?”

System already knows:
- User lives in Mianus
- That fact was validated and stored earlier

Assistant:  
> “Tomorrow in Mianus, expect…”

**No guessing. No asking again. No hallucination.**

---
## Why This Is Different From Vector‑Only Memory

Most “memory systems” today:
- Store chunks of text
- Search by similarity
- Hope the retrieved text is correct

FaultLine:
- Stores **validated facts**
- Checks for conflicts
- Uses vectors only as a **hint system**, never as truth

> Vectors help you *find* memory  
> Facts decide what memory *is true*

---
## Extremely Simple Mental Model

Flow in plain words:
1. User says something
2. It lives in short‑term memory
3. If it looks like a real fact, it’s checked
4. If valid, it’s written to long‑term memory
5. Related memories are recalled to improve answers

---
## Why This Matters

FaultLine doesn’t try to be clever — it tries to be **correct**.  
It respects component boundaries, keeps user data isolated, separates conversation from memory, and only remembers things that pass validation.

The result is an assistant that feels more human:
- It remembers what matters
- It forgets what doesn’t
- It doesn’t ask the same questions over and over

---
### Novel concepts drawn from
|#|Novel Aspect|Status in the Wild|Reference|
|---|---|---|---|
|1|**Write Gate**|Theorized in research, never shipped in production|[arxiv 2603.15994](https://arxiv.org/abs/2603.15994)|
|2|**Fact Lifecycle**|Benchmarks confirm most systems fail on selective forgetting — no production system implements classified promotion|[arxiv 2603.07670](https://arxiv.org/abs/2603.07670)|
|3|**Self-Building Ontology**|Open research problem; all existing approaches require static schemas|[arxiv 2604.20795](https://arxiv.org/abs/2604.20795)|
|4|**Mnemonic Sovereignty**|Framed as a normative goal in 2026 security literature — no deployed system implements it end-to-end|[arxiv 2604.16548](https://arxiv.org/abs/2604.16548)|
|5|**Metadata-Driven Validation**|Memory governance decoupled from evolution identified as an unsolved problem|[arxiv 2603.11768](https://arxiv.org/abs/2603.11768)|
