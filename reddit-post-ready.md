FAULTLINE: PERSONAL KNOWLEDGE GRAPH FOR OPENWEBUI WITH SEMANTIC CONFLICT DETECTION

Title: FaultLine: Personal Knowledge Graph for OpenWebUI with Semantic Conflict Detection

---

Hey r/OpenWebUI community! I want to share a project we've been building that plugs right into OpenWebUI as a Filter + Function plugin: FaultLine — a write-validated personal knowledge graph system.

THE PROBLEM WE SOLVED

You're chatting with OpenWebUI, and you mention: "I have a dog named Max, a golden retriever mix."

Traditional extraction systems would store:
✓ max instance_of golden_retriever (correct)
✗ user owns golden_retriever (wrong — golden_retriever is a breed type, not a separate dog)
✗ user owns max (correct, but now you have conflicting facts)

Result: Your knowledge graph gets messy. Queries break. You end up with "two dogs" when you meant one.

WHAT FAULTLINE DOES

It intercepts OpenWebUI messages, extracts relationships, validates them against an ontology, and keeps the graph semantically consistent. No conflicting data, no hallucinations — the graph stays clean.

Here's how the three-layer validation works:

LAYER 1: EXTRACTION CONSTRAINT
- LLM prompt rule: "When you extract instance_of/subclass_of for entity B, do NOT also extract owns/has_pet for B. B is a type/category, not a separate entity."
- Prevents ~80% of conflicts at the source

LAYER 2: SEMANTIC VALIDATION (Metadata-Driven)
- At ingest time, query validation rules from the database
- rel_types table stores: is_leaf_only, inverse, is_symmetric, is_hierarchy_rel, etc.
- If a fact violates a constraint → auto-supersede it
- New rel_types self-describe their validation rules (no code changes needed)

LAYER 3: USER CORRECTIONS (Retraction Flow)
- User says: "That's wrong", "Forget that"
- System: Retracts & supersedes automatically
- Explicit user control, non-destructive updates

All facts flow to: PostgreSQL → Qdrant (vector search) → /query endpoint (graph traversal + hierarchy expansion) → OpenWebUI Filter (injects memory before model sees it)

HOW IT ACTUALLY WORKS IN PRACTICE

You say: "I have a dog named Max, a golden retriever. I work in engineering."

System extracts and validates:
✓ max instance_of golden_retriever → stored (Class A, confidence 1.0)
✓ user has_pet max → stored (Class B, confidence 0.8)
✗ user owns golden_retriever → auto-superseded (Layer 2 detected: golden_retriever is a type)
✓ user works_in engineering → stored (Class A, confidence 1.0)

Result: Knowledge graph is clean, semantically valid, no conflicts.

Later, you ask the model: "What pets do I have?"

Model gets injected memory:
⊢ FaultLine Memory
- Pets: Max (a golden retriever, a dog)
- Occupation: Engineering
- Related facts: [traversed via graph]

Model responds with accurate, consistent information.

WHAT'S DIFFERENT

Feature                    | Traditional RAG               | FaultLine
Data validation            | Post-hoc or none              | Write-time, multi-layer, metadata-driven
Validation rules           | Hardcoded or absent           | Live in database (rel_types table)
Conflict detection         | Ignored                       | Graph-aware, semantic, automatic
Cleanup                    | Manual                        | Automatic (with reasons logged)
Hierarchies                | Weak or absent                | Full support (instance_of, subclass_of, part_of, member_of)
Duplicate facts            | Ignored                       | UUID-based deduplication (no display-name duplicates)
User corrections           | Stored as new facts           | Auto-supersedes conflicts (non-destructive)
Graph quality              | Degrades over time            | Improves over time

THE TECH STACK

Backend: Python/FastAPI
Database: PostgreSQL (facts, staged_facts, entities, ontology)
Vector DB: Qdrant (derived from PostgreSQL via async re-embedder)
Extraction LLM: Configurable (tested with Qwen)
OpenWebUI Integration: Filter + Function plugins
Deployment: Docker Compose

WHAT'S LIVE RIGHT NOW

Version: v1.0.7 (Query Deduplication + Metadata-Driven Validation)
Status: Production-ready, fully tested
Tests: 114+ passing, 0 regressions
Architecture: Dumb Filter (trusts backend) + Smart Backend (validates all writes)

Latest features:
✓ Metadata-driven validation framework (validation rules live in database, not code)
✓ UUID-based query deduplication (no duplicate facts with different display names)
✓ Multi-domain hierarchy extraction (taxonomies, org charts, infrastructure, locations, software)
✓ Semantic conflict detection (auto-resolves based on graph structure)
✓ Bidirectional relationship validation (prevents impossible parent/child combinations)
✓ Name collision resolution (LLM-powered entity disambiguation)
✓ Retraction flow (user corrections handled explicitly, non-destructive)
✓ Audit trail (why facts were kept/rejected is logged)
✓ 8 production bugs fixed (dBug-001 through dBug-008, all closed)

GET STARTED

GitHub: https://github.com/tkalevra/FaultLine

git clone https://github.com/tkalevra/FaultLine.git
cd FaultLine
docker compose up --build

Then configure the OpenWebUI Filter to point to your FaultLine instance, and start chatting. Your knowledge graph will be extracted, validated, and kept clean automatically.

Key files:
- CLAUDE.md — system architecture + design principles
- PRODUCTION_DEPLOYMENT_GUIDE.md — deployment walkthrough
- docs/ARCHITECTURE_QUERY_DESIGN.md — deep dive on the design
- BUGS/ — known issues + fixes

WHAT WE'RE LOOKING FOR

1. Community Testing: Try it locally. Does it keep your personal knowledge graph clean?
2. Feedback: Ideas for improvements? Edge cases we missed? Better ways to handle conflicts?
3. Use Cases: What would you use this for? Family knowledge base? Research notes? Personal CRM?
4. Integration Help: Want to help integrate FaultLine with other OpenWebUI extensions or LLMs?

QUESTIONS?

Drop them in the comments or open an issue on GitHub. We're actively building this and love feedback from the OpenWebUI community.

Star the repo if you find this useful!

---

UPDATE: This project evolved from an earlier discussion on memory approaches in OpenWebUI (https://www.reddit.com/r/OpenWebUI/comments/1szyer4/trying_a_different_approach_to_memory_in/). We built FaultLine to solve the exact problem discussed there: keeping personal knowledge graphs clean and semantically valid instead of letting hallucinations and conflicts pile up over time.
