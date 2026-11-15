# GLiNER2 1.3.2 -> 2.5 Upgrade Pathway -- Migration Report

**Date:** 2026-11-06 | **Repo state:** saas @ 5d7435a3, clean tree
**Harness:** benchmarks/gliner25_upgrade/ (venv-isolated, out-of-tree; nothing in
pyproject.toml / Dockerfile / src/ was modified)
**Verdict in one line:** the PACKAGE upgrade (gliner2 1.3.2 -> 2.1.0) is evidenced GO
with zero code changes; the GLiNER2.5 MODEL swap is evidenced NO-GO today (intent gate
fails), preserved as a gate-gated staged patch. The Stage-2 RETRY candidate
(fastino/GLiNER2.5-Decide, the classification specialist) was measured 2026-11 and is
also **NO-GO** -- it fails 4 of the 5 gates (sec. 4b); the model-swap line is CLOSED on
CPU prod absent a new candidate that clears all five gates on this same harness.

---

## 1. Research findings (sources cited)

### 1.1 Naming disambiguation (resolves the "2.5.x" question)

There is NO gliner2 2.5.x pip package. PyPI (pypi.org/pypi/gliner2/json, fetched
2026-11-06) publishes: ... 1.3.1, **1.3.2** (uploaded 2026-06-30), **2.0.0**
(2026-08-24), **2.1.0** (2026-10-09, current latest). "GLiNER2.5" is the MODEL FAMILY
name (the boundary-prediction architecture + its checkpoints), not a package version.
The task's "1.3.2 -> 2.5.x" therefore resolves to:

- package: gliner2 1.3.2 -> **2.1.0** (the 2.x line; 2.0.0 is the release that
  introduced GLiNER2.5 support, same day as the model release),
- model: fastino/gliner2-base-v1 (span) today; fastino/gliner2.5-base-v1 (boundary)
  as the 2.5-family candidate.

### 1.2 Primary sources

- Fastino blog, "Introducing GLiNER2.5: Efficient Span-Free Information Extraction
  with Schema-Driven Interface", Mary Newhauser & Urchade Zaratiana, 2026-08-24:
  https://fastino.ai/blog/gliner2-5-span-free-information-extraction
  - Boundary prediction replaces span enumeration (scores start/end directly; inference
    scales linearly with document length; no fixed ~12-word span-width ceiling).
  - Trained on sequences up to 4,096 words; library adds chunked long-document APIs.
  - Five new capabilities: long-context extraction/classification, unlimited span
    length, joint entity+relation extraction (JointIE), constrained classification
    (Classifier), span attributes.
  - Three Apache-2.0 checkpoints: gliner2.5-base-v1 (0.2B), gliner2.5-multi-v1 (0.3B),
    gliner2.5-small-v1 (74M).
- GitHub README (github.com/fastino-ai/GLiNER2, main, fetched 2026-11-06):
  - Two architectures behind one public API: span (GLiNER2 / SpanExtractor) and
    boundary (BoundaryExtractor, GLiNER2.5), dispatched by the checkpoint's saved
    architecture field via AutoExtractor.from_pretrained.
  - "GLiNER2.from_pretrained(...) remains span-only and will NOT load GLiNER2.5
    boundary checkpoints." (MEASURED CONFIRMED -- see section 4 loader probe.)
  - extract_entities(text, labels) survives unchanged in shape:
    {"entities": {label: [str | {text, start, end, confidence}]}}.
  - classify_text / batch_classify_text survive (Decide checkpoints "use classify_text").
  - extract_relations(text, [rel_labels]) returns
    {"relation_extraction": {rel: [(head, tail)]}} -- the shape
    main.py::_convert_gliner_relations_to_edges already consumes.
  - Model table: fastino/gliner2-base-v1 = 205M DeBERTa-v3-base English span (the
    current FaultLine model); gliner2.5-base-v1 = 194M DeBERTa-v3-base English boundary
    ("the everyday English model"); Decide family = classification specialists:
    fastino/GLiNER2.5-Decide (340M DeBERTa-v3-large, SPAN architecture despite the 2.5
    name -- so GLiNER2.from_pretrained also loads it; intent/routing/sentiment use).
- HF model card fastino/gliner2.5-base-v1: pip install "gliner2[local]"; Python >=3.10;
  "Always use AutoExtractor for GLiNER2.5"; this checkpoint trained with
  enable_relations=True.
- PyPI metadata gliner2 2.1.0: requires-python >=3.10; extras -- [local] = torch>=2.1,<3
  + transformers>=4.43,<6 + numpy/peft/safetensors; [disentangled-flash] = optimized
  DeBERTa-v2/v3 attention, needs transformers>=4.57 (optional, NOT in this harness --
  candidate follow-up); [train]/[dev]/[benchmark] as published. The BASE install is
  torch-free -- that is exactly the 2026-08-27 build-break mechanism recorded in
  pyproject.toml (ModuleNotFoundError: transformers at the Dockerfile pre-download).
- RELEASE.md (same repo): quality gates run with BOTH exact checkpoints -- span
  fastino/gliner2-base-v1 AND boundary fastino/gliner2.5-multi-v1.

### 1.3 Compatibility with our four call-site families (census)

| FaultLine site | call | 1.3.2 | 2.1.0 verdict |
|---|---|---|---|
| main.py startup (lifespan) + embedder.py::_recall_uplift_gliner | GLiNER2.from_pretrained("fastino/gliner2-base-v1") | works | works UNCHANGED for the span checkpoint (harness arm pkg-only loads it); boundary checkpoints REQUIRE AutoExtractor (staged patch) |
| main.py::_gliner2_type_entities | extract_entities(text, labels, threshold=0.5, include_confidence=True, max_len=2048) | works | signature identical (+ optional overlap_policy, default None); output shape identical; spans verified in harness |
| main.py::classify_intent | classify_text(text, {"intent": [labels]}, include_confidence=True) | returns {"intent": {"label", "confidence"}} | SAME signature; SAME output shape measured on 2.1.0 (raw: {'intent': {'label': 'stating a fact, preference, or habit', 'confidence': 0.9998}}); the endpoint's dict/bare-string tolerant parse is untouched |
| main.py::_build_typed_doc (+ /ingest candidate path, embedder mirror) | extract_relations(text, relation_types=..., threshold=0.5, max_len=2048) | works | kwarg surface IDENTICAL on 2.1.0 (probe ok on all arms); output {"relation_extraction": {rel: [(head, tail)]}} -- tuple pairs feed _convert_gliner_relations_to_edges unchanged; span->spaCy Doc seeding in _build_typed_doc consumes surfaces only, shape-stable |

Conclusion: the pip bump is API-compatible with ZERO source changes. The only adapter
the ecosystem demands is the LOADER (AutoExtractor) -- and only if a boundary
checkpoint is ever baked.

---

## 2. Harness (isolated; how to reproduce)

Location: benchmarks/gliner25_upgrade/ -- run_all.sh builds nothing in-tree and needs
two throwaway venvs (commands documented in run_all.sh header):

- /tmp/gliner132-venv: gliner2[local]==1.3.2 + torch==2.13.0+cpu (CPU index) +
  transformers==4.57.6 + tokenizers==0.22.2 -- the EXACT live pre-prod combo recorded
  in pyproject.toml's pin comment.
- /tmp/gliner25-venv: gliner2[local]==2.1.0 + CPU torch (resolved torch 2.15.0+cpu,
  transformers 5.22.1). Both within upstream's declared ranges.

Three arms x four gates + one probe, one process per arm (sequential -- concurrent
runs would pollute Gate D):

| arm | package | model | loader |
|---|---|---|---|
| baseline | 1.3.2 | fastino/gliner2-base-v1 | GLiNER2.from_pretrained (prod today) |
| pkg-only | 2.1.0 | fastino/gliner2-base-v1 | GLiNER2.from_pretrained (Stage-1 candidate) |
| target | 2.1.0 | fastino/gliner2.5-base-v1 | AutoExtractor.from_pretrained (Stage-2 candidate) |

Honesty notes on method:

- The 39-case D1_HYBRID eval set is loaded by AST from the SHIPPED source
  (tests/test_intent_label_regression.py::CASES), and the intent labels are extracted
  by AST from src/api/main.py::_build_intent_descriptions_for_gliner2 -- single source
  of truth, no hand-copied strings.
- HARNESS VALIDITY PROOF: the baseline arm reproduces the shipped benchmark numbers
  EXACTLY (87.2% / 0-of-10 / 20-of-20) -- this harness measures the same thing the
  2026-06 benchmark measured (DEV/DESIGN-intent-classification-gliner2-label-safety.md).
- "benchie": at harness-build time benchmarks/harness/benchie did NOT exist in this
  tree (the directory held EP2_CALL.md / README-executors.md only), so Gates A/C
  implement the benchie local seam HERE: typing over a fixed gold set through the live
  call shapes. NOTE: a CONCURRENT lane added benchmarks/harness/benchie/run_benchie*.py
  mid-session (uncommitted work by another agent, not part of this change-set); this
  report was written against HEAD 5d7435a3 where benchie was absent.
- Gold set (gold_set.py): 20 FaultLine-style user-memory sentences, 43 gold entities,
  six canonical labels ONLY (Pitfall 11 closed set). Gold entries carry a SET of
  acceptable labels where two canonical readings are defensible (e.g. software reads
  Object or Concept) so precision is fair to both architectures. Loose match =
  label-intersection + surface overlap (the signal _build_typed_doc actually consumes:
  substring locate); exact match = normalized equality.
- AGGREGATION BUG (found and fixed, disclosed): the first in-arm aggregation matched
  every sentence's spans against the FULL gold set inside the sentence loop
  (denominator x20 -> nonsense 6.3% recall in the first printed output). The corrected
  numbers below were RECOMPUTED PER-SENTENCE from the saved raw spans
  (recompute_typing_metrics.py over results/*.json typing_rows) -- no model was re-run,
  and the raw spans are committed for audit.
- Absolute latency is a dev-box CPU number; relative deltas across arms are the claim
  (prod measures classify ~0.95s warm under load, main.py comment -- same order of
  magnitude as our p50 with contention excluded).

---

## 3. Gate results (numbers)

### Gate A -- detection-rate parity, six canonical labels (PASS)

Loose / exact recall over 43 gold entities; per-sentence full coverage:

| arm | loose recall | exact recall | full-coverage sentences |
|---|---|---|---|
| baseline (1.3.2) | 0.837 | 0.814 | 13/20 |
| pkg-only (2.1.0, same model) | 0.837 | 0.814 | 13/20 -- BYTE-IDENTICAL spans to baseline |
| target (2.5 boundary) | 0.861 | 0.861 | 14/20 |

Pitfall-11 detection-rate parity HOLDS: same labels, same sentences. pkg-only is exact
parity (identical returned spans -- same weights, same code path). The boundary model
is slightly BETTER (+2.3pp), with visible trade-offs in results/target.json typing_rows
(e.g. stops returning the bare pronoun "I" as Person; misses "Whiskers" in one sentence
while gaining coverage elsewhere).

### Gate B -- D1_HYBRID 39-case intent benchmark (PASS for Stage 1; FAIL for Stage 2)

| arm | accuracy | editor/usage false-CORRECTION | STATEMENT recall | per-class |
|---|---|---|---|---|
| baseline | 87.2% (34/39) | 0/10 | 20/20 | Q 6/6, S 20/20, R 5/6, C 3/7 |
| pkg-only | 87.2% (34/39) | 0/10 | 20/20 | IDENTICAL to baseline |
| target | **69.2% (27/39)** | **2/10** | **15/20** | Q 3/6, S 15/20, R 5/6, C 4/7 |

Target-arm failures (the live-repro failure mode RETURNS on gliner2.5-base-v1 with the
SAME concise D1 labels): "I edit my configs in vi" -> CORRECTION, "I update my dotfiles
with vim" -> CORRECTION (the exact editor/usage misroute Pitfall 11 exists to prevent);
plus broad discrimination collapse: 3x STATEMENT->QUERY, 3x CORRECTION->STATEMENT,
1x RETRACTION->STATEMENT, 3x QUERY->STATEMENT. Stage-2 verdict: **measured NO-GO** --
a verbose-label "fix" is forbidden by Pitfall 11 (the 07a5484 regression measured 48.7%
on that path); the candidate retry is a classification-specialist checkpoint
(fastino/GLiNER2.5-Decide) benchmarked through this same harness.

### Gate C -- typing F1, benchie local seam (PASS)

Micro precision/recall/F1 over the gold set:

| arm | loose P/R/F1 | exact P/R/F1 |
|---|---|---|
| baseline | 0.712 / 0.837 / 0.769 | 0.692 / 0.814 / 0.748 |
| pkg-only | 0.712 / 0.837 / 0.769 (identical) | 0.692 / 0.814 / 0.748 |
| target | 0.771 / 0.861 / 0.813 (+0.044 F1) | 0.771 / 0.861 / 0.813 (+0.065 F1) |

### Gate D -- CPU latency per call, mean / p50 / p95 ms (PASS; neutral-to-better)

| arm | classify short | typing short | typing long (117 words) |
|---|---|---|---|
| baseline | 66.2 / 65.7 / 67.0 | 66.6 / 66.5 / 66.9 | 150.0 / 152.6 / 153.4 |
| pkg-only | 66.0 / 65.8 / 66.7 | 64.9 / 64.9 / 65.1 | 130.6 / 130.0 / 132.9 |
| target | 58.7 / 58.6 / 59.7 | 59.9 / 57.8 / 66.5 | 115.2 / 115.0 / 117.0 |

The 2.5 boundary model is ~10-13% FASTER per call on CPU (linear-length inference),
but Stage 2 is blocked by Gate B regardless.

### Loader probe (evidence for the Stage-2 adapter)

Under gliner2 2.1.0: GLiNER2.from_pretrained("fastino/gliner2.5-base-v1") REFUSES the
boundary checkpoint -- AttributeError: 'ExtractorConfig' object has no attribute
'max_width' (probe_loader_refusal.py). AutoExtractor.from_pretrained loads it as
BoundaryExtractor. So Stage 2 REQUIRES the two-load-site adapter (staged patch), and a
naive model-id edit without the adapter would crash startup (fail-loud, at least).

---

## 4. Decision and change-set

### Stage 1 -- GO (package bump, model unchanged)

Change-set = ONE patch file, verified to apply cleanly:
- benchmarks/gliner25_upgrade/patches/pyproject-gliner2-2.1.0.patch
  ("gliner2==1.3.2" -> "gliner2[local]==2.1.0" + explicit "transformers>=4.57,<6",
  with the revalidation evidence inlined as the pin comment's continuation).
- NO src/ changes (section 1.3), NO Dockerfile changes (same model id + loader baked),
  NO model-cache changes (HF bake identical), no data or format migrations.
- The [local] extra is the structural fix for the 2026-08-27 incident class:
  transformers/torch become DECLARED dependencies of the pin instead of transitive
  accidents (1.3.2 pulled transformers only via peft).

### Stage 2 -- NO-GO (GLiNER2.5 model swap), gate-gated staged patch

- benchmarks/gliner25_upgrade/patches/staged-2.5-model-swap-DO-NOT-APPLY.patch:
  AutoExtractor adapter at src/api/main.py startup + embedder.py::_recall_uplift_gliner
  + Dockerfile pre-download line; header records the failing numbers and the re-pass
  condition (run bash benchmarks/gliner25_upgrade/run_all.sh on a candidate checkpoint
  -- e.g. fastino/GLiNER2.5-Decide, which is span-architecture and loads via
  GLiNER2.from_pretrained -- BEFORE applying).
- Worth noting for the retry design: typing IMPROVED on the boundary model while intent
  collapsed -- a SPLIT deployment (span model for /classify-intent, boundary for typing
  + relations) would pass today's gates but doubles the baked weights and the memory
  footprint; treat as a separate measured decision, not a silent bundling.

### Stage 2 retry (2026-11): fastino/GLiNER2.5-Decide -- measured NO-GO, line CLOSED

The named retry candidate was benchmarked through the SAME harness and the SAME gate
definitions (arm `decide` added to harness_common.ARMS; nothing else in the gate math
changed). Model identity: HF revision **9d1bfb848cd16d93ee56ccbc59cd92bf0f284d90**
(lastModified 2026-10-08), config `architecture="span"`, max_width=8, base_model
fastino/gliner2-large-v1, Apache-2.0. The card says "340M"; safetensors metadata
records 486,444,053 F32 params (large encoder + heads) -- both recorded. Loader probe
(probe_decide_loader.py -> results/stage2-decide/loader_probe.json): the PROD loader
`GLiNER2.from_pretrained` LOADS it unchanged (3.0s, class GLiNER2); AutoExtractor
dispatches it to SpanExtractor -- so NO adapter was needed and the staged boundary
patch is confirmed the wrong shape for this candidate. Control arm: `pkg-only`
re-run same box/session in a prod-fidelity venv (gliner2[local]==2.1.0 /
transformers 4.57.6 / torch 2.15.0+cpu / tokenizers 0.22.2) -- it reproduced the
committed Stage-1 numbers EXACTLY (intent 34/39 = 87.2%, typing loose recall 0.8372 /
exact 0.814, full-coverage 13/20, Gate C loose F1 0.7692), so the box is valid and
the deltas below are weight-only.

| gate | required | Decide measured | verdict |
|---|---|---|---|
| 1 intent (39-case D1) | >= 34/39 (= 87.2%) | **33/39 (84.6%)**, editor false-CORRECTION 2/10, STATEMENT 16/20, mean confidence 0.629 (control 0.965); 23/39 preds below the prod 0.70 confidence gate | **FAIL** |
| 2 typing parity | byte-identical or improvement, zero regressions | loose recall 0.8372 -> **0.7209**, full coverage 13/20 -> 10/20, 5 sentences gain NEW pinned regressions (Whiskers+cat, Cupertino, anxiety, Comet+greyhound, onboarding handbook) | **FAIL** |
| 3 relation/span | F1 not regressed beyond Stage-1 tolerance | Gate C F1 +0.016 nominal (precision-through-sparsity, 36 vs 52 spans) BUT the relation lane is DEAD: relations on **2/20** sentences vs 16/20 control (18/20 return zero pairs -- the live `_build_typed_doc` -> `extract_relations` path would go silent) | **FAIL** |
| 4 latency | no worse than +15% | classify 57.5 -> 212.1 ms (**+269%**), typing short 55.4 -> 202.7 ms (**+266%**), typing long 120.3 -> 417.1 ms (**+247%**); sequential same-box, relative delta is the claim | **FAIL** |
| 5 Pitfall 11 labels | label text untouched; no label phrasing demanded | labels pulled by AST from `_build_intent_descriptions_for_gliner2` / `_CANONICAL_ENTITY_TYPES` -- zero text changed; the card prescribes no phrasing ("Pass any label set at call time") | HOLD (process) -- but the editor/usage -> CORRECTION misroute CLASS returns via the WEIGHTS (gate 1) |

Gate-1 failure detail (full per-case preds are committed, not just the score):
`I edit code with nano` -> CORRECTION and `I edit my configs in vi` -> CORRECTION (the
original 07a5484 live-repro case), plus a NEW worse mode: plain biographical
STATEMENT -> RETRACTION (`I graduated from MIT`; `I update my dotfiles with vim` ->
RETRACTION), which in production routes to `/retract/correct`.

**Decision: NO-GO.** No source change shipped; `staged-2.5-model-swap-DO-NOT-APPLY.patch`
stays DO-NOT-APPLY (and is now doubly stale: its named retry candidate has been
measured and failed). Closing recommendation: the 2.5 model-swap line is CLOSED on CPU
prod. The specialist fine-tune trades exactly the abilities the gates pin (editor/usage
discrimination, typing recall, the relation head) for routing breadth, at ~3.7x the
per-call CPU cost. Re-open only if a future checkpoint clears ALL five gates on this
same harness; a smaller-than-base specialist or a separately-gated split deployment
(doubled weights, its own latency pass) would be the shapes to try next.

Evidence: benchmarks/gliner25_upgrade/results/stage2-decide/{loader_probe.json,
pkg-only.json, decide.json, gate_verdict.json, typing_relations_diff.json} -- decide.json
carries all 39 per-case intent preds + every returned typing span + raw relations per
gold sentence, so a reviewer can diff misses without re-running anything.

---

## 5. Rollout plan (Stage 1) and rollback

Rollout:
1. Apply the patch; regenerate the lockfile (uv lock) and RECORD the resolved versions
   in the PR body (harness reference: gliner2 2.1.0 / transformers 5.22.1 / torch
   2.15.0+cpu; live prod today: transformers 4.57.6 / torch 2.13.0+cpu -- both inside
   upstream's declared ranges).
2. Rebuild the image (Dockerfile unchanged). The CPU-torch-first trick keeps precedence;
   the GLiNER2 pre-download step bakes the SAME model id.
3. Pre-prod deploy; run tests/test_intent_label_regression.py live against the image
   (it loads the shipped labels and the same call shape), plus one spine ingest turn
   end-to-end and one /classify-intent. Rule 2 of CLAUDE.md applies: verify on the
   container, not in the lockfile.
4. Prod deploy through the normal measured-decision process.

Rollback: single-line revert of the pin to gliner2==1.3.2 and drop the transformers
floor; rebuild. Dependency-only -- no model-cache, data, schema, or API-shape changes
are involved, so rollback is fast and total. 1.3.2 remains published on PyPI.

## 6. Risks

- transformers resolution moves off 4.57.6 (live) to the lockfile's choice within
  >=4.57,<6 (harness verified 5.22.1). Behavior-equivalent on every gate we ran, but
  the pre-prod soak is MANDATORY -- this dependency class already broke a build once
  (the pin comment exists for exactly this).
- The span checkpoint's ~384-word trained truncation (main.py input-window safety
  comment) remains TRUE under 2.1.0 with the same model -- no semantic change. Stage 2
  would CHANGE that window (4,096-word training), altering _GLINER2_MAX_INPUT_TOKENS
  semantics -- one more reason the model swap is its own measured decision.
- Latency parity is evidenced on a dev box; prod CPU contention differs (main.py
  measured 0.95s warm / 22s under provisioning contention). Relative parity is the
  claim; absolute numbers are not transferable.
- The gold set is 20 sentences -- a parity smoke, not a full NER evaluation. Gate B
  (the shipped 39-case benchmark, reproduced exactly) is the primary evidence.
- The "classification" lane and "typing" lane share one model today; a future Stage 2
  split or Decide swap must re-run BOTH gates per lane, never assume transfer.

## 7. Audit trail

- Harness + gates: benchmarks/gliner25_upgrade/ (gold_set.py, harness_common.py,
  run_arm.py, recompute_typing_metrics.py, probe_loader_refusal.py, run_all.sh,
  README.md)
- Raw results: benchmarks/gliner25_upgrade/results/{baseline,pkg-only,target}.json
  (typing_rows carry every returned span per sentence)
- Stage-2 retry evidence: benchmarks/gliner25_upgrade/results/stage2-decide/
  {loader_probe,pkg-only,decide,gate_verdict,typing_relations_diff}.json +
  probe_decide_loader.py; the decide arm (harness_common.ARMS) and the additive
  per-case preds / relations_rows capture in run_arm.py are part of this kit
- Patches: benchmarks/gliner25_upgrade/patches/ (+ README with apply/no-apply and
  rollout/rollback)
- Report: docs/GLINER2-2.5-UPGRADE-MIGRATION.md (this file)
- Nothing in pyproject.toml, Dockerfile, src/, or uv.lock was modified.
