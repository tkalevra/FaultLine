# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What FaultLine Is

FaultLine is a **write-validated knowledge graph** pipeline that intercepts OpenWebUI conversations, extracts named entities and relationships, validates them against an ontology, and persists them to PostgreSQL. Qdrant is a derived vector index — facts flow Postgres → Qdrant via the re-embedder and are queried for memory recall during the inlet phase.

## Pipeline Flow
OpenWebUI inlet filter
├─▶ Retraction detection (if "forget", "delete", "wrong", etc.)
│     └─▶ LLM retraction extraction → POST /retract (inline Qdrant cleanup)
│           └─▶ entity_aliases cleanup (pref_name hard-delete only)
│                 └─▶ confirmation system message → short-circuit (skip ingest/query)
│
├─▶ POST /extract (preflight)   GLiNER2 entity typing (subject/object type context)
├─▶ LLM triple rewrite   entity-typed structured edge extraction from text
│     ├─▶ POST /ingest (fire-and-forget) [typed edges]
│     │     └─▶ GLiNER2 extract_json   typed schema edge extraction (fallback/override)
│     │           └─▶ WGMValidationGate   ontology + conflict check → status
│     │                 └─▶ Fact Classification (Phase 4)
│     │                       ├─▶ Class A (identity/structural)
│     │                       │     └─▶ FactStoreManager.commit()  INSERT INTO facts immediately
│     │                       │           └─▶ re_embedder (background) → Qdrant upsert
│     │                       ├─▶ Class B (behavioral/contextual)
│     │                       │     └─▶ _commit_staged()  INSERT INTO staged_facts
│     │                       │           └─▶ immediate Qdrant sync (same as Class A, no poll delay)
│     │                       │           └─▶ re_embedder promotes when confirmed_count >= 3
│     │                       │                 └─▶ staged Qdrant point deleted after commit
│     │                       │                       └─▶ new facts point upserted next poll cycle
│     │                       └─▶ Class C (ephemeral/novel)
│     │                             └─▶ _commit_staged()  INSERT INTO staged_facts
│     │                                   └─▶ re_embedder upserts to Qdrant
│     │                                         └─▶ expires after 30 days if unconfirmed
│     └─▶ POST /store_context (fire-and-forget) [no typed edges]
│           └─▶ Embed text (nomic-embed-text) → direct Qdrant upsert
│                 └─▶ fact_class=C, confidence=0.4, rel_type="context"
│                       └─▶ no WGM gate, no Postgres write, Qdrant only
│
└─▶ POST /query (synchronous, before model sees message)
├─▶ PostgreSQL baseline facts (always returned for known identity)
├─▶ PostgreSQL graph traversal (self-referential signals, 2-hop)
└─▶ Qdrant cosine search (nomic-embed-text, score_threshold: 0.3)
└─▶ calculate_relevance_score() gate (threshold 0.4)
└─▶ entity attributes scored separately before injection
└─▶ merged, deduplicated → injected as system message before last user message
└─▶ ⊢ FaultLine Memory header + event_emitter status notification
OpenWebUI outlet filter
└─▶ pass-through (no-op)

## Inlet Short-Circuit & Retraction

**Retraction check (first):**
If text contains retraction signals ("forget", "delete", "wrong", "no longer", etc.), the LLM extracts `{subject, rel_type?, old_value?}` and POSTs to `/retract`. If successful:
- `hard_delete` behavior (pref_name, also_known_as): DELETE from facts + DELETE from entity_aliases where is_preferred=true
- `supersede` behavior: superseded_at = now(), qdrant_synced = false
- `immutable` behavior: no-op, user rejection
A confirmation system message is injected and inlet returns early — no ingest or query happens.

**Ingest gate:**
Before calling the LLM for fact extraction:
1. Word count ≥ 3, OR message matches a self-identification pattern (`my name is`, `I am`, `call me`, etc.)

If neither condition is met, `will_ingest = False`. `will_query` is always `True` when `QUERY_ENABLED` is set.

**Memory injection gate:**
Facts are scored via `calculate_relevance_score()` before injection. Facts scoring below 0.4 are excluded. Entity attributes are scored separately using synthetic fact dicts before reaching `_build_memory_block()`. Identity relationships (`also_known_as`, `pref_name`, `same_as`) always pass regardless of score. Memory is injected only when facts survive the gate, positioned immediately before the last user message. A visible status notification is emitted via `__event_emitter__` showing fact count.

## Relevance Scoring

`calculate_relevance_score(fact, query) -> float [0.0, 1.0]`

Three components:

1. **Query signal match (0.0–0.6):** keyword overlap between lowercased query and `_CAT_SIGNALS[fact.category]`, capped at 0.6
2. **Confidence bonus (0.0–0.3):** `fact.confidence * 0.3`
3. **Sensitivity penalty (-0.5):** applied when `fact.rel_type` is in `_SENSITIVE_RELS` (`born_on`, `lives_at`, `lives_in`, `height`, `weight`, `born_in`) and no explicit request term found in query

`_SENSITIVE_TERMS`: `{"born", "birth", "live", "address", "height", "weight", "birthplace", "tall", "how tall", "heavy", "how heavy"}`

Identity rels (`also_known_as`, `pref_name`, `same_as`) always bypass scoring.

Threshold: `RELEVANCE_THRESHOLD = 0.4` (local constant in `_filter_relevant_facts()`).

**Critical:** `_filter_relevant_facts()` returns `scored` (never falls back to `cleaned`). When no facts score above threshold, nothing injects. The previous fallback leak (`return scored if scored else cleaned`) has been removed.

**Entity attributes:** Filtered via synthetic fact dicts with `_ATTR_CATEGORY_MAP` before reaching `_build_memory_block()`. Not injected unconditionally.

Conversation state awareness is planned as a future score contributor (0.0–0.4) — see NEXT_STEPS.md.

## Fact Classification (Phase 4)

Facts are classified at ingest time into three classes:

**Class A — Identity/Structural** (write-through to PostgreSQL immediately)
- pref_name, also_known_as, same_as, parent_of, child_of, spouse, sibling_of, born_on, born_in, has_gender, nationality, instance_of, subclass_of
- **Confidence**: 1.0 if user-stated or correction, 0.8 if llm_inferred
- **Lifecycle**: Committed immediately to `facts` table
- **User corrections always Class A** regardless of rel_type
- **Qdrant**: Synced by re_embedder after insertion

**Class B — Behavioral/Contextual** (staged, promoted on confirmation)
- lives_at, lives_in, works_for, occupation, educated_at, owns, likes, dislikes, prefers, friend_of, knows, met, located_in, related_to, has_pet, part_of, created_by
- **Confidence**: 0.8 if user_stated, 0.6 if llm_inferred
- **Lifecycle**: Staged to `staged_facts`; immediate Qdrant sync at ingest time (no poll delay); promoted when `confirmed_count >= 3`; staged Qdrant point deleted after promotion commits (best-effort, outside transaction); new facts point upserted next poll cycle
- **TTL**: Promoted facts persist indefinitely
- **Query visibility**: Immediately visible via `_fetch_user_facts()` UNION (PostgreSQL) and immediate Qdrant sync (vector search) — no 3-confirmation wait for retrieval

**Class C — Ephemeral/Novel** (staged, expiring without confirmation)
- Anything not in A or B; engine-generated types; confidence < 0.6; novel types rejected by LLM
- **Confidence**: 0.4 if llm_inferred
- **Lifecycle**: Staged with `expires_at = now() + 30 days`; no promotion path; deleted by re_embedder on expiry

## Query / Retrieval Path & Filtering

`/query` runs three parallel sources:
1. **Baseline facts** (PostgreSQL, always) — identity-anchored scalar and relationship facts
2. **Graph traversal** (PostgreSQL, signal-gated) — self-referential signals, 2-hop
3. **Vector similarity** (Qdrant) — `nomic-embed-text-v1.5`, cosine, `score_threshold: 0.3`, `limit: 10`

Merged and deduplicated on `(subject, object, rel_type)` with PostgreSQL winning on conflict.

**`_fetch_user_facts()` UNION helper:** Both the baseline and graph-traversal queries use `_fetch_user_facts(db, user_id, entity_id, rel_types)` which UNIONs the `facts` and `staged_facts` tables. This ensures Class B/C staged facts are immediately visible to all PostgreSQL query paths without waiting for the 3-confirmation promotion cycle. The function is defined at the top of `/query` (before the `try` block) and must remain there — call sites must follow the definition.

**Signal-gating:**
- `_SELF_REF_SIGNALS`: triggers full graph traversal (2-hop) for queries like "where do i live", "tell me about me", "my family"
- `_ATTRIBUTE_SIGNALS`: triggers named-entity resolution for queries containing `live`, `address`, `home`, `location`, `age`, `height`, `weight`, `job`, `work`, `occupation`, `born`, `birthday`

**Scalar facts as facts:** Entity attributes returned in both `attributes` dict AND `facts` list. Both paths are now scored before injection.

## LLM Configuration

### Filter (`openwebui/faultline_tool.py`)

Model resolution via `_resolve_llm_config(valves, body)`:
- `LLM_MODEL` valve non-empty → use that model (explicit override)
- `LLM_MODEL` valve empty → passthrough `body.get("model")` (user's selected OpenWebUI model)
- `LLM_URL` valve non-empty → use that endpoint
- `LLM_URL` valve empty → use OpenWebUI's internal endpoint

Eliminates cold-load penalties in LM Studio when the user's selected model is already warm.

### Backend (`src/wgm/gate.py`, `src/api/main.py`)

All backend LLM model strings are env-var controlled:

| Env Var | Default | Purpose |
|---|---|---|
| `WGM_LLM_MODEL` | `qwen/qwen3.5-9b` | Novel type validation in `WGMValidationGate._try_approve_novel_type()` |
| `CATEGORY_LLM_MODEL` | `qwen2.5-coder` | Category inference in `_assign_category_via_llm()` |
| `QWEN_API_URL` | `http://localhost:11434/v1/chat/completions` | LLM endpoint for all backend calls |

Embedding model (`text-embedding-nomic-embed-text-v1.5`) remains hardcoded — infrastructure, not user-configurable.

## Key Files

| File | Role |
|---|---|
| `src/api/main.py` | FastAPI app — `/ingest`, `/query`, `/retract`, `/store_context` endpoints, GLiNER2 lifecycle, fact classification, Qdrant cleanup, `_fetch_user_facts()` UNION helper, `_assign_category_via_llm()` (model: `CATEGORY_LLM_MODEL`) |
| `src/api/models.py` | Pydantic models — IngestRequest, QueryRequest, RetractRequest, RetractResponse, StoreContextRequest, StoreContextResponse |
| `src/wgm/gate.py` | `WGMValidationGate` — ontology check + conflict detection + type constraint validation + novel type approval (model: `WGM_LLM_MODEL`) |
| `src/fact_store/store.py` | `FactStoreManager` — `commit()` for ingest, `retract()` for user-driven fact removal |
| `src/schema_oracle/oracle.py` | `resolve_entities()`, `LABEL_MAP`, `GLIREL_LABELS` — entity resolution helpers |
| `src/entity_registry/registry.py` | DB-backed `EntityRegistry` — canonical ID assignment, alias tracking, preferred name resolution |
| `src/re_embedder/embedder.py` | Background poll loop — embeds unsynced facts/staged_facts, promotes Class B (deletes staged Qdrant point post-promotion), expires Class C, runs reconciliation pass |
| `openwebui/faultline_tool.py` | OpenWebUI **Filter** — retraction detection + LLM extraction, ingest/unstructured fallback, relevance-scored query injection, `⊢ FaultLine Memory` branding, `__event_emitter__` status |
| `openwebui/faultline_function.py` | OpenWebUI **Function** (tool call) — explicit `store_fact()` with LLM rewrite |
| `migrations/001_create_facts.sql` | Schema: `facts` table + `qdrant_synced` column + lowercase trigger |
| `migrations/007_correction_behavior.sql` | Correction behavior enum (supersede/hard_delete/immutable) per rel_type |
| `migrations/012_staged_facts.sql` | Phase 4: `staged_facts` table, promotion/expiration indexes, fact_class + fact_provenance columns |
| `migrations/013_rel_type_category.sql` | Adds `category TEXT` to `rel_types` for query intent matching |
| `migrations/014_entity_attributes_unique.sql` | Unique `(user_id, entity_id, attribute)` on `entity_attributes` |

## GLiNER2 Extraction

Both `/ingest` and `/extract` use `model.extract_json(text, schema)`. The `rel_type` constraint is built dynamically from the `rel_types` DB table at startup via `_build_rel_type_constraint`; comprehensive hardcoded fallback used when DB is unavailable.

`/ingest` schema — 3 fields:
```python
{
    "facts": [
        "subject::str::The full proper name of the first entity. Never a pronoun.",
        "object::str::The full proper name of the second entity. Never a pronoun.",
        "rel_type::[<db-loaded constraint>]::str::The relationship type from subject to object.",
    ]
}
```

`/extract` (preflight) schema — 5 fields:
```python
{
    "facts": [
        "subject::str::...", "object::str::...", "rel_type::[...]::str::...",
        "subject_type::[Person|Animal|Organization|Location|Object|Concept]::str::...",
        "object_type::[Person|Animal|Organization|Location|Object|Concept]::str::...",
    ]
}
```
The bracket syntax (`[a|b|c]`) is native GLiNER2 choices constraint — do not change it.

**Edge validation:** UUID values in subject or object rejected before entity resolution.

**Type persistence:** `subject_type`/`object_type` persisted to `entities` table only when current `entity_type = 'unknown'`.

When `req.edges` are supplied (from LLM rewrite), they override GLiNER2 inferred edges.

## Qdrant Collection Naming

`re_embedder` derives collection names via `derive_collection(user_id)`:
- `"anonymous"`, `""`, or `"legacy"` → env `QDRANT_COLLECTION` (default `"faultline-test"`)
- Any other user_id → `"faultline-{user_id}"`

Both Filter and Function pass the OpenWebUI user UUID as `user_id`.

## Re-embedder Promotion & Expiry

**Promotion (`promote_staged_facts`):**
1. Query: `fact_class='B' AND confirmed_count >= 3 AND promoted_at IS NULL AND expires_at > now()`
2. Per-row: INSERT into facts (ON CONFLICT increments confirmed_count) → UPDATE promoted_at → commit
3. Post-commit (outside transaction, best-effort): DELETE staged Qdrant point
4. Next poll cycle: upsert new facts-table point to Qdrant

**Known reconciliation gap:** `reconcile_qdrant()` only queries `facts` table. Expired `staged_facts` rows surviving a failed Qdrant delete are invisible to reconciliation until next successful expiry run. No fix needed for single-instance deployment.

## WGM Ontology

Triple model `(subject_id, rel_type, object_id)` aligned to Wikidata PIDs. SKOS/OWL semantics where applicable.

**Symmetric:** spouse, sibling_of, same_as, friend_of, knows, met

**Inverse:** parent_of ↔ child_of

Novel `rel_type` values → LLM approval call (model: `WGM_LLM_MODEL`). Confidence ≥ 0.7 → auto-approve. Lower → `pending_types`, edge dropped.

**Type constraints:** `rel_types.head_types` and `tail_types` (ARRAY). `ARRAY['ANY']` = unconstrained. `ARRAY['SCALAR']` = scalar value.

| rel_type | Wikidata PID | Inverse | Symmetric | W3C Mapping | Notes |
|---|---|---|---|---|---|
| is_a | P31/P279 | — | No | rdf:type (dep.) | **deprecated** |
| instance_of | P31 | — | No | rdf:type | NOT transitive |
| subclass_of | P279 | — | No | rdfs:subClassOf | IS transitive |
| part_of | P361 | — | No | — | component → whole |
| created_by | P170 (inv) | — | No | — | creation → creator |
| works_for | P108 (inv) | — | No | — | employee → employer |
| parent_of | P40 | child_of | No | — | parent → child |
| child_of | P40 (inv) | parent_of | No | — | child → parent |
| spouse | P26 | spouse | Yes | — | symmetric |
| sibling_of | P3373 | sibling_of | Yes | — | symmetric |
| also_known_as | P742/P1449 | — | No | skos:altLabel | entity → alias |
| pref_name | — | — | No | skos:prefLabel | entity → preferred name |
| same_as | Q39893449 | same_as | Yes | owl:sameAs | identity, symmetric |
| related_to | P1659 | — | No | skos:related | loose link |
| likes | — | — | No | — | subject preference |
| dislikes | — | — | No | — | subject preference |
| prefers | — | — | No | — | subject preference |
| owns | P1830 (inv) | — | No | — | owner → property |
| located_in | P131 | — | No | — | entity → location |
| educated_at | P69 | — | No | — | student → institution |
| nationality | P27 | — | No | — | person → country |
| occupation | P106 | — | No | — | person → profession |
| born_on | P569 | — | No | — | person → date |
| age | — | — | No | — | person → value |
| knows | P1891 | knows | Yes | — | symmetric |
| friend_of | — | friend_of | Yes | — | symmetric |
| met | — | met | Yes | — | symmetric |
| lives_in | P551 | — | No | — | person → location (residence) |
| born_in | P19 | — | No | — | person → location (birthplace) |
| has_gender | P21 | — | No | — | person → gender |

## Database Schema & Fact Lifecycle

Primary tables:
- `facts(id, user_id, subject_id, object_id, rel_type, provenance, fact_provenance, fact_class, created_at, qdrant_synced, superseded_at, confidence, confirmed_count, last_seen_at, contradicted_by, is_preferred_label)` — unique on `(user_id, subject_id, object_id, rel_type)`. Soft-delete via `superseded_at IS NOT NULL`.
- `staged_facts(id, user_id, subject_id, object_id, rel_type, fact_class, provenance, confidence, confirmed_count, first_seen_at, last_seen_at, expires_at, promoted_at, qdrant_synced)` — Class B promoted when `confirmed_count >= 3`; Class C auto-deleted when `expires_at <= now()`.
- `entity_attributes(user_id, entity_id, attribute, value_text, value_int, value_float, value_date, provenance, sensitivity)` — scalar facts. Unique on `(user_id, entity_id, attribute)`. `entity_id` always normalized to `"user"` anchor for user-identity scalars.
- `entities(id, user_id, entity_type)` + `entity_aliases(entity_id, user_id, alias, is_preferred)` — canonical entity registry. Note: `entity_aliases` rename to `entity_names` planned.
- `rel_types(rel_type, label, wikidata_pid, engine_generated, confidence, source, correction_behavior, category, head_types, tail_types)` — live ontology. Loaded at startup into `_REL_TYPE_META`.
- `pending_types(id, rel_type, subject_id, object_id, flagged_at)` — novel types awaiting approval.

DB triggers: Lowercase `subject_id`, `object_id`, `rel_type` on every INSERT/UPDATE. **NO RECURSIVE MATCHING** — all string comparisons must use pre-lowercased values only.

## Running / Developing

```bash
pip install -e ".[test]"

pytest tests/ --ignore=tests/evaluation --ignore=tests/feature_extraction \
              --ignore=tests/model_inference --ignore=tests/preprocessing

pytest tests/embedder/test_promotion.py -v
pytest tests/filter/test_relevance.py -v
pytest tests/api/test_retract.py -v

uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload
docker compose up --build
```

## Environment Variables
POSTGRES_DSN=postgresql://user:pass@localhost:5432/faultline
QWEN_API_URL=http://localhost:11434/v1/chat/completions
WGM_LLM_MODEL=qwen/qwen3.5-9b
CATEGORY_LLM_MODEL=qwen2.5-coder
QDRANT_URL=http://qdrant:6333
QDRANT_COLLECTION=faultline-test
REEMBED_INTERVAL=10

## OpenWebUI Integration

Two artifacts in `openwebui/`:

- **`faultline_tool.py`** — OpenWebUI **Filter**. Inlet flow:
  1. **Retraction check**: LLM extracts → `/retract` → entity_aliases cleanup (pref_name) → short-circuit
  2. **LLM config resolution**: `_resolve_llm_config(valves, body)` — model and URL resolved once per inlet call
  3. **Memory query**: `/query` → relevance scored (threshold 0.4) → entity attributes scored separately → injected before last user message → `⊢ FaultLine Memory` header → `__event_emitter__` status
  4. **Ingest flow**: typed edges → `/ingest`; unstructured fallback → `/store_context`
  5. Outlet: no-op pass-through

**Filter valves:**
- `RETRACTION_ENABLED: bool`
- `INGEST_ENABLED: bool`
- `QUERY_ENABLED: bool`
- `MIN_INJECT_CONFIDENCE: float` (default 0.5)
- `LLM_MODEL: str` (empty = passthrough user's model)
- `LLM_URL: str` (empty = OpenWebUI internal endpoint)
- `QWEN_TIMEOUT: int` (default 10)
- `ENABLE_DEBUG: bool`

Defaults: Filter defaults to `"http://192.168.40.10:8001"`; Function defaults to `"http://faultline:8001"`. Internal port 8000; external 8001.

- **`faultline_function.py`** — OpenWebUI **Function/Tool**. Explicit `store_fact(text, __user__)`. LLM rewrites to triples → `/ingest`.

## /query Endpoint: Self-Referential Graph Traversal (Fixed May 2026)

The `/query` endpoint detects self-referential signals ("where do i live", "about me", "my family", etc.) and executes a **PostgreSQL graph traversal** to fetch facts anchored to the user's identity. This returns both long-term facts (from `facts` table) and staged facts (from `staged_facts` table) **immediately** without waiting for promotion.

**Critical initialization (line ~1495):**
```python
try:
    db = psycopg2.connect(os.environ.get("POSTGRES_DSN"))
    registry = EntityRegistry(db)
    canonical_identity = registry.get_preferred_name(user_id, user_id)
except Exception as _e:
    log.warning("query.db_init_failed", error=str(_e))
    db = None
    registry = None
    canonical_identity = None
```

**Why this matters:** Without explicit initialization, `db`, `registry`, and `canonical_identity` remain `None`, causing the graph traversal condition (`if db and registry and canonical_identity:`) to always fail. Result: staged facts (Class B location, contact info, behavioral metadata) never reach the Filter, and user memories fail to inject.

**Tested behavior (May 8, 2026):**
- User tells system: "My home address is 156 Cedar St. S, Kitchener, ON"
- User queries: "Where do I live?"
- `/query` endpoint detects signal → initializes db/registry → calls `_fetch_user_facts()`
- Returns: `lives_at` fact marked `fact_state: "staged"` with TTL/promotion metadata
- Filter injects fact into memory → LLM answers correctly

The `finally` block (line 1921) ensures `db.close()` on all code paths.

## Key Principles (Do Not Violate)

- **LLM never has unsupervised write access** — all writes flow through the WGM validation gate
- **PostgreSQL is authoritative** — Qdrant is a derived read-only view
- **Write-time normalization** — `entity_id` normalized to `"user"` anchor at write time
- **No recursive matching** — all string comparisons use pre-lowercased values; guard comments required
- **`entity_aliases` is the authoritative alias registry**
- **Wren deployment cannot be trusted verbally** — verify edits via `sed` before rebuild
- **`faultline-{user_id}` per-user collection naming is live** — must never be broken
- **Fallback leak is fixed** — `_filter_relevant_facts()` returns `scored` only, never `cleaned`
- **Nested function definitions must precede call sites** — `_fetch_user_facts()` is defined before the `try` block in `/query`; do not move it below its callers
- **ON CONFLICT must match actual unique constraints** — `entity_aliases` uses `UNIQUE (user_id, alias)`, so all ON CONFLICT clauses must target `(user_id, alias)`, never `(entity_id, user_id, alias)`
- **No name-based entity pre-creation** — entities are created exclusively via `EntityRegistry.resolve()` which generates UUID v5 surrogates; the old pre-creation loop that inserted raw names as `id` has been removed
- **Alias sync uses display names, not UUIDs** — `_canonical_to_display` dict maps canonical UUID → original display name; always resolve through this mapping when inserting into `entity_aliases`

## Do Not Develop Here

`FaultLine/` (nested directory) is a shed-tool artifact. Do not edit files inside it.
`tests/evaluation/`, `tests/feature_extraction/`, `tests/model_inference/`, `tests/preprocessing/` — exclude from standard test runs.