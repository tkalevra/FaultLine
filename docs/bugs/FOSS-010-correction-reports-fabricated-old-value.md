# FOSS-010 — the correction lane fabricates the "old value" it reports: user said 10.0.1.5 (never stored), lane answered "lives_at=10.0.1.9 -> lives_at=10.0.1.10" on two of three branches

- **Branch matrix:** es 761cf914 n/a (failed earlier in the lottery, see FOSS-004) · it 9f2724ac REPRODUCED ("ip_address=10.0.1.9 -> ip_address=10.0.1.10") · main c0fe9322 REPRODUCED ("lives_at=10.0.1.9 -> lives_at=10.0.1.10")
- **Severity:** High (the tool reports a stored-past that never existed; the user's audit trail lies, and any UI rendering "old -> new" shows a fabricated previous value)
- **Discovered:** 2026-10-17, fresh-install functional gauntlet

## Symptom

On fresh it and en installs the original IP remember never landed as an attribute (en: extraction_degraded + retained; it: misrouted to a pref_name flip, FOSS-011). The subsequent correction nevertheless reported a SPECIFIC old value:

    en: "✓ devbox: lives_at=10.0.1.9 → lives_at=10.0.1.10"  (facts_superseded=1)
    it: "✓ devbox: ip_address=10.0.1.9 → ip_address=10.0.1.10" (facts_superseded=1)

The DB audit (en, exact): entity_attributes had NO 10.0.1.9 row (the final row is an INSERT,
created_at = correction second, provenance=user_corrected); entity_attributes_history EMPTY;
staged_facts EMPTY. The value 10.0.1.9 appears NOWHERE in any table — it exists only in the
correction lane's own re-extraction of the original sentence (its LLM mis-read 10.0.1.5),
and the same mis-read happened independently on two installs. facts_superseded=1 counts a
supersede of something that was never there.

## Root cause

correct_fact's scalar path trusts the correction extractor's re-derived old value as if it
were read from storage. Nothing reconciles the reported old value against an actual row
(entity_attributes / entity_attributes_history / staged_facts). When no row exists, the
lane invents the "before" half of its story and writes the new row — the write is fine, the
REPORT is fiction, and the supersede accounting is inflated.

## Reproduce

Fresh en or it install -> degrade/lose the original IP capture (a ReadTimeout does it on a
busy local LLM) -> issue the correction sentence -> observe "old=10.0.1.9" in the response;
SELECT across the tenant schema finds no 10.0.1.9 anywhere.

## Fix direction (gauntlet work order)

1. Ground the old value: correct_fact reads the prior row (by resolved entity+attribute
   precedence, then staged) and reports THAT; if no prior row exists, report old=None /
   prior=unknown — never the extractor's imagination.
2. facts_superseded must count only real row transitions (INSERT of a first value is not a
   supersede).
3. If the extractor's old value disagrees with a stored row, prefer the stored row and say
   so in the message.
4. Pins: (i) red-pin the battery above — no fabricated old value in any response;
   (ii) pin that first-value corrections report prior=None and superseded=0;
   (iii) pin the honest case (real prior row) still reports old correctly.
5. **You are NOT your own critic.** When green, request a clean critic agent; the critic
   re-runs the battery on a fresh en stack, greps every table for the fabricated value, and
   runs semgrep + codeql over the diff before passing.
