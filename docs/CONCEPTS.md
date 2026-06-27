# FaultLine Core Concepts

The ideas FaultLine is built on, hinged together and cross-linked. Read this after
the [README](../README.md) and alongside [ARCHITECTURE.md](ARCHITECTURE.md) (the
mechanical view) and [FLOW-BRIEF.md](FLOW-BRIEF.md) (the plain-English view).

Each concept below links to where it shows up mechanically.

---

## 1. The hard line — a memory vs. the place to put a memory

This is the distinction everything else hangs on.

- **A memory** is *what the user told you*: a pet named Rex, "my favourite colour
  is red," an IP address, a person's name. It is **user truth — grounded, never
  modified by the engine**. It lives as captured values in the entity/alias/
  attribute/fact rows.
- **The place** (the type/class hierarchy — see *L4* below) is *what a thing IS*:
  `dog → canine → mammal → animal`. It is the shelf, the index. It is **built and
  grown by the engine.**

You *file* a memory **at** a place and *walk* the place to **retrieve** it. A name or
value never becomes a place; a place is never user content. Classifying a *name*
(Rex) into the type hierarchy is a category error — names live in the naming
layer (`also_known_as` / `pref_name`); only *types* (dog, canine) are classified.

This line is the **truth firewall**: because a memory is grounded user content *filed
at* a place, the engine growing **places** can never corrupt the **memory**. It is
the boundary between *what the engine may assume and walk* (places) and *what is
actually true* (memories).

→ Mechanically: the entity model and naming layer in
[ARCHITECTURE.md → Entity model](ARCHITECTURE.md#entity-model); class assignment in
[Class tiering](ARCHITECTURE.md#class-tiering-durability--ab-in-postgresql-c-in-qdrant).

---

## 2. L4 — the founding bounding layer (hierarchy as index)

L4 is the **type/class hierarchy** — the "place" from concept 1. It is the *founding*
layer because almost everything is hierarchable (feelings, concepts, time, IP
addresses), so L4 is a **universal index**, not a per-subject feature.

Two things make L4 the keystone:

- **It is built at ingest, not at query.** All the intelligence is spent grounding a
  fact into the hierarchy when it arrives. The query is then a *dumb walk* of what
  ingest laid down.
- **It is the anti-stray measure.** Recall stays anchored to what's *true* (a real
  row in a real place) instead of what an LLM half-remembers 40k tokens deep. A
  query miss is resolved by **walking** the hierarchy with the same machinery ingest
  used to ground it — groundable ⇒ findable, no fuzzy matching.

A coherent standalone grouping is first-class: ingest an entire domain and it forms
its own walkable body, surfaced deterministically, *without* being tied to the user.
Success is "did the deterministic walk surface the right coherent grouping for the
anchor" — not "did it connect to the user."

→ Mechanically: [ARCHITECTURE.md → The query pipeline](ARCHITECTURE.md#the-query-pipeline-lean)
and [the three storage paths](ARCHITECTURE.md#the-three-storage-paths-where-a-fact-goes).

---

## 3. Determinism — strong ingest → deterministic walk → fail loud

FaultLine's contract is: **spend all the intelligence at ingest, make retrieval a
brain-dead deterministic walk, and fail loud when a fact is absent.**

- **Strong ingest:** extraction, validation, A/B/C classification, directionality,
  conflict resolution, and hierarchy construction all happen on the way in.
- **Lean query:** the query traverses, retrieves, and renders. It does *not*
  re-validate, re-structure, or fuzzy-match. If recall is wrong, the fix belongs at
  ingest.
- **Fail loud:** a missing row is reported, not papered over with the nearest-looking
  match.

> "Strong ingest, *dumb extract*" is a corruption of this — extraction is part of the
> *strong* ingest half. The *lean/dumb* half is the query walk.

→ Mechanically: [ARCHITECTURE.md → Why this isn't RAG](ARCHITECTURE.md#why-this-isnt-rag-mechanically).

---

## 4. The WGM gate — no unsupervised writes

The Write-Gated Memory gate is the single chokehold every write passes through. The
LLM never writes unsupervised. The gate validates against the metadata-driven
ontology, detects semantic conflicts, enforces directionality, assigns a class, and
commits. Validation is entirely metadata-driven — adding a relationship type is a
*data* change, not a *code* change.

→ Mechanically: [ARCHITECTURE.md → The WGM validation gate](ARCHITECTURE.md#the-wgm-validation-gate).

---

## 5. Class tiering — A/B authoritative, C short-term

- **Class A** — user-stated / structural. PostgreSQL, written through immediately.
- **Class B** — inferred but following established ontology. PostgreSQL staged,
  promoted at three confirmations.
- **Class C** — couldn't-classify-yet / short-term. PostgreSQL staged **and**
  mirrored to the Qdrant vector index; expires unless promoted (C→B).

The growth engine hinges on **B**: on an ingest miss we *grow* the ontology so the
fact has somewhere walkable to live — we do not drop it. **We don't forget**: a
classification failure lands in C and can be promoted later. A/B never promote;
promotion is a C-tier mechanism.

→ Mechanically: [ARCHITECTURE.md → Class tiering](ARCHITECTURE.md#class-tiering-durability--ab-in-postgresql-c-in-qdrant).

---

## 6. The spine deriver — deterministic structured extraction

When the deterministic statement extractor ("spine") is engaged, structured
extraction is done by a **spaCy-dependency engine** — subject-verb-object plus
possessive/genitive/copula chains and a temporal date layer. The LLM is used for
*atomization only* (splitting a rambling statement into single-fact sentences),
never to invent triples. GLiNER2 types entities on the clean sentence. Lexical
decisions (kinship nouns, measurement units, naming verbs) read **bounded,
per-tenant cue classes** from the database — grown, never enumerated in code.

→ Mechanically: [ARCHITECTURE.md → The ingest pipeline](ARCHITECTURE.md#the-ingest-pipeline-strong).

---

## 7. The temporal hinge — deterministic dates, dual clock

Time is an ingest-parsed hinge, determined deterministically — **no LLM
tense-guessing, and GLiNER2 never touches dates.**

- Dates are extracted by spaCy DATE NER + a small regex, then normalized and
  validated by `dateparser` (a rule engine, no embeddings). It **never fabricates a
  date** — a miss is NULL, never today's wall clock.
- **Dual clock:** `event_date` (valid time — *what* a fact refers to) is kept
  orthogonal to `superseded_at`/`archived_at` (belief currency).
- The stored coarse status goes stale, so **query-time temporal reasoning recomputes
  from `event_date` vs now** rather than trusting the stored boundary.

→ Mechanically: [ARCHITECTURE.md → Temporal model](ARCHITECTURE.md#temporal-model-deterministic-dates).

---

## 8. Feelings tiering — by provenance, not a fixed class

Feelings and states are hierarchable like anything else, but their durability is set
by **provenance**:

- A **stated** feeling ("I'm worried") is durable — it grounds to the feeling layer
  and is queryable by structure. It is not forced into a short-term grave.
- An **inferred** feeling is erode-OK — it sits in the short-term tier and only
  promotes if it recurs.

The lesson here is "tier by where it came from, not by a blanket rule" — a stated
feeling should never be forced into a 30-day expiry just because feelings *can* be
ephemeral.

→ Related: [Class tiering](ARCHITECTURE.md#class-tiering-durability--ab-in-postgresql-c-in-qdrant).

---

## 9. Two growth pathways — ±6 natural growth vs. /expand

Do not conflate these:

- **Engine / natural growth (the ±6).** Every ingest auto-grows a bounded
  hierarchical "semblance" — ±6 up/down around the entity — so it is connected and
  walkable. An instant synchronous attach plus an async build-out that never blocks
  recall. **The ±6 is an *ingest* build-out, not a query bound.**
- **`/expand` (learn).** A separate, explicit mechanism that learns a subject's
  fuller depth (optionally web-grounded) with its own anti-sprawl bounding — bound to
  the subject plus a little outward, deliberately not mapping the whole world to
  disk.

Both are subject-agnostic and grow **per-tenant**: `public` is a read-only template
you seed *from*, never *into*.

→ Mechanically: [ARCHITECTURE.md → Self-building ontology](ARCHITECTURE.md#self-building-ontology-growth)
and the [`/expand` section in the README](../README.md#expand--on-demand-domain-intelligence).

---

## 10. Subject-agnostic & per-tenant

FaultLine has no idea whether the domain is networking, law, biology, or anything
else — the domain is just data grown into L4. The one language "hook" is that
**"I"/"me" = the speaking user**; every other entity is scoped, walked, and returned
the same way. Each tenant lives in its own PostgreSQL schema with `search_path`
bound *without* `public`, so one user's world can never leak into another's.

→ Mechanically: [ARCHITECTURE.md → Per-tenant isolation](ARCHITECTURE.md#per-tenant-isolation).

---

## How the concepts hinge together

```
            THE HARD LINE  (memory vs. place)
                    │
            L4 = the place  ──── built AT INGEST ───┐
                    │                               │
   strong ingest ──┤                                ├── deterministic WALK (lean query)
                    │                                │        │
            WGM gate (no unsupervised writes)        │        └── fail loud if absent
                    │                                │
        A/B (Postgres, authoritative)  ◀── promote ──┴── C (Qdrant, short-term)
                    │
   temporal hinge · feelings tiering · ±6 growth · /expand · per-tenant
        (all hinge on L4 being built right at ingest)
```

If recall is ever wrong, the answer is almost always: **L4 wasn't built right at
ingest.** Fix it there.
