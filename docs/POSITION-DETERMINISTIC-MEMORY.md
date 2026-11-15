# Position: Deterministic Memory for Agents

**Why FaultLine is a different category than vector memory — a technical position paper**

*Every architectural claim is cited to this repository by path (GLM.md is the master doctrine). Honesty contract: every number is a measurement committed in this repo; every claim not yet measured is marked **[EVIDENCE-PENDING]** with what would measure it; where we are not ahead of the field, the text says so.*

---

## 0. The category claim

Agent memory in 2026 is dominated by one architecture family: extract salient text, embed it, retrieve by similarity, stuff context. Mem0 made memory a one-afternoon install and published real latency/token results against full-context (arXiv 2504.19413); Zep/Graphiti proved temporal validity windows beat overwrite-on-write (arXiv 2501.13956); Letta (MemGPT) let the agent self-edit context as virtual memory (arXiv 2310.08560). The 2026 comparison wave treats these as one category with different dials.

FaultLine is not that category with different dials. It is a **per-tenant, write-validated knowledge graph where PostgreSQL is authoritative, the LLM never holds unsupervised write access, and recall is a deterministic walk of filed structure** (GLM.md, "What FaultLine Is"; DEV/ARCHITECTURE-OVERVIEW.md). In SQL terms: recall is a **SELECT with joins over a graph the engine filed**, not a nearest-neighbor search.

The founding distinction everything else hangs on (GLM.md, "THE HARD LINE"):

- **A memory is what the user told you** — grounded values, sacred, never modified by the engine.
- **L4 is the *place* to put a memory** — the type hierarchy (`dog → canine → mammal → animal`), built and grown by the engine. You FILE a memory AT a place and WALK the place to RETRIEVE it; a name never becomes a place, a place is never user content.

That line is the truth-firewall: engine growth only ever touches *places*, so the system can grow, re-index, and re-derive forever without corrupting what the user actually said. Seven properties follow.

## 1. The agent cortex is a separate authority domain

Most systems have one memory and let the model manage it. FaultLine gives every seat **two hemispheres with different authority** (DEV/DESIGN-agent-cortex.md):

- `faultline_<uuid>` — the **human's** validated memory: every write passes the WGM gate (`src/wgm/gate.py`), provenance is stamped, every answer traces to a real row, every layer is user-correctable.
- `flagent_<uuid>` — the **agent's own operational memory** (gotchas, failed commands, pitfalls, corrections, how-tos): deliberately ungated, because the agent is the authority on its own operating experience — and **never served as a user fact**.

The wall is structural: a distinct schema-name shape (`flagent_` prefix, fail-loud refusal otherwise), a distinct Qdrant collection prefix, poison tests asserting neither hemisphere leaks into the other (`tests/test_cortex_firewall.py` + `tests/test_cortex_lifecycle.py`, 15/15; GLM.md cortex section). The agent gets *smarter about operating* — notes harden into rules at observation count ≥ 3 — while the user's record of truth stays exactly what the user said.

The alternative bet — the agent self-edits memory (Letta) — makes truth only as disciplined as the model doing the editing. FaultLine's bet: **the model is an interchangeable client; the memory is the authoritative thing** (GLM.md, "Model-agnostic secured memory") — connect Claude or Mistral, both read the identical validated graph.

## 2. Deterministic recall — no LLM in the hot path

Recall resolves the question's anchor with the **same machinery ingest used to ground it** (GLiNER2 typing + rel-type resolution), then walks the L4 structure ingest built — scope-projected, depth carried by the reference itself (GLM.md, "Query Path"; DEV/QUERY-PIPELINE.md). No cosine, no generative call in the decision. Compound patterns execute in **<1 ms** (GLM.md extraction section; `src/extraction/compound.py`); synonym surfaces weld by **convergence-by-identity** — normalized-token identity inside `resolve()` (`src/entity_registry/registry.py`), never a similarity score.

Measured walls, live stack:

| measurement | result | source |
|---|---|---|
| deterministic walk, warm | `recall_walk_done elapsed_ms=24` / `28` | `DEV/review/gauntlet/inprocess-mcp-client/round_1..2/repro.txt` |
| `/query` under concurrent STATEMENT churn + 32-chunk doc drain | **p50 35.7 ms, p95 86.2 ms, 0 failures** (n=279) | `DEV/review/gauntlet/interactive-latency/ledger.json` |
| live seat (5,189 facts, 64k episodic rows), idle query | p95 ≈ 70 ms | same ledger, `pre_prod_live_seat` |
| async serving-loop headroom during full churn | 404-control route **p99 2.2 ms / max 2.3 ms** | `.../interactive-latency/round_4/RUN_NOTES.md` |

Intent classification on the recall door is GLiNER2 zero-shot — a small span model, not a generative LLM — pinned at **87.2%** on the 39-case D1_HYBRID benchmark (48.7% before the concise-label fix; `tests/test_intent_label_regression.py`). A sick brain must never hang a recall: one handler carries a hard ceiling (`MCP_RECALL_TOTAL_BUDGET_S=12`, GLM.md environment section), and the 30.7 s burn wound that motivated it is documented, not hidden (`DEV/BUGS/BUG-recall-door-statement-input-llm-burn-20261026.md`).

**The honest trade:** deterministic recall has *hard misses* — if ingest did not file a fact, the walk returns nothing; it cannot fuzzy-guess a plausible near-miss. That is the correct failure mode: a miss is loud and fixable at write time, and when six benchmark misses were investigated, **6/6 were capture failures and 0 were walk failures** (GLM.md, query-path findings). The token argument follows mechanically: return *the fact, not the haystack* — one scoped sentence, not re-sent history or a pile of similar chunks. We state this by mechanism, not an invented percentage; Mem0's own paper already concedes the haystack costs ~90% of tokens versus full-context, and vector-RAG recall still stuffs chunks. **[EVIDENCE-PENDING]** a measured end-to-end token comparison (identical reader and question set, walk vs context stuffing).

## 3. Poison resistance — ASI06 replay is structurally closed

OWASP added **ASI06: Memory & Context Poisoning** to its 2026 Top 10 for Agentic Applications: an attacker plants content in persistent memory; the agent later reasons from it as truth. The generic stack is exposed by construction — anything can be written, recall serves whatever is stored.

FaultLine closes the replay path at three independent layers:

1. **The write gate.** Every edge passes the WGM gate: metadata-driven validation against the tenant's own `rel_types` constraints — head/tail types, hierarchy flags, correction behavior (GLM.md, "Ingest Pipeline & WGM Gate"). Junk must be expressible in the tenant's typed ontology, with provenance, to enter at all.
2. **Provenance is a rank, not a label.** Unattested content is **forced** to `fact_provenance="llm_inferred"` and staged Class B — never asserted, never rendered as user truth (GLM.md, provenance routing; the asserted-vs-inferred firewall is enforced across every response mode, DEV/DESIGN-fact-realization-and-voice.md). Poisoned content can enter only as tentative holdings, promotable only by repeated confirmation — the opposite of the one-shot replay ASI06 describes.
3. **Containment.** A poisoned row lives inside one tenant schema and cannot traverse the seat boundary (§4).

Plus a direct front door: `_check_injection_signals` rejects prompt-injection constructs before text reaches extraction (`src/mcp/server.py`; DEV/DESIGN-llm-authority-hardening.md honestly rates it MED, defense-in-depth — 7 patterns, two tools, not a complete filter).

Honest limit: no store is unpoisonable; an adversary who controls *the user's own words* correctly controls what is filed for that user. The structural claim is narrower and provable: **the replay vector — write once, served as asserted truth forever — requires bypassing validation, provenance ranking, and tenant containment simultaneously.** **[EVIDENCE-PENDING]** an adversarial red-team suite attacking ingest content (today's poison tests assert hemisphere and tenant containment, not adversarial content).

## 4. Hard per-seat isolation — schemas beat row-level security

Every seat is its own PostgreSQL schema, `faultline_<slug>`, bound by `SET search_path TO <schema>` **without `public`** at every request, ingest, query, and gate cursor; the gate re-binds after any rollback so a failed transaction cannot fall through to the template (`src/wgm/gate.py::_reapply_search_path`; GLM.md, "Per-Tenant Isolation (HARD RULE)"). `public.*` is a seed template, never read at runtime.

Row-level security isolates rows inside one shared schema; a missing `WHERE` clause or NULL tenant key silently co-mingles tenants. Schema-per-seat changes the failure shape: the boundary is the database's own namespace resolution, not application discipline, and export, purge, and litigation-hold are provably scoped per seat. With the user-memory vector tier retired entirely (`USER_MEMORY_VECTOR_LANE=false`, GLM.md Critical Truth #4), no user data lives outside the schemas at all.

Isolation had two near-misses, both caught by adversarial process — that audit trail is the honesty: an unauthenticated route that could write the *seed template*, its docstring claiming a guard the code (a literal `pass`) never had — fixed and split by write-target with operator-only opt-in (GLM.md, `7f693704` note); and a test-pin override that would have collapsed tenants onto one schema (GLM.md, testing doctrine: "making it pass" would have re-introduced the vulnerability). **The known cost, stated plainly:** schema-per-seat is a real scaling tax — a per-schema migration ledger, pooler discipline pending `SET LOCAL`, and a designed-but-not-yet-battle-tested per-account grain for ~10× fewer schemas (GLM.md, scale plan). **[EVIDENCE-PENDING]** a published multi-thousand-schema scaling benchmark.

## 5. Corrections — an authority order, not a guess

Contradiction handling in production memory systems is, per the first formal treatment, **heuristic**: TOKI (arXiv 2606.06240) formalizes bitemporal contradiction resolution as concurrency control and documents that deployed systems improvise with last-writer-wins, evidence-weighted merge, or await-confirmation — or leave conflict detection to the LLM's judgment. Guessing whether two statements conflict is where a memory starts disagreeing with its owner.

FaultLine routes the decision through structure:

- **Intent first.** Every turn is classified QUERY / STATEMENT / CORRECTION / RETRACTION by GLiNER2 zero-shot (87.2%, regression-pinned). A CORRECTION is an *instruction*, not an extract to weigh: `is_correction=True` commits Class A at confidence 1.0 (GLM.md, class rules).
- **Authority order `user > seed > growth`** (DEV/DESIGN-canonicalization.md §1.2): the user's naming is stored as the preferred surface; engine aliases hang off the same node as non-preferred reachability only. The preference-rank ladder (`user_stated=5` … `provisioned=1`, `src/entity_registry/registry.py`) makes it **structurally impossible** for an inferred, growth-authored edge to demote user truth — the rank arithmetic forbids it, no judgment involved.
- **Archive, never annihilate.** A corrected value is retired and retained (GLM.md, scalar supersession); bi-temporal residence currency keeps the former address answerable — `"where did I used to live?"` returns Toronto, not "no relevant facts" (DEV/DESIGN-residence-currency-valid-from.md, including the live failure that motivated the fix). Forgetting is two tools: `retract_fact` for plain-language retraction, `forget_fact` for a precise tombstone addressed by the row's own unique key — never fuzzy matching (GLM.md, advertised tool surface).
- **Replays cannot resurrect.** Every deferred re-send of an attempted write carries a replay marker so an already-retired row stays retired (GLM.md, deferred-replay marker — shipped after a live resurrection wound in the reextract lane).

**[EVIDENCE-PENDING]** correction precision/recall on naturalistic correction-heavy streams (mechanism pinned by unit tests; the field-level accuracy number does not exist yet).

## 6. Ontology growth into the unknown

The seed is **intentionally minimal** — a zero-shot grounding framework, with `public` as a read-only template seeded FROM, never INTO (GLM.md, "Operating Model"). Thousands of topics are unseeded by design; a missing concept ("animal", a decade) must be **grown into**, never ignored, never hardcoded. Widening seeds would hide the growth engine's defects, not fix them — that is the standing rule separating growth from cheat.

Growth is bounded by construction, along two pathways (GLM.md, "TWO DISTINCT pathways"):

1. **Natural growth:** every ingest auto-grows a bounded ±6 hierarchical "semblance" around the entity — instant synchronous attach (retrievable now) plus an async buildout that **never blocks recall**.
2. **`/expand` (learn):** a separate, explicit mechanism that learns a subject's full depth online, bound to the subject plus a little outward — anti-sprawl is part of the mechanism, not a hope.

Welding is deterministic: convergence-by-identity (§2), not cosine — the classic redundant-node problem of open KB construction (Galárraga et al., CIKM 2014, cited in DEV/DESIGN-canonicalization.md) solved offline by identity and WordNet synset membership. Novel relation types are frequency-gated; the seeded backbone is the attach ceiling. Growth is **inspectable and retirable**: `/structure/grown` surfaces what the engine grew with an `inert` flag for hollow groupings, and `review_structure` retires one wrong cue by its exact unique key (GLM.md, structure section). Floors are real floors: seeded cue classes resolve as `floor ∪ tenant` with user-retired keys filtered from the floor, so a correction cannot be resurrected by a code default (GLM.md, cue-class rails). Known defects are measured and owned as growth-path work — inert groupings from unsatisfiable constraints (migration 271) — not papered over as seed gaps. **[EVIDENCE-PENDING]** a growth-quality metric: over a corpus with out-of-seed topics, the fraction that become walkable structure within N ingests, and hollow-rate over time.

## 7. Documents ingested as memory — not as chunk pointers

The dominant pattern ingests a document as *pointers*: chunk, embed, later serve the chunk that scores best. The system then *knows the document says a thing* — it does not *know the thing*. Knowing a document mentions kung-fu is not the same as knowing kung-fu.

FaultLine's document lane is a **learning** lane: chunks are re-extracted through the same spine and gates as conversation — deterministic dependency derivation, typed edges staged and committed into the seat's graph, chunk-level retry, honest zero-yield accounting for cite-dense reference blocks (GLM.md, document lane; `benchmarks/doc-ingest-litmus/README.md`) — leaving internalized, connected, walkable structure. The document was only the input.

**The deletion test.** Remove the source. A RAG stack loses the knowledge — the chunk *was* the memory. FaultLine keeps it — the grown facts are the memory. Structural, and with a measured corollary: **vectors are frozen at write time; retained structure is not.** The user-memory vector tier was retired on exactly this argument: a retained turn can be **re-read against the grown ontology** (`reextract_episodic`, provenance-preserving), so growth runs *backwards over history*; an embedding blob cannot be reinterpreted (GLM.md, Critical Truth #4). When the ontology grows, yesterday's corpus quietly gets smarter.

**The honest ceiling — extraction yield.** Internalizing is bounded by extraction, and extraction is hard: on BenchIE, the only OpenIE benchmark listing every acceptable fact surface, **the best system on Earth scores F1 = 0.34** on well-formed English — a deterministic rule-based extractor, every neural system scoring worse (DEV/DESIGN-document-extraction-sota.md §1, citing Gashteovski et al., ACL 2022). The same document shows the biggest jump comes from *having a schema* (~0.34 open-domain → ~0.79–0.82 schema-guided upper bound; ~0.56–0.60 realistic prose) — the argument for the validated ontology, and the admission in the same breath: a real percentage of dense prose yields nothing, and honest zero-yield classification is a shipped feature, not a hidden failure. The document lane's measured state is published, pre-fix baseline included: of six hard litmus targets the pre-fix stack met two — recall p95 of 2,077 ms under drain load among them — and failed four, on the record (`benchmarks/doc-ingest-litmus/README.md`). The parse-model split that fixed document-quality wounds is benchie-gated: +3 gates, junk-rate −4 pp (GLM.md, parse-model split addendum). **[EVIDENCE-PENDING]** a retention score: ingest corpus → delete source → measure knowledge retained and answerable after N days of growth.

---

## The bridge

One organism, six proofs: **corrections** prove it honors truth; **documents** prove it learns; **recall** proves it is fast and auditable; **growth** proves it is open-ended; **isolation and poison-resistance** prove it is safe; **the cortex** proves the agent itself gets smarter. Each is a shipped mechanism in this repository, not a roadmap item — and each has a named gap in the scoreboard below.

## The open scoreboard (what we have not measured yet)

1. A **third-party-comparable benchmark on the real ingest path**. Standing ruling: LongMemEval's litmus — bulk-planting facts into the DB then testing recall — bypasses the entire engine and is structurally dishonest for a system whose differentiator is that path. Honest instrument: naturalistic messy prose → grown memory → naturally-phrased questions, measuring hit quality, correction honoring, latency, token cost (DEV/EVIDENCE-longmemeval-and-methodology.md).
2. **Adversarial poison red-team** against ingest content (today's poison tests cover containment, not content).
3. **Correction precision/recall** on correction-heavy naturalistic streams.
4. **Growth-quality metrics** (walkable-within-N, hollow-rate).
5. **Deletion-test retention** scores.
6. **Yield ledger**: per-corpus extraction yield against the 0.34/0.79 F1 ceiling, published rather than implied.

A category claim survives only if its gaps are as public as its numbers. These six are ours.

## References

**Repository** (HEAD `eea12a11`): `GLM.md` (master doctrine; sections cited inline by title); `DEV/ARCHITECTURE-OVERVIEW.md`; `DEV/QUERY-PIPELINE.md`; `DEV/DESIGN-agent-cortex.md`; `DEV/DESIGN-canonicalization.md`; `DEV/DESIGN-llm-authority-hardening.md`; `DEV/DESIGN-residence-currency-valid-from.md`; `DEV/DESIGN-document-extraction-sota.md`; `DEV/DESIGN-fact-realization-and-voice.md`; `DEV/EVIDENCE-longmemeval-and-methodology.md`; gauntlet evidence under `DEV/review/gauntlet/` (interactive-latency ledger + round_4 notes; inprocess-mcp-client round_1–2 repros); `DEV/BUGS/BUG-recall-door-statement-input-llm-burn-20261026.md`; `benchmarks/doc-ingest-litmus/README.md`; `tests/test_intent_label_regression.py`; `tests/test_cortex_firewall.py`; `tests/test_cortex_lifecycle.py`; `src/wgm/gate.py`; `src/mcp/server.py`; `src/entity_registry/registry.py`; `src/extraction/compound.py`.

**External** (primary sources): OWASP Agentic Application Security, *ASI06: Memory & Context Poisoning*, 2026 Top 10 for Agentic Applications — <https://genai.owasp.org/2026/05/13/memory-is-a-feature-it-is-also-an-attack-surface/>; explainer <https://vectorize.io/articles/owasp-asi06>. Rasmussen et al., *Zep: A Temporal Knowledge Graph Architecture for Agent Memory*, arXiv 2501.13956. Wang, *TOKI: A Bitemporal Operator Algebra for Contradiction Resolution in LLM-Agent Persistent Memory*, arXiv 2606.06240. *Ontology-grounded Automatic Knowledge Graph Construction by LLM*, arXiv 2412.20942. Bian et al., *LLM-empowered knowledge graph construction: A survey*, arXiv 2510.20345. Chhikara et al., *Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory*, arXiv 2504.19413. Packer et al., *MemGPT: Towards LLMs as Operating Systems*, arXiv 2310.08560. Gashteovski et al., *BenchIE*, ACL 2022, <https://aclanthology.org/2022.acl-long.307/>. 2026 comparison wave: Mem0 vs Letta vs Zep head-to-heads (digitalapplied.com 2026-08; particula.tech 2026-09; coworker.ai 2026-08).
