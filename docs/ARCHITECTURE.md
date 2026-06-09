# FaultLine Architecture

FaultLine is a **write-validated, self-building knowledge graph** that intercepts OpenWebUI conversations, extracts entities and relationships, validates them against a living ontology, and persists them to PostgreSQL. Qdrant serves as a derived vector index — facts flow PostgreSQL → Qdrant via the background re-embedder for semantic recall during conversation.

## System Overview

```
OpenWebUI Filter (dumb, stateless)
    │
    ├─▶ Inlet: Extract facts → POST /ingest (fire-and-forget)
    │         └─▶ Detect retractions first (short-circuit)
    │
    ├─▶ POST /query (synchronous, before LLM sees message)
    │         ├─▶ PostgreSQL: baseline facts, graph traversal, hierarchy expansion
    │         ├─▶ Qdrant: vector similarity search (cosine, score ≥ 0.3)
    │         ├─▶ Attributes: entity_attributes → fact dicts
    │         ├─▶ Dedup: UUID-based (prevents alias-duplicate hallucination)
    │         └─▶ Inject ranked facts as system context
    │
    └─▶ Outlet: pass-through (no-op)
```

**Dumb filter, smart backend.** The Filter trusts the backend's ranking and injects facts in returned order. No client-side relevance scoring. The backend owns all graph-proximity ranking, taxonomy-aware filtering, and sensitivity gating.

## Three-Dimensional Fact Classification

Every fact is classified along **three orthogonal axes** at ingest time. This is the architectural backbone — strong ingest with deterministic routing justifies a dumb extraction layer.

### Dimension 1: Storage Path (WHERE)

Determined by `rel_types.tail_types` metadata:

| Path | Table | Object type | Example rel_types |
|------|-------|-------------|-------------------|
| **SCALAR** | `entity_attributes` | String value | age, height, weight, pref_name, also_known_as, occupation |
| **RELATIONAL** | `facts` | UUID identity | spouse, parent_of, child_of, works_for, has_pet, knows |
| **HIERARCHICAL** | `facts` | UUID identity | instance_of, subclass_of, part_of, member_of |

Scalar values are NEVER stored in `facts`. Relational and hierarchical facts share the `facts` table but are traversed by separate systems (graph vs hierarchy).

### Dimension 2: Confidence Class (WHO + ontology completeness)

Determined by source authority and whether metadata had to be created in-flow:

| Class | Confidence | Source | Lifecycle |
|-------|-----------|--------|-----------|
| **A — Identity/Structural** | 1.0 (user) / 0.8 (LLM) | User-stated or direct extraction | Commit immediately to `facts` |
| **B — Behavioral/Contextual** | 0.8 (user) / 0.6 (LLM) | LLM following established ontology | Stage → promote at 3 confirmations |
| **C — Novel/Ephemeral** | 0.4 | Unknown rel_types, low confidence | Stage → expire after 30 days |

**User corrections are always Class A** regardless of rel_type. User authority overrides everything.

Confidence penalties apply when the system must create ontology metadata in-flow: -0.2 per piece (ontology + hierarchy creation).

### Dimension 3: Directionality (HOW)

Determined by `rel_types.is_symmetric` + `inverse_rel_type` metadata:

| Type | Behavior | Examples |
|------|----------|----------|
| **Symmetric** | One row implies both directions | spouse, sibling_of, friend_of, knows, met |
| **Asymmetric** | Single direction; inverse auto-enforced | parent_of ↔ child_of, works_for |
| **Hierarchical** | Composition/classification chains | instance_of, subclass_of, part_of |

**Example — "My son is 12":**

| Dimension | Value | Determination |
|-----------|-------|---------------|
| Storage | SCALAR | rel_type="age" → tail_types={SCALAR} → entity_attributes |
| Class | A (1.0) | Source = user-stated |
| Direction | N/A | Scalar facts have no directionality |

Result: `entity_attributes(user_id, child_uuid, age, "12")` — never touches `facts` table.

**Example — LLM infers "child works for Acme" (Acme type doesn't exist):**

| Dimension | Value | Determination |
|-----------|-------|---------------|
| Storage | RELATIONAL | rel_type="works_for" → facts table |
| Class | B (0.6) | LLM-inferred + hierarchy created in-flow (-0.2) |
| Direction | Asymmetric | Single direction, no inverse |

Result: `staged_facts(...)` with `confirmed_count=1`, promotes to `facts` at 3 confirmations.

## Data Pipeline

### Ingest Pipeline

```
LLM extract → pattern-first extraction → WGM gate
    → semantic conflict detection → bidirectional validation
    → 3D classification → Class A/B/C → commit
```

1. **Extraction**: GLiNER2 pre-flight entity typing → LLM triple rewrite → typed edges
2. **Pattern-first**: Regex extraction catches common patterns before LLM call (reduces latency)
3. **WGM Gate**: Validates against `rel_types` ontology + type constraints + category
4. **Semantic Conflict Detection**: Auto-supersedes ownership facts on type entities
5. **Bidirectional Validation**: Prevents contradictory directional pairs (child_of + parent_of)
6. **3D Classification**: Assigns storage path, class, and directionality
7. **Commit**: Class A → `facts` immediately; Class B/C → `staged_facts`

All validation is **metadata-driven** via `rel_types` table columns. No hardcoded rules.

### Query Pipeline

```
baseline facts → graph traversal → hierarchy expansion
    → Qdrant search → attributes → UUID dedup → aliases → inject
```

1. **Baseline**: All identity-anchored facts for the user from `facts` UNION `staged_facts`
2. **Graph Traversal**: Single-hop connectivity (who am I connected to?)
3. **Hierarchy Expansion**: Classification chains upward (what am I?)
4. **Vector Search**: Qdrant cosine similarity, 10 results, score ≥ 0.3
5. **Attributes**: Entity attributes converted to fact dicts
6. **Dedup**: Grouped by `(subject_uuid, rel_type, object_uuid)`, highest confidence kept
7. **Aliases**: `_aliases` metadata attached (all known names, `is_preferred` flag)
8. **Injection**: Facts formatted as plain English system message

### Retraction Pipeline

```
User message → retraction signals detected ("forget", "wrong", "delete", etc.)
    → LLM extracts {subject, rel_type, old_value}
    → POST /retract
        ├─▶ hard_delete: DELETE from facts + entity_aliases (pref_name/also_known_as)
        ├─▶ supersede: superseded_at = now() (soft-delete, audit trail preserved)
        └─▶ immutable: no-op (facts like born_on)
    → Confirmation system message injected
    → Inlet returns early (no ingest/query for retraction messages)
```

## Traversal Systems

Two orthogonal systems run in parallel during query. Results merged and deduplicated.

### Graph Traversal (`_REL_TYPE_GRAPH`)

**Semantics: Connectivity** — who am I connected to?

Rel_types: spouse, parent_of, child_of, sibling_of, has_pet, knows, friend_of, met, works_for, lives_at, lives_in, located_in, owns, educated_at, member_of

Max 1 hop. Fetches from both `facts` and `staged_facts`.

### Hierarchy Traversal (`_REL_TYPE_HIERARCHY`)

**Semantics: Classification** — what am I? what do I belong to?

Rel_types: instance_of, subclass_of, part_of, is_a, member_of

Bidirectional with CTE recursion:
- `direction="up"`: entity → class chain (pet → instance_of → dog → subclass_of → animal)
- `direction="down"`: class → members

Cycle protection via depth tracking. Max depth 3.

Note: `member_of` appears in both systems — it carries both connectivity and classification semantics.

## Self-Building Ontology

The system continuously strengthens itself through three feedback loops:

### What Grows

| Entity | Table | Trigger | Growth mechanism |
|--------|-------|---------|-----------------|
| **rel_types** | rel_types | occurrence ≥ 3 | re_embedder approves novel types |
| **entity_types** | entities | GLiNER2 + hierarchy | Progressively refined from 'unknown' |
| **entity_aliases** | entity_aliases | Every mention/correction | Names accumulate, preferred status shifts |
| **entity_taxonomies** | entity_taxonomies | Novel rel_types need grouping | LLM discovers taxonomy patterns |
| **correction_patterns** | correction_patterns | User correction behavior | LLM identifies semantic patterns |
| **ontology_evaluations** | ontology_evaluations | First occurrence | Frequency counter → approval gate |

### Strengthening Cycle

```
Novel rel_type detected (e.g., "manages")
    ↓
Class C, staged with 30-day expiry
    ↓
ontology_evaluations: occurrence_count += 1
    ↓ (occurrence_count >= 3)
re_embedder LLM evaluates:
    ├─▶ confidence ≥ 0.7 → INSERT INTO rel_types (engine_generated=true)
    ├─▶ cosine > 0.85 with existing → map to existing type
    └─▶ reject → DELETE from evaluations
    ↓
_refresh_rel_type_cache() → immediately available
↓
Future occurrences get Class B (0.8) — system has learned the pattern
```

Rate-limited by occurrence thresholds and confidence gates. Prevents single-mention noise.

## Entity Architecture

### ID vs Display Name

**Critical separation — violations cause entity loss:**

- **Entity UUIDs**: UUID v5 surrogates stored in `facts.subject_id`, `facts.object_id`, `entities.id`. Never display names.
- **Display Names**: Human-readable strings stored in `entity_aliases.alias` (lowercased). Never in `*_id` columns.
- **Scalar values**: Strings stored in `entity_attributes.value_*`. Never resolved to UUIDs.

### Name Resolution

`entity_aliases` is the authoritative alias registry:
- Multiple aliases per entity
- One `is_preferred=true` per entity
- ON CONFLICT preserves existing `is_preferred=true` (never downgrades)
- Name collisions detected and queued for LLM-powered resolution
- `entity_name_conflicts` table tracks disputes non-destructively

### Entity Type Propagation

Three-layer inference, persisted to `entities.entity_type`:
1. **GLiNER2**: Named entity recognition at extraction time
2. **Relationship semantics**: Type implied by rel_type (works_for → Person subject)
3. **Hierarchy inference**: instance_of facts propagate object's type to subject

Type only updated when current `entity_type = 'unknown'`. Known types: Person, Organization, Location, Animal, Concept, Object, Event.

## Validation Architecture

All validation is **metadata-driven** via `rel_types` table:

| Column | Purpose |
|--------|---------|
| `is_symmetric` | Bidirectional (spouse) vs directed (parent_of) |
| `inverse_rel_type` | Opposite relationship (parent_of ↔ child_of) |
| `is_leaf_only` | Object can't have further relationships |
| `is_hierarchy_rel` | Classification vs connectivity routing |
| `head_types` / `tail_types` | Entity type constraints (Person, SCALAR, ANY) |
| `correction_behavior` | hard_delete / supersede / immutable |

**Zero hardcoded validation constants.** New rel_types created by the engine self-describe their constraints via these columns. Adding a new relationship type requires zero code changes.

## Deduplication Strategy

UUID-based, not name-based:

1. All entity IDs stored as UUIDs (`_subject_id`, `_object_id`)
2. Display names stored separately in `entity_aliases`
3. `pg_keys` uses UUIDs for identity, not display names
4. Final dedup groups by `(subject_uuid, rel_type, object_uuid)`, keeps highest confidence
5. PostgreSQL wins on conflict with Qdrant

**Result**: Same entity with multiple aliases = one deduplicated fact. Prevents hallucination from alias variation.

## Filter Architecture

The OpenWebUI Filter is intentionally **dumb and stateless**:

### Inlet (before LLM sees message)

1. **Retraction check** (first): If text signals retraction → extract + POST /retract → short-circuit
2. **Ingest gate**: Word count ≥ 3 OR self-identification pattern → POST /extract + /ingest
3. **Query**: ALWAYS calls POST /query (synchronous, before model responds)
4. **Injection**: Ranks facts by backend-provided order, formats as plain English system message

### Relevance Gating

- **Identity facts always pass**: also_known_as, pref_name, same_as, spouse, parent_of, child_of, sibling_of
- **Everything else**: passes if `confidence ≥ 0.4` (tunable via `MIN_INJECT_CONFIDENCE`)
- **Sensitivity penalty**: PII facts (born_on, lives_at, lives_in, height, weight, born_in) gated unless explicitly asked
- **No tier gating, no keyword re-scoring** — backend ranking is authoritative

### Injection Format

Facts are injected as **plain English prose**, never machine-readable tuples. The LLM receives:
- "You are the assistant. The user is 'Alice'."
- "Your spouse is Taylor."
- "Alex is your child, age 14."

NOT: `("uuid-xxx", "spouse", "uuid-yyy")` — smaller LLMs cannot parse structured tuples in context.

### Filter Trusts Backend

**Backend graph-proximity is authoritative for relevance.** Filter doesn't re-rank, re-score, or second-guess. Backend ranks by:
1. Graph proximity (direct connections first)
2. Hierarchy relevance (taxonomy matches)
3. Confidence (Class A > B > C)
4. Vector similarity (associative context)

Filter injects in returned order. No keyword-based re-scoring.

## LLM Endpoint Resolution

Centralized auto-detection with fallback chain:

**Priority chain (same everywhere — backend, filter, re-embedder):**
1. Auto-detect OpenWebUI (Docker service name → localhost → explicit env)
2. Environment variable (QWEN_API_URL or OPENWEBUI_URL if set)
3. Hardcoded fallback (localhost only, absolute last resort)

No scattered endpoint resolution logic. All modules resolve the same way.

## Re-embedder Background Loop

Runs continuously, polls database every `REEMBED_INTERVAL` seconds (default **60s**):

| Task | Query | Action |
|------|-------|--------|
| **Promotion** | Class B, `confirmed_count >= 3`, not yet promoted | INSERT into `facts`, mark promoted |
| **Expiry** | Class C, `expires_at <= now()` | DELETE from `staged_facts` |
| **Embedding** | Unsynced facts/staged_facts | Embed + upsert to Qdrant |
| **Ontology eval** | `ontology_evaluations` with `occurrence >= 3` | LLM approve/map/reject |
| **Name conflicts** | `entity_name_conflicts` status=pending | LLM-powered disambiguation |

Post-promotion, staged Qdrant point is deleted (best-effort, outside transaction). New facts point upserted next poll cycle.

## Qdrant Collection Naming

Per-user isolation:
- `"anonymous"`, `""`, or `"legacy"` → env `QDRANT_COLLECTION` (default `"faultline-test"`)
- Any other user_id → `"faultline-{user_id}"`

Ensures multi-tenant deployments remain isolated. The Filter passes the OpenWebUI user UUID as `user_id`.

## Database Schema

### Core Tables

| Table | Purpose | Key constraint |
|-------|---------|----------------|
| `facts` | Immutable, validated facts | UNIQUE `(user_id, subject_id, object_id, rel_type)` |
| `staged_facts` | Unconfirmed/ephemeral facts | Auto-promote (Class B) or expire (Class C) |
| `entity_attributes` | Scalar values (age, names, occupation) | UNIQUE `(user_id, entity_id, attribute)` |
| `entities` | Canonical entity registry (UUIDs) | One row per unique entity |
| `entity_aliases` | Authoritative name mapping | UNIQUE `(user_id, alias)` |
| `rel_types` | Live ontology + validation metadata | Engine self-populates |
| `entity_taxonomies` | Semantic groupings (family, work, etc.) | Data-driven query filtering |
| `entity_name_conflicts` | Collision detection + resolution | UNIQUE `(user_id, alias)` |
| `ontology_evaluations` | Novel type frequency tracking | occurrence_count → approval gate |

Key principles:
- **Soft-delete**: `superseded_at IS NOT NULL` (never hard-delete facts)
- **NO RECURSIVE MATCHING**: All string comparisons use pre-lowercased values
- **ON CONFLICT matches actual unique constraints**: `entity_aliases` uses `UNIQUE (user_id, alias)`
- **Aggregation queries**: `entity_attributes` uses `ON CONFLICT (user_id, entity_id, attribute)` — values overwrite, never accumulate

## Archive Model

User corrections are authoritative and non-destructive:

- Conflicting facts archived at write-time in WGM gate, not filtered at query-time
- `archived_at TIMESTAMP` column on `facts`
- `/query` filters `archived_at IS NULL` by default (current state)
- User can ask historical questions: "Where did I used to live?" (queries `archived_at IS NOT NULL`)
- Supersession rule: new type/ownership contradicting facts → old facts archived

**Why**: Corrections respected at ingest time. Database contains authoritative current state — no contradictions to filter. Historical context preserved.

## Production Hardening

**Startup**: Validates POSTGRES_DSN, QDRANT_URL (non-fatal warning if missing)

**Health**: `/health` returns JSON with database, qdrant, llm, re-embedder status (5s cache)

**Timeouts**: HTTP 10s, DB 30s, Qdrant 10s (all tunable)

**Rate limiting**: Per-user_id tracking, 100 req/min default

**Query fallback**: PostgreSQL-only response when embedding/Qdrant unavailable

## Key Design Principles

- **LLM has no unsupervised write access** — all writes flow through WGM validation gate
- **PostgreSQL is authoritative** — Qdrant is a read-only derived view
- **Validation is metadata-driven** — `rel_types` table is the single source of truth
- **Strong ingest, dumb extract** — extraction produces triples without validation; ingest enforces all rules
- **Three-dimensional classification** — storage path, confidence class, and directionality are orthogonal
- **UUID-based deduplication** — prevents hallucination from alias variation
- **Non-destructive corrections** — user retractions soft-delete via `superseded_at`, never hard-delete
- **Self-healing graph** — semantic conflicts auto-superseded, no manual intervention
- **Self-building ontology** — new rel_types learned from usage patterns, no code changes needed
- **Entity IDs are UUIDs, display names are strings** — never conflate the two

## Running

```bash
# Develop
pip install -e ".[test]"
uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload

# Test
pytest tests/ --ignore=tests/evaluation --ignore=tests/feature_extraction \
              --ignore=tests/model_inference --ignore=tests/preprocessing

# Deploy
docker compose up --build
```

## Environment Variables

```
# Database
POSTGRES_DSN=postgresql://user:pass@localhost:5432/faultline
POSTGRES_USER=faultline
POSTGRES_PASSWORD=faultline
POSTGRES_DB=faultline

# LLM (extraction, validation, category assignment)
QWEN_API_URL=http://localhost:11434/v1/chat/completions
WGM_LLM_MODEL=qwen/qwen3.5-9b
CATEGORY_LLM_MODEL=qwen2.5-coder

# Vector search
QDRANT_URL=http://qdrant:6333
QDRANT_COLLECTION=faultline-test

# Background re-embedder
REEMBED_INTERVAL=60

# Performance
HTTPX_TIMEOUT=10
DB_TIMEOUT=30
QDRANT_TIMEOUT=10
DB_POOL_SIZE=10
RATE_LIMIT_PER_MIN=100

# API (used by OpenWebUI Filter)
FAULTLINE_API_URL=http://localhost:8000
```
