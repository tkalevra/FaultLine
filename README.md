## Design Principles (Plain English)

FaultLine is built to **play nicely with every layer it touches**. Nothing is forced, hacked, or “almost compatible”.

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

This is not just privacy‑friendly — it’s **correct**.

> “The assistant remembers me” does not mean  
> “The system remembers everyone”

---

### 3. Short‑term vs Long‑term memory (human‑style)

FaultLine models memory the way people do it:

- **Short‑term memory**  
  What’s being talked about right now (the current conversation)

- **Long‑term memory**  
  Verified facts that persist across conversations

- **Fast recall**  
  “This seems related” hints used to surface helpful context

Nothing automatically becomes long‑term memory.  
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

(See Mermaid diagram in project docs)

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
