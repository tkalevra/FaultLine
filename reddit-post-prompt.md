# Reddit Post: FaultLine — Personal Knowledge Graph with Semantic Validation

**Subreddit:** r/programming, r/databases, r/rust (cross-post or choose primary)  
**Title:** "FaultLine: A Write-Validated Personal Knowledge Graph with Semantic Conflict Detection"  
**Format:** Detailed technical post with ASCII diagram, GitHub link, call to action

---

## Post Content

### Opening Hook

We built **FaultLine** — a personal knowledge graph system that intercepts conversations, extracts relationships, validates them against an ontology, and keeps them semantically consistent. No hallucinations, no conflicting data. Graph stays clean.

**The problem we solved:** Extraction systems create conflicting facts. User says "I have a dog named Fraggle, a morkie mix." System extracts:
- ✅ `fraggle instance_of morkie` (correct)
- ❌ `user owns morkie` (wrong — morkie is a breed type, not a separate entity)

We built **three layers of defense** to prevent this.

---

### The System Architecture

```
OpenWebUI Message
     ↓
┌────────────────────────────────────┐
│  Layer 1: EXTRACTION CONSTRAINT    │ ← dprompt-58
│  ─────────────────────────────────── 
│  LLM Prompt tells model: "When     │
│  you extract instance_of/subclass_ │
│  of for entity B, do NOT also      │
│  extract owns/has_pet for B.       │
│  (B is a type, not separate entity)│
└────────────────────────────────────┘
     ↓ (prevents ~80% of conflicts)
┌────────────────────────────────────┐
│  Layer 2: SEMANTIC VALIDATION      │ ← dprompt-59
│  ─────────────────────────────────── 
│  At ingest time, check: does this  │
│  new fact contradict the graph?    │
│  Query hierarchy relationships.    │
│  Auto-supersede conflicting facts. │
└────────────────────────────────────┘
     ↓ (catches the rest)
┌────────────────────────────────────┐
│  Layer 3: USER CORRECTIONS         │ ← Retraction Flow
│  ─────────────────────────────────── 
│  User says "forget that", "wrong", │
│  "no longer" → system retracts &   │
│  supersedes. Explicit control.     │
└────────────────────────────────────┘
     ↓
PostgreSQL (facts table — source of truth)
     ↓
Qdrant (vector search, re-embedded async)
     ↓
Backend /query (graph traversal + hierarchy expansion + vector search)
     ↓
OpenWebUI Filter (trusts backend ranking, injects memory)
```

---

### What Makes This Different

| Approach | Traditional LLM RAG | FaultLine |
|----------|-------------------|-----------|
| **Data validation** | Post-hoc or none | Write-time, multi-layer |
| **Conflict detection** | Ignored | Graph-aware, semantic |
| **Data cleanup** | Manual | Automatic (with audit trail) |
| **Graph semantics** | Treated as strings | Enforced via ontology |
| **Hierarchy support** | Limited | Full (instance_of, subclass_of, part_of, member_of) |
| **User corrections** | Stored as new facts | Auto-supersedes conflicting old facts |
| **Audit trail** | None | Full: why facts were kept/rejected |

---

### Key Technical Wins

**1. Extraction-Time Constraint (dprompt-58)**
- Teaches LLM semantic principles with multi-domain examples
- Scope: "When X is a type/category/component, it's not a separate entity"
- Applies across domains: pet breeds, org roles, network components, geography containers, software modules

**2. Semantic Conflict Detection (dprompt-59)**
- Queries graph: is this entity object of a hierarchy relationship?
- If yes + new fact tries to assign independent properties → CONFLICT
- Auto-supersedes with reason: "type_conflict: morkie is object of instance_of"
- No manual intervention needed

**3. Proactive + Reactive**
- Layer 1: Prevent conflicts at extraction (LLM constraint)
- Layer 2: Catch conflicts at ingest (graph validation)
- Layer 3: Handle explicit user corrections (retraction flow)

**Result:** Graph is self-healing. Data quality improves over time, not degrades.

---

### The Stack

- **Backend:** Python/FastAPI (`src/api/main.py`)
- **Database:** PostgreSQL (facts, staged_facts, entities, ontology)
- **Vector DB:** Qdrant (derived from PostgreSQL via async re-embedder)
- **Extraction LLM:** Qwen (configurable, tested with faultline-wgm-test-10)
- **Frontend:** OpenWebUI (Filter + Function plugins)
- **Deployment:** Docker Compose, TrueNAS homelab (tested in production)

**Ontology:** 30+ relationship types (parent_of, spouse, instance_of, part_of, works_for, etc.) with semantic constraints.

---

### Current State

- **Version:** v1.0.3 (conflict detection deployed)
- **Status:** Production-ready, actively tested
- **Tests:** 114+ passing, 0 regressions
- **Lines of code:** ~3,600 (tight, focused scope)
- **Architecture:** Dumb Filter (trusts backend) + Smart Backend (validates all writes)

**Latest features:**
- ✅ Hierarchy extraction (multi-domain: taxonomies, orgs, infrastructure, etc.)
- ✅ Semantic conflict detection (auto-supersedes based on graph)
- ✅ Retraction flow (user corrections explicitly handled)
- ✅ Audit trail (why facts were kept/rejected logged)

---

### What We're Looking For

1. **Community Testing:** Try it on your own knowledge graph. Does it keep your data clean?
2. **Feedback:** Architecture ideas? Better conflict patterns? Edge cases we missed?
3. **Contributions:** Want to add new rel_types, improve extraction, or extend the ontology?
4. **Use Cases:** What would you use this for? Personal CRM? Research notes? Family knowledge base?

---

### GitHub & Getting Started

**Repository:** [tkalevra/FaultLine](https://github.com/tkalevra/FaultLine)

```bash
git clone https://github.com/tkalevra/FaultLine.git
cd FaultLine
docker compose up --build
```

Then open OpenWebUI at `http://localhost:3000`, configure the Filter, and start talking.

**Documentation:**
- `CLAUDE.md` — system architecture + key principles
- `PRODUCTION_DEPLOYMENT_GUIDE.md` — deployment SOP
- `docs/ARCHITECTURE_QUERY_DESIGN.md` — design decisions
- `BUGS/` directory — known issues + fixes in progress

---

### Questions?

- How does this compare to other personal knowledge graph systems?
- What hierarchies would you extract?
- Should conflict detection be more/less aggressive?
- Want to contribute a new rel_type or validation pattern?

**Drop a comment or open an issue on GitHub.** We're actively building this and love feedback from the community.

---

## Post Strategy

**Why Reddit?**
- r/programming: Broad audience, appreciate architecture + implementation
- r/databases: Focus on PostgreSQL + ontology validation angle
- r/rust: If we mention Rust anywhere (or for general systems folks)
- Cross-post to r/MachineLearning if emphasizing the extraction + validation loop

**Call-to-action priorities:**
1. Star the GitHub repo (visibility)
2. Try it locally (hands-on feedback)
3. Open issues (edge cases, improvements)
4. Discuss in comments (architecture, use cases)

**Tone:** Technical but accessible. Show the innovation (semantic validation), not just the code. Invite collaboration.

---

## Follow-Up Posts (Optional)

If this gains traction, follow up with:
1. **Deep dive:** How semantic conflict detection works (code walkthrough)
2. **Case study:** Using FaultLine for a specific domain (family tree, research notes, etc.)
3. **Architecture:** Comparison with other knowledge graph systems
4. **Lessons learned:** What we've built, what we'd do differently

---

**Ready to post?** Copy the content, adjust subreddit as needed, and launch!
