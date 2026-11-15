# FOSS-000 — FOSS fresh-install gauntlet index (2026-10-17): method, environment, verdicts, and how to reproduce every number

This index summarizes a full documented-path install + functional gauntlet over the three
public FOSS branches, run EXACTLY as a human would (clone from GitHub, run the wizard,
answer its prompts, let it build and boot, then drive the memory through the same REST door
the wizard's own integration guide prints).

## Method (per branch)

1. Throwaway docker:dind on a lab Linux host (fresh /var/lib/docker volume per branch —
   nothing shared, nothing pre-warmed).
2. apk add git python3 (the dind's own minimalism; the repo's documented prereqs are
   Docker+Compose and Python 3.8+).
3. git clone https://github.com/tkalevra/FaultLine.git (9 s, 6.5 MB with .git).
4. ./setup.sh (documented primary command; on the dind: python3 quickstart.py — see FOSS-003).
5. Wizard answered exactly as a user would: language -> LLM backend -> URL/key -> model
   (from the wizard's own connect-and-list) -> built-in embedder -> generated MCP key ->
   generated tenant id -> default resource names -> "build + start now".
6. Functional battery over POST :8002/<tool> with the wizard-generated MCP_API_KEY and
   X-OpenWebUI-User-Id: remember x2, recall x2, correction, re-recall, negation,
   negation-recall, plain-language retract, retract-recall. Same battery shape in the
   branch's language (Spanish/Italian/English).

LLM backend for the battery: a local llama.cpp server (Qwen2.5-7B-Instruct Q4_K_M, CPU)
so no hosted quota/UA quirks can confound the engine observations. A hosted OpenAI-compatible gateway endpoint
named by the lab stack was used for the UA finding (FOSS-001) before its quota
402'd — that 402 itself demonstrated the honest degradation path (edges withheld, turn
retained for re-extraction, CRIT logs).

## Environment facts (for every reproduce)

- Heads under test: es 761cf914, it 9f2724a, main (english) c0fe9322 (public repo heads).
- dind storage per branch: one throwaway volume per branch.

## Measured verdicts

| Question | es | it | en/main |
|---|---|---|---|
| Wizard completes | YES (after FOSS-001 workaround) | YES (happy path: connect+list worked against a non-filtering endpoint) | YES (happy path) |
| Build succeeds | YES (~11 min) | YES (~13 min) | YES (~12 min) |
| First boot to healthy | ~9.9 min | ~8.5 min | ~9 min |
| remember works | YES (pet+alias+age; IP scalar; one GLiNER2 Person-mistype refusal) | PARTIAL — "si chiama" naming sentence misrouted to CORRECTION, relational+IP capture lost (FOSS-011) | YES (cat pref_name + age; second turn degraded by LLM contention, honestly retained) |
| recall works | YES for IP; pet NAME missing (FOSS-009) | YES (IP exact after correction; pet renders with name) | YES ("your cat's preferred name is luna") |
| correction works | DATA yes / RESPONSE failed (FOSS-004) | YES status=corrected, recall correct — but old value FABRICATED (FOSS-010) | YES status=corrected, recall correct — old value FABRICATED (FOSS-010) |
| plain retract safe | NO — retired the SERVER's IP for a cat request (FOSS-008) | PARTIAL — retired the user's has_pet edge (right target) yet the pet still renders | NO — retired junk entity "any server".has_port for a cat request (FOSS-008) |
| negation safe | NO — misrouted RETRACTION, failed, minted affirmative (user,usar,puerto) junk (FOSS-005) | YES — failed honestly ("relationship type not identified"), nothing stored | YES — captured with polarity preserved; recall renders "You do not use Port" |
| Image size | 2.76 GB | 2.12 GB | 1.99 GB |
| Fresh install disk (total) | 15.8 GB (2.9 images + 4.5 build cache + volumes) | same pattern | same pattern |
| Bloat/garbage in image | NONE material (no CUDA wheels, no tests/.git shipped; 3.4 MB pip cache residue) | none material | none material |

Common shape (why one fix lane can serve several branches): the install surface
(quickstart.py, setup.sh, README, Dockerfile) is near-identical across the three branches,
and every functional failure traced into the SAME shared engine lanes (/ingest,
/retract/correct, re-embedder) — the language branches differ only in the parse/intent
patterns feeding those lanes. Fix the lanes once; port only the language-specific pins.

The prior external report of a ">150 GB" FOSS footprint DID NOT REPRODUCE on the
documented path — see FOSS-006 for the measured matrix and the documentation gap that
made the report unverifiable.

## Bug docs in this folder

- FOSS-001 quickstart probe UA 403 (common)
- FOSS-002 es spaCy model default contradiction (es-only)
- FOSS-003 setup.sh bash shebang (common)
- FOSS-004 correction reports failed on success (common engine lane)
- FOSS-005 negation -> failed retraction mints affirmative junk (common engine lane)
- FOSS-006 README stale model-download claim + undocumented disk footprint (common)
- FOSS-007 first-boot timing vs docs/wizard/healthcheck budgets (common)
- FOSS-008 plain-language retract wrong-entity destruction (common engine lane)
- FOSS-009 recall render omits pet name-alias (es render lane)
- FOSS-010 correction lane fabricates the "old value" it reports (en + it)
- FOSS-011 it intent misroutes first-mention naming sentences to CORRECTION (it)
- FOSS-012 es affirmative statements route to CORRECTION gate-passed, capture silently lost (found by the round-1 D-es critic's affirmative control)
- FOSS-013 narrow walk lane drops polarity -> recall asserts affirmative from a negated row (found by the round-3 D-es critic; store-safe, read-unsafe)
- FOSS-014 verbatim re-statement after retraction does not resurrect; paraphrase does (found by the round-3 C critic; dedupe path treats user truth as a replay)
- FOSS-015 it pro-drop correction/retraction turns cannot ground their bearer — split-turn renames park at a loud failure (round-1's silent corruption fixed; completion exposed)
- FOSS-016 the lane-C stopword alias screen ships inert on it: _STOPWORD_ALIASES has no Italian entries (found in the owner-requested shape audit; recommended shape: spaCy tok.is_stop)

## Working rules for every fixing agent (applies to all FOSS-* docs)

- You are NOT your own critic. When your work is green, request a clean critic agent.
- Critics run semgrep AND codeql over the diff, re-derive at least one claim against the
  live stack or upstream docs (online tooling allowed), and must reproduce at least one
  red-check (the bug) and one green-check (the fix) themselves.
- Every fix lands with its pin test force-added (tests/ is gitignored wholesale: use
  git add -f tests/<file>.py and verify git show --stat lists it).
- One lane per bug doc; no lane touches another lane's files without saying so in its
  report.

## Fix lanes (2026-10-17) — commits and critic verdicts

Four worker lanes committed fixes on local branches foss-fix/{main,es,it} (never pushed).
Four independent clean critics judge them (semgrep + codeql + live-stack rebuild + red-checks):

| Lane | Worker commits | Critic round 1 |
|---|---|---|
| B install surface (001/003/006/007) | main bb2445c8..714d5479; ports es 1f4e1911, it 9d184ec2 | **PASS** — live-proven (old-UA 403 Cloudflare 1010 vs new-UA 200 + model list with the real key); 7/7 tautology mutations red; ports faithful (setup.sh + pins blob-identical across branches, per-language values intact); static: zero new security findings. Flags (non-blocking): the 403-wording regex matches a body containing "Forbidden" (nginx default page would read as auth) — provider body surfaced either way; the 16 GB headline traces to du on ZFS while docker system df components sum to ~7.5 GB (peak-vs-accounted gap, README errs safe; noted in FOSS-006). |
| C correction/retract engine (004/008/010/005-atomicity) | main e4de56b5..26f38fd5, rework c42ed6f1, round-3 557da006 | R1 **FAIL**, R2 **FAIL** (one item + stopword lockout), R3 **PASS** — all three swap seams guarded fail-closed; stopword lockout closed both ends (live lockout re-test lands honestly); wording honest; static set-diff empty; live cat/unicorn regression green, junk-that seat unlocked, no stopword mints. Out-of-scope observations filed: FOSS-014 (resurrection asymmetry) + cosmetic display notes (pre-existing). Lane C CLEAN. |
| D-it it naming preroute (011) + FOSS-015 pro-drop grounding | it 4179a890, aaeb9429 (R2), f1e7ccdc (R3) | R1 FAIL (UnboundLocalError 500 on every /retract/correct — CodeQL caught it; preroute stole split turns via English-only cue seed). R2 **CONDITIONAL PASS**: both blockers fixed + live-proven (200s both arms, migration 283 live with warm back-fill, no silent corruption), but pro-drop turns could not ground their bearer -> split rename parked loud. R3 (FOSS-015): _ground_prodrop_bearer lookup-only + forced SCALAR alias-flip on the SAME entity, 12 pins red-checked. R3 critic: FAIL live (door gated on facts-table naming rows the it ingest never writes — labels are alias-only; speaker-flattened subject missed). R4: alias-table door + speaker-shape + collision rung + FOSS-016 (tok.is_stop) — critic PASS on the core, ONE reach finding (seat-wide holders probe refused every pro-drop turn on multi-entity seats). R5: door candidates scoped by the TURN'S OWN CONTENT (pure-prodrop vacuum keeps the unique-holder fallback) — critic **PASS all four duties**: the round-5 criterion live (multi-entity retraction clarifies naming the bearer, not the entity-lane no-op), single-entity control byte-identical, correction arm live-proven (first phrasing routed CORRECTION and superseded), FOSS-016 reproduced (naming-frame opt-out documented: a user-asserted given name 'Che' files; the screen bites non-frame surfaces). foss-fix/it @ 7cd5e2b0 CLEAN — merge-ready. |
| E read polarity (013) | read 53fab803 (+8ef4127d gate hygiene) | R1 **PASS** duties 1-4 (fourth-path hunt found 8 more polarity-omitting dicts — deferred future lane, disjoint proven; en-never attack measured safe: the destructive router consumes analyze_directive, not the scope analyzer). LIVE **FINAL PASS**: same wound row A/B on the en stack — base renders "You use Port", fixed renders "You do not use Port"; affirmative + legacy shapes unregressed; superseded-history defer verified disjoint live (its own future-lane ledger note: the Berlin->Lisbon correction path left no superseded_at row). |
| F gate hygiene (pre-existing on release heads since a11b3d03/5241de80) | read 8ef4127d | foss smoke teardown dropped the flagent_ cortex-schema cleanup strings; the gate's own corpus test excluded from its scan (enforcement fixture, same convention as the script). All heads gate CLEAN after lane F. |
| PORT engine fixes -> es | es 5cd6f515 + six picks -> 74e03fd7 | Critic **PASS all five duties**: controlled merge delta exactly 4 files (lane E byte-identical, lane F verbatim); six cherry-picks core-lines identical to foss-fix/main; es content intact (veto body byte-identical, linguistics md5 unchanged); pins 28/28 + 8/8 + es baseline identical with a wired spot-red-check; LIVE negated render + surgical retraction + veto, zero 500s; statics zero-new; gate CLEAN. Pre-existing (not port blame): es response envelope misreports facts_superseded/isError on a SUCCEEDING retraction; re-extract re-stages has_pet as Class-B. |
| FOSS-012 es affirmative eaten | es d30ff08f -> 3f27fe0e | R1 critic **ALL FIVE DUTIES PASS** (shape audit: decline is parse+morphology + migration-293 DB rails, no new in-code zoo; the line pinned at 3 levels; flag kill-switch; order verified — sub-gate decline @0.642 pays no escalation LLM; live: wounded sentence now valid/committed=1/recall HIT, genuine anzi correction + native retraction + FOSS-005 veto all green, residual ip-variant fails safe). Nits fixed round 2 (3f27fe0e, test/doc/comment only): 3 es-model-context test defects + migration comment corrected (en floor seeds stop=xcomp_progressive; parar recorded as an explicit OWNER CALL, not added). Critic re-verify **FINAL PASS**: delta-only proven, both envs green (49/12 default; 0 failed under es-md; en pins activate under en model 52/9), claim-hygiene folded into the doc. |
| FINAL STATE — RELEASED | the three gate-CLEAN heads were published as squashed, personal-data-scrubbed trees built in isolated worktrees (a shared-checkout staging hazard that briefly touched a first-draft es head was caught in the A/D/M audit, rebuilt, never pushed). Scrub convention: pet names -> synthetic placeholders (Luna/Miso), real seat UUIDs -> synthetic constants, private branch names/shas/image refs -> neutral labels. Gates: tools/foss_gate.sh CLEAN x3 + the personal-data push gate. |
| FOSS-014 verbatim re-statement resurrection | resurrect 4823384f (on foss-main-local) | Worker root-caused (NOT the store upserts — the /ingest Redis idempotency validator trusted a cache hit when the vouched rows still existed although RETIRED; the paraphrase missed the cache and resurrected via the fresh CASE). Fix: validator trusts hits only on LIVE rows (retired/absent both bust, logged apart); staged Phase-4 tombstone replay-aware; marker is the ONLY discriminator. 11 pins, red 8F/3P at parent; 94 replay-guard pins green. Critic **ALL DUTIES PASS**: diff attacks adjudicated (live hits still serve; marked lanes untouched on both tables; TTL window no new divergence); red-check reproduced 8F/3P; subtree failure-set identity; LIVE marker-isolation proven on same bytes/same key (marked stays retired, unmarked resurrects); statics compensated (semgrep env-broken — CodeQL security 0 both sides, 183=183 pure line-shift pairing, manual all-SQL-parameterized); gate CLEAN. Merged into foss-main-local. |
| D-es es language (002/005-intent/009) | es d8a891fb, 0adc81a1, bc0e6b82, reworks 11aa2e5a + 139acc07 | R1 FAIL (sub-gate hole + async flip), R2 repairs verified but veto LIVE-INERT (es grammar yields no scopes), R3 **PASS** — UD sentential-negator arm: live veto evidence on both surfaces (CORRECTION@0.63/0.524 -> STATEMENT), zero escalations, zero /retract/correct for the negation seat, store polarity-safe; FOSS-002/009 untouched-PASS; en contract pins byte-identical. Two findings handed up: en-never scope delta (MEASURED correct, pinned as contract by lane E) + the read-side render gap -> FOSS-013 (lane E fired). |
| D-it it naming intent (011) | it 4179a890 | R1 **FAIL** (live on the rebuilt it stack): (1) BLOCKER — the guard reads is_retraction at main.py:59117 before its assignment at 59154 (UnboundLocalError -> every /retract/correct 500s under shipped flags; CodeQL py/uninitialized-local-variable caught it); (2) HIGH — the one-name-per-turn preroute steals split-turn it corrections/retractions ("Anzi, si chiama DevBox." routed STATEMENT, no supersede, orphan pref_name; "dimentica..." wrote an alias) because the bootstrap cue seed is English-only. The original wound IS fixed at classify (first-mention battery never misroutes; en-parity anchor confirmed). Round 2 ordered (assignment order + stored-bearer-gated preroute + it cue seeds + split-turn pins). |
