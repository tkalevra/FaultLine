# FaultLine Flow: Plain English Overview

A simple explanation of how FaultLine remembers conversations.

---

## The Problem We're Solving

Regular AI assistants forget things. They forget:
- Your name by the next conversation
- Where you live
- Who your family is
- What you told them last week

Why? Because all they have is the current conversation window. Once you scroll up, it's gone.

FaultLine solves this by **actually remembering things you say** and using those memories in future conversations.

---

## How It Works: Three Simple Steps

### Step 1: Listen and Understand (Extract)

When you say something to the assistant, FaultLine listens for facts.

**Example:** "My son Dev is 12, and my wife is Mara."

FaultLine identifies:
- **Fact 1:** Dev is a son (relationship: parent_of)
- **Fact 2:** Mara is a wife (relationship: spouse)
- **Fact 3:** Dev's age is 12 (attribute)

**But wait, you might correct yourself:**
- "Actually, Dev is 14, not 12"

FaultLine notices the correction word "Actually" and marks it as an override. Your correction wins over any AI guess.

**Or you might forget something:**
- "I told you we had a dog, but we don't anymore"

FaultLine notices the retraction word "don't" and removes the old fact.

---

### Step 2: Learn and Store (Ingest)

Now FaultLine validates these facts before storing them.

**The system asks itself:**
- "Is 'parent_of' a known relationship type?" 
  - YES? Use what we know about it
  - NO? Learn it as something new
  
- "Is Dev a known type of person?"
  - YES? Skip
  - NO? Remember Dev is a person

- "Does this conflict with anything we know?"
  - Example: Can't have "Dev is a type of dog" AND "Dev is your son" at the same time

**Then it stores the fact with a confidence level:**
- **High confidence (you said it directly):** "My son Dev" → stored immediately ✓
- **Medium confidence (AI inferred it):** "Based on context, Dev is probably 14" → ask for confirmation ⚠️
- **Low confidence (AI guessed something new):** "Dev might be a Gemini zodiac sign" → hold for later evaluation ❓

---

### Step 3: Use It (Recall)

Later, you ask: "How old is Dev?"

FaultLine:
1. **Finds all relevant facts** about Dev
   - Direct facts: Dev is your son, age is 14
   - Connections: Dev is connected to you, to Mara
   - Context: Related to family memories

2. **Checks for consistency**
   - Does age=14 conflict with anything? No
   - Is the source trustworthy? Yes (you said it)

3. **Tells the AI**
   - Adds to context: "You have a son named Dev who is 14"
   - The AI sees this before answering

4. **AI responds naturally**
   - "Dev is 14 years old. How is he doing?"
   - No guessing, no forgetting, no asking "who is Dev?"

---

## Why This Is Better Than Just RAG (Vector Search)

Most AI memory systems use "vector search" — they turn everything into numbers and search by similarity.

**Problem:** Similarity doesn't mean truth.

Example:
- You say: "I live in Toronto"
- AI searches for similar concepts
- Finds: "Toronto is in Canada" and "Vancouver is in Canada"
- Might incorrectly suggest you live in Vancouver

**FaultLine's approach:** Store facts as facts, not as vectors.
- We KNOW you live in Toronto because you said it
- We KNOW Toronto is in Canada from data
- We NEVER confuse one for the other

Vectors help us find related memories, but **facts decide what's true**.

---

## The Learning Loop: No Hardcoding Needed

Traditional systems need someone to hardcode:
- "These are the relationship types we support"
- "These are the entity types we support"
- "Here's the validation logic"

FaultLine learns these as it goes.

**Example:** First time you mention "parent_of"
1. System doesn't know it yet
2. Creates an entry: "parent_of = a relationship type"
3. Stores: Is it symmetric? (No. If I'm parent of you, you're child of me, not parent)
4. Next time someone uses "parent_of", the system already knows the rules

**Result:** Every fact makes the system smarter. No hardcoding. No code changes. Just learning.

---

## Key Concept: Three Types of Storage

Facts get stored in different places depending on what they are:

| Type | Example | Storage | Purpose |
|------|---------|---------|---------|
| **Attributes** | "age = 14" | Single value table | Quick lookup |
| **Relationships** | "spouse → Mara" | Connection graph | Find connected people |
| **Classifications** | "Dev is a Person" | Hierarchy | Understand what things are |

This matters because the system can answer different questions efficiently:
- "How old is Dev?" → Quick attribute lookup
- "Who is Dev connected to?" → Graph traversal
- "What am I?" → Hierarchy traversal

---

## Three Confidence Levels

Not all facts are equal.

**Class A — You said it directly**
- "My name is Alex"
- Confidence: 100% (1.0)
- Storage: Permanent (in `facts` immediately; never reclassified)

**Class B — AI inferred it following the established ontology**
- AI saw: "I work at Acme Corp" (Acme's ontology already exists)
- Confidence: 80% (0.8) or 60% (0.6) if ontology was created in-flow
- Storage: Lives in `staged_facts` first; **materializes** into `facts` (still Class B — NOT promoted to A) once it's been seen 3 times

**Class C — AI guessed something new (no ontology yet)**
- AI inferred: "You might be interested in gardening" (based on one mention)
- Confidence: 40% (0.4)
- Storage: Temporary (30-day expiry). If it keeps showing up — or keeps being recalled — it **graduates to Class B** at the 3-confirmation threshold (the only tier change in the system). If a recall hit keeps bumping it, the same C→B graduation fires.

**Why?** Because corrections from you matter most. AI guesses come second. Random patterns expire. A and B never change class; only C ever promotes (to B).

---

## Example: Full Cycle

### Day 1
**You say:** "My son Dev is 12, and my wife is Mara"

FaultLine:
- Extracts facts
- Validates them
- Stores with high confidence (you said it)
- Learns: parent_of relationship, spouse relationship

### Day 2
**You say:** "Actually Dev is 14 now"

FaultLine:
- Detects the correction
- Updates the fact
- Marks it as user-corrected

### Day 3
**You ask:** "Tell me about my family"

FaultLine:
- Retrieves: Dev (son, age 14), Mara (spouse)
- Formats naturally: "Your son Dev is 14 and your wife is Mara"
- Injects into AI context
- AI responds: "You have a son Dev who is 14 and a wife Mara. That's a nice family! How are they doing?"

**No guessing. No forgetting. Just remembering.**

---

## What Makes This Different

**Traditional AI:**
- "I don't have information about your family"
- "You'll need to tell me again"
- Hallucinations: "Your son is probably named John" (just guessing)

**FaultLine-Enhanced AI:**
- "Your son Dev is 14 and your wife is Mara"
- Remembers across conversations
- Never guesses about your personal facts
- Asks for clarification if unsure

---

## The Philosophy

FaultLine treats **facts as sacred**:
- Facts come from you (highest trust)
- Facts are validated before storage (no garbage in)
- Facts are organized intelligently (right retrieval path)
- Facts are used carefully (not confabulated)

**Result:** An assistant that actually remembers you, respects your corrections, and never pretends to know what it doesn't.

That's FaultLine.
