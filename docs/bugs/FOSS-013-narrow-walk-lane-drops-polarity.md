# FOSS-013 — the narrow walk lane drops polarity: a stored polarity='negated' fact renders as an AFFIRMATIVE assertion ("You use Puerto") at recall on the spine-shaped graph

- **Branch matrix:** shared walk code — reproduced live on the rebuilt es stack (foss-fix/es @ 139acc07) and present on main/it by code shape (untouched lane; fix must land on main + ports)
- **Severity:** High (the FOSS-005 family's user-visible promise is store-safe but READ-unsafe: after the intent fix finally lets "No uso el puerto 9004..." ingest via the spine, recall asserts the OPPOSITE of what the user said)
- **Discovered:** 2026-10-17, D-es round-3 critic live battery (seat 8888)

## Symptom

Store: exactly ONE Class-A row, (usar, polarity='negated', user_stated) — correct.
Recall "Uso el puerto 9004?" renders: **"You use Puerto"** — affirmative prose from a
negated row. The legacy-shaped graph (anchor entity "puerto 9004", dict carrying
temporal_status) renders correctly ("You does not usar Puerto") because ITS walk fact dict
carries the polarity field; the NARROW walk lane's fact dict (spine-shaped rows) has no
polarity column in the served shape, so the prose renderer has nothing to negate with.

## Root cause

Two walk shapes exist; only the legacy shape selects/projects polarity into the served
fact dict. The narrow (spine) shape — now the COMMON shape for fresh seats — omits it, so
the renderer treats every row as affirmative. The capture side is correct end to end; the
READ side is the gap.

## Fix direction (gauntlet work order)

1. The narrow walk lane must select/project the polarity column into the served fact dict
   (same field name the legacy lane uses) and the prose renderer must render the negated
   state from it (match the legacy lane's render behavior).
2. Pins: (i) es battery — remember the negation sentence, recall the port -> the response
   must NOT assert affirmative use (assert the negated render); (ii) en real-parse twin:
   "I never use port 9004 on any server." (adverbial-never now yields a scope after the UD
   negator arm) -> STATEMENT capture with polarity='negated' AND negated render at recall —
   this also MEASURES the en-never scope delta the round-3 D-es critic disclosed (measure,
   then either keep the un-gated lemma set with this pin as its contract, or scope the set);
   (iii) legacy-shape regression: the temporal-status graph still renders negated.
3. Port to es/it with the other engine ports.
4. **You are NOT your own critic.** Clean critic rebuilds a stack, re-runs the batteries,
   semgrep + codeql the diff.
