# FOSS-015 — it branch: pro-drop correction/retraction turns cannot ground their bearer; split-turn renames park at a loud failure (round-1's silent corruption is gone; completion is not)

- **Branch matrix:** it 9f2724a + foss-fix/it @ aaeb9429 (live) · en/es N/A (their shapes name the subject; the pro-drop shape is it-typical)
- **Severity:** Medium (loud, lossless, honest — but the flagship split-turn rename does not complete: apollo stays preferred, devbox never supersedes; the MCP-path retraction of a pro-drop shape parks at clarification_needed)
- **Discovered:** 2026-10-17, D-it round-2 critic live battery on the rebuilt it stack

## Symptom (after FOSS-011's round-2 fix)

    b1: "Il mio server si chiama Apollo."   -> STATEMENT, owns + pref_name apollo captured
    b2: "Anzi, si chiama DevBox."           -> routes CORRECTION (preroute declines hold:
        pro-drop empty bearer, anzi screen, stored-bearer gate) BUT the corrector's LLM
        returns subject=null on the pro-drop turn -> "Old fact not found for subject
        change: pref_name", facts_superseded=0, deterministic (twice). Recall: "You own Apollo".
    retraction: "dimentica che ho una gatta che si chiama Luna." -> clarification_needed
        (candidates listed, confirm asked), nothing written. The DIRECT intent=RETRACTION
        call DID supersede — the lane works when the TEXT grounds a subject.

Round-1's wounds (silent wrong writes, orphan entities) are gone; the failure is loud and
lossless. What is missing is bearer GROUNDING for pro-drop turns.

## Fix direction (gauntlet work order)

1. Ground the pro-drop bearer in the correction/retraction lanes (either or both):
   (a) feed the discourse prior-NP (the session reference machinery the spine already uses)
   to correction_extraction as a subject hint on turns whose subject resolves null;
   (b) when subject is null, match stored pref_name/alias rows by the turn's naming content
   (old_value/alias overlap) and offer the resolved bearer — never mint, never guess into a
   write without the agreement gate.
2. Pins: split-turn supersede LANDS (apollo->devbox pref_name on the SAME entity, recall
   shows DevBox); the pro-drop retraction completes (or clarifies with the bearer named in
   the candidates) — assert facts move or the clarification NAMES the bearer; all FOSS-011
   round-2 pins stay green.
3. Red-check at aaeb9429. **You are NOT your own critic.**
