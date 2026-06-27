from typing import Optional
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
    status: str  # "stored" or "error"
    point_id: str  # Qdrant point UUID


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
