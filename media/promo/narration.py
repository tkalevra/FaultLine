"""Voiceover script, keyed to the on-screen cue points.

Each entry maps (SceneClass, cue text) -> spoken line. The cue text is the
exact string passed to `say()` in the scene, so the two can never silently
drift apart: build.py fails loudly if a cue has no line, or a line has no cue.

`delay` nudges a line later than its cue, for beats where the visual should
land a moment before the voice does.

Lines are written to be *spoken*, so they differ slightly from the on-screen
text — the caption is the claim, the narration is the argument.
"""

# (scene, cue) -> line   |   or (line, delay_seconds)
SCRIPT = {
    # ---------------------------------------------------- 01  how RAG works
    ("RagPipeline", "Your documents go in."):
        "It starts with your documents.",
    ("RagPipeline", "Chopped into fixed-size chunks."):
        "They get chopped into fixed-size chunks.",
    ("RagPipeline", "Each one becomes a vector — a point in space."):
        "Each chunk is turned into a vector. A point in space.",
    ("RagPipeline", "Text becomes coordinates. Meaning becomes distance."):
        "Text becomes coordinates. Meaning becomes distance.",
    ("RagPipeline", "This is RAG — retrieval-augmented generation."):
        "This is RAG. Retrieval-augmented generation.",
    ("RagPipeline", "Your question is embedded the same way."):
        "Your question is embedded the same way,",
    ("RagPipeline", "It returns whatever sits closest."):
        "and the system hands back whatever happens to sit closest.",
    ("RagPipeline", "Nothing here was verified. It was ranked."):
        "Nothing here was verified. It was ranked.",

    # ------------------------------------------------- 02  where it breaks
    ("RagFailure", "You asked for one fact. Here is what came back."):
        "You asked for one fact. Here is what came back.",
    ("RagFailure", "The row you needed ranked 47th. Similarity is not truth."):
        "Near misses. The row you needed ranked forty-seventh.",
    ("RagFailure", "So you correct it."):
        "So you correct it.",
    ("RagFailure", "The old chunk is still there. Both get retrieved."):
        "But the old chunk is still there, and both get retrieved.",
    ("RagFailure", "There is no row to update — only copies to outvote."):
        "There is no row to update. Only copies to outvote.",
    ("RagFailure", "You can't fix precision. So you compensate with volume."):
        "You can't fix the precision, so you compensate with volume.",
    ("RagFailure", "Forty chunks. To answer one question."):
        "Forty chunks, to answer one question.",

    # ------------------------------ 03  why more context makes the answer worse
    ("ContextCollapse", "You retrieve a handful of chunks. That is your context."):
        "What you retrieve becomes the model's context.",
    ("ContextCollapse", "Short context: the model reads all of it. Your row lands well."):
        "A short context is read well.",
    ("ContextCollapse", "But precision was bad, so you retrieved more."):
        "But precision was bad, so you retrieved more. And more.",
    ("ContextCollapse", "The context grew — and the middle of it collapsed."):
        "The context grew, and the middle collapsed.",
    ("ContextCollapse", "Your row is now buried exactly where accuracy is worst."):
        "Buried exactly where accuracy is worst.",
    ("ContextCollapse", "So it does what it always does."):
        "So it does what it always does. It answers anyway.",
    ("ContextCollapse", "It will not say “I don’t know.” It fills the gap."):
        "It will not tell you it doesn't know. It fills the gap.",

    # ---------------------------------------------------- 04  RAG poisoning
    ("Poisoning", "Everything so far was RAG failing on its own."):
        "Everything so far was an accident.",
    ("Poisoning", "This is someone making it fail — an attack called RAG poisoning."):
        "This is deliberate. It's called RAG poisoning.",
    ("Poisoning", "Anything that reaches the index becomes knowledge."):
        "Nothing checks what goes in. Anything reaching the index becomes "
        "knowledge.",
    ("Poisoning", "The same pipeline. No validation. No gate."):
        "The same pipeline. No validation. No gate.",
    ("Poisoning", "Ask the question they targeted."):
        "Five texts, crafted to sit nearest the question they target.",
    ("Poisoning", "It is retrieved, trusted, and repeated as fact."):
        "Retrieved, trusted, and repeated as fact.",
    ("Poisoning", "Five documents. In a corpus of millions."):
        "Five documents, in a corpus of millions.",

    # ------------------------------------------ 05  what FaultLine does instead
    ("TheGate", "@logo"):
        ("FaultLine does the opposite.", 1.9),
    ("TheGate", "Nothing is stored until it has been checked."):
        "Nothing is stored until it has been checked. Every write passes a "
        "validation gate.",
    ("TheGate", "Facts land as typed relationships — not text, not chunks."):
        "What passes lands as typed relationships. Not text. Not chunks.",
    ("TheGate", "A hallucination never becomes a memory."):
        "A hallucination never becomes a memory.",
    ("TheGate", "And when something changes —"):
        "And when something changes, the record is superseded.",
    ("TheGate", "the record is superseded — the old value archived, not competing."):
        "The old value is archived. Not left to compete.",

    # -------------------------------------------------- 06  asking for it back
    ("TheWalk", "No search. It resolves the entity."):
        "This is not a search. It resolves the entity, then walks the edge.",
    ("TheWalk", "It returns the row. Not the neighbourhood."):
        "It returns the row. Not the neighbourhood.",
    ("TheWalk", "Same question, same walk, same rows — every time."):
        "Same question, same walk, same rows. Every time.",
    ("TheWalk", "Yes — there is still a vector index here."):
        "There is still a vector index.",
    ("TheWalk", "It holds what can't be classified yet — never the source of truth."):
        "It holds what can't be classified yet — never the source of truth.",
    ("TheWalk", "@claims"):
        "One memory, across every model. Self-hosted, or hosted for you.",
    ("TheWalk", "@endcard"):
        ("FaultLine. Write-validated memory. By Volenti.", 1.0),
}
