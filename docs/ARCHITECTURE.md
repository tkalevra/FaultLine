# FaultLine Architecture

FaultLine is a **write-validated knowledge graph** pipeline that intercepts OpenWebUI conversations, extracts named entities and relationships, validates them against an ontology, and persists them to PostgreSQL. Qdrant serves as a derived vector index — facts flow from PostgreSQL → Qdrant via the background re-embedder for memory recall during the query phase.

## System Overview

```
OpenWebUI Function
    ├─ Extract facts from conversation
    ├─ Call /ingest (async)
    └─ Call /query (sync, before LLM sees message)
        ├─ PostgreSQL: baseline facts, graph traversal, hierarchy expansion
        ├─ Qdrant: vector similarity search
        └─ Inject ranked facts as system context
```

## Memory Architecture: Short-term & Long-term

**Short-term Memory (Qdrant — Vector Search)**
- Fast, fuzzy semantic matching on recent/similar context
- Class B (behavioral) facts: promoted after 3 confirmations
- Class C (ephemeral) facts: expire after 30 days if unconfirmed
- Immediate retrieval without confirmation waiting period
- Useful for: "What was I working on?", "Who did I mention?"

**Long-term Memory (PostgreSQL — Relational)**
- Immutable, validated facts locked in forever
- Class A (identity/structural) facts: name, relationships, properties
- Non-destructive corrections via user-driven retractions
- Query-backed by graph and hierarchy traversal
- Useful for: "Where do I work?", "Who's my spouse?", "What are my kids' names?"

**Both work together:** Short-term catches loose threads; long-term locks in truth.

## Data Flow

### Ingest Pipeline

```
LLM extract → WGM gate → semantic conflict detection 
    → bidirectional validation → Class A/B/C → commit
```

1. **Extraction**: LLM converts conversation into typed relationship triples (subject, rel_type, object)
2. **WGM Gate**: Validates against ontology (`rel_types` table) and type constraints
3. **Conflict Detection**: Auto-supersedes facts when objects are already defined as types (e.g., can't own a type)
4. **Bidirectional Validation**: Prevents impossible relationships (e.g., child_of + parent_of for same pair)
5. **Classification**: Assigns to Class A (identity), Class B (behavioral), or Class C (novel/ephemeral)
6. **Commit**: Writes to PostgreSQL and/or stages facts for promotion

### Query Pipeline

```
baseline facts → graph traversal → hierarchy expansion 
    → Qdrant search → entity metadata → deduplication → rank & inject
```

1. **Baseline**: Load all identity-anchored facts from PostgreSQL
2. **Graph Traversal**: Single-hop connectivity across relationships (spouse, parent, works_for, etc.)
3. **Hierarchy Expansion**: Walk classification chains upward (instance_of, subclass_of, part_of)
4. **Vector Search**: Qdrant cosine similarity (nomic-embed-text, score ≥ 0.3)
5. **Metadata**: Attach entity types and alias lists
6. **Deduplication**: UUID-based merging (prevents duplicates from alias variation)
7. **Injection**: Inject ranked facts as system context before model responds

## Fact Classification (Phase 4)

### Class A — Identity/Structural (Write-through, Immediate)

Committed directly to `facts` table:
- pref_name, also_known_as, same_as
- parent_of, child_of, spouse, sibling_of
- born_on, born_in, has_gender, nationality, occupation
- instance_of, subclass_of

**Confidence**: 1.0 if user-stated; 0.8 if LLM-inferred
**User corrections always Class A** regardless of relationship type
**Synced to Qdrant** by re-embedder after insertion

### Class B — Behavioral/Contextual (Staged, Promoted on Confirmation)

Staged to `staged_facts`, promoted when `confirmed_count >= 3`:
- lives_at, lives_in, works_for, educated_at, owns
- likes, dislikes, prefers, friend_of, knows, met
- located_in, related_to, has_pet, part_of, created_by

**Confidence**: 0.8 if user-stated; 0.6 if LLM-inferred
**Lifecycle**: Staged → visible immediately in queries (no waiting) → promoted to facts when confirmed 3x
**TTL**: Indefinite (once promoted)
**Qdrant**: Synced immediately at ingest; staged point deleted after promotion

### Class C — Ephemeral/Novel (Staged, Expiring)

Staged with auto-expiry:
- Anything not in A or B
- Engine-generated relationship types
- Confidence < 0.6

**Confidence**: 0.4 if LLM-inferred
**Lifecycle**: Staged with `expires_at = now() + 30 days`
**Expiry**: Auto-deleted by re-embedder if unconfirmed
**No promotion path** — facts either confirm to B or expire

## Validation Pipeline

### Semantic Conflict Detection

Prevents ownership/behavioral facts on type entities:

```
If entity X instance_of TYPE_Y:
    Reject: owns(X), has_pet(X), works_for(X)
```

**Principle**: If Y is a type/category, it's not a separate entity. Can't own a type.

### Bidirectional Validation

Prevents contradictory directional relationships:

```
If child_of(A, B) exists and parent_of(A, B) attempted:
    Keep higher-confidence version, supersede lower
```

### Metadata-Driven Validation

All validation properties stored in `rel_types` table at runtime:

- `is_symmetric`: Relationship goes both ways (spouse, friend_of)
- `inverse_rel_type`: Opposite relationship (parent_of ↔ child_of)
- `is_leaf_only`: Object can't have further relationships
- `is_hierarchy_rel`: Part of classification chain

**Zero hardcoded rules.** New rel_types self-describe constraints via metadata columns.

## Traversal Systems (Orthogonal)

### Graph Traversal (`_REL_TYPE_GRAPH`)

Connectivity — who am I connected to?

**Included rels**: spouse, parent_of, child_of, sibling_of, has_pet, knows, friend_of, met, works_for, lives_at, lives_in, located_in, owns, educated_at, member_of

**Single-hop**: Fetches direct neighbors only

### Hierarchy Traversal (`_REL_TYPE_HIERARCHY`)

Composition & classification — what are they, what do they belong to?

**Included rels**: instance_of, subclass_of, part_of, is_a, member_of

**Bidirectional**:
- `direction="up"`: Entity → class chain (fraggle → instance_of → dog → subclass_of → animal)
- `direction="down"`: Class → members

**Cycle protection** via depth tracking

Both systems run in parallel during query. Results merged and deduplicated on `(subject_uuid, rel_type, object_uuid)`.

## Ontology & Relationship Types

Triple model: `(subject_id, rel_type, object_id)` aligned to Wikidata PIDs.

### Symmetric (bidirectional)
spouse, sibling_of, same_as, friend_of, knows, met

### Inverse Pairs
- parent_of ↔ child_of
- (Others defined in `rel_types.inverse_rel_type`)

### Self-Building

Novel `rel_type` values:
1. Classified as Class C initially
2. Tracked in `ontology_evaluations` table
3. Re-embedder evaluates asynchronously:
   - Frequency ≥ 3 occurrences → approve as new rel_type
   - Cosine similarity > 0.85 with existing type → map to existing
   - Otherwise → reject and expire

**No manual approval required** — system self-builds ontology from usage patterns.

## Database Schema

### Core Tables

**facts**: Immutable, validated facts
- Unique: `(user_id, subject_id, object_id, rel_type)`
- Soft-delete via `superseded_at IS NOT NULL`

**staged_facts**: Unconfirmed or ephemeral facts awaiting promotion/expiry
- Class B: promoted when `confirmed_count >= 3`
- Class C: deleted when `expires_at <= now()`

**entities**: Canonical entity registry with UUIDs
- `id`: UUID v5 surrogate (never display name)
- `entity_type`: Inferred type (Person, Location, Organization, etc.)

**entity_aliases**: Authoritative name mapping
- Unique: `(user_id, alias)` (case-insensitive, lowercased)
- `is_preferred`: Single preferred name per entity

**rel_types**: Live ontology with validation metadata
- Loaded at startup into memory cache
- Queried at runtime for constraint validation
- Columns: `is_symmetric`, `inverse_rel_type`, `is_leaf_only`, `is_hierarchy_rel`, `head_types`, `tail_types`

**entity_name_conflicts**: Collision detection & resolution
- Triggered when two entities claim same preferred name
- LLM-powered disambiguation loop
- Non-destructive: all names preserved, only preferred status reassigned

## Deduplication Strategy

UUID-based, not name-based:

1. All entity IDs stored as UUIDs (`_subject_id`, `_object_id`)
2. Display names stored separately in `entity_aliases`
3. Query dedup groups by `(subject_uuid, rel_type, object_uuid)`
4. **Result**: Same entity with multiple aliases = single deduplicated fact

Prevents hallucination from alias variation (chris/user/Christopher = one entity).

## Entity Type Classification

Three-layer inference:

1. **GLiNER2 extraction**: Named entity recognition → types
2. **Relationship semantics**: Type implied by rel_type (works_for → Person subject)
3. **Descriptor context**: Inferred from conversation

Type persisted to `entities.entity_type` only when current type = 'unknown'.

## Relevance & Sensitivity

**Identity facts always pass**:
- pref_name, also_known_as, same_as
- Relationship identities (spouse, parent_of, child_of, sibling_of)

**Everything else** passes if `confidence ≥ 0.4` (tunable via `MIN_INJECT_CONFIDENCE`)

**Sensitivity penalty** applied to PII:
- born_on, lives_at, lives_in, height, weight, born_in
- Penalized unless explicitly requested in query

## Re-embedder Background Loop

Runs continuously, polls database every `REEMBED_INTERVAL` seconds (default 10s).

### Tasks

1. **Promotion**: Class B facts with `confirmed_count >= 3` → facts table
2. **Expiry**: Class C facts with `expires_at <= now()` → deleted
3. **Embedding**: Unsynced facts → Qdrant upsert (nomic-embed-text-v1.5)
4. **Ontology Evaluation**: Novel rel_types evaluated for approval
5. **Name Conflict Resolution**: LLM-powered disambiguation

Post-promotion, staged Qdrant point is deleted (best-effort, outside transaction).

## Production Hardening

**Startup Validation**: Checks POSTGRES_DSN, QDRANT_URL, LLM endpoints (non-fatal if missing)

**Health Endpoint**: `/health` returns JSON with database, qdrant, llm, re-embedder status (5s cache)

**Timeouts**:
- HTTP: 10s (tunable `HTTPX_TIMEOUT`)
- Database: 30s (tunable `DB_TIMEOUT`)
- Qdrant: 10s (tunable `QDRANT_TIMEOUT`)

**Rate Limiting**: Per-user_id tracking, 100 req/min default (tunable `RATE_LIMIT_PER_MIN`)

**Query Fallback**: PostgreSQL-only response when embedding/Qdrant unavailable

## Qdrant Collection Naming

Per-user isolation:
- `"anonymous"`, `""`, or `"legacy"` user_id → env `QDRANT_COLLECTION` (default `"faultline-test"`)
- Any other user_id → `"faultline-{user_id}"`

Ensures multi-tenant deployments remain isolated.

## Key Design Principles

- **LLM has no unsupervised write access** — all writes flow through WGM validation gate
- **PostgreSQL is authoritative** — Qdrant is read-only derived view
- **Validation is metadata-driven** — no hardcoded rules, all in `rel_types` table
- **Write-time normalization** — entity IDs normalized to UUID v5 surrogates at ingestion
- **UUID-based deduplication** — prevents hallucination from alias variation
- **Non-destructive corrections** — user retractions soft-delete via `superseded_at`, never hard-delete
- **Self-healing graph** — semantic conflicts auto-superseded, no manual intervention
- **Dumb filter, smart backend** — OpenWebUI Function is stateless, backend does all ranking

## Running

```bash
# Develop
pip install -e ".[test]"
uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload

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

# LLM (extraction & conflict resolution)
QWEN_API_URL=http://localhost:11434/v1/chat/completions
WGM_LLM_MODEL=qwen/qwen3.5-9b
CATEGORY_LLM_MODEL=qwen2.5-coder

# Vector Search
QDRANT_URL=http://qdrant:6333
QDRANT_COLLECTION=faultline-test

# Performance Tuning
REEMBED_INTERVAL=10
HTTPX_TIMEOUT=10
DB_TIMEOUT=30
QDRANT_TIMEOUT=10
DB_POOL_SIZE=10
RATE_LIMIT_PER_MIN=100

# API
FAULTLINE_API_URL=http://localhost:8001
```
