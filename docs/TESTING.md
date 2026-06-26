# Testing & Evaluation Approach

How FaultLine is validated. This describes the *methodology*; it contains no tenant
data, no real user ids, and no infrastructure detail.

---

## Two layers of testing

### 1. Unit & integration tests

The standard suite covers extraction, the WGM validation gate, classification, the
storage paths, traversal, and prose rendering:

```bash
pip install -e ".[test]"
pytest tests/ --ignore=tests/evaluation --ignore=tests/feature_extraction \
              --ignore=tests/model_inference --ignore=tests/preprocessing
```

A hard-won lesson is baked into how these are read: **clean-input unit tests can
lie.** A pattern that passes on a tidy sentence can still fail on real,
conversational input. So unit tests gate regressions, but behaviour is confirmed on
the live system, not in isolation.

### 2. End-to-end recall evaluation

Beyond unit tests, recall quality is measured end to end: ingest a body of
statements, then ask questions and check whether the right grounded facts come back.
This is where the **strong-ingest / lean-query** contract is actually proven — if a
fact doesn't come back, it's almost always an *ingest* problem (the fact was never
grounded), not a query problem.

---

## Determinism is the headline metric

FaultLine's core claim is **deterministic recall** (see
[ARCHITECTURE.md → Why this isn't RAG](ARCHITECTURE.md#why-this-isnt-rag-mechanically)).
So the most important thing to measure is not just "is the answer right once" but
**"is it right *every* time"** — same input, same rows, no flicker.

### The reader/judge problem

A naïve end-to-end harness grades with an LLM that composes an answer and a second
LLM that judges it against a gold answer. Both are **nondeterministic** and dominate
the score — a single run can swing widely from reader/judge variance alone, which
tells you nothing about whether capture and recall actually improved.

### The deterministic instrument

To cut through that noise, evaluation uses a **second, fully deterministic signal**
that bypasses the reader and judge entirely. Given a gold answer and the raw recall
output (the memory prose recall returns, *before* any LLM reader), it asks a simple,
inspectable question: **did the gold fact actually appear in the recall output?**

- It is a pure function over two strings plus a deterministic token extractor — no
  network calls, no model variance.
- Every hit reports *what* it matched on, so any verdict is human-auditable.
- It handles both entity-shaped golds (a content/proper noun must appear) and
  computed golds (e.g. a duration in days — either the phrase appears, or the raw
  dated events needed to compute it are present).

Same input → same verdict, every time. That isolates capture-and-recall quality from
reader/judge noise.

### Determinism gating (repeats = N)

Because flicker is the enemy, the deterministic pass is run **repeatedly** on each
question (e.g. repeats = 5). A result only counts as solid when it is **stable across
every repeat** — `N/N`, zero flicker. A "lucky" single run that happens to pass is
explicitly *not* treated as a pass. The bar is repeat-stable determinism, not a
best-of-N score.

This is what turned an apparent "10/10" into an honest one: an earlier run was a
flaky single-pass result; only after gating on `repeats × N/N` stability did a
genuinely repeat-stable result hold up — and the eventual fix was a determinism bug
in a metadata cache, not the model.

---

## How the harness executes

- **Recall is verbatim-bounded and runs through the live MCP path** — both ingest and
  query go through the real tools, so the harness measures the real system, not a
  mock.
- **Each question runs against a clean slate** — the evaluation tenant is wiped
  between questions (schema dropped, vector collection cleared, idempotency cache
  flushed) so one question can't contaminate the next.
- **Always print the full raw recall output and the underlying rows before blaming
  the reader or the model.** A wrong answer is almost always an ingest drop. Truncating
  recall and assuming content has cost real debugging time — don't.
- **Test against a freshly built deployment.** Stale images produce invalid runs.

---

## Principles for writing tests

- **Test with intent, aligned to the one change you made.** When a test exposes a
  *known* gap, resolve it — don't re-run to re-confirm it.
- **Per-fix checks are cheap and targeted; the full end-to-end suite is for
  comprehensive change-sets**, not every one-line edit.
- **Validate on the live system, not in isolation** — clean-input unit tests have
  repeatedly disagreed with real behaviour.
- **A capture miss is a bug in our extraction, never "unclassifiable."** The fix is
  to grow the engine (frequency-gated), not to enumerate a special case in code.

---

## Sanitization note

All example data in tests and evaluation must be neutral and fictional — no real
names, emails, family details, IP addresses, internal hostnames, deployment paths,
or tenant UUIDs. Evaluation runs against a throwaway tenant, never a real user's
store.
