from typing import Optional, Union
from datetime import datetime

from pydantic import BaseModel


class EdgeInput(BaseModel):
    subject: str
    object: str
    rel_type: str
    is_preferred_label: bool = False
    is_correction: bool = False
    confidence: Optional[float] = None  # User corrections: 1.0. Default: None (ingest computes based on provenance)
    fact_provenance: str = "llm_inferred"  # user_stated | llm_inferred | llm_learned
    subject_type: Optional[str] = None  # Person, Animal, Organization, Location, Object, Concept (from GLiNER2)
    object_type: Optional[str] = None  # Person, Animal, Organization, Location, Object, Concept (from GLiNER2)
    object_datatype: Optional[str] = None  # SCALAR-TYPE discipline (migration 101): datatype label
    #   from the atomic detector (ipv4|mac|email|cidr|fqdn|url|phone|date|uuid|...). Threaded into
    #   entity_attributes.datatype at ingest. None → fall back to the rel_type's scalar_datatype.
    definition: Optional[str] = None  # semantic definition of rel_type, LLM-generated at extraction time (dprompt-85)
    temporal_context: Optional[str] = None  # dBug-055: Text qualifier ("in 4 days", "next Tuesday", etc.)
    temporal_context_resolved_at: Optional[str] = None  # ISO 8601 timestamp when temporal expression resolves

    # TEMPORAL FACT METADATA (Issue #5)
    statement_date: Optional[str] = None  # ISO 8601 when user says fact is/was/will be true (e.g., "2024-05-01")
    valid_until: Optional[str] = None  # ISO 8601 when fact expires/was superseded (e.g., "2024-08-15")
    temporal_confidence: Optional[float] = None  # 0.5-0.95 confidence in date extraction (explicit: 0.95, implicit: 0.50)

    # PER-EDGE EVENT DATE (occurrence-reification keystone): when a seam reifies a per-occurrence
    # entity (an EVENT occurrence) it knows THIS occurrence's OWN date — parsed from ITS OWN clause
    # — which the single request-level date cannot represent in a multi-event turn. When set, the
    # row-build site hosts THIS date on the occurrence's participated_in edge INSTEAD of smearing
    # the request-level date (the multi-event single-date-per-request collapse fix). None → the
    # request-level temporal_class gate decides (today's behavior). Granularity is the TRUE
    # resolver-determined precision (year/month/day/…), never a hardcoded "day".
    event_date: Optional[str] = None              # ISO 8601 — THIS occurrence's own event date
    event_date_granularity: Optional[str] = None  # year | month | day | … (true resolver granularity)

    # ASSERTION POLARITY (Q1 — ConText/NegEx assertion model). The polarity of THIS fact as the
    # user asserted it: 'affirmed' (default) or 'negated'. Set 'negated' for a NEGATED genuine STATE
    # ("the GPS is not functioning") so the fact reads back NEGATED, never as its positive opposite.
    # This is NOT a correction/retraction (those are routed by the intent gate BEFORE extraction and
    # never produce an edge). Threaded into facts/staged_facts.polarity at ingest, exactly like
    # temporal_status/event_date ride their own columns. Mirrors the facts.polarity DEFAULT.
    polarity: str = "affirmed"                     # affirmed | negated

    # PRESUPPOSED (Frege-Strawson backgrounding). True when the deriver captured this fact from
    # BACKGROUNDED content rather than from what the clause ASSERTS — the possessive/definite
    # presupposition of "my favourite colour", which survives negation and questioning ("What is my
    # favourite colour?" still presupposes the user HAS one). SEP "Presupposition" (Beaver, Geurts &
    # Denlinger 2021, §2 "The Frege-Strawson tradition"): presupposed information is "taken for
    # granted, rather than being part of the main propositional content of a speech act", and
    # possessives are listed triggers. The deriver has computed this flag all along; it had no field
    # to ride on, so it was DROPPED at the transport and backgrounded content committed at Class A /
    # confidence 1.0 identically to asserted content. Consumed by assign_class_and_confidence under
    # PRESUPPOSED_TIER_B. Default False → every existing producer is unchanged.
    presupposed: bool = False

    # PER-EDGE TEMPORAL STATUS OVERRIDE (relocation / past-habitual residence). Normally
    # temporal_status (now|past|future) is derived at request level from the parsed event_date vs now
    # (purely date-driven). A GRAMMATICALLY-past residence ("I used to live in London") carries NO
    # event_date, so the date-driven detector reads it 'now'. A deriver chain that recognizes such a
    # grammatical past marker sets this override to 'past'; the ingest row-build PREFERS it over the
    # request-level status. None → the request-level derivation (every normal edge). Mirrors how
    # polarity/event_date ride their own fields.
    temporal_status: Optional[str] = None          # now | past | future (per-edge override)


class ExtractContext(BaseModel):
    known_entities: list[dict] | None = None  # [{"name":"${USER}","type":"Person","uuid":"..."},...]
    ontology_hints: list[str] | None = None    # ["has_injury → Person,body_part", ...]
    user_profile: str | None = None            # "User: ${USER}. Family: spouse=${SPOUSE}..."


class IngestRequest(BaseModel):
    text: str
    source: str = "api"
    edges: list[EdgeInput] | None = None
    known_types: list[str] = ["Person", "Organization", "Location", "Event", "Concept"]
    user_id: Optional[str] = "anonymous"
    chat_id: Optional[str] = None  # dBug-016: Preserve OpenWebUI conversation context
    context: ExtractContext | None = None  # Optional context enrichment for /extract (dBug-018)
    memory_facts: list[dict] | None = None  # Prior facts for pronoun resolution during extraction
    is_correction: bool = False  # dBug-041: User correction flag — bypass blocklist validation
    idempotency_key: Optional[str] = None  # Phase 2: Deduplicate retried requests via idempotency cache
    source_ref: Optional[str] = None  # Migration 128: citable provenance (URL/filename/title) from the ingest_document lane; NULL for conversational facts


class EntityResult(BaseModel):
    entity: str
    label: str
    canonical_id: str


class FactResult(BaseModel):
    subject: str
    object: str
    rel_type: str
    status: str
    fact_class: str = "A"  # A, B, or C
    provenance: str = "llm_inferred"
    definition: Optional[str] = None  # Natural language template from rel_types table (e.g., "X is Y's spouse")
    category: Optional[str] = None  # Category from rel_types.category (family, work, location, etc.)


class IngestResponse(BaseModel):
    status: str
    committed: int
    staged: int = 0  # Facts written to staged_facts (Class B + C)
    entities: list[EntityResult]
    facts: list[FactResult]
    error: Optional[str] = None  # Error message when status != "ok"


class RelTypeRequest(BaseModel):
    rel_type: str
    label: str
    subject_role: str = "entity"
    object_role: str = "entity"
    correction_behavior: str = "supersede"
    wikidata_pid: Optional[str] = None
    # Metadata for classification routing (dprompt-97)
    head_types: Optional[list[str]] = None  # subject entity types (e.g., ['Person', 'Organization'])
    tail_types: Optional[list[str]] = None  # object entity/value types (e.g., ['SCALAR'], ['Person'])
    is_symmetric: Optional[bool] = None     # bidirectional (spouse, knows, same_as)
    inverse_rel_type: Optional[str] = None  # reverse relationship (parent_of ↔ child_of)
    is_hierarchy_rel: Optional[bool] = None # classification/taxonomy (instance_of, subclass_of)
    # ── WHERE the write lands (see POST /ontology/rel_types) ───────────────────────────
    # `user_id` names the TENANT whose ontology is being asserted against; the handler
    # derives `faultline_<slug>` from it and binds `SET search_path TO <schema>` WITHOUT
    # `public`, exactly as `DeactivateCueRequest` does for the cue lane. Required for the
    # default (`scope="tenant"`) lane.
    user_id: Optional[str] = None
    # `scope` is the seed-template ESCAPE HATCH and defaults to the tenant lane. Only the
    # literal "public" targets `public.rel_types` — the template every future tenant is
    # provisioned from — and that lane additionally demands the operator credential. It is
    # opt-in and explicit so a `public` write can never be the accidental default of an
    # unbound connection (which is precisely how it behaved before).
    scope: Optional[str] = None


class DeactivateCueRequest(BaseModel):
    """Retire (or restore) ONE engine-grown / seeded linguistic cue for ONE tenant.

    The target is the table's own UNIQUE key `(cue, category)` -- an EXACT identifier the
    caller must have read from `/structure/grown` first. It is deliberately NOT free text:
    a free-text target would need matching, and matching is the seam that turns a structure
    edit into a memory deletion. `reactivate=True` is the inverse (is_active back to true),
    which is what makes the whole lane reversible.
    """
    user_id: str
    cue: str
    category: str
    reactivate: bool = False


class RetractRequest(BaseModel):
    user_id: str
    subject: Optional[str] = None
    rel_type: Optional[str] = None
    old_value: Optional[str] = None
    scope: Optional[dict] = None


class RetractResponse(BaseModel):
    status: str
    retracted: int
    mode: str
    note: Optional[str] = None
    scope_level: Optional[str] = None


class StoreContextRequest(BaseModel):
    text: str
    user_id: str = "anonymous"
    source: str = "openwebui"
    context_type: str = "unstructured"


class StoreContextResponse(BaseModel):
    # "stored" | "disabled" | "deferred" (embed/Qdrant failed — text degraded to
    # episodic_log, not yet vector-indexed) | "error"
    status: str
    point_id: str  # Qdrant point UUID ("" unless status == "stored")


class EpisodicAppendRequest(BaseModel):
    """Append one verbatim ingest input to the per-tenant episodic_log.

    Durable, append-only raw-text safety net captured BEFORE extraction so that
    short fragments, misrouted queries, and no-triple ramblings are never lost.
    Purely additive — does NOT touch the WGM gate, class assignment, or query scope.
    """
    user_id: str
    raw_text: str
    source: str = "mcp"
    source_ref: Optional[str] = None
    intent: Optional[str] = None
    extracted_fact_count: Optional[int] = None
    # Per-turn idempotency key (migration 274): generated ONCE per turn by the client and carried
    # by every retry attempt of that turn. Enforced by a partial UNIQUE index, so two in-flight
    # attempts converge on one row and the second attempt's answer is the read-back. Optional:
    # a keyless writer keeps the plain append.
    turn_key: Optional[str] = None


class ArtefactPlacement(BaseModel):
    """ONE embedded image's placement + the deterministic verdicts computed about it.

    Every field here was derived WITHOUT a model of any kind: the box comes off the PDF's
    own content stream, the caption from page GEOMETRY (`src/ingest/artefact_geometry.py`),
    the chunk index from locating that caption verbatim in the chunked text. A decline is
    carried explicitly (`caption_declined_reason`) because a decline is a RESULT, not a
    failure — it is what lets recall say "I kept this figure but could not tell which text
    describes it" instead of asserting a neighbour's caption.
    """
    page_index: int
    artefact_index: int
    bbox: list[float] = []            # x0, top, x1, bottom — TOP-ORIGIN, pdfplumber's
    page_size: list[float] = []       # width, height
    caption_text: Optional[str] = None
    caption_confidence: Optional[float] = None
    caption_method: Optional[str] = None
    caption_declined_reason: Optional[str] = None
    chunk_index: Optional[int] = None
    chunk_bind_method: Optional[str] = None


class ArtefactRetainRequest(BaseModel):
    """Persist a retained upload + its placements into the per-tenant `artefacts` table.

    The BYTES arrive here because this process is the one that binds the tenant schema
    (`SET search_path TO faultline_<slug>` WITHOUT public). The MCP door does the
    deterministic compute (sniff, extract, geometry, caption, chunk tie) and never touches
    the database — that separation is deliberate, and it is why this lane adds NO new
    isolation boundary: it reuses the existing tenant-derivation chokepoint exactly as
    `/documents/enqueue` does.

    `data_b64` carries the container file. Base64 is used on this INTERNAL hop only (the
    public door takes raw bytes) because the hop is JSON over the container network and
    the 33 % inflation buys a single uniform request shape.
    """
    user_id: str
    media_type: str
    data_b64: str
    filename: Optional[str] = None
    source_ref: Optional[str] = None
    document_id: Optional[int] = None
    placements: list[ArtefactPlacement] = []


class DocumentEnqueueRequest(BaseModel):
    """Enqueue a chunked document into the per-tenant async ingestion registry.

    The flagship `ingest_document` lane chunks the document deterministically
    (server-side, no LLM) and posts the chunk list here; this endpoint writes ONE
    `documents` row status='pending' (chunks retained verbatim) and returns fast.
    The re_embedder poll loop drains it and runs the per-chunk hybrid extraction.
    Purely additive — no WGM gate / class assignment / query scope at enqueue time.
    """
    user_id: str
    chunks: list[str]
    chunk_count: int = 0
    source_ref: Optional[str] = None
    title: Optional[str] = None
    truncated: bool = False
    # DOCLOSS-A part accounting: a document larger than the per-row chunk bound is
    # SEGMENTED across consecutive rows instead of having its tail discarded. These
    # describe WHICH slice of the original document this row carries, so the terminal
    # signal read off the registry can state the truth about the whole document.
    # Defaults reproduce a single-part document exactly (legacy callers unchanged).
    part_index: int = 0
    part_count: int = 1
    total_chunks: int = 0


class LearnTopicRequest(BaseModel):
    topic: str
    user_id: str = "anonymous"
    source_text: Optional[str] = None   # pre-fetched content from a URL
    source_url: Optional[str] = None    # informational, logged but not fetched again


class RewriteRequest(BaseModel):
    """Request for LLM-based fact extraction (triple rewriting).
    Called by OpenWebUI Filter instead of hitting OpenWebUI's LLM directly.
    FaultLine controls which LLM to use and manages all LLM configuration."""
    text: str
    user_id: Optional[str] = "anonymous"
    chat_id: Optional[str] = None  # dBug-016: Preserve OpenWebUI conversation context
    messages: list[dict] | None = None  # Prior conversation context
    typed_entities: list[dict] | None = None  # Pre-extracted entities from GLiNER2
    memory_facts: list[dict] | None = None  # Prior facts for pronoun resolution
    force_relation_extraction: bool = False  # DOC LANE: force LLM relation extraction even when the global flag is detect-only (interactive path unaffected)
    # DOC LANE: per-request override of SPINE_DETERMINISTIC_SEGMENTATION for /harvest-spans.
    # None (every caller today) → the process-wide flag decides → byte-identical behaviour.
    # True → the spine segments with the deterministic decomposer and escalates to the LLM
    # atomizer ONLY for units whose parse still shows an un-decomposed second assertion.
    # WHY a per-REQUEST field rather than one global flag: the atomizer's prompt and budget are
    # sized for "one short chat message" (LLMMaxTokens REFRAME=256, LLMTimeouts REFRAME=6.0s),
    # which a multi-sentence document chunk overruns — while a chat turn still benefits from it.
    # The shape of the INPUT differs per caller, so the decision belongs on the call, not the process.
    deterministic_segmentation: Optional[bool] = None


class RewriteResponse(BaseModel):
    """LLM-extracted edges (facts) from input text."""
    status: str  # "success" or "error"
    edges: list[EdgeInput] = []  # Extracted facts with types


class FactCorrectionRequest(BaseModel):
    """User correction: old fact is wrong, new fact is right.
    Surgical update: only supersede one specific fact, re-ingest through WGM gate.
    """
    text: str  # "Rex is a dog not a bunny"
    user_id: str  # User UUID (will be validated against authenticated user)
    intent: Optional[str] = None  # GLiNER2 classification from Filter: CORRECTION or RETRACTION
    context_facts: Optional[list[dict]] = None  # Recent facts for entity resolution
    idempotency_key: Optional[str] = None  # Deduplicate retried correction requests (via Redis)
    # AUTHORSHIP (default True): the correction was EXPLICITLY routed by the model through
    # the retract_fact tool -- the model's attestation that a human said it, so the
    # superseding rows are written user_stated / Class A / confidence 1.0 exactly as
    # historically. False marks an UNATTESTED lane (recall's auto-detected CORRECTION
    # divert): the same supersede mechanics run, but the superseding rows land
    # llm_inferred / Class B / 0.7 -- a machine side-effect never claims the human said
    # it, and the ladder to A stays open via a later explicit correction or remember_facts.
    attested: bool = True


class FactCorrectionResponse(BaseModel):
    """Surgical correction result."""
    status: str  # "corrected", "failed", "disambiguation_needed"
    subject_uuid: Optional[str] = None
    subject_name: Optional[str] = None
    old_rel_type: Optional[str] = None
    old_value: Optional[str] = None
    new_rel_type: Optional[str] = None
    new_value: Optional[str] = None
    dimension: Optional[str] = None  # SCALAR | RELATIONAL | HIERARCHICAL | SUBJECT | REL_TYPE | ENTITY_TYPE
    confidence: float = 0.0
    facts_superseded: int = 0
    hierarchies_modified: list[str] = []
    message: Optional[str] = None
    error: Optional[str] = None


# Phase 1: Query Redesign Models
class ConversationMessage(BaseModel):
    """Single message in conversation history."""
    role: str  # "user" or "assistant"
    content: str
    timestamp: Optional[datetime] = None


class QueryPath(BaseModel):
    """Determines which database paths to query based on keywords.

    Carries the single declarative SCOPE object resolved once in determine_path()
    (DESIGN-query-scope-resolution.md, Pillar 1). All structured sources are
    PROJECTED by `allowed_rels`; Qdrant is the only source that passes through the
    admission backstop. This is the one place that says "a fact is returned iff it
    satisfies the scope."
    """
    scalar_rels: list[str] = []
    relationship_rels: list[str] = []
    taxonomy_groups: list[str] = []
    traversal_depth: int = 1
    fetch_all_details: bool = False

    # ── Forward-projection scope (Pillar 1) ───────────────────────────────────
    # member_types: entity types (from entity_taxonomies.member_entity_types) that
    #   a foreign entity must classify as for a node-gate pass. Empty = no type
    #   constraint (e.g. unscoped queries).
    member_types: list[str] = []
    # direction: hierarchy traversal direction implied by the query (Pillar 1b).
    #   "down" = membership/contents (default), "up" = classification.
    direction: str = "down"
    # termination: where traversal ends — "entity" (expand to member entities),
    #   "scalar" (terminate at values), or "mixed". Hint for downstream expansion.
    termination: str = "entity"
    # scope_active: True when a concrete scope was resolved (taxonomy or rel match).
    #   When False, scoping is inert and behaviour matches the legacy fetch-all path.
    scope_active: bool = False
    # axis: which of the TWO orthogonal hierarchies the query walks (DESIGN-hierarchy-
    #   ladder §"Query model — axis-scoped deterministic walk"). The two axes meet at
    #   the entity but a question picks ONE:
    #     "membership"     → membership/composition rels (parent_of, has_pet, member_of,
    #                        part_of, …) — "tell me about my family / my pets / my network".
    #     "classification" → the is_hierarchy_rel set (instance_of, subclass_of) —
    #                        "what is Rex / my animals / what kind of …".
    #     None             → unresolved / not applicable (legacy behaviour preserved).
    #   Resolved deterministically from the scope's defining rels (is_hierarchy_rel) +
    #   minimal "what is / what kind" intent cues. Metadata-driven, no hardcoded rel
    #   name lists. The membership axis EXCLUDES classification facts of reached members
    #   ("Rex instance_of poodle" is a different question), and vice-versa.
    axis: Optional[str] = None
    # nesting_rels: the NESTING/sub-grouping rel_types a resolved taxonomy declares via
    #   its own `transitive_rel_types` ∪ the defining rels of its `member_taxonomies`
    #   (e.g. family ⊃ pets via has_pet). These are the structural edges that anchor a
    #   sub-group's members UNDER the parent group — the user's OWN `has_pet Rex`
    #   membership edge that hangs the nested `pets` sub-tree off `family`.
    #
    #   Kept SEPARATE from relationship_rels (and therefore OUT of allowed_rels) so the
    #   concept-projection semantics ("has_* excluded") are unchanged for non-membership
    #   queries. They are re-admitted ONLY on the membership axis, by the staged+facts
    #   anchor projection in fetch_facts_from_anchor — so the deterministic walk can
    #   descend family→pets→Rex via the CORRECT structural edge.
    #
    #   Metadata-driven (read from the taxonomy row, no rel literal) and subject-agnostic
    #   (network ⊃ subnets, body ⊃ parts behave identically). Empty for a plain concept/
    #   temporal query → that projection is byte-for-byte unchanged.
    nesting_rels: list[str] = []
    # aspect_bound_rels: rels the query's OWN possessed noun phrase resolved to through the
    #   tenant's grown place-index (rel_type_aliases: "my email address" → email_address →
    #   has_email — the row `_ground_aspect_term_place` wrote at ingest). A fact on one of
    #   these rels is on the question's topic BY CONSTRUCTION (the tenant's ontology bound
    #   the user's own term to it), so the lexical topic gate must not re-judge it — the
    #   same authority the taxonomy-scope skip already carries. Subset of allowed_rels.
    #   Empty when no possessed NP resolved → every consumer is byte-for-byte unchanged.
    aspect_bound_rels: list[str] = []
    # hierarchy_intent: True when the QUERY REFERENCE asks for a hierarchy / map / full
    #   grouping and the walk should DESCEND the containment/membership tree from the
    #   anchor ("the network hierarchy under dc-toronto", "the org tree from acme",
    #   "tell me about my family" — a grouping WORD in the query). False for a bare
    #   single-entity reference ("tell me about core-1"), which returns the anchor's
    #   TIGHT neighbourhood (own edges + scalars + immediate container + its type) and
    #   does NOT climb the full type ladder or fan the tree out to siblings.
    #
    #   BREADTH is thus reference-determined, NOT a fixed policy for every concrete
    #   anchor. Set in determine_path from (a) minimal hierarchy/map SURFACE cues and
    #   (b) a taxonomy resolved by a grouping WORD in the query text. Subject-agnostic
    #   (names the SHAPE of the answer, never a domain). Consumed by
    #   fetch_facts_from_anchor to gate the container-seeded descent + the multi-rung
    #   ladder climb. Default False → a plain query stays tight unless it references a
    #   hierarchy; user-anchored recall (anchor == user) descends regardless.
    hierarchy_intent: bool = False
    # aspect_grown: when the aspect-synonym GROWTH engine (determine_path inline path)
    #   maps a novel query aspect word to one of the anchor's ACTUAL scalar attributes on
    #   a MISS ("tall" → height), it (a) writes the per-tenant rel_type_aliases link so
    #   every future query reads it deterministically (model-free) and (b) resolves THIS
    #   query in-place by admitting the attribute to scalar_rels. This field carries the
    #   grown canonical attribute for observability/tests only — None on the steady-state
    #   (deterministic) path and on a non-grow miss. NOT part of allowed_rels.
    aspect_grown: Optional[str] = None
    # when_pinned_entity_ids (issue #17, when-ask object-pinning): the SPECIFIC entity
    #   ids a when-interrogative names ("When did I start taking METFORMIN?" → the
    #   metformin entity), resolved in determine_path from the query's own concept
    #   tokens against THIS tenant's alias registry (the same grounding
    #   `_query_scoped_to_absent_concept` uses). When non-empty, a dated row riding the
    #   aspect-bound exemption must BELONG to one of these entities (its subject or
    #   object slot) — rel-granular breadth (#15) is kept ONLY for entity-less shapes
    #   ("When did I take anything?"). L4 places (type nodes) and the speaker are never
    #   pinned. Empty → byte-for-byte the #15 behaviour.
    when_pinned_entity_ids: list[str] = []
    # measure_axis_admission (issue #17, measure-axis grown-rung voice): True when the
    #   measure-interrogative admission (#13/#11) actually resolved the axis from the
    #   anchor's own stored rows (`measure_interrogative_aspect` fired). The render then
    #   gives the ASKED CHAIN's grown classification rungs their voice back on THIS
    #   question — they render in the HELD band (their own provenance), the same
    #   contract the ladder question already carries, without touching the growth gate
    #   or what the walk admits. False (default) → the rung-silencing gate is
    #   byte-for-byte unchanged.
    measure_axis_admission: bool = False

    @property
    def allowed_rels(self) -> set[str]:
        """The single defining rel set for this query's concept (projection key).

        Union of scalar + relationship rels (already taxonomy-expanded and
        inverse-expanded in determine_path). Structural rels (member_of,
        instance_of, has_*) are deliberately NOT included here — they are an
        explicit, separately-flagged lane (see fetch_facts_from_anchor), never a
        silent union into concept scope.
        """
        return {r.lower() for r in (self.scalar_rels + self.relationship_rels)}


class QueryRequest(BaseModel):
    """Updated QueryRequest with conversation history for Phase 1."""
    text: str
    source: Optional[str] = "openwebui"
    user_id: Optional[str] = "anonymous"
    conversation_history: Optional[list[ConversationMessage]] = None
    known_entities: Optional[dict[str, str]] = None  # {name: uuid}

    # TEMPORAL QUERY SCOPE (Issue #5)
    temporal_scope: Optional[str] = None  # ISO date or date range: "2024-05-01" or "2024-01-01/2024-12-31"
                                         # When set, filters facts to only those valid during period


class QueryResponse(BaseModel):
    """Response from /query endpoint."""
    anchor: str  # UUID or user_id of grounding entity
    facts: list[dict] = []  # Structured facts with metadata (definition contains prose)
    preferred_names: dict = {}  # UUID → display name mapping for Filter's UUID resolution
    canonical_identity: Optional[str] = None  # Same as anchor, for backward compatibility
    # OWNER RULING (2026-08-20, additive option): `anchor`/`canonical_identity` KEEP the
    # raw entity UUID — consuming software (MCP slot resolution, OpenWebUI filter) keys on
    # it — and this ADDITIVE field carries the human-readable name ALONGSIDE it. Sourced
    # from the store's alias tables (entity_aliases, preferred-first; the user-identity
    # lane for the seat owner), never an enumerated list. None = honest absence (the
    # anchor is not a known entity or carries no surfaceable name) — a UUID is never the
    # value and no placeholder name is invented.
    anchor_name: Optional[str] = None
    attributes: dict = {}  # entity_id → {attr: value} mapping for attributes
    confidence_applied: bool = True
    staged_facts_count: int = 0  # Class C facts included
    error: Optional[str] = None
    alerts: list[dict] = []  # Active system alerts (e.g. Qdrant collection mismatch); empty = no issues
    # PART 2 (DESIGN-ingest-spine-and-temporal-recall §"RECALL-SIDE TEMPORAL ORDERING"):
    # True when the backend resolved a temporal pivot/ordinal and pre-sorted the dated
    # facts chronologically. Signals the recall layer to hand the model a timestamp-
    # prefixed, pre-sorted evidence list (Event #[i] [date]: …) so it never reorders.
    temporal_ordered: bool = False
    # QUERY INTENT ROUTER (DESIGN — "bright enough to answer the question"): the
    # question-shape TEMPLATE the recall was routed into — one of "scalar_lookup",
    # "temporal_first_last", "hierarchical_scope", "relational_walk". Observability only;
    # None when the router did not run (flag off / typed-walk early-return). Additive.
    template: Optional[str] = None
    # P1 — TEMPORAL CALCULATION (deterministic interval arithmetic). Set when the query
    # carried a calc intent ("how long ago", "how long between X and Y", "did X happen
    # before Y", "same week as", duration, Nth-between). The MATH is pure Python date
    # arithmetic over the real event_date column (no LLM); the answer CITES the source
    # event_dates (`cited_dates`). On an undated/unresolvable anchor it is a MISS-LOUD
    # result (`miss=True`, no fabricated number). None when no calc intent. Additive.
    #   {op, answer, value, unit, granule, miss, cited_dates, [miss_reason]}
    temporal_computation: Optional[dict] = None




