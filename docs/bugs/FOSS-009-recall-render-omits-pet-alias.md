# FOSS-009 — recall surfaces an owned pet only by its type noun; the registered name-alias (Luna) never renders, so "what is my cat's name?" is unanswerable

- **Branch matrix:** es 761cf914 reproduced (renders type noun "Gata", alias never surfaces) · it 9f2724ac OK ("You have a pet that is Luna") · main c0fe9322 OK ("your cat's preferred name is luna") — the difference is the naming LANE: en files pref_name (rendered), es files the name as also_known_as (not rendered). The fix: es should file/prefer pref_name like en, or the renderer must promote name-aliases
- **Severity:** Medium (capture works, the read path drops the one fact the user asked for)
- **Discovered:** 2026-10-17, fresh-install functional gauntlet, es

## Symptom

    remember_facts("Mi gata se llama Luna y tiene 3 años.")
    -> status=valid; committed=1 (owns); aliases_registered=1 (luna);
       scalar_committed=2 (age=3); entity 48d885f7 (Animal) + also_known_as -> luna

    recall_memory("¿Cómo se llama mi gata?")
    -> {"memory": "Gata tiene 3 anos ... Less certain ...: You have a pet that is Gata"}

The answer names the TYPE NOUN ("Gata") twice and never the registered alias "Luna".
The fact needed to answer the question is IN the store (alias row + also_known_as edge)
and the recall walk reaches the entity — the render just never promotes the name.

## Root cause

The recall renderer prefers the entity's canonical/type-noun label and does not surface
name-aliases for owned/pet entities (the also_known_as edge exists; the walk visits it for
grounding but the render omits it). Note this is the FOSS visible face of the known
saas-side residual "pet named-instance capture is not on the spine path yet" — on FOSS the
capture DOES land, so only the render is missing.

## Reproduce

Fresh install -> remember the Luna sentence in the install language -> recall "what is
my cat's name?" -> answer contains the type noun, not Luna.

## Fix direction (gauntlet work order)

1. In the recall render, when a grounded entity carries a name-alias (also_known_as with a
   proper-noun value, or pref_name), surface it as the entity's HEAD label — the type noun
   becomes the descriptor ("your cat Luna, 3 years"), not the identity.
2. Pin: the exact battery above asserts the recall text contains "Luna" and answers a
   name-question with the alias.
3. Sweep sibling renders (dashboard entity cards) for the same type-noun preference while
   in the file — same fix, one commit.
4. **You are NOT your own critic.** When green, request a clean critic agent; the critic
   re-runs the battery in all three install languages on fresh stacks and runs
   semgrep + codeql over the diff before passing.
