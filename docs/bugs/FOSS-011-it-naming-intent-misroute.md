# FOSS-011 — it branch: naming a NEW thing ("Il mio server principale si chiama DevBox, il suo IP è 10.0.1.5") is classified CORRECTION; the lane mints an entity and destructively flips its pref_name instead of capturing the statement — the IP is never captured

- **Branch matrix:** it 9f2724ac REPRODUCED twice (both attempts) · es 761cf914 does NOT show it ("se llama" captured as STATEMENT, owns+alias+IP) · main c0fe9322 does NOT show it ("is called" captured as STATEMENT)
- **Severity:** High on it (the README's flagship remember scenario silently loses its data; the user sees "corrected" and believes it worked)
- **Discovered:** 2026-10-17, fresh-install functional gauntlet, it

## Symptom

    remember_facts("Il mio server principale si chiama DevBox, il suo IP e 10.0.1.5.")
    -> {"status": "corrected", "subject_name": "server",
        "old_rel_type": "pref_name", "old_value": "server",
        "new_rel_type": "pref_name", "new_value": "devbox",
        "facts_superseded": 1, "committed": 0}

No owns edge, no IP scalar, no devbox alias from the naming chain — the statement lane never
ran. The tenant acquired a "server" entity (minted by the correction path itself) renamed to
devbox. Recall for the IP before the later correction step: honest-empty (the data is gone,
and only the retained-turn re-mine eventually resurfaced it via a different lane).

## Root cause

The it intent pattern set classifies "X si chiama Y" as CORRECTION (a naming correction)
rather than STATEMENT-with-naming. The correction lane then has no real prior to protect —
it mints the subject ("server"), flips pref_name, and reports success. The classification is
deterministically wrong for FIRST mentions; "si chiama" is both the naming pattern AND
threshold-adjacent to correction patterns, and on it the correction reading wins.

## Reproduce

Fresh it install -> remember the sentence above (twice, to rule out LLM noise — it is
deterministic) -> observe status=corrected with pref_name flip and committed=0; recall the
IP -> empty.

## Fix direction (gauntlet work order)

1. First-mention guard: when the correction lane's resolved subject has no prior naming
   fact (nothing to supersede) AND the sentence matches the naming pattern, route back
   through the STATEMENT path (or at minimum capture the relational/scalar content in
   addition to the pref_name flip).
2. it intent patterns: "si chiama" must not out-score STATEMENT on first mention — align the
   it correction_patterns with the es/en behavior (measured: es and en both classify the
   equivalent sentence as STATEMENT).
3. Pins: (i) red-pin the exact sentence on it — assert STATEMENT classification (or
   statement-lane capture) with owns + alias + has_ip landing in one call; (ii) keep a
   true it naming-correction pin ("il mio server si chiama X... no, si chiama Y") green;
   (iii) parity pin: the same battery sentence classifies STATEMENT on all three branches.
4. **You are NOT your own critic.** When green, request a clean critic agent; the critic
   re-runs the battery on fresh it AND es stacks, runs semgrep + codeql over the diff, and
   reconciles the intent-pattern diffs across the three branches before passing.
