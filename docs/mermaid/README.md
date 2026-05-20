# FaultLine Pipeline Diagrams (Mermaid)

Pure Mermaid graph files for use with [Mermaid Live Editor](https://mermaid.live).

## Files

- **extract-pipeline.mmd** - How FaultLine identifies and corrects facts from conversation
- **ingest-pipeline.mmd** - How FaultLine learns, validates, and stores facts (with 3-layer dynamic type creation)
- **recall-pipeline.mmd** - How FaultLine retrieves and injects facts into LLM context

## How to Use

1. Go to https://mermaid.live
2. Open the `.mmd` file in a text editor (or raw view on GitHub)
3. Select all content (Ctrl+A / Cmd+A)
4. Copy (Ctrl+C / Cmd+C)
5. Paste into the Mermaid Live editor
6. Diagram renders automatically

## What Each Pipeline Shows

### Extract Pipeline
- Correction detection (actually, wrong, corrected to)
- Retraction detection (forget, delete, no longer)
- Rel_type validation against database metadata
- Novel rel_type identification
- Type constraint validation
- Flags facts with correction/retraction markers before ingest

### Ingest Pipeline
- **Layer 1:** Rel_type creation (engine learns new relationships)
- **Layer 2:** Entity type creation (engine learns classifications)
- **Layer 3:** Fact storage (SCALAR / RELATIONAL / HIERARCHICAL)
- Correction path (bypasses staging → Class A immediately)
- Retraction path (deletes/supersedes facts)
- Semantic conflict detection
- Bidirectional relationship validation
- Class A/B/C assignment logic
- Re-embedder evaluation loop

### Recall Pipeline
- 4 parallel retrieval sources (Baseline, Graph, Hierarchy, Attributes)
- UUID-based deduplication (prevents alias multiplication)
- Scope-based filtering (family, work, etc.)
- Display name resolution (UUID → human readable)
- Prose formatting (natural language, not raw tuples)
- Context injection into LLM

## Key Concept: Three-Layer Learning

```
User Input
    ↓
[LAYER 1] Engine learns new rel_types
    ↓
[LAYER 2] Engine learns entity types
    ↓
[LAYER 3] Engine stores facts with correct routing
    ↓
Result: Dynamic knowledge base, zero hardcoding
```

This is why FaultLine requires **no hardcoded relationship types, entity types, or validation rules**. The engine learns as it goes.

---

All diagrams are:
- ✅ Technically accurate
- ✅ Accessible to lay people
- ✅ Color-coded for clarity
- ✅ Example-driven
- ✅ Pure Mermaid syntax (no Markdown wrapper)
