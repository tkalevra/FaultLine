# FOSS-008 — plain-language retraction retires an UNRELATED entity's attribute: "forget my cat Luna" destroyed the server's IP

- **Branch matrix:** es 761cf914 WRONG-ENTITY destruction (cat request retired servidor.has_ip, response even named subject_name="servidor") · it 9f2724ac right-target-but-incomplete (retired the user's has_pet edge; the pet still renders from the naming lane) · main c0fe9322 WRONG-ENTITY destruction (cat request retired junk entity "any server".has_port) — three installs, three different outcomes for the SAME sentence shape: the lane is ungrounded and unreliable everywhere
- **Severity:** Critical (silent destruction of user truth by the forget path; outcomes vary by language/entity set, which makes it worse, not better)
- **Discovered:** 2026-10-17, fresh-install functional gauntlet, es

## Symptom

Fresh es install, tenant holds exactly: gata(Animal, age=3, alias Luna), servidor/DevBox (has_ip=10.0.1.10).

    retract_fact("olvida que tengo una gata que se llama Luna")
    -> {"status": "corrected", "subject_uuid": "ff64ab01...", "subject_name": "servidor",
        "old_rel_type": "has_ip", "message": "OK servidor: has_ip=None -> has_ip=None",
        "facts_superseded": 1}

DB after:

    entity_attributes: has_ip 10.0.1.10 superseded_at=16:05:57
    entity_attributes_history: action=retired, cause=retract_lane

The CAT is untouched; the SERVER's IP is retired. recall "¿Cómo se llama mi gata?" still
returns the pet; recall about DevBox's IP now returns nothing.

## Root cause

The plain-language retraction resolves a subject entity and applies a destructive retire
without verifying the resolved subject against the sentence it was given. With two entities
in the tenant it picked servidor/DevBox for a sentence containing only gata/Luna, then
retired that entity's only scalar. The response echoes subject_name="servidor" — the
information needed for a sanity check was in hand and unused. No confirmation gate exists
for ambiguous subject resolution, and the retirement is applied, not staged.

## Reproduce

1. Fresh install, remember the gata sentence and the DevBox IP sentence.
2. retract_fact("olvida que tengo una gata que se llama Luna") (English: "forget that I
   have a cat named Luna").
3. Observe corrected/facts_superseded=1 on servidor.has_ip; cat intact; IP gone.

## Fix direction (gauntlet work order)

> **Round-1 critic addendum (live, on a fixed branch):** a first cut that gates the SUBJECT at
> the retraction lane still lost on EN — the speaker-exempt (subject==user) plus the
> pre-existing SS4 "unique-holder" rung re-targets subject AND attribute AFTER the gate
> ("axis_scalar_via_stored_attribute … via=unique_holder"), so the cat sentence retired
> devbox.has_ip again; and "Forget that my unicorn has wings." (subject=user, object grounds
> nothing) superseded the user's LIVE (user, has_pet, luna) edge with a false corrected.
> The grounding contract must bind the FINAL (subject, attribute) after every post-gate
> re-target, and the RELATIONAL leg must sentence-ground the object/value too — a fact whose
> object appears nowhere in the sentence is never a retraction target. Both wrong writes were
> ledgered actually_retracted=t/was_correct=t — the learning amplifier must see refusals, not
> reinforced destruction.

1. Subject-sentence agreement gate: every token/alias of the resolved subject (and of the
   attribute being retired) must be grounded in the input sentence; on mismatch, refuse with
   a no-match status and write NOTHING. The check is deterministic (string/alias overlap +
   the linguistic layer), not an LLM opinion.
2. Never hard-apply an ambiguous plain-language retract: if more than one entity is a
   plausible subject, return the candidates and retire nothing.
3. Retirement stays reversible and loud: keep the current history-row pattern
   (action=retired, cause=retract_lane) and add the resolved-subject id + sentence to the
   response so a wrong hit is visible in the tool result itself.
4. Pins: (i) the exact reproduction above — cat request must not touch servidor.has_ip
   (red today); (ii) property pin over N entity combinations: a retract mentioning only
   entity X never mutates entity Y; (iii) a true-positive pin: "olvida la IP de DevBox"
   still retires has_ip.
5. **You are NOT your own critic.** When green, request a clean critic agent; the critic
   fuzzes subject resolution (same battery, other entity pairs and languages) and runs
   semgrep + codeql before passing.
