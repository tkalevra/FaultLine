<p align="center">
  <img src="docs/faultline_logo.svg" alt="FaultLine Logo" />
</p>

# FaultLine

**FaultLine is a self-correcting memory system for LLM applications.**

Most AI memory systems degrade over time — accumulating stale, incorrect, and conflicting information.

FaultLine does the opposite.

It enforces a controlled write path, builds structured knowledge from interaction, and continuously converges toward higher-quality memory through validation, reinforcement, correction, and decay.

---

## Why FaultLine Exists

LLM memory systems fail in production for predictable reasons:

- They trust the model to write correct data  
- They store everything without validation  
- They accumulate stale or contradictory facts  
- They have no mechanism for correction or decay  

Over time, this leads to:

- retrieval noise  
- conflicting context  
- degraded agent performance  

**FaultLine solves this by treating the LLM as an untrusted writer.**

Memory is not stored by default — it is evaluated, structured, and earned.

---

## What It Does

FaultLine transforms raw interaction into structured, governed memory:

- Extracts entities and relationships from conversations  
- Infers missing structure (hierarchies, causality, relationships)  
- Validates all writes against a dynamic ontology  
- Builds a continuously evolving knowledge graph  
- Promotes high-confidence knowledge to long-term memory  
- Removes unused or unreinforced knowledge automatically  
- Allows real-time correction of facts and structure  

---

## Core Mechanics

### Write Validation
No information is persisted without passing a validation gate.

### Memory Promotion (C → B → A)
Facts move through a lifecycle:

- **Class C** — Candidate (ephemeral, low confidence)  
- **Class B** — Confirmed (reinforced through interaction)  
- **Class A** — Canonical (user-confirmed truth)  

### Reinforcement
Frequently referenced knowledge strengthens and persists.

### Decay
Unused or unreinforced knowledge expires automatically.

### Structural Inference
FaultLine builds missing relationships:
- hierarchy (family → members → pets → animals)
- causality
- relational links

### Live Correction
Users can modify the graph through natural interaction:

"my name isn’t Todd, it’s Bradly"  
"my pets are not part of my family"

FaultLine updates:
- facts  
- relationships  
- graph structure  

All changes are non-destructive and auditable.

---

## How It’s Different

| Traditional Systems | FaultLine |
|-------------------|----------|
| Trust model output blindly | Treat model as untrusted writer |
| Store everything | Validate before persistence |
| Accumulate noise over time | Decay unused knowledge |
| Static or no schema | Self-evolving ontology |
| Hard to correct | Interactive correction |
| Requires cleanup | Self-maintaining |

---

## System Model

Agent Runtime (OpenWebUI / LangChain / etc.)  
↓  
FaultLine (extract → validate → infer → promote → decay)  
↓  
PostgreSQL (graph) + Qdrant (semantic)

FaultLine sits beneath your application.

- Your agent writes → FaultLine decides what becomes memory  
- Your agent reads → FaultLine returns structured knowledge  

---

## Retrieval Model

FaultLine uses multi-path retrieval:

- Graph traversal (relationships, hierarchy)  
- Semantic similarity (Qdrant)  
- Confidence-weighted facts (A/B/C classes)  

This ensures:

- weak signals remain visible  
- strong knowledge dominates  
- incorrect structure can be corrected through use  

---

## Design Philosophy

### Memory must be earned
Inference alone is not enough — knowledge must be used and reinforced.

### Memory must adapt
Facts, relationships, and structure must be correctable through interaction.

### Memory must decay
Unverified or unused knowledge should not persist indefinitely.

---

## Positioning

FaultLine is not:

- a vector database  
- a RAG system  
- an agent framework  

It is:

→ a governed memory layer that ensures long-term memory remains usable

Use it with:
- OpenWebUI  
- LangChain  
- custom agents  

---

## Quick Start

```bash
git clone https://github.com/your-org/FaultLine.git
cd FaultLine

cp .env.example .env
# edit .env

docker compose up -d
```

---

## Development Status

- Core architecture implemented ✅  
- Self-correcting lifecycle ✅  
- Dynamic ontology ✅  
- Production-scale validation → in progress  

---

## License

Licensed under the MIT License — see [LICENSE](LICENSE)
