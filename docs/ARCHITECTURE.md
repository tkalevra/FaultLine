# FaultLine Architecture

FaultLine is a **per-tenant, write-validated, deterministic knowledge-graph memory**
for LLM conversations. It extracts entities and relationships from what a user
says, validates them against a metadata-driven ontology, and persists them to
**PostgreSQL** — the authoritative store. A Qdrant vector index is a *derived,
short-term scratchpad* for one tier of facts only; it is never the source of truth.

The live integration path is **MCP tools** (`recall_memory`, `remember_facts`,
`learn_facts`, `retract_fact`). The legacy OpenWebUI Filter exists in the tree but
is intentionally disabled — it is not the production path.

---

## The shape of it

```
                 ┌──────────────────────────── INGEST (strong) ───────────────────────────┐
  user message → │ intent classify → extract (deterministic deriver + GLiNER2 typing +    │
   (via MCP)     │ regex layer) → WGM validation gate → conflict/directionality checks →   │
                 │ Class A/B/C assignment → commit to per-tenant PostgreSQL schema         │
                 └────────────────────────────────────────────────────────────────────────┘
                                                  │
                       PostgreSQL (per-tenant schema) = AUTHORITATIVE long-term memory
                                                  │
                          re-embedder syncs Class C ──▶ Qdrant (short-term only)
                                                  │
                 ┌──────────────────────────── QUERY (lean) ──────────────────────────────┐
  recall call  → │ resolve anchor → build scope → DETERMINISTIC PostgreSQL walk (PRIMARY)  │
   (via MCP)     │ + Qdrant Class-C lane (backstop / promotion gate) → dedup → prose       │
                 └────────────────────────────────────────────────────────────────────────┘
```

**Strong ingest, lean query.** All the intelligence is spent at ingest —
extraction, validation, classification, directionality, conflict resolution,
hierarchy construction. The query side is a deterministic *walk* of what ingest
laid down: it traverses, retrieves, and renders. It does not re-validate,
re-structure, or fuzzy-match. If recall is wrong, the fix belongs at ingest.

> "Strong ingest, *dumb extract*" is a corruption of this principle and appears in
> older docs. Extraction is part of the *strong* ingest half. The *lean* half is
> the query walk.

---

## Where the truth lives: PostgreSQL is authoritative

PostgreSQL holds the structured knowledge graph and **is** long-term memory. Every
served fact resolves to a real row you can point at. There is no step where an LLM
re-interprets retrieved text to answer — the walk returns grounded rows.

Qdrant is a **derived, Class-C-only short-term scratchpad** (see *Class tiering*
below). It is a backstop, not the library. The deterministic PostgreSQL walk runs
first and is authoritative; the vector lane only surfaces short-term "couldn't
classify yet" material and acts as a promotion gate. **The vector never overrides
PostgreSQL.**

This inverts the usual memory-system layout, where a vector store is the
long-term RAG library and a small context window is the short-term scratch.
FaultLine flips it: **the graph is long-term; the vector is short-term.**

---

## Why this isn't RAG (mechanically)

This section is for engineers who have built RAG and want the concrete difference,
not a marketing line.

| | Typical RAG | FaultLine |
|---|---|---|
| **Source of truth at answer time** | A vector index of document chunks | A PostgreSQL knowledge graph (per-tenant) |
| **Retrieval** | Cosine similarity over embeddings → top-k chunks | Anchor resolution → deterministic graph/hierarchy **walk** projected by a scope object |
| **What the LLM gets** | Raw chunks it must interpret | Grounded, resolved facts as plain prose — already true |
| **Write path** | Embed and store everything; no validation | Every write passes the **WGM validation gate** before it can land |
| **Hallucination handling** | Stored alongside real text; surfaces at recall | Rejected at the gate; never written as authoritative |
| **Corrections** | New chunk competes with the old by similarity | Deterministic supersede/archive — the corrected row wins, old soft-deleted |
| **Determinism** | Similarity scores drift with phrasing/model | Same anchor → same walk → same rows, repeatably |
| **Vector role** | The library | A short-term (Class C) scratchpad backstop only |

Mechanically, the difference is this: **RAG answers by ranking text by similarity
and trusting the LLM to read it. FaultLine answers by walking a validated graph and
returning the rows it finds.** Similarity is associative and probabilistic; a graph
walk is structural and deterministic. When you ask "what's DevBox's IP," FaultLine
does not find the most *similar* sentence — it resolves the `DevBox` entity and
reads its `has_ip` attribute. If that row does not exist, it says so (fail loud)
rather than returning the nearest-looking chunk.

The vector index still earns its keep — for the short-term tier of facts the engine
could not yet classify — but it is a backstop behind the deterministic walk, never
the primary retrieval mechanism.

---

## Per-tenant isolation

Each user gets their own PostgreSQL schema (`{prefix}_{user_slug}`). Every request
binds it with `SET search_path TO {schema}` **without `public`**. Consequences:

- One tenant's data is never visible to another at the SQL level.
- **All runtime metadata** (ontology, taxonomies, extraction patterns, linguistic
  cue classes) lives *inside* each tenant schema, seeded from a `public` template
  at provisioning. `public.*` is a **seed source / template only** — never read at
  runtime by ingest or query.
- A per-user Qdrant collection (`faultline-{user_id}`) keeps the short-term tier
  isolated too.

Schemas are created by a background provisioning worker (~1 minute), auto-enqueued
on a user's first request.

---

## The WGM validation gate

The Write-Gated Memory (WGM) gate is the single chokehold every write passes
through. The LLM never has unsupervised write access. The gate:

1. **Routes provenance** — `source="mcp"` → `user_stated`; `source="llm_learn"` →
   `llm_learned`; otherwise `llm_inferred`.
2. **Validates against the ontology** — type constraints (`head_types`/`tail_types`),
   symmetry, inverse relations, hierarchy flags, all read from the `rel_types`
   table via a per-tenant overlay. **Zero hardcoded validation constants** — a new
   relationship type self-describes its rules through table columns.
3. **Detects semantic conflicts** — e.g. if `X instance_of Y` (Y is a type), it
   won't accept `owns Y` / `has_pet Y` on that type entity.
4. **Enforces directionality** — inverts asymmetric relations per metadata
   (`parent_of ↔ child_of`) and resolves symmetric self-loops.
5. **Assigns a class** (A/B/C) and confidence, then commits.

Validation is entirely **metadata-driven**. Adding a relationship type is a data
change, not a code change.

---

## The three storage paths (WHERE a fact goes)

Determined by `rel_types` metadata at ingest:

| Path | Condition | Table | Object |
|---|---|---|---|
| **SCALAR** | `tail_types = {SCALAR}` | `entity_attributes` | A string value (age, height, IP, MAC, hostname, …) |
| **RELATIONAL** | `is_hierarchy_rel = false` | `facts` | A UUID identity (graph edge) |
| **HIERARCHICAL** | `is_hierarchy_rel = true` | `facts` | A UUID identity (classification/composition edge) |

Scalar values are *never* stored in `facts`. Relational and hierarchical facts
share the `facts` table but are traversed by two orthogonal systems — graph
(connectivity: *who am I connected to?*) and hierarchy (classification: *what is
this, what does it belong to?*). Do not conflate them.

---

## Class tiering (durability — A/B in PostgreSQL, C in Qdrant)

Every fact is assigned a class that decides its durability and which store backs it:

| Class | Meaning | Confidence | Backed by | Lifecycle |
|---|---|---|---|---|
| **A** | User-stated / structural | 1.0 | PostgreSQL `facts` | Written through immediately. Authoritative. |
| **B** | Inferred but following established ontology | 0.8 | PostgreSQL `staged_facts` | Promoted into `facts` at `confirmed_count >= 3`. |
| **C** | Couldn't-classify-yet / short-term | ~0.4 | PostgreSQL `staged_facts` **and** Qdrant | Expires after 30 days unless promoted (C→B at occurrence ≥ 3). |

Key invariants:

- **Class A/B live in PostgreSQL only.** They are *never* vector-indexed and
  *never* served from the vector lane.
- **Qdrant holds Class C only** — the short-term scratchpad. The re-embedder skips
  the A/B sync; the query lane drops any Qdrant result whose authoritative class is
  A/B.
- **User corrections are always Class A**, regardless of rel_type. User authority
  overrides everything.
- **We don't forget.** A classification *failure* is not a dropped fact — it is
  captured as Class C and can be promoted later on enough evidence. A/B never
  promote; promotion is a C-tier mechanism only.

---

## The ingest pipeline (strong)

```
intent classify → extract → WGM gate → conflict detection
  → bidirectional/directionality validation → A/B/C assignment → commit
```

1. **Intent classification** routes the message: a question goes to recall, a
   correction/retraction bypasses to the supersede path, a statement goes to
   extraction.
2. **Extraction** is deterministic-first: a spaCy-dependency deriver (SVO,
   possessive/genitive/copula chains, a temporal date layer) does the structured
   work; **GLiNER2 is used for entity typing/discovery only**; a metadata-driven
   regex layer catches atomic scalars (IPs, MACs, emails, dates) and compound
   patterns. An LLM is used for atomization/relation-fill only on the spine path —
   never to invent triples wholesale. (A legacy LLM chunked-relation extractor is
   the default fallback path.)
3. **The WGM gate** validates, resolves conflicts, enforces directionality.
4. **Class assignment + commit** to the per-tenant schema.

### GLiNER2 purity (hard constraint)

GLiNER2 labels must be concise zero-shot type names (`Person`, `Animal`,
`Organization`, …, max 6). Injecting taxonomy descriptions, aggregated blobs, or
extraction patterns into its labels collapses detection. It is GLiNER2 — not "the
LLM," not "RLM."

---

## The query pipeline (lean)

```
resolve anchor → build scope → PostgreSQL walk (PRIMARY) → Qdrant Class-C lane → dedup → prose
```

1. **Resolve the anchor** — pronouns/possessives/names → a user or entity UUID,
   using the *same* resolver ingest used to ground it (groundable ⇒ findable, no
   cosine).
2. **Build a scope object** (`QueryPath`) — a single declarative scope (allowed
   relations, member types, traversal depth/direction) resolved once from the
   per-tenant taxonomy overlay.
3. **Walk PostgreSQL** — baseline facts + 1-hop graph + hierarchy expansion,
   projected by scope. This is authoritative and primary.
4. **Qdrant Class-C lane** — surfaces short-term memory and gates promotion (a
   recall hit bumps a C fact toward C→B). A/B Qdrant results are dropped. It never
   overrides the PostgreSQL walk.
5. **Dedup** on `(subject_uuid, rel_type, object_uuid)` using UUIDs (never display
   names); highest confidence wins; PostgreSQL beats Qdrant.
6. **Render to prose** — perspective resolved at build time (the querying user's own
   facts read as "you", others by preferred alias). No UUIDs or rel_type tokens
   leak into output.

The single language "hook" is that **"I"/"me" = the speaking user**. Everything else
is subject-agnostic: any entity is scoped, walked, and returned the same way.

---

## Self-building ontology (growth)

The system grows its ontology from usage, deterministically — not by code changes
and not by embedding similarity:

- A **novel relationship type** is held as Class C; once it recurs (frequency ≥ 3)
  the re-embedder approves it into `rel_types` (`engine_generated=true`).
- **Synonyms collapse deterministically** via convergence-by-identity plus a
  curated alias table — *not* cosine matching. (A legacy cosine rel-map is retired
  and off by default.)
- **Entity types** refine progressively from `unknown`.
- **Names accumulate** in `entity_aliases`; collisions are queued and arbitrated
  non-destructively.

Growth is **per-tenant**: each user's world grows in their own schema. `public` is
a read-only template you seed *from*, never *into*.

There are two distinct growth pathways — do not conflate them:

- **Engine / natural growth (the ±6).** Every ingest auto-grows a bounded
  hierarchical "semblance" (±6 up/down) around the entity so it is connected and
  walkable — an instant synchronous attach plus an async build-out that never
  blocks recall. The `±6` belongs here; it is an **ingest** build-out, not a query
  bound.
- **`/expand` (learn).** A separate, explicit mechanism that learns a subject's
  fuller depth (optionally web-grounded) with its own anti-sprawl bounding.

---

## Entity model

- **UUIDs are surrogates** (UUID v5) stored in `facts.subject_id/object_id`,
  `entities.id`, `entity_aliases.entity_id`. Never store display names in `*_id`
  columns.
- **Display names** live in `entity_aliases.alias` (lowercased) and
  `entity_attributes.value_*`.
- `entity_aliases` is the authoritative alias registry, unique on
  `(user_id, alias)`.
- **Registry is dumb, query layer is smart** — name filtering/rendering happens
  only at read time in the query path, never at registration.

---

## Temporal model (deterministic dates)

Time is an ingest-parsed hinge, determined deterministically — there is no LLM
tense-guessing and GLiNER2 never touches dates.

- Every fact carries a temporal status (`now`/`past`/`future`) and an optional
  `event_date`.
- Dates are extracted by spaCy DATE NER plus a small numeric regex, then normalized
  and validated by `dateparser` (a rule engine — no embeddings). First valid date
  wins; a non-date span yields `None` and is dropped. **It never fabricates a
  date** — a miss is NULL, never today's wall clock.
- **Dual clock:** `event_date` (valid time — *what* the fact refers to) is kept
  orthogonal to `superseded_at`/`archived_at` (belief/transaction currency).
- The stored coarse status goes stale (an appointment is "future" when stated,
  "past" next month), so **query-time temporal reasoning recomputes from
  `event_date` vs query-now** and does not trust the stored boundary.

---

## Re-embedder background loop

Polls the database (default every 60s) and:

- Embeds unsynced **Class C** staged rows and upserts them to the per-user Qdrant
  collection (the A/B sync is skipped under the C-only model).
- Promotes Class B (`confirmed_count >= 3`) into `facts`; expires Class C past its
  TTL.
- Evaluates ontology candidates (frequency ≥ 3 → approve; synonyms collapse
  deterministically; else reject).
- Resolves name conflicts; reconciles Qdrant against PostgreSQL ground truth
  (deletes orphaned/superseded points).

Qdrant deletes filter by `(source_table, fact_id)` payload — `facts` and
`staged_facts` share a per-user collection with independent id sequences, so a bare
point-id delete is unsafe.

---

## Archive model

User corrections are authoritative and archive conflicting facts **at write time**,
not at query time. `archived_at` is a non-destructive soft-delete; `/query` filters
`archived_at IS NULL` by default, and history is queryable. Supersession is semantic
(e.g. "X is a computer" archives `has_pet X`).

---

## Key design principles

- **PostgreSQL is authoritative (long-term A/B); Qdrant is a derived Class-C-only
  short-term scratchpad.** The vector never overrides PostgreSQL. This is not RAG.
- **Strong ingest, lean query.** Extraction is part of strong ingest; the lean half
  is the query walk. Fix bad recall at ingest, never by bolting cleanup onto query.
- **The LLM has no unsupervised write access** — all writes pass the WGM gate.
- **Validation is metadata-driven** — `rel_types`/`entity_taxonomies` via per-tenant
  overlays; zero hardcoded validation constants.
- **Per-tenant `search_path` has no `public`** — seed metadata into the tenant
  schema; `public` is template-only.
- **GLiNER2 purity** — concise zero-shot type labels only; never inject patterns.
- **Dedup uses UUIDs, not display names.**
- **Fail loud, never silent** — a missing row is reported, not papered over with the
  nearest-looking match.
