# Changelog

## Unreleased (2026-11-04) — shadow-memory routing: initialize.instructions names the host note tools as NOT FaultLine (port of foss 5bf9e382)

- **The defect (foss user report, 2026-10-24, live stack).** A model routed "save to FaultLine" into the HOST's built-in note tools (`write_note` / `search_notes` / `view_note`) — the data landed in ephemeral host notes, never in the knowledge graph. Nothing at connect time told the model those tools are not FaultLine memory.
- **The port.** A `HOST NOTE TOOLS ARE NOT THIS MEMORY` routing block rides `premise.SERVER_INSTRUCTIONS` — the `initialize.instructions` text both doors serve (http_server + stdio) and `server/discover` re-uses — so native-MCP clients (OpenWebUI ≥0.6.31 surfaces instructions in the system prompt) get the rule before any tool call. Premise 3578 → 3890; ceiling raised 3600 → 3900 (deliberate raise #2, evidence = this port).
- **Adjudicated residual.** Foss also led the canonical recall/remember tool descriptions with differentiation prose. This line's served descriptions sit at 1009/1011 chars against the 1024 OpenAI limit (15/13 chars headroom — measured via `_build_tool_openapi`); growing `TOOL_PREMISE` past 1024 triggers the serving-seam clamp, which silently drops the description tail (the 2026-10 document_status wound). The description-lead surface needs an owner-approved copy pass and was NOT silently widened. Pinned by `tests/mcp/test_shadow_memory_differentiation.py`.

## Unreleased (2026-11-04) — drop the WWW-Authenticate challenge on MCP 401s (port of foss 595d084c)

- **The defect (foss, live public stack).** Returning `WWW-Authenticate: Bearer` on a 401 triggers Claude Desktop's OAuth 2.1 discovery flow: it ignores the configured static bearer key and sends a DCR-generated token instead, so every request 401s. A plain 401 with no challenge keeps static-key auth working.
- **The port.** The challenge header is removed at the single serving seam (`require_auth`'s 401 raise, which all three doors — /mcp JSON-RPC, REST tools, attachment bridge — funnel through). The RFC-9728 posture comment is updated to the stronger form: no challenge header at all, so no client is invited into a flow this server cannot complete. The four pins that asserted the header's presence are flipped WITH this evidence.

## Unreleased (2026-10-25) — MCP brain-independence: recall serves passages regardless of brain state (the MCP-layer twin of the passage seam)

**MCP (`src/mcp/server.py`) — the recall fast path:**
- **The defect (live on prod 2026-10-24).** A direct `POST /query` served 3 document_hits in <10s with the NIM brain rate-limited (passage seam landed), but `recall_memory` through the MCP hung 15s+: its PRE-QUERY brain call — `POST /classify-intent` — wraps three LLM tiers server-side (TIER-2 cue corroboration 4s, the NEG-ENSEMBLE adjudicator `timeout=30s` × retry, the sub-gate INTENT_ADJUDICATION escalation 4s), and the MCP's 10s probe budget let that call alone eat the turn before `/query` ever fired.
- **Bounded probe (Sub-fix A).** The RECALL probe — the classify call `recall_memory` makes — now rides `MCP_CLASSIFY_TIMEOUT_S` (default 3s), passed explicitly at the call site. The shared table default (`MCP_BRAIN_TIMEOUT_CLASSIFY_INTENT`, 10s) is UNCHANGED: the write lanes (remember/retract) consume escalated verdicts whose server-side budgets are 4s+4s, and a 3s ceiling there would time out a healthy brain's escalation and route corrections through the STATEMENT fail-open (round-2 review finding 4). On recall-probe budget exhaustion the seat is marked slow and — on lanes that still need a route (remember_facts opts in via `gliner2_fallback=True`) — ONE re-ask with `skip_llm: true` fetches the backend's DETERMINISTIC answer (spaCy preroutes + GLiNER2 + DB pattern layers; still the brain's route, not a transport guess). If even that fails, the ORIGINAL `BrainUnavailable` surfaces with its own endpoint/cause/attempts — the loud contract is unchanged.
- **Brain-slow breaker.** A PER-SEAT window (`MCP_BRAIN_SLOW_COOLDOWN_S`, default 60s) opens when that seat's classify probe fails and closes on the seat's next healthy full-route classify (half-open recovery — one turn). Per-seat on purpose: SaaS runs per-account BYO brains, so one tenant's rate-limited brain must not fast-path another tenant's healthy turns. `MCP_BRAIN_DEGRADED=1` is the ops-declared explicit arm (global — the operator describes the box).
- **Recall fast path (Sub-fix B).** When the window is open, `recall_memory` SKIPS intent routing and the confidence gate entirely and fires `/query` directly with the raw text (the API's passage-first floor answers regardless of brain state; `/query` parses the text itself). The pre-walk harvest is replaced by bounded deterministic retention (`_episodic_capture` — one INSERT, no LLM; the same degradation the harvest's own degraded arm takes), so `/harvest-spans`'s REFRAME-tailored lanes cannot re-hang the turn after routing was skipped. The retention decision keys on the breaker STATE at harvest time — not the entry-time flag — so the TRIP turn (probe timed out mid-turn) and `RECALL_INTENT_ROUTING=off` turns get it too (round-2 review findings 1/2). With a healthy brain the pipeline is byte-for-byte the full route.
- **Wall awareness (Sub-fix C).** `MCP_TURN_WALL_S` (240s) is documented as the never-exceed CEILING, not a target: a degraded turn aims for the fast-path total (<10s — steady state is 0s routing + the API's own `QUERY_WALK_TIMEOUT_S` bound; the first slow turn pays one 3s probe).

**API (`src/api/main.py`) — `/classify-intent` `skip_llm` flag:**
- Request body `{"skip_llm": true}` gates every LLM tier inside the endpoint: TIER-2 ambiguous cues take the no-agreement fall-through to GLiNER2 (a destructive bypass still requires corroboration), the NEG-ENSEMBLE takes its adjudicator-unavailable fail-open (lone-signal rule enforced, NO grow — an outage must not manufacture evidence), and the sub-gate takes the STATEMENT safety net. All deterministic layers (interrogative/affective/declarative preroutes, GLiNER2, DB patterns/cache, feedback writes) are unchanged. Flag absent → byte-identical legacy behavior.

**Tests:** `tests/mcp/test_recall_fast_path_brain_slow.py` — pins (a) slow-brain <5s + breaker opens, (b) dead-brain passages, (c) fast-brain full pipeline + breaker closed, (d) open breaker → ZERO classify traffic + episodic retention + `/query` direct, (e) end-to-end through the REST door with the turn wall live; plus the degraded-flag arm, half-open recovery, the GLiNER2-only re-ask (both arms), and env-table rows. `tests/test_classify_skip_llm_flag.py` — the three LLM tiers each pinned WITH and WITHOUT the flag (LLM-recorder A/B), ensemble no-grow, gate-passed flag-invariance. Neighbor bars amended where they pin the OLD default (classify 10.0 → 3.0) and extended where the lane adds surface (`_ENV_LIMITS` copy, `_TASK_CREATION_ALLOWLIST` for the fast-path retention task).

**Gates:** semgrep subject-agnostic — 151 findings lane == 151 base, fingerprint-identical sets, ZERO on changed lines. CodeQL CLI absent → LOUD SKIP. Neighbor suites: `tests/mcp/` 630 passed / 5 pre-existing reds (byte-identical on base 197e3a0e, filed as BUG-12); `tests/test_mcp_no_silent_route_downgrade.py + test_inprocess_mcp_client.py` 153 passed / 6 pre-existing reds (the base corpus-boot + floor-table set).

## Unreleased (2026-10-25) — P0 passage seam: the C tier answers regardless of brain state

**API (`src/api/main.py`) — `/query` passage-first + bounded walk (BUG-01/BUG-02/BUG-07, DEV/BUGS/adversarial-bench-20261024/):**
- **Passage-first (BUG-01, BUG-02).** `document_hits` is computed BEFORE the graph walk, on a dedicated executor (`DOCUMENT_HITS_LANE_WORKERS`, default 8 — never sharing anyio's walk pool), and committed to every response path: walk in budget → both surfaces; walk slow → floor served; walk failed → floor served. The old concurrent-future shape silently arrived `document_hits: []` on exactly the slow turns that needed the lane (bench: 226–887s responses with 0 hits while the same SQL returned 3 in isolation) and died with the response it rode on under partial brain outage (96% of bench questions got nothing).
- **Bounded walk (BUG-07).** The walk runs under `asyncio.wait_for` with `QUERY_WALK_TIMEOUT_S` (default 30s) over `anyio.to_thread.run_sync(..., abandon_on_cancel=True)`: on expiry — or a client disconnect — the await ends, the worker thread is abandoned (pool token released immediately; the blocking call itself finishes discarded), and the response is served from the floor. A walk-internal `TimeoutError` is told apart from budget expiry by the elapsed clock (`walk_failed` vs `walk_budget_exceeded`). Invalid/non-positive env values fall back to the default with a warning.
- **Bounded lane.** The passage read itself is bounded by `DOCUMENT_HITS_LANE_TIMEOUT_S` (default 5s) so a wedged Postgres cannot become the new head-of-line blocker passage-first would otherwise create; a shed lane is an honest `document_hits: []` + `document_hits_lane_timeout` alert.
- **MCP contract honored.** `error` is set only when there is NO floor to serve — the MCP client's `backend_soft_failure` drops every error-bearing body's passages, so an error set alongside hits would recreate BUG-01 one field up; with no floor, `error` MUST be set so the empty 200 never reads as "you have no facts". Fault alerts (`walk_budget_exceeded`, `walk_failed`, `document_hits_lane_timeout`) ride the existing `alerts` array for HTTP callers.
- **Fault ≠ miss.** A shed or failed walk neither sets `escalated` nor writes recall_miss markers — "could not look" is not "looked and found nothing" (round-2 critic MINOR 1 rule extended to the shed case).
- **Ops contract.** Keep `QUERY_WALK_TIMEOUT_S` below the MCP client's `MCP_BRAIN_TIMEOUT_QUERY_WALK` (default 30s) minus ~2s, or the response lands after the client gives up — see DEV/BUGS/adversarial-bench-20261024/BUG-11-mcp-walk-budget-races-api-walk-budget.md.

**Tests:** `tests/test_query_passage_seam_p0.py` — 10 pins via `tools/fltest.py`: (a) slow brain → passages <5s; (b) dead brain → passages (and honest error when no floor); (c) fast brain → both surfaces; (d) budget exceeded → floor complete, no markers; (e) 10 concurrent slow-brain queries → all 10 get their own passages; (f) hung lane → response still lands; plus lane-before-walk ordering, R7 marker regression, env parsing. PG-backed R7 marker-DB pins in `tests/test_recall_miss_escalation.py` skipped on boxes without a control-plane DB (honest skip).

**Gates:** semgrep `p/secrets` + `p/python` → 0 findings on changed files (3 taint-rule engine timeouts reproduce identically on base content at the same path — path flake, not content; `p/python` re-run with raised timeout killed by shell budget and documented in the review record). CodeQL CLI absent on this host → LOUD SKIP. 15 pre-existing test failures verified byte-identical on base commit f54c9d59 (filed as BUG-10).

## Unreleased (2026-08-03) — MCP Safety + UX Pass

**MCP layer (`src/mcp/server.py`):**
- **Safety — `retract_fact` STATEMENT mis-pick no longer destroys data.** A model that calls `retract_fact` with text the brain classifies as STATEMENT was previously force-routed to RETRACTION (destructive delete of data the user meant to store). It now redirects to `remember_facts` (ingest), failing toward data preservation. CORRECTION and RETRACTION paths are unchanged. The `confidence < gate` low-confidence fallback is preserved. No redirect loop: the `remember_facts` → `retract_fact` re-entry carries `classified_intent`, skipping the classify block.
- **UX — `remember_facts` `no_ingest` returns are now directive.** The two `no_ingest` returns ("Text too short for fact extraction" and "No memorable fact detected — nothing stored.") now emit `"Respond normally; do not mention memory or storage."` so weak models do not parrot system-failure state. The diagnostic cause is logged via `_log` before each return.
- **UX — `recall_memory` appends a re-query hint on non-empty recall.** `"\n\nIf their message touched on other distinct topics, recall each before responding."` — model-facing GUIDANCE for compound messages, not a backend-state claim (the backend `/query` returns no `more_available` field, so an MCP boolean would be unsound). The empty-recall sentinel `{"memory": "No relevant facts found."}` is UNCHANGED (benchmark contract: `benchmarks/harness/owui_pathway.py:128,323`).

**Tests:**
- `tests/mcp/test_server.py` gained 3 regression tests + 2 mock helpers covering the STATEMENT-redirect safety fix (the directive `no_ingest` and non-empty-recall re-query hint were verified via standalone scripts, not committed pytest tests — noted as a coverage gap, not claimed as tested here).

**Withdrawn / superseded:**
- A proposed `recall_memory` query-specificity hint was withdrawn: the dev tree's verbatim-passthrough `query` Field description (`src/mcp/http_server.py:40` — "copy VERBATIM, never reduce to a keyword") already solves specificity in the brain-not-transport direction. Adding an MCP-layer hint would fight that design.

## v1.1.0 (2026-06-01) — MCP Native Server + Security Hardening

**MCP Support:**
- Native MCP server (stdio + HTTP/Streamable transport) — `src/mcp/`
- Three high-level tools: `recall_memory`, `remember_facts`, `retract_fact`
- Bearer token auth on HTTP transport (`MCP_API_KEY`)
- `faultline-mcp` Docker sidecar in all compose variants
- `FAULTLINE_USER_ID` single-user mode for Claude Desktop
- Transparent provisioning on first tool call per user
- Protocol version 2025-03-26

**Security Hardening:**
- UUID format validation in schema name construction (`derive_user_slug_from_uuid`)
- Entity name length cap (256 chars) — eliminates injection payload viability
- 7-pattern prompt injection filter at MCP tool boundary
- Memory block structural framing (`[/FAULTLINE_MEMORY]` closing tag)

**Per-User Architecture:**
- Migrations 050-059: per-user PostgreSQL schema provisioning system
- Background provisioning worker, heartbeat monitoring
- Per-user extraction patterns, intent classes, preference signals

**Compose Variants:**
- Added: `docker-compose-portainer.yml`, `docker-compose-portainer-withoutqdrant.yml`
- Added: `docker-compose.local.external.yml`, `docker-compose.local.internal.yml`
- All variants include `faultline-mcp` sidecar service

## v1.0.9 (2026-05-13) — Context-Enriched /extract Endpoint (dBug-018)

**Extraction pipeline:** `/extract` endpoint now passes database-sourced context to GLiNER2: entity registry (known entities), ontology metadata (rel_types with type constraints), and user facts. All context sourced fresh from database per request (zero hardcoding, zero stale caches).

**Phase A (dprompt-80):** Entity registry enrichment — ExtractContext model, _build_extract_context() helper querying entity_aliases/rel_types/facts fresh from DB. GLiNER2 schema enriched with known entity names, user profile, ontology hints.

**Phase B (dprompt-81):** Body parts taxonomy + ontology-driven post-processing — Migration 026 pre-seeds body_parts taxonomy. Post-processing uses rel_types.tail_types for entity type-aware resolution.

**Fixes:** dBug-018 (context-dependent extraction: "what'd I do to my back?" now extracts subject=user, object=back instead of nulls). Implicit pronouns ("I" → user) now resolved. Body parts recognized via ontology.

**Migration:** 026_body_parts_taxonomy.sql (idempotent, pre-seeds 15 body parts with deterministic UUIDs)

**Constraint:** dprompt-79 — All context to GLiNER2 from database on each request. Zero hardcoding, zero stale caching. Context evolved with ontology automatically.

## v1.0.8 (2026-05-15) — Bidirectional Relationship Completeness

**Extraction + ingest:** Two-phase fix. Prompt mandates bidirectional emission (parent_of/child_of, spouse, sibling_of). Ingest auto-creates missing inverses with same confidence/fact_class. Graph traversal now complete in both directions.

**Fixes:** dBug-012

## v1.0.7 (2026-05-14) — Query Deduplication Fix

**Query path:** pg_keys dedup uses UUIDs (`_subject_id`/`_object_id`) instead of display names. Fixes duplicate facts when same entity has multiple aliases (chris/user → single deduplicated fact).

**Fixes:** dBug-report-008

## v1.0.6 (2026-05-13) — Metadata-Driven Validation

**rel_types table:** Added metadata columns (`is_symmetric`, `inverse_rel_type`, `is_leaf_only`, `is_hierarchy_rel`). `_get_rel_type_metadata()` with caching replaces all hardcoded validation constants. New rel_types self-describe constraints without code changes.

**Migration:** 022_rel_types_metadata.sql (idempotent column expansion + metadata pre-population).

## v1.0.5 (2026-05-13) — Bidirectional Relationship Validation

**Ingest pipeline:** `_validate_bidirectional_relationships()` prevents impossible bidirectional relationships (`child_of` + `parent_of`). Keeps higher confidence, supersedes lower. Checks `facts` + `staged_facts`.

**Fixes:** dBug-report-006 (bidirectional impossibilities, staged gap)

## v1.0.4 (2026-05-12) — Query Deduplication & Alias Metadata

**Query path:** `/query` deduplicates facts by `(subject_uuid, rel_type, object_uuid)` after merge, keeping highest confidence per triple. `_aliases` metadata attached to each fact with all entity names and `is_preferred` flag.

**Effect:** No more duplicate facts from alias redundancy (christopher spouse mars + chris spouse mars → single deduplicated fact). Filter gets clean results.

**Fixes:** dBug-report-005 (alias redundancy in query results)

## v1.0.3 (2026-05-12) — Semantic Conflict Detection

**Ingest pipeline:** `_detect_semantic_conflicts()` added before Class A/B/C commit. Auto-supersedes ownership/relationship facts when the object entity is already defined as a type/category via hierarchy rels.

**Principle:** The graph IS the source of truth. If `X instance_of Y`, Y is a TYPE — don't allow `owns`/`has_pet`/`works_for` on type entities. Graph self-heals.

**Fixes:** dBug-report-003/004 (type/ownership conflict cleanup)

## v1.0.2 (2026-05-12) — Hierarchy Extraction Enhancement

**Filter prompt:** Hierarchy relationships (`instance_of`, `subclass_of`, `member_of`, `part_of`) moved to primary extraction list in `_TRIPLE_SYSTEM_PROMPT` with 6 multi-domain examples.

**Changes:**
- Added HIERARCHY RELATIONSHIPS section to Filter prompt with definitions and chain examples
- Moved `instance_of`, `subclass_of`, `member_of`, `part_of` from absent/weak to "Common" (primary) extraction list
- 6 multi-domain examples: taxonomic, organizational, infrastructure, hardware, geographical, software

**Fixes:**
- dBug-report-002: Hierarchical Entity Relationships Missing
- `part_of` previously had 0 facts — now extracted with Class B confidence
- `instance_of` and `member_of` now extracted with complete multi-link chains

## v1.0.1 (2026-05-12) — Filter Simplification

**Architecture:** Backend-first query design. Filter no longer implements three-tier gating — it trusts backend /query ranking (Class A > B > C + confidence).

**Changes:**
- Removed Tier 1/2/3 relevance gating logic from Filter (`openwebui/faultline_filter.py`)
- Removed `entity_types` parameter passing (Filter no longer re-gates on entity type)
- Removed Concept/unknown entity filtering from Filter
- Simplified `_filter_relevant_facts()` to: identity rels always pass + confidence threshold

**Fixes:**
- dBug-report-001: Tier 2 Identity Fallback Blocks Tier 3 after Concept Filter
- Category queries ("our pets", "my family") now return complete fact sets
- No UUID leaks in responses for category queries

**See:** docs/ARCHITECTURE_QUERY_DESIGN.md

## v1.0.0 (2026-05-12) — Initial Production Release

Initial production release of FaultLine — a write-validated knowledge graph pipeline with GLiNER2 entity typing, WGM ontology validation, three-class fact classification (A/B/C), graph + hierarchy traversal, self-building ontology, and OpenWebUI filter/function integration.
