# FOSS-005 — a negation sentence is routed RETRACTION, the retraction fails, and the failed attempt MINTS an affirmative edge (junk entity + "You use Puerto" surfaced by recall)

- **Branch matrix:** es 761cf914 CORRUPTED (misrouted RETRACTION, failed, minted affirmative (user,usar,puerto); recall asserts "You use Puerto") · it 9f2724ac SAFE (misrouted CORRECTION but failed honestly, nothing stored) · main c0fe9322 SAFE (kept as STATEMENT with polarity preserved; recall renders "You do not use Port") — the corruption is es-specific; the no-side-effects-on-failure defect is common (es proved inserts happen on the failed path)
- **Severity:** High on es — silent corruption class: the store ends up asserting the OPPOSITE of what the user said, from an operation that reported failure
- **Discovered:** 2026-10-17, fresh-install functional gauntlet, es first

## Symptom

On a fresh es install:

1. remember: "No uso el puerto 9004 en ningún servidor." ("I do NOT use port 9004 on any server.")
   -> remember_facts returns status=failed, "Old fact not found for rel_type change: uses", isError=true.
2. The DB now contains a Class-A fact:  (user) -usar-> <entity type=unknown>  created at the same second.
3. recall "¿Qué puerto uso?" -> {"memory": "You use Puerto"} — the engine now ASSERTS the user uses "Puerto".

## Root cause

Two stacked defects in the retraction lane:

(a) The intent classifier reads "No uso..." as RETRACTION (arguably defensible — a negated present is retraction-adjacent) and routes to /retract/correct. That lane re-extracts server-side; its extractor parses the Spanish sentence and DROPS the negation, producing an affirmative usar edge. The es branch's own docs claim the spaCy language guard prevents exactly this ("finds NO negation dependency ... stored as an AFFIRMATIVE fact" — that guard only checks the model's LANGUAGE, and even with the correct Spanish model loaded, this lane loses the negation).

(b) The retraction operation then fails ("old fact not found") — but the freshly extracted affirmative edges are WRITTEN anyway. A failed destructive op must not have insert side effects. Nothing refuses the junk entity (type=unknown, noun "puerto") and nothing marks the write as originating from a failed retraction.

## Reproduce

Fresh es install -> remember_facts("No uso el puerto 9004 en ningún servidor.") -> observe failed response -> SELECT ... FROM facts WHERE rel_type='usar' -> row exists -> recall_memory("¿Qué puerto uso?") -> "You use Puerto".

## Fix direction (gauntlet work order)

1. Atomicity: the /retract/correct lane must apply extraction findings ONLY if the overall operation succeeds; on the not-found path, roll back / never commit newly minted entities and edges (or gate all inserts behind the supersede decision).
2. Negation honesty in that lane: no edge may be emitted from a sentence whose parse carries sentential negation on the predicate (reuse the linguistic layer's negation detection — with the CORRECT language model this signal exists; assert it is consulted on this path specifically).
3. Junk-entity guard: an entity with type=unknown minted from a negated/retracted sentence must not survive the operation.
4. Pins: (i) red-pin the exact sentence above — assert NO (user, *usar/use*, puerto) edge exists after the call and recall never returns "You use Puerto"; (ii) pin that a failed retraction leaves entity/fact counts unchanged; (iii) pin the affirmative-control sentence ("Uso el puerto 9004") still captures normally.
5. **You are NOT your own critic.** When green, request a clean critic agent: critic re-runs the battery on a fresh stack, runs semgrep + codeql over the diff, and specifically hunts for OTHER failed-path insert side effects in the same lane before passing.
