# FOSS-016 — the lane-C stopword alias screen ships INERT on the it branch: _STOPWORD_ALIASES carries zero Italian entries

- **Branch matrix:** foss-fix/it (every head, incl. the ported f57f9b36) — foss-main-local and foss-fix/es carry their languages' entries and are covered
- **Severity:** Medium (the exact lane-C round-2 wound shape — a harvest-minted stopword alias ambiguity-locking retractions — is OPEN for Italian closed-class words on it: che, il, non, si can mint as entity aliases and lock out retraction agreement exactly like en's 'that' did)
- **Discovered:** 2026-10-17, owner-requested subject-agnostic/language-specific shape audit (set inspected on the ported head)

## Symptom

The lane-C discipline (gate-side screen + StopwordSurfaceError mint refusal + non-preferred registration refusal) is sound MECHANISM, but its data set is a hand list labelled 'English closed-class' + 'Spanish closed-class (the es battery shares the same seams)' — **no Italian section**. The it stack runs the same seams with an inert screen for its own language.

## Shape defect (the honest part)

A hand-maintained per-language word list in code is the exact anti-shape the house doctrine forbids (lexical decisions ride DB-grown flagged cue classes or grammar — never in-code zoos). Closed-class function words are a linguistically CLOSED set (they cannot accrete domain content the way an emotion/preference zoo does), which is why the mechanism is acceptable — but the it hole proves the hand list does not scale across language branches.

## Fix direction (gauntlet work order)

1. PRIMARY SHAPE FIX (online-validated): consult the parsed tokens' native stop attribute — spaCy ships per-language stop lists and exposes tok.is_stop ('Is the token part of a stop list, i.e. the most common words of the language?'). The screen becomes language-native by construction (the loaded model IS the language), keeps the registry set as a floor for the no-model path only, and the it hole closes itself (it_core_news_sm's stop list ships with the model).
2. If the model-free path must stay exact: add the Italian closed-class section (che, non, il, lo, la, gli, le, un, uno, una, e, od, o, ma, pero, si, anche, come, per, con, questo, questa, questi, queste, mio, mia, tuo, suo, sono, era, essere, ho, hai, ha, abbiamo, molto, gia, gia_ampersand, anche) as the FLOOR, not the fix.
3. Pins: an it stopword surface (che) is refused at mint AND cannot lock out an agreement; a content-bearing it surface still mints; en/es behavior byte-identical to today.
4. **You are NOT your own critic.**