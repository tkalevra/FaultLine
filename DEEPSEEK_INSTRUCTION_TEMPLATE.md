# DEEPSEEK_INSTRUCTION_TEMPLATE.md

## Purpose

This document defines the instruction format for all future prompts to deepseek (V4-Pro). Follow this structure to ensure instructions are clear, actionable, and aligned with V4-Pro's strengths.

**Why this matters:** V4-Pro performs best when instructions are explicitly structured, placed in user prompts (not buried in context), and use "Wrong/Right" pairs instead of "DO NOT" directives.

---

## Standard Instruction Format

All prompts to deepseek should follow this structure:

```markdown
## #deepseek [STATUS]: [PROMPT_NAME] — [DATE]

### Task:
One sentence: what should deepseek build/fix/understand?

### Context:
Background: why is this needed? What was tried before? What failed?

### Constraints:
- Explicit limitations or requirements
- Wrong: [common mistake you don't want]
- Right: [correct approach]
- MUST: [non-negotiable requirement]
- MAY: [optional enhancement]

### Sequence (if multi-step):
1. [First step]
2. [Second step]
3. [Third step]

DO NOT skip steps. DO NOT reorder.

### Deliverable:
What should the code/analysis look like? What should change?

### Files to Modify:
- `file1.py` — [what changes]
- `file2.sql` — [what changes]

### Success Criteria:
How do we know this works?
- Test scenario: [describe]
- Expected result: [describe]
```

---

## Why This Format Works for V4-Pro

| Element | Why It Works |
|---------|------------|
| **Task:** first | V4-Pro reads ahead; seeing the task upfront anchors reasoning |
| **Context:** after Task | Background informs decision-making without burying the goal |
| **Wrong/Right pairs** | V4-Pro responds better to explicit contrasts than "DO NOT" directives |
| **Sequence: ordered list** | Numbered lists signal "must follow this order" more reliably than prose |
| **Deliverable:** explicit | Reduces ambiguity about what "done" looks like |
| **Files to Modify:** explicit | No guessing about scope |

---

## Common Mistakes (Don't Do This)

```markdown
❌ WRONG:

## dprompt-X: Nested Taxonomy Layers

This prompt is about fixing the cascade logic. The system currently has scope layers 
that don't match the intended architecture. You need to understand the graph + hierarchy 
model instead. Here's a lot of context about why this matters... [10 paragraphs] ... 
So the key constraints are: DO NOT deploy the old code, DO NOT use scope layers, 
you MUST implement graph traversal first, SHOULD add hierarchy later...

✅ RIGHT:

## #deepseek [STATUS]: dprompt-X — Date

### Task:
Redesign /query endpoint to use graph + hierarchy traversal instead of scope layers.

### Context:
Scope layer model (dprompt-24/25) was implemented but doesn't match intended architecture. 
Graph = connectivity (who matters), Hierarchy = composition (what they are).

### Constraints:
- Wrong: Cascade queries on scope layers (layer <= 2)
- Right: Graph traversal finds relevant entities, hierarchy enriches with categories
- MUST: Read dprompt-26 first to understand correct architecture

### Sequence:
1. Read dprompt-26.md
2. Code dprompt-27 (graph + hierarchy redesign)
```

---

## Applying This Template

When writing a new prompt:

1. **Use this format explicitly** — don't deviate
2. **Keep Task: one sentence** — if it needs more, it's not clear enough
3. **Use Wrong/Right pairs instead of "DO NOT"** — V4-Pro responds better
4. **Order lists when sequence matters** — numbered = mandatory order
5. **Restate the task after context** ("task sandwich") — prevents burying goals

---

## Examples

### Example 1: Bug Fix Prompt

```markdown
## #deepseek NEXT: dprompt-25 — Layer Assignment Bug Fix

### Task:
Fix FactStoreManager to assign layer values to facts during ingest.

### Context:
Migration 020 added `layer` column to facts table (default 1). Entities get layer 
assignments correctly, but facts are inserted without specifying layer, so they 
all default to layer 1. Cascade queries fetch facts by layer and miss layer 2+ facts.

### Constraints:
- Wrong: Facts inserted without layer parameter (current state)
- Right: Facts passed layer value from `_classify_entity_layer(rel_type)`
- MUST: Update both commit() and _commit_staged() calls
- MAY: Add logging to trace layer assignments

### Sequence:
1. Update FactStoreManager.commit() signature to accept layer parameter
2. Update INSERT statement to include layer column
3. Update all commit() calls in ingest to pass _entity_layer
4. Update staged_facts INSERT to include layer

### Deliverable:
- FactStoreManager.commit() accepts layer parameter
- Facts inserted with correct layer value from rel_type context
- Staged facts also receive layer assignments

### Files to Modify:
- `src/fact_store/store.py` — commit() signature and INSERT
- `src/api/main.py` — update commit() calls, staged_facts INSERT

### Success Criteria:
- Test: POST /ingest "I have wife Mars. Mars has dog Fraggle."
- Expected: has_pet fact stored with layer=2
- Verify: SELECT * FROM staged_facts WHERE rel_type='has_pet' shows layer=2
```

### Example 2: Architecture Clarification Prompt

```markdown
## #deepseek NEXT: dprompt-26 — Architecture Clarification

### Task:
Understand the correct architecture: graph + hierarchy, not scope layers.

### Context:
dprompt-24 implemented scope layers (layer 1/2/3/4 filtering queries). This doesn't 
match the intended architecture. Real intent: graph traversal finds relevant entities, 
hierarchy traversal finds their composition/classification. Two separate concerns, 
not nested scope filters.

### Constraints:
- Wrong: Scope layers constrain query results (old approach)
- Right: Graph = connectivity, Hierarchy = composition; traverse both independently
- MUST: Read and understand before coding dprompt-27
- MUST: Do NOT implement dprompt-24/25 as-written

### Deliverable:
Understanding of:
- Graph edges (spouse, has_pet, knows) for relevance
- Hierarchy edges (part_of, instance_of, subclass_of) for details
- Query flow: graph traversal → hierarchy enrichment → merged results

### Files to Reference:
- `dprompt-24.md` — understand why scope layers are wrong
- `dprompt-26.md` — correct architecture design

### Success Criteria:
- Can explain: "graph finds Mars and Fraggle; hierarchy shows they're Family"
- Can distinguish: connectivity vs composition
- Ready to code dprompt-27 (query redesign)
```

---

## Moving Forward

**All future prompts to deepseek must use this format.** It ensures:
- Clear task definition
- Explicit constraints
- Unambiguous success criteria
- Better V4-Pro performance
- Easier handoffs between agents

If you deviate from this template, deepseek may miss critical constraints or misunderstand priorities.
