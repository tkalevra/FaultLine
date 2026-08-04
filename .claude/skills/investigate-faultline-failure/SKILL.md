---
name: investigate-faultline-failure
description: Diagnose a FaultLine test or pipeline failure by chaining pre-prod logs → DB state → constraint violations → root cause, framed by what FaultLine is actually building, then strengthen the fix with online search. Use when a FaultLine test fails, recall returns wrong/empty data, ingest doesn't commit, or behavior is off and you need the root cause before proposing a fix.
---

# Investigate FaultLine Failure

Root-cause a failure on **pre-prod first** (`ssh truenas "sudo docker ..."`), then ground every finding against the DB, **judged against what FaultLine is actually building** (below), and **strengthen the proposed solution with online search** before handing off. Specific commands, queries, and the per-tenant query model are in `reference.yaml`.

## What we are building (frame every diagnosis with this — source of truth is CLAUDE.md)

FaultLine is a **PostgreSQL-authoritative, subject-agnostic, write-validated knowledge-graph memory**. A failure almost always means an **ingest** layer didn't build structure correctly — fix it at ingest, not query.

- **L4 (the hierarchy) is the founding layer, built AT INGEST** — "the gateway to how we find shit." ±6 is an *ingest* build-out (up/down, avoid islands), NOT a query bound. Query is intentionally **dumb**: parse `(intent, anchor, hierarchical reference ± depth)` and **walk** the L4 ingest already built. Discovery uses the SAME resolver as ingest (no fuzzy).
- **Strong ingest, lean query.** If recall is wrong, the bug is at ingest. Query traverses + presents; it never cleans up / re-validates.
- **Postgres is authoritative (A/B); Qdrant is Class-C-only short-term.** The flip: Postgres = long-term library, vector = short-term scratchpad. The C lane is surfaced first (short-term + promotion gate C→B) but **never overrides Postgres A/B**. A/B are never vector-served. (Writing B to Qdrant = a bug.)
- **Subject-agnostic & growable — never hardcode.** No literal subject/pronoun/rel_type lists in code; lean into the growth engine (miss → grow via `/learn`, never fail). Self-reference ("I"/"me"=user) is detected GRAMMATICALLY, not via a token list. A "fix" that hardcodes a subject/keyword is itself a bug.
- **Ingest hinges (all deterministic, all hinge on L4):** the **negation/correction dual-gate = spaCy ∧ LLM** (GLiNER2 NEVER judges negation), the **temporal determination** layer (spaCy DATE NER + dateparser, no LLM tense-guessing; dual-clock event_date vs belief-currency; query recomputes from event_date), the **growth engine**, and the **correction gate** (user-is-truth).
- **GLiNER2 purity (Pitfall 11):** concise zero-shot labels only; it does entity typing, NOT negation/intent zoos. Call it GLiNER2, never "the LLM".

## Method

1. **Pre-prod logs first** — `ssh truenas "sudo docker logs faultline --since 30m 2>&1 | grep -iE '...'"`. The **backend container is `faultline`** (NOT `faultline-api`). `faultline-mcp` does **NOT** log to stdout — never a verification channel. The OpenWebUI **Filter is disabled** — do not look for it, do not report it as a failure. **Verify firing via explicit MCP status / backend logs / DB — NEVER `sources[]`** (Native mode leaves it empty by design).
2. **Reproduce on a FRESH tenant** via direct MCP (`POST truenas:8002/remember_facts` + `/recall_memory`, `Bearer myownprivateapikey`, header `X-OpenWebUI-User-Id`). The oracle tenant (`10d7d879…`) is polluted — use a throwaway UUID.
3. **DB ground truth — per-tenant.** Tenant tables have **NO `user_id` column**; the boundary is the schema. Always `SET search_path TO faultline_<uuid_with_underscores>;` then query `facts` / `staged_facts` / `entity_attributes` / `entity_aliases` unqualified. (See reference.yaml `diagnostic_queries`.)
4. **Constraint checks** — per-tenant `search_path` (no `public`), seeded metadata present, UUIDs not display names in `*_id`, scalar vs relational routing, Graph vs Hierarchy, A/B-never-in-vector.
5. **Name the single failing link** — distinguish data/seed/config/model from a code bug, mapped to the layer that should have built the structure at ingest.
6. **Strengthen the fix with online search** — before handing off, validate the proposed solution and any library/parser behavior you rely on (spaCy dependency labels, dateparser, NegEx, Postgres, Qdrant, KG-memory practice) with **WebSearch + WebFetch**, and cite sources. A root cause is not done until the fix is corroborated against current external evidence.

## Diagnostic discipline (project rules)

- **Diff our own change first.** "Worked for months, broke today" → diff `last-good..HEAD` on what *we* shipped before blaming model/data/infra.
- **Empty recall on a fresh tenant is correct**, not a bug — confirm schema/data exists first.
- **Don't report the disabled OpenWebUI Filter** as a failure; **never judge firing by `sources[]`**.
- **Fix at INGEST, never bolt cleanup onto query.** A fix that hardcodes a subject/keyword/rel_type, or that adds fuzzy matching for A/B, violates the architecture — reject it.
- Hand the confirmed, online-corroborated root cause to the `propose-faultline-fix` skill.
