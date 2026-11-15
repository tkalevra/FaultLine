# FOSS-012 — es branch: plain affirmative statements route to CORRECTION (GLiNER2 destructive lean, gate-passed); the capture silently lands nothing and the user reads an unrelated correction error

- **Branch matrix:** es 761cf914 REPRODUCED live (and on the fixed lane branch foss-fix/es — base-identical, not a fix regression) · it 9f2724a not reproduced with the it sentence set · main c0fe9322 not reproduced with the en sentence set
- **Severity:** High on es (a normal statement about the user's own setup is silently NOT captured; the response is an unrelated "Incomplete correction" error — the user's mental model of "it remembered" is broken in both directions)
- **Discovered:** 2026-10-17, round-1 critic live battery on the rebuilt fl-eval-es stack (FOSS-005 fix verification)

## Symptom

    remember_facts("Uso el puerto 9004 en el servidor principal.")
    -> {"status": "failed", "message": "Incomplete correction — relationship type not identified",
        "isError": true}          # NOTHING captured; recall -> "I don't have any information about that"

Logs (verbatim shape): GLiNER2 labels the sentence with the CORRECTION-verbal label at
confidence 0.845 -> gate 0.70 PASSES -> layer=gliner2_native -> /retract/correct. The
statement lane never runs. The identical-shape English sentence ("I use port 9004 on the
main server.") classifies STATEMENT and captures (user, use, port) normally.

## Root cause

The es zero-shot intent label set over-scores a destructive lean on first-person present
use/possession statements ("uso", "tengo", "tiene"). The per-user confidence gate passes it
(0.845 > 0.70) and the destructive route fires before any grammar evidence is consulted.
Related but distinct from FOSS-005 (negation) and FOSS-011 (it naming): same family —
the es/it destructive lean wins on shapes en classifies STATEMENT — but this one is
gate-passed confidence, not a sub-gate escalation, and affirmative.

## Reproduce

Fresh es seat -> remember the sentence above -> status failed/correction, zero edges;
recall the port -> empty. (Critic evidence seat 33333333-… on fl-eval-es, image 715e5911….)

## Fix direction (gauntlet work order)

1. First-person use/possession guard on the destructive route: when the turn is a
   declarative first-person present-tense assertion with no prior of the guessed
   subject/attribute (nothing to correct — "a correction never mints"), route STATEMENT.
   The FOSS-011 first-mention guard and the FOSS-008 "no stored bearer" discipline are the
   same principle — apply the shared guard at the classify/lane seam, not per-language.
2. es intent label audit: re-score the es label strings against the concise-label bench
   (PITFALL 11 house rule: whole-label replacement benchmark-gated, never accretion) — the
   es label set demonstrably over-leans destructive vs en on the same shape.
3. Pins: (i) red-pin the sentence — STATEMENT classification + capture; (ii) keep a true es
   correction green; (iii) parity: en twin STATEMENT.
4. **You are NOT your own critic.** Clean critic re-runs the battery on a fresh es stack;
   semgrep + codeql the diff.

## Re-measurement @ 74e03fd7 (2026-10-18, FOSS-012 work order, STEP 1)

End-to-end on a FRESH seat (c28d095f-e50c-413a-9be8-e6fafab0bace) on the live
fl-eval-es stack (finger-printed byte-identical to 74e03fd7 on main.py / linguistics.py /
server.py / linguistic_cue_overlay.py before probing):

- `remember_facts("Uso el puerto 9004 en el servidor principal.")` → `status: failed`,
  `message: "Incomplete correction — relationship type not identified"`, `isError: true`,
  `retention: "confirmed"`. The user is told, in effect, that their correction failed —
  for a turn that was never a correction.
- DB evidence: the turn is retained VERBATIM TWICE in `episodic_log` — row 1
  `source=mcp, intent=CORRECTION` (the misrouted turn) and row 2
  `source=store_context_deferred` (the Class-C residue lane, a NON-elevatable origin) —
  with **zero queryable rows**: `facts=0`, `staged_facts=0`.
- `recall_memory` of the port: **MISS at every poll through the full 10-minute measured
  window** (+5s / +30s / +90s / +180s / +360s / +600s) — the user experience is the
  original wound's "silently not captured" (no Class-C echo either, because the residue
  lane staged nothing).
- The only recovery path is the hourly `reextract_episodic` re-mine
  (`created_at < now() - INTERVAL '1 hour'`), i.e. the capture can land **no earlier than
  ~1 h after the turn**, and only via the re_embedder cycle reaching the seat. The
  durability net (retention + re-mine) is a backstop, NOT a fix: the turn still ANSWERS
  as a failed correction. (Long-poll measurement of the >1 h re-mine: see the live
  section below.)

## Fix shipped (work-order STEP 2, commit 537955a5 on foss-fix/es)

The FOSS-005 veto discipline, affirmative twin, at the same seam
(`classify_intent`, hoisted above gate + escalation routing so BOTH routes are bound):

- **The decline:** a gate-passed CORRECTION lean whose turn is the FIRST-PERSON
  PRESENT-TENSE AFFIRMATIVE DECLARATIVE shape (grammar: `Person=1` read on the finite
  predicate — the Spanish pro-drop agreement "uso"/"tengo"/"soy" — OR an explicit
  `Person=1 PronType=Prs` subject; `Tense=Pres`; non-interrogative) AND which carries
  ZERO correction-shape evidence demotes to STATEMENT. Zero evidence = the SAME closed
  screens every destructive route here already requires: no imperative removal-verb cue,
  no temporal-cessation adverbial, no contrastive old-vs-new negation, no clause-initial
  repair marker, no "I meant" repair verb, no repair FRAME (`_repair_frame`), no
  tenant-grown `correction_signals` literal (`_turn_matches_grown_correction_cue`), no
  DB-admitted lexically-expressed removal (`_cessative_shape_admitted`, mirroring
  `_spacy_cue_route`'s admission), and NO negation scope anywhere (affirmative — the
  negated twin stays the FOSS-005 veto's lane; disjoint by the scopes screen).
- **Grammar only, no new word zoo:** `linguistics.analyze_first_person_present_statement`
  returns pure `PersonTenseShape` morphology facts (ROOT VERB/AUX, or the `cop` AUX of a
  Spanish pro-drop copular clause; language-neutral by morphology — the en twin
  `"I use port 9004"` matches via the explicit-subject arm). GLiNER2 stays frozen
  (Pitfall 11). Flag: `AFFIRMATIVE_STATEMENT_DECLINE` (default ON, documented escape
  hatch). New intent layer name: `affirmative_statement_decline`.
- **Scope is CORRECTION only:** a RETRACTION lean is untouched; every
  marked/contrastive/negated/removal shape keeps today's route. A genuine es correction
  ("En realidad la ip es X" — the predicate agrees 3rd person; the anzi-shape
  old-vs-new contrast) can never match the decline shape.
- **Migration 293** (seed-only, idempotent, both stores live-read ENGLISH-ONLY/EMPTY on
  the es stack before seeding): `correction_signals` es literals (en realidad, en cambio,
  espera, perdón/perdon, disculpa, quería decir, me equivoqué/me equivoque, mi error,
  no es verdad, no es cierto — each mirroring a 032 en row) and `linguistic_cues`
  `cessative_verb` es members dejar/abandonar/cesar (SIL cessative aspect, Freed 1979;
  shapes ride the description column, 272 convention). **parar is deliberately NOT
  seeded — CORRECTED (critic round 2):** the en floor does NOT exclude "stop"; it seeds
  stop with the xcomp_progressive-ONLY shape, i.e. polysemy is handled by shape
  restriction, not exclusion. The shape rail could disambiguate parar the same way
  ("paro de fumar" vs "paré el coche"), and seeding `parar=xcomp_progressive` is the
  stop-mirroring option — an explicit OWNER CALL, not silently taken here: the es
  removal turns observed on this stack express cessation through dejar/abandonar/cesar,
  and the sm parser's unstable ROOT choice adds pure surface risk with no observed
  removal to admit. The migration's header comment now states this corrected rationale.
  Without the cessative rows the decline would have demoted "dejo de usar el puerto X"
  (an evidence-free-shaped removal); with them the admission screen keeps it out of the
  decline.
- **Pins (47 passed / 11 skipped in the no-model env):** the battery sentence facts
  decline → STATEMENT; the genuine 3rd-person es correction never declines (classification
  pin + endpoint pin); RETRACTION leans untouched; marked/grown-cue/cessative/contrast
  shapes all keep the correction lane; the negated twin stays the veto's lane
  (disjointness asserted); sub-gate binds BEFORE the escalation LLM is paid for; the
  flag-off escape hatch restores the legacy route; fail-safe (no grammar facts → no
  demote). RED-CHECK at 74e03fd7: the new pin file fails at COLLECTION (the decision,
  analyzer, flag and layer do not exist at base). The two FOSS-005-file pins that had
  frozen the OLD affirmative contract ("the escalation owns this band until FOSS-012
  lands") are flipped to the new contract with the reversal documented in place; every
  negated-shape pin is untouched.
- Adjacent suites green (es language gates, es naming, correction/retraction pins,
  model parity, boot migrations incl. 293, cessative rails). The 3
  `test_cessation_deriver_characterization` failures are BASE-IDENTICAL at 74e03fd7
  (model-dependent characterization in a no-model env — pre-existing, not this fix).

## LIVE VERIFICATION (work-order STEP 4, 2026-10-18, stack rebuilt from d30ff08f)

fl-eval-es rebuilt from the fix commit `d30ff08f` (the work order recipe: git archive →
dind `/srv/FaultLine`, `.env` preserved — md5 `d2f852a3…` byte-identical through the
rebuild — `docker-compose up -d --build`). FINGERPRINT before testing: all five changed
files md5-identical between the commit and the RUNNING image (main.py `571d952c…`,
linguistics.py `34e11414…`, server.py `90e66d78…` unchanged, overlay `69b901b1…`
unchanged, migration `7ba88811…`). Boot ran migration 293 (per-tenant NOTICEs in the
api log); live read after boot: `public.correction_signals` 29 rows (12 en + 17 es),
`linguistic_cues` cessative_verb carries dejar/abandonar/cesar. Note: the dind lease
moved 172.16.0.6 → 172.16.0.13 mid-round and the staged MCP key predated the current
`.env` (43-byte key, sha-mismatched) — re-staged from the live env before probing.

Battery on fresh seats (attempts honest; the dind restart makes the first remember
cold — S1 took 88.3 s, 1 attempt, under the 240 s client timeout):

1. **THE battery sentence** `remember_facts("Uso el puerto 9004 en el servidor
   principal.")` → **`status: valid, committed: 1`** — the pre-fix answer on this exact
   turn was `failed / "Incomplete correction — relationship type not identified"`. DB:
   `facts` row `(user, uses, port 9004)` — `rel_type_definition "user uses port 9004"`,
   `fact_class A`, `fact_provenance user_stated`, `polarity affirmed`, in-session.
   Log: `classify_intent.layer_decision intent=STATEMENT layer=affirmative_statement_decline`.
   The decline ALSO fired live in the sub-gate band on a second seat
   ("Tengo un gato que se llama Bigotes." scored GLiNER2 CORRECTION@0.642 → declined to
   STATEMENT **before the escalation LLM was paid for** — the pre-fix contract would have
   escalated): `AFFIRMATIVE-STATEMENT DECLINE: GLiNER2 CORRECTION@0.642 → STATEMENT`.
   **Scope note (critic round 2): the sub-gate decline is proven WHEN THE LEAN IS
   CORRECTION — twice, live (0.563 and 0.642) — not unconditionally**; a RETRACTION lean
   at any band keeps the retraction lane (pinned, and live at 0.98).
   Recall caveat (adjacent, honest): recall queries with bare "puerto" ground to a
   DIFFERENT entity than the captured "port 9004" alias (the es recall-aliasing gap —
   "cual es mi puerto?" renders "You own Puerto"); the capture row itself is reachable
   via its "port 9004" alias. This is a query-side es lexicon gap, not the capture.
2. **Genuine anzi-shape correction** "En realidad la ip es 10.0.0.5." → routed
   **CORRECTION** (the decline correctly did not touch it: 3rd-person copular + marker).
   It answered failed-only because its SEED had not stored (see residual below), so
   there was no stored value to supersede **in THIS battery — claim hygiene (critic
   round 2): the anzi-correction supersede over STORED facts was proven live in earlier
   es rounds, not here**; this battery proves only that the decline leaves the genuine
   correction lane untouched.
3. **Genuine retraction** "Olvida a mi gato Bigotes." → GLiNER2 **RETRACTION@0.98**
   `layer=gliner2_native`, routed the retract lane (`status: corrected,
   facts_superseded: 1`) — RETRACTION leans are untouched by the decline, as pinned.
4. **FOSS-005 destructive-lean regression** "No uso el puerto 9004 en ningún servidor."
   → veto fired live: `negated_statement_veto gliner2_confidence=0.524 CORRECTION →
   STATEMENT (polarity preserved at ingest)`; captured `(user, usar, port 9004)`
   **`polarity = negated`, Class A, user_stated**. Green.
5. **En-twin parity** "I use port 9004 on the main server." → **STATEMENT** — a direct
   `/classify-intent` probe (backend-cred door), not the MCP path. **Claim hygiene
   (critic round 2):** the es-stack MCP path wobbles (a CORRECTION-native gate-passed
   landing CAN come back queryable in the same session's logs); the invariant that
   matters is **never-eaten — the user's affirmative statement is never answered as a
   failed correction and never destroyed** — and that invariant held in BOTH runs.

**Honest residuals measured live (out of FOSS-012 scope, filed for the critic):**
- `Uso la ip 10.0.0.9 en el servidor principal.` — `es_core_news_sm` roots this variant
  on **"servidor" NOUN** (not the initial "Uso"), so the shape analyzer fails safe to
  None, the decline skips, the turn escalates and stays CORRECTION → still answers as a
  failed correction. The sm ROOT choice is unstable across near-identical sentences
  ("Uso el puerto 9004…" roots "Uso"); a root-anchored grammar can only see what the
  parser roots. The canonical battery sentence and the gato sentence both decline.
- The ip SEED itself ("La ip del servidor principal es 10.0.0.9.") misroutes to
  CORRECTION on GLiNER2 (third-person; deliberately outside the decline's shape) — the
  same adjacent third-person es wound, unchanged by this fix.
- The se-llama naming construction ("Tengo un gato que se llama Bigotes.") still
  self-loop-drops at the DERIVER on sm ("bigotes instance of gato") even after the
  decline routes it STATEMENT — capture-side es-model weakness, pre-existing.
- STEP 1's ">1 h re-mine" backstop was observed only as continuous recall MISS through
  a ~75-minute window post-turn (polls every 5 min); the rebuild (required by the work
  order) ended the observation before the `created_at < now()-1 h` gate could open.
  The durability net is documented; the fix removes the user-visible dependency on it.

**VERDICT: READY FOR CRITIC.** Fix live on fl-eval-es @ d30ff08f (fingerprinted),
battery green on the wounded turn, both sibling lanes (retraction, negated veto) and
the en twin verified unaffected, seats cleaned (only the 3 demo tenants remain).


## Critic round 2 — fix-up (test/doc only; no production code touched)

- **NIT 1 folded:** the migration-293 header comment and this doc corrected — the en
  floor does NOT exclude "stop"; it seeds `stop` with the xcomp_progressive-ONLY shape
  (polysemy handled by shape restriction). parar stays UNSEEDED as an explicit owner
  call (mirroring stop would be `parar=xcomp_progressive`); the comment-only migration
  edit is verified comment-only (0 non-comment diff lines).
- **Three test defects fixed** in `tests/test_es_affirmative_statement_decline.py`:
  (1) `_classify` now stubs the shape analyzer UNCONDITIONALLY, so
  `test_no_shape_facts_falls_through_to_the_legacy_route` proves the no-shape
  fall-through in BOTH envs (under es-md the real analyzer no longer sneaks a shape
  into the "no shape" pin); (2) `test_en_twin_real_parse_carries_the_subject_shape`
  gained an `_en_real` gate (skip-with-reason under an es-model process, not red);
  (3) `test_es_real_parse_negated_twin_still_vetoes_not_declines` now reads the layer
  stats under the key the classifier actually writes (`user_id[:8]` = `foss012-`).
  The two sibling en-twin pins in `test_es_negated_statement_intent.py` carried the
  same en-model assumption and got the same skip-with-reason gate.
- **Measured after the fix-up:** default (no model) **49 passed / 12 skipped** (held);
  `SPACY_MODEL=es_core_news_md` **0 failed** — combined 58 passed / 3 skipped (the 3
  skips are the en-model pins, skip-with-reason); the target file alone 34 passed /
  1 skipped. (The round-2 brief's "52 passed" estimate assumed different collection
  arithmetic; the invariant demanded — 0 failures under es-md, 49/12 held by default —
  is met.)
- **Red-check re-run:** `git checkout 74e03fd7 -- src/api/main.py
  src/extraction/linguistics.py` → the pin file still **fails at COLLECTION**; restored
  to d30ff08f src afterward.

